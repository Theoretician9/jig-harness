#!/usr/bin/env bash
#
# test_dispatcher_stall_watch.sh — проба сторожа harness/demons/dispatcher-stall-watch.sh.
#
# Настоящий сторож; подставлены и названы: Bot API (свой HTTP-сервер с
# pending_update_count), emergency-send и systemctl (файлы стенда через ключи
# DISPATCH_STALL_EMERGENCY / DISPATCH_STALL_SYSTEMCTL), паспорт и пороги (свои
# файлы HARNESS_INSTALL_CONF / HARNESS_CONF с LOG_DIR стенда). Боевой юнит и
# боевой канал не задеваются.
#
# Третья работа — страж ответа владельцу (случаи О1–О16, спека
# .tasks/hr.alert-self-sensors-attention/spec.md §8.2): заглушка tmux и обёртка
# python3 в своём PATH, свой HARNESS_PANEL_STATE, подставной tg_send.sh в
# PROJECT_DIR стенда, «сейчас» — дверь OWNER_REPLY_NOW. Сторож, прибор и pane.sh
# берутся рядом с пробой: мутации идут «из-копии».
#
#   bash scripts/test_dispatcher_stall_watch.sh [--сторож <путь>]
set -uo pipefail
export LC_ALL=C.UTF-8
KOREN="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
WATCH="$KOREN/harness/demons/dispatcher-stall-watch.sh"
[ "${1:-}" = "--сторож" ] && WATCH="$2"
[ -r "$WATCH" ] || { echo "ПРОБА: нет сторожа $WATCH"; exit 1; }
source "$KOREN/scripts/lib/selftest_cases.sh"

T=$(mktemp -d /tmp/proba-stall-XXXXXX)
API_PID=""
trap '[ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null; rm -rf "$T"' EXIT

