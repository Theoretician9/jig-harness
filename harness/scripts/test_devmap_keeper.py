#!/usr/bin/env python3
"""Проба ведения карты: умолчания по схеме, факт when из git и процесс-хозяин.

Задача «схема карты», шаги 2–3 (связь — трейлер Dev-Map коммита); контракт —
docs/specs/2026-11-20-devmap-keeper.md. Написана ДО кода: до него красная
по причине «нет функции / нет процесса», а не по сбою стенда.

Стенд свой: временный каталог, своя git-репа (свой GIT_CONFIG_GLOBAL, хуки
выключены), карта из четырёх карточек и семь коммитов с датами 01–10.11.2026
в UTC. Свой лок, свой LOG_DIR и HEARTBEAT_DIR. Живая карта сверяется по sha1
до и после; живые конфиги библиотеки и реестра только читаются.

Двери (умолчания — файлы рядом с пробой):
    HARNESS_CARDS_LIB             каталог с cards.py под пробой
    DEVMAP_KEEPER                 scripts/devmap-keeper.py
    ARCHIVE_MAP                   scripts/archive-map.py
    TASK_EFFICIENCY_SCRIPT        scripts/task-efficiency.py
    DEVMAP_SCHEMA                 harness/config/devmap-schema.yaml
    HARNESS_PROCESS_LIBRARY       harness/config/process-library.yaml
    HARNESS_PROCESS_LIBRARY_JSON  harness/config/process-library.json
    HARNESS_SYSTEM_JOBS           harness/config/system-jobs.yaml
    HARNESS_MECHANISMS            harness/config/mechanisms.yaml
    CHECK_REGISTRY                scripts/check-registry.py

Прогон (только фоном):
    python3 scripts/test_devmap_keeper.py
Последняя строка: `ВЕДЕНИЕ КАРТЫ: случаев N, неудач F`.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import yaml

os.environ["TZ"] = "UTC"
time.tzset()

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG = ROOT / "harness" / "config"


def door(name: str, default: Path) -> Path:
    return Path(os.environ.get(name) or default)


CARDS_DIR = door("HARNESS_CARDS_LIB", HERE / "lib")
KEEPER = door("DEVMAP_KEEPER", HERE / "devmap-keeper.py")
ARCHIVE_MAP = door("ARCHIVE_MAP", HERE / "archive-map.py")
TASK_EFFICIENCY = door("TASK_EFFICIENCY_SCRIPT", HERE / "task-efficiency.py")
LIVE_SCHEMA = door("DEVMAP_SCHEMA", CONFIG / "devmap-schema.yaml")
LIBRARY_YAML = door("HARNESS_PROCESS_LIBRARY", CONFIG / "process-library.yaml")
LIBRARY_JSON = door("HARNESS_PROCESS_LIBRARY_JSON", CONFIG / "process-library.json")
SYSTEM_JOBS = door("HARNESS_SYSTEM_JOBS", CONFIG / "system-jobs.yaml")
MECHANISMS = door("HARNESS_MECHANISMS", CONFIG / "mechanisms.yaml")
CHECK_REGISTRY = door("CHECK_REGISTRY", HERE / "check-registry.py")
LIVE_MAP = ROOT / "dev-map.yaml"

PROCESS_ID = "devmap-keeper"
KEEPER_FILE = "scripts/devmap-keeper.py"
TASK_FILES = (KEEPER_FILE, "scripts/lib/cards.py", "scripts/test_devmap_keeper.py",
              "scripts/archive-map.py", "scripts/task-efficiency.py")
LIBRARY_MINIMUM = ("id", "название", "класс", "запуск", "статус", "файлы", "доля_нагрузки")

CASES = 0
FAILS = 0


def case(name: str):
    """Случай: функция вернула (ждали, вышло); исключение — красный с причиной."""
    def wrap(fn):
        global CASES, FAILS
        CASES += 1
        try:
            expected, actual = fn()
        except Exception as error:  # noqa: BLE001 — причина красного печатается целиком
            FAILS += 1
            reason = f"{type(error).__name__}: {error}".splitlines()[0][:300]
            print(f"  ПЛОХО {name}: {reason}")
            return fn
        if expected == actual:
            print(f"  ок    {name}")
        else:
            FAILS += 1
            print(f"  ПЛОХО {name}: ждали «{expected}», вышло «{actual}»")
        return fn
    return wrap


def load_module(name: str, path: Path):
    if not path.is_file():
        raise FileNotFoundError(f"нет {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cards_lib():
    sys.path.insert(0, str(CARDS_DIR))
    try:
        return load_module("cards_under_probe", CARDS_DIR / "cards.py")
    finally:
        sys.path.remove(str(CARDS_DIR))


def need(module, name: str):
    fn = getattr(module, name, None)
    if fn is None:
        raise AttributeError(f"нет функции cards.{name} (контракт спеки §3.2)")
    return fn


def day(stamp: str) -> int:
    return int(time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M")))


# ── стенд: схема и карта ───────────────────────────────────────────────────

SCHEMA = """\
версия: 1
поля:
  id: { тип: строка }
  status: { тип: строка }
  title: { тип: строка }
  summary: { тип: строка }
  owner_testable: { тип: логическое, умолчание: true }
  blocked: { тип: [строка, пусто] }
  volume:
    тип: словарь
    умолчание: { loc: 100, complexity: 3, tokens_k: 50, estimated: true }
  when:
    тип: словарь
    умолчание: { started: null, done: null, tested: null, estimated: true }
  tested_at: { тип: [строка, пусто] }
  checklist: { тип: список }
