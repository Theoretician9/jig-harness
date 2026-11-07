#!/usr/bin/env python3
"""Пробы диспетчера процессов, часть В2 (hr.process-dispatcher, спека §В.2, §В.3, §В.5, §В.7).

Доли запусков и токенов, метки вызовов модели, сверка с tokens-collector. Проба
строит во временном каталоге свой мир: журналы process-runs, process-load,
demon-spend, вызовы-агентов, помесячные tokens-YYYY-MM.jsonl и tokens-daily.json,
транскрипты основной сессии, помощника и головы, свою библиотеку и поддельный
/proc — и судит вывод `process-dispatcher.py --за 24ч|7д` и `--сейчас`. Метки
сбора (`agent`, `headless`) проба судит на настоящем tokens-collector.sh, а
`demon.start` и `--session-id` — на настоящем claude-demon.sh с заглушкой claude.
Каждый случай назван поломкой, на которой обязан покраснеть (номера 12–19 —
из раздела «Пробы и критерии частей» спеки).

Контракт показа, который судит проба (§В.5):
    шапка таблицы — строка с «процесс |» и столбцами «запусков», «в работе»,
    «доля запусков», «токены», «доля токенов» (разделитель « | »);
    «в работе» — «N с» / «N мин» / «N ч» (или ч:мм:сс), у оценки — «≈»;
    «доля запусков» — «<по времени> % / <по счёту> %»;
    у служб в «запусков» — «постоянно»; у задания с пропусками — «выключено: N пропусков»;
    у начатого без конца — «оборван» (мёртв) или «идёт» (жив по pid и starttime);
    подвал --за 24ч — «Σ по процессам контекст X / вывод Y; tokens-daily.json X′ / Y′;
    расхождение N токенов», день — `дата` из tokens-daily.json по `дата_события`.
Разрешитель (§В.3 п.3): `process_load.model_call_process(событие, указатели)`;
указатели строит `process_load.model_call_pointers(журналы, транскрипты)`.

Двери кода (CLI): --proc, --cgroup, --библиотека, --проект, --транскрипты, --журналы.
Двери окружения: HARNESS_LOG_DIR / LOG_DIR, TZ (пояс владельца UTC+5 задаёт проба).
Двери пробы:
    HARNESS_PROCESS_DISPATCHER   путь к process-dispatcher.py
    HARNESS_PROCESS_LOAD         путь к lib/process_load.py
    HARNESS_TOKENS_COLLECTOR     путь к tokens-collector.sh
    HARNESS_CLAUDE_DEMON         путь к claude-demon.sh
    HARNESS_PROCESS_LIBRARY_JSON библиотека-образец

Прогон (только фоном):
    python3 scripts/test_process_dispatcher_v2.py
"""
import datetime as dt
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
TICK = 100
PAGE = 4096
RUN = uuid.uuid4().hex[:8]
OWNER_TZ = dt.timezone(dt.timedelta(hours=5))
OWNER_TZ_NAME = "Etc/GMT-5"   # знак POSIX обратный: это UTC+5
LIVE_LOGS = Path("/var/log/harness")
COLLECTOR_DAY = "2026-06-15"


def config_dir() -> Path:
    for folder in HERE.parents:
        if (folder / "config" / "process-library.json").is_file():
            return folder / "config"
    return HERE.parent / "harness" / "config"


def first_existing(*candidates: Path) -> Path:
    return next((c for c in candidates if c.exists()), candidates[0])


CONFIG = config_dir()
DISPATCHER = Path(os.environ.get("HARNESS_PROCESS_DISPATCHER") or HERE / "process-dispatcher.py")
PROCESS_LOAD = Path(os.environ.get("HARNESS_PROCESS_LOAD") or HERE / "lib" / "process_load.py")
LIBRARY_JSON = Path(os.environ.get("HARNESS_PROCESS_LIBRARY_JSON") or CONFIG / "process-library.json")
TOKENS_COLLECTOR = Path(os.environ.get("HARNESS_TOKENS_COLLECTOR") or first_existing(
    HERE.parent / "harness" / "demons" / "tokens-collector.sh", HERE.parent / "demons" / "tokens-collector.sh"))
CLAUDE_DEMON = Path(os.environ.get("HARNESS_CLAUDE_DEMON") or HERE / "claude-demon.sh")
STREAM_FIXTURE = HERE / "fixtures" / "stream-json-sample.jsonl"
# Раскладка проекта и пакета: claude-demon.sh зовёт $PROJECT_DIR/scripts/…
PROJECT_FOR_DEMON = HERE.parent
WRAPPER = HERE / "run-background.sh"

paths = failures = 0


def report(ok: bool, text: str, bad=()) -> None:
    global paths, failures
    paths += 1
    if ok:
        print("ок    · " + text)
        return
    failures += 1
    bad = [str(b) for b in bad]
    tail = f": {'; '.join(bad[:8])}" + (f" … ещё {len(bad) - 8}" if len(bad) > 8 else "") if bad else ""
    print("НЕ ок · " + text + tail)


def near(value, expected: float, tolerance: float) -> bool:
    return isinstance(value, (int, float)) and abs(value - expected) <= tolerance


CASES: list = []


def case(title: str):
    def wrap(fn):
        def runner():
            print(f"── {title} ──")
            try:
                fn()
            except Exception as error:  # noqa: BLE001 — проба называет, а не падает
                report(False, f"{title}: случай упал", [f"{type(error).__name__}: {error}"])
        CASES.append(runner)
        return fn
    return wrap


TMP = Path(tempfile.mkdtemp(prefix=f"test-pd-v2-{RUN}-"))


# ───────────────────────── время и события ─────────────────────────
NOW = time.time()


