#!/usr/bin/env bash
#
# resource-report.sh — обёртка cron над resource_report.py: раз в сутки шлёт
# владельцу одно короткое сообщение, на что уходят процессор, память и диск.
#
# Зачем: владелец 04.11.2026 — «по нагрузке сервера надо сделать регулярный
# мониторинг и мне скидывать раз в день на что ресурсы уходят». Нарушения
# потолков ловит server-hygiene; это сведение для глаз, без задач модели.
#
# Время отправки — RESOURCE_REPORT_TIME в harness.conf (по умолчанию 09:00).
# Cron зовёт обёртку каждые 5 минут, а решает она сама: время ещё не пришло или
# сегодня уже слали — выходит молча. Так время остаётся данными, а не строкой cron.
# Тот же частый проход каждый раз дописывает выборку для итога за сутки.
#
# Режимы:
#   --selftest  самотест сбора мимо канала и метки;
#   --dry-run   печатает сообщение, не шлёт, метку, отметку дня и выборки не трогает;
#   --force     шлёт сейчас мимо часа и отметки дня (живая проверка); после
#               доставки ставит отметку СЕГОДНЯШНЕЙ датой, как обычный проход:
#               сводка за день ушла, и вторая в тот же день была бы повтором;
#   (без ключей) обычный прогон по часам.
#
# Чем доказывается: --dry-run показывает сообщение; на боевом — запись
# «доставлено (ok:true)» в $LOG_DIR/resource-report.log и метка свежести.
set -euo pipefail

# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/config.sh"
HARNESS_CONF="${HARNESS_CONF:-$(konf_koren)/harness.conf}"
[ -r "$HARNESS_CONF" ] || { echo "resource-report: нет $HARNESS_CONF — скопируйте harness/config/harness.conf в $(konf_koren)/"; exit 1; }
konf_zagruzit
: "${HEARTBEAT_DIR:?пуст HEARTBEAT_DIR}" "${LOG_DIR:?пуст LOG_DIR}" "${PROJECT_DIR:?пуст PROJECT_DIR}"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

REPORT_PY="$(cd "$(dirname "$0")" && pwd)/resource_report.py"

# Самотест — мимо метки и канала ([[the-probe-played-on-production]]).
if [ "${1:-}" = "--selftest" ]; then
    exec python3 "$REPORT_PY" --selftest
fi
[ -f "$REPORT_PY" ] || { say "нет $REPORT_PY — раскладка харнеса неполна"; exit 2; }

if [ "${1:-}" = "--dry-run" ]; then
    exec python3 "$REPORT_PY"
fi

LOCK_FILE="$LOG_DIR/locks/${SERVICE_LOCKS_SUBDIR:-services}/resource-report.lock"
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>>"$LOCK_FILE"
flock -n 9 || { say "прогон уже идёт (лок $LOCK_FILE) — выхожу"; exit 0; }

# Выборка на каждом проходе, до проверки времени: итог за сутки строится по ним.
# Сбой выборки отправку не срывает: сводка уйдёт, просто с дырой в итоге.
LOG_DIR="$LOG_DIR" python3 "$REPORT_PY" --sample || say "выборка не записана (код $?) — иду дальше"

# Метка ставится на каждом проходе: она говорит «демон жив», а не «сегодня
# отправлено»; несостоявшуюся отправку видно по журналу и по коду выхода.
touch_mark() { touch "$HEARTBEAT_DIR/resource-report"; }

SEND_AT="${RESOURCE_REPORT_TIME:-09:00}"
# «9:00» и «08:05» приводятся к минутам суток; 10# — чтобы «08» не стало восьмеричным.
SEND_MIN=$(( 10#${SEND_AT%%:*} * 60 + 10#${SEND_AT##*:} ))
NOW_MIN=$(( 10#$(date +%H) * 60 + 10#$(date +%M) ))
SENT_FILE="$LOG_DIR/resource-report.sent"
TODAY=$(date +%F)
if [ "${1:-}" != "--force" ]; then
    if [ "$NOW_MIN" -lt "$SEND_MIN" ]; then
        touch_mark; exit 0
    fi
    if [ "$(cat "$SENT_FILE" 2>/dev/null || true)" = "$TODAY" ]; then
        touch_mark; exit 0
    fi
fi

# Запуском считается только настоящая отправка: пустые проходы до часа и после отметки дня выше.
harness_run resource-report
trap 'rc=$?; harness_run_end "$rc"' EXIT

RC=0
TEXT=$(python3 "$REPORT_PY" 2>&1) || RC=$?
if [ "$RC" -ne 0 ]; then
    say "сбор сломан (код $RC) — метку не ставлю"
    printf '%s\n' "$TEXT"
    exit "$RC"
fi
printf '%s\n' "$TEXT"

# Недельный замер диспетчера процессов — в библиотеку, раз в сутки вместе с отправкой.
# Сбой замера отправку не срывает.
python3 "$PROJECT_DIR/scripts/process-dispatcher.py" --в-библиотеку || say "замер в библиотеку не внесён (код $?)"

# Дверь канала зовётся без ключей: при лимите она ставит весть в очередь
# досыла и отвечает нулём; обхода двери нет — сведение не срочное.
if "$PROJECT_DIR/scripts/tg_send.sh" "$TEXT"; then
    say "доставлено (ok:true)"
    printf '%s\n' "$TODAY" > "$SENT_FILE"
    touch_mark
else
    say "канал не доставил — повторю на следующем проходе"
    exit 1
fi
