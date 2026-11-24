#!/usr/bin/env python3
"""Кнопки статус-виджета на стороне диспетчера: судья нажатия и правка виджета нужным видом.

Вызов (диспетчер, ветка w:*):  widget_buttons.py press <LOG_DIR>   — callback_query JSON на stdin.
Вид рисуется из последнего JSON пульса (status-widget-data.json рядом с состоянием), без сборки.
Токен, чат и адрес API — из окружения (TG_TOKEN_FILE, TG_CHAT_ID, TG_OWNER_ID, TG_API_BASE),
не из argv. Печатает одну строку {"verdict": "accepted|refused", "log": "..."} и всегда
выходит с кодом 0: диспетчер под set -e. Ответ на нажатие даёт сам модуль, диспетчер второго
не шлёт (Bot API отверг бы повторный answerCallbackQuery).

Журнал $LOG_DIR/widget-buttons.jsonl: ни chat_id, ни from.id, ни токена в нём нет.
"""
import json
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import status_widget  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import events  # noqa: E402

JOURNAL = "widget-buttons.jsonl"
EVENT = "widget.press"
VIEW_OF = {"w:refresh": "status", "w:tasks": "tasks", "w:helpers": "helpers"}
ACTION_MAX = 32


def refusal(callback: dict, chat_id: str, owner_id: str, saved: dict | None) -> tuple | None:
    """(причина, ответ владельцу) первого несовпадения; ответ None — молчать. Принято — None."""
    message = callback.get("message") or {}
    if str((message.get("chat") or {}).get("id")) != chat_id:
        return "чужой чат", None
    if str((callback.get("from") or {}).get("id")) != (owner_id or chat_id):
        return "чужой отправитель", None
    if callback.get("data") not in VIEW_OF:
        return "данные не из списка", "Не понял нажатие"
    if saved is None or message.get("message_id") != saved["message_id"]:
        return "не последний статус", "Нажми в последнем статусе"
    return None


def redraw(chat_id: str, message_id: int, view: str, snapshot: Path) -> str:
    """Причина неудачи правки; пусто — виджет перерисован."""
    try:
        data, taken = status_widget.load_snapshot(snapshot)  # последний пульс: без синхронной сборки
        if status_widget.edit_view(chat_id, message_id, status_widget.chat_data(data, view, taken)):
            return ""
    except (status_widget.ApiError, OSError, ValueError, KeyError) as error:
        return type(error).__name__
    return "сообщение не найдено"


def answer(callback: dict, text: str | None) -> None:
    if text is None:
        return
    try:
        status_widget.api_call("answerCallbackQuery", {"callback_query_id": callback.get("id", ""), "text": text})
    except status_widget.ApiError:
        pass  # нажатие и так учтено в журнале; пустой ответ только оставит часики на кнопке


def journal(log_dir: Path, callback: dict, verdict: str, why: str) -> None:
    """Строка журнала нажатий — через events.emit в каталог log_dir. Не записано — OSError:
    main его ловит, а диспетчер получает отказ с причиной, не молчаливую потерю."""
    previous = os.environ.get("HARNESS_LOG_DIR")
    os.environ["HARNESS_LOG_DIR"] = str(log_dir)
    try:
        data = {"action": str(callback.get("data", ""))[:ACTION_MAX], "verdict": verdict, "why": why}
        written = events.emit("scripts/lib/widget_buttons.py", EVENT, "ok" if verdict == "accepted" else "refused",
                              journal="widget-buttons", data=data)
    finally:
        if previous is None:
            del os.environ["HARNESS_LOG_DIR"]
        else:
            os.environ["HARNESS_LOG_DIR"] = previous
    if not written:
        raise OSError(f"{JOURNAL}: нажатие не попало в журнал")


def printable(raw) -> str:
    """Данные нажатия для строки журнала диспетчера: без переводов строки и управляющих знаков."""
    return "".join(c if c.isprintable() else "?" for c in str(raw)[:ACTION_MAX])


def press(log_dir: Path, callback: dict) -> dict:
    chat_id = os.environ["TG_CHAT_ID"]
    state_file = os.environ.get("STATUS_WIDGET_STATE") or str(log_dir / "status-widget.json")
    saved = status_widget.load_state(chat_id, Path(state_file))
    denied = refusal(callback, chat_id, os.environ.get("TG_OWNER_ID", ""), saved)
    if denied:
        verdict, why, reply = "refused", denied[0], denied[1]
    else:
        failed = redraw(chat_id, saved["message_id"], VIEW_OF[callback["data"]],
                        status_widget.snapshot_path(Path(state_file)))
        verdict, why, reply = "accepted", "перерисован" if not failed else f"правка не вышла: {failed}", \
            "Обновлено" if not failed else "Не обновилось"
    answer(callback, reply)
    journal(log_dir, callback, verdict, why)
    return {"verdict": verdict, "log": f"{printable(callback.get('data', ''))}: {verdict}, {why}"}


def main(argv: list) -> int:
    if len(argv) != 3 or argv[1] != "press":
        print("usage: widget_buttons.py press <LOG_DIR>", file=sys.stderr)
        return 2
    try:
        result = press(Path(argv[2]), json.load(sys.stdin))
    except (ValueError, KeyError, OSError, TypeError, AttributeError) as error:
        result = {"verdict": "refused", "log": f"нажатие не разобрано: {type(error).__name__}"}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
