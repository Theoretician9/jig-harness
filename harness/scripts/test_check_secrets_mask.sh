#!/usr/bin/env bash
#
# test_check_secrets_mask.sh — проба: check-secrets.sh, найдя секрет, называет
# место и вид, но НЕ печатает значение (И-3: вывод гейта оседает в журналах
# ворот, в выводе publish.sh и в транскрипте сессии).
#
# КОНТРАКТ ВЫВОДА (для правки check-secrets.sh):
#   одна строка на находку, значение и любой его кусок длиннее 4 знаков в
#   строку не попадают:
#       СЕКРЕТ? <путь>:<номер строки>: <вид> [скрыто]
#   <вид> — из словаря: присвоение (имя token/secret/password/api_key и
#   значение рядом), telegram, aws, google, github, slack, pem, форма-43.
#   Префикс «СЕКРЕТ? » сохранён: строк находок никто, кроме самотеста самого
#   check-secrets.sh, не разбирает — gates.sh, deploy.sh, pre-commit-gate.sh
#   смотрят только код возврата, publish.sh печатает вывод как есть (потому и
#   маска «[скрыто]», а не «первые 4 знака…»: показывать нечего и некому).
#   Коды не меняются: 1 — находка, 0 — чисто.
#
# Случаи:
#   1. присвоение в .env-стиле: код 1, «файл:строка» и вид «присвоение»;
#   2. токен формата github: код 1, «файл:строка» и вид «github»;
#   3. в stdout+stderr (режим по дереву) нет ни одного куска значения длиннее
#      4 знаков — ни у одного из двух секретов;
#   4. то же в режиме --staged (им зовёт pre-commit-gate.sh);
#   5. чистое дерево — код 0.
#
# Значения порождаются при каждом прогоне и в выводе пробы не печатаются.
# Формат github собирается из частей: целиком он в этом файле не лежит, иначе
# сама проба краснила бы check-secrets.sh на дереве.
#
# Дверь для мутаций: CHECK_SECRETS_SRC — путь судимого скрипта.
#   bash scripts/test_check_secrets_mask.sh
# Код возврата: 0 — зелёная, 1 — красная.
set -uo pipefail
export LC_ALL=C.UTF-8

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
SRC="${CHECK_SECRETS_SRC:-$ROOT/scripts/check-secrets.sh}"

T=$(mktemp -d /tmp/proba-maska-sekreta-XXXXXX)
trap 'rm -rf "$T"' EXIT

# Судимый скрипт — копией в свой каталог вместе с загрузчиком паспорта, а
# паспорт — пустой: каталог ключей живой установки к пробе отношения не имеет.
mkdir -p "$T/bin/lib" "$T/repo"
cp "$SRC" "$T/bin/check-secrets.sh"
cp "$ROOT/scripts/lib/config.sh" "$T/bin/lib/config.sh"
: > "$T/install.conf"; : > "$T/harness.conf"
export HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf"

FAILS=0
check() {  # $1 = имя случая, $2 = 0 если прошёл
    if [ "$2" = 0 ]; then echo "  ок · $1"; else echo "  ПЛОХО · $1"; FAILS=$((FAILS + 1)); fi
}

rand() {  # $1 = длина; буквы обоих регистров и цифры, хотя бы одна цифра
    local s
    s=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$(($1 - 1))")
    printf '%s%s' "$s" "$((RANDOM % 10))"
}

# Есть ли в тексте кусок значения длиннее 4 знаков. 0 — утечка есть.
leaks() {  # $1 = текст, $2 = значение
    local text="$1" val="$2" i
    for ((i = 0; i + 5 <= ${#val}; i++)); do
        [[ "$text" == *"${val:i:5}"* ]] && return 0
    done
    return 1
}

run_gate() {  # печатает stdout+stderr, код — в файл $T/rc
    local rc=0
    bash "$T/bin/check-secrets.sh" "$@" > "$T/out" 2>&1 || rc=$?
    echo "$rc" > "$T/rc"
}

git -C "$T/repo" init -q
VAL_PLAIN="$(rand 24)"
GH_PREFIX="gh"; GH_PREFIX="${GH_PREFIX}p_"
VAL_GH="${GH_PREFIX}$(rand 36)"

# Строка 1 — пустышка, чтобы номер строки находки был не тривиальной «1».
printf '# стенд\nDB_PASSWORD=%s\n' "$VAL_PLAIN" > "$T/repo/app.env"
printf 'x = 1\ny = 2\nghtok = %s\n' "$VAL_GH" > "$T/repo/deploy.cfg"
git -C "$T/repo" add -A

# Контроль стенда: значения действительно лежат в индексе, иначе случай 3
# зеленел бы на пустом месте.
check "контроль: оба значения в индексе стенда" \
    "$(git -C "$T/repo" grep --cached -qF "$VAL_PLAIN" && git -C "$T/repo" grep --cached -qF "$VAL_GH"; echo $?)"

echo "── 1-3. режим по дереву"
run_gate "$T/repo"
OUT="$(cat "$T/out")"; RC="$(cat "$T/rc")"
check "находка — код 1 (получен $RC)" "$([ "$RC" = 1 ]; echo $?)"
check "присвоение названо: app.env:2 и вид «присвоение»" \
    "$(grep -qE '^СЕКРЕТ\? .*app\.env:2: присвоение' <<< "$OUT"; echo $?)"
check "токен github назван: deploy.cfg:3 и вид «github»" \
    "$(grep -qE '^СЕКРЕТ\? .*deploy\.cfg:3: github' <<< "$OUT"; echo $?)"
check "в выводе нет куска значения присвоения длиннее 4 знаков" \
    "$(! leaks "$OUT" "$VAL_PLAIN"; echo $?)"
check "в выводе нет куска токена github длиннее 4 знаков" \
    "$(! leaks "$OUT" "$VAL_GH"; echo $?)"

echo "── 4. режим --staged"
run_gate --staged "$T/repo"
OUT="$(cat "$T/out")"; RC="$(cat "$T/rc")"
check "--staged: находка — код 1 (получен $RC)" "$([ "$RC" = 1 ]; echo $?)"
check "--staged: нет куска ни одного значения длиннее 4 знаков" \
    "$(! leaks "$OUT" "$VAL_PLAIN" && ! leaks "$OUT" "$VAL_GH"; echo $?)"

echo "── 5. чистое дерево"
printf '# стенд\nDB_PASSWORD_FILE=/etc/stand/db\n' > "$T/repo/app.env"
printf 'x = 1\n' > "$T/repo/deploy.cfg"
git -C "$T/repo" add -A
run_gate "$T/repo"
RC="$(cat "$T/rc")"
check "чистое дерево — код 0 (получен $RC)" "$([ "$RC" = 0 ]; echo $?)"

if [ "$FAILS" != 0 ]; then
    echo "ПРОБА КРАСНАЯ: неудач $FAILS"
    exit 1
fi
echo "ПРОБА ЗЕЛЁНАЯ"
exit 0
