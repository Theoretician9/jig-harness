#!/usr/bin/env python3
"""test_consent_buttons.py — пробы v1 задачи hr.consent-buttons (кнопки «Да / Нет»).

Слово владельца 05.11.2026: «Мне вообще надо просто по нажатию». Сессия зовёт
ask-consent.py, диспетчер шлёт сообщение с кнопками, нажатие «Да» ложится в
$LOG_DIR/consent-buttons.jsonl, и protected-delete.py принимает его как слово об
ЭТОМ пути. Договор — .tasks/hr.consent-buttons/pretask.yaml.

Работает НАСТОЯЩИЙ tg-dispatcher.sh на своём стенде: свой LOG_DIR, свой порт
подставного Bot API (сверяется меткой стенда — чужой ответ = отказ фикстуры),
tmux и модель подставлены как в test_ack_receipts.sh. Подставной API, как
Telegram, отдаёт только типы из allowed_updates, считает answerCallbackQuery и
editMessageText и снимает журнал кнопок в момент ответа на нажатие.
Механизмы берутся РЯДОМ с пробой (подмена «из-копии»): мутацию ставят в копию
дерева и зовут пробу из неё.

    python3 scripts/test_consent_buttons.py [--случай <имя>]...
Случаи: запрос да чужой повтор срок битый дверь носитель охрана; виды-1 … виды-11 —
пробы 1-11 задачи hr.consent-buttons-for-every-yes (виды согласия «удаление» и «пуш»,
.tasks/hr.consent-buttons-for-every-yes/spec.md §7).
Код возврата: 0 — «падений 0», 1 — красная (или отказ фикстуры: строка «ФИКСТУРА»).
"""
from __future__ import annotations

import argparse
import atexit
import datetime as dt
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DISPATCHER = ROOT / "scripts" / "tg-dispatcher.sh"
ASK = ROOT / "scripts" / "ask-consent.py"
DELETE = ROOT / "scripts" / "protected-delete.py"
GUARD = ROOT / "scripts" / "hooks" / "guard_bash.py"
LIB = ROOT / "scripts" / "lib"
SWEEP = LIB / "stand_sweep.sh"
sys.path.insert(0, str(LIB))
import config  # noqa: E402
from selftest_cases import Cases  # noqa: E402
from test_consent_push import GIT_ENV, REAL_GIT, GitBench, button, git  # noqa: E402

OWNER = 777000
STRANGER = 555000
TTL_MIN = 15
WAIT_S = 20
PREFIX = "/tmp/proba-consent-buttons-"
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
# Причина судьи при битом журнале кнопок (спека §2: НетНосителя).
UNREAD = "журнал кнопок не прочитан"
CALLBACK_RE = re.compile(r"^c:([A-Za-z0-9_-]{22}):(y|n)$")
KIND_CASES = tuple(f"виды-{n}" for n in range(1, 12))
CASE_NAMES = ("запрос", "да", "чужой", "повтор", "срок", "битый", "дверь", "носитель", "охрана",
              *KIND_CASES)

cases = Cases()
check = cases.check


def now_iso(ago_s: float = 0) -> str:
    """Время в форме `date -Is`: так пишут inbox.jsonl и (по договору) журналы кнопок."""
    when = dt.datetime.now().astimezone() - dt.timedelta(seconds=ago_s)
    return when.isoformat(timespec="seconds")


def read_jsonl(path: Path) -> list:
    """Строки журнала; битые пропускаются — их судит дверь, не проба."""
    rows = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# Журналы кнопок — конверты events.emit: имя события в event, nonce и цель — в data.
REQUEST, SENT, YES, NO = "consent.request", "consent.sent", "consent.yes", "consent.no"


def data_of(row: dict) -> dict:
    data = row.get("data")
    return data if isinstance(data, dict) else {}


def envelope(src: str, event: str, ts: str, data: dict) -> str:
    """Строка журнала в форме, которую пишет events.emit."""
    return json.dumps({"v": 1, "ts": ts, "src": src, "event": event, "outcome": "ok", "data": data},
                      ensure_ascii=False)


def line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def wait_for(cond, limit: float = WAIT_S) -> bool:
    end = time.time() + limit
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.3)
    return bool(cond())


# ── подставной Bot API ──────────────────────────────────────────────────────
# Порт берёт сам сервер (bind на 0): занять не смог — упал, и проба падает
# вместе с ним, а не говорит с чужой службой. Метку стенда проба сверяет
# отдельным запросом.
API_PY = r'''
import json, os, sys, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

stand, stamp, buttons = sys.argv[1], sys.argv[2], sys.argv[3]
updates, calls, filtered = stand + "/updates.jsonl", stand + "/calls.jsonl", stand + "/filtered"
drop_flag, polls = stand + "/drop-next-id", stand + "/polls"
lock = threading.Lock()
state = {"next_id": 1000}


def kind(update):
    return next((k for k in update if k != "update_id"), "")


def pending(offset, allowed):
    try:
        rows = [json.loads(s) for s in open(updates, encoding="utf-8") if s.strip()]
    except FileNotFoundError:
        rows = []
    out = []
    for u in rows:
        if u["update_id"] < offset:
            continue
        if allowed is not None and kind(u) not in allowed:
            # Telegram такие обновления не отдаёт вовсе; отметка — чтобы проба
            # не ждала их обработки впустую.
            with open(filtered, "a") as fh:
                fh.write(f"{u['update_id']}\n")
            continue
        out.append(u)
    return out


class Handler(BaseHTTPRequestHandler):
    def reply(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def form(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        query = urllib.parse.urlparse(self.path).query
        if "json" in (self.headers.get("Content-Type") or ""):
            try:
                data = json.loads(raw or "{}")
            except ValueError:
                data = {}
            data = {k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                    for k, v in data.items()}
        else:
            data = {k: v[-1] for k, v in urllib.parse.parse_qs(raw + "&" + query).items()}
        return data, raw

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/proba-stand":
            self.reply({"stamp": stamp})
            return
        method = path.rsplit("/", 1)[-1]
        data, raw = self.form()
        if method == "getUpdates":
            with open(polls, "a") as fh:
                fh.write("1\n")
            allowed = None
            if "allowed_updates" in data:
                try:
                    allowed = json.loads(data["allowed_updates"])
                except ValueError:
                    allowed = []
            found = pending(int(data.get("offset") or 0), allowed)
            if not found:
                time.sleep(0.3)
            self.reply({"ok": True, "result": found})
            return
        row = {"method": method, "form": data, "raw": raw, "t": time.time()}
        with lock:
            if method == "sendMessage":
                state["next_id"] += 1
                mid = state["next_id"]
                row["assigned"] = mid
                drop = "reply_markup" in data and os.path.exists(drop_flag)
                if drop:
                    os.unlink(drop_flag)
                row["reported"] = not drop
                result = {} if drop else {"message_id": mid, "date": int(time.time()),
                                           "chat": {"id": int(data.get("chat_id") or 0)},
                                           "text": data.get("text", "")}
                body = {"ok": True, "result": result}
            else:
                if method == "answerCallbackQuery":
                    try:
                        row["buttons_at_answer"] = open(buttons, encoding="utf-8").read()
                    except OSError:
                        row["buttons_at_answer"] = ""
                body = {"ok": True, "result": True}
            with open(calls, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.reply(body)

    do_GET = do_POST

    def log_message(self, *_):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
with open(stand + "/api.port", "w") as fh:
    fh.write(str(server.server_address[1]))
server.serve_forever()
'''

TMUX_SH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${PROBA_DIR:?}/tmux-calls"
case "$1" in
    display-message) case "$*" in *pane_dead*) echo 0 ;; *) echo claude ;; esac ;;
