#!/usr/bin/env bash
# Проба: выкат ПРОДУКТА идёт через deploy.sh, а не мимо него (И-2).
#
# Откуда взята. Ревизия 14.10.2026: продукт объявлен (PRODUCT_DIR, PRODUCT_REPO),
# секция STACK_* пуста — и deploy.sh выходил КОДОМ 0 словами «выкатывать
# нечего». Тег и сверка секретов судили репозиторий ХАРНЕСА (PROJECT_DIR), а
# не продукта; docker из сессии без группы давал «образов нет — первый выкат»
# вместо «docker недоступен»; пересоздать службы ПОСЛЕ миграций было нечем.
#
# Проверка СОЗДАЁТ своё условие: свой паспорт, свой проект харнеса, свой
# репозиторий продукта, подставной docker в PATH. Боевую установку не трогает.
# Имена переменных ЛАТИНИЦЕЙ: bash не берёт кириллические идентификаторы.
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

STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT

HARNESS="$STAND/harnes"
PRODUCT="$STAND/produkt"
FAKEBIN="$STAND/bin"
mkdir -p "$HARNESS" "$PRODUCT" "$FAKEBIN" "$STAND/zhurnal" "$STAND/sekrety"
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

for repo in "$HARNESS" "$PRODUCT"; do
    printf 'стенд\n' > "$repo/README"
    git -C "$repo" init -q
    git -C "$repo" -c user.email="proba@stend" -c user.name="проба" add -A
    git -C "$repo" -c user.email="proba@stend" -c user.name="проба" commit -qm "стенд"
done

# Подставной docker: пишет вызовы в журнал; DOCKER_DEAD=1 — «сокет не пускает».
cat > "$FAKEBIN/docker" <<'SH'
#!/usr/bin/env bash
[ "${DOCKER_DEAD:-0}" = 1 ] && { echo "permission denied while trying to connect to the docker API" >&2; exit 1; }
printf '%s\n' "$*" >> "${DOCKER_JOURNAL:-/dev/null}"
exit 0
SH
chmod +x "$FAKEBIN/docker"

conf() {  # пишет harness.conf стенда; $1 — добавочные строки
    {
        printf 'PRODUCT_DIR="proba"\nPRODUCT_REPO="%s"\nBACKUP_DIR="%s/kopii"\n' "$PRODUCT" "$STAND"
        printf '%s\n' "$1"
    } > "$STAND/harness.conf"
}

run_deploy() {  # $@ — ключи deploy.sh; печатает вывод, код — в RC
    VYVOD=$(PATH="$FAKEBIN:$PATH" DOCKER_JOURNAL="$STAND/docker.log" \
            HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
            timeout 300 bash "$HERE/deploy.sh" "$@" 2>&1)
    RC=$?
    printf '%s\n' "$VYVOD" | sed 's/^/      /' | tail -6
}

svezhee_da() {  # свежее «да» владельца и ожидание именно на выкат
    python3 - "$STAND" <<'PY'
import json, sys, time
stand = sys.argv[1]
ts = time.strftime("%Y-%m-%dT%H:%M:%S+05:00")
with open(f"{stand}/zhurnal/confirmations.jsonl", "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"ts": ts, "text": "да", "message_id": "1",
                         "kind": "any"}, ensure_ascii=False) + "\n")
with open(f"{stand}/zhurnal/согласие.json", "w", encoding="utf-8") as fh:
    json.dump({"ожидание": {"действие": "deploy", "ts": time.time()}}, fh,
              ensure_ascii=False)
PY
}

DEAD_URL="http://127.0.0.1:9/nikogo"   # порт discard: соединение отказано сразу

echo "── 1. продукт объявлен, секция STACK_* пуста — отказ, а не «нечего» кодом 0 ──"
conf ''
run_deploy
itog "1" "$RC" "код выхода ненулевой"
itog "да" "$(has "$VYVOD" "STACK_BUILD_CMD")" "отказ называет пустой ключ"

# Запуск из `env -i`: локаль C, как у юнита и cron. Под ней bash `%q` кодирует
# кириллицу в $'…', а sg исполняет строку через dash, который этого не знает:
# «--план» доезжал искажённым и шёл мимо плана в ворота (ревью 14.10.2026).
run_bare() {  # $@ — ключи deploy.sh; docker «мёртв», окружение пустое
    VYVOD=$(env -i PATH="$FAKEBIN:/usr/bin:/bin" HOME="$STAND" DOCKER_DEAD=1 \
            HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
            timeout 120 bash "$HERE/deploy.sh" "$@" 2>&1)
    RC=$?
    printf '%s\n' "$VYVOD" | sed 's/^/      /' | tail -4
}

