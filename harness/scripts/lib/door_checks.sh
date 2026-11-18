# shellcheck shell=bash
# door_checks.sh — тестовый этап двери выката: наборы ветки идут в одноразовых
# контейнерах без сети, с потолками памяти, времени и процессов; итог ложится
# запиской deploy.checks в журнал deploy-checks.
#
#   source scripts/lib/door_checks.sh
#   door_checks_run    <sha> <ветка> [<образ>:<sha12>]   прогнать наборы, записать записку
#   door_checks_cached <sha> <ветка> [<образ>:<sha12>]   записка success есть — не гонять
#
# Зависимости: функция say (ставит зовущий), git -C "$PRODUCT_REPO", соседние
# server_checks.py и events.sh. Ключи: PROJECT_NAME PRODUCT_REPO LOG_DIR
# STACK_CHECKS_FILE STACK_CHECKS_ONLY STACK_CHECK_MIN_FREE_MB STACK_CHECK_UID
# STACK_RUN_AS STACK_STAND_IMAGES («pg=<образ> redis=<образ>») STACK_STAND_ENV_PREFIX.
#
# Записка tests=success — единственное, чему верит выкат на прод, поэтому «не
# отработало» (установка, таймаут, память, отказ docker) всегда error, а не failure:
# по такому прогону о коде судить нельзя. Контейнеры, сети и образы стенда — только
# по именам с префиксом этой установки.
#
# Два вида набора. С полем image (артефакт этапа B или готовый образ): тесты идут
# на том образе, что едет, сети нет с самого run, установки нет. Без поля: ОДИН
# контейнер образа base — старт с сетью, установка через exec, затем отключение
# сети, затем шаги через exec; ни commit, ни build, ни кэша образа.

DCHK_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
DOOR_CHECKS_PY="${HARNESS_SERVER_CHECKS_PY:-$DCHK_DIR/server_checks.py}"
# Потолок процессов контейнера набора: форк-бомба кода ветки не должна съесть сервер.
DOOR_CHECKS_PIDS_LIMIT=512

# shellcheck disable=SC1091
source "$DCHK_DIR/events.sh"

DCHK_ID=""
DCHK_PREFIX=""
DCHK_UID=""
DCHK_COPY=""
DCHK_WORK_ROOT=""
DCHK_LOG_ROOT=""
DCHK_CONT=""          # контейнер набора, пока он жив
DCHK_STAND_NET=""     # имя сети стенда, пока он поднят; пусто — стенда нет
DCHK_MEM_NOW=0
DCHK_MEM_MIN=""
DCHK_LOAD_MAX=0
DCHK_REASON=""
DCHK_RESULT=error
DCHK_PASSED=0
DCHK_SKIPPED=0
DCHK_RESULTS="[]"
DCHK_TESTS=""

# Префикс — граница между установками на одном docker: уборка сирот и всё прочее
# судят по нему одному.
dchk_prefix() { printf '%s-check-' "$PROJECT_NAME"; }

