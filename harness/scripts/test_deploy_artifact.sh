#!/usr/bin/env bash
# Проба: выкат продукта катит ОДНУ сборку, адресуемую тегом образа :<sha12>, и
# откатывается на образ ПРОШЛОГО ВЫКАТА, а не на снимок «что было в :latest»
# (спека эталонного выката §3 стр. 5, 7, 8; §5.2; §5.3 п. 4; план, фаза 2).
#
# Что охраняет. Снимок :previous — это «что лежало в :latest перед сборкой», а не
# «что было выкачено»: ручной ретег, чужая сборка или упавший прошлый выкат кладут
# в :latest образ, которого на проде не было, и откат поднимал его. Тег :<sha12>
# связывает образ с коммитом, по которому стоит тег выката: откат берёт образ
# прошлого ЖИВОГО выката — последнего тега deploy-*, у которого в журнале итогов
# deploy-results.jsonl итог «выкачено» (§5.3 п. 7, 8). Тег ставится до сборки, и у
# проваленного выката он тоже есть: он остаётся адресом аварии, но отката на него
# нет. Итогов нет вовсе (первый выкат после обновления) — откат на снимок :previous,
# словами. Проверенное и едущее — одна сборка. Предмет --ветка берёт sha из
# хранилища продукта (STACK_REMOTE), а не из того, что лежит в дереве; пуш ветки —
# только fast-forward: дверь не имеет права переписать main чужого хранилища.
#
# Стенд свой: паспорт, харнес, продукт и голое хранилище продукта (bare) в tmp;
# подставной docker С ПАМЯТЬЮ — файл «образ:тег → id»: build даёт новый id, tag
# переносит id, image inspect и inspect --format {{.Id}} читают его. Ворота —
# заглушка, которая открывает всегда и пишет, как её звали: проба судит образы и
# предмет, а не кнопку. Боевой паспорт не читается (дверь — копия вне PROJECT_DIR
# машины, паспорт ей называет HARNESS_DOOR_STAND_HOME). Механизм — deploy.sh и
# lib/door_*.sh рядом с пробой (мутации — из копии дерева).
#
# Раунд ревью фазы 2 (случаи C1, C2, I1–I4, M2–M5, M7 в конце файла): откат судится по
# id образа живого выката, а не по тегу :<sha12> — повторная сборка того же sha тег
# перезаписывает; поэтому docker стенда находит образ и по id («sha256:…», 64 hex, как
# настоящий), а журнал итогов обязан нести id. Ворота-заглушка закрываются по
# PROBA_GATE_RC=1. Два прохода разом (замок дерева продукта) — первый фоном.
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
has() { printf '%s' "$1" | grep -qF -- "$2" && echo да || echo нет; }

STAND=$(mktemp -d -t proba-deploy-artifact-XXXXXX)
trap 'rm -rf "$STAND"' EXIT

HARNESS="$STAND/harnes"
PRODUCT="$STAND/produkt"
BARE="$STAND/hranilishche.git"
WORK="$STAND/rabochij-klon"
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
for repo in "$HARNESS" "$PRODUCT"; do
    printf 'стенд\n' > "$repo/README"
    git -C "$repo" init -q -b main
    g -C "$repo" add -A
    g -C "$repo" commit -qm "стенд"
done
git init -q --bare -b main "$BARE"
git -C "$PRODUCT" remote add origin "$BARE"
git -C "$PRODUCT" push -q origin main
commit_product() { g -C "$PRODUCT" commit -q --allow-empty -m "$1"; }

# Подставной docker с памятью. Ссылка без тега — :latest, как у docker.
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
        # id как у docker (64 hex): писатель событий прячет такие строки, и запись id в
        # журнал итогов обязана это пережить.
        id="sha256:$(printf 'sborka-%s' "$n" | sha256sum | cut -c1-64)"; tagged=0
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
            *) exit 0 ;;
        esac ;;
    inspect) shift; inspect "$@"; exit 0 ;;
    *) exit 0 ;;
esac
SH
chmod +x "$FAKEBIN/docker"
STATE="$STAND/docker-state"
: > "$STATE"
id_of() { PATH="$FAKEBIN:$PATH" DOCKER_STATE="$STATE" docker image inspect --format '{{.Id}}' "$1" 2>/dev/null || true; }
set_id() {  # $1=ссылка $2=id — подложить образ мимо выката (ручной ретег, чужая сборка)
    awk -v r="$1" '$1 != r' "$STATE" > "$STATE.new"; printf '%s %s\n' "$1" "$2" >> "$STATE.new"; mv "$STATE.new" "$STATE"
}
same_id() {  # «да», если оба id непусты и равны: пустой == пустой — не равенство образов
    [ -n "$1" ] && [ "$1" = "$2" ] && echo да || echo нет
}

