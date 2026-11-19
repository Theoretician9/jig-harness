#!/usr/bin/env bash
#
# test_door_checks.sh — проба тестового этапа двери (scripts/lib/door_checks.sh):
# наборы ветки идут в одноразовых контейнерах без сети, с потолками памяти,
# времени и процессов, а итог ложится запиской в deploy-checks.jsonl.
#
# Зачем. Записка tests=success — единственное, чему верит выкат на прод. Набор,
# «прошедший» без прогона, контейнер, переживший таймаут, копия кода из рабочего
# дерева вместо sha, тесты не на том образе, что едет, или уборка, снёсшая стенд
# соседней установки, дают ложное «зелено» или вред на общем сервере (спека §3
# стр. 2, 8, 9; §4). Поэтому docker НАСТОЯЩИЙ: подставной судил бы свои ответы,
# а не cgroup, --init и --network none.
#
# Драйвер зовёт функции библиотеки напрямую (source в чистом bash под env -i), а
# не через deploy.sh: помощнику deploy.sh закрыт сторожем, цепочки этапа T ещё нет
# (план §0 п. 2, фаза 3а). Живой /etc/harness/harness.conf проба не читает: ключи
# идут окружением, корень установки — каталог стенда (HARNESS_HOME,
# HARNESS_DOOR_STAND_HOME).
#
# Контракт, который проба ждёт от библиотеки:
#   source scripts/lib/door_checks.sh   — зависимости: say (ставит зовущий) и
#                                         git -C "$PRODUCT_REPO"; записку пишет
#                                         сама через harness_event (events.sh рядом)
#   door_checks_run <sha> <ветка> [<образ>:<sha12>]
#       лок $LOG_DIR/locks/checks.lock (занят → «занято: идёт проверка», код ≠0),
#       уборка сирот своего префикса, наборы, записка; код 0 только при success.
#       Набор с полем `image: артефакт` идёт на переданном образе, без docker
#       commit: docker run сразу с --network none, установки нет (текст install
#       в docker не уходит). Набор без поля — ОДИН контейнер образа base: docker
#       run с сетью (не none), install через docker exec, затем docker network
#       disconnect <сеть> <контейнер>, затем шаги через docker exec; ни commit,
#       ни build, ни кэша образа. Текст install и cmd шага стоит доводом своего
#       docker exec (по нему журнал обёртки судит порядок). Оба вида: у docker
#       run --pids-limit 512 и --user из ключа (решение управляющего 16.11 03:25,
#       п. 3).
#       Записка несёт настоящие data.sha (40 знаков) и data.checks_file_sha (64):
#       маска событий их не скрывает (ключи в DIGEST_KEYS events.py, п. 2).
#   door_checks_cached <sha> <ветка> [<образ>:<sha12>]
#       записка success по (sha, хэш файла, STACK_CHECKS_ONLY) есть → «по записке»,
#       код 0, наборы не гоняются; нет — door_checks_run.
# Ключи (спека §5.2): PROJECT_NAME PRODUCT_REPO LOG_DIR STACK_CHECKS_FILE
#   STACK_CHECKS_ONLY STACK_CHECK_MIN_FREE_MB STACK_CHECK_UID STACK_RUN_AS
#   STACK_STAND_IMAGES («pg=<образ> redis=<образ>», digest допустим)
#   STACK_STAND_ENV_PREFIX (проба ставит INTEGRATION_TEST_).
#   STACK_CHECK_UID и STACK_RUN_AS пусты, или STACK_RUN_AS=root при пустом
#   STACK_CHECK_UID — отказ ДО первого вызова docker. Пустой STACK_CHECK_UID —
#   --user <uid>:<gid> пользователя STACK_RUN_AS.
# Имена (префикс P = «<PROJECT_NAME>-check-», константа): контейнер набора
#   P<id>-<набор>, сеть стенда P<id>, службы P<id>-pg и P<id>-redis; уборка сирот
#   трогает только P*-pg, P*-redis и сети P*, кроме своего P<id>. Потолок процессов
#   контейнера набора — --pids-limit 512 (константа). Копия кода — <корень>/<sha8>-<id>,
#   лог набора — <каталог логов>/<sha8>-<набор>.log.
# Двери для проб (окружение):
#   DEPLOY_CHECK_RUN_ID <id>, DEPLOY_CHECK_WORK_ROOT (вместо /var/tmp/door-checks),
#   DEPLOY_CHECK_LOG_ROOT (вместо /var/log/harness/door-checks), DEPLOY_CHECK_MEMINFO
#   (вместо /proc/meminfo), DEPLOY_CHECK_MEM_WAIT_SEC (600), DEPLOY_CHECK_STAND_WAIT_SEC
#   (120), DEPLOY_CHECK_STAND_EXEC_SEC (10); HARNESS_DOOR_CHECKS_LIB — путь к
#   библиотеке (мутированная копия).
#
# Стенд свой во временном каталоге, на каждый случай — новый: work → remote.git
# (bare) → door (клон, PRODUCT_REPO; драйвер сам делает в нём fetch, HEAD клона
# стоит на main). PROJECT_NAME несёт метку прогона пробы, поэтому все имена docker
# пробы — под её уникальным префиксом; уборка пробы снимает только их и одного
# «чужого» соседа other-check-<метка>-pg, которого проба сама и поднимает.
# Первым в PATH библиотеки стоит обёртка docker: пишет каждый вызов строкой в
# журнал случая, отказывает или виснет по шаблонам случая, прочее отдаёт
# настоящему docker. Подставной sudo отказывает: тестам прод не нужен.
#
# Образы: база наборов — alpine:3.20 (sh и busybox wget есть, сети не надо).
# Стенд pg-redis — образы из DOOR_CHECKS_STAND_IMAGES (умолчание
# «pg=postgres:15 redis=redis:7-alpine»); проба их не тянет. Нет образа — случаи
# «пропущен: нет образа <имя>»: не зелёные и не красные, счёт в последней строке.
#
#   bash scripts/test_door_checks.sh        (только фоном: гоняет docker)
# Код: 0 — неудач нет, 1 — есть. Итог:
#   «DOOR-CHECKS: случаев N, неудач M, пропущено K[: нет образа …]».
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
LIB="${HARNESS_DOOR_CHECKS_LIB:-$HERE/lib/door_checks.sh}"
source "$HERE/lib/selftest_cases.sh"

ID="$$t$(date +%s)"
PNAME="dc$ID"
P="$PNAME-check-"
FOREIGN="other-check-$ID-pg"
BASE=alpine:3.20
STAND_IMAGES="${DOOR_CHECKS_STAND_IMAGES:-pg=postgres:15 redis=redis:7-alpine}"
PG_IMG=$(printf '%s\n' $STAND_IMAGES | sed -n 's/^pg=//p')
REDIS_IMG=$(printf '%s\n' $STAND_IMAGES | sed -n 's/^redis=//p')
LIVE_NOTES=/var/log/harness/deploy-checks.jsonl
T=$(mktemp -d -t door-checks-proba.XXXXXX)
BG_PIDS=()
SKIPPED=0
SKIP_WHY=""

# Уборка только своего: всё docker под префиксом прогона пробы и свой «чужой» сосед.
cleanup() {
    local pid name
    for pid in ${BG_PIDS[@]+"${BG_PIDS[@]}"}; do kill "$pid" 2>/dev/null; done
    pkill -f "docker-hang-$ID" 2>/dev/null
    for name in $(docker ps -a --format '{{.Names}}' | grep -E "^$P") "$FOREIGN"; do
        docker rm -f "$name" >/dev/null 2>&1
    done
    for name in $(docker images --format '{{.Repository}}:{{.Tag}}' | grep -E "^$PNAME-"); do
        docker rmi "$name" >/dev/null 2>&1
    done
    for name in $(docker network ls --format '{{.Name}}' | grep -E "^$P"); do
        docker network rm "$name" >/dev/null 2>&1
    done
    rm -rf "$T"
}
trap cleanup EXIT

# missing_image <образ…>: печатает через пробел образы, которых нет локально.
missing_image() {
    local img out=""
    for img in "$@"; do
        [ -n "$img" ] && docker image inspect "$img" >/dev/null 2>&1 || out="${out:+$out }${img:-<не задан>}"
    done
    printf '%s' "$out"
}
skip_case() {  # $1 — случай, $2 — образ
    SKIPPED=$((SKIPPED + 1))
    printf '  пропуск %s: нет образа %s\n' "$1" "$2"
    case " $SKIP_WHY " in *" $2 "*) ;; *) SKIP_WHY="${SKIP_WHY:+$SKIP_WHY }$2" ;; esac
}
BASE_MISSING=$(missing_image "$BASE")
STAND_MISSING=$(missing_image "$PG_IMG" "$REDIS_IMG")

# ── общее для всех стендов ─────────────────────────────────────────────────
mkdir -p "$T/bin"
cat > "$T/bin/sudo" <<'SH'
#!/usr/bin/env bash
echo "sudo стенда: тестам прод не нужен" >&2
exit 1
SH
chmod +x "$T/bin/sudo"

