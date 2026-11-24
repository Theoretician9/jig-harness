#!/usr/bin/env bash
# П7 (ротация с преемником): остановка юнита harness-agent гасит ОБЕ смены.
#
# Спека docs/specs/2026-11-02-ротация-ждёт-помощников.md, раздел 14.12. После
# ротации с преемником под сервером tmux агента живут две сессии: преемник
# `<TMUX_SESSION>` и дорабатывающая `<TMUX_SESSION>-old-<epoch>`. ExecStop юнита
# сегодня `tmux kill-session -t agent`: при живом преемнике точное имя побеждает
# префикс, и гаснет только он, а дорабатывающая остаётся сиротой до добивания
# всей группы; в миг между rename и new-session то же `-t agent` бьёт по
# префиксу в случайную старую. Требование: ExecStop гасит точно `=<TMUX_SESSION>`
# и все `<TMUX_SESSION>-old-<цифры>`, чужие сессии с похожим именем не трогает,
# и кончается кодом 0 (systemd считает ненулевой ExecStop провалом остановки).
#
# Судится ТЕКСТ генерируемого юнита: строка ExecStop из here-doc шага 4
# установщика (USTANOVIT.sh, дверь HARNESS_INSTALLER) исполняется на стенде:
# свой сервер tmux (сокет «проба-$$», TMUX_TMPDIR стенда, обёртка tmux в PATH),
# `$PROJECT_DIR` → корень этого дерева, `/usr/bin/tmux` → обёртка. Итог — строка
# «ПРОБА ЗЕЛЁНАЯ: …» / «ПРОБА КРАСНАЯ: …», код 0 / 1; стенд не годен — «ПРОБА
# СЛОМАНА: …», код 2.
set -uo pipefail
export LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"
TREE="$(cd "$HERE/.." && pwd)"   # каталог, где лежит scripts/start-agent.sh: проект или harness/ пакета

INSTALLER="${HARNESS_INSTALLER:-}"
for c in "$HERE/../../USTANOVIT.sh" /home/user/starter/STARTER-PACKAGE/USTANOVIT.sh; do
    [ -n "$INSTALLER" ] || { [ -f "$c" ] && INSTALLER="$c"; }
done
[ -f "$INSTALLER" ] || { echo "ПРОБА СЛОМАНА: П7 — установщика USTANOVIT.sh нет"; exit 2; }

STAND=$(mktemp -d)
REAL_TMUX="$(command -v tmux)"
SOCK_NAME="проба-$$"
unset TMUX TMUX_PANE
export TMUX_TMPDIR="$STAND/tmux"
mkdir -p "$STAND/tmux-bin" "$TMUX_TMPDIR" "$STAND/log"
printf '#!/bin/sh\nexec "%s" -L "%s" "$@"\n' "$REAL_TMUX" "$SOCK_NAME" > "$STAND/tmux-bin/tmux"
chmod +x "$STAND/tmux-bin/tmux"
export PATH="$STAND/tmux-bin:$PATH"
trap '"$REAL_TMUX" -L "$SOCK_NAME" kill-server 2>/dev/null; rm -rf "$STAND"' EXIT

NAME="agent"
printf 'TMUX_SESSION="%s"\nPROJECT_DIR="%s"\nLOG_DIR="%s"\n' "$NAME" "$STAND" "$STAND/log" > "$STAND/install.conf"
printf 'AGENT_START_CMD="sleep 600"\n' > "$STAND/harness.conf"
export HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" KICK_CMD="true"

# ExecStop из here-doc юнита harness-agent (между «записать_файл …harness-agent.service» и EOF).
STOP_LINE=$(awk '/записать_файл \/etc\/systemd\/system\/harness-agent\.service/ {on=1; next}
                 on && /^EOF$/ {exit} on && /^ExecStop=/ {sub(/^ExecStop=/, ""); print}' "$INSTALLER")
[ -n "$STOP_LINE" ] || { echo "ПРОБА СЛОМАНА: П7 — в юните установщика нет строки ExecStop ($INSTALLER)"; exit 2; }
STOP_CMD="${STOP_LINE//\$PROJECT_DIR/$TREE}"
STOP_CMD="${STOP_CMD//\/usr\/bin\/tmux/tmux}"
# Стенд не выпускает команду на боевой сервер: ни абсолютного tmux, ни своего сокета.
case "$STOP_CMD" in
    */tmux\ *|*" -L"*|*" -S"*) echo "ПРОБА СЛОМАНА: П7 — ExecStop обходит обёртку стенда: $STOP_CMD"; exit 2 ;;
