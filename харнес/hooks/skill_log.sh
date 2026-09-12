#!/usr/bin/env bash
#
# skill_log.sh — хук PostToolUse на инструмент Skill: пишет журнал вызовов
# скилов САМ.
#
# Откуда взят. Владелец 11.09.2026: «пайплайн контролируется по концам. Вход и
# выход. Середина на честном слове <…> Всё, что можно сделать кодом, делается
# кодом. Внимание модели — последний носитель правила, а не первый».
# Замер того же дня: журнал $LOG_DIR/skills.jsonl заполняла САМА МОДЕЛЬ — в
# шапке каждого SKILL.md стоит «ПЕРВОЙ СТРОКОЙ ДЕЙСТВИЯ записать вызов в
# журнал». За 11.09 в журнале ноль записей при полной смене работы и четырёх
# коммитах. Гейт по такому журналу был бы гейтом по самоотчёту: кто пропустил
# шаг, пропустит и запись, а кому мешает — допишет строку.
#
# Форма полезной нагрузки СНЯТА с живого вызова (11.09, проба на keybindings-help),
# а не выдумана — выдуманный образец чужого вывода уже делал проверку слепой
# ([[маркер-меню-cli-не-больше-меньше]]):
#   {"hook_event_name":"PostToolUse","tool_name":"Skill",
#    "tool_input":{"skill":"keybindings-help"},
#    "tool_response":{"success":true,"commandName":"keybindings-help",…},
#    "session_id":…,"cwd":…,"duration_ms":32}
#
# Признак «источник»: "хук" отличает машинную запись от строки, написанной
# моделью руками. Гейты пайплайна считают ТОЛЬКО машинные.
#
# Хук не вправе мешать работе: любая своя беда — молча в hook_failures.log и
# выход нулём. Сторож пайплайна, роняющий ход из-за собственного журнала,
# хуже отсутствующего.
#
# Проверка: bash scripts/hooks/skill_log.sh --selftest
set -uo pipefail

# Конфиг грузится так, чтобы ОКРУЖЕНИЕ было старше файла: иначе пробу хука,
# который правит конфиг и шлёт клавиши в панель, нельзя прогнать, не задев
# боевую установку (находка ревью кода 12.09.2026 — проба писала в боевой
# журнал вместо своего).
_HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
[ -r "$_HOOK_DIR/../lib/konf.sh" ] && source "$_HOOK_DIR/../lib/konf.sh" \
    && konf_zagruzit PROJECT_DIR LOG_DIR
# shellcheck disable=SC1091
[ -r "$_HOOK_DIR/../lib/modeli.sh" ] && source "$_HOOK_DIR/../lib/modeli.sh"
LOG_DIR="${LOG_DIR:-/var/log/harness}"
SKILLS_LOG_PATH="${SKILLS_LOG:-$LOG_DIR/${SKILLS_LOG_NAME:-skills.jsonl}}"
FAIL_LOG="$LOG_DIR/hook_failures.log"

беда() {  # причина — в лог и выход НУЛЁМ: работа важнее журнала
    printf '{"ts":"%s","hook":"skill_log","why":"%s"}\n' "$(date -Is)" "$1" \
        >> "$FAIL_LOG" 2>/dev/null || true
    exit 0
}

