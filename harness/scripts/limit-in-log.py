#!/usr/bin/env python3
"""Лимит подписки в журнале сессии: машинный факт, а не догадка по экрану.

CLI пишет в транскрипт запись с `"error": "rate_limit"` и `"apiErrorStatus": 429`
в момент отказа. Это ФАКТ: слово «лимит» я и сам печатаю в отчётах, а экранный
признак угадывает чужой текст.

Один источник для двух сторожей. До 13.09.2026 разбор жил heredoc-ом внутри
`session-warden.sh`, и второму сторожу (ежеминутному `limit-watch.sh`) пришлось
бы его скопировать — а две копии одного признака расходятся молча, и
разошедшаяся половина молчит ровно тогда, когда нужна.

Прогон:
    python3 scripts/limit-in-log.py <транскрипт>   # минуты с последнего 429
    python3 scripts/limit-in-log.py --сброс-числом <транскрипт>  # unix-время обновления
    python3 scripts/limit-in-log.py --selftest
Код возврата: 0 — отказ найден (минуты на stdout), 1 — записей нет.
"""
import datetime
import json
import re
import os
import sys
import time
from pathlib import Path

# Хвост, а не весь файл: транскрипт растёт до сотен мегабайт, а нас интересует
# последний отказ. То же число, что у остальных веток сторожа.
ХВОСТ_БАЙТ = int(os.environ.get("STALL_TAIL_BYTES", 4_000_000))


