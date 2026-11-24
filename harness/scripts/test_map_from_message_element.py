#!/usr/bin/env python3
"""Проба: задача владельца при рождении получает место в архитектуре.

Владелец 20.11: «если я говорю какую-то задачу, то эта задача должна либо
создавать какой-то новый элемент в этой архитектуре, либо сразу же
автоматически относиться к тому элементу архитектуры, который есть».
Носитель — перехват: карточка рождается с полем `элемент` (или с частью и
`новый_элемент`). План: план-носитель.md задачи разбиения харнеса на части,
Task А1 (раздел «библиотека») и Task А2 (раздел «перехват», случаи К6 1–10).

══════════════════════════════ КОНТРАКТ ══════════════════════════════════════
Библиотека места (scripts/lib/architecture.py, рядом с механизмом под пробой):
 • resolve_element(реестр, сырое) → (элемент, None) для id из реестра;
   «новый: <id части>: <имя>» с частью из реестра → (часть, имя); всё прочее —
   часть вне реестра, id вне реестра, пусто, None, список — (None, None);
 • element_menu(реестр) — по строке «id — имя: что» на часть (что = обязанность)
   и на элемент; на живом реестре дерева не длиннее 4000 знаков;
 • judged_lines(конф) — линии карты, которые судит архитектура: ключ
   ARCHITECTURE_LINES через пробел, без ключа — {"harness"};
   settings()["линии"] — то же из файлов (HARNESS_CONF, потом конфиг дерева).
Перехват (`map-from-message.py --сообщение <id> --карта К --инбокс И --линия L`):
 • ответ модели несёт «элементы» в тех же позициях, что «задачи»; карточка
   получает `элемент: <id>` или `элемент: <часть>` + `новый_элемент: "<имя>"`,
   только если значение прошло resolve_element и линия карточки — из
   ARCHITECTURE_LINES;
 • в вопрос модели кладётся блок «ЭЛЕМЕНТЫ АРХИТЕКТУРЫ» с меню, только если
   линия карточки судима (линия названа доводом --линия) или — без довода —
   в карте есть судимая линия; иначе вопрос побайтно прежний;
 • реестр не читается → карточка как прежде, в выводе «реестр не читается»,
   код 0; модель молчит → карточка без места;
 • печать «длина вопроса: N» (N — знаков в вопросе) — база цены вопроса (К14);
 • карта с новыми полями проходит схему дерева при DEVMAP_SCHEMA_STRICT=1.
═════════════════════════════════════════════════════════════════════════════

Стенд: свой temp-каталог на случай, свой реестр (HARNESS_ARCHITECTURE), свой
harness.conf (HARNESS_CONF), свой LOG_DIR; модель — заглушка
HARNESS_SPROSIT_MODEL, копирующая вопрос в файл и отдающая заданный ответ.
Дверь: MAP_FROM_MESSAGE_PY — копия механизма для мутаций; библиотека берётся
из lib/ рядом с ней (так её берёт сам перехват).
Прогон: python3 scripts/test_map_from_message_element.py; код 0 — зелёный.
"""
import datetime
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.dont_write_bytecode = True

UNDER_TEST = Path(os.environ.get("MAP_FROM_MESSAGE_PY")
                  or Path(__file__).resolve().parent / "map-from-message.py")
TREE = Path(__file__).resolve().parents[1]
TREE_SCHEMA = TREE / "harness" / "config" / "devmap-schema.yaml"
TREE_REGISTRY = TREE / "harness" / "config" / "architecture.yaml"
GATE = TREE / "scripts" / "pre-commit-gate.sh"

TODAY = datetime.date.today().isoformat()
MENU_HEADER = "ЭЛЕМЕНТЫ АРХИТЕКТУРЫ"

REGISTRY = {
    "версия": 1,
    "части": [
        {"id": "talk", "имя": "Разговор", "обязанность": "приём и отправка сообщений владельца",
         "элементы": [{"id": "talk.send", "имя": "Отправка", "что": "отправка вести владельцу"}]},
        {"id": "host", "имя": "Машина", "обязанность": "сервер, диск и службы",
         "элементы": []},
    ],
}
EXPECTED_MENU = {
    "talk — Разговор: приём и отправка сообщений владельца",
    "talk.send — Отправка: отправка вести владельцу",
    "host — Машина: сервер, диск и службы",
}

