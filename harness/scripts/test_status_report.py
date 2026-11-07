#!/usr/bin/env python3
"""Проба: отчёт о статусе строится КОДОМ — три блока, вложенность, подписи моделей.

Приказ владельца 03.11.2026: «что уже сделано, что делается, что будет взято
следующим — в виде вложенности: сабы твои, управляющий, сабы — с подписями
моделей <…> с иконками <…> это должно быть задано тоже кодом».

ИНТЕРФЕЙС scripts/status-report.py (исполнитель кода следует ему дословно):

    python3 scripts/status-report.py \
        --журнал-вызовов F   # jsonl запусков; умолчание $LOG_DIR/вызовы-агентов.jsonl
        --карта F            # dev-map.yaml; умолчание $PROJECT_DIR/dev-map.yaml
        --очередь F          # вывод `work-queue.py <карта> --порядок` файлом;
                             # без ключа — зовёт work-queue.py сам
        --модели F           # models.yaml; умолчание harness/config/models.yaml;
                             # модели.смена — оркестратор, модели.управляющий — ДАЛЬШЕ
        --сессия ID          # чьи запуски; умолчание — все сессии журнала
        --с ISO-время        # запуски не раньше; умолчание — окно из данных
    Конфиг — harness.conf по $HARNESS_CONF (scripts/lib/config.py):
        STATUS_STRUCT_MAX_CHARS="1400"   потолок всего отчёта, знаков; ключа нет —
                                         сборщик И сторож берут один запасной 1400
                                         (config.status_ceiling, умолчание system-jobs.yaml)
        STATUS_MODEL_LABELS="opus=Opus 5.5;sonnet=Sonnet 5.5;haiku=Haiku 5.5"
        STATUS_ROLE_LABELS="main=Главный оркестратор;lead=Оркестратор;worker="
                                         названия ролей — данные, тот же формат
                                         «ключ=подпись;…»; пустое — строка без названия
                                         (main — корень дерева, lead — harness-lead,
                                         worker — исполнитель)
    Вывод: текст отчёта в stdout, код 0.

Журнал (строки events v1, лишнее и битое пропускается молча):
    запуск:  {"event":"agent.launch","outcome":"ok","ts":…,"session":…,
              "data":{"описание":…,"тип":"harness-lead|harness-worker|…",
                      "модель":"opus|sonnet|haiku","ревью":bool,
                      "вызов":"<tool_use_id>","родитель":"<tool_use_id управляющего>"}}
              «родитель» нет — запуск самого оркестратора. outcome≠ok, нет «вызов»
              (старый формат), чужая сессия, ts раньше --с — не показываются.
    конец:   {"event":"agent.finish","outcome":"ok","ts":…,"session":…,"data":{"вызов":…}}
    Есть конец — СДЕЛАНО, нет — ДЕЛАЕТСЯ.

Форма ПО ЗАДАЧАМ (слово владельца 06.11, память/owner-orders-06-11-verbatim.md: «что за
задачи <…> а под ними кто делал и какие стадии»; строка ≤ 80 знаков, ни id карточек,
ни «Часть:», ни английских ярлыков запусков), случаи 51–58:
    ✅ СДЕЛАНО
    1. <заголовок задачи>
       делали: <подпись> (<роль>), <подпись> (<роль> и <роль>)
       этапы: тест ✔ код ✔ проверка ✔ мутации ✔ коммит ✔
    прочее N                       помощники без метки, одной строкой с числом
    📝 коммитов N, последний: …    как прежде, последней строкой СДЕЛАНО
    🔄 ДЕЛАЕТСЯ   (пусто — «пока ничего»)
    2. <заголовок задачи>
       делают: <подпись> (<роль>)
       этап: <последний начатый>
    ⚠ ПОТЕРЯН     как прежде: «• <описание запуска>, с HH:MM»
    ⏭ ДАЛЬШЕ
    3. <название карточки очереди>   нумерация сквозная через все блоки
    Задача — id из метки «Часть: <id>#n» в data.часть записи запуска (хук agent_budget_guard.py
    берёт её из первой строки заказа; метка в описании — только старые строки);
    запуски с одним id — одна задача, в одном блоке: есть идущий (не потерянный)
    запуск — ДЕЛАЕТСЯ, иначе СДЕЛАНО (свежесть — как прежде: окно или метка).
    Порядок задач в блоке — по первому запуску. Отступ строк под задачей — 3 пробела.
    Заголовок — title карточки из карты, если в нём есть кириллица; иначе (или карточки
    нет) — описание первого запуска задачи без латинских слов и без ярлыка роли.
    «делали/делают» — одна запись на модель в порядке первого запуска: «<подпись>
    (<роль>, <роль>)», записи через «, »; роли без повторов в порядке вёл, тесты, код,
    проверка, помощник. Роль: тип harness-lead — «вёл» (флаг «ревью» роли не меняет);
    описание начинается с «ревью»/«review» — «проверка» (флаг сам не в счёт); слово
    тест/проба/пробы/test/probe (и «test_») в описании — «тесты»; harness-worker — «код»;
    иной тип без этих примет — «помощник».
    Задача без метки (круг 2): исполнитель наследует задачу родителя-оркестратора; оркестратор
    без метки с кириллицей в описании — своя задача, заголовок из описания: ярлык
    «Оркестратор:» с двоеточием отрезается, без двоеточия описание целиком; в «прочее» — только запуск без метки, без родителя с задачей и без
    кириллицы в описании. Блок ⚠ называет задачу теми же словами, с моделью и временем.
    «этапы» — только сделанные, порядок тест · код · проверка · мутации · коммит:
        тест ✔      $PROJECT_DIR/.tasks/<id>/журнал.md или коммит задачи несут «test_»
        код ✔       законченный запуск с ролью «код» или коммит задачи
        проверка ✔  есть $PROJECT_DIR/.tasks/<id>/harness-code-review.md
        мутации ✔   есть $PROJECT_DIR/.tasks/<id>/мутации.log
        коммит ✔    в git $PROJECT_DIR коммит со строкой «Dev-Map: <id>» (id целиком)
        Нет ни одного — строки «этапы:» нет. В ДЕЛАЕТСЯ — «этап: …» по роли последнего
        идущего запуска (проверка — со словом «проверка»).
    Сверх потолка задачи с конца блока сворачиваются в строку «ещё N задач».
    Боевой harness.conf держит STATUS_STRUCT_MAX_CHARS ≤ 1400 (TONE.md, жёсткий потолок).
    STATUS_ROLE_LABELS форме по задачам не нужен: его значения в отчёт не попадают; из
    боевого harness.conf и system-jobs.yaml ключ убран.

ПО РЕВЬЮ (review/generalist.yaml, решения-ревью-1.md), случаи 64–73:
    Возобновление: {"event":"agent.resume","data":{"вызов":<вызов адресата SendMessage>}}.
    Запуск идёт, если после его последнего agent.finish есть agent.resume. Настоящий конец —
    первый finish после запуска или после resume; повторный finish без resume — не новый конец,
    и задача, уже сообщённая отметкой, в СДЕЛАНО не повторяется (слово 03.11).
    Живость не судится окном: незаконченный непотерянный запуск, начатый до окна, — идёт.
    Отметка по задачам: --отметить засчитывает сообщёнными только задачи, реально показанные
    последней сборкой; свёрнутая в «ещё N задач» сделанная задача приходит в следующем
    статусе. Свёртка сперва снимает «делали/этапы» у задач, затем сами задачи.
    Ведущий без метки, у которого есть дети с задачей, отдельной задачей не выводится.
    «делают:» — только идущие запуски; законченные участники идущей задачи — строкой «делали:».
    Строка 📝 — без id карточек и слова plan. «проверка ✔» — и за законченный запуск с ролью
    «проверка». В режиме окна родитель вне окна читается для наследования задачи.
    Круг 2 ревью, случаи 74–77: идущий ведущий без метки с детьми-задачами — «вёл» в ДЕЛАЕТСЯ
    (под задачей ребёнка), не в ⚠; после конца задачи детей не дублируются. agent.resume без
    единого события работы после него не держит запуск идущим и не роняет его в ⚠ через 2 ч.
    Задача, показанная в СДЕЛАНО/ДЕЛАЕТСЯ, в ДАЛЬШЕ не повторяется. Длинный заголовок
    урезается по границе слова с «…».
    Случай 78: потерянный помощник задачи, уже сданной (карточка test/done/принято или коммит
    с её Dev-Map после его запуска), в ⚠ не показывается.

Канал: `tg_send.sh --статус` БЕЗ текста сам зовёт $PROJECT_DIR/scripts/status-report.py
(на стенде — подставной, который зовёт настоящий сборщик с аргументами из переменных
пробы STAND_*) и отдаёт сторожу check-channel-messages.py `--статус`: потолок
длины — STATUS_STRUCT_MAX_CHARS вместо жёсткого из TONE.md, самокомментарий режется
как всегда. `--статус "<текст>"` — отказ: ручной текст дверью не поднимается.

Всё на своём стенде во временном каталоге: свой harness.conf, журнал, карта,
очередь, паспорт и подставной curl — боевой канал и данные не трогаются.
Механизмы берутся РЯДОМ с пробой (подмена «из-копии» судит копию).

СДЕЛАНО БЕЗ ПОВТОРОВ (приказ 03.11: «если ты один раз написал о сделанном, то больше
писать не нужно»), случаи 17–27:
    STATUS_DONE_SINCE_LAST="1"       harness.conf; "0" или нет ключа — старое окно 12 ч
    $LOG_DIR/status-report.last      метка: одна строка ISO-времени с поясом — момент
                                     прошлого ДОСТАВЛЕННОГО статуса
    status-report.py --отметить ISO  пишет ISO в метку и выходит с кодом 0, отчёт не
                                     собирает; обычная сборка метку не пишет никогда
    tg_send.sh --статус              запоминает время ДО сборки, после подтверждённой
                                     доставки (message_id) зовёт
                                     `python3 "$STATUS_BUILDER" --отметить "<то время>"`;
                                     отказ сторожа или API — метка не двигается
    Режим 1: СДЕЛАНО — запуски, чей agent.finish ПОЗЖЕ метки (судит конец, не запуск);
    метки нет — прежнее окно (--с или 12 ч). Явный --с главнее метки. Нового нет —
    в СДЕЛАНО «с HH:MM нового нет; последний помощник кончил HH:MM» (метка и самый
    поздний agent.finish журнала); концов в журнале нет — «пока нового нет». Идущий оркестратор — в ДЕЛАЕТСЯ, его новые
    законченные исполнители — в СДЕЛАНО под ним, показанные раньше не повторяются;
    законченный оркестратор показан один раз.
    ДАЛЬШЕ — 2–3 первых строки «  N. <id> · <вес> · <почему>» вывода
    `work-queue.py <карта> --порядок`, кроме карточки строки «сейчас делаем: <id> — …»
    и начатых (wip); названия из карты без id; нет таких — строка «очередь пуста».

ПОТЕРЯННЫЕ ЗАПУСКИ (04.11: помощник оборвался на лимите API, agent.finish не записан —
запуск и его оркестратор висели в ДЕЛАЕТСЯ вечно), случаи 36–46:
    STATUS_LOST_HOURS="2"            harness.conf, часы; нет ключа или мусор — 2
    Запуск без конца ПОТЕРЯН, если самая поздняя запись его жизни старше порога по
    настоящим часам. Записи жизни: свой launch; любая запись с data.вызов = его вызов;
    launch и finish потомка (data.родитель — на любую глубину).
    Потерянный — не в ДЕЛАЕТСЯ, а в блоке «⚠ ПОТЕРЯН» между ДЕЛАЕТСЯ и ДАЛЬШЕ: строка на
    запуск (описание и время запуска HH:MM местное), не больше 3, остаток — «и ещё N».
    Потерянных нет — блока нет, вывод прежний. Оркестратор со своим концом, у которого
    не кончили только потерянные, — законченный; без конца со свежим потомком — идёт.

Прогон: python3 scripts/test_status_report.py   (код 0 — все случаи зелёные)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Дверь пробы мутаций: путь к копии сборщика; нет переменной — сборщик рядом с пробой.
REPORT = Path(os.environ.get("HARNESS_STATUS_REPORT_UNDER_TEST") or HERE / "status-report.py")
TG_SEND_FILES = ("tg_send.sh", "limit_mark.py", "check-channel-messages.py", "language-level.py")
TG_SEND_LIBS = ("config.sh", "config.py", "patterns.py")

SESSION = "sessiya-proby"


def day(clock: str) -> str:
    """Час условного дня пробы, чьи 13:35 — настоящие часы в момент вызова: запуск без конца
    старше STATUS_LOST_HOURS по часам потерян, и вчерашний журнал потерял бы всех идущих."""
    hours, minutes = map(int, clock.split(":"))
    start = datetime.now(timezone.utc).astimezone() - timedelta(hours=13, minutes=35)
    return (start + timedelta(hours=hours, minutes=minutes)).isoformat(timespec="seconds")


CEILING = 1700
SMALL_CEILING = 900
FALLBACK_CEILING = 1400     # общий запасной потолок сборщика и сторожа без ключа = жёсткий TONE.md
WIDE_CEILING = 2000         # широкий потолок стенда: ручной текст и «всё влезло»
HARD_CEILING = 1400         # жёсткий потолок обычной вести из TONE.md стенда
LABELS = {"opus": "Opus 9.1", "sonnet": "Sonnet 9.1", "haiku": "Haiku 9.1"}
ROLES = {"main": "Главный оркестратор", "lead": "Оркестратор", "worker": ""}
# Смена значений — вывод обязан пойти за данными, а не за литералом в коде.
OTHER_ROLES = {"main": "Капитан смены", "lead": "Дирижёр", "worker": "Исполнитель"}
OTHER_LABELS = {"opus": "Опус-7", "sonnet": "Сонет-7", "haiku": "Хайку-7"}
HEADERS = ("✅ СДЕЛАНО", "🔄 ДЕЛАЕТСЯ", "⏭ ДАЛЬШЕ")
# Блок потерянных — между ДЕЛАЕТСЯ и ДАЛЬШЕ и только когда есть что показать.
LOST_HEADER = "⚠ ПОТЕРЯН"
CARD_ID = re.compile(r"\b(?:hr|pr)\.[\w-]+")
NEXT_TITLE = "Почистить старые журналы"
WIP_TITLE = "Текущая работа смены"
# Карточки задач стенда: заголовок задачи в отчёте берётся отсюда. Вне очереди — ДАЛЬШЕ прежний.
STAND_CARDS = {
    "hr.demo-format": "Отчёт о статусе по задачам",
    "hr.demo-cleanup": "Уборка старых журналов",
    "hr.demo-status": "Статус в канал понятным языком",
    "hr.demo-english": "Status tree rework",
    "hr.demo-sverka": "Сверка заявок владельца",
    "hr.demo-spravka": "Новая справка для владельца",
    "hr.demo-fresh": "Свежая сверка",
    "hr.demo-old": "Давняя сверка",
    "hr.demo-this": "Работа этой сессии",
    "hr.demo-past": "Работа прошлой сессии",
    "hr.demo-guard": "Охрана удаления: ключи и чужие проекты",
    "hr.demo-chapters": "Правка справки",
    "hr.demo-deep": "Долгая сверка журналов",
    "hr.demo-unload": "Долгая выгрузка журналов",
    "hr.demo-three": "Трёхчасовая работа",
    "hr.demo-half": "Полуторачасовая работа",
    "hr.demo-young": "Молодая сверка заявок",
    "hr.demo-night": "Ночная сверка таблицы",
    "hr.demo-memory": "Правка памяти",
    "hr.demo-oldclean": "Давняя уборка логов",
    "hr.demo-stages": "Сторож убирает забытые открытые порты",
    "hr.demo-bare": "Голая задача без бумаг",
    "hr.demo-ctest": "Проба из коммита",
    "hr.demo-running": "Контроль библиотеки процессов",
    "hr.demo-roles": "Роли помощников словами",
}
TASK_LINE = re.compile(r"^(\d+)\. (.+)$")
OTHER_LINE = re.compile(r"^прочее\D*(\d+)")
MORE_TASKS = re.compile(r"^ещё (\d+) задач")


class Probe:
    def __init__(self) -> None:
        self.checks = 0
        self.failed = 0
        self.cases = 0
        self.failed_cases = 0
        self._case_failed = False

    def start(self, title: str) -> None:
        self.cases += 1
        self._case_failed = False
        print(title)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks += 1
        if ok:
            print(f"  ок    {name}")
        else:
            self.failed += 1
            self._case_failed = True
            print(f"  ПЛОХО {name}: {str(detail)[:600]}")
        return ok

    def finish(self) -> None:
        if self._case_failed:
            self.failed_cases += 1


PROBE = Probe()


# ── стенд ───────────────────────────────────────────────────────────────────

def write_conf(stand: Path, name: str, ceiling: int, roles: dict = ROLES,
               models: dict = LABELS) -> Path:
    """ceiling 0 — ключа STATUS_STRUCT_MAX_CHARS нет; пустой roles — нет STATUS_ROLE_LABELS."""
    labels = ";".join(f"{k}={v}" for k, v in models.items())
    role_labels = ";".join(f"{k}={v}" for k, v in roles.items())
    path = stand / name
    key = f'STATUS_STRUCT_MAX_CHARS="{ceiling}"\n' if ceiling else ""
    role_key = f'STATUS_ROLE_LABELS="{role_labels}"\n' if roles else ""
    path.write_text(key + f'STATUS_MODEL_LABELS="{labels}"\n' + role_key, encoding="utf-8")
    return path


def cards_yaml(cards: dict) -> str:
    """Карточки plan строками задач карты; название в JSON-кавычках — двоеточие не ломает YAML."""
    return "".join(f"  - id: {cid}\n    status: plan\n    title: {json.dumps(title, ensure_ascii=False)}\n"
                   for cid, title in cards.items())


def add_cards(stand: Path, cards: dict) -> None:
    with (stand / "dev-map.yaml").open("a", encoding="utf-8") as out:
        out.write(cards_yaml(cards))


def make_stand() -> Path:
    stand = Path(tempfile.mkdtemp(prefix="proba-statusa-"))
    (stand / "log").mkdir()
    (stand / "project").mkdir()
    write_conf(stand, "harness.conf", CEILING)
    write_conf(stand, "harness-small.conf", SMALL_CEILING)
    write_conf(stand, "harness-wide.conf", WIDE_CEILING)
    write_conf(stand, "harness-nokey.conf", 0, {})
    write_conf(stand, "harness-captain.conf", CEILING, {"main": "Капитан"})
    write_conf(stand, "harness-roles.conf", CEILING, OTHER_ROLES)
    write_conf(stand, "harness-models.conf", CEILING, models=OTHER_LABELS)
    # Смена на haiku нарочно: подпись оркестратора обязана прийти из данных.
    (stand / "models.yaml").write_text(
        "модели:\n  смена: haiku\n  управляющий: opus\n  код: sonnet\n", encoding="utf-8")
    (stand / "dev-map.yaml").write_text(
        "queue: [hr.demo-next, hr.demo-wip]\n"
        "lines:\n"
        "- id: harness\n"
        "  tasks:\n"
        "  - id: hr.demo-wip\n"
        "    status: wip\n"
        f"    title: {WIP_TITLE}\n"
        "  - id: hr.demo-next\n"
        "    status: plan\n"
        f"    title: {NEXT_TITLE}\n" + cards_yaml(STAND_CARDS), encoding="utf-8")
    (stand / "queue.txt").write_text(
        "работа: 2\n"
        "ждут: \n"
        "сейчас делаем: hr.demo-wip — начата\n"
        "  1. hr.demo-wip · 30 · начата\n"
        "  2. hr.demo-next · 20 · в очереди\n", encoding="utf-8")
    return stand


def launch(call: str, desc: str, typ: str, model: str, parent: str = "",
           review: bool = False, ts: str = "",
           session: str = SESSION, outcome: str = "ok", part: str = "") -> dict:
    """part — data.часть: метка «Часть: <id>#n» из первой строки заказа (agent_budget_guard.py)."""
    ts = ts or day("12:00")
    data = {"описание": desc, "тип": typ, "модель": model, "ревью": review, "вызов": call}
    if parent:
        data["родитель"] = parent
    if part:
        data["часть"] = part
    return {"v": 1, "ts": ts, "src": "scripts/hooks/agent_budget_guard.py",
            "event": "agent.launch", "outcome": outcome, "task": "hr.demo-wip",
            "session": session, "data": data}


