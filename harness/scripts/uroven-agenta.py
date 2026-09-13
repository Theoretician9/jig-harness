#!/usr/bin/env python3
"""Звать ли помощника на этот повод — по уровню из настроек, а не на глаз.

Владелец 12.09.2026: «Надо сделать возможность выбирать уровень когда
вызывается агенты а когда нет. И на максимальном мне бы не надо было писать
тебе „запусти агента на эту задачу“ а сам бы просто запустил и все, а на
минимальном только сам, кроме однозначно когда запускаются сабагенты, например
на ревью».

Два решения, каждое под своё правило проекта:

* **Список поводов ЗАКРЫТЫЙ.** Повода нет в данных — не зовётся нигде, включая
  максимум. «Можно всё, что не запрещено» превращает уровень в украшение: на
  максимуме я подвёл бы под него что угодно.
* **Уровень — ключ AGENT_LEVEL в harness.conf**, рядом с AUTONOMY: выбор
  владельца живёт в настройках установки, а не в коде. Незнакомое значение
  запрещает всё и называет себя — молчаливое умолчание скрыло бы опечатку.

Прогон:
    python3 scripts/uroven-agenta.py --повод ревью-кода [--уровень максимум]
    python3 scripts/uroven-agenta.py --показать    # уровень и что он разрешает
    python3 scripts/uroven-agenta.py --selftest
Код возврата: 0 — зову, 1 — не зову (причина в выводе), 2 — спросить нечем.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

КОРЕНЬ = Path(__file__).resolve().parent.parent
ДАННЫЕ = КОРЕНЬ / "harness" / "config" / "агенты.yaml"
ЖУРНАЛ = Path(os.environ.get("LOG_DIR", "/var/log/harness")) / "вызовы-агентов.jsonl"


class Отказ(Exception):
    """Спросить нечем: данных нет или они битые."""


def данные(путь=ДАННЫЕ) -> dict:
    try:
        дано = yaml.safe_load(Path(путь).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as беда:
        raise Отказ(f"данные о помощниках не прочитаны ({путь}): {беда}") from беда
    for ключ in ("уровни", "поводы"):
        if not isinstance(дано, dict) or ключ not in дано:
            raise Отказ(f"в данных {путь} нет ключа «{ключ}»")
    return дано


def уровень_установки() -> str:
    """AGENT_LEVEL из harness.conf. Окружение старше файла — для проб."""
    из_окружения = os.environ.get("AGENT_LEVEL")
    if из_окружения:
        return из_окружения
    конф = Path(os.environ.get("HARNESS_CONF", "/etc/harness/harness.conf"))
    try:
        текст = конф.read_text(encoding="utf-8")
    except OSError:
        return ""
    найдено = re.search(r'^AGENT_LEVEL=["\']?([^"\'\s#]+)', текст, re.M)
    return найдено.group(1) if найдено else ""


def звать(повод: str, уровень: str, дано: dict) -> tuple:
    """(звать ли, причина словами). Причина обязана называть уровень."""
    имена = [str(у["имя"]) for у in дано["уровни"]]
    if уровень not in имена:
        return False, (f"уровень «{уровень}» не из списка {', '.join(имена)} — "
                       "не зову ничего, пока владелец не поправит AGENT_LEVEL")
    запись = next((п for п in дано["поводы"] if п.get("повод") == повод), None)
    if запись is None:
        return False, (f"повода «{повод}» нет в данных — список закрытый, "
                       f"не зову даже на уровне «{уровень}»")
    нужен = str(запись.get("с_уровня", ""))
    if нужен not in имена:
        return False, f"у повода «{повод}» уровень «{нужен}» не из списка"
    if имена.index(уровень) >= имена.index(нужен):
        return True, (f"повод «{повод}» разрешён с уровня «{нужен}», "
                      f"сейчас «{уровень}»")
    return False, (f"повод «{повод}» разрешён только с уровня «{нужен}», "
                   f"сейчас «{уровень}»")


def записать(повод: str, уровень: str, зову: bool, причина: str) -> None:
    """След решения: без журнала «позвал/не позвал» нечем мерить уровень."""
    строка = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "повод": повод, "уровень": уровень, "зову": зову,
              "причина": причина}
    try:
        ЖУРНАЛ.parent.mkdir(parents=True, exist_ok=True)
        with ЖУРНАЛ.open("a", encoding="utf-8") as ф:
            ф.write(json.dumps(строка, ensure_ascii=False) + "\n")
    except OSError:
        pass          # журнал — след, а не условие работы


def _selftest() -> int:
    проба = КОРЕНЬ / "scripts" / "test_uroven_agenta.py"
    if not проба.exists():
        print(f"[уровень] пробы нет: {проба}")
        return 1
    return subprocess.run([sys.executable, str(проба)], timeout=300).returncode


def главная(argv: list) -> int:
    if "--selftest" in argv:
        return _selftest()

    def довод(имя):
        return argv[argv.index(имя) + 1] if имя in argv else None

    try:
        дано = данные()
    except Отказ as беда:
        print(f"ОТКАЗ: {беда}", file=sys.stderr)
        return 2
    уровень = довод("--уровень") or уровень_установки() or "минимум"

    if "--показать" in argv:
        print(f"уровень: {уровень}")
        for п in дано["поводы"]:
            зову, _ = звать(п["повод"], уровень, дано)
            print(f"  {'зову     ' if зову else 'не зову  '} {п['повод']}"
                  f"  (с уровня «{п['с_уровня']}»)")
        return 0

    повод = довод("--повод")
    if not повод:
        print("нужен --повод <имя> (список: --показать)", file=sys.stderr)
        return 2
    зову, причина = звать(повод, уровень, дано)
    записать(повод, уровень, зову, причина)
    print(("зову помощника: " if зову else "не зову помощника: ") + причина)
    return 0 if зову else 1


if __name__ == "__main__":
    sys.exit(главная(sys.argv[1:]))
