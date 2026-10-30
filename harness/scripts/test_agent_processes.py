#!/usr/bin/env python3
"""Проба библиотеки scripts/lib/agent_processes.py — «кто из claude основной».

Откуда взята. Отчёт с другого сервера (docs/specs/2026-10-29-сверка-отчёта-
другого-сервера.md, п.1–2): гнездо tmux пропало при живом сервере, сторож
увидел «нет has-session» и поднял ВТОРОЙ сервер и вторую смену. Две смены
работали параллельно, и тревоги об этом не было вовсе. Сторожу нужен судья,
который по дереву процессов отличает «гнездо пропало» от «агента нет», и
считает основных агентов, не путая их с подагентами и пробами.

Дерево процессов здесь ПОДДЕЛЬНОЕ (AGENT_PROC_ROOT): проба никогда не видит
настоящих pid — живой агент работает под тем же пользователем, и утечка в
настоящий /proc видна случаем «пусто» (он насчитал бы живую смену). tmux,
kill и systemctl — заглушки через PATH; kill только пишет журнал.

Путь к библиотеке — из окружения (AGENT_PROCESSES_LIB), чтобы испорченная
мутацией копия доходила до пробы.
"""
import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

LIB = Path(os.environ.get("AGENT_PROCESSES_LIB", HERE / "lib" / "agent_processes.py"))
MY_UID = os.getuid()
MY_USER = pwd.getpwuid(MY_UID).pw_name
# Чужой uid берётся заведомо несуществующим: не путается ни с кем на машине.
FOREIGN_UID = 54321
TMUX_COMM = "tmux: server"
# Настоящий claude опознаётся по цели <pid>/exe, а не по comm: comm=claude
# бывает у любой заглушки-скрипта. Живая форма на этой машине (readlink
# /proc/<pid>/exe основного агента, 29.10.2026) — npm-установка:
CLAUDE_EXE_NPM = "/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
# Форма родного установщика: каталог версий.
CLAUDE_EXE_NATIVE = "/home/x/.local/share/claude/versions/2.1/claude"
# Заглушки проб: comm=claude, а исполняет оболочка или питон.
STUB_EXE_BASH = "/bin/bash"
STUB_EXE_PY = "/usr/bin/python3"
EXE_BY_COMM = {"claude": CLAUDE_EXE_NPM, TMUX_COMM: "/usr/bin/tmux", "systemd": "/usr/lib/systemd/systemd"}

TMUX_STUB = """#!/bin/sh
# Заглушка tmux: has-session отвечает кодом из файла, остальное — отказ.
echo "$@" >> "{log}"
for a in "$@"; do
    [ "$a" = has-session ] && exit "$(cat "{rc}")"
done
exit 1
"""
KILL_STUB = """#!/bin/sh
# Заглушка kill: только журнал, никакого сигнала.
echo "$@" >> "{log}"
exit 0
"""
SYSTEMCTL_STUB = """#!/bin/sh
echo "$@" >> "{log}"
echo 777
"""


