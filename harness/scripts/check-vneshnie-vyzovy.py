#!/usr/bin/env python3
"""Гейт: расписания и юниты зовут ФАЙЛЫ, которые существуют.

Откуда взят. Перевод имён на латиницу 11.09.2026 переименовал
`scripts/fonovaya-cel.sh`, и файл расписания продукта в `/etc/cron.d` стал
звать по исчезнувшему имени. Ни один сторож этого не видел: гейты судят
репозиторий, а cron и systemd лежат в /etc. Отказ проявился бы ночью строкой
«No such file or directory» в логе, который никто не читает.

Смотрит: crontab пользователя, /etc/cron.d/*, юниты systemd проекта. Из них
берёт пути, ведущие В КОРЕНЬ ПРОЕКТА (абсолютные и относительные после `cd`),
и проверяет, что файл на месте. Чужие пути не трогает.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
РАСШИРЕНИЯ = (".sh", ".py")


def строки_расписаний():
    """(откуда, строка) по всем местам, где живут внешние вызовы."""
    итог = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if итог.returncode == 0:
        for строка in итог.stdout.splitlines():
            yield "crontab", строка
    места = list(Path("/etc/cron.d").glob("*")) if Path("/etc/cron.d").is_dir() else []
    места += list(Path("/etc/systemd/system").glob("*.service"))
    места += list(Path("/etc/systemd/system").glob("*.timer"))
    for файл in места:
        if not файл.is_file() or not os.access(файл, os.R_OK):
            continue
        for строка in файл.read_text(encoding="utf-8", errors="replace").splitlines():
            yield str(файл), строка


def пути_строки(строка: str, корень: Path):
    """Пути к файлам проекта, которые зовёт эта строка."""
    if строка.lstrip().startswith("#"):
        return
    # `cd <куда> && …` меняет точку отсчёта относительных путей.
    где = корень
    сменил = re.search(r"\bcd[ \t]+(\S+)", строка)
    if сменил:
        кандидат = Path(сменил.group(1))
        if str(кандидат).startswith(str(корень)):
            где = кандидат
    # Знак «=» — разделитель: в юните путь стоит как «ExecStart=/путь/к.sh»,
    # и без этого весь токен принимался за относительный путь.
    for кусок in re.findall(r"[^\s'\"<>|&;=]+", строка):
        if not кусок.endswith(РАСШИРЕНИЯ):
            continue
        путь = Path(кусок) if кусок.startswith("/") else где / кусок
        try:
            путь = путь.resolve()
        except OSError:
            continue
        if str(путь).startswith(str(корень)):
            yield путь


def устаревшие_сервисы(корень=КОРЕНЬ):
    """Сервисы, работающие на КОДЕ СТАРШЕ своего запуска.

    Живой случай 11.09.2026: файлы переименованы, а диспетчер канала —
    долгоживущий сервис — держал в памяти прежний текст скрипта и звал
    скачивание вложений по исчезнувшему имени. Картинка владельца не дошла,
    а в репозитории всё было правильно. Правка файла не доходит до running
    процесса: его надо перезапустить.
    """
    # Юнит агента судить нечем: его ExecStart — скрипт ЗАПУСКА, он отрабатывает
    # один раз и в памяти не живёт, а «перезапуск» означал бы ротацию сессии,
    # которая идёт своим механизмом. Долгоживущие сервисы (диспетчер канала,
    # сторожа) — судятся.
    НЕ_СУДИМ = {"harness-agent.service"}
    из_systemd = subprocess.run(
        ["systemctl", "list-units", "--type=service", "--all", "--no-legend", "harness-*"],
        capture_output=True, text=True)
    беды = []
    for строка in из_systemd.stdout.splitlines():
        юнит = строка.split()[0] if строка.split() else ""
        if not юнит.endswith(".service") or юнит in НЕ_СУДИМ:
            continue
        показ = subprocess.run(
            ["systemctl", "show", юнит, "-p", "ActiveEnterTimestamp", "-p", "ExecStart",
             "-p", "ActiveState"],
            capture_output=True, text=True).stdout
        поля = dict(с.split("=", 1) for с in показ.splitlines() if "=" in с)
        if поля.get("ActiveState") != "active" or not поля.get("ActiveEnterTimestamp"):
            continue
        старт = subprocess.run(["date", "-d", поля["ActiveEnterTimestamp"], "+%s"],
                               capture_output=True, text=True).stdout.strip()
        if not старт.isdigit():
            continue
        # systemd печатает ExecStart как «{ path=/путь ; argv[]=… }» — путь
        # отделяется от «path=» знаком равенства, иначе он не начинается с «/»
        # и проверка молча ничего не находит (поймано пробой 11.09).
        for кусок in re.findall(r"[^\s;{}=]+", поля.get("ExecStart", "")):
            if not кусок.startswith("/") or not кусок.endswith(РАСШИРЕНИЯ):
                continue
            файл = Path(кусок)
            if not str(файл).startswith(str(корень)) or not файл.exists():
                continue
            if файл.stat().st_mtime > int(старт) + 5:
                беда = (f"{юнит}: работает на старом коде — {файл} правился "
                        f"после запуска. Перезапустить: sudo systemctl restart {юнит}")
                if беда not in беды:      # systemd печатает путь дважды: path= и argv[]
                    беды.append(беда)
    return беды


def находки(корень=КОРЕНЬ):
    беды, проверено = [], 0
    for откуда, строка in строки_расписаний():
        for путь in пути_строки(строка, корень):
            проверено += 1
            if not путь.exists():
                беды.append(f"{откуда}: нет файла {путь}  ←  {строка.strip()[:70]}")
    return беды, проверено


def самотест():
    """Гейт обязан создать своё условие: строка, зовущая исчезнувший файл."""
    ok = True
    живой = f"0 4 * * * {КОРЕНЬ}/scripts/check-imports.py"
    мёртвый = f"20 1 * * * agent cd {КОРЕНЬ}/app && scripts/net-takogo.sh риск"
    чужой = "0 3 * * * /usr/local/bin/chuzhoj-skript.sh"

    если_живой = list(пути_строки(живой, КОРЕНЬ))
    if len(если_живой) == 1 and если_живой[0].exists():
        print("  ок    существующий файл гейт не трогает")
    else:
        ok = False
        print(f"  ПЛОХО живой путь не разобран: {если_живой}")

    если_мёртвый = list(пути_строки(мёртвый, КОРЕНЬ))
    if len(если_мёртвый) == 1 and not если_мёртвый[0].exists():
        print("  ок    БОЛЬНОЙ СЛУЧАЙ: относительный путь после `cd` разобран и назван мёртвым")
    else:
        ok = False
        print(f"  ПЛОХО мёртвый путь после cd не найден: {если_мёртвый}")

    юнит = f"ExecStart={КОРЕНЬ}/scripts/net-takogo.sh --boot"
    если_юнит = list(пути_строки(юнит, КОРЕНЬ))
    if len(если_юнит) == 1 and если_юнит[0].name == "net-takogo.sh":
        print("  ок    БОЛЬНОЙ СЛУЧАЙ: путь в строке юнита отделяется от ExecStart=")
    else:
        ok = False
        print(f"  ПЛОХО строка юнита разобрана неверно: {если_юнит}")

    if not list(пути_строки(чужой, КОРЕНЬ)):
        print("  ок    чужой путь вне проекта не судится")
    else:
        ok = False
        print("  ПЛОХО чужой путь попал под суд")

    print("САМОТЕСТ %s: 4 пути, два — больные случаи"
          % ("ПРОЙДЕН" if ok else "ПРОВАЛЕН"))
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        return самотест()
    беды, проверено = находки()
    беды += устаревшие_сервисы()
    if беды:
        print(f"[внешние вызовы] СЛОМАНЫ: {len(беды)} из {проверено}")
        for строка in беды[:20]:
            print("   ", строка)
        return 1
    print(f"[внешние вызовы] расписания и юниты зовут существующие файлы ({проверено})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
