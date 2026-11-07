#!/usr/bin/env bash
# Проба: пропавшее гнездо tmux не поднимает вторую смену; две смены — тревога.
#
# Откуда взята. Отчёт с другого сервера (docs/specs/2026-10-29-сверка-отчёта-
# другого-сервера.md, п.1–2). Сокет tmux удалили при живом сервере; сторож
# спросил has-session, получил «нет» и через start_agent → start-agent.sh
# поднял ВТОРОЙ сервер tmux со второй сменой. Обе работали параллельно, и
# сказать об этом было некому: счёта агентов у сторожа нет.
#
# Что судится (функции берутся из ЖИВОГО файла сторожа, путь — из окружения,
# чтобы испорченная мутацией копия доходила до пробы):
#   start_agent       — при «гнездо пропало» возвращает сокет сигналом USR1
#                       серверу, а не поднимает новый; при «агент жив вне tmux»
#                       отказывает с тревогой; при «никого нет» — штатный подъём;
#   check_two_agents  — круг сторожа: две смены → файл состояния; тревога ОДНА
#                       и только когда тот же состав держится два круга подряд
#                       не короче TWO_AGENTS_CONFIRM_SEC; одна смена → файл снят.
#
# Песочница. Свой сервер tmux (-L своё имя, свой TMUX_TMPDIR), подставной
# PROJECT_DIR с заглушками start-agent.sh и tg_send.sh — их сторож зовёт
# по пути "$PROJECT_DIR/scripts/…". Дерево процессов поддельное
# (AGENT_PROC_ROOT), AGENT_MAINPID=0, kill — заглушка через PATH: настоящий
# сигнал уходит ТОЛЬКО своему серверу и только USR1. Живой сервер агента
# проба не видит ни через /proc, ни через сокет.
#
# Имена переменных ЛАТИНИЦЕЙ: bash не берёт кириллические идентификаторы.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

WARDEN="${HARNESS_SESSION_WARDEN:-$HERE/../harness/demons/session-warden.sh}"
[ -r "$WARDEN" ] || WARDEN="$HERE/../demons/session-warden.sh"
LIB_PY="${AGENT_PROCESSES_LIB:-$HERE/lib/agent_processes.py}"
LIB_PY="$(readlink -f "$LIB_PY")"
REAL_TMUX="$(command -v tmux)"

STAND=$(mktemp -d)
SOCK_NAME="w1t-nest-$$"
SES="agent-w1t-$$"
MY_SERVER_PID=""
# Переменная TMUX указывает клиенту на сокет ЖИВОГО сервера, если проба
# запущена из панели: без снятия любой голый tmux ушёл бы туда.
unset TMUX TMUX_PANE
export TMUX_TMPDIR="$STAND/tmux"
mkdir -p "$TMUX_TMPDIR" "$STAND/bin" "$STAND/proj/scripts/lib"

own_tmux() { "$REAL_TMUX" -L "$SOCK_NAME" "$@"; }

cleanup() {
    own_tmux kill-server 2>/dev/null
    # Сервер без сокета kill-server не слышит: сперва вернуть ему сокет.
    if [ -n "$MY_SERVER_PID" ] && [ -d "/proc/$MY_SERVER_PID" ]; then
        /bin/kill -USR1 "$MY_SERVER_PID" 2>/dev/null
        sleep 0.5
        own_tmux kill-server 2>/dev/null
    fi
    rm -rf "$STAND"
}
trap cleanup EXIT

# ── заглушки ────────────────────────────────────────────────────────────────
# tmux: любой вызов — в журнал и на СВОЙ сервер.
cat > "$STAND/bin/tmux" <<EOF
#!/bin/sh
echo "\$*" >> "$STAND/tmux.log"
exec "$REAL_TMUX" -L "$SOCK_NAME" "\$@"
EOF
# kill: журнал; настоящий сигнал — только «-USR1 <pid своего сервера>».
cat > "$STAND/bin/kill" <<EOF
#!/bin/sh
echo "\$*" >> "$STAND/kill.log"
own=\$(cat "$STAND/own-server.pid" 2>/dev/null)
if [ -n "\$own" ] && [ "\$*" = "-USR1 \$own" ]; then
    # kill-delay: сокет возвращается не сразу — сторож обязан дождаться.
    d=\$(cat "$STAND/kill-delay" 2>/dev/null)
    if [ -n "\$d" ]; then
        (sleep "\$d"; /bin/kill -USR1 "\$own") >/dev/null 2>&1 </dev/null &
        exit 0
    fi
    exec /bin/kill -USR1 "\$own"
