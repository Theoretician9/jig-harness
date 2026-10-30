#!/usr/bin/env bash
#
# dispatcher-stall-watch.sh — сторож «приём канала встал».
#
# Больной случай 25.10.2026 (чужой сервер): `tmux send-keys` из цикла диспетчера висел
# 57 минут, getUpdates не звался, пять сообщений владельца не дошли, а журнал
# молчал. Предел на вызов tmux и фоновый толчок чинят САМУ причину; этот сторож
# ловит любую другую, из-за которой цикл перестал крутиться (зависший потомок,
# сеть, дедлок), — по признаку круга, который диспетчер пишет сам.
#
# Раз в минуту (cron). Тревога, если одновременно:
#   1. $LOG_DIR/tg-dispatcher.alive старше DISPATCH_STALL_MIN минут;
#   2. юнит диспетчера активен (лежащий юнит — другой сигнал, его ловит деадман);
#   3. в канале есть новое (getWebhookInfo.pending_update_count > 0).
#
# Выключен по умолчанию (DISPATCH_STALL_ENABLED=0): включает смена после живой
# проверки, не раскладчик.
set -uo pipefail
export LC_ALL=C.UTF-8

# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/config.sh"
konf_zagruzit

PROJECT_DIR="${PROJECT_DIR:?пуст PROJECT_DIR — не прочитан паспорт установки}"
LOG_DIR="${LOG_DIR:?пуст LOG_DIR}"
HEARTBEAT_DIR="${HEARTBEAT_DIR:-$LOG_DIR/heartbeat}"
mkdir -p "$HEARTBEAT_DIR" && : >"$HEARTBEAT_DIR/dispatcher-stall-watch"
[ "${DISPATCH_STALL_ENABLED:-0}" = 1 ] || exit 0

STALL_MIN="${DISPATCH_STALL_MIN:-5}"
TELL_MIN="${DISPATCH_STALL_TELL_MIN:-60}"
HEAL="${DISPATCH_STALL_HEAL:-0}"
HEAL_WAIT="${DISPATCH_STALL_HEAL_WAIT:-60}"
UNIT="${DISPATCH_STALL_UNIT:-harness-dispatcher}"
SYSTEMCTL="${DISPATCH_STALL_SYSTEMCTL:-systemctl}"
EMERGENCY="${DISPATCH_STALL_EMERGENCY:-$PROJECT_DIR/scripts/emergency-send.sh}"
ALIVE_FILE="$LOG_DIR/tg-dispatcher.alive"
TOLD_FILE="$LOG_DIR/dispatcher-stall.told"
WATCH_LOG="$LOG_DIR/dispatcher-stall-watch.log"
LOCKS="$LOG_DIR/locks/${SERVICE_LOCKS_SUBDIR:-services}"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$WATCH_LOG"; }

mkdir -p "$LOCKS"
exec 9>>"$LOCKS/dispatcher-stall-watch.lock"
flock -n 9 || exit 0

# Возраст признака в секундах; нет файла — признака ещё не было (диспетчер
# старый или только поднят), это не поломка круга.
alive_age() {
    local stamp
    [ -s "$ALIVE_FILE" ] || return 1
    stamp=$(head -1 "$ALIVE_FILE" 2>/dev/null)
    case "$stamp" in ''|*[!0-9]*) stamp=$(stat -c %Y "$ALIVE_FILE") ;; esac
    echo $(( $(date +%s) - stamp ))
}

# Сколько сообщений ждёт в канале. Не забирает обновлений и не мешает
# long-poll диспетчера. «?» — Bot API не ответил или поля нет.
#
# Спека «Неясно 1»: отдаёт ли getWebhookInfo pending_update_count при long-poll
# без вебхука — НЕ замерено живьём. Пока не замерено, «?» не гасит тревогу:
# сторож работает по одному возрасту признака (при живом цикле он не стареет и
# без сообщений, так что сигнал всё равно различает). Если замер покажет, что
# поле всегда есть, — «?» стоит считать отказом сторожа, а не тревогой.
pending_count() {
    local token resp n
    [ -s "${TG_TOKEN_FILE:-}" ] || { echo "?"; return 0; }
    token=$(tr -d '[:space:]' < "$TG_TOKEN_FILE")
    resp=$(printf 'url = "%s/bot%s/getWebhookInfo"\n' "${TG_API_BASE:-https://api.telegram.org}" "$token" \
           | curl -sS --max-time 15 -K - 2>/dev/null) || { echo "?"; return 0; }
    n=$(printf '%s' "$resp" | jq -r '.result.pending_update_count // "?"' 2>/dev/null) || n="?"
    echo "${n:-?}"
}

AGE=$(alive_age) || exit 0
[ "$AGE" -ge $(( STALL_MIN * 60 )) ] || exit 0

"$SYSTEMCTL" is-active --quiet "$UNIT" || exit 0
MAIN_PID=$("$SYSTEMCTL" show -p MainPID --value "$UNIT" 2>/dev/null || echo 0)

PENDING=$(pending_count)
[ "$PENDING" = 0 ] && exit 0

# Висящие потомки диспетчера старше порога: у живого цикла их нет (curl
# long-poll живёт ≤ 60 с, фоновые толчки ограничены пределом tmux).
stuck_children() {
    [ "${MAIN_PID:-0}" -gt 0 ] || return 0
    ps -o pid=,etimes=,args= --ppid "$MAIN_PID" 2>/dev/null \
        | awk -v min="$(( STALL_MIN * 60 ))" '$2 >= min { print }'
}
CHILDREN=$(stuck_children)

NOW=$(date +%s)
LAST=$(cat "$TOLD_FILE" 2>/dev/null || echo 0)
case "$LAST" in ''|*[!0-9]*) LAST=0 ;; esac
if [ $(( NOW - LAST )) -ge $(( TELL_MIN * 60 )) ]; then
    DETAIL="признак круга старше $(( AGE / 60 )) мин, PID диспетчера $MAIN_PID, ждёт сообщений: $PENDING"
    [ -z "$CHILDREN" ] || DETAIL="$DETAIL; висит: $(printf '%s' "$CHILDREN" | head -3 | cut -c1-120 | tr '\n' '|')"
    if "$EMERGENCY" "приём канала встал" "$DETAIL" >/dev/null 2>&1; then
        echo "$NOW" > "$TOLD_FILE"
        say "тревога отправлена: $DETAIL"
    else
        say "тревога НЕ ушла (emergency-send отказал): $DETAIL"
    fi
fi

[ "$HEAL" = 1 ] || exit 0

# Самолечение: сперва потомки (чаще всего виноват один зависший), диспетчер и
# tmux-сервер не трогаются. Не ожил за HEAL_WAIT — рестарт юнита.
if [ -n "$CHILDREN" ]; then
    printf '%s\n' "$CHILDREN" | while read -r pid _; do kill -TERM "$pid" 2>/dev/null; done
    say "самолечение: убраны висящие потомки диспетчера"
fi
sleep "$HEAL_WAIT"
NEW_AGE=$(alive_age) || exit 0
if [ "$NEW_AGE" -ge $(( STALL_MIN * 60 )) ]; then
    say "самолечение: признак не ожил за ${HEAL_WAIT}с — рестарт $UNIT"
    "$SYSTEMCTL" restart "$UNIT" >>"$WATCH_LOG" 2>&1 || say "рестарт $UNIT не удался"
fi
