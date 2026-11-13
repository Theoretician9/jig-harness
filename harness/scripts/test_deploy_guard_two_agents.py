#!/usr/bin/env python3
"""Две смены агента на одной машине — выкат запрещён даже «--силой».

Отчёт другого сервера (сверка 29.10.2026, п.2): гнездо tmux пропало, сторож
поднял второй сервер, и два основных агента полдня писали в одно дерево. Выкат
поверх такого дерева выкатывает неизвестно чью работу, а «--силой» снимает
только занятость — потому запрет двух смен силой не снимается: сначала
оставить одну смену (scripts/one-shift.sh, п.4).

Проба СОЗДАЁТ условие сама: поддельный корень /proc (AGENT_PROC_ROOT), свой
MainPID (AGENT_MAINPID), свой юнит, заглушки tmux/systemctl первыми в PATH.
Поддельные pid лежат выше kernel.pid_max — таких процессов на машине быть не
может, и ошибка в коде не дотянется сигналом ни до кого живого.
Подсчёт ворота обязаны делать ЖИВЬЁМ: признак сторожа two-agents.state может
устареть, поэтому случай «устаревший признак при одном агенте» — свой.
Ручки согласия стенда приходят к судье носителем: ворота зовёт раннер пробы,
который подменяет owner_word.носитель в процессе ворот (окружение судья не слушает).
"""
import getpass
import json
import os
import pathlib
import subprocess
import sys
import tempfile

# Корень — рядом с самим файлом: проба судит тот прибор, с которым лежит.
ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parent.parent
GUARD = ROOT / "scripts" / "deploy_guard.py"

PID_MAX = int(pathlib.Path("/proc/sys/kernel/pid_max").read_text().strip())
BASE = PID_MAX + 1000
UID = os.getuid()
FOREIGN_UID = UID + 1

# Настоящий claude опознаётся по цели <pid>/exe, не по comm. Живая форма на этой
# машине (readlink /proc/<pid>/exe основного агента, 29.10.2026) — npm-установка.
CLAUDE_EXE = "/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
EXE_BY_COMM = {"claude": CLAUDE_EXE, "tmux: server": "/usr/bin/tmux", "systemd": "/usr/lib/systemd/systemd"}

INIT = BASE + 1
MAINPID = BASE + 400
UNIT_TMUX = BASE + 500
AGENT = BASE + 520
SUBAGENT = BASE + 530
SECOND_TMUX = BASE + 600
SECOND_AGENT = BASE + 620
HEADLESS = BASE + 700
FOREIGN_TMUX = BASE + 800
FOREIGN_AGENT = BASE + 820
STUB_CLAUDE = BASE + 920
PROBE_TMUX = BASE + 940
PROBE_CLAUDE = BASE + 960

# (pid, ppid, comm, uid, cmdline) — дерево юнита: MainPID → tmux-сервер → shell → claude.
UNIT_TREE = [
    (INIT, 0, "systemd", 0, ["/sbin/init"]),
    (MAINPID, INIT, "bash", UID, ["bash", "scripts/start-agent.sh"]),
    (UNIT_TMUX, MAINPID, "tmux: server", UID, ["tmux", "new-session", "-d"]),
    (BASE + 510, UNIT_TMUX, "bash", UID, ["-bash"]),
    (AGENT, BASE + 510, "claude", UID, ["claude", "--dangerously-skip-permissions"]),
]
SECOND_SHIFT = [
    (SECOND_TMUX, INIT, "tmux: server", UID, ["tmux", "new-session", "-d"]),
    (BASE + 610, SECOND_TMUX, "bash", UID, ["-bash"]),
    (SECOND_AGENT, BASE + 610, "claude", UID, ["claude", "--dangerously-skip-permissions"]),
]
SUBAGENT_TREE = [(SUBAGENT, AGENT, "claude", UID, ["claude", "-p", "--strict-mcp-config"])]
HEADLESS_TREE = [(HEADLESS, INIT, "claude", UID, ["claude", "-p", "demon"])]
FOREIGN_TREE = [
    (FOREIGN_TMUX, INIT, "tmux: server", FOREIGN_UID, ["tmux"]),
    (BASE + 810, FOREIGN_TMUX, "bash", FOREIGN_UID, ["-bash"]),
    (FOREIGN_AGENT, BASE + 810, "claude", FOREIGN_UID, ["claude"]),
]

