#!/usr/bin/env bash
#
# nightly-mutations.sh — недельный полный прогон мутационной пробы (`mutation-probe.sh`)
# отдельно от полных ворот, со своим пределом и своим журналом.
#
# Зачем отдельно. Гейт «мутации» съедал 86-94 % ночного прогона ворот (17 ч из
# предела 20 ч), и остальные ~37 проверок `--всё` не судились три ночи подряд.
# Разбор: .tasks/hr.nightly-gates-limit/разбор.md. Теперь у проб свой предел
# (NIGHTLY_MUTATIONS_MAX_MIN), а ворота `--всё` бегут без них.
#
# Вердикт берётся из СТРОКИ ИТОГА прибора («=== мутационная проба: … дыр M …
# ==="), а не из кода выхода: код прибора равен числу дыр и бывает 124.
# Владельцу говорим о дырах и об обрыве; зелёное молчит, пульс виден по метке.
#
# Не чаще раза в NIGHTLY_MUTATIONS_ALERT_GAP_H часов: красное держится неделями,
# и одна и та же весть при каждом прогоне — шум. Штамп — LOG_DIR/nightly-mutations.alert-ts
# (эпоха последней ОТПРАВЛЕННОЙ вести; неудачная отправка штамп не обновляет).
# Штампов два: alert-ts — для обрыва (broken), alert-red-ts — для дыр (red).
#
# Запуск: cron, раз в неделю (вторник 09:00). Метка присмотра: nightly-mutations (срок — ключ
# HEARTBEAT_PERIODS в harness.conf).
#
# Весь скрипт — одна группа { … }: bash разбирает группу ЦЕЛИКОМ до исполнения,
# и правка файла во время многочасового прогона идущий прогон не задевает
# ([[editing-a-running-bash-script]]). Последняя строка группы — exit.
{ # весь скрипт в памяти до запуска
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/../../scripts/lib/config.sh"
INSTALL_CONF="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-$(konf_koren)/install.conf}}"
HARNESS_CONF="${HARNESS_CONF:-$(konf_koren)/harness.conf}"
konf_zagruzit

NIGHTLY_MUTATIONS_MAX_MIN="${NIGHTLY_MUTATIONS_MAX_MIN:-1140}"
NIGHTLY_MUTATIONS_ALERT_GAP_H="${NIGHTLY_MUTATIONS_ALERT_GAP_H:-24}"
# Прибор должен кончить сам за столько минут до жёсткого предела: убитый
# пределом прибор не печатает итога, и прогон выглядит обрывом.
MUTATION_SOFT_STOP_MIN_BEFORE="${MUTATION_SOFT_STOP_MIN_BEFORE:-15}"
PROJECT_DIR="${PROJECT_DIR:-}"
MUT_CMD="${NIGHTLY_MUTATIONS_CMD:-bash $PROJECT_DIR/scripts/mutation-probe.sh}"
# Пауза вестей у каждого рода своя: весть об обрыве не глушит весть о дырах.
STAMP_BROKEN="${LOG_DIR:-}/nightly-mutations.alert-ts"
STAMP_RED="${LOG_DIR:-}/nightly-mutations.alert-red-ts"
STAMP_YELLOW="${LOG_DIR:-}/nightly-mutations.alert-yellow-ts"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# Писателя задач может не быть (раскладка неполна) — сторож от этого не падает.
alert_task() {  # $@ = доводы писателя: --resolve <ключ>
    [ -f "$PROJECT_DIR/scripts/alert-task.py" ] || return 0
    python3 "$PROJECT_DIR/scripts/alert-task.py" "$@" </dev/null >/dev/null 2>&1 || true
}

kanal() {  # $1 = текст владельцу, дальше --task <ключ>; исход отправки пишется в журнал
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

stamp_fresh() {  # $1 = штамп; успех, если с прошлой отправленной вести прошло меньше GAP часов
    local ts
    ts="$(cat "$1" 2>/dev/null)"
    case "$ts" in ''|*[!0-9]*) return 1 ;; esac
    awk -v now="$(date +%s)" -v ts="$ts" -v gap="$NIGHTLY_MUTATIONS_ALERT_GAP_H" \
        'BEGIN { exit !((now - ts) < gap * 3600) }'
}

notify() {  # $1 = штамп рода вести, дальше как у kanal; единственный путь вести владельцу, с паузой между вестями
    local stamp="$1"; shift
    if stamp_fresh "$stamp"; then
        say "весть не шлю: прошлая была $(date -d "@$(cat "$stamp")" '+%Y-%m-%d %H:%M:%S'), пауза ${NIGHTLY_MUTATIONS_ALERT_GAP_H} ч"
        return 0
    fi
    kanal "$@" && date +%s > "$stamp"
    return 0
}

itog_stroka() {  # $1 = журнал прогона; печатает строку итога или пустую
    grep -E '=== мутационная проба: ' "$1" 2>/dev/null | tail -1
}

dyr_v() {  # $1 = строка итога; печатает число дыр (пусто, если не разобрал)
    printf '%s' "$1" | sed -n 's/.*дыр \([0-9]\+\).*/\1/p' | head -1
}

nedogon_v() {  # $1 = журнал прогона; печатает «K D» (не прогнано, из них выпало) или пусто
    grep -E '=== мутационная проба НЕДОГНАНА: ' "$1" 2>/dev/null | tail -1 \
        | sed -n 's/.*не прогнано \([0-9]\+\) из \([0-9]\+\) (по сроку [0-9]\+, выпало \([0-9]\+\)).*/\1 \3 \2/p'
}

# Любой выход боевого пути без вердикта — «прогон закончился без итога»: падение
# прибора и самого демона сходятся в одно место
# ([[unreadable-source-turns-check-green]]).
VERDICT=""
report_no_summary() {  # $1 = код выхода демона
    [ -n "$VERDICT" ] && return 0
    say "ОТКАЗ: прогон закончился без итога (код демона $1), журнал $ZHURNAL"
    notify "$STAMP_BROKEN" "Полный прогон мутаций закончился без итога. Журнал: $(basename "$ZHURNAL")" --task nightly-mutations:broken
}

# ── самотест ────────────────────────────────────────────────────────────────
if [ "${1:-}" = "--selftest" ]; then
    T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
    ok=1; total=0; sick=0
    proba() {  # $1 = имя, $2 = ждём, $3 = вышло
        total=$((total + 1))
        case "$1" in БОЛЬНОЙ*) sick=$((sick + 1)) ;; esac
        if [ "$2" = "$3" ]; then printf '  ок    %s\n' "$1"
        else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$3"; ok=0; fi
    }
    printf '2026-11-03 01:00:00  === мутационная проба: покраснели как надо 40, дыр 3, из кэша 7 ===\n' > "$T/dyry.log"
    printf '=== мутационная проба: покраснели как надо 43, дыр 0, из кэша 0 ===\n' > "$T/chisto.log"
    : > "$T/pustoj.log"

    proba "БОЛЬНОЙ: дыры из итога разобраны" "3" "$(dyr_v "$(itog_stroka "$T/dyry.log")")"
    proba "итог без дыр разобран" "0" "$(dyr_v "$(itog_stroka "$T/chisto.log")")"
    proba "БОЛЬНОЙ: пустой журнал — итога нет" "" "$(itog_stroka "$T/pustoj.log")"

    mkdir -p "$T/hb" "$T/proekt/scripts"
    cat > "$T/proekt/scripts/tg_send.sh" <<'TG'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TG_LOG"
