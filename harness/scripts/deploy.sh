#!/usr/bin/env bash
#
# deploy.sh — выкатка одной командой в гарантированном порядке.
#
# Откуда взят: UNIFIED/templates/deploy.sh — СЕМЯ первой установки, а не живой
# код: оно отстало и не обновляется (замер 27.09.2026).
# Что изменено: имена контейнеров и команды стека вынесены в /etc/harness/harness.conf
# (секция STACK_* — данные, не код); добавлены ворота deploy_guard.py, тег выката,
# снимок образов :previous и авто-откат на него при мёртвом health, пересборка
# --no-cache при смене манифестов зависимостей (CLAUDE.md §9), reload прокси;
# на чистом сервере без продукта скрипт честно говорит «выкатывать нечего» и
# выходит нулём — харнес ставится раньше продукта, и деплой не имеет права
# врать, будто что-то выкатил.
#
# Порядок (менять нельзя, каждый шаг стоит на случившемся отказе):
#   лок (flock, до ворот — К-3) → ворота → тег → снимок образов → сборка →
#   ДАМП БАЗЫ → миграции → health циклом → (при провале — откат на :previous) →
#   снимок карты → reload прокси.
#
# Почему дамп именно здесь, а не в демоне бэкапа: откатить образы можно, а
# миграцию — нет. `alembic upgrade head` меняет схему необратимо, и при
# провале health авто-откат возвращает СТАРЫЙ код на НОВУЮ схему. До 02.09.2026
# в этом скрипте не было ни одного упоминания дампа: точка отката существовала
# только для образов (И-1).
#
# Улика из шаблона: работа, прошедшая мимо карты, чинится здесь же. Деплой —
# единственный путь, которым код попадает на прод: пропустить его, оставив
# работу работающей, невозможно, поэтому сверка тут надёжнее запрета на коммит.
#
# Запуск: ./scripts/deploy.sh [--силой]   (--силой уходит в ворота как есть)
#         ./scripts/deploy.sh --план     что сделал бы: шаги и команды, без ворот и выката
set -euo pipefail

# ── конфиги ─────────────────────────────────────────────────────────────────
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/config.sh"
INSTALL_CONF="${HARNESS_INSTALL_CONF:-$(konf_koren)/install.conf}"
HARNESS_CONF="${HARNESS_CONF:-$(konf_koren)/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "deploy: нет $INSTALL_CONF — установка не завершена (01-SPEC §0)"; exit 1; }
# shellcheck disable=SC1090
# Конфиги грузятся так, чтобы ОКРУЖЕНИЕ было старше файла: иначе проба с
# подставным путём судит БОЕВУЮ установку (улика 12.09.2026).
# shellcheck disable=SC1091
# Путь берётся по РЕАЛЬНОМУ файлу (readlink -f): pre-commit подключён в
# .git/hooks симлинком, и dirname дал бы .git/hooks, где библиотеки нет.
# Поймано первым же коммитом после правки, 12.09.2026.
konf_zagruzit PROJECT_DIR LOG_DIR TMUX_SESSION SECRETS_DIR AGENT_START_CMD
# harness.conf несёт секцию STACK_*; без него продукт заведомо не задан.
: "${PROJECT_DIR:?в install.conf пуст PROJECT_DIR}" "${LOG_DIR:?пуст LOG_DIR}"

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DEPLOY_LOG="$LOG_DIR/deploy.log"
mkdir -p "$LOG_DIR" "$LOG_DIR/locks"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$DEPLOY_LOG"; }

# Репозиторий того, что выкатывается: тег, сверка секретов, манифесты и файл
# расписания судят ПРОДУКТ. Продукт живёт своим репозиторием рядом с харнесом,
# и PROJECT_DIR здесь — каталог харнеса: тег и сверка секретов по нему судили
# бы не то, что едет на прод.
PRODUCT_TREE="${PRODUCT_REPO:-$PROJECT_DIR}"
cd "$PRODUCT_TREE"

# ── 0. Продукт задан? ───────────────────────────────────────────────────────
# Пустая STACK_BUILD_CMD и отсутствие docker-compose.yml = продукта ещё нет.
# Честный выход нулём, а не имитация выката: харнес ставится на чистый сервер
# раньше первой строки кода продукта, и этот случай — штатный, не ошибка.
# Но продукт, ОБЪЯВЛЕННЫЙ в harness.conf, при пустой секции — не «нечего», а
# выкат мимо ворот: нуль здесь читался как «выкачено» (И-2).
if [ -z "${STACK_BUILD_CMD:-}" ] && [ -n "${PRODUCT_DIR:-}${PRODUCT_REPO:-}" ]; then
    say "ОШИБКА: продукт объявлен (PRODUCT_DIR=${PRODUCT_DIR:-}, PRODUCT_REPO=${PRODUCT_REPO:-}), а STACK_BUILD_CMD в $HARNESS_CONF пуст — заполни секцию STACK_*, деплой вслепую не выкатывает"
    exit 1
