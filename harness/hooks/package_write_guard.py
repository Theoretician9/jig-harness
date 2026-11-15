#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сторож записи в общий пакет из дерева помощника: файл харнеса в пакет несёт очередь.

PreToolUse на Write|Edit|NotebookEdit|Bash. При HELPER_WORKTREE=1 у каждого помощника своё
дерево, а пакет — один на всех. 15.11.2026 помощники писали файлы харнеса прямо в пакет
до слияния своей ветки, и merge-queue.py отказывал кодом 7 («пакет разошёлся») всем
остальным веткам, пока чужая правка лежала в пакете, — ветки стояли часами. Файл,
у которого есть пара в проекте, очередь сама переносит в пакет из диффа ветки и
коммитит; помощнику его в пакет писать незачем.

Файлы пакета без пары в проекте (раскладчик, слой 1 harness/self/, CLAUDE.md пакета)
очередь не переносит и кодом 7 не судит — их хук пропускает: отказ оставил бы им
ноль дверей. Основная сессия (оркестратор, управляющий) пишет пакет как раньше.

Сессия помощника узнаётся по полю cwd входа: оно лежит под <проект>/.claude/worktrees/.
Отказ — код 2, причина в stderr. Сбой самого хука — пропуск со строкой в stderr,
как у соседей: сломанный сторож не должен останавливать работу.
Двери проб: HARNESS_CONF (ключ), HARNESS_STARTER (пакет).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lib"))
import config  # noqa: E402
from orders_write_guard import write_targets  # noqa: E402

KEY = "HELPER_WORKTREE"
DENY = ("{rel} в пакете — файл харнеса с парой в проекте ({pair}). Из дерева помощника пакет "
        "не пишут: правку делай в своём дереве ({pair}) и коммить в ветку — "
        "очередь слияния (python3 scripts/merge-queue.py <ветка>) сама перенесёт его в пакет "
        "и закоммитит. Чужая правка в общем пакете стопорит очередь кодом 7 для всех веток.")


def worktree_mode() -> bool:
    """Как у launch_plan_guard: установленный harness.conf, затем конфиг дерева; не «1» = 0."""
    tree_conf = ROOT / "harness" / "config" / "harness.conf"
    value = (config.из_файла(config.путь_harness_conf()).get(KEY)
             or config.из_файла(str(tree_conf)).get(KEY) or "")
    return value == "1"


def in_helper_tree(cwd: str) -> bool:
    return any(p.name == "worktrees" and p.parent.name == ".claude"
               for p in Path(os.path.realpath(cwd)).parents)


def load_compare():
    spec = importlib.util.spec_from_file_location("compare_with_package",
                                                  HERE.parent / "compare-with-package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def carried(targets: list) -> tuple[str, str] | None:
    """(путь в пакете, пара в проекте) первой цели, которую несёт очередь; None — таких нет."""
    package = os.path.realpath(config.katalog_paketa())
    compare = load_compare()
    for target in targets:
        real = os.path.realpath(target)
        if not real.startswith(package + os.sep):
            continue
        rel = os.path.relpath(real, package)
        pair = compare.место_в_проекте(rel)
        if pair and not compare.живой(pair):
            return rel, pair
    return None


def main() -> int:
    try:
        event = json.loads(sys.stdin.read())
        hit = None
        if in_helper_tree(str(event.get("cwd") or os.getcwd())) and worktree_mode():
            hit = carried(write_targets(event))
    except Exception as error:  # noqa: BLE001 — сбой хука не запрещает работу
        print(f"package_write_guard: пропуск, хук не разобрал вход: {type(error).__name__}: {error}",
              file=sys.stderr)
        return 0
    if hit:
        print(DENY.format(rel=hit[0], pair=hit[1]), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
