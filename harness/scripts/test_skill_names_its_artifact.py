#!/usr/bin/env python3
"""Проба «скил велит оставить тот файл, которого ждёт его живая проверка».

harness/config/skill-artifacts.yaml называет файл, который скил обязан
оставить в каталоге задачи, а живая проверка класса E без этого файла вызов не
засчитывает. Если текст скила файла не называет, работа по скилу его не
оставит никогда, и предикат недостижим настоящей работой.

Скил судится по тексту, собранному генератором ИЗ ГЛАВ (render), а не по
SKILL.md на диске: правка обязана жить в источнике, иначе перегенерация её
сотрёт. Судится ИМЯ файла: главы пишут каталог задачи по-разному
(`.tasks/<id>/`, «каталог задачи»), а имя файла одно.

Судятся маски вида `.tasks/{задача}/<файл>` без шаблонных знаков; маски с `*`
и вне каталога задачи называются числом, а не пропускаются молча. Пустой набор
судимых — провал: зелёный на пустых данных ничего не доказывает.

Дверь для мутаций: PROBE_CHAPTER_FILE — путь к копии главы; судится каталог
глав, в котором она лежит.

Прогон:
    python3 scripts/test_skill_names_its_artifact.py
Код возврата: 0 — каждый судимый скил называет свой файл, 1 — нет.
"""
import importlib.util
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TASK_DIR_PREFIX = ".tasks/{задача}/"
GLOB_CHARS = set("*?[")


def load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_skills", ROOT / "harness" / "BUILD-SKILLS.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def chapters_dir(builder):
    chapter_file = os.environ.get("PROBE_CHAPTER_FILE")
    return Path(chapter_file).parent if chapter_file else builder.find_chapters_dir()


def judged_artifacts():
    """(судимые {скил: имя файла}, несудимые [скил]) из данных артефактов."""
    data = yaml.safe_load(
        (ROOT / "harness" / "config" / "skill-artifacts.yaml").read_text(encoding="utf-8"))
    judged, skipped = {}, []
    for skill, entry in ((data or {}).get("скилы") or {}).items():
        mask = str((entry or {}).get("артефакт") or "")
        rest = mask[len(TASK_DIR_PREFIX):]
        if mask.startswith(TASK_DIR_PREFIX) and rest and not GLOB_CHARS & set(rest):
            judged[skill] = Path(rest).name
        else:
            skipped.append(skill)
    return judged, skipped


def failures(rendered, judged):
    found = []
    for skill, file_name in sorted(judged.items()):
        text = rendered.get(skill)
        if text is None:
            found.append(f"{skill}: генератор такого скила не собирает")
        elif file_name not in text:
            found.append(f"{skill}: текст скила не называет файл «{file_name}» "
                         f"(skill-artifacts.yaml его ждёт)")
    return found


def main():
    builder = load_builder()
    rendered = builder.render(chapters_dir(builder))
    judged, skipped = judged_artifacts()
    found = failures(rendered, judged) if judged else [
        "судимых масок 0 — проба ничего не проверила"]
    for line in found:
        print("ПЛОХО · " + line)
    print(f"судимых скилов {len(judged)}, несудимых масок {len(skipped)} "
          f"({', '.join(sorted(skipped))}), провалов {len(found)}")
    sys.exit(1 if found else 0)


if __name__ == "__main__":
    main()
