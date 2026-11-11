#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проба гейта прополки без потерь: строка приказов не исчезает молча.

Откуда взята. Спека hr.standards-and-policy §5.1, §5.3: файлы «без потерь»
(память/ORDERS.md, память/owner-orders-*-verbatim.md) модель дописывает и
пропалывает, но каждая ушедшая строка обязана лечь в память/archive/. Гейт
сверяет эти файлы с HEAD~ и краснеет на первой потерянной строке. Без него
прополка ORDERS (передача 09.11, 21:32) теряла бы приказы владельца, и никто
бы этого не увидел: в рабочем дереве потерю не видно.

Что судится (каждый случай — свой стенд-репозиторий, git init во временном
каталоге sap-ch11-*):
  - строка удалена без переноса — rc 1 и эта строка в выводе; не закоммичено
    (pre-commit) и закоммичено (ворота после коммита) — одинаково;
  - строка перенесена в память/archive/ — rc 0;
  - сокращение строки — это удаление прежнего текста: без архива rc 1;
  - переименование файла, удаление файла — строки ушли, rc 1;
  - перенос не в архив (в другой файл памяти) — не перенос, rc 1;
  - дописать, переставить строки, убрать пустую строку, править файл вне
    «без потерь» — rc 0;
  - список файлов гейт берёт из memory-layers.yaml (шаблон owner-orders-*
    разбирается; другой файл слоёв меняет решение).

Двери:
  MEMORY_WEEDING_PY   — путь гейта (мутационная проба подставляет копию);
  PRECOMMIT_UNDER_TEST — хук pre-commit, где гейт обязан стоять строкой «гейт»;
  MEMORY_WEEDING_ROOT — корень репозитория, который судит гейт;
  MEMORY_LAYERS       — файл слоёв.

Интерфейс гейта: `python3 scripts/check-memory-weeding.py`, база — HEAD~,
сверяется с рабочим деревом; 0 — потерь нет, 1 — потеря (строка в выводе).

Прогон: python3 scripts/test_memory_weeding.py
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = Path(os.environ.get("MEMORY_WEEDING_PY") or ROOT / "scripts" / "check-memory-weeding.py")
PRECOMMIT = Path(os.environ.get("PRECOMMIT_UNDER_TEST") or Path(__file__).resolve().parent / "pre-commit-gate.sh")

STAND = Path(tempfile.mkdtemp(prefix="sap-ch11-"))
atexit.register(shutil.rmtree, STAND, ignore_errors=True)
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="проба", GIT_AUTHOR_EMAIL="p@stand.invalid",
               GIT_COMMITTER_NAME="проба", GIT_COMMITTER_EMAIL="p@stand.invalid",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")

LAYERS_TEXT = """\
неизменяемое: [harness/self/core.json]
человека: [harness/self/instance.yaml]
без_потерь: [память/ORDERS.md, "память/owner-orders-*-verbatim.md"]
архив_прополки: память/archive/
обязательные_поля_человека: [копия]
"""
# Строки — живого вида: приказ с датой и источником. Длинные и разные, чтобы
# найденная в выводе строка не совпала с соседкой по подстроке.
ORDERS = [
    "# Приказы владельца",
    "",
    "- 01.11 проба-первая: все вопросы о Да приходят кнопками в канал",
    "- 02.11 проба-вторая: личность подаётся системной инструкцией каждой модели",
    "- 03.11 проба-третья: прополка памяти идёт переносом в архив, без потерь",
    "",
    "- 04.11 проба-четвёртая: выкат только через ворота, руками нельзя",
]
LINE2, LINE3, LINE4, LINE5 = ORDERS[2], ORDERS[3], ORDERS[4], ORDERS[6]
VERBATIM = ["# Слова владельца 09.11 дословно", "",
            "1. проба-дословно: «сделай так, чтобы модель не могла не узнать»"]
NOTE = ["# заметка смены", "- проба-заметка: эта строка уходит свободно"]

cases = 0
failures = 0


