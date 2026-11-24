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
# here-string, не конвейер: grep -q выходит на первом совпадении, и printf в конвейере под
# pipefail получал SIGPIPE (141) — «нет» на совпавшем тексте (флейк под нагрузкой).
has() { grep -qF -- "$2" <<<"$1" && echo да || echo нет; }

STAND=$(mktemp -d -t proba-deploy-product-XXXXXX)
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
TG_CHAT_ID="4242"
CONF

for repo in "$HARNESS" "$PRODUCT"; do
    printf 'стенд\n' > "$repo/README"
    git -C "$repo" init -q
    git -C "$repo" -c user.email="proba@stend" -c user.name="проба" add -A
    git -C "$repo" -c user.email="proba@stend" -c user.name="проба" commit -qm "стенд"
done

# Подставной docker: пишет вызовы в журнал; DOCKER_DEAD=1 — «сокет не пускает».
# DOCKER_ONLY_TAG=<тег> — образов нет, кроме собранного этим выкатом (первый выкат:
# ни :latest, ни образа прошлого выпуска).
# Память id (файл docker-state рядом с bin/): откат идёт по id образа живого выката (решение
# I2 раунда ревью фазы 2), и стенд без памяти ответил бы на запрос id пустой строкой. Образ,
# о котором стенд ещё не слышал, есть всегда, его id — sha256 имени (64 hex, как у docker);
# tag переносит id; ссылка «sha256:…» находит образ по id.
cat > "$FAKEBIN/docker" <<'SH'
#!/usr/bin/env bash
[ "${DOCKER_DEAD:-0}" = 1 ] && { echo "permission denied while trying to connect to the docker API" >&2; exit 1; }
printf '%s\n' "$*" >> "${DOCKER_JOURNAL:-/dev/null}"
state="$(dirname "$(readlink -f "$0")")/../docker-state"
touch "$state"
norm() { case "${1##*/}" in *:*) printf '%s' "$1" ;; *) printf '%s:latest' "$1" ;; esac; }
get() {
    local r id
    case "$1" in
        sha256:*) awk -v i="$1" '$2 == i { print i; exit }' "$state"; return ;;
    esac
    r=$(norm "$1")
    id=$(awk -v r="$r" '$1 == r { id = $2 } END { if (id) print id }' "$state")
    [ -n "$id" ] || { id="sha256:$(printf '%s' "$r" | sha256sum | cut -c1-64)"; printf '%s %s\n' "$r" "$id" >> "$state"; }
    printf '%s\n' "$id"
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
    for ref in "$@"; do
        if [ -n "${DOCKER_ONLY_TAG:-}" ]; then
            case "$ref" in *":$DOCKER_ONLY_TAG") ;; *) echo "Error: No such image: $ref" >&2; exit 1 ;; esac
        fi
        id=$(get "$ref")
        [ -n "$id" ] || { echo "Error: No such image: $ref" >&2; exit 1; }
        case "$fmt" in
            "") printf '[{"Id": "%s", "RepoTags": ["%s"]}]\n' "$id" "$(norm "$ref")" ;;
            *) printf '%s\n' "$id" ;;
        esac
    done
}
case "${1:-}" in
    image) [ "${2:-}" = inspect ] || exit 0; shift 2; inspect "$@" ;;
    inspect) shift; inspect "$@" ;;
    tag)
        [ -z "${DOCKER_ONLY_TAG:-}" ] || exit 0
        id=$(get "$2"); [ -n "$id" ] || { echo "Error: No such image: $2" >&2; exit 1; }
        put "$3" "$id" ;;
esac
exit 0
SH
chmod +x "$FAKEBIN/docker"
id_of() { PATH="$FAKEBIN:$PATH" docker image inspect --format '{{.Id}}' "$1" 2>/dev/null || true; }

conf() {  # пишет harness.conf стенда; $1 — добавочные строки
    {
        printf 'PRODUCT_DIR="proba"\nPRODUCT_REPO="%s"\nBACKUP_DIR="%s/kopii"\n' "$PRODUCT" "$STAND"
        # Потолок живости стенда: мёртвая ручка не ждёт 30 попыток по 2 с в каждом случае.
        printf 'DEPLOY_HEALTH_WAIT_SEC="4"\n'
        printf '%s\n' "$1"
    } > "$STAND/harness.conf"
}

run_deploy() {  # $@ — ключи deploy.sh; печатает вывод, код — в RC (копия scripts/ с обёрткой ворот)
    VYVOD=$(PATH="$FAKEBIN:$PATH" DOCKER_JOURNAL="$STAND/docker.log" \
            HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
            timeout 300 bash "$SCRIPTS_COPY/deploy.sh" "$@" 2>&1)
    RC=$?
    printf '%s\n' "$VYVOD" | sed 's/^/      /' | tail -6
}

# Журнал слова и ключи согласия судья берёт у носителя: отдельным процессом — у машины,
# поэтому ворота стенда идут обёрткой с носителем стенда. Журнал входящих есть всегда —
# без него отзыв прочитать нечем, и это отказ.
export HARNESS_LOG_DIR="$STAND/zhurnal"
# Дверь берёт конфиг только своей дверью (АР-3): HARNESS_INSTALL_CONF/HARNESS_CONF она не
# слушает, а паспорт стенда называет HARNESS_DOOR_STAND_HOME — и только КОПИИ вне PROJECT_DIR
# машины. Поэтому каждый запуск ниже — копия deploy.sh в $STAND; настоящий файл пошёл бы на
# паспорт машины. HARNESS_INSTALL_CONF/HARNESS_CONF остаются для детей двери.
export HARNESS_DOOR_STAND_HOME="$STAND"
# Занятость ворота судят по /proc МАШИНЫ: чужой claude -p или pytest старше порога
# останавливает выкат стенда «занято», и чистая копия краснеет от того, что идёт рядом.
# Занятость судят пробы ворот; здесь порог за пределом любой работы.
export DEPLOY_BUSY_MIN_SEC=999999999
: > "$STAND/zhurnal/inbox.jsonl"

# Обёртка ворот пробы: настоящие ворота deploy_guard.py в процессе обёртки, их судье —
# носитель стенда. После карточки А судья не слушает окружение ни при каком ключе
# (спека §3.2), и отдельным процессом дверь «Да» стенда не видит — открытие проба
# судит только так (отступление 7 плана), отказ — и отдельным процессом.
# Без Носитель у судьи (до А3) обёртка печатает «ОБЁРТКА ПРОБЫ: …» и выходит кодом 3.
obertka_vorot() {  # $1=файл обёртки $2=настоящие ворота $3=паспорт стенда $4=ручки стенда
    # Путь обёртки бывает ссылкой на настоящие ворота (проект стенда из ссылок): открыть его
    # на запись значило бы переписать сами ворота, поэтому ссылка снимается до записи.
    rm -f "$1"
    python3 - "$1" "$(readlink -f "$2")" "$3" "$4" <<'PY'
import sys
out, real, passport, handles = sys.argv[1:5]
head = f"REAL, PASSPORT, HANDLES = {real!r}, {passport!r}, {handles!r}\n"
body = r'''
import dataclasses, importlib.util, pathlib, sys

def number(value, default):
    text = str(value or "").strip()
    return int(text) if text.isdigit() else default

def stand_carrier(judge, config):
    cls = getattr(judge, "Носитель", None)
    if cls is None or not dataclasses.is_dataclass(cls):
        raise LookupError("в судье нет dataclass Носитель")
    values, keys = config.из_файла(PASSPORT), config.из_файла(HANDLES)
    folder = pathlib.Path(values.get("LOG_DIR") or PASSPORT)
    known = {"папка": folder, "uid": None, "паспорт": values, "граница": folder,
             "окно_мин": number(keys.get("CONFIRM_MAX_AGE_MIN"), 60),
             "срок_кнопки_мин": number(keys.get("CONSENT_BUTTON_TTL_MIN"), None), "ручки": keys}
    fields = dataclasses.fields(cls)
    unknown = [f.name for f in fields if f.name not in known
               and f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING]
    if unknown:
        raise LookupError(f"у Носитель обязательные поля, которых проба не знает: {unknown}")
    return cls(**{f.name: known[f.name] for f in fields if f.name in known})

sys.argv[0] = REAL
spec = importlib.util.spec_from_file_location("deploy_guard", REAL)
door = importlib.util.module_from_spec(spec)
spec.loader.exec_module(door)
try:
    carrier = stand_carrier(door.слово_вл, door.конф)
except LookupError as trouble:
    print(f"ОБЁРТКА ПРОБЫ: {trouble}")
    sys.exit(3)
door.слово_вл.носитель = lambda: carrier
sys.exit(door.main())
'''
with open(out, "w", encoding="utf-8") as fh:
    fh.write("# Обёртка пробы (носитель стенда): сгенерирована пробой, не править.\n" + head + body)
PY
}