REAL_DOCKER=$(command -v docker)
cat > "$T/bin/docker" <<SH
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "\${DOCKER_JOURNAL:-/dev/null}"
if [ -s "\${DOCKER_FAIL:-}" ] && printf '%s\n' "\$*" | grep -qEf "\$DOCKER_FAIL"; then
    echo "docker стенда: отказ по шаблону" >&2
    exit 1
fi
# Зависший вызов: exec сохраняет pid, timeout библиотеки убивает сам sleep;
# имя процесса несёт метку пробы — уборка пробы снимает брошенное.
# Подставной успех: службы стенда без их образов (случай 36 судит учётку, а не pg).
if [ -s "\${DOCKER_OK:-}" ] && printf '%s\n' "\$*" | grep -qEf "\$DOCKER_OK"; then
    echo "docker стенда: успех по шаблону"
    exit 0
fi
if [ -s "\${DOCKER_HANG:-}" ] && printf '%s\n' "\$*" | grep -qEf "\$DOCKER_HANG"; then
    date +%s >> "\$DOCKER_HANG.t"
    exec -a "docker-hang-$ID" sleep 3600
fi
exec "$REAL_DOCKER" "\$@"
SH
chmod +x "$T/bin/docker"

GIT_ID=(-c user.email=proba@stend -c user.name=proba)

# meminfo <МБ> <файл>: подставной /proc/meminfo; замена атомарна (mv).
meminfo() {
    printf 'MemTotal:       65536000 kB\nMemFree:        1000 kB\nMemAvailable:   %s kB\n' \
        "$(( $1 * 1024 ))" > "$2.tmp"
    mv "$2.tmp" "$2"
}

new_stand() {  # $1 — имя случая; ставит S, RID и стенд целиком
    S="$T/$1"
    RID="$1"
    mkdir -p "$S/zhurnal" "$S/home" "$S/work" "$S/kopii" "$S/logi"
    cat > "$S/home/install.conf" <<CONF
PROJECT_NAME="$PNAME"
PROJECT_DIR="$S/net-harnesa"
LOG_DIR="$S/zhurnal"
SECRETS_DIR="$S/home/sekrety"
TMUX_SESSION="net-takoj"
AGENT_START_CMD="true"
TG_CHAT_ID="4242"
CONF
    : > "$S/home/harness.conf"
    git -C "$S/work" init -q -b main
    printf 'стенд\n' > "$S/work/README"
    git -C "$S/work" add -A
    git "${GIT_ID[@]}" -C "$S/work" commit -qm стенд
    git clone -q --bare "$S/work" "$S/remote.git"
    git -C "$S/work" remote add origin "$S/remote.git"
    git clone -q "$S/remote.git" "$S/door"
    meminfo 20000 "$S/meminfo"
    MEM_WAIT=600; STAND_WAIT=60; EXEC_WAIT=""; RUN_LIMIT=300
    ONLY=""; CHECK_UID="$(id -u):$(id -g)"; RUN_AS=""; STAND_PG=""
}

# suite <имя> <install> <timeout_s> <cmd>…: набор файла проверок на базе $BASE.
# Команды в кавычках: голые true/false YAML прочёл бы как булевы.
suite() {
    local name=$1 install=$2 limit=$3 cmd; shift 3
    printf "  - name: %s\n    base: %s\n    install: '%s'\n    manifests: [m.txt]\n    timeout_s: %s\n    steps:\n" \
        "$name" "$BASE" "$install" "$limit"
    for cmd in "$@"; do printf "      - cmd: '%s'\n" "$cmd"; done
}
INSTALL_OK='test -s /w/m.txt'

# branch_with <тело suites>: ветка x в remote.git; клон двери получает её fetch-ем
# (подготовка стенда, не библиотека), HEAD клона остаётся на main. Ставит SHA и CHECKS_SHA.
branch_with() {
    git -C "$S/work" checkout -q -B x main
    mkdir -p "$S/work/deploy"
    printf 'suites:\n%s\n' "$1" > "$S/work/deploy/server-checks.yaml"
    printf 'манифест\n' > "$S/work/m.txt"
    printf 'маркер %s\n' "$S" > "$S/work/marker.txt"
    git -C "$S/work" add -A
    git "${GIT_ID[@]}" -C "$S/work" commit -qm "ветка x"
    git -C "$S/remote.git" fetch -q "$S/work" "+refs/heads/x:refs/heads/x"
    git -C "$S/door" fetch -q origin
    CHECKS_SHA=$(sha256sum "$S/work/deploy/server-checks.yaml" | cut -d' ' -f1)
    SHA=$(git -C "$S/work" rev-parse HEAD)
    printf '%s\n' "${SHA:0:8}" >> "$T/sha8"
}

# Чистый bash: say ставит драйвер, остальное библиотека берёт сама.
DRIVER='say() { printf "%s\n" "$*"; }
lib=$1; shift
[ -r "$lib" ] || { say "ПРОБА: нет библиотеки $lib"; exit 127; }
source "$lib" || { say "ПРОБА: библиотека не подключилась"; exit 127; }
"$@"'

# run_lib <функция> <доводы…>: вызов библиотеки в стенде $S; вывод OUT, код RC, секунды SECS.
# Под env -i: унаследованная переменная увела бы прогон в живую установку.
run_lib() {
    local t0
    t0=$(date +%s)
    OUT=$(cd "$S" && env -i PATH="$T/bin:/usr/local/bin:/usr/bin:/bin" HOME="$S/home" LC_ALL=C.UTF-8 \
          HARNESS_HOME="$S/home" HARNESS_DOOR_STAND_HOME="$S/home" \
          HARNESS_INSTALL_CONF="$S/home/install.conf" HARNESS_CONF="$S/home/harness.conf" \
          HARNESS_LOG_DIR="$S/zhurnal" HARNESS_TASK=- \
          PROJECT_NAME="$PNAME" LOG_DIR="$S/zhurnal" PRODUCT_REPO="$S/door" \
          STACK_CHECKS_FILE="deploy/server-checks.yaml" STACK_CHECKS_ONLY="$ONLY" \
          STACK_CHECK_MIN_FREE_MB=4096 STACK_CHECK_UID="$CHECK_UID" STACK_RUN_AS="$RUN_AS" \
          STACK_STAND_IMAGES="$STAND_IMAGES" STACK_STAND_ENV_PREFIX=INTEGRATION_TEST_ \
          STACK_STAND_PG="$STAND_PG" DOCKER_OK="$S/docker.ok" \
          DEPLOY_CHECK_RUN_ID="$RID" DEPLOY_CHECK_WORK_ROOT="$S/kopii" DEPLOY_CHECK_LOG_ROOT="$S/logi" \
          DEPLOY_CHECK_MEMINFO="$S/meminfo" DEPLOY_CHECK_MEM_WAIT_SEC="$MEM_WAIT" \
          DEPLOY_CHECK_STAND_WAIT_SEC="$STAND_WAIT" DEPLOY_CHECK_STAND_EXEC_SEC="$EXEC_WAIT" \
          DOCKER_JOURNAL="$S/docker.log" DOCKER_FAIL="$S/docker.fail" DOCKER_HANG="$S/docker.hang" \
          timeout "$RUN_LIMIT" bash -c "$DRIVER" door-checks-proba "$LIB" "$@" 2>&1)
    RC=$?
    SECS=$(( $(date +%s) - t0 ))
}

