#!/usr/bin/env python3
"""Пробы судьи режима скорости (`harness/demons/speed_mode.py`, Ч2 hr.speed-modes).

Контракт — `.tasks/hr.speed-modes/ч2/контракт.md`; критерии — spec.md 10.3 п. 1–6, 8а, 16.
Судья решает, сколько задач карты вести одновременно: счёт задач в работе по журналу
запусков, «ждёт владельца», лестница мощности с гистерезисом, авария, пауза, лимит по факту
429 и по расходу, выкат без захвата лока, ответ сторожу. Ошибка любого из них — либо пинок
«возьми ещё» на перегруженном сервере, либо молчание при простое.

Каждый случай строит свой стенд во временном каталоге: свои install.conf и harness.conf,
LOG_DIR, panel-state, карта линии `h`, каталог вместо /proc (`SPEED_PROC_DIR`), транскрипты
(`CLAUDE_PROJECTS_DIR`), файл лимитов и свои данные (копия блока спеки 4.3). Живые данные
не читаются и не пишутся, кроме случая `data_live` (только чтение файла данных). Часы судьи —
`--на`. Судья зовётся процессом с пределом 20 с, публичные функции — драйвером в отдельном
процессе. Списки, которые судятся, берутся разбором вывода: пустой там, где ждём непустой, —
красный.

Двери: SPEED_MODE_BIN — судья (копия под мутацией), SPEED_MODE_DATA — файл данных для
`data_live`, WORK_QUEUE_BIN — проходит к судье как есть; SPEED_GATES_SH, SPEED_MECHANISMS,
SPEED_SIGNALS — ворота и реестры для случаев регистрации Ч3 (копии под мутацией).

Ч3 (spec.md 8.3, 8.5) — толкование там, где спека неоднозначна, взято минимальное:
- `--журнал [--за Nч] [--на ISO]` — только чтение, код 0, состояние судьи не нужно и не
  пишется. Лента — события `$LOG_DIR/режим-скорости.jsonl` (любой писатель: судья и сторож)
  плюс только `agent.launch` / `agent.finish` из `$LOG_DIR/вызовы-агентов.jsonl`; прочие
  журналы и прочие события вызовов не попадают. Одна строка на событие, строка начинается с
  «ЧЧ:ММ » (местное время ts), по возрастанию времени через оба файла; битая строка
  пропускается. Содержимое: mode.decision — `data.причина` дословно; mode.change —
  «было→стало»; speed.kick — id из `data.предложено`; task.taken — `data.id`; agent.launch —
  слово «запуск» и id задачи из `data.часть` («Часть: <id>#N»); agent.finish — слово «конец».
  `--за 2ч` — только события не старше 2 ч от часов; негодное `--за` — код 1. Пусто — код 0,
  непустой вывод без строк со временем (молчание не ответ).
- Ворота: две строки `проверка "…"` в scripts/gates.sh — `python3 scripts/test_speed_mode.py`
  и `bash scripts/test_warden_speed.sh` (8.5); паспорт test_speed_mode.py после этого не
  говорит «не включена».
- Паспорта test_warden_speed.sh и test_dispatcher_speed.sh: вид гейт, тот же набор полей,
  что у паспорта test_speed_mode.py, `проверяется` зовёт сам файл, `доказательство` — живой
  файл (сторож / диспетчер).
- signals.yaml: причина launch-plan.py в «знают_сами» факта «какая задача идёт сейчас»
  называет судью (`speed_mode`) — число задач в работе теперь его.

Прогон (в фоне, по правилам проекта):
    python3 scripts/test_speed_mode.py [--случай имя]
Последняя строка: «СКОРОСТЬ: проб N, проверок K, провалов M»; код 0 — все зелёные.
"""
from __future__ import annotations

import copy
import ctypes
import datetime as dt
import fcntl
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def tree_file(*parts: str) -> Path:
    # Проект: scripts/ рядом с harness/; пакет: harness/scripts/ рядом с demons/ и config/.
    for candidate in (HERE.parent / "harness" / Path(*parts), HERE.parent / Path(*parts)):
        if candidate.is_file():
            return candidate
    return HERE.parent / "harness" / Path(*parts)


JUDGE = Path(os.environ.get("SPEED_MODE_BIN") or tree_file("demons", "speed_mode.py"))
LIVE_DATA = Path(os.environ.get("SPEED_MODE_DATA") or tree_file("config", "speed-modes.yaml"))
JUDGE_SRC = "harness/demons/speed_mode.py"
GATES_SH = Path(os.environ.get("SPEED_GATES_SH") or HERE / "gates.sh")
MECHANISMS = Path(os.environ.get("SPEED_MECHANISMS") or tree_file("config", "mechanisms.yaml"))
SIGNALS = Path(os.environ.get("SPEED_SIGNALS") or tree_file("config", "signals.yaml"))
WARDEN_SRC = "harness/demons/session-warden.sh"
JOURNAL = "режим-скорости.jsonl"
TIMEOUT = 20
LINE = "h"
MIN = 60
HOUR = 3600
CPUS = 4
BASE = int(time.time()) // MIN * MIN
# Пороги мощности по доле CPU за интервал замера (hr.speed-uses-whole-server): ключи harness.conf
# стенда. Кража — по-прежнему «мощность.кража_выше_pct» данных (15).
CPU_BUSY_MAX = 90
CPU_BUSY_QUIET = 75
CPU_IOWAIT_MAX = 20

# Копия блока спеки 4.3: пробы судят поведение на известных числах, а не на живых данных,
# которые калибруются. Живые данные сверяет со структурой этого блока случай data_live.
STAND_DATA = {
    "режимы": {
        "slow": {"задач": 1, "имя": "медленный", "живых_потолок": 3},
        "medium": {"задач": 3, "имя": "средний", "живых_потолок": 5},
        "fast": {"задач": 5, "имя": "быстрый", "живых_потолок": 8},
        "ultra": {"задач": 10, "имя": "ультра", "живых_потолок": 12},
    },
    "живых_при_аварии": 2,
    "по_умолчанию": "medium",
    "ступени": [1, 3, 5, 10],
    "открытый_запуск_макс_мин": 240,
    "старт_ждать_с": 120,
    "спек_ждут_владельца_максимум": 3,
    "блокеры": {
        "пауза": {"потолок": 0, "держится_мин": 0, "отпускает_мин": 0, "говорить": "нет"},
        "лимит": {"потолок": 0, "держится_мин": 0, "отпускает_мин": 0, "говорить": "нет"},
        "лимит_расход": {"потолок": "вычисляемый", "держится_мин": 0, "отпускает_мин": 0,
                         "говорить": "да"},
        "выкат": {"потолок": "заморозка", "держится_мин": 0, "отпускает_мин": 0,
                  "говорить": "нет"},
        "ротация": {"потолок": "заморозка", "держится_мин": 0, "отпускает_мин": 0,
                    "говорить": "нет"},
        "мощность": {"потолок": "ступень_вниз",
                     "кража_выше_pct": 15, "держится_мин": 10, "отпускает_мин": 20,
                     "говорить": "да"},
        "авария": {"потолок": 1, "нагрузка_на_ядро_выше": 4.0, "диска_выше_pct": 90,
                   "держится_мин": 4, "отпускает_мин": 20, "говорить": "да"},
        "мало_задач": {"потолок": "в_работе_плюс_готовые", "держится_мин": 0,
                       "отпускает_мин": 0, "говорить": "да"},
        "файлы": {"потолок": "ступень_вниз", "предупреждений_за_30_мин": 3, "держится_мин": 0,
                  "отпускает_мин": 30, "говорить": "да"},
    },
    "сторож": {
        "пинок_не_чаще_мин": 20,
        "после_подъёма_ждать_мин": 20,
        "в_зоне_добирать_по": 1,
        "лишнее_не_чаще_мин": 60,
        "эскалация_после_пинков": 3,
        "эскалация_не_чаще_мин": 720,
    },
    "сообщения": {"не_чаще_мин": 30},
    "лимит_подписки": {
        "адаптер": "claude",
        "файл_claude": "",
        "устарел_мин": 15,
        "порог_прогноза_pct": 95,
        "стоп_pct": 98,
        "окно_расхода_мин": {"пять_часов": 60, "неделя": 360},
        "мин_задача_часов": {"пять_часов": 0.5, "неделя": 3},
        "скорость_без_данных_pct_ч_на_задачу": {"пять_часов": 8, "неделя": 0.5},
        "подъём_не_чаще_мин": 20,
        "запас_подъёма_pct": 3,
        "сброс_падение_pct": 10,
    },
}
REQUIRED_BLOCKERS = set(STAND_DATA["блокеры"])

# Ключи живого окружения, которые судья или зовомые им модули читают раньше файла:
# оставленные, они играли бы пробу на живых значениях.
STRIP_ENV = ("SPEED_MODE", "AGENT_LEVEL", "AUTOMATION_LEVEL", "HARNESS_TASK", "HELPER_HUNG_MIN",
             "LOAD_MAX_PER_CPU", "MEM_MIN_FREE_PCT", "SWAP_MAX_PCT", "LIMIT_STALE_MIN",
             "ROTATE_FLAG_STALE_MIN", "CLAUDE_CODE_SESSION_ID", "ACTIVE_LINE", "HARNESS_HOME",
             "HARNESS_JOURNALS", "SPEED_MSG_WINDOW_MIN", "SPEED_TG_SEND")

DRIVER = r'''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("speed_mode", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(json.dumps(getattr(mod, sys.argv[2])(*json.loads(sys.argv[3])), ensure_ascii=False))
'''

# Неожиданное исключение в решении стенд создаёт подменой decide: порча данных была бы
# ошибкой данных (код 1), а пробе нужен сбой, которого судья не предвидел.
BROKEN_DECIDE_DRIVER = r'''
import importlib.util, sys
spec = importlib.util.spec_from_file_location("speed_mode", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def broken(*args, **kwargs):
    raise RuntimeError("проба: неожиданный сбой решения")

mod.decide = broken
sys.exit(mod.main(sys.argv[2:]))
'''


class ProbeError(Exception):
    """Стенд не смог получить ответ судьи — случай красный с этой причиной."""


RESULTS: list[tuple[str, bool, str]] = []
CASES = []


def check(name: str, ok, detail="") -> bool:
    RESULTS.append((name, bool(ok), str(detail)))
    print(f"  {'ок   ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f": {detail}"), flush=True)
    return bool(ok)


def case(fn):
    CASES.append(fn)
    return fn


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def epoch(text) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(text)).timestamp()
    except ValueError:
        return None


def number(value: str) -> float | None:
    try:
        return float(str(value).strip().split()[0].replace(",", "."))
    except (ValueError, IndexError):
        return None


