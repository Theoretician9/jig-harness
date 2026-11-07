#!/usr/bin/env bash
#
# test_pane_send_now.sh — проба ручки PANE_SEND_NOW (карточка hr.pane-send-now-switch).
#
# Claude Code держит сообщение, пришедшее пока модель занята, в очереди;
# аккорд Ctrl+X Ctrl+S (chat:sendNow) отдаёт его сразу. Ручка в harness.conf
# решает, шлёт ли диспетчер этот аккорд после толчка текста. Спека:
# .tasks/hr.pane-send-now-switch/spec.md.
#
# Работает НАСТОЯЩИЙ tg-dispatcher.sh со своим циклом (образец —
# test_dispatcher_hang.sh). Подставлено и названо:
#   • Bot API — свой HTTP-сервер на localhost (TG_API_BASE);
#   • паспорт и пороги — свои файлы (HARNESS_INSTALL_CONF, HARNESS_CONF);
#   • tmux — свой файл первым в PATH: пишет каждый вызов и умеет висеть на
#     `send-keys … C-x`. Настоящий tmux проба не зовёт вовсе.
#
#   bash scripts/test_pane_send_now.sh [--диспетчер <путь>]
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

T=$(mktemp -d /tmp/proba-sendnow-XXXXXX)
echo $$ > "$T/owner"
STAMP="proba-sendnow-$$"        # имя висящих процессов подделки (их убирает и sweep_stand: PATH стенда)
WAIT_S=25                       # предел ожидания одного признака
QUIET_S=3                       # сколько ждать, что лишнего вызова НЕ будет
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
sweep_dead_stands /tmp/proba-sendnow-

# ── подделка tmux: журнал вызовов, зависание на C-x по режиму ───────────────
mkdir -p "$T/bin"
cat > "$T/bin/tmux" <<EOF
#!/usr/bin/env bash
dir="\${PROBA_DIR:?}"
mode=\$(cat "\$dir/mode" 2>/dev/null)
printf '%s\n' "\$*" >> "\$dir/tmux-calls"
case "\$1" in
    has-session)     exit 0 ;;
    display-message) case "\$*" in *pane_dead*) echo 0 ;; *) [ "\$mode" = shell ] && echo bash || echo claude ;; esac
                     exit 0 ;;
    send-keys)       case "\$*" in *" C-x") [ "\$mode" = hang-cx ] && exec -a "$STAMP" sleep 600 ;;
                                    *" C-s") [ "\$mode" = hang-cs ] && exec -a "$STAMP" sleep 600 ;; esac
                     exit 0 ;;
esac
exit 0
EOF
chmod +x "$T/bin/tmux"

# ── подставной Bot API ──────────────────────────────────────────────────────
# Сообщение 101 — сразу; 102 — только когда стенд положил файл release
# (проба успевает поменять ключ на живом диспетчере между толчками).
cat > "$T/api.py" <<'PYEOF'
import json
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

port, acks, ready, release = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]


def update(uid):
    return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": 777000},
                                          "text": f"срочно, сообщение {uid}"}}


def available():
    return [101, 102] if os.path.exists(release) else [101]


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
            if not ids:
                time.sleep(1)
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
called() { grep -Eqs "$2" "$1/tmux-calls"; }
enters_at_least() { [ "$(grep -cs '^send-keys .* Enter$' "$1/tmux-calls")" -ge "$2" ] 2>/dev/null; }

# Последовательность нажатий одной строкой: T<номер> — текст, E — Enter,
# X — C-x, S — C-s, ? — иное нажатие (видно, если появится лишнее).
key_seq() {  # $1 = каталог стенда
    grep '^send-keys' "$1/tmux-calls" 2>/dev/null \
      | sed -E 's/^send-keys .* -l -- .*сообщение ([0-9]+).*/T\1/;
                s/^send-keys -t [^ ]+ Enter$/E/;
                s/^send-keys -t [^ ]+ C-x$/X/;
                s/^send-keys -t [^ ]+ C-s$/S/;
                s/^send-keys .*/?/' | tr '\n' ' '
}

