#!/usr/bin/env bash
#
# backup.sh — ежесуточные дампы того, что не восстановить переустановкой.
#
# Что держит: невоспроизводимое состояние сервера. Код переживёт что угодно
# в git-remote, а вот память проекта, handover, паспорт установки и crontab
# живут в одном экземпляре на этой машине — их потеря означает потерю
# контекста агента, и заметили бы её только в момент, когда нужно
# восстанавливаться.
#
# Что дампится:
#   1. $PROJECT_DIR как git bundle (вся история одним файлом) — если это репо;
#   2. память/ + docs/handover/ + STATE.md + dev-map.yaml — tar поверх bundle:
#      bundle несёт только закоммиченное, а память меняется чаще коммитов;
#   3. crontab пользователя агента (расписание демонов — 01-SPEC §9);
#   4. /etc/harness (паспорт установки + пороги механики);
#   5. базы продукта — по списку BACKUP_DBS из harness.conf; пустой список —
#      честная строка в лог (продукт ещё не начат), место под дампы готово.
#
# Откуда взят: собран для стартового пакета по нормам UNIFIED/08 (мягкий
# отказ оставляет след; успех — проверенный, не заявленный).
# Чем доказывается: прогоном на приёмке (01-SPEC §9) — в $BACKUP_DIR
# появляется датированный каталог, bundle раскрывается обратно
# (git clone из bundle), tar листается; ротация — подделкой старого каталога.
# Целостность каждого ночного прогона скрипт проверяет сам: git bundle verify
# и tar -tzf (секунда работы; битый архив — отказ шага, не «бэкап есть»).
#
# Запуск: cron, ночью, от имени $AGENT_USER.
set -euo pipefail

# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/config.sh"
INSTALL_CONF="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-$(konf_koren)/install.conf}}"
HARNESS_CONF="${HARNESS_CONF:-$(konf_koren)/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "backup: нет $INSTALL_CONF — установка не завершена (01-SPEC §0)"; exit 1; }
[ -r "$HARNESS_CONF" ] || { echo "backup: нет $HARNESS_CONF — скопируйте harness/config/harness.conf в $(konf_koren)/"; exit 1; }
# Окружение старше конфига: общий загрузчик вместо голого source (улика
# 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
konf_zagruzit
: "${PROJECT_DIR:?пуст PROJECT_DIR}" "${HEARTBEAT_DIR:?пуст HEARTBEAT_DIR}" "${LOG_DIR:?пуст LOG_DIR}"
: "${BACKUP_DIR:?пуст BACKUP_DIR}" "${BACKUP_KEEP_DAYS:?пуст BACKUP_KEEP_DAYS}"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# Дамп ДОЧИТАН до конца? `pg_dump` в текстовом формате всегда завершает вывод
# строкой «PostgreSQL database dump complete»; её отсутствие значит обрыв —
# кончилось место, упал контейнер, порвалась труба. Обрыв даёт файл, который
# «не пуст» и потому проходил прежнюю проверку `[ -s ]`.
# Улика 10.09.2026 (ревизия харнеса): у git-бэкапа рядом стоит настоящий
# `bundle verify`, у tar — `tar -tzf`, а у базы — только «файл не пустой»;
# восстановление не пробовали ни разу. Инвариант И-1 держался на вере.
дамп_дочитан() { # $1 = файл дампа → rc 0, если конец на месте
    [ -s "$1" ] || return 1
    # Хвоста довольно: маркер стоит последней строкой, а дамп бывает в гигабайты.
    tail -c 4096 "$1" 2>/dev/null | grep -q 'PostgreSQL database dump complete'
}

