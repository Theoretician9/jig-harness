#!/usr/bin/env bash
#
# test_delivery_marks.sh — проба меток доставки и прибора задержки (спека
# docs/specs/2026-10-27-мгновенная-доставка-сообщений.md §4.6, §5 К-1, К-2, К-3(б),
# К-5, К-6, строка Ф1 §8; план docs/plans/2026-10-28-доставка-метки.md; карточка
# hr.remove-message-interception-mechanism).
#
# ЧТО ОХРАНЯЕТ. Задержку доставки до сих пор мерили по чужим следам: строке «в
# очередь» журнала диспетчера и стенограммам. Время отправки из телеграма не
# хранилось нигде, толчок и отказ двери следа с меткой не оставляли, и прежний
# прибор считал постановку в очередь Claude Code доставкой («p90 12,5 с» вместо
# 308 с). Метки этапов пишет сам путь доставки, а прибор отказывает, когда судить
# нечего, — иначе пустой журнал выглядел бы зелёным.
#
# Пути (печатается их число):
#   1. диспетчер (tg-dispatcher.sh) целым циклом с подставным Bot API и подделкой
#      tmux: метки received/queued/kick_sent, no_kick команды, отказ паузы и лимита,
#      склейка десяти сообщений, текст сообщения в метки не попадает, боевой журнал
#      стенд не трогает;
#   2. прибор (delivery-latency.py) на стендовых журналах, записанных боевой дверью
#      emit (events.py), и стендовых стенограммах: пустой набор и мало данных —
#      ОТКАЗ, К-1 с ∞ и законным отказом, расширение окна, К-2 с NTP, К-3, потери
#      (К-5), К-6 по очереди Claude Code, в том числе по строкам диспетчера без меток;
#   3. код: журнал «доставка» в реестре с обоими писателями.
#
# Стенд: свой LOG_DIR, каталог паузы, Bot API на своём порту, tmux — подделка первой
# в PATH (живой сервер tmux не зовётся), timedatectl — подделка с ответом стенда.
# Вызовов модели нет (карты dev-map.yaml на стенде нет).
#
#   bash scripts/test_delivery_marks.sh [--случай диспетчер|прибор|код]
# Код возврата: 0 — зелёная, 1 — красная. Последняя строка — итог с числом путей.
# Механизмы берутся от места самой пробы (../ от scripts/): мутации идут «из-копии».
#
# Имена переменных ЛАТИНИЦЕЙ (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8

KOREN="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
CONF_SEED="$KOREN/harness/config/harness.conf"; [ -f "$CONF_SEED" ] || CONF_SEED="$KOREN/config/harness.conf"
JOURNALS_YAML="$KOREN/harness/config/journals.yaml"; [ -f "$JOURNALS_YAML" ] || JOURNALS_YAML="$KOREN/config/journals.yaml"
DISPATCHER="$KOREN/scripts/tg-dispatcher.sh"
PANE_LIB="$KOREN/scripts/lib/pane.sh"
EVENTS_LIB="$KOREN/scripts/lib/events.py"
INSTRUMENT="$KOREN/scripts/delivery-latency.py"
PAUSE_PY="$KOREN/scripts/pause.py"
LIMIT_MARK="$KOREN/scripts/limit_mark.py"
for f in "$DISPATCHER" "$PANE_LIB" "$EVENTS_LIB" "$PAUSE_PY" "$LIMIT_MARK" "$JOURNALS_YAML"; do
    [ -r "$f" ] || { echo "ПРОБА: нет механизма $f — проверять нечего"; exit 1; }
done
# Прибора до кода нет: его отсутствие — красные случаи, а не отказ пробы.

ONLY=""
if [ "${1:-}" = "--случай" ]; then
    ONLY="${2:-}"
    case "$ONLY" in диспетчер|прибор|код) ;; *) echo "ПРОБА: неизвестный случай «$ONLY» (диспетчер|прибор|код)"; exit 1 ;; esac
elif [ -n "${1:-}" ]; then
    echo "ПРОБА: неизвестный ключ «$1»"; exit 1
fi
want() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }

source "$KOREN/scripts/lib/selftest_cases.sh"

T=$(mktemp -d /tmp/proba-delivery-marks-XXXXXX)
STAMP="proba-delivery-marks-$$"
PATHS_JUDGED=0
PIDS_KILL=()
cleanup() {
    local p
    for p in "${PIDS_KILL[@]}"; do kill -KILL -- "-$p" 2>/dev/null; kill -KILL "$p" 2>/dev/null; done
    pkill -KILL -f "^$STAMP" 2>/dev/null
    if [ -n "${PROBA_KEEP:-}" ]; then echo "стенд оставлен (PROBA_KEEP): $T"; else rm -rf "$T"; fi
}
trap cleanup EXIT

wait_for() {  # $1 = предел, остальное — условие
    local end=$(( $(date +%s) + $1 )); shift
    while [ "$(date +%s)" -lt "$end" ]; do "$@" && return 0; sleep 0.3; done
    "$@"
}
wait_quiet() {  # файл $1 не менялся $2 с (предел $3 с)
    local end=$(( $(date +%s) + $3 )) age
    while [ "$(date +%s)" -lt "$end" ]; do
        age=$(( $(date +%s) - $(stat -c %Y "$1" 2>/dev/null || echo 0) ))
        [ "$age" -ge "$2" ] && return 0
        sleep 0.5
    done
}

