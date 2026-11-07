#!/usr/bin/env bash
# Одна дверь в панель агента: нажатия в tmux и признак «кто в панели».
#
# ЗАЧЕМ ОДНА. Эти семь строк жили двумя побайтово равными копиями — в
# session-warden.sh и tg-dispatcher.sh. Копий стало бы три, когда пинок
# понадобился ежеминутному limit-watch.sh (владелец 17.10.2026: «дернуть он
# должен ровно в тот момент когда лимит обновится»), а две копии одного
# признака расходятся молча.
#
# Ждёт в окружении: TMUX_SESSION (имя панели), PANE_ENTER_DELAY (пауза перед
# Enter, по умолчанию 0,7 с), PANE_TMUX_TIMEOUT (предел одного вызова tmux,
# по умолчанию 10 с).

# Единственная дверь к tmux. Предел нужен потому, что tmux-клиент не имеет
# своего: 25.10.2026 на чужом сервере `send-keys` висел 57 минут, а звавший его цикл
# приёма канала всё это время не читал сообщения владельца. `-k 2` — на случай,
# когда клиент в неотвечающем состоянии не реагирует на TERM.
#
# Вывод команды остаётся в PANE_OUT, а не в stdout: подстановка `$(...)` — это
# подоболочка, и причина зависания (PANE_HUNG) из неё не доехала бы до звонящего.
# Поэтому звать дверь надо БЕЗ подстановки.
pane_tmux() {  # подкоманда tmux и её доводы
    local limit="${PANE_TMUX_TIMEOUT:-10}" rc=0
    # Дескрипторы лока двери и лока сторожа (8) потомку закрываются: tmux-клиент,
    # поднявший сервер, держал бы их вечно (память lock-leaked-into-a-child).
    if [ -n "${PANE_LOCK_FD:-}" ]; then
        PANE_OUT=$(timeout -k 2 "$limit" tmux "$@" {PANE_LOCK_FD}>&- 8>&-) || rc=$?
    else
        PANE_OUT=$(timeout -k 2 "$limit" tmux "$@" 8>&-) || rc=$?
    fi
    case "$rc" in
        124|137) PANE_HUNG="tmux $1 не вернулся за $limit с (код $rc)" ;;
    esac
    return "$rc"
}

# Текст в панель. Перевод строки заменяется пробелом: tmux принял бы его за
# отдельное нажатие и отправил бы половину текста.
#
# Ключи `-l --` обязательны: без них tmux принимает текст за ИМЯ КЛАВИШИ
# (память: tmux-send-keys-takes-key-name), а Enter подаётся отдельно — своим
# нажатием, иначе строка остаётся в приглашении ненабранной.
send_to_pane() {  # текст
    local flat="${1//$'\n'/ }"
    flat="${flat//$'\r'/ }"
    pane_tmux send-keys -t "${TMUX_SESSION:-}" -l -- "$flat" || return 1
    sleep "${PANE_ENTER_DELAY:-0.7}"
    pane_tmux send-keys -t "${TMUX_SESSION:-}" Enter || return 1
}

# Отдать очередь модели сразу: аккорд Ctrl+X Ctrl+S (chat:sendNow). Клавиши идут
# ДВУМЯ вызовами: если C-x завис или отказал, C-s не шлётся — оборванный аккорд,
# дописанный вслепую, мог бы собрать чужое действие. Итог — в PANE_SEND_NOW_RESULT.
pane_send_now() {
    PANE_HUNG=""
    PANE_CHORD_AT=""
    pane_tmux send-keys -t "${TMUX_SESSION:-}" C-x || {
        PANE_CHORD_AT=$(date +%s.%N)
        PANE_SEND_NOW_RESULT="C-x C-s не отдан: ${PANE_HUNG:-send-keys C-x не прошёл}"
        return 1
    }
    PANE_CHORD_AT=$(date +%s.%N)
    if ! pane_tmux send-keys -t "${TMUX_SESSION:-}" C-s; then
        PANE_SEND_NOW_RESULT="C-x C-s не отдан: ${PANE_HUNG:-send-keys C-s не прошёл}"
        return 1
    fi
    PANE_SEND_NOW_RESULT="толчок отдан сразу (C-x C-s)"
}

