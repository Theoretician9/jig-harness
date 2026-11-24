#!/usr/bin/env bash
#
# stack-watch.sh — свежесть стека: суточный замер и недельная сводка владельцу.
#
# Владелец 23.10.2026: «Автоматически регулярно проверяет все патчи, новые версии
# по, и рекомендует обновиться». Демон только зовёт scripts/stack_freshness.py и
# доставляет его решение; решать «слать / отложить / молчать» и собирать текст —
# дело Python (там это проверяется без отправки).
#
#   stack-watch.sh            суточный замер (cron 07:10) + разбор ответов; метка stack-watch;
#                             затем разбор обновлений (scripts/update_impact.py) и его отчёт владельцу —
#                             при UPDATE_IMPACT≠0; провал разбора метку замера не снимает
#   stack-watch.sh --догнать  только отправка (cron раз в час): ответы, затем сводка, если срок или лежит
#                             метка «отложена»; метку замера НЕ трогает — за жизнь демона отвечает суточный прогон;
#                             STACK_WEEKLY_SUMMARY=0 — сводку не шлёт: решения несёт отчёт разбора (один канал решений)
#   stack-watch.sh --selftest пробы Ф2
#
# Коды суточного прогона: 0 — отставания нет, 1 — есть что рекомендовать (метка стоит в обоих случаях),
# 2 — реестр битый, часть не мерится или Python упал (метки НЕТ: сторож меток закричит).
set -uo pipefail
export LC_ALL=C.UTF-8

# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/config.sh"
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/channel_send.sh"
konf_zagruzit

PROJECT_DIR="${PROJECT_DIR:?пуст PROJECT_DIR — не прочитан паспорт установки}"
LOG_DIR="${LOG_DIR:?пуст LOG_DIR}"
HEARTBEAT_DIR="${HEARTBEAT_DIR:?пуст HEARTBEAT_DIR}"
TOOL="$PROJECT_DIR/scripts/stack_freshness.py"
IMPACT="$PROJECT_DIR/scripts/update_impact.py"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

if [ "${1:-}" = "--selftest" ]; then
    exec python3 -B "$PROJECT_DIR/scripts/test_stack_watch.py"
fi
[ -f "$TOOL" ] || { say "нет $TOOL"; exit 2; }

# Отправка: --не-досылать, потому что tg_send без ключа при лимите отвечает 0 («придержано, уйдёт досылом»),
# а сводка, записанная доставленной раньше доставки, потеряла бы окно ответа владельца.
deliver() {   # $1=текст → 0 доставлено, 3 придержано лимитом, 4 отказ правил, иное — отказ
    local rc=0
    channel_send harness/demons/stack-watch.sh --не-досылать "$1" || rc=$?
    return "$rc"
}

send_notices() {   # stdin: вывод --ответы; каждую строку «отказ: …» отдельным сообщением
    local line rc
    while IFS= read -r line; do
        case "$line" in
            "отказ: "*)
                # «Сказано» пишется только после доставки: при лимите или сбое канала отказ уйдёт со следующим кругом.
                # Отказ правил (4) — тоже «сказано»: тот же текст следующим кругом получил бы тот же отказ.
                rc=0
                deliver "${line#отказ: }" || rc=$?
                case "$rc" in
                    0|"$CHANNEL_RULES_REFUSED") python3 -B "$TOOL" --сказано "${line#отказ: }" >/dev/null ;;
                    *) say "ответ владельцу не ушёл (код $rc) — уйдёт со следующим кругом" ;;
                esac ;;
            *) say "$line" ;;
        esac
    done
}

catch_up() {
    local out rc=0 first
    python3 -B "$TOOL" --ответы | send_notices
    if [ "${STACK_WEEKLY_SUMMARY:-1}" = 0 ]; then
        say "недельная сводка выключена ключом STACK_WEEKLY_SUMMARY=0"
        return 0
    fi
    out=$(python3 -B "$TOOL" --сводка) || rc=$?
    first="${out%%$'\n'*}"
    case "$first" in
        шлю:*)
            local week="${first#шлю: }" flags=() text
            week="${week%% *}"
            [[ "$first" == *вне_очереди* ]] && flags=(--вне-очереди)
            text="${out#*$'\n'}"
            rc=0
            # Сводка — документ: не пропущенная правилами канала текстом, она уходит вложением.
            channel_send_doc harness/demons/stack-watch.sh "$LOG_DIR/stack-сводка-недели-$week.md" \
                "Свежесть стека: сводка недели $week во вложении" "$text" --не-досылать || rc=$?
            case "$rc" in
                0) python3 -B "$TOOL" --отправлено --неделя "$week" "${flags[@]}" >/dev/null
                   say "сводка недели $week доставлена" ;;
                3) say "придержано лимитом — сводка отложена" ;;
                "$CHANNEL_RULES_REFUSED")
                   python3 -B "$TOOL" --отправлено --неделя "$week" "${flags[@]}" >/dev/null
                   say "сводка недели $week отвергнута правилами канала и вложением — не повторяю, отказ записан событием" ;;
                *) say "сводка не ушла (код $rc) — метка «отложена» осталась, дошлём в следующий круг" ;;
            esac ;;
        *) say "$first" ;;
    esac
    return 0
}

# Отчёт разбора уходит раз в сутки: его зовёт только суточный прогон. Придержанный лимитом не досылается —
# завтрашний разбор соберёт свежий (досылка с кнопками — Р4 спеки, `--нажатия`).
impact() {
    if [ "${UPDATE_IMPACT:-0}" = 0 ]; then
        say "разбор обновлений выключен ключом UPDATE_IMPACT=0"
        return 0
    fi
    local out rc=0 first
    python3 -B "$IMPACT" || rc=$?
    if [ "$rc" != 0 ]; then
        say "разбор обновлений не прошёл (код $rc) — отчёта нет"
        return 0
    fi
    out=$(python3 -B "$IMPACT" --отчёт) || rc=$?
    first="${out%%$'\n'*}"
    case "$first" in
        шлю:*)
            rc=0
            deliver "${out#*$'\n'}" || rc=$?
            case "$rc" in
                0) say "отчёт об обновлениях доставлен" ;;
                *) say "отчёт об обновлениях не ушёл (код $rc) — завтрашний разбор соберёт новый" ;;
            esac ;;
        *) say "разбор обновлений: $first" ;;
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
    impact
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
