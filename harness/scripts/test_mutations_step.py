#!/usr/bin/env python3
"""Пробы шага «мутации» до test: бумага задачи по каждому механизму, дыр 0,
не старше правки (спека .tasks/hr.split-checks-and-dev-streams/мутации-шагом-задачи.md §6).

    python3 scripts/test_mutations_step.py

Стенд — временный каталог со своим git (коммиты с трейлером `Dev-Map: <id>`),
своими invariants.yaml и probe-mutations.yaml, подставным прибором мутаций и
подставным писателем тревог с журналом.

Двери для мутационного прогона ([[probe-loads-the-live-mechanism]]):
    HARNESS_ACCEPTANCE       путь acceptance.py под пробой
    HARNESS_CHECK_PIPELINE   путь check-pipeline.py под пробой
Последняя строка: `шаг мутаций: N путей, F ПЛОХО`.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

ACCEPTANCE = Path(os.environ.get("HARNESS_ACCEPTANCE") or HERE / "acceptance.py")
CHECK_PIPELINE = Path(os.environ.get("HARNESS_CHECK_PIPELINE") or HERE / "check-pipeline.py")

cases = Cases()
check = cases.check

MECHANISM = "scripts/m.py"
SECOND = "scripts/inv.py"
PROOF = "scripts/test_m.py"
PLAIN = "scripts/plain.py"

INVARIANTS = f"""инварианты:
  - id: И-стенд
    файл: {SECOND}
    мутация: 's@a@b@'
"""
PROBES = f"""пробы:
  {PROOF}:
    механизм: {MECHANISM}
    доказательство: python3 {PROOF}
    подмена: HARNESS_M
    мутация:
    - 's@a@b@'
"""

# Подставной прибор: дыры механизма берутся из среды STUB_HOLES («файл=N,…»).
STUB_PROBE = '''#!/usr/bin/env python3
import os, sys
holes = dict(p.split("=") for p in os.environ.get("STUB_HOLES", "").split(",") if p)
name = sys.argv[-1]
n = int(holes.get(name, "0"))
print("=== мутационная проба: покраснели как надо 3, дыр %d, из кэша 0 ===" % n)
sys.exit(1 if n else 0)
'''

# Подставной писатель тревог: каждый вызов — строка журнала.
STUB_ALERT = '''#!/usr/bin/env python3
import os, sys
with open(os.environ["STUB_ALERT_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
'''


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Stand:
    """Корень со своим git: коммиты задач — настоящие, с трейлером."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "p", "GIT_AUTHOR_EMAIL": "p@s",
                    "GIT_COMMITTER_NAME": "p", "GIT_COMMITTER_EMAIL": "p@s"}
        root.mkdir(parents=True)
        self.git("init", "-q")
        config = root / "harness" / "config"
        config.mkdir(parents=True)
        (config / "invariants.yaml").write_text(INVARIANTS, encoding="utf-8")
        (config / "probe-mutations.yaml").write_text(PROBES, encoding="utf-8")
        for name in (MECHANISM, SECOND, PROOF, PLAIN):
            self.write(name)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "стенд")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, name: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("x = 1\n")

    def commit(self, task: str, *names: str) -> str:
        for name in names:
            self.write(name)
        self.git("add", *names)
        self.git("commit", "-q", "-m", f"правка\n\nDev-Map: {task}")
        return self.git("rev-parse", "HEAD")

    def paper(self, task: str, sha: str, sections: dict) -> None:
        lines = [f"коммит {sha}"]
        for mechanism, body in sections.items():
            lines += [f"--- {mechanism}", body]
        lines.append(f"=== мутации задачи {task}: покраснели 3, дыр 0 ===")
        log = self.root / ".tasks" / task / "мутации.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")


GREEN = "=== мутационная проба: покраснели как надо 3, дыр 0, из кэша 0 ==="


class FakePrice:
    """Цена без git: признаки задают уровень прямо."""

    @staticmethod
    def цена(признаки, веса):
        return признаки["значение"]

    @staticmethod
    def уровень(значение, пороги):
        return "крупная" if значение >= 6 else "обычная" if значение >= 3 else "мелкая"


