#!/usr/bin/env bash
# Проба: перезагрузка сервера в окне ротации (карточка hr.reboot-in-rotation-window,
# спека docs/specs/2026-11-02-перезагрузка-в-окне-ротации.md, итерация 2).
#
# Слово владельца 27.10 (входящее 9705): «…не прерывать работу резко, если
# работает агент, то дожидаться его смены сессии, точнее делать это в момент
# смены сессии». 02.11 на вопрос «в момент смены сессии и только когда
# помощники не работают?» — «Да» (подтверждение 10558).
#
# Судится:
#   1. reboot_window_verdict — каждая ветка «нет» своей причиной и кодом 1;
#   2. reboot_for_rotation — доставленная весть → след и метка → последний
#      взгляд → команда; команда только из REBOOT_CMD;
#   3. способ_ротации с 7-м доводом «перезагрузка встала бы» (решение 1б, B-11);
#   4. место зова в rotate_full (внутри FORCE_ROTATE = 0, после ворот, до «/exit»);
#   5. проба-сторож: команд перезагрузки мимо $REBOOT_CMD в коде нет (B-4);
#   6. предикат stack_freshness.py --перезагрузка;
#   7. ключи harness.conf и каталог метки по умолчанию;
#   8. стенд: сторож целым прогоном (B-21).
# Функции берутся из ЖИВОГО файла сторожа (дверь мутаций HARNESS_SESSION_WARDEN),
# предикат — HARNESS_STACK_FRESHNESS, носитель проверки — HARNESS_BOOT_CHECK.
#
# У agent полный sudo без пароля (B-4): ошибка кода здесь = настоящая
# перезагрузка машины. Поэтому ПЕРВЫМ делом REBOOT_CMD ставится на заглушку во
# временном каталоге и сверяется; не туда — отказ до любого прогона. В PATH —
# заглушки systemctl/sudo/reboot/shutdown/poweroff/halt, пишущие зов и
# выходящие кодом 1.
#
# Имена переменных латиницей: bash не берёт кириллические идентификаторы.
set -uo pipefail
export LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

STAND=$(mktemp -d -t proba-restart-window-XXXXXX)
PREFIX="warden-srestart-"
PANE="$PREFIX$$"
# Свой сервер tmux на прогон: панель стенда на боевом сервере ворота выката
# читали как работу агента (11.11.2026). Обёртка первой в PATH ведёт туда и
# голый tmux сторожа; TMUX снят, иначе клиент пошёл бы по сокету из неё.
# Сокет — в каталоге стенда (TMUX_TMPDIR) и уходит вместе с ним.
REAL_TMUX="$(command -v tmux)"
SOCK_NAME="$PANE"
unset TMUX TMUX_PANE
export TMUX_TMPDIR="$STAND/tmux"
mkdir -p "$STAND/tmux-bin" "$TMUX_TMPDIR"
printf '#!/bin/sh\nexec "%s" -L "%s" "$@"\n' "$REAL_TMUX" "$SOCK_NAME" > "$STAND/tmux-bin/tmux"
chmod +x "$STAND/tmux-bin/tmux"
export PATH="$STAND/tmux-bin:$PATH"
# Проба личности сторожа (self-prompt-live.sh) со стенда ушла бы фоном к модели
# через claude-run.sh стенда, а её тревога легла бы в канал стенда посреди
# чужого случая. Стенду — пустая проба.
printf '#!/bin/sh\nexit 0\n' > "$STAND/self-prompt-live.sh"
export SELF_PROMPT_LIVE_SH="$STAND/self-prompt-live.sh"
# Прогон, убитый KILL-ом, ловушку не зовёт: панель сама гасит свой сервер,
# когда прогона или каталога стенда не стало. Сервер — по pid из $TMUX панели:
# сокет к тому мигу может уйти вместе с каталогом. Сторож отвязан двойным
# fork: потомок claude в панели сторож сессий читал бы как работу агента.
pane_cmd() {  # $1 = команда панели → она же со сторожем жизни стенда
    printf '( { while kill -0 %s && [ -d %q ]; do sleep 2; done; s=${TMUX#*,}; kill "${s%%%%,*}"; } >/dev/null 2>&1 & ); %s' \
        "$$" "$STAND" "$1"
}
LOCK_PID=""
WARDEN_PID=""
cleanup() {
    [ -n "$WARDEN_PID" ] && kill "$WARDEN_PID" 2>/dev/null
    [ -n "$LOCK_PID" ] && kill "$LOCK_PID" 2>/dev/null
    "$REAL_TMUX" -L "$SOCK_NAME" kill-server 2>/dev/null
    rm -rf "$STAND"
}
trap cleanup EXIT
# Обрыв таймаутом пробы мутаций или ворот — тоже выход с уборкой. SIGKILL
# ловушку минует: на него сторож и подставной агент стенда привязаны к жизни
# этого процесса сами (раздел 8).
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# ── 0. заглушка команды и опасных команд — ДО всего остального ─────────────
BIN="$STAND/bin"; mkdir -p "$BIN"
REAL_CALLS="$STAND/real-calls.log"; : > "$REAL_CALLS"
for cmd in systemctl sudo reboot shutdown poweroff halt; do
    printf '#!/bin/sh\necho "%s $*" >> "%s"\nexit 1\n' "$cmd" "$REAL_CALLS" > "$BIN/$cmd"
    chmod +x "$BIN/$cmd"