class Stand:
    """Своя песочница: поддельный /proc, заглушки и их журналы."""

    def __init__(self, base: Path, name: str):
        self.dir = base / name
        self.proc = self.dir / "proc"
        self.bin = self.dir / "bin"
        self.proc.mkdir(parents=True)
        self.bin.mkdir()
        self.tmux_log = self.dir / "tmux.log"
        self.kill_log = self.dir / "kill.log"
        self.systemctl_log = self.dir / "systemctl.log"
        self.has_session_rc = self.dir / "has-session.rc"
        self.has_session_rc.write_text("1\n")
        for name_, body in (("tmux", TMUX_STUB.format(log=self.tmux_log, rc=self.has_session_rc)),
                            ("kill", KILL_STUB.format(log=self.kill_log)),
                            ("systemctl", SYSTEMCTL_STUB.format(log=self.systemctl_log))):
            stub = self.bin / name_
            stub.write_text(body)
            stub.chmod(0o755)
        self.add(1, 0, "systemd", ["/sbin/init"], uid=0)

    def add(self, pid: int, ppid: int, comm: str, cmdline: list, uid: int = MY_UID,
            exe: str | None = None) -> None:
        d = self.proc / str(pid)
        d.mkdir()
        # Цель симлинка не обязана существовать: судья читает readlink, не файл.
        os.symlink(exe or EXE_BY_COMM.get(comm, "/bin/bash"), d / "exe")
        (d / "comm").write_text(comm + "\n")
        (d / "status").write_text(
            f"Name:\t{comm}\nState:\tS (sleeping)\nPid:\t{pid}\nPPid:\t{ppid}\n"
            f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in cmdline) + b"\0")

    def tmux_server(self, pid: int, uid: int = MY_UID, sock: list | None = None) -> None:
        """sock=None — сервер сокета по умолчанию (сервер агента); ["-L", имя] или
        ["-S", путь] — стенд пробы."""
        self.add(pid, 1, TMUX_COMM, ["tmux", *(sock or []), "new-session", "-d", "-s", "agent"], uid)

    def claude(self, pid: int, ppid: int, uid: int = MY_UID, exe: str | None = None,
               argv: list | None = None) -> None:
        self.add(pid, ppid, "claude", argv or ["claude", "--dangerously-skip-permissions"], uid, exe)

    def stub(self, pid: int, ppid: int, exe: str = STUB_EXE_BASH) -> None:
        """Скрипт-заглушка claude из проб: comm и argv[0] — claude, exe — интерпретатор."""
        self.add(pid, ppid, "claude", ["claude", "--dangerously-skip-permissions"], exe=exe)

    def run(self, *args: str, mainpid: str | None = "0", unit: str | None = None):
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.dir),
            "LC_ALL": "C.UTF-8",
            "AGENT_PROC_ROOT": str(self.proc),
            "AGENT_USER": MY_USER,
            "TMUX_SESSION": "agent",
            "TMUX_TMPDIR": str(self.dir),
        }
        if mainpid is not None:
            env["AGENT_MAINPID"] = mainpid
        if unit is not None:
            env["AGENT_UNIT"] = unit
        r = subprocess.run([sys.executable, str(LIB), *args], env=env, capture_output=True,
                           text=True, timeout=30)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def log(self, path: Path) -> str:
        return path.read_text().strip() if path.exists() else ""


def word(out: str) -> str:
    return out.split()[0] if out.split() else ""


def pids(out: str) -> set:
    return {t for t in out.replace(",", " ").split() if t.isdigit()}


