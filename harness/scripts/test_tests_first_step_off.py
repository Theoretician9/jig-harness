#!/usr/bin/env python3
"""Проба «шаг „тесты“ выключен — гейт тестов до кода не требует».

Приказ владельца 17.11.2026 (память/owner-orders-17-11-verbatim.md): выключатель
шага в панели обязан выключать шаг КОДОМ. Шаг «тесты» держит гейт
scripts/check-tests-first.py, а он читал только раздел «тесты_до_кода» и шаги
уровня не смотрел: выключение кнопкой не меняло ничего.

Уровень коммита — тот же, что у суда пайплайна: цена индекса (pipeline.py).
Стенд — свой git-репозиторий во временном каталоге с КОПИЕЙ гейта, pipeline.py
и scripts/lib: КОРЕНЬ гейта считается от его файла, так стенд получает свой
pipeline.yaml без двери. Свои harness.conf, install.conf и LOG_DIR (машинное
состояние строгости гейта пишется туда, а не в боевой каталог).

Коммит стенда: один новый модуль app/lib/pump.py без теста, одна строка —
цена 1 (новый файл), уровень «мелкая».
"""
import datetime
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Дверь мутаций: гейт стенда копируется отсюда (мутация судит КОПИЮ, не живой файл).
GATE = Path(os.environ.get("TESTS_FIRST_UNDER_TEST") or ROOT / "scripts" / "check-tests-first.py")
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="проба", GIT_AUTHOR_EMAIL="p@stand.invalid",
               GIT_COMMITTER_NAME="проба", GIT_COMMITTER_EMAIL="p@stand.invalid",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
# Строгость красная: жёлтый код 77 не отличал бы «судил и нашёл» от обкатки.
TAIL = """
исключения_путей: [scripts/, harness/]
тесты_до_кода: {строгость: красный}
"""

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


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=GIT_ENV)


def pult_text(levels):
    """Пульт стенда: раздел «шаги» по `levels` и хвост строгости."""
    return "шаги:\n" + "".join(f"  {level}: [{', '.join(steps)}]\n" for level, steps in levels.items()) + TAIL


# Карта стенда: одна wip-карточка; её уровень — глубина.уровень (хакатон или нет).
DEV_MAP = """version: 1
tasks:
  - id: pr.stand
    title: проба
    status: wip
    глубина: {{уровень: {level}}}
"""


def stand(pult, card_level=None, hackathon_until=None, prod_paths=""):
    """Репозиторий стенда с пультом `pult` (None — файла нет) и новым модулем без теста в индексе.

    prod_paths — PROD_PATHS harness.conf стенда («app/» — код продукта, цена 1 + 3 = «обычная»);
    card_level — уровень wip-карточки в dev-map.yaml стенда (None — карты нет);
    hackathon_until — срок режима хакатона в harness.conf стенда (None — режим выключен).
    """
    yard = Path(tempfile.mkdtemp(prefix="proba-testy-vykl-"))
    yards.append(yard)
    repo = yard / "repo"
    (repo / "scripts").mkdir(parents=True)
    # hackathon.py и check-pipeline.py — судьи уровня хакатона: гейт вправе звать их.
    for name in ("pipeline.py", "hackathon.py", "check-pipeline.py"):
        shutil.copy(ROOT / "scripts" / name, repo / "scripts" / name)
    shutil.copy(GATE, repo / "scripts" / "check-tests-first.py")
    shutil.copytree(ROOT / "scripts" / "lib", repo / "scripts" / "lib",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (repo / "harness" / "config").mkdir(parents=True)
    if pult is not None:
        (repo / "harness" / "config" / "pipeline.yaml").write_text(pult, encoding="utf-8")
    if card_level is not None:
        (repo / "dev-map.yaml").write_text(DEV_MAP.format(level=card_level), encoding="utf-8")
    (yard / "log").mkdir()
    conf = f'MIGRATIONS_GLOB=""\nPROD_PATHS="{prod_paths}"\nGUARD_PATHS=""\n'
    if hackathon_until:
        conf += f'HACKATHON_UNTIL="{hackathon_until}"\n'
    (yard / "harness.conf").write_text(conf, encoding="utf-8")
    (yard / "install.conf").write_text(f'LOG_DIR="{yard / "log"}"\n', encoding="utf-8")
    git(repo, "init", "-q", "-b", "master")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "стенд")
    (repo / "app" / "lib").mkdir(parents=True)
    (repo / "app" / "lib" / "pump.py").write_text("RATE = 1\n", encoding="utf-8")
    git(repo, "add", "app/lib/pump.py")
    return yard, repo


