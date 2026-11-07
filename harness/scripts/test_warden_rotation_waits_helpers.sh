#!/usr/bin/env bash
# Проба: ротация ждёт идущих помощников (карточка hr.drain-queue-before-rotation,
# спека docs/specs/2026-11-02-ротация-ждёт-помощников.md, итерация 2).
#
# Слово владельца 02.11.2026: «Перезапуск при работающих помощниках нельзя
# делать, это не просьба, это должно быть системной частью харнеса… Если смена
# подходит к ротации, надо дождаться завершение всех текущих задач у всех
# помощников, кодом запрещать начинать выполнять любые новые задачи, только
# класть в очередь».
#
# Помощник живёт внутри процесса claude смены: «/exit» и «/clear» сторожа
# обрывают его на середине — работа и токены в мусор.
#
# Судится (номера — разделы спеки):
#   1. rotation_helpers_verdict — слово И код, зависшие (3.1);
#   2. rotation_waited_sec — пауза в ожидание не засчитывается (3.2, R-9);
#   3. judge_work — сбой прибора при непустом subagents/ даёт «?» (3.3, R-8);
#   4. выход_из_ротации и клавиши_подтверждения держат выход, пока помощники
#      идут; agent_rotation_prompt не подаёт «/exit»/«/clear» мимо ворот (R-3);
#   5. порядок в разделе «5. Решение о ротации» — по якорю (R-15);
#   6. снятие флага и выпуск очереди после доставки (R-4, R-13);
#   7. стенд: сторож целым прогоном (R-15, R-4, R-5, R-7, R-14, R-18; ревью:
#      С7 headless рядом, С9 флаг сам по себе, С10 пауза, W9 «идёт» в журнале).
# Функции берутся из ЖИВОГО файла сторожа (дверь мутаций —
# HARNESS_SESSION_WARDEN), иначе испорченная копия до пробы не доходит.
#
# Имена переменных ЛАТИНИЦЕЙ: bash не берёт кириллические идентификаторы.
set -uo pipefail
export LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

WARDEN="${HARNESS_SESSION_WARDEN:-$HERE/../harness/demons/session-warden.sh}"
# В пакете демоны лежат рядом со scripts (harness/demons), в проекте — под harness/.
[ -f "$WARDEN" ] || WARDEN="$HERE/../demons/session-warden.sh"
[ -f "$WARDEN" ] || { echo "ПЛОХО: сторожа нет по пути раскладки — проверять нечего"; exit 1; }
PREFIX="warden-rothelp-"
stale=0
while IFS= read -r s; do
    [ -n "$s" ] || continue
    tmux kill-session -t "$s" 2>/dev/null && stale=$((stale + 1))
done <<<"$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep "^$PREFIX" || true)"
[ "$stale" -gt 0 ] && echo "убрано своих хвостовых tmux-сессий: $stale"
STAND=$(mktemp -d)
PANE="$PREFIX$$"
trap 'tmux kill-session -t "$PANE" 2>/dev/null; rm -rf "$STAND"' EXIT

# Тело функции из живого файла; пусто — функции нет.
body() { sed -n "/^$1() {/,/^}/p" "$WARDEN"; }
yes_no() { if "$@"; then echo да; else echo нет; fi; }

# ── 1. чистая функция ───────────────────────────────────────────────────────
FUNC="$STAND/verdict.sh"
body rotation_helpers_verdict > "$FUNC"
case_check "функция rotation_helpers_verdict вынута из файла сторожа" \
    "да" "$([ -s "$FUNC" ] && echo да || echo нет)"

verdict_text() {  # $1=идут $2=зависших $3=ждали мин $4=предел мин $5=действие по сроку
    bash -c 'set -uo pipefail; source "$1"; shift; rotation_helpers_verdict "$@"' \
        _ "$FUNC" "$@" 2>&1
}
# Слово и код одной строкой «слово;код»: сторож смотрит на КОД, журнал — на
# СЛОВО, мутация, разведшая их, обязана краснить.
verdict() {
    local out rc=0
    out=$(verdict_text "$@") || rc=$?
    printf '%s;%s\n' "${out%%:*}" "$rc"
}

echo "── БОЛЬНОЙ СЛУЧАЙ 02.11.2026: окно подошло к порогу, помощники идут ──"
case_check "идут 2, ждём 0 мин из 60 — ротация ждёт" \
    "ждать;1" "$(verdict 2 0 0 60 сообщить)"
