#!/usr/bin/env bash
# Проба ключа события живой пробы личности (hr.standards-and-policy, спека §6.2, K49).
#
# Проба «назови метку» идёт только по событию: сменилась версия Claude Code,
# версия харнеса или носители личности. Замечает событие сторож сессий
# (harness/demons/session-warden.sh) — сверяет ключ с файлом состояния и, если
# ключ другой, один раз фоном запускает scripts/self-prompt-live.sh. Потолок —
# 4 запуска за сутки: при скачущем ключе (версия CLI мигает, носитель правят в
# цикле) иначе каждый обход сторожа раз в 10 минут тратил бы вызов модели.
#
# Что судится — две функции сторожа (берутся из ЖИВОГО файла, дверь мутаций
# HARNESS_SESSION_WARDEN; весь сторож не запускается — он поднимал бы смену):
#   self_prompt_live_key    печатает ключ одной строкой: версия CLI
#                           («$CLAUDE_BIN --version»), версия харнеса (файл
#                           config.version_file(), дверь HARNESS_VERSION_FILE) и
#                           sha256 носителей и слоя 1 под $PROJECT_DIR;
#   self_prompt_live_check  ключ ≠ $SELF_PROMPT_LIVE_STATE и запусков за 24 ч в
#                           $LOG_DIR/self-prompt-live.runs меньше 4 → строка
#                           «<epoch> <ключ>» в .runs и один фоновый запуск
#                           «bash $SELF_PROMPT_LIVE_SH --ключ <ключ>».
# Берутся все функции сторожа с именем self_prompt_live*; из прочего им доступны
# только say, jlog, alert_task и tg_send (здесь — заглушки).
#
# Стенд: свой каталог проекта с носителями-пустышками, подставной claude
# (печатает версию из файла стенда) и подставная проба (пишет след; «зелёная»
# записывает ключ в файл состояния, как настоящая при названной метке).
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

BASE=$(mktemp -d /tmp/sap-proba-XXXXXX)
trap 'rm -rf "$BASE"' EXIT

FNS="$BASE/fns.sh"
mapfile -t FN_NAMES < <(grep -oE '^self_prompt_live[A-Za-z0-9_]*\(\) \{' "$WARDEN" | sed 's/() {$//')
for fn in "${FN_NAMES[@]}"; do
    sed -n "/^$fn() {/,/^}/p" "$WARDEN" >> "$FNS"
done
touch "$FNS"
for fn in self_prompt_live_key self_prompt_live_check; do
    grep -q "^$fn() {" "$FNS" || echo "  (функции $fn в сторожe $WARDEN нет — случаи ниже красные)"
done

# Стенд: $1 = имя. Носители — пустышки с разным текстом: проба судит, что ключ
# следует за их байтами, а не что в них написано.
make_stand() {
    local s="$BASE/$1"
    mkdir -p "$s/proj/scripts/lib" "$s/proj/.claude/agents" "$s/proj/harness/config" \
             "$s/proj/harness/self/roles" "$s/log" "$s/bin" "$s/state"
    echo 'echo носитель' > "$s/proj/scripts/claude-run.sh"
    echo 'print("сборщик")' > "$s/proj/scripts/self-prompt.py"
    echo 'konf_zagruzit() { :; }' > "$s/proj/scripts/lib/config.sh"
    echo 'echo обёртка демона' > "$s/proj/scripts/claude-demon.sh"
    echo 'echo посторонний' > "$s/proj/scripts/other.sh"
    printf -- '---\nname: harness-worker\n---\nотпечаток: исполнитель-000001\n' \
        > "$s/proj/.claude/agents/harness-worker.md"
    echo 'классы: {прибор: [ядро_кратко]}' > "$s/proj/harness/config/launch-policy.yaml"
    echo 'прочее: 1' > "$s/proj/harness/config/other.yaml"
    echo '{"сущность": "проба"}' > "$s/proj/harness/self/core.json"
    echo 'роль исполнителя' > "$s/proj/harness/self/roles/worker.md"
    echo '1.0.0' > "$s/state/версия-харнеса"
    echo '2.1.299' > "$s/ver"
    printf '#!/usr/bin/env bash\n[ "${1:-}" = --version ] && { echo "$(cat %q) (Claude Code)"; exit 0; }\nexit 1\n' \
        "$s/ver" > "$s/bin/claude"
    chmod +x "$s/bin/claude"
    # Подставная проба: след — после записи состояния, чтобы ожидание следа
    # означало и готовое состояние. Режим берётся из файла стенда, а не из
    # окружения: сторож вправе звать пробу с урезанным окружением.
    echo green > "$s/mode"
    cat > "$s/probe.sh" <<EOF
#!/usr/bin/env bash
key=""
while [ \$# -gt 0 ]; do
    case "\$1" in --ключ) key="\${2:-}"; shift 2 ;; *) shift ;; esac
