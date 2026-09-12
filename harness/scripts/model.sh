#!/usr/bin/env bash
#
# model.sh — показать или сменить модель, на которой работает агент.
#
# Откуда взят: задача владельца 15.08.2026 «мне нужен инструмент, который может
# переключать модели, мне нужно переключить на фейбл». Владелец видит только
# канал, терминала у него нет, поэтому переключение обязано делаться сообщением
# в Telegram — ветку «модель …» разбирает tg-dispatcher.sh и зовёт этот скрипт.
#
# Модель выбирается при СТАРТЕ сессии (ключ --model команды claude), поэтому
# смена модели — это смена команды запуска плюс ротация сессии. Ротацию не
# изобретаем: session-warden.sh --rotate-now умеет ровно это — завершить
# текущего агента и поднять преемника, ничего не потеряв.
#
# Единственный источник истины команды запуска — AGENT_START_CMD в harness.conf:
# его же читают юнит systemd, session-warden и sentinel. Второй источник завёл
# бы расхождение «сервер поднимает одной командой, сторож другой».
#
#   model.sh                 показать текущую модель (агента и демонов)
#   model.sh --список        какие имена принимаются
#   model.sh fable           переключить агента и уйти в ротацию
#   model.sh --демоны haiku  сменить модель headless-вызовов демонов (без ротации:
#                             каждый их вызов — новая сессия, применится со следующего)
#   model.sh --selftest      самотест на временном конфиге, без ротации
#
set -euo pipefail

INSTALL_CONF="${HARNESS_INSTALL_CONF:-/etc/harness/install.conf}"
HARNESS_CONF="${HARNESS_CONF:-/etc/harness/harness.conf}"
# Окружение старше конфига: общий загрузчик вместо голого source (улика
# 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/konf.sh"
konf_zagruzit

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${LOG_DIR:-/var/log/harness}"

# Алиасы CLI: claude --model понимает и их, и полное имя. Список короткий
# намеренно — это то, чем пользуется владелец; полное имя тоже разрешено, но
# проверяется шаблоном, чтобы опечатка не превратилась в неподнимающуюся сессию.
# shellcheck disable=SC1091
[ -r "$(dirname "${BASH_SOURCE[0]}")/lib/modeli.sh" ] \
    && source "$(dirname "${BASH_SOURCE[0]}")/lib/modeli.sh"
# Список имён — ОДИН на оба скрипта (lib/modeli.sh). Локальная копия оставлена
# только как запасная на случай, когда библиотека не разложена.
ALIASES="${MODEL_ALIASES:-fable opus sonnet haiku}"

текущая_модель() {  # команда запуска → имя модели или «по умолчанию»
    local cmd="${1:-}" model=""
    model=$(printf '%s' "$cmd" | sed -n 's/.*--model[= ]\+\([^ ]\+\).*/\1/p')
    printf '%s' "${model:-по умолчанию (модель выбирает claude)}"
}

имя_допустимо() {  # имя → 0/1; отказ печатает причину с адресом
    local name="${1:-}"
    for a in $ALIASES; do
        [ "$name" = "$a" ] && return 0
    done
    # Полное имя: claude-fable-5, claude-haiku-4-5-20251001 и т.п.
    if printf '%s' "$name" | grep -Eq '^claude-[a-z0-9][a-z0-9.-]*$'; then
        return 0
    fi
    return 1
}

новая_команда() {  # старая команда, имя модели → новая команда
    local cmd="${1:-}" name="${2:-}"
    if printf '%s' "$cmd" | grep -q -- '--model'; then
        printf '%s' "$cmd" | sed "s/--model[= ]\+[^ ]\+/--model $name/"
    else
        # Ключ идёт сразу за именем команды: claude … — так же, как его пишут руками.
        printf '%s' "$cmd" | sed "s/^\([^ ]\+\)/\1 --model $name/"
    fi
}

