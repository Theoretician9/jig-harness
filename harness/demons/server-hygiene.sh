#!/usr/bin/env bash
#
# server-hygiene.sh — обёртка cron над server_hygiene_check.py.
#
# Что держит: ежесуточную проверку потолков машины и сервисов (диск, своп,
# swappiness, mem_limit/restart/порты compose). Сама логика — в
# server_hygiene_check.py рядом; обёртка отвечает за конфиги, канал и метку.
#
# Откуда взята: собрана для стартового пакета; проверка — живой скрипт
# боевого сервера (улики в его шапке: своп 91% 08.08, полстека после
# перезагрузки 05.07, открытые наружу MySQL/Redis/RabbitMQ/Vault 03.08).
#
# Чем доказывается: больным случаем на приёмке — временно vm.swappiness=60
# → завтрашний прогон называет нарушение и шлёт его владельцу.
#
# Различение исходов — по коду выхода проверки:
#   0 — чисто: метка, тишина в канал (сводку каждое утро шлёт heartbeat-watch);
#   1 — НАРУШЕНИЯ: проверка отработала (метка ставится), нарушения — владельцу;
#   ≥2 — проверка сама сломана: метки НЕТ, сигнал владельцу сразу (плюс
#        heartbeat-watch назовёт демона по имени завтра утром).
set -euo pipefail

INSTALL_CONF="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-/etc/harness/install.conf}}"
HARNESS_CONF="${HARNESS_CONF:-/etc/harness/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "server-hygiene: нет $INSTALL_CONF — установка не завершена (01-SPEC §0)"; exit 1; }
[ -r "$HARNESS_CONF" ] || { echo "server-hygiene: нет $HARNESS_CONF — скопируйте harness/config/harness.conf в /etc/harness/"; exit 1; }
# Окружение старше конфига: общий загрузчик вместо голого source (улика
# 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/konf.sh"
konf_zagruzit
: "${HEARTBEAT_DIR:?пуст HEARTBEAT_DIR}" "${LOG_DIR:?пуст LOG_DIR}"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# Один прогон за раз (Д-10, единый паттерн демонов). Лок — сервисный каталог.
LOCK_FILE="$LOG_DIR/locks/${SERVICE_LOCKS_SUBDIR:-services}/server-hygiene.lock"
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>>"$LOCK_FILE"
flock -n 9 || { say "прогон уже идёт (лок $LOCK_FILE) — выхожу"; exit 0; }

tg_send() {  # успех только при ok:true (норма 01-SPEC §7)
    local text="$1"
    if [ -x "$PROJECT_DIR/scripts/tg_send.sh" ]; then
        if "$PROJECT_DIR/scripts/tg_send.sh" "$text"; then return 0; fi
        say "tg_send.sh отказал — пробую прямой curl"
    fi
    if [ -z "${TG_CHAT_ID:-}" ] || [ ! -s "${TG_TOKEN_FILE:-/nonexistent}" ]; then
        say "КАНАЛ НЕ НАСТРОЕН — нарушения гигиены владельцу НЕ ушли"
        return 1
    fi
    local token; token=$(cat "$TG_TOKEN_FILE")
    # URL с токеном — через stdin (curl -K -), не в argv: argv виден любому в ps.
    printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" \
        | curl -s -m 20 -K - \
            -d chat_id="$TG_CHAT_ID" --data-urlencode "text=$text" \
        | jq -e '.ok == true' >/dev/null
}

CHECK="$(cd "$(dirname "$0")" && pwd)/server_hygiene_check.py"
[ -f "$CHECK" ] || { say "нет $CHECK — раскладка харнеса неполна"; exit 2; }

RC=0
REPORT=$(INSTALL_CONF="$INSTALL_CONF" HARNESS_CONF="$HARNESS_CONF" python3 "$CHECK" 2>&1) || RC=$?
printf '%s\n' "$REPORT"

case "$RC" in
    0)
        say "гигиена чиста"
        touch "$HEARTBEAT_DIR/server-hygiene"
        ;;
    1)
        say "нарушения найдены — шлю владельцу"
        if tg_send "Гигиена сервера: есть нарушения.
$REPORT"; then
            say "доставлено (ok:true)"
        else
            # Д-2: без этой строки утренняя сводка назвала бы демона мёртвым,
            # хотя проверка отработала — не доставил КАНАЛ. Метки всё равно
            # нет намеренно: недоставка должна быть видна, а не проглочена.
            say "нарушения найдены, но канал НЕ доставил — виноват канал, не проверка; нарушения видны только в логе"
            exit 1
        fi
        # Проверка отработала честно — метка ставится: страховка жива,
        # даже когда машина грязная.
        touch "$HEARTBEAT_DIR/server-hygiene"
        ;;
    *)
        # Код ≥2 — сломана САМА проверка, это не «нарушения»: метки НЕТ
        # (heartbeat-watch назовёт демона завтра), а владельцу — сигнал сразу:
        # пока проверка лежит, нарушения гигиены не ловит никто.
        say "проверка сама сломана (код $RC) — метку не ставлю, зову владельца"
        if tg_send "Гигиена сервера: проверка СЛОМАНА (код $RC), нарушения сейчас никто не ловит.
Traceback — в $LOG_DIR/server-hygiene.log; чинить: harness/demons/server_hygiene_check.py"; then
            say "сигнал о поломке доставлен (ok:true)"
        else
            say "сигнал о поломке НЕ доставлен — виден только в логе"
        fi
        exit "$RC"
        ;;
esac
