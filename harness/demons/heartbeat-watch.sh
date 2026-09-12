#!/usr/bin/env bash
#
# heartbeat-watch.sh — сторож меток: мёртвый демон называется по имени.
#
# Что держит: страховку нельзя оценивать по частоте обращений к ней
# (01-SPEC §9). Каждый демон ставит метку в $HEARTBEAT_DIR только при
# успешном прогоне; сторож раз в сутки сверяет возраст КАЖДОЙ метки с её
# периодом ×2 и шлёт владельцу сводку ВСЕГДА — и зелёную «все демоны живы»,
# и красную поимённо. Молчание сторожа неотличимо от его смерти, поэтому
# смерть самого сторожа владелец обнаруживает по ОТСУТСТВИЮ утреннего
# сообщения (норма UNIFIED/08).
#
# Откуда взят: собран для стартового пакета по 01-SPEC §9; таблица периодов —
# в harness.conf (HEARTBEAT_PERIODS), данные, не код.
#
# Чем доказывается: встроенным больным случаем (`--selftest`: просроченная
# метка → сводка называет имя) и живым больным случаем на приёмке —
# закомментировать строку одного демона в crontab → завтрашняя сводка обязана
# назвать его. Успех отправки — только ok:true (curl+jq); при провале отправки
# выход ненулевой и метку себе сторож НЕ ставит.
#
# Запуск: cron, раз в сутки 08:00, от имени $AGENT_USER.
set -euo pipefail

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# ── самотест: больной случай во временном каталоге ──────────────────────────
if [ "${1:-}" = "--selftest" ]; then
    T=$(mktemp -d)
    trap 'rm -rf "$T"' EXIT
    mkdir -p "$T/hb"
    cat > "$T/install.conf" <<EOF
PROJECT_NAME="selftest"
PROJECT_DIR="$T"
AGENT_USER="selftest"
SECRETS_DIR="$T"
TG_TOKEN_FILE="$T/no-token"
TG_CHAT_ID=""
AUTONOMY="semi"
HEARTBEAT_DIR="$T/hb"
LOG_DIR="$T"
EOF
    cat > "$T/harness.conf" <<EOF
HEARTBEAT_PERIODS="alpha:10 beta:10"
EOF
    touch "$T/hb/alpha"                          # живой: только что
    touch -d '2 hours ago' "$T/hb/beta"          # мёртвый: 120 мин > 10×2
    # bash "$0", не "$0": после scp с Windows exec-бита может не быть (П-5).
    OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон упал"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'beta' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: просроченная beta не названа по имени"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'МЁРТВ' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: в сводке нет слова МЁРТВ"; echo "$OUT"; exit 1; }
    if echo "$OUT" | grep -q 'alpha.*МЁРТВ'; then
        echo "САМОТЕСТ ПРОВАЛЕН: живая alpha объявлена мёртвой"; echo "$OUT"; exit 1
    fi
    # больной случай: guard_exceptions.log вырос с прошлого прогона (LOG_DIR="$T")
    printf '%s\n' '{"ts":1,"reason":"т","detail":"т"}' '{"ts":2,"reason":"т","detail":"т"}' \
                  '{"ts":3,"reason":"т","detail":"т"}' > "$T/guard_exceptions.log"
    OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон с приростом лога упал (а не должен)"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'сторож команд падал 3 раз' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: прирост guard_exceptions.log не назван в сводке"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'guard_exceptions.log' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: сводка не говорит, куда смотреть"; echo "$OUT"; exit 1; }
    # без прироста предупреждение обязано исчезнуть (размер запомнен)
    OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: повторный прогон упал"; echo "$OUT"; exit 1; }
    if echo "$OUT" | grep -q 'сторож команд падал'; then
        echo "САМОТЕСТ ПРОВАЛЕН: без прироста лога сводка всё равно пугает падениями"; echo "$OUT"; exit 1
    fi
    # больной случай Д-4а: сводка НЕ доставлена (канал не настроен) → размер
    # НЕ запоминается, прирост обязан всплыть в СЛЕДУЮЩЕЙ доставленной сводке.
    printf '%s\n' '{"ts":4,"reason":"т","detail":"т"}' >> "$T/guard_exceptions.log"
    if OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" bash "$0" 2>&1); then
        echo "САМОТЕСТ ПРОВАЛЕН: недоставленная сводка не сделала прогон красным"; echo "$OUT"; exit 1
    fi
    OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон после недоставки упал"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'сторож команд падал 1 раз' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: недоставка съела сигнал прироста — SIZE_FILE записан до отправки (Д-4а)"; echo "$OUT"; exit 1; }
    # сводка обязана говорить о юните диспетчера (active или честное «не проверено»)
    echo "$OUT" | grep -q 'harness-dispatcher' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: сводка молчит про юнит harness-dispatcher (Д-4б)"; echo "$OUT"; exit 1; }
    # строка расхода токенов включается в сводку, когда файл есть (Д-4в)
    echo "токены за сутки: 1234 (демо)" > "$T/tokens-daily.summary"
    OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон с tokens-daily.summary упал"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'токены за сутки: 1234' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: строка tokens-daily.summary не попала в сводку (Д-4в)"; echo "$OUT"; exit 1; }
    # Подробный отчёт вытесняет короткую строку: он содержит те же числа и
    # ещё распределение. Обе сразу — двойной расход внимания владельца.
    printf 'РАСХОД ЗА СУТКИ — демо\n  медиана                   42\n' > "$T/tokens-daily.report"
    OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон с tokens-daily.report упал"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'медиана                   42' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: подробный отчёт не попал в сводку"; echo "$OUT"; exit 1; }
    if echo "$OUT" | grep -q 'токены за сутки: 1234'; then
        echo "САМОТЕСТ ПРОВАЛЕН: при наличии отчёта короткая строка обязана уступить место"; echo "$OUT"; exit 1
    fi
    rm -f "$T/tokens-daily.report"
    # Д-4-остаток: свежий каталог БЕЗ метки — «ещё не прогонялся», не МЁРТВ;
    # заодно П-15: при TG_CHANNEL_VARIANT="A" — «диспетчер выключен», не крик.
    mkdir -p "$T/hb2"
    cat > "$T/install2.conf" <<EOF
