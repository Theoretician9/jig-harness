#!/usr/bin/env bash
#
# test_hackathon_menu.sh — проба команд хакатона в меню бота (задача hr.hackathon-switch).
#
# Слово владельца 29.10.2026: «надо простой способ включения выключения
# [режима хакатона] в панель и в меню в телеграме». Три команды: hackathon_on
# (включить на 72 часа), hackathon_off (выключить), hackathon (статус). Пишет
# ключ единственный писатель настроек write-key.py, судья включённости —
# scripts/hackathon.py active().
#
# Работает НАСТОЯЩИЙ tg-dispatcher.sh со своим циклом (образец —
# test_pane_send_now.sh). Подставлено и названо:
#   • Bot API — свой HTTP-сервер на localhost (TG_API_BASE); входящие он
#     берёт из файла стенда, исходящие пишет в файл «чат<TAB>текст»;
#   • паспорт и пороги — свои файлы (HARNESS_INSTALL_CONF, HARNESS_CONF),
#     PANEL_NO_SUDO=1: писатель настроек правит файл стенда, а не /etc;
#   • PROJECT_DIR стенда — пустой каталог со ссылкой scripts на настоящие
#     скрипты: ветка вправе звать писателя и через $HERE, и через $PROJECT_DIR;
#   • tmux — свой файл первым в PATH; настоящий tmux проба не зовёт;
#   • реестр переключателей — копия настоящего (HARNESS_PERECLUCHATELI):
#     её порча — способ отказа писателя, не зависящий от прав root.
#
#   bash scripts/test_hackathon_menu.sh [--диспетчер <путь>]
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

T=$(mktemp -d /tmp/proba-hackathon-menu-XXXXXX)
S="$T/stand"
OWNER=777000
STRANGER=555111
WAIT_S=25                       # предел ожидания одного признака
QUIET_S=4                       # сколько ждать, что записи НЕ будет
PID_API=""
PID_DISP=""

pribrat() {
    [ -n "$PID_DISP" ] && { kill -KILL -- "-$PID_DISP" 2>/dev/null; kill -KILL "$PID_DISP" 2>/dev/null; }
    [ -n "$PID_API" ] && kill "$PID_API" 2>/dev/null
    rm -rf "$T"
}
trap pribrat EXIT

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

# ═══ 1. данные меню: слова владельца ведут в команды хакатона ══════════════
echo "— данные меню (bot-commands.yaml и --какая)"
for pair in "хакатон включить:hackathon_on" "включи хакатон:hackathon_on" \
            "/hackathon_on:hackathon_on" \
            "хакатон выключить:hackathon_off" "выключи хакатон:hackathon_off" \
            "/hackathon_off:hackathon_off" \
            "хакатон:hackathon" "хакатон статус:hackathon" "/hackathon:hackathon"; do
    word="${pair%:*}"; want="${pair##*:}"
    case_check "«$word» → /$want" "$want" \
        "$(python3 "$KOREN/scripts/bot-commands.py" --какая "$word" 2>/dev/null | sed -n 1p)"
done
descr() {  # $1 = имя команды → описание из данных
    python3 - "$KOREN/harness/config/bot-commands.yaml" "$1" <<'PYEOF'
import sys, yaml
data = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
print(next((c.get("описание", "") for c in data.get("команды") or [] if c.get("имя") == sys.argv[2]), ""))
PYEOF
}
case_check "описание hackathon_on" "Хакатон: включить на 72 часа" "$(descr hackathon_on)"
case_check "описание hackathon_off" "Хакатон: выключить" "$(descr hackathon_off)"
case_check "описание hackathon" "Хакатон: статус" "$(descr hackathon)"

# ═══ 2. стенд диспетчера ═══════════════════════════════════════════════════
mkdir -p "$T/bin" "$S/log" "$S/project" "$S/panel"
ln -s "$KOREN/scripts" "$S/project/scripts"
: > "$S/updates.jsonl"
printf '000:СТЕНДОВЫЙ' > "$S/token"   # не-секрет: подставной Bot API стенда

cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${PROBA_DIR:?}/tmux-calls"
case "$1" in
    display-message) case "$*" in *pane_dead*) echo 0 ;; *) echo claude ;; esac ;;
esac
exit 0
EOF
chmod +x "$T/bin/tmux"

# Bot API: входящие — строки JSON из updates.jsonl (стенд дописывает их по
# ходу), исходящие sendMessage — «чат<TAB>текст» в acks.
cat > "$T/api.py" <<'PYEOF'
import json
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

port, updates, acks, ready = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]


