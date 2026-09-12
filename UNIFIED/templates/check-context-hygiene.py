#!/usr/bin/env python3
"""Гигиена того, что едет в контекст каждой сессии.

За один день всплыли три вещи одного рода: записка для новой сессии разрослась до 172
тысяч символов, рядом обнаружилась вторая, осиротевшая память, и обе выросли из одного —
правило существовало, но держалось вниманием. Разбирать это руками раз в два месяца
означает согласиться разбирать вечно, поэтому проверка стоит в гейте.

Проверяется ровно то, что уже ломалось. Защиты от воображаемых бед здесь нет.

Запуск: python3 scripts/check-context-hygiene.py [--repo <путь>]
Код возврата 1 — есть нарушение, блокирующее коммит.
"""
import argparse
import sys
from pathlib import Path

STATE_MAX = 10_000  # символов; всё, что не «сейчас», уезжает в docs/handover/
INDEX_MAX = 25_000  # индекс памяти грузится каждую сессию — та же болезнь, мягкий порог

MEMORY_HOME = Path.home() / ".claude/projects/<слаг-проекта>/memory"


class Report:
    def __init__(self):
        self.blocking: list[str] = []
        self.warnings: list[str] = []

    def block(self, msg: str) -> None:
        self.blocking.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def chars(path: Path) -> int:
    return len(path.read_text(encoding="utf-8"))


def check_state_size(repo: Path, r: Report) -> None:
    state = repo / "STATE.md"
    if not state.exists():
        r.block("STATE.md не найден — на него смотрит @-include в CLAUDE.md")
        return
    size = chars(state)
    if size > STATE_MAX:
        r.block(
            f"STATE.md разросся: {size} символов при потолке {STATE_MAX}.\n"
            f"    Вытесни завершённое в docs/handover/ГГГГ-ММ.md — STATE.md держит только «что верно сейчас»."
        )


def check_single_memory(repo: Path, r: Report) -> None:
    """Память проекта должна быть в одном месте.

    Осиротевший .claude/memory/ прожил в репозитории 2,5 месяца после того, как Claude Code
    сменил место хранения. Его инварианты за это время разошлись с реальностью: правило
    «никакого учебного режима» осталось записанным, когда тренировочный тур уже работал.
    """
    stray = repo / ".claude/memory"
    if stray.exists():
        r.block(
            f"В репозитории вторая память: {stray}.\n"
            f"    Память живёт только в {MEMORY_HOME}. Перенеси недостающее и удали каталог."
        )


def check_memory_index(r: Report) -> None:
    index = MEMORY_HOME / "MEMORY.md"
    if not index.exists():
        r.warn(f"Индекс памяти не найден: {index}")
        return
    size = chars(index)
    if size > INDEX_MAX:
        r.warn(
            f"Индекс памяти {size} символов при ориентире {INDEX_MAX} — он едет в контекст каждую сессию.\n"
            f"    Пора прополоть: удалить устаревшие факты, слить дубли."
        )


def check_includes_alive(repo: Path, r: Report) -> None:
    """@-include на несуществующий файл — тихая потеря контекста, а не ошибка старта."""
    claude_md = repo / "CLAUDE.md"
    if not claude_md.exists():
        r.block("CLAUDE.md не найден")
        return
    for n, line in enumerate(claude_md.read_text(encoding="utf-8").split("\n"), 1):
        if not line.startswith("@"):
            continue
        target = repo / line[1:].strip()
        if not target.exists():
            r.block(f"CLAUDE.md:{n} подключает несуществующий файл: {line.strip()}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    repo = Path(ap.parse_args().repo)

    r = Report()
    check_state_size(repo, r)
    check_single_memory(repo, r)
    check_memory_index(r)
    check_includes_alive(repo, r)

    for w in r.warnings:
        print(f"[гигиена] ⚠ {w}")
    for b in r.blocking:
        print(f"[гигиена] ✗ {b}", file=sys.stderr)

    if r.blocking:
        return 1
    print("[гигиена] контекст в порядке" + (f" ({len(r.warnings)} предупр.)" if r.warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
