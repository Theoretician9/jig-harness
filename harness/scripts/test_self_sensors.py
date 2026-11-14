#!/usr/bin/env python3
"""Проба датчиков ядра scripts/self-sensors.py — написана ДО кода.

Спека .tasks/hr.self-configuration-environment/spec.md, ред. 2, §7 (случаи 1–11).
Каждый случай играет на СВОЁМ стенде: свой LOG_DIR (он же HARNESS_LOG_DIR),
паспорт, harness.conf, проект с картой и архивом, копия self-sensors.yaml.
CLAUDE_CODE_SESSION_ID снят: иначе alert-task ответит 6 и случаи 7–8 судят
пустоту. В конце — боевые карта и журналы не изменились.

    python3 scripts/test_self_sensors.py

Двери для мутаций ([[probe-loads-the-live-mechanism]]):
    HARNESS_SELF_SENSORS         путь self-sensors.py под пробой
    HARNESS_SELF_SENSORS_CONFIG  путь self-sensors.yaml под пробой (проба кладёт
                                 его копию в стенд и передаёт коду в ТОЙ ЖЕ
                                 переменной — код читает конфиг из неё)

Договор с кодом, который судит проба:
    --show [--at <epoch>]  печатает итог одним JSON-объектом (data строки журнала):
        {"дата", "окно", "без_даты": N,
         "датчики": {<ключ из конфига>: {"сейчас", "прошлое", "вердикт",
                     "счёты": {"сейчас": {...}, "прошлое": {...}}}}}
        вердикт: «хуже» | «лучше» | «в пороге» | null; счёты по ключам:
        attention {напоминания, идеи} · autonomy {сообщения, закрыто} ·
        efficiency {токены — целое, контекст+вывод; закрыто} · creation {создано, сломано}
    --record  раз в сутки строка sensors.daily в $LOG_DIR/self-sensors.jsonl,
        карточки и --resolve через alert-task.py; второй зов за сутки — код 0, «уже есть».
Последняя строка: `self-sensors проба: случаев N, падений F`.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent            # scripts/ (harness/scripts/ в пакете)
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402
from config import конфиг  # noqa: E402


def config_file(name: str) -> Path:
    """harness/config в проекте, config/ рядом со scripts/ в пакете."""
    for candidate in (ROOT / "harness" / "config" / name, ROOT / "config" / name):
        if candidate.is_file():
            return candidate
    return ROOT / "harness" / "config" / name


SENSORS = Path(os.environ.get("HARNESS_SELF_SENSORS") or HERE / "self-sensors.py")
CONFIG_SOURCE = Path(os.environ.get("HARNESS_SELF_SENSORS_CONFIG")
                     or config_file("self-sensors.yaml"))
LIVE_MAP = ROOT / "dev-map.yaml"
ALERT_JOURNAL = "тревоги-сторожей.jsonl"
SENSORS_JOURNAL = "self-sensors.jsonl"
KEYS = ("attention", "autonomy", "efficiency", "creation")

DAY = 86400
OWNER_TZ = datetime.timezone(datetime.timedelta(hours=5))
# Окна кончаются не в NOW, а в END = NOW − отставание из конфига: данные
# случаев 1–12 ставятся от END, поэтому мутация отставания краснеет только
# случаем 13, который судит само отставание.
# Даты карты — без времени, начало суток +05: сдвиг до суток. Поэтому «внутри
# окна» — 3 и 10 суток до END, далеко от границ 7 и 14.
NOW = int(time.time())
LAG = (yaml.safe_load(CONFIG_SOURCE.read_text(encoding="utf-8")) or {}).get("отставание_суток", 0) * DAY \
    if CONFIG_SOURCE.is_file() else 0
END = NOW - LAG
IN_CURRENT = END - 3 * DAY
IN_PAST = END - 10 * DAY

cases = Cases()
check = cases.check


# ── помощники ──────────────────────────────────────────────────────────────

def sha1(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return "нет файла"


def day_of(moment: float) -> str:
    return datetime.datetime.fromtimestamp(moment, OWNER_TZ).date().isoformat()


def iso_of(moment: float) -> str:
    return datetime.datetime.fromtimestamp(moment, OWNER_TZ).isoformat(timespec="seconds")


def jsonl_rows(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def task_block(task_id: str, status: str, done: str | None = None, tested: str | None = None,
               tested_at: str | None = None, accepted: str | None = None) -> str:
    def quoted(value):
        return f'"{value}"' if value else "null"
    extra = ""
    if tested_at:
        extra += f'        tested_at: "{tested_at}"\n'
    if accepted:
        extra += f'        принято_когда: "{accepted}"\n'
    return (f"      - id: {task_id}\n"
            f"        status: {status}\n"
            f"        owner_testable: false\n"
            f"        title: \"карточка стенда {task_id}\"\n"
            f"        summary: \"образец\"\n"
            f"{extra}"
            f"        volume: {{ loc: 1, complexity: 1, tokens_k: 1, estimated: true }}\n"
            f"        when: {{ started: null, done: {quoted(done)}, tested: {quoted(tested)}, "
            f"estimated: true }}\n")


def map_text(tasks: str = "") -> str:
    return ("version: 1\n"
            "layers:\n"
            "  - { id: infra, label: \"Инфраструктура\", color: \"#5aa06a\" }\n"
            "queue: []\n"
            "lines:\n"
            "  - id: harness\n"
            "    layer: infra\n"
            "    name: \"Харнес\"\n"
            "    tasks:\n"
            + task_block("hr.stand-plan", "plan") + tasks)


def archive_text(tasks: str = "") -> str:
    # Принятая задача далеко за окнами: нижняя граница прошлого окна её отсекает.
    return ("version: 1\n"
            "lines:\n"
            "  - id: harness\n"
            "    name: \"Харнес\"\n"
            "    tasks:\n"
            + task_block("hr.stand-ancient", "принято", accepted="2026-01-10") + tasks)


class Stand:
    """Свой мир одного случая."""

    def __init__(self, base: Path, tasks: str = "", archived: str = "", marker: str | None = None):
        self.base = base
        self.project = base / "project"
        self.log = base / "log"
        self.inbox = self.log / "inbox" / "обработано"
        self.map = self.project / "dev-map.yaml"
        self.archive = self.project / "docs" / "dev-map-archive.yaml"
        self.config = base / "self-sensors.yaml"
        self.serial = 0
        for d in (self.project / "docs", self.log / "locks" / "services", base / "secrets",
                  base / "panel", base / "home", base / "heartbeat", base / "state"):
            d.mkdir(parents=True, exist_ok=True)
        self.map.write_text(map_text(tasks), encoding="utf-8")
        self.archive.write_text(archive_text(archived), encoding="utf-8")
        settings = yaml.safe_load(CONFIG_SOURCE.read_text(encoding="utf-8"))
        if marker:
            settings["маркеры"]["напоминание"].append(marker)
        self.config.write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")
        self.settings = {"ACTIVE_LINE": "harness", "ALERT_TASKS_PER_WATCHER": "3",
                         "ALERT_MASS_MIN": "3", "ALERT_LOCK_WAIT_SEC": "5",
                         "DEVMAP_SUMMARY_MAX": "500", "SERVICE_LOCKS_SUBDIR": "services"}
        passport = {"PROJECT_DIR": str(self.project), "LOG_DIR": str(self.log),
                    "SECRETS_DIR": str(base / "secrets"), "TMUX_SESSION": "proba-datchikov",
                    "AGENT_START_CMD": "true", "TG_CHAT_ID": "100500",   # чат стенда, не секрет
                    "TG_TOKEN_FILE": str(base / "token"), "OWNER_TZ": ""}
        (base / "install.conf").write_text(
            "".join(f'{k}="{v}"\n' for k, v in passport.items()), encoding="utf-8")
        (base / "harness.conf").write_text(
            "".join(f'{k}="{v}"\n' for k, v in self.settings.items()), encoding="utf-8")

    def env(self) -> dict:
        env = dict(os.environ)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("HARNESS_REPORT", None)
        env.update({
            "PYTHONDONTWRITEBYTECODE": "1",
            "HARNESS_HOME": str(self.base / "home"),
            "HARNESS_INSTALL_CONF": str(self.base / "install.conf"),
            "HARNESS_CONF": str(self.base / "harness.conf"),
            "PROJECT_DIR": str(self.project),
            "LOG_DIR": str(self.log), "HARNESS_LOG_DIR": str(self.log),
            "SECRETS_DIR": str(self.base / "secrets"),
            "HARNESS_DEVMAP": str(self.map), "DEVMAP_ARCHIVE": str(self.archive),
            "HARNESS_ACTIVE_LINE": "harness", "HARNESS_PANEL_STATE": str(self.base / "panel"),
            "HARNESS_DOSKA": str(self.log / "доска.json"),
            "HEARTBEAT_DIR": str(self.base / "heartbeat"), "STATE_DIR": str(self.base / "state"),
            "HARNESS_SELF_SENSORS_CONFIG": str(self.config),
        })
        env.update(self.settings)
        return env

    def run(self, *args: str) -> tuple[int, str, str]:
        try:
            done = subprocess.run([sys.executable, str(SENSORS), *args], env=self.env(),
                                  capture_output=True, text=True, timeout=120, cwd=str(self.base))
        except subprocess.TimeoutExpired:
            return -9, "", "предел 120 с истёк"
        return done.returncode, done.stdout, done.stderr

    def show(self) -> tuple[int, dict, str]:
        """Итог --show; не JSON — пустой словарь и причина в третьем поле."""
        rc, out, err = self.run("--show", "--at", str(NOW))
        start = out.find("{")
        try:
            data, _ = json.JSONDecoder().raw_decode(out[start:]) if start >= 0 else ({}, 0)
        except ValueError:
            data = {}
        return rc, data if isinstance(data, dict) else {}, (err or out)[-300:]

    def record(self) -> tuple[int, str]:
        rc, out, err = self.run("--record")
        return rc, out + err

    def next_day(self) -> None:
        """Строка журнала за сегодня снята: следующий --record считает заново, как назавтра."""
        (self.log / SENSORS_JOURNAL).unlink(missing_ok=True)

    def message(self, moment: float, text: str) -> None:
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.serial += 1
        (self.inbox / f"{int(moment)}.{self.serial:09d}.txt").write_text(text, encoding="utf-8")

    def our_message(self, moment: float, text: str) -> None:
        with (self.log / "tg_out.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": iso_of(moment), "text": text}, ensure_ascii=False) + "\n")

    def tokens(self, moment: float, context: int, output: int) -> None:
        month = datetime.date.fromtimestamp(moment).strftime("%Y-%m")
        stamp = datetime.datetime.fromtimestamp(moment, datetime.timezone.utc)
        row = {"ts": stamp.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "session": "stand",
               "model": "stand", "in": context, "out": output, "cache_w": 0, "cache_r": 0}
        with (self.log / f"tokens-{month}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def alert(self, moment: float, key: str, event: str) -> None:
        row = {"ts": iso_of(moment), "ключ": key, "событие": event, "id": "",
               "сторож": key.split(":")[0], "почему": ""}
        with (self.log / ALERT_JOURNAL).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def miss(self, moment: float, what: str) -> None:
        row = {"дата": day_of(moment), "что": what, "механизм": "стенд",
               "почему": "стенд", "чинит": "стенд"}
        with (self.log / "пропуски.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def cards(self, key: str) -> list[dict]:
        data = yaml.safe_load(self.map.read_text(encoding="utf-8")) or {}
        return [t for line in data.get("lines") or [] for t in line.get("tasks") or []
                if t.get("источник") == f"тревога:{key}"]

    def alert_events(self, key: str) -> list[str]:
        return [r.get("событие") for r in jsonl_rows(self.log / ALERT_JOURNAL) if r.get("ключ") == key]

    def daily_rows(self) -> list[dict]:
        return [r for r in jsonl_rows(self.log / SENSORS_JOURNAL) if r.get("event") == "sensors.daily"]


def sensor(data: dict, key: str) -> dict:
    return (data.get("датчики") or {}).get(key) or {}


def counts(data: dict, key: str, window: str) -> dict:
    return (sensor(data, key).get("счёты") or {}).get(window) or {}


def find_key(data, name: str):
    """Значение ключа где угодно в итоге: место счёта «без_даты» спека не закрепляет."""
    if isinstance(data, dict):
        if name in data:
            return data[name]
        data = list(data.values())
    if isinstance(data, list):
        for item in data:
            found = find_key(item, name)
            if found is not None:
                return found
    return None


def ancestors_named(name: str) -> list[int]:
    found, pid = [], os.getppid()
    while pid > 1:
        try:
            status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        except OSError:
            break
        fields = dict(line.split(":\t", 1) for line in status.splitlines() if ":\t" in line)
        if fields.get("Name", "").strip() == name:
            found.append(pid)
        pid = int(fields.get("PPid", "0").strip() or 0)
    return found


def classify(base: Path, text: str, marker: str | None = None, quoted: str | None = None) -> str:
    """Чем код счёл ОДНО сообщение: по счётам внимания текущего окна."""
    st = Stand(base, marker=marker)
    if quoted:
        st.our_message(END - DAY - 600, quoted)
    st.message(END - DAY, text)
    rc, data, why = st.show()
    if rc != 0 or not data:
        return f"нет итога (код {rc}): {why}"
    now = counts(data, "attention", "сейчас")
    found = [label for label, field in (("напоминание", "напоминания"), ("идея", "идеи"))
             if now.get(field) == 1]
    return found[0] if len(found) == 1 else ("прочее" if not found else f"оба: {now}")


def attention_stand(base: Path, current_reminders: int, current_ideas: int) -> Stand:
    """Прошлое окно 1 напоминание / 5 идей = 0.17; текущее — как задано."""
    st = Stand(base)
    for i, text in enumerate(["почему пусто"] + ["предлагаю новый раздел"] * 5):
        st.message(IN_PAST + i * 60, text)
    for i in range(current_reminders):
        st.message(IN_CURRENT + i * 60, "почему пусто")
    for i in range(current_ideas):
        st.message(IN_CURRENT + 3600 + i * 60, "предлагаю новый раздел")
    return st


# ── случаи §7 ──────────────────────────────────────────────────────────────

def case_classifier(tmp: Path) -> None:
    print("── 1. классификатор ──")
    check("«почему не работает» → напоминание", "напоминание",
          classify(tmp / "a", "почему не работает"))
    check("«почему панель пустая» (только маркер «почему») → напоминание", "напоминание",
          classify(tmp / "b", "почему панель пустая"))
    check("«где отчёт» → напоминание", "напоминание", classify(tmp / "c", "где отчёт"))
    check("«давай сделаем экспорт отчётов» → идея", "идея",
          classify(tmp / "d", "давай сделаем экспорт отчётов"))
    check("«Да» → прочее", "прочее", classify(tmp / "e", "Да"))
    check("«везде тишина» — «где» целым словом не срабатывает → прочее", "прочее",
          classify(tmp / "f", "везде тишина"))
    check("без маркера в config «фиолетово» → прочее", "прочее",
          classify(tmp / "g", "фиолетово"))
    check("маркер «фиолетово», вставленный в config, работает без правки кода → напоминание",
          "напоминание", classify(tmp / "h", "фиолетово", marker="фиолетово"))


def case_quote(tmp: Path) -> None:
    print("── 2. цитата нашего сообщения ──")
    owner = "Почему не работает сборка панели: уже разбираю\nПринято"
    check("без нашего сообщения в tg_out — напоминание (цитата несёт маркер)", "напоминание",
          classify(tmp / "a", owner))
    check("строка из tg_out снята поодиночке — ответ не «напоминание» → прочее", "прочее",
          classify(tmp / "b", owner, quoted="Почему не работает сборка панели: уже разбираю"))


def case_windows(tmp: Path) -> None:
    print("── 3. окна ──")
    st = Stand(tmp)
    st.message(END - 8 * DAY, "почему пусто")
    st.message(END - DAY, "предлагаю новый раздел")
    rc, data, why = st.show()
    check(f"--show код 0 ({why[:80]})" if rc else "--show код 0", 0, rc)
    check("сообщение 8 суток назад в текущее окно не попало", 0,
          counts(data, "attention", "сейчас").get("напоминания"))
    check("сообщение 8 суток назад попало в прошлое окно", 1,
          counts(data, "attention", "прошлое").get("напоминания"))
    check("сообщение суток назад — в текущем окне", 1, counts(data, "attention", "сейчас").get("идеи"))
    check("автономность: непустых сообщений в текущем окне 1", 1,
          counts(data, "autonomy", "сейчас").get("сообщения"))


def case_closed(tmp: Path) -> None:
    print("── 4. закрытые задачи ──")
    tasks = (task_block("hr.closed-test-now", "test", done=day_of(IN_CURRENT))
             + task_block("hr.closed-moved", "принято", done=day_of(IN_PAST),
                          accepted=day_of(IN_CURRENT))
             + task_block("hr.closed-nodate", "test")
             + task_block("hr.wip-dated", "wip", done=day_of(IN_CURRENT)))
    archived = task_block("hr.arch-accepted", "принято", accepted=day_of(IN_CURRENT))
    st = Stand(tmp, tasks=tasks, archived=archived)
    rc, data, why = st.show()
    check(f"--show код 0 ({why[:80]})" if rc else "--show код 0", 0, rc)
    check("текущее окно: test+when.done и принятая из архива — закрыто 2", 2,
          counts(data, "autonomy", "сейчас").get("закрыто"))
    check("when.done в прошлом, принято_когда в текущем — считается в ПРОШЛОМ: закрыто 1", 1,
          counts(data, "autonomy", "прошлое").get("закрыто"))
    check("эффективность берёт тот же счёт закрытых", 2,
          counts(data, "efficiency", "сейчас").get("закрыто"))
    check("задача test без дат не считается: без_даты 1", 1, find_key(data, "без_даты"))


def case_minimum(tmp: Path) -> None:
    print("── 5. знаменатель меньше минимума ──")
    st = attention_stand(tmp, current_reminders=3, current_ideas=1)
    rc, data, why = st.show()
    check(f"--show код 0 ({why[:80]})" if rc else "--show код 0", 0, rc)
    check("знаменатель 4 < 5 → сейчас null", None, sensor(data, "attention").get("сейчас", "нет поля"))
    check("прошлое окно (6 ≥ 5) посчитано", True,
          isinstance(sensor(data, "attention").get("прошлое"), (int, float)))
    rc, out = st.record()
    check(f"--record код 0 ({out[-80:]})" if rc else "--record код 0", 0, rc)
    check("карточки нет", [], st.cards("self-sensors:attention"))
    check("в журнале тревог нет «заведена»", [], st.alert_events("self-sensors:attention"))


def case_record_once(tmp: Path) -> None:
    print("── 6. --record дважды за сутки ──")
    st = Stand(tmp)
    rc1, out1 = st.record()
    rc2, out2 = st.record()
    check("первый --record код 0", 0, rc1)
    check("второй --record код 0", 0, rc2)
    check("второй отвечает «уже есть»", True, "уже есть" in out2)
    check("в журнале одна строка sensors.daily", 1, len(st.daily_rows()))
    row = (st.daily_rows() or [{}])[0]
    check("в строке data с четырьмя датчиками", sorted(KEYS),
          sorted((row.get("data") or {}).get("датчики") or {}))


def case_worse(tmp: Path) -> None:
    print("── 7. ухудшение > порога → одна карточка ──")
    st = attention_stand(tmp, current_reminders=4, current_ideas=2)
    rc, data, why = st.show()
    check("внимание 0.17 → 0.67: вердикт «хуже»", "хуже", sensor(data, "attention").get("вердикт"))
    rc, out = st.record()
    check(f"--record код 0 ({out[-80:]})" if rc else "--record код 0", 0, rc)
    cards = st.cards("self-sensors:attention")
    check("одна карточка источник: тревога:self-sensors:attention", 1, len(cards))
    check("заголовок «… ушёл не туда …»", True,
          bool(cards) and "ушёл не туда" in str(cards[0].get("title")))
    check("другие датчики (знаменатель 0) карточек не завели", [],
          [k for k in KEYS[1:] if st.cards(f"self-sensors:{k}")])
    st.next_day()
    rc, out = st.record()
    check("второй прогон код 0", 0, rc)
    check("второй прогон: в журнале тревог «повтор»", True,
          "повтор" in st.alert_events("self-sensors:attention"))
    check("карточка по-прежнему одна", 1, len(st.cards("self-sensors:attention")))


def case_resolve(tmp: Path) -> None:
    print("── 8. возврат в порог → --resolve ──")
    st = attention_stand(tmp, current_reminders=4, current_ideas=2)
    st.record()
    check("до возврата тревога заведена", True,
          "заведена" in st.alert_events("self-sensors:attention"))
    for i in range(18):
        st.message(IN_CURRENT + 7200 + i * 60, "предлагаю новый раздел")
    rc, data, why = st.show()
    check("внимание 4/24 = прошлому 1/6: вердикт «в пороге»", "в пороге",
          sensor(data, "attention").get("вердикт"))
    st.next_day()
    rc, out = st.record()
    check(f"--record код 0 ({out[-80:]})" if rc else "--record код 0", 0, rc)
    events = st.alert_events("self-sensors:attention")
    check("последнее событие тревоги — «зелёное» (--resolve)", "зелёное",
          events[-1] if events else "нет событий")


def case_empty(tmp: Path) -> None:
    print("── 9. нет входящих и журнала токенов ──")
    st = Stand(tmp)
    rc, data, why = st.show()
    check(f"--show код 0 ({why[:80]})" if rc else "--show код 0", 0, rc)
    check("все четыре датчика сейчас null", {k: None for k in KEYS},
          {k: sensor(data, k).get("сейчас", "нет поля") for k in KEYS})
    rc, out = st.record()
    check(f"--record код 0 ({out[-80:]})" if rc else "--record код 0", 0, rc)


def case_token_window(tmp: Path) -> None:
    print("── 10. верхняя граница окна токенов ──")
    st = Stand(tmp)
    st.tokens(IN_PAST, 100, 900)
    st.tokens(END - 2 * DAY, 3, 4)
    rc, data, why = st.show()
    check(f"--show код 0 ({why[:80]})" if rc else "--show код 0", 0, rc)
    check("текущее окно: токенов 7", 7, counts(data, "efficiency", "сейчас").get("токены"))
    check("прошлое окно: токенов 1000 — событие текущего окна в него не попало", 1000,
          counts(data, "efficiency", "прошлое").get("токены"))


def case_own_alerts(tmp: Path) -> None:
    print("── 11. своя карточка не «сломано» ──")
    st = Stand(tmp)
    st.alert(END - 2 * DAY, "self-sensors:attention", "заведена")
    st.alert(END - 2 * DAY, "nightly-gates:red", "заведена")
    st.alert(END - 2 * DAY + 60, "nightly-gates:red", "повтор")
    st.alert(IN_PAST, "nightly-gates:old", "заведена")
    st.miss(IN_CURRENT, "пропуск стенда")
    rc, data, why = st.show()
    check(f"--show код 0 ({why[:80]})" if rc else "--show код 0", 0, rc)
    check("сломано сейчас 2: чужая «заведена» + пропуск; self-sensors и «повтор» не входят", 2,
          counts(data, "creation", "сейчас").get("сломано"))
    check("сломано в прошлом окне 1", 1, counts(data, "creation", "прошлое").get("сломано"))


def case_alert_refused(tmp: Path) -> None:
    print("── 12. alert-task отказал → строки дня нет ──")
    st = attention_stand(tmp, current_reminders=4, current_ideas=2)
    copy = tmp / "copy"
    copy.mkdir(parents=True)
    shutil.copy(SENSORS, copy / "self-sensors.py")
    (copy / "lib").symlink_to(HERE / "lib")
    (copy / "alert-task.py").write_text("import sys\nsys.exit(6)\n", encoding="utf-8")
    rc, out, err = run_copy(st, copy / "self-sensors.py")
    check(f"alert-task ответил 6: --record код 0 ({(out + err)[-80:]})" if rc else "--record код 0", 0, rc)
    check("alert-task ответил 6: строки дня в журнале нет", [], st.daily_rows())
    (copy / "alert-task.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    run_copy(st, copy / "self-sensors.py")
    check("alert-task ответил 0: строка дня записана", 1, len(st.daily_rows()))


def case_closed_window(tmp: Path) -> None:
    print("── 13. судится закрытое окно: закрытия доезжают задним числом ──")
    # Токены пишутся сразу, а закрытия задач датируются задним числом: за
    # последние сутки токены уже есть, закрытий ещё нет. Каждые сутки, кроме
    # последних, — 2 закрытия и 1 млн токенов, все в полдень суток (далеко от
    # границ окон при любом целом отставании ≤ 1). Последние сутки — 5 млн
    # токенов и 0 закрытий. Датчик без отставания видит в них «хуже»
    # (11 млн / 12 против 7 млн / 14).
    def noon(k: int) -> float:
        return NOW - (k + 0.5) * DAY
    tasks = "".join(task_block(f"hr.closed-day{k}-{i}", "test", tested_at=iso_of(noon(k) + i * 60))
                    for k in range(1, 16) for i in range(2))
    st = Stand(tmp, tasks=tasks)
    for k in range(1, 16):
        st.tokens(noon(k), 1_000_000, 0)
    st.tokens(noon(0), 5_000_000, 0)
    rc, before, why = st.show()
    check(f"--show код 0 ({why[:80]})" if rc else "--show код 0", 0, rc)
    check("последние сутки ещё дописываются: эффективность не «хуже» — сравниваются закрытые окна",
          "в пороге", sensor(before, "efficiency").get("вердикт"))
    late = "".join(task_block(f"hr.closed-late-{i}", "test", tested_at=iso_of(noon(0) + i * 60))
                   for i in range(10))
    st.map.write_text(map_text(tasks + late), encoding="utf-8")
    rc, after, why = st.show()
    check("закрытия, доехавшие задним числом за последние сутки, не меняют судимые окна",
          [sensor(before, k).get("счёты") for k in ("autonomy", "efficiency")],
          [sensor(after, k).get("счёты") for k in ("autonomy", "efficiency")])


def run_copy(st: "Stand", script: Path) -> tuple[int, str, str]:
    done = subprocess.run([sys.executable, str(script), "--record"], env=st.env(),
                          capture_output=True, text=True, timeout=120, cwd=str(st.base))
    return done.returncode, done.stdout, done.stderr


CASES = (case_classifier, case_quote, case_windows, case_closed, case_minimum,
         case_record_once, case_worse, case_resolve, case_empty, case_token_window,
         case_own_alerts, case_alert_refused, case_closed_window)


# ── стенд не протёк ────────────────────────────────────────────────────────

def live_state() -> dict:
    """Боевые карта и журналы. Журнал тревог пишут и живые сторожа во время
    прогона, а карту правят соседние задачи, поэтому судятся не sha, а числа
    строк и карточек с ключом self-sensors."""
    log = Path(конфиг()["LOG_DIR"])
    own = sum(1 for r in jsonl_rows(log / ALERT_JOURNAL)
              if str(r.get("ключ", "")).startswith("self-sensors:"))
    cards = sum(1 for line in (yaml.safe_load(LIVE_MAP.read_text(encoding="utf-8")) or {}).get("lines") or []
                for task in line.get("tasks") or []
                if str(task.get("источник", "")).startswith("тревога:self-sensors:"))
    return {"карточек self-sensors в карте": cards, "журнал датчиков": sha1(log / SENSORS_JOURNAL),
            "строк self-sensors в журнале тревог": own}


def main() -> int:
    check("проба не под сессией (иначе alert-task отвечает 6 и случаи 7–8 судят пустоту)",
          [], ancestors_named("claude"))
    check("код scripts/self-sensors.py существует", True, SENSORS.is_file())
    check("конфиг self-sensors.yaml существует", True, CONFIG_SOURCE.is_file())
    if not (SENSORS.is_file() and CONFIG_SOURCE.is_file()):
        missing = "нет scripts/self-sensors.py" if not SENSORS.is_file() else "нет self-sensors.yaml"
        print(f"self-sensors проба: случаев {cases.paths}, падений {cases.failures} — "
              f"{missing}, случаи 1–13 не прогнаны")
        return 1
    before = live_state()
    with tempfile.TemporaryDirectory(prefix="self-sensors-probe-") as tmp:
        for case in CASES:
            try:
                case(Path(tmp) / case.__name__)
            except Exception as error:  # падение случая — провал, а не тишина
                check(f"{case.__name__} дошёл до конца", "дошёл",
                      f"упал: {type(error).__name__}: {error}")
    check("боевые карта и журналы не тронуты", before, live_state())
    print(f"self-sensors проба: случаев {cases.paths}, падений {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
