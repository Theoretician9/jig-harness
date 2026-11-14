#!/usr/bin/env bash
#
# test_voice_worker.sh — проба «голос отдельным обработчиком» (спека
# docs/specs/2026-10-27-мгновенная-доставка-сообщений.md §4.3, §4.6, К-4, П-2, П-3,
# П-9, П-10, П-11, П-15; план docs/plans/2026-10-28-доставка-голос.md; карточка
# hr.remove-message-interception-mechanism).
#
# ЧТО ОХРАНЯЕТ. Скачивание и расшифровка голосового шли внутри цикла приёма: до
# 71 с на живом окне, и всё это время канал не принимал ничего — текст, пришедший
# следом, ждал расшифровки чужого сообщения. Голос уходит обработчику (один на
# машину, под пределом памяти), диспетчер забирает итог на круге и толкает
# расшифровку под меткой исходного сообщения.
#
# Пути (печатается их число):
#   1. диспетчер целым циклом с подставным Bot API, подделкой tmux и заглушками
#      скачивания и расшифровки: текст не ждёт голос, метки голоса, сырого JSON в
#      корне входящих нет, пауза, предел одного обработчика, убитый обработчик,
#      дубль message_id, отказ скачивания и расшифровки, секрет в расшифровке;
#   2. прибор: голос не судится в К-1 (его задержка — расшифровка);
#   3. код: ключ предела памяти в harness.conf пакета.
#
# Стенд: свой LOG_DIR и каталог паузы, Bot API на своём порту, tmux — подделка первой
# в PATH, наружный https закрыт (https_proxy в никуда): ни Telegram, ни whisper не
# зовутся. Процессы стенда находятся по пути стенда в окружении и убиваются по нему.
#
#   bash scripts/test_voice_worker.sh [--случай диспетчер|прибор|код]
# Код возврата: 0 — зелёная, 1 — красная. Последняя строка — итог с числом путей.
# Механизмы берутся от места самой пробы (../ от scripts/): мутации идут «из-копии».
#
# Имена переменных ЛАТИНИЦЕЙ (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8

KOREN="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
CONF_SEED="$KOREN/harness/config/harness.conf"; [ -f "$CONF_SEED" ] || CONF_SEED="$KOREN/config/harness.conf"
DISPATCHER="$KOREN/scripts/tg-dispatcher.sh"
INSTRUMENT="$KOREN/scripts/delivery-latency.py"
PAUSE_PY="$KOREN/scripts/pause.py"
for f in "$DISPATCHER" "$INSTRUMENT" "$PAUSE_PY" "$CONF_SEED" "$KOREN/scripts/lib/events.py"; do
    [ -r "$f" ] || { echo "ПРОБА: нет механизма $f — проверять нечего"; exit 1; }
done

ONLY=""
if [ "${1:-}" = "--случай" ]; then
    ONLY="${2:-}"
    case "$ONLY" in диспетчер|прибор|код) ;; *) echo "ПРОБА: неизвестный случай «$ONLY» (диспетчер|прибор|код)"; exit 1 ;; esac
elif [ -n "${1:-}" ]; then
    echo "ПРОБА: неизвестный ключ «$1»"; exit 1
fi
want() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }

source "$KOREN/scripts/lib/selftest_cases.sh"

