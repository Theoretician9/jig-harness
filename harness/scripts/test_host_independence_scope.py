#!/usr/bin/env python3
"""Пробы круга прибора «хозяин машины» (hr.host-independence-only-changed).

Второй круг гоняет не всё, а изменённое с последнего чистого полного круга: иначе
он занимает 167 из 237 минут ворот. Цена ошибки в обе стороны настоящая: круг, не
взявший изменённую пробу, молчит о зависимости от машины, а база, сдвинутая
красным или частичным кругом, навсегда прячет то, что круг не гонял. Копиям
проверка не нужна вовсе — у них она выключена ключом.

Контракт прибора `scripts/check-host-independence.py`:
- `choose_circle(gates_path, mark_path, root, now, full_days) -> (mode, checks, reason)`:
  mode — «полный» или «изменённые», checks — как у `проверки()`, reason — русская строка;
- `write_mark(mark_path, root, now)` — JSON {"sha": HEAD, "ts": now};
- метка `$LOG_DIR/host-independence-full.json`, путь подменяет HOST_INDEPENDENCE_MARK;
- ключи harness.conf: HOST_INDEPENDENCE_FULL_DAYS (7), HOST_INDEPENDENCE_CHECK.

Толкование там, где контракт молчит: `now` и `ts` — секунды эпохи числом.

Каждый случай строит свой стенд в своём каталоге: git-репозиторий с мини-воротами,
своим реестром механизмов, своими данными прибора и копией прибора под настоящим
именем (КОРЕНЬ прибора считается от его файла, поэтому прогон процессом судит
стенд, а не живое дерево). Живые журналы и живой конфиг не читаются и не пишутся.
Случаи про выключение идут через копию НАСТОЯЩЕГО gates.sh с заглушкой прибора:
условие судит сам bash, а не grep по тексту.

Двери для мутаций: HOST_INDEPENDENCE_BIN — прибор, HOST_SCOPE_GATES_SH — ворота,
HOST_SCOPE_SAMPLE_CONF — образец harness.conf.

    python3 scripts/test_host_independence_scope.py [--случай <имя>]
"""
from __future__ import annotations

import collections
import contextlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = Path(os.environ.get("HOST_INDEPENDENCE_BIN")
           or ROOT / "scripts" / "check-host-independence.py")
GATES_SH = Path(os.environ.get("HOST_SCOPE_GATES_SH") or ROOT / "scripts" / "gates.sh")
SAMPLE_CONF = Path(os.environ.get("HOST_SCOPE_SAMPLE_CONF")
                   or ROOT / "harness" / "config" / "harness.conf")

INSTRUMENT = "scripts/check-host-independence.py"
INSTRUMENT_DATA = "harness/config/host-independence.yaml"
MARK_NAME = "host-independence-full.json"
FULL = "полный"
PARTIAL = "изменённые"
LOUD_EMPTY = "изменённых нет"
GATE_LINE = "хозяин машины"
FULL_DAYS = 7
DAY = 86400
NOW = int(time.time())
CASE_LIMIT_SEC = 90
RUN_LIMIT_SEC = 60

# Стенду нельзя наследовать чужое: в pre-commit GIT_DIR и GIT_INDEX_FILE увели бы
# git стенда в настоящий репозиторий, а ключи прибора подменили бы условие случая.
for _name in list(os.environ):
    if _name.startswith(("GIT_", "HOST_INDEPENDENCE_")):
        del os.environ[_name]

RESULTS: list[tuple[str, bool, str]] = []

# Проверки стенда: имя, команда, файл. «поздняя» зовёт файл, которого в базе нет;
# «тяжёлая» объявлена в данных прибора и не гоняется вовсе.
CHECKS = (
    ("изменяемая", "python3 scripts/test_changed.py", "scripts/test_changed.py"),
    ("тихая", "python3 scripts/test_quiet.py", "scripts/test_quiet.py"),
    ("через реестр", "python3 scripts/test_registry.py", "scripts/test_registry.py"),
    ("замыкание", "python3 scripts/test_closure.py", "scripts/test_closure.py"),
    ("импорт", "python3 scripts/test_import.py", "scripts/test_import.py"),
    ("оболочка", "bash scripts/test_shell.sh --selftest", "scripts/test_shell.sh"),
    ("тяжёлая", "python3 scripts/test_heavy.py", "scripts/test_heavy.py"),
)
LATE_CHECK = ("поздняя", "python3 scripts/test_late.py", "scripts/test_late.py")
BOUND_CHECK = ("зависимая", "python3 scripts/test_bound.py", "scripts/test_bound.py")

