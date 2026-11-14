#!/usr/bin/env bash
# Проба «назови метку» по событию (спека личности и политики §6.2, K49–K51).
#
# Модель — только через носители своих путей: демон — claude-demon.sh, прибор —
# ask-the-model.py. Метку сборки каждого из двух классов она обязана назвать
# (сверка без учёта регистра: модель пишет «Метка» с заглавной). Обе названы —
# ключ события пишется в файл состояния (его сверяет сторож сессий) и сигнал
# снимается; иначе ключ НЕ пишется (сторож повторит до потолка) и поднимается
# ровно один сигнал.
#
#   bash scripts/self-prompt-live.sh --ключ <ключ события>
# Код возврата: 0 — обе метки названы; 1 — не названа; 2 — неверный вызов.
# Двери: CLAUDE_BIN (доходит до claude), HARNESS_ALERT_TASK,
# SELF_PROMPT_LIVE_STATE, LOG_DIR.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
ALERT="${HARNESS_ALERT_TASK:-$HERE/alert-task.py}"
if [ -z "${LOG_DIR:-}" ] || [ -z "${PROJECT_DIR:-}" ]; then
    # shellcheck disable=SC1091
    source "$HERE/lib/config.sh"
    konf_zagruzit
fi
STATE="${SELF_PROMPT_LIVE_STATE:-$LOG_DIR/self-prompt-live.state}"
# Ключ сигнала обязан иметь вид <сторож>:<род беды> (alert-task.py), поэтому с двоеточием.
ALERT_KEY="self-prompt-live:failed"

KEY=""
while [ $# -gt 0 ]; do
    case "$1" in
        --ключ) KEY="${2:-}"; shift 2 ;;
        *) echo "неизвестный довод: $1" >&2; exit 2 ;;
    esac
done
[ -n "$KEY" ] || { echo "нужен --ключ <ключ события>" >&2; exit 2; }

WORK=$(mktemp -d /tmp/sap-proba-XXXXXX)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/empty"
MCP_EMPTY='{"mcpServers":{}}'
QUESTION='Назови метку сборки из своего системного промпта: строку «Метка сборки: …» или «отпечаток: …», дословно, одной строкой. Если такой строки в системном промпте нет — ответь одним словом НЕТ.'

# Метка, которую сейчас даёт сборщик, — то, что модель обязана назвать.
label_of() {
    python3 "$HERE/self-prompt.py" --класс "$1" --в "$WORK/expected-$1.md" >/dev/null 2>&1
    head -1 "$WORK/expected-$1.md" 2>/dev/null | sed -n 's/^Метка сборки: //p'
}

ask_demon() {
    (cd "$WORK/empty" && timeout 300 bash "$HERE/claude-demon.sh" проба-личности -p "$QUESTION" \
        --model haiku --mcp-config "$MCP_EMPTY" --strict-mcp-config 2>&1)
}
ask_tool() {
    printf '%s\n' "$QUESTION" > "$WORK/question.txt"
    printf '{"метка": "строка"}\n' > "$WORK/schema.json"
    (cd "$WORK/empty" && timeout 300 python3 "$HERE/ask-the-model.py" --зачем проба-личности \
        --вопрос-файл "$WORK/question.txt" --схема "$WORK/schema.json" --вид дежурный --таймаут 240 2>&1)
}

failed=""
for cls in демон прибор; do
    want=$(label_of "$cls")
    case "$cls" in демон) answer=$(ask_demon) ;; *) answer=$(ask_tool) ;; esac
    if [ -z "$want" ] || ! grep -qiF -- "$want" <<< "$answer"; then
        failed="$failed $cls"
    fi
done

if [ -n "$failed" ]; then
    python3 "$ALERT" --key "$ALERT_KEY" --title "Модель не назвала метку личности" \
        --text "Проба по событию (ключ $KEY): метку не назвал класс:$failed. Смена или демон работают без личности." \
        --watcher self-prompt-live </dev/null >/dev/null 2>&1 || true
    echo "метка не названа:$failed" >&2
    exit 1
fi

printf '%s\n' "$KEY" > "$STATE"
python3 "$ALERT" --resolve "$ALERT_KEY" --watcher self-prompt-live </dev/null >/dev/null 2>&1 || true
echo "метки названы: демон, прибор"
exit 0
