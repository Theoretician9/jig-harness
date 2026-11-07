"""process_load.py — доли загрузки сервера по процессам харнеса (hr.process-dispatcher §В.1, §В.4).

Принцип: cgroup даёт точный итог, дерево процессов его только делит.
Модуль чистый: читает /proc и cgroup из названных каталогов (двери проб —
снимки), состояние прошлого прохода получает аргументом и отдаёт новое. Файлы,
замки, журнал и показ — в scripts/process-dispatcher.py.

Командные строки процессов наружу не выходят: в аргументах бывают секреты, в
группы идут id библиотеки, имя файла корня крона и comm.
"""
from __future__ import annotations

import collections
import datetime as dt
import fcntl
import json
import os
import re
import shlex
from pathlib import Path

TICK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")

# Юниты, чей cgroup делится по дереву процессов: путь целиком, не хвост, потому
# что внутри LXC стоят свои system.slice/harness-agent.service.
SPLIT_UNITS = {"/system.slice/cron.service": "cron",
               "/system.slice/harness-agent.service": "agent",
               "/system.slice/harness-dispatcher.service": "dispatcher"}
# Потоки ядра и всё без юнита: счётчик корня равен всей машине, делить им нельзя.
ROOT_CGROUP = "/"
CRON_NAMES = ("cron", "CRON", "crond")
ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=")
MAX_GROUPS = 80
TINY_CPU_S = 0.05
TINY_RSS_MB = 20
TRANSCRIPT_AGE_S = 15 * 60
TRANSCRIPT_TAIL = 256 * 1024
RUN_FILES = ("process-load", "process-runs")
UNDETERMINED = "не определён"
UNDETERMINED_RETRY_S = 600


def read_text(path, default: str = "") -> str:
    try:
        with open(path, "rb") as handle:                  # без TextIOWrapper: на 300 процессов это тысячи чтений
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return default


# ───────────────────────── правила из библиотеки ─────────────────────────

def load_rules(library: dict) -> dict:
    """Признаки записей со способом «cgroup» → таблицы поиска.
    prefix: путь cgroup → id (юнит или LXC целиком); unit_ids: id делимых юнитов;
    tokens: слово из argv → id (docker-scope без известного пути); scripts:
    имя скрипта порождения службы → id."""
    rules = {"prefix": {}, "unit_ids": {}, "tokens": {}, "scripts": {}}
    for record in library.get("процессы", []):
        sign = (record.get("доля_нагрузки") or {}).get("приписка") or {}
        if sign.get("способ") != "cgroup":
            continue
        head, _, tail = str(sign.get("признак", "")).partition(",")
        head, tail = head.strip(), tail.strip()
        if head.endswith(".service"):
            path = "/system.slice/" + head
            if tail.startswith("cmdline "):
                rules["scripts"][os.path.basename(tail.split()[1])] = record["id"]
            elif path in SPLIT_UNITS:
                rules["unit_ids"][path] = record["id"]
            else:
                rules["prefix"][path] = record["id"]
        elif head.startswith("lxc."):
            rules["prefix"]["/" + head] = record["id"]
        else:
            for word in re.findall(r"\(([^)]+)\)", head):
                rules["tokens"][word] = record["id"]
    return rules


# ───────────────────────── снимок /proc и cgroup ─────────────────────────

def wants_argv(cgroup: str, rules: dict) -> bool:
    """Командная строка нужна делимым юнитам и корзинам без пути в библиотеке (слово из argv)."""
    return cgroup in SPLIT_UNITS or not any(cgroup == p or cgroup.startswith(p + "/") for p in rules["prefix"])


def read_procs(proc_dir, rules: dict) -> dict:
    procs = {}
    for name in os.listdir(proc_dir):
        if not name.isdigit():
            continue
        base = os.path.join(proc_dir, name)
        try:
            stat = read_text(os.path.join(base, "stat"))
            # comm может содержать пробелы и скобки: поля считаются от последней «)».
            tail = stat[stat.rindex(")") + 2:].split()
            comm = stat[stat.index("(") + 1:stat.rindex(")")]
            statm = read_text(os.path.join(base, "statm")).split()
            cgroup = read_text(os.path.join(base, "cgroup")).partition("::")[2].strip()
            argv = [a for a in read_text(os.path.join(base, "cmdline")).split("\0") if a] \
                if wants_argv(cgroup, rules) else []
            procs[int(name)] = {
                "pid": int(name), "ppid": int(tail[1]), "sid": int(tail[3]), "comm": comm, "argv": argv,
                "cg": cgroup, "start": int(tail[19]), "own": int(tail[11]) + int(tail[12]),
                "kids": int(tail[13]) + int(tail[14]),
                "rss": int(statm[1]) * PAGE / 1048576 if len(statm) > 1 else 0.0}
        except (OSError, ValueError, IndexError):
            continue                        # процесс исчез между listdir и чтением
    return procs


def cgroup_usage(cg_dir, path: str):
    text = read_text(os.path.join(cg_dir, path.strip("/"), "cpu.stat"))
    match = re.search(r"^usage_usec (\d+)", text, re.M)
    return int(match.group(1)) / 1e6 if match else None


def bucket_of(cgroup: str, procs: list, rules: dict) -> tuple:
    """(id, путь корзины) неделимой корзины: самый длинный префикс из библиотеки,
    затем слово из argv, иначе имя хвоста пути без id библиотеки."""
    best = max((p for p in rules["prefix"] if cgroup == p or cgroup.startswith(p + "/")), key=len, default=None)
    if best:
        return rules["prefix"][best], best
    for proc in procs:
        for word, ident in rules["tokens"].items():
            if any(word in arg for arg in proc["argv"]):
                return ident, cgroup
    return cgroup.rstrip("/").rsplit("/", 1)[-1] or "/", cgroup


