#!/usr/bin/env bash
#
# update-watch.sh — раз в сутки спросить публичное хранилище: вышла ли
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
# Чем проверяется: --selftest (решение по режиму — чистой функцией; демон
# целиком на стенде через настоящее ядро) плюс проверка ядра
# scripts/test_update.py.
#
# Запуск: cron, раз в сутки; метка heartbeat — update-watch.
set -uo pipefail
export LC_ALL=C.UTF-8

# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/config.sh"
INSTALL_CONF="${HARNESS_INSTALL_CONF:-$(konf_koren)/install.conf}"
HARNESS_CONF="${HARNESS_CONF:-$(konf_koren)/harness.conf}"
# Окружение старше конфига: общий загрузчик вместо голого source (улика
# 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
konf_zagruzit

PROJECT_DIR="${PROJECT_DIR:?пуст PROJECT_DIR — не прочитан паспорт установки}"
UPDATE_MODE="${UPDATE_MODE:-manual}"
YADRO="$PROJECT_DIR/scripts/update.py"

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

    # Демон гоняется целиком на стенде, ядро — НАСТОЯЩЕЕ (подменён только ответ
    # хранилища): код «есть новое» записан и в ядре, и в демоне, и только
    # прогон через ядро ловит их расхождение.
    stend=$(mktemp -d); trap 'rm -rf "$stend"' EXIT
    mkdir -p "$stend/scripts" "$stend/hb"
    printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >> "%s/kanal"\n' "$stend" > "$stend/scripts/tg_send.sh"
    chmod +x "$stend/scripts/tg_send.sh"
    yadro_stenda() { # $1 = python-выражение: ответ хранилища; $2 = чем ответить ядру
        cat > "$stend/scripts/update.py" <<PY
import importlib.util, sys
спец = importlib.util.spec_from_file_location("ядро", "$YADRO_BOEVOE")
ядро = importlib.util.module_from_spec(спец)
спец.loader.exec_module(ядро)
ядро.спросить_обновления = lambda: $1
$2
PY
        rm -f "$stend/kanal" "$stend/hb/update-watch"
        PROJECT_DIR="$stend" HEARTBEAT_DIR="$stend/hb" UPDATE_MODE=manual \
            bash "$0" > "$stend/zhurnal" 2>&1
    }
    YADRO_BOEVOE="$(dirname "$(readlink -f "$0")")/../../scripts/update.py"
    yadro_stenda '{"есть новое": True, "коммит": "a" * 40, "дата": "2026-10-14"}' \
        'sys.exit(ядро.main(["--сверить"]))'
    if grep -q "Вышла новая версия" "$stend/kanal" 2>/dev/null; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: ядро ответило «есть новое» — владелец узнал"
    else echo "  ПЛОХО код ядра «есть новое» демон не узнал: $(cat "$stend/zhurnal")"; ok=0; fi
    yadro_stenda 'None' 'print("совсем другая формулировка"); sys.exit(ядро.КОД_ЕСТЬ_НОВОЕ)'
    if grep -q "Вышла новая версия" "$stend/kanal" 2>/dev/null; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: другая фраза с кодом «есть новое» — весть не погасла"
    else echo "  ПЛОХО другая фраза ядра погасила весть: $(cat "$stend/zhurnal")"; ok=0; fi
    yadro_stenda '{"есть новое": False, "коммит": "b" * 40}' 'sys.exit(ядро.main(["--сверить"]))'
    if [ ! -e "$stend/kanal" ]; then
        echo "  ок    нового нет — молчим"
    else echo "  ПЛОХО весть ушла, когда нового нет"; ok=0; fi
    yadro_stenda '{"беда": "хранилище недоступно"}' 'sys.exit(ядро.main(["--сверить"]))'
    if [ ! -e "$stend/kanal" ] && [ -e "$stend/hb/update-watch" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: спросить не удалось — молчим, метка жива"
    else echo "  ПЛОХО отказ хранилища: весть $([ -e "$stend/kanal" ] && echo ушла || echo нет), метка $([ -e "$stend/hb/update-watch" ] && echo есть || echo нет)"; ok=0; fi
    [ "$ok" = 1 ] && { echo "SELFTEST: зелёный (9 путей, пять больных)"; exit 0; }
    echo "SELFTEST: КРАСНЫЙ"; exit 1
fi

[ -f "$YADRO" ] || { say "ядра обновления нет: $YADRO"; exit 1; }

ANSWER=$(python3 "$YADRO" --сверить 2>&1)
RC=$?
# Исход — по коду возврата ядра (КОД_ЕСТЬ_НОВОЕ в update.py), а не по словам
# вывода: формулировка ядра меняется свободно. Совпадение кода с ядром держит
# самотест — он гоняет демон через настоящее ядро.
case "$RC" in
    0) HAS_NEW=0 ;;
    3) HAS_NEW=1 ;;
    *)
        # Недоступное хранилище — не повод будить владельца каждые сутки: пишем в
        # журнал, метку ставим (демон жив), молчим.
        say "спросить обновления не удалось (код $RC): $ANSWER"
        touch "$HEARTBEAT_DIR/update-watch"
        exit 0 ;;
esac
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

touch "$HEARTBEAT_DIR/update-watch"
