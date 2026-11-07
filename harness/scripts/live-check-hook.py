#!/usr/bin/env python3
"""Живая проверка перехватчика (класс B1): совершено действие — видно решение.

Откуда взято: спека `docs/specs/2026-10-15-живая-проверка-механизмов.md` §3.1,
класс B1 — «совершено перехватываемое действие и увидено решение». Носителя
этому предикату не было, и шесть хуков оболочки оставались непроверенными:
гейт судил только общий след, а предикат класса не судил никто.

Почему не класс C1. У хука оболочки при РАЗРЕШЕНИИ нет вывода вовсе — он
пропускает молча, и «здоровая половина» доказывалась бы эхом проверяющего
(находка 17.10.2026). Поэтому доказательство одно: хук позван СОБЫТИЕМ ровно
той формы, какую шлёт ему оболочка, и он ответил своими словами.

Два разных факта и два разных отказа. «Решение видно» — это текст хука;
«решение записано» — это строка в ЕГО журнале, написанная им самим. Прибор
требует ПРИРОСТ журнала, а не его существование: файл есть всегда, и отказ по
одному коду был бы неотличим от другого ([[a-check-creates-its-own-condition]]).

Запуск:
    python3 scripts/live-check-hook.py <механизм> [<механизм>…] [--записать]
    python3 scripts/live-check-hook.py --всё [--записать]
Код возврата: 0 — решение доказано у всех названных, 1 — хоть у одного нет.

Чем доказан: scripts/test_live_check_hook.py (больным случаем первым — хук,
который промолчал).
"""
from __future__ import annotations

import datetime
import os
import pathlib
import subprocess
import sys

import yaml

КОРЕНЬ = pathlib.Path(os.environ.get("HARNESS_LIVE_CHECK_REPO")
                      or pathlib.Path(__file__).resolve().parent.parent)
СОСТОЯНИЕ = pathlib.Path(os.environ.get("HARNESS_LIVE_CHECK_STATE")
                         or КОРЕНЬ / "harness" / "config" / "live-check-state.yaml")
ДАННЫЕ = pathlib.Path(os.environ.get("HARNESS_LIVE_CHECK_HOOKS")
                      or КОРЕНЬ / "harness" / "config" / "live-check-hooks.yaml")
