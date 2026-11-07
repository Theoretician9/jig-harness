#!/bin/bash
# Уборка процессов стендов проб. Подключается через source, побочных эффектов нет.
# Ожидается: у каждого стенда есть файл $T/owner с pid прогона, а в окружении
# процессов стенда — значение с путём стенда (PATH, PROBA_STAND и т.п.).

# Процессы стенда находятся по окружению: командная строка диспетчера метки не несёт.
sweep_stand() {  # $@ = каталоги стендов со слешем; печатает число убитых
    local f pid n=0 k pat=()
    [ $# -gt 0 ] || { echo 0; return; }
    for f in "$@"; do pat+=(-e "=$f"); done
    # Диспетчер живёт, пока проход до него не дошёл, и порождает потомков уже после
    # снимка /proc: проходы повторяются, пока очередной не убьёт никого.
    while :; do
        k=0
        for f in /proc/[0-9]*/environ; do
            pid="${f#/proc/}"; pid="${pid%/environ}"
            [ "$pid" = "$$" ] && continue
            grep -qaF "${pat[@]}" "$f" 2>/dev/null && kill -KILL "$pid" 2>/dev/null && k=$((k+1))
        done
        n=$((n + k)); [ "$k" -gt 0 ] || break
    done
    echo "$n"
}

# Стенды прогонов, убитых KILL-ом (ловушка не сработала). Живой хозяин — это
# параллельный прогон той же пробы: его стенд не трогаем.
sweep_dead_stands() {  # $1 = префикс каталогов, например /tmp/proba-pane-lock-
    local d owner left dead=()
    for d in "$1"*/; do
        [ -d "$d" ] || continue
        owner=$(cat "$d/owner" 2>/dev/null)
        [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null && continue
        dead+=("$d")
    done
    [ ${#dead[@]} -gt 0 ] || return 0
    left=$(sweep_stand "${dead[@]}")
    if [ "$left" -gt 0 ]; then
        echo "убрано процессов от прерванных прогонов: $left"
    fi
}
