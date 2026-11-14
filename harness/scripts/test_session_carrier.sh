#!/usr/bin/env bash
# Проба сверки носителя личности при старте смены
# (спека §6.2 «при старте смены», контракт Н-5).
#
# Хук SessionStart (scripts/hooks/session_state.sh) обязан заметить смену,
# поднятую без личности: голым claude мимо komanda_agenta, своей AGENT_START_CMD,
# со старым файлом личности. Иначе смена работает без стандартов, и никто об
# этом не знает.
#
# Родитель хука — НЕ claude. Замер 10.11.2026 (живой SessionStart под haiku):
#   hook.sh (bash) → «/bin/sh -c $CLAUDE_PROJECT_DIR/hook.sh» → claude … → …
# $PPID хука — промежуточная sh. Поэтому хук ищет вверх по цепочке предков
# БЛИЖАЙШИЙ процесс с argv0 claude (имя файла argv0, путь допустим), а не верит
# $PPID. Ближайший claude с -p/--print — не смена (SessionStart идёт и у каждого
# claude -p, запущенного из каталога проекта: демоны, приборы) — хук молчит.
#
# Двери хука, которые судит проба:
#   SESSION_PROC_ROOT   корень дерева процессов (по умолчанию /proc): у каждого
#                       <pid> — cmdline (NUL-разделённый), status (строка PPid:)
#                       и stat; проба кладёт подставное дерево;
#   SESSION_PROC_START  с какого pid начинать (по умолчанию $PPID), он сам —
#                       первое звено цепочки;
#   SHIFT_SELF_MARK     файл отметки (по умолчанию
#                       $LOG_DIR/shift-without-self.mark, LOG_DIR — из install.conf);
#   HARNESS_ALERT_TASK  писатель сигналов: хук его НЕ зовёт ни при каком исходе,
#                       проба ставит сюда заглушку-счётчик и требует ноль вызовов.
# Сверка: у ближайшего claude есть «--append-system-prompt-file <файл>», файл
# есть, его первая строка — метка, которую СЕЙЧАС даёт сборщик self-prompt.py
# (рядом с хуком: <каталог хука>/../self-prompt.py) для класса «оркестратор».
# Иначе ПЕРВАЯ строка вывода — «смена поднята без личности: <причина>» и
# отметка: одна строка «<epoch> <причина>», записанная атомарно (временный файл
# рядом и mv — хвостов «shift-without-self.mark*» не остаётся). При порядке —
# ни строки, а прежняя отметка снята. У ближайшего claude с -p/--print — ни
# строки, отметка не тронута.
#
# Почему отметка, а не сигнал: хук SessionStart идёт под claude, а боевой
# scripts/alert-task.py отказывает зовам из-под claude (called_from_session) —
# сигнал из хука в бою не доходит. Сигнал по отметке шлёт сторож сессий на
# обходе (проба scripts/test_shift_self_mark.sh).
#
# Имена переменных латиницей: bash не берёт кириллические идентификаторы.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

HOOK="${HARNESS_SESSION_STATE:-$HERE/hooks/session_state.sh}"
# В пакете хуки лежат в harness/hooks, рядом со scripts.
[ -f "$HOOK" ] || HOOK="$HERE/../hooks/session_state.sh"
[ -f "$HOOK" ] || { echo "ПЛОХО: хука session_state.sh нет по пути раскладки — проверять нечего"; exit 1; }
ASSEMBLER="$HERE/self-prompt.py"

S=$(mktemp -d /tmp/sap-proba-XXXXXX)
trap 'rm -rf "$S"' EXIT
mkdir -p "$S/home" "$S/proj" "$S/log" "$S/hb"
printf 'PROJECT_NAME=proba\nPROJECT_DIR=%s\nLOG_DIR=%s\nHEARTBEAT_DIR=%s\n' \
    "$S/proj" "$S/log" "$S/hb" > "$S/home/install.conf"