# root в любой записи: имя, «0», «00» — и в uid, и в gid (1000:0 даёт группу root).
dchk_id_is_root() { [ "$1" = root ] || { [[ $1 =~ ^[0-9]+$ ]] && [ $((10#$1)) = 0 ]; }; }

# Тесты идут не от root: пользователь контейнера — из ключа, пусто — пользователь
# STACK_RUN_AS. Отказ ДО первого вызова docker, словами с именами ключей.
dchk_uid_resolve() {
    local user=${STACK_RUN_AS:-} uid
    if [ -n "${STACK_CHECK_UID:-}" ]; then
        if dchk_id_is_root "${STACK_CHECK_UID%%:*}" || dchk_id_is_root "${STACK_CHECK_UID##*:}"; then
            say "ОШИБКА: проверки: STACK_CHECK_UID «$STACK_CHECK_UID» — uid или gid 0 это root; тесты ветки не идут от него; отказ до docker"
            return 1
        fi
        DCHK_UID=$STACK_CHECK_UID
        return 0
    fi
    if [ -z "$user" ]; then
        say "ОШИБКА: проверки: не заданы STACK_CHECK_UID и STACK_RUN_AS — тесты пошли бы от root контейнера; отказ до docker"
        return 1
    fi
    uid=$(id -u "$user" 2>/dev/null) \
        || { say "ОШИБКА: проверки: STACK_RUN_AS «$user» — такого пользователя нет; задай STACK_CHECK_UID или пользователя; отказ до docker"; return 1; }
    if [ "$uid" = 0 ] || [ "$user" = root ] || [ "$user" = "$(id -un)" ]; then
        say "ОШИБКА: проверки: STACK_RUN_AS «$user» — root или пользователь агента, STACK_CHECK_UID пуст; тесты ветки не должны идти от него; отказ до docker"
        return 1
    fi
    DCHK_UID="$uid:$(id -g "$user")"
}

dchk_keys_ok() {
    local key missing=""
    for key in PROJECT_NAME PRODUCT_REPO LOG_DIR STACK_CHECKS_FILE STACK_CHECK_MIN_FREE_MB; do
        [ -n "${!key:-}" ] || missing="$missing $key"
    done
    [ -z "$missing" ] || { say "ОШИБКА: проверки: не заданы ключи:$missing — отказ до docker"; return 1; }
    dchk_uid_resolve
}

dchk_cleanup() {
    dchk_cleanup_suite
    [ -z "$DCHK_COPY" ] || rm -rf "$DCHK_COPY"
    DCHK_COPY=""
}

# Стенд pg-redis: сеть --internal не пускает код ветки наружу, потолки памяти нужны,
# потому что сервер переподписан. Убирается по точным именам с меткой прогона.
# Алиас redis зовётся stand-kv: предохранитель тестов продукта считает боевым любой
# адрес с подстрокой «redis».
dchk_stand_down() {
    [ -n "$DCHK_STAND_NET" ] || return 0
    docker rm -f "$DCHK_STAND_NET-pg" "$DCHK_STAND_NET-redis" >/dev/null 2>&1 || true
    docker network rm "$DCHK_STAND_NET" >/dev/null 2>&1 || true
    DCHK_STAND_NET=""
}

# kill -9 двери оставляет стенд: trap не срабатывает. Под checks.lock этой установки
# чужого живого стенда с её префиксом быть не может, поэтому всё с префиксом, кроме
# своего, осиротело; стенд другой установки несёт другой префикс. Имена без
# -pg/-redis (контейнеры наборов) не трогаем.
dchk_sweep_orphans() {
    local prefix=$DCHK_PREFIX name
    while IFS= read -r name; do
        case "$name" in
            "$prefix"*-pg|"$prefix"*-redis) ;;
            *) continue ;;
        esac
        case "$name" in "$prefix$DCHK_ID-"*) continue ;; esac
        docker rm -f "$name" >/dev/null 2>&1 </dev/null 6>&- && say "убран осиротевший стенд: $name"
    done < <(docker ps -a --format '{{.Names}}' </dev/null 6>&- 2>/dev/null)
    while IFS= read -r name; do
        case "$name" in "$prefix"*) ;; *) continue ;; esac
        [ "$name" != "$prefix$DCHK_ID" ] || continue
        docker network rm "$name" >/dev/null 2>&1 </dev/null 6>&- && say "убран осиротевший стенд: $name"
    done < <(docker network ls --format '{{.Name}}' </dev/null 6>&- 2>/dev/null)
    return 0
}

# Образ службы стенда из STACK_STAND_IMAGES («pg=<образ> redis=<образ>»); нет — отказ.
dchk_stand_image() {  # $1 = pg|redis
    local word
    for word in ${STACK_STAND_IMAGES:-}; do
        case "$word" in "$1="?*) printf '%s' "${word#*=}"; return 0 ;; esac
    done
    return 1
}

