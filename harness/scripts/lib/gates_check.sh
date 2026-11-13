# shellcheck shell=bash
# Обёртка одной проверки ворот (подключается через source из scripts/gates.sh).
# Своим файлом — чтобы пробу можно было вести через ЭТОТ вход на стенде из
# двух-трёх фиктивных проверок, не запуская полные ворота.
#
# Секунды каждой проверки — строкой gate.check в $LOG_DIR/gates-timing.jsonl
# (читалка — scripts/gates-slowest.py). Отдельный журнал, а не поле в
# ворота-итог.jsonl или gates.jsonl: те — одна строка на ПРОГОН, и их читатели
# (панель, сводка событий, evolution-collector) считали бы строку проверки
# прогоном. Ждёт в окружении: LOG_DIR; GATES_TIMING_DIR — каталог журнала
# секунд для самотеста ворот (свой LOG_DIR ему подменять нельзя: от LOG_DIR
# считается путь ворота-итог.jsonl).
#
# Две меры скорости (карточка hr.parallel-cached-checks, спека в .tasks/):
#   • пул: при GATES_JOBS>1 проверки идут фоном, а печатает и считает ТОЛЬКО
#     родитель, в порядке объявления (_gates_flush). Умолчание обёртки — 1:
#     самотест ворот и пробы-стенды читают счётчики сразу после вызова.
#     Пул включает gates_join_or_lead (его зовёт только gates.sh).
#   • кэш: зелёный итог проверки запоминается по хешу её входов (поле `входы:`
#     механизма в mechanisms.yaml) в $LOG_DIR/gates-cache.tsv. Красное и жёлтое
#     не запоминаются, а пишутся строкой-гашением. GATES_NO_CACHE=1 — не читать.
# После последней проверки gates.sh зовёт gates_wait_all: до него счёт неполон.

# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/events.sh"

GATES_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Метка прогона: читалка отделяет по ней последний прогон от прежних. PID —
# в метке, потому что два прогона в одну секунду иначе слились бы в один.
GATES_RUN="$(date -Is) $$"

GREEN=0
RED=0
YELLOW=0

# Третье состояние гейта — ЖЁЛТОЕ: находки есть, но гейт ещё обкатывается и
# коммит не валит. Носитель состояния — код возврата 77, а не догадка о тексте:
# без него обкатываемый гейт обязан был выходить нулём, попадал в зелёные, и
# его находки срезались до последних 90 знаков последней строки — владелец
# видел «все зелёные» при живых нарушениях (находка ревью I-2).
YELLOW_CODE=77

# Состояние пула и кэша. Заполняется ленивым _gates_init при первой проверке.
declare -A GATES_INPUTS=()      # файл механизма → хеш входов | NOINPUTS | MISSING <шаблон>
declare -A GATES_CACHED=()      # ключ → код ПОСЛЕДНЕЙ строки кэша
declare -A GATES_CACHE_LAST=()  # ключ → последняя строка вывода из кэша
declare -A GATES_SERIAL=()      # имя проверки → группа последовательных
declare -A GATES_PIDS=()        # номер проверки → pid фоновой работы
GATES_INIT_DONE=""
GATES_OUT_DIR=""                # база блоков вывода; живёт, пока есть непропечатанные
GATES_N=0                       # сколько проверок объявлено в текущей базе
GATES_NEXT_PRINT=1              # номер проверки, чей блок печатать следующим
GATES_STAT_HIT=0
GATES_STAT_RUN=0
GATES_STAT_NONE=0
GATES_KEY=""                    # результат _gates_cache_key
GATES_NOTE=""

# ── ленивая инициализация ────────────────────────────────────────────────────

_gates_helper() {
    local p
    for p in "$GATES_LIB_DIR/../gates_cache.py" "$GATES_ROOT/scripts/gates_cache.py"; do
        [ -f "$p" ] && { printf '%s' "$p"; return; }
    done
}

_gates_load_inputs() {
    local py=$1 f v
    [ -f "$GATES_MECHANISMS" ] && [ -n "$py" ] || return 0
    while IFS=$'\t' read -r f v; do
        GATES_INPUTS[$f]=$v
    done < <(python3 -I "$py" inputs --mechanisms "$GATES_MECHANISMS" --root "$GATES_ROOT")
}

_gates_load_serial() {
    local py=$1 c g
    [ -f "$GATES_SERIAL_FILE" ] && [ -n "$py" ] || return 0
    while IFS=$'\t' read -r c g; do
        GATES_SERIAL[$c]=$g
    done < <(python3 -I "$py" serial --file "$GATES_SERIAL_FILE")
}

