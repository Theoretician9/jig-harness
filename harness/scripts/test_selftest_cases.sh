#!/usr/bin/env bash
# Проба общего помощника самотестов: scripts/lib/selftest_cases.{py,sh}.
#
# Проба судит ОБЕ реализации на одних случаях: порядок доводов один, счёт
# путей и неудач ведёт помощник, неудача называет ждали/вышло. Помощником
# пользуются самотесты механизмов — сломанный в «всегда ок», он зеленит их все.
#
# Запуск: bash scripts/test_selftest_cases.sh
set -uo pipefail
LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib"
PUTEJ=0; NEUDACH=0

itog() { # $1 = имя, $2 = ждём, $3 = вышло — судит саму пробу, не помощника
    PUTEJ=$((PUTEJ + 1))
    if [ "$2" = "$3" ]; then printf '  ок    %s\n' "$1"
    else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$3"; NEUDACH=$((NEUDACH + 1)); fi
}

# ── python ──────────────────────────────────────────────────────────────────
vyvod=$(python3 - "$LIB" <<'PY' 2>&1
import sys
sys.path.insert(0, sys.argv[1])
from selftest_cases import Cases
c = Cases()
c.check("совпало", 1, 1)
c.check("разошлось", "да", "нет")
print(f"счёт {c.paths} {c.failures} {c.ok}")
PY
)
itog "python: совпадение печатает «ок» с именем" 1 "$(grep -c '^  ок    совпало$' <<< "$vyvod")"
itog "БОЛЬНОЙ СЛУЧАЙ python: неудача называет ждали и вышло в ОДНОМ порядке" 1 \
    "$(grep -c '^  ПЛОХО разошлось: ждали «да», вышло «нет»$' <<< "$vyvod")"
itog "python: пути и неудачи считает помощник" "счёт 2 1 False" "$(grep '^счёт' <<< "$vyvod")"

vyvod=$(python3 - "$LIB" <<'PY' 2>&1
import sys
sys.path.insert(0, sys.argv[1])
from selftest_cases import Cases
c = Cases()
c.check("а", [1], [1])
print(f"счёт {c.paths} {c.failures} {c.ok}")
PY
)
itog "python: без неудач ok истинно" "счёт 1 0 True" "$(grep '^счёт' <<< "$vyvod")"

# ── shell ───────────────────────────────────────────────────────────────────
vyvod=$(bash -c '
    source "$1/selftest_cases.sh"
    case_check "совпало" 1 1
    case_check "разошлось" да нет
    echo "счёт $CASES_PATHS $CASES_FAILED"
' _ "$LIB" 2>&1)
itog "shell: совпадение печатает «ок» с именем" 1 "$(grep -c '^  ок    совпало$' <<< "$vyvod")"
itog "БОЛЬНОЙ СЛУЧАЙ shell: неудача называет ждали и вышло в том же порядке, что python" 1 \
    "$(grep -c '^  ПЛОХО разошлось: ждали «да», вышло «нет»$' <<< "$vyvod")"
itog "shell: пути и неудачи считает помощник" "счёт 2 1" "$(grep '^счёт' <<< "$vyvod")"

# Пустое «вышло» — тоже ответ: прибор, не сказавший ничего, не равен ждущему «0».
vyvod=$(bash -c 'source "$1/selftest_cases.sh"; case_check "пусто" 0 ""; echo "счёт $CASES_PATHS $CASES_FAILED"' _ "$LIB" 2>&1)
itog "БОЛЬНОЙ СЛУЧАЙ shell: пустой ответ не равен «0»" "счёт 1 1" "$(grep '^счёт' <<< "$vyvod")"

# Помощник подключается под set -u и set -e вызывающего и не роняет его на неудаче.
vyvod=$(bash -c 'set -euo pipefail; source "$1/selftest_cases.sh"; case_check "x" 1 2; echo дожил' _ "$LIB" 2>&1)
itog "shell: неудача под set -euo pipefail не роняет вызывающего" 1 "$(grep -c '^дожил$' <<< "$vyvod")"

printf 'SELFTEST-CASES: путей %d, неудач %d\n' "$PUTEJ" "$NEUDACH"
[ "$NEUDACH" = 0 ]
