#!/usr/bin/env python3
"""Сторож архитектуры: у каждого механизма и каждой карточки — своё место.

Владелец 20.11.2026: харнес делится на части, и всякая работа либо создаёт новый
элемент, либо относится к существующему. Реестр частей — данные
(`harness/config/architecture.yaml`), его разбор — `scripts/lib/architecture.py`.
Сторож судит три предмета:
  --реестр     форма реестра, контракты частей, политики, читатели реестра;
  --механизмы  значение поля `элемент` в `harness/config/mechanisms.yaml`;
  --карта      значение и срок поля `элемент` у карточек карты.
Присутствие поля `элемент` у механизма судит `check-registry.py` по паспорту
(один судья факта); здесь оно только считается.
Без режима идут все три; код — максимум из трёх. `--selftest` зовёт пробу
`scripts/test_check_architecture.py` рядом.

Двери (для проб): HARNESS_ARCHITECTURE, HARNESS_USTROJSTVO, HARNESS_DEVMAP,
HARNESS_PIPELINE_CONF, HARNESS_GATES_SH, HARNESS_ARCHITECTURE_READERS,
HARNESS_CONF, ARCHITECTURE_NOW (часы, эпоха).
Код возврата: 0 — чисто, 1 — есть претензия, 2 — неверный вызов.
"""
import datetime
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ЗДЕСЬ = Path(__file__).resolve().parent
sys.path.insert(0, str(ЗДЕСЬ / "lib"))
import architecture  # noqa: E402
import config  # noqa: E402

ДЕРЕВО = config.корень_дерева(__file__)
ЧИТАТЕЛИ = ("map-from-message.py", "check-pipeline.py")
ЗВЁЗДОЧКА = "*"
ВНЕШНИЙ = "внешний:"
КЛЮЧ_ПОТОЛКА = "ARCHITECTURE_PART_LEVEL_CEILING"
СТАТУСЫ_КАРТЫ = {"plan", "wip", "test", "беклог", "принято"}
ПОКАЗАТЬ_ID = 10
ID_ЧАСТИ = re.compile(r"[a-z-]+")
ID_ЭЛЕМЕНТА = re.compile(r"[a-z-]+\.[a-z0-9-]+")
СТРОКА_ПРОВЕРКИ = re.compile(r'^\s*проверка\s+"([^"]+)"', re.M)


# ─── настройки ──────────────────────────────────────────────────────────────
# Читает библиотека: тот же судья ключей ARCHITECTURE_* нужен перехвату.
конфиг_дерева = architecture.tree_conf
машинный_конфиг = architecture.machine_conf
настройки = architecture.settings


def цифры(сырое) -> int:
    return int(сырое) if isinstance(сырое, str) and сырое.isdigit() else 0


def потолок_master():
    """Потолок в коммите master дерева; None — master или ключа в нём нет."""
    результат = subprocess.run(
        ["git", "-C", str(ДЕРЕВО), "show", "master:harness/config/harness.conf"],
        capture_output=True, text=True, timeout=30)
    if результат.returncode != 0:
        return None
    значения = config.из_текста(результат.stdout)
    return цифры(значения[КЛЮЧ_ПОТОЛКА]) if КЛЮЧ_ПОТОЛКА in значения else None


def потолок_частей() -> tuple:
    """Потолок и претензии храповика. Значение СТРОГО из файла (машинный, потом
    дерево). Откат на дерево открыт правке одним коммитом, поэтому дерево не
    может быть выше, чем в master: потолок опускается, а не поднимается."""
    дерево, претензии = конфиг_дерева(), []
    база = потолок_master()
    if КЛЮЧ_ПОТОЛКА in дерево and база is not None and цифры(дерево[КЛЮЧ_ПОТОЛКА]) > база:
        претензии.append(f"{КЛЮЧ_ПОТОЛКА} в дереве {цифры(дерево[КЛЮЧ_ПОТОЛКА])} выше, "
                         f"чем в master {база} — потолок опускается, а не поднимается")
    for значения in (машинный_конфиг(), дерево):
        if КЛЮЧ_ПОТОЛКА in значения:
            return цифры(значения[КЛЮЧ_ПОТОЛКА]), претензии
    return 0, претензии


# ─── пути (двери) ───────────────────────────────────────────────────────────
def путь(дверь: str, относительный: str) -> Path:
    return Path(os.environ.get(дверь) or ДЕРЕВО / относительный)


