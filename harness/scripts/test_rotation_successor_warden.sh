#!/usr/bin/env bash
# П1 (ротация с преемником): ротация не ждёт помощников — преемник сразу.
#
# Спека docs/specs/2026-11-02-ротация-ждёт-помощников.md, раздел 14.4. Владелец
# 21.11: «скорость сторожи тормозит всю работу на серверах постоянно снижая
# скорость… скорость работы — максимальная, сторожа её не снижают». Слово 02.11 в силе:
# ни один помощник не убивается.
#
# Стенд: свой сервер tmux (сокет «проба-$$» в каталоге стенда), свои LOG_DIR,
# panel-state, транскрипты; смена — подставной «claude» (bash под именем claude:
# пишет присланное в файл, на поручение ротации ставит метку конца хода), у неё
# идёт помощник. Нужна ротация (компактов 1 при потолке 1), передача написана
# (судья передачи стенда отвечает «да»).
#
# Ждём от ОДНОГО прогона сторожа: старая tmux-сессия переименована в
# <TMUX_SESSION>-old-<epoch> и жива с тем же процессом (помощник не оборван,
# «/exit» не подан); новая сессия с именем TMUX_SESSION поднята командой
# AGENT_START_CMD и получила пинок; флаг ротации — «дорабатывает» с полями
# «tmux», «сессия», «преемник».
#
# Дверь P1_VIA_REQUEST=1 (её зовёт П8, test_rotation_successor_update.py): нужды
# ротации по компактам нет (потолок 5), есть заявка ротации «обновление»
# (rotation_flag.py --request, спека 14.15) — тот же путь преемника; добавочно
# судится, что заявка снята, а причина перенесена во флаг и в событие.
#
# Сторож — живой файл (дверь мутаций HARNESS_SESSION_WARDEN). Итог — строка
# «ПРОБА ЗЕЛЁНАЯ: …» / «ПРОБА КРАСНАЯ: …», код 0 / 1; стенд не годен — «ПРОБА
# СЛОМАНА: …», код 2. Имена переменных латиницей: bash не берёт кириллические.
set -uo pipefail
export LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

WARDEN="${HARNESS_SESSION_WARDEN:-$HERE/../harness/demons/session-warden.sh}"
[ -f "$WARDEN" ] || WARDEN="$HERE/../demons/session-warden.sh"
[ -f "$WARDEN" ] || { echo "ПРОБА СЛОМАНА: П1 — сторожа нет по пути раскладки"; exit 2; }

STAND=$(mktemp -d)
PANE="rotsucc-$$"
# Свой сервер tmux: обёртка первой в PATH ведёт туда и голый tmux сторожа; TMUX
# снят, иначе клиент пошёл бы по сокету из неё. Сокет — в каталоге стенда.
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
# Панель сама гасит свой сервер, когда прогона или каталога стенда не стало.
pane_cmd() {  # $1 = команда панели → она же со сторожем жизни стенда
    printf '( { while kill -0 %s && [ -d %q ]; do sleep 2; done; s=${TMUX#*,}; kill "${s%%%%,*}"; } >/dev/null 2>&1 & ); %s' \
        "$$" "$STAND" "$1"
}
trap '"$REAL_TMUX" -L "$SOCK_NAME" kill-server 2>/dev/null; rm -rf "$STAND"' EXIT

T="$STAND/a"
SESSION="proba"
SUCCESSOR="succ-0002"
VIA_REQUEST="${P1_VIA_REQUEST:-0}"
MAX_COMPACTS_STAND=1
[ "$VIA_REQUEST" != 1 ] || MAX_COMPACTS_STAND=5
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
# Судья передачи стенда: передача написана — дверь ротации открыта.
printf '#!/usr/bin/env bash\necho "передача стенда: написана"\nexit 0\n' > "$T/project/scripts/handover-check.sh"
# Приёмка headless стенда: модель не зовётся.
printf '#!/usr/bin/env bash\necho "задача стенда, шаг стенда"\n' > "$T/demon.sh"
printf '#!/usr/bin/env bash\necho отвязан >> "%s"\n' "$T/detached.txt" > "$T/bin/setsid"
# Старая смена: на поручение ротации — конец хода (метки хука Stop), на «/exit» — выход.
cat > "$T/old-agent.sh" <<OLD
while IFS= read -r line; do
    printf '%s\n' "\$line" >> "$T/pane-old.txt"
    case "\$line" in
        *"Пора менять"*) ( sleep 2; date +%s > "$T/log/turn-end.stamp"; date +%s > "$T/log/turn-end.$SESSION.stamp" ) & ;;
        /exit) exit 0 ;;
    esac
done
OLD
# Преемник: пинок «новая сессия» рождает его транскрипт (Claude Code пишет файл с первым сообщением).
cat > "$T/successor-agent.sh" <<NEW
while IFS= read -r line; do
    printf '%s\n' "\$line" >> "$T/pane-new.txt"
    case "\$line" in
        *"новая сессия"*)
            printf '{"type":"user","timestamp":"%s","sessionId":"$SUCCESSOR","entrypoint":"cli","cwd":"/stand","message":{"role":"user","content":"пинок"}}\n' \
                "\$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$TR_DIR/$SUCCESSOR.jsonl" ;;
    esac
