#!/usr/bin/env bash
# Проба: демоны гигиены и уборки диска говорят с владельцем через дверь канала
# и НЕ обходят её на отказе.
#
# Почему. Отказ двери (сторож канала отклонил длинный текст кодом 1) — не
# повод уходить прямым curl мимо неё. Код 1 у двери — и отказ по правилам, и
# сбой транспорта; звонящий их не различает, поэтому обход на отказе делает
# правила канала пустыми.
# Прямой путь законен только там, где двери нет физически.
#
# Судится ТЕКСТ обёртки tg_send() из живого файла демона; пути берутся из
# окружения (HARNESS_SERVER_HYGIENE, HARNESS_DISK_CLEANUP), иначе испорченная
# копия мутации до пробы не доходит ([[probe-loads-the-live-mechanism]]).
# Уборка диска целиком не запускается: на стенде она чистила бы docker машины.
# Гигиена гоняется целиком из копии — её проверка на стенде заглушка.
#
# Имена переменных ЛАТИНИЦЕЙ: bash не берёт кириллические идентификаторы.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

HYGIENE="${HARNESS_SERVER_HYGIENE:-$HERE/../harness/demons/server-hygiene.sh}"
DISK="${HARNESS_DISK_CLEANUP:-$HERE/../harness/demons/disk-cleanup.sh}"
STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT
mkdir -p "$STAND/proekt/scripts" "$STAND/bin"

CURL_LOG="$STAND/curl.log"
CHAT_STENDA=100500          # не-секрет: выдуманный номер чата стенда
# Ключ стенда ГЕНЕРИРУЕТСЯ ([[the-probes-secret-was-a-real-one]]).
python3 -c "import secrets,io,sys; io.open(sys.argv[1],'w').write('11'+secrets.token_hex(16)+'\n')" \
    "$STAND/klyuch"

cat > "$STAND/bin/curl" <<'CURL_EOF'
#!/usr/bin/env bash
printf 'vyzvan\n' >> "$STAND_CURL_LOG"
printf '{"ok":true,"result":{"message_id":1}}'
CURL_EOF
chmod +x "$STAND/bin/curl"

# Подставная дверь: на любой зов (текст или --file) отвечает кодом DOOR_CODE.
cat > "$STAND/proekt/scripts/tg_send.sh" <<'DOOR_EOF'
#!/usr/bin/env bash
exit "${DOOR_CODE:-0}"
DOOR_EOF
chmod +x "$STAND/proekt/scripts/tg_send.sh"

call_wrapper() {  # $1 = файл обёртки, $2 = код двери; печатает «0|отказ;вызовов curl»
    local rc=0 calls
    : > "$CURL_LOG"
    # LOG_DIR задан: без него ветка отказа (вложение в $LOG_DIR) падала на
    # unbound, и «отказ, curl 0» выходил из падения, а не из кода обёртки.
    LOG_DIR="$STAND" DOOR_CODE="$2" PROJECT_DIR="$STAND/proekt" TG_CHAT_ID="$CHAT_STENDA" \
    TG_TOKEN_FILE="$STAND/klyuch" STAND_CURL_LOG="$CURL_LOG" \
    PATH="$STAND/bin:$PATH" \
        bash -c 'set -euo pipefail; say() { :; }; source "$1"; tg_send "весть стенда"' \
            _ "$1" >/dev/null 2>&1 || rc=$?
    calls=$(wc -l < "$CURL_LOG" 2>/dev/null || echo 0)
    [ "$rc" = 0 ] || rc=отказ
    printf '%s;%s\n' "$rc" "${calls// /}"
}

judge_demon() {  # $1 = имя для отчёта, $2 = файл демона
    local name="$1" demon="$2" wrapper="$STAND/$1.wrapper.sh"
    echo "── $name ──"
    sed -n '/^tg_send() {/,/^}$/p' "$demon" > "$wrapper"
    if ! grep -q 'scripts/tg_send.sh' "$wrapper"; then
        # Красный от ОТСУТСТВИЯ механизма: отправка не собрана в одну обёртку.
        case_check "$name: отправка собрана в обёртку tg_send() и зовёт дверь" "да" "нет"
        return 0
    fi
    case_check "$name: отправка собрана в обёртку tg_send() и зовёт дверь" "да" "да"
    case_check "$name: дверь доставила (код 0) — curl не зовём" "0;0" "$(call_wrapper "$wrapper" 0)"
    case_check "$name: дверь ОТКАЗАЛА (код 1) — curl не зовём, обёртка отвечает недоставкой" \
        "отказ;0" "$(call_wrapper "$wrapper" 1)"
    chmod -x "$STAND/proekt/scripts/tg_send.sh"
    case_check "$name: двери нет (не исполняется) — запасной curl зовём" "0;1" "$(call_wrapper "$wrapper" 0)"
    chmod +x "$STAND/proekt/scripts/tg_send.sh"
    # Второй прямой путь в другом месте файла обходил бы и эту пробу, и дверь.
    case_check "$name: адрес Bot API в файле только внутри обёртки" \
        "$(grep -c 'api\.telegram\.org' "$demon")" "$(grep -c 'api\.telegram\.org' "$wrapper")"
}

