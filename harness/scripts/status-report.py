#!/usr/bin/env python3
"""Статус владельцу, собранный КОДОМ: по ЗАДАЧАМ, а под каждой — кто делал и какие этапы.

Слово владельца 06.11 (память/owner-orders-06-11-verbatim.md): дерево помощников с ярлыками
запусков непонятно, нужны задачи, а под ними — кто делал и какие стадии. Форма:

    ✅ СДЕЛАНО
    • выпуск vX опубликован в HH:MM    (шаги машины после среза, по строке на вид; см. ниже)
    • слито веток: N
    • письма копиям: N — <кому>
    1. <короткое название задачи>
       делали: <подпись> (<роль>), <подпись> (<роль> и <роль>)
       этапы: тест ✔ код ✔ проверка ✔ мутации ✔ коммит ✔
       коммитов N, последний: <тема>      (только коммиты «Dev-Map: <id>» семьи после среза)
    прочее N                  (помощники без метки задачи)
    🔄 ДЕЛАЕТСЯ               (пусто — «пока ничего»)
    2. <короткое название задачи>
       делали: <подпись> (<роль>)         (кто уже кончил)
       делают: <подпись> (<роль>)         (идущие; нет идущих — «сейчас никто не работает»)
       этап: <этап последнего идущего запуска>
    ⚠ ПОТЕРЯН                 (только если есть потерянные запуски; иначе блока нет)
    • <описание>, с HH:MM      (не больше 3, остаток — «и ещё N»)
    ⏭ ДАЛЬШЕ
    3. <название карточки очереди>     (одна строка, нумерация сквозная; нет карточки — первая пачка
                                        «пачка из N: <название>», нет и её — «очередь пуста»)

Блок задачи с карточкой судит КАРТА, не запуски: test/done/принято — СДЕЛАНО (даже если запуск идёт),
wip/plan — ДЕЛАЕТСЯ (даже если все запуски кончились). Задача без карточки («дело:…») — по запускам:
есть идущий не потерянный — ДЕЛАЕТСЯ, иначе СДЕЛАНО. В отчёт входит задача только с работой в окне:
свежий запуск или коммит «Dev-Map: <id>» после среза. Строки «📝 коммитов» нет: коммиты — примечание
задачи.
Семья: карточка, чья spec лежит в .tasks/<X>/…, где X — другая карточка, входит в семью X. Семья —
одна строка: член с последней работой (зонтик X — только если у детей работы нет); коммиты — всей
семьи. Потерянный запуск семьи, показанной как сданная, не в ⚠; ДАЛЬШЕ не предлагает показанную
семью и карточки test/done/принято.
Название: поле карточки «коротко»; иначе заголовок, а длиннее строки — его часть до « — » или «: ».
Урезка строки не оставляет служебное слово или тире перед «…».
Задача — id из метки «Часть: <id>#n» в описании запуска; исполнитель без метки наследует задачу
родителя по дереву запусков; запуск без метки и без родителя с задачей, но с русским описанием, —
своя задача (заголовок — описание; ярлык «Оркестратор:» с двоеточием отрезается, без двоеточия остаётся); «прочее» — остальные. «делали/делают» —
только запуски окна; подпись модели — из STATUS_MODEL_LABELS, нет там — id модели как есть. Роль запуска:
harness-lead — «вёл»; описание начинается со слова «ревью»/«review» — «проверка» (помощник,
правящий код по итогам ревью, — «код», хотя хук и ставит ему флаг «ревью»); слова тест/проба/test/probe в
описании — «тесты»; investigate/survey/audit/measure/разбор/замер/исследов — «разведка»; harness-worker —
«код»; иное — «помощник».

    status-report.py [--журнал-вызовов F] [--карта F] [--очередь F] [--пачки F] [--модели F]
                     [--сессия ID] [--с ISO-время]    (сессия по умолчанию пуста: весь журнал установки)
    status-report.py --отметить ISO-время   (пишет метку $LOG_DIR/status-report.last и выходит)
Конфиг (harness.conf): STATUS_STRUCT_MAX_CHARS — потолок всего отчёта (нет ключа — 1400; мягкий потолок
$PROJECT_DIR/память/TONE.md меньше — он: сторож пускает длиннее, а владелец читает до мягкого);
сверх потолка задачи сворачиваются с конца блока в «ещё N задач»,
STATUS_MODEL_LABELS — «модель=подпись;…»,
STATUS_DONE_SINCE_LAST — «1»: СДЕЛАНО судится по концу работы позже метки прошлого доставленного
статуса (явный --с главнее метки; метки нет — окно 12 ч); «0»/нет ключа — окно,
STATUS_LOST_HOURS — часы тишины, после которых запуск без agent.finish потерян (нет ключа или мусор — 2).
Потерянный: самая поздняя запись его жизни (свой launch, запись с data.вызов = его вызов или
data.родитель = его вызов, launch и finish потомков любой глубины, запись его транскрипта) старше
порога. Он не в ДЕЛАЕТСЯ, а в блоке «⚠ ПОТЕРЯН»; был после него запуск той же задачи — работу доделал
другой, и это не потеря: в ⚠ его нет. Задача, где остались только потерянные запуски и законченные, — законченная.
Шаги машины в СДЕЛАНО (после метки или окна): теги PUBLIC_BUILD_DIR (publish.sh ставит тег после отправки
ветки), события «mq.слито» $LOG_DIR/merge-queue.jsonl, письма — файлы почта/<копия>/…, добавленные на
refs/remotes/origin/main клона EVOLUTION_DIR коммитами автора PROJECT_NAME (паспорт установки).
ДАЛЬШЕ: первая карточка `work-queue.py --порядок`, кроме «сейчас делаем», показанных выше и сданных
(начатая тоже); нет — первая пачка `--пачки` с открытыми карточками. --очередь F без --пачки F — пачек нет.
В СДЕЛАНО при STATUS_DONE_SINCE_LAST=1 и пустом блоке: живы помощники — «с HH:MM ничего не сдано, работа
идёт», иначе — «с HH:MM нового нет; последний помощник кончил HH:MM».
--json — вместо текста один JSON-объект version 1 (поле text — тот же текст) для status_widget.py.
Этапы берутся из $PROJECT_DIR/.tasks/<id>/ (журнал.md, harness-code-review.md, мутации.log) и
из git (коммиты со строкой «Dev-Map: <id>»). Вывод в stdout, код 0.
"""
import argparse
import functools
import importlib.util
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
# Помощники живы — «нового нет» читается как «стоим» (находка копии сети 12.11): сказать, что работа идёт.
STILL_WORKING = "с {since} ничего не сдано, работа идёт"
COMMITS_NOTE = "коммитов {count}, последний: {subject}"
NOBODY_NOTE = "сейчас никто не работает"
ORCHESTRATOR_NOTE = "сейчас: оркестратор, помощников нет"
MAP_FILE = "dev-map.yaml"
GIT_TIMEOUT = 30
QUEUE_EMPTY = "очередь пуста"
QUEUE_UNAVAILABLE = "очередь недоступна"
# ДАЛЬШЕ — одна строка: владельцу нужен следующий шаг, а не список.
NEXT_MAX = 1
WHAT_MAX = 140
WAITING_MAX = 5
BATCH_LINE = "пачка из {count}: {title}"
DEFAULT_LOST_HOURS = 2
LOST_MAX = 3
# Блок потерянных: по строке на запуск, остаток одной строкой.
LOST_HEADER = "⚠ ПОТЕРЯН"
LOST_LINE = "• {title}, {model}, с {clock}"
LOST_UNNAMED = "работа без названия"
# Шаги машины в СДЕЛАНО (отчёт 19.11: выпуск, слияния и письма копиям не видели карточки).
STEP_RELEASE = "• выпуск {tags} опубликован в {clock}"
STEP_MERGED = "• слито веток: {count}"
STEP_LETTERS = "• письма копиям: {count} — {whom}"
MERGED_EVENT = "mq.слито"
MERGE_JOURNAL = "merge-queue.jsonl"
MAIL_ROOT = "почта"
TONE_FILE = "память/TONE.md"
TONE_SOFT = re.compile(r"<!--\s*потолки:\s*мягкий=(\d{3,5})")
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
STAGE_OF_ROLE = {"вёл": "ведение задачи", "разведка": "разведка", "тесты": "тест", "код": "код",
                 "проверка": "проверка", "помощник": "работа"}
