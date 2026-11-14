#!/usr/bin/env python3
"""Пробы общей записи событий харнеса: scripts/lib/events.py (emit, read) и
bash-дверь scripts/lib/events.sh (harness_event).

Зачем. До этой задачи у каждого из 35 журналов был свой писатель, своё время
и своя тихая потеря: `except OSError: pass`, `>> …jsonl 2>/dev/null || true`.
Строка, которая не записалась, не оставляла следа нигде; секрет в данных
уходил в журнал как есть; два писателя на одном файле склеивали строки.
Спека docs/specs/2026-10-27-логирование-харнеса.md §3–§5.2, критерии 1, 2,
3 (сторона писателя), 4 (вид времени). Каждый случай ниже — поломка, которую
он обязан поймать; больные случаи идут первыми.

Стенд. Всё — во временном каталоге, механизм зовётся в ДОЧЕРНЕМ процессе с
дверями окружения (HARNESS_LOG_DIR, HARNESS_EVENTS_STATE, HARNESS_DEVMAP,
EVENTS_GROUP, PANEL_STATE_DIR, SECRETS_DIR): боевые журналы проба не читает и
не пишет — это проверяется снимком /var/log/harness/.events до и после.
Путь к механизму — дверь HARNESS_EVENTS_LIB (и HARNESS_EVENTS_SH для
bash-двери): мутация ставится в копию, иначе проба судила бы боевой файл.

    python3 scripts/test_events_lib.py
Код возврата: 0 — все пути прошли; 1 — есть упавший или судить было нечего.
«НЕ СУДИТСЯ» (второго пользователя нет, механизм ему не читается) — не
зелёный путь: число таких случаев печатается в итоге отдельно.
Прогон — только фоном (CLAUDE.md): гонка 50×200 и сроки блокировки идут
десятки секунд.
"""
from __future__ import annotations

import datetime as dt
import grp
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import string
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = os.environ.get("HARNESS_EVENTS_LIB") or str(ROOT / "scripts" / "lib" / "events.py")
SH = os.environ.get("HARNESS_EVENTS_SH") or str(ROOT / "scripts" / "lib" / "events.sh")
LIVE_EVENTS = Path("/var/log/harness/.events")
STAND_PREFIX = "events-lib-probe-"
# Реестр журналов — ДАННЫЕ: ребёнок получает его копию дверью HARNESS_JOURNALS.
# Без двери механизм ищет реестр рядом с собой, и копия механизма в каталоге
# мутаций читала бы чужой (или никакой) реестр — мутации M19/M28 судились бы
# не тем реестром, что у пробы.
REGISTRY = Path(os.environ.get("HARNESS_JOURNALS") or ROOT / "harness" / "config" / "journals.yaml")
TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
OUTCOMES = {"ok", "refused", "error", "skipped", "held"}

paths = failures = not_judged = 0


def check(name: str, expected, actual) -> None:
    global paths, failures
    paths += 1
    if actual == expected:
        print(f"ок  · {name}")
    else:
        failures += 1
        print(f"ПЛОХО · {name}: ждали «{expected}», вышло «{str(actual)[:300]}»")


def skip(name: str, why: str) -> None:
    """Случай не судился — это НЕ зелёный, он называется в итоге отдельно."""
    global not_judged
    not_judged += 1
    print(f"НЕ СУДИТСЯ · {name}: {why}")


def events_group() -> tuple[str, int]:
    try:
        g = grp.getgrnam("harness-panel")
        if g.gr_gid in os.getgroups():
            return g.gr_name, g.gr_gid
    except KeyError:
        pass
    return grp.getgrgid(os.getgid()).gr_name, os.getgid()


GROUP_NAME, GROUP_GID = events_group()

# Ребёнок грузит механизм по пути из двери и печатает результат одной строкой.
CHILD_HEAD = r'''
import importlib.util, json, os, sys
_spec = importlib.util.spec_from_file_location("events", os.environ["HARNESS_EVENTS_LIB"])
ev = importlib.util.module_from_spec(_spec)
sys.modules["events"] = ev
_spec.loader.exec_module(ev)
def out(x):
    print("RESULT=" + json.dumps(x, ensure_ascii=False, default=str), flush=True)
'''


class Stand:
    """Один временный каталог на случай: журналы, состояние, карта."""

    def __init__(self, base: Path, name: str, wip: int = 0):
        self.root = base / name
        self.log = self.root / "log"
        self.state = self.log / ".events"
        self.panel = self.root / "panel-state"
        self.secrets = self.root / "secrets"
        for d in (self.log, self.panel, self.secrets):
            d.mkdir(parents=True, exist_ok=True)
        self.devmap = self.root / "dev-map.yaml"
        make_devmap(self.devmap, wip)
        self.registry = self.root / "journals.yaml"
        shutil.copyfile(REGISTRY, self.registry)

    def env(self, **extra) -> dict:
        e = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": "C.UTF-8",
            "HARNESS_EVENTS_LIB": LIB,
            "HARNESS_EVENTS_SH": SH,
            "HARNESS_LOG_DIR": str(self.log),
            "LOG_DIR": str(self.log),
            "HARNESS_EVENTS_STATE": str(self.state),
            "HARNESS_DEVMAP": str(self.devmap),
            "HARNESS_JOURNALS": str(self.registry),
            "PANEL_STATE_DIR": str(self.panel),
            "SECRETS_DIR": str(self.secrets),
            "EVENTS_GROUP": GROUP_NAME,
        }
        e.update({k: v for k, v in extra.items() if v is not None})
        return e

    def lines(self, journal: str) -> list[str]:
        p = self.log / f"{journal}.jsonl"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def lost(self) -> list[dict]:
        p = self.state / "lost.jsonl"
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def map_cards(n: int) -> list[str]:
    """Первые n карточек НАСТОЯЩЕЙ карты. Имена не вписаны в пробу: расчистка
    карты уносит карточки в архив, и вписанное имя валило стенд."""
    text = (ROOT / "dev-map.yaml").read_text(encoding="utf-8")
    ids = re.findall(r"- id: (hr\.[a-z0-9.-]+)\n\s+status: ", text)[:n]
    if len(ids) < n:
        raise SystemExit(f"проба: в dev-map.yaml карточек hr.* меньше {n} — стенд карты не собрать")
    return ids


WIP_CARDS = map_cards(2)


def make_devmap(path: Path, wip: int) -> None:
    """Копия НАСТОЯЩЕЙ карты (данные репозитория, не журнал) с заданным числом
    карточек wip: форма карты — её, а не выдуманная пробой."""
    text = (ROOT / "dev-map.yaml").read_text(encoding="utf-8")
    text = re.sub(r"(\n\s+status: )wip\b", r"\1test", text)
    ids = WIP_CARDS[:wip]
    for card in ids:
        pat = re.compile(r"(- id: " + re.escape(card) + r"\n\s+status: )\w+")
        text, n = pat.subn(r"\1wip", text, count=1)
        if n != 1:
            raise SystemExit(f"проба: карточка {card} не найдена в dev-map.yaml — стенд карты не собрать")
    path.write_text(text, encoding="utf-8")


def run_child(body: str, env: dict, timeout: int = 60, as_user: str | None = None):
    code = CHILD_HEAD + body
    cmd = [sys.executable, "-I", "-c", code]
    if as_user:
        cmd = ["sudo", "-n", "-u", as_user, "env"] + [f"{k}={v}" for k, v in env.items()] + cmd
        env = None
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, cwd="/")
    result = None
    for line in p.stdout.splitlines():
        if line.startswith("RESULT="):
            result = json.loads(line[len("RESULT="):])
    return p.returncode, result, p.stderr


def emit_body(**kw) -> str:
    return f"out(ev.emit(**{kw!r}))\n"


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def gen_secret() -> str:
    """Свежий секрет формы ключа: не скопирован ниоткуда ([[the-probes-secret-was-a-real-one]])."""
    alphabet = string.ascii_letters + string.digits
    while True:
        s = "".join(secrets.choice(alphabet) for _ in range(40))
        if any(c.islower() for c in s) and any(c.isupper() for c in s) and any(c.isdigit() for c in s):
            return s


def live_snapshot():
    """Следы стенда в боевом .events, а не тишина каталога: туда пишет вся машина.
    Журналы стенда в реестре не значатся, а кэш задачи хранит путь карты стенда."""
    if not LIVE_EVENTS.exists():
        return ("нет",)
    import yaml
    registered = set(yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))["journals"])
    lost = (LIVE_EVENTS / "lost.jsonl").read_text(encoding="utf-8", errors="replace")
    stand_rows = sum(1 for m in re.finditer(r'"journal": "([^"]*)"', lost) if m.group(1) not in registered)
    cache = LIVE_EVENTS / "current-task.json"
    return stand_rows, cache.exists() and STAND_PREFIX in cache.read_text(encoding="utf-8", errors="replace")