# layers, queue и layer у линий — обязательные поля карты для гейта схемы (случай 9).
MAP_TEXT = f"""version: 1
queue: []
layers:
  - {{ id: infra, label: "Харнес", color: "#5aa06a" }}
  - {{ id: product, label: "Продукт", color: "#2b9bd6" }}
lines:
  - id: core
    name: Ядро
    layer: infra
    tasks:
      - id: hr.stand-core-task
        status: wip
        заведено: "{TODAY}"
        title: "Сторож скорости тормозит работу"
        summary: "сторож скорости и очереди тормозят смену"
  - id: prod
    name: Продукт
    layer: product
    tasks:
      - id: pr.stand-product-task
        status: plan
        заведено: "{TODAY}"
        title: "Витрина продукта"
        summary: "страница продукта"
"""

NEW_WORK = "Надо сделать выгрузку отчёта в CSV для панели, его сейчас нет вовсе"
TWO_WORKS = ("Надо сделать выгрузку отчёта в CSV для панели и ещё добавить "
             "голосовые заметки в канал, их сейчас нет вовсе")


def answer(elements, tasks=("выгрузка отчёта в CSV",), layer="harness") -> dict:
    names = ["report-csv-export", "voice-notes-channel", "third-task"][:len(tasks)]
    reply = {"исход": "новая", "карточка": "", "задачи": list(tasks), "имена": names,
             "слои": [layer] * len(tasks), "указания": [], "вопросы": [], "ответы": []}
    if elements is not None:
        reply["элементы"] = list(elements)
    return reply


checked = failed = 0


def case(name: str, ok: bool, detail: str = "") -> None:
    global checked, failed
    checked += 1
    failed += 0 if ok else 1
    print(("  ок    " if ok else "  ПЛОХО ") + name + ("" if ok or not detail else f": {detail}"))


# ─── библиотека (Task А1) ─────────────────────────────────────────────────────

def load_library():
    """architecture.py из lib/ рядом с механизмом под пробой: мутация копии
    библиотеки (план, Task А8 п. 4) обязана ронять эту же пробу."""
    lib = str(UNDER_TEST.parent / "lib")
    sys.path.insert(0, lib)
    try:
        sys.modules.pop("architecture", None)
        return importlib.import_module("architecture")
    finally:
        sys.path.remove(lib)


def library_names(arch) -> None:
    # Поломка: функцию не завели или назвали иначе — остальные случаи раздела
    # тогда не судятся вовсе, и это видно отдельной строкой.
    for name in ("resolve_element", "element_menu", "judged_lines", "settings"):
        case(f"библиотека: {name} есть", callable(getattr(arch, name, None)),
             f"в {arch.__file__} нет функции {name}")


def library_resolve(arch) -> None:
    # Поломка: id не сверяется с реестром; «новый:» принят без проверки части;
    # список или словарь принят за место; имя нового элемента с пробелами по краям;
    # «новый:» с элементом вместо части или без имени принят за место.
    resolve = getattr(arch, "resolve_element", None)
    if resolve is None:
        return
    cases = [
        ("id из реестра", "talk.send", ("talk.send", None)),
        ("новый: часть: имя → часть + имя", "новый: talk: голосовые заметки",
         ("talk", "голосовые заметки")),
        ("новый: часть вне реестра", "новый: ghost: x", (None, None)),
        ("новый: элемент вместо части", "новый: talk.send: x", (None, None)),
        ("новый: без имени", "новый: talk: ", (None, None)),
        ("id вне реестра", "talk.nope", (None, None)),
        ("пустая строка", "", (None, None)),
        ("None", None, (None, None)),
        ("список вместо строки", ["talk"], (None, None)),
    ]
    for name, raw, expected in cases:
        try:
            got = resolve(REGISTRY, raw)
        except Exception as exc:  # noqa: BLE001 — падение = провал случая
            got = f"{type(exc).__name__}: {exc}"
        case(f"библиотека: resolve_element {name}", got == expected, f"{raw!r} → {got!r}")


def library_menu(arch) -> None:
    # Поломка: меню без частей или без элементов, иной формат строки (модель
    # получит id не в том виде), меню разрослось на живом реестре.
    menu = getattr(arch, "element_menu", None)
    if menu is None:
        return
    lines = [ln for ln in menu(REGISTRY).splitlines() if ln.strip()]
    case("библиотека: element_menu — по строке «id — имя: что» на часть и элемент",
         set(lines) == EXPECTED_MENU and len(lines) == len(EXPECTED_MENU), repr(lines))
    live = arch.load(TREE_REGISTRY)
    live_ids = arch.element_ids(live)
    live_menu = menu(live)
    live_lines = [ln for ln in live_menu.splitlines() if ln.strip()]
    case("библиотека: живой реестр разобран (частей и элементов больше нуля)", bool(live_ids))
    case("библиотека: element_menu живого реестра — строка на каждый id",
         bool(live_ids) and {ln.split(" — ", 1)[0] for ln in live_lines} == live_ids
         and len(live_lines) == len(live_ids), f"строк {len(live_lines)}, id {len(live_ids)}")
    case("библиотека: element_menu живого реестра ≤ 4000 знаков", len(live_menu) <= 4000,
         f"длина {len(live_menu)}")


