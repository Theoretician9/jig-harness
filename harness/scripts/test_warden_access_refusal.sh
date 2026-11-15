#!/usr/bin/env bash
# Проба: подписка отказала в доступе — сторож сессий говорит это владельцу сам
# (находка копии bkb 11.11.2026).
#
# Больной случай 11.11.2026 ~21:40–23:50: все сессии упали на API 403
# `oauth_org_not_allowed` («Claude Code can't use your Claude subscription»).
# Сторож два часа писал смене «Работы не видно N мин, признака лимита нет» и
# пинал мёртвую сессию; владелец узнал о простое сам и перелогинился. Отказ
# доступа не кончается сам, как лимит: нужен человек — вход или оплата.
#
# Запись отказа снята с ЖИВОГО транскрипта этой машины (11.11.2026 16:45Z,
# прогон sdk-cli): поля error, isApiErrorMessage, apiErrorStatus, apiErrorCode и
# текст — дословно; меняются только время и sessionId стенда.
#
# Случаи: А0 — контроль (тот же журнал без отказа: обычная немота, пинок «Работы
# не видно»); А1 — отказ: владельцу одна весть о доступе, «Работы не видно» в
# панель не уходит, метка с поводом access; А2 — второй обход: вести не
# повторяются; А3 — доступ вернулся (настоящий ответ после отказа): весть о
# возобновлении называет доступ.
#
# Дверь мутаций: HARNESS_SESSION_WARDEN — путь к испорченной копии сторожа.
#   bash scripts/test_warden_access_refusal.sh
set -uo pipefail
export LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"
DAEMON="${HARNESS_SESSION_WARDEN:-$HERE/../harness/demons/session-warden.sh}"
# В пакете демоны лежат в harness/demons, пробы — в harness/scripts.
[ -f "$DAEMON" ] || DAEMON="$HERE/../demons/session-warden.sh"
[ -s "$DAEMON" ] || { echo "ПЛОХО: нет сторожа по пути раскладки: $DAEMON — проверять нечего"; exit 1; }

PREFIX="warden-access-"
T=$(mktemp -d -t warden-access.XXXXXX)
PANE="${PREFIX}$$"
# Свой сервер tmux на прогон (как у test_warden_limit_silence.sh): панель стенда
# на боевом сервере читалась бы как работа агента.
REAL_TMUX="$(command -v tmux)"
unset TMUX TMUX_PANE
export TMUX_TMPDIR="$T/tmux"
mkdir -p "$T/tmux-bin" "$TMUX_TMPDIR"
printf '#!/bin/sh\nexec "%s" -L "%s" "$@"\n' "$REAL_TMUX" "$PANE" > "$T/tmux-bin/tmux"
chmod +x "$T/tmux-bin/tmux"
export PATH="$T/tmux-bin:$PATH"
printf '#!/bin/sh\nexit 0\n' > "$T/self-prompt-live.sh"
export SELF_PROMPT_LIVE_SH="$T/self-prompt-live.sh"
pane_cmd() {  # $1 = команда панели → она же со сторожем жизни стенда
    printf '( { while kill -0 %s && [ -d %q ]; do sleep 2; done; s=${TMUX#*,}; kill "${s%%%%,*}"; } >/dev/null 2>&1 & ); %s' \
        "$$" "$T" "$1"
}
trap '"$REAL_TMUX" -L "$PANE" kill-server 2>/dev/null; rm -rf "$T"' EXIT

# ── стенд сторожа: своя панель, свой LOG_DIR, канал-файл ───────────────────
cp /bin/cat "$T/claude"
mkdir -p "$T/project/scripts/lib" "$T/projects"
for f in work-queue.py limit_mark.py limit-in-log.py stall-in-log.py; do
    cp "$HERE/$f" "$T/project/scripts/"
