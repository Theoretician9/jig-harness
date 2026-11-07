#!/usr/bin/env python3
"""Статус владельцу, собранный КОДОМ: по ЗАДАЧАМ, а под каждой — кто делал и какие этапы.

Слово владельца 06.11 (память/owner-orders-06-11-verbatim.md): дерево помощников с ярлыками
запусков непонятно, нужны задачи, а под ними — кто делал и какие стадии. Форма:

    ✅ СДЕЛАНО
    1. <заголовок задачи>
       делали: <подпись> (<роль>), <подпись> (<роль> и <роль>)
       этапы: тест ✔ код ✔ проверка ✔ мутации ✔ коммит ✔
    прочее N                  (помощники без метки задачи)
    📝 коммитов N, последний: …
    🔄 ДЕЛАЕТСЯ               (пусто — «пока ничего»)
    2. <заголовок задачи>
       делают: <подпись> (<роль>)
       этап: <этап последнего идущего запуска>
    ⚠ ПОТЕРЯН                 (только если есть потерянные запуски; иначе блока нет)
    • <описание>, с HH:MM      (не больше 3, остаток — «и ещё N»)
    ⏭ ДАЛЬШЕ
    3. <название карточки очереди>     (нумерация сквозная)

Задача — id из метки «Часть: <id>#n» в описании запуска; исполнитель без метки наследует задачу
родителя по дереву запусков; запуск без метки и без родителя с задачей, но с русским описанием, —
своя задача (заголовок — описание; ярлык «Оркестратор:» с двоеточием отрезается, без двоеточия остаётся); «прочее» — остальные. Запуски с одной
задачей — в одном блоке (есть идущий не потерянный запуск — ДЕЛАЕТСЯ, иначе СДЕЛАНО). Роль запуска:
harness-lead — «вёл»; описание начинается со слова «ревью»/«review» — «проверка» (помощник,
правящий код по итогам ревью, — «код», хотя хук и ставит ему флаг «ревью»); слова тест/проба/test/probe в
описании — «тесты»; harness-worker — «код»; иное — «помощник».

    status-report.py [--журнал-вызовов F] [--карта F] [--очередь F] [--модели F]
                     [--сессия ID] [--с ISO-время]    (сессия по умолчанию пуста: весь журнал установки)
    status-report.py --отметить ISO-время   (пишет метку $LOG_DIR/status-report.last и выходит)
Конфиг (harness.conf): STATUS_STRUCT_MAX_CHARS — потолок всего отчёта (нет ключа — 1400);
сверх потолка задачи сворачиваются с конца блока в «ещё N задач»,
STATUS_MODEL_LABELS — «модель=подпись;…»,
STATUS_DONE_SINCE_LAST — «1»: СДЕЛАНО судится по концу работы позже метки прошлого доставленного
статуса (явный --с главнее метки; метки нет — окно 12 ч); «0»/нет ключа — окно,
STATUS_LOST_HOURS — часы тишины, после которых запуск без agent.finish потерян (нет ключа или мусор — 2).
Потерянный: самая поздняя запись его жизни (свой launch, запись с data.вызов = его вызов или
data.родитель = его вызов, launch и finish потомков любой глубины) старше порога. Он не в ДЕЛАЕТСЯ,
а в блоке «⚠ ПОТЕРЯН»; задача, где остались только потерянные запуски и законченные, — законченная.
Этапы берутся из $PROJECT_DIR/.tasks/<id>/ (журнал.md, harness-code-review.md, мутации.log) и
из git (коммиты со строкой «Dev-Map: <id>»). Вывод в stdout, код 0.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import config  # noqa: E402

LINE_MAX = 80
MIN_WIDTH = 40
DEFAULT_WINDOW = timedelta(hours=12)
NOTHING = "пока ничего"
NOTHING_NEW = "пока нового нет"
# Простой виден как простой: с какого часа тихо и когда кончил последний помощник журнала.
IDLE_SINCE = "с {since} нового нет; последний помощник кончил {last}"
COMMITS_LINE = "📝 коммитов {count}, последний: {subject}"
# Правки памяти с панели делает владелец руками, это не работа смены.
PANEL_COMMIT_PREFIX = "Правка памяти с панели"
GIT_TIMEOUT = 30
QUEUE_EMPTY = "очередь пуста"
QUEUE_UNAVAILABLE = "очередь недоступна"
NEXT_MAX = 3
DEFAULT_LOST_HOURS = 2
LOST_MAX = 3
# Блок потерянных: по строке на запуск, остаток одной строкой.
LOST_HEADER = "⚠ ПОТЕРЯН"
LOST_LINE = "• {title}, {model}, с {clock}"
LOST_UNNAMED = "работа без названия"
LOST_MORE = "и ещё {count}"
MARK_NAME = "status-report.last"
# Что не влезло в потолок: сборка пишет PENDING, --отметить после доставки переводит его в CARRY.
# Свёрнутая сделанная задача так приходит в следующем статусе, хотя метка уже ушла вперёд.
PENDING_NAME = "status-report.pending"
CARRY_NAME = "status-report.carry"
SYNTHETIC_PREFIX = "дело:"
# Карточка в этих статусах — работа сделана: оборванный помощник задачи уже не потерян.
FINISHED_CARD_STATUSES = ("test", "done", "принято")
H_DONE, H_DOING, H_NEXT = config.STATUS_HEADERS

# Этап — слово для владельца; для идущего запуска он выводится из его роли.
STAGE_OF_ROLE = {"вёл": "ведение задачи", "тесты": "тест", "код": "код",
                 "проверка": "проверка", "помощник": "работа"}
DEFAULT_TITLE = "работа"

TASK_MARK = re.compile(r"Часть:\s*((?:hr|pr)\.[\w.-]*\w)")
CARD_ID = re.compile(r"\b(?:hr|pr)\.[\w.-]*\w(?:#\d+)?")
DANGLING_TAIL = re.compile(r"(?:\s+(?:для|по|в|на|из|к|с|и|от|за))+\s*$")
CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
LATIN_TOKEN = re.compile(r"\S*[A-Za-z]\S*")
# Порядок ролей в «делали/делают»: вёл → тесты → код → проверка.
ROLE_ORDER = {"вёл": 0, "тесты": 1, "код": 2, "проверка": 3, "помощник": 4}
# «ревью» только в начале описания: слово посреди («правки по ревью») отмечает того, кто правит код.
TEST_WORD = re.compile(r"\b(?:тест\w*|проб(?:а|ы|у|ой|е)|tests?|probes?)\b|test_", re.IGNORECASE)
REVIEW_START = re.compile(r"(?:ревью|review)\b", re.IGNORECASE)


class Launch:
    """Запуск помощника из журнала; задача — id из метки «Часть:», пусто — помощник без метки."""

    def __init__(self, row: dict, data: dict) -> None:
        raw = str(data.get("описание") or "")
        # Метка стоит в тексте заказа; хук кладёт её в data.часть, в описании её бывает только в старых строках.
        mark = TASK_MARK.search(f"{data.get('часть') or ''} {raw}")
        self.task = mark.group(1) if mark else ""
        self.call = data["вызов"]
        self.parent = data.get("родитель") or ""
        self.ts = parse_time(row.get("ts"))
        self.desc = clean_desc(raw)
        self.kind = data.get("тип")
        self.model = str(data.get("модель") or "")
        self.finished_at = None
        self.fresh = True
        self.key = ""      # ключ задачи: id из метки, «дело:<вызов>» из русского описания, пусто — «прочее»

    @property
    def done(self) -> bool:
        return self.finished_at is not None

    @property
    def role(self) -> str:
        if self.kind == "harness-lead":
            return "вёл"
        if REVIEW_START.match(self.desc):
            return "проверка"
        if TEST_WORD.search(self.desc):
            return "тесты"
        return "код" if self.kind == "harness-worker" else "помощник"


class Task(NamedTuple):
    key: str
    members: list      # запуски по времени, без потерянных
    running: list      # из них идущие и свежие


class Entry(NamedTuple):
    key: str
    title: str
    notes: list


def parse_time(value) -> datetime:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def clean_desc(text: str) -> str:
    """Без «Часть:», id карточек и ярлыка роли («Оркестратор:», старого «Управляющий:») — остальное остаётся."""
    text = CARD_ID.sub("", text).replace("Часть:", "")
    text = re.sub(r"^\s*(?:Оркестратор|Управляющий):\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip(" :,-")
    return DANGLING_TAIL.sub("", text).strip() or DEFAULT_TITLE


def russian_only(text: str) -> str:
    """Описание без латинских слов: английские ярлыки запусков владельцу ничего не говорят."""
    text = re.sub(r"\s+", " ", LATIN_TOKEN.sub("", text)).strip(" :,-")
    return DANGLING_TAIL.sub("", text).strip()


def parse_labels(raw: str) -> dict:
    pairs = (item.split("=", 1) for item in raw.split(";") if "=" in item)
    return {k.strip(): v.strip() for k, v in pairs}


def label_of(labels: dict, model: str) -> str:
    return labels.get(model) or (model.capitalize() if model else "модель не названа")


def read_rows(path: Path):
    """Строки журнала; битое и чужое пропускается молча."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