fi
exit 0
EOF
cat > "$STAND/bin/systemctl" <<EOF
#!/bin/sh
echo "\$*" >> "$STAND/systemctl.log"
echo 0
EOF
cat > "$STAND/proj/scripts/start-agent.sh" <<EOF
#!/bin/sh
echo started >> "$STAND/start-agent.mark"
[ -e "$STAND/start-agent.fail" ] && exit 1
exit 0
EOF
# tg-fail: доставка отказывает (rc 1) на всех вызовах, пока файл лежит.
cat > "$STAND/proj/scripts/tg_send.sh" <<EOF
#!/bin/sh
if [ -e "$STAND/tg-fail" ]; then printf 'FAIL %s\n' "\$*" >> "$STAND/tg.log"; exit 1; fi
printf 'CALL %s\n' "\$*" >> "$STAND/tg.log"
exit 0
EOF
# Писатель карточек — обёртка: доводы в alert.log и вызов НАСТОЯЩЕМУ писателю с
# LOG_DIR сторожа. Судится событие в журнале тревог, а не факт вызова: повторное
# снятие закрытой карточки — штатный ход, а снятия открытой быть обязано.
cat > "$STAND/proj/scripts/alert-task.py" <<EOF
import os, sys
open("$STAND/alert.log", "a").write(" ".join(sys.argv[1:]) + "\\n")
os.execv(sys.executable, [sys.executable, "$HERE/alert-task.py", *sys.argv[1:]])
EOF
chmod +x "$STAND"/bin/* "$STAND"/proj/scripts/*.sh
ln -s "$LIB_PY" "$STAND/proj/scripts/lib/agent_processes.py"

# Все функции сторожа верхнего уровня: какие помощники выделит код, проба
# заранее не знает, а заглавие тела — «^}» строкой целиком.
FUNCS="$STAND/warden-funcs.sh"
awk '/^[^[:space:]#()=]+\(\) *\{/{on=1; if ($0 ~ /\}[[:space:]]*$/) {print; on=0; next}}
     on{print} on&&/^\}[[:space:]]*$/{on=0}' "$WARDEN" > "$FUNCS"
# Круг «подъём не удался»: совет перезапустить юнит — забота refusal_hint,
# верхний уровень сторожа зовёт её после неудачного start_agent.
printf '%s\n' 'w8_round() { start_agent || refusal_hint; }' >> "$FUNCS"
case_check "функции сторожа вынуты и разбираются bash" \
    "да" "$(bash -n "$FUNCS" 2>/dev/null && [ -s "$FUNCS" ] && echo да || echo нет)"
case_check "библиотека agent_processes.py есть" "да" "$([ -f "$LIB_PY" ] && echo да || echo нет)"

# ── поддельное дерево процессов ─────────────────────────────────────────────
MY_UID=$(id -u)
# Настоящий claude опознаётся по цели <pid>/exe. Живая форма на этой машине
# (readlink /proc/<pid>/exe основного агента, 29.10.2026):
CLAUDE_EXE="/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
# MKPROC_EXE=<цель> перед mkproc — заглушка: comm=claude, а исполняет интерпретатор.
mkproc() {  # $1=корень $2=pid $3=ppid $4=comm $5..=cmdline
    local root="$1" pid="$2" ppid="$3" comm="$4" exe; shift 4
    mkdir -p "$root/$pid"
    case "$comm" in
        claude) exe="$CLAUDE_EXE" ;;
        "tmux: server") exe=/usr/bin/tmux ;;
        *) exe=/bin/bash ;;
    esac
    ln -s "${MKPROC_EXE:-$exe}" "$root/$pid/exe"
    printf '%s\n' "$comm" > "$root/$pid/comm"
    printf 'Name:\t%s\nPid:\t%s\nPPid:\t%s\nUid:\t%s\t%s\t%s\t%s\n' \
        "$comm" "$pid" "$ppid" "$MY_UID" "$MY_UID" "$MY_UID" "$MY_UID" > "$root/$pid/status"
    printf '%s\0' "$@" > "$root/$pid/cmdline"
}
new_tree() {  # $1=имя; печатает корень с одним init
    local root="$STAND/proc-$1"
    rm -rf "$root"
    mkdir -p "$root/1"
    printf 'systemd\n' > "$root/1/comm"
    printf 'Name:\tsystemd\nPid:\t1\nPPid:\t0\nUid:\t0\t0\t0\t0\n' > "$root/1/status"
    printf '/sbin/init\0' > "$root/1/cmdline"
    ln -s /usr/lib/systemd/systemd "$root/1/exe"
    printf '%s\n' "$root"
}

reset_logs() {
    rm -f "$STAND/tmux.log" "$STAND/kill.log" "$STAND/start-agent.mark" "$STAND/tg.log" \
          "$STAND/kill-delay" "$STAND/tg-fail" "$STAND/start-agent.fail"
}

# Вызов функции сторожа в его окружении; печатает код возврата.
warden_call() {  # $1=корень дерева $2=LOG_DIR $3..=функция и доводы
    local root="$1" logdir="$2"; shift 2
    [ -n "$root" ] && [ -d "$root" ] || { echo "нет-дерева"; return; }
    mkdir -p "$logdir" "$STAND/hb"
    # Без CLAUDE_CODE_SESSION_ID: настоящий писатель отказал бы как зову из сессии.
    env -u CLAUDE_CODE_SESSION_ID PATH="$STAND/bin:$PATH" PROJECT_DIR="$STAND/proj" HARNESS_SCRIPTS="$STAND/proj/scripts" \
        LOG_DIR="$logdir" HEARTBEAT_DIR="$STAND/hb" TMUX_SESSION="$SES" \
        AGENT_START_CMD="true" AGENT_START_TIMEOUT_SEC=5 NEST_RESTORE_WAIT_SEC="${W_NEST_WAIT:-5}" \
        TWO_AGENTS_CONFIRM_SEC="${W_CONFIRM_SEC:-}" \
        TWO_AGENTS_ALARM="${W_ALARM:-1}" \
        AGENT_PROC_ROOT="$root" AGENT_MAINPID=0 AGENT_UNIT="w1t-no-such-unit" \
        AGENT_USER="$(id -un)" TG_CHAT_ID="" TG_TOKEN_FILE=/nonexistent \
        bash -c 'set -euo pipefail
                 source "$1/lib/config.sh"; source "$1/lib/pane.sh"; source "$2"
                 shift 2; "$@"' _ "$HERE" "$FUNCS" "$@" >> "$STAND/warden.out" 2>&1
    echo $?
}

# События ключа session-warden:two-agents в журнале тревог: строки разбираются
# как JSON, нет журнала — ноль событий.
journal_events() {  # $1=LOG_DIR $2=событие
    python3 - "$1/тревоги-сторожей.jsonl" "$2" <<'PY'
import json, sys
try:
    lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
except OSError:
    lines = []
rows = [json.loads(l) for l in lines if l.strip()]
print(sum(1 for r in rows if r.get("ключ") == "session-warden:two-agents" and r.get("событие") == sys.argv[2]))
PY
}
has_mark() { [ -s "$STAND/start-agent.mark" ] && echo да || echo нет; }
new_session_called() { grep -qw 'new-session' "$STAND/tmux.log" 2>/dev/null && echo да || echo нет; }
# Только доставленные (CALL): отказы доставки пишутся строкой FAIL.
tg_calls() { local n; n=$(grep -c "^CALL.*${1:-}" "$STAND/tg.log" 2>/dev/null) || true; echo "${n:-0}"; }

# ── (а) БОЛЬНОЙ СЛУЧАЙ: сессия жива, сокет удалён ───────────────────────────
echo "── (а) гнездо пропало при живом сервере: вернуть сокет, а не поднять второй ──"
own_tmux new-session -d -s "$SES" "sleep 600"
MY_SERVER_PID=$(own_tmux display -p -t "$SES" '#{pid}')
printf '%s\n' "$MY_SERVER_PID" > "$STAND/own-server.pid"
SOCK="$TMUX_TMPDIR/tmux-$MY_UID/$SOCK_NAME"
case_check "свой сервер поднят, сокет на месте" \
    "да;да" "$([ -n "$MY_SERVER_PID" ] && echo -n да || echo -n нет);$([ -S "$SOCK" ] && echo да || echo нет)"
rm -f "$SOCK"
case_check "больной случай воспроизведён: has-session НЕТ, сервер жив" \
    "нет;жив" "$(own_tmux has-session -t "$SES" 2>/dev/null && echo -n да || echo -n нет);$([ -d "/proc/$MY_SERVER_PID" ] && echo жив || echo мёртв)"

ROOT=$(new_tree lost)
mkproc "$ROOT" "$MY_SERVER_PID" 1 "tmux: server" tmux new-session -d -s "$SES"
mkproc "$ROOT" 90001 "$MY_SERVER_PID" claude claude --dangerously-skip-permissions
reset_logs
RC=$(warden_call "$ROOT" "$STAND/log-a" start_agent)
case_check "start_agent вернул гнездо — rc 0" "0" "$RC"
case_check "start-agent.sh НЕ зван (вторая смена не поднята)" "нет" "$(has_mark)"
case_check "tmux new-session НЕ зван" "нет" "$(new_session_called)"
case_check "сигнал USR1 ушёл своему серверу" "да" \
    "$(grep -qx -- "-USR1 $MY_SERVER_PID" "$STAND/kill.log" 2>/dev/null && echo да || echo нет)"
case_check "после USR1 has-session снова отвечает" "да" \
    "$(own_tmux has-session -t "$SES" 2>/dev/null && echo да || echo нет)"
case_check "сервер tmux один и тот же" "$MY_SERVER_PID" "$(own_tmux display -p -t "$SES" '#{pid}' 2>/dev/null)"
own_tmux kill-server 2>/dev/null
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -d "/proc/$MY_SERVER_PID" ] || break; sleep 0.3; done
if [ -d "/proc/$MY_SERVER_PID" ]; then
    /bin/kill -USR1 "$MY_SERVER_PID" 2>/dev/null; sleep 0.5; own_tmux kill-server 2>/dev/null
fi
MY_SERVER_PID=""
: > "$STAND/own-server.pid"

# ── (а2) гнездо пропало, сигнал не вернул его ───────────────────────────────
echo "── (а2) гнездо не вернулось за срок: отказ и тревога, не новый сервер ──"
ROOT=$(new_tree lost-dead)
mkproc "$ROOT" 90100 1 "tmux: server" tmux new-session -d -s "$SES"
mkproc "$ROOT" 90101 90100 claude claude --dangerously-skip-permissions
reset_logs
RC=$(warden_call "$ROOT" "$STAND/log-a2" start_agent)
case_check "гнездо не вернулось — rc 1" "1" "$RC"
case_check "USR1 послан серверу из дерева" "да" \
    "$(grep -qx -- "-USR1 90100" "$STAND/kill.log" 2>/dev/null && echo да || echo нет)"
case_check "start-agent.sh НЕ зван" "нет" "$(has_mark)"
case_check "tmux new-session НЕ зван" "нет" "$(new_session_called)"
case_check "тревога владельцу ушла" "да" "$([ "$(tg_calls)" -ge 1 ] && echo да || echo нет)"

# ── (б) никого нет: штатный подъём ──────────────────────────────────────────
echo "── (б) ни сервера, ни агента — штатный подъём ──"
ROOT=$(new_tree absent)
reset_logs
RC=$(warden_call "$ROOT" "$STAND/log-b" start_agent)
case_check "start-agent.sh зван (подъём не сломан)" "да" "$(has_mark)"
case_check "USR1 никому не слан" "нет" "$(grep -q USR1 "$STAND/kill.log" 2>/dev/null && echo да || echo нет)"

# ── (в) агент жив вне tmux ──────────────────────────────────────────────────
echo "── (в) агент жив вне tmux, сервера нет — отказ, без второй смены ──"
ROOT=$(new_tree orphan)
mkproc "$ROOT" 90200 1 claude claude --dangerously-skip-permissions
reset_logs
: > "$STAND/warden.out"
RC=$(warden_call "$ROOT" "$STAND/log-c" start_agent)
case_check "orphan — rc 1" "1" "$RC"
case_check "start-agent.sh НЕ зван" "нет" "$(has_mark)"
case_check "tmux new-session НЕ зван" "нет" "$(new_session_called)"
case_check "тревога владельцу ушла" "да" "$([ "$(tg_calls)" -ge 1 ] && echo да || echo нет)"
case_check "при живом агенте нет подсказки «systemctl restart harness-agent»" "нет" \
    "$(cat "$STAND/tg.log" "$STAND/warden.out" 2>/dev/null | grep -q 'systemctl restart harness-agent' && echo да || echo нет)"

# ── (г) две смены: круг сторожа ─────────────────────────────────────────────
echo "── (г) два агента: файл состояния и одна тревога после подтверждения ──"
case_check "check_two_agents есть в сторожe" "да" \
    "$(grep -q '^check_two_agents() *{' "$WARDEN" && echo да || echo нет)"
# Счёт, а не grep -q: под pipefail ранний выход -q рвёт трубу (SIGPIPE, 141).
CALLS=$(grep -v '^check_two_agents() *{' "$WARDEN" | grep -v '^[[:space:]]*#' | grep -cw 'check_two_agents' || true)
case_check "check_two_agents зовётся вне своего тела (круг сторожа)" "да" \
    "$([ "${CALLS:-0}" -gt 0 ] && echo да || echo нет)"
ROOT=$(new_tree two)
mkproc "$ROOT" 90300 1 "tmux: server" tmux new-session -d -s agent
mkproc "$ROOT" 90301 90300 claude claude
mkproc "$ROOT" 90400 1 "tmux: server" tmux new-session -d -s agent
mkproc "$ROOT" 90401 90400 claude claude
LOGD="$STAND/log-d"
STATE="$LOGD/two-agents.state"
reset_logs
warden_call "$ROOT" "$LOGD" check_two_agents >/dev/null
SINCE1=$(sed -n 's/^since=//p' "$STATE" 2>/dev/null)
case_check "первый круг двух смен — тревоги нет (ждёт подтверждения)" "0" "$(tg_calls 'session-warden:two-agents')"
warden_call "$ROOT" "$LOGD" check_two_agents >/dev/null
case_check "второй круг тот же состав, но порог по умолчанию не прошёл — тревоги нет" "0" \
    "$(tg_calls 'session-warden:two-agents')"
sleep 1.1
W_CONFIRM_SEC=1 warden_call "$ROOT" "$LOGD" check_two_agents >/dev/null
W_CONFIRM_SEC=1 warden_call "$ROOT" "$LOGD" check_two_agents >/dev/null
case_check "файл состояния есть" "да" "$([ -f "$STATE" ] && echo да || echo нет)"
case_check "count=2" "2" "$(sed -n 's/^count=//p' "$STATE" 2>/dev/null)"
case_check "pids= называет обоих агентов" "да;да" \
    "$(sed -n 's/^pids=//p' "$STATE" 2>/dev/null | grep -qw 90301 && echo -n да || echo -n нет);$(sed -n 's/^pids=//p' "$STATE" 2>/dev/null | grep -qw 90401 && echo да || echo нет)"
case_check "since= и updated= заполнены" "да" \
    "$([ -n "$SINCE1" ] && grep -q '^updated=.' "$STATE" 2>/dev/null && echo да || echo нет)"
case_check "since= не сдвинулся на втором круге" "${SINCE1:-нет-since}" "$(sed -n 's/^since=//p' "$STATE" 2>/dev/null | grep . || echo пусто)"
case_check "тот же состав дольше порога — ровно одна тревога session-warden:two-agents" "1" "$(tg_calls 'session-warden:two-agents')"

# Выключатель данными: подтверждённые две смены при TWO_AGENTS_ALARM=0 — молчание.
LOGK="$STAND/log-killswitch"
reset_logs
W_ALARM=0 W_CONFIRM_SEC=0 warden_call "$ROOT" "$LOGK" check_two_agents >/dev/null
W_ALARM=0 W_CONFIRM_SEC=0 warden_call "$ROOT" "$LOGK" check_two_agents >/dev/null
case_check "TWO_AGENTS_ALARM=0: подтверждённые две смены — тревог 0" "0" "$(tg_calls 'session-warden:two-agents')"

# БОЛЬНОЙ СЛУЧАЙ 30.10.2026: на каждом круге второй — новый короткоживущий pid
# (3711826, 3872376, 4045210). Это не смена: тревоги быть не должно.
LOGF="$STAND/log-flicker"
reset_logs
for second in 3711826 3872376 4045210; do
    ROOT=$(new_tree "flicker-$second")
    mkproc "$ROOT" 90300 1 "tmux: server" tmux new-session -d -s agent
    mkproc "$ROOT" 90301 90300 claude claude
    mkproc "$ROOT" 90400 1 "tmux: server" tmux new-session -d -s agent
    mkproc "$ROOT" "$second" 90400 claude claude
    sleep 1.1
    W_CONFIRM_SEC=1 warden_call "$ROOT" "$LOGF" check_two_agents >/dev/null
done
case_check "три круга, второй каждый раз новый pid — тревог 0" "0" "$(tg_calls 'session-warden:two-agents')"
case_check "since= сброшен при смене состава (pids= — последний)" "да" \
    "$(sed -n 's/^pids=//p' "$LOGF/two-agents.state" 2>/dev/null | grep -qw 4045210 && echo да || echo нет)"

STATE_BEFORE=$([ -f "$STATE" ] && echo да || echo нет)
ROOT=$(new_tree one)
mkproc "$ROOT" 90300 1 "tmux: server" tmux new-session -d -s agent
mkproc "$ROOT" 90301 90300 claude claude
mkproc "$ROOT" 90302 90301 claude claude
warden_call "$ROOT" "$LOGD" check_two_agents >/dev/null
case_check "одна смена (с подагентом) — файл состояния был и снят" "да;нет" "$STATE_BEFORE;$([ -e "$STATE" ] && echo да || echo нет)"
case_check "тревога была, условие ушло — карточка two-agents снята (resolve)" "1" \
    "$(grep -c '^--resolve session-warden:two-agents$' "$STAND/alert.log" 2>/dev/null || true)"
# Карточки в журнале нет — снимать нечего: события снятия не появляется.
warden_call "$ROOT" "$LOGD" check_two_agents >/dev/null
case_check "карточки two-agents нет — события снятия в журнале нет" "0" \
    "$(journal_events "$LOGD" 'зелёное')"

# ── (г2) карточка открыта, метки .told нет ──────────────────────────────────
# Метку стёрли (перезапуск, уборка), а запись в журнале тревог открыта: судья
# видит одну смену — снять обязан, иначе тревога висит красной вечно.
echo "── (г2) журнал открыт, .told нет, одна смена — карточка снимается один раз ──"
LOGJ="$STAND/log-journal"
mkdir -p "$LOGJ"
printf '%s\n' '{"ts": "2026-11-03T00:00:00.000000+00:00", "ключ": "session-warden:two-agents", "событие": "заведена", "id": "hr.w1t-two-agents", "сторож": "session-warden", "почему": ""}' \
    > "$LOGJ/тревоги-сторожей.jsonl"
warden_call "$ROOT" "$LOGJ" check_two_agents >/dev/null
case_check "г2: журнал открыт, .told нет, одна смена — событие снятия в журнале" "1" \
    "$(journal_events "$LOGJ" 'зелёное')"
warden_call "$ROOT" "$LOGJ" check_two_agents >/dev/null
case_check "г2: повторный круг при закрытой карточке — второго снятия нет" "1" \
    "$(journal_events "$LOGJ" 'зелёное')"
case_check "г2: писатель не отказал как зову из сессии" "0" \
    "$(journal_events "$LOGJ" 'из-сессии')"

# ── свой сервер под пробы с настоящим tmux ──────────────────────────────────
own_server_up() {  # $1=сессия; печатать нечего, pid — в MY_SERVER_PID и own-server.pid
    own_tmux new-session -d -s "$1" "sleep 600"
    MY_SERVER_PID=$(own_tmux display -p -t "$1" '#{pid}')
    printf '%s\n' "$MY_SERVER_PID" > "$STAND/own-server.pid"
}
own_server_down() {
    own_tmux kill-server 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do [ -d "/proc/$MY_SERVER_PID" ] || break; sleep 0.3; done
    if [ -d "/proc/$MY_SERVER_PID" ]; then
        /bin/kill -USR1 "$MY_SERVER_PID" 2>/dev/null; sleep 0.5; own_tmux kill-server 2>/dev/null
    fi
    MY_SERVER_PID=""
    : > "$STAND/own-server.pid"
}
no_usr1() { grep -q USR1 "$STAND/kill.log" 2>/dev/null && echo да || echo нет; }

# ── (а3) жив только стенд пробы -L, сервера агента нет ──────────────────────
echo "── (а3) стенд tmux -L с заглушкой claude — не гнездо агента: подъём штатный ──"
own_server_up "stand-w1t-$$"
ROOT=$(new_tree stand-only)
mkproc "$ROOT" "$MY_SERVER_PID" 1 "tmux: server" tmux -L "$SOCK_NAME" new-session -d -s "stand-w1t-$$"
mkproc "$ROOT" 90500 "$MY_SERVER_PID" bash -bash
MKPROC_EXE=/bin/bash mkproc "$ROOT" 90501 90500 claude claude --dangerously-skip-permissions
reset_logs
RC=$(warden_call "$ROOT" "$STAND/log-a3" start_agent)
case_check "стенд -L: start-agent.sh зван (штатный подъём)" "да" "$(has_mark)"
case_check "стенд -L: USR1 никому не слан" "нет" "$(no_usr1)"
case_check "стенд -L: тревоги nest-* нет" "0" "$(tg_calls 'session-warden:nest-')"
own_server_down

# ── (W3) сокет возвращается с задержкой ─────────────────────────────────────
echo "── (W3) USR1 возвращает сокет через 2 с: сторож дожидается, тревоги нет ──"
own_server_up "$SES"
rm -f "$TMUX_TMPDIR/tmux-$MY_UID/$SOCK_NAME"
ROOT=$(new_tree lost-slow)
mkproc "$ROOT" "$MY_SERVER_PID" 1 "tmux: server" tmux new-session -d -s "$SES"
mkproc "$ROOT" 90601 "$MY_SERVER_PID" claude claude --dangerously-skip-permissions
reset_logs
echo 2 > "$STAND/kill-delay"
RC=$(warden_call "$ROOT" "$STAND/log-w3" start_agent)
case_check "W3: сокет вернулся с задержкой — rc 0" "0" "$RC"
case_check "W3: USR1 ушёл своему серверу" "да" \
    "$(grep -qx -- "-USR1 $MY_SERVER_PID" "$STAND/kill.log" 2>/dev/null && echo да || echo нет)"
case_check "W3: тревог 0" "0" "$(tg_calls)"
case_check "W3: start-agent.sh НЕ зван" "нет" "$(has_mark)"
case_check "W3: has-session снова отвечает" "да" \
    "$(own_tmux has-session -t "$SES" 2>/dev/null && echo да || echo нет)"
own_server_down

# ── (W10 + антиповтор) тревога nest-lost ────────────────────────────────────
echo "── (W10) nest-lost: неудачная доставка не метит «сказано»; удачная — одна за круги ──"
ROOT=$(new_tree lost-repeat)
mkproc "$ROOT" 90700 1 "tmux: server" tmux new-session -d -s "$SES"
mkproc "$ROOT" 90701 90700 claude claude --dangerously-skip-permissions
LOGR="$STAND/log-repeat"
reset_logs
touch "$STAND/tg-fail"
RC1=$(W_NEST_WAIT=1 warden_call "$ROOT" "$LOGR" start_agent)
case_check "W10: круг 1, доставка отказала — rc 1, тревога пробовала уйти" "1;да" \
    "$RC1;$(grep -q '^FAIL.*session-warden:nest-lost' "$STAND/tg.log" 2>/dev/null && echo да || echo нет)"
rm -f "$STAND/tg-fail"
RC2=$(W_NEST_WAIT=1 warden_call "$ROOT" "$LOGR" start_agent)
case_check "W10: круг 2 после отказа шлёт снова — одна доставленная тревога" "1;1" \
    "$RC2;$(tg_calls 'session-warden:nest-lost')"
RC3=$(W_NEST_WAIT=1 warden_call "$ROOT" "$LOGR" start_agent)
case_check "антиповтор: круг 3 при том же неуспехе — новой отправки нет" "1;1" \
    "$RC3;$(tg_calls 'session-warden:nest-lost')"
case_check "три круга lost — start-agent.sh НЕ зван" "нет" "$(has_mark)"

ROOT=$(new_tree orphan-repeat)
mkproc "$ROOT" 90800 1 claude claude --dangerously-skip-permissions
LOGR="$STAND/log-repeat-orphan"
reset_logs
warden_call "$ROOT" "$LOGR" start_agent >/dev/null
warden_call "$ROOT" "$LOGR" start_agent >/dev/null
case_check "антиповтор: два круга orphan — одна тревога" "1" "$(tg_calls 'session-warden:nest-')"

# ── (W10) тревога о двух сменах: неудачная доставка не метит .told ─────────
echo "── (W10) две смены: отказ доставки — .told нет, следующий круг шлёт ──"
ROOT=$(new_tree two-fail)
mkproc "$ROOT" 90300 1 "tmux: server" tmux new-session -d -s agent
mkproc "$ROOT" 90301 90300 claude claude
mkproc "$ROOT" 90400 1 "tmux: server" tmux new-session -d -s agent2
mkproc "$ROOT" 90401 90400 claude claude
LOGT="$STAND/log-two-fail"
reset_logs
W_CONFIRM_SEC=0 warden_call "$ROOT" "$LOGT" check_two_agents >/dev/null
touch "$STAND/tg-fail"
W_CONFIRM_SEC=0 warden_call "$ROOT" "$LOGT" check_two_agents >/dev/null
case_check "W10: доставка отказала — попытка была, .told не появился" "да;нет" \
    "$(grep -q '^FAIL.*session-warden:two-agents' "$STAND/tg.log" 2>/dev/null && echo -n да || echo -n нет);$([ -e "$LOGT/two-agents.state.told" ] && echo да || echo нет)"
rm -f "$STAND/tg-fail"
W_CONFIRM_SEC=0 warden_call "$ROOT" "$LOGT" check_two_agents >/dev/null
case_check "W10: следующий круг шлёт снова и метит .told" "1;да" \
    "$(tg_calls 'session-warden:two-agents');$([ -e "$LOGT/two-agents.state.told" ] && echo да || echo нет)"

# ── заглушки claude и стенды рядом с агентом — не вторая смена ─────────────
echo "── заглушки claude (exe=интерпретатор) и стенд -L рядом с агентом — не две смены ──"
ROOT=$(new_tree stubs)
mkproc "$ROOT" 90300 1 "tmux: server" tmux new-session -d -s agent
mkproc "$ROOT" 90310 90300 bash -bash
mkproc "$ROOT" 90301 90310 claude claude
mkproc "$ROOT" 90320 90300 bash -bash
MKPROC_EXE=/bin/bash mkproc "$ROOT" 90321 90320 claude claude --dangerously-skip-permissions
MKPROC_EXE=/usr/bin/python3 mkproc "$ROOT" 90330 1 claude claude --dangerously-skip-permissions
# Живой образец 30.10.2026: копия /bin/cat под именем claude прямо под сервером агента.
MKPROC_EXE=/tmp/warden-subwork.ysT2ZI/claude mkproc "$ROOT" 90340 90300 claude /tmp/warden-subwork.ysT2ZI/claude
mkproc "$ROOT" 90900 1 "tmux: server" tmux -L "w1t-stand" new-session -d -s agent
MKPROC_EXE=/bin/bash mkproc "$ROOT" 90901 90900 claude claude
mkproc "$ROOT" 90950 1 "tmux: server" tmux -S /tmp/w1t-stand.sock new-session -d -s agent
mkproc "$ROOT" 90951 90950 claude claude
LOGS="$STAND/log-stubs"
reset_logs
warden_call "$ROOT" "$LOGS" check_two_agents >/dev/null
case_check "заглушки и стенды рядом с агентом: two-agents.state не пишется" "нет" \
    "$([ -e "$LOGS/two-agents.state" ] && echo да || echo нет)"
case_check "заглушки и стенды рядом с агентом: тревог 0" "0" "$(tg_calls)"

# ── (W8) совет «systemctl restart» не уходит при живом агенте ──────────────
echo "── (W8) refusal_hint: при lost/orphan совета перезапустить юнит нет ──"
case_check "refusal_hint есть в сторожe" "да" \
    "$(grep -q '^refusal_hint() *{' "$WARDEN" && echo да || echo нет)"
HINTS=$(grep -v '^refusal_hint() *{' "$WARDEN" | grep -v '^[[:space:]]*#' | grep -cw 'refusal_hint' || true)
case_check "refusal_hint зовётся вне своего тела" "да" "$([ "${HINTS:-0}" -gt 0 ] && echo да || echo нет)"
hint_sent() { grep -q 'systemctl restart' "$STAND/tg.log" 2>/dev/null && echo да || echo нет; }
ROOT=$(new_tree w8-lost)
mkproc "$ROOT" 91000 1 "tmux: server" tmux new-session -d -s "$SES"
mkproc "$ROOT" 91001 91000 claude claude --dangerously-skip-permissions
reset_logs
W_NEST_WAIT=1 warden_call "$ROOT" "$STAND/log-w8a" w8_round >/dev/null
case_check "W8 lost: тревога ушла, совета «systemctl restart» нет" "да;нет" \
    "$([ "$(tg_calls)" -ge 1 ] && echo -n да || echo -n нет);$(hint_sent)"
ROOT=$(new_tree w8-orphan)
mkproc "$ROOT" 91100 1 claude claude --dangerously-skip-permissions
reset_logs
warden_call "$ROOT" "$STAND/log-w8b" w8_round >/dev/null
case_check "W8 orphan: тревога ушла, совета «systemctl restart» нет" "да;нет" \
    "$([ "$(tg_calls)" -ge 1 ] && echo -n да || echo -n нет);$(hint_sent)"
ROOT=$(new_tree w8-absent)
reset_logs
touch "$STAND/start-agent.fail"
warden_call "$ROOT" "$STAND/log-w8c" w8_round >/dev/null
case_check "W8 никого нет и подъём не удался: совет «systemctl restart» есть (проба жива)" "да" "$(hint_sent)"

case_check "systemctl ни разу не спрошен (MainPID задан)" "" "$(cat "$STAND/systemctl.log" 2>/dev/null)"

printf '\nГНЕЗДО И ДВЕ СМЕНЫ: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
