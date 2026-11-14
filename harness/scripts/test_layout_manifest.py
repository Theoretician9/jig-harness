#!/usr/bin/env python3
"""Проба модуля описи раскладки (lib/layout_manifest.py): разбор, запись, суд «лишнего».

Модуль берётся РЯДОМ с пробой (Path(__file__).parent / "lib"): мутации подставляет
mutation-probe.sh копией корня scripts/, и мутированный модуль обязан дойти до пробы.

Каждый случай — свой mktemp с мини-проектом; живой проект не читается и не пишется.
Модуля нет — каждый случай красный «нет модуля», а не трасса и не зелень.
Итог: «ОПИСЬ РАСКЛАДКИ: путей N, неудач M», код 0/1.
"""
import ast
import hashlib
import importlib.util
import inspect
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
MODULE_PATH = (HERE / "lib" / "layout_manifest.py").absolute()
COMPARE_PATH = (HERE / "compare-with-package.py").absolute()
# Зеркало LIVE_SETTINGS раскладчика: живой файл — ярус 1, в опись не идёт.
LIVE_SETTINGS = ("pipeline.yaml", "queue.yaml", "models.yaml", "audit.yaml",
                 "live-check-state.yaml", "gates-quarantine.yaml")


def load_module():
    if not MODULE_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location("layout_manifest", MODULE_PATH)
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
        if LM is None:
            print(f"  ПЛОХО {label} нет модуля {MODULE_PATH}")
        else:
            print(f"  ПЛОХО {label} проба упала: {type(error).__name__}: {error}")


PROBE = Probe()
LM = None


# ─────────────────────────────── помощники ───────────────────────────────────

def blob_of_bytes(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def blob_of(path) -> str:
    return blob_of_bytes(Path(path).read_bytes())


def by_path(path: str) -> bytes:
    return path.encode("utf-8")


def manifest_text(body, kept=(), package: str = "-") -> str:
    """body: (blob, группа, путь); kept: (blob, причина, путь) — в договорном порядке."""
    lines = ["# layout-manifest 2", f"# пакет {package}"]
    lines += [f"{b}\t{g}\t{p}" for b, g, p in sorted(body, key=lambda r: by_path(r[2]))]
    lines.append("# оставлено")
    lines += [f"{b}\t{r}\t{p}" for b, r, p in sorted(kept, key=lambda r: by_path(r[2]))]
    return "\n".join(lines) + "\n"


def as_paths(value) -> set:
    """would_remove / gone / broken / not_removable: множество путей, как ни отдано."""
    if value is None:
        return set()
    if isinstance(value, dict):
        return set(value)
    return {item[0] if isinstance(item, tuple) else item for item in value}


def as_body(value) -> dict:
    """new_body / new_kept → {путь: (blob, группа|причина)}."""
    if isinstance(value, dict):
        return {path: tuple(fields) for path, fields in value.items()}
    return {path: (b, g) for path, b, g in value}


def summary(verdict, removed=(), overwritten: int = 0) -> str:
    """Сводка Ф-в — по факту снятия: summary_lines(removed, overwritten)."""
    return "\n".join(verdict.summary_lines(set(removed), overwritten))


def summary_any(verdict) -> str:
    """Случаи M8-M10 судят только отдельные строки Ф-б («опись испорчена: », «уже не было: »,
    «оставлено 1: »), которые Ф-в не меняет: зовут сводку той формой, какую модуль знает,
    чтобы смена интерфейса не красила их чужой причиной."""
    params = inspect.signature(verdict.summary_lines).parameters
    lines = verdict.summary_lines(set(), 0) if len(params) >= 2 else verdict.summary_lines()
    return "\n".join(lines)


class Mini:
    """Мини-проект в mktemp: файлы с известным blob, прежняя опись текстом."""

    def __init__(self) -> None:
        self.t = Path(tempfile.mkdtemp(prefix="layout-manifest-probe-"))
        self.proj = self.t / "проект"
        self.proj.mkdir()

    def __enter__(self) -> "Mini":
        return self

    def __exit__(self, *_exc) -> None:
        for path in self.t.rglob("*"):
            if not path.is_symlink():
                try:
                    os.chmod(path, 0o700)
                except OSError:
                    pass
        shutil.rmtree(self.t, ignore_errors=True)

    def put(self, rel: str, text: str | None = None) -> str:
        """Файл проекта; возвращает его blob."""
        path = self.proj / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if text is not None else f"содержимое {rel}\n", encoding="utf-8")
        return blob_of(path)

    def old(self, body, kept=()):
        parsed = LM.parse(manifest_text(body, kept))
        if isinstance(parsed, LM.ManifestBroken):
            raise RuntimeError(f"стенд: годная опись разобрана как испорченная: {parsed.reason}")
        return parsed

    def judge(self, old, plan, absent=(), percent=None, lift=False, project=None):
        # lift передаётся только когда он есть: случаи Ф-б не зависят от нового довода.
        extra = {"lift": True} if lift else {}
        return LM.judge_extra(old, set(plan), list(absent), str(project or self.proj), percent,
                              **extra)


# ─────────────────────────────── случаи ──────────────────────────────────────

def case_m1() -> None:
    """Круг render(parse(x)) == x; порядок входа не меняет байт; нет времени (R1-18).
    Зелен на поломке? «render без сортировки» — нет: перетасованный вход даёт иные байты."""
    blobs = [blob_of_bytes(f"{i}\n".encode()) for i in range(8)]
    body = [(blobs[0], "scripts", "scripts/a.sh"),
            (blobs[1], "unified", "UNIFIED/глава.md"),
            (blobs[2], "templates", "harness/шаблоны-задач/план.md"),
            (blobs[3], "harness-top", "harness/BUILD-SKILLS.py"),
            (blobs[4], "config", "harness/config/x.yaml")]
    kept = [(blobs[5], "правлен здесь", "scripts/x.sh"), (blobs[6], "ссылка", "scripts/b.sh")]
    text = manifest_text(body, kept, package="a" * 40)
    PROBE.check("M1", "render(parse(x)) == x (тело, кириллический путь, «оставлено»)",
                LM.render(LM.parse(text)) == text, f"render дал {LM.render(LM.parse(text))!r}")
    lines = text.splitlines()
    head, rest = lines[:2], lines[2:]
    cut = rest.index("# оставлено")
    for seed in (1, 2):
        shuffled_body, shuffled_kept = rest[:cut], rest[cut + 1:]
        random.Random(seed).shuffle(shuffled_body)
        random.Random(seed).shuffle(shuffled_kept)
        shuffled = "\n".join(head + shuffled_body + ["# оставлено"] + shuffled_kept) + "\n"
        parsed = LM.parse(shuffled)
        rendered = LM.render(parsed) if not isinstance(parsed, LM.ManifestBroken) else None
        PROBE.check("M1", f"перестановка входа {seed} → те же байты (сортировка по пути, UTF-8)",
                    rendered == text, f"получено {rendered!r}")
    empty_kept = manifest_text(body[:2], (), package="-")
    PROBE.check("M1", "пустая секция — одна строка «# оставлено», файл кончается «\\n»",
                LM.render(LM.parse(empty_kept)) == empty_kept and empty_kept.endswith("# оставлено\n"),
                f"render дал {LM.render(LM.parse(empty_kept))!r}")
    rendered = LM.render(LM.parse(text))
    PROBE.check("M1", "в тексте нет времени и пути архива (2026, 2025, «раскладка/»)",
                not any(word in rendered for word in ("2026", "2025", "раскладка/")), rendered)


