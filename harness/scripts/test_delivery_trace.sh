#!/usr/bin/env bash
#
# test_delivery_trace.sh — проба «принял» фоном и сверки «каждое сообщение имеет
# след» (спека docs/specs/2026-10-27-мгновенная-доставка-сообщений.md v2 §2, §4.1,
# §4.5, §4.6, §5; план docs/plans/2026-10-28-доставка-след.md; карточка
# hr.remove-message-interception-mechanism).
#
# ЧТО ОХРАНЯЕТ. (1) «Принял» — синхронный curl: медленный Bot API держит цикл
# приёма, и толчок сообщения ждёт ответа на подтверждение. (2) Метки доставки
# пишутся, но никто не спрашивал, у КАЖДОГО ли принятого сообщения есть конечная
# метка: путь, забывший метку, выглядел бы потерей только в К-5, и то лишь для
# толкаемых. Сверка судит все пути, а пустой журнал — ОТКАЗ, не зелёный.
#
# Пути (печатается их число):
#   1. диспетчер (tg-dispatcher.sh) целым циклом с подставным Bot API, который
#      отвечает на sendMessage с задержкой или рвёт связь: толчок не ждёт «принял»,
#      «принял» доходит, фоновый «принял» не держит лок диспетчера, отказ «принял»
#      не роняет цикл, живые пути (текст, команда, пауза) оставляют след;
#   2. прибор (delivery-latency.py --след) на журналах, записанных боевой дверью emit;
#   3. код: строка ворот зовёт сверку, проба стоит в воротах.
#
#   bash scripts/test_delivery_trace.sh [--случай диспетчер|прибор|код]
# Код возврата: 0 — зелёная, 1 — красная. Последняя строка — итог с числом путей.
# Механизмы берутся от места самой пробы (../ от scripts/): мутации идут «из-копии».
#
# Имена переменных ЛАТИНИЦЕЙ (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8

KOREN="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
DISPATCHER="$KOREN/scripts/tg-dispatcher.sh"
EVENTS_LIB="$KOREN/scripts/lib/events.py"
INSTRUMENT="$KOREN/scripts/delivery-latency.py"
PAUSE_PY="$KOREN/scripts/pause.py"
GATES="$KOREN/scripts/gates.sh"
for f in "$DISPATCHER" "$EVENTS_LIB" "$PAUSE_PY" "$INSTRUMENT" "$GATES"; do
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

T=$(mktemp -d /tmp/proba-delivery-trace-XXXXXX)
STAMP="proba-delivery-trace-$$"
PATHS_JUDGED=0
PIDS_KILL=()
cleanup() {
    local p
    for p in "${PIDS_KILL[@]}"; do kill -KILL -- "-$p" 2>/dev/null; kill -KILL "$p" 2>/dev/null; done
    pkill -KILL -f "^$STAMP" 2>/dev/null
    if [ -n "${PROBA_KEEP:-}" ]; then echo "стенд оставлен (PROBA_KEEP): $T"; else rm -rf "$T"; fi
}
trap cleanup EXIT

wait_for() {  # $1 = предел, остальное — условие
    local end=$(( $(date +%s) + $1 )); shift
    while [ "$(date +%s)" -lt "$end" ]; do "$@" && return 0; sleep 0.3; done
    "$@"
}
wait_quiet() {  # файл $1 не менялся $2 с (предел $3 с)
    local end=$(( $(date +%s) + $3 )) age
    while [ "$(date +%s)" -lt "$end" ]; do
        age=$(( $(date +%s) - $(stat -c %Y "$1" 2>/dev/null || echo 0) ))
        [ "$age" -ge "$2" ] && return 0
        sleep 0.5
    done
}

# ── подделка tmux: живой сервер не зовётся ─────────────────────────────────
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

# Сверка «первые метки» по журналу: поле data, не текст строки.
cat > "$T/marks.py" <<'PYEOF'
import json, sys

def rows(path, event):
    out = []
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("event") == f"delivery.{event}":
                out.append(r.get("data") or {})
    except OSError:
        pass
    return out

def rec_when(path, uid):
    xs = [float(d["когда"]) for d in rows(path, "received") if str(d.get("update_id")) == uid]
    return min(xs) if xs else None

