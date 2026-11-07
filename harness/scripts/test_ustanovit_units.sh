#!/usr/bin/env bash
# Проба: шаблоны юнитов в USTANOVIT.sh (sentinel-boot и dispatcher) не расходятся с живыми.
#
# Откуда взята. 03.11.2026 после перезагрузки harness-sentinel-boot отработал раньше стены
# и docker и дал ложное «НЕ ПОДНЯЛСЯ»: в After= не было юнитов, которые проверяет
# boot-check.yaml. И в шаблоне dispatcher не было PANEL_SECRETS_DIR, которое есть в живом юните.
# Блок шаблона достаётся ПАРСИНГОМ установщика (от записать_файл до EOF); пустой блок — провал.
# Дверь мутаций: USTANOVIT_UNDER_TEST — установщик, который судит проба.
set -u
here=$(cd "$(dirname "$0")/.." && pwd)
src=${USTANOVIT_UNDER_TEST:-$here/../USTANOVIT.sh}
fail=0
block() {
  awk -v unit="$1" '
    index($0, "записать_файл /etc/systemd/system/" unit ".service") {on=1; next}
    on && /^EOF$/ {exit}
    on {print}' "$src"
}
check() { # блок, шаблон, описание
  if grep -Eq -- "$2" <<<"$1"; then echo "ok:   $3"; else echo "FAIL: $3"; fail=1; fi
}
boot=$(block harness-sentinel-boot)
disp=$(block harness-dispatcher)
[ -n "$boot" ] || { echo "FAIL: блок harness-sentinel-boot не найден"; exit 1; }
[ -n "$disp" ] || { echo "FAIL: блок harness-dispatcher не найден"; exit 1; }
for u in docker nginx php8.3-fpm harness-panel harness-firewall; do
  check "$(grep '^After=' <<<"$boot")" "[ =]$u\.service( |$)" "sentinel-boot: After= содержит $u"
done
for u in docker nginx php8.3-fpm harness-panel harness-firewall; do
  check "$(grep '^Wants=' <<<"$boot")" "[ =]$u\.service( |$)" "sentinel-boot: $u желаемый (Wants)"
done
if grep -q '^Requires=' <<<"$boot"; then echo "FAIL: sentinel-boot: Requires= запрещён"; fail=1; fi
check "$disp" '^Environment=PANEL_SECRETS_DIR=/var/lib/harness/panel-state$' "dispatcher: PANEL_SECRETS_DIR в шаблоне"
[ $fail = 0 ] && echo "ВСЁ ЗЕЛЁНОЕ" || echo "КРАСНОЕ"
exit $fail
