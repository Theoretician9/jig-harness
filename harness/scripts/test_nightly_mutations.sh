#!/usr/bin/env bash
#
# test_nightly_mutations.sh — проба ночного демона мутационной пробы
# (hr.alert-nightly-gates-broken). Судит harness/demons/nightly-mutations.sh на
# стенде: свой PROJECT_DIR, подставные tg_send.sh и alert-task.py, подставной
# прогон прибора, предел и промежуток вестей из harness.conf СТЕНДА (доли минуты
# и часа — проба не спит боевые сроки). Дверь для мутаций:
# NIGHTLY_MUTATIONS_UNDER_TEST.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
DAEMON="${NIGHTLY_MUTATIONS_UNDER_TEST:-$HERE/../harness/demons/nightly-mutations.sh}"
[ -f "$DAEMON" ] || { echo "ПРОБА КРАСНАЯ: механизма нет ($DAEMON)"; exit 1; }

# Маркер стенда стоит в пути каталога: его несут командные строки всех
# процессов стенда, по нему и только по нему чистим (рядом идёт боевой прогон).
MARK="nmut-$$-$RANDOM"
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

STAMP="$T/log/nightly-mutations.alert-ts"
HB="$T/hb/nightly-mutations"

mkdir -p "$T/proekt/scripts" "$T/hb" "$T/log"
cat > "$T/proekt/scripts/tg_send.sh" <<'TG'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TG_LOG"
TG
chmod +x "$T/proekt/scripts/tg_send.sh"
cat > "$T/proekt/scripts/alert-task.py" <<'ALERT'
import os, sys
with open(os.environ["ALERT_LOG"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
ALERT

# Висящий прогон: строка прогресса, потом потомок с маркером в командной строке
# (два оператора — bash не заменит себя на sleep) и ожидание его.
cat > "$T/sleepy.sh" <<SLEEPY
printf 'мутация 3 из 40: успела-строка\n'
bash -c 'sleep 60; true' "$MARK-child" &
wait
SLEEPY
# Итог с метками времени и код 124: прибор выходит кодом, равным числу дыр, и
# совпадение с кодом timeout не делает его пределом. Числа 7 и 2 — чтобы «3»
# в вести могло прийти только из дыр.
cat > "$T/holes124.sh" <<'HOLES'
printf '2026-11-03 01:00:00  мутация 40 из 40\n'
printf '2026-11-03 01:00:01  === мутационная проба: покраснели как надо 7, дыр 3, из кэша 2 ===\n'
exit 124
HOLES
cat > "$T/clean.sh" <<'CLEAN'
printf '=== мутационная проба: покраснели как надо 9, дыр 0, из кэша 1 ===\n'
CLEAN
cat > "$T/torn.sh" <<'TORN'
printf 'мутация 1 из 40: начало\n'
exit 1
TORN

now_epoch() { printf '%(%s)T' -1; }

# Готовит стенд и запускает демона фоном. $1 = предел в минутах, $2 = подставной
# прогон, $3 = промежуток вестей в часах, $4 = keep — штамп не трогать.
start_daemon() {
    printf 'NIGHTLY_MUTATIONS_MAX_MIN=%s\nNIGHTLY_MUTATIONS_ALERT_GAP_H=%s\nNIGHTLY_MUTATIONS_LOCK_WAIT_MIN=0.02\n' "$1" "$3" > "$T/harness.conf"
    : > "$T/install.conf"; : > "$T/tg.log"; : > "$T/alert.log"
    rm -f "$T/log"/nightly-mutations-*.log "$T/daemon.log" "$HB"
    [ "${4:-}" = keep ] || rm -f "$STAMP"
    env -u NIGHTLY_MUTATIONS_MAX_MIN -u NIGHTLY_MUTATIONS_ALERT_GAP_H -u NIGHTLY_MUTATIONS_LOCK_WAIT_MIN -u HARNESS_HOME -u HARNESS_EVENTS_STATE \
        HEARTBEAT_DIR="$T/hb" LOG_DIR="$T/log" PROJECT_DIR="$T/proekt" \
        TG_LOG="$T/tg.log" ALERT_LOG="$T/alert.log" NIGHTLY_MUTATIONS_CMD="bash $T/$2" \
        HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
        bash "$DAEMON" > "$T/daemon.log" 2>&1 < /dev/null &
    DAEMON_PIDS+=("$!")
    LAST_PID=$!
}

# Ждёт конца демона не дольше $2 секунд. Код 0 — кончился сам (его код в
# DAEMON_RC), 1 — не кончился: демон убит KILL'ом (только он, не группа —
# иначе потомки умрут от нашей руки и проверка потомка позеленеет даром).
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
hasnt() { grep -q -- "$1" "$2" 2>/dev/null && echo 0 || echo 1; }
yes_if() { "$@" && echo 1 || echo 0; }
tg_text() { tr '\n' ' ' < "$T/tg.log"; }

# (1) БОЛЬНОЙ: прогон висит, предел 0.05 мин (3 с) — демон обязан кончиться сам.
start_daemon 0.05 sleepy.sh 24
started=$SECONDS
wait_daemon "$LAST_PID" 20; ended=$?
took=$((SECONDS - started))
check "1 БОЛЬНОЙ: висящий прогон обрезан пределом (<20 с)" "$(yes_if [ "$ended" = 0 ])" \
      "демон не кончился за 20 с, убит стендом"
check "1 БОЛЬНОЙ: код 1" "$(yes_if [ "$DAEMON_RC" = 1 ])" "код $DAEMON_RC за $took с"
check "1 БОЛЬНОЙ: журнал демона «упёрся в предел»" "$(has 'упёрся в предел' "$T/daemon.log")" "строки нет"
check "1 БОЛЬНОЙ: весть с ключом nightly-mutations:broken" "$(has 'nightly-mutations:broken' "$T/tg.log")" \
      "в вести: «$(tg_text)»"
check "1 БОЛЬНОЙ: весть несёт последнюю строку прибора" "$(has 'успела-строка' "$T/tg.log")" \
      "в вести: «$(tg_text)»"
check "1 БОЛЬНОЙ: исход отправки в журнале" "$(has 'канал: отправлено владельцу' "$T/daemon.log")" "строки нет"
check "1 БОЛЬНОЙ: метка свежести стоит и на пределе" "$(yes_if [ -s "$HB" ])" "метки нет"
check "1 БОЛЬНОЙ: штамп вести записан эпохой" \
      "$(yes_if grep -qE '^[0-9]{9,}$' "$STAMP")" "штамп: «$(cat "$STAMP" 2>/dev/null)»"
# Потомок прогона не переживает предел (-k 60 тут не ждём: TERM группе).
sleep 2
check "1 БОЛЬНОЙ: потомок прогона убит вместе с прогоном" \
      "$(pgrep -f "$MARK-child" >/dev/null && echo 0 || echo 1)" \
      "жив: $(pgrep -af "$MARK-child" | head -1)"
pkill -9 -f "$MARK" 2>/dev/null

# (2) БОЛЬНОЙ: второй предел подряд, штамп час назад, промежуток 24 ч — весть
# не шлётся, а журнал говорит, что не шлёт.
old=$(( $(now_epoch) - 3600 ))
printf '%s\n' "$old" > "$STAMP"
start_daemon 0.05 sleepy.sh 24 keep
wait_daemon "$LAST_PID" 20
check "2 БОЛЬНОЙ: при свежем штампе вести нет" "$(yes_if [ ! -s "$T/tg.log" ])" "весть: «$(tg_text)»"
check "2 БОЛЬНОЙ: журнал демона «весть не шлю»" "$(has 'весть не шлю' "$T/daemon.log")" "строки нет"
check "2 БОЛЬНОЙ: штамп не сдвинут" "$(yes_if [ "$(cat "$STAMP" 2>/dev/null)" = "$old" ])" \
      "было $old, стало «$(cat "$STAMP" 2>/dev/null)»"
check "2 БОЛЬНОЙ: предел всё равно назван, код 1" \
      "$([ "$DAEMON_RC" = 1 ] && grep -q 'упёрся в предел' "$T/daemon.log" && echo 1 || echo 0)" "код $DAEMON_RC"
sleep 1; pkill -9 -f "$MARK" 2>/dev/null

# (3) Штамп 25 ч назад при промежутке 24 ч — весть идёт снова, штамп сдвинут.
old=$(( $(now_epoch) - 25 * 3600 ))
printf '%s\n' "$old" > "$STAMP"
start_daemon 0.05 sleepy.sh 24 keep
wait_daemon "$LAST_PID" 20
check "3 штамп старше промежутка: весть nightly-mutations:broken идёт" \
      "$(has 'nightly-mutations:broken' "$T/tg.log")" "в вести: «$(tg_text)»"
check "3 штамп старше промежутка: «весть не шлю» нет" "$(hasnt 'весть не шлю' "$T/daemon.log")" \
      "демон придержал весть"
stamp=$(cat "$STAMP" 2>/dev/null)
check "3 штамп старше промежутка: штамп сдвинут вперёд" \
      "$(yes_if [ "${stamp:-0}" -gt $((old + 24 * 3600)) ] 2>/dev/null)" "было $old, стало «$stamp»"
sleep 1; pkill -9 -f "$MARK" 2>/dev/null

# (4) БОЛЬНОЙ: итог есть, код 124 от самого прибора (дыр 3) — это красная
# проба, а не предел: решает строка итога, не код.
start_daemon 0.05 holes124.sh 24
wait_daemon "$LAST_PID" 20
check "4 БОЛЬНОЙ: итог с кодом 124 — «упёрся» нет" "$(hasnt 'упёрся' "$T/daemon.log")" \
      "демон принял 3 дыры с кодом 124 за предел"
check "4 БОЛЬНОЙ: весть с ключом nightly-mutations:red" "$(has 'nightly-mutations:red' "$T/tg.log")" \
      "в вести: «$(tg_text)»"
check "4 БОЛЬНОЙ: весть несёт число дыр 3" "$(yes_if grep -qE '(^|[^0-9])3([^0-9]|$)' "$T/tg.log")" \
      "в вести: «$(tg_text)»"
check "4 БОЛЬНОЙ: ключа nightly-mutations:broken нет" "$(hasnt 'nightly-mutations:broken' "$T/tg.log")" \
      "в вести: «$(tg_text)»"
check "4 БОЛЬНОЙ: код 0" "$(yes_if [ "$DAEMON_RC" = 0 ])" "код $DAEMON_RC"

# (5) Чистый итог: вести нет, метка есть, оба ключа погашены.
start_daemon 0.05 clean.sh 24
wait_daemon "$LAST_PID" 20
check "5 дыр 0: вести нет" "$(yes_if [ ! -s "$T/tg.log" ])" "весть: «$(tg_text)»"
check "5 дыр 0: метка свежести стоит" "$(yes_if [ -s "$HB" ])" "метки нет"
check "5 дыр 0: код 0" "$(yes_if [ "$DAEMON_RC" = 0 ])" "код $DAEMON_RC"
check "5 дыр 0: погашен nightly-mutations:red" \
      "$(yes_if grep -qE -- '--resolve nightly-mutations:red( |$)' "$T/alert.log")" \
      "писатель звали: «$(tr '\n' ';' < "$T/alert.log")»"
check "5 дыр 0: погашен nightly-mutations:broken" \
      "$(yes_if grep -qE -- '--resolve nightly-mutations:broken( |$)' "$T/alert.log")" \
      "писатель звали: «$(tr '\n' ';' < "$T/alert.log")»"

# (6) Предел из ДАННЫХ: 0.15 мин (9 с). Жив через 6 с — значит не 3-секундная
# константа; кончился сам до 25 с — значит предел вообще есть.
start_daemon 0.15 sleepy.sh 24
sleep 6
check "6 предел из данных: при 0.15 мин демон жив через 6 с" \
      "$(kill -0 "$LAST_PID" 2>/dev/null && echo 1 || echo 0)" "кончился раньше 6 с"
wait_daemon "$LAST_PID" 19; ended=$?
check "6 предел из данных: при 0.15 мин обрезан до 25 с" \
      "$([ "$ended" = 0 ] && grep -q 'упёрся в предел' "$T/daemon.log" && echo 1 || echo 0)" \
      "кончился=$([ "$ended" = 0 ] && echo да || echo нет), код $DAEMON_RC"
sleep 1; pkill -9 -f "$MARK" 2>/dev/null

# (7) БОЛЬНОЙ: прибор вышел кодом 1 сам, без итога — оборван, не «чисто».
start_daemon 0.05 torn.sh 24
wait_daemon "$LAST_PID" 20
check "7 БОЛЬНОЙ: журнал демона «без итога»" "$(has 'без итога' "$T/daemon.log")" "строки нет"
check "7 БОЛЬНОЙ: весть с ключом nightly-mutations:broken" "$(has 'nightly-mutations:broken' "$T/tg.log")" \
      "в вести: «$(tg_text)»"
check "7 БОЛЬНОЙ: «упёрся» нет" "$(hasnt 'упёрся' "$T/daemon.log")" "обрыв принят за предел"
check "7 БОЛЬНОЙ: код 1" "$(yes_if [ "$DAEMON_RC" = 1 ])" "код $DAEMON_RC"

# (8) Метки запуска §5.4 от прогона (7): .start и .end одного pid, в .end код 1.
RUNS="$T/log/.events/runs"
start_pid=$(sed -n 's/.*pid=\([0-9]*\).*/\1/p' "$RUNS/nightly-mutations.start" 2>/dev/null)
check "8 метка .start несёт pid демона" "$(yes_if [ "$start_pid" = "$LAST_PID" ])" \
      "ждали pid=$LAST_PID, в метке: «$(cat "$RUNS/nightly-mutations.start" 2>/dev/null)»"
check "8 БОЛЬНОЙ: метка .end того же pid с кодом 1" \
      "$(yes_if grep -qE "^pid=$LAST_PID exit=1 " "$RUNS/nightly-mutations.end" 2>/dev/null)" \
      "в метке: «$(cat "$RUNS/nightly-mutations.end" 2>/dev/null)»"

# (9) БОЛЬНОЙ: прибор не прогнал ни одной мутации — строка итога есть, числа дыр
# нет. Это обрыв, а не «дыр нет»: весть broken, тревоги не гасятся.
cat > "$T/empty.sh" <<'EMPTY'
printf '2026-11-03 01:00:00  === мутационная проба: НИ ОДНОЙ мутации не прогнано — таблица пуста или данные не прочитались ===\n'
exit 1
EMPTY
start_daemon 0.05 empty.sh 24
wait_daemon "$LAST_PID" 20
check "9 БОЛЬНОЙ: пустой прогон не назван «дыр нет»" "$(hasnt 'дыр нет' "$T/daemon.log")" "демон счёл пустой прогон чистым"
check "9 БОЛЬНОЙ: весть с ключом nightly-mutations:broken" "$(has 'nightly-mutations:broken' "$T/tg.log")" \
      "в вести: «$(tg_text)»"
check "9 БОЛЬНОЙ: тревоги не погашены" "$(hasnt 'resolve' "$T/alert.log")" \
      "писатель звали: «$(tr '\n' ';' < "$T/alert.log")»"
check "9 БОЛЬНОЙ: код 1" "$(yes_if [ "$DAEMON_RC" = 1 ])" "код $DAEMON_RC"

# (10) БОЛЬНОЙ: идёт прошлый прогон (лок занят) — второй прибор не стартует,
# демон ждёт замок (0.02 мин) и говорит владельцу, что не стартовал.
LOCK="$T/log/locks/services/nightly-mutations.lock"
mkdir -p "$(dirname "$LOCK")"
flock "$LOCK" bash -c "sleep 30; true" "$MARK-lock" &
sleep 0.5
cat > "$T/touch.sh" <<TOUCH
: > "$T/started"
printf '=== мутационная проба: покраснели как надо 9, дыр 0, из кэша 1 ===\n'
TOUCH
rm -f "$T/started"
start_daemon 0.05 touch.sh 24
wait_daemon "$LAST_PID" 20
check "10 БОЛЬНОЙ: при занятом локе прибор не запущен" "$(yes_if [ ! -e "$T/started" ])" "второй прогон пошёл"
check "10 БОЛЬНОЙ: журнал «замок занят»" "$(has 'замок занят' "$T/daemon.log")" "строки нет"
check "10 БОЛЬНОЙ: весть «не стартовал: замок занят»" "$(has 'не стартовал: замок занят' "$T/tg.log")" "весть: «$(tg_text)»"
check "10 БОЛЬНОЙ: ключ nightly-mutations:broken" "$(has 'nightly-mutations:broken' "$T/tg.log")" "весть: «$(tg_text)»"
pkill -9 -f "$MARK-lock" 2>/dev/null

if [ "$failed" = 0 ]; then echo "ПРОБА ЗЕЛЁНАЯ: случаев $total"; exit 0; fi
echo "ПРОБА КРАСНАЯ: неудач $failed из $total"; exit 1