T=$(mktemp -d /tmp/proba-voice-worker-XXXXXX)
echo $$ > "$T/owner"
STAMP="proba-voice-worker-$$"
PATHS_JUDGED=0
PIDS_KILL=()
# Процессы стенда находятся по окружению: у каждого есть значение с путём стенда
# (PATH, HARNESS_PANEL_STATE, PROBA_STAND), а командная строка диспетчера метки не несёт.
sweep_stand() {  # $@ = каталоги стендов со слешем; печатает число убитых
    local f pid n=0 k pat=()
    [ $# -gt 0 ] || { echo 0; return; }
    for f in "$@"; do pat+=(-e "=$f"); done
    # Диспетчер живёт, пока проход до него не дошёл, и порождает потомков уже после
    # снимка /proc: проходы повторяются, пока очередной не убьёт никого.
    while :; do
        k=0
        for f in /proc/[0-9]*/environ; do
            pid="${f#/proc/}"; pid="${pid%/environ}"
            [ "$pid" = "$$" ] && continue
            grep -qaF "${pat[@]}" "$f" 2>/dev/null && kill -KILL "$pid" 2>/dev/null && k=$((k+1))
        done
        n=$((n + k)); [ "$k" -gt 0 ] || break
    done
    echo "$n"
}
cleanup() {
    local p
    for p in "${PIDS_KILL[@]}"; do kill -KILL -- "-$p" 2>/dev/null; kill -KILL "$p" 2>/dev/null; done
    sweep_stand "$T/" >/dev/null
    if [ -n "${PROBA_KEEP:-}" ]; then echo "стенд оставлен (PROBA_KEEP): $T"; else rm -rf "$T"; fi
}
trap cleanup EXIT
# Стенды прогонов, убитых KILL-ом (ловушка не сработала). Живой хозяин — это
# параллельный прогон той же пробы: его стенд не трогаем.
DEAD=()
for d in /tmp/proba-voice-worker-*/; do
    owner=$(cat "$d/owner" 2>/dev/null)
    [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null && continue
    DEAD+=("$d")
done
LEFT=$(sweep_stand "${DEAD[@]}")
if [ "$LEFT" -gt 0 ]; then
    echo "убрано процессов от прерванных прогонов: $LEFT"
fi

wait_for() {  # $1 = предел, остальное — условие
    local end=$(( $(date +%s) + $1 )); shift
    while [ "$(date +%s)" -lt "$end" ]; do "$@" && return 0; sleep 0.3; done
    "$@"
}

# ── подделка tmux ──────────────────────────────────────────────────────────
mkdir -p "$T/bin" "$T/tmuxsock"
cat > "$T/bin/tmux" <<'EOF'
#!/usr/bin/env bash
log="${PROBA_TMUX_LOG:-}"
[ -n "$log" ] || { echo "подделка tmux: позвана вне стенда" >&2; exit 1; }
printf '%s %s\n' "$(date +%s.%N)" "$*" >> "$log"
case "${1:-}" in
    has-session)     exit 0 ;;
    display-message) case "$*" in
                         *pane_dead*)            echo 0 ;;
                         *pane_current_command*) echo claude ;;
                         *activity*)             date +%s ;;
                         *)                      echo "" ;;
                     esac ;;
esac
exit 0
EOF
chmod +x "$T/bin/tmux"
stand_env() { env -u TMUX TMUX_TMPDIR="$T/tmuxsock" PATH="$T/bin:$PATH" "$@"; }
[ "$(stand_env bash -c 'command -v tmux')" = "$T/bin/tmux" ] \
    || { echo "ПРОБА: под окружением стенда tmux — не подделка; стенд не собран"; exit 1; }

# ── заглушки скачивания и расшифровки ──────────────────────────────────────
# Поведение — файлы стенда рядом с журналом заглушки: сон, отказ, текст. Файл
# читается на КАЖДОМ вызове: случай «убит посреди расшифровки» меняет сон между
# первым и вторым запуском.
cat > "$T/dl.py" <<'PYEOF'
import json, os, sys
msg = json.load(open(sys.argv[1], encoding="utf-8"))
stand = os.path.dirname(os.environ["PROBA_TR_LOG"])
if os.path.exists(os.path.join(stand, "dl-fail")):
    print("getFile: Bad Request: wrong file_id (проба)", file=sys.stderr)
    sys.exit(1)
os.makedirs(os.path.join(stand, "files"), exist_ok=True)
path = os.path.join(stand, "files", f"voice-{msg['voice']['file_id']}.oga")
open(path, "w").write("звук")
print(path)
PYEOF
cat > "$T/tr.py" <<'PYEOF'
import os, resource, sys, time
log = os.environ["PROBA_TR_LOG"]; stand = os.path.dirname(log)
name = os.path.basename(sys.argv[1])
soft = resource.getrlimit(resource.RLIMIT_AS)[0]
def put(line):
    with open(log, "a") as fh:
        fh.write(line + "\n")
put(f"start {time.time():.3f} {os.getpid()} {os.getsid(0)} {name} {soft}")
def read(n, d=""):
    try:
        return open(os.path.join(stand, n), encoding="utf-8").read().strip()
    except OSError:
        return d
time.sleep(float(read("tr-sleep", "0") or 0))
put(f"end {time.time():.3f} {os.getpid()} {name}")
if os.path.exists(os.path.join(stand, "tr-fail")):
    print("расшифровка упала: " + "х" * 350 + " RuntimeError: проба")
    sys.exit(2)
print(read("tr-text") or f"расшифровка {name}")
PYEOF

# ── разбор журнала меток ───────────────────────────────────────────────────
cat > "$T/marks.py" <<'PYEOF'
import json, sys

def rows(path):
    out = []
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out

def of(path, event):
    return [r for r in rows(path) if r.get("event") == f"delivery.{event}"]

def data(r):
    return r.get("data") or {}

def ts_list(r):
    return [x for x in str(data(r).get("ts_список") or "").split(",") if x]

def ts_of(path, mid, nth="0"):
    hits = [str(data(r).get("ts")) for r in of(path, "received") if str(data(r).get("message_id")) == mid]
    return hits[int(nth)] if len(hits) > int(nth) else "нет"

def count(path, event, key, value):
    return str(sum(1 for r in of(path, event) if str(data(r).get(key)) == value))

