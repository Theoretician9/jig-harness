#!/usr/bin/env python3
"""Трение коммитов: сколько попыток хука pre-commit приходится на один коммит master.

    python3 scripts/commit-friction.py --дней 7           текст
    python3 scripts/commit-friction.py --дней 7 --json    один объект

Источники за N дней (окно по метке времени): $LOG_DIR/gates.jsonl (прогоны pre-commit),
merge-queue.jsonl (итоги очереди; прогон её ворот — строка с числом в data.ворота_с, он
в gates.jsonl тоже pre-commit и из прогонов хука вычитается), guard.jsonl и
pamyat-kommit.log (следы замков git); коммиты — `git log --first-parent master`.
Код возврата: 0 — ответ дан (в том числе «нет данных»), 2 — неверный вызов.
Контракт: .tasks/hr.per-helper-code-copies/спека.md §4.5.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import config  # noqa: E402

LOCK_MARKERS = ("index.lock", "cannot lock ref")
MEMORY_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


def moment(text: str) -> dt.datetime | None:
    """Метка времени строки журнала; без пояса — местное время машины."""
    try:
        value = dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo else value.astimezone()


def read_rows(path: Path, since: dt.datetime) -> list[dict]:
    """Строки журнала JSONL в окне; битая строка и строка без метки не считаются."""
    rows: list[dict] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        stamp = moment(row.get("ts")) if isinstance(row, dict) else None
        if stamp and stamp >= since:
            rows.append(row)
    return rows


def numbers(rows: list[dict], key: str) -> list[float]:
    values = [(r.get("data") or {}).get(key) for r in rows]
    return [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]


def p50(values: list[float]):
    return statistics.median(values) if values else None


def lock_traces_in_guard(rows: list[dict]) -> int:
    return sum(1 for r in rows if any(m in str(r.get("cmd_head", "")) for m in LOCK_MARKERS))


def lock_traces_in_memory_log(path: Path, since: dt.datetime) -> int:
    """Строка с датой задаёт время следующим строкам с отступом — их и ищем."""
    if not path.is_file():
        return 0
    count, current = 0, None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stamp = MEMORY_STAMP.match(line)
        if stamp:
            current = moment(stamp.group(1).replace(" ", "T"))
        elif line[:1].isspace() and current and current >= since:
            count += any(m in line for m in LOCK_MARKERS)
    return count


def count_commits(project: str, since: dt.datetime) -> int:
    proc = subprocess.run(["git", "-C", project, "log", "--first-parent", "master",
                           f"--since={since.isoformat()}", "--format=%H"],
                          capture_output=True, text=True)
    return len(proc.stdout.split()) if proc.returncode == 0 else 0


def failures(hook_rows: list[dict]) -> Counter:
    """Красные прогоны по гейтам: поле failed — имена через запятую."""
    counter: Counter = Counter()
    for row in hook_rows:
        if row.get("verdict") == "fail":
            counter.update(name.strip() for name in str(row.get("failed", "")).split(",")
                           if name.strip())
    return counter


def measure(days: int) -> dict:
    log_dir = Path(config.log_dir())
    since = dt.datetime.now().astimezone() - dt.timedelta(days=days)
    hook = [r for r in read_rows(log_dir / "gates.jsonl", since) if r.get("gate") == "pre-commit"]
    queue = read_rows(log_dir / "merge-queue.jsonl", since)
    queue_gates = numbers(queue, "ворота_с")
    commits = count_commits(config.конфиг()["PROJECT_DIR"], since)
    hook_runs = max(len(hook) - len(queue_gates), 0)
    fell = failures(hook)
    waits = numbers(queue, "ожидание_с")
    return {
        "дней": days,
        "прогонов_хука": hook_runs,
        "прогонов_ворот_очереди": len(queue_gates),
        "коммитов": commits,
        "попыток_на_коммит": round(hook_runs / commits, 2) if commits else None,
        "падения": dict(fell),
        "package_sync": fell.get("package_sync", 0),
        "очередь": dict(Counter(str(r.get("event", "")).removeprefix("mq.") for r in queue)),
        "ожидание_с": {"p50": p50(waits), "max": max(waits) if waits else None},
        "ворота_с": {"p50": p50(queue_gates)},
        "следы_замков": lock_traces_in_guard(read_rows(log_dir / "guard.jsonl", since))
        + lock_traces_in_memory_log(log_dir / "pamyat-kommit.log", since),
    }


def is_empty(result: dict) -> bool:
    return not (result["прогонов_хука"] or result["прогонов_ворот_очереди"] or result["коммитов"]
                or result["очередь"] or result["следы_замков"])


def render(result: dict) -> str:
    if is_empty(result):
        return f"за {result['дней']} дн.: нет данных"
    attempts = result["попыток_на_коммит"]
    return "\n".join([
        f"трение коммитов за {result['дней']} дн.",
        f"  прогонов хука {result['прогонов_хука']} (ворот очереди ещё "
        f"{result['прогонов_ворот_очереди']}), коммитов master {result['коммитов']}, "
        f"попыток на коммит {'—' if attempts is None else attempts}",
        f"  падения по гейтам: {result['падения'] or 'нет'}; package_sync {result['package_sync']}",
        f"  очередь: {result['очередь'] or 'пуста'}; ожидание {result['ожидание_с']}, "
        f"ворота {result['ворота_с']}",
        f"  следов замков git: {result['следы_замков']}",
    ])


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="commit-friction.py", description=__doc__.splitlines()[0])
    parser.add_argument("--дней", type=int, required=True, dest="days")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if args.days <= 0:
        parser.error("--дней — целое больше нуля")
    return args


def main(argv: list[str]) -> int:
    args = parse(argv)
    result = measure(args.days)
    print(json.dumps(result, ensure_ascii=False) if args.as_json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