# Задача и ОТКУДА она взята. Три источника по убыванию достоверности:
# переменная, единственная задача в статусе wip на карте, последний трейлер
# Dev-Map. Третий источник даёт id ПРЕДЫДУЩЕЙ задачи — все скилы новой задачи
# до её первого коммита записались бы на уже закрытую (находка ревью I-5),
# поэтому источник пишется в запись, и гейт закрытия не вправе считать
# «историю» доказательством своего шага.
задача_сейчас() {
    if [ -n "${HARNESS_TASK:-}" ]; then printf '%s\t%s' "$HARNESS_TASK" "переменная"; return; fi
    local tid
    tid=$(python3 - "${PROJECT_DIR:?пуст PROJECT_DIR — не прочитан паспорт установки}/dev-map.yaml" <<'PYEOF' 2>/dev/null
import sys, yaml
def обход(узел):
    if isinstance(узел, dict):
        if "id" in узел and узел.get("status") == "wip":
            yield узел["id"]
        for v in узел.values():
            yield from обход(v)
    elif isinstance(узел, list):
        for v in узел:
            yield from обход(v)
with open(sys.argv[1], encoding="utf-8") as fh:
    задачи = list(обход(yaml.safe_load(fh)))
print(задачи[0] if len(задачи) == 1 else "")
PYEOF
) || true
    if [ -n "$tid" ]; then printf '%s\t%s' "$tid" "карта"; return; fi
    tid=$(git -C "${PROJECT_DIR:?пуст PROJECT_DIR — не прочитан паспорт установки}" log -30 --format=%B 2>/dev/null \
         | sed -n 's/^Dev-Map: *//p' | head -1) || true
    [ -n "$tid" ] && { printf '%s\t%s' "$tid" "история"; return; }
    printf '%s\t%s' "-" "неизвестно"
}

запись() {  # $1 = JSON полезной нагрузки → строка журнала либо пусто
    local pair
    pair="$(задача_сейчас)"
    TASK_ID="${pair%%	*}" TASK_FROM="${pair##*	}" python3 -c '
import json, os, sys, datetime
try:
    полезное = json.load(sys.stdin)
except Exception:
    sys.exit(3)                      # не разобрали — молча мимо
if полезное.get("tool_name") != "Skill":
    sys.exit(4)                      # чужой инструмент — не наше дело
скил = (полезное.get("tool_input") or {}).get("skill") or ""
if not скил:
    sys.exit(5)                      # вызов без имени скила записывать нечего
ответ = полезное.get("tool_response") or {}
print(json.dumps({
    "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "skill": скил,
    "task": os.environ.get("TASK_ID") or "-",
    "откуда_задача": os.environ.get("TASK_FROM") or "неизвестно",
    "источник": "хук",
    **({"проба": True} if os.environ.get("HARNESS_SKILL_PROBE") else {}),
    "успех": bool(ответ.get("success", True)),
    "сессия": полезное.get("session_id") or "",
}, ensure_ascii=False))
' <<< "$1"
}


# ── модель под шаг работы ───────────────────────────────────────────────────
# Владелец 12.09.2026: «автоматическое изменение моделей в зависимости от
# задач, например можно переключать на фейбл чтобы писать спеки».
#
# Место именно здесь: вызов скила — это граница шага пайплайна, и хук на него
# уже стоит. Правило, записанное в тексте скила, носителем не является —
# скилы генерируются из UNIFIED, и правка руками умрёт при пересборке.
#
# Переключение идёт через model.sh: он подаёт «/model» в панель агента, сессия
# НЕ перезапускается и контекст остаётся (замер 11.09.2026).
#
# Предохранители: не чаще одного переключения в MODEL_SWITCH_PAUSE_MIN минут
# (цепочка скилов иначе дёргала бы панель каждые полминуты) и молчание, когда
# модель уже та, что нужна. Своя беда — в лог и выход нулём, как у всего хука.
MODEL_SWITCH_PAUSE_SEC="${MODEL_SWITCH_PAUSE_SEC:-90}"
# Каталог СКРИПТОВ — рядом с хуком (scripts/hooks → scripts). Не PROJECT_DIR:
# он про проект, который судит передача смены, и подстановка его в пробе
# отбирала у хука путь к model-dlya.sh (поймано красным самотестом 12.09.2026).
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_SWITCH_STAMP="${MODEL_SWITCH_STAMP:-$LOG_DIR/model-switch.последний}"

вид_работы() {  # имя скила → вид работы для таблицы моделей
    case "${1:-}" in
        пишу-спеку)            echo "спека" ;;
        ревью-спеки)           echo "ревью_спеки" ;;
        пишу-план)             echo "план" ;;
        pretask-контракт)      echo "контракт" ;;
        тдд-тесты-до-кода)     echo "тесты" ;;
        ревью-кода)            echo "ревью_кода" ;;
        разбор-аварии|падение-тестов) echo "разбор_аварии" ;;
        добыча-улик|постройка-прибора) echo "исследование" ;;
        *)                     echo "" ;;   # шаг не назван — модель не трогаем
    esac
}

