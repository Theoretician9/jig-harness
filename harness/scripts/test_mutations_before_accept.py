#!/usr/bin/env python3
"""Probes of the "mutations" step judged at acceptance, written BEFORE its code.

Task hr.mutations-before-accept (plan 2026-10-28, item 4a/4b). The shift used
to wait for mutations before `test` (a third of a big task's time). Now the
step is debt: the task goes to `test`, acceptance refuses `принято` while the
debt is open, and the night run of the task's mechanisms closes it (holes 0)
or raises a "weak probe" card (holes found).

That debt mode is a knob: it holds only while `шаги_приёмки` lists the step.
The live knob is empty (the step is judged before `test`, see
scripts/test_mutations_step.py), so the debt cases run on a stand knob and a
stand git with real `Dev-Map:` commits.

    python3 scripts/test_mutations_before_accept.py

Doors for mutation runs ([[probe-loads-the-live-mechanism]]):
    HARNESS_ACCEPTANCE       path of acceptance.py under test
    HARNESS_CHECK_PIPELINE   path of check-pipeline.py under test
Last line: `мутации до принятия: случаев N, падений F`.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

ACCEPTANCE = Path(os.environ.get("HARNESS_ACCEPTANCE") or HERE / "acceptance.py")
CHECK_PIPELINE = Path(os.environ.get("HARNESS_CHECK_PIPELINE") or HERE / "check-pipeline.py")
TASK = "hr.mut-probe"
MECHANISM = "scripts/m.py"

cases = Cases()
check = cases.check

# Подставной прибор мутаций: режим берётся из среды, как у боевого — из таблицы.
STUB_PROBE = '''#!/usr/bin/env python3
import os, sys
mode = os.environ["STUB_MUT_MODE"]
if mode == "none":
    print("mutation-probe: записей для механизма нет: " + sys.argv[-1])
    sys.exit(2)
holes = 0 if mode == "clean" else 2
print("2026-10-29 06:00:00  === мутационная проба: покраснели как надо 3, дыр %d, из кэша 0 ===" % holes)
sys.exit(holes)
'''

MAP = '''version: 1
lines:
  - id: harness
    layer: infra
    name: "Ядро"
    tasks:
      - id: hr.mut-probe
        status: test
        title: "Проба долга мутаций"
        summary: "x"
'''


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


STAND_PROBES = f"""пробы:
  scripts/test_m.py:
    механизм: {MECHANISM}
    доказательство: python3 scripts/test_m.py
    подмена: HARNESS_M
    мутация:
    - 's@a@b@'