echo "── БОЛЬНОЙ СЛУЧАЙ: отказ двери уходил прямым curl ──"
judge_demon server-hygiene "$HYGIENE"
judge_demon disk-cleanup "$DISK"

# ── Отчёт гигиены длиннее потолка двери: уходит ВЛОЖЕНИЕМ через ту же дверь ──
# Отчёт гигиены — неограниченный вывод проверки: две строки на каждый
# контейнер без потолка памяти и политики перезапуска. При восьми таких
# контейнерах текст длиннее потолка сторожа канала, дверь отказывает тексту, и
# нарушения И-3 не видит никто. Как сводка heartbeat-watch: отказ текстом —
# отчёт файлом через ту же дверь (`--file`), прогон удачен, метка стоит.
# Демон гоняется ЦЕЛИКОМ из копии в своём дереве: проверка — заглушка рядом с
# ним, дверь — заглушка, которая отказывает простыне и берёт вложение.
TREE="$STAND/derevo"
mkdir -p "$TREE/harness/demons" "$TREE/scripts/lib" "$STAND/zhurnal/hb" "$STAND/zhurnal/locks"
cp "$HYGIENE" "$TREE/harness/demons/server-hygiene.sh"
cp "$HERE/lib/config.sh" "$HERE/lib/selftest_cases.sh" "$TREE/scripts/lib/"
cat > "$TREE/harness/demons/server_hygiene_check.py" <<'CHECK_EOF'
import sys
print("Гигиена: диск 41%, своп 0%, swappiness 10 — в норме")
for n in range(1, 9):
    print(f"• app-svc{n}: нет потолка памяти (mem_limit) — контейнер может съесть всю память машины")
    print(f"• app-svc{n}: нет политики перезапуска (restart) — после перезагрузки сервис не поднимется")
print("ИТОГО-СТЕНДА: нарушений 16")
sys.exit(1)
CHECK_EOF
cat > "$STAND/install.conf" <<CONF
PROJECT_DIR="$STAND/proekt"
LOG_DIR="$STAND/zhurnal"
HEARTBEAT_DIR="$STAND/zhurnal/hb"
TG_CHAT_ID="$CHAT_STENDA"
TG_TOKEN_FILE="$STAND/klyuch"
CONF
: > "$STAND/harness.conf"
DOOR_LOG="$STAND/dver.log"
# Дверь-заглушка с памятью: текст длиннее потолка — отказ кодом 1 (как сторож
# канала на простыне), вложение — копия файла и код 0.
cat > "$STAND/proekt/scripts/tg_send.sh" <<'DOOR_EOF'
#!/usr/bin/env bash
if [ -n "${STAND_DOOR_LOG:-}" ]; then
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
fi
exit "${DOOR_CODE:-0}"
DOOR_EOF
chmod +x "$STAND/proekt/scripts/tg_send.sh"

: > "$DOOR_LOG"; : > "$CURL_LOG"
HYG_RC=0
env -i PATH="$STAND/bin:/usr/bin:/bin" HOME="$STAND" LC_ALL=C.UTF-8 HARNESS_HOME="$STAND" \
    HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
    STAND_DOOR_LOG="$DOOR_LOG" STAND_CURL_LOG="$CURL_LOG" \
    timeout 60 bash "$TREE/harness/demons/server-hygiene.sh" > "$STAND/gigiena.log" 2>&1 || HYG_RC=$?
tail -3 "$STAND/gigiena.log" | sed 's/^/      /'
echo "── БОЛЬНОЙ СЛУЧАЙ: отчёт гигиены (8 контейнеров без потолков) длиннее потолка двери ──"
case_check "server-hygiene: условие создано — дверь отказала отчёту текстом" \
    "да" "$(grep -qx tekst-otkaz "$DOOR_LOG" && echo да || echo нет)"
case_check "server-hygiene: длинный отчёт — дверь отказала тексту, прогон удачен (код 0)" "0" "$HYG_RC"
case_check "server-hygiene: отчёт ушёл ВЛОЖЕНИЕМ через ту же дверь" \
    "да" "$(grep -qx vlozhenie "$DOOR_LOG" && echo да || echo нет)"
case_check "server-hygiene: вложение несёт отчёт целиком (8-й контейнер и итог)" "да" \
    "$(grep -q 'app-svc8: нет политики перезапуска' "$DOOR_LOG.vlozhenie" 2>/dev/null \
       && grep -q 'ИТОГО-СТЕНДА: нарушений 16' "$DOOR_LOG.vlozhenie" && echo да || echo нет)"
case_check "server-hygiene: прямой curl мимо двери не звался" "0" "$(wc -l < "$CURL_LOG" | tr -d ' ')"
case_check "server-hygiene: метка свежести стоит — проверка отработала и доставлена" \
    "да" "$([ -e "$STAND/zhurnal/hb/server-hygiene" ] && echo да || echo нет)"

printf '\nДВЕРЬ КАНАЛА У ДЕМОНОВ: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