def минут_с_отказа(путь: str, хвост: int = ХВОСТ_БАЙТ, сейчас: float | None = None):
    """→ минуты с последнего отказа 429, либо None — отказов нет."""
    сейчас = сейчас if сейчас is not None else time.time()
    try:
        with open(путь, "rb") as файл:
            файл.seek(0, os.SEEK_END)
            начало = max(0, файл.tell() - хвост)
            файл.seek(начало)
            куски = файл.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    последняя = None
    for строка in куски.splitlines():
        # Дешёвый отсев до разбора JSON: строк в транскрипте десятки тысяч.
        if "rate_limit" not in строка and "429" not in строка:
            continue
        try:
            запись = json.loads(строка)
        except ValueError:
            continue
        if запись.get("error") == "rate_limit" or запись.get("apiErrorStatus") == 429:
            метка = запись.get("timestamp")
            if метка:
                последняя = метка
    if not последняя:
        return None
    try:
        момент = datetime.datetime.fromisoformat(str(последняя).replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((сейчас - момент.timestamp()) // 60)


# Тот же факт, второй носитель: headless-прогон демона в журнал сессии не
# пишет — он получает отказ ТЕКСТОМ на экран («You've hit your weekly limit ·
# resets Sep 16, 6pm»). Судья обязан быть один: копия разбора внутри обёртки
# разошлась бы с этим прибором молча (случай 13.09.2026 — демоны принимали
# лимит за обычную неудачу и молчали двое суток).
СЛОВА_ЛИМИТА = re.compile(r"hit your .*limit|rate limit|429|usage limit",
                          re.IGNORECASE)
# Срок называют две фразы CLI: «resets 5:30pm» в отказе и «continuing
# automatically at 5:30pm» в заметке о лимите. Хвост «(error type…» — не срок:
# в срок входят только час и пояс «(Asia/Qostanay)», а скобка с пробелом внутри
# его обрывает. Перевода строки нет в классе отрицания — тот же капкан, что «\s
# включает \n»: совпадение тянулось через десятки строк и сообщение владельцу
# выходило на 15 КБ (находка CRITICAL-2 ревью 27.09.2026).
_СБРОС = re.compile(r"(?:resets(?:\s+at)?|continuing automatically at)\s+"
                    r"(?P<срок>[^·|\n\r(]+(?:\([\w/+\-]+\))?)", re.IGNORECASE)
# Сколько знаков строки сброса имеет смысл: владельцу нужен час, не простыня.
ПОТОЛОК_СТРОКИ = 200


# Часы вида «6pm», «3:10 PM» — в 24-часовые. Владелец 17.10.2026: «время
# должно быть в 24 часовом формате, написано все должно быть человеческим
# языком»; CLI печатает свой отказ по-американски («resets 3:10pm»), и этот
# текст уходил владельцу как есть. Перевод живёт ЗДЕСЬ, у судьи лимита: второй
# перевод того же часа в другом файле разъехался бы молча.
_ЧАС_AMPM = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", re.IGNORECASE)


def в_24_часа(текст: str) -> str:
    """«resets 3:10pm (Asia/Qostanay)» → «resets 15:10 (Asia/Qostanay)»."""
    def заменить(совпадение: "re.Match[str]") -> str:
        часы = int(совпадение.group(1)) % 12
        if совпадение.group(3).lower() == "p":
            часы += 12
        return f"{часы:02d}:{совпадение.group(2) or '00'}"
    return _ЧАС_AMPM.sub(заменить, текст)


def лимит_в_тексте(текст: str) -> str | None:
    """Отказ по лимиту в выводе CLI → строка сброса (или «срок не назван»).

    None означает «это не лимит»: обычная неудача модели обязана остаться
    обычной неудачей, иначе демон начнёт пропускать работу без причины.
    """
    if not СЛОВА_ЛИМИТА.search(текст):
        return None
    найдено = _СБРОС.search(текст)
    # Времени нет — «resets None», пустая строка: часа, которого CLI не назвал,
    # не выдумываем. Пояс в скобках — часть срока: обрезка по «)» давала
    # «6pm (Asia/Qostanay», а владелец читает именно час.
    if not найдено or not re.search(r"\d", найдено.group("срок")):
        return "срок сброса CLI не назвал"
    # «resets 15:10» → «обновится в 15:10»: строка идёт ВЛАДЕЛЬЦУ, а он читает
    # час, а не английское слово (его правило 17.10.2026 — человеческим языком).
    return "обновится в " + в_24_часа(найдено.group("срок").strip()[:ПОТОЛОК_СТРОКИ])


def сброс_из_журнала(путь: str, хвост: int = ХВОСТ_БАЙТ,
                    сейчас: float | None = None) -> tuple[int, str] | None:
    """Когда обновится лимит по ПОСЛЕДНЕЙ записи отказа: (минут, «в HH:MM»).

    `None` — записей отказа в хвосте журнала нет.

    Время берётся ЧИСЛОМ из поля `resetsAt`, которое печатает сам CLI, а не
    разбором человеческой строки «resets 9pm»: разбор текста на журнале сессии
    ловил «429» внутри обычного JSON и цитату исходника сторожа, то есть
    объявлял лимит почти всегда, а захваченный кусок тянулся на 15 КБ и ломал
    сообщение владельцу целиком (CRITICAL-1 и CRITICAL-2 ревью 27.09.2026).
    Берётся ПОСЛЕДНЯЯ запись: первая в файле может быть недельной давности.
    """
    сейчас = сейчас if сейчас is not None else time.time()
    последний = сброс_числом(путь, хвост)
    if not последний:
        return None
    осталось = int((последний - сейчас) // 60)
    часы, минуты = divmod(max(0, осталось), 60)
    сколько = f"{часы} ч {минуты} мин" if часы else f"{минуты} мин"
    когда = datetime.datetime.fromtimestamp(последний).strftime("%H:%M")
    return осталось, f"в {когда}, через {сколько}"


def сброс_числом(путь: str, хвост: int = ХВОСТ_БАЙТ) -> int | None:
    """Unix-время обновления лимита по ПОСЛЕДНЕЙ записи отказа; None — записей нет.

    Числом его просит метка остановки (`limit_mark.py --поставить limit
    --сброс`), строкой — сообщение владельцу. Разбор один: час, разобранный
    дважды, разъезжается молча.
    """
    try:
        with open(путь, "rb") as файл:
            файл.seek(0, os.SEEK_END)
            файл.seek(max(0, файл.tell() - хвост))
            куски = файл.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    последний = None
    for строка in куски.splitlines():
        if "resetsAt" not in строка:
            continue
        try:
            запись = json.loads(строка)
        except ValueError:
            continue
        # Отказом запись называет САМ CLI своими полями — не наш поиск слов.
        отказ = (запись.get("error") == "rate_limit"
                 or запись.get("apiErrorStatus") == 429
                 or запись.get("isApiErrorMessage") is True)
        метка = _resets_at(запись) if отказ else None
        if метка:
            последний = метка
    return последний


def _resets_at(узел) -> int | None:
    """Значение `resetsAt` из записи, на любой глубине. Unix-время в секундах."""
    if isinstance(узел, dict):
        for ключ, значение in узел.items():
            if ключ == "resetsAt" and isinstance(значение, (int, float)):
                return int(значение)
            найдено = _resets_at(значение)
            if найдено:
                return найдено
    elif isinstance(узел, list):
        for значение in узел:
            найдено = _resets_at(значение)
            if найдено:
                return найдено
    return None


# Поток `--output-format stream-json --verbose`: отказ называют ПОЛЯ события
# result, а не слова. Слова судили бы и ответ модели — check-builder пишет
# пробы про лимиты, и «429» в его ответе объявлялся лимитом подписки.
def события_потока(текст: str) -> list[dict]:
    """JSON-события вывода; строки не-JSON (stderr, шум) пропускаются."""
    найдено = []
    for строка in текст.splitlines():
        if not строка.startswith("{"):
            continue
        try:
            событие = json.loads(строка)
        except ValueError:
            continue
        if isinstance(событие, dict) and "type" in событие:
            найдено.append(событие)
    return найдено


def итог_потока(события: list[dict]) -> dict | None:
    итоги = [e for e in события if e["type"] == "result"]
    return итоги[-1] if итоги else None


def _сброс_отказа(события: list[dict]) -> int | None:
    """resetsAt отказа: ПОСЛЕДНИЙ из assistant.api_error_params и rejected.

    allowed_warning — предупреждение о другом окне, его час не срок отказа.
    """
    метка = None
    for e in события:
        if e["type"] == "assistant":
            инфо = (e.get("api_error_params") or {}).get("rate_limit_info")
        elif e["type"] == "rate_limit_event":
            инфо = e.get("rate_limit_info")
            if (инфо or {}).get("status") != "rejected":
                инфо = None
        else:
            continue
        число = (инфо or {}).get("resetsAt")
        if isinstance(число, (int, float)):
            метка = int(число)
    return метка


def лимит_в_итоге(события: list[dict], итог: dict) -> str | None:
    """Строка сброса, если result — отказ по лимиту; None — не лимит."""
    if итог.get("is_error") is not True:
        return None
    по_полям = (итог.get("api_error") == "usage_limit_reached"
                or итог.get("api_error_status") == 429)
    текст = лимит_в_тексте(str(итог.get("result", "")))
    # Неизвестный вид отказа считается отсутствующим: CLI переименует вид, а
    # лимит должен остаться лимитом. Но текст судится ТОЛЬКО при is_error.
    if not по_полям and текст is None:
        return None
    число = _сброс_отказа(события)
    if число:
        return "обновится в " + datetime.datetime.fromtimestamp(число).strftime("%H:%M")
    return текст or "срок сброса CLI не назвал"


def строка_расхода(события: list[dict], итог: dict, демон: str, когда: str) -> dict:
    """Расход берётся из result.usage: у assistant счётчик промежуточный."""
    расход = итог.get("usage") or {}
    начало = next((e for e in события if e["type"] == "system"), {})
    return {"когда": когда, "демон": демон, "модель": начало.get("model"),
            "session_id": итог.get("session_id"),
            "total_cost_usd": итог.get("total_cost_usd"),
            "input_tokens": расход.get("input_tokens"),
            "output_tokens": расход.get("output_tokens"),
            "cache_creation_input_tokens": расход.get("cache_creation_input_tokens"),
            "cache_read_input_tokens": расход.get("cache_read_input_tokens"),
            "num_turns": итог.get("num_turns"), "duration_ms": итог.get("duration_ms"),
            "is_error": итог.get("is_error"), "api_error": итог.get("api_error")}


def в_потоке(путь: str, режим: str = "суд", демон: str = "", когда: str = "") -> int:
    """Судья потока. Коды: 0 лимит, 1 нет, 2 файл не прочитан, 3 нет result."""
    try:
        текст = Path(путь).read_text(encoding="utf-8", errors="replace")
    except OSError as беда:
        print(f"вывод не прочитан: {беда}", file=sys.stderr)
        return 2
    события = события_потока(текст)
    if not события:
        # Вывод без единого события (CLI упал до потока): прежний разбор слов.
        сброс = лимит_в_тексте(текст)
        if режим == "текст":
            print(текст, end="")
            return 0
        if режим == "расход":
            return 1
        if сброс is None:
            return 1
        print(сброс)
        return 0
    итог = итог_потока(события)
    if итог is None:
        return 3
    if режим == "текст":
        print(итог.get("result", ""))
        return 0
    if режим == "расход":
        print(json.dumps(строка_расхода(события, итог, демон, когда), ensure_ascii=False))
        return 0
    сброс = лимит_в_итоге(события, итог)
    if сброс is None:
        return 1
    print(сброс)
    return 0


# ── Единый судья «лимит держится» ───────────────────────────────────────────
# Лимит в журнале появляется раньше отказа 429: заметка CLI wrap_up и запись
# system/informational «continuing automatically at 5:30pm» стоят за десятки
# минут до него. Судья, видевший только 429, объявлял «доказан 0», и сторож
# каждые десять минут пинал смену в живой лимит (30.10.2026). Здесь все
# носители факта сведены к одному ответу: когда обновится и когда будить.
_ВРЕМЯ_СУТОК = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b|(\d{1,2}):(\d{2})\b", re.IGNORECASE)
_ИМЯ_ПОЯСА = re.compile(r"\(([A-Za-z_]+/[\w/+\-]+|UTC)\)")


def _ключ_конфига(имя: str) -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
    import config as конф                              # noqa: PLC0415
    return str(конф.конфиг(конф.путь_harness_conf()).get(имя) or "").strip()


def _пояс(имя: str):
    """Пояс срока: названный в тексте, иначе пояс владельца, иначе машины (None)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError   # noqa: PLC0415
    for кандидат in (имя, _ключ_конфига("OWNER_TZ")):
        try:
            if кандидат:
                return ZoneInfo(кандидат)
        except ZoneInfoNotFoundError:
            continue
    return None


def сброс_из_текста(текст: str, якорь: float, после_якоря: bool = False) -> int | None:
    """unix-время срока из «resets 5:30pm (Asia/Qostanay)»; None — срока нет.

    Даты в тексте нет. Заметка CLI пишется в момент лимита и называет БУДУЩИЙ
    час — берётся первое наступление после её времени (`после_якоря`). Экран же
    может остаться после сброса, и «следующее наступление» обещало бы тот же
    час на завтра: для него берётся ближайшее к `якорю`.
    """
    найдено = _СБРОС.search(текст)
    время = _ВРЕМЯ_СУТОК.match(найдено.group("срок").strip()) if найдено else None
    if not время:
        return None
    if время.group(3):
        часы = int(время.group(1)) % 12 + (12 if время.group(3).lower() == "p" else 0)
        минуты = int(время.group(2) or 0)
    else:
        часы, минуты = int(время.group(4)), int(время.group(5))
    if часы > 23 or минуты > 59:
        return None
    названный = _ИМЯ_ПОЯСА.search(найдено.group("срок"))
    пояс = _пояс(названный.group(1) if названный else "")
    опорный = datetime.datetime.fromtimestamp(якорь, пояс)
    сутки = [опорный.replace(hour=часы, minute=минуты, second=0, microsecond=0)
             + datetime.timedelta(days=д) for д in (-1, 0, 1)]
    if после_якоря:
        return int(min(c.timestamp() for c in сутки if c.timestamp() > якорь))
    return int(min(сутки, key=lambda c: abs(c.timestamp() - якорь)).timestamp())


def _момент(запись: dict) -> float | None:
    try:
        return datetime.datetime.fromisoformat(
            str(запись.get("timestamp")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _текст_записи(запись: dict) -> str:
    содержимое = (запись.get("message") or {}).get("content")
    if isinstance(содержимое, str):
        return содержимое
    return " ".join(str(блок.get("text") or "") for блок in содержимое or [] if isinstance(блок, dict))


def _сигналы_журнала(путь: str, сейчас: float, хвост: int = ХВОСТ_БАЙТ):
    """→ ([(момент, сброс|None, вид)], момент последнего настоящего ответа,
    момент последнего содержательного ответа).

    Вид — «отказ» (429) или «текст» (заметка CLI). Настоящий ответ — не отказ и
    не синтетическая запись CLI: отказ без срока старше него уже не лимит.
    Содержательный ответ — настоящий, чей текст сам не говорит о лимите: он снимает
    метку и отказ старше себя.

    Заметка release сигналом НЕ является: CLI пишет её в ответ на пинок при
    ДЕРЖАЩЕМСЯ лимите. Записи новее `сейчас` не берутся: судят «на тот миг».
    """
    try:
        with open(путь, "rb") as файл:
            файл.seek(0, os.SEEK_END)
            файл.seek(max(0, файл.tell() - хвост))
            куски = файл.read().decode("utf-8", errors="replace")
    except OSError:
        return [], 0.0, 0.0
    сигналы, ответ, содержательный = [], 0.0, 0.0
    for строка in куски.splitlines():
        try:
            запись = json.loads(строка)
        except ValueError:
            continue
        момент = _момент(запись) if isinstance(запись, dict) else None
        if момент is None or момент > сейчас:
            continue
        сброс = _resets_at(запись)
        if запись.get("type") == "assistant" and (
                запись.get("error") == "rate_limit" or запись.get("apiErrorStatus") == 429
                or (запись.get("isApiErrorMessage") is True and сброс)):
            сигналы.append((момент, сброс, "отказ"))
        elif запись.get("type") == "assistant" and (запись.get("message") or {}).get("model") != "<synthetic>":
            ответ = max(ответ, момент)
            if лимит_в_тексте(_текст_записи(запись)) is None:
                содержательный = max(содержательный, момент)
        elif запись.get("usageLimitNote") == "wrap_up":
            сигналы.append((момент, None, "текст"))
        elif запись.get("subtype") == "informational" and лимит_в_тексте(str(запись.get("content"))):
            сигналы.append((момент, сброс_из_текста(str(запись["content"]), момент, True), "текст"))
    return сигналы, ответ, содержательный


def действует(путь: str, экран: str = "", сейчас: float | None = None,
              с_меткой: bool = False) -> tuple[int | None, int | None, bool] | None:
    """Держится ли лимит: (сброс, подъём, был_отказ); срок None — не назван; None — не держится.

    Лимит держится до подъёма (сброс + LIMIT_KICK_DELAY_SEC), если срок назван;
    без срока — пока признак моложе LIMIT_KICK_MAX_MIN. Срок из числа resetsAt старше срока из текста: текст
    («5:30pm») без даты, и учитывается лишь новее последнего отказа.
    """
    сейчас = сейчас if сейчас is not None else time.time()
    задержка = int(_ключ_конфига("LIMIT_KICK_DELAY_SEC") or 10)
    потолок = int(_ключ_конфига("LIMIT_KICK_MAX_MIN") or 60) * 60
    сигналы, ответ, содержательный = _сигналы_журнала(путь, сейчас)
    if с_меткой:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import limit_mark as метка                     # noqa: PLC0415
        когда, повод, сброс = метка.прочитать()
        if метка.файл().exists() and повод == "limit" and сброс and когда <= сейчас:
            сигналы.append((когда, сброс, "метка"))
    # Смена отвечает по делу после метки или отказа — модель лимита не видит
    # (недельный лимит другой модели); держать лимит дальше значит глушить живую смену.
    сигналы = [с for с in сигналы if с[2] == "текст" or с[0] > содержательный]
    if экран and лимит_в_тексте(экран) is not None:
        сигналы.append((сейчас, сброс_из_текста(экран, сейчас), "текст"))
    машинный = max((м for м, _, вид in сигналы if вид != "текст"), default=0)
    сбросы = [с for м, с, вид in сигналы if с and (вид != "текст" or м > машинный)]
    подъём = max(сбросы) + задержка if сбросы else 0
    if сейчас < подъём:
        return max(сбросы), подъём, any(вид == "отказ" and с == max(сбросы) for _, с, вид in сигналы)
    # Заметка CLI ответом не гасится: в окно лимита смена отвечает сама
    # («лимит достигнут, останавливаюсь»). Отказ — гасится: он новее ответа или нет.
    новые = [вид for м, с, вид in сигналы
             if с is None and м > подъём and сейчас - м <= потолок and (вид == "текст" or м > ответ)]
    return (None, None, "отказ" in новые) if новые else None


def _пробы_сброса(проба) -> None:
    """Пробы на `сброс_из_журнала`. Больные случаи — из ревью 27.09.2026.

    C-1: признак лимита по словам ловил «429» внутри обычного JSON и цитату
    исходника сторожа — лимит объявлялся почти на любом живом журнале.
    M-1: бралось САМОЕ СТАРОЕ совпадение, то есть час недельной давности.
    """
    import tempfile
    from pathlib import Path as П
    сейчас = time.time()
    with tempfile.TemporaryDirectory() as врем:
        путь = П(врем) / "журнал.jsonl"
        # БОЛЬНОЙ СЛУЧАЙ: 429 в полях расхода и слова лимита в обычном тексте.
        путь.write_text(json.dumps({
            "type": "assistant",
            "message": {"usage": {"output_tokens": 429}},
            "text": "hit your session limit · resets 9pm"}) + "\n",
            encoding="utf-8")
        проба("БОЛЬНОЙ СЛУЧАЙ: «429» в расходе и слова лимита в тексте — не отказ",
              сброс_из_журнала(str(путь), сейчас=сейчас) is None)
        # Запись, которую отказом называет САМ CLI: время берётся числом.
        строки = [
            json.dumps({"apiErrorStatus": 429,
                        "quotaLimits": {"resetsAt": int(сейчас) + 3600}}),
            json.dumps({"type": "assistant", "message": {"content": "работаю"}}),
            json.dumps({"isApiErrorMessage": True,
                        "quotaLimits": {"resetsAt": int(сейчас) + 1800}}),
        ]
        путь.write_text("\n".join(строки) + "\n", encoding="utf-8")
        итог = сброс_из_журнала(str(путь), сейчас=сейчас)
        проба("время сброса взято из записи отказа",
              итог is not None and 29 <= итог[0] <= 30)
        проба("БОЛЬНОЙ СЛУЧАЙ: берётся ПОСЛЕДНЯЯ запись, а не первая в файле",
              итог is not None and "через 29 мин" in итог[1] or "через 30 мин" in итог[1])
        проба("строка для владельца коротка — не простыня",
              итог is not None and len(итог[1]) <= 60)
        # Сброс уже прошёл: минуты отрицательные, и это законный ответ.
        путь.write_text(json.dumps({
            "apiErrorStatus": 429,
            "quotaLimits": {"resetsAt": int(сейчас) - 600}}) + "\n",
            encoding="utf-8")
        итог = сброс_из_журнала(str(путь), сейчас=сейчас)
        проба("прошедший сброс даёт отрицательные минуты",
              итог is not None and итог[0] < 0)
        проба("файла нет — не отказ, а пусто (сброс)",
              сброс_из_журнала(str(П(врем) / "нету.jsonl")) is None)


def _пробы_ответа_после_отказа(проба) -> None:
    """Содержательный ответ новее отказа снимает лимит (больной случай 02.11.2026)."""
    import tempfile
    from pathlib import Path as П
    сейчас = time.time()

    def момент(назад: int) -> str:
        return datetime.datetime.fromtimestamp(сейчас - назад, datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")

    отказ = {"type": "assistant", "error": "rate_limit", "apiErrorStatus": 429, "isApiErrorMessage": True,
             "quotaLimits": {"resetsAt": int(сейчас) + 86400},
             "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "You've hit your weekly limit"}]}}

    def ответ(текст: str) -> dict:
        return {"type": "assistant", "message": {"model": "claude-sonnet-4-5",
                                                 "content": [{"type": "text", "text": текст}]}}

    def судья(*записи: tuple[int, dict]) -> bool:
        with tempfile.TemporaryDirectory() as врем:
            путь = П(врем) / "журнал.jsonl"
            путь.write_text("".join(json.dumps({**з, "timestamp": момент(н)}) + "\n" for н, з in записи),
                            encoding="utf-8")
            return действует(str(путь), сейчас=сейчас) is not None

    проба("отказ со сроком впереди и без ответов — держится", судья((600, отказ)))
    проба("БОЛЬНОЙ СЛУЧАЙ: ответ по делу новее отказа — лимит снят",
          not судья((600, отказ), (60, ответ("Сделано: пробы написаны."))))
    проба("ответ с текстом лимита новее отказа не снимает",
          судья((600, отказ), (60, ответ("You've hit your weekly limit · resets Nov 4, 6pm"))))
    проба("отказ новее ответа — держится", судья((600, ответ("Сделано.")), (60, отказ)))


def _selftest() -> int:
    import tempfile
    плохо = 0

    def проба(имя, хорошо):
        nonlocal плохо
        print(("  ок    " if хорошо else "  ПЛОХО ") + имя)
        плохо = плохо or not хорошо

    # БОЛЬНОЙ СЛУЧАЙ 17.10.2026 (снимок владельца): в канал ушло «resets
    # 3:10pm» — чужой формат часа и чужое слово. Его правило: 24 часа и
    # человеческим языком.
    проба("час после полудня переведён в 24-часовой",
          лимит_в_тексте("hit your limit · resets 3:10pm (Asia/Qostanay)")
          == "обновится в 15:10 (Asia/Qostanay)")
    проба("полночь не становится 12:05",
          лимит_в_тексте("rate limit · resets 12:05 AM") == "обновится в 00:05")
    проба("полдень становится 12:00, а не 00:00",
          в_24_часа("resets 12pm") == "resets 12:00")
    проба("час уже 24-часовой — не трогаем",
          лимит_в_тексте("rate limit · resets 15:10") == "обновится в 15:10")
    проба("обычная неудача модели лимитом не становится",
          лимит_в_тексте("connection reset by peer") is None)

    сейчас = time.time()
    давно = datetime.datetime.fromtimestamp(сейчас - 1800, datetime.timezone.utc).isoformat()
    недавно = datetime.datetime.fromtimestamp(сейчас - 120, datetime.timezone.utc).isoformat()

    with tempfile.TemporaryDirectory() as врем:
        путь = os.path.join(врем, "проба.jsonl")

        # БОЛЬНОЙ СЛУЧАЙ 13.09.2026: смена стояла в лимите, а владелец узнавал
        # об этом через 5 минут в лучшем случае — потому что признак искали
        # только на десятиминутном обходе.
        with open(путь, "w", encoding="utf-8") as ф:
            ф.write(json.dumps({"type": "assistant", "timestamp": давно}) + "\n")
            ф.write(json.dumps({"error": "rate_limit", "timestamp": недавно}) + "\n")
        вышло = минут_с_отказа(путь, сейчас=сейчас)
        проба(f"БОЛЬНОЙ СЛУЧАЙ: свежий отказ 429 найден ({вышло} мин назад)",
              вышло is not None and вышло <= 3)

        # Второе поле того же факта: apiErrorStatus вместо error.
        with open(путь, "w", encoding="utf-8") as ф:
            ф.write(json.dumps({"apiErrorStatus": 429, "timestamp": недавно}) + "\n")
        проба("отказ узнаётся и по apiErrorStatus",
              минут_с_отказа(путь, сейчас=сейчас) is not None)

        # Слово «лимит» в моём же отчёте признаком быть не должно.
        with open(путь, "w", encoding="utf-8") as ф:
            ф.write(json.dumps({"type": "assistant", "timestamp": недавно,
                                "message": "упёрся в лимит подписки, rate_limit"}) + "\n")
        проба("текст про лимит в моём отчёте признаком НЕ становится",
              минут_с_отказа(путь, сейчас=сейчас) is None)

        # Берётся ПОСЛЕДНИЙ отказ, а не первый: иначе старый лимит вечно
        # выглядел бы свежим.
        with open(путь, "w", encoding="utf-8") as ф:
            ф.write(json.dumps({"error": "rate_limit", "timestamp": давно}) + "\n")
            ф.write(json.dumps({"error": "rate_limit", "timestamp": недавно}) + "\n")
        вышло = минут_с_отказа(путь, сейчас=сейчас)
        проба(f"берётся последний отказ, а не первый ({вышло} мин)",
              вышло is not None and вышло <= 3)

        # Битая строка не роняет разбор: транскрипт пишется на лету.
        with open(путь, "w", encoding="utf-8") as ф:
            ф.write("{не json\n")
            ф.write(json.dumps({"error": "rate_limit", "timestamp": недавно}) + "\n")
        проба("битая строка не роняет разбор",
              минут_с_отказа(путь, сейчас=сейчас) is not None)

        проба("файла нет — не отказ, а пусто", минут_с_отказа(путь + ".нет") is None)


    # Поток: отказ называют поля result, слова в ответе модели — нет.
    успех = [{"type": "result", "is_error": False, "result": "проба: код 429, rate limit"}]
    отказ = [{"type": "result", "is_error": True, "api_error_status": 429,
              "result": "You've hit your usage limit"}]
    проба("поток: «429» в ответе модели при is_error false — не лимит",
          лимит_в_итоге(успех, успех[0]) is None)
    проба("поток: is_error и api_error_status 429 — лимит",
          лимит_в_итоге(отказ, отказ[0]) == "срок сброса CLI не назвал")
    проба("поток: час сброса числом из rejected, а не из allowed_warning",
          _сброс_отказа([{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning", "resetsAt": 1}},
                         {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 2}}]) == 2)

    _пробы_сброса(проба)
    _пробы_ответа_после_отказа(проба)

    print("САМОТЕСТ %s: 15 путей, первым — больной случай"
          % ("ПРОВАЛЕН" if плохо else "ПРОЙДЕН"))
    return 1 if плохо else 0


def main(argv) -> int:
    if "--действует" in argv:
        # Зовёт сторож сессий (limit_active) и limit-watch: «1<TAB>сброс<TAB>подъём», код 0 —
        # лимит держится; «0», код 1 — нет. Срок не назван — поля пусты.
        def довод(ключ):
            return argv[argv.index(ключ) + 1] if ключ in argv else None
        экран = довод("--экран")
        текст = Path(экран).read_text(encoding="utf-8", errors="replace") if экран else ""
        сейчас = довод("--сейчас")
        итог = действует(argv[argv.index("--действует") + 1], текст,
                         float(сейчас) if сейчас else None, "--метка" in argv)
        if итог is None:
            print(0)
            return 1
        срок = tuple("" if x is None else x for x in итог[:2])
        # Четвёртое поле — только по просьбе: оно говорит, был ли ОТКАЗ 429, а не
        # одна заметка CLI (владельцу «упёрся» говорят после отказа).
        print("1\t%s\t%s" % срок + ("\t%d" % итог[2] if "--с-отказом" in argv else ""))
        return 0
    if "--в-тексте" in argv:
        # Зовёт обёртка claude-demon.sh: у факта «лимит подписки» один судья.
        путь = argv[argv.index("--в-тексте") + 1]
        try:
            текст = Path(путь).read_text(encoding="utf-8", errors="replace")
        except OSError as беда:
            print(f"вывод не прочитан: {беда}", file=sys.stderr)
            return 2
        сброс = лимит_в_тексте(текст)
        if сброс is None:
            return 1
        print(сброс)
        return 0
    for флаг, режим in (("--в-потоке", "суд"), ("--текст-итога", "текст"), ("--расход", "расход")):
        if флаг in argv:
            # Зовёт обёртка: --расход <файл> <демон> <когда> печатает строку расхода.
            место = argv.index(флаг)
            хвост = argv[место + 1:]
            return в_потоке(хвост[0], режим, *хвост[1:3])
    if "--сброс" in argv:
        # Зовёт сторож сессий, и ТОЛЬКО при доказанном свежем отказе.
        # Печатает «<минут>\t<в HH:MM, через N мин>»: решение принимается ЧИСЛОМ,
        # а человеческая строка идёт владельцу.
        итог = сброс_из_журнала(argv[argv.index("--сброс") + 1])
        if итог is None:
            return 1
        print("%d\t%s" % итог)
        return 0
    if "--сброс-числом" in argv:
        # Зовёт ежеминутный limit-watch.sh, чтобы положить час в метку: метка
        # хранит ЧИСЛО, а слова владельцу собирает её судья (limit_mark.py).
        метка = сброс_числом(argv[argv.index("--сброс-числом") + 1])
        if метка is None:
            return 1
        print(метка)
        return 0
    if "--selftest" in argv:
        return _selftest()
    if not argv:
        print("нужен путь к транскрипту", file=sys.stderr)
        return 2
    минут = минут_с_отказа(argv[0])
    if минут is None:
        return 1
    print(минут)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
