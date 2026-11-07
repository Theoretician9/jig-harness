#!/usr/bin/env python3
"""Проба: map-from-message.py ставит новой карточке `глава:`, когда владелец
ЯВНО назвал задачу с планом (спека правила-вложенности-кодом §5.2, ревью Н-6).

══════════════════════════════ КОНТРАКТ ══════════════════════════════════════
При заведении карточки (не --сухой) код ищет в словах владельца id карточек
карты — целым словом (соседи не из [\\w.-]: «hr.big-plan-x» не называет
hr.big-plan). Для первой названной задачи, у которой есть план:
  • задача — владелец плана (блок `план:` в <планы>/<план>.md, поле владелец)
    и сама без `глава:` — новой карточке `глава: <план>/<первая глава верхнего
    уровня плана>`;
  • у задачи есть `глава: <план>/<глава>[/<подглава>…]` и такая глава есть в
    плане — новой карточке корневая глава её пути: `глава: <план>/<глава>`.
Строка `глава:` — в тексте той же вставки под dev-map.lock (комментарии карты
целы, число карточек +1). Событие: events.emit в $LOG_DIR/вложенность.jsonl
(journal «вложенность»), event nesting.chapter, src scripts/map-from-message.py,
outcome ok, data {задача: id новой карточки, глава, источник: "сообщение"}.
Задача не названа или у названной нет плана — поля нет, события нет.
Каталог планов: env HARNESS_PLANS_DIR, иначе <PROJECT_DIR или корень дерева>/docs/plans.
═════════════════════════════════════════════════════════════════════════════

Стенд: свой temp-каталог — карта с комментариями, планы вне дерева (только
через HARNESS_PLANS_DIR), очередь, журналы; модель — заглушка
HARNESS_SPROSIT_MODEL (как у test_map_from_message_not_work.py). Механизм
зовётся как в бою: `--сообщение <id> --карта <карта> --инбокс <инбокс>
--линия core`.

Дверь: MAP_FROM_MESSAGE_PY — копия map-from-message.py для мутаций (по
умолчанию рядом с пробой). Дверь в никуда — провал, не боевой файл.

Прогон: python3 scripts/test_map_from_message_chapter.py
Код: 0 — все случаи зелёные, 1 — хоть один плохой.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.dont_write_bytecode = True

UNDER_TEST = Path(os.environ.get("MAP_FROM_MESSAGE_PY")
                  or Path(__file__).resolve().parent / "map-from-message.py")
SRC = "scripts/map-from-message.py"
MAP_COMMENT = "# Комментарий карты: правка кодом обязана его сохранить."

MAP_TEXT = f"""version: 1
{MAP_COMMENT}
queue: []
layers:
  - {{ id: infra, label: И, color: "#1" }}
lines:
  - id: core
    layer: infra
    name: Ядро
    tasks:
      # владелец плана big-plan
      - id: hr.big-plan
        status: wip
        title: "Большая работа с планом"
        summary: "владелец плана"
      # задача в подглаве плана
      - id: hr.in-chapter
        status: plan
        title: "Задача главы"
        summary: "лежит в подглаве"
        глава: big-plan/core/sub
      - id: hr.lonely
        status: plan
        title: "Задача без плана"
        summary: "плана нет"
# Хвост карты: тоже комментарий.
"""

PLAN_TEXT = """# План big-plan