SRC = "scripts/hooks/guard_bash.py"

# Образцы адаптеров (крит. 4, находка В9): по строке на каждый журнал реестра
# со временем; где файл был, сняты с БОЕВЫХ файлов 27.10.2026 (последняя строка; оставлены поле
# времени и короткие поля механизма, сессии заменены на «s-sample», длинные и
# секретоподобные значения выброшены). Ожидаемое время в UTC посчитано при
# составлении и записано литералом — проба не зовёт для ожидания тот же разбор,
# что судит. Наивное время ожидается в поясе машины +05:00 (§12: такие журналы
# писаны в поясе машины). «date:» — реестры с точностью до суток.
# Формат: (каталог-ключ реестра, файл относительно каталога, строка, ожидание).
SAMPLES = [
    ("log", 'confirmations.jsonl', {"ts": "2026-10-27T13:35:09+05:00", "kind": "any"}, '2026-10-27T08:35:09+00:00'),
    ("log", 'consent-buttons.jsonl', {"v": 1, "ts": "2026-11-05T09:22:16+05:00", "src": "scripts/lib/consent_buttons.py", "event": "consent.sent", "outcome": "ok", "task": "hr.sample", "task_from": "map", "session": None, "data": {"nonce": "n-sample", "цель": "/tmp/sample", "message_id": 1, "from_id": None}}, '2026-11-05T04:22:16+00:00'),
    ("log", 'consent-requests.jsonl', {"v": 1, "ts": "2026-11-05T09:21:39+05:00", "src": "scripts/ask-consent.py", "event": "consent.request", "outcome": "ok", "task": "hr.sample", "task_from": "map", "session": "s-sample", "data": {"nonce": "n-sample", "цель": "/tmp/sample", "почему": "образец"}}, '2026-11-05T04:21:39+00:00'),
    ("log", 'db-drops.jsonl', {"ts": "2026-08-13T09:50:12+05:00"}, '2026-08-13T04:50:12+00:00'),
    ("log", 'demon-spend.jsonl', {"когда": "2026-10-27T12:46:53+05:00", "session_id": "s-sample"}, '2026-10-27T07:46:53+00:00'),
    ("log", 'gates.jsonl', {"ts": "2026-10-27T14:02:36+05:00", "gate": "pre-commit", "verdict": "pass"}, '2026-10-27T09:02:36+00:00'),
    ("log", 'gates-timing.jsonl', {"v": 1, "ts": "2026-10-29T02:11:23+05:00", "src": "scripts/gates.sh", "event": "gate.check", "outcome": "ok", "data": {"name": "секреты", "secs": 0.556, "rc": 0}}, '2026-10-28T21:11:23+00:00'),
    ("log", 'guard.jsonl', {"ts": "2026-10-27T14:25:43+05:00", "verdict": "followup", "outcome": "переиначил"}, '2026-10-27T09:25:43+00:00'),
    ("log", 'inbox.jsonl', {"ts": "2026-10-27T13:51:38+05:00"}, '2026-10-27T08:51:38+00:00'),
    ("log", 'model-po-shagu.jsonl', {"ts": "2026-10-18T19:35:37+05:00"}, '2026-10-18T14:35:37+00:00'),
    ("log", 'model-switch.jsonl', {"ts": "2026-10-18T19:35:37+05:00"}, '2026-10-18T14:35:37+00:00'),
    ("log", 'process-load.jsonl', {"v": 1, "ts": "2026-11-06T07:15:02+05:00", "src": "scripts/process-dispatcher.py", "event": "process.sample", "outcome": "ok", "task": "-", "task_from": "arg", "session": None, "data": {"окно_с": 60.1, "ядер": 4, "машина_cpu_с": 182.1, "группы": [{"id": "sample", "прогон": "1", "cpu_с": 1.0, "rss_мб": 10, "процессов": 1}], "остаток": {"cron.service": 0.1, "вне_корзин": 0.1}, "цена": {"cpu_мс": 60, "мс": 70}}}, '2026-11-06T02:15:02+00:00'),
    ("log", 'process-runs.jsonl', {"v": 1, "ts": "2026-11-05T13:20:06+05:00", "src": "scripts/run-background.sh", "event": "process.end", "outcome": "ok", "task": "-", "task_from": "env", "session": None, "data": {"id": "sample", "pid": 1, "rc": 0, "с": 1.0, "cpu_с": 0.01}}, '2026-11-05T08:20:06+00:00'),
    ("log", 'rotation.jsonl', {"ts": "2026-10-27T13:20:05+05:00", "event": "silence_kick", "session": "s-sample"}, '2026-10-27T08:20:05+00:00'),
    ("log", 'skills.jsonl', {"ts": "2026-10-27T09:08:34Z", "skill": "pretask-contract", "task": "hr.harness-detailed-logging", "сессия": "s-sample"}, '2026-10-27T09:08:34+00:00'),
    ("log", 'stack-вопросы.jsonl', {"ts": "2026-10-26T15:45:03.842938+05:00"}, '2026-10-26T10:45:03+00:00'),
    ("log", 'tg_out.jsonl', {"ts": "2026-10-27T14:03:20+05:00"}, '2026-10-27T09:03:20+00:00'),
    ("log", 'tmp-sweep-incident-journal.jsonl', {"когда": "2026-10-24T23:24:19+0500"}, '2026-10-24T18:24:19+00:00'),
    ("log", 'artifact-seals.jsonl', {"ts": "2026-11-06T20:56:38Z", "file": ".tasks/hr.sample/безопасность.yaml", "agent": "a-sample", "model": "sample", "источник": "хук"}, '2026-11-06T20:56:38+00:00'),
    ("log", 'checks-slice.jsonl', {"v": 1, "ts": "2026-11-06T18:04:13+05:00", "src": "scripts/run-in-checks-slice.sh", "event": "checks.slice.queue-busy", "outcome": "refused", "task": "-", "task_from": "env", "session": "s-sample"}, '2026-11-06T13:04:13+00:00'),
    ("log", 'self-sensors.jsonl', {"v": 1, "ts": "2026-11-07T03:05:21+05:00", "src": "scripts/self-sensors.py", "event": "sensors.daily", "outcome": "ok", "task": "hr.sample", "task_from": "map", "session": None}, '2026-11-06T22:05:21+00:00'),
    ("log", 'self-set.jsonl', {"v": 1, "ts": "2026-11-10T20:46:37+05:00", "src": "scripts/self-set.py", "event": "self-set.state", "outcome": "ok", "task": "hr.sample", "task_from": "map", "session": "s-sample", "data": {"поле": "проект.описание", "значение": "", "источник": "вопрос", "состояние": "задан"}}, '2026-11-10T15:46:37+00:00'),
    ("log", 'tokens-2026-08.jsonl', {"ts": "2026-08-31T11:23:10.490Z", "session": "s-sample"}, '2026-08-31T11:23:10+00:00'),
    ("log", 'tokens-2026-09.jsonl', {"ts": "2026-09-30T18:09:42.075Z", "session": "s-sample"}, '2026-09-30T18:09:42+00:00'),
    ("log", 'tokens-2026-10.jsonl', {"ts": "2026-10-27T09:09:34.617Z", "session": "s-sample"}, '2026-10-27T09:09:34+00:00'),
    ("log", 'внимание.jsonl', {"id": "1793085816.170080706", "когда": "2026-10-27T12:23:36+05:00"}, '2026-10-27T07:23:36+00:00'),
    ("log", 'вложенность.jsonl', {"v": 1, "ts": "2026-11-04T09:22:31+05:00", "src": "scripts/hooks/launch_plan_guard.py", "event": "nesting.decision", "outcome": "skipped", "task": "hr.sample", "task_from": "map", "session": "s-sample"}, '2026-11-04T04:22:31+00:00'),
    ("log", 'ответы-в-канал.jsonl', {"v": 1, "ts": "2026-11-04T00:14:53+05:00", "src": "scripts/hooks/channel_reply_guard.py", "event": "channel_reply.block", "outcome": "refused", "task": "hr.sample", "task_from": "map", "session": "s-sample"}, '2026-11-03T19:14:53+00:00'),
    ("log", 'роли-помощников.jsonl', {"v": 1, "ts": "2026-11-04T09:26:18+05:00", "src": "scripts/hooks/lead_role_guard.py", "event": "role.decision", "outcome": "ok", "task": "hr.sample", "task_from": "map", "session": "s-sample"}, '2026-11-04T04:26:18+00:00'),
    ("log", 'merge-queue.jsonl', {"v": 1, "ts": "2026-11-13T04:04:22+05:00", "src": "scripts/merge-queue.py", "event": "mq.слито", "outcome": "ok", "task": "hr.sample", "task_from": "arg", "session": None}, '2026-11-12T23:04:22+00:00'),
    ("log", 'account-buttons.jsonl', {"v": 1, "ts": "2026-11-13T11:10:49+05:00", "src": "scripts/lib/account_buttons.py", "event": "account.menu", "outcome": "ok", "task": "hr.sample", "task_from": "arg", "session": None, "data": {"nonce": "RanfbmYrhajlIy6rvP8qEA", "message_id": 13015, "имена": ["acc-a", "login"]}}, '2026-11-13T06:10:49+00:00'),
    ("log", 'режим-скорости.jsonl', {"v": 1, "ts": "2026-11-05T13:20:06+05:00", "at": "2026-11-05T13:20:03+05:00", "src": "harness/demons/speed_mode.py", "event": "blocker.on", "outcome": "ok", "task": "hr.sample", "task_from": "map", "session": None, "data": {"блокер": "sample", "порог": 1}}, '2026-11-05T08:20:03+00:00'),
    ("log", 'тревоги-ждут.jsonl', {"ts": "2026-11-01T09:01:12.345678+05:00", "key": "sample:broken", "title": "образец", "line": "harness", "watcher": "sample"}, '2026-11-01T04:01:12+00:00'),
    ("log", 'доставка.jsonl', {"v": 1, "ts": "2026-10-28T13:43:12+05:00", "src": "scripts/tg-dispatcher.sh", "event": "delivery.received", "outcome": "ok", "data": {"update_id": "1", "ts": "1793176992.643578412"}}, '2026-10-28T08:43:12+00:00'),
    ("log", 'возможности.jsonl', {"ts": "2026-08-12T11:58:27Z"}, '2026-08-12T11:58:27+00:00'),
    ("log", 'ворота-итог.jsonl', {"ts": "2026-10-27T14:01:22+05:00"}, '2026-10-27T09:01:22+00:00'),
    ("log", 'вызовы-агентов.jsonl', {"ts": "2026-10-27T08:57:01+00:00"}, '2026-10-27T08:57:01+00:00'),
    ("log", 'заявки-на-файлы.jsonl', {"v": 1, "ts": "2026-10-29T10:29:07+05:00", "src": "scripts/hooks/file_claim_guard.py", "event": "file.claim", "outcome": "ok", "task": "-", "task_from": "arg", "session": "s-sample"}, '2026-10-29T05:29:07+00:00'),
    ("log", 'модель-вызовы.jsonl', {"время": 1793091689.7539518}, '2026-10-27T09:01:29+00:00'),
    ("log", 'пайплайн-обходы.jsonl', {"ts": "2026-10-27T14:02:27", "задача": "-"}, '2026-10-27T09:02:27+00:00'),
    ("log", 'панель.jsonl', {"ts": "2026-10-25T08:36:25"}, '2026-10-25T03:36:25+00:00'),
    ("log", 'предложения.jsonl', {"id": "65ce1407", "дата": "2026-10-25", "статус": "открыто"}, 'date:2026-10-25'),
    ("log", 'придержано-лимитом.jsonl', {"ts": "2026-10-25T06:06:29+05:00", "script": "tg_send"}, '2026-10-25T01:06:29+00:00'),
    ("log", 'пропуски.jsonl', {"дата": "2026-10-13"}, 'date:2026-10-13'),
    ("log", 'раскладка-спрошено.jsonl', {"ts": "2026-10-24T20:57:46+05:00"}, '2026-10-24T15:57:46+00:00'),
    ("log", 'спеки-владельцу.jsonl', {"ts": "2026-10-24T02:20:55", "задача": "hr.layout-manifest-removes-stale"}, '2026-10-23T21:20:55+00:00'),
    ("log", 'срок-входа.jsonl', {"ts": "2026-10-27T14:15:02"}, '2026-10-27T09:15:02+00:00'),
    ("log", 'тревоги-сторожей.jsonl', {"ts": "2026-10-27T14:00:30.570787+05:00", "id": ""}, '2026-10-27T09:00:30+00:00'),
    ("log", 'уборка-tmp.jsonl', {"когда": "2026-10-27T03:00:12+0500"}, '2026-10-26T22:00:12+00:00'),
    ("log", 'удаления-секретов.jsonl', {"ts": "2026-10-17T16:53:16+0500"}, '2026-10-17T11:53:16+00:00'),
    ("log", 'удаления-спрошено.jsonl', {"ts": 1792237922.9296453}, '2026-10-17T11:52:02+00:00'),
    ("log", 'evo/summary.jsonl', {"ts": "2026-10-27T14:05:02+05:00", "kind": "hourly"}, '2026-10-27T09:05:02+00:00'),
    ("log", 'rotation.jsonl', {"ts": "2026-10-20T06:00:00Z", "event": "rotate_now", "session": "s-sample"}, '2026-10-20T06:00:00+00:00'),
    ("panel_state", 'панель.jsonl', {"событие": "вход", "ts": "2026-10-24T20:59:35"}, '2026-10-24T15:59:35+00:00'),
    # Журналы без боевого файла на 27.10: строка — по коду писателя (указан), не выдумка.
    ("log", 'account-switch.jsonl', {"v": 1, "ts": "2026-11-13T17:07:45+05:00", "src": "scripts/account-switch.py", "event": "account.switch", "outcome": "ok", "task": "hr.sample", "task_from": "map", "session": None, "data": {"вход": "итог", "откуда": "acc-a", "куда": "acc-b", "код": 0}}, '2026-11-13T12:07:45+00:00'),
    # scripts/deploy.sh → harness_event (формат v1).
    ("log", 'deploy-releases.jsonl', {"v": 1, "ts": "2026-11-14T10:20:31+05:00", "src": "scripts/deploy.sh", "event": "deploy.release", "outcome": "ok", "task": "-", "task_from": "env", "session": None, "data": {"note": "цель=abc1234 /srv/sample", "тег": "sample"}}, '2026-11-14T05:20:31+00:00'),
    # scripts/docker-sweep.py journal_row: время — «когда», UTC isoformat.
    ("log", 'уборка-docker.jsonl', {"когда": "2026-11-14T05:20:31+00:00", "класс": "образ", "идентификатор": "sha256:sample", "мб": 12.5, "правило": "sample", "действие": "удалено"}, '2026-11-14T05:20:31+00:00'),
    # Два писателя очереди досыла: панель (server.py, ts — epoch) и tg_send.sh (ts — date -Is).
    ("log", 'панель-недоставленное.jsonl', {"ts": 1794634831.25, "текст": "образец"}, '2026-11-14T05:40:31+00:00'),
    ("log", 'панель-недоставленное.jsonl', {"ts": "2026-11-14T10:20:31+05:00", "текст": "образец"}, '2026-11-14T05:20:31+00:00'),
    # scripts/comments-intake.py record: ts — имя заявки (epoch строкой, ключ повтора), момент доставки — «доставлено».
    ("log", 'panel-comments-delivered.jsonl', {"ts": "1794634831.250000000", "исход": "доставлено", "доставлено": "2026-11-14T10:20:31+05:00"}, '2026-11-14T05:20:31+00:00'),
    # Писатель — teardown.sh проекта вне дерева харнеса: строка по реестру (поле ts с поясом).
    ("log", 'env-teardown.jsonl', {"ts": "2026-11-14T10:20:31+05:00", "исход": "снято"}, '2026-11-14T05:20:31+00:00'),
]