cat > "$S/alert.py" <<EOF
import json, sys
with open("$S/signals", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\n")
EOF

# Файлы личности стенда собирает ЖИВОЙ сборщик с тем же окружением, что у хука:
# эталона в стенде нет (код 3 — законная деградация), метка от этого стабильна.
NO_ETALON="$S/no-etalon"
build() { # $1 = класс, $2 = файл
    SELF_ETALON_DIR="$NO_ETALON" python3 "$ASSEMBLER" --класс "$1" --в "$2" >/dev/null 2>&1
    [ -s "$2" ] || echo "  (сборщик не собрал класс $1 — случаи с файлом личности ниже красные)"
}
ORCH="$S/orch.md"; build оркестратор "$ORCH"
TOOL="$S/tool.md"; build прибор "$TOOL"
STALE="$S/stale.md"
{ echo "Метка сборки: оркестратор-000000"; tail -n +2 "$ORCH"; } > "$STALE"
: > "$S/empty.md"

# Подставное дерево процессов: $1 = каталог, $2 = pid, $3 = ppid, $4… = argv.
proc() {
    local root="$1" pid="$2" ppid="$3"; shift 3
    mkdir -p "$root/$pid"
    printf '%s\0' "$@" > "$root/$pid/cmdline"
    printf 'Name:\t%s\nState:\tS (sleeping)\nPid:\t%s\nPPid:\t%s\n' "$(basename -- "$1" | cut -c1-15)" "$pid" "$ppid" \
        > "$root/$pid/status"
    printf '%s (%s) S %s 0 0 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 0\n' "$pid" "$(basename -- "$1" | cut -c1-15)" "$ppid" \
        > "$root/$pid/stat"
}
HOOK_SH=(/bin/sh -c '$CLAUDE_PROJECT_DIR/scripts/hooks/session_state.sh')
SHIFT_SHELL=(-bash)
TMUX_TOP=(tmux new-session -d -s agent)

# Прогон хука на дереве $1 с началом цепочки $2. Печатает вывод хука; зовы
# писателя сигналов — в $S/signals. $MARK_DOOR (если задан) — дверь SHIFT_SELF_MARK.
MARK="$S/log/shift-without-self.mark"
run_hook() {
    : > "$S/signals"
    local door=()
    [ -n "${MARK_DOOR:-}" ] && door=(SHIFT_SELF_MARK="$MARK_DOOR")
    (cd "$S/proj" && printf '{"source":"startup"}' | env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 \
        HARNESS_HOME="$S/home" SELF_ETALON_DIR="$NO_ETALON" \
        SESSION_PROC_ROOT="$1" SESSION_PROC_START="$2" HARNESS_ALERT_TASK="$S/alert.py" \
        "${door[@]}" bash "$HOOK" 2>/dev/null)
}
calls() { grep -c . "$S/signals" 2>/dev/null | tr -d ' '; }
OLD_MARK="1000000000 старая причина"

# Отметка $1 против первой строки вывода $2: одна строка «<epoch> <причина>»,
# epoch свежий (±300 с), причина — та, что в строке «без личности».
mark_verdict() {
    local file="$1" first="$2" lines line epoch reason now
    [ -f "$file" ] || { echo "отметки нет"; return; }
    lines=$(wc -l < "$file" | tr -d ' ')
    [ "$lines" = 1 ] || { echo "строк $lines"; return; }
    line=$(cat "$file")
    epoch=${line%% *}; reason=${line#* }
    [[ "$epoch" =~ ^[0-9]{9,}$ ]] && [ "$reason" != "$line" ] || { echo "не «<epoch> <причина>»: «${line:0:80}»"; return; }
    now=$(date +%s)
    [ $((now - epoch)) -le 300 ] && [ $((epoch - now)) -le 300 ] || { echo "epoch не свежий: $epoch"; return; }
    [ "$reason" = "${first#смена поднята без личности: }" ] || { echo "причина «${reason:0:60}» не та, что в строке"; return; }
    echo да
}
# Хвосты неатомарной записи: всё рядом с отметкой с её именем, кроме неё самой.
mark_tails() { find "$(dirname "$1")" -maxdepth 1 -name "$(basename "$1")?*" 2>/dev/null | wc -l | tr -d ' '; }

# $1 = имя случая, $2 = корень дерева, $3 = начало цепочки,
# $4 = «норма» | «print» | «строка» [$5 = обязательная часть причины]
judge() {
    local out first
    if [ "$4" = строка ]; then
        rm -f "$MARK"
    else
        printf '%s\n' "$OLD_MARK" > "$MARK"
    fi
    out=$(run_hook "$2" "$3")
    first=$(printf '%s\n' "$out" | head -1)
    case_check "$1: писатель сигналов не зван (хук под claude — alert-task откажет)" "0" "$(calls)"
    case "$4" in
    норма|print)
        case_check "$1: строки «без личности» нет" "нет" \
            "$(grep -q 'без личности' <<< "$out" && echo да || echo нет)"
        if [ "$4" = норма ]; then
            case_check "$1: прежняя отметка снята (смена поднята с личностью)" "нет" \
                "$([ -e "$MARK" ] && echo есть || echo нет)"
        else
            case_check "$1: claude -p — отметка не тронута" "$OLD_MARK" "$(cat "$MARK" 2>/dev/null || echo 'отметки нет')"
            rm -f "$MARK"; run_hook "$2" "$3" >/dev/null
            case_check "$1: claude -p — отметка не заведена" "нет" \
                "$([ -e "$MARK" ] && echo есть || echo нет)"
        fi
        ;;
    строка)
        case_check "$1: первая строка — «смена поднята без личности: …»" "да" \
            "$(case "$first" in 'смена поднята без личности: '?*) echo да ;; *) echo "нет («${first:0:80}»)" ;; esac)"
        [ -n "${5:-}" ] && case_check "$1: причина называет «$5»" "да" \
            "$(grep -qF -- "$5" <<< "$first" && echo да || echo нет)"
        case_check "$1: отметка — одна строка «<epoch> <причина>» с причиной из строки" "да" \
            "$(mark_verdict "$MARK" "$first")"
        case_check "$1: запись атомарна — хвостов рядом с отметкой нет" "0" "$(mark_tails "$MARK")"
        ;;
    esac
}

