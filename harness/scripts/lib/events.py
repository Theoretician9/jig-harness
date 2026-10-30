#!/usr/bin/env python3
"""Общая запись и чтение событий харнеса (формат v1).

Одно место, где журнал открывается на дозапись (`emit`), одно место, где
старые журналы приводятся к общему виду (`read` + адаптеры из
harness/config/journals.yaml), одно место, где определяется задача
(`current_task`), и инкрементальная сводка для запроса агента (`index_*`).

Горячий путь `emit` не импортирует yaml и не читает реестр: bash-писатель
платит процессом python на каждое событие, а реестр с картой — это десятки
миллисекунд. Нужные записи данные (каталоги журналов, digest_keys) живут
короткой таблицей ниже; её равенство реестру судят ворота G2.

Дверь bash: python3 -I scripts/lib/events.py emit <журнал> <src> <событие> <исход>
[ключ=значение …] [--json '<data>'] — всегда код 0.
"""
from __future__ import annotations

import bisect
import dataclasses
import datetime as dt
import fcntl
import glob
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

OUTCOMES = ("ok", "refused", "error", "skipped", "held")

JOURNAL_NAME = re.compile(r"[a-zа-яё0-9_-]+")
HASH_VALUE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
DIGEST_KEYS = frozenset({"sha256", "head", "commit", "ino_from"})

MAX_LINE_BYTES = 16384
MAX_VALUE_CHARS = 4000
WRITE_DEADLINE_S = 2.0
LOCK_POLL_S = 0.02

# Журнал панели лежит в panel-state: LOG_DIR ей закрыт на запись.
PANEL_STATE_JOURNALS = frozenset({"панель"})
MODE_JOURNAL = 0o640
MODE_SHARED = 0o660
MODE_DIR = 0o2770

INDEX_VERSION = 2


# ───────────────────────── места ─────────────────────────

def log_dir() -> str:
    return os.environ.get("HARNESS_LOG_DIR") or config.log_dir()


def state_dir() -> str:
    return os.environ.get("HARNESS_EVENTS_STATE") or os.path.join(log_dir(), ".events")


def panel_state_dir() -> str:
    return config.panel_state_dir()


def devmap_path() -> str:
    return os.environ.get("HARNESS_DEVMAP") or str(config.корень_дерева(__file__) / "dev-map.yaml")


def registry_path() -> str:
    return os.environ.get("HARNESS_JOURNALS") or str(
        config.корень_дерева(__file__) / "harness" / "config" / "journals.yaml")


def events_gid() -> int:
    import grp
    name = (os.environ.get("EVENTS_GROUP")
            or config.из_файла(config.путь_harness_conf()).get("EVENTS_GROUP")
            or "harness-panel")
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return os.getgid()


def _journal_target(name: str) -> tuple[str, int]:
    if name in PANEL_STATE_JOURNALS:
        return os.path.join(panel_state_dir(), f"{name}.jsonl"), MODE_SHARED
    return os.path.join(log_dir(), f"{name}.jsonl"), MODE_JOURNAL


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _set_group(fd_or_path, gid: int) -> None:
    """Группа не сменилась (агент не в ней) — права доступа остаются по режиму."""
    try:
        if isinstance(fd_or_path, int):
            os.fchown(fd_or_path, -1, gid)
        else:
            os.chown(fd_or_path, -1, gid)
    except OSError:
        pass


def _ensure_dir(path: str) -> None:
    """Каталог состояния лечит себя сам: первый emit агента создаёт его без root."""
    if os.path.isdir(path):
        return
    try:
        os.mkdir(path)
    except FileExistsError:
        return
    _set_group(path, events_gid())
    os.chmod(path, MODE_DIR)


def _atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, MODE_SHARED)
    try:
        os.fchmod(fd, MODE_SHARED)
        _set_group(fd, events_gid())
        data = text.encode("utf-8")
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)
    os.replace(tmp, path)


# ───────────────────────── запись ─────────────────────────

def _append_line(path: str, mode: int, payload: bytes) -> str | None:
    """Один срок на событие: ожидание блокировки и повторы после ротации делят
    его вместе. Возвращает причину отказа или None."""
    started = time.monotonic()
    while True:
        created = False
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL, mode)
            created = True
        except FileExistsError:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_APPEND)
            except FileNotFoundError:
                continue
        try:
            if created:
                os.fchmod(fd, mode)
                _set_group(fd, events_gid())
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() - started >= WRITE_DEADLINE_S:
                        return "lock-timeout"
                    time.sleep(LOCK_POLL_S)
            held = os.fstat(fd)
            try:
                live = os.stat(path)
            except FileNotFoundError:
                live = None
            if live is None or (live.st_ino, live.st_dev) != (held.st_ino, held.st_dev):
                if time.monotonic() - started >= WRITE_DEADLINE_S:
                    return "rotated-away"
                continue
            written = os.write(fd, payload)
            if written != len(payload):
                os.ftruncate(fd, held.st_size)
                return "short-write"
            return None
        finally:
            os.close(fd)