class Cut(NamedTuple):
    """Что считать свежим: since — окно по запуску; mark — метка прошлого статуса (судит конец)."""
    since: datetime
    mark: datetime | None


def lost_threshold(conf: dict) -> timedelta:
    """Порог потери из STATUS_LOST_HOURS; пусто, мусор (nan, inf, 1e20) и неположительное — 2 часа."""
    raw = str(conf.get("STATUS_LOST_HOURS") or "").strip()
    try:
        return timedelta(hours=float(raw)) if float(raw) > 0 else timedelta(hours=DEFAULT_LOST_HOURS)
    except (ValueError, OverflowError):
        return timedelta(hours=DEFAULT_LOST_HOURS)


def last_alive(rows: list) -> dict:
    """Вызов → самая поздняя запись его жизни: свои записи, записи с ним в родителях и всех потомков."""
    parents, seen = {}, []
    for row in rows:
        data = row.get("data")
        if not isinstance(data, dict) or not data.get("вызов"):
            continue
        if row.get("event") == "agent.launch":
            parents[data["вызов"]] = data.get("родитель") or ""
        seen.append((data["вызов"], data.get("родитель") or "", parse_time(row.get("ts"))))
    alive = {}
    for call, parent, moment in seen:
        # Предки идут по цепочке launch; счётчик защищает от петли в битом журнале.
        chain, hops = [call, parent], 0
        step = call
        while step in parents and hops < len(parents):
            step = parents[step]
            chain.append(step)
            hops += 1
        for name in filter(None, chain):
            alive[name] = max(alive.get(name, moment), moment)
    return alive