# Т1. Норма: хук → sh -c → claude смены с файлом личности текущей сборки.
# $PPID хука — sh, не claude (замер Н-5): кто верит $PPID, здесь кричит зря.
T="$S/t1"; proc "$T" 300 200 "${HOOK_SH[@]}"
proc "$T" 200 100 claude --append-system-prompt-file "$ORCH" --model claude-opus-5-5 --dangerously-skip-permissions
proc "$T" 100 50 "${SHIFT_SHELL[@]}"; proc "$T" 50 1 "${TMUX_TOP[@]}"
judge "Т1 норма (родитель хука — sh, claude выше)" "$T" 300 норма

# Т2. Голый claude: смена поднята мимо komanda_agenta.
T="$S/t2"; proc "$T" 300 200 "${HOOK_SH[@]}"
proc "$T" 200 100 claude --model claude-opus-5-5 --dangerously-skip-permissions
proc "$T" 100 1 "${SHIFT_SHELL[@]}"
judge "Т2 claude без --append-system-prompt-file" "$T" 300 строка

# Т3. Флаг есть, файла нет.
T="$S/t3"; proc "$T" 300 200 "${HOOK_SH[@]}"
proc "$T" 200 1 claude --append-system-prompt-file "$S/nonexistent.md" --model claude-opus-5-5
judge "Т3 файла личности нет" "$T" 300 строка

# Т4. Файл старой сборки: метка не та, что даёт сборщик сейчас.
T="$S/t4"; proc "$T" 300 200 "${HOOK_SH[@]}"
proc "$T" 200 1 claude --append-system-prompt-file "$STALE" --model claude-opus-5-5
judge "Т4 метка файла не равна текущей сборке" "$T" 300 строка

# Т5. Файл чужого класса (прибор): личность не смены.
T="$S/t5"; proc "$T" 300 200 "${HOOK_SH[@]}"
proc "$T" 200 1 claude --append-system-prompt-file "$TOOL" --model claude-opus-5-5
judge "Т5 файл класса «прибор», не «оркестратор»" "$T" 300 строка

# Т6. Пустой файл: claude с ним стартует с кодом 0 и без личности (спека §3.2).
T="$S/t6"; proc "$T" 300 200 "${HOOK_SH[@]}"
proc "$T" 200 1 claude --append-system-prompt-file "$S/empty.md" --model claude-opus-5-5
judge "Т6 файл личности пуст" "$T" 300 строка

# Т7. В цепочке нет claude вовсе (хук позван руками из оболочки).
T="$S/t7"; proc "$T" 300 200 "${HOOK_SH[@]}"
proc "$T" 200 100 bash; proc "$T" 100 1 "${TMUX_TOP[@]}"
judge "Т7 claude в цепочке нет" "$T" 300 строка "родитель не claude"

# Т8. Приманка: в доводах промежуточной оболочки есть «claude», argv0 — bash.
# Кто ищет подстроку в cmdline, судит приманку (флага нет) и кричит зря.
T="$S/t8"; proc "$T" 300 250 "${HOOK_SH[@]}"
proc "$T" 250 200 bash /var/www/x/scripts/claude-run.sh --класс оркестратор
proc "$T" 200 1 claude --append-system-prompt-file "$ORCH" --model claude-opus-5-5
judge "Т8 приманка argv0=bash с claude-run.sh в доводах, выше — claude в порядке" "$T" 300 норма