def debts(cp, root: Path, task: str) -> list:
    return cp.проверить({"id": task}, root, ["мутации"])


def probe_paper(acc, cp, stand: Stand) -> None:
    root = stand.root
    stand.commit("hr.none", PLAIN)
    check("1 задача без механизмов с записями — шаг пуст, бумага не нужна",
          ([], [], None),
          (acc.механизмы_задачи("hr.none", root), debts(cp, root, "hr.none"),
           acc.мутации_задачи("hr.none", root, ["false"])))

    stand.commit("hr.nopaper", MECHANISM)
    got = debts(cp, root, "hr.nopaper")
    check("2 БОЛЬНОЙ: механизм правлен, бумаги нет — «нет мутации.log»", True,
          any("нет .tasks/hr.nopaper/мутации.log" in б for б in got))

    sha = stand.commit("hr.holes", MECHANISM)
    stand.paper("hr.holes", sha, {MECHANISM: GREEN.replace("дыр 0", "дыр 1")})
    got = debts(cp, root, "hr.holes")
    check("3 БОЛЬНОЙ: дыр 1 в разделе", True,
          any(MECHANISM in б and "дыр 1" in б for б in got))

    sha = stand.commit("hr.two", MECHANISM, SECOND)
    stand.paper("hr.two", sha, {MECHANISM: GREEN})
    got = debts(cp, root, "hr.two")
    check("4 БОЛЬНОЙ: два механизма, раздел один", True,
          any(SECOND in б and "нет раздела" in б for б in got))

    sha = stand.commit("hr.stale", MECHANISM)
    stand.paper("hr.stale", sha, {MECHANISM: GREEN})
    fresh = debts(cp, root, "hr.stale")
    stand.commit("hr.stale", MECHANISM)
    got = debts(cp, root, "hr.stale")
    check("5 БОЛЬНОЙ: бумага старше правки", ([], True),
          (fresh, any("бумага старше правки" in б and MECHANISM in б for б in got)))

    sha = stand.commit("hr.foreign", MECHANISM)
    stand.paper("hr.foreign", sha, {MECHANISM: GREEN})
    stand.commit("hr.someone-else", MECHANISM)
    check("6 чужой коммит в тот же механизм после sha — бумага не устаревает", [],
          debts(cp, root, "hr.foreign"))

    stand.commit("hr.proof", PROOF)
    got = debts(cp, root, "hr.proof")
    check("7 БОЛЬНОЙ: правлено только доказательство", ([MECHANISM], True),
          (acc.механизмы_задачи("hr.proof", root), any(MECHANISM in б for б in got)))

    sha = stand.commit("hr.fell", MECHANISM)
    stand.paper("hr.fell", sha, {MECHANISM: "Traceback: прибор упал"})
    got = debts(cp, root, "hr.fell")
    check("8 БОЛЬНОЙ: раздел без итога", True,
          any(MECHANISM in б and "нет итога" in б for б in got))

    sha = stand.commit("hr.short", MECHANISM)
    stand.paper("hr.short", sha, {MECHANISM: GREEN + "\nмутация НЕДОГНАНА: срок"})
    got = debts(cp, root, "hr.short")
    check("9 БОЛЬНОЙ: «НЕДОГНАНА» в разделе", True,
          any(MECHANISM in б and "НЕДОГНАН" in б for б in got))

    stand.commit("hr.orphan", MECHANISM)
    orphan = stand.git("commit-tree", "HEAD^{tree}", "-m", "вне истории")
    stand.paper("hr.orphan", orphan, {MECHANISM: GREEN})
    got = debts(cp, root, "hr.orphan")
    check("10 БОЛЬНОЙ: sha бумаги не предок HEAD", True,
          any("не предок HEAD" in б for б in got))