def finish(call: str, ts: str = "", session: str = SESSION) -> dict:
    ts = ts or day("13:00")
    return {"v": 1, "ts": ts, "src": "scripts/hooks/agent_finish.py", "event": "agent.finish",
            "outcome": "ok", "task": "hr.demo-wip", "session": session, "data": {"вызов": call}}


def write_journal(stand: Path, rows: list, junk: tuple = ()) -> Path:
    path = stand / "log" / "вызовы-агентов.jsonl"
    with path.open("w", encoding="utf-8") as out:
        for line in junk:
            out.write(line + "\n")
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def stand_env(stand: Path, conf: str = "harness.conf") -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_SESSION_ID", "HARNESS_REPORT", "HARNESS_TG_FORCE")}
    env.update(HARNESS_CONF=str(stand / conf), LOG_DIR=str(stand / "log"),
               PROJECT_DIR=str(stand / "project"), HARNESS_HOME=str(stand))
    return env


def report_args(stand: Path, journal: Path) -> list:
    return ["--журнал-вызовов", str(journal),
            "--карта", str(stand / "dev-map.yaml"), "--очередь", str(stand / "queue.txt"),
            "--модели", str(stand / "models.yaml"), "--сессия", SESSION, "--с", day("10:00")]


def run_report(stand: Path, journal: Path, conf: str = "harness.conf",
               args: list = None, env: dict = None, cwd: Path = None) -> str:
    """Текст отчёта; нет модуля или код ≠ 0 — исключение с причиной (красный случай)."""
    if not REPORT.is_file():
        raise RuntimeError(f"нет {REPORT}")
    done = subprocess.run(
        [sys.executable, str(REPORT), *(args if args is not None else report_args(stand, journal))],
        capture_output=True, text=True, timeout=60, env={**stand_env(stand, conf), **(env or {})},
        cwd=str(cwd or stand))
    if done.returncode != 0:
        raise RuntimeError(f"код {done.returncode}: {done.stderr.strip()[-400:]}")
    return done.stdout.rstrip("\n")


def blocks(text: str) -> dict:
    """Заголовок → строки блока. Блоки ищутся РАЗБОРОМ вывода, пустой разбор — провал."""
    found, current = {}, None
    for line in text.splitlines():
        head = next((h for h in HEADERS + (LOST_HEADER,) if line.startswith(h)), None)
        if head:
            current = head
            found[head] = []
        elif current and line.strip():
            found[current].append(line)
    return found


def line_with(lines: list, fragment: str) -> str:
    return next((l for l in lines if fragment in l), "")


def parse_tasks(lines: list) -> list:
    """Задачи блока РАЗБОРОМ: «N. заголовок» и строки под ней с отступом в 3 пробела."""
    tasks = []
    for line in lines:
        match = TASK_LINE.match(line)
        if match:
            tasks.append({"num": int(match.group(1)), "title": match.group(2), "rest": []})
        elif tasks and line.startswith("   ") and not line.startswith("    "):
            tasks[-1]["rest"].append(line[3:])
    return tasks


def task_of(lines: list, title: str) -> dict:
    """Задача блока с этим заголовком (начало строки); нет — пустой словарь."""
    return next((t for t in parse_tasks(lines) if t["title"].startswith(title)), {})


def field(task: dict, head: str) -> str:
    """Строка задачи, начатая с «head:», без него; нет — «»."""
    return next((l[len(head) + 1:].strip() for l in task.get("rest", [])
                 if l.startswith(head + ":")), "")


def who_pairs(text: str, labels: dict = LABELS) -> list:
    """(подпись, роль) из строки «делали/делают»: «Подпись (роль, роль), …»."""
    pairs = []
    for match in re.finditer(r"([^,()]+?) \(([^)]*)\)", text):
        label = match.group(1).strip(" ,")
        pairs += [(label, role.strip()) for role in match.group(2).split(",")]
    return pairs


def latin_words(text: str, labels: dict = LABELS) -> list:
    """Латинские слова отчёта, кроме подписей моделей: остаток — английские ярлыки."""
    for label in labels.values():
        text = text.replace(label, "")
    return re.findall(r"[A-Za-z_]{2,}", text)


def other_count(lines: list):
    """Число в строке «прочее N»; строки нет — None."""
    match = next((OTHER_LINE.match(l) for l in lines if OTHER_LINE.match(l)), None)
    return int(match.group(1)) if match else None


def all_titles(text: str) -> list:
    return [m.group(2) for m in map(TASK_LINE.match, text.splitlines()) if m]


# ── основной расклад ────────────────────────────────────────────────────────
# Задача «формат» (L1) идёт: W1 кончил, W2 и W3 (ревью) работают.
# Задача «уборка» (L2) кончила вместе с W4 (haiku) и W5. O1 — исполнитель без метки.
FORMAT = STAND_CARDS["hr.demo-format"]
CLEANUP = STAND_CARDS["hr.demo-cleanup"]


def main_rows() -> list:
    return [
        launch("c-L1", "Часть: hr.demo-format#1 Оркестратор: формат отчёта", "harness-lead", "opus",
               ts=day("11:00")),
        launch("c-W1", "Часть: hr.demo-format#1 Пробы формата для hr.demo-card", "harness-worker",
               "opus", "c-L1"),
        finish("c-W1"),
        launch("c-W2", "Часть: hr.demo-format#2 Код сборщика отчёта", "harness-worker", "sonnet",
               "c-L1", ts=day("13:10")),
        launch("c-W3", "Часть: hr.demo-format#3 Ревью сборщика отчёта", "harness-worker", "opus",
               "c-L1", review=True, ts=day("13:20")),
        launch("c-L2", "Часть: hr.demo-cleanup#1 Управляющий: уборка журналов", "harness-lead",
               "opus", ts=day("11:30")),
        launch("c-W4", "Часть: hr.demo-cleanup#1 Подсчёт старых файлов", "harness-worker", "haiku",
               "c-L2"),
        launch("c-W5", "Часть: hr.demo-cleanup#2 удаление по списку", "harness-worker",
               "sonnet", "c-L2", ts=day("12:10")),
        finish("c-W4"), finish("c-W5"), finish("c-L2", ts=day("13:30")),
        launch("c-O1", "Shift handoff notes", "harness-worker", "sonnet",
               ts=day("12:30")),
        finish("c-O1", ts=day("12:50")),
        # Шум: каждый обязан выпасть из отчёта и из счёта «прочее».
        launch("c-N1", "Часть: hr.demo-old#1 Отказанный запуск", "harness-worker", "sonnet",
               outcome="refused"),
        launch("c-N2", "Запуск чужой сессии", "harness-worker", "sonnet", session="chuzhaya"),
        launch("c-N3", "Давний запуск", "harness-worker", "sonnet",
               ts=day("08:00")),
        {"v": 1, "ts": day("12:00"), "event": "agent.launch", "outcome": "ok",
         "session": SESSION, "data": {"описание": "Запуск старого формата", "ревью": False}},
        {"v": 1, "ts": day("12:00"), "event": "agent.launch", "outcome": "ok",
         "session": SESSION, "data": "{'описание': 'Строковые данные'}"},
        finish("c-unknown"),
    ]


NOISE = ("Отказанный запуск", "Запуск чужой сессии", "Давний запуск",
         "Запуск старого формата", "Строковые данные", STAND_CARDS["hr.demo-old"])
JUNK = ("не json вовсе", '{"ts": "2026-11-03T12:00:00+05:00", "повод": "чужая строка журнала"}')


def case_empty_doing(stand: Path) -> None:
    rows = [launch("c-O1", "Shift handoff notes", "harness-worker", "sonnet"), finish("c-O1")]
    out = run_report(stand, write_journal(stand, rows))
    got = blocks(out)
    PROBE.check("три заголовка блоков по порядку", list(got) == list(HEADERS), out)
    doing = got.get("🔄 ДЕЛАЕТСЯ", [])
    PROBE.check("ДЕЛАЕТСЯ пишет «пока ничего»", doing == ["пока ничего"], doing)
    done = got.get("✅ СДЕЛАНО", [])
    PROBE.check("законченный исполнитель без метки — в СДЕЛАНО строкой «прочее 1»",
                other_count(done) == 1 and not parse_tasks(done), done or out)
    PROBE.check("описание запуска без метки в отчёт не попало", "handoff" not in out, out)


def case_nesting(stand: Path) -> None:
    out = run_report(stand, write_journal(stand, main_rows(), JUNK))
    got = blocks(out)
    done, doing = got.get("✅ СДЕЛАНО", []), got.get("🔄 ДЕЛАЕТСЯ", [])
    PROBE.check("СДЕЛАНО: законченная задача «уборка» — заголовком из карты",
                bool(task_of(done, CLEANUP)), done or out)
    PROBE.check("ДЕЛАЕТСЯ: задача «формат» с идущими запусками — заголовком из карты",
                bool(task_of(doing, FORMAT)), doing or out)
    titles = all_titles(out)
    PROBE.check("каждая задача — одна строка на весь отчёт (не в двух блоках)",
                titles.count(FORMAT) == 1 and titles.count(CLEANUP) == 1, titles)
    PROBE.check("нумерация сквозная 1..N по порядку строк",
                [t["num"] for h in HEADERS for t in parse_tasks(got.get(h, []))]
                == list(range(1, len(titles) + 1)), titles)
    PROBE.check("под задачей СДЕЛАНО — строка «делали:», под ДЕЛАЕТСЯ — «делают:»",
                bool(field(task_of(done, CLEANUP), "делали"))
                and bool(field(task_of(doing, FORMAT), "делают")), out)
    pairs = who_pairs(field(task_of(done, CLEANUP), "делали"))
    need = [(LABELS["opus"], "вёл"), (LABELS["haiku"], "код"), (LABELS["sonnet"], "код")]
    PROBE.check("«уборка»: делали Opus (вёл), Haiku (код), Sonnet (код) — по порядку запусков",
                pairs == need, pairs)
    pairs = who_pairs(field(task_of(doing, FORMAT), "делают"))
    PROBE.check("«формат»: в «делают» вёл, код и проверка с подписями моделей",
                all(p in pairs for p in ((LABELS["opus"], "вёл"), (LABELS["sonnet"], "код"),
                                         (LABELS["opus"], "проверка"))), pairs)
    PROBE.check("исполнитель без метки — «прочее 1» в СДЕЛАНО", other_count(done) == 1, done)


def long_cards(count: int, words: tuple) -> dict:
    return {f"hr.demo-long-{i}": f"Задача номер {i}: " + " ".join(
        words[(i + k) % len(words)] for k in range(8)) for i in range(count)}


def long_journal(stand: Path, models: tuple = ("opus", "sonnet")) -> Path:
    """Сорок задач (оркестратор и исполнитель у каждой) и длинное ДАЛЬШЕ: отчёт обязан сворачиваться."""
    words = ("сверка", "журнала", "вызовов", "агентов", "по", "сессии", "и", "окну",
             "времени", "со", "сборкой", "дерева", "управляющих", "исполнителей")
    cards = long_cards(40, words)
    add_cards(stand, cards)
    rows = []
    for i, cid in enumerate(cards):
        rows.append(launch(f"c-L{i}", f"Часть: {cid}#1 Оркестратор: длинная работа", "harness-lead",
                           "opus", ts=day("11:00")))
        rows.append(launch(f"c-D{i}", f"Часть: {cid}#2 " + " ".join(words[:6]), "harness-worker",
                           models[i % len(models)], f"c-L{i}"))
        if i < 30:
            rows += [finish(f"c-D{i}"), finish(f"c-L{i}")]
    long_title = NEXT_TITLE + " " + " ".join(words) + " целиком без остатка"
    dev_map = stand / "dev-map.yaml"
    dev_map.write_text(dev_map.read_text(encoding="utf-8").replace(
        f"title: {NEXT_TITLE}", f"title: {long_title}"), encoding="utf-8")
    return write_journal(stand, rows)


def case_ceiling(stand: Path) -> None:
    journal = long_journal(stand)
    for conf, ceiling in (("harness.conf", CEILING), ("harness-small.conf", SMALL_CEILING)):
        out = run_report(stand, journal, conf)
        PROBE.check(f"длина {len(out)} ≤ потолка {ceiling} из STATUS_STRUCT_MAX_CHARS",
                    0 < len(out) <= ceiling, len(out))
        wide = [l for l in out.splitlines() if len(l) > 80]
        PROBE.check(f"потолок {ceiling}: каждая строка ≤ 80 знаков", not wide, wide[:2])
        PROBE.check(f"потолок {ceiling}: все три блока на месте",
                    list(blocks(out)) == list(HEADERS), out[:300])
        PROBE.check(f"потолок {ceiling}: ДАЛЬШЕ не съеден обрезкой",
                    NEXT_TITLE[:20] in "\n".join(blocks(out).get("⏭ ДАЛЬШЕ", [])), out[-300:])
        PROBE.check(f"потолок {ceiling}: показаны задачи, а не свёрнуто всё",
                    len(all_titles(out)) > 1, out[:300])


def case_roles(stand: Path) -> None:
    rows = [
        launch("c-L", "Часть: hr.demo-roles#1 Оркестратор: ревью и правки", "harness-lead", "opus",
               review=True, ts=day("11:00")),
        launch("c-T", "Часть: hr.demo-roles#2 test_status_report: новые случаи", "harness-worker",
               "sonnet", "c-L", ts=day("11:10")),
        launch("c-C", "Часть: hr.demo-roles#3 Сборщик статуса", "harness-worker", "haiku", "c-L",
               ts=day("11:20")),
        launch("c-R", "Часть: hr.demo-roles#4 Ревью сборщика", "harness-worker", "opus", "c-L",
               review=True, ts=day("11:30")),
        launch("c-G", "Часть: hr.demo-roles#5 Разбор вывода", "general-purpose", "sonnet", "c-L",
               ts=day("11:40")),
    ]
    rows += [finish(c, ts=day("12:30")) for c in ("c-T", "c-C", "c-R", "c-G", "c-L")]
    out = run_report(stand, write_journal(stand, rows))
    task = task_of(blocks(out).get(HEADERS[0], []), STAND_CARDS["hr.demo-roles"])
    pairs = who_pairs(field(task, "делали"))
    need = [(LABELS["opus"], "вёл"), (LABELS["opus"], "проверка"), (LABELS["sonnet"], "тесты"), (LABELS["sonnet"], "помощник"),
            (LABELS["haiku"], "код")]
    PROBE.check("роли: lead — вёл (флаг ревью не в счёт), test_ — тесты, worker — код, "
                "ревью — проверка, иной тип — помощник", pairs == need, pairs or out)
    PROBE.check("в отчёте нет иконок и слов прежнего дерева",
                not any(s in out for s in ("🧭", "🎼", "├", "└", "Оркестратор", "оркестратор")), out)


def case_role_prefix(stand: Path) -> None:
    # Карточки нет: заголовок — описание первого запуска, ярлык роли из него убран.
    rows = [launch("c-L", "Часть: hr.demo-nocard#1 Оркестратор: выгрузка заявок", "harness-lead",
                   "opus", ts=day("12:30")),
            launch("c-M", "Часть: hr.demo-nocard2#1 Управляющий: правка справки", "harness-lead",
                   "opus", ts=day("12:35"))]
    out = run_report(stand, write_journal(stand, rows))
    titles = all_titles(out)
    for fragment in ("выгрузка заявок", "правка справки"):
        PROBE.check(f"«{fragment}»: заголовок без карточки — описание без ярлыка роли",
                    any(fragment in t.lower() for t in titles[:2]) and "Оркестратор" not in out
                    and "Управляющий" not in out, titles or out)


def case_role_labels_from_conf(stand: Path) -> None:
    out = run_report(stand, write_journal(stand, main_rows(), JUNK), "harness-models.conf")
    task = task_of(blocks(out).get(HEADERS[0], []), CLEANUP)
    pairs = who_pairs(field(task, "делали"))
    PROBE.check("подписи моделей из STATUS_MODEL_LABELS: смена значений — вывод за данными",
                pairs[:1] == [(OTHER_LABELS["opus"], "вёл")]
                and not any(v in out for v in LABELS.values()), pairs or out)
    out = run_report(stand, write_journal(stand, main_rows(), JUNK), "harness-roles.conf")
    PROBE.check("значения STATUS_ROLE_LABELS в форму по задачам не попадают",
                not any(v in out for v in ("Капитан смены", "Дирижёр", "Исполнитель")), out)


def case_role_labels_default(stand: Path) -> None:
    journal = write_journal(stand, main_rows(), JUNK)
    # run_report бросает при коде ≠ 0 — дошедший до проверок вызов вернул 0.
    for conf in ("harness-nokey.conf", "harness-captain.conf"):
        out = run_report(stand, journal, conf)
        PROBE.check(f"{conf}: задачи на месте, слов ролей прежнего дерева нет",
                    FORMAT in all_titles(out) and CLEANUP in all_titles(out)
                    and "оркестратор" not in out.lower() and "Капитан" not in out, out)


