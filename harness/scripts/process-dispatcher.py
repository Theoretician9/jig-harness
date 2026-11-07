#!/usr/bin/env python3
"""process-dispatcher.py — диспетчер процессов: загрузка сервера каждым процессом харнеса.

Что делает: раз в минуту (--замер, под кроном через run-background.sh) делит CPU
и память сервера между процессами библиотеки и дописывает строку process.sample
в process-load.jsonl; по запросу показывает «сейчас», «за 24ч|7д» и вносит
недельный замер в process-library.json. Разбор cgroup и дерева процессов — в
scripts/lib/process_load.py: cgroup даёт точный итог, дерево его только делит.

Режимы:
  --замер            один проход сборщика (под замком, с ротацией журналов);
  --сейчас           два скана с паузой 2 с, таблица;
  --за 24ч|7д        свёртка журнала;
  --в-библиотеку     замер за неделю → доля_нагрузки.замер в process-library.json;
  --витрина          собрать файл витрины для панели (проход --замер зовёт то же раз в ~10 минут);
  --selftest         разбор на искусственном снимке.

Двери проб: --proc, --cgroup, --библиотека, --проект, --транскрипты, --журналы;
окружение — HARNESS_LOG_DIR, PROCESS_LOAD_STATE, PROCESS_LOAD_MAX_MB.
Командные строки процессов в журнал не пишутся: в аргументах бывают секреты.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import resource
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import config      # noqa: E402
import events      # noqa: E402
import process_load  # noqa: E402

SRC = "scripts/process-dispatcher.py"
STARTED = time.monotonic()
SHOW_PAUSE_S = 2
VIEW_FILE = "process-dispatcher-view.json"
# Проход --замер идёт раз в минуту; витрину он пересобирает, когда прежней больше 9 минут.
VIEW_EVERY_S = 540
# «сейчас» панели — последние 10 минут: одна минута слишком шумна, а свёртка берёт те же журналы.
VIEW_WINDOWS = (("сейчас", 1 / 6), ("24ч", 24), ("7д", 168))
DEFAULT_MAX_MB = 32
HOURS = {"24ч": 24, "7д": 168}
ENDS_TAIL_BYTES = 64 * 1024
UNPARSED_KEY = "process-dispatcher:не-разобрано"
UNPARSED_WINDOW_H = 1
UNPARSED_MIN_COVER_H = 0.9    # короче — доля по горстке выборок, а не по часу
ENDS_SLACK_S = 10          # process.end пишется чуть позже конца процесса, уже после прошлой выборки


def setting(name: str, default: str) -> str:
    return os.environ.get(name) or config.из_файла(config.путь_harness_conf()).get(name) or default


def locked(path: Path):
    """Дескриптор с неблокирующим flock (держит замок, пока жив процесс) или None, если замок занят."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o640)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def replace_atomically(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    if path.exists():                               # mkstemp даёт 0600: права и владелец прежнего файла переезжают на замену
        before = path.stat()
        os.chmod(tmp, stat.S_IMODE(before.st_mode))
        try:
            os.chown(tmp, before.st_uid, before.st_gid)
        except PermissionError:
            pass                                    # чужой владелец при не-root: права сохранены, владелец — писавший
    os.replace(tmp, path)


def cost() -> dict:
    used = resource.getrusage(resource.RUSAGE_SELF)
    return {"cpu_мс": round((used.ru_utime + used.ru_stime) * 1000), "мс": round((time.monotonic() - STARTED) * 1000)}


# ───────────────────────── сборщик ─────────────────────────

