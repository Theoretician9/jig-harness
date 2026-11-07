#!/usr/bin/env python3
"""Пробы ускорения мутаций (слово владельца 06.11: мутации — узкое горлышко,
задача сторожа — 194 мутации, 10 ч).

    python3 scripts/test_mutation_speed.py

Три причины:
  (а) mutation-probe.sh гоняет чистую копию пробы на КАЖДУЮ мутацию — у N
      мутаций одной пробы чистый прогон обязан быть один;
  (б) acceptance.py --мутации-задачи берёт все мутации тронутых механизмов —
      мутация, чья строка-цель вне строк, изменённых задачей, в шаг не входит;
      полный набор (без задачи) не сужается;
  (в) второй прогон мутаций ждёт первый (замок) — запуски не пересекаются.

Стенд — временный каталог: копии mutation-probe.sh, двери, acceptance.py и
lib/, свой механизм scripts/mech.sh и подставная проба scripts/probe_mech.sh,
которая пишет каждый свой запуск (метка, начало/конец, чистая/испорченная
копия) в файл MUTPROBE_SPEED_COUNT. Свои LOG_DIR и HARNESS_HOME; живой
мутации.log не трогается.

Дверь для мутационного прогона:
    HARNESS_MUTATION_SPEED_SRC   каталог скриптов под пробой (по умолчанию свой)
Последняя строка: `скорость мутаций: N путей, F ПЛОХО`.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

SRC = Path(os.environ.get("HARNESS_MUTATION_SPEED_SRC") or HERE)
TASK = "hr.проба-скорости"

cases = Cases()
check = cases.check

MECH = "#!/usr/bin/env bash\nA=1\nB=1\nC=1\n"
# Проба зелёная, только если все три строки на месте; пишет каждый запуск.
PROBE = """#!/usr/bin/env bash
ok() { grep -q '^A=1$' "$1" && grep -q '^B=1$' "$1" && grep -q '^C=1$' "$1"; }
state=mutated; ok "$1" && state=clean
[ -n "${MUTPROBE_SPEED_BROKEN:-}" ] && state=broken
echo "${MUTPROBE_SPEED_TAG:-x} start $(date +%s.%N) $state" >> "$MUTPROBE_SPEED_COUNT"
sleep "${MUTPROBE_SPEED_SLEEP:-0}"
echo "${MUTPROBE_SPEED_TAG:-x} end $(date +%s.%N) $state" >> "$MUTPROBE_SPEED_COUNT"
[ "$state" = clean ]
"""
PROBE_HOOK = """#!/usr/bin/env bash
# Чистая — копия механизма равна образцу И сосед harness/config/flag = ok.
# На первой испорченной копии «чужая правка» дописывает строку в живой файл
# стенда (исходник, саму пробу или соседа): как другой помощник между мутациями.
m="$1"; d="$(cd "$(dirname "$m")/.." && pwd)"
state=mutated
cmp -s "$m" "$MUTPROBE_SPEED_REF" && [ "$(cat "$d/harness/config/flag" 2>/dev/null)" = ok ] && state=clean
echo "x start 0 $state" >> "$MUTPROBE_SPEED_COUNT"
if [ "$state" = mutated ] && [ -n "${MUTPROBE_SPEED_HOOK:-}" ] && [ ! -e "$MUTPROBE_SPEED_HOOK_DONE" ]; then
    : > "$MUTPROBE_SPEED_HOOK_DONE"
    printf '%s\\n' "$MUTPROBE_SPEED_HOOK_LINE" >> "$MUTPROBE_SPEED_HOOK"