ИМЯ_РЕЕСТРА = architecture.registry_path().name


def читатели_корень() -> Path:
    return Path(os.environ.get("HARNESS_ARCHITECTURE_READERS") or ДЕРЕВО)


# ─── реестр ─────────────────────────────────────────────────────────────────
def части_реестра(реестр: dict) -> list:
    return [ч for ч in реестр.get("части") or [] if isinstance(ч, dict)]


def претензии_частей(части: list) -> list:
    """Правила 1–2 формы: id частей и элементов, уникальность, префикс."""
    итог, видели = [], set()
    if not части:
        return ["реестр: нет ни одной части"]
    for часть in части:
        id_ = часть.get("id")
        if not isinstance(id_, str) or not ID_ЧАСТИ.fullmatch(id_):
            итог.append(f"часть {id_!r}: id не [a-z-]+")
            continue
        if id_ in видели:
            итог.append(f"часть {id_}: id повторён")
        видели.add(id_)
        for элемент in часть.get("элементы") or []:
            итог += претензии_элемента(id_, элемент, видели)
    return итог


def претензии_элемента(часть: str, элемент, видели: set) -> list:
    id_ = элемент.get("id") if isinstance(элемент, dict) else None
    if not isinstance(id_, str) or not ID_ЭЛЕМЕНТА.fullmatch(id_):
        return [f"элемент {id_!r} части {часть}: id не <часть>.<имя>"]
    if architecture.part_of(id_) != часть:
        return [f"элемент {id_}: префикс не совпадает с частью {часть}"]
    if id_ in видели:
        return [f"элемент {id_}: id повторён"]
    видели.add(id_)
    return []


def запись_контракта(запись, ключ: str) -> str:
    return запись.get(ключ) if isinstance(запись, dict) else None


def известна_ли_сторона(сторона, ids: set) -> bool:
    return (isinstance(сторона, str)
            and (сторона == ЗВЁЗДОЧКА or сторона.startswith(ВНЕШНИЙ) or сторона in ids))


def претензии_контрактов(части: list) -> tuple:
    """Правило 3: пары с двух сторон. Звёздочка входа даёт пару любому выходу к
    части; звёздочка выхода — любому входу от неё."""
    ids = {ч["id"] for ч in части if isinstance(ч.get("id"), str)}
    выходы = {(ч["id"], запись_контракта(з, "кому")) for ч in части if ч.get("id") in ids
              for з in ч.get("выходы") or []}
    входы = {(ч["id"], запись_контракта(з, "от")) for ч in части if ч.get("id") in ids
             for з in ч.get("входы") or []}
    итог = [f"часть {часть}: {что} {сторона!r} — такой части нет"
            for пары, что in ((выходы, "выход к"), (входы, "вход от"))
            for часть, сторона in sorted(пары, key=str) if not известна_ли_сторона(сторона, ids)]
    for откуда, куда in sorted(выходы, key=str):
        итог += парный_вход(откуда, куда, входы, ids)
    for куда, откуда in sorted(входы, key=str):
        итог += парный_выход(куда, откуда, выходы, ids)
    return итог, len(выходы)


def парный_вход(откуда: str, куда: str, входы: set, ids: set) -> list:
    if куда not in ids and куда != ЗВЁЗДОЧКА:
        return []
    есть = ((куда, откуда) in входы or (куда, ЗВЁЗДОЧКА) in входы if куда != ЗВЁЗДОЧКА
            else any(от == откуда and ч != откуда for ч, от in входы))
    return [] if есть else [f"выход {откуда} → {куда} без парного входа"]


def парный_выход(куда: str, откуда: str, выходы: set, ids: set) -> list:
    if откуда not in ids and откуда != ЗВЁЗДОЧКА:
        return []
    if откуда == ЗВЁЗДОЧКА:
        есть = any(к in (куда, ЗВЁЗДОЧКА) and ч != куда for ч, к in выходы)
    else:
        есть = (откуда, куда) in выходы or (откуда, ЗВЁЗДОЧКА) in выходы
    return [] if есть else [f"вход {куда} ← {откуда} без парного выхода"]


def шаги_пайплайна() -> dict:
    шаги = architecture.load_yaml_fast(путь("HARNESS_PIPELINE_CONF", "harness/config/pipeline.yaml"))
    return {к: list(з or []) for к, з in (шаги or {}).get("шаги", {}).items()}


