#!/usr/bin/env bash
# Проба: выкат продукта не судит и не пишет репозиторий харнеса (спека
# эталонного выката §5.4 «Граница продукт/харнес», Г1 и Г2).
#
# Что охраняет. Без PRODUCT_REPO предметом выката становился PROJECT_DIR —
# каталог ХАРНЕСА: тег deploy-* вставал в репозиторий харнеса, сверка секретов
# и манифесты судили не то, что едет на прод (Г1, случай sample-32). Признаком
# продукта служил docker-compose.yml в каталоге харнеса (Г2): файл харнеса решал,
# есть ли продукт. Защиту копии сети 10.11 («продукт объявлен, сборки нет — отказ, а
# не нуль») граница не ослабляет — её держит случай 3.
#
# Стенд свой: паспорт, харнес и продукт в tmp, подставной docker. Ворота —
# заглушка, которая открывает всегда и пишет, как её звали: проба судит
# границу, а не кнопку, и отказ по границе обязан стоять ДО ворот. Боевой
# паспорт машины не читается: дверь идёт копией вне PROJECT_DIR машины,
# паспорт ей называет HARNESS_DOOR_STAND_HOME. Механизм — deploy.sh и
# lib/door_*.sh рядом с пробой (мутации — из копии дерева).
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

STAND=$(mktemp -d -t proba-deploy-boundary-XXXXXX)
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

# compose лежит в харнесе ЗАКОММИЧЕННЫМ: признак Г2 виден, а дерево харнеса чистое —
# иначе случай 2 отказывал бы «незакоммиченным» раньше тега и судил бы не границу.
printf 'services: {}\n' > "$HARNESS/docker-compose.yml"
for repo in "$HARNESS" "$PRODUCT"; do
    printf 'стенд\n' > "$repo/README"
    git -C "$repo" init -q
    git -C "$repo" -c user.email="proba@stend" -c user.name="проба" add -A
    git -C "$repo" -c user.email="proba@stend" -c user.name="проба" commit -qm "стенд"
done

cat > "$FAKEBIN/docker" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "$FAKEBIN/docker"

