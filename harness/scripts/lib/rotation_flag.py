#!/usr/bin/env python3
"""Флаг «ротация ждёт идущих помощников» — ЧТЕНИЕ (спека ротация-ждёт-помощников, раздел 4).

Файл `$PANEL_STATE_DIR/rotation-waits-helpers.json` ставит сторож сессий. Хук
очереди и сторож читают его только здесь: два судьи одного
флага разошлись бы в том, что считать протухшим или битым.

Три ответа, а не два: «нет» | «живой» | «протух». Живость — mtime файла, а не
поле внутри: так она есть и у битого флага, который сторож всё равно освежает.
Битый, но живой флаг остаётся флагом (data пуст) — молчаливая смена не должна
получить зелёный свет из-за оборванной записи.

Запись и снятие — ТОЖЕ здесь (`--set`, `--clear`): сторож зовёт этот CLI, а не
собирает JSON сам; две точки записи разошлись бы в полях и правилах переноса.

`--set --stage S --helpers N --waited SEC [--forced 0|1] [--told EPOCH]
[--session ID] [--reason TEXT]` пишет флаг; «с», «причина», «принуждена» и
«сказано» переходят из прежнего флага: «с» нужен для отсчёта, «сказано» — чтобы
письмо владельцу не повторялось. `--clear` снимает флаг и метку «принято на
ожидании» (её ставит диспетчер канала).

`--state` печатает содержимое ЖИВОГО флага одной строкой JSON (последний взгляд
перед перезагрузкой в окне ротации). Нет флага или протух — пусто и код 1: хук
протухший флаг не исполняет, значит новые запуски помощников уже не держатся.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as конф                                     # noqa: E402

FLAG_NAME = "rotation-waits-helpers.json"
STALE_DEFAULT_MIN = 30


def folder() -> Path:
    """Каталог состояния панели; дверь стенда — HARNESS_PANEL_STATE, как у pause.py."""
    return Path(os.environ.get("HARNESS_PANEL_STATE") or конф.panel_state_dir())


def path() -> Path:
    return folder() / FLAG_NAME


def stale_min() -> int:
    value = (os.environ.get("ROTATE_FLAG_STALE_MIN")
             or конф.из_файла(конф.путь_harness_conf()).get("ROTATE_FLAG_STALE_MIN") or "")
    return int(value) if value.strip().isdigit() else STALE_DEFAULT_MIN


def state(now: float | None = None) -> tuple[str, dict]:
    """(«нет» | «живой» | «протух», содержимое; пусто, если флаг не разобран)."""
    try:
        age_sec = (now if now is not None else time.time()) - path().stat().st_mtime
    except OSError:
        return "нет", {}
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return ("протух" if age_sec > stale_min() * 60 else "живой"), data


def put(stage: str, helpers: str, waited: int, forced: int | None,
        told: int | None, session: str, reason: str) -> None:
    """Запись через временный файл и rename: хук читает флаг в любой миг и не должен увидеть половину."""
    now = int(time.time())
    try:
        old = json.loads(path().read_text(encoding="utf-8")) if path().stat().st_size else {}
    except (OSError, ValueError):
        old = {}
    old = old if isinstance(old, dict) else {}

    def kept(key, default):
        return default if old.get(key) is None else old[key]

    data = {"сессия": session, "этап": stage, "с": kept("с", now), "проверено": now,
            "ждали_сек": waited, "причина": kept("причина", reason),
            "помощников": int(helpers) if helpers != "?" else helpers,
            "принуждена": forced if forced is not None else kept("принуждена", 0),
            "сказано": told if told is not None else old.get("сказано")}
    folder().mkdir(parents=True, exist_ok=True)
    tmp = path().with_name(f"{FLAG_NAME}.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path())
    finally:
        tmp.unlink(missing_ok=True)


def clear() -> None:
    path().unlink(missing_ok=True)
    (folder() / "пауза-сказано-приём").unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rotation_flag.py")
    act = parser.add_mutually_exclusive_group(required=True)
    act.add_argument("--state", action="store_true")
    act.add_argument("--set", action="store_true")
    act.add_argument("--clear", action="store_true")
    parser.add_argument("--stage")
    parser.add_argument("--helpers")
    parser.add_argument("--waited", type=int)
    parser.add_argument("--forced", type=int, choices=(0, 1))
    parser.add_argument("--told", type=int)
    parser.add_argument("--session", default="")
    parser.add_argument("--reason", default="")
    args = parser.parse_args(argv)
    if args.set:
        if args.stage is None or args.helpers is None or args.waited is None:
            parser.error("--set требует --stage, --helpers, --waited")
        # «?» — сторож не смог сосчитать помощников; всё прочее, кроме ASCII-цифр
        # («-1», «1.5», арабо-индийские, «²»), отвергается до записи флага.
        if args.helpers != "?" and not re.fullmatch(r"[0-9]+", args.helpers):
            parser.error("--helpers: ожидаются цифры 0-9 или «?»")
        put(args.stage, args.helpers, args.waited, args.forced, args.told, args.session, args.reason)
        return 0
    if args.clear:
        clear()
        return 0
    kind, data = state()
    if kind != "живой":
        return 1
    print(json.dumps(data, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