def field(path, event, key, value, want):
    hits = [r for r in of(path, event) if str(data(r).get(key)) == value]
    if not hits:
        return "нет метки"
    return str(hits[0].get(want)) if want in ("src", "outcome") else str(data(hits[0]).get(want))

def covered(path, event, ts):
    return str(sum(1 for r in of(path, event) if ts in ts_list(r)))

def cover_field(path, event, ts, want):
    hits = [r for r in of(path, event) if ts in ts_list(r)]
    if not hits:
        return "нет метки"
    return str(hits[0].get(want)) if want in ("src", "outcome") else str(data(hits[0]).get(want))

def kick_when(path, ts):
    xs = [float(data(r).get("когда")) for r in of(path, "kick_sent") if ts in ts_list(r)]
    return f"{min(xs):.3f}" if xs else "нет"

def journal(path, mid, kind):
    n = 0
    for r in rows(path):
        if str(r.get("message_id")) == mid and str(r.get("вид")) == kind:
            n += 1
    return str(n)

cmd, args = sys.argv[1], sys.argv[2:]
print({"ts_of": ts_of, "count": count, "field": field, "covered": covered,
       "cover_field": cover_field, "kick_when": kick_when, "journal": journal}[cmd](*args))
PYEOF
mk() { python3 "$T/marks.py" "$@"; }
cnt() { local n; n=$(grep -c -- "$1" "$2" 2>/dev/null); echo "${n:-0}"; }  # $1 = образец, $2 = файл

# ═══ 1. ДИСПЕТЧЕР целым циклом ═════════════════════════════════════════════
if want диспетчер; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 1: tg-dispatcher.sh целым циклом — голос отдельным обработчиком"
    # Bot API: update виден с момента «after» от старта, первая выдача пишется в
    # served — это момент, когда сообщение пришло на машину.
    cat > "$T/api.py" <<'PYEOF'
import json, sys, time, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
port, acks, ready, upd, served = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
UPDATES = json.load(open(upd, encoding="utf-8"))
START = time.time()
SEEN = set()
def update(u):
    m = {"message_id": u["mid"], "date": int(time.time()), "chat": {"id": 777000}}
    if u["kind"] == "voice":
        m["voice"] = {"file_id": f"F{u['mid']}", "duration": 3}
    else:
        m["text"] = u["text"]
    return {"update_id": u["id"], "message": m}
