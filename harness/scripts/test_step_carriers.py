#!/usr/bin/env python3
"""Проба «носители имени шага согласны» — пишется ДО правки носителей.

Шаг пайплайна называют много мест (кнопка панели, колонка доски, маска
артефакта, строка таблицы моделей, скил…). Источник истины — `ШАГИ` в
write-step.py; каждый носитель РАЗБИРАЕТСЯ (ast, safe_load, регулярка по
функции), а не ищется словом. Пустой разбор — провал, не зелёный
([[a-fix-makes-its-own-probe-unfalsifiable]]). Исключения названы поимённо с
причиной.

Проба знает раскладку ПРОЕКТА (scripts/, harness/config, harness/panel).
"""
import ast
import re
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FILES = [
    "scripts/write-step.py", "scripts/check-pipeline.py", "scripts/hooks/skill_log.sh",
    "scripts/test_build_board.py", "scripts/test_write_step.py",
    "harness/panel/state.py", "harness/panel/test_task_board.py", "harness/BUILD-SKILLS.py",
    "harness/config/pipeline.yaml", "harness/config/board.yaml", "harness/config/models.yaml",
    "harness/config/model-artifacts.yaml", "harness/config/system-jobs.yaml",
    "harness/config/skill-artifacts.yaml",
]


class EmptyParse(Exception):
    pass


def text_of(root, rel):
    return (Path(root) / rel).read_text(encoding="utf-8")


def tree_of(root, rel):
    return ast.parse(text_of(root, rel))


def assigned(root, rel, name):
    for node in ast.walk(tree_of(root, rel)):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name
                                                for t in node.targets):
            return node.value
    raise EmptyParse(f"{rel}: нет присваивания {name}")


def literal_strings(node):
    items = node.keys if isinstance(node, ast.Dict) else node.elts
    return {i.value for i in items if isinstance(i, ast.Constant) and isinstance(i.value, str)}


def steps_of(root):
    return literal_strings(assigned(root, "scripts/write-step.py", "ШАГИ"))


def parse_state(root):
    return literal_strings(assigned(root, "harness/panel/state.py", "ПОЯСНЕНИЯ_ШАГОВ"))


def parse_artifact(root):
    for node in ast.walk(tree_of(root, "scripts/check-pipeline.py")):
        if isinstance(node, ast.FunctionDef) and node.name == "_где_артефакт":
            return {c.value for cmp_ in ast.walk(node) if isinstance(cmp_, ast.Compare)
                    and isinstance(cmp_.left, ast.Name) and cmp_.left.id == "шаг"
                    for c in cmp_.comparators if isinstance(c, ast.Constant)}
    raise EmptyParse("нет функции _где_артефакт")


def parse_pipeline_comment(root):
    text = text_of(root, "harness/config/pipeline.yaml")
    start = text.find("Имена шагов:")
    if start < 0:
        raise EmptyParse("нет строки «Имена шагов:»")
    chunk = text[start + len("Имена шагов:"):start + 400]
    chunk = re.sub(r"\n#\s*", " ", chunk).split(". ")[0]
    return {p.strip() for p in chunk.split("·") if p.strip()}


def parse_board(root):
    data = yaml.safe_load(text_of(root, "harness/config/board.yaml")) or {}
    return set(data.get("колонки") or [])


def skill_kinds(root):
    """skill → вид работы из вид_работы() в skill_log.sh."""
    text = text_of(root, "scripts/hooks/skill_log.sh")
    body = text[text.find("вид_работы() {"):]
    body = body[:body.find("\n}\n")]
    pairs = {}
    for match in re.finditer(r'^\s*([\w|\-а-яё]+)\)\s+echo\s+"([^"]+)"', body, re.M):
        for skill in match.group(1).split("|"):
            pairs[skill] = match.group(2)
    return pairs


def parse_skill_log(root):
    return set(skill_kinds(root).values())