def real_end(finishes: list, resumes: list) -> datetime | None:
    """Первый конец после последнего возобновления (не возобновляли — первый конец вообще):
    повторный finish без resume — не новый конец. Нет такого конца — запуск идёт."""
    reopened = max(resumes, default=datetime.min.replace(tzinfo=timezone.utc))
    return min((f for f in finishes if f >= reopened), default=None)


def is_fresh(launch: Launch, cut: Cut) -> bool:
    """Идущий свеж всегда (живость окном не судится); законченный — если конец позже метки или начала окна."""
    return not launch.done or launch.finished_at > (cut.mark or cut.since)


def parse_launches(rows, session: str, cut: Cut, lost_after: timedelta) -> dict:
    """Вызов → запуск: удачные запуски нужной сессии. Окно к разбору не применяется: родитель вне окна
    нужен детям для наследования задачи. Возобновление действует, пока оно моложе порога потери или после
    него была работа: хук пишет его до выполнения SendMessage, и соседний хук мог поручение отменить."""
    launches, finishes, resumes = {}, {}, {}
    worked = last_alive([r for r in rows if r.get("event") != "agent.resume"])
    now = datetime.now(timezone.utc)
    for row in rows:
        data = row.get("data")
        if not isinstance(data, dict) or not data.get("вызов"):
            continue
        moment = parse_time(row.get("ts"))
        if row.get("event") == "agent.finish":
            finishes.setdefault(data["вызов"], []).append(moment)
        elif row.get("event") == "agent.resume":
            resumes.setdefault(data["вызов"], []).append(moment)
        elif (row.get("event") == "agent.launch" and row.get("outcome") == "ok"
              and (not session or row.get("session") == session)):
            launches[data["вызов"]] = Launch(row, data)
    for call, launch in launches.items():
        effective = [r for r in resumes.get(call, [])
                     if now - r <= lost_after or worked.get(call, r) > r]
        launch.finished_at = real_end(finishes.get(call, []), effective)
        launch.fresh = is_fresh(launch, cut)
    return launches


