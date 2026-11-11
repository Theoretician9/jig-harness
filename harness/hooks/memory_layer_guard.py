#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Хук записи слоёв памяти: ядро и слой человека не правятся мимо двери.

PreToolUse на Write|Edit|NotebookEdit (спека hr.standards-and-policy §5.3). Слой 1
(личность, стандарты, политика запуска) меняется только выпуском харнеса — отказ
с подсказкой «правка ядра идёт в пакет». Слой 2 (instance.yaml) пишет только
scripts/self-set.py по кнопке «Да» — отказ с этой подсказкой. Остальное, включая
память и STATE.md, пропускается: хук, режущий заметки смены, отнимает у неё память.

Это подсказка от случайной правки, а не замок: Bash хук не разбирает. Замок слоя 1 —
эталон, слоя 2 — сверка sha256 при старте смены. Поэтому любая беда самого хука
(битый вход, нет файла слоёв) — пропуск со строкой в stderr: сломанный хук, который
запрещает всё, останавливает смену на ровном месте.

Отказ — код 2, причина в stderr (как у соседних хуков). Пропуск — код 0.
Двери проб: MEMORY_LAYERS (файл слоёв), MEMORY_GUARD_ROOT (корень проекта).
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("MEMORY_GUARD_ROOT") or os.environ.get("CLAUDE_PROJECT_DIR")
            or Path(__file__).resolve().parent.parent.parent)
LAYERS = Path(os.environ.get("MEMORY_LAYERS") or ROOT / "harness" / "config" / "memory-layers.yaml")
PATH_KEYS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
             "NotebookEdit": "notebook_path"}
CORE_HINT = ("{rel} — слой 1 памяти (личность, стандарты, политика запуска): правка ядра идёт в пакет "
             "харнеса и доезжает выпуском; в проекте этот файл не правят.")
HUMAN_HINT = ("{rel} — слой 2 памяти (личность этой копии): пишется только дверью "
              "python3 scripts/self-set.py <поле> <значение>, по кнопке «Да» владельца.")


def target_path(event: dict) -> str | None:
    key = PATH_KEYS.get(event.get("tool_name"))
    tool_input = event.get("tool_input")
    if key is None or not isinstance(tool_input, dict):
        return None
    return tool_input.get(key) or None


def relative_to_root(path: str, cwd: str) -> str | None:
    """Путь от корня проекта по настоящему файлу: «..» и «./» не меняют решения.
    Вне проекта — None: то же имя в пакете не слой проекта."""
    absolute = path if os.path.isabs(path) else os.path.join(cwd, path)
    real = os.path.realpath(absolute)
    try:
        return Path(real).relative_to(os.path.realpath(ROOT)).as_posix()
    except ValueError:
        return None


def matches(rel: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(rel, str(p)) for p in patterns or [])


def verdict(rel: str, layers: dict) -> str | None:
    if matches(rel, layers.get("неизменяемое")):
        return CORE_HINT.format(rel=rel)
    if matches(rel, layers.get("человека")):
        return HUMAN_HINT.format(rel=rel)
    return None


def main() -> int:
    try:
        event = json.loads(sys.stdin.read())
        path = target_path(event)
        if path is None:
            return 0
        rel = relative_to_root(path, event.get("cwd") or os.getcwd())
        if rel is None:
            return 0
        layers = yaml.safe_load(LAYERS.read_text(encoding="utf-8")) or {}
        reason = verdict(rel, layers)
    except Exception as error:  # noqa: BLE001 — сбой хука не запрещает запись
        print(f"memory_layer_guard: пропуск, хук не разобрал вход: {type(error).__name__}: {error}",
              file=sys.stderr)
        return 0
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
