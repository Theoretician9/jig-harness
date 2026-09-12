#!/usr/bin/env bash
#
# proba-vhod-bez-sboya.sh — живая проба: входящее не сбивает шаг агента.
#
# Правило проекта: проба обязана звать механизм ТАК ЖЕ, как боевой путь. Поэтому
# здесь работает НАСТОЯЩИЙ tg-dispatcher.sh — со своим циклом getUpdates, своей
# укладкой в очередь и своим пинком в tmux. Подставлено ровно три вещи, и все
# три названы:
#   • Bot API — свой HTTP-сервер на localhost (TG_API_BASE), он отдаёт два
#     заготовленных сообщения владельца: срочное и несрочное;
#   • паспорт установки и пороги — свои файлы (HARNESS_INSTALL_CONF, HARNESS_CONF):
#     свой LOG_DIR, свой PROJECT_DIR, своя tmux-сессия. Боевых каталогов проба
#     не касается;
#   • вход к модели — свой скрипт (HARNESS_SPROSIT_MODEL): разбор смысла тут не
#     проверяется, проверяется, что карточку заводит КОД и она ложится в карту.
#
# Что доказывается (больной случай первым):
#   1. БОЛЬНОЙ СЛУЧАЙ: несрочное сообщение НЕ пинает панель агента;
#   2. срочное пинает её в тот же миг, как раньше;
#   3. оба сообщения легли в очередь канала файлами — ничего не потеряно;
#   4. карточка из несрочного заведена КОДОМ, со ссылкой на сообщение;
#   5. свод рубежа показывает накопленное одним сортированным списком.
#
#   bash scripts/proba-vhod-bez-sboya.sh [--диспетчер <путь>]
# Код возврата: 0 — зелёная, 1 — красная.
#
# Имена переменных ЛАТИНИЦЕЙ: кириллические bash не берёт и печатает в ошибку
# само значение (память: кириллица-в-именах-bash).
set -uo pipefail
export LC_ALL=C.UTF-8

KOREN="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
DISPATCHER="$KOREN/scripts/tg-dispatcher.sh"
[ "${1:-}" = "--диспетчер" ] && DISPATCHER="$2"

T=$(mktemp -d /tmp/proba-vhod-XXXXXX)
SESSION="proba-vhod-$$"
TOKEN="000:СТЕНДОВЫЙ"   # не-секрет: подставной Bot API стенда, боевого токена здесь нет
ok=1
API_PID=""
DISP_PID=""

itog() {  # ожидание, факт, имя случая
    if [ "$2" = "$1" ]; then printf '  ок    %s\n' "$3"
    else printf '  ПЛОХО %s: ждали «%s», получили «%s»\n' "$3" "$1" "$2"; ok=0; fi
}

pribrat() {
    [ -n "$DISP_PID" ] && kill "$DISP_PID" 2>/dev/null
    [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null
    tmux kill-session -t "$SESSION" 2>/dev/null
    rm -rf "$T"
}
trap pribrat EXIT

command -v tmux >/dev/null 2>&1 || { echo "ПРОБА: нет tmux — доставку проверять нечем"; exit 1; }

mkdir -p "$T/log" "$T/project/harness/config"
cp "$KOREN/dev-map.yaml" "$T/project/dev-map.yaml"
cp "$KOREN/harness/config/очередь.yaml" "$T/project/harness/config/очередь.yaml"
printf '%s' "$TOKEN" > "$T/token"

# ── подставной Bot API: отдаёт два сообщения владельца один раз ─────────────
cat > "$T/api.py" <<'PYEOF'
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

ОТДАНО = {"было": False}
НЕСРОЧНОЕ = "Надо добавить в карту экран памяти и переделать раскладку панели"
СРОЧНОЕ = "срочно останови выкат"


def обновления():
    if ОТДАНО["было"]:
        time.sleep(1)      # long-poll: не крутим цикл диспетчера впустую
        return []
    ОТДАНО["было"] = True
    return [
        {"update_id": 1, "message": {"message_id": 11, "chat": {"id": 777000},
                                     "text": НЕСРОЧНОЕ}},
        {"update_id": 2, "message": {"message_id": 12, "chat": {"id": 777000},
                                     "text": СРОЧНОЕ}},
    ]


class Рука(BaseHTTPRequestHandler):
    def ответить(self):
        если_updates = self.path.endswith("/getUpdates")
        тело = ({"ok": True, "result": обновления()} if если_updates
                else {"ok": True, "result": {"message_id": 1}})
        сырое = json.dumps(тело).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(сырое)))
        self.end_headers()
        self.wfile.write(сырое)

    def do_GET(self):
        self.ответить()

    def do_POST(self):
        длина = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(длина)
        self.ответить()

    def log_message(self, *_):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), Рука).serve_forever()
PYEOF

PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
python3 "$T/api.py" "$PORT" & API_PID=$!
sleep 1

# ── подставной вход к модели: отдаёт данные по схеме повода ─────────────────
cat > "$T/model.py" <<'PYEOF'
import json
print(json.dumps({"задачи": ["добавить экран памяти"], "указания": [],
                  "вопросы": []}, ensure_ascii=False))
PYEOF

