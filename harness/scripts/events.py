#!/usr/bin/env python3
"""Запрос агента к журналам харнеса: ответ — сводка с бюджетом в токенах.

    python3 scripts/events.py status            что сейчас не так (первый запрос смены)
    python3 scripts/events.py task <id>         что было с задачей
    python3 scripts/events.py refusals          где отказы
    python3 scripts/events.py journals          какие журналы и в каком виде
    python3 scripts/events.py lost [--ack <разбор>]   что потеряно
    python3 scripts/events.py export --out <файл>     поток для скрипта (в контекст не идёт)
    python3 scripts/events.py index [--rebuild]       догон сводки
    python3 scripts/events.py install                 каталоги состояния и права журналов

Общие ключи: --since 24h|7d|ГГГГ-ММ-ДД, --бюджет N, --подробно (бюджет ×4),
--json (один объект: verdict, counts, items, truncated, next).
Код возврата: 0 — ответ дан (в том числе «событий нет»), 2 — неверный вызов,
1 — сводку прочитать не удалось.

Агент платит за каждый знак ответа в каждом следующем шаге сессии, поэтому
ответ собирается блоками по приоритету и режется по бюджету с названием
урезанного; пустой ответ называет, сколько просмотрено, и подсказки не выдумывает.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent / "lib"
sys.path.insert(0, str(LIB_DIR))
_spec = importlib.util.spec_from_file_location("events_lib", LIB_DIR / "events.py")
lib = importlib.util.module_from_spec(_spec)
sys.modules["events_lib"] = lib
_spec.loader.exec_module(lib)
from token_estimate import tokens  # noqa: E402

BUDGETS = {"status": 150, "task": 400, "journals": 300, "lost": 200, "refusals": 300,
           "export": 50, "index": 50, "daemon": 300, "install": 100}
DEFAULT_SINCE = {"status": "24h", "task": "30d", "refusals": "7d", "journals": "7d",
                 "export": "7d", "lost": "7d", "index": "7d", "daemon": "3d", "install": "1d"}
TASK_ID = re.compile(r"[^\s/\x00-\x1f]+")
TOP_KEYS = 10

NOW = dt.datetime.now().astimezone()


@dataclass
class Answer:
    verdict: str
    lines: list[str] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    next: list[str] = field(default_factory=list)
    empty: bool = False
    more_cmd: str = "events.py --help"


# ───────────────────────── формат ─────────────────────────

GUESS_FROM = ("history", "inferred")


def stem(src: str) -> str:
    return Path(src).stem


def fmt_time(moment: dt.datetime) -> str:
    local = moment.astimezone()
    if local.date() == NOW.date():
        return local.strftime("%H:%M")
    return local.strftime("%d.%m %H:%M")


def tz_label() -> str:
    raw = NOW.strftime("%z")
    return f"{raw[:3]}:{raw[3:]}"


def parse_since(text: str) -> dt.datetime:
    m = re.fullmatch(r"(\d+)([mhdw])", text)
    if m:
        unit = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[m.group(2)]
        return NOW - dt.timedelta(**{unit: int(m.group(1))})
    return dt.datetime.fromisoformat(text).astimezone()


def since_arg(text: str) -> str:
    """Неверное окно — ошибка вызова (rc 2), а не «не прочитано»."""
    try:
        parse_since(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"неверное окно «{text}» (24h, 7d или ГГГГ-ММ-ДД)") from exc
    return text


def top_line(label: str, counter: dict[str, int], limit: int = 3) -> str:
    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = ", ".join(f"{stem(k)} {n}" for k, n in ordered[:limit])
    rest = len(ordered) - limit
    tail = f", ещё {rest}" if rest > 0 else ""
    return f"{label} {sum(counter.values())}: {shown}{tail}"


def fit(answer: Answer, budget: int) -> tuple[list[str], list[str], int]:
    """Блоки по приоритету: что не влезло — не печатается, а называется числом.
    Возвращает (строки ответа, оставшиеся строки блоков, срезано)."""
    kept = list(answer.lines)
    hint = "дальше: " + "; ".join(answer.next) if answer.next and not answer.empty else None
    cut = 0

    def build(with_hint: bool) -> list[str]:
        out = [answer.verdict, *kept]
        if cut:
            out.append(f"срезано: {cut} строк; полнее: {answer.more_cmd}")
        if hint and with_hint:
            out.append(hint)
        return out

    while kept and tokens("\n".join(build(True))) > budget:
        kept.pop()
        cut += 1
    out = build(True)
    if tokens("\n".join(out)) > budget:
        out = build(False)
    text = "\n".join(out)
    while tokens(text) > budget and len(out[0]) > 20:
        out[0] = out[0][: int(len(out[0]) * 0.8)].rstrip() + "…"
        text = "\n".join(out)
    return out, kept, cut


def emit_answer(answer: Answer, budget: int, as_json: bool) -> int:
    out, kept, cut = fit(answer, budget)
    if as_json:
        print(json.dumps({"verdict": answer.verdict, "counts": answer.counts, "items": kept,
                          "truncated": cut, "next": [] if answer.empty else answer.next},
                         ensure_ascii=False, separators=(",", ":")))
    else:
        print("\n".join(out))
    return 0


# ───────────────────────── сводка ─────────────────────────

def refresh_index() -> tuple[dict, str | None]:
    """Догон сводки; занято — читаем как есть и говорим об этом."""
    result = lib.index_update()
    return result, ("сводка занята другим запросом — цифры могут отставать" if result.get("busy") else None)


def scanned() -> tuple[int, int]:
    meta = lib.index_meta()
    return len(meta), sum(m.get("lines", 0) for m in meta.values())


def by_src(totals: dict, outcome: str) -> dict[str, int]:
    counter: dict[str, int] = {}
    for (oc, src, _event, _task), n in totals.items():
        if oc == outcome:
            counter[src] = counter.get(src, 0) + n
    return counter


def lost_by_reason(rows: list[dict]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for r in rows:
        grouped.setdefault(r.get("reason", "?"), []).append(r.get("journal", "?"))
    return grouped


# ───────────────────────── команды ─────────────────────────

def cmd_status(args) -> Answer:
    result, warn = refresh_index()
    since = parse_since(args.since)
    totals, _last = lib.window_counts(since, NOW)
    refused, errors = by_src(totals, "refused"), by_src(totals, "error")
    unacked = lib.lost_unacked()
    journals, lines = scanned()
    events_total = sum(totals.values())
    counts = {"events": events_total, "refused": sum(refused.values()), "error": sum(errors.values()),
              "lost": len(unacked), "journals": journals, "lines": lines}
    body: list[str] = []
    if warn:
        body.append(warn)
    if unacked:
        reasons = lost_by_reason(unacked)
        body.append(f"потеряно записей {len(unacked)}: " + ", ".join(
            f"{r} {len(js)}" for r, js in sorted(reasons.items())))
    if errors:
        body.append(top_line("ошибок", errors))
    if refused:
        body.append(top_line("отказов", refused))
    bad = bool(unacked or errors)
    hints: list[str] = []
    if unacked:
        hints.append("events.py lost")
    if refused:
        hints.append(f"events.py refusals --src {stem(max(refused, key=refused.get))} --by event")
    elif errors:
        hints.append(f"events.py refusals --src {stem(max(errors, key=errors.get))} --by event")
    if bad:
        verdict = f"не в норме за {args.since} (пояс {tz_label()})"
    else:
        verdict = (f"всё в норме за {args.since}, просмотрено {journals} журналов, {lines} строк "
                   f"(пояс {tz_label()})")
    return Answer(verdict, body, counts, hints, empty=(events_total == 0 and not unacked),
                  more_cmd="events.py status --подробно")


def cmd_task(args) -> Answer:
    if not TASK_ID.fullmatch(args.id):
        raise SystemExit(usage_error(f"task: неверный id задачи «{args.id}»"))
    refresh_index()
    since = parse_since(args.since)
    rows = lib.task_events(args.id, since, NOW)
    journals, lines = scanned()
    if not rows:
        return Answer(f"по задаче {args.id} за {args.since} событий нет; просмотрено {journals} журналов, "
                      f"{lines} строк", counts={"events": 0, "journals": journals, "lines": lines}, empty=True)
    facts = [r for r in rows if r["task_from"] not in GUESS_FROM]
    guesses = [r for r in rows if r["task_from"] in GUESS_FROM]
    spent_fact = sum(int(r.get("n") or 0) for r in facts)
    spent_guess = sum(int(r.get("n") or 0) for r in guesses)
    outcomes: dict[str, int] = {}
    for r in rows:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    per_src: dict[str, list[dict]] = {}
    for r in facts:
        per_src.setdefault(r["src"], []).append(r)
    body = ["исходы: " + ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items(), key=lambda kv: -kv[1]))]
    failures = [r for r in rows if r["outcome"] in ("refused", "error")][-3:]
    for r in failures:
        why = f" — {r['reason']}" if r.get("reason") else ""
        body.append(f"{r['outcome']} {fmt_time(r['when'])} {stem(r['src'])} {r['event']}{why}")
    for src, items in sorted(per_src.items(), key=lambda kv: kv[1][0]["when"]):
        body.append(f"{stem(src)} {fmt_time(items[0]['when'])}–{fmt_time(items[-1]['when'])} ×{len(items)}")
    if guesses:
        names: dict[str, int] = {}
        for r in guesses:
            names[r["src"]] = names.get(r["src"], 0) + 1
        body.append("приписано (догадка по истории коммитов, сессии): " + ", ".join(
            f"{stem(s)} ×{n}" for s, n in sorted(names.items())))
    if spent_fact or spent_guess:
        body.append(f"расход: факт {spent_fact}, приписано {spent_guess} ток.")
    if args.feed:
        body += [f"{fmt_time(r['when'])} {stem(r['src'])} {r['event']} {r['outcome']}" for r in rows]
    srcs = {r["src"] for r in rows}
    verdict = f"задача {args.id}, {args.since}: {len(rows)} событий, {len(srcs)} механизмов (пояс {tz_label()})"
    hints = [f"events.py task {args.id} --лента"]
    if failures:
        hints.append(f"events.py refusals --by src --since {args.since}")
    return Answer(verdict, body, {"events": len(rows), "facts": len(facts), "guesses": len(guesses),
                                  "spent_fact": spent_fact, "spent_guess": spent_guess, "outcomes": outcomes}, hints, more_cmd=f"events.py task {args.id} --подробно")


def cmd_refusals(args) -> Answer:
    refresh_index()
    since = parse_since(args.since)
    totals, last = lib.window_counts(since, NOW)
    wanted = args.src
    grouped: dict[str, int] = {}
    for (oc, src, event, task), n in totals.items():
        if oc != "refused" or (wanted and stem(src) != stem(wanted)):
            continue
        key = {"src": src, "event": event, "task": task}[args.by]
        grouped[key] = grouped.get(key, 0) + n
    journals, lines = scanned()
    if not grouped:
        return Answer(f"отказов за {args.since} нет; просмотрено {journals} журналов, {lines} строк",
                      counts={"refused": 0}, empty=True)
    ordered = sorted(grouped.items(), key=lambda kv: (-kv[1], kv[0]))
    total = sum(grouped.values())
    body = [f"итог: {total} отказов в {len(grouped)} ключах"]
    for key, n in ordered[:TOP_KEYS]:
        label = stem(key) if args.by == "src" else key
        rec = last.get(key) if args.by == "src" else None
        tail = f" · {fmt_time(dt.datetime.fromisoformat(rec[0]))} {rec[1]} {rec[2]}".rstrip() if rec else ""
        body.append(f"{label} {n}{tail}")
    rest = ordered[TOP_KEYS:]
    if rest:
        body.append(f"ещё {len(rest)} ключей на {sum(n for _, n in rest)} отказов")
    top_key = ordered[0][0]
    if args.by == "src":
        hints = [f"events.py refusals --src {stem(top_key)} --by event"]
    elif args.by == "event":
        hints = ["events.py refusals --by task"]
    else:
        runnable = top_key != "-" and TASK_ID.fullmatch(top_key) and " " not in top_key
        hints = [f"events.py task {top_key}"] if runnable else ["events.py refusals --by src"]
    return Answer(f"отказы за {args.since} по {args.by} (пояс {tz_label()})", body,
                  {"refused": total, "keys": len(grouped)}, hints, more_cmd="events.py refusals --подробно")


def cmd_journals(args) -> Answer:
    refresh_index()
    meta = lib.index_meta()
    journals, lines = scanned()
    deviations: list[tuple[int, str]] = []
    v1_total = events_total = 0
    for name, m in meta.items():
        v1_total += m.get("v1", 0)
        events_total += m.get("events", 0)
        if m.get("broken"):
            deviations.append((0, f"{name}: битых {m['broken']} из {m['lines']} строк"))
        elif m.get("no_time"):
            deviations.append((1, f"{name}: без времени, не приводится ({m['lines']} строк)"))
    deviations.sort()
    body = [d for _, d in deviations]
    body.append(f"итог: строк v1 {v1_total} из {events_total} событий; образцов потока пропущено "
                f"{sum(1 for m in meta.values() if m.get('sample'))}")
    verdict = (f"журналов {journals}, строк {lines}; отклонений {len(deviations)}" if deviations
               else f"журналов {journals}, строк {lines}; отклонений нет")
    return Answer(verdict, body, {"journals": journals, "lines": lines, "deviations": len(deviations)},
                  ["events.py status"], empty=(journals == 0), more_cmd="events.py journals --подробно")


def cmd_lost(args) -> Answer:
    if args.ack is not None:
        lib.lost_ack(args.ack)
        return Answer(f"разбор записан: {args.ack[:80]}; потерь без разбора нет", empty=True)
    rows = lib.lost_unacked()
    if not rows:
        return Answer("потерь без разбора нет", counts={"lost": 0}, empty=True)
    grouped = lost_by_reason(rows)
    body = []
    for reason, journals in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        names = sorted(set(journals))
        body.append(f"{reason} {len(journals)}: {', '.join(names[:4])}")
    last_ts = max(r.get("ts", "") for r in rows)
    try:
        body.append("последняя: " + fmt_time(dt.datetime.fromisoformat(last_ts)))
    except ValueError:
        pass
    return Answer(f"потеряно записей {len(rows)} (без разбора)", body, {"lost": len(rows)},
                  ['events.py lost --ack "что разобрано"'], more_cmd="events.py lost --подробно")


def cmd_index(args) -> Answer:
    result = lib.index_update(rebuild=args.rebuild)
    journals, lines = scanned()
    if result.get("busy"):
        return Answer("сводка занята другим запросом", empty=True)
    note = f"; пересборка: {result['rebuilt']}" if result.get("rebuilt") else ""
    return Answer(f"сводка: журналов {journals}, строк {lines}{note}", counts={"journals": journals,
                                                                              "lines": lines}, empty=True)


def cmd_install(args) -> Answer:
    result = lib.install()
    if result["failed"]:
        raise RuntimeError(f"права не приведены: {', '.join(result['failed'][:5])}")
    return Answer(f"установлено: каталогов {result['dirs']}, журналов {result['journals']}",
                  counts=result, empty=True)


def cmd_daemon(args) -> Answer:
    return Answer(f"меток запусков демонов пока нет — по «{args.name}» судить нечем", empty=True)


def cmd_export(args) -> Answer:
    since, until = parse_since(args.since), NOW if not args.until else parse_since(args.until)
    count = 0
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh) if args.format == "csv" else None
        if writer:
            writer.writerow(["time", "journal", "src", "event", "outcome", "task", "task_from"])
        for ev in sorted(lib.read(since, until), key=lambda e: e.time):
            if args.task and ev.task != args.task:
                continue
            if writer:
                writer.writerow([ev.time.isoformat(), ev.journal, ev.src, ev.event, ev.outcome,
                                 ev.task or "", ev.task_from])
            else:
                fh.write(json.dumps({"time": ev.time.isoformat(), "journal": ev.journal, "src": ev.src,
                                     "event": ev.event, "outcome": ev.outcome, "task": ev.task,
                                     "task_from": ev.task_from, "data": ev.data}, ensure_ascii=False,
                                    default=str) + "\n")
            count += 1
    size = os.path.getsize(args.out)
    return Answer(f"записано {count} событий, {size} байт: {args.out}", counts={"events": count, "bytes": size},
                  empty=True)


COMMANDS = {"status": cmd_status, "task": cmd_task, "refusals": cmd_refusals, "journals": cmd_journals,
            "lost": cmd_lost, "index": cmd_index, "daemon": cmd_daemon, "export": cmd_export,
            "install": cmd_install}


# ───────────────────────── разбор вызова ─────────────────────────

def usage_error(message: str) -> int:
    print(f"events.py: {message}\nкоманды: {', '.join(COMMANDS)} (events.py --help)", file=sys.stderr)
    return 2


def run_lost_gate() -> int:
    """Вывод ворот, а не ответ агенту: бюджет не режет красные строки."""
    code, lines = lib.lost_gate()
    print("\n".join(lines))
    print("КРАСНЫЙ" if code else "зелёный")
    return code


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--since", default=None, type=since_arg, help="окно: 24h, 7d, 30d или ГГГГ-ММ-ДД")
    common.add_argument("--бюджет", dest="budget", type=int, default=None, help="потолок ответа в токенах")
    common.add_argument("--подробно", dest="verbose", action="store_true", help="бюджет ×4")
    common.add_argument("--json", dest="as_json", action="store_true", help="один объект JSON")
    parser = argparse.ArgumentParser(prog="events.py", description="Запрос агента к журналам харнеса.")
    sub = parser.add_subparsers(dest="command", required=True, metavar="команда")
    sub.add_parser("status", parents=[common], help="что сейчас не так")
    p = sub.add_parser("task", parents=[common], help="что было с задачей")
    p.add_argument("id")
    p.add_argument("--лента", dest="feed", action="store_true", help="события по одному в строке")
    p = sub.add_parser("refusals", parents=[common], help="где отказы")
    p.add_argument("--by", choices=("src", "event", "task"), default="src")
    p.add_argument("--src", default=None)
    sub.add_parser("journals", parents=[common], help="какие журналы и в каком виде")
    p = sub.add_parser("lost", parents=[common], help="что потеряно")
    p.add_argument("--ack", default=None, help="записать разбор потерь")
    p.add_argument("--gate", action="store_true", help="ворота G3: код возврата 1 при потере без разбора или тишине")
    p = sub.add_parser("index", parents=[common], help="догон сводки")
    p.add_argument("--rebuild", action="store_true")
    sub.add_parser("install", parents=[common], help="привести .events и права журналов (идемпотентно)")
    p = sub.add_parser("daemon", parents=[common], help="почему встал демон")
    p.add_argument("name")
    p = sub.add_parser("export", parents=[common], help="поток для скрипта в файл")
    p.add_argument("--until", default=None)
    p.add_argument("--task", default=None)
    p.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    p.add_argument("--out", required=True)
    return parser


def main(argv: list[str]) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    args.since = args.since or DEFAULT_SINCE[args.command]
    if args.command == "lost" and args.gate:
        return run_lost_gate()
    budget = args.budget or BUDGETS[args.command] * (4 if args.verbose else 1)
    try:
        answer = COMMANDS[args.command](args)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    except Exception as exc:  # noqa: BLE001 — не прочитано называется причиной, а не трассировкой
        print(f"не прочитано: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return emit_answer(answer, budget, args.as_json)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
