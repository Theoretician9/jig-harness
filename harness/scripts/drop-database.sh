#!/usr/bin/env bash
#
# drop-database.sh — удаление ПРОБНОЙ базы данных под воротами инварианта И-1.
#
# Зачем инструмент, если есть dropdb. Сторож команд (hooks/guard_bash.py)
# отменяет `dropdb` и `drop database` из сессии безусловно и правильно: сам
# по себе агент не имеет права стирать данные владельца. Но проверки
# развёртывания с нуля оставляют после себя пробные базы, и владелец,
# разрешив их убрать, упирался в стену — разрешение сторож читать не умеет,
# он видит только команду. Дыру закрывает то же, чем закрыт запрет ручного
# выката: не отключение сторожа, а единственная законная дверь со своими
# воротами (13.08.2026, живой случай — база app_проба_0813).
#
# Трое ворот, каждые — отказ, а не предупреждение:
#   1. ИМЯ. Удаляется только база с префиксом DROPPABLE_DB_PREFIX. Боевая
#      база физически не может быть названа в аргументе — опечатка отбивается
#      раньше, чем что-либо произойдёт.
#   2. СОГЛАСИЕ. В $LOG_DIR/consent-buttons.jsonl должно лежать нажатое «Да» кнопкой
#      на ЭТУ базу (его пишет tg-dispatcher), свежее срока кнопки. Текст владельца дверь
#      не открывает. Нет кнопки — ворота сами просят её у владельца (слово владельца
#      05.11.2026: «любое „да“ — кнопками»).
#   3. ДАМП. Перед удалением снимается pg_dump в $BACKUP_DIR и проверяется на
#      непустоту. И-1 требует свежий бэкап перед необратимым — здесь он
#      делается прямо сейчас, поэтому операция перестаёт быть необратимой.
#
# Имена переменных — латиницей: кириллические имена bash не принимает вовсе
# (грабля 11.08.2026, `shellcheck -S error`).
#
# Запуск:  bash scripts/drop-database.sh <имя_базы>
# Самотест: bash scripts/drop-database.sh --selftest   (заводит свою базу и
#           проверяет исходы: без согласия — отказ, чужое имя — отказ,
#           с согласием — дамп снят и база удалена)
set -euo pipefail
export LC_ALL=C.UTF-8

source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/config.sh"
INSTALL_CONF="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-$(konf_koren)/install.conf}}"
HARNESS_CONF="${HARNESS_CONF:-$(konf_koren)/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "убрать-базу: нет $INSTALL_CONF — установка не завершена"; exit 1; }
# Конфиги грузятся так, чтобы ОКРУЖЕНИЕ было старше файла: иначе проба с
# подставным путём судит БОЕВУЮ установку (улика 12.09.2026 — команда смены
# модели ушла в рабочую панель агента).
# shellcheck disable=SC1091
# Путь берётся по РЕАЛЬНОМУ файлу (readlink -f): pre-commit подключён в
# .git/hooks симлинком, и dirname дал бы .git/hooks, где библиотеки нет.
# Поймано первым же коммитом после правки, 12.09.2026.
konf_zagruzit PROJECT_DIR LOG_DIR TMUX_SESSION SECRETS_DIR AGENT_START_CMD

BACKUP_DIR="${BACKUP_DIR:-/var/backups/harness}"
CONFIRM_MAX_AGE_MIN="${CONFIRM_MAX_AGE_MIN:-60}"
DROPPABLE_DB_PREFIX="${DROPPABLE_DB_PREFIX:-}"
DB_ADMIN_EXEC="${DB_ADMIN_EXEC:-}"
DB_ADMIN_USER="${DB_ADMIN_USER:-postgres}"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# ── ворота 2: согласие владельца — ЧУЖИМ правилом, не своим ─────────────────
# Правило ОДНО и живёт в deploy_guard.py (owner_word.судить_кнопкой): предмет согласия,
# срок, расход («одно да — одно действие»: гасит его след db-drops.jsonl) и отзыв.
# Комментарий о единственности, не подкреплённый вызовом, — это просьба помнить, а не
# механизм. --просить: нет кнопки — ворота просят её сами; каталог журналов — явно.
consent() { # $1=база
    local scripts="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/scripts"
    if [ ! -r "$scripts/deploy_guard.py" ]; then
        say "ворот согласия нет на месте ($scripts/deploy_guard.py) — удаление без них не делаю"
        return 1
    fi
    HARNESS_LOG_DIR="$LOG_DIR" python3 "$scripts/deploy_guard.py" --действие dropdb --база "$1" --только-согласие --просить
}

