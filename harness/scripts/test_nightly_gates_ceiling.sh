#!/usr/bin/env bash
#
# test_nightly_gates_ceiling.sh — проба предела времени ночного прогона ворот
# (hr.nightly-gates-time-ceiling). Судит harness/demons/nightly-gates.sh на
# стенде: свой PROJECT_DIR, подставной tg_send.sh, подставной прогон, предел
# NIGHTLY_GATES_MAX_MIN из harness.conf СТЕНДА (доли минуты — проба не спит
# боевой предел). Дверь для мутаций: NIGHTLY_GATES_UNDER_TEST.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
DAEMON="${NIGHTLY_GATES_UNDER_TEST:-$HERE/../harness/demons/nightly-gates.sh}"
[ -f "$DAEMON" ] || { echo "ПРОБА КРАСНАЯ: механизма нет ($DAEMON)"; exit 1; }

# Маркер стенда стоит в пути каталога: его несут командные строки всех
# процессов стенда, по нему и только по нему чистим (рядом идёт боевой прогон).
MARK="ngceil-$$-$RANDOM"
T="$(mktemp -d "/tmp/$MARK.XXXX")"
DAEMON_PIDS=()
cleanup() {
    local pid
    for pid in ${DAEMON_PIDS[@]+"${DAEMON_PIDS[@]}"}; do kill -9 "$pid" 2>/dev/null; done
    pkill -9 -f "$MARK" 2>/dev/null
    rm -rf "$T"
}
trap cleanup EXIT

total=0; failed=0
check() {  # $1 = имя случая, $2 = 0/1 (1 — выполнено), $3 = подробность при неудаче
    total=$((total + 1))
    if [ "$2" = 1 ]; then printf '  ок    %s\n' "$1"
    else printf '  ПЛОХО %s: %s\n' "$1" "$3"; failed=$((failed + 1)); fi
}

mkdir -p "$T/proekt/scripts" "$T/hb" "$T/log"
cat > "$T/proekt/scripts/tg_send.sh" <<'TG'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TG_LOG"
TG
chmod +x "$T/proekt/scripts/tg_send.sh"

# Спящий прогон: строка гейта, потом потомок с маркером в командной строке
# (два оператора — bash не заменит себя на sleep) и ожидание его.
cat > "$T/sleepy.sh" <<SLEEPY
printf '  ок · первый\n'
bash -c 'sleep 60; true' "$MARK-child" &
wait
SLEEPY
cat > "$T/fast.sh" <<'FAST'
printf '  ок · первый\n=== итог: зелёных 5, красных 0 ===\n'
FAST
# Прогон дошёл до итога и сам вышел кодом 124: gates.sh выходит кодом, равным
# числу красных, и 124 красных — не предел времени.
cat > "$T/red124.sh" <<'RED124'
printf '  ПЛОХО · первый\n=== итог: зелёных 1, красных 124 ===\n'
exit 124
RED124

# Запускает демона фоном на стенде. $1 = предел в минутах (в harness.conf
# стенда), $2 = подставной прогон. Печатает pid.
start_daemon() {
    printf 'NIGHTLY_GATES_MAX_MIN=%s\n' "$1" > "$T/harness.conf"
    : > "$T/install.conf"; : > "$T/tg.log"; rm -f "$T/log"/nightly-gates-*.log
    env -u NIGHTLY_GATES_MAX_MIN -u HARNESS_HOME \
        HEARTBEAT_DIR="$T/hb" LOG_DIR="$T/log" PROJECT_DIR="$T/proekt" \
        TG_LOG="$T/tg.log" NIGHTLY_GATES_CMD="bash $T/$2" \
        HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
        bash "$DAEMON" > "$T/daemon.log" 2>&1 < /dev/null &
    DAEMON_PIDS+=("$!")
    LAST_PID=$!
}

