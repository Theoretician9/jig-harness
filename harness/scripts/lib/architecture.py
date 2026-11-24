"""Чтение реестра архитектуры — ОДНО место на всех, кто его разбирает.

Реестр `harness/config/architecture.yaml` — данные. Сторож архитектуры, перехват,
ведение карты и гейт пайплайна берут части и элементы отсюда: свой разбор файла у
любого из них был бы вторым судьёй одного факта (`scripts/check-one-judge.py`).
Имя файла реестра встречается только здесь.
"""
import os
from pathlib import Path

import yaml

import config
from config import корень_дерева

# Ключи ARCHITECTURE_* и их умолчания: один судья и для сторожа, и для перехвата.
DEFAULTS = {"ARCHITECTURE_LINES": "harness", "ARCHITECTURE_STRICT": "0",
            "ARCHITECTURE_GRACE_HOURS": "24"}

class RegistryError(Exception):
    """Реестра нет или он не разбирается; в тексте — путь."""


def registry_path() -> Path:
    # Дверь HARNESS_ARCHITECTURE нужна пробам: стенд подаёт свой реестр.
    дверь = os.environ.get("HARNESS_ARCHITECTURE")
    if дверь:
        return Path(дверь)
    return корень_дерева(__file__) / "harness" / "config" / "architecture.yaml"


def load_yaml_fast(path) -> object:
    """YAML через C-разборщик: mechanisms и карта по ~800 КБ, чистый Python
    разбирает их секундами, а ворота обязаны уложиться в пять."""
    with open(path, encoding="utf-8") as файл:
        return yaml.load(файл, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))


def load(path=None) -> dict:
    путь = Path(path) if path else registry_path()
    try:
        данные = load_yaml_fast(путь)
    except (OSError, yaml.YAMLError) as ошибка:
        raise RegistryError(f"реестр {путь}: {ошибка}") from ошибка
    if not isinstance(данные, dict):
        raise RegistryError(f"реестр {путь}: корень не словарь")
    return данные


def element_ids(registry: dict) -> set:
    """id частей и элементов: сама часть тоже законное место."""
    найденные = []
    for часть in registry.get("части") or []:
        найденные.append(часть.get("id"))
        for элемент in часть.get("элементы") or []:
            найденные.append(элемент.get("id"))
    # Только строки: id-словарь в битом реестре не должен ронять set().
    return {i for i in найденные if isinstance(i, str)}


def part_of(element: str) -> str:
    # Части отдельным полем нет: два поля разошлись бы. Часть — префикс до точки.
    return element.split(".", 1)[0]


def has_element(registry: dict, value) -> bool:
    # Список или словарь — не место: «ровно одно» держит тип значения.
    return isinstance(value, str) and value in element_ids(registry)


def element_menu(registry: dict) -> str:
    """Меню для модели: строка «id — имя: что» на часть (что — обязанность) и элемент."""
    строки = []
    for часть in registry.get("части") or []:
        строки.append(f"{часть.get('id')} — {часть.get('имя')}: {часть.get('обязанность')}")
        for элемент in часть.get("элементы") or []:
            строки.append(f"{элемент.get('id')} — {элемент.get('имя')}: {элемент.get('что')}")
    return "\n".join(строки)


NEW_ELEMENT_PREFIX = "новый:"


def resolve_element(registry: dict, raw) -> tuple:
    """(элемент, новый_элемент) по слову модели; (None, None) — места нет.

    Слово модели — не факт: id сверяется с реестром, «новый: <часть>: <имя>» —
    только при части из реестра."""
    if has_element(registry, raw):
        return raw, None
    if not isinstance(raw, str) or not raw.startswith(NEW_ELEMENT_PREFIX):
        return None, None
    часть, _, имя = raw[len(NEW_ELEMENT_PREFIX):].partition(":")
    часть, имя = часть.strip(), имя.strip()
    if имя and часть in element_ids(registry) and part_of(часть) == часть:
        return часть, имя
    return None, None


def judged_lines(conf: dict) -> set:
    """Линии карты, которые судит архитектура: ключ через пробел, без него — harness."""
    return set((conf.get("ARCHITECTURE_LINES") or DEFAULTS["ARCHITECTURE_LINES"]).split())


def tree_conf() -> dict:
    return config.из_файла(str(корень_дерева(__file__) / "harness" / "config" / "harness.conf"))


def machine_conf() -> dict:
    return config.из_файла(config.путь_harness_conf())


def setting(key: str, machine: dict, tree: dict) -> tuple:
    """Значение и откуда оно: машина, потом дерево, потом умолчание."""
    if machine.get(key):
        return machine[key], "машинный конфиг"
    if tree.get(key):
        return tree[key], "конфиг дерева"
    return DEFAULTS[key], "умолчание"


def settings() -> dict:
    # Ключи строго из файлов (config.из_файла): окружение не хозяин, иначе
    # переменная снимает строгость и храповик. Пробы задают ключи файлом.
    machine, tree = machine_conf(), tree_conf()
    values = {k: setting(k, machine, tree) for k in DEFAULTS}
    for key, (_, откуда) in values.items():
        if откуда == "умолчание":
            print(f"настройка {key} нигде не задана — умолчание {DEFAULTS[key]}")
    строго = values["ARCHITECTURE_STRICT"][0].strip().lower() in ("1", "true", "yes")
    срок = values["ARCHITECTURE_GRACE_HOURS"][0]
    if not срок.isdigit():
        print(f"ARCHITECTURE_GRACE_HOURS={срок} не число — умолчание "
              f"{DEFAULTS['ARCHITECTURE_GRACE_HOURS']}")
    return {"линии": judged_lines({k: v[0] for k, v in values.items()}),
            "строго": строго,
            "срок_ч": int(срок) if срок.isdigit() else int(DEFAULTS["ARCHITECTURE_GRACE_HOURS"])}