# Поднять стенд; при отказе — DCHK_REASON «стенд не поднялся: …», уборка сделана.
# Срок готовности Postgres: DEPLOY_CHECK_STAND_WAIT_SEC (штатно 120 с).
dchk_stand_up() {  # $1 = лог
    local net="$DCHK_PREFIX$DCHK_ID" deadline pg_image redis_image
    pg_image=$(dchk_stand_image pg) || { DCHK_REASON="стенд не поднялся: в STACK_STAND_IMAGES нет образа pg"; return 1; }
    redis_image=$(dchk_stand_image redis) || { DCHK_REASON="стенд не поднялся: в STACK_STAND_IMAGES нет образа redis"; return 1; }
    DCHK_STAND_NET="$net"  # сразу: отказ на полпути убирает то, что успело встать
    if ! docker network create --internal "$net" >>"$1" 2>&1 </dev/null 6>&-; then
        DCHK_REASON="стенд не поднялся: сеть не создана"
    elif ! docker run -d --name "$net-pg" --memory 512m --memory-swap 512m --network "$net" --network-alias pg \
            -e POSTGRES_USER=app_user -e POSTGRES_PASSWORD=test -e POSTGRES_DB=app_test \
            "$pg_image" >>"$1" 2>&1 </dev/null 6>&-; then
        DCHK_REASON="стенд не поднялся: postgres не запущен"
    elif ! docker run -d --name "$net-redis" --memory 128m --memory-swap 128m --network "$net" --network-alias stand-kv \
            "$redis_image" >>"$1" 2>&1 </dev/null 6>&-; then
        DCHK_REASON="стенд не поднялся: redis не запущен"
    else
        # Срок считается от старта служб: запуск контейнеров сам занимает секунды.
        deadline=$((SECONDS + ${DEPLOY_CHECK_STAND_WAIT_SEC:-120}))
        # -h 127.0.0.1: временный сервер initdb слушает только unix-сокет, и без -h
        # pg_isready даёт ложное «готов» до настоящего старта. Потолок на сам exec:
        # зависший docker иначе съел бы срок ожидания целиком.
        until timeout "${DEPLOY_CHECK_STAND_EXEC_SEC:-10}" docker exec "$net-pg" pg_isready -h 127.0.0.1 -U app_user -d app_test >>"$1" 2>&1 </dev/null 6>&-; do
            if [ "$SECONDS" -ge "$deadline" ]; then
                DCHK_REASON="стенд не поднялся: postgres не готов за ${DEPLOY_CHECK_STAND_WAIT_SEC:-120} с"
                break
            fi
            sleep 1
        done
    fi
    [ -n "$DCHK_REASON" ] || return 0
    dchk_stand_down
    return 1
}

# Один замер: свободная память и нагрузка; минимум и максимум идут в записку.
dchk_sample() {
    local load
    DCHK_MEM_NOW=$(awk '/^MemAvailable:/ {printf "%d", $2 / 1024}' "${DEPLOY_CHECK_MEMINFO:-/proc/meminfo}")
    DCHK_MEM_NOW=${DCHK_MEM_NOW:-0}
    if [ -z "$DCHK_MEM_MIN" ] || [ "$DCHK_MEM_NOW" -lt "$DCHK_MEM_MIN" ]; then DCHK_MEM_MIN=$DCHK_MEM_NOW; fi
    load=$(cut -d' ' -f1 /proc/loadavg)
    DCHK_LOAD_MAX=$(awk -v a="$DCHK_LOAD_MAX" -v b="$load" 'BEGIN {print (b > a) ? b : a}')
}

dchk_wait_mem() {
    local deadline=$((SECONDS + ${DEPLOY_CHECK_MEM_WAIT_SEC:-600}))
    while :; do
        dchk_sample
        [ "$DCHK_MEM_NOW" -lt "$STACK_CHECK_MIN_FREE_MB" ] || return 0
        [ "$SECONDS" -lt "$deadline" ] || break
        sleep 1
    done
    DCHK_REASON="доступно $DCHK_MEM_NOW МБ, нужно $STACK_CHECK_MIN_FREE_MB"
    return 1
}