# Рядом лежат записи, какие есть в настоящем реестре: «проверяется» прозой, про
# файл вне ворот и без поля вовсе — реестр не обязан быть удобным.
MECHANISMS = """механизмы:
- файл: harness/demons/watched.sh
  вид: демон
  имя: Наблюдаемый
  проверяется: "python3 scripts/test_registry.py (3 пути, первым больной)"
- файл: harness/demons/other.sh
  вид: демон
  проверяется: bash harness/demons/other.sh --selftest
- файл: harness/demons/silent.sh
  вид: демон
"""
INSTRUMENT_DATA_TEXT = """судят_машину: []
тяжёлые:
  - проверка: "тяжёлая"
    причина: "идёт минутами"
потолок_секунд: 20
"""


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    tail = f" — {detail}" if detail and not ok else ""
    print(f"  {'ок   ' if ok else 'ПЛОХО'} {name}{tail}", flush=True)


def load_instrument():
    # Копия прибора за дверью лежит вне дерева: библиотеки ей показывает проба.
    sys.path.insert(0, str(ROOT / "scripts" / "lib"))
    spec =importlib.util.spec_from_file_location("check_host_independence", BIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INSTRUMENT_MODULE = load_instrument()


def trace_writer(trace: Path, name: str) -> str:
    return f'with open({str(trace)!r}, "a") as out:\n    out.write("{name}\\n")\n'


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=probe", "-c", "user.email=probe@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=root, capture_output=True, text=True, timeout=30, check=True)
    return done.stdout.strip()


def stand_dir() -> Path:
    # Литералом: реестр уборки сверяет префиксы разбором вызова mkdtemp.
    return Path(tempfile.mkdtemp(prefix="host-scope-probe-"))


def copy_lib(root: Path) -> None:
    shutil.copytree(ROOT / "scripts" / "lib", root / "scripts" / "lib",
                    ignore=shutil.ignore_patterns("__pycache__"))


