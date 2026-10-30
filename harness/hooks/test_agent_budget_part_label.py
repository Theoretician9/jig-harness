#!/usr/bin/env python3
"""Проба: хук бюджета помощников пишет метку «Часть:» в строку agent.launch.

Случай: ревью hr.nesting-creation-rules Н-1. У agent.launch в бою task всегда
«-», ид ребёнка в строке нет — задачу запуска знает только метка в prompt.
План запуска (scripts/launch-plan.py, контракт «живые» в docstring
scripts/test_launch_plan.py) считает задачи в работе по data.часть.

══════════════════ КОНТРАКТ agent_budget_guard.py (добавка) ══════════════════

Каждая строка agent.launch (ok, refused, skipped) несёт data.часть — первая
метка prompt по регулярке Часть:\\s*(?P<path>[^\\s#]+)(?:#(?P<n>\\d+))?,
записанная как «Часть: <path>[#<n>]» (один пробел, без хвоста строки).
Метки нет — ключа data.часть нет. Прочие поля строки (ревью, описание,
уровень, управляющий, почему) не меняются.

═════════════════════════════════════════════════════════════════════════════

Стенд: свой LOG_DIR, своя карта, свой pipeline.yaml (как у
test_agent_budget_guard.py). Механизм — рядом либо HARNESS_AGENT_BUDGET_GUARD.
Прогон: python3 scripts/hooks/test_agent_budget_part_label.py
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
GUARD = Path(os.environ.get("HARNESS_AGENT_BUDGET_GUARD") or HERE / "agent_budget_guard.py")
SRC = "scripts/hooks/agent_budget_guard.py"
JOURNAL = "вызовы-агентов.jsonl"

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
    def __init__(self, base: Path, ceilings: dict):
        self.base = base
        self.logs = base / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.devmap = base / "dev-map.yaml"
        self.devmap.write_text(yaml.safe_dump({"lines": []}), encoding="utf-8")
        self.pipeline = base / "pipeline.yaml"
        self.pipeline.write_text(yaml.safe_dump({"помощники": ceilings}, allow_unicode=True),
                                 encoding="utf-8")
        self.roles = base / "helper-roles.yaml"
        self.roles.write_text(yaml.safe_dump(
            {"роли": {"harness-lead": {"параллельно_максимум": 9}}}, allow_unicode=True),
            encoding="utf-8")

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k != "HARNESS_TASK"}
        env.update(HARNESS_LOG_DIR=str(self.logs), LOG_DIR=str(self.logs),
                   HARNESS_DEVMAP=str(self.devmap),
                   HARNESS_EVENTS_STATE=str(self.base / "state"),
                   HARNESS_PIPELINE_CONF=str(self.pipeline),
                   HARNESS_HELPER_ROLES=str(self.roles),
                   PYTHONDONTWRITEBYTECODE="1")
        return env

    def launch(self, prompt: str, lead: str | None = None) -> int:
        payload = {"tool_name": "Agent", "session_id": "проба",
                   "tool_input": {"description": "исполнитель", "prompt": prompt,
                                  "subagent_type": "harness-worker"}}
        if lead:
            payload.update(agent_id=lead, agent_type="harness-lead",
                           transcript_path=str(self.base / "s.jsonl"))
        return subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload),
                              capture_output=True, text=True, env=self.env(),
                              timeout=60).returncode

    def last(self) -> dict:
        path = self.logs / JOURNAL
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()
                if x.strip()] if path.exists() else []
        rows = [r for r in rows if r.get("src") == SRC and r.get("event") == "agent.launch"]
        return rows[-1] if rows else {}


def case_label(tmp: Path) -> None:
    print("метка Часть: в data.часть")
    s = Stand(tmp / "метка", {"без-задачи": {"всего": 0, "ревью": 0},
                              "управляющий": {"всего": 0, "ревью": 0}})
    s.launch("Часть: P/c/A#1\nПравь файл.")
    row = s.last()
    check("смена: data.часть «Часть: P/c/A#1»", "Часть: P/c/A#1", (row.get("data") or {}).get("часть"))
    check("смена: task по-прежнему «-», прочие поля на месте", ("-", False, "исполнитель", "без-задачи"),
          (row.get("task"), (row.get("data") or {}).get("ревью"),
           (row.get("data") or {}).get("описание"), (row.get("data") or {}).get("уровень")))

    s.launch("Работа.\nЧасть:   hr.x/probes/hr.x#12   и ещё текст\nЧасть: Q#9")
    check("метка в середине prompt, лишние пробелы — первая метка, один пробел",
          "Часть: hr.x/probes/hr.x#12", (s.last().get("data") or {}).get("часть"))

    s.launch("Часть: P/c/A\nВеди задачу.")
    check("метка без #n (запуск управляющего) — «Часть: P/c/A»", "Часть: P/c/A",
          (s.last().get("data") or {}).get("часть"))

    s.launch("Часть: A#2\nПравь.", lead="aL1")
    data = s.last().get("data") or {}
    check("управляющий: data.часть и data.управляющий", ("Часть: A#2", "aL1"),
          (data.get("часть"), data.get("управляющий")))

    s.launch("Без метки, ревью спеки.")
    check("без метки — ключа data.часть нет", False, "часть" in (s.last().get("data") or {}))


def case_other_outcomes(tmp: Path) -> None:
    print("метка и в refused, и в skipped")
    s = Stand(tmp / "потолок", {"без-задачи": {"всего": 1, "ревью": 0}})
    s.launch("Часть: B#1\nПравь.")
    code = s.launch("Часть: B#2\nПравь.")
    row = s.last()
    check("сверх потолка: код 2, строка refused с data.часть", (2, "refused", "Часть: B#2"),
          (code, row.get("outcome"), (row.get("data") or {}).get("часть")))

    s = Stand(tmp / "без-потолка", {})
    s.launch("Часть: C#1\nПравь.")
    row = s.last()
    check("потолок не определён: строка skipped с data.часть", ("skipped", "Часть: C#1"),
          (row.get("outcome"), (row.get("data") or {}).get("часть")))


CASES = (case_label, case_other_outcomes)


def main() -> int:
    global checked, failed
    if not GUARD.exists():
        print(f"нет agent_budget_guard.py: {GUARD}")
        print("SELFTEST: КРАСНЫЙ (механизма нет, случаи не запускались)")
        return 1
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        for case in CASES:
            try:
                case(tmp)
            except Exception as exc:  # noqa: BLE001 — падение случая = провал, не падение пробы
                checked += 1
                failed += 1
                print(f"  ПЛОХО {case.__name__}: исключение {type(exc).__name__}: {exc}")
    print(f"SELFTEST: {'зелёный' if not failed else 'КРАСНЫЙ'} ({checked} проверок, "
          f"{len(CASES)} случаев, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