def main() -> int:
    c = Cases()
    c.check("библиотека agent_processes.py существует", True, LIB.is_file())
    with tempfile.TemporaryDirectory(prefix="agent-processes-") as tmp:
        base = Path(tmp)

        print("── БОЛЬНОЙ СЛУЧАЙ отчёта: гнезда нет, а сервер с агентом жив ──")
        s = Stand(base, "lost")
        s.tmux_server(100)
        s.add(101, 100, "bash", ["-bash"])
        s.claude(102, 101)
        rc, out, _ = s.run("status")
        c.check("гнездо пропало, сервер и агент живы — lost;1", "lost;1", f"{word(out)};{rc}")
        rc, out, _ = s.run("restore-nest")
        c.check("restore-nest послал сигнал — rc 0", 0, rc)
        c.check("restore-nest: kill -USR1 ровно pid'у tmux-сервера", "-USR1 100", s.log(s.kill_log))

        print("── один агент под tmux ──")
        s = Stand(base, "one")
        s.tmux_server(100)
        s.add(101, 100, "bash", ["-bash"])
        s.claude(102, 101)
        s.has_session_rc.write_text("0\n")
        rc, out, _ = s.run("claude-count", "--main")
        c.check("основных агентов 1", "1;0", f"{out};{rc}")
        rc, out, _ = s.run("tmux-server")
        c.check("tmux-server печатает pid сервера агента", "100;0", f"{out};{rc}")
        rc, out, _ = s.run("status")
        c.check("гнездо отвечает — ok;0", "ok;0", f"{word(out)};{rc}")
        rc, out, _ = s.run("two-agents")
        c.check("один агент — two-agents rc 0", 0, rc)

        print("── агент и его подагент ──")
        s = Stand(base, "sub")
        s.tmux_server(100)
        s.add(101, 100, "bash", ["-bash"])
        s.claude(102, 101)
        s.add(103, 102, "bash", ["bash", "-c", "x"])
        s.claude(104, 103)
        rc, out, _ = s.run("claude-count", "--main")
        c.check("подагент не основной: основных 1", "1;0", f"{out};{rc}")
        rc, out, _ = s.run("claude-count")
        c.check("всех claude 2", "2", out)
        rc, out, _ = s.run("claude-list")
        c.check("claude-list называет обоих", {"102", "104"}, pids(out))
        rc, out, _ = s.run("two-agents")
        c.check("агент с подагентом — не две смены (rc 0)", 0, rc)

        print("── две смены: два агента под двумя tmux-серверами ──")
        s = Stand(base, "two")
        s.tmux_server(100)
        s.claude(102, 100)
        s.tmux_server(200)
        s.claude(202, 200)
        rc, out, _ = s.run("claude-count", "--main")
        c.check("основных агентов 2", "2", out)
        rc, out, _ = s.run("two-agents")
        c.check("two-agents rc 1", 1, rc)
        c.check("two-agents печатает pid обоих", {"102", "202"}, pids(out))

        print("── агент вне tmux, MainPID=0 ──")
        s = Stand(base, "orphan")
        s.claude(300, 1)
        rc, out, _ = s.run("status")
        c.check("агент жив, сервера нет — orphan;3", "orphan;3", f"{word(out)};{rc}")
        rc, out, _ = s.run("claude-count", "--main")
        c.check("claude без tmux и MainPID — не основной (проба)", "0", out)
        rc, out, _ = s.run("restore-nest")
        c.check("restore-nest без сервера — отказ и kill не зван", "1;", f"{min(rc, 1)};{s.log(s.kill_log)}")

        print("── агент под MainPID юнита ──")
        s = Stand(base, "mainpid")
        s.add(400, 1, "bash", ["bash", "start-agent.sh"])
        s.claude(401, 400)
        rc, out, _ = s.run("claude-count", "--main", mainpid="400")
        c.check("claude под MainPID — основной", "1", out)
        rc, out, _ = s.run("mainpid", mainpid="400")
        c.check("mainpid берёт AGENT_MAINPID", "400", out)
        c.check("при заданном AGENT_MAINPID systemctl не зван", "", s.log(s.systemctl_log))
        rc, out, _ = s.run("mainpid", mainpid=None, unit="podstavnoj-unit")
        c.check("без AGENT_MAINPID — из systemctl", "777", out)
        log = s.log(s.systemctl_log)
        c.check("systemctl спрошен про MainPID своего юнита",
                True, "MainPID" in log and "podstavnoj-unit" in log)

        print("── пусто: ни сервера, ни агента ──")
        s = Stand(base, "empty")
        rc, out, _ = s.run("status")
        c.check("ничего нет — absent;2 (живая смена из настоящего /proc не видна)",
                "absent;2", f"{word(out)};{rc}")
        rc, out, _ = s.run("tmux-server")
        c.check("tmux-server пуст — rc 1", ";1", f"{out};{rc}")
        rc, out, _ = s.run("claude-count")
        c.check("claude нет", "0", out)

        print("── чужой uid не считается ──")
        s = Stand(base, "foreign")
        s.tmux_server(500, uid=FOREIGN_UID)
        s.claude(501, 500, uid=FOREIGN_UID)
        rc, out, _ = s.run("claude-count", "--main")
        c.check("только чужой агент — основных 0", "0", out)
        rc, out, _ = s.run("status")
        c.check("только чужие процессы — absent;2", "absent;2", f"{word(out)};{rc}")
        s.tmux_server(100)
        s.claude(102, 100)
        rc, out, _ = s.run("two-agents")
        c.check("свой + чужой — не две смены (rc 0)", 0, rc)
        rc, out, _ = s.run("tmux-server")
        c.check("tmux-server — свой, не чужой", "100", out)

        print("── ЛОЖНЫЕ СРАБАТЫВАНИЯ: заглушки и стенды проб не агенты ──")
        # L4: стенд -L (меньший pid, с заглушкой) и сервер агента (больший pid).
        s = Stand(base, "l4")
        s.tmux_server(100, sock=["-L", "probe-w1t"])
        s.stub(102, 100)
        s.tmux_server(200)
        s.add(201, 200, "bash", ["-bash"])
        s.claude(202, 201)
        rc, out, _ = s.run("status")
        c.check("L4: стенд -L и сервер агента, гнезда нет — lost;1", "lost;1", f"{word(out)};{rc}")
        rc, out, _ = s.run("restore-nest")
        c.check("L4: USR1 ровно серверу агента (больший pid), не стенду",
                "-USR1 200", s.log(s.kill_log))
        rc, out, _ = s.run("tmux-server")
        c.check("L4: tmux-server — сервер агента, не стенд -L", "200", out)

        s = Stand(base, "l4-S")
        s.tmux_server(100, sock=["-S", "/tmp/probe-sock"])
        s.claude(102, 100)
        s.tmux_server(200)
        s.claude(202, 200)
        s.run("restore-nest")
        c.check("стенд -S с настоящим claude: USR1 только серверу агента",
                "-USR1 200", s.log(s.kill_log))

        # L4: два сервера сокета по умолчанию — порядок решает только агент под
        # сервером; меньший pid пустой и первым стоять не должен.
        s = Stand(base, "l4-default")
        s.tmux_server(100)
        s.tmux_server(200)
        s.add(201, 200, "bash", ["-bash"])
        s.claude(202, 201, exe=CLAUDE_EXE_NPM)
        rc, out, _ = s.run("tmux-server")
        c.check("L4: два сервера по умолчанию — tmux-server тот, где агент", "200", out)
        s.run("restore-nest")
        c.check("L4: два сервера по умолчанию — USR1 ровно серверу с агентом",
                "-USR1 200", s.log(s.kill_log))

        # L7: только разовый claude -p под init.
        s = Stand(base, "l7")
        s.claude(300, 1, argv=["claude", "-p", "--strict-mcp-config", "x"])
        rc, out, _ = s.run("status")
        c.check("L7: только claude -p под init, has-session=1 — absent;2", "absent;2", f"{word(out)};{rc}")

        for flag in ("--version", "-v", "--print", "mcp", "update", "doctor"):
            s = Stand(base, f"oneshot{flag}")
            s.claude(300, 1, argv=["claude", flag])
            rc, out, _ = s.run("status")
            c.check(f"claude {flag} вне tmux — absent;2, не orphan", "absent;2", f"{word(out)};{rc}")

        for exe in (STUB_EXE_BASH, STUB_EXE_PY):
            s = Stand(base, f"stub-orphan-{Path(exe).name}")
            s.stub(300, 1, exe=exe)
            rc, out, _ = s.run("status")
            c.check(f"заглушка comm=claude exe={exe} вне tmux — absent;2", "absent;2", f"{word(out)};{rc}")

        # Заглушка под сервером агента рядом с настоящим агентом.
        for exe in (STUB_EXE_BASH, STUB_EXE_PY):
            s = Stand(base, f"stub-beside-{Path(exe).name}")
            s.tmux_server(100)
            s.add(101, 100, "bash", ["-bash"])
            s.claude(102, 101)
            s.add(111, 100, "bash", ["-bash"])
            s.stub(112, 111, exe=exe)
            rc, out, _ = s.run("claude-count", "--main")
            c.check(f"заглушка exe={exe} под сервером агента + агент — основных 1", "1;0", f"{out};{rc}")
            rc, out, _ = s.run("two-agents")
            c.check(f"заглушка exe={exe} рядом с агентом — two-agents rc 0", 0, rc)
            rc, out, _ = s.run("claude-count")
            c.check(f"заглушка exe={exe} — не claude вовсе (всех claude 1)", "1", out)

        # Сервер -L с настоящим claude под ним + агент под сервером по умолчанию.
        s = Stand(base, "L-real")
        s.tmux_server(100, sock=["-L", "probe-x"])
        s.claude(102, 100)
        s.tmux_server(200)
        s.claude(202, 200)
        rc, out, _ = s.run("two-agents")
        c.check("claude под сервером -L + агент — two-agents rc 0", 0, rc)
        c.check("two-agents называет только агента", {"202"}, pids(out))
        rc, out, _ = s.run("claude-count", "--main")
        c.check("claude под сервером -L — не основной (основных 1)", "1", out)

        # Сервер агента есть, агента под ним нет.
        s = Stand(base, "server-empty")
        s.tmux_server(100)
        s.add(101, 100, "bash", ["-bash"])
        rc, out, _ = s.run("status")
        c.check("сервер агента без агента, has-session=1 — absent;2", "absent;2", f"{word(out)};{rc}")

        # Только стенд -L с настоящим claude, сервера агента нет.
        s = Stand(base, "only-stand")
        s.tmux_server(100, sock=["-L", "probe-x"])
        s.claude(102, 100)
        rc, out, _ = s.run("status")
        c.check("жив только стенд -L с claude — absent;2 (подъём штатный)", "absent;2", f"{word(out)};{rc}")

        print("── ЛОЖНАЯ ТРЕВОГА 30.10.2026: образцы с живой машины ──")
        # Три тревоги «два агента» за 20 минут: вторым каждый раз был новый pid.
        # Пойман живьём (readlink /proc/8566/exe, 30.10.2026 00:49): заглушка
        # пробы test_warden_subagent_work.sh — копия /bin/cat под именем claude,
        # сессия на сокете по умолчанию, то есть прямо под сервером агента.
        stub_cat = "/tmp/warden-subwork.ysT2ZI/claude"
        s = Stand(base, "fa-cat-stub")
        s.tmux_server(100)
        s.add(101, 100, "sh", ["-sh"])
        s.claude(102, 101, argv=["claude", "--model", "claude-sonnet-5-5", "--dangerously-skip-permissions"])
        s.add(8566, 100, "claude", [stub_cat], exe=stub_cat)
        rc, out, _ = s.run("two-agents")
        c.check("ЛТ-1: копия cat под именем claude под сервером агента — two-agents rc 0", 0, rc)
        c.check("ЛТ-1: two-agents называет только агента", {"102"}, pids(out))
        rc, out, _ = s.run("claude-count")
        c.check("ЛТ-1: копия cat — не claude вовсе (всех claude 1)", "1", out)
        s = Stand(base, "fa-cat-orphan")
        s.add(8566, 1, "claude", [stub_cat], exe=stub_cat)
        rc, out, _ = s.run("status")
        c.check("ЛТ-1: копия cat под init — absent;2, не orphan", "absent;2", f"{word(out)};{rc}")

        # Разовый claude -p демона, поднятый окном в tmux агента, и он же под cron.
        s = Stand(base, "fa-demon-p")
        s.tmux_server(100)
        s.add(101, 100, "sh", ["-sh"])
        s.claude(102, 101)
        s.claude(120, 100, argv=["claude", "-p", "--strict-mcp-config", "проверь"])
        s.add(130, 1, "cron", ["/usr/sbin/cron", "-f"], uid=0)
        s.add(131, 130, "sh", ["/bin/sh", "-c", "harness/demons/ton-watch.sh"])
        s.add(132, 131, "bash", ["bash", "harness/demons/ton-watch.sh"])
        s.claude(133, 132, argv=["claude", "-p", "--strict-mcp-config", "x"])
        rc, out, _ = s.run("two-agents")
        c.check("ЛТ-2: claude -p в окне tmux агента и под cron — two-agents rc 0", 0, rc)
        c.check("ЛТ-2: основной один — агент", {"102"}, pids(out))

        # Неразовый claude под cron без tmux: не смена.
        s = Stand(base, "fa-cron")
        s.tmux_server(100)
        s.claude(102, 100)
        s.add(130, 1, "cron", ["/usr/sbin/cron", "-f"], uid=0)
        s.add(131, 130, "sh", ["/bin/sh", "-c", "demon.sh"])
        s.claude(132, 131)
        rc, out, _ = s.run("two-agents")
        c.check("ЛТ-3: claude под cron + агент — two-agents rc 0", 0, rc)

        # Проба, запущенная из панели агента, поднимает настоящий claude:
        # родитель — скрипт пробы, не оболочка панели и не сервер.
        s = Stand(base, "fa-probe-in-pane")
        s.tmux_server(100)
        s.add(101, 100, "sh", ["-sh"])
        s.claude(102, 101)
        s.add(111, 100, "sh", ["-sh"])
        s.add(112, 111, "bash", ["bash", "scripts/test_x.sh"])
        s.claude(113, 112)
        rc, out, _ = s.run("two-agents")
        c.check("ЛТ-4: claude под скриптом пробы в панели агента — не основной (rc 0)", 0, rc)
        c.check("ЛТ-4: основной один — агент", {"102"}, pids(out))

        # Настоящая вторая смена: оболочка второй панели того же сервера.
        s = Stand(base, "fa-real-second")
        s.tmux_server(100)
        s.add(101, 100, "sh", ["-sh"])
        s.claude(102, 101)
        s.add(111, 100, "bash", ["-bash"])
        s.claude(112, 111)
        rc, out, _ = s.run("two-agents")
        c.check("ЛТ-5: второй claude в оболочке панели сервера агента — тревога (rc 1)", 1, rc)
        c.check("ЛТ-5: two-agents называет обоих", {"102", "112"}, pids(out))

        print("── настоящий claude во всех формах exe ──")
        for name, exe in (("npm (живая форма)", CLAUDE_EXE_NPM), ("родной установщик", CLAUDE_EXE_NATIVE),
                          ("npm после обновления (deleted)", CLAUDE_EXE_NPM + " (deleted)")):
            s = Stand(base, "exe-" + name.split()[0] + str(len(exe)))
            s.tmux_server(100)
            s.claude(102, 100, exe=exe)
            rc, out, _ = s.run("claude-count", "--main")
            c.check(f"exe {name}: claude под сервером агента — основной", "1", out)
            rc, out, _ = s.run("status")
            c.check(f"exe {name}: гнезда нет — lost;1", "lost;1", f"{word(out)};{rc}")

    print(f"\nАГЕНТЫ И ГНЕЗДО: путей {c.paths}, неудач {c.failures}")
    return 0 if c.ok else 1


if __name__ == "__main__":
    sys.exit(main())