def rec_ts(path, uid):
    xs = [str(d.get("ts")) for d in rows(path, "received") if str(d.get("update_id")) == uid]
    return xs[0] if xs else "нет"

def kick_delay(path, uid):
    """Секунды от received до kick_sent этого сообщения; нет метки — «нет»."""
    rec = rec_when(path, uid)
    ts = rec_ts(path, uid)
    kicks = [float(d["когда"]) for d in rows(path, "kick_sent")
             if ts in str(d.get("ts_список") or "").split(",")]
    if rec is None or not kicks:
        return "нет"
    return f"{min(kicks) - rec:.2f}"

def kicks(path):
    return str(len(rows(path, "kick_sent")))

cmd, args = sys.argv[1], sys.argv[2:]
print({"rec_when": rec_when, "rec_ts": rec_ts, "kick_delay": kick_delay, "kicks": kicks}[cmd](*args))
PYEOF
mk() { python3 "$T/marks.py" "$@"; }
# Имя случая постоянное (по нему мутация находит случай), значение — в ответе «нет (…)».
le() { python3 -c 'import sys; a=sys.argv[1]; print("да" if a != "нет" and float(a) <= float(sys.argv[2]) else f"нет ({a})")' "$1" "$2"; }

# ═══ 1. ДИСПЕТЧЕР: целым циклом, Bot API с задержкой «принял» ════════════════
if want диспетчер; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 1: tg-dispatcher.sh целым циклом — «принял» фоном"
    # sendMessage: «sleep» — ответ через заданные секунды, «drop» — связь рвётся без
    # ответа (curl падает, как при лежащей сети). Каждое обращение пишется С МОМЕНТОМ
    # ПРИХОДА: так судится, что «принял» ушло ≤ 30 с, а не только что дошло.
    # Сервер многопоточный: висящий sendMessage не держит getUpdates.
    cat > "$T/api.py" <<'PYEOF'
import json, sys, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
port, acks, ready, upd, mode, delay = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], float(sys.argv[6])
UPDATES = json.load(open(upd, encoding="utf-8"))
def update(u):
    return {"update_id": u["id"], "message": {"message_id": u["id"], "date": int(time.time()),
                                              "chat": {"id": 777000}, "text": u["text"]}}
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
            ids = [u for u in UPDATES if u["id"] >= off]
            if not ids:
                time.sleep(1)
            self.reply({"ok": True, "result": [update(u) for u in ids]}); return
        with open(acks, "a") as fh:
            fh.write(f"{time.time():.3f} " + (form.get("text") or [""])[0].replace("\n", " ") + "\n")
        if mode == "drop":
            self.close_connection = True; return
        time.sleep(delay)
        self.reply({"ok": True, "result": {"message_id": 1}})
    do_GET = do_POST
    def log_message(self, *_): pass
