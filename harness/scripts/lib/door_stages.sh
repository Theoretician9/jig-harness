#!/usr/bin/env bash
#
# door_stages.sh — этапы выката продукта функциями. Подключается из deploy.sh
# (`source`) сразу после `say`; сам ничего не выполняет, только определяет функции.
#
# Что читает (задано deploy.sh до подключения этапов или к моменту вызова):
#   say, harness_event (lib/events.sh), DEPLOY_LOG, LOG_DIR, HARNESS_LOG_DIR, SCRIPT_DIR,
#   PRODUCT_TREE, DEPLOY_SELF, DEPLOY_SHA, TAG, RELEASE_OK, ключи STACK_* (в том числе
#   STACK_REMOTE, STACK_BRANCH), HARNESS_CONF, TREE_DIRTY и BACKUP_DIR, BACKUP_DBS, DB_ADMIN_USER, DEPLOY_HEALTH_WAIT_SEC.
# Что ставит ГЛОБАЛЬНО (не local — по ним идут следующие этапы и итоговая строка):
#   TAG (tag_and_release), ROLLBACK_READY (snapshot_images), NOCACHE (need_nocache),
#   DB_DUMP (dump_before_migrations; его видит name_db_restore), IMAGE_SHA12 (stage_build;
#   его видит promote_images), IMAGE_IDS (stage_build: id собранных образов, их пишет
#   record_result), SUBJ_TIP и SUBJ_MAIN (door_subject; SUBJ_MAIN видят door_ff_ok,
#   door_unmoved и door_push), TREE_ORIG (door_subject: куда вернуть дерево продукта),
#   RESULT_DONE (record_result: итог выката записан один раз), RESULT_ARMED (tag_and_release),
#   IMAGES_PROMOTED (promote_images ставит, restore_images снимает; по нему door_on_exit откатывает),
#   STAGE_US и STAGE_MARK (stage_done: время этапов S X B T D G K R P M), RANGE_BASE (secrets_base),
#   SUITES_NOTE (stage_tests: «N из M»).
# Окружение между вызовами: DEPLOY_UNDER_SG=1 ставит ensure_docker перед перезапуском
# под `sg docker` и тем отсекает второй перезапуск. Ключи скрипта (`"$@"`) deploy.sh
# отдаёт в ensure_docker доводами: перезапуск зовёт тот же скрипт с теми же ключами.
# DUMP_MIN — порог «дамп состоялся» (snapshot_db), байт.
#
# Отказ этапа — явный `exit 1`, а не return: вызов `if этап; then` отключил бы
# `set -e` внутри функции, и отказ прошёл бы молча. Исключение — этапы, чей отказ
# решает вызывающий (restore_images, door_unmoved, door_push): они возвращают код. Каталог — каталог
# продукта (deploy.sh делает `cd "$PRODUCT_TREE"` до первого этапа): git без -C судит его.
#
# Порядок вызовов задаёт deploy.sh и менять его нельзя; ворота, загрузка ключей и
# сверка секретов остаются в deploy.sh дословно — их судят по тексту того файла.

# Тестовый этап (door_checks_cached, door_checks_noted) живёт в соседней библиотеке.
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/door_checks.sh"

# Порог «дамп состоялся» замерен, а не придуман (02.09.2026): pg_dump -Fc прода —
# 38 363 428 байт, только схема стенда — 92 657, дамп без единой таблицы — 0.
# 1024 отделяет ноль и обрывок от любого настоящего дампа с запасом в 90 раз.
DUMP_MIN=1024

# Разбор вывода сканера (этап D/I); путь переопределяется пробой — копия модуля для мутации.
SCAN_VERDICT_PY="${SCAN_VERDICT_PY:-$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/scan_verdict.py}"

snapshot_db() {
    local dst="$1"
    if [ -z "${STACK_DB_DUMP_CMD:-}" ]; then
        say "STACK_DB_DUMP_CMD пуст — дампа базы нет; заполни в harness.conf (И-1)"
        return 2
    fi
    # Дамп несёт данные покупателей: читать его вправе только владелец файла.
    if ! (umask 077; mkdir -p "$(dirname "$dst")" && bash -c "$STACK_DB_DUMP_CMD" > "$dst" 2>>"$DEPLOY_LOG"); then
        say "команда дампа упала — файл $dst негоден"
        return 1
    fi
    local size
    size=$(stat -c %s "$dst" 2>/dev/null || echo 0)
    if [ "$size" -lt "$DUMP_MIN" ]; then
        say "дамп мал: $size байт при пороге $DUMP_MIN — считаю несостоявшимся"
        return 1
    fi
    say "дамп базы снят: $dst ($size байт)"
    return 0
}

