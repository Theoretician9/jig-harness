#!/usr/bin/env bash
# Проба: установщик кладёт имя каталога продукта в PRODUCT_ALIASES, если прозвища не заданы.
#
# Откуда взята. Гейт чистоты харнеса (check-harness-clean.py) перестаёт судить
# PRODUCT_DIR: у копии Content-Factory это «html» — слово разметки, ложные
# находки (замер 10.11.2026: 166 в 13 файлах). Копии, где имя каталога и есть имя
# продукта, без прозвищ потеряли бы проверку молча — поэтому установщик сам
# кладёт имя каталога продукта (basename PRODUCT_PATH) в PRODUCT_ALIASES, а
# заданные прозвища не трогает.
#
# КОНТРАКТ (для правки USTANOVIT.sh):
#   default_product_aliases() { … }   — функция установщика, с начала строки;
#     $1 = harness.conf (может быть симлинком — правится цель, ссылка остаётся),
#     $2 = имя продукта.
#     PRODUCT_ALIASES="" → PRODUCT_ALIASES="$2"; ключа нет → строка дописывается;
#     PRODUCT_ALIASES непуст или $2 пуст → файл не меняется ни на байт;
#     прочие строки и комментарии целы; повторный вызов ничего не меняет.
#   Вызов — одной строкой вне функции, ПОСЛЕ того как /etc/harness/harness.conf
#     стал ссылкой на agent/harness.conf, и только при непустом PRODUCT_PATH:
#       [[ -z "$PRODUCT_PATH" ]] || делай "default_product_aliases /etc/harness/harness.conf '…$PRODUCT_PATH…'"
#     имя продукта — basename PRODUCT_PATH.
#
# Функция и строка вызова достаются ПАРСИНГОМ установщика (пустое извлечение —
# провал) и исполняются на своём стенде: живой /etc/harness проба не трогает.
# Дверь мутаций: USTANOVIT_UNDER_TEST — установщик, который судит проба.
set -u
export LC_ALL=C.UTF-8
here=$(cd "$(dirname "$0")/.." && pwd)
# Проба лежит и в пакете (harness/scripts), и в проекте (scripts/), а судит пакет.
[ -f "$here/../USTANOVIT.sh" ] || here="$(python3 -c "import sys; sys.path.insert(0, '$here/scripts/lib'); import config; print(config.katalog_paketa() / 'harness')" 2>/dev/null || true)"
src=${USTANOVIT_UNDER_TEST:-$here/../USTANOVIT.sh}
[ -f "$src" ] || { echo "SKIP: установщик $src не найден (проба живёт в пакете)"; exit 77; }
fail=0; total=0
ok()  { total=$((total + 1)); echo "ok:   $1"; }
bad() { total=$((total + 1)); echo "FAIL: $1"; fail=$((fail + 1)); }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1: ждали «$2», вышло «$3»"; fi; }
t=$(mktemp -d "${TMPDIR:-/tmp}/proba-product-aliases-XXXXXX"); trap 'rm -rf "$t"' EXIT

