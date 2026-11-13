#!/usr/bin/env bash
# Проба кодов двери канала: отказ правил переписки и сбой связи — РАЗНЫЕ коды,
# а текст, который правил не пройдёт, не ложится в очередь досыла.
#
# Откуда взята. Ревью 18.10 (R1-04, R1-06), карточка
# hr.dver-kanala-otkaz-po-pravilam-svoj-kod: tg_send.sh отвечает 1 и на отказ
# сторожа текста, и на сбой связи — звонящий не знает, переписать текст или
# повторить позже. В день лимита длинная сводка ложится в очередь досыла с
# ответом 0, а досыл (sentinel, --досыл) потом вечно получает отказ по длине.
#
# Код отказа правил — 4: код 3 уже занят («придержано лимитом без досыла»,
# его читают session-warden, stack-watch, dispatcher-stall-watch).
#
# Стенд свой: паспорт, LOG_DIR, подставные curl и limit_mark.py. Боевой канал
# не трогается, ключ в паспорте сгенерирован здесь. Дверь берётся из "$HERE":
# при подмене «из-копии» проба судит копию.
set -uo pipefail
export LC_ALL=C.UTF-8

RULES_CODE=4
TRANSPORT_CODE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT

STAND_PROJECT="$STAND/proekt"
mkdir -p "$STAND_PROJECT/scripts/lib" "$STAND/zhurnal" "$STAND/bin"
for f in tg_send.sh check-channel-messages.py language-level.py; do
    cp "$HERE/$f" "$STAND_PROJECT/scripts/"
done
cp "$HERE/lib/config.sh" "$HERE/lib/config.py" "$HERE/lib/patterns.py" \
   "$STAND_PROJECT/scripts/lib/" 2>/dev/null || true

# Подставной судья лимита: держится ли лимит, решает стенд (STAND_LIMIT=1).
cat > "$STAND_PROJECT/scripts/limit_mark.py" <<'PY'
import os, sys
holds = os.environ.get("STAND_LIMIT") == "1"
if "--час-сброса" in sys.argv:
    print("15:00" if holds else ""); sys.exit(0)
sys.exit(0 if holds and "--лимит-держится" in sys.argv else 1)
PY

{
    printf 'TG_CHAT_ID="100500"\n'                       # не-секрет: выдуманный чат стенда
    printf 'TG_TOKEN_FILE="%s"\n' "$STAND/klyuch"        # не-секрет: путь, а не значение
    printf 'PROJECT_DIR="%s"\n' "$STAND_PROJECT"
    printf 'LOG_DIR="%s"\n' "$STAND/zhurnal"
    printf 'SECRETS_DIR="%s"\n' "$STAND"
    printf 'TMUX_SESSION="proba-dveri"\n'
    printf 'AGENT_START_CMD="true"\n'
} > "$STAND/install.conf"
: > "$STAND/harness.conf"
python3 -c "import secrets,io,sys; io.open(sys.argv[1],'w').write('11'+secrets.token_hex(16)+'\n')" "$STAND/klyuch"

# Подставной curl: STAND_CURL_MODE=down — связи нет (код 7, как у curl), иначе ok:true.
cat > "$STAND/bin/curl" <<'CURL'
#!/usr/bin/env bash
printf 'vyzvan\n' >> "$STAND_CURL_LOG"
if [ "${STAND_CURL_MODE:-ok}" = down ]; then
    echo "curl: (7) Failed to connect to api.telegram.org port 443" >&2; exit 7
fi
printf '{"ok":true,"result":{"message_id":1}}'
CURL
chmod +x "$STAND/bin/curl"

LOG="$STAND/zhurnal"
CURL_LOG="$LOG/curl.log"
QUEUE="$LOG/панель-недоставленное.jsonl"
OUTPUT="$STAND/vyvod.txt"

lines_in() { [ -f "$1" ] && wc -l < "$1" | tr -d ' ' || echo 0; }

send() {  # $1 = держится ли лимит (0/1), $2 = режим curl, дальше доводы двери; печатает код
    local limit="$1" mode="$2" rc=0; shift 2
    : > "$CURL_LOG"
    env -u CLAUDE_CODE_SESSION_ID -u HARNESS_REPORT -u HARNESS_TG_FORCE -u PRODUCT_REPO \
        PATH="$STAND/bin:$PATH" STAND_CURL_LOG="$CURL_LOG" STAND_CURL_MODE="$mode" \
        STAND_LIMIT="$limit" HARNESS_HOME="$STAND" LOG_DIR="$LOG" PROJECT_DIR="$STAND_PROJECT" \
        HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
        HEARTBEAT_DIR="$LOG/heartbeat" CLAUDE_PROJECTS_DIR="$STAND/net" \
        bash "$STAND_PROJECT/scripts/tg_send.sh" "$@" > "$OUTPUT" 2>&1 || rc=$?
    printf '%s\n' "$rc"
}
said() { grep -qF -- "$1" "$OUTPUT" && echo да || echo нет; }

