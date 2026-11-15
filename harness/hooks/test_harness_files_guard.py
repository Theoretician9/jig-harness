#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проба сторожа файлов харнеса у копии (scripts/hooks/harness_files_guard.py).

Стенд (свой mktemp) — проект с описью раскладки .layout-manifest: scripts/x.py,
scripts/hooks/h.py и harness/self/instance.yaml в описи; слой копии — настоящий
harness/config/copy-layer.yaml, положенный в стенд. Конфиг установки — свой файл
через HARNESS_CONF; ключ EVOLUTION_ROLE из окружения снят.

  копия (EVOLUTION_ROLE=copy): Write scripts/x.py — отказ, в тексте три двери
    (только для чтения; находка строителю в HarnessEvolution; слой копии);
    Bash: sed -i, перенаправление, cp, mv, python write_text в файл описи — отказ;
    память/, STATE.md, .tasks/, instance.yaml (в описи, но в слое), новый файл
    вне описи, журнал вне проекта, чтение файла харнеса — проходят.
  хозяин (EVOLUTION_ROLE=host): те же записи в файлы описи — проходят.
  слой: каждая маска — путь, который раскладка не снимает (ярус ≠ 0 в layout_manifest).

Какая поломка оставит пробу зелёной? «Признак хозяина не читается, всегда копия» —
краснеет хозяин. «Всегда хозяин» — краснеют отказы. «Слой не читается» — краснеет
instance.yaml. «Bash не разбирается» — краснеет sed -i. Отказ без двери — краснеет
текст. Двери проб: HARNESS_FILES_GUARD_UNDER_TEST (копия хука для мутации).
Итог: «итог: N случаев, плохо M», код 0/1.
"""
from __future__ import annotations

import atexit
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
HOOK = Path(os.environ.get("HARNESS_FILES_GUARD_UNDER_TEST") or HERE / "harness_files_guard.py")
PROJECT = HERE.parent.parent
LAYER = PROJECT / "harness" / "config" / "copy-layer.yaml"
sys.path.insert(0, str(HERE.parent / "lib"))
import layout_manifest  # noqa: E402

STAND = Path(tempfile.mkdtemp(prefix="harness-files-guard-"))
atexit.register(shutil.rmtree, STAND, ignore_errors=True)
ROOT = STAND / "proj"
LAID = ["scripts/x.py", "scripts/hooks/h.py", "harness/self/instance.yaml"]
for rel in LAID + ["память/MEMORY.md", "STATE.md"]:
    (ROOT / rel).parent.mkdir(parents=True, exist_ok=True)
    (ROOT / rel).write_text("x\n", encoding="utf-8")
(ROOT / "harness" / "config").mkdir(parents=True, exist_ok=True)
shutil.copy(LAYER, ROOT / "harness" / "config" / "copy-layer.yaml")
rows = "".join(f"{'0' * 40}\tscripts\t{rel}\n" for rel in sorted(LAID))
(ROOT / ".layout-manifest").write_text(
    f"# layout-manifest 2\n# пакет -\n{rows}# оставлено\n", encoding="utf-8")
CONF = {role: STAND / f"harness-{role}.conf" for role in ("copy", "host")}
for role, path in CONF.items():
    path.write_text(f'EVOLUTION_ROLE="{role}"\n', encoding="utf-8")
X = str(ROOT / "scripts" / "x.py")
LAYER_PLACES = [p["место"] for p in yaml.safe_load(LAYER.read_text(encoding="utf-8"))["места"]]
DOOR_WORDS = ["только для чтения", "HarnessEvolution", "находки/<сервер>/<ГГГГ-ММ-ДД>-<суть>.md",
              "подписанным коммитом", "scripts/evolution-net.py --отправить", "README",
              "слое копии"] + LAYER_PLACES

checked = failed = 0


def check(name: str, ok: bool, details: str = "") -> None:
    global checked, failed
    checked += 1
    if ok:
        print(f"  ок    {name}")
    else:
        failed += 1
        print(f"  ПЛОХО {name} — {details[:400]}")


def run(payload: dict, role: str = "copy"):
    env = {k: v for k, v in os.environ.items() if k not in ("EVOLUTION_ROLE", "HARNESS_HOME")}
    env.update({"HARNESS_FILES_GUARD_ROOT": str(ROOT), "HARNESS_CONF": str(CONF[role]),
                "CLAUDE_PROJECT_DIR": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"})
    p = subprocess.run([sys.executable, str(HOOK)], env=env, capture_output=True, text=True,
                       input=json.dumps(payload, ensure_ascii=False), timeout=30)
    return p.returncode, p.stderr


def tool(name: str, **inp) -> dict:
    return {"session_id": "s", "hook_event_name": "PreToolUse", "cwd": str(ROOT),
            "tool_name": name, "tool_input": inp}


def write(rel: str) -> dict:
    return tool("Write", file_path=str(ROOT / rel), content="y")


def bash(cmd: str) -> dict:
    return tool("Bash", command=cmd)


def denied(name: str, payload: dict, words=("только для чтения",)) -> None:
    rc, err = run(payload)
    missing = [w for w in words if w not in err]
    check(f"копия, отказ: {name}", rc == 2 and not missing, f"код {rc}, нет слов {missing}: {err}")


def passed(name: str, payload: dict, role: str = "copy") -> None:
    rc, err = run(payload, role)
    check(f"{'хозяин' if role == 'host' else 'копия'}, проходит: {name}", rc == 0, f"код {rc}: {err}")


print("копия: файлы описи только для чтения")
denied("Write scripts/x.py, текст трёх дверей", write("scripts/x.py"), DOOR_WORDS)
denied("Edit scripts/hooks/h.py", tool("Edit", file_path=str(ROOT / "scripts/hooks/h.py"),
                                       old_string="x", new_string="y"))
denied("Bash sed -i scripts/x.py", bash("sed -i 's/x/y/' scripts/x.py"))
denied("Bash перенаправление в файл харнеса", bash(f"echo y > {X}"))
denied("Bash cp поверх файла харнеса", bash("cp /etc/hostname scripts/x.py"))
denied("Bash cd и mv", bash("cd scripts && mv x.py y.py"))
denied("Bash python write_text", bash(f"python3 -c \"import pathlib; pathlib.Path('{X}').write_text('y')\""))

print("копия: слой и своё пишутся")
for rel in ("память/MEMORY.md", "STATE.md", ".tasks/t/a.md", "harness/self/instance.yaml",
            "scripts/own-new.py"):
    passed(f"Write {rel}", write(rel))
passed("Bash перенаправление в журнал вне проекта", bash(f"echo y > {STAND}/журнал.log"))
passed("Bash sed -i в памяти", bash("sed -i 's/x/y/' память/MEMORY.md"))
passed("Bash чтение файла харнеса", bash(f"cat {X} | grep x"))

print("хозяин пишет как раньше")
passed("Write scripts/x.py", write("scripts/x.py"), "host")
passed("Bash sed -i scripts/x.py", bash("sed -i 's/x/y/' scripts/x.py"), "host")

print("слой копии: раскладка этих мест не снимает")
masks = [p["маска"] for p in yaml.safe_load(LAYER.read_text(encoding="utf-8"))["места"] if p.get("маска")]
check("в слое есть маски", bool(masks), "пустой список")
for mask in masks:
    sample = mask.replace("*", "x")
    check(f"{mask}: ярус раскладки ≠ 0", fnmatch.fnmatchcase(sample, mask)
          and layout_manifest.forbidden_tier(sample) != 0, f"ярус {layout_manifest.forbidden_tier(sample)}")

check("файл харнеса стенда не тронут", Path(X).read_text(encoding="utf-8") == "x\n")
print(f"итог: {checked} случаев, плохо {failed}")
sys.exit(1 if failed else 0)