order() { tr '\n' ' ' < "$STAND/poryadok" | sed 's/ $//'; }
retagged() { has "$(cat "$STAND/docker.log")" "tag proba-api:previous proba-api:latest"; }

echo "── 2. --план: что сделал бы, без ворот и без выката ──"
conf "STACK_BUILD_CMD=\"echo sborka\"
STACK_MIGRATE_CMD=\"echo migracii\"
STACK_RELEASE_CMD=\"echo peresozdanie\"
STACK_HEALTH_URL=\"$DEAD_URL\""
rm -f "$STAND/zhurnal/согласие.json" "$STAND/zhurnal/confirmations.jsonl"
run_deploy --план
itog "0" "$RC" "план выходит нулём без согласия владельца"
itog "да" "$(has "$VYVOD" "echo peresozdanie")" "план называет пересоздание служб"
itog "да" "$(has "$VYVOD" "$PRODUCT")" "план называет репозиторий продукта"
itog "да" "$(has "$VYVOD" "дерево продукта: чисто")" "план называет чистоту дерева продукта"
itog "нет" "$(has "$VYVOD" "ворота")" "план не зовёт ворота"

echo "── 2б. БОЛЬНОЙ СЛУЧАЙ: --план из env -i при недоступном docker — план, не ворота ──"
conf "STACK_BUILD_CMD=\"echo sborka\"
STACK_IMAGES=\"proba-api\"
STACK_HEALTH_URL=\"$DEAD_URL\""
svezhee_da
run_bare --план
itog "0" "$RC" "план из env -i выходит нулём"
itog "да" "$(has "$VYVOD" "план выката")" "напечатан план"
itog "нет" "$(has "$VYVOD" "согласие владельца")" "ворота не звались"

echo "── 3. docker недоступен при заданных образах — отказ словами, до ворот ──"
run_bare --силой
itog "1" "$RC" "код выхода ненулевой"
itog "да" "$(has "$VYVOD" "docker недоступен")" "причина названа: docker недоступен"
itog "да" "$(has "$VYVOD" "перезапуск под sg docker")" "перезапуск под sg состоялся"
itog "да" "$(has "$VYVOD" "ключи: --силой")" "кириллический ключ пережил sg в локали C"
itog "нет" "$(has "$VYVOD" "первый выкат")" "не выдаёт недоступный docker за «первый выкат»"

echo "── 4. секрет в репозитории ПРОДУКТА — выкат отказан (судится продукт, не харнес) ──"
TOKEN_CHISLA="$(head -c 9 /dev/urandom | od -An -tu1 | tr -d ' \n' | cut -c1-9)"
TOKEN_BUKVY="$(head -c 32 /dev/urandom | base64 | tr -d '+/=' | cut -c1-35)"
printf 'TOKEN = "%s:%s"\n' "$TOKEN_CHISLA" "$TOKEN_BUKVY" > "$PRODUCT/config.py"
git -C "$PRODUCT" -c user.email="proba@stend" -c user.name="проба" add -A
git -C "$PRODUCT" -c user.email="proba@stend" -c user.name="проба" commit -qm "секрет"
conf "STACK_BUILD_CMD=\"true\"
STACK_HEALTH_URL=\"$DEAD_URL\""
svezhee_da
run_deploy
itog "1" "$RC" "код выхода ненулевой"
itog "да" "$(has "$VYVOD" "секреты в репозитории")" "отказ на секретах продукта"
git -C "$PRODUCT" rm -q config.py
git -C "$PRODUCT" -c user.email="proba@stend" -c user.name="проба" commit -qm "без секрета"

echo "── 4б. незакоммиченная правка в продукте — отказ до ворот, тега нет ──"
printf 'правка\n' >> "$PRODUCT/README"
svezhee_da
run_deploy
itog "1" "$RC" "код выхода ненулевой"
itog "да" "$(has "$VYVOD" "незакоммичен")" "причина названа: незакоммиченное"
itog "нет" "$(has "$VYVOD" "согласие владельца")" "ворота не звались"
itog "нет" "$(git -C "$PRODUCT" tag -l 'deploy-*' | grep -q . && echo да || echo нет)" "тег не поставлен"
git -C "$PRODUCT" checkout -q -- README

DUMP_LINE="STACK_DB_DUMP_CMD=\"echo damp >> $STAND/poryadok; head -c 4096 /dev/zero\""
ORDER_CONF="STACK_BUILD_CMD=\"echo sborka >> $STAND/poryadok\"
STACK_BUILD_NOCACHE_CMD=\"echo sborka >> $STAND/poryadok\"
$DUMP_LINE
STACK_UP_CMD=\"echo otkat >> $STAND/poryadok\"
STACK_IMAGES=\"proba-api\""

