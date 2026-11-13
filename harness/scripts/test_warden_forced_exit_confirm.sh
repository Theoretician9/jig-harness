#!/usr/bin/env bash
# Проба: принудительная ротация отвечает на диалог выхода сама (карточка
# hr.exit-without-human).
#
# Авария 13.11.2026, 01:38: `--rotate-now`, сторож подал «/exit», Claude показал
# «Background work is running … Enter to confirm» — и сторож восемь минут не
# нажал ничего (ни rotate_exit_dialog, ни rotate_exit_sent в rotation.jsonl);
# владелец нажал «1» сам. Принудительный путь подаёт «/exit» ДО цикла ожидания,
# а цикл считал выход не поданным: ветка диалога спала, первая клавиша ушла бы
# только после ROTATE_FINISH_TIMEOUT_SEC (~930 с).
#
# Судится rotate_full ЦЕЛИКОМ, взятый из живого файла сторожа (дверь мутаций —
# HARNESS_SESSION_WARDEN), с соседними функциями решения. Подменены только руки:
# tmux, команда панели, клавиши и часы (sleep двигает виртуальное время —
# проба идёт секунды, а судит минуты).
#   1. принудительный путь: первая клавиша в диалог ≤60 с, rotate_exit_dialog
#      в журнале, панель стала оболочкой;
#   2. обычный путь (FORCE_ROTATE=0): «/exit» подаётся только после конца хода,
#      диалог подтверждён, rotate_exit_sent записан — не сломан;
#   3. принудительный путь при идущих помощниках: диалог отменяется Escape,
#      «1»/Enter не уходят, пока помощники идут; после них выход доведён.
#
# Имена переменных ЛАТИНИЦЕЙ: bash не берёт кириллические идентификаторы.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

WARDEN="${HARNESS_SESSION_WARDEN:-$HERE/../harness/demons/session-warden.sh}"
# В пакете демоны лежат рядом со scripts (harness/demons), в проекте — под harness/.
[ -f "$WARDEN" ] || WARDEN="$HERE/../demons/session-warden.sh"
[ -f "$WARDEN" ] || { echo "ПЛОХО: сторожа нет по пути раскладки — проверять нечего"; exit 1; }
STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT

# Дословный экран владельца 13.11.2026, 01:38.
SCREEN_FILE="$STAND/screen.txt"
cat > "$SCREEN_FILE" <<'EOF'
❯ /exit
────────────────
 Background work is running
 The following will stop when you exit:

 shell · P=/home/user/starter/STARTER-PACKAGE; python3 - …
 shell · tail -f -n 0 --pid=1 /var/log/harness/lc29-probe.…

 ❯ 1. Exit and stop tasks
   2. Move to background and exit
   3. Stay

 Enter to confirm · Esc to cancel
EOF

# ── функции из живого файла ─────────────────────────────────────────────────
FUNCS="$STAND/funcs.sh"
grep -E '^(PICK_MARK|RE_EXIT_ITEM|RE_PICKED_ITEM|RE_BACKGROUND_ITEM)=' "$WARDEN" > "$FUNCS"
case_check "образцы пунктов диалога вынуты из файла сторожа" "4" "$(wc -l < "$FUNCS")"
missing=""
for f in rotate_full выход_из_ротации смена_состоялась диалог_выхода_жив диалог_выхода_в \
         подсвеченный_пункт_в клавиши_подтверждения номер_пункта_выхода номер_пункта_фоном \
         agent_rotation_prompt; do
    text=$(sed -n "/^$f() {/,/^}/p" "$WARDEN")
    [ -n "$text" ] || missing="$missing $f"
    printf '%s\n' "$text" >> "$FUNCS"
done
case_check "функции ротации вынуты из файла сторожа" "" "${missing# }"

