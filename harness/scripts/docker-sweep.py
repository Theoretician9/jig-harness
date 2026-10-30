#!/usr/bin/env python3
"""Уборка docker: старые образы, остановленные контейнеры, безымянные тома, кэш сборки.

Механизм УДАЛЯЕТ (И-1). Боевой ход идёт только с --разрешено (его добавляет демон под
выключателем DOCKER_SWEEP_ENABLED), иначе отказ до первого вызова docker. Каждое
правило безопасности — отдельный случай scripts/test_docker_sweep.py.
Не вызываются никогда: system/volume/image/container prune, rm и rmi с -f/-v,
builder prune -a — они не различают своё и чужое.
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

HOUR = 3600
DAY = 86400
# Выдержка между пометкой тома и удалением: том хранит данные, и «безымянный без ссылок»
# в миг опроса может быть томом контейнера, который сейчас пересоздаётся.
VOLUME_GRACE = 24 * HOUR
PROTECTED_TAGS = ("latest", "previous")
# Теги отката ставит tag-current.sh, чистит их шаг 4 демона со своим счётом «последние N».
ROLLBACK_TAG = re.compile(r"^\d{8}_\d{4}_[0-9a-f]+$")
ANONYMOUS_LABEL = "com.docker.volume.anonymous"
COMPOSE_PROJECT = "com.docker.compose.project"
UNITS = {"B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12}
CLASSES = ("контейнеры", "образы", "тома", "кэш сборки")
MODES = {"--ночь": "ночь", "--давление": "давление", "--после-выката": "после выката"}


def say(text: str) -> None:
    # Закрытый stdout (>/dev/full) не должен ронять ход посреди удаления.
    try:
        print(text, flush=True)
    except OSError:
        sys.stdout = open(os.devnull, "w")


def to_bytes(text: str) -> int:
    match = re.match(r"\s*([\d.]+)\s*([kKMGT]?B)", text or "")
    if not match:
        return 0
    return int(float(match.group(1)) * UNITS[match.group(2).upper()])


def to_mb(nbytes: int) -> int:
    return round(nbytes / 10**6)


def parse_image_time(text: str) -> float:
    # «2026-10-07 13:03:10 +0500 +05»: хвост с именем пояса лишний.
    return datetime.strptime(" ".join(text.split()[:3]), "%Y-%m-%d %H:%M:%S %z").timestamp()


def parse_iso_time(text: str) -> float:
    # docker печатает наносекунды и Z, а fromisoformat принимает не больше микросекунд.
    text = re.sub(r"(\.\d{6})\d*", r"\1", text).replace("Z", "+00:00")
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


@dataclass
class Config:
    now: float
    image_hours: float
    stopped_hours: float
    cache_gb: float
    volume_days: float
    protect: re.Pattern | None
    own: re.Pattern | None
    volumes_on: bool


@dataclass
class Item:
    klass: str
    ident: str
    nbytes: int
    rule: str
    argv: list


def protected(cfg: Config, *names: str) -> bool:
    return cfg.protect is not None and any(cfg.protect.search(n) for n in names if n)


def is_own(cfg: Config, name: str, labels: dict) -> bool:
    # Демон docker общий для всех проектов машины: снимается только то, что названо СВОИМ
    # (compose-проект или имя), а не всё, что не попало в список защиты.
    if cfg.own is None:
        return False
    return bool(cfg.own.search(labels.get(COMPOSE_PROJECT, "")) or cfg.own.search(name))


class DockerDown(Exception):
    pass


def docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=300)


def docker_json(*args: str):
    result = docker(*args)
    if result.returncode:
        raise DockerDown((result.stderr.strip().splitlines() or ["код %d" % result.returncode])[0])
    return json.loads(result.stdout)


# --- отбор ----------------------------------------------------------------------

def image_items(inventory: dict, cfg: Config, hours: float, first_seen: dict) -> list:
    groups: dict = {}
    for row in inventory.get("Images", []):
        groups.setdefault(row["ID"], []).append(row)
    items = []
    for ident, rows in groups.items():
        refs = [f"{r['Repository']}:{r['Tag']}" for r in rows
                if r["Repository"] != "<none>" and r["Tag"] != "<none>"]
        if any(int(r.get("Containers") or 0) for r in rows):
            continue
        if any(r["Tag"] in PROTECTED_TAGS or ROLLBACK_TAG.match(r["Tag"]) for r in rows):
            continue
        if protected(cfg, *refs, ident):
            continue
        # Возраст от первой встречи механизмом: CreatedAt — час сборки у автора, и только
        # что скачанный образ двухлетней сборки ушёл бы в первую же ночь.
        if cfg.now - first_seen.get(ident, cfg.now) < hours * HOUR:
            continue
        size = to_bytes(rows[0].get("UniqueSize") or "") or to_bytes(rows[0].get("Size") or "")
        for target in refs or [ident]:
            items.append(Item("образы", target, size // len(refs or [1]), "образ без контейнера, старше срока",
                              ["rmi", target]))
    return items


def inspect_containers() -> list:
    result = docker("ps", "-a", "-q", "--no-trunc")
    if result.returncode:
        raise DockerDown((result.stderr.strip().splitlines() or ["ps -a"])[0])
    ids = result.stdout.split()
    return docker_json("inspect", *ids) if ids else []


def attached_volumes(row: dict) -> list:
    return [m["Name"] for m in row.get("Mounts") or [] if m.get("Type") == "volume"]


def container_items(cfg: Config, rows: list) -> list:
    items = []
    for row in rows:
        if row["State"]["Status"] not in ("exited", "created", "dead"):
            continue
        name = row["Name"].lstrip("/")
        labels = row["Config"].get("Labels") or {}
        if not is_own(cfg, name, labels) or protected(cfg, name, row["Config"]["Image"]):
            continue
        # Подключённый том — данные: контейнер с ним не снимается, чтобы том не остался без хозяина.
        if attached_volumes(row):
            continue
        finished = row["State"].get("FinishedAt") or ""
        stamp = parse_iso_time(finished) if finished and not finished.startswith("0001") \
            else parse_iso_time(row["Created"])
        if cfg.now - stamp < cfg.stopped_hours * HOUR:
            continue
        # Без -v: анонимные тома контейнера остаются и уходят только классом «тома» с выдержкой.
        items.append(Item("контейнеры", row["Id"], 0, "остановлен, старше срока", ["rm", row["Id"]]))
    return items


def own_attached(cfg: Config, rows: list) -> set:
    return {v for r in rows if is_own(cfg, r["Name"].lstrip("/"), r["Config"].get("Labels") or {})
            for v in attached_volumes(r)}


def volume_candidates(inventory: dict, cfg: Config, own_volumes: set) -> list:
    rows = [v for v in inventory.get("Volumes", [])
            if int(v.get("Links") or 0) == 0 and v["Name"] in own_volumes
            and (ANONYMOUS_LABEL in (v.get("Labels") or "")) and not protected(cfg, v["Name"])]
    if not rows:
        return []
    created = {v["Name"]: parse_iso_time(v["CreatedAt"])
               for v in docker_json("volume", "inspect", *[r["Name"] for r in rows])}
    return [Item("тома", r["Name"], to_bytes(r.get("Size") or ""), "безымянный без ссылок, старше срока",
                 ["volume", "rm", r["Name"]])
            for r in rows if r["Name"] in created and cfg.now - created[r["Name"]] >= cfg.volume_days * DAY]


def cache_item(cfg: Config) -> Item | None:
    for line in docker("system", "df", "--format", "{{json .}}").stdout.splitlines():
        row = json.loads(line)
        if row.get("Type") == "Build Cache":
            reclaim = min(to_bytes(row.get("Reclaimable") or ""), to_bytes(row.get("Size") or ""))
            freed = reclaim - int(cfg.cache_gb * 10**9)
            if freed <= 0:
                return None
            keep = f"{cfg.cache_gb:g}GB"
            return Item("кэш сборки", "build-cache", freed, "кэш последней сборки оставлен",
                        ["builder", "prune", "-f", "--keep-storage", keep])
    return None


# --- метки томов и журнал --------------------------------------------------------

def read_marks(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_marks(path: str, marks: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(marks, handle, ensure_ascii=False)
    os.replace(tmp, path)


def read_history(path: str) -> dict:
    data = read_marks(path)
    return {"образы": data.get("образы") or {}, "тома_свои": data.get("тома_свои") or []}


def open_journal(path: str):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "a", encoding="utf-8")


def journal_row(journal, item: Item, now: float, action: str) -> None:
    row = {"когда": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
           "класс": item.klass, "идентификатор": item.ident, "мб": to_mb(item.nbytes),
           "правило": item.rule, "действие": action}
    journal.write(json.dumps(row, ensure_ascii=False) + "\n")
    journal.flush()


def reclaimed_bytes(output: str) -> int:
    match = re.search(r"Total reclaimed space:\s*(\S+)", output)
    return to_bytes(match.group(1)) if match else 0


def execute(items: list, journal, now: float) -> list:
    done = []
    for item in items:
        try:
            result = docker(*item.argv)
        except subprocess.TimeoutExpired:
            journal_row(journal, item, now, "отказ: предел времени")
            continue
        if result.returncode == 0 and item.klass == "кэш сборки":
            item.nbytes = reclaimed_bytes(result.stdout)   # итог — замер docker, не расчёт
        if result.returncode:
            reason = (result.stderr.strip().splitlines() or ["код %d" % result.returncode])[0]
            journal_row(journal, item, now, f"отказ: {reason}")
        else:
            journal_row(journal, item, now, "удалено")
            done.append(item)
    return done


# --- ход --------------------------------------------------------------------------

def mature_volumes(candidates: list, marks: dict, now: float) -> tuple:
    names = {c.ident for c in candidates}
    kept = {n: t for n, t in marks.items() if n in names}
    for name in names:
        kept.setdefault(name, now)
    ripe = [c for c in candidates if now - kept[c.ident] >= VOLUME_GRACE]
    return kept, ripe


def summarize(plan: dict, verb: str, dry: bool) -> None:
    total_n = total_mb = 0
    for klass in CLASSES:
        items = plan.get(klass, [])
        mb = to_mb(sum(i.nbytes for i in items))
        say(f"{klass}\t{len(items)}\t{mb}\t{verb}")
        total_n += len(items)
        total_mb += mb
    say(f"docker: {'убрал бы' if dry else 'убрано'} {total_n}, {total_mb} МБ")


def arg_value(argv: list, name: str, default):
    return type(default)(argv[argv.index(name) + 1]) if name in argv else default


def sweep(argv: list) -> int:
    modes = [m for m in MODES if m in argv]
    dry = "--dry-run" in argv
    if len(modes) != 1:
        say("docker: нужен один режим --ночь, --давление или --после-выката")
        return 2
    if not dry and "--разрешено" not in argv:
        say("docker: боевой ход выключен (DOCKER_SWEEP_ENABLED=0), ничего не убрано")
        return 2
    own = arg_value(argv, "--свои", "")
    if not dry and modes[0] != "--после-выката" and not own:
        # Пустой список своих значил бы «снимай всё чужое»: отказ до первого вызова docker.
        say("docker: не задан список своих (DOCKER_SWEEP_OWN) — боевой ход отказан, ничего не убрано")
        return 2
    if shutil.which("docker") is None:
        say("docker не установлен — уборка пропущена")
        return 0
    mode = MODES[modes[0]]
    now = arg_value(argv, "--сейчас", time.time())
    protect = arg_value(argv, "--защита", "")
    cfg = Config(
        now=float(now),
        image_hours=arg_value(argv, "--образы-ч", 168.0),
        stopped_hours=arg_value(argv, "--контейнеры-ч", 24.0),
        cache_gb=arg_value(argv, "--кэш-гб", 2.0),
        volume_days=arg_value(argv, "--тома-дней", 14.0),
        protect=re.compile(protect) if protect else None,
        own=re.compile(own) if own else None,
        volumes_on="--тома" in argv)
    image_hours = arg_value(argv, "--образы-давление-ч", 48.0) if mode == "давление" else cfg.image_hours
    journal_path = arg_value(argv, "--журнал", "docker-sweep.jsonl")
    marks_path = arg_value(argv, "--метки", "docker-sweep-marks.json")
    history_path = marks_path + ".история"

    lock = open(journal_path + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        say("docker: другой ход уже идёт — выхожу")
        return 0

    plan: dict = {k: [] for k in CLASSES}
    history = read_history(history_path)
    try:
        if mode != "после выката":
            inventory = docker_json("system", "df", "-v", "--format", "{{json .}}")
            rows = inspect_containers()
            # Первая встреча пишется на каждом боевом ходе независимо от порогов: иначе
            # история копится только когда образ уже подходит под снятие.
            ids = {r["ID"] for r in inventory.get("Images", [])}
            history["образы"] = {i: history["образы"].get(i, cfg.now) for i in ids}
            first_seen = dict(history["образы"])
            plan["контейнеры"] = container_items(cfg, rows)
            plan["образы"] = image_items(inventory, cfg, image_hours, first_seen)
            if cfg.volumes_on:
                present = {v["Name"] for v in inventory.get("Volumes", [])}
                history["тома_свои"] = sorted((set(history["тома_свои"]) | own_attached(cfg, rows)) & present)
            candidates = volume_candidates(inventory, cfg, set(history["тома_свои"])) if cfg.volumes_on else []
            kept, ripe = mature_volumes(candidates, read_marks(marks_path), cfg.now)
            plan["тома"] = ripe
        cache = cache_item(cfg)
        plan["кэш сборки"] = [cache] if cache else []
    except (DockerDown, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        say(f"docker не отвечает или ответил непонятно: {exc} — уборка пропущена")
        return 0

    if dry:
        summarize(plan, "убрал бы", True)
        return 0

    done: dict = {k: [] for k in CLASSES}
    with open_journal(journal_path) as journal:
        for klass in CLASSES:
            done[klass] = execute(plan[klass], journal, cfg.now)
    if mode != "после выката":
        write_marks(history_path, history)
    if cfg.volumes_on and mode != "после выката":
        gone = {i.ident for i in done["тома"]}
        write_marks(marks_path, {n: t for n, t in kept.items() if n not in gone})
    summarize(done, "убрано", False)
    return 0


if __name__ == "__main__":
    sys.exit(sweep(sys.argv[1:]))
