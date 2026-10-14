#!/usr/bin/env bash
# Проба сторожа сессий целым прогоном: «работы нет, смена стоит у приглашения».
#
# Больной случай 14.10.2026: очередь целиком заблокирована, смена закончила ход
# и ждёт, а владелец за 40 минут получил три ложных сигнала («смена не
# отчитывается… лимит», «работа встала», «возобновилась… просто молчала»), смена
# — пинок про статус каждый час. Чистые функции судятся самотестом сторожа;
# здесь судится ОБВЯЗКА: что из этих решений доходит до канала и до панели.
#
# Свой стенд: своя tmux-панель с подставным «агентом» (копия sleep под именем
# claude), свой LOG_DIR, свой канал (файл), своя карта. В боевую панель и в
# боевой канал проба не пишет ни байта.
#
# Имена переменных латиницей (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON="$HERE/../harness/demons/session-warden.sh"
[ -f "$DAEMON" ] || { echo "ПЛОХО: сторожа нет по пути раскладки проекта: $DAEMON"; exit 1; }

T=$(mktemp -d)
PANE="warden-blocked-$$"
cleanup() { tmux kill-session -t "$PANE" 2>/dev/null || true; rm -rf "$T"; }
trap cleanup EXIT

cp /bin/sleep "$T/claude"
mkdir -p "$T/project/scripts/lib" "$T/projects"
for f in work-queue.py limit_mark.py limit-in-log.py stall-in-log.py; do
    cp "$HERE/$f" "$T/project/scripts/"
