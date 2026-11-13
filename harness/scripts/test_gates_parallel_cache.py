#!/usr/bin/env python3
"""Проба: ворота гонят проверки пулом, помнят зелёный вердикт по хешу входов и
не гонят второй одновременный прогон того же дерева (карточка hr.parallel-cached-checks).

    python3 scripts/test_gates_parallel_cache.py

Вход — обёртка проверки scripts/lib/gates_check.sh на стенде из фиктивных проверок;
полные ворота не запускаются. Каждый случай строит свой каталог: стенд (git-репозиторий
со своими проверками, своим mechanisms.yaml и своим списком последовательных), свои
LOG_DIR и GATES_TIMING_DIR. Боевые журналы не трогаются.

Что проверка ЗАПУСКАЛАСЬ, судит метка-файл: каждая фиктивная проверка дописывает
строку в $MARKS/<имя>. Вердикт «из кэша» без метки — единственное доказательство,
что команда не шла; строка вывода одна такого не докажет.

Контракт, который судится (спека .tasks/hr.parallel-cached-checks/спека.md, § Контракт):
  проверка <имя> <команда…>  — прежняя подпись; при GATES_JOBS>1 уходит в пул;
  gates_wait_all             — барьер: ждёт пул, допечатывает вывод по порядку, счётчики верны;
  gates_join_or_lead <арг…>  — замок прогона по состоянию дерева; второй присоединяется;
  GATES_JOBS, GATES_NO_CACHE, GATES_CACHE_FILE, GATES_MECHANISMS, GATES_SERIAL_FILE,
  GATES_ROOT, GATES_JOIN_DIR, GATES_LEAD_PID;
  $LOG_DIR/gates-cache.tsv: ключ<TAB>секунды-эпохи<TAB>имя<TAB>код<TAB>последняя строка.

Параллельность судится по интервалам работы из меток, а не по стене прогона: внутри
настоящих ворот проба идёт рядом с пятью тяжёлыми проверками, и порог стены там краснел
бы от нагрузки машины, а не от поломки пула.

Дверь для мутаций: GATES_CHECK_LIB — путь обёртки (мутировать КОПИЮ; помощник кэша
берётся рядом с ней, из ../gates_cache.py).
Код возврата: 0 — все случаи зелёные, 1 — есть красный.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = os.environ.get("GATES_CHECK_LIB") or str(ROOT / "scripts/lib/gates_check.sh")

# Стенд: обёртка, по желанию замок прогона, проверки из checks.sh, барьер, счёт.
# Барьер зовётся всегда: без него счётчики пула не сведены, и случай судил бы
# недосчитанное. Нет функции — bash печатает «command not found» и идёт дальше:
# проба тогда краснеет на своих случаях, а не на синтаксисе стенда.
STAND = r"""
set -uo pipefail
export LC_ALL=C.UTF-8
. "$1"
if [ "${STAND_JOIN:-0}" = 1 ]; then
    gates_join_or_lead stand-args
    printf 'ПУЛ %s\n' "${GATES_JOBS:-нет}"
fi
. ./checks.sh
gates_wait_all
printf 'СЧЁТ %s %s %s\n' "$GREEN" "$RED" "$YELLOW"
exit "$RED"
"""

# Фиктивная проверка: метка запуска, интервал работы, сон, код. Имя метки —
# первый довод, а не имя файла: одним файлом ходят разные проверки.
RUN_SH = r"""#!/usr/bin/env bash
name="$1"; secs="${2:-0}"; rc="${3:-0}"
start=$(date +%s.%N)
echo run >> "$MARKS/$name"
[ "$secs" = 0 ] || sleep "$secs"
echo "вывод-1 $name"
echo "вывод-2 $name"
printf '%s %s\n' "$start" "$(date +%s.%N)" >> "$MARKS/$name.t"
exit "$rc"
"""

# Свой файл на каждую кэшируемую проверку: механизм сопоставляется по пути
# скрипта в команде, и у каждого — свои входы.
CACHED_SH = r"""#!/usr/bin/env bash
echo run >> "$MARKS/$(basename "$0" .sh)"
echo "последняя $(basename "$0" .sh)"
exit "${RC_OVERRIDE:-__RC__}"
"""

# Одним скриптом ходят проверки с разными доводами: метка — по доводу.
ARGS_SH = r"""#!/usr/bin/env bash
echo run >> "$MARKS/args-$1"
echo "доводы $1"
"""

MECHANISMS = """механизмы:
- файл: chk/green.sh
  имя: зелёная с входами
  входы:
  - data/in.txt