def case_no_ids(stand: Path) -> None:
    out = run_report(stand, write_journal(stand, main_rows(), JUNK))
    PROBE.check("нет id карточек", not CARD_ID.search(out), CARD_ID.findall(out))
    PROBE.check("нет «Часть:»", "Часть:" not in out, out)
    PROBE.check("задачи названы заголовками карты, а не выброшены",
                FORMAT in out and CLEANUP in out, out)


def tg_stand(stand: Path) -> Path:
    tree = stand / "harnes"
    (tree / "scripts" / "lib").mkdir(parents=True)
    (tree / "память").mkdir()
    for name in TG_SEND_FILES:
        shutil.copy(HERE / name, tree / "scripts" / name)
    for name in TG_SEND_LIBS:
        if (HERE / "lib" / name).is_file():
            shutil.copy(HERE / "lib" / name, tree / "scripts" / "lib" / name)
    # Сторож канала читает признаки вопроса о согласии рядом с собой: в проекте — harness/config, в пакете — config.
    signs = next((p for p in (HERE.parent / "harness" / "config" / "consent-ask-signs.yaml",
                              HERE.parent / "config" / "consent-ask-signs.yaml") if p.is_file()), None)
    if signs:
        (tree / "harness" / "config").mkdir(parents=True)
        shutil.copy(signs, tree / "harness" / "config" / signs.name)
    (tree / "память" / "TONE.md").write_text(
        "# Тон\n\n<!-- потолки: мягкий=700 жёсткий=1400 -->\n", encoding="utf-8")
    (stand / "bin").mkdir()
    curl = stand / "bin" / "curl"
    curl.write_text("#!/usr/bin/env bash\nprintf 'vyzvan\\n' >> \"$STAND_CURL_LOG\"\n"
                    "printf '{\"ok\":true,\"result\":{\"message_id\":1}}'\n", encoding="utf-8")
    curl.chmod(0o755)
    key = stand / "klyuch"
    key.write_text("11" + os.urandom(16).hex() + "\n", encoding="utf-8")
    (stand / "install.conf").write_text(
        'TG_CHAT_ID="100500"\n'                      # не-секрет: выдуманный чат стенда
        f'TG_TOKEN_FILE="{key}"\n'                   # не-секрет: путь в стенде
        f'PROJECT_DIR="{tree}"\nLOG_DIR="{stand / "log"}"\nSECRETS_DIR="{stand}"\n'
        'TMUX_SESSION="proba-statusa"\nAGENT_START_CMD="true"\n', encoding="utf-8")
    return tree


# Подставной сборщик на месте $PROJECT_DIR/scripts/status-report.py стенда: зовёт
# НАСТОЯЩИЙ сборщик с аргументами стенда и дописывает хвост — так сторож судит
# самокомментарий и превышение потолка на боевом пути без аргумента.
BUILDER_STUB = """#!/usr/bin/env python3
import json, os, subprocess, sys
with open(os.environ["STAND_BUILDER_LOG"], "a", encoding="utf-8") as log:
    log.write("vyzvan\\n")
done = subprocess.run([sys.executable, os.environ["STAND_REAL_REPORT"],
                       *json.loads(os.environ["STAND_REPORT_ARGS"])],
                      capture_output=True, text=True)
sys.stderr.write(done.stderr)
sys.stdout.write(done.stdout.rstrip("\\n") + os.environ.get("STAND_STATUS_TAIL", "") + "\\n")
sys.exit(done.returncode)
"""


def install_builder(stand: Path, tree: Path, journal: Path, tail: str = "") -> dict:
    """Переменные пробы для подставного сборщика; вернёт их для send()."""
    stub = tree / "scripts" / "status-report.py"
    stub.write_text(BUILDER_STUB, encoding="utf-8")
    stub.chmod(0o755)
    return {"STAND_BUILDER_LOG": str(stand / "log" / "builder.log"),
            "STAND_REAL_REPORT": str(REPORT),
            "STAND_REPORT_ARGS": json.dumps(report_args(stand, journal), ensure_ascii=False),
            "STAND_STATUS_TAIL": tail}


