#!/usr/bin/env bash
# П5 (ротация с преемником): дорабатывающая смена гаснет сама, никого не обрывая.
#
# Спека docs/specs/2026-11-02-ротация-ждёт-помощников.md, раздел 14.6. После
# ротации с преемником старая смена живёт в tmux-сессии <TMUX_SESSION>-old-<epoch>
# и доводит своих помощников. Сторож каждым прогоном судит ЕЁ помощников (по её
# транскрипту, не по свежайшему) и ЕЁ конец хода (метка turn-end.<sid>.stamp):
#   Ф1 помощников 0, ход завершён → «/exit» в её панель, затем kill-session её
#      tmux-сессии, флаг снят; преемник не тронут;
#   Ф2 помощник идёт → ничего не подаётся, флаг цел и освежён;
#   Ф3 помощник идёт дольше ROTATE_FINISHING_MAX_MIN → ОДНО письмо владельцу,
#      без «/exit» и без убийства (слово 02.11).
#
# Стенд: свой сервер tmux (сокет «проба-$$» в каталоге стенда), свои LOG_DIR,
# panel-state, транскрипты; обе смены — подставные «claude». Сторож — живой файл
# (дверь мутаций HARNESS_SESSION_WARDEN). Итог — строка «ПРОБА ЗЕЛЁНАЯ: …» /
# «ПРОБА КРАСНАЯ: …», код 0 / 1; стенд не годен — «ПРОБА СЛОМАНА: …», код 2.
set -uo pipefail
export LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

WARDEN="${HARNESS_SESSION_WARDEN:-$HERE/../harness/demons/session-warden.sh}"
[ -f "$WARDEN" ] || WARDEN="$HERE/../demons/session-warden.sh"
[ -f "$WARDEN" ] || { echo "ПРОБА СЛОМАНА: П5 — сторожа нет по пути раскладки"; exit 2; }

STAND=$(mktemp -d)
PANE="rotfin-$$"
OLD_NAME="$PANE-old-1795209000"
REAL_TMUX="$(command -v tmux)"
SOCK_NAME="проба-$$"
unset TMUX TMUX_PANE
export TMUX_TMPDIR="$STAND/tmux"
mkdir -p "$STAND/tmux-bin" "$TMUX_TMPDIR"
printf '#!/bin/sh\nexec "%s" -L "%s" "$@"\n' "$REAL_TMUX" "$SOCK_NAME" > "$STAND/tmux-bin/tmux"
chmod +x "$STAND/tmux-bin/tmux"
export PATH="$STAND/tmux-bin:$PATH"
printf '#!/bin/sh\nexit 0\n' > "$STAND/self-prompt-live.sh"
export SELF_PROMPT_LIVE_SH="$STAND/self-prompt-live.sh"
pane_cmd() {  # $1 = команда панели → она же со сторожем жизни стенда
    printf '( { while kill -0 %s && [ -d %q ]; do sleep 2; done; s=${TMUX#*,}; kill "${s%%%%,*}"; } >/dev/null 2>&1 & ); %s' \
        "$$" "$STAND" "$1"
}
trap '"$REAL_TMUX" -L "$SOCK_NAME" kill-server 2>/dev/null; rm -rf "$STAND"' EXIT

T="$STAND/a"
SESSION="proba"
SUCCESSOR="succ-0002"
HELPER="a1511aa2c0ffee001"
PANEL_STATE="$T/state/panel-state"
FLAG="$PANEL_STATE/rotation-waits-helpers.json"
TR_DIR="$T/projects/${T//\//-}-project"
mkdir -p "$T/project/scripts/lib" "$T/projects" "$T/bin"
for f in work-queue.py limit_mark.py limit-in-log.py stall-in-log.py pause.py; do
    cp "$HERE/$f" "$T/project/scripts/" 2>/dev/null