def имена_проверок() -> set:
    return set(СТРОКА_ПРОВЕРКИ.findall(
        путь("HARNESS_GATES_SH", "scripts/gates.sh").read_text(encoding="utf-8")))


def претензии_политики(части: list, уровни: dict, проверки: set) -> list:
    """Правила 4–5: уровень и шаги из пайплайна, проверки из gates.sh."""
    итог = []
    все_шаги = {ш for шаги in уровни.values() for ш in шаги}
    for часть in части:
        политика = часть.get("политика") or {}
        id_ = часть.get("id")
        пол = политика.get("уровень_не_ниже")
        if пол is not None and пол not in уровни:
            итог.append(f"часть {id_}: уровень_не_ниже {пол!r} нет в pipeline.yaml")
        for шаг in политика.get("шаги_добавить") or []:
            if шаг not in все_шаги:
                итог.append(f"часть {id_}: шаг {шаг!r} нет в pipeline.yaml")
            elif шаг in уровни.get("мелкая", []):
                итог.append(f"часть {id_}: шаг {шаг!r} уже в уровне мелкая — политика ничего не меняет")
        итог += [f"часть {id_}: проверки {имя!r} нет в gates.sh"
                 for имя in политика.get("проверки") or [] if имя not in проверки]
    return итог


def претензии_читателей() -> tuple:
    """Читатели не открывают реестр сами: второй разбор = второй судья."""
    корень, итог, найдено = читатели_корень(), [], 0
    for имя in ЧИТАТЕЛИ:
        файл = корень / "scripts" / имя
        if not файл.is_file():
            итог.append(f"читателя нет: {имя}")
            continue
        найдено += 1
        if ИМЯ_РЕЕСТРА in файл.read_text(encoding="utf-8", errors="replace"):
            итог.append(f"читатель {имя} открывает {ИМЯ_РЕЕСТРА} мимо библиотеки")
    return итог, найдено


def проверить_реестр() -> int:
    реестр = architecture.load()
    части = части_реестра(реестр)
    претензии, контрактов = претензии_частей(части), 0
    if части:
        контракты, контрактов = претензии_контрактов(части)
        претензии += контракты
        претензии += претензии_политики(части, шаги_пайплайна(), имена_проверок())
    читатели, _ = претензии_читателей()
    претензии += читатели
    элементов = len(architecture.element_ids(реестр)) - len(
        {ч["id"] for ч in части if isinstance(ч.get("id"), str)})
    for п in претензии:
        print(f"ПРЕТЕНЗИЯ {п}")
    print(f"частей {len(части)}, элементов {элементов}, контрактов {контрактов}, "
          f"претензий {len(претензии)}")
    return 1 if претензии else 0


# ─── механизмы ──────────────────────────────────────────────────────────────
def проверить_механизмы() -> int:
    реестр = architecture.load()
    ids = architecture.element_ids(реестр)
    части = {ч["id"] for ч in части_реестра(реестр) if isinstance(ч.get("id"), str)}
    записи = (architecture.load_yaml_fast(путь("HARNESS_USTROJSTVO", "harness/config/mechanisms.yaml"))
              or {}).get("механизмы") or []
    претензии = [] if записи else ["механизмы: ключа «механизмы» нет или список пуст"]
    без, вне, не_строкой, на_части = 0, [], [], 0
    for запись in записи:
        значение = запись.get("элемент")
        if значение is None:
            без += 1
        elif not isinstance(значение, str):
            не_строкой.append(запись.get("файл"))
        elif значение not in ids:
            вне.append(f"{запись.get('файл')} → {значение}")
        elif значение in части:
            на_части += 1
    потолок, претензии_потолка = потолок_частей()
    претензии += претензии_потолка
    претензии += [f"вне реестра: {п}" for п in вне] + [f"элемент не строкой: {ф}" for ф in не_строкой]
    if на_части > потолок:
        претензии.append(f"механизмов на уровне части {на_части} при потолке {потолок} — "
                         f"потолок опускается, а не поднимается")
    for п in претензии:
        print(f"ПРЕТЕНЗИЯ {п}")
    print(f"механизмов {len(записи)}, без элемента {без}, вне реестра {len(вне)}, "
          f"не строкой {len(не_строкой)}, на уровне части {на_части} ≤ {потолок}")
    return 1 if претензии else 0


# ─── карта ──────────────────────────────────────────────────────────────────
def сейчас() -> float:
    return float(os.environ.get("ARCHITECTURE_NOW") or time.time())


