#!/usr/bin/env bash
# Проба: первый коммит установщика — не «коммит мимо карты» (Д-22, живая установка R7 11.11.2026).
#
# Шаг 11 установщика делает первый коммит каркаса дверью пайплайна (HARNESS_PIPELINE_FORCE,
# причина «первый коммит каркаса: делает установщик»). Трейлера Dev-Map у него нет и
# быть не должно: в карте ещё нет ни одной задачи. devmap-selfheal считал его пропуском
# и звал модель: до OAuth-входа — отказ и выход 1 на шаге 13, после входа — оплаченный
# вызов модели на «починку» коммита установщика.
#
# Условие проба создаёт сама: свой репозиторий, коммит в нём делает НАСТОЯЩАЯ функция
# установщика первый_коммит_каркаса (извлечена разбором; пустое извлечение — провал),
# pre-commit стенда — заглушка, git-конфиг — свой. Демон идёт со своими LOG_DIR,
# HEARTBEAT_DIR, PROJECT_DIR и конфигом пакета (harness.conf пакета — то, что кладёт
# установка); модель — подставная обёртка claude-demon.sh, пишет вызов в свой файл.
# Двери мутаций: USTANOVIT_UNDER_TEST, DEVMAP_SELFHEAL_UNDER_TEST, DEVMAP_CONF_UNDER_TEST.
set -uo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
# Проба лежит и в пакете (harness/scripts), и в проекте (scripts/), а судит пакет:
# в проекте рядом нет ни установщика, ни harness/demons — путь берётся у конфига.
[ -f "$here/../USTANOVIT.sh" ] || here="$(python3 -c "import sys; sys.path.insert(0, '$here/scripts/lib'); import config; print(config.katalog_paketa() / 'harness')" 2>/dev/null || true)"
src=${USTANOVIT_UNDER_TEST:-$here/../USTANOVIT.sh}
[ -f "$src" ] || { echo "SKIP: установщик $src не найден (проба живёт в пакете)"; exit 77; }
DAEMON=${DEVMAP_SELFHEAL_UNDER_TEST:-$here/demons/devmap-selfheal.sh}
CONF_PKG="${DEVMAP_CONF_UNDER_TEST:-$here/config/harness.conf}"
for f in "$DAEMON" "$CONF_PKG"; do [ -f "$f" ] || { echo "FAIL: нет $f — стенд не собрать"; exit 1; }; done
fail=0; total=0
ok()  { total=$((total + 1)); echo "ok:   $1"; }
bad() { total=$((total + 1)); echo "FAIL: $1"; fail=$((fail + 1)); }
STANDS=()
cleanup() { local d; for d in ${STANDS[@]+"${STANDS[@]}"}; do rm -rf "$d"; done; }
trap cleanup EXIT

