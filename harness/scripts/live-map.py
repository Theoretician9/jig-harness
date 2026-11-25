#!/usr/bin/env python3
"""live-map.py — сборщик снимка живой карты процесса работы.

Зачем. Владелец видит в браузере, где в системе идут события, как ходят
данные между частями, где нагрузка и кто сейчас работает. Сборщик раз в
минуту (демон под кроном) пишет `$PANEL_STATE_DIR/live-map.json`; панель его
только отдаёт. Образец формата — scripts/fixtures/live-map/snapshot-v1.json.

Откуда что берётся (своих журналов событий и замеров сборщик не заводит; журнал
запусков live-map пишет events.run, и в ленту событий он не идёт):
- части и их контракты — модуль architecture (имя файла реестра живёт только
  там: второй судья был бы вторым источником правды);
- события — журналы из journals.yaml через events.iter_journal_events;
- место события — поле `элемент` паспорта механизма в mechanisms.yaml;
- какое событие какое ребро — таблица harness/config/live-map.yaml;
- исполнители — журнал вызовов-агентов, задача → элемент из dev-map.yaml;
- доля ядра части — витрина процессов, процесс → механизм по process-library.yaml.

Режимы: без доводов — демон; `--один-снимок [--отчёт]` — один снимок и выход;
`--selftest` — сверка таблицы рёбер с реестром и journals.yaml.
"""
import datetime as dt
import fcntl
import functools
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Настоящий модуль architecture лежит в scripts/lib; пробе он подменяется дверью
# HARNESS_ARCHITECTURE, а не другим модулем.
sys.path.insert(0, str(HERE / "lib"))
import yaml  # noqa: E402
import config  # noqa: E402
import events  # noqa: E402
import process_library  # noqa: E402
try:
    import architecture  # noqa: E402
except ImportError:
    architecture = None   # итерация архитектуры не влита — состояние 6 спеки

# Через CSafeLoader: mechanisms.yaml около 900 КБ, чистый Python разбирает его
# секундами, а старт обязан уложиться в три.
LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
CONF = HERE.parent / "harness" / "config"
FIRST_READ_TAIL = 256 * 1024     # первый запуск читает только хвост журнала
MAX_SNAPSHOT_BYTES = 65536
MAX_EVENTS = 200
TOKEN_JOURNAL = re.compile(r"tokens-\d{4}-\d{2}")
OWN_JOURNAL = "live-map"
STATE_NAME = "live-map-state"
HOUR = dt.timedelta(minutes=60)


@functools.lru_cache(maxsize=1)
def conf_file():
    return config.из_файла(config.путь_harness_conf())


def conf_value(key, default=None):
    return os.environ.get(key) or conf_file().get(key) or default


def setting(key, default):
    return int(conf_value(key, default))


def fail(text):
    # Не SystemExit с текстом: events.run гасит его в код, и причина пропала бы.
    print(f"live-map: {text}", file=sys.stderr)
    sys.exit(1)


def own_state_dir():
    """Состояние и замок — под LOG_DIR: STATE_DIR на живой машине принадлежит root,
    а LOG_DIR — agent (там же места пула heavy-slots), и копии не нужен шаг root.
    Уборка журналов чистит только файлы верхнего уровня; снесённый каталог стоит
    лишь повторного чтения хвостов."""
    path = Path(events.log_dir()) / STATE_NAME
    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except OSError as err:
        fail(f"каталог состояния {path} не создать ({err.strerror})")
    return path