done
cp "$HERE"/lib/*.py "$HERE/lib/config.sh" "$T/project/scripts/lib/"
cat > "$T/project/scripts/tg_send.sh" <<CHANNEL
#!/usr/bin/env bash
for A in "\$@"; do
    case "\$A" in --*) continue ;; esac
    printf '%s\n' "\$A" | tr '\n' ' ' >> "$T/channel.txt"
    printf '\n' >> "$T/channel.txt"
    break
done
CHANNEL
chmod +x "$T/project/scripts/tg_send.sh"
A_DIR="$T/projects/${T//\//-}-project"
SEED=""
for c in "$HERE/../config/harness.conf" "$HERE/../harness/config/harness.conf" "${HARNESS_CONF:-/etc/harness/harness.conf}"; do
    [ -f "$c" ] && { SEED="$c"; break; }
done
[ -n "$SEED" ] || { echo "ПЛОХО: образца harness.conf нет — проверять нечем"; exit 1; }
cp "$SEED" "$T/harness.conf"
cat >> "$T/harness.conf" <<CONF
AGENT_USER="$(id -un)"
LOG_DIR="$T/log"
PROJECT_DIR="$T/project"
STATE_DIR="$T/state"
TMUX_SESSION="$PANE"
AGENT_START_CMD="$T/claude"
SERVICE_LOCKS_SUBDIR="проверка"
ACTIVE_LINE="hr"
CTX_WINDOW_TOKENS=1000000  # не-секрет: размер окна модели в токенах
CTX_ROTATE_PCT=95
SILENCE_MAX_MIN=10
PULSE_MAX_MIN=30
PULSE_OWNER_MIN=60
LIMIT_STALL_MIN=20
LIMIT_SAY_MIN=3
HELPER_FRESH_MIN=20
KICK_ANSWER_WAIT_MIN=5
TG_CHAT_ID=""
TG_TOKEN_FILE="/nonexistent"
CONF
SID="7a0c3e51-2b9d-4f6e-8c1a-0d5e4f3a2b10"

# journal.py <выход> <sid> <запись@минут-назад>… — записи: answer, refusal, kick.
cat > "$T/journal.py" <<'PY'
import datetime, json, os, sys, time
dst, sid, specs = sys.argv[1], sys.argv[2], sys.argv[3:]
now = time.time()
REFUSAL_TEXT = ("Your organization has disabled Claude subscription access for Claude Code"
                " · Use an Anthropic API key instead, or ask your admin to enable access")
def record(kind):
    if kind == "answer":
        return {"type": "assistant", "message": {"model": "claude-opus", "role": "assistant",
                "content": [{"type": "text", "text": "Шаг сделан, иду дальше."}]}}
    if kind == "refusal":
        return {"type": "assistant", "isApiErrorMessage": True, "apiErrorStatus": 403,
                "error": "oauth_org_not_allowed", "apiErrorCode": "oauth_not_allowed_for_organization",
                "message": {"model": "<synthetic>", "role": "assistant", "type": "message",
                            "content": [{"type": "text", "text": REFUSAL_TEXT}]}}
    return {"type": "user", "message": {"role": "user", "content": "[сторож сессий] продолжай"}}
out, newest = [], 0
for spec in specs:
    kind, ago = spec.split("@")
    moment = now - float(ago) * 60
    rec = {**record(kind), "sessionId": sid, "uuid": f"u{len(out)}",
           "timestamp": datetime.datetime.fromtimestamp(moment, datetime.timezone.utc)
           .strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    out.append(json.dumps(rec, ensure_ascii=False))
    newest = max(newest, moment)
os.makedirs(os.path.dirname(dst), exist_ok=True)
with open(dst, "w", encoding="utf-8") as stream:
    stream.write("\n".join(out) + "\n")
os.utime(dst, (newest, newest))
PY

stand() {  # свежий LOG_DIR, карта с работой, панель с подставным агентом
    rm -rf "$T/log" "$T/state" "$A_DIR"
    mkdir -p "$T/log/inbox/обработано" "$T/state/panel-state" "$A_DIR"
    : > "$T/channel.txt"; : > "$T/pane.txt"
    printf '%s\n' '{"queue": [], "lines": [{"id": "hr", "tasks": [{"id": "hr.b", "status": "wip"}]}]}' \
        > "$T/project/dev-map.yaml"
    tmux kill-session -t "$PANE" 2>/dev/null
    tmux new-session -d -x 200 -y 60 -s "$PANE" -c "$T/project" "$(pane_cmd "exec $T/claude >> $T/pane.txt")"
    sleep 0.5
    [ "$(tmux display-message -p -t "$PANE" '#{pane_current_command}')" = claude ] \
        || { echo "ПЛОХО: подставной агент не встал в панель — проверять нечего"; exit 1; }
}
journal() { python3 "$T/journal.py" "$A_DIR/$SID.jsonl" "$SID" "$@" \
    || { echo "ПЛОХО: журнал стенда не записан — проверять нечего"; exit 1; }; }
report_ago() {  # $1 = минут назад последний отчёт владельцу
    echo $(( $(date +%s) - $1 * 60 )) > "$T/log/last_report"
    touch -d "@$(( $(date +%s) - $1 * 60 ))" "$T/log/last_report"
}
run_warden() {
    env -u TMUX -u CLAUDE_CODE_SESSION_ID HARNESS_CONF="$T/harness.conf" HARNESS_INSTALL_CONF="$T/harness.conf" \
        HARNESS_ACTIVE_LINE=hr PANE_ENTER_DELAY=0.2 \
        HARNESS_PANEL_STATE="$T/state/panel-state" CLAUDE_PROJECTS_DIR="$T/projects" \
        bash "$DAEMON" >> "$T/log/warden.out" 2>&1 < /dev/null
    sleep 0.3
}
pane_has() { grep -qF -- "$1" "$T/pane.txt" 2>/dev/null && echo да || echo нет; }
said() { local n; n=$(grep -c -- "$1" "$T/channel.txt" 2>/dev/null); echo "${n:-0}"; }
mark_reason() { cut -d' ' -f2 "$T/log/лимит-подписки.состояние" 2>/dev/null || echo нет; }

echo "── А0 контроль: смена молчит 30 мин, отказа доступа нет — обычная немота, пинок ──"
stand
journal answer@30
report_ago 25
run_warden
case_check "А0: условие немоты создано — в панель ушло «Работы не видно»" "да" "$(pane_has 'Работы не видно')"
[ "$(pane_has 'Работы не видно')" = да ] || tail -5 "$T/log/warden.out" 2>/dev/null | sed 's/^/      сторож: /'

echo "── А1: БОЛЬНОЙ СЛУЧАЙ 11.11 — последний ответ 30 мин назад, затем отказ 403 ──"
stand
journal answer@30 kick@25 refusal@25 kick@10 refusal@10
report_ago 25
run_warden
case_check "А1: владельцу ушла весть об отказе доступа (подписка, вход или оплата)" "1" \
    "$(grep -c 'подписка отказала в доступе.*вход.*оплат' "$T/channel.txt" 2>/dev/null)"
case_check "А1: весть приводит ответ подписки дословно" "1" "$(said 'disabled Claude subscription access')"
case_check "А1: владельцу не ушло «Работа встала … причина пока не названа»" "0" "$(said 'причина пока не названа')"
case_check "А1: в панель не ушло «Работы не видно … признака лимита нет»" "нет" "$(pane_has 'Работы не видно')"
case_check "А1: метка простоя стоит с поводом access" "access" "$(mark_reason)"
# Суточная сверка меню бота на стенде без Telegram шлёт свою весть — она не о простое.
case_check "А1: владельцу за обход ровно одна весть (ни статуса сторожа, ни «не отчитывается»)" "1" \
    "$(grep -v '^Меню бота' "$T/channel.txt" 2>/dev/null | grep -c .)"
[ "$(said 'подписка отказала')" = 1 ] || tail -8 "$T/log/warden.out" 2>/dev/null | sed 's/^/      сторож: /'
[ "$(grep -v '^Меню бота' "$T/channel.txt" | grep -c .)" = 1 ] || cut -c1-160 "$T/channel.txt" | sed 's/^/      владельцу: /'

echo "── А2: следующий обход, отказ держится — владельцу ни слова повторно ──"
run_warden
case_check "А2: весть об отказе доступа одна за два обхода" "1" "$(said 'подписка отказала')"
case_check "А2: в панель не ушло «Работы не видно»" "нет" "$(pane_has 'Работы не видно')"

echo "── А3: владелец вошёл заново — смена ответила по делу, весть о подъёме ──"
journal answer@30 kick@25 refusal@25 kick@10 refusal@10 kick@1 answer@0.5
run_warden
case_check "А3: владельцу ушла весть «Доступ к подписке вернулся»" "1" "$(said 'Доступ к подписке вернулся')"
case_check "А3: метка простоя снята" "нет" "$(mark_reason)"

echo "── А4: отказ — машинный факт: немота 8 мин (меньше порога догадки 20) — владельцу сказано сразу ──"
stand
journal answer@8 kick@6 refusal@6
report_ago 8
run_warden
case_check "А4: при немоте 8 мин весть об отказе доступа уже ушла (порог LIMIT_SAY_MIN, не LIMIT_STALL_MIN)" "1" \
    "$(said 'подписка отказала')"

passed=$((CASES_PATHS - CASES_FAILED))
if [ "$CASES_FAILED" -eq 0 ]; then
    echo "итог: $passed/$CASES_PATHS — отказ доступа назван владельцу сразу, без пинков «работы не видно»"
    exit 0
fi
echo "итог: $passed/$CASES_PATHS — КРАСНЫЙ ($CASES_FAILED проб)"
exit 1
