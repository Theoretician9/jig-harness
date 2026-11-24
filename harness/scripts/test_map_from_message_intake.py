#!/usr/bin/env python3
"""Проба: сообщение владельца разбирается ПРОТИВ ИДУЩИХ ЗАДАЧ до карточки.

Владелец 20.11: «карта задач сама различает новую задачу и уточнение к текущей… уточнение не заводит
карточку»; 21.11: «это про мои сообщения, которые сразу карточку создают без
разбору». Из 8 его сообщений 21.11 12:1x–13:0x завелось 7 карточек-дублей
идущих задач, каждая — коммит карты в master.

══════════════════════════════ КОНТРАКТ ══════════════════════════════════════
`map-from-message.py --сообщение <id> --карта К --инбокс И --линия L`:
 • модели в вопросе показаны идущие задачи: карточки wip/plan/test, тронутые за
   MAP_INTAKE_CONTEXT_DAYS дней, и последние MAP_INTAKE_CONTEXT_RECENT карточек
   (ключи harness.conf) — id и заголовок; старая карточка вне окна не видна;
 • «исход» модели = «уточнение» и «карточка» = id из показанных: карта не
   тронута (байты), в .tasks/<id>/журнал.md дописана строка «слово владельца
   ДД.ММ ЧЧ:ММ: <текст дословно>», прежний журнал цел, в спутнике строка
   «<id> уточнение …» (гейт видит исход);
 • «уточнение» с id, которого среди показанных нет: новая карточка (слово
   владельца не теряется в чужом журнале);
 • «вопрос»: карты и журнала задачи нет, спутник «<id> не-задача: вопрос…» —
   даже если модель заодно назвала «задачу»;
 • «новая»: карточка как раньше; модель молчит — карточка как раньше;
 • каждый исход — событие журнала «разбор-сообщений» с полями исход и карточка.
═════════════════════════════════════════════════════════════════════════════

Тексты — настоящие сообщения владельца 21.11 (inbox/обработано/17952450*…).
Стенд: свой temp-каталог на случай, свой harness.conf (HARNESS_CONF), свой
LOG_DIR; модель — заглушка HARNESS_SPROSIT_MODEL, копирующая вопрос в файл.
Дверь: MAP_FROM_MESSAGE_PY — копия механизма для мутаций.
Прогон: python3 scripts/test_map_from_message_intake.py; код 0 — зелёный.
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.dont_write_bytecode = True

UNDER_TEST = Path(os.environ.get("MAP_FROM_MESSAGE_PY")
                  or Path(__file__).resolve().parent / "map-from-message.py")

TODAY = datetime.date.today().isoformat()
OLD = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()

MAP_TEXT = f"""version: 1
# Комментарий карты: байты карты при уточнении не меняются.
lines:
  - id: core
    name: Ядро
    tasks:
      - id: hr.stale-old-plan
        status: plan
        заведено: "{OLD}"
        title: "Старая забытая задача про скорость"
        summary: "давно лежит"
      - id: hr.stand-speed-task
        status: wip
        заведено: "{TODAY}"
        title: "Сторож скорости тормозит работу"
        summary: "сторож скорости и очереди тормозят смену"
      - id: hr.stand-architecture-task
        status: plan
        заведено: "{TODAY}"
        title: "Программа новой архитектуры: оркестратор и очереди"
        summary: "собеседник и работник, диспетчер"
