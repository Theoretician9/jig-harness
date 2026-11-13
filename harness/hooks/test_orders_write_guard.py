#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проба сторожа записи память/ORDERS.md агентами демонов (scripts/hooks/orders_write_guard.py).

Случай 12.11.2026 10:41. Агент потока проверок (checks-stream, сессия 19da5670)
гасил тревогу ревизии памяти «ORDERS.md 97 % потолка» и целиком переписал
память/ORDERS.md инструментом Write — указания владельца пропали. Снимает
указание только владелец; прополка идёт законным путём scripts/weed-the-orders.py.

Признак агента демона — переменная окружения HARNESS_DEMON (имя демона). Её ставит
единый вход демонов scripts/claude-demon.sh перед вызовом модели; хук получает
окружение процесса claude. У смены, оркестратора и их помощников её нет.

Что судится, на стенде (свой корень проекта во временном каталоге):
  * агент демона: Write/Edit/NotebookEdit в память/ORDERS.md — отказ (код 2), в
    тексте отказа — причина и законная дверь (карточка dev-map.yaml / оркестратор,
    снимает только владелец, прополка — weed-the-orders.py);
  * агент демона: пишущий Bash в этот путь (>, >>, tee, sed -i, cp/mv, python -c
    open(..., 'w'), cd && >, mv ИЗ него, rm/truncate/shred, dd of=, bash/sh -c)
    — отказ с той же дверью; тело heredoc — текст, не команда;
  * путь абсолютный, относительный от cwd, с ./ и .. — судится одинаково;
  * агент демона: чтение ORDERS.md (cat, wc, grep, cp ИЗ него, open на чтение,
    weed-the-orders.py) и запись в другие файлы памяти — пропуск;
  * смена/оркестратор (HARNESS_DEMON нет или пуст) — запись ORDERS.md пропускается;
  * битый вход — пропуск (код 0), как у соседних хуков.

Отказ — код 2, причина в stderr. Пропуск — код 0.
Двери: ORDERS_GUARD_UNDER_TEST — путь хука (мутационная проба подставляет копию);
       ORDERS_GUARD_ROOT — корень проекта, от которого хук судит пути.

Прогон: python3 scripts/hooks/test_orders_write_guard.py  (pytest на машине нет — проба сама
ведёт счёт, как соседние; код 1 при любом ПЛОХО)
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
HOOK = Path(os.environ.get("ORDERS_GUARD_UNDER_TEST") or HERE / "orders_write_guard.py")

STAND = Path(tempfile.mkdtemp(prefix="orders-guard-"))
atexit.register(shutil.rmtree, STAND, ignore_errors=True)
ROOT = STAND / "proj"
ELSEWHERE = STAND / "agent-cwd"          # агент демона работает из пустого каталога
(ROOT / "память").mkdir(parents=True)
ELSEWHERE.mkdir()
ORDERS = ROOT / "память" / "ORDERS.md"
ORDERS.write_text("# ORDERS.md\n- указание владельца\n", encoding="utf-8")
(ROOT / "память" / "MEMORY.md").write_text("# MEMORY\n", encoding="utf-8")
O = str(ORDERS)

# Слова двери: без них отказ не говорит агенту, куда идти законно.
DOOR_WORDS = ["dev-map.yaml", "оркестратор", "владел", "weed-the-orders.py"]

checked = failed = 0


def check(name: str, ok: bool, details: str = "") -> None:
    """Счёт живёт здесь: итоговая строка — доказательство, врать ей нельзя."""
    global checked, failed
    checked += 1
    if ok:
        print(f"  ок    {name}")
    else:
        failed += 1
        print(f"  ПЛОХО {name} — {details[:300]}")


def run(payload, demon: str | None = "checks-stream", raw: str | None = None):
    env = {k: v for k, v in os.environ.items() if k != "HARNESS_DEMON"}
    env.update({"ORDERS_GUARD_ROOT": str(ROOT), "CLAUDE_PROJECT_DIR": str(ROOT),
                "PYTHONDONTWRITEBYTECODE": "1"})
    if demon is not None:
        env["HARNESS_DEMON"] = demon
    p = subprocess.run([sys.executable, str(HOOK)], env=env, capture_output=True, text=True,
                       input=raw if raw is not None else json.dumps(payload, ensure_ascii=False),
                       timeout=30)
    return p.returncode, p.stderr


