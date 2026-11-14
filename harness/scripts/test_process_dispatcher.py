#!/usr/bin/env python3
"""Пробы диспетчера процессов, часть В1 (hr.process-dispatcher, спека §В.1, §В.4–В.7).

Принцип §В.1: cgroup даёт точный итог, дерево процессов его только делит. Проба
судит разбор на СНИМКАХ: во временном каталоге строит поддельные /proc
(pid/stat, status, cmdline, environ, statm, cgroup; stat, uptime) и
/sys/fs/cgroup (cpu.stat usage_usec), свою библиотеку, пустые транскрипты и
журналы — и гоняет `process-dispatcher.py --замер` по снимкам 0 → 1 → 2,
читая строки process.sample из своего журнала. Каждый случай назван той
поломкой, на которой обязан покраснеть (номера — из раздела «Пробы и критерии
частей» спеки).

Двери кода (CLI): --proc, --cgroup, --библиотека, --проект, --транскрипты,
--журналы. Двери окружения: HARNESS_LOG_DIR / LOG_DIR (куда events.emit пишет
process-load.jsonl, где лежат process-runs.jsonl и locks/), PROCESS_LOAD_STATE
(файл состояния), PROCESS_LOAD_MAX_MB (порог ротации), HEARTBEAT_DIR,
STATE_DIR, HARNESS_EVENTS_STATE, HARNESS_CONF, HARNESS_INSTALL_CONF.

Двери пробы:
    HARNESS_PROCESS_DISPATCHER   путь к process-dispatcher.py (умолчание — рядом с пробой)
    HARNESS_PROCESS_LIBRARY_JSON библиотека-образец (из неё строится своя копия)
    HARNESS_RESOURCE_REPORT      путь к resource_report.py
    HARNESS_COMPARE_WITH_PACKAGE путь к compare-with-package.py

Прогон (только фоном):
    python3 scripts/test_process_dispatcher.py            # снимки, быстро
    python3 scripts/test_process_dispatcher.py --живой    # плюс больной случай §В.7 на живой машине
"""
import contextlib
import datetime as dt
import fcntl
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
TICK = 100          # USER_HZ ядра Linux: так считает /proc/<pid>/stat
PAGE = 4096
SECRET = "ZZSECRET" + uuid.uuid4().hex[:12]  # не-секрет: подставной маркер пробы


def config_dir() -> Path:
    # Раскладка проекта и пакета разные: конфиги там, где config/process-library.json.
    for folder in HERE.parents:
        if (folder / "config" / "process-library.json").is_file():
            return folder / "config"
    return HERE.parent / "harness" / "config"


def first_existing(*candidates: Path) -> Path:
    return next((c for c in candidates if c.exists()), candidates[0])


CONFIG = config_dir()
ROOT = CONFIG.parent.parent
DISPATCHER = Path(os.environ.get("HARNESS_PROCESS_DISPATCHER") or HERE / "process-dispatcher.py")
LIBRARY_JSON = Path(os.environ.get("HARNESS_PROCESS_LIBRARY_JSON") or CONFIG / "process-library.json")
RESOURCE_REPORT = Path(os.environ.get("HARNESS_RESOURCE_REPORT") or first_existing(
    HERE.parent / "harness" / "demons" / "resource_report.py", HERE.parent / "demons" / "resource_report.py"))
COMPARE = Path(os.environ.get("HARNESS_COMPARE_WITH_PACKAGE") or HERE / "compare-with-package.py")
WRAPPER = HERE / "run-background.sh"
LIVE = "--живой" in sys.argv

paths = failures = 0


def report(ok: bool, text: str, bad=()) -> None:
    global paths, failures
    paths += 1
    if ok:
        print("ок    · " + text)
        return
    failures += 1
    bad = [str(b).replace(SECRET, "<SECRET>") for b in bad]
    tail = f": {'; '.join(bad[:8])}" + (f" … ещё {len(bad) - 8}" if len(bad) > 8 else "") if bad else ""
    print("НЕ ок · " + text + tail)


def near(value, expected: float, tolerance: float) -> bool:
    return isinstance(value, (int, float)) and abs(value - expected) <= tolerance


# ───────────────────────── библиотека пробы ─────────────────────────
def probe_library(tmp: Path) -> dict:
    """Настоящий реестр (число записей — его, не вшитое) плюс две записи пробы.
    Приманка `burner-decoy` держит в `файлах` путь burner.sh из argv: код,
    который ищет id по командной строке, а не по argv обёртки, найдёт её."""
    library = json.loads(LIBRARY_JSON.read_text(encoding="utf-8"))
    library["процессы"] += [
        {"id": "probe-burner", "название": "Проба: жжёт CPU", "класс": "фоновый", "запуск": "крон",
         "статус": "проба", "файлы": ["probe/burner-main.sh"],
         "доля_нагрузки": {"приписка": {"способ": "обёртка", "признак": "run-background.sh probe-burner"},
                           "замер": "не измерено"}},
        {"id": "burner-decoy", "название": "Проба: приманка подбора по команде", "класс": "фоновый",
         "запуск": "крон", "статус": "проба", "файлы": [str(tmp / "burner.sh")],
         "доля_нагрузки": {"приписка": {"способ": "обёртка", "признак": f"{tmp}/burner.sh A"},
                           "замер": "не измерено"}},
    ]
    return library


def dumps_library(library: dict) -> str:
    return json.dumps(library, indent=2, ensure_ascii=False) + "\n"


# ───────────────────────── снимки /proc и cgroup ─────────────────────────
def proc(pid, ppid, sid, comm, argv, cg, cpu=(0.0, 0.0, 0.0, 0.0), start=1.0, rss_mb=4, env=None):
    return {"pid": pid, "ppid": ppid, "sid": sid, "comm": comm, "argv": argv, "cg": cg,
            "cpu": cpu, "start": start, "rss_mb": rss_mb, "env": env or {}}


def write_snapshot(base: Path, uptime: float, procs: list, cgroups: dict, machine_extra: float = 0.0,
                   cores: int = 6) -> tuple[Path, Path]:
    """Каталог снимка: <base>/proc и <base>/cgroup. cgroups — {полный путь: usage секунд};
    родительские cpu.stat — сумма детей, как в cgroup v2. Машина = Σ корзин + machine_extra."""
    proc_dir, cg_dir = base / "proc", base / "cgroup"
    proc_dir.mkdir(parents=True)
    cg_dir.mkdir(parents=True)
    for p in procs:
        d = proc_dir / str(p["pid"])
        d.mkdir()
        ut, st, cut, cst = (round(x * TICK) for x in p["cpu"])
        pages = round(p["rss_mb"] * 1048576 / PAGE)
        fields = [p["pid"], f"({p['comm']})", "S", p["ppid"], p["sid"], p["sid"], 0, -1, 4194304,
                  0, 0, 0, 0, ut, st, cut, cst, 20, 0, 1, 0, round(p["start"] * TICK),
                  pages * PAGE * 4, pages] + [0] * 28
        (d / "stat").write_text(" ".join(map(str, fields)) + "\n")
        (d / "status").write_text(f"Name:\t{p['comm']}\nState:\tS (sleeping)\nTgid:\t{p['pid']}\n"
                                  f"Pid:\t{p['pid']}\nPPid:\t{p['ppid']}\n"
                                  f"VmRSS:\t{pages * PAGE // 1024} kB\nThreads:\t1\n")
        (d / "statm").write_text(f"{pages * 4} {pages} {pages // 4} 1 0 {pages} 0\n")
        (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in p["argv"]))
        (d / "environ").write_bytes(b"".join(f"{k}={v}".encode() + b"\0" for k, v in p["env"].items()))
        (d / "cgroup").write_text(f"0::{p['cg']}\n")
    # Корзина каждого процесса снимка существует, как на живой машине.
    cgroups = {**{p["cg"]: 0.0 for p in procs}, **cgroups}
    sums: dict[str, float] = {}
    for path, usage in cgroups.items():
        parts = path.strip("/").split("/")
        for i in range(1, len(parts) + 1):
            key = "/".join(parts[:i])
            sums[key] = sums.get(key, 0.0) + usage
    for key, usage in sums.items():
        (cg_dir / key).mkdir(parents=True, exist_ok=True)
        usec = round(usage * 1e6)
        (cg_dir / key / "cpu.stat").write_text(
            f"usage_usec {usec}\nuser_usec {usec // 2}\nsystem_usec {usec - usec // 2}\n")
    top = sum(v for k, v in sums.items() if "/" not in k)
    busy = round((top + machine_extra) * TICK)
    idle = round(uptime * cores * TICK) - busy
    lines = [f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0"]
    lines += [f"cpu{i} {busy // cores} 0 0 {idle // cores} 0 0 0 0 0 0" for i in range(cores)]
    (proc_dir / "stat").write_text("\n".join(lines) + "\nintr 0\nctxt 0\nbtime 1700000000\n")
    (proc_dir / "uptime").write_text(f"{uptime:.2f} {uptime * cores * 0.9:.2f}\n")
    return proc_dir, cg_dir


class World:
    """Своё состояние, свои журналы, своя библиотека и проект — на каждый случай."""

    def __init__(self, name: str, library: dict | None = None):
        self.dir = Path(tempfile.mkdtemp(prefix=f"pd-{name}-", dir=TMP))
        self.logs = self.dir / "logs"
        self.state = self.dir / "state"
        self.project = self.dir / "project"
        self.transcripts = self.dir / "transcripts"
        # events.run создаёт runs/ без родителей: без .events/runs метка конца теряется при rc 0
        for d in (self.logs, self.logs / ".events" / "runs", self.state / "heartbeat",
                  self.project / "scripts", self.transcripts):
            d.mkdir(parents=True)
        self.library_path = self.dir / "process-library.json"
        self.library_path.write_text(dumps_library(library or probe_library(TMP)), encoding="utf-8")
        self.conf = self.dir / "harness.conf"
        self.conf.write_text(f"LOG_DIR={self.logs}\nSTATE_DIR={self.state}\nBACKGROUND_PROCESSES=on\n")
        (self.dir / "install.conf").write_text("")
        self.journal = self.logs / "process-load.jsonl"
        self.snaps = 0
        self.env = dict(os.environ, HARNESS_LOG_DIR=str(self.logs), LOG_DIR=str(self.logs),
                        STATE_DIR=str(self.state), HEARTBEAT_DIR=str(self.state / "heartbeat"),
                        HARNESS_EVENTS_STATE=str(self.logs / ".events"),
                        PROCESS_LOAD_STATE=str(self.logs / "process-load.state.json"),
                        HARNESS_CONF=str(self.conf), HARNESS_INSTALL_CONF=str(self.dir / "install.conf"),
                        HARNESS_TASK="-")
        # Слой установки машины пробе не хозяин: свой путь, файла по умолчанию нет.
        self.layer_path = self.dir / "process-library.local.json"
        self.env["HARNESS_PROCESS_LIBRARY_LOCAL"] = str(self.layer_path)
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)

    def doors(self, proc_dir, cg_dir) -> list:
        return ["--proc", str(proc_dir), "--cgroup", str(cg_dir), "--библиотека", str(self.library_path),
                "--проект", str(self.project), "--транскрипты", str(self.transcripts),
                "--журналы", str(self.logs)]

    def run(self, *args, timeout=120) -> tuple[int, str]:
        try:
            done = subprocess.run([sys.executable, str(DISPATCHER), *args], capture_output=True, text=True,
                                  env=self.env, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "таймаут"
        return done.returncode, (done.stdout + done.stderr).strip()

    def lines(self) -> list:
        if not self.journal.exists():
            return []
        rows = []
        for raw in self.journal.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(raw))
            except ValueError:
                rows.append({"битая": raw[:80]})
        return rows

    def sample(self, uptime, procs, cgroups, machine_extra=0.0, cores=6) -> tuple[int, str, list]:
        """Один проход --замер по новому снимку; новые строки process.sample."""
        self.snaps += 1
        proc_dir, cg_dir = write_snapshot(self.dir / f"snap{self.snaps}", uptime, procs, cgroups,
                                          machine_extra, cores)
        before = len(self.lines())
        rc, out = self.run("--замер", *self.doors(proc_dir, cg_dir))
        new = [r for r in self.lines()[before:] if r.get("event") == "process.sample"]
        return rc, out, new


