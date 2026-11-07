#!/usr/bin/env bash
#
# test_dispatcher_speed.sh — проба: команды режима скорости в tg-dispatcher.sh
# (спека .tasks/hr.speed-modes/spec.md, Ч5, п. 10.3.9).
#
# «режим ультра» обязан пройти цепочку: штатный писатель ключа (write-key.py
# SPEED_MODE ultra) → пересчёт судьи (speed_mode.py --выбран бот) → ответ
# владельцу из --показать --коротко. Без пересчёта владелец читает прежний
# режим; без запасной фразы молчание судьи — пустое сообщение; отказ писателя,
# пересказанный как успех, — режим «включён», а в конфиге старый.
#
# Работает НАСТОЯЩИЙ tg-dispatcher.sh. Подставлено и названо:
#   • Bot API — свой HTTP-сервер на localhost (TG_API_BASE);
#   • паспорт и harness.conf — свои файлы: LOG_DIR стенда, поэтому копия
#     конфига от write-key.py ложится в <стенд>/log/панель-копии, а не в боевой
#     каталог (дверь — HARNESS_INSTALL_CONF; проба это проверяет);
#   • судья скорости — свой файл <проект стенда>/harness/demons/speed_mode.py:
#     пишет каждый вызов вместе со значением SPEED_MODE в конфиге на этот миг;
#   • реестр переключателей для отказа — свой (HARNESS_PERECLUCHATELI);
#   • tmux — свой файл первым в PATH.
#
#   bash scripts/test_dispatcher_speed.sh [--диспетчер <путь>]
# Код возврата: 0 — зелёная, 1 — красная.
#
# Имена переменных ЛАТИНИЦЕЙ (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
DISPATCHER="$ROOT/scripts/tg-dispatcher.sh"
[ "${1:-}" = "--диспетчер" ] && DISPATCHER="$2"
[ -r "$DISPATCHER" ] || { echo "ПРОБА: нет диспетчера $DISPATCHER"; exit 1; }
# Боевой каталог копий: write-key.py со стенда не смеет в него писать.
LIVE_COPIES="${PROBA_LIVE_COPIES:-/var/log/harness/панель-копии}"

source "$ROOT/scripts/lib/selftest_cases.sh"
source "$ROOT/scripts/lib/stand_sweep.sh"

T=$(mktemp -d /tmp/proba-dispetcher-skorost-XXXXXX)
echo $$ > "$T/owner"
STAMP="proba-dispetcher-skorost-$$"
WAIT_S=30
FALLBACK="Режим записан; судья скорости пока не ответил, увижу на ближайшей проверке."
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
sweep_dead_stands /tmp/proba-dispetcher-skorost-

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

