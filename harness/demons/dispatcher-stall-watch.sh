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
#
# Вторая работа — сверка квитанций «принял» (scripts/check-ack-receipts.py):
# сообщение владельца старше минуты без удачной квитанции в журнале доставка —
# тревога, не чаще раза в час. Ключ круга её не выключает, своё выключение —
# DISPATCH_ACK_CHECK_ENABLED=0.
#
# Третья работа — страж ответа владельцу (OWNER_REPLY_WATCH_ENABLED, срок
# OWNER_REPLY_MAX_MIN): сообщение без ответа смены дольше срока — одна весть на эпизод.
set -uo pipefail
export LC_ALL=C.UTF-8

# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/config.sh"
konf_zagruzit

PROJECT_DIR="${PROJECT_DIR:?пуст PROJECT_DIR — не прочитан паспорт установки}"
LOG_DIR="${LOG_DIR:?пуст LOG_DIR}"
HEARTBEAT_DIR="${HEARTBEAT_DIR:-$LOG_DIR/heartbeat}"
mkdir -p "$HEARTBEAT_DIR" && : >"$HEARTBEAT_DIR/dispatcher-stall-watch"

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

# Сверка квитанций «принял» (указание владельца 03.11.2026). Стоит РАНЬШЕ ключа
# DISPATCH_STALL_ENABLED: тот выключает проверку круга до живой проверки сменой,
# а немая квитанция — другая поломка, и ей ключ круга не указ. Своё выключение —
# DISPATCH_ACK_CHECK_ENABLED=0.
ACK_CHECK="${DISPATCH_ACK_CHECK:-$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/check-ack-receipts.py}"
ACK_TOLD_FILE="$LOG_DIR/dispatcher-ack.told"
ack_receipts() {
    local found now last
    [ "${DISPATCH_ACK_CHECK_ENABLED:-1}" = 1 ] || return 0
    found=$(python3 "$ACK_CHECK" --log-dir "$LOG_DIR" --hours "${DISPATCH_ACK_HOURS:-6}" \
            --grace "${DISPATCH_ACK_GRACE_S:-60}" 2>&1) && return 0
    now=$(date +%s)
    last=$(cat "$ACK_TOLD_FILE" 2>/dev/null || echo 0)
    case "$last" in ''|*[!0-9]*) last=0 ;; esac
    [ $(( now - last )) -ge $(( ${DISPATCH_ACK_TELL_MIN:-60} * 60 )) ] || return 0
    if "$EMERGENCY" "владелец не получил «принял»" "сообщений без квитанции: $(printf '%s\n' "$found" | wc -l); первое: $(printf '%s' "$found" | head -1 | cut -c1-160)" >/dev/null 2>&1; then
        echo "$now" > "$ACK_TOLD_FILE"
        say "тревога о квитанциях отправлена: $(printf '%s' "$found" | head -1 | cut -c1-160)"
    else
        say "тревога о квитанциях НЕ ушла (emergency-send отказал)"
    fi
}
ack_receipts

# Третья работа — страж ответа владельцу (спека hr.alert-self-sensors-attention §5).
# Сообщение владельца без ответа смены в канале дольше OWNER_REPLY_MAX_MIN — одна весть
# на эпизод. Стоит РАНЬШЕ DISPATCH_STALL_ENABLED: поломка круга и немая смена — разные
# беды, и ключ круга этой работе не указ. Своё выключение — OWNER_REPLY_WATCH_ENABLED=0.
# Прибор, pane.sh и остальное берутся из дерева самого сторожа, а не из PROJECT_DIR: так
# проба судит копию целиком.
OR_ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
OR_EPISODE="$LOG_DIR/owner-reply.episode"
OR_AGENT_TOLD="$LOG_DIR/agent-pane.told"

# Решение — строкой в журнал «ответы-в-канал»: $1=событие, $2=исход, $3=сообщение, $4=возраст, мин, $5=причина.
or_event() {
    HARNESS_LOG_DIR="$LOG_DIR" harness_event ответы-в-канал harness/demons/dispatcher-stall-watch.sh "$1" "$2" \
        "message=$3" "age_min=$4" "reason=$5"
}

# 0 — агента в панели нет. «?» (tmux не ответил) об агенте судить не даёт: 1.
or_agent_absent() {
    local rc=0
    pane_tmux has-session -t "${TMUX_SESSION:-}" 2>/dev/null || rc=$?
    case "$rc" in
        0) ;;
        124|137) return 1 ;;
        *) return 0 ;;
    esac
    pane_cmd_set
    case "$PANE_CMD" in
        "?") return 1 ;;
        "мертва") return 0 ;;
    esac
    ! pane_is_agent "$PANE_CMD"
}