def collect(args) -> int:
    logs = Path(args.logs)
    lock = locked(logs / "locks" / "process-dispatcher.lock")
    if lock is None:
        print("process-dispatcher: прошлый проход ещё идёт (замок занят) — выхожу")
        return 0
    max_mb = float(setting("PROCESS_LOAD_MAX_MB", str(DEFAULT_MAX_MB)))
    for name in process_load.RUN_FILES:
        process_load.rotate(str(logs / f"{name}.jsonl"), max_mb)
    rules = process_load.load_rules(load_json(Path(args.library), {}))
    state_path = Path(os.environ.get("PROCESS_LOAD_STATE") or logs / "process-load.state.json")
    snap = process_load.scan(args.proc, args.cgroup, rules)
    prev = load_json(state_path, None)
    data, state = process_load.attribute(prev, snap, rules, args.proc,
                                         process_load.Transcripts(args.transcripts, time.time()),
                                         recent_ends(logs, snap["uptime"] - (prev or {}).get("uptime", snap["uptime"])))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    replace_atomically(state_path, json.dumps(state, ensure_ascii=False))
    if view_due(logs / VIEW_FILE):
        write_view(args)
    if data is None:
        print("process-dispatcher: нет прошлого прохода для сравнения — состояние записано, строки нет")
    else:
        data["цена"] = cost()                        # после витрины: бюджет §В.4 судит проход целиком
        events.emit(SRC, "process.sample", "ok", journal="process-load", task="-", data=data)
        watch_unparsed(args, snap, prev, rules)
    return 0


def hottest_unparsed(args, snap: dict, prev, rules: dict):
    """Самый горячий процесс, которому библиотека не нашла ярлыка: делимый юнит без метки или корзина вне библиотеки.
    Только pid, comm, cgroup: командная строка в тревогу не идёт, в аргументах бывают секреты."""
    prev_procs = (prev or {}).get("procs", {})
    labels = process_load.label_procs(snap, prev_procs, rules, args.proc,
                                      process_load.Transcripts(args.transcripts, time.time()))
    known = {r["id"] for r in load_json(Path(args.library), {}).get("процессы", [])}
    cpu, _ = process_load.cpu_seconds(snap, prev_procs)
    lost = [p for pid, p in snap["procs"].items() if p["cg"] != process_load.ROOT_CGROUP
            and (pid in labels and labels[pid] is None or pid not in labels and snap["buckets"][p["cg"]][0] not in known)]
    return max(lost, key=lambda p: cpu.get(p["pid"], 0.0), default=None)


def watch_unparsed(args, snap: dict, prev, rules: dict) -> None:
    """Доля «не разобрано» за час выше порога → карточка тревоги; ниже порога → тревога закрывается."""
    now = time.time()
    summary = process_load.summarize(process_load.read_lines(args.logs), now, UNPARSED_WINDOW_H)
    if summary["hours"] < UNPARSED_MIN_COVER_H:
        return
    limit = float(setting("PROCESS_UNPARSED_ALERT_PCT", "15"))
    writer = [sys.executable, str(Path(args.project) / "scripts" / "alert-task.py")]
    if summary["unparsed_pct"] <= limit:
        command = writer + ["--resolve", UNPARSED_KEY]
    else:
        proc = hottest_unparsed(args, snap, prev, rules)
        who = f"pid {proc['pid']}, comm {proc['comm']}, cgroup {proc['cg']}" if proc else "горячий процесс не найден"
        text = (f"Доля «не разобрано» за последний час {summary['unparsed_pct']:.1f} % ёмкости при пороге {limit:g} %: "
                f"библиотека процессов не знает, кто грузит сервер. Самый горячий неразобранный: {who}. "
                "Внести процесс в harness/config/process-library.json или остановить.")
        command = writer + ["--key", UNPARSED_KEY, "--watcher", "process-dispatcher",
                            "--title", "Диспетчер процессов: слишком много нагрузки «не разобрано»", "--text", text]
    try:
        subprocess.run(command, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"process-dispatcher: писатель тревог не отработал: {error}")


def recent_ends(logs: Path, window_s: float) -> tuple:
    """(id, pid, cpu_с) process.end окна прошлого прохода: по ним делится хвост нескольких ушедших ярлыков.
    Читается хвост файла, а не весь журнал: проход идёт раз в минуту."""
    path, since = logs / "process-runs.jsonl", time.time() - window_s - ENDS_SLACK_S
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, os.fstat(handle.fileno()).st_size - ENDS_TAIL_BYTES))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ()
    found = []
    for row in process_load.jsonl_rows(line for line in lines if '"process.end"' in line):
        data = row.get("data") or {}
        moment = process_load.parse_ts(row.get("ts"))
        if row.get("event") == "process.end" and moment and moment >= since and data.get("id") and data.get("pid"):
            found.append((data["id"], data["pid"], float(data.get("cpu_с") or 0)))
    return tuple(found)


