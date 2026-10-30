#!/usr/bin/env python3
"""Немота агента по журналу сессии: минуты с последнего УСПЕШНОГО ответа.

Зачем отдельный прибор. Про «работает ли смена» судили двое и разными мерками:
`session-warden.sh` читал записи журнала и отбрасывал ответы об ошибке,
`limit-watch.sh` смотрел на `stat -c %Y` транскрипта — то есть считал работой
ЛЮБУЮ дописанную строку. Пинок сторожа дописывает строку, отказ 429 дописывает
строку. Улика 26.09.2026: 21:20 сторож пишет «лимит подписки: тишина 25 мин —
пробую разбудить смену», 21:21 ежеминутный пишет владельцу «работа пошла снова,
простой 0 мин». Работа при этом стояла. Всего таких «простой 0 мин» в журнале
пять (ревью 27.09.2026, F-01).

Тот же урок, что у `limit-in-log.py`: у факта один судья, и живёт он в одном
файле, который зовут оба сторожа.

Ответ об ошибке работой не считается — улика 12.09.2026: лимит держался 160
минут, CLI записал 20 сообщений «error: rate_limit, apiErrorStatus: 429» с
типом assistant, прибор считал их работой, и владелец остался без единого
слова.

Тот же прибор судит и РАБОТУ ПОМОЩНИКОВ. 25.10.2026 главная сессия 21 минуту
стояла у приглашения и ждала уведомления помощника, помощник отвечал каждые
полминуты, а сторож объявил «Работа встала» — он смотрел только на главную.
Работа сессии = последний успешный ответ главной ИЛИ живого помощника.

Прогон:
    python3 scripts/stall-in-log.py <транскрипт>   # минуты с последнего ответа
    python3 scripts/stall-in-log.py --работа <транскрипт|sid> [--на <ISO UTC>]
    python3 scripts/stall-in-log.py --selftest
Код возврата: 0 — ответ найден (минуты на stdout), 1 — ответов нет или файл
нечитаем. «Не прочитали» и «ответов нет» здесь одно: в обоих случаях сказать о
немоте нечего, а положительного утверждения прибор не делает.
У `--работа` ещё код 3: реестр помощников не узнал живой транскрипт — судить о
помощниках нечем, и «помощников 0» не печатается (молчать о настоящей работе
хуже, чем отказаться).
"""
import datetime
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Хвост, а не весь файл: транскрипт растёт до сотен мегабайт, а нужен последний
# ответ. То же число, что у остальных веток сторожа.
ХВОСТ_БАЙТ = int(os.environ.get("STALL_TAIL_BYTES", 4_000_000))


def последний_ответ(путь: str, хвост: int = ХВОСТ_БАЙТ):
    """→ метка времени последнего успешного ответа ассистента, либо None."""
    try:
        with open(путь, "rb") as файл:
            файл.seek(0, os.SEEK_END)
            файл.seek(max(0, файл.tell() - хвост))
            текст = файл.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    метка = None
    for строка in текст.splitlines():
        строка = строка.strip()
        if not строка or '"assistant"' not in строка:
            continue
        try:
            запись = json.loads(строка)
        except ValueError:
            continue
        if запись.get("type") != "assistant":
            continue
        if запись.get("isApiErrorMessage") or запись.get("error"):
            continue
        if запись.get("timestamp"):
            метка = запись["timestamp"]
    return метка