# Опрос раз в 10 с: порог «до набора» не ловит набор, раздувшийся на ходу.
# Ниже половины порога контейнер набора снимается — соседям на сервере память нужнее.
dchk_supervise() {  # $1 = pid клиента docker
    local ticks=0
    while kill -0 "$1" 2>/dev/null; do
        sleep 1
        ticks=$((ticks + 1))
        [ $((ticks % 10)) -eq 0 ] || continue
        dchk_sample
        if [ "$DCHK_MEM_NOW" -lt $((STACK_CHECK_MIN_FREE_MB / 2)) ]; then
            DCHK_REASON="снят по памяти: доступно $DCHK_MEM_NOW МБ при пороге $STACK_CHECK_MIN_FREE_MB"
            docker rm -f "$DCHK_CONT" >/dev/null 2>&1 || true
            return 0
        fi
    done
}

# Код клиента → слово: 0 — passed; таймаут и отказ docker — error; прочее — failed.
dchk_step_word() {  # $1 = код
    case "$1" in
        0) echo passed ;;
        124|125|126|127|137) echo error ;;
        *) echo failed ;;
    esac
}

# docker exec в контейнере набора под потолком времени и присмотром за памятью.
# fd 6 (лок) закрыт для docker: пережившее нас дитя не должно держать лок.
dchk_exec() {  # $1 каталог, $2 команда, $3 секунд, $4 лог, $5.. = -e KEY=VAL
    local dir=$1 cmd=$2 limit=$3 log=$4 rc=0 pid
    shift 4
    timeout -k 30 "$limit" docker exec -w "/w/$dir" "$@" "$DCHK_CONT" sh -c "$cmd" >>"$log" 2>&1 </dev/null 6>&- &
    pid=$!
    dchk_supervise "$pid"
    wait "$pid" || rc=$?
    [ -z "$DCHK_REASON" ] || return 125
    return "$rc"
}

# Контейнер набора: сеть $3 (пусто — сеть по умолчанию, bridge). Живёт не дольше
# потолка набора плюс запас: брошенный обрывом двери, он умирает сам.
dchk_start() {  # $1 имя, $2 образ, $3 сеть, $4 секунд жизни, $5 лог, $6 pull never|пусто
    local -a net=() pull=()
    [ -z "$3" ] || net=(--network "$3")
    [ -z "$6" ] || pull=(--pull never)
    timeout -k 30 "$4" docker run -d --init --name "$1" --memory 2g --memory-swap 2g --cpus 2 \
        --pids-limit "$DOOR_CHECKS_PIDS_LIMIT" --user "$DCHK_UID" \
        ${net[@]+"${net[@]}"} ${pull[@]+"${pull[@]}"} \
        -v "$DCHK_COPY:/w" -w /w "$2" sleep "$(($4 + 120))" >>"$5" 2>&1 </dev/null 6>&-
}

# Установка идёт С сетью (ей нужны пакеты) и исполняет install из ветки — поэтому
# только здесь, внутри этапа проверки, под потолком процессов контейнера.
dchk_install() {  # $1 = JSON набора, $2 = лог, $3 = секунд
    local rc=0
    dchk_exec . "$(jq -r .install <<<"$1")" "$3" "$2" || rc=$?
    [ "$rc" = 0 ] || DCHK_REASON="${DCHK_REASON:-установка не прошла (код $rc)}"
    return "$rc"
}

# Шаги идут без сети: сначала отключаем bridge, при стенде подключаем его сеть.
dchk_switch_net() {  # $1 = лог
    docker network disconnect bridge "$DCHK_CONT" >>"$1" 2>&1 </dev/null 6>&- \
        || { DCHK_REASON="сеть установки не отключена"; return 1; }
    [ -z "$DCHK_STAND_NET" ] || docker network connect "$DCHK_STAND_NET" "$DCHK_CONT" >>"$1" 2>&1 </dev/null 6>&- \
        || { DCHK_REASON="стенд не подключён к контейнеру набора"; return 1; }
}

