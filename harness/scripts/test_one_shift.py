#!/usr/bin/env python3
"""«Оставить одну смену»: scripts/one-shift.sh закрывает лишнего основного агента.

Сверка 29.10.2026, п.4: юнит harness-agent (Type=forking, KillMode=control-group)
пережил вторую смену, а команды убрать лишнюю не было — руками легко прибить
MainPID или сервер tmux юнита, и тогда systemd снесёт всю группу вместе с
живой сменой. Команда обязана знать MainPID и никогда не слать сигнал ни ему,
ни серверу tmux юнита; без MainPID при двух агентах — отказ, не угадывать.

Проба СОЗДАЁТ условие сама: поддельный корень /proc (AGENT_PROC_ROOT), свой
MainPID, свой юнит, заглушки kill/tmux/systemctl первыми в PATH; заглушка kill
пишет журнал «СИГНАЛ PID» и по желанию «умирает» процесс (убирает его каталог).
Поддельные pid лежат выше kernel.pid_max: даже встроенный kill оболочки в коде
не дотянется ни до кого живого — а журнал заглушки останется пустым, и проба
покраснеет.
"""
import getpass
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / "scripts" / "one-shift.sh"

PID_MAX = int(pathlib.Path("/proc/sys/kernel/pid_max").read_text().strip())
BASE = PID_MAX + 1000
UID = os.getuid()

# Настоящий claude опознаётся по цели <pid>/exe, не по comm. Живая форма на этой
# машине (readlink /proc/<pid>/exe основного агента, 29.10.2026) — npm-установка.
CLAUDE_EXE = "/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
EXE_BY_COMM = {"claude": CLAUDE_EXE, "tmux: server": "/usr/bin/tmux", "systemd": "/usr/lib/systemd/systemd"}

INIT = BASE + 1
MAINPID = BASE + 400
UNIT_TMUX = BASE + 500
AGENT = BASE + 520
SUBAGENT = BASE + 530
TWIN = BASE + 540
SECOND_TMUX = BASE + 600
SECOND_AGENT = BASE + 620
THIRD_TMUX = BASE + 900
THIRD_AGENT = BASE + 920


def shift(tmux: int, parent: int, agent: int) -> list:
    """Смена: сервер tmux → shell → claude. (pid, ppid, comm, cmdline)."""
    return [
        (tmux, parent, "tmux: server", ["tmux", "new-session", "-d"]),
        (tmux + 10, tmux, "bash", ["-bash"]),
        (agent, tmux + 10, "claude", ["claude", "--dangerously-skip-permissions"]),
    ]


ROOT_PROCS = [(INIT, 0, "systemd", ["/sbin/init"])]
# Раскладка А: MainPID — start-agent.sh, сервер tmux юнита — его потомок.
LAYOUT_A = ROOT_PROCS + [(MAINPID, INIT, "bash", ["bash", "scripts/start-agent.sh"])] \
    + shift(UNIT_TMUX, MAINPID, AGENT)
# Раскладка Б: Type=forking, MainPID и есть сервер tmux юнита.
LAYOUT_B = ROOT_PROCS + shift(MAINPID, INIT, AGENT)
SECOND = shift(SECOND_TMUX, INIT, SECOND_AGENT)

bad = 0


def judge(why: str, got, want) -> None:
    global bad
    if got == want:
        print(f"  ок    {why}")
    else:
        bad += 1
        print(f"  ПЛОХО {why}: вышло {got!r}, ждали {want!r}")


