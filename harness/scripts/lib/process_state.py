"""process_state.py — состояние процесса: работает, не запущен, упал, пропущен.
Одна чистая функция resolve(records, facts, now) на панель и CLI: логика состояния не живёт
в двух местах. Факты собирают два сборщика:
  gather()       — быстрые (systemd, журнал запусков, последняя выборка, cgroup, расписание);
                   зовёт панель при открытии страницы и CLI;
  state_facts()  — медленные (docker, плановый пропуск, последний запуск по полному журналу);
                   зовёт диспетчер при сборке витрины, панель читает их из витрины.
Каждый внешний вызов — с таймаутом: висящий docker не должен остановить замер.
Двери проб — окружение, читается при каждом вызове: PROCESS_STATE_SYSTEMCTL, PROCESS_STATE_DOCKER,
PROCESS_STATE_ANALYZE, PROCESS_STATE_CGROUP, PROCESS_STATE_DEMON.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path

import yaml

import config
import process_library
import process_load

VALUES = ("running", "waiting", "stopped", "failed", "skipped", "disabled", "removed", "done", "idle", "unknown")
LABELS = {"running": "работает", "waiting": "ждёт запуска", "stopped": "не запущен", "failed": "упал",
          "skipped": "пропущен", "disabled": "погашен выключателем", "removed": "снят",
          "done": "отработал при загрузке", "idle": "сейчас не идёт", "unknown": "неизвестно"}
# Витрина и состояния из неё старше порога — «проверка N мин назад»; метка панели «витрина устарела» судит по нему же.
PROCESS_STALE_MIN = 30
# Диспетчер пишет выборку раз в минуту: старше 5 минут — он стоит, «работает» по ней не сказать.
SAMPLE_STALE_S = 300
RUNS_TAIL_BYTES = 2_000_000
SAMPLE_TAIL_BYTES = 512 * 1024
CALL_TIMEOUT_S = 2
DEMON_LAUNCHER = "scripts/claude-demon.sh"
MISSED_SLACK_DEFAULT_MIN = 15
UNTRANSLATABLE = "расписание не переводится в календарь systemd"
SHOW_PROPS = ("LoadState,ActiveState,SubState,Result,ExecMainStatus,NRestarts,"
              "ActiveEnterTimestamp,InactiveEnterTimestamp,ExecMainExitTimestamp")
DOCKER_FORMAT = "{{.Name}} {{.State.Status}} {{.State.ExitCode}} {{.RestartCount}} {{.State.StartedAt}}"
SCHEDULED_LAUNCHES = ("крон", "таймер", "служба-проверок")
TIMER_LAUNCHES = ("таймер", "служба-проверок")
ON_DEMAND_LAUNCHES = ("команда-сессии", "агент-сессии", "вызов-модели")
WHY_LIMIT = 120
OPEN_LAUNCH_MAX_MIN_DEFAULT = 240
HELPERS_LAUNCH = "агент-сессии"


class SourceError(Exception):
    """Источник не ответил: текст — причина для строки состояния, уже без аргументов команды."""


# ───────────────────────── внешние вызовы ─────────────────────────

def _reason(name: str, text: str) -> str:
    """Причина сбоя одной строкой: флаги вида --ключ=значение и пути вырезаны — в аргументах бывают секреты."""
    text = re.sub(r"--[\w-]+=\S*", "", text)
    text = re.sub(r"(?:/[\w.@+-]+)+", "…", text)
    return f"{name}: {' '.join(text.split())}"[:WHY_LIMIT]


def _run(name: str, program: str, args: list):
    try:
        return subprocess.run([program, *args], capture_output=True, text=True, timeout=CALL_TIMEOUT_S,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise SourceError(f"{name}: нет ответа за {CALL_TIMEOUT_S} с") from None
    except OSError as error:
        raise SourceError(_reason(name, error.strerror or type(error).__name__)) from None


def _stdout(name: str, program: str, args: list) -> str:
    done = _run(name, program, args)
    if done.returncode:
        raise SourceError(_reason(name, done.stderr or f"код {done.returncode}"))
    return done.stdout


def _systemctl(*args: str) -> str:
    return _stdout("systemctl", os.environ.get("PROCESS_STATE_SYSTEMCTL") or os.environ.get("PANEL_SYSTEMCTL")
                   or "systemctl", list(args))


# ───────────────────────── правила вида и кода выхода ─────────────────────────

def default_units(record: dict, suffix: str = ".service") -> list:
    """Имена юнита записи по соглашению: сначала <id>, затем harness-<id>."""
    return [f"{record['id']}{suffix}", f"harness-{record['id']}{suffix}"]


def unit_names(record: dict, suffix: str) -> list:
    """Юниты записи с этим суффиксом: названные записью (состояние_из, юниты), иначе по соглашению."""
    named = (record.get("состояние_из") or {}).get("systemd") or record.get("юниты") or []
    return [unit for unit in named if unit.endswith(suffix)] or default_units(record, suffix)


def kind_of(record: dict, records: list):
    """constant · scheduled · boot · on_demand; у хука состояния нет (None)."""
    launch = record.get("запуск")
    if launch in SCHEDULED_LAUNCHES:
        return "scheduled"
    if launch == "загрузка":
        return "boot"
    if launch in ON_DEMAND_LAUNCHES:
        return "on_demand"
    if launch == "контейнер":
        return "constant"
    if launch == "служба":
        return "on_demand" if _hosted_by_other(record, records) else "constant"
    return None


def _hosted_by_other(record: dict, records: list) -> bool:
    """Хозяин процесса — собственный юнит другой записи (части диспетчера); иначе юнит-хозяин — условие группы."""
    owned = {unit for other in records if other is not record for unit in default_units(other)}
    return any(unit in owned for unit in record.get("юниты") or ())


def switch_state(record: dict, exempt: bool, key: str) -> str:
    """идёт · погашен · вне · снят · иное: что выключатель делает с этой записью сейчас."""
    status = record.get("статус", "")
    if status.startswith("снят"):
        return "снят"
    if status != "работает":
        return "иное"
    if record["класс"] != "фоновый" or record["запуск"] == "служба-проверок":
        return "вне"
    return "идёт" if exempt or key == "on" else "погашен"


def limit_code() -> int:
    """Код выхода «кончился лимит подписки» — из claude-demon.sh, второй раз число не пишется."""
    path = Path(os.environ.get("PROCESS_STATE_DEMON") or Path(__file__).resolve().parent.parent / "claude-demon.sh")
    return int(re.search(r"^KOD_LIMITA=(\d+)", path.read_text(encoding="utf-8"), re.M).group(1))


def exit_state(record: dict, rc, limit: int) -> tuple:
    """(value или None, why) по коду выхода: код лимита значит «пропущен» только у демонов модели."""
    if not rc:
        return None, ""
    if rc == limit and (record.get("зовёт_модель") or {}).get("через") == DEMON_LAUNCHER:
        return "skipped", "лимит подписки"
    return "failed", f"код {rc}"


# ───────────────────────── разбор ответов ─────────────────────────

def _epoch(text):
    return process_load.parse_ts(text) if text else None


def _iso(moment: float) -> str:
    return dt.datetime.fromtimestamp(moment).astimezone().isoformat(timespec="seconds")


def clock(moment: float, now: float) -> str:
    """«ЧЧ:ММ» сегодня, «ДД.ММ ЧЧ:ММ» в другой день: голое время позавчера читается как сегодняшнее."""
    stamp = dt.datetime.fromtimestamp(moment)
    return stamp.strftime("%H:%M" if stamp.date() == dt.datetime.fromtimestamp(now).date() else "%d.%m %H:%M")


def _int(text) -> int:
    return int(text) if str(text or "").lstrip("-").isdigit() else 0


def _systemd_time(text):
    """«Wed 2026-11-11 18:40:14 +05» → эпоха; пусто («n/a», 0) → None."""
    found = re.search(r"(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)", text or "")
    return dt.datetime.strptime(f"{found[1]} {found[2]}", "%Y-%m-%d %H:%M:%S").timestamp() if found else None


def _docker_time(text):
    """StartedAt с наносекундами и «Z» → эпоха: fromisoformat держит не больше шести знаков дроби."""
    return process_load.parse_ts(re.sub(r"(\.\d{6})\d+", r"\1", text or ""))


def _tail(path: Path, size: int) -> tuple:
    """(строки хвоста файла, файл длиннее хвоста). Первая строка обрезанного хвоста может быть оборвана."""
    try:
        with open(path, "rb") as handle:
            total = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, total - size))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except FileNotFoundError:
        return [], False
    return (lines[1:], True) if total > size else (lines, False)


def last_runs(lines: list, proc: str = "/proc") -> dict:
    """{id: {at, rc, outcome, seconds}}: последний process.end каждого id; открытый start новее него —
    «running» (pid жив по снимку proc) или «torn» (оборван)."""
    alive = process_load.process_alive(proc)
    last, open_runs = {}, {}
    for row in process_load.jsonl_rows(line for line in lines if '"process.start"' in line or '"process.end"' in line):
        data = row.get("data") or {}
        if not data.get("id") or _epoch(row.get("ts")) is None:
            continue
        key = (data["id"], data.get("pid"))
        if row.get("event") == "process.start":
            open_runs[key] = row["ts"]
        elif row.get("event") == "process.end":
            open_runs.pop(key, None)
            last[data["id"]] = {"at": row["ts"], "rc": data.get("rc"), "outcome": row.get("outcome"),
                                "seconds": data.get("с")}
    for (ident, pid), started in open_runs.items():
        if ident in last and _epoch(last[ident]["at"]) > _epoch(started):
            continue
        last[ident] = {"at": started, "rc": None, "seconds": None,
                       "outcome": "running" if alive(pid, _epoch(started)) else "torn"}
    return last


def _first_ts(lines: list):
    return next((row["ts"] for row in process_load.jsonl_rows(lines) if _epoch(row.get("ts")) is not None), None)


# ───────────────────────── следующий запуск ─────────────────────────

def next_calendar(expressions: dict, base=None) -> dict:
    """{ключ: эпоха следующего момента или None} по выражениям OnCalendar — одним вызовом systemd-analyze.
    Календарь считает systemd: своего разбора полей крона рядом с on_calendar нет."""
    if not expressions:
        return {}
    args = ["calendar", *([f"--base-time=@{int(base)}"] if base is not None else []), *expressions.values()]
    out = _stdout("systemd-analyze", os.environ.get("PROCESS_STATE_ANALYZE") or "systemd-analyze", args)
    blocks = [block for block in re.split(r"\n\s*\n", out.strip()) if "Next elapse:" in block]
    if len(blocks) != len(expressions):
        raise SourceError(f"systemd-analyze: ответов {len(blocks)} на {len(expressions)} выражений")
    return dict(zip(expressions, map(_calendar_moment, blocks)))


def _calendar_moment(block: str):
    """«(in UTC)» однозначен; без него (машина в UTC) строка «Next elapse» уже в местном времени."""
    utc = re.search(r"\(in UTC\):\s*\S+ (\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)", block)
    if utc:
        return dt.datetime.strptime(f"{utc[1]} {utc[2]}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
    return _systemd_time(block.split("Next elapse:", 1)[1].splitlines()[0])


def _cron_calendars(records: list, schedule: dict) -> tuple:
    """({id: OnCalendar}, {id: подсказка}): крон не «снят»; расписание, которое systemd не знает, — с подсказкой."""
    calendars, notes = {}, {}
    for record in records:
        if record.get("запуск") != "крон" or record.get("статус", "").startswith("снят"):
            continue
        try:
            calendars[record["id"]] = process_library.on_calendar((schedule.get(record["id"]) or {})["расписание"])
        except (KeyError, ValueError):
            notes[record["id"]] = UNTRANSLATABLE
    return calendars, notes


def _missed_slack_s() -> float:
    value = os.environ.get("MISSED_SLACK_MIN") or config.из_файла(config.путь_harness_conf()).get("MISSED_SLACK_MIN")
    return 60 * float(value or MISSED_SLACK_DEFAULT_MIN)


def _missed(records: list, schedule: dict, last: dict, log_start, now: float) -> dict:
    """{id крона: ISO планового момента, который не состоялся, или None}. База — конец последнего запуска,
    нет запусков — начало журнала: до него пропуск не засчитать."""
    calendars, _ = _cron_calendars(records, schedule)
    missed = {}
    try:
        for ident, expression in calendars.items():
            base = _epoch(last[ident]["at"]) if ident in last else _epoch(log_start) or now
            moment = next_calendar({ident: expression}, base)[ident]
            missed[ident] = _iso(moment) if moment is not None and moment < now - _missed_slack_s() else None
    except SourceError:
        missed.update({ident: None for ident in calendars if ident not in missed})
    return missed


# ───────────────────────── сборщики фактов ─────────────────────────

def _docker_facts(records: list) -> tuple:
    names = [r["состояние_из"]["docker"] for r in records if (r.get("состояние_из") or {}).get("docker")]
    if not names:
        return {}, None
    try:
        done = _run("docker", os.environ.get("PROCESS_STATE_DOCKER") or "docker", ["inspect", "-f", DOCKER_FORMAT, *names])
    except SourceError as error:
        return {}, str(error)
    # Контейнера нет — ответ, а не сбой: docker отвечает кодом 1 и «No such object».
    errors = [line for line in done.stderr.splitlines() if line.strip() and "No such object" not in line]
    if done.returncode and errors:
        return {}, _reason("docker", " ".join(errors))
    found = {}
    for line in done.stdout.splitlines():
        name, status, code, restarts, started = line.split()
        found[name.lstrip("/")] = {"status": status, "exit_code": int(code), "restarts": int(restarts),
                                   "started_at": started}
    return found, None


def state_facts(records: list, schedule: dict, lines: list, now: float, proc: str = "/proc") -> dict:
    """Медленные факты витрины: из строк журнала запусков, которые сборка уже прочла целиком."""
    last, log_start = last_runs(lines, proc), _first_ts(lines)
    docker, docker_error = _docker_facts(records)
    return {"checked": _iso(now), "docker": docker, "docker_error": docker_error,
            "missed": _missed(records, schedule, last, log_start, now), "log_start": log_start, "last_run": last}


def systemd_units(record: dict, kind: str) -> list:
    """Юниты, которые надо спросить у systemd для этой записи."""
    source = record.get("состояние_из") or {}
    if record.get("статус", "").startswith("снят") or ({"docker", "cgroup"} & set(source)):
        return []
    if kind in ("constant", "boot"):
        return unit_names(record, ".service")
    if kind == "scheduled" and record["запуск"] in TIMER_LAUNCHES:
        return unit_names(record, ".timer") + unit_names(record, ".service")
    return []


def _show(units: list) -> dict:
    blocks = _systemctl("show", *units, "-p", SHOW_PROPS).strip().split("\n\n")
    return {unit: dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            for unit, block in zip(units, blocks)}


def _systemd_facts(records: list) -> dict:
    kinds = [(record, kind_of(record, records)) for record in records]
    units = list(dict.fromkeys(unit for record, kind in kinds for unit in systemd_units(record, kind)))
    facts = {"units": {}, "systemd_error": None, "timers": {}, "failed_units": set()}
    try:
        facts["units"] = _show(units) if units else {}
        for entry in json.loads(_systemctl("list-timers", "harness-*", "logrotate*", "--all", "--output=json") or "[]"):
            facts["timers"][entry["unit"]] = (entry.get("next") or 0) / 1_000_000 or None
        failed = json.loads(_systemctl("list-units", "--state=failed", "--output=json", "--no-pager") or "[]")
        facts["failed_units"] = {entry["unit"] for entry in failed}
    except SourceError as error:
        facts["systemd_error"] = str(error)
    return facts


def _sample_facts(logs) -> dict:
    """Группы последней выборки диспетчера: {id: число групп с процессов > 0}; живой считается только такая."""
    lines, _ = _tail(Path(logs) / "process-load.jsonl", SAMPLE_TAIL_BYTES)
    for row in reversed(list(process_load.jsonl_rows(line for line in lines if '"process.sample"' in line))):
        at = _epoch(row.get("ts"))
        groups = {}
        for group in (row.get("data") or {}).get("группы") or []:
            groups[group["id"]] = groups.get(group["id"], 0) + (1 if group.get("процессов", 0) > 0 else 0)
        if at is not None:
            return {"at": at, "groups": groups}
    return None


def _open_launch_max_s() -> float:
    """Предел «висящего» запуска из данных режима скорости (дверь SPEED_MODE_DATA); нет файла — запасной."""
    path = Path(os.environ.get("SPEED_MODE_DATA") or config.корень_дерева() / "harness" / "config" / "speed-modes.yaml")
    try:
        minutes = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("открытый_запуск_макс_мин")
    except (OSError, yaml.YAMLError):
        minutes = None
    return 60 * float(minutes or OPEN_LAUNCH_MAX_MIN_DEFAULT)


def _agent_facts(logs, now: float) -> dict:
    """Помощники: число agent.launch (outcome ok) без agent.finish по data.вызов. Запуск старше предела —
    завис без finish (сессия умерла), живым не считается. Нет журнала — открытых нет."""
    lines, _ = _tail(Path(logs) / "вызовы-агентов.jsonl", RUNS_TAIL_BYTES)
    opened, finished = {}, set()
    for row in process_load.jsonl_rows(line for line in lines if '"agent.launch"' in line or '"agent.finish"' in line):
        call = (row.get("data") or {}).get("вызов")
        if not call:
            continue
        if row.get("event") == "agent.finish":
            finished.add(call)
        elif row.get("event") == "agent.launch" and row.get("outcome") == "ok":
            opened[call] = _epoch(row.get("ts"))
    limit = _open_launch_max_s()
    return {"open": sum(1 for call, at in opened.items() if call not in finished and at is not None and now - at <= limit)}


def _cgroup_facts(records: list) -> dict:
    root = Path(os.environ.get("PROCESS_STATE_CGROUP") or "/sys/fs/cgroup")
    facts = {}
    for record in records:
        name = (record.get("состояние_из") or {}).get("cgroup")
        if not name:
            continue
        try:
            events = dict(line.split() for line in (root / name / "cgroup.events").read_text().splitlines())
        except OSError:
            facts[name] = None
            continue
        facts[name] = {"populated": _int(events.get("populated")), "frozen": _int(events.get("frozen"))}
    return facts


def gather(records: list, schedule: dict, logs, slow, now: float, key: str, exempt, outside=(),
           proc: str = "/proc") -> dict:
    """Быстрые факты для resolve. slow — state_facts витрины или None; key — on/off выключателя;
    exempt — id исключений выключателя; outside — id строк витрины вне библиотеки."""
    lines, partial = _tail(Path(logs) / "process-runs.jsonl", RUNS_TAIL_BYTES)
    first = _first_ts(lines)
    facts = {"slow": slow, "key": key, "exempt": set(exempt), "outside": list(outside), "limit": limit_code(),
             "runs": {"last": last_runs(lines, proc), "start": _epoch(first), "partial": partial},
             "sample": _sample_facts(logs), "agents": _agent_facts(logs, now), "cgroup": _cgroup_facts(records), **_systemd_facts(records)}
    calendars, facts["calendar_notes"] = _cron_calendars(records, schedule)
    facts["calendar"] = {}
    try:
        facts["calendar"] = next_calendar(calendars)
    except SourceError as error:
        facts["calendar_notes"].update({ident: str(error) for ident in calendars})
    return facts


# ───────────────────────── разрешитель ─────────────────────────

def _entry(kind, value: str, why: str = "", **fields) -> dict:
    return {"kind": kind, "value": value, "why": why[:WHY_LIMIT], "since": None, "restarts": None, "running_n": None,
            "source": None, "checked": None, "last_run": None, "next_run": None, "next_note": "", **fields}


def _unknown(kind, why: str, source: str, checked) -> dict:
    return _entry(kind, "unknown", why, source=source, checked=checked)


def _pick_unit(record: dict, suffix: str, facts: dict):
    """Свойства первого найденного юнита записи; None — не найден ни один."""
    for unit in unit_names(record, suffix):
        props = facts["units"].get(unit)
        if props and props.get("LoadState") != "not-found":
            return unit, props
    return None, None


def _unit_verdict(record: dict, props: dict, limit: int) -> tuple:
    """(value, why, since) по ActiveState юнита."""
    if props.get("ActiveState") == "failed":
        code = _int(props.get("ExecMainStatus"))
        if exit_state(record, code, limit)[0] == "skipped":
            return "skipped", "лимит подписки", None
        return "failed", f"systemd: {props.get('Result')}, код {code}", None
    if props.get("ActiveState") in ("active", "activating", "reloading"):
        return "running", "", _systemd_time(props.get("ActiveEnterTimestamp"))
    return "stopped", "", _systemd_time(props.get("InactiveEnterTimestamp"))


def _systemd_missing(record: dict, kind: str, facts: dict, now: float):
    """Строка unknown, если спросить systemd не вышло или юнита нет; иначе None."""
    if facts["systemd_error"]:
        return _unknown(kind, facts["systemd_error"], "systemd", now)
    if _pick_unit(record, ".service", facts)[1] is None:
        return _unknown(kind, "юнит не найден", "systemd", now)
    return None


def _group_state(record: dict, facts: dict, now: float, kind: str):
    """(running_n, ответ-unknown): выборка диспетчера — условие «процесс записи жив»."""
    sample = facts["sample"]
    if sample is None:
        return None, _unknown(kind, "выборок диспетчера нет", "выборки диспетчера", None)
    if now - sample["at"] > SAMPLE_STALE_S:
        return None, _unknown(kind, "выборки старше 5 мин", "выборки диспетчера", sample["at"])
    return sample["groups"].get(record["id"], 0), None


def _constant(record: dict, facts: dict, now: float) -> dict:
    source = record.get("состояние_из") or {}
    if "docker" in source:
        return _docker_state(record, source["docker"], facts, now)
    if "cgroup" in source:
        return _cgroup_state(source["cgroup"], facts, now)
    missing = _systemd_missing(record, "constant", facts, now)
    if missing:
        return missing
    props = _pick_unit(record, ".service", facts)[1]
    value, why, since = _unit_verdict(record, props, facts["limit"])
    found = _entry("constant", value, why, since=since, restarts=_int(props.get("NRestarts")), source="systemd",
                   checked=now)
    if value == "running" and record.get("юниты"):
        found = _hosted_state(record, found, facts, now)
    return found


def _hosted_state(record: dict, found: dict, facts: dict, now: float) -> dict:
    """claude-session: юнит harness-agent.service держит tmux и жив, даже когда сам claude лёг."""
    count, unknown = _group_state(record, facts, now, "constant")
    if unknown:
        return unknown
    if not count:
        return _unknown("constant", "юнит жив, процесс сессии не найден", "выборки диспетчера", facts["sample"]["at"])
    return {**found, "running_n": count}


def _docker_state(record: dict, name: str, facts: dict, now: float) -> dict:
    slow = facts["slow"]
    if slow is None:
        return _unknown("constant", "в витрине диспетчера нет фактов docker", "docker", None)
    checked = _epoch(slow["checked"])
    if now - checked > PROCESS_STALE_MIN * 60:
        return _unknown("constant", f"проверка {round((now - checked) / 60)} мин назад", "docker", checked)
    if slow.get("docker_error"):
        return _unknown("constant", slow["docker_error"], "docker", checked)
    found = (slow.get("docker") or {}).get(name)
    if found is None:
        return _unknown("constant", "контейнер не найден", "docker", checked)
    status, code = found["status"], found["exit_code"]
    value, why = {"running": ("running", ""), "restarting": ("failed", "перезапускается раз за разом"),
                  "created": ("stopped", "создан, не запущен"), "paused": ("stopped", "приостановлен"),
                  "exited": ("stopped", "") if code == 0 else ("failed", f"docker: exited ({code})"),
                  "dead": ("failed", f"docker: dead ({code})")}.get(status, ("unknown", f"docker: статус {status}"))
    since = _docker_time(found["started_at"]) if value == "running" else None
    return _entry("constant", value, why, since=since, restarts=found["restarts"], source="docker", checked=checked)


def _cgroup_state(name: str, facts: dict, now: float) -> dict:
    events = facts["cgroup"].get(name)
    if events is None or not events["populated"]:
        value, why = "stopped", "контейнер не виден"
    elif events["frozen"]:
        value, why = "stopped", "заморожен"
    else:
        value, why = "running", ""
    return _entry("constant", value, why, source="cgroup", checked=now)


def _boot(record: dict, facts: dict, now: float) -> dict:
    missing = _systemd_missing(record, "boot", facts, now)
    if missing:
        return missing
    props = _pick_unit(record, ".service", facts)[1]
    value, why, since = _unit_verdict(record, props, facts["limit"])
    if props.get("SubState") == "exited" and value == "running" or value == "stopped" and props.get("Result") == "success":
        value = "done"
    return _entry("boot", value, why, since=since, restarts=_int(props.get("NRestarts")), source="systemd", checked=now)


def _helpers(facts: dict, now: float) -> dict:
    count = facts["agents"]["open"]
    return _entry("on_demand", "running" if count else "idle", running_n=count or None,
                  source="журнал вызовов агентов", checked=now)


def _on_demand(record: dict, facts: dict, now: float) -> dict:
    if record["запуск"] == HELPERS_LAUNCH:
        return _helpers(facts, now)
    count, unknown = _group_state(record, facts, now, "on_demand")
    if unknown:
        return unknown
    return _entry("on_demand", "running" if count else "idle", running_n=count or None,
                  source="выборки диспетчера", checked=facts["sample"]["at"])


def _last_run(ident: str, facts: dict) -> tuple:
    """(последний запуск, дыра). Хвост перекрывает факт витрины событием новее; хвост короче журнала и
    начинается позже снятия витрины — между ними дыра: что было в ней, неизвестно."""
    runs, slow = facts["runs"], facts["slow"]
    tail = runs["last"].get(ident)
    checked = _epoch((slow or {}).get("checked"))
    if tail is None and runs["partial"] and (checked is None or runs["start"] is None or runs["start"] > checked):
        return None, True
    found = [run for run in (tail, ((slow or {}).get("last_run") or {}).get(ident)) if run]
    return max(found, key=lambda run: _epoch(run["at"]), default=None), False


def _unit_last_run(record: dict, facts: dict):
    """Запуск по юниту службы: у записи без обёртки (logrotate) журнала запусков нет."""
    props = _pick_unit(record, ".service", facts)[1] or {}
    moment = _systemd_time(props.get("ExecMainExitTimestamp"))
    if moment is None:
        return None
    ok = props.get("Result") == "success"
    return {"at": _iso(moment), "rc": 0 if ok else _int(props.get("ExecMainStatus")) or 1,
            "outcome": "ok" if ok else "error", "seconds": None}


def _next_run(record: dict, facts: dict) -> tuple:
    if record["запуск"] == "крон":
        note = facts["calendar_notes"].get(record["id"], "")
        return (None, note) if note else (facts["calendar"].get(record["id"]), "")
    timer = _pick_unit(record, ".timer", facts)[0]
    return facts["timers"].get(timer), ""


def _scheduled(record: dict, facts: dict, now: float) -> dict:
    ident = record["id"]
    if switch_state(record, ident in facts["exempt"], facts["key"]) == "погашен":
        return _entry("scheduled", "disabled", source="выключатель", checked=now)
    has_unit = record["запуск"] in TIMER_LAUNCHES
    if has_unit and facts["systemd_error"]:
        return _unknown("scheduled", facts["systemd_error"], "systemd", now)
    next_run, next_note = _next_run(record, facts)
    shown = {"next_run": next_run, "next_note": next_note, "source": "журнал запусков", "checked": now}
    last, hole = _last_run(ident, facts)
    if last is None and has_unit:
        last = _unit_last_run(record, facts)
    if last and last["outcome"] == "running":
        return _entry("scheduled", "running", since=_epoch(last["at"]), last_run=last, **shown)
    timer = _pick_unit(record, ".timer", facts)[1] if has_unit else None
    if has_unit and timer is None:
        return _unknown("scheduled", "юнит не найден", "systemd", now)
    if timer and timer.get("ActiveState") != "active":
        return _entry("scheduled", "stopped", "таймер выключен", last_run=last, **shown)
    if hole:
        return _hole_unknown(facts, now, shown)
    return _from_last_run(record, last, facts, now, shown)


def _hole_unknown(facts: dict, now: float, shown: dict) -> dict:
    checked = _epoch((facts["slow"] or {}).get("checked"))
    why = "журнал длиннее прочитанного хвоста, витрины диспетчера нет" if checked is None \
        else f"проверка {round((now - checked) / 60)} мин назад"
    return _entry("scheduled", "unknown", why, **shown)


def _from_last_run(record: dict, last, facts: dict, now: float, shown: dict) -> dict:
    if last is not None:
        at = _epoch(last["at"])
        value, why = ("failed", "оборван") if last["outcome"] == "torn" else exit_state(record, last["rc"], facts["limit"])
        if value:
            return _entry("scheduled", value, f"{why} в {clock(at, now)}", last_run=last, **shown)
    slow = facts["slow"]
    missed = _epoch(((slow or {}).get("missed") or {}).get(record["id"]))
    if missed is not None and (last is None or _epoch(last["at"]) < missed):
        checked = _epoch(slow["checked"])
        seen = {**shown, "source": "витрина диспетчера", "checked": checked}
        # Старая витрина не знает, что было после неё: «пропущен» из неё — уже догадка (§4.2).
        if now - checked > PROCESS_STALE_MIN * 60:
            return _entry("scheduled", "unknown", f"проверка {round((now - checked) / 60)} мин назад", last_run=last, **seen)
        return _entry("scheduled", "skipped", f"плановый {clock(missed, now)} не состоялся", last_run=last, **seen)
    return _entry("scheduled", "waiting", "" if last else _never_started(facts), last_run=last, **shown)


def _never_started(facts: dict) -> str:
    start = _epoch((facts["slow"] or {}).get("log_start")) or facts["runs"]["start"]
    return "ещё не запускался" + (f" с {dt.datetime.fromtimestamp(start):%d.%m %H:%M}" if start else "")


_KINDS = {"constant": _constant, "scheduled": _scheduled, "boot": _boot, "on_demand": _on_demand}


def resolve(records: list, facts: dict, now: float) -> dict:
    """{id: состояние} по каждой записи библиотеки, кроме хуков, и по строкам вне библиотеки с упавшим юнитом.
    Ключи берутся разбором записей, а не выборок: упавшая служба без выборок не пропадает."""
    states = {}
    for record in records:
        kind = kind_of(record, records)
        if kind is None:
            continue
        removed = record.get("статус", "").startswith("снят")
        states[record["id"]] = _entry(kind, "removed") if removed else _KINDS[kind](record, facts, now)
    for ident in facts["outside"]:
        if ident in facts["failed_units"] and ident not in states:
            states[ident] = _entry(None, "failed", "systemd: упал", source="systemd", checked=now)
    return states
