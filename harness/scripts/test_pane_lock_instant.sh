#!/usr/bin/env bash
#
# test_pane_lock_instant.sh — проба фазы «доставка Ф1» спеки «мгновенная доставка
# сообщений» (docs/specs/2026-10-27-мгновенная-доставка-сообщений.md §2, §3.4 S2–S3,
# §4.0 п.1–2, 5–6, §4.4 п.1, П-1, П-4, П-5, П-6, П-13, К-3; карточка
# hr.remove-message-interception-mechanism; план docs/plans/2026-10-28-доставка-ф1.md).
#
# ЧТО ОХРАНЯЕТ. Владелец 27.10: «механизм, который перехватывает сообщения, убрать и
# сделать доставку сообщений мгновенную, потому что сейчас харнес тормозит». Замер
# 20.10–27.10: 13 из 109 сообщений («есть слова работы») диспетчер придержал, до
# контекста модели они шли медиану 304 с, худшее 2152 с — их отдавал сторож сессий
# через 30 минут. Второе: в панель агента пишут три писателя (диспетчер, сторож,
# limit-watch), лок берёт один диспетчер; на стенде 27.10 тексты двух писателей
# склеились до Enter в 71 из 100 попыток. Отсюда две половины фазы: ни одно
# сообщение не придерживается (толчок сразу), и все нажатия в панель идут под одним
# локом `$LOG_DIR/locks/<SERVICE_LOCKS_SUBDIR>/pane.lock`. Пауза владельца, ротация
# и лимит по-прежнему откладывают толчок (Ф0) — фаза их не ломает.
#
# Пути, которые судятся (печатается их число):
#   1. дверь в панель (scripts/lib/pane.sh) напрямую: два писателя одновременно на
#      подделке tmux с задержкой — склейка, чужое между C-x и C-s, лок, аккорд
#      двери, удержание лока после оборванного аккорда;
#   2. сторож сессий (session-warden.sh) целым прогоном: «ждём» нет, потолок и
#      сигнал живы, пауза/лимит/ротация откладывают, все его нажатия под локом;
#   3. limit-watch.sh целым прогоном: подъём после лимита — нажатия под локом;
#   4. диспетчер (tg-dispatcher.sh) целым циклом с подставным Bot API: сообщение
#      со словами работы и комментарий панели не придерживаются, 10 сообщений
#      подряд — каждое по разу и по возрастанию, пауза/ротация/лимит откладывают,
#      ключ PANE_SEND_NOW=0 аккорда не даёт, нажатия под локом;
#   5. код (второй носитель, разбор файлов): носителей перехвата нет, писателей в
#      панель мимо двери нет, умолчание PANE_SEND_NOW в пакете — 0;
#   6. model.sh --только-сессия против толчка о сообщении, поведением: «/model»,
#      Enter и Enter подтверждения диалога идут подряд, толчок не встаёт между ними
#      (диспетчер зовёт model.sh фоном и толкает то же сообщение следом).
#
# Стенд: tmux — подделка первой в PATH (пишет каждый вызов с меткой времени и
# состоянием лока pane.lock в момент нажатия; без PROBA_TMUX_LOG отказывает), TMUX
# снята, TMUX_TMPDIR свой: живой сервер tmux проба не зовёт. LOG_DIR, каталог
# паузы, канал, Bot API, стенограммы — свои. Настоящую паузу проба не ставит:
# pause.py работает на каталоге стенда (HARNESS_PANEL_STATE). Вызовов модели нет
# (карты dev-map.yaml на стенде диспетчера нет — map-from-message.py не зовётся).
#
#   bash scripts/test_pane_lock_instant.sh [--случай дверь|сторож|лимит|диспетчер|код|модель]
# Код возврата: 0 — зелёная, 1 — красная. Последняя строка — итог с числом путей.
# Механизмы берутся от места самой пробы (../ от scripts/): мутации идут «из-копии».
#
# Имена переменных ЛАТИНИЦЕЙ (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8

KOREN="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
# Раскладка проекта (harness/demons) и пакета (demons рядом со scripts).
DEMONS="$KOREN/harness/demons"; [ -d "$DEMONS" ] || DEMONS="$KOREN/demons"
CONF_SEED="$KOREN/harness/config/harness.conf"; [ -f "$CONF_SEED" ] || CONF_SEED="$KOREN/config/harness.conf"
PANE_LIB="$KOREN/scripts/lib/pane.sh"
WARDEN="$DEMONS/session-warden.sh"
LIMIT_WATCH="$DEMONS/limit-watch.sh"
DISPATCHER="$KOREN/scripts/tg-dispatcher.sh"
MODEL_SH="$KOREN/scripts/model.sh"
PAUSE_PY="$KOREN/scripts/pause.py"
LIMIT_MARK="$KOREN/scripts/limit_mark.py"
for f in "$PANE_LIB" "$WARDEN" "$LIMIT_WATCH" "$DISPATCHER" "$MODEL_SH" "$PAUSE_PY" "$LIMIT_MARK" "$CONF_SEED"; do
    [ -r "$f" ] || { echo "ПРОБА: нет механизма $f — проверять нечего"; exit 1; }
done

ONLY=""
if [ "${1:-}" = "--случай" ]; then
    ONLY="${2:-}"
    case "$ONLY" in дверь|сторож|лимит|диспетчер|код|модель) ;; *) echo "ПРОБА: неизвестный случай «$ONLY» (дверь|сторож|лимит|диспетчер|код|модель)"; exit 1 ;; esac
elif [ -n "${1:-}" ]; then
    echo "ПРОБА: неизвестный ключ «$1»"; exit 1
fi
want() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }

source "$KOREN/scripts/lib/selftest_cases.sh"

