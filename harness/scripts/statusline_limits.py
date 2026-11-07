#!/usr/bin/env python3
"""Строка состояния Claude Code: снимает лимиты подписки и печатает короткую строку.

Откуда: судье режима скорости (harness/demons/speed_mode.py, адаптер `claude`) нужны
проценты и сроки сброса лимита подписки, а Claude Code отдаёт их только скрипту statusLine
в stdin (`rate_limits.five_hour` и `seven_day`: `used_percentage` 0-100 и `resets_at` в
unix-секундах). Скрипт кладёт их в файл, который читает адаптер.

Формат файла (ключ лимит_подписки.файл_claude, лимиты-подписки.json в каталоге панели):
    {"ts": <секунды epoch, часы машины>,
     "rate_limits": {"five_hour": {"used_percentage": 42.0, "resets_at": 1767600000},
                     "seven_day": {...}}}
Окна, которого нет в stdin или чей срок сброса прошёл, в файле нет: старый процент не держим,
адаптер читает это как «нет данных по окну». Файл пишется только когда в stdin есть
`rate_limits`: без него (нет подписки, ещё не было ответа API) прежний снимок остаётся и
стареет сам.

Код выхода всегда 0: упавшая строка состояния портит интерфейс сессии.
"""
import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

FILE_NAME = "лимиты-подписки.json"
WINDOWS = (("five_hour", "5ч"), ("seven_day", "7д"))
MODE = 0o640


def number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def live_windows(rates: dict, now: float) -> dict:
    found = {}
    for key, _ in WINDOWS:
        cell = rates.get(key)
        if not isinstance(cell, dict) or not number(cell.get("used_percentage")):
            continue
        reset = cell.get("resets_at")
        if number(reset) and reset <= now:
            continue
        found[key] = {"used_percentage": cell["used_percentage"]}
        if number(reset):
            found[key]["resets_at"] = reset
    return found


def status_line(windows: dict) -> str:
    parts = [f"{label} {round(windows[key]['used_percentage'])}%"
             for key, label in WINDOWS if key in windows]
    return "лимит " + " · ".join(parts) if parts else "лимит: нет данных"


def panel_dir() -> str:
    # Стенд подменяет каталог переменной, как остальные писатели panel-state.
    if os.environ.get("HARNESS_PANEL_STATE"):
        return os.environ["HARNESS_PANEL_STATE"]
    import config
    return config.panel_state_dir()


def write_snapshot(windows: dict, now: float) -> None:
    import events
    path = os.path.join(panel_dir(), FILE_NAME)
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, MODE)
    try:
        try:
            os.fchmod(fd, MODE)
            os.fchown(fd, -1, events.events_gid())
        except OSError:
            pass  # агент не в группе панели: остаются права по режиму
        os.write(fd, json.dumps({"ts": now, "rate_limits": windows}).encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError:
        os.unlink(tmp)
        raise


def main() -> None:
    try:
        raw = json.load(sys.stdin)
    except ValueError:
        raw = None
    rates = raw.get("rate_limits") if isinstance(raw, dict) else None
    if not isinstance(rates, dict):
        print("лимит: нет данных")
        return
    now = time.time()
    windows = live_windows(rates, now)
    try:
        write_snapshot(windows, now)
    except Exception:
        pass  # строка важнее снимка: недоступный каталог не должен ломать сессию
    print(status_line(windows))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
