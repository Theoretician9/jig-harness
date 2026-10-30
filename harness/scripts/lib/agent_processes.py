#!/usr/bin/env python3
"""Кто из процессов claude — основной агент, и жив ли у него сокет tmux.

Зачем. Сокет tmux удалили при живом сервере; сторож спросил has-session,
услышал «нет» и поднял ВТОРОЙ сервер со второй сменой (отчёт другого сервера,
сверка 29.10.2026, п.1–2). Судье нужно дерево процессов: оно отличает
«гнездо пропало» от «агента нет» и считает основных агентов, не путая их с
подагентами и разовыми claude -p.

Окружение (все двери — для проб; без них берётся настоящая машина):
  AGENT_PROC_ROOT  корень дерева процессов, по умолчанию /proc
  AGENT_MAINPID    MainPID юнита; не задан — спрашивается внешний
                   `systemctl show -p MainPID --value $AGENT_UNIT`
  AGENT_UNIT       юнит агента, по умолчанию harness-agent
  AGENT_USER       чьи процессы считаются агентскими, по умолчанию текущий
  TMUX_SESSION     имя сессии для has-session, по умолчанию agent

Определения.
  настоящий claude  процесс, чей <pid>/exe указывает на бинарь claude: имя
                  claude.exe (npm) либо путь содержит /claude/ (родной установщик).
                  Имя ничего не доказывает — ни comm, ни имя файла: заглушка пробы
                  бывает копией /bin/cat по пути …/claude (живой образец 30.10.2026).
  tmux-сервер агента  процесс пользователя агента «tmux: server» БЕЗ -L/-S в
                  cmdline, то есть на сокете по умолчанию. Сервер на своём
                  сокете (-L/-S) — чужой: пробы и отчёты живут там.
  точка запуска   сервер агента либо MainPID юнита.
  основной агент  настоящий неразовый claude пользователя агента без предка
                  claude: сам MainPID либо поднятый точкой запуска — родитель
                  она сама или корень панели (оболочка) прямо под ней.
                  Так его поднимают start-agent.sh и юнит. Глубже — скрипт пробы
                  или демона в панели, и это не смена. Подагент — claude под
                  claude; claude под сервером -L/-S основным не бывает.

CLI (код возврата — в скобках):
  mainpid                 печатает MainPID (0 — нет)
  tmux-server             pid сервера агента; сервера нет — пусто (1)
  claude-list [--main]    pid'ы claude пользователя агента (--main: только основные)
  claude-count [--main]   их число
  status                  печатает слово, код возврата ему соответствует: ok(0) lost(1) absent(2) orphan(3)
                            ok     гнездо отвечает на has-session
                            lost   гнездо не отвечает, а под сервером агента жив основной агент
                            orphan сервера нет, а настоящий claude вне tmux жив и не разовый
                                   (в cmdline нет -p/--print/--version/-v/mcp/update/doctor)
                            absent иначе: штатный подъём
  restore-nest            kill -USR1 серверу агента с основным агентом под ним:
                          tmux пересоздаёт сокет; такого сервера нет — отказ (1),
                          kill не зовётся
  unit-agents             pid основных агентов самого юнита (MainPID или его потомок);
                          MainPID=0 — пусто
  two-agents              печатает pid основных агентов; (1) при двух и более

Импорт: main_agents(), main_pid(), tmux_servers(), ancestors(pid),
claude_pids(), nest_status(). kill, systemctl и tmux зовутся только внешними
программами через PATH — не os.kill: пробе нужна заглушка, а настоящий сигнал
не должен уйти чужому процессу.
"""
from __future__ import annotations

import os
import pwd
import subprocess
import sys
from pathlib import Path

TMUX_COMM = "tmux: server"
# Слова статуса и их коды возврата — одной таблицей: CLI и сторож читают её же.
STATUS_CODES = {"ok": 0, "lost": 1, "absent": 2, "orphan": 3}


def proc_root() -> Path:
    return Path(os.environ.get("AGENT_PROC_ROOT") or "/proc")


def agent_uid() -> int:
    name = os.environ.get("AGENT_USER")
    if not name:
        return os.getuid()
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        # Неизвестный пользователь не должен молча стать «текущим»: пустой счёт
        # честнее счёта чужих процессов.
        return -1


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def snapshot() -> dict:
    """pid -> {ppid, comm, uid, argv, exe} для всех читаемых процессов."""
    procs = {}
    try:
        entries = list(proc_root().iterdir())
    except OSError:
        return procs
    for d in entries:
        if not d.name.isdigit():
            continue
        status = _read(d / "status")
        ppid, uid = 0, -2
        for line in status.splitlines():
            if line.startswith("PPid:"):
                ppid = int(line.split()[1])
            elif line.startswith("Uid:"):
                uid = int(line.split()[1])
        if uid == -2:
            continue  # процесс успел умереть между листингом и чтением
        argv = [a for a in _read(d / "cmdline").split("\0") if a]
        try:
            exe = os.readlink(d / "exe")
        except OSError:
            exe = ""
        procs[int(d.name)] = {"ppid": ppid, "comm": _read(d / "comm").strip(),
                              "uid": uid, "argv": argv, "exe": exe}
    return procs


def ancestors(pid: int, procs: dict | None = None) -> list:
    """Предки pid от ближайшего к init, без самого pid."""
    procs = snapshot() if procs is None else procs
    chain, seen = [], {pid}
    cur = procs.get(pid, {}).get("ppid", 0)
    while cur > 0 and cur not in seen:
        chain.append(cur)
        seen.add(cur)
        cur = procs.get(cur, {}).get("ppid", 0)
    return chain