def builder_calls(stand: Path) -> int:
    path = stand / "log" / "builder.log"
    return len(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else 0


def builder_argv(stand: Path) -> list:
    """Аргументы каждого вызова подставного MARK_STUB; [] — сборка без аргументов."""
    path = stand / "log" / "builder.log"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def send(stand: Path, tree: Path, *args: str, conf: str = "harness.conf",
         extra: dict = None) -> tuple:
    """(код, вызовов curl, текст последней строки tg_out, вывод)."""
    curl_log = stand / "log" / "curl.log"
    curl_log.write_text("", encoding="utf-8")
    env = stand_env(stand, conf)
    env.update(PATH=f"{stand / 'bin'}:{env.get('PATH', '')}", STAND_CURL_LOG=str(curl_log),
               PROJECT_DIR=str(tree), HARNESS_INSTALL_CONF=str(stand / "install.conf"),
               HEARTBEAT_DIR=str(stand / "log" / "heartbeat"),
               CLAUDE_PROJECTS_DIR=str(stand / "net"))
    env.update(extra or {})
    done = subprocess.run(["bash", str(tree / "scripts" / "tg_send.sh"), *args],
                          capture_output=True, text=True, timeout=120, env=env)
    calls = len(curl_log.read_text(encoding="utf-8").splitlines())
    out_log = stand / "log" / "tg_out.jsonl"
    last = ""
    if out_log.is_file() and out_log.read_text(encoding="utf-8").strip():
        try:
            last = json.loads(out_log.read_text(encoding="utf-8").splitlines()[-1]).get("text", "")
        except ValueError:
            last = ""
    return done.returncode, calls, last, done.stdout + done.stderr


OVER_TAIL = "\n" + "\n".join(["└ 🛠 Sonnet 9.1: лишняя строка сверх потолка"] * 40)


def case_channel(stand: Path) -> None:
    tree = tg_stand(stand)
    rc, calls, last, out = send(stand, tree, "Ворота зелёные, беру следующую задачу.")
    PROBE.check("условие стенда: короткое сообщение доставлено", rc == 0 and calls == 1, out)
    journal = long_journal(stand)
    status = run_report(stand, journal)
    PROBE.check(f"условие: статус длиннее {HARD_CEILING} ({len(status)}) и не выше {CEILING}",
                HARD_CEILING < len(status) <= CEILING, len(status))
    plain = ("Работа идёт по плану, сборщик отчёта почти готов. " * 60)[:len(status)]
    rc, calls, _, out = send(stand, tree, plain)
    PROBE.check("обычное сообщение той же длины без метки — отказ",
                rc != 0 and calls == 0 and str(HARD_CEILING) in out,
                f"код {rc}, curl {calls}: {out[-300:]}")
    rc, calls, _, out = send(stand, tree, "--статус", plain)
    PROBE.check("--статус на тексте без трёх блоков — отказ (дверь не обход)",
                rc != 0 and calls == 0, f"код {rc}, curl {calls}: {out[-300:]}")
    extra = install_builder(stand, tree, journal, "\nЭто моя ошибка, признаю.")
    rc, calls, _, out = send(stand, tree, "--статус", extra=extra)
    PROBE.check("--статус: сборщик с самокомментарием — отказ «разбор своих ошибок»",
                rc != 0 and calls == 0 and "разбор своих ошибок" in out,
                f"код {rc}, curl {calls}: {out[-300:]}")
    extra = install_builder(stand, tree, journal, OVER_TAIL)
    rc, calls, _, out = send(stand, tree, "--статус", extra=extra)
    PROBE.check(f"--статус: сборщик длиннее {CEILING} — отказ с числом потолка",
                rc != 0 and calls == 0 and str(CEILING) in out,
                f"код {rc}, curl {calls}: {out[-300:]}")
    PROBE.check("условие: сборщик позван обоими отказами", builder_calls(stand) == 2,
                builder_calls(stand))


def manual_status(length: int) -> str:
    """Ручной текст с тремя заголовками и деревом — по виду неотличим от собранного."""
    body = [HEADERS[0], f"🧭 Главный оркестратор {LABELS['opus']}"]
    body += [f"├ 🛠 {LABELS['sonnet']}: ручная строка номер {i} о сделанном" for i in range(60)]
    text = "\n".join(body)[:length - 80]
    return text + f"\n{HEADERS[1]}\nпока ничего\n{HEADERS[2]}\n🎼 Оркестратор {LABELS['opus']}: дальше"


def case_manual_refused(stand: Path) -> None:
    tree = tg_stand(stand)
    manual = manual_status(1800)
    PROBE.check(f"условие: ручной текст {len(manual)} знаков, три заголовка, ниже "
                f"{WIDE_CEILING}", 1700 < len(manual) < WIDE_CEILING
                and list(blocks(manual)) == list(HEADERS), len(manual))
    rc, calls, last, out = send(stand, tree, "--статус", manual, conf="harness-wide.conf")
    PROBE.check("--статус «<ручной текст>» — отказ, в канал ничего (статус собирает только код)",
                rc != 0 and calls == 0 and last != manual, f"код {rc}, curl {calls}: {out[-300:]}")


def case_door_builds(stand: Path) -> None:
    tree = tg_stand(stand)
    journal = long_journal(stand)
    status = run_report(stand, journal)
    PROBE.check(f"условие: собранный статус длиннее {HARD_CEILING} ({len(status)})",
                HARD_CEILING < len(status) <= CEILING, len(status))
    extra = install_builder(stand, tree, journal)
    # Подставной с журналом аргументов: сборку и --отметить надо различить.
    (tree / "scripts" / "status-report.py").write_text(MARK_STUB, encoding="utf-8")
    rc, calls, last, out = send(stand, tree, "--статус", extra=extra)
    argv = builder_argv(stand)
    PROBE.check("--статус без текста собрал отчёт ровно раз", argv.count([]) == 1, argv)
    PROBE.check("--статус без текста: собранный текст ушёл (код 0, curl 1, текст = сборщик)",
                rc == 0 and calls == 1 and last == status,
                f"код {rc}, curl {calls}, совпал {last == status}: {out[-400:]}")
    # Ключа STATUS_DONE_SINCE_LAST в harness.conf стенда нет: метка пишется всё равно.
    marks = [a for a in argv if a[:1] == ["--отметить"]]
    PROBE.check("после доставки сборщик позван с --отметить ровно раз, других вызовов нет",
                len(marks) == 1 and len(argv) == 2, argv)
    PROBE.check("без ключа режима метка записана ISO-временем",
                parse_mark(read_mark(stand)) is not None, read_mark(stand) or "метки нет")


def case_shared_fallback(stand: Path) -> None:
    tree = tg_stand(stand)
    journal = long_journal(stand)
    status = run_report(stand, journal, "harness-nokey.conf")
    PROBE.check(f"без ключа сборщик держит запасной {FALLBACK_CEILING}: "
                f"700 < {len(status)} ≤ {FALLBACK_CEILING}",
                700 < len(status) <= FALLBACK_CEILING, len(status))
    extra = install_builder(stand, tree, journal)
    rc, calls, last, out = send(stand, tree, "--статус", conf="harness-nokey.conf", extra=extra)
    PROBE.check(f"без ключа сторож берёт тот же {FALLBACK_CEILING}: собранный статус ушёл",
                rc == 0 and calls == 1 and last == status, f"код {rc}, curl {calls}: {out[-400:]}")
    extra = install_builder(stand, tree, journal, OVER_TAIL)
    rc, calls, _, out = send(stand, tree, "--статус", conf="harness-nokey.conf", extra=extra)
    PROBE.check(f"без ключа статус длиннее {FALLBACK_CEILING} — отказ с этим числом",
                rc != 0 and calls == 0 and str(FALLBACK_CEILING) in out,
                f"код {rc}, curl {calls}: {out[-300:]}")


def case_next(stand: Path) -> None:
    out = run_report(stand, write_journal(stand, main_rows(), JUNK))
    nxt = blocks(out).get("⏭ ДАЛЬШЕ", [])
    text = "\n".join(nxt)
    PROBE.check("ДАЛЬШЕ называет следующую карточку очереди по названию", NEXT_TITLE in text, nxt)
    PROBE.check("ДАЛЬШЕ — не начатая карточка", WIP_TITLE not in text, nxt)
    shown = len([t for h in HEADERS[:2] for t in parse_tasks(blocks(out).get(h, []))])
    PROBE.check("ДАЛЬШЕ: «<следующий номер>. <название>» без подписи модели и роли",
                nxt[:1] == [f"{shown + 1}. {NEXT_TITLE}"], nxt)


def case_journal(stand: Path) -> None:
    out = run_report(stand, write_journal(stand, main_rows(), JUNK))
    got = blocks(out)
    done = [t["title"] for t in parse_tasks(got.get("✅ СДЕЛАНО", []))]
    doing = [t["title"] for t in parse_tasks(got.get("🔄 ДЕЛАЕТСЯ", []))]
    PROBE.check("все запуски задачи с концом → задача в СДЕЛАНО, не в ДЕЛАЕТСЯ",
                CLEANUP in done and CLEANUP not in doing, out)
    PROBE.check("есть запуск без конца → задача в ДЕЛАЕТСЯ, не в СДЕЛАНО",
                FORMAT in doing and FORMAT not in done, out)
    leaked = [n for n in NOISE if n in out]
    PROBE.check("отказ, чужая сессия, давнее, старый формат — не показаны", not leaked, leaked)
    PROBE.check("шум не посчитан в «прочее»: ровно 1",
                other_count(got.get("✅ СДЕЛАНО", [])) == 1
                and other_count(got.get("🔄 ДЕЛАЕТСЯ", [])) is None, out)


def case_icon_by_type(stand: Path) -> None:
    # Роль — по типу и началу описания, не по модели и не по флагу «ревью»: хук ставит флаг
    # по слову где угодно, а «Сверка по ревью списка» — это код.
    rows = [
        launch("c-L", "Часть: hr.demo-running#1 Правки двери статуса", "harness-lead", "sonnet",
               ts=day("12:30")),
        launch("c-O", "Часть: hr.demo-running#2 Дверь статуса", "harness-worker", "opus", "c-L",
               ts=day("12:40")),
        launch("c-H", "Часть: hr.demo-running#3 Сверка по ревью списка", "harness-worker", "haiku",
               "c-L", review=True, ts=day("12:50")),
        launch("c-R", "Часть: hr.demo-running#4 Ревью двери статуса", "harness-worker", "sonnet",
               "c-L", ts=day("13:00")),
    ]
    out = run_report(stand, write_journal(stand, rows))
    task = task_of(blocks(out).get(HEADERS[1], []), STAND_CARDS["hr.demo-running"])
    pairs = who_pairs(field(task, "делают"))
    PROBE.check("sonnet-оркестратор — вёл, opus — код, «Сверка по ревью» с флагом — код, "
                "«Ревью …» без флага — проверка",
                pairs == [(LABELS["sonnet"], "вёл"), (LABELS["sonnet"], "проверка"), (LABELS["opus"], "код"),
                          (LABELS["haiku"], "код")], pairs or out)
    PROBE.check("ДЕЛАЕТСЯ: «этап:» по последнему идущему — проверка",
                "проверка" in field(task, "этап"), task or out)


def case_collapsed_signed(stand: Path) -> None:
    journal = long_journal(stand)
    total = 40
    for conf, ceiling in (("harness.conf", CEILING), ("harness-small.conf", SMALL_CEILING)):
        out = run_report(stand, journal, conf)
        more = [int(m.group(1)) for m in map(MORE_TASKS.match, out.splitlines()) if m]
        shown = len([t for h in HEADERS[:2] for t in parse_tasks(blocks(out).get(h, []))])
        PROBE.check(f"потолок {ceiling}: условие — есть строка «ещё N задач»", bool(more), out[-400:])
        PROBE.check(f"потолок {ceiling}: показано {shown} + свёрнуто {sum(more)} = {total} задач",
                    shown + sum(more) == total, f"показано {shown}, ещё {more}")
        wide = [l for l in out.splitlines() if MORE_TASKS.match(l) and len(l) > 80]
        PROBE.check(f"потолок {ceiling}: «ещё N задач» ≤ 80 знаков", not wide, wide)


# ── сделано без повторов ────────────────────────────────────────────────────
MARK_NAME = "status-report.last"
NOTHING_NEW = "нового нет; последний помощник кончил"
QUEUE_EMPTY = "очередь пуста"
DONE_ONCE = (STAND_CARDS["hr.demo-cleanup"],)


def mode_conf(stand: Path, mode: str) -> str:
    """Конф стенда с ключом режима; остальное — как у основного harness.conf стенда."""
    name = f"harness-since-{mode}.conf"
    base = (stand / "harness.conf").read_text(encoding="utf-8")
    (stand / name).write_text(base + f'STATUS_DONE_SINCE_LAST="{mode}"\n', encoding="utf-8")
    return name


def at(minutes_ago: float) -> str:
    """Время относительно настоящих часов: метку tg_send пишет по часам, не по журналу."""
    moment = datetime.now(timezone.utc).astimezone() - timedelta(minutes=minutes_ago)
    return moment.isoformat(timespec="seconds")


def mark_path(stand: Path) -> Path:
    return stand / "log" / MARK_NAME


def set_mark(stand: Path, moment: str) -> None:
    mark_path(stand).write_text(moment + "\n", encoding="utf-8")


def read_mark(stand: Path) -> str:
    path = mark_path(stand)
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def parse_mark(text: str):
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def since_args(stand: Path, journal: Path) -> list:
    """Аргументы стенда БЕЗ --с: окно сделанного задаёт метка, а не вызов."""
    args = report_args(stand, journal)
    cut = args.index("--с")
    return args[:cut] + args[cut + 2:]


def lead_rows() -> list:
    """«Формат» идёт: W1 кончил 90 минут назад, W2 работает. «Уборка» кончила целиком вместе с W5."""
    return [
        launch("c-L1", "Часть: hr.demo-format#1 Оркестратор: формат отчёта", "harness-lead", "opus",
               ts=at(180)),
        launch("c-W1", "Часть: hr.demo-format#1 Пробы формата отчёта", "harness-worker", "opus",
               "c-L1", ts=at(170)),
        finish("c-W1", ts=at(90)),
        launch("c-W2", "Часть: hr.demo-format#2 Код сборщика отчёта", "harness-worker", "sonnet",
               "c-L1", ts=at(80)),
        launch("c-L2", "Часть: hr.demo-cleanup#1 Оркестратор: уборка журналов", "harness-lead",
               "opus", ts=at(170)),
        launch("c-W5", "Часть: hr.demo-cleanup#2 Удаление по списку", "harness-worker", "sonnet",
               "c-L2", ts=at(160)),
        finish("c-W5", ts=at(70)),
        finish("c-L2", ts=at(60)),
    ]


def report_since(stand: Path, journal: Path, conf: str) -> tuple:
    out = run_report(stand, journal, conf, since_args(stand, journal))
    got = blocks(out)
    return out, got.get(HEADERS[0], []), got.get(HEADERS[1], [])


def case_child_once(stand: Path) -> None:
    conf = mode_conf(stand, "1")
    journal = write_journal(stand, lead_rows())
    set_mark(stand, at(240))
    _, done, doing = report_since(stand, journal, conf)
    PROBE.check("метка до конца W1: задача с идущим W2 — в ДЕЛАЕТСЯ, не в СДЕЛАНО",
                bool(task_of(doing, FORMAT)) and not task_of(done, FORMAT), done + doing)
    set_mark(stand, at(0))
    out, done, doing = report_since(stand, journal, conf)
    PROBE.check("метка после конца W1: задача осталась в ДЕЛАЕТСЯ со строкой «делают:»",
                bool(field(task_of(doing, FORMAT), "делают")), doing)
    PROBE.check("в СДЕЛАНО идущей задачи нет", not task_of(done, FORMAT), done)
    PROBE.check("задача показана один раз", all_titles(out).count(FORMAT) == 1, out)


def case_lead_once(stand: Path) -> None:
    conf = mode_conf(stand, "1")
    journal = write_journal(stand, lead_rows())
    set_mark(stand, at(240))
    out, done, doing = report_since(stand, journal, conf)
    PROBE.check("условие: законченная задача в СДЕЛАНО, не в ДЕЛАЕТСЯ",
                bool(task_of(done, CLEANUP)) and not task_of(doing, CLEANUP), out)
    set_mark(stand, at(0))
    out, _, _ = report_since(stand, journal, conf)
    PROBE.check("показанная законченная задача исчезла целиком", CLEANUP not in out, out)


def case_lead_before_mark(stand: Path) -> None:
    # Оркестратор кончил ДО метки, а его исполнитель ещё идёт (L3) или кончил ПОСЛЕ (L4).
    conf = mode_conf(stand, "1")
    rows = [
        launch("c-L3", "Часть: hr.demo-sverka#1 Оркестратор: сверка заявок", "harness-lead", "opus",
               ts=at(200)),
        launch("c-W6", "Часть: hr.demo-sverka#2 Долгая сверка заявок", "harness-worker", "sonnet",
               "c-L3", ts=at(100)),
        finish("c-L3", ts=at(60)),
        launch("c-L4", "Часть: hr.demo-spravka#1 Оркестратор: правка справки", "harness-lead",
               "opus", ts=at(190)),
        launch("c-W7", "Часть: hr.demo-spravka#2 Новая глава справки", "harness-worker", "sonnet",
               "c-L4", ts=at(90)),
        finish("c-L4", ts=at(50)),
        finish("c-W7", ts=at(10)),
    ]
    journal = write_journal(stand, rows)
    set_mark(stand, at(30))
    out, done, doing = report_since(stand, journal, conf)
    sverka, spravka = STAND_CARDS["hr.demo-sverka"], STAND_CARDS["hr.demo-spravka"]
    PROBE.check("задача с идущим исполнителем — в ДЕЛАЕТСЯ", bool(task_of(doing, sverka)), doing)
    PROBE.check("оркестратор, кончивший до метки, без нового — задача не в СДЕЛАНО",
                not task_of(done, sverka), done)
    PROBE.check("исполнитель кончил после метки — задача в СДЕЛАНО, не в ДЕЛАЕТСЯ",
                bool(task_of(done, spravka)) and not task_of(doing, spravka), out)
    PROBE.check("каждая задача показана один раз",
                all_titles(out).count(spravka) == 1 and all_titles(out).count(sverka) == 1, out)


def case_nothing_new(stand: Path) -> None:
    conf = mode_conf(stand, "1")
    journal = write_journal(stand, lead_rows())
    set_mark(stand, at(0))
    out, done, doing = report_since(stand, journal, conf)
    PROBE.check(f"нет нового — в СДЕЛАНО строка «{NOTHING_NEW}»",
                any(NOTHING_NEW in l for l in done), done)
    leaked = [f for f in DONE_ONCE if f in "\n".join(done)]
    PROBE.check("в СДЕЛАНО нет законченного до метки", not leaked, leaked)
    PROBE.check(f"«{NOTHING_NEW}» не в ДЕЛАЕТСЯ, идущая задача там",
                not any(NOTHING_NEW in l for l in doing) and bool(task_of(doing, FORMAT)), doing)


def case_window_mode(stand: Path) -> None:
    # Страж старого поведения: при ключе 0 метка не действует.
    conf = mode_conf(stand, "0")
    rows = [launch("c-R", "Часть: hr.demo-fresh#1 Свежая", "harness-worker", "sonnet", ts=at(60)),
            finish("c-R", ts=at(50)),
            launch("c-O", "Часть: hr.demo-old#1 Давняя", "harness-worker", "sonnet",
                   ts=at(13 * 60)),
            finish("c-O", ts=at(12.5 * 60))]
    journal = write_journal(stand, rows)
    set_mark(stand, at(0))
    out, done, _ = report_since(stand, journal, conf)
    PROBE.check("ключ 0: законченное за 12 ч показано, несмотря на метку",
                bool(task_of(done, STAND_CARDS["hr.demo-fresh"])), out)
    PROBE.check("ключ 0: давнее окна 12 ч не показано", STAND_CARDS["hr.demo-old"] not in out, out)
    PROBE.check(f"ключ 0: «{NOTHING_NEW}» нет", NOTHING_NEW not in out, out)


def case_mark_command(stand: Path) -> None:
    conf = mode_conf(stand, "1")
    journal = write_journal(stand, lead_rows())
    run_report(stand, journal, conf, since_args(stand, journal))
    PROBE.check("сборка без --отметить метку не пишет", not mark_path(stand).exists(),
                read_mark(stand))
    moment = at(5)
    done = subprocess.run([sys.executable, str(REPORT), "--отметить", moment],
                          capture_output=True, text=True, timeout=60,
                          env=stand_env(stand, conf), cwd=str(stand))
    PROBE.check("--отметить ISO: код 0, в $LOG_DIR/status-report.last ровно это время",
                done.returncode == 0 and read_mark(stand) == moment,
                f"код {done.returncode}, метка «{read_mark(stand)}»: {done.stderr.strip()[-300:]}")


RACE_ROUNDS = 60
RACE_WIDTH = 4     # вызовов в раунде: пара ловит гонку слишком редко
RACE_LEAD = 0.5     # секунд до общего старта: запуск интерпретатора успевает с запасом
# Пускач гонки: импорты сборщика заранее, затем ждёт общего мгновения и исполняет
# НАСТОЯЩИЙ сборщик как __main__. Без выравнивания старта разброс запуска интерпретаторов
# в сотни раз шире окна гонки, и общий временный файл почти не ловится.
RACE_RUNNER = """import runpy, sys, time
from pathlib import Path
report, start, moment = sys.argv[1], float(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(Path(report).parent / "lib"))
import argparse, json, re, subprocess, tempfile, yaml, config
while time.time() < start:
    pass
sys.argv = [report, "--отметить", moment]
runpy.run_path(report, run_name="__main__")
"""


def case_mark_race(stand: Path) -> None:
    # Несколько доставок статуса разом: обе метки обязаны записаться без падения.
    conf = mode_conf(stand, "1")
    env = stand_env(stand, conf)
    failed, bad = [], []
    for i in range(RACE_ROUNDS):
        moments = tuple(at(i * RACE_WIDTH + k) for k in range(RACE_WIDTH))
        start = str(time.time() + RACE_LEAD)
        procs = [subprocess.Popen([sys.executable, "-c", RACE_RUNNER, str(REPORT), start, m],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  text=True, env=env, cwd=str(stand)) for m in moments]
        for proc in procs:
            _, err = proc.communicate(timeout=60)
            if proc.returncode != 0:
                failed.append(f"раунд {i}: код {proc.returncode}: {err.strip()[-200:]}")
        if read_mark(stand) not in moments:
            bad.append(f"раунд {i}: «{read_mark(stand)}»")
    PROBE.check(f"{RACE_ROUNDS} раундов по {RACE_WIDTH} параллельных --отметить: все коды 0",
                not failed, f"упало {len(failed)}: {failed[:2]}")
    PROBE.check("после каждого раунда метка — целиком одно из его ISO", not bad, bad[:3])


# Подставной сборщик стенда: без аргументов — сборка с аргументами стенда, с
# аргументами (вызов tg_send после доставки: --отметить …) — настоящий сборщик как есть.
MARK_STUB = """#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:] or json.loads(os.environ["STAND_REPORT_ARGS"])
with open(os.environ["STAND_BUILDER_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
sys.exit(subprocess.run([sys.executable, os.environ["STAND_REAL_REPORT"], *args]).returncode)
"""

# Curl стенда с отказом API по переменной пробы: ok:false — доставки нет.
CURL_STUB = """#!/usr/bin/env bash
printf 'vyzvan\\n' >> "$STAND_CURL_LOG"
if [ "${STAND_CURL_FAIL:-0}" = 1 ]; then
    printf '{"ok":false,"error_code":429,"description":"Too Many Requests"}'
else
    printf '{"ok":true,"result":{"message_id":1}}'
fi
"""


def mark_stand(stand: Path, journal: Path) -> tuple:
    """(дерево, переменные пробы) стенда tg_send с подставными сборщиком и curl."""
    tree = tg_stand(stand)
    (stand / "bin" / "curl").write_text(CURL_STUB, encoding="utf-8")
    stub = tree / "scripts" / "status-report.py"
    stub.write_text(MARK_STUB, encoding="utf-8")
    stub.chmod(0o755)
    return tree, {"STAND_BUILDER_LOG": str(stand / "log" / "builder.log"),
                  "STAND_REAL_REPORT": str(REPORT),
                  "STAND_REPORT_ARGS": json.dumps(since_args(stand, journal), ensure_ascii=False)}


def case_two_statuses(stand: Path) -> None:
    conf = mode_conf(stand, "1")
    rows = lead_rows()
    journal = write_journal(stand, rows)
    tree, extra = mark_stand(stand, journal)
    before = datetime.now(timezone.utc) - timedelta(seconds=2)
    rc, calls, first, out = send(stand, tree, "--статус", conf=conf, extra=extra)
    after = datetime.now(timezone.utc) + timedelta(seconds=2)
    PROBE.check("условие: первый статус доставлен и несёт сделанное",
                rc == 0 and calls == 1 and all(f in first for f in DONE_ONCE),
                f"код {rc}, curl {calls}: {(first or out)[-400:]}")
    moment = parse_mark(read_mark(stand))
    PROBE.check("успешная доставка записала метку временем отправки",
                moment is not None and before <= moment <= after, read_mark(stand) or "метки нет")
    # W3 запущен ДО метки, кончил ПОСЛЕ: «сделано» судит конец, а не запуск.
    cut = moment or datetime.now(timezone.utc)
    end = (cut + timedelta(seconds=1)).astimezone().isoformat(timespec="seconds")
    rows += [launch("c-W3", "Часть: hr.demo-format#3 Ревью сборщика отчёта", "harness-worker",
                    "opus", "c-L1", review=True, ts=at(30)), finish("c-W3", ts=end)]
    write_journal(stand, rows)
    time.sleep(max(0.0, (cut + timedelta(seconds=1.5) - datetime.now(timezone.utc)).total_seconds()))
    rc, calls, second, out = send(stand, tree, "--статус", conf=conf, extra=extra)
    PROBE.check("условие: второй статус доставлен", rc == 0 and calls == 1,
                f"код {rc}, curl {calls}: {out[-300:]}")
    repeated = [f for f in DONE_ONCE if f in second]
    PROBE.check("во втором статусе нет сделанного из первого", not repeated, repeated or second)
    got = blocks(second)
    task = task_of(got.get(HEADERS[1], []), FORMAT)
    PROBE.check("во втором — новое: законченная проверка W3 в «делали» идущей задачи, не в «делают»",
                (LABELS["opus"], "проверка") in who_pairs(field(task, "делали"))
                and (LABELS["opus"], "проверка") not in who_pairs(field(task, "делают")), second)
    PROBE.check("идущая задача в ДЕЛАЕТСЯ второго статуса, не в СДЕЛАНО",
                bool(task) and not task_of(got.get(HEADERS[0], []), FORMAT), second)


def case_refused_keeps_mark(stand: Path) -> None:
    conf = mode_conf(stand, "1")
    journal = write_journal(stand, lead_rows())
    tree, extra = mark_stand(stand, journal)
    rc, calls, _, out = send(stand, tree, "--статус", conf=conf, extra=extra)
    kept = read_mark(stand)
    PROBE.check("условие: доставленный статус записал метку", rc == 0 and calls == 1 and bool(kept),
                f"код {rc}, curl {calls}, метка «{kept}»: {out[-300:]}")
    time.sleep(1.2)     # иначе сдвиг метки внутри одной секунды не виден
    rc, calls, _, out = send(stand, tree, "--статус", conf=conf,
                             extra={**extra, "STAND_CURL_FAIL": "1"})
    PROBE.check("условие: API ответил ok:false — tg_send отказал", rc != 0 and calls == 1,
                f"код {rc}, curl {calls}: {out[-300:]}")
    PROBE.check("отказ доставки метку не сдвинул", read_mark(stand) == kept,
                f"было «{kept}», стало «{read_mark(stand)}»")
    rc, calls, _, out = send(stand, tree, "Ворота зелёные, беру следующую задачу.",
                             conf=conf, extra=extra)
    PROBE.check("обычное сообщение метку не двигает", rc == 0 and read_mark(stand) == kept,
                f"код {rc}, было «{kept}», стало «{read_mark(stand)}»")


# Очередь — дословный формат `work-queue.py <карта> --порядок` (напечатать()):
# две старые строки, «сейчас делаем: <id> — <почему>», затем «  N. <id> · <вес> · <почему>».
# Голова очереди тут НЕ начата (plan): «сейчас делаем» судится строкой, не статусом.
NEXT_QUEUE = (
    "работа: 7\n"
    "ждут: hr.demo-wait=его слово 03.11: «потом»\n"
    "сейчас делаем: hr.demo-head — важность 1\n"
    "  1. hr.demo-head · 48 · важность 1\n"
    "  2. hr.demo-wip · 30 · начата, важность 5\n"
    "  3. hr.demo-a · 20 · важность 2\n"
    "  4. hr.demo-ghost · 20 · важность 2\n"
    "  5. hr.demo-b · 10 · важность 3\n"
    "  6. hr.demo-c · 10 · важность 3\n"
    "  7. hr.demo-d · 5 · важность 4\n")
NEXT_TITLES = {"hr.demo-head": "Взятая сейчас карточка", "hr.demo-wip": WIP_TITLE,
               "hr.demo-a": "Почистить старые журналы", "hr.demo-b": "Сверить заявки на файлы",
               "hr.demo-c": "Убрать лишний порт", "hr.demo-d": "Переписать справку"}


def next_stand(stand: Path, queue: str) -> list:
    statuses = {"hr.demo-wip": "wip"}
    tasks = "".join(f"  - id: {cid}\n    status: {statuses.get(cid, 'plan')}\n    title: {title}\n"
                    for cid, title in NEXT_TITLES.items())
    (stand / "dev-map-next.yaml").write_text(
        "queue: [hr.demo-head, hr.demo-a]\nlines:\n- id: harness\n  tasks:\n" + tasks,
        encoding="utf-8")
    (stand / "queue-next.txt").write_text(queue, encoding="utf-8")
    args = report_args(stand, write_journal(stand, main_rows()))
    args[args.index("--карта") + 1] = str(stand / "dev-map-next.yaml")
    args[args.index("--очередь") + 1] = str(stand / "queue-next.txt")
    return args


def case_next_list(stand: Path) -> None:
    out = run_report(stand, Path(), args=next_stand(stand, NEXT_QUEUE))
    nxt = blocks(out).get(HEADERS[2], [])
    text = "\n".join(nxt)
    PROBE.check(f"ДАЛЬШЕ — 2–3 строки (сейчас {len(nxt)})", 2 <= len(nxt) <= 3, nxt)
    order = [NEXT_TITLES[c] for c in ("hr.demo-a", "hr.demo-b", "hr.demo-c")][:max(len(nxt), 2)]
    PROBE.check("ДАЛЬШЕ — первые по очереди не начатые, по порядку",
                len(nxt) >= 2 and all(t in l for t, l in zip(order, nxt)), nxt)
    PROBE.check("ДАЛЬШЕ без карточки «сейчас делаем» и начатой",
                NEXT_TITLES["hr.demo-head"] not in text and WIP_TITLE not in text, nxt)
    PROBE.check("ДАЛЬШЕ без id и слов «сейчас делаем»",
                not CARD_ID.search(text) and "сейчас делаем" not in text, nxt)
    PROBE.check("ДАЛЬШЕ: строки ≤ 80 знаков", nxt and all(len(l) <= 80 for l in nxt), nxt)


def case_next_empty(stand: Path) -> None:
    for name, queue in (
            ("очередь пуста целиком",
             "работа: 0\nждут: \nсейчас делаем: очередь пуста\n"),
            ("в очереди только «сейчас делаем»",
             "работа: 1\nждут: \nсейчас делаем: hr.demo-head — важность 1\n"
             "  1. hr.demo-head · 48 · важность 1\n")):
        out = run_report(stand, Path(), args=next_stand(stand, queue))
        nxt = blocks(out).get(HEADERS[2], [])
        PROBE.check(f"{name}: ДАЛЬШЕ — ровно «{QUEUE_EMPTY}»", nxt == [QUEUE_EMPTY], nxt)


# ── простой и коммиты; все сессии ───────────────────────────────────────────
# Потеря 04.11: смена сессии оркестратора — и работа прошлой пропала из статуса;
# тихий час без помощников выглядел как «пока нового нет» без часов.
NOTHING_AT_ALL = "пока нового нет"
COMMITS_HEAD = "📝 коммитов"
PANEL_SUBJECT = "Правка памяти с панели: ORDERS.md"
CHANNEL_CEILING = 700
# Стенд коммитов не читает чужие настройки git: ни подписи, ни глобальных хуков.
GIT_STAND = ("-c", "user.name=proba", "-c", "user.email=proba@stand.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null")


def git_stand(root: Path, commits: list) -> Path:
    """Свой репозиторий стенда: пустые коммиты (тема, ISO) по порядку времени."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env, capture_output=True)
    for subject, moment in commits:
        subprocess.run(["git", *GIT_STAND, "-C", str(root), "commit", "-q", "--allow-empty",
                        "-m", subject], check=True, capture_output=True,
                       env={**env, "GIT_AUTHOR_DATE": moment, "GIT_COMMITTER_DATE": moment})
    return root


def clock(moment: str) -> str:
    return datetime.fromisoformat(moment).astimezone().strftime("%H:%M")


def commits_line(stand: Path) -> str:
    return line_with(run_report(stand, Path(), mode_conf(stand, "1"),
                                since_args(stand, stand / "log" / "вызовы-агентов.jsonl")).splitlines(),
                     COMMITS_HEAD)


def case_commits_shown(stand: Path) -> None:
    write_journal(stand, lead_rows())
    set_mark(stand, at(100))
    git_stand(stand / "project", [("Старая работа до метки", at(120)),
                                  ("Сборщик: строка коммитов", at(50)),
                                  ("Пробы: стенд коммитов", at(20))])
    out, done, _ = report_since(stand, stand / "log" / "вызовы-агентов.jsonl", mode_conf(stand, "1"))
    expect = f"{COMMITS_HEAD} 2, последний: Пробы: стенд коммитов"
    PROBE.check(f"коммиты после метки — строка «{expect}»", expect in done, done or out)
    PROBE.check("строка 📝 — последняя в СДЕЛАНО, после задач",
                bool(done) and done[-1] == expect and bool(task_of(done, CLEANUP)), done)
    PROBE.check("коммит до метки не считан", "Старая работа" not in out, out)


def case_commits_panel(stand: Path) -> None:
    write_journal(stand, lead_rows())
    set_mark(stand, at(100))
    git_stand(stand / "project", [("Сборщик: строка коммитов", at(50)),
                                  ("Пробы: стенд коммитов", at(20)), (PANEL_SUBJECT, at(10))])
    line = commits_line(stand)
    PROBE.check("панельный коммит не считан и не назван последним",
                line == f"{COMMITS_HEAD} 2, последний: Пробы: стенд коммитов", line)


def case_idle_phrase(stand: Path) -> None:
    conf = mode_conf(stand, "1")
    # Самый поздний конец — у чужой сессии: время «последний» берётся по всему журналу.
    rows = lead_rows() + [launch("c-P", "Работа прошлой сессии", "harness-worker", "sonnet",
                                 ts=at(40), session="proshlaya"),
                          finish("c-P", ts=at(30), session="proshlaya")]
    journal = write_journal(stand, rows)
    mark = at(5)
    set_mark(stand, mark)
    git_stand(stand / "project", [("Старая работа до метки", at(120))])
    out, done, _ = report_since(stand, journal, conf)
    expect = f"с {clock(mark)} нового нет; последний помощник кончил {clock(at(30))}"
    PROBE.check(f"ни помощников, ни коммитов — СДЕЛАНО ровно «{expect}»", done == [expect],
                done or out)
    PROBE.check("строки 📝 нет без коммитов", COMMITS_HEAD not in out, out)
    journal = write_journal(stand, [r for r in rows if r["event"] != "agent.finish"])
    _, done, _ = report_since(stand, journal, conf)
    PROBE.check(f"концов в журнале нет совсем — СДЕЛАНО ровно «{NOTHING_AT_ALL}»",
                done == [NOTHING_AT_ALL], done)


def case_commits_no_repo(stand: Path) -> None:
    conf = mode_conf(stand, "1")
    journal = write_journal(stand, lead_rows())
    set_mark(stand, at(30))
    args = since_args(stand, journal)
    out = run_report(stand, journal, conf, args)
    PROBE.check("PROJECT_DIR не репозиторий: код 0, строки 📝 нет", COMMITS_HEAD not in out, out)
    repo = git_stand(stand / "repo", [("Свежая работа стенда", at(10))])
    out = run_report(stand, journal, conf, args, env={"PROJECT_DIR": str(repo)})
    PROBE.check("условие: тот же стенд с репозиторием показывает 📝", COMMITS_HEAD in out, out)
    (stand / "nogit").mkdir()
    out = run_report(stand, journal, conf, args,
                     env={"PROJECT_DIR": str(repo), "PATH": str(stand / "nogit")})
    PROBE.check("git нет в PATH: код 0, строки 📝 нет", COMMITS_HEAD not in out, out)
    (stand / "install-empty.conf").write_text("", encoding="utf-8")
    # Пустой корень проекта: `git -C ""` взял бы каталог запуска — тут он репозиторий.
    out = run_report(stand, journal, conf, args, cwd=repo,
                     env={"PROJECT_DIR": "", "HARNESS_INSTALL_CONF": str(stand / "install-empty.conf")})
    PROBE.check("пустой PROJECT_DIR: коммиты каталога запуска не взяты", COMMITS_HEAD not in out, out)


def sessions_journal(stand: Path) -> Path:
    return write_journal(stand, [
        launch("c-A", "Часть: hr.demo-this#1 Эта", "harness-worker", "sonnet"), finish("c-A"),
        launch("c-P", "Часть: hr.demo-past#1 Прошлая", "harness-worker", "sonnet",
               session="proshlaya"),
        finish("c-P", session="proshlaya")])


def case_all_sessions(stand: Path) -> None:
    journal = sessions_journal(stand)
    args = report_args(stand, journal)
    cut = args.index("--сессия")
    out = run_report(stand, journal, args=args[:cut] + args[cut + 2:],
                     env={"CLAUDE_CODE_SESSION_ID": SESSION})
    done = "\n".join(blocks(out).get(HEADERS[0], []))
    PROBE.check("без --сессия: работа ДРУГОЙ сессии в СДЕЛАНО (сессия среды не фильтр)",
                "Работа прошлой сессии" in done and "Работа этой сессии" in done, out)


def case_session_filter(stand: Path) -> None:
    out = run_report(stand, sessions_journal(stand))
    PROBE.check("--сессия X: работа другой сессии не показана, своей — показана",
                "Работа прошлой сессии" not in out
                and "Работа этой сессии" in "\n".join(blocks(out).get(HEADERS[0], [])), out)


def case_commits_only(stand: Path) -> None:
    write_journal(stand, lead_rows())
    set_mark(stand, at(45))
    git_stand(stand / "project", [("Пробы: стенд коммитов", at(20))])
    out, done, _ = report_since(stand, stand / "log" / "вызовы-агентов.jsonl", mode_conf(stand, "1"))
    PROBE.check("задач нет, коммит есть: СДЕЛАНО — только строка 📝, без фразы простоя",
                done == [f"{COMMITS_HEAD} 1, последний: Пробы: стенд коммитов"], done or out)


def case_commit_truncated(stand: Path) -> None:
    conf = write_conf(stand, "harness-700.conf", CHANNEL_CEILING).name
    with (stand / conf).open("a", encoding="utf-8") as out:
        out.write('STATUS_DONE_SINCE_LAST="1"\n')
    journal = write_journal(stand, lead_rows())
    set_mark(stand, at(30))
    subject = "Сборщик статуса: " + "длинная тема коммита про очередь и журнал " * 25
    git_stand(stand / "project", [(subject.strip(), at(10))])
    text = run_report(stand, journal, conf, since_args(stand, journal))
    line = line_with(text.splitlines(), COMMITS_HEAD)
    PROBE.check(f"длинная тема: отчёт {len(text)} ≤ {CHANNEL_CEILING}",
                0 < len(text) <= CHANNEL_CEILING, len(text))
    PROBE.check("длинная тема: строка 📝 обрезана до ≤ 80 с «…»",
                line.startswith(f"{COMMITS_HEAD} 1, последний: Сборщик статуса")
                and line.endswith("…") and len(line) <= 80, line or text)
    (stand / "install-empty.conf").write_text("", encoding="utf-8")
    done = subprocess.run(
        [sys.executable, str(HERE / "check-channel-messages.py"), "--статус", "--текст", text],
        capture_output=True, text=True, timeout=60,
        env={**stand_env(stand, conf), "HARNESS_INSTALL_CONF": str(stand / "install-empty.conf")})
    PROBE.check("check-channel-messages.py --статус пропускает (код 0)", done.returncode == 0,
                f"код {done.returncode}: {(done.stdout + done.stderr)[-300:]}")


# ── потерянные запуски ──────────────────────────────────────────────────────
LOST_KEY = "STATUS_LOST_HOURS"
LOST_MORE = "и ещё 2"


def lost_conf(stand: Path, hours) -> str:
    """Конф стенда с порогом потери; None — ключа нет."""
    name = f"harness-lost-{len(list(stand.glob('harness-lost-*')))}.conf"
    base = (stand / "harness.conf").read_text(encoding="utf-8")
    key = f'{LOST_KEY}="{hours}"\n' if hours is not None else ""
    (stand / name).write_text(base + key, encoding="utf-8")
    return name


def lost_report(stand: Path, rows: list, conf: str = "harness.conf") -> tuple:
    """(текст, блоки) за окно 12 ч по настоящим часам: время потери судит часы, не --с."""
    journal = write_journal(stand, rows)
    out = run_report(stand, journal, conf, since_args(stand, journal))
    return out, blocks(out)


def heads_order(text: str) -> list:
    return [h for line in text.splitlines()
            for h in HEADERS + (LOST_HEADER,) if line.startswith(h)]


def joined(got: dict, head: str) -> str:
    return "\n".join(got.get(head, []))


def case_lost_orphan(stand: Path) -> None:
    started = at(200)
    rows = [launch("c-X", "Сверка заявок на файлы", "harness-worker", "sonnet", ts=started),
            launch("c-F", "Передача смены", "harness-worker", "sonnet", ts=at(30)),
            finish("c-F", ts=at(10))]
    out, got = lost_report(stand, rows)
    doing = got.get(HEADERS[1], [])
    PROBE.check("сирота старше 2 ч не в ДЕЛАЕТСЯ, там «пока ничего»",
                doing == ["пока ничего"], doing or out)
    PROBE.check(f"заголовок блока — строка ровно «{LOST_HEADER}»",
                LOST_HEADER in out.splitlines(), out)
    PROBE.check("блок ⚠ после ДЕЛАЕТСЯ и перед ДАЛЬШЕ",
                heads_order(out) == [HEADERS[0], HEADERS[1], LOST_HEADER, HEADERS[2]],
                heads_order(out))
    line = line_with(got.get(LOST_HEADER, []), "Сверка заявок на файлы")
    PROBE.check(f"строка ⚠: описание и время запуска {clock(started)}",
                bool(line) and clock(started) in line, got.get(LOST_HEADER) or out)
    PROBE.check("сирота показан один раз",
                len([l for l in out.splitlines() if "Сверка заявок" in l]) == 1, out)


def case_lost_young(stand: Path) -> None:
    rows = [launch("c-X", "Часть: hr.demo-young#1 Сверка заявок на файлы", "harness-worker",
                   "sonnet", ts=at(90))]
    out, got = lost_report(stand, rows)
    PROBE.check("запуск моложе порога 2 ч — его задача в ДЕЛАЕТСЯ",
                bool(task_of(got.get(HEADERS[1], []), STAND_CARDS["hr.demo-young"])), out)
    PROBE.check("потерянных нет — ни заголовка, ни значка ⚠", "⚠" not in out, out)


def case_lost_real(stand: Path) -> None:
    # Журнал 04.11 со сдвигом к настоящим часам: оркестратор кончил в 14:51 (тут — 140 мин
    # назад), исполнитель «Раунд правок 1 по ревью» с 13:11 без конца.
    lost_at = at(240)
    rows = [
        launch("c-G", "Часть: hr.demo-guard#1 Оркестратор: Охрана удаления: ключи и чужие проекты",
               "harness-lead", "sonnet", ts=at(463)),
        finish("c-G", ts=at(461)),
        launch("c-R1", "Часть: hr.demo-guard#2 Раунд правок 1 по ревью", "harness-worker",
               "sonnet", "c-G", ts=lost_at),
        finish("c-G", ts=at(239)),
        launch("c-R2", "Часть: hr.demo-guard#3 Раунд правок 1, продолжение", "harness-worker",
               "sonnet", "c-G", ts=at(161)),
        finish("c-G", ts=at(160)), finish("c-R2", ts=at(157)), finish("c-G", ts=at(156)),
        finish("c-G", ts=at(140)),
    ]
    out, got = lost_report(stand, rows)
    doing = got.get(HEADERS[1], [])
    PROBE.check("ДЕЛАЕТСЯ — «пока ничего»: оркестратор кончил, ребёнок потерян",
                doing == ["пока ничего"], doing or out)
    lost = got.get(LOST_HEADER, [])
    line = line_with(lost, STAND_CARDS["hr.demo-guard"])
    PROBE.check(f"потерянный ребёнок в ⚠ задачей словами, с моделью и временем {clock(lost_at)}",
                len(lost) == 1 and clock(lost_at) in line and LABELS["sonnet"] in line, lost or out)
    done = got.get(HEADERS[0], [])
    pairs = who_pairs(field(task_of(done, STAND_CARDS["hr.demo-guard"]), "делали"))
    PROBE.check("задача в СДЕЛАНО: оркестратор и законченное продолжение в «делали»",
                pairs == [(LABELS["sonnet"], "вёл"), (LABELS["sonnet"], "код")], done)
    PROBE.check("оркестратор в ⚠ не попал: там одна строка, не две", len(lost) == 1, lost or out)


def case_lost_sibling(stand: Path) -> None:
    lost_at = at(250)
    rows = [
        launch("c-L", "Часть: hr.demo-chapters#1 Оркестратор: правка справки", "harness-lead",
               "opus", ts=at(300)),
        launch("c-W1", "Часть: hr.demo-chapters#2 Старая глава справки", "harness-worker",
               "sonnet", "c-L", ts=lost_at),
        launch("c-W2", "Часть: hr.demo-chapters#3 Новая глава справки", "harness-worker",
               "sonnet", "c-L", ts=at(30)),
    ]
    out, got = lost_report(stand, rows)
    doing = got.get(HEADERS[1], [])
    PROBE.check("задача: оркестратор без конца и свежий ребёнок — в ДЕЛАЕТСЯ",
                bool(task_of(doing, STAND_CARDS["hr.demo-chapters"])), doing or out)
    PROBE.check("потерянный ребёнок не в ДЕЛАЕТСЯ", "Старая глава" not in "\n".join(doing), doing)
    lost = got.get(LOST_HEADER, [])
    line = line_with(lost, STAND_CARDS["hr.demo-chapters"])
    PROBE.check(f"потерянный ребёнок в ⚠ задачей словами, с временем {clock(lost_at)}",
                bool(line) and clock(lost_at) in line, lost or out)
    PROBE.check("живой оркестратор в ⚠ не попал: там одна строка", len(lost) == 1, lost or out)


def case_lost_descendant_finish(stand: Path) -> None:
    # Свежий конец внука держит живыми и ребёнка, и оркестратора: глубина любая.
    rows = [
        launch("c-L", "Часть: hr.demo-deep#1 Оркестратор: долгая сверка", "harness-lead", "opus",
               ts=at(400)),
        launch("c-C", "Часть: hr.demo-deep#2 Сверка первой половины", "harness-worker", "opus",
               "c-L", ts=at(350)),
        launch("c-G", "Часть: hr.demo-deep#3 Сверка мелочи", "harness-worker", "haiku", "c-C",
               ts=at(300)),
        finish("c-G", ts=at(20)),
    ]
    out, got = lost_report(stand, rows)
    PROBE.check("свежий конец потомка — блока ⚠ нет", LOST_HEADER not in out, out)
    pairs = who_pairs(field(task_of(got.get(HEADERS[1], []), STAND_CARDS["hr.demo-deep"]),
                            "делают"))
    PROBE.check("оркестратор и ребёнок без конца — задача в ДЕЛАЕТСЯ, оба в «делают»",
                all(p in pairs for p in ((LABELS["opus"], "вёл"), (LABELS["opus"], "код"))), out)


def case_lost_own_record(stand: Path) -> None:
    rows = [launch("c-K", "Часть: hr.demo-unload#1 Выгрузка", "harness-worker", "sonnet",
                   ts=at(300)),
            {"v": 1, "ts": at(15), "src": "scripts/hooks/agent_budget_guard.py",
             "event": "agent.tool", "outcome": "ok", "task": "hr.demo-wip", "session": SESSION,
             "data": {"вызов": "c-K"}}]
    out, got = lost_report(stand, rows)
    PROBE.check("свежая запись с data.вызов запуска — жив, в ДЕЛАЕТСЯ, ⚠ нет",
                bool(task_of(got.get(HEADERS[1], []), STAND_CARDS["hr.demo-unload"]))
                and LOST_HEADER not in out, out)


def case_lost_more(stand: Path) -> None:
    rows = [launch(f"c-X{i}", f"Потерянная работа {i}", "harness-worker", "sonnet",
                   ts=at(200 + i * 10)) for i in range(5)]
    out, got = lost_report(stand, rows)
    lost = got.get(LOST_HEADER, [])
    named = [l for l in lost if "Потерянная работа" in l]
    PROBE.check("5 потерянных: ровно 3 строки с описанием", len(named) == 3, lost or out)
    PROBE.check(f"остаток — строка «{LOST_MORE}», блок из 4 строк",
                len(lost) == 4 and any(LOST_MORE in l for l in lost), lost or out)
    PROBE.check("ни один не в ДЕЛАЕТСЯ", "Потерянная" not in joined(got, HEADERS[1]), out)


def lost_split(stand: Path, conf: str) -> tuple:
    """(в ⚠, в ДЕЛАЕТСЯ) для запусков без конца 3 ч и 1,5 ч назад."""
    rows = [launch("c-A", "Часть: hr.demo-three#1 Трёхчасовая работа", "harness-worker", "sonnet",
                   ts=at(180)),
            launch("c-B", "Часть: hr.demo-half#1 Полуторачасовая работа", "harness-worker",
                   "sonnet", ts=at(90))]
    _, got = lost_report(stand, rows, conf)
    names = ("Трёхчасовая", "Полуторачасовая")
    return ([n for n in names if n in joined(got, LOST_HEADER)],
            [n for n in names if n in joined(got, HEADERS[1])])


def case_lost_hours(stand: Path) -> None:
    lost, doing = lost_split(stand, lost_conf(stand, "1"))
    PROBE.check(f"{LOST_KEY}=1: оба (3 ч и 1,5 ч) потеряны, в ДЕЛАЕТСЯ нет",
                len(lost) == 2 and not doing, f"⚠ {lost}, ДЕЛАЕТСЯ {doing}")
    lost, doing = lost_split(stand, lost_conf(stand, "6"))
    PROBE.check(f"{LOST_KEY}=6: тот же журнал — оба в ДЕЛАЕТСЯ, ⚠ нет",
                not lost and len(doing) == 2, f"⚠ {lost}, ДЕЛАЕТСЯ {doing}")


def case_lost_garbage(stand: Path) -> None:
    for value in (None, "abc", ""):
        lost, doing = lost_split(stand, lost_conf(stand, value))
        name = "ключа нет" if value is None else f"{LOST_KEY}=«{value}»"
        PROBE.check(f"{name}: порог 2 ч — 3 ч потерян, 1,5 ч идёт",
                    lost == ["Трёхчасовая"] and doing == ["Полуторачасовая"],
                    f"⚠ {lost}, ДЕЛАЕТСЯ {doing}")


def case_lost_none_same(stand: Path) -> None:
    # Давние законченные и давний оркестратор со свежим ребёнком — не потеряны:
    # отчёт ровно прежний, строка в строку.
    # Форма по задачам целиком: у «Свежей работы» карточки нет — заголовок из описания.
    rows = [
        launch("c-L", "Часть: hr.demo-cleanup#1 Оркестратор: старая уборка", "harness-lead", "opus",
               ts=at(400)),
        launch("c-W", "Часть: hr.demo-cleanup#2 Удаление по списку", "harness-worker", "sonnet",
               "c-L", ts=at(390)),
        finish("c-W", ts=at(385)), finish("c-L", ts=at(380)),
        launch("c-M", "Часть: hr.demo-chapters#1 Оркестратор: долгая правка", "harness-lead",
               "opus", ts=at(300)),
        launch("c-V", "Часть: hr.demo-chapters#2 Глава справки", "harness-worker", "sonnet", "c-M",
               ts=at(60)),
        launch("c-R", "Часть: hr.demo-nocard#1 Свежая работа", "harness-worker", "opus", ts=at(20)),
    ]
    out, _ = lost_report(stand, rows)
    opus, sonnet = LABELS["opus"], LABELS["sonnet"]
    expect = "\n".join([
        HEADERS[0],
        f"1. {CLEANUP}",
        f"   делали: {opus} (вёл), {sonnet} (код)",
        "   этапы: код ✔",
        HEADERS[1],
        f"2. {STAND_CARDS['hr.demo-chapters']}",
        f"   делают: {opus} (вёл), {sonnet} (код)",
        "   этап: код",
        "3. Свежая работа",
        f"   делают: {opus} (код)",
        "   этап: код",
        HEADERS[2], f"4. {NEXT_TITLE}"])
    PROBE.check("потерянных нет — отчёт по задачам байт в байт", out == expect, out)


def case_lost_ceiling(stand: Path) -> None:
    journal = long_journal(stand)
    tail = " ".join(["выгрузка журнала вызовов агентов по сессии"] * 3)
    with journal.open("a", encoding="utf-8") as out_file:
        for i in range(5):
            # Старше порога 2 ч, но позже --с стенда (13:35 − 10:00 = 215 мин).
            row = launch(f"c-X{i}", f"Потерянная работа {i} {tail}", "harness-worker", "opus",
                         ts=at(130 + i * 10))
            out_file.write(json.dumps(row, ensure_ascii=False) + "\n")
    for conf, ceiling in (("harness.conf", CEILING), ("harness-small.conf", SMALL_CEILING)):
        out = run_report(stand, journal, conf)
        PROBE.check(f"потолок {ceiling}: условие — блок ⚠ есть", LOST_HEADER in out, out[-400:])
        PROBE.check(f"потолок {ceiling}: длина {len(out)} ≤ потолка", 0 < len(out) <= ceiling,
                    len(out))
        wide = [l for l in out.splitlines() if len(l) > 80]
        PROBE.check(f"потолок {ceiling}: каждая строка ≤ 80 знаков", not wide, wide[:2])
        PROBE.check(f"потолок {ceiling}: четыре заголовка по порядку, ДАЛЬШЕ не съеден",
                    heads_order(out) == [HEADERS[0], HEADERS[1], LOST_HEADER, HEADERS[2]]
                    and NEXT_TITLE[:20] in joined(blocks(out), HEADERS[2]), out[-400:])


def case_lost_mark_mode(stand: Path) -> None:
    # Потерянный в режиме метки — незаконченный без жизни дольше порога, окно ему не судья;
    # в ⚠ он попадает, если запущен в окне ИЛИ жил после метки.
    conf = mode_conf(stand, "1")
    set_mark(stand, at(600))
    started = at(900)
    journal = write_journal(stand, [
        launch("c-L", "Часть: hr.demo-night#1 Оркестратор: ночная сверка", "harness-lead", "opus",
               ts=started),
        launch("c-W", "Часть: hr.demo-night#2 Сверка таблицы заявок", "harness-worker", "sonnet",
               "c-L", ts=at(890)),
        finish("c-W", ts=at(300))])
    out = run_report(stand, journal, conf, since_args(stand, journal))
    got = blocks(out)
    PROBE.check("корень без жизни 5 ч не в ДЕЛАЕТСЯ, там «пока ничего»",
                got.get(HEADERS[1]) == ["пока ничего"], got.get(HEADERS[1]) or out)
    line = line_with(got.get(LOST_HEADER, []), STAND_CARDS["hr.demo-night"])
    PROBE.check(f"корень вне окна 12 ч, но живший после метки — в ⚠ с временем {clock(started)}",
                bool(line) and clock(started) in line, got.get(LOST_HEADER) or out)
    done = got.get(HEADERS[0], [])
    night = STAND_CARDS["hr.demo-night"]
    PROBE.check("ребёнок, кончивший после метки, — его задача в СДЕЛАНО один раз",
                bool(task_of(done, night)) and all_titles(out).count(night) == 1, out)
    PROBE.check("в «делали» — ребёнок, потерянный корень не в счёт",
                who_pairs(field(task_of(done, night), "делали")) == [(LABELS["sonnet"], "код")],
                done)
    journal = write_journal(stand, [
        launch("c-X", "Забытая выгрузка", "harness-worker", "sonnet", ts=started)])
    out = run_report(stand, journal, conf, since_args(stand, journal))
    PROBE.check("сирота вне окна без жизни после метки — нигде, как и в режиме окна",
                "Забытая выгрузка" not in out
                and "Забытая выгрузка" not in run_report(stand, journal, "harness.conf",
                                                         since_args(stand, journal)), out)


def case_lost_hours_extreme(stand: Path) -> None:
    for value in ("inf", "1e20", "-5", "nan", "abc"):
        try:
            lost, doing = lost_split(stand, lost_conf(stand, value))
        except RuntimeError as error:
            lost, doing = str(error)[-200:], None
        PROBE.check(f"{LOST_KEY}=«{value}»: код 0, порог 2 ч — 3 ч потерян, 1,5 ч идёт",
                    lost == ["Трёхчасовая"] and doing == ["Полуторачасовая"],
                    f"⚠ {lost}, ДЕЛАЕТСЯ {doing}")


def case_lost_middle(stand: Path) -> None:
    rows = [
        launch("c-L", "Часть: hr.demo-memory#1 Оркестратор: правка памяти", "harness-lead", "opus",
               ts=at(300)),
        launch("c-C", "Часть: hr.demo-memory#2 Разбор заявок памяти", "harness-worker", "opus",
               "c-L", ts=at(280)),
        launch("c-G", "Часть: hr.demo-memory#3 Чистка дублей памяти", "harness-worker", "haiku",
               "c-C", ts=at(270)),
        finish("c-G", ts=at(240)), finish("c-L", ts=at(60)),
    ]
    out, got = lost_report(stand, rows)
    PROBE.check("условие: ребёнок без жизни 4 ч — в ⚠",
                bool(line_with(got.get(LOST_HEADER, []), STAND_CARDS["hr.demo-memory"])),
                got.get(LOST_HEADER) or out)
    done = got.get(HEADERS[0], [])
    memory = STAND_CARDS["hr.demo-memory"]
    PROBE.check("корень и законченный внук — одна задача в СДЕЛАНО, не две",
                who_pairs(field(task_of(done, memory), "делали"))
                == [(LABELS["opus"], "вёл"), (LABELS["haiku"], "код")]
                and all_titles(out).count(memory) == 1, done or out)


def case_lost_root_done_child(stand: Path) -> None:
    # Строки потерянного корня в дереве нет, поэтому законченный ребёнок висит прямо
    # под строкой главного, а не повисает отдельным деревом без неё.
    rows = [
        launch("c-L", "Часть: hr.demo-oldclean#1 Оркестратор: давняя уборка", "harness-lead",
               "opus", ts=at(400)),
        launch("c-W", "Часть: hr.demo-oldclean#2 Удаление старых логов", "harness-worker", "sonnet",
               "c-L", ts=at(390)),
        finish("c-W", ts=at(380)),
    ]
    out, got = lost_report(stand, rows)
    PROBE.check("потерянный корень — в ⚠ задачей словами, одной строкой",
                len(got.get(LOST_HEADER, [])) == 1
                and STAND_CARDS["hr.demo-oldclean"] in joined(got, LOST_HEADER), out)
    PROBE.check("ДЕЛАЕТСЯ — «пока ничего»", got.get(HEADERS[1]) == ["пока ничего"],
                got.get(HEADERS[1]) or out)
    done = got.get(HEADERS[0], [])
    oldclean = STAND_CARDS["hr.demo-oldclean"]
    PROBE.check("законченный ребёнок держит задачу в СДЕЛАНО, «делали» — только он",
                who_pairs(field(task_of(done, oldclean), "делали")) == [(LABELS["sonnet"], "код")]
                and all_titles(out).count(oldclean) == 1, done or out)


# ── по задачам ──────────────────────────────────────────────────────────────
# Слово владельца 06.11: «мне честно вообще нихрена не понятно <…> что за задачи <…> а под
# ними кто делал и какие стадии». Образец непонятного — дерево помощников с ярлыками
# вроде «Code N2, registry, mutations, commit».
STATUS_TITLE = STAND_CARDS["hr.demo-status"]
TONE_HARD = 1400


def case_tasks_sick(stand: Path) -> None:
    rows = [
        launch("c-L", "Часть: hr.demo-status#1 Оркестратор: Code N2, registry, mutations, commit",
               "harness-lead", "opus", ts=day("11:00")),
        launch("c-W", "Часть: hr.demo-status#1 Code N2 registry", "harness-worker", "sonnet", "c-L",
               ts=day("11:10")),
        finish("c-W", ts=day("12:00")), finish("c-L", ts=day("12:10")),
    ]
    out = run_report(stand, write_journal(stand, rows))
    done = blocks(out).get(HEADERS[0], [])
    tasks = parse_tasks(done)
    PROBE.check("два помощника с меткой «Часть: X#1» — ОДНА задача в СДЕЛАНО",
                len(tasks) == 1, done or out)
    PROBE.check(f"заголовок задачи — title карточки «{STATUS_TITLE}»",
                [t["title"] for t in tasks] == [STATUS_TITLE], done or out)
    PROBE.check("английских ярлыков запусков в отчёте нет",
                not latin_words(out), latin_words(out) or out)
    PROBE.check("прежнего дерева помощников нет (🧭 🎼 ├ └)",
                not any(s in out for s in ("🧭", "🎼", "├", "└")), out)
    PROBE.check("СДЕЛАНО ровно: задача, «делали» с ролями, «этапы: код ✔»",
                done == [f"1. {STATUS_TITLE}",
                         f"   делали: {LABELS['opus']} (вёл), {LABELS['sonnet']} (код)",
                         "   этапы: код ✔"], done or out)


def case_tasks_other(stand: Path) -> None:
    rows = [
        launch("c-A", "Shift handoff notes", "harness-worker", "sonnet", ts=day("12:30")),
        launch("c-B", "Code N2 registry", "harness-worker", "opus", ts=day("12:40")),
        finish("c-A"), finish("c-B"),
        launch("c-C", "Parse mutations", "harness-worker", "haiku", ts=day("13:00")),
        launch("c-L", "Часть: hr.demo-status#1 Оркестратор: статус", "harness-lead", "opus",
               ts=day("11:20")),
        finish("c-L"),
    ]
    out = run_report(stand, write_journal(stand, rows))
    got = blocks(out)
    PROBE.check("два законченных без метки — «прочее 2» в СДЕЛАНО",
                other_count(got.get(HEADERS[0], [])) == 2, got.get(HEADERS[0]) or out)
    PROBE.check("идущий без метки — «прочее 1» в ДЕЛАЕТСЯ",
                other_count(got.get(HEADERS[1], [])) == 1, got.get(HEADERS[1]) or out)
    PROBE.check("помощники без метки не стали задачами: задача одна, со своей меткой",
                [t["title"] for h in HEADERS[:2] for t in parse_tasks(got.get(h, []))]
                == [STATUS_TITLE], out)
    PROBE.check("их описаний и английских слов в отчёте нет",
                not latin_words(out) and "handoff" not in out, out)


def commit_file(root: Path, name: str, message: str, moment: str) -> None:
    """Коммит стенда с настоящим файлом: «test_» видно и в теме, и в списке файлов."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_DATE": moment, "GIT_COMMITTER_DATE": moment}
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# проба стенда\n", encoding="utf-8")
    subprocess.run(["git", *GIT_STAND, "-C", str(root), "add", name], check=True,
                   capture_output=True, env=env)
    subprocess.run(["git", *GIT_STAND, "-C", str(root), "commit", "-q", "-m", message], check=True,
                   capture_output=True, env=env)


