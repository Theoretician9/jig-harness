#!/usr/bin/env python3
"""План запуска помощников: роль, число исполнителей и части выводит код из данных.

Контракт (флаги, окружение, JSON, журнал, коды выхода) — docstring
scripts/test_launch_plan.py; спека docs/specs/2026-10-29-правила-вложенности-кодом.md.

    python3 scripts/launch-plan.py [--карта F] [--планы D] [--json] <задача>
    python3 scripts/launch-plan.py --нужен-план <задача> [--json]
    python3 scripts/launch-plan.py --проверить-план <план>
    python3 scripts/launch-plan.py --прижить <задача> <план>/<глава>… [--новая-глава "<название>"]
    python3 scripts/launch-plan.py --закрепить <план>
    python3 scripts/launch-plan.py --отметить <ид> верно|ложно [--кто <имя>]
    python3 scripts/launch-plan.py --метрики [--сутки N] [--json | --строка]
Коды: 0 решение, 1 нарушение данных, 2 неверный вызов.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import config                                                # noqa: E402
import events                                                # noqa: E402
import cards                                                 # noqa: E402
import launch_plan_lib as lib                                # noqa: E402
import pipeline                                              # noqa: E402
import hackathon                                             # noqa: E402

SRC = "scripts/launch-plan.py"
JOURNAL = "вложенность"
VALUE_FLAGS = ("--карта", "--планы", "--кто", "--сутки", "--новая-глава")
MODES = ("--нужен-план", "--проверить-план", "--прижить", "--закрепить", "--отметить", "--метрики")
WANTED_ARGS = {"--нужен-план": 1, "--проверить-план": 1, "--прижить": 2, "--закрепить": 1,
               "--отметить": 2, "--метрики": 0, None: 1}


class UsageError(Exception):
    pass


def parse_args(argv: list) -> dict:
    opts: dict = {"mode": None, "pos": [], "json": False, "строка": False}
    it = iter(argv)
    for arg in it:
        if arg in VALUE_FLAGS:
            opts[arg] = next(it, None)
            if opts[arg] is None:
                raise UsageError(f"у {arg} нет значения")
        elif arg in MODES:
            opts["mode"] = arg
        elif arg == "--json":
            opts["json"] = True
        elif arg == "--строка":
            opts["строка"] = True
        elif arg.startswith("--"):
            raise UsageError(f"неизвестный флаг {arg}")
        else:
            opts["pos"].append(arg)
    if len(opts["pos"]) != WANTED_ARGS[opts["mode"]]:
        raise UsageError(f"нужно позиционных аргументов: {WANTED_ARGS[opts['mode']]}")
    return opts


class Ctx:
    """Места и время одного вызова."""

    def __init__(self, opts: dict):
        tree = config.корень_дерева(__file__)
        self.root = Path(os.environ.get("HARNESS_PROJECT_ROOT") or tree)
        self.devmap = Path(opts.get("--карта") or os.environ.get("HARNESS_DEVMAP")
                           or tree / "dev-map.yaml")
        # Лок общий с демонами только у карты дерева; временная карта держит лок рядом с собой.
        self.lock = (cards.lock_path() if self.devmap.resolve() == (tree / "dev-map.yaml").resolve()
                     else self.devmap.parent / "dev-map.lock")
        self.lock_wait = float(os.environ.get("HARNESS_MAP_LOCK_WAIT") or 30)
        self.plans_dir = Path(opts.get("--планы") or os.environ.get("HARNESS_PLANS_DIR")
                              or self.root / "docs" / "plans")
        self.roles_path = Path(os.environ.get("HARNESS_HELPER_ROLES")
                               or tree / "harness" / "config" / "helper-roles.yaml")
        self.pipe_path = Path(os.environ.get("HARNESS_PIPELINE_CONF") or pipeline.КОНФИГ)
        self.conf_path = os.environ.get("HARNESS_CONF") or pipeline.HARNESS_CONF
        self.logs = Path(events.log_dir())
        self.now = lib.now_epoch()

    def cards(self) -> dict:
        try:
            return {c["id"]: c for c in lib.map_cards(self.devmap.read_text(encoding="utf-8"))}
        except (OSError, yaml.YAMLError) as exc:
            raise lib.DataError(f"карта {self.devmap} не читается: {exc}")

    def roles(self) -> dict:
        try:
            data = lib.load_yaml(self.roles_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise lib.Skipped(f"helper-roles.yaml не читается: {exc}")
        roles = data.get("вывод_роли") if isinstance(data, dict) else None
        if not isinstance(roles, dict):
            raise lib.Skipped("в helper-roles.yaml нет блока вывод_роли")
        return roles

    def journal_rows(self, name: str, tail=lib.TAIL_BYTES) -> list:
        return lib.read_rows(self.logs / f"{name}.jsonl", tail)


def emit(task, event: str, outcome: str, data: dict) -> None:
    events.emit(SRC, event, outcome, journal="вложенность", task=task, data=data)


def model_for(kind: str):
    try:
        out = subprocess.run(["bash", str(HERE / "model-for.sh"), kind], capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


# ───────────────────────── собрать ситуацию ─────────────────────────

def gather(ctx: Ctx, task: str, roles: dict) -> dict:
    """Всё, что известно о задаче из данных; DataError — нарушение (код 1)."""
    cards_map = ctx.cards()
    card = cards_map.get(task)
    if card is None:
        raise lib.DataError(f"нет карточки {task} в {ctx.devmap}")
    plans = lib.all_plans(ctx.plans_dir)
    chapter = card.get("глава")
    located = lib.chapter_of(plans, chapter) if chapter else None
    if chapter and not located:
        raise lib.DataError(f"глава {chapter} не найдена (карточка {task})")
    window = float(roles["предохранитель"]["окно_живых"]) * 60
    claim_rows = ctx.journal_rows("заявки-на-файлы")
    claims = lib.fresh(claim_rows, ctx.now, window, "file.claim")
    own = [r["data"]["путь"] for r in claims if r.get("task") == task and (r.get("data") or {}).get("путь")]
    found = lib.find_parts(task, located, plans, ctx.root, own)
    parts = lib.normalize_parts(found["части"], ctx.root)
    structure = lib.analyse_parts(parts)
    level = (card.get("глубина") or {}).get("уровень") or "обычная"
    pipe_conf = pipeline.конфиг(ctx.pipe_path)
    return {"card": card, "cards": cards_map, "plans": plans, "chapter": chapter, "located": located,
            "source": found, "parts": parts, "structure": structure, "claims": claims,
            "claim_rows": claim_rows,
            "level": level, "pipe_conf": pipe_conf,
            "steps": len(lib.steps_of(level, (card.get("глубина") or {}).get("характер"),
                                      pipeline, pipe_conf))}


def complexity(ctx: Ctx, g: dict, roles: dict) -> dict:
    """Сложность: цена кода по файлам, пока файлов нет — оценка карточки."""
    files, trace = g["structure"]["файлов"], []
    cut = roles["порог_резки_сложности"]
    if files:
        value = lib.price_of(files, ctx.root, pipeline, g["pipe_conf"], ctx.conf_path)
        name = lib.level_by_price(value, g["pipe_conf"]["пороги"])
        result = {"источник": "цена", "цена": value, "complexity": None, "режет": value >= cut["цена"],
                  "порог": cut["цена"], "мера": f"цена {value}"}
    else:
        estimate = (g["card"].get("volume") or {}).get("complexity")
        if estimate is None:
            raise lib.Skipped("файлов ещё нет и в карточке нет volume.complexity")
        name = lib.complexity_name(estimate, roles["сложность"]["complexity"])
        trace.append(f"сложность по оценке (volume.complexity {estimate}): файлов ещё нет")
        result = {"источник": "оценка", "цена": None, "complexity": estimate,
                  "режет": estimate >= cut["complexity"], "порог": cut["complexity"],
                  "мера": f"оценка {estimate}"}
    if g["level"] == "крупная" and name == "простая":
        name = "средняя"
        trace.append("уровень «крупная» поднял сложность до средней")
    return {**result, "имя": name, "след": trace}


def closed_plan_trace(g: dict) -> list:
    line = lib.closed_plan_line(g["located"][0], g["cards"]) if g["located"] else None
    return [line] if line else []


def plan_outcome(ctx: Ctx, g: dict, roles: dict) -> tuple:
    """Исход §5: (исход, подсказка, след)."""
    if g["located"]:
        return "часть_плана", [], []
    files, trace = g["structure"]["файлов"], []
    if files:
        hints = lib.hint_chapters(g["plans"], g["cards"], files, ctx.root,
                                  roles["порог_приживления"])
        if hints:
            return "кандидат", hints, trace
    else:
        trace.append("групп: файлов ещё нет, признак групп пропущен")
    need = roles["план_нужен"]
    big = (g["level"] == "крупная" or g["steps"] >= need["шагов"]
           or (bool(files) and g["structure"]["групп"] >= need["групп"]))
    return ("нужен_план" if big else "мелкая"), [], trace


# ───────────────────────── решение ─────────────────────────

def plural(count: int) -> str:
    word = ("исполнитель" if count % 10 == 1 and count % 100 != 11
            else "исполнителя" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14)
            else "исполнителей")
    return f"{count} {word}"


def capacity_line(cap: dict, safety: dict, cx: dict, verdict: str) -> str:
    alive = f"живых {cap['живых']}"
    if cap["расходятся"]:
        src = cap["по_источникам"]
        alive += f" (заявок {src['заявки']}, ролей {src['роли']}, вызовов {src['вызовы']})"
    loaded = cap["задач_в_работе"] >= safety["порог_нагрузки"]
    cut = f"сложность: {cx['мера']} ≥ порог резки {cx['порог']}; " if cx["режет"] and loaded else ""
    mode = ("судья скорости не отвечает, потолок как до режимов" if safety["режим"] is None
            else f"эффективный режим {safety['режим']}")
    return (f"ёмкость: свободно {cap['ёмкость']} из {safety['жёсткий_верх']} ({mode}), {alive}, "
            f"задач в работе {cap['задач_в_работе']} при пороге {safety['порог_нагрузки']}; "
            f"{cut}решение: {verdict}")


def task_plan(ctx: Ctx, g: dict, task: str):
    """Файл плана задачи: блок `план:` главы либо .tasks/<id>/план.md (как check-pipeline)."""
    if g["source"]["вид"] == "план":
        return g["source"]["файл"]
    own = ctx.root / ".tasks" / task / "план.md"
    return own if own.is_file() else None


def hackathon_lines(ctx: Ctx, g: dict, task: str) -> list:
    on = hackathon.active(conf_path=ctx.conf_path)
    mode = f"включён до {hackathon.until_text(ctx.conf_path)}" if on else "выключен"
    steps = ", ".join(pipeline.шаги_уровня(g["pipe_conf"], g["level"]))
    lines = [f"хакатон: режим {mode}; уровень задачи {g['level']}; шаги: {steps}"]
    if on and g["level"] != hackathon.LEVEL:
        lines.append(f"  перевести задачу на уровень хакатон: python3 scripts/hackathon.py "
                     f"--включить-задачу {task} --карта {ctx.devmap}")
    return lines


def judge_view(ctx: Ctx):
    """(эффективный режим, потолок живых, задач в работе) у судьи скорости; None — судья не ответил."""
    path = ctx.root / "harness" / "demons" / "speed_mode.py"
    try:
        spec = importlib.util.spec_from_file_location("speed_mode", path)
        judge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(judge)
    except (OSError, ImportError, AttributeError, KeyError, TypeError, ValueError, yaml.YAMLError):
        return None
    try:
        mode, ceiling = judge.effective(ctx.now), judge.ceiling_alive(ctx.now)
        # Один счёт «задач в работе» на сторожа и потолок: считает судья, не live_state.
        work = len(judge.in_work(ctx.now))
    except (OSError, ImportError, AttributeError, KeyError, TypeError, ValueError, yaml.YAMLError,
            judge.DataError, judge.MeasureError):
        return None
    return None if mode is None or ceiling is None else (mode, ceiling, work)


def mode_safety(ctx: Ctx, safety: dict) -> dict:
    """Предохранитель с подменой от судьи; без судьи — данные как до режимов."""
    view = judge_view(ctx)
    if view is None:
        return {**safety, "режим": None, "задач_в_работе": None}
    mode, ceiling, work = view
    return {**safety, "жёсткий_верх": ceiling, "порог_нагрузки": mode + safety["порог_сверх_режима"],
            "режим": mode, "задач_в_работе": work}


def decide(ctx: Ctx, task: str) -> dict:
    roles = ctx.roles()
    g = gather(ctx, task, roles)
    cx = complexity(ctx, g, roles)
    outcome, hints, outcome_trace = plan_outcome(ctx, g, roles)
    safety = mode_safety(ctx, roles["предохранитель"])
    window = float(safety["окно_живых"]) * 60
    live = lib.live_state(g["claim_rows"], ctx.journal_rows("роли-помощников"),
                          ctx.journal_rows("вызовы-агентов"), ctx.now, window,
                          safety["жёсткий_верх"])
    # Без судьи — счёт live_state, как до режимов.
    in_work = live["задач_в_работе"] if safety["задач_в_работе"] is None else safety["задач_в_работе"]
    live = {**live, "задач_в_работе": in_work}
    held = lib.held_paths(g["claims"], task)
    struct = g["structure"]
    waiting = {p["n"] for p in g["parts"] if held & set(p["файлы"])}
    blocked = {struct["группа"][n] for n in waiting}
    needed = struct["групп"] - len(blocked)
    facts = {"сложность": cx["имя"], "групп": needed, "файлов": len(struct["файлов"]),
             "шагов": g["steps"], "ёмкость": live["ёмкость"], "сложность_режет": cx["режет"],
             "задач_в_работе": live["задач_в_работе"]}
    rule = lib.pick_rule(roles["правила"], facts, {**roles, **safety})
    # Нужно 0 (файлов ещё нет) — один исполнитель или управляющий, который порежет работу.
    wanted = max(needed, 1)
    # Мелочь не ждёт никогда: один исполнитель и при ёмкости 0.
    trifle = rule["имя"] == "мелочь"
    room = max(live["ёмкость"], 1) if trifle else live["ёмкость"]
    count, trimmed = lib.executors(rule["исполнителей"], {"групп": wanted, "нужно": wanted,
                                                          "ёмкость": room}, room)
    wait = room == 0
    verdict = (f"ждать, ёмкость {live['ёмкость']} ({rule['имя']})" if wait
               else f"{plural(count)} ({rule['имя']}, групп {struct['групп']})")
    label = f"Часть: {g['chapter']}/{task}" if g["chapter"] else f"Часть: {task}"
    source = g["source"]
    plan_file = task_plan(ctx, g, task)
    models = {}
    if rule["роль"] == lib.LEAD_ROLE:
        models["управляющий"] = model_for("управляющий")
    for kind in dict.fromkeys(p["вид"] for p in g["parts"]):
        models[kind] = model_for(kind)
    state = ("мелкая" if rule["имя"] == "мелочь"
             else "нужен_план_нет" if outcome == "нужен_план" and not plan_file else "норма")
    return {
        "ид": "p-" + uuid.uuid4().hex[:12], "задача": task, "состояние": state,
        "правило": rule["имя"], "роль": rule["роль"], "исполнителей": count,
        "глубина": rule.get("глубина"), "ждать": wait,
        "признаки": {"групп": struct["групп"], "файлов": len(struct["файлов"]),
                     "шагов": g["steps"], "уровень": g["level"], "сложность": cx["имя"],
                     "источник_сложности": cx["источник"], "цена": cx["цена"],
                     "complexity": cx["complexity"], "сложность_режет": cx["режет"]},
        "исход_плана": outcome, "глава": g["chapter"],
        "план": str(plan_file) if plan_file else None,
        "источник_частей": str(source["файл"]) if source["файл"] else None,
        "части": [{"n": p["n"], "файлы": p["файлы"], "вид": p["вид"],
                   "зависит_от": p["зависит_от"], "группа": struct["группа"][p["n"]],
                   "волна": struct["волна"][p["n"]], "ждёт": p["n"] in waiting,
                   "метка": f"{label}#{p['n']}"} for p in g["parts"]],
        "волн": struct["волн"],
        "ёмкость": {"жёсткий_верх": safety["жёсткий_верх"], "порог_нагрузки": safety["порог_нагрузки"],
                    "режим": safety["режим"], "живых": live["живых"],
                    "свободно": live["свободно"], "задач_в_работе": live["задач_в_работе"],
                    "задачи": live["задачи"], "по_источникам": live["по_источникам"],
                    "нужно": needed, "ждут": len(blocked), "урезал": trimmed},
        "строка_ёмкости": capacity_line(live, safety, cx, verdict),
        "модели": models, "подсказка": hints,
        "строки_хакатона": hackathon_lines(ctx, g, task),
        "след": cx["след"] + outcome_trace + closed_plan_trace(g),
        "хеш_плана": lib.file_hash(source["файл"]) if source["файл"] else None,
        "_решение": verdict,
    }


def render_text(d: dict) -> str:
    sig = d["признаки"]
    out = [f"задача: {d['задача']}  состояние: {d['состояние']}",
           f"правило: {d['правило']}  роль: {d['роль']}  исполнителей: {d['исполнителей']}"
           + ("  ждать" if d["ждать"] else ""),
           f"исход плана: {d['исход_плана']}" + (f" (глава {d['глава']})" if d["глава"] else ""),
           f"сложность: {sig['сложность']} ({'цена ' + str(sig['цена']) if sig['цена'] is not None else 'по оценке complexity ' + str(sig['complexity'])}); "
           f"групп {sig['групп']}, файлов {sig['файлов']}, шагов {sig['шагов']}, волн {d['волн']}"]
    out += [f"  #{p['n']} волна {p['волна']} группа {p['группа']}{' ждёт' if p['ждёт'] else ''}: "
            f"{', '.join(p['файлы'])}  [{p['метка']}]" for p in d["части"]]
    out.append("модели: " + (", ".join(f"{k} → {v}" for k, v in d["модели"].items()) or "—"))
    out.append(d["строка_ёмкости"])
    out += d["строки_хакатона"]
    out += [f"след: {line}" for line in d["след"]]
    return "\n".join(out)


def save_decision(ctx: Ctx, d: dict) -> None:
    folder = ctx.logs / "планы-запуска"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{d['задача']}.json"
    tmp = folder / f".{d['задача']}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


def plan_event_data(d: dict) -> dict:
    sig, cap = d["признаки"], d["ёмкость"]
    return {"ид": d["ид"], "правило": d["правило"], "роль": d["роль"],
            "исполнителей": d["исполнителей"], "сложность": sig["сложность"],
            "источник_сложности": sig["источник_сложности"], "исход_плана": d["исход_плана"],
            "групп": sig["групп"], "файлов": sig["файлов"],
            "сложность_режет": sig["сложность_режет"],
            "ёмкость": {"свободно": cap["свободно"], "живых": cap["живых"],
                        "задач_в_работе": cap["задач_в_работе"]},
            "решение": d["_решение"], "урезал": cap["урезал"]}


def cmd_decide(ctx: Ctx, opts: dict) -> int:
    task = opts["pos"][0]
    try:
        d = decide(ctx, task)
    except lib.Skipped as exc:
        emit(task, "nesting.plan", "skipped", {"почему": str(exc)})
        return show({"задача": task, "состояние": "skipped", "почему": str(exc)}, opts,
                    f"{task}: пропуск — {exc}")
    emit(task, "nesting.plan", "skipped" if d["состояние"] == "мелкая" else "ok",
         {**plan_event_data(d), **({"почему": "правило мелочь"} if d["состояние"] == "мелкая" else {})})
    save_decision(ctx, d)
    return show(d, opts, render_text(d))


def show(payload: dict, opts: dict, text: str) -> int:
    print(json.dumps(payload, ensure_ascii=False) if opts["json"] else text)
    return 0


# ───────────────────────── нужен ли план ─────────────────────────

def cmd_need_plan(ctx: Ctx, opts: dict) -> int:
    task = opts["pos"][0]
    roles = ctx.roles()
    g = gather(ctx, task, roles)
    outcome, hints, trace = plan_outcome(ctx, g, roles)
    for chapter in hints:
        emit(task, "nesting.chapter", "ok", {"задача": task, "глава": chapter, "источник": "подсказка"})
    trace = trace + closed_plan_trace(g)
    result = {"задача": task, "исход": outcome, "глава": g["chapter"], "подсказка": hints, "след": trace}
    return show(result, opts, f"{task}: {outcome}" + (f" (глава {g['chapter']})" if g["chapter"] else "")
                + ("; прижить к " + ", ".join(hints) if hints else "")
                + "".join(f"\nслед: {line}" for line in trace))


# ───────────────────────── правка карты и плана ─────────────────────────

def attach_in_map(ctx: Ctx, chapters: dict) -> None:
    """Вставка строк `глава:` под dev-map.lock (cards.map_lock) не дольше HARNESS_MAP_LOCK_WAIT; сверка до записи, в файл — только сошедшееся."""
    try:
        with cards.map_lock(ctx.lock, ctx.lock_wait):
            before = ctx.devmap.read_text(encoding="utf-8")
            busy = [c["id"] for c in lib.map_cards(before) if c["id"] in chapters and c.get("глава")]
            if busy:
                raise lib.DataError(f"задача уже в главе: {', '.join(busy)}")
            after = lib.insert_chapters(before, chapters)
            lib.verify_chapters(before, after, chapters)
            ctx.devmap.write_text(after, encoding="utf-8")
    except cards.LockBusy:
        raise lib.DataError(f"лок карты занят: {ctx.lock}")


def cmd_attach(ctx: Ctx, opts: dict) -> int:
    task, path = opts["pos"]
    roles = ctx.roles()
    if task not in ctx.cards():
        raise lib.DataError(f"нет карточки {task}")
    plans = lib.all_plans(ctx.plans_dir)
    pid = path.split("/")[0]
    if pid not in plans:
        raise lib.DataError(f"план {pid} не найден в {ctx.plans_dir}")
    if path.count("/") > roles["верх_вложенности"]:
        raise lib.DataError(f"глава {path} глубже верха вложенности {roles['верх_вложенности']}")
    plan_file = plans[pid][1]
    saved_plan = plan_file.read_text(encoding="utf-8")
    title = opts.get("--новая-глава")
    if lib.chapter_of(plans, path) is None:
        if title is None:
            raise lib.DataError(f"глава {path} не найдена в плане {pid}")
        if path.count("/") != 1:
            raise lib.DataError("новая глава заводится только на верхнем уровне плана")
        plan_file.write_text(lib.insert_plan_chapter(saved_plan, path.split("/")[1], title),
                             encoding="utf-8")
        if lib.chapter_of(lib.all_plans(ctx.plans_dir), path) is None:
            plan_file.write_text(saved_plan, encoding="utf-8")
            raise lib.DataError("после вставки главы план не читается: правка плана отменена")
    try:
        attach_in_map(ctx, {task: path})
    except lib.DataError:
        plan_file.write_text(saved_plan, encoding="utf-8")
        raise
    emit(task, "nesting.chapter", "ok", {"задача": task, "глава": path, "источник": "смена"})
    print(f"{task} → {path}")
    return 0


def cmd_pin(ctx: Ctx, opts: dict) -> int:
    pid = opts["pos"][0]
    roles = ctx.roles()
    plans = lib.all_plans(ctx.plans_dir)
    if pid not in plans:
        raise lib.DataError(f"план {pid} не найден в {ctx.plans_dir}")
    plan, plan_path = plans[pid]
    problems = lib.plan_problems(plan, plan_path, ctx.cards(), roles["верх_вложенности"])
    if problems:
        raise lib.DataError(problems)
    chapters = {task: paths[0] for task, paths in lib.chapter_tasks(plan).items()}
    missing = [t for t in chapters if t not in ctx.cards()]
    if missing:
        raise lib.DataError([f"в карте нет карточки {t}" for t in missing])
    pending = {t: c for t, c in chapters.items() if ctx.cards()[t].get("глава") != c}
    attach_in_map(ctx, pending)
    for task, chapter in pending.items():
        emit(task, "nesting.chapter", "ok", {"задача": task, "глава": chapter, "источник": "план"})
    print(f"закреплено задач: {len(pending)} из {len(chapters)}")
    return 0


def cmd_check_plan(ctx: Ctx, opts: dict) -> int:
    pid = opts["pos"][0]
    plans = lib.all_plans(ctx.plans_dir)
    orphans = lib.orphan_problems(plans, ctx.cards(), pid)
    if pid not in plans:
        raise lib.DataError([f"план {pid} не найден в {ctx.plans_dir}", *orphans])
    plan, plan_path = plans[pid]
    problems = lib.plan_problems(plan, plan_path, ctx.cards(), ctx.roles()["верх_вложенности"]) + orphans
    if problems:
        raise lib.DataError(problems)
    print(f"план {pid}: правила §4.3 соблюдены")
    return 0


# ───────────────────────── отметки и метрики ─────────────────────────

def cmd_mark(ctx: Ctx, opts: dict) -> int:
    ident, verdict = opts["pos"]
    if verdict not in ("верно", "ложно"):
        raise UsageError("оценка — верно или ложно")
    found = next((r for r in ctx.journal_rows(JOURNAL, None)
                  if (r.get("data") or {}).get("ид") == ident), None)
    if found is None:
        raise lib.DataError(f"решения с ид {ident} нет в журнале {JOURNAL}")
    emit(found.get("task"), "nesting.mark", "ok", {"ид": ident, "оценка": verdict, "кто": opts.get("--кто") or "смена"})
    print(f"отмечено: {ident} — {verdict}")
    return 0


def cmd_metrics(ctx: Ctx, opts: dict) -> int:
    try:
        days = float(opts.get("--сутки") or 1)
    except ValueError:
        raise UsageError("--сутки — число")
    metrics = lib.compute_metrics(ctx.journal_rows(JOURNAL, None), ctx.now, days, ctx.roles())
    if opts["строка"]:
        print(lib.metrics_line(metrics))
    elif opts["json"]:
        print(json.dumps(metrics, ensure_ascii=False))
    else:
        print("\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}"
                         for key, value in metrics.items() if key != "к_красному"))
        print(lib.ready_line(metrics["к_красному"]))
    return 0


COMMANDS = {None: cmd_decide, "--нужен-план": cmd_need_plan, "--проверить-план": cmd_check_plan,
            "--прижить": cmd_attach, "--закрепить": cmd_pin, "--отметить": cmd_mark,
            "--метрики": cmd_metrics}


def main(argv: list) -> int:
    try:
        opts = parse_args(argv)
        return COMMANDS[opts["mode"]](Ctx(opts), opts)
    except UsageError as exc:
        print(f"неверный вызов: {exc}", file=sys.stderr)
        return 2
    except lib.DataError as exc:
        print("\n".join(exc.problems), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