cp "$HERE/deploy.sh" "$HERE/check-secrets.sh" "$COPY/"
cp -r "$HERE/lib" "$COPY/"
cat > "$COPY/deploy_guard.py" <<'PY'
# Ворота-заглушка пробы границы: открывают всегда и пишут, как их звали.
import os, sys
with open(os.environ["PROBA_GATE_ARGV"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
print("ворота-заглушка: открыто")
PY

conf() {  # $1 — строки harness.conf стенда
    printf '%s\nBACKUP_DIR="%s/kopii"\n' "$1" "$STAND" > "$STAND/harness.conf"
}
VYVOD=""
RC=0
run_deploy() {  # $@ — ключи deploy.sh
    : > "$STAND/gate-argv"; : > "$STAND/poryadok"
    VYVOD=$(PATH="$FAKEBIN:$PATH" PROBA_GATE_ARGV="$STAND/gate-argv" HARNESS_DOOR_STAND_HOME="$STAND" \
            HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
            HARNESS_LOG_DIR="$STAND/zhurnal" \
            timeout 120 bash "$COPY/deploy.sh" "$@" 2>&1)
    RC=$?
    printf '%s\n' "$VYVOD" | sed 's/^/      /' | tail -4
}
harness_tags() { git -C "$HARNESS" tag -l 'deploy-*' | grep -c . || true; }
gate_called() { grep -q . "$STAND/gate-argv" && echo да || echo нет; }

echo "── 1. ключи продукта пусты, в каталоге харнеса лежит docker-compose.yml — «продукта нет», выход 0 ──"
conf 'PRODUCT_DIR=""
PRODUCT_REPO=""
PRODUCT_PATH=""
STACK_BUILD_CMD=""'
itog "да" "$([ -f "$HARNESS/docker-compose.yml" ] && echo да || echo нет)" "1 условие: compose в PROJECT_DIR стенда есть"
run_deploy
itog "0" "$RC" "1 код выхода 0: compose в каталоге харнеса не объявляет продукт"
itog "да" "$(has "$VYVOD" "продукта нет")" "1 вывод называет «продукта нет»"
itog "нет" "$(gate_called)" "1 ворота не звались"
itog "0" "$(harness_tags)" "1 в репозитории харнеса тега выката нет"

echo "── 2. БОЛЬНОЙ СЛУЧАЙ: сборка задана, PRODUCT_REPO пуст — отказ с именем ключа, харнес не тронут ──"
conf "PRODUCT_DIR=\"proba\"
PRODUCT_REPO=\"\"
STACK_BUILD_CMD=\"echo sborka >> $STAND/poryadok\""
itog "" "$(git -C "$HARNESS" status --porcelain)" "2 условие: дерево харнеса стенда чистое"
HEAD_DO=$(git -C "$HARNESS" rev-parse HEAD)
run_deploy
itog "1" "$RC" "2 код выхода 1"
itog "да" "$(has "$VYVOD" "PRODUCT_REPO")" "2 отказ называет ключ PRODUCT_REPO"
itog "0" "$(harness_tags)" "2 в репозитории харнеса тега выката нет"
itog "нет" "$(gate_called)" "2 отказ стоит до ворот: ворота не звались"
itog "" "$(cat "$STAND/poryadok")" "2 сборка не звалась"
itog "$HEAD_DO" "$(git -C "$HARNESS" rev-parse HEAD)" "2 HEAD харнеса не сдвинулся"

echo "── 3. СТРАЖ копии сети 10.11: продукт объявлен PRODUCT_DIR, сборки нет — отказ 1, а не нуль ──"
conf 'PRODUCT_DIR="proba"
PRODUCT_REPO=""
PRODUCT_PATH=""
STACK_BUILD_CMD=""'
run_deploy
itog "1" "$RC" "3 код выхода 1"
itog "да" "$(has "$VYVOD" "STACK_BUILD_CMD")" "3 отказ называет пустой STACK_BUILD_CMD"
itog "нет" "$(gate_called)" "3 ворота не звались"

echo "── 4. граница по тексту: PROJECT_DIR в коде выката — только загрузка ключей и их проверка ──"
# Предмет — РАЗБОРОМ каталога: deploy.sh и каждый lib/door_*.sh рядом с пробой.
# Пустой список или ни одной разрешённой строки — провал: grep, который не видит
# ничего, был бы зелён на любой подстановке PROJECT_DIR.
shopt -s nullglob
FILES=("$HERE/deploy.sh" "$HERE"/lib/door_*.sh)
shopt -u nullglob
itog "да" "$([ -f "$HERE/deploy.sh" ] && [ "${#FILES[@]}" -ge 2 ] && echo да || echo нет)" \
     "4 судимых файлов: deploy.sh и lib/door_*.sh (${#FILES[@]})"
STROKI=$(for f in "${FILES[@]}"; do
    grep -nE '\$\{?PROJECT_DIR' "$f" | grep -vE '^[0-9]+:[[:space:]]*#' | sed "s|^|$(basename "$f"):|"
done)
RAZRESHENO='^[^:]+:[0-9]+:(konf_zagruzit_dveri |: "\$\{PROJECT_DIR:\?)'
itog "да" "$(printf '%s\n' "$STROKI" | grep -qE "$RAZRESHENO" && echo да || echo нет)" \
     "4 разрешённая строка «: \"\${PROJECT_DIR:?…}\"» найдена (grep видит предмет)"
LISHNIE=$(printf '%s\n' "$STROKI" | grep -vE "$RAZRESHENO" | grep . || true)
[ -z "$LISHNIE" ] || printf '%s\n' "$LISHNIE" | sed 's/^/      /'
itog "0" "$(printf '%s' "$LISHNIE" | grep -c . || true)" "4 строк кода с PROJECT_DIR вне загрузки ключей нет"

echo "── 5. Г7: продукт внутри PROJECT_DIR — --план печатает предупреждение, выкат не запрещён ──"
# Вложенный продукт — не ошибка (у части установок так исторически), но граница
# тогда держится на отдельном репозитории продукта; об этом --план говорит вслух.
VNUTRI="$HARNESS/vnutri-produkta"
mkdir -p "$VNUTRI"
printf 'продукт\n' > "$VNUTRI/README"
git -C "$VNUTRI" init -q
git -C "$VNUTRI" -c user.email="proba@stend" -c user.name="проба" add -A
git -C "$VNUTRI" -c user.email="proba@stend" -c user.name="проба" commit -qm "стенд"
conf "PRODUCT_DIR=\"proba\"
PRODUCT_REPO=\"$VNUTRI\"
STACK_BUILD_CMD=\"echo sborka >> $STAND/poryadok\""
run_deploy --план
itog "0" "$RC" "5 --план внутри PROJECT_DIR выходит нулём (не запрет)"
itog "да" "$(has "$VYVOD" "ПРЕДУПРЕЖДЕНИЕ")" "5 --план печатает ПРЕДУПРЕЖДЕНИЕ"
itog "да" "$(has "$VYVOD" "внутри PROJECT_DIR")" "5 предупреждение называет «внутри PROJECT_DIR»"
conf "PRODUCT_DIR=\"proba\"
PRODUCT_REPO=\"$PRODUCT\"
STACK_BUILD_CMD=\"echo sborka >> $STAND/poryadok\""
run_deploy --план
itog "0" "$RC" "5 --план продукта снаружи выходит нулём"
itog "нет" "$(has "$VYVOD" "ПРЕДУПРЕЖДЕНИЕ")" "5 продукт снаружи PROJECT_DIR — предупреждения нет"

printf '\nГРАНИЦА-ВЫКАТА: путей %d, неудач %d\n' "$paths" "$fails"
[ "$fails" -eq 0 ]
