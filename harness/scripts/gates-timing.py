#!/usr/bin/env python3
"""Топ медленных проверок ворот по журналу секунд $LOG_DIR/gates-timing.jsonl.

    python3 scripts/gates-timing.py                 топ-10 последнего прогона
    python3 scripts/gates-timing.py --top 20        топ-20
    python3 scripts/gates-timing.py --runs 5        среднее по пяти последним прогонам
    python3 scripts/gates-timing.py --json          один объект: verdict, run, runs, checks, total_secs, items

Журнал пишет обёртка проверки ворот (scripts/lib/gates_check.sh), строка на
проверку; прогон отделяется меткой data.run. Код возврата: 0 — ответ дан (в том
числе «нет данных»), 2 — неверный вызов.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent / "lib"
sys.path.insert(0, str(LIB_DIR))
# Своим именем, как в scripts/events.py: `import events` из каталога scripts
# взял бы сам scripts/events.py, а не библиотеку.
_spec = importlib.util.spec_from_file_location("events_lib", LIB_DIR / "events.py")
lib = importlib.util.module_from_spec(_spec)
sys.modules["events_lib"] = lib
_spec.loader.exec_module(lib)

JOURNAL = "gates-timing.jsonl"


def read_runs(path: Path) -> dict[str, list[dict]]:
    """Прогоны в порядке появления в журнале: метка → данные строк gate.check."""
    runs: dict[str, list[dict]] = {}
    if not path.is_file():
        return runs
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        data = row.get("data") if isinstance(row, dict) else None
        if row.get("event") != "gate.check" or not isinstance(data, dict):
            continue
        if isinstance(data.get("secs"), (int, float)) and data.get("name"):
            runs.setdefault(str(data.get("run") or ""), []).append(data)
    return runs


def slowest(runs: dict[str, list[dict]], last: int, top: int) -> dict:
    picked = list(runs)[-last:]
    per_check: dict[str, list[dict]] = {}
    for run in picked:
        for data in runs[run]:
            per_check.setdefault(data["name"], []).append(data)
    items = [{"name": name,
              "secs": round(sum(d["secs"] for d in seen) / len(seen), 3),
              "max": max(d["secs"] for d in seen),
              "rc": seen[-1].get("rc"),
              "seen": len(seen)}
             for name, seen in per_check.items()]
    items.sort(key=lambda i: i["secs"], reverse=True)
    return {"verdict": "ok" if items else "нет данных",
            "run": picked[-1] if picked else None,
            "runs": len(picked),
            "checks": len(per_check),
            "total_secs": round(sum(i["secs"] for i in items), 1),
            "items": items[:top]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Топ медленных проверок ворот")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--runs", type=int, default=1, help="сколько последних прогонов усреднить")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.top < 1 or args.runs < 1:
        parser.error("--top и --runs — целые от 1")

    path = Path(lib.log_dir()) / JOURNAL
    answer = slowest(read_runs(path), args.runs, args.top)
    if args.json:
        print(json.dumps(answer, ensure_ascii=False))
        return 0
    if not answer["items"]:
        print(f"нет данных: в {path} нет строк gate.check")
        return 0
    print(f"прогонов {answer['runs']} (последний {answer['run']}), проверок {answer['checks']}, "
          f"сумма {answer['total_secs']} с")
    for n, item in enumerate(answer["items"], 1):
        tail = f", макс {item['max']}" if item["seen"] > 1 else ""
        print(f"{n:>3}. {item['secs']:>8.1f} с  {item['name']} (код {item['rc']}{tail})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
