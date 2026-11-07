#!/usr/bin/env python3
"""Проба: следы живой проверки по умолчанию лежат ВНЕ дерева репозитория.

Откуда взято. Карточка hr.sledy-proizvolnyh-komand-lezhat-v-gite, ревью
безопасности 17.10 (F-sec-07): в след уезжает весь вывод произвольной команды,
а каталог следов по умолчанию стоял внутри репозитория — гит учёл 174 таких
файла, и судья секретов голое значение и имя файла-ключа не ловит.

Решение: ключ harness.conf LIVE_CHECK_TRACE_ROOT (пусто = $LOG_DIR/живая-проверка),
под ним — умолчания обёртки, шести приборов и BOOT_CHECK_TRACE_DIR; переменные
HARNESS_*_TRACE_DIR по-прежнему перекрывают умолчание.

Свой двор: обёртка зовётся из КОПИИ дерева (иначе красный прогон сам насорил бы
в боевой репозиторий), LOG_DIR и HARNESS_HOME — временный стенд. Путь обёртки
берётся из HARNESS_LIVE_CHECK_WRAPPER: так мутационная проба подсовывает
испорченную копию.

Прогон: python3 scripts/test_live_check_trace_outside_repo.py
Код возврата: 0 — все случаи зелёные, 1 — хоть один плохой.
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

СКРИПТЫ = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(СКРИПТЫ / "lib"))
import config as конф  # noqa: E402

# Корень установки, а не parent.parent: в пакете скрипты лежат в harness/scripts.
КОРЕНЬ = pathlib.Path(конф.корень_дерева(__file__))
ОБЁРТКА = pathlib.Path(os.environ.get("HARNESS_LIVE_CHECK_WRAPPER")
                       or СКРИПТЫ / "live-check.sh")
ПРИБОРЫ = ("tool", "daemon", "inbox", "sick", "hook", "skill")

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


def внутри(путь: pathlib.Path, корень: pathlib.Path) -> bool:
    try:
        путь.resolve().relative_to(корень.resolve())
        return True
    except ValueError:
        return False


def среда_стенда(стенд: pathlib.Path) -> dict:
    """Окружение без единой двери следа: судим именно умолчание."""
    журналы = стенд / "log"
    дом = стенд / "home"
    журналы.mkdir(parents=True, exist_ok=True)
    дом.mkdir(parents=True, exist_ok=True)
    shutil.copy(КОРЕНЬ / "harness" / "config" / "harness.conf", дом / "harness.conf")
    (дом / "install.conf").write_text(f'LOG_DIR="{журналы}"\n', encoding="utf-8")
    среда = {к: з for к, з in os.environ.items()
             if not (к.endswith("TRACE_DIR") or к.endswith("TRACE_ROOT")
                     or к == "HARNESS_LIVE_CHECK_REPO")}
    среда.update(LOG_DIR=str(журналы), HARNESS_LOG_DIR=str(журналы),
                 HARNESS_HOME=str(дом))
    return среда


def файлы(корень: pathlib.Path) -> set:
    return {п.relative_to(корень) for п in корень.rglob("*") if п.is_file()}


def случай_обёртки(стенд: pathlib.Path) -> None:
    """(а) обёртка без переменной пишет след под стендом, в дерево — ничего."""
    дерево = стенд / "repo"
    (дерево / "scripts").mkdir(parents=True)
    shutil.copy(ОБЁРТКА, дерево / "scripts" / "live-check.sh")
    shutil.copytree(СКРИПТЫ / "lib", дерево / "scripts" / "lib")
    shutil.copytree(КОРЕНЬ / "harness" / "config", дерево / "harness" / "config")
    до = файлы(дерево)
    метка = "метка-вывода-механизма-7f3a"
    итог = subprocess.run(
        ["bash", str(дерево / "scripts" / "live-check.sh"), "scripts/proba.sh",
         "--", "echo", метка],
        env=среда_стенда(стенд), capture_output=True, text=True, timeout=60)
    новые = sorted(str(п) for п in файлы(дерево) - до)
    случай("(а) обёртка отработала кодом 0", итог.returncode == 0,
           f"код {итог.returncode}: {итог.stderr.strip()[:200]}")
    случай("(а) в дереве репозитория нет новых файлов", not новые,
           f"появились: {новые[:3]}")
    следы = [п for п in (стенд / "log").rglob("*.log")
             if метка in п.read_text(encoding="utf-8", errors="replace")]
    случай("(а) след с выводом механизма лежит под LOG_DIR стенда", bool(следы),
           "под стендом нет файла с выводом механизма")


def случай_прибора(стенд: pathlib.Path, прибор: str) -> None:
    """(б) умолчание СЛЕДЫ прибора — под LOG_DIR стенда и вне КОРЕНЬ.

    Модуль грузится в своём процессе: кэш конфига одного прибора не должен
    подсказывать ответ другому."""
    код = ("import importlib.util, sys\n"
           f"с = importlib.util.spec_from_file_location('m', {str(СКРИПТЫ / f'live-check-{прибор}.py')!r})\n"
           "м = importlib.util.module_from_spec(с); с.loader.exec_module(м)\n"
           "print(м.СЛЕДЫ)\n")
    итог = subprocess.run([sys.executable, "-c", код], env=среда_стенда(стенд),
                          capture_output=True, text=True, timeout=60, cwd=стенд)
    if итог.returncode != 0:
        случай(f"(б) live-check-{прибор}.py: СЛЕДЫ вне репозитория", False,
               f"импорт упал: {итог.stderr.strip()[-200:]}")
        return
    следы = pathlib.Path(итог.stdout.strip().splitlines()[-1])
    случай(f"(б) live-check-{прибор}.py: СЛЕДЫ вне репозитория",
           следы.is_absolute() and not внутри(следы, КОРЕНЬ)
           and внутри(следы, стенд / "log"),
           f"СЛЕДЫ = {следы}")


def умолчание_оболочки(текст: str, ключ: str):
    """Умолчание из ${КЛЮЧ:-…} или ${КЛЮЧ:=…}; None — формы нет вовсе."""
    найдено = re.search(r"\$\{" + ключ + r":[-=]([^}]*)\}", текст)
    return найдено.group(1) if найдено else None


def случай_загрузки() -> None:
    """(в) след проверки при загрузке: ни конфиг, ни часовой не ведут в дерево."""
    # Судим файл раскладки строго, без слоя окружения: иначе подставленная
    # переменная ответила бы за шаблон.
    ключи = конф.из_файла(str(КОРЕНЬ / "harness" / "config" / "harness.conf"))
    случай("(в) в harness.conf есть ключ LIVE_CHECK_TRACE_ROOT",
           "LIVE_CHECK_TRACE_ROOT" in ключи)
    случай("(в) в harness.conf есть ключ BOOT_CHECK_TRACE_DIR",
           "BOOT_CHECK_TRACE_DIR" in ключи)
    загрузка = ключи.get("BOOT_CHECK_TRACE_DIR", "")
    случай("(в) BOOT_CHECK_TRACE_DIR пуст или абсолютный путь вне репозитория",
           загрузка == "" or (загрузка.startswith("/")
                              and not внутри(pathlib.Path(загрузка), КОРЕНЬ)),
           f"BOOT_CHECK_TRACE_DIR={загрузка}")
    часовой = (КОРЕНЬ / "harness" / "demons" / "sentinel.sh").read_text(encoding="utf-8")
    умолчание = умолчание_оболочки(часовой, "BOOT_CHECK_TRACE_DIR")
    случай("(в) sentinel.sh называет умолчание BOOT_CHECK_TRACE_DIR",
           умолчание is not None, "формы ${BOOT_CHECK_TRACE_DIR:-…} нет")
    случай("(в) умолчание sentinel.sh не ведёт в .tasks/",
           умолчание is not None and not умолчание.startswith(".tasks/")
           and not умолчание.startswith("$PROJECT_DIR/.tasks"),
           f"умолчание: {умолчание}")


def случай_игнора() -> None:
    """(г) старые следы, если где-то остались, гит не подхватит."""
    if not (КОРЕНЬ / ".tasks").is_dir():
        print("  пропуск (г): в этой раскладке нет .tasks, .gitignore проекта не наш")
        return
    игнор = (КОРЕНЬ / ".gitignore").read_text(encoding="utf-8").splitlines()
    строки = {с.strip().rstrip("/") for с in игнор}
    for образец in (".tasks/*/живьём", ".tasks/*/живьём-*"):
        случай(f"(г) .gitignore содержит {образец}",
               образец in строки or "/" + образец in строки)


def main() -> int:
    print("Следы живой проверки — вне репозитория")
    with tempfile.TemporaryDirectory(prefix="trace-outside-") as тмп:
        стенд = pathlib.Path(тмп)
        случай_обёртки(стенд / "a")
        for прибор in ПРИБОРЫ:
            случай_прибора(стенд / f"b-{прибор}", прибор)
    случай_загрузки()
    случай_игнора()
    print(f"итог: {хорошо} ок, {плохо} плохо")
    return 1 if плохо else 0


if __name__ == "__main__":
    sys.exit(main())
