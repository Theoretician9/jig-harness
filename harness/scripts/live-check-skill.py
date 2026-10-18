#!/usr/bin/env python3
"""Живая проверка скила: он был позван В ЭТОЙ ЗАДАЧЕ и оставил свой артефакт.

Откуда взято: спека `docs/specs/2026-10-15-живая-проверка-механизмов.md` §3.1,
класс E. Носителя этому предикату в коде не было, и 18 скилов оставались
непроверенными: «я его звал» — моё же слово, а не доказательство.

Доказательство здесь двойное и оба раза чужое:
  1. СТРОКА ЖУРНАЛА `$LOG_DIR/skills.jsonl`, написанная ХУКОМ оболочки
     (`"источник":"хук"`, `"успех":true`) с `ts` не раньше начала задачи. Запись
     рукой не считается — правило харнеса «журнал никогда не ведётся рукой».
  2. АРТЕФАКТ, который скил обязан оставить: маска названа в данных
     (`harness/config/skill-artifacts.yaml`), и файл обязан быть не старше
     начала задачи. Скил, оставивший файл год назад, в этой задаче не работал.
     Скил, который файла не создаёт, объявлен в данных маской «-» и причиной.

Запуск:
    python3 scripts/live-check-skill.py <скил> --задача <id> [--записать]
    python3 scripts/live-check-skill.py --всё --задача <id> [--записать]
Код возврата: 0 — вызов доказан у всех названных, 1 — хоть у одного нет.

Чем доказан: scripts/test_live_check_skill.py (больным случаем первым — вызова в
этой задаче нет).
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import sys

import yaml

КОРЕНЬ = pathlib.Path(os.environ.get("HARNESS_LIVE_CHECK_REPO")
                      or pathlib.Path(__file__).resolve().parent.parent)
СОСТОЯНИЕ = pathlib.Path(os.environ.get("HARNESS_LIVE_CHECK_STATE")
                         or КОРЕНЬ / "harness" / "config" / "live-check-state.yaml")
АРТЕФАКТЫ = pathlib.Path(os.environ.get("HARNESS_LIVE_CHECK_SKILL_ARTIFACTS")
                         or КОРЕНЬ / "harness" / "config" / "skill-artifacts.yaml")
КАРТА = pathlib.Path(os.environ.get("HARNESS_LIVE_CHECK_MAP")
                     or КОРЕНЬ / "dev-map.yaml")
СЛЕДЫ = pathlib.Path(os.environ.get("HARNESS_LIVE_CHECK_SKILL_TRACE_DIR")
                     or КОРЕНЬ / ".tasks" / "hr.zhivaya-proverka-vseh-mehanizmov"
                     / "живьём-скилы")


def _журнал() -> pathlib.Path:
    """Журнал вызовов скилов. Своя переменная старше конфига: без неё проба
    читала бы БОЕВОЙ журнал и зеленела от чужой работы."""
    свой = os.environ.get("HARNESS_LIVE_CHECK_SKILL_LOG")
    if свой:
        return pathlib.Path(свой)
    sys.path.insert(0, str(КОРЕНЬ / "scripts" / "lib"))
    import config as конф                                 # noqa: PLC0415
    return pathlib.Path(конф.log_dir()) / "skills.jsonl"


def записи_журнала() -> list[dict]:
    """Машинные записи журнала. Битая строка пропускается, а не роняет разбор."""
    итог = []
    try:
        строки = _журнал().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return итог
    for строка in строки:
        строка = строка.strip()
        if not строка:
            continue
        try:
            итог.append(json.loads(строка))
        except ValueError:
            continue
    return итог


def начало_задачи(ид: str) -> str:
    """Дата начала задачи из карты, либо «» — тогда о времени не судим вслух."""
    try:
        данные = yaml.safe_load(КАРТА.read_text(encoding="utf-8")) or {}
    except OSError:
        return ""

    def обход(узел):
        if isinstance(узел, dict):
            if узел.get("id") == ид:
                yield узел
            for значение in узел.values():
                yield from обход(значение)
        elif isinstance(узел, list):
            for элемент in узел:
                yield from обход(элемент)

    for карточка in обход(данные):
        когда = (карточка.get("when") or {}).get("started")
        if когда:
            return str(когда)
    return ""


def данные_артефактов() -> dict:
    try:
        данные = yaml.safe_load(АРТЕФАКТЫ.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    return данные.get("скилы") or {}


def _день(ts: str) -> str:
    """Дата из ts записи журнала: сравниваем ДНЯМИ, как карта их и пишет."""
    return str(ts)[:10]


def имя_скила(довод: str) -> str:
    """Имя скила из довода: принимаем и имя, и путь механизма из реестра."""
    if "/" not in довод:
        return довод
    части = pathlib.Path(довод).parts
    return части[-2] if len(части) >= 2 else довод


def ключ_записи(скил: str) -> str:
    """Ключ состояния — ПУТЬ механизма, как он назван в реестре. По имени
    запись никем не считается: гейт говорит «в состоянии есть механизмы,
    которых нет в реестре» (поймано живым прогоном 17.10.2026)."""
    return f"harness/skills/{скил}/SKILL.md"


def судить(скил: str, задача: str) -> tuple[bool, str, dict]:
    """(доказано, что сказать, поля записи) для одного скила."""
    правило = данные_артефактов().get(скил)
    if not правило or not str(правило.get("артефакт") or "").strip():
        return False, (f"{скил}: артефакт скила не назван в данных "
                       f"({АРТЕФАКТЫ.name}) — судить предикат класса E нечем; "
                       f"скил без артефакта объявляется маской «-» и причиной"), {}
    # «{задача}» в маске — подстановка id: артефакт ревью и журнал помощников
    # лежат в каталоге ИМЕННО этой задачи, и без подстановки glob искал бы
    # каталог с фигурными скобками в имени (поймано живым прогоном 17.10.2026:
    # три скила из семи отказали на обещании, которого код не исполнял).
    маска = str(правило["артефакт"]).strip().replace("{задача}", задача)
    начата = начало_задачи(задача)
    свои = [з for з in записи_журнала()
            if з.get("skill") == скил and з.get("task") == задача
            and з.get("источник") == "хук"
            and з.get("успех") in (True, "да", 1)]
    if not свои:
        return False, (f"{скил}: вызова в этой задаче нет — в журнале скилов не "
                       f"найдено машинной записи («источник»:«хук», «успех»:true) "
                       f"с task={задача}"), {}
    поздние = [з for з in свои if not начата or _день(з.get("ts", "")) >= начата]
    if not поздние:
        return False, (f"{скил}: все вызовы раньше начала задачи — запись "
                       f"{_день(свои[-1].get('ts', '?'))}, задача начата {начата}: "
                       f"чужая давняя работа предикат класса E не закрывает"), {}
    запись_журнала = поздние[-1]

    найденный = ""
    if маска != "-":
        файлы = sorted(КОРЕНЬ.glob(маска))
        if not файлы:
            return False, (f"{скил}: артефакта по маске «{маска}» нет — скил "
                           f"обязан оставить файл ({правило.get('почему') or ''})"), {}
        свежие = [ф for ф in файлы if not начата or datetime.date.fromtimestamp(
            ф.stat().st_mtime).isoformat() >= начата]
        if not свежие:
            return False, (f"{скил}: артефакт по маске «{маска}» старше начала "
                           f"задачи ({начата}) — в ЭТОЙ задаче скил не работал"), {}
        найденный = str(свежие[-1].relative_to(КОРЕНЬ))

    СЛЕДЫ.mkdir(parents=True, exist_ok=True)
    след = СЛЕДЫ / (скил + ".log")
    строка = json.dumps(запись_журнала, ensure_ascii=False)
    когда = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    след.write_text(
        f"время: {когда}\n"
        f"механизм: {скил}\n"
        f"задача: {задача} (начата {начата or 'дата не названа в карте'})\n"
        f"артефакт: {найденный or 'маска «-»: скил файла не создаёт'}\n"
        f"--- строка журнала скилов, написанная хуком ---\n{строка}\n",
        encoding="utf-8")

    путь_следа = str(след)
    if след.is_relative_to(КОРЕНЬ):
        путь_следа = str(след.relative_to(КОРЕНЬ))
    поля = {
        "живьём": (f"позван В ЭТОЙ ЗАДАЧЕ ({задача}): машинная запись журнала "
                   f"скилов от {запись_журнала.get('ts')}, артефакт — "
                   + (f"{найденный} (маска «{маска}»)" if найденный
                      else f"маска «-»: {правило.get('почему') or 'файла не создаёт'}")),
        "увидели": строка,
        "след": путь_следа,
        "когда": datetime.date.today().isoformat(),
        "исход": "ок",
    }
    return True, f"{скил}: вызов в этой задаче доказан — {строка[:90]}", поля


def скилы_класса() -> list[str]:
    """Механизмы класса E из данных классов — имя скила это имя его каталога."""
    путь = pathlib.Path(os.environ.get("HARNESS_LIVE_CHECK_CLASSES")
                        or КОРЕНЬ / "harness" / "config" / "live-check-classes.yaml")
    данные = yaml.safe_load(путь.read_text(encoding="utf-8")) or {}
    итог = []
    for файл, запись in (данные.get("классы") or {}).items():
        класс = запись.get("класс") if isinstance(запись, dict) else запись
        if класс == "E":
            итог.append(файл)
    return sorted(итог)


def записать(поля_по_скилу: dict) -> None:
    данные = yaml.safe_load(СОСТОЯНИЕ.read_text(encoding="utf-8")) or {}
    сост = данные.setdefault("проверено", {})
    for скил, поля in поля_по_скилу.items():
        сост.setdefault(скил, {}).update(поля)
    СОСТОЯНИЕ.write_text(
        yaml.dump(данные, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8")


def main() -> int:
    доводы = sys.argv[1:]
    задача = ""
    if "--задача" in доводы:
        место = доводы.index("--задача")
        задача = доводы[место + 1] if место + 1 < len(доводы) else ""
        доводы = доводы[:место] + доводы[место + 2:]
    писать = "--записать" in доводы
    имена = [д for д in доводы if not д.startswith("--")]
    кого = скилы_класса() if "--всё" in доводы else имена
    if not задача:
        print("[живая проверка скилов] нечего судить: нужен «--задача <id>» — "
              "предикат класса E про вызов В ЭТОЙ задаче", file=sys.stderr)
        return 1
    if not кого:
        print("[живая проверка скилов] нечего судить: назовите скил или «--всё»",
              file=sys.stderr)
        return 1

    поля_по_скилу: dict = {}
    беды: list[str] = []
    for довод in кого:
        скил = имя_скила(довод)
        доказано, слово, поля = судить(скил, задача)
        print(("  ок    " if доказано else "  ПЛОХО ") + слово)
        if доказано:
            поля_по_скилу[ключ_записи(скил)] = поля
        else:
            беды.append(слово)

    if писать and поля_по_скилу:
        записать(поля_по_скилу)
        print(f"[живая проверка скилов] записано скилов: {len(поля_по_скилу)}")
    if беды:
        print(f"[живая проверка скилов] ✗ не доказано: {len(беды)} из {len(кого)}",
              file=sys.stderr)
        for б in беды:
            print(f"    {б}", file=sys.stderr)
        return 1
    print(f"[живая проверка скилов] доказано все {len(кого)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