# Кэш читается всегда, кроме GATES_NO_CACHE=1: ночной прогон без кэша ничего не
# берёт из него, но дописывает свежее — его свежий зелёный и есть сверка.
_gates_load_cache() {
    local py=$1 k t n rc last
    [ -z "${GATES_NO_CACHE:-}" ] && [ -f "$GATES_CACHE_FILE" ] || return 0
    if [ -n "$py" ] && [ "$(wc -l < "$GATES_CACHE_FILE")" -gt 20000 ]; then
        python3 -I "$py" compact "$GATES_CACHE_FILE"
    fi
    while IFS=$'\t' read -r k t n rc last; do
        [ -n "$k" ] || continue
        GATES_CACHED[$k]=$rc
        GATES_CACHE_LAST[$k]=$last
    done < "$GATES_CACHE_FILE"
}

_gates_init() {
    [ -n "$GATES_INIT_DONE" ] && return 0
    GATES_INIT_DONE=1
    GATES_ROOT="${GATES_ROOT:-${ROOT:-$PWD}}"
    GATES_MECHANISMS="${GATES_MECHANISMS:-$GATES_ROOT/harness/config/mechanisms.yaml}"
    GATES_SERIAL_FILE="${GATES_SERIAL_FILE:-$GATES_ROOT/harness/config/gates-serial.yaml}"
    GATES_CACHE_FILE="${GATES_CACHE_FILE:-$LOG_DIR/gates-cache.tsv}"
    local py; py=$(_gates_helper)
    _gates_load_inputs "$py"
    _gates_load_serial "$py"
    _gates_load_cache "$py"
}

# ── ключ кэша ────────────────────────────────────────────────────────────────

# Механизм проверки — тот, чей `файл` равен первому доводу команды. Нет такого
# (bash -c '…') — входы не описаны, и проверка не кэшируется.
_gates_cache_key() {  # имя, команда...
    local name=$1 arg digest=""; shift
    GATES_KEY=""
    GATES_NOTE=" · не кэш: входы не описаны"
    for arg in "$@"; do
        [ -n "$arg" ] && [ -n "${GATES_INPUTS[$arg]+x}" ] || continue
        digest=${GATES_INPUTS[$arg]}
        break
    done
    case "$digest" in
        ""|NOINPUTS) return ;;
        MISSING\ *) GATES_NOTE=" · не кэш: вход не найден ${digest#MISSING }"; return ;;
    esac
    GATES_NOTE=""
    GATES_KEY=$({ printf 'v1\0%s\0' "$name"; printf '%s\0' "$@"; printf '%s' "$digest"; } \
        | sha256sum | cut -d' ' -f1)
}