# Ключи передаются в одинарных кавычках POSIX: sg исполняет строку через
# /bin/sh (dash), а `printf %q` в локали C даёт $'\320…', которого dash не знает.
sh_quote() { printf "'%s' " "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# Docker доступен? Снимок и откат зовут docker сами. Сессия, начатая до вступления
# пользователя в группу docker, сокета не видит, а `docker image inspect` без доступа
# неотличим от «образа нет»: выкат шёл бы дальше со словами «первый выкат —
# откатываться не на что». Член группы по /etc/group перезапускается под
# `sg docker` — один раз (DEPLOY_UNDER_SG), иначе отказ словами до ворот.
# Ключи скрипта ($@) нужны перезапуску и тексту отказа: передаются доводами.
ensure_docker() {
    if [ -n "${STACK_IMAGES:-}${STACK_CHECKS_FILE:-}" ] && ! docker version >/dev/null 2>&1; then
        if [ -z "${DEPLOY_UNDER_SG:-}" ] && command -v sg >/dev/null 2>&1 \
           && getent group docker | cut -d: -f4 | tr ',' '\n' | grep -qx "$(id -un)"; then
            say "docker не виден этой сессии, а в группе docker пользователь есть — перезапуск под sg docker"
            export DEPLOY_UNDER_SG=1
            local sg_cmd="" arg
            for arg in bash "$DEPLOY_SELF" "$@"; do sg_cmd="$sg_cmd$(sh_quote "$arg")"; done
            exec sg docker -c "$sg_cmd"
        fi
        say "ОШИБКА: docker недоступен ($(docker version 2>&1 | tail -1 | cut -c1-160)) — снимка, отката и тестов не будет, выкат отменён до ворот; ключи: $*"
        exit 1
    fi
}

# Свой лок ДО ворот, атомарно через flock (К-3). Раньше лок писался ПОСЛЕ ворот
# простым printf — окно «проверил→создал» пропускало два одновременных deploy.sh
# (улика 05.08: контейнеры под временными именами, API лёг на минуту). flock атомарен:
# второй выкат упирается сюда, не в гонку. Ворота этот лок видят, но не блокируются
# им: deploy_guard пропускает лок, который держит его собственный предок.
# `exec 8>>` без команды меняет дескрипторы самой оболочки: fd 8 живёт после выхода
# из функции до конца скрипта, и flock снимается сам с его закрытием.
take_deploy_lock() {
    local lock="$LOG_DIR/locks/deploy.lock" holder
    # Открытие с >>, не >: усечение при открытии стёрло бы запись о держателе
    # раньше, чем flock скажет «занято» (та же грабля, что в tg-dispatcher.sh).
    exec 8>> "$lock"
    if ! flock -n 8; then
        holder=$(sed -n 2p "$lock" 2>/dev/null || true)
        say "выкат уже идёт (${holder:-держатель не назвался}) — второй одновременный отменён"
        exit 1
    fi
    printf '%s\nвыкат deploy.sh, начат %s\n' "$$" "$(date -Is)" > "$lock"
    # Лок-файл НЕ удаляется (П-12): flock снимается сам с закрытием fd на выходе,
    # а rm в trap открывал микро-гонку — второй выкат, успевший открыть файл до
    # удаления, держал бы flock на снесённом inode, и третий выкат прошёл бы мимо
    # него по новому файлу. Пустой файл в locks/ дешевле этой гонки.
}

# Замок дерева продукта: от выбора предмета (door_subject переключает дерево) до конца прохода.
# Отдельный файл от deploy.lock: --проверка deploy.lock не берёт, но дерево переключает так же, и
# второй вызов с --ветка сдвинул бы HEAD под идущей сборкой первого. fd 7 живёт до конца скрипта,
# flock снимается с его закрытием; шаги прода fd 7 не наследуют (door_run_limited).
take_tree_lock() {
    local lock="$LOG_DIR/locks/product-tree.lock" holder
    exec 7>> "$lock"
    if ! flock -n 7; then
        holder=$(sed -n 2p "$lock" 2>/dev/null || true)
        say "дерево продукта занято идущим проходом (${holder:-держатель не назвался}) — второй отменён до переключения дерева"
        exit 1
    fi
    printf '%s\nпроход deploy.sh, начат %s\n' "$$" "$(date -Is)" > "$lock"
}

tree_head() { GIT_NO_REPLACE_OBJECTS=1 git -C "$PRODUCT_TREE" rev-parse HEAD 2>/dev/null || true; }

# Вершина ветки $1 в STACK_REMOTE: пусто — ветки там нет; код 1 — хранилище не ответило.
remote_tip() {
    local out
    out=$(git ls-remote "$STACK_REMOTE" "refs/heads/$1") || return 1
    printf '%s\n' "${out%%[[:space:]]*}"
}

# Предмет выката из хранилища продукта: вершина ветки $1 в STACK_REMOTE, а не то, что
# лежит в дереве. STACK_REMOTE пуст → предмет — HEAD дерева, как раньше. Ставит SUBJ_MAIN —
# вершину STACK_BRANCH на момент выбора предмета: door_ff_ok, door_unmoved и door_push судят
# по ней, что выкат не переписывает чужое. Ветки STACK_BRANCH в хранилище нет — отказ: пуш
# создал бы её молча. Без $2 переключает дерево (checkout --detach; чистоту дерева судит
# deploy.sh до этого) и помнит, куда вернуть его при отказе (TREE_ORIG); с $2 = план читает
# хранилище одним ls-remote: ни fetch, ни перемены дерева.
door_subject() {  # $1 = ветка|пусто, $2 = план|пусто
    local branch="$1" tip
    SUBJ_MAIN=""
    SUBJ_TIP=""
    if [ -z "${STACK_REMOTE:-}" ]; then
        [ -z "$branch" ] || { say "ОШИБКА: --ветка $branch требует STACK_REMOTE (хранилище продукта) в $HARNESS_CONF — выкат отменён до ворот"; exit 1; }
        return 0
    fi
    # Одно дерево: службы прода смотрят в клон двери, и checkout ветки отдал бы им её файлы до «Да».
    [ -z "$branch" ] || separate_prod_tree \
        || { say "ОШИБКА: задай PRODUCT_PATH: --ветка переключила бы дерево прода до «Да» — выкат отменён до ворот"; exit 1; }
    SUBJ_MAIN=$(remote_tip "${STACK_BRANCH:-main}") \
        || { say "ОШИБКА: хранилище $STACK_REMOTE не ответило — выкат отменён до ворот"; exit 1; }
    if [ "${2:-}" != план ]; then
        [ -n "$SUBJ_MAIN" ] || { say "ОШИБКА: ветки ${STACK_BRANCH:-main} (STACK_BRANCH) нет в $STACK_REMOTE — пуш создал бы её молча, выкат отменён до ворот"; exit 1; }
        git fetch -q "$STACK_REMOTE" "refs/heads/${STACK_BRANCH:-main}" 2>/dev/null || true
    fi
    [ -n "$branch" ] || return 0
    tip=$(remote_tip "$branch") \
        || { say "ОШИБКА: хранилище $STACK_REMOTE не ответило — выкат отменён до ворот"; exit 1; }
    [ -n "$tip" ] || { say "ОШИБКА: ветки $branch нет в $STACK_REMOTE — выкат отменён до ворот"; exit 1; }
    SUBJ_TIP="$tip"
    [ "${2:-}" != план ] || return 0
    git fetch -q "$STACK_REMOTE" "refs/heads/$branch" \
        || { say "ОШИБКА: ветка $branch не получена из $STACK_REMOTE — выкат отменён до ворот"; exit 1; }
    TREE_ORIG=$(git symbolic-ref -q --short HEAD || git rev-parse HEAD)
    git checkout -q --detach "$tip" \
        || { say "ОШИБКА: дерево $PRODUCT_TREE не переключено на ${tip:0:8} ($branch) — выкат отменён до ворот"; exit 1; }
}

# Предмет — fast-forward над веткой хранилища (§5.3 п. 4): иначе на проде оказался бы код, которого
# нет в ветке, и следующий выкат ветки молча снял бы его. Отказ до ворот: «Да» не тратится.
door_ff_ok() {
    [ -n "${STACK_REMOTE:-}" ] && [ -n "$DEPLOY_SHA" ] || return 0
    [ "$DEPLOY_SHA" != "$SUBJ_MAIN" ] || return 0
    if ! git merge-base --is-ancestor "$SUBJ_MAIN" "$DEPLOY_SHA" 2>/dev/null; then
        say "ОШИБКА: ${DEPLOY_SHA:0:8} не fast-forward над $STACK_REMOTE/${STACK_BRANCH:-main} (${SUBJ_MAIN:0:8}) — выкат отменён до ворот, ни тега, ни пуша"
        exit 1
    fi
}

# Отказ или проверка не оставляют дерево на чужой ветке: следующий выкат HEAD покатил бы её.
# Выкат, дошедший до конца, дерево оставляет: прод собран и поднят из него.
restore_tree() {  # $1 = код выхода
    [ -n "${TREE_ORIG:-}" ] || return 0
    [ "$CHECK_ONLY" = 1 ] || [ "$1" != 0 ] || return 0
    git -C "$PRODUCT_TREE" checkout -q "$TREE_ORIG" 2>/dev/null \
        || say "ВНИМАНИЕ: дерево $PRODUCT_TREE не возвращено на $TREE_ORIG — верни руками"
}

# Между выбором предмета и пушем: хранилище ушло вперёд или HEAD сдвинулся — пуш не идёт.
# Код 1 — состояние «отказ после дампа, до пуша»: дамп остаётся, тега в хранилище нет.
door_unmoved() {
    [ -n "${STACK_REMOTE:-}" ] || return 0
    local now_main now_head
    now_main=$(remote_tip "${STACK_BRANCH:-main}" || echo "не ответило")
    now_head=$(tree_head)
    if [ "$now_main" != "$SUBJ_MAIN" ] || [ "$now_head" != "$DEPLOY_SHA" ]; then
        say "ОШИБКА: до пуша сдвинулось (${STACK_BRANCH:-main} ${SUBJ_MAIN:0:8} → ${now_main:0:8}, HEAD ${DEPLOY_SHA:0:8} → ${now_head:0:8}) — тега в хранилище нет, дамп остаётся: ${DB_DUMP:-<дампа нет>}"
        return 1
    fi
}

# Пуш в хранилище продукта — необратим для чужого репозитория, поэтому только здесь: после
# ворот, тега и дампа. Тег — всегда; ветка — без force (fast-forward над STACK_BRANCH судит
# door_ff_ok до ворот): дверь не переписывает чужую историю. Код 1 — пуш отказан, дальше в прод не идём.
door_push() {
    [ -n "${STACK_REMOTE:-}" ] && [ -n "$DEPLOY_SHA" ] || return 0
    local out branch="${STACK_BRANCH:-main}"
    if ! out=$(git push "$STACK_REMOTE" "refs/tags/$TAG" 2>&1); then
        printf '%s\n' "$out" | tee -a "$DEPLOY_LOG"
        say "ОШИБКА: хранилище $STACK_REMOTE отказало в теге $TAG — тег стоит локально на ${DEPLOY_SHA:0:8}, в хранилище его нет; дамп остаётся: ${DB_DUMP:-<дампа нет>}; ветка не отправлена, миграции и службы не тронуты"
        return 1
    fi
    printf '%s\n' "$out" >> "$DEPLOY_LOG"
    say "тег $TAG отправлен в $STACK_REMOTE"
    [ "$DEPLOY_SHA" != "$SUBJ_MAIN" ] || { say "ветка $branch в $STACK_REMOTE уже на ${DEPLOY_SHA:0:8}"; return 0; }
    if ! out=$(git push "$STACK_REMOTE" "$DEPLOY_SHA:refs/heads/$branch" 2>&1); then
        printf '%s\n' "$out" | tee -a "$DEPLOY_LOG"
        say "ОШИБКА: хранилище $STACK_REMOTE не приняло ветку $branch (тег $TAG в нём уже есть); дамп остаётся: ${DB_DUMP:-<дампа нет>}; миграции и службы не тронуты"
        return 1
    fi
    printf '%s\n' "$out" >> "$DEPLOY_LOG"
    say "ветка $branch в $STACK_REMOTE продвинута до ${DEPLOY_SHA:0:8} (fast-forward)"
}

# Тег выката: каждая выкатка адресуема. Тег обязателен и ставится на DEPLOY_SHA явно,
# а не на HEAD. Рядом пишется журнал выкатов: по нему гасится кнопка «Да» (тег сессия
# удалила бы одной командой, журнал охраняет сторож). Не встал тег или запись — выкат
# отменён до продвижения в прод, ловушка вернёт текстовое «да». Имя тега даёт name_release_tag
# раньше: дамп базы лежит в каталоге с этим именем и снимается до тега.
name_release_tag() { TAG="deploy-$(date +%Y%m%d-%H%M%S)"; }

tag_and_release() {
    if [ -z "$DEPLOY_SHA" ]; then
        say "репозиторий не инициализирован — тег пропущен"
        return 0
    fi
    git -C "$PRODUCT_TREE" -c user.name=harness-deploy -c user.email=deploy@localhost \
        tag -a "$TAG" -m "выкат $TAG" "$DEPLOY_SHA" \
        || { say "ОШИБКА: тег $TAG не поставлен — выкат отменён до продвижения в прод"; exit 1; }
    local release_log="${HARNESS_LOG_DIR:-$LOG_DIR}/deploy-releases.jsonl"
    HARNESS_TASK=- HARNESS_LOG_DIR="${HARNESS_LOG_DIR:-$LOG_DIR}" harness_event deploy-releases scripts/deploy.sh \
        deploy.release ok "цель=$DEPLOY_SHA $(readlink -f "$PRODUCT_TREE")" "тег=$TAG"
    # harness_event всегда код 0: что запись легла, видно только по самой строке
    tail -n 1 "$release_log" 2>/dev/null | grep -qF "\"$TAG\"" \
        || { say "ОШИБКА: запись в журнал выкатов не сделана — выкат отменён до продвижения в прод"; exit 1; }
    say "тег: $TAG"
    # С этой минуты у выката есть адрес, и любой выход без записанного итога — провал (door_on_exit).
    RESULT_ARMED=1
}

# Итог выката — строка deploy-results.jsonl: по ней откат находит последний ЖИВОЙ выкат.
# Живой итог начинается со слова «выкачено»; провал и авария — своими словами, иначе
# откат поднял бы образ, которого на проде живым не было. sha пишется 12 знаками — как тег
# образа :<sha12>. Образы выката пишутся id: тег :<sha12> повторная сборка того же sha перезаписывает, id — нет.
# Ключ sha256 писатель оставляет нетронутым (хэш, не секрет), поэтому id идут им, списком в
# порядке образов, а имена образов — рядом.
RESULT_DONE=0
RESULT_ARMED=0
IMAGES_PROMOTED=0   # :latest указывает на образы выката, а откат ещё не брался
SERVICES_RELEASED=0 # пересоздание служб начиналось, при обрыве им нужен подъём отката; ставит release_services
record_result() {  # $1 = ok|error, $2 = итог словами
    local images=()
    # Итог посреди этапа (оговорка прокси, авария отката, сигнал): время с прошлой отметки — этому
    # этапу, иначе оно не достаётся никому и сумма этапов врёт о стене.
    [ -n "${STAGE_US[M]:-}" ] || stage_done "$(stage_after "$STAGE_LAST")"
    RESULT_DONE=1
    [ -z "${IMAGE_IDS:-}" ] || images=("образы=$STACK_IMAGES" --json "$(image_ids_json)")
    HARNESS_TASK=- HARNESS_LOG_DIR="${HARNESS_LOG_DIR:-$LOG_DIR}" harness_event deploy-results scripts/deploy.sh \
        deploy.result "$1" "тег=$TAG" "sha=${DEPLOY_SHA:0:12}" "итог=$2" ${images[@]+"${images[@]}"} \
        --json "$(result_notes_json)"
}

# Заметки итога (N): длительности этапов, коммиты диапазона, наборы. Пишутся и в провал: по ним
# видно, на каком этапе ушло время. Этап, до которого выкат не дошёл, — 0.
STAGE_NAMES="S X B T D G K R P M"
declare -A STAGE_US=()
STAGE_MARK=${EPOCHREALTIME/[.,]/}   # отсчёт идёт от подключения библиотеки: конфиг до неё грузится быстро
SUITES_NOTE="0 из 0"

# Время с прошлой отметки (или со старта) уходит этапу $1: сумма этапов равна стене прохода,
# а шаги между этапами (проверка HEAD, замки) достаются ближайшему следующему.
STAGE_LAST=""   # последний закрытый этап: итог, записанный посреди этапа, закрывает следующий
stage_done() {  # $1 = буква этапа
    local now=${EPOCHREALTIME/[.,]/}
    STAGE_US[$1]=$(( ${STAGE_US[$1]:-0} + now - STAGE_MARK ))
    STAGE_MARK=$now
    STAGE_LAST=$1
}

stage_after() {  # $1 = буква этапа или пусто → следующая буква (после M — M)
    local name prev=""
    for name in $STAGE_NAMES; do
        [ "$prev" != "$1" ] || { printf '%s' "$name"; return 0; }
        prev=$name
    done
    printf 'M'
}

stage_seconds() {  # $1 = буква → «секунды.сотые»
    local us=${STAGE_US[$1]:-0}
    printf '%d.%02d' $((us / 1000000)) $((us % 1000000 / 10000))
}

stages_json() {
    local name json=""
    for name in $STAGE_NAMES; do json="$json\"$name\":$(stage_seconds "$name"),"; done
    printf '{%s}' "${json%,}"
}

stages_words() {
    local name words=""
    for name in $STAGE_NAMES; do words="$words${words:+, }$name $(stage_seconds "$name") с"; done
    printf '%s' "$words"
}

# Коммиты «<sha12> <тема>» диапазона «живой выкат..sha»; диапазона нет — история до sha. Потолок
# строк: первый выкат на длинной истории не должен раздувать запись журнала.
commits_json() {
    [ -n "${DEPLOY_SHA:-}" ] || { printf '[]'; return 0; }
    git -C "$PRODUCT_TREE" log --max-count=200 --abbrev=12 --format='%h %s' \
        "${RANGE_BASE:+$RANGE_BASE..}$DEPLOY_SHA" 2>/dev/null | jq -Rcn '[inputs]'
}

result_notes_json() {
    jq -cn --argjson stages "$(stages_json)" --argjson commits "$(commits_json)" --arg suites "$SUITES_NOTE" \
        '{"этапы": $stages, "коммиты": $commits, "наборы": $suites}'
}

image_ids_json() {
    local id json=""
    for id in $IMAGE_IDS; do json="$json\"${id#sha256:}\","; done
    printf '{"sha256":[%s]}' "${json%,}"
}

# Выход скрипта: выкат оборвался раньше, чем сказал итог сам (упала сборка, дамп, миграции) —
# итог пишется провалом; дерево продукта возвращается туда, где стояло до предмета. Шаг, шедший
# в момент сигнала, снимается: служба-потомок не должна пережить дверь и её итог.
door_on_exit() {
    local rc=$?
    if [ -n "$DOOR_STEP_PID" ]; then
        kill -TERM "$DOOR_STEP_PID" 2>/dev/null || true
        wait "$DOOR_STEP_PID" 2>/dev/null || true
        rm -f "$DOOR_STEP_OUT"
    fi
    if [ "$RESULT_DONE" = 0 ] && [ "$IMAGES_PROMOTED" = 1 ]; then
        restore_images || true
        [ "$SERVICES_RELEASED" = 0 ] || raise_restored_services
        name_db_restore
    fi
    if [ "$RESULT_ARMED" = 1 ] && [ "$RESULT_DONE" = 0 ]; then
        record_result error "ВЫКАТ ПРОВАЛЕН: остановлен кодом $rc до конца (причина — в $DEPLOY_LOG)"
    fi
    restore_tree "$rc"
    return 0
}

# Обрыв после пересоздания служб: :latest уже возвращён, а службы стоят на образах выката. Подъём
# отката ставит их на возвращённое; нет команды или она упала — сказать, где что стоит.
raise_restored_services() {
    if [ -n "${STACK_UP_CMD:-}" ] && door_step rollback "$STACK_UP_CMD"; then
        say "подъём отката после обрыва: службы подняты на ${ROLLED_TO:-прошлом :latest}"
        return 0
    fi
    [ -z "${STACK_UP_CMD:-}" ] || door_show_out
    say "ВНИМАНИЕ: службы на :$IMAGE_SHA12, :latest на ${ROLLED_TO:-<нечего вернуть>} — подними STACK_UP_CMD руками"
}

# Последний живой выкат из журнала итогов: «<тег> <sha12> <адрес образа>…» — по адресу на каждый
# образ STACK_IMAGES (id из строки итога, «-» — у строки, писанной до id). Пусто — живых итогов нет.
last_live_release() {
    python3 -I - "${HARNESS_LOG_DIR:-$LOG_DIR}/deploy-results.jsonl" "${STACK_IMAGES:-}" <<'PY'
import json, sys
last = ""
wanted = sys.argv[2].split()
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        for line in fh:
            try:
                data = json.loads(line).get("data")
            except ValueError:
                continue
            if not isinstance(data, dict) or not str(data.get("итог", "")).startswith("выкачено"):
                continue
            if data.get("тег") and len(str(data.get("sha", ""))) >= 12:
                ids = dict(zip(str(data.get("образы", "")).split(), data.get("sha256") or []))
                refs = " ".join(("sha256:" + ids[name]) if name in ids else "-" for name in wanted)
                last = f'{data["тег"]} {str(data["sha"])[:12]} {refs}'
except OSError:
    pass
print(last)
PY
}

# Этап X: от какого коммита судить секреты. Базой служит sha прошлого ЖИВОГО выката (его уже
# судили тогда), а не последний тег deploy-*: тег ставится и проваленному выкату, а main может
# нести непроверенное. Ставит RANGE_BASE; пусто — живого выката нет или его sha не предок
# выкатываемого (force-push продукта): диапазон не построить, судится весь репозиторий.
secrets_base() {
    local base
    base=$(last_live_release | cut -d' ' -f2)
    RANGE_BASE=""
    [ -n "$base" ] && [ -n "${DEPLOY_SHA:-}" ] || return 0
    if git -C "$PRODUCT_TREE" merge-base --is-ancestor "$base" "$DEPLOY_SHA" 2>/dev/null; then
        RANGE_BASE=$base
    fi
}

# Снимок образов: точка отката ДО сборки. STACK_IMAGES — имена образов через пробел
# (данные). Пусто — отката не будет, и это говорится вслух, а не выясняется в момент аварии.
snapshot_images() {
    local img
    ROLLBACK_READY=0
    if [ -n "${STACK_IMAGES:-}" ]; then
        for img in $STACK_IMAGES; do
            if docker image inspect "$img:latest" >/dev/null 2>&1; then
                docker tag "$img:latest" "$img:previous"
                ROLLBACK_READY=1
            fi
        done
        [ "$ROLLBACK_READY" = 1 ] && say "снимок: образы помечены :previous" \
            || say "снимок: образов :latest ещё нет (первый выкат) — откатываться не на что"
    else
        say "STACK_IMAGES пуст — снимка для отката нет; заполни при первом продукте"
    fi
}

# Смена манифестов зависимостей → --no-cache. CLAUDE.md §9: после изменения
# зависимостей — только --no-cache. Сравниваем манифесты с прошлым тегом выката; нет
# прошлого тега — считаем смену была (ошибиться в сторону --no-cache дешевле, чем
# собрать со старым кэшем). Ставит NOCACHE.
need_nocache() {
    local manifests="${STACK_MANIFESTS:-requirements.txt package.json package-lock.json Dockerfile docker-compose.yml}"
    local prev_tag m
    NOCACHE=0
    if git rev-parse --git-dir >/dev/null 2>&1; then
        # -refname, не -creatordate: у lightweight-тегов creatordate — дата КОММИТА,
        # не выката, и порядок врал; имена deploy-ГГГГММДД-ЧЧММСС датированы —
        # лексикографика надёжна (К-3). Сборка идёт до тега этого выката: первая строка — прошлый.
        prev_tag=$(git tag -l 'deploy-*' --sort=-refname | sed -n 1p)
        if [ -z "$prev_tag" ]; then
            NOCACHE=1
            say "прошлого тега выката нет — сборка с --no-cache (первый выкат)"
        else
            for m in $manifests; do
                if ! git diff --quiet "$prev_tag" HEAD -- "*$m" 2>/dev/null; then
                    NOCACHE=1
                    say "манифест зависимостей изменился с $prev_tag ($m) — сборка с --no-cache"
                    break
                fi
            done
        fi
    else
        NOCACHE=1
    fi
}

# Образ выката адресуется sha12: сборка, тесты, продвижение в :latest говорят об одном образе.
# Ставит IMAGE_SHA12 (пуст, когда у выката нет sha). IMAGE_TAG видит только команда сборки
# (door_build): остальные шаги, откат и подъём откатанного стека его не видят — IMAGE_TAG
# проваленного выката поднял бы в откате ту самую сборку, от которой откат уходит.
set_image_sha12() {
    IMAGE_SHA12=""
    [ -z "${DEPLOY_SHA:-}" ] || IMAGE_SHA12="${DEPLOY_SHA:0:12}"
}

image_id() { docker image inspect --format '{{.Id}}' "$1" 2>/dev/null || true; }

# Что лежало под :<sha12> и :latest до сборки: повторный выкат того же sha находит свой :<sha12>
# уже занятым, и только сравнение id скажет, положила ли сборка свежий образ под этот тег.
declare -A PREBUILD_TAGGED=() PREBUILD_LATEST=()
remember_prebuild() {
    local img
    for img in ${STACK_IMAGES:-}; do
        PREBUILD_TAGGED[$img]=$(image_id "$img:$IMAGE_SHA12")
        PREBUILD_LATEST[$img]=$(image_id "$img:latest")
    done
}

# Одна сборка на выкат. IMAGE_TAG=<sha12> уходит в команду сборки: compose продукта кладёт
# образ под этим тегом, и тот же образ потом проверяется тестами, продвигается в :latest и
# служит адресом отката (promote_images). Сборка идёт ДО «Да» (§5.1): прод она не трогает —
# службы поднимает STACK_RELEASE_CMD после миграций (door_preflight отказывает иначе).
build_product() {
    if [ "$NOCACHE" = 1 ] && [ -n "${STACK_BUILD_NOCACHE_CMD:-}" ]; then
        say "сборка (--no-cache): $STACK_BUILD_NOCACHE_CMD"
        door_build "$STACK_BUILD_NOCACHE_CMD" || { say "сборка упала ($DOOR_WHY)"; door_show_out; exit 1; }
    else
        [ "$NOCACHE" = 1 ] && say "STACK_BUILD_NOCACHE_CMD пуст — иду обычной сборкой (заполни в harness.conf)"
        say "сборка: $STACK_BUILD_CMD"
        door_build "$STACK_BUILD_CMD" || { say "сборка упала ($DOOR_WHY)"; door_show_out; exit 1; }
    fi
}

# Сверка после сборки: под каждым $img:$sha12 из STACK_IMAGES — образ ЭТОЙ сборки. Compose без
# ${IMAGE_TAG} кладёт образ прямо в :latest — тогда он помечается :sha12 ретегом (без метки
# откату не на что вернуться), и это говорится вслух: такая сборка уже сдвинула :latest до «Да».
# Тег, не тронутый сборкой, и :latest, сдвинутый ею, — именно этот случай, даже если тег занят
# образом прошлой сборки того же sha. Нет ни того, ни другого — выкат отменён: службы не трогались.
label_built_images() {
    local img tagged latest
    [ -n "${STACK_IMAGES:-}" ] && [ -n "$IMAGE_SHA12" ] || return 0
    for img in $STACK_IMAGES; do
        tagged=$(image_id "$img:$IMAGE_SHA12")
        latest=$(image_id "$img:latest")
        if [ -n "$latest" ] && [ "$latest" != "${PREBUILD_LATEST[$img]}" ] \
           && [ "$tagged" = "${PREBUILD_TAGGED[$img]}" ]; then
            say "ВНИМАНИЕ: compose не знает IMAGE_TAG — сборка положила $img прямо в :latest, помечаю :$IMAGE_SHA12 ретегом; положи образ под \${IMAGE_TAG} в compose продукта"
            docker tag "$img:latest" "$img:$IMAGE_SHA12"
            restore_latest "$img"
        elif [ -z "$tagged" ]; then
            say "ОШИБКА: сборка не дала образа $img:$IMAGE_SHA12 (и :latest не сдвинут) — выкат отменён, службы не трогались"
            exit 1
        fi
    done
}

# До «Да» :latest прода стоит на том, что было до сборки: образ выката живёт под :<sha12>,
# а в :latest его ставит только promote_images. Прежнего :latest не было (первый выкат) — метка
# снимается: иначе снимок следующего выката счёл бы неодобренную сборку откатом. Образ держит :sha12.
restore_latest() {  # $1 = образ
    if [ -n "${PREBUILD_LATEST[$1]}" ]; then
        docker tag "${PREBUILD_LATEST[$1]}" "$1:latest"
    else
        docker image rm "$1:latest" >/dev/null
    fi
}

# id образов выката по порядку STACK_IMAGES: их пишет итог выката, по ним идёт откат.
capture_image_ids() {
    local img
    IMAGE_IDS=""
    [ -n "$IMAGE_SHA12" ] || return 0
    for img in ${STACK_IMAGES:-}; do IMAGE_IDS="$IMAGE_IDS $(image_id "$img:$IMAGE_SHA12")"; done
    IMAGE_IDS=${IMAGE_IDS# }
}

# Продвижение в прод: образ выката становится :latest — после «Да», дампа и тега, до миграций.
promote_images() {
    local img
    [ -n "${STACK_IMAGES:-}" ] && [ -n "$IMAGE_SHA12" ] || return 0
    IMAGES_PROMOTED=1
    for img in $STACK_IMAGES; do
        docker tag "$img:$IMAGE_SHA12" "$img:latest"
    done
    say "образы :$IMAGE_SHA12 продвинуты в :latest"
}

# backup.sh ищется от каталога самого выката, а не от PROJECT_DIR: проект держит его в
# harness/demons (скрипты — в scripts/), пакет — в demons рядом со scripts. Берётся первый
# найденный; не найден — путь проекта, и bash откажет словами «нет такого файла».
backup_script() {
    local c
    for c in "$SCRIPT_DIR/../harness/demons/backup.sh" "$SCRIPT_DIR/../demons/backup.sh"; do
        [ -f "$c" ] && { printf '%s\n' "$c"; return; }
    done
    printf '%s\n' "$SCRIPT_DIR/../harness/demons/backup.sh"
}

# Дамп базы ПЕРЕД миграциями — точка отката схемы (И-1). Ключ пуст = продукт без базы;
# это штатно и говорится вслух. Ключ задан, а дамп не снялся — выкат останавливается:
# миграция без точки отката необратима.
dump_before_migrations() {
    local db_count
    DB_DUMP=""
    if [ -n "${STACK_DB_DUMP_CMD:-}" ]; then
        DB_DUMP="${BACKUP_DIR:-/var/backups/harness}/перед-выкатом-$TAG/база.dump"
        if ! snapshot_db "$DB_DUMP"; then
            say "дамп базы не снят — выкат остановлен (И-1): миграции необратимы, откатывать схему было бы нечем"
            exit 1
        fi
    elif [ -n "${BACKUP_DBS:-}" ]; then
        # Баз несколько, а одна команда защищает одну: дампы всех снимает backup.sh
        # (`--базы-в`). Код 0 без дампов — тоже отказ: «ноль без файлов» это выкат без копии.
        DB_DUMP="${BACKUP_DIR:-/var/backups/harness}/перед-выкатом/${TAG#deploy-}-$(printf '%.8s' "${DEPLOY_SHA:-$(tree_head)}")"
        db_count=$(printf '%s\n' "$BACKUP_DBS" | grep -c '[^[:space:]]' || true)
        say "дампы всех баз: backup.sh --базы-в $DB_DUMP"
        bash "$(backup_script)" --базы-в "$DB_DUMP" >> "$DEPLOY_LOG" 2>&1 \
            && [ "$(find "$DB_DUMP" -maxdepth 1 -name 'db-*.dump' 2>/dev/null | wc -l)" -ge "$db_count" ] \
            || { say "дампы баз не сняты (журнал: $DEPLOY_LOG) — выкат остановлен (И-1): миграции необратимы, откатывать схему было бы нечем"; exit 1; }
        say "дампы баз сняты: $DB_DUMP"
    else
        say "STACK_DB_DUMP_CMD пуст — дампа базы перед миграциями нет; если у продукта есть база, заполни ключ в harness.conf"
    fi
}

# Адреса образов последнего живого выката — по одному на образ STACK_IMAGES: id из журнала
# итогов, а у строки, писанной до id, тег :<sha12>. Тег повторная сборка того же sha перезаписывает.
live_targets() {  # $1 = sha12, остальное — id по образам или «-»
    local sha12=$1 img ref
    shift
    for img in $STACK_IMAGES; do
        ref=${1:--}
        [ "$#" -eq 0 ] || shift
        [ "$ref" != - ] || ref="$img:$sha12"
        printf '%s\n' "$ref"
    done
}

# Откат образов и подсказка восстановления — общие для упавших миграций и
# красной живости. Восстановление базы НЕ выполняется: оно необратимо и
# решается владельцем; называются дамп и команда. ROLLED_TO пуст, когда вернуть нечего.
# Три дела по очереди: команда продукта, образ последнего живого выката, снимок :previous.
restore_images() {
    local live live_tag live12 live_refs
    IMAGES_PROMOTED=0
    live=$(last_live_release)
    read -r live_tag live12 live_refs <<<"$live"
    ROLLED_TO="${live_tag:-снимок :previous}"
    if [ -n "${STACK_ROLLBACK_CMD:-}" ]; then
        rollback_by_product_cmd
        return 0
    fi
    if [ -n "${STACK_IMAGES:-}" ] && [ -n "$live" ] && retag_live_release "$live_tag" "$live12" $live_refs; then
        return 0
    fi
    retag_previous "$live" "$live_tag" "$live12"
}

# Команда продукта откатывает ВМЕСТО ретега, не вместе с ним. Её код не судится: скрипты отката
# при мёртвой живости выходят нулём, ожил ли стек, решает health.
rollback_by_product_cmd() {
    say "откат командой STACK_ROLLBACK_CMD (ретег образов не делается): $STACK_ROLLBACK_CMD"
    door_step rollback "$STACK_ROLLBACK_CMD" || say "ВНИМАНИЕ: откат вернул ${DOOR_WHY%%:*} — не судится, решит health"
}

# Образы последнего живого выката → :latest, по id из журнала итогов. Код 1 — хотя бы одного
# образа нет: откат поднял бы часть стека, и решает снимок.
retag_live_release() {  # $1 = тег выката, $2 = sha12, остальное — адреса образов
    local live_tag=$1 live12=$2 i
    local -a imgs targets
    shift 2
    imgs=(${STACK_IMAGES:-})
    mapfile -t targets < <(live_targets "$live12" "$@")
    images_present "${targets[@]}" || return 1
    say "возвращаю образы последнего живого выката $live_tag (:$live12) → :latest"
    for i in "${!imgs[@]}"; do
        docker tag "${targets[i]}" "${imgs[i]}:latest"
    done
}

# Снимок :previous — это «что лежало в :latest перед сборкой», а не «что было выкачено»:
# на него идём, только когда живого выката с образом нет (первый выкат после обновления).
retag_previous() {  # $1 = строка последнего живого выката|пусто, $2 = его тег, $3 = его sha12
    local why="" img
    if [ -n "${STACK_IMAGES:-}" ]; then
        if [ -z "$1" ]; then
            why="живого выката в журнале итогов нет"
        else
            why="образа живого выката $2 (:$3) нет"
        fi
    fi
    if [ "$ROLLBACK_READY" != 1 ]; then
        say "${why:+$why, }образов :previous нет (снимок не делался) — возвращать нечего"
        ROLLED_TO=""
        return 1
    fi
    say "$why — откат на снимок: возвращаю образы :previous → :latest"
    for img in $STACK_IMAGES; do
        docker image inspect "$img:previous" >/dev/null 2>&1 && docker tag "$img:previous" "$img:latest"
    done
    return 0
}
# Все адреса образов (тег или id) на месте — иначе откат поднял бы часть стека.
images_present() {
    local ref
    for ref in "$@"; do
        docker image inspect "$ref" >/dev/null 2>&1 || return 1
    done
}
# Адреса образов выката по тегу :<sha12> — по одному на образ STACK_IMAGES.
tagged_refs() {
    local img
    for img in ${STACK_IMAGES:-}; do printf '%s:%s\n' "$img" "$IMAGE_SHA12"; done
}
# Выход до миграций, когда :latest уже указывает на новые образы: вернуть образы, назвать
# дамп и команду восстановления, выйти кодом 1. Отсутствие образа для отката выход не
# обрывает: подсказка восстановления базы нужна именно тогда (иначе set -e прячет её).
back_out_build() {
    restore_images || true
    [ -z "$ROLLED_TO" ] || say "ВЫКАТ ПРОВАЛЕН, стек на прошлой версии $ROLLED_TO"
    name_db_restore
    exit 1
}
name_db_restore() {
    [ -n "$DB_DUMP" ] || return 0
    say "схема базы НЕ откачена автоматически; дамп до миграций: $DB_DUMP"
    if [ -n "${STACK_DB_RESTORE_HINT:-}" ]; then
        say "восстановление руками (решение владельца): $STACK_DB_RESTORE_HINT"
    elif [ -d "$DB_DUMP" ]; then
        # Дампы всех баз (`--базы-в`): у каждой свой движок, одной команды на каталог нет.
        say "восстановление руками (решение владельца): каждую базу — её дампом, командой её движка (psql или mysql): $(cd "$DB_DUMP" && ls db-*.dump 2>/dev/null | tr '\n' ' ')"
    else
        say "восстановление руками (решение владельца): pg_restore -U ${DB_ADMIN_USER:-<DB_ADMIN_USER не задан>} -d ${DB_ADMIN_USER:-<DB_ADMIN_USER не задан>} --clean $DB_DUMP"
    fi
}

# Health циклом. Потолок — DEPLOY_HEALTH_WAIT_SEC (по умолчанию 60 с): попыток вдвое
# меньше, чем секунд, пауза между ними 2 с, а запрос длится до 5 с — реальное ожидание
# от потолка до ~3,5 потолка (К-3: раньше и комментарий, и сообщение врали про «60 с»).
# STACK_HEALTH_CMD (код 0 = живо) старше STACK_HEALTH_URL (200 = живо): у
# продукта может не быть одной ручки «служба поднята», а сводная деловая ручка
# откатывала бы образы на уже новой схеме из-за отказа, к выкату не причастного.
health_once() {
    if [ -n "${STACK_HEALTH_CMD:-}" ]; then
        bash -c "$STACK_HEALTH_CMD" >/dev/null 2>&1
    else
        [ "$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$STACK_HEALTH_URL" || true)" = "200" ]
    fi
}
health_ok() {
    local i attempts=$(( ${DEPLOY_HEALTH_WAIT_SEC:-60} / 2 ))
    [ "$attempts" -ge 1 ] || attempts=1
    for i in $(seq 1 "$attempts"); do
        health_once && return 0
        sleep 2
    done
    return 1
}

# Живость не подтверждена (или пересоздание служб упало, RELEASE_OK=0): авто-откат на
# прошлый живой выкат и подъём откатанного стека. Всегда кончается `exit 1`: выкат провален, даже
# если откат ожил.
rollback_flow() {
    [ "$RELEASE_OK" = 0 ] || say "health НЕ поднялся за потолок ${DEPLOY_HEALTH_WAIT_SEC:-60} с"
    if restore_images; then
        if [ -n "${STACK_UP_CMD:-}" ]; then
            door_step rollback "$STACK_UP_CMD" || { door_show_out; say "подъём откатанного стека упал ($DOOR_WHY) — разбирать руками"; }
        else
            say "STACK_UP_CMD пуст — образы возвращены, но стек не перезапущен; заполни в harness.conf"
        fi
        name_db_restore
        if health_ok; then
            say "откат жив (health 200) — ВЫКАТ ПРОВАЛЕН, стек на прошлой версии $ROLLED_TO"
            record_result error "ВЫКАТ ПРОВАЛЕН: живость не подтверждена, откат жив, стек на прошлой версии $ROLLED_TO"
        else
            say "откат НЕ ожил — АВАРИЯ, стек лежит"
            record_result error "АВАРИЯ: живость не подтверждена, откат не ожил, стек лежит"
        fi
    else
        say "отката нет — стек лежит, разбирать руками"
        record_result error "АВАРИЯ: живость не подтверждена, отката нет, стек лежит"
    fi
    exit 1
}

# Расписание продукта ставится выкатом, а не руками при установке (улика и довод — в deploy.sh,
# шаг 7). Не блокирует деплой: без ночных расчётов система работает, просто числа стареют, —
# а вот не поднявшийся стек это авария.
install_product_cron() {
    local cron_dst
    if [ -n "${STACK_CRON_SRC:-}" ] && [ -f "$PRODUCT_TREE/$STACK_CRON_SRC" ]; then
        cron_dst="/etc/cron.d/$(basename "$STACK_CRON_SRC")"
        if sudo -n cp "$PRODUCT_TREE/$STACK_CRON_SRC" "$cron_dst" 2>/dev/null \
           && sudo -n chown root:root "$cron_dst" 2>/dev/null \
           && sudo -n chmod 644 "$cron_dst" 2>/dev/null; then
            say "расписание продукта установлено: $cron_dst"
        else
            say "расписание НЕ установлено (нет прав sudo) — ночные расчёты не пойдут: $cron_dst"
        fi
    elif [ -n "${STACK_CRON_SRC:-}" ]; then
        say "STACK_CRON_SRC=$STACK_CRON_SRC задан, а файла нет — расписание пропущено"
    fi
}

# Reload прокси, если он есть. Упал — сайт может отдавать старое: выкат не назван успешным.
reload_proxy() {
    if [ -n "${STACK_PROXY_RELOAD_CMD:-}" ]; then
        door_step proxy "$STACK_PROXY_RELOAD_CMD" && say "прокси перечитал конфиг" \
            || { door_show_out; say "reload прокси упал ($DOOR_WHY) — сайт может отдавать старое"
                 record_result ok "выкачено с оговоркой: reload прокси упал"; exit 1; }
    else
        say "STACK_PROXY_RELOAD_CMD пуст — прокси не задан (честно пропущено)"
    fi
}

# ── Предусловия, исполнитель шагов, тестовый этап, шаги прода (фаза 3б) ──────

# Имена шагов под потолком (STACK_STEP_TIMEOUTS). Ключ с неизвестным именем отказывает: опечатка
# «biuld=1800» иначе молча оставила бы сборку на потолке по умолчанию. Умолчание одно на все шаги:
# честная сборка образа укладывается, а зависший шаг с локом в руке не должен висеть часами.
DOOR_STEP_NAMES="pull build scan migrate release frontend proxy smoke rollback"
DOOR_STEP_DEFAULT_SEC=1800
declare -A DOOR_LIMIT=()
DOOR_OUT=""        # хвост вывода последнего шага
DOOR_WHY=""        # причина провала последнего шага словами
DEPLOY_CAVEATS=""  # необязательные шаги, упавшие при живом стеке — слова итога
TESTS_NOTE=""      # что случилось с тестовым этапом — слова итога
SCAN_NOTE=""       # скан пропущен — слова итога (выкат без скана не выглядит сканированным)
ROLLED_TO=""       # к какой версии вернул откат (ставит restore_images)

door_limit_of() { printf '%s' "${DOOR_LIMIT[$1]:-$DOOR_STEP_DEFAULT_SEC}"; }

door_limits_parse() {
    local pair name sec
    DOOR_LIMIT=()
    for pair in ${STACK_STEP_TIMEOUTS:-}; do
        name=${pair%%=*}; sec=${pair#*=}
        case " $DOOR_STEP_NAMES " in
            *" $name "*) ;;
            *) say "ОШИБКА: STACK_STEP_TIMEOUTS: неизвестный шаг «$name» (известны: $DOOR_STEP_NAMES) — выкат отменён до ворот"; return 1 ;;
        esac
        [[ $sec =~ ^[1-9][0-9]*$ ]] \
            || { say "ОШИБКА: STACK_STEP_TIMEOUTS: у шага $name нужно положительное число секунд, а не «$sec» — выкат отменён до ворот"; return 1; }
        DOOR_LIMIT[$name]=$sec
    done
}

# Шаги прода идут `sudo -n -u STACK_RUN_AS`: от root строка конфига исполнялась бы с
# чужими правами, от агента — без разделения. Пусто — шаги от агента, копии без отдельного
# пользователя не ломаются. Отказ ДО тестов: ветка не должна успеть собраться и пройти тесты
# под ключом, который всё равно откажет.
door_run_as_ok() {
    local user=${STACK_RUN_AS:-} uid
    [ -n "$user" ] || return 0
    uid=$(id -u "$user" 2>/dev/null) \
        || { say "ОШИБКА: STACK_RUN_AS «$user» в $HARNESS_CONF — такого пользователя нет — выкат отменён до тестов"; return 1; }
    if [ "$uid" = 0 ] || [ "$user" = root ] || [ "$user" = "$(id -un)" ]; then
        say "ОШИБКА: STACK_RUN_AS «$user» в $HARNESS_CONF — root или пользователь агента; нужен отдельный пользователь продукта или пустой ключ — выкат отменён до тестов"
        return 1
    fi
    # Каждый шаг прода идёт `sudo -n -u`: пользователь, под которого sudo не пускает, ронял бы
    # выкат после тестов и тега, а не до них.
    sudo -n -u "$user" true >/dev/null 2>&1 </dev/null \
        || { say "ОШИБКА: STACK_RUN_AS «$user» в $HARNESS_CONF — «sudo -n -u $user true» не прошёл (нужно право sudo без пароля на этого пользователя) — выкат отменён до тестов"; return 1; }
}

# Подкоманды compose, которые поднимают службы. Остальные подкоманды (список замкнут) ставят границу:
# слово «run» после `build` — довод сборки, а не подкоманда.
COMPOSE_RAISING="up start restart run"
COMPOSE_OTHER="build pull push down stop kill create config ps logs exec cp images ls pause unpause port rm top version watch events"
# Флаги compose со значением: «--profile build» — имя профиля, не подкоманда.
COMPOSE_VALUE_FLAGS="-f --file -p --project-name --profile --env-file --project-directory"

# Поднимает ли команда $1 службы через compose: судится по словам каждого простого вызова
# (границы — ; & |). `npm run build` — не compose, `run` здесь только подкоманда compose.
compose_raises_services() {
    local line word words in_compose skip
    while IFS= read -r line; do
        read -ra words <<<"$line"
        in_compose=0
        skip=0
        for word in ${words[@]+"${words[@]}"}; do
            if [ "$in_compose" = 0 ]; then
                case $word in compose|docker-compose|*/docker-compose) in_compose=1 ;; esac
                continue
            fi
            if [ "$skip" = 1 ]; then skip=0; continue; fi
            case " $COMPOSE_VALUE_FLAGS " in *" $word "*) skip=1; continue ;; esac
            case " $COMPOSE_RAISING " in *" $word "*) return 0 ;; esac
            case " $COMPOSE_OTHER " in *" $word "*) break ;; esac
        done
    done < <(tr ';&|' '\n' <<<"$1")
    return 1
}