class Stand:
    """Репозиторий с мини-воротами; база — первый коммит."""

    def __init__(self, extra_checks: tuple = (), conf: str = "") -> None:
        self.base = stand_dir()
        self.root = self.base / "repo"
        self.home = self.base / "home"
        self.log_dir = self.base / "log"
        self.trace = self.base / "trace"
        self.mark = self.base / "mark.json"
        self.gates = self.root / "scripts" / "gates.sh"
        self.checks = CHECKS + (LATE_CHECK,) + extra_checks
        self.home.mkdir()
        self.log_dir.mkdir()
        self._write_passport(conf)
        self._write_tree()
        git(self.root, "init", "-q")
        self.commit("база")
        self.head = git(self.root, "rev-parse", "HEAD")

    def _write_passport(self, conf: str) -> None:
        write(self.home / "install.conf",
              f'PROJECT_DIR="{self.root}"\nLOG_DIR="{self.log_dir}"\n')
        write(self.home / "harness.conf", conf)

    def _write_tree(self) -> None:
        copy_lib(self.root)
        lines = "".join(f'проверка "{name}" {command}\n' for name, command, _ in self.checks)
        write(self.gates, f"#!/usr/bin/env bash\n# мини-ворота стенда\n{lines}")
        write(self.root / INSTRUMENT, BIN.read_text(encoding="utf-8"))
        write(self.root / INSTRUMENT_DATA, INSTRUMENT_DATA_TEXT)
        write(self.root / "harness/config/mechanisms.yaml", MECHANISMS)
        write(self.root / "harness/demons/watched.sh", "#!/bin/bash\nexit 0\n")
        write(self.root / "harness/demons/other.sh", "#!/bin/bash\nexit 0\n")
        write(self.root / "harness/demons/silent.sh", "#!/bin/bash\nexit 0\n")
        write(self.root / "harness/config/settings_one.yaml", "порог: 1\n")
        write(self.root / "scripts/lib/helper_mod.py", "VALUE = 1\n")
        write(self.root / "scripts/tool_one.sh", "#!/bin/bash\n# зовёт scripts/deep_two.py\n")
        # Цикл упоминаний: замыкание обязано кончиться.
        write(self.root / "scripts/deep_two.py", "# вернётся в scripts/test_closure.py\n")
        write(self.root / "scripts/test_closure.py",
              trace_writer(self.trace, "замыкание")
              + 'TOOL = "scripts/tool_one.sh"\nSETTINGS = "settings_one.yaml"\n')
        write(self.root / "scripts/test_import.py",
              trace_writer(self.trace, "импорт")
              + 'import sys\nsys.path.insert(0, "scripts/lib")\nimport helper_mod\n')
        write(self.root / "scripts/test_shell.sh",
              f'#!/bin/bash\necho оболочка >> "{self.trace}"\n')
        write(self.root / "scripts/test_bound.py",
              trace_writer(self.trace, "зависимая")
              + 'import os, sys\nsys.exit(0 if os.path.isdir(os.environ.get("HOME", "")) else 1)\n')
        for name, _, path in self.checks:
            if name in ("изменяемая", "тихая", "через реестр", "тяжёлая"):
                write(self.root / path, trace_writer(self.trace, name))

    def commit(self, message: str) -> None:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)

    def touch(self, path: str) -> None:
        with open(self.root / path, "a", encoding="utf-8") as out:
            out.write("# правка\n")

    def put_mark(self, age_days: float = 1, sha: str | None = None) -> None:
        write(self.mark, json.dumps({"sha": sha or self.head,
                                     "ts": NOW - int(age_days * DAY)}))

    def circle(self) -> tuple[str, list[str], str]:
        mode, checks, reason = INSTRUMENT_MODULE.choose_circle(
            self.gates, self.mark, self.root, NOW, FULL_DAYS)
        return mode, [name for name, _ in checks], reason

    def all_names(self) -> list[str]:
        names = [name for name, _ in INSTRUMENT_MODULE.проверки(self.gates)]
        assert names, "мини-ворота не разобраны — судить нечего"
        return names

    def env(self, **extra: str) -> dict:
        env = {**os.environ, "HARNESS_HOME": str(self.home), "LOG_DIR": str(self.log_dir),
               "HOST_INDEPENDENCE_MARK": str(self.mark), "HOME": str(self.home),
               "USER": "stand", **extra}
        return env

    def run(self, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["python3", INSTRUMENT], cwd=self.root, capture_output=True,
                              text=True, timeout=RUN_LIMIT_SEC, env=env or self.env(),
                              stdin=subprocess.DEVNULL)

    def ran(self) -> collections.Counter:
        """Сколько раз какая проверка стенда прогнана на самом деле."""
        if not self.trace.exists():
            return collections.Counter()
        return collections.Counter(self.trace.read_text(encoding="utf-8").split("\n")[:-1])

    def close(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)


@contextlib.contextmanager
def stand(*args, **kwargs):
    """Стенд убирается и при красном случае."""
    current = Stand(*args, **kwargs)
    try:
        yield current
    finally:
        current.close()


def has_cyrillic(text: str) -> bool:
    return bool(re.search("[а-яё]", str(text), re.I))


def expect_changed(st: Stand, label: str, expected: set[str]) -> None:
    """Круг «изменённые» ровно из expected: лишнее здесь — те же 167 минут."""
    mode, names, reason = st.circle()
    check(f"{label}: режим «{PARTIAL}»", mode == PARTIAL, f"режим {mode!r}, причина {reason!r}")
    check(f"{label}: круг ровно {sorted(expected)}", set(names) == expected,
          f"в круге {sorted(names)}")
    check(f"{label}: причина по-русски", has_cyrillic(reason), repr(reason))


def expect_full(st: Stand, label: str) -> None:
    mode, names, reason = st.circle()
    check(f"{label}: режим «{FULL}»", mode == FULL, f"режим {mode!r}, причина {reason!r}")
    check(f"{label}: в круге все проверки ворот", names == st.all_names(),
          f"в круге {len(names)} из {len(st.all_names())}")
    check(f"{label}: причина по-русски", has_cyrillic(reason), repr(reason))