# Заглушка claude из проб под сервером агента: comm=claude, exe — оболочка.
STUB_TREE = [
    (BASE + 910, UNIT_TMUX, "bash", UID, ["-bash"]),
    (STUB_CLAUDE, BASE + 910, "claude", UID, ["claude", "--dangerously-skip-permissions"], "/bin/bash"),
]


def probe_server(sock: list) -> list:
    """Стенд пробы: tmux-сервер с -L/-S и настоящим claude под ним."""
    return [
        (PROBE_TMUX, INIT, "tmux: server", UID, ["tmux", *sock, "new-session", "-d"]),
        (PROBE_CLAUDE, PROBE_TMUX, "claude", UID, ["claude", "--dangerously-skip-permissions"]),
    ]


bad = 0


def judge(why: str, got, want) -> None:
    global bad
    if got == want:
        print(f"  ок    {why}")
    else:
        bad += 1
        print(f"  ПЛОХО {why}: вышло {got!r}, ждали {want!r}")


def build_proc(root: pathlib.Path, procs: list) -> None:
    for pid, ppid, comm, uid, cmdline, *exe in procs:
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / "exe").symlink_to(exe[0] if exe else EXE_BY_COMM.get(comm, "/bin/bash"))
        (d / "comm").write_text(comm + "\n")
        (d / "status").write_text(f"Name:\t{comm}\nPid:\t{pid}\nPPid:\t{ppid}\n"
                                  f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in cmdline) + b"\0")


STUB = """#!/usr/bin/env python3
# Заглушка: только пишет зов в журнал; настоящий {name} не зовётся никогда.
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
    for name in ("tmux", "systemctl", "kill"):
        p = bindir / name
        p.write_text(STUB.format(name=name))
        p.chmod(0o755)


# Раннер двери: судья получает носитель стенда подменой owner_word.носитель в процессе двери.
CARRIER_RUNNER = r"""
import json, pathlib, runpy, sys
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["lib"])
import owner_word
carrier = owner_word.Носитель(папка=pathlib.Path(spec["папка"]), uid=None, паспорт=spec["паспорт"],
                              граница=pathlib.Path(spec["граница"]), окно_мин=spec["окно_мин"],
                              срок_кнопки_мин=spec["срок_кнопки_мин"], ручки=spec["ручки"])