# Стенд одного случая. $1 = имя, $2 = режим tmux, $3 = строка ключа
# PANE_SEND_NOW для harness.conf (пусто — ключа нет вовсе).
start_stand() {
    local S="$T/$1" port
    mkdir -p "$S/log" "$S/project" "$S/panel"
    printf '%s' "$2" > "$S/mode"
    printf '000:СТЕНДОВЫЙ' > "$S/token"   # не-секрет: подставной Bot API стенда
    port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
    PROBA_STAND="$T/" python3 "$T/api.py" "$port" "$S/acks" "$S/api.ready" "$S/release" > "$S/api.out" 2>&1 &
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
    write_conf "$S" "$3"
    PROBA_DIR="$S" PATH="$T/bin:$PATH" HARNESS_PANEL_STATE="$S/panel" \
    HARNESS_INSTALL_CONF="$S/install.conf" HARNESS_CONF="$S/harness.conf" \
    TG_API_BASE="http://127.0.0.1:$port" \
        setsid nohup bash "$DISPATCHER" > "$S/dispatcher.out" 2>&1 < /dev/null &
    LAST_PID=$!
    disown "$LAST_PID"
    PIDS_DISP+=("$LAST_PID")
}

write_conf() {  # $1 = каталог стенда, $2 = строка ключа (пусто — без ключа)
    {
        printf 'TMUX_SESSION="%s"\n' "$STAMP-$(basename "$1")"
        printf 'SERVICE_LOCKS_SUBDIR="services"\n'
        printf 'PANE_TMUX_TIMEOUT="3"\n'
        printf 'PANE_ENTER_DELAY="0.1"\n'
        [ -z "$2" ] || printf '%s\n' "$2"
    } > "$1/harness.conf"
}

show_stand() {  # при красном — чем упал стенд
    echo "    --- журнал канала стенда $1 (хвост) ---"
    tail -8 "$T/$1/log/tg-dispatcher.log" 2>/dev/null | cut -c1-160 | sed 's/^/    /'
    echo "    --- вывод диспетчера (хвост) ---"
    tail -4 "$T/$1/dispatcher.out" 2>/dev/null | cut -c1-160 | sed 's/^/    /'
    echo "    --- вызовы tmux (хвост) ---"
    tail -8 "$T/$1/tmux-calls" 2>/dev/null | cut -c1-120 | sed 's/^/    /'
}

# Ключ не «1» — после текста и Enter ни одного лишнего нажатия.
case_no_send_now() {  # $1 = имя стенда, $2 = строка ключа, $3 = подпись
    local S="$T/$1" F0=$CASES_FAILED
    echo "— $3"
    start_stand "$1" normal "$2"
    case_check "условие создано: толчок 101 дошёл до Enter" да \
        "$(yes_no wait_for "$WAIT_S" enters_at_least "$S" 1)"
    sleep "$QUIET_S"
    case_check "$3: после текста только Enter, C-x/C-s нет" "T101 E " "$(key_seq "$S")"
    [ "$CASES_FAILED" != "$F0" ] && show_stand "$1"
    stop_stand "$LAST_PID"
}

# ═══ 1–3. ключ 0, ключа нет, ключ негодный — как сейчас ═══════════════════
case_no_send_now off  'PANE_SEND_NOW="0"' "ключ 0"
case_no_send_now none ''                  "ключа в конфиге нет"
case_no_send_now junk 'PANE_SEND_NOW="2"' "ключ негодный («2»)"

# ═══ 4. ключ 1 — текст, Enter, C-x, C-s отдельными вызовами ═══════════════
echo "— ключ 1"
F0=$CASES_FAILED
start_stand on normal 'PANE_SEND_NOW="1"'; S="$T/on"
case_check "условие создано: толчок 101 дошёл до Enter" да \
    "$(yes_no wait_for "$WAIT_S" enters_at_least "$S" 1)"
wait_for "$WAIT_S" called "$S" ' C-s$' >/dev/null
sleep 1
case_check "порядок нажатий: текст, Enter, C-x, C-s — каждое своим вызовом" \
    "T101 E X S " "$(key_seq "$S")"
case_check "журнал канала назвал отданный аккорд «C-x C-s»" да \
    "$(yes_no wait_for 5 journal_has "$S" 'C-x C-s')"
[ "$CASES_FAILED" != "$F0" ] && show_stand on
stop_stand "$LAST_PID"

# ═══ 5. ключ меняется на живом диспетчере — следующий толчок по новому ═════
# Диспетчер грузит конфиг один раз при старте. Ручка обещает «сразу», значит
# ключ обязан читаться в момент толчка, а не из памяти процесса.
echo "— ключ 0 → 1 без перезапуска диспетчера"
F0=$CASES_FAILED
start_stand live normal 'PANE_SEND_NOW="0"'; S="$T/live"
case_check "условие создано: толчок 101 дошёл до Enter при ключе 0" да \
    "$(yes_no wait_for "$WAIT_S" enters_at_least "$S" 1)"