def changed_probe_enters() -> None:
    """БОЛЬНОЙ: изменённая проба обязана попасть в круг."""
    with stand() as st:
        st.put_mark()
        st.touch("scripts/test_changed.py")
        expect_changed(st, "правка в рабочем дереве", {"изменяемая"})
    with stand() as st:
        st.put_mark()
        st.touch("scripts/test_changed.py")
        st.commit("правка пробы")
        expect_changed(st, "правка в коммите после метки", {"изменяемая"})
    with stand() as st:
        st.put_mark()
        write(st.root / "scripts/test_late.py", "# новый неотслеживаемый файл\n")
        expect_changed(st, "неотслеживаемый файл пробы", {"поздняя"})


def unchanged_probe_stays_out() -> None:
    """Неизменённая проба в круг не попадает."""
    with stand() as st:
        st.put_mark()
        st.touch("scripts/test_changed.py")
        _, names, _ = st.circle()
        check("тихая вне круга", "тихая" not in names, f"в круге {sorted(names)}")
        check("круг меньше ворот", 0 < len(names) < len(st.all_names()),
              f"{len(names)} из {len(st.all_names())}")


def mechanism_changed_enters() -> None:
    """Изменился механизм, проба нет: проба попала по реестру."""
    with stand() as st:
        st.put_mark()
        st.touch("harness/demons/watched.sh")
        expect_changed(st, "механизм реестра", {"через реестр"})
    with stand() as st:
        st.put_mark()
        st.touch("harness/demons/silent.sh")
        expect_changed(st, "механизм без поля «проверяется»", set())


def closure_file_enters() -> None:
    """Изменился файл из замыкания упоминаний: проба попала."""
    for label, path, expected in (
            ("путь в тексте пробы", "scripts/tool_one.sh", "замыкание"),
            ("голое имя файла", "harness/config/settings_one.yaml", "замыкание"),
            ("второй шаг и цикл упоминаний", "scripts/deep_two.py", "замыкание"),
            ("import модуля scripts/lib", "scripts/lib/helper_mod.py", "импорт")):
        with stand() as st:
            st.put_mark()
            st.touch(path)
            expect_changed(st, label, {expected})


def new_gates_line_enters() -> None:
    """Новая или изменённая строка gates.sh попала; остальные от этого не попали."""
    with stand() as st:
        st.put_mark()
        with open(st.gates, "a", encoding="utf-8") as out:
            out.write('проверка "новичок" python3 scripts/test_quiet.py --новый\n')
        expect_changed(st, "новая строка", {"новичок"})
    with stand() as st:
        st.put_mark()
        text = st.gates.read_text(encoding="utf-8")
        write(st.gates, text.replace("scripts/test_quiet.py", "scripts/test_quiet.py --ещё"))
        expect_changed(st, "изменённая строка", {"тихая"})
    with stand() as st:
        st.put_mark()
        st.touch("scripts/gates.sh")
        expect_changed(st, "правка gates.sh мимо строк проверок", set())


def week_passed_full() -> None:
    """Метке неделя и больше: круг целиком."""
    for label, age in (("ровно на срок", FULL_DAYS), ("срок прошёл", FULL_DAYS + 1)):
        with stand() as st:
            st.put_mark(age_days=age)
            expect_full(st, label)


def day_before_deadline_changed() -> None:
    """За сутки и за секунду до срока — ещё изменённые."""
    for label, age in (("за сутки до срока", FULL_DAYS - 1),
                       ("за секунду до срока", FULL_DAYS - 1 / DAY)):
        with stand() as st:
            st.put_mark(age_days=age)
            st.touch("scripts/test_changed.py")
            expect_changed(st, label, {"изменяемая"})


def no_mark_full() -> None:
    """Базы нет: круг целиком."""
    with stand() as st:
        expect_full(st, "нет метки")


def broken_mark_full() -> None:
    """Битая метка: круг целиком, а не падение."""
    with stand() as st:
        for label, text in (
                ("пустой файл", ""),
                ("не JSON", "не json{"),
                ("список вместо записи", "[]"),
                ("нет ts", json.dumps({"sha": st.head})),
                ("нет sha", json.dumps({"ts": NOW})),
                ("sha не строка", json.dumps({"sha": 123, "ts": NOW})),
                ("ts не число", json.dumps({"sha": st.head, "ts": "вчера"}))):
            write(st.mark, text)
            expect_full(st, label)