DEFAULT_TITLE = "работа"

TASK_MARK = re.compile(r"Часть:\s*((?:hr|pr)\.[\w.-]*\w)")
CARD_ID = re.compile(r"\b(?:hr|pr)\.[\w.-]*\w(?:#\d+)?")
EMPTY_BRACKETS = re.compile(r"\(\s*[,;]?\s*\)")
# Служебное слово или тире в хвосте читается как оборванная мысль: «…, а не…», «… — с…».
FUNCTION_WORDS = "для|по|в|на|из|к|ко|с|со|и|а|но|не|от|за|о|об|у|что|как|или|при|без|до|во|—|-"
DANGLING_TAIL = re.compile(rf"(?:\s+(?:{FUNCTION_WORDS}))+\s*$")
# Начало с служебного слова или одно слово после выкидывания латиницы — обрубок, не название задачи.
STUB_HEAD = re.compile(rf"(?:{FUNCTION_WORDS})(?:\s|$)", re.IGNORECASE)
EVOLUTION_TITLE = re.compile(r"^(\S+) от (\S+): [a-z0-9-]+$")
EVOLUTION_KIND_UNKNOWN = "?"
TITLE_SPLITS = (" — ", ": ")
# Место под «N. » перед заголовком в строке LINE_MAX.
NUMBER_ROOM = 4
NOTE_INDENT = 3   # отступ примечания под задачей
SPEC_FOLDER = re.compile(r"\.tasks/([^/]+)/")
DEV_MAP_TRAILER = re.compile(r"^Dev-Map:\s*(\S+)\s*$", re.MULTILINE)
CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
LATIN_TOKEN = re.compile(r"\S*[A-Za-z]\S*")
# Порядок ролей в «делали/делают»: вёл → тесты → код → проверка.
ROLE_ORDER = {"вёл": 0, "разведка": 1, "тесты": 2, "код": 3, "проверка": 4, "помощник": 5}
# «ревью» только в начале описания: слово посреди («правки по ревью») отмечает того, кто правит код.
TEST_WORD = re.compile(r"\b(?:тест\w*|проб(?:а|ы|у|ой|е)|tests?|probes?)\b|test_", re.IGNORECASE)
SCOUT_WORD = re.compile(r"investigat|survey|audit|measur|разбор|замер|исследов", re.IGNORECASE)
REVIEW_START = re.compile(r"(?:ревью|review)\b", re.IGNORECASE)


