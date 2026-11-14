#!/usr/bin/env bash
#
# test_restore_probe_pick_dump.sh — проба: restore-probe.sh без довода
# заливает в psql самый свежий дамп PostgreSQL, а не самый свежий db-*.dump
# любого движка (триаж-46, №9, остаток).
#
# Зачем. BACKUP_DBS снимает рядом базы PostgreSQL и MySQL; дамп MySQL ложится
# в ту же ночную копию позже и потому свежее. Проба брала его, psql давился
# синтаксисом MySQL, и целая копия объявлялась невосстановимой — ложный отказ
# И-1 и ложная тревога владельцу.
#
# Судится то, что psql ПОЛУЧИЛ на вход заливки, а не строка вывода: заглушка
# docker в PATH пробы пишет stdin `psql … ON_ERROR_STOP` в файл. Вид дампа
# узнаётся по шапке («-- PostgreSQL database dump», «-- MySQL dump»,
# «-- MariaDB dump») — так же, как его узнаёт backup.sh (DUMP_END_MARKERS).
#
# Стенд свой: корень установки (HARNESS_HOME), install.conf, harness.conf,
# каталог копий, журналы, метки, заглушка docker, заглушка двери тревоги.
# Механизм копируется в стенд рядом с lib/config.sh, дверь подмены пути —
# HARNESS_RESTORE_PROBE_SH (мутированная копия). Ни базы, ни docker машины,
# ни живых копий проба не касается.
#
#   bash scripts/test_restore_probe_pick_dump.sh
# Код возврата: 0 — зелёная, 1 — красная.
set -uo pipefail
export LC_ALL=C.UTF-8

KOREN="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
MECHANISM="${HARNESS_RESTORE_PROBE_SH:-$KOREN/scripts/restore-probe.sh}"
[ -r "$MECHANISM" ] || { echo "ПРОБА: нет механизма $MECHANISM"; echo "ПРОБА: КРАСНАЯ"; exit 1; }

# shellcheck disable=SC1091
source "$KOREN/scripts/lib/selftest_cases.sh"

T=$(mktemp -d -t proba-restore-pick-dump-XXXXXX)
trap 'rm -rf "$T"' EXIT

mkdir -p "$T/h/scripts/lib" "$T/bin" "$T/proekt"
cp "$MECHANISM" "$T/h/scripts/restore-probe.sh"
cp "$KOREN/scripts/lib/config.sh" "$T/h/scripts/lib/config.sh"
RP="$T/h/scripts/restore-probe.sh"

# Заглушка docker: своего контейнера «поднимает» сразу, готовность отвечает,
# таблиц после заливки — 1. Вход заливки (stdin psql с ON_ERROR_STOP) — в $FED.
cat > "$T/bin/docker" <<'STUB'
#!/usr/bin/env bash
case "$1" in
    inspect) exit 1 ;;
    logs) echo "PostgreSQL init process complete; ready for start up." ;;
    run) echo stubid ;;
    exec)
        case "$*" in
            *ON_ERROR_STOP*) cat > "$FED" ;;
            *information_schema*) echo 1 ;;
        esac ;;
esac
exit 0
STUB
printf '#!/usr/bin/env bash\nexit 0\n' > "$T/bin/sender.sh"
chmod +x "$T/bin/docker" "$T/bin/sender.sh"

# Шапки — как у настоящих дамперов; в теле каждого дампа его метка, по ней
# проба узнаёт, ЧТО ушло в psql.
pg_dump_file() { # $1 = путь, $2 = метка
    mkdir -p "$(dirname "$1")"
    printf -- '--\n-- PostgreSQL database dump\n--\n\n-- Dumped from database version 16.4\n-- Dumped by pg_dump version 16.4\n\nCREATE TABLE %s (a int);\n\n--\n-- PostgreSQL database dump complete\n--\n' "$2" > "$1"
}
my_dump_file() { # $1 = путь, $2 = метка, $3 = движок (MySQL|MariaDB)
    mkdir -p "$(dirname "$1")"
    printf -- '-- %s dump 10.13  Distrib 8.0.36\n--\n-- Host: localhost    Database: shop\n\nCREATE TABLE `%s` (id int) ENGINE=InnoDB;\n\n-- Dump completed on 2026-11-14  3:00:01\n' "$3" "$2" > "$1"
}

# Стенд: $1 = имя. Печатает путь стенда; каталог копий — <стенд>/kopii.
stand() {
    local s="$T/$1"
    mkdir -p "$s/home" "$s/log" "$s/hb" "$s/kopii"
    cat > "$s/home/install.conf" <<CONF
PROJECT_DIR="$T/proekt"
LOG_DIR="$s/log"
HEARTBEAT_DIR="$s/hb"
CONF
    cat > "$s/home/harness.conf" <<CONF
BACKUP_DIR="$s/kopii"
BACKUP_DBS=""
DROPPABLE_DB_PREFIX="proba_"
PROBE_DB_CONTAINER=""
PROBE_DB_USER=""
PROBE_DB_IMAGE="harness-restore-selftest.invalid/pg:0"
PROBE_DB_IMAGE_TEMPLATE=""
CONF
    printf '%s' "$s"
}

