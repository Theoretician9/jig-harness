#!/usr/bin/env bash
#
# test_transcribe_voice.sh — проба самотеста и загрузки модели transcribe_voice.py.
#
# ЧТО ОХРАНЯЕТ. Находки копий 10.11.2026 (нескольких копий сети): самотест был зелёным, а
# первое голосовое падало. (1) Модель whisper качалась при ПЕРВОЙ расшифровке —
# под ulimit -v обработчика, и скачивание падало «memory allocation of 67038270
# bytes failed». (2) Самотест судил только наличие venv и шёл без предела памяти,
# поэтому мал ли предел машины, он не видел. Теперь модель качается отдельной
# командой вне предела, а самотест судит модель в кэше и расшифровывает пробный
# звук под тем пределом, под которым его зовут.
#
# Стенд: свой каталог, подставной venv (VOICE_VENV): его python — настоящий,
# он исполняет код работы из transcribe_voice.py, но модуль faster_whisper —
# подставной: кэш моделей файлами стенда, нехватка памяти по RLIMIT_AS ниже
# «нужного» стенду. Настоящий whisper и сеть не зовутся.
#
#   bash scripts/test_transcribe_voice.sh
# Код возврата: 0 — зелёная, 1 — красная. Последняя строка — итог с числом путей.
# Механизм берётся от места пробы (../ от scripts/): мутации идут «из-копии».
set -uo pipefail
export LC_ALL=C.UTF-8

KOREN="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
TRANSCRIBE="$KOREN/scripts/transcribe_voice.py"
T="$(mktemp -d /tmp/proba-transcribe-voice-XXXXXX)"
trap 'rm -rf "$T"' EXIT

ok=1
paths=0
check() {
    paths=$((paths + 1))
    if [ "$2" = "$3" ]; then printf '  ок    %s\n' "$1"
    else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$3"; ok=0; fi
}
has() { grep -q -- "$1" "$2" 2>/dev/null && echo да || echo нет; }

# Подставной faster_whisper. Нужная расшифровке память — файл need-kb (КБ);
# модель в кэше — файл cache-<имя>. Без модели в кэше WhisperModel качает её
# сам и под пределом падает так, как упало у копии.
mkdir -p "$T/venv/bin" "$T/fake/faster_whisper"
printf '#!/bin/sh\nPYTHONPATH="%s/fake" exec /usr/bin/python3 "$@"\n' "$T" > "$T/venv/bin/python"
chmod +x "$T/venv/bin/python"
cat > "$T/fake/faster_whisper/__init__.py" <<'PYEOF'
import os, resource
from types import SimpleNamespace
_st = os.environ["PROBA_VOICE_STAND"]
_soft = resource.getrlimit(resource.RLIMIT_AS)[0]
_limited = _soft != resource.RLIM_INFINITY
def _log(line):
    with open(os.path.join(_st, "calls"), "a") as fh:
        fh.write(line + "\n")
def _cache(name):
    return os.path.join(_st, "cache-" + name)
def download_model(name, local_files_only=False):
    _log(f"model {name} {'есть' if os.path.exists(_cache(name)) else 'нет'} local_only={local_files_only}")
    if os.path.exists(_cache(name)):
        return _cache(name)
    if local_files_only:
        raise RuntimeError("LocalEntryNotFoundError: модели нет в кэше (проба)")
    if _limited:
        raise MemoryError("memory allocation of 67038270 bytes failed")
    open(_cache(name), "w").close()
    _log(f"download {name}")
    return _cache(name)
class WhisperModel:
    def __init__(self, name, device, compute_type):
        self.name = name
        if not os.path.exists(_cache(name)):
            download_model(name)
    def transcribe(self, path, language, vad_filter):
        _log(f"transcribe {os.path.basename(path)} vad={int(vad_filter)} soft={_soft if _limited else 'нет'}")
        try:
            need = int(open(os.path.join(_st, "need-kb")).read())
        except OSError:
            need = 0
        if _limited and _soft < need * 1024:
            raise RuntimeError("mkl_malloc: failed to allocate memory")
        return iter([SimpleNamespace(text=" проба ")]), SimpleNamespace(duration=3.0)
PYEOF
echo 4000000 > "$T/need-kb"
: > "$T/voice.oga"

run() {  # $1 = предел КБ или «нет», дальше доводы transcribe_voice.py
    local lim="$1"; shift
    : > "$T/calls"
    env PROBA_VOICE_STAND="$T" VOICE_VENV="$T/venv" VOICE_MODEL=small \
        bash -c '[ "$1" = нет ] || ulimit -v "$1"; shift; exec python3 "$@"' _ "$lim" "$TRANSCRIBE" "$@" > "$T/out" 2>&1
    echo $?
}

echo "— модели нет в кэше"
rm -f "$T"/cache-*
check "БОЛЬНОЙ: модели нет в кэше — самотест провален (код 1)" "1" "$(run нет --selftest)"
check "БОЛЬНОЙ: модели нет в кэше — самотест это называет" "да" "$(has 'ПЛОХО модели small нет в кэше' "$T/out")"
check "самотест модель не качает (судит кэш, а не сеть)" "нет" "$(has '^download' "$T/calls")"

echo "— --fetch-model: загрузка вне предела"
check "БОЛЬНОЙ: --fetch-model при пустом кэше — модель скачана (код 0)" "0" "$(run нет --fetch-model)"
check "--fetch-model при пустом кэше — скачивание было" "да" "$(has '^download small' "$T/calls")"
check "--fetch-model с моделью в кэше — код 0" "0" "$(run нет --fetch-model)"
check "--fetch-model с моделью в кэше — без скачивания" "нет" "$(has '^download' "$T/calls")"

echo "— самотест под пределом памяти"
check "БОЛЬНОЙ: предел 2 500 000 КБ ниже нужного расшифровке — самотест провален" "1" "$(run 2500000 --selftest)"
check "БОЛЬНОЙ: под малым пределом самотест называет упавшую расшифровку пробного звука" "да" \
    "$(has 'ПЛОХО расшифровка пробного звука под пределом 2500000 КБ' "$T/out")"
check "предел 5 000 000 КБ — самотест пройден (код 0)" "0" "$(run 5000000 --selftest)"
check "самотест расшифровывает пробный звук под тем пределом, под которым позван" "да" \
    "$(has "^transcribe .* soft=$((5000000 * 1024))" "$T/calls")"
check "пробный звук идёт без VAD (тон не вырезается, кодировщик работает)" "да" "$(has '^transcribe .* vad=0' "$T/calls")"

echo "— обычная расшифровка"
check "голосовое расшифровано (код 0)" "0" "$(run 5000000 "$T/voice.oga")"
check "голосовое идёт с VAD" "да" "$(has '^transcribe voice.oga vad=1' "$T/calls")"

if [ "$ok" = 1 ]; then echo "ПРОБА ЗЕЛЁНАЯ: путей $paths"; exit 0; fi
echo "ПРОБА КРАСНАЯ: путей $paths"; exit 1