SHORT="Демон резервной копии жив, копия за 03:00 на месте."
LONG=$(python3 -c "print('Демон сорок два мёртв, перезапуск не помог. ' * 60)")

echo "── условие стенда: короткий текст правила проходит, длинный — нет ──"
rc=0; printf '%s' "$SHORT" | HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
    python3 "$STAND_PROJECT/scripts/check-channel-messages.py" >/dev/null 2>&1 || rc=$?
case_check "короткий текст проходит check-channel-messages" "0" "$rc"
rc=0; printf '%s' "$LONG" | HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
    python3 "$STAND_PROJECT/scripts/check-channel-messages.py" >/dev/null 2>&1 || rc=$?
case_check "длинный текст (${#LONG} зн.) отклоняется check-channel-messages" "1" "$rc"
case_check "контроль: короткий текст доставлен, код 0" "0" "$(send 0 ok "$SHORT")"
case_check "контроль: подставной curl позван один раз" "1" "$(lines_in "$CURL_LOG")"

echo "── (1) отказ правил — свой код, транспорт не позван ──"
OUT_BEFORE=$(lines_in "$LOG/tg_out.jsonl")
FAIL_BEFORE=$(lines_in "$LOG/tg_failures.log")
RC_RULES=$(send 0 ok "$LONG")
case_check "отказ правил записан в tg_failures.log" "$((FAIL_BEFORE + 1))" "$(lines_in "$LOG/tg_failures.log")"
case_check "код отказа правил = $RULES_CODE" "$RULES_CODE" "$RC_RULES"
case_check "отказ назван отказом правил переписки" "да" "$(said "нарушает правила переписки")"
case_check "транспорт не позван" "0" "$(lines_in "$CURL_LOG")"
case_check "журнал исходящих не вырос" "$OUT_BEFORE" "$(lines_in "$LOG/tg_out.jsonl")"

echo "── (2) сбой связи — код транспорта ──"
FAIL_BEFORE=$(lines_in "$LOG/tg_failures.log")
RC_NET=$(send 0 down "$SHORT")
case_check "код сбоя связи = $TRANSPORT_CODE" "$TRANSPORT_CODE" "$RC_NET"
case_check "сбой записан в tg_failures.log" "$((FAIL_BEFORE + 1))" "$(lines_in "$LOG/tg_failures.log")"
case_check "БОЛЬНОЙ СЛУЧАЙ: отказ правил и сбой связи различимы по коду" "разные" \
    "$([ "$RC_RULES" != "$RC_NET" ] && echo разные || echo "оба $RC_NET")"

echo "── (3) лимит держится: непроходной текст в очередь досыла не ложится ──"
rm -f "$QUEUE"
case_check "контроль: короткий текст при лимите придержан с досылом, код 0" "0" "$(send 1 ok "$SHORT")"
case_check "контроль: короткий текст лёг в очередь досыла" "1" "$(lines_in "$QUEUE")"
rm -f "$QUEUE"
RC_HELD=$(send 1 ok "$LONG")
case_check "БОЛЬНОЙ СЛУЧАЙ: длинный текст при лимите в очередь досыла НЕ лёг" "0" "$(lines_in "$QUEUE")"
case_check "звонящий получил код отказа правил, а не 0 «уйдёт досылом»" "$RULES_CODE" "$RC_HELD"
case_check "транспорт не позван" "0" "$(lines_in "$CURL_LOG")"
case_check "при --не-досылать длинный текст — тоже отказ правил, а не 3 «придержано»" "$RULES_CODE" \
    "$(send 1 ok --не-досылать "$LONG")"

echo "── (4) подпись вложения судится теми же правилами, и при лимите тоже ──"
# Флаги по-русски: синонимы должны канонизироваться ДО проверки правил, иначе
# подпись под «--подпись» правил не увидит.
DOC="$STAND/dokument.md"
printf 'документ стенда\n' > "$DOC"
case_check "контроль: вложение с короткой подписью доставлено, код 0" "0" \
    "$(send 0 ok --файл "$DOC" --подпись "$SHORT")"
case_check "длинная подпись вложения — отказ правил" "$RULES_CODE" \
    "$(send 0 ok --файл "$DOC" --подпись "$LONG")"
case_check "транспорт не позван" "0" "$(lines_in "$CURL_LOG")"
rm -f "$QUEUE"
case_check "длинная подпись при лимите — отказ правил, а не 0 «уйдёт досылом»" "$RULES_CODE" \
    "$(send 1 ok --файл "$DOC" --подпись "$LONG")"
case_check "длинная подпись при лимите в очередь досыла НЕ легла" "0" "$(lines_in "$QUEUE")"

if [ "$CASES_FAILED" -eq 0 ]; then
    printf '\nПРОБА ЗЕЛЁНАЯ: случаев %d\n' "$CASES_PATHS"; exit 0
fi
printf '\nПРОБА КРАСНАЯ: неудач %d из %d\n' "$CASES_FAILED" "$CASES_PATHS"; exit 1