def moment(seconds_ago: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(NOW - seconds_ago, OWNER_TZ)


def iso(when: dt.datetime) -> str:
    return when.isoformat(timespec="seconds")


def utc_z(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def harness_row(when: dt.datetime, event: str, outcome: str, data: dict, src="scripts/run-background.sh",
                task="-") -> dict:
    return {"v": 1, "ts": iso(when), "src": src, "event": event, "outcome": outcome, "task": task,
            "task_from": "env", "session": None, "data": data}


def sample_row(when: dt.datetime, groups: list) -> dict:
    return harness_row(when, "process.sample", "ok",
                       {"окно_с": 60.0, "ядер": 4, "машина_cpu_с": sum(g["cpu_с"] for g in groups),
                        "группы": groups, "остаток": {"cron.service": 0.0, "вне_корзин": 0.0},
                        "цена": {"cpu_мс": 50, "мс": 60}}, src="scripts/process-dispatcher.py")


def token_event(when: dt.datetime, session: str, counts: tuple, **marks) -> dict:
    ins, out, cache_w, cache_r = counts
    event = {"ts": utc_z(when), "session": session, "model": "claude-opus-5-5", "in": ins, "out": out,
             "cache_w": cache_w, "cache_r": cache_r, "tools": 0}
    event.update(marks)
    return event


def write_jsonl(path: Path, rows: list, mode="w") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def transcript_line(when: dt.datetime, session: str, entrypoint="cli", **extra) -> dict:
    line = {"type": "assistant", "timestamp": utc_z(when), "sessionId": session, "entrypoint": entrypoint,
            "isSidechain": False, "requestId": "req_" + uuid.uuid4().hex[:10],
            "message": {"id": "msg_" + uuid.uuid4().hex[:10], "model": "claude-opus-5-5",
                        "content": [{"type": "text", "text": "ок"}],
                        "usage": {"input_tokens": 1, "output_tokens": 1}}}
    line.update(extra)
    return line


# ───────────────────────── снимок /proc ─────────────────────────
def proc(pid, ppid, sid, comm, argv, cg, started: float, cpu=(0.0, 0.0, 0.0, 0.0), rss_mb=4, env=None):
    """started — момент старта процесса, epoch (starttime пересчитывается от btime снимка)."""
    return {"pid": pid, "ppid": ppid, "sid": sid, "comm": comm, "argv": argv, "cg": cg, "cpu": cpu,
            "started": started, "rss_mb": rss_mb, "env": env or {}}


BOOT_UPTIME = 200000.0
BTIME = int(NOW - BOOT_UPTIME)


def write_snapshot(base: Path, procs: list, uptime: float = BOOT_UPTIME, cores: int = 4) -> tuple:
    """Поддельные /proc и /sys/fs/cgroup. btime = сейчас − uptime, поэтому starttime
    процесса из stat и момент process.start в журнале сопоставимы, как на живой машине."""
    proc_dir, cg_dir = base / "proc", base / "cgroup"
    proc_dir.mkdir(parents=True)
    cg_dir.mkdir(parents=True)
    btime = BTIME
    for p in procs:
        d = proc_dir / str(p["pid"])
        d.mkdir()
        ut, st, cut, cst = (round(x * TICK) for x in p["cpu"])
        pages = round(p["rss_mb"] * 1048576 / PAGE)
        start_ticks = max(1, round((p["started"] - btime) * TICK))
        fields = [p["pid"], f"({p['comm']})", "S", p["ppid"], p["sid"], p["sid"], 0, -1, 4194304,
                  0, 0, 0, 0, ut, st, cut, cst, 20, 0, 1, 0, start_ticks, pages * PAGE * 4, pages] + [0] * 28
        (d / "stat").write_text(" ".join(map(str, fields)) + "\n")
        (d / "status").write_text(f"Name:\t{p['comm']}\nState:\tS (sleeping)\nTgid:\t{p['pid']}\n"
                                  f"Pid:\t{p['pid']}\nPPid:\t{p['ppid']}\nVmRSS:\t{pages * PAGE // 1024} kB\n")
        (d / "statm").write_text(f"{pages * 4} {pages} {pages // 4} 1 0 {pages} 0\n")
        (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in p["argv"]))
        (d / "environ").write_bytes(b"".join(f"{k}={v}".encode() + b"\0" for k, v in p["env"].items()))
        (d / "cgroup").write_text(f"0::{p['cg']}\n")
    for p in procs:
        parts = p["cg"].strip("/").split("/")
        for i in range(1, len(parts) + 1):
            folder = cg_dir / "/".join(parts[:i])
            folder.mkdir(parents=True, exist_ok=True)
            usec = round(sum(sum(q["cpu"]) for q in procs if q["cg"].startswith("/" + "/".join(parts[:i]))) * 1e6)
            (folder / "cpu.stat").write_text(f"usage_usec {usec}\nuser_usec {usec // 2}\nsystem_usec {usec - usec // 2}\n")
    busy = round(sum(sum(p["cpu"]) for p in procs) * TICK)
    idle = round(uptime * cores * TICK) - busy
    lines = [f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0"]
    lines += [f"cpu{i} {busy // cores} 0 0 {idle // cores} 0 0 0 0 0 0" for i in range(cores)]
    (proc_dir / "stat").write_text("\n".join(lines) + f"\nintr 0\nctxt 0\nbtime {btime}\n")
    (proc_dir / "uptime").write_text(f"{uptime:.2f} {uptime * cores * 0.9:.2f}\n")
    return proc_dir, cg_dir


CRON = "/system.slice/cron.service"
AGENT = "/system.slice/harness-agent.service"
INIT = proc(1, 0, 1, "systemd", ["/sbin/init"], "/init.scope", NOW - 200000)
CRON_D = proc(300, 1, 300, "cron", ["/usr/sbin/cron", "-f"], CRON, NOW - 199000)


# ───────────────────────── мир пробы ─────────────────────────
def library_entry(ident: str, start="крон", klass="фоновый") -> dict:
    return {"id": ident, "название": f"Проба В2: {ident}", "класс": klass, "запуск": start, "статус": "проба",
            "файлы": [f"probe/{ident}.sh"],
            "доля_нагрузки": {"приписка": {"способ": "обёртка", "признак": f"run-background.sh {ident}"},
                              "замер": "не измерено"}}


class World:
    """Свои журналы, транскрипты, библиотека и проект — на каждый случай."""

    def __init__(self, name: str, extra_ids=()):
        self.dir = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=TMP))
        self.logs = self.dir / "logs"
        self.state = self.dir / "state"
        self.project = self.dir / "project"
        self.projects = self.dir / "claude-projects"
        self.transcripts = self.projects / "-probe-project"
        for d in (self.logs / ".events" / "runs", self.state / "heartbeat", self.project / "scripts", self.transcripts):
            d.mkdir(parents=True)
        library = json.loads(LIBRARY_JSON.read_text(encoding="utf-8"))
        library["процессы"] += [library_entry(i) for i in extra_ids]
        self.library_path = self.dir / "process-library.json"
        self.library_path.write_text(json.dumps(library, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.conf = self.dir / "harness.conf"
        self.conf.write_text(f"LOG_DIR={self.logs}\nSTATE_DIR={self.state}\nBACKGROUND_PROCESSES=on\n")
        (self.dir / "install.conf").write_text("")
        self.env = dict(os.environ, HARNESS_LOG_DIR=str(self.logs), LOG_DIR=str(self.logs), TZ=OWNER_TZ_NAME,
                        STATE_DIR=str(self.state), HEARTBEAT_DIR=str(self.state / "heartbeat"),
                        HARNESS_EVENTS_STATE=str(self.logs / ".events"),
                        PROCESS_LOAD_STATE=str(self.logs / "process-load.state.json"),
                        HARNESS_CONF=str(self.conf), HARNESS_INSTALL_CONF=str(self.dir / "install.conf"),
                        HARNESS_TASK="-")
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)
        self.snaps = 0

    def snapshot(self, procs: list, uptime: float = BOOT_UPTIME) -> tuple:
        self.snaps += 1
        return write_snapshot(self.dir / f"snap{self.snaps}", procs, uptime)

    def doors(self, snap: tuple) -> list:
        return ["--proc", str(snap[0]), "--cgroup", str(snap[1]), "--библиотека", str(self.library_path),
                "--проект", str(self.project), "--транскрипты", str(self.transcripts), "--журналы", str(self.logs)]

    def run(self, *args, timeout=120) -> tuple:
        if not DISPATCHER.exists():
            return 127, f"кода нет: {DISPATCHER}"
        try:
            done = subprocess.run([sys.executable, str(DISPATCHER), *args], capture_output=True, text=True,
                                  env=self.env, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "таймаут"
        return done.returncode, (done.stdout + done.stderr).strip()

    def tokens_file(self, when: dt.datetime) -> Path:
        return self.logs / f"tokens-{when.strftime('%Y-%m')}.jsonl"

    def link_current_tokens(self) -> None:
        current = self.tokens_file(moment(0))
        current.touch()
        link = self.logs / "tokens.jsonl"
        if not link.is_symlink():
            link.symlink_to(current.name)


# ───────────────────────── разбор показа ─────────────────────────
SPACES = re.compile(r"[\s  ]")


def numbers_of(cell: str) -> list:
    cell = re.sub(r"(?<=\d)[   ](?=\d{3}\b)", "", cell or "")
    return [float(x.replace(",", ".")) for x in re.findall(r"-?\d+(?:[.,]\d+)?", cell)]


def integer_of(cell: str):
    found = re.search(r"-?\d+", SPACES.sub("", cell or ""))
    return int(found.group()) if found else None


UNITS = {"ч": 3600, "час": 3600, "мин": 60, "м": 60, "с": 1, "сек": 1}


def seconds_of(cell: str):
    cell = cell or ""
    clock = re.search(r"(\d+):(\d{2}):(\d{2})", cell)
    if clock:
        h, m, s = map(int, clock.groups())
        return h * 3600 + m * 60 + s
    parts = re.findall(r"(\d+(?:[.,]\d+)?)\s*(час|мин|сек|ч|м|с)\b", cell)
    return sum(float(n.replace(",", ".")) * UNITS[u] for n, u in parts) if parts else None


class Table:
    """Таблица показа: шапка «процесс | …», строки до первой строки без «|»."""

    def __init__(self, out: str):
        self.columns, self.rows, self.lines = [], [], (out or "").splitlines()
        for i, line in enumerate(self.lines):
            cells = [c.strip() for c in line.split("|")]
            if len(cells) > 3 and cells[0] == "процесс":
                self.columns = cells
                for row in self.lines[i + 1:]:
                    if "|" not in row:
                        break
                    if set(row.strip()) <= set("-|+ "):
                        continue
                    self.rows.append([c.strip() for c in row.split("|")])
                break

    def column(self, name: str):
        for i, c in enumerate(self.columns):
            if c == name:
                return i
        for i, c in enumerate(self.columns):
            if c.startswith(name + " ") or c.startswith(name + ","):
                return i
        return None

    def find(self, ident: str) -> list:
        return [r for r in self.rows if r and r[0] == ident]

    def starting(self, prefix: str, skip=()) -> list:
        return [r for r in self.rows if r and r[0].startswith(prefix) and not any(s in r[0] for s in skip)]

    def cell(self, row, name: str) -> str:
        i = self.column(name)
        return row[i] if row is not None and i is not None and i < len(row) else ""

    def missing(self, names) -> list:
        return [n for n in names if self.column(n) is None]

    def ids(self) -> list:
        return [r[0] for r in self.rows]


def describe(rc, out, table: Table) -> list:
    tail = out.splitlines()[-1][:160] if out else ""
    return [f"rc={rc}" + (f" «{tail}»" if tail else ""), f"строк таблицы: {len(table.rows)}"]


def footer_numbers(out: str) -> dict:
    """Подвал сверки §В.3 п.5: контекст/вывод по процессам, по tokens-daily.json, расхождение."""
    plain = re.sub(r"(?<=\d)[   ](?=\d{3}\b)", "", out or "")
    found = {}
    m = re.search(r"Σ по процессам\D*?контекст\s+(-?\d+)\s*/\s*вывод\s+(-?\d+)", plain)
    if m:
        found["процессы"] = (int(m.group(1)), int(m.group(2)))
    m = re.search(r"tokens-daily\.json\s+(-?\d+)\s*/\s*(-?\d+)", plain)
    if m:
        found["daily"] = (int(m.group(1)), int(m.group(2)))
    m = re.search(r"расхождение\s+([+-−]?\d+)\s*токен", plain)
    if m:
        found["расхождение"] = int(m.group(1).replace("−", "-"))
    return found


# ───────────────────────── главный мир --за 24ч ─────────────────────────
JA, JB, DM = f"probe-v2-a-{RUN}", f"probe-v2-b-{RUN}", f"probe-v2-dm-{RUN}"
SESSION, AGENT_ID, TOOL_USE = f"sess-{RUN}", f"a{RUN}f00d", f"toolu_pdv2{RUN}"
SESSION_DEMON, SESSION_ASK, SESSION_LOOSE = f"dmn-{RUN}", f"ask-{RUN}", f"loose-{RUN}"
SESSION_NIGHT = f"night-{RUN}"
PROBE_TASK = f"probe-task-{RUN}"
TOKENS = {  # (in, out, cache_w, cache_r)
    "claude-session": (100, 50, 200, 650),       # 1000
    "helpers": (300, 200, 500, 2000),            # 3000
    DM: (100, 100, 300, 1000),                   # 1500
    "ask-the-model": (50, 50, 100, 500),         # 700
    "модель: голова без метки": (40, 60, 100, 200),   # 400
    "helpers: без метки": (25, 25, 50, 150),     # 250
}
TOTAL_TOKENS = sum(sum(v) for v in TOKENS.values())                 # 6850
TOTAL_OUT = sum(v[1] for v in TOKENS.values())                      # 485
RUN_SECONDS = {JA: 180.0, JB: 20.0, "helpers": 300.0, DM: 40.0}     # знаменатель 540
RUN_COUNTS = {JA: 3, JB: 1, "helpers": 1, DM: 1}                   # знаменатель 6


def build_main_world(name: str, night_alias=False) -> World:
    w = World(name, (JA, JB, DM))
    runs = []
    for k, hours in enumerate((5, 4, 3)):
        start = moment(hours * 3600)
        runs += [harness_row(start, "process.start", "ok", {"id": JA, "pid": 6001 + k, "sid": 6001 + k}),
                 harness_row(start + dt.timedelta(seconds=60), "process.end", "ok",
                             {"id": JA, "pid": 6001 + k, "rc": 0, "с": 60.0, "cpu_с": 1.0})]
    runs += [harness_row(moment(h * 3600), "process.skip", "skipped", {"id": JA, "значение": "off"}) for h in (2, 1)]
    start_b = moment(3.5 * 3600)
    runs += [harness_row(start_b, "process.start", "ok", {"id": JB, "pid": 6010, "sid": 6010}),
             harness_row(start_b + dt.timedelta(seconds=20), "process.end", "ok",
                         {"id": JB, "pid": 6010, "rc": 0, "с": 20.0, "cpu_с": 0.2})]
    # Прогон службы (её id попал в process-runs): без него проба 16 не видит службу в знаменателе.
    start_s = moment(2.5 * 3600)
    runs += [harness_row(start_s, "process.start", "ok", {"id": "harness-dispatcher", "pid": 6020, "sid": 6020}),
             harness_row(start_s + dt.timedelta(seconds=600), "process.end", "ok",
                         {"id": "harness-dispatcher", "pid": 6020, "rc": 0, "с": 600.0, "cpu_с": 5.0})]
    write_jsonl(w.logs / "process-runs.jsonl", sorted(runs, key=lambda r: r["ts"]))

    launched = moment(2 * 3600)
    launch_data = {"тип": "harness-worker", "модель": "sonnet", "вызов": TOOL_USE, "родитель": "toolu_parent",
                   "часть": f"Часть: {PROBE_TASK}#1", "описание": "проба В2"}
    write_jsonl(w.logs / "вызовы-агентов.jsonl", [
        harness_row(launched, "agent.launch", "ok", launch_data, src="scripts/hooks/agent_budget_guard.py",
                    task=PROBE_TASK),
        # Пропущенный запуск помощником не считается.
        harness_row(launched, "agent.launch", "skipped", dict(launch_data, вызов=TOOL_USE + "x"),
                    src="scripts/hooks/agent_budget_guard.py", task=PROBE_TASK)])
    sub = w.transcripts / SESSION / "subagents"
    sub.mkdir(parents=True)
    write_jsonl(sub / f"agent-{AGENT_ID}.jsonl", [
        transcript_line(launched, SESSION, isSidechain=True, agentId=AGENT_ID),
        transcript_line(launched + dt.timedelta(seconds=300), SESSION, isSidechain=True, agentId=AGENT_ID)])
    (sub / f"agent-{AGENT_ID}.meta.json").write_text(json.dumps(
        {"agentType": "harness-worker", "description": "проба В2", "toolUseId": TOOL_USE, "model": "sonnet"}))
    write_jsonl(w.transcripts / f"{SESSION}.jsonl", [transcript_line(moment(3600), SESSION)])
    ask_dir = w.projects / f"-tmp-ask-model-{RUN}"
    write_jsonl(ask_dir / f"{SESSION_ASK}.jsonl", [transcript_line(moment(3600), SESSION_ASK, entrypoint="sdk-cli")])

    called = moment(1.5 * 3600)
    spend = [{"event": "demon.start", "демон": DM, "session_id": SESSION_DEMON, "когда": iso(called)},
             {"когда": iso(called + dt.timedelta(seconds=40)), "демон": DM, "модель": "claude-sonnet-5-5",
              "session_id": SESSION_DEMON, "total_cost_usd": 0.01, "input_tokens": 100, "output_tokens": 100,
              "cache_creation_input_tokens": 300, "cache_read_input_tokens": 1000, "num_turns": 2,
              "duration_ms": 40000, "is_error": False, "api_error": None}]
    if night_alias:
        spend.append({"event": "demon.start", "демон": "checks-night", "session_id": SESSION_NIGHT,
                      "когда": iso(called)})
    write_jsonl(w.logs / "demon-spend.jsonl", spend)

    at = moment(3600)
    events = [token_event(at, SESSION, TOKENS["claude-session"]),
              token_event(at, SESSION, TOKENS["helpers"], side=True, agent=AGENT_ID),
              token_event(at, SESSION_DEMON, TOKENS[DM], headless=True),
              token_event(at, SESSION_ASK, TOKENS["ask-the-model"], headless=True),
              token_event(at, SESSION_LOOSE, TOKENS["модель: голова без метки"], headless=True),
              token_event(at, SESSION, TOKENS["helpers: без метки"], side=True)]
    write_jsonl(w.tokens_file(moment(0)), events)
    w.link_current_tokens()
    (w.logs / "tokens-daily.json").write_text(json.dumps(
        {"дата": at.date().isoformat(), "шагов": len(events), "кэш прочитано": sum(v[3] for v in TOKENS.values()),
         "кэш записано": sum(v[2] for v in TOKENS.values()), "свежий вход": sum(v[0] for v in TOKENS.values()),
         "контекст всего": TOTAL_TOKENS - TOTAL_OUT, "ответов всего": TOTAL_OUT}, ensure_ascii=False, indent=1))

    samples = [sample_row(moment(m * 60), [
        {"id": "harness-dispatcher", "прогон": "harness-dispatcher.service", "cpu_с": 0.5, "rss_мб": 30, "процессов": 3},
        {"id": "claude-session", "прогон": "harness-agent.service", "cpu_с": 2.0, "rss_мб": 400, "процессов": 5}])
        for m in range(360, 0, -10)]
    write_jsonl(w.logs / "process-load.jsonl", samples)
    return w


MAIN: dict = {}


def main_window() -> tuple:
    if not MAIN:
        w = build_main_world("main")
        snap = w.snapshot([INIT, CRON_D])
        rc, out = w.run("--за", "24ч", *w.doors(snap))
        MAIN.update(world=w, rc=rc, out=out, table=Table(out))
    return MAIN["world"], MAIN["rc"], MAIN["out"], MAIN["table"]


RUN_COLUMNS = ("запусков", "в работе", "доля запусков", "токены", "доля токенов")


def run_share(t: Table, row) -> tuple:
    found = numbers_of(t.cell(row, "доля запусков"))
    return (found[0] if found else None, found[1] if len(found) > 1 else None)


def helper_rows(t: Table) -> list:
    return t.starting("helpers", skip=("без метки", "не определён"))


@case("приёмка В2: --за 24ч даёт столбцы трёх долей (§В.5)")
def case_columns():
    w, rc, out, t = main_window()
    missing = t.missing(("ядро-ч", "% ёмкости") + RUN_COLUMNS)
    report(rc == 0 and bool(t.rows) and not missing,
           "шапка «процесс | ядро-ч | % ёмкости | … | запусков | в работе | доля запусков | токены | доля токенов»",
           [f"нет столбцов: {missing}", f"столбцы: {t.columns}"] + describe(rc, out, t))


@case("18. process.skip посчитан запуском → доля неверна")
def case_skip():
    w, rc, out, t = main_window()
    rows = t.find(JA)
    row = rows[0] if rows else None
    count = integer_of(t.cell(row, "запусков"))
    spent = seconds_of(t.cell(row, "в работе"))
    by_time, by_count = run_share(t, row)
    report(count == 3, f"{JA}: запусков 3 (два process.skip не в счёте)",
           [f"ячейка «{t.cell(row, 'запусков')}»", f"строки: {t.ids()[:12]}"] + describe(rc, out, t))
    report(near(spent, 180, 2), f"{JA}: в работе 180 с (3 × process.end.с)", [f"ячейка «{t.cell(row, 'в работе')}»"])
    report(near(by_count, 50.0, 0.6), f"{JA}: доля по счёту 3 / 6 = 50 %",
           [f"ячейка «{t.cell(row, 'доля запусков')}»"])
    skip_lines = [line for line in t.lines if JA in line and re.search(r"выключено:\s*2\s+пропуск", line)]
    report(bool(skip_lines), f"{JA}: пропуски отдельно — «выключено: 2 пропусков»",
           [l for l in t.lines if JA in l][:3] or ["строки нет"])


@case("16. службы в знаменателе → доли запусков занижены")
def case_services():
    w, rc, out, t = main_window()
    rows = t.find("harness-dispatcher")
    service = rows[0] if rows else None
    report(service is not None and "постоянно" in t.cell(service, "запусков"),
           "harness-dispatcher (служба): в «запусков» — «постоянно»",
           [f"ячейка «{t.cell(service, 'запусков')}»" if service else "строки нет"] + describe(rc, out, t))
    shares = {}
    for ident, seconds in RUN_SECONDS.items():
        rows = helper_rows(t) if ident == "helpers" else t.find(ident)
        shares[ident] = run_share(t, rows[0] if rows else None)[0]
    expected = {ident: round(100 * s / sum(RUN_SECONDS.values()), 1) for ident, s in RUN_SECONDS.items()}
    bad = [f"{i}: {shares[i]} вместо {e}" for i, e in expected.items() if not near(shares[i], e, 0.6)]
    report(not bad, "доля по времени = в работе / Σ запусков окна (540 с: 33,3 / 3,7 / 55,6 / 7,4 %), службы вне знаменателя",
           bad)


@case("12б. помощник в строке основной сессии (запуски и токены)")
def case_helper_split():
    w, rc, out, t = main_window()
    helpers = helper_rows(t)
    count = sum(integer_of(t.cell(r, "запусков")) or 0 for r in helpers)
    spent = sum(seconds_of(t.cell(r, "в работе")) or 0 for r in helpers)
    report(bool(helpers) and count == 1 and near(spent, 300, 2),
           "helpers: запуск = agent.launch ok (skipped не в счёте), в работе = последний − первый timestamp (300 с)",
           [f"строки helpers: {[r[0] for r in helpers]}", f"запусков {count}, в работе {spent}"] + describe(rc, out, t))
    tokens = sum(integer_of(t.cell(r, "токены")) or 0 for r in helpers)
    report(tokens == sum(TOKENS["helpers"]), "токены помощника с меткой agent — в helpers (3000)",
           [f"helpers {tokens}"])
    detail = " ".join(" | ".join(r) for r in helpers) + " " + out
    report("harness-worker" in detail and "sonnet" in detail,
           "разрез помощника тип/модель из agent.launch (harness-worker sonnet) виден в показе", [r[0] for r in helpers])
    rows = t.find("claude-session")
    main_tokens = integer_of(t.cell(rows[0], "токены")) if rows else None
    report(main_tokens == sum(TOKENS["claude-session"]), "claude-session: только свои 1000 токенов",
           [f"claude-session {main_tokens}"])


@case("14. headless спутан с основной сессией")
def case_headless():
    w, rc, out, t = main_window()
    got = {}
    for ident in ("claude-session", DM, "ask-the-model", "модель: голова без метки", "helpers: без метки"):
        rows = t.find(ident)
        got[ident] = integer_of(t.cell(rows[0], "токены")) if rows else None
    bad = [f"{i}: {got[i]} вместо {sum(TOKENS[i])}" for i in got if got[i] != sum(TOKENS[i])]
    report(not bad, f"голова demon-spend → {DM}, -tmp-ask-model-* → ask-the-model, прочая → «модель: голова без метки», "
                    "side без agent → «helpers: без метки», основная сессия 1000", bad + describe(rc, out, t))
    rows = t.find("claude-session")
    share = numbers_of(t.cell(rows[0], "доля токенов")) if rows else []
    report(bool(share) and near(share[0], round(100 * 1000 / TOTAL_TOKENS, 1), 0.2),
           f"доля токенов claude-session = 1000 / {TOTAL_TOKENS} (14,6 %)", [f"ячейка {share}"])


@case("приёмка В2: подвал сверки называет число (ждём 0)")
def case_footer():
    w, rc, out, t = main_window()
    found = footer_numbers(out)
    report(found.get("процессы") == (TOTAL_TOKENS - TOTAL_OUT, TOTAL_OUT),
           f"«Σ по процессам контекст {TOTAL_TOKENS - TOTAL_OUT} / вывод {TOTAL_OUT}»", [found or "подвала нет"])
    report(found.get("daily") == (TOTAL_TOKENS - TOTAL_OUT, TOTAL_OUT),
           f"«tokens-daily.json {TOTAL_TOKENS - TOTAL_OUT} / {TOTAL_OUT}»", [found or "подвала нет"])
    report(found.get("расхождение") == 0, "«расхождение 0 токенов»", [found or "подвала нет"])
    loose = [l for l in out.splitlines() if "без метки" in l and "%" in l and "|" not in l]
    report(bool(loose), "рядом — доля «без метки» в процентах", ["строки нет"])
    demons = [l for l in out.splitlines() if "demon-spend" in l and re.search(r"[+-−]?\d+(?:[.,]\d+)?\s*%", l)]
    report(bool(demons), "сверка демонов «транскрипты против demon-spend» — числом в %", ["строки нет"])


@case("§В.5 --сейчас: столбцы токенов и шапка журнала токенов")
def case_now():
    w = build_main_world("now")
    snap = w.snapshot([INIT, CRON_D])
    rc, out = w.run("--сейчас", *w.doors(snap))
    t = Table(out)
    missing = t.missing(("токены за час", "доля токенов"))
    report(rc == 0 and not missing and "журнал токенов на" in out,
           "--сейчас: «токены за час | доля токенов», шапка «журнал токенов на <время>»",
           [f"нет столбцов: {missing}", f"столбцы: {t.columns}"] + describe(rc, out, t))


# ───────────────────────── 12а: команда помощника на снимках ─────────────────────────
@case("12а. команда помощника приписана основной сессии (замер)")
def case_helper_command():
    w = World("cmd")
    command = f"make probe-v2-{RUN}"
    open_main = {"type": "assistant", "timestamp": utc_z(moment(30)), "sessionId": SESSION,
                 "message": {"content": [{"type": "tool_use", "id": "toolu_main_open", "name": "Bash",
                                          "input": {"command": "ls -la"}}]}}
    closed_same = {"type": "assistant", "timestamp": utc_z(moment(600)), "sessionId": SESSION,
                   "message": {"content": [{"type": "tool_use", "id": "toolu_main_done", "name": "Bash",
                                            "input": {"command": command}}]}}
    result = {"type": "user", "timestamp": utc_z(moment(590)), "sessionId": SESSION,
              "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_main_done", "content": "ok"}]}}
    write_jsonl(w.transcripts / f"{SESSION}.jsonl", [closed_same, result, open_main])
    helper_open = {"type": "assistant", "timestamp": utc_z(moment(20)), "sessionId": SESSION, "isSidechain": True,
                   "agentId": AGENT_ID, "message": {"content": [{"type": "tool_use", "id": "toolu_helper_open",
                                                                  "name": "Bash", "input": {"command": command}}]}}
    write_jsonl(w.transcripts / SESSION / "subagents" / f"agent-{AGENT_ID}.jsonl", [helper_open])
    env = {"CLAUDE_CODE_SESSION_ID": SESSION}
    shell = ["/bin/bash", "-c", f"source /home/user/.claude/shell-snapshots/snapshot-bash-{RUN}.sh && "
                                f"eval '{command}' < /dev/null && pwd -P >| /tmp/claude-cwd"]

    def world(cpu_child):
        return [INIT, proc(700, 650, 650, "claude", ["claude", "--dangerously-skip-permissions"], AGENT,
                           NOW - 3000, cpu=(10.0, 0, 0, 0), rss_mb=300, env=env),
                proc(710, 700, 710, "bash", shell, AGENT, NOW - 100, env=env),
                proc(711, 710, 710, "make", ["make", f"probe-v2-{RUN}"], AGENT, NOW - 99,
                     cpu=(cpu_child, 0, 0, 0), env=env)]
    journal = w.logs / "process-load.jsonl"
    w.run("--замер", *w.doors(w.snapshot(world(0.0), BOOT_UPTIME - 60)))
    rc, out = w.run("--замер", *w.doors(w.snapshot(world(1.5))))
    rows = [r for r in (json.loads(l) for l in journal.read_text(encoding="utf-8").splitlines())
            if r.get("event") == "process.sample"] if journal.exists() else []
    groups = ((rows[-1].get("data") or {}).get("группы") or []) if rows else []
    helper = [g for g in groups if g.get("id") == "helpers" and g.get("агент") == AGENT_ID]
    cpu = round(sum(float(g.get("cpu_с") or 0) for g in helper), 2)
    report(near(cpu, 1.5, 0.1), f"открытый tool_use в subagents/agent-{AGENT_ID}.jsonl → helpers, агент = id (1,5 с), "
                                "закрытая та же команда основной сессии не перебивает",
           [f"группы: {[(g.get('id'), g.get('агент'), g.get('cpu_с')) for g in groups]}",
            f"rc={rc} «{out[-120:]}»", f"строк process.sample: {len(rows)}"])