# Р-1: сборка идёт ДО «Да» (§5.1). Её команда с compose up/start/restart/run поднимает прод без
# одобрения — есть STACK_RELEASE_CMD или нет: подъём живёт только в нём, после миграций.
door_build_keeps_prod_ok() {
    local key
    for key in STACK_BUILD_CMD STACK_BUILD_NOCACHE_CMD; do
        if compose_raises_services "${!key:-}"; then
            say "ОШИБКА: $key в $HARNESS_CONF поднимает службы (compose $COMPOSE_RAISING) — сборка идёт до «Да» и обновила бы прод без одобрения; оставь в $key только сборку образов, а подъём служб вынеси в STACK_RELEASE_CMD — выкат отменён до сборки"
            return 1
        fi
    done
}

# Числовые ключи судятся до сборки: «4s» обрывала арифметику health_ok, и мёртвая живость
# выходила «выкачено» кодом 0; «4G» молча ломала порог памяти тестов.
door_numbers_ok() {
    local key
    for key in DEPLOY_HEALTH_WAIT_SEC STACK_CHECK_MIN_FREE_MB; do
        [[ ${!key:-0} =~ ^(0|[1-9][0-9]*)$ ]] \
            || { say "ОШИБКА: $key в $HARNESS_CONF — нужно целое число без знака и единиц, а не «${!key}» — выкат отменён до сборки"; return 1; }
    done
}

