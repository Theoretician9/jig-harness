#!/usr/bin/env bash
# Хук SessionStart: показать, в каком состоянии проект застала новая сессия.
#
# Без этого сессия начинается со слепого места: незакоммиченные правки прошлого
# сеанса, непринятая миграция или упавший контейнер обнаруживаются случайно и
# посреди работы. Дешевле напечатать это сразу.
#
# Печатает в stdout — харнес добавляет вывод в контекст сессии.
set -uo pipefail
cd "/opt/<проект>" 2>/dev/null || exit 0

echo "=== состояние проекта на старте сессии ==="

echo "ветка: $(git rev-parse --abbrev-ref HEAD 2>/dev/null) · последний коммит: $(git log -1 --format='%h %s' 2>/dev/null | cut -c1-80)"

ahead=$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)
[ "${ahead:-0}" -gt 0 ] && echo "⚠ не отправлено в удалённый репозиторий: $ahead коммит(ов)"

dirty=$(git status --porcelain 2>/dev/null | grep -v '^??' | wc -l)
[ "$dirty" -gt 0 ] && { echo "⚠ незакоммичено файлов: $dirty"; git status --short 2>/dev/null | grep -v '^??' | head -5 | sed 's/^/    /'; }

head_db=$(docker compose exec -T backend alembic current 2>/dev/null | tail -1 | tr -d '\r')
[ -n "$head_db" ] && echo "миграции: $head_db"

down=$(docker compose ps --status exited --status restarting --format '{{.Service}}' 2>/dev/null | tr '\n' ' ')
[ -n "$down" ] && echo "⚠ контейнеры не в строю: $down"

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8010/api/v1/health 2>/dev/null)
[ "$code" = "200" ] && echo "health: 200" || echo "⚠ health: ${code:-нет ответа}"

# Код в контейнере против рабочего дерева. Замечание соседнего проекта: при цикле
# «docker cp в живой контейнер» расхождение становится обычным состоянием, и тогда
# теряется единственный надёжный признак «выкат в пути». Знать, разошлось ли, надо
# всегда; ожидаемо это или нет — решает человек, глядя на строку.
if [ -z "$(git status --porcelain backend/app 2>/dev/null)" ]; then
  probe="app/main.py"
  mine=$(sha256sum "backend/$probe" 2>/dev/null | cut -c1-64)
  theirs=$(docker compose exec -T backend sha256sum "/app/$probe" 2>/dev/null | cut -c1-64)
  [ -n "$theirs" ] && [ "$mine" != "$theirs" ] && echo "⚠ код в контейнере разошёлся с деревом ($probe) — контейнер не пересобран"
fi

python3 scripts/check-context-hygiene.py 2>&1 | sed 's/^/  /'
