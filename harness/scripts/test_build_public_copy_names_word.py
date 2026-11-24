#!/usr/bin/env python3
"""Проба: сборщик подменяет имя копии сети только целым словом и не съедает им другие следы.

Находка 20.11.2026 (пачка l144): замок сборки (check-public-clean.py) судит
имена копий сети из `серверы/*.md` каталога EVOLUTION_DIR, а сборщик
(build-public.py, обезличить_код) берёт образцы у замка и подменяет их
ПОДСТРОКОЙ. Два следствия:

* Имя копии внутри другого следа съедает след. В дереве помощника
  PROJECT_DIR — `/var/www/<имя копии>/.claude/worktrees/…`; в рабочем коде
  путь не подменяется (это умолчание поведения, его чинит причина, а не
  сборка), но имя копии внутри пути подменялось номерным словом — путь
  переставал совпадать с образцом PROJECT_DIR, и замок 6 пропускал сборку.
  Самотест publish.sh краснел: «след установки не остановил публикацию».
* Короткое имя копии живёт внутри чужих слов («bkbx», «abkb»): подмена
  подстрокой их калечит, хотя замок судит слово целиком.

Договор пробы: после обезличить_код
1. путь установки в рабочем коде остаётся целым, и замок на сборке
   отказывает (код 1) с образцом PROJECT_DIR;
2. в тесте путь подменён правилом путей (/var/www/project), а не номерным
   словом внутри пути;
3. имя копии целым словом подменено, внутри чужого слова — не тронуто,
   и замок на результате зелёный (код 0).

Дверь мутаций: BUILD_PUBLIC_UNDER_TEST — путь к копии build-public.py;
PUBLIC_CLEAN_UNDER_TEST — к копии check-public-clean.py.

    python3 scripts/test_build_public_copy_names_word.py
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
СБОРЩИК = Path(os.environ.get("BUILD_PUBLIC_UNDER_TEST") or ЗДЕСЬ / "build-public.py")
ЗАМОК = Path(os.environ.get("PUBLIC_CLEAN_UNDER_TEST") or ЗДЕСЬ / "check-public-clean.py")
# Имя копии стоит и каталогом в пути установки — как project в дереве помощника.
КОПИЯ_В_ПУТИ = "Соседка"
КОРОТКАЯ = "sample-33"
ПУТЬ = f"/var/www/{КОПИЯ_В_ПУТИ}/.claude/worktrees/agent-x"


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
    """Пакет, проект с картой, каталог сети → паспорт и подставная сборка."""
    пакет, проект, сеть = корень / "пакет", корень / "проект", корень / "сеть"
    for к in (пакет, проект, сеть / "серверы"):
        к.mkdir(parents=True)
    git("-C", str(пакет), "init", "-q", "-b", "master")
    (пакет / "файл").write_text("пакет стенда\n", encoding="utf-8")
    закоммитить(пакет, "пакет")
    git("-C", str(проект), "init", "-q", "-b", "master")
    (проект / "dev-map.yaml").write_text(
        "version: 1\nlines:\n  - id: harness\n    directions:\n"
        "      - id: hr.stend-proby\n        status: wip\n", encoding="utf-8")
    закоммитить(проект, "п")
    for имя in ("Источник", КОПИЯ_В_ПУТИ, КОРОТКАЯ):
        (сеть / "серверы" / f"{имя}.md").write_text(f"сервер: {имя}\n", encoding="utf-8")
    хэш = subprocess.run(["git", "-C", str(пакет), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    паспорт = {"PROJECT_NAME": "Источник", "PROJECT_DIR": ПУТЬ, "AGENT_USER": "жилец",
               "EVOLUTION_DIR": str(сеть)}
    return паспорт, замок._подставная_сборка(корень, паспорт, хэш)


def main() -> int:
    for путь in (СБОРЩИК, ЗАМОК):
        if not путь.is_file():
            print(f"ПРОБА СЛОМАНА: нет {путь}")
            return 1
    сборщик = загрузить(СБОРЩИК, "build_public_copy_names")
    замок = загрузить(ЗАМОК, "public_clean_copy_names_word")
    failed = total = 0

    def case(name, ok, got):
        nonlocal failed, total
        total += 1
        failed += not ok
        print(f"  {'ок  ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f": получили {got!r}"))

    with tempfile.TemporaryDirectory(prefix="o-copy-word-") as врем:
        корень = Path(врем) / "путь"
        паспорт, сборка = стенд(корень, замок)
        код_файл = сборка / "harness" / "след.py"
        тест_файл = сборка / "harness" / "test_след.py"
        код_файл.write_text(f'ПУТЬ = "{ПУТЬ}"\n', encoding="utf-8")
        тест_файл.write_text(f'ДВОР = "{ПУТЬ}/данные"\n', encoding="utf-8")
        сборщик.обезличить_код(сборка, паспорт, корень / "пакет")
        закоммитить(сборка, "в")
        код_текст = код_файл.read_text(encoding="utf-8")
        тест_текст = тест_файл.read_text(encoding="utf-8")

        case("БОЛЬНОЙ СЛУЧАЙ: путь установки в рабочем коде цел — имя копии внутри него "
             "не подменено", ПУТЬ in код_текст, код_текст)
        код, сказано = замок._вердикт(сборка, паспорт, корень)
        case("БОЛЬНОЙ СЛУЧАЙ самотеста publish.sh: след пути в коде — замок отказывает "
             "с образцом PROJECT_DIR", код == 1 and any("PROJECT_DIR" in с for с in сказано),
             (код, сказано[-3:]))
        case("сосед: в тесте путь подменён правилом путей, без номерного слова внутри",
             "/var/www/project/данные" in тест_текст and "sample-" not in тест_текст,
             тест_текст)

    with tempfile.TemporaryDirectory(prefix="o-copy-word-") as врем:
        корень = Path(врем) / "слово"
        паспорт, сборка = стенд(корень, замок)
        файл = сборка / "harness" / "имена.py"
        чужие = f"{КОРОТКАЯ}x a{КОРОТКАЯ} {КОРОТКАЯ}2"
        файл.write_text(f"# находка копии {КОРОТКАЯ}: порог поднят\n"
                        f"# слова не копии: {чужие}\nПОРОГ = 15\n", encoding="utf-8")
        сборщик.обезличить_код(сборка, паспорт, корень / "пакет")
        закоммитить(сборка, "в")
        текст = файл.read_text(encoding="utf-8")
        граница = re.compile(rf"(?<![^\W_]){КОРОТКАЯ}(?![^\W_])", re.I)

        case("имя копии целым словом подменено", not граница.search(текст)
             and "порог поднят" in текст, текст)
        case("БОЛЬНОЙ СЛУЧАЙ: имя копии внутри чужого слова не тронуто",
             f"# слова не копии: {чужие}\n" in текст, текст)
        код, сказано = замок._вердикт(сборка, паспорт, корень)
        case("сосед: на результате подмены замок зелёный",
             код == 0 and any("следов установки нет" in с for с in сказано), (код, сказано[-3:]))

    print(f"ИТОГ: {total - failed}/{total}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
