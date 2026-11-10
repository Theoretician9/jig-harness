# shellcheck shell=bash
# Вывод панели агента — в файл, чтобы выход агента сразу после подъёма имел
# причину, а не только факт: без записи об этом нечего было сказать владельцу.
# Подключается через source start-agent.sh и session-warden.sh.
#
# Файл *.log лежит под правилом /var/log/harness/*.log из logrotate-harness
# (size 5M, copytruncate): отдельной ротации не нужно, а `cat >>` пишет в тот же
# inode и после усечения.

AGENT_PANE_LOG_NAME="agent-pane.log"
AGENT_STARTED_NAME="agent-started.at"

# $1 = сессия, $2 = каталог журналов. Идемпотентна: `pipe-pane -o` в tmux 3.4
# при открытой трубе её ЗАКРЫВАЕТ, поэтому труба открывается только если
# #{pane_pipe} = 0. Файл создаётся заранее с правами 0600: в нём весь вывод
# панели, включая вход в аккаунт. 8>&- — лок сторожа tmux-серверу не отдаётся.
pane_log_attach() {
    local file="$2/$AGENT_PANE_LOG_NAME"
    mkdir -p "$2" 2>/dev/null || return 0
    [ -e "$file" ] || (umask 077; : > "$file") 2>/dev/null
    chmod 600 "$file" 2>/dev/null || true
    [ "$(tmux display-message -p -t "$1" '#{pane_pipe}' 2>/dev/null)" = 0 ] || return 0
    tmux pipe-pane -t "$1" "cat >> '$file'" 8>&- 2>/dev/null || true
}

# Журнал не растёт без предела: logrotate приходит раз в сутки, а панель с
# перерисовками пишет быстрее. Сверх предела (КБ) остаётся последний мегабайт;
# труба пишет в режиме дозаписи, поэтому усечение на месте ей не мешает.
pane_log_trim() {  # $1 = каталог журналов, $2 = предел, КБ
    local file="$1/$AGENT_PANE_LOG_NAME" keep
    [ -f "$file" ] || return 0
    [ "$(( $(stat -c '%s' "$file") / 1024 ))" -gt "$2" ] || return 0
    keep=$(mktemp "$1/.agent-pane-trim.XXXXXX") || return 0
    chmod 600 "$keep"
    tail -c 1048576 "$file" > "$keep" && cat "$keep" > "$file"
    rm -f "$keep"
}

# Метка «команду запуска агента ввели» — от неё считается «вышел рано».
agent_started_mark() {  # $1 = каталог журналов
    date +%s > "$1/$AGENT_STARTED_NAME" 2>/dev/null || true
}

# Хвост панели для владельца: без управляющих последовательностей, длинные
# токены и присвоения секретов закрыты. Общей маски для текста в харнесе нет;
# длинные слитные строки (токены, коды входа, ссылки) режутся целиком —
# лишнее скрыть дешевле, чем показать.
pane_log_tail() {  # $1 = каталог журналов, $2 = строк
    local file="$1/$AGENT_PANE_LOG_NAME"
    [ -s "$file" ] || return 0
    tail -c 20000 "$file" \
        | sed -E 's/\x1b\[[0-9;?]*[a-zA-Z]//g; s/\x1b\][^\x07]*\x07//g; s/\r/\n/g' \
        | tr -d '\000-\010\013\014\016-\037' \
        | grep -v '^[[:space:]]*$' \
        | sed -E 's/((token|secret|password|passwd|api[_-]?key|key)[A-Za-z_]*[=:][[:space:]]*)[^[:space:]]+/\1[скрыто]/Ig; s/[A-Za-z0-9_.\/+=-]{32,}/[скрыто]/g' \
        | cut -c1-200 | tail -n "$2"
}