sleep 1
write_conf "$S" 'PANE_SEND_NOW="1"'
: > "$S/release"
case_check "условие создано: толчок 102 дошёл до Enter" да \
    "$(yes_no wait_for "$WAIT_S" enters_at_least "$S" 2)"
wait_for "$WAIT_S" called "$S" ' C-s$' >/dev/null
sleep 1
case_check "БОЛЬНОЙ СЛУЧАЙ: 101 без аккорда, 102 — с аккордом после смены ключа" \
    "T101 E T102 E X S " "$(key_seq "$S")"
[ "$CASES_FAILED" != "$F0" ] && show_stand live
stop_stand "$LAST_PID"

# ═══ 6. висит C-x — C-s не шлётся, приём канала идёт, причина в журнале ═══
echo "— ключ 1, tmux висит на C-x"
F0=$CASES_FAILED
start_stand hang hang-cx 'PANE_SEND_NOW="1"'; S="$T/hang"
case_check "условие создано: C-x позван" да \
    "$(yes_no wait_for "$WAIT_S" called "$S" ' C-x$')"
case_check "журнал назвал зависший вызов: предел и код 124" да \
    "$(yes_no wait_for "$WAIT_S" journal_has "$S" 'tmux send-keys не вернулся за 3 ?с \(код 124\)')"
: > "$S/release"
case_check "сообщение 102 легло во входящие, пока аккорд висел" да \
    "$(yes_no wait_for "$WAIT_S" inbox_has "$S" 102)"
wait_for "$WAIT_S" enters_at_least "$S" 2 >/dev/null
sleep "$QUIET_S"
case_check "C-s без C-x не отдан ни разу (оборванный аккорд не дописан вслепую)" нет \
    "$(yes_no called "$S" ' C-s$')"
[ "$CASES_FAILED" != "$F0" ] && show_stand hang
stop_stand "$LAST_PID"

# ═══ 7. ключ 1, толчок отказан (в панели оболочка) — аккорда нет ══════════
# C-x C-s шлётся только после УСПЕШНОГО толчка: в голую оболочку C-s остановил бы
# вывод терминала, а C-x собрал бы чужое.
echo "— ключ 1, в панели оболочка: толчок отказан"
F0=$CASES_FAILED
start_stand shell shell 'PANE_SEND_NOW="1"'; S="$T/shell"
case_check "условие создано: журнал назвал отказ толчка" да \
    "$(yes_no wait_for "$WAIT_S" journal_has "$S" 'пинок агенту не отдан')"
sleep "$QUIET_S"
case_check "толчок отказан — ни C-x, ни C-s, ни текста" "" "$(key_seq "$S")"
case_check "толчок отказан — журнал не обещает «отдан сразу»" нет \
    "$(yes_no journal_has "$S" 'C-x C-s')"
[ "$CASES_FAILED" != "$F0" ] && show_stand shell
stop_stand "$LAST_PID"

# ═══ 8. ключ 1, C-x прошёл, висит C-s — журнал не обещает «отдан сразу» ═══
# Худший исход по замеру этапа Б: аккорд остался открытым и ~3 с съедает
# следующее событие. Журнал обязан назвать отказ, а не успех.
echo "— ключ 1, tmux висит на C-s"
F0=$CASES_FAILED
start_stand hangcs hang-cs 'PANE_SEND_NOW="1"'; S="$T/hangcs"
case_check "условие создано: C-s позван" да \
    "$(yes_no wait_for "$WAIT_S" called "$S" ' C-s$')"
case_check "C-s висит — журнал назвал отказ аккорда и код 124" да \
    "$(yes_no wait_for "$WAIT_S" journal_has "$S" 'C-x C-s не отдан: tmux send-keys не вернулся за 3 ?с \(код 124\)')"
case_check "C-s висит — журнал не пишет «отдан сразу»" нет \
    "$(yes_no journal_has "$S" 'отдан сразу')"
[ "$CASES_FAILED" != "$F0" ] && show_stand hangcs
stop_stand "$LAST_PID"

[ "$CASES_FAILED" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: $CASES_PATHS путей"; exit 0; }
echo "ПРОБА КРАСНАЯ: $CASES_FAILED из $CASES_PATHS"; exit 1
