#!/usr/bin/env bash
#
# nightly-gates.sh — ночной ПОЛНЫЙ прогон ворот (`gates.sh --всё`).
#
# Что держит. Обычные ворота гоняются на каждом коммите, а самотесты демонов
# живут за флагом `--всё`, который по расписанию не звал никто. Улика
# 21.09.2026: самотест ревизии был красным СЕМЬЮ путями из десяти восемь суток
# подряд, и никто этого не видел — сам гейт в обычный прогон не входил, а флаг
# «--всё» вспоминался руками. Механизм, который держится тем, что кто-то
# вспомнит его запустить, мёртв ([[a-mechanism-that-needs-a-human-is-dead]]).
#
# Что делает: гонит полный прогон, кладёт итог в журнал, ставит метку свежести
# и говорит владельцу ТОЛЬКО о красном. Зелёный молчит: пульс виден по метке,
# а ночное «всё хорошо» в канале — шум (владелец 11.09: «не надо вываливать
# кучу своих рассуждений»).
#
# Чем доказывается: `--selftest` — семь путей на подставном прогоне, среди них
# три больных (красный доходит до владельца, зелёный не дёргает, оборванный
# прогон не считается чистым).
#
# Предел: прогон полного набора вырос до ~15 ч, и без предела он наложился бы
# на следующий cron; ключ NIGHTLY_GATES_MAX_MIN (минуты), по пределу — отказ с
# именем последнего гейта.
#
# Запуск: cron, ночью. Метка присмотра: nightly-gates (её срок — ключ
# HEARTBEAT_PERIODS в harness.conf; без срока сторож меток демона не ждёт).
#
# Весь скрипт — одна группа { … }: bash разбирает группу ЦЕЛИКОМ до исполнения,
# и правка файла во время многочасового прогона идущий прогон не задевает
# (оболочка читает скрипт по байтовому смещению —
# [[editing-a-running-bash-script]]). Последняя строка группы — exit: после
# неё оболочка к файлу не возвращается.
{ # весь скрипт в памяти до запуска
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/../../scripts/lib/config.sh"
INSTALL_CONF="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-$(konf_koren)/install.conf}}"
HARNESS_CONF="${HARNESS_CONF:-$(konf_koren)/harness.conf}"
konf_zagruzit

NIGHTLY_GATES_MAX_MIN="${NIGHTLY_GATES_MAX_MIN:-1200}"
PROJECT_DIR="${PROJECT_DIR:-}"
GATES_CMD="${NIGHTLY_GATES_CMD:-bash $PROJECT_DIR/scripts/gates.sh --всё}"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# Зелёное гасит задачу модели. Писателя может не быть (раскладка неполна) —
# сторож от этого не падает, а гасит ли карточка, видно по её журналу.
alert_task() {  # $@ = доводы писателя: --resolve <ключ>
    [ -f "$PROJECT_DIR/scripts/alert-task.py" ] || return 0
    python3 "$PROJECT_DIR/scripts/alert-task.py" "$@" </dev/null >/dev/null 2>&1 || true
}

kanal() {  # $1 = текст владельцу, дальше --task <ключ задачи модели>; молчание канала не роняет прогон
    # Исход отправки ПИШЕТСЯ в журнал. Улика 21.09.2026: первый живой прогон
    # нашёл три красных гейта, а в журнале после итоговой строки не было
    # НИЧЕГО — отправил демон владельцу или промолчал, узнать было нечем.
    # Молчание неотличимо от отправки, а для канала это и есть главный вопрос.
    if [ ! -x "$PROJECT_DIR/scripts/tg_send.sh" ]; then
        say "канал: отправителя нет ($PROJECT_DIR/scripts/tg_send.sh) — владелец НЕ извещён"
        return 1
    fi
    if bash "$PROJECT_DIR/scripts/tg_send.sh" "$@" >/dev/null 2>&1; then
        say "канал: отправлено владельцу"
        return 0
    fi
    say "канал: ОТКАЗ отправки — владелец НЕ извещён (причина в $LOG_DIR/tg_failures.log)"
    return 1
}

itog_stroka() {  # $1 = журнал прогона; печатает строку итога или пустую
    grep -E '^=== итог' "$1" 2>/dev/null | tail -1
}

krasnyh_v() {  # $1 = строка итога; печатает число красных (пусто, если не разобрал)
    printf '%s' "$1" | sed -n 's/.*красных \([0-9]\+\).*/\1/p' | head -1
}

# Любой выход боевого пути без вердикта — сигнал «прогон закончился без итога»:
# оборванный прогон, падение самого демона (unbound variable, чужая строка) —
# всё сюда, в одно место. «Не смог проверить» и «чисто» — разные ответы
# ([[unreadable-source-turns-check-green]]).
VERDICT=""
report_no_summary() {  # $1 = код выхода демона
    [ -n "$VERDICT" ] && return 0
    say "ОТКАЗ: прогон закончился без итога (код демона $1), журнал $ZHURNAL"
    kanal "Ночной прогон ворот оборвался — итоговой строки нет. Журнал: $(basename "$ZHURNAL")" --task nightly-gates:broken
}

# ── самотест ────────────────────────────────────────────────────────────────
if [ "${1:-}" = "--selftest" ]; then
    T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
    ok=1
    proba() {  # $1 = имя, $2 = ждём, $3 = вышло
        if [ "$2" = "$3" ]; then printf '  ок    %s\n' "$1"
        else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$3"; ok=0; fi
    }
    printf '=== итог: зелёных 105, красных 0 ===\n' > "$T/zelenyj.log"
    printf '=== итог: зелёных 99, красных 6 ===\n' > "$T/krasnyj.log"
    : > "$T/pustoj.log"

    # БОЛЬНОЙ СЛУЧАЙ: прогон КРАСНЫЙ — владелец обязан узнать.
    proba "красный прогон разобран" "6" "$(krasnyh_v "$(itog_stroka "$T/krasnyj.log")")"
    proba "зелёный прогон разобран" "0" "$(krasnyh_v "$(itog_stroka "$T/zelenyj.log")")"
    # БОЛЬНОЙ СЛУЧАЙ: прогон оборвался и итога нет — это НЕ «зелёно».
    proba "оборванный прогон: итога нет" "" "$(itog_stroka "$T/pustoj.log")"

    # Живой путь: подставной прогон, своя метка, свой канал.
    mkdir -p "$T/hb" "$T/proekt/scripts"
    cat > "$T/proekt/scripts/tg_send.sh" <<'TG'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TG_LOG"
TG
    chmod +x "$T/proekt/scripts/tg_send.sh"
    progon() {  # $1 = что печатает подставной прогон
        : > "$T/tg.log"
        env HEARTBEAT_DIR="$T/hb" LOG_DIR="$T" PROJECT_DIR="$T/proekt" \
            TG_LOG="$T/tg.log" NIGHTLY_GATES_CMD="printf '$1\n'" \
            HARNESS_INSTALL_CONF=/dev/null HARNESS_CONF=/dev/null \
            bash "$(readlink -f "${BASH_SOURCE[0]}")" > "$T/progon.log" 2>&1
    }
    progon '=== итог: зелёных 105, красных 0 ==='
    proba "БОЛЬНОЙ: зелёный прогон владельца НЕ дёргает" "" "$(cat "$T/tg.log")"
    proba "зелёный прогон ставит метку" "есть" \
          "$([ -s "$T/hb/nightly-gates" ] && echo есть || echo нет)"
    rm -f "$T/hb/nightly-gates"
    progon '=== итог: зелёных 99, красных 6 ==='
    proba "БОЛЬНОЙ: красный прогон доходит до владельца" "да" \
          "$(grep -q 'красных 6' "$T/tg.log" && echo да || echo нет)"
    # Метка ставится и на красном: демон ОТРАБОТАЛ, а красное — состояние ворот,
    # а не молчание демона. Иначе сторож меток закричал бы про мёртвый демон
    # поверх настоящей беды и увёл бы от неё.
    proba "красный прогон тоже ставит метку" "есть" \
          "$([ -s "$T/hb/nightly-gates" ] && echo есть || echo нет)"
    proba "исход отправки виден в журнале" "да" \
          "$(grep -q 'канал: отправлено владельцу' "$T/progon.log" && echo да || echo нет)"
    # БОЛЬНОЙ СЛУЧАЙ: отправитель отказал — журнал обязан сказать, что владелец
    # НЕ извещён. Иначе молчание журнала читается как «сказано».
    cat > "$T/proekt/scripts/tg_send.sh" <<'TGBAD'
#!/usr/bin/env bash
exit 1
TGBAD
    chmod +x "$T/proekt/scripts/tg_send.sh"
    progon '=== итог: зелёных 99, красных 6 ==='
    proba "БОЛЬНОЙ: отказ отправки назван, а не проглочен" "да" \
          "$(grep -q 'ОТКАЗ отправки' "$T/progon.log" && echo да || echo нет)"

    [ "$ok" = 1 ] && { echo "САМОТЕСТ ПРОЙДЕН: 9 путей, из них 4 больных"; exit 0; }
    echo "САМОТЕСТ ПРОВАЛЕН"; exit 1
fi

# ── боевой путь ─────────────────────────────────────────────────────────────
: "${PROJECT_DIR:?пуст PROJECT_DIR}"
ZHURNAL="$LOG_DIR/nightly-gates-$(date +%Y%m%d).log"
say "полный прогон ворот: $GATES_CMD"
START_TS=$(date +%s)
trap 'report_no_summary $?' EXIT
timeout -k 60 "${NIGHTLY_GATES_MAX_MIN}m" bash -c "cd '$PROJECT_DIR' && $GATES_CMD" > "$ZHURNAL" 2>&1
RC=$?

ITOG="$(itog_stroka "$ZHURNAL")"
mkdir -p "$HEARTBEAT_DIR"
date -Is > "$HEARTBEAT_DIR/nightly-gates"

if [ -z "$ITOG" ] && { [ "$RC" = 124 ] || [ "$RC" = 137 ]; }; then
    LAST_GATE="$(grep -E '^  (ок|ПЛОХО|жёлтый) · ' "$ZHURNAL" | tail -1 | sed -E 's/^  (ок|ПЛОХО|жёлтый) · //; s/ (—|\(код).*//')"
    VERDICT=limit
    say "ОТКАЗ: прогон упёрся в предел ${NIGHTLY_GATES_MAX_MIN} мин, последний гейт: ${LAST_GATE:-нет}"
    kanal "Ночной прогон ворот упёрся в предел ${NIGHTLY_GATES_MAX_MIN} мин и остановлен. Последний гейт: ${LAST_GATE:-нет}. Журнал: $(basename "$ZHURNAL")" --task nightly-gates:broken
    exit 1
fi

# Итога нет — выход без вердикта, сигнал даёт report_no_summary.
[ -n "$ITOG" ] || exit 1

VERDICT=summary
say "$ITOG за $(( ($(date +%s) - START_TS) / 60 )) мин"
# broken = «прогон оборван»: итог получен, значит оборван не был, при любом числе красных;
# red — отдельная тревога, её снимает только прогон без красных.
alert_task --resolve nightly-gates:broken
KRASNYH="$(krasnyh_v "$ITOG")"
if [ "${KRASNYH:-0}" -gt 0 ]; then
    IMENA="$(grep -E '^  ПЛОХО ·' "$ZHURNAL" | sed 's/^  ПЛОХО · //; s/ (код.*//' | paste -sd', ' -)"
    kanal "Ночной прогон ворот: $ITOG
Красные: ${IMENA:-не разобрал}
Журнал: $(basename "$ZHURNAL")" --task nightly-gates:red
else
    say "красных нет — владельца не беспокою"
    alert_task --resolve nightly-gates:red
fi
exit 0
}