# Дописывание — под замком: параллельные работы пишут в один файл. Табы в
# последней строке заменены пробелом, иначе строка развалилась бы на поля.
_gates_cache_append() {  # ключ, имя, код, последняя строка
    local last=${4//$'\t'/ }
    {
        flock 9
        printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$(date +%s)" "$2" "$3" "$last" >> "$GATES_CACHE_FILE"
    } 9> "$GATES_CACHE_FILE.lock"
}

# ── работа одной проверки: вердикт → файлы блока ─────────────────────────────

_gates_check_limit() {  # имя → секунды (0 — без предела)
    local pair pairs
    IFS='|' read -ra pairs <<< "${GATES_CHECK_TIMEOUT_LONG:-}"
    for pair in "${pairs[@]}"; do
        [ "${pair%%=*}" = "$1" ] && { printf '%s' "${pair#*=}"; return; }
    done
    printf '%s' "${GATES_CHECK_TIMEOUT_SEC:-3600}"
}

# Гейт, глухой к TERM, timeout добивает KILL через 5 с и выходит 137, а не 124;
# 137 до предела — гейт убит не нами, строка о пределе была бы ложью.
_gates_bounded() {  # предел, команда...
    local limit=$1 rc started=$SECONDS; shift
    if [ "$limit" -le 0 ]; then "$@"; return; fi
    timeout -k 5 "$limit" "$@"; rc=$?
    if [ "$rc" -eq 124 ] || { [ "$rc" -eq 137 ] && [ $((SECONDS - started)) -ge "$limit" ]; }; then
        printf 'превысил предел %s с — дерево процессов убито\n' "$limit"
    fi
    return "$rc"
}

# Команда идёт без дескриптора замка группы (8): иначе демон, поднятый пробой,
# держал бы замок группы дольше самой проверки.
_gates_run_command() {  # группа, предел, команда...
    local group=$1 limit=$2; shift 2
    if [ -z "$group" ]; then
        _gates_bounded "$limit" "$@" 2>&1
        return
    fi
    exec 8> "$GATES_OUT_DIR/group-$group.lock"
    flock 8
    _gates_bounded "$limit" "$@" 2>&1 8>&-
}

_gates_journal() {  # имя, исход, секунды(мкс), код, из кэша
    local cached=""
    [ "$5" = hit ] && cached=',"cached":true'
    HARNESS_LOG_DIR="${GATES_TIMING_DIR:-$LOG_DIR}" harness_event gates-timing scripts/gates.sh gate.check "$2" \
        name="$1" run="$GATES_RUN" \
        --json "$(printf '{"secs":%d.%03d,"rc":%d%s}' $(($3 / 1000000)) $(($3 / 1000 % 1000)) "$4" "$cached")"
}

# Файл rc кладётся ПОСЛЕДНИМ и переименованием: по нему родитель решает, что блок готов.
_gates_job() {  # каталог блока, имя, группа, команда...
    local dir=$1 name=$2 group=$3; shift 3
    local kind=none out="" rc us=0 last started outcome=ok
    _gates_cache_key "$name" "$@"
    local key=$GATES_KEY note=$GATES_NOTE
    if [ -n "$key" ] && [ "${GATES_CACHED[$key]:-x}" = 0 ]; then
        kind=hit; rc=0; last=${GATES_CACHE_LAST[$key]}; note=" · из кэша"
    else
        [ -n "$key" ] && kind=run
        # Микросекунды целым числом: EPOCHREALTIME без точки, арифметика bash
        # дробей не знает, а внешний `date` на сотнях проверок — лишние процессы.
        started=${EPOCHREALTIME/./}
        out=$(_gates_run_command "$group" "$(_gates_check_limit "$name")" "$@"); rc=$?
        us=$(( ${EPOCHREALTIME/./} - started ))
        last=$(printf '%s' "$out" | tail -1)
        [ -n "$key" ] && _gates_cache_append "$key" "$name" "$rc" "$last"
    fi
    [ "$rc" -eq 0 ] || [ "$rc" -eq "$YELLOW_CODE" ] || outcome=refused
    printf '%s\n' "$out" > "$dir/out"
    printf '%s' "$last" > "$dir/last"
    printf '%s' "$note" > "$dir/note"
    printf '%s' "$kind" > "$dir/kind"
    printf '%s' "$name" > "$dir/name"
    _gates_journal "$name" "$outcome" "$us" "$rc" "$kind"
    printf '%s' "$rc" > "$dir/rc.tmp" && mv "$dir/rc.tmp" "$dir/rc"
}

# ── печать и счёт: только в родителе, по порядку объявления ──────────────────

_gates_print_block() {  # каталог блока
    local dir=$1 name rc last note
    name=$(< "$dir/name"); rc=$(< "$dir/rc"); last=$(< "$dir/last"); note=$(< "$dir/note")
    case "$(< "$dir/kind")" in
        hit) GATES_STAT_HIT=$((GATES_STAT_HIT + 1)) ;;
        run) GATES_STAT_RUN=$((GATES_STAT_RUN + 1)) ;;
        *) GATES_STAT_NONE=$((GATES_STAT_NONE + 1)) ;;
    esac
    if [ "$rc" -eq "$YELLOW_CODE" ]; then
        YELLOW=$((YELLOW + 1))
        printf '  жёлтый · %s (обкатка, коммит не валит)\n' "$name"
        sed 's/^/       /' "$dir/out"
    elif [ "$rc" -eq 0 ]; then
        GREEN=$((GREEN + 1))
        # Резка ПОДСТРОКОЙ, а не `cut -c`: cut считает байты и рвёт кириллицу
        # посреди буквы (тот же больной случай, что в tg_send.sh 08.08.2026).
        # Выравнивания колонок нет по той же причине — printf %-22s тоже
        # байтовый, и русские имена разъезжали бы вместо ровного столбца.
        printf '  ок · %s — %s%s\n' "$name" "${last:0:90}" "$note"
    else
        RED=$((RED + 1))
        printf '  ПЛОХО · %s (код %d)\n' "$name" "$rc"
        sed 's/^/       /' "$dir/out"
    fi
}

