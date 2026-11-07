#!/usr/bin/env python3
"""Пробы зависимостей задач прибора очереди (`work-queue.py`, Ч7 hr.speed-modes).

Флаг `--готовые` читают судья режимов скорости (блокер «мало независимых
задач») и оркестратор, когда раздаёт работу параллельно: карточка, чей
предшественник не доделан, выданная в работу, — это работа поверх недостроенного.

Контракт (spec.md hr.speed-modes, 2 «Зависимости задач», 9, 10.1 Ч7, 10.3 п.4):
  1. поле карточки `зависит_от: [id, ...]`; пусто/None/нет поля — как раньше;
     строка — один id; мусор другого типа (42, True, словарь, 5 и None в
     списке) не учитывается и не роняет прибор;
  2. зависимость выполнена, когда предшественник в `test`, `done` или
     `принято`; предшественник ищется по ВСЕЙ карте, а не по активной линии;
  3. карточка с невыполненной зависимостью не идёт в `порядок()`,
     `следующая()`, «работа» у `разобрать()` (там она в «ждут» с причиной),
     `--порядок`; прежние форматы вывода не меняются. Исключение — начатая
     (wip): начатое не бросают (память queue-by-efficiency-owner-order п.1),
     она остаётся везде и не в «ждут». test без провала и без свободного
     пункта не работа и не ждёт — зависимость этого не меняет;
  4. id не из карты зависимость не держит; `--готовые --битые` печатает
     «битая зависимость: <карточка> → <id>»;
  5. цикл не вешает прибор: его члены ждут, причина называет «цикл» и
     участников; `--готовые --ждут` печатает «ждёт: <карточка> ← <id> (<статус>)»
     и «цикл зависимостей: a → b → a»;
  6. `--готовые`: «готовых: N», затем id по строке в порядке `порядок()`, без
     потолка строк, только головные и не ждущие; код 0; пусто — «готовых: 0»;
  7. карта без поля даёт `порядок()` и `--порядок` побайтно как до правки
     (эталон снят 04.11.2026 со скрипта sha256 4da60bd2…), `--selftest` зелёный;
  8. `--готовые` — ровно те id, что «работа: N» у `разобрать()`: блокер и
     слово владельца отсеиваются одним судьёй;
  9. цепочка и цикл из 2000 карточек: `--порядок`, `--готовые --ждут`,
     `--следующая` — каждый быстрее SPEED_LIMIT секунд.

Стенд: копия скрипта и его `lib/` во временном каталоге (рядом нет queue.yaml —
веса умолчательные, а не боевые), свои LOG_DIR и конфиги, линия `h`. Каждый
вызов — отдельный процесс с пределом 20 с: цикл зависимостей, повесивший
прибор, обязан дать красный, а не повесить пробу. Скрипт берётся из WQ_SCRIPT
(дверь для копии под мутацией).

Прогон:
    python3 scripts/test_work_queue_dependencies.py
Последняя строка: «ЗАВИСИМОСТИ: случаев N, провалов M»; код 0 — все зелёные.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

SOURCE_SCRIPT = Path(os.environ.get("WQ_SCRIPT")
                     or Path(__file__).resolve().parent / "work-queue.py")
TIMEOUT = 20
# Предел с запасом: прибор зовут сторож и судья на каждом проходе, а карта
# растёт; квадрат по числу карточек на 2000 даёт десятки секунд.
SPEED_LIMIT = 3
SPEED_TIMEOUT = 10
LINE = "h"
DEPENDS = "зависит_от"

# Драйвер зовёт функции прибора в своём процессе: зависание в цикле зависимостей
# убивается таймаутом, а не вешает всю пробу. Имена функций — API прибора.
DRIVER = r'''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("work_queue", sys.argv[1])
wq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wq)
req = json.loads(open(sys.argv[2], encoding="utf-8").read())
data, line, fn = req["map"], req["line"], req["fn"]
settings = getattr(wq, "НАСТРОЙКИ_ПО_УМОЛЧАНИЮ")
order_fn, heads_fn = getattr(wq, "порядок"), getattr(wq, "головные")
if fn == "order":
    places = order_fn(data, line, settings, {}, {})
    out = [[p["id"], p["вес"], p["почему"], p["ярус"]] for p in places]
elif fn == "heads":
    out = [p["id"] for p in heads_fn(order_fn(data, line, settings, {}, {}))]
elif fn == "next":
    found = getattr(wq, "следующая")(data, line, settings, {}, {})
    out = found["id"] if found else None
elif fn == "parse":
    out = list(getattr(wq, "разобрать")(data, line))
print(json.dumps(out, ensure_ascii=False))
'''


class Stand:
    """Временный каталог стенда: копия прибора, свои логи и конфиги."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="test-work-queue-deps-"))
        scripts = self.root / "scripts"
        scripts.mkdir()
        self.script = scripts / "work-queue.py"
        shutil.copy2(SOURCE_SCRIPT, self.script)
        shutil.copytree(SOURCE_SCRIPT.parent / "lib", scripts / "lib",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.driver = self.root / "driver.py"
        self.driver.write_text(DRIVER, encoding="utf-8")
        logs = self.root / "log"
        logs.mkdir()
        install_conf = self.root / "install.conf"
        install_conf.write_text(f'LOG_DIR="{logs}"\n', encoding="utf-8")
        harness_conf = self.root / "harness.conf"
        harness_conf.write_text(f'ACTIVE_LINE="{LINE}"\n', encoding="utf-8")
        self.env = dict(os.environ, HARNESS_HOME=str(self.root / "home"),
                        HARNESS_INSTALL_CONF=str(install_conf),
                        HARNESS_CONF=str(harness_conf), LOG_DIR=str(logs),
                        HARNESS_ACTIVE_LINE=LINE)
        self.counter = 0
        self.elapsed = 0.0

    def _file(self, suffix: str) -> Path:
        self.counter += 1
        return self.root / f"in-{self.counter}{suffix}"

    def call(self, fn: str, data: dict, line: str = LINE):
        """Функция прибора на карте; падение и зависание — исключение с причиной."""
        request = self._file(".json")
        request.write_text(json.dumps({"map": data, "line": line, "fn": fn},
                                      ensure_ascii=False), encoding="utf-8")
        done = self._run([sys.executable, str(self.driver), str(self.script), str(request)],
                         self.env)
        if done.returncode != 0:
            raise ProbeError(f"{fn}() упал, код {done.returncode}: {done.stderr.strip()[-300:]}")
        return json.loads(done.stdout)

    def cli(self, data: dict | None, *flags: str, line: str = LINE,
            timeout: int = TIMEOUT):
        """Прибор командой, как его зовут сторож и судья: (код, stdout).

        Время самого процесса (без записи карты) — в `self.elapsed`.
        """
        args = [sys.executable, str(self.script)]
        if data is not None:
            card_map = self._file(".yaml")
            card_map.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                                encoding="utf-8")
            args.append(str(card_map))
        started = time.monotonic()
        done = self._run(args + list(flags), dict(self.env, HARNESS_ACTIVE_LINE=line), timeout)
        self.elapsed = time.monotonic() - started
        return done.returncode, done.stdout

    @staticmethod
    def _run(args: list, env: dict, timeout: int = TIMEOUT):
        try:
            return subprocess.run(args, capture_output=True, text=True, env=env,
                                  timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ProbeError(f"завис дольше {timeout} с: {' '.join(args[1:])}") from exc


class ProbeError(Exception):
    pass


def ready(stand: Stand, data: dict, *flags: str, line: str = LINE):
    """`--готовые`: (ids, прочие строки). Нарушение формы — ProbeError.

    id — строка без пробелов; строки с пробелом бывают только у `--битые` и
    `--ждут`, без этих флагов любая такая строка — нарушение формата.
    """
    code, out = stand.cli(data, "--готовые", *flags, line=line)
    lines = out.splitlines()
    head = re.fullmatch(r"готовых: (\d+)", lines[0]) if lines else None
    if code != 0 or not head:
        raise ProbeError(f"--готовые: код {code}, первая строка не «готовых: N»: {out[:200]!r}")
    ids = [s for s in lines[1:] if re.fullmatch(r"\S+", s)]
    other = [s for s in lines[1:] if not re.fullmatch(r"\S+", s)]
    if int(head.group(1)) != len(ids):
        raise ProbeError(f"«готовых: {head.group(1)}», а id в выводе {len(ids)}: {ids}")
    if other and not flags:
        raise ProbeError(f"без флагов у --готовые лишние строки: {other}")
    return ids, other


def card(card_id: str, status: str = "plan", **fields) -> dict:
    return {"id": card_id, "status": status, **fields}


def stand_map(*tasks, other=(), queue=()) -> dict:
    """Карта линии `h` и, если надо, второй линии `p` — для поиска по всей карте."""
    lines = [{"id": LINE, "tasks": list(tasks)}]
    if other:
        lines.append({"id": "p", "tasks": list(other)})
    return {"queue": list(queue), "lines": lines}


def expect(condition: bool, what: str) -> None:
    if not condition:
        raise ProbeError(what)


def expect_ready(stand: Stand, data: dict, wanted: set, absent: set = frozenset()) -> list:
    """Состав `--готовые` и согласие с `порядок()`: оба вывода одного прибора."""
    ids, _ = ready(stand, data)
    expect(wanted <= set(ids), f"--готовые {ids}: нет {sorted(wanted - set(ids))}")
    expect(not absent & set(ids), f"--готовые {ids}: лишние {sorted(absent & set(ids))}")
    order = [p[0] for p in stand.call("order", data)]
    expect(not absent & set(order), f"порядок() {order}: ждущие {sorted(absent & set(order))}")
    return ids


def waiting_line(stand: Stand, data: dict, waiter: str, pred: str, status: str) -> None:
    _, other = ready(stand, data, "--ждут")
    line = f"ждёт: {waiter} ← {pred} ({status})"
    expect(line in other, f"--готовые --ждут: нет строки «{line}» в {other}")


def cycle_line(stand: Stand, data: dict, cycle: list) -> None:
    """Строка цикла с любой точки входа, но в направлении зависимостей."""
    _, other = ready(stand, data, "--ждут")
    rotations = [cycle[i:] + cycle[:i] for i in range(len(cycle))]
    found = []
    for s in other:
        if s.startswith("цикл зависимостей: "):
            parts = s[len("цикл зависимостей: "):].split(" → ")
            found.append(parts)
            if parts[0] == parts[-1] and parts[:-1] in rotations:
                return
    raise ProbeError(f"--готовые --ждут: нет «цикл зависимостей: "
                     f"{' → '.join(cycle + cycle[:1])}», циклы в выводе: {found}, всё: {other}")


def cycle_reason(stand: Stand, data: dict, members: list) -> None:
    _, waits = stand.call("parse", data)
    for member in members:
        reason = next((w for w in waits if w.startswith(f"{member}=")), None)
        expect(reason is not None, f"разобрать(): {member} не в «ждут»: {waits}")
        expect("цикл" in reason and all(m in reason for m in members),
               f"разобрать(): причина {reason!r} не называет цикл и всех {members}")


# ── случаи ────────────────────────────────────────────────────────────────


def case_unmet_wip_plan(stand: Stand) -> None:
    """plan, wip и прочие статусы предшественника — не выполнена."""
    data = stand_map(card("pw", "wip"), card("pp"), card("pk", "беклог"),
                     card("x1", **{DEPENDS: ["pw"]}), card("x2", **{DEPENDS: ["pp"]}),
                     card("x3", **{DEPENDS: ["pk"]}))
    expect_ready(stand, data, {"pw", "pp"}, {"x1", "x2", "x3"})
    waiting_line(stand, data, "x1", "pw", "wip")


def case_met_test_done_accepted(stand: Stand) -> None:
    data = stand_map(card("pt", "test"), card("pd", "done"), card("pa", "принято"),
                     card("x1", **{DEPENDS: ["pt"]}), card("x2", **{DEPENDS: ["pd"]}),
                     card("x3", **{DEPENDS: ["pa"]}))
    ids = expect_ready(stand, data, {"x1", "x2", "x3"})
    expect(set(ids) == {"x1", "x2", "x3"}, f"--готовые {ids}, ждали ровно x1 x2 x3")


def case_other_line(stand: Stand) -> None:
    """Предшественник в другой линии: судят его статус, а не «карточки нет»."""
    data = stand_map(card("x1", **{DEPENDS: ["o.wip"]}), card("x2", **{DEPENDS: ["o.test"]}),
                     other=[card("o.wip", "wip"), card("o.test", "test")])
    ids = expect_ready(stand, data, {"x2"}, {"x1"})
    expect(ids == ["x2"], f"--готовые {ids}, ждали [x2]")


def case_broken_id(stand: Stand) -> None:
    data = stand_map(card("x", **{DEPENDS: ["no-such"]}), card("z"))
    expect_ready(stand, data, {"x", "z"})
    _, other = ready(stand, data, "--битые")
    line = "битая зависимость: x → no-such"
    expect(line in other, f"--готовые --битые: нет «{line}» в {other}")


def case_empty_as_before_order(stand: Stand) -> None:
    """Пустое поле — как без поля: прибор уже сейчас так себя ведёт."""
    data = stand_map(card("x1", **{DEPENDS: []}), card("x2", **{DEPENDS: None}), card("x3"))
    order = [p[0] for p in stand.call("order", data)]
    expect(order == ["x1", "x2", "x3"], f"порядок() {order}, ждали x1 x2 x3")
    expect(stand.call("next", data) == "x1", "следующая() не x1")


def case_empty_as_before_ready(stand: Stand) -> None:
    data = stand_map(card("x1", **{DEPENDS: []}), card("x2", **{DEPENDS: None}), card("x3"))
    ids, _ = ready(stand, data)
    expect(ids == ["x1", "x2", "x3"], f"--готовые {ids}, ждали x1 x2 x3")


def case_scalar(stand: Stand) -> None:
    data = stand_map(card("pw", "wip"), card("pt", "test"),
                     card("x1", **{DEPENDS: "pw"}), card("x2", **{DEPENDS: "pt"}))
    expect_ready(stand, data, {"pw", "x2"}, {"x1"})


def garbage_map() -> dict:
    return stand_map(card("x1", **{DEPENDS: 42}), card("x2", **{DEPENDS: True}),
                     card("x3", **{DEPENDS: {"pw": 1}}), card("x4", **{DEPENDS: [5, None]}),
                     card("pw", "wip"))


def case_garbage_no_crash(stand: Stand) -> None:
    """Мусор в поле не роняет прибор — уже сейчас (поле не читается)."""
    data = garbage_map()
    order = [p[0] for p in stand.call("order", data)]
    expect({"x1", "x2", "x3", "x4"} <= set(order), f"порядок() {order}: мусор спрятал карточку")
    code, out = stand.cli(data, "--порядок")
    expect(code == 0 and "сейчас делаем: pw" in out, f"--порядок код {code}: {out[:200]!r}")


def case_garbage_ready(stand: Stand) -> None:
    data = garbage_map()
    data["lines"][0]["tasks"].append(card("x5", **{DEPENDS: [5, None, "pw"]}))
    expect_ready(stand, data, {"x1", "x2", "x3", "x4", "pw"}, {"x5"})


def case_mixed(stand: Stand) -> None:
    """Одна выполненная зависимость не перевешивает невыполненную."""
    data = stand_map(card("pt", "test"), card("pw", "wip"),
                     card("x", **{DEPENDS: ["pt", "pw"]}))
    expect_ready(stand, data, {"pw"}, {"x"})


def case_chain(stand: Stand) -> None:
    data = stand_map(card("a", **{DEPENDS: ["b"]}), card("b", **{DEPENDS: ["c"]}), card("c"))
    ids = expect_ready(stand, data, {"c"}, {"a", "b"})
    expect(ids == ["c"], f"--готовые {ids}, ждали [c]")


def cycles_map() -> dict:
    return stand_map(card("a", **{DEPENDS: ["b"]}), card("b", **{DEPENDS: ["a"]}),
                     card("s", **{DEPENDS: ["s"]}),
                     card("c1", **{DEPENDS: ["c2"]}), card("c2", **{DEPENDS: ["c3"]}),
                     card("c3", **{DEPENDS: ["c1"]}), card("z"))


def case_cycles(stand: Stand) -> None:
    """Циклы из 2, 3 и на себя: члены ждут, свободная z остаётся готовой."""
    data = cycles_map()
    ids = expect_ready(stand, data, {"z"}, {"a", "b", "s", "c1", "c2", "c3"})
    expect(ids == ["z"], f"--готовые {ids}, ждали [z]")
    expect(stand.call("next", data) == "z", "следующая() не z")
    cycle_reason(stand, data, ["a", "b"])
    cycle_reason(stand, data, ["s"])
    cycle_reason(stand, data, ["c1", "c2", "c3"])
    cycle_line(stand, data, ["a", "b"])
    cycle_line(stand, data, ["s"])
    cycle_line(stand, data, ["c1", "c2", "c3"])


def case_only_cycle(stand: Stand) -> None:
    data = stand_map(card("a", **{DEPENDS: ["b"]}), card("b", **{DEPENDS: ["a"]}))
    code, out = stand.cli(data, "--следующая")
    expect(code == 1 and out.startswith("СЛЕДУЮЩЕЙ РАБОТЫ НЕТ"),
           f"--следующая код {code}: {out[:200]!r}")
    ids, _ = ready(stand, data)
    expect(ids == [], f"--готовые {ids}, ждали пусто")


def waiting_base(with_waiter: bool) -> dict:
    """Ждущая — plan в очереди со свежим источником: без зависимости она была
    бы головой, и её след в порядке остальных виден сразу. Не wip: начатое
    зависимость не прячет (случай «начатое не прячем»)."""
    tasks = [card("p", "plan", источник="inbox:1000.0"),
             card("q", "plan", источник="inbox:3000.0"), card("r")]
    if with_waiter:
        tasks.insert(0, card("x", "plan", источник="inbox:9000.0", **{DEPENDS: ["p"]}))
    return stand_map(*tasks, queue=["r", "x"] if with_waiter else ["r"])


def case_order_of_others(stand: Stand) -> None:
    with_x = [p[0] for p in stand.call("order", waiting_base(True))]
    without = [p[0] for p in stand.call("order", waiting_base(False))]
    expect(len(without) == 3, f"стенд без ждущей пуст: {without}")
    expect(with_x == without, f"порядок() с ждущей {with_x}, без неё {without}")


def case_next_names_ready(stand: Stand) -> None:
    data = waiting_base(True)
    code, out = stand.cli(data, "--следующая")
    named = re.match(r"СЛЕДУЮЩАЯ РАБОТА: (\S+) — ", out)
    expect(code == 0 and named is not None, f"--следующая код {code}: {out[:200]!r}")
    expect(named.group(1) in {"p", "q", "r"}, f"--следующая назвала {named.group(1)}")
    expect(stand.call("next", data) in {"p", "q", "r"}, "следующая() назвала ждущую")


def case_order_cli_hides_waiter(stand: Stand) -> None:
    code, out = stand.cli(waiting_base(True), "--порядок")
    numbered = re.findall(r"^  \d+\. (\S+) · ", out, re.M)
    now = re.search(r"^сейчас делаем: (\S+)", out, re.M)
    expect(code == 0 and set(numbered) >= {"p", "q", "r"}, f"--порядок: список {numbered}")
    expect("x" not in numbered, f"--порядок печатает ждущую: {numbered}")
    expect(now is not None and now.group(1) != "x", f"--порядок: «сейчас делаем» {now and now.group(1)}")


def case_parse_counts(stand: Stand) -> None:
    work, waits = stand.call("parse", waiting_base(True))
    expect(work == 3, f"разобрать(): работа {work}, ждали 3 (x ждёт p)")
    reason = next((w for w in waits if w.startswith("x=")), None)
    expect(reason is not None and "p" in reason[2:], f"разобрать(): «ждут» {waits} без x=…p")


def composition_map() -> dict:
    return stand_map(
        card("w", "wip"), card("f", "test", провал_проверки="не сошлось"),
        card("p1", источник="inbox:5000.0"), card("p2"), card("q"),
        card("b", blocked="ждёт владельца"), card("d", "done"), card("a", "принято"),
        card("k", "беклог"), card("t", "test"),
        card("tf", "test", checklist=[{"label": "а", "done": True}, {"label": "б"}]),
        queue=["q"])


def case_ready_composition(stand: Stand) -> None:
    data = composition_map()
    ids = expect_ready(stand, data, {"w", "f", "p1", "p2", "q"}, {"b", "d", "a", "k", "t"})
    # test со свободным пунктом порядок() отдаёт последним ярусом (эталон случая 21),
    # а --готовые отсекает через головные() — проверяем только второе
    expect("tf" not in ids, f"--готовые {ids}: лишний tf (test со свободным пунктом)")
    heads = stand.call("heads", data)
    expect(ids == heads, f"--готовые {ids} не в порядке головных порядок() {heads}")


def case_ready_zero(stand: Stand) -> None:
    data = stand_map(card("d", "done"), card("b", blocked="ждёт"), card("t", "test"))
    code, out = stand.cli(data, "--готовые")
    expect(code == 0 and out == "готовых: 0\n", f"--готовые код {code}: {out!r}")


def case_ready_forty(stand: Stand) -> None:
    """Потолок строк порядка (10) список готовых не режет — и без линии тоже."""
    names = [f"n{i:02d}" for i in range(40)]
    data = stand_map(*[card(n) for n in names])
    for line in (LINE, ""):
        ids, _ = ready(stand, data, line=line)
        expect(ids == names, f"линия {line!r}: --готовые дал {len(ids)} из 40: {ids[:12]}")


# Эталон карты БЕЗ поля `зависит_от`, снят 04.11.2026 со скрипта до правки
# (sha256 4da60bd2…, важность 220b4056): правка Ч7 не смеет сдвинуть ни веса,
# ни порядок, ни текст. Рядом со стендом нет queue.yaml — классов нет: у всех 5,
# у провала проверки 2.
REFERENCE_MAP = stand_map(
    card("s.wip", "wip", источник="inbox:1000.0"),
    card("s.queued"),
    card("s.fail", "test", провал_проверки="не сошлось"),
    card("s.half", checklist=[{"label": "а", "done": True}, {"label": "б"}]),
    card("s.blocked", blocked="ждёт владельца"),
    card("s.blocked-free", blocked="ждёт цифру",
         checklist=[{"label": "вопрос", "blocked": "ждёт цифру"}, {"label": "свободный"}]),
    card("s.test", "test"),
    card("s.test-free", "test", checklist=[{"label": "а", "done": True}, {"label": "б"}]),
    card("s.done", "done"), card("s.accepted", "принято"), card("s.backlog", "беклог"),
    *[card(f"s.p{i:02d}", источник=f"inbox:{2000 + i}.0") for i in range(12)],
    other=[card("o.plan"), card("o.wip", "wip")],
    queue=["s.queued", "s.ghost"])

REFERENCE_ORDER_LINE = [['s.wip', 30, 'начата, важность 5', 0],
                        ['s.fail', 40, 'провалила проверку, важность 2', 1],
                        ['s.queued', 20, 'в очереди, важность 5', 1],
                        ['s.ghost', 20, 'в очереди, а карточки в карте нет', 1],
                        ['s.half', 10, 'важность 5, закрыто 50 % пунктов', 1],
                        ['s.p11', 9, 'важность 5', 1],
                        ['s.p09', 8, 'важность 5', 1],
                        ['s.p10', 8, 'важность 5', 1],
                        ['s.p08', 7, 'важность 5', 1],
                        ['s.p07', 6, 'важность 5', 1],
                        ['s.p06', 5, 'важность 5', 1],
                        ['s.p04', 4, 'важность 5', 1],
                        ['s.p05', 4, 'важность 5', 1],
                        ['s.p03', 3, 'важность 5', 1],
                        ['s.p01', 2, 'важность 5', 1],
                        ['s.p02', 2, 'важность 5', 1],
                        ['s.p00', 1, 'важность 5', 1],
                        ['s.blocked-free', 0,
                         'важность 5, блокер: ждёт цифру, но свободен пункт: «свободный»', 1],
                        ['s.test-free', 10, 'важность 5, закрыто 50 % пунктов', 2]]
REFERENCE_ORDER_ALL = [['s.wip', 30, 'начата, важность 5', 0],
                       ['o.wip', 30, 'начата, важность 5', 0],
                       ['s.fail', 40, 'провалила проверку, важность 2', 1],
                       ['s.queued', 20, 'в очереди, важность 5', 1],
                       ['s.ghost', 20, 'в очереди, а карточки в карте нет', 1],
                       ['s.half', 10, 'важность 5, закрыто 50 % пунктов', 1],
                       ['s.p11', 9, 'важность 5', 1],
                       ['s.p09', 8, 'важность 5', 1],
                       ['s.p10', 8, 'важность 5', 1],
                       ['s.p08', 7, 'важность 5', 1]]
REFERENCE_PARSE = [17, ['s.blocked=ждёт владельца', 's.blocked-free=ждёт цифру']]
REFERENCE_CLI = (
    "работа: 17\n"
    "ждут: s.blocked=ждёт владельца; s.blocked-free=ждёт цифру\n"
    "сейчас делаем: s.wip — начата, важность 5\n"
    "  1. s.wip · 30 · начата, важность 5\n"
    "  2. s.fail · 40 · провалила проверку, важность 2\n"
    "  3. s.queued · 20 · в очереди, важность 5\n"
    "  4. s.ghost · 20 · в очереди, а карточки в карте нет\n"
    "  5. s.half · 10 · важность 5, закрыто 50 % пунктов\n"
    "  6. s.p11 · 9 · важность 5\n"
    "  7. s.p09 · 8 · важность 5\n"
    "  8. s.p10 · 8 · важность 5\n"
    "  9. s.p08 · 7 · важность 5\n"
    "  10. s.p07 · 6 · важность 5\n"
)


def case_reference(stand: Stand) -> None:
    got = stand.call("order", REFERENCE_MAP)
    expect(got == REFERENCE_ORDER_LINE, f"порядок() линии h разошёлся с эталоном: {got}")
    got = stand.call("order", REFERENCE_MAP, line="")
    expect(got == REFERENCE_ORDER_ALL, f"порядок() всей карты разошёлся с эталоном: {got}")
    got = stand.call("parse", REFERENCE_MAP)
    expect(got == REFERENCE_PARSE, f"разобрать() разошёлся с эталоном: {got}")
    code, out = stand.cli(REFERENCE_MAP, "--порядок")
    expect(code == 0 and out == REFERENCE_CLI, f"--порядок разошёлся с эталоном: {out!r}")


def case_selftest(stand: Stand) -> None:
    code, out = stand.cli(None, "--selftest")
    expect(code == 0, f"--selftest код {code}: {out.strip()[-300:]}")


def big_map(cycle: bool) -> dict:
    """2000 карточек: каждая зависит от следующей; последняя plan или замыкает цикл."""
    names = [f"c{i:04d}" for i in range(2000)]
    tasks = [card(n, **{DEPENDS: [names[i + 1]]}) for i, n in enumerate(names[:-1])]
    tasks.append(card(names[-1], **({DEPENDS: [names[0]]} if cycle else {})))
    return stand_map(*tasks)


def timed(stand: Stand, data: dict, *flags: str):
    code, out = stand.cli(data, *flags, timeout=SPEED_TIMEOUT)
    expect(stand.elapsed < SPEED_LIMIT,
           f"{' '.join(flags)}: {stand.elapsed:.1f} с, предел {SPEED_LIMIT} с")
    return code, out


def case_speed_chain(stand: Stand) -> None:
    data = big_map(cycle=False)
    code, out = timed(stand, data, "--порядок")
    expect(code == 0 and "работа: 1\n" in out and "сейчас делаем: c1999 " in out,
           f"--порядок код {code}: {out[:200]!r}")
    code, out = timed(stand, data, "--готовые", "--ждут")
    expect(code == 0 and out.startswith("готовых: 1\nc1999\n"),
           f"--готовые --ждут код {code}: {out[:200]!r}")
    expect("ждёт: c0000 ← c0001 (plan)" in out, "--готовые --ждут: нет «ждёт: c0000 ← c0001»")
    code, out = timed(stand, data, "--следующая")
    expect(code == 0 and out.startswith("СЛЕДУЮЩАЯ РАБОТА: c1999 — "),
           f"--следующая код {code}: {out[:200]!r}")


def case_speed_cycle(stand: Stand) -> None:
    data = big_map(cycle=True)
    code, out = timed(stand, data, "--порядок")
    expect(code == 0 and out.startswith("работа: 0\n") and "сейчас делаем: очередь пуста" in out,
           f"--порядок код {code}: {out[:200]!r}")
    code, out = timed(stand, data, "--готовые", "--ждут")
    expect(code == 0 and out.startswith("готовых: 0\n") and "цикл зависимостей: " in out,
           f"--готовые --ждут код {code}: {out[:200]!r}")
    code, out = timed(stand, data, "--следующая")
    expect(code == 1 and out.startswith("СЛЕДУЮЩЕЙ РАБОТЫ НЕТ"),
           f"--следующая код {code}: {out[:200]!r}")


# Слово владельца: журнал внимания в LOG_DIR стенда, свежая метка. Id сообщения
# свой у каждого случая, файл убирается после случая — другие случаи его не видят.
def with_owner_word(stand: Stand, words: dict, check) -> None:
    journal = Path(stand.env["LOG_DIR"]) / "внимание.jsonl"
    now = time.time()
    journal.write_text("".join(json.dumps({"id": ид, "метка": now, "слово": слово},
                                          ensure_ascii=False) + "\n"
                               for ид, слово in words.items()), encoding="utf-8")
    try:
        check()
    finally:
        journal.unlink()


def expect_ready_is_work(stand: Stand, data: dict, work: set) -> None:
    """`--готовые` — те же id, что «работа: N»: и по составу, и по числу."""
    ids, _ = ready(stand, data)
    expect(set(ids) == work, f"--готовые {ids}, «работа» разобрать() — {sorted(work)}")
    count, _ = stand.call("parse", data)
    expect(count == len(work), f"разобрать(): работа {count}, ждали {len(work)} {sorted(work)}")
    code, out = stand.cli(data, "--порядок")
    expect(code == 0 and out.startswith(f"работа: {len(ids)}\n"),
           f"--готовые дал {len(ids)}, а --порядок: {out.splitlines()[:1]}")


def blocker_card() -> dict:
    return card("blk", blocked="ждёт цифру",
                checklist=[{"label": "вопрос", "blocked": "ждёт цифру"}, {"label": "свободный"}])


def case_same_as_work_blocker(stand: Stand) -> None:
    """Блокер со свободным пунктом: порядок() его держит, «работа» — нет."""
    expect_ready_is_work(stand, stand_map(blocker_card(), card("z")), {"z"})


def case_same_as_work_owner_word(stand: Stand) -> None:
    """blocked без свободных пунктов со словом владельца: «работа» его не считает."""
    data = stand_map(card("blk", blocked="ждёт владельца", источник="inbox:7100.5"), card("z"))
    with_owner_word(stand, {"7100.5": "срочно"},
                    lambda: expect_ready_is_work(stand, data, {"z"}))


def case_same_as_work_mixed(stand: Stand) -> None:
    """Смесь: блокеры не работа; провал проверки с блокером, начатая и plan
    со словом владельца (без блокера) — работа, как у разобрать() сейчас."""
    data = stand_map(blocker_card(),
                     card("bw", blocked="ждёт владельца", источник="inbox:7200.5"),
                     card("f", "test", провал_проверки="не сошлось", blocked="ждёт ключ",
                          checklist=[{"label": "починить"}]),
                     card("w", источник="inbox:7300.5"), card("wp", "wip"), card("z"))
    with_owner_word(stand, {"7200.5": "срочно", "7300.5": "важно"},
                    lambda: expect_ready_is_work(stand, data, {"f", "w", "wp", "z"}))


def case_started_not_hidden(stand: Stand) -> None:
    """Начатая с невыполненной зависимостью — в работе везде; plan с той же — ждёт."""
    data = stand_map(card("pp"), card("x", "wip", **{DEPENDS: ["pp"]}),
                     card("y", **{DEPENDS: ["pp"]}))
    order = [p[0] for p in stand.call("order", data)]
    expect("x" in order and "y" not in order, f"порядок() {order}: ждали x и без y")
    expect(stand.call("next", data) == "x", "следующая() не x (начатая — голова)")
    code, out = stand.cli(data, "--порядок")
    numbered = re.findall(r"^  \d+\. (\S+) · ", out, re.M)
    expect(code == 0 and "x" in numbered and "y" not in numbered, f"--порядок: список {numbered}")
    expect("сейчас делаем: x " in out, f"--порядок: «сейчас делаем» не x: {out[:200]!r}")
    code, out = stand.cli(data, "--следующая")
    expect(code == 0 and out.startswith("СЛЕДУЮЩАЯ РАБОТА: x — "), f"--следующая: {out[:200]!r}")
    ids = expect_ready(stand, data, {"x", "pp"}, {"y"})
    expect(set(ids) == {"x", "pp"}, f"--готовые {ids}, ждали x pp")
    work, waits = stand.call("parse", data)
    expect(work == 2, f"разобрать(): работа {work}, ждали 2 (pp, x)")
    expect(not any(w.startswith("x=") for w in waits), f"разобрать(): начатая x в «ждут»: {waits}")
    expect(any(w.startswith("y=") for w in waits), f"разобрать(): y не в «ждут»: {waits}")
    _, other = ready(stand, data, "--ждут")
    expect(not any(s.startswith("ждёт: x ") for s in other), f"--готовые --ждут: x ждёт: {other}")
    waiting_line(stand, data, "y", "pp", "plan")


def case_test_not_waiting(stand: Stand) -> None:
    """test без провала и свободного пункта — ни работа, ни «ждут», и с зависимостью."""
    data = stand_map(card("pp"), card("t", "test", **{DEPENDS: ["pp"]}))
    work, waits = stand.call("parse", data)
    expect(work == 1, f"разобрать(): работа {work}, ждали 1 (pp)")
    expect(not any(w.startswith("t=") for w in waits), f"разобрать(): сделанная t в «ждут»: {waits}")


CASES = [
    ("предшественник wip/plan/беклог — не готова", case_unmet_wip_plan),
    ("предшественник test/done/принято — готова", case_met_test_done_accepted),
    ("предшественник в другой линии учитывается", case_other_line),
    ("несуществующий id не держит + «битая зависимость»", case_broken_id),
    ("пусто/None/нет поля: порядок() как раньше", case_empty_as_before_order),
    ("пусто/None/нет поля: в --готовые", case_empty_as_before_ready),
    ("скаляр-строка = один id", case_scalar),
    ("мусор в поле не роняет порядок() и --порядок", case_garbage_no_crash),
    ("мусор игнорируется, строка в списке с мусором учитывается", case_garbage_ready),
    ("смесь выполненной и невыполненной — ждёт", case_mixed),
    ("цепочка a→b→c, c=plan — готова только c", case_chain),
    ("циклы 2/3/на себя не вешают, свободная z готова", case_cycles),
    ("только цикл — «СЛЕДУЮЩЕЙ РАБОТЫ НЕТ», код 1", case_only_cycle),
    ("ждущая не меняет порядок остальных", case_order_of_others),
    ("--следующая называет только готовую", case_next_names_ready),
    ("--порядок не печатает ждущую", case_order_cli_hides_waiter),
    ("разобрать() не считает ждущую работой и называет причину", case_parse_counts),
    ("состав и порядок --готовые", case_ready_composition),
    ("пусто — ровно «готовых: 0»", case_ready_zero),
    ("40 plan — «готовых: 40» без потолка строк", case_ready_forty),
    ("карта без поля — эталон порядка и --порядок побайтно", case_reference),
    ("--selftest зелёный", case_selftest),
    ("скорость: цепочка из 2000", case_speed_chain),
    ("скорость: цикл из 2000", case_speed_cycle),
    ("--готовые = «работа»: блокер со свободным пунктом", case_same_as_work_blocker),
    ("--готовые = «работа»: blocked со словом владельца", case_same_as_work_owner_word),
    ("--готовые = «работа»: смешанный стенд", case_same_as_work_mixed),
    ("начатая с невыполненной зависимостью не прячется", case_started_not_hidden),
    ("test без провала и пункта с зависимостью не в «ждут»", case_test_not_waiting),
]


def main() -> int:
    stand = Stand()
    failed = 0
    try:
        for name, case in CASES:
            try:
                case(stand)
                print(f"ok   {name}")
            except ProbeError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    finally:
        shutil.rmtree(stand.root, ignore_errors=True)
    print(f"ЗАВИСИМОСТИ: случаев {len(CASES)}, провалов {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
