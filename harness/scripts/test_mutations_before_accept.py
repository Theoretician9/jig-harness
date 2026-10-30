#!/usr/bin/env python3
"""Probes of the "mutations" step judged at acceptance, written BEFORE its code.

Task hr.mutations-before-accept (plan 2026-10-28, item 4a/4b). The shift used
to wait for mutations before `test` (a third of a big task's time). Now the
step is debt: the task goes to `test`, acceptance refuses `принято` while the
debt is open, and the night run of the task's mechanisms closes it (holes 0)
or raises a "weak probe" card (holes found).

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
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

ACCEPTANCE = Path(os.environ.get("HARNESS_ACCEPTANCE") or HERE / "acceptance.py")
CHECK_PIPELINE = Path(os.environ.get("HARNESS_CHECK_PIPELINE") or HERE / "check-pipeline.py")
TASK = "hr.mut-probe"

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


def write_log(root: Path, text: str, age_sec: int = 0) -> Path:
    log = root / ".tasks" / TASK / "мутации.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text, encoding="utf-8")
    stamp = time.time() - age_sec
    os.utime(log, (stamp, stamp))
    return log


class FakePrice:
    """Цена без git: признаки задают уровень прямо."""

    @staticmethod
    def цена(признаки, веса):
        return признаки["значение"]

    @staticmethod
    def уровень(значение, пороги):
        return "крупная" if значение >= 6 else "мелкая"


def probe_pipeline(cp) -> None:
    deferred = {"шаги_приёмки": ["мутации"]}
    check("шаги делятся: мутации — до принятия, остальное — до test",
          (["спека"], ["мутации"]),
          cp.до_test_и_до_принятия(deferred, ["спека", "мутации"]))
    check("без ключа пульта ничего не откладывается",
          (["спека", "мутации"], []),
          cp.до_test_и_до_принятия({}, ["спека", "мутации"]))
    live = cp._модуль_цены().конфиг()
    check("пульт владельца откладывает мутации до принятия",
          ["мутации"], list(live.get("шаги_приёмки") or []))
    card = {"id": TASK, "status": "test", "when": {"started": "2026-10-29"}}
    settings = {"шаги": {"крупная": ["спека", "мутации"]}, "шаги_приёмки": ["мутации"],
                "веса": {}, "пороги": {}}
    check("переход в test не требует мутаций (состояние test)",
          ["спека"], cp.шаги_под_состояние(settings, card))
    check("закрытие крупной: до test без мутаций, мутации — долг приёмки",
          (10, ["спека"], ["мутации"]),
          cp.шаги_закрытия(FakePrice, settings, {"значение": 10}, card))


def probe_debt(acc, root: Path) -> None:
    log = root / ".tasks" / TASK / "мутации.log"
    commit = time.time() - 3600
    debt = acc.долг_мутаций(TASK, root, ["мутации"], commit)
    check("нет журнала — долг «ждут ночи»", True, "ждут ночи" in str(debt))
    check("шаг не отложен — долга нет", None, acc.долг_мутаций(TASK, root, [], commit))
    write_log(root, "=== мутации задачи: покраснели 3, дыр 2 ===\n")
    check("журнал с дырами — долг называет дыры", True,
          "дыр 2" in str(acc.долг_мутаций(TASK, root, ["мутации"], commit)))
    write_log(root, "=== мутации задачи: покраснели 3, дыр 0 ===\n", age_sec=7200)
    check("журнал «дыр 0» старше последнего коммита — долг", True,
          acc.долг_мутаций(TASK, root, ["мутации"], commit) is not None)
    write_log(root, "=== мутации задачи: покраснели 3, дыр 0 ===\n")
    check("свежий журнал «дыр 0» — долга нет", None,
          acc.долг_мутаций(TASK, root, ["мутации"], commit))
    write_log(root, "мутаций не прогнано\n")
    check("журнал без итога — долг", True,
          acc.долг_мутаций(TASK, root, ["мутации"], commit) is not None)
    log.unlink()


def probe_verdict(acc, root: Path) -> None:
    acc.прогнать = lambda команда, корень, *a, **k: (0, "ПРОЙДЕН")
    acc.последний_коммит = lambda ид, корень: time.time() - 3600
    acc.шаги_приёмки_карточки = lambda задача, корень: ["мутации"]
    card = {"id": TASK, "status": "test",
            "проверка": {"команда": "python3 scripts/x.py --selftest", "ждём": "ПРОЙДЕН"}}
    verdict = acc.судить(card, root)
    check("БОЛЬНОЙ: открытый долг мутаций не даёт принять", "ждёт-мутаций",
          verdict.get("исход"))
    write_log(root, "=== мутации задачи: покраснели 3, дыр 0 ===\n")
    check("долг закрыт свежим «дыр 0» — принято", "доказано",
          acc.судить(card, root).get("исход"))
    acc.шаги_приёмки_карточки = lambda задача, корень: None
    check("история не прочитана — не принимаем", "ждёт-мутаций",
          acc.судить(card, root).get("исход"))
    (root / ".tasks" / TASK / "мутации.log").unlink()

    path = root / "dev-map.yaml"
    path.write_text(MAP, encoding="utf-8")
    acc.применить([{"id": TASK, "исход": "ждёт-мутаций",
                    "почему": "мутации: ждут ночи"}], path)
    text = path.read_text(encoding="utf-8")
    check("пометка долга в карточке", True, "мутации_ждут:" in text)
    check("статус остаётся test", True, "status: test" in text)
    acc.применить([{"id": TASK, "исход": "доказано"}], path)
    text = path.read_text(encoding="utf-8")
    check("принятая задача теряет пометку долга", False, "мутации_ждут:" in text)


def probe_night(acc, root: Path) -> None:
    stub = root / "stub-probe.py"
    stub.write_text(STUB_PROBE, encoding="utf-8")
    acc.шаги_приёмки_карточки = lambda задача, корень: ["мутации"]
    acc.последний_коммит = lambda ид, корень: time.time() - 3600
    acc.файлы_задачи = lambda ид, корень: ["scripts/m.py"]
    card_map = {"lines": [{"id": "harness", "tasks": [{"id": TASK, "status": "test"}]}]}
    probe = [sys.executable, str(stub)]

    def run(mode: str) -> list:
        # Каждая ночь — после нового коммита: прежний журнал долга не закрывает.
        (root / ".tasks" / TASK / "мутации.log").unlink(missing_ok=True)
        os.environ["STUB_MUT_MODE"] = mode
        calls: list = []
        acc.ночные_мутации(card_map, root, probe, calls.append)
        return calls

    calls = run("clean")
    check("БОЛЬНОЙ: ночь «дыр 0» закрывает долг", None,
          acc.долг_мутаций(TASK, root, ["мутации"], time.time() - 3600))
    check("ночь «дыр 0» гасит карточку слабой пробы", True,
          any("--resolve" in c for c in calls))
    calls = run("holes")
    check("БОЛЬНОЙ: дыра заводит карточку слабой пробы", True,
          any("--key" in c for c in calls))
    check("с дырой долг остаётся", True,
          "дыр 2" in str(acc.долг_мутаций(TASK, root, ["мутации"], time.time() - 3600)))
    calls = run("none")
    check("у механизмов задачи нет мутаций — карточка и долг остаются", (True, True),
          (any("--key" in c for c in calls),
           acc.долг_мутаций(TASK, root, ["мутации"], time.time() - 3600) is not None))
    acc.шаги_приёмки_карточки = lambda задача, корень: []
    os.environ["STUB_MUT_MODE"] = "clean"
    calls = []
    check("задача без долга ночью не гоняется", [],
          acc.ночные_мутации(card_map, root, probe, calls.append))


def main() -> int:
    cp = load(CHECK_PIPELINE, "check_pipeline_under_test")
    for name, probe in (("пайплайн", lambda: probe_pipeline(cp)),):
        try:
            probe()
        except Exception as error:  # проба без функции — красный путь, а не трассировка
            check(f"{name}: судья отвечает", "ответ", f"{type(error).__name__}: {error}")
    acc = load(ACCEPTANCE, "acceptance_under_test")
    for name, probe in (("долг", probe_debt), ("вердикт", probe_verdict), ("ночь", probe_night)):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                probe(acc, Path(tmp))
            except Exception as error:
                check(f"{name}: судья отвечает", "ответ", f"{type(error).__name__}: {error}")
    print(f"мутации до принятия: случаев {cases.paths}, падений {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
