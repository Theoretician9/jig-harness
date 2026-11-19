#!/usr/bin/env python3
"""Судья класса служебных путей — один на всех, кто его спрашивает.

Класс — ключ DAEMON_OWNED_PATHS в harness.conf (пути через пробел, «/» на конце —
каталог целиком): файлы, которые пишут демоны и механизмы, а не смена. Свой разбор
ключа у каждого (stop-хук, демон памяти, ворота, публикация) разошёлся бы молча.

  --путь <p>                     0 — путь в классе, 1 — нет.
  <от> <до> [--под <префикс>]    0 — разница коммитов только в классе и только
                                 добавленными строками.
  <от> [--под <префикс>]         то же против рабочего дерева (индекс, диск и
                                 неотслеженные файлы).

Почему только добавления: отметки разбора — данные проверок «указания» и «задачи
из канала». Новая строка гасит долг, удалённая его воскрешает — такая правка
меняет суд и служебной не бывает. Двоичный файл, удаление и переименование —
тоже не служебные. `--под память/`: docs/приёмка/ в классе для stop-хука, но его
читают проверки, и для итога ворот он не служебный.

Пустой или отсутствующий ключ — код 1 в любом режиме: пустой разбор не делает всё
служебным. Отказ git — код 2.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

KEY = re.compile(r"""^\s*DAEMON_OWNED_PATHS\s*=\s*(?:"([^"]*)"|'([^']*)'|([^#\s]*))""", re.M)


def conf_path() -> str:
    # Судью кладут и одного, без lib/ рядом (каталог скриптов двора пробы публикации):
    # с дверью HARNESS_CONF загрузчик не нужен.
    if os.environ.get("HARNESS_CONF"):
        return os.environ["HARNESS_CONF"]
    sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
    import config
    return config.путь_harness_conf()


def owned_paths() -> list[str]:
    try:
        text = Path(conf_path()).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    m = KEY.search(text)
    return "".join(g or "" for g in m.groups()).split() if m else []


def in_class(path: str, owned: list[str], under: str = "") -> bool:
    if under and not path.startswith(under):
        return False
    return any(path.startswith(o) if o.endswith("/") else path == o for o in owned)


def git_z(*args: str) -> list[str]:
    """Вывод git с -z, разобранный по NUL; отказ git — SystemExit(2)."""
    proc = subprocess.run(["git", "-c", "core.quotepath=false", *args],
                          capture_output=True, text=True, errors="replace")
    if proc.returncode:
        print(f"git {' '.join(args)}: {proc.stderr.strip()}", file=sys.stderr)
        raise SystemExit(2)
    return [x for x in proc.stdout.split("\0") if x]


def foreign(refs: list[str], owned: list[str], under: str) -> list[str]:
    """Что в разнице не служебное: путь вне класса или удалённые строки."""
    bad = []
    # --no-renames: иначе переименование приходит одной записью с 0/0 и прошло бы.
    # Двоичный файл numstat даёт «-\t-» — удалений не «0», отказ тем же условием.
    for entry in git_z("diff", "--numstat", "-z", "--no-renames", *refs):
        added, deleted, path = entry.split("\t", 2)
        if not in_class(path, owned, under):
            bad.append(f"{path}: вне класса")
        elif deleted != "0":
            bad.append(f"{path}: удалено строк {deleted}")
    if len(refs) == 1:
        for path in git_z("ls-files", "-z", "--others", "--exclude-standard"):
            if not in_class(path, owned, under):
                bad.append(f"{path}: неотслеженный вне класса")
    return bad


def main(argv: list[str]) -> int:
    owned = owned_paths()
    if not owned:
        print("ключ DAEMON_OWNED_PATHS пуст или не найден — служебного класса нет", file=sys.stderr)
        return 1
    if argv[:1] == ["--путь"] and len(argv) == 2:
        return 0 if in_class(argv[1], owned) else 1
    under = ""
    if "--под" in argv:
        i = argv.index("--под")
        under = argv[i + 1] if i + 1 < len(argv) else ""
        argv = argv[:i] + argv[i + 2:]
    if len(argv) not in (1, 2):
        print(__doc__, file=sys.stderr)
        return 2
    bad = foreign(argv, owned, under)
    for line in bad:
        print(line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