fi
if [ -z "${STACK_BUILD_CMD:-}" ] && [ ! -f "$PROJECT_DIR/docker-compose.yml" ]; then
    say "продукт не задан (нет docker-compose.yml/сборки) — выкатывать нечего; заполни секцию STACK_* в $HARNESS_CONF с первой задачей продукта"
    exit 0
fi
if [ -z "${STACK_BUILD_CMD:-}" ]; then
    say "ОШИБКА: docker-compose.yml есть, а STACK_BUILD_CMD в harness.conf пуст — заполни секцию STACK_*, деплой вслепую не выкатывает"
    exit 1
fi

# ── 0б. Дамп базы: функция и её самотест ────────────────────────────────────
# Порог «дамп состоялся» замерен, а не придуман (02.09.2026): pg_dump -Fc прода —
# 38 363 428 байт, только схема стенда — 92 657, дамп без единой таблицы — 0.
# 1024 отделяет ноль и обрывок от любого настоящего дампа с запасом в 90 раз.
DUMP_MIN=1024

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

# Самотест стоит ДО лока и ворот: он ничего не выкатывает, а занимать ими выкат
# не имеет права. Проверяет оба исхода живьём, на временном каталоге.
if [ "${1:-}" = "--selftest-dump" ]; then
    TMP_DIR=$(mktemp -d)
    trap 'rm -rf "$TMP_DIR"' EXIT
    ERRORS=0

    STACK_DB_DUMP_CMD="head -c 4096 /dev/urandom"
    snapshot_db "$TMP_DIR/полный.dump" \
        && say "самотест 1/3: непустой дамп принят — ВЕРНО" \
        || { say "самотест 1/3: непустой дамп отвергнут — ОШИБКА"; ERRORS=$((ERRORS+1)); }

    STACK_DB_DUMP_CMD="true"
    if snapshot_db "$TMP_DIR/пустой.dump"; then
        say "самотест 2/3: ПУСТОЙ дамп принят — ОШИБКА (выкат пошёл бы без точки отката)"
        ERRORS=$((ERRORS+1))
    else
        say "самотест 2/3: пустой дамп отвергнут — ВЕРНО"
    fi

    STACK_DB_DUMP_CMD=""
    RC=0
    snapshot_db "$TMP_DIR/нет.dump" || RC=$?
    if [ "$RC" -eq 2 ]; then
        say "самотест 3/3: незаданная команда названа вслух — ВЕРНО"
    else
        say "самотест 3/3: незаданная команда прошла молча — ОШИБКА"
        ERRORS=$((ERRORS+1))
    fi

    [ "$ERRORS" = 0 ] && { say "самотест дампа: 3/3"; exit 0; }
    say "самотест дампа: ошибок $ERRORS"
    exit 1
fi

# ── 0в. Дерево продукта чистое? ─────────────────────────────────────────────
# Образ собирается из РАБОЧЕГО дерева, а тег ставится на HEAD: незакоммиченная
# правка уехала бы на прод непроверенной, и тег адресовал бы не то, что
# поехало (И-4). Выкатывается только закоммиченное.
TREE_DIRTY=""
if git -C "$PRODUCT_TREE" rev-parse --git-dir >/dev/null 2>&1; then
    TREE_DIRTY=$(git -C "$PRODUCT_TREE" status --porcelain 2>&1) \
        || TREE_DIRTY="git status не ответил: $TREE_DIRTY"
fi
TREE_NOTE="чисто"
[ -z "$TREE_DIRTY" ] || TREE_NOTE="НЕЗАКОММИЧЕНО $(printf '%s\n' "$TREE_DIRTY" | wc -l) — выкат откажет"