def available(offset):
    with open(updates, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    return [r for r in rows if r["update_id"] >= offset]


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
            rows = available(int((form.get("offset") or ["0"])[0]))
            if not rows:
                time.sleep(1)
            self.reply({"ok": True, "result": rows})
            return
        chat = (form.get("chat_id") or [""])[0]
        # Перевод строки виден знаком ⏎: ответ владельцу обязан быть одной строкой.
        text = (form.get("text") or [""])[0].replace("\n", "⏎")
        with open(acks, "a", encoding="utf-8") as fh:
            fh.write(f"{chat}\t{text}\n")
        self.reply({"ok": True, "result": {"message_id": 1}})

    do_GET = do_POST

    def log_message(self, *_):
        pass


server = HTTPServer(("127.0.0.1", port), Handler)
open(ready, "w").close()
server.serve_forever()
PYEOF

port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
python3 "$T/api.py" "$port" "$S/updates.jsonl" "$S/acks" "$S/api.ready" > "$S/api.out" 2>&1 &
PID_API=$!
wait_for 10 test -e "$S/api.ready" \
    || { echo "ПРОБА: Bot API стенда не поднялся на порту $port"; cat "$S/api.out"; exit 1; }
: > "$S/acks"

cat > "$S/install.conf" <<EOF
PROJECT_DIR="$S/project"
PROJECT_NAME="стенд"
LOG_DIR="$S/log"
TG_CHAT_ID="$OWNER"
TG_TOKEN_FILE="$S/token"
AGENT_USER="$(id -un)"
PANEL_STATE_DIR="$S/panel"
EOF
cat > "$S/harness.conf" <<EOF
TMUX_SESSION="proba-hackathon-menu-$$"
SERVICE_LOCKS_SUBDIR="services"
PANE_TMUX_TIMEOUT="3"
PANE_ENTER_DELAY="0.1"
CTX_ROTATE_PCT=75
HACKATHON_UNTIL=""
EOF
cp "$KOREN/harness/config/switches.yaml" "$S/switches.yaml"

PROBA_DIR="$S" PATH="$T/bin:$PATH" HARNESS_PANEL_STATE="$S/panel" PANEL_STATE_DIR="$S/panel" \
HARNESS_INSTALL_CONF="$S/install.conf" HARNESS_CONF="$S/harness.conf" LOG_DIR="$S/log" \
PANEL_NO_SUDO=1 HARNESS_PERECLUCHATELI="$S/switches.yaml" TG_API_BASE="http://127.0.0.1:$port" \
    setsid nohup bash "$DISPATCHER" > "$S/dispatcher.out" 2>&1 < /dev/null &
PID_DISP=$!
disown "$PID_DISP"

NEXT_ID=101
send() {  # $1 = чат, $2 = текст; запоминает число исходящих ДО сообщения
    ACKS_BEFORE=$(wc -l < "$S/acks")
    jq -cn --argjson id "$NEXT_ID" --argjson chat "$1" --arg text "$2" \
        '{update_id:$id, message:{message_id:$id, date:0, chat:{id:$chat}, text:$text}}' \
        >> "$S/updates.jsonl"
    NEXT_ID=$((NEXT_ID + 1))
}
# Исходящие владельцу после последнего send, без «принял: …» — это общий ack.
answers() { tail -n "+$((ACKS_BEFORE + 1))" "$S/acks" | awk -F'\t' -v c="$OWNER" '$1==c{print $2}' | grep -v '^принял' ; }
# Без трубы в grep -q: при pipefail ранний выход grep ронял бы писателя трубы.
answer_has() { local got; got=$(answers); grep -Eq "$1" <<< "$got"; }
key_line() { grep -E '^HACKATHON_UNTIL=' "$S/harness.conf"; }
is_active() { python3 -c 'import importlib.util,sys
s=importlib.util.spec_from_file_location("h", sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
sys.exit(0 if m.active(conf_path=sys.argv[2]) else 1)' "$KOREN/scripts/hackathon.py" "$S/harness.conf"; }
journal_has() { grep -Eqs "$1" "$S/log/tg-dispatcher.log"; }
written_until() { key_line | sed -E 's/^HACKATHON_UNTIL="?([^"]*)"?$/\1/'; }
clock_re() {  # часы:минуты срока в его поясе или в UTC — как ни напечатай
    python3 -c 'import datetime as d,sys
m=d.datetime.fromisoformat(sys.argv[1]); print(m.strftime("%H:%M")+"|"+m.astimezone(d.timezone.utc).strftime("%H:%M"))' "$1" 2>/dev/null
}

show_stand() {
    echo "    --- журнал канала стенда (хвост) ---"
    tail -8 "$S/log/tg-dispatcher.log" 2>/dev/null | cut -c1-160 | sed 's/^/    /'
    echo "    --- вывод диспетчера (хвост) ---"
    tail -4 "$S/dispatcher.out" 2>/dev/null | cut -c1-160 | sed 's/^/    /'
    echo "    --- исходящие (хвост) ---"
    tail -6 "$S/acks" 2>/dev/null | cut -c1-160 | sed 's/^/    /'
}

case_check "условие создано: диспетчер стенда стартовал" да \
    "$(yes_no wait_for "$WAIT_S" journal_has 'старт: слушаю чат')"

# ═══ 3. чужой чат: команда хакатона ничего не пишет ════════════════════════
echo "— чужой чат шлёт /hackathon_on"
send "$STRANGER" "/hackathon_on"
case_check "условие создано: диспетчер увидел чужое входящее" да \
    "$(yes_no wait_for "$WAIT_S" journal_has "чужое входящее: chat=$STRANGER")"
sleep "$QUIET_S"
case_check "чужая команда конфиг не тронула" 'HACKATHON_UNTIL=""' "$(key_line)"
case_check "чужому чату ничего не ответили" 0 \
    "$(awk -F'\t' -v c="$STRANGER" '$1==c' "$S/acks" | wc -l | tr -d ' ')"

# ═══ 4. статус при выключенном режиме ══════════════════════════════════════
echo "— «хакатон», режим выключен"
F0=$CASES_FAILED
send "$OWNER" "хакатон"
case_check "ответ «выключен»" да "$(yes_no wait_for "$WAIT_S" answer_has 'выключен')"
case_check "статус конфиг не тронул" 'HACKATHON_UNTIL=""' "$(key_line)"
[ "$CASES_FAILED" != "$F0" ] && show_stand

# ═══ 5. включить: запись без лишнего «да», ответ с концом срока ════════════
echo "— «включи хакатон»"
F0=$CASES_FAILED
send "$OWNER" "включи хакатон"
case_check "конфиг получил срок (без дополнительного «да»)" да \
    "$(yes_no wait_for "$WAIT_S" is_active)"
UNTIL=$(written_until)
case_check "срок записан моментом ISO с поясом в кавычках" да \
    "$(yes_no grep -Eq '^HACKATHON_UNTIL="[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}[+-][0-9]{2}:[0-9]{2}"$' "$S/harness.conf")"
CLOCK=$(clock_re "$UNTIL")
case_check "ответ владельцу называет конец срока (${CLOCK:-срока нет})" да \
    "$(yes_no wait_for "$WAIT_S" answer_has "${CLOCK:-НЕТ-СРОКА}")"
[ "$CASES_FAILED" != "$F0" ] && show_stand

# ═══ 6. статус при включённом режиме ═══════════════════════════════════════
echo "— «хакатон статус», режим включён"
F0=$CASES_FAILED
send "$OWNER" "хакатон статус"
case_check "ответ «включён до …» со сроком" да \
    "$(yes_no wait_for "$WAIT_S" answer_has "включён до.*(${CLOCK:-НЕТ-СРОКА})")"
[ "$CASES_FAILED" != "$F0" ] && show_stand

# ═══ 7. статус при прошедшем сроке ═════════════════════════════════════════
echo "— «/hackathon», срок прошёл"
F0=$CASES_FAILED
PAST=$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc).astimezone()-d.timedelta(hours=1)).replace(microsecond=0).isoformat())')
sed -i -E "s|^HACKATHON_UNTIL=.*$|HACKATHON_UNTIL=\"$PAST\"|" "$S/harness.conf"
send "$OWNER" "/hackathon"
case_check "ответ «срок истёк»" да "$(yes_no wait_for "$WAIT_S" answer_has 'срок истёк')"
[ "$CASES_FAILED" != "$F0" ] && show_stand