case_check "идёт 1, ждём 59 мин из 60 — всё ещё ждёт" \
    "ждать;1" "$(verdict 1 0 59 60 сообщить)"
case_check "отказ называет число идущих помощников" \
    "да" "$(grep -q "3" <<< "$(verdict_text 3 0 5 60 сообщить)" && echo да || echo нет)"
# Судья не смог счесть помощников — незнание не разрешение: ждём.
case_check "помощники неизвестны («?») — ждём, а не рубим" \
    "ждать;1" "$(verdict '?' 0 0 60 сообщить)"
case_check "идут 2 и ещё 1 завис, срок не вышел — ждём идущих" \
    "ждать;1" "$(verdict 2 1 10 60 ротировать)"

echo "── ЗДОРОВАЯ ПОЛОВИНА: помощников нет — ротация как раньше ──"
case_check "помощников 0 — можно" "можно;0" "$(verdict 0 0 0 60 сообщить)"
case_check "помощников 0 после долгого ожидания — можно, без вести владельцу" \
    "можно;0" "$(verdict 0 0 500 60 сообщить)"

echo "── СРОК ОЖИДАНИЯ ВЫШЕЛ: действие по ключу ROTATE_HELPERS_TIMEOUT_ACTION ──"
case_check "60 из 60, «сообщить» — весть владельцу, ротация ждёт дальше" \
    "сообщить;1" "$(verdict 2 0 60 60 сообщить)"
case_check "90 из 60, «ротировать» — ротация идёт" \
    "ротировать;0" "$(verdict 2 0 90 60 ротировать)"
case_check "неизвестные помощники и срок вышел, «сообщить» — весть, ждём" \
    "сообщить;1" "$(verdict '?' 0 61 60 сообщить)"
case_check "действие не задано — как «сообщить», а не ротация" \
    "сообщить;1" "$(verdict 2 0 61 60 '')"
case_check "незнакомое действие — как «сообщить»" \
    "сообщить;1" "$(verdict 2 0 61 60 рубить)"

echo "── ЗАВИСШИЕ (молчат дольше HELPER_HUNG_MIN): сразу по ключу, срока не ждут ──"
case_check "идущих нет, 1 завис, «сообщить» — весть, ждём" \
    "сообщить;1" "$(verdict 0 1 0 60 сообщить)"
case_check "идущих нет, 1 завис, «ротировать» — ротация идёт" \
    "ротировать;0" "$(verdict 0 1 0 60 ротировать)"
case_check "весть о зависшем называет «завис»" \
    "да" "$(grep -q "завис" <<< "$(verdict_text 0 1 0 60 сообщить)" && echo да || echo нет)"

# ── 2. ожидание без паузы (R-9) ─────────────────────────────────────────────
WAITED="$STAND/waited.sh"
body rotation_waited_sec > "$WAITED"
case_check "функция rotation_waited_sec вынута из файла сторожа" \
    "да" "$([ -s "$WAITED" ] && echo да || echo нет)"
waited() {  # $1=ждали_сек $2=проверено $3=сейчас $4=пауза → новое ждали_сек
    bash -c 'source "$1"; shift; rotation_waited_sec "$@"' _ "$WAITED" "$@" 2>&1
}
NOW=$(date +%s)
case_check "прогон на паузе к ожиданию ничего не прибавляет" "0" "$(waited 0 $((NOW - 600)) "$NOW" 1)"
case_check "прогон вне паузы прибавляет прошедшее (300 + 600)" "900" "$(waited 300 $((NOW - 600)) "$NOW" 0)"
# Флаг стоит 3 часа, из них 2,5 — пауза владельца: 18 прогонов по 10 мин,
# 15 из них на паузе. Ожидание — 30 мин, а не 180: «ждать», без ложной вести.
acc=0; at=$((NOW - 10800))
for i in $(seq 1 18); do
    paused=0; [ "$i" -le 15 ] && paused=1
    acc=$(waited "$acc" "$at" $((at + 600)) "$paused"); at=$((at + 600))
    [[ "$acc" =~ ^[0-9]+$ ]] || break
done
case_check "флаг 3 ч, из них 2,5 ч пауза — ждали 1800 с" "1800" "$acc"
[[ "$acc" =~ ^[0-9]+$ ]] && acc_min=$((acc / 60)) || acc_min="нет числа"
case_check "…и решение — «ждать», а не весть по сроку" \
    "ждать;1" "$(verdict 1 0 "$acc_min" 60 сообщить)"