"""
DEFAULTS = {
    "owner_testable": True,
    "volume": {"loc": 100, "complexity": 3, "tokens_k": 50, "estimated": True},
    "when": {"started": None, "done": None, "tested": None, "estimated": True},
}

HEAD = """\
version: 1
lines:
  - id: core
    label: "Ядро"
    tasks:
"""

CARD_A = """\
      - id: t.a
        status: {status}
        owner_testable: false
        title: "A"
        summary: "a"
        volume: {{ loc: 7, complexity: 1, tokens_k: 1, estimated: true }}
        when: {{ started: null, done: null, tested: null, estimated: true }}
"""
CARD_B = """\
      - id: t.b
        status: {status}
        title: "B"
        summary: "b"
"""
CARD_C = """\
      - id: t.c
        status: plan
        owner_testable: true
        title: "C"
        summary: "{summary}"
        volume: {{ loc: 1, complexity: 1, tokens_k: 1, estimated: true }}
        when:
          started: '2026-10-01'
          done: null
          tested: null
          estimated: true
"""
CARD_D = """\
      - id: t.d
        status: test
        owner_testable: true
        title: "D"
        summary: "d"
        when:
          started: null
          done: null
          tested: null
          estimated: true
        checklist:
          - label: "пункт"
            done: false
"""
TAIL = """\
  - id: other
    label: "Другая линия"
    tasks: []