def scan(proc_dir, cg_dir, rules: dict) -> dict:
    procs = read_procs(proc_dir, rules)
    by_cgroup = collections.defaultdict(list)
    for proc in procs.values():
        by_cgroup[proc["cg"]].append(proc)
    buckets = {cg: bucket_of(cg, members, rules) for cg, members in by_cgroup.items() if cg not in SPLIT_UNITS and cg != ROOT_CGROUP}
    paths = set(SPLIT_UNITS) | {path for _, path in buckets.values()}
    top = {}
    for entry in sorted(os.listdir(cg_dir)):
        if os.path.isfile(os.path.join(cg_dir, entry, "cpu.stat")):
            top[entry] = cgroup_usage(cg_dir, "/" + entry)
    machine = read_text(os.path.join(proc_dir, "stat")).splitlines()
    fields = [int(x) for x in machine[0].split()[1:]]
    # user + nice + system + irq + softirq + steal: счётчик cgroup считает время работы задач, а на
    # виртуалке в него входит и украденное гипервизором (замер 06.11: ≈0,25 ядра), без steal
    # «вне корзин» уходит в минус. idle и iowait — не работа.
    busy = (fields[0] + fields[1] + fields[2] + fields[5] + fields[6] + fields[7]) / TICK
    return {"uptime": float(read_text(os.path.join(proc_dir, "uptime")).split()[0]), "busy": busy,
            "cores": sum(1 for line in machine if re.match(r"cpu\d+ ", line)), "procs": procs,
            "buckets": buckets, "top": top,
            "cgroups": {path: cgroup_usage(cg_dir, path) for path in paths}}


# ───────────────────────── разметка процессов ─────────────────────────

def wrapper_id(argv: list):
    for i in (0, 1):
        if len(argv) > i + 1 and os.path.basename(argv[i]) == "run-background.sh":
            return argv[i + 1]
    return None


def command_of(argv: list):
    """Команда инструмента Bash из подписи `bash -c 'source …shell-snapshots… && eval <команда> …'`."""
    if len(argv) < 3 or os.path.basename(argv[0]) not in ("bash", "zsh") or argv[1] != "-c" \
            or not argv[2].startswith("source ") or "shell-snapshots" not in argv[2]:
        return None
    try:
        words = shlex.split(argv[2])
    except ValueError:
        return None
    return words[words.index("eval") + 1] if "eval" in words[:-1] else ""


class Transcripts:
    """Хвосты свежих транскриптов, прочитанные один раз за проход и только по требованию."""

    def __init__(self, directory, now: float):
        self.directory, self.now, self._tails = Path(directory) if directory else None, now, None

    def tails(self) -> list:
        if self._tails is None:
            self._tails = []
            found = list(self.directory.glob("*.jsonl")) + list(self.directory.glob("*/subagents/agent-*.jsonl")) \
                if self.directory and self.directory.is_dir() else []
            for path in found:
                try:
                    if self.now - path.stat().st_mtime > TRANSCRIPT_AGE_S:
                        continue
                    with open(path, "rb") as handle:
                        handle.seek(max(0, path.stat().st_size - TRANSCRIPT_TAIL))
                        text = handle.read().decode("utf-8", "replace")
                except OSError:
                    continue
                self._tails.append((path, text.splitlines()))
        return self._tails

    def owner_of_command(self, command: str):
        """Чей открытый tool_use с этой командой: файл основной сессии или агента."""
        for path, lines in self.tails():
            done = {m for line in lines if '"tool_result"' in line
                    for m in re.findall(r'"tool_use_id"\s*:\s*"([^"]+)"', line)}
            for line in lines:
                if '"tool_use"' not in line:
                    continue
                try:
                    content = json.loads(line).get("message", {}).get("content", [])
                except ValueError:
                    continue
                for item in content if isinstance(content, list) else []:
                    if item.get("type") == "tool_use" and item.get("id") not in done and str(
                            (item.get("input") or {}).get("command", "")).strip() == command.strip():
                        return path
        return None

    def owner_of_background(self, task_id: str):
        for path, lines in self.tails():
            if any("backgroundTaskId" in line and task_id in line for line in lines):
                return path
        return None


def command_owner(proc: dict, proc_dir, session_label: tuple, transcripts: Transcripts) -> tuple:
    """Корень команды Bash: (а) открытый tool_use, (б) backgroundTaskId из пути stdout, (в) не определён."""
    path = transcripts.owner_of_command(command_of(proc["argv"]) or "")
    if path is None:
        try:
            task_id = Path(os.readlink(os.path.join(proc_dir, str(proc["pid"]), "fd", "1"))).stem
            path = transcripts.owner_of_background(task_id)
        except OSError:
            path = None
    if path is None:
        return ("helpers", None, UNDETERMINED)
    if path.name.startswith("agent-"):
        return ("helpers", None, path.stem[len("agent-"):])
    return session_label


def has_session_env(proc_dir, pid: int) -> bool:
    try:
        return b"CLAUDE_CODE_SESSION_ID=" in open(os.path.join(proc_dir, str(pid), "environ"), "rb").read()
    except OSError:
        return False