done
cp "$HERE"/lib/*.py "$HERE/lib/config.sh" "$T/project/scripts/lib/"
cat > "$T/project/scripts/tg_send.sh" <<KANAL
#!/usr/bin/env bash
for A in "\$@"; do
    case "\$A" in --*) continue ;; esac
    printf '%s\n' "\$A" >> "$T/kanal.txt"
    break
done
KANAL
printf '#!/usr/bin/env bash\necho "передача стенда: написана"\nexit 0\n' > "$T/project/scripts/handover-check.sh"
printf '#!/usr/bin/env bash\necho "задача стенда, шаг стенда"\n' > "$T/demon.sh"
printf '#!/usr/bin/env bash\necho отвязан >> "%s"\n' "$T/detached.txt" > "$T/bin/setsid"
cat > "$T/old-agent.sh" <<OLD
while IFS= read -r line; do
    printf '%s\n' "\$line" >> "$T/pane-old.txt"
    [ "\$line" = /exit ] && exit 0
done
OLD
cp /bin/cat "$T/claude"
chmod +x "$T/project/scripts/tg_send.sh" "$T/project/scripts/handover-check.sh" "$T/demon.sh" "$T/bin/setsid"

SEED=""
for c in "$HERE/../config/harness.conf" "$HERE/../harness/config/harness.conf"; do
    [ -f "$c" ] && { SEED="$c"; break; }
done
[ -n "$SEED" ] || { echo "ПРОБА СЛОМАНА: П5 — образца harness.conf нет"; exit 2; }
cp "$SEED" "$T/harness.conf"
cat >> "$T/harness.conf" <<CONF
AGENT_USER="$(id -un)"
LOG_DIR="$T/log"
PROJECT_DIR="$T/project"
STATE_DIR="$T/state"
HEARTBEAT_DIR="$T/state/heartbeat"
PANEL_STATE_DIR="$PANEL_STATE"
TMUX_SESSION="$PANE"
AGENT_START_CMD="$T/claude"
AGENT_START_TIMEOUT_SEC=30
SERVICE_LOCKS_SUBDIR="проверка"
ACTIVE_LINE="hr"
CTX_WINDOW_TOKENS=400000  # не-секрет: размер окна модели в токенах, а не ключ
MAX_COMPACTS=1
ROTATE_NEEDS_OWNER=0
ROTATE_IDLE_SEC=0
ROTATE_EXIT_WAIT_SEC=30
ROTATE_FLAG_STALE_MIN=30
ROTATE_FINISHING_MAX_MIN=360
ROTATE_REFUSED_TELL_GAP_MIN=180
HELPER_FRESH_MIN=20
HELPER_HUNG_MIN=120
SILENCE_MAX_MIN=30
PULSE_MAX_MIN=30
PULSE_OWNER_MIN=60
LIMIT_STALL_MIN=20
TG_CHAT_ID=""
TG_TOKEN_FILE="/nonexistent"
CONF

# $1 = live (помощник старой идёт) | done (уведомил о конце); $2 = минут доработки
stand() {
    local kind="$1" finishing_min="$2" now
    tmux kill-session -t "=$PANE" 2>/dev/null
    tmux kill-session -t "=$OLD_NAME" 2>/dev/null
    rm -rf "$T/log" "$T/state" "$TR_DIR"
    mkdir -p "$T/log/inbox/обработано" "$PANEL_STATE" "$TR_DIR"
    : > "$T/pane-old.txt"; : > "$T/pane-new.txt"; : > "$T/kanal.txt"; : > "$T/log/rotation.jsonl"
    printf '%s\n' '{"queue": [], "lines": [{"id": "hr", "tasks": [{"id": "hr.a", "status": "wip"}]}]}' > "$T/project/dev-map.yaml"
    python3 - "$TR_DIR" "$SESSION" "$SUCCESSOR" "$HELPER" "$kind" <<'PY'
import datetime, json, os, sys, time
folder, sid, succ, helper, kind = sys.argv[1:6]
now = time.time()
def ts(ago):
    return datetime.datetime.fromtimestamp(now - ago, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
def rec(kind_, ago, session, **extra):
    r = {"type": kind_, "timestamp": ts(ago), "sessionId": session, "entrypoint": "cli", "cwd": "/stand"}
    r.update(extra)
    return r
old = [
    rec("assistant", 2400, sid, message={"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_stand_" + helper, "name": "Agent",
         "input": {"description": "проба-помощник", "prompt": "задание стенда", "run_in_background": True}}]}),
    rec("user", 2400, sid, message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_stand_" + helper, "content": "Async agent launched successfully."}]},
        toolUseResult={"status": "async_launched", "agentId": helper, "description": "проба-помощник"}),
]
if kind == "done":
    old.append(rec("user", 300, sid, message={"role": "user", "content":
        "<task-notification>\n<task-id>" + helper + "</task-id>\n<status>completed</status>\n</task-notification>"}))
old.append(rec("assistant", 200, sid, message={"role": "assistant", "content": [{"type": "text", "text": "Довёл."}]}))
path = os.path.join(folder, sid + ".jsonl")
with open(path, "w", encoding="utf-8") as f:
    f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in old)
os.utime(path, (now - 200, now - 200))
sub = os.path.join(folder, sid, "subagents")
os.makedirs(sub, exist_ok=True)
hp = os.path.join(sub, "agent-" + helper + ".jsonl")
with open(hp, "w", encoding="utf-8") as f:
    f.write(json.dumps(rec("assistant", 60 if kind == "live" else 320, sid, agentId=helper, isSidechain=True,
                           message={"role": "assistant", "content": [{"type": "text", "text": "Шаг сделан."}]}),
                       ensure_ascii=False) + "\n")
with open(hp[:-len(".jsonl")] + ".meta.json", "w", encoding="utf-8") as f:
    json.dump({"description": "проба-помощник"}, f, ensure_ascii=False)
# Преемник пишет свежее старой: «свежайший транскрипт» — его.
with open(os.path.join(folder, succ + ".jsonl"), "w", encoding="utf-8") as f:
    f.write(json.dumps(rec("assistant", 20, succ, message={"role": "assistant", "content": [
        {"type": "text", "text": "Беру задачи."}]}), ensure_ascii=False) + "\n")
PY
    now=$(date +%s)
    # Хук Stop старой смены: ход закончен ПОСЛЕ её последней записи и после поручения.
    printf '%s\n' "$((now - 190))" > "$T/log/turn-end.$SESSION.stamp"
    printf '%s\n' "$((now - 10))" > "$T/log/turn-end.stamp"
    printf '{"сессия":"%s","этап":"дорабатывает","с":%s,"проверено":%s,"ждали_сек":0,"причина":"стенд П5","помощников":1,"принуждена":0,"сказано":null,"преемник":"%s","tmux":"%s","поручено":%s,"с_доработки":%s}\n' \
        "$SESSION" "$((now - finishing_min * 60 - 900))" "$now" "$SUCCESSOR" "$OLD_NAME" \
        "$((now - finishing_min * 60 - 900))" "$((now - finishing_min * 60))" > "$FLAG"
    tmux new-session -d -s "$PANE" -c "$T/project" "$(pane_cmd "exec $T/claude >> $T/pane-new.txt")"
    tmux new-session -d -s "$OLD_NAME" -c "$T/project" "$(pane_cmd "exec -a claude bash $T/old-agent.sh")"
    # Панель старой переживает выход агента: закрыть сессию обязан сторож (kill-session).
    tmux set-option -w -t "=$OLD_NAME:" remain-on-exit on >/dev/null \
        || { echo "ПРОБА СЛОМАНА: П5 — remain-on-exit для старой не поставлен"; exit 2; }
    sleep 0.5
    [ "$(tmux display-message -p -t "=$PANE:" '#{pane_current_command}')" = claude ] \
        && [ "$(tmux display-message -p -t "=$OLD_NAME:" '#{pane_current_command}')" = claude ] \
        || { echo "ПРОБА СЛОМАНА: П5 — подставные смены не встали в панели"; exit 2; }
    NEW_PID=$(tmux display-message -p -t "=$PANE:" '#{pane_pid}')
    OLD_PID=$(tmux display-message -p -t "=$OLD_NAME:" '#{pane_pid}')
}
RUN_N=0
run() {
    RUN_N=$((RUN_N + 1))
    env -u TMUX -u CLAUDE_CODE_SESSION_ID PATH="$T/bin:$PATH" \
        HARNESS_CONF="$T/harness.conf" HARNESS_INSTALL_CONF="$T/harness.conf" \
        HARNESS_ACTIVE_LINE=hr PANE_ENTER_DELAY=0.2 AGENT_WAKE_DELAY=0 \
        HARNESS_PANEL_STATE="$PANEL_STATE" CLAUDE_PROJECTS_DIR="$T/projects" CLAUDE_DEMON_SH="$T/demon.sh" \
        timeout -k 5 300 bash "$WARDEN" > "$T/log/run.$RUN_N" 2>&1 < /dev/null
    RC=$?
    sleep 1
    grep -q "сессия $SUCCESSOR\|сессия $SESSION" "$T/log/run.$RUN_N" \
        || { echo "ПРОБА СЛОМАНА: П5 — прогон $RUN_N не дошёл до замера сессии (код $RC)"; tail -20 "$T/log/run.$RUN_N"; exit 2; }
}
flag_field() { jq -r --arg k "$1" '.[$k] // empty' "$FLAG" 2>/dev/null || echo "нет флага"; }
old_alive() { tmux has-session -t "=$OLD_NAME" 2>/dev/null && echo жива || echo нет; }

echo "Ф1. помощников старой 0, ход завершён — «/exit» ей и закрытие её сессии"
stand done 30; run
case_check "Ф1 старой смене подан «/exit»" "1" "$(grep -cx '/exit' "$T/pane-old.txt")"
case_check "Ф1 tmux-сессия старой закрыта (kill-session после выхода)" "нет" "$(old_alive)"
case_check "Ф1 преемник цел: тот же процесс" "$NEW_PID" "$(tmux display-message -p -t "=$PANE:" '#{pane_pid}' 2>/dev/null || echo нет)"
case_check "Ф1 преемнику «/exit» не подан" "0" "$(grep -cx '/exit' "$T/pane-new.txt")"
case_check "Ф1 флаг снят" "нет" "$([ -e "$FLAG" ] && echo есть || echo нет)"
case_check "Ф1 событие finishing_done в журнале ротации" "да" \
    "$(grep -q '"event":"finishing_done"' "$T/log/rotation.jsonl" && echo да || echo нет)"

echo "Ф2. помощник старой идёт — ждать, флаг цел"
stand live 30; run
case_check "Ф2 «/exit» не подан" "0" "$(grep -cx '/exit' "$T/pane-old.txt")"
case_check "Ф2 старая жива тем же процессом" "$OLD_PID" "$(tmux display-message -p -t "=$OLD_NAME:" '#{pane_pid}' 2>/dev/null || echo нет)"
case_check "Ф2 флаг цел: этап «дорабатывает»" "дорабатывает" "$(flag_field этап)"
case_check "Ф2 флаг цел: сессия — старая смена (не снят как «ушедшей»)" "$SESSION" "$(flag_field сессия)"
case_check "Ф2 владельцу о дорабатывающей не писали (потолок не вышел)" "0" "$(grep -c 'дорабатывает' "$T/kanal.txt")"

echo "Ф3. помощник идёт дольше ROTATE_FINISHING_MAX_MIN — одно письмо, никого не трогать"
stand live 400; run; run
case_check "Ф3 «/exit» не подан" "0" "$(grep -cx '/exit' "$T/pane-old.txt")"
case_check "Ф3 старая жива тем же процессом" "$OLD_PID" "$(tmux display-message -p -t "=$OLD_NAME:" '#{pane_pid}' 2>/dev/null || echo нет)"
case_check "Ф3 владельцу одно письмо за два прогона о дорабатывающей смене" "1" "$(grep -c 'дорабатывает' "$T/kanal.txt")"
case_check "Ф3 флаг цел: этап «дорабатывает»" "дорабатывает" "$(flag_field этап)"

if [ "$CASES_FAILED" -eq 0 ]; then
    echo "ПРОБА ЗЕЛЁНАЯ: П5 доработка — путей $CASES_PATHS, старая гаснет сама, при потолке только письмо"
    exit 0
fi
echo "ПРОБА КРАСНАЯ: П5 доработка — неудач $CASES_FAILED из $CASES_PATHS (флаг после последнего прогона: $(flag_field этап); снятие: $(grep -o 'флаг ушедшей смены [^"]*' "$T/log/rotation.jsonl" | head -1))"
exit 1
