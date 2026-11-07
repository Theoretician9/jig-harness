#!/usr/bin/env python3
"""Проба: соседи живой проверки пишут общее состояние под замком.

Откуда взято. Карточка hr.zamka-u-sosedej-zhivoj-proverki-net (ревью 17.10,
F-sec-04): live-check-daemon.py, live-check-sick.py и live-check-skill.py
делают в записать() «прочитать yaml — поправить — write_text» без замка, и два
прогона рядом молча теряют записи (замер: 30 из 60).

Что судится, для каждого из трёх писателей:

1. В тексте есть flock — дешёвый признак, что замок вообще поставлен.
2. Два процесса одновременно зовут записать() по 30 разных ключей во ВРЕМЕННОЕ
   состояние по образцу боевого; в итоге обязаны лежать все 60 и все прежние ключи,
   а файл — разбираться как yaml. Признак (1) без (2) не спасает: замок можно
   взять не вокруг того цикла.

Проба создаёт свой двор (временный каталог); боевое состояние только читается
как образец. Дверь для мутации: HARNESS_LIVE_CHECK_FLOCK_SCRIPTS — каталог, где
лежат испытуемые (по умолчанию — каталог этой пробы).

Прогон: python3 scripts/test_live_check_flock.py
Код возврата: 0 — все случаи зелёные, 1 — хоть один плохой.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

import yaml

ЗДЕСЬ = pathlib.Path(__file__).resolve().parent
ИСПЫТУЕМЫЕ = pathlib.Path(os.environ.get("HARNESS_LIVE_CHECK_FLOCK_SCRIPTS") or ЗДЕСЬ)
ПИСАТЕЛИ = ("live-check-daemon.py", "live-check-sick.py", "live-check-skill.py")
ОБРАЗЕЦ = ЗДЕСЬ.parent / "harness" / "config" / "live-check-state.yaml"
if not ОБРАЗЕЦ.exists():
    # в пакете проба лежит в harness/scripts, а данные — в harness/config
    ОБРАЗЕЦ = ЗДЕСЬ.parent / "config" / "live-check-state.yaml"
ПО_СКОЛЬКО = 30
ПРЕЖНИХ = 5
ПРЕФИКС = "проба-замка-"

# Писатель грузит модуль по пути (имя с дефисом не импортируется), ждёт общий
# старт и пишет свои 30 ключей; старт общий, чтобы окна чтение-запись перекрылись.
ПИСАТЕЛЬ = r"""
import importlib.util, os, pathlib, sys, time
путь, метка, старт, сколько, префикс = sys.argv[1:6]
спек = importlib.util.spec_from_file_location("испытуемый", путь)
модуль = importlib.util.module_from_spec(спек)
спек.loader.exec_module(модуль)
while not pathlib.Path(старт).exists():
    time.sleep(0.001)
for i in range(int(сколько)):
    модуль.записать({f"{префикс}{метка}-{i}": {"когда": "проба", "кто": метка}})
"""

хорошо = 0
плохо = 0


def случай(имя: str, условие: bool, пояснение: str = "") -> None:
    global хорошо, плохо
    if условие:
        хорошо += 1
        print(f"  ок    {имя}")
    else:
        плохо += 1
        print(f"  ПЛОХО {имя}{(': ' + пояснение) if пояснение else ''}")


def гонка(писатель: pathlib.Path) -> None:
    двор = pathlib.Path(tempfile.mkdtemp(prefix="проба-замка-"))
    try:
        состояние = двор / "live-check-state.yaml"
        # Боевое состояние в 300 КБ делает 60 перезаписей минутными; несколько
        # его записей и своя метка дают тот же формат и проверку «прежние целы»
        # даже на пустой заготовке пакета.
        образец = yaml.safe_load(ОБРАЗЕЦ.read_text(encoding="utf-8")) or {}
        прежнее = dict(list((образец.get("проверено") or {}).items())[:ПРЕЖНИХ])
        прежнее[f"{ПРЕФИКС}прежняя"] = {"когда": "до гонки"}
        образец["проверено"] = прежнее
        состояние.write_text(yaml.dump(образец, allow_unicode=True, sort_keys=False),
                             encoding="utf-8")
        прежние = set(прежнее)
        старт = двор / "старт"
        среда = dict(os.environ, HARNESS_LIVE_CHECK_STATE=str(состояние))
        процессы = [subprocess.Popen(
            [sys.executable, "-c", ПИСАТЕЛЬ, str(писатель), метка, str(старт),
             str(ПО_СКОЛЬКО), ПРЕФИКС],
            env=среда, stderr=subprocess.PIPE, text=True) for метка in ("а", "б")]
        time.sleep(1.0)  # оба загрузили модуль и ждут старта
        старт.touch()
        ошибки = [п.communicate(timeout=120)[1] for п in процессы]
        коды = [п.returncode for п in процессы]
        имя = писатель.name
        случай(f"{имя}: оба писателя кончили без ошибки", коды == [0, 0],
               f"коды {коды}; {' | '.join(о.strip()[-200:] for о in ошибки if о.strip())}")
        try:
            итог = (yaml.safe_load(состояние.read_text(encoding="utf-8")) or {}).get(
                "проверено", {})
        except yaml.YAMLError as сбой:
            случай(f"{имя}: состояние после гонки разбирается как yaml", False, str(сбой))
            return
        новые = [к for к in итог if str(к).startswith(ПРЕФИКС) and к not in прежние]
        ждём = 2 * ПО_СКОЛЬКО
        случай(f"{имя}: после гонки двух записать() лежат все {ждём} записей",
               len(новые) == ждём, f"лежит {len(новые)} из {ждём}")
        пропали = прежние - set(итог)
        случай(f"{имя}: прежние записи состояния ({len(прежние)}) целы",
               not пропали,
               f"прежних {len(прежние)}, пропало {len(пропали)}")
    finally:
        shutil.rmtree(двор, ignore_errors=True)


def main() -> int:
    print(f"[проба замка живой проверки] испытуемые: {ИСПЫТУЕМЫЕ}")
    случай("образец состояния найден", ОБРАЗЕЦ.is_file(), str(ОБРАЗЕЦ))
    for имя in ПИСАТЕЛИ:
        писатель = ИСПЫТУЕМЫЕ / имя
        if not писатель.is_file():
            случай(f"{имя}: файл есть", False, str(писатель))
            continue
        вхождений = писатель.read_text(encoding="utf-8").count("flock")
        случай(f"{имя}: замок flock в тексте", вхождений >= 1, f"вхождений {вхождений}")
        if ОБРАЗЕЦ.is_file():
            гонка(писатель)
    print(f"[проба замка живой проверки] хорошо {хорошо}, плохо {плохо}")
    return 0 if плохо == 0 and хорошо > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
