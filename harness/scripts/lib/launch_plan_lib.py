#!/usr/bin/env python3
"""Чистые части плана запуска помощников: данные плана, части и группы, правила,
живые помощники, правка текста карты и плана, метрики.

Вызывается из scripts/launch-plan.py (контракт — docstring scripts/test_launch_plan.py).
Здесь нет разбора аргументов и записи журнала: только функции от данных.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import operator
import os
import re
import time
from pathlib import Path

import yaml

LEAD_ROLE = "harness-lead"
YAML_BLOCK = re.compile(r"```yaml\n(.*?)\n```", re.S)
OPERATORS = {"<=": operator.le, ">=": operator.ge, "==": operator.eq,
             "<": operator.lt, ">": operator.gt}
MARK_PATH = re.compile(r"Часть:\s*([^\s#]+)")
TAIL_BYTES = 1 << 21


class DataError(Exception):
    """Нарушение данных: код 1 с перечнем."""

    def __init__(self, problems):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


class Skipped(Exception):
    """Данных не хватает или они битые: код 0, строка skipped с причиной."""


def load_yaml(text: str):
    return yaml.load(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))


def real(path, root) -> str:
    path = Path(path)
    return os.path.realpath(path if path.is_absolute() else Path(root) / path)


def file_hash(path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ───────────────────────── правила ─────────────────────────

def named(names: dict, dotted: str):
    """Значение по имени из данных; «порог_мелочи.файлов» — вложенный ключ."""
    value = names
    for key in dotted.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def holds(spec, value, names: dict) -> bool:
    """Условие правила: число — равенство, строка «<=2», «>=порог_нагрузки»,
    «<=порог_мелочи.файлов», да/нет — для логических признаков."""
    if isinstance(value, bool):
        return value == (spec is True or spec == "да")
    if isinstance(spec, (int, float)):
        return value == spec
    found = re.fullmatch(r"(<=|>=|==|<|>)?\s*(.+?)\s*", str(spec))
    bound = named(names, found.group(2))
    if bound is None:
        bound = float(found.group(2)) if re.fullmatch(r"[\d.]+", found.group(2)) else found.group(2)
    return OPERATORS[found.group(1) or "=="](value, bound)


def pick_rule(rules: list, facts: dict, names: dict) -> dict:
    for rule in rules:
        if all(key in facts and holds(spec, facts[key], names)
               for key, spec in (rule.get("если") or {}).items()):
            return rule
    raise Skipped("ни одно правило не подошло")


def executors(expr, wanted: dict, capacity: int) -> tuple:
    """(сколько исполнителей, кто урезал). Число — как есть, «ёмкость» и
    «min(групп, ёмкость)» — по словарю wanted (=min(нужно, ёмкость)); сверху режет ёмкость."""
    if isinstance(expr, int):
        raw = expr
    else:
        inner = re.fullmatch(r"min\((.*)\)", str(expr))
        # Любое выражение не больше числа групп, которые вообще можно взять сейчас.
        raw = min(wanted["нужно"], *(wanted[name.strip()] for name in
                                     (inner.group(1) if inner else str(expr)).split(",")))
    count = min(raw, capacity)
    return count, ("ёмкость" if count < raw else None)


def complexity_name(estimate, bounds: dict) -> str:
    for name, spec in bounds.items():
        if holds(spec, estimate, {}):
            return name
    raise Skipped(f"оценка complexity {estimate} не входит ни в одну границу вывод_роли.сложность")


# ───────────────────────── план и главы ─────────────────────────

def load_plan_file(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
        found = YAML_BLOCK.search(text)
        data = load_yaml(found.group(1)) if found else None
    except (OSError, yaml.YAMLError):
        return None
    plan = data.get("план") if isinstance(data, dict) else None
    return plan if isinstance(plan, dict) else None


def all_plans(plans_dir) -> dict:
    """id плана → (план, путь файла)."""
    found = {}
    for path in sorted(Path(plans_dir).glob("*.md")):
        plan = load_plan_file(path)
        if plan:
            found[plan.get("id") or path.stem] = (plan, path)
    return found


def walk_chapters(nodes, prefix: str):
    for chapter in nodes or []:
        path = f"{prefix}/{chapter.get('id')}"
        yield path, chapter
        yield from walk_chapters(chapter.get("главы"), path)


def chapter_of(plans: dict, path: str):
    entry = plans.get(path.split("/")[0])
    if not entry:
        return None
    plan, plan_path = entry
    for found, chapter in walk_chapters(plan.get("главы"), plan["id"]):
        if found == path:
            return plan, plan_path, chapter
    return None


def plan_problems(plan: dict, plan_path, cards: dict, top: int) -> list:
    """Правила §4.3: глубина, уникальность id, владелец, задача в одной главе."""
    problems = []
    pid = plan.get("id")
    if pid != Path(plan_path).stem:
        problems.append(f"id плана «{pid}» не равен имени файла «{Path(plan_path).stem}»")
    seen_paths = set()
    for path, chapter in walk_chapters(plan.get("главы"), pid):
        if not re.fullmatch(r"[a-z0-9-]+", str(chapter.get("id"))):
            problems.append(f"id главы «{chapter.get('id')}» не из [a-z0-9-] ({path})")
        if path in seen_paths:
            problems.append(f"путь главы {path} повторяется")
        seen_paths.add(path)
        if path.count("/") > top:
            problems.append(f"глава {path} глубже верха вложенности {top}")
    owner = cards.get(plan.get("владелец")) or {}
    if str(owner.get("глава") or "").split("/")[0] == pid:
        problems.append(f"владелец плана {plan.get('владелец')} сам лежит в главе {owner['глава']}")
    problems += chapter_task_problems(plan)
    return problems


def chapter_tasks(plan: dict) -> dict:
    """Задача → список путей глав, в чьих `части:` она названа."""
    found: dict = {}
    for path, chapter in walk_chapters(plan.get("главы"), plan["id"]):
        for task in chapter.get("части") or {}:
            found.setdefault(task, []).append(path)
    return found


def chapter_task_problems(plan: dict) -> list:
    return [f"задача {task} названа в нескольких главах: {', '.join(paths)}"
            for task, paths in chapter_tasks(plan).items() if len(paths) > 1]


def orphan_problems(plans: dict, cards: dict, pid: str) -> list:
    """Карточки, чья глава лежит в плане pid, но главы (или самого плана) в плане нет."""
    return [f"сирота: карточка {cid} ссылается на главу {card['глава']}, которой нет в плане {pid}"
            for cid, card in cards.items()
            if str(card.get("глава") or "").split("/")[0] == pid
            and chapter_of(plans, card["глава"]) is None]


def closed_plan_line(plan: dict, cards: dict) -> str | None:
    """Владелец плана закрыт, а задачи его глав открыты: строка предупреждения с перечнем."""
    if (cards.get(plan.get("владелец")) or {}).get("status") != "done":
        return None
    prefix = f"{plan['id']}/"
    open_ids = [cid for cid, card in cards.items()
                if str(card.get("глава") or "").startswith(prefix) and card.get("status") != "done"]
    return (f"план закрыт с открытыми задачами: {plan['id']} (владелец {plan.get('владелец')}): "
            + ", ".join(open_ids)) if open_ids else None


def message_chapter(text: str, cards: dict, plans: dict):
    """(задача, глава) для новой карточки по первой ЯВНО названной в тексте задаче с планом:
    владелец плана без своей главы — первая глава верхнего уровня, задача в главе —
    корень её пути. Имя целым словом: «hr.x-y» не называет hr.x."""
    named_at = sorted((found.start(), task) for task in cards
                      for found in re.finditer(rf"(?<![\w.-]){re.escape(task)}(?![\w.-])", text))
    for _, task in named_at:
        chapter = cards[task].get("глава")
        if chapter and chapter_of(plans, chapter):
            return task, "/".join(chapter.split("/")[:2])
        for pid, (plan, _path) in plans.items():
            if plan.get("владелец") == task and not chapter and plan.get("главы"):
                return task, f"{pid}/{plan['главы'][0]['id']}"
    return None


def hint_chapters(plans: dict, cards: dict, files: list, root, threshold: float) -> list:
    """Главы открытых планов с наибольшей долей пересечения ≥ порога; ничья — все."""
    scored = []
    for pid, (plan, _) in plans.items():
        if (cards.get(plan.get("владелец")) or {}).get("status") == "done":
            continue
        for path, chapter in walk_chapters(plan.get("главы"), pid):
            cover = [real(item, root) for item in chapter.get("охват") or []]
            inside = [f for f in files
                      if any(f == c or f.startswith(c.rstrip("/") + "/") for c in cover)]
            scored.append((len(inside) / len(files), path))
    top = max((share for share, _ in scored), default=0)
    return sorted(path for share, path in scored if share == top and top > 0 and share >= threshold)


# ───────────────────────── части ─────────────────────────

def find_parts(task: str, chapter_ctx, plans: dict, root, claim_paths: list) -> dict:
    """Части задачи по порядку источников; {части, файл, вид источника}."""
    if chapter_ctx:
        plan, plan_path, chapter = chapter_ctx
        for scope in (chapter, plan):
            if (scope.get("части") or {}).get(task):
                return {"части": scope["части"][task], "файл": plan_path, "вид": "план"}
    for plan, plan_path in plans.values():
        if plan.get("владелец") == task and (plan.get("части") or {}).get(task):
            return {"части": plan["части"][task], "файл": plan_path, "вид": "план"}
    pretask = Path(root) / ".tasks" / task / "pretask.yaml"
    try:
        data = load_yaml(pretask.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    if data.get("части"):
        return {"части": data["части"], "файл": pretask, "вид": "pretask"}
    if data.get("файлы"):
        return {"части": [{"файлы": data["файлы"]}], "файл": pretask, "вид": "pretask"}
    if claim_paths:
        return {"части": [{"файлы": claim_paths}], "файл": None, "вид": "заявки"}
    return {"части": [], "файл": None, "вид": None}


def normalize_parts(raw: list, root) -> list:
    return [{"n": n, "вид": part.get("вид") or "код",
             "зависит_от": list(part.get("зависит_от") or []),
             "файлы": list(dict.fromkeys(real(f, root) for f in part.get("файлы") or []))}
            for n, part in enumerate(raw, 1)]


def waves_of(parts: list) -> dict:
    """n → номер волны (1 — без зависимостей). Цикл — DataError."""
    wave: dict = {}
    pending = {p["n"]: p["зависит_от"] for p in parts}
    while pending:
        ready = [n for n, deps in pending.items() if all(d in wave for d in deps)]
        if not ready:
            raise DataError(f"цикл в зависит_от между частями {sorted(pending)}")
        for n in ready:
            wave[n] = 1 + max((wave[d] for d in pending.pop(n)), default=0)
    return wave


def reach_of(parts: list) -> dict:
    """n → множество частей, от которых он зависит (транзитивно)."""
    direct = {p["n"]: set(p["зависит_от"]) for p in parts}

    def climb(n, seen):
        for dep in direct[n]:
            if dep not in seen:
                seen.add(dep)
                climb(dep, seen)
        return seen
    return {n: climb(n, set()) for n in direct}


def analyse_parts(parts: list) -> dict:
    """Группы, волны и проверки §3.3: зависимости, цикл, общий файл, нечего делить."""
    count = len(parts)
    problems = [f"часть {p['n']}: зависит_от {d} — такой части нет"
                for p in parts for d in p["зависит_от"] if not 1 <= d <= count or d == p["n"]]
    if problems:
        raise DataError(problems)
    wave = waves_of(parts)
    reach = reach_of(parts)
    owners: dict = {}
    for part in parts:
        for path in part["файлы"]:
            owners.setdefault(path, []).append(part["n"])
    clashes = [f"общий файл {path} у независимых частей {a} и {b}"
               for path, ns in owners.items() for i, a in enumerate(ns) for b in ns[i + 1:]
               if b not in reach[a] and a not in reach[b]]
    if clashes:
        raise DataError(clashes)
    if parts and not owners:
        raise DataError("в плане нет ни одного файла: нечего делить")
    group = {n: n for n in wave}

    def root_of(n):
        while group[n] != n:
            n = group[n]
        return n
    links = [(a, b) for ns in owners.values() for a in ns[:1] for b in ns[1:]]
    links += [(p["n"], d) for p in parts for d in p["зависит_от"]]
    for a, b in links:
        group[root_of(a)] = root_of(b)
    order = list(dict.fromkeys(root_of(p["n"]) for p in parts))
    return {"группа": {n: order.index(root_of(n)) + 1 for n in wave}, "волна": wave,
            "групп": len(order), "волн": max(wave.values(), default=0),
            "файлов": list(owners)}


# ───────────────────────── сложность ─────────────────────────

def price_of(files: list, root, pipe, pipe_conf: dict, conf_path) -> int:
    """pipeline.цена() по файлам частей без индекса git: новый — файла нет."""
    base = os.path.realpath(root)
    staged = [{"путь": os.path.relpath(f, base), "добавлено": 0, "новый": not os.path.exists(f)}
              for f in files]
    marks = pipe.признаки_коммита(staged=staged, пути=pipe.пути_установки(conf_path))
    return pipe.цена(marks, pipe_conf["веса"])


def level_by_price(value: int, limits: dict) -> str:
    if value < limits["П1"]:
        return "простая"
    return "средняя" if value < limits["П2"] else "высокая"


def steps_of(level: str, character: str, pipe, pipe_conf: dict) -> list:
    steps = pipe.шаги_уровня(pipe_conf, level)
    narrow = ((pipe_conf.get("характеры") or {}).get(character) or {}).get("шаги")
    return [s for s in steps if s in narrow] if isinstance(narrow, list) else steps


# ───────────────────────── журналы и живые ─────────────────────────

def read_rows(path, tail: int | None = TAIL_BYTES) -> list:
    try:
        with Path(path).open("rb") as fh:
            if tail:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - tail))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
            row["_at"] = dt.datetime.fromisoformat(row["ts"]).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def fresh(rows: list, now: float, window_s: float, event: str,
          outcomes: tuple = ("ok",)) -> list:
    return [r for r in rows if r.get("event") == event and r.get("outcome") in outcomes
            and 0 <= now - r["_at"] <= window_s]


def mark_task(row: dict) -> str | None:
    """Задача запуска — последний сегмент пути метки data.часть, метки нет — None."""
    found = MARK_PATH.search(str((row.get("data") or {}).get("часть") or ""))
    return found.group(1).rstrip("/").split("/")[-1] if found else None


def agent_id(row: dict) -> str | None:
    found = (row.get("data") or {}).get("агент")
    return found if found and found != "-" else None


def meta_path(payload: dict, agent: str) -> Path:
    return (Path(str(payload.get("transcript_path") or "")).parent
            / str(payload.get("session_id") or "") / "subagents"
            / f"agent-{agent}.meta.json")


def tool_use_id_of(payload: dict, agent: str) -> str:
    """tool_use_id запуска помощника `agent` из его meta.json; пусто, если файла нет."""
    try:
        return str(json.loads(meta_path(payload, agent).read_text(encoding="utf-8"))
                   .get("toolUseId") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def launch_tasks(launches: list) -> list:
    """Имена задач запусков окна. Анонимные дети управляющего — задача его меченого
    запуска, иначе одна задача на управляющего; анонимный запуск смены — своя задача."""
    by_lead: dict = {}
    for row in launches:
        lead = (row.get("data") or {}).get("управляющий")
        if lead and mark_task(row):
            by_lead.setdefault(lead, mark_task(row))
    names = set()
    for number, row in enumerate(launches):
        lead = (row.get("data") or {}).get("управляющий")
        names.add(mark_task(row) or (by_lead.get(lead) or f"управляющий:{lead}" if lead
                                     else f"запуск:{number}"))
    return sorted(names)


def live_state(claim_rows: list, role_rows: list, launch_rows: list, now: float,
               window_s: float, ceiling: int) -> dict:
    """Живые: помощники с активностью за окно (заявки и роли, по data.агент) плюс
    запуски, чей ребёнок ещё не действовал. Строки — весь прочитанный хвост журналов."""
    claimed = {agent_id(r) for r in fresh(claim_rows, now, window_s, "file.claim") if agent_id(r)}
    acting = {agent_id(r) for r in fresh(role_rows, now, window_s, "role.decision") if agent_id(r)}
    launches = fresh(launch_rows, now, window_s, "agent.launch", ("ok", "skipped"))
    first_seen: dict = {}
    for row in (*claim_rows, *role_rows):
        if agent_id(row):
            first_seen[agent_id(row)] = min(first_seen.get(agent_id(row), row["_at"]), row["_at"])
    earliest = min((r["_at"] for r in launches), default=None)
    ids = claimed | acting
    started = sum(1 for a in ids if earliest is not None and first_seen[a] >= earliest)
    idle = max(0, len(launches) - started)
    alive = len(ids) + idle
    tasks = launch_tasks(launches)
    free = ceiling - alive
    counts = {"заявки": len(claimed), "роли": len(acting), "вызовы": len(launches)}
    return {"живых": alive, "по_источникам": {**counts, "без_активности": idle},
            "задачи": tasks, "задач_в_работе": len(tasks), "свободно": free,
            "ёмкость": max(free, 0),
            "расходятся": len({v for v in counts.values() if v}) > 1}


def held_paths(claim_rows: list, task: str) -> set:
    return {real((r.get("data") or {}).get("путь"), "/") for r in claim_rows
            if r.get("task") != task and (r.get("data") or {}).get("путь")}


# ───────────────────────── правка текста ─────────────────────────

def map_cards(text: str) -> list:
    data = load_yaml(text) or {}
    return [t for line in data.get("lines") or [] for t in line.get("tasks") or []]


def insert_chapters(text: str, chapters: dict) -> str:
    """Вставка строки `глава:` после строки `id:` каждой карточки, без yaml.dump."""
    lines = text.splitlines(keepends=True)
    for task, path in chapters.items():
        pattern = re.compile(rf"^(\s*)-\s+id:\s*{re.escape(task)}\s*(#.*)?$")
        at = [i for i, line in enumerate(lines) if pattern.match(line.rstrip("\n"))]
        if len(at) != 1:
            raise DataError(f"в тексте карты карточка «{task}» найдена {len(at)} раз")
        indent = pattern.match(lines[at[0]].rstrip("\n")).group(1)
        lines.insert(at[0] + 1, f"{indent}  глава: {path}\n")
    return "".join(lines)


def verify_chapters(before: str, after: str, chapters: dict) -> None:
    """Число карточек сошлось, чужие не тронуты, у своих появилась глава."""
    expected = [dict(c, глава=chapters[c["id"]]) if c["id"] in chapters else c
                for c in map_cards(before)]
    if map_cards(after) != expected:
        raise DataError("сверка карты после вставки не сошлась: запись отменена")


def insert_plan_chapter(text: str, chapter_id: str, title: str) -> str:
    """Дописывает главу верхнего уровня в конец списка `главы:` блока плана."""
    found = YAML_BLOCK.search(text)
    lines = found.group(1).split("\n")
    root = next(i for i, line in enumerate(lines) if line.rstrip() == "план:")
    child = re.match(r" *", next(line for line in lines[root + 1:]
                                 if line.strip() and not line.strip().startswith("#"))).group(0)
    key = next((i for i in range(root + 1, len(lines))
                if re.fullmatch(rf"{child}главы:\s*(\[\s*\])?", lines[i].rstrip())), None)
    if key is None:
        raise DataError("в блоке плана нет ключа `главы:` верхнего уровня")
    end = key + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith(child + " ")
                                or lines[end].startswith(child + "- ")):
        end += 1
    while not lines[end - 1].strip():
        end -= 1
    first = next((line for line in lines[key + 1:end] if line.lstrip().startswith("-")), None)
    indent = re.match(r" *", first).group(0) if first else child + "  "
    lines[key] = f"{child}главы:"
    lines[end:end] = [f"{indent}- id: {chapter_id}",
                      f"{indent}  название: {json.dumps(title, ensure_ascii=False)}",
                      f"{indent}  охват: []"]
    return text[:found.start(1)] + "\n".join(lines) + text[found.end(1):]


# ───────────────────────── метрики ─────────────────────────

def share(count: int, total: int) -> float:
    return round(count / total, 2) if total else 0.0


def shares(values: list) -> dict:
    return {key: share(values.count(key), len(values)) for key in dict.fromkeys(values)}


def last_per_task(rows: list) -> list:
    """По одной строке на задачу — последняя: пересчёт той же задачи не новое решение."""
    latest: dict = {}
    for row in sorted(rows, key=lambda r: r["_at"]):
        latest[row.get("task") or (row.get("data") or {}).get("ид")] = row
    return list(latest.values())


def cut_reason(decision: dict) -> str:
    return "групп_1" if (decision.get("групп") or 0) < 2 else "свободно_меньше_2"


def ready_for_red(m: dict, trial: dict, days: float) -> dict:
    """Критерии §7 по блоку вывод_роли.обкатка: пункт true — выполнен."""
    warn = m["предупреждения"]
    marked = warn["верных"] + warn["ложных"]
    points = {
        "решений": m["вне_мелкой"] >= trial["решений_мин"],
        "ложных": share(warn["ложных"], marked) * 100 <= trial["ложных_макс"],
        "неотмеченных": share(warn["неотмеченных"], warn["всего"]) * 100 <= trial["неотмеченных_макс"],
        "метка": m["доля_с_меткой"] * 100 >= trial["метка_мин"],
        "сбоев": m["сбоев"] / days <= trial["сбоев_за_сутки"],
        "дверей": m["доля_дверей"] * 100 <= trial["дверей_макс"],
    }
    return {"готово": all(points.values()), "пункты": points}


def compute_metrics(rows: list, now: float, days: float, roles: dict) -> dict:
    inside = [r for r in rows if 0 <= now - r["_at"] <= days * 86400]
    data = lambda r: r.get("data") or {}                                     # noqa: E731
    plan_rows = last_per_task([r for r in inside if r.get("event") == "nesting.plan"
                               and (r.get("outcome") == "ok"
                                    or (r.get("outcome") == "skipped"
                                        and data(r).get("правило") == "мелочь"))])
    decisions = [data(r) for r in plan_rows]
    hook = [r for r in inside if r.get("event") == "nesting.decision"]
    marks = {data(r).get("ид"): data(r).get("оценка") for r in rows if r.get("event") == "nesting.mark"}
    warned = [data(r) for r in hook if data(r).get("предупреждение")]
    rules = [d.get("правило") for d in decisions]
    split_off = [d.get("правило") for d in decisions
                 if d.get("правило") in ("нагрузка", "простая") and (d.get("групп") or 0) >= 2]
    load = roles["предохранитель"]["порог_нагрузки"]
    # Порог, по которому решение принято (зависит от режима скорости), а не нынешний из данных.
    heavy = [d for d in decisions if d.get("сложность_режет")
             and (d.get("ёмкость") or {}).get("задач_в_работе", 0)
             >= (d.get("ёмкость") or {}).get("порог_нагрузки", load)]
    uncut = [d for d in heavy if not str(d.get("правило")).startswith("резка_под_нагрузкой")]
    failures = sum(1 for r in hook if r.get("outcome") == "skipped"
                   and data(r).get("сбой") in ("сбой", "таймаут"))
    result = {
        "решений": len(decisions),
        "вне_мелкой": sum(1 for d in decisions if d.get("исход_плана") != "мелкая"),
        "по_ролям": shares([d.get("роль") for d in decisions]),
        "по_сложности": shares([d.get("сложность") for d in decisions]),
        "по_исходам": shares([d.get("исход_плана") for d in decisions]),
        "правила": {rule: rules.count(rule) for rule in dict.fromkeys(rules)},
        "сняли_дробление": {name: split_off.count(name) for name in ("нагрузка", "простая")},
        "резка": {"порезали": len(heavy) - len(uncut), "не_порезали": len(uncut),
                  "почему": {name: sum(1 for d in uncut if cut_reason(d) == name)
                             for name in ("групп_1", "свободно_меньше_2")}},
        "предупреждения": {"всего": len(warned),
                           "верных": sum(1 for w in warned if marks.get(w.get("ид")) == "верно"),
                           "ложных": sum(1 for w in warned if marks.get(w.get("ид")) == "ложно"),
                           "неотмеченных": sum(1 for w in warned if w.get("ид") not in marks)},
        "доля_дверей": share(sum(1 for r in hook if data(r).get("дверь")), len(hook)),
        "доля_с_меткой": share(sum(1 for r in hook if data(r).get("часть")), len(hook)),
        "сбоев": failures,
        "параллельных": share(sum(1 for d in decisions if (d.get("исполнителей") or 0) >= 2),
                              len(decisions)),
    }
    result["к_красному"] = ready_for_red(result, roles["обкатка"], days)
    return result


def ready_line(ready: dict) -> str:
    points = ", ".join(f"{name} {'да' if ok else 'нет'}" for name, ok in ready["пункты"].items())
    return f"к красному: {'готово' if ready['готово'] else 'нет'}, {points}"


def metrics_line(m: dict) -> str:
    pct = lambda value: round(value * 100)                                   # noqa: E731
    warn = m["предупреждения"]
    return (f"вложенность: решений {m['решений']} (вне мелкой {m['вне_мелкой']}), "
            f"lead {pct(m['по_ролям'].get(LEAD_ROLE, 0))}%, параллельных {pct(m['параллельных'])}%, "
            f"тяжёлых {pct(m['по_сложности'].get('высокая', 0))}%, предупреждений {warn['всего']}: "
            f"ложных {warn['ложных']}, неотмеченных {warn['неотмеченных']}, "
            f"с меткой {pct(m['доля_с_меткой'])}%, дверей {pct(m['доля_дверей'])}%")


def now_epoch() -> float:
    return float(os.environ.get("HARNESS_NOW") or time.time())
