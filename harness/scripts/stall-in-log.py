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
    python3 scripts/stall-in-log.py --работа --shift [--на <ISO UTC>]
    python3 scripts/stall-in-log.py --selftest
Код возврата: 0 — ответ найден (минуты на stdout), 1 — ответов нет или файл
нечитаем. «Не прочитали» и «ответов нет» здесь одно: в обоих случаях сказать о
немоте нечего, а положительного утверждения прибор не делает.
У `--работа` код 1 — только «транскрипта нет»: ответов у главной может не быть,
а помощники при этом идти. Код 3: реестр помощников не узнал живой транскрипт —
судить о помощниках нечем, и «помощников 0» не печатается (молчать о настоящей
работе хуже, чем отказаться).

«Помощник идёт» — это запуск без результата в транскрипте родителя, а не
свежий ответ: помощник, ждущий длинный инструмент (лог ворот, прогон тестов),
молчит десятки минут, а ротация или перезапуск оборвали бы его так же, как
пишущего. Свежесть ответов остаётся меркой пульса «работа идёт» (`работа_мин`)
и потолком «завис» (HELPER_HUNG_MIN).
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
LEDGER_NEEDLES = ("async_launched", "SendMessage", '"is_error":true', '"is_error": true', 'success', "<task-id>a")
HELPER_FRESH_DEFAULT_MIN = 20
# Потолок «завис»: самый долгий живой вызов в замере ревью — 49 мин, 120 — запас
# в 2,4 раза; ниже этого молчащий помощник ещё считается идущим.
HELPER_HUNG_DEFAULT_MIN = 120
# Запуск помощника не фоном: tool_use в транскрипте родителя, ответ — tool_result
# с тем же id. Отбор строк регулярками, без разбора JSON: tool_result в
# транскрипте на порядки больше запусков.
LAUNCH_CALL = re.compile(r'"name":\s*"(?:Agent|Task)"')
RESULT_ID = re.compile(r'"tool_use_id":\s*"(toolu_[A-Za-z0-9_]+)"')
RECORD_STAMP = re.compile(r'"timestamp":\s*"([^"]+)"')
LAUNCH_TOOLS = ("Agent", "Task")


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
    answer_min: int      # с последнего успешного ответа; ответов не было — с начала запуска
    running_min: int
    silent_min: int      # транскрипт помощника не пишется (mtime), для строки зависшего


@dataclass
class Judgement:
    work_sec: int | None
    main_min: int | None
    helpers: list = field(default_factory=list)
    hung: list = field(default_factory=list)

    @property
    def work_min(self) -> int | None:
        return None if self.work_sec is None else self.work_sec // 60


def _epoch(stamp) -> float | None:
    try:
        return datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _minutes_key(key: str, default: int) -> int:
    """Порог в минутах: окружение старше файла, число из харнес-конфига."""
    raw = os.environ.get(key)
    if not raw:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
        try:
            import config
            raw = config.из_файла(config.путь_harness_conf()).get(key)
        except ImportError:
            raw = None
    return int(raw) if raw and raw.isdigit() else default


def fresh_minutes() -> int:
    return _minutes_key("HELPER_FRESH_MIN", HELPER_FRESH_DEFAULT_MIN)


def hung_minutes() -> int:
    return _minutes_key("HELPER_HUNG_MIN", HELPER_HUNG_DEFAULT_MIN)


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


def _is_refusal(block) -> bool:
    """Отказ вызова: is_error, либо ответ SendMessage — JSON с "success": false.

    Живой отказ 07.11 пришёл БЕЗ is_error, текстом в списке блоков text.
    """
    if block.get("is_error") is True:
        return True
    content = block.get("content")
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    if not isinstance(content, str) or '"success"' not in content:
        return False
    try:
        answer = json.loads(content)
    except ValueError:
        return False
    return isinstance(answer, dict) and answer.get("success") is False