# ── 0г. План: что сделал бы — без docker, лока, ворот и выката ─────────────
# Стоит ДО перезапуска под sg: плану docker не нужен, и ключ плана не
# проходит через чужую оболочку, где мог бы исказиться.
if [ "${1:-}" = "--план" ]; then
    say "план выката (ничего не выполняется):"
    say "  репозиторий продукта: $PRODUCT_TREE (тег deploy-…, сверка секретов, манифесты)"
    say "  дерево продукта: $TREE_NOTE"
    say "  образы для снимка :previous: ${STACK_IMAGES:-<пусто — отката не будет>}"
    say "  сборка: $STACK_BUILD_CMD"
    say "  сборка --no-cache (смена манифестов): ${STACK_BUILD_NOCACHE_CMD:-<пусто — обычная сборка>}"
    say "  дамп базы: ${STACK_DB_DUMP_CMD:-<пусто — без дампа>}"
    say "  миграции: ${STACK_MIGRATE_CMD:-<пусто>}"
    say "  пересоздание служб: ${STACK_RELEASE_CMD:-<пусто — службы поднимает сборка>}"
    say "  живость: ${STACK_HEALTH_CMD:-${STACK_HEALTH_URL:-<пусто — выкат откажет>}}"
    say "  подъём при откате: ${STACK_UP_CMD:-<пусто>}"
    say "  восстановление базы (руками): ${STACK_DB_RESTORE_HINT:-pg_restore по дампу}"
    say "  reload прокси: ${STACK_PROXY_RELOAD_CMD:-<пусто>}"
    exit 0
fi

if [ -n "$TREE_DIRTY" ]; then
    say "ОШИБКА: в $PRODUCT_TREE незакоммиченные правки — выкатывается только закоммиченное, выкат отменён до ворот:"
    printf '%s\n' "$TREE_DIRTY" | head -10 | sed 's/^/    /' | tee -a "$DEPLOY_LOG"
    exit 1
fi

