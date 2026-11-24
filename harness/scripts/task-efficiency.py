#!/usr/bin/env python3
"""Какие задачи шли неэффективно: отчёт по карте задач и истории коммитов.

Владелец 07.11: механизм, который по карте видит неэффективные задачи
(вариант 1 разбора замысла). Фактов расхода в карте
почти нет (volume и when — оценки), поэтому факт берётся из git:

* коммиты с трейлером `Dev-Map: <id>` (один `git log --numstat`) — число
  коммитов, первый коммит и строки факта (добавленные, без карты и её архива);
* карточки — из карты и архива `docs/dev-map-archive.yaml` (закрытые уезжают туда);
* история карты (`git log -p -U3 -- <карта>`, читается потоком: 16 МБ) —
  возвраты статуса test/принято/done → wip/plan и момент перехода в закрытый.

Срок — в часах: большинство задач закрывается за часы, и дни с десятой
округляли их в 0.0. Срок без второй точки во времени (один коммит, он же
закрыл задачу) не замерен — null, не ноль. Оценку volume.loc судим только
с estimated: false: при estimated: true там константа автомата (loc: 100).

Токенов на задачу здесь нет — это вариант 2.

Прогон:
    python3 scripts/task-efficiency.py [--top N] [--json]
    python3 scripts/task-efficiency.py --repo <git> --map <карта> --now <epoch>
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ.get("HARNESS_CARDS_LIB") or str(Path(__file__).resolve().parent / "lib"))
import cards  # noqa: E402

HOUR = 3600
WEEK_HOURS = 168
CLOSED = ("test", "принято", "done")
REWORK = ("wip", "plan")


def map_tasks(map_path: Path, archive_path: Path) -> dict:
    """Карточки карты и её архива; у архивной — пометка archived.

    Закрытые владельцем задачи уезжают в архив, и без него отчёт видел бы
    только недоделанное."""
    import yaml
    tasks = {}
    for path, archived in ((archive_path, True), (map_path, False)):
        if archived and not path.exists():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for line in data.get("lines") or []:
            for task in line.get("tasks") or []:
                tasks[task["id"]] = dict(task, archived=archived)
    return tasks


def trailer_commits(repo: Path, records: set) -> list[dict]:
    """Коммиты с трейлером Dev-Map: время, id задач и добавленные строки."""
    out = subprocess.run(
        ["git", "-C", str(repo), "log", "--no-merges", "--grep=Dev-Map:", "--numstat",
         "--format=%x1e%at%x1f%(trailers:key=Dev-Map,valueonly,separator=%x2C)"],
        capture_output=True, text=True, check=True).stdout
    commits = []
    for block in out.split("\x1e")[1:]:
        head, _, stat = block.partition("\n")
        when, _, trailer = head.partition("\x1f")
        ids = {i.strip() for i in trailer.split(",") if i.strip()}
        if not ids:
            continue
        added = 0
        for row in stat.splitlines():
            parts = row.split("\t")
            # Двоичный файл даёт «-»; правка карты и архива — запись о задаче, не работа.
            if len(parts) == 3 and parts[0].isdigit() and parts[2] not in records:
                added += int(parts[0])
        commits.append({"at": int(when), "ids": ids, "added": added})
    return commits


def status_history(repo: Path, map_rel: str) -> tuple[dict, dict]:
    """Возвраты и последний переход в закрытый статус по истории карты."""
    returns: dict = {}
    closed_at: dict = {}
    for task_id, rows in cards.status_transitions(repo, map_rel).items():
        for at, old, new in rows:
            if old in CLOSED and new in REWORK:
                returns[task_id] = returns.get(task_id, 0) + 1
            if new in CLOSED and old not in CLOSED:
                closed_at[task_id] = max(closed_at.get(task_id, 0), at)
    return returns, closed_at


def waste(task: dict) -> float:
    """Счёт потерь. Единица — одна переделка: возврат, неделя срока,
    десять коммитов, каждое удвоение факта сверх оценки."""
    overrun = math.log2(task["ratio"]) if task["ratio"] and task["ratio"] > 1 else 0.0
    weeks = (task["hours"] or 0) / WEEK_HOURS
    return round(task["returns"] + weeks + task["commits"] / 10 + overrun, 2)


def real_estimate(card: dict) -> int | None:
    """volume.loc, если его поставил человек или смена, а не автомат."""
    volume = card.get("volume") or {}
    return (volume.get("loc") or None) if volume.get("estimated") is False else None


def median_or_none(values: list):
    return statistics.median(values) if values else None


def build(repo: Path, map_path: Path, archive_path: Path, now: float) -> dict:
    tasks = map_tasks(map_path, archive_path)
    map_rel, archive_rel = (os.path.relpath(p.resolve(), repo.resolve())
                            for p in (map_path, archive_path))
    commits = trailer_commits(repo, {map_rel, archive_rel})
    returns, closed_at = status_history(repo, map_rel)
    facts: dict = {}
    outside = set()
    for commit in commits:
        for task_id in commit["ids"]:
            if task_id not in tasks:
                outside.add(task_id)
                continue
            fact = facts.setdefault(task_id, {"first": commit["at"], "last": commit["at"],
                                              "commits": 0, "added": 0})
            fact["first"] = min(fact["first"], commit["at"])
            fact["last"] = max(fact["last"], commit["at"])
            fact["commits"] += 1
            fact["added"] += commit["added"]
    rows = []
    for task_id, fact in facts.items():
        card = tasks[task_id]
        status = card.get("status")
        # В архиве задача уже убрана из работы, какой бы статус ни несла.
        is_open = status in REWORK and not card["archived"]
        end = closed_at.get(task_id, 0)
        if is_open:
            end = now
        elif end < fact["first"]:
            # Закрыта раньше первого коммита с трейлером (карточку завели уже
            # в test) — срок меряем по последнему коммиту.
            end = fact["last"]
        estimate = real_estimate(card)
        row = {
            "id": task_id, "status": status, "open": is_open,
            "character": (card.get("глубина") or {}).get("характер") or "—",
            "commits": fact["commits"], "returns": returns.get(task_id, 0),
            "hours": round((end - fact["first"]) / HOUR, 1) if end > fact["first"] else None,
            "loc_estimate": estimate, "loc_fact": fact["added"],
            "ratio": round(fact["added"] / estimate, 2) if estimate else None,
        }
        row["score"] = waste(row)
        rows.append(row)
    rows.sort(key=lambda r: (-r["score"], r["id"]))
    by_character: dict = {}
    for row in rows:
        by_character.setdefault(row["character"], []).append(row)
    summary = {
        name: {
            "tasks": len(group),
            "timed": sum(r["hours"] is not None for r in group),
            "median_hours": median_or_none([r["hours"] for r in group if r["hours"] is not None]),
            "commits": sum(r["commits"] for r in group),
            "returns": sum(r["returns"] for r in group),
            "median_ratio": median_or_none([r["ratio"] for r in group if r["ratio"] is not None]),
        }
        for name, group in sorted(by_character.items(), key=lambda kv: -len(kv[1]))
    }
    return {"tasks": rows, "by_character": summary, "outside_map": len(outside)}


def render(report: dict, top: list) -> str:
    out = [f"Эффективность задач: {len(report['tasks'])} с коммитами, "
           f"id трейлеров вне карты: {report['outside_map']}",
           f"Топ-{len(top)} неэффективных (счёт: возврат, неделя, 10 коммитов, удвоение оценки — по 1):"]
    for n, r in enumerate(top, 1):
        if r["hours"] is None:
            span = "срок не замерен"
        else:
            span = f"{round(r['hours'] / 24, 1)} дн. " + ("идёт" if r["open"] else "до закрытия")
        lines = (f"строк {r['loc_fact']}/{r['loc_estimate']} ×{r['ratio']}" if r["ratio"] is not None
                 else f"строк {r['loc_fact']}, оценки нет")
        out.append(f" {n}. {r['id']} [{r['character']}, {r['status']}] — {span}, "
                   f"{r['commits']} коммитов, возвратов {r['returns']}, {lines}; счёт {r['score']}")
    out.append("По характеру (медиана срока — по задачам с замеренным сроком):")
    for name, s in report["by_character"].items():
        ratio = f", медиана факт/оценка ×{s['median_ratio']}" if s["median_ratio"] is not None else ""
        hours = (f"медиана {s['median_hours']} ч по {s['timed']}" if s["median_hours"] is not None
                 else "срок не замерен ни у одной")
        out.append(f" {name}: {s['tasks']} задач, {hours}, "
                   f"коммитов {s['commits']}, возвратов {s['returns']}{ratio}")
    return "\n".join(out)


def main() -> int:
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=here)
    ap.add_argument("--map", type=Path, default=None, help="карта; по умолчанию <repo>/dev-map.yaml")
    ap.add_argument("--archive", type=Path, default=None,
                    help="архив карты; по умолчанию <repo>/docs/dev-map-archive.yaml")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--now", type=float, default=None, help="«сейчас» в секундах эпохи")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    map_path = args.map or args.repo / "dev-map.yaml"
    archive_path = args.archive or args.repo / "docs" / "dev-map-archive.yaml"
    report = build(args.repo, map_path, archive_path,
                   args.now if args.now is not None else time.time())
    top = report["tasks"][:max(0, args.top)]
    if args.json:
        print(json.dumps(dict(report, top=top), ensure_ascii=False, indent=1))
    else:
        print(render(report, top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
