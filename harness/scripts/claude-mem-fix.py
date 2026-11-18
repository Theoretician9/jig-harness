#!/usr/bin/env python3
"""Расписание сведения автопамяти: обёртка над scripts/fix-claude-mem-project.py.

Зачем отдельный файл: оригинал не трогаем (он общий с шаблоном установки), а
гейт «писатели журналов» требует events.run( в файле каждого задания
system-jobs.yaml. Сведение памяти харнеса — дело расписания харнеса, а не
выката продукта (спека §5.4 Г5), поэтому выкат его больше не зовёт.

Ключи (--dry-run) уходят оригиналу как есть; код его возврата — наш.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import events  # noqa: E402

SRC = "scripts/claude-mem-fix.py"
ORIGINAL = HERE / "fix-claude-mem-project.py"


def fix() -> int:
    code = subprocess.run([sys.executable, str(ORIGINAL), *sys.argv[1:]]).returncode
    # Свой итог в журнал: события run.start/run.end есть и без него, но реестр журналов
    # (гейт G2) узнаёт писателя по явному emit(journal=…).
    events.emit(SRC, "mem-fix.result", "ok" if code == 0 else "error", journal="claude-mem-fix",
                data={"код": code})
    return code


if __name__ == "__main__":
    sys.exit(events.run(SRC, fix, journal="claude-mem-fix", name="claude-mem-fix",
                        heartbeat="claude-mem-fix"))