записать_команду() {  # конфиг, новая команда
    local conf="$1" cmd="$2"
    # Правка строки на месте: конфиг — данные сервера, переписывать его целиком
    # значило бы терять комментарии и чужие ключи.
    sudo sed -i "s|^AGENT_START_CMD=.*|AGENT_START_CMD=\"$cmd\"|" "$conf"
}

показать() {
    local cmd="${AGENT_START_CMD:-claude --dangerously-skip-permissions}"
    echo "модель сейчас: $(текущая_модель "$cmd")"
    echo "команда запуска: $cmd"
    echo "модель демонов (headless-вызовы): ${DEMON_MODEL:-haiku (по умолчанию)}"
    echo "переключить: model.sh <имя>   ·   демонов: model.sh --демоны <имя>   ·   имена: model.sh --список"
}

список() {
    echo "алиасы (всегда последняя версия): $ALIASES"
    echo "полное имя тоже принимается, например claude-fable-5"
}

переключить() {
    local name="$1"
    local old_cmd="${AGENT_START_CMD:-claude --dangerously-skip-permissions}"
    if ! имя_допустимо "$name"; then
        echo "не знаю модель «$name». Принимаются: $ALIASES — или полное имя вида claude-fable-5." >&2
        return 2
    fi
    if [ "$(текущая_модель "$old_cmd")" = "$name" ]; then
        echo "модель уже $name — ротацию не заказываю."
        return 0
    fi
    local new_cmd; new_cmd=$(новая_команда "$old_cmd" "$name")
    записать_команду "$HARNESS_CONF" "$new_cmd"
    # Проверяем ФАКТ записи, а не то, что sed не ругнулся: конфиг мог быть
    # только для чтения, и молча несменившаяся модель хуже внятного отказа.
    local written; written=$(grep -m1 '^AGENT_START_CMD=' "$HARNESS_CONF" | cut -d'"' -f2)
    if [ "$written" != "$new_cmd" ]; then
        echo "запись в $HARNESS_CONF не состоялась: там «$written»" >&2
        return 1
    fi
    echo "{\"ts\":\"$(date -Is)\",\"было\":\"$(текущая_модель "$old_cmd")\",\"стало\":\"$name\"}" \
        >> "$LOG_DIR/model-switch.jsonl"
    echo "модель переключена: $(текущая_модель "$old_cmd") → $name"
    echo "команда запуска: $new_cmd"
    # Указание владельца 11.09.2026: «Смену модели тоже сделай через команды.
    # Вообще раз есть команды надо ими пользоваться». Перезапуск сессии ради
    # смены модели — своя механика поверх готовой команды: он стоил полной
    # ротации (до 15 минут, 48 таймаутов из 135 попыток за месяц) там, где
    # хватает «/model». Замер 11.09.2026 на отдельной сессии: команда меняет
    # модель В ТОЙ ЖЕ сессии (claude-opus-5 → claude-sonnet-5 в том же
    # транскрипте), контекст остаётся, перезапуск не нужен.
    #
    # Запись в harness.conf выше при этом обязательна: по ней поднимется
    # ПОЛНЫЙ перезапуск, когда он всё-таки случится (правка хуков, обновление
    # CLI, суточная чистка памяти).
    local session="${TMUX_SESSION:-agent}"
    if ! tmux has-session -t "$session" 2>/dev/null; then
        echo "модель записана в конфиг, но tmux-сессии «$session» нет — применится при следующем запуске агента."
        return 0
    fi
    case "$(tmux display-message -p -t "$session" '#{pane_current_command}' 2>/dev/null)" in
        claude|node) ;;
        *) echo "модель записана, но в панели «$session» не агент — команду не шлю, применится при запуске."; return 0 ;;
    esac
    echo "подаю «/model $name» в панель — сессия не перезапускается."
    tmux send-keys -t "$session" -l -- "/model $name"
    sleep 1
    tmux send-keys -t "$session" Enter
    # «/model» переспрашивает: «Switch model?» с подсвеченным пунктом «Yes».
    # Замер 11.09.2026 — без ответа диалог висит и модель не меняется.
    sleep 3
    tmux send-keys -t "$session" Enter
    echo "команда подана; модель сменится со следующего ответа агента."
}