# ───────────────────────── случаи ─────────────────────────

def case_secret_forms(base: Path) -> None:
    """Ревью Ф1 п.1 и п.6: путь-ключ ищется только целым словом, ключи data,
    src и event не маскируются вовсе. Голый путь (case_secrets) зелен и на
    этой поломке — здесь формы, в которых путь живёт в настоящих командах."""
    print("── БОЛЬНОЙ (ревью п.1): путь-ключ в кавычках, в --file=, после = и ( — не уходит в журнал ──")
    st = Stand(base, "secform")
    tok = f"tok-{secrets.token_hex(4)}"
    key_path = f"{st.panel}/{tok}"
    forms = {"quoted": f'cat "{key_path}"', "single": f"cat '{key_path}'", "opt": f"--file={key_path}",
             "assign": f"FILE={key_path};", "paren": f"open({key_path})", "comma": f"a,{key_path},b"}
    rc, res, err = run_child(emit_body(src=SRC, event="probe.form", outcome="ok", journal="secf", data=forms), st.env())
    lines = st.lines("secf")
    check("формы пути: событие записано (маска, а не отказ)", (0, True, 1), (rc, res, len(lines)))
    d = json.loads(lines[0]).get("data", {}) if lines else {}
    for name, value in forms.items():
        got = str(d.get(name))
        check(f"форма «{name}»: имени файла-ключа нет, на его месте <путь-ключ>", (False, True),
              (tok in got, "<путь-ключ>" in got))
    lost_raw = (st.state / "lost.jsonl").read_text(encoding="utf-8") if (st.state / "lost.jsonl").exists() else ""
    check("формы пути: имени файла-ключа нет ни в журнале, ни в lost, ни в stderr", (False, False, False),
          (tok in "\n".join(lines), tok in lost_raw, tok in err))

    print("── БОЛЬНОЙ (ревью п.6): секрет и путь-ключ в КЛЮЧЕ data (и вложенном), в src и в event ──")
    secret = gen_secret()
    st = Stand(base, "seckey")
    tok = f"tok-{secrets.token_hex(4)}"
    key_path = f"{st.panel}/{tok}"
    data = {secret: "1", "nested": {f"k {secret}": "v"}, key_path: "путь-ключ ключом"}
    rc, res, err = run_child(emit_body(src=SRC, event="probe.key", outcome="ok", journal="seck", data=data), st.env())
    rc2, res2, err2 = run_child(emit_body(src=f"scripts/x.sh {secret}", event=f"probe.{secret}", outcome="ok",
                                          journal="seck"), st.env())
    raw = "\n".join(st.lines("seck"))
    lost_raw = (st.state / "lost.jsonl").read_text(encoding="utf-8") if (st.state / "lost.jsonl").exists() else ""
    check("ключ data: событие записано (rc 0, True)", (0, True), (rc, res))
    check("секрет в ключе data / вложенном ключе: нет в журнале", False, secret in raw)
    check("путь-ключ ключом data: имени файла нет в журнале", False, tok in raw)
    check("секрет в src и event: ребёнок жив (rc 0)", 0, rc2)
    check("секрет в src и event: нет ни в журнале, ни в lost, ни в stderr", (False, False, False),
          (secret in raw, secret in lost_raw, secret in err + err2))

    print("── БОЛЬНОЙ: те же формы через bash-дверь (так пишут демоны) ──")
    st = Stand(base, "secbash")
    tok = f"tok-{secrets.token_hex(4)}"
    script = ('set -euo pipefail; source "$HARNESS_EVENTS_SH"; '
              'harness_event secb scripts/x.sh x.ev ok cmd="cat \\"$P/$T\\"" opt="--file=$P/$T" "$S=1"; echo AFTER')
    p = subprocess.run(["bash", "-c", script], env=st.env(P=str(st.panel), T=tok, S=secret),
                       capture_output=True, text=True, timeout=60, cwd="/")
    raw = "\n".join(st.lines("secb"))
    check("bash-дверь: событие записано, писатель дошёл до конца", (0, True, 1),
          (p.returncode, "AFTER" in p.stdout, len(st.lines("secb"))))
    check("bash-дверь: ни имени файла-ключа (кавычки, --file=), ни секрета-ключа в журнале", (False, False),
          (tok in raw, secret in raw))