# Т9. argv0 — полный путь к claude.
T="$S/t9"; proc "$T" 300 200 "${HOOK_SH[@]}"
proc "$T" 200 1 /home/user/.local/bin/claude --append-system-prompt-file "$ORCH" --model claude-opus-5-5
judge "Т9 argv0 — путь …/claude" "$T" 300 норма

# Т10. Ближайший claude — с -p (демон или прибор из каталога проекта): не смена.
# Его файл — класса «прибор»; выше — голая смена, но судится ближайший.
T="$S/t10"; proc "$T" 300 200 "${HOOK_SH[@]}"
proc "$T" 200 150 claude --append-system-prompt-file "$TOOL" -p "вопрос" --model haiku --strict-mcp-config
proc "$T" 150 120 bash /var/www/x/scripts/claude-run.sh --класс прибор -p "вопрос"
proc "$T" 120 1 claude --model claude-opus-5-5
judge "Т10 ближайший claude с -p — не смена, молчит" "$T" 300 print

# Т11. То же с --print.
T="$S/t11"; proc "$T" 300 200 "${HOOK_SH[@]}"
proc "$T" 200 1 claude --print "вопрос" --model haiku --strict-mcp-config
judge "Т11 ближайший claude с --print — молчит" "$T" 300 print

# Т12. Начало цепочки само — claude смены (хук позван без промежуточной sh).
T="$S/t12"; proc "$T" 200 1 claude --append-system-prompt-file "$ORCH" --model claude-opus-5-5
judge "Т12 начало цепочки — claude в порядке" "$T" 200 норма

# Т13. Прежняя отметка от старой смены + новый сбой: отметка переписана целиком
# (одна строка, свежий epoch, новая причина), а не дописана и не оставлена.
T="$S/t13"; proc "$T" 300 200 "${HOOK_SH[@]}"
proc "$T" 200 1 claude --model claude-opus-5-5
printf '%s\n' "$OLD_MARK" > "$MARK"
out=$(run_hook "$T" 300)
case_check "Т13 старая отметка и новый сбой: писатель сигналов не зван" "0" "$(calls)"
case_check "Т13 старая отметка и новый сбой: отметка переписана (одна строка, свежая, новая причина)" "да" \
    "$(mark_verdict "$MARK" "$(printf '%s\n' "$out" | head -1)")"

# Т14. Дверь SHIFT_SELF_MARK: отметка ложится по двери, не в $LOG_DIR.
T="$S/t14"; proc "$T" 300 200 "${HOOK_SH[@]}"
proc "$T" 200 1 claude --model claude-opus-5-5
rm -f "$MARK"; DOOR_MARK="$S/door/shift.mark"; mkdir -p "$S/door"
out=$(MARK_DOOR="$DOOR_MARK" run_hook "$T" 300)
case_check "Т14 дверь SHIFT_SELF_MARK: отметка по двери" "да" \
    "$(mark_verdict "$DOOR_MARK" "$(printf '%s\n' "$out" | head -1)")"
case_check "Т14 дверь SHIFT_SELF_MARK: в \$LOG_DIR отметки нет" "нет" \
    "$([ -e "$MARK" ] && echo есть || echo нет)"