# Что сейчас в панели: claude/node — агент, иное — оболочка, и текст в неё
# исполнился бы как команды. «?» — панели нет вовсе (или tmux не ответил),
# «мертва» — панель пережила свой процесс.
#
# Про `pane_dead` спрашивается ПЕРВЫМ делом: у панели, чей процесс умер, tmux
# по-прежнему печатает прежнюю команду («claude»), и пинок уходил бы в
# мертвеца, возвращая успех (опыт 13.08.2026, перенесён сюда из сторожа
# сессий — в первой редакции этой двери признака не было, и ревью 17.10.2026
# замерило, что она беднее той копии, которую заменяет).
#
# Ответ кладётся в PANE_CMD (не в stdout) по той же причине, что вывод
# pane_tmux; pane_cmd ниже — обёртка для тех, кому нужна подстановка.
pane_cmd_set() {
    local dead=0
    PANE_HUNG=""
    pane_tmux display-message -p -t "${TMUX_SESSION:-}" '#{pane_dead}' 2>/dev/null && dead="$PANE_OUT"
    # Не вернулся первый вопрос — второй вис бы ровно так же и удвоил простой.
    if [ -n "${PANE_HUNG:-}" ]; then PANE_CMD="?"; return 0; fi
    if [ "$dead" = 1 ]; then PANE_CMD="мертва"; return 0; fi
    if pane_tmux display-message -p -t "${TMUX_SESSION:-}" '#{pane_current_command}' 2>/dev/null; then
        PANE_CMD="$PANE_OUT"
    else
        PANE_CMD="?"
    fi
}

pane_cmd() {
    pane_cmd_set
    printf '%s\n' "$PANE_CMD"
}

# Команда в панели — это агент (claude/node), а не оболочка. Один судья факта
# «агент в панели»: на нём стоят решения сторожа сессий и страж ответа владельцу.
pane_is_agent() {  # $1 = команда панели (PANE_CMD)
    case "$1" in claude|node) return 0 ;; *) return 1 ;; esac
}

# Каталог scripts/ определяется при подключении: судьи паузы и лимита лежат в нём.
PANE_SCRIPTS="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
source "$PANE_SCRIPTS/lib/events.sh"

# 0 = ротацию ведёт другой прогон и ждёт выхода звонящего. Признак берётся из
# журнала, а не из «бежит ли процесс»: обычный прогон сторожа виден в ps так же,
# но преемника не поднимет. Судья один — им пользуются и сторож, и дверь толчка.
rotation_in_progress() {
    python3 - "$LOG_DIR/rotation.jsonl" "${ROTATE_FINISH_TIMEOUT_SEC:-900}" <<'PYEOF' 2>/dev/null
import json, sys, datetime
путь, окно = sys.argv[1], int(sys.argv[2])
последняя = None
try:
    for строка in open(путь, encoding="utf-8"):
        try:
            событие = json.loads(строка)
        except ValueError:
            continue
        if событие.get("event") in ("rotate_command", "agent_restarted",
                                    "rotate_timeout", "restart_failed"):
            последняя = событие
except OSError:
    sys.exit(1)
if not последняя or последняя.get("event") != "rotate_command":
    sys.exit(1)          # ротации нет или она уже завершилась — лок не про меня
момент = datetime.datetime.fromisoformat(последняя["ts"])
прошло = (datetime.datetime.now(момент.tzinfo) - момент).total_seconds()
sys.exit(0 if прошло < окно else 1)
PYEOF
}

# Пауза владельца — СТОП для любого толчка в панель, служебного тоже.
pane_pause_held() {
    python3 "$PANE_SCRIPTS/pause.py" --идёт-ли >/dev/null 2>&1
}

# Почему толчок о сообщении владельца сейчас нельзя: «пауза», «ротация», «лимит»
# или пусто. Порядок — от самой весомой причины. Судья лимита — --лимит-держится,
# не --стоит-ли: тихие метки blocked/nemota значат «работы нет», а не «лимит».
pane_gate_owner() {
    if pane_pause_held; then echo "пауза"
    elif rotation_in_progress; then echo "ротация"
    elif LOG_DIR="${LOG_DIR:-}" python3 "$PANE_SCRIPTS/limit_mark.py" --лимит-держится >/dev/null 2>&1; then echo "лимит"
    fi
}

