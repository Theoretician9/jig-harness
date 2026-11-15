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
    transcribe_voice.py <файл.oga|.ogg|.mp3|.m4a|.wav>
    transcribe_voice.py --selftest
    transcribe_voice.py --fetch-model

Модель качается отдельной командой `--fetch-model` и ВНЕ предела памяти: при
первой расшифровке whisper качает её сам, и под ulimit -v обработчика скачивание
падает «memory allocation of 67038270 bytes failed» (находка bkb 10.11.2026).
Самотест судит модель в кэше и расшифровывает пробный звук под тем пределом,
под которым его позвали: без предела он был зелёным там, где предел машины мал
для расшифровки (находка sample-26 10.11.2026).

Печатает расшифровку в stdout. Код возврата: 0 — расшифровано, 1 — нечем
(контур не установлен), 2 — беда с файлом.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import config as конф                                     # noqa: E402

VENV = os.environ.get("VOICE_VENV") or os.path.join(конф.конфиг()["STATE_DIR"], "voice", "venv")
PYTHON = os.path.join(VENV, "bin", "python")
МОДЕЛЬ = os.environ.get("VOICE_MODEL") or "small"

# Расшифровка идёт в ОТДЕЛЬНОМ интерпретаторе: faster-whisper живёт в своём
# venv, а зовут этот скрипт системным python (диспетчер, агент, cron).
РАБОТА = r'''
import sys, json
from faster_whisper import WhisperModel
путь, модель_имя, vad = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
модель = WhisperModel(модель_имя, device="cpu", compute_type="int8")
отрезки, инфо = модель.transcribe(путь, language="ru", vad_filter=vad)
текст = " ".join(о.text.strip() for о in отрезки).strip()
print(json.dumps({"текст": текст, "секунд": round(инфо.duration, 1)}, ensure_ascii=False))
'''
# Режим «кэш» только смотрит, «скачать» качает, если в кэше нет.
МОДЕЛЬ_В_КЭШЕ = r'''
import sys
from faster_whisper import download_model
имя, режим = sys.argv[1], sys.argv[2]
try:
    print(download_model(имя, local_files_only=True))
except Exception:
    if режим != "скачать":
        raise
    print(download_model(имя))
'''


def нет_контура() -> tuple[int, str]:
    return 1, (f"контур голосовых не установлен: нет {PYTHON}. Поставить:\n"
               f"  python3 -m venv {VENV} && {VENV}/bin/pip install faster-whisper")


def модель(режим: str, потолок_сек: int = 1800) -> tuple[int, str]:
    """(0, путь модели) либо (код, причина). Режим «кэш» или «скачать»."""
    if not os.path.exists(PYTHON):
        return нет_контура()
    try:
        p = subprocess.run([PYTHON, "-c", МОДЕЛЬ_В_КЭШЕ, МОДЕЛЬ, режим],
                           capture_output=True, text=True, timeout=потолок_сек)
    except subprocess.TimeoutExpired:
        return 2, f"модель {МОДЕЛЬ} не скачалась за {потолок_сек} с"
    if p.returncode != 0:
        return 2, f"модели {МОДЕЛЬ} нет: {(p.stderr or '').strip()[-300:]}"
    return 0, p.stdout.strip()


def прогнать(путь: str, vad: bool, потолок_сек: int = 600) -> tuple[int, dict | str]:
    """(0, ответ работы) либо (код, причина отказа)."""
    if not os.path.exists(путь):
        return 2, f"файла нет: {путь}"
    if not os.path.exists(PYTHON):
        return нет_контура()
    try:
        p = subprocess.run([PYTHON, "-c", РАБОТА, путь, МОДЕЛЬ, "1" if vad else "0"],
                           capture_output=True, text=True, timeout=потолок_сек)
    except subprocess.TimeoutExpired:
        return 2, f"расшифровка не уложилась в {потолок_сек} с"
    if p.returncode != 0:
        return 2, f"расшифровка упала: {(p.stderr or '').strip()[-400:]}"
    import json
    try:
        return 0, json.loads(p.stdout.strip().splitlines()[-1])
    except Exception as e:
        return 2, f"ответ расшифровки не разобран ({e!r}): {p.stdout[-200:]}"


def расшифровать(путь: str) -> tuple[int, str]:
    """(код возврата, текст либо причина отказа)."""
    код, ответ = прогнать(путь, vad=True)
    if код != 0:
        return код, ответ
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
    код, ответ = модель("кэш")
    if код == 0:
        print(f"  ок    модель {МОДЕЛЬ} в кэше: {ответ}")
    else:
        print(f"  ПЛОХО модели {МОДЕЛЬ} нет в кэше — скачать: transcribe_voice.py --fetch-model ({ответ[-200:]})")
        ok = False
    if ok:
        ok = пробный_звук()
    print("САМОТЕСТ %s: 4 пути, первым — больной случай"
          % ("ПРОЙДЕН" if ok else "ПРОВАЛЕН"))
    return 0 if ok else 1


def пробный_звук() -> bool:
    """Расшифровка трёх секунд тона под пределом памяти вызвавшего: память берут
    модель и кодировщик, а не речь. VAD выключен — иначе тон вырезается целиком и
    кодировщик не работает."""
    import resource
    import tempfile
    import wave
    мягкий = resource.getrlimit(resource.RLIMIT_AS)[0]
    предел = "без предела" if мягкий == resource.RLIM_INFINITY else f"под пределом {мягкий // 1024} КБ"
    with tempfile.TemporaryDirectory(prefix="harness-voice-selftest-") as d:
        путь = os.path.join(d, "тон.wav")
        with wave.open(путь, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"".join(int(8000 * ((i // 18) % 2 * 2 - 1)).to_bytes(2, "little", signed=True)
                                   for i in range(16000 * 3)))
        код, ответ = прогнать(путь, vad=False)
    if код == 0:
        print(f"  ок    пробный звук расшифрован {предел}")
        return True
    print(f"  ПЛОХО расшифровка пробного звука {предел}: {str(ответ)[-200:]}")
    return False


def main() -> int:
    if "--selftest" in sys.argv:
        return самотест()
    if "--fetch-model" in sys.argv:
        код, ответ = модель("скачать")
        print(ответ)
        return код
    if len(sys.argv) < 2:
        print("укажите путь к файлу голосового (.oga/.ogg/.mp3/.m4a/.wav)")
        return 2
    код, ответ = расшифровать(sys.argv[1])
    print(ответ)
    return код


if __name__ == "__main__":
    sys.exit(main())
