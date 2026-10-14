#!/usr/bin/env bash
#
# probe-secret-from-channel.sh — живая проба: секрет, присланный владельцем в
# канал, не оседает в журналах харнеса.
#
# Работает НАСТОЯЩИЙ tg-dispatcher.sh со своим циклом getUpdates, два стенда
# подряд. Подставлено то же, что в probe-login-without-failure.sh, и названо:
# Bot API — свой HTTP-сервер на localhost (он же пишет всё, что диспетчер
# отправил назад в канал), паспорт установки со своими каталогами, вход к
# модели — свой скрипт, который складывает каждый вопрос в файл стенда. На
# втором стенде судья секретов подменён отказом (HARNESS_SECRET_INTAKE_PY).
#
# Значение порождается при каждом прогоне (40 hex — форма ключа МойСклад) и в
# выводе пробы не печатается.
#
# Что доказывается (больные случаи первыми):
#   1. БОЛЬНОЙ СЛУЧАЙ: ключ голым текстом, ключ в задаче и ключ подписью к фото —
#      значения нет ни в одном файле стенда: журналы, очередь (и JSON вложения),
#      ответы в канал, вопрос модели, пинок агенту;
#   2. БОЛЬНОЙ СЛУЧАЙ: судья секретов отказал — значения нет нигде, сообщение с
#      ключом закрыто пометкой «секрет не принят», а «да», «1» и пауза доходят
#      как есть; владелец узнаёт о недоступной проверке один раз;
#   3. путь вопроса модели пройден (вопросов ≥ 1), иначе пункт 1 о нём пуст;
#   4. значения лежат в $LOG_DIR/inbox/секреты/<id>: файлы 600, каталог 700;
#      пометка несёт путь и запрет читать значение в сессию;
#   5. обычное сообщение (с коротким хэшем коммита) легло слово в слово.
#
#   bash scripts/probe-secret-from-channel.sh [--диспетчер <путь>]
# Код возврата: 0 — зелёная, 1 — красная.
#
# Имена переменных ЛАТИНИЦЕЙ: кириллические bash не берёт и печатает в ошибку
# само значение (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8

KOREN="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
DISPATCHER="$KOREN/scripts/tg-dispatcher.sh"
[ "${1:-}" = "--диспетчер" ] && DISPATCHER="$2"

T=$(mktemp -d /tmp/proba-sekret-XXXXXX)
TOKEN="000:СТЕНДОВЫЙ"   # не-секрет: подставной Bot API стенда, боевого токена здесь нет
PIDS=()
SESSIONS=()

source "$KOREN/scripts/lib/selftest_cases.sh"

pribrat() {
    local p s
    for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done
    for s in "${SESSIONS[@]}"; do tmux kill-session -t "$s" 2>/dev/null; done
    rm -rf "$T"
}
trap pribrat EXIT

command -v tmux >/dev/null 2>&1 || { echo "ПРОБА: нет tmux — стенд поднять нечем"; exit 1; }

# Значение — в файл, не в argv и не в вывод.
mkdir -p "$T/api"
python3 -c 'import secrets, sys; open(sys.argv[1], "w").write(secrets.token_hex(20))' "$T/api/value"
VALUE=$(cat "$T/api/value")
PLAIN="Посмотри коммит 65a59df и подними диспетчер"

# ── подставной Bot API: отдаёт сообщения из файла один раз, пишет отправленное
cat > "$T/api/api.py" <<'PYEOF'
import json
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

СООБЩЕНИЯ, ОТПРАВЛЕННОЕ = sys.argv[2], sys.argv[3]
ОТДАНО = {"было": False}


def обновления():
    if ОТДАНО["было"]:
        time.sleep(1)
        return []
    ОТДАНО["было"] = True
    return json.load(open(СООБЩЕНИЯ, encoding="utf-8"))


class Рука(BaseHTTPRequestHandler):
    def ответить(self, тело_запроса: bytes):
        если_updates = self.path.endswith("/getUpdates")
        if not если_updates:
            # Ответ владельцу — тоже журнал: значение не должно вернуться в чат.
            with open(ОТПРАВЛЕННОЕ, "a", encoding="utf-8") as ф:
                ф.write(urllib.parse.unquote_plus(тело_запроса.decode()) + "\n")
        тело = ({"ok": True, "result": обновления()} if если_updates
                else {"ok": True, "result": {"message_id": 1}})
        сырое = json.dumps(тело).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(сырое)))
        self.end_headers()
        self.wfile.write(сырое)

    def do_GET(self):
        self.ответить(b"")

    def do_POST(self):
        длина = int(self.headers.get("Content-Length") or 0)
        self.ответить(self.rfile.read(длина))

    def log_message(self, *_):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), Рука).serve_forever()
PYEOF

