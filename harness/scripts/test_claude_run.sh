#!/usr/bin/env bash
# Пробы носителя scripts/claude-run.sh (спека hr.standards-and-policy §4.3).
#
# Носитель — единственное место, где код зовёт claude как модель: он собирает
# личность класса сборщиком и отдаёт файл в --append-system-prompt-file. Беда,
# которую держат пробы, тихая: `--append-system-prompt-file ""` у claude даёт
# rc 0 без личности, так что сбой сборки не виден ни по коду, ни по выводу
# (Б-1 ревью спеки). Поэтому любой сбой сборки — код 79 и claude НЕ запущен.
#
# Проба создаёт своё условие: подставной claude пишет след (pid и доводы),
# подставной сборщик ведёт себя по сценарию, подставной alert-task.py пишет
# ключ сигнала в файл. Живой модели нет, живой LOG_DIR не трогается.
#
# Двери носителя:
#   CLAUDE_BIN          чем запускать модель
#   SELF_PROMPT_PY      сборщик (по умолчанию — scripts/self-prompt.py рядом с носителем)
#   HARNESS_ALERT_TASK  писатель сигналов (по умолчанию — scripts/alert-task.py)
#   SELF_ETALON_DIR     пробрасывается сборщику (случай деградации с настоящим сборщиком)
# Носитель ищется РЯДОМ с пробой: из копии дерева зовётся сама проба.
#
#   bash scripts/test_claude_run.sh
# Код возврата: 0 — все случаи прошли, 1 — есть упавший.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
CARRIER="$HERE/claude-run.sh"
source "$HERE/lib/selftest_cases.sh"

STAND="$(mktemp -d "${TMPDIR:-/tmp}/sap-ch2-XXXXXX")"
trap 'rm -rf "$STAND"' EXIT
mkdir -p "$STAND/bin" "$STAND/home" "$STAND/log" "$STAND/empty"
CLASS_FILE="$STAND/log/self-prompt/прибор.md"

# Подставная модель: pid (для проверки exec) и доводы через NUL — так видно
# каждый довод целиком, с пробелами, кавычками и переводами строк.
cat > "$STAND/bin/claude" <<'CLAUDE'
#!/usr/bin/env bash
printf '%s' "$$" > "$CLAUDE_TRACE.pid"
printf '%s\0' "$@" > "$CLAUDE_TRACE"
exit "${CLAUDE_RC:-0}"
CLAUDE
chmod +x "$STAND/bin/claude"

# Подставной сборщик. Пишет только внутрь стенда: носитель с пустым LOG_DIR
# иначе положил бы файл в /self-prompt/ или в живой каталог.
cat > "$STAND/bin/fake-self-prompt.py" <<'COLLECTOR'
import os, sys
args = sys.argv[1:]
cls = args[args.index("--класс") + 1] if "--класс" in args else ""
out = args[args.index("--в") + 1] if "--в" in args else ""
with open(os.environ["FAKE_ARGS"], "a", encoding="utf-8") as log:
    log.write(f"{cls}|{out}\n")
mode = os.environ.get("FAKE_MODE", "good")
if mode != "none" and out.startswith(os.environ["STAND"] + "/"):
    os.makedirs(os.path.dirname(out), exist_ok=True)
    text = {"good": "Метка сборки: проба-a1b2c3\nличность\n", "empty": "",
            "nolabel": "личность без метки\nвторая строка\n"}[mode]
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
sys.exit(int(os.environ.get("FAKE_RC", "0")))
COLLECTOR

# Ключ судится БОЕВЫМ valid_key из scripts/alert-task.py: ключ, который боевой
# писатель отвергает (rc 2), носитель глотает (|| true), и сигнал до владельца
# не доходит. Ключ без «:» был ровно такой дырой. Отвергнутый ключ ложится в
# журнал с пометкой «отвергнут:».
cat > "$STAND/bin/alert-task.py" <<'ALERT'
import importlib.util, os, sys
args = sys.argv[1:]
key = args[args.index("--key") + 1] if "--key" in args else "без-ключа:" + " ".join(args)
spec = importlib.util.spec_from_file_location("alert_task", os.environ["REAL_ALERT_TASK"])
real = importlib.util.module_from_spec(spec)
spec.loader.exec_module(real)
ok = real.valid_key(key)
with open(os.environ["ALERT_LOG"], "a", encoding="utf-8") as log:
    log.write(("" if ok else "отвергнут:") + key + "\n")
sys.exit(0 if ok else 2)
ALERT

export STAND REAL_ALERT_TASK="$HERE/alert-task.py"
export CLAUDE_TRACE="$STAND/след" FAKE_ARGS="$STAND/сборщик.txt" ALERT_LOG="$STAND/сигналы.txt"