# ── 0д. Docker доступен? ────────────────────────────────────────────────────
# Снимок и откат зовут docker сами. Сессия, начатая до вступления пользователя
# в группу docker, сокета не видит, а `docker image inspect` без доступа
# неотличим от «образа нет»: выкат шёл бы дальше со словами «первый выкат —
# откатываться не на что». Член группы по /etc/group перезапускается под
# `sg docker` — один раз (DEPLOY_UNDER_SG), иначе отказ словами до ворот.
# Ключи передаются в одинарных кавычках POSIX: sg исполняет строку через
# /bin/sh (dash), а `printf %q` в локали C даёт $'\320…', которого dash не знает.
sh_quote() { printf "'%s' " "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
if [ -n "${STACK_IMAGES:-}" ] && ! docker version >/dev/null 2>&1; then
    if [ -z "${DEPLOY_UNDER_SG:-}" ] && command -v sg >/dev/null 2>&1 \
       && getent group docker | cut -d: -f4 | tr ',' '\n' | grep -qx "$(id -un)"; then
        say "docker не виден этой сессии, а в группе docker пользователь есть — перезапуск под sg docker"
        export DEPLOY_UNDER_SG=1
        SG_CMD=""
        for arg in bash "$(readlink -f "$0")" "$@"; do SG_CMD="$SG_CMD$(sh_quote "$arg")"; done
        exec sg docker -c "$SG_CMD"
    fi
    say "ОШИБКА: docker недоступен ($(docker version 2>&1 | tail -1 | cut -c1-160)) — снимка и отката не будет, выкат отменён до ворот; ключи: $*"
    exit 1
fi

# ── 1. Свой лок ДО ворот, атомарно через flock (К-3) ────────────────────────
# Раньше лок писался ПОСЛЕ ворот простым printf — окно «проверил→создал»
# пропускало два одновременных deploy.sh (улика 05.08: контейнеры под
# временными именами, API лёг на минуту). flock атомарен: второй выкат
# упирается сюда, не в гонку. Ворота этот лок видят, но не блокируются им:
# deploy_guard пропускает лок, который держит его собственный предок.
DEPLOY_LOCK="$LOG_DIR/locks/deploy.lock"
# Открытие с >>, не >: усечение при открытии стёрло бы запись о держателе
# раньше, чем flock скажет «занято» (та же грабля, что в tg-dispatcher.sh).
exec 8>> "$DEPLOY_LOCK"
if ! flock -n 8; then
    HOLDER=$(sed -n 2p "$DEPLOY_LOCK" 2>/dev/null || true)
    say "выкат уже идёт (${HOLDER:-держатель не назвался}) — второй одновременный отменён"
    exit 1
fi
printf '%s\nвыкат deploy.sh, начат %s\n' "$$" "$(date -Is)" > "$DEPLOY_LOCK"
# Лок-файл НЕ удаляется (П-12): flock снимается сам с закрытием fd на выходе,
# а rm в trap открывал микро-гонку — второй выкат, успевший открыть файл до
# удаления, держал бы flock на снесённом inode, и третий выкат прошёл бы мимо
# него по новому файлу. Пустой файл в locks/ дешевле этой гонки.

# ── 1б. Ворота: не катить поверх идущей работы ──────────────────────────────
# 04.08 выкат перезапустил воркер и убил чужую сборку (улика в deploy_guard.py).
python3 "$SCRIPT_DIR/deploy_guard.py" "$@" || { say "ворота: занято — выкат отменён"; exit 1; }
# Разрешение владельца ИЗРАСХОДОВАНО проходом ворот, а выкат ещё впереди.
# Упадём дальше — вернём его: иначе владелец тратит «да» на действие, которого
# не было, и просить приходится второй раз (так публикация уже чинилась
# 12.09.2026; тот же дефект жил и здесь). Ловушка снимается в самом конце.
VERNUT_SOGLASIE=1
vernut_soglasie_pri_otkaze() {
    [ "${VERNUT_SOGLASIE:-0}" = 1 ] || return 0
    python3 "$SCRIPT_DIR/deploy_guard.py" --действие deploy \
        --вернуть-согласие 2>&1 | sed 's/^/    /' || true
}
trap vernut_soglasie_pri_otkaze EXIT

# ── 1а. Секреты не едут в прод (И-3: check-secrets и в pre-commit, и здесь) ──
# Дубль не лишний: pre-commit можно обойти (--no-verify), деплой — нельзя.
# Через bash, а не по биту исполнения: в пакете файл лежит 644, и отказ
# «Permission denied» читался как «секреты найдены».
bash "$SCRIPT_DIR/check-secrets.sh" "$PRODUCT_TREE" || { say "секреты в репозитории — выкат отменён (И-3)"; exit 1; }

# ── 2. Тег выката: каждая выкатка адресуема ─────────────────────────────────
TAG="deploy-$(date +%Y%m%d-%H%M%S)"
if git rev-parse --git-dir >/dev/null 2>&1; then
    git tag "$TAG" 2>/dev/null && say "тег: $TAG" || say "тег не поставлен (повтор в ту же секунду?) — не блокирует"
else
    say "репозиторий не инициализирован — тег пропущен"
fi

# ── 3. Снимок образов: точка отката ДО сборки ───────────────────────────────
# STACK_IMAGES — имена образов через пробел (данные). Пусто — отката не будет,
# и это говорится вслух, а не выясняется в момент аварии.
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

# ── 4. Сборка; смена манифестов зависимостей → --no-cache ───────────────────
# CLAUDE.md §9: после изменения зависимостей — только --no-cache. Сравниваем
# манифесты с прошлым тегом выката; нет прошлого тега — считаем смену была
# (ошибиться в сторону --no-cache дешевле, чем собрать со старым кэшем).
MANIFESTS="${STACK_MANIFESTS:-requirements.txt package.json package-lock.json Dockerfile docker-compose.yml}"
NOCACHE=0
if git rev-parse --git-dir >/dev/null 2>&1; then
    # -refname, не -creatordate: у lightweight-тегов creatordate — дата КОММИТА,
    # не выката, и порядок врал; имена deploy-ГГГГММДД-ЧЧММСС датированы —
    # лексикографика надёжна (К-3).
    PREV_TAG=$(git tag -l 'deploy-*' --sort=-refname | sed -n 2p)
    if [ -z "$PREV_TAG" ]; then
        NOCACHE=1
        say "прошлого тега выката нет — сборка с --no-cache (первый выкат)"
    else
        for m in $MANIFESTS; do
            if ! git diff --quiet "$PREV_TAG" HEAD -- "*$m" 2>/dev/null; then
                NOCACHE=1
                say "манифест зависимостей изменился с $PREV_TAG ($m) — сборка с --no-cache"
                break
            fi
        done
    fi
else
    NOCACHE=1
fi
if [ "$NOCACHE" = 1 ] && [ -n "${STACK_BUILD_NOCACHE_CMD:-}" ]; then
    say "сборка (--no-cache): $STACK_BUILD_NOCACHE_CMD"
    bash -c "$STACK_BUILD_NOCACHE_CMD" || { say "сборка упала"; exit 1; }
else
    [ "$NOCACHE" = 1 ] && say "STACK_BUILD_NOCACHE_CMD пуст — иду обычной сборкой (заполни в harness.conf)"
    say "сборка: $STACK_BUILD_CMD"
    bash -c "$STACK_BUILD_CMD" || { say "сборка упала"; exit 1; }
fi

# ── 4б. Дамп базы ПЕРЕД миграциями — точка отката схемы (И-1) ───────────────
# Ключ пуст = продукт без базы; это штатно и говорится вслух. Ключ задан, а дамп
# не снялся — выкат останавливается: миграция без точки отката необратима.
DB_DUMP=""
if [ -n "${STACK_DB_DUMP_CMD:-}" ]; then
    DB_DUMP="${BACKUP_DIR:-/var/backups/harness}/перед-выкатом-$TAG/база.dump"
    if ! snapshot_db "$DB_DUMP"; then
        say "дамп базы не снят — выкат остановлен (И-1): миграции необратимы, откатывать схему было бы нечем"
        exit 1
    fi
else
    say "STACK_DB_DUMP_CMD пуст — дампа базы перед миграциями нет; если у продукта есть база, заполни ключ в harness.conf"
fi

# Откат образов и подсказка восстановления — общие для упавших миграций и
# красной живости. Восстановление базы НЕ выполняется: оно необратимо и
# решается владельцем; называются дамп и команда.
restore_images() {
    if [ "$ROLLBACK_READY" != 1 ]; then
        say "образов :previous нет (снимок не делался) — возвращать нечего"
        return 1
    fi
    say "возвращаю образы :previous → :latest"
    for img in $STACK_IMAGES; do
        docker image inspect "$img:previous" >/dev/null 2>&1 && docker tag "$img:previous" "$img:latest"
    done
    return 0
}
name_db_restore() {
    [ -n "$DB_DUMP" ] || return 0
    say "схема базы НЕ откачена автоматически; дамп до миграций: $DB_DUMP"
    if [ -n "${STACK_DB_RESTORE_HINT:-}" ]; then
        say "восстановление руками (решение владельца): $STACK_DB_RESTORE_HINT"
    else
        say "восстановление руками (решение владельца): pg_restore -U ${DB_ADMIN_USER:-<DB_ADMIN_USER не задан>} -d ${DB_ADMIN_USER:-<DB_ADMIN_USER не задан>} --clean $DB_DUMP"
    fi
}

# ── 5. Миграции ─────────────────────────────────────────────────────────────
if [ -n "${STACK_MIGRATE_CMD:-}" ]; then
    say "миграции: $STACK_MIGRATE_CMD"
    if ! bash -c "$STACK_MIGRATE_CMD"; then
        # Службы не трогались, но :latest уже указывает на НОВЫЕ образы: любое
        # следующее пересоздание подняло бы новый код на полунакатанную схему.
        say "миграции упали — службы не пересоздавались, схема могла накатиться частично"
        restore_images
        name_db_restore
        exit 1
    fi
else
    say "STACK_MIGRATE_CMD пуст — миграций у продукта пока нет (честно пропущено)"
fi

# ── 5б. Пересоздание служб ПОСЛЕ миграций ───────────────────────────────────
# Новый код поднимается на уже новой схеме. Пусто — службы поднимает сама
# сборка (`up -d --build`), это штатно. Отказ пересоздания идёт в ветку
# отката: старые контейнеры ответили бы на health 200, и выкат, которого не
# было, назвался бы успешным.
RELEASE_OK=1
if [ -n "${STACK_RELEASE_CMD:-}" ]; then
    say "пересоздание служб: $STACK_RELEASE_CMD"
    bash -c "$STACK_RELEASE_CMD" || { say "пересоздание служб упало — выкат не состоялся, иду в откат"; RELEASE_OK=0; }
fi

# ── 6. Health циклом; провал → авто-откат на :previous ──────────────────────
# Цикл: 30 попыток × (запрос до 5 с + пауза 2 с) — от 60 с до ~210 с (К-3:
# раньше и комментарий, и сообщение врали про «60 с»).
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
    local i
    for i in $(seq 1 30); do
        health_once && return 0
        sleep 2
    done
    return 1
}
if [ -n "${STACK_HEALTH_CMD:-}${STACK_HEALTH_URL:-}" ]; then
    if [ "$RELEASE_OK" = 1 ] && health_ok; then
        say "health 200 — выкат жив"
    else
        [ "$RELEASE_OK" = 0 ] || say "health НЕ поднялся за цикл проверки (30 попыток, 60–210 с)"
        if restore_images; then
            if [ -n "${STACK_UP_CMD:-}" ]; then
                bash -c "$STACK_UP_CMD" || say "подъём откатанного стека упал — разбирать руками"
            else
                say "STACK_UP_CMD пуст — образы возвращены, но стек не перезапущен; заполни в harness.conf"
            fi
            name_db_restore
            if health_ok; then
                say "откат жив (health 200) — выкат ПРОВАЛЕН, стек на прошлой версии"
            else
                say "откат НЕ ожил — АВАРИЯ, стек лежит"
            fi
        else
            say "отката нет — стек лежит, разбирать руками"
        fi
        exit 1
    fi
