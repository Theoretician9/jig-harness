#!/usr/bin/env bash
#
# voice-worker.sh — обработчик голосовых владельца: скачивание и расшифровка
# вне цикла приёма диспетчера, чтобы минуты работы не держали канал. Один на
# машину (flock): занят — работает другой. Предел памяти расшифровки —
# от памяти машины: доля MemTotal (VOICE_VM_MEM_PERCENT), не ниже пола
# VOICE_VM_LIMIT_KB и не выше потолка VOICE_VM_MAX_KB. Замер 28.10: пол на машине
# с 4 ядрами между 1 200 000 и 1 500 000 КБ; MKL берёт память по числу ядер, поэтому
# одно число на все машины не годится.
#
# Очередь — $LOG_DIR/inbox/вложения/<message_id>.json; итог — <message_id>.done.
# В inbox/, inbox.jsonl, панель и канал обработчик не пишет: итог подбирает
# диспетчер (voice_pickup). Журнал своих строк — в stdout.
set -euo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/lib/config.sh"
INSTALL_CONF="${HARNESS_INSTALL_CONF:-$(konf_koren)/install.conf}"
HARNESS_CONF="${HARNESS_CONF:-$(konf_koren)/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "voice-worker: нет $INSTALL_CONF"; exit 1; }
konf_zagruzit
# Значения по умолчанию — ПОСЛЕ загрузчика: стоящие выше затёрли бы ключи конфига.
VOICE_VM_LIMIT_KB="${VOICE_VM_LIMIT_KB:-2500000}"
VOICE_VM_MEM_PERCENT="${VOICE_VM_MEM_PERCENT:-40}"
VOICE_VM_MAX_KB="${VOICE_VM_MAX_KB:-8000000}"
SERVICE_LOCKS_SUBDIR="${SERVICE_LOCKS_SUBDIR:-services}"

# Предел расшифровки, КБ, для памяти машины $1 (КБ). Память не прочиталась — пол.
voice_vm_limit_kb() {
    local mem="${1:-0}" limit
    limit=$(( mem * VOICE_VM_MEM_PERCENT / 100 ))
    [ "$limit" -ge "$VOICE_VM_LIMIT_KB" ] || limit="$VOICE_VM_LIMIT_KB"
    [ "$limit" -le "$VOICE_VM_MAX_KB" ] || limit="$VOICE_VM_MAX_KB"
    echo "$limit"
}
# Вычисление судит самотест на стендовых значениях MemTotal.
if [ "${1:-}" = "--limit-for" ]; then
    voice_vm_limit_kb "${2:-0}"
    exit 0
fi
VOICE_VM_EFFECTIVE_KB="$(voice_vm_limit_kb "$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null)")"

QUEUE="$LOG_DIR/inbox/вложения"
LOCK_FILE="$LOG_DIR/locks/$SERVICE_LOCKS_SUBDIR/voice-worker.lock"
mkdir -p "$(dirname "$LOCK_FILE")" "$QUEUE"
exec 8>> "$LOCK_FILE"
flock -n 8 || exit 0

log() { printf '%s  voice-worker: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# Самое старое сообщение без итога; пусто — очередь пуста.
next_job() {
    local f
    while IFS= read -r f; do
        [ -e "${f%.json}.done" ] || { echo "$f"; return 0; }
    done < <(find "$QUEUE" -maxdepth 1 -name '*.json' -printf '%T@ %p\n' 2>/dev/null | sort -n | cut -d' ' -f2-)
    return 0
}

write_done() {  # $1=id, $2=ok, $3=этап, $4=текст, $5=причина, $6=аудио
    local out="$QUEUE/$1.done"
    jq -n --argjson ok "$2" --arg stage "$3" --arg text "$4" --arg reason "$5" --arg audio "$6" \
        '{"ok":$ok,"этап":$stage,"текст":$text,"причина":$reason,"аудио":$audio}' > "$out.tmp"
    mv "$out.tmp" "$out"
}

process() {  # $1 = путь <id>.json
    local json="$1" id rc=0 audio text
    id="$(basename "$json" .json)"
    audio=$(python3 "${HARNESS_DOWNLOAD_PY:-$HERE/download_attachment.py}" "$json" 2>&1) || rc=$?
    if [ "$rc" != 0 ]; then
        log "$id: скачивание не удалось (rc=$rc): ${audio:0:200}"
        write_done "$id" false "скачивание" "" "$audio" ""
        return 0
    fi
    rc=0
    text=$(bash -c 'ulimit -v "$1"; exec python3 "$2" "$3"' _ "$VOICE_VM_EFFECTIVE_KB" \
               "${HARNESS_TRANSCRIBE_PY:-$HERE/transcribe_voice.py}" "$audio" 2>&1) || rc=$?
    if [ "$rc" != 0 ] || [ -z "$text" ]; then
        log "$id: расшифровка не удалась (rc=$rc): ${text: -200}"
        write_done "$id" false "расшифровка" "" "$text" "$audio"
        return 0
    fi
    log "$id: расшифровано, ${#text} знаков"
    write_done "$id" true "" "$text" "" "$audio"
}

while true; do
    job="$(next_job)"
    [ -n "$job" ] || break
    process "$job"
done
