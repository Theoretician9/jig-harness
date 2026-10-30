#!/usr/bin/env python3
"""Probes of the watcher-alert task writer, written BEFORE its code.

Task hr.watchers-report-must-create-model-task, spec edition 2
(.tasks/hr.watchers-report-must-create-model-task/spec.md, criteria K-1, K-4,
K-7, K-8, K-9 part). The owner 25.10.2026: a watcher's message reaches only him
and no task for the model appears. These probes describe the carrier that
turns such a message into a card the model takes by itself.

Every case plays on its OWN stand: own LOG_DIR, own map and archive, own
passport, own panel state, generated token, fake curl. The live map is
compared by sha1 before and after every section
([[the-probe-played-on-production]]). CLAUDE_CODE_SESSION_ID is removed from
the stand environment (spec R-22): a background run inherits it, and the
writer would answer 6 to every case — green by form, not by rule.

    python3 scripts/test_alert_task.py                 # all sections
    python3 scripts/test_alert_task.py --section door  # door|weights|tgsend|patterns|senders|storm
    python3 scripts/test_alert_task.py --section session,badq  # parts: session|empty|drainceil|badq|k5|stormj
    python3 scripts/test_alert_task.py --capture-reference <door.sh>

Doors for mutation runs ([[probe-loads-the-live-mechanism]]):
    HARNESS_ALERT_TASK      path of alert-task.py under test
    HARNESS_CARDS_LIB       directory holding cards.py under test
    HARNESS_TG_SEND         path of tg_send.sh under test
    HARNESS_SENDERS_GATE    path of check-owner-senders.py under test
    WORK_QUEUE_SCRIPT       path of work-queue.py under test
    GUARD_UNDER_TEST        path of hooks/guard_bash.py under test
Last line: `alert-task --selftest: случаев N, падений F`.
"""
from __future__ import annotations

import datetime
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent            # scripts/ (harness/scripts/ in the package)
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

WRITER = Path(os.environ.get("HARNESS_ALERT_TASK") or HERE / "alert-task.py")
CARDS_DIR = Path(os.environ.get("HARNESS_CARDS_LIB") or HERE / "lib")
DOOR = Path(os.environ.get("HARNESS_TG_SEND") or HERE / "tg_send.sh")
SENDERS_GATE = Path(os.environ.get("HARNESS_SENDERS_GATE") or HERE / "check-owner-senders.py")
LIVE_MAP = ROOT / "dev-map.yaml"

JOURNAL_NAME = "тревоги-сторожей.jsonl"
QUEUE_NAME = "тревоги-ждут.jsonl"
TODAY = datetime.date.today().isoformat()

# Literals the REAL gates hunt for are assembled at runtime: written plainly,
# this probe file would itself become a declared sender of the Bot API and a
# holder of the right to speak during the limit.
BOT_API = "https://api." + "telegram.org/bot$T/send" + "Message"
SPEAK_IN_LIMIT = "--несмотря-на-" + "лимит"

cases = Cases()
check = cases.check


# ── helpers ────────────────────────────────────────────────────────────────

def sha1(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return "нет файла"


def clean_env() -> dict:
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("HARNESS_REPORT", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


SAMPLE_MAP = """version: 1
layers:
  - { id: infra,   label: "Инфраструктура", color: "#5aa06a" }
  - { id: product, label: "Продукт",        color: "#2b9bd6" }
queue: []
lines:
  - id: harness
    layer: infra
    name: "Харнес"
    tasks:

      - id: hr.sample-one
        status: plan
        owner_testable: true
        источник: inbox:1790000000.100000000
        заведено: "2026-10-01"
        title: "образец карточки один"
        summary: "образец"
        volume: { loc: 10, complexity: 1, tokens_k: 5, estimated: true }
        when: { started: null, done: null, tested: null, estimated: true }
      - id: hr.sample-two
        status: wip
        owner_testable: false
        title: "образец карточки два"
        summary: "образец"
        volume: { loc: 10, complexity: 1, tokens_k: 5, estimated: true }
        when: { started: "2026-10-01", done: null, tested: null, estimated: true }
  - id: sample-29
    layer: product
    name: "Продукт"
    tasks:
      - id: kt.sample
        status: plan
        owner_testable: false
        title: "образец продукта"
        summary: "образец"
        volume: { loc: 10, complexity: 1, tokens_k: 5, estimated: true }
        when: { started: null, done: null, tested: null, estimated: true }
"""

SAMPLE_ARCHIVE = """version: 1
lines:
  - id: harness
    name: "Харнес"
    tasks:
      - id: hr.alert-arch-x
        status: принято
        owner_testable: false
        источник: тревога:arch:x
        заведено: "2026-10-01"
        title: "тревога из архива"
        summary: "закрыта"
        volume: { loc: 1, complexity: 1, tokens_k: 1, estimated: true }
        when: { started: null, done: null, tested: null, estimated: true }
"""


def manual_card(card_id: str, status: str, source: str) -> str:
    return (f"      - id: {card_id}\n"
            f"        status: {status}\n"
            f"        owner_testable: false\n"
            f"        источник: {source}\n"
            f"        title: \"карточка руками\"\n"
            f"        summary: \"образец\"\n"
            f"        volume: {{ loc: 1, complexity: 1, tokens_k: 1, estimated: true }}\n"
            f"        when: {{ started: null, done: null, tested: null, estimated: true }}\n")


class Stand:
    """Own world of one case group."""

    def __init__(self, base: Path, lock_wait: str = "1", per_watcher: str = "3"):
        self.base = base
        self.project = base / "project"
        self.log = base / "log"
        self.map = self.project / "dev-map.yaml"
        self.archive = self.project / "docs" / "dev-map-archive.yaml"
        self.journal = self.log / JOURNAL_NAME
        self.queue = self.log / QUEUE_NAME
        self.lock = self.log / "locks" / "services" / "dev-map.lock"
        self.board = self.log / "доска.json"
        self.failures = self.log / "tg_failures.log"
        self.secrets = base / "secrets"
        for d in (self.project / "docs", self.lock.parent, self.secrets,
                  base / "panel", base / "home", base / "heartbeat", base / "state"):
            d.mkdir(parents=True, exist_ok=True)
        self.map.write_text(SAMPLE_MAP, encoding="utf-8")
        self.archive.write_text(SAMPLE_ARCHIVE, encoding="utf-8")
        self.secret = "sk" + secrets.token_hex(16)
        (self.secrets / "probe-key").write_text(self.secret + "\n", encoding="utf-8")
        (base / "token").write_text("11" + secrets.token_hex(16) + "\n", encoding="utf-8")
        self.settings = {
            "ACTIVE_LINE": "harness", "ALERT_TASKS_PER_WATCHER": per_watcher,
            "ALERT_MASS_MIN": "3", "ALERT_LOCK_WAIT_SEC": lock_wait,
            "DEVMAP_SUMMARY_MAX": "500", "SERVICE_LOCKS_SUBDIR": "services",
        }
        passport = {
            "PROJECT_DIR": str(self.project), "LOG_DIR": str(self.log),
            "SECRETS_DIR": str(self.secrets), "TMUX_SESSION": "proba-trevog",
            "AGENT_START_CMD": "true", "TG_CHAT_ID": "100500",   # not a secret: stand chat
            "TG_TOKEN_FILE": str(base / "token"), "OWNER_TZ": "",
        }
        (base / "install.conf").write_text(
            "".join(f'{k}="{v}"\n' for k, v in passport.items()), encoding="utf-8")
        (base / "harness.conf").write_text(
            "".join(f'{k}="{v}"\n' for k, v in self.settings.items()), encoding="utf-8")

    def env(self, **extra) -> dict:
        env = clean_env()
        env.update({
            "HARNESS_HOME": str(self.base / "home"),
            "HARNESS_INSTALL_CONF": str(self.base / "install.conf"),
            "HARNESS_CONF": str(self.base / "harness.conf"),
            "PROJECT_DIR": str(self.project), "LOG_DIR": str(self.log),
            "SECRETS_DIR": str(self.secrets),
            "HARNESS_DEVMAP": str(self.map), "DEVMAP_ARCHIVE": str(self.archive),
            "HARNESS_ACTIVE_LINE": "harness", "HARNESS_PANEL_STATE": str(self.base / "panel"),
            "HARNESS_DOSKA": str(self.board), "HEARTBEAT_DIR": str(self.base / "heartbeat"),
            "STATE_DIR": str(self.base / "state"),
        })
        env.update(self.settings)
        env.update(extra)
        return env

    def run(self, *args: str, timeout: float = 60, **extra) -> tuple[int, str, str]:
        try:
            done = subprocess.run([sys.executable, str(WRITER), *args], env=self.env(**extra),
                                  capture_output=True, text=True, timeout=timeout,
                                  cwd=str(self.base))
        except subprocess.TimeoutExpired:
            return -9, "", f"предел {timeout} с истёк"
        return done.returncode, done.stdout, done.stderr

    def popen(self, *args: str, **extra) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, str(WRITER), *args], env=self.env(**extra),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                cwd=str(self.base))

    def raise_alert(self, key: str, title: str = "Беда сторожа", text: str = "", **extra):
        return self.run("--key", key, "--title", title, "--text", text or f"{title}. Подробности.",
                        **extra)

    def tasks(self) -> list[dict]:
        data = yaml.safe_load(self.map.read_text(encoding="utf-8")) or {}
        return [dict(t, _line=line.get("id")) for line in data.get("lines") or []
                for t in line.get("tasks") or []]

    def by_source(self, key: str) -> list[dict]:
        return [t for t in self.tasks() if t.get("источник") == f"тревога:{key}"]

    def events(self, key: str | None = None) -> list[dict]:
        found = []
        try:
            lines = self.journal.read_text(encoding="utf-8").splitlines()
        except OSError:
            return found
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if key is None or row.get("ключ") == key:
                found.append(row)
        return found

    def kinds(self, key: str) -> list[str]:
        return [e.get("событие") for e in self.events(key)]

    def queue_rows(self) -> list[str]:
        try:
            return [x for x in self.queue.read_text(encoding="utf-8").splitlines() if x.strip()]
        except OSError:
            return []

    def set_status(self, card_id: str, status: str) -> None:
        """Absent card: nothing to change — the cases about it are red already."""
        text = self.map.read_text(encoding="utf-8")
        head = f"      - id: {card_id}\n        status: "
        if head not in text:
            return
        start = text.index(head) + len(head)
        end = text.index("\n", start)
        # The validator refuses «принято» without its date: the stand map must
        # stay valid, or the final validator case judges the probe, not the writer.
        if status == "принято":
            status += f'\n        принято_когда: "{TODAY}"'
        self.map.write_text(text[:start] + status + text[end:], encoding="utf-8")

    def add_card(self, block: str) -> None:
        text = self.map.read_text(encoding="utf-8")
        anchor = "      - id: hr.sample-one\n"
        self.map.write_text(text.replace(anchor, block + anchor, 1), encoding="utf-8")

    def hold_lock(self, seconds: float = 30) -> subprocess.Popen:
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl,sys,time\n"
             "f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX)\n"
             "print('held',flush=True); time.sleep(float(sys.argv[2]))",
             str(self.lock), str(seconds)], stdout=subprocess.PIPE, text=True)
        holder.stdout.readline()
        return holder


def config_file(name: str) -> Path | None:
    """harness/config in the project, config/ beside scripts/ in the package."""
    for candidate in (ROOT / "harness" / "config" / name, ROOT / "config" / name):
        if candidate.is_file():
            return candidate
    return None