class H(BaseHTTPRequestHandler):
    def reply(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        if self.path.endswith("/getUpdates"):
            off = int((form.get("offset") or ["0"])[0])
            now = time.time()
            ids = [u for u in UPDATES if u["id"] >= off and now - START >= u.get("after", 0)]
            if not ids:
                time.sleep(1)
            with open(served, "a") as fh:
                for u in ids:
                    if u["id"] not in SEEN:
                        SEEN.add(u["id"]); fh.write(f"{u['id']} {now:.3f}\n")
            self.reply({"ok": True, "result": [update(u) for u in ids]}); return
        with open(acks, "a") as fh:
            fh.write((form.get("text") or [""])[0].replace("\n", " ") + "\n")
        self.reply({"ok": True, "result": {"message_id": 1}})
    do_GET = do_POST
    def log_message(self, *_): pass
srv = HTTPServer(("127.0.0.1", port), H)
open(ready, "w").close()
srv.serve_forever()
PYEOF
    D_STAND_BAD=0
    d_bad() { D_STAND_BAD=$((D_STAND_BAD + 1)); echo "  СТЕНД: $1"; }
    DPID=""

    # $1 = имя, $2 = JSON updates; флаги файлами стенда до старта (tr-sleep и т. п.).
    d_start() {
        local name="$1" spec="$2" port
        S="$T/disp-$name"; D_STAND_BAD=0
        mkdir -p "$S/log" "$S/project" "$S/panel"
        printf '%s' "$spec" > "$S/updates.json"
        : > "$S/tmux-calls"; : > "$S/acks"; : > "$S/served"; : > "$S/tr-log"
        printf '000:СТЕНДОВЫЙ' > "$S/token"   # не-секрет: подставной Bot API стенда
        port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
        PROBA_STAND="$T/" python3 "$T/api.py" "$port" "$S/acks" "$S/api.ready" "$S/updates.json" "$S/served" > "$S/api.out" 2>&1 &
        PIDS_KILL+=($!); disown $!
        wait_for 10 test -e "$S/api.ready" || { d_bad "Bot API не поднялся"; return; }
        cat > "$S/install.conf" <<EOF
PROJECT_DIR="$S/project"
PROJECT_NAME="стенд"
LOG_DIR="$S/log"
TG_CHAT_ID="777000"
TG_TOKEN_FILE="$S/token"
AGENT_USER="$(id -un)"
PANEL_STATE_DIR="$S/panel"
EOF
        # Предел памяти стенда отличен от умолчания пакета: заглушка видит, чей он.
        # Пол ниже потолка, доля 100%: действующий предел — потолок, и подмена
        # действующего предела полом ($VOICE_VM_LIMIT_KB) видна; при поле = потолку
        # она проходила бы молча.
        printf 'TMUX_SESSION="%s"\nSERVICE_LOCKS_SUBDIR="services"\nPANE_TMUX_TIMEOUT="5"\nPANE_ENTER_DELAY="0.1"\nPANE_SEND_NOW="0"\nVOICE_VM_LIMIT_KB="1500000"\nVOICE_VM_MEM_PERCENT="100"\nVOICE_VM_MAX_KB="2400000"\n' \
            "$STAMP-$name" > "$S/harness.conf"
        mkdir -p "$S/log/locks/services"
        [ -e "$S/pause" ] && {
            HARNESS_PANEL_STATE="$S/panel" python3 "$PAUSE_PY" --поставить "проба: владелец остановил" --кто владелец >/dev/null
            HARNESS_PANEL_STATE="$S/panel" python3 "$PAUSE_PY" --идёт-ли \
                || { echo "ПРОБА: pause.py не поставил паузу на стенде — проверять нечего"; exit 1; }
        }
        stand_env env -u HARNESS_LOG_DIR PROBA_TMUX_LOG="$S/tmux-calls" \
            PROBA_TR_LOG="$S/tr-log" \
            HARNESS_DOWNLOAD_PY="$T/dl.py" HARNESS_TRANSCRIBE_PY="$T/tr.py" \
            https_proxy="http://127.0.0.1:9" HTTPS_PROXY="http://127.0.0.1:9" no_proxy="127.0.0.1,localhost" \
            HARNESS_PANEL_STATE="$S/panel" \
            HARNESS_INSTALL_CONF="$S/install.conf" HARNESS_CONF="$S/harness.conf" \
            TG_API_BASE="http://127.0.0.1:$port" \
            setsid nohup bash "$DISPATCHER" > "$S/dispatcher.out" 2>&1 < /dev/null &
        DPID=$!
        PIDS_KILL+=("$DPID"); disown "$DPID"
        DJ="$S/log/доставка.jsonl"; IJ="$S/log/inbox.jsonl"
    }
    d_stop() {
        sleep 1.5   # фоновые метки дописываются
        kill -KILL -- "-$DPID" 2>/dev/null; kill -KILL "$DPID" 2>/dev/null
        sweep_stand "$S/" >/dev/null
        case_check "СТЕНД ($1): сбоев стенда нет" "0" "$D_STAND_BAD"
        [ "$D_STAND_BAD" = 0 ] || tail -5 "$S/log/tg-dispatcher.log" 2>/dev/null | sed 's/^/    /'
    }
    upd() { python3 -c 'import json,sys; print(json.dumps([dict(zip(("id","mid","kind","text","after"), (int(a), int(b), c, d, float(e)))) for a,b,c,d,e in (x.split(":",4) for x in sys.argv[1:])], ensure_ascii=False))' "$@"; }
    kicked() { [ "$(mk covered "$DJ" kick_sent "$(mk ts_of "$DJ" "$1")")" -ge 1 ] 2>/dev/null; }  # $1 = message_id
    started() { grep -q '^start' "$S/tr-log" 2>/dev/null; }
    root_txt() { find "$S/log/inbox" -maxdepth 1 -name "$1.txt" 2>/dev/null | grep -c .; }
    served_at() { awk -v id="$1" '$1 == id {print $2; exit}' "$S/served"; }
    max_overlap() {
        python3 - "$S/tr-log" <<'PYEOF'
import sys
ev = []
for line in open(sys.argv[1]):
    p = line.split()
    if p and p[0] in ("start", "end"):
        ev.append((float(p[1]), 1 if p[0] == "start" else -1))
cur = best = 0
for _, d in sorted(ev, key=lambda e: (e[0], e[1])):
    cur += d; best = max(best, cur)
print(best)
PYEOF
    }

    # (а) БОЛЬНОЙ К-4/П-2: голос с расшифровкой 15 с, следом текст.
    echo "— (а) голосовое, следом текст: текст не ждёт расшифровки"
    mkdir -p "$T/disp-a"; echo 15 > "$T/disp-a/tr-sleep"
    d_start a "$(upd '101:101:voice::0' '102:102:text:почини кнопку обновления:2')"
    wait_for 60 kicked 101 || d_bad "голос не толкнут за 60 с"
    wait_for 10 kicked 102 || d_bad "текст не толкнут"
    TS_V=$(mk ts_of "$DJ" 101); TS_T=$(mk ts_of "$DJ" 102)
    K_T=$(mk kick_when "$DJ" "$TS_T"); A_T=$(served_at 102)
    case_check "БОЛЬНОЙ К-4: толчок текста ≤ 3 с от его прихода на машину (голос ещё расшифровывается)" "да" \
        "$(python3 -c 'import sys
try: print("да" if float(sys.argv[1]) - float(sys.argv[2]) <= 3 else f"через {float(sys.argv[1]) - float(sys.argv[2]):.1f} с")
except ValueError: print(f"нет метки (толчок {sys.argv[1]}, приход {sys.argv[2]})")' "$K_T" "${A_T:-нет}")"
    case_check "(а) голос толкнут после итога расшифровки (kick_sent на ts приёма голоса)" "1" "$(mk covered "$DJ" kick_sent "$TS_V")"
    case_check "(а) расшифровка лежит в inbox/<ts приёма голоса>.txt" "расшифровка voice-F101.oga" \
        "$(cat "$S/log/inbox/$TS_V.txt" 2>/dev/null || echo нет файла)"
    d_stop a

    # (б) метки голоса и сырой JSON вне корня входящих.
    echo "— (б) метки голоса, JSON не в корне входящих"
    mkdir -p "$T/disp-b"; echo 6 > "$T/disp-b/tr-sleep"
    d_start b "$(upd '201:201:voice::0')"
    wait_for 20 started || d_bad "расшифровка не началась за 20 с"
    TS_V=$(mk ts_of "$DJ" 201)
    case_check "БОЛЬНОЙ (б): пока идёт расшифровка, в корне inbox/ файла голоса нет" "0" "$(root_txt "$TS_V")"
    case_check "(б) пока идёт расшифровка, сообщение ждёт в inbox/вложения/<message_id>.json" "1" \
        "$(find "$S/log/inbox/вложения" -maxdepth 1 -name '201.json' 2>/dev/null | grep -c .)"
    case_check "(б) inbox.jsonl: строка «вложение» до итога" "1" "$(mk journal "$IJ" 201 вложение)"
    wait_for 30 kicked 201 || d_bad "голос не толкнут за 30 с"
    case_check "(б) received на голос — одна" "1" "$(mk count "$DJ" received message_id 201)"
    case_check "БОЛЬНОЙ (б): attachment_started с ts приёма" "$TS_V" "$(mk field "$DJ" attachment_started message_id 201 ts)"
    case_check "(б) attachment_done: исход ok" "ok" "$(mk field "$DJ" attachment_done message_id 201 outcome)"
    case_check "(б) queued с ts приёма — одна" "1" "$(mk count "$DJ" queued ts "$TS_V")"
    case_check "(б) kick_sent на ts приёма — одна" "1" "$(mk covered "$DJ" kick_sent "$TS_V")"
    case_check "БОЛЬНОЙ (б): inbox.jsonl — строка «расшифровка» одна" "1" "$(mk journal "$IJ" 201 расшифровка)"
    case_check "(б) «расшифровал» владельцу — одно" "1" "$(cnt 'расшифровал голосовое' "$S/acks")"
    case_check "(б) пара файлов вложения убрана после подбора" "0" "$(find "$S/log/inbox/вложения" -maxdepth 1 -name '201.*' 2>/dev/null | grep -c .)"
    # 2400000 КБ — потолок harness.conf стенда (доля 100% памяти машины выше него), не пол и не умолчание пакета.
    case_check "(б) расшифровка идёт под пределом памяти из harness.conf (RLIMIT_AS)" "$((2400000 * 1024))" \
        "$(awk '$1 == "start" {print $6; exit}' "$S/tr-log")"
    d_stop b

    # (в) П-3: пауза — расшифровка легла, толчок отложен дверью.
    echo "— (в) пауза"
    mkdir -p "$T/disp-c"; echo 1 > "$T/disp-c/tr-sleep"; : > "$T/disp-c/pause"
    d_start c "$(upd '301:301:voice::0')"
    TS_V=""
    wait_for 30 bash -c '[ -n "$(find "$1" -maxdepth 1 -name "*.txt" 2>/dev/null)" ]' _ "$S/log/inbox" \
        || d_bad "расшифровка не легла во входящие за 30 с"
    sleep 2
    TS_V=$(mk ts_of "$DJ" 301)
    case_check "(в) на паузе расшифровка во входящих" "1" "$(root_txt "$TS_V")"
    case_check "(в) на паузе толчка нет" "0" "$(mk covered "$DJ" kick_sent "$TS_V")"
    case_check "БОЛЬНОЙ П-3: отказ «пауза» записала дверь pane.sh при подборе" "scripts/lib/pane.sh" \
        "$(mk cover_field "$DJ" kick_refused "$TS_V" src)"
    d_stop c

    # (г) П-11: три голосовых подряд — один обработчик, по порядку.
    echo "— (г) три голосовых подряд"
    mkdir -p "$T/disp-d"; echo 1.5 > "$T/disp-d/tr-sleep"
    d_start d "$(upd '401:401:voice::0' '402:402:voice::0' '403:403:voice::0')"
    wait_for 40 kicked 403 || d_bad "третье голосовое не толкнуто за 40 с"
    sleep 1
    case_check "П-11: одновременно расшифровывалось не больше одного" "1" "$(max_overlap)"
    case_check "(г) все три толкнуты" "3" "$(( $(kicked 401 && echo 1 || echo 0) + $(kicked 402 && echo 1 || echo 0) + $(kicked 403 && echo 1 || echo 0) ))"
    case_check "(г) расшифровка шла по порядку прихода" "voice-F401.oga voice-F402.oga voice-F403.oga" \
        "$(awk '$1 == "start" {printf "%s%s", s, $5; s=" "}' "$S/tr-log")"
    d_stop d

    # (д) П-9: обработчик убит посреди расшифровки, диспетчер жив.
    echo "— (д) обработчик убит SIGKILL"
    mkdir -p "$T/disp-e"; echo 30 > "$T/disp-e/tr-sleep"
    d_start e "$(upd '501:501:voice::0')"
    wait_for 20 started || d_bad "расшифровка не началась за 20 с"
    SID=$(awk '$1 == "start" {print $4; exit}' "$S/tr-log")
    echo 0.5 > "$S/tr-sleep"
    if [ -n "$SID" ] && [ "$SID" != "$(ps -o sid= -p "$DPID" 2>/dev/null | tr -d ' ')" ]; then
        kill -KILL -- "-$SID" 2>/dev/null
    else
        d_bad "расшифровка идёт в сессии диспетчера (sid ${SID:-нет}) — обработчика отдельно нет"
    fi
    wait_for 30 kicked 501 || d_bad "голос не толкнут за 30 с после убийства обработчика"
    sleep 1
    case_check "П-9: диспетчер жив после убийства обработчика" "да" "$(kill -0 "$DPID" 2>/dev/null && echo да || echo нет)"
    case_check "БОЛЬНОЙ П-9: обработчик поднят заново — расшифровок начато 2" "2" "$(cnt '^start' "$S/tr-log")"
    case_check "П-9: толчок о голосе — один" "1" "$(mk covered "$DJ" kick_sent "$(mk ts_of "$DJ" 501)")"
    case_check "П-9: строка «расшифровка» — одна" "1" "$(mk journal "$IJ" 501 расшифровка)"
    d_stop e

    # (е) П-10: дубль message_id — в работе и после подбора.
    echo "— (е) дубль message_id"
    mkdir -p "$T/disp-f"; echo 2 > "$T/disp-f/tr-sleep"
    d_start f "$(upd '601:600:voice::0' '602:600:voice::0' '603:600:voice::9')"
    wait_for 30 bash -c '[ -s "$1" ] && awk "\$1 == 603" "$1" | grep -q .' _ "$S/served" || d_bad "третий update не выдан за 30 с"
    sleep 4
    case_check "СТЕНД (е): received на все три update" "3" "$(mk count "$DJ" received message_id 600)"
    case_check "БОЛЬНОЙ П-10: расшифровка одна" "1" "$(cnt '^start' "$S/tr-log")"
    case_check "П-10: строка «расшифровка» — одна" "1" "$(mk journal "$IJ" 600 расшифровка)"
    case_check "П-10: «расшифровал» владельцу — одно" "1" "$(cnt 'расшифровал голосовое' "$S/acks")"
    N_K=0
    for i in 0 1 2; do N_K=$((N_K + $(mk covered "$DJ" kick_sent "$(mk ts_of "$DJ" 600 "$i")"))); done
    case_check "П-10: толчок — один на три update" "1" "$N_K"
    case_check "П-10: дубли закрыты no_kick (потерями не числятся)" "2" \
        "$(( $(mk count "$DJ" no_kick ts "$(mk ts_of "$DJ" 600 1)") + $(mk count "$DJ" no_kick ts "$(mk ts_of "$DJ" 600 2)") ))"
    d_stop f

    # (ж) отказ расшифровки и отказ скачивания.
    for stage in расшифровка скачивание; do
        echo "— (ж) отказ: $stage"
        X="$T/disp-g-$stage"; mkdir -p "$X"; echo 0.5 > "$X/tr-sleep"
        case "$stage" in расшифровка) : > "$X/tr-fail" ;; скачивание) : > "$X/dl-fail" ;; esac
        d_start "g-$stage" "$(upd '701:701:voice::0')"
        wait_for 30 kicked 701 || d_bad "толчок об отказе не отдан за 30 с"
        TS_V=$(mk ts_of "$DJ" 701)
        case_check "БОЛЬНОЙ (ж) $stage: attachment_done error, причина — этап" "error $stage" \
            "$(mk field "$DJ" attachment_done message_id 701 outcome) $(mk field "$DJ" attachment_done message_id 701 почему)"
        case "$stage" in
            расшифровка)
                case_check "(ж) владельцу — «расшифровать не смог»" "1" "$(cnt 'расшифровать не смог' "$S/acks")"
                case_check "БОЛЬНОЙ (ж) причина длиннее предела: сама ошибка в ответе владельцу и в толчке" "да да" \
                    "$(grep -qF 'RuntimeError' "$S/acks" && echo да || echo нет) $(grep -qF 'RuntimeError' "$S/tmux-calls" && echo да || echo нет)"
                case_check "(ж) толчок «расшифровать не удалось»" "да" "$(grep -qF 'расшифровать не удалось' "$S/tmux-calls" && echo да || echo нет)" ;;
            скачивание)
                case_check "(ж) владельцу — «Файл до меня НЕ дошёл»" "1" "$(cnt 'Файл до меня НЕ дошёл' "$S/acks")"
                case_check "(ж) толчок «скачать его не удалось»" "да" "$(grep -qF 'скачать его не удалось' "$S/tmux-calls" && echo да || echo нет)" ;;
        esac
        case_check "БОЛЬНОЙ (ж) $stage: толчок об отказе называет файл входящих (kick_sent на ts приёма)" "1" \
            "$(mk covered "$DJ" kick_sent "$TS_V")"
        case_check "(ж) $stage: во входящих JSON сообщения" "да" \
            "$(jq -e '.voice.file_id == "F701"' "$S/log/inbox/$TS_V.txt" >/dev/null 2>&1 && echo да || echo нет)"
        d_stop "g-$stage"
    done

    # (з) П-15: ключ в расшифровке.
    echo "— (з) ключ в расшифровке"
    KEY="ghp_$(python3 -c 'import secrets; print(secrets.token_hex(18))')"
    mkdir -p "$T/disp-h"; echo 0.5 > "$T/disp-h/tr-sleep"; printf 'вот ключ %s запомни\n' "$KEY" > "$T/disp-h/tr-text"
    d_start h "$(upd '801:801:voice::0')"
    wait_for 30 kicked 801 || d_bad "голос с ключом не толкнут за 30 с"
    N_LEAK=0
    for f in "$S/log/inbox.jsonl" "$S/log/доставка.jsonl" "$S/acks" "$S/tmux-calls" "$S/log/tg-dispatcher.log" \
             $(find "$S/log/inbox" -maxdepth 1 -name '*.txt' 2>/dev/null); do
        grep -qF -- "$KEY" "$f" 2>/dev/null && { N_LEAK=$((N_LEAK + 1)); echo "    ключ в $f"; }
    done
    case_check "П-15: ключа нет во входящих, inbox.jsonl, метках, ответах, толчке и журнале" "0" "$N_LEAK"
    case_check "БОЛЬНОЙ П-15: ключ принят судьёй секретов (лежит в inbox/секреты/)" "да" \
        "$(grep -rqF -- "$KEY" "$S/log/inbox/секреты" 2>/dev/null && echo да || echo нет)"
    d_stop h