TG
    chmod +x "$T/proekt/scripts/tg_send.sh"
    progon() {  # $1 = что печатает подставной прогон
        : > "$T/tg.log"
        env HEARTBEAT_DIR="$T/hb" LOG_DIR="$T" PROJECT_DIR="$T/proekt" \
            TG_LOG="$T/tg.log" NIGHTLY_MUTATIONS_CMD="printf '$1\n'" \
            HARNESS_INSTALL_CONF=/dev/null HARNESS_CONF=/dev/null \
            bash "$(readlink -f "${BASH_SOURCE[0]}")" > "$T/progon.log" 2>&1
    }
    progon '=== мутационная проба: покраснели как надо 43, дыр 0, из кэша 0 ==='
    proba "БОЛЬНОЙ: зелёный прогон владельца НЕ дёргает" "" "$(cat "$T/tg.log")"
    proba "зелёный прогон ставит метку" "есть" \
          "$([ -s "$T/hb/nightly-mutations" ] && echo есть || echo нет)"
    progon '=== мутационная проба: покраснели как надо 40, дыр 3, из кэша 0 ==='
    proba "БОЛЬНОЙ: красный прогон доходит до владельца" "да" \
          "$(grep -q 'дыр 3' "$T/tg.log" && echo да || echo нет)"
    proba "исход отправки виден в журнале" "да" \
          "$(grep -q 'канал: отправлено владельцу' "$T/progon.log" && echo да || echo нет)"
    proba "удачная отправка ставит штамп" "есть" \
          "$([ -s "$T/nightly-mutations.alert-red-ts" ] && echo есть || echo нет)"
    progon '=== мутационная проба: покраснели как надо 40, дыр 3, из кэша 0 ==='
    proba "БОЛЬНОЙ: второй красный при свежем штампе молчит" "" "$(cat "$T/tg.log")"
    proba "молчание названо в журнале" "да" \
          "$(grep -q 'весть не шлю' "$T/progon.log" && echo да || echo нет)"
    # Отказ отправки: журнал обязан сказать, что владелец НЕ извещён.
    rm -f "$T/nightly-mutations.alert-red-ts"
    printf '#!/usr/bin/env bash\nexit 1\n' > "$T/proekt/scripts/tg_send.sh"
    progon '=== мутационная проба: покраснели как надо 40, дыр 3, из кэша 0 ==='
    proba "БОЛЬНОЙ: отказ отправки назван, а не проглочен" "да" \
          "$(grep -q 'ОТКАЗ отправки' "$T/progon.log" && echo да || echo нет)"
    proba "БОЛЬНОЙ: отказ отправки штамп не ставит" "нет" \
          "$([ -e "$T/nightly-mutations.alert-red-ts" ] && echo есть || echo нет)"

    [ "$ok" = 1 ] && { echo "САМОТЕСТ ПРОЙДЕН: $total путей, из них $sick больных"; exit 0; }
    echo "САМОТЕСТ ПРОВАЛЕН"; exit 1
