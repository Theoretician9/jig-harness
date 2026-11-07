#!/usr/bin/env bash
# Проба: сторож сессий подключает lib/pane.sh от настроенного каталога скриптов
# (карточка hr.warden-pane-hard-path) и пишет флаг ротации ОДНОЙ дверью — CLI
# scripts/lib/rotation_flag.py, а не своим jq (карточка hr.rotation-flag-cli,
# замечание M-03 ревью кода А).
#
# Почему стенд, а не самотест сторожа: жёсткий путь ../../scripts/lib/pane.sh
# верен только в раскладке проекта; в раскладке пакета (harness/demons рядом с
# harness/scripts) его нет, и самотест сторожа из пакета падал 86 из 114.
# Проба раскладывает ОБЕ раскладки во временном каталоге и запускает начало
# живого файла сторожа — до строки подключения pane.sh включительно.
#
# Флаг: две точки записи (jq сторожа и библиотека) разошлись бы в полях и в
# правилах переноса «с»/«причина»/«принуждена»/«сказано». Договор CLI:
#   rotation_flag.py --set --stage S --helpers N --waited SEC
#                    [--forced 0|1] [--told EPOCH] [--session ID] [--reason TEXT]
#   rotation_flag.py --clear
# Функции сторожа берутся из ЖИВОГО файла (дверь мутаций —
# HARNESS_SESSION_WARDEN), библиотека — дверь HARNESS_ROTATION_FLAG_PY.
set -uo pipefail
export LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

WARDEN="${HARNESS_SESSION_WARDEN:-$HERE/../harness/demons/session-warden.sh}"
# В пакете демоны лежат рядом со scripts (harness/demons), в проекте — под harness/.
[ -f "$WARDEN" ] || WARDEN="$HERE/../demons/session-warden.sh"
[ -f "$WARDEN" ] || { echo "ПЛОХО: сторожа нет по пути раскладки — проверять нечего"; exit 1; }
FLAG_PY="${HARNESS_ROTATION_FLAG_PY:-$HERE/lib/rotation_flag.py}"
[ -f "$FLAG_PY" ] || { echo "ПЛОХО: библиотеки флага нет: $FLAG_PY"; exit 1; }

STAND=$(mktemp -d -t warden-pane-flag-XXXXXX)
trap 'rm -rf "$STAND"' EXIT

body() { sed -n "/^$1() {/,/^}/p" "$WARDEN"; }

# ── 1. pane.sh от настроенного каталога ─────────────────────────────────────
echo "1. сторож находит lib/pane.sh в обеих раскладках"
PREAMBLE="$STAND/preamble.sh"
awk '{ print } /^source .*lib\/pane\.sh/ { exit }' "$WARDEN" > "$PREAMBLE"
case_check "начало сторожа до подключения pane.sh вынуто из файла" \
    "да" "$(grep -q 'lib/pane\.sh' "$PREAMBLE" && grep -q '^HARNESS_SCRIPTS=' "$PREAMBLE" && echo да || echo нет)"

# $1 = корень стенда, $2.. = каталоги scripts, куда положить заглушку pane.sh
layout() {
    local root="$STAND/$1" dir; shift
    mkdir -p "$root/harness/demons"
    { cat "$PREAMBLE"
      printf '%s\n' 'printf "%s|%s\n" "${PANE_STUB_ID:-нет}" "$(readlink -f "$HARNESS_SCRIPTS")"'
    } > "$root/harness/demons/session-warden.sh"
    for dir in "$@"; do
        mkdir -p "$root/$dir/lib"
        printf 'PANE_STUB_ID=%s\n' "$dir" > "$root/$dir/lib/pane.sh"
    done
}
run_preamble() { bash "$1" 2>/dev/null; echo "rc=$?"; }

layout pkg harness/scripts
case_check "пакет (harness/demons + harness/scripts): pane.sh найден у harness/scripts" \
    "harness/scripts|$STAND/pkg/harness/scripts rc=0" \
    "$(run_preamble "$STAND/pkg/harness/demons/session-warden.sh" | paste -sd' ')"