echo "── 5. БОЛЬНОЙ СЛУЧАЙ: красная ручка здоровья — откат на :previous и ненулевой код ──"
: > "$STAND/docker.log"; : > "$STAND/poryadok"
conf "$ORDER_CONF
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok\"
STACK_RELEASE_CMD=\"echo peresozdanie >> $STAND/poryadok\"
STACK_HEALTH_URL=\"$DEAD_URL\""
svezhee_da
run_deploy
itog "1" "$RC" "код выхода ненулевой"
itog "sborka damp migracii peresozdanie otkat" "$(order)" \
     "порядок: сборка → дамп → миграции → пересоздание → подъём отката"
itog "да" "$(retagged)" "образ возвращён с :previous на :latest"
itog "да" "$(git -C "$PRODUCT" tag -l 'deploy-*' | grep -q . && echo да || echo нет)" \
     "тег выката стоит в репозитории ПРОДУКТА"
itog "нет" "$(git -C "$HARNESS" tag -l 'deploy-*' | grep -q . && echo да || echo нет)" \
     "в репозитории харнеса тега выката нет"

itog "600" "$(find "$STAND/kopii" -name 'база.dump' -exec stat -c %a {} \; | sort -u | tr '\n' ' ' | sed 's/ $//')" \
     "дамп перед выкатом читает только владелец (данные покупателей)"

echo "── 5б. самотест дампа проходит ──"
run_deploy --selftest-dump
itog "0" "$RC" "самотест дампа выходит нулём"
itog "да" "$(has "$VYVOD" "самотест дампа: 3/3")" "самотест дампа: 3/3"

echo "── 6. пересоздание упало при живой ручке — откат, а не «deploy ok» ──"
: > "$STAND/docker.log"; : > "$STAND/poryadok"
sleep 1   # тег выката именуется по секунде
conf "$ORDER_CONF
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok\"
STACK_RELEASE_CMD=\"echo peresozdanie >> $STAND/poryadok; false\"
STACK_HEALTH_CMD=\"true\""
svezhee_da
run_deploy
itog "1" "$RC" "код выхода ненулевой"
itog "нет" "$(has "$VYVOD" "deploy ok")" "успех не объявлен"
itog "sborka damp migracii peresozdanie otkat" "$(order)" "после отказа пересоздания — подъём отката"
itog "да" "$(retagged)" "образ возвращён с :previous на :latest"

echo "── 7. миграции упали — образы на :previous, путь к дампу и команда восстановления ──"
: > "$STAND/docker.log"; : > "$STAND/poryadok"
sleep 1
conf "$ORDER_CONF
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok; false\"
STACK_RELEASE_CMD=\"echo peresozdanie >> $STAND/poryadok\"
STACK_DB_RESTORE_HINT=\"vosstanovi-tak\"
STACK_HEALTH_CMD=\"true\""
svezhee_da
run_deploy
itog "1" "$RC" "код выхода ненулевой"
itog "sborka damp migracii" "$(order)" "после упавших миграций службы не пересоздавались"
itog "да" "$(retagged)" "образ возвращён с :previous на :latest"
itog "да" "$(has "$VYVOD" "база.dump")" "путь к дампу назван"
itog "да" "$(has "$VYVOD" "vosstanovi-tak")" "команда восстановления названа (не выполнена)"

echo "── 8. ручка живости командой: живо — выкат ok ──"
: > "$STAND/docker.log"; : > "$STAND/poryadok"
sleep 1
conf "$ORDER_CONF
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok\"
STACK_RELEASE_CMD=\"echo peresozdanie >> $STAND/poryadok\"
STACK_HEALTH_CMD=\"true\"
DEVMAP_AUDIT_DAYS=0"
svezhee_da
# Шаг 7 зовёт сверку карты и автопамяти ПРОЕКТА; на стенде их нет — подменяем
# каталог скриптов копией без них, чтобы проба не трогала машину.
SCRIPTS_COPY="$STAND/scripts"
mkdir -p "$SCRIPTS_COPY"
cp "$HERE/deploy.sh" "$HERE/deploy_guard.py" "$HERE/check-secrets.sh" "$SCRIPTS_COPY/"
cp -r "$HERE/lib" "$SCRIPTS_COPY/"
printf 'import sys\nsys.exit(0)\n' > "$SCRIPTS_COPY/fix-claude-mem-project.py"
VYVOD=$(PATH="$FAKEBIN:$PATH" DOCKER_JOURNAL="$STAND/docker.log" \
        HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
        timeout 300 bash "$SCRIPTS_COPY/deploy.sh" 2>&1)
