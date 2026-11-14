#!/usr/bin/env bash
#
# test_backup_dumps_into.sh — проба двух свойств backup.sh, перенесённых из
# опыта копии Content-Factory (триаж-46, №9-10, D3):
#
#   1. права копий не зависят от umask вызывающего: каталоги 700, файлы 600
#      (дампы несут данные покупателей, а cron и ручной запуск ходят с 022);
#   2. `backup.sh --базы-в <каталог>` — дампы ВСЕХ баз из BACKUP_DBS для двери
#      выката: код ≠ 0 при любой неудаче (выкат не идёт без копии), ничего из
#      ночной работы; ночной прогон держит в $BACKUP_DIR/перед-выкатом пять
#      новейших каталогов ПО ИМЕНИ и не зовёт этот каталог чужим.
#
# Стенд целиком свой во временном каталоге: install.conf, harness.conf, корень
# установки (HARNESS_HOME), LOG_DIR с локом, BACKUP_DIR, HEARTBEAT_DIR, проект,
# свой crontab в PATH. «Базы» — файлы стенда, команда дампа — `cat` из
# BACKUP_DBS. Механизм зовётся под `env -i`: окружение старше конфига
# (konf_zagruzit), и унаследованный BACKUP_DIR увёл бы прогон в живые копии.
# Механизм копируется в стенд рядом с config.sh: он ищет загрузчик от своего
# пути, поэтому дверь подмены HARNESS_BACKUP_SH (путь мутированной копии)
# работает из любого каталога.
#
#   bash scripts/test_backup_dumps_into.sh
# Код возврата: 0 — зелёная, 1 — красная.
set -uo pipefail
export LC_ALL=C.UTF-8

KOREN="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
MECHANISM="${HARNESS_BACKUP_SH:-$KOREN/harness/demons/backup.sh}"
[ -r "$MECHANISM" ] || { echo "ПРОБА: нет механизма $MECHANISM"; echo "ПРОБА: КРАСНАЯ"; exit 1; }

# shellcheck disable=SC1091
source "$KOREN/scripts/lib/selftest_cases.sh"

T=$(mktemp -d -t proba-backup-dumps-into-XXXXXX)
LOCK_HOLDER=""
cleanup() {
    if [ -n "$LOCK_HOLDER" ]; then
        pkill -P "$LOCK_HOLDER" 2>/dev/null
        kill "$LOCK_HOLDER" 2>/dev/null
    fi
    rm -rf "$T"
}
trap cleanup EXIT

mkdir -p "$T/h/harness/demons" "$T/h/scripts/lib" "$T/bin" "$T/src"
cp "$MECHANISM" "$T/h/harness/demons/backup.sh"
cp "$KOREN/scripts/lib/config.sh" "$T/h/scripts/lib/config.sh"
BS="$T/h/harness/demons/backup.sh"

# Свой crontab: живой `crontab -l` положил бы расписание машины в стенд.
printf '#!/usr/bin/env bash\necho "# расписание стенда"\n' > "$T/bin/crontab"
chmod +x "$T/bin/crontab"

printf -- '--\n-- PostgreSQL database dump\n--\nCREATE TABLE x();\nCOPY x FROM stdin;\n\\.\n--\n-- PostgreSQL database dump complete\n--\n' \
    > "$T/src/pg.dump"
printf -- '-- MySQL dump 10.13  Distrib 8.0.36\nCREATE TABLE `x` (id int);\nINSERT INTO `x` VALUES (1);\n\n-- Dump completed on 2026-11-09  3:00:01\n' \
    > "$T/src/mysql.dump"
printf -- '--\n-- PostgreSQL database dump\n--\nCREATE TABLE x();\nCOPY x FROM stdin;\n1\t2\n3\t' > "$T/src/pg-cut.dump"