def yaml_load(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.load(fh, Loader=LOADER)


def clip(text):
    """Данные событий идут в браузер: секреты маскируются, длина режется."""
    return events.mask_secrets(str(text))[:200] if text is not None else None


def parse_ts(text):
    return dt.datetime.fromisoformat(text)


def age_s(text, now):
    return (now - parse_ts(text)).total_seconds()


# --- реестр и таблица рёбер ---------------------------------------------------

def load_registry():
    """Реестр частей или None: нет модуля или файла — состояние 6 спеки."""
    if architecture is None:
        return None
    try:
        return architecture.load()
    except architecture.RegistryError:
        return None


def load_table(path):
    """Таблица live-map.yaml: рёбра и процессы, приписанные к части данными."""
    return (yaml_load(path) or {}) if Path(path).exists() else {}


def load_edges(path):
    return list(load_table(path).get("рёбра") or [])


def edges_path():
    return os.environ.get("LIVE_MAP_EDGES") or CONF / "live-map.yaml"


def edge_claims(edges, registry, journals):
    """Претензии таблицы к реестру: журнала нет или пары нет в контрактах."""
    parts = {p["id"]: p for p in registry.get("части") or []}
    claims = []
    for row in edges:
        if row.get("журнал") not in journals:
            claims.append(f"журнала {row.get('журнал')} нет")
            continue
        src, dst = row.get("от"), row.get("к")
        if str(src).startswith("внешний:"):
            declared = any(i.get("от") == src for i in (parts.get(dst) or {}).get("входы") or [])
        else:
            declared = any(o.get("кому") in (dst, "*") for o in (parts.get(src) or {}).get("выходы") or [])
        if not declared:
            claims.append(f"пары {src} → {dst} нет")
    return claims


def edge_key(edges, journal, event):
    # Первая подходящая строка: порядок таблицы — данные, его видно глазами.
    return next(([row["от"], row["к"]] for row in edges if row.get("журнал") == journal
                 and ("*" in row.get("события", []) or event in row.get("события", []))), None)


# --- чтение журналов ----------------------------------------------------------

def readable(entry):
    return entry.get("kind") not in ("sample", "registry") and entry.get("time_kind") not in (None, "none")


def read_complete(fh, offset):
    """Целые строки с offset → (строки, конец последней целой, прочитано байт).
    Недописанная строка остаётся до следующего раза."""
    fh.seek(offset)
    data = fh.read()
    cut = data.rfind(b"\n") + 1
    return data[:cut].decode("utf-8", "replace").splitlines(), cut, len(data)


def read_journal(name, entry, cursor):
    """Новые строки журнала с курсора (inode, смещение). Журнал без курсора — хвост
    256 КиБ: и в первый запуск, и когда запись добавили к уже большому файлу.
    Смена inode или усечение — чтение с нуля."""
    path = events.journal_file(entry)
    try:
        st = os.stat(path)
    except OSError:
        return None
    ino, offset = cursor.get(name) or (st.st_ino, max(0, st.st_size - FIRST_READ_TAIL))
    if ino != st.st_ino or offset > st.st_size:
        offset = 0
    with open(path, "rb") as fh:
        lines, cut, nbytes = read_complete(fh, offset)
    if name not in cursor and offset > 0:
        lines = lines[1:]      # хвост начался посреди строки
    cursor[name] = (st.st_ino, offset + cut)
    return lines, nbytes


def read_new(journals, cursor):
    found, size, count = [], 0, 0
    for name, entry in journals.items():
        # Токены считает отдельный отсчёт часа, не лента событий; свой журнал
        # запусков мигал бы в channel каждую минуту.
        if not readable(entry) or TOKEN_JOURNAL.fullmatch(name) or name == OWN_JOURNAL:
            continue
        got = read_journal(name, entry, cursor)
        if got is None:
            continue
        lines, nbytes = got
        count += 1
        size += nbytes
        found.extend((name, ev) for ev in events.iter_journal_events(name, entry, lines))
    return found, size, count


# --- токены и машина ----------------------------------------------------------

def month_entry(journals, name, sources):
    """Запись журнала токенов месяца; нет записи, но есть файл — по образцу
    последней записи семейства, чтобы в первый день месяца полоса не гасла."""
    if journals.get(name):
        return journals[name]
    family = sorted(n for n in journals if TOKEN_JOURNAL.fullmatch(n))
    if family and (Path(events.log_dir()) / f"{name}.jsonl").exists():
        sources.append({"имя": name, "беда": f"запись месяца по образцу {family[-1]}"})
        return dict(journals[family[-1]], file=f"{name}.jsonl")
    sources.append({"имя": name, "беда": "нет записи месяца или файла"})
    return None


def token_rows(name, entry, lines):
    """Строки журнала токенов → [время, сумма n_fields]: в состоянии лежит час, не месяц."""
    fields = entry.get("n_fields") or []
    return [[e.time.isoformat(), sum(int(e.data.get(f) or 0) for f in fields)]
            for e in events.iter_journal_events(name, entry, lines)]


def last_hour(rows):
    """Строки за 60 мин до последней, а не до «сейчас»: отставший журнал
    не должен показывать ноль вместо последнего известного расхода."""
    if not rows:
        return rows
    last = max(parse_ts(t) for t, _ in rows)
    return [r for r in rows if last - parse_ts(r[0]) < HOUR]


def read_hour_back(name, entry, fh, size):
    """Первый проход журнала токенов: месяц — десятки МБ, а нужен час. Читает с
    конца блоками по 256 КиБ, пока не встретит строку старше часа от последней.
    → (строки часа, конец последней целой строки, прочитано байт)."""
    pos = max(0, size - FIRST_READ_TAIL)
    fh.seek(pos)
    block = fh.read()
    nbytes, end = len(block), pos + block.rfind(b"\n") + 1
    block, rows = block[:end - pos], []
    while True:
        carry = b""
        if pos > 0:
            carry, _, block = block.partition(b"\n")    # первая строка блока — обрывок
        rows = token_rows(name, entry, block.decode("utf-8", "replace").splitlines()) + rows
        if pos == 0 or len(last_hour(rows)) < len(rows):
            return last_hour(rows), end, nbytes
        start = max(0, pos - FIRST_READ_TAIL)
        fh.seek(start)
        block = fh.read(pos - start) + carry
        nbytes, pos = nbytes + pos - start, start


def same_file(saved, name, st):
    return saved.get("name") == name and saved.get("ino") == st.st_ino and saved.get("offset", 0) <= st.st_size


def busy_slots(total):
    """Занятые места пула тяжёлых прогонов: flock, который держит чужой процесс."""
    busy = 0
    for k in range(1, total + 1):
        path = Path(events.log_dir()) / "heavy-slots" / f"{k}.lock"
        try:
            with open(path, "a") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fh, fcntl.LOCK_UN)
                except OSError:
                    busy += 1
        except OSError:
            pass
    return busy