def _ledger_events(record):
    """→ [(вид, id помощника, момент, id вызова)]: start | resume | notify | refused."""
    moment = _epoch(record.get("timestamp"))
    if moment is None:
        return []
    events = []
    result = record.get("toolUseResult")
    if isinstance(result, dict) and result.get("status") == "async_launched":
        agent_id = str(result.get("agentId") or "")
        if AGENT_ID.fullmatch(agent_id):
            events.append(("start", agent_id, moment, ""))
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if record.get("type") == "assistant" and block.get("type") == "tool_use" and block.get("name") == "SendMessage":
            target = str((block.get("input") or {}).get("to") or "")
            if AGENT_ID.fullmatch(target):
                events.append(("resume", target, moment, str(block.get("id"))))
        elif block.get("type") == "tool_result" and _is_refusal(block):
            events.append(("refused", "", moment, str(block.get("tool_use_id"))))
    for agent_id in NOTIFICATION.findall(_notification_text(record)):
        events.append(("notify", agent_id, moment, ""))
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
    sends, refused_calls = [], set()
    sources = [main_path, *_helper_files(main_path)]
    for path in sources:
        for record in _lines(path, LEDGER_NEEDLES):
            for kind, agent_id, moment, call_id in _ledger_events(record):
                if moment > at:
                    continue
                if kind == "start":
                    starts[agent_id] = max(moment, starts.get(agent_id, 0.0))
                elif kind == "resume":
                    sends.append((call_id, agent_id, moment))
                elif kind == "refused":
                    refused_calls.add(call_id)
                else:
                    notes.setdefault(agent_id, []).append(moment)
    # Возврат, который хук отклонил, помощника не поднял: без этого он вечно
    # «идёт» — уведомления о конце после него не будет.
    for call_id, agent_id, moment in sends:
        if call_id not in refused_calls:
            resumes[agent_id] = max(moment, resumes.get(agent_id, 0.0))
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