def probe_levels(cp) -> None:
    settings = {"шаги": {"мелкая": ["тесты", "мутации"], "обычная": ["тесты", "спека", "мутации"],
                         "крупная": ["спека", "мутации"], "хакатон": ["спека", "тесты"]},
                "шаги_включены": {"обычная": {"мутации": "2026-11-05"}},
                "характеры": {"починка": {"шаги": ["тесты", "ревью_кода"]}},
                "шаги_приёмки": [], "веса": {}, "пороги": {}}
    fix = {"id": "hr.fix", "when": {"started": "2026-11-06"},
           "глубина": {"уровень": "обычная", "характер": "починка"}}
    check("11 характер «починка», уровень «обычная» по цене — шаг остаётся", True,
          "мутации" in cp.шаги_закрытия(FakePrice, settings, {"значение": 4}, fix)[1])
    removed = {**settings, "шаги": {**settings["шаги"], "обычная": ["тесты", "спека"]}}
    early = {"id": "hr.early", "when": {"started": "2026-11-04"}}
    check("12 шаг вынут из шаги.<уровень> или задача начата до включения — не требуется",
          (False, False),
          ("мутации" in cp.шаги_закрытия(FakePrice, removed, {"значение": 4}, fix)[1],
           "мутации" in cp.шаги_закрытия(FakePrice, settings, {"значение": 4}, early)[1]))
    hack = {"id": "hr.hack", "when": {"started": "2026-11-06"},
            "глубина": {"уровень": "хакатон", "характер": "разработка"}}
    check("13 хакатон — не требуется", False,
          "мутации" in cp.шаги_закрытия(FakePrice, settings, {"значение": 10}, hack)[1])


def probe_no_git(acc, cp, root: Path) -> None:
    config = root / "harness" / "config"
    config.mkdir(parents=True)
    (config / "probe-mutations.yaml").write_text(PROBES, encoding="utf-8")
    got = debts(cp, root, "hr.nogit")
    check("14 БОЛЬНОЙ: git молчит — «история не прочитана», не «пусто»", (None, True),
          (acc.mutation_paper_debts("hr.nogit", root),
           any("не прочитана" in б for б in got)))


def probe_command(cp, stand: Stand, tmp: Path) -> None:
    """Режим --мутации-задачи из копии acceptance.py в корне стенда."""
    root = stand.root
    scripts = root / "scripts"
    shutil.copy(ACCEPTANCE, scripts / "acceptance.py")
    (scripts / "lib").symlink_to(ACCEPTANCE.resolve().parent / "lib")
    (scripts / "alert-task.py").write_text(STUB_ALERT, encoding="utf-8")
    stub = tmp / "stub-probe.py"
    stub.write_text(STUB_PROBE, encoding="utf-8")
    alerts = tmp / "alerts.log"
    stand.commit("hr.cmd", MECHANISM, SECOND)

    def run(holes: str) -> int:
        # Метка среза: на стенде нет двери run-in-checks-slice.sh, судим бумагу, а не дверь.
        env = {**os.environ, "HARNESS_MUTATION_PROBE": f"{sys.executable} {stub}",
               "HARNESS_IN_CHECKS_SLICE": "1",
               "STUB_HOLES": holes, "STUB_ALERT_LOG": str(alerts), "LOG_DIR": str(tmp / "cmd-logs")}
        return subprocess.run([sys.executable, str(scripts / "acceptance.py"),
                               "--мутации-задачи", "hr.cmd"], cwd=str(root), env=env,
                              capture_output=True, text=True, timeout=120).returncode

    red = run(f"{SECOND}=1")
    red_debts = debts(cp, root, "hr.cmd")
    green = run("")
    paper = (root / ".tasks" / "hr.cmd" / "мутации.log").read_text(encoding="utf-8")
    check("15 --мутации-задачи: код 1 при дыре, 0 при дыр 0; тревог нет; бумага судится",
          (1, True, 0, True, [], False),
          (red, any(SECOND in б and "дыр 1" in б for б in red_debts), green,
           paper.startswith("коммит " + stand.git("rev-parse", "HEAD")),
           debts(cp, root, "hr.cmd"), alerts.exists()))


