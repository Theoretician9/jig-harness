#!/usr/bin/env bash
# Проба сторожа сессий целым прогоном: «на паузе ротации нет».
#
# Владелец 20.10.2026: «работа стоит на паузе, но постоянно дёргает сменить
# сессию. если пауза то пауза». Больной случай: на паузе сторож каждые десять
# минут доходил до двери ротации, дверь отказывала («передача дел не
# написана»), в журнал ложилось «ротация ОТМЕНЕНА», в канал — «Смену сессии
# отменил». Решение о ротации стоит в потоке прогона раньше проверки паузы.
#
# Что судится: обвязка — что доходит до двери ротации, до канала и до журнала.
# Пауза ставится НАСТОЯЩИМ pause.py на каталоге состояния стенда
# (HARNESS_PANEL_STATE), тем же, который читает сторож; подмены функций нет.
#
# Условие «нужна ротация» — событие pre_compact этой сессии при MAX_COMPACTS=1.
# Дверь ротации на стенде отказывает всегда: судья передачи (handover-check.sh
# стенда) отвечает «передачи нет». Отказ двери — признак «ротация пошла»:
# быстрый и не ждёт регламента в панели 900 с.
#
# Свой стенд: своя tmux-панель с подставным «агентом» (копия sleep под именем
# claude), свой LOG_DIR, свой канал (файл), свой каталог состояния. В боевую
# панель и в боевой канал проба не пишет ни байта.
#
# Имена переменных латиницей (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON="$HERE/../harness/demons/session-warden.sh"
[ -f "$DAEMON" ] || { echo "ПЛОХО: сторожа нет по пути раскладки проекта: $DAEMON"; exit 1; }
PAUSE_PY="$HERE/pause.py"
[ -f "$PAUSE_PY" ] || { echo "ПЛОХО: pause.py нет рядом: $PAUSE_PY"; exit 1; }

PREFIX="warden-pauserot-"
# Хвосты прежних прогонов, убитых SIGKILL мимо ловушки: чистим по префиксу ДО
# работы и называем число.
T=$(mktemp -d)
PANE="$PREFIX$$"
# Свой сервер tmux на прогон: панель стенда на боевом сервере ворота выката
# читали как работу агента (11.11.2026). Обёртка первой в PATH ведёт туда и
# голый tmux сторожа; TMUX снят, иначе клиент пошёл бы по сокету из неё.
# Сокет — в каталоге стенда (TMUX_TMPDIR) и уходит вместе с ним.
REAL_TMUX="$(command -v tmux)"
SOCK_NAME="$PANE"
unset TMUX TMUX_PANE
export TMUX_TMPDIR="$T/tmux"
mkdir -p "$T/tmux-bin" "$TMUX_TMPDIR"
printf '#!/bin/sh\nexec "%s" -L "%s" "$@"\n' "$REAL_TMUX" "$SOCK_NAME" > "$T/tmux-bin/tmux"
chmod +x "$T/tmux-bin/tmux"
export PATH="$T/tmux-bin:$PATH"
# Проба личности сторожа (self-prompt-live.sh) со стенда ушла бы фоном к модели
# через claude-run.sh стенда, а её тревога легла бы в канал стенда посреди
# чужого случая. Стенду — пустая проба.
printf '#!/bin/sh\nexit 0\n' > "$T/self-prompt-live.sh"
export SELF_PROMPT_LIVE_SH="$T/self-prompt-live.sh"
# Прогон, убитый KILL-ом, ловушку не зовёт: панель сама гасит свой сервер,
# когда прогона или каталога стенда не стало. Сервер — по pid из $TMUX панели:
# сокет к тому мигу может уйти вместе с каталогом. Сторож отвязан двойным
# fork: потомок claude в панели сторож сессий читал бы как работу агента.
pane_cmd() {  # $1 = команда панели → она же со сторожем жизни стенда
    printf '( { while kill -0 %s && [ -d %q ]; do sleep 2; done; s=${TMUX#*,}; kill "${s%%%%,*}"; } >/dev/null 2>&1 & ); %s' \
        "$$" "$T" "$1"
}
cleanup() { "$REAL_TMUX" -L "$SOCK_NAME" kill-server 2>/dev/null || true; rm -rf "$T"; }
trap cleanup EXIT