def bare_name(argv: list) -> str:
    """Имя файла корня крона без обёртки: basename первого слова-пути в `sh -c '<строка>'` или
    `bash <файл>`. Присваивания `ИМЯ=…` и `env` пропускаются, а слова без «/» не берутся вовсе: в них
    бывают секреты. Пути в строке нет — имя интерпретатора."""
    try:
        words = shlex.split(argv[2]) if len(argv) > 2 and argv[1] == "-c" else argv[1:]
    except ValueError:
        words = []
    path = next((w for w in words if "/" in w and not w.endswith("/") and not w.startswith("-")
                 and not ASSIGNMENT.match(w)), None)
    return os.path.basename(path or argv[0])


def argv_base(proc: dict) -> str:
    return os.path.basename(proc["argv"][0]) if proc["argv"] else ""


def proc_key(proc: dict) -> str:
    return f"{proc['pid']}:{proc['start']}"


def label_procs(snap: dict, prev_procs: dict, rules: dict, proc_dir, transcripts: Transcripts) -> dict:
    """pid → ярлык (id, прогон, агент) или None, только для процессов делимых юнитов."""
    procs = {pid: p for pid, p in snap["procs"].items() if p["cg"] in SPLIT_UNITS}
    kind = {pid: SPLIT_UNITS[p["cg"]] for pid, p in procs.items()}
    unit_name = {pid: p["cg"].rsplit("/", 1)[-1] for pid, p in procs.items()}
    roots, by_sid = {}, {}

    def make_root(pid, label, sid_too=True):
        roots[pid] = label
        if sid_too:
            by_sid[procs[pid]["sid"]] = label

    for pid, p in procs.items():                       # 1. обёртки: id из argv, точно
        ident = wrapper_id(p["argv"])
        if ident:
            make_root(pid, (ident, str(pid), None))
    for pid, p in procs.items():                       # 2. остальные корни по юниту
        if pid in roots:
            continue
        argv0 = argv_base(p)
        # Корень крона — только ребёнок самого cron: внук с setsid тоже имеет sid = pid, но он
        # наследует ярлык задания по правилам ниже.
        if kind[pid] == "cron" and p["sid"] == pid and procs.get(p["ppid"], {}).get("comm") in CRON_NAMES \
                and argv0 not in CRON_NAMES and pid not in by_sid:
            make_root(pid, ("крон:" + bare_name(p["argv"])[:60], str(pid), None))
        elif kind[pid] == "agent" and argv0 == "claude" and "-p" not in p["argv"] and "--print" not in p["argv"]:
            make_root(pid, (rules["unit_ids"].get(p["cg"], "claude-session"), unit_name[pid], None), False)
        elif kind[pid] == "agent" and p["sid"] == pid and command_of(p["argv"]) is not None \
                and p["ppid"] in procs and argv_base(procs[p["ppid"]]) == "claude":
            remembered = (prev_procs.get(proc_key(p)) or {}).get("label")
            young = snap["uptime"] - p["start"] / TICK < UNDETERMINED_RETRY_S
            if remembered and remembered[2] == UNDETERMINED and young:
                remembered = None                      # backgroundTaskId появляется в транскрипте позже старта: допытываемся
            session = (rules["unit_ids"].get(p["cg"], "claude-session"), unit_name[pid], None)
            make_root(pid, tuple(remembered) if remembered else command_owner(p, proc_dir, session, transcripts))
        elif kind[pid] == "dispatcher" and p["sid"] == pid and any(
                os.path.basename(a) == "tg-dispatcher.sh" for a in procs.get(p["ppid"], {}).get("argv", [])):
            ident = next((rules["scripts"][os.path.basename(a)] for a in p["argv"]
                          if os.path.basename(a) in rules["scripts"]), None)
            if ident:
                make_root(pid, (ident, str(pid), None))

    labels = {}

    def label(pid, seen=()):
        if pid in labels:
            return labels[pid]
        p = procs[pid]
        found = roots.get(pid) or by_sid.get(p["sid"])
        if found is None and p["ppid"] in procs and p["ppid"] not in seen:
            found = label(p["ppid"], seen + (pid,))
        if found is None and (prev_procs.get(proc_key(p)) or {}).get("label"):
            found = tuple(prev_procs[proc_key(p)]["label"])
        if found is None and kind[pid] == "agent" and has_session_env(proc_dir, pid):
            found = ("claude-session-commands", unit_name[pid], None)
        if found is None and kind[pid] == "dispatcher":
            found = (rules["unit_ids"].get(p["cg"], "harness-dispatcher"), unit_name[pid], None)
        labels[pid] = found
        return found

    return {pid: label(pid) for pid in procs}