# Подставной прибор пачки: каждый вызов — строка «механизм|MUT_ONLY_LINES» в журнал.
BATCH_PROBE = '''#!/usr/bin/env python3
import os, sys
with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as fh:
    fh.write(sys.argv[-1] + "|" + os.environ.get("MUT_ONLY_LINES", "") + "\\n")
print("=== мутационная проба: покраснели как надо 2, дыр 0, из кэша 0 ===")
'''

# Подставная дверь среза: каждый заход — строка в журнал, затем сама команда.
BATCH_DOOR = '''#!/usr/bin/env bash
echo "$*" >> "$STUB_DOOR_LOG"
export HARNESS_IN_CHECKS_SLICE=1
exec "$@"
'''


def probe_batch(cp, stand: Stand, tmp: Path) -> None:
    """--мутации-задачи a,b,c: один заход в срез, общий механизм — один прогон,
    бумага каждой задачи — только её механизмы."""
    root = stand.root
    scripts = root / "scripts"
    shutil.copy(ACCEPTANCE, scripts / "acceptance.py")
    (scripts / "lib").symlink_to(ACCEPTANCE.resolve().parent / "lib")
    (scripts / "run-in-checks-slice.sh").write_text(BATCH_DOOR, encoding="utf-8")
    stub = tmp / "batch-probe.py"
    stub.write_text(BATCH_PROBE, encoding="utf-8")
    calls, door = tmp / "batch-calls.log", tmp / "batch-door.log"
    stand.commit("hr.a", MECHANISM)
    stand.commit("hr.b", MECHANISM, SECOND)
    stand.commit("hr.c", PLAIN)
    acc = load(scripts / "acceptance.py", "acceptance_batch_stand")
    lines = {i: set((acc.task_changed_lines(i, root, MECHANISM) or "").split(","))
             for i in ("hr.a", "hr.b")}
    env = {**os.environ, "HARNESS_MUTATION_PROBE": f"{sys.executable} {stub}",
           "STUB_CALLS": str(calls), "STUB_DOOR_LOG": str(door), "LOG_DIR": str(tmp / "batch-logs")}
    env.pop("HARNESS_IN_CHECKS_SLICE", None)
    rc = subprocess.run([sys.executable, str(scripts / "acceptance.py"), "--мутации-задачи",
                         "hr.a,hr.b,hr.c"], cwd=str(root), env=env, capture_output=True,
                        text=True, timeout=120).returncode
    runs = [l.split("|") for l in calls.read_text(encoding="utf-8").splitlines()] \
        if calls.is_file() else []

    def sections(task: str):
        log = root / ".tasks" / task / "мутации.log"
        if not log.is_file():
            return None
        return sorted(acc.paper_sections(log.read_text(encoding="utf-8"), [MECHANISM, SECOND]))

    check("17 пачка a,b,c: один заход в срез, код 0", (1, 0),
          (len(door.read_text(encoding="utf-8").splitlines()) if door.is_file() else 0, rc))
    check("18 пачка: общий механизм прогнан один раз, всего прогонов 2",
          sorted([MECHANISM, SECOND]), sorted(r[0] for r in runs))
    mech_lines = next((set(r[1].split(",")) for r in runs if r[0] == MECHANISM), set())
    check("19 пачка: прогон общего механизма покрывает строки обеих задач", True,
          bool(lines["hr.a"] - {""}) and lines["hr.a"] | lines["hr.b"] <= mech_lines)
    check("20 пачка: бумага у каждой своя — a только механизм, b оба, c без бумаги",
          ([MECHANISM], sorted([MECHANISM, SECOND]), None),
          (sections("hr.a"), sections("hr.b"), sections("hr.c")))
    check("21 пачка: бумаги a и b судятся зелёными", ([], []),
          (debts(cp, root, "hr.a"), debts(cp, root, "hr.b")))