def data_of(rows: list) -> dict:
    return (rows[-1].get("data") or {}) if rows else {}


def groups(rows: list, gid: str) -> list:
    return [g for g in (data_of(rows).get("группы") or []) if isinstance(g, dict) and g.get("id") == gid]


def cpu_of(rows: list, gid: str, run_id: str | None = None) -> float:
    return round(sum(float(g.get("cpu_с") or 0) for g in groups(rows, gid)
                     if run_id is None or str(g.get("прогон")) == run_id), 3)


def why(rc, out, rows) -> list:
    """Причина для красной строки: код прохода, хвост вывода, были ли строки."""
    tail = out.splitlines()[-1][:160] if out else ""
    return [f"проход rc={rc}" + (f" «{tail}»" if tail else ""), f"строк process.sample: {len(rows)}"]


def case(title: str):
    """Падение случая трассой — красная строка с его именем, а не смерть пробы."""
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


CASES: list = []
TMP = Path(tempfile.mkdtemp(prefix="test-process-dispatcher-"))
CRON = "/system.slice/cron.service"
AGENT = "/system.slice/harness-agent.service"
DISP = "/system.slice/harness-dispatcher.service"
INIT = proc(1, 0, 1, "systemd", ["/sbin/init"], "/init.scope")


def wrapper(pid, sid, letter="A", extra=()) -> dict:
    return proc(pid, 300, sid, "run-background.", ["/bin/bash", str(WRAPPER), "probe-burner", "--",
                                                   str(TMP / "burner.sh"), letter, *extra], CRON, rss_mb=4)


def burner(pid, ppid, sid, cpu=(0, 0, 0, 0), letter="A") -> dict:
    return proc(pid, ppid, sid, "burner.sh", ["/bin/bash", str(TMP / "burner.sh"), letter], CRON,
                cpu=cpu, rss_mb=100)


CRON_D = proc(300, 1, 300, "cron", ["/usr/sbin/cron", "-f"], CRON)


# ───────────────────────── случаи на снимках ─────────────────────────
@case("1. ярлык без «прогон» → два прогона слиты")
def case_two_runs():
    w = World("p1")
    base = [INIT, CRON_D, wrapper(1001, 1001, "A", ["--token=" + SECRET]), wrapper(2001, 2001, "B")]
    w.sample(1000.0, base + [burner(1002, 1001, 1001), burner(2002, 2001, 2001, letter="B")], {CRON: 10.0})
    rc, out, rows = w.sample(1060.0, base + [burner(1002, 1001, 1001, (2.5, 0.5, 0, 0)),
                                             burner(2002, 2001, 2001, (0.1, 0, 0, 0), "B")], {CRON: 13.1})
    runs = sorted(str(g.get("прогон")) for g in groups(rows, "probe-burner"))
    report(runs == ["1001", "2001"], "две строки probe-burner с прогон 1001 и 2001", [f"прогон: {runs}"] + why(rc, out, rows))
    a, b = cpu_of(rows, "probe-burner", "1001"), cpu_of(rows, "probe-burner", "2001")
    report(near(a, 3.0, 0.1) and b <= 0.3, "прогон A ≈ 3,0 с, прогон B ≤ 0,3 с", [f"A={a}, B={b}"])

    print("── форма строки журнала §В.4 ──")
    d = data_of(rows)
    report(bool(rows) and rows[-1].get("event") == "process.sample", "событие process.sample в process-load.jsonl",
           why(rc, out, rows))
    missing = [k for k in ("окно_с", "ядер", "машина_cpu_с", "группы", "остаток", "цена") if k not in d]
    report(bool(d) and not missing, "data: окно_с, ядер, машина_cpu_с, группы, остаток, цена", missing or ["нет data"])
    report(near(d.get("окно_с"), 60.0, 0.5), "окно_с = Δ /proc/uptime (60)", [f"окно_с={d.get('окно_с')}"])
    report(d.get("ядер") == 6, "ядер = число строк cpuN в /proc/stat снимка (6, не ядра машины пробы)",
           [f"ядер={d.get('ядер')}"])
    report(near(d.get("машина_cpu_с"), 3.1, 0.05), "машина_cpu_с = Δ user+nice+system+irq+softirq (3,1)",
           [f"машина_cpu_с={d.get('машина_cpu_с')}"])
    bad = [g for g in d.get("группы") or [] if not {"id", "cpu_с", "rss_мб", "процессов"} <= set(g)]
    report(bool(d.get("группы")) and not bad, "у каждой группы id, cpu_с, rss_мб, процессов", bad[:3])
    ga = [g for g in groups(rows, "probe-burner") if str(g.get("прогон")) == "1001"]
    report(bool(ga) and near(ga[0].get("rss_мб"), 104, 1.5) and ga[0].get("процессов") == 2,
           "группа прогона A: rss_мб = Σ statm (4+100), процессов 2", ga[:1] or ["группы нет"])
    price = d.get("цена") or {}
    report(all(isinstance(price.get(k), (int, float)) and price[k] >= 0 for k in ("cpu_мс", "мс")),
           "цена: cpu_мс и мс — числа (getrusage)", [price])
    text = w.journal.read_text(encoding="utf-8") if w.journal.exists() else ""
    report(bool(text) and SECRET not in text and "--token" not in text,
           "командные строки в журнал не пишутся: секрет из argv обёртки не попал",
           ["журнала нет"] if not text else ["секрет в журнале"])
    report((w.state / "heartbeat" / "process-dispatcher").exists(),
           "метка жизни process-dispatcher поставлена (events.run, heartbeat)", [f"нет в {w.state / 'heartbeat'}"])
    state = Path(w.env["PROCESS_LOAD_STATE"])
    try:
        state_ok = isinstance(json.loads(state.read_text(encoding="utf-8")), dict)
    except (OSError, ValueError):
        state_ok = False
    leftovers = [f.name for f in state.parent.iterdir() if f.name.startswith(state.name) and f != state]
    report(state_ok and not leftovers, "состояние — JSON-объект в PROCESS_LOAD_STATE, замена атомарная (без хвостов .tmp)",
           [f"{state.name}: {'есть' if state.exists() else 'нет'}", *leftovers])


@case("2. без правила sid → внук ушёл в «неразмечено»")
def case_sid():
    w = World("p2")
    base = [INIT, CRON_D, wrapper(1001, 1001), burner(1002, 1001, 1001)]
    w.sample(1000.0, base, {CRON: 5.0})
    # Сирота: родитель уже ушёл (ppid 1), прежде не виден — ни родителя, ни прилипания; остаётся sid корня.
    orphan = proc(1010, 1, 1001, "burnchild", ["/bin/sh", "-c", "burn"], CRON, cpu=(1.5, 0, 0, 0), start=1030.0)
    rc, out, rows = w.sample(1060.0, base + [orphan], {CRON: 6.5})
    a = cpu_of(rows, "probe-burner", "1001")
    report(near(a, 1.5, 0.1), "сирота с sid корня в прогоне 1001 (1,5 с)", [f"probe-burner={a}"] + why(rc, out, rows))


@case("3. без вычета учтённых детей → двойной счёт")
def case_double():
    w = World("p3")
    # Сосед того же юнита жжёт 3 с на проходе 2: дельта cgroup не нулевая, и ужатие не может
    # обнулить удвоенные 2 с — без соседа двойной счёт прятался бы ужатием (ревью В1, находка 3).
    base = [INIT, CRON_D, wrapper(1001, 1001), wrapper(2001, 2001, "B")]
    w.sample(1000.0, base + [burner(1002, 1001, 1001), burner(2002, 2001, 2001, letter="B")], {CRON: 5.0})
    child = proc(1003, 1002, 1001, "python3", ["python3", "-c", "burn"], CRON, cpu=(2.0, 0, 0, 0), start=1040.0)
    rc1, out1, rows1 = w.sample(1060.0, base + [burner(1002, 1001, 1001), child,
                                                burner(2002, 2001, 2001, letter="B")], {CRON: 7.0})
    # К проходу 2 ребёнка дождал родитель: его 2 с теперь в cutime родителя.
    rc2, out2, rows2 = w.sample(1120.0, base + [burner(1002, 1001, 1001, (0, 0, 2.0, 0)),
                                                burner(2002, 2001, 2001, (3.0, 0, 0, 0), "B")], {CRON: 10.0})
    first, second = cpu_of(rows1, "probe-burner", "1001"), cpu_of(rows2, "probe-burner", "1001")
    neighbour = cpu_of(rows2, "probe-burner", "2001")
    report(bool(rows1) and bool(rows2) and near(first + second, 2.0, 0.2),
           "итог двух проходов = 2 ± 0,2 с, а не 4 (и не 3,2 после ужатия при соседе 3 с)",
           [f"проход 1: {first}, проход 2: {second}"] + why(rc2, out2, rows2))
    report(near(neighbour, 3.0, 0.1), "сосед (прогон 2001) на проходе 2 = 3,0 с, не ужат чужим двойным счётом",
           [f"сосед={neighbour}"])


@case("3в. хвост ушедшего ребёнка взят из собственного CPU родителя → недосчёт")
def case_tail():
    w = World("p3c")
    base = [INIT, CRON_D, wrapper(1001, 1001)]
    w.sample(1000.0, base + [burner(1002, 1001, 1001)], {CRON: 5.0})
    child = proc(1003, 1002, 1001, "python3", ["python3", "-c", "burn"], CRON, cpu=(2.0, 0, 0, 0), start=1040.0)
    w.sample(1060.0, base + [burner(1002, 1001, 1001), child], {CRON: 7.0})
    # Проход 3: родитель сам 1,0 с; ребёнок дожёг 0,5 с после прохода 2 и дождан — cutime 2,5.
    # Хвост 0,5 — сверх собственных 1,0 родителя: итог 1,5 = дельта cgroup, остаток 0.
    rc, out, rows = w.sample(1120.0, base + [burner(1002, 1001, 1001, (1.0, 0, 2.5, 0))], {CRON: 8.5})
    total = cpu_of(rows, "probe-burner", "1001")
    rest = (data_of(rows).get("остаток") or {}).get("cron.service")
    report(near(total, 1.5, 0.05) and near(rest, 0.0, 0.05),
           "проход 3: probe-burner = свои 1,0 + хвост ребёнка 0,5 = 1,5 с, остаток cron.service 0",
           [f"probe-burner={total}, остаток={rest}"] + why(rc, out, rows))