def writer_exists() -> bool:
    return WRITER.is_file()


def first(text: str) -> str:
    return (text.strip().splitlines() or [""])[0]


# ── section: door (alert-task.py states §3.2 pp.1–9) ───────────────────────

def section_door(tmp: Path) -> None:
    print("── писатель тревог: состояния зова §3.2 ──")
    check("писатель scripts/alert-task.py существует", True, writer_exists())

    st = Stand(tmp / "main")
    validator = HERE / "pre-commit-gate.sh"
    if validator.is_file():
        rc = subprocess.run(["bash", str(validator), "--devmap-only", str(st.map)],
                            capture_output=True, text=True, env=st.env()).returncode
        check("стенд-карта проходит валидатор ДО зова (иначе провал ниже не о писателе)", 0, rc)

    # p.1 call from a session
    before = sha1(st.map)
    rc, out, err = st.raise_alert("nightly-gates:red", CLAUDE_CODE_SESSION_ID="x")
    check("п.1 зов из сессии — код 6", 6, rc)
    check("п.1 карта не тронута", before, sha1(st.map))
    check("п.1 в журнале событие «из-сессии»", ["из-сессии"], st.kinds("nightly-gates:red"))

    # p.2 new card
    ids_before = {t["id"] for t in st.tasks()}
    title = "Ночные ворота красные: check-secrets, test_guard_bash"
    rc, out, err = st.raise_alert("nightly-gates:red", title,
                                  "Сторож nightly-gates: красных 2 — check-secrets, test_guard_bash. "
                                  "Журнал nightly-gates-20261026.log.")
    check("п.2 новая беда — код 0", 0, rc)
    check("п.2 печать «заведена hr.alert-nightly-gates-red»",
          "заведена hr.alert-nightly-gates-red", first(out))
    cards = st.by_source("nightly-gates:red")
    check("п.2 в карте ровно одна карточка с источником тревога:nightly-gates:red", 1, len(cards))
    card = cards[0] if cards else {}
    ids_after = {t["id"] for t in st.tasks()}
    check("п.2 ни один прежний id не пропал", set(), ids_before - ids_after)
    check("п.2 задач стало ровно на одну больше", len(ids_before) + 1, len(ids_after))
    check("п.2 id по приставке линии: hr.alert-<ключ>", "hr.alert-nightly-gates-red", card.get("id"))
    check("п.2 карточка в линии harness", "harness", card.get("_line"))
    check("п.2 status plan", "plan", card.get("status"))
    check("п.2 owner_testable: false — в очередь владельца не встаёт", False,
          card.get("owner_testable"))
    check("п.2 blocked нет (Р-8)", False, "blocked" in card)
    check("п.2 завёл scripts/alert-task.py", "scripts/alert-task.py", card.get("завёл"))
    check("п.2 заведено сегодня", TODAY, str(card.get("заведено")))
    check("п.2 title — заголовок зова", title, card.get("title"))
    check("п.2 проверка.команда — форма приёмки --is-green",
          "python3 scripts/alert-task.py --is-green nightly-gates:red",
          (card.get("проверка") or {}).get("команда"))
    check("п.2 проверка.ждём — «ЗЕЛЁНОЕ <ключ>»", "ЗЕЛЁНОЕ nightly-gates:red",
          (card.get("проверка") or {}).get("ждём"))
    depth = card.get("глубина") or {}
    check("Р-10 глубина.характер == починка (не полный пайплайн на каждый отказ)",
          "починка", depth.get("характер"))
    check("глубина.кем == автомат", "автомат", depth.get("кем"))
    summary = str(card.get("summary") or "")
    check("Р-7 summary несёт «Перепроверить сторожем:»", True, "Перепроверить сторожем:" in summary)
    check("summary не длиннее 500", True, 0 < len(summary) <= 500)
    rows = st.events("nightly-gates:red")
    born = [e for e in rows if e.get("событие") == "заведена"]
    check("п.2 в журнале одна строка «заведена»", 1, len(born))
    if born:
        check("журнал: строка несёт id карточки", "hr.alert-nightly-gates-red", born[0].get("id"))
        check("журнал: сторож — часть ключа до «:»", "nightly-gates", born[0].get("сторож"))
        check("журнал: ts есть", True, bool(born[0].get("ts")))
    raw = st.journal.read_text(encoding="utf-8").splitlines() if st.journal.exists() else []
    check("журнал: каждая строка — JSON короче 4 КБ", True,
          bool(raw) and all(len(x.encode()) < 4096 and json.loads(x) for x in raw))

    # p.3 repeat to an open card
    before = sha1(st.map)
    rc, out, _ = st.raise_alert("nightly-gates:red", title)
    check("п.3 повтор к открытой — код 0", 0, rc)
    check("п.3 печать «повтор hr.alert-nightly-gates-red»", "повтор hr.alert-nightly-gates-red",
          first(out))
    check("п.3 карта НЕ правится на повторе (Р-2)", before, sha1(st.map))
    check("п.3 в журнале «повтор»", "повтор", (st.kinds("nightly-gates:red") or [""])[-1])

    # p.3 card in test is still a repeat
    st.set_status("hr.alert-nightly-gates-red", "test")
    before = sha1(st.map)
    rc, out, _ = st.raise_alert("nightly-gates:red", title)
    check("п.3 карточка в test — тоже повтор, код 0", 0, rc)
    check("п.3 карточка в test — карта не тронута, дубля нет (Р-7)", before, sha1(st.map))
    check("п.3 карточка в test — карточек по источнику одна", 1,
          len(st.by_source("nightly-gates:red")))

    # p.2 closed card → a new case with a suffix
    for closed, key in (("принято", "closed-p:x"), ("done", "closed-d:x")):
        rc, out, _ = st.raise_alert(key, "Беда, которая закроется")
        first_id = (st.by_source(key) or [{}])[0].get("id")
        if first_id:
            st.set_status(first_id, closed)
        rc, out, _ = st.raise_alert(key, "Та же беда после закрытия")
        ids = sorted(t["id"] for t in st.by_source(key))
        check(f"п.2 после «{closed}» новая беда — новая карточка (код 0)", 0, rc)
        check(f"п.2 после «{closed}» карточек по источнику две, id различны", 2, len(set(ids)))

    # p.2 archive takes part in the id set
    rc, out, _ = st.raise_alert("arch:x", "Беда, чья прежняя карточка уже в архиве")
    ids = [t["id"] for t in st.by_source("arch:x")]
    check("п.2 архивная карточка «принято» не мешает новой (код 0)", 0, rc)
    check("п.2 id новой не совпадает с id архивной (занятые = карта + архив)", True,
          len(ids) == 1 and ids[0] != "hr.alert-arch-x")

    # p.4 backlog
    st.add_card(manual_card("hr.alert-bk-x", "беклог", "тревога:bk:x"))
    before = sha1(st.map)
    rc, out, _ = st.raise_alert("bk:x", "Беда по беклогу")
    check("п.4 карточка в беклог — код 0", 0, rc)
    check("п.4 карта не тронута (его слово 28.09)", before, sha1(st.map))
    check("п.4 в журнале «беклог»", ["беклог"], st.kinds("bk:x"))

    # bad calls
    for bad in ("a b", "A:x", "a;rm -rf x", "nightly", "", "x:" + "a" * 80, "a:x;rm"):
        before = sha1(st.map)
        rc, out, err = st.raise_alert(bad, "кривой")
        check(f"кривой ключ {bad[:20]!r} — код 2 (писатель на месте, отказ его)",
              (2, True), (rc, writer_exists()))
        check(f"кривой ключ {bad[:20]!r} — карта не тронута", before, sha1(st.map))
    rc, _, _ = st.run()
    check("зов без доводов — код 2", (2, True), (rc, writer_exists()))
    rc, _, _ = st.run("--is-green", "a;rm")
    check("--is-green 'a;rm' — код 2", (2, True), (rc, writer_exists()))
    long_ok = "x:" + "a" * 78
    rc, out, _ = st.raise_alert(long_ok, "ключ длиной ровно 80")
    check("ключ длиной ровно 80 принимается", 0, rc)

    # summary: secret cut before trimming, long text trimmed
    rc, _, _ = st.raise_alert("sec:x", "Беда с ключом в тексте",
                              "Начало. " + st.secret + " конец. " + "Длинный хвост вести. " * 200)
    check("секрет из каталога ключей в карту не попал", False,
          st.secret in st.map.read_text(encoding="utf-8"))
    st.raise_alert("sec2:x", "Заголовок с ключом " + st.secret, "Текст без ключа.")
    check("секрет в заголовке зова в карту не попал", False,
          st.secret in st.map.read_text(encoding="utf-8"))
    summary = str((st.by_source("sec:x") or [{}])[0].get("summary") or "")
    check("длинный текст обрезан до 500, карточка есть", True, 0 < len(summary) <= 500)
    check("длинный текст режется в теле: хвост «Перепроверить сторожем:» цел", True,
          "Перепроверить сторожем:" in summary and summary.endswith("."))

    # p.8 resolve / --is-green / --open
    rc, out, _ = st.run("--is-green", "nightly-gates:red")
    check("--is-green открытой: код 1", 1, rc)
    check("--is-green открытой: «КРАСНОЕ nightly-gates:red…»", True,
          first(out).startswith("КРАСНОЕ nightly-gates:red"))
    rc, out, _ = st.run("--open")
    check("--open: код 0 и называет ключ с id", True,
          rc == 0 and "nightly-gates:red" in out and "hr.alert-nightly-gates-red" in out)
    before = sha1(st.map)
    rc, out, _ = st.run("--resolve", "nightly-gates:red")
    check("п.8 --resolve открытого — код 0", 0, rc)
    check("п.8 --resolve карту не трогает", before, sha1(st.map))
    check("п.8 в журнале «зелёное»", "зелёное", (st.kinds("nightly-gates:red") or [""])[-1])
    rc, out, _ = st.run("--is-green", "nightly-gates:red")
    check("--is-green после зелёного: код 0 и «ЗЕЛЁНОЕ nightly-gates:red»",
          (0, "ЗЕЛЁНОЕ nightly-gates:red"), (rc, first(out)))
    rows_before = len(st.events())
    rc, out, _ = st.run("--resolve", "never-seen:x")
    check("п.8 --resolve незнакомого ключа — код 0", 0, rc)
    check("п.8 --resolve незнакомого ключа журнал не шумит", rows_before, len(st.events()))
    rc, out, _ = st.run("--is-green", "never-seen:x")
    check("--is-green незнакомого: код 1 и «НЕТ never-seen:x»", (1, "НЕТ never-seen:x"),
          (rc, first(out)))
    rc, _, _ = st.raise_alert("nightly-gates:red", title)
    rc, out, _ = st.run("--is-green", "nightly-gates:red")
    check("зелёное, потом беда снова — ключ снова красный", 1, rc)

    # p.5 ceiling per watcher
    print("── потолок по сторожу (Р-5) ──")
    for name in ("a", "b", "c"):
        st.raise_alert(f"cap:{name}", f"Беда cap {name}")
    before = sha1(st.map)
    rc, out, _ = st.raise_alert("cap:d", "Четвёртая беда того же сторожа")
    check("п.5 четвёртый ключ сторожа — код 5", 5, rc)
    check("п.5 карточки четвёртому нет", 0, len(st.by_source("cap:d")))
    check("п.5 карта не тронута", before, sha1(st.map))
    check("п.5 в журнале «потолок»", ["потолок"], st.kinds("cap:d"))
    check("п.5 доска называет «тревоги-выше-потолка»", True,
          st.board.exists() and "тревоги-выше-потолка" in st.board.read_text(encoding="utf-8"))
    check("п.5 у сторожа ровно 3 карточки", 3,
          sum(len(st.by_source(f"cap:{n}")) for n in "abcd"))
    rc, out, _ = st.raise_alert("cap:a", "Повтор к открытой")
    check("п.5 повтор к открытой потолком не режется (код 0, «повтор»)",
          (0, "повтор"), (rc, first(out).split(" ")[0]))
    rc, _, _ = st.raise_alert("other:x", "Беда другого сторожа")
    check("п.5 потолок по сторожу, а не общий: чужой сторож заводит", 0, rc)
    st.run("--resolve", "cap:a")
    rc, _, _ = st.raise_alert("cap:d", "Четвёртая после зелёного одной из трёх")
    check("п.5 потолок считается по журналу: после зелёного cap:a место есть (код 0)", 0, rc)
    closed = (st.by_source("cap:b") or [{}])[0].get("id")
    if closed:
        st.set_status(closed, "done")
    rc, _, _ = st.raise_alert("cap:e", "Пятая после закрытия карточки cap:b руками")
    check("п.5 карточка, закрытая без «зелёного», места не держит (код 0)", 0, rc)

    # p.11: inactive line
    rc, out, err = st.run("--key", "prod:x", "--title", "Беда продукта",
                          "--text", "Беда продукта.", "--line", "sample-29")
    cards = st.by_source("prod:x")
    check("Р-15 неактивная линия — карточка всё равно есть, код 0", (0, 1), (rc, len(cards)))
    check("Р-15 карточка в линии sample-29 с её приставкой", True,
          bool(cards) and cards[0].get("_line") == "sample-29" and str(cards[0].get("id")).startswith("kt."))
    check("Р-15 вслух: «не активна» в выводе", True, "не активна" in out + err)
    check("Р-15 в журнале «почему» называет неактивную линию", True,
          any("не активна" in str(e.get("почему")) for e in st.events("prod:x")))

    # weeder and validator on the map after all writes
    if validator.is_file():
        rc = subprocess.run(["bash", str(validator), "--devmap-only", str(st.map)],
                            capture_output=True, text=True, env=st.env()).returncode
        check("стенд-карта после писателя проходит валидатор", 0, rc)
    check("ни одна карточка тревоги не длиннее 500 знаков summary (прополка)", [],
          [t["id"] for t in st.tasks() if str(t.get("источник", "")).startswith("тревога:")
           and len(str(t.get("summary") or "")) > 500])
    weeder = HERE / "weed-the-map.py"
    if weeder.is_file():
        copy = tmp / "weed"
        (copy / "scripts").mkdir(parents=True)
        shutil.copytree(HERE / "lib", copy / "scripts" / "lib",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(weeder, copy / "scripts" / "weed-the-map.py")
        shutil.copy(st.map, copy / "dev-map.yaml")
        rc = subprocess.run([sys.executable, str(copy / "scripts" / "weed-the-map.py"),
                             "--проверить"], capture_output=True, text=True,
                            env=st.env(PROJECT_DIR=str(copy))).returncode
        check("стенд-карта после писателя проходит weed-the-map.py --проверить", 0, rc)

    section_lock(tmp)
    section_broken_map(tmp)
    section_pause(tmp)
    section_parallel(tmp)
    section_session(tmp)
    section_empty_line(tmp)
    section_drain_ceiling(tmp)
    section_bad_queue_row(tmp)


# ── review r1 (R1-01): a session must not write the alert state by hand ──────

GUARD = Path(os.environ.get("GUARD_UNDER_TEST") or HERE / "hooks" / "guard_bash.py")


def ancestors_named(name: str) -> list[int]:
    """PIDs of this process's ancestors whose comm is `name`."""
    found, pid = [], os.getppid()
    while pid > 1:
        try:
            status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        except OSError:
            break
        fields = dict(line.split(":\t", 1) for line in status.splitlines() if ":\t" in line)
        if fields.get("Name", "").strip() == name:
            found.append(pid)
        pid = int(fields.get("PPid", "0").strip() or 0)
    return found


def section_session(tmp: Path) -> None:
    """The carrier of «called from a session» is the process ANCESTRY (a `claude`
    ancestor), not the environment: `env -u CLAUDE_CODE_SESSION_ID` strips the
    env sign in one command. The launcher below is bash under the name `claude`
    and it FORKS (the trailing `; exit $rc` stops bash from exec-ing the last
    command), so the writer sees a real `claude` parent. The control launcher is
    the same bash under another name: a carrier refusing «any bash parent»
    would turn the control red. Reasons: .tasks/hr.watchers-report-must-create-
    model-task/p-r2-notes.md."""
    print("── зов из сессии: --resolve и обход env -u (R1-01) ──")
    check("проба сама не под сессией (иначе случаи ниже судят форму: claude-предок есть у всех)",
          [], ancestors_named("claude"))
    st = Stand(tmp / "session")
    bash = shutil.which("bash") or "/bin/bash"
    launchers = st.base / "launchers"
    launchers.mkdir()
    for name in ("claude", "watcher-sh"):
        (launchers / name).symlink_to(bash)

    def under(launcher: str, *args: str, via: str = "") -> tuple[int, str, str]:
        """`via` puts one more forking shell between the launcher and the
        writer: a script of the shift's own makes `claude` a GRANDPARENT."""
        script = 'env -u CLAUDE_CODE_SESSION_ID "$@"; rc=$?; exit $rc'
        middle = [str(launchers / via), "-c", '"$@"; rc=$?; exit $rc', via] if via else []
        done = subprocess.run([str(launchers / launcher), "-c", script, launcher, *middle,
                               sys.executable, str(WRITER), *args],
                              env=st.env(), capture_output=True, text=True, timeout=60,
                              cwd=str(st.base))
        return done.returncode, done.stdout, done.stderr

    rc, _, _ = st.raise_alert("ses:x", "Беда для зова из сессии")
    check("стенд: тревога ses:x заведена сторожем (код 0)", 0, rc)

    before = st.kinds("ses:x")
    rc, _, _ = st.run("--resolve", "ses:x", CLAUDE_CODE_SESSION_ID="x")
    check("R1-01 --resolve с CLAUDE_CODE_SESSION_ID — код 6", 6, rc)
    check("R1-01 --resolve с CLAUDE_CODE_SESSION_ID — «зелёное» в журнал не легло", 0,
          st.kinds("ses:x")[len(before):].count("зелёное"))
    rc, out, _ = st.run("--is-green", "ses:x")
    check("R1-01 после --resolve из сессии --is-green — код 1 (приёмка не закрывает)", 1, rc)

    before = st.kinds("ses:x")
    rc, _, err = under("claude", "--resolve", "ses:x")
    check("R1-01 обход env -u под предком claude: --resolve — код 6", 6, rc)
    check("R1-01 обход env -u: «зелёное» в журнал не легло", 0,
          st.kinds("ses:x")[len(before):].count("зелёное"))
    rc, _, _ = st.run("--is-green", "ses:x")
    check("R1-01 обход env -u: --is-green — код 1", 1, rc)

    map_before = sha1(st.map)
    rc, _, _ = under("claude", "--key", "ses:y", "--title", "Беда руками смены", "--text", "t")
    check("R1-01 обход env -u под предком claude: --key — код 6", 6, rc)
    check("R1-01 обход env -u: --key карту не тронул", map_before, sha1(st.map))
    rc, _, _ = under("claude", "--key", "ses:z", "--title", "Беда скриптом смены", "--text", "t",
                     via="watcher-sh")
    check("R1-01 claude — дед, не отец (скрипт смены посредником): --key — код 6", 6, rc)

    rc, out, _ = under("claude", "--is-green", "ses:x")
    check("--is-green из сессии читает (код 1, «КРАСНОЕ»): чтение не запрещено", (1, True),
          (rc, out.startswith("КРАСНОЕ ses:x")))

    rc, _, _ = under("watcher-sh", "--resolve", "ses:x")
    check("контроль: тот же запуск под другим именем — --resolve код 0", 0, rc)
    rc, out, _ = st.run("--is-green", "ses:x")
    check("контроль: после --resolve сторожа --is-green 0", 0, rc)

    if GUARD.is_file():
        print("── сторож команд: alert-task.py из сессии (R1-01, второй носитель) ──")
        for verdict, command in (
                (2, "env -u CLAUDE_CODE_SESSION_ID python3 scripts/alert-task.py --resolve nightly-gates:red"),
                (2, "python3 scripts/alert-task.py --resolve nightly-gates:red"),
                (2, "CLAUDE_CODE_SESSION_ID= python3 scripts/alert-task.py --key a:b --title t"),
                (2, "setsid nohup env -u CLAUDE_CODE_SESSION_ID python3 "
                    "/var/www/project/scripts/alert-task.py --resolve a:b > /dev/null 2>&1 &"),
                (0, "python3 scripts/alert-task.py --is-green nightly-gates:red"),
                (0, "python3 scripts/alert-task.py --open")):
            done = subprocess.run([sys.executable, str(GUARD)],
                                  input=json.dumps({"tool_name": "Bash",
                                                    "tool_input": {"command": command}}),
                                  capture_output=True, text=True, env=clean_env(), timeout=60)
            check(f"guard_bash: «{command[:70]}» → {verdict}", verdict, done.returncode)


# ── review r1 (R1-02): a line without tasks loses the intent ─────────────────

EMPTY_LINES = ('  - id: empty-a\n    layer: product\n    name: "Пустая, tasks: []"\n'
               '    tasks: []\n'
               '  - id: empty-b\n    layer: product\n    name: "Пустая хвостом файла"\n'
               '    tasks:\n')


def section_empty_line(tmp: Path) -> None:
    """The live map has two lines without tasks (sample-29 `tasks: []`, app-cms
    `tasks:` at the end of the file). The writer may either put the card into
    such a line or refuse; what it may not do is lose the intent."""
    print("── линия без задач: намерение не теряется (R1-02) ──")
    for line_id in ("empty-a", "empty-b"):
        st = Stand(tmp / f"empty-{line_id}")
        st.map.write_text(SAMPLE_MAP + EMPTY_LINES, encoding="utf-8")
        validator = HERE / "pre-commit-gate.sh"
        if validator.is_file():
            rc = subprocess.run(["bash", str(validator), "--devmap-only", str(st.map)],
                                capture_output=True, text=True, env=st.env()).returncode
            check(f"{line_id}: стенд-карта с пустой линией проходит валидатор ДО зова", 0, rc)
        ids_before = {t["id"] for t in st.tasks()}
        key = f"{line_id}:x"
        rc, out, err = st.run("--key", key, "--title", "Беда пустой линии", "--text", "t",
                              "--line", line_id)
        in_line = [t for t in st.by_source(key) if t.get("_line") == line_id]
        board = st.board.read_text(encoding="utf-8") if st.board.exists() else ""
        failures = st.failures.read_text(encoding="utf-8") if st.failures.exists() else ""
        carded = rc == 0 and len(in_line) == 1
        refused = (any(f'"{key}"' in r for r in st.queue_rows()) and "отказ" in st.kinds(key)
                   and "тревога-не-заведена" in board and key in failures)
        verdict = "сохранено" if carded or refused else (
            f"потеряно: код {rc}, очередь {len(st.queue_rows())}, журнал {st.kinds(key)}, "
            f"доска {'тревога-не-заведена' in board}")
        check(f"R1-02 {line_id}: карточка в линии ЛИБО очередь+журнал «отказ»+доска+tg_failures",
              "сохранено", verdict)
        check(f"R1-02 {line_id}: без трассировки (исход назван словом)", False, "Traceback" in err)
        ids_after = {t["id"] for t in st.tasks()}
        check(f"R1-02 {line_id}: прежние задачи целы", set(), ids_before - ids_after)


# ── review r1 (R1-05): the drain must not drop an intent hitting the ceiling ─

def section_drain_ceiling(tmp: Path) -> None:
    print("── донос упёрся в потолок: доска и своё число (R1-05) ──")
    st = Stand(tmp / "drain-cap", lock_wait="1", per_watcher="3")
    for name in "abc":
        st.raise_alert(f"cap:{name}", f"Беда cap {name}")
    check("стенд: у сторожа cap три карточки, доски потолка нет", (3, False),
          (sum(len(st.by_source(f"cap:{n}")) for n in "abc"),
           st.board.exists() and "тревоги-выше-потолка" in st.board.read_text(encoding="utf-8")))
    holder = st.hold_lock(30)
    try:
        rc, _, _ = st.raise_alert("cap:d", "Четвёртая беда при занятом локе", timeout=20)
    finally:
        holder.kill()
        holder.wait()
    check("стенд: четвёртая отложена (код 4)", 4, rc)
    rc, out, _ = st.run("--drain")
    check("R1-05 донос: код 0", 0, rc)
    check("R1-05 донос у потолка пишет доску «тревоги-выше-потолка»", True,
          st.board.exists() and "тревоги-выше-потолка" in st.board.read_text(encoding="utf-8"))
    check("R1-05 донос называет потолок своим числом, а не «снято»", (True, False),
          (bool(re.search(r"потол\w*\s*:?\s*1", out)), "снято 1" in out))


# ── review r1 (R1-08): one broken queue row must not stop every alert ────────

def section_bad_queue_row(tmp: Path) -> None:
    print("── строка очереди без ключа (R1-08) ──")
    st = Stand(tmp / "bad-queue")
    st.log.mkdir(parents=True, exist_ok=True)
    st.queue.write_text('{"ts": "x"}\n', encoding="utf-8")
    rc, _, err = st.raise_alert("badq:x", "Беда после битой строки очереди")
    check("R1-08 битая строка очереди: следующий зов — код 0 и карточка", (0, 1),
          (rc, len(st.by_source("badq:x"))))
    check("R1-08 битая строка очереди: без трассировки", False, "Traceback" in err)
    st.queue.write_text('{"ts": "x"}\n', encoding="utf-8")
    rc, _, err = st.run("--drain")
    check("R1-08 --drain с битой строкой — код 0 без трассировки", (0, False),
          (rc, "Traceback" in err))


def section_lock(tmp: Path) -> None:
    print("── лок занят: отложено и донесено (п.6, п.9, Р-1, Р-23) ──")
    st = Stand(tmp / "lock", lock_wait="1")
    holder = st.hold_lock(30)
    try:
        before = sha1(st.map)
        started = time.time()
        rc, out, _ = st.raise_alert("held:x", "Беда при занятом локе", timeout=20)
        spent = time.time() - started
        check("п.6 лок занят чужим процессом — код 4 (не отказ)", 4, rc)
        check("п.6 предел ожидания держится (≤ 10 с при пороге 1 с)", True, spent <= 10)
        check("п.6 печать «отложено held:x»", "отложено held:x", first(out))
        check("п.6 карта не тронута", before, sha1(st.map))
        check("п.6 намерение легло в очередь отложенных", 1, len(st.queue_rows()))
        check("п.6 строка очереди несёт ключ", True, any('"key": "held:x"' in r.replace('":"', '": "')
                                                         for r in st.queue_rows()))
        check("п.6 в журнале «отложено»", ["отложено"], st.kinds("held:x"))
        rc, _, _ = st.raise_alert("gone:x", "Беда, которая уйдёт до записи", timeout=20)
        check("п.6 вторая беда при занятом локе — тоже 4", 4, rc)
        rc, _, _ = st.run("--resolve", "gone:x")
        check("Р-23 --resolve отложенного ключа — код 0", 0, rc)
        rc, out, _ = st.run("--drain", timeout=20)
        check("п.9 донос при занятом локе: строки остались (2)", 2, len(st.queue_rows()))
        check("п.9 донос при занятом локе печатает «в очереди 2»", True, "в очереди 2" in out)
    finally:
        holder.kill()
        holder.wait()
    rc, out, _ = st.run("--drain")
    check("п.9 донос после отпускания — код 0", 0, rc)
    check("п.9 отложенная беда донесена карточкой", 1, len(st.by_source("held:x")))
    check("п.9 в журнале «донесена»", True, "донесена" in st.kinds("held:x"))
    check("Р-23 беда, ушедшая до записи, карточки не получила", 0, len(st.by_source("gone:x")))
    check("Р-23 в журнале «снята-до-записи»", True, "снята-до-записи" in st.kinds("gone:x"))
    check("п.9 очередь пуста после доноса", 0, len(st.queue_rows()))
    check("п.9 печать «донос: донесено 1, повторов 0, снято 1, осталось 0»", True,
          "донос: донесено 1, повторов 0, снято 1, осталось 0" in out)
    check("К-1 строк «отказ» в журнале 0", 0,
          sum(1 for e in st.events() if e.get("событие") == "отказ"))

    holder = st.hold_lock(30)
    try:
        st.raise_alert("late:x", "Отложенная до следующего зова", timeout=20)
    finally:
        holder.kill()
        holder.wait()
    rc, _, _ = st.raise_alert("next:x", "Следующий зов доносит очередь")
    check("п.9 следующий обычный зов доносит очередь сам", (0, 1, 0),
          (rc, len(st.by_source("late:x")), len(st.queue_rows())))


def section_broken_map(tmp: Path) -> None:
    print("── карта нечитаема (п.7) ──")
    st = Stand(tmp / "broken")
    good = st.map.read_text(encoding="utf-8")
    st.map.write_text(good + "  - id: [сломано\n", encoding="utf-8")
    before = sha1(st.map)
    rc, out, err = st.raise_alert("broken:x", "Беда при битой карте")
    check("п.7 битая карта — код 1", 1, rc)
    check("п.7 битая карта не тронута", before, sha1(st.map))
    check("п.7 намерение легло в очередь отложенных", 1, len(st.queue_rows()))
    check("п.7 в журнале «отказ» с причиной", True,
          any(e.get("событие") == "отказ" and e.get("почему") for e in st.events("broken:x")))
    fails = []
    if st.failures.exists():
        for line in st.failures.read_text(encoding="utf-8").splitlines():
            try:
                fails.append(json.loads(line))
            except ValueError:
                pass
    check("п.7 строка {script: alert-task, why} в tg_failures.log стенда", True,
          any(f.get("script") == "alert-task" and f.get("why") for f in fails))
    check("п.7 доска называет «тревога-не-заведена»", True,
          st.board.exists() and "тревога-не-заведена" in st.board.read_text(encoding="utf-8"))
    st.map.write_text(good, encoding="utf-8")
    rc, _, _ = st.run("--drain")
    check("п.7 карту починили — донос заводит карточку", (0, 1), (rc, len(st.by_source("broken:x"))))


def section_pause(tmp: Path) -> None:
    print("── пауза владельца (п.10, Р-20) ──")
    st = Stand(tmp / "pause")
    pause = HERE / "pause.py"
    rc = subprocess.run([sys.executable, str(pause), "--поставить", "проба тревог",
                         "--кто", "владелец"], env=st.env(), capture_output=True).returncode
    check("пауза на стенде поставлена (иначе случай ниже ни о чём)", 0, rc)
    rc, _, _ = st.raise_alert("paused:x", "Беда во время паузы")
    check("п.10 писатель на паузе работает как обычно (код 0, карточка)", (0, 1),
          (rc, len(st.by_source("paused:x"))))
    rc, _, _ = st.run("--resolve", "paused:x")
    rc, out, _ = st.run("--is-green", "paused:x")
    check("Р-20 --resolve во время паузы → --is-green 0", 0, rc)
    subprocess.run([sys.executable, str(pause), "--снять"], env=st.env(), capture_output=True)
    lib = load_cards()
    if lib and hasattr(lib, "alert_weights"):
        data = yaml.safe_load(st.map.read_text(encoding="utf-8"))
        check("Р-20 после снятия паузы вес зелёного ключа 0", {},
              lib.alert_weights(str(st.journal), data))


def section_parallel(tmp: Path) -> None:
    print("── два параллельных зова одного ключа → 1 карточка (Р-4), 20 пар ──")
    st = Stand(tmp / "pairs", lock_wait="10")
    codes = []
    for n in range(20):
        key = f"pair{n}:x"
        one = st.popen("--key", key, "--title", f"Пара {n}", "--text", "гонка")
        two = st.popen("--key", key, "--title", f"Пара {n}", "--text", "гонка")
        for p in (one, two):
            try:
                p.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate()
            codes.append(p.returncode)
    counts = [len(st.by_source(f"pair{n}:x")) for n in range(20)]
    check("Р-4 в каждой из 20 пар ровно одна карточка", [1] * 20, counts)
    check("Р-4 все 40 зовов — код 0", [0] * 40, codes)
    check("Р-4 в журнале на каждую пару «заведена» одна и «повтор» один", True,
          all(sorted(st.kinds(f"pair{n}:x")) == ["заведена", "повтор"] for n in range(20)))


# ── section: weights (lib/cards.alert_weights, key_state, work-queue) ──────

def load_cards():
    path = CARDS_DIR / "cards.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("cards_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(CARDS_DIR))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(CARDS_DIR))
    return module