переключить_модель() {  # имя скила
    local skill="${1:-}" kind model current now last
    kind=$(вид_работы "$skill")
    [ -n "$kind" ] || return 0
    model=$(bash "$SCRIPTS_DIR/model-dlya.sh" "$kind" 2>/dev/null) || model=""
    [ -n "$model" ] || return 0
    # Антидребезг: цепочка скилов ОДНОГО шага не должна дёргать панель. Порог в
    # секундах, а не в десяти минутах: соседние шаги пайплайна (план → контракт)
    # идут быстрее, и минутный порог глушил бы настоящую смену работы.
    # Замок обязателен: без него две параллельные копии проходят проверку обе
    # (проба ревью кода 12.09.2026 — оба вывели «переключил»).
    now=$(date +%s)
    last=$(cat "$MODEL_SWITCH_STAMP" 2>/dev/null || echo 0)
    case "$last" in
        ''|*[!0-9]*) last=0 ;;   # полузаписанная метка не глушит механизм навечно
    esac
    [ $(( now - last )) -ge "$MODEL_SWITCH_PAUSE_SEC" ] || return 0
    exec 9>"${MODEL_SWITCH_STAMP}.замок" 2>/dev/null || true
    flock -n 9 2>/dev/null || return 0
    # Уже нужная модель — молчим: лишняя команда в панели это шум для владельца.
    # Строка берётся та, которую model.sh ПЕЧАТАЕТ («модель сейчас:»), а имена
    # сравниваются нормализованно: в конфиге лежит «claude-opus-5», а таблица
    # даёт «opus» — как строки они не равны никогда.
    current=$(bash "$SCRIPTS_DIR/model.sh" 2>/dev/null \
              | sed -n 's/^модель сейчас: *\([^ ]*\).*/\1/p' | head -1)
    model_odna_i_ta_zhe "$current" "$model" && return 0
    # Владелец 12.09.2026: «Если модель меняется во время сессии ей надо
    # полноценно передать контекст как при смене сессии». Механизм не просит
    # об этом — он не даёт сменить модель, пока передача не свежая: смена
    # модели приравнена к смене смены. Проверяет тот же скрипт, что и ротация.
    if ! bash "$SCRIPTS_DIR/handover-check.sh" >/dev/null 2>&1; then
        printf '%s' "$now" > "$MODEL_SWITCH_STAMP" 2>/dev/null || true
        printf '{"ts":"%s","hook":"skill_log","отложено":"передача смены устарела","вид":"%s","модель":"%s"}\n' \
            "$(date -Is)" "$kind" "$model" >> "$LOG_DIR/model-po-shagu.jsonl" 2>/dev/null || true
        return 0
    fi
    printf '%s' "$now" > "$MODEL_SWITCH_STAMP" 2>/dev/null || true
    # Ключ данных: пока он 0, модель НЕ меняется — только пишется, какой она
    # должна быть. Выключено по ревью кода 12.09.2026: переключение затирало
    # AGENT_START_CMD в системном конфиге, то есть выбор владельца навсегда.
    local vkl
    vkl=$(sed -n 's/^переключать_на_границе_шага:[[:space:]]*\([0-9]\).*/\1/p' \
          "${MODELS_CONF:-$SCRIPTS_DIR/../харнес/config/модели.yaml}" 2>/dev/null | head -1)
    if [ "${vkl:-0}" = "1" ]; then
        # «--только-сессия»: конфиг не трогаем. Запись AGENT_START_CMD — это
        # выбор владельца на все будущие перезапуски, шаг работы его не меняет.
        bash "$SCRIPTS_DIR/model.sh" --только-сессия "$model" >/dev/null 2>&1 || true
    fi
    printf '{"ts":"%s","hook":"skill_log","скил":"%s","вид":"%s","модель":"%s"}\n' \
        "$(date -Is)" "$skill" "$kind" "$model" >> "$LOG_DIR/model-po-shagu.jsonl" 2>/dev/null || true
}

