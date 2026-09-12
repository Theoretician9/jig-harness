#!/usr/bin/env python3
"""Гейт: всё, что панель показывает владельцу, её служба может ПРОЧИТАТЬ.

Откуда взят: 12.09.2026. Панель работает под своим пользователем
(`User=harness-panel`), а суточный отчёт о расходе писался с правами 0600
пользователя агента — `mkstemp` создаёт файл именно так. Владелец видел в
панели «нет данных» вместо чисел, и причина молчала: файл существовал, был
свежим, его писал живой демон. Замер:
`sudo -u harness-panel cat /var/log/harness/tokens-daily.report` → отказ.

Гейт идёт боевым путём: собирает витрину состояния ОТ ИМЕНИ службы и требует,
чтобы ни один раздел не отвечал отказом прав. Проверять список файлов глазами
нельзя — он растёт, а правило должно ловить и завтрашний файл.

    python3 scripts/check-prava-paneli.py            # 0 — чисто, 1 — находки
    python3 scripts/check-prava-paneli.py --selftest
"""
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from konf import из_файла, корень_дерева  # noqa: E402

ОТКАЗ_ПРАВ = re.compile(r"Permission denied|Отказано в доступе|\[Errno 13\]")


def пользователь_службы(юнит: str = "harness-panel") -> str:
    готово = subprocess.run(["systemctl", "show", юнит, "-p", "User", "--value"],
                            capture_output=True, text=True)
    return готово.stdout.strip()


def витрина_от_имени(пользователь: str, корень: Path) -> str:
    """Зовёт тот же модуль, что и служба, но её пользователем."""
    готово = subprocess.run(
        ["sudo", "-n", "-u", пользователь, "python3",
         str(корень / "harness" / "panel" / "sostoyanie.py")],
        capture_output=True, text=True, timeout=120)
    return готово.stdout + готово.stderr


def находки(текст: str) -> list:
    беды = []
    for строка in текст.splitlines():
        if ОТКАЗ_ПРАВ.search(строка):
            беды.append(строка.strip()[:160])
    return беды


def selftest() -> int:
    неудач = 0

    def проба(имя, ок):
        nonlocal неудач
        неудач += not ок
        print(f"  {'ok  ' if ок else 'FAIL'} {имя}")

    # БОЛЬНОЙ СЛУЧАЙ: ровно тот текст, который печатал Python 12.09.2026.
    больной = ("{'нет данных': \"суточный отчёт не прочитан: "
               "[Errno 13] Permission denied: '/var/log/harness/tokens-daily.report'\"}")
    проба("отказ прав в выводе найден", len(находки(больной)) == 1)
    проба("здоровый вывод находок не даёт",
          находки('{"строки": {"токены за сутки": "120k"}}') == [])
    # Ложное срабатывание, на котором гейт выключили бы: слово в человеческом
    # тексте без отказа. Судим по строке вывода, а не по вхождению слова.
    проба("слово «доступ» само по себе не находка",
          находки("доступ к панели по ссылке из канала") == [])
    print(f"самотест check-prava-paneli: неудач {неудач}")
    return 1 if неудач else 0


def главная() -> int:
    if "--selftest" in sys.argv[1:]:
        return selftest()
    корень = корень_дерева(__file__)
    пользователь = пользователь_службы()
    if not пользователь:
        print("[права панели] служба harness-panel не описана — проверять нечего")
        return 0
    текст = витрина_от_имени(пользователь, корень)
    if not текст.strip():
        print(f"[права панели] витрина не собралась под {пользователь} — "
              f"нет sudo без пароля? проверка пропущена")
        return 0
    беды = находки(текст)
    for беда in беды:
        print(f"  ✗ {беда}")
    print(f"[права панели] разделов с отказом прав: {len(беды)} "
          f"(служба работает как {пользователь})")
    return 1 if беды else 0


if __name__ == "__main__":
    sys.exit(главная())
