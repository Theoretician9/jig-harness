#!/usr/bin/env python3
"""Ведение карты задач: умолчания по схеме и факт `when` из истории статусов в git.

Слово владельца 07.11: метаданные карточки — по умолчанию и обновляются с минимальной
задержкой, отдельным процессом. Контракт — docs/specs/2026-11-20-devmap-keeper.md.

    python3 scripts/devmap-keeper.py              # сухой прогон: печать, ничего не пишет
    python3 scripts/devmap-keeper.py --применить  # запись под локом карты и коммит

Пишет только в основном дереве: ветка помощника карту фактами не правит, факт придёт
после слияния (коммиты веток видны в `git log --no-merges`). Коды: 0 — проход состоялся
(в том числе «нечего» и «лок занят»), 3 — не основное дерево, 1 — сбой.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, os.environ.get("HARNESS_CARDS_LIB") or str(HERE / "lib"))
import cards  # noqa: E402
import events  # noqa: E402

SRC = "scripts/devmap-keeper.py"
NOT_MAIN_TREE = 3


def git(folder: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def is_linked_worktree(folder: Path) -> bool:
    return git(folder, "rev-parse", "--absolute-git-dir") != \
        git(folder, "rev-parse", "--path-format=absolute", "--git-common-dir")


def locate(map_path: Path) -> tuple[Path, str]:
    """(корень репозитория, путь карты от корня)."""
    folder = map_path.resolve().parent
    repo = Path(git(folder, "rev-parse", "--show-toplevel"))
    return repo, map_path.resolve().relative_to(repo).as_posix()


def parse(argv: list) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--map", type=Path, default=ROOT / "dev-map.yaml")
    ap.add_argument("--schema", type=Path, default=ROOT / "harness" / "config" / "devmap-schema.yaml")
    ap.add_argument("--lock", type=Path, help="дверь проб; по умолчанию лок карты из конфига")
    ap.add_argument("--lock-wait", type=float, default=30)
    ap.add_argument("--применить", dest="apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    return ap.parse_args(argv)


def report(args: argparse.Namespace, changed: list, written: bool, commit: str | None,
           reason: str | None) -> None:
    if args.json:
        print(json.dumps({"изменено": changed, "записано": written, "коммит": commit,
                          "причина": reason}, ensure_ascii=False))
    else:
        print(f"изменено карточек: {len(changed)}; записано: {'да' if written else 'нет'}"
              + (f"; {reason}" if reason else ""))


def keep(args: argparse.Namespace, defaults: dict) -> tuple[list, str | None, str | None]:
    """Проход под локом: (изменённые id, коммит, причина). Пишет файл и коммитит из HEAD."""
    repo, rel = locate(args.map)
    transitions = cards.status_transitions(repo, rel)
    new_text, changed = cards.keep_map(args.map.read_text(encoding="utf-8"), transitions, defaults)
    if not changed:
        return [], None, None
    # Вызван под cards.map_lock — это общий dev-map.lock, который берут task-closer и devmap-selfheal.
    args.map.write_text(new_text, encoding="utf-8")
    before = git(repo, "rev-parse", "HEAD")
    # Коммит строится из HEAD, а не с диска: чужая незакоммиченная правка карты остаётся у автора.
    cards.commit_from_head(args.map, lambda head, name: {name: cards.keep_map(head(name), transitions, defaults)[0]},
                           changed, "факт when и умолчания")
    after = git(repo, "rev-parse", "HEAD")
    return changed, (after if after != before else None), None


def run(args: argparse.Namespace) -> int:
    defaults = cards.schema_defaults(yaml.safe_load(args.schema.read_text(encoding="utf-8")) or {})
    if not args.apply:
        _, changed = cards.keep_map(args.map.read_text(encoding="utf-8"),
                                    cards.status_transitions(*locate(args.map)), defaults)
        report(args, changed, False, None, "сухой прогон")
        return 0
    if is_linked_worktree(args.map.resolve().parent):
        print("ОТКАЗ: ведение карты — только в основном дереве", file=sys.stderr)
        return NOT_MAIN_TREE
    try:
        with cards.map_lock(args.lock or cards.lock_path(), args.lock_wait):
            changed, commit, reason = keep(args, defaults)
    except cards.LockBusy:
        report(args, [], False, None, "лок карты занят — следующий проход")
        return 0
    except cards.MapRefused as refused:
        print(refused, file=sys.stderr)
        return 1
    report(args, changed, bool(changed), commit, reason)
    return 0


def main(argv: list) -> int:
    args = parse(argv)
    return events.run(SRC, lambda: run(args), journal="devmap-keeper", name="devmap-keeper",
                      heartbeat="devmap-keeper")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