def split_lost(launches: dict, rows: list, cut: Cut, lost_after: timedelta) -> tuple:
    """(потерянные для показа, их вызовы): без конца и без жизни дольше порога. С меткой потерянный
    показывается, если запущен в окне или жил после метки, иначе не показывается нигде."""
    alive = last_alive(rows)
    now = datetime.now(timezone.utc)
    gone = {c for c, n in launches.items() if not n.done and now - alive.get(c, n.ts) > lost_after}
    shown = [launches[c] for c in gone
             if launches[c].ts >= cut.since or (cut.mark is not None and alive.get(c, launches[c].ts) > cut.mark)]
    return sorted(shown, key=lambda n: (n.ts, n.call)), gone


def task_key(launch: Launch, launches: dict) -> str:
    """Метка «Часть:» ближайшего предка (или своя); без меток — самый верхний запуск цепочки с русским
    описанием; иначе пусто. Счётчик шагов защищает от петли в битом журнале."""
    chain = [launch]
    while len(chain) <= len(launches) and chain[-1].parent in launches:
        chain.append(launches[chain[-1].parent])
    marked = next((n.task for n in chain if n.task), "")
    return marked or next((f"{SYNTHETIC_PREFIX}{n.call}" for n in reversed(chain) if CYRILLIC.search(n.desc)), "")


def assign_keys(launches: dict) -> None:
    for launch in launches.values():
        launch.key = task_key(launch, launches)


def container_keys(launches: dict) -> dict:
    """Вызов → ключи задач его детей, если сам он без метки, а дети с меткой: он только ведёт эти задачи."""
    keys = {}
    for child in launches.values():
        parent = launches.get(child.parent)
        if child.task and parent is not None and not parent.task:
            keys.setdefault(parent.call, set()).add(child.key)
    return keys


def group_by_task(launches: dict, containers: dict) -> tuple:
    """(задачи: ключ → запуски по времени, запуски без задачи). Потерянные тоже тут — название берётся
    со всех; контейнеры не тут — их присоединяет attach_containers."""
    tasks, loose = {}, []
    for launch in sorted(launches.values(), key=lambda n: n.ts):
        if launch.call in containers:
            continue
        (tasks.setdefault(launch.key, []) if launch.key else loose).append(launch)
    return tasks, loose


def attach_containers(tasks: dict, launches: dict, containers: dict) -> None:
    """Идущий контейнер «ведёт» задачи своих детей; законченный в отчёт не входит."""
    for call, keys in containers.items():
        if not launches[call].done:
            for key in keys:
                tasks[key].append(launches[call])
                tasks[key].sort(key=lambda n: n.ts)


def split_tasks(tasks: dict, gone: set, carry: set) -> tuple:
    """(СДЕЛАНО, ДЕЛАЕТСЯ): есть идущий непотерянный запуск — задача идёт; иначе законченная, если есть
    свежий конец или она осталась несообщённой с прошлой сборки (carry)."""
    done, doing = [], []
    for key, everyone in tasks.items():
        members = [m for m in everyone if m.call not in gone]
        running = [m for m in members if not m.done]
        if running:
            doing.append(Task(key, members, running))
        elif key in carry or any(m.fresh for m in members):
            done.append(Task(key, members, running))
    return done, doing


