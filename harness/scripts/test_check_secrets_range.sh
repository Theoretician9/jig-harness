#!/usr/bin/env bash
#
# test_check_secrets_range.sh — проба: check-secrets.sh --range судит только
# ДОБАВЛЕННЫЕ строки диапазона git.
#
# Откуда взята. Копия Content-Factory (10.11.2026): первый выкат встал на 229
# исторических совпадениях по всему репо продукта — всё это уже лежало в
# удалённом репо и на проде, блок ничего не защищал. Её ответ — режим
# «этот диапазон не везёт НОВЫЙ секрет». Здесь он берётся ТОЛЬКО инструментом:
# deploy.sh по-прежнему судит весь репо (смена смысла И-3 на выкате — слово
# владельца), и последний блок пробы сторожит именно это.
#
# КОНТРАКТ (для правки check-secrets.sh):
#   check-secrets.sh --range <база>..<голова> [каталог репо]
#     судятся строки «+» диффа диапазона — те же PATTERNS, тот же маркер
#     «не-секрет», тот же вывод «СЕКРЕТ? путь:номер: вид [скрыто]» (номер — в
#     файле головы); заголовки «+++ b/…» — не содержимое, а добавленная строка
#     «++ …» (в диффе «+++ …») — содержимое: заголовок узнаётся только вне ханка.
#     Удалённые строки и удалённые файлы не судятся.
#     Неверный диапазон — код 1 и сообщение, где назван сам диапазон; «чисто» —
#     никогда. --range без диапазона, с одной ревизией или с доводом на «-» —
#     код не 0 и подсказка с «--range».
#     NUL или байт не из UTF-8 в добавленной строке секрет не прячут (и в режиме
#     «весь репо» тоже); имя файла с пробелом, табуляцией, кавычкой, обратной
#     чертой названо как лежит в репо, без кавычек git.
#   Без --range — прежнее: весь репо.
#
# Секреты здесь синтетические, порождаются каждым прогоном и в вывод пробы не
# идут. Судимый скрипт — копией в свой каталог с пустым паспортом и без конфига
# источников: настоящие секретные файлы установки проба не читает.
#
# Дверь мутаций: CHECK_SECRETS_SRC — судимый скрипт; DEPLOY_SH_SRC — судимый выкат.
#   bash scripts/test_check_secrets_range.sh
# Код возврата: 0 — зелёная, 1 — красная.
set -uo pipefail
export LC_ALL=C.UTF-8

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
# В пакете проба лежит в harness/scripts рядом с судимыми, в проекте — в scripts/.
SRC="${CHECK_SECRETS_SRC:-$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/check-secrets.sh}"
DEPLOY="${DEPLOY_SH_SRC:-$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/deploy.sh}"
LIB_CONFIG="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/config.sh"

T=$(mktemp -d "${TMPDIR:-/tmp}/proba-range-sekreta-XXXXXX")
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin/lib" "$T/repo"
cp "$SRC" "$T/bin/check-secrets.sh"
cp "$LIB_CONFIG" "$T/bin/lib/config.sh"
: > "$T/install.conf"; : > "$T/harness.conf"
export HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf"
unset SECRETS_DIR PROJECT_DIR