def cpu_seconds(snap: dict, prev_procs: dict, ends: tuple = ()) -> tuple:
    """(pid → CPU процесса за окно, [(pid родителя, ярлык, cgroup ушедшего ребёнка, с)]).
    CPU процесса = Δ(utime+stime) + Δ(cutime+cstime) за вычетом уже учтённого у всего ушедшего поддерева
    (own+kids каждого потомка, не только прямого ребёнка). Хвост — то, что родитель дождался сверх
    учтённого, — принадлежит ярлыкам ушедших; он есть только у родителя с известным прошлым значением:
    у остальных нет базы вычитания, и весь накопленный cutime стал бы «хвостом». Один ярлык берёт хвост
    целиком, несколько делятся по process.end (`ends`: (id, pid, cpu_с)); неразделённое остаётся родителю."""
    live = {proc_key(p) for p in snap["procs"].values()}
    gone = {int(key.split(":")[0]): old for key, old in prev_procs.items() if key not in live}
    children = collections.defaultdict(list)
    for pid, old in gone.items():
        children[old["ppid"]].append(pid)

    def subtree(pid: int):
        yield pid
        for child in children[pid]:
            yield from subtree(child)

    ended = {(ident, str(pid)): seconds for ident, pid, seconds in ends}
    result, tails = {}, []
    for pid, p in snap["procs"].items():
        old = prev_procs.get(proc_key(p))
        if old is None:
            result[pid] = (p["own"] + p["kids"]) / TICK
            continue
        members = [m for child in children[pid] for m in subtree(child)]
        accounted, cgroup = collections.Counter(), {}
        for m in members:
            label = gone[m].get("label")
            if label and gone[m].get("cg"):
                accounted[tuple(label)] += gone[m]["own"] + gone[m]["kids"]
                cgroup[tuple(label)] = gone[m]["cg"]
        kids = max(0, p["kids"] - old["kids"] - sum(gone[m]["own"] + gone[m]["kids"] for m in members)) / TICK
        result[pid] = (max(0, p["own"] - old["own"]) / TICK) + kids
        if len(accounted) == 1:
            shares = dict.fromkeys(accounted, kids)
        else:
            shares = {label: max(0.0, ended.get(label[:2], 0.0) - ticks / TICK) for label, ticks in accounted.items()}
            total = sum(shares.values())
            if total > kids:
                shares = {label: seconds * kids / total for label, seconds in shares.items()}
        tails += [(pid, label, cgroup[label], seconds) for label, seconds in shares.items() if seconds > 0]
    return result, tails


def cap_to_cgroup(snap: dict, prev: dict, labels: dict, cpu: dict, inherited: collections.Counter) -> dict:
    """Дерево только делит итог cgroup: CPU детей, ушедших в чужой cgroup (systemd-run, sudo), дожидает
    родитель, но в счётчик юнита он не входит. Если сумма (живые + унаследованный хвост ушедших) выше дельты
    юнита, доли ужимаются пропорционально; величина ужатия в секундах возвращается и пишется в строку
    выборки: двойной счёт не прячем."""
    shrunk = {}
    for path in SPLIT_UNITS:
        total = delta(snap["cgroups"].get(path), (prev.get("cgroups") or {}).get(path))
        pids = [pid for pid in labels if snap["procs"][pid]["cg"] == path]
        tails = [key for key in inherited if key[1] == path]
        spent = sum(cpu[pid] for pid in pids) + sum(inherited[key] for key in tails)
        if spent > total + 1e-9:
            for pid in pids:
                cpu[pid] *= total / spent
            for key in tails:
                inherited[key] *= total / spent
            shrunk[path.rsplit("/", 1)[-1]] = round(spent - total, 3)
    return shrunk


def delta(now, before):
    if now is None:
        return 0.0
    return now if before is None or now < before else now - before


def fold(groups: list) -> list:
    """Мелочь и всё сверх потолка уходит в «прочее»: CPU слитого не теряется."""
    ranked = sorted(groups, key=lambda g: (-g["cpu_с"], -g["rss_мб"]))
    big = [g for g in ranked if g["cpu_с"] >= TINY_CPU_S or g["rss_мб"] >= TINY_RSS_MB]
    tiny = [g for g in ranked if g not in big]
    if len(big) + bool(tiny) > MAX_GROUPS:
        tiny += big[MAX_GROUPS - 1:]
        big = big[:MAX_GROUPS - 1]
    if tiny:
        big.append({"id": "прочее", "cpu_с": round(sum(g["cpu_с"] for g in tiny), 3),
                    "rss_мб": round(sum(g["rss_мб"] for g in tiny), 1),
                    "процессов": sum(g["процессов"] for g in tiny), "слито": len(tiny)})
    return big