reset_stand() {
    rm -rf "$STAND/log" "$CLAUDE_TRACE" "$CLAUDE_TRACE.pid"
    mkdir -p "$STAND/log"
    : > "$FAKE_ARGS"; : > "$ALERT_LOG"
}

# Носитель зовётся из каталога $1 абсолютным путём $2; доводы — остальное.
# Окружение — только стенда: чужие конфиги и PROJECT_DIR снимаются, корень
# носитель обязан найти по своему пути (Н-6). exec держит pid для проверки exec.
# $FAKE_DOOR=0 — без двери сборщика (настоящий scripts/self-prompt.py).
run_carrier() {
    local dir=$1 carrier=$2; shift 2
    local door=(SELF_PROMPT_PY="$STAND/bin/fake-self-prompt.py")
    [ "${FAKE_DOOR:-1}" = 0 ] && door=()
    (cd "$dir" && exec env -u PROJECT_DIR -u HARNESS_CONF -u HARNESS_INSTALL_CONF -u INSTALL_CONF \
        -u SELF_PROMPT_PY -u SELF_TREE_DIR -u LAUNCH_POLICY \
        HARNESS_HOME="$STAND/home" LOG_DIR="${CARRIER_LOG_DIR-$STAND/log}" \
        CLAUDE_BIN="$STAND/bin/claude" HARNESS_ALERT_TASK="$STAND/bin/alert-task.py" \
        SELF_ETALON_DIR="$STAND/нет-эталона" "${door[@]}" \
        bash "$carrier" "$@") > "$STAND/вывод.txt" 2>&1
}

yes_no() { "$@" && echo да || echo нет; }
alerts() { tr '\n' ' ' < "$ALERT_LOG" | sed 's/ $//'; }

echo "── носитель под пробой ──"
case_check "носитель есть: $CARRIER" да "$(yes_no test -f "$CARRIER")"
# Подставной писатель обязан судить ключ, иначе дыра вернётся молча.
: > "$ALERT_LOG"
python3 "$STAND/bin/alert-task.py" --key self-prompt-failed --title x >/dev/null 2>&1
case_check "подставной alert-task судит ключ боевым valid_key: ключ без «:» отвергнут" \
    отвергнут:self-prompt-failed "$(alerts)"

# Все отказы сборки: код 79, модель не запущена, сигнал self-prompt-failed один.
refusal() { # $1 имя, $2 режим сборщика, $3 код сборщика
    reset_stand
    FAKE_MODE=$2 FAKE_RC=$3 run_carrier "$STAND/empty" "$CARRIER" --класс прибор -p задание; local rc=$?
    case_check "$1: код 79" 79 "$rc"
    case_check "$1: claude не запущен" нет "$(yes_no test -e "$CLAUDE_TRACE")"
    case_check "$1: сигнал claude-run:self-prompt-failed ровно один, ключ принят боевым valid_key" \
        claude-run:self-prompt-failed "$(alerts)"
}
echo "── сбой сборки: модель не запускается ──"
refusal "сборщик вернул 1" good 1
refusal "сборщик вернул 0, файл пуст" empty 0
refusal "сборщик вернул 0, файла нет" none 0
refusal "сборщик вернул 0, первая строка не метка" nolabel 0
refusal "сборщик вернул 2" good 2

echo "── сборка удалась: доводы доходят до claude целыми и в том же порядке ──"
reset_stand
ARGS=(-p "два слова" "кавычки \"двойные\" и 'одинарные'" "" '$HOME `id` *' $'строка\nвторая' --model haiku)
FAKE_MODE=good FAKE_RC=0 run_carrier "$STAND/empty" "$CARRIER" --класс прибор "${ARGS[@]}"; RC=$?
case_check "сборка 0: код claude" 0 "$RC"
case_check "сборщик получил класс и файл класса в LOG_DIR/self-prompt" "прибор|$CLASS_FILE" "$(cat "$FAKE_ARGS")"
printf '%s\0' --append-system-prompt-file "$CLASS_FILE" "${ARGS[@]}" > "$STAND/ждём"
case_check "след: --append-system-prompt-file <файл> и все доводы как есть" да \
    "$(yes_no cmp -s "$STAND/ждём" "$CLAUDE_TRACE")"
case_check "носитель сам не добавил --strict-mcp-config (решение П-1)" 0 \
    "$(tr '\0' '\n' < "$CLAUDE_TRACE" 2>/dev/null | grep -cx -- --strict-mcp-config)"
case_check "сборка 0: сигналов нет" "" "$(alerts)"

reset_stand
FAKE_MODE=good FAKE_RC=0 run_carrier "$STAND/empty" "$CARRIER" --класс прибор -p x --strict-mcp-config
case_check "флаг вызывающего дошёл ровно один раз" 1 \
    "$(tr '\0' '\n' < "$CLAUDE_TRACE" 2>/dev/null | grep -cx -- --strict-mcp-config)"