def _mask_secrets(text: str) -> str:
    import patterns
    for secret in patterns.секреты_в_речи(text):
        text = text.replace(secret, "<СКРЫТО>")
    return text


def _record_lost(journal: str, src: str, reason: str, kind: str = "lost") -> None:
    # Аргументы сдвинутого вызова бывают секретом: строка потери и stderr читаются дальше.
    row = {"ts": _now_iso(), "journal": _mask_secrets(str(journal)[:100]),
           "src": _mask_secrets(str(src)[:200]),
           "reason": reason, "kind": kind}
    if kind == "lost":
        print(f"events: запись потеряна: журнал {row['journal']}, причина {reason}, источник {row['src']}",
              file=sys.stderr)
    try:
        state = state_dir()
        _ensure_dir(state)
        payload = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8", "replace")
        failure = _append_line(os.path.join(state, "lost.jsonl"), MODE_SHARED, payload)
        if failure:
            print(f"events: строка потери не записана ({failure})", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — потеря потери уходит только в stderr
        print(f"events: строка потери не записана ({type(exc).__name__})", file=sys.stderr)


def _scrub_string(key: str, value: str, key_dirs: list[str], mangled: list[str]) -> str:
    if key in DIGEST_KEYS and HASH_VALUE.fullmatch(value):
        return value
    for d in key_dirs:
        # Путь живёт в кавычках, в --file=, после «(» и «,»: целым словом он бывает не всегда.
        pattern = re.escape(d) + r"/[^\s\"',;)]*"
        value, hits = re.subn(pattern, "<путь-ключ>", value)
        if hits:
            mangled.append("path-key")
    import patterns
    for secret in patterns.секреты_в_речи(value):
        value = value.replace(secret, "<СКРЫТО>")
        mangled.append("digest" if HASH_VALUE.fullmatch(secret) else "secret")
    if len(value) > MAX_VALUE_CHARS:
        mangled.append("value-cut")
        value = value[:MAX_VALUE_CHARS] + f"…[+{len(value) - MAX_VALUE_CHARS}]"
    return value


def _scrub(key: str, value, key_dirs: list[str], mangled: list[str]):
    if isinstance(value, str):
        return _scrub_string(key, value, key_dirs, mangled)
    if isinstance(value, dict):
        return {_scrub_string("", str(k), key_dirs, mangled): _scrub(str(k), v, key_dirs, mangled)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(key, v, key_dirs, mangled) for v in value]
    return value


def _parse_iso(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.strip())


def _source_time(at) -> str | None:
    """Время источника приводится к виду ts: пояс машины, секунды."""
    try:
        moment = _parse_iso(str(at))
    except ValueError:
        return None
    return moment.astimezone().isoformat(timespec="seconds")


def emit(src, event, outcome, *, journal, task=None, session=None, n=None, unit=None,
         data=None, at=None) -> bool:
    try:
        return _emit(src, event, outcome, journal, task, session, n, unit, data, at)
    except Exception:  # noqa: BLE001 — писатель не умирает из-за журнала
        exc = sys.exc_info()[1]
        _record_lost(journal, src, "oserror" if isinstance(exc, OSError) else f"bug:{type(exc).__name__}")
        return False


def _emit(src, event, outcome, journal, task, session, n, unit, data, at) -> bool:
    if outcome not in OUTCOMES:
        _record_lost(journal, src, "bad-outcome")
        return False
    if not isinstance(journal, str) or not JOURNAL_NAME.fullmatch(journal):
        _record_lost(journal, src, "bad-journal-name")
        return False
    path, mode = _journal_target(journal)

    mangled: list[str] = []
    key_dirs = [d for d in (config.конфиг().get("PANEL_STATE_DIR"),) if d]
    if isinstance(src, str):
        src = _scrub_string("", src, key_dirs, mangled)
    if isinstance(event, str):
        event = _scrub_string("", event, key_dirs, mangled)
    if data is not None:
        data = _scrub("", data, key_dirs, mangled)

    if task is not None:
        task_from = "arg"
    else:
        task, task_from = current_task()
    if session is None:
        session = os.environ.get("CLAUDE_CODE_SESSION_ID") or None

    row: dict = {"v": 1, "ts": _now_iso()}
    if at is not None:
        source_time = _source_time(at)
        if source_time:
            row["at"] = source_time
        else:
            mangled.append("at-dropped")
    row.update({"src": src, "event": event, "outcome": outcome, "task": task,
                "task_from": task_from, "session": session})
    if n is not None:
        row["n"] = n
        if unit is not None:
            row["unit"] = unit
    if data is not None:
        row["data"] = data

    payload = (json.dumps(row, ensure_ascii=False, default=str) + "\n").encode("utf-8", "replace")
    if len(payload) > MAX_LINE_BYTES and data is not None:
        row["data"] = {"truncated_bytes": len(payload)}
        mangled.append("line-cut")
        payload = (json.dumps(row, ensure_ascii=False, default=str) + "\n").encode("utf-8", "replace")
    if len(payload) > MAX_LINE_BYTES:
        _record_lost(journal, src, "oversize")
        return False

    failure = _append_line(path, mode, payload)
    for reason in mangled:
        _record_lost(journal, src, reason, "mangled")
    if failure:
        _record_lost(journal, src, failure)
        return False
    return True


# ───────────────────────── задача ─────────────────────────

def _wip_ids(devmap: str) -> list[str]:
    import yaml
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    with open(devmap, encoding="utf-8") as fh:
        tree = yaml.load(fh, Loader=loader)
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if "id" in node and node.get("status") == "wip":
                found.append(str(node["id"]))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(tree)
    return found


def _task_from_map() -> str | None:
    devmap = devmap_path()
    try:
        st = os.stat(devmap)
    except OSError:
        return None
    key = {"path": devmap, "mtime_ns": st.st_mtime_ns, "size": st.st_size}
    cache = os.path.join(state_dir(), "current-task.json")
    try:
        with open(cache, encoding="utf-8") as fh:
            cached = json.load(fh)
        if all(cached.get(k) == v for k, v in key.items()):
            return cached.get("task")
    except (OSError, ValueError):
        pass
    ids = _wip_ids(devmap)
    task = ids[0] if len(ids) == 1 else None
    try:
        _ensure_dir(state_dir())
        _atomic_write(cache, json.dumps({**key, "task": task}, ensure_ascii=False))
    except OSError:
        pass  # кэш — ускорение, не данные
    return task


def _task_from_history() -> str | None:
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "-C", os.path.dirname(os.path.abspath(devmap_path())), "log", "-30", "--format=%B"],
            capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    found = re.search(r"^Dev-Map:\s*(\S+)", proc.stdout, re.MULTILINE)
    return found.group(1) if found else None


