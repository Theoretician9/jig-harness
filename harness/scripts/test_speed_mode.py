#!/usr/bin/env python3
"""Пробы судьи режима скорости (`harness/demons/speed_mode.py`, Ч2 hr.speed-modes).

Контракт — `.tasks/hr.speed-modes/ч2/контракт.md`; критерии — spec.md 10.3 п. 1–6, 8а, 16.
Судья решает, сколько задач карты вести одновременно: счёт задач в работе по журналу
запусков, «ждёт владельца», лестница мощности с гистерезисом, авария, пауза, лимит по факту
429, выкат без захвата лока, ответ сторожу; расход лимита подписки скорость не снижает. Ошибка любого из них — либо пинок
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
# Задача в работе — живой помощник, не статус карты (владелец 19.11). Стенд ведёт wip-карточку
# живым помощником, чей транскрипт «пишется» до этого мига: случай двигает часы на часы, не на месяц.
LIVE_UNTIL = BASE + 30 * 24 * HOUR
# Пороги мощности по доле CPU за интервал замера: ключи harness.conf
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

    def __init__(self, mode: str = "medium", cards=(), data_patch=None, live_wip: bool = True):
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
        self.live_wip = live_wip
        self.live: set = set()   # wip-карточки, которые ведёт живой помощник стенда
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
        if self.live_wip:
            self.sync_live()

    def sync_live(self) -> None:
        """Каждую wip-карточку ведёт живой помощник; ушла из wip — его запуск кончился."""
        wip = {c["id"] for c in self.cards if c.get("status") == "wip"}
        for card in sorted(wip - self.live):
            self.launch(f"live-{card}", LIVE_UNTIL, LIVE_UNTIL, part=card, quiet_s=0)
        for card in sorted(self.live - wip):
            self.finish(f"live-{card}", BASE)
        self.live = wip

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
    check("по умолчанию — medium (решение владельца 11.3)", data.get("по_умолчанию") == "medium",
          data.get("по_умолчанию"))
    acting = sorted(k for k, v in found.items() if (v or {}).get("действует", "да") != "нет")
    check("режут только пауза, лимит (429), выкат, ротация (правило владельца 11.11)",
          acting == sorted(["пауза", "лимит", "выкат", "ротация"]), acting)


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


# ── мощность по доле CPU, а не по loadavg ─────────
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


def linear_points(now, n, p5_now, slope5, r5, pw_now, sloped, rw, span_min=350):
    """Точки каждые 10 мин до now−10: процент растёт линейно, задач n."""
    pts = []
    for back in range(span_min, 0, -10):
        t = now - back * MIN
        pts.append((t, n, p5_now - slope5 * back / 60, r5, pw_now - sloped * back / 60, rw))
    return pts


# Ключи снижения по расходу лимита подписки, как они стояли в данных до 22.11: вернуть их
# в данные не должно вернуть снижение — скорость снижают только пауза и 429 (владелец 21.11).
LEGACY_LIMIT_PATCH = {
    "блокеры.лимит_расход": {"потолок": "вычисляемый", "держится_мин": 0, "отпускает_мин": 0,
                             "говорить": "да"},
    "лимит_подписки": {
        "адаптер": "claude", "файл_claude": "", "устарел_мин": 15,
        "порог_прогноза_pct": {"пять_часов": 95, "неделя": 95}, "стоп_pct": 98,
        "окно_расхода_мин": {"пять_часов": 60, "неделя": 360},
        "мин_задача_часов": {"пять_часов": 0.5, "неделя": 3},
        "скорость_без_данных_pct_ч_на_задачу": {"пять_часов": 8, "неделя": 0.5},
        "подъём_не_чаще_мин": 20, "запас_подъёма_pct": 3, "сброс_падение_pct": 10},
}


@case
def limit_spend_never_cuts():
    """Владелец 21.11: расход лимита подписки скорость не снижает — ни у порога, ни за стопом."""
    for label, patch in (("данные без ключей", {}), ("ключи вернули в данные", LEGACY_LIMIT_PATCH)):
        with Stand("ultra", cards("w", 2, "wip") + cards("p", 10), data_patch=patch) as s:
            if patch:
                s.data["лимит_подписки"]["файл_claude"] = str(s.limits_file)
                s.write_all()
            now = BASE
            r5, rw = now + 3 * HOUR, now + 48 * HOUR
            s.seed_points(linear_points(now, 2, 99, 20.0, r5, 97, 5.0, rw))
            s.limits(now, five=(99, r5), week=(97, rw))
            st = s.measure(now)
            check(f"{label}: эффективный 10 при 99 % лимита", st.get("эффективный") == 10,
                  st.get("эффективный"))
            named = blockers(st) | set(st.get("не_действуют") or {})
            check(f"{label}: блокера по расходу нет", "лимит_расход" not in named, named)


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
    """10.3.3: журнала запусков нет — в работе никого: статус wip без помощника не работа."""
    with Stand("ultra", [{"id": "hr.w", "status": "wip"}] + cards("p", 2), live_wip=False) as s:
        got = s.call("in_work", BASE)
        check("в работе пусто", got == [], got)


@case
def count_live_not_wip():
    """Владелец 19.11: в работе — живые помощники; 15 wip при 3 живых — в работе 3, «лишнего» нет."""
    with Stand("ultra", cards("w", 15, "wip") + cards("p", 2), live_wip=False) as s:
        for i in (1, 2, 3):
            s.launch(f"L{i}", BASE - 5 * MIN, BASE, part=f"w{i}")
        got = s.call("in_work", BASE)
        check("в работе 3 живых из 15 wip", got == ["w1", "w2", "w3"], got)
        s.measure(BASE)
        word = s.warden(0, BASE).split("\t")[0]
        check("сторож: при ultra и 3 живых — не «лишнее»", word != "лишнее", word)


@case
def count_waiting_with_launch():
    """3.1: ждущая владельца с живым запуском — в работе; wip, ждущая без запуска, — нет."""
    with Stand("ultra", [{"id": "hr.s", "status": "plan"}, {"id": "hr.t", "status": "wip"}],
               live_wip=False) as s:
        s.spec_sent("hr.s", BASE - HOUR)
        s.spec_sent("hr.t", BASE - HOUR)
        s.launch("S1", BASE - 5 * MIN, BASE, part="hr.s")
        got = s.call("in_work", BASE)
        check("в работе только hr.s", got == ["hr.s"], got)


@case
def warden_offered():
    """--предложено: сторож предлагает сперва то, чего ещё не предлагал; всё предложено — снова по порядку."""
    with Stand("medium", cards("w", 2, "wip") + cards("p", 5)) as s:
        st = s.measure(BASE)
        take = st.get("к_взятию") or []
        check("предложено: стенд — к взятию 5, добор 1", len(take) == 5 and st.get("добор") == 1,
              (take, st.get("добор")))

        def offered(ids: list) -> list:
            done = s.run("--сторожу", "--последний-пинок", "0", "--предложено", " ".join(ids), at=BASE)
            parts = done.stdout.rstrip("\n").split("\t")
            return parts[2].split() if done.returncode == 0 and parts[0] == "недобор" else [done.stdout]

        check("предложено: без памяти — голова очереди", offered([]) == take[:1], (offered([]), take))
        check("предложено: голова уже предлагалась — следующая", offered(take[:1]) == take[1:2],
              (offered(take[:1]), take))
        check("предложено: три первых предлагались — четвёртая", offered(take[:3]) == take[3:4],
              (offered(take[:3]), take))
        check("предложено: предлагалось всё — снова голова", offered(take) == take[:1],
              (offered(take), take))


# ═══════════════════════════ ждёт владельца и мало задач ══════════════════════

def waiting_map():
    return [{"id": "W", "status": "wip"}, {"id": "A", "status": "plan"},
            {"id": "B", "status": "plan"}, {"id": "C", "status": "plan"},
            {"id": "D", "status": "plan", "зависит_от": ["W"]}]


@case
def waiting_owner():
    """10.3.4: ждущие и зависимая не готовы; эффективный 2; журнал одобрений не тронут."""
    with Stand("ultra", waiting_map(), live_wip=False) as s:
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
    with Stand("ultra", waiting_map(), data_patch={"спек_ждут_владельца_максимум": 2},
               live_wip=False) as s:
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
def future_stamps():
    """4.1.6: состояние сверх допуска 5 с в будущем — не свежее."""
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


# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════ сообщения владельцу (Ч8, 10.3.10) ═══════════════════════

SEND_STUB = r'''#!/usr/bin/env bash
# Отправка стенда: строка JSON на вызов (все доводы); файл send-fails рядом — отказ.
here="$(dirname "$0")"
python3 -c 'import json, sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))' "$@" >> "$here/sent"
[ -e "$here/send-fails" ] && exit 1
exit 0
'''


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


# ── выключатель блокера данными: «действует: нет» ─────────
# Владелец 19.11: «Убери нахуй эту хуйню заебал он тормощить» про «работаю по 1 задаче вместо 10 —
# сервер загружен». Правило 11.11 «включай режим средний и не снижай»: режут только пауза и 429.
# Выключатель один для любого блокера: признак считается, потолок не режет, владельцу не пишет.
MUTED = ("мощность", "авария", "мало_задач", "файлы")


def muted_patch(names=MUTED) -> dict:
    return {f"блокеры.{name}.действует": "нет" for name in names}


@case
def muted_blockers_keep_mode():
    """19.11: занято 96 %, нагрузка 4,5/ядро, мало задач, заявки на файлы — ultra держит 10."""
    with Stand("ultra", cards("w", 2, "wip") + cards("p", 1), data_patch=muted_patch()) as s:
        s.busy_pct = 96
        s.set_load(4.5)
        warn_files(s, BASE)
        effs = []
        for minute in (0, 2, 4, 12, 14, 24, 40):
            st = s.measure(BASE + minute * MIN)
            effs.append(st.get("эффективный"))
        check("условие создано: занято ≈ 96", near(gauge(st, "занято_pct"), 96), gauge(st, "занято_pct"))
        check("условие создано: признаки посчитаны (авария и мощность видны)",
              {"мощность", "авария"} <= set(st.get("не_действуют") or {}),
              sorted(st.get("не_действуют") or {}))
        check("ГЛАВНОЕ: ultra при 96 % — эффективный 10 на всех проходах", effs == [10] * 7, effs)
        check("в действующих блокерах ни одного выключенного", not blockers(st) & set(MUTED),
              blockers(st))
        check("причина — «блокеров нет»", st.get("причина") == "блокеров нет", st.get("причина"))
        check("ceiling_alive — потолок ultra 12, не аварийные 2",
              s.call("ceiling_alive", BASE + 40 * MIN) == 12, s.call("ceiling_alive", BASE + 40 * MIN))
        check("добор — весь недобор по готовым (1), не ноль из-за занятости", st.get("добор") == 1,
              (st.get("добор"), st.get("добор_почему")))
    with Stand("ultra", cards("w", 10, "wip") + cards("p", 2),
               data_patch=muted_patch()) as s:
        s.limit_fact()
        st = s.measure(BASE)
        check("пауза и лимит не выключены: факт 429 — эффективный 0", st.get("эффективный") == 0
              and "лимит" in blockers(st), (st.get("эффективный"), blockers(st)))
        (s.log / "лимит-подписки.состояние").unlink()
        s.pause()
        st = s.measure(BASE + 2 * MIN)
        check("пауза режет при выключенных остальных", st.get("эффективный") == 0
              and "пауза" in blockers(st), (st.get("эффективный"), blockers(st)))
    with Stand("ultra", cards("w", 10, "wip") + cards("p", 2), data_patch=muted_patch(("авария",))) as s:
        s.set_load(4.5)
        effs = [s.measure(BASE + minute * MIN).get("эффективный") for minute in (0, 2, 4, 6)]
        check("условие создано: авария видна, хоть и выключена", "авария" in (s.state().get("не_действуют") or {}),
              sorted(s.state().get("не_действуют") or {}))
        check("выключенная авария не режет и через лестницу мощности", effs == [10] * 4
              and power_ceiling(s.state()) is None, (effs, power_ceiling(s.state())))
        s.set_load(0.5)
        s.measure(BASE + 8 * MIN)
        st = s.measure(BASE + 28 * MIN)
        check("условие создано: выключенная авария снята", not st.get("не_действуют"), st.get("не_действуют"))
        check("её снятие — не «подъём»: поднят_ts пуст, пинок не ждёт", st.get("поднят_ts") is None,
              st.get("поднят_ts"))


@case
def muted_blocker_releases_now():
    """19.11: мощность срезала 10→5; выключили данными — следующий проход 10, без «отпускает_мин» и без вести."""
    with MsgStand("ultra", cards("w", 10, "wip") + cards("p", 2)) as s:
        s.busy_pct = 97
        s.measure(BASE)
        s.measure(BASE + 2 * MIN)
        st = s.measure(BASE + 12 * MIN)
        check("условие создано: мощность режет до 5, владельцу сказано", st.get("эффективный") == 5
              and len(s.calls()) == 1, (st.get("эффективный"), s.calls()))
        s.data["блокеры"]["мощность"]["действует"] = "нет"
        s.write_all()
        st = s.measure(BASE + 13 * MIN)
        check("ГЛАВНОЕ: выключили — сразу 10", st.get("эффективный") == 10,
              (st.get("эффективный"), blockers(st)))
        later = [s.measure(BASE + minute * MIN).get("эффективный") for minute in (14, 50, 90)]
        check("и дальше 10 при 97 %", later == [10, 10, 10], later)
        check("ни одной вести о смене из-за выключенного", len(s.calls()) == 1, s.calls())
        check("mode.change 5→10 — «сказано: нет»", s.said()[-1:] == ["нет"], s.said())


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
