#!/usr/bin/env bash
# Проба этапа 1б: смена аккаунта из Telegram
# (спека задачи, раздел «Этап 1б»).
#
# Слово владельца 30.10: «мне надо аккаунт сменить … чтобы я через cli это не
# делал?»; 07.11: «все аккаунты, авто или по выбору». Путь: команда «аккаунт»
# → бот шлёт аккаунты кнопками (подпись, какой идёт; ни ключа, ни имени файла
# ключа) → нажатие владельца = выбор → бот зовёт дверь
# `account-switch.py --хочу <имя>` фоном. Переключение в тишине делает сторож
# сессий — его здесь нет: дверь подменена, проба судит только маршрут бота.
#
# Беды, которые ловятся: команды нет, и слово уходит в inbox; в кнопке или
# тексте виден ключ или имя файла ключа (И-3); чужой from.id или chat.id
# переключает аккаунт; одно меню переключает дважды; нажатие на имя, которого
# в меню не было, доходит до двери; кнопки согласия «Да/Нет» сломаны новым
# разбором нажатий.
#
# Этап 1в (спека §1в.4, §1в.6, §1в.7): сторож ротирует смену в лимите только по
# живому «аккаунт в лимите», ответ --ждёт 4 не даёт второй ротации, помощник,
# стоящий в лимите, ротацию не держит (раздел 1в-С, чистые функции сторожа);
# меню аккаунтов помечает аккаунт в лимите (А-18); диспетчер сверяет меню бота
# фоном при старте и не держит им канал (Б-8).
#
# Работает НАСТОЯЩИЙ tg-dispatcher.sh. Подставлено и названо:
#   • Bot API — свой HTTP-сервер на 127.0.0.1 (TG_API_BASE), пишет каждый вызов;
#   • дверь переключения — свой файл <проект стенда>/scripts/account-switch.py
#     (диспетчер зовёт её от PROJECT_DIR, как session-warden.sh и pause.py);
#   • профили и state.json — свои (HARNESS_ACCOUNT_DIR), ключи — фиктивные;
#   • tmux — свой файл первым в PATH.
#
#   bash scripts/test_switch_account_bot.sh [--диспетчер <путь>]
# Код возврата: 0 — зелёная, 1 — красная.
#
# Имена переменных ЛАТИНИЦЕЙ (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
DISPATCHER="$HERE/tg-dispatcher.sh"
[ "${1:-}" = "--диспетчер" ] && DISPATCHER="$2"
[ -r "$DISPATCHER" ] || { echo "ПРОБА: нет диспетчера $DISPATCHER"; exit 1; }

source "$HERE/lib/selftest_cases.sh"
source "$HERE/lib/stand_sweep.sh"

T=$(mktemp -d /tmp/proba-switch-acc-bot-XXXXXX)
STAMP="proba-switch-acc-bot-$$"
WAIT_S=20
OWNER=777000
PIDS_API=()
DISP_PID=""

cleanup() {
    local p n
    [ -n "$DISP_PID" ] && { kill -KILL -- "-$DISP_PID" 2>/dev/null; kill -KILL "$DISP_PID" 2>/dev/null; }
    for p in "${PIDS_API[@]}"; do kill "$p" 2>/dev/null; done
    n=$(sweep_stand "$T/"); [ "$n" = 0 ] || echo "убрано процессов стенда при выходе: $n" >&2
    rm -rf "$T"
}
trap cleanup EXIT
sweep_dead_stands /tmp/proba-switch-acc-bot-

S="$T/s"
mkdir -p "$T/bin" "$S/log" "$S/project/scripts" "$S/project/harness/demons" "$S/panel" "$S/acc" "$S/secrets"
chmod 700 "$S/secrets"

cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${PROBA_DIR:?}/tmux-calls"
case "$1" in
    display-message) case "$*" in *pane_dead*) echo 0 ;; *) echo claude ;; esac ;;
esac
exit 0
EOF
chmod +x "$T/bin/tmux"