cp /bin/sleep "$T/claude"
mkdir -p "$T/project/scripts/lib" "$T/projects"
for f in work-queue.py limit_mark.py limit-in-log.py stall-in-log.py; do
    cp "$HERE/$f" "$T/project/scripts/"
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
# Судья передачи стенда: «передачи после поручения нет» (код 1) на любой вопрос.
# Исключение — флаг-файл .handover-written рядом с scripts/: его ставит только
# случай 4б, где передача «написана» и путь обязан дойти до «/exit».
cat > "$T/project/scripts/handover-check.sh" <<'SUDYA'
#!/usr/bin/env bash
if [ -e "$(dirname "$0")/../.handover-written" ]; then
    echo "передача стенда: написана"
    exit 0
fi
echo "передача стенда: не написана"
exit 1
SUDYA
chmod +x "$T/project/scripts/tg_send.sh" "$T/project/scripts/handover-check.sh"
TR_DIR="$T/projects/${T//\//-}-project"
mkdir -p "$TR_DIR"
SESSION="proba"
TRANSCRIPT="$TR_DIR/$SESSION.jsonl"

SEED=""
for c in "$HERE/../config/harness.conf" "${HARNESS_CONF:-/etc/harness/harness.conf}"; do
    [ -f "$c" ] && { SEED="$c"; break; }
done
[ -n "$SEED" ] || { echo "ПЛОХО: образца harness.conf нет ни в пакете, ни в /etc/harness — проверять нечем"; exit 1; }
cp "$SEED" "$T/harness.conf"
cat >> "$T/harness.conf" <<CONF
AGENT_USER="$(id -un)"
LOG_DIR="$T/log"
PROJECT_DIR="$T/project"
STATE_DIR="$T/state"
HEARTBEAT_DIR="$T/state/heartbeat"
TMUX_SESSION="$PANE"
AGENT_START_CMD="$T/claude 600"
SERVICE_LOCKS_SUBDIR="проверка"
ACTIVE_LINE="hr"
CTX_WINDOW_TOKENS=400000  # не-секрет: размер окна модели в токенах, а не ключ
MAX_COMPACTS=1
ROTATE_NEEDS_OWNER=0
ROTATE_WITHOUT_HANDOVER=0
ROTATE_REFUSED_TELL_GAP_MIN=180
SILENCE_MAX_MIN=30
PULSE_MAX_MIN=30
PULSE_OWNER_MIN=60
LIMIT_STALL_MIN=20
TG_CHAT_ID=""
TG_TOKEN_FILE="/nonexistent"
CONF

tmux new-session -d -s "$PANE" -c "$T/project" "$(pane_cmd "exec $T/claude 3600")"
sleep 1
[ "$(tmux display-message -p -t "$PANE" '#{pane_current_command}')" = claude ] \
    || { echo "ПЛОХО: подставной агент не встал в панель — проверять нечего"; exit 1; }

ok=1
probes=0
PANEL_STATE="$T/state/panel-state"
DEFER_LINE="ротация отложена: пауза владельца"