# ───────────────────────── показ ─────────────────────────

def switch_state():
    """(значение ключа или None, если конфиг не прочитан; число исключений)."""
    conf = config.из_файла(config.путь_harness_conf())
    value = conf.get("BACKGROUND_PROCESSES", "on") if conf else None
    if value in (None, "on"):
        return value, 0
    import process_library       # noqa: PLC0415
    return value, len((process_library.load().get("выключатель") or {}).get("исключения") or [])


def switch_line() -> str:
    value, exempt = switch_state()
    if value is None:
        return "выключатель: off (конфиг не прочитан)"
    if value == "on":
        return "выключатель: on"
    return f"выключатель: {value} (исключений {exempt})"


def tokens_log_line(logs) -> str:
    """Когда tokens-collector собирал журнал в последний раз: цифры токенов не свежее этого момента."""
    stamps = [os.stat(path).st_mtime for path in (Path(logs) / "tokens.cursor", Path(logs) / "tokens-daily.json")
              if path.exists()]
    when = dt.datetime.fromtimestamp(max(stamps)).strftime("%d.%m %H:%M") if stamps else "—"
    return f"журнал токенов на {when}"


def pct(part: float, whole: float) -> float:
    return 100 * part / whole if whole else 0.0


def grouped(number: int) -> str:
    return f"{number:,}".replace(",", " ")


def spent_text(seconds: float, approx: bool = False) -> str:
    seconds = round(seconds)
    if seconds < 60:
        text = f"{seconds} с"
    elif seconds < 3600:
        text = f"{seconds // 60} мин" + (f" {seconds % 60} с" if seconds % 60 else "")
    else:
        text = f"{seconds // 3600} ч {seconds % 3600 // 60} мин"
    return ("≈" if approx else "") + text


def run_cells(ident: str, shares: dict) -> list:
    """Столбцы «запусков | в работе | доля запусков» процесса."""
    if ident in shares["constant"]:
        return ["постоянно", "-", "-"]
    runs = shares["runs"].get(ident)
    if not runs:
        return ["-", "-", "-"]
    marks = [f"{word} {runs[key]}" if runs[key] > 1 else word for key, word in (("torn", "оборван"), ("running", "идёт"))
             if runs[key]]
    count = f"{runs['n']} ({', '.join(marks)})" if marks else str(runs["n"])
    return [count, spent_text(runs["sec"], bool(runs["torn"] or runs["running"])),
            f"{pct(runs['sec'], shares['run_sec']):.1f} % / {pct(runs['n'], shares['run_n']):.1f} %"]


def token_cells(ident: str, tokens: dict) -> list:
    found = tokens["ids"].get(ident)
    return [grouped(found["all"]), f"{pct(found['all'], tokens['total']):.1f} %"] if found else ["-", "-"]


