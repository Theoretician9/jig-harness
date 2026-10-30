#!/usr/bin/env bash
# Оставить одну смену: закрыть лишних основных агентов, не тронув агента юнита.
# Юнит harness-agent (KillMode=control-group): сигнал MainPID или серверу tmux
# юнита снесёт всю группу вместе с живой сменой, поэтому им не шлётся ничего,
# даже kill -0. Учёт процессов — в scripts/lib/agent_processes.py.
# Использование: one-shift.sh [--dry-run] [--grace N]
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="${AGENT_PROCESSES_LIB:-$HERE/lib/agent_processes.py}"
PROC_ROOT="${AGENT_PROC_ROOT:-/proc}"
dry=0
grace=10
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) dry=1 ;;
        --grace) shift; grace="${1:-}" ;;
        *) echo "использование: one-shift.sh [--dry-run] [--grace N]" >&2; exit 2 ;;
    esac
    shift
done
case "$grace" in ''|*[!0-9]*) echo "--grace: нужно целое число секунд" >&2; exit 2 ;; esac

lib() { python3 "$LIB" "$@"; }

mainpid="$(lib mainpid)" || exit 1
mains=" $(lib claude-list --main) "
unit=" $(lib unit-agents) "
count=$(wc -w <<<"$mains")

if [ "$count" -le 1 ]; then
    echo "уже одна смена (основных агентов: $count)"
    exit 0
fi
if [ "${mainpid:-0}" -le 0 ]; then
    echo "MainPID юнита harness-agent = 0: агент поднят не юнитом (сторожем через cron) — какую смену оставить, не угадываю; основные агенты:${mains% }; закрыть лишнюю руками: $(for p in $mains; do printf 'kill -TERM %s; ' "$p"; done)" >&2
    exit 1
fi
n_unit=$(wc -w <<<"$unit")
if [ "$n_unit" -ne 1 ]; then
    echo "отказ: агентов юнита (потомков MainPID $mainpid) $n_unit, а нужен ровно один; какую смену оставить — неизвестно" >&2
    exit 1
fi

extra=()
for pid in $mains; do
    case "$unit" in *" $pid "*) continue ;; esac
    extra+=("$pid")
done
echo "оставляю агента юнита$unit(MainPID $mainpid); лишние: ${extra[*]}"
[ "$dry" -eq 1 ] && { echo "--dry-run: сигналов не шлю"; exit 0; }

alive() { [ -d "$PROC_ROOT/$1" ]; }

for pid in "${extra[@]}"; do env kill -TERM "$pid"; done
deadline=$((SECONDS + grace))
while [ "$SECONDS" -lt "$deadline" ]; do
    left=0
    for pid in "${extra[@]}"; do alive "$pid" && left=1; done
    [ "$left" -eq 0 ] && break
    sleep 0.2
done
for pid in "${extra[@]}"; do
    if alive "$pid"; then
        echo "pid $pid пережил TERM за ${grace} с, шлю KILL"
        env kill -KILL "$pid"
    fi
done
echo "готово: закрыто лишних смен: ${#extra[@]}"