def unknown_sha_full() -> None:
    """sha метки нет в репозитории: круг целиком."""
    with stand() as st:
        st.put_mark(sha="0123456789" * 4)
        expect_full(st, "неизвестный sha")


def git_failure_full() -> None:
    """git сбоит: круг целиком, изменённое считать не по чему."""
    with stand() as st:
        st.put_mark()
        (st.root / ".git").rename(st.base / "git-off")
        expect_full(st, "нет репозитория")


def instrument_changed_full() -> None:
    """Изменился сам прибор или его данные: круг целиком."""
    for path in (INSTRUMENT, INSTRUMENT_DATA):
        with stand() as st:
            st.put_mark()
            st.touch(path)
            expect_full(st, f"{path} в рабочем дереве")
        with stand() as st:
            st.put_mark()
            st.touch(path)
            st.commit("правка прибора")
            expect_full(st, f"{path} в коммите")


def nothing_changed_empty() -> None:
    """Ничего не изменилось: пустой круг, код 0, вслух «изменённых нет»."""
    with stand() as st:
        st.put_mark()
        expect_changed(st, "чистое дерево", set())
        before = st.mark.read_bytes()
        done = st.run()
        check("код 0", done.returncode == 0, f"код {done.returncode}: {done.stdout[-200:]}")
        check(f"вслух «{LOUD_EMPTY}»", LOUD_EMPTY in done.stdout, done.stdout[-200:])
        check("ни одна проверка не гонялась", not st.ran(), str(st.ran()))
        check("метка на месте", st.mark.read_bytes() == before)


def write_mark_shape() -> None:
    """write_mark пишет HEAD и now, и эта метка читается как база."""
    with stand() as st:
        INSTRUMENT_MODULE.write_mark(st.mark, st.root, NOW - DAY)
        mark = json.loads(st.mark.read_text(encoding="utf-8"))
        check("sha — HEAD", mark.get("sha") == st.head, str(mark))
        check("ts — now", mark.get("ts") == NOW - DAY, str(mark))
        st.touch("scripts/test_changed.py")
        expect_changed(st, "база из write_mark", {"изменяемая"})


def red_full_keeps_no_mark() -> None:
    """Красный полный круг метку не пишет и старую не двигает."""
    with stand(extra_checks=(BOUND_CHECK,)) as st:
        done = st.run()
        check("красный круг: код 1", done.returncode == 1,
              f"код {done.returncode}: {done.stdout[-200:]}")
        check("красный круг метки не написал", not st.mark.exists())
        st.put_mark(age_days=FULL_DAYS + 1)
        before = st.mark.read_bytes()
        done = st.run()
        check("красный круг по сроку: код 1", done.returncode == 1, f"код {done.returncode}")
        check("старая метка не сдвинута", st.mark.read_bytes() == before)
        # Парой: то же дерево без находки метку пишет. Иначе молчание выше —
        # заслуга прибора, который не пишет меток вообще.
        write(st.gates, re.sub(r'^проверка "зависимая".*\n', "",
                               st.gates.read_text(encoding="utf-8"), flags=re.M))
        st.commit("находки больше нет")
        st.mark.unlink()
        st.run()
        check("тот же стенд без находки метку написал", st.mark.exists())


def clean_full_writes_mark() -> None:
    """Чистый полный круг пишет метку: HEAD и текущее время."""
    with stand() as st:
        started = time.time()
        done = st.run()
        check("код 0", done.returncode == 0, f"код {done.returncode}: {done.stdout[-200:]}")
        check("метка есть", st.mark.exists())
        mark = json.loads(st.mark.read_text(encoding="utf-8"))
        check("sha — HEAD", mark.get("sha") == st.head, str(mark))
        ts = mark.get("ts")
        check("ts — время прогона", isinstance(ts, (int, float))
              and started - 1 <= ts <= time.time() + 1, str(mark))
        ran = st.ran()
        expected = {"изменяемая", "тихая", "через реестр", "замыкание", "импорт", "оболочка"}
        check("все лёгкие прогнаны трижды", all(ran[name] == 3 for name in expected), str(ran))
        check("тяжёлая не гонялась", ran["тяжёлая"] == 0, str(ran))