# Стенд: $1 = имя; $2, $3, $4 = команды баз alfa, beta, gamma (пусто — целые
# дампы pg, mysql, pg). $5 = «пусто» — BACKUP_DBS пуст. Печатает путь стенда.
# BACKUP_DIR НЕ создаётся заранее: его права — тоже дело механизма.
stand() {
    local s="$T/$1" dbs
    mkdir -p "$s/home" "$s/log" "$s/hb" "$s/proekt/память"
    # Проект — настоящий репозиторий с памятью: ночной путь снимает с него
    # bundle и tar, и проба прав судит все виды файлов копии.
    ( cd "$s/proekt" && git init -q . && echo x > a.txt && echo m > память/m.md \
      && git add -A && git -c user.name=t -c user.email=t@t commit -qm проба ) >/dev/null 2>&1
    cat > "$s/install.conf" <<CONF
PROJECT_DIR="$s/proekt"
LOG_DIR="$s/log"
HEARTBEAT_DIR="$s/hb"
CONF
    dbs="alfa=${2:-cat $T/src/pg.dump}
beta=${3:-cat $T/src/mysql.dump}
gamma=${4:-cat $T/src/pg.dump}"
    [ "${5:-}" = пусто ] && dbs=""
    cat > "$s/harness.conf" <<CONF
BACKUP_DIR="$s/backup"
BACKUP_KEEP_DAYS=14
SERVICE_LOCKS_SUBDIR="services"
BACKUP_DBS="$dbs"
CONF
    printf '%s' "$s"
}

# Прогон механизма на стенде $1, остальное — доводы. Итог в OUT и RC.
# umask 022 задан явно: права копий обязан ставить механизм, а не вызывающий.
run_bs() {
    local s="$1"; shift
    OUT=$(cd "$s" && umask 022 && env -i PATH="$T/bin:$PATH" HOME="$s/home" LC_ALL=C.UTF-8 \
          HARNESS_HOME="$s/home" HARNESS_INSTALL_CONF="$s/install.conf" \
          HARNESS_CONF="$s/harness.conf" PRODUCT_POWER_STATE="$s/power.json" \
          bash "$BS" "$@" 2>&1)
    RC=$?
}

kod() { [ "$RC" != 0 ] && echo "≠0" || echo 0; }
est() { if eval "$1"; then echo да; else echo нет; fi; }
show_out() { printf '%s\n' "$OUT" | sed 's/^/        | /'; }

# Имена баз — РАЗБОРОМ BACKUP_DBS стенда, тем же правилом, что у механизма
# («имя=команда», строка = запись): список не вписан в пробу руками.
# Конфиг читает тот же загрузчик, что и механизм, под `env -i`.
db_names() { # $1 = стенд → имена баз через пробел
    env -i PATH="$PATH" HOME="$1/home" HARNESS_HOME="$1/home" \
        HARNESS_INSTALL_CONF="$1/install.conf" HARNESS_CONF="$1/harness.conf" \
        bash -c 'source "$1"; konf_zagruzit; printf "%s\n" "${BACKUP_DBS:-}"' _ "$T/h/scripts/lib/config.sh" \
        | sed 's/^ *//;s/ *$//' | grep -v '^$' | sed 's/=.*//' | tr '\n' ' ' | sed 's/ $//'
}
# Ночной путь оставляет датированный каталог в BACKUP_DIR; режиму --базы-в он чужой.
night_dir() { find "$1/backup" -mindepth 1 -maxdepth 1 -type d -name '20[0-9][0-9]-*_*' 2>/dev/null | head -1; }
night_ran() { [ -n "$(night_dir "$1")" ] && echo да || echo нет; }
# Права всего, что лежит под каталогом $1 (включая его самого): нарушители по
# имени и число файлов. Пустое дерево — «файлов: 0», и это провал случая.
rights() { # $1 = каталог → «каталоги не 700: …; файлы не 600: …; файлов: N»
    local bad_d bad_f n
    [ -d "$1" ] || { printf 'каталога нет'; return; }
    bad_d=$(find "$1" -type d ! -perm 700 -printf '%P(%m) ' 2>/dev/null)
    bad_f=$(find "$1" -type f ! -perm 600 -printf '%P(%m) ' 2>/dev/null)
    n=$(find "$1" -type f | wc -l)
    printf 'каталоги не 700: %s; файлы не 600: %s; файлов есть: %s' \
        "${bad_d% }" "${bad_f% }" "$([ "$n" -gt 0 ] && echo да || echo нет)"
}
full_dumps() { # $1 = каталог, $2… = имена баз → «имя:да|нет …» (дочитан ли db-<имя>.dump)
    local d="$1" n out=""; shift
    for n in "$@"; do
        if tail -c 4096 "$d/db-$n.dump" 2>/dev/null \
            | grep -qF -e 'PostgreSQL database dump complete' -e '-- Dump completed on'; then
            out="$out $n:да"
        else out="$out $n:нет"; fi
    done
    printf '%s' "${out# }"
}

RIGHTS_OK="каталоги не 700: ; файлы не 600: ; файлов есть: да"
PRE="перед-выкатом"
INTO_NAME="20261114-120000-0a1b2c3d"