else
    say "STACK_HEALTH_CMD и STACK_HEALTH_URL пусты — живость не проверена; выкат без health-проверки успехом НЕ считается, заполни ключ"
    exit 1
fi

# ── 7. Снимок карты разработки (не блокирует выкат) ─────────────────────────
# Улика шаблона: карта не должна мешать выкатке — но и выкатка не должна
# проходить мимо карты.
SELFHEAL=""
MAP_NOTE="карта сверена"
for c in "$SCRIPT_DIR/devmap-selfheal.sh" "$SCRIPT_DIR/../demons/devmap-selfheal.sh" \
         "$SCRIPT_DIR/../harness/demons/devmap-selfheal.sh"; do
    [ -x "$c" ] && SELFHEAL="$c" && break
done
if [ -n "$SELFHEAL" ]; then
    "$SELFHEAL" "${DEVMAP_AUDIT_DAYS:-14}" || { say "сверка карты не отработала (деплой не затронут)"; MAP_NOTE="сверка карты НЕ прошла"; }
else
    say "devmap-selfheal.sh не найден — сверка карты пропущена (деплой не затронут)"
    MAP_NOTE="сверка карты пропущена"
fi

# Улика шаблона: автопамять «залипает» на имени подкаталога — сводим осколки
# здесь же, где и пропуски в карте. Не блокирует деплой.
python3 "$SCRIPT_DIR/fix-claude-mem-project.py" || say "сведение имён автопамяти не отработало (деплой не затронут)"

