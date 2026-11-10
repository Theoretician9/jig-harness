#!/usr/bin/env bash
# Проба: на свежей установке (транскриптов нет) сторож всё равно смотрит панель.
#
# Откуда взята. Отчёт нового сервера 09.11.2026, дефект 10: шаг 1 демона находил
# «в ~/.claude/projects нет каталога проекта» и делал `exit 0` ДО проверки панели.
# Агент мёртв, а сторож молчит — единственный, кто мог его поднять.
#
# Что судится: ЖИВОЙ демон целиком (не вынутые функции), запущенный на стенде:
# свой каталог конфигов (HARNESS_HOME + два conf-файла), свой LOG_DIR, пустой
# CLAUDE_PROJECTS_DIR, свой сервер tmux (-L) и заглушки start-agent.sh/tg_send.sh
# в подставном PROJECT_DIR. Окружение старше конфига (konf_zagruzit), поэтому
# боевые значения стенд не подхватывает; боевой tmux проба не видит.
#
# Дверь для мутации: HARNESS_SESSION_WARDEN — путь к испорченной КОПИИ демона
# (рядом с ней должно лежать дерево scripts/, как у боевого).
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

WARDEN="${HARNESS_SESSION_WARDEN:-$HERE/../harness/demons/session-warden.sh}"
[ -r "$WARDEN" ] || WARDEN="$HERE/../demons/session-warden.sh"
WARDEN="$(readlink -f "$WARDEN")"
LIB_PY="$(readlink -f "$HERE/lib/agent_processes.py")"
REAL_TMUX="$(command -v tmux)"

STAND=$(mktemp -d)
SOCK_NAME="wnt-$$"
SES="agent-wnt-$$"
unset TMUX TMUX_PANE
export TMUX_TMPDIR="$STAND/tmux"
mkdir -p "$TMUX_TMPDIR" "$STAND/bin" "$STAND/proj/scripts/lib" "$STAND/home" "$STAND/projects" "$STAND/panel"

cleanup() { "$REAL_TMUX" -L "$SOCK_NAME" kill-server 2>/dev/null; rm -rf "$STAND"; }
trap cleanup EXIT

# tmux стенда: любой вызов демона уходит на СВОЙ сервер.
cat > "$STAND/bin/tmux" <<EOS
#!/bin/sh
exec "$REAL_TMUX" -L "$SOCK_NAME" "\$@"
EOS
cat > "$STAND/proj/scripts/start-agent.sh" <<EOS
#!/bin/sh
echo started >> "$STAND/start-agent.mark"
exit 0
EOS
cat > "$STAND/proj/scripts/tg_send.sh" <<EOS
#!/bin/sh
printf 'CALL %s\n' "\$*" >> "$STAND/tg.log"
exit 0
EOS
# kill: сигналов не шлём никому — боевой tmux агента проба не трогает.
printf '#!/bin/sh\necho "$*" >> "%s/kill.log"\nexit 0\n' "$STAND" > "$STAND/bin/kill"
# Поддельное дерево процессов: в нём один init, настоящих серверов tmux нет.
mkdir -p "$STAND/proc/1"
printf 'systemd\n' > "$STAND/proc/1/comm"
printf 'Name:\tsystemd\nPid:\t1\nPPid:\t0\nUid:\t0\t0\t0\t0\n' > "$STAND/proc/1/status"
printf '/sbin/init\0' > "$STAND/proc/1/cmdline"
ln -s /usr/lib/systemd/systemd "$STAND/proc/1/exe"
chmod +x "$STAND"/bin/* "$STAND"/proj/scripts/*.sh
ln -s "$LIB_PY" "$STAND/proj/scripts/lib/agent_processes.py"

cat > "$STAND/home/install.conf" <<EOS
AGENT_USER=$(id -un)
PROJECT_DIR=$STAND/proj
LOG_DIR=$STAND/log
HEARTBEAT_DIR=$STAND/hb
TMUX_SESSION=$SES
EOS
cat > "$STAND/home/harness.conf" <<EOS
CTX_WINDOW_TOKENS=1000000  # не-секрет: размер окна модели в токенах
AGENT_START_CMD=true
AGENT_START_TIMEOUT_SEC=1
AGENT_DEAD_MIN=0
EOS

# Прогон демона на стенде; печатает журнал прогона.
run_warden() {
    rm -rf "$STAND/log" "$STAND/hb" "$STAND/start-agent.mark" "$STAND/tg.log"
    PATH="$STAND/bin:$PATH" HARNESS_HOME="$STAND/home" HARNESS_INSTALL_CONF="$STAND/home/install.conf" \
        HARNESS_CONF="$STAND/home/harness.conf" CLAUDE_PROJECTS_DIR="$STAND/projects" \
        HARNESS_PANEL_STATE="$STAND/panel" \
        AGENT_PROC_ROOT="$STAND/proc" AGENT_MAINPID=0 AGENT_UNIT=wnt-no-such-unit \
        timeout 100 bash "$WARDEN" 2>&1 </dev/null
}
marked() { [ -e "$STAND/start-agent.mark" ] && echo да || echo нет; }

# 1. Каталога проекта в projects нет, сессии tmux нет: агента нет вовсе.
OUT=$(run_warden)
case_check "БОЛЬНОЙ СЛУЧАЙ: транскриптов нет, сессии нет — подъём начат, а не тихий выход" "да" "$(marked)"
case_check "в журнале названа причина «агента нет», не «надзирать не за чем»" "да" \
    "$(grep -q 'агента нет вовсе' <<<"$OUT" && ! grep -q 'надзирать не за чем' <<<"$OUT" && echo да || echo нет)"

# 2. Каталог projects есть, но пуст (Claude уже стоит, сессий не было).
mkdir -p "$STAND/projects"
OUT=$(run_warden)
case_check "пустой projects, сессии нет — подъём начат" "да" "$(marked)"

# 3. Сессия есть, в панели оболочка (агент вышел): порог 0 мин — поднимать сразу.
"$REAL_TMUX" -L "$SOCK_NAME" new-session -d -s "$SES" "bash --norc"
sleep 0.5
OUT=$(run_warden)
case_check "транскриптов нет, в панели оболочка — сторож пошёл по ветке «агент выпал»" "да" \
    "$(grep -q "в панели 'bash'" <<<"$OUT" && echo да || echo нет)"

# 4. Здоровый путь не сломан: транскрипт есть — шаг 1 идёт дальше, к окну.
"$REAL_TMUX" -L "$SOCK_NAME" kill-server 2>/dev/null
ENC="${STAND//\//-}/proj"; ENC="$(printf '%s' "$STAND/proj" | sed 's/[^a-zA-Z0-9]/-/g')"
mkdir -p "$STAND/projects/$ENC"
printf '{"type":"user"}\n' > "$STAND/projects/$ENC/aaaa.jsonl"
OUT=$(run_warden)
case_check "транскрипт есть — демон дошёл до замера окна (прежний путь цел)" "да" \
    "$(grep -q 'сессия aaaa' <<<"$OUT" && echo да || echo нет)"

case_check "сигналов не слали: боевой tmux не тронут" "" "$(cat "$STAND/kill.log" 2>/dev/null)"
printf '\nСТОРОЖ БЕЗ ТРАНСКРИПТОВ: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