# ───────────────────────── 15: сверка в поясе владельца ─────────────────────────
@case("15. сверка в UTC вместо дата_события → ≠ 0 на событии 20:00Z")
def case_reconcile_tz():
    w = World("tz")
    day = (moment(0) - dt.timedelta(days=1)).date()        # вчера по часам владельца
    before = day - dt.timedelta(days=1)
    z = lambda d, hh: dt.datetime(d.year, d.month, d.day, hh, 0, tzinfo=dt.timezone.utc)
    events = [token_event(z(before, 20), SESSION, (100, 10, 0, 0)),    # 01:00 дня владельца
              token_event(z(day, 10), SESSION, (200, 20, 0, 0)),       # 15:00 дня владельца
              token_event(z(day, 20), SESSION, (5000, 500, 0, 0))]     # 01:00 следующего дня
    write_jsonl(w.logs / f"tokens-{day.strftime('%Y-%m')}.jsonl", events)
    w.link_current_tokens()
    (w.logs / "tokens-daily.json").write_text(json.dumps(
        {"дата": day.isoformat(), "контекст всего": 300, "ответов всего": 30, "свежий вход": 300,
         "кэш прочитано": 0, "кэш записано": 0}, ensure_ascii=False, indent=1))
    rc, out = w.run("--за", "24ч", *w.doors(w.snapshot([INIT, CRON_D])))
    found = footer_numbers(out)
    report(found.get("процессы") == (300, 30) and found.get("daily") == (300, 30) and found.get("расхождение") == 0,
           f"день {day} по дата_события (UTC+5): контекст 300 / вывод 30 с обеих сторон, расхождение 0",
           [found or "подвала нет", f"rc={rc} «{out[-160:]}»"])