done
NEW
printf '#!/usr/bin/env bash\nexec -a claude bash %q\n' "$T/successor-agent.sh" > "$T/successor-start"
chmod +x "$T/project/scripts/tg_send.sh" "$T/project/scripts/handover-check.sh" "$T/demon.sh" \
    "$T/bin/setsid" "$T/successor-start"

SEED=""
for c in "$HERE/../config/harness.conf" "$HERE/../harness/config/harness.conf"; do
    [ -f "$c" ] && { SEED="$c"; break; }
done
[ -n "$SEED" ] || { echo "ПРОБА СЛОМАНА: П1 — образца harness.conf нет"; exit 2; }
cp "$SEED" "$T/harness.conf"
cat >> "$T/harness.conf" <<CONF
AGENT_USER="$(id -un)"
LOG_DIR="$T/log"
PROJECT_DIR="$T/project"
STATE_DIR="$T/state"
HEARTBEAT_DIR="$T/state/heartbeat"
PANEL_STATE_DIR="$PANEL_STATE"
TMUX_SESSION="$PANE"
AGENT_START_CMD="$T/successor-start"
AGENT_START_TIMEOUT_SEC=30
SERVICE_LOCKS_SUBDIR="проверка"
ACTIVE_LINE="hr"
CTX_WINDOW_TOKENS=400000  # не-секрет: размер окна модели в токенах, а не ключ
MAX_COMPACTS=$MAX_COMPACTS_STAND
ROTATE_NEEDS_OWNER=0
ROTATE_WITHOUT_HANDOVER=0
ROTATE_IDLE_SEC=0
ROTATE_FINISH_TIMEOUT_SEC=120
ROTATE_EXIT_WAIT_SEC=30
ROTATE_FLAG_STALE_MIN=30
ROTATE_FINISHING_MAX_MIN=360
HELPER_FRESH_MIN=20
HELPER_HUNG_MIN=120
SILENCE_MAX_MIN=30
PULSE_MAX_MIN=30
PULSE_OWNER_MIN=60
LIMIT_STALL_MIN=20
TG_CHAT_ID=""
TG_TOKEN_FILE="/nonexistent"
CONF

# Транскрипт смены с идущим помощником (фоновый запуск без уведомления о конце).
rm -rf "$TR_DIR"; mkdir -p "$TR_DIR" "$T/log/inbox/обработано" "$PANEL_STATE"
python3 - "$TR_DIR" "$SESSION" "$HELPER" <<'PY'
import datetime, json, os, sys, time
folder, sid, helper = sys.argv[1:4]
now = time.time()
def ts(ago):
    return datetime.datetime.fromtimestamp(now - ago, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
def rec(kind, ago, **extra):
    r = {"type": kind, "timestamp": ts(ago), "sessionId": sid, "entrypoint": "cli", "cwd": "/stand"}
    r.update(extra)
    return r
main = [
    rec("assistant", 2400, message={"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_stand_" + helper, "name": "Agent",
         "input": {"description": "проба-помощник", "prompt": "задание стенда", "run_in_background": True}}]}),
    rec("user", 2400, message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_stand_" + helper, "content": "Async agent launched successfully."}]},
        toolUseResult={"status": "async_launched", "agentId": helper, "description": "проба-помощник"}),
    rec("assistant", 30, message={"role": "assistant", "content": [{"type": "text", "text": "Жду."}]}),
]
with open(os.path.join(folder, sid + ".jsonl"), "w", encoding="utf-8") as f:
    f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in main)
sub = os.path.join(folder, sid, "subagents")
os.makedirs(sub, exist_ok=True)
hp = os.path.join(sub, "agent-" + helper + ".jsonl")
with open(hp, "w", encoding="utf-8") as f:
    f.write(json.dumps(rec("assistant", 60, agentId=helper, isSidechain=True,
                           message={"role": "assistant", "content": [{"type": "text", "text": "Шаг сделан."}]}),
                       ensure_ascii=False) + "\n")
with open(hp[:-len(".jsonl")] + ".meta.json", "w", encoding="utf-8") as f:
    json.dump({"description": "проба-помощник"}, f, ensure_ascii=False)
PY
printf '%s\n' '{"queue": [], "lines": [{"id": "hr", "tasks": [{"id": "hr.a", "status": "wip"}]}]}' > "$T/project/dev-map.yaml"
jq -cn --arg ts "$(date -Is)" --arg s "$SESSION" '{ts:$ts, event:"pre_compact", session:$s, detail:"стенд"}' \
    > "$T/log/rotation.jsonl"
: > "$T/pane-old.txt"; : > "$T/pane-new.txt"; : > "$T/kanal.txt"

tmux new-session -d -s "$PANE" -c "$T/project" "$(pane_cmd "exec -a claude bash $T/old-agent.sh")"
sleep 0.5
[ "$(tmux display-message -p -t "=$PANE:" '#{pane_current_command}')" = claude ] \
    || { echo "ПРОБА СЛОМАНА: П1 — подставная смена не встала в панель"; exit 2; }
