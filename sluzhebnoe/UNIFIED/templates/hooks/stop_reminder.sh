#!/usr/bin/env bash
# Хук Stop: напомнить о хвостах, которые регулярно забываются.
#
# Молчит, когда всё в порядке. Напоминание, срабатывающее всегда, перестают
# читать через неделю — говорит только когда есть что сказать.
set -uo pipefail
cd "/opt/<проект>" 2>/dev/null || exit 0

msgs=()

ahead=$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)
[ "${ahead:-0}" -gt 0 ] && msgs+=("не отправлено в удалённый репозиторий: $ahead коммит(ов) — git push")

dirty=$(git status --porcelain 2>/dev/null | grep -vc '^??' || true)
[ "${dirty:-0}" -gt 0 ] && msgs+=("незакоммиченных изменений: $dirty")

# Коммит без трейлера Dev-Map — работа, невидимая владельцу на дашборде.
# Не блокирует: сверка при деплое заведёт пропуск и сама. Это лишь просьба
# сделать это осмысленно, пока помнишь, что именно делал.
today=0; tagged=0
for h in $(git log --since=midnight --format=%H 2>/dev/null); do
  today=$((today + 1))
  git log -1 --format=%B "$h" | grep -q '^Dev-Map:' && tagged=$((tagged + 1))
done
[ "$today" -gt "$tagged" ] && msgs+=("коммитов за сегодня $today, с отметкой Dev-Map $tagged — работа без отметки не попадёт на дашборд")

[ ${#msgs[@]} -eq 0 ] && exit 0

echo "=== перед завершением ==="
for m in "${msgs[@]}"; do echo "  ⚠ $m"; done