@case("3б. ужатие до дельты cgroup не видно в строке → двойной счёт спрятан")
def case_cap_visible():
    w = World("p3b")
    base = [INIT, CRON_D, wrapper(1001, 1001)]
    w.sample(1000.0, base + [burner(1002, 1001, 1001)], {CRON: 5.0})
    # 2 с в cutime — ребёнок, ушедший в чужой cgroup (systemd-run): в счётчик юнита он не входит,
    # Σ процессов 3,0 > дельты cgroup 1,0 — код обязан ужать и назвать ужатое (2,0).
    rc, out, rows = w.sample(1060.0, base + [burner(1002, 1001, 1001, (1.0, 0, 2.0, 0))], {CRON: 6.0})
    d = data_of(rows)
    squeezed = d.get("ужатие") if isinstance(d.get("ужатие"), dict) else {}
    report(near(squeezed.get("cron.service"), 2.0, 0.05),
           "data.ужатие[\"cron.service\"] = Σ процессов 3,0 − дельта cgroup 1,0 = 2,0 с",
           [f"ужатие={d.get('ужатие')!r}"] + why(rc, out, rows))
    report(near(cpu_of(rows, "probe-burner", "1001"), 1.0, 0.05), "после ужатия probe-burner = дельта cgroup 1,0 с",
           [f"probe-burner={cpu_of(rows, 'probe-burner', '1001')}"])
    rc, out, rows = w.sample(1120.0, base + [burner(1002, 1001, 1001, (2.0, 0, 2.0, 0))], {CRON: 7.0})
    d = data_of(rows)
    squeezed = d.get("ужатие") if isinstance(d.get("ужатие"), dict) else {}
    report(bool(rows) and not squeezed.get("cron.service"),
           "проход без превышения (Σ 1,0 = дельта 1,0): ужатия по cron.service нет или 0",
           [f"ужатие={d.get('ужатие')!r}"] + why(rc, out, rows))


@case("4. без cutime/cstime → недосчёт A")
def case_cutime():
    w = World("p4")
    base = [INIT, CRON_D, wrapper(1001, 1001)]
    w.sample(1000.0, base + [burner(1002, 1001, 1001, (1.0, 0, 0, 0))], {CRON: 5.0})
    # Короткий ребёнок родился и дождан между проходами: виден только в cutime/cstime.
    rc, out, rows = w.sample(1060.0, base + [burner(1002, 1001, 1001, (1.6, 0.4, 1.0, 0.5))], {CRON: 7.5})
    a = cpu_of(rows, "probe-burner", "1001")
    report(near(a, 2.5, 0.1), "Δ(utime+stime) 1,0 + Δ(cutime+cstime) 1,5 = 2,5 с", [f"probe-burner={a}"] + why(rc, out, rows))


@case("5. остаток не пишется")
def case_rest():
    w = World("p5")
    base = [INIT, CRON_D, wrapper(1001, 1001)]
    w.sample(1000.0, base + [burner(1002, 1001, 1001)], {CRON: 5.0}, machine_extra=10.0)
    rc, out, rows = w.sample(1060.0, base + [burner(1002, 1001, 1001, (1.0, 0, 0, 0))], {CRON: 8.0},
                             machine_extra=11.0)
    rest = data_of(rows).get("остаток") or {}
    report(near(rest.get("cron.service"), 2.0, 0.1), "остаток cron.service = Δcgroup 3,0 − разделённое 1,0",
           [f"остаток={rest}"] + why(rc, out, rows))
    report(near(rest.get("вне_корзин"), 1.0, 0.1), "вне_корзин = машина − Σ корзин верхнего уровня (1,0)",
           [f"остаток={rest}"])


@case("6. id взят не из argv обёртки → probe-burner не найден")
def case_argv_id():
    w = World("p6")
    base = [INIT, CRON_D, wrapper(1001, 1001)]
    w.sample(1000.0, base + [burner(1002, 1001, 1001)], {CRON: 5.0})
    rc, out, rows = w.sample(1060.0, base + [burner(1002, 1001, 1001, (1.0, 0, 0, 0))], {CRON: 6.0})
    report(bool(groups(rows, "probe-burner")) and not groups(rows, "burner-decoy"),
           "id probe-burner из argv run-background.sh; приманка burner-decoy (путь burner.sh в «файлах») пуста",
           [f"группы: {sorted({g.get('id') for g in data_of(rows).get('группы') or []})}"] + why(rc, out, rows))


@case("7. юнит по хвосту cgroup → claude LXC в claude-session; запись чужого контейнера — из слоя установки")
def case_lxc():
    w = World("p7")
    w.layer_path.write_text(dumps_library({"процессы": [
        {"id": "probe-lxc", "название": "Проба: чужой контейнер", "класс": "сторонний", "запуск": "контейнер",
         "статус": "работает", "файлы": ["/srv/probe-lxc"],
         "доля_нагрузки": {"приписка": {"способ": "cgroup", "признак": "lxc.payload.probelxc"},
                           "замер": "не измерено"}}]}), encoding="utf-8")
    lxc_cg = "/lxc.payload.probelxc/system.slice/harness-agent.service"
    session_env = {"CLAUDE_CODE_SESSION_ID": "sess-host"}

    def world(cpu_host, cpu_lxc):
        return [INIT, proc(700, 650, 650, "claude", ["claude", "--dangerously-skip-permissions"], AGENT,
                           cpu=(cpu_host, 0, 0, 0), rss_mb=300, env=session_env),
                proc(800, 790, 790, "claude", ["claude", "--dangerously-skip-permissions"], lxc_cg,
                     cpu=(cpu_lxc, 0, 0, 0), rss_mb=200)]
    w.sample(1000.0, world(10.0, 20.0), {AGENT: 10.0, lxc_cg: 20.0})
    rc, out, rows = w.sample(1060.0, world(11.0, 22.0), {AGENT: 11.0, lxc_cg: 22.0})
    lxc, host = cpu_of(rows, "probe-lxc"), cpu_of(rows, "claude-session")
    report(near(lxc, 2.0, 0.1) and near(host, 1.0, 0.1),
           "корзина по ПОЛНОМУ пути: probe-lxc 2,0 с, claude-session 1,0 с",
           [f"probe-lxc={lxc}, claude-session={host}"] + why(rc, out, rows))


@case("8. нет ротации")
def case_rotation():
    old = json.dumps({"v": 1, "ts": "2026-10-01T00:00:00+05:00", "event": "OLDLINE", "data": {"x": "y" * 900}})
    for name, size_mb, rotated in (("малые файлы", 0.1, False), ("файлы выше порога", 1.2, True)):
        w = World("p8")
        w.env["PROCESS_LOAD_MAX_MB"] = "1"
        filler = (old + "\n") * int(size_mb * 1048576 / (len(old) + 1) + 1)
        for journal in ("process-load", "process-runs"):
            (w.logs / f"{journal}.jsonl").write_text(filler, encoding="utf-8")
        base = [INIT, CRON_D, wrapper(1001, 1001), burner(1002, 1001, 1001)]
        w.sample(1000.0, base, {CRON: 5.0})
        rc, out, rows = w.sample(1060.0, base, {CRON: 5.5})
        for journal in ("process-load", "process-runs"):
            live, archive = w.logs / f"{journal}.jsonl", w.logs / f"{journal}.jsonl.1"
            if not rotated:
                report(bool(rows) and not archive.exists(), f"{name}: {journal} ниже PROCESS_LOAD_MAX_MB не ротирован",
                       [f"есть {archive.name}"])
                continue
            archived = archive.read_text(encoding="utf-8", errors="replace") if archive.exists() else ""
            size = live.stat().st_size if live.exists() else 0
            report(archived.startswith(old) and size < 1048576 and "OLDLINE" not in (
                live.read_text(encoding="utf-8", errors="replace") if live.exists() else ""),
                   f"{name}: {journal} → .1 по PROCESS_LOAD_MAX_MB=1, живой файл свежий",
                   [f".1 {'есть' if archive.exists() else 'нет'}, живой {size} Б"] + why(rc, out, rows))
        both = (w.logs / "process-load.jsonl", w.logs / "process-load.jsonl.1")
        fresh = [r for p in both if p.exists() for r in p.read_text(errors="replace").splitlines()
                 if '"process.sample"' in r]
        report(len(fresh) >= 1, f"{name}: новая строка process.sample не потеряна при ротации", [f"строк: {len(fresh)}"])


def sample_row(ts: dt.datetime, groups_: list, rest: dict, cores: int, window: float = 60.0) -> str:
    return json.dumps({"v": 1, "ts": ts.isoformat(timespec="seconds"), "src": "scripts/process-dispatcher.py",
                       "event": "process.sample", "outcome": "ok", "task": "-", "task_from": "arg",
                       "session": None, "data": {"окно_с": window, "ядер": cores,
                                                 "машина_cpu_с": sum(g["cpu_с"] for g in groups_),
                                                 "группы": groups_, "остаток": rest,
                                                 "цена": {"cpu_мс": 50, "мс": 60}}}, ensure_ascii=False)


def hour_of_samples(groups_: list, rest: dict, cores: int, count: int = 60) -> list:
    now = dt.datetime.now().astimezone()
    return [sample_row(now - dt.timedelta(minutes=count - i), groups_, rest, cores) for i in range(count)]


