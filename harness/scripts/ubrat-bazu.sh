#!/usr/bin/env bash
#
# ubrat-bazu.sh — удаление ПРОБНОЙ базы данных под воротами инварианта И-1.
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
#   2. СОГЛАСИЕ. В $LOG_DIR/confirmations.jsonl должна лежать запись владельца
#      свежее CONFIRM_MAX_AGE_MIN (её пишет tg-dispatcher на «да»/«ок»/
#      «подтверждаю»). Нет записи — нет действия (решение владельца 08.08.2026).
#   3. ДАМП. Перед удалением снимается pg_dump в $BACKUP_DIR и проверяется на
#      непустоту. И-1 требует свежий бэкап перед необратимым — здесь он
#      делается прямо сейчас, поэтому операция перестаёт быть необратимой.
#
# Имена переменных — латиницей: кириллические имена bash не принимает вовсе
# (грабля 11.08.2026, `shellcheck -S error`).
#
# Запуск:  bash scripts/ubrat-bazu.sh <имя_базы>
# Самотест: bash scripts/ubrat-bazu.sh --selftest   (заводит свою базу и
#           проверяет исходы: без согласия — отказ, чужое имя — отказ,
#           с согласием — дамп снят и база удалена)
set -euo pipefail
export LC_ALL=C.UTF-8

INSTALL_CONF="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-/etc/harness/install.conf}}"
HARNESS_CONF="${HARNESS_CONF:-/etc/harness/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "убрать-базу: нет $INSTALL_CONF — установка не завершена"; exit 1; }
# Конфиги грузятся так, чтобы ОКРУЖЕНИЕ было старше файла: иначе проба с
# подставным путём судит БОЕВУЮ установку (улика 12.09.2026 — команда смены
# модели ушла в рабочую панель агента).
# shellcheck disable=SC1091
# Путь берётся по РЕАЛЬНОМУ файлу (readlink -f): pre-commit подключён в
# .git/hooks симлинком, и dirname дал бы .git/hooks, где библиотеки нет.
# Поймано первым же коммитом после правки, 12.09.2026.
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/konf.sh"
konf_zagruzit PROJECT_DIR LOG_DIR TMUX_SESSION SECRETS_DIR AGENT_START_CMD

LOG_DIR="${LOG_DIR:-/var/log/harness}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/harness}"
CONFIRM_MAX_AGE_MIN="${CONFIRM_MAX_AGE_MIN:-60}"
DROPPABLE_DB_PREFIX="${DROPPABLE_DB_PREFIX:-}"
DB_ADMIN_EXEC="${DB_ADMIN_EXEC:-}"
DB_ADMIN_USER="${DB_ADMIN_USER:-postgres}"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# ── ворота 2: согласие владельца — ЧУЖИМ правилом, не своим ─────────────────
# Прежде здесь жила своя копия правила образца до 20.08: список из четырёх слов
# без предмета, без отрицаний, без расхода. Комментарий над ней утверждал
# «читает тем же правилом, что ворота выката» — а правила разошлись в обе
# стороны (ревью 11.09.2026): «моё да» ворота выката принимали, а эта дверь
# нет; и наоборот — ЛЮБОЕ «да», сказанное за час на любой вопрос, открывало
# удаление базы, потому что предмета копия не проверяла вовсе.
#
# Теперь правило ОДНО и живёт в deploy_guard.py: там же предмет согласия,
# расход («одно да — одно действие») и отзыв. Комментарий о единственности,
# не подкреплённый вызовом, — это просьба помнить, а не механизм.
consent() {
    local guard="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/deploy_guard.py"
    if [ ! -r "$guard" ]; then
        say "ворот согласия нет на месте ($guard) — удаление без них не делаю"
        return 1
    fi
    HARNESS_LOG_DIR="$LOG_DIR" python3 "$guard" --действие dropdb --только-согласие
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

    if ! ok=$(consent); then
        echo "убрать-базу: нет согласия владельца свежее $CONFIRM_MAX_AGE_MIN мин в $LOG_DIR/confirmations.jsonl (И-1)."
        echo "  Спроси в канале и дождись ответа «да» — диспетчер запишет его сам."
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
        "$(date -Is)" "$db" "$dump" "${ok//\"/}" >> "$LOG_DIR/db-drops.jsonl"
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
    : > "$T/confirmations.jsonl"

    $DB_ADMIN_EXEC psql -U "$DB_ADMIN_USER" -d postgres -q -c "CREATE DATABASE \"$PROBE\"" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: не удалось завести пробную базу"; exit 1; }

    if drop_db "$PROBE" >/dev/null 2>&1; then
        echo "САМОТЕСТ ПРОВАЛЕН: база удалена БЕЗ согласия владельца — ворота И-1 не держат"
        exit 1
    fi
    $DB_ADMIN_EXEC psql -U "$DB_ADMIN_USER" -d postgres -tAc \
        "select 1 from pg_database where datname = '$PROBE'" | grep -q 1 \
        || { echo "САМОТЕСТ ПРОВАЛЕН: отказ отказом, а база исчезла"; exit 1; }

    # Чужое имя при живом согласии — тоже отказ: ворота имени и ворота
    # согласия должны держать по отдельности, иначе одно прикрывает другое.
    printf '{"ts":"%s","text":"да","message_id":"selftest"}\n' "$(date -Is)" >> "$T/confirmations.jsonl"
    if drop_db "postgres" >/dev/null 2>&1; then
        echo "САМОТЕСТ ПРОВАЛЕН: удалена база без пробного префикса"
        exit 1
    fi

    drop_db "$PROBE" >/dev/null || { echo "САМОТЕСТ ПРОВАЛЕН: с согласием удаление не прошло"; exit 1; }
    ls "$T/backup/$PROBE-"*.sql >/dev/null 2>&1 \
        || { echo "САМОТЕСТ ПРОВАЛЕН: дампа перед удалением нет"; exit 1; }
    grep -q "\"база\":\"$PROBE\"" "$T/db-drops.jsonl" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: событие не записано в db-drops.jsonl"; exit 1; }
    echo "САМОТЕСТ ПРОЙДЕН: без согласия — отказ и база цела; чужое имя — отказ;"
    echo "с согласием — дамп снят, база удалена, событие записано"
    exit 0
fi

[ $# -eq 1 ] || { echo "запуск: bash scripts/ubrat-bazu.sh <имя_базы>   (или --selftest)"; exit 1; }
drop_db "$1"
