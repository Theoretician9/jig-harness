#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Каждый файл кода харнеса принадлежит хотя бы одной области ревизии.

Откуда взят. Круг ревизии ходит ПО ОБЛАСТЯМ (harness/config/ревизия.yaml), и
файл, не попавший ни в одну маску, не читает никто и никогда — молча. Замер
28.09.2026 на живом дереве: из 199 файлов кода харнеса вне всех областей
оказались 6, среди них генератор скилов `harness/BUILD-SKILLS.py` и помощник
демона гигиены. Обратная беда (один файл в нескольких областях) уже закрыта в
`revision-batches.файлы_области`: файл достаётся области, которая идёт раньше в
порядке круга.

Население — то, что знает git: ls-files с `core.quotepath=false`, иначе
кириллическое имя приезжает в октальных escape-последовательностях и не
сходится ни с одной маской. Отказ git — это НЕ «нарушений нет»: код 2.

Исключения объявляются в том же конфиге ключом «вне_обзора» — список записей
{путь, причина}; запись без причины не действует.

    python3 scripts/check-revision-coverage.py
    python3 scripts/check-revision-coverage.py --selftest
Код возврата: 0 — все покрыты, 1 — есть непокрытые, 2 — судить не удалось.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
РАСШИРЕНИЯ = (".py", ".sh")
КАТАЛОГИ = ("scripts/", "harness/")


def конфиг(путь: Path) -> dict | None:
    """Данные ревизии. None — прочитать не удалось (это не «нарушений нет»)."""
    try:
        import yaml
        return yaml.safe_load(путь.read_text(encoding="utf-8")) or {}
    except (OSError, ImportError):
        return None
    except Exception:                       # noqa: BLE001 — битый YAML
        return None


def население(корень: Path) -> list[str] | None:
    """Файлы кода харнеса по git. None — git молчит, судить нечем."""
    try:
        готово = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-C", str(корень), "ls-files"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if готово.returncode != 0:
        return None
    return [ф for ф in готово.stdout.splitlines()
            if ф.endswith(РАСШИРЕНИЯ) and ф.startswith(КАТАЛОГИ)]


def покрытые(данные: dict, корень: Path) -> set:
    """Файлы, попавшие хотя бы в одну маску хотя бы одной области."""
    итог = set()
    for область in (данные.get("области") or []):
        for маска in (область.get("пути") or []):
            for путь in корень.glob(маска):
                if путь.is_file():
                    итог.add(str(путь.relative_to(корень)))
    return итог


def объявленные(данные: dict) -> set:
    """Пути, объявленные вне обзора С ПРИЧИНОЙ. Без причины — не считается."""
    итог = set()
    for запись in (данные.get("вне_обзора") or []):
        if not isinstance(запись, dict):
            continue
        путь = str(запись.get("путь") or "").strip()
        причина = str(запись.get("причина") or "").strip()
        if путь and причина:
            итог.add(путь)
    return итог


def судить(корень: Path, путь_конфига: Path) -> tuple[int, list[str]]:
    """(код возврата, строки отчёта)."""
    данные = конфиг(путь_конфига)
    if данные is None:
        return 2, [f"[области ревизии] конфиг не прочитан: {путь_конфига}"]
    люди = население(корень)
    if люди is None:
        return 2, ["[области ревизии] git молчит о составе — судить нечем, "
                   "и это не «нарушений нет»"]
    вне = sorted(set(люди) - покрытые(данные, корень) - объявленные(данные))
    if вне:
        строки = [f"[области ревизии] вне всех областей: {len(вне)} из {len(люди)}"]
        строки += [f"    {ф} — его не прочитает ни один круг ревизии" for ф in вне]
        строки.append("    Починка: расширить маску области в "
                      "harness/config/ревизия.yaml либо объявить путь в "
                      "«вне_обзора» с причиной.")
        return 1, строки
    return 0, [f"[области ревизии] чисто: все {len(люди)} файлов кода харнеса "
               f"принадлежат областям"]


def селфтест() -> int:
    import tempfile
    ok = True
    путей = 0

    def проба(условие: bool, имя: str) -> None:
        nonlocal ok, путей
        путей += 1
        print(f"  {'ок   ' if условие else 'ПЛОХО'} {имя}")
        if not условие:
            ok = False

    with tempfile.TemporaryDirectory() as врем:
        двор = Path(врем)
        (двор / "scripts").mkdir()
        (двор / "harness").mkdir()
        (двор / "scripts" / "a.py").write_text("# один\n", encoding="utf-8")
        (двор / "harness" / "b.sh").write_text("# два\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(двор), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(двор), "add", "-A"], check=True)

        конф = двор / "ревизия.yaml"
        # БОЛЬНОЙ СЛУЧАЙ: файл не попал ни в одну маску — его не читает никто.
        конф.write_text("области:\n  - имя: одна\n    пути: [\"scripts/*.py\"]\n",
                        encoding="utf-8")
        код, строки = судить(двор, конф)
        проба(код == 1 and any("harness/b.sh" in с for с in строки),
              "БОЛЬНОЙ СЛУЧАЙ: файл вне всех областей назван по имени")

        конф.write_text("области:\n  - имя: одна\n    пути: [\"scripts/*.py\"]\n"
                        "  - имя: две\n    пути: [\"harness/*.sh\"]\n",
                        encoding="utf-8")
        проба(судить(двор, конф)[0] == 0, "обе маски вместе покрывают всё — чисто")

        конф.write_text("области:\n  - имя: одна\n    пути: [\"scripts/*.py\"]\n"
                        "вне_обзора:\n  - путь: harness/b.sh\n"
                        "    причина: «чужой код, ревизии не подлежит»\n",
                        encoding="utf-8")
        проба(судить(двор, конф)[0] == 0,
              "объявленный вне обзора С ПРИЧИНОЙ не считается непокрытым")

        конф.write_text("области:\n  - имя: одна\n    пути: [\"scripts/*.py\"]\n"
                        "вне_обзора:\n  - путь: harness/b.sh\n", encoding="utf-8")
        проба(судить(двор, конф)[0] == 1,
              "БОЛЬНОЙ СЛУЧАЙ: объявление БЕЗ причины не освобождает файл")

        проба(судить(двор, двор / "нет-такого.yaml")[0] == 2,
              "конфига нет — код 2 «судить нечем», а не зелёный")

        пусто = Path(tempfile.mkdtemp())
        проба(судить(пусто, конф)[0] == 2,
              "БОЛЬНОЙ СЛУЧАЙ: git молчит (не репозиторий) — код 2, а не чистота")

    print(f"SELFTEST: {'зелёный' if ok else 'КРАСНЫЙ'} ({путей} путей, "
          "первым — больной случай «файл вне всех областей»)")
    return 0 if ok else 1


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        return селфтест()
    путь = Path(os.environ.get("REVISION_CONF")
                or КОРЕНЬ / "harness" / "config" / "ревизия.yaml")
    код, строки = судить(КОРЕНЬ, путь)
    for с in строки:
        print(с)
    return код


if __name__ == "__main__":
    sys.exit(main())