# ───────────────────────── 7 суток через границу месяца ─────────────────────────
@case("§В.3 п.4. окно 7 сут через границу месяца: два файла, символлинк не вдвое")
def case_week():
    w = World("week")
    old, new, ancient = moment(6 * 86400), moment(3600), moment(8 * 86400)
    old_file, new_file = w.tokens_file(moment(7 * 86400)), w.tokens_file(moment(0))
    write_jsonl(old_file, [token_event(ancient, SESSION, (9000, 0, 0, 0)), token_event(old, SESSION, (400, 0, 0, 0))])
    write_jsonl(new_file, [token_event(new, SESSION, (600, 0, 0, 0))], mode="a")
    w.link_current_tokens()
    rc, out = w.run("--за", "7д", *w.doors(w.snapshot([INIT, CRON_D])))
    t = Table(out)
    rows = t.find("claude-session")
    tokens = integer_of(t.cell(rows[0], "токены")) if rows else None
    border = "да" if old_file != new_file else "нет (на эту дату граница вне окна)"
    report(tokens == 1000, f"claude-session за 7д = 400 (−6 сут) + 600 (−1 ч); −8 сут вне окна; граница месяца в окне: {border}",
           [f"токены {tokens}"] + describe(rc, out, t))


# ───────────────────────── 19: оборван / идёт ─────────────────────────
@case("19. process.start без end при мёртвом pid показан «идёт» или пропал")
def case_torn():
    jc, jd, je = f"probe-v2-torn-{RUN}", f"probe-v2-live-{RUN}", f"probe-v2-reuse-{RUN}"
    w = World("torn", (jc, jd, je))
    t_c, t_d, t_e = moment(3 * 3600), moment(20 * 60), moment(2 * 3600)
    write_jsonl(w.logs / "process-runs.jsonl", [
        harness_row(t_c, "process.start", "ok", {"id": jc, "pid": 7001, "sid": 7001}),
        harness_row(t_e, "process.start", "ok", {"id": je, "pid": 7003, "sid": 7003}),
        harness_row(t_d, "process.start", "ok", {"id": jd, "pid": 7002, "sid": 7002})])
    samples = []
    for m in range(180, 0, -1):
        groups = []
        if 170 <= m <= 180:   # оборванный виден 10 минут после старта
            groups.append({"id": jc, "прогон": "7001", "cpu_с": 1.0, "rss_мб": 10, "процессов": 1})
        if 115 <= m <= 120:   # pid позже занят чужим процессом; свой виден 5 минут
            groups.append({"id": je, "прогон": "7003", "cpu_с": 1.0, "rss_мб": 10, "процессов": 1})
        if m <= 20:
            groups.append({"id": jd, "прогон": "7002", "cpu_с": 1.0, "rss_мб": 10, "процессов": 1})
        if groups or m % 10 == 0:
            samples.append(sample_row(moment(m * 60), groups))
    write_jsonl(w.logs / "process-load.jsonl", samples)
    alive = proc(7002, 300, 7002, "run-background.", ["/bin/bash", str(WRAPPER), jd, "--", "/bin/sleep", "9999"],
                 CRON, t_d.timestamp())
    reused = proc(7003, 1, 7003, "sleep", ["sleep", "60"], "/user.slice", NOW - 60)
    rc, out = w.run("--за", "24ч", *w.doors(w.snapshot([INIT, CRON_D, alive, reused])))
    t = Table(out)

    def row_of(ident):
        rows = t.find(ident)
        return rows[0] if rows else None
    torn, live, reuse = row_of(jc), row_of(jd), row_of(je)
    torn_text = " | ".join(torn or [])
    report(torn is not None and "оборван" in torn_text and "идёт" not in torn_text,
           f"{jc}: pid мёртв → «оборван»", [torn_text or "строка пропала"] + describe(rc, out, t))
    report(integer_of(t.cell(torn, "запусков")) == 1, f"{jc}: оборванный прогон в счёте (запусков 1)",
           [f"ячейка «{t.cell(torn, 'запусков')}»"])
    spent_cell = t.cell(torn, "в работе")
    report("≈" in spent_cell and near(seconds_of(spent_cell), 600, 120),
           f"{jc}: в работе ≈ до последнего прохода В1, где был виден (≈ 10 мин)", [f"ячейка «{spent_cell}»"])
    live_text = " | ".join(live or [])
    report(live is not None and "идёт" in live_text and "оборван" not in live_text,
           f"{jd}: (pid, starttime) жив → «идёт»", [live_text or "строки нет"])
    reuse_text = " | ".join(reuse or [])
    report(reuse is not None and "оборван" in reuse_text and near(seconds_of(t.cell(reuse, "в работе")), 300, 120),
           f"{je}: pid занят чужим процессом (другой starttime) → «оборван», ≈ 5 мин", [reuse_text or "строки нет"])


