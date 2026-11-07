#!/usr/bin/env bash
#
# test_pane_kick_confirm.sh — проба подтверждения пинка владельца снимком экрана
# (scripts/lib/pane.sh: pane_confirm_kick, pane_tail_on_screen; карточка
# hr.owner-messages-delivery-chaos).
#
# ЧТО ОХРАНЯЕТ. Замер 03.11.2026: пинки о сообщениях владельца лежали в строке
# ввода claude до 43 минут и всплывали дублем, а метка kick_sent говорила «отдан»
# — код возврата tmux значит лишь «клавиши дошли до панели». Дверь теперь смотрит
# на экран после Enter: иголка «<ts>.txt» в области ввода — один повторный Enter;
# не ушло и со второго — событие kick_stuck (метка sent всё равно ставится, чтобы
# сторож не склеил второй экземпляр в ту же строку). Строку ввода дверь не чистит
# никогда: там может быть набор человека.
#
# Случаи (печатается их число):
#   accept          — Enter принят с первого раза: один Enter, kick_sent;
#   eat-first       — первый Enter съеден: два Enter, kick_sent;
#   never           — Enter не принимается: kick_stuck, метка sent есть,
#                     ни Ctrl+U, ни C-c, ни Escape в панель не послано;
#   echo-dispatcher — пинок в виде диспетчера, ниже строки ввода — прошлый пинок
#                     с другим ts и тем же хвостом «…/обработано/.»: один Enter;
#   never-wrapped   — путь в строке ввода разорван переносом с рамкой: kick_stuck;
#   PANE_CONFIRM=0  — подтверждение выключено (на «never»): один Enter, kick_sent;
#   capture-pane падает (на «never») — fail-open: один Enter, kick_sent.
#
# Стенд: свой TMUX_TMPDIR (свой сервер tmux, живой не зовётся), TMUX снята,
# LOG_DIR и каталог паузы свои. В панели — фальшивая «claude»: python под именем
# node (дверь шлёт текст только в claude/node), рисует строку ввода «> …» и пишет
# в журнал каждый Enter, Ctrl+U, C-c и Escape. Вызовов модели нет.
#
#   bash scripts/test_pane_kick_confirm.sh
# Код возврата: 0 — зелёная, 1 — красная. Последняя строка — итог с числом случаев.
# Дверь берётся от места самой пробы (../ от scripts/): мутации идут «из-копии».
set -uo pipefail
export LC_ALL=C.UTF-8

KOREN="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
PANE_LIB="$KOREN/scripts/lib/pane.sh"
[ -r "$PANE_LIB" ] || { echo "ПРОБА: нет механизма $PANE_LIB — проверять нечего"; exit 1; }
command -v tmux >/dev/null 2>&1 || { echo "ПРОБА: нет tmux — проверять нечего"; exit 1; }
source "$KOREN/scripts/lib/selftest_cases.sh"

T=$(mktemp -d)
export TMUX_TMPDIR="$T/tmux"
mkdir -p "$TMUX_TMPDIR" "$T/bin" "$T/broken-capture"
unset TMUX
cleanup() { tmux kill-server >/dev/null 2>&1; rm -rf "$T"; }
trap cleanup EXIT

ln -s "$(command -v python3)" "$T/bin/node"
REAL_TMUX="$(command -v tmux)"
# tmux, у которого снимок экрана падает: остальное — настоящему.
cat > "$T/broken-capture/tmux" <<EOF
#!/usr/bin/env bash
[ "\${1:-}" = capture-pane ] && exit 1
exec "$REAL_TMUX" "\$@"
EOF
chmod +x "$T/broken-capture/tmux"

cat > "$T/fakeclaude.py" <<'PYEOF'
import os, sys, tty
mode, keylog, acclog, old = sys.argv[1:5]
fd = sys.stdin.fileno()
tty.setraw(fd)
buf, hist, enters = "", [], 0
# Прошлый пинок диспетчера: другой ts, тот же хвост; стоит НИЖЕ строки ввода.
queued = ("  [в очереди] Владелец написал: старое — полная версия в " + old
          + ", прочитай и перенеси в " + os.path.dirname(old) + "/обработано/.")
wrap = 37 if mode == "never-wrapped" else 0
def line():
    if not wrap:
        return "> " + buf
    parts = [buf[i:i + wrap] for i in range(0, len(buf), wrap)] or [""]
    return "\r\n".join(("│ > " if i == 0 else "│   ") + p for i, p in enumerate(parts))
def draw():
    out = "\x1b[2J\x1b[H"
    if mode not in ("echo-dispatcher", "never-wrapped"):
        out += "".join("  " + h[:70] + "\r\n" for h in hist[-4:])
    out += "\r\n---------------\r\n" + line() + "\r\n"
    if mode in ("echo-dispatcher", "never-wrapped"):
        out += queued + "\r\n"
    out += "---------------\r\n  ? for shortcuts\r\n"
    sys.stdout.write(out); sys.stdout.flush()
def note(word):
    with open(keylog, "a") as fh:
        fh.write(word + "\n")
