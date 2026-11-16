#!/usr/bin/env bash
# Проба: этапы D/I выката — скан собранного образа :<sha12> до лока и ворот, вердикт по
# политике (спека эталонного выката §2.2 D/I, §5.1, §5.3 п. 3 и 7, §7 стр. 4; план, фаза 4).
#
# Что охраняет. Образ, который поедет на прод, сканируется ДО «Да»: CRITICAL с исправлением
# при политике critical-fixable — отказ до изменений (ворот нет, тега нет, :latest прежний).
# Остальное выкат не останавливает, но и не молчит: при warn блокирующие — оговорка в итоге;
# CRITICAL без исправления названы; скан, который не прочитан (сканер упал, вывод битый), —
# «скан не прочитан» оговоркой, а не зелёный выкат (И-4); пустой STACK_SCAN_CMD — «скан
# пропущен: не задано» вслух.
#
# Контракт, который проба ждёт от кода фазы 4 (имена и слова — его решения, здесь названы):
#   - STACK_SCAN_CMD зовётся на каждый образ STACK_IMAGES с доводом «<образ>:<sha12>»
#     (последний довод команды); его stdout — Trivy JSON, разбор — scripts/lib/scan_verdict.py
#     (коды 0 / 1 / 2, судит test_scan_verdict_lib.py);
#   - STACK_SCAN_POLICY: critical-fixable (умолчание, пустой ключ — то же) | warn; иное
#     значение — отказ до сборки с именем ключа;
#   - critical-fixable и код 1 — отказ, id уязвимости в выводе; warn и код 1 — выкат идёт,
#     итог в deploy-results.jsonl «выкачено с оговоркой: скан…», id в выводе;
#   - код 0 с CRITICAL без исправления — выкат идёт, id в выводе;
#   - код 2 или сканер упал — выкат идёт, вывод и итог называют «скан не прочитан», итог
#     начинается «выкачено с оговоркой»;
#   - пустой STACK_SCAN_CMD — строка вывода называет STACK_SCAN_CMD и «скан пропущен: не задано»,
#     итоговая строка «deploy ok:» и запись итога — «скан: пропущен»;
#   - `--проверка` судит скан так же, как выкат: CRITICAL с исправлением — код 1.
#
# Сканера на машине нет (which trivy grype пусто, план фаза 4): сканер стенда — скрипт,
# который пишет свои доводы и отдаёт фикстуру JSON из этой пробы. Стенд свой, как у
# test_deploy_stages.sh: паспорт, харнес и продукт в tmp, копия scripts/ с воротами-заглушкой,
# docker подставной с памятью образов. Контейнеров и образов docker проба не создаёт.
#
#   bash scripts/test_deploy_scan.sh        (только фоном)
# Итог: «СКАН-ОБРАЗА: путей N, неудач M»; код 0 — неудач нет.
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

STAND=$(mktemp -d -t o-cicd-46-proba-scan-XXXXXX)
trap 'rm -rf "$STAND"' EXIT

HARNESS="$STAND/harnes"
PRODUCT="$STAND/produkt"
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
printf 'стенд\n' > "$PRODUCT/README"
git -C "$PRODUCT" init -q -b main
g -C "$PRODUCT" add -A
g -C "$PRODUCT" commit -qm "стенд"
bump() { g -C "$PRODUCT" commit -q --allow-empty -m "$1"; }

# Подставной docker с памятью «образ:тег id» (как в test_deploy_stages.sh, без наборов).
cat > "$FAKEBIN/docker" <<'SH'
#!/usr/bin/env bash
state="$DOCKER_STATE"
touch "$state"
printf '%s\n' "$*" >> "${DOCKER_JOURNAL:-/dev/null}"
norm() { case "${1##*/}" in *:*) printf '%s' "$1" ;; *) printf '%s:latest' "$1" ;; esac; }
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
drop() {
    local r; r=$(norm "$1")
    awk -v r="$r" '$1 != r' "$state" > "$state.new"; mv "$state.new" "$state"
}
inspect() {
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
    *) exit 0 ;;
esac
SH
chmod +x "$FAKEBIN/docker"
STATE="$STAND/docker-state"
: > "$STATE"
id_of() { PATH="$FAKEBIN:$PATH" DOCKER_STATE="$STATE" docker image inspect --format '{{.Id}}' "$1" 2>/dev/null || true; }

