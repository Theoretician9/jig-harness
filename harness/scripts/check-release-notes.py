#!/usr/bin/env python3
"""Замок публикации: релиз без записи простым языком не уходит.

Судит ВЕРХНЮЮ запись ЧТО-НОВОГО.md в собранном дереве: она новая (в прошлом
опубликованном коммите сборки такого заголовка нет), заполнена и без жаргона.

Запуск:  python3 scripts/check-release-notes.py <каталог сборки>
Код: 0 — годится, 1 — отказ с причинами.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import release_notes as заметки  # noqa: E402


def вышедшие_заголовки(сборка: Path) -> set:
    """Заголовки записей из последнего коммита сборки; истории нет — пусто."""
    готово = subprocess.run(["git", "-C", str(сборка), "show", f"HEAD:{заметки.ИМЯ_ФАЙЛА}"],
                            capture_output=True, text=True)
    if готово.returncode != 0:
        return set()
    return {заголовок for заголовок, _ in заметки.записи(готово.stdout)}


def main(argv: list) -> int:
    if len(argv) < 2:
        print("использование: check-release-notes.py <каталог сборки>", file=sys.stderr)
        return 2
    сборка = Path(argv[1])
    файл = сборка / заметки.ИМЯ_ФАЙЛА
    if not файл.is_file():
        print(f"ОТКАЗ: нет файла {заметки.ИМЯ_ФАЙЛА} в сборке — выпуск без заметок простым языком")
        return 1
    причины = заметки.отказы(файл.read_text(encoding="utf-8"), вышедшие_заголовки(сборка))
    for причина in причины:
        print(f"ОТКАЗ: {причина}")
    if not причины:
        print(f"заметки выпуска: {заметки.ИМЯ_ФАЙЛА} годится")
    return 1 if причины else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