# ═══ 8. выключить ══════════════════════════════════════════════════════════
echo "— «выключи хакатон»"
F0=$CASES_FAILED
sed -i -E "s|^HACKATHON_UNTIL=.*$|HACKATHON_UNTIL=\"$UNTIL\"|" "$S/harness.conf"
send "$OWNER" "выключи хакатон"
case_check "конфиг: HACKATHON_UNTIL=\"\"" да \
    "$(yes_no wait_for "$WAIT_S" grep -qx 'HACKATHON_UNTIL=""' "$S/harness.conf")"
case_check "режим выключен по судье hackathon.py" нет "$(yes_no is_active)"
case_check "ответ «выключен» и про открытые долги" да \
    "$(yes_no wait_for "$WAIT_S" answer_has 'выключен.*долг|долг.*выключен')"
[ "$CASES_FAILED" != "$F0" ] && show_stand

# ═══ 9. писатель отказал: владелец слышит отказ одной строкой ══════════════
# Испорченный YAML: писатель отказывает многострочной причиной парсера.
echo "— «включи хакатон», писатель отказывает"
F0=$CASES_FAILED
cp "$S/switches.yaml" "$S/switches.yaml.good"
printf 'переключатели: [\n  - ключ: HACKATHON_UNTIL\n' > "$S/switches.yaml"
LINE_BEFORE=$(key_line)
send "$OWNER" "включи хакатон"
case_check "ответ «не включён»" да "$(yes_no wait_for "$WAIT_S" answer_has 'не включён')"
case_check "строка ключа не изменилась" "$LINE_BEFORE" "$(key_line)"
case_check "отказ — одна строка, без трассы" нет "$(yes_no answer_has '⏎|Traceback')"
mv "$S/switches.yaml.good" "$S/switches.yaml"
[ "$CASES_FAILED" != "$F0" ] && show_stand

[ "$CASES_FAILED" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: $CASES_PATHS путей"; exit 0; }
echo "ПРОБА КРАСНАЯ: $CASES_FAILED из $CASES_PATHS"; exit 1