def attribute(prev, snap: dict, rules: dict, proc_dir, transcripts: Transcripts, ends: tuple = ()):
    """(data для process.sample или None, состояние для следующего прохода).
    None — первого прохода нет с чем сравнить, либо время пошло назад (перезагрузка).
    ends — (id, pid, cpu_с) process.end окна: по ним делится хвост нескольких ушедших ярлыков."""
    prev = prev or {}
    labels = label_procs(snap, prev.get("procs", {}), rules, proc_dir, transcripts)
    cpu, tails = cpu_seconds(snap, prev.get("procs", {}), ends)
    inherited = collections.Counter()                  # хвост ушедших заданий: (ярлык, cgroup ребёнка) → с
    for pid, label, cgroup, seconds in tails:
        cpu[pid] = max(0.0, cpu[pid] - seconds)
        inherited[(label, cgroup)] += seconds
    shrunk = cap_to_cgroup(snap, prev, labels, cpu, inherited)
    # Родители отслеживаемых процессов (в том числе pid 1 у сирот) тоже запоминаются: без прошлого
    # значения у родителя хвост ушедшего ребёнка не отделить от всего накопленного cutime.
    parents = {snap["procs"][pid]["ppid"] for pid in labels}
    tracked = {pid: labels.get(pid) for pid, p in snap["procs"].items()
               if pid in labels or p["cg"] == ROOT_CGROUP or pid in parents}
    state = {"uptime": snap["uptime"], "busy": snap["busy"], "cgroups": snap["cgroups"], "top": snap["top"],
             "procs": {proc_key(snap["procs"][pid]): {
                 "own": snap["procs"][pid]["own"], "kids": snap["procs"][pid]["kids"],
                 "ppid": snap["procs"][pid]["ppid"], "cg": snap["procs"][pid]["cg"],
                 "label": list(label) if label else None}
                 for pid, label in tracked.items()}}
    window = snap["uptime"] - prev.get("uptime", snap["uptime"])
    if window <= 0:
        return None, state
    groups: dict = {}
    split_cpu = collections.Counter()
    for pid, p in snap["procs"].items():
        if p["cg"] == ROOT_CGROUP:
            ident, run, agent, add = "cgroup-корень", None, None, cpu[pid]
        elif pid in labels:
            unit = p["cg"].rsplit("/", 1)[-1]
            ident, run, agent = labels[pid] or (unit + ": неразмечено", unit, None)
            split_cpu[unit] += cpu[pid]
            add = cpu[pid]
        else:
            ident, path = snap["buckets"][p["cg"]]
            run, agent, add = path.rsplit("/", 1)[-1], None, 0.0
        group = groups.setdefault((ident, run, agent), {"cpu": 0.0, "rss": 0.0, "n": 0})
        group["cpu"] += add
        group["rss"] += p["rss"]
        group["n"] += 1
    for (label, cgroup), seconds in inherited.items():
        groups.setdefault(label, {"cpu": 0.0, "rss": 0.0, "n": 0})["cpu"] += seconds
        split_cpu[cgroup.rsplit("/", 1)[-1]] += seconds
    for ident, path in set(snap["buckets"].values()):
        groups[(ident, path.rsplit("/", 1)[-1], None)]["cpu"] += delta(
            snap["cgroups"][path], (prev.get("cgroups") or {}).get(path))
    out = []
    for (ident, run, agent), g in groups.items():
        row = {"id": ident, "прогон": run} if run else {"id": ident}
        if agent:
            row["агент"] = agent
        out.append({**row, "cpu_с": round(g["cpu"], 3) + 0.0, "rss_мб": round(g["rss"], 1), "процессов": g["n"]})
    rest = {unit: round(delta(snap["cgroups"][path], (prev.get("cgroups") or {}).get(path)) - split_cpu[unit], 3) + 0.0
            for path, unit in ((p, p.rsplit("/", 1)[-1]) for p in SPLIT_UNITS) if snap["cgroups"].get(path) is not None}
    machine = snap["busy"] - prev["busy"]
    rest["вне_корзин"] = round(machine - sum(
        delta(now, (prev.get("top") or {}).get(name)) for name, now in snap["top"].items()), 3) + 0.0
    return {"окно_с": round(window, 1), "ядер": snap["cores"], "машина_cpu_с": round(machine, 2),
            "группы": fold(out), "остаток": rest, "ужатие": shrunk}, state


# ───────────────────────── журнал: чтение и свёртка ─────────────────────────

def rotate(path: str, max_mb: float) -> bool:
    """Протокол ротации, которого ждёт events._append_line: flock живого файла →
    rename в .1 → отпустить. Писатель после своего замка сверяет inode и пишет в новый файл."""
    try:
        if os.stat(path).st_size <= max_mb * 1048576:
            return False
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.rename(path, path + ".1")
    finally:
        os.close(fd)
    return True


def read_lines(logs, name: str = "process-load") -> list:
    """Строки журнала: архив .1 первым, затем живой файл."""
    base = os.path.join(logs, name + ".jsonl")
    return read_text(base + ".1").splitlines() + read_text(base).splitlines()


CAPACITY_SLACK = 1.02       # доли округляются: Σ групп чуть выше ёмкости ещё честная строка


def breaks_capacity(data: dict) -> bool:
    """Группа выше окно_с × ядер или Σ групп выше ёмкости +2 % — физически невозможная строка (сбой замера)."""
    capacity = data["окно_с"] * data["ядер"]
    cpu = [g["cpu_с"] for g in data["группы"]]
    return any(seconds > capacity for seconds in cpu) or sum(cpu) > capacity * CAPACITY_SLACK


def raw_samples(lines: list, now: float, hours: float) -> list:
    """[(момент, data)] выборок окна, включая невозможные; строка другой формы пропускается, а не роняет сводку."""
    since = now - hours * 3600
    rows = []
    for line in lines:
        if '"process.sample"' not in line:
            continue
        try:
            row = json.loads(line)
            data, moment = row["data"], dt.datetime.fromisoformat(row["ts"]).timestamp()
            if row.get("event") == "process.sample" and moment >= since and data["окно_с"] and data["ядер"] \
                    and all(g["id"] for g in data["группы"]) and isinstance(data["остаток"], dict):
                rows.append((moment, data))
        except (ValueError, KeyError, TypeError):
            continue
    return rows


def timed_samples(lines: list, now: float, hours: float) -> list:
    """Выборки окна без строк, нарушающих ограничитель ёмкости; журнал при этом не правится."""
    return [(moment, data) for moment, data in raw_samples(lines, now, hours) if not breaks_capacity(data)]


def samples(lines: list, now: float, hours: float) -> list:
    return [data for _, data in timed_samples(lines, now, hours)]