def case_m2() -> None:
    """Нечитаемое — ManifestBroken со СВОЕЙ причиной, не пустая опись.
    Зелен на поломке? «всё ManifestBroken» — нет: пара — годный текст обязан разобраться."""
    good_blob = "b" * 40
    good = manifest_text([(good_blob, "scripts", "scripts/a.sh")])
    parsed = LM.parse(good)
    PROBE.check("M2", "пара: годная опись — не ManifestBroken, тело из одной строки",
                not isinstance(parsed, LM.ManifestBroken)
                and as_body(parsed.body) == {"scripts/a.sh": (good_blob, "scripts")},
                f"разбор дал {parsed!r}")
    broken = {
        "версия": good.replace("# layout-manifest 2", "# layout-manifest 1"),
        "нет строки пакета": good.replace("# пакет -\n", ""),
        "строка не из трёх полей": good.replace(f"{good_blob}\tscripts\t", f"{good_blob}\t"),
        "blob не 40 hex": good.replace(good_blob, "z" * 40),
        "повтор пути в теле": good.replace("# оставлено", f"{'c' * 40}\tscripts\tscripts/a.sh\n# оставлено"),
        "повтор пути тело/оставлено": good + f"{'c' * 40}\tправлен здесь\tscripts/a.sh\n",
        "причина «оставлено» вне четырёх": good + f"{'c' * 40}\tвозвращён откатом\tscripts/k.sh\n",
        "нет секции «# оставлено»": good.replace("# оставлено\n", ""),
        "пустой текст": "",
    }
    reasons = {}
    for name, text in broken.items():
        result = LM.parse(text)
        is_broken = isinstance(result, LM.ManifestBroken)
        reason = getattr(result, "reason", "") if is_broken else ""
        reasons[name] = reason
        PROBE.check("M2", f"{name} → ManifestBroken с непустой причиной",
                    is_broken and isinstance(reason, str) and reason.strip() != "",
                    f"разбор дал {result!r}")
    core = [reasons[k] for k in ("версия", "строка не из трёх полей", "blob не 40 hex",
                                 "повтор пути в теле")]
    PROBE.check("M2", "причины версии, полей, blob и повтора — разные слова",
                len(set(core)) == len(core) and all(core), f"причины {core}")


def case_m3() -> None:
    """A → B: кандидат назван и ОСТАЁТСЯ в new_body с прежними blob и группой — снимает
    дверь, тело без снятых даёт body_after (M16). Сводка — по факту снятия (Ф-в).
    Зелен на поломке? «кандидат выпал из new_body» — нет: судится new_body[old.sh]."""
    with Mini() as m:
        old_b, keep_b, new_b = m.put("scripts/old.sh"), m.put("scripts/keep.sh"), m.put("scripts/new.sh")
        old = m.old([(old_b, "scripts", "scripts/old.sh"), (keep_b, "scripts", "scripts/keep.sh")])
        verdict = m.judge(old, [("scripts/keep.sh", keep_b, "scripts"),
                                ("scripts/new.sh", new_b, "scripts")], percent=100)
        PROBE.check("M3", "would_remove == {scripts/old.sh}",
                    as_paths(verdict.would_remove) == {"scripts/old.sh"},
                    f"would_remove {as_paths(verdict.would_remove)}")
        body = as_body(verdict.new_body)
        PROBE.check("M3", "new_body ⊇ old.sh, keep.sh, new.sh",
                    {"scripts/old.sh", "scripts/keep.sh", "scripts/new.sh"} <= set(body),
                    f"тело {sorted(body)}")
        PROBE.check("M3", "old.sh в теле с прежними blob и группой",
                    body.get("scripts/old.sh") == (old_b, "scripts"), f"{body.get('scripts/old.sh')}")
        text = summary(verdict, removed={"scripts/old.sh"})
        PROBE.check("M3", "сводка по факту «снято 1: scripts/old.sh», без «снял бы»",
                    "снято 1: scripts/old.sh" in text and "снял бы" not in text, text)
        PROBE.check("M3", "файл old.sh на месте (модуль не снимает — снимает дверь)",
                    (m.proj / "scripts/old.sh").is_file(), "файла нет")


def unified_stand(m: Mini):
    rows = [(m.put(f"UNIFIED/{n}.md"), "unified", f"UNIFIED/{n}.md") for n in ("01", "02", "03")]
    keep_b = m.put("scripts/keep.sh")
    old = m.old(rows + [(keep_b, "scripts", "scripts/keep.sh")])
    return old, rows, [("scripts/keep.sh", keep_b, "scripts")]


def case_m4() -> None:
    """Группа без источника: строки не судятся, остаются в теле. Пара без absent — кандидаты 3.
    Зелен на поломке? «absent не судится» — нет: would_remove был бы {3}, пара доказывает стенд."""
    with Mini() as m:
        old, rows, plan = unified_stand(m)
        verdict = m.judge(old, plan, absent=["UNIFIED/"], percent=100)
        PROBE.check("M4", "absent=[UNIFIED/] → would_remove пуст",
                    as_paths(verdict.would_remove) == set(),
                    f"would_remove {as_paths(verdict.would_remove)}")
        body = as_body(verdict.new_body)
        PROBE.check("M4", "три строки UNIFIED/ в new_body с прежними blob и группой",
                    all(body.get(p) == (b, g) for b, g, p in rows), f"тело {body}")
        deferred = dict(verdict.deferred)
        PROBE.check("M4", "три строки отложены «группа без источника»",
                    all(deferred.get(p) == "группа без источника" for _b, _g, p in rows),
                    f"deferred {deferred}")
        text = summary(verdict)
        PROBE.check("M4", "сводка «группа unified: источник не найден — её 3 », «отложено 3 (группа без источника 3)»",
                    "группа unified: источник не найден — её 3 " in text
                    and "отложено 3 (группа без источника 3)" in text, text)
    with Mini() as m:
        old, rows, plan = unified_stand(m)
        verdict = m.judge(old, plan, percent=100)
        extra = {p for _b, _g, p in rows}
        text = summary(verdict, removed=as_paths(verdict.would_remove))
        PROBE.check("M4", "пара без absent: would_remove — три строки UNIFIED/, «снято 3»",
                    as_paths(verdict.would_remove) == extra and "снято 3: " in text,
                    f"would_remove {as_paths(verdict.would_remove)}; {text}")


