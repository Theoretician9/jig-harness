#!/usr/bin/env python3
"""Прибор задержки доставки сообщений владельца (спека мгновенной доставки §4.6, §5).

Читает метки журнала «доставка» (их пишут диспетчер и дверь pane.sh) и стенограммы
Claude Code. Пустой набор и мало данных — ОТКАЗ, а не зелёный: прежний прибор считал
постановку в очередь доставкой и давал «p90 12,5 с» вместо 308 с.

  delivery-latency.py --окно "<от>" "<до>" [--критерий К-1|К-2|К-3|К-6 | --потери]
                      [--стенограммы <каталог>]
  delivery-latency.py --след (--окно "<от>" "<до>" | --ворота) [--зазор <с>]

Время в --окно местное: «ГГГГ-ММ-ДД ЧЧ:ММ[:СС]». Последняя непустая строка вывода —
«ВЕРДИКТ <режим>: ПРОЙДЕН|ПРОВАЛ|ОТКАЗ — <причина>, n=<N>»; код 0 / 1 / 2.
"""
import argparse
import collections
import glob
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "lib"))
import config  # noqa: E402

LEGAL_REFUSALS = {"пауза", "ротация", "лимит"}
MIN_N = 20          # меньше — судить рано
WIDEN_DAYS = 3
GATE_WINDOW_DAYS = 7
GATE_YELLOW = 77  # код «жёлтый» ворот: виден, не зелёный, не красный
INF = float("inf")
CODES = {"ПРОЙДЕН": 0, "ПРОВАЛ": 1, "ОТКАЗ": 2}


def fmt(x):
    return "∞" if x == INF else f"{x:.2f}"


def parse_local(text):
    for form in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(text, form))
        except ValueError:
            pass
    sys.exit(f"прибор: время «{text}» не разобрано (ГГГГ-ММ-ДД ЧЧ:ММ[:СС])")


def percentile90(values):
    xs = sorted(values)
    return xs[max(0, math.ceil(0.9 * len(xs)) - 1)] if xs else None


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Marks:
    """Первая метка каждого вида на сообщение (дубли рестарта не в счёт)."""

    def __init__(self, path):
        self.rec, self.que, self.nok, self.kick, self.ref, self.att, self.done = {}, {}, {}, {}, {}, {}, {}
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            return
        with fh:
            for line in fh:
                self._take(line)

    def _take(self, line):
        try:
            row = json.loads(line)
        except ValueError:
            return
        event = str(row.get("event") or "")
        data = row.get("data") or {}
        if not event.startswith("delivery.") or not isinstance(data, dict):
            return
        kind = event[len("delivery."):]
        if kind in ("received", "queued", "no_kick", "attachment_started", "attachment_done"):
            table = {"received": self.rec, "queued": self.que, "no_kick": self.nok,
                     "attachment_started": self.att, "attachment_done": self.done}[kind]
            table.setdefault(str(data.get("ts")), data)
        elif kind in ("kick_sent", "kick_refused"):
            table = self.kick if kind == "kick_sent" else self.ref
            for ts in str(data.get("ts_список") or "").split(","):
                if ts:
                    table.setdefault(ts, data)

    def when(self, ts):
        """Момент сообщения: «когда» его received, у queued без received — сам ts."""
        if ts in self.rec:
            return to_float(self.rec[ts].get("когда"))
        return to_float(ts)

    def in_window(self, lo, hi):
        stamps = set(self.rec) | set(self.que)
        return sorted(ts for ts in stamps if (w := self.when(ts)) is not None and lo <= w <= hi)

    def by_update(self, lo, hi):
        """update_id → ts его received в окне; без update_id ключом служит сам ts."""
        groups = collections.defaultdict(list)
        for ts in self.in_window(lo, hi):
            if ts in self.rec:
                groups[str(self.rec[ts].get("update_id") or ts)].append(ts)
        return groups

    def has_final(self, ts):
        return ts in self.kick or ts in self.ref or ts in self.nok

    def legal_refusal(self, ts):
        return str((self.ref.get(ts) or {}).get("почему")) in LEGAL_REFUSALS

    def delay(self, ts, since):
        """Сколько прошло от since до толчка; нет толчка — ∞."""
        sent = to_float((self.kick.get(ts) or {}).get("когда"))
        return INF if sent is None else sent - since


