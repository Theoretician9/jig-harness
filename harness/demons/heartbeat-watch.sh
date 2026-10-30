#!/usr/bin/env bash
#
# heartbeat-watch.sh — сторож меток: мёртвый демон называется по имени.
#
# Что держит: страховку нельзя оценивать по частоте обращений к ней
# (01-SPEC §9). Каждый демон ставит метку в $HEARTBEAT_DIR только при
# успешном прогоне; сторож раз в сутки сверяет возраст КАЖДОЙ метки с её
# периодом ×2 и шлёт владельцу сводку ВСЕГДА — и зелёную «все демоны живы»,
# и красную поимённо. Молчание сторожа неотличимо от его смерти, поэтому
# смерть самого сторожа владелец обнаруживает по ОТСУТСТВИЮ утреннего
# сообщения (норма UNIFIED/08).
#
# Откуда взят: собран для стартового пакета по 01-SPEC §9; таблица периодов —
# в harness.conf (HEARTBEAT_PERIODS), данные, не код.
#
# Чем доказывается: встроенным больным случаем (`--selftest`: просроченная
# метка → сводка называет имя) и живым больным случаем на приёмке —
# закомментировать строку одного демона в crontab → завтрашняя сводка обязана
# назвать его. Успех отправки — только ok:true (curl+jq); при провале отправки
# выход ненулевой и метку себе сторож НЕ ставит.
#
# Запуск: cron, раз в сутки 08:00, от имени $AGENT_USER.
set -euo pipefail

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# ── самотест: больной случай во временном каталоге ──────────────────────────
if [ "${1:-}" = "--selftest" ]; then
    T=$(mktemp -d)
    trap 'rm -rf "$T"' EXIT
    mkdir -p "$T/hb"
    cat > "$T/install.conf" <<EOF
PROJECT_NAME="selftest"
PROJECT_DIR="$T"
AGENT_USER="selftest"
SECRETS_DIR="$T"
TG_TOKEN_FILE="$T/no-token"
TG_CHAT_ID=""
AUTONOMY="semi"
HEARTBEAT_DIR="$T/hb"
LOG_DIR="$T"
EOF
    cat > "$T/harness.conf" <<EOF