def parse_models(root):
    own = set((yaml.safe_load(text_of(root, "harness/config/models.yaml")) or {}).get("модели") or {})
    jobs = yaml.safe_load(text_of(root, "harness/config/system-jobs.yaml")) or {}
    delivered = {str(p.get("ключ")).split(".", 1)[1] for p in jobs.get("живые_правки") or []
                 if str(p.get("ключ") or "").startswith("модели.")}
    return own | delivered


def parse_masks(root):
    data = yaml.safe_load(text_of(root, "harness/config/model-artifacts.yaml")) or {}
    return {str(v.get("вид")) for v in (data.get("артефакты") or {}).values() if isinstance(v, dict)}


def parse_build_order(root):
    return literal_strings(assigned(root, "scripts/test_build_board.py", "PIPELINE_ORDER"))


def parse_task_columns(root):
    return {c.replace(" ", "_") for c in literal_strings(
        assigned(root, "harness/panel/test_task_board.py", "COLUMNS"))}


def parse_delivery(root):
    """Шаги, которые доставка включает командами `write-step.py <уровень> <шаг> включить`."""
    jobs = yaml.safe_load(text_of(root, "harness/config/system-jobs.yaml")) or {}
    found = set()
    for fix in jobs.get("живые_правки") or []:
        for command in fix.get("команды") or []:
            match = re.search(r"write-step\.py\s+\S+\s+(\S+)\s+включить", command)
            if match:
                found.add(match.group(1))
    return found


# Шаги, что стояли в пульте владельца ДО заведения доставки: их она не включает.
BEFORE_DELIVERY = ("спека", "ревью_спеки", "контракт", "план", "ревью_кода", "тесты", "мутации")

# носитель → (файлы, разбор, исключения шаг → причина)
STEP_CARRIERS = {
    "кнопка панели (state.py)": (["harness/panel/state.py"], parse_state, {}),
    "артефакт шага (check-pipeline.py)": (["scripts/check-pipeline.py"], parse_artifact,
                                          {"тесты": "судится гейтом тестов по коммитам, файла нет"}),
    "имена шагов в пульте (pipeline.yaml)": (["harness/config/pipeline.yaml"], parse_pipeline_comment, {}),
    "доставка в установки (system-jobs.yaml живые_правки)": (
        ["harness/config/system-jobs.yaml"], parse_delivery,
        {step: "включён в пульте до заведения доставки" for step in BEFORE_DELIVERY}),
    "колонки доски (board.yaml)": (["harness/config/board.yaml"], parse_board, {}),
    "вид работы скила (skill_log.sh)": (["scripts/hooks/skill_log.sh"], parse_skill_log,
                                        {"мутации": "скила у шага нет: итог кладёт mutation-probe.sh"}),
    "таблица моделей (models.yaml/живые_правки)": (
        ["harness/config/models.yaml", "harness/config/system-jobs.yaml"], parse_models, {}),
    "маска артефакта (model-artifacts.yaml)": (["harness/config/model-artifacts.yaml"], parse_masks,
                                               {"мутации": "итог пишет скрипт, не помощник"}),
    "порядок пробы доски (test_build_board.PIPELINE_ORDER)": (
        ["scripts/test_build_board.py"], parse_build_order, {}),
    "колонки пробы панели (test_task_board.COLUMNS)": (
        ["harness/panel/test_task_board.py"], parse_task_columns, {}),
}


def parse_build_skills(root):
    return literal_strings(assigned(root, "harness/BUILD-SKILLS.py", "SKILLS"))


def parse_skill_artifacts(root):
    data = yaml.safe_load(text_of(root, "harness/config/skill-artifacts.yaml")) or {}
    return set(data.get("скилы") or {})


SKILL_CARRIERS = {
    "словарь скилов (BUILD-SKILLS.py)": (["harness/BUILD-SKILLS.py"], parse_build_skills),
    "артефакты скилов (skill-artifacts.yaml)": (["harness/config/skill-artifacts.yaml"], parse_skill_artifacts),
}


