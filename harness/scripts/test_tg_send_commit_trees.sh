#!/usr/bin/env bash
# Проба: хеш, названный словом «коммит», сверяется с деревом харнеса И с
# деревом продукта (ключ PRODUCT_REPO в harness.conf). Хеш проходит, если он
# коммит хотя бы в одном; отказ называет оба дерева; пустой ключ — одно дерево.
#
# Стенд свой: паспорт, harness.conf, LOG_DIR, подставной curl, два git-репозитория.
# Боевой канал не трогается, ключ в паспорте сгенерирован здесь.
# tg_send.sh берётся из "$HERE": при подмене «из-копии» проба судит копию.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT

HARNESS_TREE="$STAND/harnes"
PRODUCT_TREE="$STAND/produkt"
mkdir -p "$HARNESS_TREE/scripts/lib" "$PRODUCT_TREE" "$STAND/zhurnal" "$STAND/bin"

for f in tg_send.sh limit_mark.py check-channel-messages.py language-level.py; do
    cp "$HERE/$f" "$HARNESS_TREE/scripts/"
done
cp "$HERE/lib/config.sh" "$HERE/lib/config.py" "$HERE/lib/patterns.py" \
   "$HARNESS_TREE/scripts/lib/" 2>/dev/null || true

# Чужие хуки и глобальный конфиг git стенду не нужны.
stand_git() { GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
              git -c user.name=x -c user.email=x@x "$@"; }
make_repo() {  # $1 = каталог; печатает короткий хеш его коммита
    stand_git -C "$1" init -q
    printf '%s\n' "$1" > "$1/metka.txt"
    stand_git -C "$1" add metka.txt
    stand_git -C "$1" commit -q -m proba
    stand_git -C "$1" rev-parse --short=12 HEAD
}
HARNESS_HASH=$(make_repo "$HARNESS_TREE")
PRODUCT_HASH=$(make_repo "$PRODUCT_TREE")

is_commit_in() {  # $1 = дерево, $2 = хеш
    stand_git -C "$1" cat-file -e "${2}^{commit}" 2>/dev/null
}
# Выдуманный хеш: случайный и проверенно чужой обоим деревьям.
while :; do
    FAKE_HASH=$(python3 -c 'import secrets; print(secrets.token_hex(6))')
    is_commit_in "$HARNESS_TREE" "$FAKE_HASH" || is_commit_in "$PRODUCT_TREE" "$FAKE_HASH" || break
done

{
    printf 'TG_CHAT_ID="100500"\n'                       # не-секрет: выдуманный чат стенда
    printf 'TG_TOKEN_FILE="%s"\n' "$STAND/klyuch"        # не-секрет: путь, а не значение
    printf 'PROJECT_DIR="%s"\n' "$HARNESS_TREE"
    printf 'LOG_DIR="%s"\n' "$STAND/zhurnal"
    printf 'SECRETS_DIR="%s"\n' "$STAND"
    printf 'TMUX_SESSION="proba-kommitov"\n'
    printf 'AGENT_START_CMD="true"\n'
} > "$STAND/install.conf"
python3 -c "import secrets,io,sys; io.open(sys.argv[1],'w').write('11'+secrets.token_hex(16)+'\n')" "$STAND/klyuch"
printf 'PRODUCT_REPO="%s"\n' "$PRODUCT_TREE" > "$STAND/harness-with-product.conf"
printf 'PRODUCT_REPO=""\n' > "$STAND/harness-no-product.conf"

cat > "$STAND/bin/curl" <<'CURL'
#!/usr/bin/env bash
printf 'vyzvan\n' >> "$STAND_CURL_LOG"
printf '{"ok":true,"result":{"message_id":1}}'
CURL
chmod +x "$STAND/bin/curl"

CURL_LOG="$STAND/zhurnal/curl.log"
FAIL_LOG="$STAND/zhurnal/tg_failures.log"
OUT_LOG="$STAND/zhurnal/tg_out.jsonl"
OUTPUT="$STAND/vyvod.txt"

stand_env() {  # $1 = harness.conf стенда, дальше команда
    local conf="$1"; shift
    env -u CLAUDE_CODE_SESSION_ID -u HARNESS_REPORT -u HARNESS_TG_FORCE -u PRODUCT_REPO \
        PATH="$STAND/bin:$PATH" STAND_CURL_LOG="$CURL_LOG" \
        HARNESS_HOME="$STAND" LOG_DIR="$STAND/zhurnal" PROJECT_DIR="$HARNESS_TREE" \
        HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$conf" \
        HEARTBEAT_DIR="$STAND/zhurnal/heartbeat" CLAUDE_PROJECTS_DIR="$STAND/net" \
        "$@"
}

lines_in() { [ -f "$1" ] && wc -l < "$1" | tr -d ' ' || echo 0; }