def _meta(path) -> dict:
    try:
        meta = json.loads(Path(str(path)[:-len(".jsonl")] + ".meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _description(path) -> str:
    return " ".join(str(_meta(path).get("description") or "").split())


def _launch_calls(sources, at: float):
    """→ ({id вызова: (момент, описание)}, {id вызовов, у которых есть результат}).

    Запуск помощника не фоном виден только так: у вызова Agent/Task нет записи
    tool_result. Фоновый запуск результат получает сразу («launched»), поэтому
    сюда он попадает закрытым — его ведёт реестр.
    """
    calls, answered = {}, set()
    for path in sources:
        try:
            stream = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with stream:
            for line in stream:
                if '"tool_result"' in line:
                    stamp = RECORD_STAMP.search(line)
                    moment = _epoch(stamp.group(1)) if stamp else None
                    if moment is None or moment <= at:
                        answered.update(RESULT_ID.findall(line))
                elif '"tool_use"' in line and LAUNCH_CALL.search(line):
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    moment = _epoch(record.get("timestamp"))
                    message = record.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if moment is None or moment > at or not isinstance(content, list):
                        continue
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use" \
                                and block.get("name") in LAUNCH_TOOLS:
                            description = (block.get("input") or {}).get("description") or ""
                            calls[str(block.get("id"))] = (moment, " ".join(str(description).split()))
    return calls, answered


def _mtime(path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None   # помощник исчез между перечислением и чтением


def _minutes_since(moment: float, at: float) -> int:
    return max(0, int((at - moment) // 60))


def _make_helper(agent_id, description, answered, started, silent_since, at) -> Helper:
    """Строка помощника; answered=None — ответов не было, ответ мерится с запуска."""
    return Helper(agent_id, description, _minutes_since(answered if answered is not None else started, at),
                  _minutes_since(started, at), _minutes_since(silent_since, at))


def _is_fresh_work(answered, refused, at: float, fresh_sec: int) -> bool:
    """Мерка пульса: недавний успешный ответ, за которым не стоит отказ."""
    if answered is None or at - answered >= fresh_sec:
        return False
    return refused is None or refused <= answered


def work_judgement(main_path, at: float, fresh_min: int, hung_min: int = HELPER_HUNG_DEFAULT_MIN) -> Judgement:
    """Работа сессии: главная ИЛИ живой помощник; at — момент суждения (epoch).

    helpers — идущие (запуск без результата), hung — идущие, но молчащие дольше
    hung_min; обе группы удерживают ротацию, а в `работа_*` входят только свежие
    ответы.
    """
    files = {path.stem[len("agent-"):]: path for path in _helper_files(main_path)}
    ledger = helper_ledger(main_path, at)
    calls, answered_calls = _launch_calls([main_path, *files.values()], at)
    open_calls = {call: info for call, info in calls.items() if call not in answered_calls}
    fresh_sec, hung_sec = fresh_min * 60, hung_min * 60
    live, hung, ages = [], [], []

    def place(helper: Helper, answered, refused, silent_sec):
        (hung if silent_sec >= hung_sec else live).append(helper)
        if _is_fresh_work(answered, refused, at, fresh_sec):
            ages.append(max(0, at - answered))

    for agent_id, entry in sorted(ledger.items()):
        if entry.activated_at is None or entry.notified_at is not None:
            continue
        path = files.get(agent_id)
        answered, refused = _answers(path, at) if path else (None, None)
        started = entry.started_at if entry.started_at is not None else entry.activated_at
        written = _mtime(path) if path else None
        silent_since = max(started, min(written, at)) if written is not None else started
        helper = _make_helper(agent_id, _description(path) if path else "", answered, started, silent_since, at)
        place(helper, answered, refused, at - silent_since)

    # Файлы вне реестра: помощник не фоном (его вызов открыт) либо реестр не узнал живого.
    registered = set(ledger)
    outside = {agent_id: path for agent_id, path in files.items() if agent_id not in registered}
    claimed = {agent_id for agent_id, path in outside.items() if _meta(path).get("toolUseId") in calls}
    stray = [agent_id for agent_id in outside if agent_id not in claimed]
    for call, (moment, description) in sorted(open_calls.items(), key=lambda item: item[1][0]):
        mine = [outside[a] for a in outside if _meta(outside[a]).get("toolUseId") == call]
        # Без метки вызова в .meta.json помощник не фоном — любой посторонний файл,
        # тронутый после запуска: старее запуска он принадлежать этому вызову не может.
        mine = mine or [outside[a] for a in stray if (_mtime(outside[a]) or 0) >= moment]
        answers = [_answers(path, at) for path in mine]
        answered = max((a for a, _ in answers if a is not None), default=None)
        refused = max((r for _, r in answers if r is not None), default=None)
        written = max((m for m in map(_mtime, mine) if m is not None), default=None)
        silent_since = max(moment, min(written, at)) if written is not None else moment
        place(_make_helper(call, description, answered, moment, silent_since, at),
              answered, refused, at - silent_since)

    if not open_calls:
        unknown = []
        for agent_id in stray:
            written = _mtime(outside[agent_id])
            if written is None or 0 <= at - written >= fresh_sec:
                continue    # файл, не тронутый дольше порога, свежего ответа не несёт
            answered, _ = _answers(outside[agent_id], at)
            if answered is not None and at - answered < fresh_sec:
                unknown.append(agent_id)
        if unknown:
            raise LedgerRefusal(
                f"отказ: в реестре помощников нет {len(unknown)} живых транскриптов "
                f"(моложе {fresh_min} мин): {', '.join(unknown)} — формат записи запуска сменился?")
    main_answered, _ = _answers(main_path, at)
    main_min = None
    if main_answered is not None:
        ages.append(max(0, at - main_answered))
        main_min = int(ages[-1] // 60)
    return Judgement(int(min(ages)) if ages else None, main_min, live, hung)


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


def _entrypoint(path) -> str | None:
    """Откуда запущена сессия (cli — смена, sdk-cli — headless-прогон); из первых записей."""
    for number, record in enumerate(_lines(path)):
        if record.get("entrypoint"):
            return str(record["entrypoint"])
        if number >= 50:
            break
    return None


def shift_transcript(folder: Path) -> str | None:
    """Свежайший верхний транскрипт СМЕНЫ: headless-прогоны (sdk-cli) пропускаются.

    Они пишут рядом и бывают свежее смены (16 из 40 последних), а помощники
    живут в транскрипте смены.
    """
    for path in sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        if _entrypoint(path) == "cli":
            return str(path)
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
    # Пустое значение, а не «нет»: у ответов главной может не быть, а потребители
    # (limit-watch, сторож) числовые поля сверяют регуляркой и пустое отбрасывают.
    blank = lambda value: "" if value is None else value
    rows = [("работа_мин", blank(judgement.work_min)), ("работа_сек", blank(judgement.work_sec)),
            ("главная_мин", "нет" if judgement.main_min is None else judgement.main_min),
            ("помощников", len(judgement.helpers)), ("зависших", len(judgement.hung))]
    lines = [f"{key}\t{value}" for key, value in rows]
    lines += [f"помощник\t{h.agent_id}\t{h.answer_min}\t{h.running_min}\t{h.description}"
              for h in judgement.helpers]
    lines += [f"зависший\t{h.agent_id}\t{h.silent_min}\t{h.running_min}\t{h.description}"
              for h in judgement.hung]
    return "\n".join(lines)


def _work_cli(argv) -> int:
    if not argv or (argv[0].startswith("--") and argv[0] != "--shift"):
        print("нужен транскрипт, sid или --shift", file=sys.stderr)
        return 2
    at = time.time()
    rest = argv[1:]
    if rest:
        if rest[0] != "--на" or len(rest) != 2 or _epoch(rest[1]) is None:
            print("незнакомый довод: ожидалось --на <ISO UTC>", file=sys.stderr)
            return 2
        at = _epoch(rest[1])
    if argv[0] == "--shift":
        folder = project_transcripts()
        path = shift_transcript(folder) if folder is not None else None
    else:
        path = resolve_transcript(argv[0])
    if path is None:
        return 1
    try:
        judgement = work_judgement(path, at, fresh_minutes(), hung_minutes())
    except LedgerRefusal as refusal:
        print(refusal, file=sys.stderr)
        return 3
    print(_format_judgement(judgement))
    # Какой транскрипт судили: `--shift` выбирает его сам, а сторожу нужен id смены.
    print(f"транскрипт\t{path}")
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

    # ── помощники: «идёт» = запуск без результата, а не свежий ответ ─────────
    helper_id = "a" + "1" * 16

    def запись(вид, минут_назад, **extra):
        return {"type": вид, "timestamp": метка(минут_назад), "entrypoint": "cli", **extra}

    def сцена(имя, главная, помощник=None, помощник_молчит_мин=0, meta=None):
        папка = os.path.join(с_каталогом, имя)
        os.makedirs(os.path.join(папка, "s", "subagents"))
        путь = os.path.join(папка, "s.jsonl")
        with open(путь, "w", encoding="utf-8") as фх:
            фх.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in главная)
        if помощник is not None:
            файл = os.path.join(папка, "s", "subagents", f"agent-{helper_id}.jsonl")
            with open(файл, "w", encoding="utf-8") as фх:
                фх.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in помощник)
            os.utime(файл, (сейчас - помощник_молчит_мин * 60,) * 2)
            if meta:
                with open(файл[:-len(".jsonl")] + ".meta.json", "w", encoding="utf-8") as фх:
                    json.dump(meta, фх, ensure_ascii=False)
        return путь

    def запуск_фоном(минут_назад):
        return [запись("assistant", минут_назад, message={"content": [
                    {"type": "tool_use", "id": "toolu_bg", "name": "Agent",
                     "input": {"description": "фон", "run_in_background": True}}]}),
                запись("user", минут_назад, message={"content": [
                    {"type": "tool_result", "tool_use_id": "toolu_bg", "content": "запущен"}]},
                       toolUseResult={"status": "async_launched", "agentId": helper_id})]

    def вызов_не_фоном(минут_назад):
        return запись("assistant", минут_назад, message={"content": [
            {"type": "tool_use", "id": "toolu_fg", "name": "Agent",
             "input": {"description": "не-фон", "prompt": "работа"}}]})

    ответ_главной = запись("assistant", 1, message={"content": [{"type": "text", "text": "жду"}]})
    ответ_помощника = lambda минут: запись("assistant", минут, agentId=helper_id, isSidechain=True,
                                           message={"content": [{"type": "text", "text": "шаг"}]})

    def суд(путь):
        return work_judgement(путь, сейчас, 20, 120)

    j = суд(сцена("тихий", запуск_фоном(60) + [ответ_главной], [ответ_помощника(25)], 25))
    проба("БОЛЬНОЙ СЛУЧАЙ 02.11: помощник молчит 25 мин, уведомления нет — идёт",
          (len(j.helpers), len(j.hung)), (1, 0))
    j = суд(сцена("законченный", запуск_фоном(60) + [ответ_главной, запись("user", 5, message={"content":
          "<task-notification>\n<task-id>" + helper_id + "</task-id>\n</task-notification>"})],
          [ответ_помощника(6)], 6))
    проба("уведомление о конце — не идёт", (len(j.helpers), len(j.hung)), (0, 0))
    уведомление = lambda минут: запись("user", минут, message={"content":
        "<task-notification>\n<task-id>" + helper_id + "</task-id>\n</task-notification>"})
    счётчик_вызовов = iter(range(100))

    # Форма снята с живого tool_result 07.11 (транскрипт 35b41ed1…): без is_error,
    # content — список блоков text, в тексте JSON.
    отказ_без_ошибки = [{"type": "text", "text": json.dumps({"success": False, "message":
        'Agent "' + helper_id + '" could not be resumed: No transcript found for agent ID: ' + helper_id + "."},
        separators=(",", ":"))}]
    успех_json = [{"type": "text", "text": json.dumps({"success": True, "message": "Message queued"})}]

    def возврат(минут, отклонён=False, content="итог"):
        вызов = f"toolu_sm{next(счётчик_вызовов)}"
        итог = {"type": "tool_result", "tool_use_id": вызов, "content": content}
        if отклонён:
            итог["is_error"] = True
        return [запись("assistant", минут, message={"content": [
                    {"type": "tool_use", "id": вызов, "name": "SendMessage", "input": {"to": helper_id}}]}),
                запись("user", минут, message={"content": [итог]})]
    j = суд(сцена("возвращён-и-закончил", запуск_фоном(90) + [уведомление(80), *возврат(30),
          уведомление(20), ответ_главной], [ответ_помощника(21)], 21))
    проба("возвращён и закончил (уведомление после возврата) — не идёт и не завис",
          (len(j.helpers), len(j.hung)), (0, 0))
    j = суд(сцена("возвращён-идёт", запуск_фоном(90) + [уведомление(80), *возврат(30), ответ_главной],
          [ответ_помощника(2)], 2))
    проба("возвращён и ещё не закончил — идёт", (len(j.helpers), len(j.hung)), (1, 0))
    j = суд(сцена("возвращён-закончил-возвращён", запуск_фоном(90) + [уведомление(80), *возврат(60),
          уведомление(50), *возврат(10), ответ_главной], [ответ_помощника(2)], 2))
    проба("возвращён, закончил, возвращён снова — идёт", (len(j.helpers), len(j.hung)), (1, 0))
    j = суд(сцена("возврат-отклонён", запуск_фоном(90) + [уведомление(80), *возврат(10, отклонён=True), ответ_главной],
          [ответ_помощника(81)], 81))
    проба("БОЛЬНОЙ СЛУЧАЙ 04.11: возврат отклонён хуком — помощник не поднят, не идёт",
          (len(j.helpers), len(j.hung)), (0, 0))
    j = суд(сцена("отказ-без-is_error", запуск_фоном(90) + [уведомление(80), *возврат(10, content=отказ_без_ошибки),
          ответ_главной], [ответ_помощника(81)], 81))
    проба("БОЛЬНОЙ СЛУЧАЙ 07.11: отказ SendMessage текстом success:false без is_error — помощник не идёт",
          (len(j.helpers), len(j.hung)), (0, 0))
    j = суд(сцена("успех-json", запуск_фоном(90) + [уведомление(80), *возврат(10, content=успех_json),
          ответ_главной], [ответ_помощника(2)], 2))
    проба("успешный SendMessage (success:true) — помощник идёт", (len(j.helpers), len(j.hung)), (1, 0))
    j = суд(сцена("завис", запуск_фоном(300) + [ответ_главной], [ответ_помощника(130)], 130))
    проба("молчит 130 мин (> 120) — завис, а не идёт", (len(j.helpers), len(j.hung)), (0, 1))
    j = суд(сцена("не-фоном", [вызов_не_фоном(30), ответ_главной], [ответ_помощника(1)], 1))
    проба("вызов не фоном без результата — идёт, строка несёт id вызова и описание",
          [(h.agent_id, h.description) for h in j.helpers], [("toolu_fg", "не-фон")])
    j = суд(сцена("не-фоном-готов", [вызов_не_фоном(30), запись("user", 20, message={"content": [
          {"type": "tool_result", "tool_use_id": "toolu_fg", "content": "готово"}]}), ответ_главной]))
    проба("вызов не фоном получил результат — не идёт", (len(j.helpers), len(j.hung)), (0, 0))
    проба("помощников нет и ответов главной нет — суждение есть, работы нет",
          суд(сцена("пусто", [])).work_sec, None)

    headless = os.path.join(с_каталогом, "проект")
    os.makedirs(headless)
    for имя, вход, назад in (("смена", "cli", 5), ("прогон", "sdk-cli", 1)):
        файл = os.path.join(headless, имя + ".jsonl")
        with open(файл, "w", encoding="utf-8") as фх:
            фх.write(json.dumps({"type": "assistant", "entrypoint": вход, "timestamp": метка(назад)}) + "\n")
        os.utime(файл, (сейчас - назад * 60,) * 2)
    проба("--shift пропускает headless-прогон и берёт свежайшую смену",
          os.path.basename(shift_transcript(Path(headless)) or ""), "смена.jsonl")

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
