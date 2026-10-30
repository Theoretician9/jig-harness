#!/usr/bin/env bash
#
# test_nightly_gates_live_edit.sh — проба «правка демона на ходу не роняет
# идущий ночной прогон» и «прогон без итога назван явно»
# (hr.nightly-gates-script-edit-breaks-run).
#
# Стенд: копия демона и scripts/lib/config.sh в своём дереве (правится КОПИЯ,
# боевой файл не трогается), свой PROJECT_DIR, LOG_DIR, HEARTBEAT_DIR,
# подставной tg_send.sh и подставной прогон вместо gates.sh. Демон берётся
# рядом с пробой (подмена «из-копии» в probe-mutations.yaml).
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
SRC_DAEMON="$HERE/../harness/demons/nightly-gates.sh"
SRC_CONFIG="$HERE/lib/config.sh"
[ -f "$SRC_DAEMON" ] || { echo "ПРОБА КРАСНАЯ: механизма нет ($SRC_DAEMON)"; exit 1; }

# Маркер стенда стоит в пути каталога: по нему и только по нему чистим.
MARK="ngedit-$$-$RANDOM"
T="$(mktemp -d "/tmp/$MARK.XXXX")"
cleanup() { pkill -9 -f "$MARK" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT

total=0; failed=0
check() {  # $1 = имя случая, $2 = 0/1 (1 — выполнено), $3 = подробность при неудаче
    total=$((total + 1))
    if [ "$2" = 1 ]; then printf '  ок    %s\n' "$1"
    else printf '  ПЛОХО %s: %s\n' "$1" "$3"; failed=$((failed + 1)); fi
}
has() { grep -q -- "$1" "$2" 2>/dev/null && echo 1 || echo 0; }

DAEMON="$T/tree/harness/demons/nightly-gates.sh"
mkdir -p "$T/tree/harness/demons" "$T/tree/scripts/lib" "$T/proekt/scripts" "$T/hb" "$T/log"
cp "$SRC_CONFIG" "$T/tree/scripts/lib/config.sh"
cat > "$T/proekt/scripts/tg_send.sh" <<'TG'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TG_LOG"
TG
chmod +x "$T/proekt/scripts/tg_send.sh"
: > "$T/install.conf"; : > "$T/harness.conf"

# Прогон, который даёт пробе окно для правки: знак «начал», сон, зелёный итог.
cat > "$T/sleepy.sh" <<SLEEPY
: > "$T/started"
sleep 3
printf '=== итог: зелёных 5, красных 0 ===\n'
SLEEPY
# Прогон без итоговой строки (оборвался, упал, вывел мусор).
printf "printf '  ок · первый\\\\n'\n" > "$T/noitog.sh"

run_daemon() {  # $1 = подставной прогон; фоном, pid в LAST_PID
    cp "$SRC_DAEMON" "$DAEMON"
    rm -f "$T/started" "$T/log"/nightly-gates-*.log; : > "$T/tg.log"
    env -u HARNESS_HOME -u NIGHTLY_GATES_MAX_MIN \
        HEARTBEAT_DIR="$T/hb" LOG_DIR="$T/log" PROJECT_DIR="$T/proekt" \
        TG_LOG="$T/tg.log" NIGHTLY_GATES_CMD="bash $T/$1" \
        HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
        bash "$DAEMON" > "$T/daemon.log" 2>&1 < /dev/null &
    LAST_PID=$!
}

wait_pid() {  # $1 = pid, $2 = предел в секундах; код демона в DAEMON_RC
    local i=0
    while kill -0 "$1" 2>/dev/null && [ "$i" -lt $(($2 * 10)) ]; do sleep 0.1; i=$((i + 1)); done
    if kill -0 "$1" 2>/dev/null; then kill -9 "$1" 2>/dev/null; wait "$1" 2>/dev/null; DAEMON_RC=KILL; return; fi
    wait "$1"; DAEMON_RC=$?
}

# (1) БОЛЬНОЙ: файл демона правят НА МЕСТЕ (тот же inode, как пишет редактор),
# пока прогон идёт. Правка — снятие шапки комментариев: всё ниже сдвигается
# назад, и оболочка, читающая файл по смещению, попадает в чужую середину.
run_daemon sleepy.sh
i=0; while [ ! -e "$T/started" ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
check "1 стенд: прогон начался" "$([ -e "$T/started" ] && echo 1 || echo 0)" "знака «начал» нет за 10 с"
python3 -I - "$DAEMON" <<'EDIT'
import sys
path = sys.argv[1]
lines = open(path, encoding="utf-8").read().split("\n")
# шапка — комментарии после строки shebang
end = 1
while end < len(lines) and lines[end].startswith("#"):
    end += 1
with open(path, "r+", encoding="utf-8") as f:
    f.seek(0); f.write("\n".join(lines[:1] + lines[end:])); f.truncate()
EDIT
wait_pid "$LAST_PID" 30
check "1 БОЛЬНОЙ: правка на ходу — прогон дошёл до итога" \
      "$(has '=== итог: зелёных 5, красных 0 ===' "$T/daemon.log")" \
      "итога в журнале демона нет: «$(tail -3 "$T/daemon.log" | tr '\n' ' ')»"
check "1 БОЛЬНОЙ: правка на ходу — вердикт «красных нет»" \
      "$(has 'красных нет' "$T/daemon.log")" "вердикта нет"
check "1 БОЛЬНОЙ: правка на ходу — код 0" "$([ "$DAEMON_RC" = 0 ] && echo 1 || echo 0)" "код $DAEMON_RC"
check "1 БОЛЬНОЙ: правка на ходу — в журнале нет ошибок оболочки" \
      "$(grep -qE 'command not found|syntax error|unbound variable' "$T/daemon.log" && echo 0 || echo 1)" \
      "«$(grep -E 'command not found|syntax error|unbound variable' "$T/daemon.log" | head -1)»"
check "1 стенд: журнал прогона лёг в LOG_DIR стенда" \
      "$(ls "$T/log"/nightly-gates-*.log >/dev/null 2>&1 && echo 1 || echo 0)" "журнала в $T/log нет"

# (2) БОЛЬНОЙ: прогон кончился без итоговой строки — сигнал в журнал и задача модели.
run_daemon noitog.sh
wait_pid "$LAST_PID" 30
check "2 БОЛЬНОЙ: без итога — журнал «прогон закончился без итога»" \
      "$(has 'прогон закончился без итога' "$T/daemon.log")" \
      "«$(tail -3 "$T/daemon.log" | tr '\n' ' ')»"
check "2 БОЛЬНОЙ: без итога — задача nightly-gates:broken" "$(has 'nightly-gates:broken' "$T/tg.log")" \
      "в вести: «$(tr '\n' ' ' < "$T/tg.log")»"
check "2 БОЛЬНОЙ: без итога — код 1" "$([ "$DAEMON_RC" = 1 ] && echo 1 || echo 0)" "код $DAEMON_RC"
check "2 без итога — сигнал один, не дважды" \
      "$([ "$(grep -c 'прогон закончился без итога' "$T/daemon.log")" = 1 ] && echo 1 || echo 0)" \
      "строк: $(grep -c 'прогон закончился без итога' "$T/daemon.log")"

# (3) Зелёный прогон без правки сигнала «без итога» не даёт.
run_daemon sleepy.sh
wait_pid "$LAST_PID" 30
check "3 зелёный прогон: «без итога» нет" "$(has 'без итога' "$T/daemon.log" | tr 01 10)" \
      "ложный сигнал на зелёном"
check "3 зелёный прогон: вести нет" "$([ -s "$T/tg.log" ] && echo 0 || echo 1)" \
      "весть: «$(tr '\n' ' ' < "$T/tg.log")»"

if [ "$failed" = 0 ]; then echo "ПРОБА ЗЕЛЁНАЯ: случаев $total"; exit 0; fi
echo "ПРОБА КРАСНАЯ: неудач $failed из $total"; exit 1