# Отправка: печатает код возврата; вывод — в $OUTPUT, новая строка отказа — в $STAND/otkaz.txt.
send() {  # $1 = harness.conf, $2 = текст
    local rc=0 before
    : > "$CURL_LOG"
    before=$(lines_in "$FAIL_LOG")
    stand_env "$1" bash "$HARNESS_TREE/scripts/tg_send.sh" "$2" > "$OUTPUT" 2>&1 || rc=$?
    { tail -n +"$((before + 1))" "$FAIL_LOG" 2>/dev/null; cat "$OUTPUT"; } > "$STAND/otkaz.txt"
    printf '%s\n' "$rc"
}
delivered_text() {  # $1 = текст; «да», если подставной curl позван и строка легла в tg_out
    [ "$(lines_in "$CURL_LOG")" = 1 ] \
        && [ -f "$OUT_LOG" ] && tail -1 "$OUT_LOG" | jq -e --arg t "$1" '.text == $t' >/dev/null 2>&1 \
        && echo да || echo нет
}
refusal_says() {  # $@ = слова; «да», если отказ содержит каждое
    local w
    for w in "$@"; do grep -qF -- "$w" "$STAND/otkaz.txt" || { echo нет; return; }; done
    echo да
}

text_for() { printf 'Готово, коммит %s. Проба прошла, 5 случаев.' "$1"; }

echo "── условие стенда: тексты проходят правила переписки, хеши настоящие ──"
for h in "$HARNESS_HASH" "$PRODUCT_HASH" "$FAKE_HASH"; do
    rc=0
    text_for "$h" | stand_env "$STAND/harness-with-product.conf" \
        python3 "$HARNESS_TREE/scripts/check-channel-messages.py" >/dev/null 2>&1 || rc=$?
    case_check "текст с хешем $h проходит check-channel-messages" "0" "$rc"
done
case_check "коммит харнеса есть только в дереве харнеса" "да-нет" \
    "$(is_commit_in "$HARNESS_TREE" "$HARNESS_HASH" && echo да || echo нет)-$(is_commit_in "$PRODUCT_TREE" "$HARNESS_HASH" && echo да || echo нет)"
case_check "коммит продукта есть только в дереве продукта" "нет-да" \
    "$(is_commit_in "$HARNESS_TREE" "$PRODUCT_HASH" && echo да || echo нет)-$(is_commit_in "$PRODUCT_TREE" "$PRODUCT_HASH" && echo да || echo нет)"

echo "── (1) БОЛЬНОЙ СЛУЧАЙ: коммит продукта при заданном PRODUCT_REPO уходит ──"
T=$(text_for "$PRODUCT_HASH")
case_check "код 0" "0" "$(send "$STAND/harness-with-product.conf" "$T")"
case_check "сообщение доставлено (curl позван, строка в tg_out.jsonl)" "да" "$(delivered_text "$T")"
case_check "отказа «которого нет» не было" "нет" "$(refusal_says "которого нет")"

echo "── (2) коммит харнеса уходит ──"
T=$(text_for "$HARNESS_HASH")
case_check "код 0" "0" "$(send "$STAND/harness-with-product.conf" "$T")"
case_check "сообщение доставлено (curl позван, строка в tg_out.jsonl)" "да" "$(delivered_text "$T")"

echo "── (3) выдуманный хеш — отказ правила о коммите ──"
T=$(text_for "$FAKE_HASH")
case_check "код 1" "1" "$(send "$STAND/harness-with-product.conf" "$T")"
case_check "отказ говорит «которого нет» и называет хеш" "да" "$(refusal_says "которого нет" "$FAKE_HASH")"
case_check "транспорт не позван" "0" "$(lines_in "$CURL_LOG")"

echo "── (4) PRODUCT_REPO пуст — коммит продукта отказывается, как прежде ──"
T=$(text_for "$PRODUCT_HASH")
case_check "код 1" "1" "$(send "$STAND/harness-no-product.conf" "$T")"
case_check "отказ говорит «которого нет» и называет хеш" "да" "$(refusal_says "которого нет" "$PRODUCT_HASH")"
case_check "второй корень не выдуман: путь продукта в отказе не назван" "нет" "$(refusal_says "$PRODUCT_TREE")"
case_check "транспорт не позван" "0" "$(lines_in "$CURL_LOG")"

echo "── (5) отказ при заданном PRODUCT_REPO называет оба дерева ──"
T=$(text_for "$FAKE_HASH")
case_check "код 1" "1" "$(send "$STAND/harness-with-product.conf" "$T")"
case_check "это отказ правила о коммите («которого нет»)" "да" "$(refusal_says "которого нет")"
case_check "отказ называет дерево харнеса" "да" "$(refusal_says "$HARNESS_TREE")"
case_check "отказ называет дерево продукта" "да" "$(refusal_says "$PRODUCT_TREE")"

if [ "$CASES_FAILED" -eq 0 ]; then
    printf '\nПРОБА ЗЕЛЁНАЯ: случаев %d\n' "$CASES_PATHS"; exit 0
fi
printf '\nПРОБА КРАСНАЯ: неудач %d из %d\n' "$CASES_FAILED" "$CASES_PATHS"; exit 1