@case("9. замер без даты/источника")
def case_library():
    w = World("p9")
    before_text = w.library_path.read_text(encoding="utf-8")
    w.journal.write_text("\n".join(hour_of_samples(
        [{"id": "probe-burner", "прогон": "1001", "cpu_с": 30.0, "rss_мб": 100, "процессов": 2}],
        {"cron.service": 1.0, "вне_корзин": 0.5}, 6)) + "\n", encoding="utf-8")
    rc, out = w.run("--в-библиотеку", "--библиотека", str(w.library_path), "--журналы", str(w.logs),
                    "--проект", str(w.project))
    after_text = w.library_path.read_text(encoding="utf-8")
    try:
        before, after = json.loads(before_text), json.loads(after_text)
    except ValueError as error:
        report(False, "--в-библиотеку оставил читаемый JSON", [error])
        return

    def stripped(lib):
        copy = json.loads(json.dumps(lib))
        for r in copy["процессы"]:
            r.get("доля_нагрузки", {}).pop("замер", None)
        return copy
    report(rc == 0 and stripped(before) == stripped(after),
           "--в-библиотеку меняет только доля_нагрузки.замер (прочее и число записей прежние)",
           [f"rc={rc} «{out[-160:]}»", f"записей {len(before['процессы'])} → {len(after['процессы'])}"])
    by_id = {r["id"]: r for r in after["процессы"]}
    measured = by_id["probe-burner"]["доля_нагрузки"]["замер"]
    today = dt.date.today()
    date_ok = isinstance(measured, dict) and any(
        f in str(measured.get("дата")) for f in (today.isoformat(), today.strftime("%d.%m.%Y")))
    report(date_ok and bool(str(measured.get("источник") or "").strip()),
           "замер probe-burner: «дата» сегодняшняя и «источник» непустой", [repr(measured)[:200]])
    week = measured.get("неделя") if isinstance(measured, dict) else None
    server = (week or {}).get("сервер") or {}
    report(isinstance(week, dict) and near(week.get("покрытие_ч"), 1.0, 0.1)
           and near(server.get("ядро_часов"), 0.5, 0.02),
           "замер.неделя: покрытие_ч ≈ 1, сервер.ядро_часов = 60×30 с / 3600 = 0,5", [repr(week)[:200]])
    untouched = [r["id"] for r in before["процессы"] if r["id"] != "probe-burner"
                 and r["доля_нагрузки"]["замер"] != by_id[r["id"]]["доля_нагрузки"]["замер"]]
    # Без удачного первого прохода эти строки зелены даром: судим их только при rc 0.
    report(rc == 0 and not untouched, "записи без данных в журнале сохранили прежний замер", untouched or [f"rc={rc}"])
    report(rc == 0 and after_text == dumps_library(after), "файл = json.dumps(indent=2, ensure_ascii=False) + «\\n»",
           [f"rc={rc}" if rc else "оформление разошлось"])

    w2 = World("p9b")
    foreign = json.dumps(json.loads(w2.library_path.read_text(encoding="utf-8")), indent=1, ensure_ascii=False) + "\n"
    w2.library_path.write_text(foreign, encoding="utf-8")
    shutil.copy(w.journal, w2.journal)
    rc2, out2 = w2.run("--в-библиотеку", "--библиотека", str(w2.library_path), "--журналы", str(w2.logs),
                       "--проект", str(w2.project))
    report(rc == 0 and rc2 != 0 and bool(out2) and w2.library_path.read_text(encoding="utf-8") == foreign,
           "чужое оформление (indent=1) → отказ с причиной, файл не тронут", [f"rc={rc2} «{out2[-160:]}»"])


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@case("10. отчёт без строки «по процессам»")
def case_report():
    sys.path.insert(0, str(HERE / "lib"))
    rr = load_module("resource_report_probe", RESOURCE_REPORT)
    try:
        import process_load  # noqa: PLC0415
    except ImportError as error:
        report(False, "scripts/lib/process_load.py импортируется (summarize для отчёта)", [error])
        return
    now = time.time()
    cfg = dict(rr.DEFAULTS)
    res_line = lambda age: json.dumps({"ts": now - age * 3600, "items": [  # noqa: E731
        {"name": "claude", "cpu": 70.0, "mem_mb": 900.0}]})
    daily = rr.aggregate([res_line(24 - i) for i in range(25)], now, 24)
    lines = hour_of_samples([{"id": "checks-stream", "прогон": "2302843", "cpu_с": 48.0, "rss_мб": 400, "процессов": 12},
                             {"id": "claude-session", "прогон": "harness-agent.service", "cpu_с": 36.0,
                              "rss_мб": 900, "процессов": 3}], {"cron.service": 4.8, "вне_корзин": 0.0}, 4)
    msg = rr.build_message([], [], [], 10, cfg, daily, processes=process_load.summarize(lines, now, 24))
    line = next((s for s in msg.splitlines() if s.startswith("по процессам — ")), "")
    report(bool(re.search(r"checks-stream 0,8( ядра)? \(20\s?%\)", line)) and "не разобрано" in line,
           "строка «по процессам — checks-stream 0,8 ядра (20 %) … не разобрано N %»", [msg.replace("\n", " ⏎ ")[:300]])
    low = line.lower()
    report(bool(line) and ("claude" not in low or low.find("checks-stream") < low.find("claude")),
           "порядок по убыванию CPU: checks-stream раньше сессии", [line])
    report(bool(line) and msg.find("память, пик") < msg.find("по процессам"),
           "строка стоит после «память, пик»", [msg.replace("\n", " ⏎ ")[:300]])
    empty = rr.build_message([], [], [], 10, cfg, daily, processes=process_load.summarize([], now, 24))
    report("по процессам — данных пока нет" in empty, "журнала нет → «по процессам — данных пока нет»",
           [empty.replace("\n", " ⏎ ")[:300]])
    many_groups = [{"id": f"process-with-a-long-name-{k:02d}", "прогон": str(k), "cpu_с": 50.0 - k,
                    "rss_мб": 100, "процессов": 1} for k in range(20)]
    many = [{"name": "n" * 40 + str(k), "cpu": 300.0 - k, "mem_mb": 9000.0 - k} for k in range(9)]
    big_daily = rr.aggregate([json.dumps({"ts": now - h * 3600, "items": many}) for h in (0, 23)], now, 24)
    big = rr.build_message(many, many, [{"path": "/p" * 20, "mb": 5000}], 99, cfg, big_daily,
                           processes=process_load.summarize(hour_of_samples(many_groups, {}, 4), now, 24))
    report(len(big) <= rr.MAX_CHARS and "по процессам" in big,
           f"длинный случай урезан тем же циклом: ≤ MAX_CHARS ({rr.MAX_CHARS}), строка цела", [f"длина {len(big)}"])


@case("11. compare-with-package без вычистки замера / вычищен весь файл")
def case_compare():
    cmp_ = load_module("compare_probe", COMPARE)
    library = json.loads(LIBRARY_JSON.read_text(encoding="utf-8"))
    # Живая библиотека проекта несёт замер из resource-report.sh; пакет — всегда «не измерено».
    for record in library["процессы"]:
        if isinstance(record.get("доля_нагрузки"), dict):
            record["доля_нагрузки"]["замер"] = "не измерено"
    package_text = dumps_library(library)
    today = dt.date.today().strftime("%d.%m")

    def git(where, *args):
        subprocess.run(["git", "-C", str(where), "-c", "user.name=p", "-c", "user.email=p@p", *args],
                       capture_output=True, check=True)

    def stand(name: str, project_text: str):
        project, package = TMP / f"cmp-{name}-проект", TMP / f"cmp-{name}-пакет"
        for root, text in ((project, project_text), (package, package_text)):
            (root / "harness" / "config").mkdir(parents=True)
            git(root, "init", "-q")
            (root / "harness" / "config" / "process-library.json").write_text(text, encoding="utf-8")
        journal = project / cmp_.ЖУРНАЛ_В_ПРОЕКТЕ
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(f"# журнал\n\n| {today} | правка | доказано здесь |\n", encoding="utf-8")
        for root in (project, package):
            git(root, "add", "-A")
            git(root, "commit", "-qm", "начальный")
        return project, package

    def judge(project_text: str, name: str):
        project, package = stand(name, project_text)
        rc, lines = cmp_.проверить(["harness/config/process-library.json"], package, project)
        return rc, "\n".join(lines)

    measured = json.loads(package_text)
    measured["процессы"][0]["доля_нагрузки"]["замер"] = {"дата": "2026-11-06", "источник": "process-load.jsonl",
                                                         "неделя": {"покрытие_ч": 40}}
    measured["процессы"][1]["доля_нагрузки"]["замер"] = {"дата": "2026-11-06", "источник": "x", "неделя": {}}
    rc, text = judge(dumps_library(measured), "числа")
    report(rc == 0 and "расходится" not in text, "разные числа в «замер» — сверка зелёная", [f"rc={rc}", text[-200:]])

    other = json.loads(package_text)
    other["процессы"][0]["лишнее_поле"] = 1
    rc, text = judge(dumps_library(other), "структура")
    report(rc == 1 and "расходится" in text, "разная структура (лишнее поле записи) — сверка красная", [f"rc={rc}", text[-200:]])

    other = json.loads(package_text)
    other["процессы"][0]["доля_нагрузки"]["приписка"]["признак"] += " иначе"
    rc, text = judge(dumps_library(other), "приписка")
    report(rc == 1 and "расходится" in text, "разная «приписка» рядом с замером — сверка красная", [f"rc={rc}", text[-200:]])

    rc, text = judge(package_text[:-3], "битый")
    report(rc == 1 and "расходится" in text, "битый JSON сравнивается байтами — красная", [f"rc={rc}", text[-200:]])

    src = TMP / "cmp-measured.json"
    src.write_text(dumps_library(measured), encoding="utf-8")
    done = subprocess.run([sys.executable, str(COMPARE), "--вычищенный", str(src)], capture_output=True, text=True)
    report(done.returncode == 0 and done.stdout == package_text,
           "--вычищенный <файл> печатает библиотеку с «замер» = «не измерено» байт в байт",
           [f"rc={done.returncode} «{done.stderr.strip()[-160:]}»"])


@case("20. ребёнок диспетчера voice-worker.sh в строке harness-dispatcher")
def case_voice_worker():
    w = World("p20")

    def world(vw, ff, curl):
        return [INIT, proc(500, 1, 500, "tg-dispatcher.s", ["/bin/bash", str(w.project / "scripts/tg-dispatcher.sh")],
                           DISP, rss_mb=5),
                proc(510, 500, 510, "voice-worker.sh", ["/bin/bash", str(w.project / "scripts/voice-worker.sh"),
                                                        "/tmp/voice.ogg"], DISP, cpu=(vw, 0, 0, 0), start=900.0),
                proc(511, 510, 510, "ffmpeg", ["ffmpeg", "-i", "/tmp/voice.ogg"], DISP, cpu=(ff, 0, 0, 0), start=900.0),
                proc(520, 500, 500, "curl", ["curl", "-s", f"https://api.telegram.org/bot{SECRET}/getUpdates"], DISP,
                     cpu=(curl, 0, 0, 0), start=900.0)]
    w.sample(1000.0, world(1.0, 1.0, 1.0), {DISP: 3.0})
    rc, out, rows = w.sample(1060.0, world(2.2, 1.5, 1.3), {DISP: 5.0})
    vw, hd = cpu_of(rows, "voice-worker"), cpu_of(rows, "harness-dispatcher")
    report(near(vw, 1.7, 0.1) and near(hd, 0.3, 0.1),
           "ребёнок tg-dispatcher.sh с sid=pid → voice-worker (с внуком 1,7 с); прочее — harness-dispatcher (0,3 с)",
           [f"voice-worker={vw}, harness-dispatcher={hd}"] + why(rc, out, rows))
    text = w.journal.read_text(encoding="utf-8") if w.journal.exists() else ""
    report(bool(text) and SECRET not in text, "секрет из argv curl в журнал не попал", ["журнала нет"] if not text else ["секрет в журнале"])