# Прогон пробы восстановления на стенде $1, остальное — доводы. Итог: OUT, RC;
# что получил psql — в FED_TEXT (пусто, если заливки не было).
run_rp() {
    local s="$1"; shift
    rm -f "$s/fed"
    OUT=$(env -i PATH="$T/bin:$PATH" HOME="$s/home" LC_ALL=C.UTF-8 HARNESS_HOME="$s/home" \
          FED="$s/fed" RESTORE_PROBE_SENDER="$T/bin/sender.sh" \
          timeout 120 bash "$RP" --без-отправки "$@" 2>&1 </dev/null)
    RC=$?
    FED_TEXT=$(cat "$s/fed" 2>/dev/null)
}
fed_label() { # что ушло в psql → метка дампа или «ничего»
    local m
    m=$(printf '%s' "$FED_TEXT" | sed -n 's/^CREATE TABLE `\{0,1\}\([a-z0-9_]*\)`\{0,1\} .*/\1/p' | head -1)
    printf '%s' "${m:-ничего}"
}
show_out() { printf '%s\n' "$OUT" | tail -5 | sed 's/^/        | /'; }

echo "--- выбор дампа пробой восстановления ($MECHANISM)"

# 1. БОЛЬНОЙ: в одной ночной копии дамп PostgreSQL и дамп MySQL, MySQL снят
# позже (BACKUP_DBS идёт по строкам) — в psql должен уйти PostgreSQL.
S=$(stand s1)
pg_dump_file "$S/kopii/2026-11-14_0300/db-core.dump" pg_core
my_dump_file "$S/kopii/2026-11-14_0300/db-legacy.dump" my_legacy MySQL
touch -d '2026-11-14 03:00:10' "$S/kopii/2026-11-14_0300/db-core.dump"
touch -d '2026-11-14 03:00:50' "$S/kopii/2026-11-14_0300/db-legacy.dump"
run_rp "$S"
case_check "БОЛЬНОЙ: рядом свежий MySQL-дамп — psql получил дамп PostgreSQL" \
    "в psql: pg_core; путь назван: да" \
    "в psql: $(fed_label); путь назван: $(printf '%s' "$OUT" | grep -q '^дамп: .*db-core\.dump' && echo да || echo нет)"
[ "$(fed_label)" = pg_core ] || show_out

# 2. БОЛЬНОЙ: из нескольких PostgreSQL-дампов — САМЫЙ СВЕЖИЙ, даже когда
# MySQL и MariaDB ещё свежее. Выбор «любой PG» или «старший PG» здесь виден.
S=$(stand s2)
pg_dump_file "$S/kopii/2026-11-12_0300/db-core.dump" pg_old
pg_dump_file "$S/kopii/2026-11-13_0300/db-core.dump" pg_new
my_dump_file "$S/kopii/2026-11-13_0300/db-legacy.dump" my_legacy MySQL
my_dump_file "$S/kopii/2026-11-13_0300/db-shop.dump" maria_shop MariaDB
touch -d '2026-11-12 03:00:10' "$S/kopii/2026-11-12_0300/db-core.dump"
touch -d '2026-11-13 03:00:10' "$S/kopii/2026-11-13_0300/db-core.dump"
touch -d '2026-11-13 03:00:40' "$S/kopii/2026-11-13_0300/db-legacy.dump"
touch -d '2026-11-13 03:00:50' "$S/kopii/2026-11-13_0300/db-shop.dump"
run_rp "$S"
case_check "БОЛЬНОЙ: два PG-дампа и свежее их MySQL и MariaDB — psql получил самый свежий PG" \
    "pg_new" "$(fed_label)"

# 3. БОЛЬНОЙ: дампов PostgreSQL нет вовсе, только MySQL — в psql не уходит
# ничего: заливка MySQL в psql — это ложный отказ, а не проверка.
S=$(stand s3)
my_dump_file "$S/kopii/2026-11-14_0300/db-legacy.dump" my_legacy MySQL
run_rp "$S"
case_check "БОЛЬНОЙ: только MySQL-дамп — psql его не получил, «восстанавливается» не объявлено" \
    "в psql: ничего; ПРОБА ЗАВЕРШЕНА: нет" \
    "в psql: $(fed_label); ПРОБА ЗАВЕРШЕНА: $(printf '%s' "$OUT" | grep -q 'ПРОБА ЗАВЕРШЕНА: дамп восстанавливается' && echo да || echo нет)"

# 4. Контроль: явный довод сильнее выбора — заливается названный дамп, даже
# если есть свежее PG. Без этого случая «выбор PG» мог бы съесть довод.
S=$(stand s4)
pg_dump_file "$S/kopii/2026-11-12_0300/db-core.dump" pg_old
pg_dump_file "$S/kopii/2026-11-13_0300/db-core.dump" pg_new
touch -d '2026-11-12 03:00:10' "$S/kopii/2026-11-12_0300/db-core.dump"
touch -d '2026-11-13 03:00:10' "$S/kopii/2026-11-13_0300/db-core.dump"
run_rp "$S" "$S/kopii/2026-11-12_0300/db-core.dump"
case_check "контроль: явный довод — заливается названный дамп, а не свежий" \
    "pg_old" "$(fed_label)"

# 5. Контроль стенда: один PG-дамп — заливка дошла и объявлена (иначе случаи
# выше судили бы стенд, где psql не зовётся вовсе).
S=$(stand s5)
pg_dump_file "$S/kopii/2026-11-14_0300/db-core.dump" pg_core
run_rp "$S"
case_check "контроль стенда: единственный PG-дамп залит, проба завершена с кодом 0" \
    "в psql: pg_core; код 0" "в psql: $(fed_label); код $RC"
[ "$RC" = 0 ] || show_out

if [ "$CASES_FAILED" = 0 ]; then
    echo "ПРОБА: ЗЕЛЁНАЯ ($CASES_PATHS путей)"; exit 0
fi
echo "ПРОБА: КРАСНАЯ ($CASES_FAILED из $CASES_PATHS)"; exit 1