def task_dir(stand: Path, cid: str, files: dict) -> None:
    folder = stand / "project" / ".tasks" / cid
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")


def stage_rows() -> list:
    return [
        launch("c-SL", "Часть: hr.demo-stages#1 Оркестратор: порты", "harness-lead", "opus",
               ts=at(200)),
        launch("c-ST", "Часть: hr.demo-stages#2 test_ports: проба", "harness-worker", "opus",
               "c-SL", ts=at(190)),
        launch("c-SC", "Часть: hr.demo-stages#3 Code ports", "harness-worker", "sonnet", "c-SL",
               ts=at(180)),
        launch("c-SR", "Часть: hr.demo-stages#4 Ревью кода", "harness-worker", "opus", "c-SL",
               review=True, ts=at(170)),
        finish("c-ST", ts=at(185)), finish("c-SC", ts=at(175)), finish("c-SR", ts=at(165)),
        finish("c-SL", ts=at(100)),
        launch("c-BL", "Часть: hr.demo-bare#1 Оркестратор: голая", "harness-lead", "sonnet",
               ts=at(160)),
        launch("c-BC", "Часть: hr.demo-bare#2 Код", "harness-worker", "sonnet", "c-BL", ts=at(150)),
        finish("c-BC", ts=at(145)), finish("c-BL", ts=at(140)),
        launch("c-CT", "Часть: hr.demo-ctest#1 Правка", "harness-worker", "sonnet", ts=at(130)),
        finish("c-CT", ts=at(125)),
        launch("c-RL", "Часть: hr.demo-running#1 Оркестратор: контроль", "harness-lead", "opus",
               ts=at(60)),
        launch("c-RC", "Часть: hr.demo-running#2 Код", "harness-worker", "sonnet", "c-RL",
               ts=at(50)),
        finish("c-RC", ts=at(40)),
        launch("c-RR", "Часть: hr.demo-running#3 Ревью кода", "harness-worker", "opus", "c-RL",
               review=True, ts=at(30)),
    ]