reset_stand
CLAUDE_RC=7 FAKE_MODE=good FAKE_RC=0 run_carrier "$STAND/empty" "$CARRIER" --класс прибор -p x; RC=$?
case_check "код модели отдан вызывающему как есть" 7 "$RC"

echo "── exec: процесс носителя становится claude ──"
reset_stand
( cd "$STAND/empty" && exec env -u PROJECT_DIR -u HARNESS_CONF -u HARNESS_INSTALL_CONF -u INSTALL_CONF \
    HARNESS_HOME="$STAND/home" LOG_DIR="$STAND/log" CLAUDE_BIN="$STAND/bin/claude" \
    HARNESS_ALERT_TASK="$STAND/bin/alert-task.py" SELF_PROMPT_PY="$STAND/bin/fake-self-prompt.py" \
    FAKE_MODE=good FAKE_RC=0 bash "$CARRIER" --класс прибор -p x ) > /dev/null 2>&1 &
PID=$!
wait "$PID"
case_check "pid claude равен pid носителя" "$PID" "$(cat "$CLAUDE_TRACE.pid" 2>/dev/null)"

echo "── деградация: эталона нет — claude запущен, сигнал один ──"
reset_stand
FAKE_MODE=good FAKE_RC=3 run_carrier "$STAND/empty" "$CARRIER" --класс прибор -p x; RC=$?
case_check "сборщик 3: claude запущен" да "$(yes_no test -s "$CLAUDE_TRACE")"
case_check "сборщик 3: код claude" 0 "$RC"
case_check "сборщик 3: сигнал claude-run:self-etalon-missing ровно один, ключ принят боевым valid_key" \
    claude-run:self-etalon-missing "$(alerts)"

echo "── запуск из пустого каталога: корень по своему пути, настоящий сборщик (Н-6) ──"
# Приманка в текущем каталоге: носитель, ищущий корень от cwd, взял бы её.
mkdir -p "$STAND/decoy/scripts/lib"
cat > "$STAND/decoy/scripts/self-prompt.py" <<'DECOY'
import os
open(os.path.join(os.environ["STAND"], "приманка-взята"), "w").close()
DECOY
cp "$STAND/decoy/scripts/self-prompt.py" "$STAND/decoy/scripts/lib/config.sh"
reset_stand
FAKE_DOOR=0 run_carrier "$STAND/decoy" "$CARRIER" --класс прибор -p x; RC=$?
case_check "из чужого каталога: код claude" 0 "$RC"
case_check "приманка в текущем каталоге не взята" нет "$(yes_no test -e "$STAND/приманка-взята")"
case_check "файл класса собран настоящим сборщиком, первая строка — метка" да \
    "$(yes_no grep -qE '^Метка сборки: \S+-[0-9a-f]{6}$' <(head -1 "$CLASS_FILE" 2>/dev/null))"
case_check "claude получил этот файл" да \
    "$(yes_no cmp -s <(printf '%s\0' --append-system-prompt-file "$CLASS_FILE" -p x) "$CLAUDE_TRACE")"
case_check "эталона нет: сигнал claude-run:self-etalon-missing ровно один" claude-run:self-etalon-missing "$(alerts)"

reset_stand
ln -s "$CARRIER" "$STAND/empty/link.sh"
FAKE_DOOR=0 run_carrier "$STAND/empty" "$STAND/empty/link.sh" --класс прибор -p x; RC=$?
case_check "вызов через ссылку из пустого каталога: claude запущен" да "$(yes_no test -s "$CLAUDE_TRACE")"
rm -f "$STAND/empty/link.sh"

echo "── пустой LOG_DIR: отказ 79, а не файл в /self-prompt/ ──"
# Копия носителя с конфигом, который отдаёт пустой LOG_DIR: корень по своему
# пути приводит носитель именно к этому конфигу.
mkdir -p "$STAND/copy/scripts/lib"
cp "$CARRIER" "$STAND/copy/scripts/claude-run.sh" 2>/dev/null
cat > "$STAND/copy/scripts/lib/config.sh" <<'CONF'
konf_koren() { printf '%s' "$HARNESS_HOME"; }
konf_zagruzit() { LOG_DIR=""; return 0; }
CONF
reset_stand
CARRIER_LOG_DIR="" FAKE_MODE=good FAKE_RC=0 run_carrier "$STAND/empty" "$STAND/copy/scripts/claude-run.sh" \
    --класс прибор -p x; RC=$?
case_check "пустой LOG_DIR: код 79" 79 "$RC"
case_check "пустой LOG_DIR: claude не запущен" нет "$(yes_no test -e "$CLAUDE_TRACE")"
case_check "пустой LOG_DIR: сборщику не дан путь /self-prompt/…" 0 "$(grep -c '|/self-prompt/' "$FAKE_ARGS")"

printf '\nитог test_claude_run: случаев %s, неудач %s\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