fi

# ═══ 2. ПРИБОР: голос в К-1 не судится ═════════════════════════════════════
if want прибор; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 2: delivery-latency.py — голос выпадает из К-1"
    cat > "$T/gen.py" <<'PYEOF'
import os, sys
lib, log_dir, base, voice = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4] == "1"
os.environ["HARNESS_LOG_DIR"] = log_dir
sys.path.insert(0, lib)
import events  # noqa: E402
D = "scripts/tg-dispatcher.sh"; P = "scripts/lib/pane.sh"
def put(src, ev, outcome, **data):
    if not events.emit(src, f"delivery.{ev}", outcome, journal="доставка", task="hr.proba",
                       data={k: str(v) for k, v in data.items()}):
        sys.exit(f"emit не записал {ev}")
for i in range(21):
    at = base + i * 7.123456789; ts = f"{at:.9f}"; uid = 9000 + i
    put(D, "received", "ok", update_id=uid, message_id=uid, ts=ts, tg_date=int(at), когда=ts)
    lag = 0.5
    if i == 20:
        lag = 20.0
        if voice:
            put(D, "attachment_started", "ok", message_id=uid, ts=ts, когда=f"{at + 0.05:.6f}")
            put(D, "attachment_done", "ok", message_id=uid, ts=ts, когда=f"{at + 19.5:.6f}")
    put(D, "queued", "ok", message_id=uid, ts=ts, когда=f"{at + lag - 0.4:.6f}")
    put(P, "kick_sent", "ok", ts_список=ts, когда=f"{at + lag:.6f}")