def case_tasks_stages(stand: Path) -> None:
    root = git_stand(stand / "project", [("Начало стенда", at(300))])
    commit_file(root, "scripts/ports.py", "Сторож портов\n\nDev-Map: hr.demo-stages", at(110))
    # Трейлер ДРУГОЙ задачи с тем же началом id — не коммит голой задачи.
    commit_file(root, "scripts/bare_extra.py", "Чужая задача\n\nDev-Map: hr.demo-bare-extra",
                at(105))
    commit_file(root, "scripts/test_ctest.py", "Пробы: scripts/test_ctest.py\n\nDev-Map: hr.demo-ctest",
                at(120))
    task_dir(stand, "hr.demo-stages", {"harness-code-review.md": "# ревью\n", "мутации.log": "ок\n",
                                       "журнал.md": "scripts/test_ports.py красный, затем зелёный\n"})
    task_dir(stand, "hr.demo-bare", {"журнал.md": "код готов\n"})
    task_dir(stand, "hr.demo-bare-extra", {"harness-code-review.md": "# ревью\n",
                                           "мутации.log": "ок\n", "журнал.md": "test_extra\n"})
    out, _ = lost_report(stand, stage_rows())
    got = blocks(out)
    done, doing = got.get(HEADERS[0], []), got.get(HEADERS[1], [])
    stages = task_of(done, STAND_CARDS["hr.demo-stages"])
    PROBE.check("все бумаги, проба в журнале и коммит с трейлером — все пять этапов",
                field(stages, "этапы") == "тест ✔ код ✔ проверка ✔ мутации ✔ коммит ✔",
                stages or out)
    PROBE.check("«делали»: одна запись на модель, роли в порядке вёл, тесты, код, проверка",
                field(stages, "делали") == f"{LABELS['opus']} (вёл, тесты, проверка), "
                                           f"{LABELS['sonnet']} (код)", stages or out)
    bare = task_of(done, STAND_CARDS["hr.demo-bare"])
    PROBE.check("без бумаг и своего коммита — только «код ✔» (чужой id с тем же началом не в счёт)",
                field(bare, "этапы") == "код ✔", bare or out)
    PROBE.check("«делали: Sonnet (вёл, код)» — одна запись на модель",
                field(bare, "делали") == f"{LABELS['sonnet']} (вёл, код)", bare or out)
    ctest = task_of(done, STAND_CARDS["hr.demo-ctest"])
    PROBE.check("test_ в коммите задачи — «тест ✔», трейлер — «коммит ✔»",
                field(ctest, "этапы") == "тест ✔ код ✔ коммит ✔", ctest or out)
    running = task_of(doing, STAND_CARDS["hr.demo-running"])
    PROBE.check("идущая проверка — в ДЕЛАЕТСЯ «этап: …проверка…», без «проверка ✔»",
                "проверка" in field(running, "этап")
                and "проверка ✔" not in "\n".join(running.get("rest", [])), running or out)


def case_tasks_title(stand: Path) -> None:
    rows = [
        launch("c-L", "Часть: hr.demo-english#1 Оркестратор: Сделать статус понятным: registry, "
               "mutations, commit", "harness-lead", "opus", ts=day("11:00")),
        launch("c-W", "Часть: hr.demo-english#2 Code N2", "harness-worker", "sonnet", "c-L",
               ts=day("11:10")),
        finish("c-W"), finish("c-L"),
    ]
    out = run_report(stand, write_journal(stand, rows))
    titles = [t["title"] for t in parse_tasks(blocks(out).get(HEADERS[0], []))]
    PROBE.check("title карточки без кириллицы — заголовок из описания первого запуска",
                len(titles) == 1 and titles[0].startswith("Сделать статус понятным"), titles or out)
    PROBE.check("в заголовке и отчёте нет латиницы и английского title",
                not latin_words(out) and STAND_CARDS["hr.demo-english"] not in out,
                latin_words(out) or out)