def aggregate(lines: list, now: float, hours: float) -> dict:
    """Выборки окна → {samples, dropped (строк со сбоем замера), hours (покрытие), capacity_с, rss_peak
    (пик суммарной памяти выборки, МБ), ids: {id: {cpu_с, rss_пик, rss_ср}}, rest,
    last_seen: {(id, прогон): момент последней выборки}}."""
    raw = raw_samples(lines, now, hours)
    rows = [(moment, data) for moment, data in raw if not breaks_capacity(data)]
    cpu, peak, rss_sum, rss_n = (collections.Counter() for _ in range(4))
    rest, last_seen = collections.Counter(), {}
    covered = capacity = rss_peak = 0.0
    for moment, data in rows:
        rss_peak = max(rss_peak, sum(g["rss_мб"] for g in data["группы"]))
        covered += data["окно_с"]
        capacity += data["окно_с"] * data["ядер"]
        per_id = collections.Counter()
        for g in data["группы"]:
            cpu[g["id"]] += g["cpu_с"]
            per_id[g["id"]] += g["rss_мб"]
            last_seen[(g["id"], g.get("прогон"))] = moment
        for ident, mb in per_id.items():
            peak[ident] = max(peak[ident], mb)
            rss_sum[ident] += mb
            rss_n[ident] += 1
        rest.update(data["остаток"])
    ids = {i: {"cpu_с": cpu[i], "rss_пик": peak[i], "rss_ср": rss_sum[i] / rss_n[i]} for i in cpu}
    return {"samples": len(rows), "dropped": len(raw) - len(rows), "hours": covered / 3600, "covered_с": covered,
            "capacity_с": capacity, "rss_peak": rss_peak, "ids": ids, "rest": dict(rest), "last_seen": last_seen}


def summarize(lines: list, now: float, hours: float) -> dict:
    """Для суточного отчёта: {samples, hours, unparsed_pct, items: [{name, cores, share_pct}]} по убыванию CPU.
    «Не разобрано» — остатки делимых юнитов и неразмеченное, в долях ёмкости."""
    agg = aggregate(lines, now, hours)
    if not agg["samples"]:
        return {"samples": 0, "hours": 0.0, "unparsed_pct": 0.0, "items": []}
    items = [{"name": i, "cores": v["cpu_с"] / agg["covered_с"], "share_pct": 100 * v["cpu_с"] / agg["capacity_с"]}
             for i, v in agg["ids"].items()]
    lost = sum(v for k, v in agg["rest"].items() if k != "вне_корзин") + sum(
        v["cpu_с"] for i, v in agg["ids"].items() if i.endswith(": неразмечено"))
    return {"samples": agg["samples"], "hours": agg["hours"], "unparsed_pct": 100 * max(0.0, lost) / agg["capacity_с"],
            "items": sorted(items, key=lambda i: -i["cores"])}


# ───────────────────────── запуски и токены (В2) ─────────────────────────

CONSTANT_STARTS = ("служба", "контейнер")
DEMON_ALIASES = {"checks-night": "checks-stream"}
HELPERS = "helpers"
NO_LABEL_HELPERS = "helpers: без метки"
NO_LABEL_HEAD = "модель: голова без метки"
LIVE_START_SLACK_S = (-2, 60)       # событие process.start пишется чуть позже старта процесса обёртки
SPEND_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def parse_ts(text):
    try:
        return dt.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def jsonl_rows(lines: list):
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def constant_ids(library: dict) -> set:
    """Службы и контейнеры: работают постоянно, в знаменатель запусков не входят."""
    return {r["id"] for r in library.get("процессы", []) if r.get("запуск") in CONSTANT_STARTS}


def process_alive(proc_dir):
    """alive(pid, момент process.start): процесс с этим pid жив и стартовал перед событием, а не занял pid потом."""
    boot = next((int(line.split()[1]) for line in read_text(os.path.join(proc_dir, "stat")).splitlines()
                 if line.startswith("btime ")), 0)

    def alive(pid: int, started: float) -> bool:
        stat = read_text(os.path.join(proc_dir, str(pid), "stat"))
        if not stat:
            return False
        began = boot + int(stat[stat.rindex(")") + 2:].split()[19]) / TICK
        return LIVE_START_SLACK_S[0] <= started - began <= LIVE_START_SLACK_S[1]
    return alive


def run_counts(lines: list, last_seen: dict, now: float, hours: float, alive) -> dict:
    """{id: {n, sec, skipped, torn, running}} по process-runs. Конец без начала в окне не считается.
    Начало без конца: pid жив (тот же starttime) — «идёт», время с начала; иначе «оборван», время до
    последней выборки В1, где прогон был виден (оценка)."""
    since = now - hours * 3600
    runs = collections.defaultdict(lambda: {"n": 0, "sec": 0.0, "skipped": 0, "torn": 0, "running": 0})

    def tear(ident, pid, started):
        runs[ident]["torn"] += 1
        runs[ident]["sec"] += max(0.0, last_seen.get((ident, str(pid)), started) - started)
    open_runs = {}
    for row in jsonl_rows(line for line in lines if '"process.' in line):
        moment, data = parse_ts(row.get("ts")), row.get("data") or {}
        if moment is None or moment < since or not data.get("id"):
            continue
        key = (data["id"], data.get("pid"))
        if row["event"] == "process.skip":
            runs[data["id"]]["skipped"] += 1
        elif row["event"] == "process.start":
            if key in open_runs:                                  # pid занят снова: прежний прогон конца не оставил
                tear(*key, open_runs[key])
            open_runs[key] = moment
            runs[data["id"]]["n"] += 1
        elif row["event"] == "process.end" and key in open_runs:
            runs[data["id"]]["sec"] += float(data.get("с") or 0)
            del open_runs[key]
    for (ident, pid), started in open_runs.items():
        if alive(pid, started):
            runs[ident]["running"] += 1
            runs[ident]["sec"] += max(0.0, now - started)
        else:
            tear(ident, pid, started)
    return dict(runs)