# ── ОДИН ЛОК НА ВСЕХ ПИСАТЕЛЕЙ В ПАНЕЛЬ ─────────────────────────────────────
# Нажатия в панель агента идут только отсюда и только под pane.lock: текст,
# пауза перед Enter, Enter и аккорд — одна последовательность. Без общего лока
# тексты двух писателей склеивались до Enter (стенд 27.10: 71 из 100), а чужие
# байты между C-x и C-s съедали аккорд. Лок живёт в двери, а не у звонящих: так
# его нельзя забыть в новом пути.
#
# Функция звонящего выполняется в ЭТОЙ ЖЕ оболочке (не в подоболочке flock):
# PANE_REFUSAL и PANE_SEND_NOW_RESULT нужны звонящему, а из подоболочки не
# доехали бы.
pane_lock_run() {  # функция и её доводы
    local lock wait_s rc=0
    lock="${LOG_DIR:-}/locks/${SERVICE_LOCKS_SUBDIR:-services}/pane.lock"
    wait_s=$(awk -v t="${PANE_TMUX_TIMEOUT:-10}" -v d="${PANE_ENTER_DELAY:-0.7}" 'BEGIN { print 4 * t + d + 5 }')
    mkdir -p "$(dirname "$lock")" 2>/dev/null
    if ! exec {PANE_LOCK_FD}>>"$lock"; then
        PANE_LOCK_FD=""; PANE_REFUSAL="очередь нажатий недоступна: нет лока $lock"; return 1
    fi
    if ! flock -w "$wait_s" "$PANE_LOCK_FD"; then
        exec {PANE_LOCK_FD}>&-; PANE_LOCK_FD=""
        PANE_REFUSAL="очередь нажатий занята"; return 1
    fi
    "$@" || rc=$?
    exec {PANE_LOCK_FD}>&-; PANE_LOCK_FD=""
    return "$rc"
}

# Окно аккорда Claude Code 2,5–3 с (замер 26.10): после оборванного C-x чужой
# текст раньше этого срока был бы съеден молча.
PANE_CHORD_HOLD_SEC=3

pane_hold_after_chord() {  # лок держится до C-x + PANE_CHORD_HOLD_SEC
    local left
    [ -n "${PANE_CHORD_AT:-}" ] || return 0
    left=$(awk -v at="$PANE_CHORD_AT" -v now="$(date +%s.%N)" -v hold="$PANE_CHORD_HOLD_SEC" \
        'BEGIN { l = at + hold - now; print (l > 0) ? l : 0 }')
    [ "$left" = 0 ] || sleep "$left"
}

# Ещё не отданные сообщения владельца: файлы корня входящих без метки
# kick-state/sent-<ts>, по возрастанию ts. Метка — после успешного Enter.
pane_unsent_ts() {
    local inbox="${LOG_DIR:-}/inbox" ts
    [ -d "$inbox" ] || return 0
    while IFS= read -r ts; do
        [ -e "${LOG_DIR:-}/kick-state/sent-$ts" ] || printf '%s\n' "$ts"
    done < <(find "$inbox" -maxdepth 1 -type f -name '[0-9]*.[0-9]*.txt' -printf '%f\n' 2>/dev/null \
                 | sed 's/\.txt$//' | sort -t. -k1,1n -k2,2n)
}

# Склейка: строка «ЧЧ:ММ:СС — первые 120 знаков — путь» на сообщение, не больше
# 10, дальше ссылка на свод. Короткая нарочно: длинную склейку Claude Code может
# свернуть в «[Pasted text]». Первая строка вывода — текст, остальные — ts,
# названные в нём (их и помечать отданными).
pane_glue_text() {  # ts…
    python3 - "${LOG_DIR:-}/inbox" "$@" <<'PYEOF'
import sys, time
inbox, stamps = sys.argv[1], sys.argv[2:]
rows, named = [], []
for ts in stamps[:10]:
    try:
        raw = open(f"{inbox}/{ts}.txt", encoding="utf-8", errors="replace").read(600)
    except OSError:
        raw = ""
    head = " ".join(raw.split())[:120]
    clock = time.strftime("%H:%M:%S", time.localtime(float(ts)))
    rows.append(f"{clock} — {head} — {inbox}/{ts}.txt")
    named.append(ts)
more = len(stamps) - len(named)
tail = f" | и ещё {more}, свод: python3 scripts/inbox-digest.py" if more else ""
print(f"Владелец написал ({len(stamps)}), по возрастанию времени; прочитай и перенеси в {inbox}/обработано/: "
      + " | ".join(rows) + tail)
print("\n".join(named))
PYEOF
}