echo "--- права копий и backup.sh --базы-в ($MECHANISM)"

# ── (а) права копий при umask 022 вызывающего ──────────────────────────────
# 1. БОЛЬНОЙ: ночная копия ложилась с правами вызывающего (644/755): дамп базы
# с данными покупателей читал любой пользователь машины.
S=$(stand a1)
run_bs "$S"
NIGHT=$(night_dir "$S")
case_check "БОЛЬНОЙ (а): ночная копия при umask 022 — каталоги 700, файлы 600 (bundle, tar, crontab, дампы)" \
    "$RIGHTS_OK" "$(rights "${NIGHT:-$S/backup/нет-ночной-копии}")"
case_check "(а): дампы всех баз списка есть в ночной копии — права судят и их" \
    "$(db_names "$S" | sed 's/\([^ ]*\)/\1:да/g')" "$(full_dumps "${NIGHT:-/nonexistent}" $(db_names "$S"))"
case_check "БОЛЬНОЙ (а): каталог копий, созданный механизмом, — 700" \
    "700" "$(stat -c %a "$S/backup" 2>/dev/null || echo нет)"

# ── (в) --базы-в: дампы всех баз для двери выката ──────────────────────────
# 2. Здоровый: каждая база списка дала дочитанный дамп, права 600/700, ничего
# из ночной работы (ни bundle/tar/crontab, ни метки, ни ночного каталога).
S=$(stand v2)
INTO="$S/backup/$PRE/$INTO_NAME"
run_bs "$S" --базы-в "$INTO"
NAMES=$(db_names "$S")
case_check "(в): разбор BACKUP_DBS стенда дал список баз — пустой список был бы провалом" \
    "alfa beta gamma" "$NAMES"
# shellcheck disable=SC2086
case_check "(в) здоровый --базы-в: код 0, каждая база списка — дочитанный db-<имя>.dump" \
    "код 0; $(printf '%s' "$NAMES" | sed 's/\([^ ]*\)/\1:да/g')" "код $(kod); $(full_dumps "$INTO" $NAMES)"
[ "$RC" = 0 ] || show_out
EXTRA=$(find "$INTO" -mindepth 1 -maxdepth 1 ! -name 'db-*.dump' -printf '%f ' 2>/dev/null)
case_check "(в) здоровый --базы-в: лишнего в каталоге нет, метки нет, ночного прогона нет" \
    "лишнее: ; метка: нет; ночной прогон: нет" \
    "лишнее: ${EXTRA% }; метка: $(est '[ -e "$S/hb/backup" ]'); ночной прогон: $(night_ran "$S")"
case_check "БОЛЬНОЙ (а)+(в): права дампов --базы-в при umask 022 — 700/600" \
    "$RIGHTS_OK" "$(rights "$INTO")"

# 3. БОЛЬНОЙ: команда одной базы падает — код ≠ 0, база названа. Ноль здесь
# пустил бы миграцию без копии этой базы.
S=$(stand v3 "" false "")
run_bs "$S" --базы-в "$S/backup/$PRE/$INTO_NAME"
case_check "БОЛЬНОЙ (в): команда базы beta упала — код ≠ 0, beta названа, ночного прогона нет" \
    "код ≠0; beta: да; ночной прогон: нет" \
    "код $(kod); beta: $(est 'printf "%s" "$OUT" | grep -q beta'); ночной прогон: $(night_ran "$S")"

# 4. БОЛЬНОЙ: дамп оборван без маркера — .НЕПОЛНЫЙ и код ≠ 0.
S=$(stand v4 "" "" "cat $T/src/pg-cut.dump")
INTO="$S/backup/$PRE/$INTO_NAME"
run_bs "$S" --базы-в "$INTO"
case_check "БОЛЬНОЙ (в): дамп gamma оборван — db-gamma.dump.НЕПОЛНЫЙ и код ≠ 0" \
    "код ≠0; НЕПОЛНЫЙ: да; целый gamma: нет" \
    "код $(kod); НЕПОЛНЫЙ: $(est '[ -s "$INTO/db-gamma.dump.НЕПОЛНЫЙ" ]'); целый gamma: $(est '[ -e "$INTO/db-gamma.dump" ]')"

