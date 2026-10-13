#!/usr/bin/env python3
"""Комментарии владельца из панели → пинки агенту (спека hr.kanban §4).

Панель кладёт заявку `comments/new/<ts>.json` в свой каталог состояния;
диспетчер зовёт этот приём первой строкой своего цикла:

    comments-intake.py                  — по строке `<ts>\\t<пинок>` на каждую
                                          заявку, которой нет в журнале доставки;
    comments-intake.py --доставлено TS  — отметить доставку TS в журнале.

Код 1 — заявки прочитать нельзя; причина словами в stdout. Негодная заявка
(не разбирается, чужой id, длиннее потолка) не печатается: она отмечается в
журнале исходом «битая» с причиной — один раз.

Почему отметка — отдельным вызовом, а не здесь же при печати: доставка
случается в диспетчере (`agent_kick`), и между печатью и пинком он может
умереть. Неотмеченная заявка повторится на следующем круге — дубль пинка
дешевле потерянного слова владельца.

Журнал доставки — свой, в LOG_DIR: файлы панели агент не правит и не удаляет.
Текст комментария во входящие НЕ кладётся: оттуда ворота выката и одобрение
спеки берут согласия владельца, а комментарий — разговор, не команда.
"""
import datetime
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import pipeline  # noqa: E402

DELIVERED_JOURNAL = "panel-comments-delivered.jsonl"
# То же правило, что у панели (state.TASK_ID): id — имя файла комментария.
TASK_ID = re.compile(r"(?!\.)[\w.\-]{1,200}")


class IntakeError(Exception):
    """Заявки прочитать нельзя — это отказ, а не «комментариев нет»."""


def claims_dir() -> Path:
    return Path(config.panel_state_dir()) / "comments" / "new"


def delivered_path() -> Path:
    return Path(config.log_dir()) / DELIVERED_JOURNAL


def text_limit() -> int:
    """Потолок длины — тот же, что у формы панели и сообщения канала (данные)."""
    limit = (pipeline.конфиг().get("канал") or {}).get("отказ_с")
    if not limit:
        raise IntakeError("потолок длины канал.отказ_с не прочитан из pipeline.yaml")
    return int(limit)


def delivered() -> set:
    try:
        lines = delivered_path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    done = set()
    for line in lines:
        try:
            done.add(str(json.loads(line)["ts"]))
        except (ValueError, KeyError, TypeError):
            continue
    return done


def claim_names() -> list:
    """Имена заявок. Нечитаемый каталог — отказ: glob вернул бы пусто, и слово
    владельца пропало бы без сигнала."""
    try:
        names = os.listdir(claims_dir())
    except FileNotFoundError:
        return []
    except OSError as error:
        raise IntakeError(f"каталог заявок {claims_dir()} не читается: {error.strerror}")
    # Точка в начале — заявка, которую панель ещё пишет (переименует по готовности).
    return sorted(n for n in names if n.endswith(".json") and not n.startswith("."))


def judge(path: Path, limit: int) -> tuple:
    """(заявка, None) годная или (None, причина) негодная. Нечитаемый файл —
    отказ всего приёма: это права, а не порча заявки."""
    if path.stat().st_size > limit * 4 + 1024:
        return None, f"заявка {path.stat().st_size} байт — больше потолка {limit} знаков"
    try:
        claim = json.loads(path.read_text(encoding="utf-8"))
    except PermissionError as error:
        raise IntakeError(f"заявка {path.name} не читается: {error.strerror}")
    except (ValueError, UnicodeDecodeError) as error:
        return None, f"заявка не разбирается: {error}"
    if not isinstance(claim, dict):
        return None, "заявка не словарь"
    if not TASK_ID.fullmatch(str(claim.get("id") or "")):
        return None, f"негодный id задачи: {str(claim.get('id'))[:80]!r}"
    if len(str(claim.get("текст") or "")) > limit:
        return None, f"текст длиннее потолка {limit} знаков"
    return claim, None


def pending() -> list:
    """[(ts, заявка)] недоставленных, по времени. Негодная заявка отмечается
    исходом «битая» один раз и больше не печатается и не шумит."""
    done, limit = delivered(), text_limit()
    found = []
    for name in claim_names():
        ts = name[:-len(".json")]
        if ts in done:
            continue
        claim, broken = judge(claims_dir() / name, limit)
        if broken:
            record(ts, "битая", broken)
        else:
            found.append((ts, claim))
    return found


def flat(text: str) -> str:
    """Текст одной строкой без управляющих символов: пинок уходит в TUI агента
    нажатиями (ESC прерывает ход, Ctrl-C закрывает), а перевод строки
    разорвал бы протокол диспетчера. Категория C — управляющие, формат
    (bidi, нулевой ширины) и прочие невидимые."""
    visible = "".join(" " if unicodedata.category(c).startswith("C") else c for c in text)
    return " ".join(visible.split())


def kick_text(claim: dict) -> str:
    task = str(claim["id"])
    return (f"Комментарий владельца из панели к {task}: {flat(str(claim.get('текст') or ''))} "
            f"(полностью — панель, задача {task})")


def record(ts: str, outcome: str, reason: str = "") -> None:
    """Исход заявки в журнал доставки; повтор не дублирует."""
    if ts in delivered():
        return
    row = {"ts": ts, "исход": outcome,
           "доставлено": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    if reason:
        row["причина"] = reason
    with open(delivered_path(), "a", encoding="utf-8") as journal:
        journal.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv: list) -> int:
    if len(argv) == 2 and argv[0] == "--доставлено":
        record(argv[1], "доставлено")
        return 0
    if argv:
        print("использование: comments-intake.py [--доставлено <ts>]", file=sys.stderr)
        return 2
    try:
        claims = pending()
    except IntakeError as error:
        # Причина — в stdout: диспетчер говорит её словами, stderr у него в журнал.
        print(f"приём комментариев не работает: {error}")
        return 1
    for ts, claim in claims:
        print(f"{ts}\t{kick_text(claim)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