# Что отдать вместо текста звонящего. Текст, не называющий файл входящих
# (комментарий панели, «пауза снята», повтор сторожа), идёт как есть. Текст,
# называющий файл, отдаётся как есть, только если неотдан ровно этот один; иначе
# склейка по порядку. Нечего отдавать (всё уже отдал толчок, взявший лок раньше)
# — PANE_PRESS пуст.
# Фраза в конце каждого пинка о сообщении: пинок мог устареть (файл прочитан иным
# путём, пока строка ждала в вводе), а «принял» владельцу уже отправил диспетчер.
PANE_OWNER_NOTE="Если файла уже нет во входящих — пинок устарел, повторно не отвечай; «принял» владельцу уже отправил диспетчер, не квитируй, отвечай по сути."

pane_compose() {  # текст звонящего
    local text="$1" stamps=() ts
    PANE_PRESS="$text"; PANE_NAMED=()
    case "$text" in *"${LOG_DIR:-}/inbox/"[0-9]*".txt"*) ;; *) return 0 ;; esac
    while IFS= read -r ts; do [ -n "$ts" ] && stamps+=("$ts"); done < <(pane_unsent_ts)
    if [ "${#stamps[@]}" = 0 ]; then PANE_PRESS=""; return 0; fi
    if [ "${#stamps[@]}" = 1 ] && [[ "$text" == *"/${stamps[0]}.txt"* ]]; then
        PANE_NAMED=("${stamps[0]}"); PANE_PRESS="$text $PANE_OWNER_NOTE"; return 0
    fi
    mapfile -t glued < <(pane_glue_text "${stamps[@]}")
    PANE_PRESS="${glued[0]} $PANE_OWNER_NOTE"
    PANE_NAMED=("${glued[@]:1}")
}

pane_mark_sent() {  # ts…
    local ts
    mkdir -p "${LOG_DIR:-}/kick-state"
    for ts in "$@"; do : > "${LOG_DIR:-}/kick-state/sent-$ts"; done
}

# ── ПОДТВЕРЖДЕНИЕ ПРИЁМА ПИНКА ──────────────────────────────────────────────
# Код возврата tmux говорит лишь «клавиши доставлены в панель», а не «Enter
# принят приглашением»: замер 03.11.2026 — пинки лежали в строке ввода claude по
# 43 минуты и всплывали дублем, часть не появлялась вовсе. Поэтому после Enter
# смотрим на экран: стоит ли хвост нашего текста В СТРОКЕ ВВОДА.
#
# Иголка — «<ts>.txt» последнего названного в пинке файла входящих. Хвост текста
# не годится: пинок диспетчера кончается одним и тем же «…/обработано/.», и эхо
# (или ещё стоящий в очереди) прошлый пинок дал бы ложное «не принят». ts уникален.
# Названных файлов нет (комментарий панели, «пауза снята») — подтверждения нет.
# Область — от последней строки приглашения («>» или «❯», перед ней допустима рамка
# «│») до низа экрана: так захватывается и строка ввода, перенесённая на несколько
# строк, и не захватывается история выше. Эхо принятого сообщения в истории тоже
# начинается с «>», но строка ввода всегда НИЖЕ него, поэтому «последняя» верна.
# Приглашения на экране нет (меню, диалог) — запасное окно: последние
# PANE_CONFIRM_LINES непустых строк (по умолчанию 5). Как рисует настоящий claude,
# здесь не проверено: стенд — фальшивый процесс с тем же видом строки.
#
# Строку ввода НЕ чистим ни при каких обстоятельствах (ни Ctrl+U, ни C-c, ни
# Escape): там может быть набор человека. Единственное действие — повторный Enter.
# Не удалось снять экран — ведём себя как до подтверждения (fail-open).
# PANE_CONFIRM=0 выключает всё это; PANE_CONFIRM_DELAY — пауза перед снимком.

