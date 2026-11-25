#!/usr/bin/env python3
"""Статус владельцу виджетом Telegram: JSON status-report.py --json -> HTML -> sendWidget.

Спека — в каталоге задачи (трейлер Dev-Map коммита). Данные и рисунок разделены: сборщик
отдаёт задачи, этот модуль рисует их страничкой в цветах чата (только var(--tg-theme-*)).

    status_widget.py             JSON на stdin -> HTML на stdout (код 3 — не влезло в бюджет)
    status_widget.py --text      видимый текст виджета (сторож тона, пробы)
    status_widget.py --caption   подпись одной строкой
    status_widget.py --send      один виджет в чате: есть сохранённое сообщение — editMessageWidgetData,
                                 нет или пропало — sendWidget. Токен из файла TG_TOKEN_FILE, чат из
                                 TG_CHAT_ID, состояние — STATUS_WIDGET_STATE, адрес панели для кнопки
                                 «Живая карта» — STATUS_WIDGET_PANEL_URL (окружение, не argv)

Коды: 0 — готово; 2 — на stdin не тот JSON; 3 — не влезло; 5 — Telegram не принял.
Правку виджета по кнопке делает lib/widget_buttons.py через edit_view() отсюда.
"""
import html
import json
import hashlib
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

DEFAULT_BUDGET = 3800        # лимит Bot API 4096 после zlib 9; запас на разброс сжатия
RAW_LIMIT = 32768
DOING_MAX = 4                # потолок высоты ~600 px: четыре идущих и пять сданных карточек
DELIVERED_MAX = 5
CAPTION_MAX = 200
SEND_TIMEOUT = 30
DEFAULT_API = "https://api.telegram.org"
MARKS = {"passed": "✓", "current": "▶", "left": "○"}
NO_STAGES = "стадии не назначены"
CHAT_DATA_MAX = 1024         # лимит editMessageWidgetData на chat_data, байт
ROW_MAX = 80
SNAPSHOT_NAME = "status-widget-data.json"
GONE_MARKS = ("message to edit not found", "message can't be edited")
NOT_MODIFIED_MARK = "message is not modified"
BUTTONS = (("w:refresh", "Обновить"), ("w:tasks", "Задачи"), ("w:helpers", "Помощники"))

# Скрипт-оболочка: тело перерисовывается из chat_data. Как виджет читает chat_data
# (Telegram.Widget.chatData) и узнаёт о смене (событие chatDataChanged) — не проверено по
# документации; без этого API остаётся статическое тело из первичных данных (видно всегда).
# Только textContent: строки из данных не становятся разметкой.
SCRIPT = (
    "<script>(function(){"
    "var w=window.Telegram&&window.Telegram.Widget;if(!w)return;"
    "function draw(){var d=w.chatData;if(typeof d==='string')d=JSON.parse(d);"
    "if(!d||!d.rows)return;var b=document.body;b.textContent='';"
    "if(d.n){var h=document.createElement('div');h.className='row h';"
    "h.textContent=d.at+(d.age>0?' ('+d.age+' мин назад)':'')+' · в работе '+d.n.doing"
    "+' · сдано '+d.n.delivered+' · ждёт тебя '+d.n.waiting;b.appendChild(h)}"
    "d.rows.forEach(function(r){var e=document.createElement('div');e.className='row';"
    "e.textContent=r;b.appendChild(e)})}"
    "try{draw();if(w.onEvent)w.onEvent('chatDataChanged',draw)}catch(e){}"
    "})()</script>")

STYLE = (
    "<style>"
    "body{margin:0;font:14px/1.35 system-ui,sans-serif;color:var(--tg-theme-text-color)}"
    ".hd{margin:8px 0 4px;font-weight:600;color:var(--tg-theme-button-color)}"
    "section{margin:4px 0;padding:6px 8px;border-radius:8px;background:var(--tg-theme-secondary-bg-color)}"
    "section p,.row{margin:2px 0}"
    ".h{font-size:12px;color:var(--tg-theme-hint-color)}"
    ".cur{font-weight:600;color:var(--tg-theme-button-color)}"
    "</style>")

BLOCK_TAGS = {"div", "section", "p", "style"}


