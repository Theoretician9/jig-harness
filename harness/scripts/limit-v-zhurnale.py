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
    python3 scripts/limit-v-zhurnale.py <транскрипт>   # минуты с последнего 429
    python3 scripts/limit-v-zhurnale.py --selftest
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
_СБРОС = re.compile(r"resets [^·|]+", re.IGNORECASE)


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
    return найдено.group(0).strip() if найдено else "срок сброса CLI не назвал"


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

    print("САМОТЕСТ %s: 6 путей, первым — больной случай"
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
