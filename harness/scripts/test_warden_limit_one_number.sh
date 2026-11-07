#!/usr/bin/env bash
# Проба: весть владельцу о молчании смены при лимите называет простой ОДНИМ
# числом (карточка hr.soobshcheniya-o-limite-v-poryadok).
#
# Больной случай 17.10.2026: в одной вести стояли «не отчитывается 63 минут» и
# «последний отказ 9 мин назад» — две мерки одного простоя; при пустой доске
# туда же уезжало «До остановки смена делала: смена: записи нет».
#
# Функции берутся из ЖИВОГО файла сторожа (дверь мутаций HARNESS_SESSION_WARDEN),
# иначе испорченная копия до пробы не доходит ([[probe-loads-the-live-mechanism]]).
# Канал — подставной tg_send стенда, транскрипт и доска — во временном каталоге.
#
# Имена переменных ЛАТИНИЦЕЙ: bash не берёт кириллические идентификаторы.
set -uo pipefail
export LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

WARDEN="${HARNESS_SESSION_WARDEN:-$HERE/../harness/demons/session-warden.sh}"
# В пакете демоны лежат рядом со scripts (harness/demons), в проекте — под harness/.
[ -f "$WARDEN" ] || WARDEN="$HERE/../demons/session-warden.sh"
[ -f "$WARDEN" ] || { echo "ПЛОХО: сторожа нет по пути раскладки — проверять нечего"; exit 1; }
# board.py ищется сторожем как $PROJECT_DIR/scripts/board.py: в обеих раскладках
# это каталог над пробой.
ROOT="$(cd "$HERE/.." && pwd)"

STAND=$(mktemp -d -t warden-onenum.XXXXXX)
trap 'rm -rf "$STAND"' EXIT

FNS="$STAND/fns.sh"
for fn in сказать_владельцу_о_молчании подавлен_кулдауном лимит_в_транскрипте_минут немота_агента_минут; do
    sed -n "/^$fn() {/,/^}/p" "$WARDEN" >> "$FNS"
    grep -q "^$fn() {" "$FNS" || { echo "ПЛОХО: функции $fn в файле сторожа нет — проверять нечего"; exit 1; }
done

ago() { date -u -d "$1 minutes ago" +%Y-%m-%dT%H:%M:%SZ; }
answer() { printf '{"type":"assistant","timestamp":"%s","message":{"content":[{"type":"text","text":"x"}]}}\n' "$(ago "$1")"; }
refusal() { printf '{"type":"assistant","error":"rate_limit","apiErrorStatus":429,"timestamp":"%s"}\n' "$(ago "$1")"; }

# $1 = транскрипт, $2 = слово смены на доске ("" — доска пуста), $3 = минут без отчёта,
# $4 = минут с работы. Печатает текст, ушедший в канал.
tell() {
    local case_dir
    case_dir=$(mktemp -d "$STAND/case.XXXXXX")
    if [ -n "$2" ]; then
        HARNESS_DOSKA="$case_dir/доска.json" PROJECT_DIR="$ROOT" \
            python3 "$ROOT/scripts/board.py" --сказать смена "$2" --на 60 >/dev/null 2>&1 \
            || { echo "ПЛОХО: доска стенда не записалась"; return 0; }
    fi
    LOG_DIR="$case_dir" TRANSCRIPT="$1" HARNESS_SCRIPTS="$HERE" PROJECT_DIR="$ROOT" \
    HARNESS_DOSKA="$case_dir/доска.json" PULSE_OWNER_MIN=60 SAID="$case_dir/channel.txt" \
        bash -c 'set -euo pipefail
                 say() { :; }; jlog() { :; }
                 tg_send() { printf "%s\n" "$1" > "$SAID"; }
                 source "$1"; shift
                 сказать_владельцу_о_молчании "$@"' _ "$FNS" "$3" "$4" >/dev/null 2>&1
    cat "$case_dir/channel.txt" 2>/dev/null
}
has() { grep -qF -- "$2" <<< "$1" && echo да || echo нет; }
minutes_named() { grep -oE '[0-9]+ (мин|минут)' <<< "$1" | wc -l | tr -d ' '; }

{ answer 63; refusal 9; } > "$STAND/limit.jsonl"
answer 5 > "$STAND/clean.jsonl"

echo "── БОЛЬНОЙ СЛУЧАЙ 17.10.2026: ответ 63 мин назад, отказ 429 — 9 мин, доска пуста ──"
TEXT=$(tell "$STAND/limit.jsonl" "" 63 9)
echo "      весть: $TEXT"
case_check "Л1: весть ушла и называет лимит подписки" "да" "$(has "$TEXT" 'из-за лимита подписки')"
case_check "Л2: простой назван числом с последнего ответа — «стоит 63 мин»" "да" "$(has "$TEXT" 'стоит 63 мин')"
case_check "Л3: возраст отказа (9 мин) не назван" "нет" \
    "$(grep -qE '(^|[^0-9])9 мин' <<< "$TEXT" && echo да || echo нет)"
case_check "Л4: в вести одно число минут" "1" "$(minutes_named "$TEXT")"
case_check "Л5: нет «записи нет»" "нет" "$(has "$TEXT" 'записи нет')"
case_check "Л6: при пустой доске нет «До остановки смена делала»" "нет" "$(has "$TEXT" 'До остановки смена делала')"

echo "── лимит и запись смены на доске ──"
TEXT=$(tell "$STAND/limit.jsonl" "разбираю архив владельца" 63 9)
echo "      весть: $TEXT"
case_check "Д1: слово доски дошло — «До остановки смена делала: разбираю архив владельца»" "да" \
    "$(has "$TEXT" 'До остановки смена делала: разбираю архив владельца')"
case_check "Д2: и здесь одно число — «стоит 63 мин»" "да;1" \
    "$(has "$TEXT" 'стоит 63 мин');$(minutes_named "$TEXT")"

echo "── контроль: лимита нет — текст прежний ──"
TEXT=$(tell "$STAND/clean.jsonl" "" 200 9)
case_check "Б1: без лимита и без доски — прежняя весть дословно" \
    "Смена работает, но не отчитывается вам 200 минут — статус у неё прошу уже несколько раз. Работа идёт (последняя запись 9 мин назад), признаков лимита подписки в журнале нет." \
    "$TEXT"
TEXT=$(tell "$STAND/clean.jsonl" "жду ответа по развилке" 200 9)
case_check "Б2: без лимита с доской — прежняя весть дословно" \
    "Смена работает, но не отчитывается вам 200 минут — статус у неё прошу уже несколько раз. Смена сообщает о себе: жду ответа по развилке" \
    "$TEXT"

passed=$((CASES_PATHS - CASES_FAILED))
if [ "$CASES_FAILED" -eq 0 ]; then
    echo "итог: $passed/$CASES_PATHS — при лимите простой назван одним числом"
    exit 0
fi
echo "итог: $passed/$CASES_PATHS — КРАСНЫЙ ($CASES_FAILED проб)"
exit 1