# 0 — иголка в области ввода, 1 — нет, 2 — экран снять не удалось.
pane_tail_on_screen() {  # иголка
    local want screen lines="${PANE_CONFIRM_LINES:-5}"
    want="${1//[[:space:]]/}"; want="${want//│/}"
    [ -n "$want" ] || return 1
    pane_tmux capture-pane -p -t "${TMUX_SESSION:-}" || return 2
    screen=$(printf '%s\n' "$PANE_OUT" | awk -v n="$lines" '
        { l[NR] = $0 }
        NF { last = NR }
        $0 ~ /^[[:space:]]*(│[[:space:]]*)?(>|❯)/ { prompt = NR }
        END {
            from = prompt ? prompt : last - n + 1
            for (i = from; i <= last; i++) if (i > 0) print l[i]
        }')
    screen="${screen//[[:space:]]/}"; screen="${screen//│/}"
    [[ "$screen" == *"$want"* ]]
}

# 0 — принят (или выяснить нельзя), 1 — Enter не принят даже со второго раза.
# Принятый Enter виден на первом же снимке, поэтому в обычном случае цена —
# одна короткая пауза под локом панели.
pane_confirm_kick() {  # иголка
    [ "${PANE_CONFIRM:-1}" != 0 ] || return 0
    local delay="${PANE_CONFIRM_DELAY:-0.2}" rc=0
    sleep "$delay"
    pane_tail_on_screen "$1" || rc=$?
    [ "$rc" = 0 ] || return 0
    pane_tmux send-keys -t "${TMUX_SESSION:-}" Enter || return 0
    sleep "$delay"
    pane_tail_on_screen "$1" || return 0
    return 1
}

pane_kick_owner_locked() {  # текст
    PANE_SEND_NOW_RESULT=""
    PANE_REFUSAL="$(pane_gate_owner)"
    if [ -n "$PANE_REFUSAL" ]; then
        PANE_REFUSAL="$PANE_REFUSAL: толчок отложен, сообщение ждёт во входящих"
        PANE_KICK_AT=$(date +%s.%N)
        return 1
    fi
    pane_compose "$1"
    [ -n "$PANE_PRESS" ] || { PANE_REFUSAL=""; return 0; }
    pane_kick "$PANE_PRESS" || { PANE_KICK_AT=$(date +%s.%N); return 1; }
    PANE_KICK_AT=$(date +%s.%N)
    # Текст УЖЕ стоит в строке ввода, поэтому метка отдачи ставится и при «не принят»:
    # без неё следующий пинок сторожа склеил бы второй экземпляр в ту же строку, и
    # оба ушли бы одним сообщением. Правду о застревании несёт событие kick_stuck.
    # Код 0 нарочно: повтор диспетчера (код 1) положил бы ещё один такой же.
    if [ "${#PANE_NAMED[@]}" -gt 0 ] && ! pane_confirm_kick "${PANE_NAMED[-1]}.txt"; then PANE_KICK_STUCK=1; fi
    [ "${#PANE_NAMED[@]}" = 0 ] || pane_mark_sent "${PANE_NAMED[@]}"
    [ -z "${PANE_KICK_STUCK:-}" ] || return 0
    [ "${PANE_SEND_NOW:-0}" = 1 ] || return 0
    # В оболочке C-s — XOFF и терминал замирает: аккорд только в живого агента.
    pane_cmd_set
    case "$PANE_CMD" in
        claude|node) pane_send_now || pane_hold_after_chord ;;
        *) PANE_SEND_NOW_RESULT="C-x C-s не отдан: в панели '$PANE_CMD', не claude/node" ;;
    esac
    return 0
}

# Дверь толчка о сообщении владельца. Отказ — «отложить»: файл остаётся во
# входящих, причина в PANE_REFUSAL (первое слово — слово причины). Гейты
# судятся ПОД локом: пауза, поставленная пока толчок ждал очереди, его
# остановит.
pane_kick_owner() {  # текст
    local rc=0
    PANE_REFUSAL=""
    PANE_SEND_NOW_RESULT=""
    PANE_KICK_AT=""
    PANE_KICK_STUCK=""
    PANE_PRESS=""
    PANE_NAMED=()
    pane_lock_run pane_kick_owner_locked "$1" || rc=$?
    # Флаг прохода сторожа: один толчок tmux за проход, ветка 6д смотрит его последней.
    [ "$rc" != 0 ] || TMUX_KICKED=1
    pane_delivery_mark "$1" "$rc"
    return "$rc"
}

