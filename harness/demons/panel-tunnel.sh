#!/usr/bin/env bash
#
# panel-tunnel.sh — панель владельца в интернете без root: туннель cloudflared
# от имени агента, которого держит этот демон.
#
# Что держит: ссылка, которую владелец берёт командой /panel, ведёт на живую
# панель. Вывод наружу через nginx и certbot требует root на каждую правку;
# быстрый туннель cloudflared root не требует: процесс агента сам ходит
# наружу, адрес выдаёт Cloudflare.
#
# Раз в минуту:
#   • туннеля нет — поднять (setsid, журнал $LOG_DIR/panel-tunnel.log);
#   • туннель есть, но без связи с Cloudflare (/ready) дольше
#     PANEL_TUNNEL_GRACE_SEC, считая от потери связи, — снять и поднять заново;
#     раньше — ждать: новый туннель — новый адрес панели и новый запрос к
#     api.trycloudflare.com, а он ограничен по частоте;
#   • адрес туннеля ≠ PANEL_URL — записать PANEL_URL (копия конфига в
#     $LOG_DIR/панель-копии). Панель читает PANEL_URL при каждой выдаче ссылки,
#     перезапуск ей не нужен;
#   • владельцу не пишет ничего: ссылку он берёт сам.
#
# Адресу верим только от СВОЕГО cloudflared: порт метрик — обычный TCP на
# петле, его может занять любая учётка машины, а PANEL_URL уходит владельцу
# ссылкой с токеном входа и читается `source` каждым демоном. Поэтому слушатель
# порта обязан быть нашим процессом cloudflared, а адрес — строго
# https://<слова>.trycloudflare.com.
#
# Молчит (только метка), если PANEL_TUNNEL=0 или PANEL_URL — свой домен
# (https://имя без пути): панель тогда выведена через nginx. PANEL_URL иной
# формы — прогон красный без метки, чтобы сторож меток назвал демона.
#
# Известное ограничение: за туннелем все посетители панели видны как
# 127.0.0.1, поэтому лимит частоты входа у них общий.
#
# Бинарь: если его нет, демон ставит закреплённую версию в ~/.local/bin и
# сверяет sha256 — без совпадения не ставит.
#
# Запуск: cron, раз в минуту, от имени $AGENT_USER; метка panel-tunnel.
# Служебное: --selftest (стенд с поддельным cloudflared).
set -euo pipefail
export LC_ALL=C.UTF-8

HERE="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

CLOUDFLARED_VERSION="2025.8.1"
declare -A CLOUDFLARED_SHA256=(
    [amd64]="a66353004197ee4c1fcb68549203824882bba62378ad4d00d234bdb8251f1114"
    [arm64]="9e2088063c8b8f71ce4b15d65e6f4b1ef345f90c9c15e762cfd2bc8fc63cf22a"
)
TUNNEL_HOST_RE='^[a-z0-9-]+\.trycloudflare\.com$'

# ── самотест ────────────────────────────────────────────────────────────────
if [ "${1:-}" = "--selftest" ]; then
    T=$(mktemp -d)
    STRAY=()
    cleanup() {
        local p
        for p in "${STRAY[@]}" "$(cat "$T/log/panel-tunnel.pid" 2>/dev/null || true)"; do
            [ -z "$p" ] || kill "$p" 2>/dev/null || true
        done
        rm -rf "$T"
    }
    trap cleanup EXIT
    mkdir -p "$T/log" "$T/hb" "$T/agent"
    PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
    printf 'PROJECT_DIR="%s"\nLOG_DIR="%s/log"\nHEARTBEAT_DIR="%s/hb"\n' "$T" "$T" "$T" > "$T/install.conf"
    # Конфиг — симлинк, как на боевой машине: правка обязана идти в цель ссылки.
    ln -s "$T/agent/harness.conf" "$T/harness.conf"

    # Поддельный cloudflared: /quicktunnel — адрес из файла hostname рядом с
    # собой, /ready — 200 при «1» в файле ready, иначе 503.
    cat > "$T/fake-cloudflared" <<'PY'