"""

SPEED = "когда ты починишь скорость работы, очереди вот эти все"
QUESTION = "Что сейчас со скоростью"
NEW_WORK = "Надо сделать выгрузку отчёта в CSV для панели, его сейчас нет вовсе"


def answer(outcome: str, card: str = "", tasks=()) -> dict:
    return {"исход": outcome, "карточка": card, "задачи": list(tasks),
            "имена": ["report-csv-export"] if tasks else [], "слои": ["harness"] if tasks else [],
            "указания": [], "вопросы": [], "ответы": []}


checked = failed = 0


def case(name: str, ok: bool, detail: str = "") -> None:
    global checked, failed
    checked += 1
    failed += 0 if ok else 1
    print(("  ок    " if ok else "  ПЛОХО ") + name + ("" if ok or not detail else f": {detail}"))


def cards(text: str) -> dict:
    data = yaml.safe_load(text) or {}
    return {t["id"]: t for line in data.get("lines") or [] for t in line.get("tasks") or []}


class Stand:
    """Свой каталог: карта, инбокс, спутник, журналы, harness.conf, заглушка модели."""

    def __init__(self, root: Path, text: str, reply, conf: str = "",
                 old_journal: str = ""):
        self.root = root
        self.message_id = f"{int(datetime.datetime.now().timestamp()) - 600}.123456789"
        done = root / "inbox" / "обработано"
        done.mkdir(parents=True)
        (done / f"{self.message_id}.txt").write_text(text, encoding="utf-8")
        (root / "docs").mkdir()
        (root / "docs" / "dev-map-archive.yaml").write_text("tasks: []\n", encoding="utf-8")
        (root / "память").mkdir()
        if old_journal:
            journal = root / ".tasks" / "hr.stand-speed-task" / "журнал.md"
            journal.parent.mkdir(parents=True)
            journal.write_text(old_journal, encoding="utf-8")
        self.log = root / "log"
        self.log.mkdir()
        self.map = root / "dev-map.yaml"
        self.map.write_text(MAP_TEXT, encoding="utf-8")
        self.question = root / "вопрос-модели.txt"
        stub = root / "stub-model.py"
        body = ("import sys\nsys.exit(3)\n" if reply is None else
                f"print({json.dumps(reply, ensure_ascii=False)!r})\n")
        stub.write_text("import shutil, sys\n"
                        f"shutil.copy(sys.argv[sys.argv.index('--вопрос-файл') + 1], "
                        f"{str(self.question)!r})\n" + body, encoding="utf-8")
        conf_file = root / "harness.conf"
        conf_file.write_text(conf or "MAP_INTAKE_CONTEXT_DAYS=3\nMAP_INTAKE_CONTEXT_RECENT=1\n",
                             encoding="utf-8")
        self.env = dict(os.environ, LOG_DIR=str(self.log), HARNESS_LOG_DIR=str(self.log),
                        HARNESS_EVENTS_STATE=str(self.log / ".events"),
                        HEARTBEAT_DIR=str(self.log / "heartbeat"),
                        HARNESS_PANEL_STATE=str(self.log / "panel-state"),
                        HARNESS_CONF=str(conf_file),
                        HARNESS_SPROSIT_MODEL=str(stub),
                        HARNESS_PLANS_DIR=str(root / "планы"), PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("HARNESS_DEVMAP", None)

    def run(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(UNDER_TEST), "--сообщение", self.message_id,
             "--карта", str(self.map), "--инбокс", str(self.root / "inbox"), "--линия", "core"],
            capture_output=True, text=True, env=self.env, cwd=self.root, timeout=180)

    def triaged(self) -> str:
        path = self.root / "память" / ".tasks-triaged"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return next((ln for ln in lines if ln.startswith(self.message_id)), "")

    def journal(self, card: str) -> Path:
        return self.root / ".tasks" / card / "журнал.md"

    def events(self) -> list:
        path = self.log / "разбор-сообщений.jsonl"
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def event_data(self) -> dict:
        found = [e.get("data") or {} for e in self.events()
                 if (e.get("data") or {}).get("сообщение") == self.message_id]
        return found[-1] if found else {}

    def stamp(self) -> str:
        return datetime.datetime.fromtimestamp(float(self.message_id)).strftime("%d.%m %H:%M")


def tail(run) -> list:
    return (run.stdout + run.stderr).strip().splitlines()[-3:]


def refinement(tmp: Path) -> None:
    # Поломка: уточнение заводит карточку (как 21.11), пишет в карту, теряет
    # прежний журнал задачи (перезапись вместо дозаписи), пишет пересказ вместо
    # слов владельца, забывает исход для гейта или событие.
    name = "уточнение идущей задачи"
    old = "# журнал задачи\nпрежняя запись смены\n"
    s = Stand(tmp / "уточнение", SPEED,
              answer("уточнение", "hr.stand-speed-task", ["починить скорость работы"]),
              old_journal=old)
    before = s.map.read_bytes()
    run = s.run()
    case(f"{name}: код 0", run.returncode == 0, f"код {run.returncode}, {tail(run)}")
    case(f"{name}: карта не тронута (байты)", s.map.read_bytes() == before, str(tail(run)))
    journal = s.journal("hr.stand-speed-task")
    text = journal.read_text(encoding="utf-8") if journal.exists() else ""
    line = f"слово владельца {s.stamp()}: {SPEED}"
    case(f"{name}: строка в журнале задачи дословно «{line[:40]}…»", line in text, repr(text[-200:]))
    case(f"{name}: прежний журнал задачи цел", text.startswith(old), repr(text[:80]))
    case(f"{name}: исход в спутнике называет уточнение и карточку",
         "уточнение" in s.triaged() and "hr.stand-speed-task" in s.triaged(),
         repr(s.triaged()))
    data = s.event_data()
    case(f"{name}: событие исход=уточнение карточка=hr.stand-speed-task",
         data.get("исход") == "уточнение" and data.get("карточка") == "hr.stand-speed-task",
         repr(data))


def context_window(tmp: Path) -> None:
    # Поломка: модели не показали идущие задачи (вопрос без списка), окно дней
    # не применено (старая карточка в списке), ключи harness.conf не читаются.
    name = "окно идущих задач"
    s = Stand(tmp / "окно", SPEED, answer("вопрос"))
    s.run()
    question = s.question.read_text(encoding="utf-8") if s.question.exists() else ""
    case(f"{name}: wip-задача дня показана модели с заголовком",
         "hr.stand-speed-task" in question and "Сторож скорости тормозит" in question,
         question[-300:])
    case(f"{name}: свежая plan-задача показана модели", "hr.stand-architecture-task" in question)
    case(f"{name}: старая plan-задача вне окна дней и вне последних K не показана",
         "hr.stale-old-plan" not in question, question[-300:])
    wide = Stand(tmp / "окно-широкое", SPEED, answer("вопрос"),
                 conf="MAP_INTAKE_CONTEXT_DAYS=60\nMAP_INTAKE_CONTEXT_RECENT=1\n")
    wide.run()
    question = wide.question.read_text(encoding="utf-8") if wide.question.exists() else ""
    case(f"{name}: окно 60 дней из harness.conf показывает и старую", "hr.stale-old-plan" in question)


def unknown_card(tmp: Path) -> None:
    # Поломка: id от модели не сверяется со списком — слово владельца уходит в
    # журнал выдуманной или забытой задачи, и его никто не прочтёт.
    name = "уточнение карточки вне списка"
    s = Stand(tmp / "чужая", NEW_WORK,
              answer("уточнение", "hr.stale-old-plan", ["выгрузка отчёта в CSV"]))
    before = cards(s.map.read_text(encoding="utf-8"))
    s.run()
    after = cards(s.map.read_text(encoding="utf-8"))
    new = sorted(set(after) - set(before))
    case(f"{name}: заведена новая карточка", len(new) == 1, repr(new))
    case(f"{name}: журнал невидимой задачи не тронут", not s.journal("hr.stale-old-plan").exists())


def question(tmp: Path) -> None:
    # Поломка: «вопрос» модели не слушается, и названная заодно «задача»
    # заводит карточку; или исход не записан, и гейт задач из канала красен.
    name = "вопрос"
    s = Stand(tmp / "вопрос", QUESTION, answer("вопрос", "", ["что со скоростью"]))
    before = s.map.read_bytes()
    run = s.run()
    case(f"{name}: код 0", run.returncode == 0, str(tail(run)))
    case(f"{name}: карта не тронута (байты)", s.map.read_bytes() == before, str(tail(run)))
    case(f"{name}: журналов задач нет", not (s.root / ".tasks").exists())
    case(f"{name}: спутник «не-задача: вопрос»",
         "не-задача" in s.triaged() and "вопрос" in s.triaged(), repr(s.triaged()))
    case(f"{name}: событие исход=вопрос", s.event_data().get("исход") == "вопрос",
         repr(s.event_data()))


def new_task(tmp: Path) -> None:
    # Поломка: новый разбор глотает настоящую новую работу.
    name = "новая задача"
    s = Stand(tmp / "новая", NEW_WORK, answer("новая", "", ["выгрузка отчёта в CSV"]))
    before = cards(s.map.read_text(encoding="utf-8"))
    run = s.run()
    after = cards(s.map.read_text(encoding="utf-8"))
    new = sorted(set(after) - set(before))
    case(f"{name}: карточка +1, прежние на месте",
         len(new) == 1 and set(before) <= set(after), f"{new}, {tail(run)}")
    case(f"{name}: источник inbox:<id>",
         bool(new) and after[new[0]].get("источник") == f"inbox:{s.message_id}")
    data = s.event_data()
    case(f"{name}: событие исход=новая карточка={new[0] if new else '?'}",
         data.get("исход") == "новая" and bool(new) and data.get("карточка") == new[0], repr(data))


def silent_model(tmp: Path) -> None:
    # Поломка: при молчании модели новый разбор считает сообщение уточнением
    # или вопросом — и слово владельца теряется (И-1).
    name = "модель молчит"
    s = Stand(tmp / "молчит", NEW_WORK, None)
    before = cards(s.map.read_text(encoding="utf-8"))
    s.run()
    after = cards(s.map.read_text(encoding="utf-8"))
    new = sorted(set(after) - set(before))
    case(f"{name}: карточка заведена как раньше", len(new) == 1, repr(new))
    case(f"{name}: журналов задач нет", not (s.root / ".tasks").exists())
    case(f"{name}: событие исход=новая", s.event_data().get("исход") == "новая",
         repr(s.event_data()))


def main() -> int:
    if not UNDER_TEST.is_file():
        case(f"механизм по двери MAP_FROM_MESSAGE_PY ({UNDER_TEST})", False,
             "файла нет — боевой файл вместо него не беру")
        print(f"SELFTEST: КРАСНЫЙ ({checked} проверок, неудач {failed})")
        return 1
    with tempfile.TemporaryDirectory(prefix="map-intake-") as raw:
        tmp = Path(raw)
        for probe in (refinement, context_window, unknown_card, question, new_task, silent_model):
            try:
                probe(tmp)
            except Exception as exc:  # noqa: BLE001 — падение случая = провал, не падение пробы
                case(f"{probe.__name__}: исключение", False, f"{type(exc).__name__}: {exc}")
    print(f"SELFTEST: {'зелёный' if not failed else 'КРАСНЫЙ'} ({checked} проверок, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