def current_task() -> tuple[str | None, str]:
    """Порядок §3.3; аргумент вызова судит сам emit. Две wip или падение чтения
    карты — карта задачу не называет."""
    task = os.environ.get("HARNESS_TASK")
    if task:
        return task, "env"
    try:
        task = _task_from_map()
    except Exception:  # noqa: BLE001 — битая карта не мешает записи события
        task = None
    if task:
        return task, "map"
    task = _task_from_history()
    if task:
        return task, "history"
    return None, "none"


# ───────────────────────── читалка ─────────────────────────

@dataclass
class Event:
    time: dt.datetime
    journal: str
    src: str
    event: str
    outcome: str
    task: str | None = None
    task_from: str = "none"
    session: str | None = None
    n: float | None = None
    unit: str | None = None
    data: dict = field(default_factory=dict)
    tz_assumed: bool = False


def load_registry() -> dict:
    import yaml
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    with open(registry_path(), encoding="utf-8") as fh:
        return yaml.load(fh, Loader=loader) or {}


def journal_file(entry: dict) -> str:
    base = panel_state_dir() if entry.get("dir") == "panel_state" else log_dir()
    return os.path.join(base, entry["file"])


def parse_time(raw, kind: str) -> tuple[dt.datetime, bool]:
    """Время строки с поясом всегда: наивное — пояс машины с пометкой."""
    if isinstance(raw, (int, float)) or kind == "epoch":
        return dt.datetime.fromtimestamp(float(raw), tz=dt.timezone.utc).astimezone(), False
    text = str(raw).strip()
    if kind == "date" or len(text) == 10:
        return dt.datetime.fromisoformat(text).astimezone(), False
    moment = _parse_iso(text)
    if moment.tzinfo is None:
        return moment.astimezone(), True
    return moment, False


def _event_code(entry: dict, row: dict) -> str:
    source = str(entry.get("event_from") or "")
    if source.startswith("field:"):
        return str(row.get(source[6:]) or "unknown")
    return source or "row"


def _outcome(entry: dict, row: dict) -> str:
    default = entry.get("outcome_default", "ok")
    fieldname = entry.get("outcome_field")
    if fieldname:
        return (entry.get("outcome_map") or {}).get(str(row.get(fieldname)), default)
    return default


