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

run_watch() {  # $1=ENABLED $2=HEAL («-» — ключа в конфиге нет); остальное — стендовое
    cat > "$T/harness.conf" <<CONF
SERVICE_LOCKS_SUBDIR="services"
$([ "$1" = - ] || echo "DISPATCH_STALL_ENABLED=\"$1\"")
$([ "$2" = - ] || echo "DISPATCH_STALL_HEAL=\"$2\"")
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

[ "$CASES_FAILED" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: $CASES_PATHS путей"; exit 0; }
echo "ПРОБА КРАСНАЯ: $CASES_FAILED из $CASES_PATHS"; exit 1