mkdir -p "$T/log" "$T/project"
printf '000:СТЕНДОВЫЙ' > "$T/token"
cat > "$T/api.py" <<'PYEOF'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port, pending_file, ready = int(sys.argv[1]), sys.argv[2], sys.argv[3]
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        n = int(open(pending_file).read().strip() or 0)
        raw = json.dumps({"ok": True, "result": {"url": "", "pending_update_count": n}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers()
        self.wfile.write(raw)
    do_POST = do_GET
    def log_message(self, *_): pass
s = HTTPServer(("127.0.0.1", port), H); open(ready, "w").close(); s.serve_forever()
PYEOF
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
echo 0 > "$T/pending"
python3 "$T/api.py" "$PORT" "$T/pending" "$T/ready" > "$T/api.out" 2>&1 &
API_PID=$!
for _ in $(seq 40); do [ -e "$T/ready" ] && break; sleep 0.25; done
[ -e "$T/ready" ] || { echo "ПРОБА: Bot API стенда не поднялся"; cat "$T/api.out"; exit 1; }

printf '#!/bin/sh\necho "$1 | $2" >> "%s/alarms"\n' "$T" > "$T/emergency"
printf '#!/bin/sh\ncase "$1" in\n is-active) exit 0 ;;\n show) echo 0 ;;\n restart) echo "$2" >> "%s/restarts" ;;\nesac\n' "$T" > "$T/systemctl"
chmod +x "$T/emergency" "$T/systemctl"
cat > "$T/install.conf" <<CONF
PROJECT_DIR="$T/project"
PROJECT_NAME="стенд"
LOG_DIR="$T/log"
TG_CHAT_ID="777000"
TG_TOKEN_FILE="$T/token"
AGENT_USER="$(id -un)"
CONF

run_watch() {  # $1=ENABLED $2=HEAL [$3=ACK_CHECK_ENABLED] («-»/пусто — ключа в конфиге нет)
    cat > "$T/harness.conf" <<CONF
SERVICE_LOCKS_SUBDIR="services"
$([ "$1" = - ] || echo "DISPATCH_STALL_ENABLED=\"$1\"")
$([ "$2" = - ] || echo "DISPATCH_STALL_HEAL=\"$2\"")
$([ -z "${3:-}" ] || echo "DISPATCH_ACK_CHECK_ENABLED=\"$3\"")
DISPATCH_STALL_HEAL_WAIT="0"
DISPATCH_STALL_EMERGENCY="$T/emergency"
DISPATCH_STALL_SYSTEMCTL="$T/systemctl"
CONF
    HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
    TG_API_BASE="http://127.0.0.1:$PORT" bash "$WATCH" >/dev/null 2>&1
}
stand() {  # $1=возраст признака, с; $2=ждущих сообщений
    rm -f "$T/alarms" "$T/restarts" "$T/log/dispatcher-stall.told"
    echo $(( $(date +%s) - $1 )) > "$T/log/tg-dispatcher.alive"
    echo "$2" > "$T/pending"
}
count() { [ -e "$1" ] && wc -l < "$1" || echo 0; }

stand 600 3; run_watch 1 0
case_check "признак старый + в канале есть новое — одна тревога" 1 "$(count "$T/alarms")"
grep -q "приём канала встал" "$T/alarms" 2>/dev/null; case_check "тревога называет обрыв" 0 $?
run_watch 1 0
case_check "повтор в окне — тишина" 1 "$(count "$T/alarms")"
stand 600 0; run_watch 1 0
case_check "признак старый, в канале пусто — тишина" 0 "$(count "$T/alarms")"
stand 10 3; run_watch 1 0
case_check "признак свежий, в канале есть новое — тишина" 0 "$(count "$T/alarms")"
stand 600 3; run_watch 0 0
case_check "сторож выключен ключом — тишина" 0 "$(count "$T/alarms")"
stand 600 3; run_watch 1 0
case_check "DISPATCH_STALL_HEAL=0 — юнит не перезапускается" 0 "$(count "$T/restarts")"
# Умолчания ключей судятся БЕЗ ключа: случай, задающий ключ сам, слеп к
# умолчанию в коде. Сторож и самолечение включает смена, не раскладчик.
stand 600 3; run_watch - 0
case_check "ключа DISPATCH_STALL_ENABLED нет — сторож выключен" 0 "$(count "$T/alarms")"
stand 600 3; run_watch 1 -
case_check "ключа DISPATCH_STALL_HEAL нет — юнит не перезапускается" 0 "$(count "$T/restarts")"
stand 600 3; run_watch 1 1
case_check "DISPATCH_STALL_HEAL=1 и признак не ожил — рестарт юнита (условие пробы)" 1 "$(count "$T/restarts")"

# Сверка квитанций «принял»: настоящий check-ack-receipts.py рядом со сторожем.
# Круг свежий и ключа круга нет — тревога может прийти только от сверки.
ack_stand() {  # $1=ok|- — есть ли удачная квитанция на сообщение 90-секундной давности
    local now msg_id
    now=$(date +%s); msg_id="$(( now - 90 )).000000001"
    rm -rf "$T/alarms" "$T/log/dispatcher-ack.told" "$T/log/inbox" "$T/log/доставка.jsonl"
    stand 10 0
    mkdir -p "$T/log/inbox/обработано"
    echo "стенд" > "$T/log/inbox/обработано/$msg_id.txt"
    jq -cn --arg s "$(( now - 7200 ))" \
        '{event:"delivery.ack_journal_on", outcome:"ok", data:{started_at:$s}}' >> "$T/log/доставка.jsonl"
    [ "$1" = ok ] && jq -cn --arg ts "$msg_id" --arg s "$(( now - 89 ))" \
        '{event:"delivery.ack", outcome:"ok", data:{ts:$ts, sent_at:$s}}' >> "$T/log/доставка.jsonl"
    return 0
}
ack_stand -; run_watch - 0
case_check "сообщение 90 с без квитанции, ключа круга нет — одна тревога" 1 "$(count "$T/alarms")"
grep -q "принял" "$T/alarms" 2>/dev/null; case_check "тревога называет «принял»" 0 $?
run_watch - 0
case_check "квитанции нет, повтор в окне — тишина" 1 "$(count "$T/alarms")"
ack_stand ok; run_watch - 0
case_check "квитанция есть — тишина" 0 "$(count "$T/alarms")"
ack_stand -; run_watch - 0 0
case_check "DISPATCH_ACK_CHECK_ENABLED=0 — тишина" 0 "$(count "$T/alarms")"

# ═══ Страж ответа владельцу (третья работа, спека hr.alert-self-sensors-attention §5, §8.2) ═══
# Свой LOG_DIR, свой HARNESS_PANEL_STATE, заглушка tmux и обёртка python3 первыми в
# своём PATH, подставной tg_send.sh в PROJECT_DIR стенда. «Сейчас» — дверь
# OWNER_REPLY_NOW (эпоха); все моменты стенда отсчитаны от неё, а она — от
# настоящих часов: судья ротации меряет по ним.
OR="$T/or"
REAL_PY=$(command -v python3)
mkdir -p "$T/bin" "$T/projects"
cat > "$T/bin/tmux" <<'TMUXEOF'
#!/usr/bin/env bash
# Подделка tmux стенда: пишет каждый вызов, отвечает из файлов стенда.
st="${PROBA_OR_DIR:?подделка tmux позвана вне стенда}"
printf '%s\n' "$*" >> "$st/tmux-calls"
case "$1" in
    has-session)
        [ ! -e "$st/tmux-hangs" ] || exec sleep 120
        [ "$(cat "$st/tmux-session")" = есть ] ;;
    display-message)
        [ "$(cat "$st/tmux-answers")" = да ] || exit 1
        case "$*" in
            *pane_dead*) cat "$st/pane-dead" ;;
            *pane_current_command*) cat "$st/pane-cmd" ;;
            *) exit 1 ;;
        esac ;;
    *) exit 0 ;;
