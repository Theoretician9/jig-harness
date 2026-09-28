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
# Перевод строки в классе отрицания — тот же капкан, что «\s включает \n»:
# на журнале сессии совпадение тянулось до первого «·» через десятки строк, и
# сообщение владельцу выходило на 15 КБ — сторож канала его отвергал, то есть о
# лимите владелец не узнавал ВООБЩЕ (находка CRITICAL-2 ревью 27.09.2026).
_СБРОС = re.compile(r"resets [^·|\n\r]+", re.IGNORECASE)
# Сколько знаков строки сброса имеет смысл: владельцу нужен час, не простыня.
ПОТОЛОК_СТРОКИ = 200


def лимит_в_тексте(текст: str) -> str | None:
    """Отказ по лимиту в выводе CLI → строка сброса (или «срок не назван»).

    None означает «это не лимит»: обычная неудача модели обязана остаться
    обычной неудачей, иначе демон начнёт пропускать работу без причины.
    """
    if not СЛОВА_ЛИМИТА.search(текст):
        return None
    найдено = _СБРОС.search(текст)
    # Пояс в скобках — часть времени: обрезка по «)» давала «6pm (Asia/Qostanay»,
    # а владелец читает именно час (живой прогон 13.09.2026).
    if not найдено:
        return "срок сброса CLI не назвал"
    return найдено.group(0).strip()[:ПОТОЛОК_СТРОКИ]


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
    if not последний:
        return None
    осталось = int((последний - сейчас) // 60)
    часы, минуты = divmod(max(0, осталось), 60)
    сколько = f"{часы} ч {минуты} мин" if часы else f"{минуты} мин"
    когда = datetime.datetime.fromtimestamp(последний).strftime("%H:%M")
    return осталось, f"в {когда}, через {сколько}"


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


def _selftest() -> int:
    import tempfile
    плохо = 0

    def проба(имя, хорошо):
        nonlocal плохо
        print(("  ок    " if хорошо else "  ПЛОХО ") + имя)
        плохо = плохо or not хорошо

    сейчас = time.time()
    давно = datetime.datetime.fromtimestamp(сейчас - 1800, datetime.UTC).isoformat()
    недавно = datetime.datetime.fromtimestamp(сейчас - 120, datetime.UTC).isoformat()

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

    _пробы_сброса(проба)

    print("САМОТЕСТ %s: 12 путей, первым — больной случай"
          % ("ПРОВАЛЕН" if плохо else "ПРОЙДЕН"))
    return 1 if плохо else 0


def main(argv) -> int:
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
    if "--сброс" in argv:
        # Зовёт сторож сессий, и ТОЛЬКО при доказанном свежем отказе.
        # Печатает «<минут>\t<в HH:MM, через N мин>»: решение принимается ЧИСЛОМ,
        # а человеческая строка идёт владельцу.
        итог = сброс_из_журнала(argv[argv.index("--сброс") + 1])
        if итог is None:
            return 1
        print("%d\t%s" % итог)
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