def machine():
    with open("/proc/loadavg") as fh:
        load1 = float(fh.read().split()[0])
    total = setting("HEAVY_SLOTS", 4)
    return {"load1": load1, "ядер": os.cpu_count(), "места": {"занято": busy_slots(total), "всего": total}}


# --- карта задач и механизмов -------------------------------------------------

def devmap_elements():
    """Задача → элемент реестра из dev-map.yaml (доска board.json полей не несёт)."""
    out = {}

    def walk(node):
        if isinstance(node, dict):
            if node.get("id") and node.get("элемент"):
                out[node["id"]] = node["элемент"]
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    try:
        walk(yaml_load(events.devmap_path()))
    except OSError:
        pass
    return out


def part_of(element):
    # Правило «элемент → часть» живёт в модуле architecture; без модуля частей нет.
    return architecture.part_of(element) if element and architecture else None


def mechanism_parts():
    """Файл механизма → id части его `элемент`."""
    path = os.environ.get("HARNESS_USTROJSTVO") or str(CONF / "mechanisms.yaml")
    out = {}
    for row in (yaml_load(path) or {}).get("механизмы") or []:
        if isinstance(row, dict) and row.get("файл"):
            out[row["файл"]] = part_of(row.get("элемент"))
    return out


def process_parts(mech, assigned):
    """Процесс витрины → часть. Строка таблицы старше: у агентов (helpers,
    claude-session…) файла-механизма нет, а нагрузка их — главная. Иначе — первый
    файл записи process-library.json («механизмы», затем «файлы») с частью."""
    out = dict(assigned)
    for row in process_library.registry().get("процессы") or []:
        if row.get("id") in out:
            continue
        files = (row.get("механизмы") or []) + (row.get("файлы") or [])
        out[row.get("id")] = next((mech[f] for f in files if mech.get(f)), None)
    return out


# --- сборка снимка ------------------------------------------------------------

