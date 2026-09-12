#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Расшифровка голосового сообщения владельца.

Владелец 08.08 проверил, читаю ли я голосовые. Напрямую — нет: инструмент чтения
файлов понимает картинки и PDF, звук не понимает. Но расшифровка стоит треть
цента за полминуты и снимает вопрос совсем: голосом писать можно.

Механизм, а не разовый фокус (правило: всё, что можно забыть, оформляется
кодом). После перезапуска сессии агент видит запись в памяти и приходит сюда.

    rasshifrovat_golos.py <файл.oga|.ogg|.mp3|.m4a>

Ключ берётся из `OPENAI_API_KEY`. У нас ключ лежит в хранилище секретов проекта,
и скрипт запускается внутри образа воркера, где переменная уже проставлена, —
подставьте свой источник, если он другой.

Файл голосового скачивается инструментом канала связи в его каталог входящих.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("укажите путь к файлу голосового (.oga/.ogg/.mp3/.m4a)")
        return 2
    ключ = os.environ.get("OPENAI_API_KEY", "").strip()
    if not ключ:
        print("нет OPENAI_API_KEY — расшифровать нечем")
        return 1

    from openai import OpenAI

    клиент = OpenAI(api_key=ключ)
    with open(sys.argv[1], "rb") as ф:
        из = клиент.audio.transcriptions.create(model="whisper-1", file=ф, language="ru")
    print(из.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
