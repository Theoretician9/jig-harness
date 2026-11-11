#!/usr/bin/env bash
# Проба: журнал панели агента, тревога о раннем выходе, каталог запуска claude.
#
# Что судится, на стенде со своим tmux (-L), своими конфигами и заглушками:
#   1. start-agent.sh включает запись панели ДО команды: вывод упавшей команды
#      запуска лежит в agent-pane.log;
#   2. сторож при выходе агента через секунды после запуска шлёт тревогу с
#      хвостом панели, в хвосте нет значения секрета, а повтор молчит;
#   3. агент, проработавший дольше порога, тревоги о раннем выходе не вызывает;
#   4. claude в панели, запущенный не из PROJECT_DIR, — сторож объявляет
#      ротацию и говорит владельцу; из PROJECT_DIR — молчит;
#   5. хук старта сессии первой строкой предупреждает о чужом каталоге.
# Живой tmux агента проба не видит: свой сокет, kill-заглушка, поддельное
# дерево процессов, свой PROJECT_DIR. Дверь для мутации — HARNESS_SESSION_WARDEN.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

WARDEN="${HARNESS_SESSION_WARDEN:-$HERE/../harness/demons/session-warden.sh}"
[ -r "$WARDEN" ] || WARDEN="$HERE/../demons/session-warden.sh"
WARDEN="$(readlink -f "$WARDEN")"
START_AGENT="$HERE/start-agent.sh"
HOOK_SRC="$HERE/hooks/session_state.sh"
[ -r "$HOOK_SRC" ] || HOOK_SRC="$HERE/../hooks/session_state.sh"
LIB_PY="$(readlink -f "$HERE/lib/agent_processes.py")"
REAL_TMUX="$(command -v tmux)"

STAND=$(mktemp -d)
SOCK_NAME="apl-$$"
SES="agent-apl-$$"
unset TMUX TMUX_PANE
export TMUX_TMPDIR="$STAND/tmux"
mkdir -p "$STAND/proj/sub"
mkdir -p "$TMUX_TMPDIR" "$STAND/bin" "$STAND/proj/scripts/lib" "$STAND/home" "$STAND/projects" "$STAND/panel" "$STAND/other"

cleanup() { "$REAL_TMUX" -L "$SOCK_NAME" kill-server 2>/dev/null; rm -rf "$STAND"; }
trap cleanup EXIT

