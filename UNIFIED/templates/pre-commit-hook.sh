#!/usr/bin/env bash
# Единственный источник истины по гейтам коммита.
#
# Симлинк: ln -sf ../../.tasks/_config/pre-commit-hook.sh .git/hooks/pre-commit
#
# Набор гейтов выбирается по составу staged-файлов: тронут backend/ — тесты бэка,
# тронут frontend/ — тесты и типы, появилась миграция — сухой прогон. Тир задаётся
# переменной TIER (по умолчанию T2) и определяет глубину:
#
#   T1 — тривиальное (правка документации, манифеста, конфига): гигиена + типы фронта
#   T2 — обычное: гигиена + тесты бэка + тесты фронта + типы
#   T3 — тяжёлое (миграция, инфраструктура, релиз): всё T2 + сборки + health
#
# Рядом лежали gates.yaml и policy.yaml, описывавшие то же самое ещё раз. Замер
# 08.08.2026: их не читал НИКТО, кроме моей же документации; policy.yaml с 27 мая
# правился один раз, в день заведения. Объявленные там бюджеты в токенах на тир не
# мерились ничем — счётчика в харнесе нет вовсе, то есть «превышение бюджета» не
# могло наступить никогда. Второй источник истины, который никто не читает, — это
# не документация, а ложь про устройство: удалён, гейты живут здесь.
#
# Ненулевой код возврата отменяет коммит.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

CHANGED=$(git diff --cached --name-only)
TIER="${TIER:-T2}"     # Override via TIER=T3 git commit ... ; defaults to T2
echo "[pre-commit] tier=$TIER, staged files:"
echo "$CHANGED" | sed 's/^/  /'

fail() {
    echo "[pre-commit] GATE FAIL: $1" >&2
    exit 1
}

# Гигиена того, что едет в контекст каждой сессии: размер STATE.md, единственность
# памяти, живость @-include. Всё это уже ломалось (CLAUDE.md §17, §18), и каждый раз
# правило держалось вниманием — то есть не держалось. Проверка дешёвая, гоняем всегда.
python3 scripts/check-context-hygiene.py || fail "context_hygiene"

# Always-required gates (T2/T3): backend + frontend tests + frontend types.
case "$TIER" in
    T2|T3)
        if echo "$CHANGED" | grep -qE "^backend/"; then
            echo "[pre-commit] running backend tests..."
            docker compose exec -T backend python -m pytest tests/ --tb=line -q \
                || fail "backend_tests"
        fi
        if echo "$CHANGED" | grep -qE "^frontend/"; then
            echo "[pre-commit] running frontend tests..."
            ( cd frontend && npx vitest run --reporter=basic ) \
                || fail "frontend_tests"
            echo "[pre-commit] running tsc --noEmit..."
            ( cd frontend && npx tsc --noEmit ) \
                || fail "frontend_types"
        fi
        ;;
    T1)
        if echo "$CHANGED" | grep -qE "^frontend/"; then
            ( cd frontend && npx tsc --noEmit ) || fail "frontend_types"
        fi
        ;;
esac

# T3-only: build + health checks
if [ "$TIER" = "T3" ]; then
    if echo "$CHANGED" | grep -qE "^(backend/|docker-compose\.yml)"; then
        echo "[pre-commit] T3: rebuilding backend stack..."
        docker compose up -d --build backend worker worker_media beat || fail "backend_build"
    fi
    if echo "$CHANGED" | grep -qE "^(frontend/|docker-compose\.yml)"; then
        echo "[pre-commit] T3: rebuilding frontend..."
        docker compose up -d --build frontend || fail "frontend_build"
    fi
    if echo "$CHANGED" | grep -qE "^livekit/"; then
        echo "[pre-commit] T3: rebuilding livekit stack..."
        docker compose up -d --build livekit livekit-egress || fail "livekit_build"
    fi
    # Allow services a moment to come up before health check
    sleep 4
    echo "[pre-commit] T3: health checks..."
    curl -fsS http://127.0.0.1:8010/api/v1/health || fail "health"
    test "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 https://<домен>/)" = "200" || fail "site_health"
fi

# Migration detection
if echo "$CHANGED" | grep -qE "^backend/migrations/versions/"; then
    echo "[pre-commit] migration detected — running dry-run..."
    docker compose exec -T backend alembic upgrade head --sql > /dev/null || fail "migration_dry_run"
fi

echo "[pre-commit] all gates passed for tier=$TIER"
exit 0
