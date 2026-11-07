#!/usr/bin/env bash
#
# test_dispatcher_hang.sh — проба: зависший tmux не останавливает приём канала.
#
# Больной случай 25.10.2026 (чужой сервер, отчёт
# .tasks/<карточка отчёта другой установки>/otchet.md §1). В 07:29 диспетчер принял
# «Выкатывай» и толкнул его в панель агента; `tmux send-keys -l` не вернулся
# 57 минут. Толчок шёл синхронно в цикле приёма: getUpdates не звался,
# tg_offset стоял, журнал молчал, пять сообщений владельца не дошли, пока tmux
# не убили руками.
#
# Работает НАСТОЯЩИЙ tg-dispatcher.sh со своим циклом. Подставлено и названо:
#   • Bot API — свой HTTP-сервер на localhost (TG_API_BASE);
#   • паспорт и пороги — свои файлы (HARNESS_INSTALL_CONF, HARNESS_CONF): свой
#     LOG_DIR, PROJECT_DIR, каталог панели, имя tmux-сессии;
#   • tmux — свой файл первым в PATH. Настоящий tmux проба не зовёт вовсе,
#     ни боевую сессию, ни свой сокет. Подделка пишет каждый вызов в журнал и
#     виснет на заданной подкоманде через `exec`: timeout должен убивать
#     именно висящий процесс, а не обёртку над ним.
#
# Порог толчка — ключ PANE_TMUX_TIMEOUT в harness.conf стенда (спека
# .tasks/<карточка отчёта другой установки>/spec-p1.md): проба не спит боевой
# потолок, предел ожидания каждого признака — WAIT_S.
#
#   bash scripts/test_dispatcher_hang.sh [--диспетчер <путь>]
# Код возврата: 0 — зелёная, 1 — красная.
#
# Имена переменных ЛАТИНИЦЕЙ (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8

KOREN="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
DISPATCHER="$KOREN/scripts/tg-dispatcher.sh"
[ "${1:-}" = "--диспетчер" ] && DISPATCHER="$2"
[ -r "$DISPATCHER" ] || { echo "ПРОБА: нет диспетчера $DISPATCHER"; exit 1; }

source "$KOREN/scripts/lib/selftest_cases.sh"
source "$KOREN/scripts/lib/stand_sweep.sh"

T=$(mktemp -d /tmp/proba-zavis-XXXXXX)
echo $$ > "$T/owner"
STAMP="proba-zavis-$$"          # имя висящих процессов подделки (их убирает и sweep_stand: PATH стенда)
WAIT_S=25                       # предел ожидания одного признака
PIDS_API=()
PIDS_DISP=()
LAST_PID=""

stop_stand() {  # $1 = PID диспетчера: он лидер своей группы (setsid)
    kill -KILL -- "-$1" 2>/dev/null; kill -KILL "$1" 2>/dev/null
    return 0
}

pribrat() {
    local p n
    for p in "${PIDS_DISP[@]}"; do stop_stand "$p"; done
    for p in "${PIDS_API[@]}"; do kill "$p" 2>/dev/null; done
    pkill -KILL -f "^$STAMP" 2>/dev/null
    n=$(sweep_stand "$T/"); [ "$n" = 0 ] || echo "убрано процессов стенда при выходе: $n" >&2
    rm -rf "$T"
}
trap pribrat EXIT
sweep_dead_stands /tmp/proba-zavis-

# ── подделка tmux: журнал вызовов и зависание на заданной подкоманде ────────
mkdir -p "$T/bin"
cat > "$T/bin/tmux" <<EOF
#!/usr/bin/env bash
dir="\${PROBA_DIR:?}"
mode=\$(cat "\$dir/mode" 2>/dev/null)
printf '%s\n' "\$*" >> "\$dir/tmux-calls"
case "\$1" in
    has-session)     [ "\$mode" = hang-has ] && exec -a "$STAMP" sleep 600; exit 0 ;;
    display-message) [ "\$mode" = hang-display ] && exec -a "$STAMP" sleep 600
                     case "\$*" in *pane_dead*) echo 0 ;; *) echo claude ;; esac; exit 0 ;;
    send-keys)       [ "\$mode" = hang-send ] && exec -a "$STAMP" sleep 600
                     [ "\$mode" = slow ] && sleep 1; exit 0 ;;
esac
exit 0
EOF
chmod +x "$T/bin/tmux"

# ── подставной Bot API ──────────────────────────────────────────────────────
# staged: update 101 сразу, 102 и 103 — через 2 с после первой выдачи 101
#         (владелец пишет ПОСЛЕ того, как толчок завис);
# all:    все три сразу.
# Каждый sendMessage пишется строкой в acks — так проба видит «принял».
cat > "$T/api.py" <<'PYEOF'
import json
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

port, mode, acks, ready = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
first_served = {"t": None}


def update(uid):
    return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": 777000},
                                          "text": f"срочно, сообщение {uid}"}}


