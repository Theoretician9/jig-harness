#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проба сторожа записи в общий пакет из дерева помощника (scripts/hooks/package_write_guard.py).

Стенд (свой mktemp): пакет-пустышка через HARNESS_STARTER, конфиг установки — свой файл
через HARNESS_CONF (ключ HELPER_WORKTREE), сессия помощника — поле cwd входа хука под
<стенд>/proj/.claude/worktrees/agent-x, основная сессия — <стенд>/proj.

  ключ 1, дерево помощника: Write/Edit файла пакета, у которого есть пара в проекте
    (harness/scripts/…, harness/config/probe-mutations.yaml, harness/agents/…,
    harness/hooks/project-settings.json) — отказ, текст называет дверь «очередь слияния»;
    Bash: перенаправление, sed -i, cp, tee, cd + mv, python write_text — отказ.
  ключ 1, дерево помощника, проходит: файл пакета без пары в проекте (раскладчик,
    слой 1 harness/self/, CLAUDE.md пакета), живой путь (pipeline.yaml), запись в своё
    дерево, чтение пакета, путь-соседка «STARTER-PACKAGE-x».
  ключ 1, основная сессия — проходит; ключ 0 (и без ключа) в дереве помощника — проходит.
  сбой разбора входа — пропуск (код 0) со строкой в stderr.

Какая поломка оставит пробу зелёной? «Дерево не узнаётся по cwd» — краснеют отказы.
«Ключ не читается, всегда 1» — краснеет ключ 0. «Сравнение по префиксу строки» — краснеет
соседка. «Отказ всему пакету» — краснеют раскладчик и слой 1. «Отказ без двери» — краснеет
текст. Дверь мутаций: PACKAGE_WRITE_GUARD_UNDER_TEST (копия хука).
Итог: «итог: N случаев, плохо M», код 0/1.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = Path(os.environ.get("PACKAGE_WRITE_GUARD_UNDER_TEST") or HERE / "package_write_guard.py")

STAND = Path(tempfile.mkdtemp(prefix="package-write-guard-"))
atexit.register(shutil.rmtree, STAND, ignore_errors=True)
PKG = STAND / "STARTER-PACKAGE"
MAIN = STAND / "proj"
TREE = MAIN / ".claude" / "worktrees" / "agent-x"
for where in (PKG / "harness" / "scripts", PKG / "harness" / "config", TREE / "scripts"):
    where.mkdir(parents=True, exist_ok=True)
(STAND / "STARTER-PACKAGE-x").mkdir()
CONF = {key: STAND / f"harness-{key}.conf" for key in ("1", "0", "нет")}
for key, path in CONF.items():
    path.write_text("" if key == "нет" else f'HELPER_WORKTREE="{key}"\n', encoding="utf-8")
TOOL = str(PKG / "harness" / "scripts" / "tool.py")
REGISTRY = str(PKG / "harness" / "config" / "probe-mutations.yaml")
DOOR_WORDS = ["очередь слияния", "merge-queue.py", "в своём дереве"]

checked = failed = 0


def check(name: str, ok: bool, details: str = "") -> None:
    global checked, failed
    checked += 1
    if ok:
        print(f"  ок    {name}")
    else:
        failed += 1
        print(f"  ПЛОХО {name} — {details[:400]}")


def run(payload, key: str = "1") -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("STARTER_DIR", "HARNESS_HOME")}
    env.update({"HARNESS_CONF": str(CONF[key]), "HARNESS_STARTER": str(PKG),
                "PYTHONDONTWRITEBYTECODE": "1"})
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    proc = subprocess.run([sys.executable, str(HOOK)], env=env, capture_output=True, text=True,
                          input=text, timeout=30, cwd=STAND)
    return proc.returncode, proc.stderr


def tool(name: str, cwd: Path = TREE, **inp) -> dict:
    return {"session_id": "s", "hook_event_name": "PreToolUse", "cwd": str(cwd),
            "tool_name": name, "tool_input": inp}


def write(path: str, cwd: Path = TREE) -> dict:
    return tool("Write", cwd, file_path=path, content="y")


def bash(cmd: str, cwd: Path = TREE) -> dict:
    return tool("Bash", cwd, command=cmd)


def denied(name: str, payload: dict, words=("очередь слияния",)) -> None:
    rc, err = run(payload)
    missing = [w for w in words if w not in err]
    check(f"отказ: {name}", rc == 2 and not missing, f"код {rc}, нет слов {missing}: {err}")


def passed(name: str, payload, key: str = "1") -> None:
    rc, err = run(payload, key)
    check(f"проходит: {name}", rc == 0, f"код {rc}: {err}")


print("ключ 1, дерево помощника: файл пакета с парой в проекте — отказ")
denied("Write harness/scripts/tool.py, текст двери", write(TOOL), DOOR_WORDS)
denied("Edit probe-mutations.yaml", tool("Edit", file_path=REGISTRY, old_string="a", new_string="b"))
denied("Write подкаталог cwd-дерева", write(TOOL, TREE / "scripts"))
denied("Write harness/agents/harness-worker.md", write(str(PKG / "harness/agents/harness-worker.md")))
denied("Write project-settings.json", write(str(PKG / "harness/hooks/project-settings.json")))
denied("Bash перенаправление", bash(f"echo y >> {REGISTRY}"))
denied("Bash sed -i", bash(f"sed -i 's/a/b/' {TOOL}"))
denied("Bash cp из дерева в пакет", bash(f"cp scripts/tool.py {TOOL}"))
denied("Bash tee", bash(f"printf y | tee {REGISTRY} >/dev/null"))
denied("Bash cd в пакет и mv", bash(f"cd {PKG}/harness && mv scripts/tool.py scripts/t2.py"))
denied("Bash python write_text", bash(
    f"python3 -c \"import pathlib; pathlib.Path('{TOOL}').write_text('y')\""))

print("ключ 1, дерево помощника: пакет без пары, своё дерево, чтение — проходят")
for rel in ("harness/razlozhit-harnes.sh", "harness/self/roles/worker.md", "CLAUDE.md",
            "harness/config/pipeline.yaml"):
    passed(f"Write {rel}", write(str(PKG / rel)))
passed("Write в своё дерево", write(str(TREE / "scripts" / "tool.py")))
passed("Bash чтение пакета", bash(f"cat {TOOL} | grep x; git -C {PKG} status --short"))
passed("Write соседка STARTER-PACKAGE-x", write(str(STAND / "STARTER-PACKAGE-x/harness/scripts/t.py")))

print("основная сессия и ключ 0 — проходят")
passed("ключ 1, основная сессия: Write в пакет", write(TOOL, MAIN))
passed("ключ 1, основная сессия: Bash sed -i в пакете", bash(f"sed -i 's/a/b/' {TOOL}", MAIN))
passed("ключ 0, дерево помощника: Write в пакет", write(TOOL), "0")
passed("без ключа, дерево помощника: Write в пакет", write(TOOL), "нет")

print("сбой разбора — пропуск")
rc, err = run("не json")
check("битый вход: код 0 и строка в stderr", rc == 0 and "package_write_guard" in err, f"{rc}: {err}")

print(f"итог: {checked} случаев, плохо {failed}")
sys.exit(1 if failed else 0)