def adapt_row(name: str, entry: dict, row: dict) -> Event:
    """Строка журнала → Event. ValueError — строка не приводится (битая)."""
    if row.get("v") == 1:
        outcome = row.get("outcome")
        if outcome not in OUTCOMES:
            raise ValueError("outcome")
        moment, assumed = parse_time(row.get("at") or row["ts"], "tz")
        return Event(moment, name, row.get("src") or "unknown", row.get("event") or "unknown",
                     outcome, row.get("task"), row.get("task_from") or "none", row.get("session"),
                     row.get("n"), row.get("unit"), row.get("data") or {}, assumed)
    try:
        moment, assumed = parse_time(row[entry["time_field"]], entry["time_kind"])
    except (KeyError, TypeError) as exc:
        raise ValueError("time") from exc
    task = row.get(entry["task_field"]) if entry.get("task_field") else None
    if task in ("-", ""):
        task = None
    task_from = "none"
    if task:
        tf_field = entry.get("task_from_field")
        task_from = (entry.get("task_from_map") or {}).get(str(row.get(tf_field)), "arg") if tf_field else "arg"
    session = row.get(entry["session_field"]) if entry.get("session_field") else None
    writers = entry.get("writers")
    src = writers[0] if isinstance(writers, list) and writers else "unknown"
    return Event(moment, name, src, _event_code(entry, row), _outcome(entry, row),
                 task, task_from, session or None, None, None, row, assumed)


def iter_journal_events(name: str, entry: dict, lines, counters: dict | None = None):
    """Общий разбор для читалки и сводки: события из сырых строк; битые считает."""
    for raw in lines:
        if not raw.strip():
            continue
        if counters is not None:
            counters["lines"] = counters.get("lines", 0) + 1
        try:
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise ValueError("shape")
            ev = adapt_row(name, entry, row)
        except (ValueError, TypeError):
            if counters is not None:
                counters["broken"] = counters.get("broken", 0) + 1
            continue
        if counters is not None and row.get("v") == 1:
            counters["v1"] = counters.get("v1", 0) + 1
        yield ev


def read(since: dt.datetime, until: dt.datetime, journals=None, stats: dict | None = None):
    stats = stats if stats is not None else {}
    broken = stats.setdefault("broken", {})
    stats.setdefault("skipped_sample", 0)
    stats.setdefault("scanned_journals", 0)
    stats.setdefault("scanned_lines", 0)
    stats.setdefault("no_time", [])
    stats.setdefault("skipped_old", 0)
    for name, entry in (load_registry().get("journals") or {}).items():
        if journals is not None and name not in journals:
            continue
        path = journal_file(entry)
        if not os.path.exists(path):
            continue
        if entry.get("kind") == "sample":
            stats["skipped_sample"] += 1
            continue
        if entry.get("time_kind") in (None, "none"):
            stats["no_time"].append(name)
            continue
        if os.stat(path).st_mtime < since.timestamp() - 1:
            stats["skipped_old"] += 1
            continue
        counters: dict = {}
        stats["scanned_journals"] += 1
        with open(path, encoding="utf-8", errors="replace") as fh:
            for ev in iter_journal_events(name, entry, fh, counters):
                if since <= ev.time < until:
                    yield ev
        stats["scanned_lines"] += counters.get("lines", 0)
        if counters.get("broken"):
            broken[name] = broken.get(name, 0) + counters["broken"]


# ───────────────────────── сводка (§8.5) ─────────────────────────

def index_dir() -> str:
    return os.path.join(state_dir(), "index")