def default_mark_in_log_dir() -> None:
    """Метка по умолчанию лежит в LOG_DIR стенда."""
    with stand() as st:
        env = st.env()
        del env["HOST_INDEPENDENCE_MARK"]
        st.run(env=env)
        check(f"$LOG_DIR/{MARK_NAME} написана", (st.log_dir / MARK_NAME).exists(),
              str(os.listdir(st.log_dir)))


def partial_keeps_mark() -> None:
    """Частичный круг гоняет только своё и базу не двигает."""
    with stand() as st:
        st.put_mark()
        before = st.mark.read_bytes()
        st.touch("scripts/test_changed.py")
        st.touch("scripts/test_heavy.py")
        done = st.run()
        check("код 0", done.returncode == 0, f"код {done.returncode}: {done.stdout[-300:]}")
        check("метка на месте", st.mark.read_bytes() == before)
        ran = st.ran()
        check("изменённая прогнана трижды", ran["изменяемая"] == 3, str(ran))
        check("тихая не гонялась", ran["тихая"] == 0, str(ran))
        check("тяжёлая не гонялась и в частичном круге", ran["тяжёлая"] == 0, str(ran))
    # Тяжёлая вне круга: сверка данных прибора судит ворота, а не круг.
    with stand() as st:
        st.put_mark()
        st.touch("scripts/test_changed.py")
        done = st.run()
        check("запись о проверке вне круга не устарела",
              done.returncode == 0 and "устарела" not in done.stdout, done.stdout[-300:])


def partial_red_keeps_mark() -> None:
    """Красный частичный круг: код 1, находка есть, база стоит."""
    with stand(extra_checks=(BOUND_CHECK,)) as st:
        st.put_mark()
        before = st.mark.read_bytes()
        st.touch("scripts/test_bound.py")
        done = st.run()
        check("код 1", done.returncode == 1, f"код {done.returncode}: {done.stdout[-300:]}")
        check("находка названа", "зависимая" in done.stdout, done.stdout[-300:])
        check("гонялась только она", set(st.ran()) == {"зависимая"}, str(st.ran()))
        check("метка на месте", st.mark.read_bytes() == before)


def full_days_key() -> None:
    """Срок берётся из HOST_INDEPENDENCE_FULL_DAYS, без ключа — 7 суток."""
    age = 3
    with stand(conf="HOST_INDEPENDENCE_FULL_DAYS=2\n") as st:
        st.put_mark(age_days=age)
        st.run()
        check("ключ 2 при метке 3 суток: круг целиком", st.ran()["тихая"] == 3, str(st.ran()))
    with stand() as st:
        st.put_mark(age_days=age)
        done = st.run()
        check("без ключа метка 3 суток свежа", not st.ran(), str(st.ran()))
        check(f"без ключа вслух «{LOUD_EMPTY}»", LOUD_EMPTY in done.stdout, done.stdout[-200:])


class GatesStand:
    """Копия настоящего gates.sh; прибор в ней — заглушка, отмечающая запуск."""

    def __init__(self, conf: str) -> None:
        self.base = stand_dir()
        self.root = self.base / "repo"
        self.home = self.base / "home"
        self.trace = self.base / "trace"
        write(self.root / "scripts/gates.sh", GATES_SH.read_text(encoding="utf-8"))
        copy_lib(self.root)
        write(self.root / INSTRUMENT, trace_writer(self.trace, "заглушка"))
        write(self.home / "install.conf",
              f'PROJECT_DIR="{self.root}"\nLOG_DIR="{self.base}/log"\n')
        write(self.home / "harness.conf", conf)
        (self.base / "log").mkdir()

    def run(self) -> tuple[int, str, bool]:
        """Код, вывод без служебной строки выборки, запускался ли прибор."""
        done = subprocess.run(
            ["bash", "scripts/gates.sh", "--только", GATE_LINE, "--всё"], cwd=self.root,
            capture_output=True, text=True, timeout=RUN_LIMIT_SEC, stdin=subprocess.DEVNULL,
            env={**os.environ, "HARNESS_HOME": str(self.home),
                 "LOG_DIR": str(self.base / "log")})
        # Выборка сама печатает своё имя: это не строка проверки.
        output = "\n".join(line for line in (done.stdout + done.stderr).splitlines()
                           if not line.startswith("--- выборка"))
        return done.returncode, output, self.trace.exists()

    def close(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)


