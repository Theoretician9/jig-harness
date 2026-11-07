#!/usr/bin/env bash
#
# test_nightly_mutations_lock.sh — проба замка и очереди полного прогона мутаций
# (hr.mutations-schedule): nightly-mutations.sh при занятом замке держит замок
# .want (флаг уступки — держатель, а не файл: умер процесс — флага нет) и ждёт
# замок NIGHTLY_MUTATIONS_LOCK_WAIT_MIN минут, а не выходит сразу; не дождался —
# весть nightly-mutations:broken и код 1. Срок мягкой остановки считается от
# старта процесса, с запасом не меньше MUTATION_RUN_TIMEOUT_S + 60 с; замки
# прибору не достаются. Зелёный итог гасит свои карточки nightly-mutations:red
# и :broken, а файлов снятого сторожа mutations-changed не трогает: его
# карточки гасит alert-task.py --drain по снятые_сторожа.
# Стенд по образцу test_nightly_mutations.sh: свой PROJECT_DIR, подставные
# tg_send.sh и alert-task.py, подставной прогон, сроки из harness.conf СТЕНДА
# долями минуты. Дверь для мутаций: NIGHTLY_MUTATIONS_UNDER_TEST.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
DAEMON="${NIGHTLY_MUTATIONS_UNDER_TEST:-$HERE/../harness/demons/nightly-mutations.sh}"
[ -f "$DAEMON" ] || { echo "ПРОБА КРАСНАЯ: механизма нет ($DAEMON)"; exit 1; }

