#!/usr/bin/env bash
# Проба: самотест жёлтого гейта (`pre-commit-gate.sh --selftest-yellow`) не
# меняет «боевой» gates.jsonl ни на байт.
#
# Откуда взята. Замер смены 29.10 и 20.11: каждый
# прогон самотеста оставлял три строки в боевом /var/log/harness/gates.jsonl
# с деревом и base живого коммита, а очередь слияния судит по ним дерево
# (замер 20.11: 939 строк стенда из 8747). Самотест судит себя сам, но судья
# внутри механизма падает вместе с ним: мутация, снявшая и дверь, и его
# проверку, оставила бы его зелёным. Эта проба судит снаружи — по байтам.
#
# Стенд свой: «боевой» каталог журналов — временный, с засеянным журналом
# известных байтов; механизм видит его через LOG_DIR (окружение сильнее
# конфига). Настоящий /var/log/harness/gates.jsonl проба не читает и не пишет.
# Дверь мутаций: PRECOMMIT_UNDER_TEST — хук, который судит проба (мутировать КОПИЮ).
# Вторая дверь: YELLOW_GATES_SH — ворота, которые обязаны звать эту пробу.
set -u
here=$(cd "$(dirname "$0")" && pwd)
src=${PRECOMMIT_UNDER_TEST:-$here/pre-commit-gate.sh}
gates_sh=${YELLOW_GATES_SH:-$here/gates.sh}
# shellcheck disable=SC1091
source "$here/lib/selftest_cases.sh"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
LIVE="$tmp/live"
LIVE_LOG="$LIVE/gates.jsonl"
mkdir -p "$LIVE"
printf '{"head":"seed","verdict":"pass","failed":""}\n' > "$LIVE_LOG"
BEFORE=$(sha256sum "$LIVE_LOG" | cut -d' ' -f1)
LIST_BEFORE=$(ls -A "$LIVE")

rc=0
env -u GATES_LOG_NAME LOG_DIR="$LIVE" bash "$src" --selftest-yellow > "$tmp/out" 2>&1 || rc=$?
sed 's/^/    | /' "$tmp/out"

case_check "самотест жёлтого гейта прошёл" "0" "$rc"
case_check "байты боевого журнала ворот не изменились" "$BEFORE" \
    "$(sha256sum "$LIVE_LOG" | cut -d' ' -f1)"
case_check "в боевом каталоге журналов не появилось файлов" "$LIST_BEFORE" "$(ls -A "$LIVE")"
case_check "в воротах: bash scripts/test_precommit_yellow_live_log.sh" "да" \
    "$(grep -qE '^[[:space:]]*проверка "[^"]+" bash scripts/test_precommit_yellow_live_log\.sh$' "$gates_sh" && echo да || echo нет)"

[ "$CASES_PATHS" -gt 0 ] || { echo "FAIL: ни одного случая"; exit 1; }
echo "SELFTEST: $([ "$CASES_FAILED" = 0 ] && echo зелёный || echo КРАСНЫЙ) ($CASES_PATHS проверок, неудач $CASES_FAILED)"
[ "$CASES_FAILED" = 0 ]