fi
[ "$state" = clean ]
"""
PROBE_REF = """#!/usr/bin/env bash
cmp -s "$1" "$MUTPROBE_SPEED_REF/$(basename "$1")"
"""


def table(mutations: list[tuple[str, str]]) -> str:
    exprs = ", ".join(f'"{e}"' for e, _ in mutations)
    names = ", ".join(f'"{n}"' for _, n in mutations)
    return ("инварианты:\n  - id: скорость\n    файл: scripts/mech.sh\n"
            "    доказательство: bash scripts/probe_mech.sh scripts/mech.sh\n"
            f"    мутация: [{exprs}]\n    мутация_случай: [{names}]\n")


ALL = [("s/^A=1$/A=2/", "a"), ("s/^B=1$/B=2/", "b"), ("s/^C=1$/C=2/", "c")]


class Stand:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.root = tmp / "дерево"
        scripts = self.root / "scripts"
        shutil.copytree(SRC / "lib", scripts / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("acceptance.py", "mutation-probe.sh", "run-in-checks-slice.sh"):
            if (SRC / name).is_file():
                shutil.copy2(SRC / name, scripts / name)
        (scripts / "mech.sh").write_text(MECH, encoding="utf-8")
        (scripts / "probe_mech.sh").write_text(PROBE, encoding="utf-8")
        (self.root / "harness" / "config").mkdir(parents=True)
        self.write_table(ALL)
        (tmp / "home").mkdir()
        self.count = tmp / "запуски.txt"
        self.cgroup = tmp / "cgroup"
        self.cgroup.write_text("0::/user.slice/session-1.scope\n", encoding="utf-8")

    def write_table(self, mutations: list) -> None:
        (self.root / "harness" / "config" / "invariants.yaml").write_text(table(mutations), encoding="utf-8")

    def env(self, **extra: str) -> dict:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.tmp / "home"),
               "LANG": "C.UTF-8", "HARNESS_HOME": str(self.tmp / "home"),
               "LOG_DIR": str(self.tmp / "home" / "log"), "MUT_CACHE_DAYS": "0",
               "HARNESS_PROBE_MUTATIONS": str(self.tmp / "нет.yaml"),
               "HARNESS_CHECKS_SLICE_LOCK": str(self.tmp / "очередь.lock"),
               "HARNESS_IN_CHECKS_SLICE": "1", "MUTPROBE_SPEED_COUNT": str(self.count),
               "GIT_CEILING_DIRECTORIES": str(self.tmp)}
        env.update(extra)
        return env

    def runs(self, tag: str = "x") -> list[tuple[str, float, str]]:
        """Запуски пробы метки: (start|end, время, clean|mutated|broken)."""
        if not self.count.is_file():
            return []
        out = []
        for line in self.count.read_text(encoding="utf-8").splitlines():
            t, kind, when, state = line.split()
            if t == tag:
                out.append((kind, float(when), state))
        return out

    def starts(self, state: str, tag: str = "x") -> int:
        return sum(1 for k, _, s in self.runs(tag) if k == "start" and s == state)

    def probe(self, *args: str, env: dict) -> tuple[int, str]:
        self.count.unlink(missing_ok=True)
        r = subprocess.run(["bash", str(self.root / "scripts" / "mutation-probe.sh"), *args],
                           cwd=str(self.tmp), env=env, capture_output=True, text=True,
                           errors="replace", timeout=300)
        return r.returncode, r.stdout + r.stderr

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=probe", "-c", "user.email=p@s",
                        *args], check=True, capture_output=True, timeout=60)


def probe_clean_once(s: Stand) -> None:
    """(а) N мутаций одной пробы — чистая копия гоняется один раз."""
    for args, label in (((), "полный набор"), (("--механизм", "scripts/mech.sh"), "--механизм")):
        rc, out = s.probe(*args, env=s.env())
        check(f"(а) {label}: 3 мутации пойманы, дыр 0", True, "покраснели как надо 3, дыр 0" in out)
        check(f"(а) {label}: испорченная копия гоняется на каждую мутацию (3)", 3, s.starts("mutated"))
        check(f"(а) {label}: чистая копия гоняется ровно 1 раз на 3 мутации", 1, s.starts("clean"))
    # Красная чистая копия по-прежнему красит КАЖДУЮ мутацию: ускорение не
    # смеет превратить «не доказано» в «пропущено».
    rc, out = s.probe(env=s.env(MUTPROBE_SPEED_BROKEN="1"))
    check("(а) красная чистая копия: все 3 мутации — дыры", True, "покраснели как надо 0, дыр 3" in out)


def probe_task_lines(s: Stand) -> None:
    """(б) шаг задачи берёт только мутации, чья строка-цель изменена задачей."""
    mech = s.root / "scripts" / "mech.sh"
    s.write_table([ALL[0], ALL[2]])
    mech.write_text(MECH.replace("A=1", "A=0"), encoding="utf-8")
    s.git("init", "-q")
    s.git("add", "-A")
    s.git("commit", "-q", "-m", "база")
    mech.write_text(MECH, encoding="utf-8")  # задача правит строку 2 (A), строка 4 (C) не тронута
    s.git("commit", "-q", "-am", f"правка A\n\nDev-Map: {TASK}")

    def task(ид: str) -> str:
        subprocess.run([sys.executable, str(s.root / "scripts" / "acceptance.py"), "--мутации-задачи", ид],
                       cwd=str(s.root), env=s.env(), capture_output=True, timeout=300)
        paper = s.root / ".tasks" / ид / "мутации.log"
        return paper.read_text(encoding="utf-8") if paper.is_file() else ""

    paper = task(TASK)
    check("(б) бумага задачи написана", True, bool(paper))
    check("(б) мутация на изменённой строке входит в шаг задачи", True, "скорость: a" in paper)
    check("(б) мутация на НЕтронутой строке в шаг задачи не входит", False, "скорость: c" in paper)
    for args, label in (((), "полный набор"), (("--механизм", "scripts/mech.sh"), "--механизм")):
        _, out = s.probe(*args, env=s.env())
        check(f"(б) {label} без задачи не сужается: a и c", [True, True],
              ["скорость: a" in out, "скорость: c" in out])


def probe_lock(s: Stand) -> None:
    """(в) два параллельных прогона через дверь: второй ждёт первый. Обёртка —
    копия настоящей со стендовым systemd-run и каталогом замков."""
    from test_checks_slice_door import SliceStand
    ss = SliceStand(s.tmp / "срез")
    door = s.root / "scripts" / "run-in-checks-slice.sh"
    if door.is_file():
        door.write_text(ss.localize(door.read_text(encoding="utf-8")), encoding="utf-8")
    s.count.unlink(missing_ok=True)
    procs = {}
    for tag, expr in (("one", "s/^A=1$/A=2/"), ("two", "s/^A=1$/A=3/")):
        # Разные выражения — разные ключи кэша: второй не отвечает из кэша первого.
        t = s.tmp / f"таблица-{tag}.yaml"
        t.write_text(table([(expr, tag)]), encoding="utf-8")
        env = s.env(MUTPROBE_SPEED_TAG=tag, MUTPROBE_SPEED_SLEEP="1", HARNESS_INVARIANTS=str(t),
                    HARNESS_CHECKS_SLICE_WRAPPER=str(ss.wrapper),
                    HARNESS_CHECKS_SLICE_CGROUP_FILE=str(s.cgroup),
                    HARNESS_CHECKS_SLICE_LOCK=str(ss.lock),
                    PATH=f"{ss.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}")
        del env["HARNESS_IN_CHECKS_SLICE"]
        procs[tag] = subprocess.Popen(["bash", str(s.root / "scripts" / "mutation-probe.sh")],
                                      cwd=str(s.tmp), env=env, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
    rcs = {tag: p.wait(timeout=300) for tag, p in procs.items()}
    check("(в) оба прогона кончились зелёными (второй ждал, а не отказал)", {"one": 0, "two": 0}, rcs)
    spans = {}
    for tag in procs:
        times = [w for _, w, _ in s.runs(tag)]
        spans[tag] = (min(times), max(times)) if times else None
    check("(в) каждый прогон гонял пробу", True, all(spans.values()))
    if all(spans.values()):
        (a0, a1), (b0, b1) = spans["one"], spans["two"]
        check("(в) запуски проб двух прогонов не пересекаются", True, a1 <= b0 or b1 <= a0)


# ── Круг 2 (ревью review/generalist.yaml) ──────────────────────────────────

def lines_file(prefix: str, n: int, zero: tuple = (), extra: dict | None = None) -> str:
    """Строки «<prefix><i>=1», i=1..n; номера из zero — «=0»; extra вставляет
    строку ПЕРЕД номером (сдвиг нумерации — для удаления в G-08)."""
    out = []
    for i in range(1, n + 1):
        if extra and i in extra:
            out.append(extra[i])
        out.append(f"{prefix}{i}={0 if i in zero else 1}")
    return "\n".join(out) + "\n"


def record(ид: str, файл: str, mutations: list) -> str:
    exprs = ", ".join(f'"{e}"' for e, _ in mutations)
    names = ", ".join(f'"{n}"' for _, n in mutations)
    return (f"  - id: {ид}\n    файл: {файл}\n    доказательство: bash scripts/probe_ref.sh {файл}\n"
            f"    мутация: [{exprs}]\n    мутация_случай: [{names}]\n")


M1 = record("m1", "scripts/mech.sh", [("s/^L50=1$/L50=2/", "l50"), ("s/^L60=1$/L60=2/", "l60")])
M2 = record("m2", "scripts/mech2.sh", [("s/^M20=1$/M20=2/", "m20")])
M3 = record("m3", "scripts/mech3.sh", [("s/^N40=1$/N40=2/", "n40"), ("s/^N90=1$/N90=2/", "n90")])


class TaskStand(Stand):
    """git-стенд: базовый коммит и один коммит задачи `Dev-Map: <ид>`."""

    def __init__(self, tmp: Path, ид: str, records: str, base: dict, task: dict) -> None:
        super().__init__(tmp)
        self.ид = ид
        (self.root / "harness" / "config" / "invariants.yaml").write_text(
            "инварианты:\n" + records, encoding="utf-8")
        (self.root / "scripts" / "probe_ref.sh").write_text(PROBE_REF, encoding="utf-8")
        self.ref = tmp / "образец"
        self.ref.mkdir()
        for name, text in base.items():
            (self.root / name).write_text(text, encoding="utf-8")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "база")
        for name, text in task.items():
            (self.root / name).write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"правка задачи\n\nDev-Map: {ид}")
        for name in base:
            shutil.copy2(self.root / name, self.ref / Path(name).name)

    def env(self, **extra: str) -> dict:
        return super().env(MUTPROBE_SPEED_REF=str(self.ref), **extra)

    def accept(self, **extra: str) -> tuple[int, str, str]:
        """(код, вывод, бумага) шага мутаций задачи."""
        r = subprocess.run([sys.executable, str(self.root / "scripts" / "acceptance.py"),
                            "--мутации-задачи", self.ид], cwd=str(self.root), env=self.env(**extra),
                           capture_output=True, text=True, errors="replace", timeout=300)
        paper = self.root / ".tasks" / self.ид / "мутации.log"
        return r.returncode, r.stdout + r.stderr, paper.read_text(encoding="utf-8") if paper.is_file() else ""


def uncovered(text: str) -> list[str]:
    return [l for l in text.splitlines() if "правка без мутаций" in l]


def g01(tmp: Path) -> None:
    """G-01: правка механизма, которую не задевает ни одна мутация, — дыра, а не «дыр 0»."""
    s = TaskStand(tmp / "1", "hr.g01-a", M1, {"scripts/mech.sh": lines_file("L", 70, zero=(10,))},
                  {"scripts/mech.sh": lines_file("L", 70)})
    rc, out, paper = s.accept()
    check("G-01 больной: правка строки 10 без мутаций — код ≠ 0", True, rc != 0)
    check("G-01 больной: итог называет «правка без мутаций: scripts/mech.sh строки 10–10»", True,
          any("scripts/mech.sh" in l and re.search(r"10[–-]10", l) for l in uncovered(out)))
    check("G-01 больной: то же в мутации.log задачи", True, bool(uncovered(paper)))
    s = TaskStand(tmp / "2", "hr.g01-b", M1, {"scripts/mech.sh": lines_file("L", 70, zero=(50,))},
                  {"scripts/mech.sh": lines_file("L", 70)})
    rc, out, paper = s.accept()
    check("G-01: правка задевает строку мутации — зелёный, мутация l50 поймана, без «правка без мутаций»",
          [0, True, []], [rc, "мутация поймана: m1: l50" in paper, uncovered(out + paper)])
    s = TaskStand(tmp / "3", "hr.g01-c", M1, {"scripts/mech.sh": lines_file("L", 70), "scripts/plain.txt": "x\n"},
                  {"scripts/plain.txt": "y\n"})
    rc, out, _ = s.accept()
    check("G-01: правка без механизмов с мутациями — прежнее «шаг пуст», код 0", [0, True],
          [rc, "шаг пуст" in out])
    s = TaskStand(tmp / "4", "hr.g01-d", M1 + M2,
                  {"scripts/mech.sh": lines_file("L", 70, zero=(60,)), "scripts/mech2.sh": lines_file("M", 30, zero=(5,))},
                  {"scripts/mech.sh": lines_file("L", 70), "scripts/mech2.sh": lines_file("M", 30)})
    rc, out, paper = s.accept()
    names = uncovered(out + paper)
    check("G-01: два механизма — дыра называет второй (mech2), не первый", [True, False],
          [any("scripts/mech2.sh" in l for l in names), any("scripts/mech.sh" in l for l in names)])


def g02(tmp: Path) -> None:
    """G-02/G-10: память чистой копии не переживает правку исходника, пробы, соседа."""
    for label, hook, line in (("исходник", "scripts/mech.sh", "# чужая правка"),
                              ("файл пробы", "scripts/probe_hook.sh", "exit 1"),
                              ("сосед harness/config/flag", "harness/config/flag", "сломан")):
        s = Stand(tmp / label.split()[0])
        (s.root / "scripts" / "probe_hook.sh").write_text(PROBE_HOOK, encoding="utf-8")
        (s.root / "harness" / "config" / "flag").write_text("ok\n", encoding="utf-8")
        (s.tmp / "home" / "log").mkdir(parents=True)
        (s.root / "harness" / "config" / "invariants.yaml").write_text(
            table(ALL).replace("probe_mech.sh", "probe_hook.sh"), encoding="utf-8")
        ref = s.tmp / "образец.sh"
        ref.write_text(MECH, encoding="utf-8")
        _, out = s.probe(env=s.env(MUT_CACHE_DAYS="7", MUTPROBE_SPEED_REF=str(ref),
                                   MUTPROBE_SPEED_HOOK=str(s.root / hook), MUTPROBE_SPEED_HOOK_LINE=line,
                                   MUTPROBE_SPEED_HOOK_DONE=str(s.tmp / "правка.done")))
        prefix = "G-02 больной" if label == "исходник" else "G-02"
        check(f"{prefix}: правка ({label}) между мутациями — чистая копия перемерена, «чистая копия уже красная»",
              True, "чистая копия уже красная" in out)
        if label == "исходник":
            cache = s.tmp / "home" / "log" / "mutations-cache.tsv"
            rows = [r.split("\t") for r in cache.read_text(encoding="utf-8").splitlines()] if cache.is_file() else []
            check("G-02 больной: ложная зелень после правки в кэш не записана (b, c)", [],
                  [r[2] for r in rows if len(r) >= 3 and r[2].endswith((": b", ": c")) and not (r[3:] and r[3])])
    print("  SKIP  G-10: «из-копии» и без подмены с одним src/proof/probe из таблиц не строятся "
          "(ключ класса D — путь пробы, у инвариантов проба «-»); столкновение только теоретическое")


def g04_acceptance(tmp: Path) -> None:
    """G-04: acceptance отличает «очередь среза занята» (код 75) от дыры."""
    s = TaskStand(tmp, "hr.g04", M1, {"scripts/mech.sh": lines_file("L", 70, zero=(50,))},
                  {"scripts/mech.sh": lines_file("L", 70)})
    busy = s.tmp / "занято.sh"
    busy.write_text("#!/usr/bin/env bash\necho 'run-in-checks-slice: очередь среза занята "
                    "(держатель 4242 sleep 15:00)' >&2\nexit 75\n", encoding="utf-8")
    rc, out, paper = s.accept(HARNESS_MUTATION_PROBE=f"bash {busy}")
    check("G-04 больной: код 75 прибора — «не прогнано: очередь среза занята», не «дыр N»", [True, False],
          ["не прогнано: очередь среза занята" in out + paper, bool(re.search(r"дыр [1-9]", out))])


def g08(tmp: Path) -> None:
    """G-08: ханк чистого удаления (+N,0) отдаёт строки по краям удаления."""
    s = TaskStand(tmp, "hr.g08", M3, {"scripts/mech3.sh": lines_file("N", 95, zero=(90,), extra={41: "GUARD=1"})},
                  {"scripts/mech3.sh": lines_file("N", 95)})
    _, _, paper = s.accept()
    check("G-08 больной: удалена строка 41 — мутация соседней строки (N40) входит в шаг", True, "m3: n40" in paper)
    check("G-08: изменённая строка 90 — её мутация входит", True, "m3: n90" in paper)


def g09(tmp: Path) -> None:
    """G-09: ночной --мутации-долга гоняет полный набор (MUT_ONLY_LINES нет в env прибора)."""
    import importlib.util
    s = TaskStand(tmp, "hr.g09", M1, {"scripts/mech.sh": lines_file("L", 70, zero=(50,))},
                  {"scripts/mech.sh": lines_file("L", 70)})
    seen = s.tmp / "env.txt"
    dump = s.tmp / "env.sh"
    dump.write_text(f'#!/usr/bin/env bash\necho "${{MUT_ONLY_LINES-<нет>}}" >> {seen}\n'
                    'echo "=== мутационная проба: покраснели как надо 2, дыр 0, из кэша 0 ==="\n', encoding="utf-8")
    spec = importlib.util.spec_from_file_location("acceptance_g09", s.root / "scripts" / "acceptance.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.карточки = lambda карта: [{"id": s.ид, "status": "test"}]
    mod.долг_мутаций_задачи = lambda задача, корень: "мутации: долг"
    saved = os.environ.pop("MUT_ONLY_LINES", None)
    try:
        mod.ночные_мутации({}, s.root, ["bash", str(dump)], lambda доводы: None)
    finally:
        if saved is not None:
            os.environ["MUT_ONLY_LINES"] = saved
    got = seen.read_text(encoding="utf-8").split() if seen.is_file() else []
    check("G-09 больной: ночной путь не сужает (MUT_ONLY_LINES нет у прибора)", ["<нет>"], got)


def data_files(tmp: Path) -> None:
    """Файл данных — не механизм для G-01: правка строки без мутаций в
    harness/config/*.conf дырой не считается; в скрипте — считается."""
    for label, файл, sick in (("harness/config/x.conf", "harness/config/x.conf", True),
                              ("scripts/x.sh", "scripts/x.sh", False)):
        ид = "hr.data-" + Path(файл).suffix.lstrip(".")
        s = TaskStand(tmp / ид, ид, record("x", файл, [("s/^L50=1$/L50=2/", "l50")]),
                      {файл: lines_file("L", 70, zero=(10,))}, {файл: lines_file("L", 70)})
        rc, out, paper = s.accept()
        if sick:
            check(f"данные больной: {label}, правка строки 10 без мутаций — не дыра: код 0, "
                  "без «правка без мутаций», итог «покраснели 0, дыр 0»", [0, [], True],
                  [rc, uncovered(out + paper), "покраснели 0, дыр 0" in paper])
        else:
            check(f"данные: {label}, тот же сценарий — по-прежнему дыра «правка без мутаций»", [True, True],
                  [rc != 0, any(файл in l for l in uncovered(out + paper))])


def night_queue_busy(tmp: Path) -> None:
    """Ночь: отказ очереди среза (код 75) не заводит и не гасит карточку
    «слабая проба» — механизм пропущен, ночь повторит."""
    import importlib.util
    s = TaskStand(tmp, "hr.night-busy", M1, {"scripts/mech.sh": lines_file("L", 70, zero=(50,))},
                  {"scripts/mech.sh": lines_file("L", 70)})
    busy = s.tmp / "занято.sh"
    busy.write_text("#!/usr/bin/env bash\necho 'run-in-checks-slice: очередь среза занята' >&2\nexit 75\n",
                    encoding="utf-8")
    spec = importlib.util.spec_from_file_location("acceptance_night_busy", s.root / "scripts" / "acceptance.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.карточки = lambda карта: [{"id": s.ид, "status": "test"}]
    mod.долг_мутаций_задачи = lambda задача, корень: "мутации: долг"
    written = []
    try:
        итог = mod.ночные_мутации({}, s.root, ["bash", str(busy)], written.append)
    except Exception as error:  # отказ очереди не должен ронять ночь
        итог = f"{type(error).__name__}: {error}"
    check("ночь больной: очередь занята (75) — карточка не заведена и не погашена, задача пропущена",
          [[], []], [written, итог])


PROBE_ENV = """#!/usr/bin/env bash
m="${1:-$MUTPROBE_SPEED_MECH}"
echo "${MUT_ONLY_LINES-<нет>}" >> "$MUTPROBE_SPEED_SEEN"
[ -z "${MUT_ONLY_LINES+x}" ] || exit 1
grep -q '^A=1$' "$m" && grep -q '^B=1$' "$m" && grep -q '^C=1$' "$m"
"""


def only_lines_not_leaked(tmp: Path) -> None:
    """MUT_ONLY_LINES — указ прибору, а не команде доказательства: вложенный
    стенд пробы с ним в окружении краснеет на чистой копии. Обе ветки `прогнать`."""
    exprs = ", ".join(f'"{e}"' for e, _ in ALL)
    names = ", ".join(f'"{n}"' for _, n in ALL)
    for label, proof, subst in (("путь в команде", "bash scripts/probe_env.sh scripts/mech.sh", ""),
                                ("подмена через окружение", "bash scripts/probe_env.sh",
                                 "    мутация_подмена: MUTPROBE_SPEED_MECH\n")):
        s = Stand(tmp / label.split()[0])
        (s.root / "scripts" / "probe_env.sh").write_text(PROBE_ENV, encoding="utf-8")
        (s.root / "harness" / "config" / "invariants.yaml").write_text(
            "инварианты:\n  - id: скорость\n    файл: scripts/mech.sh\n"
            f"    доказательство: {proof}\n{subst}    мутация: [{exprs}]\n    мутация_случай: [{names}]\n",
            encoding="utf-8")
        seen = s.tmp / "видела-проба.txt"
        _, out = s.probe(env=s.env(MUT_ONLY_LINES="2,3,4", MUTPROBE_SPEED_SEEN=str(seen)))
        got = set(seen.read_text(encoding="utf-8").split()) if seen.is_file() else set()
        check(f"MUT_ONLY_LINES больной ({label}): не доходит до доказательства, 3 мутации пойманы",
              [True, {"<нет>"}], ["покраснели как надо 3, дыр 0" in out, got])


def g11() -> None:
    """G-11: новые идентификаторы — английские."""
    names = ("def строки_правок_задачи", "вне_правок", "коммиты = _git_lines", "дифф = _git_lines",
             "ход = re.match", "среда = dict(os.environ)", "дверь = str(")
    text = "".join((SRC / f).read_text(encoding="utf-8") for f in ("acceptance.py", "mutation-probe.sh")
                   if (SRC / f).is_file())
    check("G-11 больной: новых русских идентификаторов (def/local) нет", [], [n for n in names if n in text])


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for name, probe in (("(а)", lambda: probe_clean_once(Stand(tmp / "a"))),
                            ("(б)", lambda: probe_task_lines(Stand(tmp / "b"))),
                            ("(в)", lambda: probe_lock(Stand(tmp / "c"))),
                            ("G-01", lambda: g01(tmp / "g01")),
                            ("G-02", lambda: g02(tmp / "g02")),
                            ("G-04", lambda: g04_acceptance(tmp / "g04")),
                            ("G-08", lambda: g08(tmp / "g08")),
                            ("G-09", lambda: g09(tmp / "g09")),
                            ("G-11", g11),
                            ("данные", lambda: data_files(tmp / "data")),
                            ("ночь", lambda: night_queue_busy(tmp / "night")),
                            ("MUT_ONLY_LINES", lambda: only_lines_not_leaked(tmp / "only"))):
            try:
                probe()
            except Exception as error:  # упавший стенд — красный путь, а не трассировка
                check(f"{name}: стенд отвечает", "ответ", f"{type(error).__name__}: {error}")
    print(f"скорость мутаций: {cases.paths} путей, {cases.failures} ПЛОХО")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
