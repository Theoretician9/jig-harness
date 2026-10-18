#!/usr/bin/env bash
# Проба: сторож сессий говорит с владельцем ТОЛЬКО через дверь канала.
#
# Откуда взята. Владелец 17.10.2026 снимком канала: «и опять» — при держащемся
# лимите подписки он получил «Смена работает, но не отчитывается вам 63 минут»
# (18:10) и то же самое в 19:10. Дверь обе вести ПРИДЕРЖАЛА и сказала об этом
# кодом 3, но обёртка сторожа читала код СОСТАВНОГО оператора `if` — на ложном
# условии он даёт 0 — и досылала весть прямым curl мимо двери. Журнал сторожа
# обе минуты честно печатал «tg_send.sh отказал — пробую прямой curl».
#
# Судится ТЕКСТ обёртки из живого файла сторожа; путь берётся из окружения
# (HARNESS_SESSION_WARDEN), иначе испорченная копия мутации до пробы не
# доходит ([[probe-loads-the-live-mechanism]]).
#
# Имена переменных ЛАТИНИЦЕЙ: bash не берёт кириллические идентификаторы.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

WARDEN="${HARNESS_SESSION_WARDEN:-$HERE/../harness/demons/session-warden.sh}"
STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT
mkdir -p "$STAND/proekt/scripts" "$STAND/bin"

CURL_LOG="$STAND/curl.log"
CHAT_STENDA=100500          # не-секрет: выдуманный номер чата стенда
# Ключ стенда ГЕНЕРИРУЕТСЯ: имя файла из боевого каталога само было бы
# доступом ([[the-probes-secret-was-a-real-one]]).
python3 -c "import secrets,io,sys; io.open(sys.argv[1],'w').write('11'+secrets.token_hex(16)+'\n')" \
    "$STAND/klyuch"

# Подставной curl: отвечает так, как ждёт обёртка, и оставляет след вызова.
cat > "$STAND/bin/curl" <<'CURL_EOF'
#!/usr/bin/env bash
printf 'vyzvan\n' >> "$STAND_CURL_LOG"
printf '{"ok":true,"result":{"message_id":1}}'
CURL_EOF
chmod +x "$STAND/bin/curl"

# Подставная дверь: отвечает тем кодом, который ей велели, и ничего не шлёт.
cat > "$STAND/proekt/scripts/tg_send.sh" <<'DOOR_EOF'
#!/usr/bin/env bash
exit "${DOOR_CODE:-0}"
DOOR_EOF
chmod +x "$STAND/proekt/scripts/tg_send.sh"

WRAPPER="$STAND/wrapper.sh"
sed -n '/^tg_send() {$/,/^}$/p' "$WARDEN" > "$WRAPPER"
case_check "обёртка сторожа вынута из его файла" \
    "да" "$(grep -q 'scripts/tg_send.sh' "$WRAPPER" && echo да || echo нет)"

call_wrapper() {  # $1 = код двери; печатает «код обёртки;вызовов curl»
    local rc=0 calls
    : > "$CURL_LOG"
    # Оболочка стенда — как у живого демона (`set -euo pipefail`): под другими
    # флагами судилась бы не та дверь.
    DOOR_CODE="$1" PROJECT_DIR="$STAND/proekt" TG_CHAT_ID="$CHAT_STENDA" \
    TG_TOKEN_FILE="$STAND/klyuch" STAND_CURL_LOG="$CURL_LOG" \
    PATH="$STAND/bin:$PATH" \
        bash -c 'set -euo pipefail; say() { :; }; source "$1"; tg_send "весть стенда" --не-досылать' \
            _ "$WRAPPER" >/dev/null 2>&1 || rc=$?
    calls=$(wc -l < "$CURL_LOG" 2>/dev/null || echo 0)
    printf '%s;%s\n' "$rc" "${calls// /}"
}

echo "── БОЛЬНОЙ СЛУЧАЙ 17.10.2026: придержанное дверью уходило прямым curl ──"
case_check "дверь придержала (код 3) — curl не зовём, обёртка отвечает успехом" \
    "0;0" "$(call_wrapper 3)"
case_check "дверь доставила (код 0) — curl не зовём" "0;0" "$(call_wrapper 0)"