# ───────────────────────── разрешитель §В.3 п.3 ─────────────────────────
def process_id(result):
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return result.get("id")
    if isinstance(result, (tuple, list)) and result:
        return result[0]
    return None


@case("§В.3 п.3. разрешитель process_load.model_call_process(событие, указатели)")
def case_resolver():
    if not PROCESS_LOAD.exists():
        report(False, "модуль process_load есть", [f"нет {PROCESS_LOAD}"])
        return
    w = build_main_world("resolver", night_alias=True)
    os.environ.update(HARNESS_LOG_DIR=str(w.logs), LOG_DIR=str(w.logs), HARNESS_TASK="-")
    for folder in (str(PROCESS_LOAD.parent), str(PROCESS_LOAD.parent.parent)):
        if folder not in sys.path:
            sys.path.insert(0, folder)
    spec = importlib.util.spec_from_file_location("process_load_v2probe", PROCESS_LOAD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resolve = getattr(module, "model_call_process", None)
    build = getattr(module, "model_call_pointers", None)
    report(callable(resolve) and len(inspect.signature(resolve).parameters) >= 2,
           "model_call_process(событие, указатели) есть", [f"model_call_process: {resolve!r}"])
    report(callable(build), "model_call_pointers(журналы, транскрипты) есть", [f"model_call_pointers: {build!r}"])
    if not (callable(resolve) and callable(build)):
        return
    pointers = build(str(w.logs), str(w.transcripts))
    at = moment(3600)
    expectations = [
        ("основная сессия (не headless, не side)", token_event(at, SESSION, (1, 1, 0, 0)), "claude-session"),
        ("agent → meta → agent.launch", token_event(at, SESSION, (1, 1, 0, 0), side=True, agent=AGENT_ID), "helpers"),
        ("headless + session в demon-spend", token_event(at, SESSION_DEMON, (1, 1, 0, 0), headless=True), DM),
        ("headless + demon-spend checks-night → checks-stream",
         token_event(at, SESSION_NIGHT, (1, 1, 0, 0), headless=True), "checks-stream"),
        ("headless + -tmp-ask-model-*", token_event(at, SESSION_ASK, (1, 1, 0, 0), headless=True), "ask-the-model"),
        ("прочий headless", token_event(at, SESSION_LOOSE, (1, 1, 0, 0), headless=True), "модель: голова без метки"),
        ("side без agent (события до правки)", token_event(at, SESSION, (1, 1, 0, 0), side=True), "helpers: без метки"),
    ]
    for title, event, expected in expectations:
        got = resolve(event, pointers)
        report(process_id(got) == expected, f"{title} → {expected}", [f"вышло {got!r}"])
    got = resolve(token_event(at, SESSION, (1, 1, 0, 0), side=True, agent=AGENT_ID), pointers)
    text = json.dumps(got, ensure_ascii=False, default=str)
    report(all(x in text for x in ("harness-worker", "sonnet", PROBE_TASK)),
           "помощник: тип/модель/задача из agent.launch (harness-worker, sonnet, задача)", [text[:200]])


# ───────────────────────── 13: tokens-collector ставит метки ─────────────────────────
@case("13. tokens-collector без «agent» / без «headless»")
def case_collector():
    if not TOKENS_COLLECTOR.exists():
        report(False, "tokens-collector.sh есть", [f"нет {TOKENS_COLLECTOR}"])
        return
    stand = Path(tempfile.mkdtemp(prefix="collector-", dir=TMP))
    logs, hb, tr = stand / "log", stand / "hb", stand / "tr"
    for d in (logs, hb):
        d.mkdir()
    (stand / "install.conf").write_text(f'AGENT_USER="no-such-user-pdv2"\nHEARTBEAT_DIR="{hb}"\nLOG_DIR="{logs}"\n')
    (stand / "harness.conf").write_text("")
    day = dt.date.fromisoformat(COLLECTOR_DAY)
    at = lambda hh: dt.datetime(day.year, day.month, day.day, hh, 0, tzinfo=dt.timezone.utc)
    usage = {"input_tokens": 11, "output_tokens": 7, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 3}

    def line(hh, session, **extra):
        row = transcript_line(at(hh), session, **extra)
        row["message"]["usage"] = dict(usage)
        return row
    main = line(9, "S1")
    write_jsonl(tr / "-probe-project" / "S1.jsonl", [main])
    write_jsonl(tr / "-probe-project" / "S1" / "subagents" / "agent-ab12cd.jsonl",
                [line(10, "S1", isSidechain=True, agentId="ab12cd")])
    nameless = line(11, "S1", isSidechain=True)
    write_jsonl(tr / "-probe-project" / "S1" / "subagents" / "agent-ef34gh.jsonl", [nameless])
    write_jsonl(tr / f"-tmp-ask-model-{RUN}" / "H1.jsonl", [line(12, "H1", entrypoint="sdk-cli")])
    env = dict(os.environ, TZ=OWNER_TZ_NAME, TOKENS_TODAY=COLLECTOR_DAY, HARNESS_INSTALL_CONF=str(stand / "install.conf"),
               HARNESS_CONF=str(stand / "harness.conf"), TOKENS_TRANSCRIPTS_DIR=str(tr), LOG_DIR=str(logs),
               HEARTBEAT_DIR=str(hb), AGENT_USER="no-such-user-pdv2")
    env.pop("PROJECT_DIR", None)
    done = subprocess.run(["bash", str(TOKENS_COLLECTOR)], capture_output=True, text=True, env=env, timeout=120)
    events_path = logs / f"tokens-{COLLECTOR_DAY[:7]}.jsonl"
    raw = events_path.read_text(encoding="utf-8").splitlines() if events_path.exists() else []
    events = [json.loads(r) for r in raw]
    report(done.returncode == 0 and len(events) == 4, "сбор прошёл, событий 4 (пустой разбор — красный)",
           [f"rc={done.returncode}", f"событий {len(events)}", (done.stdout + done.stderr).strip()[-160:]])
    by_ts = {e.get("ts"): (e, r) for e, r in zip(events, raw)}
    expected_main = json.dumps({"ts": main["timestamp"], "session": "S1", "model": "claude-opus-5-5", "in": 11,
                                "out": 7, "cache_w": 5, "cache_r": 3, "tools": 0}, ensure_ascii=False)
    got_main = by_ts.get(main["timestamp"], ({}, ""))[1]
    report(got_main == expected_main, "событие основной сессии байт в байт прежнее (без agent/headless/side)",
           [f"вышло {got_main[:200]}"])
    helper = by_ts.get(utc_z(at(10)), ({}, ""))[0]
    report(helper.get("agent") == "ab12cd" and helper.get("side") is True and "headless" not in helper,
           "помощник: \"agent\": agentId из строки, side остаётся", [helper or "события нет"])
    fallback = by_ts.get(utc_z(at(11)), ({}, ""))[0]
    report(fallback.get("agent") == "ef34gh", "помощник без agentId в строке: agent из имени файла agent-<id>.jsonl",
           [fallback or "события нет"])
    head = by_ts.get(utc_z(at(12)), ({}, ""))[0]
    report(head.get("headless") is True and "agent" not in head and "side" not in head,
           "головная сессия (entrypoint ≠ cli): \"headless\": true", [head or "события нет"])


# ───────────────────────── 17: claude-demon пишет demon.start ─────────────────────────
STUB_CLAUDE = r"""#!/usr/bin/env bash
printf '%s\n' "$@" >> "$CLAUDE_ARGS"
printf -- '--конец вызова--\n' >> "$CLAUDE_ARGS"
sid="" prev=""
for a in "$@"; do
    [ "$prev" = "--session-id" ] && sid="$a"
    case "$a" in --session-id=*) sid="${a#*=}" ;; esac
    prev="$a"
done
if grep -q '"demon.start"' "$LOG_DIR/demon-spend.jsonl" 2>/dev/null && grep -q "$sid" "$LOG_DIR/demon-spend.jsonl" 2>/dev/null && [ -n "$sid" ]; then
    echo да >> "$CLAUDE_SAW"
else
    echo нет >> "$CLAUDE_SAW"
fi
if [ -n "$sid" ]; then
    sed "s/\"session_id\":\"[^\"]*\"/\"session_id\":\"$sid\"/g" "$CLAUDE_FLOW"
else
    cat "$CLAUDE_FLOW"
fi
"""
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@case("17. нет demon.start / --session-id в claude-demon.sh")
def case_demon():
    if not (CLAUDE_DEMON.exists() and STREAM_FIXTURE.exists()):
        report(False, "claude-demon.sh и образец потока есть", [f"{CLAUDE_DEMON}", f"{STREAM_FIXTURE}"])
        return
    stand = Path(tempfile.mkdtemp(prefix="demon-", dir=TMP))
    logs, binary, panel = stand / "log", stand / "bin", stand / "panel-state"
    for d in (logs, binary, panel, stand / "hb"):
        d.mkdir()
    (binary / "claude").write_text(STUB_CLAUDE)
    (binary / "tg_send.sh").write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$TG_OUT\"\n")
    for f in ("claude", "tg_send.sh"):
        (binary / f).chmod(0o755)
    (stand / "install.conf").write_text(f'PROJECT_DIR="{PROJECT_FOR_DEMON}"\nLOG_DIR="{logs}"\n'
                                        f'HEARTBEAT_DIR="{stand / "hb"}"\nAGENT_USER="no-such-user-pdv2"\n')
    (stand / "harness.conf").write_text("")
    env = dict(os.environ, PATH=f"{binary}:{os.environ.get('PATH', '')}", LOG_DIR=str(logs),
               HEARTBEAT_DIR=str(stand / "hb"), PROJECT_DIR=str(PROJECT_FOR_DEMON), HARNESS_PANEL_STATE=str(panel),
               HARNESS_INSTALL_CONF=str(stand / "install.conf"), HARNESS_CONF=str(stand / "harness.conf"),
               TG_SEND=str(binary / "tg_send.sh"), TG_OUT=str(stand / "told.txt"), CLAUDE_ARGS=str(stand / "args.txt"),
               CLAUDE_SAW=str(stand / "saw.txt"), CLAUDE_FLOW=str(STREAM_FIXTURE), PYTHONDONTWRITEBYTECODE="1")
    demon = f"probe-v2-demon-{RUN}"
    codes = [subprocess.run(["bash", str(CLAUDE_DEMON), demon, "-p", "проба"], capture_output=True, text=True,
                            env=env, timeout=120).returncode for _ in range(2)]
    calls = [c.strip().split("\n") for c in (stand / "args.txt").read_text().split("--конец вызова--")
             if c.strip()] if (stand / "args.txt").exists() else []
    ids = [c[c.index("--session-id") + 1] if "--session-id" in c[:-1] else None for c in calls]
    report(codes == [0, 0] and len(calls) == 2, "два вызова обёртки дошли до claude-заглушки с кодом 0",
           [f"коды {codes}, вызовов {len(calls)}"])
    report(len(ids) == 2 and all(i and UUID.match(i) for i in ids) and ids[0] != ids[1],
           "claude получает --session-id <uuid>, свой на каждый вызов", [f"id: {ids}"])
    report(not any("--resume" in c for c in calls), "--resume не передаётся", [])
    spend_path = logs / "demon-spend.jsonl"
    spend = [json.loads(l) for l in spend_path.read_text(encoding="utf-8").splitlines() if l.strip()] \
        if spend_path.exists() else []
    starts = [r for r in spend if r.get("event") == "demon.start"]
    good = [r for r in starts if r.get("демон") == demon and r.get("session_id") in ids]
    try:
        stamped = all(dt.datetime.fromisoformat(str(r.get("когда"))).tzinfo for r in good)
    except ValueError:
        stamped = False
    report(len(good) == 2 and stamped, "demon-spend: demon.start {демон, session_id, когда с поясом} на каждый вызов",
           [f"строк demon.start {len(starts)}", *(json.dumps(r, ensure_ascii=False)[:160] for r in starts[:2])])
    saw = (stand / "saw.txt").read_text().split() if (stand / "saw.txt").exists() else []
    report(saw == ["да", "да"], "demon.start записан ДО вызова claude (заглушка видит свою строку)", [f"увидено {saw}"])
    spent = [r for r in spend if r.get("event") != "demon.start"]
    order = [(r.get("event") or "расход", r.get("session_id")) for r in spend]
    paired = all(("demon.start", i) in order and ("расход", i) in order
                 and order.index(("demon.start", i)) < order.index(("расход", i)) for i in ids)
    report(len(spent) == 2 and [r.get("session_id") for r in spent] == ids and paired,
           "строка расхода несёт тот же session_id и стоит после своей demon.start", [f"порядок: {order}"])


