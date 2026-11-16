#!/usr/bin/env bash
# Проба: строка gates.jsonl от pre-commit-gate.sh несёт `secs` (секунды прогона)
# и `tree` (git write-tree проверенного индекса).
#
# Откуда взята. Спека эталонного выката §3 строка 13, §3а, фаза 7:
# очередь слияния второй раз гоняет pre-commit-gate.sh на том же дереве
# (60 с медиана × 77 слияний за две недели). Пропустить повтор очередь может,
# только если журнал коммита называет, КАКОЕ дерево он проверил: сейчас строка
# пишет head, staged, verdict, failed — дерева и секунд нет, судить нечем.
#
# Контракт, который судится:
#   * каждая строка журнала (pass, fail, yellow) несёт:
#       tree — `git write-tree` индекса, который судил хук: учитывается
#              GIT_INDEX_FILE (коммит `-o` идёт с временным индексом), не HEAD и
#              не рабочее дерево; 40 знаков hex;
#       secs — число секунд от начала прогона хука до записи (целое или дробное);
#   * поля head, staged, verdict, failed остаются.
#   Начало прогона хук берёт из `$SECONDS` своей оболочки или из метки верхнего
#   уровня с именем `GATES_*` (строка `GATES_...=` вне функций) — проба берёт в
#   свою библиотеку функции journal/fail/подвести/гейт и все строки `GATES_*=`
#   верхнего уровня РАЗБОРОМ исходника хука; пустое извлечение — провал.
#
# Стенд свой: временный git-репозиторий, свой LOG_DIR; боевой журнал не трогается.
# Дверь мутаций: PRECOMMIT_UNDER_TEST — хук, который судит проба (мутировать КОПИЮ).
set -u
here=$(cd "$(dirname "$0")" && pwd)
src=${PRECOMMIT_UNDER_TEST:-$here/pre-commit-gate.sh}
# shellcheck disable=SC1091
source "$here/lib/selftest_cases.sh"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
extract() { awk -v f="$1" 'index($0, f "() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src"; }

{
  echo 'set -euo pipefail'
  echo 'LOG_DIR="$1"'
  grep -E '^GATES_[A-Z0-9_]*=' "$src"
  echo 'GATES_LOG="$1/gates.jsonl"; HEAD_SHA=probe; CHANGED="b.txt"; YELLOW_CODE=77'
  grep -m1 '^GATE_FAILURES=' "$src"
  for f in journal fail подвести гейт; do
    body=$(extract "$f"); [ -n "$body" ] || { echo "FAIL: функция $f не найдена в хуке" >&2; exit 1; }
    echo "$body"
  done
} > "$tmp/lib.sh" || exit 1

R="$tmp/repo"
g() { GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 git -C "$R" -c user.name=p -c user.email=p@p "$@"; }
mkdir -p "$R"
g init -q
printf 'a0\n' > "$R/a.txt"; g add a.txt; g commit -qm начало
printf 'b-индекс\n' > "$R/b.txt"; g add b.txt
printf 'b-рабочее\n' > "$R/b.txt"          # рабочее дерево ≠ индекс
INDEX_TREE=$(g write-tree)
HEAD_TREE=$(g rev-parse 'HEAD^{tree}')
WORK_TREE=$(cd "$R" && GIT_INDEX_FILE="$tmp/work-index" git read-tree HEAD \
            && GIT_INDEX_FILE="$tmp/work-index" git add -A && GIT_INDEX_FILE="$tmp/work-index" git write-tree)
case_check "стенд: индекс, HEAD и рабочее дерево — три разных дерева" "да" \
    "$([ "$INDEX_TREE" != "$HEAD_TREE" ] && [ "$INDEX_TREE" != "$WORK_TREE" ] && echo да || echo нет)"

run() {  # $1 = тело сценария, $2 = номер; судится журнал $tmp/log-<n>, не вывод
    LOG="$tmp/log-$2"
    (cd "$R" && bash -c ". '$tmp/lib.sh' '$LOG'; $1") >/dev/null 2>&1 || true
}
field() {  # $1 = вердикт строки, $2 = поле → значение из ПОСЛЕДНЕЙ строки с этим вердиктом
    python3 - "$LOG/gates.jsonl" "$1" "$2" <<'PY'
import json, sys
from pathlib import Path
path, verdict, key = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []
rows = [r for r in rows if r.get("verdict") == verdict]
if not rows:
    print("нет-строки"); sys.exit()
value = rows[-1].get(key, "нет-поля")
print(json.dumps(value) if not isinstance(value, str) else value)
PY
}
secs_between() {  # $1 = значение secs, $2 = от, $3 = до → да/нет
    python3 -c 'import sys
try: v = float(sys.argv[1])
except ValueError: print("нет"); sys.exit()
print("да" if float(sys.argv[2]) <= v <= float(sys.argv[3]) else "нет")' "$1" "$2" "$3"
}

echo "── 1. pass: дерево индекса и секунды прогона ──"
run 'sleep 2; journal pass ""' 1
case_check "pass: tree = git write-tree индекса" "$INDEX_TREE" "$(field pass tree)"
case_check "pass: tree не HEAD и не рабочее дерево" "да" \
    "$(t=$(field pass tree); [ "$t" != "$HEAD_TREE" ] && [ "$t" != "$WORK_TREE" ] && echo да || echo нет)"
case_check "pass: secs — число от 2 до 30 (прогон спал 2 с)" "да" "$(secs_between "$(field pass secs)" 2 30)"
case_check "pass: поля head/staged/verdict/failed на месте" "probe|1|pass|" \
    "$(field pass head)|$(field pass staged)|$(field pass verdict)|$(field pass failed)"

echo "── 2. fail: подвести пишет то же дерево и секунды ──"
run 'sleep 1; false || fail "первый"; подвести' 2
case_check "fail: tree = git write-tree индекса" "$INDEX_TREE" "$(field fail tree)"
case_check "fail: secs — число от 1 до 30" "да" "$(secs_between "$(field fail secs)" 1 30)"
case_check "fail: failed называет гейт" "первый" "$(field fail failed)"

echo "── 3. yellow: жёлтый гейт пишет дерево ──"
run 'гейт "жёлтый" bash -c "exit 77"' 3
case_check "yellow: tree = git write-tree индекса" "$INDEX_TREE" "$(field yellow tree)"
case_check "yellow: secs — число от 0 до 30" "да" "$(secs_between "$(field yellow secs)" 0 30)"

echo "── 4. БОЛЬНОЙ СЛУЧАЙ: коммит -o — свой временный индекс (GIT_INDEX_FILE) ──"
ALT="$tmp/alt-index"
(cd "$R" && GIT_INDEX_FILE="$ALT" git read-tree HEAD && printf 'c\n' > c.txt && GIT_INDEX_FILE="$ALT" git add c.txt)
ALT_TREE=$(cd "$R" && GIT_INDEX_FILE="$ALT" git write-tree)
case_check "стенд: временный индекс ≠ основному" "да" "$([ "$ALT_TREE" != "$INDEX_TREE" ] && echo да || echo нет)"
LOG="$tmp/log-4"
(cd "$R" && GIT_INDEX_FILE="$ALT" bash -c ". '$tmp/lib.sh' '$LOG'; journal pass ''") >/dev/null 2>&1 || true
case_check "-o: tree = дерево временного индекса, не основного" "$ALT_TREE" "$(field pass tree)"

[ "$CASES_PATHS" -gt 0 ] || { echo "FAIL: ни одного случая"; exit 1; }
echo "SELFTEST: $([ "$CASES_FAILED" = 0 ] && echo зелёный || echo КРАСНЫЙ) ($CASES_PATHS проверок, неудач $CASES_FAILED)"
[ "$CASES_FAILED" = 0 ]