cp "$HERE/deploy.sh" "$HERE/check-secrets.sh" "$COPY/"
cp -r "$HERE/lib" "$COPY/"
cat > "$COPY/deploy_guard.py" <<'PY'
# Ворота-заглушка пробы образа: открывают и пишут, как их звали; PROBA_GATE_RC=1 — «Да» нет.
import os, sys
with open(os.environ["PROBA_GATE_ARGV"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
rc = int(os.environ.get("PROBA_GATE_RC") or 0)
print("ворота-заглушка: " + ("закрыто" if rc else "открыто"))
sys.exit(rc)
PY

# Сборка кладёт образ по IMAGE_TAG (контракт с compose продукта); без IMAGE_TAG — в
# :latest, как compose, не знающий ключа. Одинарные кавычки: конфиг исполняется как
# код, и ${IMAGE_TAG} должен дойти до сборки, а не раскрыться при чтении конфига.
BUILD_TAGGED="docker build -t proba-api:\${IMAGE_TAG:-latest} . && echo sborka >> $STAND/poryadok"
BUILD_LATEST="docker build -t proba-api:latest . && echo sborka >> $STAND/poryadok"
conf() {  # $1 — сборка, $2 — команда живости, $3 — добавочные строки
    {
        printf 'PRODUCT_DIR="proba"\nPRODUCT_REPO="%s"\nBACKUP_DIR="%s/kopii"\n' "$PRODUCT" "$STAND"
        printf 'DEPLOY_HEALTH_WAIT_SEC="4"\nSTACK_IMAGES="proba-api"\n'
        printf "STACK_BUILD_CMD='%s'\n" "$1"
        printf 'STACK_HEALTH_CMD="%s"\nSTACK_UP_CMD="echo otkat >> %s/poryadok"\n' "$2" "$STAND"
        printf '%s\n' "${3:-}"
    } > "$STAND/harness.conf"
}

VYVOD=""
RC=0
WALL=0
run_deploy() {  # $@ — ключи deploy.sh; журналы docker, ворот и порядка — на этот запуск
    : > "$STAND/gate-argv"; : > "$STAND/poryadok"; : > "$STAND/docker.log"
    sleep 1   # тег выката именуется по секунде
    local t0=$SECONDS
    VYVOD=$(PATH="$FAKEBIN:$PATH" DOCKER_STATE="$STATE" DOCKER_JOURNAL="$STAND/docker.log" \
            PROBA_GATE_ARGV="$STAND/gate-argv" HARNESS_DOOR_STAND_HOME="$STAND" \
            HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
            HARNESS_LOG_DIR="$STAND/zhurnal" \
            timeout 300 bash "$COPY/deploy.sh" "$@" 2>&1)
    RC=$?
    WALL=$((SECONDS - t0))
    printf '%s\n' "$VYVOD" | sed 's/^/      /' | tail -5
}
sborok() { grep -c '^sborka$' "$STAND/poryadok" 2>/dev/null || true; }
gate_called() { grep -q . "$STAND/gate-argv" && echo да || echo нет; }
last_tag() { git -C "$PRODUCT" tag -l 'deploy-*' --sort=-refname | head -1; }
sha12_of() { git -C "$PRODUCT" rev-list -n1 "$1" 2>/dev/null | cut -c1-12; }
head12() { git -C "$PRODUCT" rev-parse HEAD | cut -c1-12; }
docker_tags() { grep -c '^tag ' "$STAND/docker.log" 2>/dev/null || true; }
RESULTS="$STAND/zhurnal/deploy-results.jsonl"
# Итог выката по тегу из журнала итогов стенда: «<да|нет: sha совпал> <итог>». sha в
# строке — полный или sha12 (не короче), итог — data.итог. Живой итог начинается с
# «выкачено» (п. 8; п. 7 — «выкачено с оговоркой: <шаг>»). Строки нет — «нет-строки».
result_of() {  # $1=тег $2=полный sha
    python3 -I - "$RESULTS" "$1" "$2" <<'PY'
import json, sys
path, tag, full = sys.argv[1:4]
found = None
try:
    for line in open(path, encoding="utf-8"):
        try:
            data = json.loads(line).get("data", {})
        except ValueError:
            continue
        if data.get("тег") == tag:
            found = data
except OSError:
    pass
if found is None:
    print("нет-строки")
else:
    sha = str(found.get("sha", ""))
    ok = "да" if len(sha) >= 12 and full.startswith(sha) else "нет"
    print(ok, found.get("итог", ""))
PY
}
is_live() { case "$1" in "да выкачено"*) echo да ;; *) echo нет ;; esac; }
is_failed() {  # итог записан, sha совпал, и он НЕ живой
    case "$1" in "да выкачено"*) echo нет ;; "да "?*) echo да ;; *) echo нет ;; esac
}

ZHIVO="true"
MERTVO="false"

echo "── 1. два выката подряд — :latest указывает на образ :<sha12> второго ──"
conf "$BUILD_TAGGED" "$ZHIVO"
run_deploy
itog "0" "$RC" "1 первый выкат выходит нулём"
itog "1" "$(sborok)" "3 первый выкат: сборок 1"
A12=$(head12)
ID_A=$(id_of "proba-api:$A12")
TAG_A=$(last_tag)
R_A=$(result_of "$TAG_A" "$(git -C "$PRODUCT" rev-parse HEAD)")
itog "да" "$(is_live "$R_A")" "итог: в deploy-results.jsonl строка $TAG_A, sha первого, итог «выкачено» (вышло: $R_A)"
itog "да" "$([ -n "$ID_A" ] && echo да || echo нет)" "1 после первого выката образ proba-api:$A12 есть"
commit_product "второй"
run_deploy
itog "0" "$RC" "1 второй выкат выходит нулём"
itog "1" "$(sborok)" "3 второй выкат: сборок 1"
B12=$(head12)
ID_B=$(id_of "proba-api:$B12")
TAG_B=$(last_tag)
itog "да" "$(is_live "$(result_of "$TAG_B" "$(git -C "$PRODUCT" rev-parse HEAD)")")" \
     "итог: строка $TAG_B второго выката — «выкачено»"
itog "да" "$(same_id "$(id_of proba-api:latest)" "$ID_B")" "1 id :latest == id :$B12 второго выката"
itog "да" "$([ -n "$ID_B" ] && [ "$ID_A" != "$ID_B" ] && echo да || echo нет)" \
     "1 стенд: у второго выката свой образ (id :$A12 ≠ id :$B12)"

echo "── 2. третий выкат, живость мертва — откат на образ прошлого ВЫКАТА, не на снимок ──"
# :latest и :previous подложены чужим образом (ручной ретег до выката): снимок
# :latest → :previous возьмёт его, и откат на снимок поднял бы образ, которого не
# было ни в одном выкате. Откат по тегу прошлого выката даёт образ второго.
# Два коммита с прошлого выката: HEAD~1 — не прошлый выкат, и откат «на образ
# родительского коммита» вместо «на образ тега» здесь краснеет (образа HEAD~1 нет).
commit_product "третий-а"
commit_product "третий"
set_id "proba-api:latest" "sha256:chuzhoj"
set_id "proba-api:previous" "sha256:chuzhoj"
conf "$BUILD_TAGGED" "$MERTVO"
run_deploy
WALL_DEAD=$WALL
itog "1" "$RC" "2 выкат с мёртвой живостью — код 1"
itog "1" "$(sborok)" "3 выкат с откатом: сборок 1 (откат не собирает)"
itog "да" "$(has "$(cat "$STAND/poryadok")" "otkat")" "2 подъём отката звался"
itog "да" "$(same_id "$(id_of proba-api:latest)" "$ID_B")" "2 после отката id :latest == id :$B12 (прошлый выкат)"
itog "нет" "$([ "$(id_of proba-api:latest)" = "sha256:chuzhoj" ] && echo да || echo нет)" \
     "2 откат не поднял подложенный образ снимка"
C12=$(head12)
ID_C=$(id_of "proba-api:$C12")
TAG_C=$(last_tag)
R_C=$(result_of "$TAG_C" "$(git -C "$PRODUCT" rev-parse HEAD)")
itog "да" "$(is_failed "$R_C")" "итог: строка $TAG_C проваленного выката — итог провала, не «выкачено» (вышло: $R_C)"
itog "да" "$([ -n "$TAG_C" ] && [ "$TAG_C" != "$TAG_B" ] && echo да || echo нет)" \
     "2 тег проваленного выката $TAG_C остаётся (адрес аварии)"