draw()
while True:
    data = os.read(fd, 4096).decode("utf-8", "replace")
    if not data:
        break
    for c in data:
        if c in "\r\n":
            enters += 1
            note("ENTER")
            if mode in ("never", "never-wrapped") or (mode == "eat-first" and enters == 1):
                continue
            with open(acclog, "a") as fh:
                fh.write(buf + "\n")
            hist.append("> " + buf); buf = ""
        elif c == "\x15": note("CTRL-U"); buf = ""
        elif c == "\x03": note("CTRL-C")
        elif c == "\x1b": note("ESCAPE")
        elif c == "\x7f": buf = buf[:-1]
        else: buf += c
    draw()
PYEOF

# Один случай: своя сессия, свой LOG_DIR; итог — строка «ключ=значение» в $W/result.
run_case() {  # имя случая, режим фальшивки, вид пинка (short|dispatcher), доп. окружение…
    local name="$1" mode="$2" kind="$3"; shift 3
    local W="$T/$name" ts=1762000000.123456 kick i
    mkdir -p "$W/log/inbox" "$W/panel"
    : > "$W/keys"; : > "$W/accepted"
    (
        export LOG_DIR="$W/log" HARNESS_PANEL_STATE="$W/panel" TMUX_SESSION="$name"
        # Аккорд C-x C-s не нужен пробе: судится только Enter.
        export PANE_ENTER_DELAY=0.3 PANE_CONFIRM_DELAY=0.6 PANE_SEND_NOW=0
        for kv in "$@"; do export "${kv?}"; done
        echo "сообщение владельца для пробы подтверждения пинка" > "$LOG_DIR/inbox/$ts.txt"
        "$REAL_TMUX" new-session -d -s "$name" -x 100 -y 30 \
            "$T/bin/node $T/fakeclaude.py $mode $W/keys $W/accepted $LOG_DIR/inbox/1761999999.000001.txt"
        for i in $(seq 50); do
            "$REAL_TMUX" capture-pane -p -t "$name" 2>/dev/null | grep -q 'for shortcuts' && break
            sleep 0.1
        done
        source "$PANE_LIB"
        if [ "$kind" = dispatcher ]; then
            kick="Владелец написал: проверь почту — полная версия в $LOG_DIR/inbox/$ts.txt, прочитай и перенеси в $LOG_DIR/inbox/обработано/."
        else
            kick="Новое сообщение владельца: $LOG_DIR/inbox/$ts.txt"
        fi
        pane_kick_owner "$kick"; local rc=$?
        sleep 0.5
        {
            echo "rc=$rc"
            echo "enters=$(grep -c '^ENTER$' "$W/keys" 2>/dev/null)"
            echo "accepted=$(grep -c . "$W/accepted" 2>/dev/null)"
            echo "wipes=$(grep -cE '^(CTRL-U|CTRL-C|ESCAPE)$' "$W/keys" 2>/dev/null)"
            echo "sent=$([ -e "$LOG_DIR/kick-state/sent-$ts" ] && echo да || echo нет)"
            echo "events=$(python3 - "$LOG_DIR/доставка.jsonl" <<'EVEOF'
import json, sys
try:
    rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
except OSError:
    rows = []
print(",".join(r.get("event", "?") for r in rows) or "нет")
EVEOF
)"
        } > "$W/result"
        "$REAL_TMUX" kill-session -t "$name" >/dev/null 2>&1
    )
}

field() { sed -n "s/^$2=//p" "$T/$1/result" 2>/dev/null; }

judge() {  # имя, ждём Enter, ждём событие, ждём принятых
    local name="$1"
    echo "═══ $name"
    [ -s "$T/$name/result" ] || { case_check "$name: случай отработал" "да" "нет"; return; }
    case_check "$name: код двери" "0" "$(field "$name" rc)"
    case_check "$name: Enter в панель" "$2" "$(field "$name" enters)"
    case_check "$name: событие доставки" "$3" "$(field "$name" events)"
    case_check "$name: строк принято панелью" "$4" "$(field "$name" accepted)"
    case_check "$name: метка sent стоит" "да" "$(field "$name" sent)"
    case_check "$name: строку ввода не чистили (Ctrl+U/C-c/Escape)" "0" "$(field "$name" wipes)"
}

run_case accept accept short
run_case eat-first eat-first short
run_case never never short
run_case echo-dispatcher echo-dispatcher dispatcher
run_case never-wrapped never-wrapped dispatcher
run_case confirm-off never short PANE_CONFIRM=0
run_case capture-broken never short "PATH=$T/broken-capture:$PATH"

judge accept 1 delivery.kick_sent 1
judge eat-first 2 delivery.kick_sent 1
judge never 2 delivery.kick_stuck 0
judge echo-dispatcher 1 delivery.kick_sent 1
judge never-wrapped 2 delivery.kick_stuck 0
judge confirm-off 1 delivery.kick_sent 0
judge capture-broken 1 delivery.kick_sent 0

[ "$CASES_FAILED" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: всего $CASES_PATHS, неудач: 0"; exit 0; }
echo "ПРОБА КРАСНАЯ: всего $CASES_PATHS, неудач: $CASES_FAILED"; exit 1
