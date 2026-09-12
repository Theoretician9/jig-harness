#!/usr/bin/env bash
# СКВОЗНОЙ-ПРОГОН-ВОЗМОЖНОСТЕЙ.sh — проверка пути ЦЕЛИКОМ, а не деталей.
#
# Селфтесты доказывают детали: менеджер, установщик, гейт, сторож. Но путь
# «раскладка → установка набора → врастание во ВСЕ носители → снятие» ни один
# из них не проходит, а именно на пути живут беды сборки (UNIFIED/08, тип 7:
# тест проверяет функцию, путь проверяет только путь).
#
# Улика, ради которой этот прогон и появился: 11.08 он нашёл дефект, который все
# 12 селфтестов пропустили — раскладчик не доставлял на сервер `skills-src/`,
# и любая пересборка скилов на сервере падала. Установка набора при этом честно
# говорила «скилы не собраны», но заметить это можно было только пройдя путь.
#
# Сеть, sudo и Android в прогоне заглушены (HARNESS_PACK_SELFTEST=1,
# SUDO_FORBIDDEN=1): проверяется МЕХАНИЗМ, а не наличие Android SDK.
# Живая часть (эмулятор, Test Lab, стор) — разделы Д–Е чек-листа набора.
#
# Запуск: bash СКВОЗНОЙ-ПРОГОН-ВОЗМОЖНОСТЕЙ.sh   (из каталога пакета)
# Код возврата: 0 — путь пройден целиком, 1 — назван шаг и проверка, что упала.
set -uo pipefail
export LC_ALL=C.UTF-8
PKG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T=$(mktemp -d)
bad=0
ok()  { printf '  ок    %s\n' "$1"; }
no()  { printf '  ПЛОХО %s\n' "$1"; bad=$((bad+1)); }
chk() { if eval "$1" >/dev/null 2>&1; then ok "$2"; else no "$2"; fi; }

mkdir -p "$T/home" "$T/log" "$T/hb" "$T/secrets"
: > "$T/home/.bashrc"
cat > "$T/install.conf" <<EOF
PROJECT_NAME="проба"
PROJECT_DIR="$T/proj"
AGENT_USER="$(id -un)"
LOG_DIR="$T/log"
HEARTBEAT_DIR="$T/hb"
SECRETS_DIR="$T/secrets"
AUTONOMY="semi"
EOF
export HARNESS_INSTALL_CONF="$T/install.conf"
export HARNESS_CONF="$PKG/харнес/config/harness.conf"

echo "=== 1. Раскладка харнеса"
HARNESS_SELFTEST=1 bash "$PKG/харнес/разложить-харнес.sh" >/dev/null 2>&1
chk "[ -f '$T/proj/scripts/возможность.sh' ]" "менеджер возможностей доставлен"
chk "[ -f '$T/proj/харнес/возможности/мобильная-разработка/МАНИФЕСТ.conf' ]" "набор доставлен"
chk "! ls '$T/proj/харнес/skills' | grep -q mobile" "скилов набора НЕТ до установки (контекст чист)"

echo "=== 2. Список и требования до установки"
out=$(bash "$T/proj/scripts/возможность.sh" список 2>&1)
grep -q '\[ не стоит  \] мобильная-разработка' <<< "$out" && ok "список: набор виден как «не стоит»" || no "список: набор не показан"
grep -q 'условие: в задаче продукта' <<< "$out" && ok "список: условие включения названо" || no "список: нет условия"
out=$(HOME="$T/home" bash "$T/proj/scripts/возможность.sh" требования мобильная-разработка 2>&1)
grep -qE '\[(ок |НЕТ| ~ )\] диск' <<< "$out" && ok "требования: диск замерен вещью (df)" || no "требования: диск не замерен"
grep -q 'kvm' <<< "$out" && ok "требования: KVM проверен вещью, с адресом" || no "требования: KVM не проверен"

# Шаги 3–9 проверяют МЕХАНИЗМ установки, а не тариф этой машины. Жёсткое
# требование памяти замерено выше вещью (шаг 2) — здесь оно снимается в
# ВРЕМЕННОЙ копии паспорта, иначе прогон зеленеет только на серверах ≥4 ГБ, а
# на меньших валит 20 проверок подряд из-за честного отказа установщика.
# Улика приёмки 11.08.2026: VPS 3546 МБ — отказ верный, прогон был неверен.
sed -i 's/^NEED_MEM_TOTAL_MB=.*/NEED_MEM_TOTAL_MB=0/' \
  "$T/proj/харнес/возможности/мобильная-разработка/МАНИФЕСТ.conf"

echo "=== 3. Сухой прогон установки"
HOME="$T/home" HARNESS_PACK_SELFTEST=1 SUDO_FORBIDDEN=1 \
  bash "$T/proj/scripts/возможность.sh" поставить мобильная-разработка --dry-run >/dev/null 2>&1
chk "[ ! -f '$T/proj/харнес/возможности/мобильная-разработка/.установлено' ]" "--dry-run не поставил метку"

echo "=== 4. Установка (сеть, sudo и Android заглушены — проверяется механизм)"
HOME="$T/home" HARNESS_PACK_SELFTEST=1 SUDO_FORBIDDEN=1 \
  bash "$T/proj/scripts/возможность.sh" поставить мобильная-разработка > "$T/установка.log" 2>&1
