#!/usr/bin/env python3
"""Датчики ядра (core.json) меряет КОД: раз в сутки, за два соседних окна.

Раньше четыре датчика и правило «рост одного за счёт других» жили текстом, и
07.11 доля напоминаний выросла с 0.35 до 0.54 так, что никто не заметил.
Спека: .tasks/hr.self-configuration-environment/spec.md (ред. 2).

    self-sensors.py --show [--at EPOCH]   итог одним JSON, без записи
    self-sensors.py --record              раз в сутки строка sensors.daily + карточки
    self-sensors.py --selftest            проба scripts/test_self_sensors.py

Данные (окна, отставание окон, маркеры, порог, статусы «закрыта») — harness/config/self-sensors.yaml,
путь можно подменить HARNESS_SELF_SENSORS_CONFIG.
Код возврата --record: 0 — записано, уже есть или alert-task отказал (код 6 из
сессии, 4/5 отложено/потолок, таймаут): тогда строки дня нет и крон повторит через час.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import zoneinfo
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import events  # noqa: E402
import process_load  # noqa: E402
from config import конфиг  # noqa: E402
from neighbor import модуль  # noqa: E402

DAY = 86400
JOURNAL = "self-sensors"
WATCHER = "self-sensors"   # сторож в журнале тревог: свою карточку в «сломано» не считаем
ALERT_JOURNAL = "тревоги-сторожей.jsonl"
MECHANISMS = "harness/config/mechanisms.yaml"
CHANNEL = модуль("tasks-from-channel")
FROM_MESSAGE = модуль("map-from-message")

# Датчик → (поля числителя, поля знаменателя, делитель). Из одной таблицы берутся и
# доля, и показываемые счёты. Знаменатель судит минимум выборки — других правил про ноль нет.
RATIOS = {
    "attention": (("напоминания",), ("напоминания", "идеи"), 1),
    "autonomy": (("сообщения",), ("закрыто",), 1),
    "efficiency": (("токены",), ("закрыто",), 1e6),
    "creation": (("создано",), ("создано", "сломано"), 1),
}


def ratio(key: str, window: dict) -> tuple[float, float]:
    top, bottom, scale = RATIOS[key]
    return sum(window[f] for f in top) / scale, sum(window[f] for f in bottom)


def shown_fields(key: str) -> list[str]:
    top, bottom, _ = RATIOS[key]
    return list(dict.fromkeys((*top, *bottom)))


def config_path() -> Path:
    given = os.environ.get("HARNESS_SELF_SENSORS_CONFIG")
    root = HERE.parent
    candidates = [Path(given)] if given else [root / "harness" / "config" / "self-sensors.yaml",
                                              root / "config" / "self-sensors.yaml"]
    return next((p for p in candidates if p.is_file()), candidates[0])


class World:
    """Всё, что код читает снаружи: настройки, пояс владельца, пути."""

    def __init__(self, at: float) -> None:
        passport = конфиг()
        self.settings = yaml.safe_load(config_path().read_text(encoding="utf-8"))
        self.at = at
        self.log = Path(passport["LOG_DIR"])
        self.project = Path(passport.get("PROJECT_DIR") or HERE.parent)
        self.maps = [Path(os.environ.get("HARNESS_DEVMAP") or self.project / "dev-map.yaml"),
                     Path(os.environ.get("DEVMAP_ARCHIVE")
                          or self.project / "docs" / "dev-map-archive.yaml")]
        name = str(passport.get("OWNER_TZ") or "").strip()
        # Пустой пояс владельца — пояс машины: aware-время с tzinfo=None остаётся наивным.
        self.tz = zoneinfo.ZoneInfo(name) if name else None
        # Окна кончаются не в «сейчас», а на отставание раньше: закрытия задач
        # доезжают задним числом, а токены пишутся сразу, и недописанный край
        # окна давал ложное «хуже» (13.11: в 00:05 закрыто 49, к 13:11 по тому
        # же окну — 58).
        self.span = self.settings["окно_суток"] * DAY
        self.end = at - self.settings["отставание_суток"] * DAY
        self.windows = {"сейчас": (self.end - self.span, self.end),
                        "прошлое": (self.end - 2 * self.span, self.end - self.span)}

    def day(self) -> str:
        return datetime.datetime.fromtimestamp(self.at, self.tz).date().isoformat()

    def moment(self, value) -> float | None:
        """Секунда из поля карты или журнала; дата без времени — начало суток владельца."""
        try:
            parsed = datetime.datetime.fromisoformat(str(value).strip())
        except ValueError:
            return None
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=self.tz)).timestamp()

    def window_of(self, moment: float | None) -> str | None:
        return next((name for name, (lo, hi) in self.windows.items()
                     if moment is not None and lo <= moment < hi), None)


def jsonl_rows(path: Path) -> list[dict]:
    return list(process_load.jsonl_rows(process_load.read_text(str(path)).splitlines()))


# ── сообщения владельца ────────────────────────────────────────────────────

def marker_regex(words) -> re.Pattern:
    """Целое слово или фраза: «где» не срабатывает на «везде»."""
    body = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{body})(?!\w)", re.I)


def owner_words(raw: str, ours: set) -> str:
    """Слова владельца без цитат. Совпавшую с нашей длинную строку снимаем и
    поодиночке: судье карточек нужно ≥2 строк цитаты, а для счёта потеря одной
    строки владельца безвредна."""
    text = CHANNEL.слова_владельца(raw, ours)
    kept = [line for line in text.splitlines()
            if not (CHANNEL.ровно(line) in ours and len(CHANNEL.ровно(line)) >= CHANNEL.МИН_ДЛИНА_ЦИТАТЫ)]
    return "\n".join(kept).strip()


def message_counts(world: World) -> dict:
    marks = world.settings["маркеры"]
    reminder = marker_regex(marks["напоминание"])
    idea = marker_regex(list(CHANNEL.МАРКЕРЫ) + marks["идея"])
    ours = FROM_MESSAGE.наши_строки(world.log / "tg_out.jsonl", world.at, CHANNEL)
    counts = {w: {"напоминания": 0, "идеи": 0, "сообщения": 0} for w in world.windows}
    for file in sorted((world.log / "inbox" / "обработано").glob("*.txt")):
        window = world.window_of(float(file.name.split(".")[0]))
        raw = CHANNEL.текст_сообщения(file)
        if window is None or not raw:
            continue
        words = owner_words(raw, ours)
        counts[window]["сообщения"] += 1
        counts[window]["напоминания"] += bool(reminder.search(words))
        counts[window]["идеи"] += bool(not reminder.search(words) and idea.search(words))
    return counts


# ── закрытые задачи, токены, созидательность ───────────────────────────────

def field_at(task: dict, path: str):
    for part in path.split("."):
        task = task.get(part) if isinstance(task, dict) else None
    return task


def closed_counts(world: World) -> tuple[dict, int]:
    """Закрыта = агент закончил (статус из конфига); дата — первое непустое поле.
    Дата агента первой: приёмка владельцем не двигает задачу между окнами."""
    rule = world.settings["закрыта"]
    tasks = {}
    for path in world.maps:
        if not path.is_file():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for line in data.get("lines") or []:
            for task in line.get("tasks") or []:
                tasks.setdefault(task.get("id"), task)
    closed = [t for t in tasks.values() if t.get("status") in rule["статусы"]]
    counts = {w: 0 for w in world.windows}
    undated = 0
    for task in closed:
        value = next((v for v in (field_at(task, p) for p in rule["дата_из"]) if v), None)
        window = world.window_of(world.moment(value))
        counts[window] = counts.get(window, 0) + 1
        undated += value is None
    return {w: counts[w] for w in world.windows}, undated


def token_counts(world: World) -> dict:
    lo = world.windows["прошлое"][0]
    found = process_load.token_events(str(world.log), lo, world.at)
    counts = {w: 0 for w in world.windows}
    for event in found:
        window = world.window_of(event["_t"])
        # Верхнюю границу окна token_events не ставит: её ставит window_of.
        counts[window] = counts.get(window, 0) + sum(process_load.event_tokens(event))
    return {w: counts[w] for w in world.windows}


def mechanisms_at(world: World, moment: float) -> int:
    """Записей реестра механизмов в последнем коммите до момента; нет git — 0."""
    git = ["git", "-C", str(world.project)]
    rev = subprocess.run([*git, "rev-list", "-1", f"--before=@{int(moment)}", "HEAD", "--", MECHANISMS],
                         capture_output=True, text=True).stdout.strip()
    text = subprocess.run([*git, "show", f"{rev}:{MECHANISMS}"],
                          capture_output=True, text=True).stdout if rev else ""
    return len(re.findall(r"^\s*- файл:", text, re.M))


def broken_counts(world: World) -> dict:
    counts = {w: 0 for w in world.windows}
    for row in jsonl_rows(world.log / ALERT_JOURNAL):
        if row.get("событие") == "заведена" and row.get("сторож") != WATCHER:
            window = world.window_of(world.moment(row.get("ts")))
            counts[window] = counts.get(window, 0) + 1
    for row in jsonl_rows(world.log / "пропуски.jsonl"):
        window = world.window_of(world.moment(row.get("дата")))
        counts[window] = counts.get(window, 0) + 1
    return {w: counts[w] for w in world.windows}


def created_counts(world: World) -> dict:
    edges = [mechanisms_at(world, world.end - k * world.span) for k in (0, 1, 2)]
    # Зажим max(0, …): убыль реестра — не «создано минус N», а «создано 0».
    return {"сейчас": max(0, edges[0] - edges[1]), "прошлое": max(0, edges[1] - edges[2])}


# ── итог ───────────────────────────────────────────────────────────────────

def verdict(good_down: bool, now, past, threshold: float):
    """Хуже/лучше в СВОЁМ направлении больше порога от прошлого. Прошлое 0 не
    особый случай: любой рост от нуля — уже больше нуля."""
    if now is None or past is None:
        return None
    moved = (now - past) * (1 if good_down else -1)
    return "хуже" if moved > threshold * past else "лучше" if moved < -threshold * past else "в пороге"


def window_counts(world: World) -> tuple[dict, int]:
    """Сырые счёты обоих окон по всем источникам; вторым — задач закрыто без даты."""
    closed, undated = closed_counts(world)
    parts = {"закрыто": closed, "токены": token_counts(world),
             "создано": created_counts(world), "сломано": broken_counts(world)}
    messages = message_counts(world)
    return {w: {**messages[w], **{name: part[w] for name, part in parts.items()}}
            for w in world.windows}, undated


def measure(world: World) -> dict:
    raw, undated = window_counts(world)
    minimum = world.settings["минимум_выборки"]
    sensors = {}
    for name, rule in world.settings["датчики"].items():
        values = {}
        for w in world.windows:
            top, bottom = ratio(rule["ключ"], raw[w])
            values[w] = round(top / bottom, 3) if bottom >= minimum else None
        sensors[rule["ключ"]] = {
            "имя": name, "сейчас": values["сейчас"], "прошлое": values["прошлое"],
            "вердикт": verdict(rule["хорошо"] == "вниз", values["сейчас"], values["прошлое"],
                               world.settings["порог_ухудшения"]),
            "счёты": {w: {f: raw[w][f] for f in shown_fields(rule["ключ"])} for w in world.windows}}
    return {"дата": world.day(),
            "окно": {w: [lo, hi] for w, (lo, hi) in world.windows.items()},
            "без_даты": undated, "датчики": sensors}


# ── запись и карточки ──────────────────────────────────────────────────────

def alert(*args: str) -> int:
    """Код alert-task; не ответил за срок — тоже отказ (-1), а не трасса."""
    try:
        return subprocess.run([sys.executable, str(HERE / "alert-task.py"), *args],
                              capture_output=True, text=True, timeout=120).returncode
    except subprocess.TimeoutExpired:
        return -1


def counts_text(window: dict) -> str:
    return ", ".join(f"{name} {value}" for name, value in window.items())


def card_text(key: str, result: dict) -> str:
    sensor = result["датчики"][key]
    better = [s["имя"] for k, s in result["датчики"].items() if s["вердикт"] == "лучше"]
    return (f"Против прошлого окна: "
            f"сейчас: {counts_text(sensor['счёты']['сейчас'])}; "
            f"прошлое: {counts_text(sensor['счёты']['прошлое'])}. "
            f"Рост за счёт: {', '.join(better) or 'других датчиков нет'}. Пересмотреть настройку "
            f"(маркеры в harness/config/self-sensors.yaml, механизм, ядро) по данным: "
            f"python3 scripts/self-sensors.py --show.")


def raise_or_resolve(result: dict) -> bool:
    """Ушёл не туда → карточка (повтор alert-task не плодит); вернулся → --resolve.
    Истина — alert-task ответил 0 на всё: иначе карточки нет, и строку дня писать нельзя."""
    for key, sensor in result["датчики"].items():
        source = f"{WATCHER}:{key}"
        if sensor["вердикт"] == "хуже":
            title = f"Датчик {sensor['имя']} ушёл не туда: {sensor['прошлое']} → {sensor['сейчас']}"
            sensor["тревога"] = alert("--key", source, "--title", title, "--text", card_text(key, result))
        elif sensor["вердикт"] is not None:
            sensor["тревога"] = alert("--resolve", source)
    return all(s.get("тревога", 0) == 0 for s in result["датчики"].values())


def recorded_today(world: World) -> bool:
    return any(r.get("event") == "sensors.daily" and (r.get("data") or {}).get("дата") == world.day()
               for r in jsonl_rows(world.log / f"{JOURNAL}.jsonl"))


def record(world: World) -> int:
    if recorded_today(world):
        print(f"self-sensors: уже есть за {world.day()}")
        return 0
    result = measure(world)
    if not raise_or_resolve(result):
        print(f"self-sensors: alert-task отказал, строка за {world.day()} не пишется, повтор в следующий час")
        return 0
    written = events.emit("scripts/self-sensors.py", "sensors.daily", "ok", journal="self-sensors", data=result)
    print(f"self-sensors: записано за {world.day()}" if written else "self-sensors: журнал не записан")
    return 0 if written else 1


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--show", action="store_true")
    mode.add_argument("--record", action="store_true")
    mode.add_argument("--selftest", action="store_true")
    parser.add_argument("--at", type=float, default=None)
    args = parser.parse_args(argv)
    if args.selftest:
        return subprocess.run([sys.executable, str(HERE / "test_self_sensors.py")]).returncode
    world = World(args.at or time.time())
    if args.record:
        return record(world)
    print(json.dumps(measure(world), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
