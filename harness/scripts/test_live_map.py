#!/usr/bin/env python3
"""Проба сборщика живой карты (`scripts/live-map.py`).

Откуда взято. Владелец 21.11.2026: «весь процесс работы визуализировать … где
видно как ходят данные, где какие события происходят, где какие нагрузки, кто
это делает, в какой иерархии … в онлайн режиме». Спека живой карты
§5.1–§5.6, критерии К-1…К-8; план — Task 1–3 (задача — трейлер Dev-Map коммита).
Снимок уходит в браузер владельца каждые 5 с: если
сборщик врёт о части, о ребре или о том, кто работает, владелец решает по
неправде, а ворота этого не видят.

Каждый случай строит СВОЙ стенд во временном каталоге: свои LOG_DIR,
HARNESS_LOG_DIR, STATE_DIR, PANEL_STATE_DIR, HEARTBEAT_DIR, свой harness.conf с
ключами LIVE_MAP_*, свой journals.yaml (HARNESS_JOURNALS; записи скопированы из
боевого реестра РАЗБОРОМ, не руками), свой mechanisms.yaml (HARNESS_USTROJSTVO),
свой dev-map.yaml (HARNESS_DEVMAP) и свой реестр частей (HARNESS_ARCHITECTURE).
Модуля `architecture` на master нет (он в ветке 19c5ba51), поэтому стенд кладёт
подставной `architecture.py` по контракту 19c5ba51 и ставит его каталог первым
в PYTHONPATH сборщика. Боевые журналы и боевой каталог панели не читаются и не
пишутся.

Дверь мутаций — LIVE_MAP_BIN: путь сборщика (умолчание — `live-map.py` рядом
с пробой). Таблица рёбер для сверки К-4 подаётся дверью LIVE_MAP_EDGES; без неё
сборщик берёт боевую `harness/config/live-map.yaml`. Образец формата снимка —
`scripts/fixtures/live-map/snapshot-v1.json`.

Прогон (только фоном и через пул тяжёлых прогонов):
    python3 scripts/test_live_map.py
Последняя строка: «ЖИВАЯ КАРТА: путей N, неудач M»; код 0 — все пути зелёные.
"""
import datetime as dt
import fcntl
import importlib.util
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
# Проект: scripts/ рядом с harness/; пакет: harness/scripts/ рядом с harness/config.
if (HERE.parent / "harness" / "config").is_dir():
    CONFIG_DIR = HERE.parent / "harness" / "config"
    LIVE_DEVMAP = HERE.parent / "dev-map.yaml"
else:
    CONFIG_DIR = HERE.parent / "config"
    LIVE_DEVMAP = HERE.parent / "dev-map.yaml"
LIB_DIR = HERE / "lib"
COLLECTOR = Path(os.environ.get("LIVE_MAP_BIN") or HERE / "live-map.py")
FIXTURE = HERE / "fixtures" / "live-map" / "snapshot-v1.json"
LIVE_EDGES = CONFIG_DIR / "live-map.yaml"
LIVE_JOURNALS = CONFIG_DIR / "journals.yaml"
LIVE_MECHANISMS = CONFIG_DIR / "mechanisms.yaml"
LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

TMP = Path(tempfile.mkdtemp(prefix="test-live-map-"))
SNAPSHOT_LIMIT = 65536
TEXT_LIMIT = 200
EVENTS_LIMIT = 200
SECRET = "deadbeefcafebabe0123456789abcdef01234567"  # не-секрет: образец больного случая
REPORT_RE = re.compile(r"прочитано_байт=(\d+) событий=(\d+) журналов=(\d+) сборка_мс=(\d+)")
CPU_RE = re.compile(r"цп_мс=(\d+)")
STATE_NAME = "live-map-state"
# Процесс витрины → механизм: записи process-library.json стенда. build-board несёт
# «механизмы», probe-sender — только «файлы»; агенты (helpers, claude-session…)
# приписаны к части строкой боевой таблицы live-map.yaml, а не файлом.
STAND_PROCESSES = [
    {"id": "build-board", "файлы": ["scripts/build-board.py"], "механизмы": ["scripts/build-board.py"]},
    {"id": "probe-sender", "файлы": ["scripts/tg_send.sh"], "механизмы": None},
    {"id": "helpers", "файлы": [".claude/agents/harness-worker.md"], "механизмы": None},
    {"id": "claude-session-commands", "файлы": ["scripts/gates.sh"], "механизмы": None},
]

sys.path.insert(0, str(LIB_DIR))
import events  # noqa: E402

paths = failures = 0


def path_ok(text: str) -> None:
    global paths
    paths += 1
    print("ок  · " + text)


def path_bad(text: str) -> None:
    global paths, failures
    paths += 1
    failures += 1
    print("ПЛОХО · " + text)


def check(condition, good: str, bad: str) -> None:
    if condition:
        path_ok(good)
    else:
        path_bad(bad)


def section(title: str) -> None:
    print(f"── {title}")


def finish() -> None:
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"ЖИВАЯ КАРТА: путей {paths}, неудач {failures}")
    sys.exit(1 if failures else 0)


def load_yaml(path: Path):
    with open(path, encoding="utf-8") as fh:
        return yaml.load(fh, Loader=LOADER)


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso(moment: dt.datetime) -> str:
    return moment.isoformat()


def iso_z(moment: dt.datetime) -> str:
    # Журнал токенов пишет время в UTC с «Z», как боевой сборщик токенов.
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def v1_row(src: str, event: str, outcome: str = "ok", at: dt.datetime | None = None,
           task: str = "-", data: dict | None = None) -> dict:
    return {"v": 1, "ts": iso(at or now_local()), "src": src, "event": event,
            "outcome": outcome, "task": task, "task_from": "arg", "session": "probe-session",
            "data": data or {}}


# ── реестр частей стенда: восемь частей и контракты 19c5ba51 плюс выход №21 ──
# Части и пары взяты из `git show 19c5ba51:harness/config/architecture.yaml`
# (разбор 22.11); №21 `channel → внешний:владелец` добавляет версия 1 (§5.4).
STAND_PARTS = [
    {"id": "channel", "имя": "Канал",
     "входы": [{"от": "*", "что": "весть владельцу"}, {"от": "map", "что": "список задач для отчёта"},
               {"от": "внешний:владелец", "что": "сообщения"}],
     "выходы": [{"кому": "map", "что": "разбор сообщения"}, {"кому": "release", "что": "согласие"},
                {"кому": "executors", "что": "толчок смене"},
                {"кому": "внешний:владелец", "что": "весть, отчёт, кнопки согласия"}],
     "элементы": [{"id": "channel.intake", "имя": "Приём"}, {"id": "channel.send", "имя": "Отправка"},
                  {"id": "channel.panel", "имя": "Страница управления"}]},
    {"id": "map", "имя": "Карта",
     "входы": [{"от": "channel", "что": "разбор сообщения"}, {"от": "network", "что": "находка"},
               {"от": "quality", "что": "тревога о красном"}, {"от": "executors", "что": "статус работы"}],
     "выходы": [{"кому": "pipeline", "что": "карточка с элементом и глубиной"},
                {"кому": "channel", "что": "задачи для отчёта"}],
     "элементы": [{"id": "map.cards", "имя": "Карточки"}, {"id": "map.board", "имя": "Экран"}]},
    {"id": "pipeline", "имя": "Пайплайн",
     "входы": [{"от": "map", "что": "карточка"}],
     "выходы": [{"кому": "executors", "что": "шаг с моделью шага"},
                {"кому": "quality", "что": "перечень обязательных проверок задачи"}],
     "элементы": [{"id": "pipeline.steps", "имя": "Шаги"}]},
    {"id": "quality", "имя": "Качество",
     "входы": [{"от": "executors", "что": "правка в дереве"}, {"от": "pipeline", "что": "перечень проверок"}],
     "выходы": [{"кому": "release", "что": "вердикт ворот"}, {"кому": "map", "что": "тревога о красном"},
                {"кому": "channel", "что": "числа для вести"}],
     "элементы": [{"id": "quality.gates", "имя": "Ворота"}]},
    {"id": "executors", "имя": "Исполнители",
     "входы": [{"от": "pipeline", "что": "шаг"}, {"от": "machine", "что": "ресурс"},
               {"от": "channel", "что": "толчок смене"}],
     "выходы": [{"кому": "quality", "что": "правка"}, {"кому": "map", "что": "статус работы"},
                {"кому": "channel", "что": "пульс смены"}],
     "элементы": [{"id": "executors.launch", "имя": "Запуск"}]},
    {"id": "release", "имя": "Выпуск",
     "входы": [{"от": "quality", "что": "вердикт ворот"}, {"от": "channel", "что": "согласие"}],
     "выходы": [{"кому": "network", "что": "версия"}, {"кому": "machine", "что": "команда обновления"}],
     "элементы": [{"id": "release.deploy", "имя": "Выкат"}]},
    {"id": "machine", "имя": "Машина",
     "входы": [{"от": "*", "что": "регистрация процесса"}, {"от": "release", "что": "команда обновления"}],
     "выходы": [{"кому": "*", "что": "процесс, ресурс, копия данных"},
                {"кому": "channel", "что": "весть о беде сервера"}],
     "элементы": [{"id": "machine.processes", "имя": "Процессы"}]},
    {"id": "network", "имя": "Сеть копий",
     "входы": [{"от": "release", "что": "версия"}, {"от": "внешний:копии", "что": "находки"}],
     "выходы": [{"кому": "map", "что": "находка"}, {"кому": "channel", "что": "весть о копии"},
                {"кому": "внешний:копии", "что": "версия"}],
     "элементы": [{"id": "network.copies", "имя": "Копии"}]},
]

