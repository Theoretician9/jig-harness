#!/usr/bin/env python3
"""Кто какой файл занял сейчас: живые заявки помощников за окно FILE_CLAIM_MIN.

Заявки пишет хук scripts/hooks/file_claim_guard.py в $LOG_DIR/заявки-на-файлы.jsonl;
окно и разбор журнала — его же, чтобы список и предупреждение судили одинаково.
    python3 scripts/who-writes.py           путь и кто его пишет
    python3 scripts/who-writes.py --count   число занятых путей (строка session_state.sh)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import file_claim_guard as guard                         # noqa: E402


def main() -> int:
    window_s = guard.window_seconds()
    claims = guard.holders(guard.recent_rows(time.time(), window_s))
    if "--count" in sys.argv:
        print(len(claims))
        return 0
    if not claims:
        print(f"заявок на файлы за {window_s / 60:g} мин нет")
        return 0
    for path in sorted(claims):
        holders = claims[path]
        mark = "  ← занят несколькими" if len(holders) > 1 else ""
        print(f"{path}{mark}")
        for agent, row in holders.items():
            print(f"    {guard.describe(agent, row)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