class Launch:
    """Запуск помощника из журнала; задача — id из метки «Часть:», пусто — помощник без метки."""

    def __init__(self, row: dict, data: dict) -> None:
        raw = str(data.get("описание") or "")
        # Метка стоит в тексте заказа; хук кладёт её в data.часть, в описании её бывает только в старых строках.
        mark = TASK_MARK.search(f"{data.get('часть') or ''} {raw}")
        self.task = mark.group(1) if mark else ""
        self.call = data["вызов"]
        self.session = str(row.get("session") or "")
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
        if SCOUT_WORD.search(self.desc):
            return "разведка"
        return "код" if self.kind == "harness-worker" else "помощник"


class Task(NamedTuple):
    key: str
    members: list      # запуски по времени, без потерянных
    running: list      # из них идущие и свежие
    window: list       # из них работа окна: о ней «делали/делают»


class Commit(NamedTuple):
    ts: datetime
    subject: str


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
    original, stripped = text, LATIN_TOKEN.sub("", text)
    text = re.sub(r"\s+", " ", stripped).strip(" :,-")
    text = DANGLING_TAIL.sub("", text).strip()
    # Латиница была и осталась одна приставка или предлог («Пачка», «В спеках») — это обрубок, а не суть.
    if LATIN_TOKEN.search(original) and (len(text.split()) < 2 or STUB_HEAD.match(text)):
        return ""
    return text


def parse_labels(raw: str) -> dict:
    pairs = (item.split("=", 1) for item in raw.split(";") if "=" in item)
    return {k.strip(): v.strip() for k, v in pairs}


def label_of(labels: dict, model: str) -> str:
    # Модель вне STATUS_MODEL_LABELS называется своим id: выдуманная подпись — враньё.
    return labels.get(model) or model or "модель не названа"


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


def transcript_heard(project: str, silent: list) -> dict:
    """Вызов → последняя запись транскрипта помощника (mtime jsonl рядом с meta по toolUseId).
    Долгий прогон помощника молчит в журнале вызовов часами, а транскрипт пишется, пока он жив."""
    if not project or not silent:
        return {}
    base = Path(os.environ.get("CLAUDE_PROJECTS_DIR") or Path.home() / ".claude" / "projects")
    # То же кодирование пути проекта, что у сторожей (stall-in-log.py project_transcripts).
    folders = {base / project.replace("/", "-"), base / re.sub(r"[^a-zA-Z0-9]", "-", project)}
    wanted = {n.call for n in silent}
    heard = {}
    for folder in folders:
        for session in {n.session for n in silent if n.session}:
            for meta in (folder / session / "subagents").glob("agent-*.meta.json"):
                try:
                    call = json.loads(meta.read_text(encoding="utf-8")).get("toolUseId")
                    body = meta.with_name(meta.name.replace(".meta.json", ".jsonl"))
                    if call in wanted:
                        heard[call] = datetime.fromtimestamp(body.stat().st_mtime, timezone.utc)
                except (OSError, ValueError, AttributeError):
                    continue
    return heard


def orchestrator_alive(project: str, sessions: set, lost_after: timedelta) -> bool:
    """Оркестратор сам сливает, выпускает и зовёт помощников — в журнале вызовов его нет; признак жизни —
    транскрипт его сессии моложе порога потери."""
    if not project:
        return False
    base = Path(os.environ.get("CLAUDE_PROJECTS_DIR") or Path.home() / ".claude" / "projects")
    folders = {base / project.replace("/", "-"), base / re.sub(r"[^a-zA-Z0-9]", "-", project)}
    now = datetime.now(timezone.utc)
    for folder in folders:
        for session in sessions:
            try:
                heard = datetime.fromtimestamp((folder / f"{session}.jsonl").stat().st_mtime, timezone.utc)
            except OSError:
                continue
            if now - heard <= lost_after:
                return True
    return False


def split_lost(launches: dict, rows: list, cut: Cut, lost_after: timedelta, project: str) -> tuple:
    """(потерянные для показа, их вызовы): без конца и без жизни дольше порога — ни в журнале, ни в
    транскрипте. С меткой потерянный показывается, если запущен в окне или жил после метки."""
    alive = last_alive(rows)
    now = datetime.now(timezone.utc)
    silent = [n for c, n in launches.items() if not n.done and now - alive.get(c, n.ts) > lost_after]
    for call, moment in transcript_heard(project, silent).items():
        alive[call] = max(alive.get(call, moment), moment)
    gone = {c for c, n in launches.items() if not n.done and now - alive.get(c, n.ts) > lost_after}
    shown = [launches[c] for c in gone
             if launches[c].ts >= cut.since or (cut.mark is not None and alive.get(c, launches[c].ts) > cut.mark)]
    return sorted(shown, key=lambda n: (n.ts, n.call)), gone


def descends_from(launch: Launch, ancestor: str, launches: dict) -> bool:
    """Запуск — потомок вызова ancestor по цепочке родителей; счётчик защищает от петли."""
    step, hops = launch.parent, 0
    while step and hops <= len(launches):
        if step == ancestor:
            return True
        step = launches[step].parent if step in launches else ""
        hops += 1
    return False