echo "── БОЛЬНОЙ СЛУЧАЙ: отказ двери уходил прямым curl ──"
# Код 1 у двери — это и отказ сторожа канала по правилам (простыня,
# самокомментарий), и сбой транспорта: отличить их звонящий не может. Обход на
# отказе делает правила канала пустыми. Отказ — недоставка (код обёртки 1),
# прямой curl остаётся только там, где двери нет физически.
case_check "дверь ОТКАЗАЛА (код 1) — curl не зовём, обёртка отвечает недоставкой" "1;0" "$(call_wrapper 1)"
chmod -x "$STAND/proekt/scripts/tg_send.sh"
case_check "двери нет (не исполняется) — запасной curl зовём" "0;1" "$(call_wrapper 0)"
chmod +x "$STAND/proekt/scripts/tg_send.sh"
# Прямой путь живёт ТОЛЬКО в обёртке: второй curl к Bot API в другом месте
# файла обходил бы и эту пробу, и дверь.
case_check "адрес Bot API в файле сторожа только внутри обёртки" \
    "$(grep -c 'api\.telegram\.org' "$WARDEN")" "$(grep -c 'api\.telegram\.org' "$WRAPPER")"

echo "── БОЛЬНОЙ СЛУЧАЙ: весть длиннее потолка двери уходит ВЛОЖЕНИЕМ через ту же дверь ──"
# Отказ двери тексту — недоставка, а не повод её обойти; но и не молчание:
# как сводка heartbeat-watch, весть уходит файлом через ту же дверь (`--file`).
# Дверь-заглушка с памятью: текст длиннее потолка — отказ кодом 1 (как сторож
# канала на простыне), вложение — копия файла и код 0.
mkdir -p "$STAND/proekt2/scripts" "$STAND/zhurnal"
DOOR_LOG="$STAND/dver.log"
cat > "$STAND/proekt2/scripts/tg_send.sh" <<'DOOR_EOF'
#!/usr/bin/env bash
prev=""
for arg in "$@"; do   # «--file» узнаётся в любом месте ряда ключей
    if [ "$prev" = "--file" ]; then
        printf 'vlozhenie\n' >> "$STAND_DOOR_LOG"
        cp "$arg" "$STAND_DOOR_LOG.vlozhenie"
        exit 0
    fi
    prev="$arg"
done
last="${!#}"
if [ "${#last}" -gt 1400 ]; then printf 'tekst-otkaz\n' >> "$STAND_DOOR_LOG"; exit 1; fi
printf 'tekst\n' >> "$STAND_DOOR_LOG"
exit 0
DOOR_EOF
chmod +x "$STAND/proekt2/scripts/tg_send.sh"
DLINNAYA="Сторож сессий: отчёт стенда"
for n in $(seq 1 30); do
    DLINNAYA="$DLINNAYA
• строка отчёта $n: смена молчит, последняя запись журнала старше порога"
done
DLINNAYA="$DLINNAYA
ИТОГО-СТЕНДА-СТОРОЖА"
: > "$DOOR_LOG"; : > "$CURL_LOG"
WARD_RC=0
DLINNAYA="$DLINNAYA" PROJECT_DIR="$STAND/proekt2" LOG_DIR="$STAND/zhurnal" TG_CHAT_ID="$CHAT_STENDA" \
TG_TOKEN_FILE="$STAND/klyuch" STAND_CURL_LOG="$CURL_LOG" STAND_DOOR_LOG="$DOOR_LOG" \
PATH="$STAND/bin:$PATH" \
    bash -c 'set -euo pipefail; say() { :; }; source "$1"; tg_send "$DLINNAYA" --не-досылать' \
        _ "$WRAPPER" >/dev/null 2>&1 || WARD_RC=$?
case_check "условие создано — дверь отказала длинной вести текстом" \
    "да" "$(grep -qx tekst-otkaz "$DOOR_LOG" && echo да || echo нет)"
case_check "длинная весть: обёртка отвечает доставкой (код 0)" "0" "$WARD_RC"
case_check "длинная весть ушла ВЛОЖЕНИЕМ через ту же дверь" \
    "да" "$(grep -qx vlozhenie "$DOOR_LOG" && echo да || echo нет)"
case_check "вложение несёт весть целиком (последняя строка)" "да" \
    "$(grep -q 'ИТОГО-СТЕНДА-СТОРОЖА' "$DOOR_LOG.vlozhenie" 2>/dev/null && echo да || echo нет)"
case_check "длинная весть: прямой curl мимо двери не звался" "0" "$(wc -l < "$CURL_LOG" | tr -d ' ')"

printf '\nДВЕРЬ КАНАЛА У СТОРОЖА: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