def judged_received(marks, lo, hi):
    """Принятые в окне, которым толчок положен: без no_kick, без законного отказа и без вложений (их задержка — расшифровка)."""
    return [ts for ts in marks.in_window(lo, hi)
            if ts in marks.rec and ts not in marks.nok and ts not in marks.att and not (ts not in marks.kick and marks.legal_refusal(ts))]


def widened(judge, lo, hi):
    """Набор мал — окно растёт до трёх суток от «до»; сообщает, если выросло."""
    stamps = judge(lo, hi)
    if len(stamps) >= MIN_N:
        return stamps, lo
    wide_lo = min(lo, hi - WIDEN_DAYS * 86400)
    stamps = judge(wide_lo, hi)
    print(f"в окне меньше {MIN_N} сообщений — окно расширено до {WIDEN_DAYS} суток от «до»")
    return stamps, wide_lo


def verdict(mode, word, reason, n):
    print(f"ВЕРДИКТ {mode}: {word} — {reason}, n={n}")
    return CODES[word]


def share_line(name, delays, limit):
    ok = sum(1 for d in delays if d <= limit)
    print(f"{name}: доля ≤ {limit} с — {ok}/{len(delays)} ({100 * ok / len(delays):.0f} %), "
          f"максимум {fmt(max(delays))} с, p90 {fmt(percentile90(delays))} с")


def criterion_k1(marks, lo, hi):
    stamps, _ = widened(lambda a, b: judged_received(marks, a, b), lo, hi)
    if len(stamps) < MIN_N:
        return verdict("К-1", "ОТКАЗ", f"мало данных (нужно {MIN_N})", len(stamps))
    delays = [marks.delay(ts, marks.when(ts)) for ts in stamps]
    share_line("received→kick_sent", delays, 2.0)
    for ts, d in zip(stamps, delays):
        if d == INF:
            print(f"  без толчка: {ts} (отказ: {(marks.ref.get(ts) or {}).get('почему', 'нет метки')}) — ∞")
    good = sum(1 for d in delays if d <= 2.0) / len(delays) >= 0.9 and max(delays) <= 4.0
    reason = "доля ≤ 2 с не ниже 90 %, максимум ≤ 4 с" if good else "доля ≤ 2 с ниже 90 % или максимум выше 4 с"
    return verdict("К-1", "ПРОЙДЕН" if good else "ПРОВАЛ", reason, len(delays))


def ntp_synchronized():
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"timedatectl не ответил ({type(exc).__name__})"
    if out.returncode != 0:
        return False, f"timedatectl отказал (код {out.returncode})"
    answer = out.stdout.strip()
    return answer == "yes", f"NTPSynchronized={answer or 'пусто'}"


def criterion_k2(marks, lo, hi):
    synced, detail = ntp_synchronized()
    if not synced:
        return verdict("К-2", "ОТКАЗ", f"часы не подтверждены по NTP ({detail}): разность с временем телеграма не судится", 0)

    def judge(a, b):
        return [ts for ts in judged_received(marks, a, b) if to_float(marks.rec[ts].get("tg_date")) is not None]
    stamps, _ = widened(judge, lo, hi)
    if len(stamps) < MIN_N:
        return verdict("К-2", "ОТКАЗ", f"мало данных (нужно {MIN_N})", len(stamps))
    delays = [marks.delay(ts, to_float(marks.rec[ts]["tg_date"])) for ts in stamps]
    p90 = percentile90(delays)
    print(f"tg_date→kick_sent: p90 {fmt(p90)} с, максимум {fmt(max(delays))} с")
    good = p90 <= 3.0
    return verdict("К-2", "ПРОЙДЕН" if good else "ПРОВАЛ", f"p90 {fmt(p90)} с, предел 3 с", len(delays))


def criterion_k3(marks, lo, hi):
    stamps = [ts for ts in marks.in_window(lo, hi) if ts in marks.rec and ts not in marks.nok]
    if len(stamps) < MIN_N:
        return verdict("К-3", "ОТКАЗ", f"мало данных (нужно {MIN_N})", len(stamps))
    bare = [ts for ts in stamps if ts not in marks.kick and not marks.legal_refusal(ts)]
    for ts in bare:
        print(f"  ни толчка, ни законного отказа: {ts}")
    word = "ПРОВАЛ" if bare else "ПРОЙДЕН"
    return verdict("К-3", word, f"без толчка и без причины: {len(bare)}", len(stamps))