# ── 3. судья не знает — значит «?» (R-8) ────────────────────────────────────
JUDGE="$STAND/judge.sh"
{ body judge_work; body judge_field; } > "$JUDGE"
mkdir -p "$STAND/j/scripts" "$STAND/j/proj/s/subagents" "$STAND/j/empty/s"
printf '{"type":"assistant","timestamp":"2026-11-02T06:00:00Z"}\n' > "$STAND/j/proj/s.jsonl"
cp "$STAND/j/proj/s.jsonl" "$STAND/j/empty/s.jsonl"
: > "$STAND/j/proj/s/subagents/agent-a1511aa2c0ffee001.jsonl"
judge_helpers() {  # $1 = код подставного прибора, $2 = транскрипт → JUDGE_HELPERS
    printf '#!/usr/bin/env python3\nimport sys\nsys.exit(%s)\n' "$1" > "$STAND/j/scripts/stall-in-log.py"
    bash -c 'say() { :; }; немота_агента_минут() { echo 3; }
        source "$1"; HARNESS_SCRIPTS="$2"; TRANSCRIPT="$3"; judge_work; printf "%s\n" "$JUDGE_HELPERS"' \
        _ "$JUDGE" "$STAND/j/scripts" "$2" 2>&1 | tail -1
}
case_check "judge_work вынут из файла сторожа" "да" "$([ -s "$JUDGE" ] && echo да || echo нет)"
case_check "прибор упал (код 1), subagents/ не пуст — помощники «?», а не 0" \
    "?" "$(judge_helpers 1 "$STAND/j/proj/s.jsonl")"
case_check "прибор отказал кодом 3, subagents/ не пуст — «?»" \
    "?" "$(judge_helpers 3 "$STAND/j/proj/s.jsonl")"
case_check "прибор упал, subagents/ нет — помощников 0, как раньше" \
    "0" "$(judge_helpers 1 "$STAND/j/empty/s.jsonl")"

# ── 4. выход держится, пока помощники идут (R-3) ────────────────────────────
EXITF="$STAND/exit.sh"
body выход_из_ротации > "$EXITF"
exit_act() {  # доводы выход_из_ротации → ответ
    bash -c 'source "$1"; shift; ROTATE_IDLE_SEC=120 выход_из_ротации "$@"' _ "$EXITF" "$@" 2>&1
}
case_check "ход закончен, панель молчит 150 с, помощники держат — «ждём», не «/exit»" \
    "ждём" "$(exit_act ждём 150 0 300 900 1 0 1)"
case_check "срок регламента вышел, помощники держат — «ждём», не «принудительно»" \
    "ждём" "$(exit_act ждём 10 0 900 900 0 0 1)"
case_check "помощники не держат — «выход», как раньше" \
    "выход" "$(exit_act ждём 150 0 300 900 1 0 0)"
case_check "помощники не держат, срок вышел — «принудительно», как раньше" \
    "принудительно" "$(exit_act ждём 10 0 900 900 0 0 0)"

KEYSF="$STAND/keys.sh"
body клавиши_подтверждения > "$KEYSF"
keys() { bash -c 'source "$1"; shift; клавиши_подтверждения "$@"' _ "$KEYSF" "$@" 2>&1; }
case_check "диалог выхода, попытка 3, помощники держат — только Escape, не «Exit and stop tasks»" \
    "Escape" "$(keys 3 1)"
case_check "диалог выхода, попытка 1, помощники держат — Escape" "Escape" "$(keys 1 1)"
case_check "диалог выхода, попытка 3, помощники не держат — пункт выхода, как раньше" \
    "ПУНКТ Enter" "$(keys 3 0)"

