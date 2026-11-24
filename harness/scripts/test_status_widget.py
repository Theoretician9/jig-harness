#!/usr/bin/env python3
"""Пробы статуса-виджета Telegram (спека итерации 2, К-1…К-13; версия 2, К-15…К-28).

Слово владельца 20.11 15:2x: в отчёте единица — ЗАДАЧА; у неё человеческое название, что и
зачем, часть чего и стадии пайплайна (пройдены / текущая / остались).

Стенд каждой пробы — свой mktemp: карта, журнал вызовов, harness.conf, паспорт с выдуманным
токеном и чатом. Bot API — заглушка http.server на 127.0.0.1 и свободном порту
(TG_API_BASE). Живой Telegram недостижим вдвойне: https_proxy стенда — мёртвый адрес, а
пробы tg_send.sh не запускаются вовсе, пока в нём нет двери TG_API_BASE.

    python3 -m pytest -q scripts/test_status_widget.py
"""
import html
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORT = HERE / "status-report.py"
WIDGET = HERE / "status_widget.py"
TG_SEND = HERE / "tg_send.sh"
DIRECTIONS = HERE / "build-directions.py"
TG_SEND_FILES = ("tg_send.sh", "limit_mark.py", "check-channel-messages.py", "language-level.py")
CARD_ID = re.compile(r"\b(?:hr|pr)\.[\w.-]*\w")
FORBIDDEN_TAGS = re.compile(r"<(script|img|input|progress|meter|iframe|link|meta)\b", re.IGNORECASE)
COLOR_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(")
API_DOOR = "TG_API_BASE:-https://api.telegram.org"
DEAD_PROXY = "http://127.0.0.1:9"
CHAT_ID = "-777123987"
CEILING = 1400
LABELS = "opus=Opus 5.5;sonnet=Sonnet 5.5;haiku=Haiku 5.5"
SESSION = "sessiya-vidzheta"
TASK_ID = "hr.demo-widget-kartochka"
SHORT = "Статус владельцу картинкой"
SUMMARY = ("Статус приходит одной карточкой вместо лапши, чтобы владелец за секунды видел, "
           "что делается. Подробности в спеке hr.demo-fixture-card.")
LINE_TITLE = "Канал владельца"
DIRECTION_NAME = "Разговор с владельцем"


# ── общие куски ─────────────────────────────────────────────────────────────

def run(cmd, env=None, stdin="", timeout=120, cwd=None):
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout,
                          env=env, cwd=cwd)


def widget(*args, data=None, env=None):
    """Прогон рендера; нет модуля — провал пробы с причиной (красное до кода)."""
    if not WIDGET.is_file():
        raise AssertionError(f"нет {WIDGET} — рендера виджета ещё нет")
    return run([sys.executable, str(WIDGET), *args], stdin=json.dumps(data, ensure_ascii=False),
               env={**os.environ, **(env or {})})


def stage_list(passed, current, left):
    return ([{"step": s, "state": "passed"} for s in passed]
            + ([{"step": current, "state": "current"}] if current else [])
            + [{"step": s, "state": "left"} for s in left])


def task(n, title=None, what="Чтобы владелец видел работу глазами.", part=LINE_TITLE,
         stages=None, status="wip"):
    return {"id": f"hr.fixture-{n}", "title": title or f"Задача номер {n}", "what": what,
            "part": part, "status": status,
            "stages": stages if stages is not None else stage_list(["спека"], "ревью_спеки", ["код"]),
            "who": [{"model": "Opus 5.5", "role": "код", "running": True}]}


def report_data(doing, delivered=(), waiting=(), waiting_total=None):
    return {"version": 1, "built_at": "2026-11-20T18:00:00+05:00", "text": "статус текстом",
            "doing": list(doing), "doing_more": 0, "delivered": list(delivered), "delivered_more": 0,
            "other": 0, "waiting_owner": list(waiting),
            "waiting_total": len(waiting) if waiting_total is None else waiting_total,
            "next": ["Следующая задача очереди"], "account": ""}


# Детерминированные «слова»: zlib сжимает повтор почти до нуля, и проба бюджета на
# одинаковых названиях прошла бы на любой вёрстке.
ALPHABET = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"


def noisy_title(seed: int, length: int = 120) -> str:
    out, x = [], seed * 7919 + 13
    while len("".join(out)) < length:
        x = (x * 1103515245 + 12345) % (1 << 31)
        word = "".join(ALPHABET[(x >> k) % len(ALPHABET)] for k in range(0, 3 + x % 6 * 3, 3))
        out.append(word + " ")
    return "".join(out)[:length]


# ── стенд сборщика ──────────────────────────────────────────────────────────

def launch(call, desc, typ, model, part):
    data = {"описание": desc, "тип": typ, "модель": model, "ревью": False, "вызов": call,
            "часть": f"Часть: {part}"}
    return {"v": 1, "ts": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
            "src": "scripts/hooks/agent_budget_guard.py", "event": "agent.launch", "outcome": "ok",
            "task": TASK_ID, "session": SESSION, "data": data}


def finish(call):
    return {"v": 1, "ts": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            "src": "scripts/hooks/agent_finish.py", "event": "agent.finish", "outcome": "ok",
            "task": TASK_ID, "session": SESSION, "data": {"вызов": call}}