```yaml
план:
  id: big-plan
  владелец: hr.big-plan
  цель: "проба"
  главы:
    - id: core
      название: "Ядро"
      охват: []
      главы:
        - {id: sub, название: "Подглава", охват: []}
    - id: extra
      название: "Второе"
      охват: []
```
"""

# (имя случая, текст владельца, ждём глава: или None)
CASES = (
    ("названа владелец плана", "надо доделать hr.big-plan: добавить экспорт отчёта",
     "big-plan/core"),
    ("названа задача в подглаве", "надо по hr.in-chapter ещё добавить экспорт отчёта",
     "big-plan/core"),
    ("названа задача без плана", "надо к hr.lonely добавить экспорт отчёта", None),
    ("ничего не названо", "надо добавить экспорт отчёта в панель", None),
    ("похожее имя не называет задачу", "надо глянуть hr.big-plan-x и добавить экспорт отчёта",
     None),
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


def build_stand(stand: Path, message_id: str, text: str) -> dict:
    done = stand / "inbox" / "обработано"
    done.mkdir(parents=True)
    (done / f"{message_id}.txt").write_text(text, encoding="utf-8")
    (stand / "docs").mkdir()
    (stand / "docs" / "dev-map-archive.yaml").write_text("tasks: []\n", encoding="utf-8")
    (stand / "память").mkdir()
    (stand / "log").mkdir()
    plans = stand / "планы-вне-дерева"
    plans.mkdir()
    (plans / "big-plan.md").write_text(PLAN_TEXT, encoding="utf-8")
    map_file = stand / "dev-map.yaml"
    map_file.write_text(MAP_TEXT, encoding="utf-8")
    stub = stand / "stub-model.py"
    stub.write_text(
        "import json\n"
        "print(json.dumps({'задачи': ['добавить экспорт отчёта'], 'имена': ['report-export'],"
        " 'слои': ['harness'], 'указания': [], 'вопросы': []}, ensure_ascii=False))\n",
        encoding="utf-8")
    log = stand / "log"
    env = dict(os.environ, LOG_DIR=str(log), HARNESS_LOG_DIR=str(log),
               HARNESS_EVENTS_STATE=str(log / ".events"),
               HEARTBEAT_DIR=str(log / "heartbeat"),
               HARNESS_PANEL_STATE=str(log / "panel-state"),
               HARNESS_SPROSIT_MODEL=str(stub), HARNESS_PLANS_DIR=str(plans),
               PYTHONDONTWRITEBYTECODE="1")
    env.pop("HARNESS_DEVMAP", None)
    return {"map": map_file, "inbox": stand / "inbox", "env": env, "log": log}


def chapter_rows(log: Path) -> list:
    path = log / "вложенность.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event") == "nesting.chapter":
            rows.append(row)
    return rows


def judge(name: str, text: str, expected, tmp: Path, number: int) -> None:
    message_id = f"77{number}.5"
    stand = tmp / f"случай-{number}"
    stand.mkdir()
    s = build_stand(stand, message_id, text)
    before = s["map"].read_text(encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(UNDER_TEST), "--сообщение", message_id, "--карта", str(s["map"]),
         "--инбокс", str(s["inbox"]), "--линия", "core"],
        capture_output=True, text=True, env=s["env"], cwd=stand, timeout=600)
    tail = (run.stdout + run.stderr).strip().splitlines()[-3:]
    after = s["map"].read_text(encoding="utf-8")
    try:
        cards_before, cards_after = card_map(before), card_map(after)
    except yaml.YAMLError as exc:
        case(f"{name}: карта после правки читается yaml.safe_load", False, str(exc)[:200])
        return
    new_ids = sorted(set(cards_after) - set(cards_before))
    case(f"{name}: код 0, карточек +1 (одна новая), прежние на месте",
         run.returncode == 0 and len(new_ids) == 1 and len(cards_after) == len(cards_before) + 1
         and set(cards_before) <= set(cards_after),
         f"код {run.returncode}, новые {new_ids}, хвост {tail}")
    if len(new_ids) != 1:
        return
    new_id = new_ids[0]
    case(f"{name}: комментарии карты целы", comments(before) == comments(after),
         f"было {comments(before)}, стало {comments(after)}")
    case(f"{name}: прежние карточки не тронуты",
         all(cards_before[i] == cards_after[i] for i in cards_before))
    got = cards_after[new_id].get("глава")
    case(f"{name}: глава: новой карточки — {expected}", got == expected, f"вышло {got!r}")
    rows = chapter_rows(s["log"])
    want = ([(SRC, "ok", new_id, expected, "сообщение")] if expected else [])
    seen = [(r.get("src"), r.get("outcome"), (r.get("data") or {}).get("задача"),
             (r.get("data") or {}).get("глава"), (r.get("data") or {}).get("источник"))
            for r in rows]
    case(f"{name}: событие nesting.chapter — {'одно, источник сообщение' if expected else 'нет'}",
         seen == want, f"вышло {seen}")
    lines = [line for line in after.splitlines() if re.match(r"\s+глава:", line)]
    case(f"{name}: строк «глава:» в карте {1 + bool(expected)}", len(lines) == 1 + bool(expected),
         f"вышло {lines}")


def main() -> int:
    if not UNDER_TEST.is_file():
        case(f"механизм по двери MAP_FROM_MESSAGE_PY ({UNDER_TEST})", False,
             "файла нет — боевой файл вместо него не беру")
        print(f"SELFTEST: КРАСНЫЙ ({checked} проверок, неудач {failed})")
        return 1
    with tempfile.TemporaryDirectory(prefix="map-chapter-") as raw:
        tmp = Path(raw)
        for number, (name, text, expected) in enumerate(CASES, 1):
            print(name)
            try:
                judge(name, text, expected, tmp, number)
            except Exception as exc:  # noqa: BLE001 — падение случая = провал, не падение пробы
                case(f"{name}: исключение", False, f"{type(exc).__name__}: {exc}")
    print(f"SELFTEST: {'зелёный' if not failed else 'КРАСНЫЙ'} ({checked} проверок, "
          f"{len(CASES)} случаев, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