PROMPTF="$STAND/prompt.sh"
body agent_rotation_prompt > "$PROMPTF"
# Подставные соседи: панель — claude, подача записывается, ворота — по доводу.
sent_via_prompt() {  # $1 = код ворот, $2 = текст → что ушло в панель («» — ничего)
    : > "$STAND/sent.txt"
    bash -c 'OUT="$4"; GATE_RC="$3"
        say() { :; }; jlog() { :; }; pane_cmd() { echo claude; }
        pane_command() { printf "%s\n" "$2" >> "$OUT"; }
        rotation_helpers_gate() { return "$GATE_RC"; }
        source "$1"; agent_rotation_prompt "$2"' _ "$PROMPTF" "$2" "$1" "$STAND/sent.txt" >/dev/null 2>&1
    tr -d '\n' < "$STAND/sent.txt"
}
case_check "ворота закрыты — «/exit» в панель не подаётся" "" "$(sent_via_prompt 1 /exit)"
case_check "ворота закрыты — «/clear» в панель не подаётся" "" "$(sent_via_prompt 1 /clear)"
case_check "ворота открыты — «/exit» уходит, как раньше" "/exit" "$(sent_via_prompt 0 /exit)"
case_check "«/clear» не подаётся мимо agent_rotation_prompt (pane_command text \"/clear\" в файле нет)" \
    "0" "$(grep -c 'pane_command text "/clear"' "$WARDEN")"
case_check "rotate_clear подаёт «/clear» через agent_rotation_prompt" \
    "да" "$(yes_no grep -q 'agent_rotation_prompt "/clear"' <<< "$(body rotate_clear)")"

# ── 5. зов вставлен в путь ротации, а не только написан (R-15) ──────────────
# Строки ищутся ПОСЛЕ якоря раздела 5: самотест в начале файла зовёт те же
# функции, и первая строка файла была бы строкой самотеста.
ANCHOR=$(grep -n '^# ── 5\. Решение о ротации' "$WARDEN" | head -1 | cut -d: -f1)
case_check "якорь раздела «5. Решение о ротации» найден" "да" "$([ -n "$ANCHOR" ] && echo да || echo нет)"
after_anchor() {  # $1 = образец → номер первой строки-зова после якоря
    awk -v from="${ANCHOR:-0}" -v pat="$1" 'NR > from && index($0, pat) && $0 !~ /\(\) \{/ { print NR; exit }' "$WARDEN"
}
CALL=$(after_anchor 'rotation_helpers_gate')
CLEAR=$(after_anchor 'rotate_clear || ROTATE_RC')
FULL=$(after_anchor 'rotate_full || ROTATE_RC')
SEMI=$(after_anchor 'rotate_semi || WARDEN_RC')
METHOD=$(after_anchor 'способ_ротации')
JUDGE_CALL=$(grep -n '^judge_work$' "$WARDEN" | head -1 | cut -d: -f1)
order() {  # $1 < $2 → да
    if [ -n "$1" ] && [ -n "$2" ] && [ "$1" -lt "$2" ]; then echo да
    else echo "нет (${1:-нет} против ${2:-нет})"; fi
}
case_check "ворота помощников спрошены ДО быстрого пути (rotate_clear)" "да" "$(order "$CALL" "$CLEAR")"
case_check "ворота помощников спрошены ДО тяжёлого пути (rotate_full)" "да" "$(order "$CALL" "$FULL")"
case_check "ворота помощников спрошены ДО вопроса владельцу (rotate_semi)" "да" "$(order "$CALL" "$SEMI")"
case_check "ворота помощников спрошены ДО выбора способа ротации" "да" "$(order "$CALL" "$METHOD")"
case_check "помощники сочтены (judge_work) ДО ворот" "да" "$(order "$JUDGE_CALL" "$CALL")"
GATE=$(body rotation_helpers_gate)
case_check "ворота зовут rotation_helpers_verdict" \
    "да" "$(yes_no grep -q 'rotation_helpers_verdict ' <<< "$GATE")"

# ── 6. снятие флага и выпуск очереди (R-4, R-13) ────────────────────────────
KICK=$(body подхватить_работу)
case_check "тело подхватить_работу вынуто" "да" "$([ -n "$KICK" ] && echo да || echo нет)"
case_check "функция rotation_flag_clear есть" "да" "$([ -n "$(body rotation_flag_clear)" ] && echo да || echo нет)"
case_check "подхватить_работу снимает флаг (full, --kick, дозор, подъём выпавшего)" \
    "да" "$(yes_no grep -q 'rotation_flag_clear' <<< "$KICK")"
case_check "успешный rotate_clear снимает флаг" \
    "да" "$(yes_no grep -q 'rotation_flag_clear' <<< "$(body rotate_clear)")"
case_check "пинок преемнику берёт очередь отложенных запусков (--release)" \
    "да" "$(yes_no grep -q 'rotation_queue_guard.py.*--release' <<< "$KICK")"