def case_m5() -> None:
    """Одиночный файл без источника: точное совпадение, не префикс (расхождение 1).
    Зелен на поломке? «префикс вместо точного» — нет: README-ПЛАГИН.md.bak обязан быть кандидатом."""
    with Mini() as m:
        readme = "harness/README-ПЛАГИН.md"
        readme_b, build_b, bak_b = m.put(readme), m.put("harness/BUILD-SKILLS.py"), m.put(readme + ".bak")
        old = m.old([(readme_b, "harness-top", readme), (build_b, "harness-top", "harness/BUILD-SKILLS.py"),
                     (bak_b, "harness-top", readme + ".bak")])
        verdict = m.judge(old, [("harness/BUILD-SKILLS.py", build_b, "harness-top")], absent=[readme],
                          percent=100)
        PROBE.check("M5", "README-ПЛАГИН.md не кандидат, отложен «группа без источника»",
                    readme not in as_paths(verdict.would_remove)
                    and dict(verdict.deferred).get(readme) == "группа без источника",
                    f"would_remove {as_paths(verdict.would_remove)}; deferred {dict(verdict.deferred)}")
        PROBE.check("M5", "README-ПЛАГИН.md в теле с прежним blob",
                    as_body(verdict.new_body).get(readme) == (readme_b, "harness-top"),
                    f"{as_body(verdict.new_body).get(readme)}")
        PROBE.check("M5", "соседний README-ПЛАГИН.md.bak вне плана — кандидат (не префикс)",
                    readme + ".bak" in as_paths(verdict.would_remove),
                    f"would_remove {as_paths(verdict.would_remove)}")


def threshold_stand(m: Mini):
    """8 строк прежнего тела, 3 из них — кандидаты (37,5 %)."""
    rows = [(m.put(f"scripts/s{i}.sh"), "scripts", f"scripts/s{i}.sh") for i in range(8)]
    plan = [(p, b, g) for b, g, p in rows[3:]]
    return m.old(rows), {p for _b, _g, p in rows[:3]}, plan


def case_m6() -> None:
    """Порог: доля 37,5 % больше 25 — всё отложено и в теле; 50 и 37,5 — снимается 3 (строго больше).
    Зелен на поломке? «>» → «>=» — нет: граница 37.5 обязана дать would_remove = 3."""
    with Mini() as m:
        old, extra, plan = threshold_stand(m)
        verdict = m.judge(old, plan, percent=25)
        PROBE.check("M6", "percent=25: would_remove пуст", as_paths(verdict.would_remove) == set(),
                    f"would_remove {as_paths(verdict.would_remove)}")
        PROBE.check("M6", "percent=25: три кандидата отложены «порог»",
                    {p: r for p, r in dict(verdict.deferred).items() if p in extra}
                    == {p: "порог" for p in extra}, f"deferred {dict(verdict.deferred)}")
        PROBE.check("M6", "percent=25: три строки лишнего в new_body",
                    extra <= set(as_body(verdict.new_body)), f"тело {sorted(as_body(verdict.new_body))}")
        text = summary(verdict)
        PROBE.check("M6", "сводка «порог: снять 3 из 8 (37.5 %) больше LAYOUT_REMOVE_MAX_PERCENT=25 % — отложено», «отложено 3 (порог 3)», «снято 0»",
                    "порог: снять 3 из 8 (37.5 %) больше LAYOUT_REMOVE_MAX_PERCENT=25 % — отложено" in text
                    and "отложено 3 (порог 3)" in text and "снято 0;" in text
                    and "снял бы" not in text, text)
        for percent in (50, 37.5):
            verdict = m.judge(old, plan, percent=percent)
            text = summary(verdict, removed=as_paths(verdict.would_remove))
            PROBE.check("M6", f"percent={percent}: снимается 3 (порог строго «больше»)",
                        as_paths(verdict.would_remove) == extra and "снято 3: " in text
                        and "порог:" not in text,
                        f"would_remove {as_paths(verdict.would_remove)}; {text}")


def case_m7() -> None:
    """Ключа порога нет (percent None) — снятие ОТЛОЖЕНО, не «снимать всё» (решение смены 3);
    ключ «не число» (сырое значение строкой, решение а) — отложено своей причиной; не бросает.
    Зелен на поломке? «None = снимать» (Ф-б) — нет: would_remove обязан быть пуст;
    «не число = не задан» — нет: причина обязана начинаться «порог не число»."""
    with Mini() as m:
        old, extra, plan = threshold_stand(m)
        try:
            verdict = m.judge(old, plan, percent=None)
        except Exception as error:      # the case is exactly «it must not raise»
            PROBE.check("M7", "percent=None не бросает", False, f"{type(error).__name__}: {error}")
            return
        PROBE.check("M7", "percent=None: would_remove пуст",
                    as_paths(verdict.would_remove) == set(), f"would_remove {as_paths(verdict.would_remove)}")
        PROBE.check("M7", "percent=None: три кандидата отложены «ключ не задан»",
                    {p: r for p, r in dict(verdict.deferred).items() if p in extra}
                    == {p: "ключ не задан" for p in extra}, f"deferred {dict(verdict.deferred)}")
        PROBE.check("M7", "percent=None: три строки в new_body и в body_after(set())",
                    extra <= set(as_body(verdict.new_body))
                    and extra <= set(as_body(verdict.body_after(set()))),
                    f"тело {sorted(as_body(verdict.new_body))}")
        text = summary(verdict)
        PROBE.check("M7", "сводка «ключ LAYOUT_REMOVE_MAX_PERCENT не задан — отложено 3», «отложено 3 (ключ не задан 3)», «снято 0»",
                    "ключ LAYOUT_REMOVE_MAX_PERCENT не задан — отложено 3" in text
                    and "отложено 3 (ключ не задан 3)" in text and "снято 0;" in text
                    and "в Ф-в такое" not in text and "снял бы" not in text, text)
        try:
            verdict = m.judge(old, plan, percent="abc")
        except Exception as error:      # the case is exactly «it must not raise»
            PROBE.check("M7", "percent='abc' не бросает", False, f"{type(error).__name__}: {error}")
            return
        reasons = {p: r for p, r in dict(verdict.deferred).items() if p in extra}
        PROBE.check("M7", "percent='abc': would_remove пуст, три отложены причиной «порог не число…»",
                    as_paths(verdict.would_remove) == set() and set(reasons) == extra
                    and all(isinstance(r, str) and r.startswith("порог не число") for r in reasons.values()),
                    f"would_remove {as_paths(verdict.would_remove)}; deferred {dict(verdict.deferred)}")
        text = summary(verdict)
        PROBE.check("M7", "percent='abc': сводка называет значение «abc», три строки в теле",
                    "abc" in text and "отложено 3" in text and extra <= set(as_body(verdict.new_body)),
                    text)


TIER1 = ("STATE.md", "память/x.md", "harness/config/pipeline.yaml",
         "harness/vozmozhnosti/a/.установлено", "scripts/.x.layout-tmp", "docs/a.md",
         ".layout-manifest")
