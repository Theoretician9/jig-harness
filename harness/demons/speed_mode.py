#!/usr/bin/env python3
"""Судья режима скорости: сколько задач карты вести одновременно (hr.speed-modes, Ч2).

Владелец выбирает режим (slow/medium/fast/ultra), судья каждые 2 минуты считает
ЭФФЕКТИВНЫЙ: выбранный, ограниченный действующими блокерами (пауза, лимит, выкат,
ротация, мощность, авария, мало задач, файлы). Блокер с «действует: нет» в данных
считается и пишется («не_действуют»), но потолок не режет.
Сторож и launch-plan читают итог, а не пересчитывают: судья один.

    speed_mode.py --замер                       один проход: замер, решение, запись состояния
    speed_mode.py --показать [--коротко]        печать из состояния, без пересчёта
    speed_mode.py --журнал [--за Nч]            лента событий (только чтение, состояние не нужно)
    speed_mode.py --сторожу --последний-пинок N [--предложено "id …"] [--последнее-лишнее M]
                                                одна строка для сторожа (только чтение)
    speed_mode.py --выбран <панель|бот|чат|руки> mode.selected, выбор_ts, полный проход
    общий флаг   --на <ISO 8601 с поясом>       подменные часы (пробы)

Коды: 0 — готово; 1 — ошибка данных или аргументов; 2 — замер упал, либо состояния
нет / оно битое / старше 10 минут («судья скорости не отвечает»).
Публичные функции для импорта: in_work, ceiling_alive, effective.

Двери стенда: SPEED_MODE_DATA, SPEED_PROC_DIR, SPEED_AFFINITY (число доступных ядер), SPEED_DISK_PATH, WORK_QUEUE_BIN,
HARNESS_PANEL_STATE, HARNESS_DEVMAP, HARNESS_LOG_DIR, CLAUDE_PROJECTS_DIR, SPEED_TG_SEND
(скрипт отправки владельцу; стенд без него не шлёт), SPEED_MSG_WINDOW_MIN (окно сообщений).
"""
from __future__ import annotations

import datetime as dt
import fcntl
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import yaml


def _find_root() -> Path:
    # Верно в обеих раскладках (проект и пакет) и для копии судьи под мутацией в дереве-копии.
    for folder in Path(__file__).resolve().parents:
        if (folder / "scripts" / "lib" / "config.py").is_file():
            return folder
    raise RuntimeError("не найден scripts/lib/config.py — корень установки неизвестен")


ROOT = _find_root()
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import config  # noqa: E402
import events  # noqa: E402
import launch_plan_lib  # noqa: E402
import rotation_flag  # noqa: E402

SRC = "harness/demons/speed_mode.py"
JOURNAL = "режим-скорости"
STALE_S = 600
FUTURE_SLACK_S = 5      # допуск часов между писателем состояния и читателем
PULSE_S = 600
SELECTORS = ("панель", "бот", "чат", "руки")
OPEN_STATUSES_OFF = ("test", "done", "принято")
NOT_ANSWERING = "судья скорости не отвечает"


class DataError(Exception):
    """Данные судьи отсутствуют или негодны — код 1."""


class MeasureError(Exception):
    """Замер или источник счёта не прочитан — код 2, состояние не переписывается."""


# ───────────────────────── мелочи ─────────────────────────

def to_iso(moment: float) -> str:
    return dt.datetime.fromtimestamp(moment).astimezone().isoformat(timespec="seconds")


def parse_ts(text) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def num(value: float, digits: int = 2) -> str:
    text = f"{round(value, digits):.{digits}f}"
    # Нули режем только в дробной части: у круглых «60» хвост — значащий.
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")


