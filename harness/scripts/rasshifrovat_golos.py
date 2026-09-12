#!/usr/bin/env python3
"""Расшифровка голосового сообщения владельца — ЛОКАЛЬНО, без платного API.

Почему переписан (11.09.2026, пункт 12 разбора кода). Путей было два, и оба
мёртвые: этот скрипт ходил в платный API, ключа к которому нет и не было
(`$SECRETS_DIR/openai_api_key` пуст, пакет openai не установлен), а сессия при
получении голосового каждый раз бралась искать whisper заново и не находила.
Улика: голосовое владельца от 11.09.2026 00:13 (79 секунд) так и осталось
нерасшифрованным — а в нём был заказ на разбор кода харнеса.

Теперь путь ОДИН: faster-whisper в отдельном venv (`/var/lib/harness/voice/venv`,
436 МБ). Системный python не трогаем — он под PEP 668 и на нём живёт весь
харнес. Замер на том самом голосовом: 79 с аудио → 24 с расшифровки, модель
small, CPU, int8.

Запуск:
    rasshifrovat_golos.py <файл.oga|.ogg|.mp3|.m4a|.wav>
    rasshifrovat_golos.py --selftest

Печатает расшифровку в stdout. Код возврата: 0 — расшифровано, 1 — нечем
(контур не установлен), 2 — беда с файлом.
"""
import os
import subprocess
import sys

VENV = os.environ.get("VOICE_VENV") or "/var/lib/harness/voice/venv"
PYTHON = os.path.join(VENV, "bin", "python")
МОДЕЛЬ = os.environ.get("VOICE_MODEL") or "small"

# Расшифровка идёт в ОТДЕЛЬНОМ интерпретаторе: faster-whisper живёт в своём
# venv, а зовут этот скрипт системным python (диспетчер, агент, cron).
РАБОТА = r'''
import sys, json
from faster_whisper import WhisperModel
путь, модель_имя = sys.argv[1], sys.argv[2]
модель = WhisperModel(модель_имя, device="cpu", compute_type="int8")
отрезки, инфо = модель.transcribe(путь, language="ru", vad_filter=True)
текст = " ".join(о.text.strip() for о in отрезки).strip()
print(json.dumps({"текст": текст, "секунд": round(инфо.duration, 1)}, ensure_ascii=False))
'''


def расшифровать(путь: str, потолок_сек: int = 600) -> tuple[int, str]:
    """(код возврата, текст либо причина отказа)."""
    if not os.path.exists(путь):
        return 2, f"файла нет: {путь}"
    if not os.path.exists(PYTHON):
        return 1, (f"контур голосовых не установлен: нет {PYTHON}. Поставить:\n"
                   f"  python3 -m venv {VENV} && {VENV}/bin/pip install faster-whisper")
    try:
        p = subprocess.run([PYTHON, "-c", РАБОТА, путь, МОДЕЛЬ],
                           capture_output=True, text=True, timeout=потолок_сек)
    except subprocess.TimeoutExpired:
        return 2, f"расшифровка не уложилась в {потолок_сек} с"
    if p.returncode != 0:
        return 2, f"расшифровка упала: {(p.stderr or '').strip()[-400:]}"
    import json
    try:
        ответ = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception as e:
        return 2, f"ответ расшифровки не разобран ({e!r}): {p.stdout[-200:]}"
    текст = ответ.get("текст") or ""
    if not текст:
        # Пустая расшифровка — не успех: владелец сказал что-то, а мы молчим.
        return 2, f"расшифровка пуста ({ответ.get('секунд')} с аудио) — тишина или чужой язык"
    return 0, текст


def самотест() -> int:
    """Проверяет контур на СВОЁМ файле: синтезировать речь нечем, поэтому
    доказательство — что контур установлен и отвечает на настоящем аудио."""
    ok = True
    код, ответ = расшифровать("/заведомо/нет/такого.oga")
    if код == 2 and "файла нет" in ответ:
        print("  ок    БОЛЬНОЙ СЛУЧАЙ: файла нет — отказ, а не молчание")
    else:
        print(f"  ПЛОХО файла нет: код {код}, ответ {ответ[:80]}"); ok = False
    if os.path.exists(PYTHON):
        print(f"  ок    контур установлен: {PYTHON}")
    else:
        print(f"  ПЛОХО контура нет: {PYTHON} отсутствует — голосовые расшифровать нечем"); ok = False
    print("САМОТЕСТ %s: 2 пути, первым — больной случай"
          % ("ПРОЙДЕН" if ok else "ПРОВАЛЕН"))
    return 0 if ok else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return самотест()
    if len(sys.argv) < 2:
        print("укажите путь к файлу голосового (.oga/.ogg/.mp3/.m4a/.wav)")
        return 2
    код, ответ = расшифровать(sys.argv[1])
    print(ответ)
    return код


if __name__ == "__main__":
    sys.exit(main())