TIER2 = (".claude/settings.json", "scripts/hooks/guard_bash.py")
# Пути, которые в опись записать нельзя вовсе (listable): только они — порча описи.
UNLISTABLE = ("/abs/x.sh", "scripts/../x.sh")
OUT_OF_SCOPE_LINE = "из описи вынут (не дело раскладки), файл не тронут: "


def case_m8() -> None:
    """Запретный список в прежней описи: ярус 1 — «из описи вынут (не дело раскладки), файл
    не тронут», вне тела, НЕ «опись испорчена»: такую строку пишет прежняя версия пакета, когда
    путь был нашим (живой случай: harness/config/gates-quarantine.yaml после 04abc9d). Порча —
    только путь, который в опись не записать (UNLISTABLE). Ярус 2 — «не снимается», в теле.
    Файлы лежат с blob описи — без запретного все были бы кандидатами (percent=100: при ключе
    «не задан» Ф-в откладывает всё, и проверка «не кандидат» ослепла бы).
    Зелен на поломке? «ярус 2 → ярус 1» — нет: хук и settings обязаны остаться в new_body."""
    with Mini() as m:
        rows = [(m.put(p), "scripts", p) for p in TIER1 + TIER2]
        rows += [("e69de29bb2d1d6434b8b29ae775ad8c2e48c5391", "scripts", p) for p in UNLISTABLE]
        old = m.old(rows)
        verdict = m.judge(old, [], percent=100)
        text = summary_any(verdict)
        body = as_body(verdict.new_body)
        removed = as_paths(verdict.would_remove)
        for path in TIER1:
            PROBE.check("M8", f"ярус 1 {path}: «из описи вынут…файл не тронут», не «испорчена», "
                        "не в теле, в out_of_scope",
                        OUT_OF_SCOPE_LINE + path in text and f"опись испорчена: {path}" not in text
                        and path not in body
                        and path in as_paths(getattr(verdict, "out_of_scope", set())),
                        f"тело {path in body}; broken {as_paths(verdict.broken)}; {text}")
        for path in UNLISTABLE:
            PROBE.check("M8", f"не записываемый путь {path}: «опись испорчена», не в теле, в broken",
                        f"опись испорчена: {path}" in text and path not in body
                        and path in as_paths(verdict.broken),
                        f"тело {path in body}; broken {as_paths(verdict.broken)}; {text}")
        for path in TIER2:
            PROBE.check("M8", f"ярус 2 {path}: «не снимается», в теле, в not_removable",
                        f"не снимается: {path}" in text and path in body
                        and path in as_paths(verdict.not_removable),
                        f"тело {path in body}; not_removable {as_paths(verdict.not_removable)}; {text}")
        PROBE.check("M8", "ни один из девяти не в would_remove", not removed & set(TIER1 + TIER2),
                    f"would_remove {removed}")
        tiers = {p: LM.forbidden_tier(p) for p in TIER1 + TIER2 + ("scripts/deploy.sh",)}
        PROBE.check("M8", "forbidden_tier: ярус 1 → 1, ярус 2 → 2, scripts/deploy.sh → 0",
                    all(tiers[p] == 1 for p in TIER1) and all(tiers[p] == 2 for p in TIER2)
                    and tiers["scripts/deploy.sh"] == 0, f"{tiers}")


def state_case(name: str, setup, expect_kept: str | None) -> None:
    """Один путь лишнего в своём стенде: kept с причиной (или gone), не в would_remove."""
    with Mini() as m:
        rel, row_blob, guard = setup(m)
        before = {p: p.read_bytes() for p in m.t.rglob("*") if p.is_file() and not p.is_symlink()
                  and os.access(p, os.R_OK)}
        keep_b = m.put("scripts/keep.sh")
        old = m.old([(row_blob, "scripts", rel), (keep_b, "scripts", "scripts/keep.sh")])
        verdict = m.judge(old, [("scripts/keep.sh", keep_b, "scripts")], percent=100)
        PROBE.check("M9", f"{name}: не в would_remove", rel not in as_paths(verdict.would_remove),
                    f"would_remove {as_paths(verdict.would_remove)}")
        kept = as_body(verdict.kept)
        if expect_kept is not None:
            PROBE.check("M9", f"{name}: kept «{expect_kept}»",
                        kept.get(rel, (None, None))[1] == expect_kept, f"kept {kept}")
            PROBE.check("M9", f"{name}: в new_kept, не в new_body",
                        rel in as_body(verdict.new_kept) and rel not in as_body(verdict.new_body),
                        f"new_kept {sorted(as_body(verdict.new_kept))}")
        else:
            text = summary_any(verdict)
            PROBE.check("M9", f"{name}: gone и «уже не было: {rel}»",
                        rel in as_paths(verdict.gone) and f"уже не было: {rel}" in text,
                        f"gone {as_paths(verdict.gone)}; {text}")
            PROBE.check("M9", f"{name}: ни в new_body, ни в new_kept",
                        rel not in as_body(verdict.new_body) and rel not in as_body(verdict.new_kept),
                        "путь остался")
        after = {p: p.read_bytes() for p in before}
        PROBE.check("M9", f"{name}: байты стенда (и целей ссылок) прежние", before == after,
                    "изменились")
        if guard:
            guard()


def case_m9() -> None:
    """Состояния лишнего: правлен, ссылка, каталог, за проектом, нет. Каждое — свой стенд.
    Зелен на поломке? «realpath родителя не судится» — нет: blob цели равен строке описи,
    и путь стал бы кандидатом (percent=100, иначе Ф-в откладывает всё); цель без права чтения
    вдобавок ловит чтение через ссылку."""
    def edited(m):
        old_b = blob_of_bytes("прежнее\n".encode())
        m.put("scripts/a.sh", "правлено здесь\n")
        return "scripts/a.sh", old_b, None

    def link(m):
        target = m.t / "цели" / "b.txt"
        target.parent.mkdir()
        target.write_text("цель ссылки\n", encoding="utf-8")
        (m.proj / "scripts").mkdir(exist_ok=True)
        (m.proj / "scripts/b.sh").symlink_to(target)
        return "scripts/b.sh", blob_of(target), None

    def directory(m):
        (m.proj / "scripts/c.sh").mkdir(parents=True)
        return "scripts/c.sh", blob_of_bytes(b"x\n"), None

    def outside(m):
        far = m.t / "вне"
        far.mkdir()
        (far / "d.sh").write_text("за проектом\n", encoding="utf-8")
        row_blob = blob_of(far / "d.sh")
        os.chmod(far / "d.sh", 0o000)
        (m.proj / "harness").mkdir()
        (m.proj / "harness/вне").symlink_to(far)
        return "harness/вне/d.sh", row_blob, lambda: os.chmod(far / "d.sh", 0o600)

    def missing(m):
        return "scripts/e.sh", blob_of_bytes(b"e\n"), None

    for name, setup, expect in (("а) правлен", edited, "правлен здесь"),
                                ("б) ссылка", link, "ссылка"),
                                ("в) каталог", directory, "не файл"),
                                ("г) за проектом", outside, "за проектом"),
                                ("д) нет файла", missing, None)):
        try:
            state_case(name, setup, expect)
        except Exception as error:
            PROBE.crashed(f"M9 {name}", error)


