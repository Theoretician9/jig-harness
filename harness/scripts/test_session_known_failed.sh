#!/usr/bin/env bash
# Проба: упавший юнит хостинга из boot-check.yaml → «известный отказ», а не «упавшие юниты».
# Свой стенд: поддельный systemctl и копия перечня; блок берётся из хука по маркерам.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Раскладка проекта первой, пакета — запасной.
HOOK="${SESSION_STATE_UNDER_TEST:-$HERE/hooks/session_state.sh}"
[ -r "$HOOK" ] || HOOK="$HERE/../hooks/session_state.sh"
LIST="$HERE/../harness/config/boot-check.yaml"
[ -r "$LIST" ] || LIST="$HERE/../config/boot-check.yaml"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/proj/harness/config"
cp "$LIST" "$T/proj/harness/config/"
cat > "$T/bin/systemctl" <<'SH'
#!/bin/bash
for u in $FAKE_UNITS; do echo "$u loaded failed failed x"; done
SH
chmod +x "$T/bin/systemctl"
{ echo "project_dir=$T/proj"
  sed -n '/^# --- упавшие systemd-юниты/,/^# --- контейнеры/p' "$HOOK" | head -n -1; } > "$T/blk.sh"
[ "$(wc -l < "$T/blk.sh")" -gt 5 ] || { echo "ПЛОХО: блок юнитов в хуке не найден"; exit 1; }
fail=0
run() { FAKE_UNITS="$1" PATH="$T/bin:$PATH" bash "$T/blk.sh" 2>&1; }
check() { # имя, вывод, шаблон-есть, шаблон-нет
    if grep -q "$3" <<<"$2" && ! grep -q "$4" <<<"$2"; then echo "  ок    $1"
    else echo "  ПЛОХО $1: $2"; fail=1; fi
}
check "cloud-init один — не красный, назван известным" "$(run cloud-init.service)" 'известный отказ.*cloud-init' 'упавшие'
check "cloud-init и чужой — красный только чужой" "$(run 'cloud-init.service foo.service')" 'упавшие systemd-юниты: foo.service $' 'упавшие.*cloud-init'
check "чужой юнит — красный" "$(run foo.service)" 'упавшие systemd-юниты: foo.service' 'известный'
check "похожее имя не гасится" "$(run my-cloud-init.service)" 'упавшие systemd-юниты: my-cloud-init' 'известный'
echo "упало: $fail"; exit "$fail"