echo "── 2б. после провала — снова провал: откат на последний ЖИВОЙ выкат, не на тег проваленного ──"
# A и B выкачены, C провален (откат на B). D тоже проваливается: новейший тег до D —
# C, и «прошлый тег» (sed -n 2p после тега D) поднял бы образ C, которого живым не
# было. Образ :<sha12 C> на стенде есть (сборка C прошла) — откат на него не упадёт
# молча, а поднимет не тот образ. Снимок снова подложен: откат на :previous тоже виден.
itog "да" "$([ -n "$ID_C" ] && [ "$ID_C" != "$ID_B" ] && echo да || echo нет)" \
     "2б условие: у проваленного C свой образ :$C12 (≠ образа B)"
commit_product "четвёртый-провал"
set_id "proba-api:latest" "sha256:chuzhoj"
set_id "proba-api:previous" "sha256:chuzhoj"
conf "$BUILD_TAGGED" "$MERTVO"
run_deploy
itog "1" "$RC" "2б второй провал подряд — код 1"
itog "да" "$(has "$(cat "$STAND/poryadok")" "otkat")" "2б подъём отката звался"
itog "да" "$(same_id "$(id_of proba-api:latest)" "$ID_B")" "2б после отката id :latest == id :$B12 (последний живой — B)"
itog "нет" "$(same_id "$(id_of proba-api:latest)" "$ID_C")" "2б откат не поднял образ проваленного C :$C12"
itog "да" "$(is_failed "$(result_of "$(last_tag)" "$(git -C "$PRODUCT" rev-parse HEAD)")")" \
     "2б итог: строка проваленного D — итог провала"

echo "── 6. потолок живости: DEPLOY_HEALTH_WAIT_SEC=4 — проход мёртвой живости с откатом короче 30 с ──"
itog "да" "$([ "$WALL_DEAD" -lt 30 ] && echo да || echo нет)" "6 стена выката с мёртвой живостью ${WALL_DEAD} с < 30 с"

echo "── 5. переходный случай: живых итогов нет (первый выкат после обновления) — откат на снимок, словами ──"
# Первый выкат после обновления: теги deploy-* есть, журнала итогов ещё нет. Новейший
# тег — проваленного D, его образ :<sha12> на стенде есть: «прошлый тег» поднял бы
# его, а не снимок.
PREV_TAG=$(last_tag)
PREV12=$(sha12_of "$PREV_TAG")
itog "да" "$([ -n "$PREV12" ] && [ -n "$(id_of "proba-api:$PREV12")" ] && echo да || echo нет)" \
     "5 условие: новейший тег $PREV_TAG есть, и его образ :$PREV12 на стенде есть"
[ -f "$RESULTS" ] && mv "$RESULTS" "$RESULTS.do-obnovleniya"
commit_product "пятый-провал"
set_id "proba-api:latest" "sha256:snimok"
set_id "proba-api:previous" "sha256:snimok"
conf "$BUILD_TAGGED" "$MERTVO"
run_deploy
itog "1" "$RC" "5 код 1"
itog "sha256:snimok" "$(id_of proba-api:latest)" "5 после отката :latest — снимок, который был до выката"
itog "да" "$(has "$VYVOD" "живого выката")" "5 сказано: живого выката в журнале итогов нет"
itog "да" "$(has "$VYVOD" "откат на снимок")" "5 сказано: откат на снимок"

echo "── 1б. compose не знает IMAGE_TAG (сборка кладёт :latest) — образ помечен :<sha12> ретегом ──"
commit_product "пятый"
conf "$BUILD_LATEST" "$ZHIVO"
run_deploy
E12=$(head12)
itog "0" "$RC" "1б выкат выходит нулём"
itog "да" "$(same_id "$(id_of "proba-api:$E12")" "$(id_of proba-api:latest)")" "1б id :$E12 == id :latest"
itog "да" "$(has "$VYVOD" "IMAGE_TAG")" "1б вывод называет IMAGE_TAG (compose его не знает)"

echo "── край: STACK_IMAGES пуст — ни ретега, ни отката ──"
commit_product "шестой"
conf "$BUILD_TAGGED" "$MERTVO" 'STACK_IMAGES=""'
run_deploy
itog "1" "$RC" "пусто: мёртвая живость — код 1"
itog "0" "$(docker_tags)" "пусто: ни одного docker tag"
itog "да" "$(has "$VYVOD" "снимка для отката нет")" "пусто: сказано «снимка для отката нет»"

echo "── 4. без STACK_REMOTE — HEAD дерева, в хранилище ничего не уехало ──"
itog "" "$(git -C "$BARE" tag -l 'deploy-*')" "4 STACK_REMOTE пуст: тегов выката в хранилище нет"
itog "$(git -C "$PRODUCT" rev-parse HEAD)" "$(git -C "$PRODUCT" rev-parse "$(last_tag)^{commit}")" \
     "4 STACK_REMOTE пуст: тег на HEAD дерева"

# Ветки в хранилище — из рабочего клона, как их кладёт разработчик продукта.
git -C "$PRODUCT" push -q origin main
git clone -q "$BARE" "$WORK"
vetka() {  # $1=ветка $2=от чего ответвить (ref клона) → sha вершины
    git -C "$WORK" fetch -q origin
    git -C "$WORK" checkout -q -B "$1" "$2"
    printf '%s\n' "$1" > "$WORK/$1.txt"
    g -C "$WORK" add -A
    g -C "$WORK" commit -qm "ветка $1"
    git -C "$WORK" push -q origin "$1"
    git -C "$WORK" rev-parse HEAD
}
# --ветка катит только при отдельном дереве прода (К-42): клон двери переключается на ветку,
# а службы смотрят в PRODUCT_PATH, куда выкат доходит pull-ом после «Да».
git clone -q "$BARE" "$STAND/prod"
REMOTE_CONF="STACK_REMOTE=\"origin\"
STACK_BRANCH=\"main\"
PRODUCT_PATH=\"$STAND/prod\""
MAIN0=$(git -C "$BARE" rev-parse main)

echo "── 4. --ветка X при STACK_REMOTE: предмет — вершина X в хранилище, тег уехал в хранилище ──"
X_TIP=$(vetka vetka-x origin/main)
itog "нет" "$([ "$(git -C "$PRODUCT" rev-parse HEAD)" = "$X_TIP" ] && echo да || echo нет)" \
     "4 условие: дерево продукта НЕ на вершине X (предмет берётся из хранилища)"