class Stand:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="proba-vidzheta-"))
        self.log = self.root / "log"
        self.project = self.root / "project"
        self.log.mkdir()
        self.project.mkdir()
        (self.root / "harness.conf").write_text(
            f'STATUS_STRUCT_MAX_CHARS="{CEILING}"\nSTATUS_MODEL_LABELS="{LABELS}"\n', encoding="utf-8")
        (self.root / "dev-map.yaml").write_text(
            "queue: [hr.demo-next-zadacha]\n"
            "lines:\n"
            "- id: kanal\n"
            f"  title: {LINE_TITLE}\n"
            "  tasks:\n"
            f"  - id: {TASK_ID}\n"
            "    status: wip\n"
            "    title: Статус владельцу Telegram HTML-виджетом вместо текстового статуса tg_send\n"
            f"    коротко: {SHORT}\n"
            f"    summary: {json.dumps(SUMMARY, ensure_ascii=False)}\n"
            "    глубина: {уровень: крупная, характер: разработка}\n"
            "  - id: hr.demo-next-zadacha\n"
            "    status: plan\n"
            "    title: Следующая задача очереди\n", encoding="utf-8")
        (self.root / "queue.txt").write_text(
            "работа: 2\nждут: \n"
            f"сейчас делаем: {TASK_ID} — начата\n"
            f"  1. {TASK_ID} · 30 · начата\n"
            "  2. hr.demo-next-zadacha · 20 · в очереди\n", encoding="utf-8")
        rows = [launch("c1", "тесты статуса виджетом", "harness-worker", "opus", f"{TASK_ID}#1"),
                finish("c1"),
                launch("c2", "код статуса виджетом", "harness-worker", "sonnet", f"{TASK_ID}#2"),
                finish("c2"),
                launch("c3", "ревью кода статуса", "harness-worker", "opus", f"{TASK_ID}#3")]
        self.journal = self.log / "вызовы-агентов.jsonl"
        self.journal.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                encoding="utf-8")

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_CODE_SESSION_ID", "HARNESS_REPORT", "HARNESS_TG_FORCE",
                            "STATUS_WIDGET", "TG_API_BASE", "HARNESS_DIRECTIONS_LOCAL")}
        env.update(HARNESS_CONF=str(self.root / "harness.conf"), LOG_DIR=str(self.log),
                   PROJECT_DIR=str(self.project), HARNESS_HOME=str(self.root),
                   CLAUDE_PROJECTS_DIR=str(self.root / "projects"),
                   HARNESS_DIRECTIONS=str(self.root / "net-napravleniy.yaml"),
                   https_proxy=DEAD_PROXY, HTTPS_PROXY=DEAD_PROXY, no_proxy="127.0.0.1",
                   NO_PROXY="127.0.0.1")
        env.update(extra)
        return env

    def args(self):
        since = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        return ["--журнал-вызовов", str(self.journal), "--карта", str(self.root / "dev-map.yaml"),
                "--очередь", str(self.root / "queue.txt"), "--сессия", SESSION, "--с", since]

    def report(self, *extra, **env):
        done = run([sys.executable, str(REPORT), *self.args(), *extra], env=self.env(**env),
                   cwd=str(self.root))
        if done.returncode != 0:
            raise AssertionError(f"status-report {' '.join(extra)}: код {done.returncode}: "
                                 f"{done.stderr.strip()[-400:]}")
        return done.stdout

    def report_json(self, **env):
        out = self.report("--json", **env)
        try:
            return json.loads(out)
        except ValueError as error:
            raise AssertionError(f"--json напечатал не JSON ({error}): {out[:300]!r}")


SCHEMA_KEYS = {"version", "built_at", "text", "doing", "doing_more", "delivered", "delivered_more",
               "other", "waiting_owner", "waiting_total", "next", "account"}
TASK_KEYS = {"id", "title", "what", "part", "status", "stages", "who"}


def expected_stages(stand: Stand, card_id: str) -> list:
    """Полоса стадий суждением гейта — в окружении стенда, тем путём, что велит спека."""
    snippet = (
        "import importlib.util, json, yaml\n"
        f"spec = importlib.util.spec_from_file_location('build_board', {str(HERE / 'build-board.py')!r})\n"
        "board = importlib.util.module_from_spec(spec); spec.loader.exec_module(board)\n"
        f"data = yaml.safe_load(open({str(stand.root / 'dev-map.yaml')!r}, encoding='utf-8'))\n"
        f"card = next(c for l in data['lines'] for c in l['tasks'] if c['id'] == {card_id!r})\n"
        "steps = board.column(card, board.load_gate())[2]\n"
        "print(json.dumps([[s['шаг'], s['есть'], s['почему']] for s in steps], ensure_ascii=False))\n")
    done = run([sys.executable, "-c", snippet], env=stand.env())
    if done.returncode != 0:
        raise AssertionError(f"суждение гейта не получено: {done.stderr[-400:]}")
    steps = [s for s in json.loads(done.stdout) if not str(s[2]).startswith("не положен")]
    out, current_given = [], False
    for name, present, _why in steps:
        if present is True:
            out.append({"step": name, "state": "passed"})
        elif not current_given:
            out.append({"step": name, "state": "current"})
            current_given = True
        else:
            out.append({"step": name, "state": "left"})
    return out


# ── заглушка Bot API ────────────────────────────────────────────────────────