# ── чтение итогов ──────────────────────────────────────────────────────────
has() { printf '%s' "$1" | grep -qF -- "$2" && echo да || echo нет; }
lacks() { printf '%s' "$1" | grep -qF -- "$2" && echo нет || echo да; }
kod() { [ "$RC" = 0 ] && echo 0 || echo "≠0"; }
yes_no() { "$@" && echo да || echo нет; }
NOTES() { printf '%s' "$S/zhurnal/deploy-checks.jsonl"; }
# note <jq-выражение>: поле последней записки; нет записки — «нет записки».
note() {
    local line
    line=$(tail -n 1 "$(NOTES)" 2>/dev/null)
    [ -n "$line" ] || { echo "нет записки"; return; }
    printf '%s' "$line" | jq -r "$1" 2>/dev/null || echo "не json"
}
# Свои контейнеры прогона — живые или мёртвые; после прогона их быть не должно.
leftovers() { docker ps -a --format '{{.Names}}' | grep -cE -- "^$P$RID(-|\$)"; }
# Образы под префиксом установки: кэша образов наборов нет, их быть не должно.
own_images() { docker images --format '{{.Repository}}' | grep -cE -- "^$P"; }
# Сколько контейнеров прогона docker СОЗДАЛ с момента $1: уборка прячет их из ps, события — нет.
created_since() {
    docker events --since "$1" --until "$(date +%s)" --filter type=container --filter event=create \
        --format '{{.Actor.Attributes.name}}' 2>/dev/null | grep -cE -- "^$P$2(-|\$)"
}
# watch_for <сек> <флаг> <команда…>: фоном ждёт, пока команда станет истинной.
watch_for() {
    local limit=$1 flag=$2; shift 2
    ( for _ in $(seq 1 $((limit * 2))); do "$@" && { : > "$flag"; exit 0; }; sleep 0.5; done ) &
    BG_PIDS+=($!)
}
running() { docker ps --filter "name=$1" --format '{{.Names}}' | grep -qxF -- "$1"; }
show() {
    printf '    → код %s, %s с\n' "$RC" "$SECS"
    printf '%s\n' "$OUT" | tail -6 | sed 's/^/      | /'
}
DLOG() { printf '%s' "$S/docker.log"; }
# jcount <regex>: сколько вызовов docker в журнале случая подходят под шаблон.
jcount() { { cat "$(DLOG)" 2>/dev/null || true; } | grep -cE -- "$1"; }
# jrun <имя>: первый docker run с --name <имя>; нет такого — пусто.
jrun() { { cat "$(DLOG)" 2>/dev/null || true; } | grep -E -- "^run (.* )?--name $1( |\$)" | head -n 1; }
# has_words <строка> <фрагмент…>: каждый фрагмент стоит в строке отдельными словами.
has_words() {
    [ -n "$1" ] || { echo нет; return; }
    local line=" $1 " f; shift
    for f in "$@"; do case "$line" in *" $f "*) ;; *) echo нет; return ;; esac; done
    echo да
}
# runs_all <regex>: все строки docker run набора несут фрагмент $2 словом; строк нет — «нет строк».
runs_all() {
    local lines line
    lines=$({ cat "$(DLOG)" 2>/dev/null || true; } | grep -E -- "^run (.* )?--name $P$RID-")
    [ -n "$lines" ] || { echo "нет строк"; return; }
    while IFS= read -r line; do
        [ "$(has_words "$line" "$1")" = да ] || { echo нет; return; }
    done <<<"$lines"
    echo да
}
# jline <regex>: номер первой строки журнала обёртки под шаблоном; нет — пусто.
jline() { { cat "$(DLOG)" 2>/dev/null || true; } | grep -nE -- "$1" | head -n 1 | cut -d: -f1; }
# in_order <номер…>: все номера есть и строго растут (порядок вызовов docker).
in_order() {
    local prev=0 n
    for n in "$@"; do
        [ -n "$n" ] && [ "$n" -gt "$prev" ] || return 1
        prev=$n
    done
}
# Сеть изнутри контейнера: интерфейс кроме lo есть только у подключённого к сети.
# Внешний адрес не нужен — стенд без интернета судится так же.
HAS_NET='ls /sys/class/net | grep -qvx lo'

LIVE_BEFORE=$(stat -c %s "$LIVE_NOTES" 2>/dev/null || echo нет)
echo "--- тестовый этап двери ($LIB; стенд $T; префикс $P)"

NO_STAND_CASES=(1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 34 35 36)
if [ -n "$BASE_MISSING" ]; then
    for n in "${NO_STAND_CASES[@]}"; do skip_case "$n" "$BASE_MISSING"; done
else

# ── 1. зелёный набор ────────────────────────────────────────────────────────
# Почему: записка success — пропуск на прод. Судится не код выхода, а запись:
# набор назван, его результат passed, итог success, контейнеров не осталось.
echo "── 1. набор cmd: true → passed, записка tests=success"
new_stand c01
branch_with "$(suite ok "$INSTALL_OK" 120 'true')"
run_lib door_checks_run "$SHA" x; show
case_check "cmd true — код 0, записка: набор passed, tests=success, контейнеров 0" \
    "код 0; набор: ok passed; tests: success; контейнеров: 0" \
    "код $(kod); набор: $(note '.data.suites[0] | "\(.name) \(.result)"'); tests: $(note .data.tests); контейнеров: $(leftovers)"
C01_S=$S; C01_SHA=$SHA; C01_CHECKS_SHA=$CHECKS_SHA

# ── 2. красный набор ────────────────────────────────────────────────────────
# Почему: failed, записанный как passed, выкатывает красную ветку. Отказ обязан
# назвать набор и путь лога, а лог — лежать там, где назван.
echo "── 2. набор cmd: false → failure, имя набора и путь лога"
new_stand c02
branch_with "$(suite bad "$INSTALL_OK" 120 'false')"
run_lib door_checks_run "$SHA" x; show
LOG="$S/logi/${SHA:0:8}-bad.log"
case_check "cmd false — код ≠0, tests=failure, набор failed, вывод называет набор и лог, лог есть" \
    "код ≠0; tests: failure; набор: failed; имя: да; путь лога: да; лог есть: да" \
    "код $(kod); tests: $(note .data.tests); набор: $(note '.data.suites[0].result'); имя: $(has "$OUT" "набор bad"); путь лога: $(has "$OUT" "$LOG"); лог есть: $(yes_no test -f "$LOG")"

# ── 3. установка не прошла ──────────────────────────────────────────────────
# Почему: упавшая установка — «не отработало», а не «красные тесты»: по ней нельзя
# судить код. Образа набора нет вовсе (кэша нет), контейнер убран.
echo "── 3. install: exit 7 → error «установка не прошла», образов и контейнеров нет"
new_stand c03
branch_with "$(suite inst 'exit 7' 120 'true')"
run_lib door_checks_run "$SHA" x; show
case_check "install exit 7 — код ≠0, tests=error, слова, образов префикса 0, контейнеров 0" \
    "код ≠0; tests: error; слова: да; образов: 0; контейнеров: 0" \
    "код $(kod); tests: $(note .data.tests); слова: $(has "$OUT" 'установка не прошла'); образов: $(own_images); контейнеров: $(leftovers)"

# ── 4. таймаут набора ───────────────────────────────────────────────────────
# Почему: timeout без --init и -k убивает лишь клиента docker, а контейнер живёт
# и ест память сервера. Потолок 5 с: error не позже 40 с и ни одного контейнера.
echo "── 4. sleep 3600 при timeout_s 5 → error не позже 40 с, контейнеров 0"
new_stand c04
branch_with "$(suite slow "$INSTALL_OK" 5 'sleep 3600')"
run_lib door_checks_run "$SHA" x; show
case_check "sleep 3600, timeout_s 5 — код ≠0, tests=error, ≤40 с, контейнеров 0" \
    "код ≠0; tests: error; не дольше 40 с: да; контейнеров: 0" \
    "код $(kod); tests: $(note .data.tests); не дольше 40 с: $(yes_no test "$SECS" -le 40); контейнеров: $(leftovers)"

# ── 5. мало памяти до набора ────────────────────────────────────────────────
# Почему: набор на 2 ГБ при 1 ГБ свободных роняет прод. Отказ называет цифры и
# приходит ПОСЛЕ ожидания (3 с вместо 600), контейнер набора не создаётся.
echo "── 5. MemAvailable 1000 МБ при пороге 4096 → «доступно 1000 МБ, нужно 4096», без контейнера"
new_stand c05
branch_with "$(suite ok "$INSTALL_OK" 120 'true')"
meminfo 1000 "$S/meminfo"; MEM_WAIT=3
T0=$(date +%s)
run_lib door_checks_run "$SHA" x; show
case_check "мало памяти — код ≠0, слова с цифрами, ждал ≥3 с, контейнеров не создано" \
    "код ≠0; слова: да; ждал: да; создано: 0" \
    "код $(kod); слова: $(has "$OUT" 'доступно 1000 МБ, нужно 4096'); ждал: $(yes_no test "$SECS" -ge 3); создано: $(created_since "$T0" "$RID")"

# ── 6. память проседает во время набора ─────────────────────────────────────
# Почему: порог «до» не ловит набор, раздувшийся на ходу. Опрос раз в 10 с снимает
# СВОЙ контейнер до конца sleep 90: error «снят по памяти», ≤50 с, контейнеров 0.
echo "── 6. MemAvailable падает до 1000 МБ во время набора → error «снят по памяти»"
new_stand c06
branch_with "$(suite mem "$INSTALL_OK" 120 'sleep 90')"
drop_mem() { running "$P$RID-mem" && meminfo 1000 "$S/meminfo"; }
watch_for 120 "$S/seen" drop_mem
run_lib door_checks_run "$SHA" x; show
case_check "просадка памяти — код ≠0, tests=error, слова, набор жил, ≤50 с, контейнеров 0" \
    "код ≠0; tests: error; слова: да; набор жил: да; не дольше 50 с: да; контейнеров: 0" \
    "код $(kod); tests: $(note .data.tests); слова: $(has "$OUT" 'снят по памяти'); набор жил: $(yes_no test -e "$S/seen"); не дольше 50 с: $(yes_no test "$SECS" -le 50); контейнеров: $(leftovers)"

# ── 7. копия кода ──────────────────────────────────────────────────────────
# Почему: копия — ровно sha, без неотслеживаемого из клона, своя под меткой
# прогона и удалённая после. Набор доказывает содержимое копии изнутри.
echo "── 7. копия <sha8>-<id> — из sha, без неотслеживаемого, удалена после прогона"
new_stand c07
branch_with "$(suite copy "$INSTALL_OK" 120 'test -f /w/marker.txt && test -f /w/deploy/server-checks.yaml && ! test -e /w/nezakommicheno.txt && sleep 3')"
printf 'не в sha\n' > "$S/door/nezakommicheno.txt"
watch_for 120 "$S/seen" test -d "$S/kopii/${SHA:0:8}-$RID"
run_lib door_checks_run "$SHA" x; show
case_check "копия — код 0, набор passed, копия была в корне, после прогона корень пуст, в /var/tmp её нет" \
    "код 0; набор: passed; была: да; корень пуст: да; /var/tmp: нет" \
    "код $(kod); набор: $(note '.data.suites[0].result'); была: $(yes_no test -e "$S/seen"); корень пуст: $(yes_no test -z "$(ls -A "$S/kopii")"); /var/tmp: $(yes_no test -e "/var/tmp/door-checks/${SHA:0:8}-$RID")"

