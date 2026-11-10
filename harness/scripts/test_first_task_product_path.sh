#!/usr/bin/env bash
# Проба блока «ПЕРВАЯ ЗАДАЧА» хука старта сессии: копия-проект без пути к коду
# продукта (PRODUCT_PATH) получает требование, с путём — тишину, наша копия-харнес
# молчит всегда. PRODUCT_DIR — имя compose-проекта, путём он не считается.
# Условие создаёт проба: свой каталог конфигов (HARNESS_HOME), свой проект с
# instance.yaml. Дверь мутаций: SESSION_STATE_SH — путь к копии хука.
set -uo pipefail
export LC_ALL=C.UTF-8
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"
HOOK="${SESSION_STATE_SH:-$HERE/hooks/session_state.sh}"
STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT
mkdir -p "$STAND/home" "$STAND/proj/harness/self" "$STAND/bin"
# docker и systemctl хуку не нужны для этого блока: пустые заглушки, живое не спрашивается.
printf '#!/bin/sh\nexit 0\n' > "$STAND/bin/docker"
printf '#!/bin/sh\nexit 0\n' > "$STAND/bin/systemctl"
chmod +x "$STAND/bin/"*

# $1 = имя проекта, $2 = роль, $3 = PRODUCT_PATH, $4 = PRODUCT_DIR → первые строки хука
run_hook() {
    printf 'PROJECT_NAME=%s\nPROJECT_DIR=%s\nLOG_DIR=%s\n' "$1" "$STAND/proj" "$STAND/log" > "$STAND/home/install.conf"
    printf 'PRODUCT_PATH="%s"\nPRODUCT_DIR="%s"\n' "$3" "$4" > "$STAND/home/harness.conf"
    printf 'копия: %s\nроль: %s\nцель: проба\n' "$1" "$2" > "$STAND/proj/harness/self/instance.yaml"
    (cd "$STAND/proj" && env -u PRODUCT_PATH -u PRODUCT_DIR -u PROJECT_NAME -u PROJECT_DIR \
        PATH="$STAND/bin:/usr/bin:/bin" HARNESS_HOME="$STAND/home" \
        HARNESS_INSTALL_CONF="$STAND/home/install.conf" HARNESS_CONF="$STAND/home/harness.conf" \
        timeout 60 bash "$HOOK" </dev/null 2>/dev/null | head -20)
}
has() { grep -qF -- "$2" <<<"$1" && echo да || echo нет; }

OUT=$(run_hook shop "агент проекта shop" "" "")
case_check "БОЛЬНОЙ СЛУЧАЙ: копия-проект, PRODUCT_PATH пуст — первая задача названа" "да" "$(has "$OUT" "ПЕРВАЯ ЗАДАЧА")"
case_check "причина названа ключом PRODUCT_PATH" "да" "$(has "$OUT" "PRODUCT_PATH пуст")"
case_check "просьба записать путь в PRODUCT_PATH, а не в PRODUCT_DIR" "да/нет" \
    "$(has "$OUT" "в PRODUCT_PATH (harness.conf)")/$(has "$OUT" "в PRODUCT_DIR")"

OUT=$(run_hook shop "агент проекта shop" "" "shop")
case_check "БОЛЬНОЙ СЛУЧАЙ: задано только имя compose (PRODUCT_DIR) — путь всё равно требуется" "да" "$(has "$OUT" "PRODUCT_PATH пуст")"

OUT=$(run_hook shop "агент проекта shop" "$STAND/product" "")
case_check "PRODUCT_PATH задан — первой задачи нет" "нет" "$(has "$OUT" "ПЕРВАЯ ЗАДАЧА")"

OUT=$(run_hook project "агент самого харнеса" "" "")
case_check "наша копия-харнес без продукта — молчит" "нет" "$(has "$OUT" "ПЕРВАЯ ЗАДАЧА")"
case_check "хук дошёл до отчёта (стенд не пустой)" "да" "$(has "$OUT" "состояние на старте сессии")"

printf '\nПЕРВАЯ ЗАДАЧА И PRODUCT_PATH: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ] && [ "$CASES_PATHS" -gt 0 ]