# 5. БОЛЬНОЙ: лок держит другой прогон. Ночной код при занятом локе выходит 0
# («прогон уже идёт») — дверь сочла бы дамп снятым.
S=$(stand v5)
mkdir -p "$S/log/locks/services"
flock "$S/log/locks/services/backup.lock" bash -c 'touch "$1"; exec sleep 120' _ "$S/held" &
LOCK_HOLDER=$!
for _ in $(seq 50); do [ -e "$S/held" ] && break; sleep 0.1; done
run_bs "$S" --базы-в "$S/backup/$PRE/$INTO_NAME"
pkill -P "$LOCK_HOLDER" 2>/dev/null; kill "$LOCK_HOLDER" 2>/dev/null; LOCK_HOLDER=""
case_check "БОЛЬНОЙ (в): лок занят — код ≠ 0 и слово «лок», дампов 0" \
    "код ≠0; лок: да; дампов: 0" \
    "код $(kod); лок: $(est 'printf "%s" "$OUT" | grep -qi лок'); дампов: $(ls "$S/backup/$PRE/$INTO_NAME"/db-*.dump 2>/dev/null | wc -l)"

# 6. Продукт выключен владельцем: базы не работают, дверь узнаёт это словами.
S=$(stand v6)
printf '{"когда":"2026-11-10T09:00:00","контейнеры":["a"],"сайт":1,"cron":1}' > "$S/power.json"
run_bs "$S" --базы-в "$S/backup/$PRE/$INTO_NAME"
case_check "(в): продукт выключен — код ≠ 0 и слово «выключен», дампов и ночи нет" \
    "код ≠0; выключен: да; дампов: 0; ночной прогон: нет" \
    "код $(kod); выключен: $(est 'printf "%s" "$OUT" | grep -qi выключен'); дампов: $(ls "$S/backup/$PRE/$INTO_NAME"/db-*.dump 2>/dev/null | wc -l); ночной прогон: $(night_ran "$S")"

# 7. БОЛЬНОЙ: список баз пуст — снимать нечего, и ноль читался бы «копия есть».
S=$(stand v7 "" "" "" пусто)
run_bs "$S" --базы-в "$S/backup/$PRE/$INTO_NAME"
case_check "БОЛЬНОЙ (в): BACKUP_DBS пуст — код ≠ 0 и ключ назван, ночного прогона нет" \
    "код ≠0; BACKUP_DBS: да; ночной прогон: нет" \
    "код $(kod); BACKUP_DBS: $(est 'printf "%s" "$OUT" | grep -q BACKUP_DBS'); ночной прогон: $(night_ran "$S")"

# 8. Каталог не задан — отказ, а не ночной прогон.
S=$(stand v8)
run_bs "$S" --базы-в
case_check "(в): --базы-в без каталога — код ≠ 0, ночного прогона нет" \
    "код ≠0; ночной прогон: нет" "код $(kod); ночной прогон: $(night_ran "$S")"

# 9. И-1: --базы-в не убирает соседей своего каталога. Каталог назначает
# вызывающий; ротация «рядом с ним» снесла бы чужое по любому пути.
S=$(stand v9)
for i in 1 2 3 4 5 6 7; do mkdir -p "$T/v9-ryadom/2026110$i-120000-0a1b2c3$i"; done
run_bs "$S" --базы-в "$T/v9-ryadom/20261114-120000-0a1b2c3d"
case_check "И-1 (в): соседи каталога --базы-в вне перед-выкатом не тронуты (7 из 7)" \
    "7" "$(find "$T/v9-ryadom" -mindepth 1 -maxdepth 1 -type d -name '2026110*' | wc -l)"

# ── (в) ночной прогон и каталог перед-выкатом ──────────────────────────────
# 10. Каталог перед-выкатом — свой: ночь не зовёт его чужим; рядом настоящий
# чужой — фильтр по имени не вправе заглушить строку о чужих.
S=$(stand v10)
mkdir -p "$S/backup/$PRE/20261001-000000-0a1b2c3d" "$S/backup/x-чужой"
touch -d '30 days ago' "$S/backup/$PRE" "$S/backup/x-чужой"
run_bs "$S"
CHUZHIH=$(printf '%s' "$OUT" | sed -n 's/.*чужих каталогов старше [0-9]* дн\. — \([0-9]*\) .*/\1/p' | tail -1)
case_check "(в): старый «перед-выкатом» не чужой, x-чужой — по-прежнему чужой" \
    "ротация прошла: да; чужих: 1" \
    "ротация прошла: $(est 'printf "%s" "$OUT" | grep -q "удалено старых каталогов"'); чужих: ${CHUZHIH:-0}"