# ── подделки: tmux и timedatectl ───────────────────────────────────────────
mkdir -p "$T/bin" "$T/tmuxsock"
cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
log="${PROBA_TMUX_LOG:-}"
[ -n "$log" ] || { echo "подделка tmux: позвана вне стенда" >&2; exit 1; }
printf '%s %s\n' "$(date +%s.%N)" "$*" >> "$log"
case "${1:-}" in
    has-session)     exit 0 ;;
    display-message) case "$*" in
                         *pane_dead*)            echo 0 ;;
                         *pane_current_command*) echo claude ;;
                         *activity*)             date +%s ;;
                         *)                      echo "" ;;
                     esac ;;
esac
exit 0
EOF
# Ответ NTP — данные стенда: файл рядом с подделкой. Нет файла — подделка
# отказывает, и прибор обязан сказать «не судится», а не «синхронно».
cat > "$T/bin/timedatectl" <<'EOF'
#!/usr/bin/env bash
answer="${PROBA_NTP_FILE:-}"
[ -n "$answer" ] && [ -r "$answer" ] || { echo "подделка timedatectl: ответа стенда нет" >&2; exit 1; }
case "$*" in *NTPSynchronized*) cat "$answer" ;; *) exit 1 ;; esac
EOF
chmod +x "$T/bin/tmux" "$T/bin/timedatectl"
stand_env() { env -u TMUX TMUX_TMPDIR="$T/tmuxsock" PATH="$T/bin:$PATH" "$@"; }
[ "$(stand_env bash -c 'command -v tmux')" = "$T/bin/tmux" ] \
    || { echo "ПРОБА: под окружением стенда tmux — не подделка; стенд не собран"; exit 1; }

# ── разбор журнала меток ───────────────────────────────────────────────────
# Метка ищется по полю data, а не по тексту строки: «ts» есть и в конверте.
cat > "$T/marks.py" <<'PYEOF'
import json, sys

def rows(path):
    out = []
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                row = json.loads(line)
            except ValueError:
                out.append({"_broken": True}); continue
            out.append(row)
    except OSError:
        pass
    return out

def of(path, event):
    return [r for r in rows(path) if r.get("event") == f"delivery.{event}"]

def data(r):
    return r.get("data") or {}

def ts_list(r):
    return [x for x in str(data(r).get("ts_список") or "").split(",") if x]

def count(path, event, key, value):
    return str(sum(1 for r in of(path, event) if str(data(r).get(key)) == value))

def field(path, event, key, value, want):
    hits = [r for r in of(path, event) if str(data(r).get(key)) == value]
    if not hits:
        return "нет метки"
    if want in ("src", "outcome", "v"):
        return str(hits[0].get(want))
    return str(data(hits[0]).get(want))

def covered(path, event, ts):
    return str(sum(1 for r in of(path, event) if ts in ts_list(r)))

def cover_field(path, event, ts, want):
    hits = [r for r in of(path, event) if ts in ts_list(r)]
    if not hits:
        return "нет метки"
    if want in ("src", "outcome"):
        return str(hits[0].get(want))
    return str(data(hits[0]).get(want))

def order(path, ts):
    # received.когда ≤ queued.когда ≤ kick_sent.когда (первые метки), и толчок не
    # позже 5 с от приёма: стенд толкает сразу.
    def first(event, pick):
        xs = [float(data(r).get("когда")) for r in of(path, event) if pick(r)]
        return min(xs) if xs else None
    rec = first("received", lambda r: str(data(r).get("ts")) == ts)
    que = first("queued", lambda r: str(data(r).get("ts")) == ts)
    kick = first("kick_sent", lambda r: ts in ts_list(r))
    if None in (rec, que, kick):
        return f"нет метки (received {rec}, queued {que}, kick_sent {kick})"
    if not (rec <= que <= kick):
        return f"порядок нарушен ({rec:.3f} {que:.3f} {kick:.3f})"
    return "да" if kick - rec <= 5 else f"толчок через {kick - rec:.2f} с"

def envelope(path):
    rs = rows(path)
    bad = sum(1 for r in rs if r.get("_broken") or r.get("v") != 1)
    return f"{len(rs)} {bad}"

def kicks_total(path):
    return str(len(of(path, "kick_sent")))

cmd, args = sys.argv[1], sys.argv[2:]
print({"count": count, "field": field, "covered": covered, "cover_field": cover_field,
       "order": order, "envelope": envelope, "kicks": kicks_total}[cmd](*args))
PYEOF
mk() { python3 "$T/marks.py" "$@"; }
cnt() { local n; n=$(grep -c -- "$1" "$2" 2>/dev/null); echo "${n:-0}"; }  # $1 = образец, $2 = файл

# ═══ 1. ДИСПЕТЧЕР: целым циклом, метки пишет боевой путь ════════════════════
if want диспетчер; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 1: tg-dispatcher.sh целым циклом — метки этапов"
    # Время отправки из телеграма — значение, которого стенд иначе не создаёт:
    # метка, взявшая «сейчас» вместо message.date, на нём краснеет.
    TG_DATE_SENTINEL=$(( $(date +%s) - 7 ))
    cat > "$T/api.py" <<'PYEOF'
import json, sys, time, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
port, acks, ready, upd, tg_date = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
UPDATES = json.load(open(upd, encoding="utf-8"))
def update(u):
    return {"update_id": u["id"], "message": {"message_id": u["id"], "date": tg_date,
                                              "chat": {"id": 777000}, "text": u["text"]}}
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
    cat > "$T/holder.py" <<'PYEOF'
