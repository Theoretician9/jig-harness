#!/usr/bin/env bash
#
# model-dlya.sh — какой моделью делать эту работу.
#
# Откуда взят. Владелец 12.09.2026: «автоматическое изменение моделей в
# зависимости от задач, например можно переключать на фейбл чтобы писать спеки
# для сложных задач или для каких то исследований».
#
# До этого модель выбиралась дважды за жизнь установки: AGENT_START_CMD — для
# смены, DEMON_MODEL — для ВСЕХ ночных вызовов разом. Ни один выбор не был
# связан с тем, что именно делается: сверка тем коммитов и разбор аварии шли
# на одной модели.
#
# Таблица «вид работы → модель» — данные (харнес/config/модели.yaml). Здесь
# только чтение и подстраховка: неизвестный вид получает умолчание и строку в
# лог, а не пустоту — пустое `--model` уронило бы вызов claude.
#
#   model-dlya.sh спека         → fable
#   model-dlya.sh дежурный      → haiku
#   model-dlya.sh что-то-новое  → haiku (умолчание, с записью в лог)
#   model-dlya.sh --таблица     показать таблицу целиком
#   model-dlya.sh --selftest    самотест
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Конфиги грузятся так, чтобы ОКРУЖЕНИЕ было старше файла: иначе проба,
# задавшая PROJECT_DIR или LOG_DIR, играет на боевом (улика 12.09.2026 —
# «/model» ушла в рабочую панель агента).
# shellcheck disable=SC1091
# Путь берётся по РЕАЛЬНОМУ файлу (readlink -f): pre-commit подключён в
# .git/hooks симлинком, и dirname дал бы .git/hooks, где библиотеки нет.
# Поймано первым же коммитом после правки, 12.09.2026.
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/konf.sh"
konf_zagruzit PROJECT_DIR LOG_DIR DEMON_MODEL
# Библиотека имён обязательна: без неё проверка имени молча исчезает и ЛЮБАЯ
# модель из таблицы подменяется умолчанием — проба ревью кода 12.09.2026 дала
# «haiku, rc=0» там, где ждали «fable», и жалобу никто не увидел (демоны зовут
# с 2>/dev/null). Падать громко лучше, чем тихо отдавать не то.
# shellcheck disable=SC1091
source "$HERE/lib/modeli.sh"

LOG_DIR="${LOG_DIR:-/var/log/harness}"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$HERE/.." && pwd)}"
MODELS_CONF="${MODELS_CONF:-$PROJECT_DIR/харнес/config/модели.yaml}"
# Умолчание живёт ЗДЕСЬ, а не только в файле данных: на свежей установке файла
# ещё нет, а демоны уже зовут — и пустая строка уронила бы им вызов.
FALLBACK="haiku"

таблица() {
    [ -r "$MODELS_CONF" ] || { echo "таблицы нет: $MODELS_CONF"; return 0; }
    MODELS_CONF="$MODELS_CONF" python3 - <<'PY'
import os, sys, yaml
try:
    d = yaml.safe_load(open(os.environ["MODELS_CONF"], encoding="utf-8")) or {}
except Exception as e:
    # Битый YAML — внятная строка, а не трейсбек на тридцать строк: эту команду
    # зовёт владелец и демон без вида работы (находка ревью кода 12.09.2026).
    print(f"таблица моделей не читается: {e.__class__.__name__}", file=sys.stderr)
    raise SystemExit(1)
for вид, модель in (d.get("модели") or {}).items():
    print(f"{вид:16} {модель}")
print(f"{'(умолчание)':16} {d.get('умолчание') or 'haiku'}")

PY
}

# Ручка владельца из канала (model.sh --демоны) старше таблицы для ДЕЖУРНЫХ
# видов: команда, которой он пользуется, обязана продолжать работать.
для_вида() {  # вид работы → имя модели
    local kind="${1:-}" handle="${DEMON_MODEL:-}" model=""
    if [ -r "$MODELS_CONF" ]; then
        model=$(MODELS_CONF="$MODELS_CONF" VID="$kind" python3 - <<'PY' 2>/dev/null || true
import os, yaml
try:
    d = yaml.safe_load(open(os.environ["MODELS_CONF"], encoding="utf-8")) or {}
except Exception:
    raise SystemExit
взято = (d.get("модели") or {}).get(os.environ.get("VID", "")) or d.get("умолчание") or ""
print(взято)
PY
)
    fi
    # Ручка владельца — АВАРИЙНОЕ переопределение, а не главный источник: ключ
    # DEMON_MODEL задан на каждой установке, и «ключ старше таблицы» означало бы,
    # что таблицу не спросят никогда (находка ревью C-2). Пока ключ непуст,
    # дежурные виды идут по нему — команда «model.sh --демоны» работает как была.
    case "$kind" in
        дежурный|поиск|замер_речи)
            [ -n "$handle" ] && model="$handle" ;;
    esac
    if [ -z "$model" ] || ! model_imya_dopustimo "$model"; then
        printf '%s [model-dlya] вид «%s»: ответ «%s» не годится, беру %s\n' \
            "$(date -Iseconds)" "$kind" "$model" "$FALLBACK" \
            >> "$LOG_DIR/model-dlya.log" 2>/dev/null || true
        model="$FALLBACK"
    fi
    printf '%s' "$model"
}

