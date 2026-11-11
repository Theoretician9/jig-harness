#!/usr/bin/env bash
# Проба: тест волны 3 сторожей, запущенный ИЗ ПАНЕЛИ, не гасит чужой tmux.
#
# Окружение `setsid nohup` переменную TMUX не чистит, а tmux при заданной
# TMUX берёт сокет из неё и TMUX_TMPDIR не слушает. Уборка стенда, гасившая
# сервер через одну TMUX_TMPDIR, гасила боевой сервер вместе с сессией agent.
#
# Проба поднимает ЧАСТНЫЙ сервер с сессией agent, зовёт тест волны 3 так,
# будто он запущен из панели этого сервера (TMUX указывает на частный сокет),
# и судит: частная agent жива; ни один сервер tmux, поднятый из стенда теста,
# не пережил прогон. Волна 3 получает частный TMUX_TMPDIR: уборка без -S и
# без TMUX ушла бы иначе на сокет по умолчанию — боевой сервер.
#
# Дверь для мутаций: HARNESS_WAVE3 — путь к тесту волны 3.
# Довод: имя стенда волны 3 (по умолчанию session-warden — только он поднимает
# настоящий tmux, на sentinel проверка утечки пуста по построению).
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
WAVE3="${HARNESS_WAVE3:-$HERE/test_watchers_wave3.sh}"
STAND_NAME="${1:-session-warden}"

# Свой tmux пробы — только по явному сокету и без унаследованной TMUX:
# сама проба тоже бывает запущена из панели. Свой сервер — без TMUX_TMPDIR
# под $P, чтобы поиск утёкших серверов под $P его не находил.
unset TMUX TMUX_PANE
P=$(mktemp -d)
SOCK="$P/private.sock"
own_tmux() { env -u TMUX -u TMUX_PANE -u TMUX_TMPDIR tmux -S "$SOCK" "$@"; }
mkdir -p "$P/tmux" "$P/tmp"
cleanup() {
    [ -n "${WATCHER:-}" ] && kill "$WATCHER" 2>/dev/null
    own_tmux kill-server 2>/dev/null
    rm -rf "$P"
}
trap cleanup EXIT

# Серверы tmux, чьё окружение несёт TMUX_TMPDIR под каталогом $1: так
# узнаётся сервер, поднятый тестом (стендом под TMPDIR или голым tmux под
# частным TMUX_TMPDIR).
servers_under() {
    local pid
    # Сервер tmux меняет себе имя процесса (comm) на «tmux: server».
    for pid in $(pgrep -x 'tmux: server'); do
        { tr '\0' '\n' < "/proc/$pid/environ"; } 2>/dev/null \
            | grep -q "^TMUX_TMPDIR=$1/" && echo "$pid"
    done | sort -u
}

own_tmux new-session -d -s agent sleep 100000 || { echo "ПРОБА КРАСНАЯ: частный сервер не поднялся"; exit 1; }
SERVER_PID=$(own_tmux display-message -p '#{pid}')
echo "частный сервер pid=$SERVER_PID; ДО: agent $(own_tmux has-session -t agent 2>/dev/null && echo жива || echo мертва)"

# Серверы стенда, замеченные за прогон: без них живая agent ничего не доказывает.
: > "$P/seen"
while :; do servers_under "$P" >> "$P/seen"; sleep 0.2; done &
WATCHER=$!

rc=0
TMUX="$SOCK,$SERVER_PID,0" TMUX_PANE="%0" TMPDIR="$P/tmp" TMUX_TMPDIR="$P/tmux" \
    bash "$WAVE3" --сторож "$STAND_NAME" > "$P/wave3.log" 2>&1 || rc=$?
kill "$WATCHER" 2>/dev/null; wait "$WATCHER" 2>/dev/null; WATCHER=
# Неудачи сторожей — их суд в своих воротах: они зависят от журналов и
# процессов машины. Здесь судится изоляция, итог волны — только в вывод.
echo "волна 3: rc=$rc, $(grep "ВОЛНА-3 СТОРОЖЕЙ" "$P/wave3.log" | tail -n 1)"

failed=0
seen=$(sort -u "$P/seen" | grep -c .)
if [ "$seen" -gt 0 ]; then
    echo "ПОСЛЕ: серверов стенда за прогон замечено $seen"
else
    echo "ПОСЛЕ: волна 3 не подняла ни одного сервера стенда — изоляцию судить нечем"
    failed=1
fi
# Тест, который не дошёл до итога, ничего не убирал — живая agent тогда ничего не доказывает.
if ! grep -q "ВОЛНА-3 СТОРОЖЕЙ: путей" "$P/wave3.log"; then
    echo "ПОСЛЕ: тест волны 3 не дошёл до итога — судить нечего"
    failed=1
fi
if own_tmux has-session -t agent 2>/dev/null; then
    echo "ПОСЛЕ: частная agent жива"
else
    echo "ПОСЛЕ: частная agent МЕРТВА — уборка теста погасила сервер из \$TMUX"
    failed=1
fi
leaked=$(servers_under "$P")
if [ -n "$leaked" ]; then
    echo "ПОСЛЕ: сервер стенда утёк, pid: $(echo $leaked)"
    # Свой мусор — свой: сервер со стендом под нашим каталогом гасим сами.
    for pid in $leaked; do kill "$pid" 2>/dev/null; done
    failed=1
else
    echo "ПОСЛЕ: серверов стенда не осталось"
fi

if [ "$failed" -eq 0 ]; then
    echo "ПРОБА ЗЕЛЁНАЯ"
    exit 0
fi
echo "ПРОБА КРАСНАЯ"
exit 1
