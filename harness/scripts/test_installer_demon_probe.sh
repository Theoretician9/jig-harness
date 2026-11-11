#!/usr/bin/env bash
# Проба: шаг 13 установщика — прогон демонов в env -i (Д-20, Д-21, живая установка R7 11.11.2026).
#
# Д-20: проверке запускаемости ночных ворот и мутаций шаг 13 подставляет команду
#   `true`. Она не печатает строку итога, и демоны считают прогон оборванным:
#   выход 1 и тревога владельцу «прогон оборвался» на КАЖДОЙ установке.
#   Сверх того: лёгкий прогон не имеет права выдать себя за настоящий итог и
#   погасить тревогу ночи (alert-task --resolve), которой он не проверял.
# Д-21: check-login-expiry до OAuth-входа выходит с кодом 2 («входа ещё нет»), а
#   шаг 13 идёт ДО входа (пункт «а» ОСТАЛОСЬ РУКАМИ) — ожидание записано отказом,
#   и установка выходит с кодом 1 всегда.
#
# Условие проба создаёт сама. Шаг 13 — настоящая функция установщика (извлечена
# разбором; пустое извлечение — провал), список демонов — из реестра пакета.
# sudo подставной (без смены пользователя); env -i настоящий — до демона доходит
# ровно то, что передаёт шаг. Подставная обёртка run-background.sh стенда
# возвращает окружение стенда и запускает: nightly-gates и nightly-mutations —
# НАСТОЯЩИЕ демоны (копии в проекте стенда), check-login-expiry — как настоящий
# до входа (код из файла стенда), остальные — выход 0. Канал и писатель тревог
# подставные. Пользователь агента — несуществующий: файла входа у него нет.
# Боевые /etc/harness, /var/log/harness и crontab не трогаются.
# Двери мутаций: USTANOVIT_UNDER_TEST, NIGHTLY_GATES_UNDER_TEST, NIGHTLY_MUTATIONS_UNDER_TEST.
set -u
here=$(cd "$(dirname "$0")/.." && pwd)
# Проба лежит и в пакете (harness/scripts), и в проекте (scripts/), а судит пакет:
# в проекте рядом нет ни установщика, ни harness/demons — путь берётся у конфига.
[ -f "$here/../USTANOVIT.sh" ] || here="$(python3 -c "import sys; sys.path.insert(0, '$here/scripts/lib'); import config; print(config.katalog_paketa() / 'harness')" 2>/dev/null || true)"
src=${USTANOVIT_UNDER_TEST:-$here/../USTANOVIT.sh}
[ -f "$src" ] || { echo "SKIP: установщик $src не найден (проба живёт в пакете)"; exit 77; }
gates_src=${NIGHTLY_GATES_UNDER_TEST:-$here/demons/nightly-gates.sh}
muts_src=${NIGHTLY_MUTATIONS_UNDER_TEST:-$here/demons/nightly-mutations.sh}
spec="$here/config/system-jobs.yaml"
for f in "$gates_src" "$muts_src" "$spec" "$here/scripts/lib/config.sh"; do
  [ -f "$f" ] || { echo "FAIL: нет $f — стенд не собрать"; exit 1; }
done
fail=0; total=0
ok()  { total=$((total + 1)); echo "ok:   $1"; }
bad() { total=$((total + 1)); echo "FAIL: $1"; fail=$((fail + 1)); }
t=$(mktemp -d "${TMPDIR:-/tmp}/harness-run-XXXXXX"); trap 'rm -rf "$t"' EXIT

extract() { awk -v f="$1" 'index($0, f "() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src"; }
: > "$t/lib.sh"
for f in шаг_13 демоны_из_спеки команда_скрипта; do
  body=$(extract "$f")
  [ -n "$body" ] || { echo "FAIL: функция $f не найдена в установщике"; exit 1; }
  printf '%s\n' "$body" >> "$t/lib.sh"
done

# ── проект стенда ──
P="$t/proekt"
mkdir -p "$P/harness/demons" "$P/scripts" "$t/etc" "$t/log" "$t/hb" "$t/instlog" "$t/events"
ln -s "$here/scripts/lib" "$P/scripts/lib"
cp "$gates_src" "$P/harness/demons/nightly-gates.sh"
cp "$muts_src" "$P/harness/demons/nightly-mutations.sh"
: > "$t/etc/install.conf"; : > "$t/etc/harness.conf"
cat > "$P/scripts/tg_send.sh" <<TG
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$t/tg.log"
TG
cat > "$P/scripts/alert-task.py" <<AT
import sys
with open("$t/alert.log", "a", encoding="utf-8") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
AT
cat > "$P/scripts/run-background.sh" <<RB
#!/usr/bin/env bash
# Обёртка стенда: env -i снял всё — окружение стенда возвращается здесь, а
# переданное шагом (лёгкая работа) остаётся как есть.
id="\$1"; shift 2
export LC_ALL=C.UTF-8 HARNESS_HOME="$t/etc" HARNESS_INSTALL_CONF="$t/etc/install.conf" \\
  HARNESS_CONF="$t/etc/harness.conf" PROJECT_DIR="$P" LOG_DIR="$t/log" \\
  HEARTBEAT_DIR="$t/hb" HARNESS_EVENTS_STATE="$t/events"
echo "\$id" >> "$t/ran.log"
case "\$id" in
  nightly-gates|nightly-mutations) exec bash "\$@" ;;
  check-login-expiry)
    echo "[срок-входа] срок входа прочитать нечем: нет файла входа или в нём нет полей срока — проверить вручную"
    echo "[аккаунт] входить нечем: ни переменной, ни ключа подписки в файле входа"
    exit "\$(cat "$t/login.rc")" ;;
  *) exit 0 ;;
