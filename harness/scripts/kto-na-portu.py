#!/usr/bin/env python3
"""Кто занял порт: PID, команда и пользователь — без ss, lsof и fuser.

СЛУЧАЙ (ревью кода 13.09.2026, F-22). Шаг установки панели проверяет, свободен
ли порт, и при отказе печатал ровно «занят» — а чинить надо ПРОЦЕСС, которого
он не называл. У владельца терминала нет, и «посмотрите сами, кто там» —
инструкция человеку, которого в контуре не бывает.

Почему свой разбор, а не готовый инструмент: на чистой машине нет ни `ss`
(iproute2 не ставится — живая проба в контейнере 13.09.2026), ни `lsof`, ни
`fuser`. Python есть всегда — он ставится вторым шагом установки.

Как: слушающий сокет в /proc/net/tcp{,6} даёт inode, а /proc/<pid>/fd/* на
него ссылается («socket:[<inode>]»). Совпадение inode и есть владелец порта.
Чужие процессы читаются только под root — под обычным пользователем ответ
честно говорит, что видно не всё.

    python3 scripts/kto-na-portu.py 8787
Код возврата: 0 — порт занят и хозяин назван, 1 — порт свободен, 2 — занят,
но хозяина не видно (не хватило прав).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

СЛУШАЕТ = "0A"          # TCP_LISTEN в /proc/net/tcp


def слушающие_inode(порт: int) -> set[str]:
    """Inode слушающих сокетов на этом порту (IPv4 и IPv6)."""
    найдено = set()
    for таблица in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            строки = таблица.read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for строка in строки:
            поля = строка.split()
            if len(поля) < 10 or поля[3] != СЛУШАЕТ:
                continue
            if int(поля[1].split(":")[1], 16) == порт:
                найдено.add(поля[9])
    return найдено


def хозяева(inode: set[str]) -> list[tuple[int, str, str]]:
    """(pid, команда, пользователь) процессов, держащих эти сокеты."""
    цели = {f"socket:[{и}]" for и in inode}
    итог = []
    for каталог in Path("/proc").iterdir():
        if not каталог.name.isdigit():
            continue
        try:
            ссылки = [os.readlink(файл) for файл in (каталог / "fd").iterdir()]
        except OSError:
            continue        # процесс ушёл или чужой: без root видно не всё
        if not цели & set(ссылки):
            continue
        try:
            команда = (каталог / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace").strip()
            владелец = каталог.owner()
        except (OSError, KeyError):
            команда, владелец = "?", "?"
        итог.append((int(каталог.name), команда or "?", владелец))
    return итог


def главная(порт: int) -> int:
    inode = слушающие_inode(порт)
    if not inode:
        print(f"порт {порт} свободен: слушающих сокетов нет")
        return 1
    кто = хозяева(inode)
    if not кто:
        print(f"порт {порт} ЗАНЯТ, но хозяина не видно "
              f"(нужен root: /proc чужих процессов закрыт)")
        return 2
    for pid, команда, владелец in кто:
        print(f"порт {порт} занят: PID {pid} · {владелец} · {команда[:160]}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print(__doc__)
        sys.exit(2)
    sys.exit(главная(int(sys.argv[1])))