def journal_line(ts: str, key: str, event: str, card_id: str = "") -> str:
    return json.dumps({"ts": ts, "ключ": key, "событие": event, "id": card_id,
                       "сторож": key.split(":")[0], "почему": ""}, ensure_ascii=False)


def map_with(*cards: tuple[str, str, str]) -> dict:
    return {"lines": [{"id": "harness", "tasks": [
        {"id": i, "status": s, "источник": src, "title": "t"} for i, s, src in cards]}]}


def section_weights(tmp: Path) -> None:
    print("── вес тревоги и состояние ключа (Р-3, Р-6) ──")
    lib = load_cards()
    for name in ("key_state", "alert_weights", "open_card_by_source", "write_cards_locked"):
        check(f"lib/cards.py несёт {name}", True, bool(lib) and hasattr(lib, name))
    if not lib or not hasattr(lib, "key_state") or not hasattr(lib, "alert_weights"):
        return
    t1, t2, t3 = ("2026-10-26T06:00:00+05:00", "2026-10-26T06:10:00+05:00",
                  "2026-10-26T06:20:00+05:00")
    seq = {
        ("заведена", "повтор"): "открыта",
        ("заведена", "зелёное"): "зелёное",
        ("заведена", "зелёное", "повтор"): "открыта",
        ("донесена",): "открыта",
        (): "нет",
    }
    for events, want in seq.items():
        lines = [journal_line(t1, "k:x", e, "hr.alert-k-x") for e in events]
        check(f"key_state {','.join(events) or 'пусто'} → {want}", want, lib.key_state(lines, "k:x"))
    lines = [journal_line(t1, "k:x", "заведена", "hr.alert-k-x"), "{битая строка",
             journal_line(t2, "k:x", "зелёное")]
    check("key_state: битая строка пропускается, не роняет", "зелёное", lib.key_state(lines, "k:x"))
    lines = [journal_line(t1, "k:x", "заведена", "hr.alert-k-x"),
             journal_line(t2, "k:x", "потолок"), journal_line(t3, "k:x", "отложено")]
    check("key_state: «потолок»/«отложено» состояние не меняют", "открыта",
          lib.key_state(lines, "k:x"))
    check("key_state: повтор без рождённой карточки → нет", "нет",
          lib.key_state([journal_line(t1, "k:x", "повтор")], "k:x"))

    tmp.mkdir(parents=True, exist_ok=True)

    def weights(lines: list[str], data: dict) -> dict:
        path = tmp / f"journal-{secrets.token_hex(4)}.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return lib.alert_weights(str(path), data)

    three = map_with(("hr.alert-a-x", "plan", "тревога:a:x"), ("hr.alert-b-x", "plan", "тревога:b:x"),
                     ("hr.alert-c-x", "wip", "тревога:c:x"))
    born = [journal_line(t2, "b:x", "заведена", "hr.alert-b-x"),
            journal_line(t1, "a:x", "заведена", "hr.alert-a-x"),
            journal_line(t3, "c:x", "донесена", "hr.alert-c-x")]
    check("вес получает ОДИН id — самый ранний по ts", {"hr.alert-a-x": "a:x"}, weights(born, three))
    check("старшая позеленела — вес у следующей", {"hr.alert-b-x": "b:x"},
          weights(born + [journal_line(t3, "a:x", "зелёное")], three))
    check("карточка тревога: руками без журнала → 0",
          {}, weights([], map_with(("hr.alert-m-x", "plan", "тревога:m:x"))))
    check("журнал без карточки → 0", {}, weights([born[1]], map_with()))
    check("источник карточки ≠ ключу журнала → 0", {},
          weights([born[1]], map_with(("hr.alert-a-x", "plan", "тревога:zzz:x"))))
    tie = [journal_line(t1, "b:x", "заведена", "hr.alert-b-x"),
           journal_line(t1, "a:x", "заведена", "hr.alert-a-x")]
    for parked in ("test", "беклог"):
        check(f"карточка старшей тревоги в {parked} — веса нет, вес у следующей",
              {"hr.alert-b-x": "b:x"},
              weights(born, map_with(("hr.alert-a-x", parked, "тревога:a:x"),
                                     ("hr.alert-b-x", "plan", "тревога:b:x"),
                                     ("hr.alert-c-x", "wip", "тревога:c:x"))))
    check("равенство ts — меньший id", {"hr.alert-a-x": "a:x"}, weights(tie, three))
    check("битая строка журнала вес не роняет", {"hr.alert-a-x": "a:x"},
          weights(["{битая", born[1]], three))

    queue_path = config_file("queue.yaml")
    queue_cfg = yaml.safe_load(queue_path.read_text(encoding="utf-8")) if queue_path else {}
    check("queue.yaml: вес тревога_сторожа = 35 (Р-6)", 35,
          ((queue_cfg or {}).get("веса") or {}).get("тревога_сторожа"))

    # work-queue on a stand: the oldest alert gets 35, a queued card stays second
    st = Stand(tmp / "queue")
    st.set_status("hr.sample-two", "plan")      # a wip card is a tier of its own
    st.run("--key", "aa:x", "--title", "Старшая беда", "--text", "старшая")
    time.sleep(1.1)
    for n in range(10):
        st.run("--key", f"w{n}:x", "--title", f"Беда {n}", "--text", "младшая")
    text = st.map.read_text(encoding="utf-8").replace("queue: []", "queue: [hr.sample-one]")
    st.map.write_text(text, encoding="utf-8")
    done = subprocess.run([sys.executable, str(HERE / "work-queue.py"), str(st.map), "--порядок"],
                          env=st.env(), capture_output=True, text=True)
    rows = re.findall(r"^\s+\d+\. (\S+) · (\d+) · (.*)$", done.stdout, re.M)
    order = [r[0] for r in rows]
    check("work-queue: голова — старшая тревога", "hr.alert-aa-x", order[0] if order else None)
    check("work-queue: старшая тревога весит 35", "35", rows[0][1] if rows else None)
    check("work-queue: причина называет старшую тревогу сторожа", True,
          bool(rows) and "тревог" in rows[0][2])
    check("work-queue: 11 тревог + 1 queue → queue на 2-м месте", "hr.sample-one",
          order[1] if len(order) > 1 else None)
    check("work-queue: младшие тревоги веса не получают", True,
          all(int(w) < 35 for i, w, _ in rows if i.startswith("hr.alert-w")))
    section_k5(tmp)