def show_now(args) -> int:
    rules = process_load.load_rules(load_json(Path(args.library), {}))
    first = process_load.scan(args.proc, args.cgroup, rules)
    _, state = process_load.attribute(None, first, rules, args.proc,
                                      process_load.Transcripts(args.transcripts, time.time()))
    time.sleep(SHOW_PAUSE_S)
    second = process_load.scan(args.proc, args.cgroup, rules)
    data, _ = process_load.attribute(state, second, rules, args.proc,
                                     process_load.Transcripts(args.transcripts, time.time()))
    print(switch_line())
    print(tokens_log_line(args.logs))
    now = time.time()
    tokens = process_load.token_totals(process_load.token_events(args.logs, now - 3600, now),
                                       process_load.model_call_pointers(args.logs, args.transcripts), now - 3600)
    window, cores = (data["окно_с"], data["ядер"]) if data else (0, 0)
    if data:
        print(f"окно {window} с, ядер {cores}, машина {100 * data['машина_cpu_с'] / (window * cores):.1f} % ёмкости")
    else:
        print("процессы: окно измерения нулевое")          # токены от окна не зависят и показываются всё равно
    print("процесс | прогон/агент | CPU % ёмкости | ядер | память МБ | процессов | токены за час | доля токенов")
    shown = set()
    for g in sorted(data["группы"] if data else [], key=lambda g: -g["cpu_с"]):
        cells = token_cells(g["id"], tokens) if g["id"] not in shown else ["-", "-"]
        shown.add(g["id"])
        print(f"{g['id']} | {g.get('агент') or g.get('прогон') or '-'} | {100 * g['cpu_с'] / (window * cores):.1f} "
              f"| {g['cpu_с'] / window:.2f} | {g['rss_мб']:.0f} | {g['процессов']} | " + " | ".join(cells))
    for ident in sorted(set(tokens["ids"]) - shown, key=lambda i: -tokens["ids"][i]["all"]):
        print(f"{ident} | - | - | - | - | - | " + " | ".join(token_cells(ident, tokens)))
    if data:
        print("остаток: " + ", ".join(f"{k} {v:.2f} с" for k, v in data["остаток"].items()))
        print("ужатие долей к cgroup: " + (", ".join(f"{k} {v:.2f} с" for k, v in data["ужатие"].items()) or "нет"))
    print("память — сумма RSS, общие страницы считаются дважды")
    return 0


def show_window(args, label: str) -> int:
    hours, now = HOURS[label], time.time()
    agg = process_load.aggregate(process_load.read_lines(args.logs), now, hours)
    shares = process_load.window_shares(args.logs, args.transcripts, args.proc, agg,
                                        load_json(Path(args.library), {}), now, hours)
    tokens = shares["tokens"]
    print(switch_line())
    print(f"данных {round(agg['hours'])} ч из {hours}; отброшено строк со сбоем замера: {agg['dropped']}")
    print("процесс | ядро-ч | % ёмкости | память пик/ср МБ | запусков | в работе | доля запусков | токены | доля токенов")
    idents = set(agg["ids"]) | set(shares["runs"]) | set(tokens["ids"])
    cpu = lambda i: agg["ids"].get(i, {}).get("cpu_с", 0.0)  # noqa: E731
    for ident in sorted(idents, key=lambda i: (-cpu(i), -tokens["ids"].get(i, {}).get("all", 0), i)):
        v = agg["ids"].get(ident)
        server = [f"{v['cpu_с'] / 3600:.2f}", f"{pct(v['cpu_с'], agg['capacity_с']):.1f}",
                  f"{v['rss_пик']:.0f}/{v['rss_ср']:.0f}"] if v else ["-", "-", "-"]
        print(" | ".join([ident] + server + run_cells(ident, shares) + token_cells(ident, tokens)))
        parts = shares["runs"].get(ident, {}).get("parts", {})
        for part in sorted(set(parts) | set(tokens["ids"].get(ident, {}).get("parts", {}))):
            run = parts.get(part)
            part_tokens = tokens["ids"][ident]["parts"].get(part) if ident in tokens["ids"] else None
            print(f"└ {part} | - | - | - | " + " | ".join([
                str(run["n"]) if run else "-", spent_text(run["sec"]) if run else "-", "-",
                grouped(part_tokens) if part_tokens else "-", f"{pct(part_tokens or 0, tokens['total']):.1f} %"]))
    for ident, runs in sorted(shares["runs"].items()):
        if runs["skipped"]:
            print(f"{ident} — выключено: {runs['skipped']} пропусков (process.skip, в запуски не входят)")
    print("неразмеченное по юнитам, ядро-ч: " + ", ".join(
        f"{k} {v / 3600:.2f}" for k, v in agg["rest"].items() if k != "вне_корзин"))
    print(f"вне корзин, ядро-ч: {agg['rest'].get('вне_корзин', 0.0) / 3600:.2f}")
    print(f"без метки: {unlabelled_text(tokens)}")
    if label == "24ч":
        print_reconcile(shares, now)
    return 0