T=$(mktemp -d /tmp/proba-pane-lock-XXXXXX)
echo $$ > "$T/owner"
STAMP="proba-pane-lock-$$"
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
for d in /tmp/proba-pane-lock-*/; do
    owner=$(cat "$d/owner" 2>/dev/null)
    [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null && continue
    DEAD+=("$d")
done
LEFT=$(sweep_stand "${DEAD[@]}")
if [ "$LEFT" -gt 0 ]; then
    echo "убрано процессов от прерванных прогонов: $LEFT"
fi

# ── подделка tmux ──────────────────────────────────────────────────────────
# Строка журнала: «<эпоха> <лок> <доводы>», лок — held|free для send-keys (свободен
# ли pane.lock стенда в момент нажатия: держит его писатель — flock -n не берёт),
# «-» для прочих подкоманд. PROBA_TMUX_DELAY растягивает каждое нажатие (окно
# гонки), PROBA_TMUX_FAIL_CS роняет C-s (оборванный аккорд).
mkdir -p "$T/bin" "$T/tmuxsock"
cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
log="${PROBA_TMUX_LOG:-}"
[ -n "$log" ] || { echo "подделка tmux: позвана вне стенда" >&2; exit 1; }
lock="-"
if [ "${1:-}" = send-keys ]; then
    lock="free"
    if [ -n "${PROBA_PANE_LOCK:-}" ] && [ -d "$(dirname "$PROBA_PANE_LOCK")" ]; then
        flock -n -E 99 "$PROBA_PANE_LOCK" true 2>/dev/null
        [ "$?" = 99 ] && lock="held"
    fi
fi
printf '%s %s %s\n' "$(date +%s.%N)" "$lock" "$*" >> "$log"
case "${1:-}" in
    has-session)     exit 0 ;;
    display-message) case "$*" in
                         *pane_dead*)            echo 0 ;;
                         *pane_current_command*) echo claude ;;
                         *activity*)             date +%s ;;
                         *)                      echo "" ;;
                     esac ;;
    # Диалог «Switch model?»: виден, когда с текста «/model» прошло
    # PROBA_MODEL_DIALOG секунд (у живого Claude Code он рисуется не сразу).
    capture-pane)
        if [ -n "${PROBA_MODEL_DIALOG:-}" ]; then
            t=$(grep -F -- ' -l -- /model' "$log" | tail -1 | cut -d' ' -f1)
            [ -n "$t" ] && awk -v t="$t" -v d="$PROBA_MODEL_DIALOG" -v now="$(date +%s.%N)" \
                'BEGIN { exit !(now - t >= d) }' && echo "Switch model? 1. Yes 2. No"
        fi ;;
    send-keys)
        [ -n "${PROBA_TMUX_DELAY:-}" ] && sleep "$PROBA_TMUX_DELAY"
        case "$*" in *" C-s") [ -n "${PROBA_TMUX_FAIL_CS:-}" ] && exit 1 ;; esac ;;
esac
exit 0
EOF
chmod +x "$T/bin/tmux"

stand_env() {  # подделка tmux первой, живой tmux недостижим
    env -u TMUX TMUX_TMPDIR="$T/tmuxsock" PATH="$T/bin:$PATH" "$@"
}
[ "$(stand_env bash -c 'command -v tmux')" = "$T/bin/tmux" ] \
    || { echo "ПРОБА: под окружением стенда tmux — не подделка; стенд не собран"; exit 1; }

# ── разбор журнала подделки ────────────────────────────────────────────────
cat > "$T/an.py" <<'PYEOF'
import re, sys

def keys(path):
    out = []
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split(" ", 2)
        if len(parts) < 3 or not parts[2].startswith("send-keys "):
            continue
        t, lock, args = float(parts[0]), parts[1], parts[2]
        m = re.match(r"send-keys -t \S* (.*)$", args)
        rest = m.group(1) if m else ""
        if rest.startswith("-l -- "):
            out.append((t, lock, "text", rest[6:]))
        else:
            out.append((t, lock, {"Enter": "enter", "C-x": "cx", "C-s": "cs"}.get(rest, "other"), rest))
    return out

def race(path, a, b):
    seq = ""
    for _, _, kind, text in keys(path):
        if kind == "text":
            seq += "A" if a in text else ("B" if b in text else "?")
        else:
            seq += {"enter": "E", "cx": "X", "cs": "S"}.get(kind, "o")
    glue = any(c in "AB?" and seq[i + 1:i + 2] != "E" for i, c in enumerate(seq))
    chord = any(c == "X" and seq[i + 1:i + 2] != "S" for i, c in enumerate(seq))
    if glue:
        return "склейка:" + seq
    if chord:
        return "аккорд-разорван:" + seq
    if seq.count("A") != 1 or seq.count("B") != 1:
        return "недоставка:" + seq
    if seq.count("X") == 0:
        return "нет-аккорда:" + seq
    return "ok:" + seq

def lockfree(path):
    ks = keys(path)
    return f"{sum(1 for k in ks if k[1] != 'held')} {len(ks)}"

def kick_time(path, needle):
    for t, _, kind, text in keys(path):
        if kind == "text" and needle in text:
            return f"{t:.3f}"
    return "нет"

def order(path, inbox):
    pat = re.compile(re.escape(inbox) + r"/(\d+\.\d+)\.txt")
    found = []
    for _, _, kind, text in keys(path):
        if kind == "text":
            found += pat.findall(text)
    return " ".join(found)

def gap_after_cx(path, marker):
    ks = keys(path)
    cx = next((k[0] for k in ks if k[2] == "cx"), None)
    if cx is None:
        return "нет-C-x"
    nxt = next((k[0] for k in ks if k[0] > cx and k[2] == "text" and marker in k[3]), None)
    return "нет-второго" if nxt is None else f"{nxt - cx:.2f}"

def count_texts(path, needle):
    return str(sum(1 for k in keys(path) if k[2] == "text" and needle in k[3]))

def chords(path):
    ks = keys(path)
    return f"{sum(1 for k in ks if k[2] == 'cx')}/{sum(1 for k in ks if k[2] == 'cs')}"

def model_seq(path, model, kick):
    # M — текст «/model», K — текст толчка, E — Enter. Целая последовательность
    # смены модели — «M E E» подряд: Enter набора и Enter подтверждения диалога.
    seq = ""
    for _, _, kind, text in keys(path):
        if kind == "text":
            seq += "M" if model in text else ("K" if kick in text else "?")
        else:
            seq += {"enter": "E"}.get(kind, "o")
    if seq.count("M") != 1 or seq.count("K") != 1 or seq.count("E") != 3:
        return "недоставка:" + seq
    i = seq.index("M")
    return ("ok:" if seq[i:i + 3] == "MEE" else "разрыв:") + seq

cmd, args = sys.argv[1], sys.argv[2:]
print({"race": race, "lockfree": lockfree, "kick_time": kick_time, "order": order,
       "gap": gap_after_cx, "texts": count_texts, "chords": chords, "modelseq": model_seq}[cmd](*args))
PYEOF
an() { python3 "$T/an.py" "$@"; }
n_lines() { local n; n=$(grep -c -- "$2" "$1" 2>/dev/null); echo "${n:-0}"; }

pause_py() { HARNESS_PANEL_STATE="$1" python3 "$PAUSE_PY" "${@:2}"; }
pause_on() {
    mkdir -p "$1"
    pause_py "$1" --поставить "проба: владелец остановил" --кто владелец >/dev/null \
        && pause_py "$1" --идёт-ли \
        || { echo "ПРОБА: pause.py не поставил паузу на стенде — проверять нечего"; exit 1; }
}
pause_off() {
    pause_py "$1" --снять >/dev/null; ! pause_py "$1" --идёт-ли \
        || { echo "ПРОБА: пауза стоит после --снять"; exit 1; }
}
limit_on() {  # $1 = LOG_DIR, $2 = повод
    LOG_DIR="$1" python3 "$LIMIT_MARK" --поставить "$2" >/dev/null
    LOG_DIR="$1" python3 "$LIMIT_MARK" --стоит-ли \
        || { echo "ПРОБА: limit_mark.py не поставил метку «$2» — проверять нечего"; exit 1; }
}
rotation_event() {  # $1 = LOG_DIR, $2 = событие, $3 = секунд назад
    jq -cn --arg ts "$(date -Is -d "@$(( $(date +%s) - $3 ))")" --arg e "$2" \
        '{ts:$ts, event:$e, session:"proba", detail:"стенд"}' >> "$1/rotation.jsonl"
}
wait_for() {  # $1 = предел, остальное — условие
    local end=$(( $(date +%s) + $1 )); shift
    while [ "$(date +%s)" -lt "$end" ]; do "$@" && return 0; sleep 0.3; done
    "$@"
}
# Ждать, пока подделка tmux замолчит на $2 с (предел $3 с): толчки идут фоном.
wait_quiet() {
    local end=$(( $(date +%s) + $3 )) age
    while [ "$(date +%s)" -lt "$end" ]; do
        age=$(( $(date +%s) - $(stat -c %Y "$1" 2>/dev/null || echo 0) ))
        [ "$age" -ge "$2" ] && return 0
        sleep 0.5
    done
}