def probe_foreign_lines(acc, stand: Stand) -> None:
    """Строки задачи — только её: правка другой задачи ПОСЛЕ первого коммита
    задачи в тот же файл в её набор не входит. Иначе шаг судил задаче чужие
    строки (журнал 16.11: задача — 35 из 41 строки реестра журналов
    чужие)."""
    stand.commit("hr.own", MECHANISM)          # строка 2: задача
    stand.commit("hr.other", MECHANISM)        # строка 3: другая задача
    stand.commit("hr.own", MECHANISM)          # строка 4: снова задача
    stand.write(MECHANISM)                     # строка 5: правка в работе, ещё без коммита
    got = acc.task_changed_lines("hr.own", stand.root, MECHANISM)
    check("34 строки задачи без строки другой задачи между её коммитами", "2,4,5", got)


def probe_deleted_lines(acc, stand: Stand) -> None:
    """Чистое удаление строк не оставляет за задачей строки по blame: стык
    удаления — её, хотя соседние строки старше задачи."""
    stand.commit("hr.base", MECHANISM)         # строка 2
    path = stand.root / MECHANISM
    path.write_text("x = 1\n", encoding="utf-8")
    stand.git("commit", "-q", "-am", "удаление\n\nDev-Map: hr.gone")
    check("35 чистое удаление задачей — стык строк 1,2 остаётся её", "1,2",
          acc.task_changed_lines("hr.gone", stand.root, MECHANISM))


def set_lines(stand: Stand, task: str, *lines: str) -> None:
    """Файл механизма — ровно эти строки; task пуст — правка остаётся в работе."""
    (stand.root / MECHANISM).write_text("".join(f"{x}\n" for x in lines), encoding="utf-8")
    if task:
        stand.git("commit", "-q", "-am", f"правка\n\nDev-Map: {task}")


def probe_foreign_deletion(acc, stand: Stand) -> None:
    """Стык удаления, сделанного ДРУГОЙ задачей после первого коммита задачи,
    — не её: иначе шаг требовал мутаций там, где задача ничего не меняла
    (16.11: ask-consent.py 293–294, owner_word.py — удалял 6bc44771)."""
    set_lines(stand, "hr.base", "a = 1", "b = 1", "c = 1", "d = 1")
    set_lines(stand, "hr.own", "a = 1", "b = 1", "c = 1", "d = 1", "e = 1")
    set_lines(stand, "hr.other", "a = 1", "c = 1", "d = 1", "e = 1")
    check("36 стык удаления другой задачей — не задаче", "4",
          acc.task_changed_lines("hr.own", stand.root, MECHANISM))


def probe_own_deletion_shifted(acc, stand: Stand) -> None:
    """Стык удаления задачей остаётся её и после того, как чужая вставка выше
    сдвинула его; удаление в работе — тоже её."""
    set_lines(stand, "hr.base", "a = 1", "b = 1", "c = 1", "d = 1", "e = 1")
    set_lines(stand, "hr.own", "a = 1", "b = 1", "d = 1", "e = 1")            # стык 2,3
    set_lines(stand, "hr.other", "z = 1", "a = 1", "b = 1", "d = 1", "e = 1")  # стык 3,4
    set_lines(stand, "hr.own", "z = 1", "a = 1", "b = 1", "d = 1", "n = 1", "e = 1")
    set_lines(stand, "", "z = 1", "a = 1", "b = 1", "d = 1", "n = 1")          # в работе: стык 5,6
    check("37 удаление задачей (сдвинутое чужой вставкой и в работе) — задаче", "3,4,5,6",
          acc.task_changed_lines("hr.own", stand.root, MECHANISM))


def probe_comment_lines(acc, stand: Stand) -> None:
    """Комментарий и пустая строка поведения не несут — мутировать нечего
    (16.11: pre-commit-gate.sh 748 — комментарий, 753 — пустая)."""
    set_lines(stand, "hr.own", "x = 1", "# почему", "", "    # отступ", "y = 2")
    check("38 комментарии и пустые строки — не в наборе", "5",
          acc.task_changed_lines("hr.own", stand.root, MECHANISM))