# Т15. Каждая проба, что зовёт хук, ставит дверь SHIFT_SELF_MARK. Под тестом
# родитель хука не claude, и без двери отметка ложится в $LOG_DIR из install.conf —
# у пробы без своего install.conf это боевой каталог: сторож шлёт владельцу
# ложную «смену без личности» на каждый прогон набора. Список — разбором
# исходников рядом с пробой (путь хука в строке кода, не в комментарии).
# Одним grep, без конвейера: под pipefail ранний выход grep -q рвёт трубу
# писателю, и конвейер по большому файлу отвечает 141 — проба теряла вызывающего.
code_has() { grep -aqE "^[[:space:]]*([^#[:space:]].*)?($2)" "$1"; }
# Питон-проба может читать хук как ТЕКСТ (вычитать строки сообщений) — это не
# вызов, и дверь ей не нужна. Судит разбор AST: упоминание пути хука — вызов,
# если по дороге к своему оператору оно проходит через запуск процесса (имя
# вида run/exec/spawn/popen/system/call или функция этого файла, которая сама,
# прямо или через другую свою, запускает процесс), либо оператор не голое
# выражение (путь сохранён — его могут запустить позже). Голое выражение без
# запуска (докстрока, judge(..., message_lines(путь))) — чтение. Не разобрался
# файл — считаем вызывающим: сомнение судится строже.
py_calls_hook() {
    python3 -I - "$1" <<'PY'
import ast
import re
import sys

HOOK = re.compile(r"hooks/session_state\.sh|^session_state\.sh$")
RUNS = re.compile(r"run|exec|spawn|popen|system|call|check_output|getoutput", re.I)
try:
    tree = ast.parse(open(sys.argv[1], encoding="utf-8", errors="replace").read())
except (SyntaxError, ValueError):
    sys.exit(0)
parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
defs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def name_of(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


runners = set()


def starts_process(call):
    name = name_of(call)
    return name in runners if name in defs else bool(RUNS.search(name))


grown = True
while grown:
    grown = False
    for name, fn in defs.items():
        if name not in runners and any(isinstance(c, ast.Call) and starts_process(c) for c in ast.walk(fn)):
            runners.add(name)
            grown = True

for node in ast.walk(tree):
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str) and HOOK.search(node.value)):
        continue
    cur = node
    while not isinstance(cur, ast.stmt):
        cur = parent[cur]
        if isinstance(cur, ast.Call) and starts_process(cur):
            sys.exit(0)
    if not isinstance(cur, ast.Expr):
        sys.exit(0)
sys.exit(1)
PY
}
calls_hook() {
    case "$1" in
        *.py) py_calls_hook "$1" ;;
        *) code_has "$1" "hooks/session_state\.sh|[\"']session_state\.sh[\"']" ;;
    esac
}
sets_door() { code_has "$1" "SHIFT_SELF_MARK=|[\"']SHIFT_SELF_MARK[\"'][[:space:]]*:"; }
# Судья сам на образцах: читающий хук как текст — не вызывающий; прямой зов,
# зов через свою функцию и путь, сохранённый для позднего зова, — вызывающие.
J="$S/t15-judge"; mkdir -p "$J"
cat > "$J/reader.py" <<'EOF'
"""Строки-сообщения hooks/session_state.sh судятся как текст."""
from pathlib import Path


def message_lines(path):
    return [l for l in path.read_text(encoding="utf-8").splitlines() if "echo" in l]


def judge(name, lines):
    print(name, len(lines))


judge("hooks/session_state.sh", message_lines(Path("pkg") / "hooks" / "session_state.sh"))
EOF
cat > "$J/direct.py" <<'EOF'
import subprocess
subprocess.run(["bash", "scripts/hooks/session_state.sh"])
EOF
cat > "$J/helper.py" <<'EOF'
import subprocess
from pathlib import Path


def shell(path):
    return subprocess.run(["bash", str(path)], capture_output=True)


def run_hook(path):
    return shell(path).returncode


print(run_hook(Path("scripts") / "hooks" / "session_state.sh"))
EOF
cat > "$J/stored.py" <<'EOF'
import os
import subprocess
from pathlib import Path
HOOK = Path(os.environ.get("SESSION_STATE_SH") or Path("scripts") / "hooks" / "session_state.sh")
subprocess.run(["bash", str(HOOK)])
EOF
for sample in reader:нет direct:да helper:да stored:да; do
    case_check "Т15 судья: образец ${sample%%:*}.py зовёт хук" "${sample#*:}" \
        "$(calls_hook "$J/${sample%%:*}.py" && echo да || echo нет)"
done
hook_callers=0; doorless=""
for f in "$HERE"/test_*.sh "$HERE"/test_*.py "$(dirname "$HOOK")"/test_*.sh "$(dirname "$HOOK")"/test_*.py; do
    [ -f "$f" ] && calls_hook "$f" || continue
    hook_callers=$((hook_callers + 1))
    sets_door "$f" || doorless="$doorless $(basename "$f")"
done
case_check "Т15 пробы, зовущие хук, найдены разбором (пусто — судить нечего)" "да" \
    "$([ "$hook_callers" -gt 0 ] && echo да || echo "нет, найдено 0")"
case_check "Т15 каждая из $hook_callers проб, зовущих хук, ставит дверь SHIFT_SELF_MARK" "" "${doorless# }"

echo
if [ "$CASES_FAILED" -eq 0 ]; then
    echo "ПРОБА ЗЕЛЁНАЯ: случаев $CASES_PATHS, падений 0"
    exit 0
fi
echo "ПРОБА КРАСНАЯ: падений $CASES_FAILED из $CASES_PATHS случаев"
exit 1
