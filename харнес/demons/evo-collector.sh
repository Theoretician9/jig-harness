#!/usr/bin/env bash
#
# evo-collector.sh — сбор сигналов эволюции харнеса.
#
# Что держит: точку крепления модуля эволюции (00-ВВОДНЫЕ §3): JSONL-журналы
# гейтов и скилов собираются в одну сводку с первого дня, журнал дефектов и
# реестр механизмов заводятся пустыми. Полигона оркестрации здесь нет —
# только дешёвые крепления, чтобы будущему модулю было что читать: сигнал,
# который не собирали с первого дня, не восстановить задним числом.
#
# Откуда взят: собран для стартового пакета (планы №2–3 полигона, решение
# плана №1); норма «пустой журнал — нормально, отсутствующий — нет» —
# из 01-SPEC §10 п.7.
#
# Чем доказывается: после первого прогона существуют $LOG_DIR/evo/summary.jsonl
# (растёт на строку в час), журнал-дефектов.jsonl и реестр-механизмов.yaml;
# еженедельная строка «спеки vs коммиты» появляется по понедельникам ровно
# один раз (проверка на приёмке — два прогона подряд, вторая строка не дублируется).
#
# Запуск: cron, раз в час, от имени $AGENT_USER.
set -euo pipefail

INSTALL_CONF="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-/etc/harness/install.conf}}"
HARNESS_CONF="${HARNESS_CONF:-/etc/harness/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "evo-collector: нет $INSTALL_CONF — установка не завершена (01-SPEC §0)"; exit 1; }
[ -r "$HARNESS_CONF" ] || { echo "evo-collector: нет $HARNESS_CONF — скопируйте харнес/config/harness.conf в /etc/harness/"; exit 1; }
# Окружение старше конфига: общий загрузчик вместо голого source (улика
# 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/konf.sh"
konf_zagruzit
: "${PROJECT_DIR:?пуст PROJECT_DIR}" "${LOG_DIR:?пуст LOG_DIR}" "${HEARTBEAT_DIR:?пуст HEARTBEAT_DIR}"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# Один прогон за раз (Д-10, единый паттерн демонов): два сборщика написали бы
# в summary.jsonl вперемешку. Лок — сервисный каталог (конвенция К-1б).
LOCK_FILE="$LOG_DIR/locks/${SERVICE_LOCKS_SUBDIR:-services}/evo-collector.lock"
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>>"$LOCK_FILE"
flock -n 9 || { say "прогон уже идёт (лок $LOCK_FILE) — выхожу"; exit 0; }

EVO_DIR="$LOG_DIR/evo"
mkdir -p "$EVO_DIR"

# ── журнал дефектов и реестр механизмов: завести пустыми при отсутствии ─────
# Пустой файл — «сигналов пока нет»; отсутствующий — «никто не собирает».
# Различие принципиально: по нему модуль эволюции отличает тишину от дыры.
if [ ! -f "$EVO_DIR/журнал-дефектов.jsonl" ]; then
    touch "$EVO_DIR/журнал-дефектов.jsonl"
    say "заведён пустой журнал-дефектов.jsonl"
fi
# журнал вызовов скилов — с первого дня, даже пустым: «отсутствующий — нет» (01-SPEC §10)
if [ ! -f "$LOG_DIR/${SKILLS_LOG_NAME:-skills.jsonl}" ]; then
    touch "$LOG_DIR/${SKILLS_LOG_NAME:-skills.jsonl}"
    say "заведён пустой журнал вызовов скилов"
fi
# журнал гейтов — по той же норме (Д-8): пустой = «коммитов ещё не было»,
# отсутствующий выглядел бы как «гейты никто не журналирует»
if [ ! -f "$LOG_DIR/${GATES_LOG_NAME:-gates.jsonl}" ]; then
    touch "$LOG_DIR/${GATES_LOG_NAME:-gates.jsonl}"
    say "заведён пустой журнал гейтов"
fi
if [ ! -f "$EVO_DIR/реестр-механизмов.yaml" ]; then
    cat > "$EVO_DIR/реестр-механизмов.yaml" <<'EOF'
# Реестр механизмов харнеса: имя → что держит → чем доказывается.
# Заполняется при заведении каждого нового гейта/демона/хука.
# Пустой список — механизмов сверх стартового набора ещё нет.
mechanisms: []
EOF
    say "заведён пустой реестр-механизмов.yaml"
fi

# ── счётчики журналов ───────────────────────────────────────────────────────
# Счёт строк, не содержимое: сводка — индекс, сырьё остаётся в журналах.
# Отсутствующий журнал даёт null и honest-строку в лог — не ноль: ноль значит
# «собирали и пусто», null значит «журнала нет вовсе».
count_or_null() {  # путь → число строк | null
    # grep -c '', не wc -l (Д-8): wc -l считает переводы строки и недосчитывает
    # последнюю строку без \n — журнал, дописанный без перевода, терял запись.
    # || true: у grep «0 совпадений» — код 1, а set -e счёл бы это смертью.
    if [ -f "$1" ]; then grep -c '' "$1" || true; else echo "null"; fi
}
GATES_LOG="$LOG_DIR/${GATES_LOG_NAME:-gates.jsonl}"
SKILLS_LOG="$LOG_DIR/${SKILLS_LOG_NAME:-skills.jsonl}"
ROTATION_LOG="$LOG_DIR/rotation.jsonl"

