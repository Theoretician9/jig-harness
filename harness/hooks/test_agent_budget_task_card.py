#!/usr/bin/env python3
"""Проба: задача запуска помощника — id карточки из метки «Часть:» (Ч1 hr.speed-modes).

Случай: спека .tasks/hr.speed-modes/spec.md, 10.3 п.11 и 9. В бою две wip
дают task «-» у каждого запуска: счёт потолка и «задач в работе» не знают,
чей запуск. Метка «Часть:» уже пишется в data.часть, но задачей не служит.

══════════════════ КОНТРАКТ agent_budget_guard.py (Ч1) ══════════════════

* Задача запуска = launch_plan_lib.mark_task (последний сегмент пути метки),
  если такой id ЕСТЬ в карте (наличие — по id, не по уровню: карточка без
  `глубина` присутствует). Уровень и потолок — от этой задачи.
* Метка есть, id в карте нет — задача по-старому (current_task() или «-»),
  строка agent.unknown-card с outcome ok; решение запуска не меняется.
* Запуск смены (без agent_id) без метки при непустом SPEED_MODE в
  harness.conf (HARNESS_CONF) — строка agent.unlabeled; без SPEED_MODE — нет.
* Помощник без метки — задача по-старому (current_task() или «-»), agent.unlabeled
  не пишет. Задачу родителя хук не ищет: это работа судьи Ч2 (спека, 3.2).
* Оба пути judge — отказ по глубине и основной — пишут задачу метки.
* Дверь пишется на current_task(): при двух wip меченую задачу открывает
  только `HARNESS_TASK=<id> … --door` (дверь «-» её не открывает), и текст
  отказа меченой задаче подсказывает именно эту дверь.
* Карточка метки без `глубина` — потолок «без-задачи», не пропуск.
* id сверяется только строковый: метка «Часть: None» не находит узел без id.
* Карта не читается (и битая кодировка) — хук не падает: unknown-card с
  data.почему «карта не читается», решение по-старому.

═════════════════════════════════════════════════════════════════════════

Стенд: свои HARNESS_LOG_DIR, HARNESS_DEVMAP, HARNESS_CONF, pipeline.yaml,
helper-roles.yaml; боевые каталоги не пишутся (сессия пробы ищется в боевом
журнале — ноль строк). Хук — рядом либо BUDGET_GUARD_BIN (дверь мутаций).
Прогон: python3 scripts/hooks/test_agent_budget_task_card.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
GUARD = Path(os.environ.get("BUDGET_GUARD_BIN") or HERE / "agent_budget_guard.py")
SRC = "scripts/hooks/agent_budget_guard.py"
JOURNAL = "вызовы-агентов.jsonl"
REAL_JOURNAL = Path("/var/log/harness") / JOURNAL
SESSION = f"проба-карточка-{os.getpid()}"
UNKNOWN = "agent.unknown-card"
UNLABELED = "agent.unlabeled"
LAUNCH = "agent.launch"

# Сломать запись только сигнальных событий нельзя каталогом или файлом: все
# события хука идут в один журнал. А events.emit сам глушит свои сбои, так что
# except в note() хука живым путём не достигается. Поэтому хук запускается
# через runpy с заранее импортированным events, у которого emit падает на
# названных событиях; agent.launch идёт в настоящий emit.
BROKEN_EMIT = """
import runpy, sys
from pathlib import Path
guard = Path(sys.argv[1]).resolve()
broken = set(sys.argv[2].split(","))
sys.path.insert(0, str(guard.parent.parent.parent / "scripts" / "lib"))
import events
real_emit = events.emit
def emit(src, event, *args, **kwargs):
    if event in broken:
        raise OSError("стенд: запись сигнального события сломана")
    return real_emit(src, event, *args, **kwargs)