- файл: chk/red.sh
  имя: красная с входами
  входы: [data/in.txt]
- файл: chk/yellow.sh
  имя: жёлтая с входами
  входы: [data/in.txt]
- файл: chk/flaky.sh
  имя: зелёная, которая краснеет
  входы: [data/in.txt]
- файл: chk/noinputs.sh
  имя: зелёная без входов
- файл: chk/missing.sh
  имя: зелёная с несуществующим входом
  входы: [nope/*.txt]
- файл: chk/run.sh
  имя: общая фиктивная
- файл: chk/args.sh
  имя: зелёная с доводами
  входы: [data/in.txt]
"""

SERIAL = """группы:
  стенд-общий:
    почему: проба — две проверки делят один ресурс стенда
    проверки: [общая-1, общая-2]
  одна:
    почему: проба — проверка идёт одна, пул ждёт
    проверки: [одиночка]
"""

failed: list[str] = []
passed: list[str] = []


def case(name: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(name)
    print(f"  {'ок' if ok else 'ПЛОХО'} · {name}" + ("" if ok else f" — {detail}"))


class Stand:
    """Свой каталог на случай: репозиторий стенда, метки, журналы."""

    def __init__(self, tmp: Path, checks: str):
        self.dir = tmp / "stand"
        self.marks = tmp / "marks"
        self.log = tmp / "log"
        self.timing = tmp / "timing"
        for d in (self.dir / "chk", self.dir / "data", self.marks, self.log, self.timing):
            d.mkdir(parents=True)
        (self.dir / "chk/run.sh").write_text(RUN_SH, encoding="utf-8")
        for name, rc in (("green", 0), ("red", 3), ("yellow", 77), ("flaky", 0),
                         ("noinputs", 0), ("missing", 0)):
            (self.dir / f"chk/{name}.sh").write_text(CACHED_SH.replace("__RC__", str(rc)),
                                                     encoding="utf-8")
        (self.dir / "chk/args.sh").write_text(ARGS_SH, encoding="utf-8")
        (self.dir / "data/in.txt").write_text("вход 1\n", encoding="utf-8")
        (self.dir / "mechanisms.yaml").write_text(MECHANISMS, encoding="utf-8")
        (self.dir / "serial.yaml").write_text(SERIAL, encoding="utf-8")
        (self.dir / "checks.sh").write_text(checks, encoding="utf-8")
        (self.dir / "stand.sh").write_text(STAND, encoding="utf-8")
        git = ["git", "-c", "user.name=проба", "-c", "user.email=probe@localhost"]
        subprocess.run(["git", "init", "-q", str(self.dir)], check=True)
        subprocess.run(git + ["-C", str(self.dir), "add", "-A"], check=True)
        subprocess.run(git + ["-C", str(self.dir), "commit", "-qm", "стенд"], check=True)

    def env(self, **extra: str) -> dict:
        # Унаследованные ключи ворот и журнала вычищаются: прогон пробы из-под
        # настоящих ворот иначе взял бы их пул, кэш и признак вложенного прогона.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GATES_", "HARNESS_LOG_DIR", "GIT_"))
               and k not in ("LOG_DIR", "ONLY_CHECKS")}
        env.update(LOG_DIR=str(self.log), GATES_TIMING_DIR=str(self.timing),
                   MARKS=str(self.marks), GATES_ROOT=str(self.dir),
                   GATES_MECHANISMS=str(self.dir / "mechanisms.yaml"),
                   GATES_SERIAL_FILE=str(self.dir / "serial.yaml"),
                   GATES_JOIN_DIR=str(self.log), LC_ALL="C.UTF-8")
        env.update(extra)
        return env

    def popen(self, **extra: str) -> subprocess.Popen:
        return subprocess.Popen(["bash", str(self.dir / "stand.sh"), LIB], cwd=self.dir,
                                env=self.env(**extra), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)

    def run(self, **extra: str) -> tuple[str, int, float]:
        started = time.monotonic()
        proc = self.popen(**extra)
        try:
            out, _ = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            out += "\n[проба: стенд не уложился в 60 с]"
        return out, proc.returncode, time.monotonic() - started

    def runs(self, name: str) -> int:
        p = self.marks / name
        return len(p.read_text(encoding="utf-8").splitlines()) if p.is_file() else 0

    def intervals(self, name: str) -> list[tuple[float, float]]:
        p = self.marks / f"{name}.t"
        if not p.is_file():
            return []
        return [tuple(map(float, line.split())) for line in p.read_text().splitlines() if line]

    def cache_rows(self) -> list[list[str]]:
        p = self.log / "gates-cache.tsv"
        if not p.is_file():
            return []
        return [line.split("\t") for line in p.read_text(encoding="utf-8").splitlines() if line]


def check_lines(out: str) -> list[str]:
    """Имена проверок в порядке печати — разбором строк вердикта."""
    return re.findall(r"^  (?:ок|ПЛОХО|жёлтый) · (.+?)(?: — | \(|$)", out, flags=re.M)


def tail(out: str) -> str:
    return out[-600:].replace("\n", " ⏎ ")


def overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def max_concurrent(intervals: list[tuple[float, float]]) -> int:
    """Наибольшее число работ, шедших одновременно: обход концов интервалов."""
    edges = sorted([(s, 1) for s, _ in intervals] + [(e, -1) for _, e in intervals])
    now = peak = 0
    for _, step in edges:
        now += step
        peak = max(peak, now)
    return peak


def stand_processes(marks: Path) -> list[int]:
    """Живые процессы стенда: все потомки стенда наследуют MARKS=<его каталог меток>."""
    needle = f"MARKS={marks}".encode()
    found = []
    for env in Path("/proc").glob("[0-9]*/environ"):
        try:
            if needle in env.read_bytes().split(b"\0"):
                found.append(int(env.parent.name))
        except OSError:
            continue
    return found


def with_stand(checks: str, body) -> None:
    with tempfile.TemporaryDirectory(prefix="gates-pcc-") as tmp:
        body(Stand(Path(tmp), checks))


# ── (а)(б)(в) пул, порядок, счётчики ─────────────────────────────────────────

def case_parallel_wall(st: Stand) -> None:
    out, _, _ = st.run(GATES_JOBS="6")
    names = check_lines(out)
    ivs = [iv for i in range(1, 7) for iv in st.intervals(f"п{i}")]
    case("(а) 6 проверок по 1 с при пуле 6 идут все шесть одновременно",
         len(ivs) == 6 and max_concurrent(ivs) == 6 and len(names) == 6,
         f"интервалов {len(ivs)}, одновременно {max_concurrent(ivs)}, в выводе {len(names)}; {tail(out)}")


PARALLEL_SIX = "".join(f'проверка "п{i}" bash chk/run.sh п{i} 1\n' for i in range(1, 7))


def case_order_and_counts(st: Stand) -> None:
    out, rc, _ = st.run(GATES_JOBS="6")
    names = check_lines(out)
    want = [f"п{i}" for i in range(1, 7)]
    # Первая объявленная кончается последней: порядок завершения обратный объявленному.
    ends = {n: iv[1] for n in want for iv in st.intervals(n)}
    finished = sorted(ends, key=ends.get)
    case("(б) вывод в порядке объявления, хотя кончились они в обратном",
         names == want and finished == want[::-1], f"порядок {names}, кончились {finished}")
    block = re.search(r"^  ПЛОХО · п3 \(код 3\)\n(.*\n){0,2}", out, flags=re.M)
    case("(б) вывод красной идёт целым блоком под её строкой",
         bool(block) and "вывод-1 п3" in block.group(0) and "вывод-2 п3" in block.group(0),
         tail(out))
    case("(в) после барьера счёт верен, код 77 — жёлтый: СЧЁТ 4 1 1, выход 1",
         "СЧЁТ 4 1 1" in out and rc == 1, f"код {rc}; {tail(out)}")


POOL_LIMIT = "".join(f'проверка "л{i}" bash chk/run.sh л{i} 0.5\n' for i in range(1, 5))


def case_pool_limit(st: Stand) -> None:
    out, _, _ = st.run(GATES_JOBS="2")
    ivs = [iv for i in range(1, 5) for iv in st.intervals(f"л{i}")]
    case("пул 2: одновременно идут ровно две проверки, не больше",
         len(ivs) == 4 and max_concurrent(ivs) == 2,
         f"интервалов {len(ivs)}, одновременно {max_concurrent(ivs)}; {tail(out)}")


ORDER_SIX = (
    'проверка "п1" bash chk/run.sh п1 1.5\n'
    'проверка "п2" bash chk/run.sh п2 1.2\n'
    'проверка "п3" bash chk/run.sh п3 0.9 3\n'
    'проверка "п4" bash chk/run.sh п4 0.6\n'
    'проверка "п5" bash chk/run.sh п5 0.3 77\n'
    'проверка "п6" bash chk/run.sh п6 0\n'
)


def case_default_is_sequential(st: Stand) -> None:
    # Без GATES_JOBS и без замка обёртка ведёт себя как прежде: счётчики
    # растут сразу. На это опираются самотест ворот и пробы-стенды других
    # механизмов (test_gates_timing.py читает счёт без барьера).
    stand = (". \"$1\"\n"
             "проверка \"с1\" bash chk/run.sh с1 0\n"
             "printf 'СРАЗУ %s\\n' \"$GREEN\"\n")
    (st.dir / "sync.sh").write_text(stand, encoding="utf-8")
    proc = subprocess.run(["bash", str(st.dir / "sync.sh"), LIB], cwd=st.dir, env=st.env(),
                          capture_output=True, text=True, timeout=60)
    case("(в) без пула счётчик растёт сразу после вызова (прежнее поведение)",
         "СРАЗУ 1" in proc.stdout, tail(proc.stdout + proc.stderr))


# ── (г)(д)(е)(ж) кэш ────────────────────────────────────────────────────────

CACHE_CHECKS = (
    'проверка "кэшируемая" bash chk/green.sh\n'
    'проверка "красная" bash chk/red.sh\n'
    'проверка "жёлтая" bash chk/yellow.sh\n'
    'проверка "без входов" bash chk/noinputs.sh\n'
    "проверка \"без механизма\" bash -c 'echo run >> \"$MARKS/без-механизма\"; echo ок'\n"
    'проверка "вход потерян" bash chk/missing.sh\n'
)


def line_of(out: str, name: str) -> str:
    m = re.search(rf"^  (?:ок|ПЛОХО|жёлтый) · {re.escape(name)}(?: — | \(|$).*$", out, flags=re.M)
    return m.group(0) if m else ""


def case_cache(st: Stand) -> None:
    out1, _, _ = st.run()
    case("(г) первый прогон: зелёная с входами запускалась", st.runs("green") == 1,
         f"запусков {st.runs('green')}; {tail(out1)}")
    rows = [r for r in st.cache_rows() if len(r) >= 3 and r[2] == "кэшируемая"]
    case("(г) вердикт зелёной лёг строкой gates-cache.tsv: ключ sha256, секунды, имя, код 0, последняя строка",
         len(rows) == 1 and len(rows[0]) == 5 and re.fullmatch(r"[0-9a-f]{64}", rows[0][0]) is not None
         and rows[0][1].isdigit() and rows[0][3] == "0" and rows[0][4] == "последняя green",
         f"строки tsv: {st.cache_rows()}")

    out2, _, _ = st.run()
    case("(г) повтор с теми же входами: вердикт из кэша, команда не запускалась",
         st.runs("green") == 1 and "из кэша" in line_of(out2, "кэшируемая")
         and "СЧЁТ" in out2 and re.search(r"СЧЁТ [1-9]", out2) is not None,
         f"запусков {st.runs('green')}, строка «{line_of(out2, 'кэшируемая')}»")
    case("(г) строка из кэша сохраняет вид «  ок · имя — последняя строка» (его разбирает nightly-gates.sh)",
         line_of(out2, "кэшируемая").startswith("  ок · кэшируемая — последняя green")
         and "из кэша" in line_of(out2, "кэшируемая"),
         f"строка «{line_of(out2, 'кэшируемая')}»")
    case("(д) красная не кэшируется: запускалась оба раза", st.runs("red") == 2,
         f"запусков {st.runs('red')}")
    case("(д) в tsv нет строки с кодом 0 для красной",
         not [r for r in st.cache_rows() if len(r) >= 4 and r[2] == "красная" and r[3] == "0"],
         f"строки tsv: {st.cache_rows()}")
    case("(д) жёлтая (код 77) не кэшируется: запускалась оба раза", st.runs("yellow") == 2,
         f"запусков {st.runs('yellow')}")
    case("(е) механизм без «входы:» не кэшируется: запускалась оба раза", st.runs("noinputs") == 2,
         f"запусков {st.runs('noinputs')}")
    case("(е) и печатает это: «входы не описаны» в её строке",
         "входы не описаны" in line_of(out2, "без входов"), f"строка «{line_of(out2, 'без входов')}»")
    case("(е) команда без механизма не кэшируется и печатает «входы не описаны»",
         st.runs("без-механизма") == 2 and "входы не описаны" in line_of(out2, "без механизма"),
         f"запусков {st.runs('без-механизма')}, строка «{line_of(out2, 'без механизма')}»")
    case("(е) вход-шаблон без единого файла: не кэшируется, «вход не найден» в строке",
         st.runs("missing") == 2 and "вход не найден" in line_of(out2, "вход потерян"),
         f"запусков {st.runs('missing')}, строка «{line_of(out2, 'вход потерян')}»")

    (st.dir / "data/in.txt").write_text("вход 2\n", encoding="utf-8")
    st.run()
    case("(г) правка входа — проверка запускается снова", st.runs("green") == 2,
         f"запусков {st.runs('green')}")
    with (st.dir / "chk/green.sh").open("a", encoding="utf-8") as f:
        f.write("# правка самого скрипта проверки\n")
    st.run()
    case("(г) правка самого скрипта проверки (он вход всегда) — запускается снова",
         st.runs("green") == 3, f"запусков {st.runs('green')}")

    out5, _, _ = st.run(GATES_NO_CACHE="1")
    case("(ж) GATES_NO_CACHE=1 гонит и закэшированную",
         st.runs("green") == 4 and "из кэша" not in line_of(out5, "кэшируемая"),
         f"запусков {st.runs('green')}, строка «{line_of(out5, 'кэшируемая')}»")
    st.run()
    case("(ж) прогон без кэша сам пишет свежий зелёный: следующий обычный берёт из кэша",
         st.runs("green") == 4, f"запусков {st.runs('green')}")

    (st.dir / "checks.sh").write_text('проверка "доводы" bash chk/args.sh а\n', encoding="utf-8")
    st.run()
    (st.dir / "checks.sh").write_text('проверка "доводы" bash chk/args.sh б\n', encoding="utf-8")
    out6, _, _ = st.run()
    case("(г) та же проверка с другими доводами — другой ключ: запускается, не из кэша",
         st.runs("args-б") == 1 and "из кэша" not in line_of(out6, "доводы"),
         f"запусков с «б» {st.runs('args-б')}, строка «{line_of(out6, 'доводы')}»")

    timing = st.timing / "gates-timing.jsonl"
    text = timing.read_text(encoding="utf-8") if timing.is_file() else ""
    case("журнал секунд: строка из кэша помечена \"cached\": true",
         re.search(r'"name": "кэшируемая".*"cached": true', text) is not None,
         f"строк журнала {text.count(chr(10))}")


def case_tombstone(st: Stand) -> None:
    st.run()
    st.run()
    hits_before = st.runs("flaky")
    st.run(GATES_NO_CACHE="1", RC_OVERRIDE="3")
    st.run()
    case("(д) красный на тех же входах гасит прежний зелёный: следующий прогон гонит заново",
         hits_before == 1 and st.runs("flaky") == 3,
         f"запусков до {hits_before}, после {st.runs('flaky')}")


TOMB_CHECKS = 'проверка "мерцающая" bash chk/flaky.sh\n'


# ── последовательные проверки — данными ────────────────────────────────────

SERIAL_CHECKS = (
    'проверка "общая-1" bash chk/run.sh общая-1 1\n'
    'проверка "свободная-1" bash chk/run.sh свободная-1 1\n'
    'проверка "общая-2" bash chk/run.sh общая-2 1\n'
    'проверка "свободная-2" bash chk/run.sh свободная-2 1\n'
)


def case_serial_group(st: Stand) -> None:
    out, _, _ = st.run(GATES_JOBS="4")
    a, b = st.intervals("общая-1"), st.intervals("общая-2")
    free = st.intervals("свободная-1")
    case("группа из списка последовательных: её проверки не пересекаются",
         len(a) == 1 and len(b) == 1 and not overlap(a[0], b[0]), f"интервалы {a} {b}; {tail(out)}")
    case("группа не останавливает пул: свободная шла рядом с общей",
         len(a) == 1 and len(free) == 1 and overlap(a[0], free[0]), f"интервалы {a} {free}")
    case("вывод группы — тоже в порядке объявления",
         check_lines(out) == ["общая-1", "свободная-1", "общая-2", "свободная-2"],
         f"порядок {check_lines(out)}")


ALONE_CHECKS = (
    'проверка "до-1" bash chk/run.sh до-1 0.5\n'
    'проверка "до-2" bash chk/run.sh до-2 0.5\n'
    'проверка "одиночка" bash chk/run.sh одиночка 0.5\n'
    'проверка "после-1" bash chk/run.sh после-1 0.5\n'
    'проверка "после-2" bash chk/run.sh после-2 0.5\n'
)


def case_alone(st: Stand) -> None:
    out, _, _ = st.run(GATES_JOBS="4")
    alone = st.intervals("одиночка")
    others = [iv for n in ("до-1", "до-2", "после-1", "после-2") for iv in st.intervals(n)]
    case("группа «одна»: ни одна проверка не идёт одновременно с одиночкой",
         len(alone) == 1 and len(others) == 4 and not any(overlap(alone[0], o) for o in others),
         f"одиночка {alone}, прочие {others}; {tail(out)}")
    case("группа «одна»: прочие парами всё же шли параллельно",
         len(others) == 4 and overlap(others[0], others[1]) and overlap(others[2], others[3]),
         f"прочие {others}")


# ── (з) замок прогона ─────────────────────────────────────────────────────────

JOIN_CHECKS = 'проверка "медленная" bash chk/run.sh медленная 2\n'


def case_join(st: Stand) -> None:
    first = st.popen(STAND_JOIN="1")
    time.sleep(0.5)
    second = st.popen(STAND_JOIN="1")
    out1, _ = first.communicate(timeout=60)
    out2, _ = second.communicate(timeout=60)
    case("(з) второй одновременный прогон того же дерева присоединяется: команда шла один раз",
         st.runs("медленная") == 1, f"запусков {st.runs('медленная')}; 1: {tail(out1)} 2: {tail(out2)}")
    case("(з) присоединившийся печатает, к чему присоединился, и итог первого",
         "присоединяюсь к прогону" in out2 and "ок · медленная" in out2 and "СЧЁТ 1 0 0" in out2,
         tail(out2))
    case("(з) коды выхода обоих равны", first.returncode == second.returncode == 0,
         f"{first.returncode} и {second.returncode}")
    nproc = os.cpu_count() or 1
    case(f"(з) после замка без GATES_JOBS пул = ядра−2, минимум 1 (здесь {max(1, nproc - 2)})",
         f"ПУЛ {max(1, nproc - 2)}" in out1, tail(out1))


def case_join_other_tree(st: Stand) -> None:
    first = st.popen(STAND_JOIN="1")
    time.sleep(0.5)
    # Незакоммиченная правка: ворота судят рабочее дерево, а не HEAD.
    (st.dir / "data/новый.txt").write_text("правка после старта первого\n", encoding="utf-8")
    second = st.popen(STAND_JOIN="1")
    out1, _ = first.communicate(timeout=60)
    out2, _ = second.communicate(timeout=60)
    case("(з) другое состояние рабочего дерева при том же HEAD — не присоединяется",
         st.runs("медленная") == 2 and "присоединяюсь" not in out2,
         f"запусков {st.runs('медленная')}; {tail(out2)}")


def case_join_tracked_edit(st: Stand) -> None:
    first = st.popen(STAND_JOIN="1")
    time.sleep(0.5)
    # Правка ОТСЛЕЖИВАЕМОГО файла: HEAD и список неотслеживаемых те же, другим стал diff.
    (st.dir / "data/in.txt").write_text("правка отслеживаемого\n", encoding="utf-8")
    second = st.popen(STAND_JOIN="1")
    first.communicate(timeout=60)
    out2, _ = second.communicate(timeout=60)
    case("(з) правка отслеживаемого файла при том же HEAD — не присоединяется",
         st.runs("медленная") == 2 and "присоединяюсь" not in out2,
         f"запусков {st.runs('медленная')}; {tail(out2)}")


def case_join_nested(st: Stand) -> None:
    first = st.popen(STAND_JOIN="1")
    time.sleep(0.5)
    # Вложенный прогон (ворота внутри проверки ворот) наследует GATES_LEAD_PID:
    # присоединись он к внешнему — ждал бы сам себя.
    second = st.popen(STAND_JOIN="1", GATES_LEAD_PID=str(first.pid))
    out1, _ = first.communicate(timeout=60)
    out2, _ = second.communicate(timeout=60)
    case("(з) вложенный прогон (GATES_LEAD_PID) не присоединяется и гонит сам, пул 1",
         st.runs("медленная") == 2 and "присоединяюсь" not in out2 and "ПУЛ 1" in out2,
         f"запусков {st.runs('медленная')}; {tail(out2)}")


LEAK_CHECKS = "".join(f'проверка "долгая-{i}" bash chk/run.sh долгая-{i} 30\n' for i in range(1, 4))


def case_parent_killed(st: Stand) -> None:
    # Сигнал одному родителю (kill <pid> сторожем или вызывающим), а не всей группе,
    # как у timeout и Ctrl-C: работы пула ему не дети по группе и сами не умрут.
    proc = st.popen(STAND_JOIN="1", GATES_JOBS="3")
    deadline = time.monotonic() + 10
    while len(list(st.marks.glob("долгая-?"))) < 3 and time.monotonic() < deadline:
        time.sleep(0.1)
    proc.send_signal(signal.SIGTERM)
    started = time.monotonic()
    try:
        proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
    took = time.monotonic() - started
    time.sleep(0.5)
    left = stand_processes(st.marks)
    for pid in left:
        os.kill(pid, signal.SIGKILL)
    case("SIGTERM родителю: работы пула убраны вместе с ним, выход < 5 с",
         not left and took < 5, f"осталось процессов стенда {len(left)}, выход за {took:.1f} с")


def main() -> int:
    print(f"обёртка: {LIB}")
    scenarios = [
        (PARALLEL_SIX, case_parallel_wall),
        (ORDER_SIX, case_order_and_counts),
        (POOL_LIMIT, case_pool_limit),
        ("", case_default_is_sequential),
        (CACHE_CHECKS, case_cache),
        (TOMB_CHECKS, case_tombstone),
        (SERIAL_CHECKS, case_serial_group),
        (ALONE_CHECKS, case_alone),
        (JOIN_CHECKS, case_join),
        (JOIN_CHECKS, case_join_other_tree),
        (JOIN_CHECKS, case_join_tracked_edit),
        (JOIN_CHECKS, case_join_nested),
        (LEAK_CHECKS, case_parent_killed),
    ]
    for checks, body in scenarios:
        try:
            with_stand(checks, body)
        except Exception as exc:  # стенд упал — это красный случай, а не тихий пропуск
            case(f"стенд {body.__name__}", False, f"{type(exc).__name__}: {exc}")
    total = len(passed) + len(failed)
    if total == 0:
        print("ПЛОХО: ни одного случая не прогнано")
        return 1
    print(f"итог: зелёных {len(passed)}, красных {len(failed)} из {total}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
