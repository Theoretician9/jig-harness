#!/usr/bin/env python3
"""Проба модуля журнала раскладки (lib/layout_journal.py): разбор журнала прямого
прогона, решения отката, поиск оборванных журналов, итог отката.

Модуль берётся РЯДОМ с пробой (Path(__file__).parent / "lib"): мутации подставляет
mutation-probe.sh копией корня scripts/, и мутированный модуль обязан дойти до пробы.

Решения отката — чистые функции: диск и копии архива подаются функциями-словарями,
модуль на диск сам не ходит. На диск ходят только J10 и J11 — в свой mktemp; живой
каталог копий, проект и /etc/harness не читаются и не пишутся.
Модуля нет — каждый случай красный «нет модуля», а не трасса и не зелень.
Итог: «ЖУРНАЛ РАСКЛАДКИ: путей N, неудач M», код 0/1.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
MODULE_PATH = (HERE / "lib" / "layout_journal.py").absolute()


def load_module():
    if not MODULE_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location("layout_journal", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Probe:
    def __init__(self) -> None:
        self.paths = 0
        self.failures = 0

    def check(self, label: str, name: str, ok: bool, detail: str = "") -> None:
        self.paths += 1
        if ok:
            print(f"  ок    {label} {name}")
        else:
            self.failures += 1
            print(f"  ПЛОХО {label} {name}: {detail}")

    def crashed(self, label: str, error: Exception | None) -> None:
        self.paths += 1
        self.failures += 1
        if LJ is None:
            print(f"  ПЛОХО {label} нет модуля {MODULE_PATH}")
        else:
            print(f"  ПЛОХО {label} проба упала: {type(error).__name__}: {error}")


PROBE = Probe()
LJ = None

# Blobs are literals, not computed: the module compares strings, never reads files.
X = "1" * 40          # own file before the update
B = "b" * 40          # what the new version laid
A = "a" * 40          # what plan A lays
C = "c" * 40          # edited after the update
Y = "e" * 40          # edited copy left in the forward archive (Stop after rename)
FWD = "/копии/раскладка/стенд-20261024-000000-1"


# ─────────────────────────────── помощники ───────────────────────────────────

def text(*lines: str, header: str = "# прогон 1 обновление план=4 перекрыть=1") -> str:
    """Журнал строками; каждая строка с \\n, как пишет дверь."""
    return "".join(line + "\n" for line in (header, *lines))


def act(kind: str, was: str, now: str, path: str) -> str:
    return f"{kind}\t{was}\t{now}\t{path}"


def done(path: str) -> str:
    return f"готово\t-\t-\t{path}"


def broken_reason(raw: str) -> str | None:
    """Причина JournalBroken или None. Договор не говорит, бросается JournalBroken или
    возвращается (как ManifestBroken описи) — проба принимает оба вида, но не третий."""
    try:
        result = LJ.parse_journal(raw)
    except Exception as error:
        if type(error).__name__ != "JournalBroken":
            raise
        return str(getattr(error, "reason", "") or error) or "(пустая причина)"
    if isinstance(result, LJ.JournalBroken):
        return str(getattr(result, "reason", "")) or "(пустая причина)"
    return None


def parse(raw: str):
    reason = broken_reason(raw)
    if reason is not None:
        raise AssertionError(f"разбор отказал на годном журнале: {reason}")
    return LJ.parse_journal(raw)


def count(value) -> int:
    """unknown / undone: число или список — договор называет смысл, не тип."""
    return value if isinstance(value, int) else len(value or ())


def as_paths(value) -> set:
    """remove / relaid_new: пути, как ни отдано (множество, список, пары, словарь)."""
    if value is None:
        return set()
    if isinstance(value, dict):
        return set(value)
    return {item[0] if isinstance(item, tuple) else item for item in value}


def as_map(value) -> dict:
    """restore / keep: {путь: blob|причина}; пары (путь, значение) тоже принимаются."""
    if isinstance(value, dict):
        return dict(value)
    return {path: other for path, other in (value or ())}


def decide(raw: str, plan_a: dict, disk: dict, copies: dict, mode_differs=None):
    """rollback_decisions с диском «после укладки A» и копиями архива прямого словарями:
    пути нет в словаре — файла нет (None)."""
    extra = {} if mode_differs is None else {"mode_differs": mode_differs}
    return LJ.rollback_decisions(parse(raw), plan_a, lambda p: disk.get(p),
                                 lambda p: copies.get(p), **extra)


def touched(d, path: str) -> list:
    """В каких решениях стоит путь: «ничего» = пустой список."""
    found = []
    for name in ("remove", "relaid_new"):
        if path in as_paths(getattr(d, name)):
            found.append(name)
    for name in ("restore", "keep"):
        if path in as_map(getattr(d, name)):
            found.append(name)
    return found


def write_archive(root: Path, name: str, lines: list | None, extra_files=()) -> Path:
    """Каталог архива с journal.tsv (lines None — без журнала) и файлами в files/."""
    archive = root / name
    (archive / "files").mkdir(parents=True)
    if lines is not None:
        (archive / "journal.tsv").write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    for rel, data in extra_files:
        path = archive / "files" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return archive


def names(found) -> dict:
    """[(архив, K, …)] → {имя каталога: K}: архив отдан строкой или Path — судится имя."""
    return {Path(str(row[0])).name: row[1] for row in found}


# ─────────────────────────────── случаи ──────────────────────────────────────

def case_j1() -> None:
    """Разбор: шапки трёх режимов, «по=», копия описи, «итог», два «разобран» после итога;
    «готово» связывается с действием по ПУТИ.
    Зелен на поломке? «готово закрывает предыдущую строку, чей бы путь ни был» — нет:
    «готово» чужого пути обязано оставить действие открытым; «разобран читается только
    до итога» — нет: две строки после итога судятся списком. Привязку к первому или
    последнему открытому действию пути случай НЕ различает: дверь пишет «готово» сразу за
    своим действием, и журнал двери этих двух прочтений не разводит."""
    manifest_blob = "f" * 40
    raw = text(f"# опись до прогона: files/.layout-manifest {manifest_blob}",
               act("перекрыт", X, B, "scripts/deploy.sh"), done("scripts/deploy.sh"),
               act("положен-новый", "-", B, "scripts/new.sh"),
               "итог\t-\t-\t2",
               "разобран\t-\t-\t/копии/раскладка/стенд-20261024-000100-2",
               "разобран\t-\t-\t/копии/раскладка/стенд-20261024-000200-3-откат")
    j = parse(raw)
    PROBE.check("J1", "шапка «обновление»: mode", j.mode == "обновление", f"mode {j.mode!r}")
    PROBE.check("J1", "обновление без «по=»: forward_of пуст", not j.forward_of,
                f"forward_of {j.forward_of!r}")
    kinds = [(a.kind, a.path, a.was, a.now, a.done) for a in j.actions]
    PROBE.check("J1", "действия: перекрыт (готово) и положен-новый (без готово)",
                kinds == [("перекрыт", "scripts/deploy.sh", X, B, True),
                          ("положен-новый", "scripts/new.sh", "-", B, False)], f"{kinds}")
    PROBE.check("J1", "итог есть", j.has_total is True, f"has_total {j.has_total!r}")
    PROBE.check("J1", "два «разобран» после итога — списком, в порядке",
                list(j.resolved_by) == ["/копии/раскладка/стенд-20261024-000100-2",
                                        "/копии/раскладка/стенд-20261024-000200-3-откат"],
                f"resolved_by {j.resolved_by!r}")
    PROBE.check("J1", "копия описи: blob из «# опись до прогона:»",
                j.manifest_copy is not None and manifest_blob in str(j.manifest_copy),
                f"manifest_copy {j.manifest_copy!r}")
    j = parse(text(header="# прогон 7 раскладка план=4 перекрыть=0"))
    PROBE.check("J1", "шапка «раскладка», без действий, без итога, копии описи нет",
                j.mode == "раскладка" and not j.actions and j.has_total is False
                and j.manifest_copy is None, f"{j.mode!r} {j.actions!r} {j.has_total!r}")
    j = parse(text(act("перекрыт", B, A, "scripts/deploy.sh"), done("scripts/deploy.sh"),
                   act("снят", B, "-", "scripts/new.sh"), done("scripts/new.sh"),
                   act("возвращён", A, X, "scripts/deploy.sh"),
                   f"# оставлен\tправлен после обновления\tscripts/b.sh",
                   header=f"# прогон 9 откат план=4 перекрыть=1 по={FWD}"))
    PROBE.check("J1", "шапка «откат»: mode и forward_of = путь после «по=»",
                j.mode == "откат" and str(j.forward_of) == FWD,
                f"mode {j.mode!r}, forward_of {j.forward_of!r}")
    last = [(a.kind, a.done) for a in j.actions if a.path == "scripts/deploy.sh"]
    PROBE.check("J1", "один путь дважды: «готово» у перекрыт, у возвращён — нет",
                last == [("перекрыт", True), ("возвращён", False)], f"{last}")
    PROBE.check("J1", "«# оставлен» → kept [(причина, путь)]",
                [tuple(k) for k in j.kept] == [("правлен после обновления", "scripts/b.sh")],
                f"kept {j.kept!r}")
    j = parse(text(act("перекрыт", X, B, "scripts/a.sh"), done("scripts/b.sh")))
    PROBE.check("J1", "«готово» чужого пути не закрывает действие",
                [(a.path, a.done) for a in j.actions] == [("scripts/a.sh", False)], f"{j.actions!r}")


def case_j2() -> None:
    """Отказ разбора своим словом (решение смены (б): незнакомое ДЕЙСТВИЕ → код 3, вслепую
    не откатывать): «перенесён», строка из трёх полей, blob не 40 hex.
    Зелен на поломке? «нечитаемое = пустой журнал» — нет: пустой журнал дал бы «снято 0»,
    здесь обязан быть JournalBroken с причиной, называющей строку."""
    good = act("перекрыт", X, B, "scripts/deploy.sh")
    cases = (("незнакомое действие «перенесён»", act("перенесён", X, B, "scripts/a.sh"),
              "перенесён"),
             ("строка из трёх полей", f"перекрыт\t{X}\tscripts/a.sh", None),
             ("blob не 40 hex", act("перекрыт", "abc", B, "scripts/a.sh"), None),
             ("blob-стало не hex", act("положен-новый", "-", "Z" * 40, "scripts/a.sh"), None))
    for name, line, word in cases:
        reason = broken_reason(text(good, done("scripts/deploy.sh"), line, "итог\t-\t-\t2"))
        PROBE.check("J2", f"{name} → JournalBroken с причиной", bool(reason),
                    f"разобрано без отказа: {line!r}")
        if word:
            PROBE.check("J2", f"{name}: причина называет строку ({word})",
                        reason is not None and word in reason, f"причина {reason!r}")
    PROBE.check("J2", "пара: годный журнал разбирается без отказа",
                broken_reason(text(good, done("scripts/deploy.sh"), "итог\t-\t-\t1")) is None,
                "годный журнал отвергнут")


def case_j3() -> None:
    """Таблица отката, «положен-новый p B», p ∉ A: диск B → снять; диск C → оставить
    «правлен после обновления»; диска нет → оставить «уже не было».
    Зелен на поломке? «снимать по журналу, не глядя на диск» — нет: диск C обязан
    остаться; «нет файла = снять» — нет: причина «уже не было» судится словом."""
    raw = text(act("положен-новый", "-", B, "scripts/new.sh"), done("scripts/new.sh"),
               "итог\t-\t-\t1")
    plan_a = {"scripts/deploy.sh": A}
    d = decide(raw, plan_a, {"scripts/new.sh": B}, {})
    PROBE.check("J3", "диск B → remove", touched(d, "scripts/new.sh") == ["remove"],
                f"решения {touched(d, 'scripts/new.sh')}")
    d = decide(raw, plan_a, {"scripts/new.sh": C}, {})
    PROBE.check("J3", "диск C → keep «правлен после обновления»",
                touched(d, "scripts/new.sh") == ["keep"]
                and as_map(d.keep)["scripts/new.sh"] == "правлен после обновления",
                f"решения {touched(d, 'scripts/new.sh')}, keep {as_map(d.keep)}")
    d = decide(raw, plan_a, {}, {})
    PROBE.check("J3", "диска нет → keep «уже не было»",
                touched(d, "scripts/new.sh") == ["keep"]
                and as_map(d.keep)["scripts/new.sh"] == "уже не было",
                f"решения {touched(d, 'scripts/new.sh')}, keep {as_map(d.keep)}")


def case_j4() -> None:
    """«перекрыт p X→B» с «готово» (расхождение 4 плана): X = A[p] → ничего; X ≠ A[p], диск
    после укладки A = A[p], копия X → вернуть X; p ∉ A, диск B, копия X → вернуть X; копии
    нет или копия не X → оставить «копии нет в архиве»; диск C ∉ {A[p], B} → оставить
    «правлен после обновления».
    Зелен на поломке? «возврат поверх правки» — нет: диск C обязан остаться; «возврат без
    сверки копии» — нет: копия Y ≠ X обязана дать «копии нет в архиве»."""
    p = "scripts/deploy.sh"
    raw = lambda was: text(act("перекрыт", was, B, p), done(p), "итог\t-\t-\t1")
    d = decide(raw(A), {p: A}, {p: A}, {p: A})
    PROBE.check("J4", "X = A[p] → ничего", touched(d, p) == [], f"решения {touched(d, p)}")
    d = decide(raw(X), {p: A}, {p: A}, {p: X})
    PROBE.check("J4", "X ≠ A[p], диск A[p], копия X → restore X",
                touched(d, p) == ["restore"] and as_map(d.restore)[p] == X,
                f"решения {touched(d, p)}, restore {as_map(d.restore)}")
    d = decide(raw(X), {}, {p: B}, {p: X})
    PROBE.check("J4", "p ∉ A, диск B, копия X → restore X",
                touched(d, p) == ["restore"] and as_map(d.restore)[p] == X,
                f"решения {touched(d, p)}, restore {as_map(d.restore)}")
    for name, copies in (("копии нет", {}), ("копия не X", {p: Y})):
        d = decide(raw(X), {}, {p: B}, copies)
        PROBE.check("J4", f"{name} → keep «копии нет в архиве»",
                    touched(d, p) == ["keep"] and as_map(d.keep)[p] == "копии нет в архиве",
                    f"решения {touched(d, p)}, keep {as_map(d.keep)}")
    for name, plan_a in (("p ∉ A", {}), ("p ∈ A", {p: A})):
        d = decide(raw(X), plan_a, {p: C}, {p: X})
        PROBE.check("J4", f"{name}, диск C ∉ {{A[p], B}} → keep «правлен после обновления»",
                    touched(d, p) == ["keep"] and as_map(d.keep)[p] == "правлен после обновления",
                    f"решения {touched(d, p)}, keep {as_map(d.keep)}")


def case_j4_mode() -> None:
    """Блоб владельца совпал с блобом плана A, но копия хранит режим владельца, а укладка A
    кладёт режим пакета: путь с другим режимом возвращается из копии, с тем же — нет."""
    p = "scripts/deploy.sh"
    raw = text(act("перекрыт", A, B, p), done(p), "итог\t-\t-\t1")
    d = decide(raw, {p: A}, {p: A}, {p: A}, mode_differs=lambda path: path == p)
    PROBE.check("J4м", "X = A[p], режим копии другой → restore",
                touched(d, p) == ["restore"] and as_map(d.restore)[p] == A,
                f"решения {touched(d, p)}, restore {as_map(d.restore)}")
    d = decide(raw, {p: A}, {p: A}, {p: A}, mode_differs=lambda path: False)
    PROBE.check("J4м", "X = A[p], режим тот же → ничего", touched(d, p) == [],
                f"решения {touched(d, p)}")


def case_j_unbounded() -> None:
    """journal.tsv — симлинк на бесконечное устройство: ответ (None, причина) быстро, без роста памяти."""
    import resource
    import tempfile
    import time
    for device in ("/dev/zero", "/dev/full"):
        with tempfile.TemporaryDirectory() as archive:
            os.symlink(device, os.path.join(archive, "journal.tsv"))
            before, started = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, time.time()
            journal, reason = LJ.read_journal(archive)
            grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
            PROBE.check("Jпредел", f"{device} → (None, причина) за < 20 с, рост < 200 МБ",
                        journal is None and bool(reason) and time.time() - started < 20
                        and grown < 200 * 1024,
                        f"журнал {journal!r}, причина {reason!r}, рост {grown} КБ")


def case_j5() -> None:
    """R1-05 ревью Ф-а: «без готово» судится диском, а не строкой. «перекрыт p X→B» без
    «готово», диск X (Stop до replace) → ничего, undone = 1; тот же, диск B (убит после
    replace) → вернуть X; «положен-новый» без «готово», диска нет → ничего, не «уже не было».
    Зелен на поломке? «без готово = не сделано» — нет: диск B обязан дать возврат;
    «без готово = сделано» — нет: диск X не возвращается поверх себя."""
    p = "scripts/own.sh"
    raw = text(act("перекрыт", X, B, p))
    d = decide(raw, {}, {p: X}, {p: X})
    PROBE.check("J5", "перекрыт без готово, диск X → ничего", touched(d, p) == [],
                f"решения {touched(d, p)}")
    PROBE.check("J5", "undone = 1 (путей без «готово»)", count(d.undone) == 1,
                f"undone {d.undone!r}")
    d = decide(raw, {}, {p: B}, {p: X})
    PROBE.check("J5", "перекрыт без готово, диск B → restore X",
                touched(d, p) == ["restore"] and as_map(d.restore)[p] == X,
                f"решения {touched(d, p)}, restore {as_map(d.restore)}")
    q = "scripts/new.sh"
    d = decide(text(act("положен-новый", "-", B, q)), {}, {}, {})
    PROBE.check("J5", "положен-новый без готово, диска нет → ничего (не «уже не было»)",
                touched(d, q) == [], f"решения {touched(d, q)}, keep {as_map(d.keep)}")
    d = decide(text(act("положен-новый", "-", B, q)), {}, {q: B}, {})
    PROBE.check("J5", "пара: положен-новый без готово, диск B → remove",
                touched(d, q) == ["remove"], f"решения {touched(d, q)}")


def case_j6() -> None:
    """«снят p X» с «готово» (расхождение 2 плана): p ∈ A → ничего (укладка A кладёт);
    p ∉ A, диска нет, копия X → вернуть X; p ∉ A, диск занят → оставить «занято».
    Зелен на поломке? «снятое не возвращается» — нет: p ∉ A обязан вернуться; «возврат
    поверх занятого» — нет: занятый путь обязан остаться."""
    p = "scripts/old.sh"
    raw = text(act("снят", X, "-", p), done(p), "итог\t-\t-\t1")
    d = decide(raw, {p: A}, {p: A}, {p: X})
    PROBE.check("J6", "p ∈ A → ничего", touched(d, p) == [], f"решения {touched(d, p)}")
    d = decide(raw, {}, {}, {p: X})
    PROBE.check("J6", "p ∉ A, диска нет, копия X → restore X",
                touched(d, p) == ["restore"] and as_map(d.restore)[p] == X,
                f"решения {touched(d, p)}, restore {as_map(d.restore)}")
    d = decide(raw, {}, {p: C}, {p: X})
    PROBE.check("J6", "p ∉ A, диск занят → keep «занято»",
                touched(d, p) == ["keep"] and as_map(d.keep)[p] == "занято",
                f"решения {touched(d, p)}, keep {as_map(d.keep)}")


def case_j7() -> None:
    """«положен-новый p», а p есть в плане A (расхождение 3): relaid_new, не снимать.
    Зелен на поломке? «снять всё положенное новым» — нет: файл плана A снят бы и тут же
    положен — или, хуже, снят после укладки A."""
    p = "scripts/deploy.sh"
    d = decide(text(act("положен-новый", "-", B, p), done(p), "итог\t-\t-\t1"),
               {p: A}, {p: A}, {})
    PROBE.check("J7", "p ∈ A → relaid_new = {p}", as_paths(d.relaid_new) == {p},
                f"relaid_new {d.relaid_new!r}")
    PROBE.check("J7", "p ∈ A → не в remove", p not in as_paths(d.remove), f"remove {d.remove!r}")


def case_j8() -> None:
    """Порога у отката нет (R2-07): 40 «положен-новый», все на диске B → снять все 40.
    Зелен на поломке? «порог доли и для отката» — нет: при любом пороге < 100 % часть
    осталась бы."""
    paths = [f"scripts/new{i:02d}.sh" for i in range(40)]
    lines = [line for p in paths for line in (act("положен-новый", "-", B, p), done(p))]
    d = decide(text(*lines, "итог\t-\t-\t40"), {"scripts/deploy.sh": A},
               {p: B for p in paths}, {})
    PROBE.check("J8", "40 из 40 → remove", as_paths(d.remove) == set(paths),
                f"remove {len(as_paths(d.remove))} из 40")


def case_j9() -> None:
    """Имя архива — выражением по точному имени проекта (расхождение 11): маска
    «<имя>-*» поймала бы архивы проекта «<имя>-x» второго харнеса на той же машине.
    Зелен на поломке? «startswith(имя)» — нет: «стенд-x-…» обязан не подойти; «без $» —
    нет: «….bak» обязан не подойти; «без re.escape» — нет: «a.b» против «aXb-…»."""
    pattern = LJ.archive_name_pattern("стенд")
    for name, want in (("стенд-20261024-003900-77", True),
                       ("стенд-20261024-003900-77-откат", True),
                       ("стенд-x-20261024-003900-77", False),
                       ("стенд-20261024-003900-77.bak", False),
                       ("стенд-20261024-0039-77", False),
                       ("xстенд-20261024-003900-77", False)):
        PROBE.check("J9", f"«{name}» → {'да' if want else 'нет'}",
                    bool(pattern.search(name)) == want, f"search {pattern.pattern!r}")
    dotted = LJ.archive_name_pattern("a.b")
    PROBE.check("J9", "имя «a.b»: «a.b-…» да, «aXb-…» нет (re.escape)",
                bool(dotted.search("a.b-20261024-003900-7"))
                and not dotted.search("aXb-20261024-003900-7"), f"{dotted.pattern!r}")


def case_j10() -> None:
    """find_orphans на mktemp: с итогом; без итога; без итога, но «разобран»; без итога у
    проекта «стенд-x»; в exclude; каталог без journal.tsv → ровно один оборванный, K =
    путей без «готово» (решение смены (е)); остаток *.copy-tmp в files/ K не меняет;
    пустой корень и нет корня → [].
    Зелен на поломке? «итог ⇒ не оборван, остальное — оборван» — нет: «разобран» и
    exclude обязаны выпасть; «маска имени» — нет: стенд-x; «каталог = журнал» — нет: пустой
    каталог обязан не назваться."""
    root = Path(tempfile.mkdtemp(prefix="layout-journal-probe-"))
    try:
        head = "# прогон 1 обновление план=4 перекрыть=1"
        partial = [head, act("перекрыт", X, B, "scripts/a.sh"), done("scripts/a.sh"),
                   act("положен-новый", "-", B, "scripts/b.sh"),
                   act("положен-новый", "-", B, "scripts/c.sh")]
        write_archive(root, "стенд-20261024-000001-1", partial[:3] + ["итог\t-\t-\t1"])
        write_archive(root, "стенд-20261024-000002-2", partial,
                      [("scripts/.a.sh.copy-tmp", b"half")])
        write_archive(root, "стенд-20261024-000003-3", partial + ["разобран\t-\t-\t-"])
        write_archive(root, "стенд-x-20261024-000004-4", partial)
        excluded = write_archive(root, "стенд-20261024-000005-5-откат", partial)
        write_archive(root, "стенд-20261024-000006-6", None)
        write_archive(root, "стенд-20261024-000007-7", [head, "перекрыт\tне-blob\t-\tscripts/a.sh"])
        # exclude: path as str and as Path — the contract names the set, not the element type.
        found = names(LJ.find_orphans(str(root), "стенд", {str(excluded), excluded}))
        PROBE.check("J10", "найдены: оборванный 000002-2 и непрочитанный 000007-7 (K None)",
                    found == {"стенд-20261024-000002-2": 2, "стенд-20261024-000007-7": None},
                    f"найдено {found}")
        broken = [row for row in LJ.find_orphans(str(root), "стенд", {str(excluded)})
                  if row[0].endswith("000007-7")]
        PROBE.check("J10", "непрочитанный назван причиной словами",
                    len(broken) == 1 and broken[0][2] and "blob" in broken[0][2], f"{broken}")
        empty = root / "пусто"
        empty.mkdir()
        PROBE.check("J10", "пустой корень → []", list(LJ.find_orphans(str(empty), "стенд", set())) == [],
                    "не пусто")
        PROBE.check("J10", "нет корня → [] (не трасса)",
                    list(LJ.find_orphans(str(root / "нет"), "стенд", set())) == [], "не пусто")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_j11() -> None:
    """resolved_by_archive по МНОЖЕСТВУ архивов постановки (Р12: оборванный называет и
    откатный прогон): «разобран» одного журнала называет X, другого — Y, третьего — Z, у
    чужого проекта — X → при {X, Y} ровно два своих, с K путей без «готово».
    Зелен на поломке? «ищет по одному архиву» — нет: Y обязан найтись; «маска имени» —
    нет: стенд-x с X обязан выпасть."""
    root = Path(tempfile.mkdtemp(prefix="layout-journal-probe-"))
    try:
        x, y, z = (str(root / f"стенд-20261024-0100{i}0-9{i}") for i in (1, 2, 3))
        head = "# прогон 1 раскладка план=4 перекрыть=0"
        one = [head, act("положен-новый", "-", B, "scripts/b.sh")]
        write_archive(root, "стенд-20261023-000001-1", one + [f"разобран\t-\t-\t{x}"])
        write_archive(root, "стенд-20261023-000002-2",
                      one + [act("положен-новый", "-", B, "scripts/c.sh"), f"разобран\t-\t-\t{y}"])
        write_archive(root, "стенд-20261023-000003-3", one + [f"разобран\t-\t-\t{z}"])
        write_archive(root, "стенд-x-20261023-000004-4", one + [f"разобран\t-\t-\t{x}"])
        write_archive(root, "стенд-20261023-000005-5",
                      one + [done("scripts/b.sh"), "итог\t-\t-\t1", f"разобран\t-\t-\t{x}"])
        found = names(LJ.resolved_by_archive(str(root), "стенд", {x, y}))
        PROBE.check("J11", "{X, Y} → ровно два своих журнала",
                    set(found) == {"стенд-20261023-000001-1", "стенд-20261023-000002-2"},
                    f"найдено {found} (журнал с «итог» оборванным не был: 000005-5 не в счёте)")
        PROBE.check("J11", "K = путей без «готово»: 1 и 2",
                    found.get("стенд-20261023-000001-1") == 1
                    and found.get("стенд-20261023-000002-2") == 2, f"найдено {found}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_j12() -> None:
    """rollback_outcome журнала отката: снят×3 (2 с готово), возвращён×1 с готово,
    «# оставлен»×2 → снято 2, возвращено 1, оставлено 2; шапка «обновление» → по журналу
    False; count_new_version_files прямого журнала из 9 действий → 9.
    Зелен на поломке? «снято = строк снят» — нет: 3 строки, готово у двух; «по журналу
    всегда» — нет: шапка обновление обязана дать False."""
    lines = [act("снят", B, "-", "scripts/n1.sh"), done("scripts/n1.sh"),
             act("снят", B, "-", "scripts/n2.sh"), done("scripts/n2.sh"),
             act("снят", B, "-", "scripts/n3.sh"),
             act("возвращён", A, X, "scripts/deploy.sh"), done("scripts/deploy.sh"),
             "# оставлен\tправлен после обновления\tscripts/e1.sh",
             "# оставлен\tзанято\tscripts/e2.sh"]
    out = LJ.rollback_outcome(parse(text(*lines, header=f"# прогон 9 откат план=4 перекрыть=0 по={FWD}")))
    PROBE.check("J12", "по журналу: True", out.get("по журналу") is True, f"итог {out}")
    PROBE.check("J12", "снято 2, возвращено 1",
                out.get("снято") == 2 and out.get("возвращено") == 1, f"итог {out}")
    kept = out.get("оставлено") or []
    PROBE.check("J12", "оставлено — 2, оба пути названы",
                len(kept) == 2 and all(any(p in str(k) for k in kept)
                                       for p in ("scripts/e1.sh", "scripts/e2.sh")),
                f"оставлено {kept!r}")
    out = LJ.rollback_outcome(parse(text(*lines)))
    PROBE.check("J12", "шапка «обновление» → по журналу False", out.get("по журналу") is False,
                f"итог {out}")
    forward = [act(kind, was, now, f"scripts/{kind}{i}.sh")
               for i in range(3) for kind, was, now in
               (("перекрыт", X, B), ("положен-новый", "-", B), ("снят", X, "-"))]
    forward += [done("scripts/перекрыт0.sh"), done("scripts/снят1.sh")]
    PROBE.check("J12", "count_new_version_files: 9 действий (с готово и без) → 9",
                LJ.count_new_version_files(parse(text(*forward))) == 9,
                f"{LJ.count_new_version_files(parse(text(*forward)))!r}")


def case_j13() -> None:
    """Комментарии журнала: «# снять сверх порога: x» (Ф-в), «# опись нечитаема: …» (Ф-б),
    «# будущее: что-то» (пишет новая версия, читает старая, Р11), «# опись не снята: причина»
    → разбор проходит; причина неснятой описи прочитана; действия разобраны.
    Зелен на поломке? «незнакомый # = беда» — нет: откат старой дверью над журналом новой
    упал бы на любой новой строке; «причина теряется» — нет: manifest_missing сверен."""
    raw = text("# снять сверх порога: проба", "# опись нечитаема: files/.layout-manifest",
               "# будущее: что-то", "# опись не снята: диск полон", act("перекрыт", X, B, "scripts/a.sh"), done("scripts/a.sh"),
               "итог\t-\t-\t1")
    reason = broken_reason(raw)
    PROBE.check("J13", "три комментария → разбор без отказа", reason is None, f"причина {reason!r}")
    if reason is None:
        j = LJ.parse_journal(raw)
        PROBE.check("J13", "причина неснятой описи прочитана", j.manifest_missing == "диск полон",
                    f"manifest_missing {j.manifest_missing!r}")
        PROBE.check("J13", "действие разобрано, готово связано",
                    [(a.kind, a.path, a.done) for a in j.actions] == [("перекрыт", "scripts/a.sh", True)],
                    f"{j.actions!r}")


def case_j14() -> None:
    """Путь действия за проектом (Р21: журнал — данные в каталоге копий, его можно
    подложить): «../x», «/etc/x», «a/../../b», пустой → JournalBroken «за проектом».
    Путь архива в «разобран» абсолютный — это не путь проекта, отказа нет.
    Зелен на поломке? «пути журнала не судятся» — нет: возврат по «../x» писал бы за
    проект; «судится и разобран» — нет: годный «разобран /копии/…» обязан пройти."""
    for path in ("../x", "/etc/x", "scripts/../../b", ""):
        for line in (act("положен-новый", "-", B, path), act("перекрыт", X, B, path)):
            reason = broken_reason(text(line))
            PROBE.check("J14", f"«{line.split(chr(9))[0]}» путь «{path}» → JournalBroken «за проектом»",
                        reason is not None and "за проектом" in reason, f"причина {reason!r}")
    reason = broken_reason(text(act("положен-новый", "-", B, "scripts/a.sh"), done("scripts/a.sh"),
                                "итог\t-\t-\t1", f"разобран\t-\t-\t{FWD}"))
    PROBE.check("J14", "пара: «разобран» с абсолютным путём архива — без отказа", reason is None,
                f"причина {reason!r}")


def case_j15() -> None:
    """Недописанная последняя строка (SIGKILL посреди записи): без \\n это не
    действие; обрывок «перекр» — не беда разбора. Та же строка с \\n — действие.
    Зелен на поломке? «недописанная = действие» — нет: путь обязан не попасть в actions;
    «обрывок = JournalBroken» — нет: оборванный журнал обязан читаться."""
    head = "# прогон 1 обновление план=4 перекрыть=1\n"
    whole = act("перекрыт", X, B, "scripts/a.sh")
    j = parse(head + whole)
    PROBE.check("J15", "строка без \\n: не действие",
                not [a for a in j.actions if a.path == "scripts/a.sh"], f"{j.actions!r}")
    reason = broken_reason(head + "перекр")
    PROBE.check("J15", "обрывок «перекр» → не JournalBroken", reason is None, f"причина {reason!r}")
    j = parse(head + whole + "\n")
    PROBE.check("J15", "пара: та же строка с \\n — действие без готово",
                [(a.kind, a.done) for a in j.actions] == [("перекрыт", False)], f"{j.actions!r}")


def case_j16() -> None:
    """Пять состояний «снят p X» (договор Ф-в: перенос rename или копия при EXDEV), p ∉ A:
    (1) с «готово», файла нет, копия X → вернуть X;
    (2) без «готово», файла нет, копия X (rename прошёл, убит) → вернуть X;
    (3) без «готово», файла нет, копия Y ≠ X (Stop после rename, правка) → вернуть Y
        (решение смены (г): возвращается, если пути в проекте нет);
    (4) без «готово», файл X и копия X (убит между replace и unlink EXDEV) → ничего;
    (5) без «готово», файл X, копии нет (EXDEV, копия не совпала) → ничего.
    Зелен на поломке? «без готово = не сделано» — нет: (2) и (3) обязаны вернуть;
    «файл есть = занято» — нет: (4) и (5) не «занято»; «копия ≠ X = копии нет» — нет: (3)."""
    p = "scripts/old.sh"
    with_done = text(act("снят", X, "-", p), done(p), "итог\t-\t-\t1")
    undone = text(act("снят", X, "-", p))
    for name, raw, disk, copies, want in (
            ("(1) с готово, файла нет, копия X", with_done, {}, {p: X}, ("restore", X)),
            ("(2) без готово, файла нет, копия X", undone, {}, {p: X}, ("restore", X)),
            ("(3) без готово, файла нет, копия Y", undone, {}, {p: Y}, ("restore", Y)),
            ("(4) без готово, файл X и копия X", undone, {p: X}, {p: X}, None),
            ("(5) без готово, файл X, копии нет", undone, {p: X}, {}, None)):
        d = decide(raw, {}, disk, copies)
        if want is None:
            PROBE.check("J16", f"{name} → ничего", touched(d, p) == [],
                        f"решения {touched(d, p)}, keep {as_map(d.keep)}")
        else:
            PROBE.check("J16", f"{name} → restore {want[1][:4]}…",
                        touched(d, p) == ["restore"] and as_map(d.restore)[p] == want[1],
                        f"решения {touched(d, p)}, restore {as_map(d.restore)}")


def case_j17() -> None:
    """Путь занят — возврат не пишет (решение смены (г)): «снят p X» без «готово», p ∉ A,
    файл C ≠ X: копия X → оставить «занято»; копия Y (правка в архиве) → оставить, не
    вернуть; p ∈ A без «готово» → ничего (укладка A кладёт).
    Зелен на поломке? «копия правленая — вернуть всегда» — нет: занятый путь обязан
    остаться; «p ∈ A вернуть из копии» — нет: укладка A уже положила файл плана."""
    p = "scripts/old.sh"
    undone = text(act("снят", X, "-", p))
    d = decide(undone, {}, {p: C}, {p: X})
    PROBE.check("J17", "файл C, копия X → keep «занято»",
                touched(d, p) == ["keep"] and as_map(d.keep)[p] == "занято",
                f"решения {touched(d, p)}, keep {as_map(d.keep)}")
    d = decide(undone, {}, {p: C}, {p: Y})
    PROBE.check("J17", "файл C, копия Y → keep (не restore)", touched(d, p) == ["keep"],
                f"решения {touched(d, p)}, keep {as_map(d.keep)}")
    d = decide(undone, {p: A}, {p: A}, {p: X})
    PROBE.check("J17", "p ∈ A без готово → ничего", touched(d, p) == [], f"решения {touched(d, p)}")


def case_j18() -> None:
    """«возвращён» без «готово» не считается возвращённым; count_new_version_files не
    считает «возвращён» (это действие отката, не новой версии).
    Зелен на поломке? «возвращено = строк возвращён» — нет: 2 строки, готово у одной."""
    lines = [act("возвращён", A, X, "scripts/a.sh"), done("scripts/a.sh"),
             act("возвращён", A, X, "scripts/b.sh")]
    j = parse(text(*lines, header=f"# прогон 9 откат план=4 перекрыть=0 по={FWD}"))
    out = LJ.rollback_outcome(j)
    PROBE.check("J18", "возвращено 1 (без готово не считается)", out.get("возвращено") == 1,
                f"итог {out}")
    j = parse(text(act("положен-новый", "-", B, "scripts/n.sh"), *lines))
    PROBE.check("J18", "count_new_version_files: возвращён не считается → 1",
                LJ.count_new_version_files(j) == 1, f"{LJ.count_new_version_files(j)!r}")


def case_j19() -> None:
    """read_journal: нет файла → (None, None); битая строка → (None, причина словами); годный →
    (журнал, None). «Нет» и «не прочитан» — разные ответы: тишина на втором прячет оборванный прогон.
    Зелен на поломке? «любой сбой = нет» — нет: причина обязана быть непустой."""
    root = Path(tempfile.mkdtemp(prefix="layout-journal-probe-"))
    try:
        head = "# прогон 1 обновление план=4 перекрыть=1"
        none = write_archive(root, "стенд-20261024-000001-1", None)
        bad = write_archive(root, "стенд-20261024-000002-2", [head, "мусор"])
        good = write_archive(root, "стенд-20261024-000003-3", [head, act("перекрыт", X, B, "a")])
        PROBE.check("J19", "нет файла → (None, None)", LJ.read_journal(none) == (None, None),
                    f"{LJ.read_journal(none)!r}")
        journal, reason = LJ.read_journal(bad)
        PROBE.check("J19", "битый → (None, причина)", journal is None and bool(reason), f"{reason!r}")
        journal, reason = LJ.read_journal(good)
        PROBE.check("J19", "годный → (журнал, None)", journal is not None and reason is None
                    and len(journal.actions) == 1, f"{journal!r} {reason!r}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


CASES = (case_j1, case_j2, case_j3, case_j4, case_j4_mode, case_j_unbounded, case_j5, case_j6, case_j7, case_j8, case_j9,
         case_j10, case_j11, case_j12, case_j13, case_j14, case_j15, case_j16, case_j17,
         case_j18, case_j19)


def main() -> int:
    global LJ
    try:
        LJ = load_module()
    except Exception as error:      # a broken module must show as red per case, not a trace
        print(f"модуль {MODULE_PATH} не загрузился: {type(error).__name__}: {error}")
        LJ = None
    print(f"модуль: {MODULE_PATH}" + ("" if LJ is not None else " — НЕТ, каждый случай красный"))
    for case in CASES:
        label = case.__name__.removeprefix("case_").upper()
        if LJ is None:
            PROBE.crashed(label, None)
            continue
        try:
            case()
        except Exception as error:      # a probe bug must show as red, not as a trace
            PROBE.crashed(label, error)
    print(f"ЖУРНАЛ РАСКЛАДКИ: путей {PROBE.paths}, неудач {PROBE.failures}")
    return 0 if PROBE.failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
