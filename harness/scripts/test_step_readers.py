#!/usr/bin/env python3
"""Проба «у каждого выключаемого шага есть читатель в коде».

Приказ владельца 17.11.2026 (память/owner-orders-17-11-verbatim.md): выключил шаг
в панели — КОД не даёт его делать и не требует. Кнопка, которую никто не
читает, выглядит рабочей и не делает ничего (так было с «тесты»: гейт тестов до
кода шаги уровня не смотрел). Носитель — данные пульта pipeline.yaml
«читатели_шагов: {шаг: путь}»; источник списка шагов — `ШАГИ` в write-step.py
(то, что панель позволяет выключить).

Читатель засчитан, если файл есть, в его КОДЕ (не в комментарии) зовётся
шаги_уровня или шаги_приёмки_задачи и шаг назван строкой в кавычках.
Живость судьи доказывают стенды: свой pipeline.yaml и свои файлы-читатели.
"""
import ast
import re
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CALLS = ("шаги_уровня", "шаги_приёмки_задачи")

paths = 0
failures = 0
yards = []


def check(name, ok, details=""):
    global paths, failures
    paths += 1
    if ok:
        print(f"  ок    {name}")
    else:
        failures += 1
        print(f"  ПЛОХО {name} — {details}")


def steps_of(root):
    """ШАГИ write-step.py — разбором, а не поиском слова."""
    for node in ast.walk(ast.parse((root / "scripts" / "write-step.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ШАГИ" for t in node.targets):
            return [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
    return []


def code_lines(text):
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def troubles(root):
    """Беды судьи: каждая начинается с имени шага. Пустой список шагов — беда."""
    steps = steps_of(root)
    if not steps:
        return ["ШАГИ write-step.py: разбор пуст"]
    data = yaml.safe_load((root / "harness" / "config" / "pipeline.yaml").read_text(encoding="utf-8")) or {}
    readers = data.get("читатели_шагов")
    if not isinstance(readers, dict) or not readers:
        return [f"{step}: в pipeline.yaml нет раздела «читатели_шагов»" for step in steps]
    found = []
    for step in steps:
        rel = readers.get(step)
        if not rel:
            found.append(f"{step}: читателя в «читатели_шагов» нет")
            continue
        path = root / str(rel)
        if not path.is_file():
            found.append(f"{step}: читатель {rel} — файла нет")
            continue
        code = "\n".join(code_lines(path.read_text(encoding="utf-8", errors="replace")))
        if not any(re.search(rf"\b{call}\s*\(", code) for call in CALLS):
            found.append(f"{step}: читатель {rel} не зовёт {' / '.join(CALLS)}")
        if not re.search(rf"[\"']{re.escape(step)}[\"']", code):
            found.append(f"{step}: читатель {rel} шаг «{step}» в коде не называет")
    return found


def stand():
    """Двор: копия write-step.py и пульт, где у каждого шага свой годный читатель."""
    yard = Path(tempfile.mkdtemp(prefix="proba-chitateli-shagov-"))
    yards.append(yard)
    (yard / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "write-step.py", yard / "scripts" / "write-step.py")
    (yard / "harness" / "config").mkdir(parents=True)
    readers = {}
    for number, step in enumerate(steps_of(ROOT)):
        rel = f"scripts/reader_{number}.py"
        (yard / rel).write_text(f'if "{step}" in pipeline.шаги_уровня(настройки, уровень):\n    pass\n',
                                encoding="utf-8")
        readers[step] = rel
    (yard / "harness" / "config" / "pipeline.yaml").write_text(
        "читатели_шагов:\n" + "".join(f"  {s}: {r}\n" for s, r in readers.items()), encoding="utf-8")
    return yard, readers


def edit_pult(yard, old, new):
    path = yard / "harness" / "config" / "pipeline.yaml"
    text = path.read_text(encoding="utf-8")
    assert old in text, f"якоря «{old}» нет"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# 1. Настоящее дерево: у каждого шага панели — читатель.
real_steps = steps_of(ROOT)
check("ШАГИ write-step.py разобраны (8 шагов)", len(real_steps) == 8, f"{real_steps}")
real = troubles(ROOT)
check("настоящее дерево: у каждого шага панели есть читатель в коде", not real, "\n      ".join(real))
check("настоящее дерево: у шага «тесты» есть читатель", not [t for t in real if t.startswith("тесты:")],
      "\n      ".join(t for t in real if t.startswith("тесты:")))

# 2. Живость судьи: годный двор зелёный, каждая поломка краснеет ИМЕННО на своём шаге.
yard, readers = stand()
check("двор: у каждого шага годный читатель → бед нет", not troubles(yard), f"{troubles(yard)}")

yard, readers = stand()
edit_pult(yard, f"  тесты: {readers['тесты']}\n", "")
check("двор: записи «тесты» нет → беда на «тесты»",
      any(t.startswith("тесты:") and "нет" in t for t in troubles(yard)), f"{troubles(yard)}")

yard, readers = stand()
(yard / readers["тесты"]).unlink()
check("двор: файла читателя нет → беда на «тесты»",
      any(t.startswith("тесты:") and "файла нет" in t for t in troubles(yard)), f"{troubles(yard)}")

yard, readers = stand()
(yard / readers["тесты"]).write_text('# pipeline.шаги_уровня(настройки, уровень)\nif "тесты":\n    pass\n',
                                     encoding="utf-8")
check("двор: читатель шаги уровня не зовёт (зов только в комментарии) → беда",
      any(t.startswith("тесты:") and "не зовёт" in t for t in troubles(yard)), f"{troubles(yard)}")

yard, readers = stand()
(yard / readers["мутации"]).write_text("x = шаги_приёмки_задачи(карточка)\n# «мутации»\n", encoding="utf-8")
check("двор: читатель шаг не называет (имя только в комментарии) → беда на «мутации»",
      any(t.startswith("мутации:") and "не называет" in t for t in troubles(yard)), f"{troubles(yard)}")

yard, _ = stand()
(yard / "harness" / "config" / "pipeline.yaml").write_text("шаги: {}\n", encoding="utf-8")
check("двор: раздела «читатели_шагов» нет → беда на каждом шаге", len(troubles(yard)) == len(real_steps),
      f"{troubles(yard)}")

yard, _ = stand()
(yard / "scripts" / "write-step.py").write_text("ШАГИ = ()\n", encoding="utf-8")
check("двор: ШАГИ пусты → беда, а не зелёный", troubles(yard) == ["ШАГИ write-step.py: разбор пуст"],
      f"{troubles(yard)}")

for path in yards:
    shutil.rmtree(path, ignore_errors=True)
print(f"ЧИТАТЕЛИ-ШАГОВ: путей {paths}, неудач {failures}")
sys.exit(1 if failures else 0)