def case_index_lock(base: Path) -> None:
    print("── БОЛЬНОЙ (ревью п.2): index/.lock 0660 группы EVENTS_GROUP — сводку собирают оба пользователя ──")
    st = Stand(base, "idxlock")
    rc, res, err = run_child("os.umask(0o022)\nout(ev.index_update())\n", st.env())
    check("index_update: rc 0, без Traceback", (0, False), (rc, "Traceback" in err))
    lock = st.state / "index" / ".lock"
    if not lock.exists():
        check("index/.lock создан", True, False)
        return
    s = lock.stat()
    check("index/.lock 0660 при umask 0022 (fchmod после os.open)", oct(0o660), oct(stat.S_IMODE(s.st_mode)))
    check(f"index/.lock группы {GROUP_NAME}", GROUP_GID, s.st_gid)
    idx = (st.state / "index").stat()
    check("index/ 2770", oct(0o2770), oct(stat.S_IMODE(idx.st_mode)))


def case_lock_timeout(base: Path) -> None:
    print("── БОЛЬНОЙ: блокировка занята дольше 2 с → строка потери, не зависание ──")
    st = Stand(base, "lock")
    p = st.log / "probe.jsonl"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl,os,sys,time\n"
         f"fd=os.open({str(p)!r}, os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o640)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('locked', flush=True)\ntime.sleep(6)\n"],
        stdout=subprocess.PIPE, text=True)
    try:
        holder.stdout.readline()
        t0 = time.monotonic()
        rc, res, err = run_child(emit_body(src=SRC, event="probe.x", outcome="ok", journal="probe"), st.env())
        took = time.monotonic() - t0
    finally:
        holder.kill()
        holder.wait()
    check("занятая блокировка: emit вернул False", False, res)
    check("занятая блокировка: ребёнок не упал (rc 0)", 0, rc)
    check("срок — около 2 с, не вечность и не мгновенно", True, 1.8 <= took <= 4.5)
    check("в журнал ничего не легло", [], st.lines("probe"))
    check("строка потери с причиной lock-timeout", ["lock-timeout"],
          [x.get("reason") for x in st.lost() if x.get("journal") == "probe"])
    check("отказ назван в stderr", True, bool(err.strip()))


def case_unwritable(base: Path) -> None:
    print("── БОЛЬНОЙ: каталог журнала без прав записи → потеря видна, писатель жив ──")
    st = Stand(base, "ro")
    ro = st.root / "ro-log"
    ro.mkdir()
    os.chmod(ro, 0o555)
    try:
        rc, res, err = run_child(emit_body(src=SRC, event="probe.x", outcome="ok", journal="probe"),
                                 st.env(HARNESS_LOG_DIR=str(ro), LOG_DIR=str(ro)))
        check("нет прав: emit вернул False", False, res)
        check("нет прав: rc 0", 0, rc)
        check("нет прав: строка потери с именем журнала", True,
              any(x.get("journal") == "probe" and x.get("kind") == "lost" for x in st.lost()))
        print("── БОЛЬНОЙ: не пишется и журнал, и строка потери → только stderr, без исключения ──")
        rc, res, err = run_child(emit_body(src=SRC, event="probe.x", outcome="ok", journal="probe"),
                                 st.env(HARNESS_LOG_DIR=str(ro), LOG_DIR=str(ro),
                                        HARNESS_EVENTS_STATE=str(ro / ".events")))
        check("двойной отказ: rc 0 (исключение не вышло наружу)", 0, rc)
        check("двойной отказ: emit вернул False", False, res)
        check("двойной отказ: stderr не пуст", True, bool(err.strip()))
        check("двойной отказ: без Traceback", False, "Traceback" in err)
    finally:
        os.chmod(ro, 0o755)


def case_short_write(base: Path) -> None:
    print("── БОЛЬНОЙ: короткая запись (места на 10 байт) откатывается ftruncate ──")
    st = Stand(base, "short")
    body = (
        "import resource, signal\n"
        "signal.signal(signal.SIGXFSZ, signal.SIG_IGN)\n"
        f"ev.emit(src={SRC!r}, event='probe.first', outcome='ok', journal='sw', data={{'a': 'x'}})\n"
        "p = os.path.join(os.environ['HARNESS_LOG_DIR'], 'sw.jsonl')\n"
        "size = os.path.getsize(p)\n"
        "resource.setrlimit(resource.RLIMIT_FSIZE, (size + 10, size + 10))\n"
        f"out(ev.emit(src={SRC!r}, event='probe.big', outcome='ok', journal='sw', data={{'a': 'y' * 600}}))\n"
    )
    rc, res, err = run_child(body, st.env())
    check("короткая запись: emit вернул False", False, res)
    check("короткая запись: rc 0", 0, rc)
    rc, res, err = run_child(emit_body(src=SRC, event="probe.after", outcome="ok", journal="sw"), st.env())
    lines = st.lines("sw")
    valid = 0
    for x in lines:
        try:
            json.loads(x)
            valid += 1
        except ValueError:
            pass
    check("после отката: две строки, обе валидный JSON (обрывок не склеил следующую)", (2, 2), (len(lines), valid))
    check("строка потери short-write", True, any(x.get("reason") == "short-write" for x in st.lost()))