printf '#!/bin/sh\nexec "%s" -L "%s" "$@"\n' "$REAL_TMUX" "$SOCK_NAME" > "$STAND/bin/tmux"
printf '#!/bin/sh\necho "$*" >> "%s/kill.log"\nexit 0\n' "$STAND" > "$STAND/bin/kill"
# «claude» стенда: долгоживущий процесс с comm=claude.
cp "$(command -v sleep)" "$STAND/bin/claude"
printf '#!/bin/sh\necho started >> "%s/start-agent.mark"\nexit 0\n' "$STAND" > "$STAND/proj/scripts/start-agent.sh"
printf '#!/bin/sh\nprintf "CALL %%s\\n" "$*" >> "%s/tg.log"\nexit 0\n' "$STAND" > "$STAND/proj/scripts/tg_send.sh"
chmod +x "$STAND"/bin/* "$STAND"/proj/scripts/*.sh
ln -s "$LIB_PY" "$STAND/proj/scripts/lib/agent_processes.py"
# Хук ищет lib/ рядом со своим каталогом (раскладка проекта): собираем её на стенде.
mkdir -p "$STAND/proj/scripts/hooks"
cp "$HOOK_SRC" "$STAND/proj/scripts/hooks/session_state.sh"
HOOK="$STAND/proj/scripts/hooks/session_state.sh"
ln -s "$HERE/lib/config.sh" "$STAND/proj/scripts/lib/config.sh"
mkdir -p "$STAND/proc/1"
printf 'systemd\n' > "$STAND/proc/1/comm"
printf 'Name:\tsystemd\nPid:\t1\nPPid:\t0\nUid:\t0\t0\t0\t0\n' > "$STAND/proc/1/status"
printf '/sbin/init\0' > "$STAND/proc/1/cmdline"
ln -s /usr/lib/systemd/systemd "$STAND/proc/1/exe"
# Смена стенда поднята как надо (claude с файлом личности): иначе сверка
# носителя в хуке старта печатает «смена поднята без личности» первой строкой,
# и случай «чужой каталог» судил бы не своё предупреждение.
mkdir -p "$STAND/sproc/4242"
printf 'Name:\tclaude\nPid:\t4242\nPPid:\t1\n' > "$STAND/sproc/4242/status"
printf 'Метка сборки: стенд\n' > "$STAND/self.md"
printf 'claude\0--append-system-prompt-file\0%s\0' "$STAND/self.md" > "$STAND/sproc/4242/cmdline"
# Проба личности по событию (self_prompt_live_check сторожа) зовёт модель и
# настоящий alert-task; на стенде её место — заглушка.
printf '#!/bin/sh\nexit 0\n' > "$STAND/self-prompt-live-stub.sh"

# Значение «секрета» собирается из частей: целиком в файле его нет.
SECRET_TAIL="A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"  # не-секрет: поддельный хвост, проба ждёт его маскировки
SECRET="tok$(printf 'en')-$SECRET_TAIL"  # не-секрет: поддельный токен из частей выше
cat > "$STAND/home/install.conf" <<EOS
AGENT_USER=$(id -un)
PROJECT_DIR=$STAND/proj
LOG_DIR=$STAND/log
HEARTBEAT_DIR=$STAND/hb
TMUX_SESSION=$SES
EOS
cat > "$STAND/home/harness.conf" <<EOS
CTX_WINDOW_TOKENS=1000000  # не-секрет: размер окна модели в токенах
AGENT_START_CMD="sh -c 'printf \"%s%s\\n\" ЗАПУСК-УПАЛ-token- $SECRET_TAIL; exit 1'"
AGENT_START_TIMEOUT_SEC=1
AGENT_DEAD_MIN=0
AGENT_EARLY_EXIT_SEC=60
MAX_COMPACTS=1
AUTO_REBOOT_IN_ROTATION=1
REBOOT_CMD=true
CTX_ROTATE_PCT=90
EOS

stand_env() {
    PATH="$STAND/bin:$PATH" HARNESS_HOME="$STAND/home" HARNESS_INSTALL_CONF="$STAND/home/install.conf" \
        HARNESS_CONF="$STAND/home/harness.conf" CLAUDE_PROJECTS_DIR="$STAND/projects" \
        HARNESS_PANEL_STATE="$STAND/panel" AGENT_PROC_ROOT="$STAND/proc" AGENT_MAINPID=0 \
        AGENT_UNIT=apl-no-such-unit SELF_PROMPT_LIVE_SH="$STAND/self-prompt-live-stub.sh" "$@"
}
run_warden() { stand_env timeout "${1:-60}" bash "$WARDEN" 2>&1 </dev/null; }
tg() { cat "$STAND/tg.log" 2>/dev/null; }
own_tmux() { "$REAL_TMUX" -L "$SOCK_NAME" "$@"; }

# ── 1–3. журнал панели и ранний выход ───────────────────────────────────────
rm -rf "$STAND/log"; mkdir -p "$STAND/log"
KICK_CMD=true stand_env bash "$START_AGENT" >/dev/null 2>&1
sleep 1.5
LOGF="$STAND/log/agent-pane.log"
case_check "БОЛЬНОЙ СЛУЧАЙ: вывод упавшей команды запуска лежит в журнале панели" "да" \
    "$(grep -q 'ЗАПУСК-УПАЛ' "$LOGF" 2>/dev/null && echo да || echo нет)"
case_check "метка запуска поставлена" "да" "$([ -s "$STAND/log/agent-started.at" ] && echo да || echo нет)"
mkdir -p "$STAND/hb"
OUT=$(run_warden)
case_check "БОЛЬНОЙ СЛУЧАЙ: агент вышел сразу после запуска — владельцу тревога" "да" \
    "$(tg | grep -q 'Агент вышел через' && echo да || echo нет)"
case_check "в тревоге хвост панели" "да" "$(tg | grep -q 'ЗАПУСК-УПАЛ' && echo да || echo нет)"
case_check "значение секрета в тревогу не попало" "нет" "$(tg | grep -q "$SECRET" && echo да || echo нет)"
# Той же метке запуска тревога уже сказана — круг молчит (метка «сказано» = метка запуска).
rm -f "$STAND/tg.log"
cp "$STAND/log/agent-started.at" "$STAND/log/agent-early-exit.told"
OUT=$(run_warden)
case_check "повторный круг по той же метке запуска тревоги не шлёт" "нет" \
    "$(tg | grep -q 'Агент вышел через' && echo да || echo нет)"

# Агент проработал дольше порога: метка запуска намного старше последнего вывода.
rm -f "$STAND/tg.log" "$STAND/log/agent-early-exit.told"
echo $(( $(date +%s) - 3600 )) > "$STAND/log/agent-started.at"
OUT=$(run_warden)
case_check "агент жил долго — тревоги о раннем выходе нет" "нет" \
    "$(tg | grep -q 'Агент вышел через' && echo да || echo нет)"
own_tmux kill-server 2>/dev/null

# ── 4. каталог запуска claude ───────────────────────────────────────────────
ENC="$(printf '%s' "$STAND/proj" | sed 's/[^a-zA-Z0-9]/-/g')"
mkdir -p "$STAND/projects/$ENC"
printf '{"type":"user"}\n' > "$STAND/projects/$ENC/aaaa.jsonl"
# Хвост прогона срезан timeout: ротация в стенде доводить до конца незачем.
own_tmux new-session -d -s "$SES" -c "$STAND/other" "$STAND/bin/claude 300"
sleep 0.5
rm -rf "$STAND/log" "$STAND/tg.log"
OUT=$(run_warden 25)
case_check "БОЛЬНОЙ СЛУЧАЙ: claude запущен не из PROJECT_DIR — ротация объявлена" "да" \
    "$(grep -q 'нужна ротация: агент запущен не из' <<<"$OUT" && echo да || echo нет)"
case_check "владельцу сказано, где стоит агент" "да" "$(tg | grep -q "запущен не из $STAND/proj, а из $STAND/other" && echo да || echo нет)"
own_tmux kill-server 2>/dev/null
own_tmux new-session -d -s "$SES" -c "$STAND/proj" "$STAND/bin/claude 300"
sleep 0.5
rm -rf "$STAND/log" "$STAND/tg.log"
OUT=$(run_warden 25)
case_check "claude из PROJECT_DIR — каталог не жалоба" "нет" \
    "$(grep -q 'запущен не из' <<<"$OUT" && echo да || echo нет)"
own_tmux kill-server 2>/dev/null

# Ротация по чужому каталогу уже была, каталог прежний: вторую не делаем, говорим.
own_tmux new-session -d -s "$SES" -c "$STAND/other" "$STAND/bin/claude 300"
sleep 0.5
rm -rf "$STAND/log" "$STAND/tg.log"
OUT=$(run_warden 25)
case_check "после первой ротации стоит метка «уже вращали»" "да" "$([ -e "$STAND/log/agent-wrong-cwd.rotated" ] && echo да || echo нет)"
rm -f "$STAND/tg.log"
OUT=$(run_warden 25)
case_check "БОЛЬНОЙ СЛУЧАЙ: каталог всё ещё чужой — второй ротации нет" "нет" \
    "$(grep -q 'нужна ротация: агент запущен не из' <<<"$OUT" && echo да || echo нет)"
case_check "и владельцу сказано повторно, а не молчание" "да" "$(tg | grep -q 'Повторно не перезапускаю' && echo да || echo нет)"
own_tmux kill-server 2>/dev/null
own_tmux new-session -d -s "$SES" -c "$STAND/proj/sub" "$STAND/bin/claude 300"
sleep 0.5
rm -rf "$STAND/log" "$STAND/tg.log"
OUT=$(run_warden 25)
case_check "БОЛЬНОЙ СЛУЧАЙ: claude из подкаталога проекта — свой, жалобы нет" "нет" \
    "$(grep -q 'запущен не из' <<<"$OUT" && echo да || echo нет)"
own_tmux kill-server 2>/dev/null

# Перезагрузка сервера причиной «чужой каталог» не оправдана.
FUNCS="$STAND/warden-funcs.sh"
awk '/^[^[:space:]#()=]+\(\) *\{/{on=1; if ($0 ~ /\}[[:space:]]*$/) {print; on=0; next}}
     on{print} on&&/^\}[[:space:]]*$/{on=0}' "$WARDEN" > "$FUNCS"
sw() { ( export "$@"; bash -c 'source "$0"; reboot_switch' "$FUNCS" 2>/dev/null ); }
case_check "БОЛЬНОЙ СЛУЧАЙ: чужой каталог — перезагрузка при смене выключена даже при AUTO_REBOOT_IN_ROTATION=1" "0" \
    "$(sw CWD_ROTATION=1 AUTO_REBOOT_IN_ROTATION=1)"
case_check "обычная ротация — ключ перезагрузки действует" "1" "$(sw CWD_ROTATION=0 AUTO_REBOOT_IN_ROTATION=1)"

# ── труба панели: идемпотентность, права, предел размера ───────────────────
PIPE_DIR="$STAND/pipelog"
own_tmux new-session -d -s "$SES-p" -c "$STAND"
sleep 0.3
attach_n() { ( PATH="$STAND/bin:$PATH"; source "$HERE/lib/agent_pane_log.sh"
    for _ in $(seq "$1"); do pane_log_attach "$SES-p" "$PIPE_DIR"; done ); }
pipe_open() { own_tmux display-message -p -t "$SES-p" '#{pane_pipe}'; }
attach_n 2
case_check "БОЛЬНОЙ СЛУЧАЙ: два вызова подряд — труба открыта (tmux -o второй раз её закрывает)" "1" "$(pipe_open)"
attach_n 1
case_check "третий вызов — труба по-прежнему открыта" "1" "$(pipe_open)"
case_check "БОЛЬНОЙ СЛУЧАЙ: журнал панели доступен только владельцу (0600)" "600" "$(stat -c '%a' "$PIPE_DIR/agent-pane.log")"
own_tmux send-keys -t "$SES-p" "echo ПИШЕТ-В-ЖУРНАЛ" Enter
sleep 0.7
case_check "вывод панели пишется в журнал" "да" "$(grep -q 'ПИШЕТ-В-ЖУРНАЛ' "$PIPE_DIR/agent-pane.log" && echo да || echo нет)"
head -c 2097152 /dev/zero | tr '\0' 'x' >> "$PIPE_DIR/agent-pane.log"
( source "$HERE/lib/agent_pane_log.sh"; pane_log_trim "$PIPE_DIR" 1024 )
case_check "БОЛЬНОЙ СЛУЧАЙ: журнал выше предела усечён до последнего мегабайта" "да" \
    "$([ "$(stat -c '%s' "$PIPE_DIR/agent-pane.log")" -le 1048576 ] && echo да || echo нет)"
own_tmux send-keys -t "$SES-p" "echo ПОСЛЕ-УСЕЧЕНИЯ" Enter
sleep 0.7
case_check "после усечения труба пишет дальше" "да" "$(grep -q 'ПОСЛЕ-УСЕЧЕНИЯ' "$PIPE_DIR/agent-pane.log" && echo да || echo нет)"
# Замер скорости роста: синтетический «спиннер» (10 кадров/с по ~250 байт), НЕ живой claude.
S0=$(stat -c '%s' "$PIPE_DIR/agent-pane.log")
own_tmux send-keys -t "$SES-p" 'for i in $(seq 50); do printf "\033[2K\033[1G\033[38;5;208m✻ Thinking…\033[0m %s %0200d\n" "$i" 0; sleep 0.1; done' Enter
sleep 6
S1=$(stat -c '%s' "$PIPE_DIR/agent-pane.log")
printf '  замер  синтетическая перерисовка: %d КБ за 5 с = около %d МБ/ч (оценка сверху, не живой claude)\n' \
    $(( (S1 - S0) / 1024 )) $(( (S1 - S0) * 720 / 1048576 ))
own_tmux kill-server 2>/dev/null

# ── 5. хук старта ───────────────────────────────────────────────────────────
hook_first() { (cd "$1" && HARNESS_INSTALL_CONF="$STAND/home/install.conf" HARNESS_HOME="$STAND/home" \
    SESSION_PROC_ROOT="$STAND/sproc" SESSION_PROC_START=4242 \
    timeout 30 bash "$HOOK" </dev/null 2>/dev/null | head -1); }
# Вывод берётся в переменную: pipefail и ранний выход head дали бы ложное «нет».
OUT_FOREIGN=$(hook_first "$STAND/other")
OUT_HOME=$(hook_first "$STAND/proj")
case_check "БОЛЬНОЙ СЛУЧАЙ: хук из чужого каталога — первой строкой предупреждение" "да" \
    "$(grep -q 'СЕССИЯ ЗАПУЩЕНА НЕ ИЗ' <<<"$OUT_FOREIGN" && echo да || echo нет)"
case_check "хук из PROJECT_DIR — предупреждения нет" "нет" \
    "$(grep -q 'СЕССИЯ ЗАПУЩЕНА НЕ ИЗ' <<<"$OUT_HOME" && echo да || echo нет)"
OUT_SUB=$(hook_first "$STAND/proj/sub")
case_check "БОЛЬНОЙ СЛУЧАЙ: хук из подкаталога проекта — предупреждения нет" "нет" \
    "$(grep -q 'СЕССИЯ ЗАПУЩЕНА НЕ ИЗ' <<<"$OUT_SUB" && echo да || echo нет)"
case_check "сигналов не слали: боевой tmux не тронут" "" "$(cat "$STAND/kill.log" 2>/dev/null)"

printf '\nЖУРНАЛ ПАНЕЛИ И КАТАЛОГ ЗАПУСКА: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