# Копия scripts/ стенда: deploy.sh зовёт ворота рядом с собой ($SCRIPT_DIR по readlink -f).
# Шаг 7 зовёт сверку карты и автопамяти ПРОЕКТА; на стенде их нет — в копии их нет тоже,
# автопамять заглушена, чтобы проба не трогала машину.
SCRIPTS_COPY="$STAND/scripts"
mkdir -p "$SCRIPTS_COPY"
cp "$HERE/deploy.sh" "$HERE/check-secrets.sh" "$SCRIPTS_COPY/"
cp -r "$HERE/lib" "$SCRIPTS_COPY/"
printf 'import sys\nsys.exit(0)\n' > "$SCRIPTS_COPY/fix-claude-mem-project.py"
obertka_vorot "$SCRIPTS_COPY/deploy_guard.py" "$HERE/deploy_guard.py" "$STAND/install.conf" "$STAND/harness.conf"

svezhee_da() {  # свежее «Да» кнопкой на предмет выката «<sha продукта> <realpath дерева>»
    # Секунда паузы: строка журнала выкатов прошлого случая в ту же секунду погасила бы это «Да».
    sleep 1
    python3 - "$STAND/zhurnal" "$(git -C "$PRODUCT" rev-parse HEAD) $(readlink -f "$PRODUCT")" <<'PY'
import datetime, json, sys
log, target = sys.argv[1], sys.argv[2]
# Метка — текущая секунда: строка журнала выкатов той же секунды гасит «Да» (начат ≥ нажатия).
ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
data = {"nonce": "proba-vykat", "вид": "выкат", "цель": target, "message_id": 4000, "from_id": "4242"}
# Живой порядок: просьба ask-consent не позже нажатия, нажатие — с from_id владельца стенда.
with open(f"{log}/consent-requests.jsonl", "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"v": 1, "ts": ts, "src": "scripts/ask-consent.py", "event": "consent" + ".request",
                         "outcome": "ok", "data": {"nonce": "proba-vykat", "вид": "выкат", "цель": target,
                                                  "почему": "проба"}}, ensure_ascii=False) + "\n")
with open(f"{log}/consent-buttons.jsonl", "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"v": 1, "ts": ts, "src": "проба", "event": "consent" + ".yes", "outcome": "ok",
                         "data": data}, ensure_ascii=False) + "\n")
PY
}

DEAD_URL="http://127.0.0.1:9/nikogo"   # порт discard: соединение отказано сразу

echo "── 1. продукт объявлен, секция STACK_* пуста — отказ, а не «нечего» кодом 0 ──"
conf ''
run_deploy
itog "1" "$RC" "код выхода ненулевой"
itog "да" "$(has "$VYVOD" "STACK_BUILD_CMD")" "отказ называет пустой ключ"

# Находка копии сети 10.11.2026: установщик кладёт ТОЛЬКО PRODUCT_PATH, PRODUCT_DIR и
# PRODUCT_REPO пусты, compose у продукта, а не в каталоге харнеса — выкат отвечал
# «выкатывать нечего» кодом 0 о продукте, который на машине объявлен.
echo "── 1б. объявлен только PRODUCT_PATH (как кладёт установщик) — отказ с путём к заготовке ──"
printf 'PRODUCT_PATH="%s"\nBACKUP_DIR="%s/kopii"\n' "$PRODUCT" "$STAND" > "$STAND/harness.conf"
run_deploy
itog "1" "$RC" "1б код выхода ненулевой"
itog "да" "$(has "$VYVOD" "stack-template.py")" "1б отказ называет заготовку из compose продукта"

# Запуск из `env -i`: локаль C, как у юнита и cron. Под ней bash `%q` кодирует
# кириллицу в $'…', а sg исполняет строку через dash, который этого не знает:
# «--план» доезжал искажённым и шёл мимо плана в ворота (ревью 14.10.2026).
run_bare() {  # $@ — ключи deploy.sh; docker «мёртв», окружение пустое
    VYVOD=$(env -i PATH="$FAKEBIN:/usr/bin:/bin" HOME="$STAND" DOCKER_DEAD=1 \
            HARNESS_DOOR_STAND_HOME="$STAND" \
            HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
            timeout 120 bash "$SCRIPTS_COPY/deploy.sh" "$@" 2>&1)
    RC=$?
    printf '%s\n' "$VYVOD" | sed 's/^/      /' | tail -4
}

order() { tr '\n' ' ' < "$STAND/poryadok" | sed 's/ $//'; }
# Откат — ретег образа ПРОШЛОГО ЖИВОГО ВЫКАТА (sha12 последнего тега deploy-* с итогом
# «выкачено») в :latest, не снимка :previous и не образа новейшего тега: тег ставится до
# сборки, и у проваленного выката он тоже есть. Живой выкат проба помнит САМА (LIVE12
# после выката, вышедшего нулём): прочитай она его из журнала итогов, который пишет
# дверь, — «итог провала записан как выкачено» обманул бы и дверь, и пробу разом.
newest_tag12() { git -C "$PRODUCT" rev-list -n1 "$(git -C "$PRODUCT" tag -l 'deploy-*' --sort=-refname | head -1)" 2>/dev/null | cut -c1-12; }
LIVE12=""
LIVE_ID=""
remember_live() {  # после выката: вышел нулём — его тег теперь последний живой, его образ — :latest
    [ "$RC" = 0 ] && { LIVE12=$(newest_tag12); LIVE_ID=$(id_of proba-api:latest); }
    return 0
}
# Откат по id (решение I2): тег :<sha12> повторная сборка того же sha перезаписывает, id — нет.
# Судится и вызов (ретег из id живого образа), и итог (id :latest после отката).
retagged_id() {  # $1 — id образа: «да», если он ретегнут в :latest по id (с префиксом sha256: или без)
    [ -n "$1" ] && grep -qxE "tag (sha256:)?${1#sha256:} proba-api:latest" "$STAND/docker.log" && echo да || echo нет
}
# Возврат на образ по ЛЮБОМУ адресу — тегу :<sha12> или id: отрицание судит оба.
retagged_from() {  # $1 — sha12
    { has "$(cat "$STAND/docker.log")" "tag proba-api:$1 proba-api:latest" | grep -q да \
      || [ "$(retagged_id "$(id_of "proba-api:$1")")" = да ]; } && echo да || echo нет
}
retagged_previous() { has "$(cat "$STAND/docker.log")" "tag proba-api:previous proba-api:latest"; }
bump() { git -C "$PRODUCT" -c user.email="proba@stend" -c user.name="проба" commit -q --allow-empty -m "$1"; }