drop_db() {
    local db="$1" dump ok

    [ -n "$DROPPABLE_DB_PREFIX" ] || {
        echo "убрать-базу: DROPPABLE_DB_PREFIX не задан в harness.conf — удалять нечего и нечем."
        echo "  Пока префикс пуст, инструмент не удаляет НИЧЕГО: пустой префикс совпал бы с любым именем."
        return 1
    }
    case "$db" in
        "$DROPPABLE_DB_PREFIX"*) ;;
        *)
            echo "убрать-базу: «$db» не начинается на «$DROPPABLE_DB_PREFIX» — это не пробная база (И-1)."
            echo "  Инструмент трогает только пробные. Боевую базу удаляет владелец руками и осознанно."
            return 1
            ;;
    esac
    [ -n "$DB_ADMIN_EXEC" ] || { echo "убрать-базу: DB_ADMIN_EXEC не задан в harness.conf — нечем ходить в СУБД"; return 1; }

    if ! ok=$(consent "$db"); then
        echo "убрать-базу: нет согласия владельца — нужна кнопка «Да» в Telegram, текст не засчитывается (И-1)."
        printf '%s\n' "$ok" | sed 's/^/  /'
        return 1
    fi
    say "$ok"

    mkdir -p "$BACKUP_DIR"
    dump="$BACKUP_DIR/$db-$(date +%Y%m%d-%H%M%S).sql"
    say "снимаю дамп перед удалением: $dump"
    if ! $DB_ADMIN_EXEC pg_dump -U "$DB_ADMIN_USER" "$db" > "$dump" 2>"$dump.err"; then
        echo "убрать-базу: pg_dump не отработал — база НЕ тронута. Причина:"; cat "$dump.err"
        rm -f "$dump" "$dump.err"
        return 1
    fi
    rm -f "$dump.err"
    # Пустой дамп — не бэкап. Даже схема без данных даёт сотни байт; ноль
    # означает, что pg_dump промолчал об ошибке, а мы бы удаляли вслепую.
    if [ ! -s "$dump" ]; then
        echo "убрать-базу: дамп пуст — база НЕ тронута (И-1: без бэкапа необратимого не делаем)"
        rm -f "$dump"
        return 1
    fi
    say "дамп снят: $(wc -c < "$dump") байт"

    printf 'DROP DATABASE "%s";\n' "$db" | $DB_ADMIN_EXEC psql -U "$DB_ADMIN_USER" -d postgres -v ON_ERROR_STOP=1 -q
    # Проверяем ФАКТ, а не код возврата: «команда прошла» и «базы больше нет» —
    # разные утверждения, и владельцу мы отчитываемся вторым.
    if $DB_ADMIN_EXEC psql -U "$DB_ADMIN_USER" -d postgres -tAc \
        "select 1 from pg_database where datname = '$db'" | grep -q 1; then
        echo "убрать-базу: база «$db» на месте после удаления — считать сделанным нельзя"
        return 1
    fi
    say "база «$db» удалена, дамп остался в $dump"
    printf '{"ts":"%s","база":"%s","дамп":"%s","согласие":"%s"}\n' \
        "$(date -Is)" "$db" "$dump" "$(printf '%s' "${ok//\"/}" | tr '\n' ' ')" >> "$LOG_DIR/db-drops.jsonl"
}