def probe_batch_law(stand: Stand, tmp: Path) -> None:
    """Закон пачек (слово владельца 14.11): два вызова ПОДРЯД по одному id в окне
    сбора — один заход в срез, бумага каждой задаче; очередь — в своём LOG_DIR."""
    root = stand.root
    scripts = root / "scripts"
    shutil.copy(ACCEPTANCE, scripts / "acceptance.py")
    (scripts / "lib").symlink_to(ACCEPTANCE.resolve().parent / "lib")
    (scripts / "run-in-checks-slice.sh").write_text(BATCH_DOOR, encoding="utf-8")
    (root / "harness" / "config" / "pipeline.yaml").write_text(
        "пачка: {задач: 6, помощников: 3, окно_сбора_сек: 3, ждать_итога_мин: 2}\n", encoding="utf-8")
    stub = tmp / "law-probe.py"
    stub.write_text(BATCH_PROBE, encoding="utf-8")
    calls, door = tmp / "law-calls.log", tmp / "law-door.log"
    stand.commit("hr.a", MECHANISM)
    stand.commit("hr.b", SECOND)
    env = {**os.environ, "HARNESS_MUTATION_PROBE": f"{sys.executable} {stub}",
           "STUB_CALLS": str(calls), "STUB_DOOR_LOG": str(door), "LOG_DIR": str(tmp / "law-logs")}
    env.pop("HARNESS_IN_CHECKS_SLICE", None)

    def call(task: str) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, str(scripts / "acceptance.py"), "--мутации-задачи", task],
                                cwd=str(root), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)

    first = call("hr.a")
    time.sleep(0.5)
    second = call("hr.b")
    outs = [p.communicate(timeout=120)[0] for p in (first, second)]
    entries = door.read_text(encoding="utf-8").splitlines() if door.is_file() else []
    check("22 закон пачек: два вызова подряд в окне — один заход в срез с обоими id",
          (1, True), (len(entries), bool(entries) and entries[0].endswith("hr.a,hr.b")))
    check("23 закон пачек: бумага каждой задаче, код 0 обоим",
          (True, True, 0, 0),
          ((root / ".tasks" / "hr.a" / "мутации.log").is_file(),
           (root / ".tasks" / "hr.b" / "мутации.log").is_file(), first.returncode, second.returncode))
    check("24 закон пачек: каждый видит итог своей задачи, а не чужой", (True, False, True, False),
          ("мутации задачи hr.a: дыр 0" in outs[0], "мутации задачи hr.b" in outs[0],
           "мутации задачи hr.b: дыр 0" in outs[1], "мутации задачи hr.a" in outs[1]))
    acc = load(scripts / "acceptance.py", "acceptance_law_stand")
    ran = []
    acc.механизмы_задачи = lambda ид, корень: None if ид == "hr.unread" else [MECHANISM]
    acc.мутации_пачки = lambda ids, *rest, **kw: ran.append(ids) or dict.fromkeys(ids, 0)
    # Не hr.a: её свежая зелёная бумага из 23 повтором не перегоняется.
    check("25 закон пачек: нечитаемая чужая задача не отнимает прогон у остальных",
          (2, [["hr.new"]]), (acc._task_mutations(["hr.new", "hr.unread"]), ran))
    check("26 закон пачек: дыры своей задачи в общем заходе — код 1", 1,
          acc._own_view(["hr.a"], 1, ["мутации задачи hr.b: дыр 0, бумага b",
                                      "мутации задачи hr.a: дыр 2, бумага a"])[0])
    code, seen = acc._own_view(["hr.a"], 0, ["мутации задачи hr.b: дыр 0, бумага b"])
    check("27 закон пачек: заход без итога своей задачи — код 2 и слово, а не тихий 0",
          (2, True), (code, any("не дал итога" in line for line in seen)))


# Подставной прибор с задержкой: механизм из STUB_SLOW спит STUB_SLEEP с; каждый
# вызов — строка «механизм|MUTATION_DEADLINE_TS» в журнал STUB_CALLS.
SLOW_PROBE = '''#!/usr/bin/env python3
import os, sys, time
name = sys.argv[-1]
with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as fh:
    fh.write(name + "|" + os.environ.get("MUTATION_DEADLINE_TS", "") + "\\n")
if name == os.environ.get("STUB_SLOW"):
    time.sleep(float(os.environ.get("STUB_SLEEP", "60")))
print("=== мутационная проба: покраснели как надо 2, дыр 0, из кэша 0 ===")
'''