# Стенд с нуля: свежий журнал, пустой канал, нет паузы, условие «нужна ротация».
stand() {
    RUN_N=0
    rm -rf "$T/log" "$T/state" "$T/kanal.txt"
    mkdir -p "$T/log/inbox/обработано" "$PANEL_STATE"
    : > "$T/kanal.txt"
    printf '%s\n' '{"queue": [], "lines": [{"id": "hr", "tasks": [{"id": "hr.a", "status": "wip"}]}]}' \
        > "$T/project/dev-map.yaml"
    printf '{"type":"assistant","timestamp":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$TRANSCRIPT"
    jq -cn --arg ts "$(date -Is)" --arg s "$SESSION" \
        '{ts:$ts, event:"pre_compact", session:$s, detail:"стенд"}' > "$T/log/rotation.jsonl"
}
pause_py() { HARNESS_PANEL_STATE="$PANEL_STATE" python3 "$PAUSE_PY" "$@"; }
pause_on() {
    pause_py --поставить "проба: владелец остановил" --кто владелец \
        || { echo "ПЛОХО: pause.py не поставил паузу на стенде — проверять нечего"; exit 1; }
    pause_py --идёт-ли || { echo "ПЛОХО: pause.py не видит своей паузы — проверять нечего"; exit 1; }
}
pause_off() {
    pause_py --снять || { echo "ПЛОХО: pause.py не снял паузу на стенде"; exit 1; }
    ! pause_py --идёт-ли || { echo "ПЛОХО: пауза стоит после --снять"; exit 1; }
}

# Один прогон сторожа. Вывод прогона — отдельным файлом (run.N), журнал стенда —
# их сумма (warden.out). $1 — довод сторожу (пусто | --rotate-now).
RUN_N=0
run() {
    RUN_N=$((RUN_N + 1))
    env -u TMUX HARNESS_CONF="$T/harness.conf" HARNESS_INSTALL_CONF="$T/harness.conf" \
        HARNESS_ACTIVE_LINE=hr WARDEN_DETACHED=1 \
        HARNESS_PANEL_STATE="$PANEL_STATE" CLAUDE_PROJECTS_DIR="$T/projects" \
        bash "$DAEMON" ${1:+"$1"} > "$T/log/run.$RUN_N" 2>&1 < /dev/null
    cat "$T/log/run.$RUN_N" >> "$T/log/warden.out"
    # Стенд обязан ДОЙТИ до решения о ротации: иначе проба судит падение, а не
    # поведение. Без довода условие — компакт; с --rotate-now — просьба агента.
    if [ -z "${1:-}" ]; then
        grep -q "компактов за сессию: 1 (потолок 1)" "$T/log/run.$RUN_N" \
            || { echo "ПЛОХО СТЕНД: прогон $RUN_N не дошёл до счёта компактов — условие «нужна ротация» не стоит"; tail -20 "$T/log/run.$RUN_N"; exit 1; }
    fi
}
in_run() { local n; n=$(grep -c -- "$2" "$T/log/run.$1" 2>/dev/null); echo "${n:-0}"; }
in_log() { local n; n=$(grep -c -- "$1" "$T/log/warden.out" 2>/dev/null); echo "${n:-0}"; }
said() { local n; n=$(grep -ci -- "$1" "$T/kanal.txt" 2>/dev/null); echo "${n:-0}"; }
event() { local n; n=$(grep -c "\"event\":\"$1\"" "$T/log/rotation.jsonl" 2>/dev/null); echo "${n:-0}"; }
check() {  # $1=ждём $2=вышло $3=зачем
    probes=$((probes + 1))
    if [ "$1" = "$2" ]; then printf '  ок    %s\n' "$3"
    else printf '  ПЛОХО %s: вышло «%s», ждали «%s»\n' "$3" "$2" "$1"; ok=0; fi
}
door_called() { echo $(( $(in_log 'дверь ротации:') + $(in_log 'ротация ОТМЕНЕНА') )); }

# ── 1. БОЛЬНОЙ СЛУЧАЙ: пауза владельца, нужна ротация, два прогона ──────────
# Какая поломка оставит его зелёным? Починка, глушащая ротацию всегда, — её
# ловит случай 2; не снимающая метку антиповтора — случай 5; пишущая строку
# каждый прогон — счёт «ровно одна» здесь.
echo "1. на паузе ротации нет (два прогона)"
stand; pause_on; run; run
check 0 "$(event rotate_refused)" "БОЛЬНОЙ СЛУЧАЙ: на паузе дверь ротации не отказывает — события rotate_refused нет"
check 0 "$(door_called)" "дверь ротации на паузе не зовётся вовсе (ни «дверь ротации:», ни «ротация ОТМЕНЕНА»)"
check 0 "$(event rotate_command)" "поручения ротации в панель нет"
check 0 "$(said 'Смену сессии отменил')" "в канал нет «Смену сессии отменил»"
check 0 "$(said 'ротац')" "в канал о ротации ни слова"
check 1 "$(in_log "$DEFER_LINE")" "в журнале стенда ровно одна строка «$DEFER_LINE» за два прогона"
check 0 "$(in_run 2 "$DEFER_LINE")" "второй прогон на той же паузе строку не повторяет"
check 0 "$(pause_py --идёт-ли; echo $?)" "пауза владельца стоит после прогонов (сторож её не снял)"