conf "$BUILD_TAGGED" "$ZHIVO" "$REMOTE_CONF"
run_deploy --ветка vetka-x
TAG_X=$(last_tag)
itog "0" "$RC" "4 выкат --ветка vetka-x выходит нулём"
itog "да" "$(grep -qF -- "--выкат $PRODUCT $X_TIP" "$STAND/gate-argv" && echo да || echo нет)" \
     "4 ворота судят вершину X (${X_TIP:0:8})"
itog "$X_TIP" "$(git -C "$PRODUCT" rev-parse "$TAG_X^{commit}" 2>/dev/null)" "4 тег выката на вершине X"
itog "$X_TIP" "$(git -C "$BARE" rev-parse "$TAG_X^{commit}" 2>/dev/null)" "4 тег выката $TAG_X есть в хранилище"
itog "$X_TIP" "$(git -C "$BARE" rev-parse main)" "4 X — fast-forward над main: main хранилища продвинут до X"
itog "да" "$(same_id "$(id_of proba-api:latest)" "$(id_of "proba-api:${X_TIP:0:12}")")" "4 :latest — образ :${X_TIP:0:12}"

echo "── край: ветки нет в хранилище — отказ до ворот, ветка названа ──"
conf "$BUILD_TAGGED" "$ZHIVO" "$REMOTE_CONF"
TAGS_DO=$(git -C "$PRODUCT" tag -l 'deploy-*' | grep -c .)
run_deploy --ветка net-takoj-vetki
itog "1" "$RC" "нет ветки: код 1"
itog "да" "$(has "$VYVOD" "net-takoj-vetki")" "нет ветки: отказ называет ветку"
itog "нет" "$(gate_called)" "нет ветки: ворота не звались"
itog "0" "$(sborok)" "нет ветки: сборок 0"
itog "$TAGS_DO" "$(git -C "$PRODUCT" tag -l 'deploy-*' | grep -c .)" "нет ветки: тег не поставлен"

echo "── край: --ветка при незакоммиченной правке в дереве продукта — отказ, дерево не тронуто ──"
Y_TIP=$(vetka vetka-gryaz origin/main)
HEAD_DO=$(git -C "$PRODUCT" rev-parse HEAD)
printf 'правка руками\n' >> "$PRODUCT/README"
cp "$PRODUCT/README" "$STAND/README-gryaz"
run_deploy --ветка vetka-gryaz
itog "1" "$RC" "грязь: код 1"
itog "да" "$(has "$VYVOD" "незакоммичен")" "грязь: причина названа — незакоммиченное"
itog "нет" "$(gate_called)" "грязь: ворота не звались"
itog "$HEAD_DO" "$(git -C "$PRODUCT" rev-parse HEAD)" "грязь: HEAD дерева не сдвинут (не на ${Y_TIP:0:8})"
itog "да" "$(cmp -s "$PRODUCT/README" "$STAND/README-gryaz" && echo да || echo нет)" "грязь: правка в дереве цела"
printf 'стенд\n' > "$PRODUCT/README"
itog "" "$(git -C "$PRODUCT" status --porcelain)" "грязь: стенд вернул дерево чистым"

echo "── M4. ветка НЕ fast-forward над main — отказ до ворот (§5.3 п. 4), main и дерево не тронуты ──"
# Катить не-ff нельзя: на проде оказался бы код, которого нет в main хранилища, и
# следующий выкат main молча снял бы его. Отказ — до ворот, тега нет ни здесь, ни там;
# дерево продукта не остаётся на чужой ветке (следующий выкат HEAD покатил бы её).
MAIN_DO=$(git -C "$BARE" rev-parse main)
NFF_TIP=$(vetka vetka-ne-ff "$MAIN0~1")
itog "нет" "$(git -C "$BARE" merge-base --is-ancestor "$MAIN_DO" "$NFF_TIP" && echo да || echo нет)" \
     "не-ff: условие — main хранилища не предок вершины ветки"
conf "$BUILD_TAGGED" "$ZHIVO" "$REMOTE_CONF"
HEAD_DO=$(git -C "$PRODUCT" rev-parse HEAD)
TAGS_DO=$(git -C "$PRODUCT" tag -l 'deploy-*' | grep -c .)
run_deploy --ветка vetka-ne-ff
itog "$MAIN_DO" "$(git -C "$BARE" rev-parse main)" "не-ff: main хранилища остался ${MAIN_DO:0:8}"
# «не fast-forward» — слова двери; git на отвергнутый пуш скажет «non-fast-forward».
itog "да" "$(has "$VYVOD" "не fast-forward")" "не-ff: причина названа — не fast-forward над main"
itog "1" "$RC" "M4 не-ff: отказ — код 1"
itog "нет" "$(gate_called)" "M4 не-ff: ворота не звались (отказ до ворот)"
itog "$TAGS_DO" "$(git -C "$PRODUCT" tag -l 'deploy-*' | grep -c .)" "M4 не-ff: тег выката не поставлен"
itog "" "$(git -C "$BARE" tag --points-at "$NFF_TIP")" "M4 не-ff: в хранилище тега на вершине ветки нет"
itog "$HEAD_DO" "$(git -C "$PRODUCT" rev-parse HEAD)" "M4 не-ff: дерево продукта не осталось на вершине ветки"

