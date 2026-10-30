#!/usr/bin/env python3
"""Probes of the map-writers gate on a writer by ARGUMENT, written BEFORE its code.

Task hr.watchers-report-must-create-model-task, criterion K-9 (spec R-14,
Ф-15; plan Task 1.2). check-map-writers.py sees a file only where the map path
is ASSIGNED and then written. The shared writer `write_cards_locked` in
scripts/lib/cards.py writes the path it RECEIVES, and alert-task.py writes
through it: lose the lock in the library, or let the new writer write the map
by itself, and today's gate stays silent.

Three groups of cases:
  * samples: a library writing the map by argument without dev-map.lock is
    red; the same with the lock is clean; a library writing a NON-map file by
    argument stays clean (the gate on the whole tree must not redden healthy code);
  * mutated COPIES of the real carriers: cards.py with the common lock taken
    away, alert-task.py writing the map by itself instead of through the
    library — each must be red;
  * the real tree: the gate stays clean, and its own --selftest names the case.

    python3 scripts/test_map_writers_library.py

Door for mutation runs ([[probe-loads-the-live-mechanism]]):
    HARNESS_MAP_WRITERS   path of check-map-writers.py under test
Last line: `map-writers library: случаев N, падений F`.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

SUBJECT = Path(os.environ.get("HARNESS_MAP_WRITERS") or HERE / "check-map-writers.py")
CARDS = HERE / "lib" / "cards.py"
WRITER = HERE / "alert-task.py"

cases = Cases()
check = cases.check

SICK_LIBRARY = '''"""Writes cards into dev-map.yaml."""
from pathlib import Path


def write_cards(map_path, line_id, cards):
    map_path = Path(map_path)
    text = map_path.read_text(encoding="utf-8")
    map_path.write_text(insert(text, line_id, cards), encoding="utf-8")
'''

SICK_LIBRARY_REPLACE = '''"""Writes cards into dev-map.yaml atomically."""
import os


def write_cards(map_path, text):
    tmp = map_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, map_path)
'''

SICK_LIBRARY_OPEN = '''"""Writes cards into dev-map.yaml."""


def write_cards(map_path, text):
    with open(map_path, "w", encoding="utf-8") as handle:
        handle.write(text)
'''

HEALTHY_LIBRARY = '''"""Writes cards into dev-map.yaml under the common lock."""
import fcntl
from pathlib import Path


def lock_path():
    return Path(LOG_DIR) / "locks" / "services" / "dev-map.lock"


def write_cards(map_path, line_id, cards):
    map_path = Path(map_path)
    with open(lock_path(), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        text = map_path.read_text(encoding="utf-8")
        map_path.write_text(insert(text, line_id, cards), encoding="utf-8")
'''

REPORT_LIBRARY = '''"""Reads dev-map.yaml and saves a report next to it."""
from pathlib import Path


def save_report(report_path, text):
    Path(report_path).write_text(text, encoding="utf-8")
'''


def load(path: Path):
    spec = importlib.util.spec_from_file_location("map_writers_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def findings(gate, text: str, path: str) -> int:
    return len(gate.находки_в(text, path))


def without_common_lock(text: str) -> str:
    """cards.py whose lock is its own, not the common one: the gate's sick case 2."""
    return text.replace("dev-map.lock", "cards-own.lock")


def writing_by_itself(text: str) -> tuple[str, int]:
    """alert-task.py writing the map by itself instead of through the library."""
    return re.subn(r"cards\.write_cards_locked\(\s*([\w.]+)\s*,[^)]*\)",
                   r"\1.write_text(build(\1.read_text()))", text)


def main() -> int:
    gate = load(SUBJECT)

    print("── образцы: библиотека пишет карту по аргументу (Р-14) ──")
    for name, text, want in (
            ("БОЛЬНОЙ: библиотека пишет карту по аргументу write_text без лока", SICK_LIBRARY, 1),
            ("БОЛЬНОЙ: та же через os.replace без лока", SICK_LIBRARY_REPLACE, 1),
            ("БОЛЬНОЙ: та же через open(…, \"w\") без лока", SICK_LIBRARY_OPEN, 1),
            ("та же библиотека с общим dev-map.lock — здорова", HEALTHY_LIBRARY, 0),
            ("библиотека пишет по аргументу НЕ карту, а отчёт — здорова", REPORT_LIBRARY, 0)):
        check(name, want, findings(gate, text, "scripts/lib/sample.py"))

    print("── копии боевых носителей с поломкой ──")
    cards_text = CARDS.read_text(encoding="utf-8")
    check("cards.py как есть — чист", 0, findings(gate, cards_text, "scripts/lib/cards.py"))
    mutated = without_common_lock(cards_text)
    check("мутация встала: в копии cards.py нет dev-map.lock", False, "dev-map.lock" in mutated)
    check("БОЛЬНОЙ: cards.py без общего лока — красный", True,
          findings(gate, mutated, "scripts/lib/cards.py") >= 1)

    writer_text = WRITER.read_text(encoding="utf-8")
    check("alert-task.py как есть — чист", 0, findings(gate, writer_text, "scripts/alert-task.py"))
    mutated, replaced = writing_by_itself(writer_text)
    check("мутация встала: зов write_cards_locked в копии alert-task.py заменён", 1, replaced)
    check("БОЛЬНОЙ: alert-task.py пишет карту сам, мимо общего писателя — красный", True,
          findings(gate, mutated, "scripts/alert-task.py") >= 1)

    print("── живое дерево и самотест гейта ──")
    done = subprocess.run([sys.executable, str(SUBJECT)], capture_output=True, text=True,
                          cwd=str(HERE.parent), timeout=120)
    check("гейт по живому дереву: код 0, «чисто»", (0, True),
          (done.returncode, "чисто" in done.stdout))
    done = subprocess.run([sys.executable, str(SUBJECT), "--selftest"], capture_output=True,
                          text=True, timeout=120)
    check("самотест гейта зелёный", 0, done.returncode)
    check("самотест гейта несёт случай «библиотека» (К-9)", True,
          "библиотек" in done.stdout.lower())

    print(f"map-writers library: случаев {cases.paths}, падений {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