class VisibleText(HTMLParser):
    """Видимый текст страницы: каждый блочный тег — с новой строки, style не в счёт."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines, self.skip = [""], False

    def handle_starttag(self, tag, attrs):
        self.skip = tag == "style"
        if tag in BLOCK_TAGS:
            self.lines.append("")

    def handle_endtag(self, tag):
        self.skip = False

    def handle_data(self, data):
        if not self.skip:
            self.lines[-1] += data


def visible_text(page: str) -> str:
    parser = VisibleText()
    parser.feed(page)
    return "\n".join(line.strip() for line in parser.lines if line.strip())


def esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def stage_bar(stages: list) -> str:
    if not stages:
        return f'<p class="h">{NO_STAGES}</p>'
    cells = " ".join(
        f'<span{" class=cur" if s["state"] == "current" else ""}>{MARKS[s["state"]]} {esc(s["step"].replace("_", " "))}</span>'
        for s in stages)
    return f"<p>{cells}</p>"


def who_line(who: list) -> str:
    """Подписи идущих запусков одной строкой: «Opus 5.5 (код, тесты), Sonnet 5.5 (код)»."""
    roles: dict = {}
    for member in who:
        if member.get("running"):
            roles.setdefault(member["model"], []).append(member["role"])
    if not roles:
        return ""
    names = ", ".join(f"{m} ({', '.join(dict.fromkeys(r))})" for m, r in roles.items())
    return f'<p class="h">кто: {esc(names)}</p>'


def card(task: dict) -> str:
    part = f'<p class="h">часть: {esc(task["part"])}</p>' if task.get("part") else ""
    what = f'<p>{esc(task["what"])}</p>' if task.get("what") else ""
    return (f'<section><p><b>{esc(task["title"])}</b></p>{what}{part}'
            f'{stage_bar(task.get("stages") or [])}{who_line(task.get("who") or [])}</section>')


def waiting_block(data: dict) -> str:
    items = data.get("waiting_owner") or []
    if not items:
        return ""
    rows = "".join(f'<div class="row">• {esc(i["title"])}</div>' for i in items)
    rest = data.get("waiting_total", len(items)) - len(items)
    more = f'<div class="row h">и ещё {rest}</div>' if rest > 0 else ""
    return f'<div class="hd">Ждёт тебя</div>{rows}{more}'


def block(header: str, tasks: list) -> str:
    return f'<div class="hd">{header}</div>' + "".join(card(t) for t in tasks) if tasks else ""


def render(data: dict, doing_n: int, delivered_n: int, script: str = "") -> str:
    doing, delivered = data["doing"], data["delivered"]
    hidden = (len(doing) - doing_n + len(delivered) - delivered_n
              + data.get("doing_more", 0) + data.get("delivered_more", 0))
    more = f'<div class="row h">ещё {hidden}</div>' if hidden else ""
    nxt = (f'<div class="hd">Дальше</div><div class="row">{esc(data["next"][0])}</div>'
           if data.get("next") else "")
    return (STYLE + waiting_block(data) + block("Делается", doing[:doing_n])
            + block("Сдано", delivered[:delivered_n]) + more + nxt + script)


def zlib_budget() -> int:
    raw = os.environ.get("STATUS_WIDGET_ZLIB_BUDGET", "")
    return int(raw) if raw.isdecimal() else DEFAULT_BUDGET


def fits(page: str) -> bool:
    raw = page.encode("utf-8")
    return len(raw) <= RAW_LIMIT and len(zlib.compress(raw, 9)) <= zlib_budget()


def fit(data: dict, script: str = "") -> str | None:
    """Страница в бюджете; не влезло — карточки с конца (сперва «Сдано», идущая работа дороже), «ещё N».
    Не влезла и одна карточка — None."""
    doing_n, delivered_n = min(len(data["doing"]), DOING_MAX), min(len(data["delivered"]), DELIVERED_MAX)
    while not fits(page := render(data, doing_n, delivered_n, script)):
        if delivered_n and doing_n + delivered_n > 1:
            delivered_n -= 1
        elif doing_n > 1:
            doing_n -= 1
        else:
            return None
    return page


def caption(data: dict) -> str:
    doing = len(data["doing"]) + data.get("doing_more", 0)
    delivered = len(data["delivered"]) + data.get("delivered_more", 0)
    line = f"в работе {doing} · сдано {delivered} · ждёт тебя {data.get('waiting_total', 0)}"
    prefix = os.environ.get("STATUS_WIDGET_PREFIX", "").strip()
    return (f"{prefix} · {line}" if prefix else line)[:CAPTION_MAX]


class ApiError(RuntimeError):
    """Отказ Bot API; description — причина без токена и адреса."""

    def __init__(self, method: str, code: int, description: str) -> None:
        text = f"{method}: HTTP {code} {description}" if code else f"{method}: {description}"
        super().__init__(text.strip())
        self.description = description


def refusal(error: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(error.read().decode("utf-8")).get("description", ""))[:200]
    except (ValueError, OSError):
        return ""


def api_call(method: str, fields: dict) -> dict:
    """result принятого вызова; иначе ApiError (токен и адрес в текст не попадают)."""
    base = os.environ.get("TG_API_BASE") or DEFAULT_API
    with open(os.environ["TG_TOKEN_FILE"], encoding="utf-8") as handle:
        token = handle.read().strip()
    request = urllib.request.Request(f"{base.rstrip('/')}/bot{token}/{method}",
                                     data=urllib.parse.urlencode(fields).encode())
    try:
        with urllib.request.urlopen(request, timeout=SEND_TIMEOUT) as answer:
            reply = json.loads(answer.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise ApiError(method, error.code, refusal(error)) from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ApiError(method, 0, type(error).__name__) from None
    if reply.get("ok") is not True:
        raise ApiError(method, 0, "ответ без ok")
    return reply.get("result") or {}


def clip(text: str) -> str:
    return text if len(text) <= ROW_MAX else text[:ROW_MAX - 1] + "…"


def view_rows(data: dict, view: str) -> list:
    doing = data["doing"]
    if view == "tasks":
        return [t["title"] for t in doing] + [i["title"] for i in data.get("waiting_owner") or []]
    if view == "helpers":
        pairs = [f'{m["model"]} · {m["role"]}' for t in doing for m in t.get("who") or [] if m.get("running")]
        return list(dict.fromkeys(pairs))
    return [t["title"] for t in doing]


def chat_data(data: dict, view: str, taken: datetime | None = None) -> str:
    """JSON ≤ 1024 байт: счётчики, время снимка данных (at) и возраст в минутах (age), строки вида;
    не влезло — строки с конца и «ещё N». taken — когда собраны данные; нет — данные только что."""
    now = datetime.now().astimezone()
    taken = taken or now
    age = max(0, int((now - taken).total_seconds() // 60))
    rows = [clip(r) for r in view_rows(data, view)]
    counts = {"doing": len(data["doing"]) + data.get("doing_more", 0),
              "delivered": len(data["delivered"]) + data.get("delivered_more", 0),
              "waiting": data.get("waiting_total", 0)}
    for keep in range(len(rows), -1, -1):
        shown = rows[:keep] + ([f"ещё {len(rows) - keep}"] if keep < len(rows) else [])
        raw = json.dumps({"v": 1, "view": view, "at": taken.strftime("%H:%M"), "age": age,
                          "n": counts, "rows": shown}, ensure_ascii=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) <= CHAT_DATA_MAX or keep == 0:
            return raw


def chat_print(chat_id: str) -> str:
    """Отпечаток чата для файла состояния: сам chat_id в LOG_DIR не пишется."""
    return hashlib.sha256(chat_id.encode()).hexdigest()[:16]


def state_path() -> Path | None:
    raw = os.environ.get("STATUS_WIDGET_STATE")
    return Path(raw) if raw else None


def load_state(chat_id: str, path: Path | None = None) -> dict | None:
    """Сохранённый виджет этого чата; нет файла, чужой чат или не наш формат — None."""
    path = path or state_path()
    if path is None:
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ours = isinstance(state, dict) and state.get("v") == 1 and state.get("chat") == chat_print(chat_id)
    return state if ours and isinstance(state.get("message_id"), int) else None


def save_state(message_id: int, chat_id: str) -> None:
    path = state_path()
    if path is None:
        return
    record = {"v": 1, "message_id": message_id, "chat": chat_print(chat_id),
              "at": datetime.now().astimezone().isoformat(timespec="seconds")}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(tmp, path)


def snapshot_path(state: Path | None = None) -> Path | None:
    """Последний JSON статуса рядом с файлом состояния: из него кнопки рисуют вид без новой сборки."""
    state = state or state_path()
    return state.with_name(SNAPSHOT_NAME) if state else None


def save_snapshot(data: dict) -> None:
    path = snapshot_path()
    if path is None:
        return
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"), "data": data},
                              ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_snapshot(path: Path) -> tuple:
    """(данные, время снимка) последнего пульса; нет файла или не наш формат — OSError/ValueError/KeyError."""
    record = json.loads(path.read_text(encoding="utf-8"))
    return record["data"], datetime.fromisoformat(record["at"])


def edit_view(chat_id: str, message_id: int, data_json: str) -> bool:
    """True — данные на месте (в том числе «не изменилось»); False — сообщения больше нет;
    прочий отказ — ApiError. Тексты ошибок editMessageWidgetData не проверены по документации."""
    try:
        api_call("editMessageWidgetData", {"chat_id": chat_id, "message_id": message_id, "chat_data": data_json})
    except ApiError as error:
        reason = error.description.lower()
        if NOT_MODIFIED_MARK in reason:
            return True
        if any(mark in reason for mark in GONE_MARKS):
            return False
        raise
    return True


def live_map_button() -> dict | None:
    """Кнопка «Живая карта» на <панель>/puls; панели нет или она не на https — None.
    Telegram принимает web_app в инлайн-клавиатуре только по HTTPS и только в личном чате,
    иначе отказал бы весь sendWidget. Без callback_data: нажатие открывает страницу, а не идёт в кнопки."""
    panel = os.environ.get("STATUS_WIDGET_PANEL_URL", "").strip()
    if not panel.startswith("https://"):
        return None
    return {"text": "Живая карта", "web_app": {"url": panel.rstrip("/") + "/puls"}}


def send_widget(chat_id: str, page: str, line: str, data_json: str) -> int:
    rows = [[{"text": text, "callback_data": code} for code, text in BUTTONS]]
    # Отдельным рядом: четыре кнопки в один ряд на экране 360 px не читаются.
    if (live_map := live_map_button()) is not None:
        rows.append([live_map])
    markup = {"inline_keyboard": rows}
    result = api_call("sendWidget", {"chat_id": chat_id, "html": page, "caption": line, "chat_data": data_json,
                                     "reply_markup": json.dumps(markup, ensure_ascii=False)})
    if result.get("message_id") is None:
        raise ApiError("sendWidget", 0, "ответ без message_id")
    return int(result["message_id"])


def deliver(data: dict, page: str, line: str) -> int:
    """message_id виджета в чате: правка сохранённого, а нет его — новый виджет и новое состояние."""
    chat_id = os.environ["TG_CHAT_ID"]
    data_json = chat_data(data, "status")
    save_snapshot(data)
    state = load_state(chat_id)
    if state and edit_view(chat_id, state["message_id"], data_json):
        return state["message_id"]
    message_id = send_widget(chat_id, page, line, data_json)
    save_state(message_id, chat_id)
    return message_id


def send(data: dict, page: str) -> int:
    try:
        print(deliver(data, page, caption(data)))
    except ApiError as error:
        print(f"status_widget: {error}", file=sys.stderr)
        return 5
    except (KeyError, OSError) as error:
        print(f"status_widget: нет токена, чата или файла состояния ({type(error).__name__})", file=sys.stderr)
        return 5
    return 0


def live_map_markup() -> str:
    """JSON клавиатуры для текстового статуса; пусто, если кнопки нет или чат не личный (web_app — только в личном)."""
    button = live_map_button()
    if button is None or os.environ.get("TG_CHAT_ID", "").startswith("-"):
        return ""
    return json.dumps({"inline_keyboard": [[button]]}, ensure_ascii=False)


def main(argv: list) -> int:
    mode = argv[1] if len(argv) > 1 else ""
    if mode == "--live-map-markup":
        print(live_map_markup())
        return 0
    try:
        data = json.load(sys.stdin)
        if mode == "--caption":
            print(caption(data))
            return 0
        page = fit(data, SCRIPT if mode == "--send" else "")
    except (ValueError, KeyError, TypeError) as error:
        print(f"status_widget: на stdin не JSON статуса ({type(error).__name__})", file=sys.stderr)
        return 2
    if page is None:
        print("status_widget: не влезло в бюджет", file=sys.stderr)
        return 3
    if mode == "--send":
        return send(data, page)
    if mode == "--text":
        print(visible_text(page))
    else:
        sys.stdout.write(page)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