echo "── край: хранилище отказало в теге — тег локальный есть, в хранилище нет, сказано; дальше в прод не идёт ──"
Z_TIP=$(vetka vetka-z origin/main)
cat > "$BARE/hooks/pre-receive" <<'SH'
#!/bin/sh
while read -r old new ref; do
    case "$ref" in refs/tags/*) echo "проба: хранилище тегов не принимает" >&2; exit 1 ;; esac
done
exit 0
SH
chmod +x "$BARE/hooks/pre-receive"
MAIN_DO=$(git -C "$BARE" rev-parse main)
conf "$BUILD_TAGGED" "$ZHIVO" "$REMOTE_CONF
STACK_DB_DUMP_CMD=\"head -c 4096 /dev/zero\"
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok\"
STACK_RELEASE_CMD=\"echo peresozdanie >> $STAND/poryadok\""
run_deploy --ветка vetka-z
TAG_Z=$(last_tag)
itog "1" "$RC" "тег отказан: код 1"
itog "$Z_TIP" "$(git -C "$PRODUCT" rev-parse "$TAG_Z^{commit}" 2>/dev/null)" "тег отказан: локальный тег на вершине Z есть"
itog "" "$(git -C "$BARE" tag --points-at "$Z_TIP")" "тег отказан: в хранилище тега нет"
itog "да" "$(printf '%s\n' "$VYVOD" | grep -F -- "$TAG_Z" | grep -q 'хранилищ' && echo да || echo нет)" \
     "тег отказан: строка называет тег $TAG_Z и хранилище"
itog "нет" "$(has "$(cat "$STAND/poryadok")" "migracii")" "тег отказан: миграции не шли"
itog "нет" "$(has "$(cat "$STAND/poryadok")" "peresozdanie")" "тег отказан: службы не пересоздавались"
itog "$MAIN_DO" "$(git -C "$BARE" rev-parse main)" "тег отказан: main хранилища не тронут"
itog "да" "$([ "$(find "$STAND/kopii" -path "*$TAG_Z*" -name 'база.dump' -size +1k 2>/dev/null | grep -c .)" -ge 1 ] && echo да || echo нет)" \
     "тег отказан: дамп до выката остался"

echo "── 7. тесты на той же сборке: id образа набора == id :latest прода после выката ──"
# Набор с `image: артефакт` идёт на образе этапа B (§4, решение 2а). Вторая сборка для
# тестов дала бы на стенде новый id: проверено одно, выкачено другое. Подставной docker
# запоминает образ, на котором запущен контейнер набора (довод перед «sleep»).
mkdir -p "$PRODUCT/deploy"
printf "suites:\n  - name: na-artefakte\n    image: артефакт\n    base: alpine:3.20\n    install: 'true'\n    manifests: []\n    timeout_s: 60\n    steps:\n      - cmd: 'true'\n" \
    > "$PRODUCT/deploy/server-checks.yaml"
g -C "$PRODUCT" add -A
g -C "$PRODUCT" commit -qm "проверки на артефакте"
printf 'MemTotal:       65536000 kB\nMemFree:        1000 kB\nMemAvailable:   %s kB\n' \
    "$((20000 * 1024))" > "$STAND/meminfo"
conf "$BUILD_TAGGED" "$ZHIVO" "STACK_CHECKS_FILE=\"deploy/server-checks.yaml\"
STACK_CHECK_MIN_FREE_MB=\"4096\"
STACK_CHECK_UID=\"$(id -u):$(id -g)\""
DEPLOY_CHECK_MEMINFO="$STAND/meminfo" DEPLOY_CHECK_WORK_ROOT="$STAND/kopii-proverok" \
    DEPLOY_CHECK_LOG_ROOT="$STAND/logi-proverok" run_deploy
G12=$(head12)
SUITE_IMAGE=$(awk '/^run / && / --name proba-check-/ { for (i = 2; i <= NF; i++) if ($i == "sleep") { print $(i - 1); exit } }' \
              "$STAND/docker.log")
itog "0" "$RC" "7 выкат с набором на артефакте выходит нулём"
itog "да" "$([ -n "$SUITE_IMAGE" ] && echo да || echo нет)" "7 контейнер набора запущен (образ: ${SUITE_IMAGE:-нет})"
itog "1" "$(sborok)" "7 сборок 1: тестам своей сборки нет"
itog "да" "$(same_id "$(id_of "$SUITE_IMAGE")" "$(id_of proba-api:latest)")" \
     "7 id образа набора ($SUITE_IMAGE) == id :latest прода после выката"
itog "да" "$(same_id "$(id_of "$SUITE_IMAGE")" "$(id_of "proba-api:$G12")")" "7 образ набора — :$G12 этого выката"

# ── Раунд ревью фазы 2 (журнал задачи 16.11 15:04): C1, C2, I1–I4, M2, M3, M5, M7 ──
# Хук хранилища от случая «тег отказан» снят; проверки в конфиге — только где названы.
rm -f "$BARE/hooks/pre-receive"
tags_n() { git -C "$PRODUCT" tag -l 'deploy-*' | grep -c . || true; }
head_full() { git -C "$PRODUCT" rev-parse HEAD; }
build_id() { printf 'sha256:%s' "$(printf 'sborka-%s' "$1" | sha256sum | cut -c1-64)"; }   # id сборки № $1
last_build_id() { build_id "$(cat "$STATE.n" 2>/dev/null || echo 0)"; }
drop_id() {  # $1=id — образ удалён целиком, со всеми тегами (docker rmi, чистка места)
    awk -v i="$1" '$2 != i' "$STATE" > "$STATE.new"; mv "$STATE.new" "$STATE"
}
CHECKS_CONF="STACK_CHECKS_FILE=\"deploy/server-checks.yaml\"
STACK_CHECK_MIN_FREE_MB=\"4096\"
STACK_CHECK_UID=\"$(id -u):$(id -g)\""
check_env() {  # $@ — команда; окружение тестового этапа стенда (как у случая 7)
    DEPLOY_CHECK_MEMINFO="$STAND/meminfo" DEPLOY_CHECK_WORK_ROOT="$STAND/kopii-proverok" \
        DEPLOY_CHECK_LOG_ROOT="$STAND/logi-proverok" "$@"
}

echo "── C2. числовой ключ выката — не число («4s», «4G», «release=2s»): отказ до ворот с именем ключа ──"
# Опечатка в потолке живости обрывала арифметику health_ok: мёртвая живость выходила
# «выкачено: health 200» кодом 0. Ключ судится при загрузке — до сборки, ворот и тега.
for BAD in 'DEPLOY_HEALTH_WAIT_SEC="4s"' 'STACK_CHECK_MIN_FREE_MB="4G"' 'STACK_STEP_TIMEOUTS="release=2s"'; do
    KEY=${BAD%%=*}
    commit_product "c2 $KEY"
    conf "$BUILD_TAGGED" "$MERTVO" "$CHECKS_CONF
$BAD"
    TAGS_DO=$(tags_n)
    check_env run_deploy
    itog "1" "$RC" "C2 $BAD: код 1"
    itog "да" "$(has "$VYVOD" "$KEY")" "C2 $BAD: отказ называет $KEY"
    itog "нет" "$(gate_called)" "C2 $BAD: ворота не звались"
    itog "0" "$(sborok)" "C2 $BAD: сборок 0 (ключ судится при загрузке)"
    itog "$TAGS_DO" "$(tags_n)" "C2 $BAD: тег не поставлен"
    itog "нет" "$(has "$VYVOD" "выкачено")" "C2 $BAD: «выкачено» не звучит"
done

echo "── I3. провал на пути ловушки записан НЕ живым; следующий провал откатывается на настоящий живой ──"
# Ловушка выхода пишет итог сама (сборка, миграции, тег упали). Запиши она «выкачено» —
# следующий откат поднял бы образ проваленного выката.
commit_product "i3 живой"
conf "$BUILD_TAGGED" "$ZHIVO"
run_deploy
itog "0" "$RC" "I3 условие: живой выкат — код 0"
ID_L=$(id_of proba-api:latest)
commit_product "i3 миграции упали"
conf "$BUILD_TAGGED" "$ZHIVO" 'STACK_MIGRATE_CMD="false"'
run_deploy
TAG_M=$(last_tag)
M12=$(head12)
ID_M=$(id_of "proba-api:$M12")
itog "1" "$RC" "I3 миграции упали: код 1"
itog "да" "$(is_failed "$(result_of "$TAG_M" "$(head_full)")")" \
     "I3 миграции упали: строка $TAG_M в журнале итогов есть, итог провала (вышло: $(result_of "$TAG_M" "$(head_full)"))"
itog "да" "$([ -n "$ID_M" ] && [ "$ID_M" != "$ID_L" ] && echo да || echo нет)" \
     "I3 условие: образ :$M12 проваленного выката есть и ≠ образа живого"
commit_product "i3 сборка упала"
conf "false" "$ZHIVO"
run_deploy
itog "1" "$RC" "I3 сборка упала: код 1"
itog "нет" "$(is_live "$(result_of "$(last_tag)" "$(head_full)")")" "I3 сборка упала: живого итога на этот sha нет"
commit_product "i3 следующий провал"
conf "$BUILD_TAGGED" "$MERTVO"
run_deploy
itog "1" "$RC" "I3 следующий провал: код 1"
itog "да" "$(same_id "$(id_of proba-api:latest)" "$ID_L")" "I3 откат на настоящий живой выкат, не на проваленный $TAG_M"
itog "нет" "$(same_id "$(id_of proba-api:latest)" "$ID_M")" "I3 откат не поднял образ проваленного :$M12"

echo "── C1. подъём отката не видит IMAGE_TAG проваленного выката — compose поднимает образ живого ──"
# compose продукта читает image: proba-api:${IMAGE_TAG:-latest}; подъём отката пишет, какой
# образ он поднял бы. IMAGE_TAG проваленного выката в его окружении поднял бы ту самую
# сломанную сборку, от которой откат уходит.
cat > "$STAND/podem.sh" <<SH
#!/usr/bin/env bash
id=\$(docker image inspect --format '{{.Id}}' "proba-api:\${IMAGE_TAG:-latest}" 2>/dev/null)
printf 'otkat\npodem %s\n' "\${id:-net-obraza}" >> "$STAND/poryadok"
SH
commit_product "c1 живой"
conf "$BUILD_TAGGED" "$ZHIVO"
run_deploy
itog "0" "$RC" "C1 условие: живой выкат — код 0"
ID_C1=$(id_of proba-api:latest)
commit_product "c1 провал"
conf "$BUILD_TAGGED" "$MERTVO" "STACK_UP_CMD=\"bash $STAND/podem.sh\""
run_deploy
NEW_C1=$(last_build_id)
UP_ID=$(sed -n 's/^podem //p' "$STAND/poryadok" | tail -n 1)
itog "1" "$RC" "C1 код 1"
itog "да" "$(same_id "$UP_ID" "$ID_C1")" "C1 подъём отката поднял образ живого выката (вышло: ${UP_ID:-подъёма не было})"
itog "нет" "$(same_id "$UP_ID" "$NEW_C1")" "C1 подъём отката не поднял сломанную сборку этого выката"

echo "── I2. повторный выкат ТОГО ЖЕ sha: откат — на образ живого выката по id; свежая сборка не подменяется старой ──"
# Сборка того же sha перезаписывает :<sha12> живого выката: откат «по тегу» поднял бы
# сломанную сборку, а продвижение «:sha12 есть — значит, свежий» — старый образ.
commit_product "i2"
conf "$BUILD_TAGGED" "$ZHIVO"
run_deploy
itog "0" "$RC" "I2 условие: живой выкат — код 0"
P12=$(head12)
conf "$BUILD_LATEST" "$ZHIVO"
run_deploy
FRESH=$(last_build_id)
TAG_F=$(last_tag)
itog "0" "$RC" "I2 тот же sha, compose без IMAGE_TAG: код 0"
itog "да" "$(same_id "$(id_of proba-api:latest)" "$FRESH")" "I2 :latest — свежая сборка этого выката, а не старый образ :$P12"
itog "да" "$(grep -F "\"$TAG_F\"" "$RESULTS" | grep -qF "${FRESH#sha256:}" && echo да || echo нет)" \
     "I2 строка итога $TAG_F в deploy-results.jsonl несёт id выкаченного образа"
conf "$BUILD_TAGGED" "$MERTVO"
run_deploy
BROKEN=$(last_build_id)
itog "1" "$RC" "I2 тот же sha, живость мертва: код 1"
itog "да" "$(same_id "$(id_of proba-api:latest)" "$FRESH")" "I2 откат поднял образ живого выката $TAG_F по id"
itog "нет" "$(same_id "$(id_of proba-api:latest)" "$BROKEN")" "I2 откат не поднял свежую сломанную сборку того же sha"

echo "── I4. живой итог в журнале есть, а образа живого выката нет — откат на снимок, словами ──"
commit_product "i4 живой"
conf "$BUILD_TAGGED" "$ZHIVO"
run_deploy
itog "0" "$RC" "I4 условие: живой выкат — код 0"
Q12=$(head12)
drop_id "$(id_of proba-api:latest)"
set_id "proba-api:latest" "sha256:snimok-i4"
itog "" "$(id_of "proba-api:$Q12")" "I4 условие: образа живого выката :$Q12 нет (удалён со всеми тегами)"
commit_product "i4 провал"
conf "$BUILD_TAGGED" "$MERTVO"
run_deploy
itog "1" "$RC" "I4 код 1"
itog "sha256:snimok-i4" "$(id_of proba-api:latest)" "I4 :latest — снимок, что был до выката"
itog "да" "$(printf '%s\n' "$VYVOD" | grep -q 'образа живого выката.*нет' && echo да || echo нет)" \
     "I4 сказано «образа живого выката … нет»"
itog "да" "$(has "$VYVOD" "откат на снимок")" "I4 сказано «откат на снимок»"

echo "── M3а. reload прокси упал — итог «выкачено с оговоркой» (§5.3 п. 7) ──"
commit_product "m3 proksi"
conf "$BUILD_TAGGED" "$ZHIVO" 'STACK_PROXY_RELOAD_CMD="false"'
run_deploy
R_PX=$(result_of "$(last_tag)" "$(head_full)")
itog "да" "$(case "$R_PX" in "да выкачено с оговоркой"*прокси*) echo да ;; *) echo нет ;; esac)" \
     "M3а итог «выкачено с оговоркой: …прокси…» (вышло: $R_PX)"

# Хранилище. Дерево продукта перед выкатом HEAD содержит main хранилища: выкат идёт
# fast-forward, и отказ случая — его собственный, а не отказ M4.
sync_main() {
    git -C "$PRODUCT" fetch -q origin main
    g -C "$PRODUCT" merge -q --no-edit FETCH_HEAD >/dev/null
}

echo "── M5. STACK_BRANCH нет в хранилище — отказ до ворот, ветка в хранилище не создана ──"
sync_main
commit_product "m5"
conf "$BUILD_TAGGED" "$ZHIVO" 'STACK_REMOTE="origin"
STACK_BRANCH="net-takoj-main"'
TAGS_DO=$(tags_n)
run_deploy
itog "1" "$RC" "M5 код 1"
itog "да" "$(has "$VYVOD" "net-takoj-main")" "M5 отказ называет ветку net-takoj-main"
itog "нет" "$(gate_called)" "M5 ворота не звались"
itog "$TAGS_DO" "$(tags_n)" "M5 тег не поставлен"
itog "нет" "$(git -C "$BARE" rev-parse -q --verify refs/heads/net-takoj-main >/dev/null && echo да || echo нет)" \
     "M5 ветка net-takoj-main в хранилище не создана"

echo "── M7. --план --ветка: ни git fetch, ни перемены дерева ──"
PLAN_TIP=$(vetka vetka-plan origin/main)
conf "$BUILD_TAGGED" "$ZHIVO" "$REMOTE_CONF"
rm -f "$PRODUCT/.git/FETCH_HEAD"
HEAD_DO=$(head_full)
run_deploy --план --ветка vetka-plan
itog "0" "$RC" "M7 --план: код 0"
itog "нет" "$([ -e "$PRODUCT/.git/FETCH_HEAD" ] && echo да || echo нет)" "M7 git fetch не звался (FETCH_HEAD не появился)"
itog "нет" "$(git -C "$PRODUCT" cat-file -e "$PLAN_TIP^{commit}" 2>/dev/null && echo да || echo нет)" \
     "M7 вершина vetka-plan в репозиторий продукта не скачана"
itog "$HEAD_DO" "$(head_full)" "M7 HEAD дерева не сдвинут"

echo "── M2. main хранилища сдвинулся между предметом и пушем — отказ, main не переписан, тега там нет ──"
cat > "$STAND/sdvig.sh" <<SH
#!/usr/bin/env bash
set -e
git -C "$WORK" fetch -q origin
git -C "$WORK" checkout -q -B main origin/main
git -C "$WORK" -c user.email=proba@stend -c user.name=proba commit -q --allow-empty -m "сдвиг main во время выката"
git -C "$WORK" push -q origin main
git -C "$WORK" rev-parse HEAD > "$STAND/sdvig-sha"
SH
sync_main
commit_product "m2"
conf "bash $STAND/sdvig.sh && $BUILD_TAGGED" "$ZHIVO" "$REMOTE_CONF
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok\""
rm -f "$STAND/sdvig-sha"
run_deploy
itog "1" "$RC" "M2 код 1"
itog "да" "$([ -s "$STAND/sdvig-sha" ] && echo да || echo нет)" "M2 условие: main сдвинут во время сборки"
itog "$(cat "$STAND/sdvig-sha" 2>/dev/null)" "$(git -C "$BARE" rev-parse main)" "M2 main хранилища — сдвинутый, выкат его не переписал"
itog "" "$(git -C "$BARE" tag --points-at "$(head_full)")" "M2 тега выката в хранилище нет"
itog "нет" "$(has "$(cat "$STAND/poryadok")" "migracii")" "M2 миграции не шли"
itog "нет" "$(is_live "$(result_of "$(last_tag)" "$(head_full)")")" "M2 итог не «выкачено»"

echo "── M3. хранилище отказало в ветке — отказ, main не тронут, в прод не пошло ──"
sync_main
commit_product "m3"
cat > "$BARE/hooks/pre-receive" <<'SH'
#!/bin/sh
while read -r old new ref; do
    case "$ref" in refs/heads/*) echo "проба: хранилище веток не принимает" >&2; exit 1 ;; esac
done
exit 0
SH
chmod +x "$BARE/hooks/pre-receive"
MAIN_DO=$(git -C "$BARE" rev-parse main)
conf "$BUILD_TAGGED" "$ZHIVO" "$REMOTE_CONF
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok\""
run_deploy
rm -f "$BARE/hooks/pre-receive"
itog "1" "$RC" "M3 код 1"
itog "$MAIN_DO" "$(git -C "$BARE" rev-parse main)" "M3 main хранилища не тронут"
itog "нет" "$(has "$(cat "$STAND/poryadok")" "migracii")" "M3 миграции не шли"
itog "нет" "$(is_live "$(result_of "$(last_tag)" "$(head_full)")")" "M3 итог не «выкачено»"

echo "── I1а. ворота отказали («Да» нет) — дерево продукта не осталось на чужой ветке ──"
sync_main
G_TIP=$(vetka vetka-g origin/main)
conf "$BUILD_TAGGED" "$ZHIVO" "$REMOTE_CONF"
HEAD_DO=$(head_full)
PROBA_GATE_RC=1 run_deploy --ветка vetka-g
itog "1" "$RC" "I1а код 1"
itog "да" "$(gate_called)" "I1а условие: ворота звались (и отказали)"
itog "$HEAD_DO" "$(head_full)" "I1а HEAD дерева продукта прежний, не ${G_TIP:0:8} ветки vetka-g"

# Первый проход — фоном, со своим журналом ворот; второй — run_deploy через 3 с, пока
# первый собирает (сборка спит 8 с). Сборка до ворот (§5.1): без замка дерева второй
# переключил бы дерево под первым, и первый пал бы на «HEAD сдвинулся во время сборки».
deploy_bg() {  # $@ — ключи; вывод — $STAND/pervyj.out, код — $STAND/pervyj.rc
    rm -f "$STAND/pervyj.rc"
    ( PATH="$FAKEBIN:$PATH" DOCKER_STATE="$STATE" DOCKER_JOURNAL="$STAND/docker-pervyj.log" \
      PROBA_GATE_ARGV="$STAND/gate-argv-pervyj" HARNESS_DOOR_STAND_HOME="$STAND" \
      HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
      HARNESS_LOG_DIR="$STAND/zhurnal" \
      timeout 300 bash "$COPY/deploy.sh" "$@" > "$STAND/pervyj.out" 2>&1
      echo $? > "$STAND/pervyj.rc" ) &
    BG_PID=$!
}
first_says() { grep -m1 -E 'ОШИБКА|сдвинулся|отменён|незнакомый' "$STAND/pervyj.out" | cut -c1-120; }

echo "── I1б. идёт выкат — второй вызов с --ветка отказан замком, дерево не переключено, первый жив ──"
sync_main
commit_product "i1 pervyj"
D_SHA=$(head_full)
R_TIP=$(vetka vetka-r origin/main)
conf "sleep 8; $BUILD_TAGGED" "$ZHIVO" "$REMOTE_CONF"
deploy_bg
sleep 3
run_deploy --ветка vetka-r
wait "$BG_PID"
itog "1" "$RC" "I1б второй вызов отказан — код 1"
itog "$D_SHA" "$(head_full)" "I1б HEAD дерева — sha первого выката, не ${R_TIP:0:8} ветки vetka-r"
itog "0" "$(cat "$STAND/pervyj.rc" 2>/dev/null)" "I1б первый выкат не убит вторым ($(first_says))"

echo "── I1в. идёт --проверка — второй вызов с --ветка отказан замком, дерево не переключено ──"
sync_main
commit_product "i1 proverka"
D2_SHA=$(head_full)
R2_TIP=$(vetka vetka-r2 origin/main)
conf "sleep 8; $BUILD_TAGGED" "$ZHIVO" "$REMOTE_CONF
$CHECKS_CONF"
check_env deploy_bg --проверка
sleep 3
check_env run_deploy --ветка vetka-r2
wait "$BG_PID"
itog "1" "$RC" "I1в второй вызов отказан — код 1"
itog "$D2_SHA" "$(head_full)" "I1в HEAD дерева — sha проверки, не ${R2_TIP:0:8} ветки vetka-r2"
itog "0" "$(cat "$STAND/pervyj.rc" 2>/dev/null)" "I1в --проверка не убита вторым ($(first_says))"

# ═════ Раунд ревью 35 (журнал задачи 16.11 16:3x): В1, E4, М1 (дерево) ═════
cur_branch() { git -C "$PRODUCT" symbolic-ref -q --short HEAD || git -C "$PRODUCT" rev-parse HEAD; }

echo "── В1а. --проверка, compose без IMAGE_TAG — :latest прода не сдвинут ──"
# Сборка до «Да» кладёт образ прямо в :latest; помеченный :<sha12> ретегом, он нужен тестам,
# но :latest прода возвращается на то, что было до сборки: прод двигается только после «Да».
commit_product "в1 proverka"
conf "$BUILD_LATEST" "$ZHIVO"
LATEST_DO=$(id_of proba-api:latest)
run_deploy --проверка
V12=$(head12)
itog "0" "$RC" "В1а --проверка: код 0"
itog "да" "$([ -n "$LATEST_DO" ] && echo да || echo нет)" "В1а условие: :latest прода до проверки есть"
itog "да" "$([ -n "$(id_of "proba-api:$V12")" ] && [ "$(id_of "proba-api:$V12")" != "$LATEST_DO" ] && echo да || echo нет)" \
     "В1а сборка помечена :$V12 (свой образ, не прежний :latest)"
itog "$LATEST_DO" "$(id_of proba-api:latest)" "В1а :latest прода прежний после --проверка"

echo "── В1б. ворота отказали («Да» нет), compose без IMAGE_TAG — :latest прода не сдвинут ──"
commit_product "в1 vorota"
conf "$BUILD_LATEST" "$ZHIVO"
LATEST_DO=$(id_of proba-api:latest)
PROBA_GATE_RC=1 run_deploy
itog "1" "$RC" "В1б код 1"
itog "да" "$(gate_called)" "В1б условие: ворота звались (и отказали)"
itog "1" "$(sborok)" "В1б условие: сборка шла"
itog "$LATEST_DO" "$(id_of proba-api:latest)" "В1б :latest прода прежний после отказа ворот"

echo "── E4. --проверка --ветка дошла до конца — дерево возвращено туда, где стояло ──"
sync_main
E4_TIP=$(vetka vetka-e4 origin/main)
conf "$BUILD_TAGGED" "$ZHIVO" "$REMOTE_CONF"
HEAD_DO=$(head_full)
BR_DO=$(cur_branch)
run_deploy --проверка --ветка vetka-e4
itog "0" "$RC" "E4 --проверка --ветка: код 0"
itog "да" "$(has "$VYVOD" "${E4_TIP:0:8}")" "E4 условие: проверялась вершина ${E4_TIP:0:8} ветки vetka-e4"
itog "$HEAD_DO" "$(head_full)" "E4 HEAD дерева прежний, не ${E4_TIP:0:8}"
itog "$BR_DO" "$(cur_branch)" "E4 дерево на прежней ветке $BR_DO"

echo "── М1. SIGTERM выкату --ветка во время сборки — код 143, дерево возвращено ──"
sync_main
T_TIP=$(vetka vetka-term origin/main)
conf "sleep 4; $BUILD_TAGGED" "$ZHIVO" "$REMOTE_CONF"
HEAD_DO=$(head_full)
BR_DO=$(cur_branch)
PATH="$FAKEBIN:$PATH" DOCKER_STATE="$STATE" DOCKER_JOURNAL="$STAND/docker.log" \
    PROBA_GATE_ARGV="$STAND/gate-argv" HARNESS_DOOR_STAND_HOME="$STAND" \
    HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
    HARNESS_LOG_DIR="$STAND/zhurnal" bash "$COPY/deploy.sh" --ветка vetka-term > "$STAND/o39-m1.out" 2>&1 &
DPID=$!
for _ in $(seq 1 20); do [ "$(head_full)" = "$T_TIP" ] && break; sleep 0.5; done
itog "$T_TIP" "$(head_full)" "М1 условие: дерево переключено на vetka-term до сигнала"
sleep 1
kill -TERM "$DPID"
wait "$DPID"; M1RC=$?
itog "143" "$M1RC" "М1 код выхода 143"
itog "$HEAD_DO" "$(head_full)" "М1 HEAD дерева прежний после SIGTERM, не ${T_TIP:0:8}"
itog "$BR_DO" "$(cur_branch)" "М1 дерево на прежней ветке $BR_DO"
sleep 5   # сборка, пережившая дверь на старом коде, доходит до конца раньше уборки стенда

printf '\nОБРАЗ-ВЫКАТА: путей %d, неудач %d\n' "$paths" "$fails"
[ "$fails" -eq 0 ]