PROJECT_NAME="selftest"
PROJECT_DIR="$T"
AGENT_USER="selftest"
SECRETS_DIR="$T"
TG_TOKEN_FILE="$T/no-token"
TG_CHAT_ID=""
AUTONOMY="semi"
HEARTBEAT_DIR="$T/hb2"
LOG_DIR="$T/log2"
TG_CHANNEL_VARIANT="A"
EOF
    mkdir -p "$T/log2"
    cat > "$T/harness2.conf" <<EOF
HEARTBEAT_PERIODS="gamma:10"
EOF
    OUT=$(INSTALL_CONF="$T/install2.conf" HARNESS_CONF="$T/harness2.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон со свежим каталогом упал"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'gamma — ещё не прогонялся' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: свежеустановленный демон без метки не назван «ещё не прогонялся» (Д-4)"; echo "$OUT"; exit 1; }
    if echo "$OUT" | grep -q '— МЁРТВ'; then
        echo "САМОТЕСТ ПРОВАЛЕН: свежий каталог без метки объявлен МЁРТВым (Д-4 — ложный крик до первого прогона)"; echo "$OUT"; exit 1
    fi
    echo "$OUT" | grep -q 'диспетчер выключен (вариант А — эксперимент)' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: при варианте А нет строки «диспетчер выключен» (П-15)"; echo "$OUT"; exit 1; }
    # маркер установки постарел, метки так и нет → по-прежнему МЁРТВ
    touch -d '2 hours ago' "$T/hb2/.install-stamp"
    OUT=$(INSTALL_CONF="$T/install2.conf" HARNESS_CONF="$T/harness2.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон со старым маркером упал"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'gamma — МЁРТВ' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: метки нет при старом маркере установки, а gamma не МЁРТВ"; echo "$OUT"; exit 1; }
    echo "САМОТЕСТ ПРОЙДЕН: мёртвая метка названа, живая не оклеветана, прирост лога сторожа замечен,"
    echo "недоставка не съедает сигнал (SIZE_FILE после отправки), юнит диспетчера и токены — в сводке,"
    echo "свежая установка без метки — «ещё не прогонялся», вариант А — «диспетчер выключен»"
    echo "(доставка в Telegram самотестом не проверяется — доказывается на живом при установке, 01-SPEC §7)"
    exit 0
fi

# ── конфиги ─────────────────────────────────────────────────────────────────
INSTALL_CONF="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-/etc/harness/install.conf}}"
HARNESS_CONF="${HARNESS_CONF:-/etc/harness/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "heartbeat-watch: нет $INSTALL_CONF — установка не завершена (01-SPEC §0)"; exit 1; }
[ -r "$HARNESS_CONF" ] || { echo "heartbeat-watch: нет $HARNESS_CONF — скопируйте harness/config/harness.conf в /etc/harness/"; exit 1; }
# Окружение старше конфига: общий загрузчик вместо голого source (улика
# 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/konf.sh"
konf_zagruzit
: "${HEARTBEAT_DIR:?пуст HEARTBEAT_DIR}" "${HEARTBEAT_PERIODS:?пуст HEARTBEAT_PERIODS — таблица периодов живёт в harness.conf}"

NOW=$(date +%s)

# Д-4-остаток: «метки нет вообще = МЁРТВ» кричал ложно сразу после установки —
# демон просто ещё не успел прогнаться. Маркер установки: файл создаётся этим
# сторожем при ПЕРВОМ прогоне и больше не трогается (каталог HEARTBEAT_DIR не
# годится — его mtime обновляет каждая новая метка). Метки нет при маркере
# моложе период×2 → «ещё не прогонялся», старше → по-прежнему МЁРТВ.
INSTALL_STAMP="$HEARTBEAT_DIR/.install-stamp"
mkdir -p "$HEARTBEAT_DIR"
[ -f "$INSTALL_STAMP" ] || touch "$INSTALL_STAMP"
STAMP_AGE_MIN=$(( (NOW - $(stat -c '%Y' "$INSTALL_STAMP")) / 60 ))

DEAD=""; FRESH=""; ALIVE_N=0; DEAD_N=0; FRESH_N=0

# Таблица: "имя:период-в-минутах …". Порог тревоги = период ×2 — один
# пропущенный прогон прощается (машина могла перезагружаться), два — нет.
for entry in $HEARTBEAT_PERIODS; do
    name="${entry%%:*}"; period_min="${entry##*:}"
    mark="$HEARTBEAT_DIR/$name"
    if [ ! -f "$mark" ]; then
        if [ "$STAMP_AGE_MIN" -lt $((period_min * 2)) ]; then
            FRESH="${FRESH}
• $name — ещё не прогонялся (установлен недавно)"
            FRESH_N=$((FRESH_N + 1))
        else
            DEAD="${DEAD}
• $name — МЁРТВ: метки нет вообще (ни одного успешного прогона за период ×2)"
            DEAD_N=$((DEAD_N + 1))
        fi
        continue
    fi
    age_min=$(( (NOW - $(stat -c '%Y' "$mark")) / 60 ))
    if [ "$age_min" -gt $((period_min * 2)) ]; then
        DEAD="${DEAD}
• $name — МЁРТВ: метка ${age_min} мин при периоде ${period_min} (порог ×2)"
        DEAD_N=$((DEAD_N + 1))
    else
        ALIVE_N=$((ALIVE_N + 1))
    fi
done

if [ "$DEAD_N" = 0 ] && [ "$FRESH_N" = 0 ]; then
    SUMMARY="Утренняя сводка харнеса: все демоны живы ($ALIVE_N/$ALIVE_N)."
elif [ "$DEAD_N" = 0 ]; then
    SUMMARY="Утренняя сводка харнеса: живых $ALIVE_N, МЁРТВЫХ нет, ещё не прогонялись $FRESH_N:$FRESH"
else
    SUMMARY="Утренняя сводка харнеса: живых $ALIVE_N, МЁРТВЫХ $DEAD_N:$DEAD$FRESH
Лечение: лог демона в $LOG_DIR/<имя>.log, прогон вручную в env -i (01-SPEC §9)."
fi

# ── прирост guard_exceptions.log: падения сторожа команд видны в сводке ─────
# Сторож fail-open: упав, он пропускает команду и лишь пишет след в лог.
# Без этой сверки след никто не читает — молчаливый fail-open неотличим от
# здорового сторожа. Прирост НЕ делает прогон красным: демоны-то живы.
GUARD_LOG="${LOG_DIR:-/var/log/harness}/guard_exceptions.log"
SIZE_FILE="$HEARTBEAT_DIR/.guard_exceptions.size"
GUARD_NEWSIZE=""
if [ -f "$GUARD_LOG" ]; then
    prev=$(cat "$SIZE_FILE" 2>/dev/null || echo 0)
    case "$prev" in (''|*[!0-9]*) prev=0;; esac
    cur=$(stat -c '%s' "$GUARD_LOG")
    if [ "$cur" -gt "$prev" ]; then
        n=$(tail -c +"$((prev + 1))" "$GUARD_LOG" | wc -l)
        SUMMARY="$SUMMARY
⚠ сторож команд падал $n раз с прошлой сводки — смотреть guard_exceptions.log"
    fi
    GUARD_NEWSIZE="$cur"
fi

# SIZE_FILE — ТОЛЬКО после доставленной сводки (Д-4а): запись до отправки
# съедала водораздел, и при недоставке сигнал о падениях терялся навсегда.
remember_guard_size() {
    if [ -n "$GUARD_NEWSIZE" ]; then
        mkdir -p "$HEARTBEAT_DIR"
        echo "$GUARD_NEWSIZE" > "$SIZE_FILE"
    fi
}

# ── юнит диспетчера канала (Д-4б): мёртвый диспетчер = глухой владелец ──────
# П-15: при варианте А канала (TG_CHANNEL_VARIANT из install.conf) диспетчера
# НЕТ по замыслу — getUpdates владеет telegram-плагин; красный крик про
# выключенный юнит был бы ложью.
DISPATCHER_UNIT="${DISPATCHER_UNIT:-harness-dispatcher}"
if [ "${TG_CHANNEL_VARIANT:-B}" = "A" ]; then
    SUMMARY="$SUMMARY
диспетчер выключен (вариант А — эксперимент)"
elif command -v systemctl >/dev/null 2>&1; then
    UNIT_STATE=$(systemctl is-active "$DISPATCHER_UNIT" 2>&1 | head -1) || true
    if [ "$UNIT_STATE" = "active" ]; then
        # Живой юнит называется так же, как мёртвый: сводка обязана говорить о
        # диспетчере ВСЕГДА (Д-4б). Молчание о нём читается как «не проверяли».
        SUMMARY="$SUMMARY
диспетчер канала: юнит $DISPATCHER_UNIT active"
    else
        SUMMARY="$SUMMARY
⚠ диспетчер канала: юнит $DISPATCHER_UNIT НЕ active (${UNIT_STATE:-нет ответа}) — входящие владельца не читаются"
    fi
else
    SUMMARY="$SUMMARY
systemctl недоступен — состояние юнита $DISPATCHER_UNIT не проверено"
fi

# ── расход токенов (Д-4в): строку готовит демон-счётчик, мы только включаем ─
# Подробный отчёт, если он есть, вытесняет короткую строку: в нём те же числа
# плюс распределение контекста и сверка баланса (13.08.2026, запрос владельца).
TOKENS_SUMMARY="${LOG_DIR:-/var/log/harness}/tokens-daily.summary"
TOKENS_REPORT="${LOG_DIR:-/var/log/harness}/tokens-daily.report"
[ -f "$TOKENS_REPORT" ] && TOKENS_SUMMARY="$TOKENS_REPORT"
if [ -f "$TOKENS_SUMMARY" ]; then
    # П-15: head -c 1000 резал БАЙТЫ и рвал многобайтную кириллицу посередине.
    # Срез по СИМВОЛАМ: ${var:0:N} в подоболочке с LC_ALL=C.UTF-8 (в env -i
    # cron живёт в локали C, где ${var:0:N} снова считал бы байты).
    # Потолок 2000: подробный отчёт — тридцать строк, прежняя тысяча резала бы
    # его посередине, и владелец получал бы сводку без последних показателей.
    TOKENS_LINE=$(
        LC_ALL=C.UTF-8
        export LC_ALL
        line=$(cat "$TOKENS_SUMMARY")
        printf '%s' "${line:0:2000}"
    )
    SUMMARY="$SUMMARY
$TOKENS_LINE"
fi

say "$SUMMARY"

# Самотест не ходит в сеть: доставка доказывается на живом при установке.
if [ "${HEARTBEAT_DRY_SEND:-0}" = 1 ]; then
    say "(HEARTBEAT_DRY_SEND=1 — отправка пропущена)"
    remember_guard_size
    exit 0
fi

# ── отправка: ВСЕГДА, успех только при ok:true ──────────────────────────────
tg_deliver() {
    if [ -x "$PROJECT_DIR/scripts/tg_send.sh" ]; then
        if "$PROJECT_DIR/scripts/tg_send.sh" "$SUMMARY"; then return 0; fi
        say "tg_send.sh отказал — пробую прямой curl"
    fi
    if [ -z "${TG_CHAT_ID:-}" ] || [ ! -s "${TG_TOKEN_FILE:-/nonexistent}" ]; then
        say "КАНАЛ НЕ НАСТРОЕН: TG_CHAT_ID/TG_TOKEN_FILE — сводке некуда идти"
        return 1
    fi
    # URL с токеном — через stdin (curl -K -), а не в argv: argv виден любому
    # в ps. Образец — scripts/tg_send.sh; здесь была копия с токеном наружу.
    local token; token=$(cat "$TG_TOKEN_FILE")
    printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" \
        | curl -s -m 20 -K - -X POST \
        --data-urlencode "chat_id=$TG_CHAT_ID" --data-urlencode "text=$SUMMARY" \
        | jq -e '.ok == true' >/dev/null
}

if tg_deliver; then
    say "сводка доставлена (ok:true)"
    remember_guard_size
else
    # Недоставленная сводка = сторож молчит = сторож мёртв. Метку не ставим,
    # выходим с ошибкой — след остаётся хотя бы в логе cron.
    say "сводка НЕ доставлена — считаю прогон ПРОВАЛЕННЫМ"
    exit 1
fi

touch "$HEARTBEAT_DIR/heartbeat-watch"