def unlabelled_text(tokens: dict) -> str:
    lost = sum(v["all"] for i, v in tokens["ids"].items() if "без метки" in i)
    return f"{process_load.unlabelled_pct(tokens):.1f} % токенов ({grouped(lost)} из {grouped(tokens['total'])})"


def print_reconcile(shares: dict, now: float) -> None:
    """Подвал §В.3 п.5: сумма по процессам против tokens-daily.json (ждём 0) и демоны против demon-spend."""
    if shares["daily"]:
        found = process_load.reconcile(shares["events"], shares["daily"])
        (context, out), (daily_context, daily_out) = found["процессы"], found["daily"]
        gap = found["расхождение"]
        print(f"Σ по процессам контекст {context} / вывод {out}; tokens-daily.json {daily_context} / {daily_out}; "
              f"расхождение {gap if gap <= 0 else '+' + str(gap)} токенов (день {found['день']})")
    else:
        print("сверка с tokens-collector: tokens-daily.json не прочитан")
    check = process_load.demon_check(shares["events"], shares["pointers"], shares["since"])
    print("сверка демонов, транскрипты против demon-spend: " + (
        f"{100 * (check[0] - check[1]) / check[1]:+.1f} % ({grouped(check[0])} против {grouped(check[1])} токенов)"
        if check and check[1] else "нет вызовов демонов в окне"))


# ───────────────────────── витрина для панели ─────────────────────────

def view_due(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime >= VIEW_EVERY_S
    except FileNotFoundError:
        return True


def number(value, digits: int):
    """None остаётся None: отсутствующее значение панель пишет «не измерено», а не нулём."""
    return None if value is None else round(value, digits)


def view_row(ident: str, agg: dict, shares: dict, accuracy: dict) -> dict:
    """Строка витрины: числа процесса за окно, null там, где данных нет; командных строк нет."""
    server = agg["ids"].get(ident)
    runs = None if ident in shares["constant"] else shares["runs"].get(ident)
    spent = shares["tokens"]["ids"].get(ident)
    launched = {name for _, name, _ in shares["pointers"]["demon_calls"]} | {process_load.HELPERS}
    return {
        "id": ident,
        "ядро_ч": number(server and server["cpu_с"] / 3600, 3),
        "доля_ёмкости_проц": number(server and agg["capacity_с"] and pct(server["cpu_с"], agg["capacity_с"]), 2)
        if agg["capacity_с"] else None,
        "память_пик_мб": number(server and server["rss_пик"], 0),
        "память_ср_мб": number(server and server["rss_ср"], 0),
        "запусков": runs and runs["n"],
        "в_работе_ч": number(runs and runs["sec"] / 3600, 3),
        "доля_запусков_проц": number(runs and shares["run_sec"] and pct(runs["sec"], shares["run_sec"]), 1),
        "пропусков": runs and runs["skipped"],
        "токены": spent and spent["all"],
        "доля_токенов_проц": number(spent and pct(spent["all"], shares["tokens"]["total"]), 1),
        "точность": {
            "сервер": "точно" if accuracy.get(ident) in ("cgroup", "обёртка") else "≈",
            "запуски": "≈" if runs and (runs["torn"] or runs["running"] or ident in launched) else "точно",
            "токены": "точно" if ident == "claude-session" else "≈"},
    }


def unmarked_shares(agg: dict) -> dict:
    """Не разобрано по юниту, % ёмкости: остаток cgroup и строка «<юнит>: неразмечено» — одна величина,
    иначе подвал панели спорит со строкой того же юнита."""
    capacity = agg["capacity_с"]
    return {unit: number(capacity and pct(seconds + agg["ids"].get(f"{unit}: неразмечено", {}).get("cpu_с", 0.0), capacity), 1)
            for unit, seconds in agg["rest"].items()}


def view_window(args, hours: float, now: float, library: dict, accuracy: dict):
    agg = process_load.aggregate(process_load.read_lines(args.logs), now, hours)
    shares = process_load.window_shares(args.logs, args.transcripts, args.proc, agg, library, now, hours)
    idents = set(agg["ids"]) | set(shares["runs"]) | set(shares["tokens"]["ids"])
    window = {"покрытие_ч": number(agg["samples"] and agg["hours"], 1),
              "строки": [view_row(ident, agg, shares, accuracy) for ident in sorted(idents)],
              "память_пик_мб": number(agg["samples"] and agg["rss_peak"], 0),
              "неразмечено": unmarked_shares(agg),
              "отброшено_строк": agg["dropped"]}
    return window, agg, shares


def build_view(args) -> dict:
    now = time.time()
    library = load_json(Path(args.library), {})
    accuracy = {r["id"]: (r.get("доля_нагрузки") or {}).get("приписка", {}).get("способ")
                for r in library.get("процессы", [])}
    windows = {name: view_window(args, hours, now, library, accuracy) for name, hours in VIEW_WINDOWS}
    _, agg, shares = windows["24ч"]
    gap = process_load.reconcile(shares["events"], shares["daily"])["расхождение"] if shares["daily"] else None
    key, exempt = switch_state()
    return {"снято": dt.datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
            "выключатель": {"ключ": key, "исключений": exempt},
            "окна": {name: found[0] for name, found in windows.items()},
            "сверка": {"расхождение_токенов": gap, "без_метки_проц": number(process_load.unlabelled_pct(shares["tokens"]), 1)},
            "неразмечено": windows["24ч"][0]["неразмечено"]}


def write_view(args) -> None:
    """Временный файл + os.replace: панель читает целый файл или прежний, не половину."""
    path = Path(args.logs) / VIEW_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=VIEW_FILE + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(build_view(args), handle, ensure_ascii=False)
        os.chmod(tmp, events.MODE_JOURNAL)
        try:
            os.chown(tmp, -1, events.events_gid())
        except OSError:
            pass                                      # агент не в группе панели: права по режиму остаются
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):                       # сборка упала до replace: огрызок не остаётся
            os.unlink(tmp)