GATES_N=$(count_or_null "$GATES_LOG")
SKILLS_N=$(count_or_null "$SKILLS_LOG")
ROTATION_N=$(count_or_null "$ROTATION_LOG")
DEFECTS_N=$(count_or_null "$EVO_DIR/журнал-дефектов.jsonl")
[ "$GATES_N" = "null" ] && say "журнала гейтов $GATES_LOG нет — pre-commit ещё не прогонялся"
[ "$SKILLS_N" = "null" ] && say "журнала скилов $SKILLS_LOG нет — скилы ещё не вызывались или хук не пишет"
[ "$ROTATION_N" = "null" ] && say "rotation.jsonl нет — ротаций ещё не было"

jq -cn --arg ts "$(date -Is)" \
    --argjson gates "$GATES_N" --argjson skills "$SKILLS_N" \
    --argjson rotation "$ROTATION_N" --argjson defects "$DEFECTS_N" \
    '{ts:$ts, kind:"hourly", gates_events:$gates, skill_calls:$skills,
      rotation_events:$rotation, defects:$defects}' >> "$EVO_DIR/summary.jsonl"
say "часовая сводка записана (гейты=$GATES_N скилы=$SKILLS_N ротации=$ROTATION_N)"

# ── еженедельно: спеки/планы vs коммиты ─────────────────────────────────────
# Дешёвый показатель дисциплины пайплайна: сколько спек и планов написано
# на сколько коммитов. Пишется по EVO_WEEKLY_DOW ровно один раз в день —
# защита от дублей нужна, потому что демон ходит каждый час.
DOW=$(date +%u)
TODAY=$(date +%Y-%m-%d)
if [ "$DOW" = "${EVO_WEEKLY_DOW:-1}" ]; then
    if grep -q "\"kind\":\"weekly\".*\"date\":\"$TODAY\"" "$EVO_DIR/summary.jsonl" 2>/dev/null; then
        say "еженедельная сводка за $TODAY уже есть — не дублирую"
    else
        # `|| true`: docs/ может ещё не существовать, find вернёт 1, а pipefail
        # превратил бы это в смерть демона на присвоении (грабля из смоука
        # session-warden — та же самая).
        SPECS_N=$(find "$PROJECT_DIR/docs" -name '*.md' \
                  \( -path '*spec*' -o -path '*спек*' -o -path '*plan*' -o -path '*план*' \) \
                  -newermt '7 days ago' 2>/dev/null | wc -l | tr -d ' ' || true)
        [ -n "$SPECS_N" ] || SPECS_N=0
        if git -C "$PROJECT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
            COMMITS_N=$(git -C "$PROJECT_DIR" rev-list --count --since='7 days ago' HEAD 2>/dev/null || echo 0)
        else
            COMMITS_N=0
            say "git-репозитория ещё нет — коммитов 0 честно"
        fi
        jq -cn --arg ts "$(date -Is)" --arg date "$TODAY" \
            --argjson specs "$SPECS_N" --argjson commits "$COMMITS_N" \
            '{ts:$ts, kind:"weekly", date:$date, specs_and_plans_7d:$specs, commits_7d:$commits}' \
            >> "$EVO_DIR/summary.jsonl"
        say "еженедельная сводка: спек/планов за 7 дн. — $SPECS_N, коммитов — $COMMITS_N"
    fi
fi

# ── потолок сводки (Д-8): summary.jsonl не растёт бесконечно ────────────────
# Сводка — индекс за обозримое прошлое, не архив: демон ходит каждый час,
# и без потолка файл пух бы вечно. Старшие строки срезаются с записью в лог.
SUMMARY_MAX="${EVO_SUMMARY_MAX_LINES:-2000}"
# grep -c '' (Д-8): считает и последнюю строку без \n — см. count_or_null.
SUMMARY_LINES=$(grep -c '' "$EVO_DIR/summary.jsonl" || true)
if [ "$SUMMARY_LINES" -gt "$SUMMARY_MAX" ]; then
    CUT_N=$((SUMMARY_LINES - SUMMARY_MAX))
    tail -n "$SUMMARY_MAX" "$EVO_DIR/summary.jsonl" > "$EVO_DIR/summary.jsonl.tmp"
    mv "$EVO_DIR/summary.jsonl.tmp" "$EVO_DIR/summary.jsonl"
    say "summary.jsonl превысил потолок $SUMMARY_MAX — срезано $CUT_N строк"
fi

touch "$HEARTBEAT_DIR/evo-collector"