"""


def map_text(a="plan", b="plan", c_summary="c", with_d=False, a_tested=False) -> str:
    card_a = CARD_A.format(status=a)
    if a_tested:
        card_a += '        tested_at: "2026-11-10"\n'
    return (HEAD + card_a + CARD_B.format(status=b) + CARD_C.format(summary=c_summary)
            + (CARD_D if with_d else "") + TAIL)


# Семь коммитов: (когда, текст карты). t.a: plan→wip→test→wip→test→done;
# t.b: plan→wip; t.c: только правка summary (шум, не переход); t.d рождена в test.
HISTORY = [
    ("2026-11-01 10:00", map_text()),
    ("2026-11-03 10:00", map_text(a="wip")),
    ("2026-11-05 10:00", map_text(a="test", b="wip", with_d=True)),
    ("2026-11-06 10:00", map_text(a="wip", b="wip", with_d=True)),
    ("2026-11-07 10:00", map_text(a="wip", b="wip", c_summary="c2", with_d=True)),
    ("2026-11-08 10:00", map_text(a="test", b="wip", c_summary="c2", with_d=True)),
    ("2026-11-10 10:00", map_text(a="done", b="wip", c_summary="c2", with_d=True, a_tested=True)),
]

FACT_A = {"started": "2026-11-03", "done": "2026-11-08", "tested": "2026-11-10"}
FACT_B = {"started": "2026-11-05", "done": None, "tested": None}
FACT_D = {"started": "2026-11-05", "done": "2026-11-05", "tested": None}
TRANSITIONS_A = [("2026-11-01", None, "plan"), ("2026-11-03", "plan", "wip"),
                 ("2026-11-05", "wip", "test"), ("2026-11-06", "test", "wip"),
                 ("2026-11-08", "wip", "test"), ("2026-11-10", "test", "done")]


class Stand:
    def __init__(self, base: Path):
        self.base = base
        self.repo = base / "repo"
        self.logs = base / "logs"
        self.beats = base / "beats"
        self.lock = base / "locks" / "dev-map.lock"
        self.schema = base / "devmap-schema.yaml"
        for folder in (self.repo, self.logs, self.beats, self.lock.parent, base / "hooks"):
            folder.mkdir(parents=True, exist_ok=True)
        self.schema.write_text(SCHEMA, encoding="utf-8")
        gitconfig = base / "gitconfig"
        gitconfig.write_text(f"[user]\n\tname = probe\n\temail = probe@stand\n"
                             f"[core]\n\thooksPath = {base / 'hooks'}\n"
                             f"[init]\n\tdefaultBranch = master\n", encoding="utf-8")
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
                    "TZ": "UTC", "LOG_DIR": str(self.logs), "HARNESS_LOG_DIR": str(self.logs),
                    "HEARTBEAT_DIR": str(self.beats),
                    "HARNESS_EVENTS_STATE": str(self.logs / ".events")}
        for key in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "HARNESS_COMMIT_LOCK_HELD"):
            self.env.pop(key, None)
        self.git("init", "-q")
        for when, text in HISTORY:
            self.map.write_text(text, encoding="utf-8")
            self.git("add", "dev-map.yaml")
            stamp = when.replace(" ", "T") + ":00+0000"
            self.git("commit", "-q", "-m", f"карта {when}",
                     extra={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})

    @property
    def map(self) -> Path:
        return self.repo / "dev-map.yaml"

    def git(self, *args: str, extra: dict | None = None, cwd: Path | None = None) -> str:
        done = subprocess.run(["git", "-C", str(cwd or self.repo), *args], capture_output=True,
                              text=True, env={**self.env, **(extra or {})})
        if done.returncode:
            raise RuntimeError(f"стенд: git {' '.join(args)}: {done.stderr.strip()}")
        return done.stdout.strip()

    def keeper(self, *args: str, map_path: Path | None = None) -> tuple[int, dict | None, str]:
        if not KEEPER.is_file():
            raise FileNotFoundError(f"нет {KEEPER} (процесс ведения карты, спека §3.3)")
        done = subprocess.run(
            [sys.executable, str(KEEPER), "--map", str(map_path or self.map), "--schema", str(self.schema),
             "--lock", str(self.lock), "--json", *args],
            capture_output=True, text=True, env=self.env, cwd=str(self.base), timeout=120)
        lines = [ln for ln in done.stdout.splitlines() if ln.strip().startswith("{")]
        report = json.loads(lines[-1]) if lines else None
        return done.returncode, report, (done.stderr.strip().splitlines() or [""])[-1]


def parsed_tasks(text: str) -> dict:
    data = yaml.safe_load(text) or {}
    return {t["id"]: t for line in data.get("lines") or [] for t in line.get("tasks") or []}


def card_block(text: str, card_id: str) -> str:
    found = re.search(rf"^      - id: {re.escape(card_id)}\n(?:(?!      - id: |  - id: ).*\n)*", text, re.M)
    if not found:
        raise AssertionError(f"нет блока {card_id}")
    return found.group(0)


def short(when: dict) -> dict:
    return {k: when.get(k) for k in ("started", "done", "tested")}


def sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest() if path.exists() else "нет файла"


# ── случаи ─────────────────────────────────────────────────────────────────

def lib_cases(cards) -> None:
    schema = yaml.safe_load(SCHEMA)
    text = HISTORY[-1][1]

    @case("schema_defaults: ровно поля с умолчанием, значения из схемы")
    def _():
        return DEFAULTS, need(cards, "schema_defaults")(schema)

    @case("schema_defaults: живая схема несёт умолчания owner_testable, volume, when")
    def _():
        got = need(cards, "schema_defaults")(yaml.safe_load(LIVE_SCHEMA.read_text(encoding="utf-8")))
        return ((True, True, DEFAULTS["when"]),
                (got.get("owner_testable"), (got.get("volume") or {}).get("estimated"), got.get("when")))

    @case("fill_defaults: недостающие поля дописаны, стоящие строки целы и по порядку")
    def _():
        block = card_block(text, "t.b")
        out = need(cards, "fill_defaults")(block, DEFAULTS)
        card = yaml.safe_load("x:\n" + out)["x"][0]
        kept = [ln for ln in out.splitlines() if ln in block.splitlines()]
        return ((True, DEFAULTS["volume"], DEFAULTS["when"], block.splitlines()),
                (card.get("owner_testable"), card.get("volume"), card.get("when"), kept))

    @case("fill_defaults: стоящее не затирается — полная карточка байт в байт")
    def _():
        block = card_block(text, "t.a")
        return block, need(cards, "fill_defaults")(block, DEFAULTS)

    @case("fill_defaults: у карточки с checklist volume дописан, checklist цел")
    def _():
        block = card_block(text, "t.d")
        card = yaml.safe_load("x:\n" + need(cards, "fill_defaults")(block, DEFAULTS))["x"][0]
        return ((DEFAULTS["volume"], [{"label": "пункт", "done": False}]),
                (card.get("volume"), card.get("checklist")))

    @case("when_fact: первый вход в работу, последний в закрытое, последний в done")
    def _():
        transitions = [(day(d + " 10:00"), old, new) for d, old, new in TRANSITIONS_A]
        return FACT_A, need(cards, "when_fact")(transitions)

    @case("when_fact: рождена в test — started и done в день рождения")
    def _():
        return FACT_D, need(cards, "when_fact")([(day("2026-11-05 10:00"), None, "test")])

    @case("when_fact: только рождение в plan — фактов нет")
    def _():
        return ({"started": None, "done": None, "tested": None},
                need(cards, "when_fact")([(day("2026-11-01 10:00"), None, "plan")]))

    @case("set_when: без факта — блок байт в байт")
    def _():
        block = card_block(text, "t.c")
        return block, need(cards, "set_when")(block, {"started": None, "done": None, "tested": None})

    @case("set_when: блочный when → одна строка, estimated: false, прочее цело")
    def _():
        block = card_block(text, "t.d")
        out = need(cards, "set_when")(block, FACT_D)
        before = yaml.safe_load("x:\n" + block)["x"][0]
        after = yaml.safe_load("x:\n" + out)["x"][0]
        rest = lambda c: {k: v for k, v in c.items() if k != "when"}  # noqa: E731
        when_lines = len(re.findall(r"^\s+when:", out, re.M))
        # Подполя судятся только под строкой `when:` (отступ глубже её): у t.d есть
        # checklist с `done: false`, и счёт по всему блоку принимал его за подполе when.
        sub_lines = len(re.findall(r"^(\s+)when:[^\n]*\n((?:\1\s+\S[^\n]*\n)*)", out, re.M)[0][1]
                        .splitlines()) if when_lines else 0
        return ((dict(FACT_D, estimated=False), rest(before), 1, 0),
                (after.get("when"), rest(after), when_lines, sub_lines))


def history_cases(cards, stand: Stand) -> None:
    @case("status_transitions: переходы t.a по истории, правка summary — не переход")
    def _():
        got = need(cards, "status_transitions")(stand.repo, "dev-map.yaml")
        as_days = lambda rows: [(time.strftime("%Y-%m-%d", time.localtime(at)), o, n)  # noqa: E731
                                for at, o, n in rows]
        return ((TRANSITIONS_A, [("2026-11-01", None, "plan")], [("2026-11-05", None, "test")]),
                (as_days(got.get("t.a", [])), as_days(got.get("t.c", [])), as_days(got.get("t.d", []))))

    @case("status_transitions: перенос карточки между линиями — не переход")
    def _():
        # Своя репа, а не HISTORY: перенос меняет порядок блоков, и случаи CLI судили бы иное.
        # Строка status: переносимой карточки стоит в диффе с обеих сторон с тем же значением.
        moved = stand.base / "moved"
        moved.mkdir()
        stand.git("init", "-q", cwd=moved)
        # Линия «other» длиннее карточки: так git оставляет её контекстом и двигает карточку.
        card = CARD_B.format(status="wip")
        other = '  - id: other\n' + "".join(f'    note{i}: "{i}"\n' for i in range(8)) + "    tasks:{}\n"
        for when, text in (("2026-11-01 10:00", HEAD + card + other.format(" []")),
                           ("2026-11-02 10:00", HEAD.replace("    tasks:\n", "    tasks: []\n")
                            + other.format("") + card)):
            (moved / "dev-map.yaml").write_text(text, encoding="utf-8")
            stand.git("add", "dev-map.yaml", cwd=moved)
            stamp = when.replace(" ", "T") + ":00+0000"
            stand.git("commit", "-q", "-m", when, cwd=moved,
                      extra={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
        got = need(cards, "status_transitions")(moved, "dev-map.yaml")
        return [(None, "wip")], [(o, n) for _, o, n in got.get("t.b", [])]

    @case("task-efficiency на общем разборе: возвратов t.a 1, закрыта 08.11")
    def _():
        module = load_module("task_efficiency_under_probe", TASK_EFFICIENCY)
        returns, closed_at = module.status_history(stand.repo, "dev-map.yaml")
        return (1, day("2026-11-08 10:00")), (returns.get("t.a"), closed_at.get("t.a"))


def keeper_cases(stand: Stand) -> None:
    base_text = stand.map.read_text(encoding="utf-8")
    base_head = stand.git("rev-parse", "HEAD")

    @case("CLI сухой прогон: список изменённых, файл и HEAD не тронуты")
    def _():
        code, report, tail = stand.keeper()
        return ((0, ["t.a", "t.b", "t.d"], False, base_text, base_head),
                (code, (report or {}).get("изменено"), (report or {}).get("записано"),
                 stand.map.read_text(encoding="utf-8"), stand.git("rev-parse", "HEAD")))

    @case("CLI лок карты занят: код 0, ничего не записано")
    def _():
        with open(stand.lock, "a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            started = time.monotonic()
            code, report, tail = stand.keeper("--применить", "--lock-wait", "1")
            spent = time.monotonic() - started
        return ((0, False, base_text, base_head, True),
                (code, (report or {}).get("записано"), stand.map.read_text(encoding="utf-8"),
                 stand.git("rev-parse", "HEAD"), spent < 30))

    # Чужая незакоммиченная правка карты: на диске остаётся, в коммит не уходит.
    foreign = base_text.replace('summary: "c2"', 'summary: "c-чужая"')
    stand.map.write_text(foreign, encoding="utf-8")

    applied: dict = {}

    def after_apply() -> None:
        # Следующие случаи судят итог ОДНОГО прохода «применить»: без него
        # «карточка байт в байт» и «чужая правка на диске» зеленели бы сами.
        if applied.get("code") != 0 or not (applied.get("report") or {}).get("записано"):
            raise AssertionError(f"проход --применить не состоялся: {applied or 'не запускался'}")

    @case("CLI применить: факты t.a/t.b/t.d, estimated: false, стоящее не затёрто")
    def _():
        code, report, tail = stand.keeper("--применить")
        applied.update(code=code, report=report, tail=tail)
        tasks = parsed_tasks(stand.map.read_text(encoding="utf-8"))
        a, b, d = tasks["t.a"], tasks["t.b"], tasks["t.d"]
        return ((0, True, FACT_A, FACT_B, FACT_D, (False, False, False), False, 7,
                 True, DEFAULTS["volume"], DEFAULTS["volume"], [{"label": "пункт", "done": False}]),
                (code, (report or {}).get("записано"), short(a["when"]), short(b["when"]), short(d["when"]),
                 (a["when"].get("estimated"), b["when"].get("estimated"), d["when"].get("estimated")),
                 a.get("owner_testable"), a["volume"]["loc"], b.get("owner_testable"), b.get("volume"),
                 d.get("volume"), d.get("checklist")))

    @case("CLI применить: карточка без перехода байт в байт, линии и id целы")
    def _():
        after_apply()
        text = stand.map.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        return ((card_block(foreign, "t.c"), ["core", "other"], ["t.a", "t.b", "t.c", "t.d"]),
                (card_block(text, "t.c"), [ln["id"] for ln in data["lines"]],
                 [t["id"] for t in data["lines"][0]["tasks"]]))

    @case("CLI применить: один коммит из HEAD — чужая правка на диске, не в коммите")
    def _():
        after_apply()
        head_text = stand.git("show", "HEAD:dev-map.yaml")
        commits = stand.git("rev-list", "--count", f"{base_head}..HEAD")
        return (("1", True, False, FACT_A),
                (commits, "c-чужая" in stand.map.read_text(encoding="utf-8"), "c-чужая" in head_text,
                 short(parsed_tasks(head_text)["t.a"]["when"])))

    @case("CLI применить: метка жизни devmap-keeper поставлена")
    def _():
        after_apply()
        return True, (stand.beats / PROCESS_ID).exists()

    @case("CLI повтор: ничего не меняет — файл и HEAD те же")
    def _():
        after_apply()
        text, head = stand.map.read_text(encoding="utf-8"), stand.git("rev-parse", "HEAD")
        code, report, tail = stand.keeper("--применить")
        return ((0, [], False, text, head),
                (code, (report or {}).get("изменено"), (report or {}).get("записано"),
                 stand.map.read_text(encoding="utf-8"), stand.git("rev-parse", "HEAD")))

    @case("CLI в worktree помощника: отказ кодом 3, карта и ветка не тронуты")
    def _():
        tree = stand.base / "wt"
        stand.git("worktree", "add", "-q", "-b", "helper", str(tree), base_head)
        tree_map = tree / "dev-map.yaml"
        before, head = tree_map.read_text(encoding="utf-8"), stand.git("rev-parse", "HEAD", cwd=tree)
        code, report, tail = stand.keeper("--применить", map_path=tree_map)
        return ((3, before, head),
                (code, tree_map.read_text(encoding="utf-8"), stand.git("rev-parse", "HEAD", cwd=tree)))


ARCHIVE_CARD = """\
version: 1
lines:
  - id: core
    tasks:
      - id: t.closed
        status: plan
        closed_reason: "брошена: wip без движения"
        title: "X"
        summary: "x"