done
# Долгая проба (режим slow): след сразу, потом минуты работы — exec, чтобы PID
# в файле был тем процессом, что держит унаследованные дескрипторы.
[ "\$(cat '$s/mode')" = slow ] && { echo \$\$ > '$s/slow.pid'; printf '%s\n' "\$key" >> '$s/trace'; exec sleep 60; }
[ "\$(cat '$s/mode')" = green ] && printf '%s\n' "\$key" > '$s/log/self-prompt-live.state'
printf '%s\n' "\$key" >> '$s/trace'
EOF
    : > "$s/trace"
    printf '%s' "$s"
}

# Окружение функций сторожа на стенде $1; $2… — команда в нём.
in_stand() {
    local s="$1"; shift
    PROJECT_DIR="$s/proj" LOG_DIR="$s/log" HARNESS_SCRIPTS="$HERE" \
    CLAUDE_BIN="$s/bin/claude" HARNESS_VERSION_FILE="$s/state/версия-харнеса" \
    SELF_ETALON_DIR="${ETALON:-$s/no-etalon}" \
    SELF_PROMPT_LIVE_SH="$s/probe.sh" SELF_PROMPT_LIVE_STATE="$s/log/self-prompt-live.state" \
    SAY_LOG="$s/say.log" FNS="$FNS" \
        bash -c 'set -euo pipefail
                 say() { printf "%s\n" "$*" >> "$SAY_LOG"; }
                 jlog() { :; }; alert_task() { :; }; tg_send() { :; }
                 source "$FNS"
                 "$@"' _ "$@"
}

key_of() { in_stand "$1" self_prompt_live_key 2>/dev/null; }
# Сравнение ключей до и после: пустой ключ — «пусто», не «тот же», иначе случаи
# «ключ тот же» зеленели бы без всякого ключа.
compare() { # $1 = до, $2 = после
    if [ -z "$1" ] || [ -z "$2" ]; then echo пусто
    elif [ "$1" = "$2" ]; then echo "тот же"
    else echo другой; fi
}
launches() { wc -l < "$1/trace" | tr -d ' '; }

# Один обход сторожа: печатает «<rc> <новых запусков>». Запуск фоновый — ждём
# след до 10 с, затем ещё секунду, чтобы второй (лишний) запуск успел оставить след.
pass() {
    local s="$1" before rc i=0
    before=$(launches "$s")
    in_stand "$s" self_prompt_live_check >/dev/null 2>&1
    rc=$?
    while [ "$(launches "$s")" -le "$before" ] && [ "$i" -lt "${2:-100}" ]; do sleep 0.1; i=$((i + 1)); done
    sleep 1
    echo "$rc $(( $(launches "$s") - before ))"
}

echo "── ключ события: из чего он складывается ──"
S=$(make_stand key)
K0=$(key_of "$S")
case_check "К1: ключ не пуст" "да" "$([ -n "$K0" ] && echo да || echo нет)"
case_check "К2: тот же стенд — тот же ключ" "тот же" "$(compare "$K0" "$(key_of "$S")")"