# ── 8. клон двери не двигается ─────────────────────────────────────────────
# Почему: клон двери — предмет выката; checkout под тестами сдвинул бы то, что
# оркестратор уже планировал. Клон стоит на main, sha записки — sha ветки.
echo "── 8. door_checks_run: HEAD, ветка и статус клона прежние, sha записки = sha ветки"
new_stand c08
branch_with "$(suite ok "$INSTALL_OK" 120 'true')"
HEAD0=$(git -C "$S/door" rev-parse HEAD); REF0=$(git -C "$S/door" symbolic-ref -q HEAD)
STAT0=$(git -C "$S/door" status --porcelain)
run_lib door_checks_run "$SHA" x; show
case_check "клон — код 0, HEAD прежний, ветка прежняя, статус прежний, sha записки = sha ветки ≠ HEAD" \
    "код 0; HEAD: да; ветка: да; статус: да; sha записки: да" \
    "код $(kod); HEAD: $(yes_no test "$(git -C "$S/door" rev-parse HEAD)" = "$HEAD0"); ветка: $(yes_no test "$(git -C "$S/door" symbolic-ref -q HEAD)" = "$REF0"); статус: $(yes_no test "$(git -C "$S/door" status --porcelain)" = "$STAT0"); sha записки: $(yes_no test "$(note .data.sha)" = "$SHA" -a "$SHA" != "$HEAD0")"

# ── 9. checks.lock занят ────────────────────────────────────────────────────
# Почему: две проверки сразу — двойная нагрузка на общий сервер (спека §3 стр. 2).
# Держатель — свой процесс пробы; отказ сразу, словами, без контейнеров.
echo "── 9. checks.lock держит другой процесс → «занято: идёт проверка», без контейнеров"
new_stand c09
branch_with "$(suite ok "$INSTALL_OK" 120 'true')"
mkdir -p "$S/zhurnal/locks"
bash -c 'exec 7>>"$1"; flock -x 7; exec sleep 300' _ "$S/zhurnal/locks/checks.lock" &
HOLDER=$!; BG_PIDS+=("$HOLDER")
for _ in $(seq 1 20); do flock -n "$S/zhurnal/locks/checks.lock" true || break; sleep 0.2; done
T0=$(date +%s)
run_lib door_checks_run "$SHA" x; show
kill "$HOLDER" 2>/dev/null
case_check "лок занят — код ≠0, «занято: идёт проверка», ≤20 с, контейнеров не создано" \
    "код ≠0; слова: да; сразу: да; создано: 0" \
    "код $(kod); слова: $(has "$OUT" 'занято: идёт проверка'); сразу: $(yes_no test "$SECS" -le 20); создано: $(created_since "$T0" "$RID")"

# ── 10. записка избавляет от повтора ────────────────────────────────────────
# Почему: тот же (sha, хэш файла, только) уже доказан; второй прогон — минуты
# нагрузки. Контроли: первый вызов контейнеры создавал (иначе «ноль созданных»
# прошёл бы у библиотеки, что не гоняет ничего), а другой STACK_CHECKS_ONLY
# записку не берёт и гонит снова (иначе «по записке» на любой журнал).
echo "── 10. door_checks_cached дважды по тому же предмету → второй «по записке», ноль контейнеров"
new_stand c10
branch_with "$(suite ok "$INSTALL_OK" 120 'true')"
T0=$(date +%s)
run_lib door_checks_cached "$SHA" x; show
RC1=$(kod); FIRST=$(created_since "$T0" "$RID")
RID=c10b; T0=$(date +%s)
run_lib door_checks_cached "$SHA" x; show
RC2=$(kod); WORDS2=$(has "$OUT" 'по записке'); SECOND=$(created_since "$T0" "$RID")
RID=c10c; ONLY=ok; T0=$(date +%s)
run_lib door_checks_cached "$SHA" x; show
case_check "записка — первый 0 и создавал; второй 0, «по записке», создано 0; другой только — гоняет снова" \
    "первый: 0; создавал: да; второй: 0; слова: да; создано: 0; другой только: да" \
    "первый: $RC1; создавал: $(yes_no test "$FIRST" -ge 1); второй: $RC2; слова: $WORDS2; создано: $SECOND; другой только: $(yes_no test "$(created_since "$T0" "$RID")" -ge 1)"

# ── 11. нет сети в контейнере набора ────────────────────────────────────────
# Почему: набор исполняет код ветки; с сетью он дотянулся бы до живых служб.
# Лог несёт слова busybox wget «can't connect» — иначе failed мог прийти и от
# отсутствия wget, а не от --network none.
echo "── 11. cmd: wget -T 3 http://1.1.1.1 → failed, в логе «can't connect»"
new_stand c11
branch_with "$(suite net "$INSTALL_OK" 120 'wget -T 3 -q -O /dev/null http://1.1.1.1')"
run_lib door_checks_run "$SHA" x; show
LOG="$S/logi/${SHA:0:8}-net.log"
case_check "нет сети — код ≠0, набор failed, лог говорит «can't connect»" \
    "код ≠0; набор: failed; лог: да" \
    "код $(kod); набор: $(note '.data.suites[0].result'); лог: $(yes_no grep -qsF "can't connect" "$LOG")"

# ── 12. схема записки ───────────────────────────────────────────────────────
# Почему: записку ищут по тройке (sha, хэш файла, только); поле не того типа,
# скрытый журналом хэш или чужой писатель — и выкат не найдёт её или найдёт не ту.
# Писатель — scripts/lib/door_checks.sh (реестр журналов, G2).
echo "── 12. записка случая 1 по схеме — поля разбором jq"
S=$C01_S
LOG="$S/logi/${C01_SHA:0:8}-ok.log"
case_check "схема — событие, исход, писатель, ts, sha, ветка, хэш файла, only, набор с числами и логом, mem/load/seconds" \
    "строк: 1; event: deploy.checks ok; src: scripts/lib/door_checks.sh; ts: да; sha: да; branch: x; checks_file_sha: да; only: array; suite: да; лог есть: да; числа: да" \
    "строк: $(cat "$(NOTES)" 2>/dev/null | wc -l); event: $(note '"\(.event) \(.outcome)"'); src: $(note .src); ts: $(yes_no test -n "$(note '.ts // empty')"); sha: $(yes_no test "$(note .data.sha)" = "$C01_SHA"); branch: $(note .data.branch); checks_file_sha: $(yes_no test "$(note .data.checks_file_sha)" = "$C01_CHECKS_SHA"); only: $(note '.data.only | type'); suite: $(yes_no test "$(note '.data.suites | length == 1 and (.[0] | (.passed|type) == "number" and (.skipped|type) == "number" and (.seconds|type) == "number" and .log == "'"$LOG"'")')" = true); лог есть: $(yes_no test -f "$LOG"); числа: $(note '[.data.mem_min_mb, .data.load_max, .data.seconds] | map(type == "number") | all | if . then "да" else "нет" end')"

# ── 13. STACK_CHECKS_ONLY ───────────────────────────────────────────────────
# Почему: частичный выбор обязан сказать «наборов N из M», иначе частичная
# проверка прозвучит как полная. Невыбранный красный набор не гоняется.
echo "── 13. STACK_CHECKS_ONLY выбирает 1 из 2 → success, only в записке, «наборов 1 из 2»"
new_stand c13
branch_with "$(suite ok "$INSTALL_OK" 120 'true')"$'\n'"$(suite bad "$INSTALL_OK" 120 'false')"
ONLY=ok
run_lib door_checks_run "$SHA" x; show
case_check "только — код 0, tests=success, only = [ok], наборов в записке 1, «наборов 1 из 2»" \
    "код 0; tests: success; only: ok; наборов: 1; слова: да" \
    "код $(kod); tests: $(note .data.tests); only: $(note '.data.only | join(" ")'); наборов: $(note '.data.suites | length'); слова: $(has "$OUT" 'наборов 1 из 2')"

# ── 14. набор без артефакта: один контейнер базы, сеть только у установки ──
# Почему: «docker commit поверх base» — второй артефакт рядом с тем, что едет
# (спека §3 стр. 8); шаг с сетью исполняет код ветки с выходом наружу. Набор без
# image ставит зависимости и гоняет шаги в ОДНОМ контейнере base (решение 3):
# run с сетью, install через exec, network disconnect, шаги через exec. Сеть
# установки подставной docker не даёт и не нужна: install сам доказывает
# изнутри, что интерфейс кроме lo есть, шаг — что его нет (иначе код ≠0). Журнал
# обёртки судит порядок: install < disconnect < шаг 1 < шаг 2. След установки
# (/tmp/inst-mark) виден обоим шагам; run 1, commit и build 0, образов 0.
echo "── 14. install с сетью → disconnect → 2 шага без сети в одном контейнере base, commit 0"
new_stand c14
branch_with "$(suite two "$INSTALL_OK && $HAS_NET && touch /tmp/inst-mark" 120 \
    "test -f /tmp/inst-mark && ! $HAS_NET && : shag-1" "test -f /tmp/inst-mark && ! $HAS_NET && : shag-2")"