# ── самотест: своя база, все исходы ─────────────────────────────────────────
# Условие проверка создаёт сама: заводит пробную базу с настоящим префиксом,
# сначала пробует убрать её БЕЗ согласия (обязан быть отказ и база на месте),
# потом с согласием — но под чужим именем, и лишь затем по-настоящему.
if [ "${1:-}" = "--selftest" ]; then
    [ -n "$DB_ADMIN_EXEC" ] || { echo "САМОТЕСТ ПРОПУЩЕН: DB_ADMIN_EXEC не задан — СУБД в этой установке не описана"; exit 0; }
    T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
    PROBE="${DROPPABLE_DB_PREFIX}самотест_$$"
    LOG_DIR="$T"; BACKUP_DIR="$T/backup"
    # Журнал кнопок — свой каталог $T: подмена каталога разрешена только ключом стенда.
    export HARNESS_OWNER_WORD_STEND=1
    : > "$T/inbox.jsonl"

    # «Да» кнопкой, как его пишет диспетчер: «отправлено» и решение с одним nonce.
    button_press() { # $1=consent.yes|consent.no $2=база
        local nonce="selftest$RANDOM$RANDOM" ts
        ts=$(date -Is)
        printf '{"v":1,"ts":"%s","src":"selftest","event":"consent.sent","outcome":"ok","data":{"nonce":"%s","вид":"снос-базы","цель":"%s","message_id":4242,"from_id":null}}\n' \
            "$ts" "$nonce" "$2" >> "$T/consent-buttons.jsonl"
        printf '{"v":1,"ts":"%s","src":"selftest","event":"%s","outcome":"ok","data":{"nonce":"%s","вид":"снос-базы","цель":"%s","message_id":4242,"from_id":777000}}\n' \
            "$ts" "$1" "$nonce" "$2" >> "$T/consent-buttons.jsonl"
    }
    db_still_there() {
        $DB_ADMIN_EXEC psql -U "$DB_ADMIN_USER" -d postgres -tAc \
            "select 1 from pg_database where datname = '$PROBE'" | grep -q 1
    }

    $DB_ADMIN_EXEC psql -U "$DB_ADMIN_USER" -d postgres -q -c "CREATE DATABASE \"$PROBE\"" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: не удалось завести пробную базу"; exit 1; }

    REFUSAL_TEXT=$(drop_db "$PROBE" 2>&1) && {
        echo "САМОТЕСТ ПРОВАЛЕН: база удалена БЕЗ согласия владельца — ворота И-1 не держат"
        exit 1
    }
    db_still_there || { echo "САМОТЕСТ ПРОВАЛЕН: отказ отказом, а база исчезла"; exit 1; }
    grep -q "кнопк" <<< "$REFUSAL_TEXT" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: отказ не называет кнопку: ${REFUSAL_TEXT:0:160}"; exit 1; }
    # Дверь сама просит кнопку: ровно один запрос этого вида на эту базу, повтор не плодит.
    drop_db "$PROBE" >/dev/null 2>&1 || true
    [ "$(grep -c "\"цель\": *\"$PROBE\"" "$T/consent-requests.jsonl" 2>/dev/null || true)" = 1 ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: запрос кнопки на базу не один"; exit 1; }

    # Текст владельца не открывает: слово «да» в журнале входящих базу не сносит.
    printf '{"ts":"%s","message_id":"selftest-raw","chat_id":"selftest","text":"да, удаляй"}\n' \
        "$(date -Is)" >> "$T/inbox.jsonl"
    if drop_db "$PROBE" >/dev/null 2>&1; then
        echo "САМОТЕСТ ПРОВАЛЕН: база удалена по тексту владельца — открывает только кнопка"
        exit 1
    fi
    db_still_there || { echo "САМОТЕСТ ПРОВАЛЕН: отказ отказом, а база исчезла"; exit 1; }

    # Чужое имя при живой кнопке — тоже отказ: ворота имени и ворота
    # согласия должны держать по отдельности, иначе одно прикрывает другое.
    sleep 1.1
    button_press consent.yes "postgres"
    if drop_db "postgres" >/dev/null 2>&1; then
        echo "САМОТЕСТ ПРОВАЛЕН: удалена база без пробного префикса"
        exit 1
    fi
    # «Да» на другую базу эту не открывает.
    button_press consent.yes "${DROPPABLE_DB_PREFIX}другая"
    if drop_db "$PROBE" >/dev/null 2>&1; then
        echo "САМОТЕСТ ПРОВАЛЕН: «Да» на другую базу открыло эту"
        exit 1
    fi
    button_press consent.yes "$PROBE"

    drop_db "$PROBE" >/dev/null || { echo "САМОТЕСТ ПРОВАЛЕН: с согласием удаление не прошло"; exit 1; }
    ls "$T/backup/$PROBE-"*.sql >/dev/null 2>&1 \
        || { echo "САМОТЕСТ ПРОВАЛЕН: дампа перед удалением нет"; exit 1; }
    grep -q "\"база\":\"$PROBE\"" "$T/db-drops.jsonl" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: событие не записано в db-drops.jsonl"; exit 1; }
    echo "САМОТЕСТ ПРОЙДЕН: без кнопки — отказ, база цела, кнопка запрошена один раз; текст"
    echo "владельца — отказ; чужое имя и «Да» на другую базу — отказ;"
    echo "с кнопкой «Да» — дамп снят, база удалена, событие записано"
    exit 0
fi

[ $# -eq 1 ] || { echo "запуск: bash scripts/drop-database.sh <имя_базы>   (или --selftest)"; exit 1; }
drop_db "$1"
