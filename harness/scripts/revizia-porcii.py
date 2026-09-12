#!/usr/bin/env python3
"""Какие файлы читает область ревизии в этот прогон.

Откуда взят. Замер 12.09.2026: код харнеса — 2 062 845 знаков
(`find scripts harness -type f \\( -name '*.py' -o -name '*.sh' \\) -exec wc -c {} + | tail -1`),
а порция — 300 000, то есть седьмая часть. Без памяти о прочитанном ревизия
каждый месяц читала бы одни и те же первые файлы (ревью спеки итерации 2,
C-7), а самый крупный файл (`session-warden.sh`, 184 711 знаков) без правила
«крупный идёт один и целиком» не поместился бы в порцию никогда — и не был бы
прочитан ни разу.

Порядок — по дате правки, свежие первыми: недавно правленное вероятнее сломано.

Запуск:
    revizia-porcii.py <область>              # пути порции, курсор не трогаем
    revizia-porcii.py <область> --продвинуть # то же и запомнить место
    revizia-porcii.py --области              # имена областей
    revizia-porcii.py --пороги               # строки ИМЯ=число для eval в демоне
Код возврата: 0 — напечатано, 2 — области нет.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml

ПРОЕКТ = Path(os.environ.get("PROJECT_DIR") or Path(__file__).resolve().parent.parent)
ЖУРНАЛЫ = Path(os.environ.get("LOG_DIR") or "/var/log/harness")
КОНФИГ = Path(os.environ.get("REVIZIA_CONF") or ПРОЕКТ / "harness" / "config" / "ревизия.yaml")
КУРСОР = ЖУРНАЛЫ / "revizia" / "курсор.json"

# Имена порогов для оболочки — латиницей: bash кириллические имена не берёт и
# печатает в ошибку САМО ЗНАЧЕНИЕ ([[кириллица-в-именах-bash]]).
ПОРОГИ_ПО_ЛАТЫНИ = {
    "знаков_на_порцию": "ZNAKOV_NA_PORCIYU",
    "потолок_минут": "POTOLOK_MINUT",
    "максимум_находок": "MAKSIMUM_NAHODOK",
    "максимум_знаков_карточки": "MAKSIMUM_ZNAKOV_KARTOCHKI",
    "срок_находки_суток": "SROK_NAHODKI_SUTOK",
}


def конфиг() -> dict:
    return yaml.safe_load(КОНФИГ.read_text(encoding="utf-8")) or {}


def файлы_области(имя: str, корень: Path | None = None) -> list[tuple[str, int, float]]:
    """[(путь от корня, знаков, время правки)] по маскам области.

    Знаки, а не байты: порция меряется в знаках, потому что в токены
    переводятся знаки, а кириллица весит два байта.
    """
    корень = корень or ПРОЕКТ
    область = next((о for о in конфиг().get("области") or [] if о.get("имя") == имя), None)
    if область is None:
        raise KeyError(имя)
    найдено: dict[str, tuple[str, int, float]] = {}
    for маска in область.get("пути") or []:
        for путь in sorted(корень.glob(маска)):
            if not путь.is_file():
                continue
            отн = str(путь.relative_to(корень))
            if отн in найдено:      # одна маска области перекрыла другую
                continue
            найдено[отн] = (отн, len(путь.read_text(encoding="utf-8", errors="replace")),
                            путь.stat().st_mtime)
    return list(найдено.values())


def порция(файлы: list[tuple[str, int, float]], курсор: str | None,
           потолок: int) -> tuple[list[str], str | None]:
    """(пути порции, новый курсор). Свежие первыми; курсор — где остановились.

    Курсор на файл, которого больше нет (переименован, удалён), означает
    «начать сначала»: иначе область замолчала бы навсегда.
    """
    по_свежести = [п for п, _, _ in sorted(файлы, key=lambda ф: -ф[2])]
    размер = {п: з for п, з, _ in файлы}
    начало = по_свежести.index(курсор) + 1 if курсор in по_свежести else 0

    выбрано: list[str] = []
    набрано = 0
    for путь in по_свежести[начало:]:
        # Файл крупнее потолка идёт ОДИН и целиком — иначе он не читался бы
        # никогда: разрезать его нельзя, а пропускать значит не проверять.
        if размер[путь] > потолок:
            if выбрано:
                break
            выбрано = [путь]
            набрано = размер[путь]
            break
        if набрано + размер[путь] > потолок:
            break
        выбрано.append(путь)
        набрано += размер[путь]

    if not выбрано:
        return [], None
    последний = выбрано[-1]
    исчерпан = по_свежести.index(последний) == len(по_свежести) - 1
    return выбрано, (None if исчерпан else последний)


def курсор_прочитать() -> dict:
    try:
        return json.loads(КУРСОР.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def курсор_записать(область: str, значение: str | None) -> None:
    все = курсор_прочитать()
    if значение is None:
        все.pop(область, None)
    else:
        все[область] = значение
    КУРСОР.parent.mkdir(parents=True, exist_ok=True)
    КУРСОР.write_text(json.dumps(все, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    доводы = sys.argv[1:]
    if "--области" in доводы:
        for область in конфиг().get("области") or []:
            print(область.get("имя", ""))
        return 0
    if "--пороги" in доводы:
        пороги = конфиг().get("пороги") or {}
        for ключ, имя in ПОРОГИ_ПО_ЛАТЫНИ.items():
            # Только целые: строку `eval` в демоне исполнил бы как команду.
            print(f"{имя}={int(пороги.get(ключ, 0))}")
        return 0

    имена = [д for д in доводы if not д.startswith("--")]
    if not имена:
        print("назови область: revizia-porcii.py <область>", file=sys.stderr)
        return 2
    область = имена[0]
    try:
        файлы = файлы_области(область)
    except KeyError:
        print(f"области нет: {область}", file=sys.stderr)
        return 2

    потолок = int((конфиг().get("пороги") or {}).get("знаков_на_порцию", 300000))
    выбрано, новый = порция(файлы, курсор_прочитать().get(область), потолок)
    for путь in выбрано:
        print(путь)
    if "--продвинуть" in доводы:
        курсор_записать(область, новый)
    return 0


if __name__ == "__main__":
    sys.exit(main())
