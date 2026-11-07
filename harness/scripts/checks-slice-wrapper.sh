#!/bin/bash
#
# Корневая половина двери run-in-checks-slice.sh. Ставится установщиком в
# /usr/local/sbin/harness-run-in-checks (root:root 0755), в sudoers — одна
# строка на этот файл. Безопасность держит то, что пользователь в юните
# зашит при установке, а не берётся из доводов: ни одну команду от root
# через эту дверь запустить нельзя, только от агента и только в срезе.
#
# Замок очереди и предел жизни — ВНУТРИ юнита: убитая внешняя цепочка
# (перезапуск службы агента, конец сессии) не освобождает замок, пока жива
# работа. Путь замка зашит здесь и доводом не принимается.
#
# Вызов: harness-run-in-checks [--run-max-min N] [--lock-wait-min M] <каталог>
#            [ИМЯ=значение ...] -- команда [доводы...]
set -u
AGENT_USER='@AGENT_USER@'
LOCK=/run/lock/harness-checks-slice.lock
OWNER=/run/lock/harness-checks-slice.lock.owner
BUSY_RC=75
run_max=120; lock_wait=30
while [ "${1:-}" = --run-max-min ] || [ "${1:-}" = --lock-wait-min ]; do
    [[ "${2:-}" =~ ^[0-9]+$ ]] || { echo "harness-run-in-checks: $1 — не число" >&2; exit 2; }
    if [ "$1" = --run-max-min ]; then run_max="$((10#$2))"; else lock_wait="$((10#$2))"; fi
    shift 2
done
# Предел жизни юнита — механизм, а не договор: без верхней границы вызывающий
# отключил бы RuntimeMaxSec и держал очередь вечно.
MAX_MIN=$((24 * 60))
{ [ "$run_max" -ge 1 ] && [ "$run_max" -le "$MAX_MIN" ] && [ "$lock_wait" -le "$MAX_MIN" ]; } \
    || { echo "harness-run-in-checks: предел вне 1..$MAX_MIN минут" >&2; exit 2; }
cwd="${1:-}"; shift || true
case "$cwd" in /*) ;; *) echo "harness-run-in-checks: нужен абсолютный каталог" >&2; exit 2 ;; esac
env_args=()
while [ $# -gt 0 ] && [ "$1" != -- ]; do
    [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] \
        || { echo "harness-run-in-checks: не ИМЯ=значение: ${1%%=*}" >&2; exit 2; }
    # Двери пробы двери в юнит не доходят.
    case "$1" in HARNESS_CHECKS_SLICE_*) ;; *) env_args+=("--setenv=$1") ;; esac
    shift
done
[ "${1:-}" = -- ] && [ $# -ge 2 ] || { echo "harness-run-in-checks: нет команды" >&2; exit 2; }
shift
# Держатель записывает себя после взятия замка: отказ называет его по этой записи.
# Без «$$»: systemd-run схлопывает его в «$» и пид терялся. Пид оболочки — родитель cut.
holder_script='own="$1"; shift; printf "%s %s %s\n" "$(cut -d" " -f4 /proc/self/stat)" "$(/bin/date +%H:%M)" "$*" > "$own"; exec "$@"'
exec /usr/bin/systemd-run --quiet --slice=harness-checks.slice --uid="$AGENT_USER" \
    --collect --wait --pipe -p "RuntimeMaxSec=$((run_max * 60))" --working-directory="$cwd" \
    "${env_args[@]}" -- /usr/bin/flock -E "$BUSY_RC" -w "$((lock_wait * 60))" "$LOCK" \
    /bin/bash -c "$holder_script" _ "$OWNER" "$@"
