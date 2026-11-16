#!/usr/bin/env bash
# Проба: выкат продукта — ОДНА цепочка, и тесты продукта на сервере (этап T) — её этап до
# лока и ворот, а не второй выкат сбоку (спека эталонного выката §5.1, §5.3 п. 3–8, §7
# стр. 3; план, фаза 3б). Перепись CF test_deploy_server.sh (патч 16d0ced) с режима
# server на общую цепочку deploy.sh.
#
# Что охраняет. Цепочка §5.1: S предмет → X секреты → B сборка :sha12 → T тесты (кэш по
# записке) → G лок+ворота → грязь прода → K дамп → R тег(+пуш) → грязь прода под локом →
# P шаги прода (потолки STACK_STEP_TIMEOUTS, от STACK_RUN_AS) → M живость+смоук → [O откат].
# Тесты красные или «не отработали» — отказ ДО лока, ворот и тега: прод и хранилище не
# тронуты. `--проверка` — та же цепочка, обрезанная после T: записка, без лока, ворот,
# тега, :latest и служб. Выкат того же sha читает записку: ни наборов, ни сборки.
#
# Контракт, который проба ждёт от кода фазы 3б (слова и имена — её решения):
#   - отказ по тестам: «тесты красные» (failure) и «тесты не отработали» (error) — разные
#     слова, слово одного исхода не звучит в другом (§5.3 п. 3);
#   - STACK_RUN_AS = root или пользователь агента → отказ до тестов с именем ключа; пусто —
#     шаги от агента, как сейчас;
#   - STACK_RELEASE_CMD пуст, а STACK_BUILD_CMD поднимает службы (`up -d`, `up --build`) —
#     отказ до сборки с именами обоих ключей (Р-1: сборка идёт до «Да»);
#   - STACK_CHECKS_FILE пуст — строка «… STACK_CHECKS_FILE … пропущен: не задано», выкат идёт;
#   - B не повторяется, только когда записка success на (sha, хэш файла проверок) есть И
#     образ :<sha12> есть; без записки (наборов нет) сборка идёт, как сейчас;
#   - шаг прода сверх потолка → «ВЫКАТ ПРОВАЛЕН, стек на прошлой версии <тег живого выката>»;
#     имя шага пересоздания служб в STACK_STEP_TIMEOUTS — release (как у CF);
#   - смоук (STACK_SMOKE_CMD) упал при живой ручке → «выкачено с оговоркой: смоук», код 0;
#     упал, и ручка мертва → откат и «ВЫКАТ ПРОВАЛЕН» с тегом прошлой версии, код 1;
#   - STACK_ROLLBACK_CMD задан → откат идёт им вместо ретега :latest, и IMAGE_TAG проваленного
#     выката он не видит; шаг миграций сверх потолка → откат на живой образ, «ВЫКАТ ПРОВАЛЕН»
#     с тегом прошлой версии словами и в журнале итогов (раунд ревью кода 3б);
#   - грязь прода (PRODUCT_PATH ≠ PRODUCT_REPO), появившаяся после дампа, → отказ, файл
#     назван, тега в хранилище нет, дамп остаётся; итог в журнале итогов — со словом «отказ»,
#     не «ВЫКАТ ПРОВАЛЕН» (раунд ревью 35, М2);
#   - ревью 35: up/start/restart/run compose в STACK_BUILD_CMD или STACK_BUILD_NOCACHE_CMD —
#     отказ до сборки при любом STACK_RELEASE_CMD и под --проверка (К1); ключи тестового этапа
#     и `sudo -n -u STACK_RUN_AS` судятся до сборки (В4, М3); «не отработали» судится по
#     записке своего sha (В2); proxy и подъём отката — под потолком, ни один шаг не держит
#     fd замков (В3); SIGTERM — итог называет 143, шаг прода не переживает дверь (М1).
# Имена кода для мутаций (harness/config/probe-mutations.yaml, записи этой пробы): строка
# `stage_tests` в deploy.sh (вызов этапа T) стоит до строки `take_deploy_lock`; функции
# door_stages.sh `door_run_as_ok` (отказ root/агента) и `prod_smoke` (шаг смоука).
#
# Стенд свой (как у test_deploy_product.sh и test_deploy_artifact.sh): паспорт, харнес,
# продукт и голое хранилище продукта в tmp; копия scripts/ с воротами-заглушкой (открывают
# всегда и пишут, как их звали: проба судит порядок этапов, а не кнопку — кнопку судит
# test_deploy_product.sh). Docker подставной С ПАМЯТЬЮ образов: build даёт новый id, tag
# переносит id; run запоминает контейнер; exec исполняет команду шага набора на хосте
# (`sh -c`), поэтому зелёный, красный и зависший набор настоящие по коду и потолку, а
# cgroup, сеть и --pids-limit судит test_door_checks.sh на настоящем docker. Контейнеров,
# сетей и образов docker проба не создаёт. sudo подставной и отказывает: шагам стенда
# (STACK_RUN_AS пуст) он не нужен. Боевой паспорт не читается: дверь — копия вне
# PROJECT_DIR машины, паспорт ей называет HARNESS_DOOR_STAND_HOME.
#
#   bash scripts/test_deploy_stages.sh        (только фоном)
# Итог: «ЭТАПЫ-ВЫКАТА: путей N, неудач M»; код 0 — неудач нет.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
paths=0
fails=0

itog() {  # $1=ждём $2=вышло $3=имя
    paths=$((paths + 1))
    if [ "$1" = "$2" ]; then printf '  ок    %s\n' "$3"
    else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$3" "$1" "$2"; fails=$((fails + 1)); fi
}
# here-string, не конвейер: grep -q выходит на первом совпадении, и printf в конвейере под
# pipefail получал SIGPIPE (141) — «нет» на совпавшем тексте (флейк под нагрузкой).
has() { grep -qF -- "$2" <<<"$1" && echo да || echo нет; }

STAND=$(mktemp -d -t proba-deploy-stages-XXXXXX)
trap 'rm -rf "$STAND"' EXIT

HARNESS="$STAND/harnes"
PRODUCT="$STAND/produkt"
BARE="$STAND/hranilishche.git"
PROD_TREE="$STAND/prod"
FAKEBIN="$STAND/bin"
COPY="$STAND/scripts"
mkdir -p "$HARNESS" "$PRODUCT" "$FAKEBIN" "$STAND/zhurnal" "$STAND/sekrety" "$COPY"
cat > "$STAND/install.conf" <<CONF
PROJECT_NAME="proba"
PROJECT_DIR="$HARNESS"
LOG_DIR="$STAND/zhurnal"
SECRETS_DIR="$STAND/sekrety"
TMUX_SESSION="net-takoj"
AGENT_START_CMD="true"
AUTONOMY="semi"
HEARTBEAT_DIR="$STAND/zhurnal/hb"
CONF

g() { git -c user.email="proba@stend" -c user.name="проба" "$@"; }
printf 'стенд\n' > "$HARNESS/README"
git -C "$HARNESS" init -q -b main
g -C "$HARNESS" add -A
g -C "$HARNESS" commit -qm "стенд"
mkdir -p "$PRODUCT/web"
printf 'стенд\n' > "$PRODUCT/README"
printf 'витрина\n' > "$PRODUCT/web/a.txt"
git -C "$PRODUCT" init -q -b main
g -C "$PRODUCT" add -A
g -C "$PRODUCT" commit -qm "стенд"
git init -q --bare -b main "$BARE"
git -C "$PRODUCT" remote add origin "$BARE"
git -C "$PRODUCT" push -q origin main
bump() { g -C "$PRODUCT" commit -q --allow-empty -m "$1"; }

# Подставной docker с памятью: «образ:тег id» в $DOCKER_STATE, контейнеры «имя образ» в
# $DOCKER_STATE.cont. Ссылка без тега — :latest, как у docker.
cat > "$FAKEBIN/docker" <<'SH'
#!/usr/bin/env bash
state="$DOCKER_STATE"
touch "$state"
printf '%s\n' "$*" >> "${DOCKER_JOURNAL:-/dev/null}"
norm() { case "${1##*/}" in *:*) printf '%s' "$1" ;; *) printf '%s:latest' "$1" ;; esac; }
# Ссылка «sha256:…» — id образа, как у docker: откат по id ищет образ, а не тег.
get() {
    case "$1" in
        sha256:*) awk -v i="$1" '$2 == i { print i; exit }' "$state" ;;
        *) awk -v r="$(norm "$1")" '$1 == r { id = $2 } END { if (id) print id }' "$state" ;;
    esac
}
put() {
    local r; r=$(norm "$1")
    awk -v r="$r" '$1 != r' "$state" > "$state.new"; printf '%s %s\n' "$r" "$2" >> "$state.new"
    mv "$state.new" "$state"
}
# image rm снимает метку (как docker: образ с другой меткой остаётся под ней).
drop() {
    local r; r=$(norm "$1")
    awk -v r="$r" '$1 != r' "$state" > "$state.new"; mv "$state.new" "$state"
}
inspect() {  # [--format F | -f F | --format=F] ссылка…
    local fmt="" ref id
    while [ $# -gt 0 ]; do
        case "$1" in
            --format|-f) fmt="$2"; shift 2 ;;
            --format=*) fmt="${1#--format=}"; shift ;;
            --type=*) shift ;;
            *) break ;;
        esac
    done
    [ $# -gt 0 ] || { echo "inspect: нет ссылки" >&2; exit 1; }
    for ref in "$@"; do
        id=$(get "$ref")
        [ -n "$id" ] || { echo "Error: No such image: $ref" >&2; exit 1; }
        case "$fmt" in
            "") printf '[{"Id": "%s", "RepoTags": ["%s"]}]\n' "$id" "$(norm "$ref")" ;;
            *) printf '%s\n' "$id" ;;
        esac
    done
}
case "${1:-}" in
    version) exit 0 ;;
    build)
        shift
        n=$(( $(cat "$state.n" 2>/dev/null || echo 0) + 1 )); printf '%s\n' "$n" > "$state.n"
        id="sha256:sborka-$n"; tagged=0
        while [ $# -gt 0 ]; do
            case "$1" in
                -t|--tag) put "$2" "$id"; tagged=1; shift 2 ;;
                --tag=*) put "${1#--tag=}" "$id"; tagged=1; shift ;;
                *) shift ;;
            esac
        done
        [ "$tagged" = 1 ] || { echo "build: стенд ждёт -t <образ:тег>" >&2; exit 1; }
        exit 0 ;;
    tag)
        id=$(get "$2"); [ -n "$id" ] || { echo "Error: No such image: $2" >&2; exit 1; }
        put "$3" "$id"; exit 0 ;;
    image)
        case "${2:-}" in
            inspect) shift 2; inspect "$@"; exit 0 ;;
            rm) shift 2; for r in "$@"; do [ "${r#-}" != "$r" ] || drop "$r"; done; exit 0 ;;
            *) exit 0 ;;
        esac ;;
    inspect) shift; inspect "$@"; exit 0 ;;
    run)
        # Образ контейнера набора стоит перед командой жизни «sleep N».
        name=""; image=""; prev=""
        for a in "$@"; do
            [ "$prev" = --name ] && name=$a
            [ "$a" = sleep ] && image=$prev
            prev=$a
        done
        printf '%s %s\n' "$name" "$image" >> "$state.cont"
        echo "kontejner-stenda"
        exit 0 ;;
    # Команда шага — последний довод (`… <контейнер> sh -c <команда>`): исполняется здесь же,
    # и потолок timeout двери снимает её как снял бы клиент docker.
    exec) exec sh -c "${*: -1}" ;;
    *) exit 0 ;;