def who_line(members: list, labels: dict) -> str:
    """«Подпись (роль, роль), …»: одна запись на модель, роли без повторов в порядке ROLE_ORDER."""
    roles = {}
    for member in members:
        roles.setdefault(label_of(labels, member.model), set()).add(member.role)
    return ", ".join(f"{label} ({', '.join(sorted(found, key=ROLE_ORDER.get))})"
                     for label, found in roles.items())


def card_index(dev_map: dict) -> dict:
    return {t.get("id"): t for line in dev_map.get("lines") or [] for t in line.get("tasks") or []}


def title_of(key: str, members: list, cards: dict) -> str:
    """Название карточки, если оно русское; иначе — описание запуска без латиницы и ярлыка роли."""
    card = str((cards.get(key) or {}).get("title") or "").strip()
    if CYRILLIC.search(card):
        return card
    text = next((t for t in (russian_only(m.desc) for m in members) if t), DEFAULT_TITLE)
    return text[0].upper() + text[1:]


def git_output(project: str, *args: str) -> str:
    """Вывод git в корне проекта; нет git, репозитория или корня — пусто."""
    if not project:   # `git -C ""` молча берёт текущий каталог — чужой репозиторий
        return ""
    try:
        done = subprocess.run(["git", "-C", project, *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT)
    except FileNotFoundError:
        return ""
    return done.stdout if done.returncode == 0 else ""


def dev_map_grep(task_id: str) -> str:
    return "--grep=^Dev-Map: " + task_id.replace(".", r"\.") + "$"


def task_commits(project: str, task_id: str) -> str:
    """Темы и файлы коммитов со строкой «Dev-Map: <id>» целиком — по ней же видно «test_»."""
    return git_output(project, "log", "-E", dev_map_grep(task_id), "--name-only", "--format=%s")


def task_finished(launch: Launch, cards: dict, project: str) -> bool:
    """Задача оборванного запуска уже сделана: карточка в test/done/принято или после запуска есть её коммит."""
    if not launch.key or launch.key.startswith(SYNTHETIC_PREFIX):
        return False
    if (cards.get(launch.key) or {}).get("status") in FINISHED_CARD_STATUSES:
        return True
    return bool(git_output(project, "log", "-E", dev_map_grep(launch.key),
                           f"--since={launch.ts.isoformat()}", "--format=%H").strip())


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def done_stages(task: Task, project: str) -> str:
    """Сделанные этапы по порядку: бумаги задачи, коммит и законченные запуски с ролями «код», «проверка».
    У задачи «дело:…» карточки нет — ни бумаг, ни коммитов."""
    card = bool(project) and not task.key.startswith(SYNTHETIC_PREFIX)
    folder = Path(project) / ".tasks" / task.key if card else None
    commits = task_commits(project, task.key) if card else ""
    journal = read_text(folder / "журнал.md") if folder else ""
    finished_roles = {m.role for m in task.members if m.done}
    stages = {
        "тест": "test_" in journal or "test_" in commits,
        "код": bool(commits) or "код" in finished_roles,
        "проверка": "проверка" in finished_roles or bool(folder and (folder / "harness-code-review.md").is_file()),
        "мутации": bool(folder and (folder / "мутации.log").is_file()),
        "коммит": bool(commits),
    }
    return " ".join(f"{name} ✔" for name, ok in stages.items() if ok)


def done_entry(task: Task, titles: dict, labels: dict, project: str) -> Entry:
    notes = [f"делали: {who_line(task.members, labels)}"]
    stages = done_stages(task, project)
    return Entry(task.key, titles[task.key], notes + ([f"этапы: {stages}"] if stages else []))


def doing_entry(task: Task, titles: dict, labels: dict) -> Entry:
    """«делают» — только идущие; кто уже закончил, — строкой «делали»."""
    finished = [m for m in task.members if m.done]
    notes = [f"делали: {who_line(finished, labels)}"] if finished else []
    stage = STAGE_OF_ROLE[task.running[-1].role]
    return Entry(task.key, titles[task.key], notes + [f"делают: {who_line(task.running, labels)}", f"этап: {stage}"])


def clip(line: str, width: int) -> str:
    """Урезка по границе слова: обрубок посреди слова владелец читает как ошибку."""
    if len(line) <= width:
        return line
    head = line[:width - 1]
    if line[width - 1] != " " and " " in head:
        head = head[:head.rindex(" ")]
    return head.rstrip(" ,;:—-") + "…"


def tasks_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "задача"
    return "задачи" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else "задач"


class Section:
    """Блок отчёта: задачи (сколько показано — shown, остаток сворачивается), «прочее», хвост."""

    def __init__(self, header: str, entries: list, loose: int, empty: str, tail: str = "") -> None:
        self.header, self.entries, self.loose, self.empty, self.tail = header, entries, loose, empty, tail
        self.shown = len(entries)
        self.bare = 0   # сколько показанных с конца идут одним заголовком, без примечаний

    @property
    def can_shrink(self) -> bool:
        return self.shown > 1 or (self.shown == 1 and self.bare == 0)

    def shrink(self) -> None:
        """Сперва примечания у последней показанной, затем сама она: граничная задача всегда идёт голым заголовком."""
        if self.bare == 0:
            self.bare = 1
        else:
            self.shown -= 1

    def hidden_keys(self) -> list:
        return [e.key for e in self.entries[self.shown:]]

    def lines(self, first_number: int, width: int) -> list:
        lines = [self.header]
        for number, entry in enumerate(self.entries[:self.shown], first_number):
            lines.append(clip(f"{number}. {entry.title}", width))
            # Примечания не режутся: строка «делали:» с полным списком ролей — смысл отчёта.
            if number - first_number < self.shown - self.bare:
                lines += [f"   {note}" for note in entry.notes]
        hidden = len(self.entries) - self.shown
        if hidden:
            lines.append(f"ещё {hidden} {tasks_word(hidden)}")
        if self.loose:
            lines.append(f"прочее {self.loose}")
        if self.tail:
            lines.append(clip(self.tail, width))
        return lines if len(lines) > 1 else lines + [self.empty]


class LostItem(NamedTuple):
    title: str
    model: str
    ts: datetime


def lost_items(lost: list, titles: dict, labels: dict, finished) -> list:
    """По строке на задачу (самый ранний потерянный запуск); без задачи — по строке на запуск.
    Сделанная задача строкой не показывается: finished(запуск) — судья «уже сделана»."""
    firsts = {}
    for launch in lost:
        if not finished(launch):
            firsts.setdefault(launch.key or launch.call, launch)
    return [LostItem(titles.get(n.key, LOST_UNNAMED), label_of(labels, n.model), n.ts) for n in firsts.values()]


def lost_lines(lost: list, shown: int, width: int) -> list:
    """Заголовок и строки блока потерянных; пусто, если потерянных нет. Время не режется, название — да."""
    if not lost:
        return []
    lines = [LOST_HEADER]
    for item in lost[:shown]:
        fields = {"model": item.model, "clock": item.ts.astimezone().strftime("%H:%M")}
        line = LOST_LINE.format(title=item.title, **fields)
        if len(line) > width:
            room = width - len(LOST_LINE.format(title="…", **fields))
            line = LOST_LINE.format(title=item.title[:max(room, 1)].rstrip() + "…", **fields)
        lines.append(line)
    if len(lost) > shown:
        lines.append(LOST_MORE.format(count=len(lost) - shown))
    return lines


def next_cards(queue_text: str, dev_map: dict, taken: set) -> list:
    """Названия первых не начатых карточек очереди, кроме взятой сейчас («сейчас делаем: <id> — …»)
    и тех, что уже показаны выше как задачи."""
    tasks = card_index(dev_map)
    current = re.search(r"^сейчас делаем:\s*(\S+)\s+—", queue_text, re.MULTILINE)
    skip = current.group(1) if current else ""
    titles = []
    for match in re.finditer(r"^\s*\d+\.\s+(\S+)\s+·", queue_text, re.MULTILINE):
        task = tasks.get(match.group(1))
        if match.group(1) in taken:
            continue
        if task and match.group(1) != skip and task.get("status") != "wip":
            titles.append(clean_desc(str(task.get("title") or "")))
    return titles[:NEXT_MAX]


def queue_text_of(args, map_path: Path) -> str | None:
    """Вывод очереди; None — очередь недоступна (упал, завис, файл не читается)."""
    if args.queue:
        try:
            return Path(args.queue).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    try:
        done = subprocess.run([sys.executable, str(HERE / "work-queue.py"), str(map_path), "--порядок"],
                              capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return done.stdout if done.returncode == 0 else None


def parse_iso(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def read_mark(path: Path) -> datetime | None:
    try:
        return parse_iso(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def write_atomic(path: Path, text: str) -> None:
    """Файл через временный: читатель не увидит половину строки."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Уникальное имя: два параллельных вызова не топчут общий временный файл.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def write_mark(path: Path, moment: str) -> None:
    write_atomic(path, moment + "\n")


def read_keys(path: Path) -> set:
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return set()


def commit_carry(log_dir: str) -> None:
    """Доставка подтверждена: не влезшие в последнюю сборку задачи становятся несообщёнными до показа.
    Сборки не было — переносить нечего, прежний carry остаётся."""
    pending = Path(log_dir) / PENDING_NAME
    if pending.is_file():
        write_atomic(Path(log_dir) / CARRY_NAME, pending.read_text(encoding="utf-8"))


def commits_since(project_root: str, mark: datetime) -> list:
    """Темы коммитов после метки, без правок памяти с панели."""
    log = git_output(project_root, "log", f"--since={mark.isoformat()}", "--format=%s")
    return [human_subject(s) for s in log.splitlines() if s and not s.startswith(PANEL_COMMIT_PREFIX)]


def human_subject(subject: str) -> str:
    """Тема коммита без id карточки и служебного слова plan: владельцу они ничего не говорят."""
    return re.sub(r"\s+", " ", re.sub(r"\bplan\b", "", CARD_ID.sub("", subject))).strip(" :,-")


def last_finish(rows: list) -> datetime | None:
    """Конец самой свежей работы в журнале, любой сессии и любого окна."""
    moments = [parse_time(r.get("ts")) for r in rows if r.get("event") == "agent.finish"]
    return max(moments, default=None)


def idle_phrase(quiet_from: datetime, finished: datetime | None) -> str:
    """«Нового нет» с часами; в журнале нет ни одного конца — прежняя фраза."""
    if finished is None:
        return NOTHING_NEW
    clock = lambda moment: moment.astimezone().strftime("%H:%M")
    return IDLE_SINCE.format(since=clock(quiet_from), last=clock(finished))


class Report:
    """Отчёт целиком; shrink() сворачивает ровно настолько, чтобы текст влез в потолок."""

    def __init__(self, sections: list, lost: list, upcoming: list, fallback: str) -> None:
        self.sections, self.lost, self.upcoming, self.fallback = sections, lost, upcoming, fallback
        self.lost_shown = LOST_MAX
        self.width = LINE_MAX

    def text(self) -> str:
        parts, number = [], 1
        for section in self.sections:
            parts += section.lines(number, self.width)
            number += section.shown
        parts += lost_lines(self.lost, self.lost_shown, self.width) + [H_NEXT]
        parts += [clip(f"{number + i}. {t}", self.width) for i, t in enumerate(self.upcoming)] or [self.fallback]
        return "\n".join(parts)

    def shrink(self) -> bool:
        """Один шаг: примечания и задачи с конца большего блока, затем потерянные, затем ширина; False — нечего."""
        shrinkable = [sec for sec in self.sections if sec.can_shrink]
        if shrinkable:
            max(shrinkable, key=lambda sec: sec.shown).shrink()
        elif self.lost_shown > 1:
            self.lost_shown -= 1
        elif self.width > MIN_WIDTH:
            self.width -= 5
        else:
            return False
        return True


def assemble(report: Report, ceiling: int) -> str:
    while len(report.text()) > ceiling and report.shrink():
        pass
    return report.text()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--журнал-вызовов", dest="journal")
    parser.add_argument("--карта", dest="map")
    parser.add_argument("--очередь", dest="queue")
    # Форма по задачам моделей из файла не берёт; ключ принят, чтобы прежние вызовы не падали.
    parser.add_argument("--модели", dest="models")
    parser.add_argument("--сессия", dest="session")
    parser.add_argument("--с", dest="since")
    parser.add_argument("--отметить", dest="mark")
    args = parser.parse_args()

    conf = config.из_файла(config.путь_harness_conf())
    labels = parse_labels(conf.get("STATUS_MODEL_LABELS", ""))
    ceiling = config.status_ceiling(conf)
    log_dir = os.environ.get("LOG_DIR") or config.log_dir()
    project = os.environ.get("PROJECT_DIR") or config.конфиг().get("PROJECT_DIR", "")
    mark_path = Path(log_dir) / MARK_NAME
    if args.mark:
        if parse_iso(args.mark) is None:
            print(f"--отметить: не ISO-время «{args.mark}»", file=sys.stderr)
            return 2
        write_mark(mark_path, args.mark)
        commit_carry(log_dir)
        return 0
    journal = Path(args.journal or f"{log_dir}/вызовы-агентов.jsonl")
    map_path = Path(args.map or f"{project}/dev-map.yaml")
    # Пустая сессия — весь журнал: работа прошлой сессии оркестратора иначе пропадает из статуса.
    session = args.session or ""
    since = parse_time(args.since) if args.since else datetime.now(timezone.utc) - DEFAULT_WINDOW
    since_last = conf.get("STATUS_DONE_SINCE_LAST") == "1"
    # Явный --с главнее метки; метки нет — прежнее окно.
    mark = read_mark(mark_path) if since_last and not args.since else None
    cut = Cut(since, mark)

    dev_map = yaml.safe_load(map_path.read_text(encoding="utf-8")) or {}
    cards = card_index(dev_map)
    rows = list(read_rows(journal))
    lost_after = lost_threshold(conf)
    launches = parse_launches(rows, session, cut, lost_after)
    assign_keys(launches)
    containers = container_keys(launches)
    lost, gone = split_lost(launches, rows, cut, lost_after)
    lost = [n for n in lost if n.call not in containers]
    tasks, loose = group_by_task(launches, containers)
    titles = {key: title_of(key, members, cards) for key, members in tasks.items()}
    attach_containers(tasks, launches, containers)
    carry = read_keys(Path(log_dir) / CARRY_NAME) if mark else set()
    done, doing = split_tasks(tasks, gone, carry)
    quiet_from = mark or since
    commits = commits_since(project, quiet_from)
    queue_text = queue_text_of(args, map_path)
    upcoming = next_cards(queue_text, dev_map, {t.key for t in done + doing}) if queue_text is not None else []
    done_empty = idle_phrase(quiet_from, last_finish(rows)) if since_last else NOTHING
    commits_line = COMMITS_LINE.format(count=len(commits), subject=commits[0]) if commits else ""
    fresh_loose = [n for n in loose if n.fresh and n.call not in gone]
    sections = [
        Section(H_DONE, [done_entry(t, titles, labels, project) for t in done],
                sum(n.done for n in fresh_loose), done_empty, commits_line),
        Section(H_DOING, [doing_entry(t, titles, labels) for t in doing],
                sum(not n.done for n in fresh_loose), NOTHING),
    ]
    fallback = QUEUE_EMPTY if queue_text is not None else QUEUE_UNAVAILABLE
    print(assemble(Report(sections, lost_items(lost, titles, labels, lambda n: task_finished(n, cards, project)), upcoming, fallback), ceiling))
    write_atomic(Path(log_dir) / PENDING_NAME, json.dumps(sections[0].hidden_keys(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
