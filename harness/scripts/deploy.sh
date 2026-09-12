#!/usr/bin/env bash
#
# deploy.sh — выкатка одной командой в гарантированном порядке.
#
# Откуда взят: UNIFIED/templates/deploy.sh (живой скрипт боевого сервера).
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
set -euo pipefail

# ── конфиги ─────────────────────────────────────────────────────────────────
INSTALL_CONF="${HARNESS_INSTALL_CONF:-/etc/harness/install.conf}"
HARNESS_CONF="${HARNESS_CONF:-/etc/harness/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "deploy: нет $INSTALL_CONF — установка не завершена (01-SPEC §0)"; exit 1; }
# shellcheck disable=SC1090
# Конфиги грузятся так, чтобы ОКРУЖЕНИЕ было старше файла: иначе проба с
# подставным путём судит БОЕВУЮ установку (улика 12.09.2026).
# shellcheck disable=SC1091
# Путь берётся по РЕАЛЬНОМУ файлу (readlink -f): pre-commit подключён в
# .git/hooks симлинком, и dirname дал бы .git/hooks, где библиотеки нет.
# Поймано первым же коммитом после правки, 12.09.2026.
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/konf.sh"
konf_zagruzit PROJECT_DIR LOG_DIR TMUX_SESSION SECRETS_DIR AGENT_START_CMD
# harness.conf несёт секцию STACK_*; без него продукт заведомо не задан.
: "${PROJECT_DIR:?в install.conf пуст PROJECT_DIR}" "${LOG_DIR:?пуст LOG_DIR}"

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DEPLOY_LOG="$LOG_DIR/deploy.log"
mkdir -p "$LOG_DIR" "$LOG_DIR/locks"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$DEPLOY_LOG"; }

cd "$PROJECT_DIR"

# ── 0. Продукт задан? ───────────────────────────────────────────────────────
# Пустая STACK_BUILD_CMD и отсутствие docker-compose.yml = продукта ещё нет.
# Честный выход нулём, а не имитация выката: харнес ставится на чистый сервер
# раньше первой строки кода продукта, и этот случай — штатный, не ошибка.
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
    mkdir -p "$(dirname "$dst")"
    if ! bash -c "$STACK_DB_DUMP_CMD" > "$dst" 2>>"$DEPLOY_LOG"; then
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

# ── 1а. Секреты не едут в прод (И-3: check-secrets и в pre-commit, и здесь) ──
# Дубль не лишний: pre-commit можно обойти (--no-verify), деплой — нельзя.
"$SCRIPT_DIR/check-secrets.sh" "$PROJECT_DIR" || { say "секреты в репозитории — выкат отменён (И-3)"; exit 1; }

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

# ── 5. Миграции ─────────────────────────────────────────────────────────────
if [ -n "${STACK_MIGRATE_CMD:-}" ]; then
    say "миграции: $STACK_MIGRATE_CMD"
    bash -c "$STACK_MIGRATE_CMD" || { say "миграции упали — стек в неопределённом состоянии, разбирать руками"; exit 1; }
else
    say "STACK_MIGRATE_CMD пуст — миграций у продукта пока нет (честно пропущено)"
fi

# ── 6. Health циклом; провал → авто-откат на :previous ──────────────────────
# Цикл: 30 попыток × (запрос до 5 с + пауза 2 с) — от 60 с до ~210 с (К-3:
# раньше и комментарий, и сообщение врали про «60 с»).
health_ok() {
    local code i
    for i in $(seq 1 30); do
        code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$STACK_HEALTH_URL" || true)
        [ "$code" = "200" ] && return 0
        sleep 2
    done
    return 1
}
if [ -n "${STACK_HEALTH_URL:-}" ]; then
    if health_ok; then
        say "health 200 — выкат жив"
    else
        say "health НЕ поднялся за цикл проверки (30 попыток, 60–210 с)"
        if [ "$ROLLBACK_READY" = 1 ]; then
            say "авто-откат: возвращаю образы :previous"
            for img in $STACK_IMAGES; do
                docker image inspect "$img:previous" >/dev/null 2>&1 && docker tag "$img:previous" "$img:latest"
            done
            if [ -n "${STACK_UP_CMD:-}" ]; then
                bash -c "$STACK_UP_CMD" || say "подъём откатанного стека упал — разбирать руками"
            else
                say "STACK_UP_CMD пуст — образы возвращены, но стек не перезапущен; заполни в harness.conf"
            fi
            # Образы откатились, схема — нет: alembic вперёд не отменяет сам себя.
            # Путь к дампу называется здесь, потому что читать его будут в аварии,
            # а не в спокойный день.
            if [ -n "$DB_DUMP" ]; then
                say "схема базы НЕ откачена автоматически; дамп до миграций: $DB_DUMP"
                # Имя базы и пользователя — из конфига: вшитое имя продукта на
                # чужом проекте дало бы владельцу команду, которая не восстановит.
                say "восстановление руками: pg_restore -U ${DB_ADMIN_USER:-<DB_ADMIN_USER не задан>} -d ${DB_ADMIN_USER:-<DB_ADMIN_USER не задан>} --clean $DB_DUMP"
            fi
            if health_ok; then
                say "откат жив (health 200) — выкат ПРОВАЛЕН, стек на прошлой версии"
            else
                say "откат НЕ ожил — АВАРИЯ, стек лежит"
            fi
        else
            say "отката нет (снимок не делался) — стек лежит, разбирать руками"
        fi
        exit 1
    fi
else
    say "STACK_HEALTH_URL пуст — живость не проверена; выкат без health-проверки успехом НЕ считается, заполни ключ"
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
if [ -n "${STACK_CRON_SRC:-}" ] && [ -f "$PROJECT_DIR/$STACK_CRON_SRC" ]; then
    CRON_DST="/etc/cron.d/$(basename "$STACK_CRON_SRC")"
    if sudo -n cp "$PROJECT_DIR/$STACK_CRON_SRC" "$CRON_DST" 2>/dev/null \
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
say "deploy ok: тег $TAG, health 200, $MAP_NOTE"
