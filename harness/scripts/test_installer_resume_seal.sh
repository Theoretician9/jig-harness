#!/usr/bin/env bash
# Проба: эталон личности на обновлении (Д-23, живая установка R7 11.11.2026).
#
# Машина 0.9.2 несёт штампы всех шагов. `--resume` пропускает шаг 1 по штампу, а
# правило sudoers на self-seal и сам self-seal ставит только шаг 1 — на обновлённую
# машину они не приезжают. Раскладчик говорит об этом строкой в свой журнал, и
# строка не доходит ни до «УПАЛО», ни до «ОСТАЛОСЬ РУКАМИ».
#   (а) при `--resume` со штампами всех шагов правило sudo и self-seal ставятся,
#       и ставятся ДО раскладки (шаг 7-а печатает эталон через это правило);
#   (б) раскладка сказала «эталон не напечатан» — это видно в сводке установщика.
#
# Условие проба создаёт сама: копия установщика в своём каталоге, штампы в своём
# каталоге (строка STAMP_DIR копии переписана — ПУСТОЕ извлечение = провал), конфиг
# установки в своём каталоге. (а) — настоящий `--dry-run --resume`: root не нужен,
# ничего не исполняется. (б) — функции установщика с подставными исполнителями.
# Дверь мутаций: USTANOVIT_UNDER_TEST — установщик, который судит проба.
set -u
here=$(cd "$(dirname "$0")/.." && pwd)
# Проба лежит и в пакете (harness/scripts), и в проекте (scripts/), а судит пакет:
# в проекте рядом нет ни установщика, ни harness/demons — путь берётся у конфига.
[ -f "$here/../USTANOVIT.sh" ] || here="$(python3 -c "import sys; sys.path.insert(0, '$here/scripts/lib'); import config; print(config.katalog_paketa() / 'harness')" 2>/dev/null || true)"
src=${USTANOVIT_UNDER_TEST:-$here/../USTANOVIT.sh}
[ -f "$src" ] || { echo "SKIP: установщик $src не найден (проба живёт в пакете)"; exit 77; }
fail=0; total=0
ok()  { total=$((total + 1)); echo "ok:   $1"; }
bad() { total=$((total + 1)); echo "FAIL: $1"; fail=$((fail + 1)); }
t=$(mktemp -d "${TMPDIR:-/tmp}/harness-run-XXXXXX"); trap 'rm -rf "$t"' EXIT

# Пакет стенда: копия установщика рядом с настоящими harness/ и UNIFIED/.
mkdir -p "$t/pkg" "$t/stamps" "$t/etc"
ln -s "$here" "$t/pkg/harness"
for u in "$here/../UNIFIED" "$here/../../UNIFIED"; do
  [ -d "$u" ] && { ln -s "$(cd "$u" && pwd)" "$t/pkg/UNIFIED"; break; }
done
[ -e "$t/pkg/UNIFIED" ] || { echo "FAIL: UNIFIED рядом с пакетом не найден — стенд не собрать"; exit 1; }
sed "s|^STAMP_DIR=/var/lib/harness/install-steps\$|STAMP_DIR=$t/stamps|" "$src" > "$t/pkg/USTANOVIT.sh"
[ "$(grep -c "^STAMP_DIR=$t/stamps\$" "$t/pkg/USTANOVIT.sh")" = 1 ] \
  || { echo "FAIL: строка STAMP_DIR установщика не найдена — штампы стенда не подставить (проба сломана)"; exit 1; }

# Шаги — РАЗБОРОМ главной последовательности установщика.
mapfile -t steps < <(awk '/^шаг_0$/ {on=1} on && /^шаг_[^ ]+$/ {print}' "$src")
[ "${#steps[@]}" -ge 10 ] || { echo "FAIL: главная последовательность шагов не разобрана (${#steps[@]})"; exit 1; }
stamp_092() { local s; for s in "${steps[@]}"; do touch "$t/stamps/$s.done"; done; }

dry() { HARNESS_INSTALL_CONF="$t/etc/install.conf" bash "$t/pkg/USTANOVIT.sh" --dry-run "$@" 2>&1; }
seal_install_re="install .*self-seal\.sh.* /usr/local/lib/harness/self-seal"
seal_rule_re="NOPASSWD: /usr/local/lib/harness/self-seal"
line_of() { grep -nE -- "$1" <<<"$2" | head -1 | cut -d: -f1; }

# ── контроль: свежая установка ставит и правило, и self-seal ──
out=$(dry); rc=$?
[ "$rc" = 0 ] && ok "свежий --dry-run: код 0" || bad "свежий --dry-run: код $rc"
grep -qE -- "$seal_install_re" <<<"$out" && grep -qE -- "$seal_rule_re" <<<"$out" \
  && ok "свежая установка: правило sudo и self-seal ставятся" \
  || bad "свежая установка: нет правила sudo или self-seal — проба ищет не то"

# ── (а) БОЛЬНОЙ СЛУЧАЙ R7: обновление 0.9.2 → 0.9.3, штампы всех шагов стоят ──
stamp_092
out=$(dry --resume); rc=$?
[ "$rc" = 0 ] && ok "--resume со штампами: код 0" || bad "--resume со штампами: код $rc"
skipped=$(grep -c "пропуск (--resume)" <<<"$out")
[ "$skipped" -ge 10 ] \
  && ok "условие стенда: шаги пропускаются по штампам ($skipped)" || bad "условие стенда: штампы не легли (пропусков $skipped)"