# ── 2. БОЛЬНОЙ СЛУЧАЙ живьём: без паузы — как сейчас ────────────────────────
# Замер на коде до починки (24.10.2026): дверь зовётся каждый прогон, отказ
# rotate_refused пишется каждый прогон, весть «Смену сессии отменил» — одна
# (кулдаун ROTATE_REFUSED_TELL_GAP_MIN=180). Поломка, которую он ловит:
# починка, глушащая ротацию без паузы.
echo "2. без паузы ротация идёт, как прежде"
stand; run; run
check 2 "$(event rotate_refused)" "без паузы дверь зовётся оба прогона — rotate_refused дважды"
check 1 "$(said 'Смену сессии отменил')" "без паузы весть об отказе в канал — одна (кулдаун)"
check 0 "$(in_log "$DEFER_LINE")" "без паузы строки «$DEFER_LINE» нет"

# ── 3. Конец паузы: ротация снова идёт, «отложена» не повторяется ──────────
# Поломка, которую он ловит: метка «отложена» переживает паузу и глушит
# ротацию после её конца; строка «отложена» пишется и без паузы.
echo "3. после pause.py --снять ротация снова идёт"
stand; pause_on; run; pause_off; run
check 1 "$(in_log "$DEFER_LINE")" "строка «$DEFER_LINE» — одна, от прогона на паузе"
check 0 "$(in_run 2 "$DEFER_LINE")" "после снятия паузы строка не повторяется"
check 1 "$(event rotate_refused)" "после снятия паузы дверь зовётся — ровно один отказ (во втором прогоне)"
check 1 "$(( $(in_run 2 'ротация ОТМЕНЕНА') > 0 ? 1 : 0 ))" "отказ двери — в прогоне ПОСЛЕ снятия"

# ── 4. Явный --rotate-now паузой не гасится ─────────────────────────────────
# Агент просит смену сам, отработав регламент: пауза не вправе отнять у него
# единственный законный уход. Поломка: починка гасит ВСЮ ветку ротации.
echo "4. --rotate-now на паузе идёт до двери"
stand; pause_on; run --rotate-now
check 1 "$(in_log 'ротацию запросил агент')" "на паузе --rotate-now дошёл до решения о ротации"
check 1 "$(event rotate_refused)" "на паузе --rotate-now дошёл до двери (её отказ записан)"
check 0 "$(in_log "$DEFER_LINE")" "на --rotate-now строки «$DEFER_LINE» нет"

# ── 4б. Пауза, передача написана, --rotate-now: «/exit» доходит до панели ────
# Случай 4 останавливается на отказе двери и потому слеп к тому, ЧЕМ подаётся
# «/exit»: подай его служебная дверь (её держит пауза) — «/exit» не уйдёт, а в
# канал ляжет ложное «в панели не агент». Здесь дверь открыта, и судится сам
# «/exit»: его получил агент стенда, и канал не врёт про «не агент».
#
# Агент стенда — копия python3 под именем claude: пишет каждую полученную
# строку в файл и выходит на «/exit», как настоящий. Его уход закрывает
# tmux-сессию, и сторож доводит смену до подъёма преемника (sleep-«claude»).
echo "4б. --rotate-now на паузе, передача написана: «/exit» доходит до панели"
mkdir -p "$T/py"
cp "$(readlink -f "$(command -v python3)")" "$T/py/claude"
cat > "$T/agent.py" <<'AGENT'
import sys
log = sys.argv[1]
for line in sys.stdin:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(line)
    if line.strip() == "/exit":
        break