def available():
    if mode == "all":
        return [101, 102, 103]
    if first_served["t"] is not None and time.time() - first_served["t"] >= 2:
        return [101, 102, 103]
    return [101]


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
            offset = int((form.get("offset") or ["0"])[0])
            ids = [u for u in available() if u >= offset]
            if ids and first_served["t"] is None:
                first_served["t"] = time.time()
            if not ids:
                time.sleep(1)       # long-poll: не крутить цикл впустую
            self.reply({"ok": True, "result": [update(u) for u in ids]})
            return
        with open(acks, "a") as fh:
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
inbox_has() { grep -qs "сообщение $2" "$1"/log/inbox/*.txt; }
journal_has() { grep -Eqs "$2" "$1/log/tg-dispatcher.log"; }
offset_is() { [ "$(cat "$1/log/tg_offset" 2>/dev/null)" = "$2" ]; }
ack_has() { grep -qs "принял: срочно, сообщение $2" "$1/acks"; }
kick_started() { grep -qs '^send-keys .* -l -- Владелец написал' "$1/tmux-calls"; }
called() { grep -qs "^$2" "$1/tmux-calls"; }

# Стенд одного случая: свой каталог, свой порт, свой диспетчер.
start_stand() {  # $1 = имя, $2 = режим tmux, $3 = PANE_TMUX_TIMEOUT, $4 = режим API
    local S="$T/$1" port
    mkdir -p "$S/log" "$S/project" "$S/panel"
    printf '%s' "$2" > "$S/mode"
    printf '000:СТЕНДОВЫЙ' > "$S/token"   # не-секрет: подставной Bot API стенда
    port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
    PROBA_STAND="$T/" python3 "$T/api.py" "$port" "$4" "$S/acks" "$S/api.ready" > "$S/api.out" 2>&1 &
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
PANE_TMUX_TIMEOUT="$3"
PANE_ENTER_DELAY="0.1"
EOF
    PROBA_DIR="$S" PATH="$T/bin:$PATH" HARNESS_PANEL_STATE="$S/panel" \
    HARNESS_INSTALL_CONF="$S/install.conf" HARNESS_CONF="$S/harness.conf" \
    TG_API_BASE="http://127.0.0.1:$port" \
        setsid nohup bash "$DISPATCHER" > "$S/dispatcher.out" 2>&1 < /dev/null &
    LAST_PID=$!
    disown "$LAST_PID"          # без строки «Killed» от bash при уборке
    PIDS_DISP+=("$LAST_PID")
}

show_stand() {  # при красном — чем упал стенд
    echo "    --- журнал канала стенда $1 (хвост) ---"
    tail -8 "$T/$1/log/tg-dispatcher.log" 2>/dev/null | cut -c1-160 | sed 's/^/    /'
    echo "    --- вывод диспетчера (хвост) ---"
    tail -4 "$T/$1/dispatcher.out" 2>/dev/null | cut -c1-160 | sed 's/^/    /'
    echo "    --- вызовы tmux (хвост) ---"
    tail -6 "$T/$1/tmux-calls" 2>/dev/null | cut -c1-120 | sed 's/^/    /'
}

# Висит одна подкоманда — приём идёт, журнал называет её. Общая часть
# случаев 2 и 3: has-session и display-message виснут так же, как send-keys.
hang_on() {  # $1 = подкоманда tmux
    local name="${1%%-*}" S F0=$CASES_FAILED
    echo "— зависает tmux $1"
    start_stand "$name" "hang-$name" 3 staged; S="$T/$name"
    case_check "условие создано: $1 позван" да "$(yes_no wait_for "$WAIT_S" called "$S" "$1")"
    case_check "сообщение 103 легло во входящие при зависшем $1" да \
        "$(yes_no wait_for "$WAIT_S" inbox_has "$S" 103)"
    case_check "журнал назвал зависший $1: предел и код 124" да \
        "$(yes_no wait_for "$WAIT_S" journal_has "$S" "tmux $1 не вернулся за 3 ?с \(код 124\)")"
    [ "$CASES_FAILED" != "$F0" ] && show_stand "$name"
    stop_stand "$LAST_PID"
}

# ═══ 1. БОЛЬНОЙ СЛУЧАЙ: висит send-keys ═══════════════════════════════════
echo "— зависает tmux send-keys (больной случай 25.10)"
start_stand send hang-send 3 staged; S="$T/send"
case_check "условие создано: толчок сообщения 101 ушёл в send-keys" да \
    "$(yes_no wait_for "$WAIT_S" kick_started "$S")"
case_check "сообщение 101 не потеряно — лежит во входящих" да "$(yes_no inbox_has "$S" 101)"
case_check "БОЛЬНОЙ СЛУЧАЙ: сообщение 102, пришедшее после зависания, легло во входящие" да \
    "$(yes_no wait_for "$WAIT_S" inbox_has "$S" 102)"
case_check "сообщение 103 легло во входящие" да "$(yes_no wait_for 5 inbox_has "$S" 103)"
case_check "владелец получил «принял» на 103" да "$(yes_no wait_for 5 ack_has "$S" 103)"
case_check "offset на диске прошёл 103 (tg_offset = 104)" да "$(yes_no wait_for 5 offset_is "$S" 104)"
case_check "журнал канала назвал зависший толчок: подкоманда, предел, код 124" да \
    "$(yes_no wait_for "$WAIT_S" journal_has "$S" 'tmux send-keys не вернулся за 3 ?с \(код 124\)')"
sleep 3
ALIVE_AGE=$(( $(date +%s) - $(stat -c %Y "$S/log/tg-dispatcher.alive" 2>/dev/null || echo 0) ))
case_check "признак круга tg-dispatcher.alive свежий (≤ 3 с), пока толчки висят" да \
    "$([ "$ALIVE_AGE" -le 3 ] && echo да || echo нет)"
[ "$CASES_FAILED" != 0 ] && show_stand send
stop_stand "$LAST_PID"

# ═══ 2–3. висят has-session и display-message ═════════════════════════════
hang_on has-session
hang_on display-message

# ═══ 4. толчки фоном не перемешиваются ════════════════════════════════════
# Риск САМОЙ починки: два фоновых толчка, набранные одновременно, дают
# «текст1 текст2 Enter Enter» — склеенное сообщение в строке ввода агента.
# Дверь толчка вправе отдать ждущие сообщения одной склейкой (lib/pane.sh,
# pane_compose): судится, что каждое названо ровно раз и у каждого текста свой Enter.
echo "— три срочных подряд, tmux медленный (1 с на нажатие)"
F0=$CASES_FAILED
start_stand slow slow 10 all; S="$T/slow"
named_msgs() { grep '^send-keys .* -l -- ' "$1/tmux-calls" 2>/dev/null | grep -oE 'сообщение [0-9]+' | sed 's/^сообщение //' | sort | tr '\n' ' '; }
all_named_and_entered() { [ "$(named_msgs "$1" | tr ' ' '\n' | sort -u | grep -c .)" -ge 3 ] && grep '^send-keys' "$1/tmux-calls" | tail -1 | grep -q ' Enter$'; }
wait_for 30 all_named_and_entered "$S" >/dev/null
SEQ=$(grep '^send-keys' "$S/tmux-calls" 2>/dev/null \
      | sed -E 's/^send-keys .* -l -- .*/T/; s/^send-keys .* Enter$/E/' | tr '\n' ' ')
case_check "три сообщения названы в толчках, каждое ровно раз" "101 102 103 " "$(named_msgs "$S")"
case_check "каждый текст толчка со своим Enter, без перемешивания" да \
    "$(grep -qE '^(T E )+$' <<< "$SEQ" && echo да || echo "нет ($SEQ)")"
[ "$CASES_FAILED" != "$F0" ] && show_stand slow
stop_stand "$LAST_PID"

# ═══ 5. фоновый толчок не держит лок диспетчера ═══════════════════════════
# Лок взят дескриптором 9; потомок наследует его и держит, пока жив (память:
# lock-leaked-into-a-child). Толчок, переживший диспетчер, держал бы лок, и
# рестарт ответил бы «УЖЕ РАБОТАЕТ» — канал лёг бы второй раз. Предел толчка
# здесь 60 с, чтобы висящий процесс был жив в момент проверки.
echo "— диспетчер умер, пока толчок висит"
F0=$CASES_FAILED
start_stand lock hang-send 60 staged; S="$T/lock"
LOCK="$S/log/locks/services/tg-dispatcher.lock"
wait_for "$WAIT_S" kick_started "$S" >/dev/null
# Толчок висит дольше предела ожидания: синхронный толчок держал бы цикл, и 102
# не дошло бы. Случаю с пределом 3 с этого не видно — там цикл отвисает сам.
case_check "толчок висит 60 с — сообщение 102 всё равно легло во входящие" да \
    "$(yes_no wait_for "$WAIT_S" inbox_has "$S" 102)"
kill -TERM "$LAST_PID" 2>/dev/null
sleep 1
case_check "условие создано: висящий tmux жив после смерти диспетчера" да \
    "$(pgrep -f "^$STAMP" >/dev/null && echo да || echo нет)"
case_check "лок диспетчера свободен — рестарт поднимется" свободен \
    "$(flock -n "$LOCK" true && echo свободен || echo занят)"
[ "$CASES_FAILED" != "$F0" ] && show_stand lock
stop_stand "$LAST_PID"

[ "$CASES_FAILED" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: $CASES_PATHS путей, первым — больной случай 25.10 (зависший send-keys)"; exit 0; }
echo "ПРОБА КРАСНАЯ: $CASES_FAILED из $CASES_PATHS"; exit 1