def library_lines(arch, tmp: Path) -> None:
    # Поломка: без ключа линий нет (архитектура перестаёт судить харнес),
    # ключ не делится по пробелу, settings() читает не те файлы.
    judged = getattr(arch, "judged_lines", None)
    if judged is not None:
        case("библиотека: judged_lines без ключа → {harness}", judged({}) == {"harness"},
             repr(judged({})))
        got = judged({"ARCHITECTURE_LINES": "a b"})
        case("библиотека: judged_lines ARCHITECTURE_LINES=\"a b\" → {a, b}", got == {"a", "b"},
             repr(got))
    settings = getattr(arch, "settings", None)
    if settings is None:
        return
    conf = tmp / "settings-harness.conf"
    conf.write_text('ARCHITECTURE_LINES="a b"\n', encoding="utf-8")
    before = os.environ.get("HARNESS_CONF")
    os.environ["HARNESS_CONF"] = str(conf)
    try:
        got = settings().get("линии")
    finally:
        if before is None:
            os.environ.pop("HARNESS_CONF")
        else:
            os.environ["HARNESS_CONF"] = before
    case("библиотека: settings()[\"линии\"] из HARNESS_CONF → {a, b}", got == {"a", "b"}, repr(got))


def library(tmp: Path) -> None:
    arch = load_library()
    library_names(arch)
    library_resolve(arch)
    library_menu(arch)
    library_lines(arch, tmp)


# ─── перехват (Task А2, случаи К6) ────────────────────────────────────────────

def cards(text: str) -> dict:
    data = yaml.safe_load(text) or {}
    return {t["id"]: dict(t, линия=line.get("id"))
            for line in data.get("lines") or [] for t in line.get("tasks") or []}