# Помечать — только после доставки: --mark стоит ПОСЛЕ подачи пинка в панель.
mark_after_send() {
    awk '/pane_command text "Ты — новая сессия/ { sent = NR } /rotation_queue_guard.py.*--mark/ { mark = NR }
         END { print (sent && mark > sent) ? "да" : "нет" }' <<< "$KICK"
}
case_check "очередь помечается выпущенной (--mark) ПОСЛЕ подачи пинка" "да" "$(mark_after_send)"

# ── 7. стенд: сторож целым прогоном ─────────────────────────────────────────
# Свой LOG_DIR, свой panel-state, своя tmux-панель с подставным «агентом»
# (копия cat под именем claude пишет присланное в файл), свой канал-файл.
# Условие «нужна ротация» — pre_compact этой сессии при MAX_COMPACTS=1.
# Дверь ротации стенда отказывает всегда (судья передачи: «не написана»):
# её зов — признак «ротация пошла», и он быстрый.
echo "── СТЕНД: сторож целым прогоном ──"
T="$STAND/a"
mkdir -p "$T/project/scripts/lib" "$T/projects" "$T/bin"
cp /bin/cat "$T/claude"
for f in work-queue.py limit_mark.py limit-in-log.py stall-in-log.py pause.py; do
    cp "$HERE/$f" "$T/project/scripts/" 2>/dev/null