def fixed(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


def plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def tasks_word(count: int) -> str:
    return f"{count} {plural(count, 'задача', 'задачи', 'задач')}"


def conf_value(key: str, default: str = "") -> str:
    return os.environ.get(key) or config.из_файла(config.путь_harness_conf()).get(key) or default


def conf_float(key: str, default: float) -> float:
    try:
        return float(conf_value(key, str(default)))
    except ValueError:
        return default


def panel_dir() -> Path:
    return Path(os.environ.get("HARNESS_PANEL_STATE") or config.panel_state_dir())


def read_tail_rows(path: Path, needles: tuple, max_bytes: int = 8 << 20) -> list:
    """Строки журнала с одной из подстрок; хвост файла — старше горизонта судье не нужно."""
    try:
        with open(path, "rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - max_bytes))
            text = stream.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = text.splitlines()
    if size > max_bytes:
        lines = lines[1:]
    rows = []
    for line in lines:
        if not any(needle in line for needle in needles):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ───────────────────────── данные ─────────────────────────

def data_path() -> Path:
    return Path(os.environ.get("SPEED_MODE_DATA")
                or Path(__file__).resolve().parent.parent / "config" / "speed-modes.yaml")


def load_data() -> dict:
    path = data_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DataError(f"данные {path} не читаются: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("блокеры"), dict):
        raise DataError(f"в данных {path} нет раздела блокеры")
    silent = sorted(name for name, body in data["блокеры"].items()
                    if not isinstance(body, dict) or "говорить" not in body)
    if silent:
        raise DataError(f"у блокера нет поля «говорить»: {', '.join(silent)}")
    return data


def resolve_mode(data: dict) -> tuple[str, bool]:
    """(имя режима, знакомо ли значение). Пусто — умолчание без ошибки."""
    raw = conf_value("SPEED_MODE").strip()
    if not raw:
        return data["по_умолчанию"], True
    if raw in data["режимы"]:
        return raw, True
    return data["по_умолчанию"], False


def step_below(steps: list, value: int) -> int:
    return max([s for s in steps if s < value], default=1)


def step_above(steps: list, value: int):
    return min([s for s in steps if s > value], default=None)


# ───────────────────────── счёт задач (3.2) ─────────────────────────

def load_cards() -> list:
    path = Path(os.environ.get("HARNESS_DEVMAP") or ROOT / "dev-map.yaml")
    try:
        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        tree = yaml.load(path.read_text(encoding="utf-8"), Loader=loader) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise MeasureError(f"карта {path} не читается: {exc}") from exc
    return [(line.get("id"), card) for line in tree.get("lines") or []
            for card in line.get("tasks") or [] if isinstance(card, dict)]


def meta_index(transcripts, session) -> dict:
    """toolUseId → файл meta помощника сессии."""
    found = {}
    if transcripts is None or not session:
        return found
    for meta in (Path(transcripts) / str(session) / "subagents").glob("agent-*.meta.json"):
        try:
            found[json.loads(meta.read_text(encoding="utf-8")).get("toolUseId")] = meta
        except (OSError, ValueError):
            continue
    return found


def launch_state(row: dict, now: float, data: dict, transcripts, hung_s: float, cache: dict) -> str:
    """alive | phantom | hung: признак жизни открытого запуска."""
    session = row.get("session")
    if session not in cache:
        cache[session] = meta_index(transcripts, session)
    meta = cache[session].get((row.get("data") or {}).get("вызов"))
    if meta is None:
        # Хук мог записать launch ok и отказать соседним хуком — помощник не стартовал.
        return "alive" if now - row["_at"] < data["старт_ждать_с"] else "phantom"
    transcript = meta.with_name(meta.name.replace(".meta.json", ".jsonl"))
    try:
        written = (transcript if transcript.exists() else meta).stat().st_mtime
    except OSError:
        return "hung"
    return "alive" if now - written <= hung_s else "hung"


def task_of(row: dict, by_call: dict, card_ids: set, depth: int = 0) -> str:
    mark = launch_plan_lib.mark_task(row)
    if mark:
        return mark if mark in card_ids else f"без-карточки:{mark}"
    body = row.get("data") or {}
    parent = body.get("родитель")
    if parent and depth < 3:
        parent_row = by_call.get(parent)
        return task_of(parent_row, by_call, card_ids, depth + 1) if parent_row else f"без-метки:{parent}"
    return f"без-метки:{body['вызов']}"


def count_launches(now: float, data: dict, card_ids: set) -> dict:
    stall = load_script("stall_in_log", "stall-in-log.py")
    rows = read_tail_rows(Path(events.log_dir()) / "вызовы-агентов.jsonl",
                          ("agent.launch", "agent.finish"))
    finished, by_call, launches = set(), {}, []
    for row in rows:
        call = (row.get("data") or {}).get("вызов")
        at = parse_ts(row.get("ts"))
        if not call or at is None:
            continue
        row["_at"] = at
        if row.get("event") == "agent.finish":
            finished.add(call)
        elif row.get("event") == "agent.launch" and row.get("outcome") == "ok":
            by_call[call] = row
            launches.append(row)
    horizon = data["открытый_запуск_макс_мин"] * 60
    transcripts, hung_s, cache = stall.project_transcripts(), stall.hung_minutes() * 60, {}
    tasks, phantoms = {}, 0
    for row in launches:
        call = row["data"]["вызов"]
        if call in finished or now - row["_at"] > horizon:
            continue
        state = launch_state(row, now, data, transcripts, hung_s, cache)
        if state == "phantom":
            phantoms += 1
        elif state == "alive":
            tasks.setdefault(task_of(row, by_call, card_ids), []).append(call)
    return {"tasks": tasks, "phantoms": phantoms}


def run_work_queue() -> tuple[list, list, int]:
    """(готовые id по порядку, строки «битая зависимость», число зависимостей в карте)."""
    queue = os.environ.get("WORK_QUEUE_BIN") or str(ROOT / "scripts" / "work-queue.py")
    devmap = os.environ.get("HARNESS_DEVMAP") or str(ROOT / "dev-map.yaml")
    try:
        done = subprocess.run([sys.executable, queue, devmap, "--готовые", "--битые"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MeasureError(f"work-queue не ответил: {exc}") from exc
    if done.returncode != 0:
        raise MeasureError(f"work-queue код {done.returncode}: {done.stderr.strip()[-200:]}")
    lines = done.stdout.splitlines()
    try:
        count = int(lines[0].split(":")[1])
    except (IndexError, ValueError) as exc:
        raise MeasureError("work-queue: нет строки «готовых: N»") from exc
    ready, broken, deps = lines[1:1 + count], [], 0
    for line in lines[1 + count:]:
        if line.startswith("битая зависимость:"):
            broken.append(line.split(": ", 1)[1])
        elif line.startswith("зависимостей в карте:"):
            deps = int(line.split(":")[1])
    return ready, broken, deps


def gather(now: float, data: dict, want_ready: bool = True) -> dict:
    cards = load_cards()
    line = config.активная_линия()
    mine = [card for line_id, card in cards if not line or line_id == line]
    status = {card.get("id"): card.get("status") for card in mine}
    launches = count_launches(now, data, {card.get("id") for _, card in cards})
    approval = load_script("spec_approval", "spec_approval.py")
    partial = approval.уровень() == approval.ЧАСТИЧНЫЙ
    records = approval.записи() if partial else []
    # Один записи() на проход; судья журнал одобрений только читает (3.1).
    waiting = {i for i in status if partial and approval.отправлена(i, records)
               and not approval.одобрена(i, records)}
    launched = set(launches["tasks"])
    # В работе — то, что ведёт живой помощник: статус wip без помощника — брошенная карточка,
    # и счёт по статусам при 3 живых велел смене при ультра не брать новых.
    work = sorted(launched)
    idle_waiting = waiting - launched
    snap = {"in_work": work, "launched": launches["tasks"], "phantoms": launches["phantoms"],
            "no_label": sum(1 for i in work if i.startswith("без-метки:")),
            "waiting_count": sum(1 for i in idle_waiting if status.get(i) not in OPEN_STATUSES_OFF),
            "take": [], "broken": [], "deps": 0, "spec_limit": False}
    if not want_ready:
        return snap
    ready, snap["broken"], snap["deps"] = run_work_queue()
    take = [i for i in ready if i not in work and i not in idle_waiting]
    snap["spec_limit"] = partial and snap["waiting_count"] >= data["спек_ждут_владельца_максимум"]
    if snap["spec_limit"]:
        take = [i for i in take if not (status.get(i) == "plan" and approval.одобрена(i, records) is None)]
    snap["take"] = take
    return snap


# ───────────────────────── замер и факты ─────────────────────────

def proc_dir() -> Path:
    return Path(os.environ.get("SPEED_PROC_DIR") or "/proc")


def disk_used_pct() -> float:
    stat = os.statvfs(os.environ.get("SPEED_DISK_PATH") or "/")
    used = stat.f_blocks - stat.f_bfree
    return 100.0 * used / (used + stat.f_bavail) if used + stat.f_bavail else 0.0


def read_meminfo(proc: Path) -> dict:
    try:
        text = (proc / "meminfo").read_text(encoding="utf-8")
    except OSError:
        return {}
    return {name: float(rest.split()[0]) for name, _, rest in
            (line.partition(":") for line in text.splitlines()) if rest.split()}


def read_pressure(proc: Path):
    try:
        for field in (proc / "pressure" / "cpu").read_text(encoding="utf-8").splitlines()[0].split():
            if field.startswith("avg300="):
                return float(field[7:])
    except (OSError, ValueError, IndexError):
        pass
    return None


def rounded(value):
    return None if value is None else round(value, 1)


def available_cores() -> int:
    """Ядра, на которых могут работать процессы судьи: cpuset cgroup уже числа строк cpuN."""
    forced = os.environ.get("SPEED_AFFINITY")
    return int(forced) if forced else len(os.sched_getaffinity(0))


def cpu_shares(prev_cpu, cpu: dict) -> dict:
    """Доли занятости, ожидания диска и кражи за интервал между снимками; None — интервала нет.

    Занятость — всё, кроме idle и iowait (кража входит): loadavg считает очередь коротких
    процессов и на живой машине врёт о загрузке в разы."""
    unknown = {"busy": None, "iowait": None, "steal": None}
    if not prev_cpu or not all(key in prev_cpu for key in cpu):
        return unknown
    delta = {key: cpu[key] - prev_cpu[key] for key in cpu}
    if delta["всего"] <= 0 or min(delta.values()) < 0:
        return unknown
    total = delta["всего"]
    return {"busy": 100.0 * (total - delta["idle"] - delta["iowait"]) / total,
            "iowait": 100.0 * delta["iowait"] / total, "steal": 100.0 * delta["steal"] / total}


def measure(prev_cpu) -> dict:
    proc = proc_dir()
    try:
        load5 = float((proc / "loadavg").read_text(encoding="utf-8").split()[1])
        stat_lines = (proc / "stat").read_text(encoding="utf-8").splitlines()
        fields = [int(x) for x in stat_lines[0].split()[1:9]]
    except (OSError, ValueError, IndexError) as exc:
        raise MeasureError(f"замер не прочитан: {exc}") from exc
    # Ядра считаем по /proc/stat, а не os.cpu_count(): так их подменяет стенд.
    cores = sum(1 for line in stat_lines if line.startswith("cpu") and line[3:4].isdigit())
    if cores < 1:
        raise MeasureError("в stat нет строк cpuN")
    cpu = {"steal": fields[7], "всего": sum(fields), "idle": fields[3], "iowait": fields[4]}
    shares = cpu_shares(prev_cpu, cpu)
    mem = read_meminfo(proc)
    free_pct = 100.0 * mem["MemAvailable"] / mem["MemTotal"] if mem.get("MemTotal") else None
    swap_pct = (100.0 * (mem["SwapTotal"] - mem["SwapFree"]) / mem["SwapTotal"]
                if mem.get("SwapTotal") else 0.0)
    available = available_cores()
    return {"raw": {"load": load5 / cores, **shares, "free": free_pct,
                    "swap": swap_pct, "disk": disk_used_pct()},
            "state": {"нагрузка_на_ядро": round(load5 / cores, 2), "занято_pct": rounded(shares["busy"]),
                      "ожидание_диска_pct": rounded(shares["iowait"]), "кража_pct": rounded(shares["steal"]),
                      "память_pct": None if free_pct is None else round(free_pct, 1),
                      "своп_pct": round(swap_pct, 1), "давление_cpu_300": read_pressure(proc),
                      "диск_pct": round(disk_used_pct(), 1), "ядер": cores,
                      **({"ядер_доступно": available} if available < cores else {})},
            "cpu": cpu}


def deploy_running() -> bool:
    """Выкат по занятости лока БЕЗ захвата: stat лока и его inode в locks; файл не открывается."""
    lock = Path(config.log_dir()) / "locks" / "deploy.lock"
    try:
        inode = str(os.stat(lock).st_ino)
        text = (proc_dir() / "locks").read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        parts = line.split()
        if "FLOCK" in parts and any(p.count(":") == 2 and p.rsplit(":", 1)[1] == inode for p in parts):
            return True
    return False


def facts() -> dict:
    pause = load_script("pause_script", "pause.py")
    limit = load_script("limit_mark", "limit_mark.py")
    # Блокер «ротация»: флаг живой и роль ещё не передана преемнику (этапы ждёт/идёт — минуты передачи).
    # На этапе «дорабатывает» задачи берёт преемник, старая лишь доводит помощников: блокер держал бы потолок
    # на 0 часами (замер 21.11: 41 % времени). ROTATE_SUCCESSOR=0 — как до преемника: любой живой флаг блокирует.
    # Единственное место цепочки скорости, где читается флаг: план запуска и бюджет судят по этому ответу.
    flag_kind, flag = rotation_flag.state()
    # Флаг без разобранного этапа (оборванная запись) блокирует: молчаливая смена не получает зелёный свет.
    rotation = flag_kind == "живой" and (flag.get("этап") != rotation_flag.FINISHING
                                         or not rotation_flag.successor_enabled())
    return {"пауза": pause.стоит() is not None, "лимит": limit.лимит_держится(),
            "выкат": deploy_running(), "ротация": rotation}


def file_warnings(now: float) -> int:
    rows = read_tail_rows(Path(events.log_dir()) / "заявки-на-файлы.jsonl", ("file.claim.warn",))
    return sum(1 for r in rows if r.get("event") == "file.claim.warn"
               and 0 <= now - (parse_ts(r.get("ts")) or 0) <= 1800)


# ───────────────────────── блокеры и решение ─────────────────────────

def sign_record(prev_sign, present: bool, now: float) -> dict:
    if prev_sign and prev_sign.get("есть") == present and parse_ts(prev_sign.get("с")) is not None:
        return prev_sign
    return {"есть": present, "с": to_iso(now)}


def track(prev_rec, prev_cand, present: bool, cfg: dict, now: float, extra: dict) -> tuple:
    """Гистерезис блокера: (действующий, кандидат, включился, выключился)."""
    sign = sign_record((prev_rec or prev_cand or {}).get("признак"), present, now)
    since = parse_ts(sign["с"])
    active, started, ended = prev_rec is not None, False, False
    if not active and present and now - since >= cfg["держится_мин"] * 60:
        active, started = True, True
    elif active and not present and now - since >= cfg["отпускает_мин"] * 60:
        active, ended = False, True
    if active:
        return {**(prev_rec or {"с": to_iso(now)}), "признак": sign, **extra}, None, started, ended
    if present:
        return None, {"с": sign["с"], "признак": sign, **extra}, started, ended
    return None, None, started, ended


def power_sign(raw: dict, cfg: dict) -> tuple[bool, str]:
    """Мощность занята: CPU, ожидание диска или кража выше порога; без интервала — не занята."""
    limits = (("busy", "занято", conf_float("CPU_BUSY_MAX_PCT", 90)),
              ("iowait", "ожидание диска", conf_float("CPU_IOWAIT_MAX_PCT", 20)),
              ("steal", "кража", cfg["кража_выше_pct"]))
    reasons = [f"{label} {num(raw[key], 1)} %, порог {num(limit, 1)} %"
               for key, label, limit in limits if raw[key] is not None and raw[key] > limit]
    return bool(reasons), ", ".join(reasons)


def power_quiet(raw: dict, cfg: dict) -> bool:
    """«Тихо» — занятость известна и ниже порога тишины, признака мощности нет."""
    return (raw["busy"] is not None and raw["busy"] < conf_float("CPU_BUSY_QUIET_PCT", 75)
            and not power_sign(raw, cfg)[0])


def accident_sign(raw: dict, cfg: dict) -> tuple[bool, str]:
    reasons = []
    if raw["load"] > cfg["нагрузка_на_ядро_выше"]:
        reasons.append(f"нагрузка {num(raw['load'])}/ядро, порог {fixed(cfg['нагрузка_на_ядро_выше'])}")
    if raw["free"] is not None and raw["free"] < conf_float("MEM_MIN_FREE_PCT", 10):
        reasons.append(f"свободной памяти {num(raw['free'], 1)} %")
    if raw["swap"] > conf_float("SWAP_MAX_PCT", 85):
        reasons.append(f"своп {num(raw['swap'], 1)} %")
    if raw["disk"] > cfg["диска_выше_pct"]:
        reasons.append(f"диск {num(raw['disk'], 1)} %")
    return bool(reasons), ", ".join(reasons)


def ladder(lad: dict, sign: dict, quiet: dict, cfg: dict, steps: list, tasks: int, work_count: int, now: float):
    """Лестница мощности (4.2): шаг вниз — по признаку, шаг вверх — только в «тихо» (гистерезис 4.4)."""
    last_step = parse_ts(lad["шаг_ts"]) or 0
    ceiling, raised = lad["потолок"], False
    if sign["есть"] and now - max(parse_ts(sign["с"]), last_step) >= cfg["держится_мин"] * 60:
        ceiling = step_below(steps, ceiling if ceiling is not None else max(1, work_count))
        lad = {"потолок": ceiling, "шаг_ts": to_iso(now)}
    elif (quiet["есть"] and ceiling is not None
          and now - max(parse_ts(quiet["с"]), last_step) >= cfg["отпускает_мин"] * 60):
        above = step_above(steps, ceiling)
        lad = {"потолок": None if above is None or above >= tasks else above, "шаг_ts": to_iso(now)}
        raised = True
    return lad, raised


def short_reason(name: str, rec: dict) -> str:
    return f"{name}: {rec['причина']}"


def acts(blocker_cfg: dict) -> bool:
    """Выключатель данными «действует: нет»: признак считается и пишется, потолок не режет.

    Правило владельца 11.11 «включай режим средний и не снижай»: снижают только пауза и 429.
    """
    return blocker_cfg.get("действует", "да") != "нет"


def decide(prev: dict | None, now: float, data: dict, snap: dict, meas: dict, fact: dict,
           mode: str) -> dict:
    prev = prev or {}
    tasks = data["режимы"][mode]["задач"]
    steps = data["ступени"]
    work = snap["in_work"]
    work_count = len(work)
    cfg = data["блокеры"]
    acting = {name for name, body in cfg.items() if acts(body)}
    # Выключенный блокер хранит гистерезис рядом: включат снова — продолжит, а не начнёт с нуля.
    before = {**(prev.get("не_действуют") or {}), **(prev.get("блокеры") or {})}
    cand_before = prev.get("кандидаты") or {}
    block, cand, ceilings = {}, {}, {}

    def immediate(name: str, ceiling: int, why: str) -> None:
        block[name] = {"с": (before.get(name) or {}).get("с") or to_iso(now),
                       "потолок": ceiling, "причина": f"{why} — потолок {ceiling}"}
        ceilings[name] = ceiling

    if fact["пауза"]:
        immediate("пауза", 0, "пауза владельца")
    if fact["лимит"]:
        immediate("лимит", 0, "лимит подписки по факту 429")
    if fact["выкат"]:
        immediate("выкат", work_count, "идёт выкат, новых задач нет")
    if fact["ротация"]:
        immediate("ротация", work_count, "ротация смены, новых задач нет")

    few = work_count + len(snap["take"])
    if few < tasks:
        why = (f"{snap['waiting_count']} {plural(snap['waiting_count'], 'спека ждёт', 'спеки ждут', 'спек ждут')}"
               f" твоего «да», новых не начинаю" if snap["spec_limit"] else f"в карте готово только {len(snap['take'])}")
        immediate("мало_задач", few, why)

    files_cfg = cfg["файлы"]
    files_count = file_warnings(now)
    rec, cand_rec, _, _ = track(
        before.get("файлы"), cand_before.get("файлы"), files_count >= files_cfg["предупреждений_за_30_мин"],
        files_cfg, now, {"замер": {"предупреждений": files_count}})
    if rec:
        # Потолок ставится при включении и дальше не пересчитывается: иначе блокер опускал бы сам себя.
        rec["потолок"] = rec.get("потолок", step_below(steps, work_count))
        rec["причина"] = f"{files_count} заявок на файлы за 30 мин — потолок {rec['потолок']}"
        block["файлы"], ceilings["файлы"] = rec, rec["потолок"]
    elif cand_rec:
        cand["файлы"] = {**cand_rec, "причина": f"{files_count} заявок на файлы за 30 мин"}

    # Авария раньше лестницы: её снятие ставит шаг_ts, и подъём идёт по ступеням, не прыжком.
    acc_cfg = cfg["авария"]
    acc_on, acc_why = accident_sign(meas["raw"], acc_cfg)
    acc, acc_cand, acc_started, acc_ended = track(
        before.get("авария"), cand_before.get("авария"), acc_on, acc_cfg, now, {"замер": meas["state"]})
    # Выключенная авария не режет и через лестницу мощности.
    steers = "авария" in acting
    old_power = before.get("мощность") or {}
    lad = {"потолок": old_power.get("потолок"), "шаг_ts": old_power.get("шаг_ts")}
    raised_at = prev.get("поднят_ts")
    if acc_started and steers:
        lad = {"потолок": 1, "шаг_ts": to_iso(now)}
    if acc_ended and steers:
        lad["шаг_ts"] = to_iso(now)
        raised_at = to_iso(now)
    if acc:
        acc["потолок"] = acc_cfg["потолок"]
        acc["причина"] = f"{acc_why} — потолок {acc['потолок']}"
        block["авария"], ceilings["авария"] = acc, acc["потолок"]
    elif acc_cand:
        cand["авария"] = acc_cand
        acc_cand["причина"] = acc_why

    pow_cfg = cfg["мощность"]
    pow_on, pow_why = power_sign(meas["raw"], pow_cfg)
    sign = sign_record((old_power or cand_before.get("мощность") or {}).get("признак"), pow_on, now)
    quiet = sign_record(old_power.get("тихо"), power_quiet(meas["raw"], pow_cfg), now)
    if acc and steers:
        # Пока авария действует, лестница стоит: подъёмы за её спиной не «отрабатываются».
        lad, rose = {"потолок": 1, "шаг_ts": to_iso(now)}, False
    else:
        lad, rose = ladder(lad, sign, quiet, pow_cfg, steps, tasks, work_count, now)
    if rose:
        raised_at = to_iso(now)
    if lad["потолок"] is not None:
        block["мощность"] = {"с": old_power.get("с") or to_iso(now), "потолок": lad["потолок"],
                             "шаг_ts": lad["шаг_ts"], "признак": sign, "тихо": quiet, "замер": meas["state"],
                             "причина": f"{pow_why or 'занятость снизилась, жду отпускания'} — потолок {lad['потолок']}"}
        ceilings["мощность"] = lad["потолок"]
    elif pow_on:
        cand["мощность"] = {"с": sign["с"], "признак": sign, "замер": meas["state"], "причина": pow_why}

    muted = {name: block.pop(name) for name in list(block) if name not in acting}
    eff = max(0, min([tasks] + [ceiling for name, ceiling in ceilings.items() if name in acting]))
    binding = min(block.items(), key=lambda item: item[1]["потолок"], default=None)
    reason = short_reason(*binding) if binding and binding[1]["потолок"] < tasks else "блокеров нет"
    return {"tasks": tasks, "eff": eff, "block": block, "muted": muted, "cand": cand,
            "reason": reason, "raised_at": raised_at}


def fill_quota(eff: int, work_count: int, take: list, raw: dict, data: dict) -> tuple[int, str]:
    """Сколько задач сторож может попросить взять сейчас (6.2) и почему."""
    short = eff - work_count
    if short <= 0:
        return 0, "недобора нет"
    if not take:
        return 0, "готовых задач нет"
    pow_cfg = data["блокеры"]["мощность"]
    busy = "занятость неизвестна" if raw["busy"] is None else f"занято {num(raw['busy'], 0)} %"
    if not acts(pow_cfg):
        return min(short, len(take)), f"{busy}, мощность не действует — добираю весь недобор"
    if power_sign(raw, pow_cfg)[0]:
        return 0, f"не добираю — кандидат мощности, {busy}"
    if power_quiet(raw, pow_cfg):
        return min(short, len(take)), f"{busy} — добираю весь недобор"
    return min(short, data["сторож"]["в_зоне_добирать_по"], len(take)), f"добираю по одной — {busy}"


# ───────────────────────── сообщения владельцу (7.2) ─────────────────────────

# Техническое «почему» блокера владельцу надо обернуть в простые слова.
WHY_PREFIX = {"мощность": "сервер загружен", "авария": "авария на сервере"}
BACK_WORDS = {"мощность": "сервер разгрузился", "авария": "сервер разгрузился"}
SEND_TIMEOUT_S = 30


def tasks_dative(count: int) -> str:
    return f"{count} {plural(count, 'задаче', 'задачи', 'задач')}"


def binding_blocker(verdict: dict):
    """Имя блокера с наименьшим потолком, если он режет ниже выбранного; иначе None."""
    name, rec = min(verdict["block"].items(), key=lambda item: item[1]["потолок"], default=(None, None))
    return name if rec and rec["потолок"] < verdict["tasks"] else None


def message_text(changes: int, old_blocker, eff: int, mode: str, data: dict, verdict: dict, now: float) -> str:
    tasks, binding = verdict["tasks"], binding_blocker(verdict)
    if binding is None:
        back = BACK_WORDS.get(old_blocker, "помехи ушли")
        text = f"Скорость: вернул «{data['режимы'][mode]['имя']}», {tasks_word(tasks)} — {back}."
    else:
        detail = verdict["block"][binding]["причина"].rsplit(" — потолок", 1)[0]
        why = f"{WHY_PREFIX[binding]} ({detail})" if binding in WHY_PREFIX else detail
        head = "стою" if eff == 0 else f"работаю по {tasks_dative(eff)} вместо {tasks}"
        text = f"Скорость: {head} — {why}. Верну сам."
    if changes >= 2:
        text += f" Пока ждал, режим менялся {changes} {plural(changes, 'раз', 'раза', 'раз')}."
    return text


def plan_message(prev: dict, now: float, data: dict, mode: str, verdict: dict, window_s: float):
    """(запись «сказано» без отправки, запись после доставки или None, метка для mode.change).

    «Сказано» — последнее, что владелец услышал: с ним сравнивается ТЕКУЩЕЕ состояние, поэтому
    несколько смен за окно дают одно сообщение с итогом, а возврат к сказанному — ни одного.
    """
    eff, binding = verdict["eff"], binding_blocker(verdict)
    told = dict(prev.get("сказано") or {"эфф": prev.get("эффективный", eff), "режим": mode, "ts": None,
                                        "блокер": None, "смен": 0})
    if prev.get("эффективный", eff) != eff:
        told["смен"] += 1
    if told["режим"] != mode:
        # Смену режима владельцем сообщает диспетчер, судья не дублирует.
        return {**told, "эфф": eff, "режим": mode, "блокер": binding, "смен": 0}, None, "нет"
    if eff == told["эфф"]:
        return {**told, "смен": 0}, None, "нет"
    if binding is None and told["блокер"] is not None and not acts(data["блокеры"].get(told["блокер"], {})):
        # Сказанный блокер выключили данными: возврат скорости — не новость, владелец о нём не просил.
        return {**told, "эфф": eff, "блокер": None, "смен": 0}, None, "нет"
    if binding is not None and data["блокеры"][binding]["говорить"] != "да":
        return told, None, "нет"
    sent_at = parse_ts(told["ts"])
    if sent_at is not None and now - sent_at < window_s:
        return told, None, "подавлено: окно"
    return told, {**told, "эфф": eff, "ts": to_iso(now), "блокер": binding, "смен": 0}, "да"


def deliver(text: str) -> tuple[str, bool]:
    """(исход для журнала, доставлено ли). Стенд без своей двери владельцу не пишет."""
    door = os.environ.get("SPEED_TG_SEND")
    if not door and os.environ.get("HARNESS_PANEL_STATE"):
        return "skipped", True
    command = ["bash", door or str(ROOT / "scripts" / "tg_send.sh")]
    try:
        done = subprocess.run(command + [text], capture_output=True, text=True, timeout=SEND_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return "error", False
    return ("ok", True) if done.returncode == 0 else ("error", False)


def tell_owner(prev: dict, now: float, data: dict, mode: str, verdict: dict):
    """Решает и шлёт сообщение о смене эффективного; возвращает (запись «сказано», метка)."""
    window_s = conf_float("SPEED_MSG_WINDOW_MIN", data["сообщения"]["не_чаще_мин"]) * 60
    told, announce, said = plan_message(prev, now, data, mode, verdict, window_s)
    if announce is None:
        return told, said
    old_blocker = told["блокер"]
    text = message_text(told["смен"], old_blocker, verdict["eff"], mode, data, verdict, now)
    outcome, delivered = deliver(text)
    record("owner.told", outcome, {"текст": text}, now)
    # Недоставленное остаётся несказанным: следующий проход попробует снова.
    return (announce, said) if delivered else (told, "ошибка отправки")


# ───────────────────────── проход ─────────────────────────

def record(event: str, outcome: str, body: dict, now: float) -> None:
    events.emit(SRC, event, outcome, journal="режим-скорости", data=body, at=to_iso(now))


def read_state(path: Path):
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def write_state(path: Path, state: dict) -> None:
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def state_lock(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "скорость.json.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def blocker_events(prev: dict, verdict: dict, meas: dict, now: float) -> None:
    before = set((prev.get("блокеры") or {}))
    for name in sorted(set(verdict["block"]) - before):
        record("blocker.on", "ok", {"блокер": name, "замер": meas["state"],
                                  "порог": verdict["block"][name]["причина"], "держался_мин": 0}, now)
    for name in sorted(before - set(verdict["block"])):
        record("blocker.off", "ok", {"блокер": name, "замер": meas["state"],
                                   "порог": (prev["блокеры"][name] or {}).get("причина"),
                                   "держался_мин": round((now - (parse_ts(prev["блокеры"][name].get("с")) or now)) / 60)}, now)


def task_events(prev: dict, snap: dict, now: float) -> None:
    old = set(prev.get("в_работе") or [])
    for task in sorted(set(snap["in_work"]) - old):
        origin = "без-метки" if task.startswith("без-") else "метка"
        record("task.taken", "ok", {"id": task, "откуда": origin,
                                  "вызовы": snap["launched"].get(task, [])}, now)
    for task in sorted(old - set(snap["in_work"])):
        record("task.released", "ok", {"id": task, "откуда": "в_работе", "вызовы": []}, now)


def run_pass(now: float, who: str | None = None) -> int:
    data = load_data()
    folder = panel_dir()
    with state_lock(folder):
        try:
            return judge_pass(now, who, data, folder / "скорость.json")
        except Exception as exc:  # любой сбой прохода: состояние не трогаем, судья не молчит
            why = str(exc) if isinstance(exc, MeasureError) else f"{type(exc).__name__}: {exc}"
            record("mode.decision", "error", {"причина": why}, now)
            print(f"проход упал: {why}", file=sys.stderr)
            return 2


def judge_pass(now: float, who: str | None, data: dict, path: Path) -> int:
    prev = read_state(path)
    meas = measure((prev or {}).get("cpu_прошлый"))
    snap = gather(now, data)
    fact = facts()
    mode, known = resolve_mode(data)
    verdict = decide(prev, now, data, snap, meas, fact, mode)
    eff, work = verdict["eff"], snap["in_work"]
    quota, quota_why = fill_quota(eff, len(work), snap["take"], meas["raw"], data)
    told, said = tell_owner(prev or {}, now, data, mode, verdict)
    selected_changed = bool(prev) and (prev.get("выбранный") != mode)
    chose = who is not None or selected_changed
    state = {
        "ts": to_iso(now), "выбранный": mode, "выбранный_задач": verdict["tasks"],
        "эффективный": eff, "в_работе": work, "готовых": len(snap["take"]),
        "ждут_владельца": snap["waiting_count"], "к_взятию": snap["take"],
        "фантомов": snap["phantoms"], "без_метки": snap["no_label"],
        "зависимостей": snap["deps"], "битые": snap["broken"],
        "блокеры": verdict["block"], "не_действуют": verdict["muted"], "кандидаты": verdict["cand"],
        "поднят_ts": verdict["raised_at"],
        "выбор_ts": to_iso(now) if chose else (prev or {}).get("выбор_ts"),
        "добор": quota, "добор_почему": quota_why,
        "замер": meas["state"], "cpu_прошлый": meas["cpu"], "причина": verdict["reason"],
        "сказано": told,
    }
    outcome = "ok" if known else "error"
    fingerprint = json.dumps([mode, eff, work, sorted(verdict["block"]), state["готовых"], quota, outcome],
                             ensure_ascii=False)
    due = (not prev or prev.get("решение_отпечаток") != fingerprint
           or now - (parse_ts(prev.get("решение_ts")) or 0) >= PULSE_S or not known)
    state["решение_ts"] = to_iso(now) if due else prev.get("решение_ts")
    state["решение_отпечаток"] = fingerprint
    # Состояние раньше событий: сторож и launch-plan читают его, журнал может задержать.
    write_state(path, state)
    if prev:
        if chose:
            record("mode.selected", "ok", {"было": prev.get("выбранный"), "стало": mode,
                                         "кто": who or "неизвестно"}, now)
        if prev.get("эффективный") != eff:
            record("mode.change", "ok", {"было": prev.get("эффективный"), "стало": eff,
                                       "причина": verdict["reason"], "сказано": said}, now)
        blocker_events(prev, verdict, meas, now)
        task_events(prev, snap, now)
    if due:
        body = {"выбранный": mode, "выбранный_задач": verdict["tasks"], "эффективный": eff,
                "было": (prev or {}).get("эффективный"), "в_работе": work, "взято": len(work),
                "готовых": len(snap["take"]), "без_метки": snap["no_label"],
                "фантомов": snap["phantoms"], "ждут_владельца": snap["waiting_count"],
                "блокеры": {name: rec.get("замер") or rec["причина"] for name, rec in verdict["block"].items()},
                "не_действуют": {name: rec["причина"] for name, rec in verdict["muted"].items()},
                "замер": meas["state"], "добор": quota, "причина": verdict["reason"]}
        record("mode.decision", outcome, body if known else {**body, "ошибка": "незнакомый SPEED_MODE"}, now)
    return 0


# ───────────────────────── чтение состояния ─────────────────────────

STATE_FIELDS = ("выбранный", "выбранный_задач", "в_работе", "блокеры", "кандидаты", "к_взятию", "добор",
                "добор_почему", "причина", "готовых", "ждут_владельца", "фантомов", "зависимостей", "битые")


def fresh_state(now: float):
    """Состояние, которому можно верить: свежее с обеих сторон и со всеми полями, что печатают читатели."""
    st = read_state(panel_dir() / "скорость.json")
    ts = parse_ts((st or {}).get("ts"))
    if st is None or ts is None or not -FUTURE_SLACK_S <= now - ts <= STALE_S:
        return None
    eff = st.get("эффективный")
    if isinstance(eff, bool) or not isinstance(eff, int) or any(key not in st for key in STATE_FIELDS):
        return None
    return st


def in_work(now: float | None = None) -> list:
    return gather(time.time() if now is None else now, load_data(), want_ready=False)["in_work"]


def ceiling_alive(now: float | None = None):
    st = fresh_state(time.time() if now is None else now)
    if st is None:
        return None
    try:
        data = load_data()
    except DataError:
        return None
    if "авария" in (st.get("блокеры") or {}):
        return data["живых_при_аварии"]
    fits = [m for m in data["режимы"].values() if m["задач"] >= st["эффективный"]]
    return min(fits, key=lambda m: m["задач"])["живых_потолок"]


def effective(now: float | None = None):
    st = fresh_state(time.time() if now is None else now)
    return None if st is None else st["эффективный"]


def pinned_lines(st: dict) -> list:
    gauge = st.get("замер") or {}
    if "ядер_доступно" not in gauge:
        return []
    return [f"привязка к {gauge['ядер_доступно']} из {gauge['ядер']} ядер"]


def show(st: dict, data: dict, short: bool) -> list:
    name = data["режимы"][st["выбранный"]]["имя"]
    work = st["в_работе"]
    lines = []
    if short:
        why = "" if st["эффективный"] >= st["выбранный_задач"] else f" — {st['причина']}"
        lines.append(f"Режим «{name}»: {tasks_word(st['выбранный_задач'])} одновременно. "
                     f"Сейчас могу {st['эффективный']}{why}.")
        lines.append(f"В работе {len(work)}, {st['добор_почему']}.")
        return lines + pinned_lines(st)
    lines += [f"выбранный: {st['выбранный']} ({st['выбранный_задач']})",
              f"эффективный: {st['эффективный']}",
              f"в работе: {len(work)}" + (f" — {' '.join(work)}" if work else ""),
              f"готовых: {st['готовых']}", f"ждут владельца: {st['ждут_владельца']}",
              f"фантомов: {st['фантомов']}",
              f"добор: {st['добор']} — {st['добор_почему']}"]
    lines += [f"блокер {n}: {rec['причина']}" for n, rec in st["блокеры"].items()]
    lines += [f"кандидат {n}: {rec['причина']} — жду {wait_minutes(n, rec, data, st)} мин"
              for n, rec in st["кандидаты"].items()]
    lines += pinned_lines(st)
    lines.append(f"зависимостей в карте: {st['зависимостей']}")
    lines += [f"битая зависимость: {line}" for line in st["битые"]]
    return lines


def wait_minutes(name: str, rec: dict, data: dict, st: dict) -> int:
    held = (parse_ts(st["ts"]) or 0) - (parse_ts(rec["признак"]["с"]) or 0)
    return max(0, math.ceil(data["блокеры"][name]["держится_мин"] - held / 60))


def warden_line(st: dict, last_kick: float, now: float, data: dict, last_over: float = 0.0,
                offered: frozenset = frozenset()) -> str:
    block = st["блокеры"]
    for name in ("пауза", "лимит", "выкат", "ротация"):
        if name in block:
            return f"молчу\t{block[name]['причина']}"
    work, eff = len(st["в_работе"]), st["эффективный"]
    if eff == 0:
        return f"молчу\t{st['причина']}"
    if work > eff:
        gap = data["сторож"]["лишнее_не_чаще_мин"]
        if last_over > 0 and now - last_over < gap * 60:
            return f"молчу\tо лишнем говорили {round((now - last_over) / 60)} мин назад"
        return f"лишнее\t{work - eff}"
    if eff - work < 1:
        return "ровно"
    cfg = data["сторож"]
    if not st["к_взятию"]:
        return "молчу\tготовых задач нет"
    chosen = parse_ts(st.get("выбор_ts"))
    if last_kick > 0 and now - last_kick < cfg["пинок_не_чаще_мин"] * 60 and (chosen is None or last_kick >= chosen):
        return f"молчу\tпинок был {round((now - last_kick) / 60)} мин назад"
    raised = parse_ts(st.get("поднят_ts"))
    if raised is not None and now - raised < cfg["после_подъёма_ждать_мин"] * 60:
        return f"молчу\tмощность недавно поднята, жду {math.ceil(cfg['после_подъёма_ждать_мин'] - (now - raised) / 60)} мин"
    for name, label in (("мощность", "мощности"), ("авария", "аварии")):
        if name in st["кандидаты"]:
            rec = st["кандидаты"][name]
            return f"молчу\tкандидат {label}: {rec['причина']}, жду {wait_minutes(name, rec, data, st)} мин"
        if (block.get(name) or {}).get("признак", {}).get("есть"):
            return f"молчу\tпризнак {label} держится: {block[name]['причина']}"
    if st["добор"] < 1:
        return f"молчу\t{st['добор_почему']}"
    take = sorted(st["к_взятию"], key=lambda task: task in offered)
    return f"недобор\t{st['добор']}\t{' '.join(take[:st['добор']])}"


# ───────────────────────── лента событий ─────────────────────────

CALL_EVENTS = ("agent.launch", "agent.finish")
PART_ID = re.compile(r"Часть:\s*([^\s#]+)")


def journal_text(row: dict) -> str | None:
    data = row.get("data")
    if not isinstance(data, dict):
        data = {}
    event = row.get("event")
    if event == "mode.decision":
        return str(data.get("причина", ""))
    if event == "mode.change":
        return f"{data.get('было')}→{data.get('стало')}"
    if event == "speed.kick":
        return "пинок: " + " ".join(map(str, data.get("предложено") or []))
    if event == "task.taken":
        return f"взята {data.get('id')}"
    if event == "agent.launch":
        found = PART_ID.search(str(data.get("часть", "")))
        return f"запуск {found.group(1) if found else '?'}"
    if event == "agent.finish":
        return "конец"
    return None


def journal_rows(path: Path, only: tuple | None) -> list:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        stamp = parse_ts(row.get("ts")) if isinstance(row, dict) else None
        if stamp is not None and (only is None or row.get("event") in only):
            rows.append((stamp, row))
    return rows


def run_journal(args: list, now: float) -> int:
    window = pop_value(args, "--за")
    since = None
    if window is not None:
        found = re.fullmatch(r"(\d+(?:[.,]\d+)?)ч", window)
        if not found:
            print(f"--за: ожидаю Nч, получено «{window}»", file=sys.stderr)
            return 1
        since = now - float(found.group(1).replace(",", ".")) * 3600
    if args:
        print(f"--журнал: незнакомый ключ {args[0]}", file=sys.stderr)
        return 1
    logs = Path(events.log_dir())
    rows = journal_rows(logs / f"{JOURNAL}.jsonl", None) + journal_rows(logs / "вызовы-агентов.jsonl", CALL_EVENTS)
    lines = []
    for stamp, row in sorted(rows, key=lambda item: item[0]):
        text = journal_text(row)
        if text is not None and (since is None or since <= stamp) and stamp <= now:
            lines.append(f"{dt.datetime.fromtimestamp(stamp).strftime('%H:%M')} {text}")
    print("\n".join(lines) if lines else "событий нет")
    return 0


# ───────────────────────── вход ─────────────────────────

def pop_value(args: list, flag: str):
    if flag not in args:
        return None
    index = args.index(flag)
    value = args[index + 1] if index + 1 < len(args) else ""
    del args[index:index + 2]
    return value


def main(argv: list) -> int:
    args = list(argv)
    clock = pop_value(args, "--на")
    now = time.time() if clock is None else parse_ts(clock)
    if now is None:
        print(f"--на: не время ISO 8601: {clock}", file=sys.stderr)
        return 1
    try:
        if "--журнал" in args:
            args.remove("--журнал")
            return run_journal(args, now)
        if "--замер" in args:
            return run_pass(now)
        if "--выбран" in args:
            who = pop_value(args, "--выбран")
            if who not in SELECTORS:
                print(f"--выбран: кто — одно из {', '.join(SELECTORS)}", file=sys.stderr)
                return 1
            return run_pass(now, who)
        if "--показать" in args or "--сторожу" in args:
            data = load_data()
            st = fresh_state(now)
            if st is None:
                print(NOT_ANSWERING)
                return 2
            if "--показать" in args:
                print("\n".join(show(st, data, "--коротко" in args)))
            else:
                kick = pop_value(args, "--последний-пинок") or "0"
                try:
                    last_kick = float(kick)
                except ValueError:
                    last_kick = 0.0
                over = pop_value(args, "--последнее-лишнее") or "0"
                try:
                    last_over = float(over)
                except ValueError:
                    last_over = 0.0
                offered = frozenset((pop_value(args, "--предложено") or "").split())
                print(warden_line(st, last_kick if math.isfinite(last_kick) else 0.0, now, data,
                                  last_over if math.isfinite(last_over) else 0.0, offered))
            return 0
    except DataError as exc:
        print(f"ошибка данных: {exc}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    if "--замер" in sys.argv[1:]:
        sys.exit(events.run(SRC, lambda: main(sys.argv[1:]), journal=JOURNAL, name="speed-mode",
                            heartbeat="speed_mode"))
    sys.exit(main(sys.argv[1:]))