def edge_list(registry, table):
    """Рёбра реестра: каждый выход части плюс вход владельца в канал."""
    edges = [{"от": p["id"], "к": o["кому"], "что": clip(o.get("что"))}
             for p in registry["части"] for o in p.get("выходы") or []]
    edges.append({"от": "внешний:владелец", "к": "channel", "что": "сообщения"})
    have = {(r["от"], r["к"]) for r in table}
    for edge in edges:
        edge.update({"источник": (edge["от"], edge["к"]) in have, "событий": 0, "последнее": None})
    return edges


def part_list(registry, mech, shares):
    # Нет витрины — доля неизвестна (null); витрина есть, процессов части нет — 0.
    return [{"id": p["id"], "имя": p["имя"],
             "механизмов": sum(1 for v in mech.values() if v == p["id"]),
             "событий": 0, "исходы": {},
             "доля_ядра": round(shares.get(p["id"], 0.0), 4) if shares is not None else None,
             "последнее": None}
            for p in registry["части"]]


def external_list(registry):
    out = []
    for part in registry["части"]:
        names = [i.get("от") for i in part.get("входы") or []] + [o.get("кому") for o in part.get("выходы") or []]
        for name in names:
            if str(name).startswith("внешний:") and name not in [x["id"] for x in out]:
                out.append({"id": name, "имя": name.split(":")[1].capitalize()})
    return out


def place_events(window, mech, parts, edges, table):
    """Раскладка окна событий по частям и рёбрам; чужие — в «без места».
    Ребро ищется здесь, по окну: смена таблицы сразу видна и в старых событиях."""
    by_id = {p["id"]: p for p in parts}
    nowhere = {"событий": 0, "src": []}
    out = []
    for ev in sorted(window, key=lambda x: parse_ts(x["ts"])):
        pid = mech.get(ev["src"])
        key = edge_key(table, ev["журнал"], ev["событие"])
        idx = next((i for i, e in enumerate(edges) if key and [e["от"], e["к"]] == key), None)
        src, outcome = clip(ev["src"]), clip(ev["исход"])
        if pid in by_id:
            part = by_id[pid]
            part["событий"] += 1
            part["исходы"][outcome] = part["исходы"].get(outcome, 0) + 1
            part["последнее"] = {"ts": ev["ts"], "src": src, "событие": clip(ev["событие"])}
        else:
            nowhere["событий"] += 1
            if src not in nowhere["src"]:
                nowhere["src"].append(src)
        if idx is not None:
            edges[idx]["событий"] += 1
            edges[idx]["последнее"] = ev["ts"]
        out.append({"ts": ev["ts"], "часть": pid if pid in by_id else None, "src": src,
                    "событие": clip(ev["событие"]), "исход": outcome, "ребро": idx})
    return out, nowhere


def running_agents(launches, task_elements):
    out = []
    for call, launch in launches.items():
        data = launch["data"] or {}
        element = task_elements.get(launch["task"])
        out.append({"вызов": clip(call), "тип": clip(data.get("тип")), "модель": clip(data.get("модель")),
                    "задача": clip(launch["task"]), "часть": part_of(element),
                    "родитель": clip(data.get("родитель")), "управляющий": clip(data.get("управляющий")),
                    "с": launch["ts"]})
    return out


def dispatcher_view(now, procs):
    """Доли ядра частей и строка источника. Доля — `доля_ёмкости_проц` строки
    окна «сейчас» (процент всей машины), сложенная по частям процессов."""
    path = Path(events.log_dir()) / "process-dispatcher-view.json"
    try:
        shot = json.loads(path.read_text())
        age = int(age_s(shot["снято"], now))
        rows = shot["окна"]["сейчас"]["строки"]
    except (OSError, ValueError, KeyError, TypeError):
        return None, {"имя": "process-dispatcher-view", "возраст_с": None, "беда": "витрины нет"}
    shares = {}
    for row in rows:
        owner = procs.get(row.get("id"))
        if owner:
            shares[owner] = shares.get(owner, 0.0) + float(row.get("доля_ёмкости_проц") or 0) / 100
    return shares, {"имя": "process-dispatcher-view", "возраст_с": age, "беда": None}


