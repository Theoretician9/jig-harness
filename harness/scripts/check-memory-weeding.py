#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Гейт прополки без потерь: строка приказов не исчезает молча.

Файлы «без_потерь» из memory-layers.yaml (память/ORDERS.md, дословные слова владельца)
модель дописывает и пропалывает, но каждая ушедшая строка обязана лечь в каталог
«архив_прополки». Гейт берёт эти файлы из HEAD~, сверяет с рабочим деревом и краснеет
на каждой строке, которой нет ни в прежнем файле, ни в архиве. В рабочем дереве потерю
не видно: удалённая строка выглядит как обычная правка (спека личности и политики §5.3).

Строка — это текст без хвостовых пробелов; пустые строки не считаются, порядок тоже.
Сокращённая строка — это уход прежнего текста: без архива он потерян.

    python3 scripts/check-memory-weeding.py     0 — потерь нет; 1 — потеря, строки в выводе

Двери проб: MEMORY_WEEDING_ROOT (корень репозитория), MEMORY_LAYERS (файл слоёв).
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("MEMORY_WEEDING_ROOT") or Path(__file__).resolve().parent.parent)
LAYERS = Path(os.environ.get("MEMORY_LAYERS") or ROOT / "harness" / "config" / "memory-layers.yaml")
BASE = "HEAD~"
SHOWN_LIMIT = 20


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, timeout=60)


def lines_of(text: str) -> set[str]:
    return {line.rstrip() for line in text.splitlines() if line.strip()}


def base_files(patterns: list[str]) -> list[str]:
    done = git("ls-tree", "-r", "--name-only", "-z", BASE)
    names = done.stdout.decode("utf-8").split("\0")
    return sorted(n for n in names if n and any(fnmatch.fnmatchcase(n, p) for p in patterns))


def base_text(name: str) -> str:
    return git("show", f"{BASE}:{name}").stdout.decode("utf-8")


def tree_lines(name: str) -> set[str]:
    path = ROOT / name
    return lines_of(path.read_text(encoding="utf-8")) if path.is_file() else set()


def archived_lines(archive: str) -> set[str]:
    folder = ROOT / archive
    found: set[str] = set()
    for path in sorted(folder.rglob("*")) if folder.is_dir() else []:
        if path.is_file():
            found |= lines_of(path.read_text(encoding="utf-8", errors="replace"))
    return found


def lost_lines(layers: dict) -> list[tuple[str, str]]:
    patterns = [str(p) for p in layers.get("без_потерь") or []]
    archived = archived_lines(str(layers.get("архив_прополки") or "память/archive/"))
    lost = []
    for name in base_files(patterns):
        kept = tree_lines(name) | archived
        for line in base_text(name).splitlines():
            if line.strip() and line.rstrip() not in kept and (name, line.rstrip()) not in lost:
                lost.append((name, line.rstrip()))
    return lost


def main() -> int:
    if git("rev-parse", "--verify", "-q", BASE).returncode != 0:
        print(f"[прополка] нет {BASE}: сверять не с чем")
        return 0
    try:
        layers = yaml.safe_load(LAYERS.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        print(f"[прополка] ОТКАЗ: файл слоёв {LAYERS} не прочитан: {error}")
        return 1
    lost = lost_lines(layers)
    if not lost:
        print("[прополка] потерь нет: каждая строка «без потерь» на месте или в архиве")
        return 0
    archive = layers.get("архив_прополки") or "память/archive/"
    print(f"[прополка] КРАСНЫЙ: строк «без потерь» ушло без переноса в {archive}: {len(lost)}")
    for name, line in lost[:SHOWN_LIMIT]:
        print(f"  {name}: {line}")
    print("  Перенеси ушедшие строки в архив прополки (тот же коммит) или верни их.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