PYEOF
    BASE=$(( $(date +%s) - 2 * 86400 ))
    W_FROM=$(date -d "@$((BASE - 3600))" '+%F %T'); W_TO=$(date -d "@$((BASE + 3600))" '+%F %T')
    instr() {  # $1 = имя, $2 = голос 1/0 → «код|последняя строка»
        local X="$T/instr-$1" rc=0
        mkdir -p "$X/log" "$X/tr"
        python3 "$T/gen.py" "$KOREN/scripts/lib" "$X/log" "$BASE" "$2" > "$X/gen.out" 2>&1 \
            || { echo "gen-fail|$(tail -1 "$X/gen.out")"; return; }
        HARNESS_LOG_DIR="$X/log" LOG_DIR="$X/log" DELIVERY_TRANSCRIPTS_DIR="$X/tr" \
            timeout -k 2 120 python3 "$INSTRUMENT" --окно "$W_FROM" "$W_TO" --критерий К-1 > "$X/out" 2>&1 || rc=$?
        echo "$rc|$(grep -v '^[[:space:]]*$' "$X/out" | tail -1)"
    }
    verdict() { local rc="${1%%|*}" line="${1#*|}"; [ "$rc" = "$2" ] && [[ "$line" == *"$3"* ]] && echo да || echo "нет (код $rc: ${line:0:160})"; }
    R=$(instr voice 1)
    case_check "БОЛЬНОЙ К-1: 20 текстов по 0,5 с и голос с толчком через 20 с — ПРОЙДЕН, n=20" "да" \
        "$(verdict "$R" 0 'n=20')"
    R=$(instr text 0)
    case_check "К-1: то же сообщение без метки голоса — текст, ПРОВАЛ (максимум 20 с)" "да" "$(verdict "$R" 1 ПРОВАЛ)"