def check(name: str, ok: bool, details: str = "") -> None:
    """Счёт живёт здесь: итоговая строка — доказательство, врать ей нельзя."""
    global cases, failures
    cases += 1
    if ok:
        print(f"ок    · {name}")
    else:
        failures += 1
        print(f"ПЛОХО · {name} — {details[:400]}")


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=GIT_ENV)


def write(repo: Path, rel: str, lines: list[str]) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_repo(name: str) -> Path:
    """Стенд: два коммита, чтобы у HEAD был предок и у HEAD~ — тоже содержимое."""
    repo = STAND / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    write(repo, "harness/config/memory-layers.yaml", LAYERS_TEXT.splitlines())
    write(repo, "память/ORDERS.md", ORDERS)
    write(repo, "память/owner-orders-09-11-verbatim.md", VERBATIM)
    write(repo, "память/x.md", NOTE)
    write(repo, "память/archive/README.md", ["# архив прополки"])
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "стенд: начало")
    write(repo, "STATE.md", ["# состояние стенда"])
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "стенд: второй коммит")
    return repo


def run_gate(repo: Path, layers: Path | None = None) -> tuple[int, str]:
    env = dict(os.environ, MEMORY_WEEDING_ROOT=str(repo),
               MEMORY_LAYERS=str(layers or repo / "harness/config/memory-layers.yaml"))
    env.pop("MEMORY_WEEDING_PY", None)
    done = subprocess.run([sys.executable, str(GATE)], capture_output=True, text=True,
                          timeout=60, cwd=str(repo), env=env)
    return done.returncode, done.stdout + done.stderr


def scenario(name: str, change, expect_rc: int, must_show: list[str] = (),
             commit: bool = False, layers_text: str | None = None) -> None:
    """Свой репозиторий на случай: прошлый случай не подкрашивает следующий."""
    repo = make_repo(f"r{cases:03d}")
    change(repo)
    git(repo, "add", "-A")
    if commit:
        git(repo, "commit", "-q", "-m", "стенд: прополка")
    layers = None
    if layers_text is not None:
        layers = STAND / f"layers-{cases:03d}.yaml"
        layers.write_text(layers_text, encoding="utf-8")
    rc, output = run_gate(repo, layers)
    missing = [line for line in must_show if line.strip() not in output]
    stage = "закоммичено" if commit else "в индексе"
    check(f"{name} ({stage}) — rc {expect_rc}" + (", строка в выводе" if must_show else ""),
          rc == expect_rc and not missing,
          f"rc={rc}, нет в выводе: {missing}; вывод: {output.strip()[-300:]!r}")


def without(lines: list[str], *drop: str) -> list[str]:
    return [line for line in lines if line not in drop]


def case_removed_without_archive() -> None:
    for commit in (False, True):
        scenario("строка ORDERS удалена без переноса",
                 lambda r: write(r, "память/ORDERS.md", without(ORDERS, LINE3)), 1, [LINE3],
                 commit=commit)


def case_moved_to_archive() -> None:
    def move(repo: Path) -> None:
        write(repo, "память/ORDERS.md", without(ORDERS, LINE3))
        write(repo, "память/archive/ORDERS-2026-11.md", ["# прополка 10.11", LINE3])
    for commit in (False, True):
        scenario("строка ORDERS перенесена в память/archive/", move, 0, commit=commit)


def case_partial_archive() -> None:
    def half(repo: Path) -> None:
        write(repo, "память/ORDERS.md", without(ORDERS, LINE2, LINE4))
        write(repo, "память/archive/ORDERS-2026-11.md", [LINE2])
    scenario("две строки ушли, одна в архиве — красный по второй", half, 1, [LINE4])


