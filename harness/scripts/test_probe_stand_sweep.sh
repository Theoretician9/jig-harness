#!/usr/bin/env bash
#
# test_probe_stand_sweep.sh — проба уборки стендов у проб, поднимающих настоящий
# tg-dispatcher.sh (карточка hr.dispatcher-probes-leak).
#
# ЧТО ОХРАНЯЕТ. Диспетчер стенда запускается через setsid — в своей сессии, и
# сигнал, убивший пробу, до него не доходит. Пробу, убитую KILL-ом (timeout -k у
# прогона мутаций и ворот), EXIT-ловушка не убирает, а обработчик голосовых живёт
# в своей сессии и переживает убийство группы диспетчера: 04.11 нашлось 13
# сиротских диспетчеров и 29 процессов python со стендов.
#
# Пути (на каждую пробу; печатается их число):
#   1. выход: проба доходит до конца — код 0, процессов стенда после неё нет;
#   2. TERM посреди случая «диспетчер» — код 143, процессов стенда нет;
#   3. KILL посреди случая «диспетчер» оставляет сирот, следующий прогон той же
#      пробы их убирает;
#   4. стенд живого хозяина (параллельный прогон) следующий прогон не трогает.
#
#   bash scripts/test_probe_stand_sweep.sh [--случай пауза|лок|голос|метки|след]
# Метки и след уборку берут из scripts/lib/stand_sweep.sh: их пути судят и её.
# Код возврата: 0 — зелёная, 1 — красная. Последняя строка — итог с числом путей.
# Пробы берутся от места самой пробы (../ от scripts/): мутации идут «из-копии».
set -uo pipefail
export LC_ALL=C.UTF-8

KOREN="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"

# имя случая | файл пробы | префикс стенда | быстрый случай пробы (без диспетчера)
SUBJECTS=(
    "пауза|test_pane_door_pause.sh|proba-pane-door-|дверь"
    "лок|test_pane_lock_instant.sh|proba-pane-lock-|код"
    "голос|test_voice_worker.sh|proba-voice-worker-|код"
    "метки|test_delivery_marks.sh|proba-delivery-marks-|код"
    "след|test_delivery_trace.sh|proba-delivery-trace-|код"
)

ONLY=""
if [ "${1:-}" = "--случай" ]; then
    ONLY="${2:-}"
    case "$ONLY" in пауза|лок|голос|метки|след) ;; *) echo "ПРОБА: неизвестный случай «$ONLY» (пауза|лок|голос|метки|след)"; exit 1 ;; esac
elif [ -n "${1:-}" ]; then
    echo "ПРОБА: неизвестный ключ «$1»"; exit 1
fi

source "$KOREN/scripts/lib/selftest_cases.sh"

W=$(mktemp -d /tmp/proba-stand-sweep-XXXXXX)
CHILDREN=()   # прогоны проб и приманки — убрать при любом выходе
OWN_DIRS=()   # стенды, которые эта проба создала или осиротила сама
cleanup() {
    local p d
    for p in "${CHILDREN[@]}"; do kill -TERM "$p" 2>/dev/null; done
    sleep 1
    for p in "${CHILDREN[@]}"; do kill -KILL "$p" 2>/dev/null; done
    for d in "${OWN_DIRS[@]}"; do
        for p in $(stand_pids "$d"); do kill -KILL "$p" 2>/dev/null; done
        rm -rf "$d"
    done
    rm -rf "$W"
}
trap cleanup EXIT

stand_pids() {  # $1 = каталог стенда со слешем, $2 = подстрока командной строки (необязательно)
    local f pid
    for f in /proc/[0-9]*/environ; do
        grep -qaF -- "=$1" "$f" 2>/dev/null || continue
        pid="${f#/proc/}"; pid="${pid%/environ}"
        [ -z "${2:-}" ] || grep -qaF -- "$2" "/proc/$pid/cmdline" 2>/dev/null || continue
        echo "$pid"
    done
}
count() { stand_pids "$@" | wc -l; }
stand_of() {  # $1 = префикс, $2 = pid хозяина → каталог его стенда со слешем
    local f
    for f in /tmp/"$1"*/owner; do
        [ "$(cat "$f" 2>/dev/null)" = "$2" ] && { echo "${f%owner}"; return 0; }
    done
    return 1
}
wait_for() {  # $1 = предел, остальное — условие
    local end=$(( $(date +%s) + $1 )); shift
    while [ "$(date +%s)" -lt "$end" ]; do "$@" && return 0; sleep 0.2; done
    "$@"
}
alive() { kill -0 "$1" 2>/dev/null; }
running() { ps -o stat= -p "$1" 2>/dev/null | grep -qv Z; }  # зомби — уже убит
dispatcher_up() { [ "$(count "$1" tg-dispatcher.sh)" -gt 0 ]; }
yes_no() { if "$@"; then echo да; else echo нет; fi; }

