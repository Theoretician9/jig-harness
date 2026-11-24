#!/usr/bin/env python3
"""Проба: замок сборки краснеет на имени копии сети, на котором краснеет её «харнес чист».

Находка копии сети 20.11.2026: после укладки 0.14.3 ворота копии
«харнес чист» (check-harness-clean.py) нашли имя «копии сети» в 14 файлах
пакета (mechanisms.yaml, probe-mutations.yaml, speed-modes.yaml,
name-words-allowlist.txt, server_hygiene_check.py…) — а замок сборки
(publish.sh, замок 6 → check-public-clean.py) ту же сборку пропустил.

Системная причина: два суда судят РАЗНЫЕ множества слов. Замок сборки берёт
образцы только из паспорта ИСТОЧНИКА (PROJECT_NAME, PRODUCT_ALIASES, адреса,
PUBLIC_FORBIDDEN_WORDS этой установки); «харнес чист» копии — PRODUCT_ALIASES
копии, а установщик кладёт туда имя проекта копии. Источник пишет в пакет
происхождение находок по имени копии («находка копии сети»), и имя
каждой копии сети для замка — не след. Имена копий сети — данные сети:
`серверы/<имя>.md` каталога EVOLUTION_DIR (ключ harness.conf).

Договор пробы: замок, судящий сборку с паспортом, где EVOLUTION_DIR указывает
на каталог сети, обязан отказать (код 1) и назвать имя копии, если в дереве
сборки стоит имя копии из `серверы/`. Сосед: сборка без имён копий — код 0;
имя самого источника (его `серверы/<PROJECT_NAME>.md`) судится как и прежде.

Дверь мутаций: PUBLIC_CLEAN_UNDER_TEST — путь к копии check-public-clean.py;
HARNESS_CLEAN_UNDER_TEST — к копии check-harness-clean.py.

    python3 scripts/test_public_clean_copy_names.py
Код возврата: 0 — все случаи прошли, 1 — есть упавший.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ЗДЕСЬ = Path(__file__).resolve().parent
ЗАМОК = Path(os.environ.get("PUBLIC_CLEAN_UNDER_TEST") or ЗДЕСЬ / "check-public-clean.py")
ЧИСТОТА = Path(os.environ.get("HARNESS_CLEAN_UNDER_TEST") or ЗДЕСЬ / "check-harness-clean.py")
КОПИЯ = "Соседка-Фабрика"
# Короткое имя копии внутри чужого слова («порог») — не след: замок судит слово целиком.
ВНУТРИ_СЛОВА = "оро"


def загрузить(путь: Path, имя: str):
    sys.path.insert(0, str(путь.resolve().parent / "lib"))
    спец = importlib.util.spec_from_file_location(имя, путь)
    модуль = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(модуль)
    return модуль


def git(*доводы):
    subprocess.run(["git", *доводы], check=True, capture_output=True)


def стенд(корень: Path) -> tuple:
    """Пакет, проект с картой, каталог сети с двумя копиями → паспорт."""
    пакет, проект, сеть = корень / "пакет", корень / "проект", корень / "сеть"
    for к in (пакет, проект, сеть / "серверы"):
        к.mkdir(parents=True)
    git("-C", str(пакет), "init", "-q", "-b", "master")
    (пакет / "файл").write_text("пакет стенда\n", encoding="utf-8")
    git("-C", str(пакет), "add", "-A")
    git("-C", str(пакет), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "пакет")
    git("-C", str(проект), "init", "-q", "-b", "master")
    (проект / "dev-map.yaml").write_text(
        "version: 1\nlines:\n  - id: harness\n    directions:\n"
        "      - id: hr.stend-proby\n        status: wip\n", encoding="utf-8")
    git("-C", str(проект), "add", "-A")
    git("-C", str(проект), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "п")
    for имя in ("Источник", КОПИЯ, ВНУТРИ_СЛОВА):
        (сеть / "серверы" / f"{имя}.md").write_text(f"сервер: {имя}\n", encoding="utf-8")
    хэш = subprocess.run(["git", "-C", str(пакет), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    паспорт = {"PROJECT_NAME": "Источник", "PROJECT_DIR": "/srv/источник", "AGENT_USER": "жилец",
               "EVOLUTION_DIR": str(сеть)}
    return паспорт, хэш


def main() -> int:
    for путь in (ЗАМОК, ЧИСТОТА):
        if not путь.is_file():
            print(f"ПРОБА СЛОМАНА: нет {путь}")
            return 1
    замок = загрузить(ЗАМОК, "public_clean_copy_names")
    чистота = загрузить(ЧИСТОТА, "harness_clean_copy_names")
    failed = total = 0

    def case(name, ok, got):
        nonlocal failed, total
        total += 1
        failed += not ok
        print(f"  {'ок  ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f": получили {got!r}"))

    with tempfile.TemporaryDirectory(prefix="o-copy-names-") as врем:
        корень = Path(врем)
        паспорт, хэш = стенд(корень)
        сборка = замок._подставная_сборка(корень, паспорт, хэш)
        файл = сборка / "harness" / "код.py"
        файл.write_text(f"# находка копии {КОПИЯ} 20.11: порог поднят\nПОРОГ = 15\n",
                        encoding="utf-8")
        git("-C", str(сборка), "add", "-A")
        git("-C", str(сборка), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "в")

        # Условие: «харнес чист» копии с её прозвищем на этом файле краснеет.
        у_копии = чистота.находки(["harness/код.py"], [КОПИЯ], сборка)
        case("условие: «харнес чист» копии находит своё имя в файле сборки", len(у_копии) == 1,
             у_копии)

        код, сказано = замок._вердикт(сборка, паспорт, корень)
        case("БОЛЬНОЙ СЛУЧАЙ копии сети: имя копии сети в сборке — замок отказывает и называет копию",
             код == 1 and any(КОПИЯ in с for с in сказано), (код, сказано[-3:]))

        файл.write_text("# находка копии сети 20.11: порог поднят\nПОРОГ = 15\n", encoding="utf-8")
        git("-C", str(сборка), "add", "-A")
        git("-C", str(сборка), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "ч")
        код, сказано = замок._вердикт(сборка, паспорт, корень)
        case(f"сосед: имени копий нет в дереве («{ВНУТРИ_СЛОВА}» лишь внутри «порог») — замок зелёный",
             код == 0 and any("следов установки нет" in с for с in сказано), (код, сказано[-3:]))

    print(f"ИТОГ: {total - failed}/{total}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