# Подставной модуль по контракту 19c5ba51 (`registry_path`, `load`, `part_of`,
# `RegistryError`): настоящий лежит в ветке, а проба обязана жить на master.
FAKE_ARCHITECTURE = '''import os
import yaml
class RegistryError(Exception):
    pass
def registry_path():
    return os.environ.get("HARNESS_ARCHITECTURE") or "/нет/architecture.yaml"
def load(path=None):
    try:
        with open(path or registry_path(), encoding="utf-8") as fh:
            data = yaml.load(fh, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    except (OSError, yaml.YAMLError) as exc:
        raise RegistryError(str(exc)) from exc
    if not isinstance(data, dict):
        raise RegistryError("корень не словарь")
    return data
def part_of(element):
    return element.split(".", 1)[0]
'''

STAND_MECHANISMS = [
    {"файл": "scripts/tg_send.sh", "элемент": "channel.send"},
    {"файл": "scripts/tg-dispatcher.sh", "элемент": "channel.intake"},
    {"файл": "scripts/gates.sh", "элемент": "quality.gates"},
    {"файл": "scripts/hooks/agent_budget_guard.py", "элемент": "executors.launch"},
    {"файл": "scripts/hooks/agent_finish_log.py", "элемент": "executors.launch"},
    {"файл": "scripts/build-board.py", "элемент": "map.board"},
]


def live_journals() -> dict:
    return load_yaml(LIVE_JOURNALS)


def pick_journals(registry: dict) -> dict:
    """Записи стенда — из боевого реестра разбором: адаптер обязан быть тем же, что у читалки."""
    journals = registry.get("journals") or {}
    chosen = {name: dict(journals[name]) for name in ("tg_out", "gates", "inbox", "вызовы-агентов")
              if name in journals}
    sample = next((n for n, e in journals.items() if e.get("kind") == "sample"
                   and e.get("time_kind") not in (None, "none")), None)
    no_time = next((n for n, e in journals.items() if e.get("time_kind") in (None, "none")
                    and e.get("kind") != "sample"), None)
    for name in (sample, no_time):
        if name:
            chosen[name] = dict(journals[name])
    return chosen


def token_template(registry: dict) -> tuple[str, dict] | None:
    family = sorted(n for n in (registry.get("journals") or {}) if re.fullmatch(r"tokens-\d{4}-\d{2}", n))
    if not family:
        return None
    return family[-1], dict(registry["journals"][family[-1]])


def month_name(moment: dt.datetime) -> str:
    return moment.strftime("tokens-%Y-%m")


def previous_month_name(moment: dt.datetime) -> str:
    first = moment.replace(day=1)
    return month_name(first - dt.timedelta(days=1))