layout proj scripts
case_check "проект (harness/demons + scripts в корне): pane.sh найден у scripts" \
    "scripts|$STAND/proj/scripts rc=0" \
    "$(run_preamble "$STAND/proj/harness/demons/session-warden.sh" | paste -sd' ')"

layout both scripts harness/scripts
case_check "обе копии: подключена та, что в выбранном HARNESS_SCRIPTS" \
    "scripts|$STAND/both/scripts rc=0" \
    "$(run_preamble "$STAND/both/harness/demons/session-warden.sh" | paste -sd' ')"

mkdir -p "$STAND/elsewhere"
ln -s "$STAND/pkg/harness/demons/session-warden.sh" "$STAND/elsewhere/warden-link.sh"
case_check "пакет через символьную ссылку: путь считается от настоящего файла" \
    "harness/scripts|$STAND/pkg/harness/scripts rc=0" \
    "$(run_preamble "$STAND/elsewhere/warden-link.sh" | paste -sd' ')"

layout none harness/scripts
rm -f "$STAND/none/harness/scripts/lib/pane.sh"
case_check "обратный: pane.sh нет нигде — сторож падает, а не идёт дальше без него" \
    "нет" "$(run_preamble "$STAND/none/harness/demons/session-warden.sh" | grep -q 'rc=0' && echo да || echo нет)"

case_check "строка подключения pane.sh идёт от \$HARNESS_SCRIPTS, жёсткого ../../scripts нет" \
    "да" "$(grep -E '^source .*lib/pane\.sh' "$WARDEN" | grep -q '\$HARNESS_SCRIPTS/lib/pane\.sh' \
            && ! grep -qE '^source .*\.\./\.\./scripts/lib/pane\.sh' "$WARDEN" && echo да || echo нет)"

# Строка pane.sh — не единственная: любой другой путь ../../scripts без запасной
# ветки роняет сторожа в раскладке пакета так же (ревью хвоста А, Б1/Б2). Судим
# ВЕСЬ файл: такой путь допустим только присваиванием, за которым следующей
# строкой идёт запасная ветка «|| ТО_ЖЕ_ИМЯ=».
HARD_PATHS_PY='
import re, sys
lines = open(sys.argv[1], encoding="utf-8").read().split("\n")
bad = []
for i, line in enumerate(lines):
    if "../../scripts" not in line or line.lstrip().startswith("#"):
        continue
    name = re.match(r"\s*(\w+)=", line)
    after = lines[i + 1] if i + 1 < len(lines) else ""
    if not (name and re.search(r"\|\|\s*" + name.group(1) + "=", after)):
        bad.append(str(i + 1))
print(" ".join(bad) or "нет")
'
case_check "во всём стороже нет пути ../../scripts без запасной ветки (номера строк)" "нет" \
    "$(python3 -c "$HARD_PATHS_PY" "$WARDEN")"

# Живой запуск ЦЕЛОГО сторожа из раскладки пакета: настоящие скрипты под
# harness/scripts, конфигов нет — сторож обязан дойти до внятного отказа
# «нет install.conf», а не умереть на source несуществующего файла.
LIVE="$STAND/pkg-live"; mkdir -p "$LIVE/harness/demons" "$STAND/live-state"
cp "$WARDEN" "$LIVE/harness/demons/session-warden.sh"
ln -s "$(readlink -f "$HERE")" "$LIVE/harness/scripts"
LIVE_OUT="$(cd "$STAND" && env -u TMUX HARNESS_CONF="$STAND/нет-конфига" \
    HARNESS_INSTALL_CONF="$STAND/нет-конфига" HARNESS_PANEL_STATE="$STAND/live-state" \
    timeout 20 bash "$LIVE/harness/demons/session-warden.sh" 2>&1)"
case_check "сторож из раскладки пакета доходит до отказа «нет install.conf», без «No such file»" \
    "отказ-по-конфигу без-no-such-file" \
    "$(grep -qF "session-warden: нет $STAND/нет-конфига" <<<"$LIVE_OUT" && echo отказ-по-конфигу || echo не-дошёл) $(grep -q 'No such file' <<<"$LIVE_OUT" && echo no-such-file || echo без-no-such-file)"