# Сообщение со словами работы, без слов срочности: urgency.py на нём молчит, и
# сегодняшний диспетчер (занятая смена) его придерживает. Стенд это проверяет.
WORK_TEXT="почини карту задач в панели, кнопка обновления не жмётся"
[ -z "$(python3 "$KOREN/scripts/urgency.py" "$WORK_TEXT" 2>/dev/null)" ] \
    || { echo "ПРОБА: urgency.py считает «$WORK_TEXT» срочным — больной случай не воспроизвести"; exit 1; }

# ═══ 1. ДВЕРЬ: два писателя, лок, аккорд ════════════════════════════════════
RACE_N=20
if want дверь; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 1: дверь в панель (pane.sh), два писателя на подделке с задержкой"
    door_stand() {  # $1 = имя, $2 = PANE_SEND_NOW стенда
        D="$T/door-$1"
        rm -rf "$D"; mkdir -p "$D/log/locks/services" "$D/panel"
        : > "$D/tmux-calls"
        printf 'LOG_DIR="%s"\nPANEL_STATE_DIR="%s"\nSERVICE_LOCKS_SUBDIR="services"\nPANE_SEND_NOW="%s"\n' \
            "$D/log" "$D/panel" "$2" > "$D/harness.conf"
        DOOR_KEY="$2"
    }
    # Ключ PANE_SEND_NOW отдаётся и окружением, и файлом стенда: дверь вправе читать
    # его любым из двух способов, проба не привязана к способу.
    door_write() {  # $1 = функция двери, $2 = текст; печатает «rc|PANE_REFUSAL»
        stand_env PROBA_TMUX_LOG="$D/tmux-calls" PROBA_PANE_LOCK="$D/log/locks/services/pane.lock" \
            PROBA_TMUX_DELAY="${DOOR_DELAY:-}" PROBA_TMUX_FAIL_CS="${DOOR_FAIL_CS:-}" \
            TMUX_SESSION="$STAMP-door" HARNESS_PANEL_STATE="$D/panel" LOG_DIR="$D/log" \
            SERVICE_LOCKS_SUBDIR=services HARNESS_CONF="$D/harness.conf" HARNESS_INSTALL_CONF="$D/harness.conf" \
            PANE_ENTER_DELAY=0.3 PANE_TMUX_TIMEOUT=5 PANE_SEND_NOW="$DOOR_KEY" \
            bash -c 'source "$1" || exit 3
                     declare -F "$2" >/dev/null || { echo "НЕТ ДВЕРИ $2"; exit 0; }
                     rc=0; "$2" "$3" || rc=$?
                     printf "%s|%s\n" "$rc" "${PANE_REFUSAL:-}"' _ "$PANE_LIB" "$1" "$2" 2>/dev/null
    }
    SERVICE=pane_kick_service
    stand_env bash -c 'source "$1"; declare -F pane_kick_service >/dev/null' _ "$PANE_LIB" 2>/dev/null || SERVICE=pane_kick
    echo "  служебная дверь: $SERVICE (pane_kick — сегодняшняя дверь limit-watch)"

    # (а) БОЛЬНОЙ: писатель А — толчок о сообщении с аккордом (ключ 1 стенда),
    # писатель Б — служебный толчок (сторож, limit-watch) со сдвигом 0–0,95 с.
    GLUE=0; CHORD_BROKEN=0; BOTH=0; CHORD_OK=0; FREE_ALL=0; KEYS_ALL=0; SAMPLE=""
    DOOR_DELAY=0.15; DOOR_FAIL_CS=""
    for i in $(seq 0 $((RACE_N - 1))); do
        door_stand "race$i" 1
        off=$(awk -v i="$i" 'BEGIN { printf "%.2f", i * 0.05 }')
        ( door_write pane_kick_owner "проба-писатель-A сообщение владельца" > "$D/a.out" ) &
        pa=$!
        sleep "$off"
        ( door_write "$SERVICE" "проба-писатель-Б служебный толчок" > "$D/b.out" ) &
        pb=$!
        wait "$pa" "$pb"
        v=$(an race "$D/tmux-calls" писатель-A писатель-Б)
        case "$v" in
            склейка:*) GLUE=$((GLUE + 1)); [ -n "$SAMPLE" ] || SAMPLE="$v (сдвиг $off с)" ;;
            аккорд-разорван:*) CHORD_BROKEN=$((CHORD_BROKEN + 1)); [ -n "$SAMPLE" ] || SAMPLE="$v (сдвиг $off с)" ;;
        esac
        [ "$(an texts "$D/tmux-calls" писатель-A)/$(an texts "$D/tmux-calls" писатель-Б)" = 1/1 ] && BOTH=$((BOTH + 1))
        [ "$(an texts "$D/tmux-calls" писатель-A)" = 1 ] && [ "$(an chords "$D/tmux-calls")" = 1/1 ] \
            && CHORD_OK=$((CHORD_OK + 1))
        read -r fr kk <<< "$(an lockfree "$D/tmux-calls")"
        FREE_ALL=$((FREE_ALL + fr)); KEYS_ALL=$((KEYS_ALL + kk))
    done
    [ -n "$SAMPLE" ] && echo "  образец поломки: $SAMPLE (A/Б — тексты, E — Enter, X/S — C-x/C-s)"
    case_check "БОЛЬНОЙ (а) П-5: тексты двух писателей склеились до Enter — 0 из $RACE_N" "0" "$GLUE"
    case_check "БОЛЬНОЙ (а) П-5: чужое нажатие между C-x и C-s — 0 из $RACE_N" "0" "$CHORD_BROKEN"
    case_check "СТЕНД (а): оба текста дошли по одному разу — $RACE_N из $RACE_N" "$RACE_N" "$BOTH"
    case_check "(а) §4.0 п.1: аккорд отдаёт дверь сообщений под тем же локом (ключ 1) — $RACE_N из $RACE_N" "$RACE_N" "$CHORD_OK"
    case_check "СТЕНД (а): нажатий за $RACE_N попыток больше нуля" "да" "$([ "$KEYS_ALL" -gt 0 ] && echo да || echo нет)"
    case_check "БОЛЬНОЙ (а) §4.0: нажатий мимо pane.lock — 0 из $KEYS_ALL" "0" "$FREE_ALL"

    # Ключ 0 (живое значение по слову владельца 27.10): аккорда нет вовсе.
    DOOR_DELAY=""; door_stand key0 0
    door_write pane_kick_owner "проба-писатель-A ключ 0" > /dev/null
    case_check "PANE_SEND_NOW=0 — дверь отдаёт текст" "1" "$(an texts "$D/tmux-calls" писатель-A)"
    case_check "PANE_SEND_NOW=0 — аккорда C-x/C-s нет" "0/0" "$(an chords "$D/tmux-calls")"

    # П-6: C-s отказал — лок держится не меньше 3 с после C-x (окно аккорда в
    # Claude Code 2,5–3 с: иначе следующий текст съеден оборванным C-x).
    DOOR_FAIL_CS=1; door_stand chordfail 1
    ( door_write pane_kick_owner "проба-писатель-A оборванный аккорд" > "$D/a.out" ) &
    pa=$!
    sleep 0.2
    ( door_write "$SERVICE" "проба-писатель-Б после обрыва" > "$D/b.out" ) &
    pb=$!
    wait "$pa" "$pb"
    DOOR_FAIL_CS=""
    GAP=$(an gap "$D/tmux-calls" писатель-Б)
    case_check "П-6: оборванный аккорд — текст второго писателя не раньше 3 с после C-x" "да" \
        "$(awk -v g="$GAP" 'BEGIN { print (g ~ /^[0-9.]+$/ && g + 0 >= 3.0) ? "да" : "нет (" g ")" }')"
