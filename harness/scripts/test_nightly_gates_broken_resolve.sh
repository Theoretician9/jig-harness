#!/usr/bin/env bash
#
# test_nightly_gates_broken_resolve.sh — проба гашения тревог ночного прогона
# ворот (hr.alert-nightly-gates-broken). broken = «прогон оборван», red = «есть
# красные»: дошедший до итога прогон гасит broken при любом числе красных, red
# гасится только при красных 0, оборванный пределом прогон broken не гасит.
# При строке «=== карантин» red судится по «новых красных» + «бед карантина»,
# а в весть идут только новые красные и беды карантина.
# Код ворот ≠0 без строки «=== карантин» (суд упал) — тревога, как красный.
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
# Прогоны с карантином (hr.quarantine-red-checks): ворота печатают «ПЛОХО ·» и
# для красных под карантином; демон судит по строке «=== карантин».
cat > "$T/quar-held.sh" <<'QHELD'
printf '%s\n' '  ПЛОХО · второй (код 1)' '=== итог: зелёных 99, красных 1 ===' \
    '  карантин · второй — красный, карточка hr.x, до 2026-11-20' \
    '=== карантин: красных всего 1, в карантине 1, новых красных 0, бед карантина 0 ==='
QHELD
cat > "$T/quar-new.sh" <<'QNEW'
printf '%s\n' '  ПЛОХО · второй (код 1)' '  ПЛОХО · третий (код 2)' \
    '=== итог: зелёных 98, красных 2 ===' \
    '  карантин · второй — красный, карточка hr.x, до 2026-11-20' \
    '=== карантин: красных всего 2, в карантине 1, новых красных 1, бед карантина 0 ==='
exit 1
QNEW
cat > "$T/quar-defect.sh" <<'QBAD'
printf '%s\n' '=== итог: зелёных 100, красных 0 ===' \
    '  ПЛОХО · карантин: пятый — срок вышел 2026-11-12 (карточка hr.y)' \
    '=== карантин: красных всего 0, в карантине 0, новых красных 0, бед карантина 1 ==='
exit 1
QBAD
# Суд карантина упал: итог «красных 0», строки «=== карантин» нет, а код
# ворот ненулевой — молчание здесь прячет поломку самого суда.
cat > "$T/quar-crash.sh" <<'QCRASH'
printf '%s\n' '=== итог: зелёных 100, красных 0 ===' 'Traceback (most recent call last):'
exit 1
QCRASH
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

# (4) БОЛЬНОЙ: все красные под карантином — тревоги нет, red гаснет.
run_daemon 5 quar-held.sh
check "4 БОЛЬНОЙ: все красные в карантине — вести нет" "$([ -s "$T/tg.log" ] && echo 0 || echo 1)" \
      "весть: «$(tr '\n' ' ' < "$T/tg.log")»"
check "4 все красные в карантине — red погашен" "$(called '--resolve nightly-gates:red')" \
      "вызовы писателя: «$(calls)»"

# (5) БОЛЬНОЙ: новый красный рядом с карантинным — в вести только новый.
run_daemon 5 quar-new.sh
check "5 новый красный — весть с ключом nightly-gates:red" "$(sent 'nightly-gates:red')" \
      "в вести: «$(tr '\n' ' ' < "$T/tg.log")»"
check "5 новый красный назван" "$(sent 'третий')" "в вести: «$(tr '\n' ' ' < "$T/tg.log")»"
check "5 БОЛЬНОЙ: карантинный красный в вести НЕ назван" "$(sent 'второй' | tr 01 10)" \
      "в вести: «$(tr '\n' ' ' < "$T/tg.log")»"
check "5 в вести одна строка «В карантине 1»" "$(sent 'В карантине 1')" \
      "в вести: «$(tr '\n' ' ' < "$T/tg.log")»"
check "5 новый красный — red НЕ погашен" "$(not_called '--resolve nightly-gates:red')" \
      "вызовы писателя: «$(calls)»"

# (6) БОЛЬНОЙ: красных 0, но беда карантина (срок вышел) — это тревога.
run_daemon 5 quar-defect.sh
check "6 БОЛЬНОЙ: беда карантина — весть с ключом nightly-gates:red" "$(sent 'nightly-gates:red')" \
      "в вести: «$(tr '\n' ' ' < "$T/tg.log")»"
check "6 беда карантина названа" "$(sent 'срок вышел')" "в вести: «$(tr '\n' ' ' < "$T/tg.log")»"
check "6 беда карантина — red НЕ погашен" "$(not_called '--resolve nightly-gates:red')" \
      "вызовы писателя: «$(calls)»"

# (7) БОЛЬНОЙ: код ворот ненулевой без строки суда карантина — тревога, не тишина.
run_daemon 5 quar-crash.sh
check "7 БОЛЬНОЙ: суд упал — весть с ключом nightly-gates:red" "$(sent 'nightly-gates:red')" \
      "в вести: «$(tr '\n' ' ' < "$T/tg.log")»"
check "7 суд упал — названо: нет строки суда карантина" "$(sent 'нет строки суда карантина')" \
      "в вести: «$(tr '\n' ' ' < "$T/tg.log")»"
check "7 суд упал — red НЕ погашен" "$(not_called '--resolve nightly-gates:red')" \
      "вызовы писателя: «$(calls)»"

if [ "$failed" = 0 ]; then echo "ПРОБА ЗЕЛЁНАЯ: случаев $total"; exit 0; fi
echo "ПРОБА КРАСНАЯ: неудач $failed из $total"; exit 1