events.emit = emit
sys.argv = [str(guard)]
runpy.run_path(str(guard), run_name="__main__")
"""

# Уровни разные, чтобы «уровень по карточке» отличался от чужой карточки и от «без-задачи».
CEILINGS = {"мелкая": {"всего": 5, "ревью": 0},
            "обычная": {"всего": 5, "ревью": 0},
            "без-задачи": {"всего": 8, "ревью": 0}}
TWO_WIP = [("hr.x-card", "wip", "мелкая"), ("hr.w-card", "wip", "обычная"),
           ("hr.y-card", "plan", None)]
ONE_WIP = [("hr.x-card", "plan", "мелкая"), ("hr.w-card", "wip", "обычная")]

checked = failed = 0


def check(name: str, expected, actual) -> None:
    global checked, failed
    checked += 1
    if expected == actual:
        print(f"  ок    {name}")
    else:
        failed += 1
        print(f"  ПЛОХО {name}: ждали «{expected}», вышло «{actual}»")


class Stand:
    def __init__(self, base: Path, cards: list, speed_mode: str | None = "medium"):
        self.base = base
        self.logs = base / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        # Каталог задачи нарочно назван не так, как карточка: так живёт hr.speed-modes.
        (base / ".tasks" / "hr.x").mkdir(parents=True, exist_ok=True)
        self.devmap = base / "dev-map.yaml"
        self.devmap.write_text(yaml.safe_dump({"lines": [{"id": "линия", "items": [
            {"id": tid, "status": status, **({"глубина": {"уровень": level}} if level else {})}
            for tid, status, level in cards]}]}, allow_unicode=True), encoding="utf-8")
        self.conf = base / "harness.conf"
        lines = [f'LOG_DIR="{self.logs}"']
        if speed_mode is not None:
            lines.append(f'SPEED_MODE="{speed_mode}"')
        self.conf.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.pipeline = base / "pipeline.yaml"
        self.pipeline.write_text(yaml.safe_dump({"помощники": CEILINGS}, allow_unicode=True),
                                 encoding="utf-8")
        self.roles = base / "helper-roles.yaml"
        self.roles.write_text(yaml.safe_dump(
            {"роли": {"harness-lead": {"параллельно_максимум": 9}}}, allow_unicode=True),
            encoding="utf-8")
        self.calls = 0
        self.stderr = ""

    def env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in ("HARNESS_TASK", "SPEED_MODE")}
        env.update(HARNESS_LOG_DIR=str(self.logs), LOG_DIR=str(self.logs),
                   HARNESS_DEVMAP=str(self.devmap), HARNESS_CONF=str(self.conf),
                   HARNESS_EVENTS_STATE=str(self.base / "state"),
                   HARNESS_PIPELINE_CONF=str(self.pipeline),
                   HARNESS_HELPER_ROLES=str(self.roles),
                   PYTHONDONTWRITEBYTECODE="1", **extra)
        return env

    def caller(self, agent: str, depth: int, tool_use_id: str = "") -> None:
        """meta.json зовущего помощника: глубина и вызов, которым его запустили."""
        meta = self.base / SESSION / "subagents" / f"agent-{agent}.meta.json"
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({"spawnDepth": depth, "toolUseId": tool_use_id}),
                        encoding="utf-8")

    def launch(self, prompt: str, agent: str = "", broken: tuple = ()) -> tuple[int, dict, list]:
        """(код, строка agent.launch этого запуска, прочие строки хука этого запуска).
        broken — события, запись которых на этом запуске падает исключением."""
        self.calls += 1
        call = f"call-{self.calls}"
        payload = {"tool_name": "Agent", "session_id": SESSION, "tool_use_id": call,
                   "transcript_path": str(self.base / "s.jsonl"),
                   "tool_input": {"description": "исполнитель", "prompt": prompt,
                                  "subagent_type": "harness-worker"}}
        if agent:
            payload["agent_id"] = agent
        before = len(self.rows())
        argv = ([sys.executable, "-c", BROKEN_EMIT, str(GUARD), ",".join(broken)] if broken
                else [sys.executable, str(GUARD)])
        proc = subprocess.run(argv, input=json.dumps(payload),
                              capture_output=True, text=True, env=self.env(),
                              timeout=60)
        code, self.stderr = proc.returncode, proc.stderr
        fresh = self.rows()[before:]
        mine = [r for r in fresh if r.get("event") == LAUNCH
                and (r.get("data") or {}).get("вызов") == call]
        if len(mine) != 1:
            # Код в тексте: упавший хук (1) отличим от молчащего (0).
            raise AssertionError(f"запуск {call}: код {code}, строк agent.launch "
                                 f"{len(mine)}, ждали 1")
        return code, mine[0], [r for r in fresh if r is not mine[0]]

    def door(self, reason: str, **extra) -> int:
        return subprocess.run([sys.executable, str(GUARD), "--door", reason],
                              capture_output=True, text=True, env=self.env(**extra),
                              timeout=60).returncode

    def rows(self) -> list:
        """Строки хука из ВСЕХ журналов стенда: событие может лечь в любой."""
        found = []
        for path in sorted(self.logs.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("src") == SRC:
                    found.append(row)
        return found


def events_of(rows: list, name: str) -> list:
    return [r for r in rows if r.get("event") == name]


def case_card_label(tmp: Path) -> None:
    print("(а) метка с id карточки — задача и уровень карточки")
    s = Stand(tmp / "а", TWO_WIP)
    code, row, other = s.launch("Часть: hr.x-card#1\nПравь.")
    check("task = hr.x-card", "hr.x-card", row.get("task"))
    check("уровень по карточке hr.x-card", "мелкая", (row.get("data") or {}).get("уровень"))
    check("проход: код 0, outcome ok", (0, "ok"), (code, row.get("outcome")))
    check("agent.unknown-card нет", 0, len(events_of(other, UNKNOWN)))


def case_dir_label(tmp: Path) -> None:
    print("(б) метка с именем каталога, а не карточки — agent.unknown-card")
    s = Stand(tmp / "б", TWO_WIP)
    code, row, other = s.launch("Часть: hr.x#1\nПравь.")
    found = events_of(other, UNKNOWN)
    check("agent.unknown-card: одна строка", 1, len(found))
    check("agent.unknown-card: outcome ok", ["ok"], [r.get("outcome") for r in found])
    check("agent.unknown-card называет id метки hr.x", True,
          bool(found) and "hr.x" in json.dumps(found[0], ensure_ascii=False))
    # Хук пишет задачу по-старому; `без-карточки:<id>` считает судья Ч2, не хук.
    check("задача НЕ hr.x: по-старому «-» (две wip)", "-", row.get("task"))
    check("решение не меняется: код 0, outcome ok", (0, "ok"), (code, row.get("outcome")))


def case_unlabeled(tmp: Path) -> None:
    print("(в) запуск без метки: agent.unlabeled только у смены и только при SPEED_MODE")
    s = Stand(tmp / "в-режим", TWO_WIP, speed_mode="medium")
    code, row, other = s.launch("Правь без метки.")
    found = events_of(other, UNLABELED)
    check("смена без метки при SPEED_MODE: agent.unlabeled одна строка", 1, len(found))
    check("agent.unlabeled не отказ: код 0, запуск ok", (0, "ok"), (code, row.get("outcome")))

    for name, mode in (("ключа нет", None), ("ключ пуст", "")):
        s = Stand(tmp / f"в-{name}", TWO_WIP, speed_mode=mode)
        _, _, other = s.launch("Правь без метки.")
        check(f"SPEED_MODE {name}: agent.unlabeled нет", 0, len(events_of(other, UNLABELED)))

    s = Stand(tmp / "в-помощник", TWO_WIP, speed_mode="medium")
    _, parent, _ = s.launch("Часть: hr.x-card#1\nВеди.")
    s.caller("aParent", 1, (parent.get("data") or {}).get("вызов") or "")
    # Задача помощника без метки — по-старому (current_task() или «-»); задачу
    # родителя хук не ищет (судья Ч2), поэтому здесь судим только то, что меняет Ч1.
    code, row, other = s.launch("Правь без метки.", agent="aParent")
    check("помощник без метки с родителем: agent.unlabeled нет", 0,
          len(events_of(other, UNLABELED)))
    check("помощник без метки: проход, код 0, ok", (0, "ok"), (code, row.get("outcome")))


def case_card_without_depth(tmp: Path) -> None:
    print("(г) карточка без `глубина` — присутствует, наличие не по уровню")
    s = Stand(tmp / "г", TWO_WIP)
    _, row, other = s.launch("Часть: hr.y-card#1\nПравь.")
    check("task = hr.y-card", "hr.y-card", row.get("task"))
    check("agent.unknown-card нет", 0, len(events_of(other, UNKNOWN)))
    check("hr.y-card: outcome ok, не skipped", "ok", row.get("outcome"))


def case_one_wip_unknown(tmp: Path) -> None:
    print("(д) одна wip + неизвестный id — задача current_task, agent.unknown-card")
    s = Stand(tmp / "д", ONE_WIP)
    code, row, other = s.launch("Часть: hr.x#1\nПравь.")
    check("задача = единственная wip hr.w-card", "hr.w-card", row.get("task"))
    check("уровень по hr.w-card", "обычная", (row.get("data") or {}).get("уровень"))
    check("agent.unknown-card одна строка", 1, len(events_of(other, UNKNOWN)))
    check("проход: код 0", 0, code)
    _, row, _ = s.launch("Часть: hr.x-card#2\nПравь.")
    check("метка plan-карточки при одной wip старше current_task: task = hr.x-card",
          "hr.x-card", row.get("task"))


def case_depth_path(tmp: Path) -> None:
    print("(е) второй путь judge: отказ по глубине пишет задачу метки")
    s = Stand(tmp / "е", TWO_WIP)
    s.caller("aDeep", 2)
    code, row, _ = s.launch("Часть: hr.x-card#3\nПравь.", agent="aDeep")
    check("глубина 3: отказ, код 2, refused", (2, "refused"), (code, row.get("outcome")))
    check("отказ по глубине: task = hr.x-card", "hr.x-card", row.get("task"))
    _, row, _ = s.launch("Часть: hr.x#3\nПравь.", agent="aDeep")
    check("отказ по глубине с id не из карты: task «-», не hr.x", "-", row.get("task"))


def case_door(tmp: Path) -> None:
    print("(ж) дверь при меченой задаче")
    s = Stand(tmp / "ж", TWO_WIP)
    for n in range(5):
        s.launch(f"Часть: hr.x-card#{n}\nПравь.")
    code, row, _ = s.launch("Часть: hr.x-card#6\nПравь.")
    check("6-й запуск hr.x-card: отказ по потолку «мелкая» 5", (2, "refused", "hr.x-card"),
          (code, row.get("outcome"), row.get("task")))
    check("дверь без HARNESS_TASK открылась (на «-»)", 0, s.door("причина нужна для пробы двери"))
    code, row, _ = s.launch("Часть: hr.x-card#7\nПравь.")
    check("дверь «-» меченую задачу не открывает (дверь пишется на current_task)",
          (2, "refused"), (code, row.get("outcome")))
    check("дверь с HARNESS_TASK=hr.x-card открылась", 0,
          s.door("причина нужна для пробы двери", HARNESS_TASK="hr.x-card"))
    code, row, _ = s.launch("Часть: hr.x-card#8\nПравь.")
    check("дверь hr.x-card: проход, ok, data.дверь", (0, "ok", True),
          (code, row.get("outcome"), bool((row.get("data") or {}).get("дверь"))))


def case_signal_write_fails(tmp: Path) -> None:
    print("(ж2) сбой записи agent.unknown-card и agent.unlabeled решения не меняет")
    s = Stand(tmp / "ж2", TWO_WIP, speed_mode="medium")
    for prompt, event in (("Часть: hr.x#1\nПравь.", UNKNOWN), ("Правь без метки.", UNLABELED)):
        code, row, other = s.launch(prompt, broken=(UNKNOWN, UNLABELED))
        # Строки события нет — значит подмена сработала и сбой записи настоящий.
        check(f"{event}: запись сломана, строки нет", 0, len(events_of(other, event)))
        check(f"{event} не пишется: код 0, agent.launch ok", (0, "ok"),
              (code, row.get("outcome")))


def case_ceiling_by_label(tmp: Path) -> None:
    print("(з) счёт потолка по задаче метки, а не «без-задачи» 8 на сессию")
    s = Stand(tmp / "з", TWO_WIP)
    results = [s.launch(f"Часть: {card}#{n}\nПравь.")
               for n, card in enumerate(["hr.x-card"] * 5 + ["hr.w-card"] * 4)]
    check("9 запусков под двумя метками: все проходят", [(0, "ok")] * 9,
          [(code, row.get("outcome")) for code, row, _ in results])
    check("задачи строк — из меток", ["hr.x-card"] * 5 + ["hr.w-card"] * 4,
          [row.get("task") for _, row, _ in results])
    code, row, _ = s.launch("Часть: hr.x-card#9\nПравь.")
    check("6-й под hr.x-card: отказ по его потолку", (2, "refused", "hr.x-card"),
          (code, row.get("outcome"), row.get("task")))


def case_label_none(tmp: Path) -> None:
    print("(и1) метка «Часть: None» не совпадает с узлом без id")
    s = Stand(tmp / "и1", TWO_WIP)
    # У корня карты и у линии без id str(None) == "None": сверка не по строке
    # нашла бы корень задачей.
    code, row, other = s.launch("Часть: None#1\nПравь.")
    check("agent.unknown-card: одна строка", 1, len(events_of(other, UNKNOWN)))
    check("задача не «None»: по-старому «-»", "-", row.get("task"))
    check("решение не меняется: код 0, ok", (0, "ok"), (code, row.get("outcome")))


def case_unreadable_map(tmp: Path) -> None:
    print("(и2) карта с битыми байтами: отказ по глубине, хук не падает")
    s = Stand(tmp / "и2", TWO_WIP)
    # Байты не UTF-8 в комментарии: YAML цел, ломается только декодирование.
    s.devmap.write_bytes(s.devmap.read_bytes() + b"# \xff\xfe\xfa\n")
    s.caller("aDeep", 2)
    try:
        code, row, other = s.launch("Часть: hr.x-card#1\nПравь.", agent="aDeep")
    except AssertionError as exc:
        check("глубина 3 при битой карте: строка agent.launch есть", "есть", str(exc))
        return
    check("глубина 3: отказ, код 2 (не 1 — падение), refused", (2, "refused"),
          (code, row.get("outcome")))
    found = events_of(other, UNKNOWN)
    check("agent.unknown-card: одна строка", 1, len(found))
    check("agent.unknown-card: data.почему «карта не читается»", ["карта не читается"],
          [(r.get("data") or {}).get("почему") for r in found])


def case_card_without_depth_ceiling(tmp: Path) -> None:
    print("(и3) карточка без `глубина`: потолок «без-задачи», не пропуск")
    s = Stand(tmp / "и3", TWO_WIP)
    limit = int(yaml.safe_load(s.pipeline.read_text(encoding="utf-8"))
                ["помощники"]["без-задачи"]["всего"])
    results = [s.launch(f"Часть: hr.y-card#{n}\nПравь.") for n in range(limit + 1)]
    outcomes = [(code, row.get("outcome")) for code, row, _ in results]
    check(f"{limit} запусков hr.y-card проходят, {limit + 1}-й — отказ",
          [(0, "ok")] * limit + [(2, "refused")], outcomes)
    check("ни одного skipped", 0, sum(o == "skipped" for _, o in outcomes))
    check("задача отказа — hr.y-card", "hr.y-card", results[-1][1].get("task"))


def case_door_hint(tmp: Path) -> None:
    print("(и4) подсказка двери в отказе: HARNESS_TASK=<задача> у меченой, прежняя у «-»")
    s = Stand(tmp / "и4", TWO_WIP)
    for n in range(CEILINGS["мелкая"]["всего"]):
        s.launch(f"Часть: hr.x-card#{n}\nПравь.")
    code, _, _ = s.launch("Часть: hr.x-card#9\nПравь.")
    door_line = next((ln for ln in s.stderr.splitlines() if "--door" in ln), "")
    check("отказ меченой задаче: код 2", 2, code)
    check("подсказка: HARNESS_TASK=hr.x-card … --door", True,
          "HARNESS_TASK=hr.x-card" in door_line and "--door" in door_line)

    s = Stand(tmp / "и4-без", TWO_WIP, speed_mode=None)
    for n in range(CEILINGS["без-задачи"]["всего"]):
        s.launch("Правь без метки.")
    code, row, _ = s.launch("Правь без метки.")
    door_line = next((ln for ln in s.stderr.splitlines() if "--door" in ln), "")
    check("отказ задаче «-»: код 2, task «-»", (2, "-"), (code, row.get("task")))
    check("подсказка «-» прежняя, без HARNESS_TASK",
          "  python3 scripts/hooks/agent_budget_guard.py --door 'почему нужен ещё один'",
          door_line)


CASES = (case_card_label, case_dir_label, case_unlabeled, case_card_without_depth,
         case_one_wip_unknown, case_depth_path, case_door, case_signal_write_fails,
         case_ceiling_by_label, case_label_none, case_unreadable_map,
         case_card_without_depth_ceiling, case_door_hint)


def real_rows_of_session() -> int:
    try:
        text = REAL_JOURNAL.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(SESSION in line for line in text.splitlines())


def main() -> int:
    global checked, failed
    if not GUARD.exists():
        print(f"нет хука: {GUARD}")
        print("SELFTEST: КРАСНЫЙ (механизма нет, случаи не запускались)")
        return 1
    with tempfile.TemporaryDirectory(prefix="test-budget-card-") as raw:
        tmp = Path(raw)
        for case in CASES:
            try:
                case(tmp)
            except Exception as exc:  # noqa: BLE001 — падение случая = провал, не падение пробы
                checked += 1
                failed += 1
                print(f"  ПЛОХО {case.__name__}: исключение {type(exc).__name__}: {exc}")
    print("изоляция")
    check("в боевом журнале строк сессии пробы нет", 0, real_rows_of_session())
    print(f"SELFTEST: {'зелёный' if not failed else 'КРАСНЫЙ'} ({checked} проверок, "
          f"{len(CASES)} случаев, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
