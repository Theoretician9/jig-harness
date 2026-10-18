#!/usr/bin/env bash
#
# handover-check.sh — вердикт свежести документа передачи.
#
# Откуда взят: написан для стартового пакета, шаблона в UNIFIED нет; норма —
# CLAUDE.md §17 и 02-КАРТА («точка сохранения: передача обновляется на каждом
# рубеже»): контекст сессии смертен, и всё, чего нет в docs/handover/, умирает
# вместе с ним. День работы без свежей передачи — это день, который следующая
# сессия будет восстанавливать по логам.
#
# Логика: есть git-активность за сегодня в $PROJECT_DIR, а свежайший
# docs/handover/SESSION-HANDOFF-*.md старее HANDOVER_MAX_AGE_MIN (harness.conf)
# → ненулевой код + текст, что именно протухло. Нет активности или передача
# свежа → 0. Зовут его Stop-хук (stop_reminder.sh) и session-warden.
#
# Запуск:
#   handover-check.sh              # вердикт по $PROJECT_DIR
#   handover-check.sh --selftest   # больной + здоровый случай во временном каталоге
#
# Код возврата: 0 — свежо/не требуется, 1 — протухло, 2 — нет конфига.
set -euo pipefail

# ── вердикт (вынесен в функцию, чтобы самотест гонял её же, а не копию) ─────
verdict() {  # $1 = каталог проекта, $2 = порог в минутах
    local dir="$1" max_min="$2"
    if ! git -C "$dir" rev-parse --git-dir >/dev/null 2>&1; then
        echo "handover: $dir ещё не репозиторий — передача не требуется"
        return 0
    fi
    # `|| true` в пайплайнах: при set -e -o pipefail отказ git log (репозиторий
    # без коммитов) или ls (передач нет вовсе) убивал бы скрипт с rc=2 ДО
    # вердикта — снаружи это выглядело как «нет конфига», и красный случай
    # «передачи нет» терялся. Самотест это маскировал: `if verdict` гасит -e.
    local commits_today
    commits_today=$(git -C "$dir" log --oneline --since=midnight 2>/dev/null | wc -l || true)
    if [ "$commits_today" -eq 0 ]; then
        echo "handover: git-активности за сегодня нет — передаче нечего протухать"
        return 0
    fi
    local newest
    newest=$(ls -1t "$dir"/docs/handover/SESSION-HANDOFF-*.md 2>/dev/null | head -1 || true)
    if [ -z "$newest" ]; then
        echo "handover: ПРОТУХЛО — за сегодня $commits_today коммит(ов), а документа передачи docs/handover/SESSION-HANDOFF-*.md нет вовсе. Создай его: следующая сессия начнёт с нуля."
        return 1
    fi
    local age_min
    age_min=$(( ($(date +%s) - $(stat -c %Y "$newest")) / 60 ))
    if [ "$age_min" -gt "$max_min" ]; then
        echo "handover: ПРОТУХЛО — за сегодня $commits_today коммит(ов), а свежайший $(basename "$newest") возрастом $age_min мин при пороге $max_min. Обнови передачу: работа после неё живёт только в смертном контексте."
        return 1
    fi
    echo "handover: свежо — $(basename "$newest") ($age_min мин ≤ $max_min), коммитов за день: $commits_today"
    return 0
}

# ── вердикт «передача дописана ПОСЛЕ названного мига» ──────────────────────
# Второй вопрос к тому же документу, и он НЕ про возраст. Владелец 18.10.2026
# прислал беду с другого сервера: ротация сменила сессию, а передачи не было —
# под лимитом подписки модель не отвечает на поручение регламента вовсе.
# Порог возраста (`verdict` выше) там бессилен: передача, дописанная за минуту
# ДО поручения, свежа по любому порогу, а дел смены в ней нет.
#
# Судит ТОТ ЖЕ судья, что и свежесть: маску docs/handover/SESSION-HANDOFF-*.md
# и понятие «свежайшая передача» знает один файл — разъехавшись по двоим, они
# разъедутся молча ([[the-check-fell-behind-the-code]]).
после_мига() {  # $1 = каталог проекта, $2 = миг поручения (unix-время)
    local dir="$1" mig="$2" newest mt
    newest=$(ls -1t "$dir"/docs/handover/SESSION-HANDOFF-*.md 2>/dev/null | head -1 || true)
    if [ -z "$newest" ]; then
        echo "handover: передачи нет вовсе — docs/handover/SESSION-HANDOFF-*.md пуст"
        return 1
    fi
    mt=$(stat -c %Y "$newest")
    if [ "$mt" -le "$mig" ]; then
        echo "handover: передача НЕ обновлялась после поручения — $(basename "$newest") записана на $(( (mig - mt) / 60 )) мин РАНЬШЕ поручения"
        return 1
    fi
    echo "handover: передача дописана после поручения — $(basename "$newest"), $(( (mt - mig) / 60 )) мин после"
    return 0
}