# Ждёт конца демона не дольше $2 секунд. Код 0 — кончился сам (его код в
# DAEMON_RC), 1 — не кончился: демон убит KILL'ом (только он, не группа —
# иначе потомки умрут от нашей руки и случай 2 позеленеет даром).
wait_daemon() {
    local pid=$1 limit=$2 i=0
    while kill -0 "$pid" 2>/dev/null && [ "$i" -lt $((limit * 10)) ]; do
        sleep 0.1; i=$((i + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; DAEMON_RC=KILL; return 1
    fi
    wait "$pid"; DAEMON_RC=$?; return 0
}

has() { grep -q -- "$1" "$2" 2>/dev/null && echo 1 || echo 0; }

# (1) БОЛЬНОЙ: прогон висит, предел 0.05 мин (3 с) — демон обязан кончиться сам.
start_daemon 0.05 sleepy.sh
started=$SECONDS
wait_daemon "$LAST_PID" 20; ended=$?
took=$((SECONDS - started))
check "1 БОЛЬНОЙ: висящий прогон обрезан пределом (<20 с)" "$([ "$ended" = 0 ] && echo 1 || echo 0)" \
      "демон не кончился за 20 с, убит стендом"
check "1 БОЛЬНОЙ: код 1" "$([ "$DAEMON_RC" = 1 ] && echo 1 || echo 0)" "код $DAEMON_RC"
check "1 БОЛЬНОЙ: журнал демона «упёрся в предел»" "$(has 'упёрся в предел' "$T/daemon.log")" \
      "строки нет"
check "1 БОЛЬНОЙ: журнал демона называет последний гейт «первый»" \
      "$(grep 'упёрся в предел' "$T/daemon.log" 2>/dev/null | grep -q 'первый' && echo 1 || echo 0)" \
      "в строке отказа имени нет"
check "1 БОЛЬНОЙ: весть с ключом nightly-gates:broken" "$(has 'nightly-gates:broken' "$T/tg.log")" \
      "в вести: «$(tr '\n' ' ' < "$T/tg.log")»"

# (2) Потомок прогона не переживает предел (-k 60 тут не ждём: TERM группе).
sleep 2
check "2 БОЛЬНОЙ: потомок прогона убит вместе с прогоном" \
      "$(pgrep -f "$MARK-child" >/dev/null && echo 0 || echo 1)" \
      "жив: $(pgrep -af "$MARK-child" | head -1)"
pkill -9 -f "$MARK" 2>/dev/null

# (3) Быстрый прогон под пределом — как раньше.
start_daemon 0.05 fast.sh
wait_daemon "$LAST_PID" 20
check "3 быстрый прогон: код 0" "$([ "$DAEMON_RC" = 0 ] && echo 1 || echo 0)" "код $DAEMON_RC"
check "3 быстрый прогон: строка итога в журнале демона" \
      "$(has '=== итог: зелёных 5, красных 0 ===' "$T/daemon.log")" "строки нет"
check "3 быстрый прогон: «упёрся» нет" "$(has 'упёрся' "$T/daemon.log" | tr 01 10)" "отказ на быстром"
check "3 быстрый прогон: вести нет" "$([ -s "$T/tg.log" ] && echo 0 || echo 1)" \
      "весть: «$(tr '\n' ' ' < "$T/tg.log")»"

# (5) Длительность в строке итога.
check "5 итог несёт длительность «за N мин»" \
      "$(grep -qE '=== итог: зелёных 5, красных 0 ===.* за [0-9]+([.,][0-9]+)? мин' "$T/daemon.log" && echo 1 || echo 0)" \
      "строка: «$(grep '=== итог' "$T/daemon.log" | head -1)»"

# (4) Предел из ДАННЫХ: 0.15 мин (9 с). Жив через 6 с — значит не 3-секундная
# константа; кончился сам до 25 с — значит предел вообще есть.
start_daemon 0.15 sleepy.sh
sleep 6
alive=$(kill -0 "$LAST_PID" 2>/dev/null && echo 1 || echo 0)
check "4 предел из данных: при 0.15 мин демон жив через 6 с" "$alive" "кончился раньше 6 с"
wait_daemon "$LAST_PID" 19; ended=$?
check "4 предел из данных: при 0.15 мин обрезан до 25 с" \
      "$([ "$ended" = 0 ] && grep -q 'упёрся в предел' "$T/daemon.log" && echo 1 || echo 0)" \
      "кончился=$([ "$ended" = 0 ] && echo да || echo нет), код $DAEMON_RC"

# (6) Итог есть, код 124 от самого прогона (124 красных) — это красный прогон,
# а не предел: решает строка итога, не код.
start_daemon 0.05 red124.sh
wait_daemon "$LAST_PID" 20
check "6 итог с кодом 124: «упёрся в предел» нет" "$(has 'упёрся' "$T/daemon.log" | tr 01 10)" \
      "демон принял 124 красных за предел"
check "6 итог с кодом 124: строка итога в журнале демона" \
      "$(has '=== итог: зелёных 1, красных 124 ===' "$T/daemon.log")" "строки нет"
check "6 итог с кодом 124: весть с ключом nightly-gates:red" "$(has 'nightly-gates:red' "$T/tg.log")" \
      "в вести: «$(tr '\n' ' ' < "$T/tg.log")»"
check "6 итог с кодом 124: ключа nightly-gates:broken нет" "$(has 'nightly-gates:broken' "$T/tg.log" | tr 01 10)" \
      "в вести: «$(tr '\n' ' ' < "$T/tg.log")»"

if [ "$failed" = 0 ]; then echo "ПРОБА ЗЕЛЁНАЯ: случаев $total"; exit 0; fi
echo "ПРОБА КРАСНАЯ: неудач $failed из $total"; exit 1