def view_mode(args) -> int:
    lock = locked(Path(args.logs) / "locks" / "process-dispatcher.lock")
    if lock is None:
        print("process-dispatcher: проход сборщика идёт (замок занят) — витрину соберёт он")
        return 0
    write_view(args)
    print(f"process-dispatcher: витрина записана в {Path(args.logs) / VIEW_FILE}")
    return 0


# ───────────────────────── библиотека ─────────────────────────

def to_library(args) -> int:
    path = Path(args.library)
    lock = locked(Path(args.logs) / "locks" / "process-library.lock")
    if lock is None:
        print("process-dispatcher: библиотеку правит другой проход (замок занят) — выхожу")
        return 0
    text = path.read_text(encoding="utf-8")              # перечитано под замком
    library = json.loads(text)
    pretty = lambda doc: json.dumps(doc, indent=2, ensure_ascii=False) + "\n"  # noqa: E731
    if text != pretty(library):
        print(f"process-dispatcher: отказ — оформление {path} не json.dumps(indent=2, ensure_ascii=False): "
              "файл не тронут, чтобы не раздуть дифф")
        return 1
    now = time.time()
    agg = process_load.aggregate(process_load.read_lines(args.logs), now, HOURS["7д"])
    shares = process_load.window_shares(args.logs, args.transcripts, args.proc, agg, library, now, HOURS["7д"])
    today, changed = dt.date.today().isoformat(), 0
    for record in library["процессы"]:
        share = record.get("доля_нагрузки")
        if not share:
            continue
        accuracy = "точно" if share["приписка"]["способ"] in ("cgroup", "обёртка") else "≈ оценка"
        week = process_load.week_record(agg, shares, record["id"], accuracy)
        if week:
            share["замер"] = {"дата": today, "источник": "process-load.jsonl", "неделя": week}
            changed += 1
    if changed:
        replace_atomically(path, pretty(library))
    print(f"process-dispatcher: замер внесён в {changed} записей библиотеки")
    return 0


# ───────────────────────── самотест ─────────────────────────