cp "$HERE/deploy.sh" "$HERE/check-secrets.sh" "$COPY/"
cp -r "$HERE/lib" "$COPY/"
cat > "$COPY/deploy_guard.py" <<'PY'
# Ворота-заглушка пробы скана: открывают всегда и пишут, как их звали.
import os, sys
with open(os.environ["PROBA_GATE_ARGV"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
print("ворота-заглушка: открыто")
PY

# Сканер стенда: доводы — в skan-argv (по строке на вызов), stdout — фикстура skan-vyvod,
# код — из skan-kod. Первый довод — каталог стенда, образ дверь дописывает последним.
cat > "$STAND/skaner.sh" <<'SH'
stand=$1; shift
printf '%s\n' "$*" >> "$stand/skan-argv"
[ ! -f "$stand/skan-vyvod" ] || cat "$stand/skan-vyvod"
exit "$(cat "$stand/skan-kod" 2>/dev/null || echo 0)"
SH
SCAN_KEY="STACK_SCAN_CMD=\"bash $STAND/skaner.sh $STAND\""
fixture() {  # $1 — вид: fixable | unfixed | clean | broken | none (вывода нет); $2 — код сканера
    printf '%s\n' "${2:-0}" > "$STAND/skan-kod"
    rm -f "$STAND/skan-vyvod"
    case "$1" in
        fixable) printf '%s' '{"SchemaVersion": 2, "ArtifactName": "proba-api", "Results": [{"Target": "alpine", "Class": "os-pkgs", "Type": "alpine", "Vulnerabilities": [{"VulnerabilityID": "CVE-2099-4601", "PkgName": "libssl3", "InstalledVersion": "3.0.0", "FixedVersion": "3.0.1", "Severity": "CRITICAL"}]}]}' ;;
        unfixed) printf '%s' '{"SchemaVersion": 2, "ArtifactName": "proba-api", "Results": [{"Target": "alpine", "Class": "os-pkgs", "Type": "alpine", "Vulnerabilities": [{"VulnerabilityID": "CVE-2099-4602", "PkgName": "zlib", "InstalledVersion": "1.2.0", "FixedVersion": "", "Severity": "CRITICAL"}, {"VulnerabilityID": "CVE-2099-4603", "PkgName": "busybox", "InstalledVersion": "1.36.0", "FixedVersion": "1.36.1", "Severity": "HIGH"}]}]}' ;;
        clean) printf '%s' '{"SchemaVersion": 2, "ArtifactName": "proba-api", "Results": [{"Target": "alpine", "Class": "os-pkgs", "Type": "alpine"}]}' ;;
        broken) printf '%s' '{"SchemaVersion": 2, "Results": [{"Target": "alp' ;;
        none) return ;;
    esac > "$STAND/skan-vyvod.new"
    mv "$STAND/skan-vyvod.new" "$STAND/skan-vyvod"
}

BUILD="docker build -t proba-api:\${IMAGE_TAG:-latest} . && echo sborka >> $STAND/poryadok"
conf() {  # $1 — добавочные строки (позже — старше)
    {
        printf 'PRODUCT_DIR="proba"\nPRODUCT_REPO="%s"\nBACKUP_DIR="%s/kopii"\n' "$PRODUCT" "$STAND"
        printf 'DEPLOY_HEALTH_WAIT_SEC="4"\nSTACK_IMAGES="proba-api"\nSTACK_HEALTH_CMD="true"\n'
        printf "STACK_BUILD_CMD='%s'\n" "$BUILD"
        printf 'STACK_UP_CMD="echo otkat >> %s/poryadok"\n' "$STAND"
        printf 'STACK_MIGRATE_CMD="echo migracii >> %s/poryadok"\n' "$STAND"
        printf 'STACK_RELEASE_CMD="echo peresozdanie >> %s/poryadok"\n' "$STAND"
        printf '%s\n' "${1:-}"
    } > "$STAND/harness.conf"
}