echo "── 2. --план: что сделал бы, без ворот и без выката ──"
conf "STACK_BUILD_CMD=\"echo sborka\"
STACK_MIGRATE_CMD=\"echo migracii\"
STACK_RELEASE_CMD=\"echo peresozdanie\"
STACK_HEALTH_URL=\"$DEAD_URL\""
rm -f "$STAND/zhurnal/согласие.json" "$STAND/zhurnal/confirmations.jsonl" "$STAND/zhurnal/consent-buttons.jsonl"
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

# Характеризующие случаи: разбор deploy.sh на функции этапов поведения не меняет, и
# судит это не чтение кода, а вывод. Эталон снят первым прогоном на deploy.sh до
# разбора; фаза, которая меняет план или порядок, правит эталон своим коммитом (фаза 2:
# строка «образы» в плане, сверка и продвижение :<sha12> в вызовах docker; фаза 3б: этап
# тестов T и необязательные шаги прода — фронт после живости, смоук последним, как в цепочке).
plan_text() {  # вывод --план без меток времени, стенд назван <STAND>
    printf '%s\n' "${VYVOD//"$STAND"/<STAND>}" | sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8}  //'
}
plan_matches() {  # $1 — эталон; «да», если вывод --план равен ему слово в слово
    local raznica
    raznica=$(diff <(printf '%s\n' "$1") <(plan_text)) && { echo да; return; }
    printf '%s\n' "$raznica" | sed 's/^/      /' >&2
    echo нет
}

echo "── 2в. --план слово в слово: минимальный конфиг, с BACKUP_DBS, со всеми STACK_* ──"
conf "STACK_BUILD_CMD=\"echo sborka\""
run_deploy --план
itog "да" "$(plan_matches "$(cat <<'PLAN'
план выката (ничего не выполняется):
  репозиторий продукта: <STAND>/produkt (тег deploy-…, сверка секретов, манифесты)
  дерево продукта: чисто
  образы: сборка получает IMAGE_TAG=<sha12>, :<sha12> продвигается в :latest; откат — на образ последнего живого выката
  образы для снимка :previous: <пусто — отката не будет>
  сборка: echo sborka
  сборка --no-cache (смена манифестов): <пусто — обычная сборка>
  тесты (этап T): <пусто — пропущен: не задано STACK_CHECKS_FILE>
  дамп базы: <пусто — без дампа>
  миграции: <пусто>
  пересоздание служб: <пусто — службы поднимает сборка>
  живость: <пусто — выкат откажет>
  фронт (необязательный): <пусто>
  подъём при откате: <пусто>
  восстановление базы (руками): pg_restore по дампу
  reload прокси: <пусто>
  смоук (необязательный): <пусто>
PLAN
)")" "2в план минимального конфига равен эталону"

conf "STACK_BUILD_CMD=\"echo sborka\"
BACKUP_DBS=\"alfa=echo damp-alfa
beta=echo damp-beta\""
run_deploy --план
itog "да" "$(plan_matches "$(cat <<'PLAN'
план выката (ничего не выполняется):
  репозиторий продукта: <STAND>/produkt (тег deploy-…, сверка секретов, манифесты)
  дерево продукта: чисто
  образы: сборка получает IMAGE_TAG=<sha12>, :<sha12> продвигается в :latest; откат — на образ последнего живого выката
  образы для снимка :previous: <пусто — отката не будет>
  сборка: echo sborka
  сборка --no-cache (смена манифестов): <пусто — обычная сборка>
  тесты (этап T): <пусто — пропущен: не задано STACK_CHECKS_FILE>
  дамп базы: все базы BACKUP_DBS через backup.sh --базы-в <STAND>/kopii/перед-выкатом/<метка>-<sha8>
  миграции: <пусто>
  пересоздание служб: <пусто — службы поднимает сборка>
  живость: <пусто — выкат откажет>
  фронт (необязательный): <пусто>
  подъём при откате: <пусто>
  восстановление базы (руками): pg_restore по дампу
  reload прокси: <пусто>
  смоук (необязательный): <пусто>
PLAN
)")" "2в план с BACKUP_DBS равен эталону"

conf "STACK_BUILD_CMD=\"echo sborka\"
STACK_BUILD_NOCACHE_CMD=\"echo sborka-bez-kesha\"
STACK_CRON_SRC=\"cron/produkt\"
STACK_DB_DUMP_CMD=\"echo damp\"
STACK_DB_RESTORE_HINT=\"vosstanovi-tak\"
STACK_HEALTH_CMD=\"echo zhivo\"
STACK_HEALTH_URL=\"$DEAD_URL\"
STACK_IMAGES=\"proba-api proba-web\"
STACK_MANIFESTS=\"requirements.txt\"
STACK_MIGRATE_CMD=\"echo migracii\"
STACK_PROXY_RELOAD_CMD=\"echo proksi\"
STACK_RELEASE_CMD=\"echo peresozdanie\"
STACK_UP_CMD=\"echo otkat\"
STACK_CHECKS_FILE=\"deploy/server-checks.yaml\"
STACK_CHECK_MIN_FREE_MB=\"4096\"
STACK_CHECK_UID=\"$(id -u):$(id -g)\"
STACK_FRONTEND_CMD=\"echo front\"
STACK_SMOKE_CMD=\"echo smoke\"
BACKUP_DBS=\"alfa=echo damp-alfa\""
run_deploy --план
itog "да" "$(plan_matches "$(cat <<'PLAN'
план выката (ничего не выполняется):
  репозиторий продукта: <STAND>/produkt (тег deploy-…, сверка секретов, манифесты)
  дерево продукта: чисто
  образы: сборка получает IMAGE_TAG=<sha12>, :<sha12> продвигается в :latest; откат — на образ последнего живого выката
  образы для снимка :previous: proba-api proba-web
  сборка: echo sborka
  сборка --no-cache (смена манифестов): echo sborka-bez-kesha
  тесты (этап T): deploy/server-checks.yaml — на образе :<sha12>, до лока и ворот
  дамп базы: echo damp
  миграции: echo migracii
  пересоздание служб: echo peresozdanie
  живость: echo zhivo
  фронт (необязательный): echo front
  подъём при откате: echo otkat
  восстановление базы (руками): vosstanovi-tak
  reload прокси: echo proksi
  смоук (необязательный): echo smoke
PLAN
)")" "2в план полного конфига равен эталону"

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

