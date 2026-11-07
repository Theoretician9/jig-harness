#!/usr/bin/env bash
#
# test_nightly_gates_broken_resolve.sh — проба гашения тревог ночного прогона
# ворот (hr.alert-nightly-gates-broken). broken = «прогон оборван», red = «есть
# красные»: дошедший до итога прогон гасит broken при любом числе красных, red
# гасится только при красных 0, оборванный пределом прогон broken не гасит.
# Судит harness/demons/nightly-gates.sh на стенде: свой PROJECT_DIR, подставные
# tg_send.sh и alert-task.py (пишут доводы в свои журналы), подставной прогон,
# предел из harness.conf СТЕНДА. Дверь для мутаций: NIGHTLY_GATES_UNDER_TEST.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
DAEMON="${NIGHTLY_GATES_UNDER_TEST:-$HERE/../harness/demons/nightly-gates.sh}"
[ -f "$DAEMON" ] || { echo "ПРОБА КРАСНАЯ: механизма нет ($DAEMON)"; exit 1; }

# Маркер стенда в пути каталога: по нему чистим потомков спящего прогона,
# не задевая боевой ночной прогон рядом.
MARK="ngbroken-$$-$RANDOM"
T="$(mktemp -d "/tmp/$MARK.XXXX")"
cleanup() {
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
# Демон зовёт писателя как python3 <файл> — подставной пишет доводы построчно.
cat > "$T/proekt/scripts/alert-task.py" <<'AT'
import os, sys
with open(os.environ["ALERT_LOG"], "a", encoding="utf-8") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
AT

cat > "$T/red.sh" <<'RED'
printf '  ок · первый\n  ПЛОХО · второй (код 1)\n=== итог: зелёных 99, красных 3 ===\n'
exit 3
RED
cat > "$T/green.sh" <<'GREEN'
printf '  ок · первый\n=== итог: зелёных 105, красных 0 ===\n'
GREEN
# Висящий прогон: потомок с маркером в командной строке, ждём его — предел
# обрезает, итога нет, timeout отдаёт 124.
cat > "$T/sleepy.sh" <<SLEEPY
printf '  ок · первый\n'
bash -c 'sleep 60; true' "$MARK-child" &
wait
SLEEPY

# Гонит демона на стенде до конца (не дольше 30 с). $1 = предел в минутах
# (в harness.conf стенда), $2 = подставной прогон.
run_daemon() {
    printf 'NIGHTLY_GATES_MAX_MIN=%s\n' "$1" > "$T/harness.conf"
    : > "$T/install.conf"; : > "$T/tg.log"; : > "$T/alert.log"
    rm -f "$T/log"/nightly-gates-*.log
    env -u NIGHTLY_GATES_MAX_MIN -u HARNESS_HOME \
        HEARTBEAT_DIR="$T/hb" LOG_DIR="$T/log" PROJECT_DIR="$T/proekt" \
        TG_LOG="$T/tg.log" ALERT_LOG="$T/alert.log" NIGHTLY_GATES_CMD="bash $T/$2" \
        HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
        timeout -k 5 30 bash "$DAEMON" > "$T/daemon.log" 2>&1 < /dev/null
    DAEMON_RC=$?
    pkill -9 -f "$MARK-child" 2>/dev/null
}

# Строка писателя целиком: «--resolve nightly-gates:red» не должен совпасть
# с чужим ключом по подстроке.
called() { grep -qx -- "$1" "$T/alert.log" 2>/dev/null && echo 1 || echo 0; }
not_called() { called "$1" | tr 01 10; }
sent() { grep -q -- "$1" "$T/tg.log" 2>/dev/null && echo 1 || echo 0; }
calls() { tr '\n' ';' < "$T/alert.log"; }

# (1) БОЛЬНОЙ: красные есть каждую ночь — дошедший до итога прогон обязан
# гасить broken, иначе тревога «прогон оборван» не гаснет никогда.
run_daemon 5 red.sh
check "1 БОЛЬНОЙ: итог с красными 3 — код 0" "$([ "$DAEMON_RC" = 0 ] && echo 1 || echo 0)" "код $DAEMON_RC"
check "1 БОЛЬНОЙ: итог с красными 3 — broken погашен" "$(called '--resolve nightly-gates:broken')" \
      "вызовы писателя: «$(calls)»"
check "1 итог с красными 3 — red НЕ погашен" "$(not_called '--resolve nightly-gates:red')" \
      "вызовы писателя: «$(calls)»"
check "1 итог с красными 3 — весть с ключом nightly-gates:red" "$(sent 'nightly-gates:red')" \
      "в вести: «$(tr '\n' ' ' < "$T/tg.log")»"

# (2) Итог без красных — гасятся обе.
run_daemon 5 green.sh
check "2 итог с красными 0 — код 0" "$([ "$DAEMON_RC" = 0 ] && echo 1 || echo 0)" "код $DAEMON_RC"
check "2 итог с красными 0 — broken погашен" "$(called '--resolve nightly-gates:broken')" \
      "вызовы писателя: «$(calls)»"
check "2 итог с красными 0 — red погашен" "$(called '--resolve nightly-gates:red')" \
      "вызовы писателя: «$(calls)»"
check "2 итог с красными 0 — вести нет" "$([ -s "$T/tg.log" ] && echo 0 || echo 1)" \
      "весть: «$(tr '\n' ' ' < "$T/tg.log")»"

# (3) БОЛЬНОЙ: оборван пределом 0.05 мин (3 с) — broken НЕ гаснет, а шлётся.
run_daemon 0.05 sleepy.sh
check "3 БОЛЬНОЙ: оборван пределом — демон назвал предел" \
      "$(grep -q 'упёрся в предел' "$T/daemon.log" 2>/dev/null && echo 1 || echo 0)" \
      "код $DAEMON_RC, журнал: «$(tail -2 "$T/daemon.log" | tr '\n' ' ')»"
check "3 БОЛЬНОЙ: оборван пределом — broken НЕ погашен" "$(not_called '--resolve nightly-gates:broken')" \
      "вызовы писателя: «$(calls)»"
check "3 оборван пределом — red НЕ погашен" "$(not_called '--resolve nightly-gates:red')" \
      "вызовы писателя: «$(calls)»"
check "3 оборван пределом — весть с ключом nightly-gates:broken" "$(sent 'nightly-gates:broken')" \
      "в вести: «$(tr '\n' ' ' < "$T/tg.log")»"

if [ "$failed" = 0 ]; then echo "ПРОБА ЗЕЛЁНАЯ: случаев $total"; exit 0; fi
echo "ПРОБА КРАСНАЯ: неудач $failed из $total"; exit 1