fi

# ── боевой путь ─────────────────────────────────────────────────────────────
: "${PROJECT_DIR:?пуст PROJECT_DIR}"
# Срок (предел и мягкая остановка) считается от старта процесса: ожидание замка его съедает.
START_TS=$(date +%s)
# Один прогон за раз: два прибора на одном кэше дыр портят друг другу итог.
# Лок стоит ДО harness_run — пропущенный дубль не запуск.
LOCK_FILE="$LOG_DIR/locks/${SERVICE_LOCKS_SUBDIR:-services}/nightly-mutations.lock"
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>>"$LOCK_FILE"
# Замок занят (чаще всего лёгкий демон мутаций по правкам): держим замок .want —
# по нему лёгкий демон уступает — и ждём основной замок, а не пропускаем недельный прогон.
# Флаг-замок, а не файл: умершего процесса ядро освобождает само, ложного флага не остаётся.
# Не дождались — прогон не состоялся, это обрыв для владельца.
WANT_FILE="${LOCK_FILE%.lock}.want"
if ! flock -n 9; then
    LOCK_WAIT_SEC="$(awk -v m="${NIGHTLY_MUTATIONS_LOCK_WAIT_MIN:-50}" 'BEGIN { printf "%.3f", m * 60 }')"
    exec 8>>"$WANT_FILE"
    flock -n 8
    say "замок занят (лок $LOCK_FILE), жду до ${NIGHTLY_MUTATIONS_LOCK_WAIT_MIN:-50} мин"
    # 8>&-: дочерний flock не должен унаследовать .want, иначе флаг переживёт убитый демон.
    if ! flock -w "$LOCK_WAIT_SEC" 9 8>&-; then
        say "ОТКАЗ: замок не освободился за ${NIGHTLY_MUTATIONS_LOCK_WAIT_MIN:-50} мин — прогон не стартовал"
        notify "$STAMP_BROKEN" "Полный прогон мутаций не стартовал: замок занят дольше ${NIGHTLY_MUTATIONS_LOCK_WAIT_MIN:-50} мин (держит другой прогон мутаций)." --task nightly-mutations:broken
        exit 1
    fi
    exec 8>&-
