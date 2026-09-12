#!/usr/bin/env bash
#
# proba-vosstanovleniya.sh — поднять свежий дамп прода в ОТДЕЛЬНОЙ базе и
# сверить числа. Отвечает на вопрос, на который «дамп снят» не отвечает:
# встанем ли мы из него.
#
# Замечание владельца 10.09.2026 (П-2 к ревизии): И-1 держался на вере —
# дамп проверялся на «файл не пустой», восстановление не пробовали ни разу.
# Первая живая проба: 204 МБ поднялись за 19 с, 0 ошибок, 48 таблиц.
#
# Имя базы — с префиксом DROPPABLE_DB_PREFIX: только такие умеет убирать
# штатная дверь scripts/ubrat-bazu.sh, и только их сторож даёт удалить.
# Данные прода и стенда не трогаются: заливка идёт в НОВУЮ базу.
#
# Запуск: bash scripts/proba-vosstanovleniya.sh [дамп]
#         (без аргумента берётся самый свежий db-*.dump из $BACKUP_DIR)
set -uo pipefail
INSTALL_CONF="${HARNESS_INSTALL_CONF:-/etc/harness/install.conf}"
HARNESS_CONF="${HARNESS_CONF:-/etc/harness/harness.conf}"
# Конфиги грузятся так, чтобы ОКРУЖЕНИЕ было старше файла: иначе проба с
# подставным путём судит БОЕВУЮ установку (улика 12.09.2026 — команда смены
# модели ушла в рабочую панель агента).
# shellcheck disable=SC1091
# Путь берётся по РЕАЛЬНОМУ файлу (readlink -f): pre-commit подключён в
# .git/hooks симлинком, и dirname дал бы .git/hooks, где библиотеки нет.
# Поймано первым же коммитом после правки, 12.09.2026.
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/konf.sh"
konf_zagruzit PROJECT_DIR LOG_DIR TMUX_SESSION SECRETS_DIR AGENT_START_CMD
: "${BACKUP_DIR:?пуст BACKUP_DIR}" "${LOG_DIR:?пуст LOG_DIR}"
# Префикс ОБЯЗАТЕЛЕН и берётся из конфига: своё умолчание («проба_») не совпало
# бы с тем, что требует штатная дверь ubrat-bazu.sh, и база стала бы вечной
# (ревью 10.09.2026).
: "${DROPPABLE_DB_PREFIX:?в harness.conf пуст DROPPABLE_DB_PREFIX — имя пробной базы задать нечем}"
PREFIX="$DROPPABLE_DB_PREFIX"
# Контейнер и пользователь — из конфига, БЕЗ своих умолчаний: имя контейнера
# продукта, вшитое в механику харнеса, на чужом проекте увело бы пробу в
# несуществующий контейнер, а отказ выглядел бы поломкой пробы, а не установки.
: "${PROBE_DB_CONTAINER:?в harness.conf пуст PROBE_DB_CONTAINER — где поднимать пробную базу, неизвестно}"
: "${PROBE_DB_USER:?в harness.conf пуст PROBE_DB_USER — под кем поднимать пробную базу, неизвестно}"
CONT="$PROBE_DB_CONTAINER"
USER_DB="$PROBE_DB_USER"

# Дамп, уводящий заливку в ЧУЖУЮ базу. `psql < dump` исполняет метакоманды: одна
# строка «\connect app» — и 204 МБ ложатся не в пробную базу, а в ту, что названа
# в дампе. Сегодня дампы снимаются без --create, но BACKUP_DBS меняется строкой
# конфига, и тогда проба сама затрёт живые данные (ревью 10.09.2026, sec-09).
# Признак читается ЗАРАНЕЕ и отдельно от заливки — отказ должен случиться до
# создания базы, а не после первой залитой таблицы.
метакоманды_в_дампе() { # $1=путь дампа → печатает найденные строки, rc=0 если нашлись
    grep -m3 -n -E "^\\\\(connect|c)([[:space:]]|$)|^(CREATE|DROP) DATABASE" "$1" 2>/dev/null
}

if [ "${1:-}" = "--selftest" ]; then
    ok=1
    проба() { # $1=ждём(да|нет) $2=содержимое дампа $3=имя случая
        local f; f=$(mktemp)
        printf '%s\n' "$2" > "$f"
        local got=нет
        метакоманды_в_дампе "$f" >/dev/null 2>&1 && got=да
        rm -f "$f"
        if [ "$got" = "$1" ]; then printf '  ок    %s\n' "$3"
        else printf '  ПЛОХО %s: ждали «%s», получили «%s»\n' "$3" "$1" "$got"; ok=0; fi
    }
    # БОЛЬНОЙ СЛУЧАЙ: дамп с --create переключает базу посреди заливки.
    проба да  'CREATE DATABASE app;'"$(printf '\n')"'\connect app'"$(printf '\n')"'COPY x FROM stdin;' \
        "БОЛЬНОЙ СЛУЧАЙ: дамп с --create уводит заливку в чужую базу"
    проба да  '\c app' "короткая форма метакоманды \\c"
    проба да  'DROP DATABASE app;' "снос базы в теле дампа"
    проба нет 'COPY public.дома (id) FROM stdin;'"$(printf '\n')"'1' \
        "обычный дамп таблиц — метакоманд нет"
    проба нет "-- \\connect упомянут в комментарии" "упоминание в комментарии — не команда"
    проба нет "SELECT 'CREATE DATABASE x';" "текст внутри значения — не команда"
    (( ok )) && { echo "SELFTEST: зелёный (6 путей, первым — больной случай)"; exit 0; }
    echo "SELFTEST: КРАСНЫЙ"; exit 1