# Маркер стенда стоит в пути каталога и в командных строках держателей замка:
# по нему и только по нему чистим (рядом может идти боевой прогон).
MARK="nmlk-$$-$RANDOM"
T="$(mktemp -d "/tmp/$MARK.XXXX")"
cleanup() { pkill -9 -f "$MARK" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT

total=0; failed=0
check() {  # $1 = имя случая, $2 = 0/1 (1 — выполнено), $3 = подробность при неудаче
    total=$((total + 1))
    if [ "$2" = 1 ]; then printf '  ок    %s\n' "$1"
    else printf '  ПЛОХО %s: %s\n' "$1" "$3"; failed=$((failed + 1)); fi
}
has() { grep -qF -- "$1" "$2" 2>/dev/null && echo 1 || echo 0; }
yes_if() { "$@" 2>/dev/null && echo 1 || echo 0; }
not() { ! "$@"; }
tg_text() { tr '\n' ' ' < "$T/tg.log" 2>/dev/null; }
show() { cat "$1" 2>/dev/null | tr '\n' ';'; }

LOCKS="$T/log/locks/services"
LOCK="$LOCKS/nightly-mutations.lock"
WANT="$LOCKS/nightly-mutations.want"
mkdir -p "$T/proekt/scripts" "$T/hb" "$LOCKS"
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
# Подставной прогон: отмечает старт, пишет срок мягкой остановки и свои
# дескрипторы, печатает итог из $T/itog.
cat > "$T/probe.sh" <<PROBE
: > "$T/started"
printf '%s\n' "\${MUTATION_DEADLINE_TS:-нет}" > "$T/deadline"
ls -l /proc/\$\$/fd > "$T/fds.log" 2>&1
cat "$T/itog"
PROBE
CLEAN='=== мутационная проба: покраснели как надо 9, дыр 0, из кэша 1 ==='
RED3='=== мутационная проба: покраснели как надо 7, дыр 3, из кэша 2 ==='

# Запускает демона фоном. $1 = ожидание замка в минутах, $2 = строка итога,
# $3 = строки harness.conf стенда сверх умолчаний (позднее перекрывает раннее).
start_daemon() {
    printf 'NIGHTLY_MUTATIONS_MAX_MIN=1\nNIGHTLY_MUTATIONS_ALERT_GAP_H=0\nNIGHTLY_MUTATIONS_LOCK_WAIT_MIN=%s\n%s\n' "$1" "${3:-}" > "$T/harness.conf"
    printf '%s\n' "$2" > "$T/itog"
    : > "$T/install.conf"; : > "$T/tg.log"; : > "$T/alert.log"
    rm -f "$T/started" "$T/daemon.log" "$T/deadline" "$T/fds.log"
    env -u NIGHTLY_MUTATIONS_MAX_MIN -u NIGHTLY_MUTATIONS_ALERT_GAP_H -u NIGHTLY_MUTATIONS_LOCK_WAIT_MIN \
        -u MUTATION_RUN_TIMEOUT_S -u MUTATION_SOFT_STOP_MIN_BEFORE -u MUTATION_DEADLINE_TS \
        -u HARNESS_HOME -u HARNESS_EVENTS_STATE -u SERVICE_LOCKS_SUBDIR \
        HEARTBEAT_DIR="$T/hb" LOG_DIR="$T/log" PROJECT_DIR="$T/proekt" \
        TG_LOG="$T/tg.log" ALERT_LOG="$T/alert.log" NIGHTLY_MUTATIONS_CMD="bash $T/probe.sh" \
        HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
        bash "$DAEMON" > "$T/daemon.log" 2>&1 < /dev/null &
    LAST_PID=$!
    STARTED_MS=$(( $(date +%s%N) / 1000000 ))
}

# Ждёт конца демона не дольше $1 секунд; код демона — в DAEMON_RC (KILL — убит стендом),
# время от старта — в TOOK_MS (секунды SECONDS целые и дают ложную секунду).
wait_daemon() {
    local i=0
    while kill -0 "$LAST_PID" 2>/dev/null && [ "$i" -lt $(($1 * 10)) ]; do sleep 0.1; i=$((i + 1)); done
    if kill -0 "$LAST_PID" 2>/dev/null; then
        kill -9 "$LAST_PID" 2>/dev/null; wait "$LAST_PID" 2>/dev/null; DAEMON_RC=KILL; return 1
    fi
    wait "$LAST_PID"; DAEMON_RC=$?; TOOK_MS=$(( $(date +%s%N) / 1000000 - STARTED_MS ))
}

hold_lock() {  # $1 = секунд держать замок, $2 = метка держателя
    flock "$LOCK" bash -c 'exec -a "$0" sleep "$1"' "$MARK-$2" "$1" &
    disown
    sleep 0.5
}
want_held() { ! flock -n "$WANT" true 2>/dev/null; }   # успех — замок .want кем-то держится
deadline() { cat "$T/deadline" 2>/dev/null; }

# (1) Замок держат дольше ожидания (0.03 мин = 1.8 с): прогон не стартует,
# но демон ждал, ставил .want, сказал владельцу и вышел кодом ≠ 0.
hold_lock 30 hold1
start_daemon 0.03 "$CLEAN"
sleep 0.7
check "1 пока ждёт замок, замок .want держится (flock -n не берётся)" "$(yes_if want_held)" \
      "не держится (файл $([ -e "$WANT" ] && echo есть || echo нет)); демон: $(tail -2 "$T/daemon.log" 2>/dev/null | tr '\n' '|')"
wait_daemon 20
check "1 демон кончился сам" "$(yes_if [ "$DAEMON_RC" != KILL ])" "висел дольше 20 с"
check "1 ждал замок, а не вышел сразу (≥ 1 с)" "$(yes_if [ "${TOOK_MS:-0}" -ge 1000 ])" "кончился за ${TOOK_MS:-?} мс"
check "1 прогон не стартовал" "$(yes_if [ ! -e "$T/started" ])" "подставной прогон пошёл при занятом замке"
check "1 код ≠ 0" "$(yes_if [ "$DAEMON_RC" != 0 ])" "код $DAEMON_RC"
check "1 весть с ключом nightly-mutations:broken" "$(has 'nightly-mutations:broken' "$T/tg.log")" "весть: «$(tg_text)»"
check "1 весть «не стартовал: замок занят»" "$(has 'не стартовал: замок занят' "$T/tg.log")" "весть: «$(tg_text)»"
check "1 после выхода замок .want не держится" "$(yes_if not want_held)" "держится"
pkill -9 -f "$MARK-hold1" 2>/dev/null; sleep 0.3

# (2) Замок отпускают через ~2 с при ожидании 0.1 мин (6 с): прогон идёт, вести нет.
hold_lock 2 hold2
start_daemon 0.1 "$CLEAN"
wait_daemon 20
check "2 замок отпущен в пределах ожидания: прогон стартовал" "$(yes_if [ -e "$T/started" ])" \
      "прогона не было; демон: $(tail -2 "$T/daemon.log" 2>/dev/null | tr '\n' '|')"
check "2 код 0" "$(yes_if [ "$DAEMON_RC" = 0 ])" "код $DAEMON_RC"
check "2 вести нет" "$(yes_if [ ! -s "$T/tg.log" ])" "весть: «$(tg_text)»"
check "2 замок .want не держится" "$(yes_if not want_held)" "держится"
check "2 (9) прибор свои дескрипторы видит (fds.log не пуст)" "$(yes_if grep -q -- '->' "$T/fds.log")" \
      "fds.log: «$(show "$T/fds.log")»"
check "2 (9) прибор не наследует дескрипторов замков" \
      "$(yes_if not grep -qE 'nightly-mutations\.(lock|want)|mutations-changed\.lock' "$T/fds.log")" \
      "у прибора: «$(grep -E 'mutations' "$T/fds.log" 2>/dev/null | tr '\n' ';')»"
pkill -9 -f "$MARK-hold2" 2>/dev/null

# (3) Зелёный полный прогон гасит свои карточки; остатки снятого сторожа
# mutations-changed не его забота (их гасит alert-task.py --drain) — не трогает.
printf 'mech/a.sh\n' > "$T/log/mutations-changed.red"
printf 'Мутации затронутых механизмов: mech/a.sh (дыр 1).\n' > "$T/log/mutations-changed.pending"
start_daemon 0.03 "$CLEAN"
wait_daemon 20
check "3 зелёный итог: alert-task --resolve nightly-mutations:red" \
      "$(yes_if grep -qE -- '--resolve nightly-mutations:red( |$)' "$T/alert.log")" \
      "писатель звали: «$(show "$T/alert.log")»"
check "3 зелёный итог: alert-task --resolve nightly-mutations:broken" \
      "$(yes_if grep -qE -- '--resolve nightly-mutations:broken( |$)' "$T/alert.log")" \
      "писатель звали: «$(show "$T/alert.log")»"
check "3 зелёный итог: mutations-changed:red не тронут" \
      "$(grep -q 'mutations-changed' "$T/alert.log" 2>/dev/null && echo 0 || echo 1)" "писатель звали: «$(show "$T/alert.log")»"
check "3 зелёный итог: mutations-changed.red цел" "$(has 'mech/a.sh' "$T/log/mutations-changed.red")" \
      "в .red: «$(show "$T/log/mutations-changed.red")»"
check "3 зелёный итог: mutations-changed.pending цел" "$(has 'mech/a.sh' "$T/log/mutations-changed.pending")" \
      "в .pending: «$(show "$T/log/mutations-changed.pending")»"

# (4) Красный полный прогон красное лёгкого демона не трогает.
printf 'mech/a.sh\n' > "$T/log/mutations-changed.red"
printf 'Мутации затронутых механизмов: mech/a.sh (дыр 1).\n' > "$T/log/mutations-changed.pending"
start_daemon 0.03 "$RED3"
wait_daemon 20
check "4 красный итог: mutations-changed.red цел" "$(has 'mech/a.sh' "$T/log/mutations-changed.red")" \
      "в .red: «$(show "$T/log/mutations-changed.red")»"
check "4 красный итог: mutations-changed:red не погашен" \
      "$(grep -q 'mutations-changed:red' "$T/alert.log" 2>/dev/null && echo 0 || echo 1)" "писатель звали: «$(show "$T/alert.log")»"
check "4 красный итог: mutations-changed.pending цел" "$(has 'mech/a.sh' "$T/log/mutations-changed.pending")" \
      "в .pending: «$(show "$T/log/mutations-changed.pending")»"
rm -f "$T/log/mutations-changed.pending"

# (5) (3) Убитый -9 во время ожидания демон флага не оставляет: замок .want
# снимает ядро вместе с процессом, а мёртвый файл флагом не считается.
hold_lock 30 hold5
start_daemon 0.5 "$CLEAN"
sleep 0.7
check "5 до убийства: замок .want держится" "$(yes_if want_held)" \
      "не держится; демон: $(tail -2 "$T/daemon.log" 2>/dev/null | tr '\n' '|')"
kill -9 "$LAST_PID" 2>/dev/null; wait "$LAST_PID" 2>/dev/null
sleep 0.3
check "5 после kill -9 замок .want свободен (flock -n берётся)" "$(yes_if not want_held)" \
      "держится: $(fuser "$WANT" 2>&1 | tr '\n' ' ')"
pkill -9 -f "$MARK-hold5" 2>/dev/null; sleep 0.3

# (6) (5) Запас мягкой остановки не меньше MUTATION_RUN_TIMEOUT_S + 60 с:
# мутация, начатая у срока, обязана кончиться до жёсткого предела.
start_daemon 0.03 "$CLEAN" 'NIGHTLY_MUTATIONS_MAX_MIN=1140
MUTATION_RUN_TIMEOUT_S=2400'
S6=$(( STARTED_MS / 1000 ))
wait_daemon 20
D6="$(deadline)"
check "6 (5) прогон стартовал и получил срок" "$(yes_if [ "$D6" -ge "$S6" ] 2>/dev/null)" \
      "срок «$D6», старт $S6; демон: $(tail -2 "$T/daemon.log" 2>/dev/null | tr '\n' '|')"
check "6 (5) срок не позже старта + 1140×60 − 2460 с" \
      "$(yes_if [ "$D6" -le $((S6 + 1 + 1140 * 60 - 2460)) ] 2>/dev/null)" "срок «$D6», старт $S6, предел $((S6 + 1 + 1140 * 60 - 2460))"

# (7) (12) Срок полного прогона считается от старта процесса, а не от взятия
# замка: ожидание замка съедает срок. Запас в обеих формулах 120 с
# (15→2 мин и 1+60 с), чтобы судилось только начало отсчёта.
hold_lock 4 hold7
start_daemon 0.2 "$CLEAN" 'NIGHTLY_MUTATIONS_MAX_MIN=2.5
MUTATION_SOFT_STOP_MIN_BEFORE=2
MUTATION_RUN_TIMEOUT_S=1'
S7=$(( STARTED_MS / 1000 ))
wait_daemon 30
D7="$(deadline)"
check "7 (12) ждал замок (≥ 2 с) и прогон стартовал" "$(yes_if [ "${TOOK_MS:-0}" -ge 2000 -a -e "$T/started" ])" \
      "за ${TOOK_MS:-?} мс, прогон $([ -e "$T/started" ] && echo был || echo не был); демон: $(tail -2 "$T/daemon.log" 2>/dev/null | tr '\n' '|')"
check "7 (12) срок не позже старта процесса + 2.5×60 − 120 с" \
      "$(yes_if [ "$D7" -le $((S7 + 1 + 150 - 120)) -a "$D7" -ge "$S7" ] 2>/dev/null)" \
      "срок «$D7», старт процесса $S7, предел $((S7 + 1 + 150 - 120))"
pkill -9 -f "$MARK-hold7" 2>/dev/null

if [ "$failed" = 0 ]; then echo "ПРОБА ЗЕЛЁНАЯ: случаев $total"; exit 0; fi
echo "ПРОБА КРАСНАЯ: неудач $failed из $total"; exit 1
