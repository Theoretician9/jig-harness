#!/usr/bin/env bash
#
# test_pane_door_pause.sh — проба Ф0 спеки «мгновенная доставка сообщений»
# (docs/specs/2026-10-27-мгновенная-доставка-сообщений.md §0, §4.0 п.3–4, §4.4,
# П-0, П-7, П-8, П-13, К-8; карточка hr.remove-message-interception-mechanism).
#
# ЧТО ОХРАНЯЕТ. Владелец 14.09 и 21.09: «пауза — СТОП». По журналам сторожа
# сессий за 18.10–25.10 смену на паузе толкнули 116 раз (23.10 — 112): ветка 6а
# паузу не проверяла, а толчок диспетчера (`kick_async`) судил паузу ДО лока и
# не судил её в момент нажатия. Каждый новый путь толчка повторял бы ту же
# дыру, поэтому пауза, ротация и лимит судятся в ОДНОЙ двери толчка о сообщении
# владельца (`pane_kick_owner` в scripts/lib/pane.sh), а отказ двери — это
# «отложить», не «пропустить»: файл остаётся во входящих и отдаётся, когда
# причина снята. Повтор сторожа — не больше 3 раз на файл, потом один сигнал.
#
# Пути, которые судятся (печатается их число):
#   1. дверь толчка о сообщении — `pane_kick_owner` (pane.sh, напрямую);
#   2. служебная дверь — `pane_kick_service` или прежний `pane_kick` (ею идёт
#      limit-watch): пауза держит и её, лимит — нет (подъём после лимита);
#   3. ветка 6а сторожа сессий — НАСТОЯЩИЙ session-warden.sh целым прогоном;
#   4. толчок диспетчера — НАСТОЯЩИЙ tg-dispatcher.sh со своим циклом:
#      пауза, поставленная, пока толчок ждёт лока, держит нажатие.
#
# Стенд: tmux — подделка первой в PATH (пишет каждый вызов в свой файл и
# отказывает без него), TMUX снята, TMUX_TMPDIR — свой каталог: живой сервер
# tmux проба не зовёт вовсе. LOG_DIR, каталог паузы, канал (tg_send.sh), Bot API,
# каталог стенограмм — свои. Боевые журналы проба не читает.
# Пауза — настоящий pause.py, метка лимита — настоящий limit_mark.py.
#
#   bash scripts/test_pane_door_pause.sh [--случай дверь|сторож|диспетчер]
# Код возврата: 0 — зелёная, 1 — красная. Последняя строка — итог с числом путей.
#
# Механизмы берутся от места самой пробы (корень = ../ от scripts/): мутации
# идут «из-копии» — проба, позванная из копии дерева, судит копию.
#
# Имена переменных ЛАТИНИЦЕЙ (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8

KOREN="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
PANE_LIB="$KOREN/scripts/lib/pane.sh"
WARDEN="$KOREN/harness/demons/session-warden.sh"
DISPATCHER="$KOREN/scripts/tg-dispatcher.sh"
PAUSE_PY="$KOREN/scripts/pause.py"
LIMIT_MARK="$KOREN/scripts/limit_mark.py"
for f in "$PANE_LIB" "$WARDEN" "$DISPATCHER" "$PAUSE_PY" "$LIMIT_MARK"; do
    [ -r "$f" ] || { echo "ПРОБА: нет механизма $f — проверять нечего"; exit 1; }
done

ONLY=""
if [ "${1:-}" = "--случай" ]; then
    ONLY="${2:-}"
    case "$ONLY" in дверь|сторож|диспетчер) ;; *) echo "ПРОБА: неизвестный случай «$ONLY» (дверь|сторож|диспетчер)"; exit 1 ;; esac
elif [ -n "${1:-}" ]; then
    echo "ПРОБА: неизвестный ключ «$1»"; exit 1
fi
want() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }

source "$KOREN/scripts/lib/selftest_cases.sh"

T=$(mktemp -d /tmp/proba-pane-door-XXXXXX)
echo $$ > "$T/owner"
STAMP="proba-pane-door-$$"
PATHS_JUDGED=0
PIDS_KILL=()
# Процессы стенда находятся по окружению: у каждого есть значение с путём стенда
# (PATH, HARNESS_PANEL_STATE, PROBA_STAND), а командная строка диспетчера метки не несёт.
sweep_stand() {  # $@ = каталоги стендов со слешем; печатает число убитых
    local f pid n=0 k pat=()
    [ $# -gt 0 ] || { echo 0; return; }
    for f in "$@"; do pat+=(-e "=$f"); done
    # Диспетчер живёт, пока проход до него не дошёл, и порождает потомков уже после
    # снимка /proc: проходы повторяются, пока очередной не убьёт никого.
    while :; do
        k=0
        for f in /proc/[0-9]*/environ; do
            pid="${f#/proc/}"; pid="${pid%/environ}"
            [ "$pid" = "$$" ] && continue
            grep -qaF "${pat[@]}" "$f" 2>/dev/null && kill -KILL "$pid" 2>/dev/null && k=$((k+1))
        done
        n=$((n + k)); [ "$k" -gt 0 ] || break
    done
    echo "$n"
}
cleanup() {
    local p
    for p in "${PIDS_KILL[@]}"; do kill -KILL -- "-$p" 2>/dev/null; kill -KILL "$p" 2>/dev/null; done
    sweep_stand "$T/" >/dev/null
    if [ -n "${PROBA_KEEP:-}" ]; then echo "стенд оставлен (PROBA_KEEP): $T"; else rm -rf "$T"; fi
}
trap cleanup EXIT
# Стенды прогонов, убитых KILL-ом (ловушка не сработала). Живой хозяин — это
# параллельный прогон той же пробы: его стенд не трогаем.
DEAD=()
for d in /tmp/proba-pane-door-*/; do
    owner=$(cat "$d/owner" 2>/dev/null)
    [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null && continue
    DEAD+=("$d")
done
LEFT=$(sweep_stand "${DEAD[@]}")
if [ "$LEFT" -gt 0 ]; then
    echo "убрано процессов от прерванных прогонов: $LEFT"
fi

# ── подделка tmux ──────────────────────────────────────────────────────────
# Без PROBA_TMUX_LOG отказывает: вызов вне стенда виден, а не молча успешен.
mkdir -p "$T/bin" "$T/tmuxsock"
cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
log="${PROBA_TMUX_LOG:-}"
[ -n "$log" ] || { echo "подделка tmux: позвана вне стенда" >&2; exit 1; }
printf '%s\n' "$*" >> "$log"
case "${1:-}" in
    has-session)     exit 0 ;;
    display-message) case "$*" in
                         *pane_dead*)          echo 0 ;;
                         *pane_current_command*) echo claude ;;
                         *activity*)           date +%s ;;
                         *)                    echo "" ;;
                     esac ;;
    list-clients|capture-pane|list-sessions) : ;;
