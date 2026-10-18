#!/usr/bin/env python3
"""Проба гейта имён: служебные скрипты задачи судятся только на латиницу.

Беда (3.2 отчёта соседней установки, 18.10.2026): гейт имён отвергал `typecheck`,
`tsc`, `selfcheck`, `probe-sn-*` в `.tasks/*/run/` — одноразовых файлах
задачи, которые модулем не становятся. Там краснел словарь транслита, хотя
транслита в именах нет. Обход того сервера — внести слова в общий словарь
пакета (`874e272`), то есть продуктовые слова уезжали всем установкам.

Правило: под `.tasks/<задача>/run/` имя судится только на латиницу, без
словаря; всё прочее — как раньше. Два носителя у правила — гейт
(`транслитные_имена`) и хук создания файла (`--hook`), и проба судит оба.

Дверь для мутаций: CHECK_FILE_NAMES_UNDER_TEST — путь к испорченной копии.

    python3 scripts/test_check_file_names.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ЗДЕСЬ = Path(__file__).resolve().parent
ПОДСУДНЫЙ = Path(os.environ.get("CHECK_FILE_NAMES_UNDER_TEST")
                 or ЗДЕСЬ / "check-file-names.py")


def загрузить(путь: Path):
    спец = importlib.util.spec_from_file_location("check_file_names_под_тестом", путь)
    модуль = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(модуль)
    return модуль


def main() -> int:
    гейт = загрузить(ПОДСУДНЫЙ)
    словарь = гейт.английские_слова()
    плохо, всего = 0, 0

    def итог(верно: bool, имя: str, подробно: str = "") -> None:
        nonlocal плохо, всего
        всего += 1
        if верно:
            print(f"  ок    {имя}")
        else:
            плохо += 1
            print(f"  ПЛОХО {имя}" + (f" — {подробно}" if подробно else ""))

    if not словарь:
        print("ПРОБА СЛОМАНА: словарь английского не прочитан — суда о транслите нет")
        return 1

    # (путь, ждём ли находку транслита, имя случая)
    транслит = [
        (".tasks/hr.x/run/typecheck.sh", False,
         "БОЛЬНОЙ 3.2: .tasks/*/run/typecheck.sh — служебный скрипт, словарь не судит"),
        (".tasks/hr.x/run/tsc.sh", False, "БОЛЬНОЙ 3.2: .tasks/*/run/tsc.sh"),
        (".tasks/hr.x/run/selfcheck.py", False, "БОЛЬНОЙ 3.2: .tasks/*/run/selfcheck.py"),
        (".tasks/hr.x/run/probe-sn-1.py", False, "БОЛЬНОЙ 3.2: .tasks/*/run/probe-sn-1.py"),
        ("scripts/typecheck-x.py", True,
         "сосед: scripts/typecheck-x.py — код харнеса по-прежнему судится словарём"),
        (".tasks/hr.x/typecheck.sh", True,
         "сосед: .tasks/<задача>/ вне run/ — по-прежнему судится словарём"),
        ("scripts/proverka-vorot.sh", True,
         "сосед: транслит в коде харнеса — находка, как раньше"),
    ]
    for путь, ждём, имя in транслит:
        найдено = гейт.транслитные_имена([путь], словарь=словарь)
        итог(bool(найдено) == ждём, имя, f"находки: {найдено}")

    # Латиница под run/ держится по-прежнему: послабление — про словарь, не про
    # кириллицу (её git отдаёт байтами, и проверки по путям слепнут).
    найдено = гейт.находки([".tasks/hr.x/run/проверка.sh"])
    итог(len(найдено) == 1,
         "сосед: .tasks/*/run/проверка.sh — кириллица в имени кода по-прежнему находка",
         f"находки: {найдено}")

    # Второй носитель — хук создания файла: правило, перекрытое одним путём из
    # двух, обходится вторым. Путь несуществующий: хук судит только рождение.
    with tempfile.TemporaryDirectory() as стенд:
        хук = [
            (".tasks/hr.zz-проба/run/typecheck.sh", 0,
             "БОЛЬНОЙ 3.2: хук пропускает создание .tasks/*/run/typecheck.sh"),
            (".tasks/hr.zz-проба/run/проверка.sh", 2,
             "сосед: хук отказывает .tasks/*/run/проверка.sh (кириллица)"),
            ("scripts/typecheck-x.py", 2,
             "сосед: хук отказывает scripts/typecheck-x.py (словарь)"),
        ]
        for путь, ждём, имя in хук:
            нагрузка = json.dumps({"tool_name": "Write",
                                   "tool_input": {"file_path": путь}})
            вышло = subprocess.run([sys.executable, str(ПОДСУДНЫЙ), "--hook"],
                                   input=нагрузка, capture_output=True, text=True,
                                   cwd=стенд, timeout=60)
            итог(вышло.returncode == ждём, имя,
                 f"код {вышло.returncode}, ждали {ждём}: {вышло.stderr.strip()[:160]}")

    print(f"\nвсего путей: {всего}, неудач: {плохо}")
    return 1 if плохо else 0


if __name__ == "__main__":
    sys.exit(main())
