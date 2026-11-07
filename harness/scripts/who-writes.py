#!/usr/bin/env python3
"""Кто какой файл занял сейчас: живые заявки помощников за окно FILE_CLAIM_MIN.

Заявки пишет хук scripts/hooks/file_claim_guard.py в $LOG_DIR/заявки-на-файлы.jsonl;
окно и разбор журнала — его же, чтобы список и предупреждение судили одинаково.
Исключительный путь (FILE_CLAIM_EXCLUSIVE) держится до коммита, а не до конца окна:
пока файл грязный, заявки после последнего коммита видны всегда — «держит до
коммита», старше окна — «брошено»; грязный путь без таких заявок назван «ханки без
заявки»; совпавший с HEAD путь не держит никто.
    python3 scripts/who-writes.py           путь и кто его пишет
    python3 scripts/who-writes.py --count   число занятых путей (строка session_state.sh)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import file_claim_guard as guard                         # noqa: E402


def state_of(rel: str, states: dict):
    if rel not in states:
        states[rel] = guard.lock_state(rel)
    return states[rel]


def visible(rows: list, window_s: float, states: dict) -> list:
    """Заявки, которые что-то держат: обычные — за окно, исключительные — после
    коммита, пока файл грязный."""
    kept = []
    for row in rows:
        path = (row.get("data") or {}).get("путь") or ""
        rel = guard.exclusive_rel(path)
        if rel is None:
            if row["_age"] <= window_s:
                kept.append(row)
            continue
        state = state_of(rel, states)
        if state and state[0] and row["_at"] > state[1]:
            kept.append(row)
    return kept


def main() -> int:
    window_s = guard.window_seconds()
    rows = [r for r in guard.recent_rows(time.time(), None)
            if r.get("event") == guard.CLAIM_EVENT and r.get("outcome") == "ok"
            and not (r.get("data") or {}).get("дверь")]
    states: dict = {}
    claims = guard.holders(visible(rows, window_s, states))
    held = {guard.exclusive_rel(path) for path in claims}
    orphans = [rel for rel in guard.setting(guard.EXCLUSIVE_KEY).split() if rel not in held
               and (state := state_of(rel, states)) and state[0]]
    if "--count" in sys.argv:
        print(len(claims) + len(orphans))
        return 0
    if not claims and not orphans:
        print(f"заявок на файлы за {window_s / 60:g} мин нет")
        return 0
    for path in sorted(claims):
        holders = claims[path]
        mark = "  ← занят несколькими" if len(holders) > 1 else ""
        print(f"{path}{mark}")
        for agent, row in holders.items():
            note = ""
            if guard.exclusive_rel(path):
                note = "  — брошено" if row["_age"] > window_s else "  — держит до коммита"
            print(f"    {guard.describe(agent, row)}{note}")
    for rel in orphans:
        print(f"{rel}  — ханки без заявки (держатель неизвестен)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