@case("21. хук (ребёнок claude без подписи shell-snapshots) принят за корень команды")
def case_hook():
    w = World("p21")
    env = {"CLAUDE_CODE_SESSION_ID": "sess-main"}
    snapshot_cmd = ("source /root/.claude/shell-snapshots/snapshot-bash-1.sh && eval 'make test' "
                    "< /dev/null && pwd -P >| /tmp/claude-1-cwd")

    def world(claude, hook, cmd, make, with_children):
        procs = [INIT, proc(700, 650, 650, "claude", ["claude", "--dangerously-skip-permissions"], AGENT,
                            cpu=(claude, 0, 0, 0), rss_mb=300, env=env)]
        if with_children:
            procs += [proc(710, 700, 710, "python3", ["python3", str(w.project / "scripts/hooks/guard_bash.py")], AGENT,
                           cpu=(hook, 0, 0, 0), start=1030.0, env=env),
                      proc(720, 700, 720, "bash", ["/bin/bash", "-c", snapshot_cmd], AGENT, cpu=(cmd, 0, 0, 0),
                           start=1030.0, env=env),
                      proc(721, 720, 720, "make", ["make", "test"], AGENT, cpu=(make, 0, 0, 0), start=1031.0, env=env)]
        return procs
    w.sample(1000.0, world(10.0, 0, 0, 0, False), {AGENT: 10.0})
    rc, out, rows = w.sample(1060.0, world(11.0, 0.4, 0.8, 0.6, True), {AGENT: 12.8})
    session = cpu_of(rows, "claude-session")
    helpers = round(sum(float(g.get("cpu_с") or 0) for g in data_of(rows).get("группы") or []
                        if str(g.get("id", "")).startswith("helpers")), 3)
    report(near(session, 1.4, 0.1), "хук (sid=pid, без подписи) — в строке claude-session: 1,0 + 0,4",
           [f"claude-session={session}, helpers={helpers}"] + why(rc, out, rows))
    report(near(helpers, 1.4, 0.1), "корень команды с подписью shell-snapshots без транскрипта → helpers (0,8 + 0,6)",
           [f"helpers={helpers}"])


@case("§В.1 п.1. неделимая корзина не по дельте cpu.stat / счётчик уменьшился → дельта отрицательна")
def case_plain_cgroup():
    w = World("cg")
    panel = "/system.slice/harness-panel.service"

    def world():
        return [INIT, proc(600, 1, 600, "python3", ["python3", str(w.project / "harness/panel/server.py")], panel,
                           cpu=(1.0, 0, 0, 0), rss_mb=40)]
    w.sample(1000.0, world(), {panel: 50.0})
    rc, out, rows = w.sample(1060.0, world(), {panel: 53.0})
    first = cpu_of(rows, "harness-panel")
    report(near(first, 3.0, 0.05), "harness-panel (способ cgroup) = Δ usage_usec 3,0 с, а не Δ дерева 0",
           [f"harness-panel={first}"] + why(rc, out, rows))
    # Служба перезапущена: корзина новая, счётчик с нуля — текущее значение и есть дельта.
    rc, out, rows = w.sample(1120.0, world(), {panel: 2.0})
    second = cpu_of(rows, "harness-panel")
    report(near(second, 2.0, 0.05), "счётчик уменьшился (53 → 2) → дельта от нуля 2,0 с",
           [f"harness-panel={second}"] + why(rc, out, rows))


@case("§В.1 п.2. корень крона без обёртки не назван «крон:<имя файла>»")
def case_bare_cron():
    w = World("bare")

    def world(cpu):
        return [INIT, CRON_D,
                proc(4001, 300, 4001, "sh", ["/bin/sh", "-c", f"/opt/legacy/old-job.sh --key={SECRET}"], CRON),
                proc(4002, 4001, 4001, "old-job.sh", ["/bin/bash", "/opt/legacy/old-job.sh", f"--key={SECRET}"],
                     CRON, cpu=(cpu, 0, 0, 0))]
    w.sample(1000.0, world(0.0), {CRON: 1.0})
    rc, out, rows = w.sample(1060.0, world(1.2), {CRON: 2.2})
    named = [g for g in data_of(rows).get("группы") or [] if str(g.get("id", "")).startswith("крон:")]
    report(len(named) == 1 and "old-job.sh" in str(named[0].get("id")) and near(named[0].get("cpu_с"), 1.2, 0.05),
           "одна группа «крон:old-job.sh» с 1,2 с (вне библиотеки, не «неразмечено»)",
           [f"группы: {sorted(str(g.get('id')) for g in data_of(rows).get('группы') or [])}"] + why(rc, out, rows))
    text = w.journal.read_text(encoding="utf-8") if w.journal.exists() else ""
    report(bool(text) and SECRET not in text, "довод из argv корня крона в журнал не попал",
           ["журнала нет"] if not text else ["секрет в журнале"])


def cron_child(pid) -> dict:
    """Ребёнок демона cron, как на живой машине: argv /usr/sbin/CRON, comm cron, sid демона."""
    return proc(pid, 300, 300, "cron", ["/usr/sbin/CRON", "-f", "-P"], CRON)


def crontab_names(rows: list) -> list:
    return sorted(str(g.get("id")) for g in data_of(rows).get("группы") or [] if str(g.get("id", "")).startswith("крон:"))


@case("§В.7 A. корень крона без проверки родителя-cron → внук `setsid nohup` ушёл в «крон:…»")
def case_setsid_grandchild():
    w = World("setsid")
    task = [str(TMP / "burner.sh"), "A"]
    base = [INIT, CRON_D, cron_child(301),
            proc(1000, 301, 1000, "sh", ["/bin/sh", "-c", f"{WRAPPER} probe-burner -- {' '.join(task)}"], CRON),
            proc(1001, 1000, 1000, "run-background.", ["/bin/bash", str(WRAPPER), "probe-burner", "--", *task], CRON)]
    burn_code = "import sys, time\nend = time.process_time() + float(sys.argv[1])"

    def grand(ppid, cpu):
        # `setsid nohup python3 -c …` из burner.sh: своя сессия (sid = pid), родитель не cron.
        return proc(1003, ppid, 1003, "python3", ["python3", "-c", burn_code, "1.0"], CRON, cpu=(cpu, 0, 0, 0),
                    start=990.0, rss_mb=10)
    w.sample(1000.0, base + [burner(1002, 1001, 1000), grand(1002, 0.0)], {CRON: 5.0})
    rc, out, rows = w.sample(1060.0, base + [burner(1002, 1001, 1000, (1.0, 0, 0, 0)), grand(1002, 1.5)], {CRON: 7.5})
    a = cpu_of(rows, "probe-burner", "1001")
    report(near(a, 2.5, 0.1) and not crontab_names(rows),
           "родитель жив: внук sid = pid в строке probe-burner прогона 1001 (1,0 + 1,5), групп «крон:» нет",
           [f"probe-burner={a}", f"крон: {crontab_names(rows)}"] + why(rc, out, rows))
    # Родитель ушёл (обёртка дождала его 1,0 с в cutime), внук осиротел: ppid 1, держится прилипанием.
    rc, out, rows = w.sample(1120.0, base[:-1] + [
        proc(1001, 1000, 1000, "run-background.", ["/bin/bash", str(WRAPPER), "probe-burner", "--", *task], CRON,
             cpu=(0, 0, 1.0, 0)), grand(1, 2.5)], {CRON: 8.5})
    a = cpu_of(rows, "probe-burner", "1001")
    report(near(a, 1.0, 0.1) and not crontab_names(rows),
           "родитель ушёл: сирота (ppid 1, sid = pid) прилип к probe-burner 1001 (1,0 с), групп «крон:» нет",
           [f"probe-burner={a}", f"крон: {crontab_names(rows)}"] + why(rc, out, rows))

    w2 = World("notcron")
    w2.sample(1000.0, [INIT, CRON_D], {CRON: 1.0})
    daemon = proc(1500, 1, 1500, "tmux: server", ["tmux", "-S", "/tmp/probe.sock", "new-session", "-d"], CRON,
                  cpu=(0.7, 0, 0, 0), start=1030.0)
    rc, out, rows = w2.sample(1060.0, [INIT, CRON_D, daemon], {CRON: 1.7})
    lost = cpu_of(rows, "cron.service: неразмечено")
    report(not crontab_names(rows) and near(lost, 0.7, 0.05),
           "sid = pid в cron.service, родитель НЕ cron (ppid 1, новый) → не корень крона: «cron.service: неразмечено» 0,7 с",
           [f"крон: {crontab_names(rows)}", f"неразмечено={lost}"] + why(rc, out, rows))


@case("§В.1 п.2. корень крона с env-префиксом → секрет в имени группы")
def case_cron_env_prefix():
    w = World("envpfx")
    env = {"API_TOKEN": SECRET}

    def world(x, y):
        return [INIT, CRON_D, cron_child(301), cron_child(302),
                proc(4001, 301, 4001, "sh", ["/bin/sh", "-c", f"API_TOKEN={SECRET} /opt/legacy/x.sh --once"], CRON),
                proc(4002, 4001, 4001, "x.sh", ["/bin/bash", "/opt/legacy/x.sh", "--once"], CRON,
                     cpu=(x, 0, 0, 0), env=env),
                proc(5001, 302, 5001, "sh", ["/bin/sh", "-c", f"env API_TOKEN={SECRET} LANG=C /opt/legacy/y.sh"], CRON),
                proc(5002, 5001, 5001, "y.sh", ["/bin/bash", "/opt/legacy/y.sh"], CRON, cpu=(y, 0, 0, 0), env=env)]
    w.sample(1000.0, world(0.0, 0.0), {CRON: 1.0})
    rc, out, rows = w.sample(1060.0, world(1.2, 0.8), {CRON: 3.0})
    x, y = cpu_of(rows, "крон:x.sh"), cpu_of(rows, "крон:y.sh")
    report(crontab_names(rows) == ["крон:x.sh", "крон:y.sh"] and near(x, 1.2, 0.05) and near(y, 0.8, 0.05),
           "`sh -c 'ИМЯ=… /path/x.sh'` → «крон:x.sh» 1,2 с; `sh -c 'env ИМЯ=… /path/y.sh'` → «крон:y.sh» 0,8 с",
           [f"крон: {crontab_names(rows)}", f"x={x}, y={y}"] + why(rc, out, rows))
    text = w.journal.read_text(encoding="utf-8") if w.journal.exists() else ""
    report(bool(text) and SECRET not in text and "API_TOKEN" not in text,
           "секрет env-префикса и имя переменной в process-load.jsonl не попали",
           ["журнала нет"] if not text else ["секрет или API_TOKEN в журнале"])
    state = Path(w.env["PROCESS_LOAD_STATE"])
    saved = state.read_text(encoding="utf-8") if state.exists() else ""
    report(bool(saved) and SECRET not in saved, "секрет env-префикса и в файле состояния не лежит",
           ["состояния нет"] if not saved else ["секрет в состоянии"])


def serve_fifo(fifo: Path, values: list, stop: threading.Event) -> None:
    """Каждое открытие FIFO на чтение получает следующее значение (последнее — повторно)."""
    served = 0
    while not stop.is_set():
        try:
            fd = os.open(fifo, os.O_WRONLY)
        except OSError:
            return
        try:
            if not stop.is_set():
                os.write(fd, values[min(served, len(values) - 1)].encode())
        except OSError:
            pass
        finally:
            os.close(fd)
        served += 1


