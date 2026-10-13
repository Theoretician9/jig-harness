#!/usr/bin/env python3
"""Проверка снимка доски канбана (`build-board.py`, спека hr.kanban §3).

Откуда взято. Владелец 13.10.2026: «все задачи … на какой они стадии (весь
пайплайн) были на канбане и чтобы прям точно это соблюдалось … железобетонно».
Стадия нигде не записана — её при коммите выводит гейт `check-pipeline.py`.
Любая вторая реализация «стадии» для экрана разойдётся с ним молча, и владелец
закроет задачу по доске, а упрётся в красные ворота. Поэтому тест судит две
вещи: каждая строка таблицы §3.2 даёт свою колонку и пилюлю, и доска говорит
то же, что гейт (§7.1, приложение Б2).

Всё — на своих фикстурах во временном каталоге: своя карта, свои бумаги, свой
LOG_DIR, свой PANEL_STATE_DIR, свой harness.conf. Живая карта и боевые журналы
не читаются и не пишутся: `беды_одобрения` дописывает журнал одобрений, и тест
без своего LOG_DIR писал бы в боевой (контракт, Б12).

Судья (`judge`) — свежая копия модуля гейта, у которой подменено ровно то, что
зовёт git: модуль цены отдаёт признаки коммитов из таблицы теста. Остальное —
НАСТОЯЩИЕ функции гейта (`долг_задачи`, `шаги_для_задачи`, `корень_бумаг`,
`беды_в_двух_корнях`, `spec_approval.беды_одобрения`): доска и гейт обязаны
говорить одно, а сверка с переписанной копией гейта зеленела бы всегда.
Корень гейта (`КОРЕНЬ`) переставлен на каталог фикстуры — значит снимок обязан
брать модуль цены через `judge._модуль_цены()`, карточки — через
`judge._все_карточки()` и корень бумаг — через `judge.корень_бумаг()`.

Прогон:
    python3 scripts/test_build_board.py
Последняя строка: «ДОСКА: путей N, неудач M»; код 0 — все пути зелёные.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import traceback
import types
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="test-build-board-"))
ROOT = TMP / "repo"
LOGS = TMP / "log"
PANEL = TMP / "panel-state"
for directory in (ROOT, LOGS / "inbox" / "обработано", PANEL):
    directory.mkdir(parents=True, exist_ok=True)

# Окружение ставится ДО импорта гейта: журнал обходов и путь harness.conf
# вычисляются при импорте, и поздняя подмена увела бы их на боевые места.
HARNESS_CONF = TMP / "harness.conf"
HARNESS_CONF.write_text('PRODUCT_REPO=""\nAUTOMATION_LEVEL="partial"\nACTIVE_LINE=""\n',
                        encoding="utf-8")
INSTALL_CONF = TMP / "install.conf"
SECRETS = TMP / "secrets"
SECRETS.mkdir()
SECRET_VALUE = "deadbeefcafebabe0123456789abcdef01234567"  # не-секрет: образец больного случая
(SECRETS / "probe_key").write_text(SECRET_VALUE + "\n", encoding="utf-8")
INSTALL_CONF.write_text(f'LOG_DIR="{LOGS}"\nPANEL_STATE_DIR="{PANEL}"\n'
                        f'STATE_DIR="{TMP / "state"}"\nSECRETS_DIR="{SECRETS}"\n', encoding="utf-8")
os.environ.update({
    "HARNESS_CONF": str(HARNESS_CONF),
    "HARNESS_INSTALL_CONF": str(INSTALL_CONF),
    "LOG_DIR": str(LOGS),
    "HARNESS_LOG_DIR": str(LOGS),
    "PANEL_STATE_DIR": str(PANEL),
    "AUTOMATION_LEVEL": "partial",
    "HARNESS_ACTIVE_LINE": "",
})
os.environ.pop("PRODUCT_REPO", None)

paths = failures = 0


def path_ok(text: str) -> None:
    global paths
    paths += 1
    print("ок  · " + text)


def path_bad(text: str) -> None:
    global paths, failures
    paths += 1
    failures += 1
    print("ПЛОХО · " + text)


def check(condition: bool, good: str, bad: str) -> None:
    if condition:
        path_ok(good)
    else:
        path_bad(bad)


def finish() -> None:
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"ДОСКА: путей {paths}, неудач {failures}")
    sys.exit(1 if failures else 0)


def load(name: str, file: Path):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── фикстура: карта, бумаги, журналы ──────────────────────────────────────
import yaml  # noqa: E402

STARTED = {"started": "2026-10-10"}   # позже всех дат «шаги_включены»: судятся все шаги
DEPTH = {"уровень": "крупная", "характер": "разработка", "кем": "проба", "когда": "2026-10-10"}
LONG = "ключ " + "deadbeefcafebabe0123456789abcdef01234567" + " " + "длинный текст пробы " * 300  # не-секрет: образец
SOURCE_ID = "1790000000.000000001"


def card(task_id: str, status: str, **fields) -> dict:
    return {"id": task_id, "status": status, "title": f"проба {task_id}",
            "owner_testable": True, "when": dict(STARTED), **fields}


def wip(task_id: str, spec: str, **fields) -> dict:
    return card(task_id, "wip", глубина=dict(DEPTH), spec=spec, **fields)


HARNESS_TASKS = [
    card("t.plan", "plan", источник="ревизия:проба:scripts/x.py:строка как есть"),
    card("t.backlog", "беклог"),
    card("t.nodepth", "wip", spec="docs/specs/t-nodepth.md"),
    wip("t.small", "docs/specs/t-small.md"),
    wip("t.debt", "docs/specs/t-debt.md"),
    wip("t.debt2", "docs/specs/t-debt2.md"),
    wip("t.approval", "docs/specs/t-approval.md"),
    wip("t.approval2", "docs/specs/t-approval2.md"),
    wip("t.allpapers", "docs/specs/t-all.md", источник=f"inbox:{SOURCE_ID}"),
    wip("t.gitmute", "docs/specs/t-gitmute.md"),
    card("t.nospec", "wip", глубина=dict(DEPTH)),
    wip("t.named", "docs/specs/t-named.md", бумаги={"план": "docs/plans/общий-план.md"}),
    card("t.test1", "test", чем_подтверждено="селфтест 12 путей и живой прогон"),
    card("t.test2", "test"),
    card("t.accepted", "принято"),
    card("t.done", "done", tested_at="2026-10-12"),
    card("t.weird", "почти"),
    card("t.blockedplan", "plan", blocked="ключ от владельца"),
    wip("t.blockedwip", "docs/specs/t-blockedwip.md", blocked="чужая поставка"),
]
PRODUCT_TASKS = [wip("t.product", "docs/specs/t-product.md")]
MAP = {"version": 1, "lines": [
    {"id": "harness", "layer": "infra", "name": "Харнес", "tasks": HARNESS_TASKS},
    {"id": "product", "layer": "product", "name": "Продукт", "tasks": PRODUCT_TASKS},
]}
MAP_PATH = ROOT / "dev-map.yaml"
MAP_PATH.write_text(yaml.safe_dump(MAP, allow_unicode=True, sort_keys=False), encoding="utf-8")
ALL_IDS = [t["id"] for line in MAP["lines"] for t in line["tasks"]]

ARCHIVE = {"version": 1, "lines": [{"id": "harness", "tasks": [
    card("t.old1", "done", closed_reason="проба"), card("t.old2", "plan", closed_reason="проба")]}]}
(ROOT / "docs").mkdir(parents=True, exist_ok=True)
(ROOT / "docs" / "dev-map-archive.yaml").write_text(
    yaml.safe_dump(ARCHIVE, allow_unicode=True, sort_keys=False), encoding="utf-8")


def put(relative: str, text: str = "бумага пробы\n") -> None:
    target = ROOT / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def papers(task_id: str, spec_stem: str, steps: list) -> None:
    """Кладёт бумаги шагов по конвенции гейта (`_где_артефакт`)."""
    where = {
        "спека": f"docs/specs/{spec_stem}.md",
        "ревью_спеки": f"docs/specs/{spec_stem}-REVIEW.md",
        "ревью_кода": f"docs/specs/{spec_stem}-CODE-REVIEW.md",
        "контракт": f".tasks/{task_id}/pretask.yaml",
        "план": f".tasks/{task_id}/план.md",
        "мутации": f".tasks/{task_id}/мутации.log",
    }
    for step in steps:
        put(where[step])


SPEC_ONLY = ["спека", "ревью_спеки"]
ALL_PAPERS = ["спека", "ревью_спеки", "контракт", "план", "ревью_кода", "мутации"]
papers("t.small", "t-small", SPEC_ONLY)
papers("t.debt", "t-debt", SPEC_ONLY)
papers("t.debt2", "t-debt2", SPEC_ONLY + ["контракт"])
papers("t.approval", "t-approval", SPEC_ONLY)
papers("t.approval2", "t-approval2", ALL_PAPERS)
papers("t.allpapers", "t-all", ALL_PAPERS)
papers("t.gitmute", "t-gitmute", SPEC_ONLY)
papers("t.blockedwip", "t-blockedwip", SPEC_ONLY)
papers("t.named", "t-named", ["спека", "ревью_спеки", "контракт"])
put("docs/plans/общий-план.md")
put(".tasks/t.allpapers/журнал.md", LONG)
(LOGS / "inbox" / "обработано" / f"{SOURCE_ID}.txt").write_text(LONG, encoding="utf-8")

# Журнал одобрений спек: у одних задач слово владельца есть, у t.approval2
# спека отправлена, но слова нет, у t.approval не отправлена вовсе.
journal = []
for task_id in ("t.debt", "t.debt2", "t.allpapers", "t.blockedwip", "t.named"):
    journal.append({"ts": "2026-10-11T10:00:00", "событие": "отправлена", "задача": task_id})
    journal.append({"ts": "2026-10-11T11:00:00", "событие": "одобрена", "задача": task_id,
                    "сказано": "2026-10-11T11:00:00"})
journal.append({"ts": "2026-10-12T09:30:00", "событие": "отправлена", "задача": "t.approval2"})
(LOGS / "спеки-владельцу.jsonl").write_text(
    "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in journal), encoding="utf-8")
(LOGS / "skills.jsonl").write_text(json.dumps(
    {"ts": "2026-10-12T08:00:00Z", "skill": "plan-writing", "task": "t.allpapers",
     "откуда_задача": "карта", "источник": "хук", "успех": True}, ensure_ascii=False) + "\n",
    encoding="utf-8")

# Признаки коммитов по задаче — вместо git. None — «git молчит» (форма
# настоящего `признаки_задачи`), {"коммитов": 0} — работы не было.
BIG = {"коммитов": 3, "сторож_или_гейт": True, "код_продукта": True}
MID = {"коммитов": 1, "сторож_или_гейт": True}
FEATURES = {task_id: dict(BIG) for task_id in ALL_IDS}
FEATURES["t.small"] = dict(MID)
FEATURES["t.gitmute"] = None

_loaded = 0


def make_judge(root: Path, features: dict):
    """Свежая копия гейта: корень — фикстура, git заменён таблицей признаков."""
    global _loaded
    _loaded += 1
    gate = load(f"check_pipeline_probe_{_loaded}", HERE / "check-pipeline.py")
    real_price = getattr(gate, "_модуль_цены")()
    fake_price = types.SimpleNamespace(
        **{name: value for name, value in vars(real_price).items() if not name.startswith("__")})

    def fake_features(task_id, *args, **kwargs):
        return features.get(task_id, {"коммитов": 0})

    setattr(fake_price, "признаки_задачи", fake_features)
    setattr(gate, "_модуль_цены", lambda: fake_price)
    setattr(gate, "КОРЕНЬ", root)
    setattr(gate.spec_approval, "КОРЕНЬ", root)
    return gate, fake_price


def joined(pills) -> str:
    return " | ".join(str(p) for p in pills or [])


# ── загрузка снимка ──────────────────────────────────────────────────────
print("── загрузка ──")
try:
    board_module = load("build_board", HERE / "build-board.py")
    path_ok("build-board.py загружен")
except Exception as exc:  # noqa: BLE001 — красный прогон обязан назвать причину
    path_bad(f"build-board.py не загружен: {type(exc).__name__}: {exc}")
    finish()

judge, price = make_judge(ROOT, FEATURES)
settings = getattr(price, "конфиг")()
STEP_KEYS = getattr(price, "шаги_уровня")(settings, "крупная")
STEP_COLUMNS = [step.replace("_", " ") for step in STEP_KEYS]
COLUMNS = ["Очередь"] + STEP_COLUMNS + ["Проверка", "Готово"]
FIRST = STEP_COLUMNS[0]
cards = {c["id"]: c for c in getattr(judge, "_все_карточки")()}
check(set(cards) == set(ALL_IDS), "гейт видит все карточки фикстуры",
      f"гейт видит не те карточки: {sorted(set(cards) ^ set(ALL_IDS))}")
check(cards.get("t.product", {}).get("layer") == "product",
      "слой product спущен с линии на карточку (Б3)", "у карточки продукта нет слоя линии")


def price_pill(features: dict) -> str:
    value = getattr(price, "цена")(features, settings["веса"])
    level = getattr(price, "уровень")(value, settings["пороги"])
    return f"цена {value}, по коммитам {level}"


# ── строки таблицы §3.2 ──────────────────────────────────────────────────
# Каждая строка охраняет одно состояние задачи, которое владелец видит на
# доске. Ожидание — (колонка, подстроки, которые ОБЯЗАНЫ быть в пилюлях,
# подстроки, которых быть НЕ должно). Ошибка любой строки — владелец видит не
# ту стадию: «чисто» там, где судить нечем, или «готово» там, где долг.
ROWS = [
    # plan — задача ещё не взята: место в очереди, без пометок.
    ("t.plan", "Очередь", [], ["ждёт", "непонятный"], "plan → Очередь"),
    # Беклог трогает только владелец (28.09.2026) — доска говорит это прямо.
    ("t.backlog", "Очередь", ["не трогаю без слова"], [], "беклог → Очередь с «не трогаю без слова»"),
    # Взята без глубины: гейт шагов не назначил — шагов нет, жёлтая пометка.
    ("t.nodepth", FIRST, ["глубины нет — шаги не назначены"], [],
     "wip без глубины → первая колонка шагов"),
    # Цена по коммитам ниже «крупной»: гейт бумаг не требует, доска ставит
    # первый шаг без бумаги и называет цену и уровень — иначе «молчит» = «готово».
    ("t.small", "контракт", ["гейт шагов не требует", price_pill(MID)], ["бумаги собраны"],
     "wip ниже крупной → первый шаг без бумаги, пилюля с ценой"),
    # Крупная с долгом: колонка — первый шаг, по которому гейт называет долг
    # по ПОЛНОМУ списку. Без этого доска ставила бы «ревью кода» при пустом контракте.
    ("t.debt", "контракт", [], ["бумаги собраны"], "wip крупная, нет контракта → контракт"),
    # Шаг «тесты» гейт файлом не судит — карточку он не держит.
    ("t.debt2", "план", [], ["бумаги собраны"], "wip крупная, нет плана → план (тесты не держат)"),
    # Спека и ревью есть, уровень «частичный», спека не отправлена — ждём слова владельца.
    ("t.approval", "ревью спеки", ["ждёт твоего слова по спеке", "не отправлена"], ["бумаги собраны"],
     "ждёт одобрения, не отправлена → ревью спеки"),
    # Отправлена, слова нет — даже при всех бумагах: работа начинается после одобрения.
    ("t.approval2", "ревью спеки", ["ждёт твоего слова по спеке", "отправлена 2026-10-12"],
     ["бумаги собраны", "не отправлена"], "отправлена без одобрения при всех бумагах → ревью спеки"),
    # Все бумаги и одобрение: осталось закрыть в test.
    ("t.allpapers", "ревью кода", ["бумаги собраны — закрыть в test"], [],
     "все бумаги → ревью кода, «бумаги собраны»"),
    # Задача продукта при пустом PRODUCT_REPO: судить нечем — красным, и это
    # НЕ «история не прочитана» и не «чисто» (Б4: два смысла одной None-цены).
    ("t.product", FIRST, ["судить нечем: PRODUCT_REPO пуст"], ["бумаги собраны", "история не прочитана"],
     "layer product + пустой PRODUCT_REPO → «судить нечем»"),
    # git не ответил: «не прочитали» — не «нарушений нет».
    ("t.gitmute", FIRST, ["история не прочитана"], ["бумаги собраны", "судить нечем"],
     "git молчит → «история не прочитана»"),
    # Крупная без поля spec: гейт называет долг «нет поля spec:» без имени шага —
    # доска обязана поставить карточку на спеку, а не пропустить её (ревью G-01).
    ("t.nospec", FIRST, [], ["бумаги собраны"], "крупная без spec → колонка «спека»"),
    # План назван картой (поле «бумаги») не по конвенции: гейт его видит — и
    # доска тоже, путь шага — названный (ревью G-02).
    ("t.named", "ревью кода", [], ["бумаги собраны"], "бумага по полю «бумаги» засчитана"),
    # test — ждёт живой проверки; чем подтверждено — видно на карточке.
    ("t.test1", "Проверка", ["селфтест 12 путей и живой прогон"], ["нечем подтверждено"],
     "test с чем_подтверждено → Проверка"),
    ("t.test2", "Проверка", ["нечем подтверждено"], [], "test без чем_подтверждено → Проверка, жёлтая"),
    # Готово: чьей проверкой — машины или владельца.
    ("t.accepted", "Готово", ["проверено машиной"], ["проверено вами"], "принято → Готово, машиной"),
    ("t.done", "Готово", ["проверено вами"], ["проверено машиной"], "done → Готово, вами"),
    # Статус вне списка не теряется и не прячется в очередь молча.
    ("t.weird", "Очередь", ["непонятный статус: почти"], [], "статус вне списка → красная пилюля"),
    # blocked не двигает карточку, а добавляет причину — в любой колонке.
    ("t.blockedplan", "Очередь", ["ждёт: ключ от владельца"], [], "blocked в Очереди → «ждёт: …»"),
    ("t.blockedwip", "контракт", ["ждёт: чужая поставка"], ["бумаги собраны"],
     "blocked в колонке шага → та же колонка + «ждёт: …»"),
]

print("── таблица §3.2 ──")
column = getattr(board_module, "column", None)
results = {}
for task_id, want_column, must, must_not, label in ROWS:
    try:
        got_column, pills, _steps = column(cards[task_id], judge)
    except Exception as exc:  # noqa: BLE001
        path_bad(f"{label}: column() упала — {type(exc).__name__}: {exc}")
        continue
    results[task_id] = (got_column, pills)
    text = joined(pills)
    problems = []
    if got_column != want_column:
        problems.append(f"колонка «{got_column}», ждали «{want_column}»")
    problems += [f"нет пилюли «{m}»" for m in must if m not in text]
    problems += [f"лишняя пилюля «{m}»" for m in must_not if m in text]
    if task_id == "t.plan" and pills:
        problems.append(f"у plan пилюли быть не должно: {text}")
    check(not problems, label, f"{label}: {'; '.join(problems)} (пилюли: {text or '—'})")

# ── одна правда (§7.1, приложение Б2) ────────────────────────────────────
# Охраняет главное: владелец закрывает задачу по доске — и гейт обязан
# согласиться. «Бумаги собраны» ⇔ гейт при переходе в test долга не назовёт;
# «ждёт твоего слова» ⇔ в долге гейта беда одобрения. Судят НАСТОЯЩИЕ функции
# гейта на той же фикстуре; расхождение — две правды о стадии.
print("── одна правда ──")
debt_of = getattr(judge, "долг_задачи")
paper_root = getattr(judge, "корень_бумаг")
approval_of = getattr(judge.spec_approval, "беды_одобрения")
big_wip = [t for t in ALL_IDS if cards[t]["status"] == "wip" and FEATURES.get(t) == BIG]
seen_ready = seen_waiting = False
for task_id in big_wip:
    if task_id not in results:
        path_bad(f"{task_id}: колонки нет — одна правда не проверена")
        continue
    got_column, pills = results[task_id]
    text = joined(pills)
    the_card = cards[task_id]
    board_ready = got_column == "ревью кода" and "бумаги собраны" in text
    gate_ready = debt_of({**the_card, "status": "test"}, price, settings)[1] == []
    check(board_ready == gate_ready,
          f"{task_id}: «бумаги собраны» на доске ({board_ready}) = гейт при закрытии чист ({gate_ready})",
          f"{task_id}: доска «бумаги собраны»={board_ready}, а гейт при закрытии чист={gate_ready}")
    root, trouble = paper_root(the_card)
    waiting_debts = approval_of(task_id, the_card, root) if root is not None else []
    gate_waiting = any(d in waiting_debts for d in debt_of(the_card, price, settings)[1])
    board_waiting = got_column == "ревью спеки" and "ждёт твоего слова по спеке" in text
    check(board_waiting == gate_waiting,
          f"{task_id}: «ждёт слова» на доске ({board_waiting}) = беда одобрения у гейта ({gate_waiting})",
          f"{task_id}: доска «ждёт слова»={board_waiting}, а беда одобрения у гейта={gate_waiting}")
    seen_ready |= gate_ready
    seen_waiting |= gate_waiting
# Предикат, ни разу не истинный, доказывает только себя: нужна хоть одна
# фикстура, где гейт чист, и хоть одна, где он ждёт одобрения.
check(seen_ready and seen_waiting, "одна правда не пустая: есть и «чисто», и «ждёт слова»",
      f"фикстура вырождена: чисто={seen_ready}, ждёт слова={seen_waiting}")

# Тот же разбор «git молчит» и «PRODUCT_REPO пуст» гейтом: обе — (None, [беда]),
# различаются только корнем бумаг. Проверка фикстуры, а не снимка: без неё
# строки таблицы выше могли бы зеленеть на неверной посылке.
mute = debt_of(cards["t.gitmute"], price, settings)
prod = debt_of(cards["t.product"], price, settings)
check(mute[0] is None and prod[0] is None and paper_root(cards["t.product"])[1]
      and not paper_root(cards["t.gitmute"])[1],
      "гейт: оба случая — цена None, различаются по корню бумаг (Б4)",
      f"посылка фикстуры неверна: git молчит {mute}, продукт {prod}")

# ── формат board.json (§3.3) ─────────────────────────────────────────────
# Охраняет то, что читает панель: колонки в порядке пульта владельца (данные,
# не литерал), ни одна карточка не потеряна (обрезка 40 была причиной задачи),
# тексты источников обрезаны — снимок едет в браузер.
print("── формат снимка ──")
NOW = datetime(2026, 10, 14, 12, 0, 0)
board = None
try:
    board = getattr(board_module, "build")(MAP_PATH, judge, NOW)
    path_ok("build() отдал снимок")
except Exception as exc:  # noqa: BLE001
    path_bad(f"build() упал — {type(exc).__name__}: {exc}")
    traceback.print_exc(limit=3)

if isinstance(board, dict):
    top = {"снято", "направление", "архив", "колонки", "задачи", "беда"}
    check(top <= set(board), "ключи верхнего уровня на месте",
          f"нет ключей: {sorted(top - set(board))}")
    check(board.get("колонки") == COLUMNS,
          f"колонки: Очередь · шаги «крупной» из pipeline.yaml ({len(STEP_COLUMNS)}) · Проверка · Готово",
          f"колонки {board.get('колонки')}, ждали {COLUMNS}")
    check(str(board.get("снято", "")).startswith("2026-10-14T12:00"), "«снято» — переданное время в ISO",
          f"«снято» = {board.get('снято')!r}")
    check(board.get("беда") is None, "у исправной карты беды нет", f"беда: {board.get('беда')!r}")
    check(board.get("архив") == 2, "архив — числом (2 закрытых в фикстуре)",
          f"архив = {board.get('архив')!r}, ждали 2")
    tasks = board.get("задачи") or []
    ids = [t.get("id") for t in tasks]
    check(len(tasks) == len(ALL_IDS) and set(ids) == set(ALL_IDS),
          f"все карточки карты в снимке: {len(tasks)} = {len(ALL_IDS)}",
          f"в снимке {len(tasks)} задач, в карте {len(ALL_IDS)}; расхождение {sorted(set(ids) ^ set(ALL_IDS))}")
    task_keys = {"id", "название", "статус", "колонка", "пилюли", "уровень_объявлен",
                 "уровень_по_коммитам", "цена", "шаги", "источники", "журнал", "коммиты", "скилы"}
    missing = {t.get("id"): sorted(task_keys - set(t)) for t in tasks if task_keys - set(t)}
    check(not missing, "у каждой задачи полный набор полей §3.3", f"нет полей: {missing}")
    stray = {t.get("id"): t.get("колонка") for t in tasks if t.get("колонка") not in COLUMNS}
    check(not stray, "колонка каждой задачи — из списка колонок", f"колонки вне списка: {stray}")
    by_id = {t.get("id"): t for t in tasks}
    same = {i: (by_id[i].get("колонка"), results[i][0]) for i in results
            if i in by_id and by_id[i].get("колонка") != results[i][0]}
    check(not same, "снимок и column() ставят задачи в одни колонки", f"расходятся: {same}")

    full = by_id.get("t.allpapers") or {}
    steps = {s.get("шаг"): s for s in full.get("шаги") or []}
    check(set(steps) == set(STEP_KEYS), "шаги задачи — полный список «крупной»",
          f"шаги задачи: {sorted(steps)}, ждали {STEP_KEYS}")
    step_keys = {"шаг", "путь", "есть", "почему"}
    check(all(step_keys <= set(s) for s in steps.values()), "у шага есть шаг/путь/есть/почему",
          f"неполные шаги: {[s for s in steps.values() if not step_keys <= set(s)]}")
    check(steps.get("тесты", {}).get("есть", False) is None,
          "«тесты» машиной не судятся: есть = null", f"тесты: {steps.get('тесты')}")
    check(all(steps.get(k, {}).get("есть") is True for k in ALL_PAPERS),
          "положенные бумаги отмечены «есть»", f"шаги: {steps}")
    check(str(steps.get("спека", {}).get("путь", "")).endswith("docs/specs/t-all.md"),
          "путь спеки — тот, что назвал гейт", f"путь спеки: {steps.get('спека')}")
    named = {s.get("шаг"): s for s in (by_id.get("t.named") or {}).get("шаги") or []}.get("план", {})
    check(str(named.get("путь", "")).endswith("docs/plans/общий-план.md") and named.get("есть") is True,
          "путь бумаги из поля «бумаги» — тот, что назвала карта", f"план t.named: {named}")
    contract = {s.get("шаг"): s for s in (by_id.get("t.debt") or {}).get("шаги") or []}.get("контракт", {})
    check(contract.get("есть") is False, "недостающая бумага отмечена «нет»", f"контракт t.debt: {contract}")

    sources = full.get("источники") or []
    text = (sources[0].get("текст") if sources else None) or ""
    check(sources and sources[0].get("id") == f"inbox:{SOURCE_ID}" and 0 < len(text) <= 4000,
          f"текст источника из inbox обрезан до 4000 ({len(text)})",
          f"источник: {[(s.get('id'), len(s.get('текст') or '')) for s in sources]}")
    # Владелец присылал в канал ключ (29.09.2026): карточка вырезает его
    # фильтром разборщика, а доска брала сырой текст и несла ключ в веб-панель.
    check(SECRET_VALUE not in json.dumps(board, ensure_ascii=False),
          "ключ из сообщения владельца на доску не попадает",
          "ключ из сообщения владельца лежит в снимке доски")
    log_text = full.get("журнал") or ""
    check(0 < len(log_text) <= 4000, f"журнал.md обрезан до 4000 ({len(log_text)})",
          f"журнал: {len(log_text)} знаков")
    plan_sources = (by_id.get("t.plan") or {}).get("источники") or []
    check(plan_sources and plan_sources[0].get("текст") is None
          and plan_sources[0].get("строка") == "ревизия:проба:scripts/x.py:строка как есть",
          "источник не из inbox — строкой как есть, текст null", f"источник t.plan: {plan_sources}")
    skills = full.get("скилы") or []
    check(len(skills) == 1 and skills[0].get("скил") == "plan-writing"
          and skills[0].get("откуда_задача") == "карта",
          "журнал скилов — по задаче, с «откуда_задача»", f"скилы: {skills}")
    check(isinstance(full.get("коммиты"), list), "коммиты — списком (вне git — пусто, не падение)",
          f"коммиты: {full.get('коммиты')!r}")
    check(full.get("уровень_объявлен") == "крупная" and full.get("цена") == getattr(price, "цена")(BIG, settings["веса"]),
          "уровень объявленный и цена по коммитам — оба на карточке (C3)",
          f"уровень {full.get('уровень_объявлен')!r}, цена {full.get('цена')!r}")

# Битая карта — беда словами, а не пустая доска: пустая доска читается как
# «задач нет», и владелец не узнает, что снимок не прочитал карту.
print("── битая карта ──")
BROKEN = TMP / "broken"
BROKEN.mkdir()
(BROKEN / "dev-map.yaml").write_text("lines: [ {id: harness, tasks: [\n", encoding="utf-8")
try:
    broken_judge, _ = make_judge(BROKEN, FEATURES)
    broken = getattr(board_module, "build")(BROKEN / "dev-map.yaml", broken_judge, NOW)
    check(str(broken.get("беда") or "").startswith("карта не прочитана"),
          "битая карта → «беда»: карта не прочитана", f"беда: {broken.get('беда')!r}")
except Exception as exc:  # noqa: BLE001
    path_bad(f"битая карта уронила build() — {type(exc).__name__}: {exc}")

# ── main(): атомарная запись ─────────────────────────────────────────────
# Служба панели читает board.json в любой момент: полфайла — битая доска,
# огрызок tmp — мусор в каталоге состояния. Запись — tmp + os.replace, права
# 0640 (группа читает, контракт). Сборка подменена готовым снимком: main()
# судится за запись, а не за чтение живой карты.
print("── main() ──")
target = PANEL / "board.json"
target.write_text("старый снимок", encoding="utf-8")
# Каталог состояния пишет и служба панели: подложенный ею симлинк на месте
# временного файла заставил бы снимок агента затирать чужой файл каждую минуту.
victim = TMP / "victim.txt"
victim.write_text("чужой файл", encoding="utf-8")
(PANEL / "board.json.tmp").symlink_to(victim)
old_inode = target.stat().st_ino
fixture_board = board if isinstance(board, dict) else {
    "снято": NOW.isoformat(), "направление": "", "архив": 0, "колонки": COLUMNS,
    "задачи": [], "беда": None}
setattr(board_module, "build", lambda *args, **kwargs: fixture_board)
saved_argv = sys.argv
sys.argv = [str(HERE / "build-board.py")]
output = io.StringIO()
try:
    with contextlib.redirect_stdout(output):
        rc = getattr(board_module, "main")()
    check(rc in (0, None), "main() вернул 0", f"main() вернул {rc!r}")
except SystemExit as stop:
    check(stop.code in (0, None), "main() вышел с 0", f"main() вышел с {stop.code!r}")
except Exception as exc:  # noqa: BLE001
    path_bad(f"main() упал — {type(exc).__name__}: {exc}")
finally:
    sys.argv = saved_argv
printed = output.getvalue()
check("доска: задач" in printed and "снято" in printed, "main() печатает «доска: задач N, снято ЧЧ:ММ»",
      f"вывод main(): {printed.strip()!r}")
try:
    written = json.loads(target.read_text(encoding="utf-8"))
    check(written == fixture_board, "board.json — валидный JSON, ровно снимок",
          "board.json не совпадает со снимком")
except (OSError, ValueError) as exc:
    path_bad(f"board.json не читается как JSON: {exc}")
check(target.exists() and target.stat().st_ino != old_inode,
      "файл заменён целиком (os.replace), а не дописан поверх",
      "inode тот же — запись поверх живого файла, служба увидит полфайла")
check(victim.read_text(encoding="utf-8") == "чужой файл",
      "подложенный симлинк на tmp не ведёт запись в чужой файл",
      "снимок записан через подложенный симлинк в чужой файл")
(PANEL / "board.json.tmp").unlink(missing_ok=True)
leftovers = sorted(p.name for p in PANEL.iterdir() if p.name != "board.json")
check(not leftovers, "в каталоге состояния нет tmp-огрызков", f"огрызки: {leftovers}")
mode = target.stat().st_mode & 0o777 if target.exists() else None
check(mode == 0o640, "права board.json 0640", f"права board.json {oct(mode) if mode else mode}")

finish()