body=$(awk 'index($0, "default_product_aliases() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src")
call=$(grep -n 'default_product_aliases /etc/harness/harness.conf' "$src" | grep -v 'default_product_aliases() {')
link_line=$(grep -n 'ln -s agent/harness.conf /etc/harness/harness.conf' "$src" | head -1 | cut -d: -f1)

if [ -z "$body" ]; then
  bad "функция default_product_aliases не найдена в установщике"
fi
if [ -z "$call" ]; then
  bad "строка вызова default_product_aliases /etc/harness/harness.conf не найдена"
fi
[ -n "$link_line" ] || bad "контроль: шаг ссылки /etc/harness/harness.conf → agent/harness.conf не найден"

# Образец конфига — с комментариями и соседними ключами, как настоящий harness.conf.
sample() { # $1 = строка PRODUCT_ALIASES (пусто — ключа нет совсем)
  printf '# прочие написания имени продукта\n'
  printf 'PRODUCT_DIR=""\nPRODUCT_PATH=""\n'
  [ -n "$1" ] && printf '%s\n' "$1"
  printf '# состояние приёмника тревог\nPRODUCT_QUIET_STATE=""\n'
}
aliases_of() { sed -n 's/^PRODUCT_ALIASES=//p' "$1"; }

if [ -n "$body" ]; then
  printf '%s\n' "$body" > "$t/lib.sh"
  run_fn() { bash -c ". '$t/lib.sh'; default_product_aliases \"\$1\" \"\$2\"" _ "$1" "$2" 2>&1; }

  echo "── функция"
  sample 'PRODUCT_ALIASES=""' > "$t/empty.conf"
  run_fn "$t/empty.conf" shopxyz >/dev/null
  check "БОЛЬНОЙ СЛУЧАЙ: прозвищ нет — кладётся имя каталога продукта" '"shopxyz"' "$(aliases_of "$t/empty.conf")"
  check "ключ PRODUCT_ALIASES один" 1 "$(grep -c '^PRODUCT_ALIASES=' "$t/empty.conf")"
  check "прочие строки целы (комментарии, соседние ключи)" \
    "$(sample 'PRODUCT_ALIASES=""' | grep -v '^PRODUCT_ALIASES=')" "$(grep -v '^PRODUCT_ALIASES=' "$t/empty.conf")"
  cp "$t/empty.conf" "$t/again.conf"; run_fn "$t/again.conf" shopxyz >/dev/null
  check "повторный вызов ничего не меняет" same "$(cmp -s "$t/empty.conf" "$t/again.conf" && echo same || echo diff)"

  sample 'PRODUCT_ALIASES="Mojik МОЙПРОДУКТ"' > "$t/set.conf"; cp "$t/set.conf" "$t/set.orig"
  run_fn "$t/set.conf" shopxyz >/dev/null
  check "заданные прозвища не затираются (файл тот же до байта)" same \
    "$(cmp -s "$t/set.conf" "$t/set.orig" && echo same || echo diff)"

  sample '' > "$t/nokey.conf"
  run_fn "$t/nokey.conf" shopxyz >/dev/null
  check "ключа нет (старый живой конфиг) — строка дописана" '"shopxyz"' "$(aliases_of "$t/nokey.conf")"

  sample 'PRODUCT_ALIASES=""' > "$t/noname.conf"; cp "$t/noname.conf" "$t/noname.orig"
  run_fn "$t/noname.conf" "" >/dev/null
  check "имя продукта пусто — файл не меняется" same \
    "$(cmp -s "$t/noname.conf" "$t/noname.orig" && echo same || echo diff)"

  # Живая раскладка: /etc/harness/harness.conf — ссылка на agent/harness.conf.
  mkdir -p "$t/etc/agent"; sample 'PRODUCT_ALIASES=""' > "$t/etc/agent/harness.conf"
  ln -s agent/harness.conf "$t/etc/harness.conf"
  run_fn "$t/etc/harness.conf" shopxyz >/dev/null
  check "конфиг-ссылка: ссылка осталась ссылкой" yes "$([ -L "$t/etc/harness.conf" ] && echo yes || echo no)"
  check "конфиг-ссылка: правится цель" '"shopxyz"' "$(aliases_of "$t/etc/agent/harness.conf")"
fi

if [ -n "$call" ] && [ -n "$body" ]; then
  echo "── строка вызова"
  call_no=${call%%:*}; call_text=${call#*:}
  check "вызов одной строкой, одно место" 1 "$(printf '%s\n' "$call" | wc -l)"
  check "вызов после шага ссылки harness.conf (строка $call_no > $link_line)" yes \
    "$([ -n "$link_line" ] && [ "$call_no" -gt "$link_line" ] && echo yes || echo no)"
  # Строка исполняется как есть: делай подменяет живой путь путём стенда.
  run_call() { # $1 = PRODUCT_PATH
    rm -rf "$t/etc2"; mkdir -p "$t/etc2"; sample 'PRODUCT_ALIASES=""' > "$t/etc2/harness.conf"
    PRODUCT_PATH="$1" STAND="$t/etc2/harness.conf" LIB="$t/lib.sh" CALL="$call_text" bash -c '
      . "$LIB"
      делай() { local c="${1//\/etc\/harness\/harness.conf/$STAND}"; eval "$c"; }
      eval "$CALL"' >/dev/null 2>&1
    aliases_of "$t/etc2/harness.conf"
  }
  check "вызов: PRODUCT_PATH=/srv/www/shopxyz — прозвище «shopxyz»" '"shopxyz"' "$(run_call /srv/www/shopxyz)"
  check "вызов: PRODUCT_PATH пуст — прозвищ нет" '""' "$(run_call "")"
fi

[ "$total" -gt 0 ] || { echo "FAIL: ни одного случая не прогнано"; exit 1; }
echo "ИТОГ: путей $total, плохих $fail"
[ "$fail" = 0 ]
