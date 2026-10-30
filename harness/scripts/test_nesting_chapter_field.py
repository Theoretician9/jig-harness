#!/usr/bin/env python3
"""Пробы поля карточки `глава:` (спека правил вложенности, §4.2, §8) — до кода.

Что судит, на своём temp-стенде (своя карта, свой корень бумаг):
  * карточка с `глава: <план>/<глава>` проходит схему карты
    (pre-commit-gate.sh --devmap-only) — поле не ломает валидатор; живость
    вызова: карточка без title на той же карте — код 1;
  * общий разбор карточек scripts/lib/cards.py видит поле: поле() отдаёт
    значение, блок карточки включает строку `глава:`;
  * check-pipeline.py ищет план задачи по главе: `глава: P/…` →
    docs/plans/P.md РАНЬШЕ .tasks/<id>/план.md (§8); подглава — тот же файл;
    файла плана нет — прежний путь .tasks/<id>/план.md; явное поле
    `бумаги.план` по-прежнему сильнее главы; без главы — прежний путь.

Контракт для check-pipeline.py: путь_бумаги(карточка, "план", корень) при
`глава: <план>/…` и существующем <корень>/docs/plans/<план>.md отдаёт этот
путь (имя шага — любое, третий элемент — None); иначе — как сейчас.

Механизмы — рядом либо HARNESS_CHECK_PIPELINE / HARNESS_PRE_COMMIT_GATE
(двери для мутаций копий). Прогон: python3 scripts/test_nesting_chapter_field.py
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECK_PIPELINE = Path(os.environ.get("HARNESS_CHECK_PIPELINE") or HERE / "check-pipeline.py")
GATE = Path(os.environ.get("HARNESS_PRE_COMMIT_GATE") or HERE / "pre-commit-gate.sh")
sys.path.insert(0, str(HERE / "lib"))
import cards                                             # noqa: E402

CHAPTER = "hr.plan-x/data/groups"

checked = failed = 0


def check(name: str, expected, actual) -> None:
    global checked, failed
    checked += 1
    if expected == actual:
        print(f"  ок    {name}")
    else:
        failed += 1
        print(f"  ПЛОХО {name}: ждали «{expected}», вышло «{actual}»")


def map_text(chapter_line: bool = True, title: bool = True) -> str:
    lines = ["version: 1", "# комментарий карты", "lines:", "  - id: стенд", "    layer: infra",
             '    name: "Стенд"', "    tasks:",
             "      - id: hr.task-a",
             "        status: plan"]
    if title:
        lines.append('        title: "задача с главой"')
    lines += ['        summary: "карточка стенда"',
              '        глубина: { уровень: обычная, характер: разработка, кем: проба, '
              'когда: "2026-10-30" }']
    if chapter_line:
        lines.append(f"        глава: {CHAPTER}")
    lines += ["      - id: hr.task-b", "        status: plan", '        title: "соседняя"',
              '        summary: "без главы"', "layers: []", "queue: []", ""]
    return "\n".join(lines)


def load_check_pipeline():
    spec = importlib.util.spec_from_file_location("check_pipeline_probe", CHECK_PIPELINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def gate_schema(path: Path) -> tuple:
    proc = subprocess.run(["bash", str(GATE), "--devmap-only", str(path)],
                          capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout + proc.stderr


def case_schema(tmp: Path) -> None:
    print("схема карты: поле глава")
    good = tmp / "карта-глава.yaml"
    good.write_text(map_text(), encoding="utf-8")
    code, out = gate_schema(good)
    check("схема: карточка с глава: проходит --devmap-only", 0, code)
    bad = tmp / "карта-без-title.yaml"
    bad.write_text(map_text(title=False), encoding="utf-8")
    check("схема: живость — карточка без title на той же карте даёт код 1", 1,
          gate_schema(bad)[0])


def case_cards_lib(tmp: Path) -> None:
    print("разбор карточек cards.py")
    text = map_text()
    found = cards.блок(text, "hr.task-a")
    body = found[0] if found else ""
    check("cards: поле глава читается", CHAPTER, cards.поле(body, "глава"))
    check("cards: блок карточки включает строку глава", True, f"глава: {CHAPTER}" in body)
    other = cards.блок(text, "hr.task-b")
    check("cards: соседняя карточка без поля глава", None,
          cards.поле(other[0] if other else "", "глава"))


def case_check_pipeline(tmp: Path) -> None:
    print("check-pipeline: путь плана по главе")
    module = load_check_pipeline()
    root = tmp / "корень"
    (root / "docs" / "plans").mkdir(parents=True)
    plan_file = root / "docs" / "plans" / "hr.plan-x.md"
    plan_file.write_text("# план\n", encoding="utf-8")
    task_plan = root / ".tasks" / "hr.task-a" / "план.md"
    task_plan.parent.mkdir(parents=True)
    task_plan.write_text("# план задачи\n", encoding="utf-8")
    card = {"id": "hr.task-a", "status": "wip", "глава": CHAPTER}

    path, _, trouble = module.путь_бумаги(card, "план", root)
    check("глава: план из docs/plans/<план>.md раньше .tasks/<id>/план.md",
          (plan_file, None), (Path(path) if path else None, trouble))
    path, _, _ = module.путь_бумаги({**card, "глава": "hr.plan-x/data"}, "план", root)
    check("глава без подглавы: тот же файл плана", plan_file, Path(path) if path else None)
    path, _, _ = module.путь_бумаги({"id": "hr.task-a", "status": "wip"}, "план", root)
    check("без главы: прежний путь .tasks/<id>/план.md", task_plan,
          Path(path) if path else None)
    path, _, _ = module.путь_бумаги({**card, "глава": "hr.нет-плана/data"}, "план", root)
    check("файла плана главы нет: прежний путь .tasks/<id>/план.md", task_plan,
          Path(path) if path else None)
    named = root / "docs" / "другой-план.md"
    named.write_text("# назван картой\n", encoding="utf-8")
    path, _, _ = module.путь_бумаги({**card, "бумаги": {"план": "docs/другой-план.md"}},
                                    "план", root)
    check("бумаги.план сильнее главы", named, Path(path) if path else None)
    path, _, _ = module.путь_бумаги(card, "контракт", root)
    check("глава не трогает другие шаги (контракт)",
          root / ".tasks" / "hr.task-a" / "pretask.yaml", Path(path) if path else None)


CASES = (case_schema, case_cards_lib, case_check_pipeline)


def main() -> int:
    global checked, failed
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