fi

# ═══ 2. СТОРОЖ: ветка 6а целым прогоном ═════════════════════════════════════
if want сторож; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 2: session-warden.sh целым прогоном (6а, пульс, лок)"
    W="$T/warden"
    mkdir -p "$W/project/scripts/lib" "$W/projects"
    for f in work-queue.py limit_mark.py limit-in-log.py stall-in-log.py inbox-digest.py urgency.py; do
        [ -f "$KOREN/scripts/$f" ] && cp "$KOREN/scripts/$f" "$W/project/scripts/"
    done
    cp "$KOREN"/scripts/lib/*.py "$KOREN/scripts/lib/config.sh" "$W/project/scripts/lib/"
    mkdir -p "$W/project/harness/config"
    cp "$KOREN/harness/config/queue.yaml" "$W/project/harness/config/" 2>/dev/null \
        || cp "$KOREN/config/queue.yaml" "$W/project/harness/config/" 2>/dev/null || true
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
    cp "$CONF_SEED" "$W/harness.conf"
    # INBOX_WAIT_MAX_MIN — живое умолчание (30): при нём сегодняшний сторож держит
    # пятиминутное несрочное сообщение «до рубежа».
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
INBOX_WAIT_MAX_MIN=30
ROTATE_FINISH_TIMEOUT_SEC=900
PANE_ENTER_DELAY=0
PANE_TMUX_TIMEOUT=3
PANE_SEND_NOW=0
TG_CHAT_ID=""
TG_TOKEN_FILE="/nonexistent"
CONF
    PANEL_W="$W/state/panel-state"
    INBOX_W="$W/log/inbox"
    W_LOCK="$W/log/locks/проверка/pane.lock"
    : > "$W/all-calls"

    ts_ago() { date -u -d "-$1 min" +%Y-%m-%dT%H:%M:%SZ; }
    tr_ok() {
        printf '{"type":"assistant","timestamp":"%s"}\n' "$(ts_ago "$1")" > "$TRANSCRIPT"
        touch -d "-$1 min" "$TRANSCRIPT"
    }
    tr_refusal() {
        printf '{"type":"assistant","timestamp":"%s","error":"rate_limit","apiErrorStatus":429,"isApiErrorMessage":true,"message":{"model":"<synthetic>","role":"assistant","content":[{"type":"text","text":"You'"'"'ve hit your session limit · resets 6:10am"}]}}\n' \
            "$(ts_ago "$1")" >> "$TRANSCRIPT"
    }
    tr_limit() { tr_ok 60; tr_refusal 1; touch -d '-1 min' "$TRANSCRIPT"; }
    tr_resumed() {
        tr_ok 60; tr_refusal 10
        printf '{"type":"assistant","timestamp":"%s"}\n' "$(ts_ago 0)" >> "$TRANSCRIPT"
    }
    limit_held() { LOG_DIR="$W/log" python3 "$LIMIT_MARK" --лимит-держится >/dev/null 2>&1 && echo да || echo нет; }

    w_stand() {
        [ -f "$W/tmux-calls" ] && cat "$W/tmux-calls" >> "$W/all-calls"
        WRUN=0; W_UNREACHED=0
        rm -rf "$W/log" "$W/state" "$W/kanal.txt" "$W/kanal-held.txt" "$W/tmux-calls" "$W/alert-task.txt" "$W/warden.out"
        mkdir -p "$INBOX_W/обработано" "$PANEL_W"
        : > "$W/kanal.txt"; : > "$W/kanal-held.txt"; : > "$W/tmux-calls"; : > "$W/alert-task.txt"; : > "$W/warden.out"
        printf '%s\n' '{"queue": [], "lines": []}' > "$W/project/dev-map.yaml"
        tr_ok 1
        : > "$W/log/rotation.jsonl"
        MSG_EPOCH=$(( $(date +%s) - 300 ))
        MSG_FILE="$INBOX_W/$MSG_EPOCH.000000001.txt"
        printf '%s\n' "$WORK_TEXT" > "$MSG_FILE"
        touch -d "@$MSG_EPOCH" "$MSG_FILE"
    }
    w_run() {
        local rc=0
        WRUN=$((WRUN + 1))
        stand_env PROBA_TMUX_LOG="$W/tmux-calls" PROBA_PANE_LOCK="$W_LOCK" \
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
    w_reached() { case_check "СТЕНД ($1): все $WRUN прогонов прошли 6а и дошли до 6б" "0" "$W_UNREACHED"; }
    in_inbox() { [ -e "$MSG_FILE" ] && echo есть || echo нет; }
    w_owner() { an texts "$W/tmux-calls" "$INBOX_W"; }
    w_signals() {
        local n; n=$(grep -F -- "$(date -d "@$MSG_EPOCH" +%H:%M)" "$W/kanal.txt" | grep -c -- '--task'); echo "${n:-0}"
    }
    w_hold_lines() { n_lines "$W/warden.out" 'шаг не прерываю'; }

    # (б) БОЛЬНОЙ: сообщение со словами работы лежит 5 мин при пороге 30, ход не
    # завершён. Сегодня решение «ждём» — сессия узнает через 30 мин.
    echo "— (б) несрочное сообщение 5 мин, порог 30, ход идёт"
    w_stand; w_run
    case_check "СТЕНД (б): urgency в своде молчит — сообщение несрочное" "0" \
        "$(python3 "$KOREN/scripts/inbox-digest.py" --инбокс "$INBOX_W" --сколько-срочных 2>/dev/null || echo ?)"
    case_check "БОЛЬНОЙ (б) §4.4 п.1: первый же круг — один толчок о сообщении (без «ждём»)" "1" "$(w_owner)"
    case_check "БОЛЬНОЙ (б): строки «шаг не прерываю, их подаст свод на рубеже» нет" "0" "$(w_hold_lines)"
    w_reached "несрочное"

    # (е) П-13: потолок и сигнал — пять кругов, сессия файл не берёт.
    echo "— (е) потолок повторов: пять кругов"
    w_stand; for _ in 1 2 3 4 5; do w_run; done
    case_check "(е) П-13: за пять кругов толчков ровно 3 (потолок на файл)" "3" "$(w_owner)"
    case_check "(е) П-13: после потолка — один сигнал владельцу с временем сообщения и задачей модели" "1" "$(w_signals)"
    case_check "(е) потолок не удаляет сообщение" "есть" "$(in_inbox)"
    w_reached "потолок"

    # (д) пауза — отложено, не пропущено.
    echo "— (д) пауза: два круга, потом снята"
    w_stand; pause_on "$PANEL_W"; w_run; w_run
    case_check "(д) пауза: толчков о сообщении 0 за два круга" "0" "$(w_owner)"
    case_check "(д) пауза: нажатий текста в панель 0" "0" "$(an texts "$W/tmux-calls" ' ')"
    case_check "(д) пауза: сообщение во входящих" "есть" "$(in_inbox)"
    BEFORE=$(w_owner); pause_off "$PANEL_W"; w_run
    case_check "(д) пауза снята — на ближайшем круге один толчок" "1" "$(( $(w_owner) - BEFORE ))"
    w_reached "пауза"

    echo "— (д) лимит: два круга, потом подъём"
    w_stand; tr_limit; limit_on "$W/log" limit; w_run; w_run
    case_check "СТЕНД: метка лимита держится после кругов" "да" "$(limit_held)"
    case_check "(д) лимит: толчков о сообщении 0" "0" "$(w_owner)"
    BEFORE=$(w_owner); tr_resumed; w_run
    case_check "СТЕНД: лимит снят судьёй сторожа" "нет" "$(limit_held)"
    case_check "(д) лимит снят — на ближайшем круге один толчок" "1" "$(( $(w_owner) - BEFORE ))"
    w_reached "лимит"

    echo "— (д) ротация в ходу, потом завершена"
    w_stand; rotation_event "$W/log" rotate_command 30; w_run
    case_check "(д) ротация в ходу — толчков о сообщении 0" "0" "$(w_owner)"
    BEFORE=$(w_owner); rotation_event "$W/log" agent_restarted 5; w_run
    case_check "(д) ротация завершена — один толчок" "1" "$(( $(w_owner) - BEFORE ))"
    w_reached "ротация"

    # Все нажатия сторожа за все круги (толчки 6а, пульс 6в) — под pane.lock.
    cat "$W/tmux-calls" >> "$W/all-calls"
    read -r fr kk <<< "$(an lockfree "$W/all-calls")"
    case_check "СТЕНД: сторож нажимал в панель (нажатий больше нуля)" "да" "$([ "$kk" -gt 0 ] && echo да || echo нет)"
    case_check "БОЛЬНОЙ (а): нажатий сторожа мимо pane.lock — 0 из $kk" "0" "$fr"
fi

# ═══ 3. LIMIT-WATCH: подъём после лимита ════════════════════════════════════
if want лимит; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 3: limit-watch.sh целым прогоном (подъём в момент сброса)"
    L="$T/limit"
    LP="$L/project"
    mkdir -p "$LP/scripts/lib" "$L/log" "$L/panel" "$L/tr/${LP//\//-}"
    for f in limit_mark.py limit-in-log.py stall-in-log.py pause.py; do cp "$KOREN/scripts/$f" "$LP/scripts/"; done
    cp "$KOREN"/scripts/lib/*.py "$KOREN/scripts/lib/config.sh" "$LP/scripts/lib/"
    printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >> "%s/kanal.txt"\n' "$L" > "$LP/scripts/tg_send.sh"
    chmod +x "$LP/scripts/tg_send.sh"
    printf 'PROJECT_DIR="%s"\nLOG_DIR="%s"\nTMUX_SESSION="%s"\nTG_CHAT_ID="100500"\nTG_TOKEN_FILE="/nonexistent"\nPANEL_STATE_DIR="%s"\nOWNER_TZ=""\nPANE_SEND_NOW="0"\n' \
        "$LP" "$L/log" "$STAMP-limit" "$L/panel" > "$L/install.conf"
    : > "$L/tmux-calls"
    python3 - "$L/tr/${LP//\//-}/proba.jsonl" <<'ZH'
import datetime, json, sys, time
def mark(sec):
    return datetime.datetime.fromtimestamp(time.time() - sec, datetime.UTC).isoformat()
rows = [{"type": "assistant", "timestamp": mark(1500)},
        {"apiErrorStatus": 429, "timestamp": mark(60), "resetsAt": int(time.time()) + 7200}]
open(sys.argv[1], "w", encoding="utf-8").write("\n".join(json.dumps(r) for r in rows) + "\n")
ZH
    LOG_DIR="$L/log" python3 "$LIMIT_MARK" --поставить limit --сброс "$(( $(date +%s) - 5 ))" >/dev/null
    stand_env PROBA_TMUX_LOG="$L/tmux-calls" PROBA_PANE_LOCK="$L/log/locks/services/pane.lock" \
        LOG_DIR="$L/log" PROJECT_DIR="$LP" HARNESS_INSTALL_CONF="$L/install.conf" HARNESS_CONF="$L/install.conf" \
        HEARTBEAT_DIR="$L/log/heartbeat" CLAUDE_PROJECTS_DIR="$L/tr" HARNESS_PANEL_STATE="$L/panel" \
        PANE_ENTER_DELAY=0 LIMIT_KICK_DELAY_SEC=0 \
        timeout -k 5 60 bash "$LIMIT_WATCH" > "$L/run.out" 2>&1 < /dev/null
    case_check "СТЕНД: limit-watch дал толчок подъёма (текст сторожа лимита в панели)" "1" \
        "$(an texts "$L/tmux-calls" 'сторож лимита')"
    read -r fr kk <<< "$(an lockfree "$L/tmux-calls")"
    case_check "БОЛЬНОЙ (а): нажатий limit-watch мимо pane.lock — 0 из $kk" "0" "$fr"
    [ "$kk" -gt 0 ] || tail -5 "$L/log/limit-watch.log" 2>/dev/null | sed 's/^/    /'
fi

# ═══ 4. ДИСПЕТЧЕР: целым циклом с подставным Bot API ════════════════════════
if want диспетчер; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 4: tg-dispatcher.sh целым циклом"
    cat > "$T/api.py" <<'PYEOF'
import json, sys, time, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
port, acks, ready, upd = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
UPDATES = json.load(open(upd, encoding="utf-8"))
def update(u):
    return {"update_id": u["id"], "message": {"message_id": u["id"], "chat": {"id": 777000}, "text": u["text"]}}
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
            ids = [u for u in UPDATES if u["id"] >= off]
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
    # Держатель лока: оба имени (сегодняшнее pane-kick.lock и общее pane.lock §4.0).
    cat > "$T/holder.py" <<'PYEOF'
import fcntl, os, sys, time
fs = [open(p, "a") for p in sys.argv[3:]]
for f in fs:
    fcntl.flock(f, fcntl.LOCK_EX)
open(sys.argv[2], "w").close()
while not os.path.exists(sys.argv[1]):
    time.sleep(0.05)
PYEOF
    offset_is() { [ "$(cat "$1/log/tg_offset" 2>/dev/null)" = "$2" ]; }
    D_COMMENT_TS="1761500000.5"
    : > "$T/disp-all-calls"
    d_bad() { D_STAND_BAD=$((D_STAND_BAD + 1)); echo "  СТЕНД: $1"; }
    d_reached() { case_check "СТЕНД ($1): диспетчер разобрал все сообщения" "0" "$D_STAND_BAD"; }

    # $1 = имя, $2 = «id:текст;id:текст…» (пусто — сообщений нет), дальше флаги:
    # hold (держать лок, пока разбираются все), pause, rotation, limit, comment, key1.
    d_case() {
        local name="$1" spec="$2" S port lockdir last=0 flag key=0 hold="" comment=""
        shift 2
        S="$T/disp-$name"; DS="$S"; D_STAND_BAD=0
        mkdir -p "$S/log" "$S/project" "$S/panel"
        for flag in "$@"; do
            case "$flag" in key1) key=1 ;; hold) hold=1 ;; comment) comment=1 ;; esac
        done
        python3 - "$spec" "$S/updates.json" <<'PYEOF'
import json, sys
items = [p.split(":", 1) for p in sys.argv[1].split(";") if p]
json.dump([{"id": int(i), "text": t} for i, t in items], open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
PYEOF
        last=$(python3 -c 'import json,sys; u=json.load(open(sys.argv[1])); print(max([x["id"] for x in u] or [0]))' "$S/updates.json")
        if [ -n "$comment" ]; then
            mkdir -p "$S/panel/comments/new"
            printf '{"id":"hr.proba","текст":"%s"}' "$WORK_TEXT" > "$S/panel/comments/new/$D_COMMENT_TS.json"
        fi
        : > "$S/tmux-calls"
        printf '000:СТЕНДОВЫЙ' > "$S/token"   # не-секрет: подставной Bot API стенда
        port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
        PROBA_STAND="$T/" python3 "$T/api.py" "$port" "$S/acks" "$S/api.ready" "$S/updates.json" > "$S/api.out" 2>&1 &
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
        printf 'TMUX_SESSION="%s"\nSERVICE_LOCKS_SUBDIR="services"\nPANE_TMUX_TIMEOUT="5"\nPANE_ENTER_DELAY="0.1"\nPANE_SEND_NOW="%s"\n' \
            "$STAMP-disp" "$key" > "$S/harness.conf"
        lockdir="$S/log/locks/services"; mkdir -p "$lockdir"
        if [ -n "$hold" ]; then
            PROBA_STAND="$T/" python3 "$T/holder.py" "$S/release" "$S/held" "$lockdir/pane-kick.lock" "$lockdir/pane.lock" &
            PIDS_KILL+=($!); disown $!
            wait_for 5 test -e "$S/held" || { d_bad "лок толчков не взят"; return; }
        fi
        for flag in "$@"; do
            case "$flag" in
                pause) pause_on "$S/panel" ;;
                rotation) rotation_event "$S/log" rotate_command 30 ;;
                limit) limit_on "$S/log" limit ;;
            esac
        done
        stand_env PROBA_TMUX_LOG="$S/tmux-calls" PROBA_PANE_LOCK="$lockdir/pane.lock" \
            HARNESS_PANEL_STATE="$S/panel" \
            HARNESS_INSTALL_CONF="$S/install.conf" HARNESS_CONF="$S/harness.conf" \
            TG_API_BASE="http://127.0.0.1:$port" \
            setsid nohup bash "$DISPATCHER" > "$S/dispatcher.out" 2>&1 < /dev/null &
        DPID=$!
        PIDS_KILL+=("$DPID"); disown "$DPID"
        if [ "$last" -gt 0 ]; then
            wait_for 40 offset_is "$S" $((last + 1)) \
                || { d_bad "диспетчер не разобрал update $last за 40 с"; tail -5 "$S/log/tg-dispatcher.log" 2>/dev/null | sed 's/^/    /'; }
        fi
        [ -n "$hold" ] && { sleep 0.5; : > "$S/release"; }
        sleep 3
        wait_quiet "$S/tmux-calls" 3 30
        kill -KILL -- "-$DPID" 2>/dev/null; kill -KILL "$DPID" 2>/dev/null
        cat "$S/tmux-calls" >> "$T/disp-all-calls"
        DLOG="$S/log/tg-dispatcher.log"
    }
    inbox_ts() { find "$1/log/inbox" -maxdepth 1 -name '*.txt' -printf '%f\n' 2>/dev/null | sed 's/\.txt$//' | sort -n; }
    d_inbox_n() { inbox_ts "$1" | grep -c . ; }

    # Контроль: сообщение со словом срочности толкается и сегодня — на нём видно,
    # что стенд жмёт в панель и что лок судится не на пустом наборе нажатий.
    echo "— контроль: срочное сообщение"
    d_case control "101:срочно, $WORK_TEXT"; d_reached "контроль"
    case_check "СТЕНД (контроль): срочное сообщение дало толчок в панель" "1" \
        "$(an texts "$DS/tmux-calls" "$DS/log/inbox/")"

    # (б) БОЛЬНОЙ П-1: одно сообщение со словами работы, смена занята (карты нет —
    # сегодняшний smena_zanyata считает «занята»). Толчок ≤ 2 с от приёма.
    echo "— (б) сообщение со словами работы, смена занята"
    d_case work "201:$WORK_TEXT"; d_reached "работа"
    TS1=$(inbox_ts "$DS" | head -1)
    case_check "СТЕНД (б): сообщение легло во входящие" "1" "$(d_inbox_n "$DS")"
    KT=$(an kick_time "$DS/tmux-calls" "$DS/log/inbox/$TS1.txt")
    case_check "БОЛЬНОЙ (б) П-1: толчок о сообщении в панели не позже 2 с от приёма" "да" \
        "$(awk -v k="$KT" -v t="$TS1" 'BEGIN { if (k == "нет") print "нет (толчка нет)"; else print (k - t <= 2.0) ? "да" : sprintf("нет (%.2f с)", k - t) }')"
    case_check "БОЛЬНОЙ (б): строки «несрочное — шаг агента не прерываю» нет" "0" "$(n_lines "$DLOG" 'шаг агента не прерываю')"
    case_check "PANE_SEND_NOW=0 на стенде — аккорда нет" "0/0" "$(an chords "$DS/tmux-calls")"

    # (в) комментарий панели со словами работы при занятой смене.
    echo "— (в) комментарий панели, смена занята"
    d_case comment "" comment
    case_check "СТЕНД (в): приём выдал заявку (о ней есть строка или толчок)" "да" \
        "$({ grep -q "комментарий $D_COMMENT_TS" "$DLOG" 2>/dev/null || [ "$(an texts "$DS/tmux-calls" 'Комментарий владельца')" -gt 0 ]; } && echo да || echo нет)"
    case_check "БОЛЬНОЙ (в): комментарий панели отдан в панель (не удержан)" "1" "$(an texts "$DS/tmux-calls" 'Комментарий владельца')"
    case_check "БОЛЬНОЙ (в): строки «ждёт: несрочный» нет" "0" "$(n_lines "$DLOG" 'несрочный')"

    # (г) П-4: 10 сообщений за раз, лок занят, пока все разбираются: толчки ждут и
    # срываются разом — flock без FIFO переставил бы их.
    echo "— (г) 10 сообщений подряд, лок занят на время разбора"
    SPEC=""
    for i in $(seq 1 10); do SPEC="$SPEC;$((300 + i)):$WORK_TEXT, пункт $i"; done
    d_case order "$SPEC" hold; d_reached "10 подряд"
    EXP=$(inbox_ts "$DS" | tr '\n' ' ' | sed 's/ $//')
    GOT=$(an order "$DS/tmux-calls" "$DS/log/inbox")
    case_check "СТЕНД (г): во входящих 10 файлов" "10" "$(d_inbox_n "$DS")"
    case_check "СТЕНД (г): ни один толчок не сдался на занятом локе" "0" "$(n_lines "$DLOG" 'очередь толчков занята')"
    case_check "(г) П-4: каждое из 10 сообщений названо в толчках" "10" \
        "$(python3 -c 'import sys; e=sys.argv[1].split(); g=sys.argv[2].split(); print(sum(1 for x in e if x in g))' "$EXP" "$GOT")"
    case_check "(г) П-4: ни одно не названо дважды" "0" \
        "$(python3 -c 'import sys; g=sys.argv[1].split(); print(len(g) - len(set(g)))' "$GOT")"
    case_check "(г) П-4: сообщения в панели по возрастанию времени приёма" "да" \
        "$(python3 -c 'import sys; g=[float(x) for x in sys.argv[1].split()]; print("да" if g and g == sorted(g) else "нет")' "$GOT")"

    # (д) пауза, ротация, лимит — отложено: толчка нет, файл ждёт, отказ назван.
    for gate in pause rotation limit; do
        case "$gate" in pause) word=пауза ;; rotation) word=ротация ;; limit) word=лимит ;; esac
        echo "— (д) $word: сообщение со словами работы"
        d_case "gate-$gate" "401:$WORK_TEXT" "$gate"; d_reached "$word"
        case_check "(д) $word: текстов в панель 0" "0" "$(an texts "$DS/tmux-calls" ' ')"
        case_check "(д) $word: сообщение во входящих (отложено, не потеряно)" "1" "$(d_inbox_n "$DS")"
        if [ "$gate" != pause ]; then
            # На паузе сообщение раньше двери держит свой гейт (tg-dispatcher.sh «стоит
            # пауза — сообщение легло во входящие»); ротацию и лимит судит дверь.
            case_check "(д) $word: отказ двери назван в журнале («не отдан: $word»)" "да" \
                "$(grep -q "не отдан: $word" "$DLOG" 2>/dev/null && echo да || echo нет)"
        fi
    done

    read -r fr kk <<< "$(an lockfree "$T/disp-all-calls")"
    case_check "СТЕНД: диспетчер нажимал в панель (нажатий больше нуля)" "да" "$([ "$kk" -gt 0 ] && echo да || echo нет)"
    case_check "БОЛЬНОЙ (а): нажатий диспетчера мимо pane.lock — 0 из $kk" "0" "$fr"
fi

# ═══ 5. КОД: второй носитель ════════════════════════════════════════════════
if want код; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 5: код — носители перехвата, писатели мимо двери, умолчание ключа"
    # Блок --selftest и строки-комментарии не судятся: самотест живёт на своих
    # одноразовых сессиях tmux, не в панели агента.
    cat > "$T/code.py" <<'PYEOF'
import re, sys
WRITER = re.compile(r"(^|[\s;&|({!])(tmux\s+send-keys|send_to_pane|pane_send_now|pane_tmux\s+send-keys)\b")
DOOR = re.compile(r"\b(pane_kick_owner|pane_kick_service|pane_command)\b")
def body(path):
    lines = open(path, encoding="utf-8").read().splitlines()
    out, skip = [], False
    for n, line in enumerate(lines, 1):
        if re.match(r'^if \[ "\$\{1:-\}" = "--selftest" \]; then', line):
            skip = True; continue
        if skip:
            if re.match(r"^fi\s*$", line):
                skip = False
            continue
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append((n, line))
    return out
mode, files = sys.argv[1], sys.argv[2:]
if mode == "writers":
    bad, doors = [], 0
    for f in files:
        b = body(f)
        bad += [f"{f.rsplit('/', 1)[-1]}:{n}" for n, l in b if WRITER.search(l)]
        doors += sum(1 for _, l in b if DOOR.search(l))
    print(f"{len(bad)}|{doors}|{' '.join(bad[:12])}")
elif mode == "count":   # count <регэксп> <файл…>: строки не-комментарии вне самотеста
    rx = re.compile(files[0])
    hits = [f"{f.rsplit('/', 1)[-1]}:{n}" for f in files[1:] for n, l in body(f) if rx.search(l)]
    print(f"{len(hits)}|{' '.join(hits[:12])}")
elif mode == "anywhere":  # во всём файле, кроме комментариев (и в самотесте тоже)
    rx = re.compile(files[0])
    hits = []
    for f in files[1:]:
        for n, l in enumerate(open(f, encoding="utf-8").read().splitlines(), 1):
            if l.strip().startswith("#"):
                continue
            if rx.search(l):
                hits.append(f"{f.rsplit('/', 1)[-1]}:{n}")
    print(f"{len(hits)}|{' '.join(hits[:12])}")
PYEOF
    IFS='|' read -r BAD DOORS WHERE <<< "$(python3 "$T/code.py" writers "$WARDEN" "$DISPATCHER" "$LIMIT_WATCH" "$MODEL_SH")"
    echo "  файлов-писателей судимо: 4 (session-warden.sh, tg-dispatcher.sh, limit-watch.sh, model.sh); зовов двери: $DOORS"
    [ "$BAD" = 0 ] || echo "  мимо двери: $WHERE"
    case_check "(а) писателей в панель мимо двери (tmux send-keys / send_to_pane / pane_send_now вне pane.sh) — 0" "0" "$BAD"
    case_check "(а) двери зовутся (набор не пуст: pane_kick_owner|pane_kick_service|pane_command)" "да" \
        "$([ "${DOORS:-0}" -gt 0 ] && echo да || echo нет)"
    for fw in "$WARDEN" "$DISPATCHER" "$LIMIT_WATCH" "$MODEL_SH"; do
        IFS='|' read -r BAD DOORS WHERE <<< "$(python3 "$T/code.py" writers "$fw")"
        case_check "(а) $(basename "$fw") зовёт дверь хотя бы раз" "да" "$([ "${DOORS:-0}" -gt 0 ] && echo да || echo нет)"
    done
    IFS='|' read -r N WHERE <<< "$(python3 "$T/code.py" anywhere 'urgency\.py|URGENCY_PY|smena_zanyata' "$DISPATCHER")"
    [ "$N" = 0 ] || echo "  носители: $WHERE"
    case_check "БОЛЬНОЙ К-3(а): в диспетчере нет urgency.py / smena_zanyata (вызовов и определений)" "0" "$N"
    IFS='|' read -r N WHERE <<< "$(python3 "$T/code.py" anywhere 'несрочн' "$DISPATCHER")"
    [ "$N" = 0 ] || echo "  носители: $WHERE"
    case_check "БОЛЬНОЙ К-3(а): в диспетчере нет ветки «несрочное/несрочный»" "0" "$N"
    IFS='|' read -r N WHERE <<< "$(python3 "$T/code.py" anywhere 'reshenie_po_vhodyashchim|INBOX_RESHENIE|сколько-срочных' "$WARDEN")"
    [ "$N" = 0 ] || echo "  носители: $WHERE"
    case_check "БОЛЬНОЙ К-3(а): в стороже нет решения «ждём» по входящим" "0" "$N"
    # Слово владельца 27.10: ключ остаётся 0, пока не подтвердится, что оркестратор
    # сам ничего не делает. Фаза ключ не включает.
    case_check "умолчание пакета PANE_SEND_NOW=0 (фаза ключ не включает)" "1" \
        "$(grep -cE '^PANE_SEND_NOW="?0"?\s*(#.*)?$' "$CONF_SEED")"
fi

# ═══ 6. MODEL.SH: «/model» и подтверждение — одна последовательность ═════════
# Диспетчер на «модель opus» зовёт model.sh фоном и сразу толкает то же
# сообщение. Лок на каждое нажатие отдельно отпускается на время ожидания
# диалога, и толчок с Enter встаёт между «/model» и подтверждением: Enter
# толчка отвечает диалогу, сообщение съедено, но помечено отданным. Какая
# поломка оставит случай зелёным? Толчок, пришедший раньше «/model», — поэтому
# толчок подаётся ПОСЛЕ того, как текст «/model» лёг в журнал подделки, со
# сдвигом внутри окна диалога, и проба требует, чтобы K стоял после M.
if want модель; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 6: model.sh --только-сессия против толчка о сообщении (порядок нажатий)"
    MODEL_OFFSETS="0.0 0.3 0.6 0.9"
    M_SPLIT=0; M_OK=0; M_LATE=0; M_TRIES=0; M_MODEL_DONE=0; M_SAMPLE=""
    for off in $MODEL_OFFSETS; do
        M="$T/model-$M_TRIES"; M_TRIES=$((M_TRIES + 1))
        rm -rf "$M"; mkdir -p "$M/log/locks/services" "$M/log/inbox" "$M/panel"
        : > "$M/tmux-calls"
        printf 'LOG_DIR="%s"\nSERVICE_LOCKS_SUBDIR="services"\nPANE_SEND_NOW="0"\n' "$M/log" > "$M/harness.conf"
        MTS="$(date +%s).$(date +%N)"
        echo "модель opus" > "$M/log/inbox/$MTS.txt"
        m_env() {
            stand_env PROBA_TMUX_LOG="$M/tmux-calls" PROBA_PANE_LOCK="$M/log/locks/services/pane.lock" \
                PROBA_MODEL_DIALOG=1.0 TMUX_SESSION="$STAMP-model" LOG_DIR="$M/log" \
                SERVICE_LOCKS_SUBDIR=services HARNESS_CONF="$M/harness.conf" HARNESS_INSTALL_CONF="$M/harness.conf" \
                HARNESS_PANEL_STATE="$M/panel" PANE_ENTER_DELAY=0.3 PANE_TMUX_TIMEOUT=5 PANE_SEND_NOW=0 "$@"
        }
        ( m_env timeout -k 2 40 bash "$MODEL_SH" --только-сессия opus > "$M/model.out" 2>&1 ) &
        pm=$!
        wait_for 10 grep -qF -- ' -l -- /model' "$M/tmux-calls" \
            || echo "  СТЕНД ($off): «/model» не лёг в журнал подделки за 10 с"
        sleep "$off"
        ( m_env timeout -k 2 40 bash -c 'source "$1" || exit 3; rc=0; pane_kick_owner "$2" || rc=$?; echo "rc=$rc [${PANE_REFUSAL:-}]"' \
            _ "$PANE_LIB" "Владелец написал: модель opus — полная версия в $M/log/inbox/$MTS.txt, прочитай" \
            > "$M/kick.out" 2>&1 ) &
        pk=$!
        wait "$pm" "$pk"
        grep -q 'модель сессии: opus' "$M/model.out" && M_MODEL_DONE=$((M_MODEL_DONE + 1))
        v=$(an modelseq "$M/tmux-calls" "/model opus" "Владелец написал")
        case "$v" in
            ok:*)     M_OK=$((M_OK + 1)) ;;
            разрыв:*) M_SPLIT=$((M_SPLIT + 1)); [ -n "$M_SAMPLE" ] || M_SAMPLE="$v (сдвиг $off с)" ;;
            *)        [ -n "$M_SAMPLE" ] || M_SAMPLE="$v (сдвиг $off с)" ;;
        esac
        case "${v#*:}" in *M*K*) M_LATE=$((M_LATE + 1)) ;; esac
    done
    [ -n "$M_SAMPLE" ] && echo "  образец: $M_SAMPLE (M — «/model», K — толчок, E — Enter)"
    case_check "СТЕНД: model.sh довёл смену до конца — $M_TRIES из $M_TRIES" "$M_TRIES" "$M_MODEL_DONE"
    case_check "СТЕНД: толчок подан после «/model» (гонка создана) — $M_TRIES из $M_TRIES" "$M_TRIES" "$M_LATE"
    case_check "БОЛЬНОЙ MAJOR-2: толчок встал между «/model» и Enter подтверждения — 0 из $M_TRIES" "0" "$M_SPLIT"
    case_check "§4.0 п.1: «/model» Enter Enter подряд, толчок отдан один раз — $M_TRIES из $M_TRIES" "$M_TRIES" "$M_OK"
fi

echo "судимых путей: $PATHS_JUDGED"
[ "$CASES_FAILED" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: путей $PATHS_JUDGED, случаев $CASES_PATHS"; exit 0; }
echo "ПРОБА КРАСНАЯ: путей $PATHS_JUDGED, неудач $CASES_FAILED из $CASES_PATHS"; exit 1