# Судья стенда: строка на вызов — доводы и SPEED_MODE в конфиге на этот миг.
# Файл judge-silent в стенде — судья молчит и падает.
cat > "$T/judge.py" <<'PYEOF'
import os, re, sys
stand = os.environ["PROBA_DIR"]
conf = open(os.environ["HARNESS_CONF"], encoding="utf-8").read()
found = re.search(r'^SPEED_MODE="?([a-z]*)"?', conf, re.M)
with open(stand + "/judge-calls", "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + " | SPEED_MODE=" + (found.group(1) if found else "-") + "\n")
if os.path.exists(stand + "/judge-silent"):
    sys.exit(2)
if "--показать" in sys.argv:
    mode = found.group(1) if found else "-"
    print(f"стенд-коротко: режим {mode}" if "--коротко" in sys.argv else f"стенд-полный: режим {mode}")
PYEOF

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
        if self.path.endswith("/sendMessage"):
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
count() { local n; n=$(grep -c "$@" 2>/dev/null); echo "${n:-0}"; }
# Ответы по существу: квитанция «принял: …» идёт на каждое сообщение и судится не здесь.
answers() { grep -v '^принял: ' "$1/acks" 2>/dev/null; }
offset_is() { [ "$(cat "$1/log/tg_offset" 2>/dev/null)" = "$2" ]; }
conf_mode() { sed -n 's/^SPEED_MODE="\{0,1\}\([a-z]*\)"\{0,1\}$/\1/p' "$1/harness.conf"; }
files_in() { find "$1" -maxdepth 1 -type f 2>/dev/null | wc -l; }

start_stand() {  # $1 = имя; окружение диспетчера дополняют EXTRA_ENV (массив VAR=знач)
    local S="$T/$1" port
    mkdir -p "$S/log" "$S/project/harness/demons" "$S/panel"
    printf 'lines: []\n' > "$S/project/dev-map.yaml"
    cp "$T/judge.py" "$S/project/harness/demons/speed_mode.py"
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
SPEED_MODE="medium"
EOF
    env "${EXTRA_ENV[@]}" PROBA_DIR="$S" PATH="$T/bin:$PATH" HARNESS_PANEL_STATE="$S/panel" \
        HARNESS_INSTALL_CONF="$S/install.conf" HARNESS_CONF="$S/harness.conf" \
        TG_API_BASE="http://127.0.0.1:$port" \
        setsid nohup bash "$DISPATCHER" > "$S/dispatcher.out" 2>&1 < /dev/null &
    LAST_PID=$!
    disown "$LAST_PID"
    PIDS_DISP+=("$LAST_PID")
}

show_stand() {
    echo "    --- ответы владельцу ---"
    sed 's/^/    /' "$T/$1/acks" 2>/dev/null | cut -c1-200
    echo "    --- вызовы судьи ---"
    sed 's/^/    /' "$T/$1/judge-calls" 2>/dev/null
    echo "    --- журнал канала (хвост) ---"
    tail -8 "$T/$1/log/tg-dispatcher.log" 2>/dev/null | cut -c1-200 | sed 's/^/    /'
    echo "    --- вывод диспетчера (хвост) ---"
    tail -4 "$T/$1/dispatcher.out" 2>/dev/null | cut -c1-200 | sed 's/^/    /'
}

# Метка начала: копию стенда в боевом каталоге узнаём по содержимому (имя сессии стенда).
touch "$T/start.mark"

# ═══ 1. «режим ультра»: писатель → судья пересчитал → ответ судьи ══════════
echo "— «режим ультра»: ключ записан, судья пересчитал, владелец получил его ответ"
EXTRA_ENV=()
start_stand ok; S="$T/ok"
case_check "условие создано: в конфиге стенда medium" medium "$(conf_mode "$S")"
say_owner "$S" 501 "режим ультра"
wait_for "$WAIT_S" offset_is "$S" 502 >/dev/null
answered() { [ "$(answers "$1" | grep -c .)" -ge 1 ]; }
wait_for "$WAIT_S" answered "$S" >/dev/null
sleep 2   # лишний ответ успел бы прийти
case_check "а) в конфиге стенда SPEED_MODE ultra" ultra "$(conf_mode "$S")"
case_check "а) копия прежнего конфига — в каталоге копий СТЕНДА" 1 "$(files_in "$S/log/панель-копии")"
case_check "а) запись ключа в журнале панели стенда" 1 "$(count 'SPEED_MODE' "$S/log/панель.jsonl")"
case_check "б) судья позван --выбран бот ПОСЛЕ записи ключа (видит ultra)" 1 \
    "$(count -xF -- '--выбран бот | SPEED_MODE=ultra' "$S/judge-calls")"
case_check "б) затем --показать --коротко, ровно два вызова судьи в этом порядке" \
    "--выбран бот|--показать --коротко" \
    "$(cut -d'|' -f1 "$S/judge-calls" 2>/dev/null | sed 's/ $//' | paste -sd'|')"
case_check "в) квитанция «принял» — одна" 1 "$(count '^принял: ' "$S/acks")"
case_check "в) владелец получил ровно один ответ — ответ судьи" "стенд-коротко: режим ultra" \
    "$(answers "$S" | paste -sd'#')"
case_check "в) в журнале канала — выбор владельца" 1 \
    "$(count 'владелец выбрал режим скорости ultra' "$S/log/tg-dispatcher.log")"

echo "— «скорость»: полный ответ судьи, конфиг не тронут"
: > "$S/judge-calls"
say_owner "$S" 502 "скорость"
second() { [ "$(answers "$1" | grep -c .)" -ge 2 ]; }
wait_for "$WAIT_S" second "$S" >/dev/null
sleep 2
case_check "г) второй ответ — полный --показать" "стенд-полный: режим ultra" "$(answers "$S" | sed -n 2p)"
case_check "г) судья позван один раз, без --выбран" "--показать | SPEED_MODE=ultra" "$(paste -sd'#' "$S/judge-calls")"
case_check "г) копий конфига не прибавилось" 1 "$(files_in "$S/log/панель-копии")"
[ "$CASES_FAILED" != 0 ] && show_stand ok
stop_stand "$LAST_PID"

# ═══ 2. судья молчит — запасная фраза, режим всё равно записан ═══════════
echo "— судья не ответил: запасная фраза, ключ записан"
F0=$CASES_FAILED
EXTRA_ENV=()
start_stand silent; S="$T/silent"
touch "$S/judge-silent"
say_owner "$S" 601 "режим медленный"
wait_for "$WAIT_S" answered "$S" >/dev/null
sleep 2
case_check "д) в конфиге стенда slow" slow "$(conf_mode "$S")"
case_check "д) владелец получил запасную фразу, одну" "$FALLBACK" "$(answers "$S" | paste -sd'#')"
case_check "д) судья всё же звался: --выбран и --показать" 2 "$(count . "$S/judge-calls")"
[ "$CASES_FAILED" != "$F0" ] && show_stand silent
stop_stand "$LAST_PID"

# ═══ 3. писатель отказал — строка отказа, судья не пересчитывает ════════
echo "— писатель настроек отказал: владелец слышит отказ, конфиг прежний"
F0=$CASES_FAILED
printf 'переключатели: []\nчёрный_список:\n  - { ключ: "SPEED_*", причина: "проба запретила" }\n' > "$T/registry.yaml"
EXTRA_ENV=("HARNESS_PERECLUCHATELI=$T/registry.yaml")
start_stand refused; S="$T/refused"
say_owner "$S" 701 "режим быстрый"
wait_for "$WAIT_S" answered "$S" >/dev/null
sleep 2
case_check "е) конфиг стенда прежний — medium" medium "$(conf_mode "$S")"
case_check "е) владелец получил строку отказа писателя" \
    "Режим не изменён: писатель настроек отказал — [запись-ключа] отказ: ключ «SPEED_MODE» менять нельзя — проба запретила" \
    "$(answers "$S" | paste -sd'#')"
case_check "е) судья не звался" 0 "$(count . "$S/judge-calls")"
case_check "е) в журнале канала — «не записан»" 1 \
    "$(count 'режим скорости fast не записан' "$S/log/tg-dispatcher.log")"
case_check "е) копии конфига нет" 0 "$(files_in "$S/log/панель-копии")"
[ "$CASES_FAILED" != "$F0" ] && show_stand refused
stop_stand "$LAST_PID"

stand_copies_live() {
    find "$LIVE_COPIES" -maxdepth 1 -type f -newer "$T/start.mark" -print0 2>/dev/null \
        | xargs -0 -r grep -lF "$STAMP" 2>/dev/null | wc -l
}
case_check "ж) в боевом каталоге копий нет ни одной копии конфига стенда" 0 "$(stand_copies_live)"

[ "$CASES_FAILED" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: $CASES_PATHS путей, первым — «режим ультра» доходит до судьи"; exit 0; }
echo "ПРОБА КРАСНАЯ: $CASES_FAILED из $CASES_PATHS"; exit 1
