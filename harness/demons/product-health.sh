#!/usr/bin/env bash
#
# product-health.sh — сторож здоровья ПРОДУКТА: его служба говорит, он слушает.
#
# ЧТО ДЕРЖИТ. Служба продукта умеет сказать о себе правду («данные не
# обновляются», «очередь не разбирается»), но сказать её НЕКОМУ: демоны харнеса
# смотрели только на харнес. Улика 02.10.2026 — сторож остатка данных отказывал
# каждые пять минут 11 часов 37 минут, витрина обещала покупателю вчерашние
# числа, ручка здоровья отвечала «жив», потому что жива была СЛУЖБА. Беду нашёл
# человек глазами в журнале; механизма, который нашёл бы её сам, не было вовсе.
#
# ОТКУДА АДРЕС. `PRODUCT_HEALTH_URL` в harness.conf — ДАННЫЕ: у каждого продукта
# своя ручка, а пустой ключ означает «продукта с ручкой здоровья здесь нет» и
# заставляет сторожа молчать. Так харнес остаётся универсальным: он знает, что
# бывает ручка здоровья, и не знает ничего про книжный магазин.
#
# ПОЧЕМУ ГОВОРИТ ТОЛЬКО НА СМЕНЕ СОСТОЯНИЯ. Опрос идёт каждые пять минут, то есть
# 288 раз в сутки. Сообщение на каждый красный опрос — это 288 сообщений, и
# владелец перестанет их читать на третьем; молчание же про возврат к норме
# оставляет его в тревоге. Поэтому говорится ДВАЖДЫ: когда стало плохо и когда
# снова стало хорошо. Состояние помнится файлом (`$LOG_DIR/product-health.state`).
#
# Чем доказывается: `--selftest` поднимает свою ручку на своём порту и проводит
# больные случаи — 503 говорит, повтор 503 молчит, возврат 200 говорит, мёртвый
# адрес говорит. Живой больной случай на приёмке: сделать данные продукта
# несвежими и дождаться сообщения БЕЗ своего участия.
#
# Запуск: cron, каждые 5 минут, от имени $AGENT_USER.
set -euo pipefail

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# ── самотест: больные случаи на своей ручке ─────────────────────────────────
if [ "${1:-}" = "--selftest" ]; then
    T=$(mktemp -d)
    PORT=18907
    SERVER_PID=""
    # shellcheck disable=SC2064
    trap 'rm -rf "$T"; [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null' EXIT

    # Свой порт и отказ при занятом: на чужом экземпляре проба стала бы зелёной
    # ни от чего и оставалась бы такой до дня, когда ей поверят.
    if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
        echo "САМОТЕСТ ПРОВАЛЕН: порт $PORT занят — отвечал бы кто-то другой"; exit 1
    fi

    cat > "$T/health.py" <<'PY'
import http.server, json, pathlib, sys

STATE = pathlib.Path(sys.argv[2])


class Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code = int(STATE.read_text().strip())
        body = json.dumps({
            'ok': code == 200,
            'checks': [{'name': 'наличие витрины', 'ok': code == 200,
                        'detail': 'последний обход 1253 с назад, потолок 900 с'}],
        }).encode()
        self.send_response(code)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), Health).serve_forever()