def judge_copy_off(label: str, conf: str) -> None:
    gates = GatesStand(conf)
    try:
        code, output, instrument_ran = gates.run()
        check(f"{label}: прибор не запускался", not instrument_ran, output[-300:])
        check(f"{label}: строки «{GATE_LINE}» нет", GATE_LINE not in output, output[-300:])
        check(f"{label}: жалоб нет", not re.search(
            r"ПЛОХО|unbound|not found|No such file|синтакс", output), output[-300:])
        check(f"{label}: код 0", code == 0, f"код {code}")
    finally:
        gates.close()


def copy_off_key_zero() -> None:
    """Копия с ключом 0: проверка не идёт и молчит."""
    judge_copy_off("ключ 0", "HOST_INDEPENDENCE_CHECK=0\n")


def copy_off_key_missing() -> None:
    """Копия без ключа: как 0, молча — set -u ворот не должен упасть."""
    judge_copy_off("нет ключа", "")


def host_on_key_one() -> None:
    """Ключ 1: тот же стенд проверку ведёт. Иначе случаи выше зелены от немоты стенда."""
    gates = GatesStand("HOST_INDEPENDENCE_CHECK=1\n")
    try:
        code, output, instrument_ran = gates.run()
        check("прибор запущен", instrument_ran, output[-300:])
        check(f"строка «{GATE_LINE}» в выводе", GATE_LINE in output, output[-300:])
        check("код 0", code == 0, f"код {code}: {output[-300:]}")
    finally:
        gates.close()


def gates_line_stays_parsed() -> None:
    """Строка прибора в настоящих воротах разбирается прибором.

    Запись «ключ && проверка …» в одну строку выпала бы из разбора, и данные прибора
    разошлись бы с воротами незаметно для глаз.
    """
    names = [name for name, _ in INSTRUMENT_MODULE.проверки(GATES_SH)]
    check("настоящие ворота разобраны", len(names) > 100, f"проверок {len(names)}")
    check(f"«{GATE_LINE}» среди проверок", GATE_LINE in names)


def sample_conf_keys() -> None:
    """Образец harness.conf: у копии проверка выключена, срок записан."""
    from config import из_файла
    values = из_файла(SAMPLE_CONF)
    check("HOST_INDEPENDENCE_CHECK=0 в образце", values.get("HOST_INDEPENDENCE_CHECK") == "0")
    check("HOST_INDEPENDENCE_FULL_DAYS=7 в образце", values.get("HOST_INDEPENDENCE_FULL_DAYS") == "7")


CASES = (
    changed_probe_enters, unchanged_probe_stays_out, mechanism_changed_enters,
    closure_file_enters, new_gates_line_enters, week_passed_full,
    day_before_deadline_changed, no_mark_full, broken_mark_full, unknown_sha_full,
    git_failure_full, instrument_changed_full, nothing_changed_empty, write_mark_shape,
    red_full_keeps_no_mark, clean_full_writes_mark, default_mark_in_log_dir,
    partial_keeps_mark, partial_red_keeps_mark, full_days_key, copy_off_key_zero,
    copy_off_key_missing, host_on_key_one, gates_line_stays_parsed, sample_conf_keys,
)


def on_case_limit(*_) -> None:
    raise TimeoutError(f"случай не уложился в {CASE_LIMIT_SEC} с")


def main(argv: list[str]) -> int:
    only = argv[argv.index("--случай") + 1] if "--случай" in argv else None
    chosen = [fn for fn in CASES if only in (None, fn.__name__)]
    if not chosen:
        print(f"нет случая {only}; есть: {', '.join(fn.__name__ for fn in CASES)}")
        return 2
    print(f"прибор: {BIN}")
    signal.signal(signal.SIGALRM, on_case_limit)
    for fn in chosen:
        print(f"[{fn.__name__}] {(fn.__doc__ or '').strip().splitlines()[0]}", flush=True)
        signal.alarm(CASE_LIMIT_SEC)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — случай краснеет с причиной, проба идёт дальше
            check(f"{fn.__name__}: дошёл до конца", False, f"{type(exc).__name__}: {exc}")
        signal.alarm(0)
    bad = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"ХОЗЯИН-КРУГ: случаев {len(chosen)}, путей {len(RESULTS)}, неудач {bad}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