def main_pid() -> int:
    env = os.environ.get("AGENT_MAINPID")
    if env is not None:
        return int(env) if env.isdigit() else 0
    unit = os.environ.get("AGENT_UNIT") or "harness-agent"
    try:
        out = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", unit],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 0
    return int(out) if out.isdigit() else 0


def _own(p: dict) -> bool:
    return p["uid"] == agent_uid()


def _is_claude(p: dict) -> bool:
    exe = p["exe"].removesuffix(" (deleted)")  # бинарь заменили обновлением, процесс жив
    return Path(exe).name == "claude.exe" or "/claude/" in exe


def _is_tmux_server(p: dict) -> bool:
    return p["comm"] == TMUX_COMM or (bool(p["argv"]) and p["argv"][0].startswith(TMUX_COMM))


def _own_socket(p: dict) -> bool:
    """-L/-S в cmdline: сервер на своём сокете, не на сокете агента."""
    return any(a[:2] in ("-L", "-S") for a in p["argv"][1:])


def _is_agent_server(p: dict) -> bool:
    return _is_tmux_server(p) and not _own_socket(p)


def claude_pids(procs: dict | None = None) -> list:
    procs = snapshot() if procs is None else procs
    return sorted(pid for pid, p in procs.items() if _own(p) and _is_claude(p))


def _launched(pid: int, procs: dict, mainpid: int) -> bool:
    """Сам MainPID, ребёнок точки запуска или ребёнок корня панели под ней:
    туда start-agent.sh шлёт команду запуска."""
    def launcher(q: int) -> bool:
        return q > 0 and (q == mainpid or (q in procs and _own(procs[q]) and _is_agent_server(procs[q])))
    parent = procs[pid]["ppid"]
    return pid == mainpid or launcher(parent) or launcher(procs.get(parent, {}).get("ppid", 0))


def main_agents(procs: dict | None = None) -> list:
    procs = snapshot() if procs is None else procs
    mainpid = main_pid()
    return [pid for pid in claude_pids(procs)
            if not _oneshot(procs[pid])
            and not any(_is_claude(procs[a]) for a in ancestors(pid, procs) if a in procs)
            and _launched(pid, procs, mainpid)]


def unit_agents(procs: dict | None = None) -> list:
    """Основные агенты юнита: сам MainPID или его потомок. MainPID=0 — никого."""
    procs = snapshot() if procs is None else procs
    mainpid = main_pid()
    if mainpid <= 0:
        return []
    return [pid for pid in main_agents(procs) if pid == mainpid or mainpid in ancestors(pid, procs)]


def tmux_servers(procs: dict | None = None) -> list:
    """Серверы агента (сокет по умолчанию); впереди тот, под которым живёт основной агент."""
    procs = snapshot() if procs is None else procs
    servers = sorted(pid for pid, p in procs.items() if _own(p) and _is_agent_server(p))
    agents_servers = {a for pid in main_agents(procs) for a in ancestors(pid, procs)}
    return sorted(servers, key=lambda s: s not in agents_servers)


def nest_servers(procs: dict | None = None) -> list:
    """Серверы агента с основным агентом под ними: только им имеет смысл слать USR1."""
    procs = snapshot() if procs is None else procs
    agents_servers = {a for pid in main_agents(procs) for a in ancestors(pid, procs)}
    return [s for s in tmux_servers(procs) if s in agents_servers]


# Разовые вызовы: не смена, а команда на минуту.
ONESHOT_ARGS = {"-p", "--print", "--version", "-v", "mcp", "update", "doctor"}


def _oneshot(p: dict) -> bool:
    return bool(ONESHOT_ARGS.intersection(p["argv"][1:]))


def _has_session() -> bool:
    session = os.environ.get("TMUX_SESSION") or "agent"
    try:
        return subprocess.run(["tmux", "has-session", "-t", session],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def nest_status() -> str:
    if _has_session():
        return "ok"
    procs = snapshot()
    if nest_servers(procs):
        return "lost"
    for pid in claude_pids(procs):
        chain = [procs[a] for a in ancestors(pid, procs) if a in procs]
        if not _oneshot(procs[pid]) and not any(_is_claude(a) or _is_tmux_server(a) for a in chain):
            return "orphan"
    return "absent"


def _flag_main(args: list) -> bool:
    return "--main" in args


def cmd_restore_nest() -> int:
    servers = nest_servers()
    if not servers:
        print("сервера агента с основным агентом нет: сигнал слать некому", file=sys.stderr)
        return 1
    return subprocess.run(["kill", "-USR1", str(servers[0])]).returncode


def cli(argv: list) -> int:
    cmd, args = (argv[0] if argv else ""), argv[1:]
    if cmd == "mainpid":
        print(main_pid())
    elif cmd == "tmux-server":
        servers = tmux_servers()
        if servers:
            print(servers[0])
        return 0 if servers else 1
    elif cmd in ("claude-list", "claude-count"):
        pids = main_agents() if _flag_main(args) else claude_pids()
        print(" ".join(map(str, pids)) if cmd == "claude-list" else len(pids))
    elif cmd == "status":
        word = nest_status()
        print(word)
        return STATUS_CODES[word]
    elif cmd == "restore-nest":
        return cmd_restore_nest()
    elif cmd == "unit-agents":
        print(" ".join(map(str, unit_agents())))
    elif cmd == "two-agents":
        agents = main_agents()
        print(" ".join(map(str, agents)))
        return 1 if len(agents) >= 2 else 0
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
