# shellcheck shell=bash
# Общий помощник самотестов (подключается через source): случай
# «ждём/вышло → ок/ПЛОХО» и его счёт. Порядок доводов тот же, что у пары
# selftest_cases.py: имя, ждём, вышло. Счёт ведёт помощник — число путей в
# итоговой строке руками не вписывается.
CASES_PATHS=0
CASES_FAILED=0

case_check() { # $1 = имя случая, $2 = ждём, $3 = вышло
    CASES_PATHS=$((CASES_PATHS + 1))
    if [ "$2" = "$3" ]; then
        printf '  ок    %s\n' "$1"
    else
        CASES_FAILED=$((CASES_FAILED + 1))
        printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$3"
    fi
    return 0
}