def case_never_raises(base: Path) -> None:
    print("── БОЛЬНОЙ: несериализуемые данные и плохой исход не роняют писателя ──")
    st = Stand(base, "raise")
    rc, res, err = run_child("out(ev.emit(src='x', event='probe.x', outcome='ok', journal='probe', data={'o': object()}))\n",
                             st.env())
    check("object() в data: rc 0 и ответ bool", (0, True), (rc, isinstance(res, bool)))
    check("object() в data: без Traceback", False, "Traceback" in err)
    # object() сериализуется через default=str и исключения не даёт вовсе:
    # «emit не бросает» проверяется только данными, на которых бросает str().
    boom = "class Boom:\n    def __str__(self):\n        raise RuntimeError('boom')\n    __repr__ = __str__\n"
    for label, data in (("значение", "{'v': Boom()}"), ("ключ", "{Boom(): 1}")):
        journal = "boomv" if label == "значение" else "boomk"
        rc, res, err = run_child(boom + f"out(ev.emit(src='x', event='probe.x', outcome='ok', journal={journal!r}, "
                                 f"data={data}))\n", st.env())
        check(f"{label} data, чей __str__ бросает: rc 0 и ответ bool", (0, True), (rc, isinstance(res, bool)))
        check(f"{label} data, чей __str__ бросает: без Traceback", False, "Traceback" in err)
        check(f"{label} data, чей __str__ бросает: след в lost.jsonl", True,
              any(x.get("journal") == journal for x in st.lost()) or res is True)
    rc, res, err = run_child(emit_body(src=SRC, event="probe.x", outcome="weird", journal="badout"), st.env())
    check("исход вне пяти: False", False, res)
    check("исход вне пяти: строка не легла", [], st.lines("badout"))
    check("исход вне пяти: потеря bad-outcome", True,
          any(x.get("reason") == "bad-outcome" for x in st.lost()))


def case_bad_journal_name(base: Path) -> None:
    print("── БОЛЬНОЙ: имя журнала с путём → отказ, файла вне каталога нет ──")
    st = Stand(base, "badname")
    rc, res, err = run_child(emit_body(src=SRC, event="probe.x", outcome="ok", journal="../escape"), st.env())
    check("плохое имя: False", False, res)
    check("плохое имя: файла выше каталога журналов нет", False, (st.root / "escape.jsonl").exists())
    check("плохое имя: потеря bad-journal-name", True,
          any(x.get("reason") == "bad-journal-name" for x in st.lost()))
    lost = st.lost()
    check("строка потери: ровно ключи ts, journal, src, reason, kind",
          True, bool(lost) and set(lost[-1]) == {"ts", "journal", "src", "reason", "kind"})
    check("строка потери: ts с поясом", True, bool(lost) and bool(TS_RE.match(str(lost[-1].get("ts")))))


def case_secrets(base: Path) -> None:
    print("── БОЛЬНОЙ: секрет, хэш вне digest_keys, путь-ключ не уходят в журнал ──")
    sys.path.insert(0, str(ROOT / "scripts" / "lib"))
    import patterns  # noqa: E402 — дверь формы секрета, общая с механизмом
    secret = gen_secret()
    sha = hashlib.sha256(os.urandom(16)).hexdigest()
    if patterns.секреты_в_речи(secret) != [secret]:
        check("проба: сгенерированный секрет узнаётся формой (иначе случай пустой)", [secret],
              patterns.секреты_в_речи(secret))
        return
    if patterns.секреты_в_речи(sha) != [sha]:
        skip("sha256 вне digest_keys", "форма не узнаёт sha256 — случай пустой")
    st = Stand(base, "secret")
    key_path = f"{st.panel}/tok-{secrets.token_hex(4)}"
    keep_path = f"{st.secrets}/tg-token"
    rc, res, err = run_child(emit_body(src=SRC, event="probe.secret", outcome="ok", journal="sec",
                                       data={"note": f"ключ {secret} в речи", "sha256": sha, "other": sha,
                                             "head": f"x {secret}", "file": key_path, "copy": keep_path}),
                             st.env())
    raw = "\n".join(st.lines("sec"))
    check("секрет: emit вернул True (событие пишется)", True, res)
    check("секрет: значения нет в файле", False, secret in raw)
    d = json.loads(st.lines("sec")[0])["data"] if st.lines("sec") else {}
    check("секрет: заменён на <СКРЫТО>", True, "<СКРЫТО>" in str(d.get("note")))
    check("sha256 в ключе digest_keys остался как есть", sha, d.get("sha256"))
    check("тот же sha256 в ключе вне списка — <СКРЫТО>", "<СКРЫТО>", d.get("other"))
    check("digest-ключ, но значение не хэш целиком — судится формой", False, secret in str(d.get("head")))
    check("путь под PANEL_STATE_DIR — <путь-ключ>", "<путь-ключ>", d.get("file"))
    check("путь под SECRETS_DIR остался (журнал удалений без пути бесполезен, И-1)", keep_path, d.get("copy"))
    check("каждая замена посчитана как mangled", True,
          sum(1 for x in st.lost() if x.get("kind") == "mangled") >= 1)


def case_sizes(base: Path) -> None:
    print("── БОЛЬНОЙ: строка больше 16384 байт пишется урезанной, значение 3000 знаков — целиком ──")
    st = Stand(base, "size")
    big = {f"k{i}": "ж" * 3000 for i in range(5)}  # 5 × 6000 байт UTF-8
    rc, res, err = run_child(emit_body(src=SRC, event="probe.big", outcome="ok", journal="big", data=big), st.env())
    lines = st.lines("big")
    check("большая строка: событие записано", (True, 1), (res, len(lines)))
    if lines:
        check("большая строка: ≤ 16384 байт", True, len(lines[0].encode()) <= 16384)
        check("большая строка: data → {truncated_bytes: N}", ["truncated_bytes"], list(json.loads(lines[0]).get("data", {})))
    rc, res, err = run_child(emit_body(src=SRC, event="probe.mid", outcome="ok", journal="mid",
                                       data={"t": "ж" * 3000}), st.env())
    lines = st.lines("mid")
    check("3000 знаков не обрезаны (варианты «ответь цифрой» стоят в конце)", "ж" * 3000,
          json.loads(lines[0])["data"]["t"] if lines else None)
    rc, res, err = run_child(emit_body(src=SRC, event="probe.long", outcome="ok", journal="long",
                                       data={"t": "ж" * 5000}), st.env())
    lines = st.lines("long")
    t = json.loads(lines[0])["data"]["t"] if lines else ""
    check("5000 знаков: начало сохранено, хвост помечен …[+N]", True,
          t.startswith("ж" * 3990) and "…[+" in t and len(t) <= 4020)
    check("обрезки посчитаны как mangled", True,
          sum(1 for x in st.lost() if x.get("kind") == "mangled") >= 2)


def case_format(base: Path) -> None:
    print("── формат конверта v1: ключи, время с поясом, разделители по умолчанию ──")
    st = Stand(base, "fmt")
    before = dt.datetime.now().astimezone()
    rc, res, err = run_child(emit_body(src=SRC, event="guard.refused", outcome="refused", journal="probe",
                                       task="hr.x", session="s1", n=3, unit="count",
                                       data={"verdict": "deny", "ts": "1999-01-01T00:00:00Z"}), st.env())
    lines = st.lines("probe")
    check("одна строка", (True, 1), (res, len(lines)))
    if not lines:
        return
    e = json.loads(lines[0])
    check("v == 1", 1, e.get("v"))
    check("ts — ISO с поясом ±ЧЧ:ММ (как date -Is)", True, bool(TS_RE.match(str(e.get("ts")))))
    if TS_RE.match(str(e.get("ts"))):
        t = parse_ts(e["ts"])
        check("ts — пояс машины", before.utcoffset(), t.utcoffset())
        check("ts — время записи, не переданное писателем", True, abs((t - before).total_seconds()) < 30)
    check("src, event, outcome", (SRC, "guard.refused", "refused"), (e.get("src"), e.get("event"), e.get("outcome")))
    check("task и task_from=arg", ("hr.x", "arg"), (e.get("task"), e.get("task_from")))
    check("session, n, unit", ("s1", 3, "count"), (e.get("session"), e.get("n"), e.get("unit")))
    check("ts писателя остался в data, конверт его не берёт", "1999-01-01T00:00:00Z", e.get("data", {}).get("ts"))
    check("разделители по умолчанию: признак invariants.yaml находится", True, '"verdict": "deny"' in lines[0])
    check("кириллица не экранирована (ensure_ascii=False)", True, "\\u" not in lines[0])
    rc, res, err = run_child(emit_body(src=SRC, event="tokens.spent", outcome="ok", journal="at",
                                       at="2026-10-27T06:00:00Z"), st.env())
    lines = st.lines("at")
    a = json.loads(lines[0]).get("at") if lines else None
    check("at приведён к виду ts (пояс машины, не Z)", True,
          bool(a) and bool(TS_RE.match(a)) and parse_ts(a) == parse_ts("2026-10-27T06:00:00+00:00"))