RC=$?
printf '%s\n' "$VYVOD" | sed 's/^/      /' | tail -3
itog "0" "$RC" "выкат с живой ручкой выходит нулём"
itog "да" "$(has "$VYVOD" "deploy ok")" "успех объявлен"
itog "нет" "$(retagged)" "отката не было"

echo "── 9. БОЛЬНОЙ СЛУЧАЙ: запуск ОТНОСИТЕЛЬНЫМ путём — sg получает путь к самому deploy.sh ──"
# Перезапуск под sg docker зовёт скрипт заново по его пути. Путь брался
# `readlink -f "$0"` ПОСЛЕ `cd "$PRODUCT_TREE"`, и относительный «./scripts/
# deploy.sh» разрешался внутри дерева ПРОДУКТА: файла там нет, перезапуск шёл
# в пустоту. Свои sg и getent в отдельном PATH: случай 3 судит настоящий sg.
SGBIN=$(mktemp -d -p "$STAND")
cp "$FAKEBIN/docker" "$SGBIN/docker"
cat > "$SGBIN/sg" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$3" > "$SG_JOURNAL"
exit 0
SH
cat > "$SGBIN/getent" <<'SH'
#!/usr/bin/env bash
printf 'docker:x:999:%s\n' "$(id -un)"
SH
chmod +x "$SGBIN/sg" "$SGBIN/getent"
conf "STACK_BUILD_CMD=\"echo sborka\"
STACK_IMAGES=\"proba-api\"
STACK_HEALTH_URL=\"$DEAD_URL\""
svezhee_da
# Каталог скриптов есть и в продукте (пустой, гит его не видит): так
# ошибочный путь разрешается в ЧУЖОЕ дерево, а не в пустоту, и это видно.
mkdir -p "$PRODUCT/$(basename "$HERE")"
SAM="$(readlink -f "$HERE/deploy.sh")"
VYVOD=$(cd "$(dirname "$HERE")" && env -i PATH="$SGBIN:$FAKEBIN:/usr/bin:/bin" HOME="$STAND" \
        DOCKER_DEAD=1 SG_JOURNAL="$STAND/sg.log" \
        HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
        timeout 120 bash "./$(basename "$HERE")/deploy.sh" --силой 2>&1)
RC=$?
printf '%s\n' "$VYVOD" | sed 's/^/      /' | tail -3
itog "да" "$(has "$VYVOD" "перезапуск под sg docker")" "ветка sg достигнута"
SG_STROKA="$(cat "$STAND/sg.log" 2>/dev/null || true)"
printf '      строка sg: %s\n' "$SG_STROKA"
itog "да" "$(has "$SG_STROKA" "'$SAM'")" "sg зовёт сам deploy.sh ($SAM), а не путь в дереве продукта"
itog "нет" "$(has "$SG_STROKA" "$PRODUCT/")" "в строке sg нет пути внутри продукта"
itog "да" "$(has "$SG_STROKA" "'--силой'")" "ключ выката доехал до перезапуска"

run_copy() {  # $1 — каталог копии скриптов, дальше ключи deploy.sh; код — в RC
    local kopiya="$1"
    shift
    VYVOD=$(PATH="$FAKEBIN:$PATH" DOCKER_JOURNAL="$STAND/docker.log" \
            HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
            timeout 300 bash "$kopiya/deploy.sh" "$@" 2>&1)
    RC=$?
    printf '%s\n' "$VYVOD" | sed 's/^/      /' | tail -3
}
sborok() { grep -c '^sborka$' "$STAND/poryadok" 2>/dev/null || true; }
soglasie_zhivo() {  # спросить ворота стенда, не тратя: дожило ли «да»
    PATH="$FAKEBIN:$PATH" HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
        timeout 60 python3 "$SCRIPTS_COPY/deploy_guard.py" --только-согласие --не-тратить 2>&1 \
        | grep -q "согласие владельца есть" && echo да || echo нет
}

# Окно «той же попытки» закрыто (-1): иначе второй выкат шёл бы законно как
# продолжение первого, и расход «да» прятался бы за повтором.
ODNO_DA_CONF="$ORDER_CONF
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok\"
STACK_RELEASE_CMD=\"echo peresozdanie >> $STAND/poryadok\"
STACK_HEALTH_CMD=\"true\"
DEVMAP_AUDIT_DAYS=0
CONFIRM_RETRY_SEC=-1"