if [ "${1:-}" = "--selftest" ]; then
    tmp=$(mktemp -d); ok=1
    проба() { # $1=ожидание(да|нет) $2=файл $3=имя случая
        local got=нет
        дамп_дочитан "$2" && got=да
        if [ "$got" = "$1" ]; then printf '  ок    %s\n' "$3"
        else printf '  ПЛОХО %s: ждали «%s», получили «%s»\n' "$3" "$1" "$got"; ok=0; fi
    }
    printf 'CREATE TABLE x();\nCOPY x FROM stdin;\n\\.\n--\n-- PostgreSQL database dump complete\n--\n' > "$tmp/полный.sql"
    # БОЛЬНОЙ СЛУЧАЙ: место кончилось на середине COPY — файл большой, «не пуст», и
    # прежняя проверка звала его бэкапом.
    printf 'CREATE TABLE x();\nCOPY x FROM stdin;\n1\t2\n3\t' > "$tmp/обрезанный.sql"
    : > "$tmp/пустой.sql"
    проба нет "$tmp/обрезанный.sql" "БОЛЬНОЙ СЛУЧАЙ: дамп оборван на середине — не бэкап"
    проба да  "$tmp/полный.sql"     "дамп дочитан до маркера конца — бэкап"
    проба нет "$tmp/пустой.sql"     "пустой файл — не бэкап"
    проба нет "$tmp/нет-такого.sql" "файла нет вовсе — не бэкап"

    # ── БОЛЬНОЙ СЛУЧАЙ 21.09.2026: продукт выключен намеренно ────────────────
    # Владелец велел погасить продукт совсем. Его база тогда не работает,
    # команда дампа падает — и ночной бэкап считал бы это неудачей КАЖДУЮ
    # ночь. Сторож, который кричит всегда, перестаёт быть сторожем.
    # Проба СТРОИТ условие: свой проект, своя команда дампа, которая падает.
    stend="$tmp/стенд"
    mkdir -p "$stend/proekt" "$stend/backup" "$stend/log/locks" "$stend/hb"
    ( cd "$stend/proekt" && git init -q . && echo x > a.txt && git add -A \
      && git -c user.name=t -c user.email=t@t commit -qm проба ) >/dev/null 2>&1
    cat > "$stend/install.conf" <<CONF
PROJECT_DIR="$stend/proekt"
LOG_DIR="$stend/log"
HEARTBEAT_DIR="$stend/hb"
CONF
    cat > "$stend/harness.conf" <<CONF
BACKUP_DIR="$stend/backup"
BACKUP_KEEP_DAYS=14
BACKUP_DBS="проба=false"
CONF
    # `|| true` у вызывающих обязателен: под `set -e` ненулевой код демона
    # (а он ненулевой ровно в проверяемом случае) убил бы сам самотест.
    progon_backup() {  # $1 — путь к состоянию выключения (может не существовать)
        HARNESS_INSTALL_CONF="$stend/install.conf" HARNESS_CONF="$stend/harness.conf" \
        PRODUCT_POWER_STATE="$1" bash "$0" 2>&1
    }

    # Здоровая жизнь: продукт работает, команда дампа упала — это НЕУДАЧА.
    vyvod=$(progon_backup "$stend/нет.json" || true)
    if printf '%s' "$vyvod" | grep -q "НЕ бэкап"; then
        echo "  ок    продукт работает, дамп упал — это неудача, как и было"
    else
        echo "  ПЛОХО упавший дамп перестал быть неудачей"; ok=0
    fi

    # Продукт выключен владельцем: не неудача, но и НЕ молчание.
    printf '{"когда":"2026-09-21T09:00:00","контейнеры":["a"],"сайт":1,"cron":1}' \
        > "$stend/выключен.json"
    vyvod=$(progon_backup "$stend/выключен.json" || true)
    if printf '%s' "$vyvod" | grep -q "ВЫКЛЮЧЕН владельцем"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ 21.09: выключенный продукт назван словами, а не молча пропущен"
    else
        echo "  ПЛОХО про выключенный продукт в журнале ни слова"; ok=0
    fi
    if ! printf '%s' "$vyvod" | grep -q "НЕ бэкап"; then
        echo "  ок    выключенный продукт не считается неудачей бэкапа"
    else
        echo "  ПЛОХО выключенный продукт всё ещё красит бэкап каждую ночь"; ok=0
    fi
    if printf '%s' "$vyvod" | grep -q "ВЫКЛЮЧЕН владельцем (2026-09-21T09:00:00)"; then
        echo "  ок    в журнале названа ДАТА выключения — состояние не безымянно"
    else
        echo "  ПЛОХО дата выключения в журнал не попала"; ok=0
    fi

    rm -rf "$tmp"
    (( ok )) && { echo "SELFTEST: зелёный (8 путей: 4 на проверку дампа, 4 на выключенный продукт)"; exit 0; }
    echo "SELFTEST: КРАСНЫЙ"; exit 1
fi

