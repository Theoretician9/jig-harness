#!/usr/bin/env bash
# Проба: публикация не гоняет полные ворота второй раз на том же sha и не
# сканирует секреты проекта повторно на пути «по слову».
#
# Откуда взята. Спека эталонного выката §3 строки 14–15, фаза 7.
#   14: оркестратор гоняет gates.sh до публикации (медиана 72 мин), а publish.sh
#       замок 1 (стр. ~134) — ещё раз на том же дереве;
#   15: на пути «по слову» (стр. ~143) check-secrets.sh "$PROJECT_DIR" идёт рядом с
#       gates.sh --хакатон, где «секреты» уже есть (gates-hackathon.yaml), — пятый
#       прогон скана на один коммит. Скан СБОРКИ (стр. ~253) остаётся: у нового
#       репозитория хуков нет, наружу уходит именно сборка.
#
# Контракт, который судится (zamok_vorot "$LOG_DIR" "<каталог скриптов ворот>"):
#   * итог ворот — `<каталог журнала>/ворота-итог.jsonl` ($1 замка), строки gates.sh
#     с полями режим, красных, новых, sha, чисто;
#   * ПРОПУСК (gates.sh не зовётся, код 0, в выводе «замок 1»): ПОСЛЕДНЯЯ ПОЛНАЯ строка
#     (режим «обычный» или «всё») с sha = полный `git -C "$PROJECT_DIR" rev-parse HEAD`
#     СЕЙЧАС несёт код 0 и новых 0, чисто true, и `git -C "$PROJECT_DIR" status
#     --porcelain` пуст СЕЙЧАС. Правило то же, что у самих ворот: ворота выходят
#     кодом суда карантина, и «красных 3, в карантине 3, код 0» ими принято —
#     принято и замком; «красных 0, новых 0, код 1» (беда карантина) — не принято.
#     Строки хакатона и выборки после полной её не заслоняют; битая строка и
#     строка-не-объект — не итог (и без трассы Python в выводе); более поздняя
#     красная полная строка того же sha — ворота идут. Строка без «код» — не итог;
#   * иначе — полный прогон gates.sh как сейчас: другой sha, master ушёл после ворот,
#     дерево грязное сейчас, итог «чисто»: false, красный итог, режим хакатон или
#     выборка, строка без sha (старый формат), файла нет; красные ворота — отказ 1;
#   * путь «по слову» (запись publish-gates-waiver.json): gates.sh --хакатон и
#     build-public.py --selftest — по разу, check-secrets.sh по PROJECT_DIR — 0 раз;
#     скан секретов на этом пути держат ДАННЫЕ — `<каталог скриптов ворот>/../harness/
#     config/gates-hackathon.yaml` (его читает тот gates.sh): нет там проверки, чей cmd
#     зовёт check-secrets.sh, или нет файла — ОТКАЗ (код 1, строка про секреты), а не
#     публикация без скана;
#   * опубликовать_сборку --сухо: check-secrets.sh по каталогу сборки — ровно 1 раз.
#
# Функции publish.sh проба берёт РАЗБОРОМ исходника (все функции верхнего уровня и
# константы-литералы), не копирует; пустое извлечение — провал. Двор свой: git-проект,
# подставные gates.sh / check-secrets.sh / build-public.py / судьи сборки со счётом
# вызовов, свой каталог журнала. Боевые журналы и конфиг не трогаются.
# Дверь мутаций: PUBLISH_SH — путь publish.sh (мутировать КОПИЮ).
set -uo pipefail
export LC_ALL=C.UTF-8
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${PUBLISH_SH:-$HERE/publish.sh}"
# shellcheck disable=SC1091
source "$HERE/lib/selftest_cases.sh"
T=$(mktemp -d -t proba-publish-gates-reuse-XXXXXX)
trap 'rm -rf "$T"' EXIT