fi

# ═══ 3. КОД ════════════════════════════════════════════════════════════════
if want код; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 3: ключ предела памяти"
    case_check "harness.conf пакета: VOICE_VM_LIMIT_KB — число не меньше 1 500 000 (пол замера 28.10)" "да" \
        "$(v=$(sed -n 's/^VOICE_VM_LIMIT_KB="\{0,1\}\([0-9]*\)"\{0,1\}.*/\1/p' "$CONF_SEED" | head -1); [ -n "$v" ] && [ "$v" -ge 1500000 ] && echo да || echo "нет (${v:-ключа нет})")"
    # Предел — от памяти машины: доля MemTotal, не ниже пола и не выше потолка.
    # Одно число на все машины убило расшифровку на чужой (MKL берёт память по числу
    # ядер); вычисление судится на стендовых значениях MemTotal, а не на этой машине.
    VL="$T/vmlimit"; mkdir -p "$VL/log"
    printf 'PROJECT_DIR="%s"\nLOG_DIR="%s/log"\n' "$VL" "$VL" > "$VL/install.conf"
    printf 'VOICE_VM_LIMIT_KB="2500000"\nVOICE_VM_MEM_PERCENT="40"\nVOICE_VM_MAX_KB="6000000"\n' > "$VL/harness.conf"
    limit_for() {  # $1 = MemTotal стенда, КБ
        env -u VOICE_VM_LIMIT_KB HARNESS_INSTALL_CONF="$VL/install.conf" HARNESS_CONF="$VL/harness.conf" \
            timeout 20 bash "$KOREN/scripts/voice-worker.sh" --limit-for "$1" 2>&1 | tail -1
    }
    case_check "предел: память 2 ГБ — доля ниже пола, берётся пол 2 500 000" "2500000" "$(limit_for 2000000)"
    case_check "предел: память 8 ГБ — доля 40% = 3 200 000" "3200000" "$(limit_for 8000000)"
    case_check "предел: память 32 ГБ — доля выше потолка, берётся потолок 6 000 000" "6000000" "$(limit_for 32000000)"
    case_check "предел: память не прочиталась (0) — пол, а не ноль" "2500000" "$(limit_for 0)"
    case_check "harness.conf пакета: есть ключи доли и потолка предела расшифровки" "да" \
        "$(grep -q '^VOICE_VM_MEM_PERCENT=' "$CONF_SEED" && grep -q '^VOICE_VM_MAX_KB=' "$CONF_SEED" && echo да || echo нет)"
fi

echo "судимых путей: $PATHS_JUDGED"
[ "$CASES_FAILED" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: путей $PATHS_JUDGED, случаев $CASES_PATHS"; exit 0; }
echo "ПРОБА КРАСНАЯ: путей $PATHS_JUDGED, неудач $CASES_FAILED из $CASES_PATHS"; exit 1
