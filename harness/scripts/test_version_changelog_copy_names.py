#!/usr/bin/env python3
"""Проба: собранный CHANGELOG.md не несёт имён копий сети из прошлых секций.

Находка 20.11.2026 (пачка l144): замок сборки (check-public-clean.py) судит
имена копий сети из `серверы/*.md` каталога EVOLUTION_DIR. CHANGELOG.md
публичной сборки собирает version.py (оформить → _из_истории): берёт
HEAD:CHANGELOG.md сборки — ВСЕ прошлые секции — и ставит новую сверху.
В прошлых секциях уже опубликованы имена копий («Находки <копия>»);
замок 6б publish.sh (второй прогон судьи по дереву с заметками) с новым
образцом отказал бы каждому выпуску, и дверь стала бы непроходимой.

Договор пробы: version.py --оформить на сборке, в истории которой прошлая
секция несёт имена копий, а каталог сети (EVOLUTION_DIR паспорта) их
называет, кладёт CHANGELOG.md, где
1. прошлая секция на месте (история версий не срезана) и смысл строк цел;
2. имён копий целым словом нет — подмена нейтральная, та же, что у сборщика;
3. замок на сборке с этим журналом — код 0.
Сосед-условие: тот же журнал до подмены замок отвергает (код 1) — иначе
проба зеленела бы на слепом замке.

Паспорт version.py получает дверями config.py: HARNESS_INSTALL_CONF и
HARNESS_CONF. Дверь мутаций: VERSION_UNDER_TEST — путь к копии version.py;
PUBLIC_CLEAN_UNDER_TEST — к копии check-public-clean.py.

    python3 scripts/test_version_changelog_copy_names.py
Код возврата: 0 — все случаи прошли, 1 — есть упавший.
"""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ЗДЕСЬ = Path(__file__).resolve().parent
ВЕРСИЯ = Path(os.environ.get("VERSION_UNDER_TEST") or ЗДЕСЬ / "version.py")
ЗАМОК = Path(os.environ.get("PUBLIC_CLEAN_UNDER_TEST") or ЗДЕСЬ / "check-public-clean.py")
# Имена выдуманные: у копии сети прозвище продукта — имя её каталога
# (USTANOVIT.sh), и настоящее имя здесь красит у неё гейт «харнес чист».
КОПИИ = ("Fabrika-Stenda", "kopiya-zq")
ПРОШЛАЯ = ("## [0.14.2] — 2026-11-18\n\n"
           "Собрано из пакета `1111111`. Изменений: 2.\n\n"
           f"- Находки {КОПИИ[0]}: порог поднят\n"
           f"- Ответ {КОПИИ[1]}: журнал смены дописан\n")


def загрузить(путь: Path, имя: str):
    sys.path.insert(0, str(путь.resolve().parent / "lib"))
    спец = importlib.util.spec_from_file_location(имя, путь)
    модуль = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(модуль)
    return модуль


def git(*доводы):
    subprocess.run(["git", *доводы], check=True, capture_output=True)


def закоммитить(репо: Path, текст: str) -> None:
    git("-C", str(репо), "add", "-A")
    git("-C", str(репо), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", текст)


def стенд(корень: Path, замок) -> tuple:
    """Пакет, проект с картой, сеть с копиями; сборка с прошлым журналом в HEAD."""
    пакет, проект, сеть = корень / "пакет", корень / "проект", корень / "сеть"
    for к in (пакет, проект, сеть / "серверы"):
        к.mkdir(parents=True)
    git("-C", str(пакет), "init", "-q", "-b", "master")
    (пакет / "файл").write_text("пакет стенда\n", encoding="utf-8")
    закоммитить(пакет, "пакет стенда")
    git("-C", str(проект), "init", "-q", "-b", "master")
    (проект / "dev-map.yaml").write_text(
        "version: 1\nlines:\n  - id: harness\n    directions:\n"
        "      - id: hr.stend-proby\n        status: wip\n", encoding="utf-8")
    закоммитить(проект, "п")
    for имя in ("Источник", *КОПИИ):
        (сеть / "серверы" / f"{имя}.md").write_text(f"сервер: {имя}\n", encoding="utf-8")
    хэш = subprocess.run(["git", "-C", str(пакет), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    паспорт = {"PROJECT_NAME": "Источник", "PROJECT_DIR": str(проект), "AGENT_USER": "жилец",
               "EVOLUTION_DIR": str(сеть)}
    сборка = замок._подставная_сборка(корень, паспорт, хэш)
    журнал = сборка / "CHANGELOG.md"
    журнал.write_text("# Журнал изменений\n\nШапка стенда.\n\n" + ПРОШЛАЯ, encoding="utf-8")
    закоммитить(сборка, "версия от 2026-11-18, пакет 1111111")
    # Сборщик сносит артефакты релиза (rsync --delete): журнал живёт только в HEAD.
    журнал.unlink()
    return паспорт, сборка


def main() -> int:
    for путь in (ВЕРСИЯ, ЗАМОК):
        if not путь.is_file():
            print(f"ПРОБА СЛОМАНА: нет {путь}")
            return 1
    замок = загрузить(ЗАМОК, "public_clean_changelog")
    failed = total = 0

    def case(name, ok, got):
        nonlocal failed, total
        total += 1
        failed += not ok
        print(f"  {'ок  ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f": получили {got!r}"))

    with tempfile.TemporaryDirectory(prefix="o-changelog-copies-") as врем:
        корень = Path(врем)
        паспорт, сборка = стенд(корень, замок)
        журнал = сборка / "CHANGELOG.md"

        # Условие: замок видит имена копий в прошлой секции, положенной как есть.
        журнал.write_text(ПРОШЛАЯ, encoding="utf-8")
        код, сказано = замок._вердикт(сборка, паспорт, корень)
        case("условие: прошлая секция с именами копий — замок отвергает",
             код == 1 and any("CHANGELOG.md" in с for с in сказано), (код, сказано[-3:]))
        журнал.unlink()

        конф = корень / "конф.conf"
        конф.write_text("".join(f'{к}="{з}"\n' for к, з in паспорт.items()), encoding="utf-8")
        среда = dict(os.environ, HARNESS_INSTALL_CONF=str(конф), HARNESS_CONF=str(конф))
        готово = subprocess.run(
            [sys.executable, str(ВЕРСИЯ), "--оформить", "--пакет", str(корень / "пакет"),
             "--сборка", str(сборка), "--проект", str(корень / "проект")],
            capture_output=True, text=True, timeout=120, env=среда)
        case("version.py --оформить отработал", готово.returncode == 0,
             (готово.returncode, готово.stderr[-200:]))
        текст = журнал.read_text(encoding="utf-8") if журнал.exists() else ""

        case("прошлая секция на месте, смысл строк цел",
             "## [0.14.2]" in текст and "порог поднят" in текст
             and "журнал смены дописан" in текст, текст[-400:])
        остались = [и for и in КОПИИ
                    if re.search(rf"(?<![^\W_]){re.escape(и)}(?![^\W_])", текст, re.I)]
        case("БОЛЬНОЙ СЛУЧАЙ: имён копий сети в собранном журнале нет", not остались,
             (остались, текст[-300:]))
        код, сказано = замок._вердикт(сборка, паспорт, корень)
        case("БОЛЬНОЙ СЛУЧАЙ замка 6б: на собранном журнале замок — код 0",
             код == 0 and any("следов установки нет" in с for с in сказано), (код, сказано[-3:]))

    print(f"ИТОГ: {total - failed}/{total}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
