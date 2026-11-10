#!/usr/bin/env bash
# Проба: пути установщика на обновлении (Д-10, Д-11 и запись версии, повтор 10.11.2026).
#
# Д-10: копия пакета у агента сливалась с новым пакетом, и файлы, убранные новой версией,
#   оставались и шли в первый коммит. Теперь отличающаяся копия отодвигается в .bak-<метка>.
# Д-11: шаг 12 требует ss, а шаг 2 при --resume пропущен: шаг 12 обязан поставить iproute2 сам.
# Версия: путь через установщик не писал версию, и update.py --версия говорил «ещё не записана».
# Функции достаются ПАРСИНГОМ установщика; пустое извлечение — провал.
# Дверь мутаций: USTANOVIT_UNDER_TEST — установщик, который судит проба.
set -u
here=$(cd "$(dirname "$0")/.." && pwd)
src=${USTANOVIT_UNDER_TEST:-$here/../USTANOVIT.sh}
[ -f "$src" ] || { echo "SKIP: установщик $src не найден (проба живёт в пакете)"; exit 77; }
fail=0
ok()  { echo "ok:   $1"; }
bad() { echo "FAIL: $1"; fail=1; }
extract() { awk -v f="$1" 'index($0, f "() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src"; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

# ── Д-10: положить_пакет ──
fns=""
for f in положить_пакет записать_версию_пакета запуск_от_агента шаг_12; do
  body=$(extract "$f")
  [ -n "$body" ] || { echo "FAIL: функция $f не найдена в установщике"; exit 1; }
  fns+="$body"$'\n'
done
printf '%s\n' "$fns" > "$tmp/lib.sh"

new="$tmp/new"; dst="$tmp/home/STARTER-PACKAGE"
mkdir -p "$new/harness" "$dst/scripts"
echo new > "$new/a.txt"; echo fresh > "$new/harness/fresh.txt"
echo old > "$dst/a.txt"; echo gone > "$dst/scripts/test_ustanovit_units.sh"
bash -c ". '$tmp/lib.sh'; положить_пакет '$new' '$dst' T1" >/dev/null 2>&1
[ ! -e "$dst/scripts/test_ustanovit_units.sh" ] && ok "убранного новой версией файла в копии агента нет" || bad "убранного новой версией файла в копии агента нет"
[ -f "$dst.bak-T1/scripts/test_ustanovit_units.sh" ] && ok "старая копия лежит в .bak-метка" || bad "старой копии в .bak нет"
[ "$(cat "$dst/a.txt" 2>/dev/null)" = new ] && [ -f "$dst/harness/fresh.txt" ] && ok "новый пакет на месте" || bad "новый пакет не лёг"

bash -c ". '$tmp/lib.sh'; положить_пакет '$new' '$dst' T2" >/dev/null 2>&1
[ ! -e "$dst.bak-T2" ] && ok "одинаковая копия: лишнего .bak нет" || bad "одинаковая копия отодвинута"

rm -rf "$dst"
bash -c ". '$tmp/lib.sh'; положить_пакет '$new' '$dst' T3" >/dev/null 2>&1
[ ! -e "$dst.bak-T3" ] && [ -f "$dst/a.txt" ] && ok "пустое место: .bak нет, пакет лёг" || bad "пустое место: неверный исход"

body7=$(extract шаг_7а)
grep -q "положить_пакет '" <<<"$body7" && ! grep -q "cp -a '\$SCRIPT_DIR/\.'" <<<"$body7" \
  && ok "шаг 7-а кладёт пакет через положить_пакет, не слиянием" || bad "шаг 7-а сливает пакет поверх старой копии"

# ── Д-13: отличие только в __pycache__ не отодвигает копию ──
rm -rf "$dst"; mkdir -p "$dst"; cp -a "$new/." "$dst/"; mkdir -p "$dst/harness/__pycache__"; echo x > "$dst/harness/__pycache__/m.cpython-312.pyc"; echo y > "$dst/z.pyc"
bash -c ". '$tmp/lib.sh'; положить_пакет '$new' '$dst' T4" >/dev/null 2>&1
[ ! -e "$dst.bak-T4" ] && ok "отличие только в __pycache__: .bak нет" || bad "__pycache__ вызвал .bak"

# ── Д-12: источник старше копии агента ──
g() { git -C "$1" -c user.email=a@b -c user.name=n "${@:2}"; }
rm -rf "$tmp/s" "$tmp/d"; mkdir "$tmp/s"; g "$tmp/s" init -q; echo 1 > "$tmp/s/f"; g "$tmp/s" add -A; g "$tmp/s" commit -qm 1
cp -a "$tmp/s" "$tmp/d"; echo 2 > "$tmp/d/f"; g "$tmp/d" commit -qam 2
out=$(bash -c ". '$tmp/lib.sh'; скажи() { printf '%s\n' \"\$*\"; }; положить_пакет '$tmp/s' '$tmp/d' T5" 2>&1)
[ ! -e "$tmp/d.bak-T5" ] && [ "$(cat "$tmp/d/f")" = 2 ] && ok "источник старше копии: копия не тронута, .bak нет" || bad "источник старше копии: копию откатило"
grep -q "$tmp/d" <<<"$out" && ok "источник старше копии: строка с путём к копии" || bad "нет строки о более новой копии: $out"
# обратное: источник новее — копия обновляется
rm -rf "$tmp/d"; cp -a "$tmp/s" "$tmp/d"; echo 3 > "$tmp/s/f"; g "$tmp/s" commit -qam 3
bash -c ". '$tmp/lib.sh'; положить_пакет '$tmp/s' '$tmp/d' T6" >/dev/null 2>&1
[ -e "$tmp/d.bak-T6" ] && [ "$(cat "$tmp/d/f")" = 3 ] && ok "источник новее копии: копия заменена" || bad "источник новее копии не принят"

# ── Д-11: шаг 12 сам ставит iproute2, если нет ss ──
run12() { # $1 = код «command -v ss»; печатает команды, которые шаг велел исполнить
  FAKE_SS_RC=$1 bash -c "
    . '$tmp/lib.sh'
    command() { if [ \"\$1\" = -v ] && [ \"\$2\" = ss ]; then return \$FAKE_SS_RC; fi; builtin command \"\$@\"; }
    заголовок() { :; }; скажи() { :; }; выдать_sudoers() { :; }; свои_юниты() { echo unit; }
    python3() { echo id; }; стоп() { echo \"СТОП: \$2\"; exit 1; }
    делай() { echo \"+ \$1\"; }
    APT_UPDATE=apt-update; DRY=1; AGENT_USER=agent; HARNESS_SRC=/x; SCRIPT_DIR=/x
    DONE_LIST=(); MANUAL_LIST=(); FAILED_LIST=()
    шаг_12" 2>&1
}
out=$(run12 1)
grep -q 'apt_поставить iproute2' <<<"$out" && ok "шаг 12 без ss ставит iproute2" || bad "шаг 12 без ss не ставит iproute2: $out"
out=$(run12 0)
grep -q 'iproute2' <<<"$out" && bad "шаг 12 с ss ставит лишнее" || ok "шаг 12 с ss ничего не ставит"
grep -q 'apt_поставить' <(extract шаг_2 | sed -n 1,20p; grep -m1 'apt_поставить git' "$src") && ok "шаг 2 ставит той же функцией" || bad "шаг 2 ставит не той функцией"

# ── версия пишется в формате update.py ──
repo="$tmp/repo"; mkdir -p "$repo"
git -C "$repo" init -q && git -C "$repo" -c user.email=a@b -c user.name=n commit -q --allow-empty -m x
head=$(git -C "$repo" rev-parse HEAD)
vf="$tmp/state/версия-харнеса"; mkdir -p "$tmp/state"
AS_AGENT= bash -c ". '$tmp/lib.sh'; скажи() { :; }; записать_версию_пакета agent '$repo' '$vf'" >/dev/null 2>&1
got=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1], encoding='utf-8')).get('коммит',''))" "$vf" 2>/dev/null)
[ -n "$got" ] && [ "$got" = "$head" ] && ok "версия записана: коммит пакета" || bad "версия не записана или чужая ($got)"
upd="$here/scripts/update.py"
if [ -f "$upd" ]; then
  v=$(HARNESS_VERSION_FILE="$vf" python3 "$upd" --версия 2>&1)
  grep -q "$head" <<<"$v" && ok "update.py --версия видит записанное" || bad "update.py --версия не видит: $v"
fi
[ "$fail" = 0 ] && echo "ИТОГ: зелёный" || echo "ИТОГ: КРАСНЫЙ"
exit $fail