#!/usr/bin/env python3
import http.server, json, os, sys
args = sys.argv[1:]
host, port = args[args.index("--metrics") + 1].rsplit(":", 1)
here = os.path.dirname(os.path.abspath(__file__))
def read(name):
    try: return open(os.path.join(here, name)).read().strip()
    except OSError: return ""
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ready":
            ok = read("ready") == "1"
            code, body = (200 if ok else 503), {"readyConnections": 1 if ok else 0}
        else:
            code, body = 200, {"hostname": read("hostname")}
        self.send_response(code); self.end_headers(); self.wfile.write(json.dumps(body).encode())
    def log_message(self, *a): pass
http.server.HTTPServer((host, int(port)), H).serve_forever()
PY
    chmod +x "$T/fake-cloudflared"
    # Чужой слушатель — тот же сервер под другим именем: не наш cloudflared.
    cp "$T/fake-cloudflared" "$T/stranger"

    ok=1
    bad() { echo "  ПЛОХО $*"; ok=0; }
    good() { echo "  ок    $*"; }
    conf() { printf 'PANEL_TUNNEL="%s"\nPANEL_URL="%s"\n' "$1" "$2" > "$T/agent/harness.conf"; }
    run() {
        rm -f "$T/hb/panel-tunnel"
        env -u PANEL_URL -u PANEL_TUNNEL HARNESS_HOME="$T" \
            HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
            CLOUDFLARED_BIN="${BIN:-$T/fake-cloudflared}" PANEL_TUNNEL_METRICS="127.0.0.1:$PORT" \
            PANEL_TUNNEL_WAIT_SEC=10 PANEL_TUNNEL_GRACE_SEC="${GRACE:-300}" \
            bash "$0" >> "$T/run.log" 2>&1
    }
    url_now() { sed -n 's/^PANEL_URL="\(.*\)"/\1/p' "$T/agent/harness.conf"; }
    tunnel_pid() { cat "$T/log/panel-tunnel.pid" 2>/dev/null || true; }
    hb() { [ -e "$T/hb/panel-tunnel" ]; }
    stop_tunnel() {
        local p; p=$(tunnel_pid)
        [ -z "$p" ] || kill "$p" 2>/dev/null || true
        rm -f "$T/log/panel-tunnel.pid"; sleep 0.3
    }

    # 1. Туннеля нет → поднят, адрес записан.
    echo "alpha-one.trycloudflare.com" > "$T/hostname"; echo 1 > "$T/ready"
    conf 1 ""
    run || bad "прогон «туннеля нет» упал: $(tail -3 "$T/run.log")"
    PID=$(tunnel_pid)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then good "БОЛЬНОЙ СЛУЧАЙ: туннеля нет — поднят"
    else bad "туннеля нет — не поднят"; fi
    if [ "$(url_now)" = "https://alpha-one.trycloudflare.com" ] && hb; then good "адрес нового туннеля записан, метка стоит"
    else bad "после подъёма PANEL_URL=«$(url_now)»"; fi
    if [ -L "$T/harness.conf" ]; then good "БОЛЬНОЙ СЛУЧАЙ: конфиг-симлинк остался симлинком"
    else bad "запись адреса заменила симлинк конфига файлом"; fi
    if [ -z "$PID" ]; then bad "pid туннеля не записан — лок проверить не на чем"
    elif readlink "/proc/$PID/fd/"* 2>/dev/null | grep -q 'panel-tunnel.lock'; then
        bad "процесс туннеля унаследовал дескриптор лока"
    else good "БОЛЬНОЙ СЛУЧАЙ: туннель не держит дескриптор лока"; fi

    # 2. Адрес сменился → PANEL_URL переписан, копия конфига, второй туннель не поднят.
    echo "beta-two.trycloudflare.com" > "$T/hostname"
    run || bad "прогон «адрес сменился» упал: $(tail -3 "$T/run.log")"
    if [ "$(url_now)" = "https://beta-two.trycloudflare.com" ]; then good "БОЛЬНОЙ СЛУЧАЙ: адрес сменился — PANEL_URL переписан"
    else bad "адрес сменился: PANEL_URL=«$(url_now)»"; fi
    if compgen -G "$T/log/панель-копии/harness.conf.*" >/dev/null; then good "копия конфига снята до правки"
    else bad "копии конфига нет"; fi
    if [ -n "$PID" ] && [ "$(tunnel_pid)" = "$PID" ]; then good "живой туннель не поднят второй раз"
    else bad "при живом туннеле поднят второй"; fi

    # 3. Адрес тот же → конфиг не тронут, метка есть.
    cp "$T/agent/harness.conf" "$T/conf.before"
    run || bad "прогон «адрес тот же» упал: $(tail -3 "$T/run.log")"
    if cmp -s "$T/agent/harness.conf" "$T/conf.before" && hb; then good "адрес тот же — конфиг не тронут, метка стоит"
    else bad "адрес тот же: конфиг $(cmp -s "$T/agent/harness.conf" "$T/conf.before" && echo цел || echo ИЗМЕНЁН)"; fi

    # 4. Связи с Cloudflare нет, разрыв свежий → не снят, метки нет.
    echo 0 > "$T/ready"
    if run; then bad "нет связи с Cloudflare, а прогон зелёный"; fi
    if [ "$(tunnel_pid)" = "$PID" ] && kill -0 "$PID" 2>/dev/null && ! hb; then
        good "БОЛЬНОЙ СЛУЧАЙ: нет связи, разрыв свежий — ждём, не снят, метки нет"
    else bad "свежий разрыв: pid $(tunnel_pid) (был $PID), метка $(hb && echo ЕСТЬ || echo нет)"; fi

    # 4б. Туннель старше срока, связь пропала только что → не снят: срок
    # считается от потери связи, а не от старта процесса.
    rm -f "$T/log/panel-tunnel.down-since"; sleep 2.2
    GRACE=2 run || true
    if [ "$(tunnel_pid)" = "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        good "БОЛЬНОЙ СЛУЧАЙ: туннель старше срока, разрыв свежий — не снят, адрес не меняется"
    else bad "старый туннель снят на первом же разрыве: pid $(tunnel_pid) (был $PID)"; fi

    # 5. Связи нет дольше срока → свой туннель снят, поднят новый.
    echo $(( $(date +%s) - 400 )) > "$T/log/panel-tunnel.down-since"
    run || true
    sleep 0.3
    if ! kill -0 "$PID" 2>/dev/null && [ -n "$(tunnel_pid)" ] && [ "$(tunnel_pid)" != "$PID" ]; then
        good "БОЛЬНОЙ СЛУЧАЙ: нет связи дольше срока — свой туннель снят, поднят новый"
    else bad "старый туннель без связи: прежний $(kill -0 "$PID" 2>/dev/null && echo ЖИВ || echo снят), новый «$(tunnel_pid)»"; fi

    # 5б. Связь вернулась → отметка разрыва снята, метка стоит.
    echo 1 > "$T/ready"
    echo $(( $(date +%s) - 100 )) > "$T/log/panel-tunnel.down-since"
    run || bad "прогон «связь вернулась» упал: $(tail -3 "$T/run.log")"
    if [ ! -e "$T/log/panel-tunnel.down-since" ] && hb; then good "связь вернулась — отметка разрыва снята"
    else bad "связь вернулась, а отметка разрыва осталась"; fi

    # 6. В pid-файле чужой живой процесс → не тронут.
    stop_tunnel
    sleep 300 & STRANGER_PID=$!; STRAY+=("$STRANGER_PID")
    echo "$STRANGER_PID" > "$T/log/panel-tunnel.pid"
    GRACE=0 run || bad "прогон с чужим pid упал: $(tail -3 "$T/run.log")"
    if kill -0 "$STRANGER_PID" 2>/dev/null && [ "$(tunnel_pid)" != "$STRANGER_PID" ]; then
        good "БОЛЬНОЙ СЛУЧАЙ: в pid-файле чужой процесс — не тронут, туннель поднят свой"
    else bad "чужой pid $STRANGER_PID: $(kill -0 "$STRANGER_PID" 2>/dev/null && echo жив || echo УБИТ), pid-файл «$(tunnel_pid)»"; fi

    # 7. Порт метрик слушает не наш cloudflared с вредным адресом → конфиг не тронут.
    stop_tunnel
    printf '%s\n' 'x$(touch '"$T"'/PWNED).evil.example' > "$T/hostname"
    "$T/stranger" --metrics "127.0.0.1:$PORT" & STRAY+=("$!"); sleep 0.5
    cp "$T/agent/harness.conf" "$T/conf.before"
    if run; then bad "чужой слушатель порта метрик: прогон зелёный"; fi
    if cmp -s "$T/agent/harness.conf" "$T/conf.before" && [ -z "$(tunnel_pid)" ] && ! hb; then
        good "БОЛЬНОЙ СЛУЧАЙ: порт метрик слушает чужой — адрес не взят, конфиг цел"
    else bad "чужой слушатель: PANEL_URL=«$(url_now)», pid-файл «$(tunnel_pid)»"; fi
    kill "${STRAY[-1]}" 2>/dev/null || true; sleep 0.3

    # 8. Свой туннель отдаёт адрес не той формы → конфиг не тронут.
    run || true
    if cmp -s "$T/agent/harness.conf" "$T/conf.before" && ! hb && [ ! -e "$T/PWNED" ]; then
        good "БОЛЬНОЙ СЛУЧАЙ: адрес не *.trycloudflare.com — не записан, метки нет"
    else bad "вредный адрес своего туннеля: PANEL_URL=«$(url_now)»"; fi
    stop_tunnel
    echo "beta-two.trycloudflare.com" > "$T/hostname"

    # 9. Свой домен (nginx) → ничего не поднято, конфиг не тронут.
    conf 1 "https://panel.example.org"
    run || bad "прогон nginx-режима упал: $(tail -3 "$T/run.log")"
    if [ -z "$(tunnel_pid)" ] && [ "$(url_now)" = "https://panel.example.org" ] && hb; then
        good "БОЛЬНОЙ СЛУЧАЙ: свой домен (nginx) — демон молчит, метка стоит"
    else bad "nginx-режим: pid-файл «$(tunnel_pid)», PANEL_URL=«$(url_now)»"; fi

    # 10. PANEL_URL непонятной формы → прогон красный, метки нет.
    conf 1 "https://abc.trycloudflare.com/"
    if run; then bad "PANEL_URL с хвостом: прогон зелёный"
    elif hb || [ -n "$(tunnel_pid)" ]; then bad "PANEL_URL с хвостом: метка или туннель"
    else good "БОЛЬНОЙ СЛУЧАЙ: PANEL_URL непонятной формы — красный, без метки"; fi

    # 11. Ключ выключен → ничего не поднято.
    conf 0 ""
    run || bad "прогон PANEL_TUNNEL=0 упал"
    if [ -z "$(tunnel_pid)" ] && [ -z "$(url_now)" ]; then good "PANEL_TUNNEL=0 — туннель не поднят"
    else bad "PANEL_TUNNEL=0, а туннель поднят"; fi

    # 12. Бинаря нет, скачанное не совпало по sha256 → не поставлено, прогон красный.
    conf 1 ""
    echo "подделка" > "$T/not-cloudflared"
    if CLOUDFLARED_URL="file://$T/not-cloudflared" BIN="$T/bin/cloudflared" run; then
        bad "чужой бинарь: прогон зелёный"
    elif [ -e "$T/bin/cloudflared" ]; then bad "бинарь с чужим sha256 поставлен"
    else good "БОЛЬНОЙ СЛУЧАЙ: sha256 не совпал — бинарь не поставлен, прогон красный"; fi

    [ "$ok" = 1 ] && { echo "SELFTEST: зелёный (19 проверок, тринадцать больных)"; exit 0; }
    echo "--- журнал стенда"; cat "$T/run.log"; tail -5 "$T/log/panel-tunnel.log" 2>/dev/null
    echo "SELFTEST: КРАСНЫЙ"; exit 1
fi

# shellcheck disable=SC1091
if [ -f "$HERE/../../scripts/lib/config.sh" ]; then
    source "$HERE/../../scripts/lib/config.sh"
else
    source "$HERE/../scripts/lib/config.sh"
fi
HARNESS_CONF="${HARNESS_CONF:-$(konf_koren)/harness.conf}"
konf_zagruzit

PANEL_TUNNEL="${PANEL_TUNNEL:-1}"
PANEL_URL="${PANEL_URL:-}"
# 20241 — первый адрес метрик, который cloudflared берёт сам: так туннель,
# поднятый руками без --metrics, тоже находится.
PANEL_TUNNEL_METRICS="${PANEL_TUNNEL_METRICS:-127.0.0.1:20241}"
PANEL_TUNNEL_WAIT_SEC="${PANEL_TUNNEL_WAIT_SEC:-30}"
PANEL_TUNNEL_GRACE_SEC="${PANEL_TUNNEL_GRACE_SEC:-300}"
CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-$HOME/.local/bin/cloudflared}"
TUNNEL_LOG="$LOG_DIR/panel-tunnel.log"
PID_FILE="$LOG_DIR/panel-tunnel.pid"
DOWN_FILE="$LOG_DIR/panel-tunnel.down-since"   # с какой секунды нет связи с Cloudflare
HB="$HEARTBEAT_DIR/panel-tunnel"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