def cut_rows(count: int) -> tuple:
    cards = {f"hr.demo-cut-{i}": f"Задача {i:02d}: сторож следит за длинным делом без пауз и сбоев"
             for i in range(count)}
    rows = []
    for i, cid in enumerate(cards):
        rows.append(launch(f"c-L{i}", f"Часть: {cid}#1 Оркестратор: дело", "harness-lead", "opus",
                           ts=day("12:30")))
        rows.append(launch(f"c-W{i}", f"Часть: {cid}#2 Код", "harness-worker", "sonnet", f"c-L{i}",
                           ts=day("12:40")))
        if i % 2 == 0:
            rows += [finish(f"c-W{i}"), finish(f"c-L{i}")]
    return cards, rows


def case_tasks_cut(stand: Path) -> None:
    cards, rows = cut_rows(12)
    add_cards(stand, cards)
    journal = write_journal(stand, rows)
    for ceiling in (700, TONE_HARD):
        conf = write_conf(stand, f"harness-cut-{ceiling}.conf", ceiling).name
        out = run_report(stand, journal, conf)
        more = [int(m.group(1)) for m in map(MORE_TASKS.match, out.splitlines()) if m]
        shown = [t["title"] for h in HEADERS[:2] for t in parse_tasks(blocks(out).get(h, []))]
        PROBE.check(f"потолок {ceiling}: длина {len(out)} ≤ {ceiling}", 0 < len(out) <= ceiling,
                    len(out))
        PROBE.check(f"потолок {ceiling}: сверх — строка «ещё N задач», показано + N = 12",
                    bool(more) and len(shown) + sum(more) == len(cards),
                    f"показано {len(shown)}, ещё {more}: {out[-300:]}")
        PROBE.check(f"потолок {ceiling}: ДАЛЬШЕ на месте, строки ≤ 80",
                    NEXT_TITLE in joined(blocks(out), HEADERS[2])
                    and all(len(l) <= 80 for l in out.splitlines()), out[-300:])
    out = run_report(stand, journal, "harness-wide.conf")
    PROBE.check(f"потолок {WIDE_CEILING}: всё влезло — 12 задач, «ещё N задач» нет",
                len(all_titles(out)) == len(cards) + 1
                and not any(MORE_TASKS.match(l) for l in out.splitlines()), out[-300:])


def live_conf() -> Path:
    """harness.conf рядом с пробой: в проекте harness/config, в пакете (harness/scripts) — ../config."""
    for path in (HERE.parent / "harness" / "config" / "harness.conf",
                 HERE.parent / "config" / "harness.conf"):
        if path.is_file():
            return path
    return Path()


def case_tasks_live_ceiling(stand: Path) -> None:
    path = live_conf()
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    match = re.search(r'^STATUS_STRUCT_MAX_CHARS="?(\d+)"?', text, re.MULTILINE)
    value = int(match.group(1)) if match else None
    PROBE.check(f"боевой STATUS_STRUCT_MAX_CHARS ({value}, {path}) ≤ {TONE_HARD} — жёсткий TONE.md",
                value is not None and 0 < value <= TONE_HARD, f"{path}: {value}")


# ── круг 2: метки «Часть:» почти ни у кого нет ─────────────────────────────
# Живой журнал 06.11 дал «прочее 6 / прочее 3» и ⚠ «#53 review V2 + mutations»: задачи
# выводятся из дерева запусков и русских описаний смены, а не только из меток.
PACK = "Оркестратор пачки мелких задач карты"


def case_tasks_pack_sick(stand: Path) -> None:
    rows = [
        launch("c-L", PACK, "harness-lead", "opus", ts=day("12:30")),
        launch("c-W", "Code N2 registry", "harness-worker", "sonnet", "c-L", ts=day("12:40")),
        launch("c-R", "Review V2 + mutations", "harness-worker", "opus", "c-L", ts=day("12:50")),
        finish("c-W", ts=day("13:00")), finish("c-R", ts=day("13:10")), finish("c-L", ts=day("13:20")),
    ]
    out = run_report(stand, write_journal(stand, rows))
    done = blocks(out).get(HEADERS[0], [])
    tasks = parse_tasks(done)
    PROBE.check("оркестратор без метки и два его исполнителя — ОДНА задача, «прочее» нет",
                len(tasks) == 1 and other_count(done) is None and "прочее" not in out, done or out)
    title = tasks[0]["title"] if tasks else ""
    PROBE.check(f"заголовок — описание целиком «{PACK}» (без двоеточия ярлык не режется)",
                title == PACK, title or out)
    PROBE.check("делали: Opus (вёл, проверка), Sonnet (код)",
                field(tasks[0] if tasks else {}, "делали")
                == f"{LABELS['opus']} (вёл, проверка), {LABELS['sonnet']} (код)", done or out)
    PROBE.check("английских описаний исполнителей в отчёте нет", not latin_words(out),
                latin_words(out) or out)


def case_tasks_inherit(stand: Path) -> None:
    rows = [
        launch("c-L", "Часть: hr.demo-status#1 Оркестратор: статус", "harness-lead", "opus",
               ts=day("12:30")),
        launch("c-W", "Code N2 registry", "harness-worker", "sonnet", "c-L", ts=day("12:40")),
        launch("c-G", "Small fix", "harness-worker", "haiku", "c-W", ts=day("12:50")),
    ]
    out = run_report(stand, write_journal(stand, rows))
    doing = blocks(out).get(HEADERS[1], [])
    task = task_of(doing, STATUS_TITLE)
    PROBE.check("исполнители без метки (и внук) — в задаче родителя, «прочее» нет",
                who_pairs(field(task, "делают")) == [(LABELS["opus"], "вёл"), (LABELS["sonnet"], "код"),
                                                     (LABELS["haiku"], "код")]
                and "прочее" not in out, doing or out)


def case_tasks_cyrillic(stand: Path) -> None:
    rows = [
        launch("c-L", "Оркестратор: сверка заявок владельца", "harness-lead", "sonnet",
               ts=day("12:30")),
        launch("c-W", "Code fix", "harness-worker", "sonnet", "c-L", ts=day("12:40")),
        launch("c-H", "Передача смены", "harness-worker", "opus", ts=day("12:45")),
        launch("c-E", "Shift handoff notes", "harness-worker", "haiku", ts=day("12:50")),
        finish("c-W"), finish("c-L"), finish("c-H"), finish("c-E"),
    ]
    out = run_report(stand, write_journal(stand, rows))
    done = blocks(out).get(HEADERS[0], [])
    titles = [t["title"].lower() for t in parse_tasks(done)]
    PROBE.check("оркестратор без метки с кириллицей — задача из описания без ярлыка роли",
                any(t.startswith("сверка заявок владельца") for t in titles)
                and "ркестратор" not in out, done or out)
    PROBE.check("одиночка без метки с кириллицей — своя задача «Передача смены»",
                "передача смены" in titles, done or out)
    PROBE.check("в «прочее» — только одиночка без кириллицы: «прочее 1»",
                other_count(done) == 1 and len(titles) == 2, done or out)


def case_tasks_lost_words(stand: Path) -> None:
    lost_at = at(200)
    rows = [
        launch("c-L", "Часть: hr.demo-memory#1 Оркестратор: правка памяти", "harness-lead", "opus",
               ts=at(260)),
        launch("c-R", "#53 review V2 + mutations", "harness-worker", "sonnet", "c-L", ts=lost_at),
        finish("c-L", ts=at(20)),
    ]
    out, got = lost_report(stand, rows)
    lost = got.get(LOST_HEADER, [])
    line = line_with(lost, STAND_CARDS["hr.demo-memory"])
    PROBE.check("⚠ называет задачу словами, с моделью и временем запуска",
                len(lost) == 1 and clock(lost_at) in line and LABELS["sonnet"] in line, lost or out)
    PROBE.check("в ⚠ нет ярлыка запуска «#53 review V2 + mutations»",
                not latin_words(joined(got, LOST_HEADER)) and "#53" not in out, lost or out)


def case_tasks_part_field(stand: Path) -> None:
    # Метка в описании другой карточки: судит data.часть, а не текст описания.
    rows = [
        launch("c-L", "Оркестратор: статус", "harness-lead", "opus", ts=day("12:30"),
               part="Часть: hr.demo-status#1"),
        launch("c-W", "Code N2 registry", "harness-worker", "sonnet", "c-L", ts=day("12:40"),
               part="Часть: hr.demo-status#2"),
        launch("c-X", "Часть: hr.demo-cleanup#1 Сборщик", "harness-worker", "haiku",
               ts=day("12:50"), part="Часть: hr.demo-format#1"),
        finish("c-W"), finish("c-L"), finish("c-X"),
    ]
    out = run_report(stand, write_journal(stand, rows))
    done = blocks(out).get(HEADERS[0], [])
    PROBE.check("два запуска с data.часть одной карточки — одна задача с заголовком карты",
                who_pairs(field(task_of(done, STATUS_TITLE), "делали"))
                == [(LABELS["opus"], "вёл"), (LABELS["sonnet"], "код")], done or out)
    PROBE.check("data.часть главнее метки в описании: задача «формат», не «уборка»",
                bool(task_of(done, FORMAT)) and CLEANUP not in out, done or out)
    PROBE.check("«прочее» нет, английских слов нет", "прочее" not in out and not latin_words(out),
                out)


def case_tasks_test_words(stand: Path) -> None:
    rows = [launch("c-L", "Часть: hr.demo-roles#1 Оркестратор: роли", "harness-lead", "opus",
                   ts=day("12:30"))]
    words = ("Тест двери статуса", "Проба формата", "Пробы стенда", "test run", "Probe ports")
    for i, desc in enumerate(words):
        rows.append(launch(f"c-T{i}", f"Часть: hr.demo-roles#{i + 2} {desc}", "harness-worker",
                           "sonnet", "c-L", ts=day("12:40")))
    rows.append(launch("c-C", "Часть: hr.demo-roles#9 Сборщик статуса", "harness-worker", "haiku",
                       "c-L", ts=day("12:50")))
    rows += [finish(r["data"]["вызов"]) for r in list(rows)]
    out = run_report(stand, write_journal(stand, rows))
    pairs = who_pairs(field(task_of(blocks(out).get(HEADERS[0], []), STAND_CARDS["hr.demo-roles"]),
                            "делали"))
    PROBE.check("тест/проба/пробы/test/probe — «тесты», остальное — «код»",
                pairs == [(LABELS["opus"], "вёл"), (LABELS["sonnet"], "тесты"),
                          (LABELS["haiku"], "код")], pairs or out)


def case_tasks_one_entry(stand: Path) -> None:
    rows = [
        launch("c-L", "Часть: hr.demo-roles#1 Оркестратор: роли", "harness-lead", "opus",
               ts=day("12:30")),
        launch("c-R", "Часть: hr.demo-roles#2 Ревью сборщика", "harness-worker", "opus", "c-L",
               ts=day("12:35")),
        launch("c-S", "Часть: hr.demo-roles#3 Сборщик статуса", "harness-worker", "sonnet", "c-L",
               ts=day("12:40")),
        launch("c-T", "Часть: hr.demo-roles#4 Пробы сборщика", "harness-worker", "opus", "c-L",
               ts=day("12:45")),
        launch("c-C", "Часть: hr.demo-roles#5 Правка вывода", "harness-worker", "opus", "c-L",
               ts=day("12:50")),
        launch("c-H", "Часть: hr.demo-roles#6 Сверка списка", "harness-worker", "haiku", "c-L",
               ts=day("12:55")),
    ]
    rows += [finish(r["data"]["вызов"]) for r in list(rows)]
    out = run_report(stand, write_journal(stand, rows))
    task = task_of(blocks(out).get(HEADERS[0], []), STAND_CARDS["hr.demo-roles"])
    PROBE.check("одна запись на модель по первому запуску, роли в порядке вёл, тесты, код, проверка",
                field(task, "делали") == f"{LABELS['opus']} (вёл, тесты, код, проверка), "
                                         f"{LABELS['sonnet']} (код), {LABELS['haiku']} (код)",
                task or out)


# ── по ревью: возобновление, окно, отметка по задачам ──────────────────────
# Живое 06.11: исполнитель «Починка: ожидание ротации…» закончен 14 раз — после SendMessage
# он снова работал, а статус писал «пока ничего» и каждый раз повторял его в СДЕЛАНО.
def resume(call: str, ts: str) -> dict:
    """Возобновление через SendMessage: запуск снова работает после своего конца."""
    return {"v": 1, "ts": ts, "src": "scripts/hooks/agent_budget_guard.py", "event": "agent.resume",
            "outcome": "ok", "task": "hr.demo-wip", "session": SESSION, "data": {"вызов": call}}


def mark_now(stand: Path, conf: str, moment: str) -> None:
    """Вызов после доставки, как у tg_send.sh: --отметить с ISO-временем."""
    done = subprocess.run([sys.executable, str(REPORT), "--отметить", moment], capture_output=True,
                          text=True, timeout=60, env=stand_env(stand, conf), cwd=str(stand))
    if done.returncode != 0:
        raise RuntimeError(f"--отметить: код {done.returncode}: {done.stderr.strip()[-300:]}")


def resumed_rows() -> list:
    return [
        launch("c-L", "Оркестратор: формат", "harness-lead", "opus", ts=at(120),
               part="Часть: hr.demo-format#1"),
        launch("c-W", "Код сборщика", "harness-worker", "sonnet", "c-L", ts=at(110),
               part="Часть: hr.demo-format#2"),
        finish("c-W", ts=at(90)), finish("c-L", ts=at(60)),
    ]


def case_resume_running(stand: Path) -> None:
    rows = resumed_rows() + [resume("c-L", at(30))]
    out, got = lost_report(stand, rows)
    PROBE.check("возобновлённый после конца ведущий — задача в ДЕЛАЕТСЯ, не в СДЕЛАНО",
                bool(task_of(got.get(HEADERS[1], []), FORMAT))
                and not task_of(got.get(HEADERS[0], []), FORMAT), out)
    PROBE.check("«делают» — возобновлённый ведущий",
                who_pairs(field(task_of(got.get(HEADERS[1], []), FORMAT), "делают"))
                == [(LABELS["opus"], "вёл")], out)


def case_resume_done_once(stand: Path) -> None:
    conf = mode_conf(stand, "1")
    journal = write_journal(stand, resumed_rows() + [resume("c-L", at(30)), finish("c-L", ts=at(10))])
    set_mark(stand, at(40))
    out, done, doing = report_since(stand, journal, conf)
    PROBE.check("новый конец после resume и после метки — задача в СДЕЛАНО один раз",
                all_titles(out).count(FORMAT) == 1 and bool(task_of(done, FORMAT)), out)
    set_mark(stand, at(0))
    out, _, _ = report_since(stand, journal, conf)
    PROBE.check("после отметки — задачи нет нигде", FORMAT not in out, out)
    journal = write_journal(stand, resumed_rows() + [finish("c-L", ts=at(5))])
    set_mark(stand, at(30))
    out, done, _ = report_since(stand, journal, conf)
    PROBE.check("повторный finish без resume после метки — не новый конец, в СДЕЛАНО не повторён",
                not task_of(done, FORMAT), done or out)


def case_long_lead(stand: Path) -> None:
    deep = STAND_CARDS["hr.demo-deep"]
    rows = [launch("c-L", "Оркестратор: долгая сверка", "harness-lead", "opus", ts=at(780),
                   part="Часть: hr.demo-deep#1"),
            launch("c-W", "Сверка половины", "harness-worker", "sonnet", "c-L", ts=at(60),
                   part="Часть: hr.demo-deep#2"),
            finish("c-W", ts=at(30))]
    journal = write_journal(stand, rows)
    for mode in ("1", "0"):
        set_mark(stand, at(45))
        out, done, doing = report_since(stand, journal, mode_conf(stand, mode))
        PROBE.check(f"STATUS_DONE_SINCE_LAST={mode}: ведущий старше окна 12 ч без конца — в ДЕЛАЕТСЯ",
                    bool(task_of(doing, deep)) and not task_of(done, deep), out)


def case_cut_reported(stand: Path) -> None:
    cards = {f"hr.demo-rep-{i}": f"Сделанная задача {i:02d}: сторож следит за длинным делом без пауз"
             for i in range(12)}
    add_cards(stand, cards)
    rows = []
    for i, cid in enumerate(cards):
        rows += [launch(f"c-L{i}", "Оркестратор: дело", "harness-lead", "opus", ts=at(200),
                        part=f"Часть: {cid}#1"),
                 launch(f"c-W{i}", "Код", "harness-worker", "sonnet", f"c-L{i}", ts=at(190),
                        part=f"Часть: {cid}#2"),
                 finish(f"c-W{i}", ts=at(150)), finish(f"c-L{i}", ts=at(140))]
    journal = write_journal(stand, rows)
    conf = write_conf(stand, "harness-rep.conf", 700).name
    with (stand / conf).open("a", encoding="utf-8") as out_file:
        out_file.write('STATUS_DONE_SINCE_LAST="1"\n')
    set_mark(stand, at(240))
    seen, repeated, first = [], [], ""
    for _ in range(8):
        out, done, _ = report_since(stand, journal, conf)
        first = first or out
        titles = [t["title"] for t in parse_tasks(done)]
        if not titles:
            break
        repeated += [t for t in titles if t in seen]
        seen += titles
        mark_now(stand, conf, at(0))
        time.sleep(1.1)
    PROBE.check("условие: первый статус свернул часть задач в «ещё N задач»",
                any(MORE_TASKS.match(l) for l in first.splitlines()), first[-300:])
    tasks = [t for t in parse_tasks(blocks(first).get(HEADERS[0], []))]
    PROBE.check("свёртка сперва снимает «делали/этапы»: есть показанная задача без примечаний",
                any(not t["rest"] for t in tasks), first)
    PROBE.check("не влезшие задачи пришли следующими статусами: все 12 по разу",
                sorted(seen) == sorted(cards.values()) and not repeated,
                f"видели {len(set(seen))} из 12, повторы {repeated[:2]}")


def case_lead_without_mark_parent(stand: Path) -> None:
    rows = [
        launch("c-L", PACK, "harness-lead", "opus", ts=day("12:30")),
        launch("c-A", "Статус", "harness-worker", "sonnet", "c-L", ts=day("12:40"),
               part="Часть: hr.demo-status#1"),
        launch("c-B", "Формат", "harness-worker", "haiku", "c-L", ts=day("12:50"),
               part="Часть: hr.demo-format#1"),
        finish("c-A"), finish("c-B"), finish("c-L", ts=day("13:10")),
    ]
    out = run_report(stand, write_journal(stand, rows))
    titles = [t["title"] for t in parse_tasks(blocks(out).get(HEADERS[0], []))]
    PROBE.check("ведущий без метки с детьми-задачами — не отдельная задача, не «прочее»",
                sorted(titles) == sorted([STATUS_TITLE, FORMAT]) and PACK not in out
                and "прочее" not in out, titles or out)


