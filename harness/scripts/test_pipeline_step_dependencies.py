#!/usr/bin/env python3
"""Проба «шаг без своей опоры не требуется»: зависимости шагов — данными пульта.

Приказ владельца 17.11.2026 (память/owner-orders-17-11-verbatim.md): выключил
тесты — мутации выключаются автоматически. Мутация судит пробы; нет шага проб —
мутации судить нечего. Носитель — ключ пульта «зависимости_шагов» (шаг → шаги,
без которых он не живёт), а не `if шаг == "мутации"` в коде.

Судей шагов уровня два: scripts/pipeline.py (печать, состояние, панель) и
scripts/check-pipeline.py (переход в test и приёмка; через него acceptance.py
держит мутации). Зависимость обязана действовать в ОБОИХ: иначе панель покажет
«мутации выключены», а гейт закрытия продолжит их требовать.

Стенд — свой pipeline.yaml во временном каталоге; живой пульт только читается.
"""
import ast
import importlib.util
import os
import subprocess
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
_YARD = Path(tempfile.mkdtemp(prefix="proba-zavisimosti-shagov-"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


price = load("pipeline_under_test", ROOT / "scripts" / "pipeline.py")
gate = load("check_pipeline_under_test", ROOT / "scripts" / "check-pipeline.py")

paths = 0
failures = 0


def check(name, ok, details=""):
    global paths, failures
    paths += 1
    if ok:
        print(f"  ок    {name}")
    else:
        failures += 1
        print(f"  ПЛОХО {name} — {details}")


def stand(text):
    path = _YARD / f"pipeline-{paths}.yaml"
    path.write_text(text, encoding="utf-8")
    return price.конфиг(path)


def judged(judge, settings, level):
    try:
        return judge(settings, level)
    except Exception as err:            # noqa: BLE001
        return f"исключение {type(err).__name__}: {err}"


JUDGES = (("pipeline.шаги_уровня", price.шаги_уровня),
          ("check-pipeline.шаги_уровня", gate.шаги_уровня))

# Вторая зависимость (безопасность ← план) доказывает, что правило — данные, а не
# частный случай «мутации»: её в коде не напишет никто.
STAND = """
зависимости_шагов:
  мутации: [тесты]
  безопасность: [план]
шаги:
  мелкая: [мутации, безопасность, план]
  обычная: [тесты, мутации]
  крупная: [мутации]
  хакатон: [безопасность, тесты]
"""
# уровень → что обязано остаться (порядок пульта сохраняется)
EXPECTED = {
    "мелкая": ["безопасность", "план"],
    "обычная": ["тесты", "мутации"],
    "крупная": [],
    "хакатон": ["тесты"],
}

settings = stand(STAND)
for judge_name, judge in JUDGES:
    for level, expected in EXPECTED.items():
        got = judged(judge, settings, level)
        check(f"{judge_name}({level}) → {expected}", got == expected, f"вышло {got!r}")

# Случаи контракта дословно: [мутации, безопасность] → [безопасность]; [тесты, мутации] → оба.
contract = stand("зависимости_шагов: {мутации: [тесты]}\n"
                 "шаги: {мелкая: [мутации, безопасность], обычная: [тесты, мутации]}\n")
for level, expected in (("мелкая", ["безопасность"]), ("обычная", ["тесты", "мутации"])):
    for judge_name, judge in JUDGES:
        got = judged(judge, contract, level)
        check(f"контракт: {judge_name}({level}) → {expected}", got == expected, f"вышло {got!r}")

# Живой пульт: зависимость «мутации ← тесты» записана данными (только чтение).
live = yaml.safe_load((ROOT / "harness" / "config" / "pipeline.yaml").read_text(encoding="utf-8")) or {}
deps = live.get("зависимости_шагов")
check("живой pipeline.yaml: зависимости_шагов.мутации содержит «тесты»",
      isinstance(deps, dict) and "тесты" in (deps.get("мутации") or []), f"зависимости_шагов = {deps!r}")

# Частного случая в коде нет: слово «мутации» не стоит в коде тел судей.
for rel in ("scripts/pipeline.py", "scripts/check-pipeline.py"):
    # ast.unparse теряет комментарии; строку документации снимаем сами — судим только код.
    found = [node for node in ast.walk(ast.parse((ROOT / rel).read_text(encoding="utf-8")))
             if isinstance(node, ast.FunctionDef) and node.name == "шаги_уровня"]
    body = found[0].body if found else []
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    code = "\n".join(ast.unparse(node) for node in body)
    check(f"{rel}: шаги_уровня без частного «мутации» в коде", bool(found) and "мутации" not in code,
          "функции нет" if not found else f"код: {code[:200]}")

# CR-8 (ревью кода 20.11): цепочка опор. ревью_кода ← план ← спека; «спека» снята —
# «план» без опоры, значит и «ревью_кода» без опоры. Один проход по СЫРОМУ списку
# оставлял «ревью_кода»: его опора «план» стоит в сыром списке.
# Поломка, что оставит зелёным: фильтр до неподвижной точки, но опоры судятся по
# сырому списку (снова один проход) — ловит цепочка; «всякая опора» вместо «хотя бы
# одна» — ловит шаг с двумя опорами.
chain = stand("зависимости_шагов:\n"
              "  план: [спека]\n"
              "  ревью_кода: [план]\n"
              "  мутации: [тесты, безопасность]\n"
              "шаги:\n"
              "  мелкая: [план, ревью_кода, тесты]\n"
              "  обычная: [безопасность, мутации]\n")
for level, expected, what in (("мелкая", ["тесты"], "цепочка ревью_кода←план←спека, «спека» снята"),
                              ("обычная", ["безопасность", "мутации"],
                               "две опоры, одна снята — шаг живёт (нужна хотя бы одна)")):
    for judge_name, judge in JUDGES:
        got = judged(judge, chain, level)
        check(f"CR-8: {judge_name}({level}): {what} → {expected}", got == expected, f"вышло {got!r}")


def selftest_on_stand(edit, break_code=None):
    """Самотест pipeline.py на КОПИИ дерева: свой pipeline.yaml, свои harness.conf и LOG_DIR.

    КОРЕНЬ самотеста считается от его файла, поэтому копия pipeline.py читает пульт
    копии; правка `edit` — вставкой текста по строке, не yaml.dump. `break_code` —
    поломка КОПИИ pipeline.py (текст → текст): судит, видит ли её самотест."""
    yard = Path(tempfile.mkdtemp(prefix="proba-samotest-pulta-", dir=_YARD))
    (yard / "scripts").mkdir()
    code_text = (ROOT / "scripts" / "pipeline.py").read_text(encoding="utf-8")
    (yard / "scripts" / "pipeline.py").write_text(break_code(code_text) if break_code else code_text,
                                                  encoding="utf-8")
    shutil.copytree(ROOT / "scripts" / "lib", yard / "scripts" / "lib",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (yard / "harness" / "config").mkdir(parents=True)
    text = edit((ROOT / "harness" / "config" / "pipeline.yaml").read_text(encoding="utf-8"))
    (yard / "harness" / "config" / "pipeline.yaml").write_text(text, encoding="utf-8")
    (yard / "log").mkdir()
    (yard / "harness.conf").write_text('MIGRATIONS_GLOB=""\nPROD_PATHS=""\nGUARD_PATHS=""\n',
                                       encoding="utf-8")
    (yard / "install.conf").write_text(f'LOG_DIR="{yard / "log"}"\n', encoding="utf-8")
    env = dict(os.environ, HARNESS_CONF=str(yard / "harness.conf"),
               HARNESS_INSTALL_CONF=str(yard / "install.conf"), LOG_DIR=str(yard / "log"),
               HARNESS_LOG_DIR=str(yard / "log"), HARNESS_HOME=str(yard))
    done = subprocess.run([sys.executable, str(yard / "scripts" / "pipeline.py"), "--selftest"],
                          capture_output=True, text=True, env=env, timeout=300, cwd=str(yard))
    steps = (yaml.safe_load(text) or {}).get("шаги") or {}
    bad = [line for line in (done.stdout + done.stderr).splitlines() if "ПЛОХО" in line]
    return done.returncode, steps, bad or [(done.stdout + done.stderr).strip()[-200:]]


def drop_tests(level):
    """Снять «тесты» с уровня `level` в тексте пульта: ровно то, что делает кнопка панели."""
    def edit(text):
        lines = text.splitlines(keepends=True)
        hits = [i for i, line in enumerate(lines) if line.startswith(f"  {level}: [")]
        assert len(hits) == 1, f"в живом пульте нет одной строки уровня «{level}»"
        head, items = lines[hits[0]].split("[", 1)
        kept = [s.strip() for s in items.split("]", 1)[0].split(",") if s.strip() != "тесты"]
        lines[hits[0]] = f"{head}[{', '.join(kept)}]\n"
        return "".join(lines)
    return edit


# CR-1 (ревью кода 20.11, critical): самотест pipeline.py сверял шаги_уровня с СЫРЫМ
# списком пульта. Владелец снимает «тесты» у уровня с «мутации» — шаги_уровня честно
# отбрасывает «мутации», а самотест (ворота «пайплайн», gates.sh) краснеет.
# Поломка, что оставит зелёным: самотест вовсе не сверяет шаги уровня (тогда его
# красного не видно никогда) — потому база стенда судится тоже; стенд, на котором
# «тесты» и «мутации» не встречаются на одном уровне.
code, steps, bad = selftest_on_stand(lambda text: text)
check("CR-1: стенд-копия пульта как есть → pipeline.py --selftest rc 0", code == 0, f"rc {code} · {bad}")
for level in ("мелкая", "обычная"):
    code, steps, bad = selftest_on_stand(drop_tests(level))
    on_stand = steps.get(level) or []
    check(f"CR-1: стенд: у «{level}» сняты «тесты», «мутации» остались",
          "тесты" not in on_stand and "мутации" in on_stand, f"шаги {on_stand!r}")
    check(f"CR-1: БОЛЬНОЙ СЛУЧАЙ: у «{level}» сняты «тесты» → pipeline.py --selftest rc 0",
          code == 0, f"rc {code} · {bad}")

# CR2-5 / SR2-3 (ревью 20.11, итерация 2): самотест pipeline.py обязан видеть отбор, а
# не только «ничего чужого». Проверка `all(...)` по выданным шагам истинна на пустом
# списке: фильтр, снявший ВСЁ при заданных опорах, и фильтр, снявший законный шаг,
# самотест пропускал (мутация N4 ревьюера выжила). Поломки ставятся в КОПИЮ
# pipeline.py стенда; живой файл не трогается. Пульт стенда — копия живого: у
# «обычная» и «крупная» «тесты» и «мутации» стоят вместе, отбор ничего не снимает.
# Поломка, что оставит зелёным: самотест, который судит только «список не пуст»
# (ловит вторая поломка — снят один «тесты», прочее осталось); самотест, который
# сверяет лишь «мелкая» (у неё снятый «тесты» тоже виден — ловят все три уровня).
FILTER_LINE = ("                 if not опоры.get(шаг) or any(опора in шаги for опора in опоры[шаг])]")
BREAKS = (
    ("фильтр снял всё, когда опоры заданы", "                 if not опоры]"),
    ("фильтр снял законный «тесты», когда опоры заданы",
     "                 if (not опоры or шаг != \"тесты\") and (not опоры.get(шаг)"
     " or any(опора in шаги for опора in опоры[шаг]))]"),
)
for what, broken in BREAKS:
    def break_code(text, broken=broken):
        if text.count(FILTER_LINE) != 1:
            return text + "\nraise SystemExit('проба: якорь поломки не найден в pipeline.py')\n"
        return text.replace(FILTER_LINE, broken)
    code, steps, bad = selftest_on_stand(lambda text: text, break_code)
    check(f"CR2-5: БОЛЬНОЙ СЛУЧАЙ: копия pipeline.py — {what} → pipeline.py --selftest не 0",
          code != 0 and "якорь поломки" not in " ".join(bad), f"rc {code} · {bad}")

# CR2-7 и пустой пульт (мутации К-2, 20.11): самотест pipeline.py обязан краснеть, когда
# опоры шага записаны строкой (`мутации: тесты` перебирается по буквам, и шаг молча
# отпадает) и когда ни на одном уровне нет шагов (сверка отбора тогда идёт вхолостую).
# Обе проверки выживали мутацией `if …:` → `if False:` — проба их не судила.
# Поломка, что оставит зелёным: самотест, который форму опор не судит (ловит первый
# стенд), и самотест, для которого пустой пульт — не провал (ловит второй).
def deps_as_string(text):
    assert text.count("\n  мутации: [тесты]\n") == 1, "в живом пульте нет строки опор «мутации»"
    return text.replace("\n  мутации: [тесты]\n", "\n  мутации: тесты\n")


def all_levels_empty(text):
    lines = text.splitlines(keepends=True)
    start = lines.index("шаги:\n")
    for i in range(start + 1, len(lines)):
        if not lines[i].startswith("  "):
            break
        if not lines[i].lstrip().startswith("#"):
            lines[i] = lines[i].split(":", 1)[0] + ": []\n"
    return "".join(lines)


for what, edit in (("опоры «мутации» строкой, не списком", deps_as_string),
                   ("на всех уровнях пульта шагов нет", all_levels_empty)):
    code, steps, bad = selftest_on_stand(edit)
    check(f"CR2-7: БОЛЬНОЙ СЛУЧАЙ: стенд: {what} → pipeline.py --selftest не 0",
          code != 0 and bool(steps), f"rc {code} · шаги стенда {steps!r} · {bad}")

# CR2-1 (ревью кода 20.11, итерация 2): второй судья без пульта или без уровня в разделе
# «шаги» держит ПРЕЖНЕЕ умолчание (спека §3.1 п.7), а не []: сломанный пульт не снимает
# требований с крупной работы — тот же класс, что CR-2 закрыла в гейте тестов.
# Умолчание — у check-pipeline, НЕ в pipeline.УМОЛЧАНИЯ: иначе гейт тестов без пульта
# увидит «мелкая: []» и назовёт тесты выключенными (CR-2 вернётся) — это держит
# test_tests_first_step_off.py.
# Поломка, что оставит зелёным: умолчание только при отсутствии раздела «шаги», а не
# уровня в нём (ловит пульт с «шаги» без «крупная»); умолчание только для «крупная»
# (ловит «обычная» → [тесты]).
OLD_DEFAULT = {"мелкая": [], "обычная": ["тесты"],
               "крупная": ["спека", "ревью_спеки", "контракт", "тесты"]}
no_pult = price.конфиг(_YARD / "нет-такого-пульта.yaml")
no_section = stand("зависимости_шагов: {мутации: [тесты]}\n")
no_level = stand("зависимости_шагов: {мутации: [тесты]}\nшаги: {мелкая: [тесты]}\n")
for what, settings_x in (("pipeline.yaml нет", no_pult), ("нет раздела «шаги»", no_section),
                         ("в «шаги» нет уровня", no_level)):
    for level in ("крупная", "обычная"):
        got = judged(gate.шаги_уровня, settings_x, level)
        check(f"CR2-1: БОЛЬНОЙ СЛУЧАЙ: {what} → check-pipeline.шаги_уровня({level}) — прежнее умолчание",
              got == OLD_DEFAULT[level], f"вышло {got!r}, ждём {OLD_DEFAULT[level]!r}")
card = {"id": "проба-cr2-1", "when": {"started": "2026-11-20"}}
got = judged(lambda s, lvl: gate.шаги_для_задачи(s, lvl, card), no_pult, "крупная")
check("CR2-1: pipeline.yaml нет → check-pipeline.шаги_для_задачи(крупная) требует спеку и ревью спеки",
      isinstance(got, list) and {"спека", "ревью_спеки", "контракт", "тесты"} <= set(got), f"вышло {got!r}")
check("CR2-1: умолчание не переехало в pipeline.py: pipeline.шаги_уровня без пульта — []",
      price.шаги_уровня(no_pult, "мелкая") == [] and price.шаги_уровня(no_pult, "крупная") == [],
      f"мелкая {price.шаги_уровня(no_pult, 'мелкая')!r}, крупная {price.шаги_уровня(no_pult, 'крупная')!r}")

shutil.rmtree(_YARD, ignore_errors=True)
print(f"ЗАВИСИМОСТИ-ШАГОВ: путей {paths}, неудач {failures}")
sys.exit(1 if failures else 0)