def tool(name: str, cwd: Path = ROOT, **inp):
    return {"session_id": "s", "hook_event_name": "PreToolUse", "cwd": str(cwd),
            "tool_name": name, "tool_input": inp}


def bash(cmd: str, cwd: Path = ROOT):
    return tool("Bash", cwd, command=cmd)


def denied(name: str, payload, demon="checks-stream") -> None:
    rc, err = run(payload, demon)
    missing = [w for w in DOOR_WORDS if w not in err]
    check(f"отказ: {name}", rc == 2 and not missing,
          f"код {rc}, нет двери {missing}: {err.strip()}")


def passed(name: str, payload, demon="checks-stream") -> None:
    rc, err = run(payload, demon)
    check(f"пропуск: {name}", rc == 0, f"код {rc}: {err.strip()}")


# Без хука питон отдал бы код 2 «can't open file» — ложный «отказ». Провал ясный.
if not HOOK.is_file():
    print(f"ПЛОХО · нет хука {HOOK}")
    print("итог: 0 случаев, хук отсутствует — КРАСНОЕ")
    sys.exit(1)

print("агент демона: файловые инструменты в ORDERS.md, любым путём")
PATH_VARIANTS = [
    (O, ELSEWHERE),                                 # абсолютный, из пустого каталога
    ("память/ORDERS.md", ROOT),                     # относительный от корня
    ("./память/ORDERS.md", ROOT),
    ("ORDERS.md", ROOT / "память"),
    ("../память/ORDERS.md", ROOT / "память"),
    (str(ROOT / "память" / "." / ".." / "память" / "ORDERS.md"), ELSEWHERE),
]
for name in ["Write", "Edit"]:
    for path, cwd in PATH_VARIANTS:
        denied(f"{name} {path} (cwd {cwd.name})",
               tool(name, cwd, file_path=path, content="x", old_string="a", new_string="b"))
denied("NotebookEdit", tool("NotebookEdit", ELSEWHERE, notebook_path=O, new_source="x"))

print("агент демона: пишущий Bash в ORDERS.md")
BASH_WRITES = [
    (f"echo x > {O}", ELSEWHERE),
    (f"echo x >> {O}", ELSEWHERE),
    ("printf 'x' > память/ORDERS.md", ROOT),
    ("echo x >./память/ORDERS.md", ROOT),
    (f"cat > {O} <<'EOF'\nновое\nEOF", ELSEWHERE),
    (f"echo x | tee {O}", ELSEWHERE),
    ("echo x | tee -a ./память/ORDERS.md", ROOT),
    (f"sed -i 's/a/b/' {O}", ELSEWHERE),
    ("sed -i.bak '1d' память/ORDERS.md", ROOT),
    (f"cp /tmp/new.md {O}", ELSEWHERE),
    ("mv /tmp/new.md память/ORDERS.md", ROOT),
    ("cp /tmp/new.md ../память/ORDERS.md", ROOT / "память"),
    (f"python3 -c \"open('{O}', 'w').write('x')\"", ELSEWHERE),
    ("python3 -c \"open('память/ORDERS.md','a').write('x')\"", ROOT),
    (f"python3 -c \"import pathlib; pathlib.Path('{O}').write_text('x')\"", ELSEWHERE),
    (f"cd {ROOT} && cat > память/ORDERS.md <<'EOF'\nx\nEOF", ELSEWHERE),   # как в случае 12.11
    (f"cd {ROOT}; echo x > ./память/ORDERS.md", ELSEWHERE),
    (f"ls; echo x > {O}", ELSEWHERE),
    (f"mv {O} /tmp/orders-away.md", ELSEWHERE),             # увести файл — тоже стереть
    (f"rm -f {O}", ELSEWHERE),
    ("truncate -s 0 память/ORDERS.md", ROOT),
    (f"shred -u {O}", ELSEWHERE),
    (f"dd if=/dev/null of={O}", ELSEWHERE),
    (f"bash -c 'echo x > {O}'", ELSEWHERE),
    ("sh -c 'cd .. && echo x >> память/ORDERS.md'", ROOT / "память"),
]
for cmd, cwd in BASH_WRITES:
    denied(cmd.splitlines()[0][:70], bash(cmd, cwd))

