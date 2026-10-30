#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Печать файла помощника: PostToolUse на Write|Edit.

Файл, чья маска в model-artifacts.yaml просит `печать: true`, получает машинную
запись в $LOG_DIR/artifact-seals.jsonl: кто писал (agent_id), какой моделью и
какой хеш у файла ПОСЛЕ записи. Гейт закрытия сверяет её с файлом: подделать
бумагу шага «безопасность» рукой смены значит оставить файл без печати или с
чужим хешем.

Хук не вправе мешать работе: любая своя беда — строка в hook_failures.log и
выход нулём, как у skill_log.sh.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_guard as guard                                  # noqa: E402

WATCHED_TOOLS = ("Write", "Edit")


def log_dir() -> Path:
    return Path(os.environ.get("LOG_DIR") or guard.конф.log_dir())


def relative_path(payload: dict) -> str:
    """Путь файла относительно корня проекта (cwd хука, иначе корень харнеса)."""
    path = str((payload.get("tool_input") or {}).get("file_path") or "")
    for base in (str(payload.get("cwd") or ""), str(guard.КОРЕНЬ)):
        if base and path.startswith(base.rstrip("/") + "/"):
            return path[len(base.rstrip("/")) + 1:]
    return path


def seal_row(payload: dict, relative: str) -> dict:
    file_path = str(payload["tool_input"]["file_path"])
    digest = hashlib.sha256(Path(file_path).read_bytes()).hexdigest()
    model = guard.модель_сессии(guard.транскрипт_пишущего(payload))
    return {
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "file": relative, "sha256": digest,
        "agent": str(payload.get("agent_id") or ""), "model": model,
        "источник": "хук",
    }


def record_failure(why: str) -> None:
    try:
        folder = log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        row = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
               "hook": "artifact_seal", "why": why}
        with open(folder / "hook_failures.log", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if payload.get("tool_name") not in WATCHED_TOOLS:
            return 0
        relative = relative_path(payload)
        if not guard.печать_по_пути(relative, guard.маски_артефактов()):
            return 0
        row = seal_row(payload, relative)
        folder = log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "artifact-seals.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as err:        # noqa: BLE001 — хук не роняет ход инструмента
        record_failure(f"{err.__class__.__name__}: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