grep -qE -- "$seal_install_re" <<<"$out" \
  && ok "(а) обновление: self-seal ставится, хотя штамп шага 1 есть" \
  || bad "(а) обновление: self-seal НЕ ставится — шаг 1 пропущен по штампу (USTANOVIT.sh, шаг_1)"
grep -qE -- "$seal_rule_re" <<<"$out" \
  && ok "(а) обновление: правило sudo на self-seal ставится" \
  || bad "(а) обновление: правила sudo на self-seal нет — шаг 1 пропущен по штампу"
l_inst=$(line_of "$seal_install_re" "$out"); l_rule=$(line_of "$seal_rule_re" "$out")
l_7a=$(line_of "=+ шаг 7-а" "$out")
[ -n "$l_inst" ] && [ -n "$l_rule" ] && [ -n "$l_7a" ] && [ "$l_inst" -lt "$l_7a" ] && [ "$l_rule" -lt "$l_7a" ] \
  && ok "(а) обновление: правило и self-seal — до раскладки (шаг 7-а печатает эталон)" \
  || bad "(а) обновление: правило/self-seal не стоят до шага 7-а (строки: self-seal ${l_inst:-нет}, правило ${l_rule:-нет}, 7-а ${l_7a:-нет})"

# ── (б) раскладка сказала «эталон не напечатан» — это видно в сводке ──
# Тело установщика до главной последовательности, без неё: функции, разбор флагов,
# опрос (в --dry-run — демо-значения). Затем живой режим с подставными исполнителями.
cut_at=$(grep -n '^шаг_0$' "$t/pkg/USTANOVIT.sh" | head -1 | cut -d: -f1)
head -n $((cut_at - 1)) "$t/pkg/USTANOVIT.sh" > "$t/pkg/lib.sh"
cat > "$t/run_b.sh" <<'RUN'
# $1 = lib, $2 = каталог стенда, $3 = 1 — раскладка предупредила, 0 — нет; дальше шаги
lib=$1; T=$2; warn=$3; shift 3
steps=("$@")
set -- --dry-run --resume
. "$lib"
set +eEu; trap - ERR
DRY=0
mkdir -p "$LOG_DIR"
# Подставные исполнители: команды записываются, не исполняются. Раскладка
# пишет свой журнал так, как пишет её настоящий вывод.
делай() {
  printf '  + %s\n' "$1"
  case "$1" in
    *razlozhit-harnes.sh*tee*)
      { [ "$WARN" = 1 ] && echo "ВНИМАНИЕ: эталон личности не напечатан — нет правила sudo на self-seal; добавьте правило и повторите раскладку"
        echo "UNIFIED доставлен"; echo "раскладка завершена: $PROJECT_DIR"; } > "$LOG_DIR/install-rollout.log" ;;
  esac
}
делай_тихо() { printf '  + %s\n' "$1"; }
проверка() { :; }
записать_sudoers() { cat >/dev/null; printf '  + sudoers %s\n' "$1"; }
записать_файл() { cat >/dev/null; printf '  + файл %s\n' "$1"; }
sudo() { return 1; }
systemctl() { return 1; }
закрыть_ssh_из_docker() { return 2; }
WARN=$warn
for s in "${steps[@]}"; do "$s"; done
финал
RUN
mkdir -p "$t/tmp"
run_b() { TMPDIR="$t/tmp" HARNESS_INSTALL_CONF="$t/etc/install.conf" bash "$t/run_b.sh" "$t/pkg/lib.sh" "$t" "$1" "${steps[@]}" 2>&1; }
summary_of() { sed -n '/ОСТАЛОСЬ РУКАМИ (только/,$p' <<<"$1"; }

out=$(run_b 1)
grep -q "ИТОГОВАЯ СВОДКА" <<<"$out" || bad "(б) стенд: сводка не напечатана — подставной прогон сломан: $(tail -3 <<<"$out" | tr '\n' ' ')"
grep -q "эталон личности не напечатан" "$t/etc/log/install-rollout.log" 2>/dev/null \
  && ok "(б) условие стенда: журнал раскладки несёт «эталон не напечатан»" \
  || bad "(б) условие стенда: журнал раскладки не записан (шаг 7-а не позвал раскладчик?)"
summ=$(summary_of "$out")
grep -q "/usr/local/lib/harness/self-seal" <<<"$summ" \
  && ok "(б) эталон не поставлен — сводка называет правило на self-seal" \
  || bad "(б) эталон не поставлен — в «ОСТАЛОСЬ РУКАМИ»/«УПАЛО» о self-seal ни слова (только в журнале раскладки)"

out=$(run_b 0)
summ=$(summary_of "$out")
grep -q "ИТОГОВАЯ СВОДКА" <<<"$out" && ! grep -q "эталон личности" <<<"$summ" \
  && ok "(б) контроль: раскладка не предупреждала — в сводке строки об эталоне нет" \
  || bad "(б) контроль: строка об эталоне в сводке без предупреждения раскладки"

echo "итого случаев $total, неудач $fail"
[ "$fail" = 0 ]