run_lib door_checks_run "$SHA" x; show
RUN=$(jrun "$P$RID-two")
N_INST=$(jline "^exec .*$P$RID-two .*touch /tmp/inst-mark")
N_DISC=$(jline "^network disconnect (.* )?$P$RID-two\$")
N_STEP1=$(jline "^exec .*$P$RID-two .*: shag-1")
N_STEP2=$(jline "^exec .*$P$RID-two .*: shag-2")
case_check "один контейнер — код 0, passed 2, run 1 на base и не в none, install → disconnect → шаги, commit 0, build 0, образов 0" \
    "код 0; набор: passed 2; run: 1; на base: да; run не в none: да; порядок: да; commit: 0; build: 0; образов: 0" \
    "код $(kod); набор: $(note '.data.suites[0] | "\(.result) \(.passed)"'); run: $(jcount '^run '); на base: $(has_words "$RUN" "$BASE"); run не в none: $(yes_no test -n "$RUN" -a "$(has_words "$RUN" '--network none')" = нет); порядок: $(yes_no in_order "$N_INST" "$N_DISC" "$N_STEP1" "$N_STEP2"); commit: $(jcount '^commit'); build: $(jcount '^(build|buildx|image build)'); образов: $(own_images)"

# ── 15. набор image: артефакт идёт на переданном образе ─────────────────────
# Почему: проверено должно быть то, что едет (спека §3 стр. 8): образ набора —
# артефакт этапа B <образ>:<sha12>, переданный доводом. Артефакт пробы — alpine с
# файлом /art-marker, которого в base нет: шаг на base упал бы. Наблюдатель
# снимает id образа живого контейнера набора и сверяет с id довода. Без довода
# набор не отработал (error), а не ушёл молча на base.
echo "── 15. image: артефакт → контейнер на образе довода, commit 0; без довода — error"
new_stand c15
branch_with "$(suite art 'true' 120 'test -f /art-marker && sleep 3'; printf '    image: артефакт\n')"
ART="$PNAME-artifact:${SHA:0:12}"
docker run --name "${P}prep-art" --network none "$BASE" sh -c 'echo art > /art-marker' >/dev/null 2>&1
docker commit "${P}prep-art" "$ART" >/dev/null 2>&1
docker rm -f "${P}prep-art" >/dev/null 2>&1
ART_ID=$(docker image inspect -f '{{.Id}}' "$ART" 2>/dev/null)
art_seen() { docker inspect -f '{{.Image}}' "$P$RID-art" > "$S/art.img" 2>/dev/null && [ -s "$S/art.img" ]; }
watch_for 120 "$S/seen" art_seen
run_lib door_checks_run "$SHA" x "$ART"; show
RC1=$(kod); RES1=$(note '.data.suites[0].result'); COMMIT1=$(jcount '^commit')
RID=c15b
run_lib door_checks_run "$SHA" x; show
case_check "артефакт — код 0, passed, id образа контейнера = id довода ≠ base, commit 0; без довода — код ≠0, error" \
    "код 0; набор: passed; образ: да; не base: да; commit: 0; без довода: ≠0 error" \
    "код $RC1; набор: $RES1; образ: $(yes_no test -n "$ART_ID" -a "$(cat "$S/art.img" 2>/dev/null)" = "$ART_ID"); не base: $(yes_no test "$ART_ID" != "$(docker image inspect -f '{{.Id}}' "$BASE" 2>/dev/null)"); commit: $COMMIT1; без довода: $(kod) $(note '.data.suites[0].result')"

# ── 16. пользователь тестов не задан ────────────────────────────────────────
# Почему: без STACK_CHECK_UID и STACK_RUN_AS тесты ветки шли бы от root
# контейнера и писали бы в копию кода как root. Отказ ДО docker: журнал обёртки
# пуст, а слова называют оба ключа — иначе молчаливый отказ не отличить от сбоя.
echo "── 16. STACK_CHECK_UID и STACK_RUN_AS пусты → отказ словами, вызовов docker 0"
new_stand c16
branch_with "$(suite ok "$INSTALL_OK" 120 'true')"
CHECK_UID=""; RUN_AS=""
run_lib door_checks_run "$SHA" x; show
case_check "нет пользователя — код ≠0, слова о STACK_CHECK_UID и STACK_RUN_AS, вызовов docker 0" \
    "код ≠0; STACK_CHECK_UID: да; STACK_RUN_AS: да; docker: 0" \
    "код $(kod); STACK_CHECK_UID: $(has "$OUT" STACK_CHECK_UID); STACK_RUN_AS: $(has "$OUT" STACK_RUN_AS); docker: $(jcount .)"

# ── 17. STACK_RUN_AS=root ───────────────────────────────────────────────────
# Почему: пустой STACK_CHECK_UID берёт uid из STACK_RUN_AS; root дал бы тестам
# ветки uid 0. Отказ ДО docker, словами с именем ключа и root.
echo "── 17. STACK_RUN_AS=root при пустом STACK_CHECK_UID → отказ, вызовов docker 0"
new_stand c17
branch_with "$(suite ok "$INSTALL_OK" 120 'true')"
CHECK_UID=""; RUN_AS=root
run_lib door_checks_run "$SHA" x; show
case_check "root — код ≠0, слова о STACK_RUN_AS и root, вызовов docker 0" \
    "код ≠0; STACK_RUN_AS: да; root: да; docker: 0" \
    "код $(kod); STACK_RUN_AS: $(has "$OUT" STACK_RUN_AS); root: $(has "$OUT" root); docker: $(jcount .)"

# ── 17б. STACK_CHECK_UID с uid или gid 0 в любой записи ─────────────────────
# Почему: заданный ключ берётся как есть; uid 0 дал бы тестам ветки root так же,
# как STACK_RUN_AS=root. Отказ ДО docker, словами с именем ключа. Root пишется не
# только «0»: имя root, «00», и gid 0 при чужом uid (1000:0) дают группу root.
echo "── 17б. STACK_CHECK_UID = 0, 0:0, root, 00, 1000:0, 0:1000 → отказ с именем ключа, вызовов docker 0"
for UID0 in 0 0:0 root 00 1000:0 0:1000; do
    new_stand "c17u_${UID0//:/_}"
    branch_with "$(suite ok "$INSTALL_OK" 120 'true')"
    CHECK_UID="$UID0"; RUN_AS=""
    run_lib door_checks_run "$SHA" x; show
    case_check "uid $UID0 — код ≠0, слова о STACK_CHECK_UID, вызовов docker 0" \
        "код ≠0; STACK_CHECK_UID: да; docker: 0" \
        "код $(kod); STACK_CHECK_UID: $(has "$OUT" STACK_CHECK_UID); docker: $(jcount .)"
done

# ── 18. флаги контейнеров обоих видов ───────────────────────────────────────
# Почему: форк-бомба кода ветки без --pids-limit съедает сервер; --user не из
# ключа — тесты не тем пользователем; шаг с сетью ходит к живым службам. Сеть
# у установки законна, у шагов — нет (решение 3): у набора без image disconnect
# стоит в журнале ДО первого exec шага и ПОСЛЕ exec установки; у набора
# image: артефакт сети нет с самого run (--network none), установки нет вовсе
# (текст install в журнале не встречается) и disconnect ему не нужен. Сеть
# установки судится изнутри (интерфейс кроме lo), шага — тоже изнутри. pids и
# user — у каждой строки docker run набора (строк нет — провал), в двух
# прогонах: uid из STACK_CHECK_UID и при пустом — из STACK_RUN_AS=nobody.
echo "── 18. run обоих видов: pids 512, user из ключа; plain — disconnect до шага; артефакт — none при run"
new_stand c18
branch_with "$(suite plain "$INSTALL_OK && $HAS_NET" 120 "! $HAS_NET && : shag-plain")"$'\n'"$(suite art ': art-install-c18' 120 "! $HAS_NET && : shag-art"; printf '    image: артефакт\n')"
ART18="$PNAME-artifact18:${SHA:0:12}"
docker tag "$BASE" "$ART18" >/dev/null 2>&1
CHECK_UID="$(id -u):$(id -g)"
plain_order() {  # exec установки → disconnect своего контейнера → exec шага
    in_order "$(jline "^exec .*$P$RID-plain .*grep -qvx lo\$")" \
             "$(jline "^network disconnect (.* )?$P$RID-plain\$")" \
             "$(jline "^exec .*$P$RID-plain .*: shag-plain")"
}
art_flags() {  # run в none, ни установки, ни disconnect, шаг был
    local line
    line=$(jrun "$P$RID-art")
    [ "$(has_words "$line" '--network none')" = да ] \
        && [ "$(jcount 'art-install-c18')" = 0 ] \
        && [ "$(jcount "^network disconnect (.* )?$P$RID-art\$")" = 0 ] \
        && [ -n "$(jline "^exec .*$P$RID-art .*: shag-art")" ]
}
plain_net() {  # run набора без image — не в none
    local line
    line=$(jrun "$P$RID-plain")
    [ -n "$line" ] && [ "$(has_words "$line" '--network none')" = нет ]
}
run_lib door_checks_run "$SHA" x "$ART18"; show
RC1=$(kod); TESTS1=$(note .data.tests); PIDS1=$(runs_all "--pids-limit 512"); USER1=$(runs_all "--user $CHECK_UID")
PLAIN1="$(yes_no plain_net) $(yes_no plain_order)"; ART1=$(yes_no art_flags)
NOBODY="$(id -u nobody):$(id -g nobody)"
RID=c18b; CHECK_UID=""; RUN_AS=nobody
run_lib door_checks_run "$SHA" x "$ART18"; show
case_check "флаги — success; pids и user у всех run; plain: run с сетью, install → disconnect → шаг; артефакт: none, без установки; то же от nobody" \
    "код 0 success; pids: да; user: да; plain: да да; артефакт: да; код 0; user nobody: да; pids: да; plain: да да; артефакт: да" \
    "код $RC1 $TESTS1; pids: $PIDS1; user: $USER1; plain: $PLAIN1; артефакт: $ART1; код $(kod); user nobody: $(runs_all "--user $NOBODY"); pids: $(runs_all "--pids-limit 512"); plain: $(yes_no plain_net) $(yes_no plain_order); артефакт: $(yes_no art_flags)"