def continued_by_other(lost: Launch, launches: dict) -> bool:
    """После оборванного задачу взял другой: позже запущен запуск той же задачи, не его потомок.
    Свой ребёнок оборванного работу не продолжает: он часть той же брошенной работы."""
    return bool(lost.key) and any(m.key == lost.key and m.ts > lost.ts and not descends_from(m, lost.call, launches)
                                  for m in launches.values())


def task_key(launch: Launch, launches: dict) -> str:
    """Метка «Часть:» ближайшего предка (или своя); без меток — самый верхний запуск цепочки с русским
    описанием; иначе пусто. Счётчик шагов защищает от петли в битом журнале."""
    chain = [launch]
    while len(chain) <= len(launches) and chain[-1].parent in launches:
        chain.append(launches[chain[-1].parent])
    marked = next((n.task for n in chain if n.task), "")
    return marked or next((f"{SYNTHETIC_PREFIX}{n.call}" for n in reversed(chain) if russian_only(n.desc)), "")


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


def has_window_work(key: str, members: list, carry: set, commits: dict) -> bool:
    """Работа в окне: свежий запуск, коммит «Dev-Map» после среза или несообщённое с прошлой сборки."""
    return key in carry or key in commits or any(m.fresh for m in members)


def window_tasks(tasks: dict, gone: set, carry: set, commits: dict, cards: dict) -> list:
    """Задачи с работой в окне. Коммит без запуска считается только у известной карточки."""
    found = []
    for key in list(tasks) + [k for k in commits if k in cards and k not in tasks]:
        members = [m for m in tasks.get(key, []) if m.call not in gone]
        if not has_window_work(key, members, carry, commits):
            continue
        fresh = [m for m in members if m.fresh]
        found.append(Task(key, members, [m for m in members if not m.done], fresh or (members if key in carry else [])))
    return found


def family_root(key: str, cards: dict) -> str:
    """Зонтик семьи: карточка, в чьей папке .tasks/<X>/ лежит спека этой карточки; иначе она сама."""
    found = SPEC_FOLDER.search(str((cards.get(key) or {}).get("spec") or ""))
    return found.group(1) if found and found.group(1) != key and found.group(1) in cards else key


def work_time(task: Task, commits: dict, now: datetime) -> datetime:
    """Когда в задаче последний раз работали: идущий запуск — сейчас, иначе конец запуска или коммит."""
    moments = [m.finished_at or now for m in task.window] + [c.ts for c in commits.get(task.key, [])]
    return max(moments, default=datetime.min.replace(tzinfo=timezone.utc))


def one_per_family(found: list, cards: dict, commits: dict) -> list:
    """Семья — одна строка: член с последней работой; зонтик — только если у детей работы нет."""
    now = datetime.now(timezone.utc)
    groups = {}
    for task in found:
        groups.setdefault(family_root(task.key, cards), []).append(task)
    picked = []
    for root, group in groups.items():
        pool = [t for t in group if t.key != root] or group
        picked.append(max(pool, key=lambda t: work_time(t, commits, now)))
    return picked


def is_finished(task: Task, cards: dict) -> bool:
    """Карточка решает: test/done/принято — сделано, иначе делается, хоть никто и не работает. Задача без
    карточки («дело:…») — по запускам: идущих нет — сделана."""
    card = cards.get(task.key)
    if card:
        return card.get("status") in FINISHED_CARD_STATUSES
    return not task.running


def split_tasks(found: list, cards: dict) -> tuple:
    done = [t for t in found if is_finished(t, cards)]
    return done, [t for t in found if not is_finished(t, cards)]


def who_line(members: list, labels: dict) -> str:
    """«Подпись (роль, роль), …»: одна запись на модель, роли без повторов в порядке ROLE_ORDER."""
    roles = {}
    for member in members:
        roles.setdefault(label_of(labels, member.model), set()).add(member.role)
    return ", ".join(f"{label} ({', '.join(sorted(found, key=ROLE_ORDER.get))})"
                     for label, found in roles.items())


def card_index(dev_map: dict) -> dict:
    return {t.get("id"): t for line in dev_map.get("lines") or [] for t in line.get("tasks") or []}


def evolution_title(title: str) -> str:
    """Заголовок карточки сети копий «<вид> от <копия>: <ярлык>» — по-человечески: латинский ярлык и «?»
    владельцу ничего не говорят. Другой заголовок возвращается как есть."""
    found = EVOLUTION_TITLE.match(title)
    if not found:
        return title
    kind, sender = found.groups()
    sender = "" if sender == EVOLUTION_KIND_UNKNOWN else f" {sender}"
    if kind == EVOLUTION_KIND_UNKNOWN:
        return f"Находка копии{sender}, не разобрана"
    return f"{kind.capitalize()} от копии{sender}"


def short_title(card: dict) -> str:
    """Подпись для строки: поле «коротко»; иначе целый заголовок, а длинный — часть до « — » или «: »."""
    own = str(card.get("коротко") or "").strip()
    title = evolution_title(str(card.get("title") or "").strip())
    if own or len(title) <= LINE_MAX - NUMBER_ROOM:
        return own or title
    # Тире отделяет суть от пояснения грубее, чем двоеточие: «Скорость: харнес … — мощность по …».
    sep = next((s for s in TITLE_SPLITS if s in title), "")
    return title.split(sep, 1)[0] if sep else title


def title_of(key: str, members: list, cards: dict) -> str:
    """Название карточки, если оно русское; иначе — описание запуска без латиницы и ярлыка роли."""
    card = short_title(cards.get(key) or {})
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