def возраст_часов(заведено) -> float:
    """Часов от `заведено`; даты нет или она не читается — карточка старая:
    молодость надо доказать, а не предположить."""
    if isinstance(заведено, datetime.datetime):
        начало = заведено
    elif isinstance(заведено, datetime.date):
        начало = datetime.datetime.combine(заведено, datetime.time())
    else:
        try:
            начало = datetime.datetime.fromisoformat(str(заведено))
        except ValueError:
            return float("inf")
    часов = (сейчас() - начало.timestamp()) / 3600
    return часов if часов >= 0 else float("inf")    # дата в будущем — не молодость


def карточки_линий(карта: dict, линии: set) -> list:
    return [к for л in карта.get("lines") or [] if л.get("id") in линии
            for к in л.get("tasks") or [] if к.get("status") in СТАТУСЫ_КАРТЫ]


def разобрать_карточки(карточки: list, реестр: dict, срок_ч: int) -> dict:
    итог = {"вне": [], "без_старых": [], "ждут": [], "обещан": []}
    for к in карточки:
        if к.get("элемент") is None:
            ключ = "без_старых" if возраст_часов(к.get("заведено")) > срок_ч else "ждут"
            итог[ключ].append(к.get("id"))
        elif not architecture.has_element(реестр, к["элемент"]):
            итог["вне"].append(к.get("id"))
        if к.get("status") == "test" and к.get("новый_элемент"):
            итог["обещан"].append(к.get("id"))
    return итог


def неизвестные_линии(карта: dict, линии: set) -> list:
    """Опечатка в ARCHITECTURE_LINES судила бы ноль карточек — и молчала."""
    есть = {л.get("id") for л in карта.get("lines") or []}
    return sorted(линии - есть)


def показать(ид: list) -> str:
    return ", ".join(ид[:ПОКАЗАТЬ_ID]) + ("…" if len(ид) > ПОКАЗАТЬ_ID else "")


def проверить_карту() -> int:
    настр = настройки()
    реестр = architecture.load()
    карта = architecture.load_yaml_fast(путь("HARNESS_DEVMAP", "dev-map.yaml")) or {}
    карточки = карточки_линий(карта, настр["линии"])
    р = разобрать_карточки(карточки, реестр, настр["срок_ч"])
    строго = настр["строго"]
    для_срока = [("без элемента старше срока", р["без_старых"]), ("test с новым элементом", р["обещан"])]
    # Вне реестра красно всегда; срок и обещанный элемент — только при строгости.
    красные = [("вне реестра", р["вне"]),
               ("линия вне карты", неизвестные_линии(карта, настр["линии"]))
               ] + (для_срока if строго else [])
    for название, ид in красные:
        if ид:
            print(f"ПРЕТЕНЗИЯ {название}: {показать(ид)}")
    if not строго:
        for название, ид in для_срока:
            if ид:
                print(f"предупреждение {название}: {показать(ид)}")
    print(f"без места, ждёт ведения {len(р['ждут'])}")
    print(f"карточек {len(карточки)}, без элемента {len(р['без_старых'])}, "
          f"вне реестра {len(р['вне'])}, ждёт ведения {len(р['ждут'])}, "
          f"test с новым элементом {len(р['обещан'])}")
    return 1 if any(ид for _, ид in красные) else 0


# ─── самотест и вход ────────────────────────────────────────────────────────
def самотест() -> int:
    проба = ЗДЕСЬ / "test_check_architecture.py"
    if not проба.is_file():
        print(f"нет пробы: {проба}")
        return 1
    окружение = dict(os.environ, CHECK_ARCHITECTURE_SRC=str(Path(__file__).resolve()))
    return subprocess.run([sys.executable, str(проба)], env=окружение).returncode


РЕЖИМЫ = {"--реестр": проверить_реестр, "--механизмы": проверить_механизмы,
          "--карта": проверить_карту, "--selftest": самотест}


def main(доводы: list) -> int:
    if not доводы:
        return max(режим() for режим in (проверить_реестр, проверить_механизмы, проверить_карту))
    if len(доводы) != 1 or доводы[0] not in РЕЖИМЫ:
        print(__doc__)
        return 2
    return РЕЖИМЫ[доводы[0]]()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except architecture.RegistryError as ошибка:
        print(f"ПРЕТЕНЗИЯ {ошибка}")
        sys.exit(1)