import fcntl, os, sys, time
f = open(sys.argv[3], "a")
fcntl.flock(f, fcntl.LOCK_EX)
open(sys.argv[2], "w").close()
while not os.path.exists(sys.argv[1]):
    time.sleep(0.05)
PYEOF
    offset_is() { [ "$(cat "$1/log/tg_offset" 2>/dev/null)" = "$2" ]; }
    D_STAND_BAD=0
    d_bad() { D_STAND_BAD=$((D_STAND_BAD + 1)); echo "  СТЕНД: $1"; }
    d_reached() { case_check "СТЕНД ($1): диспетчер разобрал все сообщения" "0" "$D_STAND_BAD"; }
    inbox_ts() { find "$1/log/inbox" -maxdepth 1 -name '*.txt' -printf '%f\n' 2>/dev/null | sed 's/\.txt$//' | sort -n; }
    WORK_TEXT="почини карту задач в панели, кнопка обновления не жмётся"

    # $1 = имя, $2 = «id:текст;…», дальше флаги: hold, pause, limit, comment.
    d_case() {
        local name="$1" spec="$2" S port lockdir last flag hold="" comment=""
        shift 2
        S="$T/disp-$name"; DS="$S"; D_STAND_BAD=0
        mkdir -p "$S/log" "$S/project" "$S/panel"
        for flag in "$@"; do
            case "$flag" in hold) hold=1 ;; comment) comment=1 ;; esac
        done
        python3 - "$spec" "$S/updates.json" <<'PYEOF'