class Stand:
    """Стенд одного случая; всё, что судья читает и пишет, лежит под self.root."""

    def __init__(self, mode: str = "medium", cards=(), data_patch=None):
        self.root = Path(tempfile.mkdtemp(prefix="speed-mode-probe-"))
        self.log = self.root / "log"
        self.panel = self.root / "panel"
        self.state_root = self.root / "state"
        self.proc = self.root / "proc"
        self.project = self.root / "proj"
        self.projects = self.root / "projects"
        self.transcripts = self.projects / str(self.project).replace("/", "-")
        for folder in (self.log, self.panel, self.state_root / "heartbeat", self.proc / "pressure",
                       self.project, self.transcripts):
            folder.mkdir(parents=True, exist_ok=True)
        self.limits_file = self.panel / "лимиты-подписки.json"
        self.data = copy.deepcopy(STAND_DATA)
        self.data["лимит_подписки"]["файл_claude"] = str(self.limits_file)
        for path, value in (data_patch or {}).items():
            node = self.data
            *head, last = path.split(".")
            for key in head:
                node = node[key]
            if value is None:
                node.pop(last, None)
            else:
                node[last] = value
        self.conf = {"SPEED_MODE": mode, "LOAD_MAX_PER_CPU": "2.0", "MEM_MIN_FREE_PCT": "10",
                     "CPU_BUSY_MAX_PCT": str(CPU_BUSY_MAX), "CPU_BUSY_QUIET_PCT": str(CPU_BUSY_QUIET),
                     "CPU_IOWAIT_MAX_PCT": str(CPU_IOWAIT_MAX),
                     "SWAP_MAX_PCT": "85", "AUTOMATION_LEVEL": "partial",
                     "HELPER_HUNG_MIN": "120", "ACTIVE_LINE": LINE}
        self.cards = [dict(c) for c in cards]
        self.load = 0.5
        self.steal_pct = 0.0
        self.affinity = CPUS  # ядер, доступных процессам судьи (cgroup cpuset); по умолчанию — все
        self.mem_free_pct = 50.0
        self.swap_pct = 0.0
        # Доли процессорного времени за проход (тик): занято (вместе с кражей), ожидание диска;
        # остальное — простой. Счётчики /proc/stat — их накопленная сумма.
        self.busy_pct = 30.0
        self.iowait_pct = 0.0
        self.cpu = {"user": 0, "idle": 0, "iowait": 0, "steal": 0}
        (self.proc / "locks").write_text("", encoding="utf-8")
        self.write_all()

    # ── файлы стенда ─────────────────────────────────────────────────────────
    def write_all(self) -> None:
        (self.root / "install.conf").write_text(
            f'LOG_DIR="{self.log}"\nSTATE_DIR="{self.state_root}"\n'
            f'PANEL_STATE_DIR="{self.panel}"\nHEARTBEAT_DIR="{self.state_root}/heartbeat"\n'
            f'SECRETS_DIR="{self.state_root}/secrets"\nPROJECT_DIR="{self.project}"\n',
            encoding="utf-8")
        self.write_conf()
        self.write_map()
        (self.root / "speed-modes.yaml").write_text(
            yaml.safe_dump(self.data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.write_proc()

    def write_conf(self) -> None:
        (self.root / "harness.conf").write_text(
            "".join(f'{k}="{v}"\n' for k, v in self.conf.items()), encoding="utf-8")

    def write_map(self) -> None:
        tasks = [{"title": c["id"], **c} for c in self.cards]
        (self.root / "dev-map.yaml").write_text(yaml.safe_dump(
            {"lines": [{"id": LINE, "name": "стенд", "tasks": tasks}]},
            allow_unicode=True, sort_keys=False), encoding="utf-8")

    def set_mode(self, mode: str) -> None:
        self.conf["SPEED_MODE"] = mode
        self.write_conf()

    def set_cards(self, cards) -> None:
        self.cards = [dict(c) for c in cards]
        self.write_map()

    def write_proc(self) -> None:
        load5 = self.load * CPUS
        (self.proc / "loadavg").write_text(f"{load5:.2f} {load5:.2f} {load5:.2f} 3/300 4242\n",
                                           encoding="utf-8")
        total_kb, swap_kb = 8_000_000, 4_000_000
        (self.proc / "meminfo").write_text(
            f"MemTotal:       {total_kb} kB\n"
            f"MemFree:        {int(total_kb * self.mem_free_pct / 200)} kB\n"
            f"MemAvailable:   {int(total_kb * self.mem_free_pct / 100)} kB\n"
            f"SwapTotal:      {swap_kb} kB\n"
            f"SwapFree:       {int(swap_kb * (100 - self.swap_pct) / 100)} kB\n",
            encoding="utf-8")
        (self.proc / "pressure" / "cpu").write_text(
            "some avg10=1.00 avg60=1.00 avg300=1.00 total=1\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n", encoding="utf-8")
        self.write_stat()

    def write_stat(self) -> None:
        def row(name: str, share: int) -> str:
            c = {k: v // share for k, v in self.cpu.items()}
            # user nice system idle iowait irq softirq steal guest guest_nice
            return f"{name} {c['user']} 0 0 {c['idle']} {c['iowait']} 0 0 {c['steal']} 0 0\n"
        text = row("cpu", 1) + "".join(row(f"cpu{i}", CPUS) for i in range(CPUS))
        (self.proc / "stat").write_text(text + "intr 0\nctxt 0\nbtime 0\n", encoding="utf-8")

    def tick(self) -> None:
        """Каждый проход видит новые счётчики /proc/stat: доли за интервал — их разница."""
        part = 10_000
        steal = int(part * self.steal_pct / 100)
        user = max(0, int(part * self.busy_pct / 100) - steal)
        iowait = int(part * self.iowait_pct / 100)
        self.cpu["steal"] += steal
        self.cpu["user"] += user
        self.cpu["iowait"] += iowait
        self.cpu["idle"] += part - steal - user - iowait
        self.write_stat()

    def set_load(self, per_cpu: float) -> None:
        self.load = per_cpu
        self.write_proc()

    def warm_up(self, at: float) -> None:
        """Проход за минуту до отсчёта: судье нужен прошлый снимок /proc/stat, иначе доли CPU
        неизвестны и признак мощности не ставится — первый «настоящий» замер был бы в at+2 мин."""
        self.measure(at - MIN)

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        env.update({
            "HARNESS_INSTALL_CONF": str(self.root / "install.conf"),
            "HARNESS_CONF": str(self.root / "harness.conf"),
            "HARNESS_LOG_DIR": str(self.log), "LOG_DIR": str(self.log),
            "STATE_DIR": str(self.state_root), "PANEL_STATE_DIR": str(self.panel),
            "HARNESS_PANEL_STATE": str(self.panel),
            "HEARTBEAT_DIR": str(self.state_root / "heartbeat"),
            "HARNESS_EVENTS_STATE": str(self.log / ".events"),
            "HARNESS_DEVMAP": str(self.root / "dev-map.yaml"), "HARNESS_ACTIVE_LINE": LINE,
            "CLAUDE_PROJECTS_DIR": str(self.projects), "PROJECT_DIR": str(self.project),
            "SPEED_PROC_DIR": str(self.proc), "SPEED_AFFINITY": str(self.affinity), "SPEED_DISK_PATH": str(self.root),
            "SPEED_MODE_DATA": str(self.root / "speed-modes.yaml"),
            "SPEED_MODE_BIN": str(JUDGE), "PYTHONDONTWRITEBYTECODE": "1",
        })
        return env

    # ── журналы стенда ───────────────────────────────────────────────────────
    def append(self, name: str, row: dict) -> None:
        with open(self.log / name, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def launch(self, call: str, at: float, now: float, part: str | None = None,
               parent: str | None = None, meta: bool = True, quiet_s: float = 60,
               session: str = "s1", outcome: str = "ok") -> None:
        """Строка agent.launch хука Ч1; meta и транскрипт — признак жизни помощника."""
        data = {"ревью": False, "описание": "проба", "тип": "harness-worker",
                "модель": "sonnet", "вызов": call}
        if parent:
            data["родитель"] = parent
        if part:
            data["часть"] = f"Часть: {part}#1"
        self.append("вызовы-агентов.jsonl", {
            "v": 1, "ts": iso(at), "src": "scripts/hooks/agent_budget_guard.py",
            "event": "agent.launch", "outcome": outcome, "task": "-", "task_from": "arg",
            "session": session, "data": data})
        if not meta:
            return
        folder = self.transcripts / session / "subagents"
        folder.mkdir(parents=True, exist_ok=True)
        agent = f"a{call}"
        meta_file = folder / f"agent-{agent}.meta.json"
        meta_file.write_text(json.dumps({"toolUseId": call, "spawnDepth": 1,
                                         "agentType": "harness-worker"}), encoding="utf-8")
        log = folder / f"agent-{agent}.jsonl"
        log.write_text("{}\n", encoding="utf-8")
        for path in (meta_file, log):
            os.utime(path, (now - quiet_s, now - quiet_s))

    def finish(self, call: str, at: float, session: str = "s1") -> None:
        self.append("вызовы-агентов.jsonl", {
            "v": 1, "ts": iso(at), "src": "scripts/hooks/agent_finish_log.py",
            "event": "agent.finish", "outcome": "ok", "task": None, "task_from": "none",
            "session": session, "data": {"вызов": call}})

    def spec_sent(self, card: str, at: float) -> None:
        self.append("спеки-владельцу.jsonl", {"ts": iso(at), "событие": "отправлена",
                                              "задача": card})

    def pause(self) -> None:
        (self.panel / "пауза.json").write_text(json.dumps(
            {"почему": "проба", "кто": "смена", "ts": int(time.time())}), encoding="utf-8")

    def limit_fact(self) -> None:
        (self.log / "лимит-подписки.состояние").write_text(f"{int(time.time())} limit 0",
                                                           encoding="utf-8")

    def rotation(self) -> None:
        (self.panel / "rotation-waits-helpers.json").write_text(
            json.dumps({"ts": int(time.time())}), encoding="utf-8")

    def limits(self, at: float, five=None, week=None, age_s: float = 0, raw: str | None = None):
        """Файл лимитов в формате контракта: окно — (процент, epoch сброса | None)."""
        if raw is not None:
            self.limits_file.write_text(raw, encoding="utf-8")
            return
        rate = {}
        for key, window in (("five_hour", five), ("seven_day", week)):
            if window is not None:
                rate[key] = {"used_percentage": window[0]}
                if window[1] is not None:
                    rate[key]["resets_at"] = window[1] if isinstance(window[1], str) else int(window[1])
        self.limits_file.write_text(json.dumps({"ts": at - age_s, "rate_limits": rate}),
                                    encoding="utf-8")

    def seed_points(self, points) -> None:
        """Точки скорости — строки mode.decision судьи с data.лимит (формат контракта)."""
        for t, tasks, p5, r5, pw, rw in points:
            self.append(JOURNAL, {
                "v": 1, "ts": iso(t), "src": JUDGE_SRC, "event": "mode.decision",
                "outcome": "ok", "task": None, "task_from": "none", "session": None,
                "data": {"лимит": {"ts": iso(t), "задач": tasks,
                                   "пять_часов": {"процент": p5, "сброс": iso(r5)},
                                   "неделя": {"процент": pw, "сброс": iso(rw)}}}})

    # ── судья ────────────────────────────────────────────────────────────────
    def run(self, *args, at: float | None = None) -> subprocess.CompletedProcess:
        if not JUDGE.is_file():
            raise ProbeError(f"нет кода судьи: {JUDGE}")
        cmd = [sys.executable, str(JUDGE), *args]
        if at is not None:
            cmd += ["--на", iso(at)]
        return subprocess.run(cmd, env=self.env(), capture_output=True, text=True,
                              timeout=TIMEOUT)

    def measure(self, at: float) -> dict:
        self.tick()
        done = self.run("--замер", at=at)
        if done.returncode != 0:
            raise ProbeError(f"--замер код {done.returncode}: {done.stderr.strip()[-400:]}")
        return self.state()

    def state(self) -> dict:
        path = self.panel / "скорость.json"
        if not path.is_file():
            raise ProbeError("состояние скорость.json не записано")
        return json.loads(path.read_text(encoding="utf-8"))

    def warden(self, last_kick: float, at: float) -> str:
        done = self.run("--сторожу", "--последний-пинок", str(int(last_kick)), at=at)
        if done.returncode != 0:
            raise ProbeError(f"--сторожу код {done.returncode}: {done.stderr.strip()[-300:]}")
        return done.stdout.strip()

    def show(self, at: float) -> tuple[dict, str]:
        done = self.run("--показать", at=at)
        if done.returncode != 0:
            raise ProbeError(f"--показать код {done.returncode}: {done.stderr.strip()[-300:]}")
        fields = {}
        for line in done.stdout.splitlines():
            key, sep, value = line.partition(": ")
            if sep:
                fields.setdefault(key.strip(), value.strip())
        return fields, done.stdout

    def call(self, fn: str, *args):
        if not JUDGE.is_file():
            raise ProbeError(f"нет кода судьи: {JUDGE}")
        done = subprocess.run([sys.executable, "-c", DRIVER, str(JUDGE), fn, json.dumps(args)],
                              env=self.env(), capture_output=True, text=True, timeout=TIMEOUT)
        if done.returncode != 0:
            raise ProbeError(f"{fn}() упал: {done.stderr.strip()[-400:]}")
        return json.loads(done.stdout.strip().splitlines()[-1])

    def events(self, event: str | None = None) -> list:
        path = self.log / JOURNAL
        if not path.is_file():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("src") == JUDGE_SRC and (event is None or row.get("event") == event):
                rows.append(row)
        return rows

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def cards(prefix: str, n: int, status: str = "plan") -> list:
    return [{"id": f"{prefix}{i}", "status": status} for i in range(1, n + 1)]


def power_ceiling(state: dict):
    return ((state.get("блокеры") or {}).get("мощность") or {}).get("потолок")


def blockers(state: dict) -> set:
    return set(state.get("блокеры") or {})


# ═══════════════════════════ данные ═══════════════════════════════════════════

@case
def data_live():
    """10.3.1: живые данные — режимы 1/3/5/10, у каждого блокера есть «говорить»."""
    if not check("файл данных есть", LIVE_DATA.is_file(), f"нет {LIVE_DATA}"):
        return
    data = yaml.safe_load(LIVE_DATA.read_text(encoding="utf-8")) or {}
    modes = data.get("режимы") or {}
    got = [((modes.get(k) or {}).get("задач")) for k in ("slow", "medium", "fast", "ultra")]
    check("задач по режимам [1, 3, 5, 10]", got == [1, 3, 5, 10], got)
    found = data.get("блокеры") or {}
    check("у каждого блокера есть «говорить»",
          sorted(k for k, v in found.items() if "говорить" not in (v or {})) == [],
          sorted(k for k, v in found.items() if "говорить" not in (v or {})))
    check("все блокеры контракта на месте", REQUIRED_BLOCKERS <= set(found),
          sorted(REQUIRED_BLOCKERS - set(found)))
    check("верхние ключи как в спеке 4.3", set(STAND_DATA) <= set(data),
          sorted(set(STAND_DATA) - set(data)))
    check("ключи лимит_подписки как в спеке 4.3",
          set(STAND_DATA["лимит_подписки"]) <= set(data.get("лимит_подписки") or {}),
          sorted(set(STAND_DATA["лимит_подписки"]) - set(data.get("лимит_подписки") or {})))
    check("по умолчанию — medium (решение владельца 11.3)", data.get("по_умолчанию") == "medium",
          data.get("по_умолчанию"))


@case
def data_without_say():
    """10.3.1: у блокера нет «говорить» — ошибка данных, код 1."""
    with Stand(data_patch={"блокеры.пауза.говорить": None}) as s:
        done = s.run("--замер", at=BASE)
        check("код 1", done.returncode == 1, f"код {done.returncode}: {done.stderr[-200:]}")


# ═══════════════════════════ лестница мощности ═══════════════════════════════

@case
def ladder_steps():
    """10.3.2: признак 9 мин — 0 шагов; 30 мин при ultra и 10 в работе — ровно 3 шага; подъём по 20 мин."""
    with Stand("ultra", cards("w", 10, "wip") + cards("p", 2)) as s:
        t0 = BASE
        s.busy_pct = 97
        s.warm_up(t0)
        st = s.measure(t0)
        check("до признака эффективный 10", st.get("эффективный") == 10, st.get("эффективный"))
        st = s.measure(t0 + 9 * MIN)
        check("9 мин — потолка мощности нет", power_ceiling(st) is None, power_ceiling(st))
        expected = [(10, 5, 5), (12, 5, 5), (20, 3, 3), (22, 3, 3), (30, 1, 1), (32, 1, 1)]
        for minute, ceiling, eff in expected:
            st = s.measure(t0 + minute * MIN)
            check(f"{minute} мин — П {ceiling}", power_ceiling(st) == ceiling, power_ceiling(st))
            check(f"{minute} мин — эффективный {eff}", st.get("эффективный") == eff,
                  st.get("эффективный"))
        changes = [((r.get("data") or {}).get("было"), (r.get("data") or {}).get("стало"))
                   for r in s.events("mode.change")]
        check("за 30 мин ровно 3 mode.change: 10→5→3→1",
              changes == [(10, 5), (5, 3), (3, 1)], changes)
        check("blocker.on мощности записан",
              any((r.get("data") or {}).get("блокер") == "мощность" for r in s.events("blocker.on")),
              "нет blocker.on")

        t1 = t0 + 34 * MIN
        s.busy_pct = 30
        s.measure(t1)
        for minute, ceiling in ((19, 1), (20, 3), (22, 3), (39, 3), (40, 5)):
            st = s.measure(t1 + minute * MIN)
            check(f"признака нет {minute} мин — П {ceiling}", power_ceiling(st) == ceiling,
                  power_ceiling(st))
        st = s.measure(t1 + 60 * MIN)
        check("60 мин — блокер мощности снят", "мощность" not in blockers(st), blockers(st))
        check("60 мин — эффективный 10", st.get("эффективный") == 10, st.get("эффективный"))
        check("blocker.off мощности записан",
              any((r.get("data") or {}).get("блокер") == "мощность" for r in s.events("blocker.off")),
              "нет blocker.off")
        check("поднят_ts — момент последнего шага вверх",
              epoch(st.get("поднят_ts")) == t1 + 60 * MIN, st.get("поднят_ts"))


@case
def ladder_from_in_work():
    """4.2: шаг вниз — от len(в_работе), а не от эффективного; П выше выбранного допустим."""
    with Stand("medium", cards("w", 10, "wip")) as s:
        s.busy_pct = 97
        s.warm_up(BASE)
        s.measure(BASE)
        st = s.measure(BASE + 10 * MIN)
        check("medium и 10 в работе: первый шаг П = 5 (ниже 10, а не ниже 3)",
              power_ceiling(st) == 5, power_ceiling(st))
        check("эффективный остаётся 3", st.get("эффективный") == 3, st.get("эффективный"))
        st = s.measure(BASE + 20 * MIN)
        check("второй шаг П = 3", power_ceiling(st) == 3, power_ceiling(st))
        st = s.measure(BASE + 30 * MIN)
        check("третий шаг П = 1, эффективный 1",
              power_ceiling(st) == 1 and st.get("эффективный") == 1,
              (power_ceiling(st), st.get("эффективный")))


@case
def steal_sign():
    """5: кража > 15 % за интервал замера — признак мощности при низкой нагрузке."""
    with Stand("ultra", cards("w", 10, "wip")) as s:
        s.steal_pct = 20
        s.measure(BASE)
        st = s.measure(BASE + 2 * MIN)
        steal = (st.get("замер") or {}).get("кража_pct")
        check("кража посчитана по разнице /proc/stat (~20 %)",
              isinstance(steal, (int, float)) and abs(steal - 20) < 1, steal)
        st = s.measure(BASE + 12 * MIN)
        check("кража 10 мин — шаг вниз П = 5", power_ceiling(st) == 5, power_ceiling(st))


# ── мощность по доле CPU, а не по loadavg (hr.speed-uses-whole-server) ─────────
# Слово владельца 12.11: «использовать сервер целиком». Замер: loadavg 17,6 на 8 ядрах при CPU
# занят 58 % — очередь коротких процессов, не загрузка. Мощность судится долей занятого CPU
# (всё, кроме idle и iowait) за интервал между двумя чтениями /proc/stat, плюс iowait и steal
# за тот же интервал. Замер пишет «занято_pct» и «ожидание_диска_pct» рядом с «кража_pct».

def cpu_stand(busy: float, load: float = 0.5, iowait: float = 0.0, steal: float = 0.0,
              mode: str = "ultra", wip: int = 10, plans: int = 2) -> Stand:
    s = Stand(mode, cards("w", wip, "wip") + cards("p", plans))
    s.busy_pct, s.iowait_pct, s.steal_pct = busy, iowait, steal
    s.set_load(load)
    return s


def gauge(st: dict, key: str):
    return (st.get("замер") or {}).get(key)


def near(got, want: float, slack: float = 1.0) -> bool:
    return isinstance(got, (int, float)) and not isinstance(got, bool) and abs(got - want) < slack


def power_candidate(st: dict) -> bool:
    return "мощность" in (st.get("кандидаты") or {})


def forget_interval(s: Stand) -> None:
    """Состояние без прошлого снимка /proc/stat — как первый проход судьи."""
    path = s.panel / "скорость.json"
    if path.is_file():
        st = json.loads(path.read_text(encoding="utf-8"))
        st.pop("cpu_прошлый", None)
        path.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


@case
def power_busy_not_load():
    """12.11: loadavg 2,2/ядро (17,6 на 8), CPU занят 58 % — мощность не режет, ультра держит 10."""
    with cpu_stand(busy=58, load=2.2) as s:
        s.measure(BASE)
        st = s.measure(BASE + 2 * MIN)
        check("занято_pct за интервал ≈ 58", near(gauge(st, "занято_pct"), 58), gauge(st, "занято_pct"))
        check("ожидание_диска_pct ≈ 0", near(gauge(st, "ожидание_диска_pct"), 0),
              gauge(st, "ожидание_диска_pct"))
        check("58 % — кандидата мощности нет", not power_candidate(st), st.get("кандидаты"))
        held = [power_ceiling(s.measure(BASE + minute * MIN)) for minute in (12, 14, 24)]
        check("24 мин при 2,2/ядро и 58 % — потолка мощности нет", held == [None] * 3, held)
        st = s.state()
        check("эффективный 10 (ультра)", st.get("эффективный") == 10, st.get("эффективный"))


@case
def power_busy_cuts():
    """Занято 97 % при loadavg 0,5/ядро — признак мощности, через 10 мин П 5; 85 % — не режет."""
    with cpu_stand(busy=97, load=0.5) as s:
        s.measure(BASE)
        st = s.measure(BASE + 2 * MIN)
        check("занято_pct за интервал ≈ 97", near(gauge(st, "занято_pct"), 97), gauge(st, "занято_pct"))
        check(f"97 % (порог {CPU_BUSY_MAX}) — кандидат мощности", power_candidate(st),
              st.get("кандидаты"))
        st = s.measure(BASE + 12 * MIN)
        check("признак 10 мин — П 5", power_ceiling(st) == 5, power_ceiling(st))
        check("признак 10 мин — эффективный 5", st.get("эффективный") == 5, st.get("эффективный"))
    with cpu_stand(busy=85, load=0.5) as s:
        held = [power_ceiling(s.measure(BASE + minute * MIN)) for minute in (0, 2, 12, 14)]
        check(f"85 % (ниже порога {CPU_BUSY_MAX}) — не режет", held == [None] * 4, held)


@case
def power_iowait_cuts():
    """iowait 30 % за интервал (порог 20) — признак мощности при занятости 40 %; 15 % — не режет."""
    with cpu_stand(busy=40, iowait=30, load=0.5) as s:
        s.measure(BASE)
        st = s.measure(BASE + 2 * MIN)
        check("ожидание_диска_pct ≈ 30", near(gauge(st, "ожидание_диска_pct"), 30),
              gauge(st, "ожидание_диска_pct"))
        check("занято_pct ≈ 40 (iowait не занятость)", near(gauge(st, "занято_pct"), 40),
              gauge(st, "занято_pct"))
        check(f"iowait 30 % (порог {CPU_IOWAIT_MAX}) — кандидат мощности", power_candidate(st),
              st.get("кандидаты"))
        st = s.measure(BASE + 12 * MIN)
        check("iowait 10 мин — П 5", power_ceiling(st) == 5, power_ceiling(st))
    with cpu_stand(busy=40, iowait=15, load=0.5) as s:
        held = [power_ceiling(s.measure(BASE + minute * MIN)) for minute in (0, 2, 12, 14)]
        check(f"iowait 15 % (ниже {CPU_IOWAIT_MAX}) — не режет", held == [None] * 4, held)


@case
def power_steal_cuts():
    """steal 20 % за интервал (порог 15 в данных) при занятости 40 % и низком loadavg — П 5."""
    with cpu_stand(busy=40, steal=20, load=0.5) as s:
        s.measure(BASE)
        st = s.measure(BASE + 2 * MIN)
        check("кража_pct ≈ 20", near(gauge(st, "кража_pct"), 20), gauge(st, "кража_pct"))
        st = s.measure(BASE + 12 * MIN)
        check("кража 10 мин — П 5", power_ceiling(st) == 5, power_ceiling(st))


@case
def power_busy_releases():
    """После среза по 97 % занятость 58 % при loadavg 2,2/ядро — «тихо», через 20 мин потолок снят."""
    with cpu_stand(busy=97, load=0.5) as s:
        s.measure(BASE)
        s.measure(BASE + 2 * MIN)
        st = s.measure(BASE + 12 * MIN)
        check("срез по занятости — П 5", power_ceiling(st) == 5, power_ceiling(st))
        t1 = BASE + 14 * MIN
        s.busy_pct = 58
        s.set_load(2.2)
        s.measure(t1)
        st = s.measure(t1 + 19 * MIN)
        check("58 % 19 мин — П ещё 5, не ниже", power_ceiling(st) == 5, power_ceiling(st))
        st = s.measure(t1 + 20 * MIN)
        check(f"58 % (ниже {CPU_BUSY_QUIET}) 20 мин — блокер снят, эффективный 10",
              "мощность" not in blockers(st) and st.get("эффективный") == 10,
              (power_ceiling(st), st.get("эффективный")))


@case
def power_no_interval():
    """Без прошлого снимка /proc/stat занятость неизвестна: не режет и не отпускает — потолок как был."""
    # 1) Высокий loadavg и накопленные с загрузки 97 % — без интервала это не замер.
    with cpu_stand(busy=97, load=2.5) as s:
        seen = []
        for minute in (0, 2, 12, 14):
            forget_interval(s)
            st = s.measure(BASE + minute * MIN)
            seen.append((power_candidate(st), power_ceiling(st)))
        check("без интервала — ни кандидата, ни потолка мощности", seen == [(False, None)] * 4, seen)
        check("без интервала занято_pct — None (неизвестно), не число с загрузки",
              gauge(st, "занято_pct") is None, gauge(st, "занято_pct"))
        check("эффективный 10", st.get("эффективный") == 10, st.get("эффективный"))
    # 2) Потолок 5 уже стоит; дальше проходы без интервала при низком loadavg — П держится 5.
    with cpu_stand(busy=97, load=0.5) as s:
        s.measure(BASE)
        s.measure(BASE + 2 * MIN)
        st = s.measure(BASE + 12 * MIN)
        check("срез по занятости — П 5", power_ceiling(st) == 5, power_ceiling(st))
        s.busy_pct = 10
        held = []
        for minute in (14, 24, 34, 44, 54):
            forget_interval(s)
            held.append(power_ceiling(s.measure(BASE + minute * MIN)))
        check("40 мин без интервала — П 5: не отпущен и не срезан ниже", held == [5] * 5, held)


@case
def power_busy_fill_quota():
    """Добор сторожа — по занятости: 58 % при 2,2/ядро — добрать весь недобор; 97 % при 0,5 — ноль."""
    with cpu_stand(busy=58, load=2.2, wip=2, plans=5) as s:
        s.measure(BASE)
        st = s.measure(BASE + 2 * MIN)
        check("58 %: добор 5 (весь недобор до 7)", st.get("добор") == 5,
              (st.get("добор"), st.get("добор_почему")))
    with cpu_stand(busy=97, load=0.5, wip=2, plans=5) as s:
        s.measure(BASE)
        st = s.measure(BASE + 2 * MIN)
        check("97 %: добор 0", st.get("добор") == 0, (st.get("добор"), st.get("добор_почему")))


@case
def power_affinity_seen():
    """Привязка процессов к части ядер (cpuset) видна в замере и в --показать; блокером не служит."""
    with cpu_stand(busy=40, load=0.5) as s:
        s.affinity = 2
        s.measure(BASE)
        st = s.measure(BASE + 2 * MIN)
        check("ядер_доступно = 2", gauge(st, "ядер_доступно") == 2, gauge(st, "ядер_доступно"))
        check("ядер в замере — все", gauge(st, "ядер") == CPUS, gauge(st, "ядер"))
        _, text = s.show(BASE + 2 * MIN)
        check(f"--показать: «привязка к 2 из {CPUS} ядер»", f"привязка к 2 из {CPUS} ядер" in text,
              text[-300:])
        check("привязка не блокер: кандидата нет, эффективный 10",
              not power_candidate(st) and st.get("эффективный") == 10,
              (st.get("кандидаты"), st.get("эффективный")))
    with cpu_stand(busy=40, load=0.5) as s:
        s.measure(BASE)
        st = s.measure(BASE + 2 * MIN)
        check("все ядра доступны — поля нет", "ядер_доступно" not in (st.get("замер") or {}),
              st.get("замер"))
        _, text = s.show(BASE + 2 * MIN)
        check("все ядра доступны — в --показать про привязку молчит", "привязка" not in text,
              text[-300:])


@case
def accident_load():
    """4.2, 10.3.2: авария — сразу 1 и П = 1; после снятия — по лестнице от 1."""
    with Stand("ultra", cards("w", 10, "wip") + cards("p", 2)) as s:
        t0 = BASE
        s.set_load(4.5)
        s.measure(t0)
        st = s.measure(t0 + 2 * MIN)
        check("2 мин аварии — ещё не сработала", st.get("эффективный") == 10, st.get("эффективный"))
        st = s.measure(t0 + 4 * MIN)
        check("4 мин — эффективный 1", st.get("эффективный") == 1, st.get("эффективный"))
        check("4 мин — П = 1", power_ceiling(st) == 1, power_ceiling(st))
        check("4 мин — блокер авария", "авария" in blockers(st), blockers(st))
        t1 = t0 + 6 * MIN
        s.set_load(0.5)
        s.measure(t1)
        st = s.measure(t1 + 19 * MIN)
        check("19 мин без признака — авария держится", "авария" in blockers(st), blockers(st))
        st = s.measure(t1 + 20 * MIN)
        check("20 мин — авария снята", "авария" not in blockers(st), blockers(st))
        check("снятие аварии — эффективный 1, не прыжок к 10", st.get("эффективный") == 1,
              st.get("эффективный"))
        check("снятие аварии пишет поднят_ts", epoch(st.get("поднят_ts")) == t1 + 20 * MIN,
              st.get("поднят_ts"))
        st = s.measure(t1 + 40 * MIN)
        check("ещё 20 мин — ступень 3", st.get("эффективный") == 3, st.get("эффективный"))


@case
def accident_memory():
    """5: свободной памяти меньше MEM_MIN_FREE_PCT 4 мин — авария, эффективный 1."""
    with Stand("medium", cards("p", 3)) as s:
        s.mem_free_pct = 5
        s.write_proc()
        s.measure(BASE)
        st = s.measure(BASE + 4 * MIN)
        check("память 5 % — авария", "авария" in blockers(st), blockers(st))
        check("эффективный 1", st.get("эффективный") == 1, st.get("эффективный"))


# ═══════════════════════════ блокеры по фактам ═══════════════════════════════

@case
def pause_blocker():
    """10.3.2: пауза — 0, сторож молчит."""
    with Stand("medium", cards("p", 5)) as s:
        s.pause()
        st = s.measure(BASE)
        check("пауза — эффективный 0", st.get("эффективный") == 0, st.get("эффективный"))
        check("пауза в блокерах", "пауза" in blockers(st), blockers(st))
        out = s.warden(0, BASE)
        check("--сторожу молчит", out.startswith("молчу\t"), out)


@case
def limit_fact_blocker():
    """10.3.2: факт 429 (метка limit_mark с поводом limit) — 0."""
    with Stand("medium", cards("p", 5)) as s:
        s.limit_fact()
        st = s.measure(BASE)
        check("лимит по факту — эффективный 0", st.get("эффективный") == 0, st.get("эффективный"))
        check("лимит в блокерах", "лимит" in blockers(st), blockers(st))


@case
def rotation_freeze():
    """4.1.5: ротация — заморозка: эффективный = len(в_работе)."""
    with Stand("ultra", cards("w", 2, "wip") + cards("p", 5)) as s:
        s.rotation()
        st = s.measure(BASE)
        check("ротация — эффективный 2", st.get("эффективный") == 2, st.get("эффективный"))
        out = s.warden(0, BASE)
        check("--сторожу молчит", out.startswith("молчу\t"), out)


@case
def files_blocker():
    """5: три file.claim.warn за 30 мин — ступень ниже len(в_работе)."""
    with Stand("ultra", cards("w", 10, "wip")) as s:
        for i in range(3):
            s.append("заявки-на-файлы.jsonl", {
                "v": 1, "ts": iso(BASE - (5 + i) * MIN), "src": "scripts/hooks/file_claim_guard.py",
                "event": "file.claim.warn", "outcome": "ok", "task": "w1", "task_from": "arg",
                "session": "s1", "data": {"путь": f"/x/{i}.py", "агент": f"a{i}",
                                          "общий": False, "занявшие": ["b"], "номер": 1}})
        st = s.measure(BASE)
        check("файлы в блокерах", "файлы" in blockers(st), blockers(st))
        check("эффективный 5", st.get("эффективный") == 5, st.get("эффективный"))


def claim_rows(s: Stand, event: str, minutes) -> None:
    for i, minute in enumerate(minutes):
        s.append("заявки-на-файлы.jsonl", {
            "v": 1, "ts": iso(BASE - minute * MIN), "src": "scripts/hooks/file_claim_guard.py",
            "event": event, "outcome": "ok", "task": "w1", "task_from": "arg", "session": "s1",
            "data": {"путь": f"/x/{event}-{i}.py", "агент": f"a{i}", "общий": False,
                     "занявшие": ["b"], "номер": 1}})


@case
def files_counts_only_warn():
    """Хук пишет file.claim.warn только на настоящее пересечение; повтор (repeat) и напоминание
    об общем файле (remind) — не предупреждения и в счёт блокера «файлы» не идут."""
    with Stand("ultra", cards("w", 10, "wip")) as s:
        claim_rows(s, "file.claim.warn", (5, 12, 29))
        claim_rows(s, "file.claim.warn", (31,))
        claim_rows(s, "file.claim.repeat", (3, 6, 9, 14, 20))
        claim_rows(s, "file.claim.remind", (4, 8))
        got, err = attempt(s, "file_warnings", BASE)
        check("за 30 мин 3 warn, 5 repeat, 2 remind (+1 warn 31 мин назад) — file_warnings 3",
              got == 3, err or got)
    with Stand("ultra", cards("w", 10, "wip")) as s:
        claim_rows(s, "file.claim.warn", (5, 12))
        claim_rows(s, "file.claim.repeat", (3, 6, 9, 14, 20))
        claim_rows(s, "file.claim.remind", (4, 8))
        st = s.measure(BASE)
        check("2 warn + 5 repeat + 2 remind — блокера «файлы» нет, эффективный 10",
              "файлы" not in blockers(st) and st.get("эффективный") == 10,
              (blockers(st), st.get("эффективный")))


@case
def unknown_mode():
    """4.1.7: незнакомый SPEED_MODE — умолчание и mode.decision outcome=error."""
    with Stand("turbo", cards("p", 5)) as s:
        st = s.measure(BASE)
        check("выбранный — medium", st.get("выбранный") == "medium", st.get("выбранный"))
        check("эффективный 3", st.get("эффективный") == 3, st.get("эффективный"))
        check("mode.decision outcome=error",
              any(r.get("outcome") == "error" for r in s.events("mode.decision")),
              [r.get("outcome") for r in s.events("mode.decision")])


@case
def measure_failure():
    """4.1.6: /proc/loadavg не читается — код 2, строка error, состояние не переписано."""
    with Stand("medium", cards("p", 5)) as s:
        s.measure(BASE)
        before = (s.panel / "скорость.json").read_bytes()
        (s.proc / "loadavg").unlink()
        done = s.run("--замер", at=BASE + 2 * MIN)
        check("код 2", done.returncode == 2, f"код {done.returncode}: {done.stderr[-200:]}")
        check("mode.decision outcome=error",
              any(r.get("outcome") == "error" for r in s.events("mode.decision")), "нет error")
        check("состояние не переписано", (s.panel / "скорость.json").read_bytes() == before,
              "файл изменён")


@case
def no_state_answers():
    """4.1.6: состояния нет — --сторожу и --показать код 2, функции — None."""
    with Stand("medium", cards("p", 5)) as s:
        check("--сторожу код 2", s.run("--сторожу", "--последний-пинок", "0",
                                        at=BASE).returncode == 2, "не 2")
        done = s.run("--показать", at=BASE)
        check("--показать код 2 и «не отвечает»",
              done.returncode == 2 and "не отвечает" in done.stdout + done.stderr,
              f"код {done.returncode}: {done.stdout[-200:]}")
        check("ceiling_alive() — None", s.call("ceiling_alive", BASE) is None, "не None")
        (s.panel / "скорость.json").write_text("{битый", encoding="utf-8")
        check("битое состояние — ceiling_alive() None", s.call("ceiling_alive", BASE) is None,
              "не None")
        check("битое состояние — effective() None", s.call("effective", BASE) is None, "не None")
        check("--выбран с чужим «кто» — код 1",
              s.run("--выбран", "сосед", at=BASE).returncode == 1, "не 1")


# ═══════════════════════════ счёт задач ═══════════════════════════════════════

@case
def count_tasks():
    """10.3.3: метка, родитель, закрытый, фантом, завис, безымянный, id не из карты, страховка 240 мин."""
    ids = [{"id": i, "status": "plan"} for i in ("hr.x", "hr.y", "hr.z", "hr.q", "hr.h", "hr.o")]
    with Stand("ultra", ids) as s:
        now = BASE
        s.launch("O1", now - 10 * MIN, now, part="hr.x")
        s.launch("C1", now - 9 * MIN, now, parent="O1")
        s.launch("C2", now - 9 * MIN, now, parent="O1")
        s.launch("L1", now - 30 * MIN, now, part="hr.y")
        s.finish("L1", now - 5 * MIN)
        s.launch("P1", now - 300, now, part="hr.z", meta=False)
        s.launch("Y1", now - 30, now, part="hr.q", meta=False)
        s.launch("H1", now - 200 * MIN, now, part="hr.h", quiet_s=121 * MIN)
        s.launch("U1", now - 5 * MIN, now)
        s.launch("N1", now - 5 * MIN, now, part="нет-такой")
        s.launch("V1", now - 300 * MIN, now, part="hr.o")
        s.launch("R1", now - 5 * MIN, now, part="hr.o", outcome="refused")
        expected = sorted(["hr.x", "hr.q", "без-метки:U1", "без-карточки:нет-такой"])
        got = s.call("in_work", now)
        check("in_work() — ровно ожидаемые задачи", got == expected, f"{got} ≠ {expected}")
        check("оркестратор + 2 исполнителя без метки = 1 задача hr.x",
              isinstance(got, list) and got.count("hr.x") == 1
              and not any(str(t).endswith(("C1", "C2")) for t in got), got)
        st = s.measure(now)
        check("состояние: в_работе как in_work()", sorted(st.get("в_работе") or []) == expected,
              st.get("в_работе"))
        check("фантомов: 1", st.get("фантомов") == 1, st.get("фантомов"))
        check("без_метки: 1", st.get("без_метки") == 1, st.get("без_метки"))


@case
def count_unlabeled_chain():
    """3.2 п. 2 (РЕШЕНО): безымянный оркестратор с исполнителем — одна задача без-метки:<оркестратор>."""
    with Stand("ultra", cards("p", 2)) as s:
        s.launch("O9", BASE - 5 * MIN, BASE)
        s.launch("C9", BASE - 4 * MIN, BASE, parent="O9")
        got = s.call("in_work", BASE)
        check("одна задача без-метки:O9", got == ["без-метки:O9"], got)


@case
def count_empty_journal():
    """10.3.3: журнала запусков нет — в работе только wip карты."""
    with Stand("ultra", [{"id": "hr.w", "status": "wip"}] + cards("p", 2)) as s:
        got = s.call("in_work", BASE)
        check("в работе только wip", got == ["hr.w"], got)


@case
def count_waiting_with_launch():
    """3.1: ждущая владельца с живым запуском — в работе; wip, ждущая без запуска, — нет."""
    with Stand("ultra", [{"id": "hr.s", "status": "plan"}, {"id": "hr.t", "status": "wip"}]) as s:
        s.spec_sent("hr.s", BASE - HOUR)
        s.spec_sent("hr.t", BASE - HOUR)
        s.launch("S1", BASE - 5 * MIN, BASE, part="hr.s")
        got = s.call("in_work", BASE)
        check("в работе только hr.s", got == ["hr.s"], got)


# ═══════════════════════════ ждёт владельца и мало задач ══════════════════════

def waiting_map():
    return [{"id": "W", "status": "wip"}, {"id": "A", "status": "plan"},
            {"id": "B", "status": "plan"}, {"id": "C", "status": "plan"},
            {"id": "D", "status": "plan", "зависит_от": ["W"]}]


@case
def waiting_owner():
    """10.3.4: ждущие и зависимая не готовы; эффективный 2; журнал одобрений не тронут."""
    with Stand("ultra", waiting_map()) as s:
        s.spec_sent("W", BASE - HOUR)
        s.spec_sent("A", BASE - HOUR)
        approvals = s.log / "спеки-владельцу.jsonl"
        before = (approvals.stat().st_size, approvals.stat().st_mtime_ns)
        st = s.measure(BASE)
        check("готовых: 2", st.get("готовых") == 2, st.get("готовых"))
        check("к_взятию — B и C", sorted(st.get("к_взятию") or []) == ["B", "C"],
              st.get("к_взятию"))
        check("W не в работе", "W" not in (st.get("в_работе") or []), st.get("в_работе"))
        check("ждут_владельца: 2", st.get("ждут_владельца") == 2, st.get("ждут_владельца"))
        check("эффективный 2 при ultra", st.get("эффективный") == 2, st.get("эффективный"))
        out = s.warden(0, BASE)
        parts = out.split("\t")
        ids = parts[2].split() if len(parts) > 2 else []
        check("--сторожу: недобор с id", parts[0] == "недобор" and ids, out)
        check("id пинка — только B и C", ids and set(ids) <= {"B", "C"}, ids)
        fields, _ = s.show(BASE)
        check("--показать: ждут владельца 2", number(fields.get("ждут владельца", "")) == 2,
              fields.get("ждут владельца"))
        check("--показать: зависимостей в карте 1",
              number(fields.get("зависимостей в карте", "")) == 1,
              fields.get("зависимостей в карте"))
        check("--показать: выбранный ultra", "ultra" in fields.get("выбранный", ""),
              fields.get("выбранный"))
        check("--показать: фантомов 0", number(fields.get("фантомов", "")) == 0,
              fields.get("фантомов"))
        for i in range(1, 10):
            s.measure(BASE + 2 * i * MIN)
        after = (approvals.stat().st_size, approvals.stat().st_mtime_ns)
        check("10 проходов — журнал одобрений не тронут", before == after, f"{before} → {after}")


@case
def waiting_limit_reached():
    """10.3.4: ждущих ≥ максимума — plan без одобрения не готовы; причина «спеки ждут»."""
    with Stand("ultra", waiting_map(), data_patch={"спек_ждут_владельца_максимум": 2}) as s:
        s.spec_sent("W", BASE - HOUR)
        s.spec_sent("A", BASE - HOUR)
        st = s.measure(BASE)
        check("готовых: 0", st.get("готовых") == 0, st.get("готовых"))
        out = s.warden(0, BASE)
        check("--сторожу молчит", out.startswith("молчу"), out)
        _, text = s.show(BASE)
        check("--показать: «2 спеки ждут твоего «да»»", "2 спеки ждут" in text, text[-300:])


@case
def broken_dependency():
    """10.3.4: зависимость на id не из карты — карточка готова, строка «битая зависимость»."""
    with Stand("ultra", [{"id": "E", "status": "plan", "зависит_от": ["нет-такого"]}]) as s:
        st = s.measure(BASE)
        check("E готова", "E" in (st.get("к_взятию") or []), st.get("к_взятию"))
        _, text = s.show(BASE)
        check("--показать: битая зависимость E → нет-такого",
              "битая зависимость: E → нет-такого" in text, text[-300:])


# ═══════════════════════════ выкат без захвата ════════════════════════════════

IN_OPEN = 0x20


def inotify_open_counter(path: Path):
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    fd = libc.inotify_init1(os.O_NONBLOCK)
    if fd < 0 or libc.inotify_add_watch(fd, str(path).encode(), IN_OPEN) < 0:
        raise ProbeError(f"inotify не поднялся: errno {ctypes.get_errno()}")

    def count() -> int:
        found = 0
        while True:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                return found
            offset = 0
            while offset < len(chunk):
                _, mask, _, size = struct.unpack_from("iIII", chunk, offset)
                found += bool(mask & IN_OPEN)
                offset += 16 + size
    return fd, count


@case
def deploy_lock():
    """10.3.5: выкат по /proc/locks — заморозка; проходы судьи лок не открывают."""
    with Stand("ultra", cards("w", 2, "wip") + cards("p", 5)) as s:
        (s.proc / "locks").unlink()
        (s.proc / "locks").symlink_to("/proc/locks")
        lock = s.log / "locks" / "deploy.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("", encoding="utf-8")
        holder = subprocess.Popen(
            [sys.executable, "-c", "import fcntl,sys,time; f=open(sys.argv[1],'a');"
             "fcntl.flock(f,fcntl.LOCK_EX); print('ok',flush=True); time.sleep(60)", str(lock)],
            stdout=subprocess.PIPE, text=True)
        try:
            holder.stdout.readline()
            st = s.measure(BASE)
            check("лок держат — выкат в блокерах", "выкат" in blockers(st), blockers(st))
            check("заморозка: эффективный 2", st.get("эффективный") == 2, st.get("эффективный"))
        finally:
            holder.kill()
            holder.wait()
        st = s.measure(BASE + 2 * MIN)
        check("лок отпущен — выката нет", "выкат" not in blockers(st), blockers(st))
        fd, count = inotify_open_counter(lock)
        try:
            open(lock, encoding="utf-8").close()
            check("наблюдатель жив: своё open() — 1 событие", count() == 1, "не 1")
            for i in range(10):
                s.measure(BASE + (4 + 2 * i) * MIN)
            opened = count()
            check("10 проходов — 0 открытий лока", opened == 0, f"открытий {opened}")
        finally:
            os.close(fd)


# ═══════════════════════════ ответ сторожу ════════════════════════════════════

def kick_stand(mode="medium", wip=1, plans=5) -> Stand:
    return Stand(mode, cards("w", wip, "wip") + cards("p", plans))


def kick_ids(out: str) -> tuple[str, int | None, list]:
    parts = out.split("\t")
    size = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    return parts[0], size, (parts[2].split() if len(parts) > 2 else [])


@case
def kick_by_load():
    """10.3.8а: занято 58 % — весь недобор; 80 % (между тишиной и максимумом) — по одной;
    97 % — молчу (кандидат мощности)."""
    plans = {f"p{i}" for i in range(1, 6)}
    with kick_stand() as s:
        s.busy_pct = 58
        s.warm_up(BASE)
        s.measure(BASE)
        kind, size, ids = kick_ids(s.warden(0, BASE))
        check("58 % — недобор 2", (kind, size) == ("недобор", 2), (kind, size))
        check("58 % — 2 id из готовых", len(ids) == 2 and set(ids) <= plans, ids)
        s.busy_pct = 80
        st = s.measure(BASE + 2 * MIN)
        kind, size, ids = kick_ids(s.warden(0, BASE + 2 * MIN))
        check("80 % — недобор 1", (kind, size) == ("недобор", 1), (kind, size))
        check("80 % — 1 id", len(ids) == 1 and set(ids) <= plans, ids)
        check("80 % — добор в состоянии 1", st.get("добор") == 1, st.get("добор"))
        _, text = s.show(BASE + 2 * MIN)
        check("--показать: «добираю по одной»", "по одной" in text, text[-300:])
        s.busy_pct = 97
        s.measure(BASE + 4 * MIN)
        out = s.warden(0, BASE + 4 * MIN)
        check("97 % — молчу: кандидат мощности",
              out.startswith("молчу\t") and "кандидат мощности" in out, out)
        _, text = s.show(BASE + 4 * MIN)
        check("--показать говорит то же", "кандидат мощности" in text, text[-300:])


@case
def kick_load_edges():
    """10.3.8а: края порогов. «Тихо» — строго ниже CPU_BUSY_QUIET_PCT (отпускает только ниже):
    74,5 % — весь недобор, ровно 75 — уже по одной. Кандидат — строго выше CPU_BUSY_MAX_PCT:
    ровно 90 — ещё по одной, 90,5 — молчу."""
    edges = ((CPU_BUSY_QUIET - 0.5, "недобор", 2, "ниже порога тишины — весь недобор 2"),
             (CPU_BUSY_QUIET, "недобор", 1, "ровно порог тишины — по одной"),
             (CPU_BUSY_MAX, "недобор", 1, "ровно максимум — по одной, не кандидат"),
             (CPU_BUSY_MAX + 0.5, "молчу", None, "выше максимума — молчу (кандидат)"))
    with kick_stand() as s:
        s.warm_up(BASE)
        for i, (busy, want_kind, want_size, label) in enumerate(edges):
            at = BASE + 2 * i * MIN
            s.busy_pct = busy
            st = s.measure(at)
            kind, size, _ = kick_ids(s.warden(0, at))
            check(f"{busy} % ({label})", kind == want_kind and size == want_size,
                  (kind, size, gauge(st, "занято_pct")))


@case
def kick_interval_and_choice():
    """10.3.8а: пинок 5 мин назад — молчу; выбор владельца после пинка — недобор; поднят_ts не тронут."""
    with kick_stand() as s:
        s.set_load(1.0)
        s.measure(BASE)
        out = s.warden(BASE - 5 * MIN, BASE)
        check("пинок 5 мин назад — молчу", out.startswith("молчу"), out)
        out = s.warden(BASE - 25 * MIN, BASE)
        check("пинок 25 мин назад — недобор", out.startswith("недобор\t"), out)
        s.set_mode("fast")
        done = s.run("--выбран", "бот", at=BASE + 2 * MIN)
        check("--выбран бот — код 0", done.returncode == 0, done.stderr[-200:])
        st = s.state()
        check("выбор_ts = момент --выбран", epoch(st.get("выбор_ts")) == BASE + 2 * MIN,
              st.get("выбор_ts"))
        check("выбор владельца не трогает поднят_ts", st.get("поднят_ts") is None,
              st.get("поднят_ts"))
        rows = [r.get("data") or {} for r in s.events("mode.selected")]
        check("mode.selected: medium → fast, кто бот",
              any(d.get("было") == "medium" and d.get("стало") == "fast" and d.get("кто") == "бот"
                  for d in rows), rows)
        out = s.warden(BASE - 3 * MIN, BASE + 2 * MIN)
        check("пинок 5 мин назад, но раньше выбора — недобор", out.startswith("недобор\t"), out)
        s.set_mode("ultra")
        s.measure(BASE + 4 * MIN)
        rows = [r.get("data") or {} for r in s.events("mode.selected")]
        check("смена ключа без --выбран — mode.selected кто неизвестно",
              any(d.get("стало") == "ultra" and d.get("кто") == "неизвестно" for d in rows), rows)


@case
def kick_after_power_rise():
    """10.3.8а: шаг вверх мощности 15 мин назад — молчу; 21 мин — недобор."""
    with kick_stand() as s:
        t0 = BASE
        s.busy_pct = 97
        s.warm_up(t0)
        s.measure(t0)
        st = s.measure(t0 + 10 * MIN)
        check("признак 10 мин при 1 в работе — П 1", power_ceiling(st) == 1, power_ceiling(st))
        t1 = t0 + 12 * MIN
        s.busy_pct = 30
        s.measure(t1)
        st = s.measure(t1 + 20 * MIN)
        check("подъём 1→3 = выбранный — блокер снят", "мощность" not in blockers(st),
              blockers(st))
        check("поднят_ts записан", epoch(st.get("поднят_ts")) == t1 + 20 * MIN, st.get("поднят_ts"))
        s.measure(t1 + 35 * MIN)
        out = s.warden(0, t1 + 35 * MIN)
        check("подъём 15 мин назад — молчу", out.startswith("молчу"), out)
        s.measure(t1 + 41 * MIN)
        out = s.warden(0, t1 + 41 * MIN)
        check("подъём 21 мин назад — недобор", out.startswith("недобор\t"), out)


@case
def kick_few_tasks_flapping():
    """10.3.8а: «мало задач» 2→3→2→3 — недобор на каждом проходе, поднят_ts не тронут."""
    with Stand("medium", cards("p", 2)) as s:
        effs = []
        for i, plans in enumerate((2, 3, 2, 3)):
            at = BASE + 2 * i * MIN
            s.set_cards(cards("p", plans))
            st = s.measure(at)
            effs.append(st.get("эффективный"))
            out = s.warden(at - 25 * MIN, at)
            check(f"проход {i + 1}: недобор", out.startswith("недобор\t"), out)
            check(f"проход {i + 1}: поднят_ts не тронут", st.get("поднят_ts") is None,
                  st.get("поднят_ts"))
        check("эффективный 2→3→2→3", effs == [2, 3, 2, 3], effs)


@case
def kick_over():
    """10.3.8а: в работе 5 при эффективном 3 — лишнее 2."""
    with kick_stand(wip=5, plans=2) as s:
        s.measure(BASE)
        out = s.warden(0, BASE)
        check("лишнее\\t2", out == "лишнее\t2", out)
    with kick_stand(wip=3, plans=2) as s:
        s.measure(BASE)
        out = s.warden(0, BASE)
        check("в работе ровно потолок при готовых — «ровно»", out == "ровно", out)


# ═══════════════════════════ события, состояние, функции ══════════════════════

@case
def task_events():
    """8.2: task.taken / task.released по разнице в_работе между проходами."""
    with Stand("medium", cards("p", 3)) as s:
        s.measure(BASE)
        s.set_cards([{"id": "p1", "status": "wip"}] + cards("p", 3)[1:])
        s.measure(BASE + 2 * MIN)
        check("task.taken p1",
              any((r.get("data") or {}).get("id") == "p1" for r in s.events("task.taken")),
              [r.get("data") for r in s.events("task.taken")])
        s.set_cards(cards("p", 3))
        s.measure(BASE + 4 * MIN)
        check("task.released p1",
              any((r.get("data") or {}).get("id") == "p1" for r in s.events("task.released")),
              [r.get("data") for r in s.events("task.released")])
        check("mode.decision пишется", len(s.events("mode.decision")) >= 1, "нет mode.decision")


@case
def state_atomic():
    """4.3: читатель в цикле не видит половины состояния."""
    with Stand("medium", cards("p", 5)) as s:
        s.measure(BASE)
        path = s.panel / "скорость.json"
        inodes = [path.stat().st_ino]
        stop = threading.Event()
        seen = {"ok": 0, "bad": 0}

        def reader():
            while not stop.is_set():
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                    seen["ok"] += 1
                except ValueError:
                    seen["bad"] += 1
                except OSError:
                    pass
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for i in range(1, 9):
                s.measure(BASE + 2 * i * MIN)
                inodes.append(path.stat().st_ino)
        finally:
            stop.set()
            thread.join()
        check("половинок 0", seen["bad"] == 0, seen)
        check("читатель читал", seen["ok"] > 0, seen)
        check("каждый проход подменяет файл целиком (inode сменился)",
              all(a != b for a, b in zip(inodes, inodes[1:])), inodes)
        check("после проходов нет *.tmp*", list(s.panel.glob("*.tmp*")) == [],
              [p.name for p in s.panel.glob("*.tmp*")])


@case
def state_flock():
    """4.3: писатель ждёт flock на скорость.json.lock."""
    with Stand("medium", cards("p", 5)) as s:
        if not JUDGE.is_file():
            raise ProbeError(f"нет кода судьи: {JUDGE}")
        lock = open(s.panel / "скорость.json.lock", "a")
        fcntl.flock(lock, fcntl.LOCK_EX)
        s.tick()
        proc = subprocess.Popen([sys.executable, str(JUDGE), "--замер", "--на", iso(BASE)],
                                env=s.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(1.5)
            check("под чужим flock судья ждёт", proc.poll() is None, f"код {proc.poll()}")
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        code = proc.wait(timeout=TIMEOUT)
        check("после снятия flock — код 0", code == 0, code)


@case
def public_functions():
    """3.3: ceiling_alive() по эффективному, авария — 2, старше 10 мин — None; effective()."""
    with kick_stand() as s:
        s.measure(BASE)
        check("medium, эффективный 3 — ceiling_alive 5", s.call("ceiling_alive", BASE) == 5,
              s.call("ceiling_alive", BASE))
        check("effective() — 3", s.call("effective", BASE) == 3, s.call("effective", BASE))
        check("in_work() — [w1]", s.call("in_work", BASE) == ["w1"], s.call("in_work", BASE))
        check("состояние старше 10 мин — None",
              s.call("ceiling_alive", BASE + 11 * MIN) is None, "не None")
    with Stand("fast", cards("p", 6)) as s:
        s.measure(BASE)
        check("fast, эффективный 5 — 8", s.call("ceiling_alive", BASE) == 8,
              s.call("ceiling_alive", BASE))
    with Stand("ultra", cards("p", 1)) as s:
        s.measure(BASE)
        check("ultra, эффективный 1 (мало задач) — 3, судится эффективный",
              s.call("ceiling_alive", BASE) == 3, s.call("ceiling_alive", BASE))
    with Stand("ultra", cards("p", 12)) as s:
        s.set_load(4.5)
        s.measure(BASE)
        s.measure(BASE + 4 * MIN)
        check("авария — 2", s.call("ceiling_alive", BASE + 4 * MIN) == 2,
              s.call("ceiling_alive", BASE + 4 * MIN))


# ═══════════════════════════ лимит по расходу (4.5, 4.6) ══════════════════════

def linear_points(now, n, p5_now, slope5, r5, pw_now, sloped, rw, span_min=350):
    """Точки каждые 10 мин до now−10: процент растёт линейно, задач n."""
    pts = []
    for back in range(span_min, 0, -10):
        t = now - back * MIN
        pts.append((t, n, p5_now - slope5 * back / 60, r5, pw_now - sloped * back / 60, rw))
    return pts


def expected_speed(points, now, p_now, window_min, key) -> tuple[float, float]:
    """Независимый счёт спеки 4.5: интервалы строятся списками, а не накоплением в цикле."""
    column = 2 if key == "пять_часов" else 4
    inside = [row for row in points if row[0] >= now - window_min * MIN]
    times = [row[0] for row in inside] + [now]
    tasks = [row[1] for row in inside]
    percents = [row[column] for row in inside] + [p_now]
    steps = [(tasks[i], percents[i + 1] - percents[i], (times[i + 1] - times[i]) / HOUR)
             for i in range(len(inside))]
    useful = [(n, rise, hours) for n, rise, hours in steps if n >= 1 and rise >= 0]
    task_hours = sum(n * hours for n, _, hours in useful)
    return (sum(rise for _, rise, _ in useful) / task_hours if task_hours else 0.0), task_hours


def limit_stand(mode="ultra") -> Stand:
    return Stand(mode, cards("w", 2, "wip") + cards("p", 10))


def window_field(st, window, field):
    return ((st.get("лимит") or {}).get(window) or {}).get(field)


@case
def limit_read_exact():
    """16а, 16б: файл подменён — проценты и «точно»; скорость сходится; неделя хуже — k = 4."""
    with limit_stand() as s:
        now = BASE
        r5, rw = now + 3 * HOUR, now + 48 * HOUR
        pts = linear_points(now, 2, 20, 2.0, r5, 60, 0.3, rw)
        s.seed_points(pts)
        s.limits(now, five=(20, r5), week=(60, rw))
        st = s.measure(now)
        lim = st.get("лимит") or {}
        check("точность «точно»", lim.get("точность") == "точно", lim.get("точность"))
        check("5 часов: 20 %", window_field(st, "пять_часов", "процент") == 20,
              window_field(st, "пять_часов", "процент"))
        check("неделя: 60 %", window_field(st, "неделя", "процент") == 60,
              window_field(st, "неделя", "процент"))
        for window, p_now, minutes in (("пять_часов", 20, 60), ("неделя", 60, 360)):
            want, _ = expected_speed(pts, now, p_now, minutes, window)
            got = window_field(st, window, "скорость_на_задачу")
            check(f"скорость {window} сходится с независимым счётом ({want:.4f})",
                  isinstance(got, (int, float)) and abs(got - want) < 1e-6, got)
        k_week = math.floor((95 - 60) / (0.15 * 48))
        check(f"неделя хуже: эффективный {k_week}", st.get("эффективный") == k_week == 4,
              st.get("эффективный"))
        check("лимит.потолок 4", lim.get("потолок") == 4, lim.get("потолок"))
        fields, _ = s.show(now)
        line = fields.get("лимит", "")
        check("--показать: лимит 20 % и 60 %, «точно»",
              "20 %" in line and "60 %" in line and "точно" in line, line)
        got = s.call("read_limit", "неделя", now)
        check("read_limit(неделя) — 60, сброс, точно",
              isinstance(got, dict) and got.get("процент") == 60 and got.get("сброс") == rw
              and got.get("точность") == "точно", got)


@case
def limit_worse_five_hours():
    """16б: 5 часов хуже недели — берётся меньшее."""
    with limit_stand() as s:
        now = BASE
        r5, rw = now + 3 * HOUR, now + 10 * HOUR
        s.seed_points(linear_points(now, 2, 79.5, 2.0, r5, 60, 0.3, rw))
        s.limits(now, five=(79.5, r5), week=(60, rw))
        st = s.measure(now)
        check("5 часов: k = floor(15,5/3) = 5", st.get("эффективный") == 5, st.get("эффективный"))


@case
def limit_few_data():
    """16б: мало данных — консервативная скорость и «оценка»."""
    with limit_stand() as s:
        now = BASE
        s.limits(now, five=(0, now + HOUR), week=(10, now + 10 * HOUR))
        st = s.measure(now)
        check("точность «оценка»", (st.get("лимит") or {}).get("точность") == "оценка",
              (st.get("лимит") or {}).get("точность"))
        check("скорость 5 часов — консервативная 8",
              window_field(st, "пять_часов", "скорость_на_задачу") == 8,
              window_field(st, "пять_часов", "скорость_на_задачу"))
        check("скорость недели — консервативная 0,5",
              window_field(st, "неделя", "скорость_на_задачу") == 0.5,
              window_field(st, "неделя", "скорость_на_задачу"))
        check("эффективный 10 (k5 = 11, kнед = 17, зажаты в N)", st.get("эффективный") == 10,
              st.get("эффективный"))


@case
def limit_stop():
    """16б: использовано ≥ стоп_pct — 0."""
    with limit_stand() as s:
        now = BASE
        s.limits(now, five=(98.5, now + HOUR), week=(10, now + 10 * HOUR))
        st = s.measure(now)
        check("эффективный 0", st.get("эффективный") == 0, st.get("эффективный"))
        out = s.warden(0, now)
        check("--сторожу молчит", out.startswith("молчу"), out)


def drop_stand(r5_after_min=150, p5=79):
    """Стенд 16в: s5 = 1,5 %/ч на задачу (2 задачи, 3 %/ч), неделя не ограничивает."""
    s = limit_stand()
    now = BASE
    r5, rw = now + r5_after_min * MIN, now + 100 * HOUR
    s.seed_points(linear_points(now, 2, p5, 3.0, r5, 0, 0.0, rw))
    s.limits(now, five=(p5, r5), week=(0, rw))
    return s, r5, rw


@case
def limit_rise_interval():
    """16в: без сброса при росте k — за 18 мин 0 подъёмов, на 20-й один."""
    s, r5, rw = drop_stand()
    with s:
        st = s.measure(BASE)
        check("снижение до 4", (st.get("лимит") or {}).get("потолок") == 4,
              (st.get("лимит") or {}).get("потолок"))
        ceilings = []
        for minute in range(2, 21, 2):
            at = BASE + minute * MIN
            s.limits(at, five=(79, r5), week=(0, rw))
            ceilings.append(((s.measure(at).get("лимит") or {}).get("потолок")))
        check("2…18 мин — потолок 4", ceilings[:-1] == [4] * 9, ceilings)
        check("20 мин — один подъём выше 4",
              isinstance(ceilings[-1], int) and ceilings[-1] > 4, ceilings[-1])
        rises = sum(1 for a, b in zip([4] + ceilings, ceilings) if b is not None and a is not None
                    and b > a)
        check("подъёмов ровно 1", rises == 1, ceilings)


@case
def limit_reset_by_drop():
    """16в: сброс падением: наклон старого окна отброшен, проход считает по догадке 8 %/ч."""
    s, r5, rw = drop_stand()
    with s:
        st = s.measure(BASE)
        check("снижение до 4", st.get("эффективный") == 4, st.get("эффективный"))
        at = BASE + 2 * MIN
        s.limits(at, five=(5, at + 5 * HOUR), week=(0, rw))
        st = s.measure(at)
        # floor((95 − 5) / (8 · 5)) = 2, начатых 2: max(2, min(10, 2)) = 2.
        check("сброс — эффективный 2 по догадке", st.get("эффективный") == 2, st.get("эффективный"))


@case
def limit_reset_by_time():
    """16в: resets_at прошёл — потолок окна снят сразу, без интервала подъёма."""
    s, r5, rw = drop_stand(r5_after_min=10, p5=93.4)
    with s:
        st = s.measure(BASE)
        check("снижение (k = floor(1,6/0,25) = 6)", st.get("эффективный") == 6,
              st.get("эффективный"))
        at = BASE + 12 * MIN
        s.limits(at, five=(93.4, r5), week=(0, rw))
        st = s.measure(at)
        check("сброс прошёл 2 мин назад — эффективный 10", st.get("эффективный") == 10,
              st.get("эффективный"))


@case
def limit_refusal():
    """16г: протух / нет / битый / без rate_limits / адаптер api — блокер не действует."""
    variants = (
        ("файл старше устарел_мин", lambda s: s.limits(BASE, five=(98.5, BASE + HOUR),
                                                       week=(98.5, BASE + HOUR), age_s=16 * MIN)),
        ("файла нет", lambda s: None),
        ("битый JSON", lambda s: s.limits(BASE, raw="{битый")),
        ("нет rate_limits", lambda s: s.limits(BASE, raw=json.dumps({"ts": BASE}))),
    )
    for name, prepare in variants:
        with limit_stand() as s:
            prepare(s)
            st = s.measure(BASE)
            check(f"{name}: точность «отказ»", (st.get("лимит") or {}).get("точность") == "отказ",
                  (st.get("лимит") or {}).get("точность"))
            check(f"{name}: эффективный не снижен (10)", st.get("эффективный") == 10,
                  st.get("эффективный"))
            got = s.call("read_limit", "пять_часов", BASE)
            check(f"{name}: read_limit — отказ",
                  isinstance(got, dict) and got.get("точность") == "отказ", got)
            _, text = s.show(BASE)
            check(f"{name}: --показать «данных нет … 429»",
                  "данных нет" in text and "429" in text, text[-300:])
    with Stand("ultra", cards("w", 2, "wip") + cards("p", 10),
               data_patch={"лимит_подписки.адаптер": "api"}) as s:
        s.limits(BASE, five=(98.5, BASE + HOUR), week=(98.5, BASE + HOUR))
        st = s.measure(BASE)
        check("адаптер api: отказ, эффективный 10",
              (st.get("лимит") or {}).get("точность") == "отказ" and st.get("эффективный") == 10,
              ((st.get("лимит") or {}).get("точность"), st.get("эффективный")))
    with limit_stand() as s:
        s.limit_fact()
        st = s.measure(BASE)
        check("отказ + факт 429 — 0", st.get("эффективный") == 0, st.get("эффективный"))


@case
def limit_keeps_started():
    """16д: снижение по лимиту ничего не останавливает — начатые в работе, сторож «лишнее»."""
    with Stand("ultra", cards("w", 5, "wip") + cards("p", 7)) as s:
        now = BASE
        r5, rw = now + 3 * HOUR, now + 48 * HOUR
        s.seed_points(linear_points(now, 2, 20, 2.0, r5, 60, 0.3, rw))
        s.limits(now, five=(20, r5), week=(60, rw))
        st = s.measure(now)
        check("эффективный 4", st.get("эффективный") == 4, st.get("эффективный"))
        check("в работе все 5", sorted(st.get("в_работе") or []) == [f"w{i}" for i in range(1, 6)],
              st.get("в_работе"))
        out = s.warden(0, now)
        check("--сторожу: лишнее\\t1", out == "лишнее\t1", out)


# ═══════════════════════════ находки ревью Ч2 ═════════════════════════════════

def attempt(s: Stand, fn: str, *args) -> tuple:
    """(ответ, текст сбоя): упавшая функция — ответ, который судится, а не сбой стенда."""
    try:
        return s.call(fn, *args), ""
    except ProbeError as exc:
        return None, str(exc)[-300:] or "упал"


def warn_files(s: Stand, at: float, count: int = 3) -> None:
    for i in range(count):
        s.append("заявки-на-файлы.jsonl", {
            "v": 1, "ts": iso(at - (5 + i) * MIN), "src": "scripts/hooks/file_claim_guard.py",
            "event": "file.claim.warn", "outcome": "ok", "task": "w1", "task_from": "arg",
            "session": "s1", "data": {"путь": f"/x/{i}.py", "агент": f"a{i}",
                                      "общий": False, "занявшие": ["b"], "номер": 1}})


def files_ceiling(state: dict):
    return ((state.get("блокеры") or {}).get("файлы") or {}).get("потолок")


def cli_verdict(done: subprocess.CompletedProcess) -> str:
    return f"код {done.returncode}: {done.stderr.strip()[-200:]}"


@case
def power_quiet_zone():
    """4.2, 4.4: занято 80 % — не «тихо», П не растёт; 58 % — подъём через 20 мин своего отсчёта."""
    with Stand("ultra", cards("w", 10, "wip") + cards("p", 2)) as s:
        t0 = BASE
        s.busy_pct = 97
        s.warm_up(t0)
        s.measure(t0)
        st = s.measure(t0 + 10 * MIN)
        check("признак 10 мин — П 5", power_ceiling(st) == 5, power_ceiling(st))
        s.busy_pct = 80
        held = [power_ceiling(s.measure(t0 + minute * MIN)) for minute in (12, 22, 32, 42)]
        check(f"80 % 30 мин (не ниже порога тишины {CPU_BUSY_QUIET}) — П держится 5",
              held == [5] * 4, held)
        t1 = t0 + 44 * MIN
        s.busy_pct = 58
        s.measure(t1)
        st = s.measure(t1 + 19 * MIN)
        check("58 % 19 мин — П ещё 5", power_ceiling(st) == 5, power_ceiling(st))
        st = s.measure(t1 + 20 * MIN)
        check("58 % 20 мин — подъём: блокер снят, эффективный 10",
              "мощность" not in blockers(st) and st.get("эффективный") == 10,
              (power_ceiling(st), st.get("эффективный")))


@case
def accident_long_memory():
    """4.2: авария по памяти 64+ мин — лестница стоит (П 1, шаг_ts каждый проход); после — 1, затем 3."""
    with Stand("ultra", cards("w", 10, "wip") + cards("p", 2)) as s:
        t0 = BASE
        s.mem_free_pct = 5
        s.write_proc()
        s.measure(t0)
        ceilings, stamps = [], []
        for minute in (4, 14, 24, 34, 44, 54, 64, 68):
            st = s.measure(t0 + minute * MIN)
            ceilings.append(power_ceiling(st))
            stamp = ((st.get("блокеры") or {}).get("мощность") or {}).get("шаг_ts")
            stamps.append(epoch(stamp) == t0 + minute * MIN)
        check("авария 4…68 мин — П 1 на каждом проходе", ceilings == [1] * 8, ceilings)
        check("авария — шаг_ts = сейчас на каждом проходе", stamps == [True] * 8, stamps)
        t1 = t0 + 70 * MIN
        s.mem_free_pct = 50
        s.write_proc()
        s.measure(t1)
        st = s.measure(t1 + 20 * MIN)
        check("20 мин без признака — авария снята", "авария" not in blockers(st), blockers(st))
        check("снятие долгой аварии — эффективный 1, а не выбранный 10",
              st.get("эффективный") == 1, st.get("эффективный"))
        st = s.measure(t1 + 40 * MIN)
        check("ещё 20 мин — ступень 3", st.get("эффективный") == 3, st.get("эффективный"))


@case
def files_ceiling_fixed():
    """5: потолок «файлы» ставится при включении: в работе 10 → 5; в работе стало 5 — остаётся 5, не 3."""
    with Stand("ultra", cards("w", 10, "wip") + cards("p", 10)) as s:
        warn_files(s, BASE)
        st = s.measure(BASE)
        check("включение при 10 в работе — потолок 5", files_ceiling(st) == 5, files_ceiling(st))
        s.set_cards(cards("w", 5, "wip") + cards("p", 10))
        st = s.measure(BASE + 2 * MIN)
        check("в работе 5 — потолок файлов остаётся 5", files_ceiling(st) == 5, files_ceiling(st))
        check("эффективный 5", st.get("эффективный") == 5, st.get("эффективный"))


@case
def limit_file_garbage():
    """4.6: NaN в процентах и сбросе, resets_at в миллисекундах — код 0, окно «оценка», сброса нет."""
    r5, rw = BASE + 3 * HOUR, BASE + 48 * HOUR
    week = {"used_percentage": 10, "resets_at": int(rw)}
    variants = (
        ("NaN в used_percentage", {"used_percentage": math.nan, "resets_at": int(r5)}, "процент"),
        ("NaN в resets_at", {"used_percentage": 20, "resets_at": math.nan}, "сброс"),
        ("resets_at в миллисекундах", {"used_percentage": 20, "resets_at": int(r5) * 1000}, "сброс"),
    )
    for name, five, empty in variants:
        with limit_stand() as s:
            s.limits(BASE, raw=json.dumps({"ts": BASE, "rate_limits": {"five_hour": five,
                                                                        "seven_day": week}}))
            s.tick()
            done = s.run("--замер", at=BASE)
            check(f"{name}: код 0 без трейсбека",
                  done.returncode == 0 and "Traceback" not in done.stderr, cli_verdict(done))
            if done.returncode != 0:
                continue
            st = s.state()
            check(f"{name}: точность «оценка»", (st.get("лимит") or {}).get("точность") == "оценка",
                  (st.get("лимит") or {}).get("точность"))
            check(f"{name}: у окна 5 часов нет поля «{empty}»",
                  window_field(st, "пять_часов", empty) is None, window_field(st, "пять_часов", empty))
            if empty == "сброс":
                got, err = attempt(s, "read_limit", "пять_часов", BASE)
                check(f"{name}: read_limit — сброса нет, «оценка»",
                      not err and got.get("сброс") is None and got.get("точность") == "оценка",
                      err or got)


@case
def unexpected_error():
    """4.1.6: исключение вне MeasureError (decide подменён) — error в журнале, код 2, состояние не переписано."""
    with Stand("medium", cards("p", 5)) as s:
        s.measure(BASE)
        before = (s.panel / "скорость.json").read_bytes()
        s.tick()
        done = subprocess.run([sys.executable, "-c", BROKEN_DECIDE_DRIVER, str(JUDGE), "--замер",
                               "--на", iso(BASE + 2 * MIN)], env=s.env(), capture_output=True,
                              text=True, timeout=TIMEOUT)
        check("код 2", done.returncode == 2, cli_verdict(done))
        check("mode.decision outcome=error",
              any(r.get("outcome") == "error" for r in s.events("mode.decision")),
              [r.get("outcome") for r in s.events("mode.decision")])
        check("состояние не переписано", (s.panel / "скорость.json").read_bytes() == before,
              "файл изменён")


@case
def limit_stop_edges():
    """4.5: p ≥ стоп при прошедшем сбросе — окно новое, потолок N; стоп ровно 98 — 0, 97,9 — 1."""
    with limit_stand() as s:
        s.limits(BASE, five=(98.5, BASE - 10 * MIN), week=(10, BASE + 10 * HOUR))
        st = s.measure(BASE)
        check("98,5 % и сброс 10 мин назад — эффективный 10", st.get("эффективный") == 10,
              st.get("эффективный"))
    # В работе 0: нижняя граница «мало данных» min(N, max(0, 1)) = 1 не маскирует 97,9 → 1.
    for percent, want in ((98, 0), (97.9, 1)):
        with Stand("ultra", cards("p", 10)) as s:
            s.limits(BASE, five=(percent, BASE + HOUR), week=(10, BASE + 10 * HOUR))
            st = s.measure(BASE)
            check(f"{percent} % до сброса — эффективный {want}", st.get("эффективный") == want,
                  st.get("эффективный"))


@case
def limit_estimate_floor():
    """4.5: мало данных — не ниже min(выбранный, max(в_работе, 1)); без resets_at — T = 168 ч, без границы."""
    with Stand("ultra", cards("w", 5, "wip") + cards("p", 10)) as s:
        s.limits(BASE, five=(0, BASE + HOUR), week=(0, BASE + 167 * HOUR))
        st = s.measure(BASE)
        check("точек нет — «оценка»", (st.get("лимит") or {}).get("точность") == "оценка",
              (st.get("лимит") or {}).get("точность"))
        check("неделя 0 %, сброс через 167 ч, 5 в работе — эффективный 5, не 1",
              st.get("эффективный") == 5, st.get("эффективный"))
    with Stand("ultra", cards("w", 5, "wip") + cards("p", 10)) as s:
        now = BASE
        r5 = now + 3 * HOUR
        s.seed_points(linear_points(now, 2, 20, 2.0, r5, 10, 0.3, now + 48 * HOUR))
        s.limits(now, five=(20, r5), week=(10, None))
        st = s.measure(now)
        want = math.floor((95 - 10) / (0.15 * 168))
        check("неделя без resets_at — «оценка», сброса нет",
              (st.get("лимит") or {}).get("точность") == "оценка"
              and window_field(st, "неделя", "сброс") is None,
              ((st.get("лимит") or {}).get("точность"), window_field(st, "неделя", "сброс")))
        check(f"без resets_at T = 168 ч, данных достаточно — эффективный {want}, границы нет",
              st.get("эффективный") == want == 3, st.get("эффективный"))


@case
def limit_rise_per_window():
    """4.5: метка смены потолка у каждого окна своя — снижение недели не откладывает подъём 5 часов."""
    s, r5, rw = drop_stand()
    with s:
        st = s.measure(BASE)
        check("5 часов снижены до 4", window_field(st, "пять_часов", "потолок") == 4,
              window_field(st, "пять_часов", "потолок"))
        for minute in range(2, 21, 2):
            at = BASE + minute * MIN
            s.limits(at, five=(79, r5), week=(0, rw) if minute < 12 else (50, rw))
            st = s.measure(at)
            if minute == 12:
                check("12 мин: неделя снизила свой потолок",
                      (window_field(st, "неделя", "потолок") or 10) < 4,
                      window_field(st, "неделя", "потолок"))
                check("12 мин: сменён_ts 5 часов — её снижение (0 мин)",
                      epoch(window_field(st, "пять_часов", "сменён_ts")) == BASE,
                      window_field(st, "пять_часов", "сменён_ts"))
                check("12 мин: сменён_ts недели — 12 мин",
                      epoch(window_field(st, "неделя", "сменён_ts")) == at,
                      window_field(st, "неделя", "сменён_ts"))
            if minute == 18:
                check("18 мин — 5 часов ещё 4", window_field(st, "пять_часов", "потолок") == 4,
                      window_field(st, "пять_часов", "потолок"))
        got = window_field(st, "пять_часов", "потолок")
        check("20 мин — потолок 5 часов поднят, смена недели на 12-й его не держит",
              isinstance(got, int) and got > 4, got)


@case
def future_stamps():
    """4.1.6, 4.6: ts в будущем — файл лимитов «отказ»; состояние сверх допуска 5 с — не свежее."""
    with limit_stand() as s:
        s.limits(BASE, five=(98.5, BASE + HOUR), week=(98.5, BASE + HOUR), age_s=-10 * MIN)
        st = s.measure(BASE)
        check("файл лимитов на 10 мин из будущего — «отказ»",
              (st.get("лимит") or {}).get("точность") == "отказ", (st.get("лимит") or {}).get("точность"))
        check("эффективный не снижен (10)", st.get("эффективный") == 10, st.get("эффективный"))
        got, err = attempt(s, "read_limit", "пять_часов", BASE)
        check("read_limit — «отказ»", not err and got.get("точность") == "отказ", err or got)
    with kick_stand() as s:
        s.measure(BASE)
        at = BASE - MIN
        for fn in ("effective", "ceiling_alive"):
            got, err = attempt(s, fn, at)
            check(f"состояние на 60 с из будущего — {fn}() None", not err and got is None, err or got)
        for args in (("--показать",), ("--сторожу", "--последний-пинок", "0")):
            done = s.run(*args, at=at)
            check(f"состояние на 60 с из будущего — {args[0]} код 2", done.returncode == 2,
                  cli_verdict(done))
        got, err = attempt(s, "effective", BASE - 3)
        check("на 3 с из будущего (в допуске) — effective() 3", not err and got == 3, err or got)


@case
def state_incomplete():
    """4.1.6: состояние без «эффективный» или поля читателей, с нечисловым, только ts — функции None, CLI код 2."""
    with kick_stand() as s:
        s.measure(BASE)
        path = s.panel / "скорость.json"
        full = json.loads(path.read_text(encoding="utf-8"))
        # bool — подкласс int: True прошёл бы как «1 задача», если судья не отсеет его явно.
        variants = [("эффективный «три»", {**full, "эффективный": "три"}),
                    ("эффективный True", {**full, "эффективный": True}),
                    ("только ts", {"ts": full["ts"]})]
        # Без поля, которое печатают --показать и --сторожу, состояние неполное, а не «пустое».
        for field in ("эффективный", "блокеры", "к_взятию", "добор"):
            if field not in full:
                raise ProbeError(f"в состоянии судьи нет поля {field} — стенд не тот")
            variants.append((f"без поля {field}", {k: v for k, v in full.items() if k != field}))
        for name, body in variants:
            path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            for fn in ("effective", "ceiling_alive"):
                got, err = attempt(s, fn, BASE)
                check(f"{name}: {fn}() — None без исключения", not err and got is None, err or got)
            for args in (("--показать",), ("--сторожу", "--последний-пинок", "0")):
                done = s.run(*args, at=BASE)
                check(f"{name}: {args[0]} — код 2 без трейсбека",
                      done.returncode == 2 and "Traceback" not in done.stderr, cli_verdict(done))


@case
def state_stale():
    """4.1.6: состояние старше 10 мин — --сторожу и --показать код 2, функции None."""
    with kick_stand() as s:
        s.measure(BASE)
        at = BASE + 11 * MIN
        for args in (("--показать",), ("--сторожу", "--последний-пинок", "0")):
            done = s.run(*args, at=at)
            check(f"11 мин — {args[0]} код 2 «не отвечает»",
                  done.returncode == 2 and "не отвечает" in done.stdout, cli_verdict(done))
        for fn in ("effective", "ceiling_alive"):
            got, err = attempt(s, fn, at)
            check(f"11 мин — {fn}() None", not err and got is None, err or got)


def drain_run_start(journal: Path) -> None:
    """Метка запуска events.run пишется первой: её пропускаем и закрываем читателя, чтобы
    следующая запись (событие прохода) снова держалась на открытии FIFO."""
    fd = os.open(journal, os.O_RDONLY | os.O_NONBLOCK)
    try:
        seen, deadline = b"", time.monotonic() + 8
        while b'"run.start"' not in seen and time.monotonic() < deadline:
            try:
                seen += os.read(fd, 65536)
            except BlockingIOError:
                pass
            time.sleep(0.05)
        check("run.start: метка запуска прошла в журнал до событий прохода", b'"run.start"' in seen,
              seen.decode("utf-8", "replace")[:200])
    finally:
        os.close(fd)


def pass_into_fifo(s: Stand, at: float, first: str) -> None:
    """Проход судьи с журналом-FIFO без читателя: первая же запись события его держит.

    Состояние к этому моменту обязано быть новым; first — какое событие прохода первое,
    чтобы вариант точно проверял ту запись, ради которой он заведён."""
    journal = s.log / JOURNAL
    journal.rename(s.log / "режим-скорости.прежний")
    os.mkfifo(journal)
    s.tick()
    if not JUDGE.is_file():
        raise ProbeError(f"нет кода судьи: {JUDGE}")
    proc = subprocess.Popen([sys.executable, str(JUDGE), "--замер", "--на", iso(at)],
                            env=s.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    reader = None
    try:
        drain_run_start(journal)
        written, deadline = False, time.monotonic() + 8
        while not written and time.monotonic() < deadline and proc.poll() is None:
            try:
                written = epoch(s.state().get("ts")) == at
            except (ProbeError, ValueError):
                pass
            time.sleep(0.1)
        check(f"{first}: судья ждёт журнал (FIFO без читателя держит первую запись)",
              proc.poll() is None, f"код {proc.poll()}")
        check(f"{first}: к первой записи в журнал состояние уже новое", written, "состояние прежнее")
        reader = os.open(journal, os.O_RDONLY | os.O_NONBLOCK)
        proc.communicate(timeout=TIMEOUT)
        chunks = []
        while True:
            try:
                chunk = os.read(reader, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        rows = [json.loads(line) for line in b"".join(chunks).decode("utf-8").splitlines() if line.strip()]
        names = [row.get("event") for row in rows]
        check(f"{first}: первое событие прохода — {first}", names[:1] == [first], names)
        check(f"{first}: mode.decision дошёл через FIFO", "mode.decision" in names, names)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if reader is not None:
            os.close(reader)


@case
def state_before_events():
    """4.3: состояние пишется раньше любого события прохода (журнал — FIFO без читателя); ошибка записи не оставляет tmp."""
    with kick_stand() as s:
        s.measure(BASE)
        pass_into_fifo(s, BASE + 11 * MIN, "mode.decision")  # пульс: других событий нет
    with kick_stand() as s:
        s.measure(BASE)
        s.set_mode("ultra")
        pass_into_fifo(s, BASE + MIN, "mode.selected")
    with kick_stand() as s:
        s.measure(BASE)
        # Эффективный → 0 паузой, не лимитом: лимит читает историю из того же журнала-FIFO.
        s.pause()
        pass_into_fifo(s, BASE + MIN, "mode.change")
    with kick_stand() as s:
        target = s.panel / "скорость.json"
        target.mkdir()
        (target / "занято").write_text("x", encoding="utf-8")
        s.tick()
        done = s.run("--замер", at=BASE)
        check("запись состояния не удалась — код не 0", done.returncode != 0, cli_verdict(done))
        leftovers = [p.name for p in s.panel.glob("скорость.json.tmp*")]
        check("после ошибки записи tmp-файла нет", leftovers == [], leftovers)


@case
def files_candidate_show():
    """5: кандидат «файлы» (держится_мин > 0 в данных стенда) — --показать печатает его и не падает."""
    with Stand("ultra", cards("w", 10, "wip"), data_patch={"блокеры.файлы.держится_мин": 10}) as s:
        warn_files(s, BASE)
        st = s.measure(BASE)
        check("файлы — кандидат, не блокер",
              "файлы" in (st.get("кандидаты") or {}) and "файлы" not in blockers(st),
              (sorted(st.get("кандидаты") or {}), blockers(st)))
        done = s.run("--показать", at=BASE)
        check("--показать код 0", done.returncode == 0, cli_verdict(done))
        check("строка «кандидат файлы»", "кандидат файлы" in done.stdout, done.stdout[-300:])


@case
def kick_value_garbage():
    """CLI: --последний-пинок «1.2.3», inf, -inf, nan — код 0, пинок неизвестен (как 0), без трейсбека."""
    with kick_stand() as s:
        s.set_load(1.0)
        s.measure(BASE)
        # float() принимает inf и nan; неотсеянный inf роняет round() в строке «пинок был … мин назад».
        for kick in ("1.2.3", "inf", "-inf", "nan"):
            done = s.run("--сторожу", "--последний-пинок", kick, at=BASE)
            check(f"{kick}: код 0 без трейсбека", done.returncode == 0 and "Traceback" not in done.stderr,
                  cli_verdict(done))
            check(f"{kick}: ответ как при пинке 0 — недобор", done.stdout.startswith("недобор\t"),
                  done.stdout)


@case
def limit_speed_window():
    """4.5: точка на границе окна входит, излом за границей — нет; интервал с n = 0 не считается."""
    with limit_stand() as s:
        now = BASE
        r5, rw = now + 3 * HOUR, now + 100 * HOUR
        # (минут назад, задач, процент 5 часов): крутой наклон за границей окна 60 мин,
        # интервал 30→20 при n = 0 (рост 7 не в счёт). Считаются 60→30 (5 пунктов) и 20→0
        # (2 пункта): 7 пунктов за 5 интервалов по 10 мин при n = 2, то есть за 5/3 задача-часа.
        shape = ((120, 2, 2), (110, 2, 4), (100, 2, 6), (90, 2, 8), (80, 2, 10), (70, 2, 12),
                 (60, 2, 30), (50, 2, 33), (40, 2, 34), (30, 0, 35), (20, 2, 42), (10, 2, 43))
        pts = [(now - back * MIN, n, p5, r5, 5, rw) for back, n, p5 in shape]
        s.seed_points(pts)
        s.limits(now, five=(44, r5), week=(5, rw))
        st = s.measure(now)
        want, _ = expected_speed(pts, now, 44, 60, "пять_часов")
        check("независимый счёт: 7 пунктов за 5/3 задача-часа = 4,2", abs(want - 4.2) < 1e-9, want)
        got = window_field(st, "пять_часов", "скорость_на_задачу")
        check("скорость 5 часов сходится с независимым счётом",
              isinstance(got, (int, float)) and abs(got - want) < 1e-6, got)


@case
def limit_inputs_misc():
    """4.6: процент > 100, resets_at ISO-строкой, одно окно из двух, сдвиг resets_at и падение — разрыв."""
    with limit_stand() as s:
        s.limits(BASE, five=(105, BASE + HOUR), week=(10, BASE + 10 * HOUR))
        st = s.measure(BASE)
        check("105 % — эффективный 0", st.get("эффективный") == 0, st.get("эффективный"))
    with limit_stand() as s:
        now = BASE
        r5, rw = now + 3 * HOUR, now + 48 * HOUR
        s.seed_points(linear_points(now, 2, 20, 2.0, r5, 60, 0.3, rw))
        s.limits(now, five=(20, iso(r5)), week=(60, iso(rw)))
        st = s.measure(now)
        check("resets_at ISO — «точно», эффективный 4",
              (st.get("лимит") or {}).get("точность") == "точно" and st.get("эффективный") == 4,
              ((st.get("лимит") or {}).get("точность"), st.get("эффективный")))
        got, err = attempt(s, "read_limit", "неделя", now)
        check("read_limit: сброс ISO → epoch", not err and got.get("сброс") == rw, err or got)
    with limit_stand() as s:
        now = BASE
        r5 = now + 3 * HOUR
        # Неделя в точках 0 %: окна нет в свежем файле — это 0 % (4.6), история не спорит с ним.
        s.seed_points(linear_points(now, 2, 20, 2.0, r5, 0, 0.0, now + 48 * HOUR))
        s.limits(now, five=(20, r5))
        st = s.measure(now)
        check("одно окно из двух — «оценка», эффективный 10",
              (st.get("лимит") or {}).get("точность") == "оценка" and st.get("эффективный") == 10,
              ((st.get("лимит") or {}).get("точность"), st.get("эффективный")))
    # Догадка 8 %/ч и порог 95 при 75 %: потолок floor(20 / (8 · T)) = 1 и при T = 150 мин
    # (сдвиг вперёд), и при T = 148 мин (сдвига нет); начатых 2: max(1, min(10, 2)) = 2.
    for shift in (2 * MIN, 0):
        s, r5, rw = drop_stand()
        with s:
            s.measure(BASE)
            at = BASE + 2 * MIN
            s.limits(at, five=(75, r5 + shift), week=(0, rw))
            st = s.measure(at)
            label = "сдвинут вперёд на 2 мин" if shift else "не сдвинут, падение 4"
            check(f"resets_at {label} — разрыв, догадка, потолок 2",
                  window_field(st, "пять_часов", "потолок") == 2,
                  window_field(st, "пять_часов", "потолок"))


GONE = {"процент": 0, "сброс": None, "точность": "оценка"}
REFUSED = {"процент": None, "сброс": None, "точность": "отказ"}


@case
def limit_window_gone():
    """4.6: свежий файл без окна — окно сброшено: 0 %, «оценка»; пустой, старый или мусорный — «отказ»."""
    r5, rw = BASE + 3 * HOUR, BASE + 48 * HOUR
    for gone, kept, five, week, want in (
            ("пять_часов", "неделя", None, (60, rw), {"процент": 60, "сброс": rw, "точность": "точно"}),
            ("неделя", "пять_часов", (20, r5), None, {"процент": 20, "сброс": r5, "точность": "точно"})):
        with limit_stand() as s:
            s.limits(BASE, five=five, week=week)
            got, err = attempt(s, "read_limit", gone, BASE)
            check(f"нет {gone}: read_limit — 0 %, сброса нет, «оценка»", not err and got == GONE,
                  err or got)
            got, err = attempt(s, "read_limit", kept, BASE)
            check(f"нет {gone}: {kept} как пришло", not err and got == want, err or got)
            st = s.measure(BASE)
            check(f"нет {gone}: в состоянии {gone} 0 %, точность «оценка»",
                  window_field(st, gone, "процент") == 0
                  and (st.get("лимит") or {}).get("точность") == "оценка",
                  (window_field(st, gone, "процент"), (st.get("лимит") or {}).get("точность")))
    # Окно названо, но не разобрать — это не сброс, а мусор: «отказ» только этому окну.
    week = {"used_percentage": 60, "resets_at": int(rw)}
    for name, cell in (("five_hour: null", None), ("five_hour без used_percentage", {"resets_at": int(r5)}),
                       ("used_percentage строкой", {"used_percentage": "20", "resets_at": int(r5)})):
        with limit_stand() as s:
            s.limits(BASE, raw=json.dumps({"ts": BASE, "rate_limits": {"five_hour": cell,
                                                                        "seven_day": week}}))
            got, err = attempt(s, "read_limit", "пять_часов", BASE)
            check(f"{name}: пять_часов «отказ», не 0 %", not err and got == REFUSED, err or got)
    # Пустой rate_limits схема Claude Code не даёт, Ч9 его не пишет — странному файлу не верим.
    stale = json.dumps({"ts": BASE - 16 * MIN, "rate_limits": {"seven_day": week}})
    for name, raw in (("rate_limits {}", json.dumps({"ts": BASE, "rate_limits": {}})),
                      ("rate_limits null", json.dumps({"ts": BASE, "rate_limits": None})),
                      ("старше устарел_мин, окна нет", stale)):
        with limit_stand() as s:
            s.limits(BASE, raw=raw)
            for window in ("пять_часов", "неделя"):
                got, err = attempt(s, "read_limit", window, BASE)
                check(f"{name}: {window} «отказ»", not err and got == REFUSED, err or got)
            st = s.measure(BASE)
            check(f"{name}: точность «отказ», эффективный 10",
                  (st.get("лимит") or {}).get("точность") == "отказ" and st.get("эффективный") == 10,
                  ((st.get("лимит") or {}).get("точность"), st.get("эффективный")))


@case
def limit_window_gone_after_reset():
    """4.5, 4.6: 5 часов снижены, сброс прошёл и окно пропало из файла — 0 %, потолок по догадке."""
    s, r5, rw = drop_stand(r5_after_min=10, p5=93.4)
    with s:
        st = s.measure(BASE)
        check("снижение до 6", st.get("эффективный") == 6, st.get("эффективный"))
        at = BASE + 12 * MIN
        s.limits(at, week=(0, rw))
        st = s.measure(at)
        # Сброса нет, T = полное окно 5 ч: floor(95 / (8 · 5)) = 2, начатых 2: max(2, 2) = 2.
        check("окна нет — эффективный 2 по догадке", st.get("эффективный") == 2,
              st.get("эффективный"))
        check("пять_часов в состоянии: 0 %, сброса нет",
              window_field(st, "пять_часов", "процент") == 0
              and window_field(st, "пять_часов", "сброс") is None,
              (st.get("лимит") or {}).get("пять_часов"))
        check("точность «оценка»", (st.get("лимит") or {}).get("точность") == "оценка",
              (st.get("лимит") or {}).get("точность"))


# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════ сообщения владельцу (Ч8, 10.3.10) ═══════════════════════

SEND_STUB = r'''#!/usr/bin/env bash
# Отправка стенда: строка JSON на вызов (все доводы); файл send-fails рядом — отказ.
here="$(dirname "$0")"
python3 -c 'import json, sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))' "$@" >> "$here/sent"
[ -e "$here/send-fails" ] && exit 1
exit 0
'''
OVER_LIMIT = "--несмотря-на-лимит"


class MsgStand(Stand):
    """Стенд со своей дверью отправки SPEED_TG_SEND: что ушло владельцу — строки файла sent."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.send = self.root / "send" / "tg_send.sh"
        self.send.parent.mkdir()
        self.send.write_text(SEND_STUB, encoding="utf-8")

    def env(self) -> dict:
        return {**super().env(), "SPEED_TG_SEND": str(self.send)}

    def calls(self) -> list:
        path = self.send.parent / "sent"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def failing(self, on: bool) -> None:
        flag = self.send.parent / "send-fails"
        flag.touch() if on else flag.unlink(missing_ok=True)

    def said(self) -> list:
        return [row["data"].get("сказано") for row in self.events("mode.change")]


def eff_pass(s: Stand, at: float, plans: int | None = None) -> int | None:
    if plans is not None:
        s.set_cards(cards("p", plans))
    return s.measure(at).get("эффективный")


@case
def msg_window():
    """10.3.10: смена — сразу; смены в окне 30 мин — подавлены; итог одним сообщением после окна."""
    with MsgStand("ultra", cards("p", 12)) as s:
        t1 = BASE + 2 * MIN
        effs = [eff_pass(s, BASE), eff_pass(s, t1, 3)]
        check("первая смена 10→3 — сообщение сразу", len(s.calls()) == 1, s.calls())
        effs += [eff_pass(s, t1 + 2 * MIN, 5), eff_pass(s, t1 + 4 * MIN, 2), eff_pass(s, t1 + 29 * MIN)]
        check("условие создано: эффективный 10→3→5→2→2", effs == [10, 3, 5, 2, 2], effs)
        check("смены 3→5→2 внутри окна и 29-я минута — подавлены, по-прежнему одно",
              len(s.calls()) == 1, s.calls())
        check("mode.change: «да», затем дважды «подавлено: окно»",
              s.said() == ["да", "подавлено: окно", "подавлено: окно"], s.said())
        eff_pass(s, t1 + 30 * MIN)
        sent = s.calls()
        check("30-я минута окна — итог вторым сообщением", len(sent) == 2, sent)
        last = sent[-1][-1] if len(sent) == 2 else ""
        check("итог — про нынешнее (по 2 задачи вместо 10) и число смен за окно",
              "работаю по 2 задачи вместо 10" in last and "менялся 2 раза" in last, last)
        check("первое — про 3 задачи и почему", sent and "работаю по 3 задачи вместо 10" in sent[0][-1]
              and "готово только 3" in sent[0][-1], sent[:1])
        check("не про лимит — флага --несмотря-на-лимит нет", all(OVER_LIMIT not in c for c in sent), sent)
        told = [r for r in s.events("owner.told")]
        check("owner.told — две строки ok", [r.get("outcome") for r in told] == ["ok", "ok"],
              [r.get("outcome") for r in told])
    with MsgStand("ultra", cards("p", 12)) as s:
        s.conf["SPEED_MSG_WINDOW_MIN"] = "5"
        s.write_conf()
        t1 = BASE + 2 * MIN
        effs = [eff_pass(s, BASE), eff_pass(s, t1, 3), eff_pass(s, t1 + 2 * MIN, 5)]
        check("SPEED_MSG_WINDOW_MIN 5: в окне подавлено", (effs, len(s.calls())) == ([10, 3, 5], 1),
              (effs, s.calls()))
        eff_pass(s, t1 + 5 * MIN)
        check("SPEED_MSG_WINDOW_MIN 5: на 5-й минуте итог", len(s.calls()) == 2, s.calls())


@case
def msg_return_silent():
    """10.3.10: смена и возврат к сказанному внутри окна — после окна ни одного сообщения."""
    with MsgStand("ultra", cards("p", 12)) as s:
        t1 = BASE + 2 * MIN
        effs = [eff_pass(s, BASE), eff_pass(s, t1, 3), eff_pass(s, t1 + 2 * MIN, 5),
                eff_pass(s, t1 + 4 * MIN, 3), eff_pass(s, t1 + 31 * MIN), eff_pass(s, t1 + 62 * MIN)]
        check("условие создано: 10→3→5→3, дальше 3", effs == [10, 3, 5, 3, 3, 3], effs)
        check("сказано одно (про 3); возврат к нему — молчание и после окна", len(s.calls()) == 1, s.calls())
        check("сказано: эфф 3, смен 0", ((s.state().get("сказано") or {}).get("эфф"),
                                         (s.state().get("сказано") or {}).get("смен")) == (3, 0),
              s.state().get("сказано"))


@case
def msg_quiet_blocker():
    """10.3.10: блокер с «говорить: нет» (пауза) — ни одного сообщения ни при входе, ни при снятии."""
    with MsgStand("ultra", cards("p", 12)) as s:
        effs = [eff_pass(s, BASE)]
        s.pause()
        effs += [eff_pass(s, BASE + 2 * MIN), eff_pass(s, BASE + 40 * MIN)]
        (s.panel / "пауза.json").unlink()
        effs += [eff_pass(s, BASE + 42 * MIN), eff_pass(s, BASE + 80 * MIN)]
        check("условие создано: 10→0 (пауза)→10", effs == [10, 0, 0, 10, 10], effs)
        check("ни одного сообщения", s.calls() == [], s.calls())
        check("mode.change: «нет» на входе и на снятии", s.said() == ["нет", "нет"], s.said())


def limit_pass_at(s: Stand, at: float, percent: float, reset: float) -> dict:
    s.limits(at, five=(percent, reset), week=(10, BASE + 10 * HOUR))
    return s.measure(at)


@case
def msg_limit_once():
    """Ч8: лимит — одно сообщение со временем обновления и флагом, дальше тишина до обновления."""
    with MsgStand("ultra", cards("p", 12)) as s:
        r5 = BASE + 4 * HOUR
        t1, t2, t3, t4 = BASE + 2 * MIN, BASE + 33 * MIN, BASE + 64 * MIN, BASE + 95 * MIN
        effs = [eff_pass(s, BASE)]
        st = limit_pass_at(s, t1, 98.5, r5)
        effs.append(st.get("эффективный"))
        sent = s.calls()
        check("стоп по лимиту: одно сообщение", len(sent) == 1, sent)
        first = sent[0] if sent else []
        check("оно с флагом --несмотря-на-лимит", OVER_LIMIT in first, first)
        check("в нём «стою» и время обновления", bool(first) and "стою" in first[-1]
              and "лимит обновится" in first[-1], first)
        st = limit_pass_at(s, t2, 90, r5)
        effs.append(st.get("эффективный"))
        check("условие создано: под лимитом эффективный сменился и держит его лимит",
              effs[-1] not in (None, 0) and (blockers(st) == {"лимит_расход"}), (effs, blockers(st)))
        st = limit_pass_at(s, t3, 98.5, r5)
        effs.append(st.get("эффективный"))
        check("условие создано: 10→0→k→0", effs[:2] == [10, 0] and effs[-1] == 0, effs)
        check("ГЛАВНОЕ: две смены под лимитом через окно — тишина, сообщение по-прежнему одно",
              len(s.calls()) == 1, s.calls())
        st = limit_pass_at(s, t4, 0, t4 + HOUR)
        effs.append(st.get("эффективный"))
        sent = s.calls()
        check("условие создано: лимит обновился — эффективный 10", effs[-1] == 10, effs)
        check("обновление — второе сообщение, с флагом", len(sent) == 2 and OVER_LIMIT in sent[-1], sent)
        check("в нём «лимит обновился»", len(sent) == 2 and "лимит обновился" in sent[-1][-1], sent[-1:])


@case
def msg_resend_after_failure():
    """Ч8: отправка отказала — несказанное не записано, следующий проход шлёт снова."""
    with MsgStand("ultra", cards("p", 12)) as s:
        eff_pass(s, BASE)
        s.failing(True)
        eff = eff_pass(s, BASE + 2 * MIN, 3)
        check("условие создано: 10→3 при отказе отправки", eff == 3 and len(s.calls()) == 1,
              (eff, s.calls()))
        check("сказано не тронуто (эфф 10)", (s.state().get("сказано") or {}).get("эфф") == 10,
              s.state().get("сказано"))
        check("mode.change: «ошибка отправки», owner.told: error",
              (s.said(), [r.get("outcome") for r in s.events("owner.told")])
              == (["ошибка отправки"], ["error"]), (s.said(), s.events("owner.told")))
        s.failing(False)
        eff_pass(s, BASE + 4 * MIN)
        sent = s.calls()
        check("следующий проход без смены — перепосылка того же", len(sent) == 2
              and sent[0][-1] == sent[1][-1], sent)
        check("теперь сказано: эфф 3", (s.state().get("сказано") or {}).get("эфф") == 3,
              s.state().get("сказано"))
        eff_pass(s, BASE + 6 * MIN)
        check("третий проход — больше не шлёт", len(s.calls()) == 2, s.calls())


# ═══════════════════════════ Ч3: журнал и регистрация ═════════════════════════

TIME_LINE = re.compile(r"^(\d\d):(\d\d) ")


def hhmm(t: float) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%H:%M")


def journal_row(s: Stand, event: str, at: float, data: dict, src: str = JUDGE_SRC,
                name: str = JOURNAL) -> None:
    s.append(name, {"v": 1, "ts": iso(at), "src": src, "event": event, "outcome": "ok",
                    "task": None, "task_from": "none", "session": None, "data": data})


def files_snapshot(*folders: Path) -> dict:
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns)
            for folder in folders for p in folder.rglob("*") if p.is_file()}


@case
def journal_feed():
    """8.3: --журнал — лента по времени из режима скорости и запусков, только чтение."""
    with Stand("fast", cards("p", 3)) as s:
        # (метка, время, проверка строки) — ожидаемая лента по порядку.
        want = [
            ("решение", BASE - 90 * MIN, ["мало задач: в карте готово только 4"]),
            ("запуск", BASE - 80 * MIN, ["запуск", "hr.x"]),
            ("смена", BASE - 70 * MIN, ["5→3"]),
            ("пинок", BASE - 60 * MIN, ["hr.y"]),
            ("взята", BASE - 50 * MIN, ["hr.z"]),
            ("конец", BASE - 40 * MIN, ["конец"]),
        ]
        journal_row(s, "mode.decision", BASE - 3 * HOUR,
                    {"выбранный": "fast", "эффективный": 5, "причина": "старое-решение-вне-окна"})
        journal_row(s, "mode.decision", want[0][1],
                    {"выбранный": "fast", "выбранный_задач": 5, "эффективный": 4, "было": 5,
                     "причина": "мало задач: в карте готово только 4"})
        with open(s.log / JOURNAL, "a", encoding="utf-8") as fh:
            fh.write("{битая строка\n")
        journal_row(s, "mode.change", want[2][1],
                    {"было": 5, "стало": 3, "причина": "мощность: 7,1 на ядро > 2,0 10 мин",
                     "сказано": "да"})
        journal_row(s, "speed.kick", want[3][1],
                    {"в_работе": ["p1"], "эффективный": 3, "предложено": ["hr.y"]},
                    src=WARDEN_SRC)
        journal_row(s, "task.taken", want[4][1], {"id": "hr.z", "откуда": "wip", "вызовы": []})
        # Окно ограничено и сверху: событие позже --на (часы разошлись) не показывается.
        journal_row(s, "mode.decision", BASE + 30 * MIN,
                    {"эффективный": 1, "причина": "событие-позже-на"})
        # Чужой журнал и прочие события вызовов в ленту не идут: строк ровно шесть.
        journal_row(s, "mode.decision", BASE - 30 * MIN,
                    {"эффективный": 1, "причина": "чужой-журнал"}, name="другой.jsonl")
        s.launch("C1", want[1][1], BASE, part="hr.x", meta=False)
        s.append("вызовы-агентов.jsonl", {
            "v": 1, "ts": iso(BASE - 45 * MIN), "src": "scripts/hooks/agent_budget_guard.py",
            "event": "agent.unknown-card", "outcome": "ok", "task": "-", "task_from": "arg",
            "session": "s1", "data": {"вызов": "C2"}})
        s.finish("C1", want[5][1])
        before = files_snapshot(s.log, s.panel)
        done = s.run("--журнал", "--за", "2ч", at=BASE)
        check("--журнал: код 0", done.returncode == 0,
              f"код {done.returncode}: {(done.stderr or done.stdout).strip()[-300:]}")
        lines = [ln for ln in done.stdout.splitlines() if TIME_LINE.match(ln)]
        check("строк со временем ровно 6 (окно, чужой журнал, прочие вызовы отсеяны)",
              len(lines) == len(want), lines)
        found = []
        for label, at, needles in want:
            hit = next((i for i, ln in enumerate(lines) if all(n in ln for n in needles)), None)
            check(f"строка «{label}» есть: {needles}", hit is not None, lines)
            if hit is not None:
                check(f"строка «{label}» со временем {hhmm(at)}", lines[hit].startswith(hhmm(at)),
                      lines[hit])
                found.append(hit)
        check("лента по возрастанию времени через оба журнала",
              len(found) == len(want) and found == sorted(found), found)
        check("событие старше окна не показано",
              not any("старое-решение-вне-окна" in ln for ln in done.stdout.splitlines()),
              done.stdout)
        check("событие позже --на не показано",
              not any("событие-позже-на" in ln for ln in done.stdout.splitlines()),
              done.stdout)
        check("чтение ничего не пишет (журналы и панель не тронуты)",
              files_snapshot(s.log, s.panel) == before, "файлы стенда изменились")


@case
def journal_empty_and_bad_window():
    """8.3: пустой журнал — код 0 и непустой ответ; негодное --за — код 1."""
    with Stand("medium", cards("p", 1)) as s:
        done = s.run("--журнал", at=BASE)
        check("пусто: код 0", done.returncode == 0,
              f"код {done.returncode}: {(done.stderr or done.stdout).strip()[-300:]}")
        out = done.stdout.strip()
        check("пусто: ответ непустой и без строк со временем",
              out and not any(TIME_LINE.match(ln) for ln in out.splitlines()), out)
        journal_row(s, "mode.decision", BASE - MIN, {"эффективный": 3, "причина": "ок"})
        bad = s.run("--журнал", "--за", "долго", at=BASE)
        # Значение в stderr: справка судьи тоже содержит «--за» (в «--замер») и кодом 1 отвечает
        # на любой незнакомый ключ — по ней одной случай был бы зелёным без чтения журнала.
        check("--за долго: код 1 и негодное значение названо в stderr",
              bad.returncode == 1 and "долго" in bad.stderr, (bad.returncode, bad.stderr[-300:]))


@case
def journal_odd_input():
    """8.3: data не словарь — лента живёт; незнакомый ключ после --журнал — код 1."""
    with Stand("medium", cards("p", 1)) as s:
        journal_row(s, "mode.decision", BASE - 20 * MIN, {"эффективный": 3, "причина": "ок"})
        # Ключ назван в stderr: молчаливое отбрасывание незнакомого ключа давало бы ленту с кодом 0.
        bad = s.run("--журнал", "--левый", at=BASE)
        check("--журнал --левый: код 1 и ключ назван в stderr",
              bad.returncode == 1 and "--левый" in bad.stderr, (bad.returncode, bad.stderr[-300:]))
        at = BASE - 10 * MIN
        journal_row(s, "mode.change", at, "строка-вместо-словаря")
        done = s.run("--журнал", "--за", "2ч", at=BASE)
        check("data строкой: код 0 и без трейсбека",
              done.returncode == 0 and "Traceback" not in done.stderr,
              (done.returncode, done.stderr[-300:]))
        lines = [ln for ln in done.stdout.splitlines() if TIME_LINE.match(ln)]
        check(f"data строкой: строка события осталась со временем {hhmm(at)}",
              any(ln.startswith(hhmm(at)) for ln in lines), done.stdout)


def gate_commands() -> list:
    """Команды строк `проверка "…"` ворот — разбором файла; комментарии не в счёт."""
    text = GATES_SH.read_text(encoding="utf-8")
    return [m.group(1) for m in (re.match(r'\s*проверка\s+"[^"]+"\s+(.*)', ln)
                                 for ln in text.splitlines()) if m]


def passports() -> dict:
    rows = (yaml.safe_load(MECHANISMS.read_text(encoding="utf-8")) or {}).get("механизмы") or []
    return {r.get("файл"): r for r in rows if isinstance(r, dict)}


def tree_has(rel: str) -> bool:
    # Реестр пишет пути от корня проекта; в пакете тот же файл лежит без приставки harness/.
    return any((HERE.parent / c).is_file() for c in (rel, rel.removeprefix("harness/")))


@case
def gates_lines():
    """8.5: ворота зовут пробу судьи и пробу сторожа; паспорт пробы судьи это знает."""
    commands = gate_commands()
    if not check("ворота разобраны: строк проверки > 0", commands, GATES_SH):
        return
    for probe, runner in (("scripts/test_speed_mode.py", "python3"),
                          ("scripts/test_warden_speed.sh", "bash")):
        hit = [c for c in commands if re.search(rf"(^|\s){runner}\s+{re.escape(probe)}(\s|$)", c)]
        check(f"в воротах: {runner} {probe}", hit, f"строк проверки {len(commands)}, этой нет")
    card = passports().get("scripts/test_speed_mode.py") or {}
    check("паспорт test_speed_mode.py не говорит «в ворота не включена»",
          card and "не включена" not in str(card.get("когда", "")), card.get("когда"))


@case
def probe_passports():
    """8.5: паспорта проб сторожа и диспетчера по образцу паспорта пробы судьи."""
    cards_by_file = passports()
    model = cards_by_file.get("scripts/test_speed_mode.py")
    if not check("образец разобран: паспорт test_speed_mode.py", model, MECHANISMS):
        return
    fields = set(model)
    for probe, proof in (("scripts/test_warden_speed.sh", "harness/demons/session-warden.sh"),
                         ("scripts/test_dispatcher_speed.sh", "scripts/tg-dispatcher.sh")):
        card = cards_by_file.get(probe)
        if not check(f"паспорт {probe} есть", card, "нет записи в реестре механизмов"):
            continue
        check(f"{probe}: вид гейт", card.get("вид") == "гейт", card.get("вид"))
        check(f"{probe}: поля как у образца", fields <= set(card), sorted(fields - set(card)))
        check(f"{probe}: проверяется зовёт сам файл", probe in str(card.get("проверяется", "")),
              card.get("проверяется"))
        evidence = str(card.get("доказательство", ""))
        check(f"{probe}: доказательство — {proof}, файл живой",
              evidence == proof and tree_has(proof), evidence)
        # «в воротах» честно, только когда ворота и правда зовут файл — разбором gates.sh.
        called = any(re.search(rf"(^|\s)bash\s+{re.escape(probe)}(\s|$)", c)
                     for c in gate_commands())
        said = "в воротах" in str(card.get("когда", ""))
        check(f"{probe}: «когда» сходится с воротами (зовут: {called})", said == called,
              card.get("когда"))


@case
def signals_launch_plan():
    """8.5 / Ч6: причина launch-plan.py в «знают_сами» называет судью скорости."""
    facts_ = (yaml.safe_load(SIGNALS.read_text(encoding="utf-8")) or {}).get("признаки") or []
    fact = next((f for f in facts_ if f.get("факт") == "какая задача идёт сейчас"), None)
    if not check("факт «какая задача идёт сейчас» разобран", fact, SIGNALS):
        return
    row = next((r for r in fact.get("знают_сами") or [] if r.get("файл") == "scripts/launch-plan.py"),
               None)
    if not check("launch-plan.py в «знают_сами»", row, fact.get("знают_сами")):
        return
    check("причина называет судью (speed_mode)", "speed_mode" in str(row.get("причина", "")),
          row.get("причина"))


# ───────────────────────── расход только текущего окна ─────────────────────────

# Окно, которое в случае не судится, держим ровным: 5 часов на 10 %, неделя на 20 %.
WINDOWS = ("пять_часов", "неделя")
QUIET_PCT = {"пять_часов": 10, "неделя": 20}
QUIET_RESET = {"пять_часов": BASE + 3 * HOUR, "неделя": BASE + 100 * HOUR}
WINDOW_SPAN_MIN = {"пять_часов": 60, "неделя": 360}
OLD_WEEK_RESET = 10 * HOUR
WEEK_MOVE = 168 * HOUR
FEW_DATA_SPEED = {"пять_часов": 8, "неделя": 0.5}


def ramp(now, window, back_from, back_to, tasks, p_from, p_to, reset, step=10):
    """Строки seed_points: процент окна window линейно от p_from до p_to, второе окно ровное."""
    rows = []
    for back in range(back_from, back_to - 1, -step):
        cells = {name: (QUIET_PCT[name], QUIET_RESET[name]) for name in WINDOWS}
        cells[window] = (p_from + (p_to - p_from) * (back_from - back) / max(back_from - back_to, 1),
                         reset)
        rows.append((now - back * MIN, tasks, *cells["пять_часов"], *cells["неделя"]))
    return rows


def old_week(now, reset):
    """Неделя до смены: 2 задачи 280 мин, наклон 1,35 %/ч на задачу, как было вживую."""
    return ramp(now, "неделя", 300, 20, 2, 81.4, 94, reset)


def seed_rows_any_reset(stand, rows, window, drop_key=False):
    """Как seed_points, но у окна window сброса нет: null либо, при drop_key, нет и ключа."""
    for t, tasks, p5, r5, pw, rw in rows:
        cells = {name: {"процент": percent, "сброс": None if name == window else iso(reset)}
                 for name, percent, reset in (("пять_часов", p5, r5), ("неделя", pw, rw))}
        if drop_key:
            del cells[window]["сброс"]
        stand.append(JOURNAL, {
            "v": 1, "ts": iso(t), "src": JUDGE_SRC, "event": "mode.decision", "outcome": "ok",
            "task": None, "task_from": "none", "session": None,
            "data": {"лимит": {"ts": iso(t), "задач": tasks, **cells}}})


def read_window(stand, now, window, percent, reset):
    """Файл лимитов: окно window как задано (None — окна в файле нет), второе ровное."""
    cells = {name: (QUIET_PCT[name], QUIET_RESET[name]) for name in WINDOWS}
    cells[window] = None if percent is None else (percent, reset)
    stand.limits(now, five=cells["пять_часов"], week=cells["неделя"])


def work_stand(in_work=2):
    """Режим «средний», выбрано 3, как у владельца; готовых хватает, мало_задач не вмешивается."""
    return Stand("medium", cards("w", in_work, "wip") + cards("p", 10))


def measured_ceiling(percent, speed, hours_left, tasks=3):
    return max(1, math.floor(min(tasks, (95 - percent) / (speed * hours_left))))


def few_data_ceiling(percent, window, hours_left, in_work, tasks=3):
    """Спека 4.5 при скорости по догадке: счёт, поднятый строкой «начатое не урезаем»."""
    return max(measured_ceiling(percent, FEW_DATA_SPEED[window], hours_left, tasks),
               min(tasks, max(in_work, 1)))


def close_to(got, want) -> bool:
    # Судья пишет скорость с точностью до 4 знаков.
    return isinstance(got, (int, float)) and abs(got - want) <= 1e-4


def check_few_data(label, st, window, percent, hours_left, in_work=2):
    """Окно началось заново: расход только по догадке, потолок по ней же."""
    got = window_field(st, window, "скорость_на_задачу")
    check(f"{label}: скорость {window} — консервативная {FEW_DATA_SPEED[window]}",
          got == FEW_DATA_SPEED[window], got)
    want = few_data_ceiling(percent, window, hours_left, in_work)
    got = window_field(st, window, "потолок")
    check(f"{label}: потолок {window} {want}", got == want, got)
    check(f"{label}: точность «оценка»", (st.get("лимит") or {}).get("точность") == "оценка",
          (st.get("лимит") or {}).get("точность"))


def check_measured(label, st, window, rows, p_now, now, hours_left):
    """Скорость равна независимому счёту по rows, потолок — по этой скорости, не по догадке."""
    want, task_hours = expected_speed(rows, now, p_now, WINDOW_SPAN_MIN[window], window)
    check(f"{label}: стенд — задача-часов {task_hours:.2f} хватает на замер", task_hours >= 3,
          task_hours)
    got = window_field(st, window, "скорость_на_задачу")
    check(f"{label}: скорость {window} сходится с независимым счётом ({want:.4f})",
          close_to(got, want), got)
    ceiling = measured_ceiling(p_now, want, hours_left)
    got = window_field(st, window, "потолок")
    check(f"{label}: потолок {window} {ceiling}", got == ceiling, got)


@case
def limit_cut_account_change():
    """A: смена аккаунта, неделя 94 → 1: старый наклон 1,35 не входит ни со сдвигом сброса, ни без."""
    now = BASE
    old_reset = now + OLD_WEEK_RESET
    for label, new_reset in (("сброс на 7 дней позже", old_reset + WEEK_MOVE),
                             ("сброс тот же", old_reset)):
        with work_stand() as s:
            old = old_week(now, old_reset)
            old_speed, task_hours = expected_speed(old, old[-1][0], 94, 360, "неделя")
            check(f"{label}: стенд — живой наклон 1,35 за ≥ 5 задача-часов",
                  abs(old_speed - 1.35) < 0.01 and task_hours >= 5, (old_speed, task_hours))
            s.seed_points(old + ramp(now, "неделя", 10, 2, 2, 1, 1, new_reset, step=4))
            read_window(s, now, "неделя", 1, new_reset)
            st = s.measure(now)
            check_few_data(label, st, "неделя", 1, (new_reset - now) / HOUR)
            got = window_field(st, "неделя", "скорость_на_задачу")
            check(f"{label}: скорость не выше старой", isinstance(got, (int, float))
                  and got < old_speed, got)


@case
def limit_cut_current_read():
    """B: падение только между последней точкой и чтением файла — первый проход после смены."""
    now = BASE
    before = now - 2 * MIN
    old_reset = now + OLD_WEEK_RESET
    for label, new_reset in (("сброс на 7 дней позже", old_reset + WEEK_MOVE),
                             ("сброс тот же", old_reset)):
        with work_stand() as s:
            s.seed_points(old_week(now, old_reset))
            read_window(s, before, "неделя", 94, old_reset)
            s.measure(before)
            read_window(s, now, "неделя", 1, new_reset)
            check_few_data(label, s.measure(now), "неделя", 1, (new_reset - now) / HOUR)


@case
def limit_cut_reset_moved():
    """C: сброс сдвинут без падения процента: в журнале и только в чтении, вперёд и назад."""
    now = BASE
    old_reset = now + 100 * HOUR
    for label, new_reset in (("сброс на час позже", old_reset + HOUR),
                             ("сброс на сутки раньше", old_reset - 24 * HOUR)):
        for place, fresh_rows in (("в журнале", 1), ("только в чтении", 0)):
            with work_stand() as s:
                rows = ramp(now, "неделя", 300, 20, 2, 60, 70, old_reset)
                if fresh_rows:
                    rows += ramp(now, "неделя", 10, 2, 2, 70, 70, new_reset, step=4)
                s.seed_points(rows)
                read_window(s, now, "неделя", 70, new_reset)
                check_few_data(f"{label}, {place}", s.measure(now), "неделя", 70,
                               (new_reset - now) / HOUR)


@case
def limit_cut_control_growth():
    """D: контроль: рост внутри окна при том же сбросе режет как раньше."""
    with work_stand() as s:
        now = BASE
        r5, rw = now + 3 * HOUR, now + 48 * HOUR
        rows = linear_points(now, 2, 20, 2.0, r5, 70, 0.6, rw)
        s.seed_points(rows)
        s.limits(now, five=(20, r5), week=(70, rw))
        st = s.measure(now)
        check_measured("рост", st, "неделя", rows, 70, now, 48)
        check("эффективный 1 меньше выбранных 3", st.get("эффективный") == 1,
              st.get("эффективный"))
        check("точность «точно»", (st.get("лимит") or {}).get("точность") == "точно",
              (st.get("лимит") or {}).get("точность"))


@case
def limit_cut_control_reset_noise():
    """E: контроль: дребезг сброса на 20 с и точки без сброса окно не делят."""
    now = BASE
    r5, rw = now + 3 * HOUR, now + 48 * HOUR
    rows = linear_points(now, 2, 20, 2.0, r5, 70, 0.6, rw)
    with work_stand() as s:
        s.seed_points([row[:5] + (rw + 20 * (index % 2),) for index, row in enumerate(rows)])
        s.limits(now, five=(20, r5), week=(70, rw + 20))
        check_measured("дребезг 20 с", s.measure(now), "неделя", rows, 70, now, 48 + 20 / HOUR)
    for label, drop_key in (("сброс null", False), ("ключа сброса нет", True)):
        with work_stand() as s:
            for index, row in enumerate(rows):
                if index % 3:
                    s.seed_points([row])
                else:
                    seed_rows_any_reset(s, [row], "неделя", drop_key)
            s.limits(now, five=(20, r5), week=(70, rw))
            check_measured(f"{label} между точками", s.measure(now), "неделя", rows, 70, now, 48)
    with work_stand() as s:
        seed_rows_any_reset(s, rows, "неделя")
        s.limits(now, five=(20, r5), week=(70, rw))
        check_measured("все точки без сброса, в чтении сброс есть", s.measure(now), "неделя",
                       rows, 70, now, 48)


@case
def limit_cut_counts_new_points():
    """F: считаются только точки после последнего разрыва, интервал через разрыв не входит."""
    now = BASE
    first, second, third = now + OLD_WEEK_RESET, now + 34 * HOUR, now + 58 * HOUR
    new = ramp(now, "неделя", 190, 10, 2, 1, 10, third)
    with work_stand() as s:
        s.seed_points(ramp(now, "неделя", 350, 200, 2, 60, 94, first) + new)
        read_window(s, now, "неделя", 10.5, third)
        check_measured("падение, потом рост", s.measure(now), "неделя", new, 10.5, now, 58)
    jumped = ramp(now, "неделя", 190, 10, 2, 75, 84, third)
    with work_stand() as s:
        s.seed_points(ramp(now, "неделя", 350, 200, 2, 60, 70, first) + jumped)
        read_window(s, now, "неделя", 84.5, third)
        check_measured("смена сброса с ростом через разрыв", s.measure(now), "неделя", jumped,
                       84.5, now, 58)
    last = ramp(now, "неделя", 120, 10, 2, 1, 7, second)
    with work_stand() as s:
        s.seed_points(ramp(now, "неделя", 350, 260, 2, 60, 94, first)
                      + ramp(now, "неделя", 250, 130, 2, 2, 50, second) + last)
        read_window(s, now, "неделя", 7.3, second)
        check_measured("два разрыва, берётся последний", s.measure(now), "неделя", last, 7.3,
                       now, 34)


@case
def limit_cut_equal_ts():
    """H: две точки с равным ts, задач 2 затем 1, процент 40 затем 50: порядок журнала, не разрыв."""
    now = BASE
    reset = now + 100 * HOUR
    rows = ramp(now, "неделя", 300, 20, 2, 30, 40, reset)
    rows += ramp(now, "неделя", 10, 10, 2, 40, 40, reset) + ramp(now, "неделя", 10, 10, 1, 50, 50, reset)
    with work_stand() as s:
        s.seed_points(rows)
        read_window(s, now, "неделя", 50.5, reset)
        check_measured("равный ts", s.measure(now), "неделя", rows, 50.5, now, 100)


@case
def limit_cut_regular_reset():
    """G: обычный сброс окна: тот же разрыв, в том числе чтение и точка без окна в файле."""
    now = BASE
    spans = {"пять_часов": (58, 18, 40, 80, 5 * HOUR), "неделя": (300, 20, 81.4, 94, WEEK_MOVE)}
    for window, (back_from, back_to, p_from, p_to, period) in spans.items():
        old_reset = now - MIN
        new_reset = old_reset + period
        old = ramp(now, window, back_from, back_to, 2, p_from, p_to, old_reset)
        hours_left = (new_reset - now) / HOUR
        with work_stand() as s:
            s.seed_points(old + ramp(now, window, 2, 2, 2, 1, 1, new_reset))
            read_window(s, now, window, 1, new_reset)
            check_few_data(f"{window}: новое окно в журнале", s.measure(now), window, 1, hours_left)
        with work_stand() as s:
            s.seed_points(old)
            read_window(s, now, window, 1, new_reset)
            check_few_data(f"{window}: новое окно в чтении", s.measure(now), window, 1, hours_left)
        with work_stand() as s:
            s.seed_points(old)
            read_window(s, now, window, None, None)
            check_few_data(f"{window}: окна нет в файле", s.measure(now), window, 0,
                           period / HOUR)
        with work_stand() as s:
            s.seed_points(old)
            seed_rows_any_reset(s, ramp(now, window, 8, 8, 2, 0, 0, None), window)
            read_window(s, now, window, 0, new_reset)
            check_few_data(f"{window}: 0 % без сброса, затем новый сброс", s.measure(now), window,
                           0, hours_left)


# Живые числа: неделя 2 %, до сброса 157 ч, выбрано 3; 93 / (0,5 · 157) = 1,18.
FEW_DATA_TABLE = {0: (1, 1), 1: (1, 1), 2: (2, 2), 3: (3, 3)}


def check_few_data_table(label, seed):
    now = BASE
    new_reset = now + 157 * HOUR
    for in_work, (ceiling, effective_now) in FEW_DATA_TABLE.items():
        with work_stand(in_work) as s:
            seed(s, now, new_reset)
            read_window(s, now, "неделя", 2, new_reset)
            st = s.measure(now)
            got = (window_field(st, "неделя", "потолок"), st.get("эффективный"),
                   window_field(st, "неделя", "скорость_на_задачу"))
            print(f"  {label}: в работе {in_work}: потолок недели, эффективный, скорость = {got}",
                  flush=True)
            check(f"{label}, в работе {in_work}: потолок {ceiling}, эффективный {effective_now}, "
                  f"скорость 0,5", got == (ceiling, effective_now, 0.5), got)


@case
def limit_cut_few_data_table():
    """H: контроль: живые числа без старых точек: потолок 1, поднимает только «начатое не урезаем»."""
    check_few_data_table("без точек", lambda s, now, new_reset: None)


@case
def limit_cut_few_data_after_change():
    """H: та же таблица с журналом старого окна до смены: старый наклон её не меняет."""
    def seed(s, now, new_reset):
        s.seed_points(old_week(now, now + 103 * HOUR)
                      + ramp(now, "неделя", 10, 2, 2, 2, 2, new_reset, step=4))
    check_few_data_table("после смены", seed)


def fresh_window_stand():
    """Выбрано 3, в работе 1 (начатое не поднимает потолок), 12 %/ч на задачу, час до сброса.

    Потолок floor(20 / 12) = 1; после разрыва догадка 8 %/ч позволяет больше, и только
    признак нового окна разрешает подъём раньше интервала."""
    s = work_stand(1)
    r5, rw = BASE + HOUR, BASE + 100 * HOUR
    s.seed_points(linear_points(BASE, 1, 75, 12.0, r5, 0, 0.0, rw))
    s.limits(BASE, five=(75, r5), week=(0, rw))
    return s, r5, rw


@case
def limit_cut_shift_boundary():
    """I: граница допуска: сдвиг сброса ровно 60 с окно не делит, на секунду больше — делит."""
    now = BASE
    reset = now + 100 * HOUR
    rows = ramp(now, "неделя", 300, 20, 2, 60, 70, reset)
    with work_stand() as s:
        s.seed_points(rows)
        read_window(s, now, "неделя", 70.5, reset + 60)
        check_measured("сдвиг ровно 60 с", s.measure(now), "неделя", rows, 70.5, now,
                       (reset + 60 - now) / HOUR)
    with work_stand() as s:
        s.seed_points(rows)
        read_window(s, now, "неделя", 70.5, reset + 61)
        check_few_data("сдвиг 61 с", s.measure(now), "неделя", 70.5, (reset + 61 - now) / HOUR)


@case
def limit_cut_shift_across_null():
    """J: точка без сброса не стирает память о нём: сдвиг через null остаётся разрывом."""
    now = BASE
    old_reset, new_reset = now + 100 * HOUR, now + 101 * HOUR
    for label, fresh_rows in (("новый сброс в журнале", 1), ("новый сброс только в чтении", 0)):
        with work_stand() as s:
            s.seed_points(ramp(now, "неделя", 300, 20, 2, 60, 70, old_reset))
            seed_rows_any_reset(s, ramp(now, "неделя", 16, 16, 2, 70, 70, None), "неделя")
            if fresh_rows:
                s.seed_points(ramp(now, "неделя", 10, 2, 2, 70, 70, new_reset, step=4))
            read_window(s, now, "неделя", 70, new_reset)
            check_few_data(label, s.measure(now), "неделя", 70, (new_reset - now) / HOUR)


@case
def limit_cut_first_interval():
    """K: разрыв между самой первой точкой ряда и следующей: первая точка тоже задаёт сброс."""
    now = BASE
    old_reset, new_reset = now + 100 * HOUR, now + 101 * HOUR
    old = ramp(now, "неделя", 20, 20, 2, 60, 60, old_reset)
    with work_stand() as s:
        s.seed_points(old)
        read_window(s, now, "неделя", 80, new_reset)
        check_few_data("одна точка и чтение", s.measure(now), "неделя", 80,
                       (new_reset - now) / HOUR)
    with work_stand() as s:
        s.seed_points(old + ramp(now, "неделя", 10, 2, 2, 80, 80, new_reset, step=4))
        read_window(s, now, "неделя", 80, new_reset)
        check_few_data("разрыв после первой точки", s.measure(now), "неделя", 80,
                       (new_reset - now) / HOUR)


@case
def limit_fresh_window():
    """4.5: новое окно поднимает потолок в тот же проход, падение меньше порога ждёт интервала."""
    at = BASE + 2 * MIN
    # T = 58 мин: при 65 % floor(30 / 7,7) = 3; при 66 % floor(29 / 7,7) = 3, запас 26 / 7,7 тоже.
    # T = 1 ч при сдвиге: floor(20 / 8) = 2. Без подъёма потолок остался бы прежней единицей.
    for label, percent, shift, want in (("падение 10", 65, 0, 3),
                                        ("падение 9 — не новое окно", 66, 0, 1),
                                        ("сброс вперёд на 2 мин", 75, 2 * MIN, 2)):
        s, r5, rw = fresh_window_stand()
        with s:
            st = s.measure(BASE)
            check(f"{label}: стенд — потолок 1 по замеру",
                  window_field(st, "пять_часов", "потолок") == 1
                  and window_field(st, "пять_часов", "скорость_на_задачу") > 8,
                  (st.get("лимит") or {}).get("пять_часов"))
            s.limits(at, five=(percent, r5 + shift), week=(0, rw))
            st = s.measure(at)
            check(f"{label}: скорость по догадке 8",
                  window_field(st, "пять_часов", "скорость_на_задачу") == 8,
                  window_field(st, "пять_часов", "скорость_на_задачу"))
            check(f"{label}: потолок {want}", window_field(st, "пять_часов", "потолок") == want,
                  window_field(st, "пять_часов", "потолок"))
            if want == 1:
                later = BASE + 20 * MIN
                s.limits(later, five=(percent, r5), week=(0, rw))
                # T = 40 мин, запас: floor(26 / 5,3) = 4, зажато выбранными 3.
                st = s.measure(later)
                check(f"{label}: на 20-й минуте интервал вышел, потолок 3",
                      window_field(st, "пять_часов", "потолок") == 3,
                      window_field(st, "пять_часов", "потолок"))


def main(argv: list[str]) -> int:
    only = argv[argv.index("--случай") + 1] if "--случай" in argv else None
    chosen = [fn for fn in CASES if only in (None, fn.__name__)]
    if not chosen:
        print(f"нет случая {only}; есть: {', '.join(fn.__name__ for fn in CASES)}")
        return 2
    print(f"судья: {JUDGE} ({'есть' if JUDGE.is_file() else 'НЕТ'})")
    for fn in chosen:
        print(f"[{fn.__name__}] {(fn.__doc__ or '').strip().splitlines()[0]}", flush=True)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — случай краснеет с причиной, проба идёт дальше
            check(f"{fn.__name__}: дошёл до конца", False, f"{type(exc).__name__}: {exc}")
    bad = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"СКОРОСТЬ: проб {len(chosen)}, проверок {len(RESULTS)}, провалов {bad}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