# Работа, умершая до файла rc (убита, кончилось место), при барьере считается
# красной: молча пропавшая проверка была бы зелёной «ни о чём».
_gates_orphan_block() {  # каталог блока
    local dir=$1
    [ -f "$dir/name" ] || printf 'проверка №%s' "${dir##*/}" > "$dir/name"
    printf 'работа проверки оборвалась до вердикта\n' > "$dir/out"
    : > "$dir/last"; : > "$dir/note"; printf none > "$dir/kind"; printf 255 > "$dir/rc"
}

# Когда напечатано всё объявленное, база сбрасывается: следующая серия проверок
# начнёт с чистого каталога, а синхронный режим не копит каталоги.
_gates_reset_base() {
    [ "$GATES_NEXT_PRINT" -gt "$GATES_N" ] || return 0
    [ -n "$GATES_OUT_DIR" ] && rm -rf "$GATES_OUT_DIR"
    GATES_OUT_DIR=""; GATES_N=0; GATES_NEXT_PRINT=1; GATES_PIDS=()
}

_gates_flush() {  # [force]
    local dir
    while [ "$GATES_NEXT_PRINT" -le "$GATES_N" ]; do
        dir="$GATES_OUT_DIR/$GATES_NEXT_PRINT"
        if [ ! -f "$dir/rc" ]; then
            [ "${1:-}" = force ] || break
            _gates_orphan_block "$dir"
        fi
        _gates_print_block "$dir"
        GATES_NEXT_PRINT=$((GATES_NEXT_PRINT + 1))
    done
    _gates_reset_base
}

# Барьер: ждёт все фоновые работы по их pid (не голый `wait`: у ведущего прогона
# есть процесс tee, который кончается только вместе с прогоном) и допечатывает.
# `wait` — без перенаправления: сигнал посреди него оставил бы сохранённую копию
# канала tee открытой, и ловушка выхода ведущего ждала бы tee до предела.
gates_wait_all() {
    local n
    for n in "${!GATES_PIDS[@]}"; do
        wait "${GATES_PIDS[$n]}" || true
    done
    _gates_flush force
    if [ $((GATES_STAT_HIT + GATES_STAT_RUN + GATES_STAT_NONE)) -gt 0 ]; then
        printf -- '--- кэш: из кэша %d, прогнано %d, не кэшируются %d ---\n' \
            "$GATES_STAT_HIT" "$GATES_STAT_RUN" "$GATES_STAT_NONE"
        GATES_STAT_HIT=0; GATES_STAT_RUN=0; GATES_STAT_NONE=0
    fi
}

# ── вход ─────────────────────────────────────────────────────────────────────

_gates_new_block() {  # → номер в GATES_N, каталог создан
    [ -n "$GATES_OUT_DIR" ] || GATES_OUT_DIR=$(mktemp -d -t harness-run-gates-XXXXXX)
    GATES_N=$((GATES_N + 1))
    mkdir -p "$GATES_OUT_DIR/$GATES_N"
}

_gates_throttle() {
    local -a running
    while running=($(jobs -rp)) && [ "${#running[@]}" -ge "${GATES_JOBS:-1}" ]; do
        wait -n || true
    done
}

_gates_launch() {  # имя, группа, команда...
    local name=$1 group=$2; shift 2
    _gates_throttle
    _gates_new_block
    ( _gates_job "$GATES_OUT_DIR/$GATES_N" "$name" "$group" "$@" ) &
    GATES_PIDS[$GATES_N]=$!
    _gates_flush
}

_gates_run_inline() {  # имя, группа, команда...
    local name=$1 group=$2; shift 2
    _gates_new_block
    _gates_job "$GATES_OUT_DIR/$GATES_N" "$name" "$group" "$@"
    _gates_flush
}

проверка() {  # имя, команда...
    local name="$1"; shift
    # Выборка (gates.sh --только): проверка вне списка не идёт и не считается.
    case "|${ONLY_CHECKS:-}|" in "||"|*"|$name|"*) ;; *) return ;; esac
    _gates_init
    # Гейт без команды — самый опасный: он всегда зелёный и всегда молчит.
    # 11.09.2026 правка ворот текстом разорвала строку «указания», команда
    # уехала в хвост соседней, и гейт полсмены отчитывался «ок» ни о чём.
    if [ "$#" -eq 0 ]; then
        gates_wait_all
        RED=$((RED + 1))
        printf '  ПЛОХО · %s — гейт объявлен БЕЗ КОМАНДЫ: он зелёный всегда\n' "$name"
        return
    fi
    local group=${GATES_SERIAL[$name]:-}
    if [ "${GATES_JOBS:-1}" -le 1 ]; then
        _gates_run_inline "$name" "" "$@"
    elif [ "$group" = "одна" ]; then
        # Особая группа: проверка идёт ОДНА — пул ждёт её до и после.
        gates_wait_all
        _gates_run_inline "$name" "" "$@"
    else
        _gates_launch "$name" "$group" "$@"
    fi
}

