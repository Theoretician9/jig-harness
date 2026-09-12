#!/usr/bin/env python3
"""Гейт: git, чей вывод читает код, зовётся с `-c core.quotepath=false`.

Без этого флага git экранирует нелатинские пути байтами: `harness/panel/x.py`
приходит как `"\\321\\205\\320\\260..."`. Дальше слепнет всё, что судит по
путям — поиск по имени не находит, отчёт печатает мусор вместо имени файла.

Живые случаи 12.09.2026, оба на глазах владельца:
  * замер «файлов кода кириллицей: 0» — при том что `УСТАНОВИТЬ.sh` лежит на
    GitHub и виден глазами; `git ls-files | grep УСТАНОВИТЬ` отдавал пусто;
  * гейт чистоты публичной сборки напечатал в отчёт `"\\321\\201\\320\\273..."`
    вместо имени файла с находкой.

Правило узкое нарочно: флаг нужен там, где ПУТЬ из вывода git читается кодом
(ls-files, grep -l, diff --name-only, status --porcelain, show --stat). Вызовы,
у которых на выходе не путь (rev-parse, log --format, merge-base, commit),
правила не требуют — иначе гейт краснел бы на каждом втором вызове и его бы
выключили.

Запуск:
  python3 scripts/check-quotepath.py            # по всему дереву
  python3 scripts/check-quotepath.py --selftest
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent

# Подкоманды git, у которых в выводе стоят ПУТИ.
ПУТИ_В_ВЫВОДЕ = ("ls-files", "ls-tree", "diff-tree", "status")
# Пары «подкоманда + довод»: сама по себе подкоманда путей не печатает.
ПУТИ_С_ДОВОДОМ = {
    "grep": ("-l", "-L", "--name-only", "--files-with-matches"),
    "diff": ("--name-only", "--name-status", "--stat"),
    "show": ("--name-only", "--name-status", "--stat"),
    "log": ("--name-only", "--name-status", "--stat"),
}
ФЛАГ = "core.quotepath"


# Признак ЗАПУСКА, а не упоминания. Без него гейт краснел на образцах команд
# внутри тестов («git status --short» строкой данных в test_guard_bash.py) —
# это текст, который никто не исполняет, и путей оттуда никто не читает.
ЗАПУСК = ("subprocess.run", "subprocess.Popen", "check_output", "_гит(", "гит(")


def вызовы_git(текст: str) -> list:
    """Куски строк, где git ЗАПУСКАЕТСЯ. Пары (номер строки, текст вызова)."""
    найдено = []
    for номер, строка in enumerate(текст.splitlines(), 1):
        # Комментарий и строка документации — не вызов (проверка судит код).
        без_комментария = строка.split("#")[0]
        if "git" not in без_комментария:
            continue
        if not any(з in без_комментария for з in ЗАПУСК):
            continue
        for кусок in re.finditer(r'["\']git["\']?[^\n]*', без_комментария):
            найдено.append((номер, кусок.group(0)))
    return найдено


def нужен_флаг(вызов: str) -> bool:
    """В выводе этого вызова будут пути?"""
    if any(f'"{под}"' in вызов or f"'{под}'" in вызов or f" {под} " in вызов
           for под in ПУТИ_В_ВЫВОДЕ):
        return True
    for под, доводы in ПУТИ_С_ДОВОДОМ.items():
        есть_под = f'"{под}"' in вызов or f"'{под}'" in вызов or f" {под} " in вызов
        if есть_под and any(д in вызов for д in доводы):
            return True
    return False


# Файл, где образцы вызовов — ДАННЫЕ пробы, а не работа: сам этот гейт.
# Иначе он краснеет на своём же самотесте, а краснеющий без причины гейт
# отключают (правило проекта).
СВОИ_ОБРАЗЦЫ = "check-quotepath.py"


def проверить(файлы: list) -> list:
    находки = []
    for путь in файлы:
        if путь.endswith(СВОИ_ОБРАЗЦЫ):
            continue
        try:
            текст = Path(путь).read_text(encoding="utf-8")
        except OSError:
            continue
        for номер, вызов in вызовы_git(текст):
            if нужен_флаг(вызов) and ФЛАГ not in вызов:
                находки.append(f"{путь}:{номер}: git печатает пути без {ФЛАГ} "
                               f"— нелатинские имена придут байтами")
    return находки


def дерево() -> list:
    готово = subprocess.run(
        ["git", "-C", str(КОРЕНЬ), "-c", "core.quotepath=false", "ls-files",
         "scripts/*.py", "scripts/hooks/*.py", "harness/*.py"],
        capture_output=True, text=True, timeout=60)
    return [str(КОРЕНЬ / п) for п in готово.stdout.split("\n") if п.strip()]


def самотест() -> int:
    """Больной случай первым: вызов без флага обязан стать находкой."""
    путей = неудач = 0

    def проба(ок: bool, текст: str) -> None:
        nonlocal путей, неудач
        путей += 1
        неудач += 0 if ок else 1
        print(("  ок    " if ок else "  ПЛОХО ") + текст)

    with tempfile.TemporaryDirectory(prefix="самотест-quotepath-") as врем:
        каталог = Path(врем)
        случаи = [
            ('слепой.py', 'subprocess.run(["git", "ls-files"])', 1,
             "БОЛЬНОЙ: ls-files без флага — находка"),
            ('зрячий.py',
             'subprocess.run(["git", "-c", "core.quotepath=false", "ls-files"])', 0,
             "тот же вызов с флагом — не находка"),
            ('grep_l.py', 'subprocess.run(["git", "grep", "-l", "образец"])', 1,
             "БОЛЬНОЙ: grep -l печатает пути — находка"),
            ('grep_c.py', 'subprocess.run(["git", "grep", "-c", "образец"])', 0,
             "grep -c печатает числа, не пути — не находка"),
            ('rev.py', 'subprocess.run(["git", "rev-parse", "HEAD"])', 0,
             "rev-parse отдаёт хэш — флаг не нужен"),
            ('diff.py', 'subprocess.run(["git", "diff", "--name-only"])', 1,
             "БОЛЬНОЙ: diff --name-only — находка"),
            ('коммент.py', '# git ls-files в документации — не вызов\nx = 1', 0,
             "упоминание в комментарии находкой не считается"),
            # БОЛЬНОЙ СЛУЧАЙ ложного срабатывания: образец команды строкой
            # данных внутри теста. Его никто не исполняет, путей из него не
            # читает — гейт, краснеющий на таком, отключают.
            ('образец.py', 'СЛУЧАИ = [(0, "обычная команда", "git status --short")]', 0,
             "БОЛЬНОЙ: образец команды в данных теста — не вызов"),
            # БОЛЬНОЙ: файл самого гейта. Его образцы — данные пробы; без
            # исключения гейт краснел на себе и был бы выключен в первый день.
            ('check-quotepath.py', 'subprocess.run(["git", "ls-files"])', 0,
             "БОЛЬНОЙ: собственные образцы гейта находкой не считаются"),
        ]
        for имя, тело, ждём, подпись in случаи:
            файл = каталог / имя
            файл.write_text(тело + "\n", encoding="utf-8")
            проба(len(проверить([str(файл)])) == ждём, подпись)

    if неудач:
        print("САМОТЕСТ ПРОВАЛЕН")
        return 1
    print(f"САМОТЕСТ ПРОЙДЕН: {путей} путей, первым — больной случай")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(самотест())
    свои = [д for д in sys.argv[1:] if not д.startswith("--")]
    находки = проверить(свои or дерево())
    if находки:
        for строка in находки:
            print(f"  {строка}")
        print(f"[quotepath] git зовётся без {ФЛАГ} там, где печатает пути: {len(находки)}")
        sys.exit(1)
    print("[quotepath] чисто: git с выводом путей всюду зовётся с флагом")