def case_m10() -> None:
    """«Оставлено» держится; путь вернулся в план — ушёл в тело; файла нет — ушёл и назван.
    Зелен на поломке? «секция не переносится» — нет: первый стенд требует путь в new_kept."""
    reason = "правлен здесь"
    with Mini() as m:
        disk_b = m.put("scripts/x.sh", "правлено\n")
        keep_b = m.put("scripts/keep.sh")
        old = m.old([(keep_b, "scripts", "scripts/keep.sh")], kept=[("e" * 40, reason, "scripts/x.sh")])
        verdict = m.judge(old, [("scripts/keep.sh", keep_b, "scripts")])
        PROBE.check("M10", "файл на месте: в new_kept с прежними blob и причиной",
                    as_body(verdict.new_kept).get("scripts/x.sh") == ("e" * 40, reason),
                    f"new_kept {as_body(verdict.new_kept)}")
        PROBE.check("M10", "сводка «оставлено 1: scripts/x.sh»",
                    "оставлено 1: scripts/x.sh" in summary_any(verdict), summary_any(verdict))
        verdict = m.judge(old, [("scripts/keep.sh", keep_b, "scripts"),
                                ("scripts/x.sh", disk_b, "scripts")])
        PROBE.check("M10", "путь вернулся в план: ушёл из секции, в теле",
                    "scripts/x.sh" not in as_body(verdict.new_kept)
                    and as_body(verdict.new_body).get("scripts/x.sh") == (disk_b, "scripts"),
                    f"new_kept {as_body(verdict.new_kept)}")
        (m.proj / "scripts/x.sh").unlink()
        verdict = m.judge(old, [("scripts/keep.sh", keep_b, "scripts")])
        PROBE.check("M10", "файла нет: ушёл из секции и назван «уже не было: scripts/x.sh»",
                    "scripts/x.sh" not in as_body(verdict.new_kept)
                    and "уже не было: scripts/x.sh" in summary_any(verdict),
                    f"new_kept {as_body(verdict.new_kept)}; {summary_any(verdict)}")


def case_m11() -> None:
    """listable: таб, перевод строки, «..», ведущий «/», пустой — причина; годный — None.
    Имена LIVE_SETTINGS и harness/skills — ярус 1 (дверь отсеивает их из плана).
    Зелен на поломке? «listable всегда причина» — нет: пара scripts/ok.sh обязана дать None."""
    for bad in ("scripts/a\tb.sh", "scripts/a\nb.sh", "scripts/../b.sh", "/abs/x.sh", ""):
        reason = LM.listable(bad)
        PROBE.check("M11", f"listable({bad!r}) — причина",
                    isinstance(reason, str) and reason.strip() != "", f"дал {reason!r}")
    PROBE.check("M11", "пара: listable('scripts/ok.sh') is None",
                LM.listable("scripts/ok.sh") is None, f"дал {LM.listable('scripts/ok.sh')!r}")
    for name in LIVE_SETTINGS:
        path = f"harness/config/{name}"
        PROBE.check("M11", f"{path} — ярус 1 (в опись не идёт)", LM.forbidden_tier(path) == 1,
                    f"ярус {LM.forbidden_tier(path)!r}")
    PROBE.check("M11", "harness/skills/x/SKILL.md — ярус 1 (группа skills вне описи)",
                LM.forbidden_tier("harness/skills/x/SKILL.md") == 1,
                f"ярус {LM.forbidden_tier('harness/skills/x/SKILL.md')!r}")
    PROBE.check("M11", "пара: harness/config/x.yaml — не запретный (0)",
                LM.forbidden_tier("harness/config/x.yaml") == 0,
                f"ярус {LM.forbidden_tier('harness/config/x.yaml')!r}")
    # Раскладчик кладёт определения помощников в .claude/agents (группа agents):
    # вне корней описи они бы не снимались, когда пакет их убрал.
    PROBE.check("M11", ".claude/agents/harness-worker.md — в описи (0)",
                LM.forbidden_tier(".claude/agents/harness-worker.md") == 0,
                f"ярус {LM.forbidden_tier('.claude/agents/harness-worker.md')!r}")