# Прогон шагов набора; результат — DCHK_RESULT, DCHK_PASSED, DCHK_SKIPPED.
# Потолок timeout_s — на набор целиком: каждому шагу достаётся остаток.
dchk_steps() {  # $1 = JSON набора, $2 = лог, $3 = начало набора (SECONDS)
    local step rc left ran limit pair
    local -a stand_env=() envs
    limit=$(jq -r .timeout_s <<<"$1")
    if [ -n "$DCHK_STAND_NET" ]; then
        local pfx=${STACK_STAND_ENV_PREFIX:-STAND_}
        stand_env=(-e "${pfx}PG_HOST=pg" -e "${pfx}PG_PORT=5432" -e "${pfx}REDIS_HOST=stand-kv" -e "${pfx}REDIS_PORT=6379")
    fi
    while IFS= read -r step; do
        left=$((limit - (SECONDS - $3)))
        [ "$left" -gt 1 ] || left=1
        envs=(${stand_env[@]+"${stand_env[@]}"})
        while IFS= read -r pair; do envs+=(-e "$pair"); done < <(jq -r '.env | to_entries[] | "\(.key)=\(.value)"' <<<"$step")
        rc=0
        dchk_exec "$(jq -r .dir <<<"$step")" "$(jq -r .cmd <<<"$step")" "$left" "$2" ${envs[@]+"${envs[@]}"} || rc=$?
        DCHK_RESULT=$(dchk_step_word "$rc")
        [ "$DCHK_RESULT" = passed ] || break
        DCHK_PASSED=$((DCHK_PASSED + 1))
    done < <(jq -c '.steps[]' <<<"$1")
    ran=$DCHK_PASSED
    [ "$DCHK_RESULT" = passed ] || ran=$((ran + 1))
    DCHK_SKIPPED=$(( $(jq '.steps | length' <<<"$1") - ran ))
}

# Образ набора: image — артефакт этапа B (переданный довод) или готовый образ; сети
# нет с самого run. Без image — база набора, сеть у установки.
dchk_suite_body() {  # $1 = JSON набора, $2 = лог, $3 = образ-артефакт, $4 = начало
    local name image limit img net pull_mode=""
    name=$(jq -r .name <<<"$1"); image=$(jq -r '.image // empty' <<<"$1"); limit=$(jq -r .timeout_s <<<"$1")
    case "$image" in
        "") img=$(jq -r .base <<<"$1"); net="" ;;
        артефакт)
            img=$3; net=none; pull_mode=never
            [ -n "$img" ] || { DCHK_REASON="набор $name: образ артефакта не передан"; return 1; }
            ;;
        *) img=$image; net=none; pull_mode=never ;;
    esac
    dchk_wait_mem || return 1
    if [ "$(jq -r '.stand // "none"' <<<"$1")" = pg-redis ]; then
        dchk_stand_up "$2" || return 1
        # Готовый образ без установки встаёт сразу в сеть стенда; прочим сеть стенда
        # подключается после установки.
        [ -z "$image" ] || net=$DCHK_STAND_NET
    fi
    DCHK_CONT="$DCHK_PREFIX$DCHK_ID-$name"
    dchk_start "$DCHK_CONT" "$img" "$net" "$limit" "$2" "$pull_mode" \
        || { DCHK_REASON="контейнер набора не запущен"; return 1; }
    if [ -z "$image" ]; then
        dchk_install "$1" "$2" "$limit" || return 1
        dchk_switch_net "$2" || return 1
    fi
    dchk_steps "$1" "$2" "$4"
}

dchk_suite() {  # $1 = JSON набора, $2 = sha8, $3 = образ-артефакт
    local name log started=$SECONDS
    name=$(jq -r .name <<<"$1")
    log="$DCHK_LOG_ROOT/$2-$name.log"
    : > "$log"
    DCHK_RESULT=error; DCHK_PASSED=0; DCHK_SKIPPED=$(jq '.steps | length' <<<"$1"); DCHK_REASON=""
    dchk_suite_body "$1" "$log" "$3" "$started" || true
    dchk_cleanup_suite
    DCHK_RESULTS=$(jq -c --arg n "$name" --arg r "$DCHK_RESULT" --argjson p "$DCHK_PASSED" \
        --argjson s "$DCHK_SKIPPED" --argjson t "$((SECONDS - started))" --arg l "$log" \
        '. + [{name: $n, result: $r, passed: $p, skipped: $s, seconds: $t, log: $l}]' <<<"$DCHK_RESULTS")
    case "$DCHK_RESULT" in
        passed) say "набор $name: passed" ;;
        failed) say "набор $name: failed — лог $log" ;;
        *) say "ОШИБКА: набор $name: ${DCHK_REASON:-не отработал (таймаут или отказ docker)} — лог $log" ;;
    esac
    [ "$DCHK_RESULT" = passed ]
}

