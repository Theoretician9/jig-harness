#!/usr/bin/env python3
"""Probes of `weed-the-map.py --дубли` — read-only report of duplicate cards
(task hr.weed-duplicate-stale-map-items, step 1: probe before code).

Where it comes from: the map review of 03.11 (`.tasks/hr.weed-duplicate-stale-
map-items/разбор.md`) found duplicates by hand — one owner message split into N
cards shares the `источник: inbox:<id>`, titles are close, and raw summary
similarity is noise because the automaton writes the same template into every
card («…Карточка заведена КОДОМ… Разобрать замысел…»). After merging, the map
marks a duplicate with `дубль_из: <main>` and the main card with `слито_из`.

The weeder resolves its map from its OWN location (КОРЕНЬ = parent of
scripts/), so every case runs a COPY of it inside a stand tree, called as the
production gate calls it: `python3 scripts/weed-the-map.py --дубли` from the
root. The live map is compared by sha1 before and after.

Expected output (contract for step 2):
    [дубли карты] просмотрено карточек: N          N = non-беклог cards
    [дубли карты] пара: <id1> <id2> — <довод>      one line per pair
    [дубли карты] ОШИБКА: <id> дубль_из <id2> — такой карточки нет
    [дубли карты] дублей нет                         when no pair
Exit code: 0 for a report (pairs or none), 1 if any ОШИБКА line.

Door for mutation runs ([[probe-loads-the-live-mechanism]]):
    HARNESS_WEED_MAP   path of weed-the-map.py under test
Last line: `weed-the-map --дубли: случаев N, падений F`.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

WEEDER = Path(os.environ.get("HARNESS_WEED_MAP") or HERE / "weed-the-map.py")
LIVE_MAP = HERE.parent / "dev-map.yaml"

# Шаблон автомата дословно как в живой карте (map-from-message.py): без него
# случай «только общий шаблон» ничего не доказывает.
TEMPLATE = ("Карточка заведена КОДОМ (scripts/map-from-message.py) в момент приёма "
            "сообщения — не вниманием сессии. Разбор модели: задач 1, указаний 0, "
            "вопросов 0. Разобрать замысел и уточнить объём — при взятии задачи в работу.")


def card(id_: str, status: str, source: str, title: str, quote: str, extra: str = "") -> str:
    summary = f"Владелец 2026-11-03 в канал: «{quote}». {TEMPLATE}"
    return (f"      - id: {id_}\n"
            f"        status: {status}\n"
            f"{extra}"
            f"        owner_testable: true\n"
            f"        источник: {source}\n"
            f"        завёл: scripts/map-from-message.py\n"
            f"        title: \"{title}\"\n"
            f"        summary: \"{summary}\"\n"
            f"        when: {{ started: null, done: null, tested: null, estimated: true }}\n")


def dev_map(*cards: str) -> str:
    body = "".join(cards) if cards else ""
    tasks = "    tasks:\n" + body if cards else "    tasks: []\n"
    return ("version: 1\nqueue: []\nlines:\n"
            "  - id: harness\n    layer: infra\n    name: \"Харнес\"\n" + tasks)


# (а) одно сообщение расщеплено на две карточки: общий inbox, близкий title
PAIR_A = card("hr.hack-fast", "plan", "inbox:1793600000.111111111",
              "быстрый режим хакатона без мутаций",
              "сделай быстрый режим хакатона без мутаций и без ночи")
PAIR_B = card("hr.hack-mode", "plan", "inbox:1793600000.111111111",
              "режим хакатона быстрый без мутаций проб",
              "сделай быстрый режим хакатона без мутаций и без ночи")
# (б) разные сообщения: общее у них только шаблон автомата
MAIL = card("hr.mail-setup", "plan", "inbox:1793600000.222222222",
            "настроить почту на ящик проекта", "настрой почту на ящик проекта")
DISK = card("hr.disk-watch", "test", "inbox:1793600000.333333333",
            "починить сторожа уборки диска", "сторож уборки диска опять молчит")
LONELY = ("      - id: hr.lonely\n        status: wip\n        источник: тревога:server-hygiene:x\n"
          "        title: \"Гигиена сервера: есть нарушения.\"\n"
          "        summary: \"Сторож server-hygiene 03.11 03:30: есть нарушения.\"\n")
# (г) беклог: общий inbox и тот же title — в отчёт не идёт вовсе
OLD_A = card("hr.old-a", "беклог", "inbox:1793600000.444444444",
             "привести сообщения о лимите в порядок", "приведи сообщения о лимите в порядок")
OLD_B = card("hr.old-b", "беклог", "inbox:1793600000.444444444",
             "привести сообщения о лимите в порядок", "приведи сообщения о лимите в порядок")
# (в) уже слитая пара: дубль помечен, основная знает о нём
MARKED = card("hr.dup-marked", "plan", "inbox:1793600000.555555555",
              "показывать квитанцию принял всегда", "квитанция принял чтобы всегда была видна",
              extra="        дубль_из: hr.main-marked\n")
MAIN = card("hr.main-marked", "plan", "inbox:1793600000.555555555",
            "квитанция принял всегда видна", "квитанция принял чтобы всегда была видна",
            extra="        слито_из: [hr.dup-marked]\n")
ORPHAN = card("hr.orphan", "plan", "inbox:1793600000.666666666",
              "убрать лишний сторож", "убери лишний сторож",
              extra="        дубль_из: hr.no-such\n")

FULL = (PAIR_A, PAIR_B, MAIL, DISK, LONELY, OLD_A, OLD_B, MARKED, MAIN)
BACKLOG_IDS = {"hr.old-a", "hr.old-b"}

COUNT = re.compile(r"просмотрено карточек:\s*(\d+)")
PAIR = re.compile(r"пара:\s*(\S+)\s+(\S+)\s+—\s*(.*\S)")
ERROR = re.compile(r"ОШИБКА:\s*(\S+)\s+дубль_из\s+(\S+)")

cases = Cases()
check = cases.check


def sha1(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return "нет файла"


def tree(root: Path) -> dict:
    """{путь: sha1} всего стенда, кроме кэша — «ничего не пишет» судится целиком."""
    return {str(p.relative_to(root)): sha1(p) for p in sorted(root.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts}


class Stand:
    def __init__(self, base: Path, text: str):
        self.root = base
        (base / "scripts").mkdir(parents=True)
        (base / "docs").mkdir()
        shutil.copytree(HERE / "lib", base / "scripts" / "lib",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(WEEDER, base / "scripts" / "weed-the-map.py")
        self.map = base / "dev-map.yaml"
        self.map.write_text(text, encoding="utf-8")

    def run(self) -> tuple[int, str]:
        env = dict(os.environ)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PROJECT_DIR"] = str(self.root)
        try:
            done = subprocess.run([sys.executable, "scripts/weed-the-map.py", "--дубли"],
                                  capture_output=True, text=True, env=env,
                                  timeout=60, cwd=str(self.root))
        except subprocess.TimeoutExpired:
            return -9, "предел 60 с истёк"
        return done.returncode, done.stdout + done.stderr


def count(out: str):
    found = COUNT.findall(out)
    return int(found[-1]) if found else None


def pairs(out: str) -> dict:
    return {frozenset(m.group(1, 2)): m.group(3) for m in PAIR.finditer(out)}


def mentioned(out: str) -> set:
    return {i for p in pairs(out) for i in p}


def main() -> int:
    live_before = sha1(LIVE_MAP)
    with tempfile.TemporaryDirectory(prefix="weed-dup-probe-") as tmp:
        print("── полная карта: пара, шаблон, беклог, слитое ──")
        st = Stand(Path(tmp) / "full", dev_map(*FULL))
        before = tree(st.root)
        rc, out = st.run()
        found = pairs(out)
        check("(ж) режим отвечает кодом 0 и без трассировки", (0, False), (rc, "Traceback" in out))
        check("(ж) число просмотренных карточек — 7 (9 минус 2 беклога)", 7, count(out))
        check("(а) список пар не пуст (разобран из вывода)", True, bool(found))
        check("(а) общий inbox + близкий title — пара hr.hack-fast ↔ hr.hack-mode", True,
              frozenset({"hr.hack-fast", "hr.hack-mode"}) in found)
        reason = found.get(frozenset({"hr.hack-fast", "hr.hack-mode"}), "")
        check("(ж) довод пары называет общий источник inbox:…111111111", True,
              "inbox:1793600000.111111111" in reason)
        check("(б) общий только шаблон — hr.mail-setup, hr.disk-watch, hr.lonely ни в одной паре",
              set(), mentioned(out) & {"hr.mail-setup", "hr.disk-watch", "hr.lonely"})
        check("(в) уже слитая пара (дубль_из/слито_из) не предлагается повторно", set(),
              mentioned(out) & {"hr.dup-marked", "hr.main-marked"})
        check("(г) беклог не попадает в отчёт ни id", set(),
              {i for i in BACKLOG_IDS if i in out})
        check("(а-г) пар ровно одна", 1, len(found))
        check("(в) без битых ссылок строки ОШИБКА нет", [], ERROR.findall(out))
        check("(е) при найденной паре строки «дублей нет» нет", False, "дублей нет" in out)
        check("(д) режим ничего не пишет: стенд (карта, docs) байт в байт", before, tree(st.root))

        print("── битая ссылка дубль_из ──")
        st = Stand(Path(tmp) / "orphan", dev_map(MAIL, DISK, LONELY, ORPHAN))
        before = sha1(st.map)
        rc, out = st.run()
        check("(в) дубль_из на несуществующий id — строка ОШИБКА с обоими id",
              [("hr.orphan", "hr.no-such")], ERROR.findall(out))
        check("(в) битая ссылка — код 1, без трассировки", (1, False), (rc, "Traceback" in out))
        check("(в) битая ссылка — отдельная ошибка, не пара", set(), mentioned(out))
        check("(ж) число просмотренных и при ошибке — 4", 4, count(out))
        check("(д) карта не тронута и при ошибке", before, sha1(st.map))

        print("── ни одной пары ──")
        st = Stand(Path(tmp) / "none", dev_map(MAIL, DISK, LONELY))
        before = tree(st.root)
        rc, out = st.run()
        check("(е) ни одной пары — «дублей нет», код 0", (0, True), (rc, "дублей нет" in out))
        check("(е) ни одной пары — число просмотренных 3", 3, count(out))
        check("(е) ни одной пары — строк «пара:» нет", {}, pairs(out))
        check("(д) стенд не тронут", before, tree(st.root))

        print("── пустая карта ──")
        st = Stand(Path(tmp) / "empty", dev_map())
        before = tree(st.root)
        rc, out = st.run()
        check("(е) пустая карта — «дублей нет», код 0, не молчание", (0, True),
              (rc, "дублей нет" in out))
        check("(е) пустая карта — число просмотренных 0 названо явно", 0, count(out))
        check("(д) стенд не тронут", before, tree(st.root))
    check("боевая карта не тронута", live_before, sha1(LIVE_MAP))
    print(f"weed-the-map --дубли: случаев {cases.paths}, падений {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
