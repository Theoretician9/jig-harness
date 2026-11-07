#!/usr/bin/env python3
"""Проба журнала запусков для статуса: начало (agent_budget_guard.py) и конец
(agent_finish_log.py) запуска помощника в вызовы-агентов.jsonl.

Статус строит дерево запусков по agent.launch (data.вызов, data.родитель) и
закрывает ветку по agent.finish с тем же вызовом; без этих полей дерево плоское
и вечно «в работе».

Что судит, на стенде (свой LOG_DIR, своя карта без wip, свой pipeline.yaml,
свои meta.json; настоящие журналы не трогаются):
  * запуск пишет «тип», «модель», «вызов» (tool_use_id) в data agent.launch;
  * «родитель» — toolUseId зовущего из его meta.json; нет файла — поля нет;
  * модель: явная tool_input.model; без неё — из frontmatter агента (стенд —
    копия хука со своим .claude/agents); у harness-worker во frontmatter модели
    нет, поэтому подпись приходит только из tool_input.model;
  * конец пишет agent.finish с data {вызов} из meta.json помощника;
  * нет meta.json, битый meta.json, нет toolUseId, мусор на входе — строки
    нет, код 0, хук молчит;
  * старая строка запуска без новых полей читается: потолок её считает,
    статус её пропускает без падения, а новый запуск с концом — закрыт.

Хуки берутся рядом с пробой либо из HARNESS_AGENT_BUDGET_GUARD и
HARNESS_AGENT_FINISH_LOG (двери для мутаций: копия с поломкой).
Прогон: python3 scripts/hooks/test_agent_finish_log.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
GUARD = Path(os.environ.get("HARNESS_AGENT_BUDGET_GUARD") or HERE / "agent_budget_guard.py")
FINISH = Path(os.environ.get("HARNESS_AGENT_FINISH_LOG") or HERE / "agent_finish_log.py")
# Проект: scripts/hooks → scripts/status-report.py; пакет: harness/hooks → harness/scripts.
STATUS = next((p for p in (ROOT / "scripts" / "status-report.py",
                           HERE.parent / "scripts" / "status-report.py") if p.exists()), None)
JOURNAL_NAME = "вызовы-агентов.jsonl"
GUARD_SRC = "scripts/hooks/agent_budget_guard.py"
FINISH_SRC = "scripts/hooks/agent_finish_log.py"
SESSION = "проба"

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
    """Свой каталог журналов, пустая карта, свой потолок и свои meta.json."""

    def __init__(self, base: Path, total: int = 0, guard: Path = GUARD):
        self.base = base
        self.guard = guard
        self.logs = base / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        (base / "dev-map.yaml").write_text("lines: []\n", encoding="utf-8")
        (base / "pipeline.yaml").write_text(yaml.safe_dump(
            {"помощники": {"без-задачи": {"всего": total, "ревью": 0}}},
            allow_unicode=True), encoding="utf-8")

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k != "HARNESS_TASK"}
        env.update(HARNESS_LOG_DIR=str(self.logs), LOG_DIR=str(self.logs),
                   HARNESS_DEVMAP=str(self.base / "dev-map.yaml"),
                   HARNESS_EVENTS_STATE=str(self.base / "state"),
                   HARNESS_PIPELINE_CONF=str(self.base / "pipeline.yaml"),
                   PYTHONDONTWRITEBYTECODE="1")
        return env

    def meta(self, agent: str, body: str) -> None:
        path = self.base / SESSION / "subagents" / f"agent-{agent}.meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def run(self, hook: Path, stdin: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(hook)], input=stdin, capture_output=True,
                              text=True, env=self.env(), timeout=60)

    def launch(self, call: str, tool_input: dict, caller: str = "") -> int:
        payload = {"tool_name": "Agent", "session_id": SESSION, "tool_use_id": call,
                   "transcript_path": str(self.base / "s.jsonl"),
                   "tool_input": {"description": "помощник: код", "prompt": "работа",
                                  **tool_input}}
        if caller:
            payload["agent_id"] = caller
        return self.run(self.guard, json.dumps(payload)).returncode

    def finish(self, agent: str) -> subprocess.CompletedProcess:
        return self.run(FINISH, json.dumps({
            "hook_event_name": "SubagentStop", "session_id": SESSION, "agent_id": agent,
            "transcript_path": str(self.base / "s.jsonl")}))

    def rows(self, event: str) -> list:
        path = self.logs / JOURNAL_NAME
        if not path.exists():
            return []
        return [r for r in map(json.loads, path.read_text(encoding="utf-8").splitlines())
                if r.get("event") == event]


def launch_fields(tmp: Path) -> None:
    stand = Stand(tmp / "запуск")
    code = stand.launch("toolu_A", {"subagent_type": "harness-worker", "model": "sonnet"})
    check("запуск: код 0", 0, code)
    rows = stand.rows("agent.launch")
    check("запуск: одна строка agent.launch от хука", [GUARD_SRC], [r["src"] for r in rows])
    data = rows[0].get("data", {}) if rows else {}
    check("запуск: тип, модель, вызов в data",
          ("harness-worker", "sonnet", "toolu_A"),
          (data.get("тип"), data.get("модель"), data.get("вызов")))
    check("запуск смены: поля «родитель» нет", False, "родитель" in data)

    stand.launch("toolu_W", {"subagent_type": "harness-worker"})
    rows = stand.rows("agent.launch")
    check("harness-worker без tool_input.model: модель пуста (во frontmatter её нет)",
          "", (rows[-1].get("data") or {}).get("модель"))


def parent_from_meta(tmp: Path) -> None:
    stand = Stand(tmp / "родитель")
    stand.meta("lead1", json.dumps({"toolUseId": "toolu_P", "spawnDepth": 1}))
    stand.launch("toolu_C", {"subagent_type": "harness-worker", "model": "sonnet"},
                 caller="lead1")
    stand.launch("toolu_D", {"subagent_type": "harness-worker", "model": "sonnet"},
                 caller="без-файла")
    by_call = {(r.get("data") or {}).get("вызов"): r.get("data") or {}
               for r in stand.rows("agent.launch")}
    check("родитель: toolUseId зовущего из его meta.json",
          "toolu_P", by_call.get("toolu_C", {}).get("родитель"))
    check("родитель: нет meta.json зовущего — поля нет",
          (True, False), ("toolu_D" in by_call, "родитель" in by_call.get("toolu_D", {})))


def model_from_frontmatter(tmp: Path) -> None:
    """Копия хука в своём корне: .claude/agents стенда, настоящие карточки не трогаются."""
    root = tmp / "корень"
    hooks = root / "scripts" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copy(GUARD, hooks / "agent_budget_guard.py")
    (hooks / "agent_skill_guard.py").symlink_to(GUARD.resolve().parent / "agent_skill_guard.py")
    lib = next(p for p in (ROOT / "scripts" / "lib", GUARD.resolve().parent.parent / "lib",
                           GUARD.resolve().parent.parent / "scripts" / "lib")
               if (p / "events.py").exists())
    (root / "scripts" / "lib").symlink_to(lib)
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "проба-опус.md").write_text(
        "---\nname: проба-опус\nmodel: opus\ntools: Read\n---\n\nтекст: model: haiku\n",
        encoding="utf-8")
    stand = Stand(tmp / "frontmatter", guard=hooks / "agent_budget_guard.py")
    stand.launch("toolu_F", {"subagent_type": "проба-опус"})
    stand.launch("toolu_G", {"subagent_type": "проба-опус", "model": "sonnet"})
    stand.launch("toolu_H", {"subagent_type": "нет-такого"})
    models = {(r.get("data") or {}).get("вызов"): (r.get("data") or {}).get("модель")
              for r in stand.rows("agent.launch")}
    check("модель: без tool_input.model — из frontmatter", "opus", models.get("toolu_F"))
    check("модель: tool_input.model важнее frontmatter", "sonnet", models.get("toolu_G"))
    check("модель: нет карточки агента — пусто", "", models.get("toolu_H"))


def finish_writes_call(tmp: Path) -> None:
    stand = Stand(tmp / "конец")
    stand.meta("w1", json.dumps({"toolUseId": "toolu_A", "spawnDepth": 1}))
    proc = stand.finish("w1")
    check("конец: код 0 и тишина", (0, "", ""), (proc.returncode, proc.stdout, proc.stderr))
    rows = stand.rows("agent.finish")
    check("конец: одна строка agent.finish", 1, len(rows))
    row = rows[0] if rows else {}
    check("конец: src, исход, сессия, data {вызов}",
          (FINISH_SRC, "ok", SESSION, {"вызов": "toolu_A"}),
          (row.get("src"), row.get("outcome"), row.get("session"), row.get("data")))


def finish_silent(tmp: Path) -> None:
    stand = Stand(tmp / "без-конца")
    stand.meta("битый", "{не json")
    stand.meta("пустой", json.dumps({"spawnDepth": 1}))
    cases = {"нет meta.json": stand.finish("нет-файла"),
             "битый meta.json": stand.finish("битый"),
             "meta.json без toolUseId": stand.finish("пустой"),
             "мусор на входе": stand.run(FINISH, "не json"),
             "пустой вход": stand.run(FINISH, "")}
    for name, proc in cases.items():
        check(f"{name}: код 0 и тишина", (0, "", ""),
              (proc.returncode, proc.stdout, proc.stderr))
    check("без вызова строки конца нет", [], stand.rows("agent.finish"))


def load_status():
    spec = importlib.util.spec_from_file_location("status_report", STATUS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def old_row_reads(tmp: Path) -> None:
    stand = Stand(tmp / "старая", total=2)
    old = {"v": 1, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "src": GUARD_SRC, "event": "agent.launch", "outcome": "ok", "task": "-",
           "task_from": "arg", "session": SESSION,
           "data": {"ревью": False, "описание": "старый запуск", "уровень": "без-задачи"}}
    (stand.logs / JOURNAL_NAME).write_text(json.dumps(old, ensure_ascii=False) + "\n",
                                           encoding="utf-8")
    check("старая строка: потолок 2, новый запуск в пределах", 0,
          stand.launch("toolu_N", {"subagent_type": "harness-worker", "model": "sonnet"}))
    check("старая строка: засчитана, третий запуск сверх потолка", 2,
          stand.launch("toolu_X", {"subagent_type": "harness-worker", "model": "sonnet"}))
    stand.meta("w2", json.dumps({"toolUseId": "toolu_N"}))
    stand.finish("w2")

    if STATUS is None:
        check("статус: status-report.py найден рядом", True, False)
        return
    status = load_status()
    rows = list(status.read_rows(stand.logs / JOURNAL_NAME))
    since = datetime.min.replace(tzinfo=timezone.utc)
    try:
        launches = status.parse_launches(rows, SESSION, status.Cut(since, None), timedelta(hours=2))
        outcome = [(n.call, n.done) for n in launches.values()]
    except Exception as exc:  # noqa: BLE001 — падение читалки и есть провал
        outcome = f"{type(exc).__name__}: {exc}"
    check("статус: старая строка пропущена, новый запуск закрыт концом",
          [("toolu_N", True)], outcome)


def main() -> int:
    for hook in (GUARD, FINISH):
        if not hook.exists():
            print(f"нет хука {hook}")
            return 1
    with tempfile.TemporaryDirectory(prefix="agent-finish-log-") as raw:
        tmp = Path(raw)
        for case in (launch_fields, parent_from_meta, model_from_frontmatter,
                     finish_writes_call, finish_silent, old_row_reads):
            print(case.__name__)
            case(tmp)
    print(f"проверок {checked}, провалов {failed}")
    return 1 if failed or not checked else 0


if __name__ == "__main__":
    sys.exit(main())