def fit_size(snapshot):
    """Снимок уходит каждые 5 с: лишнее обрезается с давних событий."""
    while len(json.dumps(snapshot, ensure_ascii=False).encode()) > MAX_SNAPSHOT_BYTES and snapshot["события"]:
        snapshot["события"] = snapshot["события"][10:]


def write_atomic(path, text, mode):
    """Через временный файл и rename: читатель не увидит половину файла. Имя с pid:
    ручной снимок при живом демоне не пишет в его временный файл."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, mode)
    os.write(fd, text.encode())
    os.close(fd)
    os.replace(tmp, path)


class Collector:
    def __init__(self):
        self.registry = load_registry()
        self.journals = events.load_registry().get("journals") or {}
        self.mech = mechanism_parts()
        table = load_table(edges_path())
        self.table = list(table.get("рёбра") or [])
        self.procs = process_parts(self.mech, table.get("процессы_частей") or {})
        self.tasks = devmap_elements()
        self.state_path = own_state_dir() / "live-map.state.json"
        self.load_state()

    def load_state(self):
        try:
            state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            state = {}
        self.cursor = {k: tuple(v) for k, v in (state.get("cursor") or {}).items()}
        self.window = state.get("window") or []
        self.launches = state.get("launches") or {}
        self.token_state = state.get("tokens") or {}

    def save_state(self):
        # 0600: в состоянии сырые строки журналов, в том числе журналов 0600.
        state = {"cursor": self.cursor, "window": self.window, "launches": self.launches,
                 "tokens": self.token_state}
        write_atomic(self.state_path, json.dumps(state, ensure_ascii=False), 0o600)

    def take(self, found):
        """Новые события — в окно; запуски и окончания агентов — в учёт исполнителей."""
        for name, ev in found:
            if name == "вызовы-агентов":
                self.track_agent(ev)
            self.window.append({"ts": ev.time.isoformat(), "src": ev.src, "событие": ev.event,
                                "исход": ev.outcome, "журнал": name})

    def track_agent(self, ev):
        call = (ev.data or {}).get("вызов")
        if ev.event == "agent.launch":
            self.launches[call] = {"ts": ev.time.isoformat(), "task": ev.task, "data": ev.data}
        if ev.event == "agent.finish":
            self.launches.pop(call, None)

    def tokens(self, now, sources):
        """Токены за час: журнал месяца на своём курсоре, строки часа — в состоянии.
        → (поле снимка, прочитано байт)."""
        name = now.strftime("tokens-%Y-%m")
        entry = month_entry(self.journals, name, sources)
        if not entry:
            return None, 0
        try:
            st = os.stat(events.journal_file(entry))
        except OSError:
            sources.append({"имя": name, "беда": "файла нет"})
            return None, 0
        saved = self.token_state
        with open(events.journal_file(entry), "rb") as fh:
            if same_file(saved, name, st):
                lines, cut, nbytes = read_complete(fh, saved["offset"])
                rows, end = last_hour(saved["rows"] + token_rows(name, entry, lines)), saved["offset"] + cut
            else:
                rows, end, nbytes = read_hour_back(name, entry, fh, st.st_size)
        self.token_state = {"name": name, "ino": st.st_ino, "offset": end, "rows": rows}
        if not rows:
            sources.append({"имя": name, "беда": "строк нет"})
            return None, nbytes
        last = max(parse_ts(t) for t, _ in rows).astimezone()
        total = sum(n for _, n in rows)
        return {"за_час": total, "до": last.isoformat(timespec="seconds"), "источник": name}, nbytes

    def expire(self, now):
        win = setting("LIVE_MAP_WINDOW_S", 60)
        ttl = setting("LIVE_MAP_AGENT_TTL_S", 7200)
        self.window = [e for e in self.window if age_s(e["ts"], now) <= win]
        # Запуск без окончания старше TTL — потерянный агент, не вечный работяга.
        self.launches = {c: l for c, l in self.launches.items() if age_s(l["ts"], now) <= ttl}
        return win

    def build(self, period, now, sources, token_field):
        win = self.expire(now)
        snap = {"v": 1, "снято": now.isoformat(timespec="seconds"), "период_с": period, "окно_с": win,
                "сборка_мс": 0, "машина": machine(), "токены": token_field}
        shares, view_row = dispatcher_view(now, self.procs)
        parts, edges, external = [], [], []
        if self.registry:
            parts = part_list(self.registry, self.mech, shares)
            edges = edge_list(self.registry, self.table)
            external = external_list(self.registry)
        placed, nowhere = place_events(self.window, self.mech, parts, edges, self.table)
        sources.append(view_row)
        snap.update({"части": parts, "внешние": external, "рёбра": edges, "события": placed[-MAX_EVENTS:],
                     "исполнители": running_agents(self.launches, self.tasks),
                     "без_места": nowhere, "источники": sources})
        if not self.registry:
            snap["беда"] = "нет architecture.yaml"
        return snap

    def snap(self, period, report=False):
        started, cpu = time.perf_counter(), time.process_time()
        now = dt.datetime.now().astimezone()
        found, size, count = read_new(self.journals, self.cursor)
        self.take(found)
        sources = []
        token_field, token_bytes = self.tokens(now, sources)
        snapshot = self.build(period, now, sources, token_field)
        fit_size(snapshot)
        snapshot["сборка_мс"] = int((time.perf_counter() - started) * 1000)
        write_atomic(Path(config.конфиг()["PANEL_STATE_DIR"]) / "live-map.json",
                     json.dumps(snapshot, ensure_ascii=False), 0o640)
        self.save_state()
        if report:
            # цп_мс — процессорное время снимка: стена под load 8–19 судит соседей.
            print(f"прочитано_байт={size + token_bytes} событий={len(found)} журналов={count} "
                  f"сборка_мс={snapshot['сборка_мс']} цп_мс={int((time.process_time() - cpu) * 1000)}")


# --- демон --------------------------------------------------------------------

def viewer_mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def life_end(start):
    """Демон живёт до 58-й секунды минуты: крон запустит следующего."""
    limit = os.environ.get("LIVE_MAP_DEADLINE_S")
    return start + int(limit) if limit else start - (start % 60) + 58


def daemon():
    lock = open(own_state_dir() / "live-map.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0       # живой сборщик уже есть
    end = life_end(time.time())
    collector = Collector()
    viewer = Path(config.конфиг()["PANEL_STATE_DIR"]) / "live-map.viewer"
    idle, fast, hold = setting("LIVE_MAP_IDLE_S", 60), setting("LIVE_MAP_PERIOD_S", 5), setting("LIVE_MAP_VIEWER_S", 120)
    mode, last = idle, 0
    while time.time() < end:
        watching = time.time() - viewer_mtime(viewer) <= hold
        due = time.time() - last >= mode
        # Будильник: зритель пришёл при медленном режиме — снимок сразу, не через минуту.
        alarm = watching and mode == idle and viewer_mtime(viewer) > last
        if due or alarm:
            mode = fast if watching else idle
            collector.snap(mode)
            last = time.time()
        time.sleep(0.2)
    return 0


def selftest():
    registry = load_registry()
    if not registry:
        print("реестра нет")
        return 2
    doc = load_table(edges_path())
    table = list(doc.get("рёбра") or [])
    claims = edge_claims(table, registry, events.load_registry().get("journals") or {})
    # Опечатка в части процесса молча увела бы долю в часть, которой в снимке нет.
    parts = {p["id"] for p in registry["части"]}
    claims += [f"части {part} процесса {proc} нет в реестре"
               for proc, part in (doc.get("процессы_частей") or {}).items() if part not in parts]
    for claim in claims:
        print("претензия:", claim)
    exits = [(p["id"], o["кому"]) for p in registry["части"] for o in p.get("выходы") or []]
    have = {(r["от"], r["к"]) for r in table}
    print(f"с источником: {sum(1 for e in exits if e in have)} из {len(exits)}")
    return 1 if claims else 0


def main(argv):
    if "--selftest" in argv:
        return selftest()
    if "--один-снимок" in argv:
        Collector().snap(setting("LIVE_MAP_IDLE_S", 60), "--отчёт" in argv)
        return 0
    return events.run("scripts/live-map.py", daemon, journal="live-map", name="live-map", heartbeat="live-map")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
