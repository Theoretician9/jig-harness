#!/usr/bin/env python3
"""Команды бота: кнопки в меню Telegram ставит КОД по данным.

Владелец 13.09.2026: «надо сделать вызов панели кнопкой прям в меню бота,
сейчас кстати перестало работать». Меню было пустым — `getMyCommands` отдавал
`[]`, — а команды приходилось угадывать словом и латиницей: слово «панель»
уходило в чат как обычное сообщение.

Два решения:

* **Список — ДАННЫЕ** (`harness/config/команды-бота.yaml`). Добавить кнопку —
  строка в файле, а не правка бота.
* **Сверка, а не надежда.** `--проверить` спрашивает Telegram, что у него
  стоит, и сравнивает с данными. Поставить один раз и надеяться нельзя: меню
  живёт на стороне Telegram, его может затереть другой клиент или сброс бота.

Прогон:
    python3 scripts/komandy-bota.py --поставить
    python3 scripts/komandy-bota.py --проверить   # гейт: 0 — совпадает
    python3 scripts/komandy-bota.py --показать
    python3 scripts/komandy-bota.py --selftest
Код возврата: 0 — совпадает/поставлено, 1 — расхождение, 2 — спросить нечем.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

КОРЕНЬ = Path(__file__).resolve().parent.parent
ДАННЫЕ = КОРЕНЬ / "harness" / "config" / "команды-бота.yaml"
API = os.environ.get("TG_API_BASE", "https://api.telegram.org")


class Отказ(Exception):
    """Спросить нечем: нет данных, нет токена, сеть молчит."""


def данные(путь=ДАННЫЕ) -> list:
    try:
        дано = yaml.safe_load(Path(путь).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as беда:
        raise Отказ(f"данные команд не прочитаны ({путь}): {беда}") from беда
    команды = (дано or {}).get("команды") or []
    if not команды:
        raise Отказ(f"в {путь} нет ни одной команды")
    return команды


def токен() -> str:
    """Токен бота — из секретов вне репозитория (И-3)."""
    из_окружения = os.environ.get("TG_TOKEN_FILE")
    путь = Path(из_окружения) if из_окружения else None
    if путь is None:
        каталог = Path(os.environ.get("SECRETS_DIR", "/var/lib/harness/secrets"))
        путь = каталог / "tg_bot_token"
    try:
        return путь.read_text(encoding="utf-8").strip()
    except OSError as беда:
        raise Отказ(f"токен бота не прочитан ({путь}): {беда}") from беда


def _позвать(метод: str, тело: dict | None = None) -> dict:
    адрес = f"{API}/bot{токен()}/{метод}"
    данные_запроса = None
    if тело is not None:
        данные_запроса = json.dumps(тело, ensure_ascii=False).encode("utf-8")
    запрос = urllib.request.Request(
        адрес, data=данные_запроса,
        headers={"Content-Type": "application/json"} if тело else {})
    try:
        with urllib.request.urlopen(запрос, timeout=20) as ответ:
            return json.loads(ответ.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as беда:
        raise Отказ(f"Telegram не ответил на {метод}: {беда}") from беда


def в_телеграме() -> list:
    ответ = _позвать("getMyCommands")
    if not ответ.get("ok"):
        raise Отказ(f"getMyCommands отказал: {ответ}")
    return ответ.get("result") or []


def как_надо(команды: list) -> list:
    return [{"command": str(к["имя"]), "description": str(к["описание"])}
            for к in команды]


def расхождения(команды: list, стоит: list) -> list:
    надо = как_надо(команды)
    если_надо = {к["command"]: к["description"] for к in надо}
    если_стоит = {к.get("command"): k.get("description")
                  for к in стоит for k in [к]}
    беды = []
    for имя, описание in если_надо.items():
        if имя not in если_стоит:
            беды.append(f"/{имя}: кнопки нет в меню бота")
        elif если_стоит[имя] != описание:
            беды.append(f"/{имя}: подпись «{если_стоит[имя]}» вместо «{описание}»")
    for имя in если_стоит:
        if имя not in если_надо:
            беды.append(f"/{имя}: лишняя кнопка, её нет в данных")
    return беды


def поставить(команды: list) -> dict:
    ответ = _позвать("setMyCommands", {"commands": как_надо(команды)})
    if not ответ.get("ok"):
        raise Отказ(f"setMyCommands отказал: {ответ}")
    return ответ


def слова_команд(команды: list) -> dict:
    """Слово владельца → имя команды. Кнопка и слово ведут в одно место."""
    итог = {}
    for к in команды:
        итог[str(к["имя"]).lower()] = str(к["имя"])
        for слово in к.get("слова") or []:
            итог[str(слово).lower()] = str(к["имя"])
    return итог


def начала(к: dict) -> list:
    """Все формы вызова одной команды: имя и слова владельца."""
    return [str(к["имя"]).lower()] + [str(с).lower() for с in к.get("слова") or []]


def разобрать(команды: list, слово: str) -> tuple:
    """Сообщение владельца → (имя команды, аргумент). Не команда → ("", "").

    Точное совпадение решает всё, кроме одного случая: команда принимает
    АРГУМЕНТ («панель наружу my.example.com»). Тогда сообщение делится на
    начало-команду и хвост, и хвост обязан целиком совпасть с формой
    аргумента из данных. Разбор «по префиксу» без этого условия превратил бы
    в команду и рассказ «панель упала, посмотри».
    """
    слово = слово.strip().lower().lstrip("/")
    прямо = слова_команд(команды).get(слово)
    if прямо:
        return прямо, ""
    for к in команды:
        форма = к.get("аргумент")
        if not форма:
            continue
        for начало in начала(к):
            if not слово.startswith(начало + " "):
                continue
            хвост = слово[len(начало):].strip()
            if re.fullmatch(str(форма), хвост):
                return str(к["имя"]), хвост
    return "", ""


def _selftest() -> int:
    проба = КОРЕНЬ / "scripts" / "test_komandy_bota.py"
    if not проба.exists():
        print(f"[команды бота] пробы нет: {проба}")
        return 1
    return subprocess.run([sys.executable, str(проба)], timeout=300).returncode


def главная(argv: list) -> int:
    if "--selftest" in argv:
        return _selftest()
    try:
        команды = данные()
        if "--какая" in argv:
            # Слово владельца → имя команды. Зовёт диспетчер на каждое
            # сообщение: список форм живёт в данных, и повторять его в коде
            # бота значит завести вторую правду (правило «у факта один судья»).
            имя, аргумент = разобрать(команды, argv[argv.index("--какая") + 1])
            # Две строки: имя команды и её аргумент. Форма аргумента живёт в
            # данных — диспетчер второй раз её не описывает (у факта один судья).
            print(имя)
            print(аргумент)
            return 0 if имя else 1
        if "--показать" in argv:
            for к in как_надо(команды):
                print(f"/{к['command']} — {к['description']}")
            return 0
        if "--поставить" in argv:
            поставить(команды)
            беды = расхождения(команды, в_телеграме())
            if беды:
                print("[команды бота] поставлены, но Telegram отдаёт другое:")
                for б in беды:
                    print("   ", б)
                return 1
            print(f"[команды бота] в меню {len(команды)} кнопок, все на месте")
            return 0
        беды = расхождения(команды, в_телеграме())
    except Отказ as беда:
        print(f"[команды бота] {беда}", file=sys.stderr)
        return 2
    if беды:
        print(f"[команды бота] МЕНЮ РАЗОШЛОСЬ С ДАННЫМИ: {len(беды)}")
        for б in беды:
            print("   ", б)
        print("Починка: python3 scripts/komandy-bota.py --поставить")
        return 1
    print(f"[команды бота] чисто: в меню {len(команды)} кнопок, как в данных")
    return 0


if __name__ == "__main__":
    sys.exit(главная(sys.argv[1:]))