AGENT
# Сроки смены — короткие: стенд не ждёт боевых 900 с, а на поломке сторож не
# уходит в долгое ожидание.
cat >> "$T/harness.conf" <<CONF
ROTATE_FINISH_TIMEOUT_SEC=60
ROTATE_EXIT_WAIT_SEC=30
AGENT_START_TIMEOUT_SEC=15
AGENT_WAKE_DELAY=0
CONF
GOT="$T/agent-got.txt"
stand; pause_on
rm -f "$GOT"
tmux kill-session -t "$PANE" 2>/dev/null
tmux new-session -d -s "$PANE" -c "$T/project" "$(pane_cmd "exec $T/py/claude -I -u $T/agent.py $GOT")"
sleep 1
[ "$(tmux display-message -p -t "$PANE" '#{pane_current_command}' 2>/dev/null)" = claude ] \
    || { echo "ПЛОХО СТЕНД: агент-python не встал в панель — проверять нечего"; exit 1; }
touch "$T/project/.handover-written"
RUN_N=$((RUN_N + 1))
env -u TMUX HARNESS_CONF="$T/harness.conf" HARNESS_INSTALL_CONF="$T/harness.conf" \
    HARNESS_ACTIVE_LINE=hr WARDEN_DETACHED=1 \
    HARNESS_PANEL_STATE="$PANEL_STATE" CLAUDE_PROJECTS_DIR="$T/projects" \
    timeout -k 5 180 bash "$DAEMON" --rotate-now > "$T/log/run.$RUN_N" 2>&1 < /dev/null
cat "$T/log/run.$RUN_N" >> "$T/log/warden.out"
rm -f "$T/project/.handover-written"
check 1 "$(( $(in_log 'дверь ротации: да') > 0 ? 1 : 0 ))" "СТЕНД: дверь ротации открыта судьёй «написана»"
check 0 "$(event rotate_refused)" "СТЕНД: дверь ротации открыта (отказа нет)"
got_exit() { local n; n=$(grep -cx -- '/exit' "$GOT" 2>/dev/null); echo "${n:-0}"; }
check 1 "$(got_exit)" "БОЛЬНОЙ: на паузе «/exit» дошёл до агента в панели"
check 0 "$(said 'не агент')" "БОЛЬНОЙ: в канал нет ложного «в панели не агент»"
check 1 "$(event rotate_command)" "команда ротации записана (rotate_command)"
check 0 "$(pause_py --идёт-ли; echo $?)" "пауза владельца стоит после смены (сторож её не снял)"
# Панель стенда — обратно sleep-«claude» для случая 5.
tmux kill-session -t "$PANE" 2>/dev/null
tmux new-session -d -s "$PANE" -c "$T/project" "$(pane_cmd "exec $T/claude 3600")"
sleep 1

# ── 5. Новая пауза после снятия — снова одна строка ─────────────────────────
# Метку антиповтора снимает конец паузы (pause.py --снять). Поломка: метка,
# которую не снимает никто, — следующая пауза молчит, и не видно, почему
# ротации нет.
echo "5. вторая пауза после снятия снова говорит одной строкой"
stand; pause_on; run; pause_off; pause_on; run; run
check 2 "$(in_log "$DEFER_LINE")" "две паузы — две строки «$DEFER_LINE» (по одной на паузу)"
check 1 "$(in_run 2 "$DEFER_LINE")" "первый прогон второй паузы строку пишет"
check 0 "$(in_run 3 "$DEFER_LINE")" "второй прогон второй паузы — молчит"
check 0 "$(event rotate_refused)" "ни на одной из пауз дверь не зовётся"

if (( ok )); then echo "SELFTEST: зелёный ($probes проб обвязки сторожа «на паузе ротации нет», 6 случаев)"; exit 0; fi
echo "SELFTEST: КРАСНЫЙ ($probes проб, 6 случаев)"; exit 1