# ── 19–20. уборка осиротевших стендов ───────────────────────────────────────
# Почему: прогон, убитый SIGKILL, trap не убирает — его Postgres на 512 МБ живёт
# до ручной уборки. Под checks.lock своей установки чужого живого стенда с её
# префиксом нет, значит всё P*-pg/-redis и сети P* — сироты. Рядом лежат: контейнер
# своего префикса без -pg/-redis (уборка не вправе его тронуть) и стенд ДРУГОЙ
# установки other-check-<метка>-pg на том же docker — его снести значит сломать
# соседу прогон (две установки на одном docker, план фаза 3а). Сироты — на alpine:
# уборка судит имена, а не образы.
echo "── 19. сирота P-orphan (сеть + redis) до прогона → убраны со словами, -keep жив"
new_stand c19
branch_with "$(suite ok "$INSTALL_OK" 120 'true')"
ORPHAN="${P}orphan"
line_ends() { printf '%s\n' "$OUT" | awk -v t="$1" 'substr($0, length($0) - length(t) + 1) == t { f = 1 } END { exit !f }'; }
docker network create --internal "$ORPHAN" >/dev/null 2>&1
docker run -d --name "$ORPHAN-redis" --network "$ORPHAN" "$BASE" sleep 600 >/dev/null 2>&1
docker run -d --name "$ORPHAN-keep" --network none "$BASE" sleep 600 >/dev/null 2>&1
docker run -d --name "$FOREIGN" --network none "$BASE" sleep 600 >/dev/null 2>&1
BEFORE="$(running "$ORPHAN-redis" && echo да || echo нет) $(running "$ORPHAN-keep" && echo да || echo нет) $(running "$FOREIGN" && echo да || echo нет) $(docker network ls --format '{{.Name}}' | grep -cxF -- "$ORPHAN")"
run_lib door_checks_run "$SHA" x; show
ORPHAN_GONE=$(yes_no test -z "$(docker ps -a --format '{{.Names}}' | grep -xF -- "$ORPHAN-redis")")
case_check "уборка — сироты были, код 0, redis сироты убран, сеть убрана, слова об обоих, -keep жив" \
    "было: да да да 1; код 0; redis убран: да; сеть: 0; слова redis: да; слова сеть: да; keep: да" \
    "было: $BEFORE; код $(kod); redis убран: $ORPHAN_GONE; сеть: $(docker network ls --format '{{.Name}}' | grep -cxF -- "$ORPHAN"); слова redis: $(has "$OUT" "убран осиротевший стенд: $ORPHAN-redis"); слова сеть: $(yes_no line_ends "убран осиротевший стенд: $ORPHAN"); keep: $(yes_no running "$ORPHAN-keep")"
echo "── 20. стенд другой установки (other-check-<метка>-pg) после уборки жив, своя сирота убрана"
case_check "чужой префикс — сосед был, после уборки жив и не назван; своя сирота того же прогона убрана" \
    "был: да; жив: да; не назван: да; своя убрана: да" \
    "был: $(echo "$BEFORE" | awk '{print $3}'); жив: $(yes_no running "$FOREIGN"); не назван: $(lacks "$OUT" "$FOREIGN"); своя убрана: $ORPHAN_GONE"
docker rm -f "$FOREIGN" >/dev/null 2>&1

# ── 21. живой журнал не тронут ──────────────────────────────────────────────
# Почему: проба, что пишет записку в живой /var/log/harness, подложила бы выкату
# прода чужое «зелено». Контроль: записки в журналах стендов есть — иначе «живой
# не тронут» прошло бы и у библиотеки, что не пишет ничего.
echo "── 21. записки легли в стенды, живой deploy-checks.jsonl не изменился"
case_check "журналы — записки стендов есть, живой журнал прежнего размера" \
    "записки стендов: да; живой: прежний" \
    "записки стендов: $(yes_no test "$(cat "$T"/c*/zhurnal/deploy-checks.jsonl 2>/dev/null | grep -c '"deploy.checks"')" -ge 10); живой: $([ "$(stat -c %s "$LIVE_NOTES" 2>/dev/null || echo нет)" = "$LIVE_BEFORE" ] && echo прежний || echo изменился)"

# ── 22. копии и логи не в живых каталогах ───────────────────────────────────
# Почему: двери DEPLOY_CHECK_WORK_ROOT и DEPLOY_CHECK_LOG_ROOT — единственное, что
# отделяет пробу от каталогов прода; лог мимо двери лёг бы в живой каталог.
# Лог и копия несут sha8 коммита стенда: в живых каталогах их быть не должно.
# Контроль — логи наборов стендов есть.
echo "── 22. логи наборов — в каталогах стендов, в живых каталогах sha пробы нет"
stand_logs() { find "$T" -path '*/logi/*.log' -type f | grep -c .; }
live_logs() { find /var/log/harness/door-checks /var/tmp/door-checks -maxdepth 1 2>/dev/null | grep -cFf "$T/sha8"; }
case_check "логи — в стендах есть, в /var/log/harness/door-checks и /var/tmp/door-checks sha пробы нет" \
    "в стендах: да; в живом: 0" \
    "в стендах: $(yes_no test "$(stand_logs)" -ge 1); в живом: $(live_logs)"

# ── 34. записка несёт настоящий sha, не маску ───────────────────────────────
# Почему: маска событий (events.py) принимает 40- и 64-значный hex за секрет и
# пишет «<СКРЫТО>», если ключ не в DIGEST_KEYS. Записка со скрытым sha не
# совпадёт ни с одной тройкой — door_checks_cached не найдёт её никогда, и каждый
# выкат гоняет наборы заново (решение управляющего 16.11 03:25, п. 2). Судится
# итог в журнале стенда (HARNESS_LOG_DIR=$S/zhurnal): поля по форме и по
# значению; ни в одной записке стендов нет «<СКРЫТО>» (контроль — записки есть);
# строка искажения (kind mangled) в $S/zhurnal/.events/lost.jsonl не легла.
echo "── 34. записка случая 1: data.sha — 40 hex = sha ветки, checks_file_sha — 64 hex = хэш файла, маски нет"
S=$C01_S
all_notes() { cat "$T"/c*/zhurnal/deploy-checks.jsonl 2>/dev/null; }
mangled_rows() { cat "$T"/c*/zhurnal/.events/lost.jsonl 2>/dev/null | grep -F '"deploy-checks"' | grep -cF '"mangled"'; }
case_check "sha не скрыт — sha 40 hex и свой, checks_file_sha 64 hex и свой, записок стендов есть, «<СКРЫТО>» 0, искажений 0" \
    "sha: да да; checks_file_sha: да да; записок: да; скрыто: 0; искажений: 0" \
    "sha: $(yes_no grep -qxE '[0-9a-f]{40}' <<<"$(note .data.sha)") $(yes_no test "$(note .data.sha)" = "$C01_SHA"); checks_file_sha: $(yes_no grep -qxE '[0-9a-f]{64}' <<<"$(note .data.checks_file_sha)") $(yes_no test "$(note .data.checks_file_sha)" = "$C01_CHECKS_SHA"); записок: $(yes_no test "$(all_notes | grep -c '"deploy.checks"')" -ge 1); скрыто: $(all_notes | grep -cF '<СКРЫТО>'); искажений: $(mangled_rows)"

