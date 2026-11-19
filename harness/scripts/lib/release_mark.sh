# shellcheck shell=bash
# release_mark.sh — метка «идёт выпуск» для пула тяжёлых прогонов (heavy-slot.sh).
#
# Пока идут ворота publish.sh, пул не выдаёт места новым заявкам, кроме потомков
# выпуска: владелец ждёт выпуск, а свободное место, отданное помощнику, растягивает
# ворота на часы. Идущие прогоны дослуживают — их не гасим.
#
# Метка — файл «<pid> <время старта pid>», путь — HARNESS_RELEASE_MARK (дверь стенда)
# или $LOG_DIR/heavy-slots/release.mark. Выпуск жив, пока жив процесс с тем же pid
# И тем же временем старта: убитый без уборки выпуск оставляет метку, и его pid
# достаётся новому процессу — без времени старта пул стоял бы за чужим процессом.
# Потомки узнаются по HARNESS_RELEASE_PID, который выпуск экспортирует воротам.

# LOG_DIR даёт konf_zagruzit вызывающего; пустой — отказ, а не угаданный путь.
release_mark_path() {
    printf '%s' "${HARNESS_RELEASE_MARK:-${LOG_DIR:?release_mark: LOG_DIR не задан — konf_zagruzit до вызова}/heavy-slots/release.mark}"
}

# Время старта процесса — поле 22 /proc/<pid>/stat; имя команды (поле 2) бывает с пробелами.
release_pid_start() {  # $1 = pid → время старта; код 1 — процесса нет
    local stat fields
    stat=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
    # После «) » идут поля с 3-го: 22-е — девятнадцатое от нуля.
    read -ra fields <<< "${stat##*) }"
    printf '%s' "${fields[19]:-}"
}

release_mark_take() {  # [$1 = pid выпуска, по умолчанию эта оболочка]
    local pid="${1:-$$}" path start
    path=$(release_mark_path)
    start=$(release_pid_start "$pid") || return 1
    mkdir -p "$(dirname "$path")"
    printf '%s %s\n' "$pid" "$start" > "$path.$pid" && mv -f "$path.$pid" "$path"
    export HARNESS_RELEASE_PID="$pid"
}

release_mark_drop() {  # снимает только свою метку: чужой выпуск дослуживает со своей
    local path pid _
    path=$(release_mark_path)
    read -r pid _ 2>/dev/null < "$path" || return 0
    [ "$pid" = "$$" ] && rm -f "$path"
    return 0
}

release_mark_owner() {  # → pid идущего выпуска; код 1 — выпуска нет
    local path pid start
    path=$(release_mark_path)
    read -r pid start 2>/dev/null < "$path" || return 1
    [ -n "$pid" ] && [ "$(release_pid_start "$pid")" = "$start" ] || return 1
    printf '%s' "$pid"
}
