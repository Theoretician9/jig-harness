#!/usr/bin/env bash
#
# obnovlenie-watch.sh — раз в сутки спросить публичное хранилище: вышла ли
# новая версия харнеса, и поступить по режиму владельца.
#
# Что держит: установленный харнес не отстаёт от публичной ветки молча.
# Владелец 11.09.2026: «А как сделать возможность ручного-автоматического
# обновления после изменений в гите публичном где это будет лежать?»
#
# Режим — ключ UPDATE_MODE в harness.conf (ручка на панели):
#   manual — только сказать владельцу (умолчание: чужую машину не трогаем сами);
#   ask    — спросить в боте, поставить по ответу;
#   auto   — поставить сразу и отчитаться.
#
# Чем проверяется: --selftest (решение по режиму — чистой функцией) плюс
# проверка ядра scripts/test_obnovlenie.py (24 пути).
#
# Запуск: cron, раз в сутки; метка heartbeat — obnovlenie-watch.
set -uo pipefail
export LC_ALL=C.UTF-8

INSTALL_CONF="${HARNESS_INSTALL_CONF:-/etc/harness/install.conf}"
HARNESS_CONF="${HARNESS_CONF:-/etc/harness/harness.conf}"
# Окружение старше конфига: общий загрузчик вместо голого source (улика
# 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/konf.sh"
konf_zagruzit

PROJECT_DIR="${PROJECT_DIR:?пуст PROJECT_DIR — не прочитан паспорт установки}"
LOG_DIR="${LOG_DIR:-/var/log/harness}"
HEARTBEAT_DIR="${HEARTBEAT_DIR:-/var/lib/harness/heartbeat}"
UPDATE_MODE="${UPDATE_MODE:-manual}"
YADRO="$PROJECT_DIR/scripts/obnovlenie.py"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# Что делать при найденном обновлении — РЕШЕНИЕ отдельно от действия: его и
# проверяет самотест, не трогая ни сети, ни файлов.
reshenie_po_rezhimu() {   # $1=режим  $2=есть ли новое (1/0) → слово
    case "$2:$1" in
        0:*)        echo "молчать" ;;
        1:auto)     echo "ставить" ;;
        1:ask)      echo "спросить" ;;
        1:manual)   echo "сказать" ;;
        1:*)        echo "сказать" ;;   # незнакомый режим — самый безопасный
    esac
}

if [ "${1:-}" = "--selftest" ]; then
    ok=1
    RESULT=$(reshenie_po_rezhimu auto 1);   [ "$RESULT" = "ставить" ]  || { echo "  ПЛОХО auto"; ok=0; }
    echo "  ок    режим «само» ставит"
    RESULT=$(reshenie_po_rezhimu ask 1);    [ "$RESULT" = "спросить" ] || { echo "  ПЛОХО ask"; ok=0; }
    echo "  ок    режим «спросить» идёт в бот"
    RESULT=$(reshenie_po_rezhimu manual 1); [ "$RESULT" = "сказать" ]  || { echo "  ПЛОХО manual"; ok=0; }
    echo "  ок    режим «только сказать» ничего не ставит"
    RESULT=$(reshenie_po_rezhimu auto 0);   [ "$RESULT" = "молчать" ]  || { echo "  ПЛОХО нет нового"; ok=0; }
    echo "  ок    БОЛЬНОЙ СЛУЧАЙ: нового нет — молчим даже в режиме «само»"
    RESULT=$(reshenie_po_rezhimu vydumannyj 1); [ "$RESULT" = "сказать" ] || { echo "  ПЛОХО незнакомый режим"; ok=0; }
    echo "  ок    БОЛЬНОЙ СЛУЧАЙ: незнакомый режим не ставит ничего сам"
    [ "$ok" = 1 ] && { echo "SELFTEST: зелёный (5 путей, два больных)"; exit 0; }
    echo "SELFTEST: КРАСНЫЙ"; exit 1
fi

[ -f "$YADRO" ] || { say "ядра обновления нет: $YADRO"; exit 1; }

ANSWER=$(python3 "$YADRO" --сверить 2>&1)
RC=$?
if [ "$RC" != 0 ]; then
    # Недоступное хранилище — не повод будить владельца каждые сутки: пишем в
    # журнал, метку ставим (демон жив), молчим.
    say "спросить обновления не удалось: $ANSWER"
    touch "$HEARTBEAT_DIR/obnovlenie-watch"
    exit 0
fi

HAS_NEW=0
printf '%s' "$ANSWER" | grep -q "есть новая версия" && HAS_NEW=1
say "$ANSWER (режим $UPDATE_MODE)"

case "$(reshenie_po_rezhimu "$UPDATE_MODE" "$HAS_NEW")" in
    молчать)
        : ;;
    сказать)
        "$PROJECT_DIR/scripts/tg_send.sh" "Вышла новая версия харнеса. $ANSWER
Поставить: напиши «обновить». Сейчас стоит режим «только сказать мне»." >/dev/null || true ;;
    спросить)
        "$PROJECT_DIR/scripts/tg_send.sh" "Вышла новая версия харнеса. $ANSWER
Поставить? Ответь «обновить» — поставлю и проверю; если новая версия окажется сломанной, вернусь на прежнюю сам." >/dev/null || true ;;
    ставить)
        say "режим auto: ставлю"
        python3 "$YADRO" --поставить 2>&1 | while IFS= read -r stroka; do say "$stroka"; done ;;
esac

touch "$HEARTBEAT_DIR/obnovlenie-watch"