def немота_минут(путь: str, хвост: int = ХВОСТ_БАЙТ, сейчас: float | None = None):
    """→ минуты с последнего успешного ответа, либо None."""
    метка = последний_ответ(путь, хвост)
    if not метка:
        return None
    try:
        момент = datetime.datetime.fromisoformat(str(метка).replace("Z", "+00:00"))
    except ValueError:
        return None
    сейчас = сейчас if сейчас is not None else time.time()
    return max(0, int((сейчас - момент.timestamp()) // 60))


# ── работа помощников ───────────────────────────────────────────────────────
AGENT_ID = re.compile(r"a[0-9a-f]{16}")
NOTIFICATION = re.compile(r"<task-id>(a[0-9a-f]{16})</task-id>")
# Подстроки отбора строк: транскрипт растёт до сотен мегабайт, а разбирать JSON
# нужно только там, где может лежать запуск, возобновление или уведомление.
LEDGER_NEEDLES = ("async_launched", "SendMessage", "<task-id>a")
HELPER_FRESH_DEFAULT_MIN = 20


class LedgerRefusal(Exception):
    """Реестр запусков не узнал живой транскрипт помощника: формат записи сменился."""


@dataclass
class LedgerEntry:
    started_at: float | None = None
    resumed_at: float | None = None
    notified_at: float | None = None   # самое раннее уведомление ПОСЛЕ последнего подъёма

    @property
    def activated_at(self) -> float | None:
        moments = [m for m in (self.started_at, self.resumed_at) if m is not None]
        return max(moments) if moments else None


@dataclass
class Helper:
    agent_id: str
    description: str
    answer_min: int
    running_min: int


@dataclass
class Judgement:
    work_sec: int | None
    main_min: int | None
    helpers: list = field(default_factory=list)

    @property
    def work_min(self) -> int | None:
        return None if self.work_sec is None else self.work_sec // 60


def _epoch(stamp) -> float | None:
    try:
        return datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fresh_minutes() -> int:
    """Порог свежести помощника: окружение старше файла, число из харнес-конфига."""
    raw = os.environ.get("HELPER_FRESH_MIN")
    if not raw:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
        try:
            import config
            raw = config.из_файла(config.путь_harness_conf()).get("HELPER_FRESH_MIN")
        except ImportError:
            raw = None
    return int(raw) if raw and raw.isdigit() else HELPER_FRESH_DEFAULT_MIN


def _lines(path, needles=None):
    """Разобранные записи файла; needles — отбор строк по подстрокам до разбора."""
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if needles and not any(needle in line for needle in needles):
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _notification_text(record) -> str:
    kind = record.get("type")
    if kind == "queue-operation":
        text = record.get("content")
    elif kind == "attachment":
        attachment = record.get("attachment")
        text = attachment.get("prompt") if isinstance(attachment, dict) else None
    elif kind == "user":
        message = record.get("message")
        text = message.get("content") if isinstance(message, dict) else None
        if isinstance(text, list):
            text = " ".join(b.get("text", "") for b in text
                            if isinstance(b, dict) and b.get("type") == "text")
    else:
        return ""
    return text if isinstance(text, str) and "<task-notification>" in text else ""


def _ledger_events(record):
    """→ [(вид, id помощника, момент)]: start | resume | notify."""
    moment = _epoch(record.get("timestamp"))
    if moment is None:
        return []
    events = []
    result = record.get("toolUseResult")
    if isinstance(result, dict) and result.get("status") == "async_launched":
        agent_id = str(result.get("agentId") or "")
        if AGENT_ID.fullmatch(agent_id):
            events.append(("start", agent_id, moment))
    if record.get("type") == "assistant":
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "SendMessage":
                target = str((block.get("input") or {}).get("to") or "")
                if AGENT_ID.fullmatch(target):
                    events.append(("resume", target, moment))
    for agent_id in NOTIFICATION.findall(_notification_text(record)):
        events.append(("notify", agent_id, moment))
    return events


def helpers_dir(main_path) -> Path:
    return Path(str(main_path)[:-len(".jsonl")]) / "subagents"


def _helper_files(main_path):
    return sorted(helpers_dir(main_path).glob("agent-*.jsonl"))


def helper_ledger(main_path, at: float | None = None) -> dict:
    """Реестр помощников сессии: id → LedgerEntry.

    Старты и возобновления берутся и из транскриптов самих помощников: запуск
    помощника второго уровня лежит в транскрипте РОДИТЕЛЯ, в главной от него
    только уведомление. Читается весь файл — запуск трёхчасовой давности лежит
    глубже хвоста.
    """
    at = float("inf") if at is None else at
    starts, resumes, notes = {}, {}, {}
    sources = [main_path, *_helper_files(main_path)]
    for path in sources:
        for record in _lines(path, LEDGER_NEEDLES):
            for kind, agent_id, moment in _ledger_events(record):
                if moment > at:
                    continue
                bucket = {"start": starts, "resume": resumes}.get(kind)
                if bucket is not None:
                    bucket[agent_id] = max(moment, bucket.get(agent_id, 0.0))
                else:
                    notes.setdefault(agent_id, []).append(moment)
    ledger = {}
    for agent_id in set(starts) | set(resumes) | set(notes):
        entry = LedgerEntry(starts.get(agent_id), resumes.get(agent_id))
        after = [m for m in notes.get(agent_id, []) if entry.activated_at is None or m > entry.activated_at]
        entry.notified_at = min(after) if after else None
        ledger[agent_id] = entry
    return ledger


def last_answer(path, at: float | None = None):
    """→ (последний успешный ответ, последний отказ), оба datetime|None, не позже at."""
    at = float("inf") if at is None else at
    success = refusal = None
    for record in _lines(path, ('"assistant"',)):
        if record.get("type") != "assistant":
            continue
        moment = _epoch(record.get("timestamp"))
        if moment is None or moment > at:
            continue
        if record.get("isApiErrorMessage") or record.get("error"):
            refusal = moment if refusal is None else max(refusal, moment)
        else:
            success = moment if success is None else max(success, moment)
    to_dt = lambda m: None if m is None else datetime.datetime.fromtimestamp(m, datetime.timezone.utc)
    return to_dt(success), to_dt(refusal)


def _answers(path, at):
    success, refusal = last_answer(path, at)
    return (None if success is None else success.timestamp(),
            None if refusal is None else refusal.timestamp())


def _description(path) -> str:
    try:
        meta = json.loads(Path(str(path)[:-len(".jsonl")] + ".meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return " ".join(str(meta.get("description") or "").split())


def work_judgement(main_path, at: float, fresh_min: int) -> Judgement:
    """Работа сессии: главная ИЛИ живой помощник; at — момент суждения (epoch)."""
    ledger = helper_ledger(main_path, at)
    fresh_sec = fresh_min * 60
    live, unknown, ages = [], [], []
    for path in _helper_files(main_path):
        agent_id = path.stem[len("agent-"):]
        # Файл, не тронутый дольше порога, свежего ответа не несёт: читать его не нужно.
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue    # помощник исчез между перечислением и чтением
        if 0 <= at - mtime >= fresh_sec:
            continue
        answered, refused = _answers(path, at)
        if answered is None or at - answered >= fresh_sec:
            continue
        entry = ledger.get(agent_id)
        if entry is None:
            unknown.append(agent_id)
            continue
        if entry.activated_at is None or entry.notified_at is not None:
            continue
        if refused is not None and refused > answered:
            continue
        started = entry.started_at if entry.started_at is not None else entry.activated_at
        live.append(Helper(agent_id, _description(path), max(0, int((at - answered) // 60)),
                           max(0, int((at - started) // 60))))
        ages.append(max(0, at - answered))
    if unknown:
        raise LedgerRefusal(
            f"отказ: в реестре помощников нет {len(unknown)} живых транскриптов "
            f"(моложе {fresh_min} мин): {', '.join(unknown)} — формат записи запуска сменился?")
    main_answered, _ = _answers(main_path, at)
    main_min = None
    if main_answered is not None:
        ages.append(max(0, at - main_answered))
        main_min = int(ages[-1] // 60)
    return Judgement(int(min(ages)) if ages else None, main_min, live)


def idle_minutes(main_path, mark_epoch: float, at: float) -> int:
    """Простой от последней работы: между последним ответом до метки и первым после.

    Работа — успешный ответ главной ИЛИ любого помощника сессии. Ответа после
    метки нет — до at; до метки не было ответов вовсе — от самой метки.
    """
    moments = []
    for path in [main_path, *_helper_files(main_path)]:
        for record in _lines(path, ('"assistant"',)):
            if record.get("type") != "assistant" or record.get("isApiErrorMessage") or record.get("error"):
                continue
            moment = _epoch(record.get("timestamp"))
            if moment is not None and moment <= at:
                moments.append(moment)
    before = max((m for m in moments if m <= mark_epoch), default=mark_epoch)
    after = min((m for m in moments if m > mark_epoch), default=at)
    return max(0, int((after - before) // 60))


def answered_after(main_path, mark_epoch: float, at: float) -> bool:
    """Был ли успешный ответ главной или помощника НОВЕЕ метки."""
    for path in [main_path, *_helper_files(main_path)]:
        success, _ = _answers(path, at)
        if success is not None and success > mark_epoch:
            return True
    return False


def projects_dir() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECTS_DIR") or Path.home() / ".claude" / "projects")


def project_transcripts() -> Path | None:
    """Каталог транскриптов проекта: то же правило кодирования пути, что у сторожей."""
    sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
    import config
    project = config.конфиг().get("PROJECT_DIR") or ""
    for encoded in (project.replace("/", "-"), re.sub(r"[^a-zA-Z0-9]", "-", project)):
        if project and (projects_dir() / encoded).is_dir():
            return projects_dir() / encoded
    return None


def resolve_transcript(argument: str) -> str | None:
    """Путь к транскрипту по пути или по sid; без довода — свежайший транскрипт проекта."""
    if argument and os.path.isfile(argument):
        return argument
    folder = project_transcripts()
    if folder is None:
        return None
    if argument:
        candidate = folder / f"{argument}.jsonl"
        return str(candidate) if candidate.is_file() else None
    files = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(files[0]) if files else None


def _format_judgement(judgement: Judgement) -> str:
    rows = [("работа_мин", judgement.work_min), ("работа_сек", judgement.work_sec),
            ("главная_мин", "нет" if judgement.main_min is None else judgement.main_min),
            ("помощников", len(judgement.helpers))]
    lines = [f"{key}\t{value}" for key, value in rows]
    lines += [f"помощник\t{h.agent_id}\t{h.answer_min}\t{h.running_min}\t{h.description}"
              for h in judgement.helpers]
    return "\n".join(lines)


def _work_cli(argv) -> int:
    if not argv or argv[0].startswith("--"):
        print("нужен транскрипт или sid", file=sys.stderr)
        return 2
    at = time.time()
    rest = argv[1:]
    if rest:
        if rest[0] != "--на" or len(rest) != 2 or _epoch(rest[1]) is None:
            print("незнакомый довод: ожидалось --на <ISO UTC>", file=sys.stderr)
            return 2
        at = _epoch(rest[1])
    path = resolve_transcript(argv[0])
    if path is None:
        return 1
    try:
        judgement = work_judgement(path, at, fresh_minutes())
    except LedgerRefusal as refusal:
        print(refusal, file=sys.stderr)
        return 3
    if judgement.work_sec is None:
        return 1
    print(_format_judgement(judgement))
    return 0


def _selftest() -> int:
    import tempfile
    плохо = 0
    путей = 0

    def проба(зачем, вышло, ждём):
        nonlocal плохо, путей
        путей += 1
        if вышло == ждём:
            print(f"  ок    {зачем}")
        else:
            плохо += 1
            print(f"  ПЛОХО {зачем}: вышло {вышло!r}, ждали {ждём!r}")

    # Целые секунды: дробное «сейчас» округляется в метке до микросекунды, и
    # ровно 25 минут становятся 24,99… — пол даёт 24 примерно в каждом втором прогоне.
    сейчас = float(int(time.time()))

    def метка(минут_назад):
        момент = datetime.datetime.fromtimestamp(
            сейчас - минут_назад * 60, datetime.timezone.utc)
        return момент.isoformat().replace("+00:00", "Z")

    с_каталогом = tempfile.mkdtemp()
    журнал = os.path.join(с_каталогом, "transkript.jsonl")

    # БОЛЬНОЙ СЛУЧАЙ 26.09.2026: сессия стоит в лимите, но строки в журнал
    # ПИШУТСЯ — отказы и пинки. По mtime это выглядело работой «простой 0 мин».
    with open(журнал, "w", encoding="utf-8") as фх:
        фх.write(json.dumps({"type": "assistant", "timestamp": метка(25)}) + "\n")
        for сколько in (4, 3, 2, 1, 0):
            фх.write(json.dumps({"type": "assistant", "timestamp": метка(сколько),
                                 "isApiErrorMessage": True,
                                 "apiErrorStatus": 429}) + "\n")
    проба("БОЛЬНОЙ СЛУЧАЙ: свежие отказы 429 работой не считаются",
          немота_минут(журнал, сейчас=сейчас), 25)

    with open(журнал, "a", encoding="utf-8") as фх:
        фх.write(json.dumps({"type": "assistant", "timestamp": метка(1),
                             "error": "rate_limit"}) + "\n")
    проба("поле error без isApiErrorMessage — тоже не работа",
          немота_минут(журнал, сейчас=сейчас), 25)

    with open(журнал, "a", encoding="utf-8") as фх:
        фх.write(json.dumps({"type": "user", "timestamp": метка(0)}) + "\n")
    проба("запись пользователя работой агента не считается",
          немота_минут(журнал, сейчас=сейчас), 25)

    with open(журнал, "a", encoding="utf-8") as фх:
        фх.write(json.dumps({"type": "assistant", "timestamp": метка(2)}) + "\n")
    проба("успешный ответ немоту сбрасывает",
          немота_минут(журнал, сейчас=сейчас), 2)

    with open(журнал, "a", encoding="utf-8") as фх:
        фх.write("{битая строка\n")
    проба("битая строка соседей не рушит",
          немота_минут(журнал, сейчас=сейчас), 2)

    пустой = os.path.join(с_каталогом, "пусто.jsonl")
    open(пустой, "w", encoding="utf-8").close()
    проба("ответов нет — немоты не назвать", немота_минут(пустой), None)
    проба("файла нет — «не прочитали», а не ноль",
          немота_минут(os.path.join(с_каталогом, "нет-такого.jsonl")), None)

    кривое = os.path.join(с_каталогом, "кривая-метка.jsonl")
    with open(кривое, "w", encoding="utf-8") as фх:
        фх.write(json.dumps({"type": "assistant", "timestamp": "позавчера"}) + "\n")
    проба("метка не разбирается — не назвать, а не ноль",
          немота_минут(кривое), None)

    будущее = os.path.join(с_каталогом, "будущее.jsonl")
    with open(будущее, "w", encoding="utf-8") as фх:
        фх.write(json.dumps({"type": "assistant", "timestamp": метка(-10)}) + "\n")
    проба("метка из будущего не даёт отрицательной немоты",
          немота_минут(будущее, сейчас=сейчас), 0)

    if плохо:
        print(f"САМОТЕСТ ПРОВАЛЕН: путей {путей}, неудач {плохо}")
        return 1
    print(f"САМОТЕСТ ПРОЙДЕН: {путей} путей, больной случай первым")
    return 0


def main(argv) -> int:
    if "--selftest" in argv:
        return _selftest()
    if argv and argv[0] == "--работа":
        return _work_cli(argv[1:])
    if not argv:
        print("нужен путь к транскрипту", file=sys.stderr)
        return 2
    минут = немота_минут(argv[0])
    if минут is None:
        return 1
    print(минут)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
