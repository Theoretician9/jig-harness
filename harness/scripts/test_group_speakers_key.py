#!/usr/bin/env python3
"""Ключ TG_GROUP_SPEAKERS доезжает до установки пустым и ничего не ломает без себя.

Больной случай копии business 23.11.2026: партнёр владельца написал в группу 25 сообщений,
диспетчер отбросил все как «чужой from.id», а вписать второго говорящего было некуда — в
паспорте один TG_OWNER_ID. Договор доставки ключа (поведение судьи и диспетчера судит
test_consent_buttons.py, случаи собеседник-судья и группа-собеседник):
  * установщик пишет в паспорт строку TG_GROUP_SPEAKERS="" с подписью — владелец видит, куда
    вписать from.id собеседника, рядом с TG_CHAT_ID;
  * паспорт копии, поставленной до ключа (строки нет), и пустой ключ — поведение как было:
    обновление не отказывает и install.conf не пишет (он у root, правит владелец).

Установщик берётся из USTANOVIT_UNDER_TEST (дверь мутаций), иначе рядом с деревом пакета
(<пакет>/USTANOVIT.sh), иначе из пакета-источника (config.katalog_paketa). Шаг 0 достаётся
РАЗБОРОМ установщика; пустое извлечение — провал. Обновлятор — рядом с пробой.

    python3 scripts/test_group_speakers_key.py      # 0 — падений 0, 1 — красная
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import config  # noqa: E402
from selftest_cases import Cases  # noqa: E402

KEY = "TG_GROUP_SPEAKERS"
GROUP = "-1001234567"
OWNER = "777000"
SPEAKER = "444000"

cases = Cases()
check = cases.check


def installer() -> Path | None:
    door = os.environ.get("USTANOVIT_UNDER_TEST")
    if door:
        return Path(door)
    for candidate in (HERE.parent.parent / "USTANOVIT.sh", config.katalog_paketa() / "USTANOVIT.sh"):
        if candidate.is_file():
            return candidate
    return None


def passport_template(text: str) -> str:
    """Тело heredoc, которым шаг 0 пишет install.conf; нет — пусто."""
    step = re.search(r"^шаг_0\(\) \{.*?^\}$", text, re.MULTILINE | re.DOTALL)
    if not step:
        return ""
    body = re.search(r'записать_файл "\$CONF"[^\n]*<<EOF\n(.*?)^EOF$', step.group(0), re.MULTILINE | re.DOTALL)
    return body.group(1) if body else ""


def probe_installer() -> None:
    print("— В1. установщик пишет в паспорт пустой TG_GROUP_SPEAKERS с подписью")
    src = installer()
    check("В1) установщик найден", True, src is not None)
    template = passport_template(src.read_text(encoding="utf-8")) if src else ""
    keys = config.из_текста(template)
    check("В1) шаг 0 разобран: в паспорте есть TG_CHAT_ID (извлечение не пустое)", True, "TG_CHAT_ID" in keys)
    check("В1) паспорт несёт TG_GROUP_SPEAKERS, значение пустое", ("есть", ""),
          ("есть" if KEY in keys else "нет", keys.get(KEY)))
    line = next((s for s in template.splitlines() if s.startswith(f"{KEY}=")), "")
    check("В1) у строки ключа подпись (что вписывать)", True, "#" in line and "from.id" in line)
    # Судья делит список по запятой (consent_buttons.speaker_ids): «через пробел» из подписи
    # склеил бы два id в один, и собеседник молча остался бы чужим.
    comment = line.partition("#")[2]
    check("В1) подпись называет разделитель — запятую, не пробел", (True, False),
          ("запят" in comment, "пробел" in comment))


def probe_update() -> None:
    """Пустой и отсутствующий ключ обновление пропускает; паспорт не трогает."""
    stand = Path(tempfile.mkdtemp(prefix="proba-group-speakers-key-"))
    logs = stand / "журналы"
    (logs / "locks").mkdir(parents=True)
    passport = stand / "install.conf"
    (stand / "harness.conf").write_text("", encoding="utf-8")
    for key in ("TG_CHAT_ID", "TG_OWNER_ID", KEY, "PROJECT_DIR", "STATE_DIR"):
        os.environ.pop(key, None)
    os.environ.update(HARNESS_INSTALL_CONF=str(passport), HARNESS_CONF=str(stand / "harness.conf"),
                      LOG_DIR=str(logs), HARNESS_SELFTEST="1", HARNESS_TG_SEND="")
    spec = importlib.util.spec_from_file_location("update_under_probe", HERE / "update.py")
    update = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(update)

    class Reached(Exception):
        """Постановка дошла до копии: суд паспорта её не остановил."""

    def tripwire(_):
        raise Reached
    update.спросить_обновления = lambda: {"есть новое": True, "коммит": "f" * 40, "дата": "сегодня"}
    update._занят_выкат = lambda: False
    update.замок_раскладки = lambda: ""
    update._count_own_commits = lambda *_: 0
    update.красные_прошлых_ворот = lambda *_: None
    update._снять_копию = tripwire

    print("— В2. обновление: группа с владельцем, ключа нет / пустой / со списком — идёт дальше")
    for label, line in (("ключа нет", ""), ("пустой", f'{KEY}=""\n'), ("список", f'{KEY}="{SPEAKER}"\n')):
        text = f'PROJECT_NAME="стенд"\nLOG_DIR="{logs}"\nTG_CHAT_ID="{GROUP}"\nTG_OWNER_ID="{OWNER}"\n{line}'
        passport.write_text(text, encoding="utf-8")
        try:
            update.поставить(разложить=lambda **_: (True, []), перезапустить=lambda: True,
                             проверить=lambda: Counter(), сказать=lambda *_: None,
                             спросить=lambda *_a, **_k: 1, сверить_корень=lambda: [])
            reached = False
        except Reached:
            reached = True
        check(f"В2) {label}: постановка дошла до копии, паспорт не тронут", (True, text),
              (reached, passport.read_text(encoding="utf-8")))
    shutil.rmtree(stand, ignore_errors=True)


def main() -> int:
    probe_installer()
    probe_update()
    if cases.ok:
        print(f"падений 0 из {cases.paths} путей")
        return 0
    print(f"ПРОБА КРАСНАЯ: падений {cases.failures} из {cases.paths} путей")
    return 1


if __name__ == "__main__":
    sys.exit(main())
