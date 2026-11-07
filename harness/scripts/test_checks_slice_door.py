#!/usr/bin/env python3
"""Пробы двери в срез harness-checks.slice: acceptance.py --мутации-задачи и
mutation-probe.sh сами перезапускаются через run-in-checks-slice.sh, если
ещё не в срезе (задача hr.task-mutations-under-checks-slice). Круг 2 — по
ревью review/generalist.yaml (G-03…G-07, G-11).

    python3 scripts/test_checks_slice_door.py

Стенд — временный каталог: копии acceptance.py, mutation-probe.sh, двери и
lib/, своя пустая таблица мутаций, свой LOG_DIR и HARNESS_HOME. Два вида
корневой обёртки:
  * записывающая (Stand): пишет свои доводы в журнал и исполняет команду с
    ТОЛЬКО переданным окружением;
  * копия checks-slice-wrapper.sh (SliceStand), где systemd-run, каталог
    /run/lock/ и пользователь заменены стендовыми. Подставной systemd-run
    запускает команду в НОВОЙ сессии без унаследованных дескрипторов — как
    PID1: юнит переживает убийство вызывающей цепочки, а замок, взятый вне
    юнита, ему не достаётся. Подставные systemctl (потолок из файла), sudo
    (отказ, как без строки sudoers) и alert-task.py (журнал вызовов).
Живая очередь среза и живая обёртка не трогаются; живой случай — только с
HARNESS_CHECKS_SLICE_LIVE=1.

Дверь для мутационного прогона:
    HARNESS_CHECKS_SLICE_SRC   каталог скриптов под пробой (по умолчанию свой)
Последняя строка: `дверь среза: N путей, F ПЛОХО`.
"""
from __future__ import annotations

import fcntl
import getpass
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

SRC = Path(os.environ.get("HARNESS_CHECKS_SLICE_SRC") or HERE)
LIVE_WRAPPER = Path("/usr/local/sbin/harness-run-in-checks")
LIVE_LOCK_DIR = "/run/lock/"
LIVE_SYSTEMD_RUN = "/usr/bin/systemd-run"
BUSY_RC = 75
# Список имён, которые дверь вправе передать корневой половине: всё прочее —
# чужое окружение, где может лежать секрет.
ALLOWED = re.compile(r"^(HARNESS_|MUTATION_|MUT_|MUTPROBE_|CHECKS_|NIGHTLY_|LC_)[A-Za-z0-9_]*$"
                     r"|^(PATH|HOME|LANG|TERM|TMPDIR|VIRTUAL_ENV|LOG_DIR|GIT_CEILING_DIRECTORIES)$")

cases = Cases()
check = cases.check

RECORDING_WRAPPER = """#!/usr/bin/env bash
python3 -c 'import json, sys; open(sys.argv[1], "a").write(json.dumps(sys.argv[2:]) + "\\n")' {calls} "$@"
while [ "${{1:0:2}}" = -- ] && [ "$1" != -- ]; do shift 2; done
cd "$1" || exit 90; shift
pairs=()
while [ $# -gt 0 ] && [ "$1" != -- ]; do pairs+=("$1"); shift; done
shift
exec env -i "${{pairs[@]}}" "$@"
"""

SYSTEMD_RUN = """#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
open({runs!r}, "a").write(json.dumps(args) + "\\n")
cwd, env, i = "/", {{}}, 0
while i < len(args) and args[i] != "--":
    a = args[i]
    if a.startswith("--setenv="):
        k, v = a[len("--setenv="):].split("=", 1); env[k] = v
    elif a in ("--setenv", "-E"):
        k, v = args[i + 1].split("=", 1); env[k] = v; i += 1
    elif a.startswith("--working-directory="):
        cwd = a.split("=", 1)[1]
    elif a in ("--working-directory", "-p", "--property", "--uid", "--slice", "--unit", "--description"):
        if a == "--working-directory":
            cwd = args[i + 1]
        i += 1
    i += 1
env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
try:
    os.chdir(cwd)
except OSError:
    sys.exit(200)
p = subprocess.Popen(args[i + 1:], env=env, start_new_session=True)
open({units!r}, "a").write(f"{{p.pid}}\\n")
rc = p.wait()
sys.exit(rc if rc >= 0 else 128 - rc)
"""