OLD_PID=$(tmux display-message -p -t "=$PANE:" '#{pane_pid}')
REQUEST="$PANEL_STATE/rotation-request.json"
if [ "$VIA_REQUEST" = 1 ]; then
    HARNESS_PANEL_STATE="$PANEL_STATE" HARNESS_CONF="$T/harness.conf" HARNESS_INSTALL_CONF="$T/harness.conf" \
        python3 "$T/project/scripts/lib/rotation_flag.py" --request --reason обновление --tmux "$PANE" \
        --role оркестратор --detail CLAUDE.md
    [ -s "$REQUEST" ] || { echo "ПРОБА СЛОМАНА: П1 — заявку ротации поставить не вышло"; exit 2; }
fi

env -u TMUX -u CLAUDE_CODE_SESSION_ID PATH="$T/bin:$PATH" \
    HARNESS_CONF="$T/harness.conf" HARNESS_INSTALL_CONF="$T/harness.conf" \
    HARNESS_ACTIVE_LINE=hr PANE_ENTER_DELAY=0.2 AGENT_WAKE_DELAY=0 \
    HARNESS_PANEL_STATE="$PANEL_STATE" CLAUDE_PROJECTS_DIR="$T/projects" CLAUDE_DEMON_SH="$T/demon.sh" \
    timeout -k 5 400 bash "$WARDEN" > "$T/log/run.1" 2>&1 < /dev/null
RC=$?
sleep 1
grep -q "компактов за сессию: 1 (потолок $MAX_COMPACTS_STAND)" "$T/log/run.1" \
    || { echo "ПРОБА СЛОМАНА: П1 — прогон не дошёл до счёта компактов (код $RC)"; tail -20 "$T/log/run.1"; exit 2; }

flag_field() { jq -r --arg k "$1" '.[$k] // empty' "$FLAG" 2>/dev/null || echo "нет флага"; }
OLD_NAME=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -E "^$PANE-old-[0-9]+\$" | head -1)
NEW_PID=$(tmux display-message -p -t "=$PANE:" '#{pane_pid}' 2>/dev/null)

echo "── один прогон сторожа при идущем помощнике ──"
case_check "старая сессия переименована в $PANE-old-<epoch>" "да" "$([ -n "$OLD_NAME" ] && echo да || echo нет)"
case_check "в старой сессии тот же процесс смены (помощник не оборван)" "$OLD_PID" \
    "$([ -n "$OLD_NAME" ] && tmux display-message -p -t "=$OLD_NAME:" '#{pane_pid}' 2>/dev/null || echo нет)"
case_check "старой смене «/exit» не подан" "0" "$(grep -cx '/exit' "$T/pane-old.txt")"
case_check "старая смена получила поручение передачи" "да" \
    "$(grep -q 'Пора менять' "$T/pane-old.txt" && echo да || echo нет)"
case_check "сессия с именем TMUX_SESSION поднята заново (другой процесс)" "да" \
    "$([ -n "$NEW_PID" ] && [ "$NEW_PID" != "$OLD_PID" ] && echo да || echo нет)"
case_check "в новой сессии — агент (claude)" "claude" \
    "$(tmux display-message -p -t "=$PANE:" '#{pane_current_command}' 2>/dev/null || echo нет)"
case_check "преемник получил пинок «новая сессия»" "да" \
    "$(grep -q 'новая сессия' "$T/pane-new.txt" && echo да || echo нет)"
case_check "флаг ротации: этап «дорабатывает»" "дорабатывает" "$(flag_field этап)"
case_check "флаг ротации: сессия — уходящая смена" "$SESSION" "$(flag_field сессия)"
case_check "флаг ротации: tmux — имя старой сессии" "${OLD_NAME:-нет сессии}" "$(flag_field tmux)"
case_check "флаг ротации: преемник — sid новой смены" "$SUCCESSOR" "$(flag_field преемник)"

if [ "$VIA_REQUEST" = 1 ]; then
    echo "── заявка ротации «обновление» ──"
    case_check "заявка снята после начала ротации" "нет" "$([ -e "$REQUEST" ] && echo есть || echo нет)"
    case_check "причина заявки перенесена во флаг" "обновление" "$(flag_field причина)"
    case_check "событие role_refreshed несёт причину «обновление»" "обновление" \
        "$(jq -r 'select(.event=="role_refreshed") | .data["причина"]' "$T/log/rotation.jsonl" 2>/dev/null | tail -1)"
fi

if [ "$CASES_FAILED" -eq 0 ]; then
    echo "ПРОБА ЗЕЛЁНАЯ: П1 сторож — путей $CASES_PATHS, преемник поднят сразу, старая дорабатывает"
    exit 0
fi
echo "ПРОБА КРАСНАЯ: П1 сторож — неудач $CASES_FAILED из $CASES_PATHS (код прогона $RC; последняя строка сторожа: $(grep -v '^\s*$' "$T/log/run.1" | grep -m1 'ротация ТРЕБУЕТСЯ\|нужна ротация' || tail -1 "$T/log/run.1"))"
exit 1