def case_shortened() -> None:
    short = "- 02.11 проба-вторая: личность — системной инструкцией"
    scenario("строка сокращена без архива (прежний текст ушёл)",
             lambda r: write(r, "память/ORDERS.md",
                             [short if line == LINE3 else line for line in ORDERS]), 1, [LINE3])

    def archived(repo: Path) -> None:
        write(repo, "память/ORDERS.md", [short if line == LINE3 else line for line in ORDERS])
        write(repo, "память/archive/ORDERS-2026-11.md", [LINE3])
    scenario("строка сокращена, прежний текст в архиве", archived, 0)


def case_renamed_and_deleted() -> None:
    def rename(repo: Path) -> None:
        (repo / "память/ORDERS.md").rename(repo / "память/ORDERS-old.md")
    scenario("ORDERS.md переименован — строки ушли из «без потерь»", rename, 1, [LINE2])

    def drop_verbatim(repo: Path) -> None:
        (repo / "память/owner-orders-09-11-verbatim.md").unlink()
    scenario("дословный файл owner-orders-*-verbatim удалён (шаблон разобран)", drop_verbatim, 1,
             [VERBATIM[2]])

    def moved_elsewhere(repo: Path) -> None:
        write(repo, "память/ORDERS.md", without(ORDERS, LINE5))
        write(repo, "память/x.md", NOTE + [LINE5])
    scenario("строка перенесена не в архив, а в память/x.md", moved_elsewhere, 1, [LINE5])


def case_harmless() -> None:
    scenario("без изменений", lambda r: None, 0)
    scenario("дописаны строки",
             lambda r: write(r, "память/ORDERS.md", ORDERS + ["- 05.11 проба-пятая: новое"]), 0)
    scenario("строки переставлены",
             lambda r: write(r, "память/ORDERS.md", [ORDERS[0], LINE5, LINE4, LINE3, LINE2]), 0)
    scenario("убрана пустая строка",
             lambda r: write(r, "память/ORDERS.md", ORDERS[:1] + ORDERS[2:]), 0)
    scenario("строка ушла из файла вне «без потерь» (память/x.md)",
             lambda r: write(r, "память/x.md", NOTE[:1]), 0)


def case_layers_from_file() -> None:
    """Список «без потерь» берётся из файла слоёв, а не зашит."""
    only_verbatim = LAYERS_TEXT.replace(
        'без_потерь: [память/ORDERS.md, "память/owner-orders-*-verbatim.md"]',
        'без_потерь: ["память/owner-orders-*-verbatim.md"]')
    assert only_verbatim != LAYERS_TEXT
    scenario("файл слоёв без ORDERS.md — удаление строки ORDERS пропускается",
             lambda r: write(r, "память/ORDERS.md", without(ORDERS, LINE3)), 0,
             layers_text=only_verbatim)
    scenario("тот же файл слоёв — дословный файл по-прежнему судится",
             lambda r: write(r, "память/owner-orders-09-11-verbatim.md", VERBATIM[:2]), 1,
             [VERBATIM[2]], layers_text=only_verbatim)


def case_hooked_in_precommit() -> None:
    """Гейт без вызова — датчик без провода: pre-commit зовёт его строкой «гейт»."""
    text = PRECOMMIT.read_text(encoding="utf-8") if PRECOMMIT.is_file() else ""
    wired = [line for line in text.splitlines()
             if re.match(r'^гейт\s+"[^"]+"\s.*\bcheck-memory-weeding\.py\b', line)]
    check("pre-commit-gate.sh зовёт check-memory-weeding.py гейтом (строка верхнего уровня)",
          len(wired) == 1, f"строк {len(wired)} в {PRECOMMIT}")


def main() -> int:
    print(f"гейт: {GATE}")
    if not GATE.is_file():
        check("файл гейта есть", False, f"нет {GATE} — гейт прополки ещё не написан")
    case_removed_without_archive()
    case_moved_to_archive()
    case_partial_archive()
    case_shortened()
    case_renamed_and_deleted()
    case_harmless()
    case_layers_from_file()
    case_hooked_in_precommit()
    print(f"итог: случаев {cases}, неудач {failures}")
    return 1 if failures or not cases else 0


if __name__ == "__main__":
    sys.exit(main())
