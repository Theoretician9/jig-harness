#!/usr/bin/env bash
# Одна дверь в панель агента: нажатия в tmux и признак «кто в панели».
#
# ЗАЧЕМ ОДНА. Эти семь строк жили двумя побайтово равными копиями — в
# session-warden.sh и tg-dispatcher.sh. Копий стало бы три, когда пинок
# понадобился ежеминутному limit-watch.sh (владелец 17.10.2026: «дернуть он
# должен ровно в тот момент когда лимит обновится»), а две копии одного
# признака расходятся молча.
#
# Ждёт в окружении: TMUX_SESSION (имя панели), PANE_ENTER_DELAY (пауза перед
# Enter, по умолчанию 0,7 с).

# Текст в панель. Перевод строки заменяется пробелом: tmux принял бы его за
# отдельное нажатие и отправил бы половину текста.
#
# Ключи `-l --` обязательны: без них tmux принимает текст за ИМЯ КЛАВИШИ
# (память: tmux-send-keys-takes-key-name), а Enter подаётся отдельно — своим
# нажатием, иначе строка остаётся в приглашении ненабранной.
send_to_pane() {  # текст
    local flat="${1//$'\n'/ }"
    flat="${flat//$'\r'/ }"
    tmux send-keys -t "${TMUX_SESSION:-}" -l -- "$flat" || return 1
    sleep "${PANE_ENTER_DELAY:-0.7}"
    tmux send-keys -t "${TMUX_SESSION:-}" Enter || return 1
}

# Что сейчас в панели: claude/node — агент, иное — оболочка, и текст в неё
# исполнился бы как команды. «?» — панели нет вовсе, «мертва» — панель
# пережила свой процесс.
#
# Про `pane_dead` спрашивается ПЕРВЫМ делом: у панели, чей процесс умер, tmux
# по-прежнему печатает прежнюю команду («claude»), и пинок уходил бы в
# мертвеца, возвращая успех (опыт 13.08.2026, перенесён сюда из сторожа
# сессий — в первой редакции этой двери признака не было, и ревью 17.10.2026
# замерило, что она беднее той копии, которую заменяет).
pane_cmd() {
    local dead
    dead=$(tmux display-message -p -t "${TMUX_SESSION:-}" '#{pane_dead}' 2>/dev/null || echo 0)
    [ "$dead" = 1 ] && { printf 'мертва\n'; return 0; }
    tmux display-message -p -t "${TMUX_SESSION:-}" '#{pane_current_command}' 2>/dev/null || echo "?"
}

# Пинок агенту: только в живую панель с агентом. Причина отказа — в
# PANE_REFUSAL (пусто — дошёл), чтобы звонящий сказал о ней один раз своим
# журналом, а не на каждом круге.
pane_kick() {  # текст
    PANE_REFUSAL=""
    command -v tmux >/dev/null 2>&1 || { PANE_REFUSAL="tmux не установлен"; return 1; }
    tmux has-session -t "${TMUX_SESSION:-}" 2>/dev/null \
        || { PANE_REFUSAL="tmux-сессии '${TMUX_SESSION:-«не задана»}' нет"; return 1; }
    local cmd; cmd=$(pane_cmd)
    case "$cmd" in
        claude|node) send_to_pane "$1" || { PANE_REFUSAL="send-keys не прошёл"; return 1; } ;;
        *) PANE_REFUSAL="в панели '$cmd', не claude/node — оболочка исполнила бы текст как команды"
           return 1 ;;
    esac
}