esac
exit 0
"""

MODEL_PY = """import json
print(json.dumps({"задачи": [], "указания": [], "вопросы": [], "ответы": []}))
"""


class Yard:
    """Каталог прогона: владелец, уборка процессов по метке, стенды внутри."""

    def __init__(self) -> None:
        self.sweep_dead()
        self.root = Path(subprocess.run(["mktemp", "-d", PREFIX + "XXXXXX"],
                                        capture_output=True, text=True, check=True).stdout.strip())
        (self.root / "owner").write_text(str(os.getpid()))
        self.pids: list[int] = []
        atexit.register(self.cleanup)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda *_: sys.exit(1))

    @staticmethod
    def sweep_dead() -> None:
        out = subprocess.run(["bash", "-c", f'source "{SWEEP}"; sweep_dead_stands "$1"', "_", PREFIX],
                             capture_output=True, text=True).stdout.strip()
        if out:
            print(out)

    def cleanup(self) -> None:
        for pid in self.pids:
            for target in (-pid, pid):
                try:
                    os.kill(target, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        out = subprocess.run(["bash", "-c", f'source "{SWEEP}"; sweep_stand "$1"', "_", f"{self.root}/"],
                             capture_output=True, text=True).stdout.strip()
        if out not in ("", "0"):
            print(f"убрано процессов стенда при выходе: {out}", file=sys.stderr)
        subprocess.run(["rm", "-rf", str(self.root)])


def fixture_fail(why: str, stand: "Stand | None" = None) -> None:
    print(f"ФИКСТУРА: {why}")
    if stand:
        stand.show()
    sys.exit(1)


class Stand:
    """Диспетчер на своём LOG_DIR и своём Bot API."""

    def __init__(self, yard: Yard) -> None:
        self.yard = yard
        s = self.dir = yard.root / "disp"
        self.log = s / "log"
        self.secrets = s / "secrets"
        self.backups = s / "backups"
        for d in (self.log, self.secrets, self.backups, s / "project" / "scripts", s / "panel", s / "bin"):
            d.mkdir(parents=True, exist_ok=True)
        (s / "project" / "dev-map.yaml").write_text("lines: []\n", encoding="utf-8")
        (s / "project" / "scripts" / "pause.py").symlink_to(ROOT / "scripts" / "pause.py")
        (s / "bin" / "tmux").write_text(TMUX_SH, encoding="utf-8")
        (s / "bin" / "tmux").chmod(0o755)
        (s / "model.py").write_text(MODEL_PY, encoding="utf-8")
        (s / "api.py").write_text(API_PY, encoding="utf-8")
        (s / "token").write_text("000:СТЕНДОВЫЙ")       # не секрет: подставной API стенда
        (s / "updates.jsonl").write_text("", encoding="utf-8")
        # Носители чужих дверей: проба «да» считает, что строк в них не прибавилось.
        (self.log / "confirmations.jsonl").write_text('{"text":"затравка стенда"}\n', encoding="utf-8")
        (self.log / "inbox.jsonl").write_text("", encoding="utf-8")
        self.buttons = self.log / "consent-buttons.jsonl"
        self.requests = self.log / "consent-requests.jsonl"
        self.stamp = secrets.token_hex(8)
        self.next_update = 100
        label = {"PROBA_STAND": f"{yard.root}/"}
        api = subprocess.Popen(["python3", str(s / "api.py"), str(s), self.stamp, str(self.buttons)],
                               stdout=open(s / "api.out", "w"), stderr=subprocess.STDOUT,
                               env=dict(os.environ, **label), start_new_session=True)
        yard.pids.append(api.pid)
        if not wait_for(lambda: (s / "api.port").exists(), 10):
            fixture_fail(f"подставной Bot API не занял порт: {(s / 'api.out').read_text()[:300]}")
        self.port = int((s / "api.port").read_text())
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/proba-stand", data=b"")
            stamp = json.loads(urllib.request.urlopen(req, timeout=5).read()).get("stamp")
        except (OSError, ValueError) as err:
            stamp = f"ошибка {err}"
        if stamp != self.stamp:
            fixture_fail(f"на порту {self.port} отвечает не наш API (метка «{stamp}»)")
        print(f"  СТЕНД: Bot API на своём порту {self.port}, метка сверена")
        (s / "install.conf").write_text(
            f'PROJECT_DIR="{s}/project"\nPROJECT_NAME="стенд"\nLOG_DIR="{self.log}"\n'
            f'TG_CHAT_ID="{OWNER}"\nTG_TOKEN_FILE="{s}/token"\nAGENT_USER="{os.environ.get("USER", "agent")}"\n'
            f'PANEL_STATE_DIR="{s}/panel"\nSECRETS_DIR="{self.secrets}"\nBACKUP_DIR="{self.backups}"\n',
            encoding="utf-8")
        (s / "harness.conf").write_text(
            f'TMUX_SESSION="proba-consent-{os.getpid()}"\nSERVICE_LOCKS_SUBDIR="services"\n'
            f'PANE_TMUX_TIMEOUT="3"\nPANE_ENTER_DELAY="0.1"\nCONSENT_BUTTON_TTL_MIN={TTL_MIN}\n',
            encoding="utf-8")
        # Окружение старше конфига: внешний LOG_DIR иначе увёл бы стенд в боевые журналы.
        self.env = dict(
            os.environ, **label, LOG_DIR=str(self.log), PROBA_DIR=str(s),
            PATH=f"{s}/bin:{os.environ.get('PATH', '')}", HARNESS_PANEL_STATE=str(s / "panel"),
            HARNESS_HOME=str(s), HARNESS_INSTALL_CONF=str(s / "install.conf"),
            HARNESS_CONF=str(s / "harness.conf"), HARNESS_SPROSIT_MODEL=str(s / "model.py"),
            TG_API_BASE=f"http://127.0.0.1:{self.port}", SECRETS_DIR=str(self.secrets),
            HARNESS_OWNER_WORD_STEND="1", CONSENT_BUTTON_TTL_MIN=str(TTL_MIN))
        disp = subprocess.Popen(["bash", str(DISPATCHER)], stdout=open(s / "dispatcher.out", "w"),
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=self.env, start_new_session=True)
        self.disp_pid = disp.pid
        yard.pids.append(disp.pid)
        started = wait_for(lambda: "старт: слушаю" in self.text("log/tg-dispatcher.log")
                           and line_count(s / "polls") >= 1)
        if not started:
            fixture_fail("диспетчер стенда не начал слушать", self)
        print(f"  СТЕНД: диспетчер слушает (pid {disp.pid}), getUpdates идёт на стенд")

    def text(self, rel: str) -> str:
        try:
            return (self.dir / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def show(self) -> None:
        for rel in ("log/tg-dispatcher.log", "dispatcher.out", "api.out"):
            tail = self.text(rel).splitlines()[-8:]
            print(f"    --- {rel} (хвост) ---")
            for line in tail:
                print(f"    {line[:160]}")

    # ── вызовы к API ────────────────────────────────────────────────────────
    def calls(self, method: str | None = None) -> list:
        rows = read_jsonl(self.dir / "calls.jsonl")
        return [r for r in rows if method is None or r.get("method") == method]

    def polls(self) -> int:
        return line_count(self.dir / "polls")

    def keyboard_sends(self, nonce: str | None) -> list:
        if not nonce:
            return []
        return [r for r in self.calls("sendMessage") if nonce in r["form"].get("reply_markup", "")]

    def answers(self, callback_id: str | None) -> int:
        return sum(1 for r in self.calls("answerCallbackQuery")
                   if callback_id and r["form"].get("callback_query_id") == callback_id)

    def edits(self, message_id) -> int:
        """Правки этого сообщения, после которых кнопок на нём нет."""
        return sum(1 for r in self.calls() if r["method"] in ("editMessageText", "editMessageReplyMarkup")
                   and message_id is not None and r["form"].get("message_id") == str(message_id)
                   and not keyboard(r["form"]))

    # ── журнал кнопок ───────────────────────────────────────────────────────
    def records(self, nonce: str | None, events=(YES, NO)) -> list:
        return [r for r in read_jsonl(self.buttons) if nonce and data_of(r).get("nonce") == nonce
                and r.get("event") in events]

    def sent_id(self, nonce: str | None):
        sent = self.records(nonce, (SENT,))
        return data_of(sent[-1]).get("message_id") if sent else None

    # ── действия ────────────────────────────────────────────────────────────
    def ask(self, target: Path | str, why: str = "проба кнопок") -> tuple[int, str, str | None]:
        if not ASK.exists():
            return 127, f"нет {ASK}", None
        done = subprocess.run(["python3", str(ASK), "--цель", str(target), "--почему", why],
                              capture_output=True, text=True, env=self.env, timeout=30)
        lines = done.stdout.strip().splitlines()
        nonce = lines[-1].strip() if done.returncode == 0 and lines else None
        return done.returncode, (done.stdout + done.stderr).strip(), nonce

    def ask_sent(self, target: Path, why: str = "проба кнопок") -> tuple[str | None, object]:
        """Запрос, дошедший до «отправлено»: (nonce, message_id)."""
        _rc, _out, nonce = self.ask(target, why)
        if nonce:
            wait_for(lambda: self.sent_id(nonce) is not None)
        return nonce, self.sent_id(nonce)

    def push(self, update: dict) -> int:
        self.next_update += 1
        update = dict(update, update_id=self.next_update)
        with open(self.dir / "updates.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(update, ensure_ascii=False) + "\n")
        return self.next_update

    def processed(self, update_id: int) -> bool:
        try:
            offset = int(self.text("log/tg_offset").strip() or 0)
        except ValueError:
            offset = 0
        filtered = self.text("filtered").split()
        return offset > update_id or str(update_id) in filtered

    def press(self, data: str | None, message_id, from_id: int = OWNER, chat_id: int = OWNER,
              with_message: bool = True) -> str:
        """Нажатие владельца (или чужое); ждёт, пока диспетчер его обработал."""
        callback = {"id": f"cb-{self.next_update + 1}", "chat_instance": "1",
                    "from": {"id": from_id, "is_bot": False, "first_name": "Проба"}}
        if data is not None:
            callback["data"] = data
        if with_message:
            callback["message"] = {"message_id": message_id, "date": int(time.time()),
                                   "chat": {"id": chat_id, "type": "private"}, "text": "запрос"}
        update_id = self.push({"callback_query": callback})
        if not wait_for(lambda: self.processed(update_id)):
            print(f"    (нажатие {callback['id']} не обработано за {WAIT_S} с)")
        return callback["id"]

    def say_owner(self, text: str) -> int:
        update_id = self.push({"message": {"message_id": self.next_update + 1, "date": int(time.time()),
                                           "chat": {"id": OWNER, "type": "private"}, "text": text}})
        wait_for(lambda: self.processed(update_id))
        return update_id

    def secret(self, name: str, size: int = 64) -> Path:
        path = self.secrets / name
        path.write_bytes(b"s" * size)
        return path


def keyboard(form: dict) -> list:
    """callback_data кнопок из reply_markup запроса; нет разметки — пусто."""
    try:
        rows = json.loads(form.get("reply_markup") or "{}").get("inline_keyboard") or []
        return [b.get("callback_data") for row in rows for b in row]
    except (ValueError, AttributeError, TypeError):
        return []


def yes_data(nonce: str | None, answer: str = "y") -> str | None:
    return f"c:{nonce}:{answer}" if nonce else None


# ── 1. запрос ───────────────────────────────────────────────────────────────
def probe_request(st: Stand) -> None:
    print("— 1. запрос → сообщение с кнопками")
    name = f"kljuch-dostupa-{secrets.token_hex(4)}.env"
    target = st.secret(name, 1234)
    before = line_count(st.requests)
    rc, out, nonce = st.ask(target, "проба: убрать старый ключ")
    check("1а) ask-consent.py: код 0", 0, rc)
    check("1а) напечатан nonce из 22 знаков", True, bool(nonce and NONCE_RE.match(nonce)))
    req = [data_of(r) for r in read_jsonl(st.requests)
           if nonce and r.get("event") == REQUEST and data_of(r).get("nonce") == nonce]
    check("1б) в consent-requests.jsonl ровно одна строка запроса", (1, before + 1),
          (len(req), line_count(st.requests)))
    check("1б) цель записана realpath-ом, почему — как назвали",
          (str(target.resolve()), "проба: убрать старый ключ"),
          (req[0].get("цель"), req[0].get("почему")) if req else None)
    if nonce:
        wait_for(lambda: st.sent_id(nonce) is not None)
    sends = st.keyboard_sends(nonce)
    check("1в) диспетчер отправил ОДНО сообщение с клавиатурой", 1, len(sends))
    form = sends[0]["form"] if sends else {}
    datas = keyboard(form)
    check("1г) кнопки — ровно c:<nonce>:y и c:<nonce>:n", sorted([f"c:{nonce}:n", f"c:{nonce}:y"]),
          sorted(d for d in datas if d))
    check("1г) callback_data каждой кнопки ≤ 64 байт и по шаблону", True,
          bool(datas) and all(d and len(d.encode()) <= 64 and CALLBACK_RE.match(d) for d in datas))
    check("1д) сообщение без parse_mode", False, "parse_mode" in form)
    check("1д) сообщение ушло владельцу", str(OWNER), form.get("chat_id"))
    leaked = [r["method"] for r in st.calls() if name in json.dumps(r, ensure_ascii=False)]
    check("1е) имени файла секрета нет ни в одном запросе к Bot API", [], leaked)
    text = form.get("text", "")
    check("1е) текст называет каталог и размер секрета", (True, True),
          (str(target.parent) in text, "1234" in text))
    check("1ж) «отправлено» с message_id, который вернул API",
          sends[0].get("assigned") if sends else "отправки нет", st.sent_id(nonce))

    # Копия: путь в тексте целиком; «почему» обрезано до 200 знаков.
    old = st.backups / f"staraja-{secrets.token_hex(3)}.dump"
    old.write_text("x", encoding="utf-8")
    long_ago = time.time() - 40 * 86400
    os.utime(old, (long_ago, long_ago))
    why = "а" * 200 + "ХВОСТ-ЗА-ПРЕДЕЛОМ" + "б" * 80
    _rc, _out, nonce_b = st.ask(old, why)
    if nonce_b:
        wait_for(lambda: st.sent_id(nonce_b) is not None)
    sends_b = st.keyboard_sends(nonce_b)
    text_b = sends_b[0]["form"].get("text", "") if sends_b else ""
    check("1з) копия: полный путь цели в тексте", True, str(old.resolve()) in text_b)
    check("1з) «почему» обрезано до 200 знаков", (True, False),
          ("а" * 200 in text_b, "ХВОСТ-ЗА-ПРЕДЕЛОМ" in text_b))

    # Секретом дверь считает всё ПОД каталогом секретов, не только его корень.
    deep_name = f"vlozhennyj-kljuch-{secrets.token_hex(4)}.env"
    deep = st.secrets / f"sub-{secrets.token_hex(3)}" / deep_name
    deep.parent.mkdir()
    deep.write_bytes(b"s" * 4321)
    rc_d, _out, nonce_d = st.ask(deep)
    check("1к) условие: запрос на секрет во вложенном каталоге принят", 0, rc_d)
    if nonce_d:
        wait_for(lambda: st.sent_id(nonce_d) is not None)
    sends_d = st.keyboard_sends(nonce_d)
    check("1к) условие: сообщение с кнопками отправлено", 1, len(sends_d))
    text_d = sends_d[0]["form"].get("text", "") if sends_d else ""
    leaked_d = [r["method"] for r in st.calls() if deep_name in json.dumps(r, ensure_ascii=False)]
    check("1к) вложенный секрет: имени нет ни в одном запросе к Bot API", [], leaked_d)
    stamp = dt.datetime.fromtimestamp(deep.stat().st_mtime).strftime("%Y-%m-%d")
    check("1к) вложенный секрет: не «Копия», в тексте каталог секретов, размер и дата",
          (False, True, True, True),
          ("Копия" in text_d, str(st.secrets) in text_d, "4321" in text_d, stamp in text_d))

    # Отказы ask-consent.py: строки нет, кода 0 нет.
    link = st.secrets / f"ssylka-{secrets.token_hex(3)}"
    link.symlink_to(target)
    folder = st.secrets / f"papka-{secrets.token_hex(3)}"
    folder.mkdir()
    outside = st.dir / f"vne-{secrets.token_hex(3)}.txt"
    outside.write_text("x", encoding="utf-8")
    fresh = st.backups / f"svezhaja-{secrets.token_hex(3)}.dump"
    fresh.write_text("x", encoding="utf-8")
    inner_dir = st.backups / "2026-11-01_0400"
    inner_dir.mkdir(exist_ok=True)
    inner = inner_dir / "db.dump"
    inner.write_text("x", encoding="utf-8")
    os.utime(inner, (long_ago, long_ago))
    for label, bad in (("симлинк", str(link)), ("несуществующий", str(st.secrets / "net-takogo")),
                       ("каталог", str(folder)), ("вне секретов и копий", str(outside)),
                       ("относительный путь", name),
                       ("свежая копия (дверь не примет, решение 3)", str(fresh)),
                       ("файл внутри ежесуточной копии", str(inner))):
        lines = line_count(st.requests)
        rc_bad, out_bad, _ = st.ask(bad)
        check(f"1и) ask-consent отказывает: {label} — код 2, строки нет", (2, lines),
              (rc_bad, line_count(st.requests)))


# ── 2. «Да» владельца ───────────────────────────────────────────────────────
def probe_yes(st: Stand) -> None:
    print("— 2. «Да» владельца")
    target = st.secret(f"p2-{secrets.token_hex(3)}.txt")
    nonce, mid = st.ask_sent(target)
    check("2) условие: запрос отправлен, message_id есть", True, mid is not None)
    confirm, inbox = line_count(st.log / "confirmations.jsonl"), line_count(st.log / "inbox.jsonl")
    cb = st.press(yes_data(nonce), mid) if nonce else None
    rec = st.records(nonce)
    check("2а) одна запись решения, событие «да»", [YES], [r.get("event") for r in rec])
    check("2а) цель — из запроса (realpath), не из callback_data", str(target.resolve()),
          data_of(rec[0]).get("цель") if rec else None)
    check("2а) from_id и message_id записи", (OWNER, mid),
          (data_of(rec[0]).get("from_id"), data_of(rec[0]).get("message_id")) if rec else None)
    check("2а) ts записи — ISO с поясом", True,
          bool(rec) and bool(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:?\d\d$", str(rec[0].get("ts")))))
    check("2б) answerCallbackQuery ровно 1 раз", 1, st.answers(cb))
    snap = [r.get("buttons_at_answer", "") for r in st.calls("answerCallbackQuery")
            if cb and r["form"].get("callback_query_id") == cb]
    at_answer = []
    for line in (snap[0] if snap else "").splitlines():
        try:
            at_answer.append(json.loads(line))
        except ValueError:
            continue
    check("2б) запись «да» легла ДО answerCallbackQuery", True,
          bool(nonce) and any(isinstance(r, dict) and data_of(r).get("nonce") == nonce
                              and r.get("event") == YES for r in at_answer))
    check("2в) клавиатура снята (editMessageText по этому сообщению)", True, st.edits(mid) >= 1)
    check("2г) в confirmations.jsonl и inbox.jsonl строк не прибавилось", (confirm, inbox),
          (line_count(st.log / "confirmations.jsonl"), line_count(st.log / "inbox.jsonl")))

    # Журнал запросов пишет сессия; «да» обязано лечь на цель, показанную владельцу.
    shown = st.secret(f"p2d-pokazan-{secrets.token_hex(3)}.txt")
    swapped = st.secret(f"p2d-podmena-{secrets.token_hex(3)}.txt")
    nonce_d, mid_d = st.ask_sent(shown)
    check("2д) условие: запрос отправлен", True, mid_d is not None)
    rewrite_request(st, nonce_d, swapped)
    check("2д) условие: строка запроса переписана на другую цель", str(swapped.resolve()),
          next((data_of(r).get("цель") for r in read_jsonl(st.requests)
                if r.get("event") == REQUEST and data_of(r).get("nonce") == nonce_d), None))
    st.press(yes_data(nonce_d), mid_d) if nonce_d else None
    rec_d = st.records(nonce_d)
    check("2д) запрос переписан после отправки: «да» легло на показанную цель", [str(shown.resolve())],
          [data_of(r).get("цель") for r in rec_d])


# ── 3. чужой from.id / chat.id ──────────────────────────────────────────────
def probe_stranger(st: Stand) -> None:
    print("— 3. чужое нажатие")
    target = st.secret(f"p3-{secrets.token_hex(3)}.txt")
    nonce, mid = st.ask_sent(target)
    check("3) условие: запрос отправлен", True, mid is not None)
    cb = st.press(yes_data(nonce), mid, from_id=STRANGER) if nonce else None
    check("3а) чужой from.id: записи нет, ответ вызван", ([], 1), (st.records(nonce), st.answers(cb)))
    cb = st.press(yes_data(nonce), mid, chat_id=STRANGER) if nonce else None
    check("3б) чужой chat.id: записи нет, ответ вызван", ([], 1), (st.records(nonce), st.answers(cb)))
    # Контроль: тот же запрос жив, отказы выше — не по чужой причине.
    st.press(yes_data(nonce), mid) if nonce else None
    check("3в) контроль: нажатие владельца по тому же запросу записано", [YES],
          [r.get("event") for r in st.records(nonce)])


# ── 4. повтор и прежнее сообщение ───────────────────────────────────────────
def probe_repeat(st: Stand) -> None:
    print("— 4. повтор нажатия, прежнее сообщение, «Нет»")
    target = st.secret(f"p4-{secrets.token_hex(3)}.txt")
    nonce, mid = st.ask_sent(target)
    st.press(yes_data(nonce), mid) if nonce else None
    cb2 = st.press(yes_data(nonce), mid) if nonce else None
    cb3 = st.press(yes_data(nonce, "n"), mid) if nonce else None
    check("4а) второе «Да» и «Нет» после «Да»: запись одна, «да»", [YES],
          [r.get("event") for r in st.records(nonce)])
    check("4а) на повторные нажатия ответ вызван", (1, 1), (st.answers(cb2), st.answers(cb3)))

    # Первый ответ API без message_id: «отправлено» не пишется, запрос шлётся заново.
    (st.dir / "drop-next-id").write_text("", encoding="utf-8")
    target_b = st.secret(f"p4b-{secrets.token_hex(3)}.txt")
    _rc, _out, nonce_b = st.ask(target_b)
    if nonce_b:
        wait_for(lambda: len(st.keyboard_sends(nonce_b)) >= 2 and st.sent_id(nonce_b) is not None)
    sends = st.keyboard_sends(nonce_b)
    first = sends[0].get("assigned") if sends else None
    last = sends[-1].get("assigned") if len(sends) >= 2 else None
    check("4б) ответ без message_id — запрос отправлен заново", True, len(sends) >= 2)
    check("4б) «отправлено» одно, с message_id второй отправки", (1, last),
          (len(st.records(nonce_b, (SENT,))), st.sent_id(nonce_b)))
    cb_old = st.press(yes_data(nonce_b), first) if nonce_b and first else None
    check("4в) нажатие на прежнем сообщении: записи нет, ответ вызван", ([], 1),
          (st.records(nonce_b), st.answers(cb_old)))
    st.press(yes_data(nonce_b), last) if nonce_b and last else None
    check("4в) контроль: нажатие на последнем сообщении записано", [YES],
          [r.get("event") for r in st.records(nonce_b)])

    target_n = st.secret(f"p4n-{secrets.token_hex(3)}.txt")
    nonce_n, mid_n = st.ask_sent(target_n)
    cb_n = st.press(yes_data(nonce_n, "n"), mid_n) if nonce_n else None
    check("4г) «Нет»: запись «нет», ответ 1, клавиатура снята", ([NO], 1, True),
          ([r.get("event") for r in st.records(nonce_n)], st.answers(cb_n), st.edits(mid_n) >= 1))


# ── 5. срок ─────────────────────────────────────────────────────────────────
def age_sent(st: Stand, nonce: str, minutes: int) -> None:
    """Старит «отправлено» в журнале стенда: нажатие, обработанное после срока."""
    lines = st.buttons.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        if row.get("event") == SENT and data_of(row).get("nonce") == nonce:
            row["ts"] = now_iso(minutes * 60)
        out.append(json.dumps(row, ensure_ascii=False))
    st.buttons.write_text("\n".join(out) + "\n", encoding="utf-8")


def rewrite_request(st: Stand, nonce: str | None, target: Path) -> None:
    """Сессия переписывает цель своего запроса (как `sed -i`) после отправки владельцу."""
    out = []
    for line in st.requests.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        if nonce and row.get("event") == REQUEST and data_of(row).get("nonce") == nonce:
            row["data"]["цель"] = str(target.resolve())
        out.append(json.dumps(row, ensure_ascii=False))
    st.requests.write_text("\n".join(out) + "\n", encoding="utf-8")


def probe_ttl(st: Stand) -> None:
    print("— 5. срок кнопки")
    target = st.secret(f"p5-{secrets.token_hex(3)}.txt")
    nonce, mid = st.ask_sent(target)
    check("5) условие: запрос отправлен", True, mid is not None)
    if nonce and mid is not None:
        age_sent(st, nonce, TTL_MIN + 5)
    cb = st.press(yes_data(nonce), mid) if nonce else None
    check(f"5а) нажатие через {TTL_MIN + 5} мин после отправки: записи нет", [], st.records(nonce))
    check("5а) ответ вызван и клавиатура снята", (1, True), (st.answers(cb), st.edits(mid) >= 1))

    # Запрос старше срока без «отправлено» — не шлётся. Контроль: свежий рядом шлётся.
    stale_target = st.secret(f"p5s-{secrets.token_hex(3)}.txt")
    stale = secrets.token_urlsafe(16)
    with open(st.requests, "a", encoding="utf-8") as fh:
        fh.write(envelope("scripts/ask-consent.py", REQUEST, now_iso((TTL_MIN + 5) * 60),
                          {"nonce": stale, "цель": str(stale_target.resolve()),
                           "почему": "старый запрос"}) + "\n")
    _fresh_nonce, fresh_mid = st.ask_sent(st.secret(f"p5f-{secrets.token_hex(3)}.txt"))
    polls = st.polls()
    wait_for(lambda: st.polls() >= polls + 3)
    check("5б) контроль: свежий запрос того же круга отправлен", True, fresh_mid is not None)
    check("5б) запрос старше срока не отправлен за 3 круга", 0, len(st.keyboard_sends(stale)))


# ── 6. битый callback ───────────────────────────────────────────────────────
def probe_broken(st: Stand) -> None:
    print("— 6. битые нажатия не роняют диспетчер")
    before = len(read_jsonl(st.buttons))
    cbs = [st.press(None, 1),
           st.press("мусор", 1),
           st.press(f"c:{secrets.token_urlsafe(16)}:y", 1),
           st.press(f"c:{secrets.token_urlsafe(16)}:y", None, with_message=False),
           st.press("c:" + "x" * 40 + ":y", 1)]
    st.say_owner("проба шесть, метка-6")
    alive = True
    try:
        os.kill(st.disp_pid, 0)
    except ProcessLookupError:
        alive = False
    check("6а) диспетчер жив после битых нажатий", True, alive)
    def acks() -> list:
        return [r for r in st.calls("sendMessage") if r["form"].get("text", "").startswith("принял:")
                and "метка-6" in r["form"].get("text", "")]
    # «принял» уходит фоном (kvitanciya_async) — после сдвига offset.
    wait_for(lambda: len(acks()) >= 1)
    check("6б) следующее сообщение владельца получило «принял:»", 1, len(acks()))
    check("6б) и легло в inbox.jsonl", True, "метка-6" in st.text("log/inbox.jsonl"))
    check("6в) на каждое битое нажатие ответ вызван ровно раз", [1] * len(cbs), [st.answers(c) for c in cbs])
    check("6г) записей в журнале кнопок не прибавилось", before, len(read_jsonl(st.buttons)))


# ── 7–8. дверь protected-delete.py ──────────────────────────────────────────
class Door:
    """Свой стенд двери: секреты, копии, журналы; записи кнопок кладёт проба."""

    def __init__(self, yard: Yard, name: str) -> None:
        d = self.dir = yard.root / name
        self.log, self.secrets, self.backups = d / "log", d / "secrets", d / "backups"
        for p in (self.log, self.secrets, self.backups):
            p.mkdir(parents=True)
        # Пустой журнал слов: иначе дверь отказала бы «слово ПРОЧИТАТЬ НЕ СМОГЛИ»
        # раньше кнопки, а слово «удали файл» открыло бы всё мимо кнопки.
        (self.log / "inbox.jsonl").write_text("", encoding="utf-8")
        self.buttons = self.log / "consent-buttons.jsonl"
        (d / "install.conf").write_text(
            f'LOG_DIR="{self.log}"\nSECRETS_DIR="{self.secrets}"\nBACKUP_DIR="{self.backups}"\n'
            f'TG_CHAT_ID="{OWNER}"\n', encoding="utf-8")
        (d / "harness.conf").write_text(f"CONSENT_BUTTON_TTL_MIN={TTL_MIN}\nCONFIRM_MAX_AGE_MIN=60\n",
                                        encoding="utf-8")
        self.env = dict(os.environ, HARNESS_HOME=str(d), HARNESS_INSTALL_CONF=str(d / "install.conf"),
                        HARNESS_CONF=str(d / "harness.conf"), LOG_DIR=str(self.log),
                        SECRETS_DIR=str(self.secrets), HARNESS_OWNER_WORD_STEND="1",
                        CONSENT_BUTTON_TTL_MIN=str(TTL_MIN), CONFIRM_MAX_AGE_MIN="60")

    def file(self, rel: str, root: Path | None = None) -> Path:
        path = (root or self.secrets) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"содержимое {rel}", encoding="utf-8")
        return path

    def decide(self, event: str, target: Path, ago_s: float) -> None:
        """Решение без поля «вид» (старая форма = удаление); просьба ask-consent — на минуту раньше."""
        data = {"nonce": secrets.token_urlsafe(16), "цель": os.path.realpath(target),
                "message_id": 4000 + int(ago_s), "from_id": OWNER}
        self.ask(target, ago_s + 60, data["nonce"])
        with open(self.buttons, "a", encoding="utf-8") as fh:
            fh.write(envelope("scripts/lib/consent_buttons.py", event, now_iso(ago_s), data) + "\n")

    def ask(self, target: Path, ago_s: float, nonce: str = "") -> None:
        """Просьба ask-consent на realpath цели (вид у неё есть всегда)."""
        with open(self.log / "consent-requests.jsonl", "a", encoding="utf-8") as fh:
            fh.write(envelope("scripts/ask-consent.py", REQUEST, now_iso(ago_s),
                              {"nonce": nonce or secrets.token_urlsafe(16), "вид": "удаление",
                               "цель": os.path.realpath(target), "почему": "проба"}) + "\n")

    def delete(self, target: Path, *extra: str) -> tuple[int, str]:
        done = subprocess.run(["python3", str(DELETE), str(target), *extra],
                              capture_output=True, text=True, env=self.env, timeout=60)
        return done.returncode, (done.stdout + done.stderr).strip()


def probe_door(yard: Yard) -> None:
    print("— 7. protected-delete.py принимает кнопку")
    d = Door(yard, "door")
    a = d.file("a.txt")
    rc, _ = d.delete(a)
    check("7) контроль: без слова и без кнопки — отказ, файл цел", (1, True), (rc, a.exists()))
    d.decide(YES,a, 30)
    rc, out = d.delete(a)
    copies = list((d.backups / "секреты-удалённые").glob("a.txt.*"))
    check("7а) «да» на путь: удалён (код 0)", (0, False), (rc, a.exists()))
    check("7а) копия в BACKUP_DIR/секреты-удалённые/ с тем же содержимым", ["содержимое a.txt"],
          [c.read_text(encoding="utf-8") for c in copies])
    trace = [r for r in read_jsonl(d.log / "удаления-секретов.jsonl") if r.get("файл") == str(a.resolve())]
    check("7а) след удаления записан", 1, len(trace))

    a.write_text("воссоздан", encoding="utf-8")
    rc, _ = d.delete(a)
    check("7б) повтор на воссозданном файле — отказ, файл цел", (1, True), (rc, a.exists()))

    # Просьба про сам файл была: иначе отказ дал бы и суд парной просьбы, а не сверка цели «Да».
    d.decide(YES,d.secrets / "b.txt", 30)
    b2 = d.file("b.txt2")
    d.ask(b2, 120)
    rc, _ = d.delete(b2)
    check("7в) сосед (b.txt → b.txt2) — отказ, файл цел", (1, True), (rc, b2.exists()))
    inner = d.file("sub/f.txt")
    d.decide(YES,d.secrets / "sub", 30)
    d.ask(inner, 120)
    rc, _ = d.delete(inner)
    check("7в) «да» на родительский каталог не открывает файл в нём", (1, True), (rc, inner.exists()))

    e = d.file("e.txt")
    d.decide(NO,e, 30)
    rc, _ = d.delete(e)
    check("7г) «Нет» — отказ", (1, True), (rc, e.exists()))
    f = d.file("f.txt")
    d.decide(YES,f, 90)
    d.decide(NO,f, 30)
    rc, _ = d.delete(f)
    check("7д) «Да», затем «Нет» вторым запросом — отказ", (1, True), (rc, f.exists()))
    g = d.file("g.txt")
    d.decide(NO,g, 90)
    d.decide(YES,g, 30)
    rc, _ = d.delete(g)
    check("7д) контроль: «Нет», затем «Да» — удалён (судится последнее)", (0, False), (rc, g.exists()))

    h = d.file("h.txt")
    d.decide(YES,h, 30)
    rc_dry, _ = d.delete(h, "--сухо")
    check("7е) --сухо: код 0, файл цел", (0, True), (rc_dry, h.exists()))
    rc, _ = d.delete(h)
    check("7е) --сухо согласия не погасил: следом удалён", (0, False), (rc, h.exists()))

    old_yes = d.file("i.txt")
    d.decide(YES,old_yes, (TTL_MIN + 5) * 60)
    rc, _ = d.delete(old_yes)
    check(f"7ж) «да» старше {TTL_MIN} мин — отказ", (1, True), (rc, old_yes.exists()))

    long_ago = time.time() - 40 * 86400
    old_copy = d.file("staraja.dump", d.backups)
    os.utime(old_copy, (long_ago, long_ago))
    d.decide(YES,old_copy, 30)
    rc, _ = d.delete(old_copy)
    check("7з) копия старше порога по кнопке удалена", (0, False), (rc, old_copy.exists()))
    fresh = d.file("svezhaja.dump", d.backups)
    d.decide(YES,fresh, 30)
    rc, _ = d.delete(fresh)
    # Кнопка снимает порог возраста (спека §2): «Да» названо на этот самый файл.
    check("7з) свежая копия: «Да» кнопкой на неё снимает порог возраста", (0, False), (rc, fresh.exists()))

    # Текст владельца дверь не открывает никогда (спека §1): ни сам, ни после «Нет» кнопкой.
    w = Door(yard, "door-word")
    target = w.file("w.txt")
    with open(w.log / "inbox.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": now_iso(60), "message_id": "9001", "chat_id": str(OWNER),
                             "text": f"удали файл {w.secrets}"}, ensure_ascii=False) + "\n")
    w.decide(NO,target, 30)
    rc, _ = w.delete(target)
    check("7и) слово владельца на каталог при последнем «Нет» кнопкой — отказ, файл цел",
          (1, True), (rc, target.exists()))


def machine_ttl() -> int:
    """Срок кнопки по harness.conf машины; ключа нет — умолчание спеки, 15."""
    value = config.из_файла(f"{config.корень_машины()}/harness.conf").get("CONSENT_BUTTON_TTL_MIN", "")
    return int(value) if value.strip().isdigit() else 15


def probe_carrier(yard: Yard) -> None:
    print("— 8. битый носитель кнопок")
    d = Door(yard, "door-broken")
    x = d.file("x.txt")
    d.decide(YES,x, 30)
    with open(d.buttons, "a", encoding="utf-8") as fh:
        fh.write("{битая строка\n")
    rc, out = d.delete(x)
    check("8а) битая строка: отказ, файл цел", (1, True), (rc, x.exists()))
    check("8а) причина названа: «журнал кнопок не прочитан» и имя носителя", (True, True),
          (UNREAD in out, "consent-buttons.jsonl" in out))
    check("8а) это не «согласия нет»", False, "мин нет" in out)

    # Валидный JSON, но не конверт (прежняя плоская форма): пропуск такой строки не
    # гасил бы «нет», поэтому дверь отказывает, хотя рядом лежит годное «да».
    flat = Door(yard, "door-flat")
    z = flat.file("z.txt")
    flat.decide(YES, z, 30)
    with open(flat.buttons, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": now_iso(20), "событие": "нет", "nonce": secrets.token_urlsafe(16),
                             "цель": os.path.realpath(z), "message_id": 4100, "from_id": OWNER},
                            ensure_ascii=False) + "\n")
    rc, out = flat.delete(z)
    check("8д) строка не-конверт (плоская старая форма): отказ, файл цел", (1, True), (rc, z.exists()))
    check("8д) причина названа: «журнал кнопок не прочитан» и имя носителя", (True, True),
          (UNREAD in out, "consent-buttons.jsonl" in out))

    n = Door(yard, "door-nofile")
    y = n.file("y.txt")
    rc, out = n.delete(y)
    check("8б) файла кнопок нет — обычный отказ «согласия нет», не «прочитать не смогли»",
          (1, False), (rc, UNREAD in out))

    probe = ("import json, sys; sys.path.insert(0, sys.argv[1]); import owner_word as o\n"
             "f = getattr(o, 'согласие_кнопкой', None)\n"
             "if f is None: print('нет функции'); raise SystemExit\n"
             "try: print(json.dumps(f('удаление', sys.argv[2], sys.argv[3]), ensure_ascii=False))\n"
             "except o.НетНосителя: print('НетНосителя')\n")
    no_key = {k: v for k, v in n.env.items() if k != "HARNESS_OWNER_WORD_STEND"}

    def reader(env: dict, target: Path, logs: Path) -> str:
        done = subprocess.run(["python3", "-c", probe, str(LIB), str(target), str(logs)],
                              capture_output=True, text=True, env=env, timeout=30)
        return (done.stdout.strip().splitlines() or [done.stderr.strip()[-120:]])[-1]

    check("8в) читатель: файла нет — None", "null", reader(n.env, y, n.log))
    n.decide(YES,y, 30)
    check("8в) читатель без ключа стенда на чужом каталоге — НетНосителя", "НетНосителя",
          reader(no_key, y, n.log))

    # Срок кнопки: корень машины пробе не подменить, поэтому ожидание — разбор
    # машинного harness.conf; окружение берётся заведомо отличным от него.
    ttl = ("import sys; sys.path.insert(0, sys.argv[1]); import owner_word as o\n"
           "print(o.срок_кнопки_мин())\n")
    machine = machine_ttl()
    longer = str(machine + 1000)

    def ttl_of(env: dict) -> str:
        done = subprocess.run(["python3", "-c", ttl, str(LIB)], capture_output=True, text=True,
                              env=env, timeout=30)
        return (done.stdout.strip().splitlines() or [done.stderr.strip()[-120:]])[-1]

    check("8г) контроль: на стенде срок кнопки — из окружения", longer,
          ttl_of(dict(n.env, CONSENT_BUTTON_TTL_MIN=longer)))
    check(f"8г) без ключа стенда окружение срок не продлевает (машина: {machine})", str(machine),
          ttl_of(dict(no_key, CONSENT_BUTTON_TTL_MIN=longer)))


# ── 9. охрана ───────────────────────────────────────────────────────────────
def probe_guard(yard: Yard) -> None:
    print("— 9. охрана носителей")
    g = yard.root / "guard"
    logs = g / "log"
    logs.mkdir(parents=True)
    for name in ("consent-buttons.jsonl", "удаления-секретов.jsonl", "confirmations.jsonl", "inbox.jsonl"):
        (logs / name).write_text("", encoding="utf-8")
    (g / "install.conf").write_text(f'LOG_DIR="{logs}"\nAUTONOMY="semi"\n', encoding="utf-8")
    (g / "harness.conf").write_text("", encoding="utf-8")
    env = dict(os.environ, HARNESS_INSTALL_CONF=str(g / "install.conf"), HARNESS_CONF=str(g / "harness.conf"))
    (logs / "consent-requests.jsonl").write_text("", encoding="utf-8")
    buttons, trace = logs / "consent-buttons.jsonl", logs / "удаления-секретов.jsonl"
    requests = logs / "consent-requests.jsonl"
    yes = '{"v":1,"event":"consent.yes","data":{"цель":"/x"}}'
    nonce = "A" * 22
    sent = '{"ok":true,"result":{"message_id":5}}'
    press = (f'{{"from":{{"id":{OWNER}}},"message":{{"chat":{{"id":{OWNER}}},"message_id":5}},'
             f'"data":"c:{nonce}:y"}}')
    for want, label, cmd in (
            (2, "echo >> consent-buttons.jsonl", f"echo '{yes}' >> {buttons}"),
            (2, "tee -a consent-buttons.jsonl", f"printf x | tee -a {buttons}"),
            (2, "cp поверх consent-buttons.jsonl", f"cp /tmp/x {buttons}"),
            (2, "sed -i по consent-buttons.jsonl", f"sed -i s/нет/да/ {buttons}"),
            (2, "echo >> удаления-секретов.jsonl (решение 1)", f"echo x >> {trace}"),
            (2, "sed -i по удаления-секретов.jsonl (вырезать погашение)", f"sed -i /a/d {trace}"),
            (2, "consent_buttons.py sent из оболочки (подделка «отправлено»)",
             f"echo '{sent}' | python3 {LIB}/consent_buttons.py sent {logs} {nonce}"),
            (2, "consent_buttons.py judge из оболочки (подделка нажатия)",
             f"echo '{press}' | python3 {LIB}/consent_buttons.py judge {logs} {OWNER}"),
            (2, "python3 -c: import consent_buttons; append_row",
             f"python3 -c \"import sys; sys.path.insert(0, '{LIB}'); import consent_buttons as c; "
             f"c.append_row(c.Path('{logs}'), {{}})\""),
            (2, "sed -i по consent-requests.jsonl (подмена цели после показа)", f"sed -i s/a/b/ {requests}"),
            (0, "контроль: consent_buttons.py unsent (только чтение)",
             f"python3 {LIB}/consent_buttons.py unsent {logs} /tmp"),
            (0, "контроль: cat consent-buttons.jsonl", f"cat {buttons}"),
            (0, "контроль: echo >> другого журнала", f"echo x >> {logs}/другой.log")):
        done = subprocess.run(["python3", str(GUARD)], capture_output=True, text=True, env=env,
                              input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}))
        check(f"9) охрана: {label}", want, done.returncode)


# ── виды согласия: пробы 1-11 hr.consent-buttons-for-every-yes ──────────────
# Договор — .tasks/hr.consent-buttons-for-every-yes/spec.md §3-§5, §7. Предмет пуша
# проба считает сама (GitBench.subject), а не берёт у механизма.
LIB_BUTTONS = LIB / "consent_buttons.py"
FAKE_SSH = """#!/bin/sh
# «Удалённый» по ssh живёт здесь же: последний аргумент — команда git той стороны.
for last; do :; done
exec sh -c "$last"
"""
HANG = "#!/bin/sh\nexec sleep 30\n"
GIT_HANG_LOG = """#!/usr/bin/env bash
# git стенда: log и rev-list висят, прочее — настоящему git.
for a in "$@"; do case "$a" in log|rev-list) exec sleep 30 ;; esac; done
exec {git} "$@"
"""
FAKE_CURL = """#!/usr/bin/env bash
# Подставной Bot API для tg_send.sh: в сеть не ходит, вызов считает.
cat > /dev/null
echo call >> "${PROBA_DIR:?}/curl-calls"
printf '{"ok":true,"result":{"message_id":7}}'
"""
TG_STUB = '#!/usr/bin/env bash\necho "$*" >> "$(dirname "$0")/../tg_send.called"\nexit 0\n'
TABLES_PY = r'''
import importlib.util, json, sys
sys.path.insert(0, sys.argv[1])
import consent_buttons, owner_word
spec = importlib.util.spec_from_file_location("ask_consent", sys.argv[2])
ask = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ask)
kinds = getattr(owner_word, "ВИДЫ_СОГЛАСИЯ", None)
known = {"удаление", "пуш"}
tables = {name: {k: sorted(map(str, v)) for k, v in vars(m).items()
                 if isinstance(v, dict) and known & set(map(str, v))}
          for name, m in (("owner_word.py", owner_word), ("consent_buttons.py", consent_buttons),
                          ("ask-consent.py", ask))}
print(json.dumps({"виды": list(kinds) if isinstance(kinds, tuple) else repr(kinds),
                  "таблицы": tables}, ensure_ascii=False))
'''


def script(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def ask_raw(env: dict, args: list[str], cwd: Path | None = None) -> tuple[int | None, str, str | None, float]:
    """ask-consent.py с любыми ключами → (rc, вывод, nonce, секунды)."""
    start = time.time()
    try:
        done = subprocess.run(["python3", str(ASK), *args], capture_output=True, text=True,
                              env=dict(env, **GIT_ENV), cwd=cwd, timeout=40)
    except subprocess.TimeoutExpired:
        return None, "ask-consent.py висит дольше 40 с", None, time.time() - start
    nonce = next((s.strip() for s in done.stdout.splitlines() if NONCE_RE.match(s.strip())), None)
    return (done.returncode, (done.stdout + done.stderr).strip(),
            nonce if done.returncode == 0 else None, time.time() - start)


def push_args(bench: GitBench, remote: str = "origin", branch: str = "master") -> list[str]:
    return ["--вид", "пуш", "--предмет", f"{remote} {branch}", "--репо", str(bench.work),
            "--почему", "проба: пуш"]


def request_of(path: Path, nonce: str | None) -> dict:
    return next((data_of(r) for r in read_jsonl(path) if nonce and r.get("event") == REQUEST
                 and data_of(r).get("nonce") == nonce), {})


def sent_form(st: Stand, nonce: str | None) -> dict:
    if nonce:
        wait_for(lambda: st.sent_id(nonce) is not None)
    sends = st.keyboard_sends(nonce)
    return sends[0]["form"] if sends else {}


def rewrite_request_data(st: Stand, nonce: str | None, **fields: str) -> None:
    """Сессия переписывает поля своего запроса после отправки владельцу."""
    if not nonce:
        return
    out = []
    for line in st.requests.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        if nonce and row.get("event") == REQUEST and data_of(row).get("nonce") == nonce:
            row["data"].update(fields)
        out.append(json.dumps(row, ensure_ascii=False))
    st.requests.write_text("\n".join(out) + "\n", encoding="utf-8")


def probe_kind_request(st: Stand) -> None:
    print("— виды-1. запрос пуша: строка, команда с хэшем, текст владельцу")
    b = GitBench(st.yard.root / "v1")
    sha = b.head()
    rc, out, nonce, _ = ask_raw(st.env, push_args(b), st.dir)
    check("в1а) ask-consent.py --вид пуш: код 0 и nonce", (0, True), (rc, bool(nonce)))
    req = request_of(st.requests, nonce)
    check("в1б) строка запроса: вид «пуш»", "пуш", req.get("вид"))
    check("в1б) цель: base..head, адрес, refs/heads/master, репо", b.subject(sha), req.get("цель"))
    check("в1в) напечатана команда с хэшем момента запроса", True,
          f"git -C {b.repo()} push origin {sha}:refs/heads/master" in out)
    form = sent_form(st, nonce)
    text = form.get("text", "")
    hashes = git("log", "--format=%h", f"{b.base}..{sha}", cwd=b.work).split()
    check("в1г) условие: в диапазоне три коммита", 3, len(hashes))
    check("в1г) владельцу: «3 коммита»", True, "3 коммита" in text)
    check("в1г) три хэша списка в тексте", [True] * len(hashes), [h in text for h in hashes])
    check("в1г) путь удалённого в тексте", True, str(b.remote) in text)
    check(f"в1г) текст непуст и ≤ 700 знаков (вышло {len(text)})", True, 0 < len(text) <= 700)
    datas = keyboard(form)
    check("в1д) callback_data каждой кнопки ≤ 64 байт", True,
          bool(datas) and all(d and len(d.encode()) <= 64 for d in datas))

    s = GitBench(st.yard.root / "v1-ssh")
    user = f"tok-{secrets.token_hex(4)}"
    ssh = script(st.yard.root / "v1-bin" / "ssh", FAKE_SSH)
    git("remote", "set-url", "origin", f"ssh://{user}@stand.invalid{s.remote}", cwd=s.work)
    rc, out, nonce_s, _ = ask_raw(dict(st.env, GIT_SSH_COMMAND=str(ssh)), push_args(s), st.dir)
    check(f"в1е) ssh-адрес с учёткой: запрос принят [{out[-160:]}]", 0, rc)
    target = str(request_of(st.requests, nonce_s).get("цель", user))
    check("в1е) цель с хостом и без учётки (И-3)", (True, False), ("stand.invalid" in target, user in target))
    text_s = sent_form(st, nonce_s).get("text", user)
    check("в1е) текст владельцу без учётки", False, user in text_s)


def probe_kind_replace(st: Stand) -> None:
    print("— виды-2. git replace не подменяет список коммитов (Н-4)")
    b = GitBench(st.yard.root / "v2", ahead=0)
    b.commit("evil 1")
    head = b.commit("evil 2")
    tree = git("rev-parse", f"{head}^{{tree}}", cwd=b.work)
    benign = git("commit-tree", tree, "-p", b.base, "-m", "benign", cwd=b.work)
    git("replace", head, benign, cwd=b.work)
    check("в2) условие: с подменой git log показывает «benign»", "benign",
          git("log", "-1", "--format=%s", head, cwd=b.work))
    rc, out, nonce, _ = ask_raw(st.env, push_args(b), st.dir)
    check(f"в2а) запрос принят [{out[-160:]}]", 0, rc)
    text = sent_form(st, nonce).get("text", "")
    check("в2б) «2 коммита», настоящие «evil 1» и «evil 2», «benign» нет", (True, True, True, False),
          ("2 коммита" in text, "evil 1" in text, "evil 2" in text, "benign" in text))


def probe_kind_refusals(yard: Yard) -> None:
    print("— виды-3. отказы --вид пуш: код 2, строки нет")
    d = Door(yard, "v3")
    requests = d.log / "consent-requests.jsonl"
    hang = script(yard.root / "v3-bin" / "hang-ssh", HANG)
    numbers = iter(range(1, 50))

    def bench(ahead: int = 1) -> GitBench:
        return GitBench(yard.root / f"v3-{next(numbers)}", ahead)

    lines = line_count(requests)
    rc, out, _nonce, _ = ask_raw(d.env, push_args(bench()), d.dir)
    check(f"в3а) контроль: годный запрос — код 0 и строка [{out[-160:]}]", (0, lines + 1),
          (rc, line_count(requests)))

    def refused(label: str, args: list[str], extra_env: dict | None = None, reason: str = "",
                limit: float = 0, parsed: bool = True) -> None:
        lines = line_count(requests)
        rc, out, _nonce, took = ask_raw(dict(d.env, **(extra_env or {})), args, d.dir)
        check(f"{label}: код 2, строки нет", (2, lines), (rc, line_count(requests)))
        if parsed:
            # Ключи годные: отказ обязан быть по сути, а не разбором аргументов.
            check(f"{label}: отказ по сути, не «usage» разбора ключей", False, "usage:" in out)
        if reason:
            check(f"{label}: причина «{reason}»", True, reason in out)
        if limit:
            check(f"{label}: ≤ {limit:.0f} с (вышло {took:.1f} с)", True, took <= limit)

    refused("в3б) пустой диапазон (head = вершина удалённого)", push_args(bench(0)))
    refused("в3в) удалённого нет", push_args(bench(), remote="proba-nowhere"))
    b = bench()
    other = b.other_clone("other")
    git("commit", "-q", "--allow-empty", "-m", "чужой", cwd=other)
    git("push", "-q", "origin", "HEAD:refs/heads/master", cwd=other)
    refused("в3г) не потомок (удалённый ушёл вперёд другим клоном)", push_args(b))
    b = bench()
    git("branch", "proba-new", cwd=b.work)
    refused("в3д) новая ветка (0 строк ls-remote)", push_args(b, branch="proba-new"))
    for letter, key, value in (("е", "remote.origin.pushurl", None),
                               ("ж", "url.file:///proba-nowhere/.insteadOf", "proba-zzz:"),
                               ("з", "url.file:///proba-nowhere/.pushInsteadOf", "proba-zzz:"),
                               ("и", "remote.origin.push", "refs/heads/master:refs/heads/master")):
        b = bench()
        git("config", key, value or b.url, cwd=b.work)
        refused(f"в3{letter}) {key.split('.')[0]}…{key.rsplit('.', 1)[1]} задан (Н-2)", push_args(b),
                reason="перенаправлен")
    b = bench()
    git("remote", "set-url", "origin", f"ssh://stand.invalid{b.remote}", cwd=b.work)
    refused("в3к) ls-remote висит", push_args(b), extra_env={"GIT_SSH_COMMAND": str(hang)}, limit=7)
    refused("в3л) --вид выкат", ["--вид", "выкат", "--предмет", "x", "--почему", "проба"], parsed=False)
    secret = d.file("v3.txt")
    refused("в3м) --цель вместе с --вид", ["--цель", str(secret), "--вид", "удаление", "--предмет",
                                          str(secret), "--почему", "проба"], parsed=False)


def probe_kind_default(yard: Yard) -> None:
    print("— виды-4. --цель без --вид — запрос вида «удаление»")
    d = Door(yard, "v4")
    f = d.file("v4.txt")
    rc, _out, nonce, _ = ask_raw(d.env, ["--цель", str(f), "--почему", "проба"])
    check("в4а) код 0 и nonce", (0, True), (rc, bool(nonce)))
    req = request_of(d.log / "consent-requests.jsonl", nonce)
    check("в4б) строка запроса: вид «удаление», цель realpath", ("удаление", str(f.resolve())),
          (req.get("вид"), req.get("цель")))


def probe_kind_yes(st: Stand) -> None:
    print("— виды-5. «Да» на пуш: вид и цель — из «отправлено»")
    b = GitBench(st.yard.root / "v5")
    subject = b.subject()
    _rc, _out, nonce, _ = ask_raw(st.env, push_args(b), st.dir)
    sent_form(st, nonce)
    mid = st.sent_id(nonce)
    check("в5) условие: запрос пуша отправлен", True, mid is not None)
    swapped = st.secret(f"v5-podmena-{secrets.token_hex(3)}.txt")
    rewrite_request_data(st, nonce, вид="удаление", цель=str(swapped.resolve()))
    if nonce and mid is not None:
        st.press(yes_data(nonce), mid)
    rec = st.records(nonce)
    check("в5а) запись «да»", [YES], [r.get("event") for r in rec])
    check("в5б) вид и цель — из «отправлено», не из переписанного запроса", ("пуш", subject),
          (data_of(rec[0]).get("вид"), data_of(rec[0]).get("цель")) if rec else None)


def probe_kind_mixed(st: Stand) -> None:
    print("— виды-6. смешанные версии: нет вида = удаление (Н-15)")
    target = st.secret(f"v6-{secrets.token_hex(3)}.txt")
    path = str(target.resolve())
    nonce = secrets.token_urlsafe(16)
    with open(st.requests, "a", encoding="utf-8") as fh:
        fh.write(envelope("scripts/ask-consent.py", REQUEST, now_iso(),
                          {"nonce": nonce, "цель": path, "почему": "запрос старой формы"}) + "\n")
    sent_form(st, nonce)
    sent = st.records(nonce, (SENT,))
    check("в6а) запрос без вида отправлен владельцу", 1, len(st.keyboard_sends(nonce)))
    check("в6а) «отправлено»: цель — путь, вид — удаление или поля нет", (path, "удаление"),
          (data_of(sent[-1]).get("цель"), data_of(sent[-1]).get("вид", "удаление")) if sent else None)

    log = st.yard.root / "v6-log"
    log.mkdir()
    b = GitBench(st.yard.root / "v6", ahead=1)

    def record(target_arg: str) -> list:
        own = secrets.token_urlsafe(16)
        subprocess.run(["python3", str(LIB_BUTTONS), "sent", str(log), own, target_arg],
                       input=json.dumps({"ok": True, "result": {"message_id": 4242}}),
                       capture_output=True, text=True, timeout=30,
                       env=dict(os.environ, LOG_DIR=str(log), HARNESS_LOG_DIR=str(log)))
        return [(data_of(r).get("вид", "удаление"), data_of(r).get("цель"))
                for r in read_jsonl(log / "consent-buttons.jsonl")
                if r.get("event") == SENT and data_of(r).get("nonce") == own]

    check("в6б) target «чужой-вид:x» (вид вне ВИДЫ_СОГЛАСИЯ) — «отправлено» не пишется", [], record("чужой-вид:x"))
    check("в6б) target «перепись:x» (законный вид, спека §5) — «отправлено» вида «перепись»",
          [("перепись", "x")], record("перепись:x"))
    check("в6в) target «пуш:<предмет>» — «отправлено» вида «пуш» на предмет", [("пуш", b.subject())],
          record(f"пуш:{b.subject()}"))
    check("в6г) target старой формы «/путь» — «отправлено» на путь, вид удаление", [("удаление", path)],
          record(path))


def probe_kind_hang(yard: Yard) -> None:
    print("— виды-7. зависший git log не держит круг диспетчера")
    log = yard.root / "v7-log"
    log.mkdir()
    b = GitBench(yard.root / "v7")
    nonce = secrets.token_urlsafe(16)
    with open(log / "consent-requests.jsonl", "a", encoding="utf-8") as fh:
        fh.write(envelope("scripts/ask-consent.py", REQUEST, now_iso(),
                          {"nonce": nonce, "вид": "пуш", "цель": b.subject(), "почему": "проба"}) + "\n")
    script(yard.root / "v7-bin" / "git", GIT_HANG_LOG.format(git=REAL_GIT))
    env = dict(os.environ, **GIT_ENV, PATH=f"{yard.root / 'v7-bin'}:{os.environ.get('PATH', '')}",
               LOG_DIR=str(log), HARNESS_LOG_DIR=str(log), HARNESS_OWNER_WORD_STEND="1",
               CONSENT_BUTTON_TTL_MIN=str(TTL_MIN))
    start = time.time()
    try:
        done = subprocess.run(["python3", str(LIB_BUTTONS), "unsent", str(log), str(yard.root / "v7-sec")],
                              capture_output=True, text=True, env=env, timeout=40)
        out, err = done.stdout, done.stderr
    except subprocess.TimeoutExpired:
        out, err = "", ""
    took = time.time() - start
    check(f"в7а) unsent() вернулся ≤ 7 с (вышло {took:.1f} с)", True, took <= 7)
    check("в7б) запрос с висящим git не отдан на отправку", False, nonce in out)
    check("в7в) причина — строкой в stderr", True, bool(err.strip()))


def probe_kind_door(yard: Yard) -> None:
    print("— виды-8. дверь удаления не открывается «Да» вида «пуш»")
    d = Door(yard, "v8")
    a = d.file("v8-a.txt")
    d.ask(a, 120)
    button(d.log, YES, "пуш", os.path.realpath(a), 30)
    rc, _ = d.delete(a)
    check("в8а) «Да» вида «пуш» с целью = путь файла — отказ, файл цел", (1, True), (rc, a.exists()))
    b = d.file("v8-b.txt")
    button(d.log, YES, "удаление", os.path.realpath(b), 30)
    rc, _ = d.delete(b)
    check("в8б) охранный: «Да» вида «удаление» на путь — удалён", (0, False), (rc, b.exists()))


def probe_kind_ask_door(yard: Yard) -> None:
    print("— виды-9. protected-delete.py --спросить зовёт ask-consent.py, а не tg_send.sh (Н-19)")
    d = Door(yard, "v9")
    # Копия дерева: tg_send.sh в ней подставной, боевой канал недостижим.
    tree = yard.root / "v9-tree" / "scripts"
    tree.mkdir(parents=True)
    for name in ("protected-delete.py", "ask-consent.py"):
        shutil.copy2(ROOT / "scripts" / name, tree / name)
    (tree / "lib").symlink_to(LIB)
    script(tree / "tg_send.sh", TG_STUB)
    f = d.file("v9.txt")
    done = subprocess.run(["python3", str(tree / "protected-delete.py"), str(f), "--спросить"],
                          capture_output=True, text=True, env=d.env, timeout=60)
    rows = [data_of(r) for r in read_jsonl(d.log / "consent-requests.jsonl") if r.get("event") == REQUEST]
    check("в9а) одна строка запроса: вид «удаление», цель realpath", [("удаление", str(f.resolve()))],
          [(r.get("вид"), r.get("цель")) for r in rows])
    check("в9б) подставной tg_send.sh не вызван", False, (yard.root / "v9-tree" / "tg_send.called").exists())
    code = f"код {rows[0].get('nonce', '')[:8]}" if rows else None
    check("в9в) напечатан код запроса — 8 знаков его nonce (спека §4)", True,
          bool(code) and len(code) == len("код ") + 8 and code in done.stdout)


def probe_kind_tables(yard: Yard) -> None:
    print("— виды-10. таблицы по виду: ключи == ВИДЫ_СОГЛАСИЯ")
    done = subprocess.run(["python3", "-c", TABLES_PY, str(LIB), str(ASK)], capture_output=True,
                          text=True, timeout=60, env=dict(os.environ, HARNESS_OWNER_WORD_STEND="1"))
    try:
        found = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        found = {"виды": done.stderr.strip()[-160:], "таблицы": {}}
    kinds = found.get("виды")
    check("в10а) owner_word.ВИДЫ_СОГЛАСИЯ — восемь видов (спека §5: + «снос-базы», «перепись»;"
          " hr.standards-and-policy: + «слой-человека»)",
          ["удаление", "пуш", "выкат", "публикация", "спека", "снос-базы", "перепись",
           "слой-человека"], kinds)
    # Отзыв текстом — только у необратимых (спека §3). «спека» обратима (m3); «слой-человека»
    # тоже: его «Да» читает self-set.py через согласие_кнопкой, правка слоя откатывается.
    reversible = {"спека", "слой-человека"}
    expected = {"ПРЕДМЕТ_ОТЗЫВА": sorted(set(kinds) - reversible)} if isinstance(kinds, list) else {}
    for name in ("owner_word.py", "consent_buttons.py", "ask-consent.py"):
        tables = found["таблицы"].get(name) or {}
        check(f"в10б) {name}: таблица по виду есть, ключи == ВИДЫ_СОГЛАСИЯ", True,
              bool(tables) and isinstance(kinds, list)
              and all(t == expected.get(k, sorted(kinds)) for k, t in tables.items()))


def probe_kind_caption(yard: Yard) -> None:
    print("— виды-11. подпись вложения проходит сторожа канала (Н-7)")
    s = yard.root / "v11"
    (s / "log").mkdir(parents=True)
    script(s / "bin" / "curl", FAKE_CURL)
    (s / "token").write_text("000:СТЕНДОВЫЙ", encoding="utf-8")   # не секрет: API подставной
    (s / "install.conf").write_text(
        f'PROJECT_DIR="{ROOT}"\nLOG_DIR="{s}/log"\nTG_CHAT_ID="{OWNER}"\nTG_TOKEN_FILE="{s}/token"\n',
        encoding="utf-8")
    (s / "harness.conf").write_text("", encoding="utf-8")
    doc = s / "otchet.md"
    doc.write_text("# отчёт стенда\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_SESSION_ID", "HARNESS_TG_FORCE", "HARNESS_REPORT")}
    env.update(PROBA_DIR=str(s), PATH=f"{s / 'bin'}:{os.environ.get('PATH', '')}", HARNESS_HOME=str(s),
               HARNESS_INSTALL_CONF=str(s / "install.conf"), HARNESS_CONF=str(s / "harness.conf"),
               LOG_DIR=str(s / "log"), PROJECT_DIR=str(ROOT), TG_CHAT_ID=str(OWNER),
               TG_TOKEN_FILE=str(s / "token"))

    def send(caption: str) -> tuple[int, int]:
        before = line_count(s / "curl-calls")
        done = subprocess.run(["bash", str(ROOT / "scripts" / "tg_send.sh"), "--несмотря-на-лимит",
                               "--file", str(doc), "--caption", caption],
                              capture_output=True, text=True, env=env, timeout=60)
        return done.returncode, line_count(s / "curl-calls") - before

    rc, calls = send("Пуш 3 коммитов в гитхаб — нужно слово «да»")
    check("в11а) подпись с вопросом о «да» на пуш: код ≠ 0, API не вызван", (True, 0), (rc != 0, calls))
    rc, calls = send("Спека задачи hr.proba целиком, итерация 2")
    check("в11б) здоровая подпись уходит: код 0, API вызван раз", (0, 1), (rc, calls))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--случай", dest="only", action="append", choices=CASE_NAMES)
    only = set(parser.parse_args().only or CASE_NAMES)
    yard = Yard()
    stand = None
    for name, needs_stand, run in (("запрос", True, probe_request), ("да", True, probe_yes),
                                   ("чужой", True, probe_stranger), ("повтор", True, probe_repeat),
                                   ("срок", True, probe_ttl), ("битый", True, probe_broken),
                                   ("дверь", False, probe_door), ("носитель", False, probe_carrier),
                                   ("охрана", False, probe_guard),
                                   ("виды-1", True, probe_kind_request), ("виды-2", True, probe_kind_replace),
                                   ("виды-3", False, probe_kind_refusals), ("виды-4", False, probe_kind_default),
                                   ("виды-5", True, probe_kind_yes), ("виды-6", True, probe_kind_mixed),
                                   ("виды-7", False, probe_kind_hang), ("виды-8", False, probe_kind_door),
                                   ("виды-9", False, probe_kind_ask_door), ("виды-10", False, probe_kind_tables),
                                   ("виды-11", False, probe_kind_caption)):
        if name not in only:
            continue
        if needs_stand:
            stand = stand or Stand(yard)
            run(stand)
        else:
            run(yard)
    if stand and not cases.ok:
        stand.show()
    if cases.ok:
        print(f"падений 0 из {cases.paths} путей")
        return 0
    print(f"ПРОБА КРАСНАЯ: падений {cases.failures} из {cases.paths} путей")
    return 1


if __name__ == "__main__":
    sys.exit(main())