class Stand:
    """Свой каталог: карта, инбокс, реестр, harness.conf, журналы, заглушка модели."""

    def __init__(self, root: Path, text: str, reply, lines: str = "core",
                 registry=REGISTRY, line_arg: str | None = "core"):
        self.root = root
        self.line_arg = line_arg
        self.message_id = f"{int(datetime.datetime.now().timestamp()) - 600}.123456789"
        done = root / "inbox" / "обработано"
        done.mkdir(parents=True)
        (done / f"{self.message_id}.txt").write_text(text, encoding="utf-8")
        (root / "docs").mkdir()
        (root / "docs" / "dev-map-archive.yaml").write_text("tasks: []\n", encoding="utf-8")
        (root / "память").mkdir()
        self.log = root / "log"
        self.log.mkdir()
        self.map = root / "dev-map.yaml"
        self.map.write_text(MAP_TEXT, encoding="utf-8")
        self.registry = root / "реестр-архитектуры.yaml"
        if registry is not None:
            self.registry.write_text(yaml.safe_dump(registry, allow_unicode=True), encoding="utf-8")
        self.question_file = root / "вопрос-модели.txt"
        stub = root / "stub-model.py"
        body = ("sys.exit(1)\n" if reply is None else
                f"print({json.dumps(reply, ensure_ascii=False)!r})\n")
        stub.write_text("import shutil, sys\n"
                        f"shutil.copy(sys.argv[sys.argv.index('--вопрос-файл') + 1], "
                        f"{str(self.question_file)!r})\n" + body, encoding="utf-8")
        conf = root / "harness.conf"
        conf.write_text(f'ARCHITECTURE_LINES="{lines}"\nACTIVE_LINE=""\n'
                        "MAP_INTAKE_CONTEXT_DAYS=3\nMAP_INTAKE_CONTEXT_RECENT=1\n",
                        encoding="utf-8")
        self.env = dict(os.environ, LOG_DIR=str(self.log), HARNESS_LOG_DIR=str(self.log),
                        HARNESS_EVENTS_STATE=str(self.log / ".events"),
                        HEARTBEAT_DIR=str(self.log / "heartbeat"),
                        HARNESS_PANEL_STATE=str(self.log / "panel-state"),
                        HARNESS_CONF=str(conf), HARNESS_ARCHITECTURE=str(self.registry),
                        HARNESS_SPROSIT_MODEL=str(stub),
                        HARNESS_PLANS_DIR=str(root / "планы"), PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("HARNESS_DEVMAP", None)
        self.before = cards(MAP_TEXT)
        self.result = None

    def run(self) -> "Stand":
        argv = [sys.executable, str(UNDER_TEST), "--сообщение", self.message_id,
                "--карта", str(self.map), "--инбокс", str(self.root / "inbox")]
        if self.line_arg:
            argv += ["--линия", self.line_arg]
        self.result = subprocess.run(argv, capture_output=True, text=True, env=self.env,
                                     cwd=self.root, timeout=180)
        return self

    @property
    def output(self) -> str:
        return self.result.stdout + self.result.stderr

    def tail(self) -> list:
        return self.output.strip().splitlines()[-3:]

    def question(self) -> str:
        return self.question_file.read_text(encoding="utf-8") if self.question_file.exists() else ""

    def new_cards(self) -> list:
        """Новые карточки в порядке карты: судимый список берётся разбором карты."""
        after = cards(self.map.read_text(encoding="utf-8"))
        return [card for cid, card in after.items() if cid not in self.before]

    def new_blocks(self) -> list:
        """Сырые строки каждой новой карточки: проба судит и форму строки поля."""
        new_ids = {c["id"] for c in self.new_cards()}
        blocks, current = [], None
        for line in self.map.read_text(encoding="utf-8").splitlines():
            found = re.match(r"^      - id: (\S+)$", line)
            if found:
                current = [line] if found.group(1) in new_ids else None
                if current is not None:
                    blocks.append(current)
            elif current is not None:
                current.append(line)
        return blocks


def place_lines(block: list) -> list:
    return [ln for ln in block if re.match(r"^\s+(элемент|новый_элемент):", ln)]


def one_card(s: Stand, name: str):
    """Ровно одна новая карточка и код 0; иначе случай дальше не судится."""
    new = s.new_cards()
    case(f"перехват: {name}: код 0, заведена одна карточка",
         s.result.returncode == 0 and len(new) == 1,
         f"код {s.result.returncode}, новых {len(new)}, {s.tail()}")
    blocks = s.new_blocks()
    return (new[0], blocks[0]) if len(new) == 1 and len(blocks) == 1 else (None, None)


def known_element(tmp: Path) -> Stand:
    # Случаи 1 и 10. Поломка: поле не пишется, пишется под другим именем или
    # отступом (вне карточки), меню реестра не легло в вопрос, длину не печатает.
    name = "элемент из реестра → поле в карточке"
    s = Stand(tmp / "1-элемент", NEW_WORK, answer(["talk.send"])).run()
    card, block = one_card(s, name)
    case(f"перехват: {name}", bool(block) and "        элемент: talk.send" in block
         and (card or {}).get("элемент") == "talk.send", repr(place_lines(block or [])))
    case(f"перехват: {name}: новый_элемент нет", bool(card) and "новый_элемент" not in card,
         repr(place_lines(block or [])))
    question = s.question()
    case(f"перехват: {name}: в вопросе блок {MENU_HEADER} с меню реестра",
         MENU_HEADER in question and "talk.send — Отправка" in question, question[-300:])
    found = re.search(r"длина вопроса: (\d+)", s.output)
    case("перехват: вопрос с реестром печатает «длина вопроса: N» (N — знаков вопроса)",
         bool(found) and bool(question) and int(found.group(1)) == len(question),
         f"{found.group(0) if found else 'строки нет'}, знаков в вопросе {len(question)}")
    return s


def new_element(tmp: Path) -> Stand:
    # Случай 2. Поломка: «новый:» пишется сырой строкой в элемент, имя теряется,
    # имя не в кавычках (yaml разберёт двоеточие в имени как словарь).
    name = "новый: имя → часть + новый_элемент"
    s = Stand(tmp / "2-новый", NEW_WORK, answer(["новый: talk: голосовые заметки"])).run()
    card, block = one_card(s, name)
    case(f"перехват: {name}",
         bool(block) and "        элемент: talk" in block
         and '        новый_элемент: "голосовые заметки"' in block
         and (card or {}).get("элемент") == "talk"
         and (card or {}).get("новый_элемент") == "голосовые заметки",
         repr(place_lines(block or [])))
    return s


def unknown_element(tmp: Path) -> None:
    # Случай 3. Поломка: id модели пишется без сверки с реестром.
    name = "id вне реестра → места нет, карточка заведена"
    s = Stand(tmp / "3-вне", NEW_WORK, answer(["talk.nope"])).run()
    card, block = one_card(s, name)
    case(f"перехват: {name}", bool(block) and not place_lines(block), repr(place_lines(block or [])))


def registry_missing(tmp: Path) -> None:
    # Случай 4. Поломка: битый реестр роняет приём сообщения (слово владельца
    # теряется) или молчит о себе.
    name = "реестр не читается → карточка как прежде"
    s = Stand(tmp / "4-без-реестра", NEW_WORK, answer(["talk.send"]), registry=None).run()
    card, block = one_card(s, name)
    case(f"перехват: {name}: в выводе «реестр не читается»", "реестр не читается" in s.output,
         repr(s.tail()))
    case(f"перехват: {name}: места нет", bool(block) and not place_lines(block),
         repr(place_lines(block or [])))


def line_not_judged(tmp: Path) -> None:
    # Случай 5. Поломка: условие линии перевёрнуто или не проверяется — место
    # пишется карточкам линии, которую архитектура не судит.
    name = "линия вне ARCHITECTURE_LINES (--линия prod)"
    s = Stand(tmp / "5-prod", NEW_WORK, answer(["talk.send"]), line_arg="prod").run()
    card, block = one_card(s, name)
    case(f"перехват: {name}: карточка в prod", (card or {}).get("линия") == "prod", repr(card))
    case(f"перехват: {name}: в вопросе нет {MENU_HEADER}",
         bool(s.question()) and MENU_HEADER not in s.question(), s.question()[-300:])
    case(f"перехват: {name}: места нет", bool(block) and not place_lines(block),
         repr(place_lines(block or [])))
    name = "линия вне ARCHITECTURE_LINES по слою модели"
    s = Stand(tmp / "5-слой", NEW_WORK, answer(["talk.send"], layer="product"),
              line_arg=None).run()
    card, block = one_card(s, name)
    case(f"перехват: {name}: карточка в prod", (card or {}).get("линия") == "prod", repr(card))
    case(f"перехват: {name}: места нет", bool(block) and not place_lines(block),
         repr(place_lines(block or [])))


def no_judged_line(tmp: Path) -> None:
    # Случай 6. Поломка: меню кладётся всегда — вопрос модели меняется (и
    # дорожает) у копий, чья карта архитектурой не судится. Без --линия, как у
    # живых копий: ACTIVE_LINE="" и диспетчер довода не передаёт — судится карта.
    name = "в карте нет судимой линии → вопрос побайтно прежний"
    s = Stand(tmp / "6-нет-линии", NEW_WORK, answer(["talk.send"]), lines="zzz",
              line_arg=None).run()
    empty = Stand(tmp / "6-пустой-реестр", NEW_WORK, answer(["talk.send"]),
                  registry={"версия": 1, "части": []}, line_arg=None).run()
    asked, base = s.question(), empty.question()
    case(f"перехват: {name}", bool(asked) and asked == base and MENU_HEADER not in base,
         f"знаков {len(asked)} против {len(base)}")
    card, block = one_card(s, name)
    case(f"перехват: {name}: места нет", bool(block) and not place_lines(block),
         repr(place_lines(block or [])))


def silent_model(tmp: Path) -> None:
    # Случай 7. Поломка: запасной путь без модели падает на отсутствии ответа
    # или выдумывает место.
    name = "модель молчит → карточка без места"
    s = Stand(tmp / "7-молчит", NEW_WORK, None).run()
    card, block = one_card(s, name)
    case(f"перехват: {name}", bool(block) and not place_lines(block),
         repr(place_lines(block or [])))


def short_elements(tmp: Path) -> None:
    # Случай 8. Поломка: индекс за краем списка роняет разбор, или место первой
    # задачи размножено на остальные.
    name = "элементы короче задач → у лишних поля нет"
    s = Stand(tmp / "8-короче", TWO_WORKS,
              answer(["talk.send"], tasks=("выгрузка отчёта в CSV", "голосовые заметки в канал"))
              ).run()
    new = s.new_cards()
    case(f"перехват: {name}: код 0, заведено две карточки",
         s.result.returncode == 0 and len(new) == 2, f"код {s.result.returncode}, {s.tail()}")
    places = [c.get("элемент") for c in new]
    case(f"перехват: {name}", places == ["talk.send", None], repr(places))


def schema_gate(gate_map: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(GATE), "--devmap-only", str(gate_map)],
                          capture_output=True, text=True, cwd=gate_map.parent, timeout=120,
                          env=dict(os.environ, DEVMAP_SCHEMA_STRICT="1"))