import json, sys
items = [p.split(":", 1) for p in sys.argv[1].split(";") if p]
json.dump([{"id": int(i), "text": t} for i, t in items], open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
PYEOF
        last=$(python3 -c 'import json,sys; u=json.load(open(sys.argv[1])); print(max([x["id"] for x in u] or [0]))' "$S/updates.json")
        if [ -n "$comment" ]; then
            mkdir -p "$S/panel/comments/new"
            printf '{"id":"hr.proba","текст":"%s"}' "$WORK_TEXT" > "$S/panel/comments/new/1761500000.5.json"
        fi
        : > "$S/tmux-calls"
        printf '000:СТЕНДОВЫЙ' > "$S/token"   # не-секрет: подставной Bot API стенда
        port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
        python3 "$T/api.py" "$port" "$S/acks" "$S/api.ready" "$S/updates.json" "$TG_DATE_SENTINEL" > "$S/api.out" 2>&1 &
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
        printf 'TMUX_SESSION="%s"\nSERVICE_LOCKS_SUBDIR="services"\nPANE_TMUX_TIMEOUT="5"\nPANE_ENTER_DELAY="0.1"\nPANE_SEND_NOW="0"\n' \
            "$STAMP-disp" > "$S/harness.conf"
        lockdir="$S/log/locks/services"; mkdir -p "$lockdir"
        if [ -n "$hold" ]; then
            python3 "$T/holder.py" "$S/release" "$S/held" "$lockdir/pane.lock" &
            PIDS_KILL+=($!); disown $!
            wait_for 5 test -e "$S/held" || { d_bad "лок толчков не взят"; return; }
        fi
        for flag in "$@"; do
            case "$flag" in
                pause)
                    HARNESS_PANEL_STATE="$S/panel" python3 "$PAUSE_PY" --поставить "проба: владелец остановил" --кто владелец >/dev/null
                    HARNESS_PANEL_STATE="$S/panel" python3 "$PAUSE_PY" --идёт-ли \
                        || { echo "ПРОБА: pause.py не поставил паузу на стенде — проверять нечего"; exit 1; } ;;
                limit)
                    LOG_DIR="$S/log" python3 "$LIMIT_MARK" --поставить limit >/dev/null
                    LOG_DIR="$S/log" python3 "$LIMIT_MARK" --стоит-ли \
                        || { echo "ПРОБА: limit_mark.py не поставил метку — проверять нечего"; exit 1; } ;;
            esac
        done
        # HARNESS_LOG_DIR снята нарочно: журнал меток обязан выбрать сам путь
        # доставки по LOG_DIR стенда, а не по переменной, которую задала проба.
        stand_env env -u HARNESS_LOG_DIR PROBA_TMUX_LOG="$S/tmux-calls" \
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
        # Метки фоном: дать им лечь до разбора.
        wait_quiet "$S/log/доставка.jsonl" 2 15
        kill -KILL -- "-$DPID" 2>/dev/null; kill -KILL "$DPID" 2>/dev/null
        DJ="$S/log/доставка.jsonl"
    }

    # (а) БОЛЬНОЙ: одно сообщение — три метки с полями из телеграма и файла очереди.
    TEXT_MARKER="ЗЕБРА$$КАРТА"
    echo "— (а) одно сообщение: received, queued, kick_sent"
    d_case one "501:$WORK_TEXT $TEXT_MARKER"; d_reached "одно"
    TS1=$(inbox_ts "$DS" | head -1)
    case_check "СТЕНД (а): сообщение легло во входящие" "1" "$(inbox_ts "$DS" | grep -c .)"
    case_check "СТЕНД (а): толчок о сообщении ушёл в панель" "да" \
        "$(grep -qF -- "$DS/log/inbox/$TS1.txt" "$DS/tmux-calls" 2>/dev/null && echo да || echo нет)"
    case_check "БОЛЬНОЙ (а): метка received на update 501 — одна" "1" "$(mk count "$DJ" received update_id 501)"
    case_check "(а) received: ts — метка файла очереди" "$TS1" "$(mk field "$DJ" received update_id 501 ts)"
    case_check "(а) received: message_id из телеграма" "501" "$(mk field "$DJ" received update_id 501 message_id)"
    case_check "(а) received: tg_date — message.date телеграма" "$TG_DATE_SENTINEL" "$(mk field "$DJ" received update_id 501 tg_date)"
    case_check "(а) received: «когда» — момент приёма (= ts)" "$TS1" "$(mk field "$DJ" received update_id 501 когда)"
    case_check "(а) received: писатель — диспетчер" "scripts/tg-dispatcher.sh" "$(mk field "$DJ" received update_id 501 src)"
    case_check "БОЛЬНОЙ (а): метка queued с ts файла — одна" "1" "$(mk count "$DJ" queued ts "$TS1")"
    case_check "БОЛЬНОЙ (а): метка kick_sent называет ts сообщения — одна" "1" "$(mk covered "$DJ" kick_sent "$TS1")"
    case_check "(а) kick_sent: писатель — дверь pane.sh" "scripts/lib/pane.sh" "$(mk cover_field "$DJ" kick_sent "$TS1" src)"
    case_check "(а) received ≤ queued ≤ kick_sent по «когда», толчок ≤ 5 с от приёма" "да" "$(mk order "$DJ" "$TS1")"
    read -r EN EBAD <<< "$(mk envelope "$DJ")"
    case_check "(а) все строки журнала — конверт v1 (строк $EN)" "0" "${EBAD:-нет журнала}"
    case_check "П-15: текста сообщения в метках нет" "0" "$(cnt "$TEXT_MARKER" "$DJ")"
    # Боевой журнал стенд не трогает (память the-probe-played-on-production).
    LIVE_LOG=$(env -u HARNESS_LOG_DIR -u HARNESS_INSTALL_CONF -u HARNESS_CONF \
        python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import config; print(config.log_dir())' "$KOREN/scripts/lib" 2>/dev/null || true)
    if [ -n "$LIVE_LOG" ] && [ "$LIVE_LOG" != "$DS/log" ]; then
        case_check "стенд не писал в боевой журнал $LIVE_LOG/доставка.jsonl" "0" \
            "$(cnt "$TS1" "$LIVE_LOG/доставка.jsonl")"
    else
        echo "  не судится: боевой LOG_DIR не прочитан — стенд против боя не сверен"
    fi

    # (б) команда бота: no_kick с причиной, толчка нет.
    echo "— (б) команда «статус»: no_kick"
    d_case cmd "601:статус"; d_reached "команда"
    TS2=$(mk field "$DJ" received update_id 601 ts)
    case_check "СТЕНД (б): received на команду есть" "1" "$(mk count "$DJ" received update_id 601)"
    case_check "БОЛЬНОЙ (б): метка no_kick на ts команды — одна" "1" "$(mk count "$DJ" no_kick ts "$TS2")"
    case_check "(б) no_kick: причина называет команду" "да" \
        "$(case "$(mk field "$DJ" no_kick ts "$TS2" почему)" in *команда*status*) echo да ;; *) echo нет ;; esac)"
    case_check "(б) no_kick: исход skipped" "skipped" "$(mk field "$DJ" no_kick ts "$TS2" outcome)"
    case_check "(б) kick_sent на команду нет" "0" "$(mk covered "$DJ" kick_sent "$TS2")"

    # (в) пауза и лимит: kick_refused с причиной, толчка нет.
    for gate in pause limit; do
        case "$gate" in pause) word=пауза ;; limit) word=лимит ;; esac
        echo "— (в) $word: kick_refused"
        d_case "gate-$gate" "701:$WORK_TEXT" "$gate"; d_reached "$word"
        TSG=$(inbox_ts "$DS" | head -1)
        case_check "СТЕНД (в) $word: сообщение во входящих" "1" "$(inbox_ts "$DS" | grep -c .)"
        case_check "БОЛЬНОЙ (в) $word: kick_refused называет ts — одна" "1" "$(mk covered "$DJ" kick_refused "$TSG")"
        case_check "(в) $word: причина отказа — «$word»" "$word" "$(mk cover_field "$DJ" kick_refused "$TSG" почему)"
        case_check "(в) $word: исход held" "held" "$(mk cover_field "$DJ" kick_refused "$TSG" outcome)"
        case_check "(в) $word: kick_sent нет" "0" "$(mk covered "$DJ" kick_sent "$TSG")"
    done

    # (г) склейка: 10 сообщений, лок занят на время разбора — каждое покрыто kick_sent.
    echo "— (г) 10 сообщений подряд, склейка"
    SPEC=""
    for i in $(seq 1 10); do SPEC="$SPEC;$((800 + i)):$WORK_TEXT, пункт $i"; done
    d_case glue "$SPEC" hold; d_reached "склейка"
    N_REC=0; N_COV=0
    while IFS= read -r ts; do
        [ -n "$ts" ] || continue
        N_REC=$((N_REC + $(mk count "$DJ" received ts "$ts")))
        [ "$(mk covered "$DJ" kick_sent "$ts")" -ge 1 ] && N_COV=$((N_COV + 1))
    done < <(inbox_ts "$DS")
    case_check "СТЕНД (г): во входящих 10 файлов" "10" "$(inbox_ts "$DS" | grep -c .)"
    case_check "(г) received на каждое из 10" "10" "$N_REC"
    case_check "БОЛЬНОЙ (г): каждое из 10 покрыто kick_sent (ts_список склейки)" "10" "$N_COV"

    # (д) комментарий панели: это не сообщение канала, меток kick_sent нет.
    echo "— (д) комментарий панели"
    d_case comment "" comment
    case_check "СТЕНД (д): комментарий отдан в панель" "да" \
        "$(grep -qF 'Комментарий владельца' "$DS/tmux-calls" 2>/dev/null && echo да || echo нет)"
    case_check "(д) комментарий панели меток kick_sent не пишет" "0" "$(mk kicks "$DJ")"