def build_proc(root: pathlib.Path, procs: list) -> None:
    for pid, ppid, comm, cmdline in procs:
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / "exe").symlink_to(EXE_BY_COMM.get(comm, "/bin/bash"))
        (d / "comm").write_text(comm + "\n")
        (d / "status").write_text(f"Name:\t{comm}\nPid:\t{pid}\nPPid:\t{ppid}\n"
                                  f"Uid:\t{UID}\t{UID}\t{UID}\t{UID}\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in cmdline) + b"\0")


KILL_STUB = r"""#!/usr/bin/env python3
# Заглушка kill: настоящий сигнал не шлётся никогда. Пишет «СИГНАЛ PID»;
# при FAKE_DIE_ON_TERM=1 «процесс умирает» — его каталог в поддельном /proc уходит;
# FAKE_DIE_DELAY>0 — уходит в фоне через столько секунд, как настоящий процесс.
import os, shutil, subprocess, sys
names = {"15": "TERM", "9": "KILL", "0": "0", "1": "HUP", "2": "INT"}
sig, pids, args = "TERM", [], sys.argv[1:]
while args:
    a = args.pop(0)
    if a in ("-s", "-n", "--signal"):
        sig = args.pop(0) if args else sig
    elif a == "--":
        continue
    elif a.startswith("-") and not a[1:].isdigit() or a in ("-0", "-1", "-2", "-9", "-15"):
        sig = a[1:]
    else:
        pids.append(a)
sig = sig.upper().removeprefix("SIG")
sig = names.get(sig, sig)
root = os.environ["AGENT_PROC_ROOT"]
alive = True
with open(os.path.join(os.environ["STUB_LOG_DIR"], "kill.log"), "a") as f:
    for p in pids:
        f.write(f"{sig} {p}\n")
        d = os.path.join(root, p.lstrip("-"))
        alive = alive and os.path.isdir(d)
        if sig == "TERM" and os.environ.get("FAKE_DIE_ON_TERM") == "1" and os.path.isdir(d):
            delay = os.environ.get("FAKE_DIE_DELAY", "0")
            if float(delay) > 0:
                subprocess.Popen(["sh", "-c", f"sleep {delay}; rm -rf '{d}'"], start_new_session=True)
            else:
                shutil.rmtree(d)
sys.exit(0 if alive else 1)
"""

LOG_STUB = """#!/usr/bin/env python3
# Заглушка {name}: только журнал зовов; MainPID и pid сервера — из среды пробы.
import os, sys
with open(os.path.join(os.environ["STUB_LOG_DIR"], "{name}.log"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
args = " ".join(sys.argv[1:])
if "{name}" == "systemctl" and "MainPID" in args:
    v = os.environ.get("AGENT_MAINPID", "0")
    print(v if "--value" in sys.argv else "MainPID=" + v)
if "{name}" == "tmux" and "#{{pid}}" in args and os.environ.get("FAKE_TMUX_PID"):
    print(os.environ["FAKE_TMUX_PID"])
"""


def make_stubs(bindir: pathlib.Path) -> None:
    bindir.mkdir()
    (bindir / "kill").write_text(KILL_STUB)
    for name in ("tmux", "systemctl"):
        (bindir / name).write_text(LOG_STUB.format(name=name))
    for p in bindir.iterdir():
        p.chmod(0o755)


def run(procs: list, mainpid: int, tmux_pid: int, args: list, die_on_term: bool = True,
        die_delay: float = 0) -> dict:
    """Прогон команды на своём дереве → код, вывод, сигналы, зовы tmux/systemctl."""
    with tempfile.TemporaryDirectory() as tmp:
        yard = pathlib.Path(tmp)
        build_proc(yard / "proc", procs)
        (yard / "log").mkdir()
        (yard / "stublog").mkdir()
        make_stubs(yard / "bin")
        env = dict(os.environ,
                   PATH=f"{yard / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                   AGENT_PROC_ROOT=str(yard / "proc"), AGENT_MAINPID=str(mainpid),
                   AGENT_UNIT="harness-agent-probe", AGENT_USER=getpass.getuser(),
                   FAKE_TMUX_PID=str(tmux_pid or ""), STUB_LOG_DIR=str(yard / "stublog"),
                   FAKE_DIE_ON_TERM="1" if die_on_term else "0", FAKE_DIE_DELAY=str(die_delay),
                   HARNESS_LOG_DIR=str(yard / "log"), LOG_DIR=str(yard / "log"))
        if TOOL.is_file():
            done = subprocess.run(["bash", str(TOOL), *args], capture_output=True, text=True,
                                  env=env, check=False, timeout=60)
            rc, out = done.returncode, done.stdout + done.stderr
        else:
            rc, out = 127, f"нет {TOOL}"
        log = yard / "stublog"

        def read(name: str) -> list:
            p = log / f"{name}.log"
            return p.read_text().splitlines() if p.is_file() else []

        signals = [tuple(line.split()) for line in read("kill")]
        return {"rc": rc, "out": out, "signals": signals,
                "tmux": read("tmux"), "systemctl": read("systemctl")}


def touched(res: dict, pid: int) -> list:
    return [s for s in res["signals"] if s[1].lstrip("-") == str(pid)]


def show(res: dict) -> None:
    print(f"    rc={res['rc']} сигналы={res['signals']} вывод: "
          + res["out"].strip().replace("\n", "\n    ")[-500:])


def untouched_unit(why: str, res: dict, mainpid: int, tmux_pid: int) -> None:
    judge(f"{why}: MainPID ни одного сигнала", touched(res, mainpid), [])
    judge(f"{why}: сервер tmux юнита ни одного сигнала", touched(res, tmux_pid), [])
    judge(f"{why}: юнит и сервер tmux не тронуты (systemctl/tmux)",
          [c for c in res["systemctl"] + res["tmux"]
           if any(w in c for w in ("restart", "stop", "kill"))], [])


judge("поддельные pid выше pid_max и в настоящем /proc их нет",
      any(pathlib.Path(f"/proc/{BASE + k}").exists() for k in range(0, 1000)), False)
judge("команда лежит на месте: scripts/one-shift.sh", TOOL.is_file(), True)

# БОЛЬНОЙ СЛУЧАЙ: две смены, обе раскладки юнита.
for name, layout, tmux_pid in (("раскладка А (MainPID=start-agent)", LAYOUT_A, UNIT_TMUX),
                               ("раскладка Б (MainPID=сервер tmux)", LAYOUT_B, MAINPID)):
    res = run(layout + SECOND, MAINPID, tmux_pid, ["--grace", "1"])
    why = f"две смены, {name}"
    judge(f"{why}: rc 0", res["rc"], 0)
    judge(f"{why}: лишний claude получил TERM", touched(res, SECOND_AGENT)[:1],
          [("TERM", str(SECOND_AGENT))])
    judge(f"{why}: умер от TERM — KILL не нужен",
          [s for s in touched(res, SECOND_AGENT) if s[0] == "KILL"], [])
    judge(f"{why}: агент юнита не тронут", touched(res, AGENT), [])
    untouched_unit(why, res, MAINPID, tmux_pid)
    if res["rc"] != 0 or not touched(res, SECOND_AGENT):
        show(res)

# Лишний не умирает от TERM — через --grace секунд KILL.
res = run(LAYOUT_A + SECOND, MAINPID, UNIT_TMUX, ["--grace", "1"], die_on_term=False)
judge("лишний пережил TERM: следом KILL ему же",
      [s[0] for s in touched(res, SECOND_AGENT) if s[0] in ("TERM", "KILL")], ["TERM", "KILL"])
untouched_unit("лишний пережил TERM", res, MAINPID, UNIT_TMUX)
judge("лишний пережил TERM: агент юнита не тронут", touched(res, AGENT), [])

# Лишний умирает не сразу — команда обязана ждать до --grace, а не слать KILL.
res = run(LAYOUT_A + SECOND, MAINPID, UNIT_TMUX, ["--grace", "2"], die_delay=0.5)
judge("лишний умер через 0.5 с при --grace 2: TERM один раз, KILL не послан",
      [s[0] for s in touched(res, SECOND_AGENT) if s[0] in ("TERM", "KILL")], ["TERM"])

# Подагент агента юнита — не вторая смена и не мишень.
res = run(LAYOUT_A + [(SUBAGENT, AGENT, "claude", ["claude", "-p"])] + SECOND,
          MAINPID, UNIT_TMUX, ["--grace", "1"])
judge("две смены и подагент: подагент юнита не тронут", touched(res, SUBAGENT), [])
judge("две смены и подагент: лишний получил TERM", touched(res, SECOND_AGENT)[:1],
      [("TERM", str(SECOND_AGENT))])

# Одна смена — «уже одна смена», ни одного вызова kill.
res = run(LAYOUT_A + [(SUBAGENT, AGENT, "claude", ["claude", "-p"])], MAINPID, UNIT_TMUX, [])
judge("одна смена: rc 0, «уже одна смена», ноль вызовов kill",
      (res["rc"], "уже одна смена" in res["out"], res["signals"]), (0, True, []))

# Отказы — rc ≠ 0 (и не «нет файла»), ноль вызовов kill.
REFUSE = [
    ("MainPID=0 при двух сменах", LAYOUT_A + SECOND, 0, UNIT_TMUX, "MainPID"),
    ("ни один агент не принадлежит юниту",
     ROOT_PROCS + [(MAINPID, INIT, "bash", ["bash", "scripts/start-agent.sh"])]
     + SECOND + shift(THIRD_TMUX, INIT, THIRD_AGENT), MAINPID, 0, None),
    ("два агента в юните — какой оставить, неизвестно",
     LAYOUT_A + [(TWIN, UNIT_TMUX + 10, "claude", ["claude"])] + SECOND, MAINPID, UNIT_TMUX, None),
]
for why, procs, mainpid, tmux_pid, word in REFUSE:
    res = run(procs, mainpid, tmux_pid, ["--grace", "1"])
    judge(f"{why}: отказ (rc не 0/126/127), ноль вызовов kill",
          (res["rc"] not in (0, 126, 127), res["signals"]), (True, []))
    if word:
        judge(f"{why}: причина названа ({word})", word in res["out"], True)

# MainPID=0: смены вне юнита — команда не угадывает, но даёт владельцу руки:
# pid каждого основного агента и чем закрыть лишнего.
res = run(LAYOUT_A + SECOND, 0, UNIT_TMUX, ["--grace", "1"])
out = res["out"]
judge("MainPID=0 при двух сменах: rc 1, ноль сигналов", (res["rc"], res["signals"]), (1, []))
judge("MainPID=0: вывод называет MainPID и что смены не юнитом/вне юнита",
      ("MainPID" in out, "не юнитом" in out or "вне юнита" in out), (True, True))
judge("MainPID=0: названы pid обоих основных агентов",
      (str(AGENT) in out, str(SECOND_AGENT) in out), (True, True))
judge("MainPID=0: подсказка закрыть руками «kill -TERM <pid>»",
      any(f"kill -TERM {p}" in out for p in (AGENT, SECOND_AGENT)), True)
if res["rc"] != 1 or "kill -TERM" not in out:
    show(res)

# --dry-run печатает, кого закрыл бы, и не шлёт ничего.
res = run(LAYOUT_A + SECOND, MAINPID, UNIT_TMUX, ["--dry-run"])
judge("--dry-run: rc 0, назван лишний pid, ноль вызовов kill",
      (res["rc"], str(SECOND_AGENT) in res["out"], res["signals"]), (0, True, []))

print(f"ИТОГ: плохих {bad}")
sys.exit(1 if bad else 0)
