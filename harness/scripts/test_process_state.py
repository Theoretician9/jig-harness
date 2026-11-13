#!/usr/bin/env python3
"""Пробы состояния процесса (hr.zapuskov-none-net-informacii-znachit, спека §3, §5, §6 п.1–12).

Разрешитель scripts/lib/process_state.py — одна функция resolve(records, facts, now)
для панели и CLI; факты собирают gather() (быстрые, панель) и state_facts()
(медленные, диспетчер при сборке витрины). Проба бьёт их своими данными: своя
библиотека, свои журналы, поддельные systemctl / docker / systemd-analyze,
свой каталог cgroup и свой claude-demon.sh с другим кодом лимита — живые
журналы, витрина, systemd и docker не трогаются (только чтение настоящего
systemd-analyze в п.9). Контракт: .tasks/hr.zapuskov-none-net-informacii-znachit/контракт.md.

Двери кода (окружение, читаются при каждом вызове): PROCESS_STATE_SYSTEMCTL,
PROCESS_STATE_DOCKER, PROCESS_STATE_ANALYZE, PROCESS_STATE_CGROUP,
PROCESS_STATE_DEMON. Диспетчер — своими дверями CLI и HARNESS_LOG_DIR и др.
Помощники (helpers) — по открытым agent.launch в <logs>/вызовы-агентов.jsonl,
предел висящего — открытый_запуск_макс_мин из файла двери SPEED_MODE_DATA.

Полнота: список значений берётся РАЗБОРОМ константы VALUES в process_state.py;
у каждого значения должен быть хотя бы один случай; пустой список — красный.

Прогон (только фоном):
    python3 scripts/test_process_state.py
"""
import ast
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE / "lib"
MODULE_PATH = LIB / "process_state.py"
DISPATCHER = HERE / "process-dispatcher.py"
REAL_DEMON = HERE / "claude-demon.sh"
REAL_SPEED = HERE.parent / "harness" / "config" / "speed-modes.yaml"
OPEN_MAX_MIN = 30     # свой предел висящего запуска: не 240 живых данных, чтобы вписанное число краснело
sys.path.insert(0, str(LIB))
TMP = Path(tempfile.mkdtemp(prefix="proba-process-state-"))
SECRET = "SEKRET123"  # не-секрет: маркер утечки для пробы
LIMIT = 66            # код лимита своего claude-demon.sh: не 77, чтобы вписанное число краснело
DEMON = {"через": "scripts/claude-demon.sh", "модель": "haiku"}

paths = failures = 0
OBSERVED = []         # (id, value, why) всех случаев: для полноты значений и правила «why не пуст»


def report(ok, text, bad=()):
    global paths, failures
    paths += 1
    if ok:
        print("ок    · " + text)
        return
    failures += 1
    bad = [str(b) for b in bad]
    print("НЕ ок · " + text + (": " + "; ".join(bad[:6]) if bad else ""))


def case(title):
    """Падение случая трассой — красная строка с его именем, а не смерть пробы."""
    def wrap(fn):
        print(f"── {title} ──")
        try:
            fn()
        except Exception as error:  # noqa: BLE001 — проба называет, а не падает
            report(False, f"{title}: случай упал", [f"{type(error).__name__}: {error}"[:300]])
        return fn
    return wrap


try:
    import process_state
    IMPORT_ERROR = ""
except Exception as error:  # noqa: BLE001
    process_state, IMPORT_ERROR = None, f"{type(error).__name__}: {error}"
try:
    import process_library
except Exception as error:  # noqa: BLE001
    process_library = None
    print(f"process_library не импортируется: {error}")


def ps():
    if process_state is None:
        raise RuntimeError(f"модуля scripts/lib/process_state.py нет или он не импортируется ({IMPORT_ERROR})")
    return process_state