print("агент демона: чтение ORDERS.md и запись в другое")
BASH_PASSES = [
    (f"cat {O}", ELSEWHERE),
    ("cat память/ORDERS.md", ROOT),
    (f"wc -m {O}", ELSEWHERE),
    (f"grep -n владел {O} | head", ELSEWHERE),
    (f"cp {O} /tmp/orders-copy.md", ELSEWHERE),               # ИЗ него — чтение
    (f"cat {O} > /tmp/orders-copy.md", ELSEWHERE),
    (f"python3 -c \"print(open('{O}').read())\"", ELSEWHERE),
    (f"python3 {ROOT}/scripts/weed-the-orders.py --сухо", ELSEWHERE),   # законная дверь
    ("echo x >> память/MEMORY.md", ROOT),
    (f"echo x > {ROOT}/память/ORDERS.md.bak", ELSEWHERE),
    (f"echo x > {STAND}/чужой/память/ORDERS.md", ELSEWHERE),   # вне корня проекта
    # Тело heredoc — текст: записка о команде, а не сама команда.
    (f"cat > /tmp/note.md <<'EOF'\necho x > {O}\nEOF", ELSEWHERE),
]
for cmd, cwd in BASH_PASSES:
    passed(cmd.splitlines()[0][:70], bash(cmd, cwd))
passed("Read ORDERS.md", tool("Read", ELSEWHERE, file_path=O))
for name in ["Write", "Edit"]:
    passed(f"{name} MEMORY.md", tool(name, ELSEWHERE, file_path=str(ROOT / "память" / "MEMORY.md"),
                                      content="x", old_string="a", new_string="b"))

print("смена/оркестратор: признака демона нет")
for demon, label in [(None, "нет HARNESS_DEMON"), ("", "HARNESS_DEMON пуст")]:
    passed(f"{label}: Write ORDERS.md", tool("Write", ROOT, file_path=O, content="x"), demon)
    passed(f"{label}: Edit память/ORDERS.md",
           tool("Edit", ROOT, file_path="память/ORDERS.md", old_string="a", new_string="b"), demon)
    passed(f"{label}: Bash >> ORDERS.md", bash(f"echo x >> {O}"), demon)

print("признак — сам факт демона, не имя checks-stream")
denied("демон devmap-selfheal: Write ORDERS.md",
       tool("Write", ELSEWHERE, file_path=O, content="x"), "devmap-selfheal")

print("корень проекта — по месту хука, а не по CLAUDE_PROJECT_DIR")
# Claude Code ставит хуку CLAUDE_PROJECT_DIR = каталог запуска, а демон идёт из
# пустого каталога: корень из этой переменной указывал бы мимо настоящего ORDERS.md.
# Стенд — свой проект с копией хука в scripts/hooks; двери ORDERS_GUARD_ROOT нет.
LAID = STAND / "laid"
(LAID / "scripts" / "hooks").mkdir(parents=True)
(LAID / "память").mkdir()
LAID_ORDERS = LAID / "память" / "ORDERS.md"
LAID_ORDERS.write_text("# ORDERS.md\n", encoding="utf-8")
LAID_HOOK = LAID / "scripts" / "hooks" / "orders_write_guard.py"
shutil.copy(HOOK, LAID_HOOK)
laid_env = {k: v for k, v in os.environ.items() if k not in ("ORDERS_GUARD_ROOT", "HARNESS_DEMON")}
laid_env.update({"CLAUDE_PROJECT_DIR": str(ELSEWHERE), "HARNESS_DEMON": "checks-stream",
                 "PYTHONDONTWRITEBYTECODE": "1"})
p = subprocess.run([sys.executable, str(LAID_HOOK)], env=laid_env, capture_output=True, text=True,
                   input=json.dumps(tool("Write", ELSEWHERE, file_path=str(LAID_ORDERS), content="x")),
                   timeout=30)
check("демон из пустого каталога (CLAUDE_PROJECT_DIR = он): Write ORDERS.md проекта хука — отказ",
      p.returncode == 2, f"код {p.returncode}: {p.stderr.strip()}")

print("сбои")
rc, _ = run(None, raw="{не json")
check("битый вход — пропуск", rc == 0, f"код {rc}")

check("ORDERS.md стенда не тронут", ORDERS.read_text(encoding="utf-8").endswith("указание владельца\n"))
print(f"итог: {checked} случаев, плохо {failed}")
sys.exit(1 if failed else 0)