esac
TMUXEOF
cat > "$T/bin/python3" <<PYWRAP
#!/usr/bin/env bash
# Обёртка стенда: считает вызовы прибора --просрочено, по файлу стенда вешает его,
# остальное отдаёт настоящему python3.
case "\$*" in *delivery-latency.py*--просрочено*)
    echo "\$*" >> "\${PROBA_OR_DIR:?}/cli-calls"
    [ ! -e "\$PROBA_OR_DIR/instrument-hangs" ] || exec sleep 120 ;;
esac
exec "$REAL_PY" "\$@"
PYWRAP
chmod +x "$T/bin/tmux" "$T/bin/python3"

or_stand() {  # свежий стенд стража; OR_NOW — «сейчас» случая
    rm -rf "$OR"
    mkdir -p "$OR/log/inbox/обработано" "$OR/panel" "$OR/project/scripts"
    OR_NOW=$(date +%s)
    : > "$OR/log/tg_out.jsonl"
    : > "$OR/harness.conf"
    echo есть > "$OR/tmux-session"; echo да > "$OR/tmux-answers"
    echo 0 > "$OR/pane-dead"; echo claude > "$OR/pane-cmd"
    echo 0 > "$OR/send-rc"
    cat > "$OR/project/scripts/tg_send.sh" <<SENDEOF
#!/usr/bin/env bash
# Подставная дверь канала: доводы одной строкой на вызов, код — из файла стенда.
printf '%s ' "\$@" >> "$OR/sent"; printf '\n' >> "$OR/sent"
exit "\$(cat "$OR/send-rc")"
SENDEOF
    chmod +x "$OR/project/scripts/tg_send.sh"
    cat > "$OR/install.conf" <<CONF
PROJECT_DIR="$OR/project"
PROJECT_NAME="стенд"
LOG_DIR="$OR/log"
TG_CHAT_ID="777000"
TG_TOKEN_FILE="$T/token"
AGENT_USER="$(id -un)"
PANEL_STATE_DIR="$OR/panel"
CONF
}
# Внешний предел — чтобы случай про висящий вызов краснел, а не вешал пробу: сторож
# обязан уложиться сам, его собственные пределы короче.
run_or() {  # [ENABLED] — прогон стража; «сейчас» = OR_AT (иначе OR_NOW), круг — OR_STALL, tmux — OR_TMUX_LIMIT
    cat > "$OR/harness.conf" <<CONF
SERVICE_LOCKS_SUBDIR="services"
TMUX_SESSION="proba-or-$$"
DISPATCH_STALL_ENABLED="${OR_STALL:-0}"
DISPATCH_ACK_CHECK_ENABLED="0"
DISPATCH_STALL_EMERGENCY="$T/emergency"
DISPATCH_STALL_SYSTEMCTL="$T/systemctl"
$([ -z "${OR_TMUX_LIMIT:-}" ] || echo "PANE_TMUX_TIMEOUT=\"$OR_TMUX_LIMIT\"")
OWNER_REPLY_WATCH_ENABLED="${1:-1}"
OWNER_REPLY_MAX_MIN="5"
CHANNEL_REPLY_MAX_AGE_MIN="120"
CONF
    env -u TMUX -u HARNESS_LOG_DIR -u LOG_DIR PATH="$T/bin:$PATH" PROBA_OR_DIR="$OR" \
        HARNESS_INSTALL_CONF="$OR/install.conf" HARNESS_CONF="$OR/harness.conf" \
        HARNESS_PANEL_STATE="$OR/panel" CLAUDE_PROJECTS_DIR="$T/projects" \
        OWNER_REPLY_NOW="${OR_AT:-$OR_NOW}" TG_API_BASE="http://127.0.0.1:$PORT" \
        timeout -k 2 45 bash "$WATCH" >> "$OR/watch.out" 2>&1 < /dev/null
}
msg() {  # $1 = inbox|done, $2 = возраст, с (от OR_NOW; минус — позже), $3 = текст → имя файла
    local dir="$OR/log/inbox" at=$(( OR_NOW - $2 ))
    [ "$1" = done ] && dir="$dir/обработано"
    printf '%s\n' "${3:-вопрос стенда}" > "$dir/$at.000000000.txt"
    touch -d "@$at" "$dir/$at.000000000.txt"
    echo "$at.000000000.txt"
}
reply() {  # $1 = iz_sessii (true|false), $2 = сдвиг от OR_NOW, с
    jq -cn --arg ts "$(date -d "@$(( OR_NOW + $2 ))" --iso-8601=seconds)" --argjson s "$1" \
        '{ts:$ts, iz_sessii:$s, text:"ответ по сути: стенд проверил и вот итог"}' >> "$OR/log/tg_out.jsonl"
}
door() {  # $1 = сдвиг момента метки «ответ не нужен» от OR_NOW, с
    echo "стенд: владелец написал «ок»" > "$OR/log/reply-not-needed"
    touch -d "@$(( OR_NOW + $1 ))" "$OR/log/reply-not-needed"
}
or_env() {  # судьи паузы и лимита ставят метки только в каталоги стенда
    env HARNESS_INSTALL_CONF="$OR/install.conf" HARNESS_CONF="$OR/harness.conf" \
        HARNESS_PANEL_STATE="$OR/panel" LOG_DIR="$OR/log" "$@"
}
sent() { count "$OR/sent"; }
sent_has() { grep -qF -- "$1" "$OR/sent" 2>/dev/null && echo да || echo нет; }
decision() { [ -e "$OR/log/owner-reply.episode" ] && cut -f2 "$OR/log/owner-reply.episode" | head -1 || echo нет-метки; }
tmux_calls() { local n; n=$(grep -cE -- "$1" "$OR/tmux-calls" 2>/dev/null); echo "${n:-0}"; }
said_why() {  # при красном — что сказал страж, а не догадка
    tail -3 "$OR/watch.out" "$OR/log/dispatcher-stall-watch.log" 2>/dev/null | sed 's/^/      страж: /'
}