# Ключи тестового этапа судятся тут же: отказ после сборки образа стоил бы минут.
door_checks_keys_ok() { [ -z "${STACK_CHECKS_FILE:-}" ] || dchk_keys_ok; }

# Политика скана — ключ-перечисление: опечатка («critical») не должна молча стать «не судить».
SCAN_POLICIES="warn critical-fixable"
door_scan_policy_ok() {
    local policy=${STACK_SCAN_POLICY:-critical-fixable}
    case " $SCAN_POLICIES " in
        *" $policy "*) return 0 ;;
    esac
    say "ОШИБКА: STACK_SCAN_POLICY «$policy» в $HARNESS_CONF — допустимо: $SCAN_POLICIES — выкат отменён до сборки"
    return 1
}

door_preflight() { door_numbers_ok && door_build_keeps_prod_ok && door_limits_parse && door_run_as_ok && door_checks_keys_ok && door_scan_policy_ok; }

# Шаг под потолком: timeout снимает и зависший шаг, и его детей; дескрипторы локов (6, 7, 8)
# закрыты, чтобы пережившая нас служба не держала лок. Вывод идёт в файл, а не в конвейер:
# потомок с открытым концом конвейера подвесил бы дверь. Шаг идёт фоном и дверь его ждёт `wait`:
# сигнал (TERM, HUP, INT) прерывает `wait` сразу, а не после шага, и door_on_exit снимает шаг.
DOOR_STEP_PID=""   # шаг, идущий сейчас; его снимает door_on_exit
DOOR_STEP_OUT=""   # файл вывода этого шага
door_run_limited() {  # $1 имя шага, $2 каталог, $3 пользователь|пусто, $4 команда → код шага
    local limit rc=0 runner=()
    limit=$(door_limit_of "$1")
    DOOR_STEP_OUT=$(mktemp)
    [ -z "$3" ] || runner=(sudo -n -u "$3")
    timeout -k 30 "$limit" ${runner[@]+"${runner[@]}"} bash -c "cd $(printf '%q' "$2") && $4" \
        >"$DOOR_STEP_OUT" 2>&1 </dev/null 6>&- 7>&- 8>&- &
    DOOR_STEP_PID=$!
    wait "$DOOR_STEP_PID" || rc=$?
    DOOR_STEP_PID=""
    cat "$DOOR_STEP_OUT" >> "$DEPLOY_LOG"
    DOOR_OUT=$(tail -n 20 "$DOOR_STEP_OUT")
    rm -f "$DOOR_STEP_OUT"
    case "$rc" in
        0) DOOR_WHY="" ;;
        124|137) DOOR_WHY="потолок $limit с" ;;
        *) DOOR_WHY="код $rc: $(tail -n 1 <<<"$DOOR_OUT" | cut -c1-200)" ;;
    esac
    return "$rc"
}