def skipped_updates(log_dir, marks, lo, hi):
    """update_id из tg-skipped-*.json: ядовитые, диспетчер их пропустил без следа в метках.
    Файл сам времени приёма не несёт: берутся те, чей id не меньше первого принятого в окне."""
    seen = {to_float(d.get("update_id")) for d in marks.rec.values()}
    window_ids = [to_float(marks.rec[ts].get("update_id")) for ts in marks.in_window(lo, hi) if ts in marks.rec]
    floor = min((i for i in window_ids if i is not None), default=None)
    found = []
    for path in glob.glob(os.path.join(log_dir, "tg-skipped-*.json")):
        m = re.search(r"tg-skipped-(\d+)\.json$", path)
        if not m or int(m.group(1)) in seen:
            continue
        uid = int(m.group(1))
        if (floor is not None and uid >= floor) or (floor is None and os.path.getmtime(path) >= lo):
            found.append(uid)
    return sorted(found)


def lost_messages(marks, stamps):
    lost = []
    for ts in stamps:
        if ts in marks.nok:
            continue
        no_queue = ts in marks.rec and ts not in marks.que
        no_kick = ts in marks.que and ts not in marks.kick and not marks.legal_refusal(ts)
        if no_queue or no_kick:
            rec = marks.rec.get(ts) or {}
            lost.append((rec.get("update_id") or ts, "нет queued" if no_queue else "нет толчка и законного отказа"))
    return lost


def update_gaps(marks, stamps):
    ids = sorted({int(u) for ts in stamps if ts in marks.rec and (u := to_float(marks.rec[ts].get("update_id"))) is not None})
    return [(a, b) for a, b in zip(ids, ids[1:]) if b - a > 1]


def criterion_losses(marks, log_dir, lo, hi):
    stamps = marks.in_window(lo, hi)
    skipped = skipped_updates(log_dir, marks, lo, hi)
    n = len([ts for ts in stamps if ts in marks.rec]) + len(skipped)
    if n == 0:
        return verdict("К-5", "ОТКАЗ", "в окне нет ни одного принятого сообщения", 0)
    lost = lost_messages(marks, stamps) + [(uid, "ядовитый update без следа") for uid in skipped]
    print(f"потерь {len(lost)}")
    for ident, why in lost:
        print(f"  потеря: {ident} — {why}")
    for a, b in update_gaps(marks, stamps):
        print(f"разрыв update_id: {a} → {b} (не потеря: могли прийти не сообщения)")
    word = "ПРОВАЛ" if lost else "ПРОЙДЕН"
    return verdict("К-5", word, f"потерь {len(lost)}", n)


# ── след: каждое принятое сообщение имеет конечную метку ──────────────────

ATTACHMENT_CEILING = 900  # потолок расшифровки вложения, спека §4.3 п.7


def trace_stage(marks, tss):
    """Последний этап сообщения без следа."""
    if any(ts in marks.done for ts in tss):
        return "итог без толчка"
    if any(ts in marks.att for ts in tss):
        return "вложение без итога"
    if any(ts in marks.que for ts in tss):
        return "queued без толчка"
    return "нет queued"


def in_transit(marks, tss, now, gap):
    """Конечной метки ещё может не быть: после приёма (у вложения — после итога) прошло мало."""
    received = max(marks.when(ts) for ts in tss)
    done = [w for ts in tss if ts in marks.done and (w := to_float(marks.done[ts].get("когда"))) is not None]
    if done:
        return now - max(done) < gap
    if any(ts in marks.att for ts in tss):
        return now - received < ATTACHMENT_CEILING
    return now - received < gap


def criterion_trace(marks, lo, hi, gap, now):
    traced, bare, moving = 0, [], 0
    for uid, tss in sorted(marks.by_update(lo, hi).items()):
        if any(marks.has_final(ts) for ts in tss):
            traced += 1
        elif in_transit(marks, tss, now, gap):
            moving += 1
        else:
            bare.append((uid, tss[0], trace_stage(marks, tss)))
    for uid, ts, stage in bare:
        print(f"  без следа: update_id={uid} ts={ts} — {stage}")
    print(f"без следа: {len(bare)}")
    print(f"в пути: {moving}")
    n = traced + len(bare)
    if n == 0:
        return verdict("след", "ОТКАЗ", "судить нечего: в окне нет принятых сообщений вне пути", 0)
    word = "ПРОВАЛ" if bare else "ПРОЙДЕН"
    return verdict("след", word, f"без следа {len(bare)}", n)


