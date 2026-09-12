#!/usr/bin/env bash
#
# rasshirenija-watch.sh — набор расширений держит ДЕМОН, а не руки.
#
# Владелец 12.09.2026: «Команды установки/удаления плагинов и скилов не для
# человека, а для демонов и хуков». До этого список расширений жил текстом
# скила со слэш-формами (`/plugin install …`) — их вводит человек в терминале,
# которого владелец не видит. Замер того же дня: расширений установлено 0,
# следов установки за месяц 0. Носитель был мёртв по устройству.
#
# Демон делает ровно одно: сводит ФАКТ с ДАННЫМИ (harness/config/расширения.yaml)
# — ставит недостающее из базового набора, снимает запрещённое вердиктом.
# Вся логика в scripts/rasshirenija.py, здесь только расписание, доклад и метка:
# две копии одной логики разъезжаются на первой правке.
#
# В канал пишет ТОЛЬКО когда что-то изменилось: сигнал, истинный каждый прогон,
# сведений не несёт ([[сигнал-без-различения]]).
#
# Запуск: cron, раз в сутки, от имени $AGENT_USER. Служебное: --selftest.
set -euo pipefail

say() { printf '%s rasshirenija-watch: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# ── самотест: больной случай на подменённом каталоге настроек ───────────────
if [ "${1:-}" = "--selftest" ]; then
    T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
    mkdir -p "$T/hb" "$T/plugins"
    D="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
    SRC="$D/../../scripts/rasshirenija.py"
    [ -r "$SRC" ] || SRC="$D/../scripts/rasshirenija.py"
    [ -r "$SRC" ] || { echo "САМОТЕСТ ПРОВАЛЕН: не нашёл rasshirenija.py рядом с демоном"; exit 1; }

    # Ставить и снимать по-настоящему самотест не имеет права: он бы менял
    # рабочую машину. Поэтому подменяется КАТАЛОГ НАСТРОЕК — тот же путь, что
    # читает боевой прогон, а не отдельная ветка кода.
    printf '{"version": 2, "plugins": {}}\n' > "$T/plugins/installed_plugins.json"
    OUT=$(CLAUDE_CONFIG_DIR="$T" python3 "$SRC" --selftest) || {
        echo "САМОТЕСТ ПРОВАЛЕН: самотест механизма красный"; echo "$OUT"; exit 1; }
    echo "$OUT" | tail -1 | grep -q 'неудач 0' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: $(echo "$OUT" | tail -1)"; exit 1; }

    # БОЛЬНОЙ СЛУЧАЙ: нет claude в PATH — демон обязан назвать причину, а не
    # молча отчитаться об успехе (проба с ожиданием «код не ноль» зеленеет и
    # от 127 «команды нет», поэтому требуется СЛОВО причины).
    # PATH без claude, но С python3: пустой PATH убил бы сам интерпретатор, и
    # проба зеленела бы от «python3: command not found» — не от того отказа.
    mkdir -p "$T/bin"; ln -sf "$(command -v python3)" "$T/bin/python3"
    ANSWER=$(CLAUDE_CONFIG_DIR="$T" PATH="$T/bin" python3 "$SRC" поставить 'нет@такого' 2>&1 || true)
    echo "$ANSWER" | grep -q 'claude не найден' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: отказ без причины: $ANSWER"; exit 1; }

    echo "САМОТЕСТ ПРОЙДЕН: механизм зелёный, отказ без claude назван словом"
    exit 0
fi

# ── конфиги ─────────────────────────────────────────────────────────────────
INSTALL_CONF="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-/etc/harness/install.conf}}"
HARNESS_CONF="${HARNESS_CONF:-/etc/harness/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "rasshirenija-watch: нет $INSTALL_CONF — установка не завершена"; exit 1; }
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/konf.sh"
konf_zagruzit
: "${PROJECT_DIR:?пуст PROJECT_DIR}" "${HEARTBEAT_DIR:?пуст HEARTBEAT_DIR}"
LOG_DIR="${LOG_DIR:-/var/log/harness}"
mkdir -p "$HEARTBEAT_DIR"

RESULT=$(python3 "$PROJECT_DIR/scripts/rasshirenija.py" свести 2>&1) || RC=$?
RC="${RC:-0}"
say "$RESULT"

PUT=$(printf '%s' "$RESULT" | grep -o 'поставлено [0-9]*' | grep -o '[0-9]*' || true)
REMOVED=$(printf '%s' "$RESULT" | grep -o 'снято [0-9]*' | grep -o '[0-9]*' || true)
CHANGED=$(( ${PUT:-0} + ${REMOVED:-0} ))

# Доклад владельцу только при изменении или отказе: набор на месте — молчим.
if [ "$CHANGED" -gt 0 ] || [ "$RC" -ne 0 ]; then
    MSG="Набор расширений: поставлено ${PUT:-0}, снято ${REMOVED:-0}."
    [ "$RC" -ne 0 ] && MSG="$MSG Есть отказ, смотрю: $(printf '%s' "$RESULT" | tail -1)"
    bash "$PROJECT_DIR/scripts/tg_send.sh" "$MSG" >/dev/null 2>&1 || \
        say "доклад в канал не ушёл — смотри $LOG_DIR/tg_send.log"
fi

touch "$HEARTBEAT_DIR/rasshirenija-watch"
exit "$RC"