class StubApi:
    """Bot API на 127.0.0.1: пишет метод и поля, ответ — по таблице метод → (код, тело)."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
                method = self.path.rstrip("/").rsplit("/", 1)[-1]
                fields = {k: v[0] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()}
                stub.calls.append({"method": method, "fields": fields})
                code, answer = stub.answers.get(method, (200, {"ok": True, "result": {"message_id": 7}}))
                raw = json.dumps(answer).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def methods(self, name):
        return [c for c in self.calls if c["method"] == name]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# Подставной сборщик в дереве tg_send стенда: зовёт НАСТОЯЩИЙ status-report.py с данными
# стенда и доводами, которые ему дал tg_send (--json, --отметить).
BUILDER_STUB = """#!/usr/bin/env python3
import json, os, subprocess, sys
with open(os.environ["STAND_BUILDER_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
extra = [] if "--отметить" in sys.argv else json.loads(os.environ["STAND_REPORT_ARGS"])
done = subprocess.run([sys.executable, os.environ["STAND_REAL_REPORT"], *extra, *sys.argv[1:]],
                      capture_output=True, text=True,
                      env={**os.environ, "PROJECT_DIR": os.environ["STAND_REPORT_PROJECT"]})
sys.stderr.write(done.stderr)
sys.stdout.write(done.stdout)
sys.exit(done.returncode)
"""


class TgStand(Stand):
    def __init__(self):
        super().__init__()
        self.tree = self.root / "harnes"
        (self.tree / "scripts").mkdir(parents=True)
        (self.tree / "память").mkdir()
        for name in TG_SEND_FILES:
            shutil.copy(HERE / name, self.tree / "scripts" / name)
        if WIDGET.is_file():
            shutil.copy(WIDGET, self.tree / "scripts" / WIDGET.name)
        shutil.copytree(HERE / "lib", self.tree / "scripts" / "lib",
                        ignore=shutil.ignore_patterns("__pycache__"))
        signs = HERE.parent / "harness" / "config" / "consent-ask-signs.yaml"
        if signs.is_file():
            (self.tree / "harness" / "config").mkdir(parents=True)
            shutil.copy(signs, self.tree / "harness" / "config" / signs.name)
        (self.tree / "память" / "TONE.md").write_text(
            "# Тон\n\n<!-- потолки: мягкий=1400 жёсткий=1400 -->\n", encoding="utf-8")
        builder = self.tree / "scripts" / "status-report.py"
        builder.write_text(BUILDER_STUB, encoding="utf-8")
        builder.chmod(0o755)
        self.token = "11" + os.urandom(16).hex()
        key = self.root / "klyuch"
        key.write_text(self.token + "\n", encoding="utf-8")
        (self.root / "install.conf").write_text(
            f'TG_CHAT_ID="{CHAT_ID}"\n'                    # не-секрет: выдуманный чат стенда
            f'TG_TOKEN_FILE="{key}"\n'                     # не-секрет: путь в стенде
            f'PROJECT_DIR="{self.tree}"\nLOG_DIR="{self.log}"\nSECRETS_DIR="{self.root}"\n'
            'TMUX_SESSION="proba-vidzheta"\nAGENT_START_CMD="true"\n', encoding="utf-8")

    def send(self, stub: StubApi, **extra):
        if API_DOOR not in TG_SEND.read_text(encoding="utf-8"):
            # Без двери tg_send.sh пошёл бы в живой api.telegram.org — не запускаем.
            raise AssertionError(f"в {TG_SEND} нет двери {API_DOOR} — проба не шлёт ничего")
        env = self.env(PROJECT_DIR=str(self.tree), HARNESS_INSTALL_CONF=str(self.root / "install.conf"),
                       HEARTBEAT_DIR=str(self.log / "heartbeat"), TG_API_BASE=stub.base,
                       STAND_BUILDER_LOG=str(self.log / "builder.log"), STAND_REAL_REPORT=str(REPORT),
                       STAND_REPORT_ARGS=json.dumps(self.args(), ensure_ascii=False),
                       STAND_REPORT_PROJECT=str(self.project))
        env.update(extra)
        return run(["bash", str(self.tree / "scripts" / "tg_send.sh"), "--статус"], env=env)

    def last_out(self) -> dict:
        path = self.log / "tg_out.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        return json.loads(lines[-1]) if lines else {}

    def status_text(self) -> str:
        return self.report_json()["text"]


# ── К-1…К-5: status-report.py --json ───────────────────────────────────────

class ReportJson(unittest.TestCase):
    def setUp(self):
        self.stand = Stand()

    def tearDown(self):
        shutil.rmtree(self.stand.root, ignore_errors=True)

    def doing_task(self, data):
        found = [t for t in data["doing"] if t.get("id") == TASK_ID]
        self.assertEqual(len(found), 1, f"задача стенда в doing: {[t.get('id') for t in data['doing']]}")
        return found[0]

    def test_k1_one_json_object_and_text_equal(self):
        """К-1. Поломка «text собран отдельно от блоков» ловится сравнением с выводом без ключа."""
        data = self.stand.report_json()
        self.assertEqual(data.get("version"), 1)
        self.assertTrue(SCHEMA_KEYS <= set(data), f"нет ключей: {SCHEMA_KEYS - set(data)}")
        self.assertRegex(data["built_at"], r"([+-]\d\d:\d\d|Z)$")
        self.assertEqual(data["text"].rstrip("\n"), self.stand.report().rstrip("\n"))
        for entry in data["doing"] + data["delivered"]:
            self.assertTrue(TASK_KEYS <= set(entry), f"задача без ключей: {TASK_KEYS - set(entry)}")

    def test_k2_human_title_what_without_ids(self):
        """К-2. «title = title карточки» или «id в what» краснит: ждём коротко и ноль hr.*."""
        entry = self.doing_task(self.stand.report_json())
        self.assertEqual(entry["title"], SHORT)
        self.assertTrue(entry["what"].startswith("Статус приходит одной карточкой"), entry["what"])
        self.assertLessEqual(len(entry["what"]), 140)
        for key in ("title", "what", "part"):
            self.assertIsNone(CARD_ID.search(entry[key] or ""), f"{key}: {entry[key]!r}")

    def test_k3_helpers_are_one_task(self):
        """К-3. Три помощника одной задачи — одна задача; «по запуску на карточку» даст 3."""
        data = self.stand.report_json()
        self.doing_task(data)
        ids = [t["id"] for t in data["doing"] + data["delivered"]]
        self.assertEqual(ids.count(TASK_ID), 1, ids)
        self.assertFalse([i for i in ids if i.startswith("дело:")], f"помощники отдельными задачами: {ids}")

    def test_k4_stages_by_gate(self):
        """К-4. Полоса = суждение гейта без «не положен»; своя копия правил разойдётся на уровне."""
        entry = self.doing_task(self.stand.report_json())
        expected = expected_stages(self.stand, TASK_ID)
        self.assertTrue(expected, "условие стенда: гейт дал шаги крупной задаче")
        self.assertEqual(entry["stages"], expected)
        self.assertEqual(sum(s["state"] == "current" for s in entry["stages"]), 1)

    def test_k5_part_falls_back_to_map_line(self):
        """К-5 (откат). Конфига направлений нет → часть = название линии карты, не её id."""
        entry = self.doing_task(self.stand.report_json())
        self.assertEqual(entry["part"], LINE_TITLE)

    @unittest.skipUnless(DIRECTIONS.is_file(), "build-directions.py ещё не в этом дереве (hr.directions-map не слита)")
    def test_k5_part_is_direction(self):
        """К-5. Есть карта направлений → часть = название направления, правило по id карточки."""
        (self.stand.root / "napravleniya.yaml").write_text(
            "версия: 1\nидёт_сейчас_до: 5\nобрезка_названия: 90\n"
            "прочее: {название: Прочее}\n"
            "группы:\n- название: Канал\n  направления:\n"
            f"  - {{id: owner-talk, название: {DIRECTION_NAME}, описание: проба, правило: {{id: demo-widget}}}}\n",
            encoding="utf-8")
        entry = self.doing_task(self.stand.report_json(
            HARNESS_DIRECTIONS=str(self.stand.root / "napravleniya.yaml"), HARNESS_DIRECTIONS_LOCAL=""))
        self.assertEqual(entry["part"], DIRECTION_NAME)


# ── К-2, К-4, К-6…К-8: рендер status_widget.py ─────────────────────────────

class Render(unittest.TestCase):
    def test_k2_card_lines_escaped_no_ids(self):
        """К-2. Название, что и зачем, часть — видны; id не виден; данные экранированы."""
        data = report_data([task(1, title="<b>x</b>& Статус картинкой", what="Чтобы видеть глазами",
                                 part="Разговор с владельцем")])
        done = widget(data=data)
        self.assertEqual(done.returncode, 0, done.stderr)
        page = done.stdout
        self.assertIn("&lt;b&gt;x&lt;/b&gt;&amp; Статус картинкой", page)
        self.assertNotIn("<b>x</b>", page)
        self.assertIn("Чтобы видеть глазами", page)
        self.assertIn("Разговор с владельцем", page)
        self.assertIsNone(CARD_ID.search(page))
        self.assertIsNone(FORBIDDEN_TAGS.search(page))
        self.assertIsNone(COLOR_LITERAL.search(page))
        self.assertIn("var(--tg-theme-", page)
        self.assertEqual(page.count("<section"), 1)

    def test_k4_stage_bar_marks(self):
        """К-4. «всё ✓» или «текущая без метки» краснит: ждём ✓/▶/○ на своих шагах."""
        stages = stage_list(["спека", "ревью_спеки"], "контракт", ["тесты", "код"])
        done = widget("--text", data=report_data([task(1, stages=stages)]))
        self.assertEqual(done.returncode, 0, done.stderr)
        text = done.stdout
        for mark in ("✓ спека", "✓ ревью спеки", "▶ контракт", "○ тесты", "○ код"):
            self.assertIn(mark, text)
        for wrong in ("✓ контракт", "✓ тесты", "▶ спека", "○ спека"):
            self.assertNotIn(wrong, text)
        self.assertIsNone(CARD_ID.search(text))
        page = widget(data=report_data([task(1, stages=stages)])).stdout
        self.assertIn("▶", page)
        self.assertIn("✓", page)

    def test_k6_budget_and_more(self):
        """К-6. 50 шумных названий: бюджет 3800 после zlib 9 и «ещё N» с верным N."""
        data = report_data([task(n, title=noisy_title(n), what=noisy_title(n + 100, 100)) for n in range(50)])
        done = widget(data=data)
        self.assertEqual(done.returncode, 0, done.stderr)
        raw = done.stdout.encode()
        self.assertLessEqual(len(raw), 32768)
        self.assertLessEqual(len(zlib.compress(raw, 9)), 3800)
        shown = done.stdout.count("<section")
        self.assertGreaterEqual(shown, 1)
        self.assertLess(shown, 50, "условие: все 50 не влезают")
        self.assertIn(f"ещё {50 - shown}", done.stdout)

    def test_k6_cut_delivered_first(self):
        """К-6 (порядок урезки). Не влезло — сперва уходит «Сдано»: идущая работа дороже."""
        doing = [task(n, title=noisy_title(n)) for n in (1, 2)]
        delivered = [task(n, title=noisy_title(n), status="test") for n in (3, 4)]
        fitted = widget(data=report_data(doing, delivered[:1]))
        self.assertEqual(fitted.returncode, 0, fitted.stderr)
        # Бюджет = 2 идущих + 1 сданная и запас на строку «ещё 1»: четвёртая карточка не влезет.
        budget = len(zlib.compress(fitted.stdout.encode(), 9)) + 40
        done = widget(data=report_data(doing, delivered), env={"STATUS_WIDGET_ZLIB_BUDGET": str(budget)})
        self.assertEqual(done.returncode, 0, done.stderr)
        page = html.unescape(done.stdout)
        for kept in (noisy_title(1), noisy_title(2), noisy_title(3)):
            self.assertIn(kept, page)
        self.assertNotIn(noisy_title(4), page)
        self.assertIn("ещё 1", page)

    def test_k7_budget_door_refuses(self):
        """К-7. Бюджет меньше одной карточки → код 3 и пустой stdout (не обрезанный HTML)."""
        done = widget(data=report_data([task(1)]), env={"STATUS_WIDGET_ZLIB_BUDGET": "50"})
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertEqual(done.stdout, "")

    def test_k8_caption_one_line_numbers(self):
        """К-8. Числа фикстуры 2/3/4 разные: перепутанные счётчики краснят."""
        data = report_data([task(1), task(2)], delivered=[task(3, status="test"), task(4, status="test"),
                                                         task(5, status="test")],
                           waiting=[{"title": "Решение по кнопке", "what": ""}], waiting_total=4)
        done = widget("--caption", data=data)
        self.assertEqual(done.returncode, 0, done.stderr)
        caption = done.stdout.rstrip("\n")
        self.assertNotIn("\n", caption)
        self.assertLessEqual(len(caption), 200)
        for part in ("в работе 2", "сдано 3", "ждёт тебя 4"):
            self.assertIn(part, caption)


# ── К-9…К-12: tg_send.sh --статус против заглушки ──────────────────────────

class Send(unittest.TestCase):
    def setUp(self):
        self.stand = TgStand()
        self.stub = None

    def tearDown(self):
        if self.stub:
            self.stub.close()
        shutil.rmtree(self.stand.root, ignore_errors=True)

    def mark(self) -> Path:
        return self.stand.log / "status-report.last"

    def test_k9_widget_sent(self):
        """К-9. Один sendWidget с полями, ноль sendMessage, метка, method=widget."""
        self.stub = StubApi()
        done = self.stand.send(self.stub)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        sent = self.stub.methods("sendWidget")
        self.assertEqual(len(sent), 1, [c["method"] for c in self.stub.calls])
        self.assertTrue({"chat_id", "html", "caption"} <= set(sent[0]["fields"]), sent[0]["fields"].keys())
        self.assertEqual(sent[0]["fields"]["chat_id"], CHAT_ID)
        self.assertIn("<section", sent[0]["fields"]["html"])
        self.assertEqual(self.stub.methods("sendMessage"), [])
        self.assertTrue(self.mark().is_file(), "метка статуса не записана")
        self.assertEqual(self.stand.last_out().get("method"), "widget")

    def test_k10_switch_off_is_text(self):
        """К-10. STATUS_WIDGET=0 → нынешний текст; «выключатель не читается» даст sendWidget."""
        self.stub = StubApi()
        done = self.stand.send(self.stub, STATUS_WIDGET="0")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.stub.methods("sendWidget"), [])
        texts = self.stub.methods("sendMessage")
        self.assertEqual(len(texts), 1, [c["method"] for c in self.stub.calls])
        self.assertEqual(texts[0]["fields"].get("text", "").rstrip("\n"),
                         self.stand.status_text().rstrip("\n"))

    def test_k11_widget_refused_falls_back(self):
        """К-11. 400 на sendWidget → текст, код 0, метка, строка в tg_failures.log, method=text."""
        self.stub = StubApi({"sendWidget": (400, {"ok": False, "error_code": 400,
                                                  "description": "Bad Request: INPUT_MEDIA_WIDGET_TOO_LARGE"})})
        done = self.stand.send(self.stub)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        order = [c["method"] for c in self.stub.calls]
        self.assertEqual(order, ["sendWidget", "sendMessage"], order)
        self.assertEqual(self.stub.methods("sendMessage")[0]["fields"].get("text", "").rstrip("\n"),
                         self.stand.status_text().rstrip("\n"))
        self.assertTrue(self.mark().is_file(), "метка статуса не записана")
        failures = self.stand.log / "tg_failures.log"
        self.assertTrue(failures.is_file() and "виджет" in failures.read_text(encoding="utf-8"))
        self.assertEqual(self.stand.last_out().get("method"), "text")

    def test_k12_no_token_no_chat_leak(self):
        """К-12. Токен и чат стенда нигде в журналах, выводе и HTML (и при отказе виджета)."""
        self.stub = StubApi({"sendWidget": (400, {"ok": False, "description": "Bad Request"})})
        done = self.stand.send(self.stub)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(self.stub.methods("sendWidget"), "условие: виджет пробовали слать")
        for secret in (self.stand.token, CHAT_ID):
            self.assertNotIn(secret, done.stdout + done.stderr)
            for path in self.stand.log.rglob("*"):
                if path.is_file():
                    self.assertNotIn(secret, path.read_text(encoding="utf-8", errors="replace"), str(path))
            self.assertNotIn(secret, self.stub.methods("sendWidget")[0]["fields"].get("html", ""))


class ApiDoor(unittest.TestCase):
    def test_k13_one_api_door(self):
        """К-13. Одна дверь базового адреса на все методы tg_send.sh."""
        text = TG_SEND.read_text(encoding="utf-8")
        self.assertEqual(sum(API_DOOR in line for line in text.splitlines()), 1)
        self.assertNotIn('API="https://api.telegram.org/bot', text)


# ── v2: К-15…К-28 — правка на месте, кнопки, судья нажатий ─────────────────

STATE_NAME = "status-widget.json"
BUTTONS_JOURNAL = "widget-buttons.jsonl"
BUTTONS = {"w:refresh": "Обновить", "w:tasks": "Задачи", "w:helpers": "Помощники"}
CHAT_DATA_MAX = 1024
ROW_MAX = 80
SNAPSHOT_NAME = "status-widget-data.json"
OWNER_ID = "424242"
WAITING_TITLE = "Решить, куда класть ключи виджета"
GONE = (400, {"ok": False, "error_code": 400, "description": "Bad Request: message to edit not found"})
NOT_MODIFIED = (400, {"ok": False, "error_code": 400,
                      "description": "Bad Request: message is not modified: specified new message content "
                                     "and reply markup are exactly the same"})
BROKEN = (500, {"ok": False, "error_code": 500, "description": "Internal Server Error"})


def widget_ok(message_id: int) -> tuple:
    return 200, {"ok": True, "result": {"message_id": message_id}}


class WidgetV2Stand(TgStand):
    """Стенд tg_send v2: состояние виджета — $LOG_DIR/status-widget.json (К-16)."""

    def state_path(self) -> Path:
        return self.log / STATE_NAME

    def state(self) -> dict:
        path = self.state_path()
        if not path.is_file():
            raise AssertionError(f"нет файла состояния виджета {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def press(self, stub: StubApi, data: str, message_id, chat=CHAT_ID, sender=CHAT_ID, owner=""):
        """Нажатие кнопки виджета через судью диспетчера (lib/widget_buttons.py press)."""
        module = self.tree / "scripts" / "lib" / "widget_buttons.py"
        if not module.is_file():
            raise AssertionError(f"нет {module.name} — судьи нажатий виджета ещё нет")
        callback = {"id": "cbq-proba-1", "data": data,
                    "from": {"id": int(sender), "is_bot": False, "first_name": "Владелец"},
                    "message": {"message_id": message_id, "chat": {"id": int(chat), "type": "private"}}}
        env = self.env(PROJECT_DIR=str(self.tree), TG_API_BASE=stub.base, TG_CHAT_ID=CHAT_ID,
                       TG_OWNER_ID=owner, TG_TOKEN_FILE=str(self.root / "klyuch"),
                       STATUS_WIDGET_STATE=str(self.state_path()),
                       STAND_BUILDER_LOG=str(self.log / "builder.log"), STAND_REAL_REPORT=str(REPORT),
                       STAND_REPORT_ARGS=json.dumps(self.args(), ensure_ascii=False),
                       STAND_REPORT_PROJECT=str(self.project))
        return run([sys.executable, str(module), "press", str(self.log)], env=env,
                   stdin=json.dumps(callback, ensure_ascii=False))

    def snapshot_path(self) -> Path:
        return self.log / SNAPSHOT_NAME

    def rewrite_snapshot(self, **record):
        """Снимок последнего пульса (его пишет tg_send --статус) с подменёнными полями записи."""
        path = self.snapshot_path()
        if not path.is_file():
            raise AssertionError(f"пульс не оставил снимка {path}")
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved.update(record)
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        return saved

    def builder_calls(self) -> int:
        path = self.log / "builder.log"
        return len(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else 0

    def journal_rows(self) -> list:
        path = self.log / BUTTONS_JOURNAL
        lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        # строка журнала — конверт events.emit: поля нажатия лежат в data
        return [json.loads(line)["data"] for line in lines if line.strip()]


def chat_data_of(call: dict) -> dict:
    raw = call["fields"].get("chat_data", "")
    if len(raw.encode("utf-8")) > CHAT_DATA_MAX:
        raise AssertionError(f"chat_data {len(raw.encode('utf-8'))} байт > {CHAT_DATA_MAX}")
    return json.loads(raw)


class InPlace(unittest.TestCase):
    """К-15…К-20: tg_send.sh --статус держит один виджет и правит его на месте."""

    def setUp(self):
        self.stand = WidgetV2Stand()
        self.stub = StubApi({"sendWidget": widget_ok(7)})

    def tearDown(self):
        self.stub.close()
        shutil.rmtree(self.stand.root, ignore_errors=True)

    def first(self):
        done = self.stand.send(self.stub)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.stub.calls.clear()

    def test_k15_second_status_edits_same_message(self):
        """К-15. Второй статус → editMessageWidgetData с сохранённым message_id, без sendWidget."""
        self.first()
        done = self.stand.send(self.stub)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        order = [c["method"] for c in self.stub.calls]
        self.assertEqual(order, ["editMessageWidgetData"], order)
        edit = self.stub.calls[0]["fields"]
        self.assertEqual(edit.get("chat_id"), CHAT_ID)
        self.assertEqual(edit.get("message_id"), "7")
        self.assertEqual(chat_data_of(self.stub.calls[0]).get("view"), "status")
        self.assertEqual(self.stand.last_out().get("method"), "widget")

    def test_k16_state_has_message_id_not_chat(self):
        """К-16. Первая отправка пишет состояние: message_id из ответа, chat_id в нём нет (К-12)."""
        self.first()
        state = self.stand.state()
        self.assertEqual(state.get("message_id"), 7)
        raw = self.stand.state_path().read_text(encoding="utf-8")
        self.assertNotIn(CHAT_ID.lstrip("-"), raw)
        self.assertNotIn(self.stand.token, raw)

    def test_k17_gone_message_sends_new_and_rewrites_state(self):
        """К-17. edit → «message to edit not found» → новый sendWidget, новый message_id в состоянии."""
        self.first()
        self.stub.answers.update({"editMessageWidgetData": GONE, "sendWidget": widget_ok(8)})
        done = self.stand.send(self.stub)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        order = [c["method"] for c in self.stub.calls]
        self.assertEqual(order, ["editMessageWidgetData", "sendWidget"], order)
        self.assertEqual(self.stand.state().get("message_id"), 8)
        self.assertEqual(self.stand.last_out().get("method"), "widget")

    def test_k18_not_modified_is_success(self):
        """К-18. «message is not modified» — доставлено: ни нового виджета, ни текста."""
        self.first()
        self.stub.answers["editMessageWidgetData"] = NOT_MODIFIED
        done = self.stand.send(self.stub)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        order = [c["method"] for c in self.stub.calls]
        self.assertEqual(order, ["editMessageWidgetData"], order)
        self.assertEqual(self.stand.state().get("message_id"), 7)

    def test_k19_edit_error_falls_back_to_text(self):
        """К-19. Иная ошибка правки → текстовый статус, строка в tg_failures.log, состояние то же."""
        self.first()
        self.stub.answers["editMessageWidgetData"] = BROKEN
        done = self.stand.send(self.stub)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        order = [c["method"] for c in self.stub.calls]
        self.assertEqual(order, ["editMessageWidgetData", "sendMessage"], order)
        self.assertEqual(self.stub.methods("sendMessage")[0]["fields"].get("text", "").rstrip("\n"),
                         self.stand.status_text().rstrip("\n"))
        failures = (self.stand.log / "tg_failures.log").read_text(encoding="utf-8")
        self.assertIn("виджет", failures)
        self.assertEqual(self.stand.state().get("message_id"), 7)
        self.assertEqual(self.stand.last_out().get("method"), "text")

    def test_k20_switch_off_keeps_state_untouched(self):
        """К-20. STATUS_WIDGET=0 → только текст; файл состояния не тронут."""
        self.first()
        before = self.stand.state_path().read_bytes()
        done = self.stand.send(self.stub, STATUS_WIDGET="0")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        order = [c["method"] for c in self.stub.calls]
        self.assertEqual(order, ["sendMessage"], order)
        self.assertEqual(self.stand.state_path().read_bytes(), before)


class Buttons(unittest.TestCase):
    """К-21, К-22: виджет несёт три кнопки из закрытого списка; chat_data в лимите."""

    def setUp(self):
        self.stand = WidgetV2Stand()
        self.stub = StubApi({"sendWidget": widget_ok(7)})

    def tearDown(self):
        self.stub.close()
        shutil.rmtree(self.stand.root, ignore_errors=True)

    def sent_once(self) -> dict:
        done = self.stand.send(self.stub)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        sent = self.stub.methods("sendWidget")
        self.assertEqual(len(sent), 1, [c["method"] for c in self.stub.calls])
        return sent[0]

    def test_k21_three_buttons_closed_list(self):
        """К-21. reply_markup sendWidget: ровно три кнопки, callback_data из закрытого списка."""
        markup = json.loads(self.sent_once()["fields"].get("reply_markup") or "{}")
        buttons = [b for row in markup.get("inline_keyboard", []) for b in row]
        self.assertEqual(len(buttons), 3, buttons)
        self.assertEqual({b.get("callback_data"): b.get("text") for b in buttons}, BUTTONS)

    def test_k22_chat_data_contract(self):
        """К-22. chat_data виджета: JSON ≤ 1024 байт, v=1, view, n{doing, delivered, waiting}, rows."""
        data = chat_data_of(self.sent_once())
        self.assertEqual(data.get("v"), 1)
        self.assertEqual(data.get("view"), "status")
        self.assertEqual(set(data.get("n") or {}), {"doing", "delivered", "waiting"})
        self.assertIsInstance(data.get("rows"), list)

    def test_k22_chat_data_cut_to_limit(self):
        """К-22. 60 задач по 120 знаков: chat_data ≤ 1024 байт, строки ≤ 80 знаков, последняя «ещё N»."""
        sys.path.insert(0, str(HERE))
        try:
            import status_widget
        finally:
            sys.path.remove(str(HERE))
        self.assertEqual(Path(status_widget.__file__).resolve(), WIDGET, "загружен не тот status_widget")
        titles = [noisy_title(seed) for seed in range(60)]
        self.assertTrue(all(len(t) == 120 for t in titles))
        data = report_data([{"title": t} for t in titles])
        raw = status_widget.chat_data(data, "status")
        self.assertLessEqual(len(raw.encode("utf-8")), CHAT_DATA_MAX)
        rows = json.loads(raw)["rows"]
        self.assertGreater(len(rows), 1, rows)
        self.assertEqual(rows[-1], f"ещё {60 - (len(rows) - 1)}")
        for row in rows:
            self.assertLessEqual(len(row), ROW_MAX, row)


class Presses(unittest.TestCase):
    """К-23…К-27: судья нажатий lib/widget_buttons.py press против заглушки."""

    def setUp(self):
        self.stand = WidgetV2Stand()
        self.stub = StubApi({"sendWidget": widget_ok(7)})
        done = self.stand.send(self.stub)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.stub.calls.clear()

    def tearDown(self):
        self.stub.close()
        shutil.rmtree(self.stand.root, ignore_errors=True)

    def accepted_view(self, data: str) -> dict:
        done = self.stand.press(self.stub, data, 7)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        order = [c["method"] for c in self.stub.calls]
        self.assertEqual(order, ["editMessageWidgetData", "answerCallbackQuery"], order)
        edit = self.stub.calls[0]["fields"]
        self.assertEqual((edit.get("chat_id"), edit.get("message_id")), (CHAT_ID, "7"))
        self.assertEqual(self.stub.calls[1]["fields"].get("callback_query_id"), "cbq-proba-1")
        self.assertEqual(self.stand.journal_rows()[-1].get("verdict"), "accepted")
        return chat_data_of(self.stub.calls[0])

    def refused(self, done, calls: list):
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual([c["method"] for c in self.stub.calls], calls)
        rows = self.stand.journal_rows()
        self.assertTrue(rows, "отказ не записан в журнал нажатий")
        self.assertEqual(rows[-1].get("verdict"), "refused")
        raw = (self.stand.log / BUTTONS_JOURNAL).read_text(encoding="utf-8")
        self.assertNotIn(CHAT_ID.lstrip("-"), raw)
        self.assertNotIn(self.stand.token, raw)

    def test_k23_refresh_edits_and_answers(self):
        """К-23. «Обновить» владельца → правка того же виджета (view=status) и ответ на запрос."""
        self.assertEqual(self.accepted_view("w:refresh").get("view"), "status")

    def test_r2_04_press_does_not_build(self):
        """R2-04. Принятое нажатие рисует из снимка пульса: сборщик (status-report) не зовётся."""
        before = self.stand.builder_calls()
        self.assertGreater(before, 0, "пульс стенда не звал сборщика — проба ничего не судит")
        self.accepted_view("w:refresh")
        self.assertEqual(self.stand.builder_calls(), before, "нажатие позвало сборщика")

    def test_r2_05_snapshot_time_and_age(self):
        """R2-05. Снимок пульса 7 минут назад → в chat_data at = ЧЧ:ММ снимка, age = 7."""
        taken = datetime.now().astimezone() - timedelta(minutes=7, seconds=10)
        self.stand.rewrite_snapshot(at=taken.isoformat(timespec="seconds"))
        data = self.accepted_view("w:refresh")
        self.assertEqual(data.get("at"), taken.strftime("%H:%M"))
        self.assertEqual(data.get("age"), 7)

    def test_r2_11_tasks_differs_from_status(self):
        """R2-11 / К-24. «Ждёт тебя» в снимке: есть в rows вида tasks и нет в rows вида status."""
        snapshot = json.loads(self.stand.snapshot_path().read_text(encoding="utf-8"))
        data = dict(snapshot["data"], waiting_owner=[{"id": "hr.demo-zhdyot", "title": WAITING_TITLE}],
                    waiting_total=1)
        self.stand.rewrite_snapshot(data=data)
        self.assertIn(WAITING_TITLE, self.accepted_view("w:tasks").get("rows") or [])
        self.stub.calls.clear()
        self.assertNotIn(WAITING_TITLE, self.accepted_view("w:refresh").get("rows") or [])

    def test_k24_tasks_view(self):
        """К-24. «Задачи» → view=tasks, в rows — название задачи стенда, без id карточки."""
        data = self.accepted_view("w:tasks")
        self.assertEqual(data.get("view"), "tasks")
        self.assertIn(SHORT, " ".join(data.get("rows") or []))
        self.assertIsNone(CARD_ID.search(json.dumps(data, ensure_ascii=False)))

    def test_k24_helpers_view(self):
        """К-24. «Помощники» → view=helpers, в rows — подпись модели идущего помощника."""
        data = self.accepted_view("w:helpers")
        self.assertEqual(data.get("view"), "helpers")
        self.assertIn("Opus 5.5", " ".join(data.get("rows") or []))

    def test_k25_stranger_chat_silent_refusal(self):
        """К-25. Чужой chat.id → ни одного вызова API, отказ в журнале без id владельца."""
        # отправитель — владелец (личка: from.id = chat.id): отказ может дать только сверка чата
        self.refused(self.stand.press(self.stub, "w:refresh", 7, chat="-100555000", sender=CHAT_ID), [])

    def test_k25_stranger_sender_silent_refusal(self):
        """К-25. Чат свой, from.id чужой → ни одного вызова API, отказ в журнале."""
        self.refused(self.stand.press(self.stub, "w:refresh", 7, sender="555000"), [])

    def test_r2_12_owner_not_chat(self):
        """R2-12. TG_OWNER_ID задан (группа): нажатие от имени чата — молчаливый отказ, от владельца — принято."""
        self.refused(self.stand.press(self.stub, "w:refresh", 7, sender=CHAT_ID, owner=OWNER_ID), [])
        done = self.stand.press(self.stub, "w:refresh", 7, sender=OWNER_ID, owner=OWNER_ID)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual([c["method"] for c in self.stub.calls], ["editMessageWidgetData", "answerCallbackQuery"])
        self.assertEqual(self.stand.journal_rows()[-1].get("verdict"), "accepted")

    def test_k26_unknown_data_refused(self):
        """К-26. Данные не из закрытого списка → без правки, только ответ-отказ, строка в журнале."""
        self.refused(self.stand.press(self.stub, "w:rm-rf", 7), ["answerCallbackQuery"])

    def test_k27_foreign_message_refused(self):
        """К-27. message_id не сохранённый → без правки, только ответ-отказ, строка в журнале."""
        self.refused(self.stand.press(self.stub, "w:refresh", 6), ["answerCallbackQuery"])


class DispatcherBranch(unittest.TestCase):
    def test_k28_dispatcher_routes_w_prefix(self):
        """К-28. handle_callback: ветка w:* зовёт lib/widget_buttons.py press раньше общего ответа."""
        text = (HERE / "tg-dispatcher.sh").read_text(encoding="utf-8")
        body = text[text.index("handle_callback() {"):]
        body = body[:body.index("\n}\n")]
        branch = [line for line in body.splitlines() if line.strip().startswith("w:*)")]
        self.assertEqual(len(branch), 1, "нет ветки w:*) в handle_callback")
        self.assertIn("widget_buttons.py", branch[0])
        self.assertIn(" press ", branch[0])
        # ветка сама кончает обработку: без return 0 нажатие уйдёт в общий путь (второй ответ)
        self.assertRegex(branch[0], r";\s*return 0\s*;;")
        # порядок веток case — по строкам кода, не по первому вхождению слова (оно бывает в комментарии)
        heads = [line.strip().split(")", 1)[0] for line in body.splitlines()
                 if re.match(r"\s*(w:\*|\*)\)", line)]
        self.assertIn("*", heads, "нет общей ветки *) в handle_callback")
        self.assertLess(heads.index("w:*"), heads.index("*"), heads)


if __name__ == "__main__":
    unittest.main()