mkdir -p "$LOG_DIR/locks" "$HEARTBEAT_DIR"
exec 9>>"$LOG_DIR/locks/panel-tunnel.lock"
flock -n 9 || exit 0

[ "$PANEL_TUNNEL" = 1 ] || { touch "$HB"; exit 0; }
if [ -n "$PANEL_URL" ] && ! [[ "$PANEL_URL" =~ ^https://(.+)$ && "${BASH_REMATCH[1]}" =~ $TUNNEL_HOST_RE ]]; then
    if [[ "$PANEL_URL" =~ ^https://[a-z0-9.-]+$ ]]; then
        touch "$HB"; exit 0          # свой домен: панель выведена nginx
    fi
    say "PANEL_URL=«$PANEL_URL» — ни свой домен (https://имя), ни туннель; не трогаю"
    exit 1
fi

# Pid процесса, слушающего порт метрик, если он виден этой учётке; «foreign» —
# порт слушают, но процесс чужой; пусто — порт свободен.
listener_pid() {
    python3 - "${PANEL_TUNNEL_METRICS##*:}" <<'PY'
import os, sys
port = "%04X" % int(sys.argv[1])
inodes = set()
for table in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        rows = open(table).read().splitlines()[1:]
    except OSError:
        continue
    for row in rows:
        f = row.split()
        if f[1].rsplit(":", 1)[1] == port and f[3] == "0A":
            inodes.add("socket:[%s]" % f[9])
if not inodes:
    sys.exit(0)
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        fds = os.listdir("/proc/%s/fd" % pid)
    except OSError:
        continue
    for fd in fds:
        try:
            if os.readlink("/proc/%s/fd/%s" % (pid, fd)) in inodes:
                print(pid); sys.exit(0)
        except OSError:
            pass
print("foreign")
PY
}

is_our_tunnel() {  # $1 = pid: живой процесс нашего cloudflared
    local pid="$1" bin
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    bin=$(readlink -f "$CLOUDFLARED_BIN")
    [ "$(readlink "/proc/$pid/exe" 2>/dev/null)" = "$bin" ] && return 0
    tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | grep -qxF -e "$CLOUDFLARED_BIN" -e "$bin"
}

metrics_get() {  # $1 = путь ручки, $2 = поле json
    curl -s -m 3 "http://$PANEL_TUNNEL_METRICS$1" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1],""))' "$2" 2>/dev/null || true
}

tunnel_ready() { [ "$(metrics_get /ready readyConnections)" -gt 0 ] 2>/dev/null; }

ensure_binary() {
    [ -x "$CLOUDFLARED_BIN" ] && return 0
    local arch url tmp got
    arch=$(dpkg --print-architecture 2>/dev/null || uname -m)
    case "$arch" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; esac
    [ -n "${CLOUDFLARED_SHA256[$arch]:-}" ] || { say "нет закреплённого cloudflared под $arch"; return 1; }
    url="${CLOUDFLARED_URL:-https://github.com/cloudflare/cloudflared/releases/download/$CLOUDFLARED_VERSION/cloudflared-linux-$arch}"
    mkdir -p "$(dirname "$CLOUDFLARED_BIN")"
    tmp=$(mktemp "$(dirname "$CLOUDFLARED_BIN")/.cloudflared.XXXXXX")
    curl -fsSL -m 120 -o "$tmp" "$url" || { rm -f "$tmp"; say "cloudflared не скачан: $url"; return 1; }
    got=$(sha256sum "$tmp" | cut -d' ' -f1)
    if [ "$got" != "${CLOUDFLARED_SHA256[$arch]}" ]; then
        rm -f "$tmp"; say "cloudflared: sha256 $got не совпал с закреплённым — не ставлю"; return 1
    fi
    chmod 0755 "$tmp"; mv "$tmp" "$CLOUDFLARED_BIN"
    say "поставлен cloudflared $CLOUDFLARED_VERSION ($arch)"
}

panel_port() {
    local port
    port=$(systemctl show harness-panel -p ExecStart 2>/dev/null \
           | grep -o -- '--порт [0-9]\+' | grep -o '[0-9]\+' | head -1 || true)
    printf '%s' "${port:-8787}"
}

start_tunnel() {
    # 9>&- — туннель живёт дольше прогона и не должен держать лок демона.
    setsid "$CLOUDFLARED_BIN" tunnel --no-autoupdate --metrics "$PANEL_TUNNEL_METRICS" \
        --url "http://127.0.0.1:$(panel_port)" >>"$TUNNEL_LOG" 2>&1 </dev/null 9>&- &
    echo $! > "$PID_FILE"
    say "туннель поднят, pid $!"
}

write_url() {  # $1 = значение PANEL_URL, уже проверенное шаблоном
    local target copies stamp
    # sed -i по симлинку заменил бы ссылку файлом в каталоге, куда агент не пишет.
    target=$(readlink -f "$HARNESS_CONF")
    copies="$LOG_DIR/панель-копии"
    stamp=$(date +%Y%m%d-%H%M%S)
    mkdir -p "$copies"
    cp -p "$target" "$copies/harness.conf.$stamp"
    if grep -qE '^\s*PANEL_URL=' "$target"; then
        sed -i "s|^\s*PANEL_URL=.*|PANEL_URL=\"$1\"|" "$target"
    else
        printf 'PANEL_URL="%s"\n' "$1" >> "$target"
    fi
    printf '{"ts":"%s","ключ":"PANEL_URL","стало":"%s","кто":"panel-tunnel"}\n' \
        "$(date -Is)" "$1" >> "$LOG_DIR/панель.jsonl"
}

LISTENER=$(listener_pid)
if [ -n "$LISTENER" ] && ! is_our_tunnel "$LISTENER"; then
    say "порт метрик $PANEL_TUNNEL_METRICS слушает не наш cloudflared (pid $LISTENER) — адрес не беру"
    exit 1
fi

PID=""
if [ -n "$LISTENER" ]; then
    PID="$LISTENER"
    [ "$(cat "$PID_FILE" 2>/dev/null || true)" = "$PID" ] || echo "$PID" > "$PID_FILE"
else
    OLD=$(cat "$PID_FILE" 2>/dev/null || true)
    if is_our_tunnel "$OLD"; then PID="$OLD"; fi
fi

if [ -z "$PID" ] || ! tunnel_ready; then
    if [ -n "$PID" ]; then
        # Срок — от потери связи, а не от старта: у живого туннеля одно
        # соединение, и мгновенный разрыв не повод менять адрес панели.
        NOW=$(date +%s)
        [ -s "$DOWN_FILE" ] || echo "$NOW" > "$DOWN_FILE"
        DOWN=$(( NOW - $(cat "$DOWN_FILE") ))
        if [ "$DOWN" -lt "$PANEL_TUNNEL_GRACE_SEC" ]; then
            say "туннель pid $PID без связи с Cloudflare ${DOWN} с из $PANEL_TUNNEL_GRACE_SEC — жду"
            exit 1
        fi
        kill "$PID" 2>/dev/null || true
        say "туннель pid $PID без связи с Cloudflare ${DOWN} с — снят"
        sleep 1
    fi
    ensure_binary
    rm -f "$DOWN_FILE"
    start_tunnel
    PID=$(cat "$PID_FILE")
    for _ in $(seq "$PANEL_TUNNEL_WAIT_SEC"); do
        sleep 1
        [ "$(listener_pid)" = "$PID" ] && tunnel_ready && break
    done
    if [ "$(listener_pid)" != "$PID" ] || ! tunnel_ready; then
        say "туннель pid $PID не связался с Cloudflare за $PANEL_TUNNEL_WAIT_SEC с — журнал $TUNNEL_LOG"
        exit 1
    fi
fi

rm -f "$DOWN_FILE"

HOST=$(metrics_get /quicktunnel hostname)
# Слушатель сверяется ещё раз ПОСЛЕ чтения: между проверкой и запросом наш
# туннель мог умереть, а порт — достаться чужому.
if [ "$(listener_pid)" != "$PID" ]; then
    say "слушатель порта метрик сменился во время чтения адреса — не записываю"
    exit 1
fi
if ! [[ "$HOST" =~ $TUNNEL_HOST_RE ]]; then
    say "туннель отдал адрес не той формы — не записываю: $(printf '%q' "$HOST")"
    exit 1
fi

if [ "https://$HOST" != "$PANEL_URL" ]; then
    write_url "https://$HOST"
    say "адрес панели: https://$HOST (было «$PANEL_URL»)"
fi
touch "$HB"