door_show_out() { [ -z "$DOOR_OUT" ] || printf '%s\n' "$DOOR_OUT" | sed 's/^/    | /'; }

# Дерево прода отдельное от клона двери (случай CF) — только тогда pull и грязь прода имеют смысл;
# иначе шаги идут в дереве двери, как шли.
separate_prod_tree() {
    [ -n "${PRODUCT_PATH:-}" ] && [ "$(readlink -f -- "$PRODUCT_PATH")" != "$(readlink -f -- "$PRODUCT_TREE")" ]
}
prod_dir() { if separate_prod_tree; then printf '%s' "$PRODUCT_PATH"; else printf '%s' "$PRODUCT_TREE"; fi; }

door_step() { door_run_limited "$1" "$(prod_dir)" "${STACK_RUN_AS:-}" "$2"; }
door_build() { IMAGE_TAG="$IMAGE_SHA12" door_run_limited build "$PRODUCT_TREE" "" "$1"; }

# Репозиторий дерева прода читается от того же пользователя, что и шаги: права и «чужое
# владение» у них одни.
prod_git() {
    if [ -n "${STACK_RUN_AS:-}" ]; then
        sudo -n -u "$STACK_RUN_AS" git -C "$PRODUCT_PATH" --no-optional-locks "$@"
    else
        git -C "$PRODUCT_PATH" --no-optional-locks "$@"
    fi
}

