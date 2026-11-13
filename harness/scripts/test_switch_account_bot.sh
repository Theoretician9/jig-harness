#!/usr/bin/env bash
# Проба этапа 1б hr.switch-account: смена аккаунта из Telegram
# (спека .tasks/hr.switch-account/spec.md, раздел «Этап 1б»).
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

# Подставная дверь: строка доводов на вызов, ничего больше.
cat > "$S/project/scripts/account-switch.py" <<'EOF'
import os, sys
with open(os.path.join(os.environ["PROBA_DIR"], "switch-calls"), "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
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
cat > "$T/api.py" <<'PYEOF'
import json, sys, time, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

port, updates, calls, ready = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
NEXT = [100]


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
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}
        method = self.path.rsplit("/", 1)[-1]
        if method == "getUpdates":
            found = pending(int(form.get("offset") or "0"))
            if not found:
                time.sleep(0.5)
            self.reply({"ok": True, "result": found})
            return
        result = True
        if method == "sendMessage":
            NEXT[0] += 1
            result = {"message_id": NEXT[0], "chat": {"id": int(form.get("chat_id") or 0)}}
            form["_message_id"] = NEXT[0]
        with open(calls, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"method": method, "form": form}, ensure_ascii=False) + "\n")
        self.reply({"ok": True, "result": result})

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
printf '000:СТЕНДОВЫЙ' > "$S/token"
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
env PROBA_DIR="$S" PATH="$T/bin:$PATH" HARNESS_PANEL_STATE="$S/panel" \
    HARNESS_INSTALL_CONF="$S/install.conf" HARNESS_CONF="$S/harness.conf" \
    HARNESS_ACCOUNT_DIR="$S/acc" SECRETS_DIR="$S/secrets" LOG_DIR="$S/log" \
    TG_API_BASE="http://127.0.0.1:$port" \
    setsid nohup bash "$DISPATCHER" > "$S/dispatcher.out" 2>&1 < /dev/null &
DISP_PID=$!
disown "$DISP_PID"

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

if [ "$CASES_FAILED" != 0 ]; then
    echo "    --- вызовы Bot API (хвост) ---"; tail -6 "$S/api-calls.jsonl" | cut -c1-220 | sed 's/^/    /'
    echo "    --- журнал канала (хвост) ---"; tail -6 "$S/log/tg-dispatcher.log" 2>/dev/null | cut -c1-200 | sed 's/^/    /'
fi
printf '\nитог test_switch_account_bot: случаев %s, неудач %s\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" = 0 ]