def run_gate(levels, extra_env=None, **kwargs):
    """levels — словарь уровней (пульт с хвостом), строка (пульт как есть) или None (файла нет)."""
    pult = pult_text(levels) if isinstance(levels, dict) else levels
    yard, repo = stand(pult, **kwargs)
    env = {k: v for k, v in os.environ.items()
           if k not in ("HARNESS_PIPELINE_FORCE", "HARNESS_PIPELINE_WHY")}
    env.update(HARNESS_CONF=str(yard / "harness.conf"), HARNESS_INSTALL_CONF=str(yard / "install.conf"),
               LOG_DIR=str(yard / "log"), HARNESS_HOME=str(yard))
    env.update(extra_env or {})
    done = subprocess.run([sys.executable, str(repo / "scripts" / "check-tests-first.py")],
                          capture_output=True, text=True, env=env, timeout=120)
    return done.returncode, (done.stdout + done.stderr).strip()


WITH_TESTS = ["тесты", "спека"]
WITHOUT_TESTS = ["спека", "безопасность"]
ALL_LEVELS = ("мелкая", "хакатон", "обычная", "крупная")

CASES = [
    ("БОЛЬНОЙ СЛУЧАЙ: «тесты» выключены на всех уровнях → гейт не требует (0)",
     {level: WITHOUT_TESTS for level in ALL_LEVELS}, 0),
    ("«тесты» включены на всех уровнях → новый модуль без теста красный (1)",
     {level: WITH_TESTS for level in ALL_LEVELS}, 1),
    # Уровень коммита «мелкая»: судит ЕГО список, а не «есть ли тесты хоть где-то».
    ("БОЛЬНОЙ СЛУЧАЙ: выключены у «мелкая» (уровень коммита), включены у прочих → 0",
     {level: (WITHOUT_TESTS if level == "мелкая" else WITH_TESTS) for level in ALL_LEVELS}, 0),
    ("включены у «мелкая», выключены у прочих → 1",
     {level: (WITH_TESTS if level == "мелкая" else WITHOUT_TESTS) for level in ALL_LEVELS}, 1),
]

for name, levels, expected in CASES:
    code, output = run_gate(levels)
    check(name, code == expected, f"код {code}, ждали {expected} · {output[-240:]!r}")
    if expected == 1 and code == 1:
        check(f"{name}: отказ называет модуль", "app/lib/pump.py" in output, output[-240:])


# CR-2 (ревью кода 20.11): «пульт не прочитан» ≠ «шаг выключен». Нет pipeline.yaml,
# нет раздела «шаги», нет в нём уровня коммита — гейт судит, как до выключателя.
# Поломка, что оставит зелёным: «выключено» при пустом списке уровня (ловит пульт без
# уровня «мелкая»); умолчания pipeline.конфиг() с выдуманным разделом «шаги» без «тесты».
def judged_not_off(name, code, output, expected):
    check(name, code == expected and "app/lib/pump.py" in output and "выключен" not in output,
          f"код {code}, ждали {expected} · {output[-240:]!r}")


code, output = run_gate(None)
# Без пульта нет и красной строгости: гейт жёлтый (77) — но судит, а не молчит «выключен».
judged_not_off("CR-2: БОЛЬНОЙ СЛУЧАЙ: pipeline.yaml нет → гейт судит (77, модуль назван), не «выключен»",
               code, output, 77)
code, output = run_gate(TAIL)
judged_not_off("CR-2: БОЛЬНОЙ СЛУЧАЙ: в pipeline.yaml нет раздела «шаги» → 1, не «выключен»",
               code, output, 1)
code, output = run_gate({"обычная": WITHOUT_TESTS, "крупная": WITH_TESTS})
judged_not_off("CR-2: БОЛЬНОЙ СЛУЧАЙ: в «шаги» нет уровня коммита «мелкая» → 1, не «выключен»",
               code, output, 1)

