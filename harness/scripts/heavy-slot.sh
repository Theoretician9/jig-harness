#!/usr/bin/env bash
#
# heavy-slot.sh — общий пул мест для тяжёлых прогонов вне среза (job server).
#
#   bash scripts/heavy-slot.sh [--не-дольше <сек>] -- <команда> [доводы...]
#
# Зачем. Пробы и ворота помощников идут в cgroup сессии, мимо потолка
# harness-checks.slice: при 13–16 помощниках машина занята на 7,4 ядра из 8,
# load 21,9, и ответ в канал ждёт вместе со всеми (замер шага 2,
# 19.11). Мест HEAVY_SLOTS (harness.conf);
# лишние ждут своей очереди, а не душат машину.
#
# Место — flock на $LOG_DIR/heavy-slots/<k>.lock, k = 1..HEAVY_SLOTS: ядро снимает
# замок вместе с убитым держателем, файл-метка пережила бы его. Команда идёт
# РЕБЁНКОМ без дескриптора замка: внук, отпущенный жить (стенд пробы), места не
# держит — иначе осиротевшая проба занимала бы место сутками.
#
# Повторный вход второго места не берёт: место держит HEAVY_SLOT_HELD=1 у потомков.
# Без этого pre-commit, который зовут ворота, ждал бы место, занятое ими же.
# В срезе (HARNESS_IN_CHECKS_SLICE) свой потолок — команда идёт сразу.
# Ожидание дольше HEAVY_SLOT_WAIT_EVENT_S (5 с) — событие heavy.slot-wait.
#
# --не-дольше — для того, кто ждёт место, держа общий замок: pre-commit идёт под
# замком коммита своего дерева (git-commit-locked.sh, предел 300 с) и под
# index.lock. Бессрочное ожидание за часовыми воротами валило бы чужие коммиты по
# пределу замка; по сроку команда идёт без места, событие heavy.slot-overflow.
#
# Пока идут ворота выпуска (метка lib/release_mark.sh), новым заявкам места нет,
# кроме потомков выпуска (HARNESS_RELEASE_PID); взявшие место дослуживают.
set -u
MAX_WAIT=""
[ "${1:-}" = "--не-дольше" ] && { MAX_WAIT="${2:-}"; shift 2; }
[ "${1:-}" = "--" ] && shift
[ $# -gt 0 ] || { echo "heavy-slot: нет команды (bash scripts/heavy-slot.sh -- <команда>)" >&2; exit 2; }
# Срез узнаётся и по cgroup: ночные юниты стоят в нём без метки HARNESS_IN_CHECKS_SLICE.
# В срезе метка места тоже ставится: вызывающий, который заворачивает сам себя
# (gates.sh), по ней не заходит в обёртку второй раз.
if [ -n "${HEAVY_SLOT_HELD:-}${HARNESS_IN_CHECKS_SLICE:-}" ] \
   || grep -q 'harness-checks\.slice' "${HARNESS_CHECKS_SLICE_CGROUP_FILE:-/proc/self/cgroup}" 2>/dev/null; then
    export HEAVY_SLOT_HELD=1
    exec "$@"
fi

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck disable=SC1091
. "$HERE/lib/config.sh"
# shellcheck disable=SC1091
. "$HERE/lib/events.sh"
# shellcheck disable=SC1091
. "$HERE/lib/release_mark.sh"
konf_zagruzit
SLOTS="${HEAVY_SLOTS:-4}"
case "$SLOTS" in ''|*[!0-9]*|0) echo "heavy-slot: HEAVY_SLOTS=«$SLOTS» не число мест — беру 4" >&2; SLOTS=4 ;; esac
DIR="$LOG_DIR/heavy-slots"
mkdir -p "$DIR"

take_slot() {  # ставит FD на открытый и взятый замок; код 1 — мест нет (заняты или идёт выпуск)
    local k
    RELEASE=$(release_mark_owner) && [ "$RELEASE" != "${HARNESS_RELEASE_PID:-}" ] && return 1
    RELEASE=""
    for k in $(seq 1 "$SLOTS"); do
        exec {FD}>>"$DIR/$k.lock" || continue
        flock -n "$FD" && return 0
        exec {FD}>&-
    done
    return 1
}

started=$(date +%s)
said=""
until take_slot; do
    if [ -n "$RELEASE" ] && [ "$said" != release ]; then
        echo "heavy-slot: идёт выпуск (pid $RELEASE) — новым заявкам места нет, жду его конца" >&2; said=release
    elif [ -z "$RELEASE" ] && [ -z "$said" ]; then
        echo "heavy-slot: все $SLOTS мест заняты — жду своей очереди" >&2; said=1
    fi
    if [ -n "$MAX_WAIT" ] && [ $(( $(date +%s) - started )) -ge "$MAX_WAIT" ]; then
        echo "heavy-slot: места нет ${MAX_WAIT} с — иду без места" >&2
        harness_event heavy-slots scripts/heavy-slot.sh heavy.slot-overflow skipped "секунд=$MAX_WAIT" \
            "команда=$(printf '%s ' "$@" | cut -c1-300)" "мест=$SLOTS"
        export HEAVY_SLOT_HELD=1
        exec nice -n 10 "$@"
    fi
    sleep 0.5
done
waited=$(( $(date +%s) - started ))
if [ "$waited" -gt "${HEAVY_SLOT_WAIT_EVENT_S:-5}" ]; then
    harness_event heavy-slots scripts/heavy-slot.sh heavy.slot-wait ok "секунд=$waited" \
        "команда=$(printf '%s ' "$@" | cut -c1-300)" "мест=$SLOTS"
fi

export HEAVY_SLOT_HELD=1
# Сигнал обёртке (timeout, kill вызывающего) доходит до команды: без пересылки
# убитая обёртка отпустила бы место, а команда шла бы дальше мимо счёта.
nice -n 10 "$@" {FD}>&- <&0 &
child=$!
trap 'kill -TERM "$child" 2>/dev/null' TERM INT HUP
rc=0
while :; do
    wait "$child"; rc=$?
    kill -0 "$child" 2>/dev/null || break
done
exit "$rc"
