#!/usr/bin/env python3
"""Гейт: команда, напечатанная человеку или агенту, ведёт к существующему файлу.

Откуда взят: 12.09.2026, живой прогон установки в чистом контейнере
ubuntu:24.04. После переименования каталогов латиницей семь документов пакета —
включая финальный экран установщика и инструкцию владельцу — продолжали звать
`scripts/возможность.sh`, которого больше нет. Гейт внешних вызовов смотрит
cron и юниты; печатные подсказки не смотрел никто, а по ним действует ЧЕЛОВЕК
на чистой машине, у которого нет ни истории, ни сессии.

Судятся ДЕЙСТВУЮЩИЕ документы (то, что едет людям и агенту), а не архив:
передача смены и старые спеки хранят команды своего времени, и требовать от
них живых путей значит красить гейт вечно.

    python3 scripts/check-komandy-v-dokumentah.py
    python3 scripts/check-komandy-v-dokumentah.py --selftest
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from konf import корень_дерева  # noqa: E402

# Что считается ДЕЙСТВУЮЩИМ документом. Данные списком: архив сюда не входит.
МЕСТА = ["CLAUDE.md", "README.md", "ИНСТРУКЦИЯ-ВЛАДЕЛЬЦУ.md", "STATE.md",
         "razrabotka", "harness/skills", "harness/skills-src", "harness/CLAUDE.md",
         "docs/УСТРОЙСТВО-ХАРНЕСА.md"]

КОМАНДА = re.compile(
    r'(?:bash|python3|sh)\s+(?:\$\{?PROJECT_DIR\}?/|\./)?'
    r'((?:scripts|harness)/[A-Za-zА-Яа-я0-9_./-]+\.(?:sh|py))')


def документы(корень: Path):
    for место in МЕСТА:
        путь = корень / место
        if путь.is_file():
            yield путь
        elif путь.is_dir():
            yield from sorted(путь.rglob("*.md"))


def существует(корень: Path, цель: str) -> bool:
    """Путь ищется в ОБЕИХ раскладках: в пакете скрипты лежат под `harness/`, а
    хуки — в `harness/hooks/`, тогда как документы называют их установленными
    путями (`scripts/`, `scripts/hooks/`). Иначе гейт красит собственный пакет
    и его выключают в первый день."""
    кандидаты = [корень / цель, корень / "harness" / цель]
    if цель.startswith("scripts/hooks/"):
        кандидаты.append(корень / "harness" / "hooks" / Path(цель).name)
    return any(п.exists() for п in кандидаты)


def находки(корень: Path) -> list:
    беды = []
    for файл in документы(корень):
        текст = файл.read_text(encoding="utf-8", errors="replace")
        for номер, строка in enumerate(текст.splitlines(), 1):
            for цель in КОМАНДА.findall(строка):
                if существует(корень, цель):
                    continue
                беды.append(f"{файл.relative_to(корень)}:{номер}: команда зовёт "
                            f"несуществующий файл — {цель}")
    return беды


def selftest() -> int:
    import tempfile
    неудач = 0

    def проба(имя, ок):
        nonlocal неудач
        неудач += not ок
        print(f"  {'ok  ' if ок else 'FAIL'} {имя}")

    with tempfile.TemporaryDirectory() as каталог:
        корень = Path(каталог)
        (корень / "razrabotka").mkdir()
        # БОЛЬНОЙ СЛУЧАЙ: ровно тот текст, что уехал бы человеку 12.09.2026.
        (корень / "razrabotka" / "инструкция.md").write_text(
            "Поставить набор:\n\n    bash scripts/возможность.sh список\n",
            encoding="utf-8")
        проба("несуществующая команда найдена", len(находки(корень)) == 1)

        (корень / "scripts").mkdir()
        (корень / "scripts" / "vozmozhnost.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (корень / "razrabotka" / "инструкция.md").write_text(
            "Поставить набор:\n\n    bash scripts/vozmozhnost.sh список\n",
            encoding="utf-8")
        проба("существующая команда не находка", находки(корень) == [])

        # ЛОЖНОЕ СРАБАТЫВАНИЕ: архив хранит команды своего времени — его не судим.
        (корень / "docs").mkdir()
        (корень / "docs" / "handover").mkdir()
        (корень / "docs" / "handover" / "старое.md").write_text(
            "bash scripts/умерший.sh\n", encoding="utf-8")
        проба("архив передачи смены не судится", находки(корень) == [])

    print(f"самотест check-komandy-v-dokumentah: неудач {неудач}")
    return 1 if неудач else 0


def главная() -> int:
    if "--selftest" in sys.argv[1:]:
        return selftest()
    беды = находки(корень_дерева(__file__))
    for беда in беды:
        print(f"  ✗ {беда}")
    print(f"[команды в документах] находок {len(беды)}")
    return 1 if беды else 0


if __name__ == "__main__":
    sys.exit(главная())