def tree_schema(tmp: Path, stands: list) -> None:
    # Случай 9. Поломка: перехват пишет поле, которого схема карты не знает, —
    # при DEVMAP_SCHEMA_STRICT=1 коммит карты отказан. Стенд гейта: копия схемы
    # дерева рядом с картой (валидатор ищет её вверх от карты).
    name = "схема дерева принимает поле"
    case(f"перехват: {name}: карты случаев 1–2 есть", len(stands) == 2, f"карт {len(stands)}")
    # Контроль стенда: карта без места проходит тот же гейт — красный ниже
    # тогда значит «поле», а не «стенд не похож на карту».
    base_root = tmp / "9-схема-база"
    (base_root / "harness" / "config").mkdir(parents=True)
    shutil.copy(TREE_SCHEMA, base_root / "harness" / "config" / "devmap-schema.yaml")
    (base_root / "dev-map.yaml").write_text(MAP_TEXT, encoding="utf-8")
    run = schema_gate(base_root / "dev-map.yaml")
    case(f"перехват: {name}: контроль — карта стенда без места проходит гейт",
         run.returncode == 0, (run.stdout + run.stderr).strip()[-300:])
    gate_maps = []
    for number, s in enumerate(stands, 1):
        gate_root = tmp / f"9-схема-{number}"
        (gate_root / "harness" / "config").mkdir(parents=True)
        shutil.copy(TREE_SCHEMA, gate_root / "harness" / "config" / "devmap-schema.yaml")
        gate_map = gate_root / "dev-map.yaml"
        shutil.copy(s.map, gate_map)
        gate_maps.append(gate_map)
        fields = {k for c in cards(gate_map.read_text(encoding="utf-8")).values() for k in c}
        case(f"перехват: {name}: в карте стенда {number} есть поле элемент (есть что судить)",
             "элемент" in fields, repr(sorted(fields)))
        run = schema_gate(gate_map)
        out = (run.stdout + run.stderr).strip()
        case(f"перехват: {name} (карта стенда {number}, DEVMAP_SCHEMA_STRICT=1)",
             run.returncode == 0 and "не найдена" not in out, out[-300:])
    if not gate_maps:
        return
    # Контроль стенда: чужое поле на той же схеме краснит — гейт судит, а не молчит.
    control = gate_maps[0]
    control.write_text(control.read_text(encoding="utf-8").replace(
        "        status: wip\n", "        status: wip\n        поле_которого_нет: x\n", 1),
        encoding="utf-8")
    run = schema_gate(control)
    case(f"перехват: {name}: контроль — чужое поле краснит тот же гейт", run.returncode != 0,
         (run.stdout + run.stderr).strip()[-200:])


