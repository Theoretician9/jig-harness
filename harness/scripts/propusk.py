#!/usr/bin/env python3
"""Журнал известных ПРОПУСКОВ: что сломалось, а гейт промолчал.

Счётчик срабатываний («1171 гейт прошёл, 131 покраснел») — метрика работы, а
не качества. Единственный вопрос, по которому судят о стороже: сколько раз
что-то сломалось и НИ ОДИН механизм не покраснел. Замечание владельца М-3 к
ревизии 10.09.2026; оно же — сырьё для новых приборов: прибор рождается из
разбора причины, а не из вдохновения.

Запись:
    python3 scripts/propusk.py --что "..." --механизм "..." --почему "..." \
        [--дата 2026-09-10] [--чинит "коммит/файл"]
Чтение:
    python3 scripts/propusk.py --список [--дней 30]
Пустой журнал через две недели работы значит, что его не заполняют, — не то,
что пропусков нет.
"""
import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from konf import log_dir, конфиг  # noqa: E402 — один парсер конфига на все гейты

ЖУРНАЛ = "пропуски.jsonl"


def путь_журнала() -> str:
    return os.path.join(log_dir(), ЖУРНАЛ)


def записать(что: str, механизм: str, почему: str, дата: str, чинит: str) -> None:
    строка = {"дата": дата, "что": что, "механизм": механизм,
              "почему": почему, "чинит": чинит}
    with open(путь_журнала(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(строка, ensure_ascii=False) + "\n")
    print(f"пропуск записан: {дата} · {что[:60]}")


def список(дней: int) -> int:
    порог = (datetime.date.today() - datetime.timedelta(days=дней)).isoformat()
    строки = []
    try:
        with open(путь_журнала(), encoding="utf-8") as fh:
            for l in fh:
                try:
                    d = json.loads(l)
                except Exception:
                    continue
                if d.get("дата", "") >= порог:
                    строки.append(d)
    except OSError:
        pass
    print(f"=== известных пропусков за {дней} дн: {len(строки)} ===")
    for d in строки:
        print(f"  {d['дата']} · {d['что']}")
        print(f"      обязан был поймать: {d['механизм']}")
        print(f"      почему не поймал:   {d['почему']}")
        if d.get("чинит"):
            print(f"      чинится:            {d['чинит']}")
    return len(строки)


def самотест() -> int:
    import tempfile
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        # Путь — через конфиг, как у боевого прогона: подмена globals() лямбдой
        # была следствием копипасты парсера (ревью 10.09.2026).
        конф_файл = os.path.join(tmp, "install.conf")
        with open(конф_файл, "w", encoding="utf-8") as fh:
            fh.write(f'LOG_DIR="{tmp}"\n')
        прежний = os.environ.get("HARNESS_INSTALL_CONF")
        try:
            os.environ["HARNESS_INSTALL_CONF"] = конф_файл
            записать("проба", "механизм", "причина", "2026-09-10", "")
            n = список(3650)
            ok = ok and n == 1
            print(f"  {'ок   ' if n == 1 else 'ПЛОХО'} запись видна в списке")
            # БОЛЬНОЙ СЛУЧАЙ: старая запись не должна попадать в окно «за 7 дней»
            записать("старое", "механизм", "причина", "2020-01-01", "")
            свежих = список(7)
            ok = ok and свежих == 1
            print(f"  {'ок   ' if свежих == 1 else 'ПЛОХО'} БОЛЬНОЙ СЛУЧАЙ: старая запись не считается свежей")
        finally:
            if прежний is None:
                os.environ.pop("HARNESS_INSTALL_CONF", None)
            else:
                os.environ["HARNESS_INSTALL_CONF"] = прежний
    print("SELFTEST: зелёный (2 пути, второй — больной случай)" if ok else "SELFTEST: КРАСНЫЙ")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("--что"); p.add_argument("--механизм"); p.add_argument("--почему")
    p.add_argument("--чинит", default="")
    p.add_argument("--дата", default=datetime.date.today().isoformat())
    p.add_argument("--список", action="store_true")
    p.add_argument("--дней", type=int, default=30)
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    if a.selftest:
        return самотест()
    if a.список:
        список(a.дней)
        return 0
    if not (a.что and a.механизм and a.почему):
        p.error("нужны --что, --механизм и --почему (или --список)")
    записать(a.что, a.механизм, a.почему, a.дата, a.чинит)
    return 0


if __name__ == "__main__":
    sys.exit(main())