# Контейнер и стенд убираются на любом исходе набора; копия кода живёт до конца прогона.
dchk_cleanup_suite() {
    [ -z "$DCHK_CONT" ] || docker rm -f "$DCHK_CONT" >/dev/null 2>&1 || true
    DCHK_CONT=""
    dchk_stand_down
}

dchk_file_sha() {  # $1 = sha → хэш файла проверок в этом коммите
    local tmp rc=0
    tmp=$(mktemp)
    git -C "$PRODUCT_REPO" show "$1:$STACK_CHECKS_FILE" > "$tmp" 2>/dev/null \
        && python3 "$DOOR_CHECKS_PY" хэш-файла "$tmp" || rc=$?
    rm -f "$tmp"
    return "$rc"
}

dchk_copy() {  # $1 = sha; копия ровно из sha, без неотслеживаемого из клона
    local tarball rc=0
    mkdir -p "$DCHK_WORK_ROOT" "$DCHK_LOG_ROOT"
    DCHK_COPY="$DCHK_WORK_ROOT/${1:0:8}-$DCHK_ID"
    tarball="$DCHK_COPY.tar"
    mkdir "$DCHK_COPY"
    # Через файл: конвейер скрыл бы отказ git archive за успехом tar.
    git -C "$PRODUCT_REPO" archive -o "$tarball" "$1" && tar -x -C "$DCHK_COPY" -f "$tarball" || rc=$?
    rm -f "$tarball"
    return "$rc"
}

# Записка пишется всегда: и когда наборы не отработали вовсе (reason вместо suites).
dchk_emit() {  # $1 = исход, $2 = data JSON
    HARNESS_TASK=- HARNESS_LOG_DIR="${HARNESS_LOG_DIR:-$LOG_DIR}" \
        harness_event deploy-checks scripts/lib/door_checks.sh deploy.checks "$1" --json "$2"
}

dchk_note_broken() {  # $1 = sha, $2 = ветка, $3 = причина
    dchk_emit error "$(jq -cn --arg sha "$1" --arg branch "$2" --arg reason "$3" \
        '{sha: $sha, branch: $branch, tests: "error", reason: $reason}')"
    DCHK_TESTS=error
    say "ОШИБКА: тесты ветки $2 по ${1:0:8}: error — $3"
}

dchk_note() {  # $1 sha, $2 ветка, $3 хэш файла, $4 JSON выбранных имён, $5 секунд
    local only payload data
    # Без кавычек: пустой ключ — ни одного слова, а не одно пустое.
    # shellcheck disable=SC2086
    only=$(jq -cn '$ARGS.positional' --args ${STACK_CHECKS_ONLY:-})
    payload=$(jq -cn --arg sha "$1" --arg branch "$2" --arg fsha "$3" --argjson only "$only" \
        --argjson selected "$4" --argjson suites "$DCHK_RESULTS" --argjson mem "${DCHK_MEM_MIN:-0}" \
        --argjson load "$DCHK_LOAD_MAX" --argjson sec "$5" \
        '{sha: $sha, branch: $branch, checks_file_sha: $fsha, selected: $selected, only: $only,
          suites: $suites, mem_min_mb: $mem, load_max: $load, seconds: $sec}')
    data=$(python3 "$DOOR_CHECKS_PY" записка-собрать <<<"$payload") \
        || { dchk_note_broken "$1" "$2" "записка не собрана"; return; }
    DCHK_TESTS=$(jq -r .tests <<<"$data")
    dchk_emit "$([ "$DCHK_TESTS" = success ] && echo ok || echo error)" "$data"
}

