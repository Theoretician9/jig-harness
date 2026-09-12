#!/usr/bin/env python3
"""Свести записи автопамяти к одному имени проекта.

Плагин автопамяти определяет проект как имя каталога, в котором стоит оболочка
(`basename(cwd)`), и переопределить это настройкой нельзя — в коде плагина такого
ключа нет. Стоит один раз выполнить команду с `cd docs/harness-export && …`, и
записи того хода уезжают под имя `harness-export`. При аварийном восстановлении
поиск по проекту их не увидит: половина работы дня пропадает из виду.

Предотвратить нечем — значит чиним автоматически, как пропуск в карте разработки.
Скрипт сводит осколки к каноническому имени. Чужие проекты не трогает: имя
считается осколком, только если такой каталог реально лежит ВНУТРИ проекта
(или это заранее названный случай вроде домашнего каталога).

Полнотекстовые индексы переживают правку: они построены по текстовым колонкам и
поле проекта не индексируют (проверено по схеме).

Запуск:
    python3 scripts/fix-claude-mem-project.py --dry-run
    python3 scripts/fix-claude-mem-project.py
"""
import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

DB = Path.home() / ".claude-mem" / "claude-mem.db"
REPO = Path("/opt/<проект>")  # корень репозитория проекта
CANONICAL = REPO.name
TABLES = ("observations", "session_summaries")

# Имена, которые каталогом проекта не являются, но осколками быть могут: оболочка
# уходила в домашний каталог, в родителя вебрута или в служебные каталоги харнеса.
KNOWN_STRAYS = {"admin", "html", ".claude", ".claude-mem"}

# Настоящие соседние проекты. Явный список важнее любой эвристики: ошибка здесь
# сливает чужую историю с нашей, и разделить её обратно будет нечем.
OTHER_PROJECTS = {"staging"}


def repo_dir_names() -> set[str]:
    """Имена каталогов проекта — по учтённым в git файлам.

    Берём из git, а не обходом файловой системы: обход втянул бы node_modules и
    прочий мусор, где найдётся имя на любой вкус, и под слияние попало бы чужое.
    """
    import subprocess

    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, timeout=30
    )
    names = set()
    for line in out.stdout.splitlines():
        names.update(Path(line).parts[:-1])
    return names


def is_stray(name: str, dir_names: set[str]) -> bool:
    """Осколок нашего проекта, а не чужой проект.

    Осколок — имя каталога внутри проекта (оболочка туда заходила) либо заранее
    названный случай. Соседние проекты защищены явным списком.
    """
    if name == CANONICAL or name in OTHER_PROJECTS:
        return False
    return name in dir_names or name in KNOWN_STRAYS


def strays(conn: sqlite3.Connection, dir_names: set[str]) -> dict[str, int]:
    """Имена-осколки и сколько записей под каждым."""
    found: dict[str, int] = {}
    for table in TABLES:
        for name, count in conn.execute(
            f"select project, count(*) from {table} group by project"
        ):
            if name and is_stray(name, dir_names):
                found[name] = found.get(name, 0) + count
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not DB.exists():
        print(f"база автопамяти не найдена: {DB}")
        return 0

    dir_names = repo_dir_names()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    found = strays(conn, dir_names)
    conn.close()

    if not found:
        print(f"осколков нет — всё под именем «{CANONICAL}»")
        return 0

    print("осколки (будут сведены к «%s»):" % CANONICAL)
    for name, count in sorted(found.items(), key=lambda kv: -kv[1]):
        why = "каталог проекта" if name in dir_names else "известный случай"
        print(f"  {count:>6}  {name}  ({why})")

    if args.dry_run:
        print("\n--dry-run: ничего не изменено")
        return 0

    backup = DB.with_suffix(".db.bak")
    shutil.copy2(DB, backup)
    print(f"\nснимок базы: {backup}")

    conn = sqlite3.connect(DB, timeout=60)
    total = 0
    try:
        with conn:
            for table in TABLES:
                for name in found:
                    cur = conn.execute(
                        f"update {table} set project = ? where project = ?",
                        (CANONICAL, name),
                    )
                    total += cur.rowcount
    except sqlite3.OperationalError as exc:
        print(f"не удалось записать ({exc}) — база занята воркером, попробуй позже")
        return 1
    finally:
        conn.close()

    print(f"сведено записей: {total}")

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    left = strays(conn, dir_names)
    conn.close()
    if left:
        print(f"ОСТАЛИСЬ осколки: {left}")
        return 1
    print("проверка: осколков не осталось")
    return 0


if __name__ == "__main__":
    sys.exit(main())