# $1 = имя случая, $2 = файл, $3 = ждём «другой»|«тот же»; правка — дописать строку.
key_after_edit() {
    local before after
    before=$(key_of "$S")
    printf '# правка пробы\n' >> "$2"
    after=$(key_of "$S")
    case_check "$1" "$3" "$(compare "$before" "$after")"
}
before=$(key_of "$S"); echo '2.1.300' > "$S/ver"
case_check "К3: версия CLI сменилась — ключ другой" "другой" \
    "$(compare "$before" "$(key_of "$S")")"
before=$(key_of "$S"); echo '1.0.1' > "$S/state/версия-харнеса"
case_check "К4: версия харнеса (config.version_file) сменилась — ключ другой" "другой" \
    "$(compare "$before" "$(key_of "$S")")"
key_after_edit "К5: правка scripts/claude-run.sh — ключ другой" "$S/proj/scripts/claude-run.sh" другой
key_after_edit "К6: правка scripts/self-prompt.py — ключ другой" "$S/proj/scripts/self-prompt.py" другой
key_after_edit "К7: правка scripts/lib/config.sh — ключ другой" "$S/proj/scripts/lib/config.sh" другой
key_after_edit "К8: правка scripts/claude-demon.sh — ключ другой" "$S/proj/scripts/claude-demon.sh" другой
key_after_edit "К9: правка карточки .claude/agents/*.md — ключ другой" \
    "$S/proj/.claude/agents/harness-worker.md" другой
key_after_edit "К10: правка launch-policy.yaml — ключ другой" "$S/proj/harness/config/launch-policy.yaml" другой
key_after_edit "К11: правка слоя 1 harness/self/core.json (эталона нет) — ключ другой" \
    "$S/proj/harness/self/core.json" другой
key_after_edit "К12: правка слоя 1 harness/self/roles/worker.md — ключ другой" \
    "$S/proj/harness/self/roles/worker.md" другой
before=$(key_of "$S")
printf -- '---\nname: harness-lead\n---\n' > "$S/proj/.claude/agents/harness-lead.md"
case_check "К13: новая карточка помощника — ключ другой" "другой" \
    "$(compare "$before" "$(key_of "$S")")"
key_after_edit "К14: правка постороннего scripts/other.sh — ключ тот же" "$S/proj/scripts/other.sh" "тот же"
key_after_edit "К15: правка постороннего harness/config/other.yaml — ключ тот же" \
    "$S/proj/harness/config/other.yaml" "тот же"
before=$(key_of "$S"); touch -d '2001-01-01' "$S/proj/scripts/claude-run.sh"
case_check "К16: сменилось только время файла носителя — ключ тот же (байты, не mtime)" "тот же" \
    "$(compare "$before" "$(key_of "$S")")"
mkdir -p "$S/etalon"; echo '{"сущность": "эталон"}' > "$S/etalon/core.json"
before=$(ETALON="$S/etalon" key_of "$S"); echo '{"сущность": "эталон, правка"}' > "$S/etalon/core.json"
case_check "К17: эталон есть — правка его core.json меняет ключ" "другой" \
    "$(compare "$before" "$(ETALON="$S/etalon" key_of "$S")")"

echo "── обходы сторожа: ровно один запуск на событие, потолок 4 за сутки ──"
S=$(make_stand day)
r=$(pass "$S"); case_check "О1: состояния нет — один запуск" "0 1" "$r"
K0=$(key_of "$S")
case_check "О2: проба получила ключ сторожа доводом --ключ" "${K0:-ключ сторожа (пуст)}" "$(tail -1 "$S/trace")"
r=$(pass "$S" 10); case_check "О3: тот же ключ (проба зелёная, ключ в состоянии) — ноль запусков" "0 0" "$r"
echo '2.1.300' > "$S/ver"
r=$(pass "$S"); case_check "О4: версия CLI сменилась — ровно один запуск" "0 1" "$r"
r=$(pass "$S" 10); case_check "О5: снова тот же ключ — ноль" "0 0" "$r"
printf '# правка\n' >> "$S/proj/scripts/claude-run.sh"
r=$(pass "$S"); case_check "О6: правка claude-run.sh — ровно один запуск" "0 1" "$r"
echo '1.0.1' > "$S/state/версия-харнеса"
r=$(pass "$S"); case_check "О7: версия харнеса сменилась — один запуск (4-й за сутки)" "0 1" "$r"
printf '# правка\n' >> "$S/proj/scripts/self-prompt.py"
r=$(pass "$S" 20); case_check "О8: 5-й запуск за сутки — не идёт (потолок 4), сторож не падает" "0 0" "$r"
case_check "О9: в журнале запусков 4 строки «<epoch> <ключ>»" "4" \
    "$(grep -cE '^[0-9]{9,} [^ ]' "$S/log/self-prompt-live.runs" 2>/dev/null || echo 0)"