def discrepancies(root):
    """(расхождения, число носителей). Каждое — строка с ИМЕНЕМ носителя."""
    found, count = [], 0
    steps = steps_of(root)
    if not steps:
        return ["write-step.ШАГИ: пусто"], 0
    for name, (_files, parse, exceptions) in STEP_CARRIERS.items():
        count += 1
        try:
            have = parse(root)
        except (EmptyParse, OSError, SyntaxError, yaml.YAMLError) as err:
            found.append(f"{name}: не разобран ({err})")
            continue
        if not have:
            found.append(f"{name}: разбор пуст")
            continue
        for step in sorted(steps - set(exceptions)):
            if step not in have:
                found.append(f"{name}: нет шага «{step}»")
    skills = {s for s, kind in skill_kinds(root).items() if kind in steps}
    for name, (_files, parse) in SKILL_CARRIERS.items():
        count += 1
        try:
            have = parse(root)
        except (EmptyParse, OSError, SyntaxError, yaml.YAMLError) as err:
            found.append(f"{name}: не разобран ({err})")
            continue
        if not have:
            found.append(f"{name}: разбор пуст")
            continue
        found += [f"{name}: нет скила «{s}»" for s in sorted(skills - have)]
    return found, count


RESULTS = []
_CLEAN = []


def stand():
    path = Path(tempfile.mkdtemp(prefix="носители-стенд-"))
    _CLEAN.append(path)
    for rel in FILES:
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, path / rel)
    return path


def case_real_tree():
    found, count = discrepancies(ROOT)
    assert not found, "\n    ".join(found)
    assert count >= 12, f"носителей {count}"
    assert len(steps_of(ROOT)) == 8, steps_of(ROOT)


def case_remove_from(name, files):
    def case():
        root = stand()
        for rel in files:
            path = root / rel
            path.write_text(path.read_text(encoding="utf-8").replace("безопасность", "ЗАМЕНА")
                            .replace("security-check", "ЗАМЕНА-скил"), encoding="utf-8")
        found, _ = discrepancies(root)
        assert any(line.startswith(name) for line in found), \
            f"удаление шага из «{name}» не замечено: {found}"
    return case


def case_empty_parse_is_red():
    root = stand()
    (root / "harness/config/board.yaml").write_text("колонки: []\n", encoding="utf-8")
    found, _ = discrepancies(root)
    assert any(line.startswith("колонки доски") and "разбор пуст" in line for line in found), found


def case_broken_carrier_is_red():
    root = stand()
    (root / "harness/panel/state.py").write_text("ПОЯСНЕНИЯ_ШАГОВ = (\n", encoding="utf-8")
    found, _ = discrepancies(root)
    assert any(line.startswith("кнопка панели") and "не разобран" in line for line in found), found


def case_no_steps_is_red():
    root = stand()
    path = root / "scripts/write-step.py"
    path.write_text("ШАГИ = ()\n", encoding="utf-8")
    found, _ = discrepancies(root)
    assert found and "ШАГИ: пусто" in found[0], found


def run_case(name, func):
    try:
        func()
        RESULTS.append(True)
        print(f"  ок    {name}")
    except Exception as err:        # noqa: BLE001
        RESULTS.append(False)
        print(f"  ПЛОХО {name}: {err or err.__class__.__name__}")


def main():
    run_case("настоящее дерево: расхождений 0, носителей >= 12, шагов 8", case_real_tree)
    carrier_files = {n: f for n, (f, *_rest) in {**STEP_CARRIERS, **SKILL_CARRIERS}.items()}
    for name, files in carrier_files.items():
        run_case(f"шаг убран из носителя: {name}", case_remove_from(name, files))
    run_case("пустой разбор носителя — красный", case_empty_parse_is_red)
    run_case("носитель не разбирается — красный", case_broken_carrier_is_red)
    run_case("источник ШАГИ пуст — красный", case_no_steps_is_red)
    for path in _CLEAN:
        shutil.rmtree(path, ignore_errors=True)
    found, count = discrepancies(ROOT)
    failed = RESULTS.count(False)
    print(f"носителей {count}, шагов {len(steps_of(ROOT))}, расхождений {len(found)}")
    print(f"НОСИТЕЛИ: случаев {len(RESULTS)}, неудач {failed}")
    return 1 if failed or found else 0


if __name__ == "__main__":
    sys.exit(main())