@case("--сейчас: первый скан без транскриптов → помощник «не определён»")
def case_show_helper():
    w = World("nowhelper")
    agent_id = "a5e1f00d" + uuid.uuid4().hex[:6]
    env = {"CLAUDE_CODE_SESSION_ID": "sess-main"}
    command = "make " + "test"
    snapshot_cmd = (f"source /root/.claude/shell-snapshots/snapshot-bash-1.sh && eval '{command}' "
                    "< /dev/null && pwd -P >| /tmp/claude-1-cwd")
    talk = w.transcripts / "sess-main" / "subagents" / f"agent-{agent_id}.jsonl"
    talk.parent.mkdir(parents=True)
    talk.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_probe1", "name": "Bash", "input": {"command": command}}]}}) + "\n")
    proc_dir, cg_dir = write_snapshot(w.dir / "now", 1000.0, [
        INIT, proc(700, 650, 650, "claude", ["claude", "--dangerously-skip-permissions"], AGENT, rss_mb=300, env=env),
        proc(720, 700, 720, "bash", ["/bin/bash", "-c", snapshot_cmd], AGENT, rss_mb=30, env=env),
        proc(721, 720, 720, "make", command.split(), AGENT, rss_mb=30, env=env)], {AGENT: 1.0})
    # Два скана --сейчас читают /proc/uptime по разу: FIFO отдаёт 1000 и 1002 — окно 2 с на неподвижном снимке.
    (proc_dir / "uptime").unlink()
    os.mkfifo(proc_dir / "uptime")
    stop = threading.Event()
    server = threading.Thread(target=serve_fifo, args=(proc_dir / "uptime", ["1000.00 5400.00\n", "1002.00 5410.00\n"],
                                                       stop), daemon=True)
    server.start()
    try:
        rc, out = w.run("--сейчас", *w.doors(proc_dir, cg_dir), timeout=60)
    finally:
        stop.set()
        try:
            os.close(os.open(proc_dir / "uptime", os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            pass
    rows = [[c.strip() for c in line.split("|")] for line in out.splitlines() if "|" in line]
    helper = [r for r in rows if r and r[0].startswith("helpers")]
    report(rc == 0 and any(len(r) > 1 and r[1] == agent_id for r in helper) and "не определён" not in out,
           f"помощник по транскрипту subagents/agent-<id>.jsonl показан агентом {agent_id}, «не определён» нет",
           [f"rc={rc}", f"строки helpers: {helper}", out.replace(chr(10), " ⏎ ")[-300:]])


@case("9б. --в-библиотеку сменил права файла библиотеки (mkstemp 0600)")
def case_library_mode():
    for mode in (0o644, 0o640):
        w = World("p9m")
        w.journal.write_text("\n".join(hour_of_samples(
            [{"id": "probe-burner", "прогон": "1001", "cpu_с": 30.0, "rss_мб": 100, "процессов": 2}],
            {"cron.service": 1.0, "вне_корзин": 0.5}, 6)) + "\n", encoding="utf-8")
        os.chmod(w.library_path, mode)
        rc, out = w.run("--в-библиотеку", "--библиотека", str(w.library_path), "--журналы", str(w.logs),
                        "--проект", str(w.project))
        got = os.stat(w.library_path).st_mode & 0o777
        written = '"неделя"' in w.library_path.read_text(encoding="utf-8")
        report(rc == 0 and written and got == mode, f"права {mode:o} до замены → {mode:o} после (замер внесён)",
               [f"rc={rc} «{out[-120:]}»", f"замер {'внесён' if written else 'не внесён'}", f"права после {got:o}"])


@case("потолок строки: групп ≤ 80, строка ≤ 16 КБ, CPU не потерян")
def case_cap():
    w = World("cap")
    runs = 120
    roots = [INIT, CRON_D] + [wrapper(3000 + 2 * k, 3000 + 2 * k) for k in range(runs)]
    w.sample(1000.0, roots + [burner(3001 + 2 * k, 3000 + 2 * k, 3000 + 2 * k) for k in range(runs)], {CRON: 1.0})
    rc, out, rows = w.sample(1060.0, roots + [burner(3001 + 2 * k, 3000 + 2 * k, 3000 + 2 * k, (1.0, 0, 0, 0))
                                              for k in range(runs)], {CRON: 1.0 + runs})
    raw = w.journal.read_bytes().splitlines()[-1] if w.journal.exists() and w.journal.stat().st_size else b""
    d = data_of(rows)
    count = len(d.get("группы") or [])
    report(bool(rows) and 0 < count <= 80, f"групп ≤ 80 при {runs} прогонах", [f"групп {count}"] + why(rc, out, rows))
    report(0 < len(raw) <= 16384 and "truncated_bytes" not in d, "строка ≤ 16 КБ и не урезана events.emit",
           [f"{len(raw)} Б"])
    total = sum(float(g.get("cpu_с") or 0) for g in d.get("группы") or []) + float(
        (d.get("остаток") or {}).get("cron.service") or 0)
    report(near(total, float(runs), runs * 0.02), "Σ групп + остаток cron.service = Δcgroup ± 2 % (слитое не спрятано)",
           [f"Σ={total:.2f} из {runs}"])


@case("замок сборщика: занят → выход 0 без строки")
def case_lock():
    w = World("lock")
    lock = w.logs / "locks" / "process-dispatcher.lock"
    lock.parent.mkdir(parents=True)
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        rc, out, rows = w.sample(1000.0, [INIT, CRON_D], {CRON: 1.0})
    report(rc == 0 and not rows and bool(out), "flock $LOG_DIR/locks/process-dispatcher.lock занят → rc 0, строка в лог, без замера",
           [f"rc={rc} «{out[-120:]}», строк {len(rows)}"])


@case("показ и самотест (--сейчас, --за 24ч, --selftest)")
def case_show():
    w = World("show")
    w.journal.write_text("\n".join(hour_of_samples(
        [{"id": "probe-burner", "прогон": "1001", "cpu_с": 30.0, "rss_мб": 100, "процессов": 2}],
        {"cron.service": 1.0, "вне_корзин": 0.5}, 6)) + "\n", encoding="utf-8")
    proc_dir, cg_dir = write_snapshot(w.dir / "now", 1000.0, [INIT, CRON_D, wrapper(1001, 1001),
                                                              burner(1002, 1001, 1001)], {CRON: 1.0})
    rc, out = w.run("--сейчас", *w.doors(proc_dir, cg_dir))
    report(rc == 0 and "выключатель:" in out and "процесс" in out,
           "--сейчас: таблица «процесс | …» и шапка «выключатель: …»", [f"rc={rc} «{out[-160:]}»"])
    rc, out = w.run("--за", "24ч", *w.doors(proc_dir, cg_dir))
    report(rc == 0 and re.search(r"данных \d+ ч из 24", out) and "probe-burner" in out,
           "--за 24ч: шапка «данных N ч из 24» и строка probe-burner", [f"rc={rc} «{out[-160:]}»"])
    rc, out = w.run("--selftest")
    report(rc == 0, "--selftest зелёный", [f"rc={rc} «{out[-160:]}»"])


# Сумма групп может чуть превысить ёмкость округлением долей; больше — уже счёт не той машины.
CAPACITY_SLACK = 1.02


def capacity_breaches(rows: list) -> list:
    """Строки, где группа или Σ групп выше ёмкости окна (окно_с × ядер): физически невозможно."""
    bad = []
    for r in rows:
        d = r.get("data") or {}
        whole = float(d.get("окно_с") or 0) * float(d.get("ядер") or 0)
        cpu = [(str(g.get("id")), float(g.get("cpu_с") or 0)) for g in d.get("группы") or []]
        bad += [f"{ident}: {seconds:.1f} с > ёмкости {whole:.0f} с" for ident, seconds in cpu if seconds > whole]
        if sum(s for _, s in cpu) > whole * CAPACITY_SLACK:
            bad.append(f"Σ групп {sum(s for _, s in cpu):.1f} с > ёмкости {whole:.0f} с")
    return bad


def window_capacity_shares(out: str) -> dict:
    """«% ёмкости» по процессам из таблицы `--за 24ч` (третий столбец); строки без числа — мимо."""
    shares = {}
    for line in out.splitlines():
        cells = line.split(" | ")
        if len(cells) >= 9 and re.fullmatch(r"-?\d+(\.\d+)?", cells[2].strip()):
            shares[cells[0].strip()] = float(cells[2])
    return shares


@case("авария 24ч: cutime недоучтённого родителя (pid 1) ушёл хвостом сироты → сотни ядро-часов")
def case_untracked_parent_tail():
    w = World("init-tail")
    env = {"CLAUDE_CODE_SESSION_ID": "probe-session"}

    def world(init_kids, orphan_cpu):
        # Сирота сессии (setsid, ppid 1) живёт в юните агента; pid 1 копит cutime всех дождавшихся
        # сирот машины за аптайм — на живой 06.11 это 577 872 с.
        init = proc(1, 0, 1, "systemd", ["/sbin/init"], "/init.scope", cpu=(100.0, 30.0, init_kids, 0))
        session = proc(700, 1, 700, "claude", ["claude"], AGENT, cpu=(5.0, 0, 0, 0), start=500.0)
        orphan = [proc(1700, 1, 1700, "python3", ["python3", "-c", "work"], AGENT, cpu=(orphan_cpu, 0, 0, 0),
                       start=900.0, env=env)] if orphan_cpu is not None else []
        return [init, session] + orphan
    w.sample(1000.0, world(500000.0, 0.5), {AGENT: 10.0, "/init.scope": 130.0})
    _, _, rows1 = w.sample(1060.0, world(500000.0, 1.0), {AGENT: 10.5, "/init.scope": 130.0})
    # Сирота дожёг 0,2 с и ушёл; pid 1 дождал его: cutime +1,2.
    rc, out, rows2 = w.sample(1120.0, world(500001.2, None), {AGENT: 10.7, "/init.scope": 130.0})
    commands = cpu_of(rows2, "claude-session-commands")
    report(bool(rows2) and commands <= 0.25,
           "ушла сирота сессии (ppid 1): claude-session-commands ≤ 0,2 с её дожога, а не cutime pid 1 (≈500 000 с)",
           [f"claude-session-commands={commands}"] + why(rc, out, rows2))
    bad = capacity_breaches(rows1 + rows2)
    report(bool(rows1) and bool(rows2) and not bad,
           "ограничитель строки: каждая группа и Σ групп ≤ окно_с × ядер (+2 %)", bad or why(rc, out, rows2))
    proc_dir, cg_dir = write_snapshot(w.dir / "show", 1120.0, world(500001.2, None), {AGENT: 10.7})
    rc, out = w.run("--за", "24ч", *w.doors(proc_dir, cg_dir))
    shares = window_capacity_shares(out)
    over = [f"{k} {v} %" for k, v in shares.items() if v > 100.0]
    total = sum(shares.values())
    report(rc == 0 and bool(shares) and not over and total <= 100.0 * CAPACITY_SLACK,
           "ограничитель окна `--за 24ч`: «% ёмкости» каждой группы ≤ 100, сумма ≤ 102",
           (over or [f"Σ {total:.1f} %"] if shares else ["таблица без чисел «% ёмкости»"]) + [f"rc={rc}"])


@case("авария 24ч после выкладки: в состоянии нет pid 1 (прежний формат без родителей) → "
      "весь его cutime ушёл хвостом сироты и ужал юнит")
def case_parent_without_base():
    w = World("init-nobase")
    env = {"CLAUDE_CODE_SESSION_ID": "probe-session"}

    def world(orphan_cpu):
        init = proc(1, 0, 1, "systemd", ["/sbin/init"], "/init.scope", cpu=(100.0, 30.0, 500001.2, 0))
        session = proc(700, 1, 700, "claude", ["claude"], AGENT, cpu=(5.0, 0, 0, 0), start=500.0)
        orphan = [proc(1700, 1, 1700, "python3", ["python3", "-c", "work"], AGENT, cpu=(orphan_cpu, 0, 0, 0),
                       start=900.0, env=env)] if orphan_cpu is not None else []
        return [init, session] + orphan
    w.sample(1000.0, world(0.5), {AGENT: 10.0, "/init.scope": 130.0})
    w.sample(1060.0, world(1.0), {AGENT: 10.5, "/init.scope": 130.0})
    # Состояние как у прохода до выкладки: родителей не запоминали, pid 1 в нём нет.
    state_path = Path(w.env["PROCESS_LOAD_STATE"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["procs"] = {k: v for k, v in state["procs"].items() if k.split(":")[0] != "1"}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    rc, out, rows = w.sample(1120.0, world(None), {AGENT: 10.7, "/init.scope": 130.0})
    squeezed = (data_of(rows).get("ужатие") or {}).get("harness-agent.service") or 0
    commands = cpu_of(rows, "claude-session-commands")
    report(bool(rows) and squeezed <= 0.05 and commands <= 0.25,
           "родитель без прошлого значения хвоста не отдаёт: ужатие юнита агента ≈ 0, "
           "claude-session-commands ≤ 0,2 с (а не 500 000 с, ужатые до дельты юнита)",
           [f"ужатие={squeezed}", f"claude-session-commands={commands}"] + why(rc, out, rows))


@case("ограничитель окна: невозможная строка журнала (группа выше окно_с × ядер) входит в `--за 24ч`")
def case_window_drops_impossible():
    w = World("window-cap")
    sane = hour_of_samples([{"id": "probe-burner", "прогон": "1001", "cpu_с": 30.0, "rss_мб": 100, "процессов": 2}],
                           {"cron.service": 1.0, "вне_корзин": 0.5}, 6, count=10)
    broken = sample_row(dt.datetime.now().astimezone() - dt.timedelta(minutes=5),
                        [{"id": "probe-burner", "прогон": "1002", "cpu_с": 500000.0, "rss_мб": 100, "процессов": 1}],
                        {"cron.service": 0.0, "вне_корзин": 0.0}, 6)
    w.journal.write_text("\n".join(sane[:5] + [broken] + sane[5:]) + "\n", encoding="utf-8")
    before = w.journal.read_bytes()
    proc_dir, cg_dir = write_snapshot(w.dir / "show", 1000.0, [INIT, CRON_D], {CRON: 1.0})
    rc, out = w.run("--за", "24ч", *w.doors(proc_dir, cg_dir))
    shares = window_capacity_shares(out)
    dropped = re.search(r"отброшено строк со сбоем замера: (\d+)", out)
    report(rc == 0 and bool(shares) and max(shares.values()) <= 100.0 and dropped and dropped.group(1) == "1"
           and w.journal.read_bytes() == before,
           "строка с группой 500 000 с при ёмкости 360 с отброшена при сборке: «отброшено … 1», «% ёмкости» ≤ 100, "
           "журнал байт в байт прежний",
           [f"доли {shares}", f"отброшено {dropped.group(1) if dropped else 'нет строки'}",
            f"журнал {'цел' if w.journal.read_bytes() == before else 'изменён'}", f"rc={rc}"])


def cron_job(n: int, letter: str, cpu: float) -> list:
    """Задание крона, как на живой машине: CRON (sid демона) → sh -c (sid = pid) → обёртка → burner."""
    sh, task = 1000 * n, [str(TMP / "burner.sh"), letter]
    return [cron_child(300 + n),
            proc(sh, 300 + n, sh, "sh", ["/bin/sh", "-c", f"{WRAPPER} probe-burner -- {' '.join(task)}"], CRON),
            proc(sh + 1, sh, sh, "run-background.", ["/bin/bash", str(WRAPPER), "probe-burner", "--", *task], CRON),
            burner(sh + 2, sh + 1, sh, (cpu, 0, 0, 0), letter)]


def cron_daemon(kids: float) -> dict:
    return proc(300, 1, 300, "cron", ["/usr/sbin/cron", "-f"], CRON, cpu=(3.0, 0, kids, 0), start=10.0)


@case("остаток крона: из cutime демона cron вычтен только CRON-ребёнок, не всё ушедшее поддерево → "
      "двойной счёт ужал соседа и лёг в «неразмечено»")
def case_cron_daemon_subtree():
    w = World("crond-two")
    # Проход 1: A и B живы и разделены (2,0 и 1,0 с). К проходу 2 оба ушли, дожёг 0,5 и 0,3 с:
    # демон дождал CRON-детей, его cutime +3,8 — из них 3,0 уже учтены живыми. Сосед C жжёт 1,0 с.
    w.sample(1000.0, [INIT, cron_daemon(100.0)] + cron_job(1, "A", 0) + cron_job(2, "B", 0) + cron_job(3, "C", 0),
             {CRON: 5.0})
    _, _, rows1 = w.sample(1060.0, [INIT, cron_daemon(100.0)] + cron_job(1, "A", 2.0) + cron_job(2, "B", 1.0)
                           + cron_job(3, "C", 0), {CRON: 8.0})
    rc, out, rows2 = w.sample(1120.0, [INIT, cron_daemon(103.8)] + cron_job(3, "C", 1.0), {CRON: 9.8})
    neighbour, lost = cpu_of(rows2, "probe-burner", "3001"), cpu_of(rows2, "cron.service: неразмечено")
    squeezed = (data_of(rows2).get("ужатие") or {}).get("cron.service") or 0
    report(bool(rows1) and near(neighbour, 1.0, 0.05) and lost <= 0.85 and squeezed <= 0.05,
           "два задания ушли: сосед C = 1,0 с (не ужат), «cron.service: неразмечено» ≤ 0,8 с дожога, ужатия нет",
           [f"C={neighbour}", f"неразмечено={lost}", f"ужатие={squeezed}"] + why(rc, out, rows2))

    w = World("crond-one")
    w.sample(1000.0, [INIT, cron_daemon(100.0)] + cron_job(1, "A", 0) + cron_job(3, "C", 0), {CRON: 5.0})
    w.sample(1060.0, [INIT, cron_daemon(100.0)] + cron_job(1, "A", 2.0) + cron_job(3, "C", 0), {CRON: 7.0})
    rc, out, rows = w.sample(1120.0, [INIT, cron_daemon(102.5)] + cron_job(3, "C", 1.0), {CRON: 8.5})
    a, neighbour = cpu_of(rows, "probe-burner", "1001"), cpu_of(rows, "probe-burner", "3001")
    report(near(a, 0.5, 0.05) and near(neighbour, 1.0, 0.05),
           "одно задание ушло: хвост A = 0,5 с дожога (не 2,5 с всей жизни), сосед C = 1,0 с",
           [f"A={a}", f"C={neighbour}"] + why(rc, out, rows))
    bad = capacity_breaches(rows1 + rows2 + rows)
    report(not bad, "ограничитель строки на заданиях крона: группа и Σ ≤ ёмкости окна", bad)


# ───────────────────────── нагрузка и состояние процесса ─────────────────────────
# Модуль берётся рядом с диспетчером: дверь HARNESS_PROCESS_DISPATCHER уводит на копию и его.
PROCESS_LOAD = DISPATCHER.parent / "lib" / "process_load.py"
CHECKS_SLICE = "/harness.slice/harness-checks.slice"


def group_ids(rows: list) -> list:
    return sorted(str(g.get("id")) for g in data_of(rows).get("группы") or [])


def library_names(w: World) -> dict:
    return {r["id"]: r.get("название") for r in json.loads(w.library_path.read_text(encoding="utf-8"))["процессы"]}


@case("Н1а. bare_name([]) — пустой argv (процесс исчез или зомби) роняет разбор IndexError")
def case_bare_name_empty():
    module = load_module("process_load_probe", PROCESS_LOAD)
    try:
        name = module.bare_name([])
    except Exception as error:  # noqa: BLE001 — проба называет падение
        name = f"{type(error).__name__}: {error}"
        report(False, "bare_name([]) не падает", [name])
        return
    report(isinstance(name, str) and bool(name.strip()), "bare_name([]) не падает и даёт непустую строку",
           [f"bare_name([]) = {name!r}"])


@case("Н1б. корень крона с пустым argv (зомби в момент замера) → проход упал, минута замера потеряна")
def case_cron_root_empty_argv():
    w = World("zombie")

    def world(cpu):
        # Ребёнок CRON с sid = pid и пустым cmdline: так выглядит sh задания, завершившийся между listdir и чтением.
        return [INIT, cron_daemon(100.0)] + cron_job(1, "A", cpu) + [
            cron_child(302), proc(2000, 302, 2000, "sh", [], CRON)]
    rc1, out1, _ = w.sample(1000.0, world(0.0), {CRON: 5.0})
    rc, out, rows = w.sample(1060.0, world(1.5), {CRON: 6.5})
    a = cpu_of(rows, "probe-burner", "1001")
    report(rc1 == 0 and rc == 0 and len(rows) == 1 and near(a, 1.5, 0.05),
           "оба прохода rc 0, выборка есть, сосед probe-burner 1001 размечен = 1,5 с",
           [f"проход 1 rc={rc1} «{out1.splitlines()[-1][:120] if out1 else ''}»", f"probe-burner={a}"]
           + why(rc, out, rows))
    empty = [i for i in group_ids(rows) if not i.strip() or i == "крон:"]
    report(bool(rows) and not empty, "у корня с пустым argv имя группы не пустое (не «крон:»)",
           [f"группы: {group_ids(rows)}"] + why(rc, out, rows))


@case("Н1в. сбой разметки одного корня (любое исключение) → разметка соседей и весь проход потеряны")
def case_find_root_isolated():
    module = load_module("process_load_isolated", PROCESS_LOAD)
    real = module.bare_name

    def broken(argv, comm=""):
        # Неизвестный будущий сбой разбора: охрана должна держать любой тип, не только IndexError.
        if any("broken.sh" in a for a in argv):
            raise RuntimeError("проба")
        return real(argv, comm)

    module.bare_name = broken
    base = Path(tempfile.mkdtemp(prefix="pd-isolated-", dir=TMP))
    snapshot = [INIT, CRON_D, cron_child(301), proc(1000, 301, 1000, "sh", ["/bin/sh", "-c", "/opt/job.sh"], CRON),
                cron_child(302), proc(2000, 302, 2000, "sh", ["/bin/sh", "-c", "/opt/broken.sh"], CRON)]
    proc_dir, cg_dir = write_snapshot(base, 1000.0, snapshot, {CRON: 1.0})
    rules = module.load_rules(probe_library(TMP))
    snap = module.scan(str(proc_dir), str(cg_dir), rules)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        labels = module.label_procs(snap, {}, rules, str(proc_dir), module.Transcripts(str(base), 1000.0))
    neighbour = (labels.get(1000) or ("",))[0]
    said = err.getvalue()
    report(neighbour == "крон:job.sh" and "2000" in said and "RuntimeError" in said,
           "сосед размечен «крон:job.sh», сбой назван в stderr: pid 2000 и тип исключения",
           [f"сосед 1000: {neighbour!r}", f"stderr: {said.strip()[:160]!r}"])


@case("Н2. `run-background.sh --разрешён <id>` внутри задания → флаг «--разрешён» стал id группы")
def case_allowed_flag_not_id():
    w = World("allowed")
    task = [str(TMP / "burner.sh"), "A"]

    def world(cpu, asked):
        # session-warden.sh:1012, heartbeat-watch.sh:466: задание спрашивает обёртку, пойдёт ли другой
        # запуск. Это вопрос, не прогон: CPU вопроса — того задания, что спросило.
        return [INIT, CRON_D, cron_child(301),
                proc(1000, 301, 1000, "sh", ["/bin/sh", "-c", f"{WRAPPER} probe-burner -- {' '.join(task)}"], CRON),
                proc(1001, 1000, 1000, "run-background.", ["/bin/bash", str(WRAPPER), "probe-burner", "--", *task],
                     CRON),
                burner(1002, 1001, 1000, (cpu, 0, 0, 0)),
                proc(1003, 1002, 1000, "run-background.", ["bash", str(WRAPPER), "--разрешён", "project-revision"],
                     CRON, cpu=(asked, 0, 0, 0), start=1030.0)]
    w.sample(1000.0, world(0.0, 0.0)[:-1], {CRON: 5.0})
    rc, out, rows = w.sample(1060.0, world(2.0, 0.3), {CRON: 7.3})
    flags = [i for i in group_ids(rows) if i.startswith("-")]
    a = cpu_of(rows, "probe-burner", "1001")
    report(bool(rows) and not flags and near(a, 2.3, 0.05),
           "нет группы с id-флагом; вопрос --разрешён в строке спросившего probe-burner 1001 (2,0 + 0,3 с)",
           [f"id-флаги: {flags}", f"probe-burner={a}", f"группы: {group_ids(rows)}"] + why(rc, out, rows))


@case("Н3. transient-юнит run-uNNN.service среза проверок → сырое имя юнита вместо id библиотеки")
def case_checks_slice_transient():
    w = World("runu")
    unit = CHECKS_SLICE + "/run-u4242.service"
    task = [str(TMP / "burner.sh"), "A"]
    sliced = ["/bin/bash", str(w.project / "scripts" / "mutation-probe.sh"), "--probe"]
    # Тело юнита — как его ставит checks-slice-wrapper.sh: flock держит очередь, bash исполняет команду.
    unit_cmd = ["/usr/bin/flock", "-E", "75", "-w", "1800", "/run/lock/harness-checks-slice.lock", "/bin/bash", "-c",
                'own="$1"; shift; exec "$@"', "_", "/run/lock/harness-checks-slice.lock.owner", *sliced]

    def world(cpu):
        # Звонящий в cron.service ждёт юнит (systemd-run --wait); юнит сам — ребёнок pid 1 в срезе.
        return [INIT, CRON_D, cron_child(301),
                proc(1000, 301, 1000, "sh", ["/bin/sh", "-c", f"{WRAPPER} probe-burner -- {' '.join(task)}"], CRON),
                proc(1001, 1000, 1000, "run-background.", ["/bin/bash", str(WRAPPER), "probe-burner", "--", *task],
                     CRON),
                burner(1002, 1001, 1000),
                proc(1003, 1002, 1000, "run-in-checks-s",
                     ["bash", str(HERE / "run-in-checks-slice.sh"), *sliced], CRON),
                proc(1004, 1003, 1000, "sudo", ["sudo", "-n", "/usr/local/sbin/harness-run-in-checks",
                                                "--run-max-min", "120", "--lock-wait-min", "30", str(w.project),
                                                "HARNESS_TASK=-", "--", *sliced], CRON),
                proc(1005, 1004, 1000, "systemd-run",
                     ["/usr/bin/systemd-run", "--quiet", "--slice=harness-checks.slice", "--uid=agent", "--collect",
                      "--wait", "--pipe", "-p", "RuntimeMaxSec=7200", f"--working-directory={w.project}",
                      "--setenv=HARNESS_TASK=-", "--", *unit_cmd], CRON),
                proc(5000, 1, 5000, "flock", unit_cmd, unit),
                proc(5001, 5000, 5000, "bash", sliced, unit, cpu=(cpu, 0, 0, 0), rss_mb=60)]
    w.sample(1000.0, world(0.0), {CRON: 5.0, unit: 10.0})
    rc, out, rows = w.sample(1060.0, world(3.0), {CRON: 5.0, unit: 13.0})
    known = library_names(w)
    raw = [i for i in group_ids(rows) if i.startswith("run-u")]
    # Звонящий сам не жжёт: всё CPU под id библиотеки — это CPU юнита.
    owned = round(sum(float(g.get("cpu_с") or 0) for g in data_of(rows).get("группы") or [] if g.get("id") in known), 3)
    report(bool(rows) and not raw and near(owned, 3.0, 0.05),
           "CPU юнита среза (3,0 с) под id библиотеки (звонящего или запущенного), групп «run-u…» нет",
           [f"run-u: {raw}", f"под id библиотеки={owned}", f"группы: {group_ids(rows)}"] + why(rc, out, rows))


@case("Н4. служба проверок harness-nightly-gates.service в срезе → строка без записи и названия библиотеки")
def case_checks_service_named():
    w = World("nightly")
    unit = CHECKS_SLICE + "/harness-nightly-gates.service"
    gates = str(w.project / "harness" / "demons" / "nightly-gates.sh")

    def world(cpu):
        # ExecStart юнита: run-background.sh nightly-gates -- nightly-gates.sh (systemctl cat).
        return [INIT, proc(7001, 1, 7001, "run-background.", ["bash", str(WRAPPER), "nightly-gates", "--", gates], unit),
                proc(7002, 7001, 7001, "nightly-gates.s", ["/bin/bash", gates], unit, cpu=(cpu, 0, 0, 0), rss_mb=80)]
    w.sample(1000.0, world(0.0), {unit: 100.0})
    rc, out, rows = w.sample(1060.0, world(4.0), {unit: 104.0})
    known = library_names(w)
    carrier = [g for g in data_of(rows).get("группы") or [] if near(g.get("cpu_с"), 4.0, 0.05)]
    ok = len(carrier) == 1 and carrier[0].get("id") in known and bool(known.get(carrier[0].get("id")))
    report(bool(rows) and ok and "harness-nightly-gates.service" not in group_ids(rows),
           "CPU юнита (4,0 с) под записью библиотеки с названием (nightly-gates), не под сырым именем юнита",
           [f"носитель: {[(g.get('id'), known.get(g.get('id'))) for g in carrier]}",
            f"группы: {group_ids(rows)}"] + why(rc, out, rows))


# ───────────────────────── живой больной случай §В.7 ─────────────────────────
BURNER = """#!/bin/bash
# Проба §В.7: A жжёт CPU и порождает внука через setsid nohup, B спит.
echo "$PPID" > "$(dirname "$0")/$1.wrapper"
burn='import sys, time
end = time.process_time() + float(sys.argv[1])
while time.process_time() < end:
    pass'
if [ "$1" = A ]; then
    setsid nohup python3 -c "$burn" 1.0 >/dev/null 2>&1 &
    python3 -c "$burn" 2.0
    wait
else
    sleep 3
fi
"""


def live_case() -> None:
    print("── живой больной случай §В.7 (настоящий run-background.sh, setsid) ──")
    unit = Path("/proc/self/cgroup").read_text().strip().split("::")[-1]
    if not any(unit.endswith(u) for u in ("cron.service", "harness-agent.service", "harness-dispatcher.service")):
        report(False, "живой случай: проба идёт вне делимого юнита", [unit])
        return
    w = World("live")
    (TMP / "burner.sh").write_text(BURNER)
    (TMP / "burner.sh").chmod(0o755)
    runner_env = dict(w.env)

    def live_pass():
        before = len(w.lines())
        rc, out = w.run("--замер", "--библиотека", str(w.library_path), "--проект", str(ROOT),
                        "--журналы", str(w.logs))
        return [r for r in w.lines()[before:] if r.get("event") == "process.sample"]
    live_pass()
    runners = [subprocess.Popen(["setsid", "sh", "-c", f"{WRAPPER} probe-burner -- {TMP / 'burner.sh'} {x}"],
                                env=runner_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
               for x in "AB"]
    rows = []
    deadline = time.time() + 30
    while time.time() < deadline and any(r.poll() is None for r in runners):
        time.sleep(1)
        rows += live_pass()
    time.sleep(1)
    rows += live_pass()
    wrappers = {x: (TMP / f"{x}.wrapper").read_text().strip() if (TMP / f"{x}.wrapper").exists() else "?" for x in "AB"}
    totals = {x: round(sum(float(g.get("cpu_с") or 0) for r in rows for g in (r.get("data") or {}).get("группы") or []
                           if g.get("id") == "probe-burner" and str(g.get("прогон")) == pid), 3)
              for x, pid in wrappers.items()}
    report(wrappers["A"] != wrappers["B"] and totals["A"] >= 2.5 and totals["B"] <= 0.3,
           "живьём: две строки probe-burner с разными прогон; A ≥ 2,5 с (с внуком), B ≤ 0,3 с",
           [f"прогоны {wrappers}, CPU {totals}, проходов {len(rows)}"])
    over = [r["data"]["остаток"] for r in rows if any(isinstance(v, (int, float)) and v < -0.05
                                                       for v in ((r.get("data") or {}).get("остаток") or {}).values())]
    report(bool(rows) and not over, "живьём: Σ разделённых не выше дельты cgroup (остаток не отрицателен)", over[:2])
    ends = [json.loads(s) for s in (w.logs / "process-runs.jsonl").read_text().splitlines()
            if '"process.end"' in s] if (w.logs / "process-runs.jsonl").exists() else []
    end_a = [e["data"].get("cpu_с") for e in ends if str(e["data"].get("pid")) == wrappers["A"]]
    report(bool(end_a) and end_a[0] <= totals["A"] + 0.1, "живьём: process.end.cpu_с у A ≤ доли дерева",
           [f"process.end {end_a}, дерево {totals['A']}"])


def main() -> int:
    if not DISPATCHER.exists():
        print(f"кода нет: {DISPATCHER} — каждый случай ниже красный по этой причине")
    for runner in CASES:
        runner()
    if LIVE:
        try:
            live_case()
        except Exception as error:  # noqa: BLE001
            report(False, "живой случай упал", [f"{type(error).__name__}: {error}"])
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"ДИСПЕТЧЕР ПРОЦЕССОВ В1: путей {paths}, неудач {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