def task_finished(launch: Launch, cards: dict, project: str, umbrellas_done: set) -> bool:
    """Задача оборванного запуска уже сделана: карточка в test/done/принято, она зонтик семьи, показанной
    сданной, или после запуска есть её коммит."""
    if not launch.key or launch.key.startswith(SYNTHETIC_PREFIX):
        return False
    # Сданный ребёнок закрывает работу зонтика, но не брата: у брата своя карточка и своя тревога.
    if launch.key in umbrellas_done:
        return True
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


def commit_note(commits: list) -> list:
    # Тема коммита бывает в сотни знаков; примечания не режутся Section, потому режем тут.
    note = COMMITS_NOTE.format(count=len(commits), subject=commits[0].subject) if commits else ""
    return [clip(note, LINE_MAX - NOTE_INDENT)] if note else []


def who_notes(verb: str, members: list, labels: dict) -> list:
    return [f"{verb}: {who_line(members, labels)}"] if members else []


def done_entry(task: Task, titles: dict, labels: dict, project: str, commits: list) -> Entry:
    stages = done_stages(task, project)
    notes = who_notes("делали", task.window, labels) + ([f"этапы: {stages}"] if stages else [])
    return Entry(task.key, titles[task.key], notes + commit_note(commits))


def doing_entry(task: Task, titles: dict, labels: dict, commits: list, orchestrating: bool = False) -> Entry:
    """«делают» — только идущие; кто уже закончил, — строкой «делали»; идущих нет — «сейчас никто не работает»."""
    notes = who_notes("делали", [m for m in task.window if m.done], labels)
    if task.running:
        notes += [f"делают: {who_line(task.running, labels)}", f"этап: {STAGE_OF_ROLE[task.running[-1].role]}"]
    else:
        notes.append(ORCHESTRATOR_NOTE if orchestrating else NOBODY_NOTE)
    return Entry(task.key, titles[task.key], notes + commit_note(commits))


def clip(line: str, width: int) -> str:
    """Урезка по границе слова: обрубок посреди слова владелец читает как ошибку."""
    if len(line) <= width:
        return line
    head = line[:width - 1]
    if line[width - 1] != " " and " " in head:
        head = head[:head.rindex(" ")]
    # Служебное слово перед «…» — обрубок мысли; срезается вместе с тире.
    head = head.rstrip(" ,;:—-")
    while (trimmed := DANGLING_TAIL.sub("", head).rstrip(" ,;:—-")) != head:
        head = trimmed
    return head + "…"


def tasks_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "задача"
    return "задачи" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else "задач"


class Section:
    """Блок отчёта: задачи (сколько показано — shown, остаток сворачивается), «прочее»."""

    def __init__(self, header: str, entries: list, loose: int, empty: str, steps: tuple = ()) -> None:
        self.header, self.entries, self.loose, self.empty = header, entries, loose, empty
        self.steps = list(steps)   # строки шагов машины: идут первыми и не сворачиваются
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
        lines = [self.header] + [clip(step, width) for step in self.steps]
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
        return lines if len(lines) > 1 else lines + [self.empty]


class LostItem(NamedTuple):
    title: str
    model: str
    ts: datetime


def lost_items(lost: list, titles: dict, labels: dict, finished, continued) -> list:
    """По строке на задачу (самый ранний потерянный запуск); без задачи — по строке на запуск.
    Потеря — только брошенная работа: finished(запуск) — задача уже сделана; continued(запуск) —
    после него в задаче был новый запуск, работу доделывает другой (отчёт 19.11: «оборван, работу
    продолжили» в ⚠ владелец читал как тревогу, которой нет)."""
    firsts = {}
    for launch in lost:
        if not finished(launch) and not continued(launch):
            firsts.setdefault(launch.key or launch.call, launch)
    return [LostItem(titles.get(n.key, LOST_UNNAMED), label_of(labels, n.model), n.ts)
            for n in firsts.values()]


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
            line = LOST_LINE.format(title=clip(item.title, max(room, 1) + 1), **fields)
        lines.append(line)
    if len(lost) > shown:
        lines.append(LOST_MORE.format(count=len(lost) - shown))
    return lines


def is_open(card_id: str, tasks: dict, taken: set) -> bool:
    """Карточка ждёт работы: есть в карте, не показана выше (taken — семьи показанных) и не сдана."""
    task = tasks.get(card_id)
    return bool(task) and card_id not in taken and task.get("status") not in FINISHED_CARD_STATUSES


def next_cards(queue_text: str, dev_map: dict, taken: set) -> list:
    """Название первой открытой карточки очереди, кроме взятой сейчас («сейчас делаем: <id> — …»).
    Начатая (wip) тоже следующая: её никто не ведёт выше, а первые десять очереди обычно начаты."""
    tasks = card_index(dev_map)
    current = re.search(r"^сейчас делаем:\s*(\S+)\s+—", queue_text, re.MULTILINE)
    skip = current.group(1) if current else ""
    ids = re.findall(r"^\s*\d+\.\s+(\S+)\s+·", queue_text, re.MULTILINE)
    return [clean_desc(short_title(tasks[cid])) for cid in ids
            if cid != skip and is_open(cid, tasks, taken)][:NEXT_MAX]