esac
SH
chmod +x "$FAKEBIN/docker"
cat > "$FAKEBIN/sudo" <<'SH'
#!/usr/bin/env bash
printf 'sudo %s\n' "$*" >> "${DOCKER_JOURNAL:-/dev/null}"
echo "sudo стенда: шагам стенда не нужен" >&2
exit 1
SH
chmod +x "$FAKEBIN/sudo"
STATE="$STAND/docker-state"
: > "$STATE"
id_of() { PATH="$FAKEBIN:$PATH" DOCKER_STATE="$STATE" docker image inspect --format '{{.Id}}' "$1" 2>/dev/null || true; }

cp "$HERE/deploy.sh" "$HERE/check-secrets.sh" "$COPY/"
cp -r "$HERE/lib" "$COPY/"
cat > "$COPY/deploy_guard.py" <<'PY'
# Ворота-заглушка пробы этапов: открывают всегда и пишут, как их звали.
import os, sys
with open(os.environ["PROBA_GATE_ARGV"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
print("ворота-заглушка: открыто")
PY

# Сборка кладёт образ по IMAGE_TAG (контракт с compose продукта, фаза 2). Одинарные кавычки
# в конфиге: ${IMAGE_TAG} доходит до сборки, а не раскрывается при чтении конфига.
BUILD="docker build -t proba-api:\${IMAGE_TAG:-latest} . && echo sborka >> $STAND/poryadok"
CHECKS_KEY='STACK_CHECKS_FILE="deploy/server-checks.yaml"'
conf() {  # $1 — добавочные строки (позже — старше: конфиг исполняется как код)
    {
        printf 'PRODUCT_DIR="proba"\nPRODUCT_REPO="%s"\nBACKUP_DIR="%s/kopii"\n' "$PRODUCT" "$STAND"
        printf 'DEPLOY_HEALTH_WAIT_SEC="4"\nSTACK_IMAGES="proba-api"\nSTACK_HEALTH_CMD="true"\n'
        printf "STACK_BUILD_CMD='%s'\n" "$BUILD"
        printf 'STACK_UP_CMD="echo otkat >> %s/poryadok"\n' "$STAND"
        printf 'STACK_MIGRATE_CMD="echo migracii >> %s/poryadok"\n' "$STAND"
        printf 'STACK_RELEASE_CMD="echo peresozdanie >> %s/poryadok"\n' "$STAND"
        printf 'STACK_CHECK_MIN_FREE_MB="4096"\nSTACK_CHECK_UID="%s"\n' "$(id -u):$(id -g)"
        printf '%s\n' "${1:-}"
    } > "$STAND/harness.conf"
}

# Файл проверок продукта: один набор «sloj» на базе (без image), шаги — доводы.
# Команды в кавычках: голые true/false YAML прочёл бы как булевы.
checks_commit() {  # $1 — timeout_s, дальше — команды шагов
    local limit=$1 cmd
    shift
    mkdir -p "$PRODUCT/deploy"
    {
        printf "suites:\n  - name: sloj\n    base: alpine:3.20\n    install: 'true'\n    manifests: []\n"
        printf '    timeout_s: %s\n    steps:\n' "$limit"
        for cmd in "$@"; do printf "      - cmd: '%s'\n" "$cmd"; done
    } > "$PRODUCT/deploy/server-checks.yaml"
    g -C "$PRODUCT" add -A
    g -C "$PRODUCT" commit -qm "проверки: $*"
}
printf 'MemTotal:       65536000 kB\nMemFree:        1000 kB\nMemAvailable:   %s kB\n' \
    "$((20000 * 1024))" > "$STAND/meminfo"

VYVOD=""
RC=0
WALL=0
LOCKF="$STAND/zhurnal/locks/deploy.lock"
run_deploy() {  # $@ — ключи deploy.sh; журналы docker, ворот, порядка и лок — на этот запуск
    : > "$STAND/gate-argv"; : > "$STAND/poryadok"; : > "$STAND/docker.log"; : > "$STATE.cont"
    rm -f "$LOCKF"
    sleep 1   # тег выката именуется по секунде
    local t0=$SECONDS
    VYVOD=$(PATH="$FAKEBIN:$PATH" DOCKER_STATE="$STATE" DOCKER_JOURNAL="$STAND/docker.log" \
            PROBA_GATE_ARGV="$STAND/gate-argv" HARNESS_DOOR_STAND_HOME="$STAND" \
            HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
            DEPLOY_CHECK_MEMINFO="$STAND/meminfo" DEPLOY_CHECK_WORK_ROOT="$STAND/kopii-proverok" \
            DEPLOY_CHECK_LOG_ROOT="$STAND/logi-proverok" DEPLOY_CHECK_MEM_WAIT_SEC=5 \
            timeout 300 bash "$COPY/deploy.sh" "$@" 2>&1)
    RC=$?
    WALL=$((SECONDS - t0))
    printf '%s\n' "$VYVOD" | sed 's/^/      /' | tail -5
}
sborok() { grep -c '^sborka$' "$STAND/poryadok" 2>/dev/null || true; }
prod_steps() { grep -cE '^(migracii|peresozdanie)$' "$STAND/poryadok" 2>/dev/null || true; }
gate_called() { grep -q . "$STAND/gate-argv" && echo да || echo нет; }
lock_taken() { [ -s "$LOCKF" ] && echo да || echo нет; }
suites_ran() { grep -c -- '--name proba-check-' "$STAND/docker.log" 2>/dev/null || true; }
tags_n() { git -C "$PRODUCT" tag -l 'deploy-*' | grep -c . || true; }
last_tag() { git -C "$PRODUCT" tag -l 'deploy-*' --sort=-refname | head -1; }
head_sha() { git -C "$PRODUCT" rev-parse HEAD; }
NOTES="$STAND/zhurnal/deploy-checks.jsonl"
# Записка success на sha: «да», если в журнале записок есть deploy.checks tests=success с data.sha.
note_success() {  # $1 = полный sha
    python3 -I - "$NOTES" "$1" <<'PY'
import json, sys
path, sha = sys.argv[1:3]
ok = False
try:
    for line in open(path, encoding="utf-8"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        data = rec.get("data") or {}
        if rec.get("event") == "deploy.checks" and data.get("sha") == sha and data.get("tests") == "success":
            ok = True
except OSError:
    pass
print("да" if ok else "нет")
PY
}
LIVE_TAG=""
remember_live() { [ "$RC" = 0 ] && LIVE_TAG=$(last_tag); return 0; }

echo "── 0. стенд: первый выкат без проверок — прод жив, :latest есть ──"
conf ""
run_deploy
remember_live
itog "0" "$RC" "0 условие: первый выкат выходит нулём"
itog "да" "$([ -n "$(id_of proba-api:latest)" ] && echo да || echo нет)" "0 условие: :latest на стенде есть"

echo "── 1. один путь: в двери нет второй цепочки (предикат §5.1) ──"
# Предикат читается разбором копии: пустой список файлов был бы «0 совпадений» без суда.
DOOR_FILES=("$COPY/deploy.sh" "$COPY"/lib/door_*.sh)
itog "да" "$([ "${#DOOR_FILES[@]}" -ge 3 ] && [ -f "${DOOR_FILES[1]}" ] && echo да || echo нет)" \
     "1 условие: deploy.sh и lib/door_*.sh найдены (${#DOOR_FILES[@]})"
itog "0" "$(cat "${DOOR_FILES[@]}" | grep -cE 'STACK_ROLLOUT|srv_main|ci_wait' || true)" \
     "1 grep -cE 'STACK_ROLLOUT|srv_main|ci_wait' по deploy.sh и lib/door_*.sh = 0"

echo "── Р-1. STACK_RELEASE_CMD пуст, а сборка поднимает службы — отказ до сборки с именами ключей ──"
# Сборка идёт до «Да» (§5.1): `up -d` в ней поднял бы прод без одобрения.
for UP in "docker compose up -d --build api" "docker compose up --build api"; do
    bump "р-1 $UP"
    conf "STACK_BUILD_CMD='$UP && echo sborka >> $STAND/poryadok'
STACK_RELEASE_CMD=\"\""
    TAGS_DO=$(tags_n)
    run_deploy
    itog "1" "$RC" "Р-1 «$UP»: код 1"
    itog "да" "$(has "$VYVOD" "STACK_RELEASE_CMD")" "Р-1 «$UP»: отказ называет STACK_RELEASE_CMD"
    itog "да" "$(has "$VYVOD" "STACK_BUILD_CMD")" "Р-1 «$UP»: отказ называет STACK_BUILD_CMD"
    itog "0" "$(sborok)" "Р-1 «$UP»: сборок 0"
    itog "нет" "$(gate_called)" "Р-1 «$UP»: ворота не звались"
    itog "$TAGS_DO" "$(tags_n)" "Р-1 «$UP»: тег не поставлен"
done
echo "── Р-1 контроль: сборка без up при пустом STACK_RELEASE_CMD — выкат идёт ──"
bump "р-1 контроль"
conf 'STACK_RELEASE_CMD=""'
run_deploy
remember_live
itog "0" "$RC" "Р-1 контроль: код 0"
itog "1" "$(sborok)" "Р-1 контроль: сборка одна"

echo "── 5. STACK_RUN_AS = root или пользователь агента — отказ до тестов ──"
checks_commit 60 "true"
for WHO in root "$(id -un)"; do
    conf "$CHECKS_KEY
STACK_RUN_AS=\"$WHO\""
    TAGS_DO=$(tags_n)
    run_deploy
    itog "1" "$RC" "5 STACK_RUN_AS=$WHO: код 1"
    itog "да" "$(has "$VYVOD" "STACK_RUN_AS")" "5 STACK_RUN_AS=$WHO: отказ называет ключ"
    itog "0" "$(suites_ran)" "5 STACK_RUN_AS=$WHO: наборы не звались"
    itog "нет" "$(gate_called)" "5 STACK_RUN_AS=$WHO: ворота не звались"
    itog "нет" "$(lock_taken)" "5 STACK_RUN_AS=$WHO: deploy.lock не взят"
    itog "$TAGS_DO" "$(tags_n)" "5 STACK_RUN_AS=$WHO: тег не поставлен"
    itog "0" "$(prod_steps)" "5 STACK_RUN_AS=$WHO: миграций и служб нет"
done

echo "── 2. тесты красные — отказ до лока: deploy.lock не взят, ворот нет, тега нет, :latest прежний ──"
checks_commit 60 "true" "false"
conf "$CHECKS_KEY"
TAGS_DO=$(tags_n)
LATEST_DO=$(id_of proba-api:latest)
run_deploy
RED_OUT=$VYVOD
itog "1" "$RC" "2 код 1"
itog "да" "$([ "$(suites_ran)" -ge 1 ] && echo да || echo нет)" "2 набор звался (этап T в цепочке)"
itog "нет" "$(lock_taken)" "2 deploy.lock не взят"
itog "нет" "$(gate_called)" "2 ворота не звались"
itog "$TAGS_DO" "$(tags_n)" "2 тег не поставлен"
itog "$LATEST_DO" "$(id_of proba-api:latest)" "2 :latest прежний (сборка до «Да» прод не трогает)"
itog "0" "$(prod_steps)" "2 миграций и служб нет"

echo "── 3. «красные» ≠ «не отработали»: разные слова итога ──"
checks_commit 3 "sleep 30"
conf "$CHECKS_KEY"
TAGS_DO=$(tags_n)
run_deploy
itog "1" "$RC" "3 зависший набор: код 1"
itog "нет" "$(gate_called)" "3 зависший набор: ворота не звались"
itog "$TAGS_DO" "$(tags_n)" "3 зависший набор: тег не поставлен"
itog "да" "$(has "$VYVOD" "тесты не отработали")" "3 зависший набор: «тесты не отработали»"
itog "нет" "$(has "$VYVOD" "тесты красные")" "3 зависший набор: не «тесты красные»"
itog "да" "$(has "$RED_OUT" "тесты красные")" "3 красный набор (случай 2): «тесты красные»"
itog "нет" "$(has "$RED_OUT" "тесты не отработали")" "3 красный набор (случай 2): не «тесты не отработали»"

echo "── 4. --проверка: записка, без лока, ворот, тега и :latest; выкат того же sha — по записке, сборок 0 ──"
checks_commit 60 "true"
conf "$CHECKS_KEY"
SHA4=$(head_sha)
TAGS_DO=$(tags_n)
LATEST_DO=$(id_of proba-api:latest)
run_deploy --проверка
itog "0" "$RC" "4 --проверка: код 0"
itog "да" "$(note_success "$SHA4")" "4 --проверка: записка success на ${SHA4:0:8}"
itog "нет" "$(lock_taken)" "4 --проверка: deploy.lock не взят"
itog "нет" "$(gate_called)" "4 --проверка: ворота не звались"
itog "$TAGS_DO" "$(tags_n)" "4 --проверка: тега нет"
itog "$LATEST_DO" "$(id_of proba-api:latest)" "4 --проверка: :latest прежний"
itog "0" "$(prod_steps)" "4 --проверка: миграций и служб нет"
itog "да" "$([ -n "$(id_of "proba-api:${SHA4:0:12}")" ] && echo да || echo нет)" "4 --проверка: образ :${SHA4:0:12} собран"
run_deploy
remember_live
itog "0" "$RC" "4 выкат того же sha: код 0"
itog "да" "$(has "$VYVOD" "по записке")" "4 выкат того же sha: «по записке»"
itog "0" "$(suites_ran)" "4 выкат того же sha: наборы не гоняются"
itog "0" "$(sborok)" "4 выкат того же sha: сборок 0"
itog "$((TAGS_DO + 1))" "$(tags_n)" "4 выкат того же sha: тег поставлен"
itog "да" "$([ -n "$(id_of "proba-api:${SHA4:0:12}")" ] && [ "$(id_of proba-api:latest)" = "$(id_of "proba-api:${SHA4:0:12}")" ] && echo да || echo нет)" \
     "4 выкат того же sha: :latest — образ :${SHA4:0:12}, собранный проверкой"

echo "── край: STACK_CHECKS_FILE пуст — этап T «пропущен: не задано» вслух, выкат идёт ──"
bump "край: проверок нет"
conf ""
run_deploy
remember_live
itog "0" "$RC" "пусто: код 0"
itog "да" "$(printf '%s\n' "$VYVOD" | grep -F 'STACK_CHECKS_FILE' | grep -qF 'пропущен: не задано' && echo да || echo нет)" \
     "пусто: строка называет STACK_CHECKS_FILE и «пропущен: не задано»"
itog "0" "$(suites_ran)" "пусто: наборы не звались"

# Конфиг копии, поставленной до эталона: ни одного ключа фаз 2–4 (обновление harness.conf
# копии не трогает). Какая поломка оставит случаи зелёными: дверь, читающая новый ключ
# умолчанием-отказом, — её ловит код 0 и строка «deploy ok» второго случая.
old_conf() {  # $1 — «с продуктом» или пусто
    {
        printf 'BACKUP_DIR="%s/kopii"\nPRODUCT_DIR=""\nPRODUCT_PATH=""\n' "$STAND"
        if [ -n "${1:-}" ]; then
            printf 'PRODUCT_REPO="%s"\nSTACK_IMAGES="proba-api"\nSTACK_HEALTH_CMD="true"\n' "$PRODUCT"
            printf "STACK_BUILD_CMD='%s'\n" "$BUILD"
            printf 'STACK_UP_CMD="echo otkat >> %s/poryadok"\n' "$STAND"
            printf 'STACK_MIGRATE_CMD="echo migracii >> %s/poryadok"\n' "$STAND"
            printf 'STACK_RELEASE_CMD="echo peresozdanie >> %s/poryadok"\n' "$STAND"
        else
            printf 'PRODUCT_REPO=""\nSTACK_BUILD_CMD=""\nSTACK_IMAGES=""\n'
        fi
    } > "$STAND/harness.conf"
}
no_new_keys() {
    grep -cE '^(STACK_(REMOTE|BRANCH|RUN_AS|STEP_TIMEOUTS|CHECKS?_[A-Z_]+|STAND_[A-Z_]+|SMOKE_CMD|FRONTEND_CMD|ROLLBACK_CMD|SCAN_[A-Z_]+)|DEPLOY_HEALTH_WAIT_SEC)=' \
        "$STAND/harness.conf" || true
}

echo "── §6.1 а. копия без продукта и без новых ключей — «продукта нет», код 0, docker не звался ──"
old_conf ""
itog "0" "$(no_new_keys)" "§6.1 а условие: в конфиге копии нет ни одного ключа фаз 2–4"
TAGS_DO=$(tags_n)
run_deploy
itog "0" "$RC" "§6.1 а код 0"
itog "да" "$(has "$VYVOD" "продукта нет")" "§6.1 а сказано «продукта нет»"
itog "0" "$(grep -c . "$STAND/docker.log" || true)" "§6.1 а docker не звался"
itog "нет" "$(gate_called)" "§6.1 а ворота не звались"
itog "$TAGS_DO" "$(tags_n)" "§6.1 а тег не поставлен"

echo "── §6.1 б. копия с продуктом и без новых ключей — выкат идёт, T и D/I «пропущен: не задано» вслух ──"
bump "§6.1: старый конфиг копии"
old_conf "с продуктом"
itog "0" "$(no_new_keys)" "§6.1 б условие: в конфиге копии нет ни одного ключа фаз 2–4"
run_deploy
remember_live
itog "0" "$RC" "§6.1 б код 0"
itog "да" "$(has "$VYVOD" "deploy ok:")" "§6.1 б итог «deploy ok»"
itog "да" "$(printf '%s\n' "$VYVOD" | grep -F 'STACK_CHECKS_FILE' | grep -qF 'пропущен: не задано' && echo да || echo нет)" \
     "§6.1 б тесты: «STACK_CHECKS_FILE … пропущен: не задано»"
itog "да" "$(printf '%s\n' "$VYVOD" | grep -F 'STACK_SCAN_CMD' | grep -qF 'пропущен: не задано' && echo да || echo нет)" \
     "§6.1 б скан: «STACK_SCAN_CMD … пропущен: не задано»"
itog "нет" "$(has "$VYVOD" "тесты: success")" "§6.1 б пропуск тестов не назван зелёным"
itog "0" "$(suites_ran)" "§6.1 б наборы не звались"
itog "1" "$(sborok)" "§6.1 б одна сборка"

echo "── край: записка success на sha, но хэш файла проверок другой — наборы гоняются ──"
bump "край: чужой хэш"
conf "$CHECKS_KEY"
SHA_H=$(head_sha)
python3 -I - "$NOTES" "$SHA_H" <<'PY'
import datetime, json, sys
path, sha = sys.argv[1:3]
ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
rec = {"v": 1, "ts": ts, "src": "проба", "event": "deploy.checks", "outcome": "ok",
       "data": {"sha": sha, "branch": "main", "checks_file_sha": "0" * 64, "only": [],
                "selected": ["sloj"], "suites": [], "tests": "success"}}
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
PY
itog "да" "$(note_success "$SHA_H")" "хэш: условие — записка success на ${SHA_H:0:8} подложена"
run_deploy
remember_live
itog "нет" "$(has "$VYVOD" "по записке")" "хэш: «по записке» не сказано"
itog "да" "$([ "$(suites_ran)" -ge 1 ] && echo да || echo нет)" "хэш: наборы гонялись"
itog "0" "$RC" "хэш: зелёные наборы — выкат идёт, код 0"

echo "── 7. смоук упал при живой ручке — «выкачено с оговоркой: смоук», выход 0 ──"
bump "смоук"
conf 'STACK_SMOKE_CMD="false"'
run_deploy
itog "0" "$RC" "7 код 0"
itog "да" "$(has "$VYVOD" "выкачено с оговоркой: смоук")" "7 сказано «выкачено с оговоркой: смоук»"
remember_live

echo "── 6. шаг прода сверх потолка — откат, «ВЫКАТ ПРОВАЛЕН, стек на прошлой версии <тег>» ──"
bump "потолок"
PREV_TAG=$LIVE_TAG
itog "да" "$([ -n "$PREV_TAG" ] && echo да || echo нет)" "6 условие: живой выкат до него есть ($PREV_TAG)"
conf 'STACK_RELEASE_CMD="sleep 90"
STACK_STEP_TIMEOUTS="release=2"'
run_deploy
itog "1" "$RC" "6 код 1"
itog "да" "$(has "$VYVOD" "ВЫКАТ ПРОВАЛЕН, стек на прошлой версии $PREV_TAG")" \
     "6 сказано «ВЫКАТ ПРОВАЛЕН, стек на прошлой версии $PREV_TAG»"
itog "да" "$(has "$(cat "$STAND/poryadok")" "otkat")" "6 подъём отката звался"
itog "да" "$([ "$WALL" -lt 60 ] && echo да || echo нет)" "6 потолок снял шаг: стена ${WALL} с < 60 с"

# Дыры ревью кода 3б (журнал задачи 16.11 15:29): откат командой продукта, пустой STACK_RUN_AS,
# смоук при мёртвой ручке, потолок миграций. Ручка живости — файл: шаг стенда «оживляет» или
# «убивает» стек, и проба судит, что дверь спросила живость в нужный момент.
RESULTS="$STAND/zhurnal/deploy-results.jsonl"
last_result() {  # data.итог последней строки журнала итогов
    python3 -I - "$RESULTS" <<'PY'
import json, sys
last = ""
try:
    for line in open(sys.argv[1], encoding="utf-8"):
        try:
            last = str((json.loads(line).get("data") or {}).get("итог", ""))
        except ValueError:
            continue
except OSError:
    pass
print(last)
PY
}
said_failed_to() {  # «да», если строка вывода называет «ВЫКАТ ПРОВАЛЕН» и тег $1
    printf '%s\n' "$VYVOD" | grep -F 'ВЫКАТ ПРОВАЛЕН' | grep -qF -- "$1" && echo да || echo нет
}
latest_retags() { grep -cE '^tag [^ ]+ proba-api:latest$' "$STAND/docker.log" 2>/dev/null || true; }
ZHIVO="$STAND/zhivo"

echo "── 9. STACK_RUN_AS пуст — шаги прода от агента, sudo не зовётся ──"
bump "run-as пуст"
conf "STACK_RUN_AS=\"\"
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok; id -un >> $STAND/kto\"
STACK_RELEASE_CMD=\"echo peresozdanie >> $STAND/poryadok; id -un >> $STAND/kto\"
STACK_FRONTEND_CMD=\"id -un >> $STAND/kto\"
STACK_SMOKE_CMD=\"id -un >> $STAND/kto\""
: > "$STAND/kto"
run_deploy
remember_live
LIVE_ID=$(id_of proba-api:latest)
itog "0" "$RC" "9 код 0"
itog "0" "$(grep -c '^sudo ' "$STAND/docker.log" || true)" "9 sudo не звался"
itog "4" "$(grep -c . "$STAND/kto" || true)" "9 шаги migrate, release, frontend, smoke исполнились"
itog "$(id -un)" "$(sort -u "$STAND/kto" | paste -sd' ')" "9 все шаги — от пользователя агента"

echo "── 10. STACK_ROLLBACK_CMD задан — откат ИМ вместо ретега, без IMAGE_TAG проваленного выката ──"
bump "откат командой"
PREV_TAG=$LIVE_TAG
FAIL12=$(head_sha | cut -c1-12)
itog "да" "$([ -n "$PREV_TAG" ] && [ -n "$LIVE_ID" ] && echo да || echo нет)" "10 условие: живой выкат 9 есть ($PREV_TAG)"
# Команда продукта пишет, какой IMAGE_TAG видит, и оживляет стек: «откат жив» судит health.
cat > "$STAND/otkat-produkta.sh" <<'SH'
printf 'otkat-komandoj IMAGE_TAG=%s\n' "${IMAGE_TAG:-<net>}" >> "$1/poryadok"
touch "$1/zhivo"
SH
rm -f "$ZHIVO"
conf "STACK_HEALTH_CMD=\"test -f $ZHIVO\"
STACK_ROLLBACK_CMD=\"bash $STAND/otkat-produkta.sh $STAND\""
run_deploy
itog "1" "$RC" "10 код 1"
itog "да" "$(has "$(cat "$STAND/poryadok")" "otkat-komandoj")" "10 команда отката звалась"
itog "нет" "$(has "$(cat "$STAND/poryadok")" "IMAGE_TAG=$FAIL12")" \
     "10 команда отката не видит IMAGE_TAG=$FAIL12 проваленного выката (вышло: $(sed -n 's/^otkat-komandoj //p' "$STAND/poryadok"))"
itog "1" "$(latest_retags)" "10 ретега в :latest для отката нет (одна строка — продвижение выката)"
itog "да" "$(said_failed_to "$PREV_TAG")" "10 сказано «ВЫКАТ ПРОВАЛЕН» и прошлая версия $PREV_TAG"
itog "нет" "$(case "$(last_result)" in выкачено*) echo да ;; *) echo нет ;; esac)" "10 итог в журнале итогов — не «выкачено»"

echo "── 11. шаг миграций сверх потолка — откат на живой выкат, «ВЫКАТ ПРОВАЛЕН» ──"
bump "потолок миграций"
conf 'STACK_MIGRATE_CMD="sleep 90"
STACK_STEP_TIMEOUTS="migrate=2"'
run_deploy
itog "1" "$RC" "11 код 1"
itog "да" "$([ "$WALL" -lt 60 ] && echo да || echo нет)" "11 потолок снял шаг: стена ${WALL} с < 60 с"
itog "да" "$(has "$VYVOD" "потолок 2 с")" "11 причина названа: потолок 2 с"
itog "0" "$(grep -c '^peresozdanie$' "$STAND/poryadok" || true)" "11 службы не пересоздавались"
itog "$LIVE_ID" "$(id_of proba-api:latest)" "11 :latest — образ живого выката $PREV_TAG"
itog "да" "$(said_failed_to "$PREV_TAG")" "11 сказано «ВЫКАТ ПРОВАЛЕН» и прошлая версия $PREV_TAG"
itog "да" "$(case "$(last_result)" in "ВЫКАТ ПРОВАЛЕН"*) echo да ;; *) echo нет ;; esac)" \
     "11 итог в журнале итогов — «ВЫКАТ ПРОВАЛЕН…» (вышло: $(last_result | cut -c1-80))"

echo "── 12. смоук упал, и ручка мертва — откат, а не «выкачено с оговоркой» ──"
# Ручка жива на этапе M (её оживляет пересоздание), смоук её убивает и падает: «необязательный
# шаг упал при живом стеке» (§5.3 п. 7) уже неправда — стек лежит, это п. 5.
bump "смоук при мёртвой ручке"
rm -f "$ZHIVO"
conf "STACK_HEALTH_CMD=\"test -f $ZHIVO\"
STACK_RELEASE_CMD=\"echo peresozdanie >> $STAND/poryadok; touch $ZHIVO\"
STACK_SMOKE_CMD=\"rm -f $ZHIVO; false\"
STACK_UP_CMD=\"echo otkat >> $STAND/poryadok; touch $ZHIVO\""
run_deploy
itog "1" "$RC" "12 код 1"
itog "нет" "$(has "$VYVOD" "выкачено с оговоркой")" "12 не «выкачено с оговоркой»"
itog "да" "$(has "$(cat "$STAND/poryadok")" "otkat")" "12 подъём отката звался"
itog "$LIVE_ID" "$(id_of proba-api:latest)" "12 :latest — образ живого выката $PREV_TAG"
itog "да" "$(said_failed_to "$PREV_TAG")" "12 сказано «ВЫКАТ ПРОВАЛЕН» и прошлая версия $PREV_TAG"
itog "нет" "$(case "$(last_result)" in выкачено*) echo да ;; *) echo нет ;; esac)" "12 итог в журнале итогов — не «выкачено»"

echo "── 8. грязь прода появилась под локом (после дампа) — отказ, тега в хранилище нет, дамп остаётся ──"
# Дерево прода отдельно от клона двери (PRODUCT_PATH ≠ PRODUCT_REPO, случай CF). Входящий
# коммит меняет web/a.txt; дамп пачкает тот же файл на проде: до дампа прод чист.
git -C "$PRODUCT" push -q origin main
git clone -q "$BARE" "$PROD_TREE"
printf 'новая витрина\n' > "$PRODUCT/web/a.txt"
g -C "$PRODUCT" commit -qam "входящий меняет web/a.txt"
conf "STACK_REMOTE=\"origin\"
STACK_BRANCH=\"main\"
PRODUCT_PATH=\"$PROD_TREE\"
STACK_DB_DUMP_CMD=\"echo pravka-rukami >> $PROD_TREE/web/a.txt; head -c 4096 /dev/zero\""
DUMPS_DO=$(find "$STAND/kopii" -name 'база.dump' -size +1k 2>/dev/null | grep -c . || true)
run_deploy
itog "1" "$RC" "8 код 1"
itog "да" "$(has "$VYVOD" "web/a.txt")" "8 отказ называет файл web/a.txt"
itog "" "$(git -C "$BARE" tag -l 'deploy-*')" "8 тега в хранилище нет"
itog "0" "$(prod_steps)" "8 миграций и служб нет"
itog "$((DUMPS_DO + 1))" "$(find "$STAND/kopii" -name 'база.dump' -size +1k 2>/dev/null | grep -c . || true)" \
     "8 дамп до выката остался"
# М2 (ревью 35): грязь под локом — отказ до пуша, прод не тронут; итог словом «отказ», а не
# «ВЫКАТ ПРОВАЛЕН» (тот значит «прод трогали, стек на прошлой версии»).
itog "да" "$(has "$(last_result)" "отказ")" "М2 итог в журнале итогов — «отказ» (вышло: $(last_result | cut -c1-80))"
itog "нет" "$(has "$(last_result)" "ВЫКАТ ПРОВАЛЕН")" "М2 итог в журнале итогов — не «ВЫКАТ ПРОВАЛЕН»"
itog "нет" "$(has "$VYVOD" "ВЫКАТ ПРОВАЛЕН")" "М2 вывод не говорит «ВЫКАТ ПРОВАЛЕН»"

# ═════ Раунд ревью 35 (журнал задачи 16.11 16:3x): К1, В2–В4, М1, М3, E5–E7 ═════
# Случаи стоят после 8: конфиг каждого — свой conf (без PRODUCT_PATH и хранилища).
STAND_ENV=(PATH="$FAKEBIN:$PATH" DOCKER_STATE="$STATE" DOCKER_JOURNAL="$STAND/docker.log"
    PROBA_GATE_ARGV="$STAND/gate-argv" HARNESS_DOOR_STAND_HOME="$STAND"
    HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf"
    DEPLOY_CHECK_MEMINFO="$STAND/meminfo" DEPLOY_CHECK_WORK_ROOT="$STAND/kopii-proverok"
    DEPLOY_CHECK_LOG_ROOT="$STAND/logi-proverok" DEPLOY_CHECK_MEM_WAIT_SEC=5)
compose_ups() { grep -cE '^compose (up|start|restart|run)( |$)' "$STAND/docker.log" 2>/dev/null || true; }
# Чужая красная записка: прогон другого sha кончился failure — последняя строка журнала записок.
foreign_red_note() {
    python3 -I - "$NOTES" <<'PY'
import datetime, json, sys
ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
rec = {"v": 1, "ts": ts, "src": "проба", "event": "deploy.checks", "outcome": "error",
       "data": {"sha": "f" * 40, "branch": "chuzhaja", "checks_file_sha": "0" * 64, "only": [],
                "selected": ["sloj"], "suites": [], "tests": "failure"}}
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
PY
}
# Шаг, оставляющий фонового потомка (служба, пережившая шаг): его pid — в o39-leak.pid.
cat > "$STAND/o39-leak.sh" <<'SH'
sleep 20 >/dev/null 2>&1 < /dev/null &
echo $! > "$1/o39-leak.pid"
SH
LEAK="bash $STAND/o39-leak.sh $STAND"
leak_alive() { [ -s "$STAND/o39-leak.pid" ] && kill -0 "$(cat "$STAND/o39-leak.pid")" 2>/dev/null && echo да || echo нет; }
kill_leak() { [ ! -s "$STAND/o39-leak.pid" ] || kill "$(cat "$STAND/o39-leak.pid")" 2>/dev/null; rm -f "$STAND/o39-leak.pid"; }

echo "── К1. сборка поднимает службы при ЗАДАННОМ STACK_RELEASE_CMD — отказ до сборки ──"
# Р-1 не зависит от STACK_RELEASE_CMD: сборка идёт до «Да», и up/start/restart/run в ней
# поднимает прод без одобрения, есть ли потом отдельное пересоздание или нет.
for UP in "docker compose up -d" "docker compose start api" "docker compose restart api" "docker compose run --rm api true"; do
    bump "к1 $UP"
    conf "STACK_BUILD_CMD='docker build -t proba-api:\${IMAGE_TAG:-latest} . && $UP && echo sborka >> $STAND/poryadok'"
    TAGS_DO=$(tags_n)
    run_deploy
    itog "1" "$RC" "К1 «$UP» при заданном STACK_RELEASE_CMD: код 1"
    itog "да" "$(has "$VYVOD" "STACK_BUILD_CMD")" "К1 «$UP»: отказ называет STACK_BUILD_CMD"
    itog "0" "$(sborok)" "К1 «$UP»: сборок 0"
    itog "0" "$(compose_ups)" "К1 «$UP»: compose up/start/restart/run не звался"
    itog "нет" "$(gate_called)" "К1 «$UP»: ворота не звались"
    itog "$TAGS_DO" "$(tags_n)" "К1 «$UP»: тег не поставлен"
done
bump "к1 nocache"
conf "STACK_BUILD_NOCACHE_CMD='docker compose up -d --build api && echo sborka >> $STAND/poryadok'"
run_deploy
itog "1" "$RC" "К1 up в STACK_BUILD_NOCACHE_CMD при заданном STACK_RELEASE_CMD: код 1"
itog "да" "$(has "$VYVOD" "STACK_BUILD_NOCACHE_CMD")" "К1 nocache: отказ называет STACK_BUILD_NOCACHE_CMD"
itog "0" "$(compose_ups)" "К1 nocache: compose up не звался"
bump "к1 proverka"
conf "STACK_BUILD_CMD='docker build -t proba-api:\${IMAGE_TAG:-latest} . && docker compose up -d && echo sborka >> $STAND/poryadok'"
run_deploy --проверка
itog "1" "$RC" "К1 --проверка со сборкой «up -d»: код 1"
itog "0" "$(sborok)" "К1 --проверка: сборок 0"
itog "0" "$(compose_ups)" "К1 --проверка: compose up не звался (прод не поднят без кнопки)"
echo "── К1 контроль: «run» вне compose (довод сборки) — не отказ ──"
conf "STACK_BUILD_CMD='docker build -t proba-api:\${IMAGE_TAG:-latest} --build-arg STEP=\"npm run build\" . && echo sborka >> $STAND/poryadok'"
run_deploy --проверка
itog "0" "$RC" "К1 контроль: «npm run build» в доводе сборки — код 0"
itog "1" "$(sborok)" "К1 контроль: сборка одна"

echo "── В4. ключ тестового этапа негоден — отказ до сборки; В2: чужая красная записка его «красным» не делает ──"
checks_commit 60 "true"
for BAD in 'STACK_CHECK_MIN_FREE_MB=""' 'STACK_CHECK_UID="0:0"'; do
    KEY=${BAD%%=*}
    bump "в4 $KEY"
    conf "$CHECKS_KEY
$BAD"
    foreign_red_note
    TAGS_DO=$(tags_n)
    run_deploy
    itog "1" "$RC" "В4 $BAD: код 1"
    itog "да" "$(has "$VYVOD" "$KEY")" "В4 $BAD: отказ называет $KEY"
    itog "0" "$(sborok)" "В4 $BAD: сборок 0 (ключ судится до сборки)"
    itog "0" "$(suites_ran)" "В4 $BAD: наборы не звались"
    itog "нет" "$(gate_called)" "В4 $BAD: ворота не звались"
    itog "нет" "$(has "$VYVOD" "тесты красные")" "В2 $BAD: наборы не шли — не «тесты красные» (последняя записка — чужая failure)"
done

echo "── В2. checks.lock занят после чужой красной записки — «не отработали», не «красные» ──"
bump "в2 zanjato"
conf "$CHECKS_KEY"
foreign_red_note
mkdir -p "$STAND/zhurnal/locks"
( exec 9>>"$STAND/zhurnal/locks/checks.lock"; flock -n 9 || exit 1; exec sleep 30 ) &
LKPID=$!
sleep 1
run_deploy
kill "$LKPID" 2>/dev/null; wait "$LKPID" 2>/dev/null
itog "1" "$RC" "В2 занятый checks.lock: код 1"
itog "да" "$(has "$VYVOD" "занято")" "В2 условие: замок проверок занят"
itog "да" "$(has "$VYVOD" "тесты не отработали")" "В2 занятый checks.lock: «тесты не отработали»"
itog "нет" "$(has "$VYVOD" "тесты красные")" "В2 занятый checks.lock: не «тесты красные» (вердикт — по своему sha)"
itog "нет" "$(gate_called)" "В2 ворота не звались"

echo "── М3. STACK_RUN_AS — пользователь, от которого sudo не работает: отказ до тестов ──"
bump "м3 run-as"
conf "$CHECKS_KEY
STACK_RUN_AS=\"nobody\""
TAGS_DO=$(tags_n)
run_deploy
itog "да" "$(id -u nobody >/dev/null 2>&1 && echo да || echo нет)" "М3 условие: пользователь nobody есть"
itog "1" "$RC" "М3 код 1"
itog "да" "$(has "$VYVOD" "STACK_RUN_AS")" "М3 отказ называет STACK_RUN_AS"
itog "0" "$(sborok)" "М3 сборок 0"
itog "0" "$(suites_ran)" "М3 наборы не звались"
itog "нет" "$(gate_called)" "М3 ворота не звались"
itog "$TAGS_DO" "$(tags_n)" "М3 тег не поставлен"
itog "0" "$(prod_steps)" "М3 миграций и служб нет"

echo "── E6. неизвестный шаг в STACK_STEP_TIMEOUTS — отказ при загрузке ──"
bump "e6"
conf 'STACK_STEP_TIMEOUTS="biuld=1800"'
TAGS_DO=$(tags_n)
run_deploy
itog "1" "$RC" "E6 «biuld=1800»: код 1"
itog "да" "$(has "$VYVOD" "biuld")" "E6 отказ называет шаг biuld"
itog "0" "$(sborok)" "E6 сборок 0"
itog "нет" "$(gate_called)" "E6 ворота не звались"
itog "$TAGS_DO" "$(tags_n)" "E6 тег не поставлен"

echo "── E7. записка success есть, а образа :sha12 нет — сборка идёт, выкат жив ──"
checks_commit 60 "true"
conf "$CHECKS_KEY"
SHA7=$(head_sha)
run_deploy --проверка
itog "да" "$(note_success "$SHA7")" "E7 условие: записка success на ${SHA7:0:8}"
awk -v r="proba-api:${SHA7:0:12}" '$1 != r' "$STATE" > "$STATE.new" && mv "$STATE.new" "$STATE"
itog "" "$(id_of "proba-api:${SHA7:0:12}")" "E7 условие: образ :${SHA7:0:12} удалён"
run_deploy
remember_live
itog "0" "$RC" "E7 код 0"
itog "1" "$(sborok)" "E7 сборка одна: записка без образа сборку не снимает"
itog "да" "$([ -n "$(id_of "proba-api:${SHA7:0:12}")" ] && [ "$(id_of proba-api:latest)" = "$(id_of "proba-api:${SHA7:0:12}")" ] && echo да || echo нет)" \
     "E7 :latest — свежий образ :${SHA7:0:12}"

echo "── В3. потолок шага прокси: proxy=1, reload спит 40 с — снят по потолку ──"
# Стена судит весь выкат, а не один шаг: под параллельной нагрузкой проход без reload сам
# идёт секунды. Поэтому сон шага много больше порога — как у случая rollback ниже.
bump "в3 proxy"
conf 'STACK_STEP_TIMEOUTS="proxy=1"
STACK_PROXY_RELOAD_CMD="sleep 40"'
run_deploy
itog "да" "$([ "$WALL" -lt 25 ] && echo да || echo нет)" "В3 proxy=1: reload снят потолком, стена ${WALL} с < 25 с"
itog "нет" "$([ "$RC" = 0 ] && echo да || echo нет)" "В3 proxy=1: reload, снятый потолком, — не код 0 (вышло $RC)"
remember_live

echo "── В3. подъём отката (STACK_UP_CMD) под потолком rollback ──"
bump "в3 rollback"
conf 'STACK_HEALTH_CMD="false"
STACK_UP_CMD="sleep 40"
STACK_STEP_TIMEOUTS="rollback=1"'
run_deploy
itog "1" "$RC" "В3 rollback=1: код 1"
itog "да" "$([ "$WALL" -lt 25 ] && echo да || echo нет)" "В3 rollback=1: подъём отката снят потолком, стена ${WALL} с < 25 с"

echo "── В3/E5. шаги не держат замки двери: фоновый потомок шага не занимает product-tree.lock ──"
# Наблюдаемое: следующий проход сразу после конца первого не слышит «дерево продукта занято».
# Потомок STACK_UP_CMD появляется только в откате: пересоздание падает, подъём отката оживляет стек.
for STEP in STACK_MIGRATE_CMD STACK_PROXY_RELOAD_CMD STACK_UP_CMD; do
    bump "в3 fd $STEP"
    rm -f "$STAND/o39-leak.pid" "$STAND/o39-zhivo"
    case "$STEP" in
        STACK_UP_CMD) conf "STACK_HEALTH_CMD=\"test -f $STAND/o39-zhivo\"
STACK_RELEASE_CMD=\"false\"
STACK_UP_CMD=\"touch $STAND/o39-zhivo; $LEAK\"" ;;
        *) conf "$STEP=\"$LEAK\"" ;;
    esac
    run_deploy
    itog "да" "$(leak_alive)" "В3 $STEP условие: потомок шага жив после прохода (код прохода $RC)"
    run_deploy --проверка
    itog "нет" "$(has "$VYVOD" "дерево продукта занято")" "В3 $STEP: следующий проход не упёрся в замок дерева"
    kill_leak
    rm -f "$STAND/o39-zhivo"
done

echo "── М1. SIGTERM двери во время миграций — итог не «код 0», миграция не переживает дверь ──"
bump "м1"
conf "STACK_MIGRATE_CMD=\"sleep 6; echo migracii >> $STAND/poryadok\""
: > "$STAND/poryadok"; rm -f "$LOCKF"; sleep 1
TAGS_DO=$(tags_n)
env "${STAND_ENV[@]}" bash "$COPY/deploy.sh" > "$STAND/o39-m1.out" 2>&1 &
DPID=$!
for _ in $(seq 1 40); do [ "$(tags_n)" -gt "$TAGS_DO" ] && break; sleep 0.5; done
sleep 1
kill -TERM "$DPID"
wait "$DPID"; M1RC=$?
itog "да" "$([ "$(tags_n)" -gt "$TAGS_DO" ] && echo да || echo нет)" "М1 условие: тег поставлен до сигнала"
itog "143" "$M1RC" "М1 код выхода 143"
itog "нет" "$(has "$(last_result)" "кодом 0")" "М1 итог не «остановлен кодом 0» (вышло: $(last_result | cut -c1-80))"
itog "да" "$(has "$(last_result)" "143")" "М1 итог называет код 143"
sleep 7
itog "0" "$(grep -c '^migracii$' "$STAND/poryadok" || true)" "М1 шаг миграций не дошёл после смерти двери"

# ═════ Раунд перепроверки 42 (журнал задачи 16.11 19:27): флаги compose, И-1, И-2, К-42 ═════
echo "── К1 флаги compose: значение флага — не подкоманда ──"
# «--profile build» — имя профиля, за ним идёт up; «-p run» — имя проекта, за ним build,
# а «run» в доводе сборки после build — не подкоманда.
bump "к1 profile"
conf "STACK_BUILD_CMD='docker build -t proba-api:\${IMAGE_TAG:-latest} . && docker compose --profile build up -d && echo sborka >> $STAND/poryadok'"
run_deploy --проверка
itog "1" "$RC" "К1 «docker compose --profile build up -d»: код 1"
itog "0" "$(sborok)" "К1 «--profile build up -d»: сборок 0"
conf "STACK_BUILD_CMD='docker build -t proba-api:\${IMAGE_TAG:-latest} . && docker compose -p run build --build-arg STEP=\"npm run build\" api && echo sborka >> $STAND/poryadok'"
run_deploy --проверка
itog "0" "$RC" "К1 «docker compose -p run build --build-arg STEP=\"npm run build\"»: не отказ, код 0"
itog "1" "$(sborok)" "К1 «-p run build»: сборка одна"

echo "── И-1. первый выкат, compose без IMAGE_TAG: --проверка A не оставляет :latest, откат B не поднимает A ──"
# Образ без прошлого :latest и журнал итогов без живых строк — первый выкат продукта.
mv "$RESULTS" "$RESULTS.o43"
bump "и-1 A"
PERV="STACK_IMAGES=\"proba-perv\"
STACK_BUILD_CMD='docker build -t proba-perv:latest . && echo sborka >> $STAND/poryadok'"
conf "$PERV"
itog "" "$(id_of proba-perv:latest)" "И-1 условие: :latest образа proba-perv не было"
run_deploy --проверка
A_ID=$(id_of "proba-perv:$(head_sha | cut -c1-12)")
itog "0" "$RC" "И-1 --проверка A: код 0"
itog "да" "$([ -n "$A_ID" ] && echo да || echo нет)" "И-1 --проверка A: образ помечен :sha12"
itog "" "$(id_of proba-perv:latest)" "И-1 --проверка A: :latest не появился (до «Да» прод не тронут)"
bump "и-1 B"
conf "$PERV
STACK_HEALTH_CMD=\"false\""
run_deploy
itog "1" "$RC" "И-1 выкат B с мёртвой живостью: код 1"
itog "нет" "$([ "$(id_of proba-perv:latest)" = "$A_ID" ] && echo да || echo нет)" \
     "И-1 откат B не поднял неодобренный A в :latest"
mv "$RESULTS.o43" "$RESULTS"

echo "── И-2. TERM после продвижения образов — :latest живой, подсказка дампа ──"
bump "и-2 живой"
conf ""
run_deploy
remember_live
LIVE_I2=$(id_of proba-api:latest)
itog "0" "$RC" "И-2 условие: живой выкат $LIVE_TAG"
bump "и-2"
conf "STACK_DB_DUMP_CMD=\"head -c 4096 /dev/zero\"
STACK_MIGRATE_CMD=\"sleep 6; echo migracii >> $STAND/poryadok\""
: > "$STAND/poryadok"; rm -f "$LOCKF"; sleep 1
env "${STAND_ENV[@]}" bash "$COPY/deploy.sh" > "$STAND/o43-i2.out" 2>&1 &
DPID=$!
for _ in $(seq 1 40); do grep -q 'продвинуты в :latest' "$STAND/o43-i2.out" && break; sleep 0.5; done
sleep 1
kill -TERM "$DPID"
wait "$DPID"; I2RC=$?
I2OUT=$(cat "$STAND/o43-i2.out")
itog "да" "$(has "$I2OUT" "продвинуты в :latest")" "И-2 условие: образы продвинуты до сигнала"
itog "143" "$I2RC" "И-2 код 143"
itog "$LIVE_I2" "$(id_of proba-api:latest)" "И-2 :latest после TERM — образ живого выката $LIVE_TAG"
itog "да" "$(has "$I2OUT" "схема базы НЕ откачена")" "И-2 вывод называет дамп до миграций"
sleep 6

echo "── К-42. --ветка без отдельного дерева прода — отказ до переключения ──"
# Дерево одно (PRODUCT_PATH пуст или = PRODUCT_REPO): checkout --detach ветки менял бы файлы,
# которые службы прода видят, до ворот и «Да».
git -C "$PRODUCT" push -q origin main
git clone -q "$BARE" "$STAND/klon-k42"
g -C "$STAND/klon-k42" checkout -q -b vetka-k42
printf 'витрина ВЕТКИ\n' > "$STAND/klon-k42/web/a.txt"
g -C "$STAND/klon-k42" commit -qam "ветка k42"
git -C "$STAND/klon-k42" push -q origin vetka-k42
for K42 in "--проверка|" "|PRODUCT_PATH=\"$PRODUCT\""; do
    KEYS=${K42%%|*}; EXTRA=${K42#*|}
    conf "STACK_REMOTE=\"origin\"
STACK_BRANCH=\"main\"
STACK_BUILD_CMD='cat $PRODUCT/web/a.txt > $STAND/k42-vo-vremya; $BUILD'
$EXTRA"
    rm -f "$STAND/k42-vo-vremya"
    TXT_DO=$(cat "$PRODUCT/web/a.txt")
    HEAD_DO=$(head_sha)
    TAGS_DO=$(tags_n)
    run_deploy $KEYS --ветка vetka-k42
    NAME="К-42 ${KEYS:-выкат} --ветка, ${EXTRA:-PRODUCT_PATH пуст}"
    itog "1" "$RC" "$NAME: код 1"
    itog "да" "$(has "$VYVOD" "задай PRODUCT_PATH")" "$NAME: отказ называет PRODUCT_PATH"
    itog "$HEAD_DO" "$(head_sha)" "$NAME: HEAD дерева не сдвинут"
    itog "$TXT_DO" "$(cat "$PRODUCT/web/a.txt")" "$NAME: файлы дерева прода — прежние"
    itog "нет" "$(has "$(cat "$STAND/k42-vo-vremya" 2>/dev/null)" "ВЕТКИ")" "$NAME: дерево прода не видело ветку и во время сборки"
    itog "0" "$(sborok)" "$NAME: сборок 0"
    itog "нет" "$(gate_called)" "$NAME: ворота не звались"
    itog "$TAGS_DO" "$(tags_n)" "$NAME: тег не поставлен"
    git -C "$PRODUCT" checkout -q main 2>/dev/null
done

# ═════ Раунд ревью фазы 4 (журнал задачи 16.11 22:16): этап провала в записи итога, TERM после служб ═════
echo "── И1. провал после P — запись итога несёт время последнего этапа: |сумма − стена| ≤ 5 с, M > 0 ──"
# Запись итога, сделанная посреди этапа (оговорка прокси, авария отката), теряла его время:
# M = 0, а 8–16 с выката ни одному этапу не доставались.
i1_rec() {  # $1 — стена прохода, с → «сумма да|нет…», «M да|нет…», «итог …»
    python3 -I - "$RESULTS" "$1" <<'PY'
import json, sys
path, wall = sys.argv[1], float(sys.argv[2])
data = {}
for line in open(path, encoding="utf-8"):
    try:
        data = json.loads(line).get("data") or {}
    except ValueError:
        continue
st = data.get("этапы") or {}
total = sum(v for v in st.values() if isinstance(v, (int, float)))
print("сумма", "да" if st and abs(total - wall) <= 5 else f"нет: сумма {total:.2f}, стена {wall:g}")
print("M", "да" if st.get("M", 0) > 0 else f"нет: {st!r}"[:120])
print("итог", str(data.get("итог", ""))[:80])
PY
}
i1_field() { printf '%s\n' "$I1_REC" | sed -n "s/^$1 //p"; }
for I1 in "оговорка прокси|STACK_PROXY_RELOAD_CMD=\"sleep 8; false\"" \
          "авария|STACK_HEALTH_CMD=\"false\"
DEPLOY_HEALTH_WAIT_SEC=\"8\""; do
    NAME="И1 ${I1%%|*}"
    bump "и1 ${I1%%|*}"
    conf "${I1#*|}"
    run_deploy
    I1_REC=$(i1_rec "$WALL")
    itog "1" "$RC" "$NAME: код 1 (итог: $(i1_field итог))"
    itog "да" "$(i1_field сумма)" "$NAME: |сумма этапов − стена ${WALL} с| ≤ 5 с"
    itog "да" "$(i1_field M)" "$NAME: последний этап M не 0"
done

echo "── И3. TERM после пересоздания служб — подъём отката или слова «подними STACK_UP_CMD руками» ──"
# :latest возвращается на живой выкат, а службы уже пересозданы на новом образе: без подъёма
# отката стек молча живёт на том, что ловушка назвала откаченным.
bump "и3 живой"
conf ""
run_deploy
remember_live
itog "0" "$RC" "И3 условие: живой выкат $LIVE_TAG"
for I3 in "STACK_UP_CMD задан|" "STACK_UP_CMD пуст|STACK_UP_CMD=\"\""; do
    NAME="И3 ${I3%%|*}"
    bump "и3 ${I3%%|*}"
    NEW12=$(head_sha | cut -c1-12)
    conf "STACK_PROXY_RELOAD_CMD=\"sleep 20\"
${I3#*|}"
    : > "$STAND/poryadok"; rm -f "$LOCKF"; sleep 1
    env "${STAND_ENV[@]}" bash "$COPY/deploy.sh" > "$STAND/o49-i3.out" 2>&1 &
    DPID=$!
    for _ in $(seq 1 60); do grep -q '^peresozdanie$' "$STAND/poryadok" && break; sleep 0.5; done
    sleep 2
    kill -TERM "$DPID"
    wait "$DPID"; I3RC=$?
    I3OUT=$(cat "$STAND/o49-i3.out")
    itog "да" "$(grep -q '^peresozdanie$' "$STAND/poryadok" && echo да || echo нет)" "$NAME условие: службы пересозданы до сигнала"
    itog "143" "$I3RC" "$NAME: код 143"
    if [ -z "${I3#*|}" ]; then
        itog "да" "$(sed -n '/^peresozdanie$/,$p' "$STAND/poryadok" | grep -qx otkat && echo да || echo нет)" \
             "$NAME: после пересоздания звался подъём отката (порядок: $(tr '\n' ' ' < "$STAND/poryadok"))"
    else
        itog "да" "$(has "$I3OUT" "службы на :$NEW12")" "$NAME: вывод называет «службы на :$NEW12»"
        itog "да" "$(has "$I3OUT" ":latest на $LIVE_TAG")" "$NAME: вывод называет «:latest на $LIVE_TAG»"
        itog "да" "$(has "$I3OUT" "подними STACK_UP_CMD руками")" "$NAME: вывод велит «подними STACK_UP_CMD руками»"
    fi
done

# ═════ Фаза 4: X по диапазону «тег прошлого ЖИВОГО выката..sha» до B и G; этапы и коммиты в итоге ═════
# Спека §3 стр. 1 (одобрена кнопкой vqkvH9tp — новое слово владельца об И-3 на выкате), §5.1,
# §5.3 п. 8, §7 стр. 4. «Прошлый тег» — тег последней строки «выкачено» журнала итогов (как у
# отката), а не последний тег deploy-*: тег ставится и проваленному выкату. Тег вне истории HEAD
# (force-push продукта) или без объекта — диапазон не строится, судится весь репозиторий, и это
# сказано словами «весь репозиторий». Контракт записи итога «выкачено» (data):
#   этапы  — объект {S, X, B, T, D, G, K, R, P, M: секунды числом}; |сумма − стена прохода| ≤ 5 с;
#   коммиты — список строк по коммиту «<sha ≥7 знаков> <тема>», диапазон «прошлый тег..sha»;
#   наборы — «N из M»; итоговая строка «deploy ok: …» называет «этапы» и «наборы N из M».
# Прошлые выкаты, которых дверь не делала, проба пишет в журнал итогов сама — строкой того же
# вида, что пишет record_result, и аннотированным тегом в репозитории продукта.
f4_val() { printf '%s%s' "$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 23)" "$((RANDOM % 10))"; }
f4_secret() {  # $1 — файл, $2 — тема коммита; значение — в F4_VAL
    F4_VAL=$(f4_val)
    printf 'api_key = "%s"\n' "$F4_VAL" > "$PRODUCT/$1"
    g -C "$PRODUCT" add -A
    g -C "$PRODUCT" commit -qm "$2"
}
f4_tag() {  # $1 — коммит; печатает имя тега выката, поставленного на него
    local name
    sleep 1
    name="deploy-$(date +%Y%m%d-%H%M%S)"
    g -C "$PRODUCT" tag -a "$name" -m "выкат $name" "$1"
    printf '%s' "$name"
}
f4_result() {  # $1 тег, $2 полный sha, $3 итог словами, $4 ok|error
    python3 -I - "$RESULTS" "$@" <<'PY'
import datetime, json, sys
path, tag, sha, words, outcome = sys.argv[1:6]
ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
rec = {"v": 1, "ts": ts, "src": "scripts/deploy.sh", "event": "deploy.result", "outcome": outcome,
       "task": "-", "task_from": "env", "session": None,
       "data": {"тег": tag, "sha": sha[:12], "итог": words}}
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
PY
}
# Вердикты последней строки журнала итогов: «итог|этапы|сумма|коммиты|наборы» по строке.
f4_record() {  # $1 — стена прохода, с; $2 — файл ожидаемых коммитов «<полный sha> <тема>»; $3 — sha выката
    python3 -I - "$RESULTS" "$1" "$2" "$3" <<'PY'
import json, sys
path, wall, want_path, head = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4]
data = {}
for line in open(path, encoding="utf-8"):
    try:
        data = json.loads(line).get("data") or {}
    except ValueError:
        continue
# Строку пишет этот проход: sha — выкатываемый, а не прошлый живой, подложенный пробой.
fresh = str(data.get("sha", "")) == head[:12]
print("итог", "да" if fresh and str(data.get("итог", "")).startswith("выкачено") else "нет")
stages = data.get("этапы")
keys = ["S", "X", "B", "T", "D", "G", "K", "R", "P", "M"]
ok = isinstance(stages, dict) and all(
    isinstance(stages.get(k), (int, float)) and not isinstance(stages.get(k), bool) and stages[k] >= 0
    for k in keys)
print("этапы", "да" if ok else f"нет: {stages!r}"[:120])
total = sum(stages[k] for k in keys) if ok else None
print("сумма", "да" if ok and abs(total - wall) <= 5 else f"нет: сумма {total}, стена {wall:g}")
want = [l.rstrip("\n").split(" ", 1) for l in open(want_path, encoding="utf-8") if l.strip()]
got = data.get("коммиты")


def listed(full, subject):
    for entry in got:
        head = str(entry).split(" ", 1)[0]
        if len(head) >= 7 and full.startswith(head) and subject in str(entry):
            return True
    return False


ok = isinstance(got, list) and len(got) == len(want) and all(listed(f, s) for f, s in want)
print("коммиты", "да" if ok else f"нет: ждали {len(want)}, вышло {got!r}"[:160])
print("наборы", str(data.get("наборы", "<нет>")))
PY
}
f4_field() { printf '%s\n' "$F4_REC" | sed -n "s/^$1 //p"; }
f4_whole() { has "$VYVOD" "весь репозиторий"; }
final_line() { printf '%s\n' "$VYVOD" | grep -F 'deploy ok:' | tail -1; }

echo "── Ф4-1. секрет только в старом, уже выкаченном коммите — выкат идёт (диапазон от живого тега) ──"
f4_secret star-sekret.py "ф4: старый секрет, уже на проде"
C_OLD=$(head_sha)
OLD_VAL=$F4_VAL
L_OLD=$(f4_tag "$C_OLD")
f4_result "$L_OLD" "$C_OLD" "выкачено: проба — прошлый живой выкат" ok
bump "ф4: чистый один"
bump "ф4: чистый два"
git -C "$PRODUCT" log --format='%H %s' "$C_OLD..HEAD" > "$STAND/f4-kommity"
itog "2" "$(grep -c . "$STAND/f4-kommity")" "Ф4-1 условие: после живого тега $L_OLD два коммита"
conf "$CHECKS_KEY"
run_deploy
F4_REC=$(f4_record "$WALL" "$STAND/f4-kommity" "$(head_sha)")
itog "0" "$RC" "Ф4-1 код 0: старый секрет вне диапазона $L_OLD..HEAD"
itog "нет" "$(has "$VYVOD" "${OLD_VAL:3:12}")" "Ф4-1 значение старого секрета в вывод не попало"
itog "да" "$(f4_field итог)" "Ф4-1 итог этого прохода (sha HEAD) в журнале итогов — «выкачено»"
itog "да" "$(f4_field этапы)" "N запись несёт этапы {S, X, B, T, D, G, K, R, P, M: с}"
itog "да" "$(f4_field сумма)" "N |сумма этапов − стена прохода ${WALL} с| ≤ 5 с"
itog "да" "$(f4_field коммиты)" "N коммиты $L_OLD..sha перечислены по строке (sha и тема)"
itog "1 из 1" "$(f4_field наборы)" "N запись несёт «наборы 1 из 1»"
itog "да" "$(has "$(final_line)" "наборы 1 из 1")" "N итоговая строка называет «наборы 1 из 1»"
itog "да" "$(has "$(final_line)" "этапы")" "N итоговая строка называет этапы"

echo "── Ф4-2. секрет в новом коммите после живого тега — отказ до сборки и ворот ──"
# Новый коммит уходит в хранилище мимо двери (main хранилища его несёт), а последний тег
# deploy-* — проваленный выкат на нём: диапазон от main или от последнего тега секрет не увидел бы.
f4_secret novyj-sekret.py "ф4: новый секрет"
C_NEW=$(head_sha)
NEW_VAL=$F4_VAL
git -C "$PRODUCT" push -q origin main
L_FAIL=$(f4_tag "$C_NEW")
f4_result "$L_FAIL" "$C_NEW" "ВЫКАТ ПРОВАЛЕН, стек на прошлой версии $L_OLD" error
bump "ф4: после проваленного"
itog "да" "$([ "$(last_tag)" = "$L_FAIL" ] && echo да || echo нет)" \
     "Ф4-2 условие: последний тег deploy-* — проваленный $L_FAIL на коммите секрета"
itog "да" "$([ "$(git -C "$BARE" rev-parse main)" = "$C_NEW" ] && echo да || echo нет)" \
     "Ф4-2 условие: main хранилища несёт коммит секрета"
conf "$CHECKS_KEY"
TAGS_DO=$(tags_n)
run_deploy
itog "1" "$RC" "Ф4-2 код 1"
itog "да" "$(has "$VYVOD" "novyj-sekret.py")" "Ф4-2 отказ называет novyj-sekret.py"
itog "да" "$(has "$VYVOD" "секреты в репозитории")" "Ф4-2 отказ словами «секреты в репозитории»"
itog "нет" "$(has "$VYVOD" "${NEW_VAL:3:12}")" "Ф4-2 значение секрета в вывод не попало"
itog "0" "$(sborok)" "Ф4-2 сборок 0 (X до B)"
itog "0" "$(suites_ran)" "Ф4-2 наборы не звались"
itog "нет" "$(gate_called)" "Ф4-2 ворота не звались (X до G)"
itog "нет" "$(lock_taken)" "Ф4-2 deploy.lock не взят"
itog "$TAGS_DO" "$(tags_n)" "Ф4-2 тег не поставлен"
run_deploy --проверка
itog "1" "$RC" "Ф4-2 --проверка: код 1 (X идёт и без выката)"
itog "0" "$(sborok)" "Ф4-2 --проверка: сборок 0"

echo "── Ф4-3. живого выката нет (первый выкат) — судится весь репозиторий ──"
g -C "$PRODUCT" rm -q novyj-sekret.py
g -C "$PRODUCT" commit -qm "ф4: новый секрет убран"
mv "$RESULTS" "$RESULTS.f4"
TAGS_DO=$(tags_n)
run_deploy
mv "$RESULTS.f4" "$RESULTS"
itog "1" "$RC" "Ф4-3 код 1"
itog "да" "$(has "$VYVOD" "star-sekret.py")" "Ф4-3 назван star-sekret.py из давнего коммита"
itog "да" "$(f4_whole)" "Ф4-3 сказано «весь репозиторий»"
itog "0" "$(sborok)" "Ф4-3 сборок 0"
itog "нет" "$(gate_called)" "Ф4-3 ворота не звались"
itog "$TAGS_DO" "$(tags_n)" "Ф4-3 тег не поставлен"

echo "── Ф4-4. тег живого выката вне истории (force-push продукта) — весь репозиторий, словами ──"
# Коммит тега — сирота с тем же деревом, что HEAD: дифф «тег..HEAD» пуст, и диапазон назвал бы
# чистым дерево, где лежит star-sekret.py.
C_ORPHAN=$(g -C "$PRODUCT" commit-tree "HEAD^{tree}" -m "ф4: история до force-push")
L_ORPHAN=$(f4_tag "$C_ORPHAN")
f4_result "$L_ORPHAN" "$C_ORPHAN" "выкачено: проба — выкат до force-push" ok
bump "ф4: после force-push"
itog "нет" "$(git -C "$PRODUCT" merge-base --is-ancestor "$C_ORPHAN" HEAD && echo да || echo нет)" \
     "Ф4-4 условие: коммит тега $L_ORPHAN не предок HEAD"
TAGS_DO=$(tags_n)
run_deploy
itog "1" "$RC" "Ф4-4 код 1"
itog "да" "$(has "$VYVOD" "star-sekret.py")" "Ф4-4 назван star-sekret.py"
itog "да" "$(f4_whole)" "Ф4-4 сказано «весь репозиторий»"
itog "0" "$(sborok)" "Ф4-4 сборок 0"
itog "нет" "$(gate_called)" "Ф4-4 ворота не звались"
itog "$TAGS_DO" "$(tags_n)" "Ф4-4 тег не поставлен"
echo "── Ф4-4б. тега живого выката нет в репозитории, sha не найден — весь репозиторий, словами ──"
f4_result "deploy-20000101-000000" "0123456789abcdef0123456789abcdef01234567" "выкачено: проба — тег удалён" ok
bump "ф4: тег удалён"
run_deploy
itog "1" "$RC" "Ф4-4б код 1"
itog "да" "$(has "$VYVOD" "star-sekret.py")" "Ф4-4б назван star-sekret.py"
itog "да" "$(f4_whole)" "Ф4-4б сказано «весь репозиторий»"
itog "0" "$(sborok)" "Ф4-4б сборок 0"

printf '\nЭТАПЫ-ВЫКАТА: путей %d, неудач %d\n' "$paths" "$fails"
[ "$fails" -eq 0 ]
