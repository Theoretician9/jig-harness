#!/usr/bin/env bash
# Проба «код 4 канала — не повтор»: код 4 tg_send.sh (отказ правил
# переписки) — дефект текста, а не временный сбой. Звонящий пишет событие ОДИН
# раз и тот же текст не повторяет; документ уходит вложением.
#
# Что судится:
#   * scripts/lib/channel_send.sh — код 4: одно событие channel.rules_refused
#     с замечанием сторожа канала причиной; коды 0, 1 и 3 событий не пишут;
#     документ по коду 4 уходит вложением, по коду 1 — нет;
#   * product-health.sh целиком на стенде (мёртвый адрес здоровья): код 4 —
#     вложение, второй прогон дверь не зовёт; код 4 и для вложения — состояние
#     запомнено, повтора нет; код 1 — состояние не запомнено (повтор честен);
#   * у остальных звонящих с повтором (acceptance-watch, checks-stream,
#     stack-watch, dispatcher-stall-watch) отправка идёт через channel_send;
#     их поведение на коде 4 судят их собственные пробы, acceptance-watch —
#     разбором (его стенд — волна 3 сторожей с эталоном побайтно).
#
# Стенд: свой каталог, подставная дверь канала (код — из файла стенда, на 4
# пишет замечание в stderr, как настоящая), `env -i`.
# Двери для мутаций: CHANNEL_SEND_LIB, HARNESS_PRODUCT_HEALTH,
#   HARNESS_ACCEPTANCE_WATCH, HARNESS_CHECKS_STREAM, HARNESS_STACK_WATCH,
#   HARNESS_DISPATCHER_STALL_WATCH.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib/selftest_cases.sh"
DEMONS="$ROOT/harness/demons"
LIB="${CHANNEL_SEND_LIB:-$HERE/lib/channel_send.sh}"
PRODUCT_HEALTH="${HARNESS_PRODUCT_HEALTH:-$DEMONS/product-health.sh}"
declare -A CALLERS=(
    [acceptance-watch]="${HARNESS_ACCEPTANCE_WATCH:-$DEMONS/acceptance-watch.sh}"
    [checks-stream]="${HARNESS_CHECKS_STREAM:-$DEMONS/checks-stream.sh}"
    [stack-watch]="${HARNESS_STACK_WATCH:-$DEMONS/stack-watch.sh}"
    [dispatcher-stall-watch]="${HARNESS_DISPATCHER_STALL_WATCH:-$DEMONS/dispatcher-stall-watch.sh}"
)
JOURNAL_NAME="канал-отказы-правил.jsonl"
REMARK="длина 5000 знаков больше жёсткого потолка 1400"

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

# Подставная дверь: ряд доводов на вызов; код текста — rc-text, вложения — rc-file.
door() {  # $1 = каталог проекта стенда
    mkdir -p "$1/scripts"
    cat > "$1/scripts/tg_send.sh" <<DOOR
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$1/calls"
rc=\$(cat "$1/rc-text")
[ "\${1:-}" = --file ] && rc=\$(cat "$1/rc-file")
if [ "\$rc" = 4 ]; then
    echo "$REMARK" >&2
    echo "tg_send: ОТКАЗ — сообщение нарушает правила переписки (владелец 11.09.2026)." >&2
    echo "  Перепиши: итог и числа в канал." >&2
fi
exit "\$rc"
DOOR
    chmod +x "$1/scripts/tg_send.sh"
}
lines() { if [ -f "$1" ]; then wc -l < "$1" | tr -d ' '; else echo 0; fi; }
event_field() {  # $1 = журнал, $2 = выражение jq
    jq -r "$2" "$1" 2>/dev/null | head -1
}

# ── библиотека ──────────────────────────────────────────────────────────────
lib_stand() {  # $1 = имя, $2 = код текста, $3 = код вложения → каталог
    local d="$T/lib-$1"
    mkdir -p "$d/log"; door "$d"
    echo "$2" > "$d/rc-text"; echo "$3" > "$d/rc-file"
    printf '%s' "$d"
}
lib_run() {  # $1 = каталог, $2 = функция, остальное — её доводы; печатает код
    local d="$1"; shift
    env -i PATH="$PATH" HOME="$d" PROJECT_DIR="$d" LOG_DIR="$d/log" \
        bash -c 'source "$1" || exit 99; shift; "$@"; echo $?' _ "$LIB" "$@" 2>/dev/null | tail -1
}

