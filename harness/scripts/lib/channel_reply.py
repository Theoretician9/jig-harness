"""Единственное правило «сообщение владельца отвечено / не отвечено».

Его читают Stop-хук channel_reply_guard.py и метрика delivery-latency.py
--без-ответа: два суждения об одном разошлись бы молча.

Сообщение владельца — файл $LOG_DIR/inbox/<эпоха>.<нс>.txt либо тот же файл в
inbox/обработано/. Пишет их только диспетчер и только для чата владельца; имя —
момент приёма, а у голоса файл с расшифровкой ложится позже имени, поэтому время
сообщения — max(имя, mtime): отправка до появления расшифровки на голос не ответ.

Ответ — строка tg_out.jsonl с iz_sessii=true (пишется после доставки) либо метка
last_report (ответ файлом в tg_out не попадает). Отказ доставки, весть демона
и короткая квитанция-обещание («Принял: проверяю… ответ следом») ответом не
считаются: обещание ответа владельцу ничего не даёт. Признаки и предел длины —
только harness/config/channel-reply.yaml; нет файла или он битый — правило
выключено. Ответ раньше сообщения — не ответ.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

MESSAGE_NAME = re.compile(r"^(\d+\.\d+)\.txt$")
DOOR_FILE = "reply-not-needed"
MAX_AGE_DEFAULT_MIN = 120


def reply_horizon_sec(settings):
    """Как долго безответное сообщение ещё считается живым; одно чтение для хука и прибора."""
    return int(settings.get("CHANNEL_REPLY_MAX_AGE_MIN") or MAX_AGE_DEFAULT_MIN) * 60

# Служебный ход начат не владельцем: уведомление помощника, стартовый промпт
# после перезапуска, пинок сторожа. «Владелец написал: …» сюда НЕ входит, даже
# с пометкой «пинок»: это его сообщение. Префиксы взяты из настоящих стенограмм.
SERVICE_PREFIXES = ("<task-notification>", "[сторож", "Ты — новая сессия после перезапуска",
                    "Работы не видно", "Контекст очищен сторожем")
TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024

def markers_path():
    import config
    return os.environ.get("CHANNEL_REPLY_MARKERS") or str(
        config.корень_дерева(__file__) / "harness" / "config" / "channel-reply.yaml")


def promise_rules():
    """(регулярки, предел длины) или None, если правило обещаний выключено.

    Единственный источник — yaml; любая неполадка в нём выключает правило:
    тогда любой исходящий смены считается ответом.
    """
    try:
        import yaml
        with open(markers_path(), encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        raw, max_chars = data["promise_markers"], data["promise_max_chars"]
        if (not isinstance(raw, list) or not raw
                or not all(isinstance(m, str) for m in raw)
                or not isinstance(max_chars, int) or isinstance(max_chars, bool)
                or max_chars <= 0):
            return None
        return [re.compile(m, re.IGNORECASE | re.DOTALL) for m in raw], max_chars
    except Exception:
        return None


def is_promise(text, rules):
    if rules is None:
        return False
    patterns, max_chars = rules
    return len(text) <= max_chars and any(p.search(text) for p in patterns)


def message_dirs(log_dir):
    inbox = Path(log_dir) / "inbox"
    return [inbox, inbox / "обработано"]


def owner_messages(log_dir):
    """[(время, имя файла)] по возрастанию времени; нет каталога — пусто."""
    found = {}
    for directory in message_dirs(log_dir):
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            match = MESSAGE_NAME.match(entry.name)
            if not match or not entry.is_file():
                continue
            moment = max(float(match.group(1)), entry.stat().st_mtime)
            # Файл могли перенести в обработано/ — имя одно, берём позднейшее время.
            found[entry.name] = max(found.get(entry.name, 0.0), moment)
    return sorted((moment, name) for name, moment in found.items())


def _line_epoch(text):
    return datetime.fromisoformat(text).timestamp()


def reply_times(log_dir):
    """Отсортированные моменты доставленных ответов смены (без квитанций-обещаний)."""
    moments = []
    rules = promise_rules()
    # Журнала нет — судить нечем: OSError уходит наверх, хук молчит (fail-open),
    # иначе «нет журнала» читалось бы как «ответов нет».
    with open(Path(log_dir) / "tg_out.jsonl", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
                if row.get("iz_sessii") is True and not is_promise(str(row.get("text") or ""), rules):
                    moments.append(_line_epoch(row["ts"]))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
    try:
        moments.append(float((Path(log_dir) / "last_report").read_text().strip()))
    except (OSError, ValueError):
        pass
    return sorted(moments)


def is_answered(moment, replies, within=None):
    """Есть ответ строго позже сообщения (и не позже within секунд, если задано)."""
    return any(r > moment and (within is None or r <= moment + within) for r in replies)


def closed_by_door(log_dir, moment, now, max_age=None):
    """Причина из метки «ответ не требуется» или None.

    Метка должна быть новее сообщения и не позже «сейчас» (иначе при повторе на
    прошлую минуту она закрыла бы всё). Срок max_age задаёт хук; страж срока не
    задаёт: дверь закрывает сообщение навсегда.
    """
    door = Path(log_dir) / DOOR_FILE
    try:
        reason = door.read_text(encoding="utf-8").strip()
        stamp = door.stat().st_mtime
    except OSError:
        return None
    return reason[:200] if reason and moment < stamp <= now and (max_age is None or now - stamp <= max_age) else None


def overdue_among(log_dir, messages, replies, within, now, horizon):
    """overdue над уже прочитанными сообщениями и ответами: сетка зовёт её на каждую минуту."""
    return [(moment, name) for moment, name in messages
            if within <= now - moment <= horizon
            and not is_answered(moment, replies, within=now - moment)
            and not closed_by_door(log_dir, moment, now)]


def overdue(log_dir, within, now, horizon):
    """[(время, имя)] сообщений, ждущих ответа не меньше within секунд, но не дольше horizon.

    «Сейчас» отрезается только через within и stamp <= now: ответ позже now
    сообщения не закрывает.
    """
    return overdue_among(log_dir, owner_messages(log_dir), reply_times(log_dir), within, now, horizon)


def unanswered_count(log_dir, lo, hi, within, now):
    """(без ответа, всего) за окно [lo, hi].

    Сообщение, чей срок ответа ещё не вышел, в счёт не входит: оно не просрочено.
    """
    replies = reply_times(log_dir)
    total = bad = 0
    for moment, _name in owner_messages(log_dir):
        if not lo <= moment <= hi or moment + within > now:
            continue
        total += 1
        if not is_answered(moment, replies, within):
            bad += 1
    return bad, total


def last_unanswered(log_dir):
    """(время, имя) последнего сообщения владельца без ответа, иначе None."""
    messages = owner_messages(log_dir)
    if not messages:
        return None
    moment, name = messages[-1]
    return None if is_answered(moment, reply_times(log_dir)) else (moment, name)


def _prompt_text(record):
    """Текст настоящего запроса или None: служебные записи и tool_result не начинают ход."""
    if record.get("type") != "user" or record.get("isMeta") or record.get("isCompactSummary"):
        return None
    content = record["message"]["content"]
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    return content.strip() or None


def turn_started_by_service(transcript_path):
    """True, если последний настоящий запрос в стенограмме — служебный.

    Нет стенограммы или запроса — False: не зная, кто начал ход, хук считает
    его владельческим и блокирует, как раньше.
    """
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - TRANSCRIPT_TAIL_BYTES))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError):
        return False
    for line in reversed(lines):
        try:
            text = _prompt_text(json.loads(line))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if text:
            return text.startswith(SERVICE_PREFIXES)
    return False