def live_paths() -> tuple:
    tree = ast.parse(COMPARE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ЖИВЫЕ"
                                                for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    return ()


def case_m12() -> None:
    """Каждый путь ЖИВЫЕ сверки с пакетом (кроме UNIFIED/) — ярус 1 запретного.
    Зелен на поломке? «пустой кортеж» — нет: пустой ЖИВЫЕ — провал пробы, не зелень."""
    paths = live_paths()
    PROBE.check("M12", f"ЖИВЫЕ из {COMPARE_PATH.name} непуст", bool(paths), "кортеж пуст или не найден")
    for path in paths:
        if path == "UNIFIED/":
            continue
        probe_path = path + "x.md" if path.endswith("/") else path
        tier = LM.forbidden_tier(probe_path)
        PROBE.check("M12", f"{probe_path} — ярус 1", tier == 1, f"ярус {tier!r}")


def case_m13() -> None:
    """lift=True (дверь --lift-threshold): порог и отсутствие ключа не откладывают — снимается всё;
    группа без источника отложена и при lift (дверь снимает порог, не предохранитель группы).
    Зелен на поломке? «lift не снимает порог» — нет: при percent=25 would_remove обязан быть 3;
    «lift снимает и группу» — нет: UNIFIED/ обязаны остаться отложенными."""
    with Mini() as m:
        old, extra, plan = threshold_stand(m)
        for percent in (25, None):
            verdict = m.judge(old, plan, percent=percent, lift=True)
            PROBE.check("M13", f"lift, percent={percent}: would_remove — все 3",
                        as_paths(verdict.would_remove) == extra,
                        f"would_remove {as_paths(verdict.would_remove)}")
            reasons = set(dict(verdict.deferred).values())
            PROBE.check("M13", f"lift, percent={percent}: в deferred нет «порог» и «ключ не задан»",
                        not reasons & {"порог", "ключ не задан"}, f"deferred {dict(verdict.deferred)}")
            text = summary(verdict, removed=extra)
            PROBE.check("M13", f"lift, percent={percent}: сводка «снято 3», без строки «порог:»",
                        "снято 3: " in text and "порог:" not in text
                        and "не задан — отложено" not in text, text)
        verdict = m.judge(old, plan, percent=25)
        PROBE.check("M13", "пара без lift при percent=25: would_remove пуст (стенд порог задевает)",
                    as_paths(verdict.would_remove) == set(), f"would_remove {as_paths(verdict.would_remove)}")
    with Mini() as m:
        old, rows, plan = unified_stand(m)
        verdict = m.judge(old, plan, absent=["UNIFIED/"], percent=None, lift=True)
        group = {p for _b, _g, p in rows}
        PROBE.check("M13", "lift + группа без источника: UNIFIED/ не кандидаты, отложены «группа без источника»",
                    not as_paths(verdict.would_remove) & group
                    and all(dict(verdict.deferred).get(p) == "группа без источника" for p in group),
                    f"would_remove {as_paths(verdict.would_remove)}; deferred {dict(verdict.deferred)}")


def case_m14() -> None:
    """Цепочка каталогов: ссылка-каталог ВНУТРИ проекта (scripts/sub → real) — «ссылка», не кандидат:
    rename scripts/sub/x.sh унёс бы scripts/real/x.sh — не тот путь, что в описи (расхождение 6).
    Пара — настоящий каталог → кандидат; проект, открытый через ссылку, своих файлов не теряет.
    Зелен на поломке? «realpath родителя внутри проекта» (Ф-б) — нет: sub → real внутри и прошёл бы."""
    with Mini() as m:
        real_b = m.put("scripts/real/x.sh")
        (m.proj / "scripts/sub").symlink_to("real")
        keep_b = m.put("scripts/keep.sh")
        old = m.old([(real_b, "scripts", "scripts/sub/x.sh"), (keep_b, "scripts", "scripts/keep.sh")])
        verdict = m.judge(old, [("scripts/keep.sh", keep_b, "scripts")], percent=100)
        PROBE.check("M14", "scripts/sub → real: scripts/sub/x.sh не кандидат",
                    "scripts/sub/x.sh" not in as_paths(verdict.would_remove),
                    f"would_remove {as_paths(verdict.would_remove)}")
        PROBE.check("M14", "scripts/sub → real: kept «ссылка»",
                    as_body(verdict.kept).get("scripts/sub/x.sh", (None, None))[1] == "ссылка",
                    f"kept {as_body(verdict.kept)}")
        PROBE.check("M14", "цель scripts/real/x.sh цела", blob_of(m.proj / "scripts/real/x.sh") == real_b,
                    "изменилась или пропала")
    with Mini() as m:
        sub_b = m.put("scripts/sub/x.sh")
        keep_b = m.put("scripts/keep.sh")
        old = m.old([(sub_b, "scripts", "scripts/sub/x.sh"), (keep_b, "scripts", "scripts/keep.sh")])
        verdict = m.judge(old, [("scripts/keep.sh", keep_b, "scripts")], percent=100)
        PROBE.check("M14", "пара: scripts/sub — настоящий каталог → кандидат",
                    as_paths(verdict.would_remove) == {"scripts/sub/x.sh"},
                    f"would_remove {as_paths(verdict.would_remove)}; kept {as_body(verdict.kept)}")
        alias = m.t / "проект-ссылка"
        alias.symlink_to(m.proj)
        verdict = m.judge(old, [("scripts/keep.sh", keep_b, "scripts")], percent=100, project=alias)
        PROBE.check("M14", "проект открыт через ссылку: scripts/sub/x.sh — кандидат, не «за проектом»",
                    as_paths(verdict.would_remove) == {"scripts/sub/x.sh"},
                    f"would_remove {as_paths(verdict.would_remove)}; kept {as_body(verdict.kept)}")


def case_m15() -> None:
    """Сводка по факту (решение смены 4): summary_lines(removed, overwritten) — одна строка «опись:»,
    нули печатаются, «снял бы» нет нигде; отдельные строки Ф-б («уже не было: p») остаются (Р5).
    Зелен на поломке? «сводка из would_remove, а не из removed» — нет: при removed=∅ обязано быть «снято 0»."""
    with Mini() as m:
        old_b, keep_b = m.put("scripts/old.sh"), m.put("scripts/keep.sh")
        old = m.old([(old_b, "scripts", "scripts/old.sh"), (keep_b, "scripts", "scripts/keep.sh")])
        verdict = m.judge(old, [("scripts/keep.sh", keep_b, "scripts")], percent=100)
        lines = verdict.summary_lines({"scripts/old.sh"}, 2)
        head = [line for line in lines if line.startswith("опись:")]
        want = ("опись: в описи 2; снято 1: scripts/old.sh; перекрыто 2; отложено 0; "
                "оставлено 0; уже не было 0")
        PROBE.check("M15", f"removed={{old.sh}}, overwritten=2 → ровно «{want}»",
                    head == [want], f"строки {lines}")
        lines = verdict.summary_lines(set(), 0)
        head = [line for line in lines if line.startswith("опись:")]
        want = "опись: в описи 2; снято 0; перекрыто 0; отложено 0; оставлено 0; уже не было 0"
        PROBE.check("M15", f"removed=∅ (кандидат не снят) → ровно «{want}»", head == [want],
                    f"строки {lines}")
        PROBE.check("M15", "нигде подстроки «снял бы» и «в Ф-в такое»",
                    not any("снял бы" in line or "в Ф-в такое" in line for line in lines), f"{lines}")
    with Mini() as m:
        old_b, keep_b = m.put("scripts/old.sh"), m.put("scripts/keep.sh")
        old = m.old([(old_b, "scripts", "scripts/old.sh"), (keep_b, "scripts", "scripts/keep.sh"),
                     (blob_of_bytes(b"gone\n"), "scripts", "scripts/gone.sh")])
        verdict = m.judge(old, [("scripts/keep.sh", keep_b, "scripts")], percent=100)
        lines = verdict.summary_lines({"scripts/old.sh"}, 0)
        PROBE.check("M15", "файла нет: «уже не было 1» в строке «опись:» И отдельная «уже не было: scripts/gone.sh»",
                    any(line.startswith("опись:") and line.endswith("; уже не было 1") for line in lines)
                    and "уже не было: scripts/gone.sh" in lines, f"{lines}")


def case_m16() -> None:
    """body_after(removed): тело без ФАКТИЧЕСКИ снятых; отложенные и ярус 2 на месте;
    body_after(∅) == new_body — обрыв до снятия не теряет ни строки.
    Зелен на поломке? «body_after выкидывает would_remove целиком» — нет: при ∅ old.sh обязан остаться."""
    with Mini() as m:
        old_b, keep_b = m.put("scripts/old.sh"), m.put("scripts/keep.sh")
        uni_b, hook_b = m.put("UNIFIED/01.md"), m.put("scripts/hooks/h.sh")
        old = m.old([(old_b, "scripts", "scripts/old.sh"), (keep_b, "scripts", "scripts/keep.sh"),
                     (uni_b, "unified", "UNIFIED/01.md"), (hook_b, "scripts", "scripts/hooks/h.sh")])
        verdict = m.judge(old, [("scripts/keep.sh", keep_b, "scripts")], absent=["UNIFIED/"],
                          percent=100)
        before = as_body(verdict.new_body)
        after = as_body(verdict.body_after({"scripts/old.sh"}))
        PROBE.check("M16", "body_after({old.sh}): old.sh нет",
                    "scripts/old.sh" not in after, f"тело {sorted(after)}")
        PROBE.check("M16", "body_after({old.sh}): keep.sh, отложенный UNIFIED/01.md, ярус 2 hooks/h.sh — с прежними полями",
                    after.get("scripts/keep.sh") == (keep_b, "scripts")
                    and after.get("UNIFIED/01.md") == (uni_b, "unified")
                    and after.get("scripts/hooks/h.sh") == (hook_b, "scripts"),
                    f"тело {after}")
        PROBE.check("M16", "body_after(∅) == new_body", as_body(verdict.body_after(set())) == before,
                    f"{as_body(verdict.body_after(set()))} против {before}")
        PROBE.check("M16", "new_body не испорчен вызовом body_after (old.sh на месте)",
                    as_body(verdict.new_body) == before, f"тело {sorted(as_body(verdict.new_body))}")


def case_m17() -> None:
    """R1-11: строки «оставлено» перепроверяются на диске. Правку откатили (blob снова = строке) →
    кандидат; всё ещё правлен → kept прежней причиной; стал ссылкой → kept НОВОЙ причиной.
    Решение (б): кандидат из «оставлено» при отложенном снятии остаётся в «оставлено» с прежней
    причиной; знаменатель порога — тело + «оставлено».
    Зелен на поломке? «kept переносится без суда диска» (Ф-б) — нет: откаченный обязан стать кандидатом;
    «знаменатель — только тело» — нет: при 40 % доля 3/6 = 50 % отложила бы снятие."""
    with Mini() as m:
        disk_b = m.put("scripts/x.sh", "вернули как в пакете\n")
        keep_b = m.put("scripts/keep.sh")
        old = m.old([(keep_b, "scripts", "scripts/keep.sh")],
                    kept=[(disk_b, "правлен здесь", "scripts/x.sh")])
        plan = [("scripts/keep.sh", keep_b, "scripts")]
        verdict = m.judge(old, plan, percent=100)
        PROBE.check("M17", "откаченная правка: scripts/x.sh — кандидат, не в new_kept",
                    "scripts/x.sh" in as_paths(verdict.would_remove)
                    and "scripts/x.sh" not in as_body(verdict.new_kept),
                    f"would_remove {as_paths(verdict.would_remove)}; new_kept {as_body(verdict.new_kept)}")
        text = summary(verdict, removed={"scripts/x.sh"})
        PROBE.check("M17", "сводка «снято 1: scripts/x.sh», «оставлено 0»",
                    "снято 1: scripts/x.sh" in text and "оставлено 0" in text, text)
        verdict = m.judge(old, plan, percent=None)
        PROBE.check("M17", "(б) ключа нет: откаченный не снимается, остаётся в «оставлено» прежней причиной",
                    "scripts/x.sh" not in as_paths(verdict.would_remove)
                    and as_body(verdict.new_kept).get("scripts/x.sh") == (disk_b, "правлен здесь")
                    and "scripts/x.sh" not in as_body(verdict.body_after(set())),
                    f"would_remove {as_paths(verdict.would_remove)}; new_kept {as_body(verdict.new_kept)}")
        m.put("scripts/x.sh", "снова правлено здесь\n")
        verdict = m.judge(old, plan, percent=100)
        PROBE.check("M17", "пара: правка на месте — kept «правлен здесь» с blob строки, не кандидат",
                    "scripts/x.sh" not in as_paths(verdict.would_remove)
                    and as_body(verdict.new_kept).get("scripts/x.sh") == (disk_b, "правлен здесь"),
                    f"would_remove {as_paths(verdict.would_remove)}; new_kept {as_body(verdict.new_kept)}")
        (m.proj / "scripts/x.sh").unlink()
        target = m.t / "цель.sh"
        target.write_text("вернули как в пакете\n", encoding="utf-8")
        (m.proj / "scripts/x.sh").symlink_to(target)
        verdict = m.judge(old, plan, percent=100)
        PROBE.check("M17", "стал ссылкой (цель с blob строки): kept с НОВОЙ причиной «ссылка», не кандидат",
                    "scripts/x.sh" not in as_paths(verdict.would_remove)
                    and as_body(verdict.new_kept).get("scripts/x.sh", (None, None))[1] == "ссылка",
                    f"would_remove {as_paths(verdict.would_remove)}; new_kept {as_body(verdict.new_kept)}")
    with Mini() as m:
        file_b = m.put("scripts/y.sh")
        keep_b = m.put("scripts/keep.sh")
        old = m.old([(keep_b, "scripts", "scripts/keep.sh")], kept=[(file_b, "ссылка", "scripts/y.sh")])
        verdict = m.judge(old, [("scripts/keep.sh", keep_b, "scripts")], percent=100)
        PROBE.check("M17", "строка «ссылка», на диске обычный файл с blob строки → кандидат",
                    "scripts/y.sh" in as_paths(verdict.would_remove),
                    f"would_remove {as_paths(verdict.would_remove)}; new_kept {as_body(verdict.new_kept)}")
    with Mini() as m:
        rows = [(m.put(f"scripts/s{i}.sh"), "scripts", f"scripts/s{i}.sh") for i in range(6)]
        kept = [(blob_of_bytes(f"было {i}\n".encode()), "правлен здесь", f"scripts/k{i}.sh") for i in range(2)]
        for i in range(2):
            m.put(f"scripts/k{i}.sh", f"правлено {i}\n")
        old = m.old(rows, kept=kept)
        extra = {p for _b, _g, p in rows[:3]}
        plan = [(p, b, g) for b, g, p in rows[3:]]
        verdict = m.judge(old, plan, percent=40)
        PROBE.check("M17", "(б) знаменатель тело+оставлено: 3 из 8 = 37.5 % ≤ 40 → снимается 3",
                    as_paths(verdict.would_remove) == extra,
                    f"would_remove {as_paths(verdict.would_remove)}; deferred {dict(verdict.deferred)}")
        verdict = m.judge(old, plan, percent=25)
        text = summary(verdict)
        PROBE.check("M17", "(б) порог 25: «порог: снять 3 из 8 (37.5 %)»",
                    "порог: снять 3 из 8 (37.5 %)" in text, text)


def case_m18() -> None:
    """R1-03: kept_after(skipped) — кандидат из «оставлено», которого дверь НЕ сняла, возвращается
    в «оставлено» с причиной перепроверки двери; снятый — нет; «уже не было» строки не даёт.
    Зелен на поломке? «kept_after отдаёт new_kept как есть» — нет: при skipped «ссылка» путь обязан
    быть в kept с причиной «ссылка», а не пропасть и не остаться прежним «правлен здесь»."""
    with Mini() as m:
        disk_b = m.put("scripts/x.sh", "вернули как в пакете\n")
        keep_b = m.put("scripts/keep.sh")
        old = m.old([(keep_b, "scripts", "scripts/keep.sh")],
                    kept=[(disk_b, "правлен здесь", "scripts/x.sh")])
        verdict = m.judge(old, [("scripts/keep.sh", keep_b, "scripts")], percent=100)
        PROBE.check("M18", "стенд: откаченный из «оставлено» — кандидат, в new_kept его нет",
                    as_paths(verdict.would_remove) == {"scripts/x.sh"}
                    and "scripts/x.sh" not in as_body(verdict.new_kept),
                    f"would_remove {as_paths(verdict.would_remove)}; new_kept {as_body(verdict.new_kept)}")
        for reason in ("правлен здесь", "ссылка"):
            kept = as_body(verdict.kept_after({"scripts/x.sh": reason}))
            PROBE.check("M18", f"дверь не сняла («{reason}») → kept_after: x.sh с blob строки и причиной «{reason}»",
                        kept.get("scripts/x.sh") == (disk_b, reason), f"kept_after {kept}")
        kept = as_body(verdict.kept_after({}))
        PROBE.check("M18", "пара: снят (skipped пуст) → kept_after без x.sh",
                    "scripts/x.sh" not in kept, f"kept_after {kept}")
        kept = as_body(verdict.kept_after({"scripts/x.sh": "уже не было"}))
        PROBE.check("M18", "пара: «уже не было» вне словаря «оставлено» → строки нет",
                    "scripts/x.sh" not in kept, f"kept_after {kept}")


def case_m19() -> None:
    """R1-06: довод снимает ТОЛЬКО отложенное прежним прогоном — секцию «# отложено» описи
    (lift_only=old.deferred); новый кандидат сверх порога по-прежнему отложен «порог».
    Зелен на поломке? «lift_only не судится, довод снимает всех кандидатов» — нет: s1, s2 вне
    секции обязаны остаться отложенными, would_remove — ровно {s0}."""
    with Mini() as m:
        rows = [(m.put(f"scripts/s{i}.sh"), "scripts", f"scripts/s{i}.sh") for i in range(8)]
        old = LM.parse(manifest_text(rows) + "# отложено\nscripts/s0.sh\n")
        if isinstance(old, LM.ManifestBroken):
            raise RuntimeError(f"стенд: опись с «# отложено» разобрана как испорченная: {old.reason}")
        PROBE.check("M19", "стенд: секция «# отложено» разобрана в deferred == {s0}",
                    set(old.deferred) == {"scripts/s0.sh"}, f"deferred {set(old.deferred)}")
        plan = {(p, b, g) for b, g, p in rows[3:]}
        verdict = LM.judge_extra(old, plan, [], str(m.proj), 25, lift=True, lift_only=old.deferred)
        PROBE.check("M19", "lift_only={s0}, порог 25: снимается ровно s0",
                    as_paths(verdict.would_remove) == {"scripts/s0.sh"},
                    f"would_remove {as_paths(verdict.would_remove)}; deferred {dict(verdict.deferred)}")
        PROBE.check("M19", "s1, s2 вне секции отложены «порог», deferred_after() == {s1, s2}",
                    dict(verdict.deferred) == {"scripts/s1.sh": "порог", "scripts/s2.sh": "порог"}
                    and verdict.deferred_after() == {"scripts/s1.sh", "scripts/s2.sh"},
                    f"deferred {dict(verdict.deferred)}; deferred_after {verdict.deferred_after()}")
        verdict = LM.judge_extra(old, plan, [], str(m.proj), 25, lift=True, lift_only=None)
        PROBE.check("M19", "пара: lift_only=None — снимаются все три",
                    as_paths(verdict.would_remove) == {f"scripts/s{i}.sh" for i in range(3)},
                    f"would_remove {as_paths(verdict.would_remove)}")


BAD_KEYS = ("-5", "inf", "nan", "abc")


def case_m20() -> None:
    """R1-07: ключ порога «-5», «inf», «nan», «abc» (сырое значение строкой) — снятие отложено
    причиной «порог не число: X», а не порогом и не разрешено.
    Зелен на поломке? «float() прошёл — значит число» — нет: «inf» и «nan» тогда снимают всё
    (share > inf ложно), «-5» откладывает причиной «порог», а не «порог не число»."""
    with Mini() as m:
        old, extra, plan = threshold_stand(m)
        for key in BAD_KEYS:
            verdict = m.judge(old, plan, percent=key)
            reasons = {p: r for p, r in dict(verdict.deferred).items() if p in extra}
            PROBE.check("M20", f"percent={key!r}: would_remove пуст, три отложены «порог не число: {key}»",
                        as_paths(verdict.would_remove) == set()
                        and reasons == {p: f"порог не число: {key}" for p in extra},
                        f"would_remove {as_paths(verdict.would_remove)}; deferred {dict(verdict.deferred)}")
        verdict = m.judge(old, plan, percent="50")
        PROBE.check("M20", "пара: percent='50' — число, снимается 3",
                    as_paths(verdict.would_remove) == extra, f"would_remove {as_paths(verdict.would_remove)}")


def case_m21() -> None:
    """R1-07: строка «ключ LAYOUT_REMOVE_MAX_PERCENT не число: X» печатается модулем РОВНО одна;
    при годном ключе и при «не задан» её нет.
    Зелен на поломке? «строку не печатает никто» — нет: M7 видит «abc» в причине отложенного,
    а здесь судится сама строка и её число."""
    with Mini() as m:
        old, _extra, plan = threshold_stand(m)
        for key in BAD_KEYS:
            lines = m.judge(old, plan, percent=key).summary_lines(set(), 0)
            want = f"ключ LAYOUT_REMOVE_MAX_PERCENT не число: {key}"
            PROBE.check("M21", f"percent={key!r}: ровно одна строка «{want}»",
                        lines.count(want) == 1
                        and sum(1 for line in lines if "не число:" in line and line.startswith("ключ ")) == 1,
                        f"строки {lines}")
        for key in ("25", None):
            lines = m.judge(old, plan, percent=key).summary_lines(set(), 0)
            PROBE.check("M21", f"пара: percent={key!r} — строки «… не число» нет",
                        not any("не число" in line for line in lines), f"строки {lines}")


CASES = (case_m1, case_m2, case_m3, case_m4, case_m5, case_m6, case_m7, case_m8, case_m9,
         case_m10, case_m11, case_m12, case_m13, case_m14, case_m15, case_m16, case_m17,
         case_m18, case_m19, case_m20, case_m21)


def main() -> int:
    global LM
    try:
        LM = load_module()
    except Exception as error:      # a broken module must show as red per case, not a trace
        print(f"модуль {MODULE_PATH} не загрузился: {type(error).__name__}: {error}")
        LM = None
    print(f"модуль: {MODULE_PATH}" + ("" if LM is not None else " — НЕТ, каждый случай красный"))
    for case in CASES:
        label = case.__name__.removeprefix("case_").upper()
        if LM is None:
            # M12 судит кортеж ЖИВЫЕ до модуля: без этой ветки он дал бы «ок» без модуля.
            PROBE.crashed(label, None)
            continue
        try:
            case()
        except Exception as error:      # a probe bug must show as red, not as a trace
            PROBE.crashed(label, error)
    print(f"ОПИСЬ РАСКЛАДКИ: путей {PROBE.paths}, неудач {PROBE.failures}")
    return 0 if PROBE.failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