echo "── страж ответа владельцу ──"
or_stand; NAME=$(msg done 360); run_or
case_check "О1: обработано/ 6 мин, ответа нет, панель claude — одна весть" 1 "$(sent)"
case_check "О1: весть говорит «взяла его»" "да" "$( [ "$(sent_has 'взяла его')" = да ] && [ "$(sent_has 'ещё не взяла')" = нет ] && echo да || echo нет)"
case_check "О1: весть идёт через дверь с --не-досылать" "да" "$(sent_has '--не-досылать')"
case_check "О1: метка эпизода называет сообщение и решение told" "$NAME	told" "$(cut -f1,2 "$OR/log/owner-reply.episode" 2>/dev/null)"
case_check "О1: в панель ни одного send-keys" 0 "$(tmux_calls '^send-keys')"
[ "$(sent)" = 1 ] || said_why
run_or
case_check "О1: второй прогон того же эпизода — ни вести, ни вызова двери" 1 "$(sent)"

or_stand; msg done 360 >/dev/null; reply true -60; run_or
case_check "О2: ответ смены позже сообщения — тишина" 0 "$(sent)"
case_check "О2: ответ есть — метки эпизода нет" нет-метки "$(decision)"
or_stand; msg done 360 >/dev/null; reply true 60; run_or
case_check "О2: ответ позже «сейчас» не виден — весть" 1 "$(sent)"

