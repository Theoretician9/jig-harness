#!/usr/bin/env python3
"""Слово собеседника группы не требует карточки; слово владельца — как было.

Больной случай копии business 23.11.2026: партнёр владельца пишет в группу. С ключом
TG_GROUP_SPEAKERS диспетчер кладёт его текст во входящие файлом, первая строка которого —
подпись «от: <имя> <id>» (scripts/tg-dispatcher.sh; судит test_consent_buttons.py, случай
группа-собеседник). Гейт задач из канала (scripts/tasks-from-channel.py) обязан обходить
такой файл, как «НЕ ПРОВЕРЕНО:»: глаголы работы в нём — слова партнёра, не поручение
владельца, и карточку по ним гейт требовать не должен. То же — письмо сети серверов
(scripts/evolution-net.py кладёт его в inbox первой строкой «письмо от <подписант>»;
у копии CF 0.15.0 такие письма красили гейт).

Гейт берётся из TASKS_FROM_CHANNEL_PY (дверь мутаций), гейт указаний (scripts/orders.py, то же
правило про письма сети) — из ORDERS_PY, иначе рядом с пробой. Очередь —
свой временный каталог; список без исхода считает сам гейт.

    python3 scripts/test_tasks_from_channel_speaker.py   # 0 — падений 0, 1 — красная
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

cases = Cases()
check = cases.check
WORK = "надо срочно починить выкат"   # «надо» — слово работы и в queue.yaml, и в умолчании гейта


def load_module(door: str, name: str, module: str):
    path = Path(os.environ.get(door) or HERE / name)
    spec = importlib.util.spec_from_file_location(module, path)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return gate


def load_gate():
    return load_module("TASKS_FROM_CHANNEL_PY", "tasks-from-channel.py", "tasks_from_channel_under_probe")


def order_debts(orders, root: Path, mid: str, text: str) -> list:
    """Долги гейта указаний по одному сообщению в своём каталоге: ORDERS.md пуст, курсора нет."""
    (root / "repo" / "память").mkdir(parents=True)
    (root / "repo" / "память" / "ORDERS.md").write_text("# указания\n", encoding="utf-8")
    done = root / "log" / "inbox" / "обработано"
    done.mkdir(parents=True)
    (done / f"{mid}.txt").write_text(text, encoding="utf-8")
    known = orders.занесённые(root / "repo")
    return [m for m, _ in orders.неразобранные(orders.очередь.сообщения(done), 0.0, known)]


def pending(gate, root: Path, files: dict) -> list:
    inbox = root / "inbox"
    inbox.mkdir()
    for name, text in files.items():
        (inbox / f"{name}.txt").write_text(text, encoding="utf-8")
    return [mid for mid, _ in gate.без_исхода(gate.очередь.сообщения(inbox), 0.0, gate.разобранные(root))]


def main() -> int:
    gate = load_gate()
    print("— С1. входящие: собеседник с «от:», владелец, непроверенное")
    with tempfile.TemporaryDirectory(prefix="proba-tasks-speaker-") as tmp:
        found = pending(gate, Path(tmp), {
            "5.0": f"от: Партнёр 444000\n{WORK}\n",
            "6.0": f"{WORK}\n",
            "7.0": f"{gate.НЕПРОВЕРЕННОЕ} паспорт без TG_OWNER_ID, from.id=7\n{WORK}\n",
            "8.0": f"Пишу от: себя — {WORK}\n",
        })
    check("С1) слово владельца с работой — без исхода (разбор не пуст, гейт зрячий)", True, "6.0" in found)
    check("С1) слово собеседника (первая строка «от:») карточки не требует", False, "5.0" in found)
    check("С1) непроверенное — как было, не требует", False, "7.0" in found)
    check("С1) «от:» не первой строкой — слово владельца, требует", True, "8.0" in found)
    # С2 (lcf2 Д4): evolution-net.py (deliver_letter) кладёт письмо сети в тот же inbox
    # первой строкой «письмо от <подписант>» — копия или principal хозяина. Глаголы
    # работы в нём — слова копии, не поручение владельца.
    print("— С2. входящие: письмо сети, слово владельца, начатое словами «письмо от»")
    with tempfile.TemporaryDirectory(prefix="proba-tasks-letter-") as tmp:
        found = pending(gate, Path(tmp), {
            "9.0": f"письмо от HarnessQ\n---\nот: HarnessQ\n---\n{WORK}\n",
            "9.1": f"письмо от host@harness-evolution\n{WORK}\n",
            "9.2": f"письмо от мамы пришло, {WORK}\n",
            "9.3": f"{WORK}\n",
        })
    check("С2) слово владельца с работой — без исхода (разбор не пуст, гейт зрячий)", True, "9.3" in found)
    check("С2) письмо копии сети карточки не требует", False, "9.0" in found)
    check("С2) письмо хозяина сети (principal с @) карточки не требует", False, "9.1" in found)
    check("С2) «письмо от …» с текстом в той же строке — слово владельца, требует", True, "9.2" in found)
    print("— С3. гейт указаний (orders.py): письмо сети с маркером указания")
    orders = load_module("ORDERS_PY", "orders.py", "orders_under_probe")
    for mid, text, owner in (("9.0", "письмо от HarnessQ\nникогда так не делай, запомни\n", False),
                             ("9.1", "письмо от host@harness-evolution\nвсегда отвечай сразу\n", False),
                             ("9.2", "письмо от мамы пришло — запомни: всегда отвечать\n", True),
                             ("9.3", "запомни: всегда отвечать\n", True)):
        with tempfile.TemporaryDirectory(prefix="proba-orders-letter-") as tmp:
            debts = order_debts(orders, Path(tmp), mid, text)
        kind = "слово владельца — долг" if owner else "письмо сети — не указание владельца"
        check(f"С3) {mid} {kind}", [mid] if owner else [], debts)
    if cases.ok:
        print(f"падений 0 из {cases.paths} путей")
        return 0
    print(f"ПРОБА КРАСНАЯ: падений {cases.failures} из {cases.paths} путей")
    return 1


if __name__ == "__main__":
    sys.exit(main())