def case_modes(base: Path) -> None:
    # Второй прогон с umask 0077: при 0022 os.open(…, 0o640) даёт 0640 и без
    # fchmod, и случай был зелен на убранном fchmod журнала (мутация M1).
    for umask in (0o022, 0o077):
        u = f"{umask:04o}"
        print(f"── права при umask {u}: журнал 0640 группы EVENTS_GROUP; .events 2770; файлы .events 0660 ──")
        st = Stand(base, f"modes{u}")
        body = f"os.umask({umask:#o})\n" + emit_body(src=SRC, event="probe.x", outcome="ok", journal="perm") + \
            "ev.emit(src='x', event='probe.x', outcome='ok', journal='../bad')\n"
        rc, res, err = run_child(body, st.env())
        p = st.log / "perm.jsonl"
        if p.exists():
            s = p.stat()
            check(f"umask {u}: новый журнал 0640", oct(0o640), oct(stat.S_IMODE(s.st_mode)))
            check(f"umask {u}: новый журнал группы {GROUP_NAME}", GROUP_GID, s.st_gid)
        else:
            check(f"umask {u}: новый журнал создан", True, False)
        if st.state.exists():
            s = st.state.stat()
            check(f"umask {u}: .events создан самолечением 2770", oct(0o2770), oct(stat.S_IMODE(s.st_mode)))
            check(f"umask {u}: .events группы {GROUP_NAME}", GROUP_GID, s.st_gid)
        else:
            check(f"umask {u}: .events создан самолечением", True, False)
        lp = st.state / "lost.jsonl"
        check(f"umask {u}: lost.jsonl 0660 (пишут оба пользователя)", oct(0o660),
              oct(stat.S_IMODE(lp.stat().st_mode)) if lp.exists() else None)


def case_other_user(base: Path) -> None:
    print("── lost.jsonl, созданный ДРУГИМ пользователем группы, дописывается первым ──")
    if GROUP_NAME != "harness-panel" or subprocess.run(
            ["sudo", "-n", "-u", "harness-panel", "true"], capture_output=True).returncode != 0:
        skip("второй пользователь", "второго пользователя нет (harness-panel или sudo -n)")
        return
    # Механизм, который harness-panel не может прочитать (копия мутаций в
    # mktemp 0700), даёт красное «нет доступа», а не правило: такой прогон
    # правило не судит и называет это, а не краснит чистую копию.
    if subprocess.run(["sudo", "-n", "-u", "harness-panel", "test", "-r", LIB],
                      capture_output=True).returncode != 0:
        skip("второй пользователь", f"механизм {LIB} не читается harness-panel (каталог копии закрыт)")
        return
    st = Stand(base, "other")
    for d in (st.root, st.log, base):
        os.chmod(d, 0o755)
    st.state.mkdir()
    os.chown(st.state, -1, GROUP_GID)
    os.chmod(st.state, 0o2770)  # так каталог ставит установщик Ф1 (§4.2)
    body = "os.umask(0o022)\nout(ev.emit(src='harness/panel/server.py', event='probe.x', outcome='ok', journal='../bad'))\n"
    rc, res, err = run_child(body, st.env(), as_user="harness-panel")
    lp = st.state / "lost.jsonl"
    check("панель создала lost.jsonl", True, lp.exists())
    if not lp.exists():
        print(f"     stderr панели: {err.strip()[:300]}")
        return
    check("файл панели 0660", oct(0o660), oct(stat.S_IMODE(lp.stat().st_mode)))
    run_child("ev.emit(src='x', event='probe.x', outcome='ok', journal='../bad')\n", st.env())
    check("агент дописал строку в файл панели (две строки)", 2, len(st.lost()))


def case_current_task(base: Path) -> None:
    print("── задача: arg > env > ровно одна wip в карте; две wip — не map ──")
    st = Stand(base, "task1", wip=1)
    rc, res, err = run_child(emit_body(src=SRC, event="probe.x", outcome="ok", journal="t"), st.env())
    e = json.loads(st.lines("t")[-1]) if st.lines("t") else {}
    check("одна wip → task из карты, task_from=map", (WIP_CARDS[0], "map"),
          (e.get("task"), e.get("task_from")))
    rc, res, err = run_child(emit_body(src=SRC, event="probe.x", outcome="ok", journal="t"),
                             st.env(HARNESS_TASK="hr.env-task"))
    e = json.loads(st.lines("t")[-1]) if st.lines("t") else {}
    check("HARNESS_TASK → env", ("hr.env-task", "env"), (e.get("task"), e.get("task_from")))
    rc, res, err = run_child(emit_body(src=SRC, event="probe.x", outcome="ok", journal="t", task="hr.arg"),
                             st.env(HARNESS_TASK="hr.env-task"))
    e = json.loads(st.lines("t")[-1]) if st.lines("t") else {}
    check("аргумент старше окружения → arg", ("hr.arg", "arg"), (e.get("task"), e.get("task_from")))
    st2 = Stand(base, "task2", wip=2)
    rc, res, err = run_child(emit_body(src=SRC, event="probe.x", outcome="ok", journal="t"), st2.env())
    e = json.loads(st2.lines("t")[-1]) if st2.lines("t") else {}
    check("две wip → не map (карта задачу не называет)", True, bool(e) and e.get("task_from") != "map")
    check("task_from из словаря §3.1", True, e.get("task_from") in {"arg", "env", "map", "history", "none"})
    print("── сессия: аргумент, иначе CLAUDE_CODE_SESSION_ID ──")
    rc, res, err = run_child(emit_body(src=SRC, event="probe.x", outcome="ok", journal="s"),
                             st.env(CLAUDE_CODE_SESSION_ID="sess-probe"))
    e = json.loads(st.lines("s")[-1]) if st.lines("s") else {}
    check("сессия из окружения", "sess-probe", e.get("session"))
    rc, res, err = run_child(emit_body(src=SRC, event="probe.x", outcome="ok", journal="s", session="sess-arg"),
                             st.env(CLAUDE_CODE_SESSION_ID="sess-probe"))
    e = json.loads(st.lines("s")[-1]) if st.lines("s") else {}
    check("сессия-аргумент старше окружения", "sess-arg", e.get("session"))


def case_bash_door(base: Path) -> None:
    print("── bash-дверь harness_event: rc 0 всегда, писатель под set -e жив ──")
    st = Stand(base, "sh")
    script = ('set -euo pipefail; source "$HARNESS_EVENTS_SH"; '
              'harness_event shj scripts/x.sh x.ev ok k=v --json \'{"a": 1}\'; '
              'harness_event "../bad" scripts/x.sh x.ev ok; echo AFTER')
    p = subprocess.run(["bash", "-c", script], env=st.env(), capture_output=True, text=True, timeout=60, cwd="/")
    check("bash: rc 0 и писатель дошёл до конца", (0, True), (p.returncode, "AFTER" in p.stdout))
    lines = st.lines("shj")
    d = json.loads(lines[0]).get("data", {}) if lines else {}
    check("bash: key=value и --json легли в data", ("v", 1), (d.get("k"), d.get("a")))
    check("bash: плохое имя — строка потери, а не смерть", True,
          any(x.get("reason") == "bad-journal-name" for x in st.lost()))
    # Питон дверью отвечает 0 всегда, и `|| true; return 0` было не судимо
    # (мутация M24 зелёная): здесь питон отвечает 2 на несуществующий файл.
    print("── БОЛЬНОЙ: питон двери упал (rc 2) — писатель под set -e всё равно жив ──")
    missing = st.root / "no-such-events.py"
    py = subprocess.run(["python3", "-I", str(missing), "emit"], capture_output=True, timeout=30)
    if py.returncode == 0:
        check("стенд: python3 на несуществующий файл отвечает не 0 (иначе условие не создано)", "не 0", 0)
        return
    script = 'set -euo pipefail; source "$HARNESS_EVENTS_SH"; harness_event shm scripts/x.sh x.ev ok k=v; echo AFTER'
    p = subprocess.run(["bash", "-c", script], env=st.env(HARNESS_EVENTS_LIB=str(missing)),
                       capture_output=True, text=True, timeout=60, cwd="/")
    check(f"питон двери rc {py.returncode}: писатель rc 0 и дошёл до AFTER", (0, True),
          (p.returncode, "AFTER" in p.stdout))