def next_batch(batches_text: str, dev_map: dict, taken: set) -> list:
    """Первая пачка вывода `work-queue.py --пачки` с открытыми карточками — строкой «пачка из N: <первая>».
    Одиночки пачкой не считаются."""
    tasks = card_index(dev_map)
    for line in batches_text.splitlines():
        head, sep, ids = line.rpartition(": ")
        if not sep or head.startswith(("пачек", "одиночки")):
            continue
        members = [cid for cid in (s.strip() for s in ids.split(",")) if is_open(cid, tasks, taken)]
        if members:
            title = clean_desc(short_title(tasks[members[0]]))
            return [BATCH_LINE.format(count=len(members), title=title)]
    return []


def queue_text_of(given: str | None, map_path: Path, mode: str) -> str | None:
    """Вывод `work-queue.py <карта> <mode>` или файла given; None — недоступен (упал, завис, не читается)."""
    if given:
        try:
            return Path(given).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    try:
        done = subprocess.run([sys.executable, str(HERE / "work-queue.py"), str(map_path), mode],
                              capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return done.stdout if done.returncode == 0 else None


def upcoming_lines(args, map_path: Path, dev_map: dict, taken: set) -> tuple:
    """(строки ДАЛЬШЕ, запасная строка): карточка очереди; нет — первая пачка; нет и её — «очередь пуста».
    Очередь из файла без файла пачек — пачек нет: стенд не зовёт настоящую очередь."""
    queue_text = queue_text_of(args.queue, map_path, "--порядок")
    if queue_text is None:
        return [], QUEUE_UNAVAILABLE
    found = next_cards(queue_text, dev_map, taken)
    if not found and (args.batches or not args.queue):
        batches = queue_text_of(args.batches, map_path, "--пачки")
        found = next_batch(batches, dev_map, taken) if batches is not None else []
    return found, QUEUE_EMPTY


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


def card_commits(project: str, since: datetime) -> dict:
    """Карточка → коммиты после среза с её трейлером «Dev-Map: <id>», новые первыми. Коммит без трейлера
    ничьей работой не считается."""
    # Срез — в коде, не в `--since`: git обрывает обход на первом старом коммите, а даты в истории бывают не по порядку.
    log = git_output(project, "log", "--grep=^Dev-Map:", "--name-only", "--format=%x01%ct%x02%s%x02%b%x02")
    found = {}
    for record in log.split("\x01")[1:]:
        stamp, subject, body, files = record.split("\x02", 3)
        # Служебная запись карты (правит один dev-map.yaml) — не работа над задачей.
        if files.split() == [MAP_FILE]:
            continue
        commit = Commit(datetime.fromtimestamp(int(stamp), timezone.utc), human_subject(subject))
        if commit.ts < since:
            continue
        for card_id in DEV_MAP_TRAILER.findall(body):
            found.setdefault(card_id, []).append(commit)
    return found


def family_commits(root: str, cards: dict, commits: dict) -> list:
    """Коммиты всей семьи карточек, новые первыми."""
    every = [c for key, found in commits.items() if family_root(key, cards) == root for c in found]
    return sorted(every, key=lambda c: c.ts, reverse=True)


def human_subject(subject: str) -> str:
    """Тема коммита без id карточки и служебного слова plan: владельцу они ничего не говорят."""
    # Скобки, где был только id, пустеют: «(hr.x)» → «()» читается как обрыв.
    bare = EMPTY_BRACKETS.sub("", re.sub(r"\bplan\b", "", CARD_ID.sub("", subject)))
    return re.sub(r"\s+", " ", bare).strip(" :,-")


def local_clock(moment: datetime) -> str:
    return moment.astimezone().strftime("%H:%M")


def release_steps(build_dir: str, since: datetime) -> list:
    """Теги публичной сборки после среза: тег ставит publish.sh только после отправки ветки."""
    refs = git_output(build_dir, "for-each-ref", "refs/tags", "--sort=creatordate",
                      "--format=%(refname:short)%09%(creatordate:unix)")
    fresh = [(name, datetime.fromtimestamp(int(stamp), timezone.utc))
             for name, _, stamp in (line.partition("\t") for line in refs.splitlines()) if stamp.isdigit()]
    fresh = [(name, moment) for name, moment in fresh if moment > since]
    if not fresh:
        return []
    return [STEP_RELEASE.format(tags=", ".join(name for name, _ in fresh), clock=local_clock(fresh[-1][1]))]


def merged_steps(log_dir: str, since: datetime) -> list:
    """Слитые ветки после среза — события «mq.слито» журнала очереди слияния."""
    count = sum(1 for row in read_rows(Path(log_dir) / MERGE_JOURNAL)
                if row.get("event") == MERGED_EVENT and parse_time(row.get("ts")) > since)
    return [STEP_MERGED.format(count=count)] if count else []


def letter_steps(clone: str, own: str, since: datetime) -> list:
    """Письма, ушедшие копиям после среза: файлы почта/<кому>/…, добавленные коммитами этой копии на main
    общего репозитория (main клона обновляет читатель сети). Входящее и находки — не письма."""
    if not own:
        return []
    log = git_output(clone, "-c", "core.quotepath=off", "log", "refs/remotes/origin/main",
                     "--diff-filter=A", "--name-only", "--format=%x01%ct%x02%an")
    whom = []
    for record in log.split("\x01")[1:]:
        head, _, names = record.partition("\n")
        stamp, _, author = head.partition("\x02")
        if author != own or not stamp.isdigit() or datetime.fromtimestamp(int(stamp), timezone.utc) <= since:
            continue
        for parts in (name.split("/") for name in names.splitlines()):
            if len(parts) > 2 and parts[0] == MAIL_ROOT and parts[1] != own:
                whom.append(parts[1])
    names = ", ".join(dict.fromkeys(sorted(whom)))
    return [STEP_LETTERS.format(count=len(whom), whom=names)] if whom else []


def work_steps(conf: dict, log_dir: str, own: str, since: datetime) -> list:
    """Сделанные шаги машины после среза — выпуск, слияния, письма; по строке на вид."""
    clone = os.path.expanduser(str(conf.get("EVOLUTION_DIR") or "").strip())
    return (release_steps(str(conf.get("PUBLIC_BUILD_DIR") or "").strip(), since)
            + merged_steps(log_dir, since) + letter_steps(clone, own, since))


def tone_ceiling(project: str) -> int | None:
    """Мягкий потолок вести из машинной строки TONE.md («<!-- потолки: мягкий=N … -->»); нет — None."""
    found = TONE_SOFT.search(read_text(Path(project) / TONE_FILE)) if project else None
    return int(found.group(1)) if found else None


def last_finish(rows: list) -> datetime | None:
    """Конец самой свежей работы в журнале, любой сессии и любого окна."""
    moments = [parse_time(r.get("ts")) for r in rows if r.get("event") == "agent.finish"]
    return max(moments, default=None)


def idle_phrase(quiet_from: datetime, finished: datetime | None, working: bool) -> str:
    """«Нового нет» с часами; помощники живы — «ничего не сдано, работа идёт»; в журнале нет ни одного
    конца — прежняя фраза."""
    clock = lambda moment: moment.astimezone().strftime("%H:%M")
    if working:
        return STILL_WORKING.format(since=clock(quiet_from))
    if finished is None:
        return NOTHING_NEW
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


@functools.lru_cache(maxsize=None)
def load_script(name: str, file: str):
    """Модуль с дефисом в имени файла — как у доски (build-board.py:load_script); один раз за запуск."""
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def line_titles(dev_map: dict) -> dict:
    """id карточки → название линии карты: подпись «часть чего», пока карты направлений нет."""
    return {t.get("id"): line.get("title") or line.get("id") or ""
            for line in dev_map.get("lines") or [] for t in line.get("tasks") or []}


def optional(what: str, build, empty):
    """Вызов чужого модуля, без которого статус живёт: подпись, полоса, «ждёт тебя».
    Сбой — empty и строка в stderr (tg_send.sh её не глушит): статус не падает, но и не молчит."""
    try:
        return build()
    # Широко намеренно: модуль чужой и грузится в процесс — его ConfigError, битый yaml, KeyError
    # на чужой схеме, SyntaxError правки соседа заранее не перечислить, а любой из них ронял бы
    # весь статус (tg_send.sh --статус зовёт только --json).
    except Exception as error:  # noqa: BLE001
        print(f"status-report: {what}: {type(error).__name__}: {str(error)[:200]}", file=sys.stderr)
        return empty


@functools.lru_cache(maxsize=1)
def gate():
    """(build-board.py, суждение гейта) — один раз за запуск, а не на каждую карточку."""
    board = load_script("build_board", "build-board.py")
    return board, board.load_gate()


def direction_names(dev_map: dict) -> dict:
    """id карточки → название направления. Нет модуля или конфига, конфиг сломан — пусто: статус
    из-за подписи падать не должен, подпись откатывается на линию карты."""
    package = Path(os.environ.get("HARNESS_DIRECTIONS") or HERE.parent / "harness" / "config" / "directions.yaml")
    if not (HERE / "build-directions.py").is_file() or not package.is_file():
        return {}

    def build() -> dict:
        module = load_script("build_directions", "build-directions.py")
        settings = module.load_config(package, module.local_layer())
        names = {d["id"]: d["название"] for group in settings["группы"] for d in group["направления"]}
        names[module.OTHER] = settings["прочее"]["название"]
        assigned = module.assign(dev_map.get("lines") or [], settings)
        return {c.get("id"): names[home] for home, group in assigned.items() for c in group}
    return optional("направления", build, {})


def stage_bar(card: dict) -> list:
    """Полоса стадий суждением гейта доски: пройдены / текущая (первая непройденная) / остались.
    Шаги «не положен этой задаче» не показываются. Гейт не прочёл бумаги — полосы нет, статус идёт."""
    if not card:
        return []
    steps = optional("полоса стадий", lambda: gate()[0].column(card, gate()[1])[2], None)
    if steps is None:
        return []
    bar, current_given = [], False
    for step in (s for s in steps if not str(s["почему"]).startswith("не положен")):
        if step["есть"] is True:
            state = "passed"
        elif not current_given:
            state, current_given = "current", True
        else:
            state = "left"
        bar.append({"step": step["шаг"], "state": state})
    return bar


def human(text: str) -> str:
    """Видимая строка без id карточек и переносов."""
    return re.sub(r"\s+", " ", CARD_ID.sub("", text or "")).strip()


def what_of(card: dict) -> str:
    """Что и зачем: первая фраза summary, не длиннее WHAT_MAX."""
    summary = human(str(card.get("summary") or ""))
    first = re.match(r"(.+?[.!?])(?:\s|$)", summary)
    return clip(first.group(1) if first else summary, WHAT_MAX)


def task_json(task: Task, titles: dict, cards: dict, labels: dict, parts: dict, lines: dict) -> dict:
    card = cards.get(task.key) or {}
    who = [{"model": label_of(labels, m.model), "role": m.role, "running": True} for m in task.running]
    return {"id": task.key, "title": human(titles[task.key]), "what": what_of(card),
            "part": human(parts.get(task.key) or lines.get(task.key) or ""),
            "status": card.get("status") or "", "stages": stage_bar(card), "who": who}


def waiting_json(dev_map: dict) -> tuple:
    """(до WAITING_MAX названий, всего решений) из work-queue.py:ждёт_владельца."""
    waiting = optional("ждёт тебя", lambda: load_script("work_queue", "work-queue.py").ждёт_владельца(dev_map),
                       {"имена": [], "решений": 0})
    return [{"title": human(name), "what": ""} for name in waiting["имена"][:WAITING_MAX]], waiting["решений"]


def account_line() -> str:
    """Строка «аккаунт: …» из account-switch.py --сейчас; профили не заведены или вызов не удался — строки нет."""
    try:
        done = subprocess.run([sys.executable, str(HERE / "account-switch.py"), "--сейчас"],
                              capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--журнал-вызовов", dest="journal")
    parser.add_argument("--карта", dest="map")
    parser.add_argument("--очередь", dest="queue")
    parser.add_argument("--пачки", dest="batches")
    # Форма по задачам моделей из файла не берёт; ключ принят, чтобы прежние вызовы не падали.
    parser.add_argument("--модели", dest="models")
    parser.add_argument("--сессия", dest="session")
    parser.add_argument("--с", dest="since")
    parser.add_argument("--отметить", dest="mark")
    parser.add_argument("--json", dest="as_json", action="store_true")
    args = parser.parse_args()

    conf = config.из_файла(config.путь_harness_conf())
    labels = parse_labels(conf.get("STATUS_MODEL_LABELS", ""))
    log_dir = os.environ.get("LOG_DIR") or config.log_dir()
    passport = config.конфиг()
    project = os.environ.get("PROJECT_DIR") or passport.get("PROJECT_DIR", "")
    # Сторож канала пускает до STATUS_STRUCT_MAX_CHARS, а владелец читает до мягкого потолка TONE.md.
    ceiling = min(filter(None, (config.status_ceiling(conf), tone_ceiling(project))))
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
    lost, gone = split_lost(launches, rows, cut, lost_after, project)
    lost = [n for n in lost if n.call not in containers]
    tasks, loose = group_by_task(launches, containers)
    quiet_from = mark or since
    commits = card_commits(project, quiet_from)
    titles = {key: title_of(key, tasks.get(key, []), cards) for key in list(tasks) + list(commits)}
    attach_containers(tasks, launches, containers)
    carry = read_keys(Path(log_dir) / CARRY_NAME) if mark else set()
    shown = one_per_family(window_tasks(tasks, gone, carry, commits, cards), cards, commits)
    done, doing = split_tasks(shown, cards)
    shown_roots = {family_root(t.key, cards) for t in shown}
    taken = {cid for cid in cards if family_root(cid, cards) in shown_roots}
    upcoming, fallback = upcoming_lines(args, map_path, dev_map, taken)
    sessions = {r["session"] for r in rows if r.get("session")}
    orchestrating = orchestrator_alive(project, sessions, lost_after)
    working = orchestrating or any(not n.done and n.call not in gone for n in launches.values())
    done_empty = idle_phrase(quiet_from, last_finish(rows), working) if since_last else NOTHING
    fresh_loose = [n for n in loose if n.fresh and n.call not in gone]
    commits_of = lambda t: family_commits(family_root(t.key, cards), cards, commits)
    sections = [
        Section(H_DONE, [done_entry(t, titles, labels, project, commits_of(t)) for t in done],
                sum(n.done for n in fresh_loose), done_empty,
                work_steps(conf, log_dir, passport.get("PROJECT_NAME", ""), quiet_from)),
        Section(H_DOING, [doing_entry(t, titles, labels, commits_of(t), orchestrating) for t in doing], sum(not n.done for n in fresh_loose), NOTHING),
    ]
    umbrellas_done = {root for t in done if (root := family_root(t.key, cards)) != t.key}
    finished = lambda n: task_finished(n, cards, project, umbrellas_done)
    continued = lambda n: continued_by_other(n, launches)
    account = account_line()
    room = ceiling - (len(account) + 1 if account else 0)
    text = assemble(Report(sections, lost_items(lost, titles, labels, finished, continued), upcoming, fallback), room)
    text += "\n" + account if account else ""
    if args.as_json:
        parts, lines = direction_names(dev_map), line_titles(dev_map)
        make = lambda t: task_json(t, titles, cards, labels, parts, lines)
        waiting, waiting_total = waiting_json(dev_map)
        report = {"version": 1, "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                  "text": text, "doing": [make(t) for t in doing], "doing_more": 0,
                  "delivered": [make(t) for t in done], "delivered_more": 0, "other": len(fresh_loose),
                  "waiting_owner": waiting, "waiting_total": waiting_total, "next": upcoming, "account": account}
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(text)
    write_atomic(Path(log_dir) / PENDING_NAME, json.dumps(sections[0].hidden_keys(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
