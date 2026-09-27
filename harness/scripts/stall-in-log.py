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

Прогон:
    python3 scripts/stall-in-log.py <транскрипт>   # минуты с последнего ответа
    python3 scripts/stall-in-log.py --selftest
Код возврата: 0 — ответ найден (минуты на stdout), 1 — ответов нет или файл
нечитаем. «Не прочитали» и «ответов нет» здесь одно: в обоих случаях сказать о
немоте нечего, а положительного утверждения прибор не делает.
"""
import datetime
import json
import os
import sys
import time

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

    сейчас = time.time()

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