# Файлы, которые правили на проде руками и которые меняет входящий sha: pull упал бы на
# них ПОСЛЕ пуша, и main ушёл бы вперёд прода. Код ≠ 0 — репозиторий не ответил: молчание
# упавшей команды прочлось бы как «чисто».
prod_dirt() {
    local own head incoming
    head=$(prod_git rev-parse HEAD 2>/dev/null) || return 1
    own=$(prod_git status --porcelain --untracked-files=all 2>/dev/null) || return 1
    incoming=$(git -C "$PRODUCT_TREE" diff --name-only "$head" "$DEPLOY_SHA" 2>/dev/null) || return 1
    comm -12 <(cut -c4- <<<"$own" | sort -u) <(sort -u <<<"$incoming")
}

# Отказ до пуша, когда у выката уже есть тег (под локом): прод не тронут, и итог называется
# «отказ», а не «ВЫКАТ ПРОВАЛЕН» — тем словом ловушка выхода помечает выкат, уже трогавший прод.
refuse_after_tag() {  # $1 = причина
    [ "$RESULT_ARMED" = 0 ] || record_result error "отказ: $1, прод не тронут"
    exit 1
}

# Дважды: до дампа (отказ ничего не оставляет) и под локом после тега (дамп снимался минуты,
# и за это время на проде могли править). После дампа дамп остаётся, в хранилище пуша нет.
prod_dirt_gate() {
    local dirt
    separate_prod_tree && [ -n "${DEPLOY_SHA:-}" ] && [ -e "$PRODUCT_PATH/.git" ] || return 0
    dirt=$(prod_dirt) || { say "ОШИБКА: не выкачено: репозиторий прода $PRODUCT_PATH не ответил при сверке правок — выкат отменён до пуша"; refuse_after_tag "репозиторий прода не ответил"; }
    [ -n "$dirt" ] || return 0
    say "ОШИБКА: не выкачено: на проде правки в $(paste -sd' ' <<<"$dirt") — ветка меняет те же файлы, pull упал бы после пуша; убери правки на проде"
    [ -z "${DB_DUMP:-}" ] || say "пуша и тега в хранилище нет, дамп остаётся: $DB_DUMP"
    refuse_after_tag "на проде правки в файлах выката"
}