fi

# ═══ 2. ПРИБОР: стендовые журналы, записанные боевой дверью emit ═══════════
if want прибор; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 2: delivery-latency.py на стендовых журналах"
    # Генератор пишет строки через events.emit — ту же функцию, что и bash-дверь
    # harness_event: значения строками, как их кладёт боевой путь. Это доказывает
    # ЧТЕНИЕ прибора; запись доказывает путь 1.
    cat > "$T/gen.py" <<'PYEOF'
import json, os, sys
lib, log_dir, spec_path = sys.argv[1], sys.argv[2], sys.argv[3]
os.environ["HARNESS_LOG_DIR"] = log_dir
sys.path.insert(0, lib)
import events  # noqa: E402
spec = json.load(open(spec_path, encoding="utf-8"))
D = "scripts/tg-dispatcher.sh"; P = "scripts/lib/pane.sh"
def put(src, ev, outcome, **data):
    if not events.emit(src, f"delivery.{ev}", outcome, journal="доставка",
                       task="hr.proba", data={k: str(v) for k, v in data.items()}):
        sys.exit(f"emit не записал {ev}")
for m in spec["messages"]:
    ts = f"{m['at']:.9f}"
    uid = m["uid"]
    if m.get("received", True):
        put(D, "received", "ok", update_id=uid, message_id=uid, ts=ts,
            tg_date=int(m["at"] - m.get("tg_lag", 0.5)), когда=ts)
    if m.get("queued", True):
        put(D, "queued", "ok", message_id=uid, ts=ts, когда=f"{m['at'] + 0.05:.6f}")
    if m.get("no_kick"):
        put(D, "no_kick", "skipped", ts=ts, почему=m["no_kick"])
    elif m.get("refused"):
        put(P, "kick_refused", "held", ts_список=ts, почему=m["refused"], когда=f"{m['at'] + 0.1:.6f}")
    elif m.get("kick") is not None:
        put(P, "kick_sent", "ok", ts_список=ts, когда=f"{m['at'] + m['kick']:.6f}")
for uid in spec.get("skipped", []):
    with open(os.path.join(log_dir, f"tg-skipped-{uid}.json"), "w") as fh:
        json.dump({"update_id": uid, "message": {"message_id": uid}}, fh)