def probe_resume(cp, stand: Stand, tmp: Path) -> None:
    """Обрыв захода (14.11: 240 мин, 0 бумаг): готовое ложится по мере готовности,
    вывод построчный, повтор гонит только недоделанное, срок — мягкая остановка."""
    root = stand.root
    scripts = root / "scripts"
    shutil.copy(ACCEPTANCE, scripts / "acceptance.py")
    (scripts / "lib").symlink_to(ACCEPTANCE.resolve().parent / "lib")
    stub = tmp / "slow-probe.py"
    stub.write_text(SLOW_PROBE, encoding="utf-8")
    calls, logs = tmp / "resume-calls.log", tmp / "resume-logs"
    logs.mkdir()
    # История длительностей: SECOND долгий, MECHANISM быстрый — заход начинает с дешёвой задачи.
    now = int(time.time())
    (logs / "мутации-механизмов.tsv").write_text(
        f"{now}\t{SECOND}\t900\t2\t0\t0\n{now}\t{MECHANISM}\t3\t2\t0\t0\n", encoding="utf-8")
    stand.commit("hr.slow", SECOND)
    stand.commit("hr.fast", MECHANISM)
    env = {**os.environ, "HARNESS_MUTATION_PROBE": f"{sys.executable} {stub}",
           "HARNESS_IN_CHECKS_SLICE": "1", "STUB_CALLS": str(calls), "LOG_DIR": str(logs),
           "STUB_SLOW": SECOND, "STUB_SLEEP": "60"}
    env.pop("MUTATION_DEADLINE_TS", None)
    env.pop("PYTHONUNBUFFERED", None)
    paper = {t: root / ".tasks" / t / "мутации.log" for t in ("hr.slow", "hr.fast")}
    launch = tmp / "resume-launch.log"
    with launch.open("w", encoding="utf-8") as out:
        run = subprocess.Popen([sys.executable, str(scripts / "acceptance.py"), "--мутации-задачи",
                                "hr.slow,hr.fast"], cwd=str(root), env=env, stdout=out,
                               stderr=subprocess.STDOUT, start_new_session=True)
        limit = time.monotonic() + 20
        while time.monotonic() < limit and not paper["hr.fast"].is_file():
            time.sleep(0.2)
        time.sleep(1)
        os.killpg(run.pid, 9)  # как systemd по RuntimeMaxSec: SIGKILL, без уборки
        run.wait(timeout=10)
    said = launch.read_text(encoding="utf-8", errors="replace")
    check("28 обрыв: убитый заход оставил бумагу готовой задачи, недоделанной — нет",
          (True, [], False),
          (paper["hr.fast"].is_file(), debts(cp, root, "hr.fast") if paper["hr.fast"].is_file() else None,
           paper["hr.slow"].is_file()))
    check("29 обрыв: журнал запуска не пуст до смерти — строка механизма с длительностью и итог задачи",
          (True, True),
          (bool(re.search(rf"{re.escape(MECHANISM)}.*\d+ с", said)), "мутации задачи hr.fast: дыр 0" in said))
    calls.unlink()
    env["STUB_SLOW"] = ""
    rc = subprocess.run([sys.executable, str(scripts / "acceptance.py"), "--мутации-задачи",
                         "hr.slow,hr.fast"], cwd=str(root), env=env, capture_output=True,
                        text=True, timeout=60).returncode
    ran = [l.split("|")[0] for l in calls.read_text(encoding="utf-8").splitlines()] if calls.is_file() else []
    check("30 повтор: прибор зван только для недоделанного, обе бумаги зелёные, код 0",
          ([SECOND], [], [], 0), (ran, debts(cp, root, "hr.fast"), debts(cp, root, "hr.slow"), rc))
    history = (logs / "мутации-механизмов.tsv").read_text(encoding="utf-8").splitlines()
    check("31 история длительностей пополняется по механизму", True,
          any(line.split("\t")[1:2] == [SECOND] and line.split("\t")[0] != str(now) for line in history))

    # Мягкий срок захода: механизм, начатый у срока, кончается; следующий не начат.
    stand.commit("hr.slow", SECOND)
    stand.commit("hr.fast", MECHANISM)
    calls.unlink()
    deadline = str(int(time.time()) + 2)
    env.update({"STUB_SLOW": MECHANISM, "STUB_SLEEP": "4", "MUTATION_DEADLINE_TS": deadline})
    done = subprocess.run([sys.executable, str(scripts / "acceptance.py"), "--мутации-задачи",
                           "hr.slow,hr.fast"], cwd=str(root), env=env, capture_output=True,
                          text=True, timeout=60)
    ran = [l.split("|") for l in calls.read_text(encoding="utf-8").splitlines()] if calls.is_file() else []
    check("32 срок: после срока новый механизм не начат, задача названа «не догнана», код 2",
          ([[MECHANISM, deadline]], True, 2),
          (ran, "мутации задачи hr.slow: не догнана" in done.stdout, done.returncode))
    acc = load(scripts / "acceptance.py", "acceptance_resume_stand")
    check("33 план без истории: задача с меньшим числом механизмов — первой",
          [SECOND, MECHANISM, PLAIN],
          acc.batch_plan({"hr.big": [MECHANISM, PLAIN], "hr.small": [SECOND]}, {}))