# Прод принимает выкат: дерево прода до sha по тегу из хранилища (конфиги, compose). Падение —
# прод не тронут: образы и службы старые.
prod_pull() {
    separate_prod_tree && [ -n "${STACK_REMOTE:-}" ] && [ -n "${DEPLOY_SHA:-}" ] && [ -e "$PRODUCT_PATH/.git" ] || return 0
    door_step pull "git fetch -q origin 'refs/tags/$TAG' && git merge --ff-only '$DEPLOY_SHA'" \
        || { door_show_out; say "ОШИБКА: прод не принял ${DEPLOY_SHA:0:8} (шаг pull: $DOOR_WHY) — прод не тронут, выкат остановлен"; exit 1; }
}

# ── Этапы B и T ──

# Образ артефакта этапа B: первый из STACK_IMAGES под тегом :sha12. Наборы с `image: артефакт`
# идут на нём, и тестируется именно то, что поедет в прод; пусто — образа нет, наборы на своей базе.
artifact_ref() {
    local first=${STACK_IMAGES:-}; first=${first%% *}
    [ -n "$first" ] && [ -n "${IMAGE_SHA12:-}" ] || return 0
    printf '%s:%s' "$first" "$IMAGE_SHA12"
}

# Этап B: сборка :sha12. Записка success на этот sha (с тем же хэшем файла проверок) и готовый
# образ делают её лишней: наборы шли на нём же, второй раз собирать и гонять нечего.
stage_build() {
    local first=${STACK_IMAGES:-}; first=${first%% *}
    set_image_sha12
    if [ -n "$first" ] && [ -n "$IMAGE_SHA12" ] && images_present $(tagged_refs) && door_checks_noted "$DEPLOY_SHA"; then
        say "по записке: тесты по ${DEPLOY_SHA:0:8} уже success, образ :$IMAGE_SHA12 есть — сборка и наборы не повторяются"
        TESTS_NOTE="тесты: по записке"
    else
        need_nocache
        remember_prebuild
        build_product
        label_built_images
    fi
    capture_image_ids
}