def case_doing_running_only(stand: Path) -> None:
    rows = [
        launch("c-L", "Оркестратор: формат", "harness-lead", "opus", ts=day("12:30"),
               part="Часть: hr.demo-format#1"),
        launch("c-T", "Пробы формата", "harness-worker", "haiku", "c-L", ts=day("12:40"),
               part="Часть: hr.demo-format#2"),
        finish("c-T", ts=day("12:50")),
        launch("c-C", "Код сборщика", "harness-worker", "sonnet", "c-L", ts=day("13:00"),
               part="Часть: hr.demo-format#3"),
    ]
    out = run_report(stand, write_journal(stand, rows))
    task = task_of(blocks(out).get(HEADERS[1], []), FORMAT)
    PROBE.check("«делают» — только идущие: Opus (вёл), Sonnet (код)",
                who_pairs(field(task, "делают")) == [(LABELS["opus"], "вёл"), (LABELS["sonnet"], "код")],
                task or out)
    PROBE.check("законченный участник идущей задачи — в «делали»",
                (LABELS["haiku"], "тесты") in who_pairs(field(task, "делали")), task or out)


def case_commits_human(stand: Path) -> None:
    write_journal(stand, lead_rows())
    set_mark(stand, at(100))
    git_stand(stand / "project", [("Карта: три карточки plan из мутаций hr.rotation-hold-not-mute-owner",
                                   at(20))])
    line = commits_line(stand)
    PROBE.check("условие: строка 📝 есть", line.startswith(COMMITS_HEAD), line)
    PROBE.check("в строке 📝 нет id карточки и слова plan",
                not CARD_ID.search(line) and "plan" not in line, line)


def case_review_stage(stand: Path) -> None:
    rows = [launch("c-C", "Код сборщика", "harness-worker", "sonnet", ts=day("12:30"),
                   part="Часть: hr.demo-bare#1"),
            launch("c-R", "Ревью сборщика", "harness-worker", "opus", ts=day("12:40"),
                   part="Часть: hr.demo-bare#2"),
            finish("c-C"), finish("c-R", ts=day("13:10"))]
    out = run_report(stand, write_journal(stand, rows))
    task = task_of(blocks(out).get(HEADERS[0], []), STAND_CARDS["hr.demo-bare"])
    PROBE.check("законченный запуск с ролью «проверка» без бумаги ревью — «проверка ✔»",
                field(task, "этапы") == "код ✔ проверка ✔", task or out)


def case_config_defaults(stand: Path) -> None:
    folder = live_conf().parent
    jobs = folder / "system-jobs.yaml"
    jobs_text = jobs.read_text(encoding="utf-8") if jobs.is_file() else ""
    match = re.search(r'^\s*STATUS_STRUCT_MAX_CHARS:\s*"(\d+)"', jobs_text, re.MULTILINE)
    PROBE.check(f"умолчание установки в {jobs.name}: STATUS_STRUCT_MAX_CHARS «1400»",
                bool(match) and match.group(1) == str(FALLBACK_CEILING), match.group(0) if match else jobs)
    done = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                           "import config; print(config.status_ceiling({}))", str(HERE / "lib")],
                          capture_output=True, text=True, timeout=60)
    PROBE.check("запасной потолок config.status_ceiling({}) — 1400",
                done.stdout.strip() == str(FALLBACK_CEILING), done.stdout + done.stderr[-200:])
    conf_text = live_conf().read_text(encoding="utf-8") if live_conf().is_file() else ""
    PROBE.check("ключа STATUS_ROLE_LABELS нет ни в harness.conf, ни в system-jobs.yaml",
                bool(conf_text) and "STATUS_ROLE_LABELS" not in conf_text + jobs_text, folder)


def case_window_parent(stand: Path) -> None:
    rows = [launch("c-L", "Оркестратор: формат", "harness-lead", "opus", ts=at(800),
                   part="Часть: hr.demo-format#1"),
            finish("c-L", ts=at(700)),
            launch("c-A", "Правка схемы", "harness-worker", "sonnet", "c-L", ts=at(650)),
            launch("c-B", "Правка экрана", "harness-worker", "sonnet", "c-L", ts=at(640)),
            finish("c-A", ts=at(600)), finish("c-B", ts=at(600))]
    journal = write_journal(stand, rows)
    out, done, _ = report_since(stand, journal, mode_conf(stand, "0"))
    titles = [t["title"] for t in parse_tasks(done)]
    PROBE.check("режим окна: родитель вне окна держит задачу детей — одна задача «формат»",
                titles == [FORMAT], titles or out)


# ── ревью круга 2: контейнер, resume без жизни, ДАЛЬШЕ без сделанного, урезка ─
def container_rows() -> list:
    return [
        launch("c-L", PACK, "harness-lead", "opus", ts=at(100)),
        launch("c-A", "Код первой", "harness-worker", "sonnet", "c-L", ts=at(90),
               part="Часть: hr.demo-status#1"),
        launch("c-B", "Код второй", "harness-worker", "haiku", "c-L", ts=at(80),
               part="Часть: hr.demo-format#1"),
        finish("c-A", ts=at(70)), finish("c-B", ts=at(60)),
    ]


def case_container_running(stand: Path) -> None:
    out, got = lost_report(stand, container_rows())
    doing = got.get(HEADERS[1], [])
    leads = [t for t in parse_tasks(doing) if (LABELS["opus"], "вёл") in who_pairs(field(t, "делают"))]
    PROBE.check("БОЛЬНОЙ: идущий ведущий без метки с детьми-задачами — «вёл» в ДЕЛАЕТСЯ",
                bool(leads) and doing != ["пока ничего"], doing or out)
    PROBE.check("ведущий не стал отдельной задачей и не потерян",
                PACK not in all_titles(out) and LOST_HEADER not in out, out)
    out, got = lost_report(stand, container_rows() + [finish("c-L", ts=at(10))])
    titles = all_titles(out)
    PROBE.check("после конца ведущего: ДЕЛАЕТСЯ пусто, задачи детей по разу",
                got.get(HEADERS[1]) == ["пока ничего"] and titles.count(STATUS_TITLE) == 1
                and titles.count(FORMAT) == 1, out)


def case_resume_without_life(stand: Path) -> None:
    rows = [launch("c-W", "Код шага", "harness-worker", "sonnet", ts=at(260),
                   part="Часть: hr.demo-bare#1"),
            finish("c-W", ts=at(200)), resume("c-W", at(150))]
    out, got = lost_report(stand, rows)
    PROBE.check("resume без единого события работы 2,5 ч — не в ⚠ ПОТЕРЯН",
                LOST_HEADER not in out, out)
    PROBE.check("и не висит в ДЕЛАЕТСЯ", not task_of(got.get(HEADERS[1], []), STAND_CARDS["hr.demo-bare"]),
                out)


def case_next_without_done(stand: Path) -> None:
    rows = [launch("c-W", "Уборка", "harness-worker", "sonnet", ts=day("12:30"),
                   part="Часть: hr.demo-next#1"), finish("c-W")]
    out = run_report(stand, write_journal(stand, rows))
    PROBE.check("условие: задача из очереди сделана — в СДЕЛАНО",
                bool(task_of(blocks(out).get(HEADERS[0], []), NEXT_TITLE)), out)
    PROBE.check("сделанная задача в ДАЛЬШЕ не повторена",
                NEXT_TITLE not in joined(blocks(out), HEADERS[2]), out)


LONG_TITLE = ("Сторож убирает за сменой забытые открытые порты и сообщает владельцу, "
              "какой порт остался слушать после ухода смены")


def case_title_word_cut(stand: Path) -> None:
    add_cards(stand, {"hr.demo-longtitle": LONG_TITLE})
    rows = [launch("c-W", "Код", "harness-worker", "sonnet", ts=day("12:30"),
                   part="Часть: hr.demo-longtitle#1"), finish("c-W")]
    out = run_report(stand, write_journal(stand, rows))
    line = line_with(out.splitlines(), "Сторож убирает")
    head = TASK_LINE.match(line).group(2) if TASK_LINE.match(line) else ""
    kept = head.removesuffix("…").rstrip(" ,;:—-")
    PROBE.check("условие: заголовок урезан до ≤ 80 с «…»",
                len(line) <= 80 and head.endswith("…") and LONG_TITLE.startswith(kept), line or out)
    PROBE.check("урезка по границе слова, не посреди",
                bool(kept) and LONG_TITLE[len(kept):len(kept) + 1] in (" ", ",", ";", ":"), line)


def case_lost_task_done(stand: Path) -> None:
    # Задача уже сдана (карточка test или коммит с её Dev-Map после запуска) — потерянный помощник не тревога.
    with (stand / "dev-map.yaml").open("a", encoding="utf-8") as out_file:
        out_file.write('  - id: hr.demo-tested\n    status: test\n    title: "Сданная задача"\n')
    rows = [launch("c-T", "Код", "harness-worker", "sonnet", ts=at(200), part="Часть: hr.demo-tested#1")]
    out, _ = lost_report(stand, rows)
    PROBE.check("БОЛЬНОЙ: задача в test — её потерянного помощника в ⚠ нет", LOST_HEADER not in out, out)
    rows = [launch("c-P", "Код", "harness-worker", "sonnet", ts=at(200), part="Часть: hr.demo-young#1")]
    out, got = lost_report(stand, rows)
    PROBE.check("та же картина с задачей в plan — помощник в ⚠",
                bool(line_with(got.get(LOST_HEADER, []), STAND_CARDS["hr.demo-young"])), out)
    git_stand(stand / "project", [("Сверка заявок\n\nDev-Map: hr.demo-young", at(100))])
    out, _ = lost_report(stand, rows)
    PROBE.check("коммит с Dev-Map задачи после запуска — в ⚠ нет", LOST_HEADER not in out, out)


CASES = (
    ("(1) нет работающих → «пока ничего», без метки → «прочее 1»", case_empty_doing),
    ("(2) группировка по задачам: «делали/делают» с подписями и ролями", case_nesting),
    ("(3) потолок из данных, строки ≤ 80", case_ceiling),
    ("(4) роли словами: вёл, тесты, код, проверка, помощник", case_roles),
    ("(5) без id карточек и «Часть:», задачи заголовками карты", case_no_ids),
    ("(6) tg_send.sh --статус и сторож канала", case_channel),
    ("(7) ДАЛЬШЕ из очереди со сквозным номером", case_next),
    ("(8) СДЕЛАНО/ДЕЛАЕТСЯ задачи из журнала", case_journal),
    ("(9 A) --статус с ручным текстом — отказ", case_manual_refused),
    ("(10 B) --статус без текста зовёт сборщик, сторож пропускает", case_door_builds),
    ("(11 C) без ключа потолка сборщик и сторож делят запасной 1400", case_shared_fallback),
    ("(12 D) роль по типу и флагу, не по модели", case_icon_by_type),
    ("(13 E) свёрнутые «ещё N задач» сходятся по счёту", case_collapsed_signed),
    ("(14) заголовок из описания без ярлыков «Оркестратор:»/«Управляющий:»", case_role_prefix),
    ("(15) подписи из STATUS_MODEL_LABELS, STATUS_ROLE_LABELS не в отчёте", case_role_labels_from_conf),
    ("(16) без ключа ролей и с частичным — форма та же", case_role_labels_default),
    ("(17) задача с идущим запуском — только в ДЕЛАЕТСЯ, один раз", case_child_once),
    ("(18) законченная задача показана один раз", case_lead_once),
    ("(19) нового нет — «с HH:MM нового нет; последний помощник кончил HH:MM»", case_nothing_new),
    ("(20) STATUS_DONE_SINCE_LAST=0 — старое окно 12 ч", case_window_mode),
    ("(21) метку пишет только --отметить, не сборка", case_mark_command),
    ("(22) два статуса подряд через tg_send — без повторов", case_two_statuses),
    ("(23) отказ доставки метку не двигает", case_refused_keeps_mark),
    ("(24) ДАЛЬШЕ — 2–3 следующих без «сейчас делаем»", case_next_list),
    ("(25) ДАЛЬШЕ при пустой очереди — «очередь пуста»", case_next_empty),
    ("(26) оркестратор кончил до метки, исполнитель после — задача по исполнителю",
     case_lead_before_mark),
    ("(27) параллельные --отметить не падают и пишут целое ISO", case_mark_race),
    ("(28) коммиты после метки — строка 📝 с темой последнего", case_commits_shown),
    ("(29) правки памяти с панели не считаются коммитами", case_commits_panel),
    ("(30) простой — «с HH:MM нового нет; последний помощник кончил HH:MM»", case_idle_phrase),
    ("(31) нет репозитория, git или корня — строки 📝 нет, сборка не падает", case_commits_no_repo),
    ("(32) без --сессия — все сессии журнала, работа прошлой сессии видна", case_all_sessions),
    ("(33) --сессия X по-прежнему фильтрует", case_session_filter),
    ("(34) СДЕЛАНО непусто только из-за коммитов", case_commits_only),
    ("(35) длинная тема коммита обрезана, отчёт ≤ 700 и проходит сторожа", case_commit_truncated),
    ("(36 a) сирота старше порога — в ⚠ ПОТЕРЯН с временем, не в ДЕЛАЕТСЯ", case_lost_orphan),
    ("(37 b) тот же запуск моложе порога — в ДЕЛАЕТСЯ, блока нет", case_lost_young),
    ("(38 c) оркестратор кончил, ребёнок потерян — ДЕЛАЕТСЯ пусто (04.11)", case_lost_real),
    ("(39 d) оркестратор без конца: потерянный в ⚠, свежий в ДЕЛАЕТСЯ", case_lost_sibling),
    ("(40 e) свежий конец потомка любой глубины — не потерян", case_lost_descendant_finish),
    ("(41) свежая запись с data.вызов — не потерян", case_lost_own_record),
    ("(42 f) пять потерянных — 3 строки и «и ещё 2»", case_lost_more),
    ("(43 g) STATUS_LOST_HOURS 1 и 6 меняют исход журнала", case_lost_hours),
    ("(44 h) нет ключа или мусор — порог 2 ч", case_lost_garbage),
    ("(45 i) потерянных нет — отчёт по задачам байт в байт", case_lost_none_same),
    ("(46 j) с блоком ⚠ отчёт в потолке, строки ≤ 80", case_lost_ceiling),
    ("(47) режим метки: корень вне окна, живший после метки, — в ⚠", case_lost_mark_mode),
    ("(48) STATUS_LOST_HOURS inf, 1e20, -5, nan, abc — не падает, порог 2 ч",
     case_lost_hours_extreme),
    ("(49) потерянный ребёнок: корень и законченный внук — одна задача", case_lost_middle),
    ("(50) потерянный корень: задача в СДЕЛАНО по законченному ребёнку", case_lost_root_done_child),
    ("(51) БОЛЬНОЙ: два помощника «Часть: X#1» — одна задача, заголовок карты, без английского",
     case_tasks_sick),
    ("(52) помощники без метки — «прочее N» в своём блоке", case_tasks_other),
    ("(53) этапы по артефактам задачи и коммиту, «делали» как в образце", case_tasks_stages),
    ("(54) title без кириллицы — заголовок из описания без латиницы", case_tasks_title),
    ("(55) урезка по длине 700/1400 — «ещё N задач»", case_tasks_cut),
    ("(56) боевой STATUS_STRUCT_MAX_CHARS ≤ 1400", case_tasks_live_ceiling),
    ("(57) БОЛЬНОЙ: «Оркестратор пачки мелких задач карты» и исполнители без меток — одна задача",
     case_tasks_pack_sick),
    ("(61) БОЛЬНОЙ: метка из data.часть, описание без метки — задача из карты", case_tasks_part_field),
    ("(62) роль «тесты» по слову тест/проба/пробы/test/probe", case_tasks_test_words),
    ("(63) несколько ролей одной модели — одна запись, порядок вёл, тесты, код, проверка",
     case_tasks_one_entry),
    ("(64) БОЛЬНОЙ: возобновлённый через SendMessage — в ДЕЛАЕТСЯ", case_resume_running),
    ("(65) после resume новый конец — в СДЕЛАНО один раз; повторный finish без resume не повтор",
     case_resume_done_once),
    ("(66) ведущий старше окна без конца — в ДЕЛАЕТСЯ", case_long_lead),
    ("(67) свёрнутые сделанные приходят следующими статусами — отметка по задачам", case_cut_reported),
    ("(68) ведущий без метки с детьми-задачами — не задача", case_lead_without_mark_parent),
    ("(69) «делают» — только идущие, законченные — в «делали»", case_doing_running_only),
    ("(70) строка 📝 без id карточки и plan", case_commits_human),
    ("(71) «проверка ✔» за законченный запуск проверки", case_review_stage),
    ("(72) умолчание потолка 1400, STATUS_ROLE_LABELS убран", case_config_defaults),
    ("(73) режим окна: родитель вне окна держит задачу детей", case_window_parent),
    ("(74) БОЛЬНОЙ: идущий ведущий без метки с детьми-задачами — в ДЕЛАЕТСЯ, не пропал",
     case_container_running),
    ("(75) resume без событий работы — не в ⚠ через 2 ч", case_resume_without_life),
    ("(76) сделанная задача не повторяется в ДАЛЬШЕ", case_next_without_done),
    ("(77) урезка заголовка по границе слова", case_title_word_cut),
    ("(78) БОЛЬНОЙ: ⚠ без помощников уже сданной задачи (test/done/принято или коммит)",
     case_lost_task_done),
    ("(58) исполнитель без метки наследует задачу родителя", case_tasks_inherit),
    ("(59) без метки с кириллицей — своя задача, без кириллицы — «прочее»", case_tasks_cyrillic),
    ("(60) ⚠ ПОТЕРЯН — задача словами, модель и время", case_tasks_lost_words),
)


def main() -> int:
    print(f"модуль: {REPORT}" + ("" if REPORT.is_file() else " — НЕТ"))
    for title, case in CASES:
        PROBE.start(title)
        stand = make_stand()
        try:
            case(stand)
        except Exception as error:      # поломка механизма — красный случай, не трасса
            PROBE.check("случай не дошёл до суда", False, f"{type(error).__name__}: {error}")
        finally:
            shutil.rmtree(stand, ignore_errors=True)
            PROBE.finish()
    print(f"проверок {PROBE.checks}, неудачных {PROBE.failed}")
    print(f"ОТЧЁТ СТАТУСА: случаев {PROBE.cases}, красных {PROBE.failed_cases}, "
          f"зелёных {PROBE.cases - PROBE.failed_cases}")
    return 0 if PROBE.failed_cases == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