def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def reason_text(ev: Event, entry: dict) -> str:
    keys = [entry["reason_field"]] if entry.get("reason_field") else []
    keys += ["reason", "why", "rule", "почему", "причина", "failed"]
    for k in keys:
        value = ev.data.get(k) if isinstance(ev.data, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\n", " ")[:80]
    return ""


def _skill_sessions(registry: dict) -> dict[str, tuple[list, list]]:
    """Сессия → (времена, задачи) вызовов скилов с задачей: по ним событию без
    задачи достаётся «задача последнего вызова скила в той же сессии»."""
    entry = registry.get("skills")
    calls: dict[str, list] = {}
    if entry and os.path.exists(journal_file(entry)):
        with open(journal_file(entry), encoding="utf-8", errors="replace") as fh:
            for ev in iter_journal_events("skills", entry, fh):
                if ev.task and ev.session:
                    calls.setdefault(ev.session, []).append((ev.time, ev.task))
    result = {}
    for session, items in calls.items():
        items.sort(key=lambda it: it[0])
        result[session] = ([it[0] for it in items], [it[1] for it in items])
    return result


def _commit_tasks(root: str) -> dict[str, str]:
    """Полный sha родителя → задача его детей, если она одна. Дети двух задач —
    родителя в карте нет: гадать между ними нельзя. git не ответил — карта пуста."""
    import subprocess
    try:
        proc = subprocess.run(["git", "-C", root, "log", "--all", "--format=%H%x00%P%x00%B%x1e"],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    seen: dict[str, set] = {}
    for record in proc.stdout.split("\x1e"):
        parts = record.strip("\n").split("\x00", 2)
        if len(parts) < 3:
            continue
        found = re.search(r"^Dev-Map:\s*(\S+)", parts[2], re.MULTILINE)
        if found:
            for parent in parts[1].split():
                seen.setdefault(parent, set()).add(found.group(1))
    return {parent: next(iter(tasks)) for parent, tasks in seen.items() if len(tasks) == 1}


class _TaskInferrer:
    """Выводит задачу событию адаптированного журнала по данным реестра
    (`task_infer`). Работает только в сводке: горячий путь emit реестра не читает."""

    MIN_SHA = 7

    def __init__(self, registry: dict):
        self.sessions = _skill_sessions(registry)
        self.commits = _commit_tasks(os.path.dirname(os.path.abspath(devmap_path())))
        self.by_head: dict[str, str | None] = {}

    def _by_commit(self, head) -> str | None:
        head = str(head or "")
        if len(head) < self.MIN_SHA:
            return None
        if head not in self.by_head:
            self.by_head[head] = next((t for sha, t in self.commits.items() if sha.startswith(head)), None)
        return self.by_head[head]

    def _by_session(self, ev: Event) -> str | None:
        times, tasks = self.sessions.get(ev.session or "", ([], []))
        at = bisect.bisect_right(times, ev.time)
        return tasks[at - 1] if at else None

    def apply(self, ev: Event, entry: dict) -> Event:
        mode = entry.get("task_infer")
        if ev.task or not mode:
            return ev
        if mode == "session":
            task = self._by_session(ev)
        else:
            task = self._by_commit(ev.data.get(entry.get("commit_field")))
        return dataclasses.replace(ev, task=task, task_from="inferred") if task else ev


def _spent(ev: Event, entry: dict):
    """Расход события в токенах: сумма n_fields сырой строки, у v1 — его n."""
    fields = entry.get("n_fields")
    if not fields:
        return ev.n
    return sum(v for v in (ev.data.get(f) for f in fields) if isinstance(v, (int, float)))


class _Accumulator:
    """Счётчики часа и ленты задач, дописываемые поверх уже лежащих файлов."""

    def __init__(self, idx: str, inferrer: _TaskInferrer | None = None):
        self.idx = idx
        self.inferrer = inferrer
        self.days: dict[str, dict] = {}
        self.tasks: dict[str, list[str]] = {}

    def day(self, name: str) -> dict:
        if name not in self.days:
            self.days[name] = _load_json(os.path.join(self.idx, f"day-{name}.json"),
                                         {"hours": {}, "last": {}})
        return self.days[name]

    def add(self, ev: Event, entry: dict) -> None:
        if self.inferrer:
            ev = self.inferrer.apply(ev, entry)
        local = ev.time.astimezone()
        day = self.day(local.strftime("%Y-%m-%d"))
        bucket = day["hours"].setdefault(local.strftime("%H"), {})
        key = "|".join((ev.outcome, ev.src, ev.event, ev.task or "-"))
        bucket[key] = bucket.get(key, 0) + 1
        reason = reason_text(ev, entry)
        if ev.outcome in ("refused", "error"):
            prior = day["last"].get(ev.src)
            if not prior or prior[0] <= local.isoformat(timespec="seconds"):
                day["last"][ev.src] = [local.isoformat(timespec="seconds"), ev.event, reason]
        if ev.task:
            self.tasks.setdefault(ev.task, []).append(json.dumps(
                {"t": local.isoformat(timespec="seconds"), "journal": ev.journal, "src": ev.src,
                 "event": ev.event, "outcome": ev.outcome, "task_from": ev.task_from,
                 "reason": reason, "n": _spent(ev, entry)}, ensure_ascii=False))

    def save(self) -> None:
        for name, day in self.days.items():
            _atomic_write(os.path.join(self.idx, f"day-{name}.json"), json.dumps(day, ensure_ascii=False))
        for task, rows in self.tasks.items():
            path = task_file(task)
            old = ""
            try:
                with open(path, encoding="utf-8") as fh:
                    old = fh.read()
            except OSError:
                pass
            _atomic_write(path, old + "".join(r + "\n" for r in rows))


def task_file(task: str) -> str:
    """Id задачи в реальных данных бывает со слэшем и знаками — имя файла кодируется."""
    import hashlib
    from urllib.parse import quote
    name = quote(task, safe="")
    if len(name) > 100:
        name = name[:60] + "-" + hashlib.sha1(task.encode("utf-8")).hexdigest()[:12]
    return os.path.join(index_dir(), "task", name + ".jsonl")


def _wipe_index(idx: str) -> None:
    for sub in (idx, os.path.join(idx, "task")):
        for fname in os.listdir(sub):
            if fname.startswith("day-") or fname.endswith(".jsonl") or fname in ("cursors.json", "journals.json"):
                os.unlink(os.path.join(sub, fname))


def index_update(rebuild: bool = False) -> dict:
    """Догон сводки по курсорам. Занято другим читателем — вернуть busy и читать
    сводку как есть. Курсор не сошёлся с файлом (ротация, усечение) — пересборка
    целиком: счётчики, уже слитые в сутки, по одному журналу не вычесть."""
    idx = index_dir()
    _ensure_dir(state_dir())
    _ensure_dir(idx)
    _ensure_dir(os.path.join(idx, "task"))
    lock_path = os.path.join(idx, ".lock")
    try:
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, MODE_SHARED)
        # umask урезает права при создании: без fchmod второй пользователь группы не откроет замок.
        os.fchmod(lock_fd, MODE_SHARED)
        _set_group(lock_fd, events_gid())
    except FileExistsError:
        lock_fd = os.open(lock_path, os.O_WRONLY)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"busy": True}
        return _index_locked(idx, rebuild)
    finally:
        os.close(lock_fd)