# Каждый шаг пайплайна обязан иметь строку в таблице: шаг без модели уходит в
# умолчание, то есть тесты до кода и контракт пишет самая дешёвая модель — и
# никто этого не видит. Требование ОДНО, а не «либо-либо»: проверка, у которой
# верны оба исхода, не проверяет ничего.
шаги_покрыты() {  # 0 — все шаги покрыты, 1 — нет
    MODELS_CONF="${MODELS_CONF}" \
    PIPELINE_CONF="${PIPELINE_CONF:-$PROJECT_DIR/харнес/config/пайплайн.yaml}" \
    python3 "$HERE/shagi-modeli.py"
}

selftest() {
    local ok=1 tmp
    tmp=$(mktemp -d)
    случай() {  # $1=ждём $2=вышло $3=зачем
        if [ "$1" = "$2" ]; then
            echo "  ок    $3"
        else
            echo "  ПЛОХО $3: ждали «$1», вышло «$2»"; ok=0
        fi
    }
    cat > "$tmp/модели.yaml" <<'YAML'
модели:
  спека: fable
  код: opus
  дежурный: haiku
умолчание: haiku
YAML
    случай "fable" "$(MODELS_CONF=$tmp/модели.yaml LOG_DIR=$tmp DEMON_MODEL= для_вида спека)" \
        "спека идёт на fable — слово владельца"
    случай "opus" "$(MODELS_CONF=$tmp/модели.yaml LOG_DIR=$tmp DEMON_MODEL= для_вида код)" \
        "код идёт на opus"
    # БОЛЬНОЙ СЛУЧАЙ: вида нет в таблице. Пустая строка уронила бы вызов
    # claude — вместо неё умолчание и запись в лог.
    случай "haiku" "$(MODELS_CONF=$tmp/модели.yaml LOG_DIR=$tmp DEMON_MODEL= для_вида новое-дело)" \
        "БОЛЬНОЙ СЛУЧАЙ: неизвестный вид даёт умолчание, а не пустоту"
    случай "haiku" "$(MODELS_CONF=$tmp/нет.yaml LOG_DIR=$tmp DEMON_MODEL= для_вида спека)" \
        "БОЛЬНОЙ СЛУЧАЙ: таблицы нет вовсе — умолчание, а не отказ"
    printf 'модели:\n  спека: "не-модель"\nумолчание: haiku\n' > "$tmp/битая.yaml"
    случай "haiku" "$(MODELS_CONF=$tmp/битая.yaml LOG_DIR=$tmp DEMON_MODEL= для_вида спека)" \
        "БОЛЬНОЙ СЛУЧАЙ: недопустимое имя из таблицы не уезжает в claude"
    случай "sonnet" "$(MODELS_CONF=$tmp/модели.yaml LOG_DIR=$tmp DEMON_MODEL=sonnet для_вида дежурный)" \
        "ручка владельца (model.sh --демоны) старше таблицы"
    случай "fable" "$(MODELS_CONF=$tmp/модели.yaml LOG_DIR=$tmp DEMON_MODEL=sonnet для_вида спека)" \
        "и при этом не трогает спеку: ручка — про дежурное"
    printf 'модели:\n  спека: claude-fable-5-1\nумолчание: haiku\n' > "$tmp/полное.yaml"
    случай "claude-fable-5-1" "$(MODELS_CONF=$tmp/полное.yaml LOG_DIR=$tmp DEMON_MODEL= для_вида спека)" \
        "полное имя модели принимается наравне с коротким"
    # БОЛЬНОЙ СЛУЧАЙ (ревью C-7): шаг пайплайна без строки в таблице.
    # Код возврата снимается через «|| rc=$?»: под set -e голый вызов, вернувший
    # 1, обрывает ВЕСЬ самотест молча — грабля «падение под set -e молчит».
    local rc=0
    printf 'шаги:\n  обычная: [тесты, спека]\n' > "$tmp/пайплайн.yaml"
    rc=0; MODELS_CONF="$tmp/модели.yaml" PIPELINE_CONF="$tmp/пайплайн.yaml" шаги_покрыты 2>/dev/null || rc=$?
    случай "1" "$rc" "БОЛЬНОЙ СЛУЧАЙ: шаг «тесты» без модели — красный"
    printf 'шаги:\n  обычная: [спека, код]\n' > "$tmp/пайплайн2.yaml"
    rc=0; MODELS_CONF="$tmp/модели.yaml" PIPELINE_CONF="$tmp/пайплайн2.yaml" шаги_покрыты 2>/dev/null || rc=$?
    случай "0" "$rc" "все шаги покрыты — зелёный"
    rm -rf "$tmp"
    (( ok )) && { echo "SELFTEST: зелёный (10 путей, среди них четыре больных случая)"; return 0; }
    echo "SELFTEST: КРАСНЫЙ"; return 1
}

case "${1:-}" in
    "")          echo "нужен вид работы, например: model-dlya.sh спека" >&2; таблица; exit 2 ;;
    --таблица)   таблица ;;
    --selftest)  selftest ;;
    *)           для_вида "$1"; echo ;;
esac