rc=$?
[ "$rc" = 0 ] && ok "установка завершилась нулём" || { no "установка вернула $rc"; tail -20 "$T/установка.log"; }
chk "[ -f '$T/proj/харнес/возможности/мобильная-разработка/.установлено' ]" "метка .установлено поставлена"
chk "[ -f '$T/home/.config/harness-mobile.conf' ]" "конфиг петли собран"
chk "[ -x '$T/home/bin/mobile-loop' ]" "скрипты петли в ~/bin исполняемы"
chk "grep -q 'ОСТАЛОСЬ РУКАМИ' '$T/установка.log'" "остаток руками назван вслух"
chk "[ -f '$T/log/mobile/системный-шаг.sh' ]" "sudo-шаг выписан файлом владельцу"

echo "=== 5. Врастание в носители"
chk "[ -f '$T/proj/харнес/skills/mobile-testing/SKILL.md' ]" "скилы набора появились"
chk "grep -q 'name: android-env' '$T/proj/харнес/skills/android-env/SKILL.md'" "скил android-env собран"
chk "grep -q 'Когда' '$T/proj/харнес/skills/mobile-release/SKILL.md'" "у скила есть условие срабатывания"
chk "grep -q 'мобильная-петля' '$T/proj/память/MEMORY.md'" "строки памяти вросли в указатель"
chk "[ -f '$T/proj/память/feedback-мобильная-петля.md' ]" "запись памяти скопирована"
chk "grep -q '\\*.apk' '$T/proj/.gitignore'" ".gitignore получил строки набора"
chk "[ -f '$T/proj/docs/возможности/мобильная-разработка-РУКАМИ.md' ]" "реестр действий владельца на месте"
chk "grep -q '\"событие\":\"установлена\"' '$T/log/возможности.jsonl'" "запись в реестре возможностей"

echo "=== 6. Сторож: запреты набора ожили"
guard() { printf '{"tool_name":"Bash","tool_input":{"command":%s}}' "$1" \
          | python3 "$T/proj/scripts/hooks/guard_bash.py" >/dev/null 2>&1; echo $?; }
[ "$(guard '"gcloud firebase test android run --app a.apk"')" = 2 ] \
  && ok "Test Lab без потолка ОТМЕНЁН" || no "Test Lab без потолка прошёл"
[ "$(guard '"gcloud firebase test android run --app a.apk --timeout 5m"')" = 0 ] \
  && ok "Test Lab с потолком пропущен" || no "Test Lab с потолком отменён (ложное срабатывание)"
[ "$(guard '"emulator -avd dev"')" = 2 ] && ok "ручной эмулятор ОТМЕНЁН" || no "ручной эмулятор прошёл"
[ "$(guard '"git status --short"')" = 0 ] && ok "обычная команда проходит" || no "обычная команда отменена"

echo "=== 7. Уборщик: каталоги набора в его списке"
mkdir -p /tmp/shots-проба
out=$(bash "$T/proj/харнес/demons/disk-cleanup.sh" --dry-run 2>&1)
grep -q '5-а' <<< "$out" && ok "шаг 5-а прочитал уборка.list установленного набора" || { no "шаг 5-а не сработал"; grep -c . <<< "$out"; }
grep -q '5-а.*каталога нет' <<< "$out" && ok "отсутствующий каталог — пропуск с записью, не молчание" || no "пропуск каталога не назван"

echo "=== 8. Гейт коммита: правка экрана требует свежей петли"
cd "$T/proj" || exit 1
git init -q . 2>/dev/null; git config user.email a@b; git config user.name a
python3 - "$T/home/.config/harness-mobile.conf" "$T/proj/app" <<'PY'
import pathlib, sys, re
p = pathlib.Path(sys.argv[1]); t = p.read_text(encoding="utf-8")
p.write_text(t.replace('APP_DIR=""', 'APP_DIR="%s"' % sys.argv[2]), encoding="utf-8")
PY
mkdir -p app && echo "export const Экран = () => null" > app/Экран.tsx
git add app/Экран.tsx
out=$(HOME="$T/home" PROJECT_DIR="$T/proj" bash "$T/proj/scripts/pre-commit-hook.sh" 2>&1)
grep -q 'GATE FAIL' <<< "$out" && ok "БОЛЬНОЙ СЛУЧАЙ: правка экрана без петли → гейт красный" || no "гейт пропустил правку экрана без петли"
grep -q 'mobile-loop' <<< "$out" && ok "гейт назвал адрес (mobile-loop)" || no "гейт не назвал адрес"
mkdir -p "$T/log/mobile" && printf '{"итог": "ок"}\n' > "$T/log/mobile/последняя-петля.json"
out=$(HOME="$T/home" PROJECT_DIR="$T/proj" bash "$T/proj/scripts/pre-commit-hook.sh" 2>&1)
grep -q 'все гейты пройдены' <<< "$out" && ok "со свежим следом петли гейт зелёный" || { no "гейт красный при свежем следе"; tail -5 <<< "$out"; }

echo "=== 9. Снятие набора"
out=$(HOME="$T/home" bash "$T/proj/scripts/возможность.sh" снять мобильная-разработка 2>&1)
chk "! ls '$T/proj/харнес/skills' | grep -q mobile" "скилы набора исчезли после снятия"
[ "$(guard '"emulator -avd dev"')" = 0 ] && ok "запреты набора замолчали после снятия" || no "запреты действуют после снятия"
grep -q 'ОСТАЛОСЬ' <<< "$out" && ok "снятие честно называет, что осталось на диске" || no "снятие молчит про остатки"

cd "$PKG" || exit 1
rm -rf "$T" /tmp/shots-проба
echo
if [ "$bad" = 0 ]; then echo "СКВОЗНОЙ ПРОГОН: зелёный, неудач 0"; else echo "СКВОЗНОЙ ПРОГОН: неудач $bad"; exit 1; fi
