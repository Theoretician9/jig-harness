#!/usr/bin/env bash
# Живая приёмка: модель не могла не узнать свою личность (спека §6.1, K50).
#
# Для КАЖДОГО класса политики запуска (harness/config/launch-policy.yaml, разбором;
# пустой список — провал) модель зовётся через НАСТОЯЩИЙ носитель своего пути и
# называет метку сборки. Метка в ответе (без учёта регистра — модель пишет её
# с заглавной) = метка, которую сейчас даёт сборщик self-prompt.py, — класс зелёный.
#   демон        claude-demon.sh проба-личности -p …
#   прибор       ask-the-model.py --зачем проба-личности (схема {метка: строка})
#   исполнитель, управляющий, разведчик — claude-run.sh --класс прибор -p из
#                каталога стенда с карточками помощников; внутри — Agent нужного
#                типа, ответ помощника дословно (карточка = его системный промпт)
#   оркестратор  komanda_agenta в своём tmux-сервере «tmux -L sap-proba-$$»
#                (отступление 3 плана: дверь сторожа не нужна), снятие —
#                «tmux -L sap-proba-$$ kill-server»
# Контроль: голый claude без носителя отвечает «НЕТ» и ни одной метки не называет.
# Затем проба по событию scripts/self-prompt-live.sh (спека §6.2): «глухая» модель —
# ключ не записан, ровно один сигнал --key self-prompt-live; зелёный прогон —
# ключ в файле состояния и снятие сигнала (--resolve self-prompt-live).
#
#   bash scripts/test_self_prompt_live.sh               живьём (haiku, ~11 вызовов)
#   bash scripts/test_self_prompt_live.sh --подставной  подставной claude стенда:
#        отвечает первой строкой файла личности, а за помощника — отпечатком его
#        карточки; модель не тратится, проверяется проводка путей
# Двери: CLAUDE_BIN (носители зовут его вместо claude), HARNESS_ALERT_TASK и
# TG_SEND — заглушки стенда: сигналы и вести владельцу из пробы не уходят.
# Живые вызовы — только haiku, только с --strict-mcp-config и пустым набором MCP.
#
# Имена переменных латиницей: bash не берёт кириллические идентификаторы.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib/selftest_cases.sh"

POLICY="$ROOT/harness/config/launch-policy.yaml"
[ -f "$POLICY" ] || POLICY="$ROOT/config/launch-policy.yaml"      # раскладка пакета
CARDS="$ROOT/.claude/agents"
[ -d "$CARDS" ] || CARDS="$ROOT/agents"
MCP_EMPTY='{"mcpServers":{}}'
SOCK="sap-proba-$$"