srv = ThreadingHTTPServer(("127.0.0.1", port), H)
srv.daemon_threads = True
open(ready, "w").close()
srv.serve_forever()
PYEOF
    D_STAND_BAD=0
    d_bad() { D_STAND_BAD=$((D_STAND_BAD + 1)); echo "  СТЕНД: $1"; }
    offset_is() { [ "$(cat "$1/log/tg_offset" 2>/dev/null)" = "$2" ]; }
    kicks_are() { [ "$(mk kicks "$1")" -ge "$2" ] 2>/dev/null; }
    lock_free() { flock -n "$1" true 2>/dev/null; }

    # $1 = имя, $2 = «id:текст;…», $3 = sleep|drop, $4 = задержка sendMessage, $5 = pause|"".
    # Диспетчер остаётся жить: остановка — d_stop (случаю (в) нужен живой «принял»).
    d_start() {
        local name="$1" spec="$2" mode="$3" delay="$4" flag="${5:-}" port
        DS="$T/disp-$name"; D_STAND_BAD=0
        mkdir -p "$DS/log/locks/services" "$DS/project" "$DS/panel"
        python3 - "$spec" "$DS/updates.json" <<'PYEOF'
import json, sys
items = [p.split(":", 1) for p in sys.argv[1].split(";") if p]
json.dump([{"id": int(i), "text": t} for i, t in items], open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
PYEOF
        D_LAST=$(python3 -c 'import json,sys; u=json.load(open(sys.argv[1])); print(max([x["id"] for x in u] or [0]))' "$DS/updates.json")
        : > "$DS/tmux-calls"; : > "$DS/acks"
        printf '000:СТЕНДОВЫЙ' > "$DS/token"   # не-секрет: подставной Bot API стенда
        port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
        python3 "$T/api.py" "$port" "$DS/acks" "$DS/api.ready" "$DS/updates.json" "$mode" "$delay" > "$DS/api.out" 2>&1 &
        PIDS_KILL+=($!); disown $!
        wait_for 10 test -e "$DS/api.ready" || { d_bad "Bot API не поднялся"; return; }
        cat > "$DS/install.conf" <<EOF
PROJECT_DIR="$DS/project"
PROJECT_NAME="стенд"
LOG_DIR="$DS/log"
TG_CHAT_ID="777000"
TG_TOKEN_FILE="$DS/token"
AGENT_USER="$(id -un)"
PANEL_STATE_DIR="$DS/panel"
EOF
        printf 'TMUX_SESSION="%s"\nSERVICE_LOCKS_SUBDIR="services"\nPANE_TMUX_TIMEOUT="5"\nPANE_ENTER_DELAY="0.1"\nPANE_SEND_NOW="0"\n' \
            "$STAMP-disp" > "$DS/harness.conf"
        if [ "$flag" = pause ]; then
            HARNESS_PANEL_STATE="$DS/panel" python3 "$PAUSE_PY" --поставить "проба: владелец остановил" --кто владелец >/dev/null
            HARNESS_PANEL_STATE="$DS/panel" python3 "$PAUSE_PY" --идёт-ли \
                || { echo "ПРОБА: pause.py не поставил паузу на стенде — проверять нечего"; exit 1; }
        fi
        stand_env env -u HARNESS_LOG_DIR PROBA_TMUX_LOG="$DS/tmux-calls" \
            HARNESS_PANEL_STATE="$DS/panel" \
            HARNESS_INSTALL_CONF="$DS/install.conf" HARNESS_CONF="$DS/harness.conf" \
            TG_API_BASE="http://127.0.0.1:$port" \
            setsid nohup bash "$DISPATCHER" > "$DS/dispatcher.out" 2>&1 < /dev/null &
        DPID=$!
        PIDS_KILL+=("$DPID"); disown "$DPID"
        DJ="$DS/log/доставка.jsonl"
        DLOCK="$DS/log/locks/services/tg-dispatcher.lock"
        # $! — обёртка: setsid вне группы-лидера форкается, и диспетчер живёт под
        # другим pid. Свой pid диспетчер пишет первой строкой лок-файла.
        wait_for 10 grep -qE '^[0-9]+$' "$DLOCK" || { d_bad "диспетчер не взял лок за 10 с"; return; }
        DPID=$(head -1 "$DLOCK")
        PIDS_KILL+=("$DPID")
    }
    # Разобрать всё и дать фоновым меткам лечь, потом погасить группу.
    d_finish() {  # $1 = предел ожидания offset, с
        wait_for "$1" offset_is "$DS" $((D_LAST + 1)) \
            || { d_bad "диспетчер не разобрал update $D_LAST за $1 с"; tail -5 "$DS/log/tg-dispatcher.log" 2>/dev/null | sed 's/^/    /'; }
        sleep 2
        wait_quiet "$DS/tmux-calls" 2 20
        wait_quiet "$DJ" 2 15
        kill -KILL -- "-$DPID" 2>/dev/null; kill -KILL "$DPID" 2>/dev/null
    }
    d_reached() { case_check "СТЕНД ($1): диспетчер разобрал все сообщения" "0" "$D_STAND_BAD"; }

    # (а) БОЛЬНОЙ: «принял» висит 8 с — толчок каждого из двух сообщений ≤ 4 с от
    # его received (спека К-1: максимум 4 с). Синхронный «принял» даёт ≥ 8 с.
    echo "— (а) sendMessage отвечает через 8 с, два текста одним пакетом"
    d_start slow "901:первое сообщение пробы про карту;902:второе сообщение пробы про панель" sleep 8
    wait_for 40 kicks_are "$DJ" 2 || true
    d_finish 40; d_reached "медленный «принял»"
    D901=$(mk kick_delay "$DJ" 901); D902=$(mk kick_delay "$DJ" 902)
    case_check "БОЛЬНОЙ (а): received→kick_sent первого ≤ 4 с при «принял» 8 с" "да" "$(le "$D901" 4)"
    case_check "БОЛЬНОЙ (а): received→kick_sent второго ≤ 4 с при «принял» 8 с" "да" "$(le "$D902" 4)"
    # (б) «принял» всё равно уходит, и не позже 30 с от приёма (правило канала 1).
    for pair in "901:первое сообщение пробы" "902:второе сообщение пробы"; do
        uid="${pair%%:*}"; words="${pair#*:}"
        rec=$(mk rec_when "$DJ" "$uid")
        got=$(python3 - "$DS/acks" "$words" "${rec:-None}" <<'PYEOF'
import sys
path, words, rec = sys.argv[1], sys.argv[2], sys.argv[3]
hits = []
for line in open(path, encoding="utf-8", errors="replace"):
    at, _, text = line.rstrip("\n").partition(" ")
    if text.startswith("принял: ") and words in text:
        hits.append(float(at))
if rec == "None" or not hits:
    print("нет")
else:
    print("да" if 0 <= min(hits) - float(rec) <= 30 else f"через {min(hits) - float(rec):.1f} с")
PYEOF
)
        case_check "(б) «принял: …» на $uid дошло до Bot API ≤ 30 с от received" "да" "$got"
    done
    case_check "(б) «принял» по одному на сообщение (всего 2)" "2" \
        "$(grep -c ' принял: ' "$DS/acks" 2>/dev/null || echo 0)"

    # (в) фоновый «принял» не держит лок диспетчера: диспетчер убит (только он, не
    # группа), пока «принял» висит в Bot API, — лок свободен для нового диспетчера.
    echo "— (в) диспетчер убит, пока «принял» висит"
    d_start lock "911:сообщение пробы про лок" sleep 12
    wait_for 8 kicks_are "$DJ" 1 || d_bad "толчка нет за 8 с — «принял» держит цикл"
    kill -KILL "$DPID" 2>/dev/null
    sleep 0.3
    HANGING=$(python3 -c 'import sys,time; xs=[float(l.split()[0]) for l in open(sys.argv[1]) if " принял: " in l]; print("да" if xs and time.time() - min(xs) < 11 else "нет")' "$DS/acks" 2>/dev/null || echo нет)
    if wait_for 4 lock_free "$DLOCK"; then FREE=да; else FREE=нет; fi
    case_check "СТЕНД (в): «принял» ещё висит в Bot API в момент проверки лока" "да" "$HANGING"
    case_check "БОЛЬНОЙ (в): после смерти диспетчера лок свободен, хотя «принял» висит" "да" "$FREE"
    kill -KILL -- "-$DPID" 2>/dev/null

    # (г) «принял» не ушёл (связь рвётся): цикл жив, толчок отдан, отказ назван в журнале.
    echo "— (г) sendMessage рвёт связь"
    d_start drop "921:сообщение пробы при лежащем канале" drop 0
    d_finish 30; d_reached "лежащий канал"
    case_check "(г) толчок отдан, хотя «принял» не ушёл" "да" "$(le "$(mk kick_delay "$DJ" 921)" 4)"
    case_check "(г) в журнале диспетчера «ack не ушёл»" "да" \
        "$(grep -qF 'ack не ушёл' "$DS/log/tg-dispatcher.log" 2>/dev/null && echo да || echo нет)"

    # (д) живые пути оставляют след: текст, команда, текст на паузе — сверка ПРОЙДЕН.
    echo "— (д) след живых путей: текст и команда, затем пауза"
    trace_on() {  # $1 = каталог журнала; печатает «код|последняя строка»
        local rc=0
        HARNESS_LOG_DIR="$1" LOG_DIR="$1" timeout -k 2 60 python3 "$INSTRUMENT" --след \
            --окно "$(date -d '-1 hour' '+%F %T')" "$(date -d '+1 hour' '+%F %T')" --зазор 0 \
            > "$1/trace.out" 2>&1 || rc=$?
        echo "$rc|$(grep -v '^[[:space:]]*$' "$1/trace.out" | tail -1)"
    }
    d_start live "931:почини кнопку обновления в панели;932:статус" sleep 0
    d_finish 40; d_reached "текст и команда"
    R=$(trace_on "$DS/log")
    case_check "(д) текст и команда: сверка ПРОЙДЕН, код 0, n=2" "0|n=2|ПРОЙДЕН" \
        "${R%%|*}|$(grep -oE 'n=[0-9]+' <<< "${R#*|}")|$(grep -oE 'ПРОЙДЕН|ПРОВАЛ|ОТКАЗ' <<< "${R#*|}")"
    d_start paused "941:почини кнопку обновления в панели" sleep 0 pause
    d_finish 40; d_reached "пауза"
    R=$(trace_on "$DS/log")
    case_check "(д) текст на паузе: сверка ПРОЙДЕН, код 0, n=1" "0|n=1|ПРОЙДЕН" \
        "${R%%|*}|$(grep -oE 'n=[0-9]+' <<< "${R#*|}")|$(grep -oE 'ПРОЙДЕН|ПРОВАЛ|ОТКАЗ' <<< "${R#*|}")"
fi

# ═══ 2. ПРИБОР: --след на журналах, записанных боевой дверью emit ═══════════
if want прибор; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 2: delivery-latency.py --след"
    # Сообщение: возраст от «сейчас» (с) и метки, которые за ним лежат. Значения
    # строками — как их кладёт bash-дверь harness_event.
    cat > "$T/gen.py" <<'PYEOF'
import json, os, sys, time
lib, log_dir, spec_path = sys.argv[1], sys.argv[2], sys.argv[3]
os.environ["HARNESS_LOG_DIR"] = log_dir
sys.path.insert(0, lib)
import events  # noqa: E402
now = time.time()
D = "scripts/tg-dispatcher.sh"; P = "scripts/lib/pane.sh"
def put(src, ev, outcome, **data):
    if not events.emit(src, f"delivery.{ev}", outcome, journal="доставка",
                       task="hr.proba", data={k: str(v) for k, v in data.items()}):
        sys.exit(f"emit не записал {ev}")
for m in json.load(open(spec_path, encoding="utf-8")):
    # Сдвиг по uid: сообщения одного возраста иначе получили бы один ts.
    at = now - m["age"] + m["uid"] * 0.001
    ts = m.get("ts") or f"{at:.9f}"
    marks = m["marks"]
    put(D, "received", "ok", update_id=m["uid"], message_id=m["uid"], ts=ts, tg_date=int(at), когда=f"{at:.6f}")
    if "queued" in marks:
        put(D, "queued", "ok", message_id=m["uid"], ts=ts, когда=f"{at + 0.05:.6f}")
    if "started" in marks:
        put(D, "attachment_started", "ok", message_id=m["uid"], ts=ts, когда=f"{at + 0.05:.6f}")
    if "done" in marks:
        put(D, "attachment_done", "ok", message_id=m["uid"], ts=ts, когда=f"{now - m['done_age']:.6f}")
    if "no_kick" in marks:
        put(D, "no_kick", "skipped", ts=ts, почему="команда status")
    if "refused" in marks:
        put(P, "kick_refused", "held", ts_список=ts, почему=m.get("why", "пауза"), когда=f"{at + 0.1:.6f}")
    if "kick" in marks:
        put(P, "kick_sent", "ok", ts_список=",".join(m.get("cover", [ts])), когда=f"{at + 0.5:.6f}")
PYEOF
    W_FROM=$(date -d '-1 hour' '+%F %T'); W_TO=$(date -d '+1 hour' '+%F %T')
    # $1 = имя, $2 = JSON-список сообщений, дальше доводы прибора. Печатает «код|последняя строка».
    run_trace() {
        local name="$1" spec="$2" X rc=0; shift 2
        X="$T/tr-$name"; mkdir -p "$X/log"
        printf '%s' "$spec" > "$X/spec.json"
        python3 "$T/gen.py" "$KOREN/scripts/lib" "$X/log" "$X/spec.json" > "$X/gen.out" 2>&1 \
            || { echo "gen-fail|$(tail -1 "$X/gen.out")"; return; }
        HARNESS_LOG_DIR="$X/log" LOG_DIR="$X/log" timeout -k 2 60 python3 "$INSTRUMENT" "$@" > "$X/out" 2>&1 || rc=$?
        echo "$rc|$(grep -v '^[[:space:]]*$' "$X/out" | tail -1)"
    }
    verdict() {  # $1 = «код|строка», $2 = ждём код, $3 = ждём слово вердикта
        local rc="${1%%|*}" line="${1#*|}"
        [ "$rc" = "$2" ] && [[ "$line" == *"$3"* ]] && [[ "$line" == *"ВЕРДИКТ след"* ]] && echo да || echo "нет (код $rc: ${line:0:160})"
    }
    nval() { grep -oE 'n=[0-9]+' <<< "${1#*|}" | head -1; }
    has() { grep -qF -- "$2" "$T/tr-$1/out" 2>/dev/null && echo да || echo нет; }
    W=(--окно "$W_FROM" "$W_TO")

    R=$(run_trace empty '[]' --след "${W[@]}")
    case_check "БОЛЬНОЙ: пустой журнал — ОТКАЗ, код 2" "да" "$(verdict "$R" 2 ОТКАЗ)"

    R=$(run_trace three '[{"uid":1,"age":600,"marks":["queued","kick"]},{"uid":2,"age":600,"marks":["no_kick"]},{"uid":3,"age":600,"marks":["queued","refused"],"why":"не claude"}]' --след "${W[@]}")
    case_check "kick_sent, no_kick, kick_refused (любая причина) — ПРОЙДЕН, код 0" "да" "$(verdict "$R" 0 ПРОЙДЕН)"
    case_check "три сообщения с конечной меткой — n=3" "n=3" "$(nval "$R")"

    GAP_TS="1793100000.123456789"
    R=$(run_trace gap "[{\"uid\":1,\"age\":600,\"marks\":[\"queued\",\"kick\"]},{\"uid\":7,\"age\":600,\"ts\":\"$GAP_TS\",\"marks\":[]}]" --след "${W[@]}")
    case_check "БОЛЬНОЙ: received без конечной метки — ПРОВАЛ, код 1" "да" "$(verdict "$R" 1 ПРОВАЛ)"
    case_check "провал называет ts сообщения без следа" "да" "$(has gap "$GAP_TS")"
    case_check "провал печатает «без следа: 1»" "да" "$(has gap 'без следа: 1')"

    R=$(run_trace queued-only '[{"uid":1,"age":600,"marks":["queued"]}]' --след "${W[@]}")
    case_check "только queued (толчка нет) — ПРОВАЛ, код 1" "да" "$(verdict "$R" 1 ПРОВАЛ)"

    R=$(run_trace fresh '[{"uid":1,"age":600,"marks":["queued","kick"]},{"uid":2,"age":10,"marks":["queued"]}]' --след "${W[@]}")
    case_check "свежее без конечной (10 с, зазор 120) — в пути, ПРОЙДЕН" "да" "$(verdict "$R" 0 ПРОЙДЕН)"
    case_check "свежее печатается «в пути: 1»" "да" "$(has fresh 'в пути: 1')"
    R=$(run_trace fresh-gap '[{"uid":2,"age":30,"marks":["queued"]}]' --след "${W[@]}" --зазор 5)
    case_check "--зазор 5: то же сообщение 30 с — ПРОВАЛ" "да" "$(verdict "$R" 1 ПРОВАЛ)"
    R=$(run_trace fresh-only '[{"uid":2,"age":10,"marks":["queued"]}]' --след "${W[@]}")
    case_check "только сообщения в пути — судить нечего, ОТКАЗ, код 2" "да" "$(verdict "$R" 2 ОТКАЗ)"

    R=$(run_trace voice-work '[{"uid":1,"age":600,"marks":["queued","kick"]},{"uid":5,"age":300,"marks":["started"]}]' --след "${W[@]}")
    case_check "голосовое 5 мин без итога — в работе, ПРОЙДЕН" "да" "$(verdict "$R" 0 ПРОЙДЕН)"
    R=$(run_trace voice-lost '[{"uid":5,"age":1200,"marks":["started"]}]' --след "${W[@]}")
    case_check "БОЛЬНОЙ: голосовое 20 мин без итога — ПРОВАЛ, код 1" "да" "$(verdict "$R" 1 ПРОВАЛ)"
    R=$(run_trace voice-nokick '[{"uid":5,"age":1200,"marks":["started","done","queued"],"done_age":600}]' --след "${W[@]}")
    case_check "голосовое: итог 10 мин назад, толчка нет — ПРОВАЛ" "да" "$(verdict "$R" 1 ПРОВАЛ)"
    R=$(run_trace voice-ok '[{"uid":5,"age":1200,"marks":["started","done","queued","kick"],"done_age":600}]' --след "${W[@]}")
    case_check "голосовое: итог и толчок — ПРОЙДЕН" "да" "$(verdict "$R" 0 ПРОЙДЕН)"

    R=$(run_trace glue '[{"uid":1,"age":600,"ts":"1793100001.1","marks":["queued"]},{"uid":2,"age":600,"ts":"1793100002.2","marks":["queued","kick"],"cover":["1793100001.1","1793100002.2"]}]' --след "${W[@]}")
    case_check "склейка: один kick_sent с ts_список покрывает оба — ПРОЙДЕН, n=2" "да|n=2" "$(verdict "$R" 0 ПРОЙДЕН)|$(nval "$R")"

    R=$(run_trace reparse '[{"uid":8,"age":600,"ts":"1793100003.3","marks":[]},{"uid":8,"age":590,"ts":"1793100004.4","marks":["queued","kick"]}]' --след "${W[@]}")
    case_check "повторный разбор: два received одного update_id, след у второго — ПРОЙДЕН, n=1" "да|n=1" "$(verdict "$R" 0 ПРОЙДЕН)|$(nval "$R")"

    R=$(run_trace outside '[{"uid":1,"age":600,"marks":["queued","kick"]},{"uid":9,"age":10800,"marks":[]}]' --след "${W[@]}")
    case_check "received вне окна не судится — ПРОЙДЕН, n=1" "да|n=1" "$(verdict "$R" 0 ПРОЙДЕН)|$(nval "$R")"

    echo "— режим ворот (--ворота: 7 суток до «сейчас», ОТКАЗ — жёлтый 77)"
    R=$(run_trace g-empty '[]' --след --ворота)
    case_check "БОЛЬНОЙ --ворота: пустой журнал — код 77 (жёлтый), не 0" "77" "${R%%|*}"
    case_check "--ворота: пустой журнал называет ОТКАЗ" "да" "$(has g-empty 'ОТКАЗ')"
    R=$(run_trace g-gap '[{"uid":7,"age":86400,"marks":[]}]' --след --ворота)
    case_check "--ворота: сутки назад без следа — код 1 (красный)" "1" "${R%%|*}"
    R=$(run_trace g-old '[{"uid":7,"age":777600,"marks":[]}]' --след --ворота)
    case_check "--ворота: 9 суток назад — вне окна 7 суток, код 77" "77" "${R%%|*}"
    R=$(run_trace g-ok '[{"uid":1,"age":86400,"marks":["queued","kick"]}]' --след --ворота)
    case_check "--ворота: сутки назад с толчком — код 0" "0" "${R%%|*}"
fi

# ═══ 3. КОД: сверка и проба в воротах ════════════════════════════════════
if want код; then
    PATHS_JUDGED=$((PATHS_JUDGED + 1))
    echo "═══ путь 3: ворота"
    # Строка ворот разбирается как ворота её читают: «проверка "<имя>" <команда>».
    gate_has() {
        python3 - "$GATES" "$1" <<'PYEOF'
import re, shlex, sys
want = sys.argv[2].split()
for line in open(sys.argv[1], encoding="utf-8"):
    if not re.match(r"^проверка\s", line):
        continue
    try:
        words = shlex.split(line, comments=True)
    except ValueError:
        continue
    cmd = words[2:]
    if all(w in cmd for w in want):
        print("да"); sys.exit()
print("нет")
PYEOF
    }
    case_check "ворота зовут сверку: delivery-latency.py --след --ворота" "да" \
        "$(gate_has "scripts/delivery-latency.py --след --ворота")"
    case_check "ворота зовут эту пробу" "да" "$(gate_has "scripts/test_delivery_trace.sh")"
fi

echo "судимых путей: $PATHS_JUDGED"
[ "$CASES_FAILED" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: путей $PATHS_JUDGED, случаев $CASES_PATHS"; exit 0; }
echo "ПРОБА КРАСНАЯ: путей $PATHS_JUDGED, неудач $CASES_FAILED из $CASES_PATHS"; exit 1