def transcript_span(path) -> float:
    """Последний − первый timestamp транскрипта помощника, с."""
    def stamp(line):
        try:
            row = json.loads(line)
        except ValueError:
            return None
        return parse_ts(row.get("timestamp")) if isinstance(row, dict) else None
    try:
        with open(path, "rb") as handle:
            head = [handle.readline() for _ in range(20)]
            handle.seek(max(0, os.fstat(handle.fileno()).st_size - TRANSCRIPT_TAIL // 4))
            tail = handle.read().splitlines()
    except OSError:
        return 0.0
    first = next((s for s in map(stamp, head) if s), None)
    last = next((s for s in map(stamp, reversed(tail)) if s), None)
    return max(0.0, last - first) if first and last else 0.0


def model_call_pointers(logs, transcripts) -> dict:
    """Указатели разрешителя, читаются один раз: agent → meta → agent.launch, session → демон, сессии ask-model."""
    launches, calls = [], {}
    for row in jsonl_rows(read_text(os.path.join(logs, "вызовы-агентов.jsonl")).splitlines()):
        data = row.get("data") or {}
        if row.get("event") == "agent.launch" and row.get("outcome") == "ok" and data.get("вызов"):
            task = row.get("task")
            launch = {"ts": parse_ts(row.get("ts")), "тип": data.get("тип"), "модель": data.get("модель"),
                      "задача": task if task not in (None, "-", "") else None, "path": None}
            launches.append(launch)
            calls[data["вызов"]] = launch
    agents = {}
    root = Path(transcripts)
    for meta_path in root.glob("*/subagents/agent-*.meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        agent = meta_path.name[len("agent-"):-len(".meta.json")]
        launch = calls.get(meta.get("toolUseId")) or {}
        if launch:
            launch["path"] = str(meta_path.with_name(f"agent-{agent}.jsonl"))
        agents[agent] = {"тип": launch.get("тип") or meta.get("agentType"),
                         "модель": launch.get("модель") or meta.get("model"), "задача": launch.get("задача")}
    demons, spend, demon_calls = {}, [], []
    for row in jsonl_rows(read_text(os.path.join(logs, "demon-spend.jsonl")).splitlines()):
        name = DEMON_ALIASES.get(row.get("демон"), row.get("демон"))
        if name and row.get("session_id"):
            demons[row["session_id"]] = name
        if name and row.get("event") != "demon.start":
            moment = parse_ts(row.get("когда"))
            spend.append((moment, row.get("session_id"), sum(int(row.get(k) or 0) for k in SPEND_KEYS)))
            demon_calls.append((moment, name, float(row.get("duration_ms") or 0) / 1000))
    ask = {path.stem for path in root.parent.glob("-tmp-ask-model-*/*.jsonl")}
    return {"launches": launches, "agents": agents, "demons": demons, "spend": spend, "demon_calls": demon_calls,
            "ask": ask}


def model_call_process(event: dict, pointers: dict) -> dict:
    """Чей вызов модели: {"id", для помощника ещё тип/модель/задача}. Порядок правил — §В.3 п.3."""
    if event.get("agent"):
        meta = pointers["agents"].get(event["agent"])
        return {"id": HELPERS, **meta} if meta else {"id": NO_LABEL_HELPERS}
    if event.get("headless"):
        session = event.get("session")
        if session in pointers["demons"]:
            return {"id": pointers["demons"][session]}
        return {"id": "ask-the-model"} if session in pointers["ask"] else {"id": NO_LABEL_HEAD}
    return {"id": NO_LABEL_HELPERS} if event.get("side") else {"id": "claude-session"}


def launch_counts(pointers: dict, now: float, hours: float) -> dict:
    """Запуски без process.start: помощник = agent.launch ok, время по транскрипту; вызов демона = строка demon-spend.
    {id: {n, sec, parts: {тип модель: {n, sec}}}}."""
    since, runs = now - hours * 3600, {}
    for launch in pointers["launches"]:
        if launch["ts"] and launch["ts"] >= since:
            entry = runs.setdefault(HELPERS, {"n": 0, "sec": 0.0, "parts": {}})
            part = entry["parts"].setdefault(f"{launch['тип']} {launch['модель']}", {"n": 0, "sec": 0.0})
            spent = transcript_span(launch["path"]) if launch["path"] else 0.0
            for target in (entry, part):
                target["n"] += 1
                target["sec"] += spent
    for moment, name, seconds in pointers["demon_calls"]:
        if moment and moment >= since:
            entry = runs.setdefault(name, {"n": 0, "sec": 0.0})
            entry["n"] += 1
            entry["sec"] += seconds
    return runs


def token_events(logs, since: float, now: float) -> list:
    """События tokens-YYYY-MM.jsonl с ts ≥ since. Читаются файлы месяцев окна (с запасом в сутки на пояс);
    символлинк tokens.jsonl не читается: это тот же файл, окно вышло бы вдвое."""
    months, day = set(), since - 86400
    while day <= now + 86400:
        months.add(dt.date.fromtimestamp(day).strftime("%Y-%m"))
        day += 86400
    events = []
    for month in sorted(months):
        for event in jsonl_rows(read_text(os.path.join(logs, f"tokens-{month}.jsonl")).splitlines()):
            moment = parse_ts(event.get("ts"))
            if moment is not None and moment >= since:
                event["_t"] = moment
                events.append(event)
    return events


def event_tokens(event: dict) -> tuple:
    """(контекст, вывод): контекст = свежий вход + запись в кэш + чтение кэша, как в tokens-daily.json."""
    return (sum(int(event.get(k) or 0) for k in ("in", "cache_w", "cache_r")), int(event.get("out") or 0))


def token_totals(events: list, pointers: dict, since: float) -> dict:
    """{ids: {id: {all, out, parts: {тип модель: all}}}, total, out}: каждому вызову модели — один процесс."""
    ids, total, out_total = {}, 0, 0
    for event in events:
        if event["_t"] < since:
            continue
        who = model_call_process(event, pointers)
        context, out = event_tokens(event)
        entry = ids.setdefault(who["id"], {"all": 0, "out": 0, "parts": {}})
        entry["all"] += context + out
        entry["out"] += out
        if who.get("тип"):
            part = f"{who['тип']} {who['модель']}"
            entry["parts"][part] = entry["parts"].get(part, 0) + context + out
        total += context + out
        out_total += out
    return {"ids": ids, "total": total, "out": out_total}


def unlabelled_pct(totals: dict) -> float:
    lost = sum(v["all"] for i, v in totals["ids"].items() if "без метки" in i)
    return 100 * lost / totals["total"] if totals["total"] else 0.0


def daily_summary(logs):
    try:
        daily = json.loads(read_text(os.path.join(logs, "tokens-daily.json")))
        dt.date.fromisoformat(daily["дата"])
        return daily
    except (ValueError, KeyError, TypeError):
        return None


def reconcile(events: list, daily: dict) -> dict:
    """Сверка с tokens-collector: сутки по дата_события (локальный день события), как в tokens-daily.json."""
    mine = [event_tokens(e) for e in events if dt.date.fromtimestamp(e["_t"]).isoformat() == daily["дата"]]
    context, out = sum(c for c, _ in mine), sum(o for _, o in mine)
    theirs = (int(daily.get("контекст всего") or 0), int(daily.get("ответов всего") or 0))
    return {"день": daily["дата"], "процессы": (context, out), "daily": theirs,
            "расхождение": context + out - sum(theirs)}


def demon_check(events: list, pointers: dict, since: float):
    """Транскрипты против demon-spend по тем же сессиям: (транскрипты, demon-spend) в токенах или None."""
    spend = [(session, tokens) for moment, session, tokens in pointers["spend"] if moment and moment >= since]
    sessions = {session for session, _ in spend}
    seen = sum(sum(event_tokens(e)) for e in events if e["_t"] >= since and e.get("session") in sessions)
    return (seen, sum(tokens for _, tokens in spend)) if spend else None


def window_shares(logs, transcripts, proc_dir, agg: dict, library: dict, now: float, hours: float) -> dict:
    """Запуски и токены окна: {runs, launches уже внутри runs, tokens, events, pointers, constant, daily}."""
    since = now - hours * 3600
    pointers = model_call_pointers(logs, transcripts)
    daily = daily_summary(logs)
    load_from = min(since, dt.datetime.fromisoformat(daily["дата"]).timestamp() - 86400) if daily else since
    events = token_events(logs, load_from, now)
    runs = run_counts(read_lines(logs, "process-runs"), agg["last_seen"], now, hours, process_alive(proc_dir))
    for ident, found in launch_counts(pointers, now, hours).items():
        entry = runs.setdefault(ident, {"n": 0, "sec": 0.0, "skipped": 0, "torn": 0, "running": 0})
        entry["n"] += found["n"]
        entry["sec"] += found["sec"]
        if "parts" in found:
            entry["parts"] = found["parts"]
    constant = constant_ids(library)
    counted = [v for i, v in runs.items() if i not in constant]
    return {"runs": runs, "run_n": sum(v["n"] for v in counted), "run_sec": sum(v["sec"] for v in counted),
            "tokens": token_totals(events, pointers, since), "events": events, "pointers": pointers,
            "constant": constant, "daily": daily, "since": since}


def week_record(agg: dict, shares: dict, ident: str, accuracy: str):
    """Блок «неделя» библиотеки: сервер, запуски, токены; пустой блок не пишется."""
    week = {"покрытие_ч": round(agg["hours"], 1)}
    if ident in agg["ids"]:
        v = agg["ids"][ident]
        week["сервер"] = {"ядро_часов": round(v["cpu_с"] / 3600, 3),
                          "доля_ёмкости_проц": round(100 * v["cpu_с"] / agg["capacity_с"], 2),
                          "память_пик_мб": round(v["rss_пик"], 1), "память_средняя_мб": round(v["rss_ср"], 1),
                          "точность": accuracy}
    runs = shares["runs"].get(ident)
    if runs and ident not in shares["constant"] and (runs["n"] or runs["skipped"]):
        week["запуски"] = {"запусков": runs["n"], "в_работе_ч": round(runs["sec"] / 3600, 3),
                           "доля_проц": round(100 * runs["sec"] / shares["run_sec"], 1) if shares["run_sec"] else 0.0,
                           "пропусков": runs["skipped"]}
    tokens = shares["tokens"]["ids"].get(ident)
    if tokens:
        total = shares["tokens"]["total"]
        week["токены"] = {"всего": tokens["all"], "вывод": tokens["out"], "доля_проц": round(100 * tokens["all"] / total, 2),
                          "без_метки_проц": round(unlabelled_pct(shares["tokens"]), 2)}
    return week if len(week) > 1 else None
