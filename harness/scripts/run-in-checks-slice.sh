#!/usr/bin/env bash
#
# Дверь запуска тяжёлых прогонов (мутации, тесты помощников) под потолок среза
# harness-checks.slice (CPUQuota/MemoryMax): иначе прогон идёт в cgroup сессии
# агента и съедает машину мимо потолка.
#
# Вызов: run-in-checks-slice.sh <команда> [доводы...]
# Уже в срезе (проверка /proc/self/cgroup) — просто exec: служба проверок не
# заворачивается второй раз. Метка HARNESS_IN_CHECKS_SLICE=1 ставится ребёнку
# в обоих случаях, по ней вызывающие (acceptance.py, mutation-probe.sh)
# не зовут дверь повторно: второго уровня нет.
# Корневая половина — harness-run-in-checks (checks-slice-wrapper.sh) через
# узкую строку sudoers; очередь и предел жизни держит она, внутри юнита.
# Нет обёртки, права на неё или потолка у среза — прогон идёт БЕЗ потолка, но
# с событием в журнал и тревогой: отказать значило бы остановить помощников.
# Окружение переносится по списку имён (доводы видны в ps — секретам там не место).
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ $# -gt 0 ] || { echo "run-in-checks-slice: нет команды" >&2; exit 2; }
export HARNESS_IN_CHECKS_SLICE=1
BUSY_RC=75
OWNER_FILE=/run/lock/harness-checks-slice.lock.owner
DEFAULT_WRAPPER=/usr/local/sbin/harness-run-in-checks
# shellcheck disable=SC1091
. "$HERE/lib/config.sh"
# shellcheck disable=SC1091
. "$HERE/lib/events.sh"
konf_zagruzit

# Двери пробы читаются в боевом пути; подставленная — не молча.
for probe_door in HARNESS_CHECKS_SLICE_CGROUP_FILE HARNESS_CHECKS_SLICE_WRAPPER; do
    [ -n "${!probe_door:-}" ] && echo "run-in-checks-slice: ВНИМАНИЕ подставлена дверь пробы $probe_door" >&2
done
if grep -q 'harness-checks\.slice' "${HARNESS_CHECKS_SLICE_CGROUP_FILE:-/proc/self/cgroup}" 2>/dev/null; then
    exec "$@"
fi

event() { # $1=событие $2=исход $3..=поля
    HARNESS_TASK=- harness_event checks-slice scripts/run-in-checks-slice.sh "$@"
}
# Один ключ тревоги: повтор в журнале тревог, не новая карточка. Из сессии агента
# alert-task отказывает (тревогой ведает сторож) — тогда след остаётся в событии.
no_ceiling() { # $1=причина
    echo "run-in-checks-slice: ВНИМАНИЕ $1 — запуск БЕЗ потолка среза" >&2
    event checks.slice.no-ceiling held "причина=$1" "итог=запуск без потолка среза"
    [ -f "$HERE/alert-task.py" ] && timeout 30 python3 "$HERE/alert-task.py" \
        --key checks-slice:no-ceiling --watcher checks-slice \
        --title "Мутации без потолка: прогон идёт мимо harness-checks.slice" \
        --text "Причина: $1. Прогоны помощников съедают машину мимо потолка CPU и памяти." \
        </dev/null >/dev/null 2>&1
    return 0
}

WRAPPER="${HARNESS_CHECKS_SLICE_WRAPPER:-$DEFAULT_WRAPPER}"
sudo_cmd=(sudo -n)
[ "$WRAPPER" != "$DEFAULT_WRAPPER" ] && sudo_cmd=()
unbounded=""
if [ ! -x "$WRAPPER" ]; then
    unbounded="нет обёртки $WRAPPER"
elif [ -n "${sudo_cmd[*]:-}" ] && ! sudo -n -l "$WRAPPER" >/dev/null 2>&1; then
    unbounded="нет права на обёртку $WRAPPER (строка sudoers)"
fi
if [ -n "$unbounded" ]; then
    no_ceiling "$unbounded"; exec "$@"
fi
quota="$(systemctl show -p CPUQuotaPerSecUSec --value harness-checks.slice 2>/dev/null)"
case "$quota" in ""|infinity) no_ceiling "у harness-checks.slice нет CPUQuota" ;; esac

RUN_MAX_MIN="${CHECKS_RUN_MAX_MIN:-120}"
LOCK_WAIT_MIN="${MUTATION_LOCK_WAIT_MIN:-30}"
pairs=()
while IFS='=' read -r -d '' name value; do
    case "$name" in
        HARNESS_CHECKS_SLICE_*) ;;
        HARNESS_*|MUTATION_*|MUT_*|MUTPROBE_*|CHECKS_*|NIGHTLY_*|LOG_DIR|GIT_CEILING_DIRECTORIES|PATH|HOME|LANG|LC_*|TERM|TMPDIR|VIRTUAL_ENV)
            case "$value" in *$'\n'*) continue ;; esac
            pairs+=("$name=$value") ;;
    esac
done < <(env -0)
cmd="$1"; shift
case "$cmd" in */*) ;; *) cmd="$(command -v "$cmd")" || { echo "run-in-checks-slice: нет команды $cmd" >&2; exit 127; } ;; esac

"${sudo_cmd[@]}" "$WRAPPER" --run-max-min "$RUN_MAX_MIN" --lock-wait-min "$LOCK_WAIT_MIN" \
    "$PWD" "${pairs[@]}" -- "$cmd" "$@"
rc=$?
if [ "$rc" = "$BUSY_RC" ]; then
    holder="$(cat "$OWNER_FILE" 2>/dev/null)"
    holder_pid="${holder%% *}"; rest="${holder#* }"; holder_since="${rest%% *}"; holder_cmd="${rest#* }"
    echo "run-in-checks-slice: очередь среза занята (держатель pid $holder_pid с $holder_since: $holder_cmd); ждали ${LOCK_WAIT_MIN} мин — отказ" >&2
    event checks.slice.queue-busy refused "держатель=$holder_pid" "с=$holder_since" "команда=$holder_cmd" "ждали_мин=$LOCK_WAIT_MIN"
fi
exit "$rc"