awk '
  /^[A-Za-z_][A-Za-z0-9_]*=[^$`(]*$/ && !on { print; next }
  /^[^[:space:]#][^[:space:]]*\(\) *\{/ { on=1 }
  on { print }
  on && /^}$/ { on=0 }
' "$SRC" > "$T/lib.sh"
for f in zamok_vorot proverit_zapis_propuska опубликовать_сборку say; do
    grep -q "^$f() *{" "$T/lib.sh" || { echo "FAIL: функция $f не найдена в $SRC"; exit 1; }
done

g() { GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 git -c user.name=p -c user.email=p@p "$@"; }

dvor() {  # $1 = каталог двора → git-проект, подставные скрипты, журнал
    local d="$1"
    mkdir -p "$d/proj" "$d/stubs" "$d/log"
    g -C "$d/proj" init -q -b master
    printf 'x\n' > "$d/proj/file.txt"
    g -C "$d/proj" add -A; g -C "$d/proj" commit -qm начало
    printf '#!/bin/sh\necho "gates $*" >> "%s/calls"\nexit "${STUB_GATES_RC:-0}"\n' "$d" > "$d/stubs/gates.sh"
    printf '#!/bin/sh\necho "secrets $*" >> "%s/calls"\n' "$d" > "$d/stubs/check-secrets.sh"
    printf 'open("%s/calls","a").write("build-selftest\\n")\n' "$d" > "$d/stubs/build-public.py"
    hackathon_yaml "$d" 'bash scripts/check-secrets.sh "$ROOT"'
}
hackathon_yaml() {  # $1 = двор, $2 = cmd проверки «секреты» → состав быстрых ворот двора
    mkdir -p "$1/harness/config"
    printf "checks:\n  - name: секреты\n    cmd: '%s'\n  - name: синтаксис\n    cmd: 'true'\n" "$2" \
        > "$1/harness/config/gates-hackathon.yaml"
}
row() {  # $1 = двор, $2 = строка итога как есть (@SHA@ — HEAD двора сейчас)
    local line="$2"
    line="${line//@SHA@/$(head_of "$1")}"
    printf '%s\n' "$line" >> "$1/log/ворота-итог.jsonl"
}
# Полная строка итога: $1 = режим, $2 = красных, $3 = новых, $4 = в_карантине, $5 = код.
full_row() {
    printf '{"ts":"2026-11-16T00:00:00+00:00","режим":"%s","зелёных":238,"красных":%s,"новых":%s,"в_карантине":%s,"флейков":0,"sha":"@SHA@","чисто":true,"код":%s}' \
        "$1" "$2" "$3" "$4" "$5"
}
summary() {  # $1 = двор, $2 = режим, $3 = красных, $4 = sha ("-" — без поля), $5 = чисто
    python3 - "$1/log/ворота-итог.jsonl" "$2" "$3" "$4" "$5" <<'PY'
import json, sys
path, mode, red, sha, clean = sys.argv[1:]
row = {"ts": "2026-11-16T00:00:00+00:00", "режим": mode, "зелёных": 238, "красных": int(red),
       "новых": int(red), "в_карантине": 0, "флейков": 0}
if sha != "-":
    row.update(sha=sha, чисто=(clean == "true"), код=int(red))
open(path, "a", encoding="utf-8").write(json.dumps(row, ensure_ascii=False) + "\n")
PY
}
head_of() { g -C "$1/proj" rev-parse HEAD; }
lock1() {  # $1 = двор → вывод замка, последняя строка rc=N
    local d="$1"
    ( . "$T/lib.sh"; PROJECT_DIR="$d/proj" CHECK_ONLY=0 zamok_vorot "$d/log" "$d/stubs" 2>&1 ); echo "rc=$?"
}
calls() {  # $1 = двор, $2 = регулярка строки вызова → число
    local n=0; [ -f "$1/calls" ] && n=$(grep -cE "$2" "$1/calls"); echo "${n:-0}"
}
full_runs() { calls "$1" '^gates *$'; }

echo "── 1. ПРОПУСК: зелёный полный итог того же sha, дерево чистое ──"
for mode in обычный всё; do
    d="$T/skip-$mode"; dvor "$d"
    summary "$d" "$mode" 0 "$(head_of "$d")" true
    OUT=$(lock1 "$d")
    case_check "режим «$mode»: gates.sh не запущен" "0" "$(full_runs "$d")"
    case_check "режим «$mode»: код 0 и вывод про замок 1" "да" \
        "$(printf '%s' "$OUT" | grep -q 'rc=0' && printf '%s' "$OUT" | grep -q 'замок 1' && echo да || echo нет)"
done

echo "── 2. ПРОГОН: итога того же sha при чистом дереве нет ──"
run_case() {  # $1 = имя, $2 = двор (итог уже записан) → судит: полный прогон 1, код 0
    local OUT; OUT=$(lock1 "$2")
    case_check "$1: gates.sh запущен один раз" "1" "$(full_runs "$2")"
    case_check "$1: код 0 (подставные ворота зелёные)" "да" "$(printf '%s' "$OUT" | grep -q 'rc=0' && echo да || echo нет)"
}
d="$T/other-sha"; dvor "$d"; summary "$d" обычный 0 "$(printf '%040d' 7)" true
run_case "другой sha" "$d"
d="$T/moved"; dvor "$d"; summary "$d" обычный 0 "$(head_of "$d")" true
printf 'y\n' >> "$d/proj/file.txt"; g -C "$d/proj" commit -qam "master ушёл"
run_case "master сдвинулся после ворот" "$d"
d="$T/dirty-now"; dvor "$d"; summary "$d" обычный 0 "$(head_of "$d")" true
printf 'лишний\n' > "$d/proj/extra.txt"
run_case "дерево грязное сейчас (неотслеживаемый файл)" "$d"
d="$T/dirty-mod"; dvor "$d"; summary "$d" обычный 0 "$(head_of "$d")" true
printf 'правка\n' >> "$d/proj/file.txt"
run_case "дерево грязное сейчас (правка отслеживаемого)" "$d"
d="$T/dirty-then"; dvor "$d"; summary "$d" обычный 0 "$(head_of "$d")" false
run_case "итог ворот снят на грязном дереве (чисто false)" "$d"
d="$T/red"; dvor "$d"; summary "$d" обычный 2 "$(head_of "$d")" true
run_case "итог того же sha красный" "$d"
for mode in хакатон выборка; do
    d="$T/mode-$mode"; dvor "$d"; summary "$d" "$mode" 0 "$(head_of "$d")" true
    run_case "режим «$mode» — не полный прогон" "$d"
done
d="$T/old-format"; dvor "$d"; summary "$d" обычный 0 - -
run_case "строка старого формата без sha" "$d"
d="$T/no-file"; dvor "$d"
run_case "файла итога нет" "$d"

echo "── 3. красные ворота при прогоне — отказ, как раньше ──"
d="$T/red-run"; dvor "$d"; summary "$d" обычный 0 "$(printf '%040d' 7)" true
OUT=$(STUB_GATES_RC=1 lock1 "$d")
case_check "красные ворота: код 1 и ОТКАЗ" "да" \
    "$(printf '%s' "$OUT" | grep -q 'rc=1' && printf '%s' "$OUT" | grep -q 'ОТКАЗ' && echo да || echo нет)"

skip_case() {  # $1 = имя, $2 = двор (итог уже записан) → судит: прогона нет, код 0, «замок 1»
    local OUT; OUT=$(lock1 "$2")
    case_check "$1: gates.sh не запущен" "0" "$(full_runs "$2")"
    case_check "$1: код 0 и вывод про замок 1" "да" \
        "$(printf '%s' "$OUT" | grep -q 'rc=0' && printf '%s' "$OUT" | grep -q 'замок 1' && echo да || echo нет)"
    LAST_OUT="$OUT"
}
no_trace() {  # $1 = имя, $2 = вывод замка → в выводе нет трассы Python
    case_check "$1: без трассы Python в выводе" "нет" \
        "$(printf '%s' "$2" | grep -qE 'Traceback|Error:' && echo есть || echo нет)"
}

echo "── 3а. код выхода ворот решает, а не число красных (правило самих ворот) ──"
d="$T/code-1"; dvor "$d"; row "$d" "$(full_row обычный 0 0 0 1)"
run_case "красных 0, новых 0, код 1 (беда карантина)" "$d"
d="$T/held"; dvor "$d"; row "$d" "$(full_row обычный 3 0 3 0)"
skip_case "красных 3, новых 0, в карантине 3, код 0 — принято воротами" "$d"
d="$T/new-1"; dvor "$d"; row "$d" "$(full_row обычный 1 1 0 1)"
run_case "новых 1, код 1" "$d"
d="$T/new-1-code-0"; dvor "$d"; row "$d" "$(full_row обычный 1 1 0 0)"
run_case "новых 1 при коде 0 (строка противоречит себе)" "$d"
d="$T/no-code"; dvor "$d"
row "$d" '{"ts":"2026-11-16T00:00:00+00:00","режим":"обычный","зелёных":238,"красных":0,"новых":0,"в_карантине":0,"флейков":0,"sha":"@SHA@","чисто":true}'
run_case "строка без поля «код» (формат 7а)" "$d"

echo "── 3б. последняя ПОЛНАЯ строка того же sha, а не последняя строка файла ──"
d="$T/green-then-red"; dvor "$d"
row "$d" "$(full_row обычный 0 0 0 0)"; row "$d" "$(full_row всё 2 2 0 2)"
run_case "зелёная полная, затем красная полная того же sha" "$d"
d="$T/red-then-green"; dvor "$d"
row "$d" "$(full_row обычный 2 2 0 2)"; row "$d" "$(full_row обычный 0 0 0 0)"
skip_case "красная полная, затем зелёная полная того же sha" "$d"
d="$T/green-then-hk"; dvor "$d"
row "$d" "$(full_row обычный 0 0 0 0)"
row "$d" '{"ts":"2026-11-16T01:00:00+00:00","режим":"хакатон","зелёных":5,"красных":1,"флейков":0,"sha":"@SHA@","чисто":true,"код":1}'
skip_case "зелёная полная, затем строка хакатона" "$d"
d="$T/green-then-pick"; dvor "$d"
row "$d" "$(full_row обычный 0 0 0 0)"; row "$d" "$(full_row выборка 1 1 0 1)"
skip_case "зелёная полная, затем строка выборки" "$d"
d="$T/green-then-broken"; dvor "$d"
row "$d" "$(full_row обычный 0 0 0 0)"; row "$d" '{"режим":"обычный","красных":'
skip_case "зелёная полная, затем битая строка" "$d"
no_trace "битая строка" "$LAST_OUT"
d="$T/green-then-num"; dvor "$d"
row "$d" "$(full_row обычный 0 0 0 0)"; row "$d" '5'
skip_case "зелёная полная, затем строка-не-объект 5" "$d"
no_trace "строка 5" "$LAST_OUT"
d="$T/only-list"; dvor "$d"; row "$d" '[]'
OUT=$(lock1 "$d")
case_check "единственная строка []: gates.sh запущен один раз" "1" "$(full_runs "$d")"
no_trace "строка []" "$OUT"

echo "── 4. путь «по слову»: секреты проекта не сканируются повторно ──"
d="$T/waiver"; dvor "$d"
n=$(date +%s)
printf '{"слово":"без прогона полного","id_входящего":"1793351076.1","причина":"проба","выдана":%s,"до":%s}' \
    "$((n - 60))" "$((n + 3600))" > "$d/log/publish-gates-waiver.json"
OUT=$(lock1 "$d")
case_check "по слову: код 0" "да" "$(printf '%s' "$OUT" | grep -q 'rc=0' && echo да || echo нет)"
case_check "по слову: gates.sh --хакатон один раз" "1" "$(calls "$d" '^gates --хакатон$')"
case_check "по слову: полный прогон не шёл" "0" "$(full_runs "$d")"
case_check "по слову: build-public.py --selftest один раз" "1" "$(calls "$d" '^build-selftest$')"
case_check "по слову: check-secrets.sh по PROJECT_DIR — 0 раз" "0" "$(calls "$d" "^secrets $d/proj\$")"
case_check "по слову: строка пропуска в publish.log" "да" \
    "$(grep -q 'ворота пропущены по слову владельца «без прогона полного»' "$d/log/publish.log" 2>/dev/null && echo да || echo нет)"

echo "── 4а. путь «по слову» без скана секретов в составе быстрых ворот — отказ ──"
waiver() {  # $1 = двор → годная запись о пропуске
    local n; n=$(date +%s)
    printf '{"слово":"без прогона полного","id_входящего":"1793351076.1","причина":"проба","выдана":%s,"до":%s}' \
        "$((n - 60))" "$((n + 3600))" > "$1/log/publish-gates-waiver.json"
}
refuse_case() {  # $1 = имя, $2 = двор → судит: код 1, ОТКАЗ про секреты, пропуск не записан
    local OUT; OUT=$(lock1 "$2")
    case_check "$1: код 1 и ОТКАЗ" "да" \
        "$(printf '%s' "$OUT" | grep -q 'rc=1' && printf '%s' "$OUT" | grep -q 'ОТКАЗ' && echo да || echo нет)"
    case_check "$1: строка отказа называет секреты" "да" "$(printf '%s' "$OUT" | grep 'ОТКАЗ' | grep -qi 'секрет' && echo да || echo нет)"
    case_check "$1: пропуск по слову не записан" "нет" \
        "$(grep -q 'ворота пропущены по слову' "$2/log/publish.log" 2>/dev/null && echo да || echo нет)"
}
d="$T/waiver-no-secrets"; dvor "$d"; hackathon_yaml "$d" 'true'
sed -i 's/name: секреты/name: имена файлов/' "$d/harness/config/gates-hackathon.yaml"; waiver "$d"
refuse_case "в составе нет проверки секретов" "$d"
d="$T/waiver-fake-secrets"; dvor "$d"; hackathon_yaml "$d" 'echo секреты'; waiver "$d"
refuse_case "проверка «секреты» не зовёт check-secrets.sh" "$d"
d="$T/waiver-no-yaml"; dvor "$d"; rm -f "$d/harness/config/gates-hackathon.yaml"; waiver "$d"
refuse_case "файла состава быстрых ворот нет" "$d"

echo "── 5. сборка: скан секретов по каталогу сборки остаётся (опубликовать_сборку --сухо) ──"
d="$T/build"; dvor "$d"
mkdir -p "$d/pkg/harness/self"
printf 'ядро\n' > "$d/pkg/harness/self/core.json"
g -C "$d/pkg" init -q -b master; g -C "$d/pkg" add -A; g -C "$d/pkg" commit -qm пакет
g init -q --bare "$d/remote.git"; g -C "$d/pkg" remote add jig "$d/remote.git"
cat > "$d/stubs/build-public.py" <<EOF
import json, os, sys
куда = sys.argv[sys.argv.index("--куда") + 1]
os.makedirs(куда, exist_ok=True)
open(os.path.join(куда, "README.md"), "w").write("сборка\n")
print(json.dumps({"пакет": "proba"}))
EOF
for judge in check-public-clean.py check-release-notes.py; do printf 'print("ок")\n' > "$d/stubs/$judge"; done
OUT=$( ( . "$T/lib.sh"
         PROJECT_DIR="$d/proj" SCRIPTS_REAL="$d/stubs" PACKAGE_DIR="$d/pkg" PUBLIC_BUILD_DIR="$d/build" \
         PUBLIC_REMOTE=jig PUBLIC_BRANCH=master PUBLIC_PASPORT="$d/pasport.conf" KONF_DVERI_KOREN="$d" \
         TYPICAL_COPY_CHECK="$d/stubs/check-public-clean.py" GIT_CONFIG_GLOBAL=/dev/null \
         опубликовать_сборку --сухо 2>&1 ); echo "rc=$?")
case_check "сухой прогон сборки: код 0" "да" "$(printf '%s' "$OUT" | grep -q 'rc=0' && echo да || echo "нет: ${OUT:0:200}")"
case_check "сборка: check-secrets.sh по каталогу сборки — 1 раз" "1" "$(calls "$d" "^secrets $d/build\$")"
case_check "сборка: check-secrets.sh по PROJECT_DIR — 0 раз" "0" "$(calls "$d" "^secrets $d/proj\$")"

[ "$CASES_PATHS" -gt 0 ] || { echo "FAIL: ни одного случая"; exit 1; }
echo "SELFTEST: $([ "$CASES_FAILED" = 0 ] && echo зелёный || echo КРАСНЫЙ) ($CASES_PATHS проверок, неудач $CASES_FAILED)"
[ "$CASES_FAILED" = 0 ]