or_stand; msg done 360 >/dev/null; reply false -60; run_or
case_check "О3: весть демона (iz_sessii=false) не ответ — весть" 1 "$(sent)"

or_stand; msg done 360 >/dev/null; door -60; run_or
case_check "О4: дверь «ответ не нужен» новее сообщения — тишина" 0 "$(sent)"
or_stand; msg done 360 >/dev/null; door 60; run_or
case_check "О4: дверь с моментом позже «сейчас» не закрывает — весть" 1 "$(sent)"
or_stand; msg done 2400 >/dev/null; door -1860; run_or
case_check "О4: дверь старше «сейчас» на 31 мин закрывает навсегда — тишина" 0 "$(sent)"
case_check "О4: дверь старше 31 мин — метки эпизода нет" нет-метки "$(decision)"

or_stand; msg done 360 >/dev/null
or_env python3 "$KOREN/scripts/pause.py" --поставить "проба: владелец остановил" --кто владелец >/dev/null 2>&1
or_env python3 "$KOREN/scripts/pause.py" --идёт-ли >/dev/null 2>&1 \
    || { echo "ПРОБА: pause.py не поставил паузу на стенде — проверять нечего"; exit 1; }
run_or
case_check "О5: пауза владельца — ни вести" 0 "$(sent)"
case_check "О5: пауза — решение silent:пауза" "silent:пауза" "$(decision)"
or_stand; msg done 360 >/dev/null
or_env python3 "$KOREN/scripts/limit_mark.py" --поставить limit >/dev/null 2>&1
or_env python3 "$KOREN/scripts/limit_mark.py" --лимит-держится >/dev/null 2>&1 \
    || { echo "ПРОБА: limit_mark.py не поставил метку лимита — проверять нечего"; exit 1; }
run_or
case_check "О5: лимит — подставная дверь не звана" 0 "$(sent)"
case_check "О5: лимит — решение silent:лимит" "silent:лимит" "$(decision)"
or_stand; msg done 360 >/dev/null
jq -cn --arg ts "$(date -d "@$(( $(date +%s) - 120 ))" --iso-8601=seconds)" '{event:"rotate_command", ts:$ts}' > "$OR/log/rotation.jsonl"
run_or
case_check "О5: rotate_command 2 мин назад — ни вести" 0 "$(sent)"
case_check "О5: ротация — решение silent:ротация" "silent:ротация" "$(decision)"