# ── замок прогона: второй на том же состоянии дерева присоединяется к первому ──

# Ворота судят рабочее дерево, а не один HEAD: ключ — HEAD, правки, неотслеживаемые
# файлы и доводы. Git идёт без GIT_INDEX_FILE: личный индекс помощника иначе менял
# бы ключ у того же дерева.
_gates_tree_key() {  # доводы...
    {
        env -u GIT_INDEX_FILE git -C "$GATES_ROOT" rev-parse HEAD
        env -u GIT_INDEX_FILE git -C "$GATES_ROOT" diff HEAD --binary
        env -u GIT_INDEX_FILE git -C "$GATES_ROOT" ls-files -o --exclude-standard -z \
            | (cd "$GATES_ROOT" && xargs -0 -r sha256sum)
        printf '%s\0' "$@"
        printf '%s' "${ONLY_CHECKS:-}"
    } 2>/dev/null | sha256sum | cut -d' ' -f1
}

# Сигнал одному ведущему (kill <pid>), а не всей группе, как у timeout и Ctrl-C,
# работ пула не трогает: они доживали бы сиротами и держали бы канал tee открытым.
# Дерево — потому что у работы есть внуки (подстановка $(…) и сама команда).
# Родитель гасится раньше детей: иначе работа успела бы записать вердикт убитой команды.
_gates_kill_tree() {  # pid
    local children child
    children=$(pgrep -P "$1")
    kill -TERM "$1" 2>/dev/null
    for child in $children; do _gates_kill_tree "$child"; done
}

# Все дети ведущего, кроме tee, а не только `jobs -rp`: синхронная проверка идёт в
# подстановке $(…) и в `jobs` её нет.
_gates_stop_pool() {
    local pid
    for pid in $(pgrep -P "$$"); do
        [ "$pid" = "$GATES_TEE_PID" ] || _gates_kill_tree "$pid"
    done
}

# Закрывает вывод ведущего, ждёт tee (тот дописывает файл) и кладёт код выхода.
# Ждать tee по таймеру, а не `wait`: демон, унаследовавший канал, держал бы tee вечно.
_gates_lead_finish() {  # код выхода
    local rc=$1 i state
    trap '' USR1
    _gates_stop_pool
    exec >&- 2>&-
    for ((i = 0; i < 100; i++)); do
        state=$(awk '{print $3}' "/proc/$GATES_TEE_PID/stat" 2>/dev/null)
        [ -z "$state" ] || [ "$state" = Z ] && break
        sleep 0.1
    done
    printf '%s' "$rc" > "$GATES_JOIN_BASE.rc.tmp" && mv "$GATES_JOIN_BASE.rc.tmp" "$GATES_JOIN_BASE.rc"
}

_gates_overrun() {
    printf '=== ворота превысили общий предел %s с — пул погашен, итог красный ===\n' "$GATES_RUN_LIMIT"
    # Итог — тем же журналом, что и у обычного конца: без строки панель показала бы прежний прогон.
    if [ -n "${ITOG_LOG:-}" ]; then
        printf '{"ts":"%s","режим":"%s","зелёных":%s,"красных":%s,"предел":true}\n' \
            "$(date -Is)" "${REZHIM:-}" "${GREEN:-0}" "$(( ${RED:-0} > 0 ? RED : 1 ))" >> "$ITOG_LOG" 2>/dev/null || true
    fi
    exit 124
}

# Сторож только шлёт USR1: bash исполняет ловушку и посреди чтения $(…), а детей
# гасит ловушка выхода. Ведущий, убитый KILL, ловушки не исполнит, и сторож
# становится сиротой: через предел его PID может принадлежать чужому процессу,
# которого USR1 по умолчанию убьёт, — поэтому сигнал только своему родителю.
_gates_arm_watchdog() {
    GATES_RUN_LIMIT=${GATES_RUN_TIMEOUT_SEC:-5400}
    if [[ ! "$GATES_RUN_LIMIT" =~ ^[0-9]+$ ]]; then
        # Нечисловой предел не выключает сторожа молча: ворота без предела виснут на часы.
        printf 'предел GATES_RUN_TIMEOUT_SEC не число: %s — взят предел по умолчанию 5400 с\n' "$GATES_RUN_LIMIT"
        GATES_RUN_LIMIT=5400
    fi
    [ "$GATES_RUN_LIMIT" -gt 0 ] || return 0
    trap _gates_overrun USR1
    ( sleep "$GATES_RUN_LIMIT"
      read -r _ _ _ parent _ < "/proc/$BASHPID/stat"
      [ "$parent" = "$$" ] && kill -USR1 "$$" ) > /dev/null 2>&1 &
    GATES_WATCHDOG_PID=$!
    disown "$GATES_WATCHDOG_PID"
}