# Метка этапа доставки о толчке (журнал «доставка», спека §4.6) — вне лока, чтобы
# запись не держала очередь нажатий. Только о толчке, чей текст называет входящие:
# комментарий панели — не сообщение канала. Какие сообщения покрыл толчок: названные
# в склейке, а у текста без файла («пауза снята», повтор сторожа) — все неотданные.
pane_delivery_mark() {  # текст звонящего, код двери
    local text="$1" rc="$2" list="" at why
    [ -n "${LOG_DIR:-}" ] || return 0
    case "$text" in *"$LOG_DIR/inbox/"*) ;; *) return 0 ;; esac
    if [ "${#PANE_NAMED[@]}" -gt 0 ]; then
        list=$(IFS=,; printf '%s' "${PANE_NAMED[*]}")
    else
        list=$(pane_unsent_ts | paste -sd, -)
    fi
    [ -n "$list" ] || return 0
    at="${PANE_KICK_AT:-$(date +%s.%N)}"
    if [ "$rc" != 0 ]; then
        why="${PANE_REFUSAL%%:*}"
        HARNESS_LOG_DIR="$LOG_DIR" harness_event доставка scripts/lib/pane.sh delivery.kick_refused held \
            "ts_список=$list" "почему=$why" "когда=$at"
    elif [ -n "${PANE_KICK_STUCK:-}" ]; then
        HARNESS_LOG_DIR="$LOG_DIR" harness_event доставка scripts/lib/pane.sh delivery.kick_stuck held \
            "ts_список=$list" "почему=enter-не-принят" "когда=$at"
    elif [ -n "$PANE_PRESS" ]; then
        HARNESS_LOG_DIR="$LOG_DIR" harness_event доставка scripts/lib/pane.sh delivery.kick_sent ok \
            "ts_список=$list" "когда=$at"
    fi
    [ -z "$PANE_SEND_NOW_RESULT" ] || \
        HARNESS_LOG_DIR="$LOG_DIR" harness_event доставка scripts/lib/pane.sh delivery.send_now ok \
            "ts_список=$list" "почему=$PANE_SEND_NOW_RESULT"
    return 0
}

# Служебный толчок (сторож: пульс, тишина, простой, промпты ротации; limit-watch:
# подъём после лимита). Держит только паузу: лимит здесь судить нельзя, потому
# что limit-watch будит смену именно в момент сброса.
pane_kick_service() {  # текст
    local rc=0
    PANE_REFUSAL=""
    pane_lock_run pane_kick "$1" || rc=$?
    [ "$rc" != 0 ] || TMUX_KICKED=1
    return "$rc"
}

# Команда сессии под тем же локом: /exit, /clear, /model, клавиши диалога,
# C-c, команда запуска. Паузу не судит: через неё идёт /exit ротации.
#   pane_command text <строка>   — строка как текст и Enter (send_to_pane)
#   pane_command keys <клавиши…> — имена клавиш tmux как есть
pane_command() {
    PANE_REFUSAL=""
    pane_lock_run pane_command_locked "$@"
}

pane_command_locked() {
    local mode="$1"; shift
    PANE_HUNG=""
    case "$mode" in
        text) send_to_pane "$1" || { PANE_REFUSAL="${PANE_HUNG:-send-keys не прошёл}"; return 1; } ;;
        keys) pane_tmux send-keys -t "${TMUX_SESSION:-}" "$@" || { PANE_REFUSAL="${PANE_HUNG:-send-keys не прошёл}"; return 1; } ;;
        *) PANE_REFUSAL="pane_command: неизвестный режим «$mode»"; return 1 ;;
    esac
}

# Пинок агенту: только в живую панель с агентом. Причина отказа — в
# PANE_REFUSAL (пусто — дошёл), чтобы звонящий сказал о ней один раз своим
# журналом, а не на каждом круге. Зависший вызов называется в ней словами
# двери (PANE_HUNG): по ним видно, КАКАЯ подкоманда не вернулась. Тело без
# лока: снаружи его зовут только через pane_kick_owner и pane_kick_service.
pane_kick() {  # текст
    PANE_REFUSAL=""
    PANE_HUNG=""
    # Лимит служебной двери судить нельзя: limit-watch будит смену в момент
    # сброса, пока метка ещё стоит.
    if pane_pause_held; then PANE_REFUSAL="пауза: толчок отложен"; return 1; fi
    command -v tmux >/dev/null 2>&1 || { PANE_REFUSAL="tmux не установлен"; return 1; }
    pane_tmux has-session -t "${TMUX_SESSION:-}" 2>/dev/null \
        || { PANE_REFUSAL="${PANE_HUNG:-tmux-сессии '${TMUX_SESSION:-«не задана»}' нет}"; return 1; }
    pane_cmd_set
    case "$PANE_CMD" in
        claude|node) send_to_pane "$1" || { PANE_REFUSAL="${PANE_HUNG:-send-keys не прошёл}"; return 1; } ;;
        *) PANE_REFUSAL="${PANE_HUNG:-в панели '$PANE_CMD', не claude/node — оболочка исполнила бы текст как команды}"
           return 1 ;;
    esac
}
