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
# Скрипты «из scratchpad»: вне дерева помощника, их текст хук читает сам.
AD_HOC = STAND / "scratch" / "patch_tool.py"
AD_HOC_PKG = STAND / "scratch" / "fix_registry.py"
AD_HOC.parent.mkdir()
AD_HOC.write_text("import sys, pathlib\np = pathlib.Path(sys.argv[1]); p.write_text('y')\n", encoding="utf-8")
AD_HOC_PKG.write_text(f"from pathlib import Path\nPath('{REGISTRY}').write_text('y')\n", encoding="utf-8")
(PKG / "harness" / "razlozhit-harnes.sh").write_text("#!/bin/bash\n", encoding="utf-8")

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

print("ключ 1, дерево помощника: python и patch с путём пакета в тексте — отказ (19.11: 4 помощника)")
PY_DOOR = ["очередь слияния", "merge-queue"]
denied("python heredoc: write_text по списку путей", bash(
    f"python3 - <<'PY'\nfrom pathlib import Path\nfor f in ['harness/config/x.yaml', '{REGISTRY}']:\n"
    f"    p = Path(f); p.write_text(p.read_text() + 'y')\nPY"), PY_DOOR)
denied("python heredoc: каталог пакета и open(..., 'w')", bash(
    f"python3 - <<'EOF'\nP = '{PKG}/harness/config'\nopen(P + '/probe-mutations.yaml', 'w').write('y')\nEOF"),
    PY_DOOR)
denied("python -c shutil.copyfile в пакет", bash(
    f"python3 -c \"import shutil; shutil.copyfile('scripts/tool.py', '{TOOL}')\""), PY_DOOR)
denied("python в cd пакета: скрипт из scratchpad, запись в нём", bash(
    f"cd {PKG} && python3 {AD_HOC} harness/scripts/tool.py"), PY_DOOR)
denied("python: скрипт из scratchpad пишет пакет, путь только в скрипте", bash(f"python3 {AD_HOC_PKG}"), PY_DOOR)
denied("patch файла пакета через переменную", bash(f"P={PKG} && patch $P/harness/scripts/tool.py t.diff"),
       PY_DOOR)
denied("patch -R по пути пакета", bash(f"patch -R {TOOL} t.diff"), PY_DOOR)
denied("cd пакета && patch -p1 < diff", bash(f"cd {PKG} && patch -p1 --no-backup-if-mismatch < /tmp/x.diff"),
       PY_DOOR)
denied("patch -d пакета", bash(f"patch -d {PKG} -p1 < /tmp/x.diff"), PY_DOOR)
denied("cd пакета && git apply", bash(f"cd {PKG} && git apply /tmp/x.patch"), PY_DOOR)

print("ключ 1, дерево помощника: python и patch, которые пакет не пишут, — проходят")
passed("python -c читает yaml пакета", bash(
    f"python3 -c \"import yaml; yaml.safe_load(open('{REGISTRY}')); print('ok')\""))
passed("python heredoc читает пакет, пишет своё дерево без пути пакета", bash(
    f"python3 - <<'PY'\nprint(open('{TOOL}').read())\nPY\npython3 -c \"open('x.txt', 'w').write('y')\""))
passed("cmp с пакетом; python пишет своё дерево (звенья судятся порознь)", bash(
    f"cmp scripts/tool.py {TOOL}; python3 -c \"open('scripts/tool.py', 'w').write('y')\""))
passed("patch --dry-run на пакет", bash(f"patch --dry-run {TOOL} t.diff"))
passed("git apply --check в пакете", bash(f"cd {PKG} && git apply --check /tmp/x.patch"))
passed("python пишет файл пакета без пары (раскладчик)", bash(
    f"python3 -c \"open('{PKG}/harness/razlozhit-harnes.sh', 'a').write('#')\""))
passed("python скрипт проекта с путём пакета доводом", bash(
    f"python3 scripts/compare-with-package.py --starter {PKG}"))

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