esac
RB
chmod +x "$P/scripts/tg_send.sh" "$P/scripts/run-background.sh"

cat > "$t/run13.sh" <<'RUN'
# $1 = lib, $2 = каталог стенда; печатает свод шага: строки FAILED/MANUAL/CHECKED
. "$1"; T=$2
заголовок() { :; }; скажи() { printf '%s\n' "$*"; }
уже_сделан() { return 1; }; отметить() { :; }
sudo() { [ "$1" = -u ] && shift 2; "$@"; }
DRY=0; PROJECT_DIR="$T/proekt"; LOG_DIR="$T/instlog"; HEARTBEAT_DIR="$T/hb"
AGENT_USER="harness-probe-nouser-$$"; SCRIPT_DIR="$SCRIPT_DIR_PKG"
DONE_LIST=(); CHECKED_LIST=(); FAILED_LIST=(); MANUAL_LIST=()
шаг_13 > "$T/step13.out" 2>&1
for x in ${FAILED_LIST[@]+"${FAILED_LIST[@]}"}; do printf 'FAILED %s\n' "$x"; done
for x in ${MANUAL_LIST[@]+"${MANUAL_LIST[@]}"}; do printf 'MANUAL %s\n' "$x"; done
for x in ${CHECKED_LIST[@]+"${CHECKED_LIST[@]}"}; do printf 'CHECKED %s\n' "$x"; done
RUN
run13() {  # $1 = код check-login-expiry стенда
  printf '%s\n' "$1" > "$t/login.rc"
  : > "$t/tg.log"; : > "$t/alert.log"; : > "$t/ran.log"
  rm -rf "$t/log" "$t/hb" "$t/events"; mkdir -p "$t/log" "$t/hb" "$t/events"
  SCRIPT_DIR_PKG="$(dirname "$here")" timeout 300 bash "$t/run13.sh" "$t/lib.sh" "$t"
}
# Строка FAILED, где назван демон: «демон <имя>: …».
failed_for() { grep -q "^FAILED .*демон $1:" <<<"$2"; }

# ── условие: шаг гоняет демонов из реестра, среди них все три судимых ──
svod=$(run13 2)
for d in nightly-gates nightly-mutations check-login-expiry; do
  grep -qx "$d" "$t/ran.log" && ok "условие стенда: шаг 13 прогнал $d" \
    || bad "условие стенда: шаг 13 не прогнал $d (прогнаны: $(tr '\n' ' ' < "$t/ran.log"))"
done
[ "$(grep -c . "$t/ran.log")" -ge 10 ] && ok "условие стенда: прогнано демонов $(grep -c . "$t/ran.log")" \
  || bad "условие стенда: прогнано демонов меньше 10 — список не разобран"

# ── Д-20: первый прогон ночных демонов, поставленных установщиком ──
for d in nightly-gates nightly-mutations; do
  failed_for "$d" "$svod" \
    && bad "Д-20: $d на проверке запускаемости — отказ шага: $(grep "^FAILED .*демон $d:" <<<"$svod" | cut -c1-120)" \
    || ok "Д-20: $d на проверке запускаемости — не отказ (выход 0)"
  grep -q "$d:broken" "$t/tg.log" \
    && bad "Д-20: $d на проверке запускаемости шлёт владельцу тревогу «прогон оборвался» ($d:broken)" \
    || ok "Д-20: $d — тревоги «прогон оборвался» нет"
  grep -q -- "--resolve $d:" "$t/alert.log" \
    && bad "Д-20: лёгкий прогон $d гасит тревогу ночи ($(grep -- "--resolve $d:" "$t/alert.log" | tr '\n' ' ')) — итога он не проверял" \
    || ok "Д-20: лёгкий прогон $d тревог ночи не гасит"
done
grep -q "ОТКАЗ: прогон закончился без итога" "$t/instlog/install-demon-probe.log" 2>/dev/null \
  && bad "Д-20: в журнале шага «ОТКАЗ: прогон закончился без итога»" \
  || ok "Д-20: в журнале шага нет «прогон закончился без итога»"

# ── Д-21: вход ещё не выполнен — не отказ и не причина кода 1 ──
failed_for check-login-expiry "$svod" \
  && bad "Д-21: check-login-expiry до входа (код 2, файла входа нет) записан в «УПАЛО»" \
  || ok "Д-21: check-login-expiry до входа — не отказ"
nfailed=$(grep -c '^FAILED ' <<<"$svod")
[ "$nfailed" = 0 ] && ok "Д-20/Д-21: остальные демоны зелёные — отказов шага 0, установка выйдет с кодом 0" \
  || bad "Д-20/Д-21: отказов шага $nfailed — установщик выйдет с кодом 1: $(grep '^FAILED ' <<<"$svod" | sed 's/ — смотреть.*//' | tr '\n' ';')"

# ── контроль: настоящий отказ сторожа входа остаётся отказом ──
svod=$(run13 1)
failed_for check-login-expiry "$svod" \
  && ok "контроль: check-login-expiry с кодом 1 — отказ шага" \
  || bad "контроль: check-login-expiry с кодом 1 не записан в «УПАЛО» — шаг глотает настоящий отказ"

echo "итого случаев $total, неудач $fail"
[ "$fail" = 0 ]