# 11. БОЛЬНОЙ: ротация перед-выкатом держит 5 новейших ПО ИМЕНИ (оно датировано).
# Время изменения выставлено наоборот: ротация по mtime убрала бы новые.
S=$(stand v11)
for i in 1 2 3 4 5 6 7; do
    mkdir -p "$S/backup/$PRE/2026110$i-120000-0a1b2c3$i"
    : > "$S/backup/$PRE/2026110$i-120000-0a1b2c3$i/db-alfa.dump"
    touch -d "$i days ago" "$S/backup/$PRE/2026110$i-120000-0a1b2c3$i"
done
run_bs "$S"
LEFT=$(find "$S/backup/$PRE" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort | tr '\n' ' ')
case_check "БОЛЬНОЙ (в): из 7 каталогов перед-выкатом остались 5 новейших по имени" \
    "20261103-120000-0a1b2c33 20261104-120000-0a1b2c34 20261105-120000-0a1b2c35 20261106-120000-0a1b2c36 20261107-120000-0a1b2c37 " \
    "$LEFT"
case_check "(в): ротация перед-выкатом назвала счёт («перед-выкатом … удалено 2»)" \
    "да" "$(est 'printf "%s" "$OUT" | grep -qE "перед-выкатом.*удалено 2([^0-9]|$)"')"

# 12. Ротация не трогает файлы и каталоги не своего вида, положенные в
# перед-выкатом руками (И-1: судьбу ручной копии решает владелец). Их имена
# старше всех по сортировке: ротация без фильтра вида убрала бы их первыми.
S=$(stand v12)
mkdir -p "$S/backup/$PRE"
for i in 1 2 3 4 5 6 7; do mkdir -p "$S/backup/$PRE/2026110$i-120000-0a1b2c3$i"; done
: > "$S/backup/$PRE/ручная-копия.dump"
: > "$S/backup/$PRE/20261100-120000-0a1b2c30"
mkdir -p "$S/backup/$PRE/20261100-120000" "$S/backup/$PRE/0-ручной-каталог"
run_bs "$S"
case_check "И-1 (в): файл, положенный в перед-выкатом руками, ротация не тронула" \
    "да" "$(est '[ -e "$S/backup/$PRE/ручная-копия.dump" ]')"
case_check "И-1 (в): файл с именем своего вида и каталоги чужого вида ротация не тронула" \
    "файл: да; без sha: да; ручной: да; своих осталось: 5" \
    "файл: $(est '[ -f "$S/backup/$PRE/20261100-120000-0a1b2c30" ]'); без sha: $(est '[ -d "$S/backup/$PRE/20261100-120000" ]'); ручной: $(est '[ -d "$S/backup/$PRE/0-ручной-каталог" ]'); своих осталось: $(find "$S/backup/$PRE" -mindepth 1 -maxdepth 1 -type d -name '2026110?-120000-0a1b2c3?' | wc -l)"

# 13. БОЛЬНОЙ И-1: BACKUP_PRE_DEPLOY_KEEP не целое положительное (0, нечисло,
# ведущий ноль) — ротация отказывает вслух и не убирает ни одного каталога.
# «Держать 0» снесло бы и дамп выката, который только что прошёл.
for KEEP in 0 abc 05 -1; do
    S=$(stand "v13-${KEEP}")
    for i in 1 2 3 4 5 6 7; do mkdir -p "$S/backup/$PRE/2026110$i-120000-0a1b2c3$i"; done
    printf 'BACKUP_PRE_DEPLOY_KEEP="%s"\n' "$KEEP" >> "$S/harness.conf"
    run_bs "$S"
    case_check "БОЛЬНОЙ И-1 (в): BACKUP_PRE_DEPLOY_KEEP=$KEEP — 7 из 7 на месте, ключ назван, код ≠ 0" \
        "осталось: 7; ключ: да; код ≠0" \
        "осталось: $(find "$S/backup/$PRE" -mindepth 1 -maxdepth 1 -type d | wc -l); ключ: $(est 'printf "%s" "$OUT" | grep -q BACKUP_PRE_DEPLOY_KEEP'); код $(kod)"
done

if [ "$CASES_FAILED" = 0 ]; then
    echo "ПРОБА: ЗЕЛЁНАЯ ($CASES_PATHS путей)"; exit 0
fi
echo "ПРОБА: КРАСНАЯ ($CASES_FAILED из $CASES_PATHS)"; exit 1