записать_ключ() {  # конфиг, ключ, значение — заменить строку или дописать ключ
    local conf="$1" key="$2" val="$3"
    if grep -q "^${key}=" "$conf"; then
        sudo sed -i "s|^${key}=.*|${key}=\"${val}\"|" "$conf"
    else
        printf '%s="%s"\n' "$key" "$val" | sudo tee -a "$conf" >/dev/null
    fi
}

переключить_демонов() {
    local name="$1"
    if ! имя_допустимо "$name"; then
        echo "не знаю модель «$name». Принимаются: $ALIASES — или полное имя вида claude-haiku-4-5." >&2
        return 2
    fi
    записать_ключ "$HARNESS_CONF" DEMON_MODEL "$name"
    # Факт записи, а не код возврата sed: молча несменившаяся модель хуже отказа.
    local written; written=$(grep -m1 '^DEMON_MODEL=' "$HARNESS_CONF" | cut -d'"' -f2)
    if [ "$written" != "$name" ]; then
        echo "запись в $HARNESS_CONF не состоялась: там «$written»" >&2
        return 1
    fi
    echo "{\"ts\":\"$(date -Is)\",\"демоны\":\"$name\"}" >> "$LOG_DIR/model-switch.jsonl"
    echo "модель демонов: ${DEMON_MODEL:-haiku (по умолчанию)} → $name"
    echo "ротация не нужна: каждый headless-вызов — новая сессия, применится со следующего прогона."
}

selftest() {
    local tmp ok=1
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' RETURN

    случай() {  # ожидание, что получили, описание
        if [ "$1" = "$2" ]; then echo "  ок    $3"
        else echo "  ПРОВАЛ $3: ждали «$1», получили «$2»"; ok=0; fi
    }

    # БОЛЬНОЙ СЛУЧАЙ ПЕРВЫМ: опечатка не должна доехать до конфига — сессия с
    # несуществующей моделью не поднимется вовсе, и канал замолчит.
    имя_допустимо "fabel" && { echo "  ПРОВАЛ опечатка «fabel» принята"; ok=0; } \
        || echo "  ок    БОЛЬНОЙ: опечатка «fabel» отвергнута"
    имя_допустимо "" && { echo "  ПРОВАЛ пустое имя принято"; ok=0; } \
        || echo "  ок    БОЛЬНОЙ: пустое имя отвергнуто"
    имя_допустимо "; rm -rf /" && { echo "  ПРОВАЛ команда в имени принята"; ok=0; } \
        || echo "  ок    БОЛЬНОЙ: команда оболочки в имени отвергнута"

    имя_допустимо "fable" && echo "  ок    ЗДОРОВЫЙ: алиас fable принят" \
        || { echo "  ПРОВАЛ алиас fable отвергнут"; ok=0; }
    имя_допустимо "claude-fable-5" && echo "  ок    ЗДОРОВЫЙ: полное имя принято" \
        || { echo "  ПРОВАЛ полное имя отвергнуто"; ok=0; }

    случай "по умолчанию (модель выбирает claude)" \
        "$(текущая_модель 'claude --dangerously-skip-permissions')" \
        "команда без --model: модель по умолчанию"
    случай "fable" "$(текущая_модель 'claude --model fable --dangerously-skip-permissions')" \
        "команда с --model: имя разобрано"
    случай "claude-opus-5" "$(текущая_модель 'claude --model=claude-opus-5 -x')" \
        "форма --model=имя тоже разбирается"

    случай "claude --model fable --dangerously-skip-permissions" \
        "$(новая_команда 'claude --dangerously-skip-permissions' fable)" \
        "ключ добавляется сразу за именем команды"
    случай "claude --model opus --dangerously-skip-permissions" \
        "$(новая_команда 'claude --model fable --dangerously-skip-permissions' opus)" \
        "существующий ключ заменяется, а не дублируется"

    # Запись в конфиг — на временном файле: настоящий трогать самотестом нельзя.
    printf 'ДРУГОЙ=1\nAGENT_START_CMD="claude --dangerously-skip-permissions"\nХВОСТ=2\n' \
        > "$tmp/conf"
    sed -i "s|^AGENT_START_CMD=.*|AGENT_START_CMD=\"claude --model fable\"|" "$tmp/conf"
    случай 'claude --model fable' "$(grep -m1 '^AGENT_START_CMD=' "$tmp/conf" | cut -d'"' -f2)" \
        "строка конфига переписана"
    случай "3" "$(wc -l < "$tmp/conf")" "соседние ключи конфига целы"

    # Модель демонов: ключа нет → дописывается; есть → заменяется, соседи целы.
    # sudo в самотесте не нужен — файл свой: подменяем запись прямым sed/append.
    if grep -q '^DEMON_MODEL=' "$tmp/conf"; then
        sed -i 's|^DEMON_MODEL=.*|DEMON_MODEL="haiku"|' "$tmp/conf"
    else
        printf 'DEMON_MODEL="haiku"\n' >> "$tmp/conf"
    fi
    случай 'haiku' "$(grep -m1 '^DEMON_MODEL=' "$tmp/conf" | cut -d'"' -f2)" \
        "ключ демонов дописан в конфиг без ключа"
    sed -i 's|^DEMON_MODEL=.*|DEMON_MODEL="sonnet"|' "$tmp/conf"
    случай 'sonnet' "$(grep -m1 '^DEMON_MODEL=' "$tmp/conf" | cut -d'"' -f2)" \
        "существующий ключ демонов заменён"
    случай "4" "$(wc -l < "$tmp/conf")" "ключ демонов не задублирован, соседи целы"

    (( ok )) && { echo "SELFTEST: зелёный (3 больных случая, 2 здоровых имени, 3 разбора, 2 сборки команды, 5 проверок записи)"; return 0; }
    echo "SELFTEST: КРАСНЫЙ"; return 1
}