PYEOF
    BASE=$(( $(date +%s) - 2 * 86400 ))
    W_FROM=$(date -d "@$((BASE - 3600))" '+%F %T'); W_TO=$(date -d "@$((BASE + 3600))" '+%F %T')
    I_CASE=0

    # $1 = имя, $2 = JSON спеки стенда, дальше доводы прибора. Печатает «код|последняя строка».
    run_instr() {
        local name="$1" spec="$2" X; shift 2
        X="$T/instr-$name"; mkdir -p "$X/log" "$X/tr"
        printf '%s' "$spec" > "$X/spec.json"
        python3 "$T/gen.py" "$KOREN/scripts/lib" "$X/log" "$X/spec.json" > "$X/gen.out" 2>&1 \
            || { echo "gen-fail|$(tail -1 "$X/gen.out")"; return; }
        local rc=0
        stand_env HARNESS_LOG_DIR="$X/log" LOG_DIR="$X/log" PROBA_NTP_FILE="${NTP_FILE:-}" \
            DELIVERY_TRANSCRIPTS_DIR="$X/tr" \
            timeout -k 2 120 python3 "$INSTRUMENT" "$@" > "$X/out" 2>&1 || rc=$?
        echo "$rc|$(grep -v '^[[:space:]]*$' "$X/out" | tail -1)"
    }
    # Спека набора: n сообщений с толчком через kick с, плюс добавки (JSON-куски).
    msgs() {  # $1 = n, $2 = kick, $3 = сдвиг uid, $4 = сдвиг времени, $5 = tg_lag
        python3 - "$1" "$2" "$3" "$BASE" "${4:-0}" "${5:-0.5}" <<'PYEOF'
import json, sys
n, kick, uid0, base, shift, lag = int(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5]), float(sys.argv[6])
print(json.dumps([{"uid": uid0 + i, "at": base + shift + i * 7.123456789, "kick": kick, "tg_lag": lag} for i in range(n)]))
PYEOF
    }
    spec_of() {  # $1 = JSON-список сообщений, $2 = JSON-список skipped
        python3 -c 'import json,sys; print(json.dumps({"messages": json.loads(sys.argv[1]), "skipped": json.loads(sys.argv[2])}))' "$1" "${2:-[]}"
    }
    join() { python3 -c 'import json,sys; print(json.dumps(sum((json.loads(a) for a in sys.argv[1:]), [])))' "$@"; }
    one() {  # $1 = uid, $2 = сдвиг, $3 = JSON-поля сверх
        python3 -c 'import json,sys; m={"uid": int(sys.argv[1]), "at": int(sys.argv[3]) + float(sys.argv[2])}; m.update(json.loads(sys.argv[4])); print(json.dumps([m]))' "$1" "$2" "$BASE" "$3"
    }
    verdict() {  # $1 = «код|строка», $2 = ждём код, $3 = ждём слово вердикта
        local rc="${1%%|*}" line="${1#*|}"
        [ "$rc" = "$2" ] && [[ "$line" == *"$3"* ]] && echo да || echo "нет (код $rc: ${line:0:160})"
    }
    has() { grep -qF -- "$2" "$T/instr-$1/out" 2>/dev/null && echo да || echo нет; }  # $1 = имя случая

    echo "— пустой набор"
    R=$(run_instr empty "$(spec_of '[]')" --окно "$W_FROM" "$W_TO" --критерий К-1)
    case_check "БОЛЬНОЙ К-1: пустой журнал — ОТКАЗ, код 2" "да" "$(verdict "$R" 2 ОТКАЗ)"
    R=$(run_instr empty-loss "$(spec_of '[]')" --окно "$W_FROM" "$W_TO" --потери)
    case_check "БОЛЬНОЙ К-5: пустой журнал — ОТКАЗ, код 2" "да" "$(verdict "$R" 2 ОТКАЗ)"
    R=$(run_instr empty-sum "$(spec_of '[]')" --окно "$W_FROM" "$W_TO")
    case_check "сводка: пустой журнал — ОТКАЗ, код 2" "да" "$(verdict "$R" 2 ОТКАЗ)"

    echo "— К-1"
    R=$(run_instr k1-ok "$(spec_of "$(msgs 20 0.5 1000)")" --окно "$W_FROM" "$W_TO" --критерий К-1)
    case_check "К-1: 20 толчков по 0,5 с — ПРОЙДЕН, код 0" "да" "$(verdict "$R" 0 ПРОЙДЕН)"
    R=$(run_instr k1-few "$(spec_of "$(msgs 5 0.5 1100)")" --окно "$W_FROM" "$W_TO" --критерий К-1)
    case_check "К-1: 5 сообщений за 3 суток — ОТКАЗ «мало данных», код 2" "да" "$(verdict "$R" 2 ОТКАЗ)"
    case_check "К-1: отказ называет «мало данных»" "да" "$(has k1-few 'мало данных')"
    R=$(run_instr k1-inf "$(spec_of "$(join "$(msgs 30 0.5 1200)" "$(one 1299 3000 '{"kick": null}')")")" \
        --окно "$W_FROM" "$W_TO" --критерий К-1)
    case_check "БОЛЬНОЙ К-1: 30 толчков и одно сообщение без толчка (доля 97 %) — ПРОВАЛ, код 1" "да" "$(verdict "$R" 1 ПРОВАЛ)"
    case_check "К-1: недоставленное названо значением ∞" "да" "$(has k1-inf '∞')"
    R=$(run_instr k1-legal "$(spec_of "$(join "$(msgs 20 0.5 1300)" "$(one 1390 3000 '{"refused": "пауза"}')" \
        "$(one 1391 3100 '{"refused": "ротация"}')" "$(one 1392 3200 '{"refused": "лимит"}')")")" \
        --окно "$W_FROM" "$W_TO" --критерий К-1)
    case_check "К-1: законные отказы (пауза, ротация, лимит) выпадают — ПРОЙДЕН, код 0" "да" "$(verdict "$R" 0 ПРОЙДЕН)"
    R=$(run_instr k1-illegal "$(spec_of "$(join "$(msgs 30 0.5 1400)" "$(one 1499 3000 '{"refused": "tmux-сессии нет"}')")")" \
        --окно "$W_FROM" "$W_TO" --критерий К-1)
    case_check "БОЛЬНОЙ К-1: незаконный отказ двери — ∞, ПРОВАЛ, код 1" "да" "$(verdict "$R" 1 ПРОВАЛ)"
    R=$(run_instr k1-max "$(spec_of "$(join "$(msgs 30 0.5 1500)" "$(one 1599 3000 '{"kick": 5.0}')")")" \
        --окно "$W_FROM" "$W_TO" --критерий К-1)
    case_check "К-1: доля 97 %, но максимум 5 с > 4 с — ПРОВАЛ, код 1" "да" "$(verdict "$R" 1 ПРОВАЛ)"
    R=$(run_instr k1-slow "$(spec_of "$(msgs 20 3.0 1600)")" --окно "$W_FROM" "$W_TO" --критерий К-1)
    case_check "К-1: все толчки по 3 с — ПРОВАЛ, код 1" "да" "$(verdict "$R" 1 ПРОВАЛ)"
    R=$(run_instr k1-nokick "$(spec_of "$(join "$(msgs 20 0.5 1700)" "$(one 1799 3000 '{"no_kick": "команда status"}')")")" \
        --окно "$W_FROM" "$W_TO" --критерий К-1)
    case_check "К-1: команда бота (no_kick) не считается недоставленной — ПРОЙДЕН" "да" "$(verdict "$R" 0 ПРОЙДЕН)"
    # Окно растёт до 3 суток от «до»: 20 сообщений лежат за 2 суток до правой
    # границы, а окно задано последним часом.
    LATE_TO=$(date -d "@$((BASE + 2 * 86400))" '+%F %T'); LATE_FROM=$(date -d "@$((BASE + 2 * 86400 - 3600))" '+%F %T')
    R=$(run_instr k1-widen "$(spec_of "$(msgs 20 0.5 1800)")" --окно "$LATE_FROM" "$LATE_TO" --критерий К-1)
    case_check "К-1: n < 20 в окне — окно растёт до 3 суток, ПРОЙДЕН" "да" "$(verdict "$R" 0 ПРОЙДЕН)"
    R=$(run_instr k1-window "$(spec_of "$(join "$(msgs 20 0.5 1900)" "$(one 1999 -90000 '{"kick": null}')")")" \
        --окно "$W_FROM" "$W_TO" --критерий К-1)
    case_check "К-1: сообщение вне окна не судится — ПРОЙДЕН" "да" "$(verdict "$R" 0 ПРОЙДЕН)"

    echo "— К-2 (NTP)"
    printf 'no\n' > "$T/ntp-no"; printf 'yes\n' > "$T/ntp-yes"
    R=$(NTP_FILE="$T/ntp-no" run_instr k2-ntp "$(spec_of "$(msgs 20 0.5 2000)")" --окно "$W_FROM" "$W_TO" --критерий К-2)
    case_check "БОЛЬНОЙ К-2: часы не синхронизированы — ОТКАЗ, код 2" "да" "$(verdict "$R" 2 ОТКАЗ)"
    case_check "К-2: отказ называет NTP" "да" "$(has k2-ntp NTP)"
    R=$(NTP_FILE="" run_instr k2-ntp-unread "$(spec_of "$(msgs 20 0.5 2050)")" --окно "$W_FROM" "$W_TO" --критерий К-2)
    case_check "К-2: timedatectl не ответил — ОТКАЗ, не ПРОЙДЕН" "да" "$(verdict "$R" 2 ОТКАЗ)"
    R=$(NTP_FILE="$T/ntp-yes" run_instr k2-ok "$(spec_of "$(msgs 20 0.5 2100 0 1.0)")" --окно "$W_FROM" "$W_TO" --критерий К-2)
    case_check "К-2: телеграм → толчок ≈ 1,5 с — ПРОЙДЕН, код 0" "да" "$(verdict "$R" 0 ПРОЙДЕН)"
    R=$(NTP_FILE="$T/ntp-yes" run_instr k2-slow "$(spec_of "$(msgs 20 0.5 2200 0 5.0)")" --окно "$W_FROM" "$W_TO" --критерий К-2)
    case_check "БОЛЬНОЙ К-2: телеграм → приём 5 с — ПРОВАЛ, код 1" "да" "$(verdict "$R" 1 ПРОВАЛ)"

    echo "— К-3"
    R=$(run_instr k3-ok "$(spec_of "$(join "$(msgs 20 0.5 2300)" "$(one 2399 3000 '{"no_kick": "ответ панели"}')")")" \
        --окно "$W_FROM" "$W_TO" --критерий К-3)
    case_check "К-3: каждому received есть толчок или no_kick — ПРОЙДЕН" "да" "$(verdict "$R" 0 ПРОЙДЕН)"
    R=$(run_instr k3-bad "$(spec_of "$(join "$(msgs 20 0.5 2400)" "$(one 2499 3000 '{"kick": null}')")")" \
        --окно "$W_FROM" "$W_TO" --критерий К-3)
    case_check "БОЛЬНОЙ К-3: received без толчка и без причины — ПРОВАЛ" "да" "$(verdict "$R" 1 ПРОВАЛ)"

    echo "— К-5 (потери)"
    R=$(run_instr loss-ok "$(spec_of "$(join "$(msgs 5 0.5 2500)" "$(one 2599 3000 '{"no_kick": "команда status"}')" \
        "$(one 2598 3100 '{"refused": "пауза"}')")")" --окно "$W_FROM" "$W_TO" --потери)
    case_check "К-5: полный набор — ПРОЙДЕН, код 0" "да" "$(verdict "$R" 0 ПРОЙДЕН)"
    case_check "К-5: печатает «потерь 0»" "да" "$(has loss-ok 'потерь 0')"
    R=$(run_instr loss-queue "$(spec_of "$(join "$(msgs 5 0.5 2600)" "$(one 2699 3000 '{"queued": false, "kick": null}')")")" \
        --окно "$W_FROM" "$W_TO" --потери)
    case_check "БОЛЬНОЙ К-5: received без queued — потеря, ПРОВАЛ" "да" "$(verdict "$R" 1 ПРОВАЛ)"
    case_check "К-5: печатает «потерь 1» (received без queued)" "да" "$(has loss-queue 'потерь 1')"
    R=$(run_instr loss-kick "$(spec_of "$(join "$(msgs 5 0.5 2700)" "$(one 2799 3000 '{"kick": null}')")")" \
        --окно "$W_FROM" "$W_TO" --потери)
    case_check "БОЛЬНОЙ К-5: queued без толчка и отказа — потеря, ПРОВАЛ" "да" "$(verdict "$R" 1 ПРОВАЛ)"
    R=$(run_instr loss-skipped "$(spec_of "$(msgs 5 0.5 2800)" '[2899]')" --окно "$W_FROM" "$W_TO" --потери)
    case_check "БОЛЬНОЙ К-5: ядовитый update (tg-skipped) без received — потеря, ПРОВАЛ" "да" "$(verdict "$R" 1 ПРОВАЛ)"
    case_check "К-5: потеря называет update 2899" "да" "$(has loss-skipped 2899)"

    echo "— К-6 (очередь Claude Code)"
    # Стенограмма: на каждое сообщение — enqueue с путём файла очереди через 1 с
    # после приёма и выход из очереди через $3 с. Строки — форма queue-operation
    # живой стенограммы (сверено с zamer/queue_wait.py).
    transcript() {  # $1 = каталог, $2 = JSON сообщений, $3 = ожидание, $4 = журнал диспетчера (пусто — нет)
        python3 - "$1" "$2" "$3" "${4:-}" <<'PYEOF'
import json, sys, datetime as dt
out, msgs, wait, dlog = sys.argv[1], json.loads(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
def z(t): return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat().replace("+00:00", "Z")
with open(f"{out}/proba-session.jsonl", "w", encoding="utf-8") as fh:
    for m in msgs:
        ts = f"{m['at']:.9f}"
        content = f"Владелец написал: стенд — полная версия в /стенд/inbox/{ts}.txt, прочитай"
        fh.write(json.dumps({"type": "queue-operation", "operation": "enqueue", "timestamp": z(m["at"] + 1), "content": content}, ensure_ascii=False) + "\n")
        fh.write(json.dumps({"type": "queue-operation", "operation": "dequeue", "timestamp": z(m["at"] + 1 + wait)}) + "\n")
if dlog:
    with open(dlog, "w", encoding="utf-8") as fh:
        for m in msgs:
            ts = f"{m['at']:.9f}"
            clock = dt.datetime.fromtimestamp(m["at"]).strftime("%Y-%m-%d %H:%M:%S")
            fh.write(f"{clock}  в очередь: /стенд/inbox/{ts}.txt (message_id={m['uid']})\n")
PYEOF
    }
    k6() {  # $1 = имя, $2 = n, $3 = ожидание, $4 = «метки» или «журнал»
        local M; M=$(msgs "$2" 0.5 $((3000 + ${#1} * 37)))
        mkdir -p "$T/instr-$1/tr" "$T/instr-$1/log"
        if [ "$4" = журнал ]; then
            transcript "$T/instr-$1/tr" "$M" "$3" "$T/instr-$1/log/tg-dispatcher.log"
            run_instr "$1" "$(spec_of '[]')" --окно "$W_FROM" "$W_TO" --критерий К-6
        else
            transcript "$T/instr-$1/tr" "$M" "$3"
            run_instr "$1" "$(spec_of "$M")" --окно "$W_FROM" "$W_TO" --критерий К-6
        fi
    }
    R=$(k6 k6-slow 6 300 метки)
    case_check "БОЛЬНОЙ К-6: 6 сообщений ждали в очереди 300 с — ПРОВАЛ, код 1" "да" "$(verdict "$R" 1 ПРОВАЛ)"
    R=$(k6 k6-fast 6 1 метки)
    case_check "К-6: 6 сообщений вышли из очереди за 1 с — ПРОЙДЕН, код 0" "да" "$(verdict "$R" 0 ПРОЙДЕН)"
    R=$(k6 k6-few 3 1 метки)
    case_check "К-6: в очереди 3 сообщения (< 5) — ОТКАЗ, код 2" "да" "$(verdict "$R" 2 ОТКАЗ)"
    R=$(k6 k6-log 6 300 журнал)
    case_check "БОЛЬНОЙ К-6: окно без меток — сообщения из строк диспетчера, ПРОВАЛ" "да" "$(verdict "$R" 1 ПРОВАЛ)"

    echo "— сводка"
    R=$(run_instr summary "$(spec_of "$(msgs 20 0.5 4000)")" --окно "$W_FROM" "$W_TO")
    case_check "сводка: 20 сообщений — код 0" "0" "${R%%|*}"
    case_check "сводка: отрезок received→kick_sent с n=20" "да" \
        "$(grep -E 'received→kick_sent' "$T/instr-summary/out" 2>/dev/null | grep -qE 'n= *20\b' && echo да || echo нет)"
fi

# ═══ 3. КОД: журнал в реестре ══════════════════════════════════════════════
if want код; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 3: реестр журналов"
    case_check "журнал «доставка» в реестре, писатели — диспетчер и дверь pane.sh" "да" \
        "$(python3 - "$JOURNALS_YAML" <<'PYEOF'
import sys, yaml
j = (yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}).get("journals") or {}
e = j.get("доставка") or {}
w = e.get("writers") or []
ok = e.get("file") == "доставка.jsonl" and "scripts/tg-dispatcher.sh" in w and "scripts/lib/pane.sh" in w
print("да" if ok else f"нет ({e})")
PYEOF
)"
    case_check "прибор scripts/delivery-latency.py есть" "да" "$([ -r "$INSTRUMENT" ] && echo да || echo нет)"
fi

echo "судимых путей: $PATHS_JUDGED"
[ "$CASES_FAILED" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: путей $PATHS_JUDGED, случаев $CASES_PATHS"; exit 0; }
echo "ПРОБА КРАСНАЯ: путей $PATHS_JUDGED, неудач $CASES_FAILED из $CASES_PATHS"; exit 1