echo "── 10. БОЛЬНОЙ СЛУЧАЙ: одно «да» — один выкат; ключи ворот deploy.sh дальше не пропускает ──"
# Ворота знают служебные ключи --вернуть-согласие, --только-согласие и
# --не-тратить: ими зовут их другие двери. Пропусти deploy.sh их воротам, и
# выкат идёт, а «да» остаётся целым или возвращается — выкатов сколько угодно.
conf "$ODNO_DA_CONF"
: > "$STAND/docker.log"; : > "$STAND/poryadok"
svezhee_da
sleep 1
run_copy "$SCRIPTS_COPY"
itog "0" "$RC" "первый выкат на свежем «да» выходит нулём"
itog "1" "$(sborok)" "первый выкат собрал образ"
for kluchi in "--вернуть-согласие" "--только-согласие --не-тратить" "--не-тратить"; do
    sleep 1
    # shellcheck disable=SC2086
    run_copy "$SCRIPTS_COPY" $kluchi
    pervyj="${kluchi%% *}"
    itog "да" "$([ "$RC" != 0 ] && echo да || echo нет)" "deploy.sh $kluchi отказан (код $RC)"
    itog "да" "$(has "$VYVOD" "незнакомый ключ $pervyj")" "deploy.sh $kluchi: отказ называет «незнакомый ключ $pervyj»"
done
itog "1" "$(sborok)" "служебные ключи ворот не дали ни одной сборки сверх первой"
itog "нет" "$(soglasie_zhivo)" "«да» после выката потрачено, а не возвращено"
sleep 1
run_copy "$SCRIPTS_COPY"
itog "1" "$RC" "второй выкат на том же «да» отказан"
itog "да" "$(has "$VYVOD" "нет согласия")" "второй выкат отказан словами «нет согласия»"
itog "1" "$(sborok)" "за всю серию на одном «да» — одна сборка"

echo "── 10б. контроль: незнакомый ключ назван по имени, а не «занято» ──"
sleep 1
svezhee_da
run_copy "$SCRIPTS_COPY" --help
itog "1" "$RC" "deploy.sh --help — код 1"
itog "да" "$(has "$VYVOD" "незнакомый ключ --help")" "отказ называет «незнакомый ключ --help»"
itog "нет" "$(has "$VYVOD" "занято")" "отказ не называет причиной «занято»"
itog "да" "$(soglasie_zhivo)" "отказ по ключу «да» не сжёг"

echo "── 10в. БОЛЬНОЙ СЛУЧАЙ: --силой без «да» — отказ «нет согласия», --силой снимает только занятость ──"
conf "$ODNO_DA_CONF"
: > "$STAND/poryadok"
rm -f "$STAND/zhurnal/confirmations.jsonl" "$STAND/zhurnal/согласие.json"
sleep 1
run_copy "$SCRIPTS_COPY" --силой
itog "1" "$RC" "deploy.sh --силой без «да» — код 1"
itog "да" "$(has "$VYVOD" "нет согласия")" "отказ называет «нет согласия»"
itog "нет" "$(has "$VYVOD" "пойдёт по живому")" "отсутствие согласия не числится занятостью, которую снимает --силой"
itog "0" "$(sborok)" "без «да» ни одной сборки"

echo "── 11. ворота отказали разбором ключей (код 2) — причина их словами, не «занято» ──"
# Ворота-заглушка с кодом 2: ветку судит её собственный вход, каким бы ключом
# deploy.sh ни звал ворота.
KOPIYA_KOD2="$STAND/scripts-kod2"
mkdir -p "$KOPIYA_KOD2"
cp "$HERE/deploy.sh" "$HERE/check-secrets.sh" "$KOPIYA_KOD2/"
cp -r "$HERE/lib" "$KOPIYA_KOD2/"
printf 'print("незнакомый ключ --proba-zaglushki; известны: --силой")\nraise SystemExit(2)\n' \
    > "$KOPIYA_KOD2/deploy_guard.py"
conf "$ODNO_DA_CONF"
: > "$STAND/poryadok"
svezhee_da
sleep 1
run_copy "$KOPIYA_KOD2"
itog "1" "$RC" "код 2 ворот — выкат отменён кодом 1"
itog "да" "$(has "$VYVOD" "ворота отказали: незнакомый ключ --proba-zaglushki")" "причина названа словами ворот"
itog "нет" "$(has "$VYVOD" "ворота: занято")" "отказ разбора не выдан за «занято»"
itog "0" "$(sborok)" "после отказа ворот ни одной сборки"

printf '\nВЫКАТ-ПРОДУКТА: путей %d, неудач %d\n' "$paths" "$fails"
[ "$fails" -eq 0 ]