def _корень_следов() -> pathlib.Path:
    """Корень следов — у общего загрузчика: умолчание внутри репозитория
    отдавало весь вывод произвольной команды гиту."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "lib"))
    import config as конф                                 # noqa: PLC0415
    return pathlib.Path(конф.live_check_trace_root())


СЛЕДЫ = pathlib.Path(os.environ.get("HARNESS_LIVE_CHECK_HOOK_TRACE_DIR")
                     or _корень_следов() / "hr.zhivaya-proverka-vseh-mehanizmov"
                     / "живьём-хуки")
РАЗДЕЛИТЕЛЬ = "--- решение хука ---"
ЦИТАТА_ЗНАКОВ = 12
ПРЕДЕЛ_СЕК = 60


def log_dir() -> str:
    """Каталог журналов. Своя переменная старше конфига: без неё проба играла
    бы на БОЕВЫХ журналах ([[the-probe-played-on-production]])."""
    свой = os.environ.get("HARNESS_LOG_DIR")
    if свой:
        return свой
    sys.path.insert(0, str(КОРЕНЬ / "scripts" / "lib"))
    import config as конф                                 # noqa: PLC0415
    return конф.log_dir()


def данные() -> dict:
    if not ДАННЫЕ.is_file():
        print(f"[живая проверка хуков] нет данных {ДАННЫЕ} — судить нечем",
              file=sys.stderr)
        return {}
    return (yaml.safe_load(ДАННЫЕ.read_text(encoding="utf-8")) or {}).get("хуки") or {}


def строки_журнала(путь: pathlib.Path) -> list[str]:
    """Строки журнала сейчас. Файла нет — пусто, а не отказ: прирост с нуля
    тоже прирост, а падение здесь читалось бы как отказ по ДРУГОЙ причине —
    и проба, судящая только код возврата, зеленела бы на нём."""
    try:
        return путь.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def строк(путь: pathlib.Path) -> int:
    return len(строки_журнала(путь))


def позвать(механизм: str, событие: str) -> subprocess.CompletedProcess:
    """Хук зовётся ТАК ЖЕ, как его зовёт оболочка: событие приходит на stdin."""
    файл = КОРЕНЬ / механизм
    команда = (["bash", str(файл)] if файл.suffix == ".sh"
               else [sys.executable, str(файл)])
    return subprocess.run(команда, input=событие, capture_output=True,
                          text=True, timeout=ПРЕДЕЛ_СЕК, cwd=str(КОРЕНЬ),
                          env=dict(os.environ, CLAUDE_PROJECT_DIR=str(КОРЕНЬ),
                                    LOG_DIR=log_dir()))


def судить(механизм: str, запись: dict) -> tuple[bool, str, dict]:
    """(доказано, что сказать, поля записи) для одного перехватчика."""
    событие = (запись or {}).get("событие")
    ждём = ((запись or {}).get("ждём_в_решении") or "").strip()
    if not событие or not ждём:
        return False, (f"{механизм}: в данных не названо событие или слово "
                       f"решения — придумывать их прибор не вправе"), {}

    журнал_имя = ((запись or {}).get("журнал") or "").strip()
    журнал = pathlib.Path(log_dir()) / журнал_имя if журнал_имя else None
    было = строк(журнал) if журнал else 0

    готово = позвать(механизм, str(событие))
    решение = (готово.stdout or "") + (готово.stderr or "")
    if ждём not in решение:
        return False, (f"{механизм}: решения не видно — в ответе хука нет "
                       f"«{ждём}» (код {готово.returncode}, ответ "
                       f"{решение.strip()[:120]!r})"), {}

    прирост = []
    if журнал is not None:
        стало = строк(журнал)
        if стало <= было:
            return False, (f"{механизм}: решение видно, а своего следа хук не "
                           f"оставил — журнал {журнал} не вырос ({было} строк "
                           f"как было)"), {}
        прирост = строки_журнала(журнал)[было:стало]

    годные = [" ".join(с.split()) for с in решение.splitlines()
              if len(" ".join(с.split())) >= ЦИТАТА_ЗНАКОВ]
    if not годные:
        return False, (f"{механизм}: в решении нет строки длиннее "
                       f"{ЦИТАТА_ЗНАКОВ} знаков — цитировать нечего"), {}

    СЛЕДЫ.mkdir(parents=True, exist_ok=True)
    след = СЛЕДЫ / (механизм.replace("/", "-") + ".log")
    когда = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    след.write_text(
        f"время: {когда}\n"
        f"механизм: {механизм}\n"
        f"событие на stdin: {str(событие)[:400]}\n"
        f"код хука: {готово.returncode}\n"
        f"{РАЗДЕЛИТЕЛЬ}\n{решение.rstrip()}\n"
        + (f"--- строки, которые хук дописал в {журнал_имя} ---\n"
           + "\n".join(прирост) + "\n" if прирост else ""),
        encoding="utf-8")
    путь_следа = str(след)
    if след.is_relative_to(КОРЕНЬ):
        путь_следа = str(след.relative_to(КОРЕНЬ))

    поля = {
        "живьём": (f"позван событием той же формы, какую шлёт оболочка: "
                   f"{str(событие)[:160]} — хук ответил своим решением"
                   + (f" и дописал {len(прирост)} строк(и) в {журнал_имя}"
                      if прирост else "")),
        "увидели": годные[0],
        "след": путь_следа,
        "когда": datetime.date.today().isoformat(),
        "исход": "ок",
    }
    return True, f"{механизм}: решение доказано — {годные[0][:80]}", поля


def записать(исходы: dict) -> None:
    данные_сост = yaml.safe_load(СОСТОЯНИЕ.read_text(encoding="utf-8")) or {}
    сост = данные_сост.setdefault("проверено", {})
    for механизм, поля in исходы.items():
        сост.setdefault(механизм, {}).update(поля)
    СОСТОЯНИЕ.write_text(
        yaml.dump(данные_сост, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8")
    print(f"[живая проверка хуков] записано механизмов: {len(исходы)}")


def main() -> int:
    писать = "--записать" in sys.argv
    названные = [д for д in sys.argv[1:] if not д.startswith("--")]
    хуки = данные()
    if "--всё" in sys.argv:
        названные = sorted(хуки)
    if not названные:
        print("[живая проверка хуков] нечего судить: назовите механизм или «--всё»",
              file=sys.stderr)
        return 1

    исходы, беды = {}, []
    for механизм in названные:
        доказано, слово, поля = судить(механизм, хуки.get(механизм) or {})
        if доказано:
            print(f"  ок    {слово}")
            исходы[механизм] = поля
        else:
            print(f"  ПЛОХО {слово}", file=sys.stderr)
            беды.append(механизм)

    if писать and исходы:
        записать(исходы)
    if беды:
        print(f"[живая проверка хуков] ✗ не доказано: {len(беды)} из "
              f"{len(названные)}", file=sys.stderr)
        return 1
    print(f"[живая проверка хуков] доказано все {len(исходы)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
