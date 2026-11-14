#!/usr/bin/env python3
"""Судья приоритета релиза: пока в карте есть незакрытая карточка с полем
«релиз: {версия, до}», прогон, чья задача не релизная, уступает срез.

    python3 -I scripts/lib/release_priority.py --задачи <id1,id2|пусто>

Код 0 — идти; код 75 — уступить (причина и законный путь в stderr).
Карта не читается или у пометки нет читаемого «до» — код 0 и «ВНИМАНИЕ»:
отказать всем прогонам из-за сбоя чтения значило бы остановить помощников.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import events  # noqa: E402
import yaml  # noqa: E402

BUSY_RC = 75
CLOSED = ("test", "done")


def warn(text: str) -> None:
    print(f"приоритет релиза: ВНИМАНИЕ {text}", file=sys.stderr)


def cards(node):
    """Все словари с ключом id во вложенной карте (проекты → задачи)."""
    if isinstance(node, dict):
        if "id" in node:
            yield node
        node = list(node.values())
    if isinstance(node, list):
        for item in node:
            yield from cards(item)


def parse_until(value) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        moment = value
    else:
        try:
            moment = dt.datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.astimezone()


def live_releases(tree, now: dt.datetime) -> list[tuple[str, str, str]]:
    """(id, версия, до) каждой незакрытой карточки с действующей пометкой."""
    found = []
    for card in cards(tree):
        mark = card.get("релиз")
        if not mark or card.get("status") in CLOSED:
            continue
        until = parse_until(mark.get("до")) if isinstance(mark, dict) else None
        if until is None:
            warn(f"у пометки «релиз» карточки {card['id']} нет читаемого срока «до» — не действует")
        elif now < until:
            found.append((str(card["id"]), str(mark.get("версия", "?")),
                          mark["до"].isoformat() if isinstance(mark["до"], dt.datetime) else str(mark["до"])))
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--задачи", default="")
    ids = {t.strip() for t in parser.parse_args().задачи.split(",") if t.strip()}
    path = events.devmap_path()
    try:
        tree = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    except (OSError, ValueError, yaml.YAMLError) as error:
        warn(f"карта {path} не читается ({type(error).__name__}) — релиз не учтён, прогон идёт")
        return 0
    releases = live_releases(tree, dt.datetime.now().astimezone())
    if not releases or (ids and ids <= {r[0] for r in releases}):
        return 0
    first = releases[0]
    release_ids = ",".join(r[0] for r in releases)
    print(f"идёт релиз {first[1]} до {first[2]} (задачи релиза: {release_ids}); "
          f"прогон задач «{','.join(sorted(ids)) or 'неизвестна'}» уступает срез.\n"
          f"  Законный путь: HARNESS_TASK={first[0]} (прогон для релиза) либо дождаться "
          f"конца релиза: карточка в test/done или срок «до».", file=sys.stderr)
    return BUSY_RC


if __name__ == "__main__":
    sys.exit(main())