fi

DUMP="${1:-$(find "$BACKUP_DIR" -name 'db-*.dump' -type f -printf '%T@ %p\n' 2>/dev/null \
      | sort -nr | head -1 | cut -d' ' -f2-)}"
[ -n "$DUMP" ] && [ -s "$DUMP" ] || { echo "проба: дампа не нашлось в $BACKUP_DIR"; exit 1; }
DB="${PREFIX}restore_$(date +%m%d_%H%M)"

# Проба оставляет за собой базу на 278 МБ, а убрать её может только штатная
# дверь со словом владельца (И-1). Ревью 10.09.2026 поймало рост: три прогона —
# 834 МБ. Пока прежняя проба не убрана, новую не заводим: инструмент не имеет
# права копить то, что сам убрать не вправе.
OLD_PROBES=$(docker exec -i "$CONT" psql -U "$USER_DB" -d postgres -tAc \
    "select count(*) from pg_database where datname like '${PREFIX}restore_%'" 2>/dev/null | tr -d ' ')
if [ "${OLD_PROBES:-0}" -gt 0 ] 2>/dev/null; then
    echo "ОТКАЗ: прежних пробных баз — ${OLD_PROBES}. Убери их штатной дверью:"
    docker exec -i "$CONT" psql -U "$USER_DB" -d postgres -tAc \
        "select '  DB_ADMIN_EXEC=''docker exec -i $CONT'' bash scripts/ubrat-bazu.sh '||datname \
         ||'   ('||pg_size_pretty(pg_database_size(datname))||')' \
         from pg_database where datname like '${PREFIX}restore_%'" 2>/dev/null
    echo "$(date '+%Y-%m-%d %H:%M:%S')  ПРОБА НЕ ПРОВЕДЕНА: место занято прежней пробой"
    exit 1
fi

FOUND=$(метакоманды_в_дампе "$DUMP")
if [ -n "$FOUND" ]; then
    echo "ОТКАЗ: в дампе есть команды смены базы — заливка ушла бы мимо пробной:"
    printf '  %s\n' "$FOUND"
    echo "Сними дамп без --create (или вырежи метакоманды) и повтори."
    echo "$(date '+%Y-%m-%d %H:%M:%S')  ПРОБА НЕ ПРОВЕДЕНА: дамп уводит заливку в чужую базу"
    exit 1
fi

echo "=== проба восстановления: $(date '+%Y-%m-%d %H:%M:%S') ==="
echo "дамп: $DUMP ($(du -h "$DUMP" | cut -f1)) → база $DB в контейнере $CONT"
docker exec -i "$CONT" psql -U "$USER_DB" -d postgres -c "CREATE DATABASE \"$DB\"" \
    || { echo "ОТКАЗ: базу не создать — проба не проведена"; exit 1; }

T0=$(date +%s)
docker exec -i "$CONT" psql -U "$USER_DB" -d "$DB" -v ON_ERROR_STOP=0 < "$DUMP" \
    > /dev/null 2> "$LOG_DIR/проба-восстановления.err"
RC=$?
T1=$(date +%s)
ERRS=$(grep -c '^ERROR' "$LOG_DIR/проба-восстановления.err" 2>/dev/null)
TABLES=$(docker exec -i "$CONT" psql -U "$USER_DB" -d "$DB" -tAc \
    "select count(*) from information_schema.tables where table_schema='public'" 2>/dev/null)
echo "psql rc=$RC, время $((T1 - T0)) с, строк ERROR: ${ERRS:-?}, таблиц поднялось: ${TABLES:-0}"
[ "${ERRS:-1}" != 0 ] && grep '^ERROR' "$LOG_DIR/проба-восстановления.err" | cut -c1-110 | head -3

# Дверь ходит в СУБД через DB_ADMIN_EXEC (у нас это контейнер ПРОДА), а база
# лежит в стенде — команда без указания контейнера не сработала бы вовсе.
echo "ПРОБНАЯ БАЗА: $DB (контейнер $CONT)"
echo "  убрать: DB_ADMIN_EXEC='docker exec -i $CONT' bash scripts/ubrat-bazu.sh $DB"
if [ "$RC" = 0 ] && [ "${ERRS:-1}" = 0 ] && [ "${TABLES:-0}" -gt 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S')  ПРОБА ЗАВЕРШЕНА: дамп восстанавливается (таблиц ${TABLES}, ошибок 0)"
    exit 0
fi
echo "$(date '+%Y-%m-%d %H:%M:%S')  ПРОБА ЗАВЕРШЕНА: дамп НЕ восстановился целиком — это отказ И-1, разбирайся сейчас"
exit 1
