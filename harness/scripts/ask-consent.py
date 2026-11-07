#!/usr/bin/env python3
"""Запросить у владельца согласие кнопками «Да / Нет»: удаление, пуш, выкат, публикация, спека.

    python3 scripts/ask-consent.py --вид удаление --предмет /абсолютный/путь --почему "зачем"
    python3 scripts/ask-consent.py --вид пуш --предмет "origin master" [--репо /абс] --почему "зачем"
    python3 scripts/ask-consent.py --цель /абсолютный/путь --почему "зачем"   # = вид удаление
    python3 scripts/ask-consent.py --вид выкат --почему "зачем"                 # предмет считает код: HEAD дерева
    python3 scripts/ask-consent.py --вид публикация [--репо /пакет] --почему "зачем"
    python3 scripts/ask-consent.py --вид спека --предмет <id задачи> --почему "название"
    python3 scripts/ask-consent.py --вид снос-базы --база <имя базы> --почему "зачем"
    python3 scripts/ask-consent.py --вид перепись --репо <копия истории> --почему "зачем"
    python3 scripts/ask-consent.py --вид <вид> --предмет-двери "<полный предмет>" --почему "зачем"

Кладёт конверт events.emit в $LOG_DIR/consent-requests.jsonl и печатает одноразовый код
запроса отдельной строкой; ждать нажатия не нужно. Сообщение с кнопками шлёт
диспетчер канала, нажатие пишет он же в consent-buttons.jsonl, и дверь (protected-delete.py
для удаления, deploy_guard.py для пуша) принимает его как слово владельца об этом предмете.
Для пуша печатается и готовая команда с хэшем: кнопкой судится только она.
У выката и публикации предмета от сессии нет вовсе: строку считает door_subjects.py тем
же кодом, что и ворота, а свободная строка разошлась бы с тем, что уедет.

Этот файл — единственный писатель consent-requests.jsonl. Носителем согласия
журнал запросов не является: дверь его не читает.

`--предмет-двери` — предмет, который назвала дверь: ask-consent пересчитывает его своим
кодом и при расхождении отказывает (код 2), запроса нет. Проверка и запись идут под одним
flock: повтор на тот же предмет не плодит кнопок.

Код возврата: 0 — запрос записан; 2 — отказ (строки нет, причина в выводе); 3 — запроса нет
намеренно: владелец уже нажал «Нет» (тишина сутки) либо кнопка на этот предмет уже ушла.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import importlib.util
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(КОРЕНЬ / "scripts" / "lib"))
import config                                              # noqa: E402
import door_subjects                                       # noqa: E402
import events                                              # noqa: E402
import owner_word                                          # noqa: E402
import push_subject                                        # noqa: E402

ОТКАЗ = 2
ПОВТОР = owner_word.КОД_ПОВТОРА
# После «Нет» кнопку на тот же вид и предмет не шлём: решение владельца не теребят.
ТИШИНА_ПОСЛЕ_НЕТ_СЕК = 24 * 3600
СОБЫТИЕ_ЗАПРОСА = next(е for е, имя in owner_word.СОБЫТИЯ_СОГЛАСИЯ.items() if имя == "запрос")
GIT_СРОК_СЕК = 5


class Отказ(Exception):
    """Запрос не записан; текст — причина для сессии."""


def _дверь():
    """protected-delete.py как модуль: цель судит ТА ЖЕ функция, что и дверь.

    Имя файла с дефисом обычным import не берётся.
    """
    спека = importlib.util.spec_from_file_location(
        "protected_delete", КОРЕНЬ / "scripts" / "protected-delete.py")
    модуль = importlib.util.module_from_spec(спека)
    спека.loader.exec_module(модуль)
    return модуль


def _под_корнем(путь: str, корни: list[str]) -> bool:
    return any(путь == к or путь.startswith(к.rstrip(os.sep) + os.sep) for к in корни)


def отказ_цели(цель: str) -> str:
    """Причина, по которой кнопку на эту цель просить нельзя; пусто — можно.

    Под секретами и копиями цель судит ТА ЖЕ функция, что и дверь (protected-delete.цель);
    под прочими корнями защиты (PROTECTED_PATHS) — только файл: каталог закрыт наглухо.
    """
    if not os.path.isabs(цель):
        return f"путь не абсолютный: {цель}"
    if os.path.islink(цель):
        return f"{цель} — симлинк: согласие даётся на файл, а не на ссылку"
    установка = owner_word.конфиг_права("install.conf")
    корни, _над_рабочим = door_subjects.protected_roots(установка, owner_word.конфиг_права("harness.conf"))
    реальный = os.path.realpath(цель)
    if not _под_корнем(реальный, корни):
        return f"{реальный} вне защищённых корней — кнопка на удаление здесь не нужна"
    дверь = _дверь()
    секреты, копии = дверь.каталог_секретов(), дверь.каталог_копий()
    for корень in (секреты, копии):
        if корень is not None and _под_корнем(реальный, [os.path.realpath(корень)]):
            return дверь.цель(цель, секреты, копии)[2]
    if os.path.isdir(реальный):
        return f"{реальный} — каталог: кнопка даётся на файл"
    return "" if os.path.exists(реальный) else f"{реальный} не существует"


def _git(репо: str, *args: str) -> str:
    try:
        готово = subprocess.run(["git", "-C", репо, *args], capture_output=True, text=True,
                                timeout=GIT_СРОК_СЕК, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as беда:
        raise Отказ(f"git {args[0]} не отработал: {type(беда).__name__}") from None
    if готово.returncode != 0:
        raise Отказ(f"git {args[0]}: {готово.stderr.strip()[:160] or 'ошибка'}")
    return готово.stdout.strip()


def предмет_удаления(предмет: str, репо: str | None, дверь: str | None) -> tuple[str, list[str]]:
    """→ (цель для строки запроса, доп. строки вывода)."""
    путь = предмет or дверь or ""
    беда = отказ_цели(путь)
    if беда:
        raise Отказ(беда)
    return os.path.realpath(путь), []


def _удалённый_по_адресу(репо: str, адрес: str) -> str:
    """Имя удалённого, чей адрес (без учётки) назван в строке двери."""
    имена = _git(репо, "remote").split()
    for имя in имена:
        if push_subject.hide_userinfo(_git(репо, "remote", "get-url", "--push", имя)) == адрес:
            return имя
    raise Отказ(f"в {репо} нет удалённого с адресом из строки двери")


def предмет_пуша(предмет: str, репо: str | None, дверь: str | None) -> tuple[str, list[str]]:
    if not предмет and дверь:
        # head берётся из строки двери: хук-дверь пересчёт без сети не трогает, ls-remote — здесь.
        try:
            названо = push_subject.parse_subject(дверь)
        except push_subject.PushSubjectError as беда:
            raise Отказ(str(беда)) from None
        репо, head = названо["repo"], названо["head"]
        удалённый, ветка = _удалённый_по_адресу(репо, названо["address"]), названо["ref"].removeprefix("refs/heads/")
    else:
        слова = предмет.split()
        if len(слова) != 2:
            raise Отказ(f"предмет пуша — «<удалённый> <ветка>», получено: {предмет!r}")
        удалённый, ветка = слова
        репо = репо or _git(os.getcwd(), "rev-parse", "--show-toplevel")
        # head берётся в момент запроса: владелец увидит именно этот коммит, и только
        # команда с этим хэшем будет судиться кнопкой.
        head = _git(репо, "rev-parse", "--verify", f"refs/heads/{ветка}")
    try:
        цель = push_subject.push_subject(репо, удалённый, head, ветка)
    except push_subject.PushSubjectError as беда:
        raise Отказ(str(беда)) from None
    реальный = цель.split(" ", 3)[3]
    флаг_репо = "" if os.path.realpath(os.getcwd()) == реальный else f" -C {реальный}"
    return цель, [f"git{флаг_репо} push {удалённый} {head}:refs/heads/{ветка}"]


def _посчитать(считать, *args) -> str:
    try:
        return считать(*args)
    except push_subject.PushSubjectError as беда:
        raise Отказ(str(беда)) from None


def предмет_выката(предмет: str, репо: str | None, дверь: str | None) -> tuple[str, list[str]]:
    дерево = door_subjects.deploy_tree(owner_word.конфиг_права("install.conf"),
                                       owner_word.конфиг_права("harness.conf"))
    return _посчитать(door_subjects.deploy_subject, дерево), []


def предмет_публикации(предмет: str, репо: str | None, дверь: str | None) -> tuple[str, list[str]]:
    сборка, удалённый, ветка = _посчитать(door_subjects.publish_settings,
                                          owner_word.конфиг_права("harness.conf"))
    пакет = репо or str(config.katalog_paketa())
    return _посчитать(door_subjects.publish_subject, пакет, сборка, удалённый, ветка), []


def предмет_спеки(предмет: str, репо: str | None, дверь: str | None) -> tuple[str, list[str]]:
    return _посчитать(door_subjects.spec_subject, предмет), []


def предмет_сноса(предмет: str, репо: str | None, дверь: str | None) -> tuple[str, list[str]]:
    база = предмет or дверь or ""
    префикс = (owner_word.конфиг_права("harness.conf").get("DROPPABLE_DB_PREFIX") or "").strip()
    if not префикс:
        raise Отказ("DROPPABLE_DB_PREFIX не задан в harness.conf — сносить нечего")
    if not база.startswith(префикс) or re.search(r"[\s/:]", база):
        raise Отказ(f"«{база}» не пробная база: имя должно начинаться на «{префикс}» (И-1)")
    return база, []


def предмет_переписи(предмет: str, репо: str | None, дверь: str | None) -> tuple[str, list[str]]:
    if not репо:
        raise Отказ("перепись: нужна --репо <копия истории>")
    _сборка, удалённый, ветка = _посчитать(door_subjects.publish_settings,
                                           owner_word.конфиг_права("harness.conf"))
    адрес = _посчитать(door_subjects.rewrite_address, str(config.katalog_paketa()), удалённый)
    return _посчитать(door_subjects.rewrite_subject, репо, адрес, ветка), []


# Разбор предмета по виду: ключи — ВИДЫ_СОГЛАСИЯ.
ПРЕДМЕТЫ = {"удаление": предмет_удаления, "пуш": предмет_пуша, "выкат": предмет_выката,
            "публикация": предмет_публикации, "спека": предмет_спеки,
            "снос-базы": предмет_сноса, "перепись": предмет_переписи}
# Виды, у которых предмет считает код: --предмет от сессии запрещён.
БЕЗ_ПРЕДМЕТА = ("выкат", "публикация", "перепись")


def вид_и_предмет(аргументы: argparse.Namespace) -> tuple[str, str]:
    if аргументы.цель and (аргументы.вид or аргументы.предмет or аргументы.база):
        raise Отказ("--цель нельзя вместе с --вид/--предмет")
    if аргументы.цель:
        return owner_word.ВИД_ПО_УМОЛЧАНИЮ, аргументы.цель
    if not аргументы.вид:
        raise Отказ("нужны --вид и --предмет (или --цель для удаления)")
    if аргументы.вид not in owner_word.ВИДЫ_СОГЛАСИЯ:
        raise Отказ(f"вид «{аргументы.вид}» — кнопки на него нет; есть: "
                    f"{', '.join(owner_word.ВИДЫ_СОГЛАСИЯ)}")
    if аргументы.база and аргументы.вид != "снос-базы":
        raise Отказ("--база — только для вида «снос-базы»")
    if аргументы.база and аргументы.предмет and аргументы.база != аргументы.предмет:
        raise Отказ("--база и --предмет называют разные базы")
    предмет = аргументы.предмет or аргументы.база or ""
    if аргументы.вид in БЕЗ_ПРЕДМЕТА:
        if предмет:
            raise Отказ(f"у вида «{аргументы.вид}» предмет считает код, --предмет не нужен")
        return аргументы.вид, ""
    if not предмет and not аргументы.предмет_двери:
        raise Отказ("нужны --вид и --предмет (или --цель для удаления)")
    return аргументы.вид, предмет


def повтор(вид: str, цель: str, каталог: Path) -> str | None:
    """Причина не писать запрос (код 3) — или None. НетНосителя летит наверх: журнал не прочитан."""
    решение = owner_word.последнее_решение(вид, цель, каталог)
    сейчас = dt.datetime.now().timestamp()
    if решение and решение["событие"] == "нет" and сейчас - решение["когда"] < ТИШИНА_ПОСЛЕ_НЕТ_СЕК:
        return (f"владелец нажал „Нет“ в {owner_word.время_чм(решение['когда'])} — "
                "тот же предмет не спрашиваю")
    открытый = owner_word.открытый_запрос(вид, цель, каталог)
    if открытый:
        return (f"кнопка уже ушла в {owner_word.время_чм(открытый['когда'])}, "
                f"код {str(открытый.get('nonce'))[:8]}")
    return None


def записать_запрос(вид: str, цель: str, почему: str) -> tuple[int, str]:
    """Проверка дублей и запись — одна секция под flock: два параллельных зова видят друг друга."""
    каталог = Path(events.log_dir())
    if owner_word.стенд() and каталог.resolve() == (owner_word.боевой_каталог_логов() or Path("/")).resolve():
        return ОТКАЗ, "стенд: запрос в боевой журнал машины не пишу"
    каталог.mkdir(parents=True, exist_ok=True)
    with open(каталог / "consent-requests.lock", "a") as замок:
        fcntl.flock(замок, fcntl.LOCK_EX)
        try:
            причина = повтор(вид, цель, каталог)
        except owner_word.НетНосителя as беда:
            return ОТКАЗ, f"журнал кнопок не прочитан: {беда}"
        if причина:
            return ПОВТОР, причина
        nonce = secrets.token_urlsafe(16)
        данные = {"nonce": nonce, "вид": вид, "цель": цель, "почему": почему}
        if not events.emit("scripts/ask-consent.py", СОБЫТИЕ_ЗАПРОСА, "ok",
                           journal="consent-requests", data=данные):
            return ОТКАЗ, "запрос не записан в журнал"
    return 0, nonce


def main() -> int:
    разбор = argparse.ArgumentParser()
    разбор.add_argument("--цель")
    разбор.add_argument("--вид")
    разбор.add_argument("--предмет")
    разбор.add_argument("--предмет-двери", dest="предмет_двери")
    разбор.add_argument("--база")
    разбор.add_argument("--репо")
    разбор.add_argument("--почему", default="")
    аргументы = разбор.parse_args()

    try:
        вид, предмет = вид_и_предмет(аргументы)
        цель, строки = ПРЕДМЕТЫ[вид](предмет, аргументы.репо, аргументы.предмет_двери)
        if аргументы.предмет_двери and аргументы.предмет_двери != цель:
            # Для удаления предмет — имя файла, в секретах оно бывает ключом (И-3): не печатаем.
            видно = "" if вид == "удаление" else f": дверь «{аргументы.предмет_двери[:80]}», пересчёт «{цель[:80]}»"
            raise Отказ(f"предмет двери ≠ пересчёт{видно}")
    except Отказ as беда:
        print(f"[согласие кнопкой] ОТКАЗ: {беда}")
        return ОТКАЗ

    код, итог = записать_запрос(вид, цель, аргументы.почему)
    if код:
        print(f"[согласие кнопкой] {'ОТКАЗ: ' if код == ОТКАЗ else ''}{итог}")
        return код
    print("[согласие кнопкой] запрос записан; диспетчер пришлёт владельцу кнопки")
    print(итог)
    for строка in строки:
        print(строка)
    return 0


if __name__ == "__main__":
    sys.exit(main())
