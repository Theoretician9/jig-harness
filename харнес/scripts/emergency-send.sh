#!/usr/bin/env bash
#
# emergency-send.sh — аварийный канал МИМО харнеса.
#
# Откуда взят: написан для стартового пакета, шаблона в UNIFIED нет; норма —
# 02-КАРТА («Аварийный канал мимо харнеса»): когда лёг сам харнес (диспетчер,
# tg_send, jq, диск с логами), владельцу всё равно надо сказать об этом.
# Поэтому здесь НИЧЕГО, кроме coreutils и curl: ни jq (успех проверяется
# grep-ом по "ok":true), ни $LOG_DIR (может лежать вместе с диском), ни
# tg_send.sh (он и есть то, что могло сломаться).
#
# Первая строка сообщения называет обрыв — владелец с телефона сразу видит,
# ЧТО умерло, без чтения хвоста.
#
# Запуск: emergency-send.sh "<что оборвалось>" ["подробности..."]
# Код возврата: 0 — Telegram подтвердил, 1 — нет (тогда хоть stderr).
# Чем доказывается на живом: периодическая cron-проба (02-КАРТА) — раз в
# неделю тестовая отправка; возраст последней успешной виден в чате.
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "укажи, что оборвалось: emergency-send.sh \"<обрыв>\" [\"детали\"]" >&2
    exit 1
fi

CONF="${HARNESS_INSTALL_CONF:-/etc/harness/install.conf}"

# НЕ source, а мини-парсер: битый install.conf (незакрытая кавычка, случайная
# команда) при source убил бы и аварийный канал — последнее, что имеет право
# умереть от чужой ошибки. Берём только нужные ключи, конфиг не исполняется.
conf_get() {  # KEY → значение из KEY="value" / KEY=value (первая строка)
    sed -n "s/^[[:space:]]*$1=[\"']\{0,1\}\([^\"']*\).*/\1/p" "$CONF" 2>/dev/null | head -1
}
if [ -r "$CONF" ]; then
    TG_CHAT_ID="${TG_CHAT_ID:-$(conf_get TG_CHAT_ID)}"
    TG_TOKEN_FILE="${TG_TOKEN_FILE:-$(conf_get TG_TOKEN_FILE)}"
    # Единственная подстановка, которая встречается в паспорте установки:
    # TG_TOKEN_FILE="$SECRETS_DIR/tg_bot_token".
    case "${TG_TOKEN_FILE:-}" in
        *'$SECRETS_DIR'*|*'${SECRETS_DIR}'*)
            SECRETS_DIR_VAL=$(conf_get SECRETS_DIR)
            TG_TOKEN_FILE=$(printf '%s' "$TG_TOKEN_FILE" \
                | sed "s|\${SECRETS_DIR}|$SECRETS_DIR_VAL|g; s|\$SECRETS_DIR|$SECRETS_DIR_VAL|g")
            ;;
    esac
fi
if [ -z "${TG_CHAT_ID:-}" ] || [ -z "${TG_TOKEN_FILE:-}" ] || [ ! -s "${TG_TOKEN_FILE:-/nonexistent}" ]; then
    echo "emergency-send: канал не настроен (TG_CHAT_ID/TG_TOKEN_FILE в $CONF) — сообщить владельцу НЕЧЕМ" >&2
    exit 1
fi
TOKEN=$(tr -d '[:space:]' < "$TG_TOKEN_FILE")

# Первая строка — обрыв; дальше — хост, время, детали. Всё, что нужно, чтобы
# начать чинить, даже если это единственное сообщение, которое дойдёт.
MSG="АВАРИЯ ХАРНЕСА: $1
хост: $(hostname), время: $(date '+%Y-%m-%d %H:%M:%S')
${2:-}"

# URL с токеном — через stdin (curl -K -), не в argv: argv любого процесса
# виден в ps, аварийный канал не имеет права светить токен.
RESP=$(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TOKEN" \
    | curl -sS --max-time 30 -K - -X POST \
    --data-urlencode "chat_id=$TG_CHAT_ID" \
    --data-urlencode "text=$MSG" 2>&1) || {
    echo "emergency-send: curl не дошёл до Telegram: $(printf '%.200s' "$RESP")" >&2
    exit 1
}

# Без jq: аварийный канал не имеет права зависеть от того, что могло умереть
# вместе с харнесом. Точной проверки message_id здесь нет — и это названная
# цена, а не забытая: строгая проверка живёт в tg_send.sh.
case "$RESP" in
    *'"ok":true'*)
        echo "emergency-send: доставлено"
        exit 0
        ;;
    *)
        echo "emergency-send: Telegram НЕ подтвердил: $(printf '%.200s' "$RESP")" >&2
        exit 1
        ;;
esac
