#!/usr/bin/env python3
"""Stop-хук: ход не кончается, пока владельцу в канал не ушёл ответ.

Владелец 30.10.2026: «он в cli отчитывается, а мне в телегу молчит». Правило
«отвечено / нет» — scripts/lib/channel_reply.py (то же читает метрика).

Режимы (harness.conf): в жёлтые CHANNEL_REPLY_YELLOW_DAYS дней от
CHANNEL_REPLY_YELLOW_FROM, а также при CHANNEL_REPLY_STRICT=0 — напоминание один
раз на сообщение (ключ — имя файла, он переживает перенос в обработано/).
После жёлтого при STRICT=1 — блок на каждом Stop, но только для сообщений не
старше CHANNEL_REPLY_MAX_AGE_MIN: древнее безответное не держит смену вечно.

Молчит: stop_hook_active (иначе петля), пустой TMUX (headless-демон claude -p
писать владельцу не вправе), любой сбой (fail-open: Stop не должен падать).
Пропускает без траты антиповтора: ход начат служебным сообщением (помощник,
перезапуск, пинок сторожа — channel_reply.turn_started_by_service) и законную
дверь — метку DOOR_FILE с причиной в LOG_DIR, новее сообщения и не старше
DOOR_MAX_AGE_S. Каждое решение по безответному сообщению — строка журнала
«ответы-в-канал», по ней следующий замер считает блоки без стенограмм.
"""
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import channel_reply  # noqa: E402

SRC = "scripts/hooks/channel_reply_guard.py"
STATE_FILE = "channel-reply-guard.state"
DOOR_MAX_AGE_S = 30 * 60
MESSAGE = (
    "ответь владельцу в канал: на его последнее сообщение нет доставленного ответа. "
    "Отправь ответ командой: bash scripts/tg_send.sh \"<текст>\" "
    "(ответ в CLI владелец не видит). Квитанция «принял / ответ следом» ответом "
    "не считается: нужен ответ по сути. Если ответ не нужен (владелец написал «ок», "
    "это команда боту) — запиши причину: echo \"<причина>\" > {door}"
)


def in_yellow(settings, today):
    start = date.fromisoformat(settings.get("CHANNEL_REPLY_YELLOW_FROM") or "2026-10-30")
    days = int(settings.get("CHANNEL_REPLY_YELLOW_DAYS") or 3)
    return start <= today < start + timedelta(days=days)


def should_block(settings, state_path, found, now, today):
    moment, name = found
    if settings.get("CHANNEL_REPLY_STRICT") == "1" and not in_yellow(settings, today):
        return now - moment <= channel_reply.reply_horizon_sec(settings)
    try:
        if state_path.read_text(encoding="utf-8").strip() == name:
            return False
    except OSError:
        pass
    state_path.write_text(name + "\n", encoding="utf-8")
    return True


def record(outcome, why, name, session, note=None):
    import events
    data = {"reason": why, "message": name}
    if note:
        data["note"] = note
    events.emit(SRC, "channel_reply." + ("block" if outcome == "refused" else "skip"), outcome,
                journal="ответы-в-канал", session=session or None, data=data)


def main():
    import config

    payload = json.loads(sys.stdin.read())
    if payload.get("stop_hook_active"):
        return 0
    if not os.environ.get("TMUX"):
        return 0
    log_dir = config.log_dir()
    found = channel_reply.last_unanswered(log_dir)
    if not found:
        return 0
    moment, name = found
    session = payload.get("session_id")
    note = channel_reply.closed_by_door(log_dir, moment, time.time(), max_age=DOOR_MAX_AGE_S)
    if note:
        record("skipped", "door", name, session, note)
        return 0
    if channel_reply.turn_started_by_service(payload.get("transcript_path")):
        record("skipped", "service", name, session)
        return 0
    settings = config.из_файла(config.путь_harness_conf())
    if not should_block(settings, Path(log_dir) / STATE_FILE, found, time.time(), date.today()):
        record("skipped", "repeat", name, session)
        return 0
    record("refused", "owner", name, session)
    print(MESSAGE.format(door=Path(log_dir) / channel_reply.DOOR_FILE), file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