# Подставная дверь: строка доводов на вызов и та же строка в stdout — по ней
# Б-8 видит, куда диспетчер кладёт вывод двери.
cat > "$S/project/scripts/account-switch.py" <<'EOF'
import os, sys
with open(os.path.join(os.environ["PROBA_DIR"], "switch-calls"), "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
print("дверь-стенда: " + " ".join(sys.argv[1:]))
EOF
printf 'lines: []\n' > "$S/project/dev-map.yaml"

# ── фиктивные ключи: имя файла — тоже секрет (И-3), его ищем в выводе ─────
rnd() { head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
KEY_A="proba-bot-klyuch-alpha-$(rnd)"; KEY_B="proba-bot-klyuch-beta-$(rnd)"
FILE_A="claude-oauth-imya-$(rnd)"; FILE_B="claude-oauth-imya-$(rnd)"
printf '%s' "$KEY_A" > "$S/secrets/$FILE_A"; printf '%s' "$KEY_B" > "$S/secrets/$FILE_B"
chmod 600 "$S/secrets"/*
python3 -I - "$S/acc" "$FILE_A" "$FILE_B" <<'PY'
import json, sys
d, fa, fb = sys.argv[1:]
json.dump({"профили": [
    {"имя": "alpha", "подпись": "Альфа", "орг": "0rg-a", "почта": "", "ключ": fa, "отпечаток": "aaaa1111"},
    {"имя": "beta", "подпись": "Бета", "орг": "0rg-b", "почта": "", "ключ": fb, "отпечаток": "bbbb2222"},
]}, open(d + "/profiles.json", "w", encoding="utf-8"), ensure_ascii=False)
json.dump({"запуск": "alpha", "идёт": "alpha", "переключения": []},
          open(d + "/state.json", "w", encoding="utf-8"), ensure_ascii=False)
PY

# ── подставной Bot API: getUpdates из файла, каждый вызов — строкой JSON ───
# Меню бота (Б-8): getMyCommands отдаёт mycommands.json стенда спустя
# mycommands.delay секунд и пишет время приёма и ответа; setMyCommands
# заменяет mycommands.json присланным. Сервер многопоточный: долгий
# getMyCommands не должен держать getUpdates — это судит Б-8, а не стенд.
cat > "$T/api.py" <<'PYEOF'
import json, os, sys, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port, updates, calls, ready = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
STAND = os.path.dirname(calls)
NEXT = [100]
LOCK = threading.Lock()


def stand_text(name, default):
    try:
        return open(os.path.join(STAND, name), encoding="utf-8").read().strip() or default
    except FileNotFoundError:
        return default


def record(row):
    with LOCK, open(calls, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


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
        raw = self.rfile.read(length).decode()
        method = self.path.rsplit("/", 1)[-1]
        if method == "getMyCommands":
            t_in = time.time()
            time.sleep(float(stand_text("mycommands.delay", "0")))
            result = json.loads(stand_text("mycommands.json", "[]"))
            record({"method": method, "form": {}, "t_in": t_in, "t_out": time.time()})
            self.reply({"ok": True, "result": result})
            return
        if method == "setMyCommands":
            body = json.loads(raw or "{}")
            with LOCK, open(os.path.join(STAND, "mycommands.json"), "w", encoding="utf-8") as fh:
                json.dump(body.get("commands") or [], fh, ensure_ascii=False)
            record({"method": method, "form": body, "t_in": time.time()})
            self.reply({"ok": True, "result": True})
            return
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
        if method == "getUpdates":
            with LOCK:
                first = os.path.join(STAND, "api-first-getupdates")
                if not os.path.exists(first):
                    with open(first, "w") as fh:
                        fh.write(repr(time.time()))
            found = pending(int(form.get("offset") or "0"))
            if not found:
                time.sleep(0.5)
            self.reply({"ok": True, "result": found})
            return
        result = True
        if method == "sendMessage":
            with LOCK:
                NEXT[0] += 1
                form["_message_id"] = NEXT[0]
            result = {"message_id": form["_message_id"], "chat": {"id": int(form.get("chat_id") or 0)}}
        record({"method": method, "form": form})
        self.reply({"ok": True, "result": result})

    do_GET = do_POST

    def log_message(self, *_):
        pass


server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
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
yes_no() { "$@" >/dev/null 2>&1 && echo да || echo нет; }
say_owner() {  # $1 = update_id, $2 = текст
    jq -cn --argjson id "$1" --arg t "$2" --argjson o "$OWNER" \
        '{update_id:$id, message:{message_id:$id, date:0, chat:{id:$o}, from:{id:$o}, text:$t}}' >> "$S/updates.jsonl"
}
press() {  # $1 = update_id, $2 = from.id, $3 = chat.id, $4 = message_id, $5 = data
    jq -cn --argjson id "$1" --argjson f "$2" --argjson c "$3" --argjson m "$4" --arg d "$5" \
        '{update_id:$id, callback_query:{id:("cb" + ($id|tostring)), from:{id:$f},
          message:{message_id:$m, chat:{id:$c}}, data:$d}}' >> "$S/updates.jsonl"
}
offset_is() { [ "$(cat "$S/log/tg_offset" 2>/dev/null)" = "$1" ]; }
# Меню — sendMessage с кнопками: печатает «message_id<TAB>reply_markup» N-го такого (с 1).
menu_n() {
    jq -r 'select(.method == "sendMessage" and (.form.reply_markup // "") != "")
           | "\(.form._message_id)\t\(.form.reply_markup)"' "$S/api-calls.jsonl" 2>/dev/null | sed -n "${1}p"
}
menus_at_least() { [ -n "$(menu_n "$1")" ]; }
calls_of() { jq -c --arg m "$1" 'select(.method == $m)' "$S/api-calls.jsonl" 2>/dev/null; }
switch_lines() { local n; n=$(grep -c . "$S/switch-calls" 2>/dev/null); echo "${n:-0}"; }
switch_at_least() { [ "$(switch_lines)" -ge "$1" ]; }
answers_at_least() { [ "$(calls_of answerCallbackQuery | grep -c .)" -ge "$1" ]; }

: > "$S/updates.jsonl"; : > "$S/api-calls.jsonl"
# Токен латиницей: bot-commands.py зовёт API через urllib, а нелатинский путь
# адреса тот не отправит вовсе (UnicodeEncodeError) — меню стенд бы не увидел.
printf '000:STENDOVYJ' > "$S/token"
# Меню бота в Telegram стенда до старта (Б-8): всё из данных, кроме account;
# ответ на getMyCommands — через 15 с, дольше, чем канал вправе ждать.
COMMANDS_YAML="$HERE/../harness/config/bot-commands.yaml"
[ -f "$COMMANDS_YAML" ] || COMMANDS_YAML="$HERE/../config/bot-commands.yaml"
python3 -I -c '
import json, sys, yaml
full = [{"command": str(k["имя"]), "description": str(k["описание"])}
        for k in yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["команды"]]
json.dump(full, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
json.dump([k for k in full if k["command"] != "account"], open(sys.argv[3], "w", encoding="utf-8"), ensure_ascii=False)
' "$COMMANDS_YAML" "$S/commands-full.json" "$S/mycommands.json" || { echo "ПРОБА: данные команд бота не прочитаны"; exit 1; }
printf '15' > "$S/mycommands.delay"
port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
python3 -I "$T/api.py" "$port" "$S/updates.jsonl" "$S/api-calls.jsonl" "$S/api.ready" > "$S/api.out" 2>&1 &
PIDS_API+=($!)
wait_for 10 test -e "$S/api.ready" || { echo "ПРОБА: Bot API стенда не поднялся"; cat "$S/api.out"; exit 1; }
cat > "$S/install.conf" <<EOF
PROJECT_DIR="$S/project"
PROJECT_NAME="стенд"
LOG_DIR="$S/log"
TG_CHAT_ID="$OWNER"
TG_TOKEN_FILE="$S/token"
AGENT_USER="$(id -un)"
PANEL_STATE_DIR="$S/panel"
SECRETS_DIR="$S/secrets"
EOF
cat > "$S/harness.conf" <<EOF
TMUX_SESSION="$STAMP"
SERVICE_LOCKS_SUBDIR="services"
PANE_TMUX_TIMEOUT="3"
PANE_ENTER_DELAY="0.1"
EOF
start_dispatcher() {
    env PROBA_DIR="$S" PATH="$T/bin:$PATH" HARNESS_PANEL_STATE="$S/panel" \
        HARNESS_INSTALL_CONF="$S/install.conf" HARNESS_CONF="$S/harness.conf" \
        HARNESS_ACCOUNT_DIR="$S/acc" SECRETS_DIR="$S/secrets" LOG_DIR="$S/log" \
        TG_API_BASE="http://127.0.0.1:$port" TG_TOKEN_FILE="$S/token" \
        setsid nohup bash "$DISPATCHER" >> "$S/dispatcher.out" 2>&1 < /dev/null &
    DISP_PID=$!
    disown "$DISP_PID"
}
start_dispatcher

echo "── Б-1: команда «аккаунт» → аккаунты кнопками, без ключей ──"
say_owner 501 "аккаунт"
wait_for "$WAIT_S" menus_at_least 1 >/dev/null
MENU1="$(menu_n 1)"
MID1="${MENU1%%$'\t'*}"; MARKUP1="${MENU1#*$'\t'}"
case_check "Б-1 бот прислал сообщение с кнопками" да "$([ -n "$MENU1" ] && echo да || echo нет)"
DATA1="$(printf '%s' "$MARKUP1" | jq -r '.inline_keyboard[][] | .callback_data' 2>/dev/null)"
case_check "Б-1 кнопок три: alpha, beta и вход /login (имена — из callback_data)" "alpha beta login" \
    "$(printf '%s\n' "$DATA1" | sed -n 's/^a:[A-Za-z0-9_-]\{22\}:\([a-z0-9-]*\)$/\1/p' | sort | paste -sd' ')"
case_check "Б-1 у всех кнопок один nonce меню" 1 \
    "$(printf '%s\n' "$DATA1" | grep . | cut -d: -f2 | sort -u | grep -c .)"
NONCE1="$(printf '%s\n' "$DATA1" | head -n1 | cut -d: -f2)"
case_check "Б-1 подпись идущего аккаунта помечена «сейчас»" да \
    "$(printf '%s' "$MARKUP1" | jq -e '[.inline_keyboard[][] | select(.callback_data | endswith(":alpha")) | .text] | .[0] | test("Альфа.*сейчас")' >/dev/null 2>&1 && echo да || echo нет)"
case_check "Б-1 подпись второго аккаунта — как назвал владелец" да \
    "$(printf '%s' "$MARKUP1" | jq -e '[.inline_keyboard[][] | .text] | any(test("Бета"))' >/dev/null 2>&1 && echo да || echo нет)"
LEAK=0
for secret in "$KEY_A" "$KEY_B" "$FILE_A" "$FILE_B"; do
    grep -qF -- "$secret" "$S/api-calls.jsonl" && LEAK=$((LEAK + 1))
done
case_check "Б-1 ни ключа, ни имени файла ключа в вызовах Bot API" "да|0" "$([ -n "$MENU1" ] && echo да || echo нет)|$LEAK"
case_check "Б-1 меню записано конвертом events.emit: account-buttons.jsonl знает nonce и message_id" да \
    "$(yes_no jq -e --arg n "$NONCE1" --argjson m "${MID1:-0}" 'select(.v == 1 and .event == "account.menu" and .data.nonce == $n and (.data.message_id|tostring) == ($m|tostring))' "$S/log/account-buttons.jsonl")"
MID1="${MID1:-0}"

echo "── Б-2: чужое нажатие не переключает ──"
press 502 666 "$OWNER" "$MID1" "a:$NONCE1:beta"
press 503 "$OWNER" 666 "$MID1" "a:$NONCE1:beta"
press 504 "$OWNER" "$OWNER" "$MID1" "a:$NONCE1:ghost"
OTHER_TAIL=X; [ "${NONCE1: -1}" = X ] && OTHER_TAIL=Y
press 505 "$OWNER" "$OWNER" "$MID1" "a:${NONCE1:0:21}$OTHER_TAIL:beta"
wait_for "$WAIT_S" offset_is 506 >/dev/null
wait_for "$WAIT_S" answers_at_least 4 >/dev/null
sleep 2   # фоновый зов двери успел бы прийти
case_check "Б-2 на каждое из четырёх нажатий бот ответил (answerCallbackQuery)" 4 "$(calls_of answerCallbackQuery | grep -c .)"
FOREIGN_CALLS=$(switch_lines)

echo "── Б-3: нажатие владельца = выбор, один раз на меню ──"
press 506 "$OWNER" "$OWNER" "$MID1" "a:$NONCE1:beta"
wait_for "$WAIT_S" switch_at_least 1 >/dev/null
case_check "Б-3 дверь позвана ровно «--хочу beta»" "--хочу beta" "$(paste -sd'#' "$S/switch-calls" 2>/dev/null)"
# Отказ чужому судится только у бота, который владельцу ДАЁТ выбрать: до кода
# дверь не зовётся вовсе, и «не звана» было бы зелёным даром.
PRESS_LIVE="$([ "$(paste -sd'#' "$S/switch-calls" 2>/dev/null)" = "--хочу beta" ] && echo жива || echo мертва)"
case_check "Б-2 чужой from.id, чужой chat.id, имя не из меню, чужой nonce — дверь не звана" "жива|0" \
    "$PRESS_LIVE|$FOREIGN_CALLS"
case_check "Б-3 сообщение меню исправлено (editMessageText того же message_id)" да \
    "$(calls_of editMessageText | jq -e --arg m "$MID1" 'select(.form.message_id == $m)' >/dev/null 2>&1 && echo да || echo нет)"
press 507 "$OWNER" "$OWNER" "$MID1" "a:$NONCE1:login"
wait_for "$WAIT_S" offset_is 508 >/dev/null
sleep 2
case_check "Б-3 второе нажатие в том же меню дверь не зовёт" "жива|1" "$PRESS_LIVE|$(switch_lines)"

echo "── Б-4: новое меню; нажатие не в нём — отказ, в нём — выбор ──"
say_owner 508 "/account"
wait_for "$WAIT_S" menus_at_least 2 >/dev/null
MENU2="$(menu_n 2)"; MID2="${MENU2%%$'\t'*}"
NONCE2="$(printf '%s' "${MENU2#*$'\t'}" | jq -r '.inline_keyboard[0][0].callback_data' 2>/dev/null | cut -d: -f2)"
case_check "Б-4 «/account» — то же меню, новый nonce" да \
    "$([ -n "$NONCE2" ] && [ "$NONCE2" != null ] && [ "$NONCE2" != "$NONCE1" ] && echo да || echo нет)"
press 509 "$OWNER" "$OWNER" "$MID1" "a:$NONCE2:login"
wait_for "$WAIT_S" offset_is 510 >/dev/null
sleep 2
case_check "Б-4 nonce нового меню в чужом сообщении — дверь не звана" "жива|1" "$PRESS_LIVE|$(switch_lines)"
press 510 "$OWNER" "$OWNER" "${MID2:-0}" "a:$NONCE2:login"
wait_for "$WAIT_S" switch_at_least 2 >/dev/null
case_check "Б-4 нажатие в новом меню — «--хочу login»" "--хочу beta#--хочу login" "$(paste -sd'#' "$S/switch-calls" 2>/dev/null)"

echo "── Б-5: кнопки согласия «Да/Нет» судит прежний судья ──"
before=$(calls_of answerCallbackQuery | grep -c .)
press 511 "$OWNER" "$OWNER" 42 "c:AAAAAAAAAAAAAAAAAAAAAA:y"
wait_for "$WAIT_S" answers_at_least $((before + 1)) >/dev/null
case_check "Б-5 c:<nonce>:y без запроса — ответ судьи согласия «Запрос не найден»" "Запрос не найден" \
    "$(calls_of answerCallbackQuery | tail -n1 | jq -r '.form.text' 2>/dev/null)"
case_check "Б-5 нажатие согласия дверь переключения не зовёт" "жива|2" "$PRESS_LIVE|$(switch_lines)"

echo "── Б-6: срок меню — нажатие старше 10 мин выбора не пишет ──"
# Судья напрямую, со сдвинутыми часами: ждать 10 минут живьём проба не может.
ttl_press() {  # $1 = минут с отправки меню → «ответ|выборов в журнале»
    local d="$T/ttl-$1"; mkdir -p "$d"
    HARNESS_ACCOUNT_DIR="$d" python3 -I - "$HERE/lib" "$d" "$OWNER" "$1" 2>"$d/err" <<'PY'
import datetime as dt, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import account_buttons as ab
log, owner, age = Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
nonce = "B" * 22
ab.append_row(log, {"событие": "меню", "nonce": nonce, "message_id": 5, "имена": ["login"]})
real_now = ab.now()
ab.now = lambda: real_now + dt.timedelta(minutes=age)
verdict = ab.judge(log, owner, {"from": {"id": int(owner)}, "message": {"chat": {"id": int(owner)}, "message_id": 5},
                                "data": f"a:{nonce}:login"})
chosen = sum(1 for r in ab.read_rows(log) if r.get("событие") == "выбор")
print(f"{verdict.get('answer')}|{chosen}")
PY
}
TTL_FRESH="$(ttl_press 9)"
case_check "Б-6 меню 9 мин — нажатие принято, выбор записан" "Принято|1" "$TTL_FRESH"
case_check "Б-6 меню 11 мин — «устарело», выбор не записан" "жива|Меню устарело — напиши «аккаунт» заново|0" \
    "$([ "$TTL_FRESH" = "Принято|1" ] && echo жива || echo мертва)|$(ttl_press 11)"

echo "── Б-7: кнопка старого меню после нового — не выбор ──"
old_menu_press() {  # → «ответ|выборов в журнале»: два меню, нажата кнопка первого в его же сообщении
    local d="$T/old-menu"; mkdir -p "$d"
    HARNESS_ACCOUNT_DIR="$d" python3 -I - "$HERE/lib" "$d" "$OWNER" 2>"$d/err" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import account_buttons as ab
log, owner = Path(sys.argv[2]), sys.argv[3]
first, second = "C" * 22, "D" * 22
ab.append_row(log, {"событие": "меню", "nonce": first, "message_id": 5, "имена": ["login"]})
ab.append_row(log, {"событие": "меню", "nonce": second, "message_id": 6, "имена": ["login"]})
verdict = ab.judge(log, owner, {"from": {"id": int(owner)}, "message": {"chat": {"id": int(owner)}, "message_id": 5},
                                "data": f"a:{first}:login"})
chosen = sum(1 for r in ab.read_rows(log) if r.get("событие") == "выбор")
print(f"{verdict.get('answer')}|{chosen}")
PY
}
case_check "Б-7 кнопка первого меню после второго — «старое меню», выбор не записан" \
    "жива|Это старое меню — напиши «аккаунт» заново|0" \
    "$([ "$TTL_FRESH" = "Принято|1" ] && echo жива || echo мертва)|$(old_menu_press)"

echo "── 1в-С: сторож — ротация смены в лимите, ответ --ждёт, помощники в лимите ──"
# Чистые функции сторожа судятся вырезанным телом (как 1б-С в test_switch_account.sh):
# полный обход сторожа на стенде — 5 тыс. строк и tmux, а решение живёт в этих функциях.
WARDEN_SH="${HARNESS_SESSION_WARDEN:-$HERE/../harness/demons/session-warden.sh}"
[ -f "$WARDEN_SH" ] || WARDEN_SH="$HERE/../demons/session-warden.sh"
wbody() { sed -n "/^$1() {/,/^}/p" "$WARDEN_SH"; }
wfn() {  # $1 = функция, дальше доводы → «слово|код»
    local fn="$1" out rc=0; shift
    out=$(timeout 10 bash -c 'eval "$1"; shift; "$@"' _ "$(wbody "$fn")" "$fn" "$@" 2>/dev/null) || rc=$?
    echo "$out|$rc"
}
word_of() { local r="$1"; echo "${r%|*}"; }
WHY_ACC="аккаунт в лимите, переход на beta: передачу написать нечем"
# Доводы ротация_разрешена: передача, лимит по транскрипту, принуждение, причина, аккаунт в лимите (--в-лимите = 0).
A9_YES="$(wfn ротация_разрешена 0 1 1 "$WHY_ACC" 1)"
case_check "А-9 аккаунт в лимите (5-й довод 1), лимит по транскрипту, принуждение → да без передачи" "да|0|аккаунт" \
    "$(case "$A9_YES" in да:*) echo да ;; *) echo нет ;; esac)|${A9_YES##*|}|$(case "$A9_YES" in *"аккаунт в лимите"*) echo аккаунт ;; *) echo - ;; esac)"
A9_LIVE="$([ "${A9_YES##*|}" = 0 ] && echo жива || echo мертва)"
# Отказы судятся только у сторожа, который ветку аккаунта ЗНАЕТ: до кода «нет» — прежняя ветка лимита, даром.
case_check "А-9 LIMIT_ACTIVE=1 по транскрипту, --в-лимите = 1 (5-й довод 0) → ротации без передачи нет" "жива|1" \
    "$A9_LIVE|$(r=$(wfn ротация_разрешена 0 1 0 "" 0); echo "${r##*|}")"
case_check "А-9 лимит + принуждение с причиной, но аккаунт не в лимите → нет (правило 18.10)" "жива|1" \
    "$A9_LIVE|$(r=$(wfn ротация_разрешена 0 1 1 "владелец велел" 0); echo "${r##*|}")"
case_check "А-9 передача написана → да, и слово — о передаче, не об аккаунте" "да: передача смены написана|0" \
    "$(wfn ротация_разрешена 1 1 1 "$WHY_ACC" 1)"

case_check "А-10 выбор_по_ждёт 0 → ротировать" ротировать "$(word_of "$(wfn выбор_по_ждёт 0)")"
case_check "А-10 выбор_по_ждёт 3 (процесс на цели) → итог" итог "$(word_of "$(wfn выбор_по_ждёт 3)")"
case_check "А-10 выбор_по_ждёт 4 (после ротации судить нечем) → итог, второй ротации нет" итог \
    "$(word_of "$(wfn выбор_по_ждёт 4)")"
W_LIVE="$([ "$(word_of "$(wfn выбор_по_ждёт 0)")" = ротировать ] && echo жива || echo мертва)"
case_check "А-10 выбор_по_ждёт 1, 2, пусто → ничего" "жива|ничего ничего ничего" \
    "$W_LIVE|$(word_of "$(wfn выбор_по_ждёт 1)") $(word_of "$(wfn выбор_по_ждёт 2)") $(word_of "$(wfn выбор_по_ждёт "")")"

case_check "А-14 помощники_для_ротации: идут 2, стоят в лимите 1, аккаунт → 1; идут 1, стоит 1 → 0" "1 0" \
    "$(word_of "$(wfn помощники_для_ротации 2 1 1)") $(word_of "$(wfn помощники_для_ротации 1 1 1)")"
H_LIVE="$([ "$(word_of "$(wfn помощники_для_ротации 1 1 1)")" = 0 ] && echo жива || echo мертва)"
case_check "А-14 без смены аккаунта стоящие в лимите держат все: идут 2, стоит 1 → 2" "жива|2" \
    "$H_LIVE|$(word_of "$(wfn помощники_для_ротации 2 1 0)")"
H_STOOD="$(word_of "$(wfn помощники_для_ротации 1 1 1)")"
case_check "А-14 единственный помощник стоит в лимите (последняя запись 429) → ворота пускают" "можно|0" \
    "$(r=$(wfn rotation_helpers_verdict "${H_STOOD:-x}" 0 5 60 сообщить); echo "${r%%:*}|${r##*|}")"
H_OPEN="$(word_of "$(wfn помощники_для_ротации 1 0 1)")"
case_check "А-14 помощник с открытым tool_use (в лимите не стоит), 5 мин тишины → ждать" "жива|ждать|1" \
    "$H_LIVE|$(r=$(wfn rotation_helpers_verdict "${H_OPEN:-x}" 0 5 60 сообщить); echo "${r%%:*}|${r##*|}")"

# В-1 (ревью кода): при ротации ради смены аккаунта rotate_full слал «Ротация
# выполнена (владелец выбрал аккаунт …)», а затем --итог — свою: две вести на
# один переход. Решение «слать ли весть о ротации» — чистая функция сторожа
# весть_о_ротации_нужна <смена аккаунта 1/0>: 0 — слать, 1 — молчать (скажет --итог).
# Ловится: функция путает доводы (два случая), и весть в rotate_full не стоит под
# ней (третий: без решения весть ушла бы при любой функции).
case_check "В-1 весть_о_ротации_нужна 0 (ротация не ради аккаунта) → 0, весть о ротации шлётся" 0 \
    "$(r=$(wfn весть_о_ротации_нужна 0); echo "${r##*|}")"
case_check "В-1 весть_о_ротации_нужна 1 (смена аккаунта) → 1, сторож молчит, говорит только --итог" 1 \
    "$(r=$(wfn весть_о_ротации_нужна 1); echo "${r##*|}")"
ROT_GATE="$(wbody rotate_full | awk -v needle='tg_send "Ротация выполнена ($REASON)' '
    /весть_о_ротации_нужна/ { g = NR }
    index($0, needle) { print (g && NR - g <= 3) ? "под-решением" : "без-решения"; found = 1; exit }
    END { if (!found) print "вести-нет" }')"
case_check "В-1 rotate_full: весть «Ротация выполнена (\$REASON)» стоит под весть_о_ротации_нужна" под-решением "$ROT_GATE"

# М-в (повторное ревью #13): весть «проверка приёма провалена… Передача, вероятно,
# битая» стояла под весть_о_ротации_нужна и молчала при ЛЮБОЙ смене аккаунта. При
# автопереходе (лимит) передачу написать нечем — провал приёма ожидаем, молчать верно;
# при ручном выборе владельца передача пишется, и её провал — настоящий сигнал.
# Решение — чистая функция сторожа весть_о_битой_передаче_нужна <смена аккаунта 1/0>
# <причина автоперехода ACCOUNT_WHY, пусто — не автопереход>: 0 — слать, 1 — молчать.
# Ловится: глушение при любой смене аккаунта (второй случай), весть при автопереходе
# (третий), весть в rotate_full не под этим решением (четвёртый).
case_check "М-в весть_о_битой_передаче_нужна 0 «» (ротация не ради аккаунта) → 0, весть идёт" 0 \
    "$(r=$(wfn весть_о_битой_передаче_нужна 0 ""); echo "${r##*|}")"
case_check "М-в весть_о_битой_передаче_нужна 1 «» (ручной выбор владельца) → 0, весть о битой передаче идёт" 0 \
    "$(r=$(wfn весть_о_битой_передаче_нужна 1 ""); echo "${r##*|}")"
case_check "М-в весть_о_битой_передаче_нужна 1 «автопереход: …» (лимит) → 1, молчит" 1 \
    "$(r=$(wfn весть_о_битой_передаче_нужна 1 "автопереход: alpha в лимите до 18:00"); echo "${r##*|}")"
HANDOFF_GATE="$(wbody rotate_full | awk -v needle='но проверка приёма провалена' \
    -v gate='весть_о_битой_передаче_нужна "$ACCOUNT_ROTATION" "${ACCOUNT_WHY:-}"' '
    index($0, gate) { g = NR }
    index($0, needle) { print (g && NR - g <= 3) ? "под-решением" : "без-решения"; found = 1; exit }
    END { if (!found) print "вести-нет" }')"
case_check "М-в rotate_full: весть «проверка приёма провалена» стоит под весть_о_битой_передаче_нужна \"\$ACCOUNT_ROTATION\" \"\${ACCOUNT_WHY:-}\"" \
    под-решением "$HANDOFF_GATE"

# Суточная сверка меню бота: $1 = день из отметки (пусто — отметки нет), $2 = сегодня.
# Каждый случай ловит поломку, которую пропустили бы два других: «сверять всегда»
# (остаётся зелёным первый и третий), «не сверять никогда» (второй), «сверять только
# без отметки» (первый и второй).
TODAY=$(date +%F); YESTERDAY=$(date -d yesterday +%F)
case_check "Б-8 сверять_меню_сегодня: отметки нет → 0 (сверять)" 0 \
    "$(r=$(wfn сверять_меню_сегодня "" "$TODAY"); echo "${r##*|}")"
case_check "Б-8 сверять_меню_сегодня: отметка сегодняшняя → 1 (уже сверяли)" 1 \
    "$(r=$(wfn сверять_меню_сегодня "$TODAY" "$TODAY"); echo "${r##*|}")"
case_check "Б-8 сверять_меню_сегодня: отметка вчерашняя → 0 (новые сутки)" 0 \
    "$(r=$(wfn сверять_меню_сегодня "$YESTERDAY" "$TODAY"); echo "${r##*|}")"

# Сколько помощников стоят в лимите — малый стенд: транскрипт смены, его subagents/,
# вывод судьи JUDGE_OUT в формате stall-in-log, НАСТОЯЩИЙ limit-in-log.py.
# aaa — последняя запись 429 (стоит); bbb — открытый tool_use (держит); ccc — 429,
# но он «зависший», не «помощник». Ловится: путь к транскрипту помощника не тот
# (ни один не прочтён → 0), код limit-in-log не судится (3), берётся не тот столбец
# или не тот вид строки (0 или 2), число считается и без смены аккаунта.
HS="$T/helpers-stood"; mkdir -p "$HS/shift/subagents"; : > "$HS/shift.jsonl"
python3 -I - "$HS/shift/subagents" <<'PY'
import json, sys
лимит = {"type": "assistant", "error": "rate_limit", "isApiErrorMessage": True, "apiErrorStatus": 429,
         "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API Error: Rate limit reached"}]}}
ответ = {"type": "assistant", "message": {"model": "proba", "content": [{"type": "text", "text": "делаю"}]}}
вызов = {"type": "assistant", "message": {"model": "proba", "content": [
    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "sleep 600"}}]}}
for имя, записи in {"aaa": [ответ, лимит], "bbb": [ответ, вызов, лимит], "ccc": [ответ, лимит]}.items():
    with open(f"{sys.argv[1]}/agent-{имя}.jsonl", "w", encoding="utf-8") as f:
        f.writelines(json.dumps(з, ensure_ascii=False) + "\n" for з in записи)
PY
HS_JUDGE=$(printf 'работа_мин\t2\nпомощников\t2\nзависших\t1\nпомощник\taaa\t1\t6\tпроба\nпомощник\tbbb\t1\t6\tпроба\nзависший\tccc\t30\t40\tпроба')
stood() {  # $1 = ACCOUNT_ROTATION → «число|код»
    ( export SHIFT_TRANSCRIPT="$HS/shift.jsonl" JUDGE_OUT="$HS_JUDGE" ACCOUNT_ROTATION="$1" HARNESS_SCRIPTS="$HERE"
      wfn helpers_stood_in_limit )
}
HS_YES="$(stood 1)"
case_check "А-14 helpers_stood_in_limit: aaa (429) стоит, bbb (tool_use) держит, ccc зависший не в счёт → ACCOUNT_STOPPED_HELPERS 1" \
    "1|0" "$HS_YES"
case_check "А-14 helpers_stood_in_limit без смены аккаунта (ACCOUNT_ROTATION=0) → 0" "жива|0|0" \
    "$([ "$HS_YES" = "1|0" ] && echo жива || echo мертва)|$(stood 0)"

# В1 (решено 13.11): откат свежей смены без передачи — 15 мин; умолчание в коде сторожа,
# потому что в живом /etc ключей ACCOUNT_* нет (план, расхождение 2).
case_check "В1 умолчание ACCOUNT_ROLLBACK_FRESH_MIN в стороже — 15 во всех местах" "есть|15" \
    "$(D=$(grep -o 'ACCOUNT_ROLLBACK_FRESH_MIN:\?[-=][0-9]*' "$WARDEN_SH" | grep -o '[0-9]*$' | sort -u | paste -sd' ');
       [ -n "$D" ] && echo есть || echo нет)|$(grep -o 'ACCOUNT_ROLLBACK_FRESH_MIN:\?[-=][0-9]*' "$WARDEN_SH" | grep -o '[0-9]*$' | sort -u | paste -sd' ')"

echo "── А-18: меню аккаунтов знает лимиты из state.json ──"
menu_with_limit() {  # $1 = сдвиг сброса beta от сейчас, с → «первая строка|кнопка beta|кнопка alpha|ожидаемый HH:MM»
    local d="$T/menu-limit$1"; mkdir -p "$d"
    cp "$S/acc/profiles.json" "$d/"
    TZ=Asia/Almaty OWNER_TZ=Asia/Almaty HARNESS_CONF="$S/harness.conf" HARNESS_ACCOUNT_DIR="$d" \
        timeout 20 python3 -I - "$HERE/lib" "$d" "$1" 2>"$d/err" <<'PY'
import datetime as dt, json, sys, time
from zoneinfo import ZoneInfo
sys.path.insert(0, sys.argv[1])
d, shift = sys.argv[2], int(sys.argv[3])
reset = int(time.time()) + shift
json.dump({"запуск": "alpha", "идёт": "alpha", "переключения": [],
           "лимиты": {"beta": {"сброс": reset, "окно": "five_hour", "когда": dt.datetime.now().astimezone().isoformat()}}},
          open(d + "/state.json", "w", encoding="utf-8"), ensure_ascii=False)
import account_buttons as ab
m = ab.menu()
labels = {b["callback_data"].rsplit(":", 1)[1]: b["text"]
          for row in json.loads(m["reply_markup"])["inline_keyboard"] for b in row}
hhmm = dt.datetime.fromtimestamp(reset, ZoneInfo("Asia/Almaty")).strftime("%H:%M")
print(f"{m['text'].splitlines()[0]}|{labels.get('beta')}|{labels.get('alpha')}|{hhmm}")
PY
}
ML="$(menu_with_limit 10800)"; IFS='|' read -r ML_FIRST ML_BETA ML_ALPHA ML_HHMM <<< "$ML"
case_check "А-18 меню: beta в лимите со сбросом через 3 ч → «Бета — в лимите до HH:MM, нажатие откажет»" \
    "Бета — в лимите до ${ML_HHMM:-?}, нажатие откажет" "$ML_BETA"
case_check "А-18 меню: первая строка текста «Автопереход при лимите: включён»" "Автопереход при лимите: включён" "$ML_FIRST"
MENU_LIVE="$([ "$ML_BETA" = "Бета — в лимите до ${ML_HHMM:-?}, нажатие откажет" ] && echo жива || echo мертва)"
MP="$(menu_with_limit -60)"; IFS='|' read -r _ MP_BETA MP_ALPHA _ <<< "$MP"
case_check "А-18 меню: сброс прошёл минуту назад → пометки нет, идущий — «сейчас»" "жива|Бета|Альфа — сейчас" \
    "$MENU_LIVE|$MP_BETA|$MP_ALPHA"

# М-а (повторное ревью #13): ZoneInfo на имени-пути («../etc», «Europe/../x») бросает
# ValueError, а не ZoneInfoNotFoundError: битый OWNER_TZ в настройках ронял меню
# аккаунтов и вести двери с часом сброса. Битый пояс — как неизвестный: час машины.
# Ловится: час_владельца ловит только ZoneInfoNotFoundError (ValueError летит наружу).
owner_hour() {  # $1 = OWNER_TZ в harness.conf → «машины» | «другой» | «ОШИБКА <тип>»
    # Значение — в файле настроек: окружение старше файла только для ключей, названных в нём.
    printf 'OWNER_TZ="%s"\n' "$1" > "$T/owner-tz.conf"
    env -u OWNER_TZ TZ=Asia/Almaty HARNESS_CONF="$T/owner-tz.conf" \
        timeout 20 python3 -I - "$HERE/lib" 2>/dev/null <<'PY' || echo "ОШИБКА выход"
import datetime as dt, sys, time
sys.path.insert(0, sys.argv[1])
import config
u = int(time.time()) + 3 * 3600
try:
    час = config.час_владельца(u)
except Exception as e:
    print(f"ОШИБКА {type(e).__name__}")
else:
    print("машины" if час == dt.datetime.fromtimestamp(u).strftime("%H:%M") else "другой")
PY
}
OH_LIVE="$([ "$(owner_hour UTC)" = другой ] && echo жива || echo мертва)"
case_check "М-а час_владельца: OWNER_TZ «../etc» и «Europe/../x» (ValueError у ZoneInfo) → не падает, час пояса машины" \
    "жива|машины машины" "$OH_LIVE|$(owner_hour ../etc) $(owner_hour Europe/../x)"

echo "── Б-8: меню бота сверяется при старте диспетчера фоном ──"
cmds_count() { calls_of "$1" | grep -c .; }
set_at_least() { [ "$(cmds_count setMyCommands)" -ge "$1" ]; }
get_at_least() { [ "$(cmds_count getMyCommands)" -ge "$1" ]; }
# getMyCommands стенда отвечает через 15 с; --поставить спрашивает ещё раз — ждём с запасом.
wait_for 50 set_at_least 1 >/dev/null
sleep 3   # второй setMyCommands, будь он, успел бы прийти
case_check "Б-8 меню без account → ровно один setMyCommands, и в нём account" "1|да" \
    "$(cmds_count setMyCommands)|$(calls_of setMyCommands | head -n1 | jq -e '[.form.commands[].command] | index("account") != null' >/dev/null 2>&1 && echo да || echo нет)"
FIRST_POLL="$(cat "$S/api-first-getupdates" 2>/dev/null)"
FIRST_ANSWER="$(calls_of getMyCommands | head -n1 | jq -r '.t_out' 2>/dev/null)"
case_check "Б-8 getMyCommands отвечает через 15 с → первый getUpdates ушёл раньше ответа сверки" "сверка была|раньше" \
    "$([ -n "$FIRST_ANSWER" ] && echo "сверка была" || echo "сверки нет")|$(python3 -c 'import sys; print("раньше" if float(sys.argv[1]) < float(sys.argv[2]) else "позже")' "${FIRST_POLL:-9e99}" "${FIRST_ANSWER:-0}" 2>/dev/null)"
case_check "Б-8 вывод --хочу диспетчер пишет в account-switch.out, account-switch.log нет" "да|нет" \
    "$(grep -qF -- 'дверь-стенда: --хочу beta' "$S/log/account-switch.out" 2>/dev/null && echo да || echo нет)|$([ -e "$S/log/account-switch.log" ] && echo да || echo нет)"
# Второй старт: меню в Telegram совпадает с данными, ответ сразу.
SET_BEFORE=$(cmds_count setMyCommands); GET_BEFORE=$(cmds_count getMyCommands)
kill -KILL -- "-$DISP_PID" 2>/dev/null; kill -KILL "$DISP_PID" 2>/dev/null
wait_for 10 bash -c '! kill -0 "$1" 2>/dev/null' _ "$DISP_PID" >/dev/null
cp "$S/commands-full.json" "$S/mycommands.json"; printf '0' > "$S/mycommands.delay"
start_dispatcher
wait_for "$WAIT_S" get_at_least $((GET_BEFORE + 1)) >/dev/null
sleep 3
case_check "Б-8 меню совпадает с данными → сверка спросила, setMyCommands нет" "жива|0" \
    "$([ "$(cmds_count getMyCommands)" -gt "$GET_BEFORE" ] && echo жива || echo мертва)|$(( $(cmds_count setMyCommands) - SET_BEFORE ))"

if [ "$CASES_FAILED" != 0 ]; then
    echo "    --- вызовы Bot API (хвост) ---"; tail -6 "$S/api-calls.jsonl" | cut -c1-220 | sed 's/^/    /'
    echo "    --- журнал канала (хвост) ---"; tail -6 "$S/log/tg-dispatcher.log" 2>/dev/null | cut -c1-200 | sed 's/^/    /'
fi
printf '\nитог test_switch_account_bot: случаев %s, неудач %s\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" = 0 ]