VYVOD=""
RC=0
run_deploy() {
    : > "$STAND/gate-argv"; : > "$STAND/poryadok"; : > "$STAND/skan-argv"
    rm -f "$STAND/zhurnal/locks/deploy.lock"
    sleep 1   # тег выката именуется по секунде
    VYVOD=$(PATH="$FAKEBIN:$PATH" DOCKER_STATE="$STATE" DOCKER_JOURNAL="$STAND/docker.log" \
            PROBA_GATE_ARGV="$STAND/gate-argv" HARNESS_DOOR_STAND_HOME="$STAND" \
            HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
            timeout 300 bash "$COPY/deploy.sh" "$@" 2>&1)
    RC=$?
    printf '%s\n' "$VYVOD" | sed 's/^/      /' | tail -5
}
sborok() { grep -c '^sborka$' "$STAND/poryadok" 2>/dev/null || true; }
prod_steps() { grep -cE '^(migracii|peresozdanie)$' "$STAND/poryadok" 2>/dev/null || true; }
gate_called() { grep -q . "$STAND/gate-argv" && echo да || echo нет; }
tags_n() { git -C "$PRODUCT" tag -l 'deploy-*' | grep -c . || true; }
head12() { git -C "$PRODUCT" rev-parse HEAD | cut -c1-12; }
# Сканер звался последним доводом «proba-api:<sha12 HEAD>».
scanned_head() { grep -qE "(^| )proba-api:$(head12)\$" "$STAND/skan-argv" && echo да || echo нет; }
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
starts() { case "$1" in "$2"*) echo да ;; *) echo нет ;; esac; }

echo "── 0. STACK_SCAN_CMD пуст — «скан пропущен: не задано» вслух, выкат идёт ──"
conf ""
run_deploy
itog "0" "$RC" "0 первый выкат без скана: код 0"
itog "да" "$(printf '%s\n' "$VYVOD" | grep -F 'STACK_SCAN_CMD' | grep -qF 'скан пропущен: не задано' && echo да || echo нет)" \
     "0 строка называет STACK_SCAN_CMD и «скан пропущен: не задано»"
# Пропуск назван и в итоге: строка «deploy ok:» и запись журнала итогов, а не только строка
# хода выката, которую никто не перечитывает (ревью фазы 4, И2).
itog "да" "$(has "$(printf '%s\n' "$VYVOD" | grep -F 'deploy ok:')" "скан: пропущен")" \
     "0 итоговая строка «deploy ok:» называет «скан: пропущен»"
itog "да" "$(has "$(last_result)" "скан: пропущен")" "0 запись итога называет «скан: пропущен» (вышло: $(last_result | cut -c1-90))"

echo "── 1. CRITICAL с исправлением, critical-fixable — отказ до ворот, тега и :latest ──"
for POL in 'STACK_SCAN_POLICY="critical-fixable"' 'STACK_SCAN_POLICY=""'; do
    bump "скан $POL"
    fixture fixable 0
    conf "$SCAN_KEY
$POL"
    TAGS_DO=$(tags_n)
    LATEST_DO=$(id_of proba-api:latest)
    run_deploy
    itog "1" "$RC" "1 $POL: код 1"
    itog "да" "$(scanned_head)" "1 $POL: сканер звался с proba-api:$(head12)"
    itog "да" "$(has "$VYVOD" "CVE-2099-4601")" "1 $POL: отказ называет CVE-2099-4601"
    itog "нет" "$(gate_called)" "1 $POL: ворота не звались"
    itog "$TAGS_DO" "$(tags_n)" "1 $POL: тег не поставлен"
    itog "$LATEST_DO" "$(id_of proba-api:latest)" "1 $POL: :latest прежний"
    itog "0" "$(prod_steps)" "1 $POL: миграций и служб нет"
done

echo "── 2. то же при warn — выкат идёт, оговорка «скан» в итоге ──"
bump "скан warn"
fixture fixable 0
conf "$SCAN_KEY
STACK_SCAN_POLICY=\"warn\""
run_deploy
itog "0" "$RC" "2 warn: код 0"
itog "да" "$(scanned_head)" "2 warn: сканер звался с proba-api:$(head12)"
itog "да" "$(has "$VYVOD" "CVE-2099-4601")" "2 warn: вывод называет CVE-2099-4601"
itog "да" "$(starts "$(last_result)" "выкачено с оговоркой")" "2 warn: итог «выкачено с оговоркой…» (вышло: $(last_result | cut -c1-80))"
itog "да" "$(has "$(last_result)" "скан")" "2 warn: оговорка называет скан"