"""


def archive_cases(base: Path) -> None:
    @case("archive-map с_причиной: второй closed_reason не вписывается, старый первым")
    def _():
        module = load_module("archive_map_under_probe", ARCHIVE_MAP)
        block = card_block(ARCHIVE_CARD, "t.closed")
        out = module.с_причиной(block, "срок находки истёк", "2026-11-20")
        reasons = re.findall(r'^\s+closed_reason:\s*"(.*)"\s*$', out, re.M)
        dates = re.findall(r"^\s+снято_когда:", out, re.M)
        return ((["брошена: wip без движения; срок находки истёк"], 1),
                (reasons, len(dates)))

    @case("archive-map с_причиной: та же причина второй раз — без «x; x»")
    def _():
        module = load_module("archive_map_under_probe", ARCHIVE_MAP)
        block = card_block(ARCHIVE_CARD, "t.closed")
        out = module.с_причиной(block, "брошена: wip без движения", "2026-11-20")
        return (["брошена: wip без движения"],
                re.findall(r'^\s+closed_reason:\s*"(.*)"\s*$', out, re.M))

    @case("archive-map снять: в архиве у карточки один closed_reason")
    def _():
        module = load_module("archive_map_under_probe", ARCHIVE_MAP)
        folder = base / "archive"
        folder.mkdir(exist_ok=True)
        card_map, archive = folder / "dev-map.yaml", folder / "dev-map-archive.yaml"
        card_map.write_text(ARCHIVE_CARD, encoding="utf-8")
        result = module.снять(card_map, archive, ["t.closed"], "срок находки истёк")
        text = archive.read_text(encoding="utf-8")
        return ((1, 1), (result.get("снято"), len(re.findall(r"^\s+closed_reason:", text, re.M))))


def registry_cases() -> None:
    @case("библиотека процессов yaml: devmap-keeper по крону, команда с --применить, метка")
    def _():
        library = yaml.safe_load(LIBRARY_YAML.read_text(encoding="utf-8")) or {}
        record = (library.get("процессы") or {}).get(PROCESS_ID)
        if record is None:
            raise LookupError(f"в {LIBRARY_YAML.name} нет процесса {PROCESS_ID}")
        command = str(record.get("команда") or "")
        return ((("крон", True, True, True)),
                (record.get("запуск"), KEEPER_FILE in command, "--применить" in command,
                 PROCESS_ID in (library.get("метки") or {})))

    @case("библиотека процессов json: запись devmap-keeper — фоновый, крон, файл процесса")
    def _():
        records = (json.loads(LIBRARY_JSON.read_text(encoding="utf-8")) or {}).get("процессы") or []
        record = next((r for r in records if r.get("id") == PROCESS_ID), None)
        if record is None:
            raise LookupError(f"в {LIBRARY_JSON.name} нет записи {PROCESS_ID}")
        return (("фоновый", "крон", True, []),
                (record.get("класс"), record.get("запуск"), KEEPER_FILE in (record.get("файлы") or []),
                 [k for k in LIBRARY_MINIMUM if k not in record]))

    @case("system-jobs.yaml пересобран: задание devmap-keeper")
    def _():
        jobs = (yaml.safe_load(SYSTEM_JOBS.read_text(encoding="utf-8")) or {}).get("задания") or []
        job = next((j for j in jobs if j.get("имя") == PROCESS_ID), None)
        if job is None:
            raise LookupError(f"в {SYSTEM_JOBS.name} нет задания {PROCESS_ID}")
        return True, KEEPER_FILE in str(job.get("команда"))

    @case("реестр механизмов: scripts/devmap-keeper.py — демон, проверяется этой пробой")
    def _():
        rows = yaml.safe_load(MECHANISMS.read_text(encoding="utf-8")) or []
        rows = rows if isinstance(rows, list) else rows.get("механизмы") or []
        row = next((r for r in rows if r.get("файл") == KEEPER_FILE), None)
        if row is None:
            raise LookupError(f"в {MECHANISMS.name} нет механизма {KEEPER_FILE}")
        return ("демон", True), (row.get("вид"), "test_devmap_keeper.py" in str(row.get("проверяется")))

    @case("check-registry.py: нет претензий по файлам этой задачи, итог напечатан")
    def _():
        # Судим свои файлы, а не код 0: реестр общий, и чужие претензии (паспорта
        # UNIFIED/templates, проба соседней ветки) красили бы пробу за не её долг.
        # Итог обязателен — упавший сверщик не зелёный: «описано N, претензий M» при претензиях,
        # «устройство: описано механизмов N, расхождений нет» при чистом реестре.
        done = subprocess.run([sys.executable, str(CHECK_REGISTRY)], capture_output=True, text=True, timeout=300)
        claims = [s.strip()[2:] for s in done.stdout.splitlines() if s.strip().startswith("✗ ")]
        own = [c for c in claims if c.split(":", 1)[0] in TASK_FILES]
        return (True, []), (bool(re.search(r"^(описано \d+, претензий \d+|устройство: описано механизмов \d+, расхождений нет)", done.stdout, re.M)), own)


def main() -> int:
    live_before = sha1(LIVE_MAP)
    with tempfile.TemporaryDirectory(prefix="devmap-keeper-probe-") as tmp:
        base = Path(tmp)
        stand = Stand(base)
        try:
            cards = cards_lib()
        except Exception:  # noqa: BLE001 — без библиотеки красны все её случаи, стенд цел
            traceback.print_exc()
            cards = None
        print("— библиотека карточек (cards.py)")
        lib_cases(cards)
        print("— история статусов")
        history_cases(cards, stand)
        print("— процесс devmap-keeper.py на стенде")
        keeper_cases(stand)
        print("— archive-map: второй closed_reason")
        archive_cases(base)
    print("— регистрация процесса")
    registry_cases()

    @case("живая карта не тронута пробой")
    def _():
        return live_before, sha1(LIVE_MAP)

    print(f"ВЕДЕНИЕ КАРТЫ: случаев {CASES}, неудач {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
