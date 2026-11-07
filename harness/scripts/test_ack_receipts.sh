#!/usr/bin/env bash
#
# test_ack_receipts.sh — проба: «принял» доходит до владельца на КАЖДОЕ сообщение
# и это видно в журнале; сверка входящих с журналом ловит пропуск.
#
# Указание владельца 03.11.2026: «Надо чтобы всегда я видел это "принял"».
# До правки диспетчер слал «принял» и не записывал этого нигде (tg_out.jsonl
# пишет только tg_send.sh, то есть ответы смены), а на паузе вместо него шёл
# один ответ на пачку: проверить, что владелец услышан, было нечем.
#
# Работает НАСТОЯЩИЙ tg-dispatcher.sh; Bot API, паспорт, tmux и модель разбора
# подставлены так же, как в test_dispatcher_pause_accumulate.sh (боевой канал
# и боевой Telegram не задеваются). Сверка — настоящий check-ack-receipts.py на
# своих каталогах.
#
#   bash scripts/test_ack_receipts.sh [--диспетчер <путь>] [--сверка <путь>]
# Код возврата: 0 — зелёная, 1 — красная.
set -uo pipefail
export LC_ALL=C.UTF-8

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
DISPATCHER="$ROOT/scripts/tg-dispatcher.sh"
CHECK="$ROOT/scripts/check-ack-receipts.py"
while [ $# -gt 0 ]; do
    case "$1" in
        --диспетчер) DISPATCHER="$2"; shift 2 ;;
        --сверка) CHECK="$2"; shift 2 ;;
        *) shift ;;
    esac
done
[ -r "$DISPATCHER" ] || { echo "ПРОБА: нет диспетчера $DISPATCHER"; exit 1; }
[ -r "$CHECK" ] || { echo "ПРОБА: нет сверки $CHECK"; exit 1; }
PAUSE_PY="$ROOT/scripts/pause.py"

source "$ROOT/scripts/lib/selftest_cases.sh"
source "$ROOT/scripts/lib/stand_sweep.sh"

T=$(mktemp -d /tmp/proba-kvitanciya-XXXXXX)
echo $$ > "$T/owner"
STAMP="proba-kvitanciya-$$"
WAIT_S=30
BATCH_ACK="Принял в очередь (пауза): разложу по карте, работать начну после «продолжить»."
PIDS_API=()
PIDS_DISP=()
LAST_PID=""

stop_stand() {  # $1 = PID диспетчера: он лидер своей группы (setsid)
    kill -KILL -- "-$1" 2>/dev/null; kill -KILL "$1" 2>/dev/null
    return 0
}

cleanup() {
    local p n
    for p in "${PIDS_DISP[@]}"; do stop_stand "$p"; done
    for p in "${PIDS_API[@]}"; do kill "$p" 2>/dev/null; done
    n=$(sweep_stand "$T/"); [ "$n" = 0 ] || echo "убрано процессов стенда при выходе: $n" >&2
    rm -rf "$T"
}
trap cleanup EXIT
sweep_dead_stands /tmp/proba-kvitanciya-

mkdir -p "$T/bin"
cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${PROBA_DIR:?}/tmux-calls"
case "$1" in
    display-message) case "$*" in *pane_dead*) echo 0 ;; *) echo claude ;; esac ;;
esac
exit 0
EOF
chmod +x "$T/bin/tmux"

# Модель разбора: одна строка на вопрос — текст сообщения владельца в одну строку.
cat > "$T/model.py" <<'PYEOF'
import json, os, sys
question = open(sys.argv[sys.argv.index("--вопрос-файл") + 1], encoding="utf-8").read()
words = question.split("СООБЩЕНИЕ ВЛАДЕЛЬЦА:\n", 1)[-1].replace("\n", " ")
with open(os.environ["PROBA_DIR"] + "/model-calls", "a", encoding="utf-8") as fh:
    fh.write(words + "\n")
print(json.dumps({"задачи": [], "указания": [], "вопросы": [], "ответы": []}))
PYEOF

# Bot API: getUpdates отдаёт строки файла updates.jsonl от offset; каждый
# sendMessage — строка в acks.
cat > "$T/api.py" <<'PYEOF'
import json, os, sys, time, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

port, updates, acks, ready = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]


def pending(offset):
    try:
        rows = [json.loads(s) for s in open(updates, encoding="utf-8") if s.strip()]
    except FileNotFoundError:
        rows = []
    return [u for u in rows if u["update_id"] >= offset]


