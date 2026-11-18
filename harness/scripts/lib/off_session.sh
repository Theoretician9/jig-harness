#!/bin/bash
# Проба вне сессии. Подключается через source, побочных эффектов нет.
#
# Писатель тревог (alert-task.py, claude_ancestors) отказывает зову, у которого
# среди предков есть процесс claude. Пробу, которая ведёт настоящего писателя,
# зовут и из-под сессии: ворота под merge-queue, запуск фоном, когда тот же
# вызов ждёт журнал. Тогда проба судила бы отказ «зов из сессии», а не свой
# случай. Проба сама создаёт условие: уходит сиротой в новую сессию и ждёт итог.

claude_above() {  # 0 — среди предков текущей оболочки есть процесс claude
    local pid=$PPID name
    while [ "${pid:-0}" -gt 1 ]; do
        name=$(sed -n 's/^Name:\t//p' "/proc/$pid/status" 2>/dev/null)
        [ "$name" = claude ] && return 0
        pid=$(sed -n 's/^PPid:\t//p' "/proc/$pid/status" 2>/dev/null)
    done
    return 1
}

# off_session_reexec <команда пробы…>: без предка claude — возврат, проба идёт
# как есть. Иначе команда уходит через setsid -f (его родитель выходит сразу,
# сирота достаётся init), её вывод идёт сюда по трубе, а код — последней
# строкой; здесь выход с этим кодом. PROBE_OFF_SESSION не даёт уйти дважды.
off_session_reexec() {
    [ -z "${PROBE_OFF_SESSION:-}" ] && claude_above || return 0
    local box line rc=1
    box=$(mktemp -d)
    # Ворота, гасящие пробу по сроку, гасят и её сироту: своя группа процессов.
    trap 'kill -TERM -- "-$(cat "'"$box"'/pid" 2>/dev/null)" 2>/dev/null; rm -rf "'"$box"'"' EXIT
    trap 'exit 143' TERM INT HUP
    while IFS= read -r line; do
        case "$line" in
            "PROBE_OFF_SESSION_RC="*) rc=${line#PROBE_OFF_SESSION_RC=} ;;
            *) printf '%s\n' "$line" ;;
        esac
    done < <(PROBE_OFF_SESSION=1 setsid -f bash -c \
        'echo $$ > "$1/pid"; shift; "$@" < /dev/null 2>&1; echo "PROBE_OFF_SESSION_RC=$?"' \
        _ "$box" "$@" 2>&1)
    rm -f "$box/pid"
    exit "$rc"
}