echo "── проба не назвала метку: ключ не записан, обходы повторяют до потолка ──"
S=$(make_stand deaf); echo deaf > "$S/mode"
total=0; rcs=""
for _ in 1 2 3 4 5 6; do r=$(pass "$S" 20); rcs="$rcs${r%% *}"; total=$((total + ${r##* })); done
case_check "Г1: 6 обходов с тем же ключом при красной пробе — запусков 4" "4" "$total"
case_check "Г2: сторож не упал ни разу" "000000" "$rcs"

echo "── скачущий ключ (версия CLI мигает) — не больше 4 за сутки ──"
S=$(make_stand jumping)
total=0
for v in 2.1.301 2.1.302 2.1.301 2.1.302 2.1.301 2.1.302 2.1.301 2.1.302; do
    echo "$v" > "$S/ver"; r=$(pass "$S" 20); total=$((total + ${r##* }))
done
case_check "С1: 8 обходов с мигающей версией — запусков 4" "4" "$total"

echo "── сутки — скользящие 24 ч по журналу запусков ──"
S=$(make_stand old)
old=$(( $(date +%s) - 25 * 3600 ))
for _ in 1 2 3 4; do echo "$old старый-ключ"; done > "$S/log/self-prompt-live.runs"
r=$(pass "$S"); case_check "Д1: 4 запуска 25 ч назад потолок не держат — один запуск" "0 1" "$r"
S=$(make_stand fresh)
recent=$(( $(date +%s) - 3600 ))
for _ in 1 2 3 4; do echo "$recent свежий-ключ"; done > "$S/log/self-prompt-live.runs"
r=$(pass "$S" 20); case_check "Д2: 4 запуска час назад — потолок держит, ноль" "0 0" "$r"

echo "── фоновая проба не держит лок сторожа (fd 8) ──"
# Сторож держит flock на дескрипторе 8 весь обход. Фоновая проба живёт минуты:
# унаследуй она fd 8, следующие обходы видели бы «лок занят» и выходили без
# действий (разбор ворот 10.11, «лок панели», «дверь толчка»).
S=$(make_stand lock); echo slow > "$S/mode"
LOCK_FILE="$S/warden.lock" in_stand "$S" eval \
    'exec 8>"$LOCK_FILE"; flock -n 8 || exit 9; self_prompt_live_check' >/dev/null 2>&1
rc=$?
i=0; while [ ! -s "$S/slow.pid" ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
case_check "Л1: обход с локом прошёл, долгая проба запущена" "0 да" \
    "$rc $([ -s "$S/slow.pid" ] && echo да || echo нет)"
case_check "Л2: обход кончился, проба ещё идёт — лок свободен (flock -n проходит)" "свободен" \
    "$(flock -n "$S/warden.lock" true 2>/dev/null && echo свободен || echo занят)"
[ -s "$S/slow.pid" ] && kill "$(cat "$S/slow.pid")" 2>/dev/null

echo "── CLI нет: сторож не падает ──"
S=$(make_stand nocli); rm -f "$S/bin/claude"
r=$(pass "$S" 20)
case_check "Н1: claude --version не отвечает — обход сторожа rc 0" "0" "${r%% *}"

echo
if [ "$CASES_FAILED" -eq 0 ]; then
    echo "ПРОБА ЗЕЛЁНАЯ: случаев $CASES_PATHS, падений 0"
    exit 0
fi
echo "ПРОБА КРАСНАЯ: падений $CASES_FAILED из $CASES_PATHS случаев"
exit 1