# Лок общий для проверок: две сразу — двойная нагрузка на сервер. Держится на fd 6
# до конца прогона (подоболочка двери его и отпускает).
dchk_lock() {
    local lock="$LOG_DIR/locks/checks.lock"
    mkdir -p "$LOG_DIR/locks"
    exec 6>>"$lock"
    flock -n 6 || { say "занято: идёт проверка $(cat "$lock" 2>/dev/null)"; return 1; }
    printf 'pid %s с %s\n' "$$" "$(date '+%H:%M:%S')" > "$lock"
}

dchk_run() {  # $1 = sha, $2 = ветка, $3 = образ-артефакт
    local sha=$1 started=$SECONDS fsha all names total chosen suite
    dchk_keys_ok || return 1
    dchk_lock || return 1
    DCHK_ID="${DEPLOY_CHECK_RUN_ID:-$$-$(date +%s)}"
    DCHK_WORK_ROOT="${DEPLOY_CHECK_WORK_ROOT:-/var/tmp/door-checks}"
    DCHK_LOG_ROOT="${DEPLOY_CHECK_LOG_ROOT:-$LOG_DIR/door-checks}"
    DCHK_RESULTS="[]"; DCHK_MEM_MIN=""; DCHK_LOAD_MAX=0
    DCHK_PREFIX=$(dchk_prefix)
    trap dchk_cleanup EXIT
    trap 'dchk_cleanup; exit 143' INT TERM
    dchk_sweep_orphans
    fsha=$(dchk_file_sha "$sha") || { dchk_note_broken "$sha" "$2" "файла $STACK_CHECKS_FILE в коммите нет"; return 1; }
    dchk_copy "$sha" || { dchk_note_broken "$sha" "$2" "копия кода не снята"; return 1; }
    # shellcheck disable=SC2015
    all=$(python3 "$DOOR_CHECKS_PY" наборы "$DCHK_COPY/$STACK_CHECKS_FILE") \
        && chosen=$(python3 "$DOOR_CHECKS_PY" наборы "$DCHK_COPY/$STACK_CHECKS_FILE" ${STACK_CHECKS_ONLY:+--только "$STACK_CHECKS_ONLY"}) \
        || { dchk_note_broken "$sha" "$2" "файл проверок не разобран"; return 1; }
    names=$(jq -c '[.[].name]' <<<"$chosen"); total=$(jq length <<<"$all")
    while IFS= read -r suite; do
        dchk_suite "$suite" "${sha:0:8}" "${3:-}" || break
    done < <(jq -c '.[]' <<<"$chosen")
    dchk_note "$sha" "$2" "$fsha" "$names" "$((SECONDS - started))"
    say "тесты ветки $2 по ${sha:0:8}: $DCHK_TESTS (наборов $(jq length <<<"$names") из $total)"
    [ "$DCHK_TESTS" = success ]
}

# Этап тестов: код 0 только при success. Своя подоболочка: лок, trap и состояние
# прогона не протекают в зовущего.
door_checks_run() {  # $1 = sha, $2 = ветка, $3 = образ-артефакт <образ>:<sha12> (необязательно)
    ( dchk_run "$@" )
}

# Записка success по тройке (sha, хэш файла проверок, только): «путь<TAB>ts» или пусто.
dchk_note_for() {  # $1 = sha
    local fsha
    fsha=$(dchk_file_sha "$1") || return 0
    python3 "$DOOR_CHECKS_PY" записка "${HARNESS_LOG_DIR:-$LOG_DIR}/deploy-checks.jsonl" "$1" "$fsha" "${STACK_CHECKS_ONLY:-}"
}

# Записка success избавляет от повторного прогона: наборы не гоняются, код 0.
door_checks_cached() {  # $1 = sha, $2 = ветка, $3 = образ-артефакт
    local found
    found=$(dchk_note_for "$1")
    if [ -n "$found" ]; then
        say "по записке: тесты ветки $2 по ${1:0:8} уже success (${found//$'\t'/ от }) — наборы не гоняются заново"
        return 0
    fi
    door_checks_run "$@"
}

# Есть ли записка success на этот sha: выкат тогда не собирает и не гоняет наборы заново.
door_checks_noted() {  # $1 = sha
    [ -n "$(dchk_note_for "$1")" ]
}