FAILS=0; TOTAL=0
check() {  # $1 = имя случая, $2 = 0 если прошёл
    TOTAL=$((TOTAL + 1))
    if [ "$2" = 0 ]; then echo "  ок · $1"; else echo "  ПЛОХО · $1"; FAILS=$((FAILS + 1)); fi
}
rand() {  # $1 = длина; буквы и цифры, хотя бы одна цифра
    local s
    s=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$(($1 - 1))")
    printf '%s%s' "$s" "$((RANDOM % 10))"
}
leaks() {  # 0 — в тексте $1 есть кусок значения $2 длиннее 4 знаков
    local i
    for ((i = 0; i + 5 <= ${#2}; i++)); do [[ "$1" == *"${2:i:5}"* ]] && return 0; done
    return 1
}
run_gate() {  # stdout+stderr → $T/out, код → RC
    RC=0
    timeout 60 bash "$T/bin/check-secrets.sh" "$@" > "$T/out" 2>&1 || RC=$?
    OUT="$(cat "$T/out")"
}
g() { git -C "$T/repo" -c user.email=proba@stand -c user.name=proba "$@"; }
commit() { g add -A && g commit -qm "$1" && g rev-parse HEAD; }

OLD_VAL="$(rand 24)"; NEW_VAL="$(rand 24)"; PP_VAL="$(rand 24)"
g init -q

# c1: старый секрет — в базе, вне диапазона.
printf 'x = 1\napi_key = "%s"\n' "$OLD_VAL" > "$T/repo/old.py"
C1=$(commit old)
# c2: чистая правка.
printf 'y = 2\n' > "$T/repo/clean.py"
C2=$(commit clean)
# c3: новый секрет третьей строкой.
printf 'a = 1\nb = 2\napi_key = "%s"\n' "$NEW_VAL" > "$T/repo/new.py"
C3=$(commit leak)
# c4: удаление строки со старым секретом (файл остаётся).
printf 'x = 1\n' > "$T/repo/old.py"
C4=$(commit drop-line)
# c5: добавленная строка, которая в диффе выглядит заголовком «+++ …».
printf 'a = 1\n++ token = "%s"\n' "$PP_VAL" > "$T/repo/pp.py"
C5=$(commit plusplus)
# c6: удаление файла целиком (заголовок «+++ /dev/null»).
g rm -q pp.py; C6=$(g commit -qm rm-file && g rev-parse HEAD)
# c7: имя файла похоже на присвоение секрета — это заголовок, не содержимое.
HDR_NAME="password=$(rand 12).txt"
printf 'z = 3\n' > "$T/repo/$HDR_NAME"
C7=$(commit header-name)
# c8: добавленный секрет с маркером «не-секрет».
printf 'api_key = "%s"  # %s\n' "$(rand 24)" "не-секрет" > "$T/repo/marked.py"
C8=$(commit marked)

# Контроль стенда: все восемь коммитов есть, секреты правда в нужных версиях.
check "контроль: восемь коммитов стенда" \
    "$([ "$(g rev-list --count HEAD)" = 8 ]; echo $?)"
check "контроль: старый секрет в c1 и c3, нет в c4" \
    "$(g grep -qF "$OLD_VAL" "$C1" -- old.py && g grep -qF "$OLD_VAL" "$C3" -- old.py \
       && ! g grep -qF "$OLD_VAL" "$C4" -- old.py; echo $?)"
check "контроль: в диффе c4..c5 есть строка «+++ token»" \
    "$(g diff "$C4..$C5" | grep -q '^+++ token'; echo $?)"

echo "── без --range: прежнее поведение, весь репо"
# Индекс стенда — голова c8: секрет new.py:3 добавлен давно (c3), но лежит в репо.
run_gate "$T/repo"
check "весь репо: секрет из старого коммита, живой в голове — код 1 (получен $RC)" "$([ "$RC" = 1 ]; echo $?)"
check "весь репо: назван new.py:3" "$(grep -qE '^СЕКРЕТ\? .*new\.py:3:' <<< "$OUT"; echo $?)"

echo "── --range"
run_gate --range "$C1..$C2" "$T/repo"
check "БОЛЬНОЙ СЛУЧАЙ: старый секрет вне диапазона — код 0 (получен $RC)" "$([ "$RC" = 0 ]; echo $?)"
check "старый секрет вне диапазона — вывод «чисто», old.py не назван" \
    "$(grep -q 'чисто' <<< "$OUT" && ! grep -q 'old\.py' <<< "$OUT"; echo $?)"

run_gate --range "$C2..$C3" "$T/repo"
check "секрет добавлен в диапазоне — код 1 (получен $RC)" "$([ "$RC" = 1 ]; echo $?)"
check "находка названа new.py:3 и видом" "$(grep -qE '^СЕКРЕТ\? .*new\.py:3: присвоение' <<< "$OUT"; echo $?)"
check "старый секрет при этом не назван (он вне диапазона)" "$(! grep -q 'old\.py' <<< "$OUT"; echo $?)"

run_gate --range "$C1..$C3" "$T/repo"
check "диапазон из двух коммитов: новый секрет пойман — код 1 (получен $RC)" "$([ "$RC" = 1 ]; echo $?)"

run_gate --range "$C3..$C4" "$T/repo"
check "удалённая строка с секретом — код 0 (получен $RC)" "$([ "$RC" = 0 ]; echo $?)"

run_gate --range "$C4..$C5" "$T/repo"
check "добавленная строка «++ token = …» — код 1 (получен $RC)" "$([ "$RC" = 1 ]; echo $?)"
check "«++ …» названа pp.py:2" "$(grep -qE '^СЕКРЕТ\? .*pp\.py:2:' <<< "$OUT"; echo $?)"

run_gate --range "$C5..$C6" "$T/repo"
check "удалённый файл с секретом (+++ /dev/null) — код 0 (получен $RC)" "$([ "$RC" = 0 ]; echo $?)"

run_gate --range "$C6..$C7" "$T/repo"
check "заголовок «+++ b/password=…» не судится как содержимое — код 0 (получен $RC)" \
    "$([ "$RC" = 0 ]; echo $?)"

run_gate --range "$C7..$C8" "$T/repo"
check "маркер «не-секрет» гасит добавленную строку — код 0 (получен $RC)" "$([ "$RC" = 0 ]; echo $?)"

ALL_OUT=""
for r in "$C2..$C3" "$C4..$C5" "$C1..$C8"; do run_gate --range "$r" "$T/repo"; ALL_OUT+="$OUT"; done
check "значения секретов в вывод не попали" \
    "$(! leaks "$ALL_OUT" "$NEW_VAL" && ! leaks "$ALL_OUT" "$PP_VAL" && ! leaks "$ALL_OUT" "$OLD_VAL"; echo $?)"
check "контроль: вывод по диапазонам непустой" "$([ -n "$ALL_OUT" ]; echo $?)"

echo "── неверные доводы"
run_gate --range "net-takogo..$C8" "$T/repo"
check "неверный диапазон — код 1 (получен $RC)" "$([ "$RC" = 1 ]; echo $?)"
check "неверный диапазон назван в сообщении, «чисто» не сказано" \
    "$(grep -qF "net-takogo..$C8" <<< "$OUT" && ! grep -q ': чисто' <<< "$OUT"; echo $?)"
run_gate --range
check "--range без диапазона — код не 0 (получен $RC)" "$([ "$RC" != 0 ]; echo $?)"
# Подсказка показывает форму довода: «--range <база>..<голова>», а не путь «--range» каталогом.
check "--range без диапазона — подсказка показывает форму «--range …..…»" \
    "$(grep -qE -- '--range[^/]*\.\.' <<< "$OUT"; echo $?)"
# Одна ревизия вместо диапазона — git diff сравнил бы её с рабочим деревом;
# довод с «-» git принял бы ключом (--output= уводит дифф в файл, судить нечего).
run_gate --range "$C7" "$T/repo"
check "одна ревизия без «..» — код не 0 и не «чисто» (получен $RC)" \
    "$([ "$RC" != 0 ] && ! grep -q ': чисто' <<< "$OUT"; echo $?)"
run_gate --range "--output=$T/diff.out" "$T/repo"
check "довод-ключ «--output=…» — код не 0 и не «чисто» (получен $RC)" \
    "$([ "$RC" != 0 ] && ! grep -q ': чисто' <<< "$OUT"; echo $?)"

echo "── содержимое и имена, которые ломают разбор"
# NUL дальше первых 8000 байт: git считает файл текстом и отдаёт строку с NUL
# в дифф; grep без -a назвал бы весь текст двоичным и промолчал о секрете.
NUL_VAL="$(rand 24)"
{ head -c 8100 /dev/zero | tr '\0' 'x'; printf '\na\0b\napi_key = "%s"\n' "$NUL_VAL"; } > "$T/repo/nul.txt"
C9=$(commit nul)
check "контроль: git отдаёт nul.txt текстом (в диффе есть строка с api_key)" \
    "$(g diff "$C8..$C9" | grep -aq '^+api_key'; echo $?)"
run_gate --range "$C8..$C9" "$T/repo"
check "NUL в добавленной строке не прячет секрет — код 1 (получен $RC)" "$([ "$RC" = 1 ]; echo $?)"
check "NUL: назван nul.txt:3" "$(grep -qE '^СЕКРЕТ\? .*nul\.txt:3:' <<< "$OUT"; echo $?)"
# Байт не из UTF-8 в строке секрета: grep в UTF-8 локали судит её двоичной.
BAD_VAL="$(rand 24)"
printf 'api_key = "%s%s"\n' "$BAD_VAL" $'\351' > "$T/repo/latin.py"
C10=$(commit latin1)
run_gate --range "$C9..$C10" "$T/repo"
check "байт не из UTF-8 не прячет секрет — код 1 (получен $RC)" "$([ "$RC" = 1 ]; echo $?)"
check "байт не из UTF-8: назван latin.py:1" "$(grep -qE '^СЕКРЕТ\? .*latin\.py:1:' <<< "$OUT"; echo $?)"
check "байт не из UTF-8: разбор без предупреждений о кодировке" "$(! grep -qai 'multibyte\|warning' <<< "$OUT"; echo $?)"
# Тот же отсев находок общий с режимом «весь репо»: там строка не прячется тоже.
run_gate "$T/repo"
check "весь репо: байт не из UTF-8 не прячет секрет — назван latin.py:1" \
    "$(grep -qE '^СЕКРЕТ\? .*latin\.py:1:' <<< "$OUT"; echo $?)"
# Имена, которые git в заголовке диффа пишет иначе: пробел — с хвостовой
# табуляцией, табуляция и кавычка — в кавычках с экранированием.
SP_VAL="$(rand 24)"; Q_VAL="$(rand 24)"
SP_NAME="мой файл.py"; Q_NAME="$(printf 't\tq"b\\\\c.py')"
printf 'api_key = "%s"\n' "$SP_VAL" > "$T/repo/$SP_NAME"
C11=$(commit space)
run_gate --range "$C10..$C11" "$T/repo"
check "имя с пробелом: код 1 (получен $RC)" "$([ "$RC" = 1 ]; echo $?)"
check "имя с пробелом названо как есть: «$SP_NAME:1:»" "$(grep -qF "СЕКРЕТ? $SP_NAME:1:" <<< "$OUT"; echo $?)"
printf 'api_key = "%s"\n' "$Q_VAL" > "$T/repo/$Q_NAME"
C12=$(commit quoted)
check "контроль: git пишет имя с табуляцией в кавычках" \
    "$(g -c core.quotepath=false diff "$C11..$C12" | grep -q '^+++ "b/'; echo $?)"
run_gate --range "$C11..$C12" "$T/repo"
check "имя с табуляцией, кавычкой и обратной чертой: код 1 (получен $RC)" "$([ "$RC" = 1 ]; echo $?)"
check "такое имя названо как есть, без кавычек git" "$(grep -qF "СЕКРЕТ? $Q_NAME:1:" <<< "$OUT"; echo $?)"
ALL_OUT=""
for r in "$C8..$C9" "$C9..$C10" "$C10..$C12"; do run_gate --range "$r" "$T/repo"; ALL_OUT+="$OUT"; done
check "значения секретов этих случаев в вывод не попали" \
    "$(! leaks "$ALL_OUT" "$NUL_VAL" && ! leaks "$ALL_OUT" "$BAD_VAL" && ! leaks "$ALL_OUT" "$SP_VAL" \
       && ! leaks "$ALL_OUT" "$Q_VAL"; echo $?)"

echo "── страж: выкат не судит диапазон (И-3 на выкате — слово владельца)"
CALLS=$(grep -n 'check-secrets\.sh' "$DEPLOY" | grep -v '^[0-9]*:[[:space:]]*#' || true)
check "контроль: в deploy.sh найден вызов check-secrets.sh" "$([ -n "$CALLS" ]; echo $?)"
check "deploy.sh не содержит --range" "$(! grep -qF -- '--range' "$DEPLOY"; echo $?)"

echo "ИТОГ: путей $TOTAL, плохих $FAILS"
[ "$FAILS" = 0 ]