# Слово итога тестов берётся из записки: прогон шёл в подоболочке, и его переменные не дошли.
# Записка — СВОЕГО sha: последняя строка журнала могла писаться чужим прогоном, а прогон, что не
# дошёл до наборов (занятый checks.lock), записки не оставляет — тогда «error», то есть «не отработали».
checks_verdict() {
    local verdict
    verdict=$(jq -r --arg sha "${DEPLOY_SHA:-}" 'select(.data.sha == $sha) | .data.tests // "error"' \
        "${HARNESS_LOG_DIR:-$LOG_DIR}/deploy-checks.jsonl" 2>/dev/null | tail -n 1) || verdict=""
    printf '%s\n' "${verdict:-error}"
}

# Этап T: наборы продукта на образе этапа B. «Красные» (код судить можно) и «не отработали»
# (установка, потолок, память, docker — судить нельзя) — разные слова: чинят их по-разному.
stage_tests() {
    local rc=0
    [ -z "$TESTS_NOTE" ] || return 0
    if [ -z "${STACK_CHECKS_FILE:-}" ]; then
        say "тесты: STACK_CHECKS_FILE пуст — этап тестов пропущен: не задано (выкат идёт без наборов продукта)"
        TESTS_NOTE="тесты: пропущены, не задано STACK_CHECKS_FILE"
        return 0
    fi
    [ -n "${DEPLOY_SHA:-}" ] || { say "ОШИБКА: тесты: у выката нет sha — выкат отменён до ворот"; exit 1; }
    door_checks_cached "$DEPLOY_SHA" "${BRANCH:-${STACK_BRANCH:-HEAD}}" "$(artifact_ref)" || rc=$?
    if [ "$rc" = 0 ]; then
        TESTS_NOTE="тесты: success"
        SUITES_NOTE=$(suites_note)
        return 0
    fi
    if [ "$(checks_verdict)" = failure ]; then
        say "ОШИБКА: тесты красные — код ветки не прошёл наборы; выкат отменён до ворот, прод не тронут"
    else
        say "ОШИБКА: тесты не отработали (установка, потолок времени, память или docker) — о коде судить нельзя; выкат отменён до ворот, прод не тронут"
    fi
    exit 1
}

# «Наборы N из M»: прошло наборов по записке success этого sha (запись несёт прошедшие, не выбранные) из всех в файле проверок на нём. Не узнано — «?»,
# а не нуль: нуль читался бы как «наборов нет».
suites_note() {
    local picked total
    picked=$(jq -r --arg sha "$DEPLOY_SHA" 'select(.data.sha == $sha and .data.tests == "success") | .data.suites | length' \
        "${HARNESS_LOG_DIR:-$LOG_DIR}/deploy-checks.jsonl" 2>/dev/null | tail -n 1) || picked=""
    total=$(git -C "$PRODUCT_TREE" show "$DEPLOY_SHA:$STACK_CHECKS_FILE" 2>/dev/null \
        | python3 "$DOOR_CHECKS_PY" наборы /dev/stdin 2>/dev/null | jq length 2>/dev/null) || total=""
    printf '%s из %s' "${picked:-?}" "${total:-?}"
}

# ── Этап D/I: скан собранных образов ──

# Один образ: сканер получает «<образ>:<sha12>» последним доводом, его stdout разбирает scan_verdict.py.
# Сканер без вывода и битый вывод — одно: «не прочитан». Код сканера не судится: Trivy с --exit-code
# выходит 1 именно на находках и всё равно отдаёт JSON; судит только разобранный вывод.
# Печатает вердикт словами в stdout (названные уязвимости), код — как у scan_verdict.py.
scan_one() {  # $1 = образ:sha12
    local out rc=0
    out=$(timeout "$(door_limit_of scan)" bash -c "$STACK_SCAN_CMD $(sh_quote "$1")" 2>/dev/null) || true
    printf '%s' "$out" | python3 -I "$SCAN_VERDICT_PY" || rc=$?
    return "$rc"
}

# Вердикт по политике: critical-fixable — отказ до ворот; warn и «не прочитан» — оговорка в итоге,
# выкат идёт; названные уязвимости в любом случае попадают в вывод.
stage_scan() {
    local img ref verdict rc
    if [ -z "${STACK_SCAN_CMD:-}" ]; then
        say "STACK_SCAN_CMD пуст — скан пропущен: не задано (образ выкатывается без скана)"
        SCAN_NOTE="скан: пропущен, не задано STACK_SCAN_CMD"
        return 0
    fi
    if [ -z "${STACK_IMAGES:-}" ] || [ -z "${IMAGE_SHA12:-}" ]; then
        say "STACK_SCAN_CMD задан, а сканировать нечего (STACK_IMAGES пуст или sha не узнан) — скан пропущен"
        SCAN_NOTE="скан: пропущен, нечего сканировать (STACK_IMAGES пуст или sha не узнан)"
        return 0
    fi
    for img in $STACK_IMAGES; do
        ref="$img:$IMAGE_SHA12"
        rc=0
        verdict=$(scan_one "$ref") || rc=$?
        [ -z "$verdict" ] || printf '%s\n' "$verdict" | sed "s|^|    скан $ref: |" | tee -a "$DEPLOY_LOG"
        case "$rc" in
            0) say "скан $ref: блокирующих уязвимостей нет" ;;
            1) if [ "${STACK_SCAN_POLICY:-critical-fixable}" = critical-fixable ]; then
                   say "ОШИБКА: скан $ref: CRITICAL с исправлением (STACK_SCAN_POLICY=critical-fixable) — выкат отменён до ворот, прод не тронут"
                   exit 1
               fi
               say "скан $ref: CRITICAL с исправлением, политика warn — выкат идёт с оговоркой"
               DEPLOY_CAVEATS="${DEPLOY_CAVEATS:+$DEPLOY_CAVEATS, }скан: CRITICAL с исправлением в $ref" ;;
            *) say "скан $ref: скан не прочитан (сканер упал или вывод не разобран) — выкат идёт с оговоркой"
               DEPLOY_CAVEATS="${DEPLOY_CAVEATS:+$DEPLOY_CAVEATS, }скан не прочитан: $ref" ;;
        esac
    done
}

# ── Шаги прода ──

run_migrations() {
    if [ -z "${STACK_MIGRATE_CMD:-}" ]; then
        say "STACK_MIGRATE_CMD пуст — миграций у продукта пока нет (честно пропущено)"
        return 0
    fi
    say "миграции: $STACK_MIGRATE_CMD"
    door_step migrate "$STACK_MIGRATE_CMD" && return 0
    door_show_out
    # Службы не трогались, но :latest уже указывает на НОВЫЕ образы: любое
    # следующее пересоздание подняло бы новый код на полунакатанную схему.
    say "миграции упали — службы не пересоздавались, схема могла накатиться частично ($DOOR_WHY)"
    back_out_build
}

# Пересоздание служб ПОСЛЕ миграций: новый код встаёт на уже новую схему. Пусто — службы
# поднимает сама сборка (штатно). Отказ идёт в откат: старые контейнеры ответили бы на
# health 200, и выкат, которого не было, назвался бы успешным. Ставит RELEASE_OK.
release_services() {
    RELEASE_OK=1
    [ -n "${STACK_RELEASE_CMD:-}" ] || return 0
    say "пересоздание служб: $STACK_RELEASE_CMD"
    # С этой минуты службы могут стоять на новом образе: возврат :latest без подъёма их не вернёт.
    SERVICES_RELEASED=1
    door_step release "$STACK_RELEASE_CMD" && return 0
    door_show_out
    say "пересоздание служб упало ($DOOR_WHY) — выкат не состоялся, иду в откат"
    RELEASE_OK=0
}

# Необязательный шаг не блокирует живой выкат: падение называется в итоге словами (§5.3 п. 7).
# Упавший шаг мог убить стек: живость спрашивается заново, и мёртвый стек — откат (п. 5), а не оговорка.
optional_step() {  # $1 имя шага, $2 команда (пусто — шаг не задан), $3 слово оговорки
    [ -n "$2" ] || return 0
    door_step "$1" "$2" && return 0
    door_show_out
    if ! health_ok; then
        say "шаг $1 упал ($DOOR_WHY), и стек после него не отвечает — иду в откат"
        rollback_flow
    fi
    say "шаг $1 упал ($DOOR_WHY) — стек жив, выкат не блокируется; в итоге: выкачено с оговоркой: $3"
    DEPLOY_CAVEATS="${DEPLOY_CAVEATS:+$DEPLOY_CAVEATS, }$3"
}

# Фронт идёт после живости: мёртвый бэкенд не получает новой витрины.
prod_frontend() {
    optional_step frontend "${STACK_FRONTEND_CMD:-}" фронт
}

prod_smoke() {
    optional_step smoke "${STACK_SMOKE_CMD:-}" смоук
}

# Итог словами: оговорки и судьба тестового этапа названы, а не прикрыты «выкачено» (И-4).
final_words() {
    local words="выкачено: health 200"
    [ -z "$DEPLOY_CAVEATS" ] || words="выкачено с оговоркой: $DEPLOY_CAVEATS"
    printf '%s%s%s' "$words" "${TESTS_NOTE:+; $TESTS_NOTE}" "${SCAN_NOTE:+; $SCAN_NOTE}"
}
