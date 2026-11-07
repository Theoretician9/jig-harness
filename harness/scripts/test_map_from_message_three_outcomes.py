#!/usr/bin/env python3
"""Проба: map-from-message.py отсекает КОДОМ, без модели, то, что задачей не бывает.

Карточки pb.replace-per-message-card-trigger (владелец 24.10: «на каждое моё
сообщение карточку создаёт — надо что-то менять… три варианта: не задача, новая
задача, добавить в текущую задачу») и hr.map-from-message-quote-not-task
(28.10: пересланный отчёт с просьбой записан «цитатой нашего же сообщения»).
Замер: .tasks/hr.kartochki-iz-soobshchenij-musor/zamer-trekh-iskhodov.md —
слова-маркеры и «не работа» прибора срочности теряют 5 из 9 дополнений.
Спеку «трёх исходов» владелец упразднил 27.10: «новая» или «в текущую» решает
сессия, а не этот прибор, — таких случаев в пробе нет. Код судит только «не
задача» там, где слов владельца о работе нет по форме сообщения.

══════════════════════════════ КОНТРАКТ ══════════════════════════════════════
`map-from-message.py --сообщение <id> --карта К --инбокс И --линия L`
даёт ровно один исход; спутник — <карта.parent>/память/.tasks-triaged.
 1. Текст пуст — исхода нет, модель не зовётся, код 0 (как сейчас).
 2. БЕЗ модели, строка спутника «<id> не-задача: <причина>», карта не тронута:
    а) слов владельца нет — всё сообщение есть цитата НАШИХ строк
       (tg_out.jsonl рядом с инбоксом) — причина со словом «цитата»;
    б) короткий ответ (≤40 знаков, ≤4 слов, все слова из закрытого списка
       да/нет/ок/давай/… или числа) — причина со словом «короткий».
    Причина называет СВОЙ случай: «цитата» у не-цитаты — ложь в журнале.
 3. Чистая пересылка (forward_origin/forward_from, вид 1793214055) кодом НЕ
    судится: пересланный отчёт бывает поручением, решает модель — она зовётся
    один раз, причины «пересланное» в спутнике нет.
═════════════════════════════════════════════════════════════════════════════

Стенд: свой temp-каталог на случай — карта с комментариями (wip, plan, done
карточки), архив, спутник, инбокс, tg_out.jsonl, журналы; модель — заглушка
HARNESS_SPROSIT_MODEL, каждый вызов оставляет строку в файле-метке (так видно,
звалась ли модель). Живой dev-map.yaml не читается и не пишется.

Дверь: MAP_FROM_MESSAGE_PY — копия map-from-message.py для мутаций (по
умолчанию рядом с пробой). Дверь в никуда — провал, не боевой файл.

Прогон: python3 scripts/test_map_from_message_three_outcomes.py
Код: 0 — все случаи зелёные, 1 — хоть один плохой.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

sys.dont_write_bytecode = True

UNDER_TEST = Path(os.environ.get("MAP_FROM_MESSAGE_PY")
                  or Path(__file__).resolve().parent / "map-from-message.py")

MAP_TEXT = """version: 1
# Комментарий карты: правка кодом обязана его сохранить.
lines:
  - id: core
    name: Ядро
    tasks:
      # открытая задача в работе — сюда ложатся дополнения
      - id: hr.lead-telegram
        status: wip
        title: "Заявки с сайта уходят в Телеграм"
        summary: "бот шлёт заявку в чат"
        checklist:
          - { label: "бот шлёт имя и телефон", done: true }
      - id: hr.export-report
        status: plan
        title: "Выгрузка отчёта"
        summary: "отчёт в CSV"
      - id: hr.closed-thing
        status: done
        title: "Закрытая задача"
        summary: "сделано"
