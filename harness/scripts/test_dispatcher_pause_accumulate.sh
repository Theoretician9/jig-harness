#!/usr/bin/env bash
#
# test_dispatcher_pause_accumulate.sh — проба: на паузе сообщения владельца
# копятся в очереди и в карте, смену не будят; «принял» идёт на каждое, а
# ответ про очередь — один на пачку.
#
# Слово владельца 02.11.2026: «я порой тебе накидываю инфу когда ты на паузе и
# мне не нужно чтобы ты сразу начинал работать, а нужно чтобы автоматом всё
# уходило в задачи и в историю, чтобы когда я включил плей ты разобрал всё и
# разложил по карте». И правило «не молчать»: тишина на паузе неотличима от
# потерянного сообщения, а два ответа на каждое слово — это шум.
#
# Работает НАСТОЯЩИЙ tg-dispatcher.sh со своим циклом. Подставлено и названо:
#   • Bot API — свой HTTP-сервер на localhost (TG_API_BASE), обновления он
#     берёт из файла стенда: проба дописывает туда «сообщения владельца»;
#   • паспорт и пороги — свои файлы (HARNESS_INSTALL_CONF, HARNESS_CONF): свой
#     LOG_DIR, PROJECT_DIR с болванкой карты, каталог панели, имя tmux-сессии;
#   • tmux — свой файл первым в PATH, пишет каждый вызов в журнал стенда;
#   • модель разбора — свой файл (HARNESS_SPROSIT_MODEL): пишет, о каком
#     сообщении её спросили, и отвечает «задач нет».
#
#   bash scripts/test_dispatcher_pause_accumulate.sh [--диспетчер <путь>]
# Код возврата: 0 — зелёная, 1 — красная.
#
# Имена переменных ЛАТИНИЦЕЙ (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
DISPATCHER="$ROOT/scripts/tg-dispatcher.sh"
[ "${1:-}" = "--диспетчер" ] && DISPATCHER="$2"
[ -r "$DISPATCHER" ] || { echo "ПРОБА: нет диспетчера $DISPATCHER"; exit 1; }
PAUSE_PY="$ROOT/scripts/pause.py"

source "$ROOT/scripts/lib/selftest_cases.sh"
source "$ROOT/scripts/lib/stand_sweep.sh"

T=$(mktemp -d /tmp/proba-pauza-kopit-XXXXXX)
echo $$ > "$T/owner"
STAMP="proba-pauza-kopit-$$"
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
sweep_dead_stands /tmp/proba-pauza-kopit-

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
import json, sys, time, urllib.parse
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
        with open(acks, "a", encoding="utf-8") as fh:
            fh.write((form.get("text") or [""])[0].replace("\n", " ") + "\n")
        self.reply({"ok": True, "result": {"message_id": 1}})

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
    PROBA_DIR="$S" PATH="$T/bin:$PATH" HARNESS_PANEL_STATE="$S/panel" \
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

# ═══ 1. БОЛЬНОЙ СЛУЧАЙ 02.11: три сообщения подряд на паузе ════════════════
echo "— пауза владельца, три сообщения подряд"
S="$T/pause"
mkdir -p "$S/panel"
HARNESS_PANEL_STATE="$S/panel" python3 "$PAUSE_PY" --поставить "проба" --кто владелец >/dev/null
case_check "условие создано: пауза стоит" да "$(yes_no test -e "$S/panel/пауза.json")"
start_stand pause
say_owner "$S" 201 "почини выгрузку отчётов, заметка-1"
say_owner "$S" 202 "добавь кнопку экспорта в таблицу, заметка-2"
say_owner "$S" 203 "сделай сводку продаж за неделю, заметка-3"
wait_for "$WAIT_S" offset_is "$S" 204 >/dev/null
wait_for "$WAIT_S" all_parsed "$S" заметка-1 заметка-2 заметка-3 >/dev/null
sleep 3   # лишний ответ или толчок фоном успел бы прийти
case_check "а) все три во входящих" "1 1 1" \
    "$(inbox_count "$S" заметка-1) $(inbox_count "$S" заметка-2) $(inbox_count "$S" заметка-3)"
case_check "а) все три в истории inbox.jsonl" "1 1 1" \
    "$(journal_count "$S" заметка-1) $(journal_count "$S" заметка-2) $(journal_count "$S" заметка-3)"
case_check "а) пауза на месте" да "$(yes_no test -e "$S/panel/пауза.json")"
case_check "б) каждый текст разобран в карту ровно раз" "1 1 1" \
    "$(model_count "$S" заметка-1) $(model_count "$S" заметка-2) $(model_count "$S" заметка-3)"
case_check "в) БОЛЬНОЙ СЛУЧАЙ: на пачку из трёх — один ответ про очередь, сверх трёх «принял»" "1 из 4" \
    "$(acks_exact "$S" "$BATCH_ACK") из $(count . "$S/acks")"
