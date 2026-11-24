#!/usr/bin/env python3
"""Обновление не ставит канал-группу без TG_OWNER_ID: отказ ДО раскладки и перезапуска.

Находка копии сети 20.11.2026: 0.14.3 стал судить слово владельца в канале-группе по
from.id с ключом паспорта TG_OWNER_ID (eccc86e7). Копия с группой и без ключа обновилась,
диспетчер перезапустился и три сообщения владельца пропали. До 0.14.3 владелец в группе
не определялся вовсе: засчитывалось слово любого участника, поэтому «прежней логики»,
из которой id можно вывести, нет.

Решение о выводе id (бумага безопасности): из данных установки id владельца группы
НЕ выводится. Всё, что там лежит с from.id (сырые JSON вложений в очереди, строки кнопок,
журнал диспетчера), до 0.14.3 мог написать любой участник группы, а после — только
при уже вписанном ключе. Вывести id оттуда — сделать владельцем того, кто первым
написал в группу. Поэтому группа без ключа (или с ключом-группой) — отказ обновления
до копии и раскладки, а причина называет ключ и install.conf: владелец вписывает id сам.

Договор, под который пишется код:
  * отказ — `{"поставлено": False, "беда": <причина>}`, причина называет TG_OWNER_ID
    и install.conf и доходит до владельца через `сказать`;
  * судится паспорт установки `конф_модуль.путь_install_conf()` (дверь пробы —
    HARNESS_INSTALL_CONF): TG_CHAT_ID с «-» и TG_OWNER_ID пуст или тоже с «-»;
  * отказ раньше копии (`_снять_копию`), раскладки и перезапуска служб;
  * install.conf обновление не пишет: ни из улик в журналах, ни вообще.

Стенд — свой каталог (mktemp): паспорт, журналы, улики. Внешнее у update.py подменено
в процессе пробы: сверка отвечает «есть новое», копия — растяжка (дошли — записано).
Обновлятор берётся РЯДОМ с пробой: из копии дерева проба судит мутанта.

    python3 scripts/test_update_owner_id.py      # 0 — падений 0, 1 — красная
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

GROUP = "-1001234567"
OWNER = "777000"
STRANGER = "555000"

STAND = Path(tempfile.mkdtemp(prefix="proba-update-owner-id-"))
LOGS = STAND / "журналы"
(LOGS / "locks").mkdir(parents=True)
PASSPORT = STAND / "install.conf"
(STAND / "harness.conf").write_text("", encoding="utf-8")
# Окружение старше файла: живые TG_* и LOG_DIR зова увели бы суд в чужой паспорт.
for key in ("TG_CHAT_ID", "TG_OWNER_ID", "PROJECT_DIR", "STATE_DIR"):
    os.environ.pop(key, None)
os.environ.update(HARNESS_INSTALL_CONF=str(PASSPORT), HARNESS_CONF=str(STAND / "harness.conf"),
                  LOG_DIR=str(LOGS), HARNESS_SELFTEST="1", HARNESS_TG_SEND="")

spec = importlib.util.spec_from_file_location("update_under_probe", HERE / "update.py")
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)

cases = Cases()
check = cases.check
traces: list = []


class Reached(Exception):
    """Постановка дошла до копии: суд паспорта её не остановил."""


def stage_stubs() -> None:
    """Всё до копии отвечает «можно»; копия — растяжка. Живое не трогается."""
    update.спросить_обновления = lambda: {"есть новое": True, "коммит": "f" * 40, "дата": "сегодня"}
    update._занят_выкат = lambda: False
    update.замок_раскладки = lambda: ""
    update._count_own_commits = lambda *_: 0
    update.красные_прошлых_ворот = lambda *_: None    # основы нет — ворота «до» отвечает проверить()

    def copy_tripwire(_):
        traces.append("копия")
        raise Reached("дошло до копии")
    update._снять_копию = copy_tripwire


def install(passport: str) -> dict:
    """Одна постановка на паспорте стенда → итог; «дошло» — суд не остановил."""
    PASSPORT.write_text(f'PROJECT_NAME="стенд"\nLOG_DIR="{LOGS}"\n{passport}', encoding="utf-8")
    traces.clear()
    told: list = []

    def lay(**_):
        traces.append("раскладка")
        return True, []

    def restart():
        traces.append("перезапуск")
        return True
    try:
        result = update.поставить(разложить=lay, перезапустить=restart, проверить=lambda: Counter(),
                                  сказать=told.append, спросить=lambda *_a, **_k: 1,
                                  сверить_корень=lambda: [])
    except Reached:
        result = {"дошло": True}
    return dict(result, сказано=told, следы=list(traces))


def refused_by_key(result: dict) -> bool:
    why = str(result.get("беда") or "")
    return (result.get("поставлено") is False and "TG_OWNER_ID" in why and "install.conf" in why
            and not result["следы"])


def plant_foreign_evidence() -> None:
    """Улики с from.id чужого участника — всё, откуда «вывести id» соблазнительно."""
    queue = LOGS / "inbox"
    queue.mkdir(exist_ok=True)
    (queue / "1763600000.1.txt").write_text(json.dumps(
        {"message_id": 9, "chat": {"id": int(GROUP), "type": "supergroup"},
         "from": {"id": int(STRANGER)}, "photo": [{"file_id": "x"}]}), encoding="utf-8")
    (LOGS / "consent-buttons.jsonl").write_text(json.dumps(
        {"event": "да", "data": {"from_id": int(STRANGER), "nonce": "n" * 22}}) + "\n", encoding="utf-8")
    (LOGS / "tg-dispatcher.log").write_text(
        f"входящее в группе не засчитано: канал — группа {GROUP}, а TG_OWNER_ID в паспорте нет;"
        f" from.id={STRANGER}\n", encoding="utf-8")


def main() -> int:
    stage_stubs()
    print("— У1. группа без TG_OWNER_ID: отказ до копии, раскладки и перезапуска")
    r = install(f'TG_CHAT_ID="{GROUP}"\n')
    check("У1) отказ называет TG_OWNER_ID и install.conf, ни копии, ни раскладки, ни перезапуска",
          True, refused_by_key(r))
    check("У1) владелец слышит причину отказа", True, any("TG_OWNER_ID" in t for t in r["сказано"]))
    print(f"    итог: {r}")

    print("— У2. группа с ключом владельца: суд паспорта пропускает")
    r = install(f'TG_CHAT_ID="{GROUP}"\nTG_OWNER_ID="{OWNER}"\n')
    check("У2) группа с TG_OWNER_ID — постановка идёт дальше (дошла до копии)", True, r.get("дошло"))

    print("— У3. личка без ключа: как была")
    r = install(f'TG_CHAT_ID="{OWNER}"\n')
    check("У3) личка без TG_OWNER_ID — постановка идёт дальше", True, r.get("дошло"))

    print("— У4. ключ-группа вместо человека: id группы человеком не бывает")
    r = install(f'TG_CHAT_ID="{GROUP}"\nTG_OWNER_ID="{GROUP}"\n')
    check("У4) TG_OWNER_ID с «-» — отказ, как без ключа", True, refused_by_key(r))

    print("— У5. безопасность: id чужого участника из улик не выводится")
    plant_foreign_evidence()
    r = install(f'TG_CHAT_ID="{GROUP}"\n')
    passport = PASSPORT.read_text(encoding="utf-8")
    check("У5) при уликах с чужим from.id — всё равно отказ", True, refused_by_key(r))
    check("У5) install.conf не тронут: ни TG_OWNER_ID, ни чужого id", (False, False),
          ("TG_OWNER_ID" in passport, STRANGER in passport))

    shutil.rmtree(STAND, ignore_errors=True)
    if cases.ok:
        print(f"падений 0 из {cases.paths} путей")
        return 0
    print(f"ПРОБА КРАСНАЯ: падений {cases.failures} из {cases.paths} путей")
    return 1


if __name__ == "__main__":
    sys.exit(main())