# ── паспорт стенда: боевых каталогов проба не касается ─────────────────────
cat > "$T/install.conf" <<EOF
PROJECT_DIR="$T/project"
PROJECT_NAME="стенд"
LOG_DIR="$T/log"
TG_CHAT_ID="777000"
TG_TOKEN_FILE="$T/token"
AGENT_USER="$(id -un)"
EOF
cat > "$T/harness.conf" <<EOF
TMUX_SESSION="$SESSION"
SERVICE_LOCKS_SUBDIR="services"
ACTIVE_LINE="harness"
EOF

# ── приёмник вместо панели агента: пинок идёт только в claude/node ─────────
# `exec -a node` — чтобы pane_current_command был «node»: диспетчер намеренно
# не пишет в голую оболочку (она исполнила бы текст как команды).
tmux new-session -d -s "$SESSION" "bash -c 'exec -a node cat > $T/панель'"
sleep 0.5

HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
HARNESS_SPROSIT_MODEL="$T/model.py" TG_API_BASE="http://127.0.0.1:$PORT" \
    setsid nohup bash "$DISPATCHER" > "$T/dispatcher.log" 2>&1 < /dev/null &
DISP_PID=$!

# Ждём по ПРИЗНАКУ, а не по времени: два файла в очереди канала.
for _ in $(seq 1 60); do
    [ "$(find "$T/log/inbox" -maxdepth 1 -name '*.txt' 2>/dev/null | wc -l)" -ge 2 ] && break
    sleep 0.5
done
sleep 4          # пинок доезжает до панели, карточка успевает лечь в карту

LEZHIT=$(find "$T/log/inbox" -maxdepth 1 -name '*.txt' 2>/dev/null | wc -l | tr -d ' ')
itog "2" "$LEZHIT" "оба сообщения легли в очередь канала файлами"

tmux send-keys -t "$SESSION" C-d 2>/dev/null
sleep 0.5
PANEL=$(cat "$T/панель" 2>/dev/null || true)

# 1. БОЛЬНОЙ СЛУЧАЙ: несрочное не имеет права прерывать шаг.
got=нет; printf '%s' "$PANEL" | grep -q 'экран памяти' && got=да
itog "нет" "$got" "БОЛЬНОЙ СЛУЧАЙ: несрочное сообщение НЕ пинает панель агента"

# 2. Срочное будит сразу — эту половину ломать нельзя.
got=нет; printf '%s' "$PANEL" | grep -q 'останови выкат' && got=да
itog "да" "$got" "срочное пинает панель в тот же миг"

# 3. Решение записано в журнал канала словами — видимость, а не догадка.
got=нет; grep -q 'несрочное — шаг агента не прерываю' "$T/log/tg-dispatcher.log" && got=да
itog "да" "$got" "в журнале канала названа причина, почему пинка не было"

# 4. Карточку завёл КОД: ссылка на сообщение стоит в карте стенда.
METKA=$(grep -rl 'экран памяти' "$T/log/inbox"/*.txt 2>/dev/null | head -1 | xargs -r basename | sed 's/\.txt$//')
got=нет
[ -n "$METKA" ] && grep -q "inbox:$METKA" "$T/project/dev-map.yaml" && got=да
itog "да" "$got" "карточка из несрочного сообщения заведена КОДОМ (источник inbox:$METKA)"

# Карта осталась целой: число задач выросло ровно на одну.
BYLO=$(grep -c '^      - id: ' "$KOREN/dev-map.yaml")
STALO=$(grep -c '^      - id: ' "$T/project/dev-map.yaml")
itog "$((BYLO + 1))" "$STALO" "в карте стенда стало ровно на одну задачу больше"

# 5. Свод рубежа: один сортированный список, срочное первым.
SVOD=$(python3 "$KOREN/scripts/inbox-svodka.py" --инбокс "$T/log/inbox" \
        --карта "$T/project/dev-map.yaml" 2>&1)
got=нет
printf '%s' "$SVOD" | grep -q 'экран памяти' && printf '%s' "$SVOD" | grep -q 'останови выкат' && got=да
itog "да" "$got" "свод рубежа показывает оба накопленных сообщения"
got=нет; printf '%s' "$SVOD" | grep -q '1\. СРОЧНО' && got=да
itog "да" "$got" "в своде срочное стоит первым"

printf '%s\n' "$SVOD" | sed 's/^/        /'

# Красная проба обязана показать, ЧЕМ упал стенд: иначе разбор начинается с
# гадания, а стенд к этому времени уже убран.
if [ "$ok" != 1 ]; then
    echo "--- вывод диспетчера на стенде ---"
    tail -20 "$T/dispatcher.log" 2>/dev/null
    echo "--- журнал канала на стенде ---"
    tail -20 "$T/log/tg-dispatcher.log" 2>/dev/null
    echo "--- карточка из сообщения ---"
    tail -10 "$T/log/karta-iz-soobshcheniya.log" 2>/dev/null
fi

[ "$ok" = 1 ] && { echo "ПРОБА ЗЕЛЁНАЯ: 8 путей, первым — больной случай (несрочное не пинает панель)"; exit 0; }
echo "ПРОБА КРАСНАЯ"; exit 1