if [ "${1:-}" = "--selftest" ]; then
    ok=1
    # БОЛЬНОЙ СЛУЧАЙ первым: настоящая нагрузка, снятая с живого вызова.
    LIVE_PAYLOAD='{"session_id":"с1","hook_event_name":"PostToolUse","tool_name":"Skill","tool_input":{"skill":"пишу-спеку"},"tool_response":{"success":true,"commandName":"пишу-спеку"},"duration_ms":32}'
    LINE=$(HARNESS_TASK="hr.проба" запись "$LIVE_PAYLOAD")
    if printf '%s' "$LINE" | grep -q '"skill": "пишу-спеку"' \
       && printf '%s' "$LINE" | grep -q '"источник": "хук"' \
       && printf '%s' "$LINE" | grep -q '"task": "hr.проба"'; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: живая нагрузка даёт машинную запись с задачей"
    else
        echo "  ПЛОХО живая нагрузка: получили «$LINE»"; ok=0
    fi
    # Задача берётся С КАРТЫ, когда переменной нет: до первого коммита новой
    # задачи история отдала бы id ПРЕДЫДУЩЕЙ (находка ревью I-5). Карта —
    # подставная: на живой бывает несколько задач в работе, и тогда правило
    # честно молчит (следующий путь).
    TMPD=$(mktemp -d)
    printf 'tasks:\n  - id: hr.одна-в-работе\n    status: wip\n  - id: b\n    status: test\n' > "$TMPD/dev-map.yaml"
    LINE=$(PROJECT_DIR="$TMPD" запись "$LIVE_PAYLOAD")
    rm -rf "$TMPD"
    if printf '%s' "$LINE" | grep -q '"откуда_задача": "карта"' \
       && printf '%s' "$LINE" | grep -q '"task": "hr.одна-в-работе"'; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: без переменной задача берётся с карты, а не из истории"
    else
        echo "  ПЛОХО без переменной: получили «$LINE»"; ok=0
    fi
    # Двух задач в работе быть не должно; если есть — карта молчит, и источник
    # честно называется «история»: гейт закрытия такую запись своей не считает.
    TMPD=$(mktemp -d)
    printf 'tasks:\n  - id: a\n    status: wip\n  - id: b\n    status: wip\n' > "$TMPD/dev-map.yaml"
    LINE=$(PROJECT_DIR="$TMPD" запись "$LIVE_PAYLOAD")
    rm -rf "$TMPD"
    if printf '%s' "$LINE" | grep -qE '"откуда_задача": "(история|неизвестно)"'; then
        echo "  ок    две задачи в работе: карта не угадывает, источник назван честно"
    else
        echo "  ПЛОХО две wip-задачи: получили «$LINE»"; ok=0
    fi
    # Проба отличима от настоящего шага (иначе гейт зачтёт её за пройденный шаг).
    LINE=$(HARNESS_SKILL_PROBE=1 HARNESS_TASK="hr.проба" запись "$LIVE_PAYLOAD")
    if printf '%s' "$LINE" | grep -q '"проба": true'; then
        echo "  ок    проба помечена и в журнале отличима"
    else
        echo "  ПЛОХО проба не помечена: «$LINE»"; ok=0
    fi
    # Чужой инструмент — не наше дело.
    if [ -z "$(запись '{"tool_name":"Bash","tool_input":{"command":"ls"}}')" ]; then
        echo "  ок    вызов другого инструмента журнал не трогает"
    else
        echo "  ПЛОХО чужой инструмент попал в журнал"; ok=0
    fi
    # Битая нагрузка — молчание, а не падение: хук не вправе ронять ход.
    if [ -z "$(запись 'не json')" ]; then
        echo "  ок    битая нагрузка: молчим, работу не роняем"
    else
        echo "  ПЛОХО битая нагрузка что-то записала"; ok=0
    fi
    # Вызов без имени скила.
    if [ -z "$(запись '{"tool_name":"Skill","tool_input":{}}')" ]; then
        echo "  ок    вызов без имени скила не пишется"
    else
        echo "  ПЛОХО вызов без имени скила попал в журнал"; ok=0
    fi
    # Недоступный журнал: хук обязан выйти нулём (иначе оболочка увидит отказ).
    LOG_DIR=/заведомо/нет/такого bash "$0" < /dev/null >/dev/null 2>&1
    if [ "$?" = 0 ]; then
        echo "  ок    недоступный журнал не роняет работу (код 0)"
    else
        echo "  ПЛОХО недоступный журнал вернул ненулевой код"; ok=0
    fi
    # ── переключение модели под шаг ────────────────────────────────────────
    TMPD=$(mktemp -d)
    LOG_DIR="$TMPD" MODEL_SWITCH_STAMP="$TMPD/метка"
    # БОЛЬНОЙ СЛУЧАЙ: два вызова подряд обязаны дать ОДНО переключение —
    # цепочка скилов в одном шаге иначе дёргала бы панель каждые полминуты.
    printf '%s' "$(date +%s)" > "$TMPD/метка"
    if MODEL_SWITCH_STAMP="$TMPD/метка" LOG_DIR="$TMPD" переключить_модель "пишу-спеку" \
       && [ ! -s "$TMPD/model-po-shagu.jsonl" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: свежая метка — второго переключения нет (антидребезг)"
    else
        echo "  ПЛОХО антидребезг не сработал"; ok=0
    fi
    # Скил, которому шаг не назначен, модель не трогает.
    if [ -z "$(вид_работы "keybindings-help")" ]; then
        echo "  ок    скил вне пайплайна модель не переключает"
    else
        echo "  ПЛОХО скил вне пайплайна получил вид работы"; ok=0
    fi
    if [ "$(вид_работы "пишу-спеку")" = "спека" ] && [ "$(вид_работы "ревью-кода")" = "ревью_кода" ]; then
        echo "  ок    имя скила переводится в вид работы таблицы"
    else
        echo "  ПЛОХО перевод имени скила в вид работы"; ok=0
    fi
    # БОЛЬНОЙ СЛУЧАЙ (владелец 12.09.2026): устаревшая передача смены обязана
    # ОТЛОЖИТЬ смену модели — «ей надо полноценно передать контекст как при
    # смене сессии». Подставляем проект без передачи, но с коммитом за сегодня.
    TMPD2=$(mktemp -d)
    git -C "$TMPD2" init -q 2>/dev/null
    git -C "$TMPD2" -c user.email=п@п -c user.name=п commit -q --allow-empty -m проба 2>/dev/null
    mkdir -p "$TMPD2/docs/handover"
    rm -f "$TMPD2/метка"
    if PROJECT_DIR="$TMPD2" LOG_DIR="$TMPD2" MODEL_SWITCH_STAMP="$TMPD2/метка" \
       MODEL_SWITCH_PAUSE_MIN=0 переключить_модель "пишу-спеку" \
       && grep -q "передача смены устарела" "$TMPD2/model-po-shagu.jsonl" 2>/dev/null; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: без свежей передачи модель не меняется"
    else
        echo "  ПЛОХО смена модели прошла без свежей передачи смены"; ok=0
    fi
    rm -rf "$TMPD2"
    rm -rf "$TMPD"
    [ "$ok" = 1 ] && { echo "САМОТЕСТ ПРОЙДЕН: 12 путей, первым — больной случай"; exit 0; }
    echo "САМОТЕСТ ПРОВАЛЕН"; exit 1
fi

PAYLOAD=$(cat) || беда "нагрузку со stdin прочитать не удалось"
LINE=$(запись "$PAYLOAD") || exit 0   # коды 3/4/5 — законные «не наше дело»
[ -n "$LINE" ] || exit 0
mkdir -p "$(dirname "$SKILLS_LOG_PATH")" 2>/dev/null || беда "каталог журнала не создать"
printf '%s\n' "$LINE" >> "$SKILLS_LOG_PATH" 2>/dev/null || беда "в журнал $SKILLS_LOG_PATH не пишется"
# Модель — ПОСЛЕ записи в журнал: журнал важнее, и своя беда тут не должна
# отнимать у гейтов пайплайна машинную запись о вызове скила.
переключить_модель "$(printf '%s' "$PAYLOAD" | python3 -c 'import sys,json;print((json.load(sys.stdin).get("tool_input") or {}).get("skill") or "")' 2>/dev/null)" || true
exit 0