def case_writer_inode(base: Path) -> None:
    print("── БОЛЬНОЙ (крит. 3а): файл переименовали, пока писатель ждал → строка в НОВОМ файле ──")
    st = Stand(base, "inode")
    p = st.log / "rot.jsonl"
    p.write_text("", encoding="utf-8")
    arch = st.log / "archive-1.jsonl"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl,os,sys\n"
         f"p={str(p)!r}; a={str(arch)!r}\n"
         "fd=os.open(p, os.O_WRONLY|os.O_APPEND)\nfcntl.flock(fd, fcntl.LOCK_EX)\n"
         "print('locked', flush=True)\nsys.stdin.readline()\n"
         "os.link(p, a); os.unlink(p)\n"
         "nfd=os.open(p, os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o640); os.close(nfd)\n"
         "fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)\nprint('rotated', flush=True)\n"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    holder.stdout.readline()
    writer = subprocess.Popen([sys.executable, "-I", "-c", CHILD_HEAD +
                               emit_body(src=SRC, event="probe.x", outcome="ok", journal="rot")],
                              env=st.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd="/")
    time.sleep(0.4)  # писатель открыл путь и ждёт блокировку на старом inode
    holder.stdin.write("go\n")
    holder.stdin.flush()
    holder.wait(timeout=10)
    out, err = writer.communicate(timeout=30)
    check("переименование под писателем: emit True", True, "RESULT=true" in out)
    check("строка в живом журнале", 1, len(st.lines("rot")))
    check("архив не изменился (пуст)", 0, arch.stat().st_size if arch.exists() else -1)

    print("── БОЛЬНОЙ (крит. 3г): пять ротаций подряд за время ожидания → строка в срок 2 с, не rotated-away ──")
    st = Stand(base, "inode5")
    p = st.log / "rot.jsonl"
    p.write_text("", encoding="utf-8")
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl,os,sys,time\n"
         f"p={str(p)!r}; d={str(st.log)!r}\n"
         "fd=os.open(p, os.O_WRONLY|os.O_APPEND)\nfcntl.flock(fd, fcntl.LOCK_EX)\n"
         "print('locked', flush=True)\nsys.stdin.readline()\n"
         "for i in range(5):\n"
         "    os.link(p, f'{d}/archive-{i}.jsonl'); os.unlink(p)\n"
         "    nfd=os.open(p, os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o640); fcntl.flock(nfd, fcntl.LOCK_EX)\n"
         "    fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd); fd=nfd\n"
         "    time.sleep(0.15)\n"
         "fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)\n"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    holder.stdout.readline()
    t0 = time.monotonic()
    writer = subprocess.Popen([sys.executable, "-I", "-c", CHILD_HEAD +
                               emit_body(src=SRC, event="probe.x", outcome="ok", journal="rot")],
                              env=st.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd="/")
    time.sleep(0.3)
    holder.stdin.write("go\n")
    holder.stdin.flush()
    holder.wait(timeout=10)
    out, err = writer.communicate(timeout=30)
    took = time.monotonic() - t0
    check("пять ротаций: emit True (повторы по сроку, не по счёту)", True, "RESULT=true" in out)
    check("пять ротаций: строка в живом журнале", 1, len(st.lines("rot")))
    archived = sum(len((st.log / f"archive-{i}.jsonl").read_text().splitlines())
                   for i in range(5) if (st.log / f"archive-{i}.jsonl").exists())
    check("пять ротаций: в архивах ни строки", 0, archived)
    check("пять ротаций: потери rotated-away нет", False, any(x.get("reason") == "rotated-away" for x in st.lost()))
    check("пять ротаций: уложился в срок (< 3 с с запуском python)", True, took < 3.5)


def case_race(base: Path) -> None:
    print("── гонка: 50 писателей × 200 событий → ровно 10000 валидных строк, каждая пара раз ──")
    st = Stand(base, "race")
    body = ("w = int(os.environ['W'])\n"
            "ok = sum(1 for k in range(200) if ev.emit(src='probe', event='race.x', outcome='ok', journal='race', "
            "task='hr.race', data={'w': w, 'k': k}))\nout(ok)\n")
    procs = []
    for w in range(50):
        env = st.env(W=str(w))
        procs.append(subprocess.Popen([sys.executable, "-I", "-c", CHILD_HEAD + body], env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd="/"))
    reported = 0
    for pr in procs:
        out, _ = pr.communicate(timeout=600)
        for line in out.splitlines():
            if line.startswith("RESULT="):
                reported += int(json.loads(line[7:]))
    lines = st.lines("race")
    pairs, broken = [], 0
    for x in lines:
        try:
            d = json.loads(x)["data"]
            pairs.append((d["w"], d["k"]))
        except (ValueError, KeyError, TypeError):
            broken += 1
    check("писатели доложили 10000 удач", 10000, reported)
    check("в журнале ровно 10000 строк", 10000, len(lines))
    check("битых строк 0", 0, broken)
    check("каждая пара (писатель, номер) ровно раз", 10000, len(set(pairs)))
    check("потерь 0", 0, len(st.lost()))