class Handler(BaseHTTPRequestHandler):
    def reply(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        if self.path.endswith("/getUpdates"):
            found = pending(int((form.get("offset") or ["0"])[0]))
            if not found:
                time.sleep(0.5)
            self.reply({"ok": True, "result": found})
            return
        if os.path.exists(updates + ".fail"):
            self.reply({"ok": False, "description": "stand: sendMessage refused"})
            return
        with open(acks, "a", encoding="utf-8") as fh:
            fh.write((form.get("text") or [""])[0].replace("\n", " ") + "\n")
        self.reply({"ok": True, "result": {"message_id": 4242}})

    do_GET = do_POST

    def log_message(self, *_):
        pass


server = HTTPServer(("127.0.0.1", port), Handler)
open(ready, "w").close()
server.serve_forever()
PYEOF

wait_for() {  # $1 = предел в секундах, остальное — условие
    local limit=$1; shift
    local end=$(( $(date +%s) + limit ))
    while [ "$(date +%s)" -lt "$end" ]; do
        "$@" && return 0
        sleep 0.5
    done
    "$@"
}
yes_no() { "$@" && echo да || echo нет; }

say_owner() {  # $1 = стенд, $2 = update_id, $3 = текст
    jq -cn --argjson id "$2" --arg t "$3" \
        '{update_id:$id, message:{message_id:$id, date:0, chat:{id:777000}, text:$t}}' >> "$1/updates.jsonl"
}
# Число строк; нет файла — 0. При нуле совпадений grep -c сам печатает 0 и
# выходит с 1, поэтому запасного «echo 0» нет.
count() { local n; n=$(grep -c "$@" 2>/dev/null); echo "${n:-0}"; }
inbox_count() { grep -ls "$2" "$1"/log/inbox/*.txt 2>/dev/null | wc -l; }
journal_count() { count "$2" "$1/log/inbox.jsonl"; }
model_count() { count "$2" "$1/model-calls"; }
acks_exact() { count -xF "$2" "$1/acks"; }
acks_starting() { count "^$2" "$1/acks"; }
offset_is() { [ "$(cat "$1/log/tg_offset" 2>/dev/null)" = "$2" ]; }
all_parsed() {  # $1 = стенд, дальше — метки сообщений: каждое разобрано
    local s=$1 n; shift
    for n in "$@"; do [ "$(model_count "$s" "$n")" -ge 1 ] || return 1; done
}
mark_age() { echo $(( $(date +%s) - $(stat -c %Y "$1" 2>/dev/null || echo 0) )); }

start_stand() {  # $1 = имя
    local S="$T/$1" port
    mkdir -p "$S/log" "$S/project/scripts" "$S/panel"
    printf 'lines: []\n' > "$S/project/dev-map.yaml"
    # «продолжай» снимает паузу через $PROJECT_DIR/scripts/pause.py.
    ln -s "$PAUSE_PY" "$S/project/scripts/pause.py"
    printf '000:СТЕНДОВЫЙ' > "$S/token"   # не-секрет: подставной Bot API стенда
    : > "$S/updates.jsonl"
    port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
    PROBA_STAND="$T/" python3 "$T/api.py" "$port" "$S/updates.jsonl" "$S/acks" "$S/api.ready" > "$S/api.out" 2>&1 &
    PIDS_API+=($!)
    wait_for 10 test -e "$S/api.ready" \
        || { echo "ПРОБА: Bot API стенда $1 не поднялся на порту $port"; cat "$S/api.out"; exit 1; }
    cat > "$S/install.conf" <<EOF
PROJECT_DIR="$S/project"
PROJECT_NAME="стенд"
LOG_DIR="$S/log"
TG_CHAT_ID="777000"
TG_TOKEN_FILE="$S/token"
AGENT_USER="$(id -un)"
PANEL_STATE_DIR="$S/panel"
EOF
    cat > "$S/harness.conf" <<EOF
TMUX_SESSION="$STAMP-$1"
SERVICE_LOCKS_SUBDIR="services"
PANE_TMUX_TIMEOUT="3"
PANE_ENTER_DELAY="0.1"
EOF
    # Окружение старше конфига: внешний LOG_DIR иначе уведёт входящие стенда в боевые.
    LOG_DIR="$S/log" PROBA_DIR="$S" PATH="$T/bin:$PATH" HARNESS_PANEL_STATE="$S/panel" \
    HARNESS_INSTALL_CONF="$S/install.conf" HARNESS_CONF="$S/harness.conf" \
    HARNESS_SPROSIT_MODEL="$T/model.py" TG_API_BASE="http://127.0.0.1:$port" \
        setsid nohup bash "$DISPATCHER" > "$S/dispatcher.out" 2>&1 < /dev/null &
    LAST_PID=$!
    disown "$LAST_PID"
    PIDS_DISP+=("$LAST_PID")
}

show_stand() {
    echo "    --- ответы владельцу ---"
    sed 's/^/    /' "$T/$1/acks" 2>/dev/null | cut -c1-160
    echo "    --- журнал канала (хвост) ---"
    tail -8 "$T/$1/log/tg-dispatcher.log" 2>/dev/null | cut -c1-160 | sed 's/^/    /'
    echo "    --- вывод диспетчера и разбора (хвост) ---"
    tail -4 "$T/$1/dispatcher.out" "$T/$1/log/map-from-message.log" 2>/dev/null | cut -c1-160 | sed 's/^/    /'
    echo "    --- вызовы tmux (хвост) ---"
    tail -4 "$T/$1/tmux-calls" 2>/dev/null | cut -c1-120 | sed 's/^/    /'
}

journal_rows() {  # $1 = стенд; строки delivery.ack журнала доставка
    grep '"event": "delivery.ack"' "$1/log/доставка.jsonl" 2>/dev/null || true
}
ack_field() {  # $1 = стенд, $2 = выражение jq над строкой, фильтр по тексту сообщения в $3
    journal_rows "$1" | jq -r "select(.data.message_id == \"$3\") | $2" 2>/dev/null | head -1
}
inbox_stem() { basename "$(grep -ls "$2" "$1"/log/inbox/*.txt 2>/dev/null | head -1)" .txt; }

# ═══ 1. обычное сообщение: «принял» ушёл и записан ═══════════════════════
echo "— обычное сообщение"
S="$T/plain"
start_stand plain
say_owner "$S" 301 "посчитай остатки склада, метка-1"
wait_for "$WAIT_S" offset_is "$S" 302 >/dev/null
sleep 3
case_check "а) владелец получил «принял: …»" 1 "$(acks_exact "$S" "принял: посчитай остатки склада, метка-1")"
case_check "а) в журнале ровно одна строка квитанции на сообщение" 1 "$(journal_rows "$S" | grep -c '"message_id": "301"')"
case_check "а) исход ok и id сообщения Telegram записан" "ok 4242" \
    "$(ack_field "$S" '[.outcome, .data.tg_message_id] | join(" ")' 301)"
STEM=$(inbox_stem "$S" метка-1)
case_check "а) метка квитанции = имя файла во входящих" "$STEM" "$(ack_field "$S" '.data.ts' 301)"
case_check "а) время отправки записано и не раньше приёма" да \
    "$(ack_field "$S" '(.data.sent_at|tonumber) >= (.data.ts|tonumber)' 301 | sed 's/true/да/;s/false/нет/')"

# ═══ 2. сбой отправки виден в журнале ═══════════════════════════════════
echo "— Telegram отказал на sendMessage"
touch "$S/updates.jsonl.fail"
say_owner "$S" 302 "проверь отгрузки, метка-2"
wait_for "$WAIT_S" offset_is "$S" 303 >/dev/null
sleep 3
case_check "б) исход error, причина названа" "error stand: sendMessage refused" \
    "$(ack_field "$S" '[.outcome, .data.error] | join(" ")' 302)"
rm -f "$S/updates.jsonl.fail"

# ═══ 3. команда меню ═════════════════════════════════════════════════════
echo "— команда меню"
say_owner "$S" 303 "/status"
wait_for "$WAIT_S" offset_is "$S" 304 >/dev/null
sleep 3
case_check "в) команда тоже квитируется (диспетчер их не исключает)" ok "$(ack_field "$S" '.outcome' 303)"
case_check "в) файла команды во входящих нет — сверка её не требует" 0 "$(inbox_count "$S" '/status')"

# ═══ 4. пауза ════════════════════════════════════════════════════════════
echo "— пауза владельца"
P="$T/pause"
mkdir -p "$P/panel"
HARNESS_PANEL_STATE="$P/panel" python3 "$PAUSE_PY" --поставить "проба" --кто владелец >/dev/null
case_check "условие создано: пауза стоит" да "$(yes_no test -e "$P/panel/пауза.json")"
start_stand pause
say_owner "$P" 401 "накидываю мысль, метка-4"
say_owner "$P" 402 "и ещё одну, метка-5"
wait_for "$WAIT_S" offset_is "$P" 403 >/dev/null
sleep 3
case_check "г) на паузе каждое сообщение получило своё «принял»" "1 1" \
    "$(acks_exact "$P" "принял: накидываю мысль, метка-4") $(acks_exact "$P" "принял: и ещё одну, метка-5")"
case_check "г) обе квитанции в журнале с исходом ok" "ok ok" \
    "$(ack_field "$P" '.outcome' 401) $(ack_field "$P" '.outcome' 402)"

# ═══ 5. журнал включён: метка старта ═════════════════════════════════════
case_check "д) диспетчер при старте отметил начало журнала квитанций" 1 \
    "$(count '"event": "delivery.ack_journal_on"' "$S/log/доставка.jsonl")"

# ═══ 6. сверка входящих с журналом (настоящий скрипт, свои каталоги) ═════
echo "— сверка входящих с журналом квитанций"
C="$T/check"
mkdir -p "$C/inbox/обработано"
NOW=$(date +%s)
jr() {  # $1 = событие, $2 = исход, $3 = метка, $4 = время отправки
    jq -cn --arg e "$1" --arg o "$2" --arg ts "$3" --argjson s "$4" \
        '{ts:"2026-11-03T00:00:00+05:00", event:$e, outcome:$o, data:{ts:$ts, sent_at:$s, started_at:$s}}' >> "$C/доставка.jsonl"
}
check_rc() { python3 "$CHECK" --log-dir "$C" >"$C/out" 2>&1; echo $?; }
jr delivery.ack_journal_on ok - $(( NOW - 7200 ))
M_OK="$(( NOW - 600 )).100000000"
M_LATE="$(( NOW - 90 )).200000000"
M_FRESH="$(( NOW - 20 )).300000000"
M_OLD="$(( NOW - 9000 )).400000000"
echo ok > "$C/inbox/обработано/$M_OK.txt"; jr delivery.ack ok "$M_OK" $(( NOW - 599 ))
case_check "е) квитанция есть — зелено" 0 "$(check_rc)"
echo x > "$C/inbox/$M_FRESH.txt"
case_check "е) без квитанции, но моложе 60 с — зелено" 0 "$(check_rc)"
echo x > "$C/inbox/$M_LATE.txt"
case_check "е) без квитанции 90 с — нарушение" 1 "$(check_rc)"
case_check "е) нарушение называет сообщение" 1 "$(grep -c "^$M_LATE" "$C/out")"
case_check "е) моложе 60 с нарушением не названо" 0 "$(grep -c "^$M_FRESH" "$C/out")"
rm "$C/inbox/$M_LATE.txt"; echo x > "$C/inbox/обработано/$M_LATE.txt"
case_check "е) обработанное без квитанции тоже нарушение" 1 "$(check_rc)"
jr delivery.ack error "$M_LATE" $(( NOW - 80 ))
case_check "е) квитанция с исходом error не считается" 1 "$(check_rc)"
jr delivery.ack ok "$M_LATE" $(( NOW - 70 ))
rm "$C/inbox/$M_FRESH.txt"
case_check "е) после удачной квитанции — зелено" 0 "$(check_rc)"
echo x > "$C/inbox/обработано/$M_OLD.txt"
case_check "е) сообщение до начала журнала не судится" 0 "$(check_rc)"
echo x > "$C/inbox/not-a-stamp.txt"
case_check "е) чужой файл в inbox не роняет сверку" 0 "$(check_rc)"
mkdir -p "$T/check-empty/inbox"
case_check "е) журнала квитанций ещё нет — не нарушение" 0 \
    "$(python3 "$CHECK" --log-dir "$T/check-empty" >/dev/null 2>&1; echo $?)"

[ "$CASES_FAILED" != 0 ] && { show_stand plain; show_stand pause; }
[ "$CASES_FAILED" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: $CASES_PATHS путей"; exit 0; }
echo "ПРОБА КРАСНАЯ: $CASES_FAILED из $CASES_PATHS"; exit 1