# Один прогон за раз (Д-10, единый паттерн демонов): cron + ручной запуск не
# должны дампить наперегонки. Лок — сервисный каталог (конвенция К-1б).
LOCK_FILE="$LOG_DIR/locks/${SERVICE_LOCKS_SUBDIR:-services}/backup.lock"
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>>"$LOCK_FILE"
flock -n 9 || { say "прогон уже идёт (лок $LOCK_FILE) — выхожу"; exit 0; }

STAMP=$(date +%Y-%m-%d_%H%M)
DEST="$BACKUP_DIR/$STAMP"
mkdir -p "$DEST"
say "=== дамп начат → $DEST ==="
FAILURES=0

# ── 1. git bundle каталога проекта ──────────────────────────────────────────
if git -C "$PROJECT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    # Целостность — фактом, не заявлением: bundle verify стоит секунду, а
    # битый бэкап, найденный в момент восстановления, стоит всего.
    if git -C "$PROJECT_DIR" bundle create "$DEST/project.bundle" --all 2>/dev/null \
       && git -C "$PROJECT_DIR" bundle verify "$DEST/project.bundle" >/dev/null 2>&1; then
        say "1. git bundle: $(du -h "$DEST/project.bundle" | cut -f1) (bundle verify — OK)"
    else
        # Пустой репозиторий (ни одного коммита) тоже сюда попадает — bundle
        # нечего паковать; след обязателен, падение не нужно.
        say "1. git bundle НЕ создан или не прошёл verify (репо пустое или битое) — смотрите git -C $PROJECT_DIR log"
        rm -f "$DEST/project.bundle"
        FAILURES=$((FAILURES + 1))
    fi
else
    say "1. $PROJECT_DIR не git-репозиторий — bundle пропущен (до git init первой задачи)"
fi

# ── 2. память, handover и живое состояние (не только закоммиченное) ─────────
TAR_LIST=()
for p in "память" "docs/handover" "STATE.md" "dev-map.yaml" "MEMORY.md" "CLAUDE.md"; do
    [ -e "$PROJECT_DIR/$p" ] && TAR_LIST+=("$p")
done
if [ "${#TAR_LIST[@]}" -gt 0 ]; then
    # tar -tzf после сборки: архив обязан листаться, иначе это не бэкап.
    if tar -C "$PROJECT_DIR" -czf "$DEST/state.tar.gz" "${TAR_LIST[@]}" 2>/dev/null \
       && tar -tzf "$DEST/state.tar.gz" >/dev/null 2>&1; then
        say "2. состояние: ${TAR_LIST[*]} → $(du -h "$DEST/state.tar.gz" | cut -f1) (tar -tzf — OK)"
    else
        say "2. tar состояния НЕ собрался или не листается"
        rm -f "$DEST/state.tar.gz"
        FAILURES=$((FAILURES + 1))
    fi
else
    say "2. состояния ещё нет (ни памяти, ни handover) — нормально до первой сессии"
fi

# ── 3. crontab агента ───────────────────────────────────────────────────────
if crontab -l > "$DEST/crontab.txt" 2>/dev/null; then
    say "3. crontab: $(wc -l < "$DEST/crontab.txt") строк"
else
    say "3. crontab пуст или недоступен — расписание демонов не сохранено (01-SPEC §9 ещё не выполнен?)"
    FAILURES=$((FAILURES + 1))
fi

# ── 4. корень установки (конфиги) ──────────────────────────────────────────
# Путь берётся от КОРНЯ: зашитое «/etc/harness» на чужой машине сохраняло бы
# пустоту и отчитывалось «сохранено» (владелец 21.09: «ставить на любую машину»).
KOREN_UST="$(konf_koren)"
if tar -czf "$DEST/etc-harness.tar.gz" -C "$(dirname "$KOREN_UST")" \
        "$(basename "$KOREN_UST")" 2>/dev/null \
   && tar -tzf "$DEST/etc-harness.tar.gz" >/dev/null 2>&1; then
    say "4. $KOREN_UST сохранён (tar -tzf — OK)"
else
    say "4. $KOREN_UST НЕ сохранён или архив не листается"
    rm -f "$DEST/etc-harness.tar.gz"
    FAILURES=$((FAILURES + 1))
fi