esac
exit 0
EOF
chmod +x "$T/bin/tmux"

stand_env() {  # общее окружение: подделка tmux первой, живой tmux недостижим
    env -u TMUX TMUX_TMPDIR="$T/tmuxsock" PATH="$T/bin:$PATH" "$@"
}
# Стенд обязан звать подделку, а не tmux системы: иначе проба играет на живом.
[ "$(stand_env bash -c 'command -v tmux')" = "$T/bin/tmux" ] \
    || { echo "ПРОБА: под окружением стенда tmux — не подделка; стенд не собран"; exit 1; }

# Счёт нажатий. Текст — `send-keys … -l -- <текст>`, Enter и аккорд — свои вызовы.
n_lines() { local n; n=$(grep -c -- "$2" "$1" 2>/dev/null); echo "${n:-0}"; }
texts() { n_lines "$1" '^send-keys .* -l -- '; }
enters() { n_lines "$1" '^send-keys -t [^ ]* Enter$'; }
chords() { echo "$(n_lines "$1" '^send-keys -t [^ ]* C-x$')/$(n_lines "$1" '^send-keys -t [^ ]* C-s$')"; }
owner_texts() {  # толчки, называющие каталог входящих стенда
    local n; n=$(grep -- '^send-keys .* -l -- ' "$1" 2>/dev/null | grep -cF -- "$2"); echo "${n:-0}"
}

pause_py() { HARNESS_PANEL_STATE="$1" python3 "$PAUSE_PY" "${@:2}"; }
pause_on() {  # $1 = каталог состояния панели
    mkdir -p "$1"
    pause_py "$1" --поставить "проба: владелец остановил" --кто владелец >/dev/null \
        && pause_py "$1" --идёт-ли \
        || { echo "ПРОБА: pause.py не поставил паузу на стенде — проверять нечего"; exit 1; }
}
pause_off() {
    pause_py "$1" --снять >/dev/null; ! pause_py "$1" --идёт-ли \
        || { echo "ПРОБА: пауза стоит после --снять"; exit 1; }
}
limit_on() {  # $1 = LOG_DIR, $2 = повод (limit|blocked|nemota)
    LOG_DIR="$1" python3 "$LIMIT_MARK" --поставить "$2" >/dev/null
    LOG_DIR="$1" python3 "$LIMIT_MARK" --стоит-ли \
        || { echo "ПРОБА: limit_mark.py не поставил метку «$2» — проверять нечего"; exit 1; }
}
rotation_event() {  # $1 = LOG_DIR, $2 = событие, $3 = сдвиг в секундах назад
    jq -cn --arg ts "$(date -Is -d "@$(( $(date +%s) - $3 ))")" --arg e "$2" \
        '{ts:$ts, event:$e, session:"proba", detail:"стенд"}' >> "$1/rotation.jsonl"
}

# ═══ 1–2. ДВЕРЬ: pane_kick_owner и служебная дверь напрямую ═════════════════
door_stand() {  # $1 = имя; готовит пустой стенд двери
    D="$T/door-$1"
    rm -rf "$D"; mkdir -p "$D/log" "$D/panel"
    : > "$D/tmux-calls"
    printf 'LOG_DIR="%s"\nPANEL_STATE_DIR="%s"\nROTATE_FINISH_TIMEOUT_SEC="900"\n' "$D/log" "$D/panel" > "$D/harness.conf"
}
# Зов двери в своей оболочке. Печатает «rc|PANE_REFUSAL»; двери нет — «НЕТ ДВЕРИ <имя>»
# (не 127: отсутствие функции не должно читаться как «отказала»).
door_call() {  # $1 = функция двери, $2 = текст
    stand_env PROBA_TMUX_LOG="$D/tmux-calls" TMUX_SESSION="$STAMP-door" \
        HARNESS_PANEL_STATE="$D/panel" LOG_DIR="$D/log" \
        HARNESS_CONF="$D/harness.conf" HARNESS_INSTALL_CONF="$D/harness.conf" \
        ROTATE_FINISH_TIMEOUT_SEC=900 PANE_ENTER_DELAY=0 PANE_TMUX_TIMEOUT=3 \
        bash -c 'source "$1" || exit 3
                 declare -F "$2" >/dev/null || { echo "НЕТ ДВЕРИ $2"; exit 0; }
                 rc=0; "$2" "$3" || rc=$?
                 printf "%s|%s\n" "$rc" "${PANE_REFUSAL:-}"' _ "$PANE_LIB" "$1" "$2" 2>/dev/null
}
# Отказ: код не 0 и причина начинается своим словом. Иначе печатается, что вышло.
refused_with() {  # $1 = вывод door_call, $2 = слово причины
    case "$1" in
        "НЕТ ДВЕРИ"*) echo "$1" ;;
        0\|*) echo "пропустила (${1#*|})" ;;
        *\|"$2"*) echo "отказ $2" ;;
        *) echo "отказ другой: ${1#*|}" ;;
    esac
}
passed() {  # $1 = вывод door_call
    case "$1" in "НЕТ ДВЕРИ"*) echo "$1" ;; "0|") echo "пропустила" ;; *) echo "отказ: ${1#*|}" ;; esac
}
# Хвост причины различает двери: у двери сообщений «сообщение ждёт во входящих»,
# у служебной — «толчок отложен» без слов о входящих. По одному слову причины
# двери неразличимы: служебная тоже отказывает «пауза», и снятый гейт паузы в
# двери сообщений прятался за ней (мутации № 1, 11, 12 стояли зелёными).
door_tail() {  # $1 = вывод door_call
    case "${1#*|}" in
        *"сообщение ждёт во входящих") echo "хвост входящих" ;;
        *"во входящих"*) echo "о входящих, но не хвостом: ${1#*|}" ;;
        *"толчок отложен"*) echo "хвост службы" ;;
        *) echo "иной: ${1#*|}" ;;
    esac
}