def intercept(tmp: Path) -> None:
    stands = []
    for probe in (known_element, new_element):
        try:
            stands.append(probe(tmp))
        except Exception as exc:  # noqa: BLE001 — падение случая = провал, не падение пробы
            case(f"перехват: {probe.__name__}: исключение", False, f"{type(exc).__name__}: {exc}")
    for probe in (unknown_element, registry_missing, line_not_judged, no_judged_line,
                  silent_model, short_elements):
        try:
            probe(tmp)
        except Exception as exc:  # noqa: BLE001
            case(f"перехват: {probe.__name__}: исключение", False, f"{type(exc).__name__}: {exc}")
    try:
        tree_schema(tmp, stands)
    except Exception as exc:  # noqa: BLE001
        case("перехват: tree_schema: исключение", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    if not UNDER_TEST.is_file():
        case(f"механизм по двери MAP_FROM_MESSAGE_PY ({UNDER_TEST})", False,
             "файла нет — боевой файл вместо него не беру")
        print(f"МЕСТО ЗАДАЧИ: КРАСНЫЙ ({checked} проверок, неудач {failed})")
        return 1
    with tempfile.TemporaryDirectory(prefix="map-element-") as raw:
        tmp = Path(raw)
        print("библиотека:")
        try:
            library(tmp)
        except Exception as exc:  # noqa: BLE001
            case("библиотека: исключение", False, f"{type(exc).__name__}: {exc}")
        print("перехват:")
        intercept(tmp)
    print(f"МЕСТО ЗАДАЧИ: {'зелёный' if not failed else 'КРАСНЫЙ'} "
          f"({checked} проверок, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