# $1 = префикс, $2 = файл пробы, $3 = файл вывода; запускает случай «диспетчер» и
# ждёт диспетчер стенда. Итог в RUN_PID и RUN_DIR («-» — стенда с диспетчером нет):
# прогон обязан быть ребёнком этой оболочки, иначе его код не дождаться.
start_dispatcher_run() {
    bash "$KOREN/scripts/$2" --случай диспетчер > "$3" 2>&1 < /dev/null &
    RUN_PID=$!; CHILDREN+=("$RUN_PID"); RUN_DIR=-
    wait_for 10 stand_of "$1" "$RUN_PID" >/dev/null || return 0
    RUN_DIR=$(stand_of "$1" "$RUN_PID")
    wait_for 60 dispatcher_up "$RUN_DIR" || RUN_DIR=-
}
leftover() {  # $1 = каталог стенда или «-»; печатает число процессов, остальное — в журнал
    local p
    [ "$1" != - ] || { echo нет-стенда; return; }
    for p in $(stand_pids "$1"); do ps -o pid=,etime=,args= -p "$p" | sed 's/^/    остался: /' >&2; done
    count "$1"
}

judge() {  # $1 = имя, $2 = файл пробы, $3 = префикс, $4 = быстрый случай
    local name="$1" probe="$2" prefix="$3" quick="$4"
    local pid d seen code xpid xd fake_live fake_dead decoy_live decoy_dead gone
    echo "── $name: $probe"

    # 1. выход
    bash "$KOREN/scripts/$probe" --случай диспетчер > "$W/$name-exit.out" 2>&1 < /dev/null &
    pid=$!; CHILDREN+=("$pid")
    wait_for 10 stand_of "$prefix" "$pid" >/dev/null
    d=$(stand_of "$prefix" "$pid") || d=""
    seen=нет
    while alive "$pid"; do
        [ -n "$d" ] && dispatcher_up "$d" && seen=да
        sleep 0.3
    done
    wait "$pid"; code=$?
    case_check "$name, выход: код пробы" 0 "$code"
    case_check "$name, выход: диспетчер стенда был виден (стенд найден по хозяину)" да "$seen"
    sleep 1
    case_check "$name, выход: процессов стенда после конца" 0 "$(leftover "${d:--}")"
    [ "$code" = 0 ] || tail -5 "$W/$name-exit.out" | sed 's/^/    /'

    # 2. TERM
    start_dispatcher_run "$prefix" "$probe" "$W/$name-term.out"; pid=$RUN_PID; d=$RUN_DIR
    case_check "$name, TERM: диспетчер стенда поднялся" да "$( [ "$d" != - ] && echo да || echo нет)"
    kill -TERM "$pid"
    wait_for 30 bash -c "! kill -0 $pid 2>/dev/null"
    wait "$pid"; code=$?
    case_check "$name, TERM: код пробы" 143 "$code"
    sleep 1
    case_check "$name, TERM: процессов стенда после TERM" 0 "$(leftover "$d")"

    # 3–4. KILL оставляет сирот; рядом стенд живого хозяина и стенд мёртвого.
    start_dispatcher_run "$prefix" "$probe" "$W/$name-kill.out"; xpid=$RUN_PID; xd=$RUN_DIR
    [ "$xd" != - ] && OWN_DIRS+=("$xd")
    kill -KILL "$xpid"; wait "$xpid" 2>/dev/null
    sleep 0.5
    case_check "$name, KILL: процессы стенда пережили пробу (условие)" да \
        "$( [ "$xd" != - ] && [ "$(count "$xd")" -gt 0 ] && echo да || echo нет)"
    fake_live=$(mktemp -d "/tmp/${prefix}XXXXXX"); OWN_DIRS+=("$fake_live/")
    echo $$ > "$fake_live/owner"
    PROBA_STAND="$fake_live/" sleep 600 &
    decoy_live=$!; CHILDREN+=("$decoy_live"); disown "$decoy_live"
    fake_dead=$(mktemp -d "/tmp/${prefix}XXXXXX"); OWN_DIRS+=("$fake_dead/")
    ( : ) & gone=$!; wait "$gone"
    echo "$gone" > "$fake_dead/owner"
    PROBA_STAND="$fake_dead/" sleep 600 &
    decoy_dead=$!; CHILDREN+=("$decoy_dead"); disown "$decoy_dead"
    bash "$KOREN/scripts/$probe" --случай "$quick" > "$W/$name-quick.out" 2>&1 < /dev/null
    code=$?
    case_check "$name, следующий прогон («$quick»): код" 0 "$code"
    case_check "$name, следующий прогон: процессов стенда, убитого KILL" 0 \
        "$(leftover "$xd")"
    case_check "$name, следующий прогон: процесс стенда мёртвого хозяина убит" нет "$(yes_no running "$decoy_dead")"
    case_check "$name, следующий прогон: процесс стенда живого хозяина цел" да "$(yes_no running "$decoy_live")"
    PATHS_JUDGED=$((PATHS_JUDGED + 4))
}

PATHS_JUDGED=0
for s in "${SUBJECTS[@]}"; do
    IFS='|' read -r name probe prefix quick <<< "$s"
    [ -z "$ONLY" ] || [ "$ONLY" = "$name" ] || continue
    judge "$name" "$probe" "$prefix" "$quick"
done

echo "судимых путей: $PATHS_JUDGED"
[ "$PATHS_JUDGED" -gt 0 ] || { echo "ПРОБА КРАСНАЯ: не судилось ни одного пути"; exit 1; }
[ "$CASES_FAILED" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: путей $PATHS_JUDGED, случаев $CASES_PATHS"; exit 0; }
echo "ПРОБА КРАСНАЯ: путей $PATHS_JUDGED, неудач $CASES_FAILED из $CASES_PATHS"; exit 1