WORK_QUEUE = Path(os.environ.get("WORK_QUEUE_SCRIPT") or HERE / "work-queue.py")


def load_work_queue():
    spec = importlib.util.spec_from_file_location("work_queue_under_test", WORK_QUEUE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def section_k5(tmp: Path) -> None:
    """K-5 (review R1-04): the order of an alert against the owner's word, a
    failed check and started work. Weights come from the LIVE queue.yaml via
    the module's own loader: a mutation «alert weight 100» in the data must
    redden «owner's word above the alert»."""
    print("── К-5: тревога против слова владельца, провала и wip (R1-04) ──")
    wq = load_work_queue()
    settings = wq.настройки()
    alert = {"id": "t.alert", "status": "plan", "источник": "тревога:a:x"}

    def head(*others: dict, attention: dict | None = None) -> list:
        data = {"queue": [], "lines": [{"id": "h", "tasks": [alert, *others]}]}
        return [m["id"] for m in wq.порядок(data, направление="h", настройки=settings,
                                             внимание=attention or {}, тревоги={"t.alert": "a:x"})]

    check("К-5 внимание владельца выше тревоги", ["t.word", "t.alert"],
          head({"id": "t.word", "status": "plan", "источник": "inbox:111.1"},
               attention={"111.1": "срочно"}))
    check("К-5 провал проверки выше тревоги", ["t.fail", "t.alert"],
          head({"id": "t.fail", "status": "test", "провал_проверки": "не сошлось"}))
    check("К-5 wip выше тревоги", ["t.wip", "t.alert"], head({"id": "t.wip", "status": "wip"}))
    check("К-5 тревога выше plan без тревоги", ["t.alert", "t.plain"],
          head({"id": "t.plain", "status": "plan"}))

    done = subprocess.run([sys.executable, str(WORK_QUEUE), "--selftest"], capture_output=True,
                          text=True, env=clean_env(), timeout=120)
    alert_ok = [line for line in done.stdout.splitlines()
                if re.match(r"^\s+ок\s", line) and "тревог" in line]
    check("К-5 work-queue.py --selftest: код 0", 0, done.returncode)
    for word, pattern in (("внимание", r"внимани"), ("провал", r"провал"), ("wip", r"wip|начат")):
        check(f"К-5 work-queue.py --selftest несёт случай «{word} выше тревоги»", True,
              any(re.search(pattern, line) for line in alert_ok))


# ── section: tg_send.sh --task (K-4, Р-12, Р-16) ───────────────────────────

STUB_WRITER = r'''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
with open(os.environ["STAND_ORDER_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"who": "alert-task", "argv": args}, ensure_ascii=False) + "\n")
key = args[args.index("--key") + 1] if "--key" in args and args.index("--key") + 1 < len(args) else ""
if len(key) > 80 or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*:[a-z0-9:._-]*", key):
    sys.exit(2)
# argparse answers 2 to an option value that looks like an option (`--title --file`).
for flag in ("--title", "--text"):
    if flag in args and (args.index(flag) + 1 >= len(args) or args[args.index(flag) + 1].startswith("-")):
        sys.exit(2)
sys.exit(int(os.environ.get("STUB_ALERT_RC", "0")))
'''

STUB_CURL = r'''#!/usr/bin/env bash
python3 -c 'import json,sys; open(sys.argv[1],"a",encoding="utf-8").write(json.dumps({"who":"curl","argv":sys.argv[2:]},ensure_ascii=False)+"\n")' "$STAND_ORDER_LOG" "$@"
cat > /dev/null
printf '{"ok":true,"result":{"message_id":1}}'
'''

# hackathon.py — блок метки tg_send.sh импортирует его (зависимости — из lib/,
# она копируется целиком); без него метка падает и громко пишет в stderr.
DOOR_FILES = ("limit_mark.py", "limit-in-log.py", "stall-in-log.py", "pause.py",
              "check-channel-messages.py", "language-level.py", "hackathon.py")

PLAIN = "Ночные ворота: красных 2 — check-secrets, test_guard_bash."
LONG_FIRST = "Сторож сказал: " + "очень длинная первая строка вести " * 6
TOO_LONG = "Отчёт. " + "Слово за словом без конца. " * 90


class DoorStand(Stand):
    def __init__(self, base: Path, door: Path):
        super().__init__(base)
        scripts = self.project / "scripts"
        shutil.copytree(HERE / "lib", scripts / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        for name in DOOR_FILES:
            if (HERE / name).is_file():
                shutil.copy(HERE / name, scripts / name)
        shutil.copy(door, scripts / "tg_send.sh")
        (scripts / "alert-task.py").write_text(STUB_WRITER, encoding="utf-8")
        (base / "bin").mkdir()
        (base / "bin" / "curl").write_text(STUB_CURL, encoding="utf-8")
        (base / "bin" / "curl").chmod(0o755)
        self.order = self.log / "order.jsonl"
        self.attachment = base / "отчёт.txt"
        self.attachment.write_text("вложение пробы\n", encoding="utf-8")

    def send(self, *args: str, writer_rc: int = 0) -> dict:
        self.order.unlink(missing_ok=True)
        env = self.env(PATH=f"{self.base / 'bin'}:{os.environ.get('PATH', '')}",
                       STAND_ORDER_LOG=str(self.order), STUB_ALERT_RC=str(writer_rc))
        done = subprocess.run(["bash", str(self.project / "scripts" / "tg_send.sh"), *args],
                              env=env, capture_output=True, text=True, timeout=60)
        rows = [json.loads(x) for x in self.order.read_text(encoding="utf-8").splitlines()] \
            if self.order.exists() else []
        def norm(text: str) -> str:   # the stand path and the reset hour vary per run
            text = text.replace(str(self.base), "<STAND>")
            return re.sub(r"обновится в \d\d:\d\d", "обновится в ЧЧ:ММ", text)
        return {"rc": done.returncode, "out": norm(done.stdout), "err": norm(done.stderr),
                "curl": [[norm(a) for a in r["argv"]] for r in rows if r["who"] == "curl"],
                "writer": [r["argv"] for r in rows if r["who"] == "alert-task"],
                "order": [r["who"] for r in rows]}

    def limit(self, on: bool) -> None:
        mark = self.log / "лимит-подписки.состояние"
        mark.unlink(missing_ok=True)
        if on:
            subprocess.run([sys.executable, str(self.project / "scripts" / "limit_mark.py"),
                            "--поставить", "limit", "--сброс", str(int(time.time()) + 7200)],
                           env=self.env(), capture_output=True)


def door_outcomes(stand: DoorStand) -> dict:
    """The four exits of a call without --task, as the owner's callers see them."""
    stand.limit(False)
    result = {"доставка": stand.send(PLAIN),
              "отказ правил": stand.send(TOO_LONG),
              "файл": stand.send("--file", str(stand.attachment), "--caption",
                                 "Отчёт пробы двери о вложении одной строкой")}
    stand.limit(True)
    result["придержано 0"] = stand.send(PLAIN)
    result["придержано 3"] = stand.send("--не-досылать", PLAIN)
    stand.limit(False)
    return {k: {f: v[f] for f in ("rc", "out", "err", "curl")} for k, v in result.items()}


# Outcomes of the door BEFORE this task, captured 26.10.2026 by
# `--capture-reference .tasks/hr.watchers-report-must-create-model-task/tg_send.before.sh`
# (the copy of scripts/tg_send.sh at 696337b4). A call without --task must
# keep every byte of them (spec §4 p.1).
REFERENCE: dict = {
    "доставка": {
        "rc": 0,
        "out": "tg_send: доставлено (частей: 1, message_id подтверждён)\n",
        "err": "",
        "curl": [
            [
                "-sS",
                "--max-time",
                "30",
                "-K",
                "-",
                "-X",
                "POST",
                "--data-urlencode",
                "chat_id=100500",
                "--data-urlencode",
                "text=Ночные ворота: красных 2 — check-secrets, test_guard_bash."
            ]
        ]
    },
    "отказ правил": {
        "rc": 1,
        "out": "",
        "err": "[переписка] 2437 знаков при потолке 1800: это простыня. Оставь вывод и числа, подробности — в журнал или вложением\ntg_send: ОТКАЗ — сообщение нарушает правила переписки (владелец 11.09.2026).\n  Перепиши: итог и числа в канал, разбор своих действий — в память и журнал.\n  Документ длиннее потолка отправляй вложением ВМЕСТЕ с подписью:\n  tg_send.sh --file <путь> --caption «о чём документ одной строкой»\n",
        "curl": []
    },
    "файл": {
        "rc": 0,
        "out": "tg_send: файл доставлен (message_id подтверждён)\n",
        "err": "",
        "curl": [
            [
                "-sS",
                "--max-time",
                "120",
                "-K",
                "-",
                "-X",
                "POST",
                "-F",
                "chat_id=100500",
                "-F",
                "document=@<STAND>/отчёт.txt",
                "-F",
                "caption=Отчёт пробы двери о вложении одной строкой"
            ]
        ]
    },
    "придержано 0": {
        "rc": 0,
        "out": "",
        "err": "tg_send: ПРИДЕРЖАНО — держится лимит подписки (обновится в ЧЧ:ММ); уйдёт досылом после обновления.\n",
        "curl": []
    },
    "придержано 3": {
        "rc": 3,
        "out": "",
        "err": "tg_send: ПРИДЕРЖАНО — держится лимит подписки (обновится в ЧЧ:ММ); досыла не будет (так позвали).\n",
        "curl": []
    }
}


def split_eq(argv: list) -> list:
    """argparse reads «--title=X» and «--title X» alike; the probe judges the value, not the spelling."""
    out = []
    for a in argv:
        if a.startswith("--") and "=" in a:
            out.extend(a.split("=", 1))
        else:
            out.append(a)
    return out


def section_tgsend(tmp: Path) -> None:
    print("── дверь tg_send.sh: ключ --task ──")
    st = DoorStand(tmp / "door", DOOR)
    st.limit(False)
    now = door_outcomes(st)
    for name, want in REFERENCE.items():
        check(f"без ключа побайтно как до правки: {name}", want, now.get(name))
    check("эталон двери снят (иначе побайтная проба ни о чём)", True, len(REFERENCE) >= 5)

    plain = st.send(PLAIN)
    tagged = st.send("--task", "nightly-gates:red", PLAIN)
    check("--task K: код доставки прежний", plain["rc"], tagged["rc"])
    check("--task K: в curl побайтно тот же текст (ключ не съел текст)", plain["curl"], tagged["curl"])
    check("--task K: писатель позван один раз", 1, len(tagged["writer"]))
    check("--task K: писатель позван ДО curl", ["alert-task", "curl"], tagged["order"])
    argv = split_eq((tagged["writer"] or [[]])[0])
    check("--task K: писателю ушли --key K --title <первая строка> --text <текст>",
          ["--key", "nightly-gates:red", "--title", PLAIN, "--text", PLAIN], argv)
    check("--task K: stdout двери прежний", plain["out"], tagged["out"])
    syn = st.send("--задача", "nightly-gates:red", PLAIN)
    check("--задача — синоним --task (писатель позван так же)", (True, tagged["writer"]),
          (bool(syn["writer"]), syn["writer"]))

    long_one = st.send("--task", "x:y", LONG_FIRST + "\nвторая строка")
    argv = split_eq((long_one["writer"] or [[]])[0])
    title = argv[argv.index("--title") + 1] if "--title" in argv else None
    check("заголовок — первая строка, обрезанная до 110", LONG_FIRST[:110], title)

    for rc in (1, 5, 6):
        got = st.send("--task", "nightly-gates:red", PLAIN, writer_rc=rc)
        check(f"писатель вернул {rc}: текст в curl побайтно прежний", plain["curl"], got["curl"])
        check(f"писатель вернул {rc}: код двери — код доставки", plain["rc"], got["rc"])
        check(f"писатель вернул {rc}: в stderr «задача НЕ заведена ({rc})»", True,
              f"задача НЕ заведена ({rc})" in got["err"])
    got = st.send("--task", "nightly-gates:red", PLAIN, writer_rc=4)
    check("писатель вернул 4 (отложено): дверь молчит об отказе", (plain["rc"], False),
          (got["rc"], "НЕ заведена" in got["err"]))

    st.limit(True)
    held = st.send("--task", "nightly-gates:red", PLAIN)
    check("лимит: писатель позван, весть придержана, код 0", (1, [], 0),
          (len(held["writer"]), held["curl"], held["rc"]))
    held3 = st.send("--не-досылать", "--task", "nightly-gates:red", PLAIN)
    check("лимит + --не-досылать: писатель позван, код 3", (1, [], 3),
          (len(held3["writer"]), held3["curl"], held3["rc"]))
    st.limit(False)

    last = st.send(PLAIN, "--task")
    check("--task последним: exit 1, curl не позван", (1, []), (last["rc"], last["curl"]))
    for bad in ("nightly", "a b"):
        got = st.send("--task", bad, PLAIN)
        check(f"--task {bad!r}: ключ судит писатель (позван с --key)", True,
              any("--key" in a and bad in a for a in got["writer"]))
        check(f"--task {bad!r}: exit 1, curl не позван", (1, []), (got["rc"], got["curl"]))

    caption = "Гигиена сервера: нарушений 3\nподробности во вложении"
    filed = st.send("--file", str(st.attachment), "--caption", caption, "--task",
                    "server-hygiene:violations")
    argv = split_eq((filed["writer"] or [[]])[0])
    check("--file … --caption C --task K: заголовок из первой строки C", True,
          "--title" in argv and argv[argv.index("--title") + 1] == caption.splitlines()[0])
    check("--file … --caption C --task K: текст писателю — C", True,
          "--text" in argv and argv[argv.index("--text") + 1] == caption)
    check("--file … --task K: вложение ушло (curl позван один раз)", 1, len(filed["curl"]))

    # R1-06: without --caption the door took «--file» for the title, the writer
    # answered 2 to argparse, and the door refused «key not by the template».
    bare = st.send("--file", str(st.attachment), "--task", "server-hygiene:violations")
    argv = split_eq((bare["writer"] or [[]])[0])
    title = argv[argv.index("--title") + 1] if "--title" in argv[:-1] else None
    check("R1-06 --file X --task K без --caption: заголовок писателю не «--file»", False,
          title is not None and title.startswith("--"))
    check("R1-06 --file X --task K без --caption: причина «не по шаблону» не названа", False,
          "не по шаблону" in bare["err"])

    # Файла писателя нет (раскладка неполна): python3 отвечает 2, и дверь не
    # должна читать это как «кривой ключ» и отказывать в вести владельцу.
    writer_file = st.project / "scripts" / "alert-task.py"
    saved = writer_file.read_text(encoding="utf-8")
    writer_file.unlink()
    try:
        gone = st.send("--task", "nightly-gates:red", PLAIN)
    finally:
        writer_file.write_text(saved, encoding="utf-8")
    check("писателя нет файлом: весть уходит (curl побайтно прежний)", plain["curl"], gone["curl"])
    check("писателя нет файлом: код двери — код доставки", plain["rc"], gone["rc"])
    check("писателя нет файлом: «не по шаблону» не названо, «задача НЕ заведена» названо",
          (False, True), ("не по шаблону" in gone["err"], "писателя задач нет" in gone["err"]))

    # R2-05: a first line starting with «-» must reach the writer as a value,
    # not as an option argparse rejects with 2 (and the door then refuses the owner).
    dash_text = "-5% места на диске стенда за сутки."
    plain_dash = st.send(dash_text)
    dashed = st.send("--task", "disk-cleanup:full", dash_text)
    check("R2-05 заголовок с «-»: весть ушла (curl как без ключа)", plain_dash["curl"], dashed["curl"])
    check("R2-05 заголовок с «-»: код двери — код доставки", plain_dash["rc"], dashed["rc"])
    check("R2-05 заголовок с «-»: «не по шаблону» не названо, задача заведена", (False, False),
          ("не по шаблону" in dashed["err"], "НЕ заведена" in dashed["err"]))


# ── section: patterns.py (Р-11, К-7) ───────────────────────────────────────

def section_patterns(tmp: Path) -> None:
    print("── форма «тревога:» в patterns.py ──")
    sys.path.insert(0, str(HERE / "lib"))
    import patterns  # noqa: E402
    source = getattr(patterns, "ИСТОЧНИК_ТРЕВОГИ", None)
    keyform = getattr(patterns, "КЛЮЧ_ТРЕВОГИ", None)
    check("patterns.ИСТОЧНИК_ТРЕВОГИ есть", True, source is not None)
    check("patterns.КЛЮЧ_ТРЕВОГИ есть", True, keyform is not None)
    if source is None or keyform is None:
        return
    found = source.search("источник: тревога:nightly-gates:red")
    check("ИСТОЧНИК_ТРЕВОГИ достаёт ключ", "nightly-gates:red", found and found.group(1))
    for good in ("nightly-gates:red", "heartbeat-watch:dead:backup", "a:" + "b" * 78):
        check(f"КЛЮЧ_ТРЕВОГИ принимает {good[:24]!r}", True, bool(keyform.fullmatch(good)))
    for bad in ("a b", "A:x", "a;rm", "a$b", "a|b", "a&b", "", "-a:x", "a:" + "b" * 79):
        check(f"КЛЮЧ_ТРЕВОГИ отвергает {bad[:24]!r}", False, bool(keyform.fullmatch(bad)))
    guarded = patterns.ОХРАНЯЕМЫЕ_ЛИТЕРАЛЫ
    check("форма тревоги в ОХРАНЯЕМЫЕ_ЛИТЕРАЛЫ", True, source.pattern in guarded.values())
    check("К-7 форм не меньше 15", True, len(guarded) >= 15)
    judge = HERE / "check-one-judge.py"
    if judge.is_file():
        spec = importlib.util.spec_from_file_location("one_judge", judge)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        stand = tmp / "judge"
        (stand / "scripts" / "lib").mkdir(parents=True)
        (stand / "scripts" / "own-parser.py").write_text(
            f"import re\nMINE = re.compile(r{source.pattern!r})\n", encoding="utf-8")
        named = [(f, n) for f, n, _ in module.копии(stand, guarded)]
        check("копия формы тревоги в чужом файле краснит гейт одного судьи", True,
              any(f == "scripts/own-parser.py" for f, _ in named))
        live = subprocess.run([sys.executable, str(judge)], capture_output=True, text=True,
                              env=clean_env())
        check("живое дерево: у формы тревоги один судья (код 0)", 0, live.returncode)


# ── section: check-owner-senders.py by keys (§5, Р-9, К-8) ─────────────────

SENDER_A = ('#!/usr/bin/env bash\n'
            'bash "$PROJECT_DIR/scripts/tg_send.sh" --task demo-a:red "ворота красные"\n'
            'python3 "$PROJECT_DIR/scripts/alert-task.py" --resolve demo-a:red\n')
SENDER_B = ('#!/usr/bin/env bash\n'
            'for N in one two; do\n'
            '  python3 "$PROJECT_DIR/scripts/alert-task.py" --key "demo-b:dead:$N" --title t --text t\n'
            '  python3 "$PROJECT_DIR/scripts/alert-task.py" --resolve "demo-b:dead:$N"\n'
            'done\n'
            'bash "$PROJECT_DIR/scripts/tg_send.sh" "сводка"\n')
SENDER_OWNER = '#!/usr/bin/env bash\nbash "$PROJECT_DIR/scripts/tg_send.sh" "о самом лимите"\n'
SENDER_KICK = '#!/usr/bin/env bash\nbash "$PROJECT_DIR/scripts/tg_send.sh" "агент разбужен"\n'
SENDER_BOARD = '#!/usr/bin/env bash\nbash "$PROJECT_DIR/scripts/tg_send.sh" "на доске"\n'
SENDER_DEBT = '#!/usr/bin/env bash\nbash "$PROJECT_DIR/scripts/tg_send.sh" "агента нет"\n'
SENDER_DEBT2 = '#!/usr/bin/env bash\nbash "$PROJECT_DIR/scripts/tg_send.sh" "диск полон"\n'
NOT_SENDER = '#!/usr/bin/env bash\n# зовёт tg_send.sh только в стенде\nbash stand/tg_send.sh x\n'


def senders_data() -> dict:
    live = config_file("owner-senders.yaml")
    data = yaml.safe_load(live.read_text(encoding="utf-8")) if live else {}
    data = dict(data or {})
    data["pryamoj_api"] = {"scripts/tg_send.sh": "сама дверь"}
    data["nesmotrya_na_limit"] = {"scripts/tg_send.sh": "сама дверь"}
    data["model_task"] = {
        "harness/demons/demo-a.sh": [{"kind": "card", "key": "demo-a:red", "why": "работа модели"}],
        "harness/demons/demo-b.sh": [
            {"kind": "card", "key": "demo-b:dead:*", "why": "мёртвый поимённо"},
            {"kind": "owner-only", "why": "сводка для сведения"}],
        "harness/demons/demo-owner.sh": [{"kind": "owner-only", "why": "о самом лимите"}],
        "harness/demons/demo-kick.sh": [{"kind": "kick", "why": "будит живую смену"}],
        "harness/demons/demo-board.sh": [{"kind": "board", "why": "доска"}],
        "harness/demons/demo-debt.sh": [{"kind": "debt", "key": "demo-debt:no-agent",
                                         "card": "hr.watchers-report-must-create-model-task",
                                         "why": "волна 2"}],
        "scripts/tg_send.sh": [{"kind": "owner-only", "why": "сама дверь"}],
    }
    data["not_senders"] = {"scripts/verify-demo.sh": "зовёт дверь в стенде"}
    data["debt_max"] = 1
    return data


class SendersStand:
    FILES = {"harness/demons/demo-a.sh": SENDER_A, "harness/demons/demo-b.sh": SENDER_B,
             "harness/demons/demo-owner.sh": SENDER_OWNER, "harness/demons/demo-kick.sh": SENDER_KICK,
             "harness/demons/demo-board.sh": SENDER_BOARD, "harness/demons/demo-debt.sh": SENDER_DEBT,
             "scripts/verify-demo.sh": NOT_SENDER}

    def __init__(self, base: Path):
        self.base = base
        (base / "scripts").mkdir(parents=True)
        (base / "harness" / "demons").mkdir(parents=True)
        (base / "harness" / "config").mkdir(parents=True)
        shutil.copytree(HERE / "lib", base / "scripts" / "lib",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(SENDERS_GATE, base / "scripts" / "check-owner-senders.py")
        door = (f'#!/usr/bin/env bash\n# дверь tg_send.sh стенда\ncurl "{BOT_API}"\n'
                f'case "$1" in {SPEAK_IN_LIMIT}) shift ;; esac\n')
        self.write("scripts/tg_send.sh", door)
        for name, text in self.FILES.items():
            self.write(name, text)
        self.data = senders_data()
        self.save()
        self.git("init", "-q")
        self.git("config", "user.email", "p@p")
        self.git("config", "user.name", "proba")
        self.track()
        self.git("commit", "-qm", "стенд")

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.base), *args], check=True, capture_output=True)

    def track(self) -> None:
        names = list(self.FILES) + ["scripts/tg_send.sh", "harness/config/owner-senders.yaml"]
        names += [n for n in getattr(self, "extra", [])]
        self.git("add", "-f", "--", *[n for n in names if (self.base / n).exists()])

    def write(self, name: str, text: str) -> None:
        (self.base / name).parent.mkdir(parents=True, exist_ok=True)
        (self.base / name).write_text(text, encoding="utf-8")

    def save(self) -> None:
        (self.base / "harness" / "config" / "owner-senders.yaml").write_text(
            yaml.safe_dump(self.data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def judge(self) -> tuple[int, str]:
        self.save()
        self.track()
        done = subprocess.run([sys.executable, str(self.base / "scripts" / "check-owner-senders.py")],
                              capture_output=True, text=True, env=clean_env(), timeout=60)
        return done.returncode, done.stdout + done.stderr


def section_senders(tmp: Path) -> None:
    print("── ворота отправителей по ключам (§5) ──")
    check("гейт отправителей есть", True, SENDERS_GATE.is_file())

    def fresh(name: str) -> SendersStand:
        return SendersStand(tmp / f"senders-{name}")

    st = fresh("ok")
    rc, out = st.judge()
    check("здоровый стенд — код 0", 0, rc)
    check("правило 7: печать числа судимых образцов вслух", True, bool(re.search(
        r"ОТПРАВИТЕЛИ: файлов 8, записей [89], card 2, kick 1, board 1, owner-only 3, "
        r"долг 1 \(потолок 1\)", out)))

    st = fresh("count")
    st.extra = ["harness/demons/demo-c.sh"]
    st.write("harness/demons/demo-c.sh", SENDER_OWNER)
    st.data["model_task"]["harness/demons/demo-c.sh"] = [{"kind": "owner-only", "why": "ещё один"}]
    rc, out = st.judge()
    check("правило 7: девятый отправитель — «файлов 9» (счёт — по файлам, а не по записям)",
          (0, True), (rc, "ОТПРАВИТЕЛИ: файлов 9," in out))

    st = fresh("nsgone")
    st.data["not_senders"]["scripts/gone-stand.sh"] = "файла нет"
    rc, out = st.judge()
    check("not_senders на файл, которого нет в гите, — красный и назван", (1, True),
          (rc, "gone-stand.sh" in out))

    st = fresh("both")
    st.data["not_senders"]["harness/demons/demo-owner.sh"] = "и там, и тут"
    rc, out = st.judge()
    check("файл и в model_task, и в not_senders — красный и назван", (1, True),
          (rc, "demo-owner.sh" in out))

    st = fresh("kind")
    st.data["model_task"]["harness/demons/demo-owner.sh"] = [{"kind": "notify", "why": "вид вне списка"}]
    rc, out = st.judge()
    # The gate's own word, not a traceback: a KeyError on the unknown kind is
    # red too and names it.
    check("вид записи вне списка — красный и назван", (1, True),
          (rc, "notify" in out and "неизвестен" in out and "Traceback" not in out))

    st = fresh("ownwriter")
    st.data["model_task"]["harness/demons/demo-owner.sh"] = [{"kind": "card", "why": "без ключа"}]
    rc, out = st.judge()
    check("card без key и без own_writer — красный", (1, True), (rc, "own_writer" in out))

    st = fresh("new")
    st.extra = ["harness/demons/new-demon.sh"]
    st.write("harness/demons/new-demon.sh", SENDER_OWNER)
    rc, out = st.judge()
    check("правило 1: новый файл с зовом двери без записи — красный и назван", (1, True),
          (rc, "new-demon.sh" in out))

    st = fresh("probe")
    st.extra = ["scripts/test_demo.sh"]
    st.write("scripts/test_demo.sh", SENDER_OWNER)
    rc, out = st.judge()
    check("правило 1: проба test_* отсекается маской — код 0", 0, rc)

    st = fresh("gone")
    st.data["model_task"]["harness/demons/gone.sh"] = [{"kind": "owner-only", "why": "файла нет"}]
    rc, out = st.judge()
    check("правило 2: запись без файла — красный и назван", (1, True), (rc, "gone.sh" in out))

    st = fresh("silent")
    st.extra = ["harness/demons/silent.sh"]
    st.write("harness/demons/silent.sh", "#!/usr/bin/env bash\necho тихо\n")
    st.data["model_task"]["harness/demons/silent.sh"] = [{"kind": "owner-only", "why": "молчит"}]
    rc, out = st.judge()
    check("правило 2: запись на файл без зова двери — красный и назван", (1, True),
          (rc, "silent.sh" in out))

    st = fresh("nokey")
    st.write("harness/demons/demo-a.sh",
             SENDER_A.replace("--task demo-a:red ", ""))
    rc, out = st.judge()
    # A card key needs a RAISE (--task/--задача/--key) as well as --resolve:
    # a watcher that only resolves never opens the card.
    check("правило 3: card без зова с ключом (есть только --resolve) — красный", (1, True),
          (rc, "demo-a" in out))

    st = fresh("noresolve")
    st.write("harness/demons/demo-a.sh", SENDER_A.splitlines(True)[0] + SENDER_A.splitlines(True)[1])
    rc, out = st.judge()
    check("правило 3: card без --resolve своего ключа — красный (Р-7)", (1, True),
          (rc, "demo-a" in out))

    st = fresh("prefix")
    st.write("harness/demons/demo-b.sh", SENDER_B.replace('--resolve "demo-b:dead:$N"', "--resolve x:y"))
    rc, out = st.judge()
    check("правило 3: ключ-префикс «*» без --resolve префикса — красный", 1, rc)

    st = fresh("reverse")
    st.write("harness/demons/demo-a.sh", SENDER_A +
             'bash "$PROJECT_DIR/scripts/tg_send.sh" --task demo-a:other "ещё"\n')
    rc, out = st.judge()
    check("правило 4: литерал ключа в файле, которого нет в данных, — красный и назван",
          (1, True), (rc, "demo-a:other" in out))

    st = fresh("debt")
    st.data["model_task"]["harness/demons/demo-debt2.sh"] = [
        {"kind": "debt", "key": "demo-debt2:full", "card": "hr.watchers-report-must-create-model-task",
         "why": "волна 1"}]
    st.extra = ["harness/demons/demo-debt2.sh"]
    st.write("harness/demons/demo-debt2.sh", SENDER_DEBT2)
    rc, out = st.judge()
    check("правило 5: долг 2 при потолке 1 — красный", 1, rc)

    st = fresh("ratchet")
    st.data["model_task"]["harness/demons/demo-debt2.sh"] = [
        {"kind": "debt", "key": "demo-debt2:full", "card": "hr.watchers-report-must-create-model-task",
         "why": "волна 1"}]
    st.data["debt_max"] = 2
    st.extra = ["harness/demons/demo-debt2.sh"]
    st.write("harness/demons/demo-debt2.sh", SENDER_DEBT2)
    rc, out = st.judge()
    check("храповик: поднять debt_max выше закоммиченного — красный (долг только убывает)", 1, rc)

    st = fresh("lower")
    st.data["model_task"]["harness/demons/demo-debt.sh"] = [
        {"kind": "owner-only", "why": "долг закрыт другой правдой"}]
    st.data["debt_max"] = 0
    rc, out = st.judge()
    check("храповик: опустить debt_max вместе с долгом — код 0", 0, rc)

    st = fresh("nowhy")
    st.data["model_task"]["harness/demons/demo-owner.sh"] = [{"kind": "owner-only"}]
    rc, out = st.judge()
    check("правило 6: owner-only без why — красный", (1, True), (rc, "demo-owner" in out))

    st = fresh("debtcard")
    st.data["model_task"]["harness/demons/demo-debt.sh"] = [
        {"kind": "debt", "key": "demo-debt:no-agent", "why": "без карточки долга"}]
    rc, out = st.judge()
    check("debt без id карточки долга — красный", 1, rc)

    if SENDERS_GATE == HERE / "check-owner-senders.py" and (ROOT / ".git").exists():
        listed = subprocess.run(["git", "-C", str(ROOT), "-c", "core.quotepath=false", "ls-files", "scripts", "harness"],
                                capture_output=True, text=True).stdout.split("\n")
        code = [n for n in listed if re.search(r"\.(py|sh)$", n)]
        senders = [n for n in code
                   if "tg_send" in (ROOT / n).read_text(encoding="utf-8", errors="replace")
                   and not re.search(r"(^|/)test_|selftest", n)] if code else []
        live = subprocess.run([sys.executable, str(SENDERS_GATE)], capture_output=True, text=True,
                              env=clean_env(), timeout=120)
        check("К-8 живое дерево: код 0", 0, live.returncode)
        check(f"К-8 живое дерево: «ОТПРАВИТЕЛИ: файлов {len(senders)}»", True,
              f"ОТПРАВИТЕЛИ: файлов {len(senders)}," in live.stdout + live.stderr)


# ── section: storm (K-1: 200 calls of one key) ─────────────────────────────

def section_storm(tmp: Path) -> None:
    print("── шторм: 200 зовов одного ключа ──")
    st = Stand(tmp / "storm")
    codes = [st.raise_alert("storm:x", "Шторм сторожа")[0]]
    after_first = sha1(st.map)
    for _ in range(199):
        codes.append(st.raise_alert("storm:x", "Шторм сторожа")[0])
    check("200 зовов: все код 0", [0] * 200, codes)
    check("200 зовов: карточек 1", 1, len(st.by_source("storm:x")))
    check("200 зовов: карта не менялась после первого", after_first, sha1(st.map))
    kinds = st.kinds("storm:x")
    check("200 зовов: строк журнала по ключу 200", 200, len(kinds))
    check("200 зовов: «заведена» 1, «повтор» 199", (1, 199),
          (kinds.count("заведена"), kinds.count("повтор")))
    section_storm_journal(tmp)


STORM_ROWS, STORM_KEYS, STORM_FRESH = 50_000, 300, 50
STORM_COMPACT_MAX = 5_000
QUEUE_ORDER_MAX_KEY = "предел_порядка_сек"


def storm_journal(path: Path) -> None:
    """50 000 rows, 300 keys, as five watchers give in five weeks. By
    construction: s0 born first and green an hour ago; s1..s199 open;
    s200..s299 green long ago; the bulk is repeats older than two days; s2
    has STORM_FRESH repeats within the hour (they must survive verbatim)."""
    now = datetime.datetime.now().astimezone()

    def old(minutes: float) -> str:
        return (now - datetime.timedelta(days=3) + datetime.timedelta(minutes=minutes)).isoformat()

    rows = [journal_line(old(n), f"s{n}:x", "заведена", f"hr.alert-s{n}-x") for n in range(STORM_KEYS)]
    rows += [journal_line(old(STORM_KEYS + n), f"s{n}:x", "зелёное") for n in range(200, STORM_KEYS)]
    fresh = [journal_line((now - datetime.timedelta(minutes=30 - n * 0.1)).isoformat(), "s2:x", "повтор",
                          "hr.alert-s2-x") for n in range(STORM_FRESH)]
    fresh.append(journal_line((now - datetime.timedelta(hours=1)).isoformat(), "s0:x", "зелёное"))
    bulk = STORM_ROWS - len(rows) - len(fresh)
    rows += [journal_line(old(STORM_KEYS * 2 + n // 300), f"s{n % 200}:x", "повтор",
                          f"hr.alert-s{n % 200}-x") for n in range(bulk)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows + fresh) + "\n", encoding="utf-8")


def storm_stand(base: Path) -> Stand:
    st = Stand(base)
    st.set_status("hr.sample-two", "plan")
    for n in range(5):
        st.add_card(manual_card(f"hr.alert-s{n}-x", "plan", f"тревога:s{n}:x"))
    storm_journal(st.journal)
    return st


def section_storm_journal(tmp: Path) -> None:
    """R1-03: the alert journal is never trimmed and the weight re-reads it per
    key. The threshold is DATA (queue.yaml); the probe also judges that the
    fast answer is the RIGHT one: s0 is green, so the weight belongs to s1."""
    print(f"── шторм журнала: {STORM_ROWS} строк, {STORM_KEYS} ключей (R1-03) ──")
    queue_path = config_file("queue.yaml")
    queue_cfg = (yaml.safe_load(queue_path.read_text(encoding="utf-8")) or {}) if queue_path else {}
    limit = queue_cfg.get(QUEUE_ORDER_MAX_KEY)
    check(f"queue.yaml несёт порог «{QUEUE_ORDER_MAX_KEY}» числом", True,
          isinstance(limit, (int, float)) and not isinstance(limit, bool))
    limit = float(limit) if isinstance(limit, (int, float)) else 1.0

    st = storm_stand(tmp / "storm-order")
    spent, rows, timed_out = [], [], False
    for _ in range(3):
        started = time.monotonic()
        try:
            done = subprocess.run([sys.executable, str(WORK_QUEUE), str(st.map), "--порядок"],
                                  env=st.env(), capture_output=True, text=True,
                                  timeout=max(15.0, limit * 10))
        except subprocess.TimeoutExpired:
            timed_out = True
            break
        spent.append(time.monotonic() - started)
        rows = re.findall(r"^\s+\d+\. (\S+) · (\d+) · (.*)$", done.stdout, re.M)
        if spent[-1] <= limit:
            break
    best = min(spent) if spent else float("inf")
    check(f"R1-03 work-queue --порядок на шторме ≤ {limit:g} с (лучший из ≤3; вышло "
          f"{'предел истёк' if timed_out else f'{best:.2f} с'})", True, best <= limit)
    if rows:
        check("R1-03 на шторме вес у s1 (s0 позеленел): голова hr.alert-s1-x · 35",
              ("hr.alert-s1-x", "35"), rows[0][:2])

    print("── сжатие журнала тревог (R1-03) ──")
    st = storm_stand(tmp / "storm-compact")
    lib = load_cards()
    born_repeats = (lib.fold_journal(lib.parse_journal(st.journal.read_text(encoding="utf-8")
                                                       .splitlines())).get("s1:x") or {}
                    ).get("repeats") if lib else None
    for args in (("--drain",), ("--key", "s1:x", "--title", "Повтор шторма", "--text", "t")):
        rc, _, err = st.run(*args, timeout=300)
        check(f"сжатие: {args[0]} на журнале шторма — код 0", 0, rc)
    lines = st.journal.read_text(encoding="utf-8").splitlines() if st.journal.exists() else []
    check(f"R1-03 журнал сжат: строк ≤ {STORM_COMPACT_MAX} (было {STORM_ROWS}, стало {len(lines)})",
          True, len(lines) <= STORM_COMPACT_MAX)
    if len(lines) > STORM_COMPACT_MAX or lib is None:
        check("R1-03 состояния ключей после сжатия (не судимо: журнал не сжат)", "судимо", "не судимо")
        return
    want = {f"s{n}:x": ("зелёное" if n == 0 or n >= 200 else "открыта") for n in range(STORM_KEYS)}
    got = {key: lib.key_state(lines, key) for key in want}
    check("R1-03 сжатие сохранило состояние каждого из 300 ключей", {},
          {k: (want[k], got[k]) for k in want if want[k] != got[k]})
    fresh = [r for r in st.events("s2:x") if r.get("событие") == "повтор"
             and cards_epoch(r.get("ts")) > time.time() - 2 * 3600]
    check(f"R1-03 свежие повторы (моложе суток) не сжимаются: s2 — {STORM_FRESH}", STORM_FRESH,
          len(fresh))
    # Merged repeats keep their count in «число»: --open must still say how many
    # times the watcher saw the trouble (+1 for the --key call above).
    rc, out, _ = st.run("--open")
    shown = re.search(r"^s1:x\t\S+\tповторов (\d+)\t", out, re.M)
    check("R1-03 --open после сжатия: число повторов s1 сохранено",
          (born_repeats or 0) + 1, int(shown.group(1)) if shown else None)
    data = yaml.safe_load(st.map.read_text(encoding="utf-8"))
    check("R1-03 вес после сжатия — у s1", {"hr.alert-s1-x": "s1:x"},
          lib.alert_weights(str(st.journal), data))


def cards_epoch(stamp) -> float:
    try:
        return datetime.datetime.fromisoformat(str(stamp)).timestamp()
    except ValueError:
        return 0.0


# ── run ─────────────────────────────────────────────────────────────────────

SECTIONS = {"door": section_door, "weights": section_weights, "tgsend": section_tgsend,
            "patterns": section_patterns, "senders": section_senders, "storm": section_storm}
# Parts of the sections above, callable alone: a mutation run judges one case
# group in seconds instead of the whole door section (70 s).
PARTS = {"session": section_session, "empty": section_empty_line,
         "drainceil": section_drain_ceiling, "badq": section_bad_queue_row,
         "k5": section_k5, "stormj": section_storm_journal}


def capture_reference(door: Path) -> int:
    with tempfile.TemporaryDirectory(prefix="alert-ref-") as tmp:
        stand = DoorStand(Path(tmp) / "ref", door)
        print(json.dumps(door_outcomes(stand), ensure_ascii=False, indent=1))
    return 0


def main(argv: list[str]) -> int:
    if "--capture-reference" in argv:
        return capture_reference(Path(argv[argv.index("--capture-reference") + 1]))
    chosen = list(SECTIONS)
    if "--section" in argv:
        chosen = argv[argv.index("--section") + 1].split(",")
    live_before = sha1(LIVE_MAP)
    with tempfile.TemporaryDirectory(prefix="alert-task-probe-") as tmp:
        for name in chosen:
            try:
                {**SECTIONS, **PARTS}[name](Path(tmp) / name)
            except Exception as error:  # a crash of a section is a failure, not a silence
                check(f"раздел {name} дошёл до конца", "дошёл", f"упал: {type(error).__name__}: {error}")
            check(f"раздел {name}: боевая карта не тронута", live_before, sha1(LIVE_MAP))
    print(f"alert-task --selftest: случаев {cases.paths}, падений {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