SYSTEMCTL = """#!/usr/bin/env bash
q="$(cat {quota})"
case " $* " in *" --value "*) printf '%s\\n' "$q" ;; *) printf 'CPUQuotaPerSecUSec=%s\\n' "$q" ;; esac
"""

SUDO = """#!/usr/bin/env bash
echo "sudo: a password is required" >&2
exit 1
"""

ALERT = """#!/usr/bin/env python3
import json, sys
open({calls!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")
"""


def executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def positional(argv: list) -> list:
    """Доводы обёртки без ведущих числовых ключей (--run-max-min N …)."""
    while argv and argv[0].startswith("--") and argv[0] != "--":
        argv = argv[2:]
    return argv


def copy_tree(root: Path, localize) -> None:
    scripts = root / "scripts"
    shutil.copytree(SRC / "lib", scripts / "lib", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("acceptance.py", "mutation-probe.sh", "run-in-checks-slice.sh"):
        if (SRC / name).is_file():
            (scripts / name).write_text(localize((SRC / name).read_text(encoding="utf-8")), encoding="utf-8")
            (scripts / name).chmod(0o755)
    config = root / "harness" / "config"
    config.mkdir(parents=True)
    for journals in (SRC.parent / "harness" / "config" / "journals.yaml", SRC.parent / "config" / "journals.yaml"):
        if journals.is_file():
            shutil.copy2(journals, config / "journals.yaml")
            break


class Stand:
    """Записывающая обёртка: сколько раз и с чем позвали корневую половину."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.root = tmp / "дерево"
        (tmp / "lock").mkdir(parents=True)
        copy_tree(self.root, lambda t: t.replace(LIVE_LOCK_DIR, str(tmp / "lock") + "/"))
        self.table = self.root / "harness" / "config" / "invariants.yaml"
        self.table.write_text("инварианты: []\n", encoding="utf-8")
        for d in ("home", "logs", "work"):
            (tmp / d).mkdir()
        self.work = tmp / "work"
        self.calls = tmp / "calls.jsonl"
        self.cgroup = tmp / "cgroup"
        self.cgroup.write_text("0::/user.slice/user-1000.slice/session-1.scope\n", encoding="utf-8")
        self.wrapper = executable(tmp / "wrapper.sh", RECORDING_WRAPPER.format(calls=self.calls))
        self.secret = "probe-secret-" + secrets.token_hex(8)  # не-секрет

    def env(self, **extra: str) -> dict:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.tmp / "home"),
               "LANG": "C.UTF-8", "HARNESS_HOME": str(self.tmp / "home"),
               "LOG_DIR": str(self.tmp / "logs"), "HARNESS_LOG_DIR": str(self.tmp / "logs"),
               "HARNESS_INVARIANTS": str(self.table),
               "HARNESS_PROBE_MUTATIONS": str(self.tmp / "нет.yaml"),
               "HARNESS_CHECKS_SLICE_WRAPPER": str(self.wrapper),
               "HARNESS_CHECKS_SLICE_CGROUP_FILE": str(self.cgroup),
               "HARNESS_CHECKS_SLICE_LOCK": str(self.tmp / "lock" / "harness-checks-slice.lock"),
               "HARNESS_PROBE_MARK": "yes", "SECRET_TOKEN": self.secret,
               "GIT_CEILING_DIRECTORIES": str(self.tmp)}
        env.update(extra)
        return env

    def run(self, argv: list, env: dict, cwd: Path, timeout: int = 60) -> tuple[int, str, list]:
        """(код, вывод, доводы обёртки за этот запуск)."""
        self.calls.unlink(missing_ok=True)
        try:
            r = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True,
                               errors="replace", timeout=timeout)
            return r.returncode, r.stdout + r.stderr, jsonl(self.calls)
        except subprocess.TimeoutExpired:
            return -1, "ТАЙМАУТ (рекурсия?)", jsonl(self.calls)

    def acceptance(self, env: dict):
        return self.run([sys.executable, str(self.root / "scripts" / "acceptance.py"),
                         "--мутации-задачи", "hr.проба-среза"], env, self.work)

    def probe(self, env: dict):
        return self.run(["bash", str(self.root / "scripts" / "mutation-probe.sh"),
                         "--механизм", "scripts/нет-такого.sh"], env, self.work)


class SliceStand:
    """Копия настоящей обёртки с подставным systemd-run; дверь и обёртка
    видят один стендовый каталог замков вместо /run/lock/."""

    def __init__(self, tmp: Path, conf: str = "") -> None:
        self.tmp = tmp
        self.lock_dir = tmp / "lock"
        self.bin = tmp / "bin"
        self.home = tmp / "home"
        self.logs = tmp / "logs"
        for d in (self.lock_dir, self.bin, self.home, self.logs, tmp / "work"):
            d.mkdir(parents=True, exist_ok=True)
        self.work = tmp / "work"
        (self.home / "harness.conf").write_text(conf, encoding="utf-8")
        self.lock = self.lock_dir / "harness-checks-slice.lock"
        self.runs = tmp / "systemd-run.jsonl"
        self.units = tmp / "units.pid"
        self.quota = tmp / "quota"
        self.quota.write_text("2s", encoding="utf-8")
        self.alerts = tmp / "alerts.jsonl"
        self.cgroup = tmp / "cgroup"
        self.cgroup.write_text("0::/user.slice/user-1000.slice/session-1.scope\n", encoding="utf-8")
        self.systemd_run = executable(self.bin / "systemd-run",
                                      SYSTEMD_RUN.format(runs=str(self.runs), units=str(self.units)))
        executable(self.bin / "systemctl", SYSTEMCTL.format(quota=self.quota))
        executable(self.bin / "sudo", SUDO)
        self.wrapper = self.bin / "harness-run-in-checks"
        if (SRC / "checks-slice-wrapper.sh").is_file():
            executable(self.wrapper, self.localize((SRC / "checks-slice-wrapper.sh").read_text(encoding="utf-8")))
        self.root = tmp / "дерево"
        copy_tree(self.root, self.localize)
        executable(self.root / "scripts" / "alert-task.py", ALERT.format(calls=str(self.alerts)))
        self.door = self.root / "scripts" / "run-in-checks-slice.sh"

    def localize(self, text: str) -> str:
        """Боевые пути — в стенд: замок и его держатель не попадают в /run/lock."""
        return (text.replace(LIVE_SYSTEMD_RUN, str(self.systemd_run))
                .replace(LIVE_LOCK_DIR, str(self.lock_dir) + "/")
                .replace("@AGENT_USER@", getpass.getuser()))

    def env(self, **extra: str) -> dict:
        env = {"PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(self.home),
               "LANG": "C.UTF-8", "HARNESS_HOME": str(self.home),
               "LOG_DIR": str(self.logs), "HARNESS_LOG_DIR": str(self.logs),
               "HARNESS_CHECKS_SLICE_WRAPPER": str(self.wrapper),
               "HARNESS_CHECKS_SLICE_CGROUP_FILE": str(self.cgroup),
               "HARNESS_CHECKS_SLICE_LOCK": str(self.lock),
               "GIT_CEILING_DIRECTORIES": str(self.tmp)}
        env.update({k: v for k, v in extra.items() if v is not None})
        for k in [k for k, v in extra.items() if v is None]:
            env.pop(k, None)
        return env

    def run(self, argv: list, env: dict, timeout: int = 60) -> tuple[int, str, float]:
        start = time.monotonic()
        try:
            r = subprocess.run(argv, cwd=str(self.work), env=env, capture_output=True, text=True,
                               errors="replace", timeout=timeout)
            return r.returncode, r.stdout + r.stderr, time.monotonic() - start
        except subprocess.TimeoutExpired:
            return -1, "ТАЙМАУТ", time.monotonic() - start

    def door_run(self, *cmd: str, env: dict, timeout: int = 60):
        return self.run(["bash", str(self.door), *cmd], env, timeout)

    def holder(self, *cmd: str, env: dict) -> subprocess.Popen:
        """Долгий прогон через дверь в своей группе процессов; ждёт, пока замок занят."""
        p = subprocess.Popen(["bash", str(self.door), *cmd], cwd=str(self.work), env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not self.lock_held():
            time.sleep(0.2)
        return p

    def lock_held(self) -> bool:
        self.lock.touch(exist_ok=True)
        with open(self.lock) as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False

    def systemd_run_calls(self) -> list:
        return jsonl(self.runs)

    def alert_calls(self) -> list:
        return jsonl(self.alerts)

    def unit_pids(self) -> list[int]:
        if not self.units.is_file():
            return []
        return [int(x) for x in self.units.read_text(encoding="utf-8").split()]

    def kill_all(self, *procs: subprocess.Popen) -> None:
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            p.wait()
        for pid in self.unit_pids():
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def events(self) -> str:
        """Все строки журналов событий стенда одним текстом."""
        return "\n".join(f.read_text(encoding="utf-8", errors="replace")
                         for f in self.logs.rglob("*") if f.is_file())


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def session(pid: int) -> int:
    try:
        return os.getsid(pid)
    except OSError:
        return -1


# ── Круг 1: перезапуск через дверь ─────────────────────────────────────────

def probe_outside(s: Stand) -> None:
    """Вне среза: обёртка ровно один раз, код и вывод ребёнка доходят."""
    rc, out, recs = s.acceptance(s.env())
    check("acceptance вне среза: обёртка вызвана ровно 1 раз", 1, len(recs))
    check("acceptance вне среза: код ребёнка сохранён (2 — история не прочитана)", 2, rc)
    check("acceptance вне среза: ответил сам acceptance", True, "история не прочитана" in out)
    check("acceptance вне среза: обёртке передан cwd вызова", str(s.work),
          positional(recs[0])[0] if recs else None)
    check_env("acceptance", s, recs)

    rc, out, recs = s.probe(s.env())
    check("mutation-probe вне среза: обёртка вызвана ровно 1 раз", 1, len(recs))
    check("mutation-probe вне среза: код ребёнка сохранён (2 — записей нет)", 2, rc)
    check("mutation-probe вне среза: ответил сам прибор", True, "записей для механизма нет" in out)
    check("mutation-probe вне среза: обёртке передан корень прибора", str(s.root),
          positional(recs[0])[0] if recs else None)


def check_env(who: str, s: Stand, recs: list) -> None:
    """Окружение — только разрешённые имена; секрет не в доводах (видны в ps)."""
    if not recs:
        check(f"{who}: окружение в доводах обёртки есть", "запись", "нет вызова")
        return
    argv = positional(recs[0])
    pairs = argv[1:argv.index("--")] if "--" in argv else []
    names = [p.split("=", 1)[0] for p in pairs]
    check(f"{who}: передано окружение (есть пары ИМЯ=значение)", True, bool(names))
    check(f"{who}: все имена из разрешённого списка", [], [n for n in names if not ALLOWED.match(n)])
    check(f"{who}: HARNESS_* дошло до обёртки", True, "HARNESS_PROBE_MARK=yes" in pairs)
    check(f"{who}: секрет не попал в доводы", [], [a for a in recs[0] if s.secret in a])


def probe_inside(s: Stand) -> None:
    """В срезе обёртка не зовётся вовсе: ни по метке, ни по файлу cgroup."""
    for who, run in (("acceptance", s.acceptance), ("mutation-probe", s.probe)):
        rc, _, recs = run(s.env(HARNESS_IN_CHECKS_SLICE="1"))
        check(f"{who} с меткой HARNESS_IN_CHECKS_SLICE=1: обёртка не вызвана", 0, len(recs))
        check(f"{who} с меткой: код ребёнка 2", 2, rc)
    sliced = s.tmp / "cgroup-срез"
    sliced.write_text("0::/harness.slice/harness-checks.slice/run-u1.service\n", encoding="utf-8")
    for who, run in (("acceptance", s.acceptance), ("mutation-probe", s.probe)):
        rc, out, recs = run(s.env(HARNESS_CHECKS_SLICE_CGROUP_FILE=str(sliced)))
        check(f"{who} в срезе по cgroup: обёртка не вызвана", 0, len(recs))
        check(f"{who} в срезе по cgroup: код ребёнка 2, рекурсии нет", 2, rc)


def probe_door(s: Stand) -> None:
    """Сама дверь: cwd, код возврата и окружение ребёнка."""
    door = str(s.root / "scripts" / "run-in-checks-slice.sh")
    rc, out, recs = s.run(["bash", door, "pwd"], s.env(), s.work)
    check("дверь: ребёнок работает в cwd вызова", True, str(s.work) in out.splitlines())
    check("дверь: обёртка вызвана 1 раз", 1, len(recs))
    rc, _, _ = s.run(["bash", door, "bash", "-c", "exit 7"], s.env(), s.work)
    check("дверь: код возврата ребёнка сохранён (7)", 7, rc)
    rc, out, _ = s.run(["bash", door, "env"], s.env(), s.work)
    check("дверь: секрета нет в окружении ребёнка", False, s.secret in out or "SECRET_TOKEN" in out)
    check("дверь: ребёнок получил метку HARNESS_IN_CHECKS_SLICE=1", True,
          "HARNESS_IN_CHECKS_SLICE=1" in out.splitlines())


def probe_live() -> None:
    """Живая обёртка: команда через дверь и правда в срезе. Берёт ЖИВУЮ очередь
    среза — только по явному HARNESS_CHECKS_SLICE_LIVE=1."""
    if os.environ.get("HARNESS_CHECKS_SLICE_LIVE") != "1":
        print("  SKIP  живая дверь: без HARNESS_CHECKS_SLICE_LIVE=1 живая очередь не трогается")
        return
    if not os.access(LIVE_WRAPPER, os.X_OK):
        print(f"  SKIP  живая обёртка: нет {LIVE_WRAPPER}")
        return
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"),
           "LANG": "C.UTF-8"}
    try:
        r = subprocess.run(["bash", str(SRC / "run-in-checks-slice.sh"), "cat", "/proc/self/cgroup"],
                           env=env, capture_output=True, text=True, timeout=120)
        out = r.stdout
    except subprocess.TimeoutExpired:
        out = "ТАЙМАУТ"
    check("живая дверь: cat /proc/self/cgroup в harness-checks.slice", True,
          "harness-checks.slice" in out)


# ── Круг 2 ──────────────────────────────────────────────────────────────────

def g03(tmp: Path) -> None:
    """G-03: замок держит юнит, а не внешняя цепочка; предел жизни юнита."""
    s = SliceStand(tmp / "kill")
    marker = s.tmp / "второй-прошёл"
    a = s.holder("sleep", "30", env=s.env())
    try:
        check("G-03 больной: первый прогон взял замок", True, s.lock_held())
        os.killpg(a.pid, signal.SIGKILL)
        a.wait()
        time.sleep(0.5)
        check("G-03 больной: юнит пережил kill -KILL вызывающей цепочки", True,
              any(alive(p) for p in s.unit_pids()))
        check("G-03 больной: после kill цепочки замок всё ещё занят юнитом", True, s.lock_held())
        s.door_run("touch", str(marker), env=s.env(MUTATION_LOCK_WAIT_MIN="0"), timeout=60)
        check("G-03 больной: второй прогон замок не получил (команда не исполнена)", False, marker.exists())
    finally:
        s.kill_all(a)

    s = SliceStand(tmp / "end")
    rc, _, _ = s.door_run("true", env=s.env())
    check("G-03: юнит кончился (код 0) — замок свободен", [0, False], [rc, s.lock_held()])

    s = SliceStand(tmp / "wrap")
    w = str(s.wrapper)
    rc, out, _ = s.run([w, "--run-max-min", "120", "--lock-wait-min", "30", str(s.work), "--", "/bin/true"],
                       s.env())
    calls = s.systemd_run_calls()
    joined = " ".join(calls[-1]) if calls else ""
    check("G-03: нормальный вызов обёртки дошёл до systemd-run (код 0)", [0, 1], [rc, len(calls)])
    check("G-03: обёртка ставит RuntimeMaxSec по --run-max-min 120", True,
          bool(re.search(r"RuntimeMaxSec=(7200s?|120min|120m|2h)\b", joined)))
    check("G-03: замок берётся внутри юнита (flock со своим путём после --)", True,
          str(s.lock) in (calls[-1][calls[-1].index("--"):] if calls and "--" in calls[-1] else []))
    for bad in (["--run-max-min", "abc", "--lock-wait-min", "30"], ["--run-max-min", "120", "--lock-wait-min", "1;x"]):
        before = len(s.systemd_run_calls())
        rc, _, _ = s.run([w, *bad, str(s.work), "--", "/bin/true"], s.env())
        check(f"G-03: обёртка отказывает на нечисловом {' '.join(bad)}", [True, before],
              [rc != 0, len(s.systemd_run_calls())])
    foreign = s.tmp / "чужой.lock"
    before = len(s.systemd_run_calls())
    rc, _, _ = s.run([w, "--run-max-min", "120", "--lock-wait-min", "30", str(s.work),
                      f"HARNESS_CHECKS_SLICE_LOCK={foreign}", "--", "/bin/true"], s.env())
    calls = s.systemd_run_calls()[before:]
    check("G-03: путь замка из доводов не принимается (юнит запущен, чужого пути нет)", [1, False],
          [len(calls), any(str(foreign) in a for c in calls for a in c)])


def g04(tmp: Path) -> None:
    """G-04: ожидание очереди с пределом, код 75, держатель назван, событие."""
    s = SliceStand(tmp / "busy", conf="CHECKS_RUN_MAX_MIN=7\nMUTATION_LOCK_WAIT_MIN=2\n")
    marker = s.tmp / "второй-прошёл"
    a = s.holder("sleep", "25", env=s.env())
    try:
        rc, out, took = s.door_run("touch", str(marker), env=s.env(MUTATION_LOCK_WAIT_MIN="0"), timeout=60)
        check("G-04 больной: замок занят, MUTATION_LOCK_WAIT_MIN=0 — код 75 без ожидания",
              [BUSY_RC, True, False], [rc, took < 10, marker.exists()])
        units = s.unit_pids()
        sid = units[0] if units else -1
        numbers = [int(n) for n in re.findall(r"\b\d{2,8}\b", out)]
        named = [n for n in numbers if alive(n) and session(n) == sid]
        check("G-04: отказ называет команду держателя (sleep)", True, "sleep" in out)
        check("G-04: отказ называет pid держателя (процесс юнита-держателя)", True, bool(named))
        check("G-04: отказ называет время держателя (ЧЧ:ММ)", True, bool(re.search(r"\b\d\d:\d\d\b", out)))
        ev = s.events()
        check("G-04: отказ по очереди записан событием в журнал", True,
              bool(re.search(r"очеред|занят|busy|lock", ev)))
    finally:
        s.kill_all(a)
    s = SliceStand(tmp / "free", conf="CHECKS_RUN_MAX_MIN=7\nMUTATION_LOCK_WAIT_MIN=2\n")
    rc, _, took = s.door_run("true", env=s.env())
    calls = s.systemd_run_calls()
    joined = " ".join(calls[-1]) if calls else ""
    check("G-04: замок свободен — без ожидания, код 0", [0, True], [rc, took < 5])
    check("G-04: CHECKS_RUN_MAX_MIN=7 из harness.conf дошёл до юнита (RuntimeMaxSec=420)", True,
          bool(re.search(r"RuntimeMaxSec=(420s?|7min|7m)\b", joined)))
    check("G-04: MUTATION_LOCK_WAIT_MIN=2 из harness.conf дошёл до flock (-w 120)", True,
          bool(re.search(r"(-w|--wait|--timeout)[ =]?120\b", joined)))


def g05(tmp: Path) -> None:
    """G-05: прогон без потолка — громко (тревога + событие), а не строкой stderr."""
    s = SliceStand(tmp / "nowrap")
    marker = s.tmp / "прошёл"
    rc, out, _ = s.door_run("touch", str(marker), env=s.env(HARNESS_CHECKS_SLICE_WRAPPER="/nonexistent"))
    alerts = s.alert_calls()
    check("G-05 больной: нет обёртки — прогон всё же идёт", [0, True], [rc, marker.exists()])
    check("G-05 больной: нет обёртки — тревога «без потолка» через alert-task.py", True,
          any("потолк" in a for c in alerts for a in c))
    check("G-05 больной: нет обёртки — событие в журнале", True, "потолк" in s.events())
    s.door_run("true", env=s.env(HARNESS_CHECKS_SLICE_WRAPPER="/nonexistent"))
    keys = {c[c.index("--key") + 1] for c in s.alert_calls() if "--key" in c and c.index("--key") + 1 < len(c)}
    check("G-05: два прогона без потолка — один ключ тревоги", 1, len(keys))
    for quota in ("", "infinity"):
        s = SliceStand(tmp / f"quota-{quota or 'пусто'}")
        s.quota.write_text(quota, encoding="utf-8")
        s.door_run("true", env=s.env())
        check(f"G-05: обёртка есть, CPUQuota среза «{quota}» — тревога", True,
              any("потолк" in a for c in s.alert_calls() for a in c))
    s = SliceStand(tmp / "normal")
    rc, _, _ = s.door_run("true", env=s.env())
    check("G-05: штатно (обёртка и потолок есть) — тревоги нет", [0, 0], [rc, len(s.alert_calls())])
    s = SliceStand(tmp / "nosudo")
    door_text = s.door.read_text(encoding="utf-8")
    if "/usr/bin/sudo" in door_text or not os.access(LIVE_WRAPPER, os.X_OK):
        print("  SKIP  G-05: нет строки sudoers — дверь зовёт sudo по абсолютному пути "
              "или боевой обёртки нет: подставной sudo не встать")
        return
    rc, out, _ = s.door_run("true", env=s.env(HARNESS_CHECKS_SLICE_WRAPPER=None))
    check("G-05: нет строки sudoers — диагноз «нет права на обёртку»", True, "нет права на обёртку" in out)


def g06() -> None:
    first = (SRC / "checks-slice-wrapper.sh").read_text(encoding="utf-8").splitlines()[:1] \
        if (SRC / "checks-slice-wrapper.sh").is_file() else []
    check("G-06 больной: первая строка корневой обёртки ровно #!/bin/bash", ["#!/bin/bash"], first)


def g07(tmp: Path) -> None:
    s = Stand(tmp / "env")
    door = str(s.root / "scripts" / "run-in-checks-slice.sh")
    _, _, recs = s.run(["bash", door, "true"], s.env(), s.work)
    argv = positional(recs[0]) if recs else []
    pairs = argv[1:argv.index("--")] if "--" in argv else []
    check("G-07 больной: двери пробы HARNESS_CHECKS_SLICE_* не уходят в окружение юнита", [],
          [p.split("=", 1)[0] for p in pairs if p.startswith("HARNESS_CHECKS_SLICE_")])
    sliced = s.tmp / "cgroup-срез"
    sliced.write_text("0::/harness.slice/harness-checks.slice/run-u1.service\n", encoding="utf-8")
    _, out, _ = s.run(["bash", door, "true"], s.env(HARNESS_CHECKS_SLICE_CGROUP_FILE=str(sliced)), s.work)
    check("G-07: подставленная дверь пробы названа строкой в stderr", True,
          "HARNESS_CHECKS_SLICE_CGROUP_FILE" in out)


def g11() -> None:
    text = (SRC / "run-in-checks-slice.sh").read_text(encoding="utf-8") \
        if (SRC / "run-in-checks-slice.sh").is_file() else ""
    check("G-11: в двери нет «срезa» с латинской a", False, "срез" + "a" in text)


def g13(tmp: Path) -> None:
    """G-13: дешёвые режимы не идут через дверь и замок; тяжёлые — идут.
    Обёртка здесь только записывает вызов: тяжёлый прогон в стенде не нужен."""
    s = Stand(tmp)
    executable(s.wrapper, "#!/usr/bin/env bash\n"
               f"python3 -c 'import json, sys; open(sys.argv[1], \"a\").write(json.dumps(sys.argv[2:]) + \"\\n\")' {s.calls} \"$@\"\n")
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "база"]):
        subprocess.run(["git", "-C", str(s.root), "-c", "user.name=p", "-c", "user.email=p@s", *args],
                       check=True, capture_output=True, timeout=60)
    probe = str(s.root / "scripts" / "mutation-probe.sh")
    # Подставной держатель: очередь среза занята, дешёвый режим обязан ответить сразу.
    lock = s.tmp / "lock" / "harness-checks-slice.lock"
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        for label, argv, timeout in (("mutation-probe --сверить-тексты", ["bash", probe, "--сверить-тексты"], 30),
                                     ("mutation-probe --selftest", ["bash", probe, "--selftest"], 300)):
            rc, out, recs = s.run(argv, s.env(), s.work, timeout)
            check(f"G-13 больной: {label} при занятой очереди — ответ без двери (обёртка 0, не таймаут)",
                  [0, False], [len(recs), rc == -1])
        rc, out, recs = s.run([sys.executable, str(s.root / "scripts" / "acceptance.py"),
                               "--мутации-задачи", "no.such.id"], s.env(), s.work, 30)
        check("G-13 больной: acceptance --мутации-задачи no.such.id — «шаг пуст» без двери",
              [0, True], [len(recs), "шаг пуст" in out])
    for label, argv in (("mutation-probe --механизм", ["bash", probe, "--механизм", "scripts/нет-такого.sh"]),
                        ("mutation-probe полный набор", ["bash", probe])):
        _, _, recs = s.run(argv, s.env(), s.work)
        check(f"G-13: {label} — идёт через дверь (обёртка 1 раз)", 1, len(recs))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for name, probe in (("вне среза", lambda: probe_outside(Stand(tmp / "a"))),
                            ("в срезе", lambda: probe_inside(Stand(tmp / "b"))),
                            ("дверь", lambda: probe_door(Stand(tmp / "c"))),
                            ("живая", probe_live),
                            ("G-03", lambda: g03(tmp / "g03")),
                            ("G-04", lambda: g04(tmp / "g04")),
                            ("G-05", lambda: g05(tmp / "g05")),
                            ("G-06", g06),
                            ("G-07", lambda: g07(tmp / "g07")),
                            ("G-11", g11),
                            ("G-13", lambda: g13(tmp / "g13"))):
            try:
                probe()
            except Exception as error:  # упавший стенд — красный путь, а не трассировка
                check(f"{name}: стенд отвечает", "ответ", f"{type(error).__name__}: {error}")
    print(f"дверь среза: {cases.paths} путей, {cases.failures} ПЛОХО")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