def parsed_values() -> list:
    """Значения состояния — разбором кода: присваивание VALUES = (...) верхнего уровня."""
    try:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == "VALUES" for t in targets) \
                and isinstance(node.value, (ast.Tuple, ast.List)):
            return [e.value for e in node.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def real_limit() -> int | None:
    found = re.search(r"^KOD_LIMITA=(\d+)", REAL_DEMON.read_text(encoding="utf-8"), re.M) if REAL_DEMON.is_file() else None
    return int(found.group(1)) if found else None


# ───────────────────────── подделки ─────────────────────────
FAKE_HEAD = f"#!{sys.executable}\n" + r'''
import json, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
args = sys.argv[1:]
NAME = Path(__file__).name
with open(here / "calls.log", "a", encoding="utf-8") as log:
    log.write(NAME + " " + " ".join(args) + "\n")
mode_file = here / (NAME + "-mode.txt")
mode = mode_file.read_text(encoding="utf-8").strip() if mode_file.exists() else ""
if mode.startswith("sleep"):
    time.sleep(float(mode.split()[1]))
if mode.startswith("fail"):
    sys.stderr.write(mode.partition(" ")[2] + "\n")
    sys.exit(1)
def table(name, default):
    path = here / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
'''

# systemctl: show (блоки key=value через пустую строку, только запрошенные -p; нет юнита —
# LoadState=not-found, код 0, как у настоящего), list-timers и list-units --state=failed — JSON.
FAKE_SYSTEMCTL = FAKE_HEAD + r'''
if "list-timers" in args:
    print(json.dumps(table("timers.json", [])))
    sys.exit(0)
if "list-units" in args:
    print(json.dumps(table("failed.json", [])))
    sys.exit(0)
if args and args[0] == "show":
    units_table = table("show.json", {})
    base = {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "Result": "success",
            "ExecMainStatus": "0", "NRestarts": "0", "ActiveEnterTimestamp": "", "InactiveEnterTimestamp": "",
            "ExecMainExitTimestamp": "", "NextElapseUSecRealtime": "", "UnitFileState": ""}
    props, units, i = [], [], 1
    while i < len(args):
        a = args[i]
        if a in ("-p", "--property"):
            props += args[i + 1].split(","); i += 2; continue
        if a.startswith("--property="):
            props += a.split("=", 1)[1].split(","); i += 1; continue
        if a.startswith("-p") and len(a) > 2:
            props += a[2:].split(","); i += 1; continue
        if not a.startswith("-"):
            units.append(a)
        i += 1
    blocks = []
    for unit in units:
        known = dict(base, **units_table[unit]) if unit in units_table else dict(base, LoadState="not-found")
        known["Id"] = unit
        blocks.append("\n".join(f"{p}={known.get(p, '')}" for p in (props or list(known))))
    print("\n\n".join(blocks))
    sys.exit(0)
sys.exit(1)
'''

# docker inspect: таблица docker.json {имя: {Status, ExitCode, RestartCount, StartedAt}};
# формат -f/--format разворачивается по полям, без формата — JSON-список, как у настоящего.
FAKE_DOCKER = FAKE_HEAD + r'''
if not args or args[0] != "inspect":
    sys.exit(1)
fmt, names, i = None, [], 1
while i < len(args):
    a = args[i]
    if a in ("-f", "--format"):
        fmt = args[i + 1]; i += 2; continue
    if a.startswith("--format="):
        fmt = a.split("=", 1)[1]; i += 1; continue
    if not a.startswith("-"):
        names.append(a)
    i += 1
known, rc, out = table("docker.json", {}), 0, []
for name in names:
    if name not in known:
        sys.stderr.write(f"Error: No such object: {name}\n")
        rc = 1
        continue
    c = known[name]
    state = {"Status": c["Status"], "ExitCode": c["ExitCode"], "StartedAt": c["StartedAt"],
             "Running": c["Status"] == "running", "Paused": c["Status"] == "paused",
             "Restarting": c["Status"] == "restarting", "Dead": c["Status"] == "dead"}
    whole = {"Name": "/" + name, "RestartCount": c["RestartCount"], "State": state}
    if fmt is None:
        out.append(whole)
        continue
    text = fmt
    for key, value in (("{{json .State}}", json.dumps(state)), ("{{json .}}", json.dumps(whole)),
                       ("{{.Name}}", "/" + name), ("{{.State.Status}}", c["Status"]),
                       ("{{.State.ExitCode}}", str(c["ExitCode"])), ("{{.RestartCount}}", str(c["RestartCount"])),
                       ("{{.State.StartedAt}}", c["StartedAt"])):
        text = text.replace(key, value)
    print(text)
if fmt is None:
    print(json.dumps(out))
sys.exit(rc)
'''

# systemd-analyze calendar: ответ по таблице analyze.json {выражение: ISO следующего момента},
# база --base-time в ответе не участвует (вызов пишется в calls.log); формат — как у systemd 255.
FAKE_ANALYZE = FAKE_HEAD + r'''
from datetime import datetime, timezone
if not args or args[0] != "calendar":
    sys.exit(1)
exprs, i = [], 1
while i < len(args):
    a = args[i]
    if a == "--base-time":
        i += 2; continue
    if not a.startswith("-"):
        exprs.append(a)
    i += 1
known, rc, blocks = table("analyze.json", {}), 0, []
for expr in exprs:
    if expr not in known:
        sys.stderr.write(f"Failed to parse calendar specification '{expr}': Invalid argument\n")
        rc = 1
        continue
    moment = datetime.fromisoformat(known[expr]).astimezone()
    utc = moment.astimezone(timezone.utc)
    blocks.append(f"  Original form: {expr}\nNormalized form: {expr}\n"
                  f"    Next elapse: {moment:%a %Y-%m-%d %H:%M:%S} {moment:%Z}\n"
                  f"       (in UTC): {utc:%a %Y-%m-%d %H:%M:%S} UTC\n       From now: 1h left")
print("\n\n".join(blocks))
sys.exit(rc)
'''


def iso(moment: float) -> str:
    return datetime.fromtimestamp(moment).astimezone().isoformat(timespec="seconds")


def sd_time(moment: float) -> str:
    """Время в виде systemctl show: «Wed 2026-11-11 18:40:14 +05»."""
    return datetime.fromtimestamp(moment).astimezone().strftime("%a %Y-%m-%d %H:%M:%S %Z")


def hhmm(moment: float, now: float) -> str:
    """Время так, как его печатает панель: не сегодня — с датой."""
    stamp = datetime.fromtimestamp(moment)
    return stamp.strftime("%H:%M" if stamp.date() == datetime.fromtimestamp(now).date() else "%d.%m %H:%M")


def usec(moment: float) -> int:
    return int(moment * 1_000_000)


# ───────────────────────── своя библиотека ─────────────────────────
def rec(ident, launch, cls="фоновый", status="работает", **extra):
    record = {"id": ident, "название": f"Проба {ident}", "класс": cls, "запуск": launch, "статус": status,
              "файлы": [], "доля_нагрузки": {"приписка": {"способ": "обёртка", "признак": "-"}, "замер": "не измерено"}}
    record.update(extra)
    return record


INTERNAL = "по внутреннему действию"
RECORDS = [
    rec("svc-a", "служба", INTERNAL),
    rec("svc-demon", "служба", INTERNAL, зовёт_модель=DEMON),
    rec("part-a", "служба", INTERNAL, юниты=["harness-svc-a.service"]),
    rec("claude-session", "служба", INTERNAL, юниты=["harness-agent.service"]),
    rec("helpers", "агент-сессии", INTERNAL),
    rec("ask", "вызов-модели", INTERNAL),
    rec("game", "контейнер", "сторонний", состояние_из={"docker": "game-server"}),
    rec("lxc", "контейнер", "сторонний", состояние_из={"cgroup": "lxc.payload.p1"}),
    rec("boot-a", "загрузка", INTERNAL),
    rec("tim", "таймер"),
    rec("chk", "служба-проверок", зовёт_модель=DEMON),
    rec("job-ok", "крон"), rec("job-fail", "крон"), rec("job-limit", "крон", зовёт_модель=DEMON),
    rec("job-plain66", "крон"), rec("job-torn", "крон"), rec("job-run", "крон"), rec("job-new", "крон"),
    rec("job-missed", "крон"), rec("job-off", "крон"), rec("job-exempt", "крон"), rec("job-skip", "крон"),
    rec("job-removed", "крон", status="снят: проба"), rec("job-both", "крон"),
    rec("job-month", "крон"), rec("job-week", "крон"), rec("job-5min", "крон"),
    rec("hook-a", "хук", INTERNAL),
]
CRON = {"job-ok": "0 * * * *", "job-fail": "10 7 * * *", "job-limit": "45 */3 * * *", "job-plain66": "20 * * * *",
        "job-torn": "30 * * * *", "job-run": "40 * * * *", "job-new": "0 9 1 * *", "job-missed": "0 9 * * *",
        "job-off": "50 * * * *", "job-exempt": "55 * * * *", "job-skip": "5 * * * *", "job-removed": "1 1 * * *",
        "job-both": "0 9 1 * 2", "job-month": "0 9 1 * *", "job-week": "0 9 * * 2", "job-5min": "*/5 * * * *"}
SCHEDULE = {**{i: {"расписание": c, "команда": f"harness/demons/{i}.sh", "журнал": f"{i}.log"} for i, c in CRON.items()},
            "chk": {"расписание": "*/20 * * * *", "команда": "harness/demons/chk.sh", "журнал": "chk.log"}}
LIB_IDS = [r["id"] for r in RECORDS if r["запуск"] != "хук"]
SYSTEMD_ROWS = ("svc-a", "svc-demon", "claude-session", "boot-a", "tim", "chk")


def calendar_of(cron):
    try:
        return process_library.on_calendar(cron)
    except ValueError:
        return None


def eligible_crons(records, schedule) -> list:
    """Крон-записи не «снят» с переводимым расписанием — разбором библиотеки."""
    return [r["id"] for r in records if r["запуск"] == "крон" and not r["статус"].startswith("снят")
            and calendar_of((schedule.get(r["id"]) or {}).get("расписание", "")) is not None]


def healthy_units(now):
    return {"harness-svc-a.service": {"ActiveState": "active", "SubState": "running",
                                      "ActiveEnterTimestamp": sd_time(now - 3600)},
            "harness-svc-demon.service": {"ActiveState": "active", "SubState": "running",
                                          "ActiveEnterTimestamp": sd_time(now - 3600)},
            "harness-agent.service": {"ActiveState": "active", "SubState": "running",
                                      "ActiveEnterTimestamp": sd_time(now - 7200)},
            "harness-boot-a.service": {"ActiveState": "active", "SubState": "exited"},
            "harness-tim.timer": {"ActiveState": "active", "SubState": "waiting"},
            "harness-tim.service": {"ActiveState": "inactive", "SubState": "dead"},
            "harness-chk.timer": {"ActiveState": "active", "SubState": "waiting"},
            "harness-chk.service": {"ActiveState": "inactive", "SubState": "dead"}}


def healthy_timers(now):
    return [{"unit": "harness-tim.timer", "activates": "harness-tim.service", "next": usec(now + 600),
             "left": usec(now + 600), "last": usec(now - 300), "passed": 300_000_000},
            {"unit": "harness-chk.timer", "activates": "harness-chk.service", "next": usec(now + 1200),
             "left": usec(now + 1200), "last": usec(now - 600), "passed": 600_000_000}]


def run_event(event, ident, at, outcome="ok", **data):
    return {"v": 1, "ts": iso(at), "src": "scripts/run-background.sh", "event": event, "outcome": outcome,
            "task": "-", "session": None, "data": {"id": ident, **data}}


def end(ident, at, rc, pid=4000, seconds=12.5):
    return [run_event("process.start", ident, at - seconds, pid=pid, sid=pid),
            run_event("process.end", ident, at, "ok" if rc == 0 else "error", pid=pid, rc=rc, с=seconds, cpu_с=1.0)]


def sample(at, groups):
    return {"v": 1, "ts": iso(at), "src": "scripts/process-dispatcher.py", "event": "process.sample", "outcome": "ok",
            "task": "-", "data": {"окно_с": 60, "ядер": 4, "машина_cpu_с": 10.0, "остаток": {}, "ужатие": {},
                                  "группы": [{"id": g, "прогон": "-", "cpu_с": 1.0, "rss_мб": 50.0, "процессов": n}
                                             for g, n in groups]}}


def agent_event(event, call, at, outcome="ok"):
    """Строка журнала вызовов в формате писателей: agent_budget_guard.py (launch), agent_finish_log.py (finish)."""
    src = "scripts/hooks/agent_finish_log.py" if event == "agent.finish" else "scripts/hooks/agent_budget_guard.py"
    data = {"вызов": call} if event == "agent.finish" else \
        {"ревью": False, "описание": f"проба {call}", "тип": "harness-worker", "модель": "sonnet", "вызов": call}
    return {"v": 1, "ts": iso(at), "src": src, "event": event, "outcome": outcome, "task": "hr.proba",
            "task_from": "arg", "session": "proba-session", "data": data}


def own_speed_modes() -> str:
    """Живой speed-modes.yaml с подменённым пределом: файл цел для любого читателя, число — своё."""
    text = REAL_SPEED.read_text(encoding="utf-8")
    swapped = re.sub(r"^открытый_запуск_макс_мин:\s*\d+", f"открытый_запуск_макс_мин: {OPEN_MAX_MIN}", text, flags=re.M)
    if swapped == text:
        raise RuntimeError(f"в {REAL_SPEED} нет ключа открытый_запуск_макс_мин — предел висящего не из данных")
    return swapped


def dead_pid() -> int:
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


class Stand:
    """Свой каталог: подделки, журналы, cgroup, claude-demon.sh с кодом лимита LIMIT."""

    def __init__(self, name):
        self.dir = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=TMP))
        self.bin, self.logs, self.cgroup = self.dir / "bin", self.dir / "logs", self.dir / "cgroup"
        for folder in (self.bin, self.logs, self.cgroup):
            folder.mkdir()
        for program, text in (("systemctl", FAKE_SYSTEMCTL), ("docker", FAKE_DOCKER), ("systemd-analyze", FAKE_ANALYZE)):
            path = self.bin / program
            path.write_text(text, encoding="utf-8")
            path.chmod(0o755)
        self.demon = self.dir / "claude-demon.sh"
        self.demon.write_text(f"#!/bin/bash\nKOD_LIMITA={LIMIT}\nexit \"$KOD_LIMITA\"\n", encoding="utf-8")
        self.env = {"PROCESS_STATE_SYSTEMCTL": str(self.bin / "systemctl"), "PANEL_SYSTEMCTL": str(self.bin / "systemctl"),
                    "PROCESS_STATE_DOCKER": str(self.bin / "docker"), "PROCESS_STATE_ANALYZE": str(self.bin / "systemd-analyze"),
                    "PROCESS_STATE_CGROUP": str(self.cgroup), "PROCESS_STATE_DEMON": str(self.demon),
                    "SPEED_MODE_DATA": str(self.dir / "speed-modes.yaml")}
        (self.dir / "speed-modes.yaml").write_text(own_speed_modes(), encoding="utf-8")
        now = time.time()
        self.put("show.json", healthy_units(now))
        self.put("timers.json", healthy_timers(now))
        self.put("failed.json", [])
        self.put("docker.json", {"game-server": {"Status": "running", "ExitCode": 0, "RestartCount": 0,
                                                 "StartedAt": datetime.fromtimestamp(now - 7200, timezone.utc).strftime(
                                                     "%Y-%m-%dT%H:%M:%S.123456789Z")}})
        self.put("analyze.json", {calendar_of(c): iso(now + 3600) for c in CRON.values() if calendar_of(c)})
        self.lxc("populated 1\nfrozen 0\n")
        self.runs([])
        self.samples([sample(now - 60, [("claude-session", 3)])])

    def put(self, name, value):
        (self.bin / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def mode(self, program, text):
        path = self.bin / f"{program}-mode.txt"
        if text:
            path.write_text(text, encoding="utf-8")
        elif path.exists():
            path.unlink()

    def unit(self, name, **props):
        table = json.loads((self.bin / "show.json").read_text(encoding="utf-8"))
        table[name] = props
        self.put("show.json", table)

    def drop_unit(self, name):
        table = json.loads((self.bin / "show.json").read_text(encoding="utf-8"))
        table.pop(name, None)
        self.put("show.json", table)

    def lxc(self, events):
        folder = self.cgroup / "lxc.payload.p1"
        if events is None:
            shutil.rmtree(folder, ignore_errors=True)
            return
        folder.mkdir(exist_ok=True)
        (folder / "cgroup.events").write_text(events, encoding="utf-8")

    def runs(self, events, append=False):
        with open(self.logs / "process-runs.jsonl", "a" if append else "w", encoding="utf-8") as log:
            log.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))

    def agents(self, rows):
        """Свой журнал вызовы-агентов.jsonl в каталоге журналов стенда (его читает gather по logs)."""
        (self.logs / "вызовы-агентов.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                                       encoding="utf-8")

    def samples(self, rows):
        (self.logs / "process-load.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                                      encoding="utf-8")

    def calls(self, program) -> list:
        path = self.bin / "calls.log"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return [line for line in lines if line.split(" ", 1)[0] == program]

    def clear_calls(self):
        (self.bin / "calls.log").unlink(missing_ok=True)

    @contextlib.contextmanager
    def doors(self):
        saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def full_lines(self) -> list:
        return (self.logs / "process-runs.jsonl").read_text(encoding="utf-8").splitlines()

    def slow(self, now, records=RECORDS, schedule=SCHEDULE):
        with self.doors():
            return ps().state_facts(records, schedule, self.full_lines(), now)

    def resolve(self, now=None, slow=None, key="on", exempt=(), outside=(), records=RECORDS, schedule=SCHEDULE):
        now = time.time() if now is None else now
        with self.doors():
            facts = ps().gather(records, schedule, self.logs, slow, now, key, set(exempt), list(outside))
        result = ps().resolve(records, facts, now)
        for ident, entry in (result or {}).items():
            OBSERVED.append((ident, (entry or {}).get("value"), (entry or {}).get("why")))
        missing = [i for i in (r["id"] for r in records if r["запуск"] != "хук") if i not in (result or {})]
        if missing:
            OBSERVED.append(("<пропущены ключи>", "missing", ", ".join(missing)))
        return result


def value(result, ident):
    return ((result or {}).get(ident) or {}).get("value")


def why(result, ident):
    return str(((result or {}).get(ident) or {}).get("why") or "")


def entry(result, ident):
    return (result or {}).get(ident) or {}


def expect(result, ident, want, text, why_has=(), why_not=()):
    got, said = value(result, ident), why(result, ident)
    lacks = [w for w in why_has if w not in said]
    leaks = [w for w in why_not if w in said]
    report(got == want and not lacks and not leaks, text,
           [f"{ident}: value={got!r} why={said!r}"] + ([f"в why нет {lacks}"] if lacks else [])
           + ([f"в why есть {leaks}"] if leaks else []))


print(f"── полнота: значения разбором VALUES в {MODULE_PATH.name} ──")
VALUES = parsed_values()
report(bool(VALUES), f"константа VALUES разобрана из process_state.py: {VALUES}",
       [f"нет файла или присваивания VALUES = (...) в {MODULE_PATH}"])


# ───────────────────────── п.1 systemd, constant ─────────────────────────
@case("1. systemd: постоянная служба")
def _systemd():
    now = time.time()
    s = Stand("p1")
    result = s.resolve(now)
    expect(result, "svc-a", "running", "active running → running")
    since = entry(result, "svc-a").get("since")
    report(isinstance(since, (int, float)) and abs(since - (now - 3600)) <= 60,
           "since = ActiveEnterTimestamp (эпоха, ±60 с)", [f"since={since!r}, ждали ≈{now - 3600:.0f}"])
    s.unit("harness-svc-a.service", ActiveState="failed", SubState="failed", Result="exit-code", ExecMainStatus="1")
    expect(s.resolve(), "svc-a", "failed", "failed exit-code 1 → failed, why с «exit-code» и «1»", ("exit-code", "1"))
    s.unit("harness-svc-demon.service", ActiveState="failed", SubState="failed", Result="exit-code",
           ExecMainStatus=str(LIMIT))
    expect(s.resolve(), "svc-demon", "skipped",
           f"демон модели: failed с ExecMainStatus = KOD_LIMITA ({LIMIT} из своего claude-demon.sh) → skipped «лимит подписки»",
           ("лимит подписки",))
    s.unit("harness-svc-a.service", ActiveState="failed", SubState="failed", Result="exit-code", ExecMainStatus=str(LIMIT))
    expect(s.resolve(), "svc-a", "failed",
           f"код лимита {LIMIT} у записи БЕЗ зовёт_модель.через = claude-demon.sh → failed «код {LIMIT}», не skipped",
           (str(LIMIT),))
    s.unit("harness-svc-demon.service", ActiveState="failed", SubState="failed", Result="exit-code", ExecMainStatus="77")
    expect(s.resolve(), "svc-demon", "failed",
           f"демон модели с кодом 77 при KOD_LIMITA={LIMIT} → failed (код читается из файла, не вписан)", ("77",))
    s.unit("harness-svc-a.service", ActiveState="inactive", SubState="dead", Result="success",
           InactiveEnterTimestamp=sd_time(now - 600))
    expect(s.resolve(), "svc-a", "stopped", "inactive dead → stopped")
    s.drop_unit("harness-svc-a.service")
    result = s.resolve()
    expect(result, "svc-a", "unknown", "LoadState=not-found у обоих имён → unknown, why непуст")
    report(bool(why(result, "svc-a").strip()), "not-found: why называет причину", [why(result, "svc-a")])

    healthy = Stand("p1-fail")
    healthy.runs(end("job-ok", now - 600, 0))
    slow = healthy.slow(now)
    healthy.mode("systemctl", f"fail Failed to connect to bus {('--' + 'token')}={SECRET} /run/dbus/x")
    result = healthy.resolve(now, slow=slow)
    bad = [f"{i}={value(result, i)}:{why(result, i)!r}" for i in SYSTEMD_ROWS
           if value(result, i) != "unknown" or not why(result, i).startswith("systemctl")]
    report(not bad, "systemctl код 1 → unknown «systemctl: …» у ВСЕХ systemd-строк", bad)
    leaked = [i for i in SYSTEMD_ROWS if SECRET in why(result, i) or len(why(result, i)) > 120]
    report(not leaked, "why сбоя systemctl без аргументов команды (маркера нет) и ≤ 120 знаков", leaked)
    report(value(result, "job-ok") == "waiting" and value(result, "game") == "running" and value(result, "lxc") == "running",
           "сбой systemctl не трогает остальные источники: крон из журнала, docker из витрины, cgroup",
           [f"job-ok={value(result, 'job-ok')}, game={value(result, 'game')}, lxc={value(result, 'lxc')}"])
    healthy.mode("systemctl", "sleep 10")
    started = time.monotonic()
    result = healthy.resolve(now, slow=slow)
    spent = time.monotonic() - started
    bad = [i for i in SYSTEMD_ROWS if value(result, i) != "unknown" or not why(result, i).startswith("systemctl")]
    report(spent < 7.5 and not bad, "systemctl спит 10 с → таймаут 2 с на вызов, unknown «systemctl: …», сбор < 7,5 с",
           [f"сбор {spent:.1f} с", f"не unknown: {bad}"])


@case("1. код лимита — одно место, общая подпись прогона (§3.2, §4.4)")
def _limit():
    s = Stand("p1-limit")
    with s.doors():
        got = ps().limit_code()
    report(got == LIMIT, f"limit_code() читает KOD_LIMITA из PROCESS_STATE_DEMON ({LIMIT})", [f"получено {got!r}"])
    saved = os.environ.pop("PROCESS_STATE_DEMON", None)
    try:
        want, got = real_limit(), ps().limit_code()
    finally:
        if saved is not None:
            os.environ["PROCESS_STATE_DEMON"] = saved
    report(want is not None and got == want, f"без двери — KOD_LIMITA из scripts/claude-demon.sh ({want})",
           [f"получено {got!r}"])
    demon, plain = rec("d", "служба-проверок", зовёт_модель=DEMON), rec("p", "крон")
    found = [ps().exit_state(demon, LIMIT, LIMIT), ps().exit_state(plain, LIMIT, LIMIT), ps().exit_state(plain, 1, LIMIT),
             ps().exit_state(plain, 0, LIMIT)]
    report(found[0][0] == "skipped" and "лимит подписки" in found[0][1]
           and found[1][0] == "failed" and f"код {LIMIT}" in found[1][1]
           and found[2][0] == "failed" and "код 1" in found[2][1] and found[3][0] is None,
           "exit_state: лимит у демона → (skipped, «лимит подписки»); у прочих → (failed, «код N»); rc 0 → (None, …)",
           [repr(found)])


# ───────────────────────── п.2 docker ─────────────────────────
def docker_case(s, status, code, now, text, want, why_has=()):
    s.put("docker.json", {"game-server": {"Status": status, "ExitCode": code, "RestartCount": 2,
                                          "StartedAt": "2026-11-05T17:28:17.715270830Z"}})
    expect(s.resolve(now, slow=s.slow(now)), "game", want, text, why_has)


@case("2. docker: игровой сервер из витрины")
def _docker():
    now = time.time()
    s = Stand("p2")
    slow = s.slow(now)
    report(isinstance(slow, dict) and {"checked", "docker", "docker_error", "missed", "log_start", "last_run"} <= set(slow),
           "state_facts: ключи checked, docker, docker_error, missed, log_start, last_run (§3.5)",
           [f"ключи {sorted(slow or {})}"])
    result = s.resolve(now, slow=slow)
    expect(result, "game", "running", "running → running")
    since = entry(result, "game").get("since")
    report(isinstance(since, (int, float)) and abs(since - (now - 7200)) <= 60,
           "since = StartedAt (наносекунды Z разобраны, ±60 с)", [f"since={since!r}"])
    for status, code, want, text, has in (("exited", 137, "failed", "exited 137 → failed", ("137",)),
                                          ("exited", 0, "stopped", "exited 0 → stopped", ()),
                                          ("restarting", 0, "failed", "restarting → failed", ()),
                                          ("paused", 0, "stopped", "paused → stopped с why", ("приостановлен",)),
                                          ("created", 0, "stopped", "created → stopped с why", ("создан",))):
        docker_case(s, status, code, now, text, want, has)
    s.put("docker.json", {})
    expect(s.resolve(now, slow=s.slow(now)), "game", "unknown", "контейнера нет → unknown «контейнер не найден»",
           ("не найден",))
    s.mode("docker", "fail Cannot connect to the Docker daemon at unix:///var/run/docker.sock")
    slow = s.slow(now)
    report(bool((slow or {}).get("docker_error")) and (slow or {}).get("docker") == {},
           "docker код 1 → state_facts.docker_error = причина, docker = {}", [repr(slow)[:200]])
    report({"missed", "log_start", "last_run"} <= set(slow or {}), "сбой docker не роняет остальные факты витрины",
           [f"ключи {sorted(slow or {})}"])
    expect(s.resolve(now, slow=slow), "game", "unknown", "docker не ответил → unknown с причиной")
    s.mode("docker", "sleep 10")
    started = time.monotonic()
    slow = s.slow(now)
    spent = time.monotonic() - started
    report(spent < 6 and bool((slow or {}).get("docker_error")),
           "docker спит 10 с → таймаут 2 с, docker_error заполнен, сбор < 6 с", [f"сбор {spent:.1f} с", repr(slow)[:160]])
    s.mode("docker", "")
    s.put("docker.json", {"game-server": {"Status": "running", "ExitCode": 0, "RestartCount": 0,
                                          "StartedAt": "2026-11-05T17:28:17Z"}})
    stale = s.slow(now - 45 * 60)
    expect(s.resolve(now, slow=stale), "game", "unknown",
           "факты витрины старше PROCESS_STALE_MIN (45 мин) → unknown «проверка N мин назад»", ("проверка", "мин назад"))
    report(getattr(ps(), "PROCESS_STALE_MIN", None) == 30, "PROCESS_STALE_MIN = 30 в process_state",
           [repr(getattr(ps(), "PROCESS_STALE_MIN", None))])


# ───────────────────────── п.3 lxc ─────────────────────────
@case("3. lxc: каталог cgroup")
def _lxc():
    s = Stand("p3")
    result = s.resolve()
    expect(result, "lxc", "running", "populated 1 frozen 0 → running")
    report(entry(result, "lxc").get("since", "нет ключа") is None, "у lxc since: null",
           [repr(entry(result, "lxc").get("since", "нет ключа"))])
    s.lxc("populated 1\nfrozen 1\n")
    expect(s.resolve(), "lxc", "stopped", "frozen 1 → stopped «заморожен»", ("заморожен",))
    s.lxc("populated 0\nfrozen 0\n")
    expect(s.resolve(), "lxc", "stopped", "populated 0 → stopped")
    s.lxc(None)
    expect(s.resolve(), "lxc", "stopped", "каталога нет → stopped «контейнер не виден»", ("не виден",))


# ───────────────────────── п.4 scheduled ─────────────────────────
@case("4. scheduled: журнал запусков, выключатель, таймер")
def _scheduled():
    now = time.time()
    s = Stand("p4")
    child = subprocess.Popen(["sleep", "60"])
    try:
        events = (end("job-ok", now - 900, 0, pid=4101) + end("job-fail", now - 800, 1, pid=4102)
                  + end("job-limit", now - 700, LIMIT, pid=4103) + end("job-plain66", now - 650, LIMIT, pid=4104)
                  + [run_event("process.start", "job-torn", now - 500, pid=dead_pid(), sid=1)]
                  + end("job-skip", now - 1200, 0, pid=4105)
                  + [run_event("process.skip", "job-skip", now - 300, "skipped", значение="off")]
                  + [run_event("process.start", "job-run", time.time(), pid=child.pid, sid=child.pid)])
        s.runs(events)
        result = s.resolve(now)
        expect(result, "job-ok", "waiting", "последний rc 0 → waiting")
        expect(result, "job-fail", "failed", "последний rc 1 → failed «код 1 в ЧЧ:ММ»", ("код 1", hhmm(now - 800, now)))
        expect(result, "job-limit", "skipped", f"демон модели, rc {LIMIT} = лимит → skipped «лимит подписки»",
               ("лимит подписки",))
        expect(result, "job-plain66", "failed", f"не демон, rc {LIMIT} → failed «код {LIMIT}»", (f"код {LIMIT}",))
        expect(result, "job-torn", "failed", "открытый start с мёртвым pid (оборван) → failed")
        expect(result, "job-run", "running", "открытый start с живым pid → running")
        report((entry(result, "job-run").get("last_run") or {}).get("outcome") == "running",
               "у идущего last_run.outcome = «running»", [repr(entry(result, "job-run").get("last_run"))])
        expect(result, "job-skip", "waiting", "последний process.skip при ключе on → НЕ disabled (waiting)")
        expect(result, "job-new", "waiting", "запусков нет, пропуска нет → waiting «ещё не запускался»",
               ("ещё не запускался",))
        expect(result, "job-removed", "removed", "статус «снят…» → removed")
        report((entry(result, "job-ok").get("last_run") or {}).get("rc") == 0,
               "last_run из журнала: rc последнего process.end", [repr(entry(result, "job-ok").get("last_run"))])
        result = s.resolve(now, key="off", exempt={"job-exempt"})
        expect(result, "job-off", "disabled", "выключатель off сейчас, фоновая запись не исключение → disabled")
        report(entry(result, "job-off").get("next_run", "нет ключа") is None, "disabled → next_run: null",
               [repr(entry(result, "job-off").get("next_run", "нет ключа"))])
        report(value(result, "job-exempt") != "disabled", "исключение выключателя при off → не disabled",
               [value(result, "job-exempt")])
        report(value(result, "chk") != "disabled", "служба-проверок при off → не disabled", [value(result, "chk")])
        s.unit("harness-tim.timer", ActiveState="inactive", SubState="dead")
        expect(s.resolve(now), "tim", "stopped", "таймер inactive → stopped «таймер выключен»", ("таймер",))
        slow = {"checked": iso(now - 120), "docker": {}, "docker_error": None, "missed": {"job-missed": iso(now - 3 * 3600)},
                "log_start": iso(now - 3600), "last_run": {}}
        expect(s.resolve(now, slow=slow), "job-missed", "skipped",
               "missed в фактах витрины → skipped «плановый ЧЧ:ММ не состоялся»",
               ("плановый", hhmm(now - 3 * 3600, now), "не состоялся"))
        old_missed = {**slow, "missed": {"job-missed": iso(now - 2 * 86400)}}
        expect(s.resolve(now, slow=old_missed), "job-missed", "skipped",
               "плановый момент не сегодня — «плановый ДД.ММ ЧЧ:ММ не состоялся» (краснеет: дата потеряна)",
               (f"плановый {datetime.fromtimestamp(now - 2 * 86400):%d.%m %H:%M} не состоялся",))
        stale = {**slow, "checked": iso(now - 45 * 60)}
        expect(s.resolve(now, slow=stale), "job-missed", "unknown",
               "missed из витрины старше PROCESS_STALE_MIN → unknown «проверка N мин назад», не «ждёт запуска» (§4.2)",
               ("проверка", "мин назад"))
    finally:
        child.kill()
        child.wait()


@case("4. state_facts.missed: плановый пропуск у кронов (systemd-analyze --base-time)")
def _missed():
    now = time.time()
    s = Stand("p4-missed")
    s.runs(end("job-missed", now - 27 * 3600, 0, pid=4201) + end("job-5min", now - 120, 0, pid=4202))
    table = {calendar_of(c): iso(now + 3600) for c in CRON.values() if calendar_of(c)}
    table[calendar_of(CRON["job-missed"])] = iso(now - 3 * 3600)
    table[calendar_of(CRON["job-5min"])] = iso(now + 180)
    s.put("analyze.json", table)
    slow = s.slow(now)
    missed = (slow or {}).get("missed") or {}
    want = set(eligible_crons(RECORDS, SCHEDULE))
    report(bool(want) and set(missed) == want, f"missed — по каждому крону не «снят» с переводимым расписанием ({len(want)})",
           [f"лишние {sorted(set(missed) - want)}", f"нет {sorted(want - set(missed))}"])
    at = missed.get("job-missed")
    moment = datetime.fromisoformat(at).timestamp() if isinstance(at, str) else None
    report(moment is not None and abs(moment - (now - 3 * 3600)) <= 60,
           "плановый момент после последнего запуска прошёл > MISSED_SLACK_MIN назад → missed = этот момент", [repr(at)])
    report(missed.get("job-5min", "нет") is None, "следующий плановый в будущем → missed null", [repr(missed.get("job-5min", "нет"))])
    calls = s.calls("systemd-analyze")
    report(bool(calls) and all("--base-time" in c for c in calls), "пропуск считается с --base-time", calls[:2])


@case("4. последний запуск старше хвоста панели (К2-1)")
def _old_tail():
    now = time.time()
    s = Stand("p4-tail")
    first = now - 3 * 86400
    head = end("job-fail", first + 60, 1, pid=4301)
    head[0]["ts"] = iso(first)
    tail_bytes = getattr(ps(), "RUNS_TAIL_BYTES", None)
    report(isinstance(tail_bytes, int) and tail_bytes > 0, "process_state.RUNS_TAIL_BYTES задан", [repr(tail_bytes)])
    tail_bytes = tail_bytes or 2_000_000
    filler = []
    line = len(json.dumps(end("filler", now, 0)[0], ensure_ascii=False)) + 1
    count = int(tail_bytes * 1.4 / line / 2) + 1
    for index in range(count):
        filler += end("filler", now - 3 * 3600 + index * (3 * 3600 - 120) / count, 0, pid=5000 + index % 3000)
    s.runs(head + filler)
    size = (s.logs / "process-runs.jsonl").stat().st_size
    report(size > tail_bytes, f"журнал {size} Б больше хвоста {tail_bytes} Б", [])
    slow_full = s.slow(now)
    last = ((slow_full or {}).get("last_run") or {}).get("job-fail") or {}
    report(last.get("rc") == 1 and (slow_full or {}).get("log_start") == head[0]["ts"],
           "state_facts из ПОЛНОГО журнала: last_run[job-fail].rc = 1, log_start = ts первой строки",
           [f"last_run={last}", f"log_start={(slow_full or {}).get('log_start')} ждали {head[0]['ts']}"])
    tail_start = now - 3 * 3600 + 0.3 * 3 * 3600   # оценка снизу: хвост — последние ~70 % наполнителя
    slow = {"checked": iso(now - 120), "docker": {}, "docker_error": None, "missed": {}, "log_start": head[0]["ts"],
            "last_run": {"job-fail": {"at": iso(first + 60), "rc": 1, "outcome": "error", "seconds": 12.5}}}
    result = s.resolve(now, slow=slow)
    expect(result, "job-fail", "failed", "rc 1 только в начале файла, факт витрины → failed «код 1 в ЧЧ:ММ»",
           ("код 1", hhmm(first + 60, now)))
    stamp = datetime.fromtimestamp(first).strftime("%d.%m %H:%M")
    expect(result, "job-new", "waiting", f"id без событий → «ещё не запускался с {stamp}» (начало ПОЛНОГО журнала)",
           ("ещё не запускался", stamp))
    s.runs(end("job-fail", now - 60, 0, pid=4302), append=True)
    expect(s.resolve(now, slow=slow), "job-fail", "waiting", "новее checked в хвосте rc 0 → waiting (хвост перекрывает факт)")
    behind = dict(slow, checked=iso(tail_start - 2 * 3600))
    s.runs(head + filler)
    expect(s.resolve(now, slow=behind), "job-fail", "unknown",
           "хвост начинается позже checked, у id нет события в хвосте → unknown", ())


# ───────────────────────── п.5 on_demand и хозяева ─────────────────────────
@case("5. on_demand и хозяева: группы последней выборки")
def _on_demand():
    now = time.time()
    s = Stand("p5")
    s.samples([sample(now - 60, [("ask", 3), ("ask", 1), ("ask", 0), ("part-a", 1), ("claude-session", 2)])])
    result = s.resolve(now)
    expect(result, "ask", "running", "группы с процессов > 0 → running")
    report(entry(result, "ask").get("running_n") == 2, "running_n = число групп с процессов > 0 (2)",
           [repr(entry(result, "ask").get("running_n"))])
    expect(result, "part-a", "running", "часть с хозяином — свой юнит другой записи → running по группе")
    expect(result, "claude-session", "running", "claude-session: юнит active и группа процессов > 0 → running")
    s.samples([sample(now - 60, [("ask", 0), ("claude-session", 2)])])
    result = s.resolve(now)
    expect(result, "ask", "idle", "группа есть, процессов: 0 → idle, не running")
    s.samples([sample(now - 60, [("ask", 0)])])
    expect(s.resolve(now), "claude-session", "unknown", "claude-session: юнит active без группы → unknown",
           ("не найден",))
    s.samples([sample(now - 600, [("ask", 2), ("claude-session", 1)])])
    result = s.resolve(now)
    expect(result, "ask", "unknown", "выборка старше 5 мин → unknown", ("старше 5 мин",))
    expect(result, "claude-session", "unknown", "claude-session: группа есть, но выборка старше 5 мин → unknown",
           ("старше 5 мин",))


# ───────────────────────── п.5б помощники ─────────────────────────
@case("5б. helpers: «идёт» по открытым agent.launch журнала вызовов, не по группе выборки")
def _helpers():
    now = time.time()
    s = Stand("p5b")
    s.samples([sample(now - 60, [("claude-session", 2)])])      # выборка без группы helpers — как в 1 106 из 1 631
    s.agents([agent_event("agent.launch", "call-a", now - 300), agent_event("agent.launch", "call-b", now - 600),
              agent_event("agent.launch", "call-c", now - 900), agent_event("agent.finish", "call-c", now - 120),
              agent_event("agent.launch", "call-r", now - 200, outcome="refused")])
    result = s.resolve(now)
    expect(result, "helpers", "running", "2 открытых launch, выборка без группы helpers → running")
    report(entry(result, "helpers").get("running_n") == 2,
           "running_n = открытые запуски (2): закрытый finish и refused не в счёте",
           [repr(entry(result, "helpers").get("running_n"))])
    s.agents([agent_event("agent.launch", "call-a", now - 300), agent_event("agent.finish", "call-a", now - 30)])
    expect(s.resolve(now), "helpers", "idle", "launch + finish → idle")
    s.agents([agent_event("agent.launch", "call-old", now - (OPEN_MAX_MIN + 30) * 60),
              agent_event("agent.launch", "call-new", now - (OPEN_MAX_MIN - 10) * 60)])
    result = s.resolve(now)
    report(value(result, "helpers") == "running" and entry(result, "helpers").get("running_n") == 1,
           f"открытый launch старше предела ({OPEN_MAX_MIN} мин из SPEED_MODE_DATA) не считается, младше — считается",
           [f"value={value(result, 'helpers')!r} running_n={entry(result, 'helpers').get('running_n')!r}"])
    s.agents([agent_event("agent.launch", "call-old", now - (OPEN_MAX_MIN + 30) * 60)])
    expect(s.resolve(now), "helpers", "idle", "только висящий старше предела → idle")
    s.samples([sample(now - 60, [("helpers", 3), ("helpers", 1), ("claude-session", 2)])])
    s.agents([agent_event("agent.launch", "call-a", now - 300), agent_event("agent.finish", "call-a", now - 30)])
    expect(s.resolve(now), "helpers", "idle", "группа helpers в выборке есть, открытых запусков нет → idle (выборка не судит)")


# ───────────────────────── п.6 boot ─────────────────────────
@case("6. boot: загрузочные юниты")
def _boot():
    s = Stand("p6")
    expect(s.resolve(), "boot-a", "done", "active exited → done")
    s.unit("harness-boot-a.service", ActiveState="inactive", SubState="dead", Result="success", ExecMainStatus="1")
    expect(s.resolve(), "boot-a", "done", "inactive dead, Result=success, ExecMainStatus=1 → done")
    s.unit("harness-boot-a.service", ActiveState="failed", SubState="failed", Result="exit-code", ExecMainStatus="2")
    expect(s.resolve(), "boot-a", "failed", "failed → failed")


# ───────────────────────── п.7–8 вид и полнота ключей ─────────────────────────
@case("7. правило вида по данным записи (§3.1)")
def _kinds():
    by_id = {r["id"]: r for r in RECORDS}
    want = {"svc-a": "constant", "part-a": "on_demand", "claude-session": "constant", "helpers": "on_demand",
            "ask": "on_demand", "game": "constant", "lxc": "constant", "boot-a": "boot", "tim": "scheduled",
            "chk": "scheduled", "job-ok": "scheduled", "hook-a": None}
    got = {i: ps().kind_of(by_id[i], RECORDS) for i in want}
    report(got == want, "kind_of: служба без юниты → constant; хозяин — свой юнит другой записи → on_demand; "
           "чужой хозяин → constant; хук → None", [f"{i}: {got[i]!r} ≠ {w!r}" for i, w in want.items() if got[i] != w])
    result = Stand("p7").resolve()
    report("hook-a" not in (result or {}), "хук — ключа в результате нет", [])
    kinds = {i: entry(result, i).get("kind") for i in want if i != "hook-a"}
    report(all(kinds[i] == want[i] for i in kinds), "result[id].kind совпадает с kind_of", [repr(kinds)])


@case("8. записи без выборок и строк витрины")
def _no_samples():
    s = Stand("p8")
    s.samples([])
    result = s.resolve()
    lost = [i for i in LIB_IDS if i not in (result or {}) or not value(result, i)]
    report(not lost, f"каждая из {len(LIB_IDS)} записей не-хуков — ключ с заполненным value (разбор библиотеки, не выборок)",
           lost)


# ───────────────────────── п.9 следующий запуск ─────────────────────────
@case("9. следующий запуск: таймеры, крон через systemd-analyze")
def _next():
    now = time.time()
    s = Stand("p9")
    answers = {"job-5min": now + 120, "job-month": now + 20 * 86400, "job-week": now + 3 * 86400}
    table = {calendar_of(c): iso(now + 3600) for c in CRON.values() if calendar_of(c)}
    for ident, moment in answers.items():
        table[calendar_of(CRON[ident])] = iso(moment)
    s.put("analyze.json", table)
    s.clear_calls()
    result = s.resolve(now)
    for ident, moment in [("tim", now + 600), ("chk", now + 1200), *answers.items()]:
        got = entry(result, ident).get("next_run")
        report(isinstance(got, (int, float)) and abs(got - moment) <= 1,
               f"{ident}: next_run = {datetime.fromtimestamp(moment):%d.%m %H:%M} "
               f"({'монотонный таймер, next из list-timers' if ident == 'tim' else 'list-timers' if ident == 'chk' else 'systemd-analyze'})",
               [repr(got)])
    report(len(s.calls("systemd-analyze")) == 1, "панель зовёт systemd-analyze один раз на все кроны",
           [f"вызовов {len(s.calls('systemd-analyze'))}"])
    both = entry(result, "job-both")
    report(both.get("next_run", "нет") is None and "не переводится" in str(both.get("next_note") or ""),
           "день месяца И день недели → next_run null, next_note «расписание не переводится в календарь systemd»",
           [repr(both)[:200]])
    saved = os.environ.pop("PROCESS_STATE_ANALYZE", None)
    try:
        base = datetime(2026, 11, 7, 23, 3).astimezone().timestamp()   # суббота
        exprs = {i: calendar_of(CRON[i]) for i in ("job-5min", "job-month", "job-week")}
        got = ps().next_calendar(exprs, base)
    finally:
        if saved is not None:
            os.environ["PROCESS_STATE_ANALYZE"] = saved
    want = {"job-5min": datetime(2026, 11, 7, 23, 5), "job-month": datetime(2026, 12, 1, 9, 0),
            "job-week": datetime(2026, 11, 10, 9, 0)}
    bad = [f"{i}: {datetime.fromtimestamp(got[i]) if isinstance((got or {}).get(i), (int, float)) else (got or {}).get(i)!r}"
           f" ≠ {w}" for i, w in want.items()
           if not isinstance((got or {}).get(i), (int, float)) or abs(got[i] - w.astimezone().timestamp()) > 1]
    report(not bad, "живой systemd-analyze от 07.11 23:03 (сб): */5 → 23:05, «0 9 1 * *» → 01.12 09:00, "
           "«0 9 * * 2» → вт 10.11 09:00", bad)


# ───────────────────────── п.10 строки вне библиотеки ─────────────────────────
@case("10. строка витрины вне библиотеки")
def _outside():
    s = Stand("p10")
    s.put("failed.json", [{"unit": "broken.service", "load": "loaded", "active": "failed", "sub": "failed",
                           "description": "проба"}])
    result = s.resolve(outside=("broken.service", "gone.service"))
    expect(result, "broken.service", "failed", "юнит в list-units --state=failed → failed «systemd: упал»")
    report(value(result, "gone.service") is None, "юнит не упал / не найден → null («—»), а не «не запущен»",
           [repr((result or {}).get("gone.service"))])


# ───────────────────────── стенд диспетчера ─────────────────────────
class Dispatcher(Stand):
    """Стенд CLI process-dispatcher.py: свои журналы, состояние, снимок /proc и cgroup, библиотека."""

    def __init__(self, name, records=RECORDS, schedule=SCHEDULE):
        super().__init__(name)
        self.records, self.schedule = records, schedule
        self.json_path, self.yaml_path = self.dir / "process-library.json", self.dir / "process-library.yaml"
        self.json_path.write_text(json.dumps({"процессы": records}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.yaml_path.write_text(json.dumps({"процессы": schedule, "выключатель": {"исключения": []}},
                                             ensure_ascii=False), encoding="utf-8")   # JSON — подмножество YAML
        proc, cg = self.dir / "proc", self.dir / "cg"
        (cg / "system.slice").mkdir(parents=True)
        proc.mkdir()
        (proc / "stat").write_text("cpu 100 0 0 1000 0 0 0 0\ncpu0 100 0 0 1000 0 0 0 0\nbtime 1700000000\n")
        (proc / "uptime").write_text("100.00 90.00\n")
        for folder in (cg, cg / "system.slice"):
            (folder / "cpu.stat").write_text("usage_usec 1000\n")
        for folder in (self.logs / ".events" / "runs", self.dir / "state" / "heartbeat", self.dir / "transcripts"):
            folder.mkdir(parents=True, exist_ok=True)
        (self.dir / "harness.conf").write_text(f"LOG_DIR={self.logs}\nBACKGROUND_PROCESSES=on\n", encoding="utf-8")
        (self.dir / "install.conf").write_text("", encoding="utf-8")
        self.args = ["--proc", str(proc), "--cgroup", str(cg), "--библиотека", str(self.json_path),
                     "--проект", str(self.dir), "--транскрипты", str(self.dir / "transcripts"), "--журналы", str(self.logs)]
        self.process_env = dict(os.environ, **self.env, HARNESS_LOG_DIR=str(self.logs), LOG_DIR=str(self.logs),
                                STATE_DIR=str(self.dir / "state"), HEARTBEAT_DIR=str(self.dir / "state" / "heartbeat"),
                                HARNESS_EVENTS_STATE=str(self.logs / ".events"),
                                PROCESS_LOAD_STATE=str(self.logs / "process-load.state.json"),
                                HARNESS_CONF=str(self.dir / "harness.conf"),
                                HARNESS_INSTALL_CONF=str(self.dir / "install.conf"), HARNESS_TASK="-",
                                HARNESS_PROCESS_LIBRARY=str(self.yaml_path),
                                HARNESS_PROCESS_LIBRARY_JSON=str(self.json_path),
                                HARNESS_PROCESS_LIBRARY_LOCAL=str(self.dir / "no-layer.json"))
        self.process_env.pop("CLAUDE_CODE_SESSION_ID", None)
        self.samples([])

    def run(self, *mode, timeout=180):
        try:
            done = subprocess.run([sys.executable, str(DISPATCHER), *mode, *self.args], capture_output=True, text=True,
                                  env=self.process_env, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "таймаут"
        return done.returncode, done.stdout + done.stderr

    def view(self):
        try:
            return json.loads((self.logs / "process-dispatcher-view.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}


@case("4/2. диспетчер кладёт state_facts в витрину (полный журнал, сбой docker)")
def _dispatcher_view():
    now = time.time()
    d = Dispatcher("pv")
    first = now - 2 * 86400
    events = end("job-fail", first + 60, 1, pid=4401)
    events[0]["ts"] = iso(first)
    d.runs(events + end("job-ok", now - 600, 0, pid=4402))
    rc, out = d.run("--витрина")
    view = d.view()
    facts = view.get("state_facts") or {}
    report(rc == 0 and (facts.get("last_run") or {}).get("job-fail", {}).get("rc") == 1
           and facts.get("log_start") == events[0]["ts"],
           "--витрина: state_facts.last_run[job-fail].rc = 1, log_start = ts первой строки журнала",
           [f"rc={rc} {out.strip()[-160:]!r}", f"state_facts={json.dumps(facts, ensure_ascii=False)[:200]}"])
    report(bool(facts) and facts.get("checked") == view.get("снято"), "state_facts.checked = «снято» той же сборки",
           [f"checked={facts.get('checked')!r} снято={view.get('снято')!r}"])
    report({"снято", "выключатель", "окна", "сверка", "неразмечено"} <= set(view),
           "старые ключи витрины на месте", [f"ключи {sorted(view)}"])
    d.mode("docker", "fail Cannot connect to the Docker daemon")
    (d.logs / "process-dispatcher-view.json").unlink(missing_ok=True)
    rc, out = d.run("--витрина")
    view = d.view()
    facts = view.get("state_facts") or {}
    report(rc == 0 and bool(facts.get("docker_error")) and facts.get("docker") == {} and "окна" in view,
           "docker код 1 → витрина записана, state_facts.docker_error = причина, docker = {}, окна на месте",
           [f"rc={rc} {out.strip()[-160:]!r}", f"state_facts={json.dumps(facts, ensure_ascii=False)[:200]}"])


# ───────────────────────── п.11 цена счётом ─────────────────────────
@case("11. цена — счётом вызовов docker и systemd-analyze")
def _cost():
    d = Dispatcher("pc")
    eligible = eligible_crons(d.records, d.schedule)
    has_timers = any(r["запуск"] in ("таймер", "служба-проверок") for r in d.records)
    report(bool(eligible) and has_timers, f"разбор своей библиотеки: кронов для пропуска {len(eligible)}, таймеры есть",
           [f"кронов {len(eligible)}"])
    d.clear_calls()
    rc, out = d.run("--замер")
    docker, analyze = len(d.calls("docker")), len(d.calls("systemd-analyze"))
    report(rc == 0 and (d.view().get("state_facts") is not None) and docker == 1 and analyze == len(eligible),
           f"проход --замер со сборкой витрины: docker 1 вызов, systemd-analyze {len(eligible)} (по крону; таймеры не зовут)",
           [f"rc={rc} {out.strip()[-120:]!r}", f"docker {docker}, systemd-analyze {analyze}",
            f"state_facts в витрине: {'есть' if d.view().get('state_facts') is not None else 'нет'}"])
    d.clear_calls()
    for _ in range(2):
        rc, out = d.run("--замер")
    docker, analyze = len(d.calls("docker")), len(d.calls("systemd-analyze"))
    report(rc == 0 and docker == 0 and analyze == 0, "2 прохода --замер без сборки витрины → 0 вызовов docker и systemd-analyze",
           [f"rc={rc}", f"docker {docker}, systemd-analyze {analyze}"])


@case("5б. helpers: панель (gather + resolve) и CLI --за 24ч видят одно состояние")
def _helpers_cli():
    now = time.time()
    d = Dispatcher("p5bcli")
    d.samples([sample(now - 3600, [("helpers", 1), ("claude-session", 2)]),
               sample(now - 60, [("claude-session", 2)])])
    d.agents([agent_event("agent.launch", "call-a", now - 300), agent_event("agent.launch", "call-b", now - 600)])
    panel = d.resolve(now)
    found = entry(panel, "helpers")
    report(found.get("value") == "running" and found.get("running_n") == 2,
           "панель: helpers running, running_n = 2", [f"value={found.get('value')!r} running_n={found.get('running_n')!r}"])
    rc, out = d.run("--за", "24ч")
    row = next((line for line in out.splitlines() if line.startswith("helpers |")), "")
    cell = row.split("|")[1].strip() if row.count("|") > 1 else ""
    labels = getattr(process_state, "LABELS", {}) or {}
    report(rc == 0 and bool(found.get("value")) and cell == labels.get(found.get("value")),
           "CLI: ячейка состояния helpers = подпись того же value, что у панели",
           [f"rc={rc}", f"строка {row!r}", f"панель {found.get('value')!r}"])


# ───────────────────────── п.12 CLI ─────────────────────────
@case("12. CLI --за 24ч: столбец «состояние», ни одного None")
def _cli():
    now = time.time()
    d = Dispatcher("pcli")
    d.runs(end("job-fail", now - 900, 1, pid=4501) + end("job-ok", now - 600, 0, pid=4502))
    rc, out = d.run("--за", "24ч")
    lines = out.splitlines()
    header = next((line for line in lines if line.startswith("процесс |")), "")
    columns = [c.strip() for c in header.split("|")]
    report(rc == 0 and len(columns) > 1 and columns[1] == "состояние",
           "шапка: первый столбец после имени — «состояние»", [f"rc={rc}", f"шапка {header!r}"])
    report(rc == 0 and "None" not in out, "в выводе нет «None»",
           [f"rc={rc}"] + [line for line in lines if "None" in line][:3])
    row = next((line for line in lines if line.startswith("job-fail |")), "")
    report("упал" in row and "код 1" in row, "строка job-fail: «упал (код 1 …)»", [repr(row)])


# ───────────────────────── итог ─────────────────────────
print("── итог по всем случаям: значения и why ──")
seen = {v for _, v, _ in OBSERVED}
report(bool(VALUES) and set(VALUES) <= seen, "у каждого значения VALUES есть хотя бы один случай",
       [f"без случая: {sorted(set(VALUES) - seen)}"])
stray = sorted({f"{i}={v}" for i, v, _ in OBSERVED if v is not None and v not in VALUES})
report(bool(OBSERVED) and bool(VALUES) and not stray, "каждое value результата — из VALUES (§5 п.1)", stray[:8])
missing = [w for i, v, w in OBSERVED if v == "missing"]
report(bool(OBSERVED) and not missing, "в каждом результате есть все записи библиотеки, кроме хуков (§5 п.1)", missing[:3])
empty = sorted({i for i, v, w in OBSERVED if v in ("failed", "skipped", "unknown") and not str(w or "").strip()})
report(bool(OBSERVED) and not empty, "нет failed/skipped/unknown без непустого why (§5 п.2)", empty)
shutil.rmtree(TMP, ignore_errors=True)
print(f"СОСТОЯНИЕ ПРОЦЕССОВ: путей {paths}, неудач {failures}")
sys.exit(1 if failures else 0)