if want дверь; then
    PATHS_JUDGED=$((PATHS_JUDGED + 2))
    OWNER=pane_kick_owner
    echo "═══ путь 1: дверь толчка о сообщении ($OWNER)"

    # Больной случай первым: на паузе дверь отказывает и в панель не уходит ни байта.
    door_stand pause; pause_on "$D/panel"
    OUT=$(door_call "$OWNER" "проба: сообщение владельца")
    case_check "БОЛЬНОЙ П-0: пауза — дверь отказывает словом «пауза»" "отказ пауза" "$(refused_with "$OUT" пауза)"
    case_check "БОЛЬНОЙ П-0: пауза — нажатий в панель 0" "0" "$(n_lines "$D/tmux-calls" '^send-keys')"
    # Какая поломка оставит два случая выше зелёными? Снятый гейт паузы в
    # pane_gate_owner: отказ тогда даёт служебная дверь, тем же словом. Судит хвост.
    case_check "БОЛЬНОЙ П-0: пауза — отказ дала дверь сообщений (хвост «сообщение ждёт во входящих»)" "хвост входящих" \
        "$(door_tail "$OUT")"

    # П-7: ротация в ходу (свежий rotate_command) — отказ «ротация».
    door_stand rot; rotation_event "$D/log" rotate_command 30
    OUT=$(door_call "$OWNER" "проба: сообщение владельца")
    case_check "П-7: ротация в ходу — отказ словом «ротация»" "отказ ротация" "$(refused_with "$OUT" ротация)"
    case_check "П-7: ротация в ходу — нажатий 0" "0" "$(n_lines "$D/tmux-calls" '^send-keys')"
    case_check "П-7: отказ ротации — хвост «сообщение ждёт во входящих»" "хвост входящих" "$(door_tail "$OUT")"

    # Какая поломка оставит П-7 зелёным? Гейт, отказывающий на ЛЮБОЙ rotate_command
    # в журнале. Его ловят два соседа: команда старше окна и завершённая ротация.
    door_stand rotold; rotation_event "$D/log" rotate_command 7200
    case_check "ротация старше окна (2 ч при 900 с) — дверь пропускает" "пропустила" \
        "$(passed "$(door_call "$OWNER" "проба: сообщение владельца")")"
    door_stand rotdone; rotation_event "$D/log" rotate_command 120; rotation_event "$D/log" agent_restarted 60
    case_check "ротация завершена (agent_restarted после команды) — дверь пропускает" "пропустила" \
        "$(passed "$(door_call "$OWNER" "проба: сообщение владельца")")"

    # П-8: держится лимит — отказ «лимит».
    door_stand lim; limit_on "$D/log" limit
    OUT=$(door_call "$OWNER" "проба: сообщение владельца")
    case_check "П-8: держится лимит — отказ словом «лимит»" "отказ лимит" "$(refused_with "$OUT" лимит)"
    case_check "П-8: держится лимит — нажатий 0" "0" "$(n_lines "$D/tmux-calls" '^send-keys')"

    # Поломка «судья лимита — любая метка» (limit_mark --стоит-ли вместо
    # --лимит-держится): тихая метка blocked значит «работы нет», а не лимит.
    door_stand blocked; limit_on "$D/log" blocked
    case_check "метка blocked (не лимит) — дверь пропускает" "пропустила" \
        "$(passed "$(door_call "$OWNER" "проба: сообщение владельца")")"

    # Порядок гейтов из спеки §4.0: пауза → ротация → лимит.
    door_stand order; pause_on "$D/panel"; limit_on "$D/log" limit
    case_check "пауза и лимит сразу — причина «пауза» (первый гейт)" "отказ пауза" \
        "$(refused_with "$(door_call "$OWNER" "проба")" пауза)"

    # Контроль: без причин — ровно один текст и один Enter (иначе отказ гейтов
    # был бы неотличим от сломанной двери, которая не толкает никогда).
    door_stand clean
    case_check "без паузы, ротации и лимита — дверь пропускает" "пропустила" \
        "$(passed "$(door_call "$OWNER" "проба: сообщение 7")")"
    case_check "без причин — ровно один текст и один Enter" "1/1" \
        "$(texts "$D/tmux-calls")/$(enters "$D/tmux-calls")"
    case_check "без причин — текст двери дошёл до панели целиком" "1" \
        "$(n_lines "$D/tmux-calls" '-l -- проба: сообщение 7$')"

    echo "═══ путь 2: служебная дверь (limit-watch)"
    SERVICE=pane_kick_service
    door_stand svc0
    stand_env bash -c 'source "$1"; declare -F pane_kick_service >/dev/null' _ "$PANE_LIB" 2>/dev/null || SERVICE=pane_kick
    door_stand svcpause; pause_on "$D/panel"
    OUT=$(door_call "$SERVICE" "[сторож лимита] проба")
    case_check "БОЛЬНОЙ: пауза держит и служебный толчок ($SERVICE)" "отказ пауза" "$(refused_with "$OUT" пауза)"
    case_check "служебный отказ — хвост «толчок отложен», о входящих ни слова (служба не про сообщение)" "хвост службы" \
        "$(door_tail "$OUT")"
    case_check "служебный толчок на паузе — нажатий 0" "0" "$(n_lines "$D/tmux-calls" '^send-keys')"
    # Лимит служебный толчок держать НЕ вправе: limit-watch будит смену ровно в
    # момент сброса, когда метка ещё стоит (приказ 17.10). Поломка «limit-watch
    # пустили через дверь сообщений» оставила бы смену спать после лимита.
    door_stand svclim; limit_on "$D/log" limit
    case_check "держится лимит — служебный толчок ($SERVICE) проходит" "пропустила" \
        "$(passed "$(door_call "$SERVICE" "[сторож лимита] проба")")"
    case_check "служебный толчок в лимите — один текст и один Enter" "1/1" \
        "$(texts "$D/tmux-calls")/$(enters "$D/tmux-calls")"
