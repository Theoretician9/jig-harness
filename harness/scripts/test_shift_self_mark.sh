#!/usr/bin/env bash
# Проба доставки сигнала «смена поднята без личности»
# (спека §6.2 «при старте смены», контракт Н-5).
#
# Хук SessionStart (scripts/hooks/session_state.sh) идёт под claude, а боевой
# scripts/alert-task.py отказывает зовам из-под claude (called_from_session):
# сигнал из хука в бою не доходит. Поэтому хук только кладёт ОТМЕТКУ (проба
# scripts/test_session_carrier.sh), а сигнал по ней шлёт сторож сессий
# (harness/demons/session-warden.sh) на обходе — он демон, не под claude.
#
# Что судится — функция сторожа shift_self_mark_check (берётся из ЖИВОГО
# файла, дверь мутаций HARNESS_SESSION_WARDEN; весь сторож не запускается — он
# поднимал бы смену). Берутся все функции сторожа с именем shift_self_mark* и
# его alert_task и alert_task_rc (если есть); прочее (say, jlog, tg_send) — заглушки.
#
# Контракт функции:
#   отметка   ${SHIFT_SELF_MARK:-$LOG_DIR/shift-without-self.mark}, одна строка
#             «<epoch> <причина>» (пишет хук);
#   есть      → ровно один зов писателя «--key session-state:shift-without-self
#             --watcher session-warden» с причиной из отметки в доводах;
#   та же     → на следующих обходах ни одного зова: отправленную отметку сторож
#             помнит сам (состояние под $LOG_DIR), на дедупликацию alert-task не
#             полагается — потолок шума держит сторож;
#   новая     (другой epoch) → снова один зов: это новый подъём без личности;
#   нет, а сигнал был → ровно один «--resolve session-state:shift-without-self»,
#             дальше — тишина;
#   resolve отказал (писатель вернул не 0) → отправленное сторож помнит дальше,
#             и следующий обход повторяет resolve: иначе сигнал красный навсегда,
#             а сторож считает его снятым;
#   битая или пустая → ни одного «--key», rc 0;
#   всегда    rc 0: сторож живёт под set -euo pipefail.
# Писатель — подставной: лежит и в $PROJECT_DIR/scripts/alert-task.py (туда
# смотрит alert_task сторожа), и в $HARNESS_ALERT_TASK. Ключ судится БОЕВЫМ
# valid_key из scripts/alert-task.py: ключ, который боевой писатель отвергает,
# сторож глотает (|| true), и сигнал не доходит.
#
# Имена переменных латиницей: bash не берёт кириллические идентификаторы.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

WARDEN="${HARNESS_SESSION_WARDEN:-$HERE/../harness/demons/session-warden.sh}"
# В пакете демоны лежат рядом со scripts (harness/demons), в проекте — под harness/.
[ -f "$WARDEN" ] || WARDEN="$HERE/../demons/session-warden.sh"
[ -f "$WARDEN" ] || { echo "ПЛОХО: сторожа нет по пути раскладки — проверять нечего"; exit 1; }
REAL_ALERT_TASK="$HERE/alert-task.py"
[ -f "$REAL_ALERT_TASK" ] || { echo "ПЛОХО: боевого alert-task.py нет рядом с пробой — ключ судить нечем"; exit 1; }
KEY=session-state:shift-without-self

BASE=$(mktemp -d /tmp/sap-proba-XXXXXX)
trap 'rm -rf "$BASE"' EXIT

FNS="$BASE/fns.sh"
mapfile -t FN_NAMES < <(grep -oE '^(shift_self_mark[A-Za-z0-9_]*|alert_task(_rc)?)\(\) \{' "$WARDEN" | sed 's/() {$//')
for fn in "${FN_NAMES[@]}"; do
    sed -n "/^$fn() {/,/^}/p" "$WARDEN" >> "$FNS"
done
touch "$FNS"
for fn in shift_self_mark_check alert_task; do
    grep -q "^$fn() {" "$FNS" || echo "  (функции $fn в сторожe $WARDEN нет — случаи ниже красные)"
done

# Подставной писатель: каждый зов — строка JSON {"args": [...], "valid": bool}.
# Дверь ALERT_FAIL_RESOLVE=1 — отказ на --resolve (код 3), как боевой писатель
# отказывает зову из-под claude.
cat > "$BASE/alert.py" <<'ALERT'
import importlib.util, json, os, sys
args = sys.argv[1:]
key = None
for flag in ("--key", "--resolve"):
    if flag in args and args.index(flag) + 1 < len(args):
        key = args[args.index(flag) + 1]
