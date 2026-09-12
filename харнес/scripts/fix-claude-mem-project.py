#!/usr/bin/env python3
"""Свести записи автопамяти к одному имени проекта.

Откуда взят: UNIFIED/templates/fix-claude-mem-project.py (живой скрипт боевого
сервера). Что изменено: корень репозитория и каноническое имя читаются из
/etc/harness/install.conf (мини-парсер ниже, конфиг не исполняется; env
HARNESS_INSTALL_CONF — для тестов); проектно-специфные списки KNOWN_STRAYS и
OTHER_PROJECTS ужаты до общих случаев чистого сервера — «admin»/«html»/staging
были именами ТОГО сервера, тащить их сюда значило бы сливать чужое.

Улика шаблона: плагин автопамяти определяет проект как имя каталога, в котором
стоит оболочка (basename(cwd)), и переопределить это настройкой нельзя — в
коде плагина такого ключа нет. Один заход в подкаталог — и записи хода уезжают
под чужое имя; при аварийном восстановлении поиск по проекту их не увидит.
Предотвратить нечем — значит чиним автоматически, как пропуск в карте.

Чужие проекты не трогаются: имя считается осколком, только если такой каталог
реально лежит ВНУТРИ проекта (или это заранее названный служебный случай).

ВНИМАНИЕ — совпадение коротких имён каталогов: если у СОСЕДНЕГО проекта имя
совпадает с именем каталога внутри нашего (docs, scripts, src, api...), его
записи будут приняты за осколки и слиты под наш канон — разделить их обратно
будет нечем. Перед боевым прогоном: --dry-run, глазами по списку, и каждое
имя настоящего соседа — в OTHER_PROJECTS.

Запуск:
    python3 scripts/fix-claude-mem-project.py --dry-run
    python3 scripts/fix-claude-mem-project.py
"""
import argparse
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path


def read_conf(path: str) -> dict:
    """Мини-парсер KEY="value". Не source: скрипту нельзя исполнять конфиг."""
    conf: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"""\s*([A-Z][A-Z0-9_]*)=("[^"]*"|'[^']*'|[^#\s]*)""", line)
                if m:
                    raw = m.group(2)
                    val = raw[1:-1] if raw[:1] in "\"'" else raw
                    for name, seen in conf.items():
                        val = val.replace("${%s}" % name, seen).replace("$" + name, seen)
                    conf[m.group(1)] = val
    except OSError:
        pass
    return conf


CONF = read_conf(os.environ.get("HARNESS_INSTALL_CONF", "/etc/harness/install.conf"))

DB = Path.home() / ".claude-mem" / "claude-mem.db"
REPO = Path(CONF.get("PROJECT_DIR") or ".")
# Каноническое имя — имя проекта из паспорта установки; пустое — имя каталога
# (так делал шаблон: у него канон совпадал с basename корня).
CANONICAL = CONF.get("PROJECT_NAME") or REPO.name
TABLES = ("observations", "session_summaries")

# Имена, которые каталогом проекта не являются, но осколками быть могут:
# служебные каталоги Claude Code и самой автопамяти плюс домашний каталог
# агента. Списки того сервера («admin», «html») сюда не перенесены — это были
# его вебруты, у нового сервера будут свои; дополнять по мере поимки.
KNOWN_STRAYS = {".claude", ".claude-mem", CONF.get("AGENT_USER") or "agent"}

# Настоящие соседние проекты. Явный список важнее любой эвристики: ошибка
# здесь сливает чужую историю с нашей, и разделить её обратно будет нечем.
# На чистом сервере соседей нет; появится второй проект — вписать сюда.
OTHER_PROJECTS: set = set()


def repo_dir_names() -> set:
    """Имена каталогов проекта — по учтённым в git файлам.

    Улика шаблона: из git, а не обходом файловой системы — обход втянул бы
    node_modules и прочий мусор, где найдётся имя на любой вкус, и под
    слияние попало бы чужое.
    """
    import subprocess

    out = subprocess.run(
        # quotepath=false: кириллические каталоги («память») иначе приходят
        # экранированными и не совпали бы с именами в базе автопамяти.
        ["git", "-C", str(REPO), "-c", "core.quotepath=false", "ls-files"],
        capture_output=True, text=True, timeout=30
    )
    names = set()
    for line in out.stdout.splitlines():
        names.update(Path(line).parts[:-1])
    return names


def is_stray(name: str, dir_names: set) -> bool:
    """Осколок нашего проекта, а не чужой проект."""
    if name == CANONICAL or name in OTHER_PROJECTS:
        return False
    return name in dir_names or name in KNOWN_STRAYS


def strays(conn: sqlite3.Connection, dir_names: set) -> dict:
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
        # Штатно на чистом сервере: служба памяти ставится после приёмки
        # базового харнеса (08-таблица расширений). Не ошибка.
        print(f"база автопамяти не найдена: {DB} — служба памяти ещё не поставлена")
        return 0

    if not (REPO / ".git").exists():
        print(f"репозиторий {REPO} ещё не инициализирован — сводить нечего")
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

    # Метка времени в имени: второй прогон не имеет права затереть снимок
    # первого — иначе «откатиться к до-первого-прогона» уже некуда.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = DB.with_name(f"{DB.name}.{stamp}.bak")
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