# ── руки стенда: панель, клавиши, часы ──────────────────────────────────────
STUBS="$STAND/stubs.sh"
cat > "$STUBS" <<'EOF'
VT=0; SHOWN=0; PANE=node
SCREEN_TEXT=$(cat "$SCREEN_FILE")
ev() { printf '%s %s\n' "$((VT / 1000))" "$*" >> "$EV"; }
sleep() {  # виртуальные часы; потолок — чтобы мутация не крутила пробу вечно
    VT=$(( VT + $(awk -v s="$1" 'BEGIN { printf "%d", s * 1000 }') ))
    [ "$VT" -lt 4000000 ] || { ev "ПРЕДЕЛ"; exit 3; }
}
helpers_now() { [ "$VT" -ge "${HELPERS_FROM_MS:-1}" ] && [ "$VT" -lt "${HELPERS_UNTIL_MS:-0}" ]; }
tmux() {
    case "$1" in
        has-session) return 0 ;;
        capture-pane) [ "$SHOWN" = 1 ] && printf '%s\n' "$SCREEN_TEXT"; return 0 ;;
    esac
    return 0
}
pane_cmd() { printf '%s\n' "$PANE"; }
pane_is_agent() { case "$1" in claude|node) return 0 ;; *) return 1 ;; esac; }
pane_command() {
    local mode="$1"; shift
    case "$mode" in
        text) ev "текст $1"; [ "$1" = /exit ] && [ "$PANE" = node ] && SHOWN=1; return 0 ;;
        keys)
            ev "клавиша $1"
            [ "$SHOWN" = 1 ] || return 0
            case "$1" in
                1|Enter) SHOWN=0; PANE=bash ;;
                Escape) SHOWN=0 ;;
            esac
            return 0 ;;
    esac
    return 1
}
agent_prompt() { ev "поручение"; return 0; }
say() { printf '%s сказано: %s\n' "$((VT / 1000))" "$*" >> "$EV.say"; }
jlog() { ev "журнал $1"; }
tg_send() { ev "канал"; return 0; }
дверь_ротации() { return 0; }
rotation_flag_put() { :; }
сохранить_снимок() { echo снимок; }
свежий_id() { printf '%s\n' "$SESSION_ID"; }
простой_панели() { echo 600; }
ход_завершён_после() { if [ "$VT" -ge "${TURN_DONE_MS:-999999999}" ]; then echo 1; else echo 0; fi; }
фоновые_задачи_живы() { return 1; }
rotation_helpers_gate() {
    if helpers_now; then JUDGE_HELPERS=1; ROTATION_GATE_WORD="ждать: идут помощники (1)"; return 1; fi
    JUDGE_HELPERS=0; ROTATION_GATE_WORD="можно: помощников нет"; return 0
}
работа_на_паузе() { return 1; }
reboot_needed() { echo "0 -"; }
reboot_switch() { echo 0; }
reboot_last_failed() { echo 0; }
reboot_window_verdict() { echo "нет: стенд"; return 1; }
reboot_for_rotation() { return 1; }
start_agent() { ev "подъём преемника (панель $PANE)"; return 1; }
подхватить_работу() { return 0; }
EOF

run() {  # $1=имя прогона, остальное — переменные окружения прогона
    local name="$1"; shift
    EV="$STAND/$name.events"; : > "$EV"
    env "$@" EV="$EV" SCREEN_FILE="$SCREEN_FILE" FUNCS="$FUNCS" STUBS="$STUBS" \
        TMUX_SESSION=стенд SESSION_ID=s1 REASON=проба LOG_DIR="$STAND" HARNESS_SCRIPTS="$STAND" \
        ROTATE_FINISH_TIMEOUT_SEC=900 PANE_ENTER_DELAY=0.7 AGENT_START_TIMEOUT_SEC=60 \
        ROTATION_WAITED_SEC=0 JUDGE_HELPERS=0 AGENT_USER=agent \
        bash -c 'set -uo pipefail; source "$FUNCS"; source "$STUBS"; rc=0; rotate_full || rc=$?; ev "конец $rc"' \
        > "$STAND/$name.out" 2>&1
}
first_at() { grep -m1 -E "^[0-9]+ $2" "$STAND/$1.events" | cut -d' ' -f1; }
count() { grep -cE "^[0-9]+ $2" "$STAND/$1.events" || true; }
yes_no() { if "$@"; then echo да; else echo нет; fi; }
first_key() { grep -m1 -E '^[0-9]+ клавиша' "$STAND/$1.events"; }