def _index_locked(idx: str, rebuild: bool) -> dict:
    registry = (load_registry().get("journals") or {})
    cursors = _load_json(os.path.join(idx, "cursors.json"), {})
    meta = _load_json(os.path.join(idx, "journals.json"), {})
    present = {n: e for n, e in registry.items() if os.path.exists(journal_file(e))}
    reason = None
    if rebuild:
        reason = "по просьбе"
    elif cursors.get("version") != INDEX_VERSION:
        reason = "первая сборка" if not cursors else "версия сводки"
    else:
        for name, entry in present.items():
            cur = cursors.get("journals", {}).get(name)
            st = os.stat(journal_file(entry))
            if cur and ((cur["dev"], cur["ino"]) != (st.st_dev, st.st_ino) or cur["offset"] > st.st_size):
                reason = f"курсор потерян: {name}"
                break
    if reason:
        _wipe_index(idx)
        cursors, meta = {"version": INDEX_VERSION, "journals": {}}, {}
    infers = any(e.get("task_infer") for e in present.values())
    acc = _Accumulator(idx, _TaskInferrer(registry) if infers else None)
    for name, entry in present.items():
        path = journal_file(entry)
        st = os.stat(path)
        cur = cursors["journals"].get(name, {"dev": st.st_dev, "ino": st.st_ino, "offset": 0})
        info = meta.setdefault(name, {"lines": 0, "broken": 0, "v1": 0, "events": 0,
                                      "no_time": False, "sample": False, "first": None, "last": None})
        with open(path, "rb") as fh:
            fh.seek(cur["offset"])
            chunk = fh.read()
        end = chunk.rfind(b"\n") + 1
        complete = chunk[:end]
        cursors["journals"][name] = {"dev": st.st_dev, "ino": st.st_ino, "offset": cur["offset"] + end}
        if entry.get("kind") == "sample":
            info["sample"] = True
            continue
        if entry.get("time_kind") in (None, "none"):
            info["no_time"] = True
            info["lines"] += complete.count(b"\n")
            continue
        counters: dict = {}
        for ev in iter_journal_events(name, entry, complete.decode("utf-8", "replace").splitlines(), counters):
            acc.add(ev, entry)
            info["events"] += 1
            stamp = ev.time.astimezone().isoformat(timespec="seconds")
            info["first"] = min(info["first"] or stamp, stamp)
            info["last"] = max(info["last"] or stamp, stamp)
        info["lines"] += counters.get("lines", 0)
        info["broken"] += counters.get("broken", 0)
        info["v1"] += counters.get("v1", 0)
    acc.save()
    _atomic_write(os.path.join(idx, "journals.json"), json.dumps(meta, ensure_ascii=False))
    _atomic_write(os.path.join(idx, "cursors.json"), json.dumps(cursors, ensure_ascii=False))
    return {"busy": False, "rebuilt": reason, "journals": len(present)}


def install() -> dict:
    """Приводит каталоги состояния и права журналов к виду §4.2. Идемпотентна:
    повтор ничего не меняет. Журнал, права которого сменить не вышло, называется
    в ответе, а не пропускается молча."""
    gid = events_gid()
    state = state_dir()
    fixed_dirs = 0
    for d in (state, os.path.join(state, "index"), os.path.join(state, "runs")):
        if not os.path.isdir(d):
            os.mkdir(d)
        os.chown(d, -1, gid)
        os.chmod(d, MODE_DIR)
        fixed_dirs += 1
    journals, failed = 0, []
    for entry in sorted(os.scandir(log_dir()), key=lambda e: e.name):
        if not entry.name.endswith(".jsonl") or not entry.is_file(follow_symlinks=False):
            continue
        try:
            os.chown(entry.path, -1, gid)
            os.chmod(entry.path, MODE_JOURNAL)
            journals += 1
        except OSError as exc:
            failed.append(f"{entry.name}: {type(exc).__name__}")
    return {"dirs": fixed_dirs, "journals": journals, "failed": failed}


