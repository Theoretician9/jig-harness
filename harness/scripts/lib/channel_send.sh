#!/usr/bin/env bash
# Отправка владельцу для демонов, которые недоставленное повторяют следующим кругом.
#
# Код 4 tg_send.sh — отказ правил переписки (длина, самокомментарий): канал
# исправен, текст не годится, и тот же текст на следующем круге получит тот же
# отказ. Демоны принимали его за временный сбой и слали непроходной текст раз в
# минуту. Здесь отказ пишется событием ОДИН раз — замечания сторожа канала идут
# причиной, чтобы `events.py refusals` назвал и звонящего, и что поправить, — а
# звонящий по коду 4 закрывает весть, а не откладывает её.
#
#   source scripts/lib/channel_send.sh
#   channel_send <src> <доводы tg_send.sh…>                   → код tg_send.sh
#   channel_send_doc <src> <файл> <подпись> <текст> [ключи…]  → код последней отправки
#
# Нужны PROJECT_DIR и LOG_DIR звонящего. CHANNEL_SEND_TIMEOUT — предел вызова двери, с.

# shellcheck source=events.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/events.sh"

CHANNEL_RULES_REFUSED=4

channel_send() {
    local src="$1" err rc=0 reason
    shift
    err=$(mktemp)
    ${CHANNEL_SEND_TIMEOUT:+timeout "$CHANNEL_SEND_TIMEOUT"} \
        bash "$PROJECT_DIR/scripts/tg_send.sh" "$@" >/dev/null 2>"$err" || rc=$?
    if [ "$rc" = "$CHANNEL_RULES_REFUSED" ]; then
        # Замечания сторожа стоят до строки «tg_send: ОТКАЗ»; дальше — общий совет двери.
        reason=$(sed '/^tg_send: ОТКАЗ/,$d' "$err" | tr '\n' ' ' | cut -c1-300)
        HARNESS_LOG_DIR="$LOG_DIR" harness_event канал-отказы-правил "$src" channel.rules_refused error \
            "reason=${reason:-сторож канала не назвал причину}"
    fi
    rm -f "$err"
    return "$rc"
}

# Документ, которому мал потолок сообщения, уходит вложением через ту же дверь:
# обходить отказ правил нельзя, а подпись короткая и своя.
channel_send_doc() {
    local src="$1" doc="$2" caption="$3" text="$4" rc=0
    shift 4
    channel_send "$src" "$text" "$@" || rc=$?
    [ "$rc" = "$CHANNEL_RULES_REFUSED" ] || return "$rc"
    printf '%s\n' "$text" > "$doc"
    rc=0
    channel_send "$src" --file "$doc" --caption "$caption" "$@" || rc=$?
    return "$rc"
}
