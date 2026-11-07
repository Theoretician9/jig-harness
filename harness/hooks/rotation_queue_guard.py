#!/usr/bin/env python3
"""PreToolUse на запуск помощника: пока ротация ждёт идущих, новые задачи — в очередь.

Слово владельца 02.11.2026: кодом запрещать начинать любые новые задачи, только
класть в очередь. Сторож ставит флаг `rotation-waits-helpers.json` (читает его
scripts/lib/rotation_flag.py); пока флаг живой и принадлежит этой смене,
запуск Agent/Task и новое поручение SendMessage отказаны (код 2) и ложатся
строкой в `rotation-queue.jsonl`. Преемник получает очередь списком.

Без флага выход сразу: хук стоит на КАЖДОМ запуске помощника.
Пропуски со следом: флаг другой сессии, флаг протухший (сторож давно его не
освежал), запуск из управляющего harness-lead — часть уже идущей задачи.

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
    "Идущих помощников не трогаю. Сообщения владельца это не касается: отвечай ему в канал как обычно.")


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
    kind, flag = rotation_flag.state()
    if kind == "нет":
        return 0                # флаг исчез между проверкой в main и здесь
    session = str(payload.get("session_id") or "")
    if kind == "протух":
        log_fail_open("флаг ожидания ротации протух — хук очереди его не исполняет", str(rotation_flag.path()))
        log_event("rotation_flag_stale", "error", session, str(rotation_flag.path()))
        return 0
    if flag.get("сессия") and flag["сессия"] != session:
        log_event("rotation_flag_foreign", "skipped", session, f"флаг сессии {flag['сессия']}")
        return 0
    if payload.get("agent_id") and payload.get("agent_type") == LEAD_TYPE:
        description = (payload.get("tool_input") or {}).get("description") or ""
        log_event("rotation_queue_lead_pass", "ok", session, f"{payload['agent_id']}: {description}")
        return 0
    row = queue_row(payload)
    Queue().add(row)
    log_event("rotation_queued", "refused", session, f"{payload.get('tool_name')}: {row.get('часть') or '—'}")
    print(REFUSAL.format(n=flag.get("помощников", "?")), file=sys.stderr)
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
    if not rotation_flag.path().exists():
        return 0
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError as беда:
        log_fail_open("хук очереди: битый JSON на stdin", repr(беда))
        return 0
    return hook(payload)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