PY
    echo 503 > "$T/code"
    python3 "$T/health.py" "$PORT" "$T/code" &
    SERVER_PID=$!
    for _ in $(seq 1 50); do
        (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break
        sleep 0.1
    done

    mkdir -p "$T/hb" "$T/log"
    cat > "$T/install.conf" <<EOF
PROJECT_NAME="selftest"
PROJECT_DIR="$T"
AGENT_USER="selftest"
SECRETS_DIR="$T"
TG_TOKEN_FILE="$T/no-token"
TG_CHAT_ID=""
AUTONOMY="semi"
HEARTBEAT_DIR="$T/hb"
LOG_DIR="$T/log"
EOF
    probeg() {
        INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
        PRODUCT_HEALTH_DRY_SEND=1 bash "$0"
    }

    # БОЛЬНОЙ СЛУЧАЙ 1: продукт говорит 503 — сторож обязан сказать.
    printf 'PRODUCT_HEALTH_URL="http://127.0.0.1:%s/health"\n' "$PORT" > "$T/harness.conf"
    OUT=$(probeg) || { echo "САМОТЕСТ ПРОВАЛЕН: прогон на 503 упал"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'наличие витрины' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: имя красной проверки не названо"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q '1253' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: число из ответа продукта не доехало"; echo "$OUT"; exit 1; }

    # БОЛЬНОЙ СЛУЧАЙ 2: та же беда через пять минут — сторож обязан МОЛЧАТЬ.
    OUT=$(probeg) || { echo "САМОТЕСТ ПРОВАЛЕН: повторный прогон упал"; echo "$OUT"; exit 1; }
    if echo "$OUT" | grep -q 'ОТПРАВКА'; then
        echo "САМОТЕСТ ПРОВАЛЕН: повтор той же беды даёт второе сообщение — 288 в сутки"; echo "$OUT"; exit 1
    fi

    # БОЛЬНОЙ СЛУЧАЙ 3: стало хорошо — сторож обязан сказать и об этом, иначе
    # владелец остаётся в тревоге после первого сообщения.
    echo 200 > "$T/code"
    OUT=$(probeg) || { echo "САМОТЕСТ ПРОВАЛЕН: прогон на 200 упал"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'снова' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: возврат к норме не назван"; echo "$OUT"; exit 1; }
    OUT=$(probeg) || { echo "САМОТЕСТ ПРОВАЛЕН: второй прогон на 200 упал"; echo "$OUT"; exit 1; }
    if echo "$OUT" | grep -q 'ОТПРАВКА'; then
        echo "САМОТЕСТ ПРОВАЛЕН: здоровая служба порождает сообщения"; echo "$OUT"; exit 1
    fi

    # БОЛЬНОЙ СЛУЧАЙ 4: служба не отвечает ВОВСЕ — это тоже беда, а не тишина.
    kill "$SERVER_PID" 2>/dev/null; SERVER_PID=""
    for _ in $(seq 1 50); do
        (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null || break
        sleep 0.1
    done
    OUT=$(probeg) || { echo "САМОТЕСТ ПРОВАЛЕН: прогон по мёртвому адресу упал"; echo "$OUT"; exit 1; }
    # Регистр не судится: слова в сообщении владельцу набраны заглавными.
    echo "$OUT" | grep -qi 'не отвечает' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: мёртвая служба не названа"; echo "$OUT"; exit 1; }

    # ЗДОРОВЫЙ СЛУЧАЙ: ключа нет — сторожу нечего слушать, и это не ошибка.
    echo 'PRODUCT_HEALTH_URL=""' > "$T/harness.conf"
    OUT=$(probeg) || { echo "САМОТЕСТ ПРОВАЛЕН: пустой адрес обязан давать выход 0"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'не настроен' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: пустой адрес не назван вслух"; echo "$OUT"; exit 1; }

    echo "САМОТЕСТ ПРОЙДЕН: 503 назван по имени и с числом, повтор молчит, возврат к норме сказан,"
    echo "мёртвый адрес — беда, пустой ключ — молчание без ошибки"
    echo "(доставка в Telegram самотестом не проверяется — доказывается на живом, 01-SPEC §7)"
    exit 0
fi

# ── конфиги ─────────────────────────────────────────────────────────────────
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/config.sh"
INSTALL_CONF="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-$(konf_koren)/install.conf}}"
HARNESS_CONF="${HARNESS_CONF:-$(konf_koren)/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "product-health: нет $INSTALL_CONF — установка не завершена (01-SPEC §0)"; exit 1; }
[ -r "$HARNESS_CONF" ] || { echo "product-health: нет $HARNESS_CONF — скопируйте harness/config/harness.conf в $(konf_koren)/"; exit 1; }
# Окружение старше конфига: общий загрузчик, а не голый source.
konf_zagruzit
: "${HEARTBEAT_DIR:?пуст HEARTBEAT_DIR}" "${LOG_DIR:?пуст LOG_DIR}"

URL="${PRODUCT_HEALTH_URL:-}"
if [ -z "$URL" ]; then
    say "PRODUCT_HEALTH_URL не настроен — продукта с ручкой здоровья здесь нет, слушать нечего"
    mkdir -p "$HEARTBEAT_DIR"
    touch "$HEARTBEAT_DIR/product-health"
    exit 0
fi

STATE_FILE="$LOG_DIR/product-health.state"
mkdir -p "$LOG_DIR" "$HEARTBEAT_DIR"
WAS=$(cat "$STATE_FILE" 2>/dev/null || echo "ok")

# Тело и код ответа одним вызовом. Ошибка сети — НЕ падение сторожа: служба,
# которая не отвечает, это беда продукта, и назвать её надо словами.
ANSWER=$(curl -s -m 15 -w '\n%{http_code}' "$URL" 2>/dev/null || true)
CODE=$(printf '%s' "$ANSWER" | tail -n1)
BODY=$(printf '%s' "$ANSWER" | sed '$d')

if [ "$CODE" = "200" ]; then
    NOW_STATE="ok"
    MESSAGE="Служба продукта снова отвечает нормально."
elif [ -z "$CODE" ] || [ "$CODE" = "000" ]; then
    NOW_STATE="dead"
    MESSAGE="Служба продукта НЕ ОТВЕЧАЕТ по адресу здоровья. Пока она молчит, сайт может показывать старые данные."
else
    NOW_STATE="bad"
    # Имена и числа берутся из ОТВЕТА ПРОДУКТА: сторож не знает, что у него
    # внутри, и придумывать за него нечего. Нет jq или тело не разбирается —
    # отдаётся код ответа, а не молчание.
    BAD=$(printf '%s' "$BODY" | jq -r '
        if (.checks? | type) == "array"
        then [.checks[] | select(.ok == false) | "• \(.name): \(.detail // "без подробностей")"] | join("\n")
        else empty end' 2>/dev/null || true)
    [ -n "$BAD" ] || BAD="• служба ответила кодом $CODE и без подробностей"
    MESSAGE="Служба продукта сообщает о беде (код $CODE):
$BAD"
fi

say "опрос $URL → код ${CODE:-нет ответа}, состояние $NOW_STATE (было $WAS)"

# ── сообщение ТОЛЬКО на смене состояния ─────────────────────────────────────
if [ "$NOW_STATE" = "$WAS" ]; then
    say "состояние не менялось — владельцу не пишем (иначе 288 сообщений в сутки)"
    printf '%s' "$NOW_STATE" > "$STATE_FILE"
    touch "$HEARTBEAT_DIR/product-health"
    exit 0
fi

say "ОТПРАВКА: $MESSAGE"
if [ "${PRODUCT_HEALTH_DRY_SEND:-0}" = 1 ]; then
    say "(PRODUCT_HEALTH_DRY_SEND=1 — отправка пропущена)"
    printf '%s' "$NOW_STATE" > "$STATE_FILE"
    touch "$HEARTBEAT_DIR/product-health"
    exit 0
fi

if [ -x "$PROJECT_DIR/scripts/tg_send.sh" ] && "$PROJECT_DIR/scripts/tg_send.sh" "$MESSAGE"; then
    say "сообщение доставлено"
    # Состояние запоминается ТОЛЬКО после удачной отправки: иначе первая же
    # недоставка съедает сигнал навсегда — следующий опрос увидит «состояние то
    # же» и промолчит про беду, о которой владельцу не сказали.
    printf '%s' "$NOW_STATE" > "$STATE_FILE"
    touch "$HEARTBEAT_DIR/product-health"
    exit 0
fi

say "сообщение НЕ доставлено — считаю прогон ПРОВАЛЕННЫМ, состояние не запоминаю"
exit 1