done
cp "$HERE"/lib/*.py "$HERE/lib/config.sh" "$T/project/scripts/lib/"
cat > "$T/project/scripts/tg_send.sh" <<KANAL
#!/usr/bin/env bash
printf '%s\n' "\$1" >> "$T/kanal.txt"
KANAL
chmod +x "$T/project/scripts/tg_send.sh"
TR_DIR="$T/projects/${T//\//-}-project"
mkdir -p "$TR_DIR"
TRANSCRIPT="$TR_DIR/proba.jsonl"

# Пороги и умолчания — из конфига пакета, сверху — свои места и пороги стенда.
# Образец: в пакете он лежит рядом (harness/config), в проекте — только
# установленный (/etc/harness). Без полного набора ключей сторож падает на
# первом же незаданном (set -u), и проба судила бы падение, а не поведение.
SEED=""
for c in "$HERE/../config/harness.conf" "${HARNESS_CONF:-/etc/harness/harness.conf}"; do
    [ -f "$c" ] && { SEED="$c"; break; }
done
[ -n "$SEED" ] || { echo "ПЛОХО: образца harness.conf нет ни в пакете, ни в /etc/harness — проверять нечем"; exit 1; }
cp "$SEED" "$T/harness.conf"
cat >> "$T/harness.conf" <<CONF
AGENT_USER="$(id -un)"
LOG_DIR="$T/log"
PROJECT_DIR="$T/project"
STATE_DIR="$T/state"
TMUX_SESSION="$PANE"
AGENT_START_CMD="$T/claude 600"
SERVICE_LOCKS_SUBDIR="проверка"
CTX_WINDOW_TOKENS=400000  # не-секрет: размер окна модели в токенах, а не ключ
SILENCE_MAX_MIN=30
PULSE_MAX_MIN=30
PULSE_OWNER_MIN=60
LIMIT_STALL_MIN=20
TG_CHAT_ID=""
TG_TOKEN_FILE="/nonexistent"
CONF

tmux new-session -d -s "$PANE" "$T/claude 3600"
sleep 1
[ "$(tmux display-message -p -t "$PANE" '#{pane_current_command}')" = claude ] \
    || { echo "ПЛОХО: подставной агент не встал в панель — проверять нечего"; exit 1; }

ok=1
NOW=$(date +%s)
ago() { echo $(( NOW - $1 * 60 )); }
iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ; }

# Стенд с нуля. $1 = карта: blocked | work
stand() {
    rm -rf "$T/log" "$T/state" "$T/kanal.txt"
    mkdir -p "$T/log/inbox/обработано" "$T/state/panel-state"
    : > "$TRANSCRIPT"
    if [ "$1" = work ]; then
        printf '%s\n' '{"queue": [], "lines": [{"id": "hr", "tasks": [{"id": "hr.a", "status": "plan", "blocked": "ждёт владельца"}, {"id": "hr.b", "status": "wip"}]}]}' \
            > "$T/project/dev-map.yaml"
    else
        printf '%s\n' '{"queue": ["hr.a"], "lines": [{"id": "hr", "tasks": [{"id": "hr.a", "status": "plan", "blocked": "ждёт владельца: решение"}]}]}' \
            > "$T/project/dev-map.yaml"
    fi
}
answer() { printf '{"type":"assistant","timestamp":"%s"}\n' "$(iso "$(ago "$1")")" >> "$TRANSCRIPT"; }
refusal() {
    printf '{"type":"assistant","error":"rate_limit","apiErrorStatus":429,"isApiErrorMessage":true,"timestamp":"%s"}\n' \
        "$(iso "$(ago "$1")")" >> "$TRANSCRIPT"
}
touched() { touch -d "@$(ago "$1")" "$TRANSCRIPT"; }        # mtime транскрипта, мин назад
report() { ago "$1" > "$T/log/last_report"; touch -d "@$(ago "$1")" "$T/log/last_report"; }
turn_end() { echo $(( $(ago "$1") + ${2:-0} )) > "$T/log/turn-end.stamp"; }
owner_said() { echo "слово" > "$T/log/inbox/обработано/$1.txt"; touch -d "@$(ago "$1")" "$T/log/inbox/обработано/$1.txt"; }
mark() { printf '%s %s\n' "$(ago "$2")" "$1" > "$T/log/лимит-подписки.состояние"; }

run() {
    env -u TMUX HARNESS_CONF="$T/harness.conf" HARNESS_INSTALL_CONF="$T/harness.conf" \
        HARNESS_PANEL_STATE="$T/state/panel-state" CLAUDE_PROJECTS_DIR="$T/projects" \
        bash "$DAEMON" >> "$T/log/warden.out" 2>&1 < /dev/null
}
said() { local n; n=$(grep -c -- "$1" "$T/kanal.txt" 2>/dev/null); echo "${n:-0}"; }
event() { local n; n=$(grep -c "\"event\":\"$1\"" "$T/log/rotation.jsonl" 2>/dev/null); echo "${n:-0}"; }
check() {  # $1=ждём $2=вышло $3=зачем
    if [ "$1" = "$2" ]; then printf '  ок    %s\n' "$3"
    else printf '  ПЛОХО %s: вышло «%s», ждали «%s»\n' "$3" "$2" "$1"; ok=0
        sed 's/^/        /' "$T/log/warden.out" | tail -15; fi
}

# ── пульс ───────────────────────────────────────────────────────────────────
# БОЛЬНОЙ СЛУЧАЙ п.1: ход закончен после последнего ответа, владелец молчит
# с прошлого отчёта (64 мин), работы нет — ни пинка, ни слова владельцу.
stand blocked; answer 5; turn_end 5 3; touched 5; report 64; owner_said 120; run
check 0 "$(event pulse_kick)" "БОЛЬНОЙ СЛУЧАЙ п.1: смена у приглашения, работы нет — пульс не пинает"
check 0 "$(said 'не отчитывается')" "БОЛЬНОЙ СЛУЧАЙ п.1: и владельцу о молчании не пишет"

# Владелец написал ПОСЛЕ последнего отчёта: смена работает над его словом,
# карточки под это нет — пульс как прежде.
stand blocked; answer 5; turn_end 5 3; touched 5; report 64; owner_said 50; run
check 1 "$(event pulse_kick)" "слово владельца новее отчёта — пульс пинает, как прежде"
check 1 "$(said 'не отчитывается')" "и владельцу о молчании сказано"

# Ход не закончен (метка конца хода старше последнего ответа): смена работает.
stand blocked; answer 5; turn_end 6; touched 5; report 64; owner_said 120; run
check 1 "$(event pulse_kick)" "ход не закончен — пульс пинает, как прежде"

# Граница: конец хода в ту же секунду, что последний ответ, — ход закончен.
stand blocked; answer 5; turn_end 5 0; touched 5; report 64; owner_said 120; run
check 0 "$(event pulse_kick)" "конец хода в секунду ответа — это конец хода, пульс молчит"

stand work; answer 5; turn_end 5 3; touched 5; report 64; owner_said 120; run
check 1 "$(event pulse_kick)" "wip-карточка без блокера — пульс пинает, как прежде"

# ── немота ──────────────────────────────────────────────────────────────────
# БОЛЬНОЙ СЛУЧАЙ п.2: живой 10:20 — «Работа встала: агент молчит 29 минут».
stand blocked; answer 29; turn_end 29 3; touched 29; report 29; owner_said 120; run
check 0 "$(said 'Работа встала')" "БОЛЬНОЙ СЛУЧАЙ п.2: смена у приглашения, работы нет — «Работа встала» не пишется"
check blocked "$(cut -d' ' -f2 "$T/log/лимит-подписки.состояние" 2>/dev/null)" \
    "и метка остановки записала повод «blocked»"

# Зависание посреди хода: ход не закончен — тревога как прежде.
stand blocked; answer 29; turn_end 31; touched 29; report 29; owner_said 120; run
check 1 "$(said 'Работа встала')" "ход не закончен, смена молчит — «Работа встала», как прежде"

stand work; answer 29; turn_end 29 3; touched 29; report 29; owner_said 120; run
check 1 "$(said 'Работа встала')" "немота при незаблокированной работе — владельцу сказано, как прежде"

# Блокировка кончилась (появилась работа), метка «blocked» стоит, смена молчит:
# метка снимается молча, и о немоте сказано.
stand work; answer 29; turn_end 29 3; touched 29; report 29; owner_said 120; mark blocked 15; run
check 1 "$(said 'Работа встала')" "метка «blocked» после конца блокировки не глушит тревогу о немоте"

# ── лимит поверх метки «blocked» ───────────────────────────────────────────
stand blocked; answer 29; turn_end 29 3; refusal 2; touched 2; report 29; owner_said 120; mark blocked 15; run
check 1 "$(said 'Упёрся в лимит')" "настоящий лимит поверх метки «blocked» — владельцу сказано"

# ── возобновление после ожидания ───────────────────────────────────────────
# БОЛЬНОЙ СЛУЧАЙ п.3: смена ожила после ожидания — «смена просто молчала» не пишется.
stand blocked; answer 1; turn_end 1 3; touched 1; report 29; owner_said 120; mark blocked 30; run
check 0 "$(said 'возобновилась')" "БОЛЬНОЙ СЛУЧАЙ п.3: ожила после ожидания — о возобновлении молчим"
check 0 "$( [ -e "$T/log/лимит-подписки.состояние" ] && echo 1 || echo 0)" "и метка снята"

(( ok )) && { echo "SELFTEST: зелёный (15 проб обвязки сторожа «работы нет, смена у приглашения»)"; exit 0; }
echo "SELFTEST: КРАСНЫЙ"; exit 1