spec = importlib.util.spec_from_file_location("alert_task", os.environ["REAL_ALERT_TASK"])
real = importlib.util.module_from_spec(spec)
spec.loader.exec_module(real)
ok = key is not None and real.valid_key(key)
with open(os.environ["ALERT_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"args": args, "valid": ok}, ensure_ascii=False) + "\n")
if "--resolve" in args and os.environ.get("ALERT_FAIL_RESOLVE"):
    sys.exit(3)
sys.exit(0 if ok else 2)
ALERT

# Стенд: $1 = имя. Свои каталоги проекта и журналов, писатель в обоих местах.
make_stand() {
    local s="$BASE/$1"
    mkdir -p "$s/proj/scripts" "$s/log"
    cp "$BASE/alert.py" "$s/proj/scripts/alert-task.py"
    : > "$s/calls"
    printf '%s' "$s"
}

# Один обход: функция сторожа на стенде $1 под set -euo pipefail. Печатает rc.
# $MARK_DOOR (если задан) — дверь SHIFT_SELF_MARK; иначе дверь снята.
pass() {
    local s="$1" door=()
    [ -n "${MARK_DOOR:-}" ] && door=(SHIFT_SELF_MARK="$MARK_DOOR")
    env -u SHIFT_SELF_MARK PROJECT_DIR="$s/proj" LOG_DIR="$s/log" HARNESS_SCRIPTS="$HERE" \
        HARNESS_ALERT_TASK="$s/proj/scripts/alert-task.py" REAL_ALERT_TASK="$REAL_ALERT_TASK" \
        ALERT_LOG="$s/calls" SAY_LOG="$s/say.log" FNS="$FNS" "${door[@]}" \
        bash -c 'set -euo pipefail
                 say() { printf "%s\n" "$*" >> "$SAY_LOG"; }
                 jlog() { :; }; tg_send() { :; }
                 source "$FNS"
                 shift_self_mark_check' >/dev/null 2>&1
    echo $?
}

# Счёт зовов стенда $1 начиная со строки $2 (0 — все): $3 = all | raise | resolve | good-raise.
# good-raise — «--key $KEY», ключ принят боевым valid_key, «--watcher session-warden».
count() {
    python3 - "$1/calls" "$2" "$3" "$KEY" <<'PY'
import json, sys
path, start, kind, key = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()][start:]
def arg(row, flag):
    a = row["args"]
    return a[a.index(flag) + 1] if flag in a and a.index(flag) + 1 < len(a) else None
pick = {
    "all": lambda r: True,
    "raise": lambda r: "--key" in r["args"],
    "resolve": lambda r: arg(r, "--resolve") == key,
    "good-raise": lambda r: arg(r, "--key") == key and r["valid"] and arg(r, "--watcher") == "session-warden",
}[kind]
print(sum(1 for r in rows if pick(r)))
PY
}
# Что сторож помнит отправленным на стенде $1 (пусто — ничего).
sent_of() { head -n1 "$1/log/shift-without-self.sent" 2>/dev/null || true; }
total() { grep -c . "$1/calls" 2>/dev/null | tr -d ' '; }
# Причина из отметки есть в доводах последнего зова «--key».
reason_in_raise() {
    python3 - "$1/calls" "$2" <<'PY'
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
raises = [r for r in rows if "--key" in r["args"]]
print("да" if raises and any(sys.argv[2] in a for a in raises[-1]["args"]) else "нет")
PY
}

echo "── подставной писатель судит ключ боевым valid_key ──"
S=$(make_stand sanity)
ALERT_LOG="$S/calls" REAL_ALERT_TASK="$REAL_ALERT_TASK" \
    python3 "$S/proj/scripts/alert-task.py" --key shift-without-self --title x >/dev/null 2>&1
case_check "П0: ключ без «:» отвергнут боевым valid_key" "0" "$(count "$S" 0 good-raise)"
: > "$S/calls"

echo "── отметки нет и не было — тишина ──"
S=$(make_stand quiet)
case_check "М1: отметки нет, сигнала не было — rc 0" "0" "$(pass "$S")"
case_check "М1: ни одного зова писателя" "0" "$(total "$S")"

echo "── отметка есть — ровно один сигнал, повтор — ни одного ──"
S=$(make_stand mark)
REASON="claude без --append-system-prompt-file"
printf '%s %s\n' "$(date +%s)" "$REASON" > "$S/log/shift-without-self.mark"
case_check "М2: отметка есть — rc 0" "0" "$(pass "$S")"
case_check "М2: ровно один зов писателя" "1" "$(total "$S")"
case_check "М2: он — --key $KEY --watcher session-warden, ключ принят боевым valid_key" "1" \
    "$(count "$S" 0 good-raise)"
case_check "М2: причина из отметки в доводах сигнала" "да" "$(reason_in_raise "$S" "$REASON")"
n=$(total "$S"); rcs=""
for _ in 1 2 3 4 5; do rcs="$rcs$(pass "$S")"; done
case_check "М3: пять обходов с той же отметкой — rc 0 каждый" "00000" "$rcs"
case_check "М3: пять обходов с той же отметкой — новых зовов 0 (потолок шума)" "0" "$(count "$S" "$n" all)"

echo "── отметка снята — один --resolve, дальше тишина ──"
rm -f "$S/log/shift-without-self.mark"
n=$(total "$S")
case_check "М4: отметки нет, сигнал был — rc 0" "0" "$(pass "$S")"
case_check "М4: ровно один --resolve $KEY" "1" "$(count "$S" "$n" resolve)"
case_check "М4: и ни одного --key" "0" "$(count "$S" "$n" raise)"
case_check "М4: resolve прошёл — отправленного нет" "" "$(sent_of "$S")"
n=$(total "$S")
pass "$S" >/dev/null; pass "$S" >/dev/null
case_check "М5: следующие обходы без отметки — новых зовов 0" "0" "$(count "$S" "$n" all)"

echo "── новая отметка (новый подъём без личности) — снова один сигнал ──"
REASON2="метка файла «x» не равна текущей сборке «y»"
printf '%s %s\n' "$(( $(date +%s) + 1 ))" "$REASON2" > "$S/log/shift-without-self.mark"
n=$(total "$S")
case_check "М6: новая отметка — rc 0" "0" "$(pass "$S")"
case_check "М6: ровно один новый зов, это сигнал $KEY" "1 1" \
    "$(count "$S" "$n" all) $(count "$S" "$n" good-raise)"
case_check "М6: причина новой отметки в доводах" "да" "$(reason_in_raise "$S" "$REASON2")"

echo "── resolve отказал — отправленное помнится, следующий обход повторяет ──"
S=$(make_stand refuse)
printf '%s %s\n' "$(date +%s)" "родитель не claude" > "$S/log/shift-without-self.mark"
pass "$S" >/dev/null
sent0=$(sent_of "$S")
case_check "Р0: сигнал отправлен — сторож помнит отметку" "да" "$([ -n "$sent0" ] && echo да || echo нет)"
rm -f "$S/log/shift-without-self.mark"
n=$(total "$S")
case_check "Р1: resolve отказал — rc 0" "0" "$(ALERT_FAIL_RESOLVE=1 pass "$S")"
case_check "Р1: зов --resolve $KEY был" "1" "$(count "$S" "$n" resolve)"
case_check "Р1: отправленное осталось прежним" "$sent0" "$(sent_of "$S")"
n=$(total "$S")
ALERT_FAIL_RESOLVE=1 pass "$S" >/dev/null
case_check "Р2: следующий обход повторяет --resolve" "1" "$(count "$S" "$n" resolve)"
n=$(total "$S")
case_check "Р3: писатель принял — rc 0" "0" "$(pass "$S")"
case_check "Р3: ровно один --resolve" "1" "$(count "$S" "$n" resolve)"
case_check "Р3: отправленного больше нет" "" "$(sent_of "$S")"
n=$(total "$S")
pass "$S" >/dev/null
case_check "Р4: после снятия — новых зовов 0" "0" "$(count "$S" "$n" all)"

echo "── битая отметка — не падает, сигнала с мусором нет ──"
S=$(make_stand empty); : > "$S/log/shift-without-self.mark"
case_check "Б1: пустая отметка — rc 0" "0" "$(pass "$S")"
case_check "Б1: пустая отметка — ни одного --key" "0" "$(count "$S" 0 raise)"
S=$(make_stand junk); printf 'не-число мусор\nвторая\n' > "$S/log/shift-without-self.mark"
case_check "Б2: отметка без epoch — rc 0" "0" "$(pass "$S")"
case_check "Б2: отметка без epoch — ни одного --key" "0" "$(count "$S" 0 raise)"
S=$(make_stand bin); head -c 64 /dev/urandom > "$S/log/shift-without-self.mark"
case_check "Б3: двоичный мусор — rc 0" "0" "$(pass "$S")"
case_check "Б3: двоичный мусор — ни одного --key" "0" "$(count "$S" 0 raise)"
S=$(make_stand nolog); rm -rf "$S/log"
case_check "Б4: каталога журналов нет — rc 0" "0" "$(pass "$S")"

echo "── дверь SHIFT_SELF_MARK ──"
S=$(make_stand door); mkdir -p "$S/door"
printf '%s %s\n' "$(date +%s)" "родитель не claude" > "$S/door/shift.mark"
case_check "Д1: отметка по двери — rc 0" "0" "$(MARK_DOOR="$S/door/shift.mark" pass "$S")"
case_check "Д1: отметка по двери — ровно один сигнал $KEY" "1 1" \
    "$(total "$S") $(count "$S" 0 good-raise)"

echo
if [ "$CASES_FAILED" -eq 0 ]; then
    echo "ПРОБА ЗЕЛЁНАЯ: случаев $CASES_PATHS, падений 0"
    exit 0
fi
echo "ПРОБА КРАСНАЯ: падений $CASES_FAILED из $CASES_PATHS случаев"
exit 1