done
cp "$HERE"/lib/*.py "$HERE/lib/config.sh" "$T/project/scripts/lib/"
cat > "$T/project/scripts/tg_send.sh" <<KANAL
#!/usr/bin/env bash
for A in "\$@"; do
    case "\$A" in --*) continue ;; esac
    printf '%s\n' "\$A" >> "$T/kanal.txt"
    break
done
KANAL
# Дверь ротации зовёт судью передачи уже ПОСЛЕ записи флага «идёт»: судья стенда
# снимает копию флага на тот миг (событие в журнале пишется отдельным вызовом и
# запись флага не доказывает).
cat > "$T/project/scripts/handover-check.sh" <<SUDYA
#!/usr/bin/env bash
cp "$T/state/panel-state/rotation-waits-helpers.json" "$T/log/flag-at-door.json" 2>/dev/null
echo "передача стенда: не написана"
exit 1
SUDYA
# Подставной setsid: «отвязка» --rotate-now записывается, а не исполняется —
# проба не оставляет живого сторожа за собой.
printf '#!/usr/bin/env bash\necho отвязан >> "%s"\n' "$T/detached.txt" > "$T/bin/setsid"
chmod +x "$T/project/scripts/tg_send.sh" "$T/project/scripts/handover-check.sh" "$T/bin/setsid"
TR_DIR="$T/projects/${T//\//-}-project"
SESSION="proba"
HELPER="a1511aa2c0ffee001"
PANEL_STATE="$T/state/panel-state"
FLAG="$PANEL_STATE/rotation-waits-helpers.json"

SEED=""
for c in "$HERE/../config/harness.conf" "$HERE/../harness/config/harness.conf" "${HARNESS_CONF:-/etc/harness/harness.conf}"; do
    [ -f "$c" ] && { SEED="$c"; break; }
done
[ -n "$SEED" ] || { echo "ПЛОХО: образца harness.conf нет — проверять нечем"; exit 1; }
cp "$SEED" "$T/harness.conf"
cat >> "$T/harness.conf" <<CONF
AGENT_USER="$(id -un)"
LOG_DIR="$T/log"
PROJECT_DIR="$T/project"
STATE_DIR="$T/state"
HEARTBEAT_DIR="$T/state/heartbeat"
PANEL_STATE_DIR="$PANEL_STATE"
TMUX_SESSION="$PANE"
AGENT_START_CMD="$T/claude"
SERVICE_LOCKS_SUBDIR="проверка"
ACTIVE_LINE="hr"
CTX_WINDOW_TOKENS=400000  # не-секрет: размер окна модели в токенах, а не ключ
MAX_COMPACTS=1
ROTATE_NEEDS_OWNER=0
ROTATE_WITHOUT_HANDOVER=0
ROTATE_REFUSED_TELL_GAP_MIN=180
ROTATE_HELPERS_WAIT_MAX_MIN=60
ROTATE_HELPERS_TIMEOUT_ACTION=сообщить
ROTATE_FLAG_STALE_MIN=30
HELPER_FRESH_MIN=20
HELPER_HUNG_MIN=120
SILENCE_MAX_MIN=30
PULSE_MAX_MIN=30
PULSE_OWNER_MIN=60
LIMIT_STALL_MIN=20
TG_CHAT_ID=""
TG_TOKEN_FILE="/nonexistent"
CONF

# Транскрипт смены и её помощник: $1 = live (идёт) | done (уведомил) | none.
scene() {
    rm -rf "$TR_DIR"; mkdir -p "$TR_DIR"
    python3 - "$TR_DIR" "$SESSION" "$HELPER" "$1" <<'PY'
import datetime, json, os, sys, time
folder, sid, helper, kind = sys.argv[1:5]
now = time.time()
def ts(ago):
    return datetime.datetime.fromtimestamp(now - ago, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
def rec(kind_, ago, **extra):
    r = {"type": kind_, "timestamp": ts(ago), "sessionId": sid, "entrypoint": "cli", "cwd": "/stand"}
    r.update(extra)
    return r
main = []
if kind in ("live", "done"):
    main.append(rec("assistant", 2400, message={"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_stand_" + helper, "name": "Agent",
         "input": {"description": "проба-помощник", "prompt": "задание стенда", "run_in_background": True}}]}))
    main.append(rec("user", 2400, message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_stand_" + helper, "content": "Async agent launched successfully."}]},
        toolUseResult={"status": "async_launched", "agentId": helper, "description": "проба-помощник"}))
if kind == "done":
    main.append(rec("user", 300, message={"role": "user", "content":
        "<task-notification>\n<task-id>" + helper + "</task-id>\n<status>completed</status>\n</task-notification>"}))
main.append(rec("assistant", 30, message={"role": "assistant", "content": [{"type": "text", "text": "Жду."}]}))
path = os.path.join(folder, sid + ".jsonl")
with open(path, "w", encoding="utf-8") as f:
    f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in main)
if kind in ("live", "done"):
    sub = os.path.join(folder, sid, "subagents")
    os.makedirs(sub, exist_ok=True)
    hp = os.path.join(sub, "agent-" + helper + ".jsonl")
    with open(hp, "w", encoding="utf-8") as f:
        f.write(json.dumps(rec("assistant", 60 if kind == "live" else 320, agentId=helper, isSidechain=True,
                               message={"role": "assistant", "content": [{"type": "text", "text": "Шаг сделан."}]}),
                           ensure_ascii=False) + "\n")
    with open(hp[:-len(".jsonl")] + ".meta.json", "w", encoding="utf-8") as f:
        json.dump({"description": "проба-помощник"}, f, ensure_ascii=False)
PY
}
stand() {  # $1 = сцена помощника
    rm -rf "$T/log" "$T/state" "$T/kanal.txt" "$T/pane.txt" "$T/detached.txt"
    mkdir -p "$T/log/inbox/обработано" "$PANEL_STATE"
    : > "$T/kanal.txt"; : > "$T/pane.txt"
    printf '%s\n' '{"queue": [], "lines": [{"id": "hr", "tasks": [{"id": "hr.a", "status": "wip"}]}]}' \
        > "$T/project/dev-map.yaml"
    scene "$1"
    jq -cn --arg ts "$(date -Is)" --arg s "$SESSION" \
        '{ts:$ts, event:"pre_compact", session:$s, detail:"стенд"}' > "$T/log/rotation.jsonl"
    tmux kill-session -t "$PANE" 2>/dev/null
    tmux new-session -d -s "$PANE" "exec $T/claude >> $T/pane.txt"
    sleep 0.5
    [ "$(tmux display-message -p -t "$PANE" '#{pane_current_command}')" = claude ] \
        || { echo "ПЛОХО СТЕНД: подставной агент не встал в панель — проверять нечего"; exit 1; }
}
RUN_N=0
run() {  # $@ = доводы сторожу; код выхода — в $T/rc
    local rc=0
    RUN_N=$((RUN_N + 1))
    env -u TMUX -u CLAUDE_CODE_SESSION_ID PATH="$T/bin:$PATH" \
        HARNESS_CONF="$T/harness.conf" HARNESS_INSTALL_CONF="$T/harness.conf" \
        HARNESS_ACTIVE_LINE=hr PANE_ENTER_DELAY=0.2 \
        HARNESS_PANEL_STATE="$PANEL_STATE" CLAUDE_PROJECTS_DIR="$T/projects" \
        timeout -k 5 120 bash "$WARDEN" "$@" > "$T/log/run.$RUN_N" 2>&1 < /dev/null || rc=$?
    cat "$T/log/run.$RUN_N" >> "$T/log/warden.out"
    sleep 0.3
    printf '%s\n' "$rc" > "$T/rc"
}
# Стенд обязан ДОЙТИ до решения о ротации, иначе проба судит падение.
reached() {
    grep -q "компактов за сессию: 1 (потолок 1)" "$T/log/run.$RUN_N" \
        || { echo "ПЛОХО СТЕНД: прогон $RUN_N не дошёл до счёта компактов — условие «нужна ротация» не стоит"; tail -20 "$T/log/run.$RUN_N"; exit 1; }
}
in_log() { local n; n=$(grep -c -- "$1" "$T/log/warden.out" 2>/dev/null); echo "${n:-0}"; }
event() { local n; n=$(grep -c "\"event\":\"$1\"" "$T/log/rotation.jsonl" 2>/dev/null); echo "${n:-0}"; }
door_called() { echo $(( $(in_log 'дверь ротации:') + $(in_log 'ротация ОТМЕНЕНА') + $(event rotate_refused) )); }
pane_count() { local n; n=$(grep -c -- "$1" "$T/pane.txt" 2>/dev/null); echo "${n:-0}"; }
flag_field() { jq -r --arg k "$1" '.[$k] // empty' "$FLAG" 2>/dev/null || echo "нет флага"; }
put_flag() {  # $1 = сессия, $2 = возраст метки, сек
    printf '{"сессия":"%s","этап":"ждёт","с":%s,"проверено":%s,"ждали_сек":0,"причина":"стенд","помощников":1,"принуждена":0,"сказано":null}\n' \
        "$1" "$(( $(date +%s) - $2 ))" "$(( $(date +%s) - $2 ))" > "$FLAG"
    touch -d "@$(( $(date +%s) - $2 ))" "$FLAG"
}

echo "С1. БОЛЬНОЙ СЛУЧАЙ: нужна ротация, помощник идёт — два прогона"
stand live; run; reached; run
case_check "С1 дверь ротации не зовётся, пока помощник идёт" "0" "$(door_called)"
case_check "С1 поручения ротации в панели нет" "0" "$(pane_count 'Пора менять')"
case_check "С1 флаг поставлен в panel-state (R-1)" "есть" "$([ -e "$FLAG" ] && echo есть || echo нет)"
case_check "С1 флаг несёт сессию смены (R-4)" "$SESSION" "$(flag_field сессия)"
case_check "С1 флаг: этап «ждёт»" "ждёт" "$(flag_field этап)"
case_check "С1 событие rotate_waits_helpers — одно за два прогона (R-18)" "1" "$(event rotate_waits_helpers)"
case_check "С1 весть в панель «ротация ждёт идущих помощников» — одна за два прогона (R-14)" \
    "1" "$(pane_count 'ротация ждёт идущих помощников')"
case_check "С1 весть в панели велит отвечать владельцу в канал" "1" "$(pane_count 'отвечай в канал как обычно')"
case_check "С1 весть в панели не велит владельцу молчать" "0" "$(pane_count 'не отвечай')"

echo "С2. ЗДОРОВАЯ ПОЛОВИНА: помощников нет — ротация как раньше"
stand none; run; reached
case_check "С2 без помощников дверь ротации зовётся" "да" "$([ "$(door_called)" -gt 0 ] && echo да || echo нет)"
case_check "С2 отказ двери — флага после прогона нет" "нет" "$([ -e "$FLAG" ] && echo есть || echo нет)"
case_check "С2 события rotate_waits_helpers нет" "0" "$(event rotate_waits_helpers)"

echo "С3. помощник уведомил о конце — ротация как раньше"
stand done; run; reached
case_check "С3 законченный помощник ротацию не держит — дверь зовётся" \
    "да" "$([ "$(door_called)" -gt 0 ] && echo да || echo нет)"

echo "С4. флаг ушедшей смены снимается любым прогоном (R-4)"
stand live; put_flag "ushedshaya-smena" 60; run; reached
case_check "С4 флаг теперь принадлежит текущей смене" "$SESSION" "$(flag_field сессия)"
case_check "С4 снятие чужого флага записано (rotation_flag_cleared)" \
    "да" "$([ "$(event rotation_flag_cleared)" -gt 0 ] && echo да || echo нет)"

echo "С5. флаг своей смены освежается каждым прогоном (R-5)"
stand live; put_flag "$SESSION" 3600; run; reached
case_check "С5 метка живости флага свежая (моложе 60 с)" "свежая" \
    "$([ -e "$FLAG" ] && [ $(( $(date +%s) - $(stat -c %Y "$FLAG") )) -lt 60 ] && echo свежая || echo старая)"

echo "С6. --rotate-now при идущем помощнике отвечает агенту ДО отвязки (R-7)"
stand live
run --rotate-now
case_check "С6 --rotate-now при помощнике — код 1" "1" "$(cat "$T/rc")"
case_check "С6 агенту сказано «смена отложена»" "да" "$(yes_no grep -q 'отложена' "$T/log/run.$RUN_N")"
case_check "С6 отвязанного процесса нет" "нет" "$([ -s "$T/detached.txt" ] && echo есть || echo нет)"
case_check "С6 флаг помечен «принуждена: 1»" "1" "$(flag_field принуждена)"
stand none
run --rotate-now
sleep 0.5   # подставной setsid пишет фоном
case_check "С6 без помощников --rotate-now отвязывается, как раньше (код 0)" \
    "0 есть" "$(cat "$T/rc") $([ -s "$T/detached.txt" ] && echo есть || echo нет)"

echo "W9. перед ротацией флаг «идёт» записан и в журнал ротации (rotation_flag_set)"
stand none; run; reached
case_check "W9 событие rotation_flag_set с этапом «идёт» в rotation.jsonl" "да" \
    "$([ "$(grep '"event":"rotation_flag_set"' "$T/log/rotation.jsonl" 2>/dev/null | grep -c 'идёт')" -gt 0 ] && echo да || echo нет)"
case_check "W9 к двери ротации флаг на диске уже с этапом «идёт»" "идёт" \
    "$(jq -r '.["этап"] // "нет флага"' "$T/log/flag-at-door.json" 2>/dev/null || echo "нет флага")"

# Проверка приёма сторожа и любые headless-прогоны (sdk-cli) пишут свой
# транскрипт рядом со сменой и свежее её: смена — не самый свежий файл.
echo "С7. свежий headless-транскрипт (sdk-cli) рядом со сменой, помощник идёт, флаг смены живой"
stand live; put_flag "$SESSION" 60
python3 - "$TR_DIR/hl-0001.jsonl" <<'HL'
import json, sys, datetime
ts = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
open(sys.argv[1], "w").write(json.dumps({"type": "assistant", "timestamp": ts, "sessionId": "hl-0001", "entrypoint": "sdk-cli",
    "message": {"role": "assistant", "content": [{"type": "text", "text": "приём"}]}}) + "\n")
HL
run
case_check "С7 флаг смены пережил прогон при свежем headless" "$SESSION" "$(flag_field сессия)"
case_check "С7 флаг смены не снят как «ушедшей смены» (rotation_flag_cleared нет)" "0" "$(event rotation_flag_cleared)"

echo "С9. флаг своей смены без иного повода — сам по себе назревшая ротация (помощники кончили)"
stand none
: > "$T/log/rotation.jsonl"     # повода по компактам нет — только флаг
put_flag "$SESSION" 60
run
case_check "С9 флаг своей смены без помощников — дверь ротации зовётся" "да" "$([ "$(door_called)" -gt 0 ] && echo да || echo нет)"
case_check "С9 после прогона флага нет (ротация шла и не состоялась — снят)" "нет" "$([ -e "$FLAG" ] && echo есть || echo нет)"

echo "С10. пауза владельца при живом флаге — флаг освежается (иначе хук сочтёт его протухшим)"
stand live; put_flag "$SESSION" 3600
HARNESS_PANEL_STATE="$PANEL_STATE" python3 "$HERE/pause.py" --поставить проба --кто владелец >/dev/null 2>&1
case_check "С10 пауза на стенде поставлена" "да" \
    "$(yes_no env HARNESS_PANEL_STATE="$PANEL_STATE" python3 "$HERE/pause.py" --идёт-ли)"
run
case_check "С10 на паузе метка живости флага свежая (моложе 60 с)" "свежая" \
    "$([ -e "$FLAG" ] && [ $(( $(date +%s) - $(stat -c %Y "$FLAG") )) -lt 60 ] && echo свежая || echo старая)"

printf '\nРОТАЦИЯ ЖДЁТ ПОМОЩНИКОВ: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