def selftest() -> int:
    """Разбор на искусственном снимке: обёртка с ребёнком, два прохода."""
    with tempfile.TemporaryDirectory() as tmp:
        tick = process_load.TICK
        cron = "/system.slice/cron.service"

        def snapshot(name: str, uptime: int, usage: float, child_ticks: int):
            proc, cg = Path(tmp) / name / "proc", Path(tmp) / name / "cgroup"
            for pid, ppid, argv, ticks in ((10, 1, ["/bin/bash", "/p/scripts/run-background.sh", "job", "--", "/p/job.sh"], 0),
                                           (11, 10, ["/bin/bash", "/p/job.sh"], child_ticks)):
                folder = proc / str(pid)
                folder.mkdir(parents=True)
                fields = [str(pid), "(x)", "S", str(ppid), "10", "10", "0", "-1", "0", "0", "0", "0", "0",
                          str(ticks), "0", "0", "0", "20", "0", "1", "0", "5", "0", "256"] + ["0"] * 20
                (folder / "stat").write_text(" ".join(fields))
                (folder / "statm").write_text("1000 256 0 0 0 0 0")
                (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
                (folder / "cgroup").write_text(f"0::{cron}\n")
            (proc / "stat").write_text(f"cpu {int(usage * tick)} 0 0 {uptime * tick} 0 0 0 0\ncpu0 0 0 0 0 0 0 0\n")
            (proc / "uptime").write_text(f"{uptime}.00 0.00\n")
            unit = cg / cron.strip("/")
            unit.mkdir(parents=True)
            (unit / "cpu.stat").write_text(f"usage_usec {int(usage * 1e6)}\n")
            (cg / "system.slice" / "cpu.stat").write_text(f"usage_usec {int(usage * 1e6)}\n")
            return str(proc), str(cg)

        rules = process_load.load_rules({})
        none = process_load.Transcripts(None, 0)
        first = process_load.scan(*snapshot("a", 100, 5.0, 0), rules)
        _, state = process_load.attribute(None, first, rules, str(Path(tmp) / "a" / "proc"), none)
        second = process_load.scan(*snapshot("b", 160, 8.0, 300), rules)
        data, _ = process_load.attribute(state, second, rules, str(Path(tmp) / "b" / "proc"), none)
    groups = {g["id"]: g for g in (data or {}).get("группы", [])}
    ok = bool(data) and groups.get("job", {}).get("прогон") == "10" and abs(groups["job"]["cpu_с"] - 3.0) < 0.01 \
        and groups["job"]["процессов"] == 2 and abs(data["остаток"]["cron.service"]) < 0.01
    print("process-dispatcher selftest:", "зелёный" if ok else f"красный: {data}")
    return 0 if ok else 1


# ───────────────────────── вход ─────────────────────────

def parse(argv: list):
    root = config.корень_дерева(__file__)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--замер", dest="collect", action="store_true")
    mode.add_argument("--сейчас", dest="now", action="store_true")
    mode.add_argument("--за", dest="window", choices=sorted(HOURS))
    mode.add_argument("--в-библиотеку", dest="to_library", action="store_true")
    mode.add_argument("--витрина", dest="view", action="store_true")
    mode.add_argument("--selftest", action="store_true")
    parser.add_argument("--proc", default="/proc")
    parser.add_argument("--cgroup", default="/sys/fs/cgroup")
    parser.add_argument("--библиотека", dest="library", default=str(root / "harness" / "config" / "process-library.json"))
    parser.add_argument("--проект", dest="project", default=str(root))
    parser.add_argument("--транскрипты", dest="transcripts", default=None)
    parser.add_argument("--журналы", dest="logs", default=None)
    args = parser.parse_args(argv)
    args.logs = args.logs or events.log_dir()
    args.transcripts = args.transcripts or str(
        Path.home() / ".claude" / "projects" / args.project.rstrip("/").replace("/", "-"))
    return args


def main(argv: list) -> int:
    args = parse(argv)
    if args.selftest:
        return selftest()
    if args.now:
        return show_now(args)
    if args.window:
        return show_window(args, args.window)
    if args.to_library:
        return to_library(args)
    if args.view:
        return view_mode(args)
    return events.run(SRC, lambda: collect(args), journal="process-load", name="process-dispatcher",
                      heartbeat="process-dispatcher")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