fi
ZHURNAL="$LOG_DIR/nightly-mutations-$(date +%Y%m%d).log"
say "недельный прогон мутаций: $MUT_CMD"
harness_run nightly-mutations
trap 'rc=$?; report_no_summary "$rc"; harness_run_end "$rc"' EXIT
# Срок мягкой остановки уходит прибору в окружении; дробные минуты допустимы.
# Запас не меньше предела одного запуска пробы плюс минута: мутация, начатая у срока,
# обязана кончиться до жёсткого предела.
MUTATION_DEADLINE_TS="$(awk -v s="$START_TS" -v m="$NIGHTLY_MUTATIONS_MAX_MIN" -v b="$MUTATION_SOFT_STOP_MIN_BEFORE" -v r="${MUTATION_RUN_TIMEOUT_S:-180}" \
    'BEGIN { res = b * 60; if (r + 60 > res) res = r + 60; d = m * 60 - res; if (d < 0) d = 0; printf "%d", s + d }')"
export MUTATION_DEADLINE_TS
# 9>&-: прибор и его потомки не держат замок прогона.
timeout -k 60 "${NIGHTLY_MUTATIONS_MAX_MIN}m" bash -c "cd '$PROJECT_DIR' && $MUT_CMD" > "$ZHURNAL" 2>&1 9>&-
RC=$?

ITOG="$(itog_stroka "$ZHURNAL")"
LIMIT=0
{ [ "$RC" = 124 ] || [ "$RC" = 137 ]; } && LIMIT=1
if [ -n "$ITOG" ] || [ "$LIMIT" = 1 ]; then
    mkdir -p "$HEARTBEAT_DIR"
    date -Is > "$HEARTBEAT_DIR/nightly-mutations"
fi

if [ -z "$ITOG" ] && [ "$LIMIT" = 1 ]; then
    LAST_LINE="$(grep -v '^[[:space:]]*$' "$ZHURNAL" | tail -1)"
    VERDICT=limit
    say "ОТКАЗ: прогон мутаций упёрся в предел ${NIGHTLY_MUTATIONS_MAX_MIN} мин, последнее: ${LAST_LINE:-нет}"
    notify "$STAMP_BROKEN" "Полный прогон мутаций упёрся в предел ${NIGHTLY_MUTATIONS_MAX_MIN} мин и остановлен (не добежал). Последнее: ${LAST_LINE:-нет}. Журнал: $(basename "$ZHURNAL")" --task nightly-mutations:broken
    exit 1
fi

# Итога нет — выход без вердикта, сигнал даёт report_no_summary.
[ -n "$ITOG" ] || exit 1

DYR="$(dyr_v "$ITOG")"
# Строка итога без числа дыр («НИ ОДНОЙ мутации не прогнано») — обрыв, не «дыр нет».
[ -n "$DYR" ] || exit 1

VERDICT=summary
say "$ITOG за $(( ($(date +%s) - START_TS) / 60 )) мин"
read -r NEDOG VYPALO VSEGO <<< "$(nedogon_v "$ZHURNAL")"
NEDOG="${NEDOG:-0}"; VYPALO="${VYPALO:-0}"; VSEGO="${VSEGO:-0}"
if [ "$DYR" -gt 0 ]; then
    notify "$STAMP_RED" "Полный прогон мутаций: $ITOG
Дыр: $DYR. Не прогнано: $NEDOG из $VSEGO.
Журнал: $(basename "$ZHURNAL")" --task nightly-mutations:red
elif [ "$VYPALO" -gt 0 ]; then
    # Выпавшие записи — поломка прибора или данных, а не нехватка времени.
    notify "$STAMP_BROKEN" "Полный прогон мутаций: выпало $VYPALO мутаций (запись не разобрана или читатель данных упал), не прогнано $NEDOG из $VSEGO. Журнал: $(basename "$ZHURNAL")" --task nightly-mutations:broken
elif [ "$NEDOG" -gt 0 ]; then
    # Жёлтое: время вышло, дыр в прогнанном нет; остальное добьёт следующий прогон через кэш.
    say "жёлтое: прогнано $((VSEGO - NEDOG)) из $VSEGO"
    notify "$STAMP_YELLOW" "Полный прогон мутаций: прогнано $((VSEGO - NEDOG)) из $VSEGO, остальное добьёт следующий запуск через кэш. Дыр в прогнанном нет. Журнал: $(basename "$ZHURNAL")"
else
    say "дыр нет — владельца не беспокою"
    alert_task --resolve nightly-mutations:red
    alert_task --resolve nightly-mutations:broken
fi
exit 0
}