# ───────────────────────── живые журналы не тронуты ─────────────────────────
def live_files() -> list:
    names = ["process-runs.jsonl", "process-load.jsonl", "demon-spend.jsonl", "tokens.cursor",
             "tokens-daily.json", "tokens-daily.summary", "tokens-daily.report", "вызовы-агентов.jsonl"]
    return [LIVE_LOGS / n for n in names] + sorted(LIVE_LOGS.glob("tokens-*.jsonl"))


def sizes() -> dict:
    return {p: p.stat().st_size for p in live_files() if p.exists()}


def live_untouched(before: dict) -> None:
    print("── живые журналы /var/log/harness не тронуты ──")
    leaked = []
    for path in live_files():
        if not path.exists():
            continue
        start = before.get(path, 0) if path.suffix == ".jsonl" else 0
        try:
            with open(path, "rb") as handle:
                handle.seek(start if start <= path.stat().st_size else 0)
                chunk = handle.read()
        except OSError:
            continue
        if RUN.encode() in chunk or str(TMP).encode() in chunk:
            leaked.append(path.name)
    daily = LIVE_LOGS / "tokens-daily.json"
    try:
        if json.loads(daily.read_text(encoding="utf-8")).get("дата") == COLLECTOR_DAY:
            leaked.append("tokens-daily.json (дата стенда)")
    except (OSError, ValueError):
        pass
    report(bool(before) and not leaked, "метка прогона пробы не попала в живые process-runs, process-load, "
                                        "demon-spend, tokens-*, вызовы-агентов", leaked or ["живых файлов не найдено"])


def main() -> int:
    before = sizes()
    for name, path in (("process-dispatcher.py", DISPATCHER), ("lib/process_load.py", PROCESS_LOAD)):
        if not path.exists():
            print(f"кода нет: {path} — случаи по нему красные")
    for runner in CASES:
        runner()
    live_untouched(before)
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"ДИСПЕТЧЕР ПРОЦЕССОВ В2: путей {paths}, неудач {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