# Сообщения стенда: «текст» — обычное, «фото» — вложение с подписью; место
# значения помечено словом КЛЮЧ и заполняется из файла значения.
soobshcheniya() {  # $1 = файл значения, $2 = куда, далее пары: message_id вид:текст
    python3 - "$@" <<'PYEOF'
import json, sys
значение = open(sys.argv[1]).read()
обновления = []
for номер, (ид, запись) in enumerate(zip(sys.argv[3::2], sys.argv[4::2]), 1):
    вид, _, текст = запись.partition(":")
    текст = текст.replace("КЛЮЧ", значение)
    сообщение = {"message_id": int(ид), "chat": {"id": 777000}}
    if вид == "фото":
        сообщение.update(photo=[{"file_id": "stend-photo", "file_unique_id": "u",
                                 "width": 1, "height": 1}], caption=текст)
    else:
        сообщение["text"] = текст
    обновления.append({"update_id": номер, "message": сообщение})
json.dump(обновления, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
PYEOF
}

# Поднять стенд, дождаться признаков, погасить. $1 = имя, $2 = сколько строк
# ждать в журнале входящих, $3 = ждать ли вопроса модели (да/нет), далее —
# окружение диспетчера (ИМЯ=значение).
stend() {
    local name="$1" lines="$2" model="$3"; shift 3
    local D="$T/$name" session="proba-sekret-$name-$$" port api disp
    mkdir -p "$D/log" "$D/project/harness/config" "$D/model-asks"
    cp "$KOREN/dev-map.yaml" "$D/project/dev-map.yaml"
    cp "$KOREN/harness/config/queue.yaml" "$D/project/harness/config/queue.yaml"
    printf '%s' "$TOKEN" > "$D/token"
    port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
    python3 "$T/api/api.py" "$port" "$T/api/$name.json" "$D/sent.log" & api=$!
    PIDS+=("$api")
    cat > "$D/model.py" <<PYEOF
import json, shutil, sys, time
shutil.copy(sys.argv[sys.argv.index("--вопрос-файл") + 1], "$D/model-asks/%s.txt" % time.time_ns())
print(json.dumps({"задачи": ["подключить склад"], "указания": [], "вопросы": []}, ensure_ascii=False))
PYEOF
    cat > "$D/install.conf" <<EOF
PROJECT_DIR="$D/project"
PROJECT_NAME="стенд"
LOG_DIR="$D/log"
TG_CHAT_ID="777000"
TG_TOKEN_FILE="$D/token"
AGENT_USER="$(id -un)"
EOF
    cat > "$D/harness.conf" <<EOF
TMUX_SESSION="$session"
SERVICE_LOCKS_SUBDIR="services"
ACTIVE_LINE="harness"
EOF
    # Приёмник вместо панели агента (`exec -a node`: пинок идёт только в claude/node).
    tmux new-session -d -s "$session" "bash -c 'exec -a node cat > $D/панель'"
    SESSIONS+=("$session")
    sleep 1
    env "$@" HARNESS_INSTALL_CONF="$D/install.conf" HARNESS_CONF="$D/harness.conf" \
        HARNESS_SPROSIT_MODEL="$D/model.py" TG_API_BASE="http://127.0.0.1:$port" \
        setsid nohup bash "$DISPATCHER" > "$D/dispatcher.log" 2>&1 < /dev/null &
    disp=$!
    PIDS+=("$disp")
    # Ждём по ПРИЗНАКУ: строки журнала входящих, затем вопрос модели.
    for _ in $(seq 1 60); do
        [ "$(cat "$D/log/inbox.jsonl" 2>/dev/null | wc -l)" -ge "$lines" ] && break
        sleep 0.5
    done
    if [ "$model" = да ]; then
        for _ in $(seq 1 60); do
            [ -n "$(ls -A "$D/model-asks" 2>/dev/null)" ] && break
            sleep 0.5
        done
    fi
    sleep 3          # пинки доезжают до панели
    kill "$disp" "$api" 2>/dev/null
    tmux send-keys -t "$session" C-d 2>/dev/null
    sleep 0.5
}

uteklo() {  # $1 = каталог стенда → файлы, где лежит значение (кроме хранилища)
    # Опции — ДО `--`: после него grep считает их именами файлов.
    grep -rlF --exclude-dir=секреты -- "$VALUE" "$1" 2>/dev/null | sed "s|^$1/||" | sort | tr '\n' ' '
}

# ── стенд 1: настоящий судья ────────────────────────────────────────────────
soobshcheniya "$T/api/value" "$T/api/zhivoj.json" \
    21 "текст:КЛЮЧ" \
    22 "текст:Надо добавить в карту подключение склада МойСклад, ключ: КЛЮЧ" \
    23 "фото:КЛЮЧ" \
    24 "текст:$PLAIN"
stend zhivoj 4 да
D="$T/zhivoj"

case_check "БОЛЬНОЙ СЛУЧАЙ: значения нет ни в одном файле стенда (текст, задача, подпись к фото)" "" "$(uteklo "$D")"
got=$(grep -cF -- "[секрет из канала: $D/log/inbox/секреты/" "$D/log/inbox.jsonl" 2>/dev/null); got=${got:-0}
case_check "в журнале входящих три пометки с путём к хранилищу" "3" "$got"
FOTO=$(grep -lF '"photo"' "$D/log/inbox"/*.txt 2>/dev/null | head -1)
got=нет
[ -n "$FOTO" ] && jq -r '.caption' "$FOTO" 2>/dev/null | grep -qF '[секрет из канала:' && got=да
case_check "подпись к фото в файле очереди (JSON вложения) — пометка" "да" "$got"
ASKS=$(find "$D/model-asks" -type f 2>/dev/null | wc -l | tr -d ' ')
case_check "путь вопроса модели пройден (вопросов ≥ 1)" "да" "$([ "$ASKS" -ge 1 ] && echo да || echo "нет ($ASKS)")"
got=нет; grep -qF 'подключение склада' "$D/model-asks"/* 2>/dev/null && got=да
case_check "модель спрошена именно о сообщении с ключом" "да" "$got"

SDIR="$D/log/inbox/секреты"
case_check "в хранилище секретов три файла" "3" "$(find "$SDIR" -type f 2>/dev/null | wc -l | tr -d ' ')"
got=$(for f in "$SDIR"/*; do grep -qxF -- "$VALUE" "$f" && echo да || echo нет; done 2>/dev/null | sort -u | tr '\n' ' ')
case_check "каждый файл хранилища хранит значение" "да " "$got"
got=$(stat -c %a "$SDIR"/* 2>/dev/null | sort -u | tr '\n' ' ')
case_check "права файлов секрета" "600 " "$got"
case_check "права каталога секретов" "700" "$(stat -c %a "$SDIR" 2>/dev/null || echo нет-каталога)"
got=нет; grep -qF 'в сессию не читать' "$D/log/inbox.jsonl" 2>/dev/null && got=да
case_check "пометка запрещает читать значение в сессию" "да" "$got"

got=$(jq -r 'select(.message_id=="24") | .text' "$D/log/inbox.jsonl" 2>/dev/null)
case_check "обычное сообщение в журнале входящих слово в слово" "$PLAIN" "$got"
got=$(grep -lxF -- "$PLAIN" "$D/log/inbox"/*.txt 2>/dev/null | wc -l | tr -d ' ')
case_check "обычное сообщение легло в очередь файлом" "1" "$got"

# ── стенд 2: судья секретов отказал ─────────────────────────────────────────
printf 'import sys\nsys.exit(3)\n' > "$T/api/otkaz.py"
soobshcheniya "$T/api/value" "$T/api/otkaz.json" 31 "текст:КЛЮЧ" 32 "текст:да" \
    33 "текст:1" 34 "текст:встань на паузу"
stend otkaz 4 нет HARNESS_SECRET_INTAKE_PY="$T/api/otkaz.py"
D2="$T/otkaz"
case_check "БОЛЬНОЙ СЛУЧАЙ: судья отказал — значения нет нигде, и в ответе владельцу тоже" "" "$(uteklo "$D2")"
got=$(jq -r 'select(.message_id=="31") | .text' "$D2/log/inbox.jsonl" 2>/dev/null | grep -c '^\[секрет не принят:')
case_check "судья отказал — сообщение с ключом закрыто пометкой «секрет не принят»" "1" "$got"
# БОЛЬНОЙ СЛУЧАЙ: лежащий судья не должен класть весь канал — согласие, ответ
# цифрой и пауза без длинных кусков доходят как есть.
for pair in "32:да" "33:1" "34:встань на паузу"; do
    got=$(jq -r "select(.message_id==\"${pair%%:*}\") | .text" "$D2/log/inbox.jsonl" 2>/dev/null)
    case_check "БОЛЬНОЙ СЛУЧАЙ: судья отказал, а «${pair#*:}» дошло как есть" "${pair#*:}" "$got"
done
got=$(jq -r 'select(.message_id=="32") | .text' "$D2/log/confirmations.jsonl" 2>/dev/null)
case_check "судья отказал, а «да» легло в журнал согласий" "да" "${got:-нет записи}"
got=$(grep -c 'Проверка секретов недоступна' "$D2/sent.log" 2>/dev/null); got=${got:-0}
case_check "владельцу сказано, что проверка недоступна, — один раз на четыре сообщения" "1" "$got"

if [ "$CASES_FAILED" != 0 ]; then
    for d in "$D" "$D2"; do
        echo "--- вывод диспетчера на стенде $(basename "$d") (без значения) ---"
        tail -15 "$d/dispatcher.log" 2>/dev/null | sed "s|$VALUE|<ЗНАЧЕНИЕ>|g"
    done
fi

[ "$CASES_FAILED" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: $CASES_PATHS путей, первыми — больные случаи (значение не осело в журналах; отказ судьи закрывает наглухо)"; exit 0; }
echo "ПРОБА КРАСНАЯ"; exit 1