echo "── 3. CRITICAL без исправления — выкат идёт, уязвимость названа ──"
bump "скан без исправления"
fixture unfixed 0
conf "$SCAN_KEY"
run_deploy
itog "0" "$RC" "3 без исправления: код 0"
itog "да" "$(scanned_head)" "3 сканер звался с proba-api:$(head12)"
itog "да" "$(has "$VYVOD" "CVE-2099-4602")" "3 вывод называет CVE-2099-4602 (CRITICAL без исправления)"
itog "да" "$(starts "$(last_result)" "выкачено")" "3 итог «выкачено…»"

echo "── 4. сканер упал / вывод битый — «скан не прочитан», не зелёный и не отказ ──"
for CASE in "none 3" "broken 0"; do
    bump "скан $CASE"
    fixture $CASE
    conf "$SCAN_KEY"
    run_deploy
    itog "0" "$RC" "4 «$CASE»: код 0 (не отказ)"
    itog "да" "$(scanned_head)" "4 «$CASE»: сканер звался"
    itog "да" "$(has "$VYVOD" "скан не прочитан")" "4 «$CASE»: вывод говорит «скан не прочитан»"
    itog "да" "$(starts "$(last_result)" "выкачено с оговоркой")" "4 «$CASE»: итог «выкачено с оговоркой…» (вышло: $(last_result | cut -c1-80))"
    itog "да" "$(has "$(last_result)" "скан не прочитан")" "4 «$CASE»: итог называет «скан не прочитан»"
done

echo "── 5. чистый образ — выкат идёт без оговорки скана ──"
bump "скан чисто"
fixture clean 0
conf "$SCAN_KEY"
run_deploy
itog "0" "$RC" "5 чисто: код 0"
itog "да" "$(scanned_head)" "5 чисто: сканер звался с proba-api:$(head12)"
itog "нет" "$(has "$(last_result)" "скан")" "5 чисто: итог без оговорки скана (вышло: $(last_result | cut -c1-80))"

echo "── 6. неизвестная политика — отказ до сборки с именем ключа ──"
bump "скан политика"
fixture clean 0
conf "$SCAN_KEY
STACK_SCAN_POLICY=\"critical\""
TAGS_DO=$(tags_n)
run_deploy
itog "1" "$RC" "6 STACK_SCAN_POLICY=critical: код 1"
itog "да" "$(has "$VYVOD" "STACK_SCAN_POLICY")" "6 отказ называет STACK_SCAN_POLICY"
itog "0" "$(sborok)" "6 сборок 0"
itog "нет" "$(gate_called)" "6 ворота не звались"
itog "$TAGS_DO" "$(tags_n)" "6 тег не поставлен"

echo "── 7. два образа в STACK_IMAGES — сканер зван на каждый, оба :<sha12> ──"
bump "скан два образа"
fixture clean 0
conf "$SCAN_KEY
STACK_IMAGES=\"proba-api proba-web\"
STACK_BUILD_CMD='docker build -t proba-api:\${IMAGE_TAG:-latest} -t proba-web:\${IMAGE_TAG:-latest} . && echo sborka >> $STAND/poryadok'"
run_deploy
itog "0" "$RC" "7 два образа: код 0"
itog "2" "$(grep -c . "$STAND/skan-argv")" "7 сканер зван 2 раза"
itog "да" "$(scanned_head)" "7 сканер звался с proba-api:$(head12)"
itog "да" "$(grep -qE "(^| )proba-web:$(head12)\$" "$STAND/skan-argv" && echo да || echo нет)" \
     "7 сканер звался с proba-web:$(head12)"

echo "── 8. --проверка судит скан так же: CRITICAL с исправлением — код 1 ──"
bump "скан проверка"
fixture fixable 0
conf "$SCAN_KEY"
run_deploy --проверка
itog "1" "$RC" "8 --проверка, CRITICAL с исправлением: код 1"
itog "да" "$(scanned_head)" "8 --проверка: сканер звался с proba-api:$(head12)"
itog "да" "$(has "$VYVOD" "CVE-2099-4601")" "8 --проверка: отказ называет CVE-2099-4601"

printf '\nСКАН-ОБРАЗА: путей %d, неудач %d\n' "$paths" "$fails"
[ "$fails" -eq 0 ]