# 2г стоит после 4б: здоровый выкат ставит тег, а 4б судит, что тегов в продукте нет.
echo "── 2г. порядок шагов: снимок :previous до сборки, сборка до тега, продвижение в :latest после дампа ──"
# Сборка и дамп зовут подставной docker: журнал вызовов docker ставит снимок, сборку, дамп
# и продвижение в один ряд. Сборка пишет, сколько тегов выката было при ней: сборка идёт до
# ворот и тега (спека §5.1, фаза 3б), а :latest — прод — двигается только после «Да» и дампа.
conf "STACK_BUILD_CMD=\"docker build proba-api; echo sborka >> $STAND/poryadok; git -C $PRODUCT tag -l | wc -l > $STAND/tegov-pri-sborke\"
STACK_BUILD_NOCACHE_CMD=\"docker build --no-cache proba-api; echo sborka >> $STAND/poryadok; git -C $PRODUCT tag -l | wc -l > $STAND/tegov-pri-sborke\"
STACK_DB_DUMP_CMD=\"echo damp >> $STAND/poryadok; docker proba-damp; head -c 4096 /dev/zero\"
STACK_UP_CMD=\"echo otkat >> $STAND/poryadok\"
STACK_IMAGES=\"proba-api\"
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok\"
STACK_RELEASE_CMD=\"echo peresozdanie >> $STAND/poryadok\"
STACK_HEALTH_CMD=\"echo zhivo >> $STAND/poryadok\"
STACK_PROXY_RELOAD_CMD=\"echo proksi >> $STAND/poryadok\"
DEVMAP_AUDIT_DAYS=0
CONFIRM_RETRY_SEC=-1"
: > "$STAND/docker.log"; : > "$STAND/poryadok"
TEGOV_DO_2G=$(git -C "$PRODUCT" tag -l | wc -l)
svezhee_da
sleep 1
run_deploy
itog "0" "$RC" "2г здоровый выкат выходит нулём"
remember_live
itog "sborka damp migracii peresozdanie zhivo proksi" "$(order)" \
     "2г порядок этапов: сборка → дамп → миграции → пересоздание → живость → прокси"
# Сборка одна, образ :<sha12> продвинут в :latest после дампа и до миграций.
SHA12=$(git -C "$PRODUCT" rev-parse HEAD | cut -c1-12)
dline() { grep -nxF -- "$1" "$STAND/docker.log" | head -1 | cut -d: -f1; }
before() { [ -n "$1" ] && [ -n "$2" ] && [ "$1" -lt "$2" ] && echo да || echo нет; }
itog "1" "$(grep -c '^build ' "$STAND/docker.log")" "2г сборка одна"
itog "да" "$(before "$(dline "tag proba-api:latest proba-api:previous")" "$(dline "build --no-cache proba-api")")" \
     "2г вызовы docker: снимок :previous до сборки"
# Продвижение — ретег образа выката в :latest по тегу :<sha12> или по его id.
SHA12_ID=$(id_of "proba-api:$SHA12")
promote_line() { grep -nxE "tag (proba-api:$SHA12|(sha256:)?${SHA12_ID#sha256:}) proba-api:latest" "$STAND/docker.log" | head -1 | cut -d: -f1; }
itog "да" "$(before "$(dline "proba-damp")" "$(promote_line)")" \
     "2г вызовы docker: продвижение :$SHA12 в :latest после дампа (прод двигается после «Да» и дампа)"
itog "$TEGOV_DO_2G" "$(tr -d ' ' < "$STAND/tegov-pri-sborke" 2>/dev/null)" \
     "2г сборка шла до тега выката (тегов при сборке — как до выката)"

DUMP_LINE="STACK_DB_DUMP_CMD=\"echo damp >> $STAND/poryadok; head -c 4096 /dev/zero\""
ORDER_CONF="STACK_BUILD_CMD=\"echo sborka >> $STAND/poryadok\"
STACK_BUILD_NOCACHE_CMD=\"echo sborka >> $STAND/poryadok\"
$DUMP_LINE
STACK_UP_CMD=\"echo otkat >> $STAND/poryadok\"
STACK_IMAGES=\"proba-api\""

echo "── 5. БОЛЬНОЙ СЛУЧАЙ: красная ручка здоровья — откат на образ прошлого выката и ненулевой код ──"
: > "$STAND/docker.log"; : > "$STAND/poryadok"
# Свой коммит: образ прошлого выката (:<sha12> его тега) отличим от только что собранного.
# Живой выкат до него — 2г.
bump "перед-5"
PREV12=$LIVE12
PREV_ID=$LIVE_ID
itog "да" "$([ -n "$PREV12" ] && [ "$PREV12" != "$(git -C "$PRODUCT" rev-parse HEAD | cut -c1-12)" ] && echo да || echo нет)" \
     "5 условие: прошлый живой выкат (2г) есть и стоит на другом sha ($PREV12)"
itog "да" "$([ -n "$PREV_ID" ] && [ "$PREV_ID" != "$(id_of "proba-api:$(git -C "$PRODUCT" rev-parse HEAD | cut -c1-12)")" ] && echo да || echo нет)" \
     "5 условие: id образа живого выката известен и отличен от образа этого выката"
conf "$ORDER_CONF
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok\"
STACK_RELEASE_CMD=\"echo peresozdanie >> $STAND/poryadok\"
STACK_HEALTH_URL=\"$DEAD_URL\""
svezhee_da
run_deploy
itog "1" "$RC" "код выхода ненулевой"
itog "sborka damp migracii peresozdanie otkat" "$(order)" \
     "порядок: сборка → дамп → миграции → пересоздание → подъём отката"
itog "да" "$(retagged_id "$PREV_ID")" "5 образ прошлого выката :$PREV12 возвращён в :latest по id (${PREV_ID:0:19}…)"
itog "$PREV_ID" "$(id_of proba-api:latest)" "5 после отката id :latest — образ прошлого живого выката"
itog "нет" "$(retagged_previous)" "5 откат не на снимок :previous"
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
bump "перед-6"
# Новейший тег — проваленного выката 5; живой — по-прежнему 2г.
PREV12=$LIVE12
PREV_ID=$LIVE_ID
FAIL12=$(newest_tag12)
itog "да" "$([ -n "$PREV12" ] && [ -n "$FAIL12" ] && [ "$PREV12" != "$FAIL12" ] && echo да || echo нет)" \
     "6 условие: новейший тег ($FAIL12) — проваленного выката 5, не живого 2г ($PREV12)"
conf "$ORDER_CONF
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok\"
STACK_RELEASE_CMD=\"echo peresozdanie >> $STAND/poryadok; false\"
STACK_HEALTH_CMD=\"true\""
svezhee_da
run_deploy
itog "1" "$RC" "код выхода ненулевой"
itog "нет" "$(has "$VYVOD" "deploy ok")" "успех не объявлен"
itog "sborka damp migracii peresozdanie otkat" "$(order)" "после отказа пересоздания — подъём отката"
itog "да" "$(retagged_id "$PREV_ID")" "6 образ прошлого ЖИВОГО выката :$PREV12 возвращён в :latest по id"
itog "$PREV_ID" "$(id_of proba-api:latest)" "6 после отката id :latest — образ прошлого живого выката"
itog "нет" "$(retagged_from "$FAIL12")" "6 откат не на образ проваленного выката 5 :$FAIL12"

echo "── 7. миграции упали — образ прошлого выката в :latest, путь к дампу и команда восстановления ──"
: > "$STAND/docker.log"; : > "$STAND/poryadok"
sleep 1
bump "перед-7"
# Новейший тег — проваленного выката 6; живой — 2г.
PREV12=$LIVE12
PREV_ID=$LIVE_ID
FAIL12=$(newest_tag12)
itog "да" "$([ -n "$PREV12" ] && [ -n "$FAIL12" ] && [ "$PREV12" != "$FAIL12" ] && echo да || echo нет)" \
     "7 условие: новейший тег ($FAIL12) — проваленного выката 6, не живого 2г ($PREV12)"
conf "$ORDER_CONF
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok; false\"
STACK_RELEASE_CMD=\"echo peresozdanie >> $STAND/poryadok\"
STACK_DB_RESTORE_HINT=\"vosstanovi-tak\"
STACK_HEALTH_CMD=\"true\""
svezhee_da
run_deploy
itog "1" "$RC" "код выхода ненулевой"
itog "sborka damp migracii" "$(order)" "после упавших миграций службы не пересоздавались"
itog "да" "$(retagged_id "$PREV_ID")" "7 образ прошлого ЖИВОГО выката :$PREV12 возвращён в :latest по id"
itog "$PREV_ID" "$(id_of proba-api:latest)" "7 после отката id :latest — образ прошлого живого выката"
itog "нет" "$(retagged_from "$FAIL12")" "7 откат не на образ проваленного выката 6 :$FAIL12"
itog "нет" "$(retagged_previous)" "7 откат не на снимок :previous"
itog "да" "$(has "$VYVOD" "база.dump")" "путь к дампу назван"
itog "да" "$(has "$VYVOD" "vosstanovi-tak")" "команда восстановления названа (не выполнена)"

