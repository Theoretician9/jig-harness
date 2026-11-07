#!/usr/bin/env python3
"""Проба места ревью спеки: одна договорённость на двух судей.

БОЛЬНОЙ СЛУЧАЙ: скил работы агентов кладёт отчёты ревьюеров в
.tasks/<id>/review/*.md, а судьи знали только <спека>-REVIEW.md рядом со
спекой — сделанное ревью смене печаталось «НЕТ», гейт краснел. И обратное:
если судьи знают разные места, check-pipeline.py и pipeline.py --состояние
отвечают о ОДНОЙ задаче по-разному.

Модули берутся РЯДОМ с пробой: из копии дерева она судит испорченную копию.
Стенд — свой каталог (корень проекта, карта, журналы через HARNESS_LOG_DIR).
"""
import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
from pathlib import Path

ЗДЕСЬ = Path(__file__).resolve().parent
ЖУРНАЛЫ = tempfile.TemporaryDirectory(prefix="spec-review-logs-")
СТЕНДЫ = tempfile.TemporaryDirectory(prefix="spec-review-stands-")
os.environ["HARNESS_LOG_DIR"] = ЖУРНАЛЫ.name
sys.path.insert(0, str(ЗДЕСЬ / "lib"))


def загрузить(имя, путь):
    спец = importlib.util.spec_from_file_location(имя, путь)
    модуль = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(модуль)
    return модуль


место = загрузить("spec_review", ЗДЕСЬ / "lib" / "spec_review.py")
гейт = загрузить("check_pipeline", ЗДЕСЬ / "check-pipeline.py")
пайплайн = загрузить("pipeline_state", ЗДЕСЬ / "pipeline.py")

ЗАДАЧА = "hr.проба-ревью"
СПЕКА = "docs/specs/проба.md"


def новый_стенд():
    корень = Path(tempfile.mkdtemp(dir=СТЕНДЫ.name))
    (корень / "docs" / "specs").mkdir(parents=True)
    (корень / СПЕКА).write_text("спека\n", encoding="utf-8")
    (корень / "dev-map.yaml").write_text(
        f"lines:\n- id: harness\n  tasks:\n  - id: {ЗАДАЧА}\n    status: wip\n    spec: {СПЕКА}\n",
        encoding="utf-8")
    return корень


def положить(корень, относительный):
    путь = корень / относительный
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text("отчёт ревьюера\n", encoding="utf-8")


def ответ_гейта(корень):
    """True — check-pipeline.py ревью спеки засчитал."""
    return гейт.проверить({"id": ЗАДАЧА, "spec": СПЕКА}, корень=корень, шаги=["ревью_спеки"]) == []


def ответ_состояния(корень):
    """True — pipeline.py --состояние печатает «ревью спеки: есть». Цена и
    шаги уровня подставлены: судится строка ревью, а не история git стенда."""
    пайплайн.КОРЕНЬ = корень
    пайплайн.признаки_задачи = lambda _id: {"строк": 0, "файлов": 0, "коммитов": 1}
    пайплайн.шаги_уровня = lambda _н, _и: ["ревью_спеки"]
    вывод = io.StringIO()
    with contextlib.redirect_stdout(вывод):
        пайплайн.состояние_задачи(ЗАДАЧА)
    строка = re.search(r"^  ревью спеки: (.*)$", вывод.getvalue(), re.M)
    if not строка:
        raise AssertionError(f"в выводе --состояние нет строки ревью спеки: {вывод.getvalue()!r}")
    return строка.group(1).startswith("есть")


# (что положить в стенд, чью задачу спрашиваем, ждём «есть», почему)
СЛУЧАИ = (
    ([], ЗАДАЧА, False, "ревью нет нигде — None"),
    (["docs/specs/проба-REVIEW.md"], ЗАДАЧА, True, "ревью рядом со спекой"),
    ([f".tasks/{ЗАДАЧА}/review/x.md"], ЗАДАЧА, True, "БОЛЬНОЙ СЛУЧАЙ: ревью в .tasks/<id>/review/x.md"),
    ([f".tasks/{ЗАДАЧА}/review/"], ЗАДАЧА, False, "пустой review/ — не ревью"),
    ([f".tasks/{ЗАДАЧА}/review/x.yaml"], ЗАДАЧА, False, ".yaml в review/ не засчитан"),
    ([".tasks/hr.другая/review/x.md"], ЗАДАЧА, False, "ревью чужой задачи не засчитано"),
)


def разложить(корень, пути):
    for относительный in пути:
        if относительный.endswith("/"):
            (корень / относительный).mkdir(parents=True)
        else:
            положить(корень, относительный)


def main() -> int:
    неудач = 0
    for пути, задача, ждём, почему in СЛУЧАИ:
        корень = новый_стенд()
        разложить(корень, пути)
        найдено = место.spec_review_path(корень, задача, СПЕКА)
        ждали_путь = (корень / пути[0]) if ждём else None
        if найдено == ждали_путь:
            print(f"  ок    {почему} → {найдено}")
        else:
            неудач += 1
            print(f"  ПЛОХО {почему}: вышло {найдено}, ждали {ждали_путь}")
    print(f"МЕСТО-РЕВЬЮ: путей {len(СЛУЧАИ)}, неудач {неудач}")

    # Оба судьи на ОДНОЙ задаче стенда: каждый случай — оба ответа равны ожиданию.
    неудач_судей = 0
    for пути, _задача, ждём, почему in СЛУЧАИ:
        корень = новый_стенд()
        разложить(корень, пути)
        гейт_есть, состояние_есть = ответ_гейта(корень), ответ_состояния(корень)
        if гейт_есть is ждём and состояние_есть is ждём:
            print(f"  ок    оба судьи: {почему} → {'есть' if ждём else 'нет'}")
        else:
            неудач_судей += 1
            print(f"  ПЛОХО судьи расходятся ({почему}): check-pipeline {гейт_есть}, "
                  f"pipeline --состояние {состояние_есть}, ждали {ждём}")
    print(f"ОБА-СУДЬИ: путей {len(СЛУЧАИ)}, неудач {неудач_судей}")
    return 1 if неудач or неудач_судей else 0


if __name__ == "__main__":
    sys.exit(main())
