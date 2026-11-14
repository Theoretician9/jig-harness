#!/usr/bin/env python3
"""Замок коммита: ДОБАВЛЕННАЯ строка файла сборки не несёт id карточки карты.

Замок сборки (check-public-clean.py) ловит id карточек только при публикации —
поздно: чужой коммит вписал id в пробу ПОСЛЕ вычистки, и релиз 0.11.1 снова
встал на замке (14.11.2026). Гейт pre-commit ставит стоп росту: судит строки
«+» индекса (git diff --cached -U0), а не файл целиком — уже лежащие строки
не блокируют чужие коммиты.

Договор (check-commit-map-ids.py):
  * добавленная строка с id текущей карты в файле, который едет в сборку →
    код 1, «<файл>:<строка>: карточка=<id>»;
  * id в уже лежащей строке, коммит трогает другую строку → код 0;
  * файл вне сборки (.tasks/, dev-map.yaml) → код 0;
  * файл, который сборка подменяет заготовкой (заменить_шаблоном) → код 0;
  * id заготовки карты (hr.accept) — не карточка установки → код 0;
  * .claude/agents/ едет в сборку (harness/agents/) → код 1.

Стенд — свой mkdtemp: git-проект с картой и данными сборки, подставной пакет
с заготовкой карты, паспорт стенда. Живые карта, пакет и /etc не читаются.
Дверь для мутаций: HARNESS_COMMIT_MAP_IDS_SRC — каталог гейта под пробой.
Итог: «КАРТОЧКИ В КОММИТЕ: путей N, неудач M», код 0/1.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ЗДЕСЬ = Path(__file__).resolve().parent
ГЕЙТ = Path(os.environ.get("HARNESS_COMMIT_MAP_IDS_SRC") or ЗДЕСЬ) / "check-commit-map-ids.py"

КАРТОЧКА = "hr.stend-kartochka-proekta"
ЗАГОТОВКА = "hr.accept"

путей = неудач = 0


def случай(ok: bool, текст: str, подробно: str = "") -> None:
    global путей, неудач
    путей += 1
    неудач += 0 if ok else 1
    print(("  ок    " if ok else "  ПЛОХО ") + текст + ("" if ok else f": {подробно}"))


def git(проект: Path, *доводы: str) -> None:
    subprocess.run(["git", "-C", str(проект), "-c", "user.name=стенд", "-c", "user.email=s@s",
                    "-c", "core.hooksPath=/dev/null", *доводы],
                   check=True, capture_output=True, text=True)


def писать(проект: Path, путь: str, текст: str) -> None:
    файл = проект / путь
    файл.parent.mkdir(parents=True, exist_ok=True)
    файл.write_text(текст, encoding="utf-8")


def стенд() -> tuple[Path, Path, Path, dict]:
    корень = Path(tempfile.mkdtemp(prefix="proba-commit-map-ids-"))
    проект, пакет = корень / "проект", корень / "пакет"
    писать(пакет, "harness/dev-map.yaml",
           f"version: 1\nlines:\n  - id: harness\n    directions:\n      - id: {ЗАГОТОВКА}\n")
    писать(проект, "dev-map.yaml",
           "version: 1\nlines:\n  - id: harness\n    directions:\n"
           f"      - id: {КАРТОЧКА}\n        status: wip\n"
           f"      - id: {ЗАГОТОВКА}\n        status: done\n")
    писать(проект, "harness/config/public-build.yaml",
           "исключить: []\nзаменить_шаблоном:\n"
           "  harness/config/live-check-hooks.yaml: harness/шаблоны-задач/x.yaml\n")
    писать(проект, "scripts/code.py", f"# старая ссылка {КАРТОЧКА}\nLIMIT = 15\n")
    git(проект, "init", "-q")
    git(проект, "add", "-A")
    git(проект, "commit", "-qm", "стенд")
    паспорт = корень / "паспорт.conf"
    паспорт.write_text('PROJECT_NAME="Подставной"\nAGENT_USER="podstavnoj"\n', encoding="utf-8")
    среда = dict(os.environ, HARNESS_INSTALL_CONF=str(паспорт), HARNESS_CONF=str(паспорт))
    return корень, проект, пакет, среда


def суд(проект: Path, пакет: Path, среда: dict) -> tuple[int, str]:
    итог = subprocess.run([sys.executable, str(ГЕЙТ), "--пакет", str(пакет)], cwd=проект,
                          capture_output=True, text=True, timeout=120, env=среда)
    return итог.returncode, (итог.stdout + итог.stderr).strip()[-400:]


def с_индексом(проект: Path, пакет: Path, среда: dict, правки: dict) -> tuple[int, str]:
    """Положить правки в индекс, судить, снять их из индекса и с диска."""
    for путь, текст in правки.items():
        писать(проект, путь, текст)
    git(проект, "add", "-A")
    try:
        return суд(проект, пакет, среда)
    finally:
        git(проект, "reset", "-q", "--hard", "HEAD")
        git(проект, "clean", "-qfd")


def main() -> int:
    if not ГЕЙТ.is_file():
        print(f"  ПЛОХО нет гейта {ГЕЙТ}")
        print("КАРТОЧКИ В КОММИТЕ: путей 1, неудач 1")
        return 1
    корень, проект, пакет, среда = стенд()
    try:
        код, вывод = с_индексом(проект, пакет, среда,
                                {"scripts/code.py": f"# старая ссылка {КАРТОЧКА}\nLIMIT = 15\n"
                                                    f"LIMIT_2 = 1\n# новая {КАРТОЧКА}\n"})
        случай(код == 1, "добавленная строка с id в файле сборки — отказ, код 1", f"код {код}: {вывод}")
        случай(f"scripts/code.py:4: карточка={КАРТОЧКА}" in вывод,
               "отказ называет файл:строку и id", вывод)
        случай("scripts/code.py:1:" not in вывод, "уже лежащая строка не названа", вывод)

        код, вывод = с_индексом(проект, пакет, среда,
                                {"scripts/code.py": f"# старая ссылка {КАРТОЧКА}\nLIMIT = 16\n"})
        случай(код == 0, "id в уже лежащей строке, правка другой — код 0", f"код {код}: {вывод}")

        код, вывод = с_индексом(проект, пакет, среда, {"scripts/code.py": "LIMIT = 15\n"})
        случай(код == 0, "строка с id удалена — вычистка, а не находка, код 0", f"код {код}: {вывод}")

        код, вывод = с_индексом(проект, пакет, среда, {
            ".tasks/hr.x/журнал.md": f"* {КАРТОЧКА}\n",
            "dev-map.yaml": (проект / "dev-map.yaml").read_text(encoding="utf-8")
            + f"      # {КАРТОЧКА}\n"})
        случай(код == 0, "файлы вне сборки (.tasks/, dev-map.yaml) — код 0", f"код {код}: {вывод}")

        код, вывод = с_индексом(проект, пакет, среда, {
            "harness/config/live-check-hooks.yaml": f"хуки: {{}}  # {КАРТОЧКА}\n"})
        случай(код == 0, "файл, подменяемый заготовкой в сборке, — код 0", f"код {код}: {вывод}")

        код, вывод = с_индексом(проект, пакет, среда,
                                {"scripts/code.py": f"# старая ссылка {КАРТОЧКА}\nLIMIT = 15\n"
                                                    f"# первая задача — {ЗАГОТОВКА}\n"})
        случай(код == 0, "id заготовки карты — не карточка установки, код 0", f"код {код}: {вывод}")

        код, вывод = с_индексом(проект, пакет, среда,
                                {".claude/agents/x.md": f"Часть: {КАРТОЧКА}#1\n"})
        случай(код == 1 and f".claude/agents/x.md:1: карточка={КАРТОЧКА}" in вывод,
               ".claude/agents/ едет в сборку — отказ", f"код {код}: {вывод}")
    finally:
        shutil.rmtree(корень, ignore_errors=True)
    print(f"КАРТОЧКИ В КОММИТЕ: путей {путей}, неудач {неудач}")
    return 1 if неудач or not путей else 0


if __name__ == "__main__":
    sys.exit(main())