S=$(mktemp -d /tmp/sap-proba-XXXXXX)
# Сервер выходит сам, когда команда сессии кончилась, и оставляет файл сокета:
# его убирает проба — каталог tmux-* уборщик /tmp не судит.
cleanup() {
    tmux -L "$SOCK" kill-server >/dev/null 2>&1
    rm -f "${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)/$SOCK"
    rm -rf "$S"
}
trap cleanup EXIT
mkdir -p "$S/log/locks" "$S/empty" "$S/agents-dir/.claude/agents" "$S/out"
# Копия дерева мутаций несёт только scripts/ и harness/ — карточек рядом нет, и проба
# была красна до мутации. Тогда карточки собирает сборщик той же копии (код 3 — без
# эталона, карточки всё равно легли).
if [ -d "$CARDS" ]; then
    cp "$CARDS"/*.md "$S/agents-dir/.claude/agents/" 2>/dev/null
else
    python3 "$HERE/self-prompt.py" --карточки --в "$S/agents-dir/.claude/agents" >/dev/null 2>&1
fi
cat > "$S/alert.py" <<EOF
import json, sys
with open("$S/signals", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\n")
EOF
printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >> %q\nexit 0\n' "$S/tg.log" > "$S/tg_send.sh"
chmod +x "$S/tg_send.sh"
: > "$S/signals"

# Подставной claude в каталоге $1, режим $2: good — честно читает свой системный
# промпт; deaf — всегда «НЕТ» (модель без личности). Форматы вывода — как у CLI.
make_fake() {
    mkdir -p "$1"; echo "$2" > "$1/mode"
    cat > "$1/claude" <<EOF
#!/usr/bin/env bash
# Подставной claude: «модель», которая честно читает свой системный промпт.
[ "\${1:-}" = --version ] && { echo "2.1.299 (Claude Code)"; exit 0; }
file=""; prompt=""; fmt=text; prev=""
for a in "\$@"; do
    [ "\$prev" = --append-system-prompt-file ] && file="\$a"
    [ "\$prev" = --output-format ] && fmt="\$a"
    case "\$a" in *метк*|*subagent_type*) prompt="\$a" ;; esac
    prev="\$a"
done
answer="НЕТ"
if [ "\$(cat '$1/mode')" = good ]; then
    type=\$(printf '%s' "\$prompt" | grep -oE 'subagent_type «[^»]+»' | sed 's/.*«//; s/»//')
    if [ -n "\$type" ]; then
        answer=\$(grep -m1 '^отпечаток: ' ".claude/agents/\$type.md" 2>/dev/null || echo НЕТ)
    elif [ -n "\$file" ] && [ -s "\$file" ]; then
        answer=\$(head -1 "\$file")
    fi
fi
case "\$fmt" in
    json) python3 -c 'import json,sys; print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":json.dumps({"метка":sys.argv[1]},ensure_ascii=False),"total_cost_usd":0,"num_turns":1}, ensure_ascii=False))' "\$answer" ;;
    stream-json) python3 -c 'import json,sys; print(json.dumps({"type":"system","subtype":"init","session_id":"proba"})); print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":sys.argv[1],"total_cost_usd":0,"num_turns":1,"duration_ms":1,"session_id":"proba","usage":{"input_tokens":1,"output_tokens":1}}, ensure_ascii=False))' "\$answer" ;;
    *) printf '%s\n' "\$answer" ;;
esac
EOF
    chmod +x "$1/claude"
}
make_fake "$S/fake-deaf" deaf
if [ "${1:-}" = "--подставной" ]; then
    make_fake "$S/fake" good
    export CLAUDE_BIN="$S/fake/claude"
    echo "режим: подставной claude стенда (модель не тратится)"
else
    echo "режим: живой claude, haiku"
fi
REAL_CLAUDE="${CLAUDE_BIN:-claude}"
# Носители и обёртки читают это окружение; LOG_DIR стенда — их журналы, счёт
# вызовов ask-the-model и файлы личности не смешиваются с живыми.
export PROJECT_DIR="$ROOT" LOG_DIR="$S/log" HARNESS_ALERT_TASK="$S/alert.py" TG_SEND="$S/tg_send.sh"

QUESTION='Назови метку сборки из своего системного промпта: строку «Метка сборки: …» или «отпечаток: …», дословно, одной строкой. Если такой строки в системном промпте нет — ответь одним словом НЕТ.'

# Классы — разбором политики, не списком в пробе: новый класс без способа
# позвать краснит пробу, а не проходит мимо.
mapfile -t CLASSES < <(python3 -c 'import sys, yaml
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
print("\n".join(d.get("классы") or {}))' "$POLICY" 2>/dev/null)
case_check "П0: классов в политике запуска не ноль ($POLICY)" "да" "$([ "${#CLASSES[@]}" -gt 0 ] && echo да || echo нет)"

# Тип помощника для класса — из раздела «карточки» той же политики.
card_of() {
    python3 -c 'import sys, yaml
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
print(next((n for n, c in (d.get("карточки") or {}).items() if c.get("класс") == sys.argv[2]), ""))' "$POLICY" "$1"
}

# Метка, которую сейчас даёт сборщик: то, что модель обязана назвать.
label_of() {
    python3 "$HERE/self-prompt.py" --класс "$1" --в "$S/expected-$1.md" >/dev/null 2>&1
    head -1 "$S/expected-$1.md" 2>/dev/null | sed -n 's/^Метка сборки: //p'
}

ask_demon() {
    (cd "$S/empty" && timeout 300 bash "$HERE/claude-demon.sh" проба-личности -p "$QUESTION" \
        --model haiku --mcp-config "$MCP_EMPTY" --strict-mcp-config 2>&1)
}
ask_tool() {
    printf '%s\n' "$QUESTION" > "$S/question.txt"
    printf '{"метка": "строка"}\n' > "$S/schema.json"
    (cd "$S/empty" && timeout 300 python3 "$HERE/ask-the-model.py" --зачем проба-личности \
        --вопрос-файл "$S/question.txt" --схема "$S/schema.json" --вид дежурный --таймаут 240 2>&1)
}
ask_helper() { # $1 = тип помощника
    (cd "$S/agents-dir" && timeout 300 bash "$HERE/claude-run.sh" --класс прибор \
        -p "Вызови инструмент Agent: subagent_type «$1», model «haiku», prompt: «$QUESTION» Ответь ТОЛЬКО дословным ответом помощника, без своих слов." \
        --model haiku --allowedTools Agent --mcp-config "$MCP_EMPTY" --strict-mcp-config 2>&1)
}
ask_orchestrator() {
    local cmd i=0
    # shellcheck disable=SC1091
    cmd=$(source "$HERE/lib/config.sh" && komanda_agenta \
        "claude --model haiku -p '$QUESTION' --mcp-config '$MCP_EMPTY' --strict-mcp-config")
    printf '%s\n' "$cmd" > "$S/out/orchestrator.cmd"
    cat > "$S/orchestrator.sh" <<EOF
cd '$S/empty'
export PROJECT_DIR='$ROOT' LOG_DIR='$S/log' HARNESS_ALERT_TASK='$S/alert.py' TG_SEND='$S/tg_send.sh'
${CLAUDE_BIN:+export CLAUDE_BIN='$CLAUDE_BIN'}
$cmd > '$S/out/orchestrator.out' 2>&1 < /dev/null
echo \$? > '$S/out/orchestrator.done'
EOF
    tmux -L "$SOCK" new-session -d -s proba "bash '$S/orchestrator.sh'" || { echo "tmux -L $SOCK не поднялся"; return 0; }
    while [ ! -f "$S/out/orchestrator.done" ] && [ "$i" -lt 300 ]; do sleep 1; i=$((i + 1)); done
    tmux -L "$SOCK" kill-server >/dev/null 2>&1
    cat "$S/out/orchestrator.out" 2>/dev/null
}

named=0; total=0; ALL_LABELS=()
for cls in "${CLASSES[@]}"; do
    total=$((total + 1))
    want=$(label_of "$cls")
    [ -n "$want" ] && ALL_LABELS+=("$want")
    case "$cls" in
        демон) answer=$(ask_demon) ;;
        прибор) answer=$(ask_tool) ;;
        оркестратор) answer=$(ask_orchestrator) ;;
        *)  card=$(card_of "$cls")
            if [ -n "$card" ]; then answer=$(ask_helper "$card")
            else answer="способа позвать класс «$cls» в пробе нет (ни путь, ни карточка помощника)"; fi ;;
    esac
    printf '%s\n' "$answer" > "$S/out/$cls.answer"
    got=$([ -n "$want" ] && grep -qiF -- "$want" <<< "$answer" && echo названа || echo "нет («$(tr '\n' ' ' <<< "$answer" | cut -c1-160)»)")
    case_check "К-$cls: модель назвала метку «${want:-сборщик не собрал}»" "названа" "$got"
    [ "$got" = названа ] && named=$((named + 1))
done
echo "назвали метку: $named/$total"

echo "── контроль: голый claude без носителя ──"
answer=$(cd "$S/empty" && timeout 300 "$REAL_CLAUDE" -p "$QUESTION" --model haiku \
    --mcp-config "$MCP_EMPTY" --strict-mcp-config 2>&1)
case_check "Б1: без носителя ответ «НЕТ»" "да" "$(grep -qi 'НЕТ' <<< "$answer" && echo да || echo "нет («${answer:0:120}»)")"
leaked=""
for l in "${ALL_LABELS[@]}"; do grep -qiF -- "$l" <<< "$answer" && leaked="$leaked $l"; done
case_check "Б2: без носителя ни одной метки" "" "$leaked"

echo "── проба по событию: scripts/self-prompt-live.sh ──"
LIVE="$HERE/self-prompt-live.sh"
STATE="$S/log/self-prompt-live.state"
case_check "Е0: scripts/self-prompt-live.sh на месте" "да" "$([ -f "$LIVE" ] && echo да || echo нет)"
# Глухая модель — всегда подставная: красный путь проверяется без трат.
deaf="$S/fake-deaf"
echo "старый-ключ" > "$STATE"; : > "$S/signals"
(cd "$S/empty" && CLAUDE_BIN="$deaf/claude" SELF_PROMPT_LIVE_STATE="$STATE" timeout 600 \
    bash "$LIVE" --ключ proba-key-deaf > "$S/out/live-deaf.out" 2>&1)
rc=$?
case_check "Е1: модель метку не назвала — код не 0" "не 0" "$([ "$rc" -ne 0 ] && echo 'не 0' || echo 0)"
case_check "Е2: ключ НЕ записан (состояние прежнее)" "старый-ключ" "$(cat "$STATE" 2>/dev/null)"
case_check "Е3: ровно один сигнал --key self-prompt-live" "1" \
    "$(grep -v -- '"--resolve"' "$S/signals" | grep -c -- '"self-prompt-live"' | tr -d ' ')"
: > "$S/signals"
(cd "$S/empty" && SELF_PROMPT_LIVE_STATE="$STATE" timeout 600 \
    bash "$LIVE" --ключ proba-key-green > "$S/out/live-green.out" 2>&1)
rc=$?
case_check "Е4: метки названы — код 0" "0" "$rc"
case_check "Е5: ключ записан в состояние" "proba-key-green" "$(cat "$STATE" 2>/dev/null)"
case_check "Е6: тревога снята зелёным (--resolve self-prompt-live)" "да" \
    "$(grep -- '"--resolve"' "$S/signals" | grep -q -- '"self-prompt-live"' && echo да || echo нет)"
[ "$rc" -eq 0 ] || sed 's/^/      /' "$S/out/live-green.out" | tail -5

echo
echo "сигналов стенда: $(wc -l < "$S/signals" | tr -d ' '), вестей владельцу (заглушка): $(cat "$S/tg.log" 2>/dev/null | wc -l | tr -d ' ')"
if [ "$CASES_FAILED" -eq 0 ]; then
    echo "ПРОБА ЗЕЛЁНАЯ: случаев $CASES_PATHS, падений 0, $named/$total"
    exit 0
fi
echo "ПРОБА КРАСНАЯ: падений $CASES_FAILED из $CASES_PATHS случаев, назвали $named/$total"
exit 1
