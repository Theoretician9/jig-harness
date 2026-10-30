#!/usr/bin/env python3
"""Проба сторожа ролей помощников (scripts/hooks/lead_role_guard.py).

Случай 29.10.2026: помощник, запускающий помощников, правил файлы сам и звал
тяжёлый тип — правило «управляющий не пишет руками» держалось текстом агента.
Владелец: это должен держать код.

Что судит, на стенде (свой LOG_DIR, живая таблица ролей из harness/config):
  * harness-lead: Edit/Write/NotebookEdit — отказ, кроме журнал.md; Bash — только
    чтение, git-чтение, разрешённые скрипты, запись только в журнал.md и в логи;
    Agent — только harness-worker и только с явной моделью;
  * harness-worker: Agent — отказ, остальное свободно;
  * смена (нет agent_type) и чужой тип — не трогаются, следа нет;
  * каждое решение по двум ролям — строка журнала {роль, инструмент, решение, причина};
  * битый вход и отсутствующая таблица — код 0 и строка skipped.

Механизм — рядом либо HARNESS_LEAD_ROLE_GUARD (дверь для мутаций).
Прогон: python3 scripts/hooks/test_lead_role_guard.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = Path(os.environ.get("HARNESS_LEAD_ROLE_GUARD") or HERE / "lead_role_guard.py")
ROLES = HERE.parent.parent / "harness" / "config" / "helper-roles.yaml"
JOURNAL = "роли-помощников.jsonl"

checked = failed = 0


def check(name, expected, actual) -> None:
    global checked, failed
    checked += 1
    if expected == actual:
        print(f"  ок    {name}")
    else:
        failed += 1
        print(f"  ПЛОХО {name}: ждали «{expected}», вышло «{actual}»")


def run(logs: Path, payload, roles: Path = ROLES, raw: str | None = None):
    env = {**os.environ, "HARNESS_LOG_DIR": str(logs), "HARNESS_HELPER_ROLES": str(roles),
           "HARNESS_TASK": "hr.stand", "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.run([sys.executable, str(GUARD)], env=env, capture_output=True, text=True,
                       input=raw if raw is not None else json.dumps(payload, ensure_ascii=False))
    return p.returncode, p.stderr


def rows(logs: Path) -> list:
    f = logs / JOURNAL
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


def call(role, tool, **inp):
    return {"agent_id": "a1", "agent_type": role, "tool_name": tool, "tool_input": inp,
            "session_id": "s"}


def bash(role, cmd):
    return call(role, "Bash", command=cmd)


LEAD, WORK = "harness-lead", "harness-worker"
J = "/var/www/project/.tasks/hr.x/журнал.md"

with tempfile.TemporaryDirectory() as tmp:
    logs = Path(tmp) / "logs"
    logs.mkdir()

    def code(payload):
        return run(logs, payload)[0]

    print("управляющий: файлы")
    check("Edit кода — отказ", 2, code(call(LEAD, "Edit", file_path="/var/www/project/a.py")))
    check("Write кода — отказ", 2, code(call(LEAD, "Write", file_path="/var/www/project/a.py")))
    check("NotebookEdit — отказ", 2, code(call(LEAD, "NotebookEdit", notebook_path="/x/a.ipynb")))
    check("Write журнала решений — можно", 0, code(call(LEAD, "Write", file_path=J)))
    check("Write журнал.md.bak — отказ", 2, code(call(LEAD, "Write", file_path=J + ".bak")))
    rc, err = run(logs, call(LEAD, "Edit", file_path="/a.py"))
    check("отказ называет harness-worker", True, "harness-worker" in err)

    print("управляющий: Bash")
    for cmd in ["ls -la /var/www", "git status --short", "git log --oneline | head -5",
                "grep -rn foo scripts | wc -l", "python3 scripts/who-writes.py",
                "bash scripts/gates-snapshot.sh",
                f"echo решение >> {J}",
                f"cat >> {J} <<'EOF'\nrm -rf x; touch y && git push\nEOF",
                "setsid nohup bash scripts/gates-snapshot.sh > /var/log/harness/g.log 2>&1 < /dev/null &",
                "tail -3 /var/log/harness/g.log; date"]:
        check(f"можно: {cmd.splitlines()[0][:50]}", 0, code(bash(LEAD, cmd)))
    for cmd in ["git commit -m x", "git push origin master", "rm -rf /tmp/x", "ls; rm x",
                "echo hi > file.txt", f"echo hi > {J}.bak", "sed -i s/a/b/ f", "python3 -c 'print(1)'",
                "ls | xargs rm", "cat x | tee y", "ls $(rm x)", "ls `rm x`", "ls && touch z",
                "bash scripts/deploy.sh", "git checkout -- a.py"]:
        check(f"отказ: {cmd[:50]}", 2, code(bash(LEAD, cmd)))

    print("управляющий: Agent")
    check("harness-worker с моделью — можно", 0,
          code(call(LEAD, "Agent", subagent_type=WORK, model="sonnet", prompt="p")))
    check("harness-worker без модели — отказ", 2,
          code(call(LEAD, "Agent", subagent_type=WORK, prompt="p")))
    check("general-purpose — отказ", 2,
          code(call(LEAD, "Agent", subagent_type="general-purpose", model="sonnet", prompt="p")))
    check("без типа — отказ", 2, code(call(LEAD, "Agent", model="sonnet", prompt="p")))

    print("исполнитель")
    check("Agent — отказ", 2, code(call(WORK, "Agent", subagent_type=WORK, model="sonnet")))
    check("Task — отказ", 2, code(call(WORK, "Task", subagent_type="Explore")))
    check("Edit — можно", 0, code(call(WORK, "Edit", file_path="/a.py")))
    check("Bash rm — можно", 0, code(bash(WORK, "rm /tmp/x")))

    print("прочие")
    n0 = len(rows(logs))
    check("смена (нет agent_type) — Edit", 0, code({"tool_name": "Edit", "tool_input": {"file_path": "/a"}}))
    check("смена — Agent general-purpose", 0,
          code({"tool_name": "Agent", "tool_input": {"subagent_type": "general-purpose"}}))
    check("чужой тип — Edit", 0, code(call("Explore", "Edit", file_path="/a")))
    check("прочие следа не оставляют", n0, len(rows(logs)))

    print("журнал")
    allr = rows(logs)
    check("журнал не пуст", True, len(allr) > 20)
    d = [r["data"] for r in allr if r.get("event") == "role.decision"]
    check("все строки — role.decision", len(allr), len(d))
    check("в строке роль, инструмент, решение, причина", True,
          bool(d) and all({"роль", "инструмент", "решение", "причина"} <= set(x) for x in d))
    check("есть и отказы, и пропуски", {"отказ", "пропуск"}, {x["решение"] for x in d})
    check("у отказа причина не пуста", True, all(x["причина"] for x in d if x["решение"] == "отказ"))

    print("сбои")
    logs2 = Path(tmp) / "l2"
    logs2.mkdir()
    check("битый вход — код 0", 0, run(logs2, None, raw="{не json")[0])
    check("нет таблицы — код 0", 0,
          run(logs2, call(LEAD, "Edit", file_path="/a"), roles=Path(tmp) / "net.yaml")[0])
    check("оба сбоя — строки skipped", ["skipped", "skipped"], [r["outcome"] for r in rows(logs2)])

print(f"\nПроверок: {checked}, упало: {failed}")
sys.exit(1 if failed else 0)