# ── 7б. Расписание продукта: ночные расчёты ставятся ВЫКАТОМ ────────────────
# Улика 10.09.2026: в crontab сервера было восемь строк, и все восемь —
# служебные, харнесовы. У продукта фона не оказалось ни одного, поэтому
# эталонный прогон на проде датировался 02.09: числа на экране диспетчера
# отставали на неделю, и заметил это владелец, а не система.
#
# Ставится здесь, а не руками при установке: решение, которое требует «чтобы
# кто-то помнил», — не решение. У заказчика тот же выкат поставит тот же файл.
# Не блокирует деплой: без ночных расчётов система работает, просто числа
# стареют, — а вот не поднявшийся стек это авария.
if [ -n "${STACK_CRON_SRC:-}" ] && [ -f "$PRODUCT_TREE/$STACK_CRON_SRC" ]; then
    CRON_DST="/etc/cron.d/$(basename "$STACK_CRON_SRC")"
    if sudo -n cp "$PRODUCT_TREE/$STACK_CRON_SRC" "$CRON_DST" 2>/dev/null \
       && sudo -n chown root:root "$CRON_DST" 2>/dev/null \
       && sudo -n chmod 644 "$CRON_DST" 2>/dev/null; then
        say "расписание продукта установлено: $CRON_DST"
    else
        say "расписание НЕ установлено (нет прав sudo) — ночные расчёты не пойдут: $CRON_DST"
    fi
elif [ -n "${STACK_CRON_SRC:-}" ]; then
    say "STACK_CRON_SRC=$STACK_CRON_SRC задан, а файла нет — расписание пропущено"
fi

# ── 8. Reload прокси, если он есть ──────────────────────────────────────────
if [ -n "${STACK_PROXY_RELOAD_CMD:-}" ]; then
    bash -c "$STACK_PROXY_RELOAD_CMD" && say "прокси перечитал конфиг" \
        || { say "reload прокси упал — сайт может отдавать старое"; exit 1; }
else
    say "STACK_PROXY_RELOAD_CMD пуст — прокси не задан (честно пропущено)"
fi

# Итоговая строка не приукрашивает: что не прошло — названо (И-4).
# Выкат СОСТОЯЛСЯ — согласие потрачено по делу, возвращать нечего.
VERNUT_SOGLASIE=0
say "deploy ok: тег $TAG, health 200, $MAP_NOTE"