# Хвост карты: тоже комментарий.
"""

# Наш отчёт владельцу; владелец вставляет его назад без своих слов.
OUR_REPORT = ("Ночные ворота прошли, красных строк две.\n"
              "Надо прогнать мутации ещё раз после правки сторожа.\n"
              "Сделаю утром, доложу числами в этот же канал.")

FORWARD_UPDATE = json.dumps({
    "message_id": 1, "from": {"id": 1, "is_bot": False, "first_name": "Владелец"},
    "chat": {"id": 1, "type": "private"}, "date": 1,
    "forward_origin": {"type": "user", "sender_user": {"id": 2, "is_bot": True,
                                                       "first_name": "Чужой бот"}, "date": 1},
    "forward_from": {"id": 2, "is_bot": True, "first_name": "Чужой бот"}, "forward_date": 1,
    "caption": "Отчёт для строителя харнеса: две смены за ночь.\n"
               "Надо исправить сторожа сессий, он поднял второго агента.",
}, ensure_ascii=False)

def answer(outcome: str, tasks=()) -> dict:
    return {"исход": outcome, "задача": "", "задачи": list(tasks), "имена": [],
            "слои": [], "указания": [], "вопросы": [], "ответы": []}


# Модель, позванная там, где код обязан решить сам, завела бы карточку.
NEW_EXPORT = dict(answer("новая", ["экспорт отчёта в панель"]),
                  имена=["panel-report-export"], слои=["harness"])

# Ожидание каждого случая записано абсолютом:
#   model — зовётся ли модель; cards — на сколько выросло число карточек;
#   triaged — подстрока причины в строке спутника (None — строки быть не должно).
CASES = (
    dict(name="пусто: исхода нет, модели нет", text="   \n ", stub=NEW_EXPORT,
         model=False, cards=0, triaged=None),
    dict(name="цитата нашего отчёта без своих слов: не-задача «цитата»", text=OUR_REPORT,
         stub=NEW_EXPORT, model=False, cards=0, triaged="цитат"),
    dict(name="короткий ответ «ок»: не-задача «короткий», не «цитата»", text="ок",
         stub=NEW_EXPORT, model=False, cards=0, triaged="коротк"),
    dict(name="короткий ответ «да, давай»: не-задача «короткий», не «цитата»",
         text="да, давай", stub=NEW_EXPORT, model=False, cards=0, triaged="коротк"),
    dict(name="чистая пересылка отчёта: код не судит, решает модель",
         text=FORWARD_UPDATE, stub=answer("не-задача"), model=True, cards=0,
         triaged="модел"),
    # Обратные: короткая настоящая просьба — не отсев. Ловит «чини»/«сделай»
    # в списке и all(...) -> any(...).
    *(dict(name=f"короткая просьба {text!r}: решает модель, не отсев «короткий»",
           text=text, stub=answer("не-задача"), model=True, cards=0, triaged="модел")
      for text in ("чини", "сделай 3", "да, сделай экспорт", "да\n\nдеплой", "нет, не надо")),
    # Границы: 4 слова из списка и ровно 40 знаков — отсев; 5 слов или 41 знак — модель.
    dict(name="4 слова из списка, ровно 40 знаков: не-задача «короткий»",
         text="согласна согласна согласна пожалуйста!!!", stub=NEW_EXPORT, model=False,
         cards=0, triaged="коротк"),
    dict(name="5 слов из списка: решает модель", text="да, хорошо, принято, спасибо, ок",
         stub=answer("не-задача"), model=True, cards=0, triaged="модел"),
    dict(name="4 слова из списка, 41 знак: решает модель",
         text="согласна согласна согласна пожалуйста!!!!", stub=answer("не-задача"),
         model=True, cards=0, triaged="модел"),
    # Решение смены (З1): число — выбор владельца из наших вариантов, это работа.
    *(dict(name=f"выбор варианта {text!r}: решает модель, не отсев «короткий»",
           text=text, stub=answer("не-задача"), model=True, cards=0, triaged="модел")
      for text in ("2", "давай 2")),
    # Б3: одна короткая строка, совпавшая с нашей («Ок»), — не цитата: у гейта
    # цитата — подряд не меньше 2 длинных строк. Просьба после неё — к модели.
    dict(name="«Ок» совпало с нашей строкой, дальше просьба: не цитата, решает модель",
         text="Ок\nа ещё глянь, почему бот молчит по ночам", ours=("Ок",),
         stub=answer("не-задача"), model=True, cards=0, triaged="модел"),
)

checked = failed = 0


def case(name: str, ok: bool, detail: str = "") -> None:
    global checked, failed
    checked += 1
    failed += 0 if ok else 1
    print(("  ок    " if ok else "  ПЛОХО ") + name + ("" if ok or not detail else f": {detail}"))


def card_map(text: str) -> dict:
    data = yaml.safe_load(text) or {}
    return {t["id"]: t for line in data.get("lines") or [] for t in line.get("tasks") or []}


def comments(text: str) -> list:
    return [line.strip() for line in text.splitlines() if line.strip().startswith("#")]


def build_stand(stand: Path, message_id: str, spec: dict) -> dict:
    done = stand / "inbox" / "обработано"
    done.mkdir(parents=True)
    (done / f"{message_id}.txt").write_text(spec["text"], encoding="utf-8")
    (stand / "docs").mkdir()
    (stand / "docs" / "dev-map-archive.yaml").write_text("tasks: []\n", encoding="utf-8")
    (stand / "память").mkdir()
    log = stand / "log"
    log.mkdir()
    # Наш отчёт ушёл владельцу за 10 минут до его сообщения.
    sent = datetime.datetime.fromtimestamp(float(message_id) - 600).isoformat()
    (stand / "tg_out.jsonl").write_text(
        "".join(json.dumps({"ts": sent, "text": t}, ensure_ascii=False) + "\n"
                for t in (OUR_REPORT, *spec.get("ours", ()))), encoding="utf-8")
    map_file = stand / "dev-map.yaml"
    map_file.write_text(MAP_TEXT, encoding="utf-8")
    mark = stand / "model-calls"
    stub = stand / "stub-model.py"
    reply = ("import sys\nprint('модель недоступна', file=sys.stderr)\nsys.exit(2)\n"
             if spec["stub"] is None else
             f"print({json.dumps(spec['stub'], ensure_ascii=False)!r})\n")
    stub.write_text(f"import pathlib\nwith open({str(mark)!r}, 'a') as f:\n    f.write('1\\n')\n"
                    + reply, encoding="utf-8")
    env = dict(os.environ, LOG_DIR=str(log), HARNESS_LOG_DIR=str(log),
               HARNESS_EVENTS_STATE=str(log / ".events"),
               HEARTBEAT_DIR=str(log / "heartbeat"),
               HARNESS_PANEL_STATE=str(log / "panel-state"),
               HARNESS_SPROSIT_MODEL=str(stub), HARNESS_PLANS_DIR=str(stand / "планы"),
               PYTHONDONTWRITEBYTECODE="1")
    env.pop("HARNESS_DEVMAP", None)
    return {"map": map_file, "inbox": stand / "inbox", "env": env, "mark": mark,
            "triaged": stand / "память" / ".tasks-triaged"}


def triaged_line(path: Path, message_id: str):
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(rf"{re.escape(message_id)}(?!\d)", line):
            return line
    return None


def judge(spec: dict, tmp: Path, number: int) -> None:
    name = spec["name"]
    message_id = f"{int(time.time()) - 3600 + number}.{number:09d}"
    stand = tmp / f"случай-{number}"
    stand.mkdir()
    s = build_stand(stand, message_id, spec)
    before = s["map"].read_text(encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(UNDER_TEST), "--сообщение", message_id, "--карта", str(s["map"]),
         "--инбокс", str(s["inbox"]), "--линия", "core"],
        capture_output=True, text=True, env=s["env"], cwd=stand, timeout=180)
    tail = (run.stdout + run.stderr).strip().splitlines()[-3:]
    after = s["map"].read_text(encoding="utf-8")
    calls = len(s["mark"].read_text().splitlines()) if s["mark"].exists() else 0
    case(f"{name}: код 0", run.returncode == 0, f"код {run.returncode}, хвост {tail}")
    case(f"{name}: модель {'зовётся один раз' if spec['model'] else 'не зовётся'}",
         calls == (1 if spec["model"] else 0), f"вызовов {calls}, хвост {tail}")
    try:
        cards_before, cards_after = card_map(before), card_map(after)
    except yaml.YAMLError as exc:
        case(f"{name}: карта после правки читается yaml.safe_load", False, str(exc)[:200])
        return
    new_ids = sorted(set(cards_after) - set(cards_before))
    case(f"{name}: карточек +{spec['cards']}, прежние на месте",
         len(cards_after) == len(cards_before) + spec["cards"]
         and set(cards_before) <= set(cards_after), f"новые {new_ids}, хвост {tail}")
    for new_id in new_ids:
        case(f"{name}: новая {new_id} несёт источник inbox:{message_id}",
             str(cards_after[new_id].get("источник")) == f"inbox:{message_id}",
             f"источник {cards_after[new_id].get('источник')!r}")
    case(f"{name}: комментарии карты целы", comments(before) == comments(after),
         f"стало {comments(after)}")
    line = triaged_line(s["triaged"], message_id)
    if spec["triaged"] is None:
        case(f"{name}: строки спутника нет", line is None, f"строка {line!r}")
    else:
        case(f"{name}: строка спутника «не-задача» с причиной «{spec['triaged']}…»",
             line is not None and re.search(r"не[- ]задача", line) is not None
             and spec["triaged"] in line.lower(), f"строка {line!r}, хвост {tail}")
        for foreign in ("цитат", "пересла", "коротк"):
            if spec["triaged"] != foreign:
                case(f"{name}: причина не выдаёт себя за «{foreign}…»",
                     line is None or foreign not in line.lower(), f"строка {line!r}")
    for card_id in cards_before:
        case(f"{name}: {card_id} не тронута", cards_before[card_id] == cards_after.get(card_id))
    has_outcome = f"inbox:{message_id}" in after or line is not None
    case(f"{name}: исход виден гейту — {'есть' if spec['text'].strip() else 'не нужен'}",
         has_outcome == bool(spec["text"].strip()), f"карта/спутник: {has_outcome}")


def main() -> int:
    if not UNDER_TEST.is_file():
        case(f"механизм по двери MAP_FROM_MESSAGE_PY ({UNDER_TEST})", False,
             "файла нет — боевой файл вместо него не беру")
        print(f"SELFTEST: КРАСНЫЙ ({checked} проверок, неудач {failed})")
        return 1
    red_cases = []
    with tempfile.TemporaryDirectory(prefix="map-outcome-three-") as raw:
        tmp = Path(raw)
        for number, spec in enumerate(CASES, 1):
            print(spec["name"])
            was = failed
            try:
                judge(spec, tmp, number)
            except Exception as exc:  # noqa: BLE001 — падение случая = провал, не падение пробы
                case(f"{spec['name']}: исключение", False, f"{type(exc).__name__}: {exc}")
            if failed > was:
                red_cases.append(spec["name"])
    print(f"SELFTEST: {'зелёный' if not failed else 'КРАСНЫЙ'} ({checked} проверок, "
          f"{len(CASES)} случаев, красных случаев {len(red_cases)}, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