class Stand:
    """Свой каталог на один случай: счётчики одного случая не мешают другому."""

    def __init__(self, name: str, *, conf: dict | None = None):
        self.root = TMP / name
        self.log = self.root / "log"
        self.state = self.root / "state"
        self.panel = self.root / "panel-state"
        self.heartbeat = self.state / "heartbeat"
        self.own_state = self.log / STATE_NAME
        self.archdir = self.root / "archmod"
        for directory in (self.log, self.state, self.panel, self.heartbeat, self.archdir,
                          self.log / "heavy-slots"):
            directory.mkdir(parents=True, exist_ok=True)
        (self.archdir / "architecture.py").write_text(FAKE_ARCHITECTURE, encoding="utf-8")
        self.architecture = self.archdir / "architecture.yaml"
        self.write_yaml(self.architecture, {"версия": 1, "части": STAND_PARTS})
        self.mechanisms = self.root / "mechanisms.yaml"
        self.write_yaml(self.mechanisms, {"механизмы": STAND_MECHANISMS})
        self.process_library = self.root / "process-library.json"
        self.process_library.write_text(json.dumps({"версия": 1, "процессы": STAND_PROCESSES},
                                                   ensure_ascii=False), encoding="utf-8")
        self.devmap = self.root / "dev-map.yaml"
        self.write_yaml(self.devmap, {"version": 1, "lines": [{"id": "hr", "tasks": [
            {"id": "hr.probe-mapped", "status": "wip", "элемент": "map.cards", "title": "проба"}]}]})
        self.journals_path = self.root / "journals.yaml"
        registry = live_journals()
        self.journals = pick_journals(registry)
        self.disk_families = registry.get("disk_families") or []
        self.save_journals()
        for slot in range(1, 5):
            (self.log / "heavy-slots" / f"{slot}.lock").touch()
        self.conf = {"LIVE_MAP_PERIOD_S": "5", "LIVE_MAP_IDLE_S": "60", "LIVE_MAP_VIEWER_S": "120",
                     "LIVE_MAP_WINDOW_S": "60", "LIVE_MAP_AGENT_TTL_S": "7200", "HEAVY_SLOTS": "4"}
        self.conf.update(conf or {})
        self.harness_conf = self.root / "harness.conf"
        self.harness_conf.write_text("".join(f'{k}="{v}"\n' for k, v in self.conf.items()),
                                     encoding="utf-8")
        self.install_conf = self.root / "install.conf"
        self.install_conf.write_text(
            f'LOG_DIR="{self.log}"\nSTATE_DIR="{self.state}"\nPANEL_STATE_DIR="{self.panel}"\n'
            f'HEARTBEAT_DIR="{self.heartbeat}"\n', encoding="utf-8")
        self.with_module = True

    @staticmethod
    def write_yaml(path: Path, data) -> None:
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def save_journals(self) -> None:
        self.write_yaml(self.journals_path, {"disk_families": self.disk_families, "journals": self.journals})

    def journal_path(self, name: str) -> Path:
        return self.log / self.journals[name]["file"]

    def touch_all(self) -> None:
        for name in self.journals:
            self.journal_path(name).touch()

    def append(self, name: str, rows, raw: str = "") -> None:
        with open(self.journal_path(name), "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write((row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)) + "\n")
            fh.write(raw)

    def env(self, **extra) -> dict:
        env = dict(os.environ)
        env.update({
            "HARNESS_HOME": str(self.root), "HARNESS_CONF": str(self.harness_conf),
            "HARNESS_INSTALL_CONF": str(self.install_conf),
            "LOG_DIR": str(self.log), "HARNESS_LOG_DIR": str(self.log),
            "HARNESS_EVENTS_STATE": str(self.log / ".events"),
            "STATE_DIR": str(self.state), "PANEL_STATE_DIR": str(self.panel),
            "HEARTBEAT_DIR": str(self.heartbeat),
            "HARNESS_JOURNALS": str(self.journals_path), "HARNESS_ARCHITECTURE": str(self.architecture),
            "HARNESS_USTROJSTVO": str(self.mechanisms), "HARNESS_DEVMAP": str(self.devmap),
            "HARNESS_PROCESS_LIBRARY_JSON": str(self.process_library),
            "HARNESS_PROCESS_LIBRARY_LOCAL": str(self.root / "нет-слоя.json"),
            "HARNESS_PROCESS_MEASURE": str(self.root / "нет-замера.json"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        env.update(self.conf)
        env.pop("LIVE_MAP_EDGES", None)
        old = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (str(self.archdir) + (":" + old if old else "")) if self.with_module else old
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def run(self, *args, timeout: float = 60, **extra) -> subprocess.CompletedProcess:
        if not COLLECTOR.is_file():
            return subprocess.CompletedProcess([str(COLLECTOR)], 127, "",
                                               f"сборщика нет: {COLLECTOR}")
        try:
            return subprocess.run([sys.executable, str(COLLECTOR), *args], env=self.env(**extra),
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess([str(COLLECTOR)], 124, "", f"не уложился в {timeout} с")

    def state_file(self) -> Path:
        return self.own_state / "live-map.state.json"

    def saved_state(self) -> dict:
        try:
            return json.loads(self.state_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def snapshot_path(self) -> Path:
        return self.panel / "live-map.json"

    def snapshot(self) -> dict | None:
        try:
            return json.loads(self.snapshot_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def why(proc: subprocess.CompletedProcess) -> str:
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
    return f"код {proc.returncode}: {tail[0][:200]}"


def part(snap: dict | None, part_id: str) -> dict:
    for item in (snap or {}).get("части") or []:
        if item.get("id") == part_id:
            return item
    return {}


def edge_index(snap: dict | None, source: str, target: str) -> int | None:
    for index, edge in enumerate((snap or {}).get("рёбра") or []):
        if edge.get("от") == source and edge.get("к") == target:
            return index
    return None


def report_numbers(proc: subprocess.CompletedProcess) -> dict | None:
    found = REPORT_RE.search(proc.stdout or "")
    if not found:
        return None
    numbers = dict(zip(("bytes", "events", "journals", "build_ms"), map(int, found.groups())))
    cpu = CPU_RE.search(proc.stdout or "")
    numbers["cpu_ms"] = int(cpu.group(1)) if cpu else None
    return numbers


def all_strings(node):
    """Каждая строка снимка — и значения, и ключи: в браузер уходят обе."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from all_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from all_strings(value)
    elif isinstance(node, str):
        yield node


def stand_exits() -> list[tuple[str, str]]:
    return [(p["id"], out["кому"]) for p in STAND_PARTS for out in p["выходы"]]


def table_rows() -> list[dict]:
    try:
        return list((load_yaml(LIVE_EDGES) or {}).get("рёбра") or [])
    except OSError:
        return []


def fixture_keys(snap: dict, key: str) -> None:
    """Ключи элементов списка снимка — как у образца §5.5: обе стороны волны судят по нему."""
    try:
        sample = (json.loads(FIXTURE.read_text(encoding="utf-8")).get(key) or [{}])[0]
    except (OSError, ValueError) as exc:
        path_bad(f"«{key}[]»: образец {FIXTURE} не прочитан ({type(exc).__name__})")
        return
    items = snap.get(key) or []
    bad = [item for item in items if set(item) != set(sample)]
    check(sample and items and not bad, f"ключи «{key}[]» — как у образца",
          f"«{key}[]»: элементов {len(items)}, ключи {sorted(set(bad[0])) if bad else '—'} "
          f"против образца {sorted(set(sample))}")


def load_collector_module():
    """Сборщик как модуль: функции сверки судятся напрямую, ниже `if __name__` логики нет."""
    if not COLLECTOR.is_file():
        return None
    spec = importlib.util.spec_from_file_location("live_map_under_test", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — любая беда импорта — провал пробы, не падение
        print(f"  импорт сборщика: {type(exc).__name__}: {exc}")
        return None
    return module


# ═════════════════════════ Task 1: ядро ═════════════════════════

def case_k1_shape() -> None:
    section("К-1(в): формат снимка по образцу §5.5")
    stand = Stand("k1-shape")
    stand.touch_all()
    proc = stand.run("--один-снимок")
    check(proc.returncode == 0, "один снимок: код 0", f"один снимок упал — {why(proc)}")
    raw = stand.snapshot_path().read_bytes() if stand.snapshot_path().exists() else b""
    snap = stand.snapshot()
    check(snap is not None, "снимок проходит json.load",
          f"снимка {stand.snapshot_path()} нет или он не JSON")
    snap = snap or {}
    check(snap.get("v") == 1, "v == 1", f"v = {snap.get('v')!r}, ждали 1")
    registry_ids = [p["id"] for p in STAND_PARTS]
    got_ids = [p.get("id") for p in snap.get("части") or []]
    check(len(registry_ids) == 8 and got_ids == registry_ids,
          "части — ровно 8 частей реестра в его порядке",
          f"части {got_ids}, ждали {registry_ids} (порядок реестра)")
    check(0 < len(raw) <= SNAPSHOT_LIMIT, f"размер {len(raw)} ≤ {SNAPSHOT_LIMIT} байт",
          f"размер снимка {len(raw)} байт (пустой или больше {SNAPSHOT_LIMIT})")
    mode = stand.snapshot_path().stat().st_mode & 0o777 if raw else None
    check(mode == 0o640, "права снимка 0640", f"права снимка {oct(mode) if mode else '—'}, ждали 0o640")
    state = stand.state_file()
    check(state.is_file(), "файл состояния $LOG_DIR/live-map-state/live-map.state.json записан",
          f"нет файла состояния {state}")
    taken = snap.get("снято")
    try:
        aware = dt.datetime.fromisoformat(taken).tzinfo is not None
    except (TypeError, ValueError):
        aware = False
    check(aware, "«снято» — RFC 3339 со смещением", f"«снято» = {taken!r}: не время со смещением")
    leftovers = [p.name for p in stand.panel.iterdir() if p.name != "live-map.json"
                 and p.name.startswith("live-map") and p.name != "live-map.viewer"]
    check(raw and not leftovers, "временных файлов записи в каталоге панели не осталось",
          f"после записи остались {leftovers or 'нет снимка'}: запись не атомарна или её нет")

    exits = stand_exits()
    expected_pairs = set(exits) | {("внешний:владелец", "channel")}
    got_pairs = [(e.get("от"), e.get("к")) for e in snap.get("рёбра") or []]
    check(len(exits) == 21 and len(got_pairs) == 22 and set(got_pairs) == expected_pairs,
          "рёбра — 21 выход реестра и вход владельца (22)",
          f"рёбер {len(got_pairs)}; лишние {sorted(set(got_pairs) - expected_pairs)}, "
          f"недостающие {sorted(expected_pairs - set(got_pairs))}")
    rows = table_rows()
    check(len(rows) > 0, f"таблица рёбер {LIVE_EDGES} разобрана: строк {len(rows)}",
          f"таблица рёбер {LIVE_EDGES} пуста или её нет")
    with_source = {(r.get("от"), r.get("к")) for r in rows}
    wrong = [(e.get("от"), e.get("к")) for e in snap.get("рёбра") or []
             if e.get("источник") is not ((e.get("от"), e.get("к")) in with_source)]
    check(rows and got_pairs and not wrong, "«источник» у каждого ребра — есть ли строка таблицы",
          f"«источник» расходится с таблицей у {wrong or 'всех (рёбер нет)'}")
    sourced_exits = sum(1 for pair in exits if pair in with_source)
    check(sourced_exits == 11, "с источником 11 выходов из 21 (§5.4)",
          f"с источником {sourced_exits} выходов из 21, ждали 11 — таблица разошлась со спекой")
    externals = {e.get("id") for e in snap.get("внешние") or []}
    check(externals == {"внешний:владелец", "внешний:копии"}, "внешние — владелец и копии из реестра",
          f"внешние {sorted(externals, key=str)}, ждали внешний:владелец и внешний:копии")
    channel = part(snap, "channel")
    expected_mech = sum(1 for m in STAND_MECHANISMS if m["элемент"].startswith("channel."))
    check(channel.get("механизмов") == expected_mech and channel.get("событий") == 0,
          f"пустая часть channel есть с нулями, механизмов {expected_mech}",
          f"channel: механизмов {channel.get('механизмов')!r} (ждали {expected_mech}), "
          f"событий {channel.get('событий')!r} (ждали 0)")

    try:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        path_bad(f"образец {FIXTURE} не прочитан ({type(exc).__name__}) — формат не с чем сверить")
        return
    check(set(snap) == set(fixture), "ключи верхнего уровня — как у образца",
          f"ключи снимка расходятся с образцом: лишние {sorted(set(snap) - set(fixture))}, "
          f"недостающие {sorted(set(fixture) - set(snap))}")
    for key in ("части", "рёбра"):
        sample_keys = set((fixture.get(key) or [{}])[0])
        bad = [item for item in snap.get(key) or [] if set(item) != sample_keys]
        check(sample_keys and snap.get(key) and not bad, f"ключи «{key}[]» — как у образца",
              f"«{key}[]»: ключи {sorted(set(bad[0])) if bad else '—'} против образца {sorted(sample_keys)}")


def case_k2_place() -> None:
    section("К-2: событие — в свою часть и на своё ребро")
    stand = Stand("k2")
    stand.touch_all()
    first = stand.run("--один-снимок")
    check(first.returncode == 0, "первый снимок на пустых журналах: код 0", f"первый снимок — {why(first)}")
    # Битые строки: читалка их пропускает, свой json.loads сборщика на них упадёт —
    # так проба отличает подачу строк в iter_journal_events от второго разбора.
    stand.append("tg_out", ["{не json", "[1, 2]", v1_row("scripts/tg_send.sh", "tg.sent")])
    stand.append("gates", [v1_row("scripts/gates.sh", "gate.verdict", outcome="refused")])
    stand.append("inbox", [v1_row("scripts/not-a-mechanism.sh", "probe.unplaced")])
    proc = stand.run("--один-снимок")
    check(proc.returncode == 0, "снимок по дописи: код 0", f"снимок по дописи — {why(proc)}")
    snap = stand.snapshot() or {}
    channel, quality = part(snap, "channel"), part(snap, "quality")
    check(channel.get("событий") == 1, "channel.событий == 1",
          f"channel.событий = {channel.get('событий')!r}, ждали 1")
    check(quality.get("событий") == 1, "quality.событий == 1",
          f"quality.событий = {quality.get('событий')!r}, ждали 1")
    others = {p.get("id"): p.get("событий") for p in snap.get("части") or []
              if p.get("id") not in ("channel", "quality")}
    check(others and all(n == 0 for n in others.values()), "остальные части — с нулём событий",
          f"события у чужих частей: {others or 'частей нет'}")
    check((channel.get("исходы") or {}).get("ok") == 1 and (quality.get("исходы") or {}).get("refused") == 1,
          "исходы: channel ok 1, quality refused 1",
          f"исходы channel {channel.get('исходы')!r}, quality {quality.get('исходы')!r}")
    last = channel.get("последнее") or {}
    check(last.get("src") == "scripts/tg_send.sh" and last.get("событие") == "tg.sent",
          "channel.последнее — tg_send.sh tg.sent", f"channel.последнее = {last!r}")
    owner_edge = edge_index(snap, "channel", "внешний:владелец")
    gates_edge = edge_index(snap, "executors", "quality")
    by_src = {e.get("src"): e for e in snap.get("события") or []}
    tg_event = by_src.get("scripts/tg_send.sh") or {}
    check(owner_edge is not None and tg_event.get("ребро") == owner_edge,
          "событие tg_out несёт индекс ребра channel → внешний:владелец",
          f"ребро события tg_out {tg_event.get('ребро')!r}, ребро channel → внешний:владелец {owner_edge!r}")
    check(tg_event.get("часть") == "channel", "событие tg_out — в части channel",
          f"событие tg_out: часть {tg_event.get('часть')!r}")
    gate_event = by_src.get("scripts/gates.sh") or {}
    check(gates_edge is not None and gate_event.get("ребро") == gates_edge,
          "gate.verdict несёт индекс ребра executors → quality (строка таблицы)",
          f"ребро события gates {gate_event.get('ребро')!r}, ждали {gates_edge!r}")
    edges = snap.get("рёбра") or []
    lit = edges[owner_edge] if owner_edge is not None else {}
    check(lit.get("событий") == 1 and lit.get("последнее"), "ребро channel → внешний:владелец: событий 1",
          f"ребро channel → внешний:владелец: {lit!r}")
    unplaced_event = by_src.get("scripts/not-a-mechanism.sh") or {}
    check(unplaced_event.get("ребро") is None and "scripts/not-a-mechanism.sh" in by_src,
          "событие без строки таблицы — без ребра", f"событие без строки таблицы: {unplaced_event!r}")
    nowhere = snap.get("без_места") or {}
    check(nowhere.get("событий") == 1 and "scripts/not-a-mechanism.sh" in (nowhere.get("src") or []),
          "src не механизма — в без_места", f"без_места = {nowhere!r}")
    fixture_keys(snap, "события")


def case_k3_cursor() -> None:
    section("К-3: курсор читает только допись")
    stand = Stand("k3-big")
    stand.touch_all()
    big = stand.journal_path("inbox")
    old = now_local() - dt.timedelta(hours=2)
    with open(big, "wb") as fh:
        fh.truncate(50 * 1024 * 1024)
    stand.append("inbox", [], raw="\n" + json.dumps(v1_row("scripts/tg-dispatcher.sh", "inbox.received",
                                                         at=old), ensure_ascii=False) + "\n")
    first = stand.run("--один-снимок", "--отчёт")
    numbers = report_numbers(first)
    check(first.returncode == 0 and numbers is not None, "первый проход по журналу 50 МБ с отчётом",
          f"первый проход — {why(first)}; строка отчёта {numbers!r}")
    check(numbers and numbers["bytes"] <= 256 * 1024,
          "первый проход читает хвост ≤ 256 КиБ, а не журнал целиком",
          f"первый проход прочитал {numbers and numbers['bytes']} байт")
    stand.append("inbox", [v1_row("scripts/tg-dispatcher.sh", "inbox.received")])
    proc = stand.run("--один-снимок", "--отчёт")
    numbers = report_numbers(proc)
    check(numbers is not None, "строка «--отчёт» разобрана",
          f"нет строки «прочитано_байт=… событий=… журналов=… сборка_мс=…»: {why(proc)}")
    numbers = numbers or {}
    check(numbers.get("bytes", 1 << 30) <= SNAPSHOT_LIMIT, f"допись: прочитано {numbers.get('bytes')} ≤ 65536 байт",
          f"допись: прочитано {numbers.get('bytes')} байт — курсор не держит смещение")
    check(numbers.get("events") == 1, "допись: событий 1", f"допись: событий {numbers.get('events')!r}")
    snap = stand.snapshot() or {}
    check(len(snap.get("события") or []) == 1 and part(snap, "channel").get("событий") == 1,
          "в снимке 1 событие окна", f"в снимке событий {len(snap.get('события') or [])}, "
          f"channel {part(snap, 'channel').get('событий')!r}")

    section("К-3: недописанная строка ждёт своего «\\n»")
    half = json.dumps(v1_row("scripts/tg-dispatcher.sh", "inbox.received"), ensure_ascii=False)
    stand.append("inbox", [], raw=half)
    proc = stand.run("--один-снимок", "--отчёт")
    numbers = report_numbers(proc) or {}
    check(numbers.get("events") == 0, "строка без «\\n» не взята",
          f"строка без «\\n» взята: событий {numbers.get('events')!r}")
    stand.append("inbox", [], raw="\n")
    proc = stand.run("--один-снимок", "--отчёт")
    numbers = report_numbers(proc) or {}
    check(numbers.get("events") == 1, "дописанный «\\n» — строка взята один раз",
          f"после «\\n»: событий {numbers.get('events')!r}, ждали 1")

    section("К-3: подмена файла (новый ino) — строки нового не теряются")
    rot = Stand("k3-rotate")
    rot.touch_all()
    pad = "п" * 60
    rot.append("gates", [v1_row("scripts/gates.sh", "gate.verdict", at=old, data={"pad": pad})
                         for _ in range(3)])
    first = rot.run("--один-снимок")
    check(first.returncode == 0, "снимок до подмены: код 0", f"снимок до подмены — {why(first)}")
    size_before = rot.journal_path("gates").stat().st_size
    fresh = rot.root / "gates.new"
    with open(fresh, "w", encoding="utf-8") as fh:
        for index in range(5):
            row = v1_row("scripts/gates.sh", "gate.verdict", data={"pad": pad * 2, "n": index})
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(fresh, rot.journal_path("gates"))
    check(rot.journal_path("gates").stat().st_size > size_before,
          "новый файл длиннее старого смещения: судит именно смену ino",
          "условие стенда: новый файл короче старого — смена ino не отличена от усечения")
    proc = rot.run("--один-снимок")
    snap = rot.snapshot() or {}
    check(part(snap, "quality").get("событий") == 5, "после подмены — все 5 строк нового файла",
          f"после подмены quality.событий = {part(snap, 'quality').get('событий')!r}, ждали 5 ({why(proc)})")

    section("К-3: журналы kind: sample и time_kind: none не читаются")
    skip = Stand("k3-skip")
    skipped = [n for n, e in skip.journals.items()
               if e.get("kind") == "sample" or e.get("time_kind") in (None, "none")]
    check(len(skipped) == 2, f"стенд несёт sample и none: {skipped}",
          f"в боевом реестре не нашлось журнала sample или none: {skipped}")
    skip.touch_all()
    for name in skipped:
        skip.append(name, [v1_row("scripts/tg_send.sh", "tg.sent")])
    readable = len(skip.journals) - len(skipped)
    proc = skip.run("--один-снимок", "--отчёт")
    numbers = report_numbers(proc) or {}
    check(readable > 0 and numbers.get("journals") == readable,
          f"журналов={readable}: sample и none не в счёте",
          f"журналов={numbers.get('journals')!r}, ждали {readable} ({why(proc)})")
    snap = skip.snapshot() or {}
    check(snap and part(snap, "channel").get("событий") == 0,
          "строки журналов sample и none не попали в части",
          f"channel.событий = {part(snap, 'channel').get('событий')!r} — прочитан журнал без времени")


def case_k4_table() -> None:
    section("К-4: таблица рёбер судима")
    stand = Stand("k4")
    rows = table_rows()
    check(len(rows) == 12, f"боевая таблица разобрана: строк {len(rows)}",
          f"боевая таблица {LIVE_EDGES}: строк {len(rows)}, ждали 12 (§5.4)")
    # Журналы таблицы — из боевого реестра: стенд судит пары, а не отсутствие записей.
    live = live_journals().get("journals") or {}
    named = {r.get("журнал") for r in rows}
    missing = sorted(n for n in named if n not in live)
    check(rows and not missing, "все журналы боевой таблицы есть в боевом journals.yaml",
          f"журналов таблицы нет в боевом journals.yaml: {missing or 'таблицы нет'}")
    stand.journals.update({n: dict(live[n]) for n in named if n in live})
    stand.save_journals()
    proc = stand.run("--selftest")
    check(proc.returncode == 0 and "с источником: 11 из 21" in proc.stdout,
          "боевая таблица против реестра с №21: 0 претензий, «с источником: 11 из 21»",
          f"самотест боевой таблицы — {why(proc)}; вывод {proc.stdout.strip()[-200:]!r}")
    bad_rows = [
        {"журнал": "нет-такого-журнала", "события": ["x"], "от": "channel", "к": "map"},
        {"журнал": "gates", "события": ["gate.verdict"], "от": "map", "к": "release"},
        {"журнал": "inbox", "события": ["inbox.received"], "от": "внешний:чужой", "к": "channel"},
    ]
    bad_table = stand.root / "live-map-bad.yaml"
    Stand.write_yaml(bad_table, {"версия": 1, "рёбра": bad_rows})
    proc = stand.run("--selftest", LIVE_MAP_EDGES=bad_table)
    check(proc.returncode == 1, "три плохие строки — код 1",
          f"самотест плохой таблицы — {why(proc)}, ждали код 1")
    # Опечатка в части процесса молча увела бы долю в часть, которой в снимке нет.
    typo_table = stand.root / "live-map-typo.yaml"
    Stand.write_yaml(typo_table, {"версия": 1, "рёбра": rows,
                                  "процессы_частей": {"helpers": "executor", "claude-session": "executors"}})
    proc = stand.run("--selftest", LIVE_MAP_EDGES=typo_table)
    claims = [line for line in proc.stdout.splitlines() if line.startswith("претензия:")]
    check(proc.returncode == 1 and len(claims) == 1 and "executor" in claims[0] and "helpers" in claims[0],
          "процессы_частей: часть «executor» не из реестра — одна претензия, код 1",
          f"самотест таблицы с опечаткой части — {why(proc)}; претензии {claims}")
    lost = Stand("k4-lost")
    lost.with_module = False
    proc = lost.run("--selftest", HARNESS_ARCHITECTURE=lost.root / "нет.yaml")
    check(proc.returncode == 2, "реестра частей нет — код 2", f"без реестра — {why(proc)}, ждали код 2")

    os.environ.update({k: v for k, v in stand.env().items() if k != "PYTHONPATH"})
    sys.path.insert(0, str(stand.archdir))
    module = load_collector_module()
    check(module is not None and callable(getattr(module, "edge_claims", None)),
          "сборщик импортируется, edge_claims есть",
          f"нет edge_claims у {COLLECTOR}")
    if module is None or not callable(getattr(module, "edge_claims", None)):
        return
    registry = {"версия": 1, "части": STAND_PARTS}
    journals = stand.journals
    probes = [
        ("журнал не в journals.yaml", bad_rows[0]),
        ("пара вне выходов части map", bad_rows[1]),
        ("внешний:чужой → channel без входа", bad_rows[2]),
        ("внешний:копии → channel: вход есть у network, не у channel",
         {"журнал": "inbox", "события": ["inbox.received"], "от": "внешний:копии", "к": "channel"}),
        ("внешний:владелец → map без входа у map",
         {"журнал": "inbox", "события": ["inbox.received"], "от": "внешний:владелец", "к": "map"}),
        ("release → quality вне выходов", {"журнал": "gates", "события": ["gate.verdict"],
                                           "от": "release", "к": "quality"}),
    ]
    for title, row in probes:
        claims = module.edge_claims([row], registry, journals)
        check(len(claims) == 1, f"{title}: одна претензия", f"{title}: претензий {len(claims)}, ждали 1")
    claims = module.edge_claims(rows, registry, journals)
    check(rows and claims == [], "боевая таблица: претензий 0", f"боевая таблица: претензии {claims}")


def case_state6() -> None:
    section("Состояние 6: реестра частей нет")
    for title, with_module in (("модуля architecture нет", False), ("модуль есть, реестра нет", True)):
        stand = Stand("s6-" + ("mod" if with_module else "nomod"))
        stand.with_module = with_module
        stand.touch_all()
        stand.append("tg_out", [v1_row("scripts/tg_send.sh", "tg.sent")])
        proc = stand.run("--один-снимок", HARNESS_ARCHITECTURE=stand.root / "нет-architecture.yaml")
        snap = stand.snapshot() or {}
        check(proc.returncode == 0, f"{title}: код 0", f"{title}: {why(proc)}")
        check(snap.get("беда") == "нет architecture.yaml" and snap.get("части") == []
              and snap.get("рёбра") == [],
              f"{title}: беда «нет architecture.yaml», частей и рёбер нет",
              f"{title}: беда {snap.get('беда')!r}, части {snap.get('части')!r}, рёбра {snap.get('рёбра')!r}")
        check(isinstance(snap.get("машина"), dict) and isinstance(snap.get("исполнители"), list),
              f"{title}: нагрузка и исполнители сняты", f"{title}: машина {snap.get('машина')!r}")


# ═════════════════════════ Task 2: нагрузка, токены, исполнители ═════════════════════════

def case_k5_executors() -> None:
    section("К-5: иерархия исполнителей")
    stand = Stand("k5")
    stand.touch_all()
    now = now_local()
    launch = "scripts/hooks/agent_budget_guard.py"
    long_task = "hr.probe ключ " + SECRET + " " + "длинно " * 60

    def started(call, at, **data):
        return v1_row(launch, "agent.launch", at=at, task=data.pop("задача", "-"),
                      data={"вызов": call, "тип": "harness-worker", "модель": "sonnet", **data})

    stand.append("вызовы-агентов", [
        started("toolu_old", now - dt.timedelta(seconds=7300), задача="hr.old"),
        started("toolu_root", now - dt.timedelta(seconds=40), задача="hr.root"),
        started("toolu_mgr", now - dt.timedelta(seconds=35), задача="hr.probe-mapped"),
        started("toolu_worker", now - dt.timedelta(seconds=30), задача=long_task,
                родитель="toolu_mgr", управляющий="agent-mgr"),
        v1_row("scripts/hooks/agent_finish_log.py", "agent.finish", at=now - dt.timedelta(seconds=10),
               data={"вызов": "toolu_root"}),
    ])
    proc = stand.run("--один-снимок")
    snap = stand.snapshot() or {}
    executors = {e.get("вызов"): e for e in snap.get("исполнители") or []}
    check(set(executors) == {"toolu_mgr", "toolu_worker"},
          "в исполнителях двое: управляющий и исполнитель",
          f"исполнители {sorted(executors, key=str)} ({why(proc)}), ждали toolu_mgr и toolu_worker")
    check(executors and "toolu_root" not in executors, "корень с agent.finish исключён",
          f"корень с agent.finish: исполнители {sorted(executors, key=str)}")
    saved = stand.saved_state()
    check(saved and "toolu_root" not in (saved.get("launches") or {}) and "finished" not in saved,
          "окончание снимает запуск из состояния, списка окончаний нет",
          f"состояние: запуски {sorted(saved.get('launches') or {})}, ключи {sorted(saved)} — окончания копятся")
    check(executors and "toolu_old" not in executors, "запуск старше LIVE_MAP_AGENT_TTL_S отсутствует",
          f"запуск старше LIVE_MAP_AGENT_TTL_S: исполнители {sorted(executors, key=str)}")
    worker = executors.get("toolu_worker") or {}
    check(worker.get("родитель") == "toolu_mgr", "у исполнителя родитель — управляющий",
          f"родитель исполнителя {worker.get('родитель')!r}")
    check(worker.get("управляющий") == "agent-mgr", "управляющий взят из data",
          f"управляющий исполнителя {worker.get('управляющий')!r}")
    manager = executors.get("toolu_mgr") or {}
    check(manager.get("часть") == "map", "задача с элементом в dev-map.yaml — часть map",
          f"часть управляющего {manager.get('часть')!r}: задача → элемент не из dev-map.yaml")
    check(manager.get("тип") == "harness-worker" and manager.get("модель") == "sonnet",
          "тип и модель взяты из запуска", f"управляющий {manager!r}")
    text = json.dumps(snap, ensure_ascii=False)
    mask = getattr(events, "mask_secrets", None) or getattr(events, "_mask_secrets")
    check(SECRET not in mask(long_task), "условие стенда: образец ключа маскируется",
          "условие стенда: events не маскирует образец ключа — проба ничего не судит")
    check(snap and SECRET not in text, "ключ из имени задачи замаскирован в снимке",
          "ключ из имени задачи лёг в снимок, который уходит в браузер")
    check(worker and len(worker.get("задача") or "") <= TEXT_LIMIT,
          f"имя задачи обрезано до {TEXT_LIMIT}",
          f"имя задачи длиной {len(worker.get('задача') or '')} — не обрезано")
    fixture_keys(snap, "исполнители")


def write_tokens(stand: Stand, name: str, last: dt.datetime) -> tuple[int, dt.datetime]:
    """Строки раз в 7 мин за 84 мин: 60 мин не ложатся на границу ни одной строки."""
    rows, total = [], 0
    for step in range(12, -1, -1):
        moment = last - dt.timedelta(minutes=7 * step)
        row = {"ts": iso_z(moment), "session": "probe", "model": "probe",
               "in": 10 + step, "out": 100 * (step + 1), "cache_w": 1000 + step, "cache_r": 99999}
        rows.append(row)
        if 7 * step < 60:
            total += row["in"] + row["out"] + row["cache_w"]
    path = stand.log / f"{name}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return total, last


def tokens_source(snap: dict, name_prefix: str = "tokens") -> list[dict]:
    return [s for s in snap.get("источники") or [] if str(s.get("имя", "")).startswith(name_prefix)]


def case_k8_tokens() -> None:
    section("К-8: токены за час")
    template = token_template(live_journals())
    check(template is not None, f"образец записи токенов из боевого реестра: {template and template[0]}",
          "в боевом journals.yaml нет записи tokens-ГГГГ-ММ — стенд не построить")
    if template is None:
        return
    now = now_local()
    current, previous = month_name(now), previous_month_name(now)

    stand = Stand("k8-month")
    stand.journals[current] = dict(template[1], file=f"{current}.jsonl")
    stand.save_journals()
    stand.touch_all()
    expected, last = write_tokens(stand, current, now - dt.timedelta(minutes=2))
    proc = stand.run("--один-снимок")
    tokens = (stand.snapshot() or {}).get("токены") or {}
    check(tokens.get("за_час") == expected, f"за_час = {expected}: in+out+cache_w за 60 мин до последней",
          f"за_час = {tokens.get('за_час')!r}, ждали {expected} ({why(proc)})")
    try:
        until = dt.datetime.fromisoformat(str(tokens.get("до")).replace("Z", "+00:00"))
        close = abs((until - last).total_seconds()) < 1
    except ValueError:
        close = False
    check(close, "«до» — время последней строки", f"«до» = {tokens.get('до')!r}, ждали {iso(last)}")
    try:
        local = dt.datetime.fromisoformat(str(tokens.get("до"))).utcoffset() == now.utcoffset()
    except ValueError:
        local = False
    check(local, "«до» — местное время, как «снято»",
          f"«до» = {tokens.get('до')!r}: смещение не местное ({now.utcoffset()})")
    check(tokens.get("источник") == current, f"источник — {current}",
          f"источник токенов {tokens.get('источник')!r}, ждали {current}")

    stand = Stand("k8-pattern")
    stand.journals[previous] = dict(template[1], file=f"{previous}.jsonl")
    stand.save_journals()
    stand.touch_all()
    expected, _ = write_tokens(stand, current, now - dt.timedelta(minutes=2))
    proc = stand.run("--один-снимок")
    snap = stand.snapshot() or {}
    check(((snap.get("токены") or {}).get("за_час")) == expected,
          "записи месяца нет, файл есть — сумма по образцу прошлого месяца",
          f"по образцу: за_час = {(snap.get('токены') or {}).get('за_час')!r}, ждали {expected} ({why(proc)})")
    rows = [s for s in tokens_source(snap) if s.get("имя") == current]
    check(rows and f"по образцу {previous}" in str(rows[0].get("беда")),
          f"в источниках {current} с бедой «по образцу {previous}»",
          f"источники токенов {tokens_source(snap)}")

    stand = Stand("k8-none")
    stand.journals[previous] = dict(template[1], file=f"{previous}.jsonl")
    stand.save_journals()
    stand.touch_all()
    proc = stand.run("--один-снимок")
    snap = stand.snapshot()
    check(snap is not None and snap.get("токены") is None, "ни записи, ни файла — токены: null",
          f"без источника токены = {(snap or {}).get('токены')!r} ({why(proc)})")
    check(any(s.get("беда") for s in tokens_source(snap or {})), "в источниках беда токенов",
          f"беды токенов в источниках нет: {(snap or {}).get('источники')!r}")

    stand = Stand("k8-empty")
    stand.journals[current] = dict(template[1], file=f"{current}.jsonl")
    stand.save_journals()
    stand.touch_all()
    proc = stand.run("--один-снимок")
    snap = stand.snapshot()
    rows = [s for s in tokens_source(snap or {}) if s.get("имя") == current and s.get("беда")]
    check(snap is not None and snap.get("токены") is None and rows,
          "файл месяца пуст — токены: null и беда в источниках",
          f"пустой файл месяца: токены {(snap or {}).get('токены')!r}, источники {tokens_source(snap or {})} ({why(proc)})")


def case_k8_view() -> None:
    section("К-8: витрина процессов — возраст в источниках")
    stand = Stand("k8-view")
    stand.touch_all()
    taken = now_local() - dt.timedelta(seconds=480)
    view = stand.log / "process-dispatcher-view.json"
    # Четыре класса строк: механизм (build-board → map), только «файлы» (probe-sender →
    # tg_send.sh → channel), агенты по таблице live-map.yaml (helpers, claude-session,
    # claude-session-commands → executors; у claude-session записи в библиотеке нет вовсе)
    # и ничьё «прочее».
    view_rows = [{"id": "build-board", "доля_ёмкости_проц": 10.16}, {"id": "probe-sender", "доля_ёмкости_проц": 2.0},
                 {"id": "helpers", "доля_ёмкости_проц": 40.0}, {"id": "claude-session", "доля_ёмкости_проц": 13.98},
                 {"id": "claude-session-commands", "доля_ёмкости_проц": 20.0},
                 {"id": "прочее", "доля_ёмкости_проц": 3.86}]
    view.write_text(json.dumps({"снято": iso(taken), "окна": {"сейчас": {"строки": view_rows}}},
                               ensure_ascii=False), encoding="utf-8")
    os.utime(view, (taken.timestamp(), taken.timestamp()))
    proc = stand.run("--один-снимок")
    snap = stand.snapshot() or {}
    rows = [s for s in snap.get("источники") or [] if s.get("имя") == "process-dispatcher-view"]
    age = rows[0].get("возраст_с") if rows else None
    check(isinstance(age, (int, float)) and 470 <= age <= 600 and not rows[0].get("беда"),
          f"process-dispatcher-view: возраст_с {age}, без беды",
          f"источник витрины {rows!r} ({why(proc)}), ждали возраст_с ≈ 480")
    shares = {p.get("id"): p.get("доля_ядра") for p in snap.get("части") or []}
    expected = {"map": 0.1016, "channel": 0.02, "executors": 0.7398, "quality": 0.0}
    wrong = {k: shares.get(k) for k, v in expected.items()
             if not isinstance(shares.get(k), (int, float)) or abs(shares[k] - v) > 1e-6}
    check(shares and not wrong, "доля_ядра: map 0.1016, channel 0.02, executors 0.7398, quality 0",
          f"доля_ядра {shares!r}: расходятся {wrong!r}, ждали {expected!r}")
    load = sum(r["доля_ёмкости_проц"] for r in view_rows) / 100
    covered = sum(v for v in shares.values() if isinstance(v, (int, float)))
    check(shares and covered >= 0.8 * load, f"доли частей покрывают {covered / load:.0%} нагрузки витрины (≥ 80 %)",
          f"доли частей покрывают {covered:.4f} из {load:.4f} нагрузки витрины — меньше 80 %")

    stand = Stand("k8-noview")
    stand.touch_all()
    proc = stand.run("--один-снимок")
    snap = stand.snapshot()
    rows = [s for s in (snap or {}).get("источники") or [] if s.get("имя") == "process-dispatcher-view"]
    check(proc.returncode == 0 and snap is not None and rows and rows[0].get("беда"),
          "витрины нет — снимок записан, у источника беда",
          f"без витрины: {why(proc)}; источник {rows!r}")
    shares = [p.get("доля_ядра") for p in (snap or {}).get("части") or []]
    check(shares and all(v is None for v in shares), "витрины нет — доля_ядра у всех частей null",
          f"без витрины доля_ядра {shares!r}")


def case_k1_size() -> None:
    section("К-1(в): 1 000 событий в окне — снимок ≤ 64 КиБ")
    stand = Stand("k1-size")
    stand.touch_all()
    first = stand.run("--один-снимок")
    check(first.returncode == 0, "первый снимок: код 0", f"первый снимок — {why(first)}")
    start = now_local() - dt.timedelta(seconds=50)
    # Слова через пробел: сплошной прогон знаков маска секретов приняла бы за ключ.
    rows = [v1_row("scripts/tg_send.sh", f"e{index:04d} " + "событие пробы " * 13,
                   at=start + dt.timedelta(milliseconds=45 * index)) for index in range(1000)]
    stand.append("tg_out", rows)
    proc = stand.run("--один-снимок")
    size = stand.snapshot_path().stat().st_size if stand.snapshot_path().exists() else 0
    snap = stand.snapshot() or {}
    kept = [str(e.get("событие", ""))[:5] for e in snap.get("события") or []]
    check(0 < size <= SNAPSHOT_LIMIT, f"размер {size} ≤ {SNAPSHOT_LIMIT}",
          f"размер снимка {size} байт ({why(proc)})")
    check(0 < len(kept) <= EVENTS_LIMIT, f"события урезаны: {len(kept)} из 1000",
          f"событий в снимке {len(kept)}")
    newest = {f"e{index:04d}" for index in range(1000 - len(kept), 1000)}
    check(kept and set(kept) == newest, "срезаны самые старые", "в снимке остались не самые новые события")
    check(part(snap, "channel").get("событий") == 1000, "счёт части — все 1000 событий окна",
          f"channel.событий = {part(snap, 'channel').get('событий')!r}, ждали 1000")
    long_names = [e for e in snap.get("события") or [] if len(str(e.get("событие", ""))) > TEXT_LIMIT]
    check(snap and not long_names, f"строки событий ≤ {TEXT_LIMIT} знаков", "строка события длиннее 200")


def case_slots() -> None:
    section("Места тяжёлых прогонов и нагрузка машины")
    stand = Stand("slots")
    stand.touch_all()
    holder = subprocess.Popen(
        [sys.executable, "-c", "import fcntl,sys,time\nf=open(sys.argv[1],'a')\n"
         "fcntl.flock(f,fcntl.LOCK_EX)\nprint('держу',flush=True)\ntime.sleep(120)",
         str(stand.log / "heavy-slots" / "1.lock")], stdout=subprocess.PIPE, text=True)
    try:
        holder.stdout.readline()
        proc = stand.run("--один-снимок")
    finally:
        holder.kill()
        holder.wait()
    machine = (stand.snapshot() or {}).get("машина") or {}
    check(machine.get("места") == {"занято": 1, "всего": 4}, "места: занято 1 из 4",
          f"места {machine.get('места')!r} ({why(proc)}), ждали занято 1 всего 4")
    check(machine.get("ядер") == os.cpu_count() and isinstance(machine.get("load1"), (int, float)),
          "ядер и load1 сняты", f"машина {machine!r}")
    free = []
    for slot in range(2, 5):
        with open(stand.log / "heavy-slots" / f"{slot}.lock", "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                free.append(slot)
            except OSError:
                pass
    check(machine and free == [2, 3, 4], "сборщик не держит места после снимка",
          f"после снимка свободны {free}, машина {machine!r}")


# ═════════════════════════ Task 3: ритм, будильник, запуск ═════════════════════════

def case_k1_start() -> None:
    section("К-1(б): холодный старт на удвоенных боевых реестрах ≤ 3,0 с ЦП")
    if not (LIVE_MECHANISMS.is_file() and LIVE_DEVMAP.is_file()):
        path_bad(f"нет копий для стенда: {LIVE_MECHANISMS} или {LIVE_DEVMAP}")
        return
    stand = Stand("k1-start")
    # Боевые реестры дважды подряд (текстом: у списка механизмов — вдвое больше
    # записей, у карты повтор ключей разбирается до конца): одна копия 22.11 —
    # 870 и 919 КБ, и yaml.safe_load на них стоит 3,0 с ЦП, ровно у порога, так
    # что мутация К-1(б) то краснела, то нет. Двойной объём — запас на рост
    # реестров (ветка 19c5ba51 добавляет +712 строк механизмов) и ясный красный.
    for source, target in ((LIVE_MECHANISMS, stand.mechanisms), (LIVE_DEVMAP, stand.devmap)):
        text = source.read_text(encoding="utf-8")
        target.write_text(text + "\n" + text, encoding="utf-8")
    stand.touch_all()
    state = stand.state_file()
    for title, keep_state in (("без файла состояния", False), ("с файлом состояния", True)):
        # Судится время ЦП подпроцесса (user+sys), стена только печатается: при
        # load 30 на 8 ядрах стена черновика 22.11 была 3,4–7,7 с при чистом коде,
        # и проба краснела от соседей. yaml.safe_load вместо CSafeLoader стоит
        # ~3 с ЦП на реестрах 870 и 919 КБ (§5.3) — это видно и под нагрузкой.
        # Живая стена К-1(б) меряется в Task 8 (`/usr/bin/time`).
        if not keep_state:
            state.unlink(missing_ok=True)
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        started = time.perf_counter()
        proc = stand.run("--один-снимок")
        wall = time.perf_counter() - started
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
        check(proc.returncode == 0 and cpu <= 3.0, f"{title}: ЦП {cpu:.2f} с ≤ 3,0 с (стена {wall:.2f} с)",
              f"{title}: ЦП {cpu:.2f} с > 3,0 с или сбой (стена {wall:.2f} с; {why(proc)})")
    check(state.is_file(), "файл состояния после запусков есть", f"после запусков нет {state}")


def wait_snapshots(stand: Stand, deadline: float, count: int, seen: list) -> None:
    """Новый снимок — новый ino файла: запись атомарна, rename меняет ino каждый раз."""
    path = stand.snapshot_path()
    last_ino = seen[-1][1] if seen else None
    target = len(seen) + count
    while time.perf_counter() < deadline and len(seen) < target:
        try:
            ino = path.stat().st_ino
        except OSError:
            ino = None
        if ino is not None and ino != last_ino:
            last_ino = ino
            seen.append((time.perf_counter(), ino, stand.snapshot() or {}))
        time.sleep(0.05)


def case_k7_rhythm() -> None:
    section("К-7: ритм простоя, будильник зрителя, частый ритм")
    life = 16
    stand = Stand("k7", conf={"LIVE_MAP_IDLE_S": "4", "LIVE_MAP_PERIOD_S": "1"})
    stand.touch_all()
    if not COLLECTOR.is_file():
        path_bad(f"сборщика нет: {COLLECTOR} — ритм не судим")
        return
    daemon = subprocess.Popen([sys.executable, str(COLLECTOR)], env=stand.env(LIVE_MAP_DEADLINE_S=life),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    seen: list = []
    try:
        wait_snapshots(stand, time.perf_counter() + 14, 3, seen)
        idle = seen[:3]
        steps = [round(b[0] - a[0], 2) for a, b in zip(idle, idle[1:])]
        check(len(idle) == 3 and all(3.0 <= s <= 5.0 for s in steps)
              and all(s[2].get("период_с") == 4 for s in idle),
              f"без зрителя: шаг {steps} с ≈ 4±1, период_с 4",
              f"без зрителя: снимков {len(idle)}, шаги {steps}, период_с "
              f"{[s[2].get('период_с') for s in idle]}")
        time.sleep(0.3)
        touched = time.perf_counter()
        (stand.panel / "live-map.viewer").touch()
        wait_snapshots(stand, touched + 1.5, 1, seen)
        woke = len(seen) > len(idle)
        check(woke, f"будильник: снимок через {seen[-1][0] - touched:.2f} с после отметки зрителя"
              if woke else "", "будильник: за 1,5 с после отметки зрителя нового снимка нет")
        if woke:
            # Снимок будильника — не шаг частого ритма: под нагрузкой первый шаг после
            # него дольше (2,44 с при load 10–14). Судятся три шага между следующими.
            fast_from = len(seen)
            wait_snapshots(stand, time.perf_counter() + 6, 4, seen)
            fast = seen[fast_from:]
            steps = [round(b[0] - a[0], 2) for a, b in zip(fast, fast[1:])]
            check(len(fast) == 4 and all(0.5 <= s <= 1.5 for s in steps)
                  and all(s[2].get("период_с") == 1 for s in fast),
                  f"при зрителе: после снимка будильника три шага {steps} ≈ 1±0,5, период_с 1",
                  f"при зрителе: снимков после будильника {len(fast)}, шаги {steps}, период_с "
                  f"{[s[2].get('период_с') for s in fast]}")
        else:
            path_bad("частый ритм не судим: будильник не сработал")
        try:
            rc = daemon.wait(timeout=life + 5)
        except subprocess.TimeoutExpired:
            rc = None
        check(rc == 0, f"демон вышел сам к пределу {life} с, код 0",
              f"демон к пределу {life} + 5 с: код {rc!r}")
        check((stand.heartbeat / "live-map").exists(), "метка свежести live-map поставлена",
              f"нет метки свежести {stand.heartbeat / 'live-map'}")
    finally:
        if daemon.poll() is None:
            daemon.kill()
        daemon.wait()


def case_single_instance() -> None:
    section("Второй запуск при живом первом — выход 0 без снимка")
    stand = Stand("lock", conf={"LIVE_MAP_IDLE_S": "30", "LIVE_MAP_PERIOD_S": "30"})
    stand.touch_all()
    if not COLLECTOR.is_file():
        path_bad(f"сборщика нет: {COLLECTOR} — замок не судим")
        return
    first = subprocess.Popen([sys.executable, str(COLLECTOR)], env=stand.env(LIVE_MAP_DEADLINE_S=25),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        seen: list = []
        wait_snapshots(stand, time.perf_counter() + 10, 1, seen)
        check(seen, "первый демон снял снимок", "первый демон за 10 с снимка не снял")
        before = stand.snapshot_path().stat().st_ino if seen else None
        second = subprocess.Popen([sys.executable, str(COLLECTOR)], env=stand.env(LIVE_MAP_DEADLINE_S=25),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            rc = second.wait(timeout=8)
        except subprocess.TimeoutExpired:
            second.kill()
            second.wait()
            rc = None
        check(rc == 0, "второй запуск вышел сразу с кодом 0",
              f"второй запуск: код {rc!r} (None — жил дольше 8 с: замок не взят)")
        after = stand.snapshot_path().stat().st_ino if stand.snapshot_path().exists() else None
        check(before is not None and before == after, "второй запуск снимка не писал",
              "снимок переписан при живом первом демоне")
    finally:
        first.kill()
        first.wait()


def write_big_tokens(stand: Stand, name: str, last: dt.datetime, size: int) -> tuple[int, int]:
    """Журнал токенов ~size байт: давние строки, затем час строк до last. Час
    (3000 строк по ~330 Б) шире блока 256 КиБ: стыков блоков в нём не меньше трёх,
    и строка, разрезанная стыком, обязана дойти до суммы. → (сумма, байт часа)."""
    old = json.dumps({"ts": iso_z(last - dt.timedelta(hours=2)), "session": "probe", "model": "probe",
                      "in": 7, "out": 7, "cache_w": 7, "cache_r": 1}) + "\n"
    hour = [json.dumps({"ts": iso_z(last - dt.timedelta(seconds=1.15 * k)), "session": "probe",
                        "model": "probe", "pad": "п" * 120, "in": k % 7, "out": 2, "cache_w": 3,
                        "cache_r": 1}, ensure_ascii=False) + "\n"
            for k in range(2999, -1, -1)]
    with open(stand.log / f"{name}.jsonl", "w", encoding="utf-8") as fh:
        fh.write(old * (size // len(old)))
        fh.write("".join(hour))
    return sum(k % 7 + 5 for k in range(3000)), len("".join(hour).encode())


def case_k1_tokens() -> None:
    section("К-1(а): журнал токенов ~30 МБ — снимок с состоянием ≤ 300 мс ЦП")
    template = token_template(live_journals())
    if template is None:
        path_bad("в боевом journals.yaml нет записи tokens-ГГГГ-ММ — стенд не построить")
        return
    now = now_local()
    current = month_name(now)
    stand = Stand("k1-tokens")
    stand.journals[current] = dict(template[1], file=f"{current}.jsonl")
    stand.save_journals()
    stand.touch_all()
    last = now - dt.timedelta(minutes=2)
    expected, hour_bytes = write_big_tokens(stand, current, last, 30 * 1024 * 1024)
    size = (stand.log / f"{current}.jsonl").stat().st_size
    check(size >= 30 * 1024 * 1024 and hour_bytes >= 3 * 256 * 1024,
          f"условие стенда: журнал токенов {size} байт, час {hour_bytes} байт (≥ 3 блоков)",
          f"условие стенда: журнал токенов {size} байт, час {hour_bytes} — меньше 30 МиБ или 3 блоков")
    cold = stand.run("--один-снимок", "--отчёт")
    numbers = report_numbers(cold) or {}
    tokens = (stand.snapshot() or {}).get("токены") or {}
    check(tokens.get("за_час") == expected, f"холодный: за_час = {expected}",
          f"холодный: за_час = {tokens.get('за_час')!r}, ждали {expected} ({why(cold)})")
    check(0 < numbers.get("bytes", 0) <= hour_bytes + 2 * 256 * 1024,
          f"холодный: прочитано {numbers.get('bytes')} ≤ час + 2 блока — журнал токенов читается с конца блоками",
          f"холодный: прочитано {numbers.get('bytes')!r} байт — журнал токенов не читался с конца "
          f"(или его байты не в счётчике «--отчёт»)")
    stand.append(current, [json.dumps({"ts": iso_z(last + dt.timedelta(seconds=1)), "session": "probe",
                                       "model": "probe", "in": 10, "out": 20, "cache_w": 30, "cache_r": 1})])
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter()
    warm = stand.run("--один-снимок", "--отчёт")
    wall = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    process_cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    numbers = report_numbers(warm) or {}
    tokens = (stand.snapshot() or {}).get("токены") or {}
    check(tokens.get("за_час") == expected + 60, f"с состоянием: за_час = {expected + 60} (допись учтена)",
          f"с состоянием: за_час = {tokens.get('за_час')!r}, ждали {expected + 60} ({why(warm)})")
    check(numbers.get("cpu_ms") is not None and numbers["cpu_ms"] <= 300,
          f"с состоянием: снимок {numbers.get('cpu_ms')} мс ЦП ≤ 300 (сборка_мс {numbers.get('build_ms')}, "
          f"стена процесса {wall:.2f} с)",
          f"с состоянием: снимок {numbers.get('cpu_ms')!r} мс ЦП > 300 или нет «цп_мс=» в «--отчёт» "
          f"(сборка_мс {numbers.get('build_ms')!r}; {why(warm)})")
    check(numbers.get("bytes") is not None and numbers["bytes"] <= SNAPSHOT_LIMIT,
          f"с состоянием: прочитано {numbers.get('bytes')} ≤ 65536 байт — журнал токенов на курсоре",
          f"с состоянием: прочитано {numbers.get('bytes')!r} байт — журнал токенов перечитан")
    # Процесс целиком, а не только снимок: тяжёлое, унесённое в запуск, цп_мс не видит.
    # Стенд малый (реестры в строки), чистый запуск — десятые доли секунды ЦП.
    check(process_cpu <= 1.0, f"с состоянием: процесс целиком {process_cpu:.2f} с ЦП ≤ 1,0",
          f"с состоянием: процесс целиком {process_cpu:.2f} с ЦП > 1,0 — журнал токенов читается при запуске")


def case_state_dir() -> None:
    section("§5.1: состояние и замок — в $LOG_DIR/live-map-state, не под STATE_DIR (root)")
    stand = Stand("state-dir")
    stand.touch_all()
    stand.state.chmod(0o555)
    try:
        proc = stand.run("--один-снимок")
    finally:
        stand.state.chmod(0o755)
    check(proc.returncode == 0 and stand.snapshot() is not None and stand.state_file().is_file(),
          "STATE_DIR закрыт — снимок и состояние в $LOG_DIR/live-map-state",
          f"STATE_DIR закрыт: {why(proc)}; снимок {stand.snapshot_path().exists()}, "
          f"состояние {stand.state_file()} {stand.state_file().exists()}")
    mode = stand.own_state.stat().st_mode & 0o777 if stand.own_state.is_dir() else None
    check(mode == 0o700, "каталог состояния создан с правами 0700",
          f"каталог {stand.own_state}: права {oct(mode) if mode else 'нет'}")
    mode = stand.state_file().stat().st_mode & 0o777 if stand.state_file().exists() else None
    check(mode == 0o600, "права файла состояния 0600",
          f"права файла состояния {oct(mode) if mode else '—'}, ждали 0o600: в нём сырые строки журналов")
    leftovers = [p.name for p in stand.own_state.iterdir() if p.name.endswith(".tmp")] \
        if stand.own_state.is_dir() else []
    check(not leftovers, "временных файлов состояния не осталось", f"остались {leftovers}")
    daemon = stand.run(LIVE_MAP_DEADLINE_S=2)
    check(daemon.returncode == 0 and (stand.own_state / "live-map.lock").exists(),
          "демон берёт замок в $LOG_DIR/live-map-state и выходит с кодом 0", f"демон: {why(daemon)}")

    kept = Stand("state-dir-kept")
    kept.touch_all()
    kept.own_state.mkdir(mode=0o700)
    kept.log.chmod(0o555)
    try:
        proc = kept.run("--один-снимок")
    finally:
        kept.log.chmod(0o755)
    check(proc.returncode == 0 and kept.state_file().is_file(),
          "LOG_DIR закрыт, каталог состояния есть — состояние записано",
          f"LOG_DIR закрыт, каталог есть: {why(proc)}")

    closed = Stand("state-dir-closed")
    closed.touch_all()
    closed.log.chmod(0o555)
    try:
        check(not os.access(closed.log, os.W_OK), "условие стенда: LOG_DIR без права записи",
              "условие стенда: в LOG_DIR пишется (проба под root?) — случай не судит")
        proc = closed.run("--один-снимок")
    finally:
        closed.log.chmod(0o755)
    check(proc.returncode != 0 and str(closed.own_state) in (proc.stderr or ""),
          "LOG_DIR закрыт и каталога нет — отказ с путём каталога состояния",
          f"каталога нет: {why(proc)} — путь {closed.own_state} не назван")


def case_mask_fields() -> None:
    section("§5.5: строки данных в снимке маскируются и режутся")
    stand = Stand("mask")
    secret_src = f"scripts/{SECRET}.sh"
    stand.write_yaml(stand.mechanisms, {"механизмы": STAND_MECHANISMS + [
        {"файл": secret_src, "элемент": "channel.send"}]})
    stand.touch_all()
    first = stand.run("--один-снимок")
    check(first.returncode == 0, "первый снимок: код 0", f"первый снимок — {why(first)}")
    tail = " длинно" * 60
    loaded = f"x {SECRET}{tail}"
    # Исход v1 читалка берёт только из словаря исходов: ключа он не несёт.
    stand.append("tg_out", [v1_row(secret_src, "tg.sent")])
    stand.append("inbox", [v1_row(f"scripts/none-{SECRET}{tail}", "probe.unplaced")])
    stand.append("вызовы-агентов", [v1_row("scripts/hooks/agent_budget_guard.py", "agent.launch",
                                           task="hr.probe-mapped",
                                           data={key: loaded for key in ("вызов", "тип", "модель",
                                                                         "родитель", "управляющий")})])
    proc = stand.run("--один-снимок")
    snap = stand.snapshot() or {}
    check(SECRET not in events.mask_secrets(secret_src) and SECRET not in events.mask_secrets(loaded),
          "условие стенда: образцы маскируются", "условие стенда: маска не берёт образцы — случай не судит")
    check(part(snap, "channel").get("событий") == 1 and snap.get("исполнители")
          and (snap.get("без_места") or {}).get("src"),
          "событие части, без места и исполнитель — в снимке",
          f"стенд не дал полей: channel {part(snap, 'channel')!r}, исполнители {snap.get('исполнители')!r}, "
          f"без_места {snap.get('без_места')!r} ({why(proc)})")
    leaked = [text[:60] for text in all_strings(snap) if SECRET in text]
    check(snap and not leaked, "ни одна строка снимка не несёт ключа",
          f"ключ в снимке: {leaked}")
    longer = [text[:60] for text in all_strings(snap) if len(text) > TEXT_LIMIT]
    check(snap and not longer, f"ни одна строка снимка не длиннее {TEXT_LIMIT}",
          f"строки длиннее {TEXT_LIMIT}: {longer}")


def case_new_journal_tail() -> None:
    section("К-3: новый журнал при живом состоянии — хвост 256 КиБ, не с нуля")
    stand = Stand("new-journal")
    stand.touch_all()
    first = stand.run("--один-снимок")
    check(first.returncode == 0 and stand.state_file().is_file(), "состояние после первого снимка есть",
          f"первый снимок — {why(first)}")
    stand.journals["probe-late"] = dict(stand.journals["gates"], file="probe-late.jsonl")
    stand.save_journals()
    old = json.dumps(v1_row("scripts/gates.sh", "gate.verdict",
                            at=now_local() - dt.timedelta(hours=2)), ensure_ascii=False) + "\n"
    stand.journal_path("probe-late").write_text(old * (2 * 1024 * 1024 // len(old.encode())), encoding="utf-8")
    proc = stand.run("--один-снимок", "--отчёт")
    numbers = report_numbers(proc) or {}
    check(numbers.get("bytes") is not None and numbers["bytes"] <= 256 * 1024 + SNAPSHOT_LIMIT,
          f"журнал 2 МиБ без курсора: прочитано {numbers.get('bytes')} ≤ 256 КиБ + допись",
          f"журнал 2 МиБ без курсора: прочитано {numbers.get('bytes')!r} байт — читан с нуля ({why(proc)})")


def case_own_journal() -> None:
    section("Свой журнал запусков сборщика — не лента событий")
    stand = Stand("own-journal")
    entry = (live_journals().get("journals") or {}).get("live-map")
    check(entry is not None, "в боевом journals.yaml есть запись live-map",
          "в боевом journals.yaml нет записи live-map — стенд не построить")
    if entry is None:
        return
    stand.journals["live-map"] = dict(entry)
    stand.save_journals()
    stand.write_yaml(stand.mechanisms, {"механизмы": STAND_MECHANISMS + [
        {"файл": "scripts/live-map.py", "элемент": "channel.panel"}]})
    stand.touch_all()
    first = stand.run("--один-снимок")
    stand.append("live-map", [v1_row("scripts/live-map.py", "run.start")])
    proc = stand.run("--один-снимок")
    snap = stand.snapshot() or {}
    check(first.returncode == 0 and snap and part(snap, "channel").get("событий") == 0,
          "run.start сборщика не мигает в части channel",
          f"channel.событий = {part(snap, 'channel').get('событий')!r} ({why(proc)}): сборщик читает свой журнал")


def main() -> None:
    print(f"сборщик: {COLLECTOR}")
    if not COLLECTOR.is_file():
        print(f"  сборщика нет: {COLLECTOR} — пути ниже красные")
    case_k1_shape()
    case_k2_place()
    case_k3_cursor()
    case_k4_table()
    case_state6()
    case_k5_executors()
    case_k8_tokens()
    case_k8_view()
    case_k1_size()
    case_slots()
    case_k1_start()
    case_k7_rhythm()
    case_single_instance()
    case_k1_tokens()
    case_state_dir()
    case_mask_fields()
    case_new_journal_tail()
    case_own_journal()
    finish()


if __name__ == "__main__":
    main()
