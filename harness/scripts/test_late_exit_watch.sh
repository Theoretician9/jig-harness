#!/usr/bin/env bash
#
# Живая проверка дозора позднего выхода (session-warden.sh --досмотр-выхода).
#
# Больной случай 13.08.2026 (снимок владельца): «/exit» подан в 23:50:32,
# сессия не закрылась ни за 210с, ни за досмотр 300с — сторож сдал смену и
# вышел. Выход случился позже, панель закрылась вместе с агентом, и преемника
# не поднимал НИКТО до планового прогона cron в 00:10: десять минут без агента,
# сообщение владельца всё это время лежало в inbox.
#
# Проверка создаёт своё условие: поднимает свою tmux-сессию с подставным
# «агентом», ставит дозор, УБИВАЕТ сессию — и смотрит, поднял ли дозор
# преемника сам. В боевую сессию агента не пишет ни байта: своё имя панели,
# свой LOG_DIR, свой PROJECT_DIR, канал заведомо не настроен.
#
# Запуск: bash harness/demons/test_late_exit_watch.sh
set -euo pipefail

# Переехал из harness/demons/ 10.09.2026 (ревизия): тест в каталоге демонов не
# запускал никто — ни cron, ни systemd, ни ворота. Теперь он рядом с другими
# проверками и входит в «vorota.sh --всё».
DAEMON="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/harness/demons/session-warden.sh"
T=$(mktemp -d); ok=1
PANE="warden-latewatch-$$"
# Уборка снимает дозор ГРУППОЙ и только потом сносит каталог: setsid уводит
# демона в свою сессию, и каталог, вынутый из-под живого процесса, оставляет
# его писать в никуда до конца прогона ворот (ревью 10.09.2026, ops-18).
снять_дозор() {
    local sid; sid=$(cat "$T/дозор.pid" 2>/dev/null || true)
    [[ "$sid" =~ ^[0-9]+$ ]] && kill -- "-$sid" 2>/dev/null || true
}
trap 'снять_дозор; tmux kill-session -t "$PANE" 2>/dev/null || true; rm -rf "$T"' EXIT

# Подставной «агент»: имя файла и есть pane_current_command, поэтому просто
# копия sleep под именем claude — start_agent ждёт в панели именно claude|node.
cp /bin/sleep "$T/claude"

cat > "$T/harness.conf" <<CONF
LOG_DIR="$T/log"
PROJECT_DIR="$T/project"
TMUX_SESSION="$PANE"
AGENT_START_CMD="$T/claude 600"
AGENT_START_TIMEOUT_SEC=30
LATE_EXIT_WATCH_SEC=120
HEARTBEAT_DIR="$T/log/heartbeat"
SERVICE_LOCKS_SUBDIR="проверка"
# Делитель замера окна к дозору отношения не имеет, но без него сторож
# законно валится ещё в прологе — конфиг проверки обязан быть валидным.
CTX_WINDOW_TOKENS=400000  # не-секрет: размер окна модели в токенах, а не ключ
TG_CHAT_ID=""
TG_TOKEN_FILE="/nonexistent"
CONF
mkdir -p "$T/log" "$T/project"

итог() { if [ "$1" = ок ]; then printf '  ок    %s\n' "$2"; else printf '  ПЛОХО %s\n' "$2"; ok=0; fi; }

# ── условие: живая панель с «агентом», которая сейчас закроется ─────────────
tmux new-session -d -s "$PANE" "$T/claude 600"
sleep 1
[ "$(tmux display-message -p -t "$PANE" '#{pane_current_command}')" = claude ] \
    || { echo "ПЛОХО: подставной агент не встал в панель — проверять нечего"; exit 1; }

# PID лидера новой сессии пишет он сам: `$!` здесь — родительский setsid,
# который тут же уходит, и kill по нему бьёт мимо живого дозора.
env -u HARNESS_CONF HARNESS_CONF="$T/harness.conf" \
    setsid bash -c 'echo $$ > "$1"; exec bash "$2" --досмотр-выхода "$3"' \
        _ "$T/дозор.pid" "$DAEMON" "проверка-$$" \
    >> "$T/дозор.log" 2>&1 < /dev/null &
sleep 3

# ── свойство 1: дозор не держит лок сторожа ────────────────────────────────
LOCKF="$T/log/locks/проверка/session-warden.lock"
if [ -e "$LOCKF" ] && fuser "$LOCKF" 2>/dev/null | grep -q '[0-9]'; then
    итог плохо "дозор держит лок сторожа — плановые прогоны встанут на три цикла cron"
else
    итог ок "дозор лок сторожа не держит — плановые прогоны cron идут своим чередом"
fi

# ── больной случай: поздний выход, панель закрывается вместе с агентом ──────
tmux kill-session -t "$PANE" 2>/dev/null || true
waited=0
while [ "$waited" -lt 60 ]; do
    sleep 3; waited=$((waited + 3))
    [ "$(tmux display-message -p -t "$PANE" '#{pane_current_command}' 2>/dev/null || echo -)" = claude ] && break
done

if [ "$(tmux display-message -p -t "$PANE" '#{pane_current_command}' 2>/dev/null || echo -)" = claude ]; then
    итог ок "БОЛЬНОЙ СЛУЧАЙ: сессия закрылась после сдачи ротации — дозор поднял преемника сам за ${waited}с (было: ждали cron до 10 мин)"
else
    итог плохо "сессия закрылась, а преемника нет — дыра до планового прогона осталась"
fi

grep -q '"event":"late_exit_caught"' "$T/log/rotation.jsonl" 2>/dev/null \
    && итог ок "поздний выход записан в rotation.jsonl — след есть" \
    || итог плохо "в rotation.jsonl нет late_exit_caught — событие не оставило следа"

# ── свойство 4: уборка действительно снимает дозор ─────────────────────────
# Иначе прогон ворот заканчивается, а демон живёт с вынутым из-под него
# каталогом — и следующий прогон встречает чужого сироту.
снять_дозор
sleep 2
watch_sid=$(cat "$T/дозор.pid" 2>/dev/null || true)
if [ -n "$watch_sid" ] && kill -0 "$watch_sid" 2>/dev/null; then
    итог плохо "дозор пережил уборку (pid $watch_sid) — сирота останется до перезагрузки"
else
    итог ок "уборка сняла дозор группой — сирот после прогона нет"
fi

(( ok )) && { echo "SELFTEST: зелёный (4 свойства дозора позднего выхода, первым — больной случай)"; exit 0; }
# Красный тест без лога подозреваемого — повод для второго прогона, а не разбор.
echo "── лог дозора ──"; cat "$T/дозор.log" 2>/dev/null || echo "(лога нет вовсе)"
echo "SELFTEST: КРАСНЫЙ"; exit 1
