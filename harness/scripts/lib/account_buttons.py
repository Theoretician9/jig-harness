#!/usr/bin/env python3
"""Кнопки выбора аккаунта на стороне диспетчера: что слать владельцу и как судить нажатие.

Нажатие владельца на аккаунт и есть выбор: второго «Да» не просим, потому что
переход обратим (откат в account-switch.py --итог). Решения («слать ли»,
«принять ли нажатие») принимает этот модуль, а не jq-цепочка диспетчера.

account-buttons.jsonl пишет только этот модуль, по вызову диспетчера и через events.emit.
Файлов ключей модуль не читает: наружу идут подпись и имя профиля (И-3).

Вызовы (каталог журналов — первый аргумент):
  menu  <log>                    → одна строка JSON {nonce, text, reply_markup, names}
  sent  <log> <nonce> <names>    → ответ sendMessage на stdin; строка «меню» только при ok:true
  judge <log> <owner_id>         → callback_query на stdin → JSON {answer, log, edit?, target?};
                                   принятое нажатие пишет строкой «выбор»
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounts  # noqa: E402
import config  # noqa: E402
import events  # noqa: E402

JOURNAL = "account-buttons.jsonl"
EVENT_OF = {"меню": "account.menu", "выбор": "account.choice"}
NAME_OF = {event: name for name, event in EVENT_OF.items()}
CALLBACK_RE = re.compile(r"^a:([A-Za-z0-9_-]{22}):([a-z0-9-]{1,20})$")
LOGIN = "login"
LOGIN_LABEL = "Вход /login"
MENU_TTL_MIN = 10
NO_ACCOUNTS_TEXT = "Аккаунтов не подключено"
MENU_HEADER = "Автопереход при лимите: включён"


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def append_row(log: Path, row: dict) -> None:
    """Запись через events.emit в журнал каталога log. Не записано — OSError:
    выбор, которого нет в журнале, повторное нажатие не узнало бы."""
    previous = os.environ.get("HARNESS_LOG_DIR")
    os.environ["HARNESS_LOG_DIR"] = str(log)
    try:
        data = {k: v for k, v in row.items() if k != "событие"}
        written = events.emit("scripts/lib/account_buttons.py", EVENT_OF[row["событие"]], "ok",
                              journal="account-buttons", data=data)
    finally:
        if previous is None:
            del os.environ["HARNESS_LOG_DIR"]
        else:
            os.environ["HARNESS_LOG_DIR"] = previous
    if not written:
        raise OSError(f"{JOURNAL}: запись «{row['событие']}» не попала в журнал")


def flat_row(row: dict) -> dict | None:
    """Конверт emit → плоская запись {ts, событие, nonce, …}; не наш конверт — None.
    ts — время записи из конверта, одноимённое поле data его не перекрывает."""
    name = NAME_OF.get(row.get("event"))
    data = row.get("data")
    if row.get("v") != 1 or name is None or not isinstance(data, dict):
        return None
    return {**data, "ts": row.get("ts"), "событие": name}


def read_rows(log: Path) -> list[dict]:
    try:
        lines = (log / JOURNAL).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        flat = flat_row(row) if isinstance(row, dict) else None
        if flat is not None:
            rows.append(flat)
    return rows


def label_of(name: str) -> str:
    profile = accounts.профиль(name)
    return str((profile or {}).get("подпись") or name) if name != LOGIN else LOGIN_LABEL


def owner_hhmm(unix: int) -> str:
    return config.час_владельца(unix)


def limit_note(limits: dict, name: str) -> str:
    """Пометка кнопки аккаунта, у которого сброс лимита ещё впереди; иначе пусто."""
    reset = (limits.get(name) or {}).get("сброс")
    if not isinstance(reset, int) or reset <= dt.datetime.now().timestamp():
        return ""
    return f" — в лимите до {owner_hhmm(reset)}, нажатие откажет"


def menu() -> dict:
    profiles = accounts.профили()
    if not profiles:
        return {"nonce": "", "text": NO_ACCOUNTS_TEXT, "reply_markup": "", "names": ""}
    state = accounts.состояние() or {}
    current = state.get("идёт")
    limits = state.get("лимиты") or {}
    nonce = secrets.token_urlsafe(16)
    names = [str(p["имя"]) for p in profiles] + [LOGIN]
    rows = []
    for name in names:
        label = label_of(name) + (" — сейчас" if name == current else "") + limit_note(limits, name)
        rows.append([{"text": label, "callback_data": f"a:{nonce}:{name}"}])
    return {"nonce": nonce, "text": MENU_HEADER + "\nАккаунты. Нажми тот, на который перейти:",
            "reply_markup": json.dumps({"inline_keyboard": rows}, ensure_ascii=False),
            "names": ",".join(names)}


def record_sent(log: Path, nonce: str, names: str, response: str) -> str:
    """Пустая строка — записано; иначе причина."""
    try:
        reply = json.loads(response)
    except ValueError:
        return "ответ Bot API не JSON"
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        return "Bot API не принял сообщение"
    message_id = (reply.get("result") or {}).get("message_id")
    append_row(log, {"событие": "меню", "nonce": nonce, "message_id": message_id,
                     "имена": [n for n in names.split(",") if n]})
    return ""


def reject(answer: str, why: str) -> dict:
    return {"answer": answer, "log": f"нажатие отвергнуто: {why}"}


def judge(log: Path, owner_id: str, callback: dict) -> dict:
    sender = (callback.get("from") or {}).get("id")
    message = callback.get("message") or {}
    chat = (message.get("chat") or {}).get("id")
    if str(sender) != owner_id:
        return reject("Это не твой запрос", f"чужой from.id={sender}")
    if str(chat) != owner_id:
        return reject("Это не твой запрос", f"чужой chat.id={chat}")
    found = CALLBACK_RE.match(str(callback.get("data") or ""))
    if not found:
        return reject("Не понял нажатие", "callback_data не по шаблону")
    nonce, name = found.groups()
    rows = read_rows(log)
    menus = [r for r in rows if r.get("событие") == "меню"]
    last = menus[-1] if menus else None
    if last is None or last.get("nonce") != nonce:
        return reject("Это старое меню — напиши «аккаунт» заново", f"nonce {nonce[:8]} не у последнего меню")
    if str(message.get("message_id")) != str(last.get("message_id")):
        return reject("Нажми в последнем сообщении", f"сообщение {message.get('message_id')} не меню")
    if any(r.get("событие") == "выбор" and r.get("nonce") == nonce for r in rows):
        return reject("Выбор уже сделан", f"в меню {nonce[:8]} выбор уже был")
    sent_at = dt.datetime.fromisoformat(str(last.get("ts")))
    if (now() - sent_at).total_seconds() > MENU_TTL_MIN * 60:
        return {**reject("Меню устарело — напиши «аккаунт» заново", f"меню {nonce[:8]} старше {MENU_TTL_MIN} мин"),
                "edit": "Меню устарело, выбор не записан."}
    if name not in (last.get("имена") or []):
        return reject("Нет такого аккаунта", f"имени {name} нет в меню {nonce[:8]}")
    append_row(log, {"событие": "выбор", "nonce": nonce, "имя": name,
                     "message_id": message.get("message_id"), "from_id": sender})
    return {"answer": "Принято", "log": f"выбран {name}, меню {nonce[:8]}", "target": name,
            "edit": f"Выбран {label_of(name)}: перейду, когда помощники закончат и смена напишет передачу"}


def main(argv: list[str]) -> int:
    command, log = argv[1], Path(argv[2])
    if command == "menu":
        print(json.dumps(menu(), ensure_ascii=False))
    elif command == "sent":
        why = record_sent(log, argv[3], argv[4] if len(argv) > 4 else "", sys.stdin.read())
        if why:
            print(why)
            return 1
    elif command == "judge":
        try:
            callback = json.loads(sys.stdin.read())
        except ValueError:
            callback = {}
        print(json.dumps(judge(log, argv[3], callback if isinstance(callback, dict) else {}), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