def index_meta() -> dict:
    return _load_json(os.path.join(index_dir(), "journals.json"), {})


def window_counts(since: dt.datetime, until: dt.datetime):
    """Счёт (outcome, src, event, task) → n и последние отказы по src за окно.
    Точность — час: событие внутри часа, куда попадает граница, входит целиком."""
    totals: dict[tuple, int] = {}
    last: dict[str, list] = {}
    idx = index_dir()
    if not os.path.isdir(idx):
        return totals, last
    first_day = since.astimezone().strftime("%Y-%m-%d")
    last_day = until.astimezone().strftime("%Y-%m-%d")
    for fname in sorted(os.listdir(idx)):
        if not (fname.startswith("day-") and fname.endswith(".json")):
            continue
        day_name = fname[4:-5]
        if not (first_day <= day_name <= last_day):
            continue
        day = _load_json(os.path.join(idx, fname), {})
        for hour, bucket in (day.get("hours") or {}).items():
            start = dt.datetime.fromisoformat(f"{day_name}T{hour}:00:00").astimezone()
            if start + dt.timedelta(hours=1) <= since or start >= until:
                continue
            for key, n in bucket.items():
                outcome, src, event, task = key.split("|", 3)
                k = (outcome, src, event, task)
                totals[k] = totals.get(k, 0) + n
        for src, rec in (day.get("last") or {}).items():
            if src not in last or last[src][0] <= rec[0]:
                last[src] = rec
    return totals, last