echo "── 1. БОЛЬНОЙ СЛУЧАЙ 13.11, 01:38: --rotate-now, «/exit» показал диалог ──"
run forced FORCE_ROTATE=1
key=$(first_at forced 'клавиша')
case_check "«/exit» подан сразу (0 с)" "0" "$(first_at forced 'текст /exit')"
case_check "первая клавиша в диалог — не позже 60 с (было ~930)" "да" \
    "$([ -n "$key" ] && [ "$key" -le 60 ] && echo да || echo "нет: ${key:-ни одной}")"
case_check "rotate_exit_dialog в журнале" "да" "$(yes_no grep -q 'журнал rotate_exit_dialog' "$STAND/forced.events")"
case_check "первая клавиша подтверждает выход («1» или Enter)" "да" \
    "$(first_key forced | grep -qE 'клавиша (1|Enter)$' && echo да || echo нет)"
case_check "панель стала оболочкой — дальше подъём преемника" "да" \
    "$(yes_no grep -q 'подъём преемника (панель bash)' "$STAND/forced.events")"
case_check "«/exit» подан один раз" "1" "$(count forced 'текст /exit')"

echo "── 2. КОНТРОЛЬ: обычная ротация (FORCE_ROTATE=0) не сломана ──"
run control FORCE_ROTATE=0 TURN_DONE_MS=90000
case_check "сперва поручение, не «/exit»" "0" "$(first_at control 'поручение')"
ex=$(first_at control 'текст /exit')
case_check "«/exit» — только после конца хода (≥90 с)" "да" \
    "$([ -n "$ex" ] && [ "$ex" -ge 90 ] && echo да || echo "нет: ${ex:-не подан}")"
case_check "rotate_exit_sent в журнале" "да" "$(yes_no grep -q 'журнал rotate_exit_sent' "$STAND/control.events")"
case_check "диалог подтверждён: rotate_exit_dialog в журнале" "да" \
    "$(yes_no grep -q 'журнал rotate_exit_dialog' "$STAND/control.events")"
case_check "панель стала оболочкой" "да" "$(yes_no grep -q 'подъём преемника (панель bash)' "$STAND/control.events")"

echo "── 3. принудительный путь, помощники пошли после «/exit»: Escape, а не «1» ──"
run helpers FORCE_ROTATE=1 HELPERS_FROM_MS=1 HELPERS_UNTIL_MS=100000
read -r esc_at _ esc_key <<< "$(first_key helpers)"
case_check "первая клавиша — Escape, и не позже 60 с" "да" \
    "$([ "${esc_key:-}" = Escape ] && [ "${esc_at:-999}" -le 60 ] && echo да || echo "нет: ${esc_at:-} ${esc_key:-ни одной}")"
case_check "ни «1», ни Enter, пока помощники идут (<100 с)" "0" \
    "$(awk '$2 == "клавиша" && ($3 == "1" || $3 == "Enter") && $1 < 100' "$STAND/helpers.events" | wc -l)"
case_check "после помощников выход доведён: панель стала оболочкой" "да" \
    "$(yes_no grep -q 'подъём преемника (панель bash)' "$STAND/helpers.events")"

if [ "$CASES_FAILED" -gt 0 ]; then
    for n in forced control helpers; do
        [ -f "$STAND/$n.events" ] || continue
        echo "── события прогона $n ──"; cat "$STAND/$n.events"; tail -5 "$STAND/$n.out"
    done
fi
printf '\nПРИНУДИТЕЛЬНЫЙ ВЫХОД ОТВЕЧАЕТ НА ДИАЛОГ: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
