#!/usr/bin/env python3
"""Сверка входящих владельца с журналом квитанций диспетчера.

Указание владельца 03.11.2026: «Надо чтобы всегда я видел это "принял"».
Диспетчер пишет в журнал доставка строку delivery.ack на каждое «принял»
(метка сообщения = имя файла во входящих, исход ok/error). Здесь файлы
inbox/ и inbox/обработано/ за последние часы сверяются с этими строками:
сообщение старше порога без удачной квитанции — нарушение.

Что НЕ судится:
  - сообщения до первой записи журнала квитанций (delivery.ack_journal_on или
    любой delivery.ack): раньше диспетчер квитанций не записывал, и «нет
    записи» там не значит «нет квитанции»;
  - команды меню: их файл диспетчер убирает сразу (ne_rabota_dlya_smeny), в
    inbox их нет — а сама квитанция на них пишется в журнал как обычная.

Прогон:
    python3 scripts/check-ack-receipts.py --log-dir <LOG_DIR> [--hours 6] [--grace 60]
Код возврата: 0 — нарушений нет, 1 — есть (по строке на сообщение).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

JOURNAL = "доставка.jsonl"
START_EVENTS = {"delivery.ack_journal_on", "delivery.ack"}


def read_journal(path: Path) -> tuple[float | None, set[str]]:
    """Начало журнала квитанций и метки с удачной квитанцией."""
    started: float | None = None
    acked: set[str] = set()
    if not path.is_file():
        return started, acked
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        event = row.get("event")
        if event not in START_EVENTS:
            continue
        data = row.get("data") or {}
        when = data.get("sent_at") or data.get("started_at") or data.get("ts")
        try:
            when = float(when)
        except (TypeError, ValueError):
            continue
        started = when if started is None else min(started, when)
        if event == "delivery.ack" and row.get("outcome") == "ok":
            acked.add(str(data.get("ts")))
    return started, acked


def inbox_stamps(log_dir: Path) -> list[tuple[str, float]]:
    """Метки сообщений: имя файла — время приёма (date +%s.%N)."""
    stamps = []
    for folder in (log_dir / "inbox", log_dir / "inbox" / "обработано"):
        for item in folder.glob("*.txt"):
            try:
                stamps.append((item.stem, float(item.stem)))
            except ValueError:
                continue
    return stamps


def violations(log_dir: Path, hours: float, grace: int, now: float) -> list[str]:
    started, acked = read_journal(log_dir / JOURNAL)
    if started is None:
        return []
    found = []
    for stem, received in sorted(inbox_stamps(log_dir), key=lambda s: s[1]):
        if received < started or received < now - hours * 3600:
            continue
        if now - received > grace and stem not in acked:
            found.append(f"{stem}: принято {int(now - received)} с назад, квитанции «принял» нет")
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", required=True)
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--grace", type=int, default=60)
    args = ap.parse_args()
    found = violations(Path(args.log_dir), args.hours, args.grace, time.time())
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
