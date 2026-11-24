#!/usr/bin/env python3
"""Строки харнеса в .gitignore проекта: создать файл или дописать недостающие.

Живой .gitignore принадлежит проекту, но новый механизм харнеса приносит свою
строку (следы живой проверки, деревья помощников), и проект, установленный
раньше, её не получал: раскладчик писал заготовку только при отсутствии файла
(находка копии сети, 11.11.2026). Поэтому только дописываем — свои строки
проекта не трогаем и не переставляем. Стоящей считается строка целиком:
закомментированная или с отступом git читает иначе.

CLI: ignore_lines.py <путь .gitignore> — печатает строку итога.
"""
import sys
from pathlib import Path

HEADER = "# Создан razlozhit-harnes.sh: IDE-файлы и байткод не должны попадать в пакет/репо."
APPENDED_HEADER = "# Дописано razlozhit-harnes.sh: строки механизмов харнеса."
LINES = (
    "__pycache__/",
    "*.pyc",
    ".env",
    ".cursor/",
    ".cursorrules",
    ".vscode/",
    ".idea/",
    "docs/handover/*.tmp",
    # Замок писателей состояния живой проверки — файл машины, не данные.
    "harness/config/*.lock",
    # Следы живой проверки живут вне репозитория (LIVE_CHECK_TRACE_ROOT);
    # прежние каталоги в задачах не подхватывать.
    ".tasks/*/живьём",
    ".tasks/*/живьём-*",
    # Деревья помощников (HELPER_WORKTREE=1): не в общий индекс и не в гейты по диску.
    ".claude/worktrees/",
)


def ensure(path: Path) -> str:
    if not path.exists():
        path.write_text("\n".join((HEADER, *LINES)) + "\n", encoding="utf-8")
        return ".gitignore создан"
    text = path.read_text(encoding="utf-8")
    present = set(text.split("\n"))
    missing = [line for line in LINES if line not in present]
    if not missing:
        return ".gitignore: строки харнеса на месте"
    gap = "\n" if text and not text.endswith("\n") else ""
    path.write_text(text + gap + "\n".join((APPENDED_HEADER, *missing)) + "\n", encoding="utf-8")
    return f".gitignore: дописано строк {len(missing)}: {' '.join(missing)}"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: ignore_lines.py <путь .gitignore>", file=sys.stderr)
        sys.exit(2)
    print(ensure(Path(sys.argv[1])))