# ── очередь Claude Code (способ замера 27.10: in_context.py) ───────────────

def norm(text):
    return re.sub(r"\s+", " ", text or "").strip()


def iso_epoch(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def transcripts_dir(arg):
    if arg:
        return arg
    if os.environ.get("DELIVERY_TRANSCRIPTS_DIR"):
        return os.environ["DELIVERY_TRANSCRIPTS_DIR"]
    project = str(config.конфиг().get("PROJECT_DIR") or "").replace("/", "-")
    return os.path.expanduser(f"~/.claude/projects/{project}")


def window_messages(marks, log_dir, lo, hi):
    """ts → {«rec»: момент приёма, «mid»: message_id}; метки плюс строки диспетчера."""
    msgs = {}
    for ts in marks.in_window(lo, hi):
        mid = (marks.que.get(ts) or marks.rec.get(ts) or {}).get("message_id")
        msgs[ts] = {"rec": marks.when(ts), "mid": str(mid)}
    pattern = re.compile(r"в очередь: \S*/(\d{10}\.\d{9})\.txt \(message_id=(\d+)\)")
    try:
        lines = open(os.path.join(log_dir, "tg-dispatcher.log"), encoding="utf-8", errors="replace")
    except OSError:
        return msgs
    with lines:
        for line in lines:
            m = pattern.search(line)
            if m and lo <= float(m.group(1)) <= hi:
                msgs.setdefault(m.group(1), {"rec": float(m.group(1)), "mid": m.group(2)})
    return msgs


def add_snippets(msgs, log_dir):
    texts = {}
    try:
        with open(os.path.join(log_dir, "inbox.jsonl"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                texts[str(row.get("message_id"))] = norm(row.get("text"))[:40]
    except OSError:
        pass
    for info in msgs.values():
        snippet = texts.get(info["mid"], "")
        info["snip"] = snippet if len(snippet) >= 12 else None


def scan_transcripts(directory, since):
    """Операции очереди: (постановка, выход, содержимое) и все записи user (время, тело)."""
    queued, users = [], []
    for path in glob.glob(os.path.join(directory, "*.jsonl")):
        if os.path.getmtime(path) < since:
            continue
        pending = []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "queue-operation" not in line and '"user"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                    at = iso_epoch(obj["timestamp"])
                except (ValueError, KeyError, TypeError):
                    continue
                if obj.get("isSidechain"):
                    continue
                if obj.get("type") == "queue-operation":
                    take_queue_operation(obj, at, pending, queued)
                elif obj.get("type") == "user":
                    users.append((at, norm(json.dumps(obj.get("message", ""), ensure_ascii=False))))
    return queued, users


def take_queue_operation(obj, at, pending, queued):
    op = obj.get("operation")
    if op == "enqueue":
        pending.append((at, norm(obj.get("content"))))
    elif op == "remove":
        content = norm(obj.get("content"))
        for i, (enq, queued_content) in enumerate(pending):
            if queued_content == content:
                queued.append((enq, at, queued_content))
                pending.pop(i)
                break
    elif op == "dequeue" and pending:
        enq, content = pending.pop(0)
        queued.append((enq, at, content))


def mentions(ts, info, body):
    return ts in body or bool(info.get("snip") and info["snip"] in body)


def queue_waits(msgs, queued):
    """ts → (ожидание в очереди, момент выхода) для сообщений, прошедших очередь."""
    out = {}
    for ts, info in msgs.items():
        hits = [q for q in queued if info["rec"] - 5 <= q[0] <= info["rec"] + 600 and mentions(ts, info, q[2])]
        if hits:
            enq, leave, _ = min(hits, key=lambda q: q[0])
            out[ts] = (leave - enq, leave)
    return out


def criterion_k6(marks, log_dir, lo, hi, transcripts):
    msgs = window_messages(marks, log_dir, lo, hi)
    add_snippets(msgs, log_dir)
    queued, _ = scan_transcripts(transcripts_dir(transcripts), lo - 3600)
    waits = [w for w, _ in queue_waits(msgs, queued).values()]
    if len(waits) < 5:
        return verdict("К-6", "ОТКАЗ", f"в очереди Claude Code меньше 5 сообщений (в окне {len(msgs)})", len(waits))
    p90, worst = percentile90(waits), max(waits)
    print(f"ожидание в очереди Claude Code: p90 {fmt(p90)} с, максимум {fmt(worst)} с, "
          f"медиана {fmt(statistics.median(waits))} с")
    good = p90 <= 5.0 and worst <= 15.0
    return verdict("К-6", "ПРОЙДЕН" if good else "ПРОВАЛ", f"p90 {fmt(p90)} с (предел 5), максимум {fmt(worst)} с (предел 15)", len(waits))


# ── сводка ──────────────────────────────────────────────────────────────────

def segment_line(name, values):
    if not values:
        print(f"{name} n=0")
        return
    print(f"{name} n={len(values)} med={statistics.median(values):.2f} "
          f"p90={percentile90(values):.2f} max={max(values):.2f}")


def context_delays(marks, msgs, transcripts, lo):
    """received → в контексте: выход из очереди, иначе первая запись user с ts/куском."""
    queued, users = scan_transcripts(transcripts_dir(transcripts), lo - 3600)
    waits = queue_waits(msgs, queued)
    out = []
    for ts, info in msgs.items():
        if ts in waits:
            out.append(waits[ts][1] - info["rec"])
            continue
        direct = [t for t, body in users if t >= info["rec"] - 5 and mentions(ts, info, body)]
        if direct:
            out.append(min(direct) - info["rec"])
    return out


def summary(marks, log_dir, lo, hi, transcripts):
    stamps = [ts for ts in marks.in_window(lo, hi) if ts in marks.rec]
    if not stamps:
        return verdict("сводка", "ОТКАЗ", "в окне нет ни одного принятого сообщения", 0)
    segment_line("tg_date→received", [marks.when(ts) - d for ts in stamps
                                      if (d := to_float(marks.rec[ts].get("tg_date"))) is not None])
    segment_line("received→kick_sent", [d for ts in stamps if (d := marks.delay(ts, marks.when(ts))) != INF])
    msgs = window_messages(marks, log_dir, lo, hi)
    add_snippets(msgs, log_dir)
    segment_line("received→в контексте", context_delays(marks, msgs, transcripts, lo))
    reasons = collections.Counter(str(marks.nok[ts].get("почему")) for ts in stamps if ts in marks.nok)
    reasons.update(f"отказ: {marks.ref[ts].get('почему')}" for ts in stamps if ts in marks.ref and ts not in marks.kick)
    print("отказы по причинам: " + (", ".join(f"{k} — {v}" for k, v in reasons.most_common()) or "нет"))
    return verdict("сводка", "ПРОЙДЕН", "метки есть", len(stamps))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--окно", nargs=2, metavar=("ОТ", "ДО"), dest="window")
    ap.add_argument("--след", action="store_true", dest="trace")
    ap.add_argument("--зазор", type=float, default=120, dest="gap")
    ap.add_argument("--ворота", action="store_true", dest="gate")
    ap.add_argument("--критерий", choices=["К-1", "К-2", "К-3", "К-6"], dest="criterion")
    ap.add_argument("--потери", action="store_true", dest="losses")
    ap.add_argument("--стенограммы", dest="transcripts")
    args = ap.parse_args()
    if args.gate and not args.trace:
        ap.error("--ворота работает только с --след")
    if not args.window and not args.gate:
        ap.error("нужен --окно (или --ворота вместе с --след)")
    now = time.time()
    if args.window:
        lo, hi = parse_local(args.window[0]), parse_local(args.window[1])
    else:
        lo, hi = now - GATE_WINDOW_DAYS * 86400, now
    log_dir = os.environ.get("HARNESS_LOG_DIR") or config.log_dir()
    marks = Marks(os.path.join(log_dir, "доставка.jsonl"))
    if args.trace:
        code = criterion_trace(marks, lo, hi, args.gap, now)
        if args.gate and code == CODES["ОТКАЗ"]:
            print("для ворот ОТКАЗ показан жёлтым: журнал пуст — сверять нечего, коммит не валит")
            return GATE_YELLOW
        return code
    if args.losses:
        return criterion_losses(marks, log_dir, lo, hi)
    if args.criterion == "К-6":
        return criterion_k6(marks, log_dir, lo, hi, args.transcripts)
    simple = {"К-1": criterion_k1, "К-2": criterion_k2, "К-3": criterion_k3}
    if args.criterion:
        return simple[args.criterion](marks, lo, hi)
    return summary(marks, log_dir, lo, hi, args.transcripts)


if __name__ == "__main__":
    sys.exit(main())
