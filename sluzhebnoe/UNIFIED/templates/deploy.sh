#!/usr/bin/env bash
# Деплой backend с гарантированным порядком: build -> migrate -> health -> dev-map снимок.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose up -d --build backend worker beat
docker compose exec -T backend alembic upgrade head
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8010/api/v1/health || true)
  [ "$code" = "200" ] && break
  sleep 2
done
[ "$code" = "200" ] || { echo "health не поднялся"; exit 1; }
docker compose exec -T backend python -m app.workers.dev_map_run
echo "deploy ok: health 200, dev-map снимок обновлён"

# Работа, прошедшая мимо карты, чинится здесь же. Деплой — единственный путь,
# которым код попадает на прод: пропустить его, оставив работу работающей,
# невозможно, поэтому проверка тут надёжнее любого запрета на коммит.
# Не блокирует деплой: карта не должна мешать выкатке.
./scripts/devmap-selfheal.sh 14 || echo "сверка карты не отработала (деплой не затронут)"

# Автопамять называет проект именем каталога, в котором стояла оболочка, и внутри
# сессии это имя «залипает»: один заход в подкаталог — и дальнейшие записи уезжают
# под чужое имя, а при восстановлении поиск по проекту их не найдёт. Настройки,
# которая это чинит, у плагина нет — значит сводим осколки здесь же, где и пропуски
# в карте. Не блокирует деплой.
python3 scripts/fix-claude-mem-project.py || echo "сведение имён автопамяти не отработало (деплой не затронут)"