# Шаговое переключение (хук вызова скила) меняет модель ТОЛЬКО В СЕССИИ и не
# трогает AGENT_START_CMD: запись в конфиг — это выбор владельца на все будущие
# перезапуски, и затирать его шагом работы нельзя (находка ревью кода
# 12.09.2026: после «пишу-спеку» старт сессии навсегда оставался на fable).
только_в_сессии() {  # имя модели
    local name="${1:-}"
    имя_допустимо "$name" || { echo "имя модели не годится: $name" >&2; return 1; }
    local session="${TMUX_SESSION:-agent}"
    tmux has-session -t "$session" 2>/dev/null || { echo "нет сессии «$session»"; return 0; }
    case "$(tmux display-message -p -t "$session" '#{pane_current_command}' 2>/dev/null)" in
        claude|node) ;;
        *) echo "в панели «$session» не агент — команду не шлю"; return 0 ;;
    esac
    tmux send-keys -t "$session" -l -- "/model $name"
    sleep 1
    tmux send-keys -t "$session" Enter
    # Второй Enter — ПО ФАКТУ появления диалога «Switch model?», а не вслепую
    # по таймеру: если к этому моменту на экране другой диалог, слепой Enter
    # подтвердит его (находка ревью кода 12.09.2026). Ждём до 10 секунд.
    local i=0
    while [ "$i" -lt 20 ]; do
        if tmux capture-pane -p -t "$session" 2>/dev/null | grep -qi "switch model"; then
            tmux send-keys -t "$session" Enter
            break
        fi
        sleep 0.5
        i=$((i + 1))
    done
    echo "{\"ts\":\"$(date -Is)\",\"стало\":\"$name\",\"только_сессия\":true}" \
        >> "$LOG_DIR/model-switch.jsonl" 2>/dev/null || true
    echo "модель сессии: $name (конфиг не тронут)"
}

case "${1:-}" in
    "")          показать ;;
    --список)    список ;;
    --selftest)  selftest ;;
    --только-сессия) только_в_сессии "${2:-}" ;;
    --демоны)    переключить_демонов "${2:-}" ;;
    -h|--help)   показать; список ;;
    *)           переключить "$1" ;;
esac