# ── 5. базы продукта — по списку из harness.conf ────────────────────────────
# Формат BACKUP_DBS: одна СТРОКА = один дамп «имя=команда» (разделитель
# записей — перевод строки, решение Д-9 задокументировано в harness.conf:
# внутри команды дампа законны и «;», и «|», резать по ним нельзя).
# Команда пишет дамп в stdout, мы кладём его в файл. Появится продукт
# с базой — добавьте строку в harness.conf, код не трогается.
# Продукт может быть ВЫКЛЮЧЕН НАМЕРЕННО (scripts/product-power.sh): тогда его
# контейнер с базой не работает, команда дампа падает, и ночной бэкап считал бы
# это неудачей КАЖДУЮ ночь. Сторож, который кричит всегда, перестаёт быть
# сторожем — его отключают, и вместе с ним исчезает настоящий сигнал.
#
# Молча пропускать тоже нельзя ([[disable-a-dangerous-mechanism-by-data]]: спящий
# механизм выглядит живым). Поэтому строка в журнале называет и состояние, и
# дату выключения, и где лежит последний дамп — тот, что снял сам выключатель
# перед остановкой.
PRODUCT_POWER_STATE="${PRODUCT_POWER_STATE:-$PANEL_STATE_DIR/product-power.json}"
if [ -s "$PRODUCT_POWER_STATE" ]; then
    KOGDA=$(python3 -c "
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding='utf-8')).get('когда', 'дата неизвестна'))
except Exception:
    print('дата нечитаема')
" "$PRODUCT_POWER_STATE" 2>/dev/null || echo "дата нечитаема")
    # `|| true` обязателен: под set -e + pipefail отсутствие файлов роняет
    # весь демон, и бэкап не доходит до ротации ([[failure-under-set-e-is-silent]]).
    POSLEDNIJ=$(ls -1t "$BACKUP_DIR"/produkt-pered-vykluchenim-*.dump 2>/dev/null | head -1 || true)
    say "5. базы продукта не копируем: продукт ВЫКЛЮЧЕН владельцем ($KOGDA)."
    say "   Последний дамп: ${POSLEDNIJ:-не найден — это повод разобраться}"
    say "   Включить обратно: bash scripts/product-power.sh on"
elif [ -n "${BACKUP_DBS:-}" ]; then
    while IFS= read -r entry; do
        entry=$(printf '%s' "$entry" | sed 's/^ *//;s/ *$//')
        [ -n "$entry" ] || continue
        db_name="${entry%%=*}"; db_cmd="${entry#*=}"
        # </dev/null: иначе команда дампа съела бы остальные строки списка.
        if bash -c "$db_cmd" > "$DEST/db-$db_name.dump" 2>/dev/null </dev/null \
           && дамп_дочитан "$DEST/db-$db_name.dump"; then
            say "5. база $db_name: $(du -h "$DEST/db-$db_name.dump" | cut -f1) (маркер конца дампа на месте)"
        elif [ -s "$DEST/db-$db_name.dump" ]; then
            # Не удаляем: обрывок иногда всё, что осталось, — но имя обязано
            # кричать, что восстанавливаться из него нельзя.
            mv "$DEST/db-$db_name.dump" "$DEST/db-$db_name.dump.НЕПОЛНЫЙ"
            say "5. база $db_name: дамп ОБОРВАН (нет маркера конца) — сохранён как .НЕПОЛНЫЙ, это НЕ бэкап"
            FAILURES=$((FAILURES + 1))
        else
            say "5. база $db_name: дамп ПУСТ или команда упала — это НЕ бэкап"
            rm -f "$DEST/db-$db_name.dump"
            FAILURES=$((FAILURES + 1))
        fi
    done <<< "$BACKUP_DBS"
else
    say "5. базы не заданы (BACKUP_DBS пуст) — продукт ещё не начат, место под дампы готово"
fi

# ── ротация ─────────────────────────────────────────────────────────────────
PRUNED=0
while IFS= read -r old; do
    rm -rf "$old"
    PRUNED=$((PRUNED + 1))
done < <(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -mtime "+$BACKUP_KEEP_DAYS" 2>/dev/null)
say "ротация: удалено старых каталогов $PRUNED (храним $BACKUP_KEEP_DAYS дн.)"

# ── итог ────────────────────────────────────────────────────────────────────
TOTAL=$(du -sh "$DEST" 2>/dev/null | cut -f1)
say "=== дамп завершён: $TOTAL в $DEST, отказов шагов: $FAILURES ==="
if [ "$FAILURES" -gt 0 ]; then
    # Полупустой бэкап страховкой не считается: метки нет, heartbeat-watch
    # назовёт демона утром.
    exit 1
fi
touch "$HEARTBEAT_DIR/backup"