"""


def git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "p", "GIT_AUTHOR_EMAIL": "p@s",
           "GIT_COMMITTER_NAME": "p", "GIT_COMMITTER_EMAIL": "p@s"}
    return subprocess.run(["git", "-C", str(root), *args], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(root: Path, task: str = TASK) -> str:
    """Коммит задачи в механизм с записями мутаций; отдаёт его sha."""
    path = root / MECHANISM
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("x = 1\n")
    git(root, "add", MECHANISM)
    git(root, "commit", "-q", "-m", f"правка\n\nDev-Map: {task}")
    return git(root, "rev-parse", "HEAD")


def stand(root: Path) -> str:
    """Свой git и своя таблица мутаций: механизм задачи судится по коммитам."""
    git(root, "init", "-q")
    config = root / "harness" / "config"
    config.mkdir(parents=True)
    (config / "probe-mutations.yaml").write_text(STAND_PROBES, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "стенд")
    return commit(root)


def write_log(root: Path, sha: str, body: str) -> Path:
    log = root / ".tasks" / TASK / "мутации.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"коммит {sha}\n--- {MECHANISM}\n{body}\n", encoding="utf-8")
    return log


def summary(holes: int) -> str:
    return f"=== мутационная проба: покраснели как надо 3, дыр {holes}, из кэша 0 ==="


class FakePrice:
    """Цена без git: признаки задают уровень прямо."""

    @staticmethod
    def цена(признаки, веса):
        return признаки["значение"]

    @staticmethod
    def уровень(значение, пороги):
        return "крупная" if значение >= 6 else "мелкая"


def stand_settings(deferred: list) -> dict:
    return {"шаги": {"крупная": ["спека", "мутации"]}, "шаги_приёмки": deferred,
            "веса": {}, "пороги": {}}


def deferred_by(cp, settings: dict):
    """Отложенные шаги карточки — по стендовому пульту, судьёй пайплайна."""
    return lambda задача, корень: cp.шаги_закрытия(FakePrice, settings, {"значение": 10},
                                                   задача)[2]


def probe_pipeline(cp) -> None:
    deferred = {"шаги_приёмки": ["мутации"]}
    check("шаги делятся: мутации — до принятия, остальное — до test",
          (["спека"], ["мутации"]),
          cp.до_test_и_до_принятия(deferred, ["спека", "мутации"]))
    check("без ключа пульта ничего не откладывается",
          (["спека", "мутации"], []),
          cp.до_test_и_до_принятия({}, ["спека", "мутации"]))
    live = cp._модуль_цены().конфиг()
    check("живой пульт: мутации — шаг до test (шаги_приёмки пуст)",
          ([], True),
          (list(live.get("шаги_приёмки") or []),
           all("мутации" in ((live.get("шаги") or {}).get(л) or [])
               for л in ("мелкая", "обычная", "крупная"))))
    card = {"id": TASK, "status": "test", "when": {"started": "2026-10-29"}}
    settings = stand_settings(["мутации"])
    check("переход в test не требует отложенных мутаций (состояние test)",
          ["спека"], cp.шаги_под_состояние(settings, card))
    check("закрытие крупной: до test без мутаций, мутации — долг приёмки",
          (10, ["спека"], ["мутации"]),
          cp.шаги_закрытия(FakePrice, settings, {"значение": 10}, card))
    check("пульт без шаги_приёмки: мутации судятся до test, долга приёмки нет",
          (10, ["спека", "мутации"], []),
          cp.шаги_закрытия(FakePrice, stand_settings([]), {"значение": 10}, card))


def probe_debt(acc, root: Path) -> None:
    sha = stand(root)
    debt = acc.долг_мутаций(TASK, root, ["мутации"])
    check("нет журнала — долг называет мутации.log", True, "мутации.log" in str(debt))
    check("шаг не отложен — долга нет", None, acc.долг_мутаций(TASK, root, []))
    write_log(root, sha, summary(2))
    check("журнал с дырами — долг называет дыры", True,
          "дыр 2" in str(acc.долг_мутаций(TASK, root, ["мутации"])))
    write_log(root, sha, summary(0))
    check("свежий журнал «дыр 0» — долга нет", None,
          acc.долг_мутаций(TASK, root, ["мутации"]))
    write_log(root, sha, "мутаций не прогнано")
    check("журнал без итога — долг", True,
          acc.долг_мутаций(TASK, root, ["мутации"]) is not None)
    write_log(root, sha, summary(0))
    commit(root)
    check("журнал «дыр 0» старше правки задачи — долг", True,
          "старше правки" in str(acc.долг_мутаций(TASK, root, ["мутации"])))


def probe_verdict(acc, cp, root: Path) -> None:
    sha = stand(root)
    acc.прогнать = lambda команда, корень, *a, **k: (0, "ПРОЙДЕН")
    card = {"id": TASK, "status": "test", "when": {"started": "2026-10-29"},
            "проверка": {"команда": "python3 scripts/x.py --selftest", "ждём": "ПРОЙДЕН"}}
    acc.шаги_приёмки_карточки = deferred_by(cp, stand_settings(["мутации"]))
    verdict = acc.судить(card, root)
    check("БОЛЬНОЙ: открытый долг мутаций не даёт принять", "ждёт-мутаций",
          verdict.get("исход"))
    acc.шаги_приёмки_карточки = deferred_by(cp, stand_settings([]))
    check("пульт без шаги_приёмки — приёмка мутаций не ждёт", "доказано",
          acc.судить(card, root).get("исход"))
    acc.шаги_приёмки_карточки = deferred_by(cp, stand_settings(["мутации"]))
    write_log(root, sha, summary(0))
    check("долг закрыт свежим «дыр 0» — принято", "доказано",
          acc.судить(card, root).get("исход"))
    acc.шаги_приёмки_карточки = lambda задача, корень: None
    check("история не прочитана — не принимаем", "ждёт-мутаций",
          acc.судить(card, root).get("исход"))
    (root / ".tasks" / TASK / "мутации.log").unlink()

    path = root / "dev-map.yaml"
    path.write_text(MAP, encoding="utf-8")
    acc.применить([{"id": TASK, "исход": "ждёт-мутаций",
                    "почему": "мутации: нет .tasks/hr.mut-probe/мутации.log"}], path)
    text = path.read_text(encoding="utf-8")
    check("пометка долга в карточке", True, "мутации_ждут:" in text)
    check("статус остаётся test", True, "status: test" in text)
    acc.применить([{"id": TASK, "исход": "доказано"}], path)
    text = path.read_text(encoding="utf-8")
    check("принятая задача теряет пометку долга", False, "мутации_ждут:" in text)


def probe_night(acc, root: Path) -> None:
    stand(root)
    stub = root / "stub-probe.py"
    stub.write_text(STUB_PROBE, encoding="utf-8")
    acc.шаги_приёмки_карточки = lambda задача, корень: ["мутации"]
    card_map = {"lines": [{"id": "harness", "tasks": [{"id": TASK, "status": "test"}]}]}
    probe = [sys.executable, str(stub)]

    def run(mode: str) -> list:
        # Каждая ночь — после нового коммита: прежний журнал долга не закрывает.
        commit(root)
        os.environ["STUB_MUT_MODE"] = mode
        calls: list = []
        acc.ночные_мутации(card_map, root, probe, calls.append)
        return calls

    calls = run("clean")
    check("БОЛЬНОЙ: ночь «дыр 0» закрывает долг", None,
          acc.долг_мутаций(TASK, root, ["мутации"]))
    check("ночь «дыр 0» гасит карточку слабой пробы", True,
          any("--resolve" in c for c in calls))
    calls = run("holes")
    check("БОЛЬНОЙ: дыра заводит карточку слабой пробы", True,
          any("--key" in c for c in calls))
    check("с дырой долг остаётся", True,
          "дыр 2" in str(acc.долг_мутаций(TASK, root, ["мутации"])))
    calls = run("none")
    check("у механизмов задачи нет мутаций — карточка и долг остаются", (True, True),
          (any("--key" in c for c in calls),
           acc.долг_мутаций(TASK, root, ["мутации"]) is not None))
    acc.шаги_приёмки_карточки = lambda задача, корень: []
    os.environ["STUB_MUT_MODE"] = "clean"
    calls = []
    check("задача без долга ночью не гоняется", [],
          acc.ночные_мутации(card_map, root, probe, calls.append))


def probe_hackathon(acc, root: Path) -> None:
    """Задача хакатона: мутации — долг карточки <id>-prod-debt, не её приёмки."""
    stand(root)
    acc.прогнать = lambda команда, корень, *a, **k: (0, "ПРОЙДЕН")
    acc.шаги_приёмки_карточки = lambda задача, корень: ["мутации"]
    proof = {"команда": "python3 scripts/x.py --selftest", "ждём": "ПРОЙДЕН"}
    hackathon = {"id": TASK, "status": "test", "проверка": proof,
                 "глубина": {"уровень": "хакатон", "характер": "разработка"}}
    ordinary = {**hackathon, "глубина": {"уровень": "крупная", "характер": "разработка"}}
    check("БОЛЬНОЙ: хакатон без мутации.log — долга мутаций нет (он у -prod-debt)", None,
          acc.долг_мутаций_задачи(hackathon, root))
    check("БОЛЬНОЙ: хакатон без мутации.log — принято", "доказано",
          acc.судить(hackathon, root).get("исход"))
    check("не хакатон без мутации.log — долг называет мутации.log", True,
          "мутации.log" in str(acc.долг_мутаций_задачи(ordinary, root)))
    check("не хакатон без мутации.log — не принято", "ждёт-мутаций",
          acc.судить(ordinary, root).get("исход"))


def main() -> int:
    cp = load(CHECK_PIPELINE, "check_pipeline_under_test")
    for name, probe in (("пайплайн", lambda: probe_pipeline(cp)),):
        try:
            probe()
        except Exception as error:  # проба без функции — красный путь, а не трассировка
            check(f"{name}: судья отвечает", "ответ", f"{type(error).__name__}: {error}")
    acc = load(ACCEPTANCE, "acceptance_under_test")
    # Судья пайплайна берёт acceptance.py под пробой, а не своего соседа.
    cp._SIBLINGS["acceptance"] = acc
    for name, probe in (("долг", probe_debt), ("вердикт", lambda a, r: probe_verdict(a, cp, r)),
                        ("ночь", probe_night), ("хакатон", probe_hackathon)):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                probe(acc, Path(tmp))
            except Exception as error:
                check(f"{name}: судья отвечает", "ответ", f"{type(error).__name__}: {error}")
    print(f"мутации до принятия: случаев {cases.paths}, падений {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
