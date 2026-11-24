#!/usr/bin/env python3
"""PreToolUse на запуск помощника: пока ротация ждёт идущих, новые задачи — в очередь.

Слово владельца 02.11.2026: кодом запрещать начинать любые новые задачи, только
класть в очередь. Сторож ставит флаг `rotation-waits-helpers.json` (читает его
scripts/lib/rotation_flag.py); пока флаг живой и принадлежит этой смене,
запуск Agent/Task и новое поручение SendMessage отказаны (код 2) и ложатся
строкой в `rotation-queue.jsonl`. Преемник получает очередь списком.

Этап «дорабатывает» (ротация с преемником, спека раздел 14.8): старая смена —
отказ и очередь преемника, но текст другой (канал уже не её); преемник — пропуск.
Флаг ищется по session_id среди всех `rotation-waits-helpers*.json`.

Без флага выход сразу: хук стоит на КАЖДОМ запуске помощника.
Пропуски со следом: флаг другой сессии, флаг протухший (сторож давно его не
освежал), SendMessage управляющего harness-lead своему исполнителю — доведение
идущей задачи, служебное «[ротация] доведи и остановись» смены идущему помощнику
(одно на помощника). Новый запуск управляющего — отказ и очередь, как у смены.

Ключи:
    --where           печатает каталог флага и очереди
    --release         печатает невыпущенные записи; ничего не помечает
    --mark <номера>   помечает названные записи выпущенными (после доставки)
Спека: docs/specs/2026-11-02-ротация-ждёт-помощников.md, разделы 5 и 6.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import config as конф                                     # noqa: E402
import events                                             # noqa: E402
import rotation_flag                                      # noqa: E402

SRC = "scripts/hooks/rotation_queue_guard.py"
TOOLS = ("Agent", "Task", "SendMessage")
LEAD_TYPE = "harness-lead"
QUEUE_NAME = "rotation-queue.jsonl"
LOCK_NAME = "rotation-queue.lock"
PART_LABEL = re.compile(r"Часть:\s*(?P<path>[^\s#]+)(?:#(?P<n>\d+))?")
REFUSAL = (
    "Запуск помощника отложен: смена ждёт ротации, пока закончат идущие помощники ({n}). "
    "Задача в очереди — преемник получит её списком; не повторяй запуск, не делай задачу сам "
    "и не вписывай её в передачу. Перезапуск делает смена сама, после конца помощников. "
    "Идущих помощников не трогаю; идущему помощнику можно один раз послать SendMessage, "
    "начатое словами «{stop}». Сообщения владельца это не касается: отвечай ему в канал как обычно.")
# Дорабатывающая смена: ротация её уже не ждёт, задачи и канал у преемника. Слов «отвечай в канал»
# здесь нет: канал закрыт ей хуком guard_bash (раздел 14.7).
FINISHING_REFUSAL = (
    "Запуск отложен: ты дорабатывающая смена, задачи берёт преемник (tmux `{tmux}`). "
    "Поручение в очереди преемника; не повторяй, не делай сам. "
    "Доведи идущих помощников и остановись; «/exit» подаст сторож.")
# Управляющий, начатый до ротации, запускал новых исполнителей, и «идущих помощников»
# всегда было ≥ 1: смена ждала ротацию 69+ мин (находка копии сети 12.11.2026).
LEAD_REFUSAL = (
    "Запуск исполнителя отложен: смена ждёт ротации. Поручение в очереди преемника. "
    "Новых не запускай — доведи уже идущих исполнителей, отчитайся смене и остановись.")
# Служебная дверь смены к идущему помощнику: сказать «доведи и остановись» смена
# иначе не могла — её SendMessage держит этот же хук. Одна на помощника за ожидание.
STOP_PREFIX = "[ротация] доведи и остановись"


def log_event(event: str, outcome: str, session: str, detail: str) -> None:
    """Событие ротации: emit сам не падает, хук из-за журнала не останавливается."""
    events.emit(SRC, event, outcome, journal="rotation", session=session or None,
                data={"detail": detail})


def log_fail_open(reason: str, detail: str) -> None:
    try:
        with open(Path(конф.log_dir()) / "guard_exceptions.log", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "reason": reason, "detail": detail},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass


class Queue:
    """Очередь отложенных запусков. Замок — отдельный файл: перезапись очереди
    через временный файл и rename оставила бы ждущего писателя на старом inode."""

    def __init__(self) -> None:
        self.file = rotation_flag.folder() / QUEUE_NAME
        self.lock_file = rotation_flag.folder() / LOCK_NAME

    def locked(self):
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_file, "a")
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle

    def rows(self) -> list[dict]:
        try:
            lines = self.file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [json.loads(line) for line in lines if line.strip()]

    def write(self, rows: list[dict]) -> None:
        temp = self.file.with_suffix(".tmp")
        temp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        temp.replace(self.file)

    def add(self, row: dict) -> None:
        """Строка без дубля среди невыпущенных (повтор отказанной смены — та же задача)."""
        with self.locked():
            rows = self.rows()
            if any(r.get("ключ") == row["ключ"] and not r.get("выпущено") for r in rows):
                return
            row["номер"] = max((r.get("номер", 0) for r in rows), default=0) + 1
            self.write([*rows, row])

    def pass_stop(self, row: dict, since) -> bool:
        """Служебное сообщение помощнику — одно на помощника за ожидание (`since` — «с» флага).
        След — строка, сразу помеченная выпущенной: преемнику она не уходит."""
        with self.locked():
            rows = self.rows()
            if any(r.get("служебное") and r.get("to") == row["to"] and r.get("с_флага") == since for r in rows):
                return False
            row.update({"служебное": True, "с_флага": since, "выпущено": int(time.time()),
                        "номер": max((r.get("номер", 0) for r in rows), default=0) + 1})
            self.write([*rows, row])
            return True

    def mark(self, numbers: set[int]) -> None:
        with self.locked():
            rows = self.rows()
            for row in rows:
                if row.get("номер") in numbers and not row.get("выпущено"):
                    row["выпущено"] = int(time.time())
            self.write(rows)


def queue_row(payload: dict) -> dict:
    tool_input = payload.get("tool_input") or {}
    base = {"время": datetime.now().astimezone().isoformat(timespec="seconds"),
            "сессия": str(payload.get("session_id") or ""), "инструмент": payload.get("tool_name")}
    if payload.get("tool_name") == "SendMessage":
        text, target = str(tool_input.get("message") or ""), str(tool_input.get("to") or "")
        return {**base, "to": target, "message": text,
                "часть": (PART_LABEL.search(text) or [""])[0],
                "ключ": hashlib.sha256(f"{text}|{target}".encode()).hexdigest()}
    prompt, kind = str(tool_input.get("prompt") or ""), str(tool_input.get("subagent_type") or "")
    return {**base, "description": tool_input.get("description"), "subagent_type": kind,
            "model": tool_input.get("model"), "prompt": prompt,
            "часть": (PART_LABEL.search(prompt) or [""])[0],
            "ключ": hashlib.sha256(f"{prompt}|{kind}".encode()).hexdigest()}


def release() -> int:
    queue = Queue()
    pending = [r for r in queue.rows() if not r.get("выпущено")]
    for row in pending:
        what = row.get("description") or row.get("to") or "—"
        kind = row.get("subagent_type") or "возобновление"
        print(f"{row['номер']}. {what} — {kind}, {row.get('model') or '—'}, {row.get('часть') or '—'}")
    if pending:
        print(f"Полные поручения (prompt целиком): {queue.file}")
    return 0


def hook(payload: dict) -> int:
    if payload.get("tool_name") not in TOOLS:
        return 0
    session = str(payload.get("session_id") or "")
    kind, flag = rotation_flag.state()
    if rotation_flag.successor_enabled() and flag.get("сессия") != session:
        # Флаг другой роли (не `TMUX_SESSION`), где эта сессия — старая смена или преемник.
        other_kind, other = rotation_flag.state_for(session)
        if other_kind != "нет":
            kind, flag = other_kind, other
    if kind == "нет":
        return 0                # флаг исчез между проверкой в main и здесь, либо это не наш флаг
    if kind == "протух":
        log_fail_open("флаг ожидания ротации протух — хук очереди его не исполняет", str(rotation_flag.path()))
        log_event("rotation_flag_stale", "error", session, str(rotation_flag.path()))
        return 0
    if flag.get("сессия") and flag["сессия"] != session:
        log_event("rotation_flag_foreign", "skipped", session, f"флаг сессии {flag['сессия']}")
        return 0
    if payload.get("agent_id") and payload.get("agent_type") == LEAD_TYPE:
        return lead_hook(payload, session)
    row = queue_row(payload)
    if is_stop_message(payload) and Queue().pass_stop(row, flag.get("с")):
        log_event("rotation_stop_message", "ok", session, f"SendMessage {row['to']}: служебное «{STOP_PREFIX}»")
        return 0
    Queue().add(row)
    log_event("rotation_queued", "refused", session, f"{payload.get('tool_name')}: {row.get('часть') or '—'}")
    if rotation_flag.finishing_flag(session):
        print(FINISHING_REFUSAL.format(tmux=flag.get("tmux_роли") or rotation_flag.main_tmux()), file=sys.stderr)
    else:
        print(REFUSAL.format(n=flag.get("помощников", "?"), stop=STOP_PREFIX), file=sys.stderr)
    return 2


def is_stop_message(payload: dict) -> bool:
    """Служебное слово смены идущему помощнику: SendMessage из главной, начатое STOP_PREFIX."""
    message = str((payload.get("tool_input") or {}).get("message") or "")
    return payload.get("tool_name") == "SendMessage" and not payload.get("agent_id") \
        and message.startswith(STOP_PREFIX)


def lead_hook(payload: dict, session: str) -> int:
    """Управляющий доводит идущих исполнителей (SendMessage проходит), новых не запускает."""
    agent = payload["agent_id"]
    if payload.get("tool_name") == "SendMessage":
        target = (payload.get("tool_input") or {}).get("to") or ""
        log_event("rotation_queue_lead_pass", "ok", session, f"{agent}: SendMessage {target}")
        return 0
    row = {**queue_row(payload), "от": agent}
    Queue().add(row)
    log_event("rotation_queue_lead_refused", "refused", session, f"{agent}: {row.get('часть') or '—'}")
    print(LEAD_REFUSAL, file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    if argv[:1] == ["--where"]:
        print(rotation_flag.folder())
        return 0
    if argv[:1] == ["--release"]:
        return release()
    if argv[:1] == ["--mark"]:
        Queue().mark({int(a) for a in argv[1:] if a.isdigit()})
        return 0
    # Без флага — выход сразу, не разбирая stdin.
    if not rotation_flag.any_flag():
        return 0
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError as беда:
        log_fail_open("хук очереди: битый JSON на stdin", repr(беда))
        return 0
    return hook(payload)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