# ── самотест: больной и здоровый случай во временном каталоге ───────────────
if [ "${1:-}" = "--selftest" ]; then
    T=$(mktemp -d)
    trap 'rm -rf "$T"' EXIT
    git -C "$T" init -q
    git -C "$T" -c user.email=t@t -c user.name=t commit -q --allow-empty -m "работа за сегодня"

    # Больной случай 1: активность есть, передачи нет вовсе → красный.
    if verdict "$T" 240 >/dev/null; then
        echo "SELFTEST FAIL: отсутствие передачи при активности не поймано"; exit 1
    fi
    echo "selftest 1/6: нет передачи при активности — пойман (красный) — OK"

    # Больной случай 2: передача есть, но старее порога → красный.
    mkdir -p "$T/docs/handover"
    touch -d "-500 minutes" "$T/docs/handover/SESSION-HANDOFF-2026-08-08-0900.md"
    if verdict "$T" 240 >/dev/null; then
        echo "SELFTEST FAIL: протухшая передача не поймана"; exit 1
    fi
    echo "selftest 2/6: протухшая передача — поймана (красный) — OK"

    # Здоровый случай: свежая передача → зелёный.
    touch "$T/docs/handover/SESSION-HANDOFF-2026-08-08-1700.md"
    if ! verdict "$T" 240 >/dev/null; then
        echo "SELFTEST FAIL: свежая передача покраснела"; exit 1
    fi
    echo "selftest 3/6: свежая передача — прошла (зелёный) — OK"

    # ── вторая дверь: «дописана ПОСЛЕ поручения» ───────────────────────────
    PORUCHENIE=$(date +%s)
    T2=$(mktemp -d); mkdir -p "$T2/docs/handover"
    if после_мига "$T2" "$PORUCHENIE" >/dev/null; then
        echo "SELFTEST FAIL: отсутствие передачи принято за состоявшуюся"; rm -rf "$T2"; exit 1
    fi
    echo "selftest 4/6: передачи нет вовсе — поймано (красный) — OK"

    # БОЛЬНОЙ СЛУЧАЙ владельца 18.10.2026: передача свежа по возрасту (минуту
    # назад), но написана ДО поручения — дел этой смены в ней нет.
    touch -d "@$((PORUCHENIE - 60))" "$T2/docs/handover/SESSION-HANDOFF-2026-10-18.md"
    if после_мига "$T2" "$PORUCHENIE" >/dev/null; then
        echo "SELFTEST FAIL: передача ДО поручения принята за состоявшуюся"; rm -rf "$T2"; exit 1
    fi
    echo "selftest 5/6: передача написана ДО поручения — поймано (красный) — OK"

    touch -d "@$((PORUCHENIE + 5))" "$T2/docs/handover/SESSION-HANDOFF-2026-10-18.md"
    if ! после_мига "$T2" "$PORUCHENIE" >/dev/null; then
        echo "SELFTEST FAIL: передача ПОСЛЕ поручения не признана"; rm -rf "$T2"; exit 1
    fi
    echo "selftest 6/6: передача дописана после поручения — прошла (зелёный) — OK"
    rm -rf "$T2"
    exit 0
fi

# ── боевой запуск ───────────────────────────────────────────────────────────
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/config.sh"
INSTALL_CONF="${HARNESS_INSTALL_CONF:-$(konf_koren)/install.conf}"
HARNESS_CONF="${HARNESS_CONF:-$(konf_koren)/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "handover: нет $INSTALL_CONF — установка не завершена (01-SPEC §0)"; exit 2; }
# Окружение старше конфига: без этого проба с подставным PROJECT_DIR судила бы
# БОЕВОЙ каталог и отвечала «свежо» на чужой передаче (улика 12.09.2026).
# shellcheck disable=SC1091
# Путь берётся по РЕАЛЬНОМУ файлу (readlink -f): pre-commit подключён в
# .git/hooks симлинком, и dirname дал бы .git/hooks, где библиотеки нет.
# Поймано первым же коммитом после правки, 12.09.2026.
konf_zagruzit PROJECT_DIR LOG_DIR HANDOVER_MAX_AGE_MIN
: "${PROJECT_DIR:?в install.conf пуст PROJECT_DIR}"
if [ "${1:-}" = "--после" ]; then
    # Миг обязателен и обязан быть числом: пустой довод превратился бы в
    # сравнение с нулём, то есть в вечное «передача состоялась».
    case "${2:-}" in
        ''|*[!0-9]*) echo "handover: --после требует миг в unix-времени, а дано «${2:-пусто}»"; exit 2 ;;
    esac
    после_мига "$PROJECT_DIR" "$2"
    exit $?
fi
verdict "$PROJECT_DIR" "${HANDOVER_MAX_AGE_MIN:-240}"