d=$(lib_stand r4 4 0)
case_check "Б1: код 4 — channel_send возвращает 4" 4 "$(lib_run "$d" channel_send demon/x.sh "текст")"
case_check "Б1: дверь звана один раз" 1 "$(lines "$d/calls")"
case_check "Б1: одно событие отказа правил" 1 "$(lines "$d/log/$JOURNAL_NAME")"
case_check "Б1: событие channel.rules_refused с исходом error от звонящего" "channel.rules_refused error demon/x.sh" \
    "$(event_field "$d/log/$JOURNAL_NAME" '"\(.event) \(.outcome) \(.src)"')"
case_check "Б1: причина — замечание сторожа канала" да \
    "$(event_field "$d/log/$JOURNAL_NAME" '.data.reason' | grep -qF "$REMARK" && echo да || echo нет)"
case_check "Б1: общий совет двери в причину не попал" нет \
    "$(event_field "$d/log/$JOURNAL_NAME" '.data.reason' | grep -qF 'ОТКАЗ' && echo да || echo нет)"

for rc in 0 1 3; do
    d=$(lib_stand "r$rc" "$rc" 0)
    case_check "Б2: код $rc — channel_send возвращает его" "$rc" "$(lib_run "$d" channel_send demon/x.sh "текст")"
    case_check "Б2: код $rc — события отказа нет" 0 "$(lines "$d/log/$JOURNAL_NAME")"
done

d=$(lib_stand doc4 4 0)
case_check "Б3: документ, текст отвергнут — вложение дошло (0)" 0 \
    "$(lib_run "$d" channel_send_doc demon/x.sh "$d/doc.md" "подпись" "длинный документ" --task x:y)"
case_check "Б3: два вызова двери — текст и вложение" 2 "$(lines "$d/calls")"
case_check "Б3: первый вызов — текст и ключи в прежнем порядке" "длинный документ --task x:y" "$(sed -n 1p "$d/calls")"
case_check "Б3: второй вызов — вложение с подписью и ключами" "--file $d/doc.md --caption подпись --task x:y" \
    "$(sed -n 2p "$d/calls")"
case_check "Б3: в файле вложения — текст" "длинный документ" "$(cat "$d/doc.md" 2>/dev/null)"

d=$(lib_stand doc1 1 0)
case_check "Б4: документ, сбой канала (1) — код 1" 1 \
    "$(lib_run "$d" channel_send_doc demon/x.sh "$d/doc.md" "подпись" "документ")"
case_check "Б4: сбой канала — вложением не шлём" 1 "$(lines "$d/calls")"

d=$(lib_stand doc44 4 4)
case_check "Б5: отказ и текста, и вложения — код 4" 4 \
    "$(lib_run "$d" channel_send_doc demon/x.sh "$d/doc.md" "подпись" "документ")"
case_check "Б5: каждый отказ — своё событие" 2 "$(lines "$d/log/$JOURNAL_NAME")"

# ── product-health целиком ──────────────────────────────────────────────────
ph_stand() {  # $1 = имя, $2 = код текста, $3 = код вложения → каталог
    local d="$T/ph-$1"
    mkdir -p "$d/log" "$d/hb"; door "$d"
    echo "$2" > "$d/rc-text"; echo "$3" > "$d/rc-file"
    mkdir -p "$d/tree/harness/demons" "$d/tree/scripts/lib"
    cp "$PRODUCT_HEALTH" "$d/tree/harness/demons/product-health.sh"
    cp "$HERE/lib/config.sh" "$HERE/lib/config.py" "$HERE/lib/events.sh" "$HERE/lib/events.py" "$HERE/lib/patterns.py" \
        "$d/tree/scripts/lib/"
    [ ! -f "$LIB" ] || cp "$LIB" "$d/tree/scripts/lib/channel_send.sh"
    cat > "$d/install.conf" <<CONF
PROJECT_NAME="стенд"
PROJECT_DIR="$d"
AGENT_USER="$(id -un)"
SECRETS_DIR="$d"
TG_TOKEN_FILE="$d/no-token"
TG_CHAT_ID=""
HEARTBEAT_DIR="$d/hb"
LOG_DIR="$d/log"
CONF
    # Порт 9 (discard) не слушает никто: служба «не отвечает» без своего сервера.
    printf 'PRODUCT_HEALTH_URL="http://127.0.0.1:9/health"\n' > "$d/harness.conf"
    printf '%s' "$d"
}
ph_run() {  # $1 = каталог; печатает код выхода
    local d="$1" rc=0
    env -i PATH="$PATH" HOME="$d" HARNESS_INSTALL_CONF="$d/install.conf" HARNESS_CONF="$d/harness.conf" \
        timeout 60 bash "$d/tree/harness/demons/product-health.sh" >> "$d/run.out" 2>&1 < /dev/null || rc=$?
    echo "$rc"
}

