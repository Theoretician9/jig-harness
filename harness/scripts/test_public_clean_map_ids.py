#!/usr/bin/env python3
"""Замок сборки: в публичном дереве нет id карточек ЭТОГО проекта.

Слово владельца 13.11.2026: «это надо решить системно, нельзя чтобы улетало в
обновления наши карточки». Копия bkb не встала на сборку b390c75: пакетный
карантин ворот вёз 30 наших карточек, и суд карантина на копии назвал каждую
«карточки нет в карте» — 31 красный. Замок живёт в гейте чистоты сборки
(check-public-clean.py, замок 6 publish.sh) — тот же класс «сборка не несёт
ничего о нашей установке», только образец берётся из карты проекта.

Договор:
  * id карточек = все `id` с точкой из <проект>/dev-map.yaml (архив карты
    docs/dev-map-archive.yaml не судится — отдельная карточка), кроме тех, что несёт заготовка карты
    в самой сборке (harness/dev-map.yaml: hr.accept, pr.first…);
  * id в любой строке дерева (комментарий тоже) → код 1, строка
    «<файл>:<строка>: карточка=<id>»;
  * id — целым словом: более длинный id, которого в карте нет, — не находка;
  * карты проекта нет → код 2 «судить нечем», а не «чисто».

Стенд — свой mkdtemp: подставная сборка с меткой, подставной проект с картой и
архивом, паспорт стенда. Живые /etc/harness, проект и пакет не читаются.
Итог: «КАРТОЧКИ В СБОРКЕ: путей N, неудач M», код 0/1.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATE = HERE / "check-public-clean.py"
LABEL = ".публичная-сборка"

MAP_ID = "hr.stend-kartochka-proekta"
ARCHIVE_ID = "hr.stend-zakrytaya-kartochka"
TEMPLATE_ID = "hr.accept"

paths = failures = 0


def check(ok: bool, text: str, detail: str = "") -> None:
    global paths, failures
    paths += 1
    failures += 0 if ok else 1
    print(("  ок    " if ok else "  ПЛОХО ") + text + ("" if ok else f": {detail}"))


def stand() -> tuple[Path, Path, Path, Path]:
    root = Path(tempfile.mkdtemp(prefix="proba-map-ids-in-build-"))
    build = root / "сборка"
    (build / "harness" / "scripts").mkdir(parents=True)
    (build / LABEL).write_text("стенд пробы\nпакет: 0000000\n", encoding="utf-8")
    (build / "README.md").write_text("Ставится куда угодно.\n", encoding="utf-8")
    (build / "harness" / "dev-map.yaml").write_text(
        "version: 1\nlines:\n  - id: harness\n    directions:\n"
        f"      - id: {TEMPLATE_ID}\n        status: plan\n", encoding="utf-8")
    (build / "harness" / "scripts" / "code.py").write_text(
        f"# первая задача установки — {TEMPLATE_ID}\nLIMIT = 15\n", encoding="utf-8")

    project = root / "проект"
    (project / "docs").mkdir(parents=True)
    (project / "dev-map.yaml").write_text(
        "version: 1\nlines:\n  - id: harness\n    directions:\n"
        f"      - id: {MAP_ID}\n        status: wip\n"
        f"      - id: {TEMPLATE_ID}\n        status: done\n", encoding="utf-8")
    (project / "docs" / "dev-map-archive.yaml").write_text(
        "version: 1\nlines:\n  - id: harness\n    directions:\n"
        f"      - id: {ARCHIVE_ID}\n        status: done\n", encoding="utf-8")

    package = root / "пакет"          # не git: свежесть сборки не судится
    package.mkdir()
    passport = root / "паспорт.conf"
    passport.write_text('PROJECT_NAME="Подставной"\nPROJECT_DIR="/srv/podstavnoj"\n'
                        'AGENT_USER="podstavnoj"\n', encoding="utf-8")
    return root, build, project, passport


def gate(build: Path, project: Path, passport: Path, package: Path) -> tuple[int, str]:
    env = dict(os.environ, HARNESS_INSTALL_CONF=str(passport), HARNESS_CONF=str(passport))
    done = subprocess.run(
        [sys.executable, str(GATE), str(build), "--паспорт", str(passport),
         "--конфиг", str(passport), "--пакет", str(package), "--проект", str(project)],
        capture_output=True, text=True, timeout=120, env=env)
    return done.returncode, done.stdout + done.stderr


def write(build: Path, rel: str, text: str) -> None:
    (build / rel).write_text(text, encoding="utf-8")


def main() -> int:
    if not GATE.is_file():
        print(f"  ПЛОХО нет гейта {GATE}")
        print("КАРТОЧКИ В СБОРКЕ: путей 1, неудач 1")
        return 1
    root, build, project, passport = stand()
    package = root / "пакет"
    try:
        code, out = gate(build, project, passport, package)
        check(code == 0, "чистая сборка (только id заготовки карты) — код 0",
              f"код {code}: {out.strip()[-300:]}")

        # Больной случай: id из карты проекта в значении данных.
        write(build, "harness/quarantine.yaml", f"карантин:\n  - карточка: {MAP_ID}\n")
        code, out = gate(build, project, passport, package)
        check(code == 1, "id карты в данных — отказ, код 1", f"код {code}: {out.strip()[-300:]}")
        check(f"harness/quarantine.yaml:2: карточка={MAP_ID}" in out,
              "отказ называет файл:строку и id", out.strip()[-300:])
        (build / "harness" / "quarantine.yaml").unlink()

        # Комментарий кода — тоже находка: исключения «для комментариев» нет.
        write(build, "harness/scripts/code.py", f"# спека {MAP_ID} §3\nLIMIT = 15\n")
        code, out = gate(build, project, passport, package)
        check(code == 1 and f"harness/scripts/code.py:1: карточка={MAP_ID}" in out,
              "id карты в комментарии кода — отказ с файл:строкой", f"код {code}: {out.strip()[-300:]}")

        # Архив карты замком не судится (вариант Б): судит только текущая карта.
        write(build, "harness/scripts/code.py", f"LIMIT = 15  # было в {ARCHIVE_ID}\n")
        code, out = gate(build, project, passport, package)
        check(code == 0 and "карточка=" not in out,
              "id только из архива карты — замок молчит, код 0", f"код {code}: {out.strip()[-300:]}")

        # Граница слова: длинный id, которого в карте нет, — не наш.
        write(build, "harness/scripts/code.py", f"# {MAP_ID}-dalshe и x{MAP_ID}\nLIMIT = 15\n")
        code, out = gate(build, project, passport, package)
        check(code == 0, "длиннее id карты (нет в карте) — не находка, код 0",
              f"код {code}: {out.strip()[-300:]}")

        # Пара к больному: строка убрана — сборка снова проходит.
        write(build, "harness/scripts/code.py", "LIMIT = 15\n")
        code, out = gate(build, project, passport, package)
        check(code == 0, "id убран — код 0", f"код {code}: {out.strip()[-300:]}")

        # Карты нет — «судить нечем», а не «чисто».
        (project / "dev-map.yaml").rename(project / "dev-map.yaml.нет")
        write(build, "harness/scripts/code.py", f"# {MAP_ID}\n")
        code, out = gate(build, project, passport, package)
        check(code == 2, "карты проекта нет — код 2 (судить нечем)", f"код {code}: {out.strip()[-300:]}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"КАРТОЧКИ В СБОРКЕ: путей {paths}, неудач {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