# ── 2. CLI библиотеки флага ─────────────────────────────────────────────────
echo "2. rotation_flag.py пишет и снимает флаг сам"
STATE="$STAND/state"; mkdir -p "$STATE"
FLAG="$STATE/rotation-waits-helpers.json"
cli() { HARNESS_PANEL_STATE="$STATE" ROTATE_FLAG_STALE_MIN=30 python3 "$FLAG_PY" "$@" >/dev/null 2>&1; echo $?; }
field() { python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print(json.dumps(v, ensure_ascii=False))' "$FLAG" "$1" 2>/dev/null || echo "нет-файла"; }

case_check "--set: код 0" "0" \
    "$(cli --set --stage ждёт --helpers 2 --waited 30 --session S1 --reason 'окно полно')"
case_check "--set: поля флага (сессия, этап, помощников числом, ждали_сек, принуждена, сказано)" \
    '"S1" "ждёт" 2 30 0 null "окно полно"' \
    "$(printf '%s ' "$(field сессия)" "$(field этап)" "$(field помощников)" "$(field ждали_сек)" \
        "$(field принуждена)" "$(field сказано)" "$(field причина)" | sed 's/ $//')"
case_check "--set: «с» и «проверено» — эпоха записи" "да" \
    "$(python3 -c 'import json,sys,time; d=json.load(open(sys.argv[1])); n=time.time(); print("да" if abs(d["с"]-n)<60 and d["с"]==d["проверено"] else "нет")' "$FLAG" 2>/dev/null || echo нет)"

python3 - "$FLAG" <<'PY' 2>/dev/null
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d["с"] = 1700000000; json.dump(d, open(p, "w"), ensure_ascii=False)
PY
cli --set --stage ждёт --helpers 1 --waited 60 --forced 1 --told 1786500000 >/dev/null
cli --set --stage идёт --helpers 0 --waited 90 --reason 'другая' >/dev/null
case_check "повторный --set переносит «с», «причина», «принуждена», «сказано» и меняет этап" \
    '1700000000 "окно полно" 1 1786500000 "идёт" 0 90' \
    "$(printf '%s ' "$(field с)" "$(field причина)" "$(field принуждена)" "$(field сказано)" \
        "$(field этап)" "$(field помощников)" "$(field ждали_сек)" | sed 's/ $//')"
case_check "после записи во каталоге нет временных хвостов" "1" "$(find "$STATE" -mindepth 1 | wc -l)"
case_check "--state читает записанное" "0" "$(cli --state)"

# Эталон ставит сама проба, а не CLI: иначе отказ предыдущих записей оставлял
# сравнение двух отсутствующих файлов (ревью хвоста А, З5).
printf '{"этап":"эталон","с":1700000000,"помощников":1}' > "$FLAG"
cp "$FLAG" "$STAND/flag.before"
case_check "обратный: --set с нечисловым --waited отвергнут (код 2)" "2" \
    "$(cli --set --stage ждёт --helpers 1 --waited много)"
case_check "обратный: отвергнутая запись флаг не тронула" "да" \
    "$(cmp -s "$FLAG" "$STAND/flag.before" && echo да || echo нет)"
case_check "обратный: неизвестный ключ — код 2" "2" "$(cli --bogus)"

# --helpers — неотрицательное целое ASCII-цифрами (так его даёт сторож); иное —
# отказ с кодом 2, а не строка в флаге и не трассировка (ревью хвоста А, З4).
helpers_reject() {
    local err rc
    err=$(HARNESS_PANEL_STATE="$STATE" ROTATE_FLAG_STALE_MIN=30 python3 "$FLAG_PY" \
        --set --stage ждёт --helpers "$1" --waited 5 2>&1 >/dev/null); rc=$?
    printf '%s:%s:%s\n' "$1" "$rc" "$(grep -q Traceback <<<"$err" && echo трасса || echo чисто)"
}
case_check "обратный: --helpers «-1», «1.5», «١», «²» — код 2 без трассировки" \
    "-1:2:чисто 1.5:2:чисто ١:2:чисто ²:2:чисто" \
    "$(for v in -1 1.5 ١ ²; do helpers_reject "$v"; done | paste -sd' ')"
case_check "обратный: отвергнутые --helpers флаг не тронули" "да" \
    "$(cmp -s "$FLAG" "$STAND/flag.before" && echo да || echo нет)"
# «?» — законное значение: сторож ставит его, когда каталог помощников непуст, а
# сосчитать их нельзя (JUDGE_HELPERS="?"); отказ CLI оставил бы смену без флага
# ровно тогда, когда помощники точно есть (ревью-2 хвоста А, З7).
case_check "--helpers «?» принят: код 0, в флаге строкой" '0 "?"' \
    "$(cli --set --stage ждёт --helpers '?' --waited 5) $(field помощников)"

: > "$STATE/пауза-сказано-приём"
case_check "--clear: код 0" "0" "$(cli --clear)"
case_check "--clear снимает флаг и метку «принято на ожидании»" "нет нет" \
    "$([ -e "$FLAG" ] && echo есть || echo нет) $([ -e "$STATE/пауза-сказано-приём" ] && echo есть || echo нет)"
case_check "--clear без флага — код 0 (снимать нечего)" "0" "$(cli --clear)"

# ── 3. сторож пишет флаг через CLI ──────────────────────────────────────────
echo "3. сторож зовёт CLI, своей записи флага у него нет"
FUNCS="$STAND/flagfuncs.sh"
{ body rotation_flag_read; body rotation_flag_put; body rotation_flag_clear; } > "$FUNCS"
case_check "функции флага вынуты из файла сторожа" "да" \
    "$(grep -q '^rotation_flag_put() {' "$FUNCS" && grep -q '^rotation_flag_clear() {' "$FUNCS" && echo да || echo нет)"

STUB="$STAND/stub-scripts"; mkdir -p "$STUB/lib"
cat > "$STUB/lib/rotation_flag.py" <<'PY'
import os, sys
with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as out:
    out.write(" ".join(sys.argv[1:]) + "\n")
rc = int(os.environ.get("STUB_RC", "0"))
if rc:
    sys.stderr.write("трасса\nпричина-заглушки\n")
sys.exit(rc)
PY
CALLS="$STAND/calls.txt"
WSTATE="$STAND/wstate"; mkdir -p "$WSTATE"
# PANEL_ENV — HARNESS_PANEL_STATE в окружении сторожа, FLAG_DIR — его
# ROTATION_STATE_DIR; по умолчанию оба $WSTATE.
warden_call() {  # $1 = каталог скриптов, $2 = код заглушки, $3.. = вызов функции
    local scripts="$1" rc="$2"; shift 2
    HARNESS_PANEL_STATE="${PANEL_ENV:-$WSTATE}" STUB_CALLS="$CALLS" STUB_RC="$rc" bash -c '
        set -uo pipefail
        say() { printf "SAY %s\n" "$*"; }; jlog() { :; }
        source "$1"; HARNESS_SCRIPTS="$2"; ROTATION_STATE_DIR="$3"
        ROTATION_FLAG="$3/rotation-waits-helpers.json"
        SHIFT_SESSION_ID=S7 REASON="тишина"; shift 3; "$@"' \
        _ "$FUNCS" "$scripts" "${FLAG_DIR:-$WSTATE}" "$@" 2>&1
}

: > "$CALLS"; warden_call "$STUB" 0 rotation_flag_put идёт 3 45 1 1786500000 >/dev/null
case_check "rotation_flag_put зовёт rotation_flag.py --set со всеми полями" \
    "--set --stage идёт --helpers 3 --waited 45 --forced 1 --told 1786500000 --session S7 --reason тишина" \
    "$(head -1 "$CALLS")"
: > "$CALLS"; warden_call "$STUB" 0 rotation_flag_put ждёт 0 0 >/dev/null
case_check "rotation_flag_put без принуждения и «сказано» их не передаёт" \
    "--set --stage ждёт --helpers 0 --waited 0 --session S7 --reason тишина" "$(head -1 "$CALLS")"
: > "$CALLS"; : > "$WSTATE/rotation-waits-helpers.json"; warden_call "$STUB" 0 rotation_flag_clear проба >/dev/null
case_check "rotation_flag_clear зовёт rotation_flag.py --clear" "--clear" "$(head -1 "$CALLS")"
: > "$CALLS"
# Причина — последняя строка stderr CLI: без неё в журнале только «не вышло» (ревью хвоста А, З2).
case_check "обратный: отказ CLI — сторож говорит «записать не вышло» с причиной, а не молчит" "да" \
    "$(warden_call "$STUB" 1 rotation_flag_put идёт 1 5 | grep -q 'флаг ротации записать не вышло: .*: причина-заглушки$' && echo да || echo нет)"

case_check "в файле сторожа нет своей записи флага (jq/mv/rm/> в \$ROTATION_FLAG)" "0" \
    "$(grep -cE '(>|mv[^|]*|rm[^|]*)[[:space:]]"\$ROTATION_FLAG"' "$WARDEN")"
case_check "rotation_flag_put не собирает JSON сам (нет jq в теле)" "0" \
    "$(body rotation_flag_put | grep -c 'jq')"

LIBDIR="$(dirname "$FLAG_PY")"
REAL="$STAND/real-scripts"; mkdir -p "$REAL"; ln -s "$LIBDIR" "$REAL/lib"
rm -f "$WSTATE"/*; warden_call "$REAL" 0 rotation_flag_put ждёт 2 30 >/dev/null
case_check "сквозной: сторож с настоящей библиотекой — флаг записан с этапом и сессией" "ждёт S7 2" \
    "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("этап"), d.get("сессия"), d.get("помощников"))' \
        "$WSTATE/rotation-waits-helpers.json" 2>/dev/null || echo нет)"

# Договор сторожа и CLI (ревью хвоста А, З1): флаг — это $ROTATION_FLAG сторожа,
# и CLI обязан писать и снимать именно его. Сторож передаёт CLI свой каталог
# состояния: HARNESS_PANEL_STATE="$ROTATION_STATE_DIR" python3 …/rotation_flag.py.
# Иначе подмена ROTATION_STATE_DIR без HARNESS_PANEL_STATE (так делают стенды
# других проб) увела бы запись в боевой каталог панели. Здесь они РАЗНЫЕ.
SPLIT="$STAND/wstate-split"; PANEL_OTHER="$STAND/panel-default"; mkdir -p "$SPLIT" "$PANEL_OTHER"
SFLAG="rotation-waits-helpers.json"
PANEL_ENV="$PANEL_OTHER" FLAG_DIR="$SPLIT" warden_call "$REAL" 0 rotation_flag_put ждёт 1 10 >/dev/null
case_check "разные каталоги: put пишет в \$ROTATION_FLAG сторожа, не в каталог по умолчанию CLI" "есть нет" \
    "$([ -e "$SPLIT/$SFLAG" ] && echo есть || echo нет) $([ -e "$PANEL_OTHER/$SFLAG" ] && echo есть || echo нет)"
printf '{}' > "$SPLIT/$SFLAG"; printf '{}' > "$PANEL_OTHER/$SFLAG"
PANEL_ENV="$PANEL_OTHER" FLAG_DIR="$SPLIT" warden_call "$REAL" 0 rotation_flag_clear проба >/dev/null
case_check "разные каталоги: clear снимает \$ROTATION_FLAG сторожа, чужой файл не трогает" "нет есть" \
    "$([ -e "$SPLIT/$SFLAG" ] && echo есть || echo нет) $([ -e "$PANEL_OTHER/$SFLAG" ] && echo есть || echo нет)"

printf '\nСТОРОЖ: PANE.SH И ФЛАГ РОТАЦИИ: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