# Состояние эпизода по таблице §5: печатает решение — silent:<причина>, defer или told.
# $1 — момент первого просроченного сообщения, эпоха.
or_decide() {
    local first_at="$1" gate
    gate=$(pane_gate_owner)
    if [ -n "$gate" ]; then echo "silent:$gate"; return 0; fi
    if or_agent_absent; then echo defer; return 0; fi
    # Сторож сессий уже сказал о беде агента после сообщения — второй вести о том же не нужно.
    if [ -f "$OR_AGENT_TOLD" ] && [ "$(stat -c %Y "$OR_AGENT_TOLD")" -gt "$first_at" ]; then
        echo "silent:агента-нет"; return 0
    fi
    echo told
}

# Весть: $1=имя файла, $2=ЧЧ:ММ, $3=возраст первого, мин, $4=текст, $5=сколько просрочено.
or_text() {
    local name="$1" hm="$2" age="$3" text="$4" count="$5" taken="ещё не взяла его" last k head
    [ -e "$LOG_DIR/inbox/$name" ] || taken="взяла его"
    k=$(timeout 30 python3 "$OR_ROOT/scripts/stall-in-log.py" --работа --shift 2>/dev/null \
        | awk -F'\t' '$1 == "работа_мин" && $2 ~ /^[0-9]+$/ { print $2; exit }')
    last=""
    [ -z "$k" ] || last="; последний ответ модели $k мин назад"
    if [ "$count" -gt 1 ]; then
        head="Ваши сообщения ($count), первое $hm, ждут ответа $age мин."
    else
        head="Ваше сообщение $hm («${text:0:40}») ждёт ответа $age мин."
    fi
    printf '%s Смена %s%s. Сообщение не потеряно.' "$head" "$taken" "$last"
}

# Эпизод кончился (набор просроченных пуст): метка снимается, конец пишется в журнал.
or_close_episode() {
    local name began now="$1"
    [ -e "$OR_EPISODE" ] || return 0
    IFS=$'\t' read -r name _ began < "$OR_EPISODE"
    rm -f "$OR_EPISODE"
    or_event owner_reply.closed ok "$name" "$(( (now - ${began:-$now}) / 60 ))" "набор просроченных пуст"
}

# Одно решение на эпизод: метка пишется только когда решение принято.
or_start_episode() {  # $1=сейчас, $2=строки прибора
    local now="$1" rows="$2" name hms age text first_at count decision text_out rc=0
    IFS=$'\t' read -r name hms age text <<< "$(printf '%s\n' "$rows" | head -1)"
    name="$name.txt"
    count=$(printf '%s\n' "$rows" | wc -l)
    first_at=$(date -d "$(date -d "@$now" +%F) $hms" +%s)
    [ "$first_at" -le "$now" ] || first_at=$(( first_at - 86400 ))
    decision=$(or_decide "$first_at")
    case "$decision" in
        defer)
            say "страж ответа: агента в панели нет, решение по $name отложено до возврата агента"
            return 0 ;;
        silent:*)
            or_event owner_reply.silent skipped "$name" "$age" "${decision#silent:}" ;;
        told)
            text_out=$(or_text "$name" "${hms:0:5}" "$age" "$text" "$count")
            timeout 60 bash "$PROJECT_DIR/scripts/tg_send.sh" --не-досылать "$text_out" >/dev/null 2>&1 || rc=$?
            case "$rc" in
                0) or_event owner_reply.told ok "$name" "$age" "смена жива, ответа нет, дверь открыта" ;;
                3) decision="silent:лимит"
                   or_event owner_reply.silent skipped "$name" "$age" "лимит начался до отправки" ;;
                *) or_event owner_reply.send_failed error "$name" "$age" "дверь канала отдала код $rc"
                   say "страж ответа: весть о $name не ушла (код $rc), повтор на следующей минуте"
                   return 0 ;;
            esac ;;
    esac
    printf '%s\t%s\t%s\n' "$name" "$decision" "$now" > "$OR_EPISODE"
}

owner_reply_watch() {
    [ "${OWNER_REPLY_WATCH_ENABLED:-1}" = 1 ] || return 0
    local now out rows
    # shellcheck disable=SC1091
    source "$OR_ROOT/scripts/lib/pane.sh"
    now="${OWNER_REPLY_NOW:-$(date +%s)}"
    out=$(HARNESS_LOG_DIR="$LOG_DIR" timeout 30 python3 "$OR_ROOT/scripts/delivery-latency.py" --просрочено --на "@$now" 2>/dev/null)
    # Без вердикта (нет tg_out.jsonl, прибор упал) судить нечем: «нет журнала» не «нет ответов».
    if ! printf '%s\n' "$out" | tail -1 | grep -q '^ВЕРДИКТ просрочено:'; then
        say "страж ответа: прибор не дал вердикта, молчу: $(printf '%s' "$out" | tail -1 | cut -c1-160)"
        return 0
    fi
    rows=$(printf '%s\n' "$out" | grep -P '^[0-9]+\.[0-9]+\t' || true)
    if [ -z "$rows" ]; then or_close_episode "$now"; return 0; fi
    [ -e "$OR_EPISODE" ] || or_start_episode "$now" "$rows"
}
owner_reply_watch

[ "${DISPATCH_STALL_ENABLED:-0}" = 1 ] || exit 0

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
