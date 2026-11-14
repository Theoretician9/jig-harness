#!/usr/bin/env bash
# Проба строки «контейнеры» в session_state.sh: разовый контейнер (миграция),
# вышедший кодом 0, — норма, а не сбой; сбой — ненулевой код или restarting.
# Условие СОЗДАЁТСЯ пробой: заглушка docker в своём PATH, ответы из файла стенда.
# Дверь мутаций: SESSION_STATE_SH — путь к копии хука.
set -uo pipefail
export LC_ALL=C.UTF-8
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Раскладка проекта (scripts/hooks) — первой, пакета (harness/hooks) — запасной.
HOOK="${SESSION_STATE_SH:-$HERE/hooks/session_state.sh}"
[ -r "$HOOK" ] || HOOK="$HERE/../hooks/session_state.sh"
STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT
mkdir -p "$STAND/bin" "$STAND/proj"

cat > "$STAND/bin/docker" <<'STUB'
#!/bin/sh
# ps с фильтром restarting — своя выдача, с exited — своя (строки «имя статус»).
case "$*" in
    *status=restarting*) cat "$STAND_DIR/restarting" 2>/dev/null ;;
    *status=exited*) cat "$STAND_DIR/exited" 2>/dev/null ;;
esac
exit 0
STUB
chmod +x "$STAND/bin/docker"

passed=0; failed=0
judge() {  # $1 = имя случая; $2 = ждали (yes|no — есть ли строка сбоя); $3 = подстрока, которую ждём/не ждём в выдаче
    local out
    out=$(cd "$STAND/proj" && STAND_DIR="$STAND" PATH="$STAND/bin:/usr/bin:/bin" PROJECT_DIR="$STAND/proj" \
          CLAUDE_PROJECT_DIR="$STAND/proj" HOME="$STAND" SHIFT_SELF_MARK="$STAND/shift.mark" \
          bash "$HOOK" 2>&1 </dev/null)
    local has=no; echo "$out" | grep -q "контейнеры не в строю" && has=yes
    local named=no; echo "$out" | grep -q "$3" && named=yes
    if [ "$has" = "$2" ] && { [ "$2" = no ] || [ "$named" = yes ]; }; then
        echo "ок  · $1"; passed=$((passed + 1))
    else
        echo "ПЛОХО · $1: сбой=$has (ждали $2), подстрока «$3»=$named"; echo "$out" | grep -i "контейнер" | head -3
        failed=$((failed + 1))
    fi
}

: > "$STAND/restarting"
printf 'html-app-migrate-1 Exited (0) 2 hours ago\n' > "$STAND/exited"
judge "exited(0) — не сбой" no "migrate"
printf 'html-app-migrate-1 Exited (0) 2 hours ago\nhtml-job-1 Exited (1) 5 minutes ago\n' > "$STAND/exited"
judge "exited(1) — сбой, назван по имени" yes "html-job-1(exited 1)"
printf 'html-app-migrate-1 Exited (0) 2 hours ago\n' > "$STAND/exited"
printf 'html-web-1(restarting)\n' > "$STAND/restarting"
judge "restarting — сбой" yes "html-web-1(restarting)"
echo "итого путей $((passed + failed)), неудач $failed"
[ "$failed" = 0 ] && [ "$passed" -gt 0 ]