esac

# Сервер стенда держит сессия-держатель: без неё сервер гаснет вместе с последней
# сессией, и следующий new-session гонится с его выходом («server exited unexpectedly»).
KEEPER="стенд-держатель"
stand() {  # имена сессий стенда
    local s
    "$REAL_TMUX" -L "$SOCK_NAME" has-session -t "=$KEEPER" 2>/dev/null \
        || "$REAL_TMUX" -L "$SOCK_NAME" new-session -d -s "$KEEPER" "sleep 3600" || return 1
    for s in $("$REAL_TMUX" -L "$SOCK_NAME" list-sessions -F '#{session_name}' 2>/dev/null); do
        [ "$s" = "$KEEPER" ] || "$REAL_TMUX" -L "$SOCK_NAME" kill-session -t "=$s" 2>/dev/null
    done
    for s in "$@"; do "$REAL_TMUX" -L "$SOCK_NAME" new-session -d -s "$s" "sleep 600" || return 1; done
}
alive() {  # $1 = имя → да / нет
    "$REAL_TMUX" -L "$SOCK_NAME" has-session -t "=$1" 2>/dev/null && echo да || echo нет
}
run_stop() {
    STOP_RC=0
    bash -c "$STOP_CMD" >"$STAND/stop.out" 2>&1 || STOP_RC=$?
}

stand "$NAME" >/dev/null 2>&1
[ "$(alive "$NAME")" = да ] || { echo "ПРОБА СЛОМАНА: П7 — сервер tmux стенда не поднялся"; exit 2; }
echo "ExecStop юнита (на стенде): $STOP_CMD"

echo "Ю1. преемник и две дорабатывающие живы — остановка юнита"
stand "$NAME" "$NAME-old-1795209000" "$NAME-old-1795209999" "${NAME}X" "чужая" || { echo "ПРОБА СЛОМАНА: П7 — сессии стенда"; exit 2; }
run_stop
case_check "Ю1 преемник «$NAME» погашен" "нет" "$(alive "$NAME")"
case_check "Ю1 дорабатывающая «$NAME-old-1795209000» погашена" "нет" "$(alive "$NAME-old-1795209000")"
case_check "Ю1 дорабатывающая «$NAME-old-1795209999» погашена" "нет" "$(alive "$NAME-old-1795209999")"
case_check "Ю1 похожая по префиксу «${NAME}X» не тронута" "да" "$(alive "${NAME}X")"
case_check "Ю1 чужая сессия не тронута" "да" "$(alive "чужая")"
case_check "Ю1 ExecStop кончился кодом 0" "0" "$STOP_RC"

echo "Ю2. миг между rename и new-session: преемника ещё нет, одна дорабатывающая"
stand "$NAME-old-1795209000" "чужая" || { echo "ПРОБА СЛОМАНА: П7 — сессии стенда"; exit 2; }
run_stop
case_check "Ю2 дорабатывающая погашена" "нет" "$(alive "$NAME-old-1795209000")"
case_check "Ю2 чужая сессия не тронута" "да" "$(alive "чужая")"
case_check "Ю2 ExecStop кончился кодом 0" "0" "$STOP_RC"

echo "Ю3. сессий агента нет вовсе (юнит останавливают после краха)"
stand "чужая" || { echo "ПРОБА СЛОМАНА: П7 — сессии стенда"; exit 2; }
run_stop
case_check "Ю3 ExecStop кончился кодом 0" "0" "$STOP_RC"
case_check "Ю3 чужая сессия не тронута" "да" "$(alive "чужая")"
case_check "сессия-держатель стенда пережила все остановки" "да" "$(alive "$KEEPER")"

if [ "$CASES_FAILED" -eq 0 ]; then
    echo "ПРОБА ЗЕЛЁНАЯ: П7 остановка юнита — путей $CASES_PATHS, гаснут преемник и все дорабатывающие, чужие целы"
    exit 0
fi
echo "ПРОБА КРАСНАЯ: П7 остановка юнита — неудач $CASES_FAILED из $CASES_PATHS (ExecStop: $STOP_LINE)"
exit 1
