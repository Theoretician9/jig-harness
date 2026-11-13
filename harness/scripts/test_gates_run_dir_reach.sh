#!/usr/bin/env bash
# Проба: три пробы, которым нужен второй пользователь и сокет tmux, зелёные
# ПОД КАТАЛОГОМ ПРОГОНА ВОРОТ, а не только соло.
#
# Каталог прогона ворот (TMPDIR=harness-run-*) наследуют все пробы. Ночью
# 11.11 три гейта краснели только под ним, а соло были зелёными:
#   «события: проба записи», «события: проба разбора» — каталог 0700, и
#     harness-panel не входит в стенд под ним (PermissionError);
#   «уборка не гасит окно» — путь сокета tmux стенда волны 3 под ним
#     113 знаков при пределе sun_path 107; tmux отвечает «File name too long»,
#     а стенд глушит ошибку, и сервер не поднимается вовсе.
# Перепрогон соло этого не видит, поэтому проба зовёт НАСТОЯЩИЕ ворота
# (gates.sh --только) — каталог строят они сами, а не копия их строки здесь.
#
# Условие своё: LOG_DIR и журнал секунд ворот — во временном каталоге пробы,
# боевые журналы не пишутся. TMPDIR снимается: ночные ворота кладут каталог
# прогона прямо в /tmp, и длина пути должна быть та же, даже если сама проба
# идёт под воротами. Подставной mktemp записывает каталог прогона ворот:
# его права, длину и то, входит ли в него harness-panel.
#
# Дверь для мутаций: GATES_SCRIPT — путь к gates.sh.
# Итог: «КАТАЛОГ ВОРОТ: …; проверок 3: ок K, красных M», код 0 только при K = 3.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
GATES="${GATES_SCRIPT:-$HERE/gates.sh}"
CHECKS=("события: проба записи" "события: проба разбора" "уборка не гасит окно")
SECOND_USER=harness-panel

if ! sudo -n -u "$SECOND_USER" true 2>/dev/null; then
    echo "ПРОБА НЕ СУДИТ: второго пользователя $SECOND_USER нет (или sudo -n) — права каталога ворот не проверить"
    exit 1
fi

W=$(mktemp -d -t gates-run-dir-reach-XXXXXX)
trap 'rm -rf "$W"' EXIT
mkdir -p "$W/bin" "$W/log"
# Подставной mktemp: вывод настоящего — как есть; для каталога ворот — строка
# «путь, права, входит ли второй пользователь» в журнал пробы.
cat > "$W/bin/mktemp" <<'SH'
#!/usr/bin/env bash
out=$(/usr/bin/mktemp "$@") || exit $?
case "$*" in
    *harness-run-*)
        # Ворота правят права ПОСЛЕ mktemp: замер — чуть позже, фоном.
        ( sleep 2; reach=нет
          sudo -n -u "$REACH_USER" test -x "$out" 2>/dev/null && reach=да
          printf '%s\t%s\t%s\n' "$out" "$(stat -c %a "$out" 2>/dev/null)" "$reach" >> "$REACH_LOG"
        ) > /dev/null 2>&1 & ;;
esac
printf '%s\n' "$out"
SH
chmod 755 "$W/bin/mktemp"

only=$(IFS='|'; printf '%s' "${CHECKS[*]}")
rc=0
env -u TMPDIR LOG_DIR="$W/log" GATES_TIMING_DIR="$W" REACH_LOG="$W/reach.tsv" REACH_USER="$SECOND_USER" \
    PATH="$W/bin:$PATH" timeout 900 bash "$GATES" --только "$only" > "$W/gates.out" 2>&1 || rc=$?

if [ -s "$W/reach.tsv" ]; then
    IFS=$'\t' read -r run_dir mode reach < "$W/reach.tsv"
    echo "КАТАЛОГ ВОРОТ: $run_dir права $mode длина ${#run_dir}; $SECOND_USER входит: $reach"
else
    echo "КАТАЛОГ ВОРОТ: ворота не создали каталог harness-run- — сверять нечего"
fi

ok=0; red=0; found=0; bad=()
for name in "${CHECKS[@]}"; do
    if grep -qF "  ок · $name — " "$W/gates.out"; then
        ok=$((ok + 1)); found=$((found + 1))
    elif grep -qF "· $name" "$W/gates.out"; then
        red=$((red + 1)); found=$((found + 1)); bad+=("$name")
    fi
done
grep -F "  ок · " "$W/gates.out"
echo "ворота: rc=$rc; проверок ${#CHECKS[@]}: найдено $found, ок $ok, красных $red${bad:+ [${bad[*]}]}"
if [ "$found" -ne "${#CHECKS[@]}" ]; then
    echo "ПРОБА СЛОМАНА: ворота прогнали не все проверки (имя сменилось?) — хвост вывода:"
    tail -n 15 "$W/gates.out"
    exit 1
fi
if [ "$ok" -eq "${#CHECKS[@]}" ] && [ "$rc" -eq 0 ]; then
    echo "ПРОБА ЗЕЛЁНАЯ"
    exit 0
fi
# Красная — с выводом красных проверок: причина видна без перепрогона.
grep -A12 "ПЛОХО ·" "$W/gates.out" | grep -E "ПЛОХО|PermissionError|ПОСЛЕ:|ПРОБА|FAIL|неудач" | head -n 30
echo "ПРОБА КРАСНАЯ"
exit 1