def task_events(task: str, since: dt.datetime, until: dt.datetime) -> list[dict]:
    rows = []
    try:
        with open(task_file(task), encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                when = dt.datetime.fromisoformat(rec["t"])
                if since <= when < until:
                    rec["when"] = when
                    rows.append(rec)
    except OSError:
        pass
    rows.sort(key=lambda r: r["when"])
    return rows


def read_lost_rows() -> list[dict]:
    """Строки lost.jsonl; нет файла — потерь не было, а OSError чтения — наружу:
    ворота обязаны отличать «не прочитал» от «пусто»."""
    rows = []
    try:
        fh = open(os.path.join(state_dir(), "lost.jsonl"), encoding="utf-8")
    except FileNotFoundError:
        return rows
    with fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def lost_rows() -> list[dict]:
    try:
        return read_lost_rows()
    except OSError:
        return []


def unacked_in(rows: list[dict]) -> list[dict]:
    last_ack = max((i for i, r in enumerate(rows) if r.get("kind") == "ack"), default=-1)
    return [r for r in rows[last_ack + 1:] if r.get("kind") == "lost"]


def lost_unacked() -> list[dict]:
    return unacked_in(lost_rows())


GATE_WINDOW_DAYS = 14


def _spent_in(window_start: dt.datetime, window_end: dt.datetime) -> bool:
    """Была ли работа сессии: в tokens-*.jsonl строка окна с in + out > 0. Читает
    файлы напрямую: расход пишет сборщик, а не судимый журнал, и месяцев много.
    OSError наружу: нечитаемый признак работы не равен «работы не было»."""
    months = {window_start.strftime("%Y-%m"), (window_end - dt.timedelta(seconds=1)).strftime("%Y-%m")}
    for month in sorted(months):
        try:
            fh = open(os.path.join(log_dir(), f"tokens-{month}.jsonl"), encoding="utf-8")
        except FileNotFoundError:
            continue
        with fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    moment = _parse_iso(row["ts"]).astimezone()
                    spent = row.get("in", 0) + row.get("out", 0)
                except (ValueError, KeyError, TypeError):
                    continue
                if window_start <= moment < window_end and spent > 0:
                    return True
    return False


def _work_sign(window_start: dt.datetime, window_end: dt.datetime) -> tuple[bool | None, str]:
    """(была ли работа | None — признак не прочитан, причина). Ни одного файла
    расхода — сборщик мёртв, а не «работы не было»."""
    try:
        if not glob.glob(os.path.join(log_dir(), "tokens-*.jsonl")):
            return None, "признак работы не читается (tokens-*.jsonl нет ни одного — сборщик расхода не писал)"
        return _spent_in(window_start, window_end), ""
    except OSError as exc:
        return None, f"признак работы не читается (tokens-*.jsonl: {type(exc).__name__})"


def _v1_per_day(entry: dict, first_day: dt.date) -> dict[dt.date, int]:
    """События "v": 1 журнала по местным суткам, начиная с first_day. Нет файла —
    пусто; OSError чтения — наружу."""
    per_day: dict[dt.date, int] = {}
    try:
        fh = open(journal_file(entry), encoding="utf-8")
    except FileNotFoundError:
        return per_day
    with fh:
        for line in fh:
            try:
                row = json.loads(line)
                if row.get("v") != 1:
                    continue
                day = _parse_iso(row["ts"]).astimezone().date()
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            if day >= first_day:
                per_day[day] = per_day.get(day, 0) + 1
    return per_day


def _journal_silence(name: str, entry: dict, timed: bool, worked: bool | None, first_day: dt.date,
                     yesterday: dt.date) -> tuple[str, str, bool]:
    """Суд одного переведённого журнала: (красное, заметка, судим ли по тишине)."""
    try:
        per_day = _v1_per_day(entry, first_day)
    except OSError as exc:
        return f"{name}: не читается ({type(exc).__name__}), тишина не судится", "", False
    if not timed:
        zero = sum(1 for i in range(GATE_WINDOW_DAYS) if not per_day.get(first_day + dt.timedelta(days=i)))
        return "", f"{name}: не судится: нулевых дней {zero} из {GATE_WINDOW_DAYS}", False
    if not worked:
        return "", "", False
    count = per_day.get(yesterday, 0)
    if count < int(entry["min_daily"]):
        return (f"{name}: за {yesterday} событий v1 {count}, нужно не меньше "
                f"{entry['min_daily']} при работе сессии"), "", True
    return "", "", True


def lost_gate(now: dt.datetime | None = None) -> tuple[int, list[str]]:
    """Ворота G3: потеря без разбора и тишина журнала, невозможная без поломки.
    Тишина судится только при работе сессии в прошлые полные сутки; пустой набор
    судимых (переведённые есть, а min_daily ни у кого) — красный; источник, который
    не прочитан, — красный, а не «пусто». Ничего не пишет."""
    now = (now or dt.datetime.now()).astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - dt.timedelta(days=1)
    red, notes = [], []
    try:
        unacked = unacked_in(read_lost_rows())
        lost_count = str(len(unacked))
    except OSError as exc:
        unacked, lost_count = [], "?"
        red.append(f"lost.jsonl не читается ({type(exc).__name__}): потери не судятся")
    migrated = {n: e for n, e in (load_registry().get("journals") or {}).items() if e.get("migrated")}
    timed = {n: e for n, e in migrated.items() if e.get("min_daily")}
    worked, why_unread = _work_sign(yesterday, today)
    red += [f"{r.get('journal', '?')}: потеря без разбора ({r.get('reason', '?')}), {r.get('src', '?')}"
            for r in unacked]
    judged = quiet = 0
    if not migrated:
        notes.append("не судится: переведённых 0")
    else:
        if worked is None:
            red.append(why_unread)
        first_day = (today - dt.timedelta(days=GATE_WINDOW_DAYS)).date()
        for name, entry in migrated.items():
            bad, note, was_judged = _journal_silence(name, entry, name in timed, worked, first_day,
                                                     yesterday.date())
            red += [bad] if bad else []
            notes += [note] if note else []
            judged += was_judged
            quiet += bool(bad) and was_judged
        if not timed:
            red.append("с min_daily 0: переведённые есть, а судить тишину нечем — пустой набор")
        elif worked is False:
            notes.append("не судится: работы не было")
    shown = {True: "да", False: "нет", None: "?"}[worked]
    head = (f"потерь без разбора {lost_count}; переведённых {len(migrated)}, с min_daily {len(timed)}, "
            f"судимых {judged} (работа была: {shown}), тихих {quiet}")
    return (1 if red else 0), [head, *notes, *red]


def lost_ack(reason: str) -> bool:
    import patterns
    for secret in patterns.секреты_в_речи(reason):
        reason = reason.replace(secret, "<СКРЫТО>")
    _record_lost("-", "scripts/events.py", reason[:200], "ack")
    return True


# ───────────────────────── дверь bash ─────────────────────────

def _cli_emit(argv: list[str]) -> int:
    if len(argv) < 4:
        print("events: emit <журнал> <src> <событие> <исход> [ключ=значение …] [--json '<data>']",
              file=sys.stderr)
        return 0
    journal, src, event, outcome, *rest = argv
    data: dict = {}
    i = 0
    while i < len(rest):
        item = rest[i]
        if item == "--json" and i + 1 < len(rest):
            try:
                parsed = json.loads(rest[i + 1])
                data.update(parsed if isinstance(parsed, dict) else {"json": parsed})
            except ValueError:
                data["json_error"] = rest[i + 1][:200]
            i += 2
            continue
        if "=" in item:
            k, v = item.split("=", 1)
            data[k] = v
        else:
            data.setdefault("args", []).append(item)
        i += 1
    emit(src, event, outcome, journal=journal, data=data or None)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "emit":
        try:
            _cli_emit(sys.argv[2:])
        except Exception as exc:  # noqa: BLE001 — дверь bash не роняет писателя
            print(f"events: дверь не отработала ({type(exc).__name__})", file=sys.stderr)
        sys.exit(0)
    print("events: библиотека; запрос агента — scripts/events.py", file=sys.stderr)
    sys.exit(2)