# CR-3 (ревью кода 20.11): уровень коммита задачи-хакатона — «хакатон», как у run_checks
# в check-pipeline (hackathon.is_hackathon(wip-карточка индекса)), а не цена индекса.
# Режим включён сроком HACKATHON_UNTIL в harness.conf стенда (так судит hackathon.py).
# Поломка, что оставит зелёным: уровень по одной цене (ловят оба первых случая);
# «хакатон» при любой wip-карточке без суда её глубины (ловит третий).
UNTIL = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)).isoformat(
    timespec="seconds")
HACK = {"card_level": "хакатон", "hackathon_until": UNTIL}
OFF_AT_HACKATHON = {level: (WITHOUT_TESTS if level == "хакатон" else WITH_TESTS) for level in ALL_LEVELS}
ON_AT_HACKATHON = {level: (WITH_TESTS if level == "хакатон" else WITHOUT_TESTS) for level in ALL_LEVELS}
code, output = run_gate(OFF_AT_HACKATHON, **HACK)
check("CR-3: БОЛЬНОЙ СЛУЧАЙ: карточка-хакатон, у «хакатон» нет «тесты» (у «мелкая» есть) → 0",
      code == 0, f"код {code} · {output[-240:]!r}")
code, output = run_gate(ON_AT_HACKATHON, **HACK)
check("CR-3: БОЛЬНОЙ СЛУЧАЙ: карточка-хакатон, у «хакатон» есть «тесты» (у «мелкая» нет) → 1, модуль назван",
      code == 1 and "app/lib/pump.py" in output, f"код {code} · {output[-240:]!r}")
code, output = run_gate(OFF_AT_HACKATHON, card_level="мелкая", hackathon_until=UNTIL)
check("CR-3: та же правка, карточка НЕ хакатон (режим включён) → 1: судит «мелкая»",
      code == 1 and "app/lib/pump.py" in output, f"код {code} · {output[-240:]!r}")

# Уровень — по цене, а не всегда «мелкая» (мутация M2 ревью): новый файл в коде продукта —
# цена 1 + 3 = 4 (П1 3, П2 6), уровень «обычная». Поломка, что оставит зелёным: уровень
# вшит, а не посчитан ценой (тогда судился бы список «мелкая»).
PRODUCT = {"prod_paths": "app/"}
code, output = run_gate({level: (WITH_TESTS if level == "обычная" else WITHOUT_TESTS) for level in ALL_LEVELS},
                        **PRODUCT)
check("коммит уровня «обычная»: «тесты» только у «обычная» → 1", code == 1 and "app/lib/pump.py" in output,
      f"код {code} · {output[-240:]!r}")
code, output = run_gate({level: (WITHOUT_TESTS if level == "обычная" else WITH_TESTS) for level in ALL_LEVELS},
                        **PRODUCT)
check("коммит уровня «обычная»: «тесты» у всех, кроме «обычная» → 0", code == 0,
      f"код {code} · {output[-240:]!r}")

# SR-5 (ревью спеки 20.11): индекс не прочитан (признаки_коммита() → None) — «тесты» НЕ
# выключены: судит основной путь и отказывает «индекс не прочитан». Условие честное: GIT_DIR
# гейта указывает на несуществующий каталог, git diff --cached отвечает отказом. Пульт —
# «тесты» выключены на всех уровнях: ложное «не прочитал = выключен» дало бы 0.
# Поломка, что оставит зелёным: «не прочитан» → «выключен» (`return False` → `return True`
# в шаг_тесты_выключен) — ловит суд кода и слова «выключен».
code, output = run_gate({level: WITHOUT_TESTS for level in ALL_LEVELS},
                        extra_env={"GIT_DIR": str(Path(tempfile.gettempdir()) / "net-takogo-git-kataloga")})
check("SR-5: БОЛЬНОЙ СЛУЧАЙ: индекс не прочитан, «тесты» выключены везде → не «выключен», код 1",
      code == 1 and "выключен" not in output and "индекс не прочитан" in output,
      f"код {code} · {output[-240:]!r}")

for yard in yards:
    shutil.rmtree(yard, ignore_errors=True)
print(f"ТЕСТЫ-ВЫКЛ: путей {paths}, неудач {failures}")
sys.exit(1 if failures else 0)
