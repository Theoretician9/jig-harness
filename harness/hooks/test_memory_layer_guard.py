#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проба хука записи слоёв памяти: ядро и слой человека не правятся мимо двери.

Откуда взята. Спека §5.1–§5.3 и Ф4: три слоя памяти.
Слой 1 (личность, стандарты, политика запуска) меняется только выпуском харнеса;
слой 2 (instance.yaml) пишет только scripts/self-set.py по кнопке «Да»; слой 3
(память/*.md, STATE.md) модель пишет сама. Хук PreToolUse Write|Edit|NotebookEdit
ловит СЛУЧАЙНУЮ правку слоёв 1–2 и называет законный ход. Bash он не разбирает
(спека §5.3): замок слоя 1 — эталон, слоя 2 — сверка sha256 при старте смены.

Что судится:
  - ключи memory-layers.yaml — ровно пять (контракт K42);
  - каждый файл слоёв 1 и 2 из ЖИВОГО memory-layers.yaml (список — разбором,
    пустой список — провал) получает отказ с законным ходом;
  - память/*.md, STATE.md, файлы без потерь — пропуск (иначе смена не ведёт
    заметок: failure_mode контракта);
  - абсолютный, относительный путь, путь с ./ и .. судятся одинаково;
  - хук читает файл слоёв, а не свой список: стендовый файл слоёв с путём,
    которого нет нигде больше, меняет решение.

Формат отказа — как у соседних хуков (model_guard.py): код 2, причина в stderr.
Пропуск — код 0 без решения deny в stdout.

Двери:
  MEMORY_GUARD_UNDER_TEST — путь хука (мутационная проба подставляет копию);
  MEMORY_LAYERS           — файл слоёв, который читает хук;
  MEMORY_GUARD_ROOT       — корень проекта, от которого хук судит пути.

Прогон: python3 scripts/hooks/test_memory_layer_guard.py
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

ROOT = Path(__file__).resolve().parent.parent.parent
HOOK = Path(os.environ.get("MEMORY_GUARD_UNDER_TEST")
            or ROOT / "scripts" / "hooks" / "memory_layer_guard.py")
LIVE_LAYERS = ROOT / "harness" / "config" / "memory-layers.yaml"
# Ключи спеки §5.2 (контракт K42): ровно эти пять, не больше.
LAYER_KEYS = {"неизменяемое", "человека", "без_потерь", "архив_прополки",
              "обязательные_поля_человека"}
# Что спека §5.2 называет в каждом списке поимённо: живой файл обязан это содержать.
SPEC_MEMBERS = {
    "неизменяемое": ["harness/self/core.json", "harness/self/standards.md",
                     "harness/self/roles/*.md", "harness/config/launch-policy.yaml",
                     "harness/config/memory-layers.yaml"],
    "человека": ["harness/self/instance.yaml"],
    "без_потерь": ["память/ORDERS.md", "память/owner-orders-*-verbatim.md"],
    "обязательные_поля_человека": ["копия", "роль", "цель", "человек", "проект.описание"],
}
CORE_HINT = "правка ядра идёт в пакет"
HUMAN_HINT = "self-set.py"

STAND = Path(tempfile.mkdtemp(prefix="sap-ch11-"))
atexit.register(shutil.rmtree, STAND, ignore_errors=True)

cases = 0
failures = 0


def check(name: str, ok: bool, details: str = "") -> None:
    """Счёт живёт здесь: итоговая строка — доказательство, врать ей нельзя."""
    global cases, failures
    cases += 1
    if ok:
        print(f"ок    · {name}")
    else:
        failures += 1
        print(f"ПЛОХО · {name} — {details[:300]}")


def run_hook(payload, layers: Path, cwd: Path = ROOT) -> tuple[int, str, str]:
    """Хук с JSON на stdin. payload-строка уходит как есть (битый вход)."""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    env = dict(os.environ, MEMORY_LAYERS=str(layers), MEMORY_GUARD_ROOT=str(ROOT),
               CLAUDE_PROJECT_DIR=str(ROOT))
    env.pop("MEMORY_GUARD_UNDER_TEST", None)
    done = subprocess.run([sys.executable, str(HOOK)], input=text, capture_output=True,
                          text=True, timeout=30, cwd=str(cwd), env=env)
    return done.returncode, done.stdout, done.stderr


def payload(tool: str, path: str) -> dict:
    """Вход PreToolUse так, как его подаёт Claude Code: cwd сверху, путь в tool_input."""
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    tool_input = {key: path}
    if tool == "Edit":
        tool_input.update(old_string="a", new_string="b")
    elif tool == "Write":
        tool_input["content"] = "b\n"
    elif tool == "NotebookEdit":
        tool_input.update(new_source="b", cell_id="c1")
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
            "cwd": str(ROOT), "session_id": "sap-ch11"}


def denied(result: tuple[int, str, str], hint: str) -> tuple[bool, str]:
    rc, out, err = result
    ok = rc == 2 and hint in err
    return ok, f"rc={rc}, ждал 2 и «{hint}» в stderr; stderr={err.strip()[:160]!r} stdout={out.strip()[:80]!r}"


def passed(result: tuple[int, str, str]) -> tuple[bool, str]:
    rc, out, err = result
    ok = rc == 0 and '"deny"' not in out
    return ok, f"rc={rc}, ждал пропуск 0; stderr={err.strip()[:160]!r} stdout={out.strip()[:80]!r}"


def expand(pattern: str) -> list[str]:
    """Пути шаблона слоя от корня проекта; шаблон без совпадений даёт придуманное имя
    по шаблону: правило — шаблон, а не файлы, которые сегодня лежат."""
    if any(ch in pattern for ch in "*?["):
        found = sorted(str(p.relative_to(ROOT)) for p in ROOT.glob(pattern) if p.is_file())
        invented = pattern.replace("*", "proba-ch11-net-takogo")
        assert fnmatch.fnmatchcase(invented, pattern)
        return found + [invented]
    return [pattern]


def case_layer_keys() -> dict | None:
    """K42 и разбор живого файла слоёв: без него судить хук нечем."""
    if not LIVE_LAYERS.is_file():
        check("memory-layers.yaml есть", False, f"нет {LIVE_LAYERS}")
        return None
    try:
        data = yaml.safe_load(LIVE_LAYERS.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        check("memory-layers.yaml разбирается", False, str(error))
        return None
    check("ключи memory-layers.yaml ровно пять (K42)", set(data) == LAYER_KEYS,
          f"ключи {sorted(data)}")
    for key, members in SPEC_MEMBERS.items():
        value = data.get(key)
        ok = isinstance(value, list) and bool(value) and all(m in value for m in members)
        check(f"«{key}» — непустой список с файлами спеки §5.2", ok, f"{key}={value!r}")
    archive = data.get("архив_прополки")
    check("«архив_прополки» — каталог память/archive/",
          isinstance(archive, str) and archive.rstrip("/") == "память/archive", f"{archive!r}")
    return data


def case_live_layers(data: dict) -> None:
    """Каждый путь слоёв 1 и 2 — отказ; без потерь — пропуск. Списки — разбором."""
    hard = [p for pattern in data.get("неизменяемое") or [] for p in expand(pattern)]
    human = [p for pattern in data.get("человека") or [] for p in expand(pattern)]
    lossless = [p for pattern in data.get("без_потерь") or [] for p in expand(pattern)]
    check("разбор слоя 1 дал пути", bool(hard), "пустой список — судить нечего")
    check("разбор слоя 2 дал пути", bool(human), "пустой список — судить нечего")
    for rel in hard:
        for tool in ("Edit", "Write"):
            ok, why = denied(run_hook(payload(tool, str(ROOT / rel)), LIVE_LAYERS), CORE_HINT)
            check(f"слой 1: {tool} {rel} — отказ «{CORE_HINT}»", ok, why)
    for rel in human:
        for tool in ("Edit", "Write"):
            ok, why = denied(run_hook(payload(tool, str(ROOT / rel)), LIVE_LAYERS), HUMAN_HINT)
            check(f"слой 2: {tool} {rel} — отказ с подсказкой {HUMAN_HINT}", ok, why)
    for rel in lossless:
        ok, why = passed(run_hook(payload("Edit", str(ROOT / rel)), LIVE_LAYERS))
        check(f"без потерь: Edit {rel} — пропуск (прополку судит гейт, не хук)", ok, why)


def case_layer3_passes() -> None:
    """Слой 3 модель пишет сама: хук, режущий память, отнимает у смены заметки."""
    for tool, rel in (("Edit", "память/x.md"), ("Write", "память/x.md"),
                      ("Edit", "STATE.md"), ("Write", "STATE.md"),
                      ("NotebookEdit", "память/x.ipynb"), ("Write", "docs/handover/x.md"),
                      ("Edit", "harness/self/coreXjson")):
        ok, why = passed(run_hook(payload(tool, str(ROOT / rel)), LIVE_LAYERS))
        check(f"слой 3: {tool} {rel} — пропуск", ok, why)


def case_path_forms() -> None:
    """Один файл любым написанием пути — одно решение."""
    core_forms = ("harness/self/core.json", "./harness/self/core.json",
                  str(ROOT / "harness" / ".." / "harness" / "self" / "core.json"),
                  str(ROOT) + "/./harness/self/core.json",
                  "harness/self/../self/core.json")
    for form in core_forms:
        ok, why = denied(run_hook(payload("Edit", form), LIVE_LAYERS), CORE_HINT)
        check(f"путь «{form}» к core.json — отказ", ok, why)
    human_forms = ("harness/self/instance.yaml", "./harness/self/instance.yaml",
                   "harness/config/../self/instance.yaml",
                   str(ROOT) + "/harness/./self/instance.yaml")
    for form in human_forms:
        ok, why = denied(run_hook(payload("Write", form), LIVE_LAYERS), HUMAN_HINT)
        check(f"путь «{form}» к instance.yaml — отказ", ok, why)
    memory_forms = ("память/x.md", "./память/x.md", str(ROOT) + "/./память/x.md",
                    "harness/../память/x.md", "STATE.md", "./STATE.md")
    for form in memory_forms:
        ok, why = passed(run_hook(payload("Edit", form), LIVE_LAYERS))
        check(f"путь «{form}» к слою 3 — пропуск", ok, why)
    # «..» уводит из слоя: решение по настоящему файлу, а не по началу строки.
    ok, why = passed(run_hook(payload("Edit", "harness/self/../../память/x.md"), LIVE_LAYERS))
    check("путь harness/self/../../память/x.md — пропуск (это слой 3)", ok, why)


def case_notebook_and_tools() -> None:
    """NotebookEdit несёт путь в notebook_path; Bash хук не разбирает (спека §5.3)."""
    ok, why = denied(run_hook(payload("NotebookEdit", str(ROOT / "harness/self/instance.yaml")),
                              LIVE_LAYERS), HUMAN_HINT)
    check("NotebookEdit по notebook_path instance.yaml — отказ", ok, why)
    ok, why = denied(run_hook(payload("NotebookEdit", str(ROOT / "harness/self/core.json")),
                              LIVE_LAYERS), CORE_HINT)
    check("NotebookEdit по notebook_path core.json — отказ", ok, why)
    bash = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(ROOT),
            "tool_input": {"command": "echo x > harness/self/core.json"}}
    ok, why = passed(run_hook(bash, LIVE_LAYERS))
    check("Bash с записью в core.json — пропуск (хук Bash не разбирает)", ok, why)
    read = {"hook_event_name": "PreToolUse", "tool_name": "Read", "cwd": str(ROOT),
            "tool_input": {"file_path": str(ROOT / "harness/self/core.json")}}
    ok, why = passed(run_hook(read, LIVE_LAYERS))
    check("Read core.json — пропуск (чтение не запись)", ok, why)


def case_outside_project() -> None:
    """Тот же относительный путь вне проекта — не слой проекта: правка ядра в пакете
    и есть законный ход из подсказки, хук обязан его пускать."""
    package = STAND / "STARTER-PACKAGE"
    for rel in ("harness/self/core.json", "harness/self/instance.yaml"):
        target = package / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
        ok, why = passed(run_hook(payload("Edit", str(target)), LIVE_LAYERS))
        check(f"пакет: Edit {target} — пропуск", ok, why)


def case_stand_layers() -> None:
    """Хук читает файл слоёв, а не свой список: в стендовом файле core.json нет,
    зато есть пути, которых нет больше нигде."""
    layers = STAND / "memory-layers.yaml"
    layers.write_text(
        "неизменяемое: [\"proba-ch11-tolko-zdes/*.txt\"]\n"
        "человека: [proba-ch11-chelovek.yaml]\n"
        "без_потерь: [память/ORDERS.md]\n"
        "архив_прополки: память/archive/\n"
        "обязательные_поля_человека: [копия]\n", encoding="utf-8")
    ok, why = denied(run_hook(payload("Edit", str(ROOT / "proba-ch11-tolko-zdes/a.txt")), layers),
                     CORE_HINT)
    check("стенд слоёв: шаблон слоя 1 только из файла — отказ", ok, why)
    ok, why = denied(run_hook(payload("Write", "proba-ch11-chelovek.yaml"), layers), HUMAN_HINT)
    check("стенд слоёв: слой 2 только из файла — отказ", ok, why)
    ok, why = passed(run_hook(payload("Edit", str(ROOT / "harness/self/core.json")), layers))
    check("стенд слоёв без core.json — Edit core.json пропуск (список не зашит)", ok, why)
    ok, why = passed(run_hook(payload("Edit", str(ROOT / "proba-ch11-tolko-zdes/a.md")), layers))
    check("стенд слоёв: имя мимо шаблона *.txt — пропуск", ok, why)


def case_fail_open() -> None:
    """Хук — подсказка, а не замок (спека §5.3): битый вход не останавливает смену."""
    for name, text in (("не JSON", "{не json"), ("пустой вход", ""),
                       ("нет tool_input", json.dumps({"tool_name": "Edit"}))):
        rc, out, err = run_hook(text, LIVE_LAYERS)
        check(f"битый вход ({name}) — пропуск 0", rc == 0, f"rc={rc} stderr={err.strip()[:160]!r}")


def main() -> int:
    print(f"хук: {HOOK}")
    if not HOOK.is_file():
        check("файл хука есть", False, f"нет {HOOK} — хук слоёв ещё не написан")
    data = case_layer_keys()
    if data is not None:
        case_live_layers(data)
    case_layer3_passes()
    case_path_forms()
    case_notebook_and_tools()
    case_outside_project()
    case_stand_layers()
    case_fail_open()
    print(f"итог: случаев {cases}, неудач {failures}")
    return 1 if failures or not cases else 0


if __name__ == "__main__":
    sys.exit(main())
