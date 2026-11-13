#!/usr/bin/env python3
"""PreToolUse на запуск помощника: роль и число исполнителей берёт код, а не привычка.

Задача — из метки «Часть: <путь>[#<n>]» в prompt. Решение по задаче лежит в
$LOG_DIR/планы-запуска/<задача>.json (его пишет scripts/launch-plan.py); файла
нет — хук зовёт launch-plan.py сам. Метки нет (спека, ревью) — пропуск: другой
задачей её считать нельзя, получился бы чужой отказ.

Пять отказов (спека правила-вложенности-кодом §3.5), проверяются по порядку
1–5; data.отказ — первый сработавший, data.нарушения — все:
  1 lead выведен, а исполнителю даны файлы из двух и более групп;
  2 запущен lead там, где выведен исполнитель;
  3 метка с #n, а части n в плане запуска нет, либо глава метки не глава решения;
  4 нужен план, а его нет;
  5 исполнителей задачи уже запущено не меньше положенного.
Мелкая задача (состояние решения «мелкая») — хук молчит: skipped. Решение старше
окна_живых пересчитывается; вызов launch-plan.py ограничен вывод_роли.таймаут_плана_с.

Строгость — helper-roles.yaml, вывод_роли.строгость: «красный» — код 2 и причина
в stderr, «жёлтый» — код 0 и то же слово предупреждением модели. Дверь:
    python3 scripts/hooks/launch_plan_guard.py --door 'почему'
открывает проход на 15 минут (файл $LOG_DIR/launch-plan.door). Сбой самого хука
или битые данные работу не роняют: код 0 и строка skipped с причиной.
Журнал — $LOG_DIR/вложенность.jsonl, событие nesting.decision.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import config                                            # noqa: E402
import events                                            # noqa: E402

SRC = "scripts/hooks/launch_plan_guard.py"
EVENT = "nesting.decision"
TOOLS = ("Agent", "Task")
LEAD_TYPE = "harness-lead"
WORKER_TYPE = "harness-worker"
ISOLATION_KEY = "HELPER_WORKTREE"
DOOR_NAME = "launch-plan.door"
DOOR_TTL_S = 15 * 60
DOOR_REASON_MIN = 10
LABEL = re.compile(r"Часть:\s*(?P<path>[^\s#]+)(?:#(?P<n>\d+))?")
ABS_PATH = re.compile(r"(?<![\w.])/[\w.\-/@+]+")
PATH_TAIL = ".,;:"
STRICTNESS_RED, STRICTNESS_YELLOW = "красный", "жёлтый"
# Быстрый разбор: хук стоит на каждом запуске помощника, чистый SafeLoader ест 0,2 с.
YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class Skip(Exception):
    """Решить нечем: пропуск со следом, не отказ. `failure` — сбой для метрик (сбой|таймаут)."""

    def __init__(self, reason: str, failure: str | None = None):
        super().__init__(reason)
        self.failure = failure


def now() -> float:
    return float(os.environ.get("HARNESS_NOW") or time.time())


def roles_path() -> Path:
    return Path(os.environ.get("HARNESS_HELPER_ROLES")
                or ROOT / "harness" / "config" / "helper-roles.yaml")


def launch_plan_path() -> Path:
    return Path(os.environ.get("HARNESS_LAUNCH_PLAN") or ROOT / "scripts" / "launch-plan.py")


def log_dir() -> Path:
    return Path(events.log_dir())


def door_path() -> Path:
    return log_dir() / DOOR_NAME


def settings() -> dict:
    """Блок вывод_роли; битый файл или нет блока — Skip."""
    try:
        block = (yaml.load(roles_path().read_text(encoding="utf-8"), Loader=YAML_LOADER)
                 or {})["вывод_роли"]
        strictness = block["строгость"]
        window_min = float(block["предохранитель"]["окно_живых"])
        plan_timeout = float(block["таймаут_плана_с"])
    except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
        raise Skip(f"helper-roles.yaml: {type(exc).__name__}: {exc}"[:200])
    if strictness not in (STRICTNESS_RED, STRICTNESS_YELLOW):
        raise Skip(f"строгость «{strictness}» не красный и не жёлтый")
    return {"строгость": strictness, "окно_с": window_min * 60, "таймаут_с": plan_timeout}


def record(outcome: str, task: str, session: str, data: dict) -> None:
    events.emit(SRC, EVENT, outcome, journal="вложенность", task=task or None,
                session=session or None, data=data)


def is_fresh(path: Path, window_s: float) -> bool:
    """Решение моложе окна_живых: при прежней ёмкости «0, ждать» дольше окна не живёт."""
    try:
        return now() - path.stat().st_mtime <= window_s
    except OSError:
        return False


def read_plan(task: str, window_s: float, timeout_s: float) -> dict:
    path = log_dir() / "планы-запуска" / f"{task}.json"
    if not is_fresh(path, window_s):
        try:
            proc = subprocess.run([sys.executable, str(launch_plan_path()), "--json", task],
                                  capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            raise Skip(f"launch-plan.py дольше {timeout_s:g} с", failure="таймаут")
        if not is_fresh(path, window_s):
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [f"код {proc.returncode}"]
            raise Skip(f"плана запуска нет: {tail[0]}"[:200])
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Skip(f"план запуска не читается: {exc}"[:200])
    if not isinstance(plan, dict) or plan.get("состояние") == "skipped":
        raise Skip(str((plan or {}).get("почему") or "план запуска skipped")[:200])
    return plan


def stale(plan: dict) -> bool:
    """Хеш из плана запуска не сошёлся с файлом-источником частей."""
    recorded, source = plan.get("хеш_плана"), plan.get("источник_частей")
    if not source:
        return False
    try:
        digest = "sha256:" + hashlib.sha256(Path(source).read_bytes()).hexdigest()
    except OSError:
        return False
    return bool(recorded) and recorded != digest


def prompt_groups(prompt: str, parts: list) -> set:
    """Группы частей, чьи файлы названы в prompt абсолютными путями."""
    by_file = {os.path.realpath(f): p.get("группа") for p in parts for f in p.get("файлы") or []}
    named = {os.path.realpath(m.rstrip(PATH_TAIL)) for m in ABS_PATH.findall(prompt)}
    return {by_file[f] for f in named if f in by_file}


def executors_launched(task: str, window_s: float) -> int:
    """Запуски исполнителей задачи за окно: прошедшие строки этого хука."""
    path = log_dir() / "вложенность.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    count = 0
    for line in lines:
        try:
            row = json.loads(line)
            at = datetime.fromisoformat(row["ts"]).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        data = row.get("data") or {}
        if (row.get("src") == SRC and row.get("outcome") == "ok" and row.get("task") == task
                and data.get("тип") not in (LEAD_TYPE, None) and now() - at <= window_s):
            count += 1
    return count


def violations(plan: dict, chapter: str | None, n: int | None, kind: str, prompt: str,
               window_s: float) -> list:
    """Номера нарушений §3.5 в порядке 1–5."""
    parts = plan.get("части") or []
    found = []
    if plan.get("роль") == LEAD_TYPE and kind != LEAD_TYPE and len(prompt_groups(prompt, parts)) >= 2:
        found.append(1)
    if kind == LEAD_TYPE and plan.get("роль") != LEAD_TYPE:
        found.append(2)
    if (n is not None and n not in {p.get("n") for p in parts}) or chapter != plan.get("глава"):
        found.append(3)
    if plan.get("исход_плана") == "нужен_план" and not plan.get("план"):
        found.append(4)
    if kind != LEAD_TYPE and executors_launched(plan["задача"], window_s) >= int(plan.get("исполнителей") or 0):
        found.append(5)
    return found


def door_reason() -> str:
    try:
        door = json.loads(door_path().read_text(encoding="utf-8"))
        return str(door["почему"]) if float(door["до"]) >= now() else ""
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def text_for(plan: dict, number: int) -> str:
    sign = plan.get("признаки") or {}
    return (f"роль по правилам: {plan.get('роль')} ({plan.get('правило')}, групп "
            f"{sign.get('групп')}, файлов {sign.get('файлов')}). Нарушение {number}: "
            f"{REASONS[number]}")


REASONS = {
    1: "исполнителю даны файлы из нескольких групп, а правила выводят управляющего",
    2: "запущен управляющий там, где правила выводят исполнителя",
    3: "метка называет часть, которой нет в плане запуска",
    4: "задаче нужен план, а его нет",
    5: "исполнителей задачи запущено уже сколько положено",
}
DOOR_HINT = ("Законная дверь на 15 минут: python3 scripts/hooks/launch_plan_guard.py "
             "--door 'причина'")


def worktree_mode() -> bool:
    """HELPER_WORKTREE=1: установленный harness.conf, затем конфиг дерева хука; не «1» = 0."""
    tree_conf = ROOT / "harness" / "config" / "harness.conf"
    value = (config.из_файла(config.путь_harness_conf()).get(ISOLATION_KEY)
             or config.из_файла(str(tree_conf)).get(ISOLATION_KEY) or "")
    return value == "1"


def isolation_verdict(kind: str, tool_input: dict) -> str:
    """Текст отказа, если запуск не по режиму своего дерева; пусто — проход. Дверь --door не открывает."""
    isolated = tool_input.get("isolation") == "worktree"
    if kind == WORKER_TYPE and not isolated:
        return ("HELPER_WORKTREE=1: исполнитель работает в своём дереве. Повтори тот же вызов с "
                'полем isolation: "worktree". После отчёта: python3 scripts/merge-queue.py '
                "<ветка из отчёта>")
    if kind == LEAD_TYPE and isolated:
        return ("HELPER_WORKTREE=1: управляющий ведёт задачу в основном дереве: сливает очередью "
                "(python3 scripts/merge-queue.py) и пишет журнал. Повтори вызов без isolation.")
    return ""


def decide(payload: dict) -> tuple:
    """(код, stderr, additionalContext)."""
    if payload.get("tool_name") not in TOOLS:
        return 0, "", ""
    tool_input = payload.get("tool_input") or {}
    if worktree_mode():
        refusal = isolation_verdict(str(tool_input.get("subagent_type") or ""), tool_input)
        if refusal:
            label = LABEL.search(str(tool_input.get("prompt") or ""))
            task = label["path"].rstrip("/").split("/")[-1] if label else ""
            record("refused", task, str(payload.get("session_id") or ""),
                   {"отказ": "isolation", "тип": str(tool_input.get("subagent_type") or "")})
            return 2, refusal, ""
    prompt = str(tool_input.get("prompt") or "")
    kind = str(tool_input.get("subagent_type") or "")
    session = str(payload.get("session_id") or "")
    label = LABEL.search(prompt)
    if not label:
        record("skipped", "", session, {"почему": "без метки"})
        return 0, "", ""
    *chapter_path, task = label["path"].rstrip("/").split("/")
    chapter = "/".join(chapter_path) or None
    n = int(label["n"]) if label["n"] else None
    part = f"{label['path']}#{n}" if n is not None else label["path"]
    data = {"ид": uuid.uuid4().hex[:12], "часть": part, "тип": kind, "отказ": None,
            "предупреждение": False, "дверь": None}
    try:
        conf = settings()
        data["строгость"] = conf["строгость"]
        plan = read_plan(task, conf["окно_с"], conf["таймаут_с"])
        if plan.get("состояние") == "мелкая":
            raise Skip("мелкая")
    except Skip as skip:
        record("skipped", task, session, {**data, "почему": str(skip),
                                          **({"сбой": skip.failure} if skip.failure else {})})
        return 0, "", ""
    data.update(роль=plan.get("роль"), правило=plan.get("правило"))
    found = violations(plan, chapter, n, kind, prompt, conf["окно_с"])
    number = found[0] if found else None
    data.update(отказ=number, нарушения=found)
    notes = [f"План запуска {task} устарел: части правились после расчёта, "
             f"пересчитай (launch-plan.py {task})."] if stale(plan) else []
    if number is None:
        record("ok", task, session, data)
        return 0, "", " ".join(notes)
    text = text_for(plan, number)
    opened = door_reason()
    if opened:
        record("ok", task, session, {**data, "дверь": opened})
        return 0, "", " ".join(notes)
    if conf["строгость"] == STRICTNESS_RED:
        record("refused", task, session, data)
        return 2, f"{text}\n{DOOR_HINT}", ""
    record("ok", task, session, {**data, "предупреждение": True})
    return 0, "", " ".join([*notes, text])


def open_door(reason: str) -> int:
    reason = reason.strip()
    if len(reason) < DOOR_REASON_MIN:
        print(f"причина короче {DOOR_REASON_MIN} знаков: дверь не открыта", file=sys.stderr)
        return 1
    path = door_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"почему": reason, "до": now() + DOOR_TTL_S}, ensure_ascii=False),
                    encoding="utf-8")
    print("дверь открыта на 15 минут")
    return 0


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--door":
        return open_door(sys.argv[2])
    try:
        payload = json.load(sys.stdin)
        code, err, context = decide(payload) if isinstance(payload, dict) else (0, "", "")
    except Exception as exc:  # noqa: BLE001 — своя ошибка хука не роняет работу
        record("skipped", "", "", {"почему": f"{type(exc).__name__}: {exc}"[:300], "сбой": "сбой"})
        return 0
    if err:
        print(err, file=sys.stderr)
    if context:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                  "additionalContext": context}},
                         ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