def case_read(base: Path) -> None:
    print("── читалка: наивное время, Z и +0500 разбираются в aware datetime и сравнимы (крит. 1, 4, В14) ──")
    st = Stand(base, "read")
    naive_local = dt.datetime(2026, 10, 27, 11, 0, 0).astimezone()
    (st.log / "срок-входа.jsonl").write_text(
        json.dumps({"ts": "2026-10-27T11:00:00", "доступ_до": "2026-10-27T15:21:24",
                    "ключ_до": "2026-11-23T20:04:35"}, ensure_ascii=False) + "\n", encoding="utf-8")
    (st.log / "skills.jsonl").write_text(
        json.dumps({"ts": "2026-10-27T06:30:00Z", "skill": "plan-writing", "task": "hr.read",
                    "откуда_задача": "карта", "источник": "хук", "успех": True}, ensure_ascii=False) + "\n"
        + "{битая строка\n", encoding="utf-8")
    (st.log / "уборка-tmp.jsonl").write_text(
        json.dumps({"когда": "2026-10-27T09:00:00+0500", "путь": "/tmp/x", "мб": 0.1, "возраст_ч": 1.0,
                    "правило": "отход пробы", "класс": "отходы", "действие": "удалено"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (st.log / "guard.jsonl").write_text(
        json.dumps({"ts": "2026-10-27T10:00:00+05:00", "verdict": "deny", "rule": "x"}, ensure_ascii=False) + "\n"
        + json.dumps({"ts": "2026-10-27T10:05:00+05:00", "verdict": "followup", "outcome": "отступил"},
                     ensure_ascii=False) + "\n"
        + json.dumps({"v": 1, "ts": "2026-10-27T10:10:00+05:00", "src": SRC, "event": "guard.refused",
                      "outcome": "refused", "task": "hr.read", "task_from": "arg", "session": None,
                      "data": {"verdict": "deny"}}, ensure_ascii=False) + "\n", encoding="utf-8")
    body = (
        "import datetime as dt\n"
        "since = dt.datetime(2026,10,26,12,tzinfo=dt.timezone.utc); until = dt.datetime(2026,10,28,tzinfo=dt.timezone.utc)\n"
        "evs = list(ev.read(since, until, journals=['срок-входа','skills','уборка-tmp','guard']))\n"
        "evs.sort(key=lambda e: e.time)\n"
        "out([{'j': e.journal, 'utc': e.time.astimezone(dt.timezone.utc).isoformat(), 'aware': e.time.tzinfo is not None,"
        " 'tz_assumed': bool(getattr(e, 'tz_assumed', False)), 'outcome': e.outcome, 'task': e.task,"
        " 'task_from': e.task_from, 'src': e.src} for e in evs])\n")
    rc, res, err = run_child(body, st.env())
    check("read: rc 0, без TypeError", (0, False), (rc, "TypeError" in err))
    res = res or []
    check("read: 6 событий (битая строка посчитана, не выдана, не уронила)", 6, len(res))
    check("read: все datetime с поясом", True, bool(res) and all(r["aware"] for r in res))
    by = {r["j"]: r for r in res if r["j"] != "guard"}
    check("+0500 (уборка-tmp, поле когда) → 04:00Z", "2026-10-27T04:00:00+00:00", by.get("уборка-tmp", {}).get("utc"))
    check("наивное (срок-входа) → пояс машины", naive_local.astimezone(dt.timezone.utc).isoformat(),
          by.get("срок-входа", {}).get("utc"))
    check("наивное помечено tz_assumed", True, by.get("срок-входа", {}).get("tz_assumed"))
    check("Z (skills) → 06:30Z", "2026-10-27T06:30:00+00:00", by.get("skills", {}).get("utc"))
    check("skills: задача из своего поля", "hr.read", by.get("skills", {}).get("task"))
    order = [r["j"] for r in res if r["j"] in ("уборка-tmp", "срок-входа", "skills")]
    expect = sorted(["уборка-tmp", "срок-входа", "skills"], key=lambda j: {
        "уборка-tmp": "2026-10-27T04:00:00+00:00", "skills": "2026-10-27T06:30:00+00:00",
        "срок-входа": naive_local.astimezone(dt.timezone.utc).isoformat()}[j])
    check("порядок по разобранному времени, не по строке", expect, order)
    g = [r for r in res if r["j"] == "guard"]
    check("guard: deny → refused, followup(«отступил» — поле механизма) → ok, v1 → refused",
          ["refused", "ok", "refused"], [r["outcome"] for r in g])
    check("guard v1: задача и task_from из конверта", ("hr.read", "arg"),
          (g[-1].get("task"), g[-1].get("task_from")) if g else None)
    check("все исходы — из пяти", True, bool(res) and all(r["outcome"] in OUTCOMES for r in res))


def registry_entries() -> dict:
    """Реестр: `journals:` — имя → запись (`file` относительно `dir`, `dir` ∈
    log|panel_state, `time_kind`, `kind`). Читается как ДАННЫЕ, не механизм."""
    import yaml
    data = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
    j = data.get("journals") or {}
    if isinstance(j, list):
        j = {e.get("name"): e for e in j}
    return j


def case_samples(base: Path) -> None:
    print("── крит. 4: адаптер каждого журнала читает СВОЁ поле времени (образец на журнал) ──")
    try:
        reg = registry_entries()
    except (OSError, ImportError, ValueError) as exc:
        check("реестр journals.yaml читается", "читается", f"{type(exc).__name__}: {exc}")
        return
    # Судимые журналы — КАЖДЫЙ журнал реестра со временем, кроме образцов
    # потока. Не «чей файл есть на этой машине»: так вердикт зависел от того,
    # что успело записаться на боевом диске, — новый журнал краснел пробу на
    # одной машине и молчал на другой.
    judged = set()
    for name, e in reg.items():
        e = e or {}
        if e.get("time_kind") in (None, "none") or e.get("kind") == "sample":
            continue
        judged.add((e.get("dir") or "log", e.get("file") or f"{name}.jsonl"))
    have = {(d, f) for d, f, _, _ in SAMPLES}
    print(f"     журналов реестра со временем: {len(judged)}; образцов: {len(SAMPLES)} на {len(have)} файлов")
    check("журналов к суду больше нуля (пустой реестр — не зелёный)", True, len(judged) > 0)
    check("у каждого судимого журнала есть образец", [], sorted(f"{d}/{f}" for d, f in judged - have))
    for i, (d, f, line, expected) in enumerate(SAMPLES):
        st = Stand(base, f"sample{i}")
        target = (st.panel if d == "panel_state" else st.log) / f
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
        body = ("import datetime as dt\n"
                "evs = list(ev.read(dt.datetime(2026,1,1,tzinfo=dt.timezone.utc), dt.datetime(2027,1,1,tzinfo=dt.timezone.utc)))\n"
                "out([[e.time.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat(), e.time.date().isoformat(),"
                " e.outcome] for e in evs])\n")
        rc, res, err = run_child(body, st.env())
        res = res or []
        if expected.startswith("date:"):
            got = [r[1] for r in res]
            want = [expected[5:]]
        else:
            got = [r[0] for r in res]
            want = [expected]
        check(f"образец {d}/{f}: время {want[0]}", want, got)
        if res:
            check(f"образец {d}/{f}: исход из пяти", True, res[0][2] in OUTCOMES)


def case_contract_extras(base: Path) -> None:
    print("── добор по контракту: 4001 знак, имя с «/», секрет не уходит и в строку потери ──")
    st = Stand(base, "extras")
    run_child(emit_body(src=SRC, event="probe.x", outcome="ok", journal="c4001", data={"t": "ж" * 4001}), st.env())
    lines = st.lines("c4001")
    t = json.loads(lines[0])["data"]["t"] if lines else ""
    check("4001 знак: 4000 + «…[+1]»", "ж" * 4000 + "…[+1]", t)
    rc, res, err = run_child(emit_body(src=SRC, event="probe.x", outcome="ok", journal="a/b"), st.env())
    check("имя «a/b»: False и файла a/b.jsonl нет", (False, False), (res, (st.log / "a" / "b.jsonl").exists()))
    secret = gen_secret()
    rc, res, err = run_child(emit_body(src=SRC, event="probe.x", outcome="ok", journal="csec",
                                       data={"note": f"ключ {secret}"}), st.env())
    lost_raw = (st.state / "lost.jsonl").read_text(encoding="utf-8") if (st.state / "lost.jsonl").exists() else ""
    check("секрет: нет ни в lost.jsonl, ни в stderr", (False, False), (secret in lost_raw, secret in err))
    check("секрет: замена посчитана (mangled есть)", True, any(x.get("kind") == "mangled" for x in st.lost()))
    print("── read(stats=…): битые и образцы потока посчитаны, не выданы ──")
    st = Stand(base, "stats")
    (st.log / "skills.jsonl").write_text(
        json.dumps({"ts": "2026-10-27T06:30:00Z", "skill": "x", "task": "hr.s"}, ensure_ascii=False)
        + "\n{битая\n{ещё битая\n", encoding="utf-8")
    (st.log / "stream-json-sample.jsonl").write_text('{"type": "system", "subtype": "init"}\n', encoding="utf-8")
    body = ("import datetime as dt\nstats = {}\n"
            "evs = list(ev.read(dt.datetime(2026,1,1,tzinfo=dt.timezone.utc), dt.datetime(2027,1,1,tzinfo=dt.timezone.utc), stats=stats))\n"
            "out({'n': len(evs), 'journals': sorted({e.journal for e in evs}), 'stats': stats})\n")
    rc, res, err = run_child(body, st.env())
    res = res or {}
    stats = res.get("stats") or {}
    check("read: одно событие из skills, образец потока не выдан", (1, ["skills"]), (res.get("n"), res.get("journals")))
    check("read: stats['broken']['skills'] == 2", 2, (stats.get("broken") or {}).get("skills"))
    check("read: образец потока посчитан пропущенным", True, bool(stats.get("skipped_sample")))


def main() -> int:
    snap = live_snapshot()
    # Литерал, а не константа: уборка /tmp (test_tmp_sweep, T-R) ищет создателя префикса по литералу в вызове.
    base = Path(tempfile.mkdtemp(prefix="events-lib-probe-"))
    check("стенд: префикс mkdtemp равен STAND_PREFIX", STAND_PREFIX, base.name[:len(STAND_PREFIX)])
    os.chmod(base, 0o755)
    print(f"стенд: {base}; механизм: {LIB}; группа событий: {GROUP_NAME}")
    cases = [case_secret_forms, case_index_lock, case_lock_timeout, case_unwritable, case_short_write, case_never_raises, case_bad_journal_name,
             case_secrets, case_sizes, case_writer_inode, case_format, case_modes, case_other_user,
             case_current_task, case_bash_door, case_read, case_samples, case_contract_extras, case_race]
    try:
        for c in cases:
            try:
                c(base)
            except Exception as exc:  # падение случая — его неудача, а не конец прогона
                check(f"{c.__name__}: случай доработал до конца", "без исключения", f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    check("боевой /var/log/harness/.events не тронут пробой", snap, live_snapshot())
    print(f"\nсудимых случаев {len(cases)}; итого путей {paths}, неудач {failures}, не судится {not_judged}")
    if paths == 0:
        print("ПЛОХО · судить было нечего")
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