echo "── 7б. БОЛЬНОЙ СЛУЧАЙ: миграции упали, а образа для отката нет (первый выкат) — подсказка с дампом всё равно есть ──"
# Ревью фазы 1 (журнал 05:30): restore_images без снимка возвращает 1, и под set -e
# выкат обрывался до name_db_restore — владелец оставался с полунакатанной схемой и
# без пути к дампу. Образов нет, кроме собранного этим выкатом.
: > "$STAND/docker.log"; : > "$STAND/poryadok"
sleep 1
bump "перед-7б"
conf "$ORDER_CONF
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok; false\"
STACK_RELEASE_CMD=\"echo peresozdanie >> $STAND/poryadok\"
STACK_DB_RESTORE_HINT=\"vosstanovi-tak\"
STACK_HEALTH_CMD=\"true\""
svezhee_da
export DOCKER_ONLY_TAG
DOCKER_ONLY_TAG=$(git -C "$PRODUCT" rev-parse HEAD | cut -c1-12)
run_deploy
unset DOCKER_ONLY_TAG
itog "1" "$RC" "7б код выхода ненулевой"
itog "sborka damp migracii" "$(order)" "7б после упавших миграций службы не пересоздавались"
# «дамп до миграций», не голое «база.dump»: имя файла печатает и сам снимок дампа,
# до миграций, — подсказку восстановления судит только её строка.
itog "да" "$(has "$VYVOD" "дамп до миграций:")" "7б путь к дампу назван в подсказке восстановления"
itog "да" "$(has "$VYVOD" "vosstanovi-tak")" "7б команда восстановления названа"

echo "── 8. ручка живости командой: живо — выкат ok ──"
: > "$STAND/docker.log"; : > "$STAND/poryadok"
sleep 1
bump "перед-8"
PREV12=$LIVE12
PREV_ID=$LIVE_ID
conf "$ORDER_CONF
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok\"
STACK_RELEASE_CMD=\"echo peresozdanie >> $STAND/poryadok\"
STACK_HEALTH_CMD=\"true\"
DEVMAP_AUDIT_DAYS=0"
svezhee_da
VYVOD=$(PATH="$FAKEBIN:$PATH" DOCKER_JOURNAL="$STAND/docker.log" \
        HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
        timeout 300 bash "$SCRIPTS_COPY/deploy.sh" 2>&1)
