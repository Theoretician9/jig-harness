#!/usr/bin/env bash
#
# stack-watch.sh — свежесть стека: суточный замер и недельная сводка владельцу.
#
# Владелец 23.10.2026: «Автоматически регулярно проверяет все патчи, новые версии
# по, и рекомендует обновиться». Демон только зовёт scripts/stack_freshness.py и
# доставляет его решение; решать «слать / отложить / молчать» и собирать текст —
# дело Python (там это проверяется без отправки).
#
#   stack-watch.sh            суточный замер (cron 07:10) + разбор ответов; метка stack-watch
#   stack-watch.sh --догнать  только отправка (cron раз в час): ответы, затем сводка, если срок или лежит
#                             метка «отложена»; метку замера НЕ трогает — за жизнь демона отвечает суточный прогон
#   stack-watch.sh --selftest пробы Ф2
#
# Коды суточного прогона: 0 — отставания нет, 1 — есть что рекомендовать (метка стоит в обоих случаях),
# 2 — реестр битый, часть не мерится или Python упал (метки НЕТ: сторож меток закричит).
set -uo pipefail
export LC_ALL=C.UTF-8

# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/config.sh"
konf_zagruzit

PROJECT_DIR="${PROJECT_DIR:?пуст PROJECT_DIR — не прочитан паспорт установки}"
LOG_DIR="${LOG_DIR:?пуст LOG_DIR}"
HEARTBEAT_DIR="${HEARTBEAT_DIR:?пуст HEARTBEAT_DIR}"
TOOL="$PROJECT_DIR/scripts/stack_freshness.py"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

if [ "${1:-}" = "--selftest" ]; then
    exec python3 -B "$PROJECT_DIR/scripts/test_stack_watch.py"
fi
[ -f "$TOOL" ] || { say "нет $TOOL"; exit 2; }

# Отправка: --не-досылать, потому что tg_send без ключа при лимите отвечает 0 («придержано, уйдёт досылом»),
# а сводка, записанная доставленной раньше доставки, потеряла бы окно ответа владельца.
deliver() {   # $1=текст → 0 доставлено, 3 придержано лимитом, иное — отказ
    local rc=0
    "$PROJECT_DIR/scripts/tg_send.sh" --не-досылать "$1" >/dev/null || rc=$?
    return "$rc"
}

send_notices() {   # stdin: вывод --ответы; каждую строку «отказ: …» отдельным сообщением
    local line
    while IFS= read -r line; do
        case "$line" in
            "отказ: "*)
                # «Сказано» пишется только после доставки: при лимите или отказе канала отказ уйдёт со следующим кругом.
                if deliver "${line#отказ: }"; then
                    python3 -B "$TOOL" --сказано "${line#отказ: }" >/dev/null
                else
                    say "ответ владельцу не ушёл (код $?) — уйдёт со следующим кругом"
                fi ;;
            *) say "$line" ;;
        esac
    done
}

catch_up() {
    local out rc=0 first
    python3 -B "$TOOL" --ответы | send_notices
    out=$(python3 -B "$TOOL" --сводка) || rc=$?
    first="${out%%$'\n'*}"
    case "$first" in
        шлю:*)
            local week="${first#шлю: }" flags=() text
            week="${week%% *}"
            [[ "$first" == *вне_очереди* ]] && flags=(--вне-очереди)
            text="${out#*$'\n'}"
            rc=0
            deliver "$text" || rc=$?
            case "$rc" in
                0) python3 -B "$TOOL" --отправлено --неделя "$week" "${flags[@]}" >/dev/null
                   say "сводка недели $week доставлена" ;;
                3) say "придержано лимитом — сводка отложена" ;;
                *) say "сводка не ушла (код $rc) — метка «отложена» осталась, дошлём в следующий круг" ;;
            esac ;;
        *) say "$first" ;;
    esac
    return 0
}

daily() {
    local rc=0 recommend
    python3 -B "$TOOL" --замер || rc=$?
    python3 -B "$TOOL" --ответы | send_notices
    # По договору --замер отвечает 0 или 2; всё иное (1 от необработанного исключения) — падение, а не успех.
    if [ "$rc" != 0 ]; then
        say "замер не прошёл (код $rc: реестр битый, часть не мерится или Python упал) — метку не ставлю"
        return 2
    fi
    touch "$HEARTBEAT_DIR/stack-watch"
    recommend=$(jq -r '.["рекомендовать"] // 0' "$LOG_DIR/stack/замер.json" 2>/dev/null) || recommend=0
    [ "${recommend:-0}" -gt 0 ] && return 1
    return 0
}

mkdir -p "$HEARTBEAT_DIR"
case "${1:-}" in
    --догнать) catch_up ;;
    "") daily ;;
    *) say "неизвестный ключ: $1"; exit 2 ;;
esac