for kind in сессии-нет bash мертва; do
    or_stand; msg done 360 >/dev/null
    case "$kind" in
        сессии-нет) echo нет > "$OR/tmux-session" ;;
        bash) echo bash > "$OR/pane-cmd" ;;
        мертва) echo 1 > "$OR/pane-dead" ;;
    esac
    run_or
    case_check "О6 ($kind): агента нет — ни вести" 0 "$(sent)"
    case_check "О6 ($kind): решение отложено — метки эпизода нет" нет-метки "$(decision)"
    case_check "О6 ($kind): строка в журнале стража" "да" "$([ "$(count "$OR/log/dispatcher-stall-watch.log")" -ge 1 ] && echo да || echo нет)"
    case_check "О6 ($kind): страж не поднимал сессию и не писал в панель" 0 "$(tmux_calls '^(new-session|send-keys)')"
done

or_stand; msg inbox 360 >/dev/null; run_or
case_check "О7: файл во входящих — одна весть" 1 "$(sent)"
case_check "О7: весть говорит «ещё не взяла его»" "да" "$(sent_has 'ещё не взяла его')"

or_stand; msg done 299 >/dev/null; run_or
case_check "О8: возраст 299 с — тишина" 0 "$(sent)"
or_stand; msg done 300 >/dev/null; run_or
case_check "О8: возраст ровно 300 с — весть" 1 "$(sent)"

or_stand; msg done 360 >/dev/null
for i in $(seq 0 29); do
    if [ $(( i / 5 % 2 )) = 1 ]; then
        [ -e "$OR/log/лимит-подписки.состояние" ] \
            || or_env python3 "$KOREN/scripts/limit_mark.py" --поставить limit >/dev/null 2>&1
    else
        rm -f "$OR/log/лимит-подписки.состояние"
    fi
    run_or
done
# Первый прогон — без лимита, поэтому весть ровно одна (спека: «≤ 1» внутри эпизода).
case_check "О9: лимит мигает 6 раз за 30 прогонов внутри эпизода — одна весть" 1 "$(sent)"

or_stand; msg done 360 >/dev/null; echo 3 > "$OR/send-rc"; run_or
case_check "О10: дверь придержала (код 3) — решение silent:лимит" "silent:лимит" "$(decision)"
run_or
case_check "О10: после кода 3 следующий прогон дверь не зовёт" 1 "$(sent)"
or_stand; msg done 360 >/dev/null; echo 1 > "$OR/send-rc"; run_or
case_check "О10: дверь отказала (код 1) — метки эпизода нет" нет-метки "$(decision)"
run_or
case_check "О10: после кода 1 следующий прогон — ровно один новый вызов" 2 "$(sent)"

or_stand; msg done 1000 >/dev/null; run_or
case_check "О11: первый эпизод — весть" 1 "$(sent)"
reply true 10
OR_AT=$(( OR_NOW + 60 )) run_or
case_check "О11: ответ появился — метка эпизода снята" нет-метки "$(decision)"
case_check "О11: конец эпизода записан owner_reply.closed" "да" \
    "$(grep -q '"owner_reply.closed"' "$OR/log/ответы-в-канал.jsonl" 2>/dev/null && echo да || echo нет)"
msg done -100 >/dev/null
OR_AT=$(( OR_NOW + 500 )) run_or
case_check "О11: новое просроченное сообщение — новый эпизод, одна весть" 2 "$(sent)"

or_stand; msg done 360 >/dev/null; rm -f "$OR/log/tg_out.jsonl"; run_or
case_check "О12: нет tg_out.jsonl — тишина" 0 "$(sent)"
case_check "О12: нет журнала — одна строка журнала стража" 1 "$(count "$OR/log/dispatcher-stall-watch.log")"