done
REBOOT_STUB="$STAND/reboot-stub.sh"
printf '#!/bin/sh\necho "заглушка общая $*" >> "%s"\nexit 1\n' "$REAL_CALLS" > "$REBOOT_STUB"
chmod +x "$REBOOT_STUB"
export REBOOT_CMD="$REBOOT_STUB"
stub_guard() {  # $1 = действующее значение REBOOT_CMD
    case "$1" in
        "$STAND"/*) [ -x "${1%% *}" ] && return 0 ;;
    esac
    echo "ОТКАЗ: REBOOT_CMD «$1» не заглушка во временном каталоге пробы — ничего не запускаю"
    exit 2
}
stub_guard "$REBOOT_CMD"
# Ядро машины на стенде — своё: 6.8.0-138 (как на машине 02.11).
printf '#!/bin/sh\ncase "$1" in -r) echo 6.8.0-138-generic ;; *) echo Linux ;; esac\n' > "$BIN/uname"
chmod +x "$BIN/uname"
export PATH="$BIN:$PATH"

WARDEN="${HARNESS_SESSION_WARDEN:-$HERE/../harness/demons/session-warden.sh}"
# В пакете демоны лежат рядом со scripts (harness/demons), в проекте — под harness/.
[ -f "$WARDEN" ] || WARDEN="$HERE/../demons/session-warden.sh"
[ -f "$WARDEN" ] || { echo "ПЛОХО: сторожа нет по пути раскладки — проверять нечего"; exit 1; }
FRESHNESS="${HARNESS_STACK_FRESHNESS:-$HERE/stack_freshness.py}"
BOOT_CHECK="${HARNESS_BOOT_CHECK:-$HERE/boot_check.py}"
CONF="$HERE/../harness/config/harness.conf"
[ -f "$CONF" ] || CONF="$HERE/../config/harness.conf"

yes_no() { if "$@"; then echo да; else echo нет; fi; }
body() { sed -n "/^$1() {/,/^}/p" "$WARDEN"; }

# ── 1. чистая функция решения ──────────────────────────────────────────────
VERDICT_FN="$STAND/verdict.sh"
body reboot_window_verdict > "$VERDICT_FN"
case_check "функция reboot_window_verdict вынута из файла сторожа" \
    "да" "$(yes_no test -s "$VERDICT_FN")"

verdict_text() {
    bash -c 'set -uo pipefail; source "$1"; shift; reboot_window_verdict "$@"' \
        _ "$VERDICT_FN" "$@" 2>&1
}
# «слово;код»: сторож смотрит на КОД, журнал и владелец — на СЛОВО.
verdict() {
    local out rc=0
    out=$(verdict_text "$@") || rc=$?
    printf '%s;%s\n' "${out%%:*}" "$rc"
}
# Доводы: 0 ключ, 1 соседи, 2 пауза, 3 act, 4 фоновые задачи, 5 помощников,
# 6 занята, 7 нужна, 8 часов с загрузки, 9 предел часов, 10 прошлая не сменила.
GOOD=(1 "neighbour-1 mariadb" 0 выход 0 0 0 1 72 24 0)
with() { VA=("${GOOD[@]}"); while [ $# -ge 2 ]; do VA[$1]="$2"; shift 2; done; }
v() { with "$@"; verdict "${VA[@]}"; }
# Причина «нет» обязана быть СВОЕЙ: перестановка веток иначе не видна.
vr() { local pattern="$1"; shift; with "$@"; yes_no grep -qi -- "$pattern" <<< "$(verdict_text "${VA[@]}")"; }

echo "── ЗДОРОВЫЙ СЛУЧАЙ: окно ротации, всё «да» — перезагрузить ──"
case_check "всё «да» — перезагрузить, код 0" "перезагрузить;0" "$(v)"
case_check "ровно на пределе часов (24 из 24) — можно" "перезагрузить;0" "$(v 8 24)"

echo "── КАЖДОЕ «НЕТ» — своей причиной ──"
case_check "ключ 0 — не перезагружать" "нет;1" "$(v 0 0)"
case_check "ключ 0 — причина называет ключ" "да" "$(vr AUTO_REBOOT_IN_ROTATION 0 0)"
case_check "ключ пуст — не перезагружать" "нет;1" "$(v 0 '')"
case_check "ключ «да» вместо 1 (опечатка) — не перезагружать" "нет;1" "$(v 0 да)"
case_check "соседи не названы (STACK_NEIGHBOURS пуст) — не перезагружать (B-14)" "нет;1" "$(v 1 '')"
case_check "пустые соседи — причина «соседи не названы владельцем»" "да" "$(vr 'соседи не названы' 1 '')"
case_check "пауза владельца — не перезагружать" "нет;1" "$(v 2 1)"
case_check "пауза — причина называет паузу владельца" "да" "$(vr 'пауза владельца' 2 1)"
# Ветка «пауза неизвестна» тоже говорит «пауза» и тоже отказывает: без этого случая
# снятая ветка «пауза владельца» не видна — отказ даёт соседняя строка.
case_check "пауза владельца — не «пауза неизвестна»" "нет" "$(vr 'неизвестна' 2 1)"
case_check "«принудительно» (агент ещё в ходу) — не перезагружать (B-2)" "нет;1" "$(v 3 принудительно)"
case_check "«принудительно» — причина называет ход" "да" "$(vr 'ходу' 3 принудительно)"
case_check "фоновая задача сессии жива — не перезагружать (B-2)" "нет;1" "$(v 4 1)"
case_check "фоновая задача — причина называет задачу" "да" "$(vr 'фонов' 4 1)"
case_check "идут 2 помощника — не перезагружать" "нет;1" "$(v 5 2)"
case_check "помощники — причина называет помощников и число" "да" \
    "$(with 5 2; yes_no grep -q 'помощ.*2' <<< "$(verdict_text "${VA[@]}")")"
case_check "помощники неизвестны («?») — не перезагружать" "нет;1" "$(v 5 '?')"
case_check "«?» помощников — причина называет помощников" "да" "$(vr помощ 5 '?')"
case_check "машина занята резервной копией — не перезагружать (B-3)" "нет;1" "$(v 6 'резервная копия')"
case_check "занята — причина называет, чем" "да" "$(vr 'занята.*резервная копия' 6 'резервная копия')"
case_check "занятость неизвестна («?») — не перезагружать" "нет;1" "$(v 6 '?')"
case_check "В-1 пауза неизвестна («?», сбой pause.py) — не перезагружать" "нет;1" "$(v 2 '?')"
case_check "В-1 «?» паузы — причина называет паузу неизвестной" "да" "$(vr 'пауз.*неизвест' 2 '?')"
case_check "reboot-required нет — не перезагружать" "нет;1" "$(v 7 0)"
case_check "не нужна — причина «не нужна»" "да" "$(vr 'не нужна' 7 0)"
case_check "нужна ли — неизвестно — не перезагружать" "нет;1" "$(v 7 '?')"
case_check "неизвестно — причина «неизвестно»" "да" "$(vr неизвестно 7 '?')"
case_check "загрузилась 3 ч назад при пределе 24 — не перезагружать (петля)" "нет;1" "$(v 8 3)"
case_check "недавняя загрузка — причина называет загрузку" "да" "$(vr загруж 8 3)"
case_check "часы с загрузки не число — не перезагружать" "нет;1" "$(v 8 '')"
case_check "прошлая перезагрузка не сменила ядро — не перезагружать" "нет;1" "$(v 10 1)"
case_check "прошлая не сменила — причина называет ядро" "да" "$(vr ядро 10 1)"
# Старшинство: выключенный ключ отвечает первым — иначе журнал сторожа каждую
# ротацию писал бы «идут помощники» про механизм, который выключен.
case_check "ключ 0 и помощники 2 — отвечает ключ" "да" "$(vr AUTO_REBOOT 0 0 5 2)"

# ── 2. действие ─────────────────────────────────────────────────────────────
GO_FN="$STAND/go.sh"
body reboot_for_rotation > "$GO_FN"
case_check "функция reboot_for_rotation вынута из файла сторожа" "да" "$(yes_no test -s "$GO_FN")"

# Последний взгляд с НАСТОЯЩИМ работа_на_паузе (В-1) и НАСТОЯЩИМ rotation_flag.py
# (R2, дверь HARNESS_ROTATION_FLAG); pause.py случая лежит в scripts стенда.
PAUSE_FN="$STAND/pause-fn.sh"
body работа_на_паузе > "$PAUSE_FN"
ROTATION_FLAG_PY="${HARNESS_ROTATION_FLAG:-$HERE/lib/rotation_flag.py}"
PAUSE_CRASH='raise RuntimeError("проба: pause.py упал")'
PAUSE_EXIT2='import sys
print("pause.py: не знаю довода", file=sys.stderr)
sys.exit(2)'

# $1 = имя случая, $2 = поведение канала, $3 = код заглушки команды,
# $4 = что сломать к последнему взгляду (none|pause|wake|helper|tasks|busy|stage|ro|nocmd|
#      shift|shift-tasks|pause-real|pause-crash|pause-exit2|pause-missing|flag-fresh|flag-stale).
# shift*: смена — не самый свежий транскрипт, SHIFT_SESSION_ID ≠ SESSION_ID (В-3).
# Печатает код функции; следы — в $STAND/$1.
run_go() {
    local d="$STAND/$1" look="${4:-none}"
    local state="$d/panel-state" shift_sid=proba-sessiya pause_fn=""
    mkdir -p "$state" "$d/scripts/lib"
    cp "$HERE"/lib/*.py "$d/scripts/lib/"
    : > "$d/msgs"; : > "$d/args"; : > "$d/jlog"; : > "$d/calls"
    echo 600 > "$d/idle"; echo идёт > "$d/stage"; echo proba-sessiya > "$d/flag-sid"
    case "$look" in
        helper) : > "$d/helper" ;;
        tasks)  : > "$d/tasks" ;;
        busy)   : > "$d/busy" ;;
        stage)  echo ждёт > "$d/stage" ;;
        ro)     chmod 555 "$state" ;;
        shift)  shift_sid=proba-smena; echo proba-smena > "$d/flag-sid" ;;
        shift-tasks) shift_sid=proba-smena; echo proba-smena > "$d/flag-sid"; : > "$d/tasks-proba-smena" ;;
        pause-real)  pause_fn="$PAUSE_FN"; cp "$HERE/pause.py" "$d/scripts/pause.py" ;;
        pause-crash) pause_fn="$PAUSE_FN"; printf '%s\n' "$PAUSE_CRASH" > "$d/scripts/pause.py" ;;
        pause-exit2) pause_fn="$PAUSE_FN"; printf '%s\n' "$PAUSE_EXIT2" > "$d/scripts/pause.py" ;;
        pause-missing) pause_fn="$PAUSE_FN"; rm -f "$d/scripts/pause.py" ;;
    esac
    # Заглушка команды записывает, ЧТО уже было к её зову: число вестей, метку,
    # строку журнала. По этой строке и судится порядок.
    cat > "$d/reboot-stub.sh" <<EOF
#!/bin/sh
printf 'вестей=%s метка=%s журнал=%s\n' "\$(wc -l < "$d/msgs" | tr -d ' ')" \
    "\$([ -f "$state/reboot-started.json" ] && echo да || echo нет)" \
    "\$(grep -c reboot_started "$d/jlog")" >> "$d/calls"
exit $3
EOF
    chmod +x "$d/reboot-stub.sh"
    local cmd="$d/reboot-stub.sh"
    [ "$look" = nocmd ] && cmd=""
    [ -z "$cmd" ] || stub_guard "$cmd"
    # Соседи последнего взгляда, которых сторож зовёт файлами: занятость машины
    # и флаг части А.
    cat > "$d/scripts/boot_check.py" <<EOF
import os, sys
if "--занято" in sys.argv:
    if os.path.exists("$d/busy"):
        print("занята: резервная копия"); sys.exit(1)
    print("свободна"); sys.exit(0)
sys.exit(2)
EOF
    cat > "$d/scripts/lib/rotation_flag.py" <<EOF
import json, sys
if "--state" in sys.argv:
    print(json.dumps({"сессия": open("$d/flag-sid", encoding="utf-8").read().strip(),
                      "этап": open("$d/stage", encoding="utf-8").read().strip()},
                     ensure_ascii=False))
EOF
    case "$look" in
        flag-fresh|flag-stale)
            cp "$ROTATION_FLAG_PY" "$d/scripts/lib/rotation_flag.py"
            printf '{"сессия": "proba-sessiya", "этап": "идёт"}\n' > "$state/rotation-waits-helpers.json"
            [ "$look" = flag-fresh ] || touch -d '-2 hours' "$state/rotation-waits-helpers.json" ;;
    esac
    local rc=0
    D="$d" MODE="$2" HARNESS_PANEL_STATE="$state" PANEL_STATE_DIR="$state" STATE_DIR="$d/no-state" \
    LOG_DIR="$d" HARNESS_SCRIPTS="$d/scripts" PROJECT_DIR="$d" REBOOT_CMD="$cmd" \
    STACK_NEIGHBOURS="neighbour-1 mariadb" SESSION_ID="proba-sessiya" SHIFT_SESSION_ID="$shift_sid" \
    REBOOT_EXPECT_MIN=3 REBOOT_LATE_MIN=15 ROTATE_IDLE_SEC=120 ROTATE_FLAG_STALE_MIN=30 \
    REASON="окно ≈80% ≥ 75%" TG_SEND_HELD=0 PAUSE_FN="$pause_fn" \
    HARNESS_INSTALL_CONF="$d/no-install.conf" HARNESS_CONF="$d/no-harness.conf" \
        bash -c 'set -euo pipefail
            source "$1"
            say() { :; }
            jlog() { printf "%s\n" "$*" >> "$D/jlog"; }
            работа_на_паузе() { [ -f "$D/pause" ]; }
            [ -z "$PAUSE_FN" ] || source "$PAUSE_FN"
            простой_панели() { cat "$D/idle"; }
            rotation_helpers_gate() { [ ! -f "$D/helper" ]; }
            фоновые_задачи_живы() { [ -f "$D/tasks" ] || [ -f "$D/tasks-${1:-}" ]; }
            # Дверь канала: «ok» доставила; «fail» отказала; «held» придержала
            # с кодом 3; «limit» — держится лимит: с --несмотря-на-лимит уходит,
            # с --не-досылать придерживается (код 3), без ключей ЛОЖИТСЯ В
            # ОЧЕРЕДЬ ДОСЫЛА с кодом 0 (tg_send.sh:489-493) — недоставленной.
            # «wake» — владелец ответил, панель ожила; «pause» — встала пауза.
            tg_send() {
                local text="$1"; shift || true
                printf "%s\n" "$text" | tr "\n" " " >> "$D/msgs"; echo >> "$D/msgs"
                printf "%s\n" "$*" >> "$D/args"
                TG_SEND_HELD=0
                case "$MODE" in
                    fail) return 1 ;;
                    held) TG_SEND_HELD=1; return 0 ;;
                    limit)
                        case " $* " in
                            *" --несмотря-на-лимит "*) : > "$D/delivered" ;;
                            *" --не-досылать "*) TG_SEND_HELD=1 ;;
                            *) : > "$D/queued" ;;
                        esac
                        return 0 ;;
                    wake) echo 0 > "$D/idle" ;;
                    pause) : > "$D/pause" ;;
                esac
                : > "$D/delivered"
                return 0
            }
            reboot_for_rotation 6.8.0-147 "$REASON"' _ "$GO_FN" > "$d/out" 2>&1 || rc=$?
    chmod 755 "$state" 2>/dev/null
    echo "$rc"
}
marker_field() {  # $1 = файл метки, $2 = поле
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2], "НЕТ ПОЛЯ"))' \
        "$1" "$2" 2>/dev/null || echo "метки нет"
}
calls_of() { wc -l < "$STAND/$1/calls" | tr -d ' '; }
mark_of() { yes_no test -f "$STAND/$1/panel-state/reboot-started.json"; }

echo "── ПОРЯДОК: доставленная весть → след → метка → команда ──"
rc=$(run_go good ok 0)
case_check "команда принята — код 0" "0" "$rc"
case_check "заглушка REBOOT_CMD вызвана ровно один раз" "1" "$(calls_of good)"
case_check "к зову команды: весть ушла, след в журнале, метка стоит" \
    "вестей=1 метка=да журнал=1" "$(head -1 "$STAND/good/calls")"
MARK="$STAND/good/panel-state/reboot-started.json"
case_check "метка в PANEL_STATE_DIR (B-1), не в STATE_DIR" "да" "$(yes_no test -f "$MARK")"
case_check "в STATE_DIR ничего не писалось" "нет" "$(yes_no test -e "$STAND/good/no-state")"
case_check "метка: ожидаемое ядро" "6.8.0-147" "$(marker_field "$MARK" ядро_ожидаемое)"
case_check "метка: ядро было (uname -r)" "6.8.0-138-generic" "$(marker_field "$MARK" ядро_было)"
case_check "метка: сессия" "proba-sessiya" "$(marker_field "$MARK" сессия)"
case_check "метка: причина ротации" "окно ≈80% ≥ 75%" "$(marker_field "$MARK" причина)"
case_check "метка: время — число секунд" "да" "$(yes_no grep -qE '^[0-9]{10}$' <<< "$(marker_field "$MARK" с)")"
ARGS=$(head -1 "$STAND/good/args")
case_check "весть с ключом --несмотря-на-лимит (B-18)" "да" "$(yes_no grep -q -- '--несмотря-на-лимит' <<< "$ARGS")"
case_check "весть с ключом --не-досылать: придержка — код 3, а не очередь (B-18)" "да" \
    "$(yes_no grep -q -- '--не-досылать' <<< "$ARGS")"
MSG=$(head -1 "$STAND/good/msgs")
case_check "весть называет соседа neighbour-1" "да" "$(yes_no grep -q 'neighbour-1' <<< "$MSG")"
case_check "весть называет соседа mariadb" "да" "$(yes_no grep -q 'mariadb' <<< "$MSG")"
case_check "весть говорит, сколько примерно (3 мин)" "да" "$(yes_no grep -q '3 мин' <<< "$MSG")"
case_check "весть даёт правило молчания «20 минут» (B-6)" "да" "$(yes_no grep -q '20 мин' <<< "$MSG")"
case_check "весть не длиннее 700 знаков (TONE.md, B-13)" "да" \
    "$([ -n "$MSG" ] && [ "${#MSG}" -le 700 ] && echo да || echo "нет (${#MSG})")"
case_check "в вести нет команд и жаргона (sudo, systemctl, tmux, юнит, uname)" "" \
    "$(grep -oiE 'sudo|systemctl|tmux|юнит|uname' <<< "$MSG" | sort -u | tr '\n' ' ')"

echo "── ВЛАДЕЛЕЦ НЕ УЗНАЛ — НЕ ПЕРЕЗАГРУЖАЕМ ──"
rc=$(run_go fail fail 0)
case_check "весть не ушла — код 1, команды нет, метки нет" "1 0 нет" "$rc $(calls_of fail) $(mark_of fail)"
rc=$(run_go held held 0)
case_check "весть придержана дверью — код 1, команды нет, метки нет" "1 0 нет" "$rc $(calls_of held) $(mark_of held)"
rc=$(run_go limit limit 0)
# Под лимитом весть без ключей легла бы в очередь кодом 0 — и сторож перезагрузил
# бы, а владелец не узнал. Верно только одно: весть доставлена и команда одна.
case_check "лимит: весть доставлена, в очереди не лежит, команда одна" \
    "доставлена=да очередь=нет команда=1" \
    "доставлена=$(yes_no test -f "$STAND/limit/delivered") очередь=$(yes_no test -f "$STAND/limit/queued") команда=$(calls_of limit)"

echo "── КОМАНДА НЕ ЗАДАНА — НИЧЕГО (B-4) ──"
rc=$(run_go nocmd ok 0 nocmd)
case_check "REBOOT_CMD пуст — код 2" "2" "$rc"
case_check "REBOOT_CMD пуст — ни вести, ни метки" "0 нет" "$(wc -l < "$STAND/nocmd/msgs" | tr -d ' ') $(mark_of nocmd)"

echo "── МЕТКА НЕ ЗАПИСАЛАСЬ — НЕ ПЕРЕЗАГРУЖАЕМ (B-1) ──"
rc=$(run_go ro ok 0 ro)
case_check "PANEL_STATE_DIR только на чтение — код 1, команды нет" "1 0" "$rc $(calls_of ro)"

echo "── ПОСЛЕДНИЙ ВЗГЛЯД ПЕРЕД КОМАНДОЙ (B-10) ──"
rc=$(run_go wake wake 0)
case_check "владелец ответил на весть, панель ожила — код 1, команды нет" "1 0" "$rc $(calls_of wake)"
rc=$(run_go pause pause 0)
case_check "за время вести встала пауза — код 1, команды нет" "1 0" "$rc $(calls_of pause)"
# Исход тот же и у ветки «пауза неизвестна»: различает их только причина в журнале.
case_check "за время вести встала пауза — reboot_skipped называет паузу владельца" "да" \
    "$(yes_no grep -q 'reboot_skipped.*пауза владельца' "$STAND/pause/jlog")"
rc=$(run_go helper ok 0 helper)
case_check "ворота помощников закрыты — код 1, команды нет" "1 0" "$rc $(calls_of helper)"
rc=$(run_go tasks ok 0 tasks)
case_check "фоновая задача сессии ожила — код 1, команды нет" "1 0" "$rc $(calls_of tasks)"
rc=$(run_go busy ok 0 busy)
case_check "машина занята — код 1, команды нет" "1 0" "$rc $(calls_of busy)"
rc=$(run_go stage ok 0 stage)
case_check "флаг части А не «идёт» — код 1, команды нет" "1 0" "$rc $(calls_of stage)"
case_check "отказ последнего взгляда — в журнале reboot_skipped" "да" \
    "$(yes_no grep -q reboot_skipped "$STAND/stage/jlog")"

echo "── В-1 СБОЙ pause.py В ПОСЛЕДНЕМ ВЗГЛЯДЕ — ПАУЗА НЕИЗВЕСТНА, НЕ ПЕРЕЗАГРУЖАЕМ ──"
rc=$(run_go pause-real ok 0 pause-real)
case_check "настоящий pause.py, паузы нет — команда звана, код 0 (обычный путь цел)" "0 1" "$rc $(calls_of pause-real)"
for kind in crash exit2 missing; do
    rc=$(run_go "pause-$kind" ok 0 "pause-$kind")
    case_check "В-1 pause.py ($kind) — код 1, команды нет" "1 0" "$rc $(calls_of "pause-$kind")"
    case_check "В-1 pause.py ($kind) — reboot_skipped называет паузу неизвестной" "да" \
        "$(yes_no grep -qE 'reboot_skipped.*пауз.*неизвест' "$STAND/pause-$kind/jlog")"
done

echo "── В-3 СМЕНА — НЕ САМЫЙ СВЕЖИЙ ТРАНСКРИПТ: флаг и задачи по SHIFT_SESSION_ID ──"
rc=$(run_go shift ok 0 shift)
case_check "В-3 флаг смены (сессия = SHIFT_SESSION_ID) «идёт» — команда звана, код 0" "0 1" "$rc $(calls_of shift)"
rc=$(run_go shift-tasks ok 0 shift-tasks)
case_check "В-3 жива фоновая задача смены — код 1, команды нет" "1 0" "$rc $(calls_of shift-tasks)"
case_check "В-3 отказ называет фоновую задачу, а не флаг" "да" \
    "$(yes_no grep -q 'reboot_skipped.*фонов' "$STAND/shift-tasks/jlog")"

echo "── R2 ПРОТУХШИЙ ФЛАГ РОТАЦИИ — НЕ «ИДЁТ» (настоящий rotation_flag.py --state) ──"
rc=$(run_go flag-fresh ok 0 flag-fresh)
case_check "R2 живой флаг — команда звана, код 0" "0 1" "$rc $(calls_of flag-fresh)"
rc=$(run_go flag-stale ok 0 flag-stale)
case_check "R2 флаг протух (2 ч при ROTATE_FLAG_STALE_MIN=30) — код 1, команды нет" "1 0" "$rc $(calls_of flag-stale)"

echo "── КОМАНДА УПАЛА — МЕТКА ЦЕЛА, В НЕЙ КОД (B-7) ──"
rc=$(run_go cmdfail ok 5)
case_check "команда упала — код 2" "2" "$rc"
case_check "команда упала — звана один раз" "1" "$(calls_of cmdfail)"
case_check "команда упала — метка цела (правду скажет время загрузки)" "да" "$(mark_of cmdfail)"
case_check "команда упала — в метке команда_код 5" "5" \
    "$(marker_field "$STAND/cmdfail/panel-state/reboot-started.json" команда_код)"
case_check "команда упала — второй вести нет (говорит sentinel по метке)" "1" \
    "$(wc -l < "$STAND/cmdfail/msgs" | tr -d ' ')"

# Вторая весть — следующая ротация: метку со сломанной командой boot_check
# закрывает итогом «не состоялась» (ядро_сменилось: null), а сторож читает его
# как «прошлая не мешает» и снова шлёт «перезагружаю» — и так каждую ротацию.
echo "── В-5 СЛОМАННАЯ КОМАНДА: следующая ротация весть не повторяет ──"
rc=$(run_go brokencmd ok 5)
BC_STATE="$STAND/brokencmd/panel-state"
: > "$STAND/bc-empty.conf"; mkdir -p "$STAND/bc-home"
BC_OUT=$(env -u HARNESS_LIVE_CHECK_TIMEOUT HARNESS_HOME="$STAND/bc-home" \
    HARNESS_INSTALL_CONF="$STAND/bc-empty.conf" HARNESS_CONF="$STAND/bc-empty.conf" \
    HARNESS_PANEL_STATE="$BC_STATE" REBOOT_LATE_MIN=0 HARNESS_BOOT_CHECK_BOOT_TIME=$(( $(date +%s) - 7200 )) \
    python3 "$BOOT_CHECK" --метка "$BC_STATE/reboot-started.json" 2>&1)
case_check "В-5 boot_check по метке со сломанной командой — «не состоялась»" "да" \
    "$(yes_no grep -q 'не состоялась' <<< "$BC_OUT")"
LAST_FN="$STAND/last-failed.sh"
body reboot_last_failed > "$LAST_FN"
BC_LAST=$(ROTATION_STATE_DIR="$BC_STATE" bash -c 'source "$1"; reboot_last_failed 6.8.0-147' _ "$LAST_FN" 2>&1)
case_check "В-5 итог «не состоялась» с кодом команды — прошлая мешает (1)" "1" "$BC_LAST"
SECOND=0
with 10 "$BC_LAST"
if verdict_text "${VA[@]}" > /dev/null; then
    run_go brokencmd2 ok 5 > /dev/null
    SECOND=$(wc -l < "$STAND/brokencmd2/msgs" | tr -d ' ')
fi
case_check "В-5 сломанная команда: за две ротации от сторожа одна весть владельцу" "1" \
    "$(( $(wc -l < "$STAND/brokencmd/msgs" | tr -d ' ') + SECOND ))"

# ── 3. способ ротации: решение 1б с B-11 ────────────────────────────────────
METHOD_FN="$STAND/method.sh"
body способ_ротации > "$METHOD_FN"
method() { bash -c 'source "$1"; shift; способ_ротации "$@"' _ "$METHOD_FN" "$@" 2>&1; }
echo "── СПОСОБ РОТАЦИИ: 7-й довод «перезагрузка встала бы» ──"
case_check "перезагрузка встала бы — тяжёлый путь" "full" "$(method 0 0 1 24 0 0 1)"
case_check "перезагрузка не встала бы — быстрый, как раньше" "clear" "$(method 0 0 1 24 0 0 0)"
case_check "без 7-го довода — быстрый, как раньше" "clear" "$(method 0 0 1 24 0 0)"
case_check "сутки без полного — тяжёлый, как раньше" "full" "$(method 0 0 30 24 0 0 0)"

# ── 4. место зова в пути ротации ────────────────────────────────────────────
BODY="$STAND/rotate_full.sh"
body rotate_full > "$BODY"
case_check "тело rotate_full вынуто" "да" "$(yes_no test -s "$BODY")"
line_of() {  # $1 = образец, $2 = искать после строки (0 — с начала); строки-комментарии мимо
    awk -v pat="$1" -v after="${2:-0}" \
        'NR > after && index($0, pat) && $0 !~ /^[[:space:]]*#/ { print NR; exit }' "$BODY"
}
order() {  # $1 < $2 → да
    if [ -n "$1" ] && [ -n "$2" ] && [ "$1" -lt "$2" ]; then echo да
    else echo "нет (${1:-нет} против ${2:-нет})"; fi
}
COND=$(line_of 'if [ "$exit_sent" = 0 ] && [ "$FORCE_ROTATE" = 0 ]')
COND_END=""
[ -n "$COND" ] && COND_END=$(awk -v from="$COND" -v ind="$(sed -n "${COND}p" "$BODY" | sed -E 's/^([[:space:]]*).*/\1/')" \
    'NR > from && $0 == ind "fi" { print NR; exit }' "$BODY")
DOOR=$(line_of 'дверь_ротации "$ordered_at"')
GATE=$(line_of 'rotation_helpers_gate' "${DOOR:-0}")
PAUSE=$(line_of 'работа_на_паузе' "${DOOR:-0}")
TASKS=$(line_of 'фоновые_задачи_живы' "${DOOR:-0}")
BUSY=$(line_of '--занято' "${DOOR:-0}")
VERDICT=$(line_of 'reboot_window_verdict ')
GO=$(line_of 'reboot_for_rotation ')
EXIT=$(line_of 'agent_rotation_prompt "/exit"' "${DOOR:-0}")
case_check "решение о перезагрузке — ПОСЛЕ двери передачи" "да" "$(order "$DOOR" "$VERDICT")"
case_check "помощники спрошены воротами части А после двери и до решения (B-9)" "да" "$(order "$GATE" "$VERDICT")"
case_check "пауза спрошена заново после двери и до решения" "да" "$(order "$PAUSE" "$VERDICT")"
case_check "фоновые задачи спрошены после двери и до решения (B-2)" "да" "$(order "$TASKS" "$VERDICT")"
case_check "занятость машины спрошена после двери и до решения (B-3)" "да" "$(order "$BUSY" "$VERDICT")"
case_check "решению передан act (выход/принудительно, B-2)" "да" \
    "$(yes_no grep -q '"$act"' <<< "$(sed -n "${VERDICT:-0}p" "$BODY")")"
case_check "действие — после решения" "да" "$(order "$VERDICT" "$GO")"
case_check "действие — ДО «/exit» (перезагрузка заменяет выход)" "да" "$(order "$GO" "$EXIT")"
case_check "действие — внутри условия FORCE_ROTATE = 0 (на --rotate-now нет, B-12)" "да" \
    "$([ -n "$COND" ] && [ -n "$COND_END" ] && [ -n "$GO" ] && [ "$COND" -lt "$GO" ] && [ "$GO" -lt "$COND_END" ] && echo да || echo нет)"
case_check "результат решения и действия не глотается (|| true)" "0" \
    "$(sed -n "${VERDICT:-0}p;${GO:-0}p" "$BODY" | grep -c '|| true')"
case_check "сторож спрашивает предикат stack_freshness.py --перезагрузка" "да" \
    "$(yes_no grep -qE 'stack_freshness\.py"? --перезагрузка' "$WARDEN")"

# ── 5. проба-сторож: команда перезагрузки — только $REBOOT_CMD (B-4) ─────────
echo "── ПРОБА-СТОРОЖ: в коде пути перезагрузки нет команд мимо \$REBOOT_CMD ──"
GO_BODY=$(cat "$GO_FN")
case_check "reboot_for_rotation зовёт \$REBOOT_CMD" "да" "$(yes_no grep -q 'REBOOT_CMD' <<< "$GO_BODY")"
case_check "у REBOOT_CMD нет умолчания в коде (\${REBOOT_CMD:-…})" "0" \
    "$(grep -cE '\$\{REBOOT_CMD:?[-=][^}]' "$WARDEN")"
forbidden_in() {  # stdin — текст кода без строк-комментариев → найденные запреты
    grep -v -E '^[[:space:]]*#' | grep -noE \
        '(^|[^A-Za-z0-9_$.-])(sudo[[:space:]]+(-[a-z]+[[:space:]]+)*)?(/[A-Za-z/]*/)?(reboot|shutdown|poweroff|halt)([[:space:]]|$|["'"'"';)])|systemctl[^|;&#]*[[:space:]"'"'"'](reboot|poweroff|halt|kexec)|/(usr/)?s?bin/(systemctl|sudo|reboot|shutdown|poweroff|halt)' \
        | tr '\n' ' '
}
case_check "reboot_window_verdict: команд перезагрузки нет" "" "$(forbidden_in < "$VERDICT_FN")"
case_check "reboot_for_rotation: команд перезагрузки мимо \$REBOOT_CMD нет" "" "$(forbidden_in <<< "$GO_BODY")"
case_check "rotate_full: команд перезагрузки нет" "" "$(forbidden_in < "$BODY")"
case_check "носитель boot_check.py на месте" "да" "$(yes_no test -f "$BOOT_CHECK")"
case_check "boot_check.py: команд перезагрузки нет" "" "$([ -f "$BOOT_CHECK" ] && forbidden_in < "$BOOT_CHECK")"
# Живость стража: подложенный вызов он обязан увидеть.
case_check "страж видит подложенное «sudo -n systemctl reboot»" "да" \
    "$([ -n "$(forbidden_in <<< '    sudo -n systemctl reboot')" ] && echo да || echo нет)"
case_check "страж видит подложенное /sbin/shutdown -r now" "да" \
    "$([ -n "$(forbidden_in <<< '    /sbin/shutdown -r now')" ] && echo да || echo нет)"
case_check "страж не путает имена reboot_started / reboot-last.json с командой" "" \
    "$(forbidden_in <<< '    jlog "reboot_started" x; cat "$D/reboot-last.json"')"

# ── 6. предикат «нужна перезагрузка» ────────────────────────────────────────
echo "── ПРЕДИКАТ stack_freshness.py --перезагрузка ──"
FR="$STAND/fr"
mkdir -p "$FR/home" "$FR/log" "$FR/state" "$FR/boot"
printf 'LOG_DIR="%s"\nSTATE_DIR="%s"\nPROJECT_DIR="%s"\n' "$FR/log" "$FR/state" "$FR" > "$FR/install.conf"
: > "$FR/harness.conf"
predicate_raw() {  # $1 = файл reboot-required
    HARNESS_HOME="$FR/home" HARNESS_INSTALL_CONF="$FR/install.conf" HARNESS_CONF="$FR/harness.conf" \
    LOG_DIR="$FR/log" STATE_DIR="$FR/state" \
    HARNESS_STACK_BOOT_DIR="$FR/boot" HARNESS_STACK_REBOOT_FILE="$1" \
        python3 "$FRESHNESS" --перезагрузка 2>&1
}
predicate() {  # «слово;код» по последней строке
    local out rc=0
    out=$(predicate_raw "$1") || rc=$?
    out=$(tail -1 <<< "$out")
    printf '%s;%s\n' "${out%%:*}" "$rc"
}
case_check "в /boot ядер нет — неизвестно, код 3" "неизвестно;3" "$(predicate "$FR/no-file")"
: > "$FR/boot/vmlinuz-6.8.0-138-generic"
case_check "ядро то же, reboot-required нет — не нужна, код 1" "не нужна;1" "$(predicate "$FR/no-file")"
: > "$FR/reboot-required"
case_check "ядро то же, reboot-required есть — нужна, код 0" "нужна;0" "$(predicate "$FR/reboot-required")"
: > "$FR/boot/vmlinuz-6.8.0-147-generic"
case_check "в /boot новее (6.8.0-147) — нужна, код 0" "нужна;0" "$(predicate "$FR/no-file")"
case_check "нужна — называет ожидаемое ядро" "нужна: 6.8.0-147" "$(predicate_raw "$FR/no-file" | tail -1)"

# Тот же предикат глазами сторожа: reboot_needed переводит код в «нужна 1|0|?».
echo "── W25 СТОРОЖ ЧИТАЕТ ПРЕДИКАТ (reboot_needed) ──"
NEED_FN="$STAND/need.sh"
body reboot_needed > "$NEED_FN"
NW="$STAND/need"; mkdir -p "$NW"
cat > "$NW/stack_freshness.py" <<EOF
import sys
print(open("$NW/out", encoding="utf-8").read().strip())
sys.exit(int(open("$NW/rc", encoding="utf-8").read()))
EOF
need_of() {  # $1 = код предиката, $2 = его печать → что прочёл сторож
    printf '%s\n' "$2" > "$NW/out"; echo "$1" > "$NW/rc"
    HARNESS_SCRIPTS="$NW" bash -c 'source "$1"; reboot_needed' _ "$NEED_FN" 2>&1
}
case_check "W25 «нужна: 6.8.0-147», код 0 — сторож читает «1 6.8.0-147»" "1 6.8.0-147" "$(need_of 0 'нужна: 6.8.0-147')"
case_check "W25 «не нужна», код 1 — сторож читает «0 -», не «нужна»" "0 -" "$(need_of 1 'не нужна')"
case_check "W25 «неизвестно», код 3 — сторож читает «? -»" "? -" "$(need_of 3 'неизвестно: в /boot ядер нет')"
case_check "W25 предикат вышел кодом 2 — сторож читает «? -»" "? -" "$(need_of 2 '')"

# 7-й довод способ_ротации спрашивает паузу сам (B-11): сбой pause.py там тоже «нет».
echo "── В-1 7-Й ДОВОД «ПЕРЕЗАГРУЗКА ВСТАЛА БЫ»: сбой pause.py ──"
FIT_FN="$STAND/fit.sh"
for fn in reboot_switch reboot_would_fit reboot_needed reboot_last_failed reboot_window_verdict работа_на_паузе; do
    body "$fn"
done > "$FIT_FN"
FW="$STAND/fit"; mkdir -p "$FW/scripts/lib" "$FW/state"
cp "$HERE"/lib/*.py "$FW/scripts/lib/"
printf 'import sys\nprint("свободна")\n' > "$FW/scripts/boot_check.py"
printf 'import sys\nprint("нужна: 6.8.0-147")\n' > "$FW/scripts/stack_freshness.py"
fit() {
    HARNESS_SCRIPTS="$FW/scripts" HARNESS_PANEL_STATE="$FW/state" ROTATION_STATE_DIR="$FW/state" \
    HARNESS_INSTALL_CONF="$FW/no-install.conf" HARNESS_CONF="$FW/no-harness.conf" \
    AUTO_REBOOT_IN_ROTATION=1 STACK_NEIGHBOURS="сайт" REBOOT_MIN_GAP_HOURS=0 \
        bash -c 'source "$1"; reboot_would_fit' _ "$FIT_FN" 2>&1
}
cp "$HERE/pause.py" "$FW/scripts/pause.py"
case_check "7-й довод: настоящий pause.py, паузы нет — встала бы (1)" "1" "$(fit)"
printf '%s\n' "$PAUSE_CRASH" > "$FW/scripts/pause.py"
case_check "В-1 7-й довод: pause.py упал — не встала бы (0)" "0" "$(fit)"
printf '%s\n' "$PAUSE_EXIT2" > "$FW/scripts/pause.py"
case_check "В-1 7-й довод: pause.py вышел кодом 2 — не встала бы (0)" "0" "$(fit)"
# Р-3: без команды перезагрузки не будет, а «встала бы» уводит ротацию в тяжёлый путь.
cp "$HERE/pause.py" "$FW/scripts/pause.py"
case_check "Р-3 7-й довод: REBOOT_CMD пуст — не встала бы (0)" "0" "$(REBOOT_CMD="" fit)"

# ── 7. ключи и каталог метки ────────────────────────────────────────────────
conf_value() { sed -n "s/^$1=//p" "$CONF" | tail -1 | sed 's/[[:space:]]*#.*//' | tr -d '"'; }
number() { yes_no grep -qE '^[1-9][0-9]*$' <<< "$1"; }
case_check "AUTO_REBOOT_IN_ROTATION=0 по умолчанию (включает оркестратор)" "0" "$(conf_value AUTO_REBOOT_IN_ROTATION)"
case_check "REBOOT_CMD — единственное значение команды, в harness.conf" "sudo -n systemctl reboot" "$(conf_value REBOOT_CMD)"
case_check "REBOOT_MIN_GAP_HOURS задан числом" "да" "$(number "$(conf_value REBOOT_MIN_GAP_HOURS)")"
# Р-5: пакет публичен — соседи этой машины живут только в живом конфиге; пусто
# на чужой установке держит «соседи не названы — не перезагружать» (B-14).
case_check "Р-5 соседи: STACK_NEIGHBOURS в репозитории пуст" "" "$(conf_value STACK_NEIGHBOURS)"
case_check "REBOOT_WAIT_SEC задан числом" "да" "$(number "$(conf_value REBOOT_WAIT_SEC)")"
case_check "REBOOT_LATE_MIN задан числом (B-6)" "да" "$(number "$(conf_value REBOOT_LATE_MIN)")"
case_check "REBOOT_CHECK_TIMEOUT_SEC больше 300 с ожидания ответа (B-8)" "да" \
    "$(v=$(conf_value REBOOT_CHECK_TIMEOUT_SEC); [[ "$v" =~ ^[0-9]+$ ]] && [ "$v" -gt 360 ] && echo да || echo нет)"
# Каталог метки по умолчанию — тот же, что у флага части А, и в него пишет agent.
DEFAULT_DIR=$(env -u HARNESS_PANEL_STATE python3 -c \
    'import sys; sys.path.insert(0, sys.argv[1]); import config; print(config.panel_state_dir())' "$HERE/lib" 2>/dev/null)
case_check "каталог метки по умолчанию (config.panel_state_dir) пишет $(id -un)" "да" \
    "$([ -n "$DEFAULT_DIR" ] && python3 -c 'import os,sys; sys.exit(0 if os.access(sys.argv[1], os.W_OK) else 1)' "$DEFAULT_DIR" && echo да || echo "нет ($DEFAULT_DIR)")"
case_check "каталог метки переживает перезагрузку (не /run, не /tmp)" "да" \
    "$(case "$DEFAULT_DIR" in ''|/run/*|/tmp/*|/var/run/*) echo "нет ($DEFAULT_DIR)" ;; *) echo да ;; esac)"

# ── 8. стенд: сторож целым прогоном (B-21) ──────────────────────────────────
# Свой LOG_DIR, свой panel-state, своя tmux-панель с подставным «агентом»
# (копия cat под именем claude пишет присланное в файл), свой канал-файл.
# Условие «нужна ротация» — pre_compact этой сессии при MAX_COMPACTS=1; метки
# полного перезапуска нет — путь тяжёлый. Судья передачи стенда: «написана».
echo "── СТЕНД: сторож целым прогоном до «/exit» ──"
T="$STAND/a"
mkdir -p "$T/project/scripts/lib" "$T/projects" "$T/bin"
cp /bin/cat "$T/claude"
for f in work-queue.py limit_mark.py limit-in-log.py stall-in-log.py pause.py; do
    cp "$HERE/$f" "$T/project/scripts/" 2>/dev/null
done
[ -f "$BOOT_CHECK" ] && cp "$BOOT_CHECK" "$T/project/scripts/boot_check.py"
cp "$HERE"/lib/*.py "$HERE/lib/config.sh" "$HERE/lib/pane.sh" "$HERE/lib/events.sh" "$HERE/lib/agent_pane_log.sh" \
    "$T/project/scripts/lib/"
# Сторож вычисляет каталог скриптов от СВОЕГО пути: запущенный с места он судил
# бы живые pause.py/stack_freshness.py, а не заглушки стенда. Копия — в раскладке
# стенда, с двери мутаций.
STAND_WARDEN="$T/project/harness/demons/session-warden.sh"
mkdir -p "$(dirname "$STAND_WARDEN")"
cp "$WARDEN" "$STAND_WARDEN"
cat > "$T/project/scripts/tg_send.sh" <<KANAL
#!/usr/bin/env bash
python3 -c 'import json,sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))' "\$@" >> "$T/kanal.jsonl"
KANAL
printf '#!/usr/bin/env bash\necho "передача стенда: написана"\nexit 0\n' > "$T/project/scripts/handover-check.sh"
# Предикат стенда: ядро ждёт всегда (сам предикат судит раздел 6).
printf 'import sys\nif "--перезагрузка" in sys.argv:\n    print("нужна: 6.8.0-147"); sys.exit(0)\nsys.exit(1)\n' \
    > "$T/project/scripts/stack_freshness.py"
printf '#!/usr/bin/env bash\necho отвязан >> "%s"\n' "$T/detached.txt" > "$T/bin/setsid"
# Опасные команды стенда: зов пишется, ничего не делается.
for cmd in systemctl sudo reboot shutdown poweroff halt; do
    printf '#!/bin/sh\necho "%s $*" >> "%s"\nexit 1\n' "$cmd" "$T/danger.log" > "$T/bin/$cmd"
done
STAND_STUB="$T/reboot-stub.sh"
printf '#!/bin/sh\necho звана >> "%s"\nexit 0\n' "$T/reboot.calls" > "$STAND_STUB"
chmod +x "$T/project/scripts/tg_send.sh" "$T/project/scripts/handover-check.sh" "$T/bin/"* "$STAND_STUB"
TR_DIR="$T/projects/${T//\//-}-project"
SESSION="proba"
HELPER="a1511aa2c0ffee001"
PANEL_STATE="$T/state/panel-state"
LOCK_FILE="$T/log/locks/проверка/backup.lock"

SEED=""
for c in "$HERE/../config/harness.conf" "$HERE/../harness/config/harness.conf" "${HARNESS_CONF:-/etc/harness/harness.conf}"; do
    [ -f "$c" ] && { SEED="$c"; break; }
done
[ -n "$SEED" ] || { echo "ПЛОХО: образца harness.conf нет — проверять нечем"; exit 1; }
cat > "$T/boot-check.yaml" <<YAML
проверки:
  - { имя: ядро, вид: ядро }
занято:
  - { имя: резервная копия, вид: замок, файл: "$LOCK_FILE" }
YAML

# Транскрипт смены: $1 = none (помощников нет) | live (идёт помощник).
# STAND_SDK=1 — рядом СВЕЖЕЕ смены пишет headless-прогон (sdk-cli): SESSION_ID
# сторожа — его, SHIFT_SESSION_ID — смены (В-3).
STAND_SDK=0
SDK_SESSION="proba-headless"
scene() {
    rm -rf "$TR_DIR"; mkdir -p "$TR_DIR"
    python3 - "$TR_DIR" "$SESSION" "$HELPER" "$1" "$STAND_SDK" "$SDK_SESSION" <<'PY'
import datetime, json, os, sys, time
folder, sid, helper, kind = sys.argv[1:5]
now = time.time()
def ts(ago):
    return datetime.datetime.fromtimestamp(now - ago, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
def rec(kind_, ago, **extra):
    r = {"type": kind_, "timestamp": ts(ago), "sessionId": sid, "entrypoint": "cli", "cwd": "/stand"}
    r.update(extra)
    return r
main = [rec("assistant", 30, message={"role": "assistant", "content": [{"type": "text", "text": "Жду."}]})]
with open(os.path.join(folder, sid + ".jsonl"), "w", encoding="utf-8") as f:
    f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in main)
if kind == "live":
    sub = os.path.join(folder, sid, "subagents")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "agent-" + helper + ".jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(rec("assistant", 60, agentId=helper, isSidechain=True,
                               message={"role": "assistant", "content": [{"type": "text", "text": "Шаг."}]}),
                           ensure_ascii=False) + "\n")
if sys.argv[5] == "1":
    sdk_sid = sys.argv[6]
    shift_path = os.path.join(folder, sid + ".jsonl")
    os.utime(shift_path, (now - 20, now - 20))
    record = rec("assistant", 5, message={"role": "assistant", "content": [{"type": "text", "text": "headless"}]})
    record.update(sessionId=sdk_sid, entrypoint="sdk-cli")
    with open(os.path.join(folder, sdk_sid + ".jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
PY
}
# $1 = сцена, $2 = ход закончен после поручения (1/0), дальше — строки конфига случая.
stand() {
    local scene_kind="$1" turn="$2"; shift 2
    rm -rf "$T/log" "$T/state" "$T/kanal.jsonl" "$T/pane.txt" "$T/reboot.calls" "$T/detached.txt" "$T/tasks"
    mkdir -p "$T/log/inbox/обработано" "$PANEL_STATE" "$(dirname "$LOCK_FILE")"
    : > "$T/kanal.jsonl"; : > "$T/pane.txt"
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
ROTATE_IDLE_SEC=1
ROTATE_FINISH_TIMEOUT_SEC=600
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
AUTO_REBOOT_IN_ROTATION=1
STACK_NEIGHBOURS="neighbour-1"
REBOOT_MIN_GAP_HOURS=0
REBOOT_WAIT_SEC=60
REBOOT_LATE_MIN=15
REBOOT_CMD="$STAND_STUB"
CONF
    local line
    for line in "$@"; do printf '%s\n' "$line" >> "$T/harness.conf"; done
    # Сторож читает конфиг стенда последним словом: действующее REBOOT_CMD — оттуда.
    stub_guard "$(bash -c 'source "$1" >/dev/null 2>&1; printf "%s" "$REBOOT_CMD"' _ "$T/harness.conf")"
    scene "$scene_kind"
    [ "$turn" = 1 ] && echo $(( $(date +%s) + 3600 )) > "$T/log/turn-end.stamp"
    # Компакты сторож считает по SESSION_ID — свежайшему транскрипту.
    local compact_sid="$SESSION"
    [ "$STAND_SDK" = 1 ] && compact_sid="$SDK_SESSION"
    jq -cn --arg ts "$(date -Is)" --arg s "$compact_sid" \
        '{ts:$ts, event:"pre_compact", session:$s, detail:"стенд"}' > "$T/log/rotation.jsonl"
    tmux kill-session -t "=$PANE" 2>/dev/null
    # Гасится сервер, а не процесс: сторож ставит панели remain-on-exit, и
    # мёртвая панель иначе остаётся висеть.
    # Агент — в каталоге проекта, как у боевого start-agent.sh: чужой каталог
    # запуска сторож лечит новой сессией и перезагрузку при смене снимает.
    tmux new-session -d -s "$PANE" -c "$T/project" "$(pane_cmd "exec $T/claude >> $T/pane.txt")"
    sleep 0.5
    [ "$(tmux display-message -p -t "$PANE" '#{pane_current_command}')" = claude ] \
        || { echo "ПЛОХО СТЕНД: подставной агент не встал в панель — проверять нечего"; exit 1; }
}
# Сторож фоном; ждём, пока не звана заглушка или не подан «/exit» ($1 = first),
# либо только «/exit» ($1 = exit). Затем ещё $2 с и гасим сторожа.
RUN_N=0
run_until() {
    local mode="$1" extra="${2:-3}" i
    RUN_N=$((RUN_N + 1))
    # Сторож, переживший тест, увидел бы закрытую панель и поднял преемника —
    # новую панель без привязки к тесту. Со смертью теста kill 0 гасит всю
    # группу timeout: сторожа и его потомков.
    env -u TMUX -u CLAUDE_CODE_SESSION_ID PATH="$T/bin:$PATH" \
        HARNESS_CONF="$T/harness.conf" HARNESS_INSTALL_CONF="$T/harness.conf" \
        HARNESS_ACTIVE_LINE=hr PANE_ENTER_DELAY=0.2 REBOOT_CMD="$STAND_STUB" \
        HARNESS_PANEL_STATE="$PANEL_STATE" CLAUDE_PROJECTS_DIR="$T/projects" \
        HARNESS_BOOT_CHECK_LIST="$T/boot-check.yaml" TASKS_ROOT="$T/tasks" \
        timeout -k 5 170 bash -c '{ tail --pid="$1" -f /dev/null; kill 0; } & exec bash "$2"' \
            _ "$$" "$STAND_WARDEN" > "$T/run.$RUN_N" 2>&1 < /dev/null &
    WARDEN_PID=$!
    for i in $(seq 1 80); do
        grep -qx '/exit' "$T/pane.txt" 2>/dev/null && break
        [ "$mode" = first ] && [ -s "$T/reboot.calls" ] && break
        kill -0 "$WARDEN_PID" 2>/dev/null || break
        sleep 2
    done
    sleep "$extra"
    kill "$WARDEN_PID" 2>/dev/null; wait "$WARDEN_PID" 2>/dev/null; WARDEN_PID=""
}
# Стенд обязан ДОЙТИ до тяжёлого пути, иначе проба судит падение.
reached() {
    grep -q "Пора менять сессию" "$T/pane.txt" \
        || { echo "ПЛОХО СТЕНД: прогон $RUN_N не подал поручение тяжёлого пути — ротация не началась"; tail -20 "$T/run.$RUN_N"; exit 1; }
}
stub_called() { cat "$T/reboot.calls" 2>/dev/null | wc -l | tr -d " "; }
exit_sent() { yes_no grep -qx '/exit' "$T/pane.txt"; }
event() { local n; n=$(grep -c "\"event\":\"$1\"" "$T/log/rotation.jsonl" 2>/dev/null); echo "${n:-0}"; }

echo "С1. «выход», всё «да» — перезагрузка вместо «/exit»"
stand none 1; run_until first 4; reached
case_check "С1 заглушка REBOOT_CMD звана один раз" "1" "$(stub_called)"
case_check "С1 «/exit» в панель не подан (машина гаснет сама)" "нет" "$(exit_sent)"
case_check "С1 весть владельцу ушла с --несмотря-на-лимит" "да" \
    "$(yes_no grep -q -- '--несмотря-на-лимит' "$T/kanal.jsonl")"
case_check "С1 событие reboot_started" "1" "$(event reboot_started)"

echo "С2. «принудительно» (срок вышел, агент в ходу) — обычный «/exit»"
stand none 0 "ROTATE_FINISH_TIMEOUT_SEC=30"; run_until first 3; reached
case_check "С2 заглушка не звана" "0" "$(stub_called)"
case_check "С2 «/exit» подан, как раньше" "да" "$(exit_sent)"

echo "С3. ключ 0 — как раньше"
stand none 1 "AUTO_REBOOT_IN_ROTATION=0"; run_until first 3; reached
case_check "С3 заглушка не звана" "0" "$(stub_called)"
case_check "С3 «/exit» подан, как раньше" "да" "$(exit_sent)"

echo "С4. замок резервной копии взят — машина занята (B-3)"
stand none 1
( exec 9>>"$LOCK_FILE"; flock 9; exec sleep 300 ) & LOCK_PID=$!
sleep 0.5
run_until first 3; reached
kill "$LOCK_PID" 2>/dev/null; wait "$LOCK_PID" 2>/dev/null; LOCK_PID=""
case_check "С4 заглушка не звана" "0" "$(stub_called)"
case_check "С4 «/exit» подан" "да" "$(exit_sent)"

echo "С5. прибор помощников падает при непустом subagents/ — «?», нельзя (B-9)"
stand live 1
cp "$T/project/scripts/stall-in-log.py" "$T/stall-in-log.py.real"
printf '#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n' > "$T/project/scripts/stall-in-log.py"
run_until first 3
cp "$T/stall-in-log.py.real" "$T/project/scripts/stall-in-log.py"
case_check "С5 заглушка не звана" "0" "$(stub_called)"

echo "С6. перезагрузка не наступила за REBOOT_WAIT_SEC=1 — обычный «/exit»"
stand none 1 "REBOOT_WAIT_SEC=1"; run_until exit 3; reached
case_check "С6 заглушка звана" "1" "$(stub_called)"
case_check "С6 после ожидания «/exit» подан" "да" "$(exit_sent)"
case_check "С6 событие reboot_not_happened" "1" "$(event reboot_not_happened)"
case_check "С6 метка цела (снимает только boot_check, B-7)" "да" \
    "$(yes_no test -f "$PANEL_STATE/reboot-started.json")"

told_reboot() { yes_no grep -q 'Перезагружаю' "$T/kanal.jsonl"; }

echo "С7. В-3 свежее смены пишет headless-прогон — перезагрузка идёт по флагу смены"
STAND_SDK=1
stand none 1; run_until first 4; reached
case_check "С7 В-3 SESSION_ID ≠ SHIFT_SESSION_ID, всё «да» — заглушка звана один раз" "1" "$(stub_called)"
case_check "С7 В-3 последний взгляд не отказал (reboot_skipped нет)" "" \
    "$(jq -r 'select(.event == "reboot_skipped") | .detail' "$T/log/rotation.jsonl" 2>/dev/null | tr '\n' ' ')"

echo "С8. В-3 жива фоновая задача СМЕНЫ (не свежайшего транскрипта) — перезагрузки нет"
stand none 1 "ROTATE_TASK_FRESH_SEC=3600" "ROTATE_TASK_WAIT_SEC=30"
mkdir -p "$T/tasks/claude-proba/proekt/$SESSION/tasks"
: > "$T/tasks/claude-proba/proekt/$SESSION/tasks/proba.output"
run_until first 4; reached
case_check "С8 В-3 заглушка не звана" "0" "$(stub_called)"
case_check "С8 В-3 «Перезагружаю» владельцу не ушло" "нет" "$(told_reboot)"
STAND_SDK=0

echo "С9. В-1 pause.py упал — пауза неизвестна: перезагрузки нет, «/exit» как раньше"
stand none 1
printf '%s\n' "$PAUSE_CRASH" > "$T/project/scripts/pause.py"
run_until first 4; reached
cp "$HERE/pause.py" "$T/project/scripts/pause.py"
case_check "С9 В-1 заглушка не звана" "0" "$(stub_called)"
case_check "С9 В-1 «Перезагружаю» владельцу не ушло" "нет" "$(told_reboot)"
case_check "С9 В-1 «/exit» подан, как раньше" "да" "$(exit_sent)"

case_check "стенд: опасные команды не звались" "" \
    "$(grep -E 'reboot|shutdown|poweroff|halt|kexec' "$T/danger.log" 2>/dev/null | tr '\n' ' ')"

# ── 9. ни одной настоящей команды ───────────────────────────────────────────
case_check "systemctl/sudo/reboot/shutdown не звались ни разу" "" "$(tr '\n' ' ' < "$REAL_CALLS")"

printf '\nПЕРЕЗАГРУЗКА В ОКНЕ РОТАЦИИ: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