extract() { awk -v f="$1" 'index($0, f "() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src"; }
lib=$(mktemp -d "${TMPDIR:-/tmp}/harness-run-XXXXXX"); STANDS+=("$lib")
for f in первый_коммит_каркаса личность_коммитов журнал_пакета_заготовка запуск_от_агента; do
  body=$(extract "$f")
  [ -n "$body" ] || { echo "FAIL: функция $f не найдена в установщике"; exit 1; }
  printf '%s\n' "$body" >> "$lib/lib.sh"
done

# Стенд: каркас с картой без задач, первый коммит — рукой установщика. $1 = 1 —
# поверх него ещё рабочий коммит без трейлера (контроль: пропуск обязан найтись).
new_stand() {
  T=$(mktemp -d "${TMPDIR:-/tmp}/harness-run-XXXXXX"); STANDS+=("$T")
  R="$T/proekt"
  mkdir -p "$R/scripts/lib" "$R/docs" "$T/log" "$T/hb" "$T/etc"
  printf 'version: 1\nlines: []\n' > "$R/dev-map.yaml"
  printf 'version: 1\nlines: []\n' > "$R/docs/dev-map-archive.yaml"
  printf 'model_dlya() { printf haiku; }\nmodel_imya_dopustimo() { return 0; }\n' > "$R/scripts/lib/models.sh"
  cat > "$T/claude-demon.sh" <<SH
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$T/model-called.log"
echo "Not logged in · Please run /login"
exit 1
SH
  # Демон — копия в раскладке проекта вне репозитория стенда: он читает
  # ../../scripts/lib/config.sh от своего места.
  mkdir -p "$T/frame/harness/demons" "$T/frame/scripts"
  cp "$DAEMON" "$T/frame/harness/demons/devmap-selfheal.sh"
  ln -s "$here/scripts/lib" "$T/frame/scripts/lib"
  printf '#!/bin/sh\nexit 0\n' > "$T/pre-commit"; chmod +x "$T/pre-commit" "$T/claude-demon.sh"
  : > "$T/gitconfig"
  export GIT_CONFIG_GLOBAL="$T/gitconfig" GIT_CONFIG_NOSYSTEM=1
  git -C "$R" init -q -b master
  ln -s "$T/pre-commit" "$R/.git/hooks/pre-commit"
  ( . "$lib/lib.sh"; скажи() { printf '%s\n' "$*"; }; AS_AGENT=""
    первый_коммит_каркаса "$R" nobody ) > "$T/first-commit.out" 2>&1
  printf '%s' $? > "$T/first-commit.rc"
  if [ "${1:-0}" = 1 ]; then
    git -C "$R" commit -q --allow-empty -m 'работа без трейлера'
  fi
  : > "$T/model-called.log"
}

run_daemon() {
  local out rc=0
  out=$(cd "$T" && env -u SELFHEAL_CLAUDE_TIMEOUT_SEC -u DEVMAP_SERVICE_PREFIXES -u HARNESS_HOME \
          HARNESS_INSTALL_CONF="$T/etc/install.conf" HARNESS_CONF="$T/etc/harness.conf" \
          PROJECT_DIR="$T/proekt" LOG_DIR="$T/log" HEARTBEAT_DIR="$T/hb" \
          CLAUDE_DEMON_SH="$T/claude-demon.sh" \
          timeout 120 bash "$T/frame/harness/demons/devmap-selfheal.sh" 2>&1) || rc=$?
  printf '%s' "$rc" > "$T/rc"
  printf '%s' "$out"
}
stand_conf() {
  printf 'PROJECT_NAME="t"\nPROJECT_DIR="%s"\nLOG_DIR="%s/log"\nHEARTBEAT_DIR="%s/hb"\n' "$T/proekt" "$T" "$T" > "$T/etc/install.conf"
  cp "$CONF_PKG" "$T/etc/harness.conf"
}

# ── БОЛЬНОЙ СЛУЧАЙ R7: в репозитории один коммит — коммит установщика ──
new_stand 0; stand_conf
first_subject=$(git -C "$T/proekt" log -1 --format=%s 2>/dev/null)
[ "$(cat "$T/first-commit.rc")" = 0 ] && [ -n "$first_subject" ] \
  && ok "условие стенда: первый коммит сделан функцией установщика («$first_subject»)" \
  || bad "условие стенда: первый коммит установщика не сделан: $(tail -2 "$T/first-commit.out" | tr '\n' ' ')"
[ "$(git -C "$T/proekt" rev-list --count HEAD 2>/dev/null)" = 1 ] \
  && ok "условие стенда: в истории ровно один коммит" || bad "условие стенда: коммитов не один"
out=$(run_daemon)
[ ! -s "$T/model-called.log" ] \
  && ok "Д-22: коммит установщика — модель не звана" \
  || bad "Д-22: коммит установщика принят за коммит мимо карты — модель звана ($(grep -o 'мимо карты: [0-9]* коммит' <<<"$out" | head -1))"
[ "$(cat "$T/rc")" = 0 ] && ok "Д-22: демон на свежей установке выходит с кодом 0" \
  || bad "Д-22: демон на свежей установке выходит с кодом $(cat "$T/rc") (шаг 13 запишет отказ)"

# ── контроль: обычный коммит без трейлера поверх — пропуск найден, модель звана ──
new_stand 1; stand_conf
out=$(run_daemon)
[ -s "$T/model-called.log" ] && ok "контроль: рабочий коммит без трейлера — модель звана" \
  || bad "контроль: рабочий коммит без трейлера не найден — проба слепа: $(tail -2 <<<"$out" | tr '\n' ' ')"
grep -q 'работа без трейлера' "$T/model-called.log" \
  && ok "контроль: в задании модели — рабочий коммит" || bad "контроль: рабочего коммита в задании модели нет"
grep -qF -- "$first_subject" "$T/model-called.log" \
  && bad "Д-22: в задании модели и коммит установщика («$first_subject») — модель «чинит» его" \
  || ok "Д-22: коммита установщика в задании модели нет"

echo "итого случаев $total, неудач $fail"
[ "$fail" = 0 ]