HEARTBEAT_PERIODS="alpha:10 beta:10"
EOF
    touch "$T/hb/alpha"                          # живой: только что
    touch -d '2 hours ago' "$T/hb/beta"          # мёртвый: 120 мин > 10×2
    # bash "$0", не "$0": после scp с Windows exec-бита может не быть (П-5).
    OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон упал"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'beta' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: просроченная beta не названа по имени"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'МЁРТВ' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: в сводке нет слова МЁРТВ"; echo "$OUT"; exit 1; }
    if echo "$OUT" | grep -q 'alpha.*МЁРТВ'; then
        echo "САМОТЕСТ ПРОВАЛЕН: живая alpha объявлена мёртвой"; echo "$OUT"; exit 1
    fi
    # больной случай: guard_exceptions.log вырос с прошлого прогона (LOG_DIR="$T")
    printf '%s\n' '{"ts":1,"reason":"т","detail":"т"}' '{"ts":2,"reason":"т","detail":"т"}' \
                  '{"ts":3,"reason":"т","detail":"т"}' > "$T/guard_exceptions.log"
    OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон с приростом лога упал (а не должен)"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'сторож команд падал 3 раз' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: прирост guard_exceptions.log не назван в сводке"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'guard_exceptions.log' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: сводка не говорит, куда смотреть"; echo "$OUT"; exit 1; }
    # без прироста предупреждение обязано исчезнуть (размер запомнен)
    OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: повторный прогон упал"; echo "$OUT"; exit 1; }
    if echo "$OUT" | grep -q 'сторож команд падал'; then
        echo "САМОТЕСТ ПРОВАЛЕН: без прироста лога сводка всё равно пугает падениями"; echo "$OUT"; exit 1
    fi
    # больной случай Д-4а: сводка НЕ доставлена (канал не настроен) → размер
    # НЕ запоминается, прирост обязан всплыть в СЛЕДУЮЩЕЙ доставленной сводке.
    printf '%s\n' '{"ts":4,"reason":"т","detail":"т"}' >> "$T/guard_exceptions.log"
    if OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" bash "$0" 2>&1); then
        echo "САМОТЕСТ ПРОВАЛЕН: недоставленная сводка не сделала прогон красным"; echo "$OUT"; exit 1
    fi
    OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон после недоставки упал"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'сторож команд падал 1 раз' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: недоставка съела сигнал прироста — SIZE_FILE записан до отправки (Д-4а)"; echo "$OUT"; exit 1; }
    # сводка обязана говорить о юните диспетчера (active или честное «не проверено»)
    echo "$OUT" | grep -q 'harness-dispatcher' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: сводка молчит про юнит harness-dispatcher (Д-4б)"; echo "$OUT"; exit 1; }
    # строка расхода токенов включается в сводку, когда файл есть (Д-4в)
    echo "токены за сутки: 1234 (демо)" > "$T/tokens-daily.summary"
    OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон с tokens-daily.summary упал"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'токены за сутки: 1234' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: строка tokens-daily.summary не попала в сводку (Д-4в)"; echo "$OUT"; exit 1; }
    # БОЛЬНОЙ СЛУЧАЙ 18.10.2026: при наличии подробного отчёта в канал уходила
    # одна строка «во вложении» — владелец: «стал приходить файлом, а не
    # сообщением. Плюс еще там нет отчёта по моделям». Теперь в сообщении
    # выжимка с моделями, а файл едет вложением с именем на .md.
    printf 'РАСХОД ЗА СУТКИ — 2026-10-17\n\nШаги (обращения к модели)   2972\n\nКэш\n  попаданий                 99,6%%\n\nОтветы модели\n  всего                     1,9M\n\nПо моделям\n  claude-opus-5             шагов 2874 · ответов 1,9M\n  <synthetic>               шагов 76 · ответов 0\n\nОтдача\n  коммитов за сутки         126\n  шагов на коммит           23\n' > "$T/tokens-daily.report"
    OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон с tokens-daily.report упал"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'По моделям: claude-opus-5 — шагов 2874' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: в сообщении нет разбивки по моделям (его слово 18.10)"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'Расход за сутки (2026-10-17): шагов 2972' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: в сообщении нет главных чисел расхода"; echo "$OUT"; exit 1; }
    # Судится СТРОКА сообщения, а не весь вывод: сам отчёт демон кладёт в
    # журнал целиком (строка 404), и grep по всему выводу нашёл бы <synthetic>
    # там, где владелец его не увидит.
    if echo "$OUT" | grep '^.*По моделям:' | grep -q '<synthetic>'; then
        echo "САМОТЕСТ ПРОВАЛЕН: служебная строка <synthetic> ушла владельцу — она токенов не стоит"; echo "$OUT"; exit 1
    fi
    echo "$OUT" | grep -qE 'вложение: .*\.md' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: вложение уходит не с расширением .md (его слово 18.10)"; echo "$OUT"; exit 1; }
    # БОЛЬНОЙ СЛУЧАЙ 24.09.2026: сводка в 08:00 несла НЕПОЛНЫЕ текущие сутки
    # под заголовком «РАСХОД ЗА СУТКИ» — владелец прочитал «Шаги 1», а модель
    # за те сутки звали четыре раза. Законченный день перевешивает текущий.
    printf 'РАСХОД ЗА СУТКИ — вчерашний день\n  шагов                     4\n' \
        > "$T/tokens-daily.report.вчера"
    OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон с отчётом за вчера упал"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'шагов                     4' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: сводка взяла неполные текущие сутки вместо законченных"; echo "$OUT"; exit 1; }
    if echo "$OUT" | grep -q 'медиана                   42'; then
        echo "САМОТЕСТ ПРОВАЛЕН: текущий неполный отчёт перевесил законченный"; echo "$OUT"; exit 1
    fi
    # Ревью спеки пайплайна I-2: журнал обходов честной двери не читал НИКТО —
    # ни панель, ни сводка. Дверь, которую видит только прошедший в неё, —
    # тихий обход, а не видимое решение.
    printf '{"ts": "%s", "гейт": "пайплайн", "задача": "hr.проба", "почему": "проба видимости"}\n' \
        "$(date +%Y-%m-%d)" > "$T/пайплайн-обходы.jsonl"
    OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон с журналом обходов упал"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'Обходов трубы за сутки: 1 ' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: обход честной двери не назван в сводке (I-2)"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'проба видимости' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: сводка назвала число обходов без ПРИЧИНЫ"; echo "$OUT"; exit 1; }
    # БОЛЬНОЙ СЛУЧАЙ 18.10.2026 (его снимок): пять строк обходов об ОДНОЙ
    # карточке, каждая обрезана посреди слова, и в тексте слова, которых уровень
    # «простой» не допускает. Журнал: три обхода задачи А (две длинные причины
    # одинаковы, третья короткая) и по одному обходу задач Б, В, Г.
    DLINNAYA=$(python3 -c "print('в коммите, репозитории, ротацией, конфига ' + ' '.join('слово%02d' % i for i in range(1, 31)))")
    printf '{"ts": "%s", "задача": "hr.zadacha-a", "почему": "%s"}\n' "$(date +%Y-%m-%d)" "$DLINNAYA" > "$T/пайплайн-обходы.jsonl"
    printf '{"ts": "%s", "задача": "hr.zadacha-a", "почему": "%s"}\n' "$(date +%Y-%m-%d)" "$DLINNAYA" >> "$T/пайплайн-обходы.jsonl"
    printf '{"ts": "%s", "задача": "hr.zadacha-a", "почему": "короткая причина"}\n' "$(date +%Y-%m-%d)" >> "$T/пайплайн-обходы.jsonl"
    printf '{"ts": "%s", "задача": "hr.zadacha-b", "почему": "другая причина"}\n' "$(date +%Y-%m-%d)" >> "$T/пайплайн-обходы.jsonl"
    printf '{"ts": "%s", "задача": "hr.zadacha-c", "почему": "третья"}\n' "$(date +%Y-%m-%d)" >> "$T/пайплайн-обходы.jsonl"
    printf '{"ts": "%s", "задача": "hr.zadacha-d", "почему": "четвёртая"}\n' "$(date +%Y-%m-%d)" >> "$T/пайплайн-обходы.jsonl"
    rm -f "$T/tokens-daily.report.вчера"
    OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон с обходами двух задач упал"; echo "$OUT"; exit 1; }
    # текст сводки — от заголовка до следующей строки журнала с отметкой времени
    TEKST=$(echo "$OUT" | awk '/Утренняя сводка/ { on = 1 } on && /^20[0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9:]+  / && !/Утренняя сводка/ { exit } on { print }')
    [ "$(echo "$TEKST" | grep -c 'hr.zadacha-a')" = 1 ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: обходы одной задачи не слиты в одну строку"; echo "$TEKST"; exit 1; }
    echo "$TEKST" | grep -q 'hr.zadacha-a ×3:' && echo "$TEKST" | grep -q 'hr.zadacha-b ×1:' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: строка задачи не несёт точного числа её обходов («<задача> ×N:»)"; echo "$TEKST"; exit 1; }
    echo "$TEKST" | grep -q 'и ещё задач: 1' && ! echo "$TEKST" | grep -q 'hr.zadacha-d' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: больше трёх задач не обрезано до трёх с остатком числом"; echo "$TEKST"; exit 1; }
    [ "$(echo "$TEKST" | grep -c 'слово01')" = 1 ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: повторяющаяся причина названа не один раз"; echo "$TEKST"; exit 1; }
    ZNAK_OBRYVA=$(echo "$TEKST" | grep -o 'слово[0-9]*[^ ]*…' | head -1 || true)
    case "$ZNAK_OBRYVA" in
        слово[0-9][0-9]…) ;;
        *) echo "САМОТЕСТ ПРОВАЛЕН: длинная причина не обрезана по границе слова со знаком обрыва («${ZNAK_OBRYVA:-нет}»)"; echo "$TEKST"; exit 1 ;;
    esac
    SLOVAR=""
    for cand in "$(dirname "$0")/../../scripts/language-level.py" "$(dirname "$0")/../scripts/language-level.py"; do
        [ -f "$cand" ] && { SLOVAR="$cand"; break; }
    done
    [ -n "$SLOVAR" ] || { echo "САМОТЕСТ ПРОВАЛЕН: не найден language-level.py — словарь терминов судить нечем"; exit 1; }
    ZAPRET=$(printf '%s' "$TEKST" | python3 -c "
import importlib.util, sys
s = importlib.util.spec_from_file_location('ll', sys.argv[1]); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
print(', '.join(sorted({w.lower() for w in m.ГЛУБОКИЕ.findall(sys.stdin.read())})))" "$SLOVAR")
    [ -z "$ZAPRET" ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: в сводке слова, которых уровень «простой» не допускает: $ZAPRET"; echo "$TEKST"; exit 1; }
    rm -f "$T/пайплайн-обходы.jsonl"
    # БОЛЬНОЙ СЛУЧАЙ: подробный отчёт расхода длиннее потолка канала. Сторож
    # канала отклоняет такой текст; отчёт уходит вложением, текст — коротким.
    python3 -c "print('РАСХОД ЗА СУТКИ — длинный\n' + '  строка отчёта расхода\n' * 150)" \
        > "$T/tokens-daily.report"
    OUT=$(INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон с длинным отчётом упал"; echo "$OUT"; exit 1; }
    # Имя вложения — копия отчёта с расширением .md (его слово 18.10), а не
    # сам tokens-daily.report: телеграм отдаёт файл без расширения без типа.
    echo "$OUT" | grep -qE "вложение: $T/.*\.md" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: длинный отчёт расхода не ушёл вложением с именем .md"; echo "$OUT"; exit 1; }
    DLINA=$(echo "$OUT" | sed -n 's/.*текст сводки: \([0-9]*\) знаков.*/\1/p' | tail -1)
    [ -n "$DLINA" ] && [ "$DLINA" -lt 1400 ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: текст сводки ${DLINA:-?} знаков — сторож канала его не пропустит"; echo "$OUT"; exit 1; }
    rm -f "$T/tokens-daily.report" "$T/tokens-daily.report.вчера"
    # Д-4-остаток: свежий каталог БЕЗ метки — «ещё не прогонялся», не МЁРТВ;
    # заодно П-15: при TG_CHANNEL_VARIANT="A" — «диспетчер выключен», не крик.
    mkdir -p "$T/hb2"
    cat > "$T/install2.conf" <<EOF
PROJECT_NAME="selftest"
PROJECT_DIR="$T"
AGENT_USER="selftest"
SECRETS_DIR="$T"
TG_TOKEN_FILE="$T/no-token"
TG_CHAT_ID=""
AUTONOMY="semi"
HEARTBEAT_DIR="$T/hb2"
LOG_DIR="$T/log2"
TG_CHANNEL_VARIANT="A"
EOF
    mkdir -p "$T/log2"
    cat > "$T/harness2.conf" <<EOF
HEARTBEAT_PERIODS="gamma:10"
EOF
    OUT=$(INSTALL_CONF="$T/install2.conf" HARNESS_CONF="$T/harness2.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон со свежим каталогом упал"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'gamma — ещё не прогонялся' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: свежеустановленный демон без метки не назван «ещё не прогонялся» (Д-4)"; echo "$OUT"; exit 1; }
    if echo "$OUT" | grep -q '— МЁРТВ'; then
        echo "САМОТЕСТ ПРОВАЛЕН: свежий каталог без метки объявлен МЁРТВым (Д-4 — ложный крик до первого прогона)"; echo "$OUT"; exit 1
    fi
    echo "$OUT" | grep -q 'диспетчер выключен (вариант А — эксперимент)' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: при варианте А нет строки «диспетчер выключен» (П-15)"; echo "$OUT"; exit 1; }
    # маркер установки постарел, метки так и нет → по-прежнему МЁРТВ
    touch -d '2 hours ago' "$T/hb2/.install-stamp"
    OUT=$(INSTALL_CONF="$T/install2.conf" HARNESS_CONF="$T/harness2.conf" \
          HEARTBEAT_DRY_SEND=1 bash "$0") || { echo "САМОТЕСТ ПРОВАЛЕН: прогон со старым маркером упал"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'gamma — МЁРТВ' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: метки нет при старом маркере установки, а gamma не МЁРТВ"; echo "$OUT"; exit 1; }
    # При 20 и больше мёртвых из 27 сводка длиннее потолка канала, сторож
    # канала её отклоняет кодом 1. Отказ двери — не повод её обойти прямым
    # curl: сводка ложится вложением через ту же дверь. Стенд СВОЙ: дверь и curl подменены, прогон
    # из env -i — иначе PROJECT_DIR/TG_CHAT_ID окружения победили бы конфиг
    # стенда, и проба сыграла бы на боевом ([[the-probe-played-on-production]]).
    K="$T/kanal"
    mkdir -p "$K/scripts" "$K/bin" "$K/hb" "$K/log"
    python3 -c "import secrets,io,sys; io.open(sys.argv[1],'w').write('11'+secrets.token_hex(16)+'\n')" "$K/klyuch"
    cat > "$K/scripts/tg_send.sh" <<'DOOR_EOF'
#!/usr/bin/env bash
# Подставная дверь: текст отклоняет кодом STAND_DOOR_TEXT_CODE, вложение —
# STAND_DOOR_FILE_CODE; каждый зов и копию вложения оставляет следом.
if [ "${1:-}" = "--file" ]; then
    printf 'file\t%s\n' "$*" >> "$STAND_DOOR_LOG"
    [ -f "${2:-}" ] && cp -f "$2" "$STAND_DOOR_LOG.vlozhenie"
    exit "${STAND_DOOR_FILE_CODE:-0}"
fi
printf 'text\n' >> "$STAND_DOOR_LOG"
exit "${STAND_DOOR_TEXT_CODE:-0}"
DOOR_EOF
    chmod +x "$K/scripts/tg_send.sh"
    cat > "$K/bin/curl" <<'CURL_EOF'
#!/usr/bin/env bash
printf 'vyzvan\n' >> "$STAND_CURL_LOG"
printf '{"ok":true,"result":{"message_id":1}}'
CURL_EOF
    chmod +x "$K/bin/curl"
    cat > "$K/install.conf" <<EOF
PROJECT_NAME="selftest"
PROJECT_DIR="$K"
AGENT_USER="selftest"
SECRETS_DIR="$K"
TG_TOKEN_FILE="$K/klyuch"
TG_CHAT_ID="100500"
AUTONOMY="semi"
HEARTBEAT_DIR="$K/hb"
LOG_DIR="$K/log"
TG_CHANNEL_VARIANT="A"
EOF
    IMENA=$(python3 -c "print(' '.join('demon%02d:10' % i for i in range(1, 28)))")
    printf 'HEARTBEAT_PERIODS="%s"\n' "$IMENA" > "$K/harness.conf"
    touch -d '30 days ago' "$K/hb/.install-stamp"
    kanal_progon() {  # $1 = код двери на текст, $2 = на вложение; печатает «код;curl;текст;вложение;метка»
        local rc=0
        : > "$K/dver.log"; : > "$K/curl.log"; rm -f "$K/dver.log.vlozhenie" "$K/hb/heartbeat-watch"
        env -i PATH="$K/bin:/usr/local/bin:/usr/bin:/bin" HOME="$K" LC_ALL=C.UTF-8 \
            HARNESS_INSTALL_CONF="$K/install.conf" HARNESS_CONF="$K/harness.conf" \
            STAND_DOOR_LOG="$K/dver.log" STAND_CURL_LOG="$K/curl.log" \
            STAND_DOOR_TEXT_CODE="$1" STAND_DOOR_FILE_CODE="$2" \
            bash "$0" > "$K/out" 2>&1 || rc=$?
        printf '%s;%s;%s;%s;%s\n' "$rc" "$(grep -c . "$K/curl.log")" \
            "$(grep -c '^text' "$K/dver.log")" "$(grep -c '^file' "$K/dver.log")" \
            "$([ -f "$K/hb/heartbeat-watch" ] && echo est || echo net)"
    }
    sostarit() {  # $1 = сколько меток состарить на 30 суток, остальные — свежие
        local i
        for i in $(seq 1 27); do
            if [ "$i" -le "$1" ]; then touch -d '30 days ago' "$K/hb/$(printf 'demon%02d' "$i")"
            else touch "$K/hb/$(printf 'demon%02d' "$i")"; fi
        done
    }
    # Контроль стенда: все живы, дверь принимает текст — вложения нет, curl нет.
    sostarit 0
    ITOG=$(kanal_progon 0 0)
    [ "$ITOG" = "0;0;1;0;est" ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: стенд канала — короткая сводка не прошла дверью текстом (код;curl;текст;вложение;метка = $ITOG, ждали 0;0;1;0;est)"; cat "$K/out"; exit 1; }
    # Больной: 27 мёртвых, сторож канала отклонил текст.
    sostarit 27
    ITOG=$(kanal_progon 1 0)
    [ "$(echo "$ITOG" | cut -d';' -f2)" = 0 ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: отказ двери — сводка ушла прямым curl мимо сторожа канала (curl позван $(echo "$ITOG" | cut -d';' -f2) раз; код;curl;текст;вложение;метка = $ITOG)"; exit 1; }
    [ "$(echo "$ITOG" | cut -d';' -f4)" = 1 ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: сводка не доставлена вложением через дверь после отказа текстом ($ITOG)"; cat "$K/out"; exit 1; }
    grep '^file' "$K/dver.log" | grep -q 'МЁРТВЫХ 27' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: подпись вложения не называет число мёртвых («МЁРТВЫХ 27»)"; cat "$K/dver.log"; exit 1; }
    [ "$(grep -c '— МЁРТВ' "$K/dver.log.vlozhenie" 2>/dev/null)" = 27 ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: во вложении не все 27 мёртвых поимённо"; exit 1; }
    [ "$ITOG" = "0;0;1;1;est" ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: сводка ушла вложением, но прогон не успешен или метки нет ($ITOG, ждали 0;0;1;1;est)"; cat "$K/out"; exit 1; }
    # Дверь отказала и вложению: прогон красный, метки нет, curl нет.
    ITOG=$(kanal_progon 1 1)
    [ "$ITOG" = "1;0;1;1;net" ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: дверь отказала и тексту, и вложению — ждали код 1, curl 0, метки нет (код;curl;текст;вложение;метка = $ITOG)"; cat "$K/out"; exit 1; }
    # Двери нет вовсе: прямого пути у сводки нет (строка снята с храповика
    # owner-senders.yaml) — прогон красный, мёртвого сторожа назовёт sentinel.
    chmod -x "$K/scripts/tg_send.sh"
    ITOG=$(kanal_progon 0 0)
    chmod +x "$K/scripts/tg_send.sh"
    [ "$ITOG" = "1;0;0;0;net" ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: двери нет — сводка обязана провалить прогон, а не уйти прямым curl (код;curl;текст;вложение;метка = $ITOG, ждали 1;0;0;0;net)"; exit 1; }
    echo "САМОТЕСТ ПРОЙДЕН: мёртвая метка названа, живая не оклеветана, прирост лога сторожа замечен,"
    echo "недоставка не съедает сигнал (SIZE_FILE после отправки), юнит диспетчера и токены — в сводке,"
    echo "свежая установка без метки — «ещё не прогонялся», вариант А — «диспетчер выключен»"
    echo "отказ двери — сводка вложением через дверь, а не прямым curl; без двери и при отказе вложению — прогон красный"
    echo "(доставка в Telegram самотестом не проверяется — доказывается на живом при установке, 01-SPEC §7)"
    exit 0
fi

# ── конфиги ─────────────────────────────────────────────────────────────────
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/config.sh"
INSTALL_CONF="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-$(konf_koren)/install.conf}}"
HARNESS_CONF="${HARNESS_CONF:-$(konf_koren)/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "heartbeat-watch: нет $INSTALL_CONF — установка не завершена (01-SPEC §0)"; exit 1; }
[ -r "$HARNESS_CONF" ] || { echo "heartbeat-watch: нет $HARNESS_CONF — скопируйте harness/config/harness.conf в $(konf_koren)/"; exit 1; }
# Окружение старше конфига: общий загрузчик вместо голого source (улика
# 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
konf_zagruzit
: "${HEARTBEAT_DIR:?пуст HEARTBEAT_DIR}" "${HEARTBEAT_PERIODS:?пуст HEARTBEAT_PERIODS — таблица периодов живёт в harness.conf}"

NOW=$(date +%s)

# Д-4-остаток: «метки нет вообще = МЁРТВ» кричал ложно сразу после установки —
# демон просто ещё не успел прогнаться. Маркер установки: файл создаётся этим
# сторожем при ПЕРВОМ прогоне и больше не трогается (каталог HEARTBEAT_DIR не
# годится — его mtime обновляет каждая новая метка). Метки нет при маркере
# моложе период×2 → «ещё не прогонялся», старше → по-прежнему МЁРТВ.
INSTALL_STAMP="$HEARTBEAT_DIR/.install-stamp"
mkdir -p "$HEARTBEAT_DIR"
[ -f "$INSTALL_STAMP" ] || touch "$INSTALL_STAMP"
STAMP_AGE_MIN=$(( (NOW - $(stat -c '%Y' "$INSTALL_STAMP")) / 60 ))

DEAD=""; FRESH=""; ALIVE_N=0; DEAD_N=0; FRESH_N=0
ALIVE_NAMES=(); DEAD_NAMES=(); DEAD_LINES=()

# Таблица: "имя:период-в-минутах …". Порог тревоги = период ×2 — один
# пропущенный прогон прощается (машина могла перезагружаться), два — нет.
for entry in $HEARTBEAT_PERIODS; do
    name="${entry%%:*}"; period_min="${entry##*:}"
    mark="$HEARTBEAT_DIR/$name"
    if [ ! -f "$mark" ]; then
        if [ "$STAMP_AGE_MIN" -lt $((period_min * 2)) ]; then
            FRESH="${FRESH}
• $name — ещё не прогонялся (установлен недавно)"
            FRESH_N=$((FRESH_N + 1))
        else
            DEAD="${DEAD}
• $name — МЁРТВ: метки нет вообще (ни одного успешного прогона за период ×2)"
            DEAD_N=$((DEAD_N + 1))
            DEAD_NAMES+=("$name"); DEAD_LINES+=("$name — МЁРТВ: метки нет вообще (ни одного успешного прогона за период ×2)")
        fi
        continue
    fi
    age_min=$(( (NOW - $(stat -c '%Y' "$mark")) / 60 ))
    if [ "$age_min" -gt $((period_min * 2)) ]; then
        DEAD="${DEAD}
• $name — МЁРТВ: метка ${age_min} мин при периоде ${period_min} (порог ×2)"
        DEAD_N=$((DEAD_N + 1))
        DEAD_NAMES+=("$name"); DEAD_LINES+=("$name — МЁРТВ: метка ${age_min} мин при периоде ${period_min} (порог ×2)")
    else
        ALIVE_N=$((ALIVE_N + 1))
        ALIVE_NAMES+=("$name")
    fi
done

if [ "$DEAD_N" = 0 ] && [ "$FRESH_N" = 0 ]; then
    SUMMARY="Утренняя сводка харнеса: все службы живы ($ALIVE_N/$ALIVE_N)."
elif [ "$DEAD_N" = 0 ]; then
    SUMMARY="Утренняя сводка харнеса: живых $ALIVE_N, МЁРТВЫХ нет, ещё не прогонялись $FRESH_N:$FRESH"
else
    SUMMARY="Утренняя сводка харнеса: живых $ALIVE_N, МЁРТВЫХ $DEAD_N:$DEAD$FRESH
Лечение: лог службы в $LOG_DIR/<имя>.log, прогон вручную в env -i (01-SPEC §9)."
fi

# ── прирост guard_exceptions.log: падения сторожа команд видны в сводке ─────
# Сторож fail-open: упав, он пропускает команду и лишь пишет след в лог.
# Без этой сверки след никто не читает — молчаливый fail-open неотличим от
# здорового сторожа. Прирост НЕ делает прогон красным: демоны-то живы.
GUARD_LOG="${LOG_DIR}/guard_exceptions.log"
SIZE_FILE="$HEARTBEAT_DIR/.guard_exceptions.size"
GUARD_NEWSIZE=""
if [ -f "$GUARD_LOG" ]; then
    prev=$(cat "$SIZE_FILE" 2>/dev/null || echo 0)
    case "$prev" in (''|*[!0-9]*) prev=0;; esac
    cur=$(stat -c '%s' "$GUARD_LOG")
    if [ "$cur" -gt "$prev" ]; then
        n=$(tail -c +"$((prev + 1))" "$GUARD_LOG" | wc -l)
        SUMMARY="$SUMMARY
⚠ сторож команд падал $n раз с прошлой сводки — смотреть guard_exceptions.log"
    fi
    GUARD_NEWSIZE="$cur"
fi

# SIZE_FILE — ТОЛЬКО после доставленной сводки (Д-4а): запись до отправки
# съедала водораздел, и при недоставке сигнал о падениях терялся навсегда.
remember_guard_size() {
    if [ -n "$GUARD_NEWSIZE" ]; then
        mkdir -p "$HEARTBEAT_DIR"
        echo "$GUARD_NEWSIZE" > "$SIZE_FILE"
    fi
}

# ── юнит диспетчера канала (Д-4б): мёртвый диспетчер = глухой владелец ──────
# П-15: при варианте А канала (TG_CHANNEL_VARIANT из install.conf) диспетчера
# НЕТ по замыслу — getUpdates владеет telegram-плагин; красный крик про
# выключенный юнит был бы ложью.
DISPATCHER_UNIT="${DISPATCHER_UNIT:-harness-dispatcher}"
if [ "${TG_CHANNEL_VARIANT:-B}" = "A" ]; then
    SUMMARY="$SUMMARY
диспетчер выключен (вариант А — эксперимент)"
elif command -v systemctl >/dev/null 2>&1; then
    UNIT_STATE=$(systemctl is-active "$DISPATCHER_UNIT" 2>&1 | head -1) || true
    if [ "$UNIT_STATE" = "active" ]; then
        # Живой юнит называется так же, как мёртвый: сводка обязана говорить о
        # диспетчере ВСЕГДА (Д-4б). Молчание о нём читается как «не проверяли».
        SUMMARY="$SUMMARY
диспетчер канала: юнит $DISPATCHER_UNIT active"
    else
        SUMMARY="$SUMMARY
⚠ диспетчер канала: юнит $DISPATCHER_UNIT НЕ active (${UNIT_STATE:-нет ответа}) — входящие владельца не читаются"
    fi
else
    SUMMARY="$SUMMARY
systemctl недоступен — состояние юнита $DISPATCHER_UNIT не проверено"
fi

# ── расход токенов (Д-4в): строку готовит демон-счётчик, мы только включаем ─
# Владелец 18.10.2026: «отчёт об использовании токенов стал приходить файлом, а
# не сообщением. Плюс еще там нет отчёта по моделям» и следом: «подробный фаил
# тоже можно слать, только надо в нём расширение сделать md, просто в сообщении
# как раньше надо основную инфу». Его правило 27.08 — отчёт ТЕКСТОМ, вложение
# только для документа.
#
# Поэтому в СООБЩЕНИЕ уходит выжимка из подробного отчёта (сутки, шаги, ответы,
# кэш и разбивка по моделям), а сам отчёт едет вложением с именем на .md.
# Прежнее решение — «подробный отчёт вытесняет короткую строку» — отменено его
# словом: вытеснение оставляло в канале одну строчку «во вложении».
TOKENS_SUMMARY="${LOG_DIR}/tokens-daily.summary"
TOKENS_REPORT="${LOG_DIR}/tokens-daily.report"
# Утренняя сводка несёт ЗАКОНЧЕННЫЕ сутки, а не восемь часов текущих. 24.09.2026
# владелец прочитал в сводке «Шаги 1», хотя за те сутки модель звали четыре
# раза: отчёт собирался ежечасно и к 08:00 знал только ночь, а заголовок обещал
# «РАСХОД ЗА СУТКИ». Копию прошедшего дня кладёт сборщик (tokens-collector).
[ -f "$TOKENS_REPORT.вчера" ] && TOKENS_REPORT="$TOKENS_REPORT.вчера"
[ -f "$TOKENS_REPORT" ] && TOKENS_SUMMARY="$TOKENS_REPORT"
# Короткая строка счётчика входит в текст; подробный отчёт (тридцать строк)
# уходит вложением: целиком в текст он выводит сводку за потолок канала, и
# сторож канала её отклоняет.
# Выжимка подробного отчёта для канала. Имена переменных awk ЛАТИНИЦЕЙ:
# кириллический идентификатор awk не берёт вовсе — «invalid char in expression»
# ([[cyrillic-in-bash-names]], тот же капкан, что и в bash).
tokens_vyzhimka() {  # $1 = файл подробного отчёта
    awk '
        NR == 1 { day = $NF }
        /^Шаги \(обращения/ { steps = $NF }
        /^Кэш$/ { part = "cache"; next }
        /^Ответы модели/ { part = "answers"; next }
        /^По моделям/ { part = "models"; next }
        /^Отдача/ { part = "output"; next }
        part == "answers" && /^  всего/ { answers = $NF }
        part == "cache" && /^  попаданий/ { hits = $NF }
        part == "models" && /^  [^ ]/ {
            name = $1; st = ""; an = ""
            for (i = 2; i <= NF; i++) {
                if ($i == "шагов")  st = $(i + 1)
                if ($i == "ответов") an = $(i + 1)
            }
            if (name != "<synthetic>" && st != "")
                models = models (models ? "; " : "") name " — шагов " st ", ответов " an
        }
        part == "output" && /^  коммитов за сутки/ { commits = $NF }
        part == "output" && /^  шагов на коммит/ { per_commit = $NF }
        END {
            printf "Расход за сутки (%s): шагов %s, ответов модели %s, повторно прочитано (дёшево) %s.\n",
                   day, steps, answers, hits
            if (models)  printf "По моделям: %s.\n", models
            if (commits) printf "Правок сохранено %s, шагов на правку %s.\n", commits, per_commit
        }
    ' "$1"
}

TOKENS_FILE=""
if [ -f "$TOKENS_REPORT" ]; then
    SUMMARY="$SUMMARY
$(tokens_vyzhimka "$TOKENS_REPORT")"
    # Имя вложения — с расширением .md (его слово 18.10): без него телеграм
    # отдаёт файл без типа, и владелец не может его открыть прямо в чате.
    TOKENS_FILE="${LOG_DIR}/расход-за-сутки.md"
    cp -f "$TOKENS_REPORT" "$TOKENS_FILE" 2>/dev/null || TOKENS_FILE="$TOKENS_REPORT"
elif [ -f "$TOKENS_SUMMARY" ]; then
    SUMMARY="$SUMMARY
$(head -n 3 "$TOKENS_SUMMARY")"
fi

# ── Обходы гейта пайплайна: честная дверь обязана быть ВИДНА ────────────────
# Ревью спеки 23.09 (I-2): спека утверждала «дверь видна на панели», а журнал
# обходов не читал НИКТО — ни панель, ни сводка. Дверь, которую видит только
# тот, кто в неё прошёл, — это не видимое решение, а тихий обход. Сводка
# называет число за сутки и причины: смена отвечает за каждую.
OBHODY="${LOG_DIR}/пайплайн-обходы.jsonl"
SLOVAR_TERMINOV=""
for cand in "$(dirname "$0")/../../scripts/language-level.py" "$(dirname "$0")/../scripts/language-level.py"; do
    [ -f "$cand" ] && { SLOVAR_TERMINOV="$cand"; break; }
done
if [ -f "$OBHODY" ]; then
    OBHODY_TEKST=$(python3 - "$OBHODY" "$SLOVAR_TERMINOV" <<'PYEOF' 2>/dev/null || true
import datetime, importlib.util, json, sys
вчера = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
сегодня = datetime.date.today().isoformat()
по_задачам = {}
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        for строка in fh:
            строка = строка.strip()
            if not строка:
                continue
            try:
                з = json.loads(строка)
            except ValueError:
                continue
            if str(з.get("ts", ""))[:10] in (вчера, сегодня):
                по_задачам.setdefault(з.get("задача", "-"), []).append(з.get("почему", ""))
except OSError:
    sys.exit()


# Причина — живой текст смены, и в нём бывают слова, которых уровень «простой» не
# допускает. Что считать таким словом, судит ГЛУБОКИЕ из language-level.py (тот
# же список, что у сторожа переписки); здесь только чем заменить — по ОСНОВЕ,
# падеж не учитывается. Слово без замены остаётся и видно замечанием сторожа.
ЗАМЕНА = {"пайплайн": "трубы", "гейт": "проверка", "коммит": "правка", "ротаци": "смена сессии",
          "репозитори": "хранилище", "конфиг": "настройки", "хук": "перехватчик",
          "демон": "служба", "кэш": "готовое", "бэкап": "копия", "дамп": "копия базы",
          "миграци": "перенос базы", "индекс": "указатель", "валидац": "проверка"}


def обиходно(текст):
    путь = sys.argv[2] if len(sys.argv) > 2 else ""
    if not путь:
        return текст
    спец = importlib.util.spec_from_file_location("уровень", путь)
    модуль = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(модуль)

    def заменить(м):
        слово = м.group(0).lower()
        return next((в for о, в in ЗАМЕНА.items() if слово.startswith(о)), м.group(0))
    return модуль.ГЛУБОКИЕ.sub(заменить, текст)


def по_слову(текст, предел=90):
    # Резать по знаку рвёт слово («не работа по спе»); рвём по пробелу и ставим «…».
    if len(текст) <= предел:
        return текст
    return текст[:предел].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"


всего = sum(len(п) for п in по_задачам.values())
if всего:
    print(f"Обходов трубы за сутки: {всего} (задач {len(по_задачам)}).")
    for задача, почему in list(по_задачам.items())[:3]:
        главная = max(dict.fromkeys(почему), key=почему.count)
        print(f"  • {задача if задача != '-' else 'без задачи'} ×{len(почему)}: {по_слову(обиходно(главная))}")
    if len(по_задачам) > 3:
        print(f"  …и ещё задач: {len(по_задачам) - 3}")
PYEOF
)
    [ -n "$OBHODY_TEKST" ] && SUMMARY="$SUMMARY
$OBHODY_TEKST"
fi

say "$SUMMARY"
[ -n "$TOKENS_FILE" ] && say "$(cat "$TOKENS_FILE")"
say "текст сводки: $(LC_ALL=C.UTF-8 bash -c 'printf %s "${#1}"' _ "$SUMMARY") знаков${TOKENS_FILE:+, вложение: $TOKENS_FILE}"

# Самотест не ходит в сеть: доставка доказывается на живом при установке.
if [ "${HEARTBEAT_DRY_SEND:-0}" = 1 ]; then
    say "(HEARTBEAT_DRY_SEND=1 — отправка пропущена)"
    [ -n "$TOKENS_FILE" ] && say "(вложение: $TOKENS_FILE)"
    remember_guard_size
    exit 0
fi

# ── задачи модели: мёртвый демон — карточка, живой гасит свою ───────────────
# Сводка у владельца одна, а ключей несколько, поэтому писатель зовётся прямо:
# дверь берёт один --task на весть. При массовом падении (≥ ALERT_MASS_MIN)
# карточка одна на всех — 27 карточек про одно событие заглушили бы очередь.
# Писателя нет — сторож не падает, сводка владельцу идёт как шла.
alert_task() {  # $@ = доводы писателя
    [ -f "$PROJECT_DIR/scripts/alert-task.py" ] || return 0
    python3 "$PROJECT_DIR/scripts/alert-task.py" "$@" </dev/null >/dev/null 2>&1 || true
}
# Писатель принимает ключ только [a-z0-9][a-z0-9:._-]: имя демона вне формы
# (кириллица в «session-warden.запуск») получает латинский хвост + хэш имени,
# чтобы ключ был детерминирован, различал разные имена и годился для гашения.
dead_slug() {
    local name="$1" ascii
    if [[ "$name" =~ ^[a-z0-9][a-z0-9._-]{0,50}$ ]]; then
        printf '%s' "$name"
        return
    fi
    ascii=$(printf '%s' "$name" | LC_ALL=C tr -c 'a-z0-9._-' '-' | sed 's/^[^a-z0-9]*//' | cut -c1-40)
    printf '%s-%s' "${ascii:-d}" "$(printf '%s' "$name" | sha1sum | cut -c1-8)"
}
sync_tasks() {
    local i name mass_min="${ALERT_MASS_MIN:-3}"
    if [ "$DEAD_N" -ge "$mass_min" ]; then
        alert_task --key heartbeat-watch:mass --title "${SUMMARY%%$'\n'*}" --text "$SUMMARY"
    else
        alert_task --resolve heartbeat-watch:mass
        for i in "${!DEAD_NAMES[@]}"; do
            alert_task --key "heartbeat-watch:dead:$(dead_slug "${DEAD_NAMES[$i]}")" \
                --title "Демон ${DEAD_NAMES[$i]} мёртв" --text "${DEAD_LINES[$i]}"
        done
    fi
    for name in "${ALIVE_NAMES[@]}"; do
        alert_task --resolve "heartbeat-watch:dead:$(dead_slug "$name")"
    done
}
sync_tasks

# ── отправка: ВСЕГДА, успех только при ok:true ──────────────────────────────
tg_deliver() {
    if [ ! -x "$PROJECT_DIR/scripts/tg_send.sh" ]; then
        say "КАНАЛ НЕ НАСТРОЕН: двери $PROJECT_DIR/scripts/tg_send.sh нет — сводке некуда идти"
        return 1
    fi
    # Дверь зовётся БЕЗ ключей: сводка несёт перечень МЁРТВЫХ демонов
    # (страховка И-1), она за сутки одна, и при лимите обязана уехать
    # досылом, а не пропасть.
    "$PROJECT_DIR/scripts/tg_send.sh" "$SUMMARY" && return 0
    # Отказ двери (потолок длины сторожа канала) — не повод её обойти: перечень
    # мёртвых — документ, он уходит вложением через ту же дверь.
    local doc="$LOG_DIR/сводка-$(date +%F).md"
    printf '%s\n' "$SUMMARY" > "$doc"
    say "текстом не прошло — шлю вложением $doc"
    "$PROJECT_DIR/scripts/tg_send.sh" --file "$doc" \
        --caption "Утренняя сводка: МЁРТВЫХ $DEAD_N — список во вложении"
}

if tg_deliver; then
    say "сводка доставлена (ok:true)"
    if [ -n "$TOKENS_FILE" ]; then
        "$PROJECT_DIR/scripts/tg_send.sh" --file "$TOKENS_FILE" \
            --caption "Расход токенов за прошедшие сутки — подробный отчёт" \
            || say "отчёт расхода вложением НЕ доставлен — сводка ушла без него"
    fi
    remember_guard_size
else
    # Недоставленная сводка = сторож молчит = сторож мёртв. Метку не ставим,
    # выходим с ошибкой — след остаётся хотя бы в логе cron.
    say "сводка НЕ доставлена — считаю прогон ПРОВАЛЕННЫМ"
    exit 1
fi

touch "$HEARTBEAT_DIR/heartbeat-watch"