fi

# ═══ 3. СТОРОЖ: ветка 6а целым прогоном ═════════════════════════════════════
if want сторож; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 3: ветка 6а session-warden.sh"
    W="$T/warden"
    mkdir -p "$W/project/scripts/lib" "$W/projects"
    for f in work-queue.py limit_mark.py limit-in-log.py stall-in-log.py; do
        cp "$KOREN/scripts/$f" "$W/project/scripts/"
    done
    cp "$KOREN"/scripts/lib/*.py "$KOREN/scripts/lib/config.sh" "$W/project/scripts/lib/"
    # Канал стенда: строка «<ключи>\t<текст>» на каждый зов. Пока держится лимит,
    # дверь канала ПРИДЕРЖИВАЕТ весть без досыла — код 3 (договор tg_send.sh,
    # функция «придержать»), строка уходит в kanal-held.txt, а не в kanal.txt:
    # «придержано» ≠ «ушло». --несмотря-на-лимит проходит, как у настоящей двери.
    cat > "$W/project/scripts/tg_send.sh" <<KANAL
#!/usr/bin/env bash
flags=""; text=""
for A in "\$@"; do
    case "\$A" in --*) flags="\$flags \$A" ;; *) [ -n "\$text" ] && flags="\$flags \$A" || text="\$A" ;; esac
done
case "\$flags" in *--несмотря-на-лимит*) ;; *)
    if LOG_DIR="$W/log" python3 "$W/project/scripts/limit_mark.py" --лимит-держится >/dev/null 2>&1; then
        printf '%s\t%s\n' "\$flags" "\$text" >> "$W/kanal-held.txt"; exit 3
    fi ;;
esac
printf '%s\t%s\n' "\$flags" "\$text" >> "$W/kanal.txt"
KANAL
    # Писатель задач модели — заглушка с журналом зовов: --resolve судится по нему.
    cat > "$W/project/scripts/alert-task.py" <<ZADACHA
import sys
with open("$W/alert-task.txt", "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
ZADACHA
    printf '#!/usr/bin/env bash\necho "передача стенда есть"\nexit 0\n' > "$W/project/scripts/handover-check.sh"
    chmod +x "$W/project/scripts/tg_send.sh" "$W/project/scripts/handover-check.sh"
    TR_DIR="$W/projects/${W//\//-}-project"
    mkdir -p "$TR_DIR"
    TRANSCRIPT="$TR_DIR/proba.jsonl"
    SEED=""
    for c in "$KOREN/harness/config/harness.conf" "$KOREN/config/harness.conf"; do
        [ -f "$c" ] && { SEED="$c"; break; }
    done
    [ -n "$SEED" ] || { echo "ПРОБА: образца harness.conf нет в дереве — стенд сторожа не собрать"; exit 1; }
    cp "$SEED" "$W/harness.conf"
    cat >> "$W/harness.conf" <<CONF
AGENT_USER="$(id -un)"
LOG_DIR="$W/log"
PROJECT_DIR="$W/project"
STATE_DIR="$W/state"
HEARTBEAT_DIR="$W/state/heartbeat"
TMUX_SESSION="$STAMP-warden"
AGENT_START_CMD="true"
SERVICE_LOCKS_SUBDIR="проверка"
ACTIVE_LINE="hr"
CTX_WINDOW_TOKENS=400000  # не-секрет: размер окна модели в токенах, а не ключ
MAX_COMPACTS=50
ROTATE_NEEDS_OWNER=0
SILENCE_MAX_MIN=600
PULSE_MAX_MIN=600
PULSE_OWNER_MIN=600
LIMIT_STALL_MIN=600
INBOX_WAIT_MAX_MIN=1
ROTATE_FINISH_TIMEOUT_SEC=900
PANE_ENTER_DELAY=0
PANE_TMUX_TIMEOUT=3
TG_CHAT_ID=""
TG_TOKEN_FILE="/nonexistent"
CONF
    PANEL_W="$W/state/panel-state"
    INBOX_W="$W/log/inbox"
    MSG_EPOCH=0

    # Стенд с нуля: одно сообщение владельца во входящих, пришло 5 мин назад
    # (порог сторожа «старше 2 мин» и INBOX_WAIT_MAX_MIN=1 пройдены: на коде до
    # Ф0 решение 6а — «пинок» без всякой срочности).
    ts_ago() { date -u -d "-$1 min" +%Y-%m-%dT%H:%M:%SZ; }
    tr_ok() {  # стенограмма работающей смены: успешный ответ $1 мин назад
        printf '{"type":"assistant","timestamp":"%s"}\n' "$(ts_ago "$1")" > "$TRANSCRIPT"
        touch -d "-$1 min" "$TRANSCRIPT"
    }
    # Запись отказа — форма снята с живого журнала сессии 27.10 (ключи
    # error/apiErrorStatus/isApiErrorMessage, модель <synthetic>), её читает limit-in-log.py.
    tr_refusal() {  # $1 = минут назад
        printf '{"type":"assistant","timestamp":"%s","error":"rate_limit","apiErrorStatus":429,"isApiErrorMessage":true,"message":{"model":"<synthetic>","role":"assistant","content":[{"type":"text","text":"You'"'"'ve hit your session limit · resets 6:10am"}]}}\n' \
            "$(ts_ago "$1")" >> "$TRANSCRIPT"
    }
    # Стенограмма ЛИМИТА: успешного ответа нет час, отказ 429 минуту назад. Свежего
    # ответа без отказа в ней нет — лимитная ветка сторожа не читает «возобновление»
    # и метку не снимает (F0-gen-01: прежний стенд снимал её до 6а сам).
    tr_limit() { tr_ok 60; tr_refusal 1; touch -d '-1 min' "$TRANSCRIPT"; }
    # Лимит обновился: отказ 10 мин назад, после него — свежий ответ. Метку снимает
    # судья лимитной ветки сторожа, как в жизни, а не проба удалением файла.
    tr_resumed() {
        tr_ok 60; tr_refusal 10
        printf '{"type":"assistant","timestamp":"%s"}\n' "$(ts_ago 0)" >> "$TRANSCRIPT"
    }
    limit_held() { LOG_DIR="$W/log" python3 "$LIMIT_MARK" --лимит-держится >/dev/null 2>&1 && echo да || echo нет; }

    w_stand() {
        WRUN=0; W_UNREACHED=0
        rm -rf "$W/log" "$W/state" "$W/kanal.txt" "$W/kanal-held.txt" "$W/tmux-calls" "$W/alert-task.txt" "$W/warden.out"
        mkdir -p "$INBOX_W/обработано" "$PANEL_W"
        : > "$W/kanal.txt"; : > "$W/kanal-held.txt"; : > "$W/tmux-calls"; : > "$W/alert-task.txt"; : > "$W/warden.out"
        printf '%s\n' '{"queue": [], "lines": []}' > "$W/project/dev-map.yaml"
        tr_ok 1
        : > "$W/log/rotation.jsonl"
        MSG_EPOCH=$(( $(date +%s) - 300 ))
        MSG_FILE="$INBOX_W/$MSG_EPOCH.000000001.txt"
        printf 'проба: сообщение владельца\n' > "$MSG_FILE"
        touch -d "@$MSG_EPOCH" "$MSG_FILE"
    }
    # Один прогон сторожа с пределом: стенд, висящий на ротации или подъёме,
    # называется, а не ждёт 900 с.
    #
    # Признак «круг прошёл 6а» — строка ветки 6б («агент на месте»): её пишет
    # КАЖДЫЙ круг при агенте в панели, после 6а. Слово «входящие» им быть не
    # может: верный код по §4.4 п.2 на повторных кругах паузы и после потолка
    # молчит. Не дошедший прогон — неудача пробы (w_reached), а не строка печати:
    # сломанный стенд оставлял бы зелёными все случаи «толчков 0».
    w_run() {
        local rc=0
        WRUN=$((WRUN + 1))
        stand_env PROBA_TMUX_LOG="$W/tmux-calls" \
            HARNESS_CONF="$W/harness.conf" HARNESS_INSTALL_CONF="$W/harness.conf" \
            HARNESS_ACTIVE_LINE=hr WARDEN_DETACHED=1 LOG_DIR="$W/log" \
            HARNESS_PANEL_STATE="$PANEL_W" CLAUDE_PROJECTS_DIR="$W/projects" \
            timeout -k 5 120 bash "$WARDEN" > "$W/run.$WRUN" 2>&1 < /dev/null || rc=$?
        cat "$W/run.$WRUN" >> "$W/warden.out"
        if [ "$rc" = 124 ] || [ "$rc" = 137 ]; then
            echo "  СТЕНД: прогон $WRUN сторожа не вернулся за 120 с (код $rc)"; tail -5 "$W/run.$WRUN" | sed 's/^/    /'
        fi
        if ! grep -qF -- "— агент на месте" "$W/run.$WRUN"; then
            W_UNREACHED=$((W_UNREACHED + 1))
            echo "  СТЕНД: прогон $WRUN не дошёл до ветки 6б (хвост ниже)"; tail -8 "$W/run.$WRUN" | sed 's/^/    /'
        fi
    }
    w_reached() {  # $1 = сценарий
        case_check "СТЕНД ($1): все $WRUN прогонов прошли 6а и дошли до 6б" "0" "$W_UNREACHED"
    }
    in_inbox() { [ -e "$MSG_FILE" ] && echo есть || echo нет; }
    w_owner() { owner_texts "$W/tmux-calls" "$INBOX_W"; }
    # Сигналы потолка о сообщении стенда: время файла и ключ задачи модели.
    w_signals() {  # $1 = файл канала (по умолчанию доставленное)
        local n; n=$(grep -F -- "$(date -d "@$MSG_EPOCH" +%H:%M)" "${1:-$W/kanal.txt}" | grep -c -- '--task'); echo "${n:-0}"
    }
    w_ceiling() { local _; for _ in 1 2 3; do w_run; done; }  # три отданных толчка — счётчик файла на потолке
    w_new_msgs() {  # $1 = сколько новых сообщений, младше старшего, но старше 2 мин
        local i e
        for i in $(seq 1 "$1"); do
            e=$((MSG_EPOCH + 10 * i))
            printf 'проба: новое сообщение %s\n' "$i" > "$INBOX_W/$e.00000000$i.txt"
            touch -d "@$e" "$INBOX_W/$e.00000000$i.txt"
        done
    }

    # 3.0а БОЛЬНОЙ (F0-gen-02): потолок, потом пауза. Потолок судился ДО двери, и
    # владелец на паузе получал «сессия не берёт сообщение» — пауза — СТОП.
    echo "— потолок повторов, потом пауза владельца"
    w_stand; w_ceiling; pause_on "$PANEL_W"; w_run; w_run
    case_check "БОЛЬНОЙ: на паузе после потолка сигналов владельцу 0 (дверь судится раньше потолка)" "0" "$(w_signals)"
    pause_off "$PANEL_W"; w_run
    case_check "пауза снята — сигнал потолка ушёл ровно один (отложен, не пропущен)" "1" "$(w_signals)"
    # F0-gen-06: сторож толкнул 3 раза; четвёртого толчка (диспетчера) могло не быть.
    case_check "сигнал называет число толчков сторожа — «3 раза», не «4 раза»" "3 раза" \
        "$(grep -F -- "$(date -d "@$MSG_EPOCH" +%H:%M)" "$W/kanal.txt" | grep -- '--task' | grep -oE '[0-9]+ раза' | head -1)"
    w_reached "потолок, потом пауза"

    # 3.0б БОЛЬНОЙ (F0-gen-02, лимит): дверь канала придерживает весть кодом 3 —
    # отметка «сказано» на придержанной вести теряет сигнал навсегда.
    echo "— потолок повторов, потом лимит подписки"
    w_stand; w_ceiling; tr_limit; limit_on "$W/log" limit; w_run; w_run
    case_check "СТЕНД: метка лимита держится после кругов (стенограмма лимита честная)" "да" "$(limit_held)"
    case_check "в лимите сигнал потолка не доставлен (0 в канале)" "0" "$(w_signals)"
    tr_resumed; w_run
    case_check "СТЕНД: лимит снят судьёй сторожа по свежему ответу" "нет" "$(limit_held)"
    case_check "БОЛЬНОЙ: лимит снят — сигнал потолка доставлен ровно один («сказано» — только когда весть ушла)" "1" \
        "$(w_signals)"
    w_reached "потолок, потом лимит"

    # 3.0в БОЛЬНОЙ (F0-gen-03): один застрявший файл на потолке глушил толчок для
    # ВСЕХ новых сообщений. Потолок — на файл, не на очередь.
    echo "— старший файл на потолке, пришли 9 новых"
    w_stand; w_ceiling; w_run
    BEFORE=$(w_owner); w_new_msgs 9; w_run
    case_check "БОЛЬНОЙ: старший на потолке + 9 новых — на круге один толчок о новых" "1" "$(( $(w_owner) - BEFORE ))"
    case_check "застрявший старший не сигналит второй раз (потолок держит файл)" "1" "$(w_signals)"
    case_check "застрявший старший по-прежнему во входящих" "есть" "$(in_inbox)"
    w_reached "старший на потолке + новые"

    # 3.0г БОЛЬНОЙ (F0-gen-04): сессия взяла застрявший файл, входящие пусты —
    # счётчики убираются и задача модели inbox-stuck закрывается тем же кругом.
    echo "— застрявший файл взят, входящие пусты"
    w_stand; w_ceiling; w_run
    mv "$MSG_FILE" "$INBOX_W/обработано/"; w_run
    case_check "БОЛЬНОЙ: файл взят при пустых входящих — --resolve session-warden:inbox-stuck ровно один" "1" \
        "$(n_lines "$W/alert-task.txt" '^--resolve session-warden:inbox-stuck$')"
    case_check "файл взят — счётчиков повторов в kick-state 0" "0" \
        "$(find "$W/log/kick-state" -maxdepth 1 -name 'repeat-*' 2>/dev/null | wc -l | tr -d ' ')"
    w_reached "файл взят"

    # 3.0д F0-gen-07: метка причины отказа снимается, когда входящие опустели —
    # второй эпизод паузы говорит «смену не бужу» снова.
    echo "— два эпизода паузы"
    w_stand; pause_on "$PANEL_W"; w_run; pause_off "$PANEL_W"
    mv "$MSG_FILE" "$INBOX_W/обработано/"; w_run
    MSG_EPOCH=$(( $(date +%s) - 300 )); MSG_FILE="$INBOX_W/$MSG_EPOCH.000000002.txt"
    printf 'проба: второе сообщение\n' > "$MSG_FILE"; touch -d "@$MSG_EPOCH" "$MSG_FILE"
    pause_on "$PANEL_W"; w_run
    case_check "второй эпизод паузы — «смену не бужу» сказано снова (за два эпизода 2)" "2" \
        "$(n_lines "$W/warden.out" 'смену не бужу')"
    pause_off "$PANEL_W"
    w_reached "два эпизода паузы"

    # 3.1 БОЛЬНОЙ П-0 (116 живых случаев): пауза, три круга сторожа.
    echo "— пауза владельца, три круга сторожа"
    w_stand; pause_on "$PANEL_W"; w_run; w_run; w_run
    case_check "БОЛЬНОЙ П-0: на паузе толчков о сообщении 0 за три круга" "0" "$(w_owner)"
    case_check "БОЛЬНОЙ П-0: на паузе нажатий текста в панель 0 (пауза — СТОП для всех веток)" "0" "$(texts "$W/tmux-calls")"
    case_check "на паузе сообщение осталось во входящих (отложено, не снято)" "есть" "$(in_inbox)"
    case_check "§4.4 п.2: «смену не бужу» сказано один раз за три круга, не на каждом" "1" \
        "$(n_lines "$W/warden.out" 'смену не бужу')"
    BEFORE=$(w_owner); pause_off "$PANEL_W"; w_run
    case_check "пауза снята — на ближайшем круге ровно один толчок (отложено, не пропущено)" "1" "$(( $(w_owner) - BEFORE ))"
    w_reached "пауза, три круга"

    # 3.2 Контроль: без причин круг толкает ровно один раз.
    echo "— без паузы, ротации и лимита"
    w_stand; w_run
    case_check "без причин — один толчок о сообщении за круг" "1" "$(w_owner)"
    # Enter считается за толчком о сообщении: служебный пульс 6в стенда (нет
    # отчёта 9999 мин) законно жмёт свой Enter, и общий счёт его бы смешал.
    case_check "без причин — за толчком о сообщении ровно один Enter" "1" \
        "$(grep '^send-keys' "$W/tmux-calls" | awk -v d="$INBOX_W" 'p && /^send-keys -t [^ ]* Enter$/ {n++} {p = (index($0, " -l -- ") && index($0, d))} END {print n+0}')"
    w_reached "без причин"

    # 3.3 П-13: потолок повторов. Сессия файл не переносит, пять кругов.
    echo "— сессия не берёт сообщение: пять кругов"
    w_stand; for _ in 1 2 3 4 5; do w_run; done
    case_check "П-13: за пять кругов толчков ровно 3 (потолок повторов на файл)" "3" "$(w_owner)"
    HHMM=$(date -d "@$MSG_EPOCH" +%H:%M)
    case_check "П-13: после потолка — один сигнал владельцу с временем сообщения ($HHMM) и задачей модели" "1" \
        "$(grep -F -- "$HHMM" "$W/kanal.txt" | grep -c -- '--task')"
    case_check "П-13: сообщение по-прежнему во входящих (потолок не удаляет)" "есть" "$(in_inbox)"
    # F0-gen-08: круг на потолке, который молча не толкает, неотличим от круга, не
    # дошедшего до 6а. Строка о потолке — одна на смену состояния, не на каждом круге.
    # «компактов … (потолок 50)» — чужая строка каждого круга, её не считаем.
    case_check "F0-gen-08: о потолке файла в журнале сторожа одна строка за пять кругов" "1" \
        "$(grep -v 'компактов' "$W/warden.out" | grep -c 'потолк')"
    w_reached "потолок, пять кругов"

    # 3.4 П-8: держится лимит — три круга без толчка, снят — толчок.
    echo "— держится лимит подписки"
    # Стенограмма лимита (tr_limit): при свежем ответе без отказа лимитная ветка
    # сторожа законно читает возобновление и снимает метку раньше 6а — судилось
    # бы снятие метки, а не дверь. Что метка выжила, проба проверяет сама.
    w_stand; tr_limit
    limit_on "$W/log" limit; w_run; w_run; w_run
    case_check "СТЕНД: метка лимита держится после трёх кругов" "да" "$(limit_held)"
    case_check "П-8: в лимите толчков о сообщении 0 за три круга" "0" "$(w_owner)"
    case_check "П-8: в лимите сообщение осталось во входящих" "есть" "$(in_inbox)"
    BEFORE=$(w_owner); tr_resumed; w_run
    case_check "СТЕНД: лимит снят судьёй сторожа по свежему ответу" "нет" "$(limit_held)"
    case_check "П-8: лимит снят — на ближайшем круге один толчок (отложено, не пропущено)" "1" "$(( $(w_owner) - BEFORE ))"
    w_reached "лимит"

    # 3.5 П-7: ротация в ходу — толчка нет; ротация завершена — толчок.
    echo "— ротация в ходу"
    w_stand; rotation_event "$W/log" rotate_command 30; w_run
    case_check "П-7: ротация в ходу — толчков о сообщении 0" "0" "$(w_owner)"
    BEFORE=$(w_owner); rotation_event "$W/log" agent_restarted 5; w_run
    case_check "П-7: ротация завершена — на следующем круге один толчок" "1" "$(( $(w_owner) - BEFORE ))"
    w_reached "ротация"
fi

# ═══ 4. ДИСПЕТЧЕР: пауза между решением и нажатием ═════════════════════════
if want диспетчер; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 4: толчок диспетчера (kick_async)"
    # Подставной Bot API: одно сообщение 101 со словом срочности (на коде до Ф3
    # оно проходит urgency.py), запись запрошенных offset.
    cat > "$T/api.py" <<'PYEOF'
import json, sys, time, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
port, acks, ready = int(sys.argv[1]), sys.argv[2], sys.argv[3]
def update(uid):
    return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": 777000},
                                          "text": f"срочно, сообщение {uid}"}}
class H(BaseHTTPRequestHandler):
    def reply(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        if self.path.endswith("/getUpdates"):
            off = int((form.get("offset") or ["0"])[0])
            ids = [u for u in [101] if u >= off]
            if not ids:
                time.sleep(1)
            self.reply({"ok": True, "result": [update(u) for u in ids]}); return
        with open(acks, "a") as fh:
            fh.write((form.get("text") or [""])[0].replace("\n", " ") + "\n")
        self.reply({"ok": True, "result": {"message_id": 1}})
    do_GET = do_POST
    def log_message(self, *_): pass
srv = HTTPServer(("127.0.0.1", port), H)
open(ready, "w").close()
srv.serve_forever()
PYEOF
    # Держатель лока толчков: оба имени (нынешнее pane-kick.lock и общее
    # pane.lock из §4.0) — проба не привязана к имени, которое сменит Ф2.
    cat > "$T/holder.py" <<'PYEOF'
import fcntl, os, sys, time
fs = [open(p, "a") for p in sys.argv[3:]]
for f in fs:
    fcntl.flock(f, fcntl.LOCK_EX)
open(sys.argv[2], "w").close()
while not os.path.exists(sys.argv[1]):
    time.sleep(0.1)
PYEOF
    wait_for() {  # $1 = предел, остальное — условие
        local end=$(( $(date +%s) + $1 )); shift
        while [ "$(date +%s)" -lt "$end" ]; do "$@" && return 0; sleep 0.3; done
        "$@"
    }
    offset_is() { [ "$(cat "$1/log/tg_offset" 2>/dev/null)" = "$2" ]; }

    # $1 = имя, $2 = when: before (пауза до сообщения) | between (пока толчок ждёт лока) | none,
    # $3 = comment — ещё и заявка панели (срочный комментарий): её толчок ждёт того же лока.
    D_COMMENT_TS="1761500000.5"
    d_case() {
        local S="$T/disp-$1" when="$2" port lockdir
        D_STAND_BAD=0
        mkdir -p "$S/log" "$S/project" "$S/panel"
        if [ "${3:-}" = comment ]; then
            mkdir -p "$S/panel/comments/new"
            printf '{"id":"hr.proba","текст":"срочно, проба комментария"}' > "$S/panel/comments/new/$D_COMMENT_TS.json"
        fi
        : > "$S/tmux-calls"
        printf '000:СТЕНДОВЫЙ' > "$S/token"   # не-секрет: подставной Bot API стенда
        port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
        PROBA_STAND="$T/" python3 "$T/api.py" "$port" "$S/acks" "$S/api.ready" > "$S/api.out" 2>&1 &
        PIDS_KILL+=($!); disown $!
        wait_for 10 test -e "$S/api.ready" || { d_bad "Bot API не поднялся"; return; }
        cat > "$S/install.conf" <<EOF
PROJECT_DIR="$S/project"
PROJECT_NAME="стенд"
LOG_DIR="$S/log"
TG_CHAT_ID="777000"
TG_TOKEN_FILE="$S/token"
AGENT_USER="$(id -un)"
PANEL_STATE_DIR="$S/panel"
EOF
        printf 'TMUX_SESSION="%s"\nSERVICE_LOCKS_SUBDIR="services"\nPANE_TMUX_TIMEOUT="3"\nPANE_ENTER_DELAY="0.1"\nPANE_SEND_NOW="1"\n' \
            "$STAMP-disp" > "$S/harness.conf"
        lockdir="$S/log/locks/services"; mkdir -p "$lockdir"
        PROBA_STAND="$T/" python3 "$T/holder.py" "$S/release" "$S/held" "$lockdir/pane-kick.lock" "$lockdir/pane.lock" &
        PIDS_KILL+=($!); disown $!
        wait_for 5 test -e "$S/held" || { d_bad "лок толчков не взят"; return; }
        [ "$when" = before ] && pause_on "$S/panel"
        stand_env PROBA_TMUX_LOG="$S/tmux-calls" HARNESS_PANEL_STATE="$S/panel" \
            HARNESS_INSTALL_CONF="$S/install.conf" HARNESS_CONF="$S/harness.conf" \
            TG_API_BASE="http://127.0.0.1:$port" \
            setsid nohup bash "$DISPATCHER" > "$S/dispatcher.out" 2>&1 < /dev/null &
        DPID=$!
        PIDS_KILL+=("$DPID"); disown "$DPID"
        # Update 101 разобран целиком (offset 102 на диске) — толчок, если его
        # дали, уже ждёт лока, который держит стенд.
        wait_for 25 offset_is "$S" 102 || { d_bad "диспетчер не разобрал update 101 за 25 с"; tail -5 "$S/log/tg-dispatcher.log" 2>/dev/null | sed 's/^/    /'; }
        sleep 1
        [ "$when" = between ] && pause_on "$S/panel"
        : > "$S/release"
        sleep 5   # предел ожидания лока у толчка 17 с; отпущен — нажатия идут сразу
        kill -KILL -- "-$DPID" 2>/dev/null; kill -KILL "$DPID" 2>/dev/null
        DS="$S"
    }
    # Стенд, не дошедший до толчка, — неудача пробы: случаи «текстов 0» на нём зелены даром.
    d_bad() { D_STAND_BAD=$((D_STAND_BAD + 1)); echo "  СТЕНД: $1"; }
    d_reached() { case_check "СТЕНД ($1): диспетчер разобрал сообщение, лок взят" "0" "$D_STAND_BAD"; }
    d_inbox() { grep -qs 'сообщение 101' "$1"/log/inbox/*.txt && echo есть || echo нет; }

    echo "— контроль: без паузы толчок ждёт лока и отдаётся после его снятия"
    d_case none none; d_reached "без паузы"
    case_check "без паузы — один толчок (текст) после снятия лока" "1" "$(texts "$DS/tmux-calls")"
    case_check "без паузы — аккорд C-x/C-s отдан (ключ 1)" "1/1" "$(chords "$DS/tmux-calls")"

    echo "— пауза поставлена, пока толчок ждал лока (и толчок комментария панели)"
    d_case between between comment; d_reached "пауза между"
    DLOG="$DS/log/tg-dispatcher.log"
    case_check "СТЕНД: оба толчка дождались лока (нет «очередь толчков занята»)" "0" "$(n_lines "$DLOG" 'очередь толчков занята')"
    case_check "БОЛЬНОЙ: пауза в момент нажатия — текстов в панель 0" "0" "$(texts "$DS/tmux-calls")"
    case_check "БОЛЬНОЙ: пауза в момент нажатия — аккорда нет" "0/0" "$(chords "$DS/tmux-calls")"
    case_check "сообщение осталось во входящих (отложено, не потеряно)" "есть" "$(d_inbox "$DS")"
    # Какая поломка оставит три случая выше зелёными? Толчок через служебную
    # pane_kick (мутация 11) или гейт до лока (12): служебная дверь тоже держит
    # паузу. Отличает их хвост причины — его даёт только дверь сообщений.
    case_check "БОЛЬНОЙ: отказ в момент нажатия дала дверь сообщений (хвост «сообщение ждёт во входящих»)" "1" \
        "$(grep 'пинок агенту не отдан: пауза' "$DLOG" | grep -c 'сообщение ждёт во входящих')"
    case_check "комментарий панели на паузе не отдан — строка в журнале есть" "1" \
        "$(n_lines "$DLOG" "комментарий $D_COMMENT_TS не отдан")"
    # F0-gen-09: комментарий во входящих не лежит — слова о входящих в его строке лгут.
    case_check "F0-gen-09: в строке отказа комментария нет слов «во входящих»" "0" \
        "$(grep "комментарий $D_COMMENT_TS не отдан" "$DLOG" | grep -c 'во входящих')"

    echo "— пауза стояла до сообщения"
    d_case before before; d_reached "пауза до"
    case_check "пауза до сообщения — текстов в панель 0" "0" "$(texts "$DS/tmux-calls")"
    case_check "пауза до сообщения — сообщение во входящих" "есть" "$(d_inbox "$DS")"
fi

echo "судимых путей толчка: $PATHS_JUDGED"
[ "$CASES_FAILED" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: путей $PATHS_JUDGED, случаев $CASES_PATHS"; exit 0; }
echo "ПРОБА КРАСНАЯ: путей $PATHS_JUDGED, неудач $CASES_FAILED из $CASES_PATHS"; exit 1