# ── 35. учётка стенда: незнакомое слово и значение не из букв ───────────────
# Почему: STACK_STAND_PG — контракт с тестами копии (имя, база, пароль pg стенда).
# Опечатка «usr=» иначе молча оставила бы умолчание, и тесты падали бы на входе в
# базу уже после сборки; значение с «;» или пробелом — не имя учётки. Отказ до
# docker, ключ и слово названы, значение (это может быть пароль) в выводе не звучит.
echo "── 35. STACK_STAND_PG: незнакомое слово, значение с «;» → отказ до docker, ключ назван, значение не напечатано"
for PGW in "user=cfu usr=cfx" "password=zz;yy"; do
    new_stand "c35_${#PGW}"
    branch_with "$(suite ok "$INSTALL_OK" 120 'true')"
    STAND_PG="$PGW"
    run_lib door_checks_run "$SHA" x; show
    case_check "STACK_STAND_PG «$PGW» — код ≠0, ключ назван, значение не напечатано, вызовов docker 0" \
        "код ≠0; ключ: да; значение скрыто: да; docker: 0" \
        "код $(kod); ключ: $(has "$OUT" STACK_STAND_PG); значение скрыто: $(lacks "$OUT" "${PGW##*=}"); docker: $(jcount .)"
done

# ── 36. учётка стенда из ключа: pg с ней, pg_isready с ней, тесты видят её ───
# Почему: тесты копии входят в pg стенда своей учёткой; вшитая app_user/app_test
# роняла бы их на входе. Службы стенда подставные (их образов на сервере может не
# быть): судится, с чем дверь их зовёт и что видит шаг. Сеть стенда и контейнер
# набора — настоящие.
echo "── 36. STACK_STAND_PG=user=cfu db=cfd password=cfp → run pg и pg_isready с ней, шаг видит PG_USER/PG_DB/PG_PASSWORD"
new_stand c36
branch_with "$(suite pgacc "$INSTALL_OK" 120 'env | grep ^INTEGRATION_TEST_PG_ | sort'; printf '    stand: pg-redis\n')"
printf '^run (.* )?--name %s(-pg|-redis) \n^exec (.* )?%s-pg pg_isready\n' "$P$RID" "$P$RID" > "$S/docker.ok"
STAND_PG="user=cfu db=cfd password=cfp"
run_lib door_checks_run "$SHA" x; show
LOG36="$S/logi/${SHA:0:8}-pgacc.log"
PG36=$(jrun "$P$RID-pg")
ISR36=$({ cat "$(DLOG)" 2>/dev/null || true; } | grep -E -- "^exec (.* )?$P$RID-pg pg_isready" | head -n 1)
case_check "учётка из ключа — код 0, pg с ней, pg_isready с ней, шаг видит её окружением" \
    "код 0; pg: да; pg_isready: да; шаг: да да да" \
    "код $(kod); pg: $(has_words "$PG36" "-e POSTGRES_USER=cfu" "-e POSTGRES_PASSWORD=cfp" "-e POSTGRES_DB=cfd"); pg_isready: $(has_words "$ISR36" "-U cfu" "-d cfd"); шаг: $(yes_no grep -qsxF INTEGRATION_TEST_PG_USER=cfu "$LOG36") $(yes_no grep -qsxF INTEGRATION_TEST_PG_DB=cfd "$LOG36") $(yes_no grep -qsxF INTEGRATION_TEST_PG_PASSWORD=cfp "$LOG36")"
for name in $(docker network ls --format '{{.Name}}' | grep -E "^$P$RID\$"); do docker network rm "$name" >/dev/null 2>&1; done

fi

# ── стенд pg-redis ─────────────────────────────────────────────────────────
STAND_CASE_NAMES=(23 24 25 26 27 28 29 30 31 32 33)
nets() { docker network ls --format '{{.Name}}' | grep -cxF -- "$P$RID"; }
stand_suite() { suite "$1" "$INSTALL_OK" "${4:-120}" "$3"; printf '    stand: %s\n' "$2"; }
env_in() {
    local want
    for want in INTEGRATION_TEST_PG_HOST=pg INTEGRATION_TEST_PG_PORT=5432 \
                INTEGRATION_TEST_REDIS_HOST=stand-kv INTEGRATION_TEST_REDIS_PORT=6379; do
        grep -qsxF -- "$want" "$1" || return 1
    done
}
step_runs() { jcount "^run (.* )?--name $P$RID-pgr( |\$)"; }

if [ -n "$BASE_MISSING$STAND_MISSING" ]; then
    for n in "${STAND_CASE_NAMES[@]}"; do skip_case "$n" "${BASE_MISSING:-$STAND_MISSING}"; done
else

# ── 23. стенд pg-redis поднялся ─────────────────────────────────────────────
# Почему: тесты стенда ходят в настоящие Postgres и Redis. Набор сам доказывает
# изнутри: адреса пришли окружением с префиксом STACK_STAND_ENV_PREFIX, порты
# открыты, наружу сети нет (--internal).
echo "── 23. stand: pg-redis → набор видит pg и redis по окружению, наружу не выходит, passed"
new_stand c23
STAND_CMD='env | grep ^INTEGRATION_TEST_ | sort; nc -z -w 5 "$INTEGRATION_TEST_REDIS_HOST" "$INTEGRATION_TEST_REDIS_PORT" && nc -z -w 5 "$INTEGRATION_TEST_PG_HOST" "$INTEGRATION_TEST_PG_PORT" && ! wget -T 3 -q -O /dev/null http://1.1.1.1 && echo STAND-OK'
branch_with "$(stand_suite pgr pg-redis "$STAND_CMD")"
run_lib door_checks_run "$SHA" x; show
LOG="$S/logi/${SHA:0:8}-pgr.log"
C23_S=$S; C23_RID=$RID
case_check "стенд — код 0, tests=success, набор passed, изнутри видны redis и pg, наружу нет, адреса из окружения" \
    "код 0; tests: success; набор: passed; стенд виден: да; env: да" \
    "код $(kod); tests: $(note .data.tests); набор: $(note '.data.suites[0].result'); стенд виден: $(yes_no grep -qsx STAND-OK "$LOG"); env: $(yes_no env_in "$LOG")"

# ── 24. как стенд поднят ────────────────────────────────────────────────────
# Почему: сервер переподписан — потолки памяти служб обязательны; образы — из
# STACK_STAND_IMAGES, а не зашитые; без pg_isready тесты бьются в недоготовый pg.
# Контейнер набора (решение управляющего 16.11 03:25, п. 3; 03:45): run в bridge
# (установке нужна сеть) → install через exec → network disconnect bridge →
# network connect P<id> (сеть стенда --internal) → шаги через exec. Прямой
# run --network P<id> установку бы лишил сети.
echo "── 24. журнал случая 23: сеть --internal, pg 512m и redis 128m из ключа, pg_isready, набор: run в bridge → disconnect bridge → connect сети стенда"
PG=$(jrun "$P$RID-pg"); RD=$(jrun "$P$RID-redis"); ST=$(jrun "$P$RID-pgr")
L_RUN=$(jline "^run (.* )?--name $P$RID-pgr( |\$)")
L_DISC=$(jline "^network disconnect (.* )?bridge $P$RID-pgr( |\$)")
L_CONN=$(jline "^network connect (.* )?$P$RID $P$RID-pgr( |\$)")
case_check "журнал стенда — сеть создана, pg и redis по контракту и из ключа, pg_isready спрошен, набор стартовал в bridge, затем disconnect bridge и connect сети стенда по порядку" \
    "сеть: да; pg: да; redis: да; pg_isready: да; run не в сети стенда и не в none: да; порядок run-disconnect-connect: да" \
    "сеть: $(yes_no test "$(jcount "^network create (.* )?--internal (.* )?$P$RID\$")" -ge 1); pg: $(has_words "$PG" "$PG_IMG" "--memory 512m" "--network $P$RID" "--network-alias pg" "-e POSTGRES_USER=app_user" "-e POSTGRES_PASSWORD=test" "-e POSTGRES_DB=app_test"); redis: $(has_words "$RD" "$REDIS_IMG" "--memory 128m" "--network $P$RID" "--network-alias stand-kv"); pg_isready: $(yes_no test "$(jcount "^exec (.* )?$P$RID-pg pg_isready")" -ge 1); run не в сети стенда и не в none: $(yes_no test -n "$ST" -a "$(has_words "$ST" '--network none')" = нет -a "$(has_words "$ST" "--network $P$RID")" = нет); порядок run-disconnect-connect: $(yes_no in_order "$L_RUN" "$L_DISC" "$L_CONN")"

# ── 25. уборка после зелёного набора со стендом ─────────────────────────────
echo "── 25. после случая 23: сетей P<id> 0, контейнеров 0, network rm и rm -f служб в журнале"
case_check "уборка после passed — сетей 0, контейнеров 0, network rm, rm -f pg и redis" \
    "сетей: 0; контейнеров: 0; network rm: да; rm pg: да; rm redis: да" \
    "сетей: $(nets); контейнеров: $(leftovers); network rm: $(yes_no test "$(jcount "^network rm (.* )?$P$RID( |\$)")" -ge 1); rm pg: $(yes_no test "$(jcount "^rm -f (.* )?$P$RID-pg( |\$)")" -ge 1); rm redis: $(yes_no test "$(jcount "^rm -f (.* )?$P$RID-redis( |\$)")" -ge 1)"

# ── 26. уборка после красного набора со стендом ─────────────────────────────
# Почему: уборка только на зелёном пути оставляла бы стенд ровно тогда, когда
# ветку будут чинить и гонять снова. Контроль «сеть была».
echo "── 26. stand: pg-redis, cmd false → failed, сеть была, после — сетей 0, контейнеров 0"
new_stand c26
branch_with "$(stand_suite pgr pg-redis 'false')"
run_lib door_checks_run "$SHA" x; show
case_check "стенд и failed — код ≠0, tests=failure, набор failed, сеть была, сетей 0, контейнеров 0" \
    "код ≠0; tests: failure; набор: failed; сеть была: да; сетей: 0; контейнеров: 0" \
    "код $(kod); tests: $(note .data.tests); набор: $(note '.data.suites[0].result'); сеть была: $(yes_no test "$(jcount "^network create (.* )?$P$RID\$")" -ge 1); сетей: $(nets); контейнеров: $(leftovers)"

# ── 27. сеть стенда не создалась ────────────────────────────────────────────
echo "── 27. docker network create отказал → error «стенд не поднялся», шаг не запускался, убрано"
new_stand c27
branch_with "$(stand_suite pgr pg-redis 'true')"
printf '^network create\n' > "$S/docker.fail"
run_lib door_checks_run "$SHA" x; show
case_check "сеть не создана — код ≠0, tests=error, набор error, слова, попытка была, шаг 0, контейнеров 0, сетей 0" \
    "код ≠0; tests: error; набор: error; слова: да; попытка: да; шаг: 0; контейнеров: 0; сетей: 0" \
    "код $(kod); tests: $(note .data.tests); набор: $(note '.data.suites[0].result'); слова: $(has "$OUT" 'стенд не поднялся'); попытка: $(yes_no test "$(jcount '^network create')" -ge 1); шаг: $(step_runs); контейнеров: $(leftovers); сетей: $(nets)"

# ── 28. Postgres не готов до срока ──────────────────────────────────────────
echo "── 28. pg_isready всегда «не готов», ожидание 3 с → error «стенд не поднялся», повтор, убрано"
new_stand c28
branch_with "$(stand_suite pgr pg-redis 'true')"
printf '^exec .*pg_isready\n' > "$S/docker.fail"
STAND_WAIT=3
run_lib door_checks_run "$SHA" x; show
case_check "pg не готов — код ≠0, tests=error, слова, pg был, pg_isready ≥2 раз, ≤60 с, шаг 0, контейнеров 0, сетей 0" \
    "код ≠0; tests: error; слова: да; pg был: да; повтор: да; не дольше 60 с: да; шаг: 0; контейнеров: 0; сетей: 0" \
    "код $(kod); tests: $(note .data.tests); слова: $(has "$OUT" 'стенд не поднялся'); pg был: $(yes_no test -n "$(jrun "$P$RID-pg")"); повтор: $(yes_no test "$(jcount "^exec (.* )?$P$RID-pg pg_isready")" -ge 2); не дольше 60 с: $(yes_no test "$SECS" -le 60); шаг: $(step_runs); контейнеров: $(leftovers); сетей: $(nets)"

# ── 29. готовность Postgres по TCP и swap служб (журнал случая 23) ─────────
# Почему: временный сервер initdb слушает только сокет — pg_isready без -h даёт
# ложное «готов»; --memory без --memory-swap даёт службе ещё столько же swap.
echo "── 29. журнал случая 23: каждый pg_isready с -h 127.0.0.1; --memory-swap = --memory"
S=$C23_S; RID=$C23_RID
ALL=$(jcount "^exec (.* )?$P$RID-pg pg_isready( |\$)")
TCP=$(jcount "^exec (.* )?$P$RID-pg pg_isready (.* )?-h 127\\.0\\.0\\.1( |\$)")
PG=$(jrun "$P$RID-pg"); RD=$(jrun "$P$RID-redis")
case_check "pg_isready по TCP и swap — вызовы были, все с -h 127.0.0.1; pg и redis с --memory-swap" \
    "вызовы: да; все по TCP: да; pg swap: да; redis swap: да" \
    "вызовы: $(yes_no test "$ALL" -ge 1); все по TCP: $(yes_no test "$ALL" -ge 1 -a "$TCP" = "$ALL"); pg swap: $(has_words "$PG" "--memory 512m" "--memory-swap 512m"); redis swap: $(has_words "$RD" "--memory 128m" "--memory-swap 128m")"

# ── 30. зависший docker exec pg_isready ─────────────────────────────────────
echo "── 30. docker exec pg_isready висит, потолок exec 2 с, срок 3 с → error ≤18 с, убрано"
new_stand c30
branch_with "$(stand_suite pgr pg-redis 'true')"
printf '^exec .*pg_isready\n' > "$S/docker.hang"
STAND_WAIT=3; EXEC_WAIT=2; RUN_LIMIT=90
run_lib door_checks_run "$SHA" x; show
END=$(date +%s); HUNG=$(head -n 1 "$S/docker.hang.t" 2>/dev/null)
case_check "exec висит — код ≠0, tests=error, слова, exec висел, ≤18 с от первого зависания, шаг 0, контейнеров 0, сетей 0" \
    "код ≠0; tests: error; слова: да; висел: да; не дольше 18 с: да; шаг: 0; контейнеров: 0; сетей: 0" \
    "код $(kod); tests: $(note .data.tests); слова: $(has "$OUT" 'стенд не поднялся'); висел: $(yes_no test -n "$HUNG"); не дольше 18 с: $(yes_no test -n "$HUNG" -a $(( END - ${HUNG:-0} )) -le 18); шаг: $(step_runs); контейнеров: $(leftovers); сетей: $(nets)"

# ── 31. stand: none не поднимает стенд ──────────────────────────────────────
echo "── 31. stand: none → шаг с --network none, сети и служб стенда нет"
new_stand c31
branch_with "$(stand_suite nonet none 'true')"
run_lib door_checks_run "$SHA" x; show
ST=$(jrun "$P$RID-nonet")
case_check "stand none — код 0, шаг в --network none, network create 0, pg не запускался" \
    "код 0; шаг в none: да; network create: 0; pg: нет" \
    "код $(kod); шаг в none: $(has_words "$ST" '--network none'); network create: $(jcount '^network create'); pg: $(yes_no test -n "$(jrun "$P$RID-pg")")"

# ── 32. адреса стенда мимо предохранителя продукта ──────────────────────────
# Почему: предохранитель тестов продукта отказывает на адресе с боевой подстрокой
# (redis, vault, …): алиас «redis» ронял весь набор стенда на живом прогоне CF.
echo "── 32. журнал случая 23: ни env шага, ни алиасы стенда не содержат боевых подстрок"
S=$C23_S; RID=$C23_RID
GUARD=$(printf '%s\n' app-postgres redis vault rabbitmq)
ST=$(jrun "$P$RID-pgr")
VALS=$(printf ' %s ' "$ST" | grep -oE -- ' -e [A-Za-z_]+=[^ ]*' | sed 's/^ -e [^=]*=//')
ALIASES=$({ cat "$(DLOG)" 2>/dev/null || true; } | grep -oE -- '--network-alias [^ ]+' | sed 's/^--network-alias //')
hits() { printf '%s\n' "$1" | grep -F -- "$GUARD" | sort -u | tr '\n' ' '; }
case_check "мимо предохранителя — env шага и алиасы есть и чисты" \
    "env шага: да; алиасы: да; боевое в env: ; боевое в алиасах: " \
    "env шага: $(yes_no test -n "$VALS"); алиасы: $(yes_no test "$(printf '%s\n' "$ALIASES" | grep -c .)" -ge 2); боевое в env: $(hits "$VALS"); боевое в алиасах: $(hits "$ALIASES")"

# ── 33. образ стенда не из ключа ────────────────────────────────────────────
# Почему: образ стенда, зашитый в коде, а не взятый из STACK_STAND_IMAGES (с
# digest), разъедется с тем, что продукт закрепил. Ключ называет несуществующий
# образ — стенд не поднимается (error), и docker run pg несёт именно его. Обёртка
# отказывает вызову с этим образом сама: настоящий docker полез бы его тянуть.
echo "── 33. STACK_STAND_IMAGES с несуществующим pg → error «стенд не поднялся», run pg с образом ключа"
new_stand c33
branch_with "$(stand_suite pgr pg-redis 'true')"
STAND_IMAGES_SAVED=$STAND_IMAGES
STAND_IMAGES="pg=$PNAME-net-takogo:0 redis=$REDIS_IMG"
printf 'net-takogo\n' > "$S/docker.fail"
run_lib door_checks_run "$SHA" x; show
STAND_IMAGES=$STAND_IMAGES_SAVED
case_check "образ из ключа — код ≠0, tests=error, слова, run pg с образом ключа, контейнеров 0, сетей 0" \
    "код ≠0; tests: error; слова: да; образ ключа: да; контейнеров: 0; сетей: 0" \
    "код $(kod); tests: $(note .data.tests); слова: $(has "$OUT" 'стенд не поднялся'); образ ключа: $(has_words "$(jrun "$P$RID-pg")" "$PNAME-net-takogo:0"); контейнеров: $(leftovers); сетей: $(nets)"

fi

echo "DOOR-CHECKS: случаев $CASES_PATHS, неудач $CASES_FAILED, пропущено $SKIPPED${SKIP_WHY:+: нет образа $SKIP_WHY}"
[ "$CASES_FAILED" = 0 ]