owner_word.носитель = lambda: carrier
sys.argv = spec["argv"]
runpy.run_path(spec["argv"][0], run_name="__main__")
"""


def stand_conf(yard: pathlib.Path) -> pathlib.Path:
    """Живой harness.conf плюс выключенное согласие последними строками: иначе
    ответ ворот зависит от ручки согласия машины, а не от правила двух смен."""
    sys.path.insert(0, str(ROOT / "scripts" / "lib"))
    import config as conf
    live = pathlib.Path(conf.путь_harness_conf())
    text = live.read_text(encoding="utf-8") if live.is_file() else ""
    own = yard / "harness.conf"
    own.write_text(text + "\nDEPLOY_NEEDS_OWNER=0\nPUSH_NEEDS_OWNER=0\n", encoding="utf-8")
    return own


def ask_guard(procs: list, force: bool, state: str | None = None) -> tuple[int, str, str]:
    """(код, ответ, журнал заглушек) ворот на своём дереве и своём LOG_DIR."""
    with tempfile.TemporaryDirectory() as tmp:
        yard = pathlib.Path(tmp)
        proc_root = yard / "proc"
        build_proc(proc_root, procs)
        (yard / "log" / "locks").mkdir(parents=True)
        (yard / "stublog").mkdir()
        make_stubs(yard / "bin")
        if state is not None:
            (yard / "log" / "two-agents.state").write_text(state)
        env = dict(os.environ,
                   PATH=f"{yard / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                   AGENT_PROC_ROOT=str(proc_root), AGENT_MAINPID=str(MAINPID),
                   AGENT_UNIT="harness-agent-probe", AGENT_USER=getpass.getuser(),
                   FAKE_TMUX_PID=str(UNIT_TMUX), STUB_LOG_DIR=str(yard / "stublog"),
                   HARNESS_LOG_DIR=str(yard / "log"), LOG_DIR=str(yard / "log"),
                   HARNESS_CONF=str(stand_conf(yard)))
        # Без носителя стенда ворота судят согласие по /etc/harness машины: там своё
        # стоящее согласие — и ответ зависел бы от машины, а не от правила двух смен.
        sys.path.insert(0, str(ROOT / "scripts" / "lib"))
        import config as conf
        carrier = {"lib": str(ROOT / "scripts" / "lib"), "папка": str(yard / "log"), "граница": str(yard),
                   "паспорт": {"AUTONOMY": "semi", "LOG_DIR": str(yard / "log")}, "окно_мин": 60,
                   "срок_кнопки_мин": None, "ручки": conf.из_файла(env["HARNESS_CONF"]),
                   "argv": [str(GUARD)] + (["--силой"] if force else [])}
        argv = [sys.executable, "-c", CARRIER_RUNNER, json.dumps(carrier, ensure_ascii=False)]
        done = subprocess.run(argv, capture_output=True, text=True, env=env,
                              check=False, timeout=120)
        stubs = "".join(p.read_text() for p in sorted((yard / "stublog").glob("*.log")))
        return done.returncode, done.stdout + done.stderr, stubs


def banned(out: str) -> bool:
    return "два агента" in out


# Защита стенда: поддельные pid обязаны не существовать на машине.
judge("поддельные pid выше pid_max и в настоящем /proc их нет",
      any(pathlib.Path(f"/proc/{BASE + k}").exists() for k in range(0, 1000)), False)

# БОЛЬНОЙ СЛУЧАЙ: две смены — запрет без силы и с силой, с pid и командой п.4.
for force in (False, True):
    label = "«--силой»" if force else "без силы"
    rc, out, stubs = ask_guard(UNIT_TREE + SECOND_SHIFT, force)
    judge(f"два основных агента, {label}: выкат запрещён (rc 1)", rc, 1)
    judge(f"два основных агента, {label}: причина «работают два агента»", banned(out), True)
    judge(f"два основных агента, {label}: названы оба pid",
          (str(AGENT) in out, str(SECOND_AGENT) in out), (True, True))
    judge(f"два основных агента, {label}: названа команда «оставить одну смену»",
          "one-shift" in out, True)
    judge(f"два основных агента, {label}: ворота не перезапускали юнит",
          any(w in stubs for w in ("restart", "stop", "kill")), False)
    if rc != 1 or not banned(out):
        print("    ответ ворот: " + out.strip().replace("\n", "\n    ")[-600:])

# Без двух смен этого запрета нет; с силой ворота пропускают (rc 0) — иначе
# запрет зеленел бы на любом отказе, а не на подсчёте основных агентов.
QUIET = [
    ("один основной агент", UNIT_TREE, None),
    ("агент и его подагент (claude под claude)", UNIT_TREE + SUBAGENT_TREE, None),
    ("агент и headless claude без tmux и MainPID", UNIT_TREE + HEADLESS_TREE, None),
    ("агент и claude чужого пользователя в своём tmux", UNIT_TREE + FOREIGN_TREE, None),
    ("устаревший признак two-agents.state при одном живом агенте", UNIT_TREE,
     f"count=2\npids={AGENT} {SECOND_AGENT}\nsince=2026-10-29T10:00:00\nupdated=2026-10-29T10:00:00\n"),
    ("агент и заглушка claude (exe=/bin/bash) под сервером агента", UNIT_TREE + STUB_TREE, None),
    ("агент и настоящий claude под стендом tmux -L", UNIT_TREE + probe_server(["-L", "probe-w1t"]), None),
    ("агент и настоящий claude под стендом tmux -S", UNIT_TREE + probe_server(["-S", "/tmp/probe.sock"]), None),
]
for why, procs, state in QUIET:
    rc, out, _ = ask_guard(procs, force=True, state=state)
    judge(f"{why}: запрета двух смен нет, «--силой» пропускает (rc 0)",
          (rc, banned(out)), (0, False))
    if rc != 0 or banned(out):
        print("    ответ ворот: " + out.strip().replace("\n", "\n    ")[-600:])

print(f"ИТОГ: плохих {bad}")
sys.exit(1 if bad else 0)