case_check "в) каждое сообщение паузы получило своё «принял: …» (указание 03.11.2026)" 3 "$(acks_starting "$S" 'принял:')"
case_check "в) ни одного «Стоит пауза — я стою…»" 0 "$(acks_starting "$S" 'Стоит пауза')"
case_check "д) в сессию ничего не передано (send-keys не звался)" 0 \
    "$(count '^send-keys' "$S/tmux-calls")"
case_check "д) толчок в панель не начинался" 0 \
    "$(count 'в панель: толчок' "$S/log/tg-dispatcher.log")"

# ═══ 2. метка ответа: обновляется каждым сообщением, молчит в окне ═══════
echo "— четвёртое сообщение в окне молчания: ответа нет, метка свежая"
MARK="$S/panel/пауза-сказано-приём"
case_check "условие создано: метка ответа стоит" да "$(yes_no test -e "$MARK")"
touch -d '-20 minutes' "$MARK"
say_owner "$S" 204 "поправь подпись на графике, заметка-4"
wait_for "$WAIT_S" all_parsed "$S" заметка-4 >/dev/null
sleep 3
case_check "г) в окне 30 минут второго ответа нет" 1 "$(acks_exact "$S" "$BATCH_ACK")"
case_check "г) метку обновило и сообщение без ответа (возраст ≤ 120 с, без обновления был бы 1200)" да \
    "$([ "$(mark_age "$MARK")" -le 120 ] && echo да || echo "нет ($(mark_age "$MARK") с)")"

echo "— сообщение после тишины ≥ 30 минут: снова один ответ"
touch -d '-31 minutes' "$MARK"
say_owner "$S" 205 "проверь отступы в письме, заметка-5"
wait_for "$WAIT_S" all_parsed "$S" заметка-5 >/dev/null
sleep 3
case_check "г) после 31 минуты тишины — снова ровно один ответ" 2 "$(acks_exact "$S" "$BATCH_ACK")"
case_check "д) в сессию по-прежнему ничего" 0 "$(count '^send-keys' "$S/tmux-calls")"

# ═══ 3. «продолжай»: смена получает накопленное как уже заведённое ═══════
echo "— владелец снял паузу словом «продолжай»"
say_owner "$S" 206 "продолжай"
resume_kicked() { grep -qs '^send-keys .*они уже заведены карточками: разбери накопленное в .*/inbox/ и разложи по карте' "$1/tmux-calls"; }
case_check "ж) толчок после снятия: накопленное уже в карточках, разложить по карте" да \
    "$(yes_no wait_for "$WAIT_S" resume_kicked "$S")"
case_check "ж) пауза снята" нет "$(yes_no test -e "$S/panel/пауза.json")"
[ "$CASES_FAILED" != 0 ] && show_stand pause
stop_stand "$LAST_PID"

# ═══ 3б. «продолжай» при пустых входящих: смена всё равно узнаёт о снятии ═══
echo "— пауза без сообщений, владелец снял её: смена обязана узнать"
F1=$CASES_FAILED
S="$T/lift"
mkdir -p "$S/panel"
HARNESS_PANEL_STATE="$S/panel" python3 "$PAUSE_PY" --поставить "проба" --кто владелец >/dev/null
case_check "условие создано: пауза стоит, входящих нет" да "$(yes_no test -e "$S/panel/пауза.json")"
start_stand lift
say_owner "$S" 401 "продолжай"
lift_kicked() { grep -qs '^send-keys .*Пауза снята владельцем — работа идёт, паузу больше не поминай' "$1/tmux-calls"; }
case_check "з) входящие пусты — смена всё равно получила слово о снятии паузы" да \
    "$(yes_no wait_for "$WAIT_S" lift_kicked "$S")"
case_check "з) пауза снята" нет "$(yes_no test -e "$S/panel/пауза.json")"
[ "$CASES_FAILED" != "$F1" ] && show_stand lift
stop_stand "$LAST_PID"

# ═══ 4. без паузы — прежнее ═══════════════════════════════════════════════
echo "— паузы нет: обычное «принял» и доставка"
F0=$CASES_FAILED
start_stand free; S="$T/free"
say_owner "$S" 301 "почини импорт клиентов, заметка-9"
delivered() { grep -qs '^send-keys .* -l -- Владелец написал: почини импорт клиентов' "$1/tmux-calls"; }
case_check "е) толчок в сессию со словами владельца" да "$(yes_no wait_for "$WAIT_S" delivered "$S")"
case_check "е) владелец получил «принял: …»" 1 "$(acks_exact "$S" "принял: почини импорт клиентов, заметка-9")"
case_check "е) ответа паузы нет" 0 "$(acks_exact "$S" "$BATCH_ACK")"
case_check "е) сообщение разобрано в карту" да "$(yes_no wait_for "$WAIT_S" all_parsed "$S" заметка-9)"
[ "$CASES_FAILED" != "$F0" ] && show_stand free
stop_stand "$LAST_PID"

[ "$CASES_FAILED" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: $CASES_PATHS путей, первым — больной случай 02.11 (три сообщения на паузе)"; exit 0; }
echo "ПРОБА КРАСНАЯ: $CASES_FAILED из $CASES_PATHS"; exit 1