d=$(ph_stand r4 4 0)
case_check "П1: текст отвергнут, вложение дошло — выход 0" 0 "$(ph_run "$d")"
case_check "П1: дверь звана дважды (текст, вложение)" 2 "$(lines "$d/calls")"
case_check "П1: второй вызов — вложение" да "$(sed -n 2p "$d/calls" | grep -q '^--file ' && echo да || echo нет)"
case_check "П1: состояние запомнено (dead)" dead "$(cat "$d/log/product-health.state" 2>/dev/null)"
case_check "П1: событие отказа правил от product-health" "harness/demons/product-health.sh" \
    "$(event_field "$d/log/$JOURNAL_NAME" '.src')"
before=$(lines "$d/calls"); ph_run "$d" >/dev/null
case_check "П2: второй прогон той же беды дверь не зовёт" "$before" "$(lines "$d/calls")"

d=$(ph_stand r44 4 4)
case_check "П3: отвергнуты и текст, и вложение — выход 1 (владелец не извещён)" 1 "$(ph_run "$d")"
case_check "П3: состояние всё же запомнено — тот же текст не повторяется" dead "$(cat "$d/log/product-health.state" 2>/dev/null)"
before=$(lines "$d/calls"); ph_run "$d" >/dev/null
case_check "П3: второй прогон дверь не зовёт" "2 2" "$before $(lines "$d/calls")"
case_check "П3: отказ правил назван в журнале сторожа" да "$(grep -q 'правилами канала' "$d/run.out" && echo да || echo нет)"

d=$(ph_stand r1 1 0)
case_check "П4: сбой канала (1) — выход 1" 1 "$(ph_run "$d")"
case_check "П4: сбой канала — состояние не запомнено" нет "$( [ -e "$d/log/product-health.state" ] && echo есть || echo нет)"
case_check "П4: сбой канала — вложением не шлём, события отказа нет" "1 0" "$(lines "$d/calls") $(lines "$d/log/$JOURNAL_NAME")"
ph_run "$d" >/dev/null
case_check "П4: следующий прогон повторяет (честный повтор сбоя)" 2 "$(lines "$d/calls")"

# ── остальные звонящие с повтором ───────────────────────────────────────────
# Разбор предмета: в каждом файле — подключение библиотеки и вызов channel_send.
for name in acceptance-watch checks-stream stack-watch dispatcher-stall-watch; do
    f="${CALLERS[$name]}"
    case_check "С: $name подключает channel_send.sh" да \
        "$(grep -qE '^[^#]*source .*scripts/lib/channel_send\.sh' "$f" && echo да || echo нет)"
    case_check "С: $name шлёт через channel_send" да \
        "$(grep -qE '^[^#]*\bchannel_send(_doc)? ' "$f" && echo да || echo нет)"
done
# Код 4 у приёмки обязан запомнить состав очереди, как и доставка.
case_check "С: acceptance-watch на коде 4 запоминает состав очереди" да \
    "$(awk '/channel_send .*Приёмка: /,/esac/' "${CALLERS[acceptance-watch]}" \
        | grep -A1 'CHANNEL_RULES_REFUSED' | grep -q 'SOSTAV.*SLED' && echo да || echo нет)"

echo
if [ "$CASES_FAILED" = 0 ]; then
    echo "ИТОГ: зелёный — путей $CASES_PATHS"
    exit 0
fi
echo "ИТОГ: КРАСНЫЙ — провалено $CASES_FAILED из $CASES_PATHS"
exit 1
