#!/usr/bin/env python3
"""Каталог следов живой проверки — ДАННЫЕ установки, а не код.

Пять приборов живой проверки (daemon, hook, inbox, sick, skill) клали следы в
каталог, имя которого — id карточки ЭТОЙ установки, зашитый в код. Код едет в
публичную сборку, и замок «карточка=» сборки краснел на каждом приборе
(14.11.2026, релиз 0.11.1). Имя каталога теперь — ключ «каталог_следов»
состояния живой проверки (harness/config/live-check-state.yaml): у нашей
установки он называет прежний каталог (живые следы читаются, как раньше), а
заготовка состояния, которая едет в сборку, ключа не несёт — копия получает
нейтральный каталог «живая-проверка», как у прибора и пробы.

Договор, по каждому из пяти приборов:
  * ключ «каталог_следов: X» в состоянии → следы под <корень>/X/…;
  * ключа нет → под <корень>/живая-проверка/…;
  * файла состояния нет → тоже нейтральный каталог, а не отказ.

Стенд — свой mkdtemp: состояние и корень следов из окружения
(HARNESS_LIVE_CHECK_STATE, LIVE_CHECK_TRACE_ROOT); переменные каталогов
следов каждого прибора сняты. Живые состояние и журналы не читаются.

Дверь для мутаций: HARNESS_LIVE_CHECK_TRACE_DIR_SRC — каталог приборов под
пробой (по умолчанию свой).
Итог: «КАТАЛОГ СЛЕДОВ ИЗ ДАННЫХ: путей N, неудач M», код 0/1.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ЗДЕСЬ = pathlib.Path(__file__).resolve().parent
ИСТОЧНИК = pathlib.Path(os.environ.get("HARNESS_LIVE_CHECK_TRACE_DIR_SRC") or ЗДЕСЬ)

# Прибор → имена его переменных-каталогов и хвост каталога под именем задачи.
ПРИБОРЫ = {
    "live-check-daemon.py": {"СЛЕДЫ": "живьём-демоны"},
    "live-check-hook.py": {"СЛЕДЫ": "живьём-хуки"},
    "live-check-inbox.py": {"СЛЕДЫ": "живьём-входящие"},
    "live-check-sick.py": {"СЛЕДЫ": "живьём-больные", "СЛЕДЫ_ЖИВЬЁМ": "живьём"},
    "live-check-skill.py": {"СЛЕДЫ": "живьём-скилы"},
}
ПЕРЕМЕННЫЕ_СЛЕДОВ = ("HARNESS_LIVE_CHECK_DAEMON_TRACE_DIR", "HARNESS_LIVE_CHECK_HOOK_TRACE_DIR",
                     "HARNESS_LIVE_CHECK_INBOX_TRACE_DIR", "HARNESS_LIVE_CHECK_SICK_TRACE_DIR",
                     "HARNESS_LIVE_CHECK_SICK_LIVE_TRACE_DIR", "HARNESS_LIVE_CHECK_SKILL_TRACE_DIR")

путей = неудач = 0


def случай(ok: bool, текст: str, подробно: str = "") -> None:
    global путей, неудач
    путей += 1
    неудач += 0 if ok else 1
    print(("  ок    " if ok else "  ПЛОХО ") + текст + ("" if ok else f": {подробно}"))


def каталоги(прибор: str, состояние: pathlib.Path, корень: pathlib.Path) -> dict:
    """Значения переменных-каталогов прибора после ввоза его модулем."""
    среда = {к: з for к, з in os.environ.items() if к not in ПЕРЕМЕННЫЕ_СЛЕДОВ}
    среда.update(HARNESS_LIVE_CHECK_STATE=str(состояние), LIVE_CHECK_TRACE_ROOT=str(корень))
    имена = list(ПРИБОРЫ[прибор])
    код = ("import importlib.util, sys\n"
           f"с = importlib.util.spec_from_file_location('прибор', {str(ИСТОЧНИК / прибор)!r})\n"
           "м = importlib.util.module_from_spec(с); с.loader.exec_module(м)\n"
           f"for и in {имена!r}: print(и + '=' + str(getattr(м, и)))\n")
    итог = subprocess.run([sys.executable, "-c", код], capture_output=True, text=True,
                          env=среда, timeout=60)
    if итог.returncode:
        return {"отказ": (итог.stderr or итог.stdout).strip()[-300:]}
    return dict(строка.split("=", 1) for строка in итог.stdout.splitlines() if "=" in строка)


def судить(прибор: str, состояние: pathlib.Path, корень: pathlib.Path,
           каталог: str, что: str) -> None:
    видно = каталоги(прибор, состояние, корень)
    for имя, хвост in ПРИБОРЫ[прибор].items():
        ждём = str(корень / каталог / хвост)
        случай(видно.get(имя) == ждём, f"{прибор} {имя}: {что} → {каталог}/{хвост}",
               f"ждали {ждём}, видно {видно.get(имя) or видно}")


def main() -> int:
    двор = pathlib.Path(tempfile.mkdtemp(prefix="proba-trace-dir-"))
    try:
        корень = двор / "следы"
        с_ключом = двор / "с-ключом.yaml"
        с_ключом.write_text("версия: 1\nкаталог_следов: stend-ustanovki\nпроверено: {}\n",
                            encoding="utf-8")
        без_ключа = двор / "без-ключа.yaml"
        без_ключа.write_text("версия: 1\nпроверено: {}\n", encoding="utf-8")
        нет_файла = двор / "нет.yaml"
        for прибор in ПРИБОРЫ:
            судить(прибор, с_ключом, корень, "stend-ustanovki", "ключ в состоянии")
            судить(прибор, без_ключа, корень, "живая-проверка", "ключа нет")
            судить(прибор, нет_файла, корень, "живая-проверка", "состояния нет")
    finally:
        shutil.rmtree(двор, ignore_errors=True)
    print(f"КАТАЛОГ СЛЕДОВ ИЗ ДАННЫХ: путей {путей}, неудач {неудач}")
    return 1 if неудач or not путей else 0


if __name__ == "__main__":
    sys.exit(main())