def probe_live(cp) -> None:
    live = cp._модуль_цены().конфиг()
    steps = live.get("шаги") or {}
    check("16 живой пульт: мутации в мелкая/обычная/крупная, шаги_приёмки без неё",
          (True, True, True, False),
          tuple("мутации" in (steps.get(л) or []) for л in ("мелкая", "обычная", "крупная"))
          + ("мутации" in (live.get("шаги_приёмки") or []),))


def main() -> int:
    acc = load(ACCEPTANCE, "acceptance_under_test")
    cp = load(CHECK_PIPELINE, "check_pipeline_under_test")
    # Судья бумаги — acceptance.py под пробой, а не сосед живого check-pipeline.py.
    cp._SIBLINGS["acceptance"] = acc
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Стенд без git не должен найти чужой репозиторий выше себя.
        os.environ["GIT_CEILING_DIRECTORIES"] = str(tmp)
        for name, probe in (
                ("бумага", lambda: probe_paper(acc, cp, Stand(tmp / "repo"))),
                ("уровни", lambda: probe_levels(cp)),
                ("без git", lambda: probe_no_git(acc, cp, tmp / "nogit")),
                ("команда", lambda: probe_command(cp, Stand(tmp / "cmd"), tmp)),
                ("пачка", lambda: probe_batch(cp, Stand(tmp / "batch"), tmp)),
                ("чужие строки", lambda: probe_foreign_lines(acc, Stand(tmp / "foreign"))),
                ("удаление", lambda: probe_deleted_lines(acc, Stand(tmp / "deleted"))),
                ("чужое удаление", lambda: probe_foreign_deletion(acc, Stand(tmp / "fdel"))),
                ("своё удаление", lambda: probe_own_deletion_shifted(acc, Stand(tmp / "odel"))),
                ("комментарии", lambda: probe_comment_lines(acc, Stand(tmp / "comments"))),
                ("закон пачек", lambda: probe_batch_law(Stand(tmp / "law"), tmp)),
                ("обрыв захода", lambda: probe_resume(cp, Stand(tmp / "resume"), tmp)),
                ("пульт", lambda: probe_live(cp))):
            try:
                probe()
            except Exception as error:  # упавший судья — красный путь, а не трассировка
                check(f"{name}: судья отвечает", "ответ", f"{type(error).__name__}: {error}")
    print(f"шаг мутаций: {cases.paths} путей, {cases.failures} ПЛОХО")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