# Ловушка СЦЕПЛЯЕТСЯ с уже стоящей (gates.sh ставит уборку каталога прогона):
# обычный `trap … EXIT` затёр бы её.
_gates_lead_start() {
    local prev
    prev=$(trap -p EXIT)
    export GATES_LEAD_PID=$$ GATES_LEAD_ROOT=$GATES_ROOT
    exec > >(tee "$GATES_JOIN_BASE.out") 2>&1
    GATES_TEE_PID=$!
    _gates_arm_watchdog
    eval "set -- ${prev:-trap -- : EXIT}"
    trap '_gates_lead_finish "$?"; '"$3" EXIT
}

# Выбор под коротким замком: есть живой ведущий без итога — присоединиться;
# иначе записать себя ведущим и убрать следы прежнего прогона.
_gates_join_decide() {  # → 0 вести, 1 присоединиться
    local leader
    exec 9> "$GATES_JOIN_DIR/gates-run.lock"
    flock 9
    leader=$(cat "$GATES_JOIN_BASE.pid" 2>/dev/null)
    if [ -n "$leader" ] && kill -0 "$leader" 2>/dev/null && [ ! -f "$GATES_JOIN_BASE.rc" ]; then
        GATES_LEADER=$leader
        exec 9>&-
        return 1
    fi
    printf '%s' "$$" > "$GATES_JOIN_BASE.pid"
    rm -f "$GATES_JOIN_BASE.out" "$GATES_JOIN_BASE.rc"
    exec 9>&-
    return 0
}

# Опрос раз в секунду, не flock: замок, унаследованный фоновыми детьми проверок,
# держался бы дольше прогона. 0 — итог получен и напечатан (выходить), 1 — ведущий оборвался.
_gates_join_wait() {
    printf 'присоединяюсь к прогону %s (то же состояние дерева)\n' "$GATES_LEADER"
    while [ ! -f "$GATES_JOIN_BASE.rc" ]; do
        if ! kill -0 "$GATES_LEADER" 2>/dev/null && [ ! -f "$GATES_JOIN_BASE.rc" ]; then
            echo "ведущий прогон оборвался без итога — веду сам"
            return 1
        fi
        sleep 1
    done
    cat "$GATES_JOIN_BASE.out"
    GATES_JOIN_RC=$(< "$GATES_JOIN_BASE.rc")
    return 0
}

gates_join_or_lead() {  # доводы...
    GATES_ROOT="${GATES_ROOT:-${ROOT:-$PWD}}"
    if [ -n "${GATES_LEAD_PID:-}" ] && kill -0 "$GATES_LEAD_PID" 2>/dev/null; then
        if [ -n "${GATES_LEAD_ROOT:-}" ] && [ "$(readlink -f "$GATES_LEAD_ROOT")" = "$(readlink -f "$GATES_ROOT")" ]; then
            printf 'уже внутри ворот (ведущий %s, корень %s) — вложенный прогон не запускается\n' "$GATES_LEAD_PID" "$GATES_ROOT"
            exit 75
        fi
        GATES_JOBS=1
        return 0
    fi
    unset GATES_LEAD_PID GATES_LEAD_ROOT
    if [ -z "${GATES_JOBS:-}" ]; then
        GATES_JOBS=$(( $(nproc) - 2 ))
        [ "$GATES_JOBS" -ge 1 ] || GATES_JOBS=1
    fi
    GATES_JOIN_DIR="${GATES_JOIN_DIR:-$LOG_DIR}"
    GATES_JOIN_BASE="$GATES_JOIN_DIR/gates-run-$(_gates_tree_key "$@")"
    mkdir -p "$GATES_JOIN_DIR"
    while ! _gates_join_decide; do
        if _gates_join_wait; then
            exit "$GATES_JOIN_RC"
        fi
    done
    _gates_lead_start
}