RC=$?
printf '%s\n' "$VYVOD" | sed 's/^/      /' | tail -3
itog "0" "$RC" "выкат с живой ручкой выходит нулём"
remember_live
itog "да" "$(has "$VYVOD" "deploy ok")" "успех объявлен"
itog "нет" "$(retagged_from "$PREV12")" "отката не было: образ прошлого выката в :latest не возвращался"
itog "нет" "$(retagged_previous)" "отката не было: снимок :previous в :latest не возвращался"

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
mkdir -p "$PRODUCT/$(basename "$SCRIPTS_COPY")"
SAM="$(readlink -f "$SCRIPTS_COPY/deploy.sh")"
VYVOD=$(cd "$(dirname "$SCRIPTS_COPY")" && env -i PATH="$SGBIN:$FAKEBIN:/usr/bin:/bin" HOME="$STAND" \
        DOCKER_DEAD=1 SG_JOURNAL="$STAND/sg.log" HARNESS_DOOR_STAND_HOME="$STAND" \
        HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
        timeout 120 bash "./$(basename "$SCRIPTS_COPY")/deploy.sh" --силой 2>&1)
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
# Сборка идёт до ворот (спека §5.1, фаза 3б): «прод не тронут» судится миграциями, а не сборкой.
vykatov() { grep -c '^migracii$' "$STAND/poryadok" 2>/dev/null || true; }
soglasie_zhivo() {  # спросить ворота стенда: открывает ли ещё кнопка «Да»; «обёртка» — судить нечем
    local otvet
    otvet=$(PATH="$FAKEBIN:$PATH" HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
        timeout 60 python3 "$SCRIPTS_COPY/deploy_guard.py" --только-согласие 2>&1)
    if printf '%s' "$otvet" | grep -q "ОБЁРТКА ПРОБЫ"; then echo обёртка
    elif printf '%s' "$otvet" | grep -q "согласие владельца есть"; then echo да
    else echo нет; fi
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
# Служебные ключи ворот (--только-согласие, прежние --вернуть-согласие и --не-тратить)
# deploy.sh отказывает сам: пропусти он их воротам, выкат шёл бы мимо их суда. Кнопку
# «Да» гасит строка журнала выкатов первого выката — второй на той же кнопке отказан.
conf "$ODNO_DA_CONF"
: > "$STAND/docker.log"; : > "$STAND/poryadok"
svezhee_da
sleep 1
run_copy "$SCRIPTS_COPY"
itog "0" "$RC" "первый выкат на свежем «да» выходит нулём"
remember_live
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
itog "нет" "$(soglasie_zhivo)" "«Да» после выката погашено журналом выкатов"
sleep 1
run_copy "$SCRIPTS_COPY"
itog "1" "$RC" "второй выкат на том же «да» отказан"
itog "да" "$(has "$VYVOD" "нет согласия")" "второй выкат отказан словами «нет согласия»"
itog "1" "$(vykatov)" "за всю серию на одном «да» — один выкат (миграции один раз)"

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
rm -f "$STAND/zhurnal/confirmations.jsonl" "$STAND/zhurnal/согласие.json" "$STAND/zhurnal/consent-buttons.jsonl"
sleep 1
run_copy "$SCRIPTS_COPY" --силой
itog "1" "$RC" "deploy.sh --силой без «да» — код 1"
itog "да" "$(has "$VYVOD" "нет согласия")" "отказ называет «нет согласия»"
itog "нет" "$(has "$VYVOD" "пойдёт по живому")" "отсутствие согласия не числится занятостью, которую снимает --силой"
itog "0" "$(vykatov)" "без «да» прод не тронут: миграций нет"

echo "── 10г. выкат при занятом deploy.lock — второй одновременный отменён до ворот, сборок 0 ──"
# Улика 05.08: два одновременных deploy.sh — контейнеры под временными именами, API
# лёг на минуту. Лок держит стенд (flock, как держал бы идущий выкат); «Да» свежее,
# поэтому без лока выкат дошёл бы до сборки, и случай краснеет.
conf "$ODNO_DA_CONF"
: > "$STAND/poryadok"
svezhee_da
sleep 1
LOCKF="$STAND/zhurnal/locks/deploy.lock"
mkdir -p "$(dirname "$LOCKF")"
mkfifo "$STAND/lock-fifo"
# Держатель — подоболочка без детей: ждёт строку в fifo (не дольше 120 с) и уходит,
# закрыв fd 9, — flock снимается сам. Открытие <> не ждёт писателя.
( exec 9>> "$LOCKF"; flock 9; printf '%s\nвыкат пробы держит лок\n' "$BASHPID" > "$LOCKF"
  read -r -t 120 _ <> "$STAND/lock-fifo" ) &
LOCK_HOLDER=$!
for _ in $(seq 1 50); do flock -n "$LOCKF" true || break; sleep 0.1; done
itog "да" "$(flock -n "$LOCKF" true && echo нет || echo да)" "10г условие: deploy.lock занят держателем стенда"
run_copy "$SCRIPTS_COPY"
printf 'отпусти\n' > "$STAND/lock-fifo"
wait "$LOCK_HOLDER" 2>/dev/null || true
itog "1" "$RC" "10г выкат при занятом локе — код 1"
itog "да" "$(has "$VYVOD" "выкат уже идёт")" "10г сказано «выкат уже идёт»"
itog "0" "$(vykatov)" "10г прод не тронут: миграций нет"
itog "да" "$(soglasie_zhivo)" "10г отказ по локу «Да» не сжёг"

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
itog "0" "$(vykatov)" "после отказа ворот прод не тронут: миграций нет"

echo "── 12. hr.consent-buttons-doors: тег аннотированный на одобренном sha и строка журнала выкатов ──"
# Кнопку «Да» на выкат гасит строка журнала выкатов (тег сессия удалила бы одной
# командой); тег адресует ровно DEPLOY_SHA, а не HEAD на момент тега.
conf "$ODNO_DA_CONF"
: > "$STAND/poryadok"
rm -f "$STAND/zhurnal/deploy-releases.jsonl"
svezhee_da
sleep 1
SHA_DO=$(git -C "$PRODUCT" rev-parse HEAD)
run_copy "$SCRIPTS_COPY"
itog "0" "$RC" "выкат на свежем «да» выходит нулём"
remember_live
TEG=$(git -C "$PRODUCT" tag -l 'deploy-*' --sort=-refname | head -1)
itog "tag" "$(git -C "$PRODUCT" cat-file -t "$TEG" 2>/dev/null)" "тег выката $TEG — аннотированный"
itog "$SHA_DO" "$(git -C "$PRODUCT" rev-parse "$TEG^{commit}" 2>/dev/null)" "тег стоит на выкатываемом sha"
STROKA=$(tail -n 1 "$STAND/zhurnal/deploy-releases.jsonl" 2>/dev/null)
itog "$SHA_DO $(readlink -f "$PRODUCT") $TEG да" "$(printf '%s' "$STROKA" | python3 -c '
import datetime, json, sys
try:
    z = json.loads(sys.stdin.read())
    ts = "да" if z["v"] == 1 and z["event"] == "deploy.release" and datetime.datetime.fromisoformat(z["ts"]) else "нет"
    print(z["data"]["цель"], z["data"]["тег"], ts)
except Exception as e:
    print("не разобрано:", type(e).__name__)')" "конверт журнала выкатов: data.цель «sha дерево», data.тег, читаемый ts"

# Продвижение в прод — ретег :<sha12> в :latest: после него compose поднимает новый образ.
# Продвижение в :latest — по тегу или по id образа (sha256:…): «не продвигался» судит оба адреса.
promoted() { grep -cE '^tag [^ ]+ proba-api:latest$' "$STAND/docker.log" 2>/dev/null || true; }

echo "── 13. тег не встал — выкат отменён до продвижения в прод, строки журнала нет ──"
# Сборка идёт до ворот и тега (спека §5.1, фаза 3б): отказ тега обрывает выкат до :latest и служб.
conf "$ODNO_DA_CONF"
: > "$STAND/poryadok"; : > "$STAND/docker.log"
svezhee_da
sleep 1
STROK_DO=$(grep -c . "$STAND/zhurnal/deploy-releases.jsonl" 2>/dev/null || echo 0)
chmod a-w "$PRODUCT/.git/refs/tags"
run_copy "$SCRIPTS_COPY"
chmod u+w "$PRODUCT/.git/refs/tags"
itog "1" "$RC" "неудача тега — код 1"
itog "да" "$(has "$VYVOD" "не поставлен — выкат отменён до продвижения в прод")" "причина названа: тег не поставлен, до продвижения в прод"
itog "0" "$(promoted)" "13 :latest не продвигался"
itog "0" "$(vykatov)" "13 миграций нет"
itog "$STROK_DO" "$(grep -c . "$STAND/zhurnal/deploy-releases.jsonl" 2>/dev/null || echo 0)" "строки журнала выкатов не прибавилось"

echo "── 14. HEAD сдвинулся после ворот — выкат отменён до тега и продвижения в прод; сборка была до ворот; ворота получили --выкат ──"
KOPIYA_SDVIG="$STAND/scripts-sdvig"
mkdir -p "$KOPIYA_SDVIG"
cp "$HERE/deploy.sh" "$HERE/check-secrets.sh" "$KOPIYA_SDVIG/"
obertka_vorot "$KOPIYA_SDVIG/deploy_guard_real.py" "$HERE/deploy_guard.py" "$STAND/install.conf" "$STAND/harness.conf"
cp -r "$HERE/lib" "$KOPIYA_SDVIG/"
printf 'import sys\nsys.exit(0)\n' > "$KOPIYA_SDVIG/fix-claude-mem-project.py"
cat > "$KOPIYA_SDVIG/deploy_guard.py" <<'PY'
# Ворота стенда: настоящие, а после их «да» в дерево продукта приходит коммит.
import os, subprocess, sys
with open(os.environ["PROBA_GATE_ARGV"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
real = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy_guard_real.py")
done = subprocess.run([sys.executable, real, *sys.argv[1:]])
if done.returncode == 0 and "--выкат" in sys.argv:
    subprocess.run(["git", "-C", os.environ["PROBA_SHIFT_TREE"], "-c", "user.email=proba@stend",
                    "-c", "user.name=проба", "commit", "-q", "--allow-empty", "-m", "коммит после ворот"])
sys.exit(done.returncode)
PY
conf "$ODNO_DA_CONF"
: > "$STAND/poryadok"; : > "$STAND/docker.log"
svezhee_da
sleep 1
SHA_DO=$(git -C "$PRODUCT" rev-parse HEAD)
TEGOV_DO=$(git -C "$PRODUCT" tag -l 'deploy-*' | grep -c .)
: > "$STAND/gate-argv"
VYVOD=$(PATH="$FAKEBIN:$PATH" DOCKER_JOURNAL="$STAND/docker.log" PROBA_GATE_ARGV="$STAND/gate-argv" \
        PROBA_SHIFT_TREE="$PRODUCT" HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
        timeout 300 bash "$KOPIYA_SDVIG/deploy.sh" 2>&1)
RC=$?
printf '%s\n' "$VYVOD" | sed 's/^/      /' | tail -3
itog "1" "$RC" "HEAD сдвинут после ворот — код 1"
itog "да" "$(has "$VYVOD" "HEAD сдвинулся или дерево изменилось после согласия")" "причина названа: HEAD сдвинулся"
itog "1" "$(sborok)" "14 сборка шла до ворот (B до G)"
itog "0" "$(promoted)" "14 :latest не продвигался"
itog "0" "$(vykatov)" "14 миграций нет"
itog "$TEGOV_DO" "$(git -C "$PRODUCT" tag -l 'deploy-*' | grep -c .)" "тег не поставлен"
itog "да" "$(grep -qF -- "--выкат $PRODUCT $SHA_DO" "$STAND/gate-argv" && echo да || echo нет)" \
     "ворота получили --выкат <дерево> <sha до ворот>"

echo "── 15. HEAD сдвинулся во время сборки — отказ до дампа и тега, прод не тронут ──"
SDVIG_SBORKA="echo sborka >> $STAND/poryadok; git -C $PRODUCT -c user.email=proba@stend -c user.name=proba commit -q --allow-empty -m vo-vremya-sborki"
conf "STACK_BUILD_CMD=\"$SDVIG_SBORKA\"
STACK_BUILD_NOCACHE_CMD=\"$SDVIG_SBORKA\"
$DUMP_LINE
STACK_UP_CMD=\"echo otkat >> $STAND/poryadok\"
STACK_IMAGES=\"proba-api\"
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok\"
STACK_RELEASE_CMD=\"echo peresozdanie >> $STAND/poryadok\"
STACK_HEALTH_CMD=\"true\"
DEVMAP_AUDIT_DAYS=0
CONFIRM_RETRY_SEC=-1"
: > "$STAND/docker.log"; : > "$STAND/poryadok"
svezhee_da
sleep 1
TEGOV_DO=$(git -C "$PRODUCT" tag -l 'deploy-*' | grep -c .)
run_copy "$SCRIPTS_COPY"
itog "1" "$RC" "HEAD сдвинут во время сборки — код 1"
itog "да" "$(has "$VYVOD" "HEAD сдвинулся во время сборки")" "причина названа: сдвиг во время сборки"
# Сборка идёт до ворот (спека §5.1, фаза 3б): сдвиг ловится до дампа и тега — откатывать нечего.
itog "sborka" "$(order)" "15 после сдвига ни дампа, ни миграций, ни пересоздания"
itog "0" "$(promoted)" "15 :latest не продвигался — прод не тронут"
itog "$TEGOV_DO" "$(git -C "$PRODUCT" tag -l 'deploy-*' | grep -c .)" "15 тег не поставлен"

echo "── 16. D3: STACK_DB_DUMP_CMD пуст, BACKUP_DBS задан — дампы ВСЕХ баз через backup.sh --базы-в до миграций ──"
# У продукта с несколькими базами одна команда STACK_DB_DUMP_CMD защищает одну;
# остальные шли в миграцию без копии (И-1). Дверь зовёт backup.sh --базы-в, и
# неудача любой базы останавливает выкат до миграций. backup.sh кладётся копией
# рядом с копией скриптов стенда (дверь зовёт его от своего каталога, не от
# PROJECT_DIR паспорта): проба судит развилку, а не выбор пути. Дверь подмены —
# HARNESS_BACKUP_SH (мутированная копия).
BACKUP_SRC="${HARNESS_BACKUP_SH:-$HERE/../harness/demons/backup.sh}"
polozhit_backup() {  # $1 = файл, который лечь как backup.sh стенда
    local d
    d="$STAND"
    mkdir -p "$d/harness/demons" "$d/scripts/lib"
    cp "$1" "$d/harness/demons/backup.sh"
    [ -e "$d/scripts/lib/config.sh" ] || cp "$HERE/lib/config.sh" "$d/scripts/lib/config.sh"
}
polozhit_backup "$BACKUP_SRC"
printf -- '--\n-- PostgreSQL database dump\n--\nCREATE TABLE x();\n--\n-- PostgreSQL database dump complete\n--\n' > "$STAND/pg.dump"
BAZY_CONF="STACK_BUILD_CMD=\"echo sborka >> $STAND/poryadok\"
STACK_BUILD_NOCACHE_CMD=\"echo sborka >> $STAND/poryadok\"
STACK_UP_CMD=\"echo otkat >> $STAND/poryadok\"
STACK_IMAGES=\"proba-api\"
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok\"
STACK_RELEASE_CMD=\"echo peresozdanie >> $STAND/poryadok\"
STACK_HEALTH_CMD=\"true\"
STACK_DB_DUMP_CMD=\"\"
DEVMAP_AUDIT_DAYS=0
CONFIRM_RETRY_SEC=-1
BACKUP_KEEP_DAYS=14"
bazy_spisok() {  # $1 = команда базы beta → строки BACKUP_DBS стенда
    printf 'alfa=echo damp-alfa >> %s/poryadok; cat %s/pg.dump\nbeta=%s' "$STAND" "$STAND" "$1"
}
# Имена баз — РАЗБОРОМ списка, тем же правилом «имя=команда», что у backup.sh.
bazy_imena() { printf '%s\n' "$1" | sed 's/=.*//' | tr '\n' ' ' | sed 's/ $//'; }
dampy_vykata() {  # имена db-*.dump в $BACKUP_DIR/перед-выкатом стенда и их права
    find "$STAND/kopii/перед-выкатом" -name 'db-*.dump' -printf '%f:%m\n' 2>/dev/null | sort | tr '\n' ' ' | sed 's/ $//'
}

SPISOK=$(bazy_spisok "echo damp-beta >> $STAND/poryadok; cat $STAND/pg.dump")
conf "$BAZY_CONF
BACKUP_DBS=\"$SPISOK\""
IMENA=$(bazy_imena "$SPISOK")
itog "alfa beta" "$IMENA" "разбор BACKUP_DBS стенда дал список баз (пустой список — провал)"

echo "── 16а. --план называет дамп всех баз через backup.sh --базы-в ──"
run_deploy --план
itog "0" "$RC" "план выходит нулём"
itog "да" "$(printf '%s\n' "$VYVOD" | grep 'дамп базы' | grep -qF -- '--базы-в' && echo да || echo нет)" \
     "строка плана «дамп базы» называет backup.sh --базы-в, а не «без дампа»"

echo "── 16б. здоровый выкат: каждая база списка дала дамп 600 ДО миграций ──"
: > "$STAND/docker.log"; : > "$STAND/poryadok"
svezhee_da
sleep 1
run_copy "$SCRIPTS_COPY"
itog "0" "$RC" "выкат с дампами всех баз выходит нулём"
itog "sborka damp-alfa damp-beta migracii peresozdanie" "$(order)" \
     "порядок: сборка → дамп каждой базы → миграции → пересоздание"
itog "$(printf '%s\n' $IMENA | sed 's/.*/db-&.dump:600/' | sort | tr '\n' ' ' | sed 's/ $//')" "$(dampy_vykata)" \
     "в перед-выкатом по дампу на каждую базу списка, права 600"

echo "── 16в. БОЛЬНОЙ СЛУЧАЙ: дамп одной базы упал — выкат остановлен до миграций ──"
rm -rf "$STAND/kopii/перед-выкатом"
conf "$BAZY_CONF
BACKUP_DBS=\"$(bazy_spisok "echo damp-beta >> $STAND/poryadok; false")\""
: > "$STAND/docker.log"; : > "$STAND/poryadok"
svezhee_da
sleep 1
run_copy "$SCRIPTS_COPY"
itog "1" "$RC" "упавший дамп базы beta — код 1"
itog "да" "$(has "$(order)" "damp-beta")" "дамп базы beta звался"
itog "нет" "$(has "$(order)" "migracii")" "после упавшего дампа миграции не шли"
itog "нет" "$(has "$(order)" "peresozdanie")" "после упавшего дампа службы не пересоздавались"

echo "── 16г. БОЛЬНОЙ СЛУЧАЙ: backup.sh вышел нулём, а дампов нет — выкат остановлен ──"
# Дверь судит не только код: «ноль без файлов» (чужой прогон, пустой список,
# ранний выход) — это выкат без копии.
printf '#!/usr/bin/env bash\nexit 0\n' > "$STAND/backup-pustoj.sh"
polozhit_backup "$STAND/backup-pustoj.sh"
rm -rf "$STAND/kopii/перед-выкатом"
conf "$BAZY_CONF
BACKUP_DBS=\"$SPISOK\""
: > "$STAND/docker.log"; : > "$STAND/poryadok"
svezhee_da
sleep 1
run_copy "$SCRIPTS_COPY"
itog "1" "$RC" "backup.sh «0 без дампов» — код 1"
itog "нет" "$(has "$(order)" "migracii")" "миграции не шли"

echo "── 16д. БОЛЬНОЙ СЛУЧАЙ: backup.sh вышел нулём, а дампов меньше, чем баз — выкат остановлен ──"
# Один дамп на две базы — вторая шла бы в миграцию без копии; «есть хоть один
# файл» здесь не судья, судит число баз списка.
printf '#!/usr/bin/env bash\nmkdir -p "$2" && cat %s/pg.dump > "$2/db-alfa.dump"\n' "$STAND" > "$STAND/backup-odin.sh"
polozhit_backup "$STAND/backup-odin.sh"
rm -rf "$STAND/kopii/перед-выкатом"
: > "$STAND/docker.log"; : > "$STAND/poryadok"
svezhee_da
sleep 1
run_copy "$SCRIPTS_COPY"
itog "1" "$RC" "backup.sh «0 и один дамп на две базы» — код 1"
itog "нет" "$(has "$(order)" "migracii")" "миграции не шли"
polozhit_backup "$BACKUP_SRC"

echo "── 16е. упавшие миграции после дампов всех баз: подсказка восстановления — для каталога дампов ──"
# Дамп выката здесь — каталог db-<имя>.dump, а не файл: «pg_restore … --clean
# <каталог>» владелец исполнил бы впустую. Подсказка называет файлы баз.
rm -rf "$STAND/kopii/перед-выкатом"
conf "$BAZY_CONF
STACK_MIGRATE_CMD=\"echo migracii >> $STAND/poryadok; false\"
BACKUP_DBS=\"$SPISOK\""
: > "$STAND/docker.log"; : > "$STAND/poryadok"
svezhee_da
sleep 1
run_copy "$SCRIPTS_COPY"
PODSKAZKA=$(printf '%s\n' "$VYVOD" | grep 'восстановление руками' || true)
itog "1" "$RC" "упавшие миграции — код 1"
itog "нет" "$(has "$PODSKAZKA" "--clean")" "подсказка не зовёт pg_restore --clean на каталог"
itog "да" "$(has "$PODSKAZKA" "db-alfa.dump")" "подсказка называет дамп каждой базы (alfa)"
itog "да" "$(has "$PODSKAZKA" "db-beta.dump")" "подсказка называет дамп каждой базы (beta)"

echo "── 16ж. BACKUP_DBS только в окружении — дверь его не берёт: дампов баз нет ──"
# Ключ в файлах стенда дверь снимает и без своего списка; судит список только ключ,
# пришедший окружением: дверь исполняет необратимое по паспорту, а не по среде.
rm -rf "$STAND/kopii/перед-выкатом"
conf "$BAZY_CONF"
BACKUP_DBS="$SPISOK" run_deploy --план
itog "да" "$(printf '%s\n' "$VYVOD" | grep 'дамп базы' | grep -q 'без дампа' && echo да || echo нет)" \
     "план по BACKUP_DBS окружения называет «без дампа», а не --базы-в"
: > "$STAND/docker.log"; : > "$STAND/poryadok"
svezhee_da
sleep 1
BACKUP_DBS="$SPISOK" run_copy "$SCRIPTS_COPY"
itog "0" "$RC" "выкат без дампов баз выходит нулём"
itog "sborka migracii peresozdanie" "$(order)" "BACKUP_DBS окружения не дал дампов: сборка → миграции → пересоздание"
itog "" "$(dampy_vykata)" "в перед-выкатом нет дампов баз"

# 18 стоит последним: здоровый выкат ставит тег в продукт, а 4б судит, что тегов в продукте нет.
echo "── 18. БОЛЬНОЙ СЛУЧАЙ: выкат продукта не трогает харнес — ни дерева, ни HEAD, ни ворот харнеса ──"
# Спека §5.4 Г3–Г5: выкат продукта звал ворота харнеса (gates.sh --хакатон по карте
# харнеса), сверку карты devmap-selfheal.sh, которая правит и коммитит dev-map.yaml
# харнеса, и сведение автопамяти агента. Копия скриптов своя: в ней все три лежат
# заглушками, которые оставляют след, — режим хакатона «включён», сверка карты
# коммитит в харнес стенда. Выкат обязан пройти, не позвав ни одну.
KOPIYA_GRANICA="$STAND/scripts-granica"
mkdir -p "$KOPIYA_GRANICA"
cp "$HERE/deploy.sh" "$HERE/check-secrets.sh" "$KOPIYA_GRANICA/"
cp -r "$HERE/lib" "$KOPIYA_GRANICA/"
obertka_vorot "$KOPIYA_GRANICA/deploy_guard.py" "$HERE/deploy_guard.py" "$STAND/install.conf" "$STAND/harness.conf"
printf 'import sys\nsys.exit(0)\n' > "$KOPIYA_GRANICA/hackathon.py"
printf '#!/usr/bin/env bash\necho "gates.sh харнеса: $*"\n' > "$KOPIYA_GRANICA/gates.sh"
printf 'print("fix-claude-mem-project: свёл автопамять")\n' > "$KOPIYA_GRANICA/fix-claude-mem-project.py"
cat > "$KOPIYA_GRANICA/devmap-selfheal.sh" <<SH
#!/usr/bin/env bash
echo "devmap-selfheal: карта харнеса сверена"
printf 'сверка выкатом\n' >> "$HARNESS/dev-map.yaml"
git -C "$HARNESS" add dev-map.yaml
git -C "$HARNESS" -c user.email=proba@stend -c user.name=proba commit -qm "devmap-selfheal из выката"
printf 'след\n' > "$HARNESS/selfheal-sled"
SH
chmod +x "$KOPIYA_GRANICA/devmap-selfheal.sh" "$KOPIYA_GRANICA/gates.sh"
conf "$ODNO_DA_CONF"
: > "$STAND/docker.log"; : > "$STAND/poryadok"
svezhee_da
sleep 1
STATUS_DO=$(git -C "$HARNESS" status --porcelain)
HEAD_DO=$(git -C "$HARNESS" rev-parse HEAD)
TEGOV_HARNESSA_DO=$(git -C "$HARNESS" tag -l 'deploy-*' | grep -c . || true)
ZHURNAL_DO=$(grep -c '' "$STAND/zhurnal/deploy.log" 2>/dev/null || echo 0)
run_copy "$KOPIYA_GRANICA"
ZHURNAL_NOVOE=$(tail -n +"$((ZHURNAL_DO + 1))" "$STAND/zhurnal/deploy.log" 2>/dev/null)
itog "0" "$RC" "18 выкат продукта выходит нулём"
itog "да" "$(has "$VYVOD" "deploy ok")" "18 успех объявлен"
itog "$HEAD_DO" "$(git -C "$HARNESS" rev-parse HEAD)" "18 HEAD харнеса до и после выката равен"
itog "$STATUS_DO" "$(git -C "$HARNESS" status --porcelain)" "18 status --porcelain харнеса до и после выката равен"
itog "$TEGOV_HARNESSA_DO" "$(git -C "$HARNESS" tag -l 'deploy-*' | grep -c . || true)" "18 тегов выката в харнесе не прибавилось"
for slovo in gates.sh devmap fix-claude-mem; do
    itog "нет" "$(has "$VYVOD" "$slovo")" "18 в выводе выката нет «$slovo»"
    itog "нет" "$(has "$ZHURNAL_NOVOE" "$slovo")" "18 в deploy.log этого выката нет «$slovo»"
done

printf '\nВЫКАТ-ПРОДУКТА: путей %d, неудач %d\n' "$paths" "$fails"
[ "$fails" -eq 0 ]