or_stand; msg done 360 >/dev/null; run_or 0
case_check "О13: OWNER_REPLY_WATCH_ENABLED=0 — прибор не зван" 0 "$(count "$OR/cli-calls")"
case_check "О13: OWNER_REPLY_WATCH_ENABLED=0 — ни вести" 0 "$(sent)"

or_stand; msg done 360 >/dev/null; echo node > "$OR/pane-cmd"; run_or
case_check "О14: панель node — агент на месте, весть" 1 "$(sent)"
or_stand; msg done 360 >/dev/null; echo нет > "$OR/tmux-answers"; run_or
case_check "О14: tmux не ответил («?») — об агенте не судим, весть" 1 "$(sent)"
PANE_LIB="$KOREN/scripts/lib/pane.sh"
for cmd_kind in claude:0 node:0 bash:1; do
    case_check "О14: pane_is_agent ${cmd_kind%%:*} — код ${cmd_kind##*:}" "${cmd_kind##*:}" \
        "$(bash -c 'source "$1" >/dev/null 2>&1; pane_is_agent "$2"; echo $?' _ "$PANE_LIB" "${cmd_kind%%:*}" 2>/dev/null | tail -1)"
done

for told in новее старше нет; do
    or_stand; msg done 900 >/dev/null; echo нет > "$OR/tmux-session"; run_or
    case_check "О15 ($told): прогон 1, сессии нет — отложено" нет-метки "$(decision)"
    echo есть > "$OR/tmux-session"
    case "$told" in
        новее) touch -d "@$(( OR_NOW - 300 ))" "$OR/log/agent-pane.told" ;;
        старше) touch -d "@$(( OR_NOW - 1200 ))" "$OR/log/agent-pane.told" ;;
    esac
    run_or
    if [ "$told" = новее ]; then
        case_check "О15: сторож сказал после сообщения — silent:агента-нет" "silent:агента-нет" "$(decision)"
        case_check "О15: сторож сказал после сообщения — вести нет" 0 "$(sent)"
    else
        case_check "О15 (метка $told): сторож промолчал — одна весть told" "1 told" "$(sent) $(decision)"
    fi
done

# Все случаи выше держат круг выключенным, поэтому порча на пути стража, обрывающая
# запуск, им не видна. Здесь в одном запуске обе беды: признак круга устарел И есть
# просрочка; предел проверяется настоящим висящим вызовом.
stale_loop() {
    echo $(( $(date +%s) - 600 )) > "$OR/log/tg-dispatcher.alive"
    echo 3 > "$T/pending"
    rm -f "$T/alarms"
}
or_stand; msg done 360 >/dev/null; stale_loop
OR_STALL=1 run_or
case_check "О16: круг + просрочка за один запуск — весть стража" 1 "$(sent)"
case_check "О16: круг + просрочка за один запуск — тревога круга" 1 "$(count "$T/alarms")"

or_stand; msg done 360 >/dev/null; stale_loop; touch "$OR/instrument-hangs"
OR_STALL=1 run_or
case_check "О16: прибор висит — сторож уложился сам" 0 "$?"
case_check "О16: прибор висит — вести нет" 0 "$(sent)"
case_check "О16: прибор висит — тревога круга дошла" 1 "$(count "$T/alarms")"

or_stand; msg done 360 >/dev/null; stale_loop; touch "$OR/tmux-hangs"
OR_STALL=1 OR_TMUX_LIMIT=1 run_or
case_check "О16: has-session висит — сторож уложился сам" 0 "$?"
case_check "О16: has-session висит — об агенте не судим, весть" 1 "$(sent)"
case_check "О16: has-session висит — тревога круга дошла" 1 "$(count "$T/alarms")"

[ "$CASES_FAILED" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: $CASES_PATHS путей"; exit 0; }
echo "ПРОБА КРАСНАЯ: $CASES_FAILED из $CASES_PATHS"; exit 1
