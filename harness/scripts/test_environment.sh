#!/usr/bin/env bash
#
# test_environment.sh — проба изолированной среды разработки. Каталог среды —
# ДАННЫЕ (PRODUCT_PATH в harness.conf), стенд подменяет его флагом.
#
# Написана ДО среды и обязана краснеть, пока среды нет: это красный шаг ТДД
# (план и спека изолированной среды от 28.09.2026, критерии П-1…П-3, П-6).
#
# Режимы:
#   --подъём   П-1: среда поднимается, база healthy, web отдаёт 200 с маркером
#   --порт     П-2: 8090 слушает ТОЛЬКО 127.0.0.1
#   --соседи   П-6: службы харнеса active, чужие контейнеры те же, что до подъёма
#   --чужое    П-3: снимки docker ДО и ПОСЛЕ, чужое множество ДО ⊆ ПОСЛЕ
#   --память   сумма лимитов служб среды не выше потолка
#   --архив    П-4: архив даёт РАБОЧУЮ копию — другое имя, другой порт, те же данные
#   --снятие   П-5, П-6: снятие доказывается на КОПИИ среды, живая не трогается
#   --selftest самопроверка самой пробы на СВОИХ стендах; с «--группа <имя>»
#              гоняется ОДНА группа (порт готовность память соседи окно жнец
#              данные перенос следы состав_архива дверь счёт_снятия) — при
#              разборе отказа это минуты
#              вместо всей самопроверки
#
# Коды возврата: 0 — прошла, 1 — отказ с названной причиной, 2 — замер сейчас
# не делается (ночное окно). Два разных «не зелено» разделены нарочно: отказ от
# замера в окне ночных механизмов — не дефект среды, и красить им карточку
# значит тратить день на несуществующую причину.
#
# Три правила, которых стоила разработка контракта задачи:
#  * Объекты среды отбираются по МЕТКЕ compose, а не по имени: docker
#    `--filter name=` сравнивает ПОДСТРОКОЙ, и фильтр по имени проекта совпал
#    бы с его томом `<проект>_dbdata` — жнец снёс бы базу среды, а архива на
#    Ф1 ещё нет (И-1).
#  * Лимит памяти судится полем .HostConfig.Memory живого контейнера: mem_limit
#    в compose — легаси-ключ, парсер вправе отбросить его молча.
#  * Список префиксов и окно — ДАННЫЕ (harness/config/environment-prefixes.yaml),
#    а не условие внутри пробы.
#
# Прогон — ТОЛЬКО фоном (указание владельца 23.08.2026):
#   setsid nohup bash scripts/test_environment.sh --selftest \
#     > /var/log/harness/proba-sredy.log 2>&1 < /dev/null &
set -uo pipefail

KOREN_SKRIPTA="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

# ── Настройки, которые проба знает про СЕБЯ (стенд подменяет их флагами) ─────
# Каталог среды — ДАННЫЕ (PRODUCT_PATH в harness.conf): вшитый путь это след
# нашей установки в общем инструменте и поломка у того, чей продукт лежит
# иначе. Конфиг читается ЗДЕСЬ, выше умолчаний: значение из окружения старше
# файла, чтобы стенд пробы не играл на производстве.
SREDA_KOREN="${SREDA_KOREN:-$(sed -n 's/^PRODUCT_PATH="\(.*\)"$/\1/p' \
    "${HARNESS_CONF:-/etc/harness/harness.conf}" 2>/dev/null | tail -1)}"
DANNYE="$KOREN_SKRIPTA/../harness/config/environment-prefixes.yaml"
PORT=8090
# Маркер тела и потолок памяти — ДАННЫЕ (среда.маркер, среда.потолок_мб в
# DANNYE): приезд приложения на порт сменил бы маркер, и проба краснела бы разом
# в трёх режимах словами «по порту отвечает не среда», уводя разбор не туда;
# третья служба упиралась бы в вписанный числом потолок (ревью кода 28.09.2026,
# I-9). Здесь только умолчания на случай, когда ключа в данных нет.
MARKER=""
POTOLOK_MB=1536
OZHIDANIE_S=90                  # решение смены 2: ждём healthy 90 секунд
SHIRINA_OKNA_S=60               # П-3: оба снимка в окне не шире 60 с
# Свой двор пробы. Объявляется ЗДЕСЬ, потому что заводит эти объекты тоже проба;
# чужие префиксы — данными. Префикс СВОЙ, а не производный от имени проекта
# среды: `--filter name=` сравнивает ПОДСТРОКОЙ, и префикс, начинающийся с
# имени проекта, захватывал бы его собственные тома (`<проект>_dbdata`) —
# жнец снёс бы базу среды. Родня по приёму — harness-restore-proba- и
# harness-install-proba- в environment-prefixes.yaml.
PREFIKS_PROBY="harness-sreda-proba-"
# Диапазон, из которого берётся порт КОПИИ (П-4: копия поднимается на другом
# порту). Занятость проверяется перед подъёмом: «port is already allocated» от
# docker не называет, КТО держит порт, и разбор такого отказа стоит смены.
PORT_KOPII_S=18090
PORT_KOPII_PO=18119
# Контрольная таблица П-4. Она заводится в ОРИГИНАЛЕ до сбора архива и ищется в
# КОПИИ: иначе «копия поднялась» доказывает только то, что postgres умеет
# заводить пустую базу, а не то, что том переехал.
TABLICA_PROBY="proba_perenosa"
STROK_V_TABLICE=137
# Возраст, старше которого временный каталог пробы считается брошенным. Возраст,
# а не имя: жнец работает и из ДРУГОГО процесса пробы, и без этого замка он снёс
# бы двор живого соседнего прогона (поймано на самопроверке: сквозной случай
# зовёт `bash $0 --архив`, и жнец в потомке убивал стенд родителя).
VOZRAST_DVORA_MIN=60

# Двор текущего прогона: объявляется ЗДЕСЬ, потому что его убирает трап, а
# local к моменту трапа уже не существует (живой прогон, см. самопроверку).
DVOR_ARHIVA=""
KOPIYA_PROEKT=""
PORT_KOPII=""
# Двор режима --снятие: копия среды, её архив и подставной каталог журналов.
# Тоже переменные ФАЙЛА, а не local: их убирает трап EXIT.
DVOR_SNYATIYA=""
SNYATIE_PROEKT=""
# Где стоит контрольная таблица П-4. Она заводится в базе ЖИВОЙ среды, и убирать
# её обязан ТРАП, а не строка в конце удачного пути: любой из семи отказов между
# заведением и сверкой оставлял чужую таблицу в базе среды навсегда — а когда в
# неё приедет приложение, такая таблица стала бы данными из ниоткуда (ревью кода
# 28.09.2026, I-7).
TABLICA_KONT=""
TABLICA_USER=""
TABLICA_BAZA=""

# Режим прогона: объявляется ЗДЕСЬ, а разбирается внизу — им назван файл журнала
# снимков, и под `set -u` неизвестная переменная роняла бы замер.
REZHIM=""
# Одна группа самопроверки вместо всех (--группа): пусто — гонятся все.
GRUPPA=""

# Данные из yaml (заполняет прочитать_данные)
METKA=""
PROEKT=""
OKNO_S=""
OKNO_PO=""
PREFIKSY=""
OSTANOVKA_S=""
OSTANOVKA_PO=""

# Куда пишутся снимки П-3. Критерий П-3 требует «оба снимка в журнал», а снимки
# только печатались в поток: критерий был объявлен выполненным без носителя
# (ревью кода 28.09.2026, I-16). Каталог журналов берётся у машины — тем же
# ключом, каким его знает харнес.
LOG_DIR="${HARNESS_LOG_DIR:-}"
if [ -z "$LOG_DIR" ] && [ -r /etc/harness/install.conf ]; then
    LOG_DIR=$(sed -n 's/^LOG_DIR=//p' /etc/harness/install.conf | tr -d '"' | head -1)
fi
LOG_DIR="${LOG_DIR:-/var/log/harness}"
ZHURNAL_SNIMKOV="$LOG_DIR/environment"

usage() {
    sed -n '3,30p' "$0"
    exit 1
}

# ── ДАННЫЕ ──────────────────────────────────────────────────────────────────
# «Не прочитали» и «пусто» — разные ответы: закрытый источник, принятый за
# пустой, красит проверку зелёным молча ([[unreadable-source-turns-check-green]]).
прочитать_данные() {
    local vyvod rc
    vyvod=$(python3 - "$DANNYE" <<'PY'
import sys
import yaml

d = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
sreda = d.get("среда") or {}
okno = d.get("окно_без_снимков") or {}
print(sreda.get("метка", ""))
print(sreda.get("проект", ""))
print(okno.get("с", ""))
print(okno.get("по", ""))
print(" ".join(d.get("исключить_префиксы") or []))
print(sreda.get("маркер", ""))
print(sreda.get("потолок_мб", ""))
print((d.get("окно_без_остановки") or {}).get("с", ""))
print((d.get("окно_без_остановки") or {}).get("по", ""))
PY
    )
    rc=$?
    if [ "$rc" != 0 ]; then
        printf 'данные не прочитаны: %s (python3 ответил %s) — судить нечем\n' "$DANNYE" "$rc"
        return 1
    fi
    METKA=$(printf '%s\n' "$vyvod" | sed -n 1p)
    PROEKT=$(printf '%s\n' "$vyvod" | sed -n 2p)
    # Подстановка «{продукт}» в данных раскрывается ИМЕНЕМ из harness.conf:
    # вписанное имя было бы следом установки в общем инструменте. Имя пусто —
    # подстановка не раскрывается, и проба отказывает ниже словами «данные
    # неполны», а не судит среду по имени «{продукт}».
    local produkt
    produkt=$(sed -n 's/^PRODUCT_DIR="\(.*\)"$/\1/p' \
        "${HARNESS_CONF:-/etc/harness/harness.conf}" 2>/dev/null | tail -1)
    PROEKT=${PROEKT//\{продукт\}/$produkt}
    OKNO_S=$(printf '%s\n' "$vyvod" | sed -n 3p)
    OKNO_PO=$(printf '%s\n' "$vyvod" | sed -n 4p)
    PREFIKSY=$(printf '%s\n' "$vyvod" | sed -n 5p)
    MARKER=$(printf '%s\n' "$vyvod" | sed -n 6p)
    MARKER=${MARKER//\{продукт\}/$produkt}
    OSTANOVKA_S=$(printf '%s\n' "$vyvod" | sed -n 8p)
    OSTANOVKA_PO=$(printf '%s\n' "$vyvod" | sed -n 9p)
    # Потолок памяти: пустой ключ — это «в данных не назван», а не «ноль».
    # Нулевой потолок покрасил бы любую среду, и причина указала бы на память
    # вместо данных, поэтому нечисло оставляет умолчание файла.
    local potolok
    potolok=$(printf '%s\n' "$vyvod" | sed -n 7p)
    case "$potolok" in
        ''|*[!0-9]*) ;;
        *) POTOLOK_MB="$potolok" ;;
    esac
    # Свой префикс добавляется к чужим: объекты ЭТОЙ пробы между снимками
    # появляются и исчезают законно.
    PREFIKSY="$PREFIKSY $PREFIKS_PROBY"
    if [ -z "$METKA" ] || [ -z "$PROEKT" ] || [ -z "$OKNO_S" ] || [ -z "$OKNO_PO" ]; then
        printf 'данные неполны: в %s нет ключей среда.метка/среда.проект/окно_без_снимков\n' "$DANNYE"
        return 1
    fi
    return 0
}

# ── ОКНО НОЧНЫХ МЕХАНИЗМОВ ──────────────────────────────────────────────────
минуты() { # $1 = ЧЧ:ММ
    local t="$1"
    printf '%s' "$(( 10#${t%%:*} * 60 + 10#${t##*:} ))"
}

в_окне() { # $1 = сейчас ЧЧ:ММ, $2 = с, $3 = по → 0, если внутри
    local a b c
    a=$(минуты "$1"); b=$(минуты "$2"); c=$(минуты "$3")
    [ "$a" -ge "$b" ] && [ "$a" -le "$c" ]
}

# ── Поиск в тексте: ГЕРЕСТРОКОЙ, а не через трубу ───────────────────────────
# `printf '%s' "$текст" | grep -q «что»` под `set -o pipefail` возвращает 141,
# если grep нашёл совпадение РАНО и вышел, а printf не успел дописать остаток:
# конвейеру достаётся SIGPIPE, и «нашлось» читается как «не нашлось». Пока тексты
# влезали в буфер трубы (64 КБ), этого не случалось; перечень архива каркаса приложения
# — 5342 записи, и судья состава отказал на архиве, где всё на месте (живой
# прогон 29.09.2026). Тело ответа приложения вырастет так же.
содержит() { # $1 = текст, $2 = подстрока → 0, если есть
    grep -qF -- "$2" <<< "$1"
}

есть_строка() { # $1 = текст, $2 = строка ЦЕЛИКОМ → 0, если есть
    grep -qxF -- "$2" <<< "$1"
}

содержит_образец() { # $1 = текст, $2 = регулярное выражение → 0, если есть
    grep -qE -- "$2" <<< "$1"
}

# ── СУДЬИ (чистые функции: вход — текст, выход — причина и код) ──────────────
# Каждый судья отделён от добычи данных нарочно: только так у режима есть
# красный и зелёный случай на СВОЁМ стенде, а не совпадающий с машиной
# ([[a-check-creates-its-own-condition]]).

порт_судить() { # $1 = вывод `ss -tlnH 'sport = :ПОРТ'`, $2 = порт
    local vyvod="$1" port="$2" adresa vsego naruzhu svoi
    adresa=$(printf '%s\n' "$vyvod" | awk 'NF {print $4}')
    vsego=$(printf '%s\n' "$adresa" | grep -c . || true)
    naruzhu=$(printf '%s\n' "$adresa" | grep -cE "^(0\.0\.0\.0|\[::\]|\*):$port\$" || true)
    svoi=$(printf '%s\n' "$adresa" | grep -cE "^127\.0\.0\.1:$port\$" || true)
    if [ "$naruzhu" != 0 ]; then
        printf 'порт %s смотрит наружу: строк 0.0.0.0/[::] — %s, внешняя поверхность сервера выросла\n' \
            "$port" "$naruzhu"
        return 1
    fi
    if [ "$svoi" != 1 ]; then
        printf 'порт %s: строк 127.0.0.1 — %s, ждали ровно 1 (всего слушателей %s)\n' \
            "$port" "$svoi" "$vsego"
        return 1
    fi
    if [ "$vsego" != 1 ]; then
        printf 'порт %s слушают лишние адреса: всего строк %s, своих 1\n' "$port" "$vsego"
        return 1
    fi
    printf 'порт %s: ровно одна строка 127.0.0.1:%s, наружу 0\n' "$port" "$port"
    return 0
}

готовность_судить() { # $1 = ответ docker inspect про Health
    case "${1:-}" in
        healthy)
            printf 'база готова: healthcheck healthy\n'; return 0 ;;
        НЕТ-HEALTHCHECK)
            printf 'healthcheck у базы НЕ объявлен — это дыра в compose, а не готовность\n'; return 1 ;;
        "")
            printf 'статус базы не прочитан: docker не ответил — судить нечем\n'; return 1 ;;
        *)
            printf 'база не готова: статус «%s»\n' "$1"; return 1 ;;
    esac
}

web_судить() { # $1 = код ответа, $2 = тело, $3 = маркер («» — судить только кодом)
    if [ "${1:-}" != 200 ]; then
        printf 'web ответил «%s», а не 200\n' "${1:-нет ответа}"
        return 1
    fi
    # Пустой маркер — законный случай, а не дыра: маркер задаётся ДАННЫМИ, и
    # приложение на этом порту заглушку с маркером сменит. Тогда судить телу
    # нечем, и проба говорит это вслух, а не краснеет «отвечает не среда»
    # (ревью кода 28.09.2026, I-9).
    if [ -z "${3:-}" ]; then
        printf 'web: код 200; маркер данными не задан — тело не судим\n'
        return 0
    fi
    if ! содержит "$2" "$3"; then
        printf 'в теле ответа нет маркера «%s» — по порту отвечает не среда\n' "$3"
        return 1
    fi
    printf 'web: код 200, маркер «%s» на месте\n' "$3"
    return 0
}

# ── СУДЬИ ПЕРЕНОСА (П-4) ────────────────────────────────────────────────────
# Каждый отделён от добычи: сумма судится по файлам стенда, таблица — по двум
# числам и двум маркам, порт — по тексту ss. Живой прогон подставляет им
# настоящие значения, самопроверка — свои.

сумма_судить() { # $1 = путь архива (файл суммы ждём рядом, «<архив>.sha256»)
    local archive="$1" sum_file="$1.sha256" dir base out rc
    if [ ! -f "$archive" ]; then
        printf 'архива нет: %s — переносить нечего\n' "$archive"
        return 1
    fi
    if [ ! -f "$sum_file" ]; then
        printf 'рядом с архивом нет файла суммы %s — сходимость проверить нечем\n' "$sum_file"
        return 1
    fi
    dir=$(dirname "$archive")
    base=$(basename "$sum_file")
    # Считает ЧУЖАЯ команда, а не своя арифметика: sha256sum -c сам скажет, что
    # именно не сошлось, и его слова уходят в причину.
    out=$(cd "$dir" && sha256sum -c "$base" 2>&1)
    rc=$?
    if [ "$rc" != 0 ]; then
        printf 'sha256 архива НЕ сходится: %s\n' "$(printf '%s' "$out" | tr '\n' ';')"
        return 1
    fi
    printf 'sha256 архива сходится: %s\n' "$(awk '{print $1}' "$sum_file")"
    return 0
}

# Сумма доказывает только саму себя (ревью кода 28.09.2026, C-3), поэтому
# состав архива судит ТОТ ЖЕ код, что и дверь снятия: две копии одного правила
# разъезжаются, и разъехавшись, проба зеленела бы на архиве, который снятие
# считает негодным. Приём тот же, что у базовый_образ: файл снятия подключается
# ради функций (TEARDOWN_TOLKO_FUNKCII), а не переписывается здесь.
состав_архива_снятием() { # $1 = teardown.sh, $2 = архив, $3 = проект, $4 = тома через пробел, $5 = каталог среды («нет» — не сверять), $6 = предел часов (пусто — взять у снятия)
    TEARDOWN_TOLKO_FUNKCII=1 bash -c '
        grep -q "^состав_архива_судить()" "$1" || {
            printf "в %s нет судьи состава архива — сверять архив с тем, что удаляется, нечем\n" "$1"
            exit 1
        }
        source "$1" "$3" >/dev/null 2>&1
        состав_архива_судить "$2" "$3" "$4" "${6:-$ARHIV_MAX_AGE_H}" "$5"' \
        _ "$1" "$2" "$3" "$4" "$5" "${6:-}"
}

# ── C-6: имя проекта копии спрашивается у compose ФАКТОМ ────────────────────
# Копия П-4 поднимается из РАСПАКОВАННОГО архива, где compose.yaml — точная
# копия боевого с именем ЖИВОГО проекта, и от среды её отделял один флаг `-p`.
# Не сработай он — путь отказа вёл к `down -v` по проекту живой среды, то есть
# к удалению тома базы (ревью кода 28.09.2026, C-6).
имя_проекта_судить() { # $1 = ожидаемое имя проекта копии, $2 = что ответил compose
    if [ -z "${2:-}" ]; then
        printf 'compose не назвал имя проекта копии — флаг «-p %s» не доказан, а том живой среды в одном «down -v» отсюда\n' "$1"
        return 1
    fi
    if [ "$2" != "$1" ]; then
        printf 'compose считает проектом копии «%s», а не «%s» — «down -v» ушёл бы по чужому проекту (И-1)\n' "$2" "$1"
        return 1
    fi
    printf 'compose подтвердил имя проекта копии: %s\n' "$2"
    return 0
}

вне_среды_судить() { # $1 = путь архива, $2 = корень среды
    case "$1/" in
        "$2"/*)
            printf 'архив %s лежит ВНУТРИ каталога среды %s — снятие среды унесло бы и его\n' "$1" "$2"
            return 1 ;;
    esac
    printf 'архив лежит вне каталога среды %s\n' "$2"
    return 0
}

# Марка сверяется наравне с числом строк: одно число совпадёт и со СТАРЫМ томом,
# уцелевшим от прошлого прогона, и тогда зелёная проба доказывала бы, что
# переехал какой-то том, а не этот архив.
таблица_судить() { # $1 = строк в оригинале, $2 = строк в копии, $3 = марка оригинала, $4 = марка копии
    case "${2:-}" in
        ''|*[!0-9]*)
            printf 'база копии ответила о числе строк не числом: «%s» — таблицы в копии нет\n' "${2:-нет ответа}"
            return 1 ;;
    esac
    if [ "$2" != "$1" ]; then
        printf 'в копии %s строк контрольной таблицы, в оригинале %s — том переехал не целиком\n' "$2" "$1"
        return 1
    fi
    if [ "${4:-}" != "$3" ]; then
        printf 'марка в копии «%s», в оригинале «%s» — база копии не из этого архива\n' "${4:-нет}" "$3"
        return 1
    fi
    printf 'контрольная таблица: %s строк, марка «%s» — та же, что в оригинале\n' "$2" "$3"
    return 0
}

порт_свободен_судить() { # $1 = порт, $2 = вывод `ss -tlnH sport = :ПОРТ`
    local busy
    busy=$(printf '%s\n' "${2:-}" | awk 'NF' | wc -l)
    if [ "$busy" != 0 ]; then
        printf 'порт %s занят (%s слушателей): %s\n' "$1" "$busy" "$(printf '%s' "$2" | tr '\n' ';')"
        return 1
    fi
    printf 'порт %s свободен\n' "$1"
    return 0
}

# Копия судится КОДОМ и МАРКЕРОМ, а не совпадением тел байт в байт. Сверка тел
# держалась на том, что по порту стоит статическая заглушка: любое приложение
# отдаёт в разметке время, csrf-токен или номер сессии, и путь стал бы вечно
# красным на ровном месте (ревью кода 28.09.2026, I-9). Что переехали ДАННЫЕ, а
# не просто поднялся пустой postgres, доказывает таблица_судить — числом строк и
# маркой; отличие тел печатается справкой, находкой не считается.
копия_судить() { # $1 = код копии, $2 = тело копии, $3 = тело оригинала, $4 = маркер
    web_судить "$1" "$2" "$4" || return 1
    if [ "$2" = "$3" ]; then
        printf 'ответ копии совпал с оригиналом байт в байт (%s знаков)\n' "${#2}"
    else
        printf 'ответ копии: код 200 и маркер на месте; тело отличается от оригинала (%s знаков против %s) — данные судит контрольная таблица\n' \
            "${#2}" "${#3}"
    fi
    return 0
}

# ── СУДЬИ СНЯТИЯ (П-5, П-6) ─────────────────────────────────────────────────
# Пустоту считает САМА проба, а не читает вывод снятия: проверяемый не может
# быть автором доказательства своей работы
# ([[a-trace-is-evidence-only-if-someone-else-wrote-it]]).
# «Не смогли прочитать» и «ноль» — разные ответы. Отказ docker, прочитанный
# как «0», означал бы «следов не осталось» при живом томе базы (ревью кода
# 28.09.2026, C-5, запись [[command-failure-read-as-empty]]).
счёт_судить() { # $1 = что считали, $2 = ответ счётчика → 0, если это ЧИСЛО
    case "${2:-}" in
        ''|*[!0-9]*)
            printf '%s: счёт не прочитан — «%s»\n' "$1" "${2:-нет ответа}"
            return 1 ;;
    esac
    return 0
}

следов_судить() { # $1..$4 = контейнеров, томов, сетей, образов; $5 = каталог (есть|нет)
    local kont="$1" tom="$2" sett="$3" obraz="$4" kat="$5" ostalos
    счёт_судить контейнеры "$kont" || return 1
    счёт_судить тома "$tom" || return 1
    счёт_судить сети "$sett" || return 1
    счёт_судить образы "$obraz" || return 1
    ostalos=$(( kont + tom + sett + obraz ))
    if [ "$ostalos" != 0 ]; then
        printf 'следы остались: контейнеров %s, томов %s, сетей %s, образов %s\n' \
            "$kont" "$tom" "$sett" "$obraz"
        return 1
    fi
    if [ "$kat" != нет ]; then
        printf 'следы остались: каталог среды на месте\n'
        return 1
    fi
    printf 'следов нет: контейнеров 0, томов 0, сетей 0, образов 0, каталога нет\n'
    return 0
}

# Второй замок поверх фильтра метки: снятие зовётся ТОЛЬКО на копию пробы.
# Ошибка имени здесь стоила бы тома с данными среды (И-1), поэтому проверяются
# оба — и имя проекта, и каталог: одно имя без каталога снесло бы чужое дерево,
# один каталог без имени — чужие тома.
копия_снятия_своя() { # $1 = имя проекта, $2 = каталог копии
    case "$1" in
        "$PREFIKS_PROBY"*) ;;
        *)  printf 'снятие НЕ зовётся: имя проекта «%s» не начинается с префикса пробы %s\n' \
                "$1" "$PREFIKS_PROBY"
            return 1 ;;
    esac
    case "$2" in
        "/tmp/$PREFIKS_PROBY"*) ;;
        *)  printf 'снятие НЕ зовётся: каталог «%s» лежит вне двора пробы /tmp/%s*\n' \
                "$2" "$PREFIKS_PROBY"
            return 1 ;;
    esac
    printf 'копия своя: проект %s, каталог %s\n' "$1" "$2"
    return 0
}

отказ_двери_судить() { # $1 = код снятия, $2 = вывод, $3 = ждём в причине, $4 = контейнеров у копии после
    if [ "${1:-}" = 0 ]; then
        printf 'снятие прошло БЕЗ слова владельца — двери нет: %s\n' "$(printf '%s' "$2" | tail -3 | tr '\n' ';')"
        return 1
    fi
    if ! содержит "$2" "$3"; then
        printf 'отказ был, но причина не названа словами «%s»: %s\n' "$3" "$(printf '%s' "$2" | tail -3 | tr '\n' ';')"
        return 1
    fi
    if [ "${4:-0}" = 0 ]; then
        printf 'отказ назван, а копия всё равно снята: контейнеров после отказа %s\n' "${4:-0}"
        return 1
    fi
    printf 'дверь держит: отказ с причиной «%s», копия цела (контейнеров %s)\n' "$3" "$4"
    return 0
}

память_судить() { # $1 = строки «имя байты», $2 = потолок МБ
    local stroka imya bajty vsego=0 sluzhb=0 bez_limita=""
    while IFS= read -r stroka; do
        [ -n "$stroka" ] || continue
        imya=${stroka%% *}
        bajty=${stroka##* }
        case "$bajty" in
            ''|*[!0-9]*)
                printf 'нечисловой лимит у %s: «%s» — docker ответил не числом\n' "$imya" "$bajty"
                return 1 ;;
        esac
        [ "$bajty" -gt 0 ] || bez_limita="$bez_limita $imya"
        vsego=$(( vsego + bajty ))
        sluzhb=$(( sluzhb + 1 ))
    done <<< "$1"
    if [ "$sluzhb" = 0 ]; then
        printf 'контейнеров среды нет — сумму лимитов считать не по чему\n'
        return 1
    fi
    if [ -n "$bez_limita" ]; then
        printf 'лимит не применён у служб:%s (.HostConfig.Memory = 0) — mem_limit отброшен парсером\n' \
            "$bez_limita"
        return 1
    fi
    local vsego_mb=$(( vsego / 1048576 ))
    if [ "$vsego_mb" -gt "$2" ]; then
        printf 'сумма лимитов %s МБ выше потолка %s МБ (служб %s)\n' "$vsego_mb" "$2" "$sluzhb"
        return 1
    fi
    printf 'сумма лимитов %s МБ при потолке %s МБ (служб %s)\n' "$vsego_mb" "$2" "$sluzhb"
    return 0
}

службы_судить() { # $1 = вывод systemctl is-active …, $2 = сколько ждём ответов
    local vsego plohie
    vsego=$(printf '%s\n' "$1" | awk 'NF' | wc -l)
    plohie=$(printf '%s\n' "$1" | awk 'NF && $0 != "active"' | tr '\n' ' ')
    if [ "$vsego" != "$2" ]; then
        printf 'службы харнеса: ждали %s ответов, получили %s — systemctl назвал не все\n' "$2" "$vsego"
        return 1
    fi
    if [ -n "$plohie" ]; then
        printf 'службы харнеса не active: %s\n' "$plohie"
        return 1
    fi
    printf 'службы харнеса: %s active\n' "$vsego"
    return 0
}

# Чужое = всё, кроме объектов СРЕДЫ (их называет метка compose) и объектов
# харнесных проб (их называют префиксы из данных).
отобрать_чужие() { # $1 = все «тип имя», $2 = объекты среды, $3 = префиксы через пробел
    local vse="$1" sredy="$2" prefiksy="$3" stroka imya prefiks svoj
    while IFS= read -r stroka; do
        [ -n "$stroka" ] || continue
        есть_строка "$sredy" "$stroka" && continue
        imya=${stroka#* }
        svoj=0
        for prefiks in $prefiksy; do
            case "$imya" in "$prefiks"*) svoj=1; break ;; esac
        done
        [ "$svoj" = 1 ] && continue
        printf '%s\n' "$stroka"
    done <<< "$vse" | sort -u
}

# Обе разности НАЗЫВАЮТСЯ отдельно: одно число «совпало/не совпало» скрывает
# причину, а причины тут разные — пропажа нарушает П-3, появление нарушает
# чистоту (находка контракта 28.09.2026).
разность_судить() { # $1 = до, $2 = после, $3 = что среда законно притянула
    local do_spisok posle_spisok zakonno propalo poyavilos n_pr n_po
    do_spisok=$(printf '%s\n' "$1" | awk 'NF' | sort -u)
    posle_spisok=$(printf '%s\n' "$2" | awk 'NF' | sort -u)
    zakonno=$(printf '%s\n' "${3:-}" | awk 'NF' | sort -u)
    propalo=$(comm -23 <(printf '%s\n' "$do_spisok") <(printf '%s\n' "$posle_spisok"))
    poyavilos=$(comm -13 <(printf '%s\n' "$do_spisok") <(printf '%s\n' "$posle_spisok") \
        | grep -vxF -f <(printf '%s\n' "$zakonno"; echo '\0нет-таких') || true)
    n_pr=$(printf '%s\n' "$propalo" | grep -c . || true)
    n_po=$(printf '%s\n' "$poyavilos" | grep -c . || true)
    printf 'пропало %s, появилось вне префикса среды %s\n' "$n_pr" "$n_po"
    [ "$n_pr" = 0 ] || printf 'пропавшие (нарушение П-3): %s\n' "$(printf '%s' "$propalo" | tr '\n' ';')"
    [ "$n_po" = 0 ] || printf 'появившиеся вне среды: %s\n' "$(printf '%s' "$poyavilos" | tr '\n' ';')"
    [ "$n_pr" = 0 ] && [ "$n_po" = 0 ]
}

# ── ДОБЫЧА ДАННЫХ У DOCKER ──────────────────────────────────────────────────
docker_жив() {
    if ! docker ps >/dev/null 2>&1; then
        printf 'docker не отвечает на `docker ps` — проба не может ничего доказать\n'
        return 1
    fi
    return 0
}

снимок_docker() { # $1 = «всё» или «контейнеры»
    local vse sredy
    vse=$(
        docker ps -a --format 'контейнер {{.Names}}' 2>/dev/null
        if [ "${1:-всё}" = "всё" ]; then
            docker volume ls --format 'том {{.Name}}' 2>/dev/null
            docker network ls --format 'сеть {{.Name}}' 2>/dev/null
            docker image ls --format 'образ {{.Repository}}:{{.Tag}}' 2>/dev/null
        fi
    )
    sredy=$(
        docker ps -a --filter "label=$METKA=$PROEKT" --format 'контейнер {{.Names}}' 2>/dev/null
        if [ "${1:-всё}" = "всё" ]; then
            docker volume ls --filter "label=$METKA=$PROEKT" --format 'том {{.Name}}' 2>/dev/null
            docker network ls --filter "label=$METKA=$PROEKT" --format 'сеть {{.Name}}' 2>/dev/null
            docker image ls --filter "label=$METKA=$PROEKT" --format 'образ {{.Repository}}:{{.Tag}}' 2>/dev/null
        fi
    )
    отобрать_чужие "$vse" "$sredy" "$PREFIKSY"
}

# Образы, которые среда ЗАКОННО притянула: на Ф1 сборки в compose нет, и
# postgres:17-* с nginx:alpine появляются как обычные теги без метки проекта
# (находка C-4 спеки). Судятся по тому, чем запущены контейнеры среды.
образы_среды() {
    docker ps -a --filter "label=$METKA=$PROEKT" --format 'образ {{.Image}}' 2>/dev/null | sort -u
}

контейнер_проекта_службы() { # $1 = проект compose, $2 = имя службы
    docker ps -a --filter "label=$METKA=$1" \
        --filter "label=com.docker.compose.service=$2" --format '{{.Names}}' 2>/dev/null | head -1
}

контейнер_службы() { # $1 = имя службы compose (в СРЕДЕ)
    контейнер_проекта_службы "$PROEKT" "$1"
}

# Одноразовый контейнер (`docker compose run --rm` — прогон тестов, накат
# миграций) службой среды НЕ считается: он живёт минуты, а потолок спеки стоит
# на памяти, занятой ПОСТОЯННО. Иначе идущий прогон красил бы пробу, и красным
# был бы не дефект среды, а её нормальная работа.
# Решение смены Р-1 плана первого шага среды: снимаем со счёта по
# метке com.docker.compose.oneoff=True, потолок 1536 МБ не поднимаем. Метку
# ставит сам compose всем одноразовым контейнерам — имён служб прогона пробе
# знать не нужно. Метки нет → контейнер постоянный, счёт прежний.
отсеять_одноразовые() { # $1 = строки «имя байты одноразовость» → «имя байты»
    local name bytes oneoff
    while IFS=' ' read -r name bytes oneoff; do
        [ -n "$name" ] || continue
        if [ "$oneoff" = "True" ]; then
            continue
        fi
        printf '%s %s\n' "$name" "$bytes"
    done <<< "$1"
}

лимиты_среды() {
    local kont
    отсеять_одноразовые "$(
        while IFS= read -r kont; do
            [ -n "$kont" ] || continue
            docker inspect \
                -f '{{.Name}} {{.HostConfig.Memory}} {{index .Config.Labels "com.docker.compose.oneoff"}}' \
                "$kont" 2>/dev/null | sed 's|^/||'
        done < <(docker ps -a --filter "label=$METKA=$PROEKT" --format '{{.Names}}' 2>/dev/null)
    )"
}

# Счёт объектов ЛЮБОГО проекта по метке — им проба доказывает и пустоту после
# снятия копии, и целость живой среды. Метка, а не имя: `--filter name=` берёт
# подстрокой, и счёт по имени проекта захватил бы объекты копии своей пробы.
# Код возврата docker проверяется ОТДЕЛЬНО от пустоты вывода: было
# `docker … 2>/dev/null | grep -c . || true`, и мёртвый демон давал «0» —
# то есть «следов не осталось» (ревью кода 28.09.2026, C-5).
объектов_проекта() { # $1 = имя проекта, $2 = вид → ЧИСЛО; при отказе docker — «не прочитано (причина)» и код 2
    local proekt="$1" vyvod rc
    case "$2" in
        контейнеры) vyvod=$(docker ps -a --filter "label=$METKA=$proekt" --format '{{.Names}}' 2>&1) ;;
        тома)       vyvod=$(docker volume ls --filter "label=$METKA=$proekt" --format '{{.Name}}' 2>&1) ;;
        сети)       vyvod=$(docker network ls --filter "label=$METKA=$proekt" --format '{{.Name}}' 2>&1) ;;
        образы)     vyvod=$(docker image ls --filter "label=$METKA=$proekt" --format '{{.ID}}' 2>&1) ;;
        *)          printf 'не прочитано (вид «%s» не назван)' "$2"; return 2 ;;
    esac
    rc=$?
    if [ "$rc" != 0 ]; then
        printf 'не прочитано (docker код %s: %s)' "$rc" "$(printf '%s' "$vyvod" | tr '\n' ';' | cut -c1-120)"
        return 2
    fi
    printf '%s' "$(printf '%s' "$vyvod" | grep -c . || true)"
    return 0
}

# Имена томов проекта — ими сверяется состав архива с тем, что будет удалено.
тома_проекта() { # $1 = имя проекта → имена через пробел; при отказе docker — «не прочитано» и код 2
    local vyvod rc
    vyvod=$(docker volume ls --filter "label=$METKA=$1" --format '{{.Name}}' 2>&1); rc=$?
    if [ "$rc" != 0 ]; then
        printf 'не прочитано (docker код %s: %s)' "$rc" "$(printf '%s' "$vyvod" | tr '\n' ';' | cut -c1-120)"
        return 2
    fi
    printf '%s' "$(printf '%s' "$vyvod" | tr '\n' ' ')"
    return 0
}

# ── ЖНЕЦ СВОЕГО МУСОРА ──────────────────────────────────────────────────────
# Второй замок поверх фильтра docker: `--filter name=` сравнивает ПОДСТРОКОЙ,
# и однажды перепутанный префикс стоил бы тома среды.
жнец_берёт() { # $1 = имя объекта → 0, если это объект ЭТОЙ пробы
    case "$1" in "$PREFIKS_PROBY"*) return 0 ;; *) return 1 ;; esac
}

убрать_остатки_пробы() {
    local imya kont=0 tom=0 sett=0 kat=0 dvor
    while IFS= read -r imya; do
        [ -n "$imya" ] || continue
        жнец_берёт "$imya" || continue
        docker rm -fv "$imya" >/dev/null 2>&1 && kont=$(( kont + 1 ))  # только префикс $PREFIKS_PROBY
    done < <(docker ps -a --filter "name=$PREFIKS_PROBY" --format '{{.Names}}' 2>/dev/null)
    while IFS= read -r imya; do
        [ -n "$imya" ] || continue
        жнец_берёт "$imya" || continue
        docker volume rm -f "$imya" >/dev/null 2>&1 && tom=$(( tom + 1 ))  # только префикс $PREFIKS_PROBY
    done < <(docker volume ls --filter "name=$PREFIKS_PROBY" --format '{{.Name}}' 2>/dev/null)
    while IFS= read -r imya; do
        [ -n "$imya" ] || continue
        жнец_берёт "$imya" || continue
        docker network rm "$imya" >/dev/null 2>&1 && sett=$(( sett + 1 ))  # только префикс $PREFIKS_PROBY
    done < <(docker network ls --filter "name=$PREFIKS_PROBY" --format '{{.Name}}' 2>/dev/null)
    # Брошенные дворы прошлых прогонов: распакованный архив весит десятки
    # мегабайт, и оборванный прогон оставляет их в /tmp насовсем. Берутся
    # ТОЛЬКО старше VOZRAST_DVORA_MIN — двор живого соседнего прогона не наш.
    while IFS= read -r dvor; do
        [ -n "$dvor" ] || continue
        жнец_берёт "$(basename "$dvor")" || continue
        rm -rf "$dvor" && kat=$(( kat + 1 ))
    done < <(find /tmp -maxdepth 1 -type d -name "$PREFIKS_PROBY*" \
        -mmin "+$VOZRAST_DVORA_MIN" 2>/dev/null)
    printf 'жнец своего мусора (%s): контейнеров убрано %s, томов %s, сетей %s, каталогов %s\n' \
        "$PREFIKS_PROBY" "$kont" "$tom" "$sett" "$kat"
}

# ── ОБЩИЕ ШАГИ РЕЖИМОВ ──────────────────────────────────────────────────────
среда_на_месте() {
    if [ ! -d "$SREDA_KOREN" ]; then
        printf 'среды нет: каталога %s не существует\n' "$SREDA_KOREN"
        return 1
    fi
    if [ ! -f "$SREDA_KOREN/compose.yaml" ]; then
        printf 'среды нет: файла %s/compose.yaml не существует\n' "$SREDA_KOREN"
        return 1
    fi
    return 0
}

поднять_среду() {
    local vyvod rc
    vyvod=$(docker compose -f "$SREDA_KOREN/compose.yaml" up -d 2>&1)
    rc=$?
    [ "$rc" = 0 ] && return 0
    if содержит "$vyvod" "already allocated"; then
        printf 'порт %s занят чужим процессом — подъём отменён, чужого не трогаем:\n%s\n' \
            "$PORT" "$(ss -tlnp "sport = :$PORT" 2>/dev/null)"
    else
        printf 'подъём не удался (код %s):\n%s\n' "$rc" "$vyvod"
    fi
    return 1
}

ждать_готовности() { # $1 = контейнер базы, $2 = предел секунд
    local kont="$1" predel="$2" nachalo status proshlo
    nachalo=$(date +%s)
    while :; do
        status=$(docker inspect \
            -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}НЕТ-HEALTHCHECK{{end}}' \
            "$kont" 2>/dev/null)
        [ "$status" = starting ] || break
        proshlo=$(( $(date +%s) - nachalo ))
        if [ "$proshlo" -ge "$predel" ]; then
            printf 'база не дошла до healthy за %s с, последний статус «%s»\n' "$predel" "$status"
            printf 'последние 20 строк журнала %s:\n' "$kont"
            docker logs --tail 20 "$kont" 2>&1
            return 1
        fi
        sleep 2
    done
    if ! готовность_судить "$status"; then
        printf 'последние 20 строк журнала %s:\n' "$kont"
        docker logs --tail 20 "$kont" 2>&1
        return 1
    fi
    printf 'ожидание готовности: %s с из %s\n' "$(( $(date +%s) - nachalo ))" "$predel"
    return 0
}

# Критерий П-3 требует ОБА снимка в журнал, а снимки только печатались в поток:
# критерий считался выполненным без носителя (ревью кода 28.09.2026, I-16).
# «Записать не смогли» — отказ с причиной, а не молчание: без файла доказательства
# у П-3 нет, и зелёный путь врал бы о нём.
снимки_в_журнал() { # $1 = режим, $2 = снимок ДО, $3 = снимок ПОСЛЕ, $4 = окно, с
    local fajl vyvod
    if ! vyvod=$(mkdir -p "$ZHURNAL_SNIMKOV" 2>&1); then
        printf 'снимки П-3 записать некуда: каталог %s не создан (%s) — у критерия «оба снимка в журнал» нет носителя\n' \
            "$ZHURNAL_SNIMKOV" "$vyvod"
        return 1
    fi
    fajl="$ZHURNAL_SNIMKOV/$(date +%Y%m%d-%H%M%S)-${1#--}.txt"
    {
        printf 'режим: %s\nкогда: %s\nокно между снимками: %s с\nпроект среды: %s\n\n' \
            "$1" "$(date -Is)" "$4" "$PROEKT"
        printf '== СНИМОК ДО ПОДЪЁМА ==\n%s\n\n' "$2"
        printf '== СНИМОК ПОСЛЕ ПОДЪЁМА ==\n%s\n' "$3"
    } > "$fajl" 2>/dev/null || {
        printf 'снимки П-3 в файл %s не записались — у критерия нет носителя\n' "$fajl"
        return 1
    }
    printf 'оба снимка П-3 в журнале: %s (%s строк)\n' "$fajl" "$(wc -l < "$fajl")"
    return 0
}

# Снимки ВОКРУГ подъёма: окно между ними меряется и называется — шире 60 с
# замер недостоверен, в него влезает чужой механизм.
сравнить_вокруг_подъёма() { # $1 = «всё» или «контейнеры»
    local vidy="${1:-всё}" snimok_do snimok_posle t1 t2 shirina zakonno
    t1=$(date +%s)
    snimok_do=$(снимок_docker "$vidy")
    поднять_среду || return 1
    snimok_posle=$(снимок_docker "$vidy")
    t2=$(date +%s)
    zakonno=$(образы_среды)
    shirina=$(( t2 - t1 ))
    printf 'окно снимков: %s с\n' "$shirina"
    снимки_в_журнал "${REZHIM:-снимки}" "$snimok_do" "$snimok_posle" "$shirina" || return 1
    if [ "$shirina" -gt "$SHIRINA_OKNA_S" ]; then
        printf 'окно снимков шире %s с — между снимками мог сработать чужой механизм, замеру верить нельзя\n' \
            "$SHIRINA_OKNA_S"
        return 1
    fi
    разность_судить "$snimok_do" "$snimok_posle" "$zakonno"
}

# Сбор архива ОСТАНАВЛИВАЕТ среду: том нельзя снимать на ходу. Пока по порту
# заглушка, это незаметно; с живым приложением запуск пробы кладёт сайт, и
# согласия на это никто не давал (ревью кода 28.09.2026, I-8). Часы, в которые
# простой запрещён, — ДАННЫЕ (окно_без_остановки); пусто = ограничения нет.
вне_окна_остановки() {
    local seychas
    [ -n "$OSTANOVKA_S" ] && [ -n "$OSTANOVKA_PO" ] || return 0
    seychas=$(date +%H:%M)
    if в_окне "$seychas" "$OSTANOVKA_S" "$OSTANOVKA_PO"; then
        printf 'ЗАМЕР ОТЛОЖЕН: сейчас %s, окно «среду не останавливать» %s–%s\n' \
            "$seychas" "$OSTANOVKA_S" "$OSTANOVKA_PO"
        printf 'сбор архива гасит службы среды, а в этом окне простой запрещён данными %s\n' "$DANNYE"
        return 1
    fi
    return 0
}

вне_ночного_окна() {
    local seychas
    seychas=$(date +%H:%M)
    if в_окне "$seychas" "$OKNO_S" "$OKNO_PO"; then
        printf 'ЗАМЕР ОТЛОЖЕН: сейчас %s, окно ночных механизмов %s–%s\n' "$seychas" "$OKNO_S" "$OKNO_PO"
        printf 'в нём состав docker меняем не мы: 03:00 disk-cleanup, 05:00 проба восстановления, 06:00 проба установки\n'
        return 1
    fi
    return 0
}

# ── РЕЖИМЫ ──────────────────────────────────────────────────────────────────
режим_подъём() {
    прочитать_данные || return 1
    среда_на_месте || return 1
    docker_жив || return 1
    убрать_остатки_пробы
    поднять_среду || return 1
    local kont_db kod telo
    kont_db=$(контейнер_службы db)
    if [ -z "$kont_db" ]; then
        printf 'контейнера службы db с меткой %s=%s нет — подъём прошёл, а базы не появилось\n' "$METKA" "$PROEKT"
        return 1
    fi
    ждать_готовности "$kont_db" "$OZHIDANIE_S" || return 1
    kod=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:$PORT/" 2>/dev/null)
    telo=$(curl -s --max-time 10 "http://127.0.0.1:$PORT/" 2>/dev/null)
    web_судить "$kod" "$telo" "$MARKER" || return 1
    return 0
}

режим_порт() {
    прочитать_данные || return 1
    local vyvod
    vyvod=$(ss -tlnH "sport = :$PORT" 2>/dev/null)
    порт_судить "$vyvod" "$PORT"
}

режим_соседи() {
    прочитать_данные || return 1
    local vyvod_sluzhb
    vyvod_sluzhb=$(systemctl is-active harness-agent harness-dispatcher harness-panel 2>&1 || true)
    службы_судить "$vyvod_sluzhb" 3 || return 1
    вне_ночного_окна || return 2
    среда_на_месте || return 1
    docker_жив || return 1
    сравнить_вокруг_подъёма контейнеры
}

режим_чужое() {
    прочитать_данные || return 1
    вне_ночного_окна || return 2
    среда_на_месте || return 1
    docker_жив || return 1
    убрать_остатки_пробы
    сравнить_вокруг_подъёма всё
}

режим_память() {
    прочитать_данные || return 1
    среда_на_месте || return 1
    docker_жив || return 1
    память_судить "$(лимиты_среды)" "$POTOLOK_MB"
}

# ── П-4: АРХИВ ДАЁТ РАБОЧУЮ КОПИЮ ───────────────────────────────────────────
# Копия поднимается под ДРУГИМ именем проекта и на ДРУГОМ порту: то же имя
# перехватило бы объекты среды, тот же порт — её трафик, и «копия отвечает»
# доказывало бы работу оригинала.

параметры_базы() { # печатает «пользователь база» по compose-файлу среды
    docker compose -f "$SREDA_KOREN/compose.yaml" config --format json 2>/dev/null \
        | python3 -c 'import sys, json
d = json.load(sys.stdin)
e = (d.get("services", {}).get("db", {}).get("environment") or {})
print(e.get("POSTGRES_USER", ""), e.get("POSTGRES_DB", ""))' 2>/dev/null
}

psql_в() { # $1 = контейнер, $2 = пользователь, $3 = база, $4 = запрос
    docker exec "$1" psql -U "$2" -d "$3" -v ON_ERROR_STOP=1 -tAc "$4" 2>&1 | tr -d '\r'
}

свободный_порт() { # печатает номер свободного порта из диапазона копии
    local port
    for (( port = PORT_KOPII_S; port <= PORT_KOPII_PO; port++ )); do
        if порт_свободен_судить "$port" "$(ss -tlnH "sport = :$port" 2>/dev/null)" >/dev/null; then
            printf '%s' "$port"
            return 0
        fi
    done
    return 1
}

копия_compose() { # $1… = аргументы docker compose для КОПИИ
    WEB_PORT="$PORT_KOPII" docker compose -p "$KOPIYA_PROEKT" \
        -f "$DVOR_ARHIVA/распакованное/files/compose.yaml" \
        --env-file "$DVOR_ARHIVA/распакованное/files/.env" "$@"
}

# Снятие копии — и на успехе, и на отказе. Имя проекта проверяется ПЕРЕД
# удалением: `down -v` сносит тома проекта, и ошибись имя — это был бы том
# среды (И-1).
# «Не смогли убрать» называется вслух: молча оставленная таблица неотличима от
# убранной, а лежит она в базе живой среды.
убрать_таблицу_пробы() {
    [ -n "$TABLICA_KONT" ] || return 0
    local vyvod rc
    vyvod=$(psql_в "$TABLICA_KONT" "$TABLICA_USER" "$TABLICA_BAZA" \
        "drop table if exists $TABLICA_PROBY"); rc=$?
    if [ "$rc" = 0 ]; then
        printf 'контрольная таблица убрана из оригинала: %s\n' "$TABLICA_PROBY"
    else
        printf 'ВНИМАНИЕ: контрольная таблица %s ОСТАЛАСЬ в базе среды (%s): %s\n' \
            "$TABLICA_PROBY" "$TABLICA_KONT" "$(printf '%s' "$vyvod" | tr '\n' ';' | cut -c1-200)"
        printf 'убрать рукой: docker exec %s psql -U %s -d %s -c "drop table %s"\n' \
            "$TABLICA_KONT" "$TABLICA_USER" "$TABLICA_BAZA" "$TABLICA_PROBY"
    fi
    TABLICA_KONT=""
    return 0
}

# Один трап на весь режим --архив: сначала своя таблица из базы среды, потом
# копия и двор. Ставится ДО первого следа в среде, а не после сбора архива.
уборка_архива() {
    убрать_таблицу_пробы
    снять_копию
}

снять_копию() {
    local svoj=0 fajl="${DVOR_ARHIVA:-}/распакованное/files/compose.yaml"
    case "${KOPIYA_PROEKT:-}" in
        "$PREFIKS_PROBY"*) svoj=1 ;;
    esac
    if [ "$svoj" = 1 ] && [ -f "$fajl" ]; then
        # Третий замок поверх «-p» и префикса: в compose копии обязано стоять
        # ИМЯ КОПИИ. Прежде замком была только ПЕРЕМЕННАЯ KOPIYA_PROEKT, а в
        # файле оставалось имя живого проекта — не сработай `-p`, `down -v` унёс бы
        # том живой среды (ревью кода 28.09.2026, C-6).
        if grep -qxF "name: $KOPIYA_PROEKT" "$fajl"; then
            копия_compose down -v --remove-orphans >/dev/null 2>&1  # только префикс $PREFIKS_PROBY
        else
            printf 'копия НЕ снята через down -v: в %s нет строки «name: %s» — том живой среды в одном шаге отсюда. Остатки уберёт жнец по префиксу %s\n' \
                "$fajl" "$KOPIYA_PROEKT" "$PREFIKS_PROBY"
        fi
    elif [ -n "${KOPIYA_PROEKT:-}" ] && [ "$svoj" = 0 ]; then
        printf 'копия НЕ снята: имя проекта «%s» не начинается с префикса пробы %s\n' \
            "$KOPIYA_PROEKT" "$PREFIKS_PROBY"
        return 1
    fi
    [ -n "${DVOR_ARHIVA:-}" ] && rm -rf "$DVOR_ARHIVA"
    DVOR_ARHIVA=""
    # Остатки, до которых compose не дотянулся (оборванный подъём): по префиксу.
    убрать_остатки_пробы
    return 0
}

# Режим разобран по ШАГАМ: одна функция на 230 строк не умещалась в голове, а
# каждый шаг здесь — отдельный предмет с отдельной причиной отказа (ревью кода
# 28.09.2026, I-15). Шаги общаются переменными ФАЙЛА: того же приёма требует трап,
# которому local уже не виден. Порядок шагов и есть смысл режима, поэтому он
# читается в одном месте — в режим_архив.
ARHIV_SBORSHCHIK=""   # путь сборщика архива (scripts/export.sh среды)
ARHIV_PUT=""          # путь собранного архива
ARHIV_TELO_ORIG=""    # ответ оригинала до сбора: с ним сверяется копия
ARHIV_MARKA=""        # марка контрольной таблицы этого прогона
ARHIV_STROK=""        # число строк контрольной таблицы в оригинале
ARHIV_TOM_STARYJ=""   # имя тома в архиве
ARHIV_KONT_KOPII=""   # контейнер базы КОПИИ
ARHIV_TOM_NOVYJ=""    # имя тома копии
ARHIV_KONT_DB=""      # контейнер базы ОРИГИНАЛА
ARHIV_USER=""
ARHIV_BAZA=""

архив_поднять_и_спросить_базу() {
    # Путь сборщика — переменная ФАЙЛА: его проверяет этот шаг, а зовёт следующий.
    ARHIV_SBORSHCHIK="$SREDA_KOREN/scripts/export.sh"
    if [ ! -f "$ARHIV_SBORSHCHIK" ]; then
        printf 'сборщика архива нет: %s — переносить нечем\n' "$ARHIV_SBORSHCHIK"
        return 1
    fi

    поднять_среду || return 1
    local params
    ARHIV_KONT_DB=$(контейнер_службы db)
    if [ -z "$ARHIV_KONT_DB" ]; then
        printf 'контейнера службы db с меткой %s=%s нет — контрольную таблицу заводить негде\n' "$METKA" "$PROEKT"
        return 1
    fi
    ждать_готовности "$ARHIV_KONT_DB" "$OZHIDANIE_S" || return 1
    params=$(параметры_базы)
    ARHIV_USER=${params%% *}
    ARHIV_BAZA=${params##* }
    if [ -z "$ARHIV_USER" ] || [ -z "$ARHIV_BAZA" ]; then
        printf 'compose не назвал POSTGRES_USER/POSTGRES_DB службы db — к базе не подключиться\n'
        return 1
    fi
    return 0
}

архив_контрольная_таблица() {
    # ── Контрольная таблица в ОРИГИНАЛЕ ─────────────────────────────────────
    # Адрес таблицы запоминается ДО её создания: оборвись create на середине,
    # убирать было бы нечего по чему (ревью кода 28.09.2026, I-7).
    local otvet marka_orig
    TABLICA_KONT="$ARHIV_KONT_DB"
    TABLICA_USER="$ARHIV_USER"
    TABLICA_BAZA="$ARHIV_BAZA"
    ARHIV_MARKA="perenos-$(date +%s)-$$"
    otvet=$(psql_в "$ARHIV_KONT_DB" "$ARHIV_USER" "$ARHIV_BAZA" \
        "drop table if exists $TABLICA_PROBY;
         create table $TABLICA_PROBY (n int, marka text);
         insert into $TABLICA_PROBY select generate_series(1,$STROK_V_TABLICE), '$ARHIV_MARKA';")
    if [ "$?" != 0 ]; then
        printf 'контрольная таблица в оригинале не завелась: %s\n' "$(printf '%s' "$otvet" | tr '\n' ';')"
        return 1
    fi
    ARHIV_STROK=$(psql_в "$ARHIV_KONT_DB" "$ARHIV_USER" "$ARHIV_BAZA" "select count(*) from $TABLICA_PROBY")
    marka_orig=$(psql_в "$ARHIV_KONT_DB" "$ARHIV_USER" "$ARHIV_BAZA" "select distinct marka from $TABLICA_PROBY")
    if [ "$ARHIV_STROK" != "$STROK_V_TABLICE" ] || [ "$marka_orig" != "$ARHIV_MARKA" ]; then
        printf 'оригинал не подтвердил свою же таблицу: строк «%s» (ждали %s), марка «%s» (ждали «%s»)\n' \
            "$ARHIV_STROK" "$STROK_V_TABLICE" "$marka_orig" "$ARHIV_MARKA"
        return 1
    fi
    printf 'контрольная таблица %s в оригинале: %s строк, марка «%s»\n' \
        "$TABLICA_PROBY" "$ARHIV_STROK" "$ARHIV_MARKA"

    local kod_orig
    kod_orig=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:$PORT/" 2>/dev/null)
    ARHIV_TELO_ORIG=$(curl -s --max-time 10 "http://127.0.0.1:$PORT/" 2>/dev/null)
    web_судить "$kod_orig" "$ARHIV_TELO_ORIG" "$MARKER" || return 1
    return 0
}

архив_собрать_и_судить() {
    # ── Сбор архива в СВОЙ двор ─────────────────────────────────────────────
    # Каталог свой, а не боевой: архивы владельца пробе не принадлежат, и
    # убирать за собой в чужом каталоге значит трогать его копии (И-1).
    DVOR_ARHIVA=$(mktemp -d "/tmp/${PREFIKS_PROBY}arhiv-XXXXXX") || {
        printf 'временный каталог пробы не создан\n'; return 1; }
    KOPIYA_PROEKT="${PREFIKS_PROBY}kopiya-$$"

    local vyvod_sbora
    vyvod_sbora=$(bash "$ARHIV_SBORSHCHIK" --каталог "$DVOR_ARHIVA/архивы" 2>&1)
    if [ "$?" != 0 ]; then
        printf 'сбор архива не удался:\n%s\n' "$vyvod_sbora"
        return 1
    fi
    ARHIV_PUT=$(printf '%s\n' "$vyvod_sbora" | sed -n 's/^архив:[[:space:]]*//p' | head -1)
    if [ -z "$ARHIV_PUT" ]; then
        printf 'сборщик не назвал путь архива строкой «архив:»:\n%s\n' "$vyvod_sbora"
        return 1
    fi
    # Простой ЖИВОЙ среды — цена этого режима, и она обязана быть названа числом.
    # Требуется от сборщика, а не меряется здесь: остановку делает он, и «сколько
    # лежало» знает только он (ревью кода 28.09.2026, I-8).
    local prostoj
    prostoj=$(printf '%s\n' "$vyvod_sbora" \
        | sed -n 's/^простой среды:[[:space:]]*\([0-9][0-9]*\)[[:space:]]*с.*/\1/p' | head -1)
    if [ -z "$prostoj" ]; then
        printf 'сборщик не назвал простой среды строкой «простой среды: N с» — длительность остановки живой среды неизвестна, а она и есть цена сбора:\n%s\n' \
            "$vyvod_sbora"
        return 1
    fi
    printf 'простой ЖИВОЙ среды на время сбора: %s с (сбор гасит службы, том нельзя снимать на ходу)\n' "$prostoj"
    printf 'собран архив: %s (%s байт)\n' "$ARHIV_PUT" "$(stat -c %s "$ARHIV_PUT" 2>/dev/null)"
    вне_среды_судить "$ARHIV_PUT" "$SREDA_KOREN" || return 1
    сумма_судить "$ARHIV_PUT" || return 1
    local toma_sredy sostav_arhiva
    toma_sredy=$(тома_проекта "$PROEKT")
    if [ "$?" != 0 ]; then
        printf 'тома среды %s не перечислены: %s — состав архива сверять не с чем\n' "$PROEKT" "$toma_sredy"
        return 1
    fi
    sostav_arhiva=$(состав_архива_снятием "$SREDA_KOREN/scripts/teardown.sh" "$ARHIV_PUT" \
        "$PROEKT" "$toma_sredy" "$SREDA_KOREN" "")
    if [ "$?" != 0 ]; then
        printf 'состав архива не сходится: %s\n' "$sostav_arhiva"
        return 1
    fi
    printf 'состав архива судьёй снятия: %s\n' "$sostav_arhiva"

    # Оригинал обязан ЖИТЬ после сбора: сборщик останавливал его контейнеры.
    local kod_posle
    kod_posle=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:$PORT/" 2>/dev/null)
    if [ "$kod_posle" != 200 ]; then
        printf 'после сбора оригинал отвечает «%s», а не 200 — сборщик не поднял среду обратно\n' "${kod_posle:-нет ответа}"
        return 1
    fi
    printf 'оригинал после сбора жив: код 200\n'
    return 0
}

архив_развернуть_копию() {
    # ── Разворачивание копии ────────────────────────────────────────────────
    local raspak="$DVOR_ARHIVA/распакованное"
    mkdir -p "$raspak"
    if ! zstd -dc "$ARHIV_PUT" 2>/dev/null | tar -C "$raspak" -xf - 2>&1; then
        printf 'архив не распаковался: %s\n' "$ARHIV_PUT"
        return 1
    fi
    # Имя проекта в распакованном compose.yaml — имя ЖИВОГО проекта (точная
    # копия боевого). Подменяется и ДОКАЗЫВАЕТСЯ грепом, как в завести_копию_среды:
    # иначе от живой среды копию отделял бы один флаг `-p`, а путь отказа вёл
    # бы к `down -v` по её проекту (ревью кода 28.09.2026, C-6).
    if [ ! -f "$raspak/files/compose.yaml" ]; then
        printf 'в распакованном архиве нет files/compose.yaml — поднимать копию нечем\n'
        return 1
    fi
    sed -i "s|^name:.*|name: $KOPIYA_PROEKT|" "$raspak/files/compose.yaml"
    if ! grep -qxF "name: $KOPIYA_PROEKT" "$raspak/files/compose.yaml"; then
        printf 'в compose распакованной копии имя проекта НЕ подменилось на %s — «down -v» ушёл бы по проекту живой среды\n' \
            "$KOPIYA_PROEKT"
        return 1
    fi
    printf 'в compose копии имя проекта подменено на %s\n' "$KOPIYA_PROEKT"

    local
    ARHIV_TOM_STARYJ=$(sed -n 's/^volume=//p' "$raspak/manifest.txt" 2>/dev/null | head -1)
    if [ -z "$ARHIV_TOM_STARYJ" ] || [ ! -f "$raspak/volumes/$ARHIV_TOM_STARYJ.tar" ]; then
        printf 'в архиве нет тома базы (manifest назвал «%s») — копия поднялась бы на пустой базе\n' \
            "${ARHIV_TOM_STARYJ:-ничего}"
        return 1
    fi
    ARHIV_TOM_NOVYJ="${KOPIYA_PROEKT}_${ARHIV_TOM_STARYJ#${PROEKT}_}"

    PORT_KOPII=$(свободный_порт)
    if [ -z "$PORT_KOPII" ]; then
        # Называются ДЕРЖАТЕЛИ портов диапазона, а не все слушатели машины:
        # «port is already allocated» без имени процесса стоит смены разбора.
        printf 'свободного порта для копии в диапазоне %s–%s нет, держат:\n%s\n' \
            "$PORT_KOPII_S" "$PORT_KOPII_PO" \
            "$(ss -tlnpH "sport >= :$PORT_KOPII_S and sport <= :$PORT_KOPII_PO" 2>/dev/null)"
        return 1
    fi
    printf 'копия: проект %s, порт %s, том %s\n' "$KOPIYA_PROEKT" "$PORT_KOPII" "$ARHIV_TOM_NOVYJ"

    local vyvod_create
    vyvod_create=$(копия_compose create 2>&1)
    if [ "$?" != 0 ]; then
        if содержит "$vyvod_create" "already allocated"; then
            printf 'порт %s копии занял кто-то между проверкой и подъёмом, держит:\n%s\n' \
                "$PORT_KOPII" "$(ss -tlnp "sport = :$PORT_KOPII" 2>/dev/null)"
        else
            printf 'контейнеры копии не создались:\n%s\n' "$vyvod_create"
        fi
        return 1
    fi
    # ФАКТ имени проекта спрашивается у compose, а не предполагается по флагу:
    # `-p` мог не сработать (другая версия compose, COMPOSE_PROJECT_NAME в
    # окружении, ошибка разбора), и тогда create присоединился бы к проекту
    # живой среды. Отказ здесь — ДО заливки тома (ревью кода 28.09.2026, C-6).
    local imya_fakt
    imya_fakt=$(копия_compose config --format json 2>/dev/null \
        | python3 -c 'import sys, json; print(json.load(sys.stdin).get("name", ""))' 2>/dev/null)
    имя_проекта_судить "$KOPIYA_PROEKT" "$imya_fakt" || return 1

    if ! docker volume inspect "$ARHIV_TOM_NOVYJ" >/dev/null 2>&1; then
        printf 'том копии %s не создан compose — заливать данные некуда\n' "$ARHIV_TOM_NOVYJ"
        return 1
    fi
    # Данные заливаются МЕЖДУ созданием и стартом: postgres разворачивает пустой
    # каталог только при первом СТАРТЕ, и залитый после старта том остался бы
    # неиспользованным.
    if ! docker run --rm -i -v "$ARHIV_TOM_NOVYJ":/v alpine:3.20 tar -C /v -xf - \
            < "$raspak/volumes/$ARHIV_TOM_STARYJ.tar" >/dev/null 2>&1; then
        printf 'том %s не залился в %s\n' "$ARHIV_TOM_STARYJ" "$ARHIV_TOM_NOVYJ"
        return 1
    fi

    local vyvod_start
    vyvod_start=$(копия_compose start 2>&1)
    if [ "$?" != 0 ]; then
        if содержит "$vyvod_start" "already allocated"; then
            printf 'порт %s копии занят, держит:\n%s\n' \
                "$PORT_KOPII" "$(ss -tlnp "sport = :$PORT_KOPII" 2>/dev/null)"
        else
            printf 'копия не поднялась:\n%s\n' "$vyvod_start"
        fi
        return 1
    fi
    ARHIV_KONT_KOPII=$(контейнер_проекта_службы "$KOPIYA_PROEKT" db)
    if [ -z "$ARHIV_KONT_KOPII" ]; then
        printf 'контейнера db у копии %s нет\n' "$KOPIYA_PROEKT"
        return 1
    fi
    ждать_готовности "$ARHIV_KONT_KOPII" "$OZHIDANIE_S" || return 1
    return 0
}

архив_сверить_копию() {
    # ── Копия отвечает тем же и держит те же данные ─────────────────────────
    local kod_kopii telo_kopii
    kod_kopii=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:$PORT_KOPII/" 2>/dev/null)
    telo_kopii=$(curl -s --max-time 10 "http://127.0.0.1:$PORT_KOPII/" 2>/dev/null)
    копия_судить "$kod_kopii" "$telo_kopii" "$ARHIV_TELO_ORIG" "$MARKER" || return 1

    local strok_kopii marka_kopii
    strok_kopii=$(psql_в "$ARHIV_KONT_KOPII" "$ARHIV_USER" "$ARHIV_BAZA" "select count(*) from $TABLICA_PROBY")
    marka_kopii=$(psql_в "$ARHIV_KONT_KOPII" "$ARHIV_USER" "$ARHIV_BAZA" "select distinct marka from $TABLICA_PROBY")
    таблица_судить "$ARHIV_STROK" "$strok_kopii" "$ARHIV_MARKA" "$marka_kopii" || return 1
    # Контрольную таблицу из базы среды убирает ТРАП (убрать_таблицу_пробы):
    # здесь строки уборки нет нарочно — она работала только на удачном пути.
    return 0
    return 0
}

режим_архив() {
    прочитать_данные || return 1
    # Окно «не останавливать» спрашивается ДО всего остального — как ночное окно
    # в других режимах: отказ от замера не зависит от того, цела ли среда.
    вне_окна_остановки || return 2
    среда_на_месте || return 1
    docker_жив || return 1
    убрать_остатки_пробы
    # Трап стоит ДО первого следа в среде: он убирает и контрольную таблицу из
    # базы среды, и копию с двором — на любом из отказов ниже, а не только на
    # удачном пути (ревью кода 28.09.2026, I-7).
    trap 'уборка_архива' EXIT
    архив_поднять_и_спросить_базу || return 1
    архив_контрольная_таблица || return 1
    архив_собрать_и_судить || return 1
    архив_развернуть_копию || return 1
    архив_сверить_копию || return 1
    # Контрольную таблицу из базы среды убирает ТРАП (убрать_таблицу_пробы):
    # строки уборки здесь нет нарочно — она работала только на удачном пути.
    return 0
}

# ── П-5, П-6: СНЯТИЕ ДОКАЗЫВАЕТСЯ НА КОПИИ ──────────────────────────────────
# Живая среда не снимается ни при каком исходе: проба поднимает СВОЮ копию —
# свой каталог во дворе пробы, своё имя проекта с префиксом, свой порт — и
# снимает её. Доказательством считается пересчёт объектов ПРОБОЙ, а не слова
# снимающего о себе.

убрать_копию_снятия() {
    local imya
    if [ -n "$SNYATIE_PROEKT" ] && [ -n "$DVOR_SNYATIYA" ] \
        && копия_снятия_своя "$SNYATIE_PROEKT" "$DVOR_SNYATIYA/$SNYATIE_PROEKT" >/dev/null; then
        # Снятие могло не дойти до конца (отказ в середине, обрыв прогона):
        # остатки копии добираются по МЕТКЕ, и только после проверки префикса.
        while IFS= read -r imya; do
            [ -n "$imya" ] || continue
            docker rm -fv "$imya" >/dev/null 2>&1
        done < <(docker ps -a --filter "label=$METKA=$SNYATIE_PROEKT" --format '{{.Names}}' 2>/dev/null)
        while IFS= read -r imya; do
            [ -n "$imya" ] || continue
            docker network rm "$imya" >/dev/null 2>&1  # только префикс $PREFIKS_PROBY, проверен копия_снятия_своя
        done < <(docker network ls --filter "label=$METKA=$SNYATIE_PROEKT" --format '{{.Name}}' 2>/dev/null)
        while IFS= read -r imya; do
            [ -n "$imya" ] || continue
            docker volume rm "$imya" >/dev/null 2>&1  # только префикс $PREFIKS_PROBY, проверен копия_снятия_своя
        done < <(docker volume ls --filter "label=$METKA=$SNYATIE_PROEKT" --format '{{.Name}}' 2>/dev/null)
    fi
    [ -n "$DVOR_SNYATIYA" ] && rm -rf "$DVOR_SNYATIYA"
    DVOR_SNYATIYA=""
    SNYATIE_PROEKT=""
    убрать_остатки_пробы
    return 0
}

# Копия — настоящая среда, а не пустой каталог: тот же compose, свои имя, порт
# и каталог. Подмена ПРОВЕРЯЕТСЯ: не сработавший sed поднял бы объекты живой
# среды под её же меткой, и снятие било бы по ней (И-1).
завести_копию_среды() { # $1 = каталог копии, $2 = имя проекта, $3 = порт
    local koren="$1" proekt="$2" port="$3"
    if ! cp -a "$SREDA_KOREN" "$koren" 2>/dev/null; then
        printf 'копия каталога среды не сделалась: %s → %s\n' "$SREDA_KOREN" "$koren"
        return 1
    fi
    sed -i "s|^name:.*|name: $proekt|" "$koren/compose.yaml"
    sed -i "s|^WEB_PORT=.*|WEB_PORT=$port|" "$koren/.env"
    if ! grep -qx "name: $proekt" "$koren/compose.yaml"; then
        printf 'в compose копии имя проекта НЕ подменилось на %s — подъём взял бы объекты живой среды\n' "$proekt"
        return 1
    fi
    if ! grep -qx "WEB_PORT=$port" "$koren/.env"; then
        printf 'в .env копии порт НЕ подменился на %s — копия встала бы на порт живой среды\n' "$port"
        return 1
    fi
    printf 'копия среды заведена: %s (проект %s, порт %s)\n' "$koren" "$proekt" "$port"
    return 0
}

# Имя файла nginx спрашивается у СНЯТИЯ — оно читает его из данных машины
# (ключ ENV_NGINX_SITE в harness.conf). Прежде имя складывалось из имени проекта
# в снятии и такой же строкой в стенде пробы: проверялась договорённость
# проверяющего с самим собой, и писатель, назвавший файл по ДОМЕНУ, оставил бы
# его на диске при зелёной пробе (ревью кода 28.09.2026, I-12). Писателя на
# машине пока нет; когда появится, имя он обязан брать из того же ключа.
имя_файла_nginx() { # $1 = teardown.sh, $2 = имя проекта
    TEARDOWN_TOLKO_FUNKCII=1 bash -c \
        'source "$1" "$2" >/dev/null 2>&1; printf "%s" "${NGINX_FAJL:-}"' _ "$1" "$2"
}

# Строка журнала диспетчера для стендов: та же форма, что пишет tg-dispatcher
# (ts, message_id, chat_id, text). Двери читают ТОЛЬКО эту форму — стенд обязан
# звать их так же, как боевой путь (ревью кода 28.09.2026, C-1).
слово_в_журнал() { # $1 = файл журнала, $2 = текст, $3 = номер сообщения
    python3 -c 'import datetime, json, sys
print(json.dumps({"ts": datetime.datetime.now().astimezone().isoformat(),
                  "message_id": sys.argv[2], "chat_id": "1000000001",
                  "text": sys.argv[1]}, ensure_ascii=False))' "$2" "${3:-900}" >> "$1"
}

# Режим разобран по ШАГАМ (ревью кода 28.09.2026, I-15): 240 строк в одной
# функции в голову не умещались, а шагов здесь ровно пять и у каждого своя
# причина отказа. Общее состояние — переменные ФАЙЛА: их же убирает трап.
SNYATIE_TELO_DO=""     # ответ живой среды до прогона
SNYATIE_SOSEDI_DO=""   # снимок чужих контейнеров до прогона
SNYATIE_KOREN_KOPII="" # каталог копии
SNYATIE_STEND_LOG=""   # подставной каталог журнала входящих
SNYATIE_STEND_ETC=""   # подставной /etc: nginx и letsencrypt
SNYATIE_NGINX_FAJL=""  # имя файла nginx, названное САМИМ снятием

снятие_живая_среда_до() {
    # ── Живая среда ДО прогона: с этим же состоянием сверяемся в конце (П-6) ─
    поднять_среду || return 1
    local kont_db kod_do
    kont_db=$(контейнер_службы db)
    if [ -z "$kont_db" ]; then
        printf 'контейнера службы db с меткой %s=%s нет — живой среды для сверки нет\n' "$METKA" "$PROEKT"
        return 1
    fi
    ждать_готовности "$kont_db" "$OZHIDANIE_S" || return 1
    kod_do=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:$PORT/" 2>/dev/null)
    SNYATIE_TELO_DO=$(curl -s --max-time 10 "http://127.0.0.1:$PORT/" 2>/dev/null)
    web_судить "$kod_do" "$SNYATIE_TELO_DO" "$MARKER" || return 1
    SNYATIE_SOSEDI_DO=$(снимок_docker контейнеры)
    return 0
}

снятие_поднять_копию() {
    # ── Копия ───────────────────────────────────────────────────────────────
    DVOR_SNYATIYA=$(mktemp -d "/tmp/${PREFIKS_PROBY}snyatie-XXXXXX") || {
        printf 'двор для копии не создан\n'; return 1; }
    SNYATIE_PROEKT="${PREFIKS_PROBY}snyatie-$$"
    trap 'убрать_копию_снятия' EXIT
    SNYATIE_KOREN_KOPII="$DVOR_SNYATIYA/$SNYATIE_PROEKT"
    копия_снятия_своя "$SNYATIE_PROEKT" "$SNYATIE_KOREN_KOPII" || return 1

    local port_kopii
    port_kopii=$(свободный_порт)
    if [ -z "$port_kopii" ]; then
        printf 'свободного порта для копии в диапазоне %s–%s нет, держат:\n%s\n' \
            "$PORT_KOPII_S" "$PORT_KOPII_PO" \
            "$(ss -tlnpH "sport >= :$PORT_KOPII_S and sport <= :$PORT_KOPII_PO" 2>/dev/null)"
        return 1
    fi
    завести_копию_среды "$SNYATIE_KOREN_KOPII" "$SNYATIE_PROEKT" "$port_kopii" || return 1

    local vyvod
    vyvod=$(docker compose -f "$SNYATIE_KOREN_KOPII/compose.yaml" up -d 2>&1)
    if [ "$?" != 0 ]; then
        printf 'копия не поднялась:\n%s\n' "$vyvod"
        return 1
    fi
    local kont_kopii
    kont_kopii=$(контейнер_проекта_службы "$SNYATIE_PROEKT" db)
    if [ -z "$kont_kopii" ]; then
        printf 'контейнера db у копии %s нет — снимать было бы нечего\n' "$SNYATIE_PROEKT"
        return 1
    fi
    ждать_готовности "$kont_kopii" "$OZHIDANIE_S" || return 1

    # Условие проверки создаётся ПРОБОЙ: пустой счёт до снятия сделал бы
    # «после снятия ноль» правдой без всякого снятия.
    local kont_do tom_do sett_do
    kont_do=$(объектов_проекта "$SNYATIE_PROEKT" контейнеры)
    tom_do=$(объектов_проекта "$SNYATIE_PROEKT" тома)
    sett_do=$(объектов_проекта "$SNYATIE_PROEKT" сети)
    printf 'копия поднята: контейнеров %s, томов %s, сетей %s\n' "$kont_do" "$tom_do" "$sett_do"
    счёт_судить "контейнеры копии" "$kont_do" || return 1
    счёт_судить "тома копии" "$tom_do" || return 1
    счёт_судить "сети копии" "$sett_do" || return 1
    if [ "$kont_do" = 0 ] || [ "$tom_do" = 0 ] || [ "$sett_do" = 0 ]; then
        printf 'у копии нет своих объектов — «после снятия ноль» доказывало бы только пустоту стенда\n'
        return 1
    fi

    # ── Архив копии: условие 2 двери, собирается СБОРЩИКОМ копии ────────────
    # Каталог архива ни сборщику, ни снятию не называется нарочно: так
    # проверяется, что дефолт у них ОДИН. Разойдись он — дверь не нашла бы
    # собранный архив и снятие стало бы невозможным на ровном месте.
    local snyatie_skript="$SNYATIE_KOREN_KOPII/scripts/teardown.sh" vyvod_sbora arhiv
    if [ ! -f "$snyatie_skript" ]; then
        printf 'снятия нет: %s — среда уехала бы без инструмента снятия\n' "$snyatie_skript"
        return 1
    fi
    vyvod_sbora=$(bash "$SNYATIE_KOREN_KOPII/scripts/export.sh" 2>&1)
    if [ "$?" != 0 ]; then
        printf 'архив копии не собрался:\n%s\n' "$vyvod_sbora"
        return 1
    fi
    arhiv=$(printf '%s\n' "$vyvod_sbora" | sed -n 's/^архив:[[:space:]]*//p' | head -1)
    if [ -z "$arhiv" ]; then
        printf 'сборщик не назвал путь архива строкой «архив:»:\n%s\n' "$vyvod_sbora"
        return 1
    fi
    вне_среды_судить "$arhiv" "$SNYATIE_KOREN_KOPII" || return 1
    сумма_судить "$arhiv" || return 1
    return 0
}

снятие_стенды_двери() {
    # ── Стенд слова владельца: свой каталог журналов, боевой не трогаем ─────
    # Слово подаётся ЖУРНАЛОМ ДИСПЕТЧЕРА (inbox.jsonl с message_id), а не
    # файлами inbox/*.txt: после ревью 28.09 (C-1) двери читают только журнал,
    # и стенд обязан звать дверь тем же путём, что боевой вызов. Подмена
    # каталога разрешена лишь ключом стенда (C-2) — боевой путь его не ставит.
    SNYATIE_STEND_LOG="$DVOR_SNYATIYA/log"
    mkdir -p "$SNYATIE_STEND_LOG"
    : > "$SNYATIE_STEND_LOG/inbox.jsonl"

    # ── Стенд носителей БЕЗ префикса: /etc тоже сужается ────────────────────
    # Живой режим сужал только каталог журнала, а nginx и letsencrypt оставлял
    # БОЕВЫМИ: `rm -rf /etc/nginx/sites-enabled/<префикс пробы>snyatie-<pid>`
    # выполнялся по настоящим путям, и спасало лишь то, что таких имён нет
    # (ревью кода 28.09.2026, I-10, запись [[narrowing-missed-shared-carriers]]).
    # Приём был известен — самопроверка эти три ручки подменяла, — и просто не
    # применён там, где риск настоящий. Заодно носители здесь ЗАВОДЯТСЯ, и
    # ветка их снятия проверяется на живом прогоне, а не только на стенде.
    SNYATIE_STEND_ETC="$DVOR_SNYATIYA/etc"
    mkdir -p "$SNYATIE_STEND_ETC/nginx/sites-available" "$SNYATIE_STEND_ETC/nginx/sites-enabled" \
             "$SNYATIE_STEND_ETC/letsencrypt/live/$SNYATIE_PROEKT" \
             "$SNYATIE_STEND_ETC/letsencrypt/archive/$SNYATIE_PROEKT" \
             "$SNYATIE_STEND_ETC/letsencrypt/renewal"
    SNYATIE_NGINX_FAJL=$(имя_файла_nginx "$SNYATIE_KOREN_KOPII/scripts/teardown.sh" "$SNYATIE_PROEKT")
    if [ -z "$SNYATIE_NGINX_FAJL" ]; then
        printf 'снятие не назвало имя файла nginx (NGINX_FAJL) — стенд носителей завести нечем\n'
        return 1
    fi
    printf 'имя файла nginx взято у снятия (данные ENV_NGINX_SITE): %s\n' "$SNYATIE_NGINX_FAJL"
    : > "$SNYATIE_STEND_ETC/nginx/sites-available/$SNYATIE_NGINX_FAJL"
    ln -sf "$SNYATIE_STEND_ETC/nginx/sites-available/$SNYATIE_NGINX_FAJL" \
           "$SNYATIE_STEND_ETC/nginx/sites-enabled/$SNYATIE_NGINX_FAJL"
    : > "$SNYATIE_STEND_ETC/letsencrypt/archive/$SNYATIE_PROEKT/privkey1.pem"
    : > "$SNYATIE_STEND_ETC/letsencrypt/renewal/$SNYATIE_PROEKT.conf"

    return 0
}

# Снятие зовётся ОДНИМ способом: те же ручки стенда у всех вызовов. Функция
# объявлена НА УРОВНЕ ФАЙЛА, а не внутри шага: её зовут два шага, и вложенное
# определение было бы видно только одному.
снятие_копии() {
    HARNESS_OWNER_WORD_STEND=1 \
    HARNESS_LOG_DIR="$SNYATIE_STEND_LOG" \
    NGINX_AVAILABLE_DIR="$SNYATIE_STEND_ETC/nginx/sites-available" \
    NGINX_ENABLED_DIR="$SNYATIE_STEND_ETC/nginx/sites-enabled" \
    LETSENCRYPT_DIR="$SNYATIE_STEND_ETC/letsencrypt" \
        bash "$SNYATIE_KOREN_KOPII/scripts/teardown.sh" "$SNYATIE_PROEKT" \
            --каталог "$SNYATIE_KOREN_KOPII" 2>&1
}

снятие_двери_и_снятие() {
    # ── БОЛЬНОЙ СЛУЧАЙ ВЖИВУЮ: снятие без слова владельца ───────────────────
    local kod_otkaza vyvod_otkaza kont_posle_otkaza
    vyvod_otkaza=$(снятие_копии)
    kod_otkaza=$?
    kont_posle_otkaza=$(объектов_проекта "$SNYATIE_PROEKT" контейнеры)
    отказ_двери_судить "$kod_otkaza" "$vyvod_otkaza" "условие 1 из трёх" "$kont_posle_otkaza" || return 1

    # ── БОЛЬНЫЕ СЛУЧАИ: слово владельца ЕСТЬ, но оно НЕ про снятие ──────────
    # Между «журнала нет» и «текст, который проба сама написала» не было ни
    # одного пути «сообщение есть, но оно про другое» — а это и есть
    # единственное место, где распознаватель может ошибиться: на живом корпусе
    # он уже ошибся раз из 1197 (ревью кода 28.09.2026, I-11).
    local pohozhee kod_pohozhego vyvod_pohozhego nomer=1100
    while IFS= read -r pohozhee; do
        [ -n "$pohozhee" ] || continue
        : > "$SNYATIE_STEND_LOG/inbox.jsonl"
        nomer=$(( nomer + 1 ))
        слово_в_журнал "$SNYATIE_STEND_LOG/inbox.jsonl" "$pohozhee" "$nomer"
        vyvod_pohozhego=$(снятие_копии)
        kod_pohozhego=$?
        if ! отказ_двери_судить "$kod_pohozhego" "$vyvod_pohozhego" "условие 1 из трёх" \
            "$(объектов_проекта "$SNYATIE_PROEKT" контейнеры)" >/dev/null; then
            printf 'БОЛЬНОЙ СЛУЧАЙ НЕ ОТРАБОТАЛ: сообщение владельца «%s» не про снятие, а дверь его приняла (код %s):\n%s\n' \
                "$pohozhee" "$kod_pohozhego" "$vyvod_pohozhego"
            return 1
        fi
        printf 'дверь не приняла за разрешение сообщение «%s»\n' "$pohozhee"
    done <<SLOVA
пришли снимок среды
Лимит снят
убери из среды лишние контейнеры
SLOVA

    # ── Слово владельца на стенде и настоящее снятие ────────────────────────
    : > "$SNYATIE_STEND_LOG/inbox.jsonl"
    слово_в_журнал "$SNYATIE_STEND_LOG/inbox.jsonl" "Снимай среду $SNYATIE_PROEKT с диска" 1000
    слово_в_журнал "$SNYATIE_STEND_LOG/inbox.jsonl" \
        "Среда $SNYATIE_PROEKT поднялась на новом сервере, всё работает" 1001
    local vyvod_snyatiya kod_snyatiya
    vyvod_snyatiya=$(снятие_копии)
    kod_snyatiya=$?
    printf '%s\n' "$vyvod_snyatiya"
    if [ "$kod_snyatiya" != 0 ]; then
        printf 'снятие копии не прошло при всех трёх условиях (код %s)\n' "$kod_snyatiya"
        return 1
    fi
    return 0
}

снятие_доказать_пустоту() {
    # ── П-5: пустоту считает проба, а не снятие о себе ──────────────────────
    local kat=нет
    [ -d "$SNYATIE_KOREN_KOPII" ] && kat=есть
    следов_судить \
        "$(объектов_проекта "$SNYATIE_PROEKT" контейнеры)" \
        "$(объектов_проекта "$SNYATIE_PROEKT" тома)" \
        "$(объектов_проекта "$SNYATIE_PROEKT" сети)" \
        "$(объектов_проекта "$SNYATIE_PROEKT" образы)" \
        "$kat" || return 1

    # ── Носители БЕЗ префикса: на стенде /etc они были, и обязаны исчезнуть ─
    local ostalos_nositelej="" nositel
    for nositel in "$SNYATIE_STEND_ETC/nginx/sites-available/$SNYATIE_NGINX_FAJL" \
                   "$SNYATIE_STEND_ETC/nginx/sites-enabled/$SNYATIE_NGINX_FAJL" \
                   "$SNYATIE_STEND_ETC/letsencrypt/live/$SNYATIE_PROEKT" \
                   "$SNYATIE_STEND_ETC/letsencrypt/archive/$SNYATIE_PROEKT" \
                   "$SNYATIE_STEND_ETC/letsencrypt/renewal/$SNYATIE_PROEKT.conf"; do
        if [ -e "$nositel" ] || [ -L "$nositel" ]; then
            ostalos_nositelej="$ostalos_nositelej $nositel"
        fi
    done
    if [ -n "$ostalos_nositelej" ]; then
        printf 'носители без префикса пережили снятие:%s — на новой машине certbot renew падал бы на этом имени, а ключ остался бы на диске\n' \
            "$ostalos_nositelej"
        return 1
    fi
    printf 'носители без префикса сняты все пять: файл nginx «%s», ссылка, live, archive, renewal\n' "$SNYATIE_NGINX_FAJL"

    # ── П-6: соседи живы ────────────────────────────────────────────────────
    local vyvod_sluzhb sosedi_posle
    vyvod_sluzhb=$(systemctl is-active harness-agent harness-dispatcher harness-panel 2>&1 || true)
    службы_судить "$vyvod_sluzhb" 3 || return 1
    sosedi_posle=$(снимок_docker контейнеры)
    разность_судить "$SNYATIE_SOSEDI_DO" "$sosedi_posle" "" || return 1

    local kont_sredy tom_sredy kod_posle telo_posle
    kont_sredy=$(объектов_проекта "$PROEKT" контейнеры)
    tom_sredy=$(объектов_проекта "$PROEKT" тома)
    # Целость живой среды — это ЧИСЛО больше нуля, а не «не ноль»: «не
    # прочитано» тоже не равно нулю и молча сошло бы за целую среду (C-5).
    счёт_судить "контейнеры живой среды" "$kont_sredy" || return 1
    счёт_судить "тома живой среды" "$tom_sredy" || return 1
    if [ "$kont_sredy" = 0 ] || [ "$tom_sredy" = 0 ]; then
        printf 'живая среда %s пострадала от снятия копии: контейнеров %s, томов %s\n' \
            "$PROEKT" "$kont_sredy" "$tom_sredy"
        return 1
    fi
    kod_posle=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:$PORT/" 2>/dev/null)
    telo_posle=$(curl -s --max-time 10 "http://127.0.0.1:$PORT/" 2>/dev/null)
    web_судить "$kod_posle" "$telo_posle" "$MARKER" || return 1
    printf 'живая среда %s цела: контейнеров %s, томов %s, код 200 с маркером\n' \
        "$PROEKT" "$kont_sredy" "$tom_sredy"

    # ── Честно: чего на копии доказать нельзя ───────────────────────────────
    printf 'НЕ ПРОВЕРЕНО НА КОПИИ (ветки, которых у копии нет):\n'
    printf '  * файл nginx и сертификат сняты на СТЕНДЕ /etc этого прогона (%s), а не на боевом:\n' "$SNYATIE_STEND_ETC"
    printf '    боевой /etc проба не трогает нарочно, и писателя этих файлов на машине нет вовсе\n'
    printf '  * «nginx -T имени не содержит» (П-5): команда требует прав root, у пробы их нет\n'
    printf '  * кеш сборки: в compose нет служб с build:, своего кеша проект не заводит —\n'
    printf '    ветка «кеш есть» не воспроизводилась, снятие называет её несделанной само\n'
    printf '  * снятие ЖИВОЙ среды %s: не проверялось нарочно, для того и поднимается копия (И-1)\n' "$PROEKT"
    return 0
    return 0
}

режим_снятие() {
    прочитать_данные || return 1
    среда_на_месте || return 1
    docker_жив || return 1
    убрать_остатки_пробы
    снятие_живая_среда_до || return 1
    снятие_поднять_копию || return 1
    снятие_стенды_двери || return 1
    снятие_двери_и_снятие || return 1
    снятие_доказать_пустоту || return 1
    return 0
}

# ── САМОПРОВЕРКА ────────────────────────────────────────────────────────────
# Каждый режим имеет красный и зелёный случай на СВОЁМ стенде: подставной
# каталог, подставной вывод ss, подставные списки объектов. Совпадение с
# состоянием машины доказательством не считается.
#
# Самопроверка разобрана ПО ПРЕДМЕТАМ (ревью кода 28.09.2026, I-15): одна функция
# на 490 строк не умещалась в голове, её вложенные определения утекали в общее
# имя, и при разборе отказа приходилось гонять все пути ради одного. Теперь у
# каждого предмета своя функция, `--selftest <предмет>` гоняет одну группу, а
# счёт путей и неудач каждая группа называет сама — прежняя ручная роспись
# «5 порт, 4 готовность…» отставала от кода при каждой новой проверке.

# Общее состояние самопроверки: счётчики и двор. Переменные ФАЙЛА, а не local:
# трап EXIT срабатывает уже после возврата из функции, и local к этому моменту
# не существует — `set -u` убивал уборку строкой «dvor: unbound variable».
PUTEJ=0
PLOHO=0
DVOR_STENDA=""
STEND_DOCKER=""

НЕУДАЧА() { PLOHO=$(( PLOHO + 1 )); }

случай() { # $1 = имя случая, $2 = ждём код, $3 = ждём подстроку (можно пусто), $4… = команда
    local imya="$1" zhdem_rc="$2" zhdem_text="$3"; shift 3
    local vyvod rc
    vyvod=$("$@" 2>&1); rc=$?
    PUTEJ=$(( PUTEJ + 1 ))
    if [ "$rc" != "$zhdem_rc" ]; then
        printf '  ПЛОХО %s: код %s, ждали %s. Вывод: %s\n' "$imya" "$rc" "$zhdem_rc" "$vyvod"
        НЕУДАЧА; return
    fi
    if [ -n "$zhdem_text" ] && ! содержит "$vyvod" "$zhdem_text"; then
        printf '  ПЛОХО %s: в выводе нет «%s». Вывод: %s\n' "$imya" "$zhdem_text" "$vyvod"
        НЕУДАЧА; return
    fi
    printf '  ок    %s\n' "$imya"
}

# Подставной docker, отвечающий ненулевым кодом: стенд для правила «не прочитано»
# ≠ «ноль». Нужен двум группам сразу, поэтому заводится отдельно от них.
стенд_подставного_docker() {
STEND_DOCKER="$DVOR_STENDA/подставной-docker"
mkdir -p "$STEND_DOCKER"
printf '#!/bin/sh\nprintf "Cannot connect to the Docker daemon at unix:///var/run/docker.sock.\\n" >&2\nexit 1\n' \
    > "$STEND_DOCKER/docker"
chmod +x "$STEND_DOCKER/docker"
}

проверка_порт() {
# ── П-2, порт: стенд — подставной вывод ss ──────────────────────────────
local ss_svoj ss_naruzhu ss_shest ss_oba
ss_svoj='LISTEN 0      511          127.0.0.1:8090       0.0.0.0:*'
ss_naruzhu='LISTEN 0      511            0.0.0.0:8090       0.0.0.0:*'
ss_shest='LISTEN 0      511               [::]:8090          [::]:*'
ss_oba="$ss_svoj
$ss_naruzhu"
случай "порт: одна строка 127.0.0.1 — зелёный" 0 "наружу 0" порт_судить "$ss_svoj" 8090
случай "БОЛЬНОЙ СЛУЧАЙ: порт опубликован на 0.0.0.0 — смотрит наружу" 1 "смотрит наружу" \
    порт_судить "$ss_naruzhu" 8090
случай "БОЛЬНОЙ СЛУЧАЙ: порт опубликован на [::] — смотрит наружу" 1 "смотрит наружу" \
    порт_судить "$ss_shest" 8090
случай "БОЛЬНОЙ СЛУЧАЙ: 127.0.0.1 есть, но рядом 0.0.0.0 — красный" 1 "смотрит наружу" \
    порт_судить "$ss_oba" 8090
случай "БОЛЬНОЙ СЛУЧАЙ: порт не слушает никто — не зелёный" 1 "строк 127.0.0.1 — 0" \
    порт_судить "" 8090
}

проверка_готовность() {
# ── П-1, готовность базы: три исхода различимы ──────────────────────────
случай "готовность: healthy — зелёный" 0 "healthcheck healthy" готовность_судить healthy
случай "БОЛЬНОЙ СЛУЧАЙ: healthcheck не объявлен — это дыра, а не готовность" 1 "НЕ объявлен" \
    готовность_судить "НЕТ-HEALTHCHECK"
случай "БОЛЬНОЙ СЛУЧАЙ: статус starting — не готовность" 1 "не готова" готовность_судить starting
случай "БОЛЬНОЙ СЛУЧАЙ: docker промолчал — не готовность" 1 "не прочитан" готовность_судить ""

# ── П-1, ответ web: код и маркер ────────────────────────────────────────
# Маркер здесь СВОЙ, стендовый, а не прочитанный из данных: проверка обязана
# создавать своё условие, а взяв боевой маркер, она замолчала бы вместе с ним
# (ревью кода 28.09.2026, I-9 — маркер стал данными и может быть пустым).
local marker_stenda="маркер-стенда-web"
случай "web: 200 с маркером — зелёный" 0 "маркер" \
    web_судить 200 "<html>$marker_stenda</html>" "$marker_stenda"
случай "БОЛЬНОЙ СЛУЧАЙ: 200 отдал ЧУЖОЙ сервер (маркера нет)" 1 "нет маркера" \
    web_судить 200 "<html>welcome to nginx</html>" "$marker_stenda"
случай "БОЛЬНОЙ СЛУЧАЙ: web ответил 502" 1 "а не 200" web_судить 502 "" "$marker_stenda"
случай "БОЛЬНОЙ СЛУЧАЙ: curl не ответил вовсе" 1 "нет ответа" web_судить "" "" "$marker_stenda"
# Маркера в данных нет (на порт приехало приложение): судим КОДОМ и говорим это
# вслух, а не краснеем «отвечает не среда» — I-9.
случай "маркер данными не задан: 200 — зелёный, тело не судим" 0 "маркер данными не задан" \
    web_судить 200 "<html>любая разметка приложения</html>" ""
случай "БОЛЬНОЙ СЛУЧАЙ: маркера в данных нет, но и кода 200 нет" 1 "а не 200" \
    web_судить 502 "<html>любая разметка приложения</html>" ""
}

проверка_память() {
# ── Память: судится ФАКТ у контейнера, а не текст compose ───────────────
случай "память: 512+512 МБ при потолке 1536 — зелёный" 0 "сумма лимитов 1024 МБ" \
    память_судить "app-db-1 536870912
app-web-1 536870912" 1536
случай "БОЛЬНОЙ СЛУЧАЙ: mem_limit отброшен, .HostConfig.Memory = 0" 1 "лимит не применён" \
    память_судить "app-db-1 536870912
app-web-1 0" 1536
случай "БОЛЬНОЙ СЛУЧАЙ: 1024+1024 МБ выше потолка — названа сумма 2048" 1 "сумма лимитов 2048 МБ" \
    память_судить "app-db-1 1073741824
app-web-1 1073741824" 1536
случай "БОЛЬНОЙ СЛУЧАЙ: контейнеров среды нет — не зелёный" 1 "не по чему" память_судить "" 1536

# Р-1: идущий прогон не красит среду. Стенд — вывод docker inspect с меткой
# com.docker.compose.oneoff, какую compose ставит контейнеру `run --rm`.
local limity_s_progonom otsev
limity_s_progonom='app-db-1 1073741824 <no value>
app-web-1 268435456 <no value>
app-test-run-abc123 1073741824 True'
otsev=$(отсеять_одноразовые "$limity_s_progonom")
PUTEJ=$(( PUTEJ + 1 ))
if [ "$otsev" = 'app-db-1 1073741824
app-web-1 268435456' ]; then
    printf '  ок    отсев: одноразовый контейнер прогона снят со счёта, постоянные оба на месте\n'
else
    printf '  ПЛОХО отсев одноразовых дал «%s»\n' "$otsev"; НЕУДАЧА
fi
случай "память: идущий прогон не красит среду — сумма постоянных 1280 МБ" 0 \
    "сумма лимитов 1280 МБ" память_судить "$otsev" 1536
случай "БОЛЬНОЙ СЛУЧАЙ: без отсева тот же стенд даёт 2304 МБ и красный" 1 \
    "сумма лимитов 2304 МБ" память_судить "app-db-1 1073741824
app-web-1 268435456
app-test-run-abc123 1073741824" 1536
случай "БОЛЬНОЙ СЛУЧАЙ: постоянная служба без метки oneoff со счёта не снимается" 1 \
    "сумма лимитов 2304 МБ" память_судить "$(отсеять_одноразовые 'app-db-1 1073741824 <no value>
app-web-1 268435456 <no value>
app-fat-1 1073741824 <no value>')" 1536
}

проверка_соседи() {
# ── П-6, службы харнеса ─────────────────────────────────────────────────
случай "соседи: три active — зелёный" 0 "3 active" службы_судить "active
active
active" 3
случай "БОЛЬНОЙ СЛУЧАЙ: одна служба inactive" 1 "не active" службы_судить "active
inactive
active" 3
случай "БОЛЬНОЙ СЛУЧАЙ: systemctl назвал две строки из трёх" 1 "ждали 3 ответов" \
    службы_судить "active
active" 3

# ── П-3: оба снимка ложатся в ЖУРНАЛ, и путь называется (I-16) ──────────
# Критерий требовал журнала, а снимки только печатались в поток. Стенд свой:
# каталог журнала подменяется, боевой не трогается; «записать не смогли» —
# отказ с причиной, иначе у критерия нет носителя, а путь зелёный.
местный_журнал() { # $1 = каталог журнала, $2 = режим
    ( ZHURNAL_SNIMKOV="$1"; снимки_в_журнал "$2" "контейнер a" "контейнер a
контейнер app-db-1" 3 )
}
случай "снимки П-3: записаны в журнал, путь и число строк названы" 0 "оба снимка П-3 в журнале" \
    местный_журнал "$DVOR_STENDA/журнал-снимков" --чужое
PUTEJ=$(( PUTEJ + 1 ))
if [ "$(find "$DVOR_STENDA/журнал-снимков" -name '*-чужое.txt' 2>/dev/null | wc -l)" = 1 ] \
    && grep -q 'СНИМОК ДО ПОДЪЁМА' "$DVOR_STENDA"/журнал-снимков/*-чужое.txt \
    && grep -q 'СНИМОК ПОСЛЕ ПОДЪЁМА' "$DVOR_STENDA"/журнал-снимков/*-чужое.txt; then
    printf '  ок    в файле журнала ОБА снимка, а не один\n'
else
    printf '  ПЛОХО в журнале снимков нет обоих снимков: %s\n' \
        "$(ls -1 "$DVOR_STENDA/журнал-снимков" 2>/dev/null | tr '\n' ' ')"; НЕУДАЧА
fi
# Каталог не создать (на месте каталога лежит ФАЙЛ): отказ с причиной, а не
# молчаливый зелёный — у критерия нет носителя, и это надо сказать.
: > "$DVOR_STENDA/журнал-это-файл"
случай "БОЛЬНОЙ СЛУЧАЙ: журнал снимков записать некуда — отказ с причиной" 1 "нет носителя" \
    местный_журнал "$DVOR_STENDA/журнал-это-файл/внутрь" --чужое

# ── П-3, отбор чужого: метка против подстроки ───────────────────────────
local vse_obj sredy_obj chuzhie
vse_obj="контейнер foreign-app-postgres-1
контейнер app-db-1
контейнер ${PREFIKS_PROBY}selftest-1
контейнер harness-restore-proba-7
том app_dbdata
том ai-app-bot-data"
sredy_obj='контейнер app-db-1
том app_dbdata'
chuzhie=$(отобрать_чужие "$vse_obj" "$sredy_obj" "harness-restore-proba- $PREFIKS_PROBY")
PUTEJ=$(( PUTEJ + 1 ))
if [ "$chuzhie" = 'контейнер foreign-app-postgres-1
том ai-app-bot-data' ]; then
    printf '  ок    отбор чужого: среда снята по метке, свои пробы по префиксам\n'
else
    printf '  ПЛОХО отбор чужого дал «%s»\n' "$chuzhie"; НЕУДАЧА
fi
PUTEJ=$(( PUTEJ + 1 ))
if содержит "$chuzhie" 'ai-app-bot-data'; then
    printf '  ок    БОЛЬНОЙ СЛУЧАЙ: чужое имя с буквами «app» внутри осталось чужим\n'
else
    printf '  ПЛОХО чужой объект ai-app-bot-data принят за объект среды — судим подстрокой\n'; НЕУДАЧА
fi

# ── П-3, разность: обе стороны названы порознь ──────────────────────────
случай "разность: множества равны — «пропало 0, появилось вне префикса среды 0»" 0 \
    "пропало 0, появилось вне префикса среды 0" \
    разность_судить "контейнер a
контейнер b" "контейнер a
контейнер b" ""
случай "БОЛЬНОЙ СЛУЧАЙ: чужой контейнер ПРОПАЛ между снимками (П-3)" 1 "пропало 1" \
    разность_судить "контейнер foreign-app-postgres-1
контейнер b" "контейнер b" ""
случай "БОЛЬНОЙ СЛУЧАЙ: между снимками ПОЯВИЛСЯ чужой объект" 1 "появилось вне префикса среды 1" \
    разность_судить "контейнер a" "контейнер a
том chuzhoj-tom" ""
случай "образ, притянутый средой, появлением чужого не считается" 0 \
    "появилось вне префикса среды 0" \
    разность_судить "контейнер a" "контейнер a
образ postgres:17-alpine" "образ postgres:17-alpine"
}

проверка_окно() {
# ── Окно ночных механизмов ──────────────────────────────────────────────
путь_окна() { # $1 = имя, $2 = ждём(да|нет), $3 = время
    local bylo=нет
    в_окне "$3" "03:00" "06:30" && bylo=да
    PUTEJ=$(( PUTEJ + 1 ))
    if [ "$bylo" = "$2" ]; then printf '  ок    %s\n' "$1"
    else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$bylo"; НЕУДАЧА; fi
}
путь_окна "окно: 04:00 внутри ночного окна" да "04:00"
путь_окна "окно: 12:00 снаружи" нет "12:00"
путь_окна "окно: 03:00 — граница внутри" да "03:00"
путь_окна "окно: 06:30 — граница внутри" да "06:30"
путь_окна "окно: 06:31 — уже снаружи" нет "06:31"
путь_окна "окно: 02:59 — ещё снаружи" нет "02:59"
}

проверка_жнец() {
# ── Жнец: что он берёт и чего не берёт ──────────────────────────────────
путь_жнеца() { # $1 = имя, $2 = ждём(да|нет), $3 = имя объекта
    local bylo=нет
    жнец_берёт "$3" && bylo=да
    PUTEJ=$(( PUTEJ + 1 ))
    if [ "$bylo" = "$2" ]; then printf '  ок    %s\n' "$1"
    else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$bylo"; НЕУДАЧА; fi
}
путь_жнеца "жнец берёт свой контейнер с префиксом пробы" да "${PREFIKS_PROBY}selftest-1"
путь_жнеца "БОЛЬНОЙ СЛУЧАЙ: том среды app_dbdata жнецу НЕ принадлежит" нет "app_dbdata"
путь_жнеца "БОЛЬНОЙ СЛУЧАЙ: контейнер среды app-db-1 жнецу НЕ принадлежит" нет "app-db-1"
путь_жнеца "чужой foreign-app-postgres-1 жнецу не принадлежит" нет "foreign-app-postgres-1"

# Источник жнеца: каждая строка удаления тома названа префиксом пробы, и
# голого фильтра по имени среды в файле нет. Проверка текста стоит РЯДОМ с
# проверкой поведения, а не вместо неё.
local stroki_rm stroki_goliy
# Комментарии отбрасываются ПЕРВЫМИ: проверка судит КОД, а не прозу о нём.
# Без этого строка объяснения «`docker volume rm <проект>_dbdata` уносил базу»
# считалась удалением без префикса и красила самопроверку — тот же приём уже
# стоял в проверке текста снятия, а здесь его не было (29.09.2026).
# Ищется ИМЯ переменной, а не её значение, — как в соседнем замке ниже. Пока
# искалось значение, условие держал хвостовой комментарий со вписанным
# префиксом: он и был носителем, и смена префикса сразу покрасила шесть строк
# верного кода. Одно и то же обязано проверяться одним способом.
stroki_rm=$(grep -vE '^[[:space:]]*#' "$0" \
    | grep -E 'docker (volume|network) (rm|prune)' | grep -cv 'PREFIKS_PROBY' || true)
stroki_goliy=$(grep -vE '^[[:space:]]*#' "$0" \
    | grep 'filter "name=' | grep -cv 'PREFIKS_PROBY' || true)
PUTEJ=$(( PUTEJ + 1 ))
if [ "$stroki_rm" = 0 ]; then
    printf '  ок    в файле нет ни одной строки удаления без префикса %s\n' "$PREFIKS_PROBY"
else
    printf '  ПЛОХО строк удаления без префикса пробы: %s\n' "$stroki_rm"; НЕУДАЧА
fi
PUTEJ=$(( PUTEJ + 1 ))
if [ "$stroki_goliy" = 0 ]; then
    printf '  ок    все фильтры по имени идут от префикса пробы: голого name=<имя среды> нет\n'
else
    printf '  ПЛОХО фильтр по имени без префикса пробы встречается %s раз(а)\n' "$stroki_goliy"; НЕУДАЧА
fi

# ── Жнец вживую: свой мусор убран, объект с именем среды цел ────────────
PUTEJ=$(( PUTEJ + 1 ))
if ! docker ps >/dev/null 2>&1; then
    printf '  ПЛОХО живую пробу жнеца провести нечем: docker не отвечает\n'; НЕУДАЧА
else
    local tom_svoj tom_primanka vyvod_zhnetsa
    tom_svoj="${PREFIKS_PROBY}selftest-$$"
    # Приманка НАЗВАНА как том среды с хвостом стенда: фильтр по имени проекта
    # взял бы и её, фильтр префикса пробы не берёт. Живой том среды в стенд не
    # заводится нарочно — И-1 не проверяется на настоящих данных.
    # Имя среды берётся из СВОЕГО префикса, а не из данных: подставить сюда
    # прочитанный проект значило бы завести стенду имя боевого тома.
    tom_primanka="${PREFIKS_PROBY%%-*}_dbdata_selftest_$$"
    docker volume create "$tom_svoj" >/dev/null 2>&1
    docker volume create "$tom_primanka" >/dev/null 2>&1
    # Дворы: старый — брошенный прошлым прогоном, свежий — двор живого
    # соседа. Возраст разделяет их, имя — нет: оба зовутся одинаково.
    local dvor_staryj dvor_svezhij
    dvor_staryj="/tmp/${PREFIKS_PROBY}двор-старый-$$"
    dvor_svezhij="/tmp/${PREFIKS_PROBY}двор-свежий-$$"
    mkdir -p "$dvor_staryj" "$dvor_svezhij"
    touch -d "$(( VOZRAST_DVORA_MIN + 10 )) minutes ago" "$dvor_staryj"
    vyvod_zhnetsa=$(убрать_остатки_пробы)
    PUTEJ=$(( PUTEJ + 1 ))
    if [ -d "$dvor_staryj" ]; then
        printf '  ПЛОХО брошенный двор %s не убран: %s\n' "$dvor_staryj" "$vyvod_zhnetsa"; НЕУДАЧА
    else
        printf '  ок    жнец убрал брошенный двор старше %s минут\n' "$VOZRAST_DVORA_MIN"
    fi
    PUTEJ=$(( PUTEJ + 1 ))
    if [ -d "$dvor_svezhij" ]; then
        printf '  ок    БОЛЬНОЙ СЛУЧАЙ: свежий двор соседнего прогона жнец НЕ тронул\n'
    else
        printf '  ПЛОХО жнец снёс свежий двор — он убил бы стенд живого соседа\n'; НЕУДАЧА
    fi
    rm -rf "$dvor_staryj" "$dvor_svezhij"
    if docker volume inspect "$tom_svoj" >/dev/null 2>&1; then
        printf '  ПЛОХО свой остаток не убран: %s\n' "$vyvod_zhnetsa"; НЕУДАЧА
        docker volume rm -f "$tom_svoj" >/dev/null 2>&1  # только префикс $PREFIKS_PROBY
    elif ! docker volume inspect "$tom_primanka" >/dev/null 2>&1; then
        printf '  ПЛОХО жнец снёс объект с именем среды (%s) — это был бы том базы\n' "$tom_primanka"; НЕУДАЧА
    elif содержит_образец "$vyvod_zhnetsa" 'томов [1-9]'; then
        printf '  ок    БОЛЬНОЙ СЛУЧАЙ вживую: свой том убран, том с именем среды цел, число названо\n'
    else
        printf '  ПЛОХО жнец молчит о числе убранного: %s\n' "$vyvod_zhnetsa"; НЕУДАЧА
    fi
    docker volume rm -f "$tom_primanka" >/dev/null 2>&1  # приманка стенда по ТОЧНОМУ имени, не фильтр $PREFIKS_PROBY
fi
}

проверка_данные() {
# ── Данные: читаются, а закрытый источник — отказ, а не «пусто» ─────────
local stend_yaml stend_okno
stend_yaml="$DVOR_STENDA/prefixes.yaml"
cat > "$stend_yaml" <<'YAML'
среда:
  метка: com.docker.compose.project
  проект: proba-sredy
  маркер: маркер-из-данных
  потолок_мб: 777
исключить_префиксы:
  - stend-odin-
  - stend-dva-
окно_без_снимков:
  с: "01:00"
  по: "02:00"
окно_без_остановки:
  с: "09:00"
  по: "21:00"
YAML
PUTEJ=$(( PUTEJ + 1 ))
if (DANNYE="$stend_yaml"; прочитать_данные >/dev/null \
    && [ "$PROEKT" = proba-sredy ] && [ "$OKNO_S" = "01:00" ] \
    && содержит "$PREFIKSY" 'stend-dva-'); then
    printf '  ок    данные читаются из файла: метка, проект, окно, префиксы\n'
else
    printf '  ПЛОХО подставной yaml прочитан неверно\n'; НЕУДАЧА
fi
случай "БОЛЬНОЙ СЛУЧАЙ: файла данных нет — отказ, а не молчаливое «пусто»" 1 "данные не прочитаны" \
    bash "$0" --порт --данные "$DVOR_STENDA/нет-такого.yaml"

# Маркер, потолок памяти и окно остановки — ДАННЫЕ (ревью кода 28.09.2026,
# I-8, I-9): вписанные числом, они краснели бы при первой же смене заглушки на
# приложение и при третьей службе.
PUTEJ=$(( PUTEJ + 1 ))
if (DANNYE="$stend_yaml"; прочитать_данные >/dev/null \
    && [ "$MARKER" = "маркер-из-данных" ] && [ "$POTOLOK_MB" = 777 ] \
    && [ "$OSTANOVKA_S" = "09:00" ] && [ "$OSTANOVKA_PO" = "21:00" ]); then
    printf '  ок    маркер, потолок памяти и окно остановки взяты ИЗ ДАННЫХ, а не из строк пробы\n'
else
    printf '  ПЛОХО данные о маркере/потолке/окне остановки прочитаны неверно: маркер «%s», потолок «%s», окно «%s–%s»\n' \
        "$MARKER" "$POTOLOK_MB" "$OSTANOVKA_S" "$OSTANOVKA_PO"; НЕУДАЧА
fi
# Ключей нет вовсе: маркер пуст (судим кодом), потолок остаётся умолчанием файла,
# окна остановки нет. «Нет ключа» — не «ноль»: нулевой потолок покрасил бы любую
# среду, и причина указала бы на память вместо данных.
local stend_bez_klyuchej="$DVOR_STENDA/prefixes-без-ключей.yaml"
grep -v 'маркер:\|потолок_мб:\|окно_без_остановки:\|"09:00"\|"21:00"' "$stend_yaml" > "$stend_bez_klyuchej"
PUTEJ=$(( PUTEJ + 1 ))
if (DANNYE="$stend_bez_klyuchej"; прочитать_данные >/dev/null \
    && [ -z "$MARKER" ] && [ "$POTOLOK_MB" = 1536 ] && [ -z "$OSTANOVKA_S" ]); then
    printf '  ок    ключей нет: маркер пуст (судим кодом), потолок — умолчание файла 1536, окна остановки нет\n'
else
    printf '  ПЛОХО без ключей вышло: маркер «%s», потолок «%s», окно «%s»\n' \
        "$MARKER" "$POTOLOK_MB" "$OSTANOVKA_S"; НЕУДАЧА
fi
# Окно остановки на все сутки: --архив ОТКЛАДЫВАЕТ замер кодом 2, а не гасит
# живую среду ради пробы (I-8).
local stend_stop_vsegda="$DVOR_STENDA/prefixes-остановка-всегда.yaml"
sed 's/"09:00"/"00:00"/; s/"21:00"/"23:59"/' "$stend_yaml" > "$stend_stop_vsegda"
случай "БОЛЬНОЙ СЛУЧАЙ: внутри окна «не останавливать» --архив отказывается от замера (код 2)" 2 \
    "среду не останавливать" \
    bash "$0" --архив --данные "$stend_stop_vsegda" --корень "$DVOR_STENDA/нет-такой-среды"

# ── Сквозные случаи: проба запускается собой на подставном корне ────────
случай "БОЛЬНОЙ СЛУЧАЙ: каталога среды нет — --подъём краснеет с причиной" 1 "среды нет" \
    bash "$0" --подъём --корень "$DVOR_STENDA/нет-такой-среды"
PUTEJ=$(( PUTEJ + 1 ))
# Вывод сначала СОБИРАЕТСЯ в переменную, потом судится: конвейер «команда | grep -q»
# под pipefail отдаёт 141 при РАННЕМ совпадении, и «слова о пройденной пробе есть»
# прочиталось бы как «их нет» — ложный зелёный (живой прогон 29.09.2026).
VYVOD_OTKAZA=$(bash "$0" --подъём --корень "$DVOR_STENDA/нет-такой-среды" 2>&1)
if содержит "$VYVOD_OTKAZA" 'ПРОБА ПРОЙДЕНА'; then
    printf '  ПЛОХО отказавшая проба всё равно печатает слова о пройденной пробе\n'; НЕУДАЧА
else
    printf '  ок    отказавшая проба слов о пройденной пробе не печатает\n'
fi
случай "БОЛЬНОЙ СЛУЧАЙ: --память без среды не считает сумму по тексту compose" 1 "среды нет" \
    bash "$0" --память --корень "$DVOR_STENDA/нет-такой-среды"

# Окно: один стенд объявляет его на все сутки (режим обязан ОТЛОЖИТЬ замер
# кодом 2, а не покраснеть), второй — на десять минут, заведомо не
# накрывающих текущую минуту: окно СЧИТАЕТСЯ от часов, а не вписано
# числом — вписанное число делало бы путь флейком раз в сутки.
local m_teper m_s m_po stend_mimo
stend_okno="$DVOR_STENDA/okno-vsegda.yaml"
sed 's/"01:00"/"00:00"/; s/"02:00"/"23:59"/' "$stend_yaml" > "$stend_okno"
m_teper=$(минуты "$(date +%H:%M)")
if [ "$m_teper" -lt 1400 ]; then m_s=$(( m_teper + 20 )); m_po=$(( m_teper + 30 ));
else m_s=0; m_po=10; fi
stend_mimo="$DVOR_STENDA/okno-mimo.yaml"
sed "s|\"01:00\"|\"$(printf '%02d:%02d' $(( m_s / 60 )) $(( m_s % 60 )))\"|; \
     s|\"02:00\"|\"$(printf '%02d:%02d' $(( m_po / 60 )) $(( m_po % 60 )))\"|" \
    "$stend_yaml" > "$stend_mimo"
случай "БОЛЬНОЙ СЛУЧАЙ: внутри ночного окна --чужое отказывается от замера (код 2)" 2 "ЗАМЕР ОТЛОЖЕН" \
    bash "$0" --чужое --данные "$stend_okno" --корень "$DVOR_STENDA/нет-такой-среды"
случай "вне окна --чужое идёт дальше и упирается в отсутствие среды" 1 "среды нет" \
    bash "$0" --чужое --данные "$stend_mimo" --корень "$DVOR_STENDA/нет-такой-среды"
}

проверка_перенос() {
# ── П-4, сумма архива: стенд — подставной архив со своей суммой ─────────
# Файл суммы считается ТОЙ ЖЕ командой, что в сборщике, а портится САМ
# архив — ровно то, что случается при обрыве копирования на другую машину.
local stend_arh="$DVOR_STENDA/стенд-архив.tar.zst"
printf 'содержимое подставного архива\n' > "$stend_arh"
( cd "$DVOR_STENDA" && sha256sum "$(basename "$stend_arh")" > "$(basename "$stend_arh").sha256" )
случай "сумма: архив цел — зелёный" 0 "сходится" сумма_судить "$stend_arh"
printf 'архив доехал не целиком\n' >> "$stend_arh"
случай "БОЛЬНОЙ СЛУЧАЙ: архив изменился после подсчёта — сумма НЕ сходится" 1 "НЕ сходится" \
    сумма_судить "$stend_arh"
rm -f "$stend_arh.sha256"
случай "БОЛЬНОЙ СЛУЧАЙ: файла суммы рядом нет — проверять нечем" 1 "нет файла суммы" \
    сумма_судить "$stend_arh"
случай "БОЛЬНОЙ СЛУЧАЙ: архива нет вовсе" 1 "архива нет" \
    сумма_судить "$DVOR_STENDA/нет-такого-архива.tar.zst"

# ── П-4, архив лежит вне каталога среды ─────────────────────────────────
случай "архив рядом со средой, но не внутри — зелёный" 0 "вне каталога" \
    вне_среды_судить "/var/www/app-архивы/a.tar.zst" "/var/www/app"
случай "БОЛЬНОЙ СЛУЧАЙ: архив внутри каталога среды — снятие унесёт и его" 1 "ВНУТРИ" \
    вне_среды_судить "/var/www/app/backup/a.tar.zst" "/var/www/app"

# ── П-4, контрольная таблица: число И марка ─────────────────────────────
случай "таблица: строк столько же и марка та же — зелёный" 0 "та же" \
    таблица_судить 137 137 "perenos-1" "perenos-1"
случай "БОЛЬНОЙ СЛУЧАЙ: в копии строк меньше — том переехал не целиком" 1 "не целиком" \
    таблица_судить 137 12 "perenos-1" "perenos-1"
случай "БОЛЬНОЙ СЛУЧАЙ: строк столько же, но марка от ПРОШЛОГО прогона" 1 "не из этого архива" \
    таблица_судить 137 137 "perenos-1" "perenos-0"
случай "БОЛЬНОЙ СЛУЧАЙ: таблицы в копии нет — psql ответил ошибкой" 1 "не числом" \
    таблица_судить 137 'ERROR:  relation "proba_perenosa" does not exist' "perenos-1" ""

# ── П-4, порт копии ─────────────────────────────────────────────────────
случай "порт копии свободен — зелёный" 0 "свободен" порт_свободен_судить 18090 ""
случай "БОЛЬНОЙ СЛУЧАЙ: порт копии занят — причина названа до подъёма" 1 "занят" \
    порт_свободен_судить 18090 'LISTEN 0 511 127.0.0.1:18090 0.0.0.0:*'

# ── П-4, ответ копии: КОД и МАРКЕР, а не тело байт в байт ───────────────
# Сверка тел держалась на статической заглушке: любое приложение отдаёт в
# разметке время, csrf-токен или номер сессии, и путь стал бы вечно красным
# (ревью кода 28.09.2026, I-9). Что переехали ДАННЫЕ, доказывает таблица_судить.
local marker_kopii="маркер-стенда-копии"
случай "копия ответила как оригинал — зелёный" 0 "байт в байт" \
    копия_судить 200 "<h1>$marker_kopii</h1>" "<h1>$marker_kopii</h1>" "$marker_kopii"
случай "БОЛЬНОЙ СЛУЧАЙ: копия отдала 200 БЕЗ маркера" 1 "нет маркера" \
    копия_судить 200 "<html>welcome to nginx</html>" "<h1>$marker_kopii</h1>" "$marker_kopii"
случай "тело копии отличается от оригинала (время, токен) — маркер и код на месте, это не находка" 0 \
    "данные судит контрольная таблица" \
    копия_судить 200 "<h1>$marker_kopii</h1> 12:31:07" "<h1>$marker_kopii</h1>" "$marker_kopii"
случай "БОЛЬНОЙ СЛУЧАЙ: копия ответила 502" 1 "а не 200" \
    копия_судить 502 "" "<h1>$marker_kopii</h1>" "$marker_kopii"

# ── C-6: имя проекта копии — ФАКТ от compose, а не надежда на «-p» ──────
случай "имя проекта копии: compose назвал то же имя — зелёный" 0 "подтвердил" \
    имя_проекта_судить "${PREFIKS_PROBY}kopiya-1" "${PREFIKS_PROBY}kopiya-1"
случай "БОЛЬНОЙ СЛУЧАЙ: compose считает проектом копии «app» — «-p» не сработал" 1 "чужому проекту" \
    имя_проекта_судить "${PREFIKS_PROBY}kopiya-1" "app"
случай "БОЛЬНОЙ СЛУЧАЙ: compose об имени проекта промолчал" 1 "не назвал" \
    имя_проекта_судить "${PREFIKS_PROBY}kopiya-1" ""

# Снятие копии вживую: третий замок судит ФАЙЛ, а не переменную. Чужое имя
# в стенде НАРОЧНО начинается с префикса пробы, а не зовётся «app»:
# сломайся замок, `down -v` ушёл бы по имени из файла, и стенд не имеет
# права называть им живую среду (И-1).
путь_снятия_копии() { # $1 = имя случая, $2 = своё|чужое имя в compose копии, $3 = ждём (снимает|отказ)
    local dvor_svoj proekt_stenda imya_v_fajle bylo=снимает vyvod
    proekt_stenda="${PREFIKS_PROBY}kopiya-stend-$$"
    if [ "$2" = своё ]; then imya_v_fajle="$proekt_stenda"
    else imya_v_fajle="${PREFIKS_PROBY}chuzhoj-stend-$$"; fi
    dvor_svoj=$(mktemp -d "/tmp/${PREFIKS_PROBY}kopiya-stend-XXXXXX")
    mkdir -p "$dvor_svoj/распакованное/files"
    printf 'name: %s\nservices:\n  web:\n    image: alpine:3.20\n' "$imya_v_fajle" \
        > "$dvor_svoj/распакованное/files/compose.yaml"
    : > "$dvor_svoj/распакованное/files/.env"
    local dvor_pomnyu="$DVOR_ARHIVA" proekt_pomnyu="$KOPIYA_PROEKT"
    DVOR_ARHIVA="$dvor_svoj"
    KOPIYA_PROEKT="$proekt_stenda"
    vyvod=$(снять_копию 2>&1)
    содержит "$vyvod" 'НЕ снята через down -v' && bylo=отказ
    DVOR_ARHIVA="$dvor_pomnyu"
    KOPIYA_PROEKT="$proekt_pomnyu"
    rm -rf "$dvor_svoj"
    PUTEJ=$(( PUTEJ + 1 ))
    if [ "$bylo" = "$3" ]; then printf '  ок    %s\n' "$1"
    else printf '  ПЛОХО %s: ждали «%s», вышло «%s». Вывод: %s\n' "$1" "$3" "$bylo" "$vyvod"; НЕУДАЧА; fi
}
путь_снятия_копии "снятие копии: в её compose стоит её имя — down -v зовётся" своё снимает
путь_снятия_копии "БОЛЬНОЙ СЛУЧАЙ: в compose копии ЧУЖОЕ имя проекта — down -v НЕ зовётся" чужое отказ

# ── C-4: сборщик судит СОСТАВ каталога, а не только наличие названного ──
состав_сборщиком() { # $1 = каталог, $2… = имена из списка CONTENT
    EXPORT_TOLKO_FUNKCII=1 bash -c '
        sborshchik="$1"; shift
        grep -q "^состав_каталога_судить()" "$sborshchik" || {
            printf "в %s нет судьи состава каталога — лишнее в каталоге среды ловить нечем\n" "$sborshchik"
            exit 1
        }
        source "$sborshchik" >/dev/null 2>&1
        состав_каталога_судить "$@"' _ "$SREDA_KOREN/scripts/export.sh" "$@"
}
local stend_sostav="$DVOR_STENDA/среда-состав"
mkdir -p "$stend_sostav/src"
: > "$stend_sostav/compose.yaml"
: > "$stend_sostav/.env"
случай "состав каталога: всё названо списком CONTENT — зелёный" 0 "совпал со списком" \
    состав_сборщиком "$stend_sostav" src compose.yaml .env
mkdir -p "$stend_sostav/media"
случай "БОЛЬНОЙ СЛУЧАЙ: в каталоге среды media/ сверх списка — сбор отказывает" 1 "media" \
    состав_сборщиком "$stend_sostav" src compose.yaml .env
# Третий ответ между «увожу» и «отказ»: кеш, объявленный списком NE_DANNYE. Без
# него каркас приложения (.pnpm-store, .home, .run.lock) остановил и сбор, и снятие
# насовсем — живой прогон 29.09.2026. Пропущенное обязано быть НАЗВАНО.
случай "объявленный кеш не едет и назван, а не выброшен молча" 0 "не данные и не едут (1): media" \
    состав_сборщиком "$stend_sostav" src compose.yaml .env кроме media
случай "БОЛЬНОЙ СЛУЧАЙ: кеш объявлен ОДИН, а лишнего в каталоге два" 1 ".env" \
    состав_сборщиком "$stend_sostav" src compose.yaml кроме media
# Тот же отказ ВЖИВУЮ: правило обязано стоять на пути сборщика, а не только
# в его функции ([[the-check-fell-behind-the-code]]).
local stend_sbora="$DVOR_STENDA/среда-лишнее"
mkdir -p "$stend_sbora/scripts" "$stend_sbora/media"
: > "$stend_sbora/compose.yaml"
cp "$SREDA_KOREN/scripts/export.sh" "$stend_sbora/scripts/export.sh" 2>/dev/null
случай "БОЛЬНОЙ СЛУЧАЙ вживую: сборщик на каталоге с лишним media/ отказывает ДО docker" 1 "нет в списке CONTENT" \
    bash "$stend_sbora/scripts/export.sh"

# ── П-4, сквозные: до разрушительных шагов проба обязана отказать ───────
случай "БОЛЬНОЙ СЛУЧАЙ: каталога среды нет — --архив краснеет с причиной" 1 "среды нет" \
    bash "$0" --архив --корень "$DVOR_STENDA/нет-такой-среды"
mkdir -p "$DVOR_STENDA/среда-без-сборщика"
: > "$DVOR_STENDA/среда-без-сборщика/compose.yaml"
случай "БОЛЬНОЙ СЛУЧАЙ: среда есть, а scripts/export.sh нет" 1 "сборщика архива нет" \
    bash "$0" --архив --корень "$DVOR_STENDA/среда-без-сборщика"
}

проверка_следы() {
# ── П-5: пустота после снятия ───────────────────────────────────────────
случай "следов нет: все четыре нуля и каталога нет — зелёный" 0 "следов нет" \
    следов_судить 0 0 0 0 нет
случай "БОЛЬНОЙ СЛУЧАЙ: контейнер проекта пережил снятие" 1 "контейнеров 1" \
    следов_судить 1 0 0 0 нет
случай "БОЛЬНОЙ СЛУЧАЙ: том проекта пережил снятие — это данные" 1 "томов 1" \
    следов_судить 0 1 0 0 нет
случай "БОЛЬНОЙ СЛУЧАЙ: объектов нет, а каталог на месте" 1 "каталог среды на месте" \
    следов_судить 0 0 0 0 есть

# ── C-5: отказ docker — это «не прочитано», а не «ноль» ────────────────
# Стенд — подставной docker, отвечающий ненулевым кодом: своё условие, а не
# ожидание, что демон когда-нибудь упадёт сам.
счёт_пробой() { # $1 = каталог подставного docker («нет» — настоящий), $2 = проект, $3 = вид
    if [ "$1" = нет ]; then
        ( METKA="com.docker.compose.project"; объектов_проекта "$2" "$3" )
    else
        ( PATH="$1:$PATH"; METKA="com.docker.compose.project"; объектов_проекта "$2" "$3" )
    fi
}
путь_счёта() { # $1 = имя случая, $2 = ждём ТОЧНО, $3… = аргументы счёта
    local imya="$1" zhdem="$2"; shift 2
    local bylo
    bylo=$(счёт_пробой "$@")
    PUTEJ=$(( PUTEJ + 1 ))
    if [ "$bylo" = "$zhdem" ]; then printf '  ок    %s\n' "$imya"
    else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$imya" "$zhdem" "$bylo"; НЕУДАЧА; fi
}
путь_счёта "счёт: живой docker, проекта нет — ровно «0»" 0 нет "${PREFIKS_PROBY}net-takogo-$$" контейнеры
случай "БОЛЬНОЙ СЛУЧАЙ: docker ответил ненулевым кодом — «не прочитано», а не 0" 2 "не прочитано" \
    счёт_пробой "$STEND_DOCKER" "${PREFIKS_PROBY}net-takogo-$$" контейнеры
случай "БОЛЬНОЙ СЛУЧАЙ: в счёте следов «не прочитано» вместо числа — красный" 1 \
    "тома: счёт не прочитан" \
    следов_судить 0 "не прочитано (docker код 1)" 0 0 нет

# ── I-3: принадлежность каталога доказывается ТОМАМИ, а не строкой name: ──
# Контейнеры погашены, тома живы, а каталог — любой, где лежит файл из одной
# строки с именем проекта: так `docker volume rm <проект>_dbdata` уносил базу, пока
# `rm -rf` уносил подставной каталог (ревью кода 28.09.2026, I-3). Судья зовётся
# ИЗ снятия, а не переписывается здесь копией.
local snyatie_dlya_sudej="$SREDA_KOREN/scripts/teardown.sh"
принадлежность_снятием() { # $1 = каталог, $2 = working_dir, $3 = name:, $4 = тома метки, $5 = тома compose
    TEARDOWN_TOLKO_FUNKCII=1 bash -c '
        source "$1" "$2" >/dev/null 2>&1
        shift 2
        принадлежность_судить "$@"' _ "$snyatie_dlya_sudej" "${PREFIKS_PROBY}sudya-$$" "$@"
}
случай "принадлежность: контейнеры проекта называют этот каталог меткой — зелёный" 0 "меткой" \
    принадлежность_снятием "/tmp/${PREFIKS_PROBY}sudya" "/tmp/${PREFIKS_PROBY}sudya" "" "" ""
случай "принадлежность: имя в compose то же и тома совпали — зелёный" 0 "тома compose совпали" \
    принадлежность_снятием "/tmp/${PREFIKS_PROBY}sudya" "" "${PREFIKS_PROBY}sudya-$$" \
    "${PREFIKS_PROBY}sudya-${$}_dbdata" "${PREFIKS_PROBY}sudya-${$}_dbdata"
случай "БОЛЬНОЙ СЛУЧАЙ: подставной каталог с одной строкой «name:», а тома живы" 1 "набор не тот" \
    принадлежность_снятием "/tmp/чужой-каталог" "" "${PREFIKS_PROBY}sudya-$$" \
    "${PREFIKS_PROBY}sudya-${$}_dbdata" ""
случай "БОЛЬНОЙ СЛУЧАЙ: контейнеры называют ДРУГОЙ каталог" 1 "ошибка имени" \
    принадлежность_снятием "/tmp/не-тот" "/tmp/${PREFIKS_PROBY}sudya" "" "" ""
случай "БОЛЬНОЙ СЛУЧАЙ: в compose каталога имя ДРУГОЙ среды" 1 "ДРУГОЙ среды" \
    принадлежность_снятием "/tmp/${PREFIKS_PROBY}sudya" "" "чужая-среда" "" ""
случай "БОЛЬНОЙ СЛУЧАЙ: доказывать принадлежность нечем — объектов нет, имени нет" 1 "доказать нечем" \
    принадлежность_снятием "/tmp/${PREFIKS_PROBY}sudya" "" "" "" ""

# ── I-14: пустота считает образы ТЕМ ЖЕ фильтром, каким снятие их снимает ──
# Базовый образ снятие пропускает (он общий с соседями), а счёт пустоты его
# учитывал: окажись метка проекта на базовом образе, снятие разрушило бы всё и
# вернуло «объекты ОСТАЛИСЬ: образов 1», когда исправлять уже нечего.
счёт_образов_снятием() { # $1 = что ответил бы docker на перечисление образов
    SPISOK_OBRAZOV="$1" TEARDOWN_TOLKO_FUNKCII=1 bash -c '
        source "$1" "$2" >/dev/null 2>&1
        объекты_проекта() { printf "%s" "$SPISOK_OBRAZOV"; }
        сколько_снимаемых_образов' _ "$snyatie_dlya_sudej" "${PREFIKS_PROBY}sudya-$$"
}
путь_счёта_образов() { # $1 = имя случая, $2 = ждём ТОЧНО, $3 = стенд перечисления
    local bylo
    bylo=$(счёт_образов_снятием "$3")
    PUTEJ=$(( PUTEJ + 1 ))
    if [ "$bylo" = "$2" ]; then printf '  ок    %s\n' "$1"
    else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$bylo"; НЕУДАЧА; fi
}
путь_счёта_образов "счёт образов: свой образ один — «1»" 1 \
    "sha256:aaa app-web:latest"
путь_счёта_образов "БОЛЬНОЙ СЛУЧАЙ: метка проекта на БАЗОВОМ образе — в счёт не идёт, его и не снимают" 0 \
    "sha256:bbb nginx:alpine"
путь_счёта_образов "счёт образов: свой и базовый рядом — «1», как и снимется один" 1 \
    "sha256:bbb nginx:alpine
sha256:aaa app-web:latest"
путь_счёта_образов "счёт образов: образов нет вовсе — «0»" 0 ""

# ── И-1: на что снятие вообще зовётся ───────────────────────────────────
случай "копия своя: префикс пробы и двор пробы — зелёный" 0 "копия своя" \
    копия_снятия_своя "${PREFIKS_PROBY}snyatie-1" "/tmp/${PREFIKS_PROBY}snyatie-1"
случай "БОЛЬНОЙ СЛУЧАЙ: имя ЖИВОЙ среды — снятие не зовётся" 1 "не начинается с префикса" \
    копия_снятия_своя "app" "/tmp/${PREFIKS_PROBY}snyatie-1"
случай "БОЛЬНОЙ СЛУЧАЙ: каталог живой среды — снятие не зовётся" 1 "вне двора пробы" \
    копия_снятия_своя "${PREFIKS_PROBY}snyatie-1" "/var/www/app"

# ── Дверь: как проба судит её отказ ─────────────────────────────────────
случай "дверь держит: отказ с причиной и копия цела — зелёный" 0 "дверь держит" \
    отказ_двери_судить 1 "ОТКАЗ: нет свежего слова владельца (условие 1 из трёх)" "условие 1 из трёх" 2
случай "БОЛЬНОЙ СЛУЧАЙ: снятие прошло без слова владельца" 1 "двери нет" \
    отказ_двери_судить 0 "СНЯТИЕ ЗАВЕРШЕНО" "условие 1 из трёх" 0
случай "БОЛЬНОЙ СЛУЧАЙ: отказ есть, но причина другая" 1 "причина не названа" \
    отказ_двери_судить 1 "ОТКАЗ: docker не отвечает" "условие 1 из трёх" 2
случай "БОЛЬНОЙ СЛУЧАЙ: причина названа, а копия всё равно снята" 1 "копия всё равно снята" \
    отказ_двери_судить 1 "ОТКАЗ: нет свежего слова владельца (условие 1 из трёх)" "условие 1 из трёх" 0
}

# ── СТЕНД СНЯТИЯ: подставные каталоги, боевые не трогаются ──────────────────
# Три группы ниже стоят на ОДНОМ стенде (подставная среда, свой /etc, свой
# каталог журнала входящих), поэтому стенд заводится отдельной функцией: группа
# обязана работать и в одиночку, `--selftest --группа дверь` тоже поднимает его
# сам (ревью кода 28.09.2026, I-15).
SNYATIE_SKRIPT=""
STEND_SNYATIYA=""
STEND_PROEKT=""
STEND_ARHIV=""

# Архив стенда — НАСТОЯЩИЙ архив, а не строка текста. Прежний стенд клал
# `printf 'подставной архив стенда'` и тем УЗАКОНИВАЛ дыру C-3: условие 2
# открывалось файлом, который проверялся сам собой.
собрать_стендовый_архив() { # $1 = путь, $2 = проект в манифесте, $3 = с-compose|без-compose, $4… = имена томов
    local put="$1" proekt="$2" compose="$3"; shift 3
    local dvor_arh tom
    dvor_arh=$(mktemp -d "/tmp/${PREFIKS_PROBY}stend-arh-XXXXXX") || return 1
    mkdir -p "$dvor_arh/files" "$dvor_arh/volumes"
    # Дата в манифесте — аргумент стенда (STEND_CREATED), а не «сейчас»:
    # иначе путь «внутри архива написано, что он прошлогодний» нечем
    # создать, а именно им закрывается подделка mtime (I-2).
    {
        printf 'project=%s\n' "$proekt"
        printf 'created=%s\n' "${STEND_CREATED:-$(date --iso-8601=seconds)}"
    } > "$dvor_arh/manifest.txt"
    if [ "$compose" = с-compose ]; then
        printf 'name: %s\n' "$proekt" > "$dvor_arh/files/compose.yaml"
        printf 'file=compose.yaml\n' >> "$dvor_arh/manifest.txt"
    fi
    for tom in "$@"; do
        printf 'подставной дамп тома %s\n' "$tom" > "$dvor_arh/volumes/$tom.tar"
        printf 'volume=%s\n' "$tom" >> "$dvor_arh/manifest.txt"
    done
    # Объявление «это не данные, увозить нарочно не стал» — строкой манифеста.
    local propusk
    for propusk in ${STEND_SKIPPED:-}; do
        printf 'skipped=%s\n' "$propusk" >> "$dvor_arh/manifest.txt"
    done
    tar -C "$dvor_arh" -cf - . | zstd -q -T0 -o "$put" -f 2>/dev/null
    rm -rf "$dvor_arh"
    ( cd "$(dirname "$put")" && sha256sum "$(basename "$put")" > "$(basename "$put").sha256" )
}

# Строка МАШИННОГО журнала входящих — носителя слова владельца. Пишет её в бою
# tg-dispatcher.sh, и `message_id` в ней от телеграма: файлы `inbox/*.txt` пишет
# сам агент, и дверь на них открывалась его же текстом (ревью кода 28.09.2026, C-1).
# Внутри НЕТ ни одного блока с отступом нарочно: дважды за смену разбор функций
# по предметам съедал четыре пробела у строки внутри `if`, python падал
# IndentationError, стенд писал ПУСТОЙ журнал — и тринадцать случаев двери
# краснели с причиной «нет слова владельца», то есть указывали не туда
# (29.09.2026). Условие пишется выражением, отступа у него быть не может.
строка_журнала() { # $1 = текст, $2 = минут назад, $3 = message_id («нет» — без поля)
    python3 - "$1" "${2:-0}" "${3:-551}" <<'PY'
import datetime, json, sys
текст, минут, nomer = sys.argv[1], float(sys.argv[2]), sys.argv[3]
когда = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=минут)
запись = {"ts": когда.isoformat(), "chat_id": "1000000001", "текст": текст}
запись.update({"message_id": nomer} if nomer != "нет" else {})
print(json.dumps(запись, ensure_ascii=False))
PY
}

# Стенд проверяет СЕБЯ: сломанный писатель журнала (python упал, кавычка съедена)
# давал пустой журнал, дверь отказывала «нет слова владельца», и тринадцать
# случаев указывали на дверь вместо стенда (29.09.2026).
стенд_журнала_цел() {
    local stroka
    stroka=$(строка_журнала "проба стенда" 0 777)
    PUTEJ=$(( PUTEJ + 1 ))
    if содержит "$stroka" '"message_id": "777"' && содержит "$stroka" 'проба стенда'; then
        printf '  ок    писатель журнала входящих стенда цел: строка с message_id получена\n'
        return 0
    fi
    printf '  ПЛОХО писатель журнала входящих стенда сломан — дверь отказала бы «нет слова владельца» по вине стенда. Строка: «%s»\n' \
        "$stroka"
    НЕУДАЧА
    return 1
}

стенд_снятия() {
    SNYATIE_SKRIPT="$SREDA_KOREN/scripts/teardown.sh"
    STEND_SNYATIYA="$DVOR_STENDA/снятие"
    STEND_PROEKT="${PREFIKS_PROBY}dver-$$"
    PUTEJ=$(( PUTEJ + 1 ))
    if [ ! -f "$SNYATIE_SKRIPT" ]; then
        printf '  ПЛОХО снятия нет на месте: %s — двери проверить нечем\n' "$SNYATIE_SKRIPT"
        НЕУДАЧА
        return 1
    fi
    printf '  ок    снятие на месте: %s\n' "$SNYATIE_SKRIPT"
    стенд_журнала_цел || return 1

    mkdir -p "$STEND_SNYATIYA/среда" "$STEND_SNYATIYA/log/inbox/обработано" \
             "$STEND_SNYATIYA/nginx/sites-available" "$STEND_SNYATIYA/nginx/sites-enabled" \
             "$STEND_SNYATIYA/letsencrypt/live/$STEND_PROEKT" \
             "$STEND_SNYATIYA/letsencrypt/renewal" "$STEND_SNYATIYA/архивы"
    printf 'name: %s\n' "$STEND_PROEKT" > "$STEND_SNYATIYA/среда/compose.yaml"
    # Архив стенда — НАСТОЯЩИЙ архив, а не строка текста. Прежний стенд
    # клал `printf 'подставной архив стенда'` и тем УЗАКОНИВАЛ дыру C-3:
    # условие 2 открывалось файлом, который проверялся сам собой.
    STEND_ARHIV="$STEND_SNYATIYA/архивы/$STEND_PROEKT-20260101-000000.tar.zst"
    собрать_стендовый_архив "$STEND_ARHIV" "$STEND_PROEKT" с-compose
    return 0
}

проверка_состав_архива() {
    стенд_снятия || return 1
    local snyatie_skript="$SNYATIE_SKRIPT" stend_proekt="$STEND_PROEKT"
    local stend_snyatiya="$STEND_SNYATIYA" stend_arhiv="$STEND_ARHIV"
    # ── C-3: состав архива судится судьёй СНЯТИЯ на своих архивах ──────
    # Каждый случай создаёт СВОЁ условие: архив собирается тут же, с тем
    # изъяном, который проверяется, а не берётся боевой.
    local sud_dvor="$stend_snyatiya/судья-архива"
    mkdir -p "$sud_dvor/среда-с-лишним"
    printf 'name: %s\n' "$stend_proekt" > "$sud_dvor/среда-с-лишним/compose.yaml"
    local arh_celyj="$sud_dvor/$stend_proekt-целый.tar.zst"
    собрать_стендовый_архив "$arh_celyj" "$stend_proekt" с-compose "${stend_proekt}_dbdata"
    случай "состав архива: compose.yaml и дамп тома на месте, архив свежий — зелёный" 0 "состав сходится" \
        состав_архива_снятием "$snyatie_skript" "$arh_celyj" "$stend_proekt" "${stend_proekt}_dbdata" нет ""
    случай "состав архива: состав каталога среды сошёлся с архивом — зелёный" 0 "состав сходится" \
        состав_архива_снятием "$snyatie_skript" "$arh_celyj" "$stend_proekt" "${stend_proekt}_dbdata" \
        "$sud_dvor/среда-с-лишним" ""
    # Сценарий ревью дословно: один байт и сумма, посчитанная с него же.
    local arh_bajt="$sud_dvor/$stend_proekt-один-байт.tar.zst"
    printf 'x' > "$arh_bajt"
    ( cd "$sud_dvor" && sha256sum "$(basename "$arh_bajt")" > "$(basename "$arh_bajt").sha256" )
    случай "БОЛЬНОЙ СЛУЧАЙ: файл в один байт со СВОЕЙ суммой — это не архив" 1 "не читается zstd" \
        состав_архива_снятием "$snyatie_skript" "$arh_bajt" "$stend_proekt" "" нет ""
    local arh_bez_compose="$sud_dvor/$stend_proekt-без-compose.tar.zst"
    собрать_стендовый_архив "$arh_bez_compose" "$stend_proekt" без-compose "${stend_proekt}_dbdata"
    случай "БОЛЬНОЙ СЛУЧАЙ: в архиве нет compose.yaml — поднять среду на новом месте нечем" 1 \
        "нет files/compose.yaml" \
        состав_архива_снятием "$snyatie_skript" "$arh_bez_compose" "$stend_proekt" "${stend_proekt}_dbdata" нет ""
    local arh_chuzhoj="$sud_dvor/$stend_proekt-чужой.tar.zst"
    собрать_стендовый_архив "$arh_chuzhoj" "${PREFIKS_PROBY}chuzhaya-sreda" с-compose
    случай "БОЛЬНОЙ СЛУЧАЙ: манифест называет ДРУГОЙ проект" 1 "архив ДРУГОЙ среды" \
        состав_архива_снятием "$snyatie_skript" "$arh_chuzhoj" "$stend_proekt" "" нет ""
    случай "БОЛЬНОЙ СЛУЧАЙ: дампа тома, который будет удалён, в архиве нет" 1 "нет дампов томов" \
        состав_архива_снятием "$snyatie_skript" "$arh_celyj" "$stend_proekt" \
        "${stend_proekt}_dbdata ${stend_proekt}_photos" нет ""
    touch -d '400 days ago' "$arh_celyj"
    случай "БОЛЬНОЙ СЛУЧАЙ: архив 400 суток от роду (mtime) — перенос это не он" 1 "при пределе" \
        состав_архива_снятием "$snyatie_skript" "$arh_celyj" "$stend_proekt" "${stend_proekt}_dbdata" нет ""
    # Сценарий ревью дословно: mtime свежий (его ставит `touch`), а собран архив
    # год назад. Дату внутри архива переписать, не пересобрав его, нельзя — и
    # именно поэтому она судится наравне с mtime (I-2).
    local arh_manifest_staryj="$sud_dvor/$stend_proekt-манифест-старый.tar.zst"
    STEND_CREATED=$(date -d '400 days ago' --iso-8601=seconds) \
        собрать_стендовый_архив "$arh_manifest_staryj" "$stend_proekt" с-compose "${stend_proekt}_dbdata"
    touch "$arh_manifest_staryj"
    случай "БОЛЬНОЙ СЛУЧАЙ: touch освежил mtime, а в манифесте created год назад — не перенос" 1 "created=" \
        состав_архива_снятием "$snyatie_skript" "$arh_manifest_staryj" "$stend_proekt" \
        "${stend_proekt}_dbdata" нет ""
    собрать_стендовый_архив "$arh_celyj" "$stend_proekt" с-compose "${stend_proekt}_dbdata"
    mkdir -p "$sud_dvor/среда-с-лишним/media"
    случай "БОЛЬНОЙ СЛУЧАЙ: media/ в каталоге среды, а в архиве его нет — rm -rf унёс бы молча" 1 \
        "чего в архиве НЕТ" \
        состав_архива_снятием "$snyatie_skript" "$arh_celyj" "$stend_proekt" "${stend_proekt}_dbdata" \
        "$sud_dvor/среда-с-лишним" ""
    rm -rf "$sud_dvor/среда-с-лишним/media"
    # Тот же каталог с кешем, ОБЪЯВЛЕННЫМ в манифесте архива: дверь открывается,
    # и число объявленных имён названо — «rm -rf унесёт кеш» это решение сборщика,
    # а не молчаливая потеря (живой прогон 29.09.2026).
    local arh_s_propuskom="$sud_dvor/$stend_proekt-с-пропуском.tar.zst"
    STEND_SKIPPED=".pnpm-store" \
        собрать_стендовый_архив "$arh_s_propuskom" "$stend_proekt" с-compose "${stend_proekt}_dbdata"
    mkdir -p "$sud_dvor/среда-с-лишним/.pnpm-store"
    случай "кеш, объявленный в манифесте строкой skipped=, снятию не мешает и назван числом" 0 \
        "объявлено не данными 1 имён" \
        состав_архива_снятием "$snyatie_skript" "$arh_s_propuskom" "$stend_proekt" \
        "${stend_proekt}_dbdata" "$sud_dvor/среда-с-лишним" ""
    случай "БОЛЬНОЙ СЛУЧАЙ: тот же кеш БЕЗ объявления в манифесте — отказ ДО rm -rf" 1 "чего в архиве НЕТ" \
        состав_архива_снятием "$snyatie_skript" "$arh_celyj" "$stend_proekt" \
        "${stend_proekt}_dbdata" "$sud_dvor/среда-с-лишним" ""
    rm -rf "$sud_dvor/среда-с-лишним/.pnpm-store"
}

проверка_дверь() {
    стенд_снятия || return 1
    local snyatie_skript="$SNYATIE_SKRIPT" stend_proekt="$STEND_PROEKT"
    local stend_snyatiya="$STEND_SNYATIYA" stend_arhiv="$STEND_ARHIV"
    # Имя файла nginx спрашивается у СНЯТИЯ (оно читает его из данных
    # harness.conf), а не пишется здесь строкой: договорённость проверяющего с
    # самим собой ничего не проверяет (ревью кода 28.09.2026, I-12).
    local nginx_fajl
    nginx_fajl=$(имя_файла_nginx "$snyatie_skript" "$stend_proekt")
    PUTEJ=$(( PUTEJ + 1 ))
    if [ -n "$nginx_fajl" ]; then
        printf '  ок    имя файла nginx снятие называет само (данные ENV_NGINX_SITE): %s\n' "$nginx_fajl"
    else
        printf '  ПЛОХО снятие не назвало имя файла nginx (NGINX_FAJL) — стенд носителей завести нечем\n'
        НЕУДАЧА
        nginx_fajl="$stend_proekt"
    fi
    : > "$stend_snyatiya/nginx/sites-available/$nginx_fajl"
    ln -sf "$stend_snyatiya/nginx/sites-available/$nginx_fajl" \
           "$stend_snyatiya/nginx/sites-enabled/$nginx_fajl"
    # archive/<имя>: у certbot ключ и цепочка лежат ТАМ, а live — ссылки на них.
    mkdir -p "$stend_snyatiya/letsencrypt/archive/$stend_proekt"
    : > "$stend_snyatiya/letsencrypt/archive/$stend_proekt/privkey1.pem"
    : > "$stend_snyatiya/letsencrypt/renewal/$stend_proekt.conf"
    # Файл nginx ЧУЖОГО имени в тех же каталогах: снятие обязано оставить его.
    # Без этого «сняло по имени» и «снесло каталог подчистую» неотличимы.
    : > "$stend_snyatiya/nginx/sites-available/sosedskij-sajt"

    # Строка МАШИННОГО журнала входящих — носителя слова владельца. Пишет
    # её в бою tg-dispatcher.sh, и `message_id` в ней от телеграма: файлы
    # `inbox/*.txt` пишет сам агент, и дверь на них открывалась его же
    # текстом (ревью кода 28.09.2026, C-1).
    # Ключ стенда — ОДИН на всю подмену (ревью кода 28.09.2026, C-2): без
    # него снятие отказывается смотреть в подставной каталог, и отдельный
    # случай ниже это доказывает.
    снятие_на_стенде() { # $1 = имя проекта
        HARNESS_OWNER_WORD_STEND=1 \
        HARNESS_LOG_DIR="$stend_snyatiya/log" \
        NGINX_AVAILABLE_DIR="$stend_snyatiya/nginx/sites-available" \
        NGINX_ENABLED_DIR="$stend_snyatiya/nginx/sites-enabled" \
        LETSENCRYPT_DIR="$stend_snyatiya/letsencrypt" \
            bash "$snyatie_skript" "$1" --каталог "$stend_snyatiya/среда" \
                --архивы "$stend_snyatiya/архивы"
    }
    снятие_без_ключа_стенда() { # тот же стенд, но ключа стенда нет
        HARNESS_LOG_DIR="$stend_snyatiya/log" \
        NGINX_AVAILABLE_DIR="$stend_snyatiya/nginx/sites-available" \
        NGINX_ENABLED_DIR="$stend_snyatiya/nginx/sites-enabled" \
        LETSENCRYPT_DIR="$stend_snyatiya/letsencrypt" \
            bash "$snyatie_skript" "$1" --каталог "$stend_snyatiya/среда" \
                --архивы "$stend_snyatiya/архивы"
    }
    каталог_стенда_цел() { # печатает «цел», если подставная среда на месте
        [ -d "$stend_snyatiya/среда" ] && printf 'цел' || printf 'снят'
    }

    # 0. Подмена каталога журнала БЕЗ ключа стенда — отказ с причиной.
    #    Это и была дыра C-2: одна переменная окружения уводила дверь в
    #    каталог, созданный вызывающим.
    случай "БОЛЬНОЙ СЛУЧАЙ: подмена каталога журнала без ключа стенда — отказ" 1 \
        "HARNESS_OWNER_WORD_STEND" снятие_без_ключа_стенда "$stend_proekt"
    случай "после отказа по подмене каталог среды цел" 0 "цел" каталог_стенда_цел

    # 1а. Журнала входящих нет вовсе. «Прочитать не смогли» — дверь обязана
    #     быть ЗАКРЫТА, а не открыта: второй ответ вместо первого снимал бы
    #     среду на любой машине без харнеса.
    случай "БОЛЬНОЙ СЛУЧАЙ: журнала входящих нет — дверь закрыта, а не открыта" 1 \
        "дверь не читает слово владельца" снятие_на_стенде "$stend_proekt"
    случай "после отказа «журнала нет» каталог среды цел" 0 "цел" каталог_стенда_цел

    # 1в. Сообщения владельца ЕСТЬ, но они НЕ про снятие этой среды. Между
    #     «журнала нет» и «текст, который проба сама написала» не было ни одного
    #     такого пути — а это и есть единственное место, где распознаватель
    #     может ошибиться: на живом корпусе он ошибся раз из 1197 (ревью кода
    #     28.09.2026, I-11). Формулировки взяты живые, а не выдуманные.
    путь_похожего_слова() { # $1 = текст владельца
        строка_журнала "$1" 0 601 > "$stend_snyatiya/log/inbox.jsonl"
        случай "БОЛЬНОЙ СЛУЧАЙ: «$1» — не разрешение на снятие" 1 "условие 1 из трёх" \
            снятие_на_стенде "$stend_proekt"
    }
    путь_похожего_слова "пришли снимок среды"
    путь_похожего_слова "Лимит снят"
    путь_похожего_слова "убери из среды лишние контейнеры"
    путь_похожего_слова "Снимай среду с диска"
    случай "после отказов по похожим словам каталог среды цел" 0 "цел" каталог_стенда_цел

    # 1б. Журнал есть, но запись БЕЗ message_id: признак авторства ставит
    #     телеграм, а не агент.
    строка_журнала "Снимай среду $stend_proekt с диска" 0 нет \
        > "$stend_snyatiya/log/inbox.jsonl"
    случай "БОЛЬНОЙ СЛУЧАЙ: запись без message_id — отказ по условию 1" 1 "условие 1 из трёх" \
        снятие_на_стенде "$stend_proekt"
    случай "после отказа по условию 1 каталог среды цел" 0 "цел" каталог_стенда_цел

    # 2. Слово есть, но архив доехал не целиком.
    строка_журнала "Снимай среду $stend_proekt с диска" > "$stend_snyatiya/log/inbox.jsonl"
    printf 'архив доехал не целиком\n' >> "$stend_arhiv"
    случай "БОЛЬНОЙ СЛУЧАЙ: сумма архива не сходится — отказ по условию 2" 1 "условие 2 из трёх" \
        снятие_на_стенде "$stend_proekt"
    случай "после отказа по условию 2 каталог среды цел" 0 "цел" каталог_стенда_цел

    # 2б. Сценарий ревью целиком: файл со своей суммой ВМЕСТО архива.
    #     Раньше дверь открывалась одним байтом — сумма проверяла сама себя.
    printf 'x' > "$stend_arhiv"
    ( cd "$stend_snyatiya/архивы" && sha256sum "$(basename "$stend_arhiv")" \
        > "$(basename "$stend_arhiv").sha256" )
    случай "БОЛЬНОЙ СЛУЧАЙ: один байт со сходящейся суммой — дверь НЕ открывается" 1 "не читается zstd" \
        снятие_на_стенде "$stend_proekt"
    случай "после отказа по содержимому архива каталог среды цел" 0 "цел" каталог_стенда_цел

    # 2в. Архив настоящий, но прошлогодний: перенос это не он.
    собрать_стендовый_архив "$stend_arhiv" "$stend_proekt" с-compose
    touch -d '400 days ago' "$stend_arhiv"
    случай "БОЛЬНОЙ СЛУЧАЙ: архив 400 суток от роду — отказ по возрасту" 1 "при пределе" \
        снятие_на_стенде "$stend_proekt"

    # 2г. Архив свежий, но в каталоге среды появилось то, чего в нём нет:
    #     `rm -rf` унёс бы это молча (C-4).
    собрать_стендовый_архив "$stend_arhiv" "$stend_proekt" с-compose
    mkdir -p "$stend_snyatiya/среда/media"
    случай "БОЛЬНОЙ СЛУЧАЙ: media/ в каталоге среды мимо архива — отказ ДО rm -rf" 1 "чего в архиве НЕТ" \
        снятие_на_стенде "$stend_proekt"
    случай "после отказа по составу каталога каталог среды цел" 0 "цел" каталог_стенда_цел
    rm -rf "$stend_snyatiya/среда/media"

    # 3. Состав архива сошёлся, но о новом месте владелец не сказал.
    случай "БОЛЬНОЙ СЛУЧАЙ: нет слова о новом месте — отказ по условию 3" 1 "условие 3 из трёх" \
        снятие_на_стенде "$stend_proekt"
    случай "после отказа по условию 3 каталог среды цел" 0 "цел" каталог_стенда_цел

    # 3б. Слово о новом месте ЕСТЬ, но среда в нём не названа — такая фраза
    #     открывала условие 3 по ЭТОЙ среде (I-1). Цитата ниже, в самом случае:
    #     разорванная переносом, она не обезличивается парами публичной сборки,
    #     и имя продукта владельца уезжало наружу (замер 17.10).
    строка_журнала "продукт развернул на новом сервере, работает" 0 553 \
        >> "$stend_snyatiya/log/inbox.jsonl"
    случай "БОЛЬНОЙ СЛУЧАЙ: новое место названо, а среда — нет: отказ по условию 3" 1 "условие 3 из трёх" \
        снятие_на_стенде "$stend_proekt"
    случай "после отказа по безымянному новому месту каталог среды цел" 0 "цел" каталог_стенда_цел

    # 4. Имя проекта не то, что в compose каталога: ошибка имени стоит тома.
    случай "БОЛЬНОЙ СЛУЧАЙ: каталог принадлежит ДРУГОЙ среде — отказ до двери" 1 "ДРУГОЙ среды" \
        снятие_на_стенде "${PREFIKS_PROBY}chuzhoj-$$"

    # 5. Все три условия: снятие проходит и уносит носители без префикса.
    # Имя среды в слове о новом месте ОБЯЗАТЕЛЬНО: «развернул на новом сервере»
    # без имени открывало условие 3 любой среды (I-1).
    строка_журнала "Среда $stend_proekt поднялась на новом сервере, всё работает" 0 552 \
        >> "$stend_snyatiya/log/inbox.jsonl"
    случай "все три условия — снятие проходит" 0 "СНЯТИЕ ЗАВЕРШЕНО" \
        снятие_на_стенде "$stend_proekt"
    случай "после снятия каталога среды нет" 0 "снят" каталог_стенда_цел

    # I-4: журнал события — носитель того, ЧЕМ кончился шаг. Прежде запись была
    # только на полном успехе, то есть ровно тот случай, ради которого журнал и
    # нужен — снятие разрушило половину и не сошлось — следа не оставлял.
    local zhurnal_sobytij
    zhurnal_sobytij=$(cat "$stend_snyatiya/log/env-teardown.jsonl" 2>/dev/null)
    PUTEJ=$(( PUTEJ + 1 ))
    if содержит "$zhurnal_sobytij" '"исход":"объекты-сняты"' \
        && содержит "$zhurnal_sobytij" '"исход":"снято"'; then
        printf '  ок    журнал события снятия: есть и запись сразу после разрушительного шага, и итог\n'
    else
        printf '  ПЛОХО в журнале события снятия нет обоих исходов («объекты-сняты» и «снято»): %s\n' \
            "$(printf '%s' "$zhurnal_sobytij" | tr '\n' ';')"; НЕУДАЧА
    fi
    путь_носителя() { # $1 = имя случая, $2 = путь, который обязан исчезнуть
        PUTEJ=$(( PUTEJ + 1 ))
        if [ -e "$2" ] || [ -L "$2" ]; then
            printf '  ПЛОХО %s: %s остался\n' "$1" "$2"; НЕУДАЧА
        else
            printf '  ок    %s\n' "$1"
        fi
    }
    путь_носителя "носитель без префикса: файл nginx снят" \
        "$stend_snyatiya/nginx/sites-available/$nginx_fajl"
    путь_носителя "носитель без префикса: ссылка sites-enabled снята" \
        "$stend_snyatiya/nginx/sites-enabled/$nginx_fajl"
    путь_носителя "носитель без префикса: /etc/letsencrypt/live/<имя> снят" \
        "$stend_snyatiya/letsencrypt/live/$stend_proekt"
    # archive/<имя>: настоящие файлы ключа. Снятие одного live оставляло ключ на
    # диске и печатало «пусто … каталога нет» (ревью кода 28.09.2026, I-6).
    путь_носителя "носитель без префикса: /etc/letsencrypt/archive/<имя> снят вместе с ключом" \
        "$stend_snyatiya/letsencrypt/archive/$stend_proekt"
    путь_носителя "носитель без префикса: renewal/<имя>.conf снят" \
        "$stend_snyatiya/letsencrypt/renewal/$stend_proekt.conf"
    PUTEJ=$(( PUTEJ + 1 ))
    if [ -f "$stend_snyatiya/nginx/sites-available/sosedskij-sajt" ]; then
        printf '  ок    БОЛЬНОЙ СЛУЧАЙ: чужой файл nginx в том же каталоге снятие НЕ тронуло\n'
    else
        printf '  ПЛОХО снятие унесло чужой файл nginx sosedskij-sajt — оно снимает каталог, а не имя\n'
        НЕУДАЧА
    fi
}

проверка_счёт_снятия() {
    стенд_снятия || return 1
    local snyatie_skript="$SNYATIE_SKRIPT" stend_proekt="$STEND_PROEKT"
    # ── C-5: отказ docker в снятии не читается как «пусто» ──────────────
    сколько_снятием() { # $1 = каталог подставного docker («нет» — настоящий), $2 = вид
        local put="$PATH"
        [ "$1" = нет ] || put="$1:$PATH"
        PATH="$put" TEARDOWN_TOLKO_FUNKCII=1 bash -c '
            source "$1" "$2" >/dev/null 2>&1
            сколько "$3"' _ "$snyatie_skript" "$stend_proekt" "$2"
    }
    пустота_снятием() { # $1… = аргументы судьи пустоты
        TEARDOWN_TOLKO_FUNKCII=1 bash -c '
            source "$1" "$2" >/dev/null 2>&1
            shift 2
            пустота_судить "$@"' _ "$snyatie_skript" "$stend_proekt" "$@"
    }
    случай "счёт снятия: живой docker, объектов проекта нет — «0»" 0 "0" сколько_снятием нет тома
    случай "БОЛЬНОЙ СЛУЧАЙ: docker снятию ответил ненулевым кодом — «не прочитано»" 2 "не прочитано" \
        сколько_снятием "$STEND_DOCKER" тома
    случай "пустота снятия: четыре нуля и каталога нет — зелёная" 0 "пусто по имени проекта" \
        пустота_снятием 0 0 0 0 нет
    # Ждём СВОИ слова, а не подстроку «не прочитан»: без замка bash роняет
    # арифметику и печатает в ошибку тот же текст — мутация прошла бы
    # незамеченной (проверено мутантным прогоном 29.09.2026).
    случай "БОЛЬНОЙ СЛУЧАЙ: пустота по «не прочитано» вместо числа — НЕ доказана" 1 \
        "НЕ доказана: счёт объектов не прочитан" \
        пустота_снятием 0 "не прочитано (docker код 1)" 0 0 нет

    # ── Базовые образы: их снятие не трогает ────────────────────────────
    # Судья зовётся ИЗ самого снятия (файл подключается ради функций), а не
    # переписывается здесь копией: две копии одного правила разъезжаются.
    путь_образа() { # $1 = имя случая, $2 = ждём (да|нет), $3 = образ
        local bylo
        bylo=$(TEARDOWN_TOLKO_FUNKCII=1 bash -c '
            source "$1" "$2" >/dev/null 2>&1
            базовый_образ "$3" && echo да || echo нет' _ "$snyatie_skript" "$stend_proekt" "$3")
        PUTEJ=$(( PUTEJ + 1 ))
        if [ "$bylo" = "$2" ]; then printf '  ок    %s\n' "$1"
        else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$bylo"; НЕУДАЧА; fi
    }
    путь_образа "БОЛЬНОЙ СЛУЧАЙ: postgres:17-alpine — базовый, не трогается" да "postgres:17-alpine"
    путь_образа "БОЛЬНОЙ СЛУЧАЙ: nginx:alpine — базовый, его делит сосед" да "nginx:alpine"
    путь_образа "БОЛЬНОЙ СЛУЧАЙ: alpine:3.20 — базовый" да "alpine:3.20"
    путь_образа "БОЛЬНОЙ СЛУЧАЙ: docker.io/library/redis:7 — базовый и с реестром в имени" да \
        "docker.io/library/redis:7"
    путь_образа "образ, собранный проектом, базовым не считается" нет "app-web:latest"

    # ── Замки в тексте снятия: рядом с поведением, а не вместо него ─────
    local prune_strok filtr_bez_metki
    prune_strok=$(grep -vE '^[[:space:]]*#' "$snyatie_skript" \
        | grep -cE 'image prune|system prune|volume prune|builder prune' || true)
    PUTEJ=$(( PUTEJ + 1 ))
    if [ "$prune_strok" = 0 ]; then
        printf '  ок    в снятии нет ни одной общей уборки docker (prune) — чужое не задето\n'
    else
        printf '  ПЛОХО в снятии %s строк общей уборки docker — она бьёт по соседям\n' "$prune_strok"; НЕУДАЧА
    fi
    filtr_bez_metki=$(grep -vE '^[[:space:]]*#' "$snyatie_skript" \
        | grep -- '--filter' | grep -cv 'label=' || true)
    PUTEJ=$(( PUTEJ + 1 ))
    if [ "$filtr_bez_metki" = 0 ]; then
        printf '  ок    все фильтры снятия идут по МЕТКЕ: фильтра по имени (подстрокой) нет\n'
    else
        printf '  ПЛОХО в снятии %s фильтров без метки — «name=» сравнивает подстрокой\n' "$filtr_bez_metki"; НЕУДАЧА
    fi

    # Труба из printf в `grep -q` под pipefail отдаёт 141 при РАННЕМ совпадении:
    # на перечне архива из 5342 записей дверь отказала словами «в архиве нет
    # compose.yaml» на архиве, где он есть (живой прогон 29.09.2026). Судьи
    # переведены на герестроку, и замок текстом держит, чтобы труба не вернулась
    # молча. Комментарии отброшены: проверка судит КОД, а не прозу о нём.
    local trub_v_grep
    trub_v_grep=$( { grep -vE '^[[:space:]]*#' "$0"
                     grep -vE '^[[:space:]]*#' "$snyatie_skript"
                     grep -vE '^[[:space:]]*#' "$SREDA_KOREN/scripts/export.sh" 2>/dev/null
                   } | grep -cE "printf [^|]*\| *grep -q" || true )
    PUTEJ=$(( PUTEJ + 1 ))
    if [ "$trub_v_grep" = 0 ]; then
        printf '  ок    ни в пробе, ни в снятии, ни в сборщике нет printf, поданного ТРУБОЙ в «grep -q»: под pipefail это 141 вместо 0\n'
    else
        printf '  ПЛОХО строк с printf, поданным ТРУБОЙ в «grep -q»: %s (проба, снятие, сборщик) — под pipefail раннее совпадение даёт 141, и «нашлось» читается как «не нашлось»\n' \
            "$trub_v_grep"; НЕУДАЧА
    fi
}

# Группы: имя → функция. ДАННЫЕ порядка, а не десять вызовов подряд.
GRUPPY="порт готовность память соседи окно жнец данные перенос следы состав_архива дверь счёт_снятия"

выполнить_группу() { # $1 = имя группы
    local do_putej="$PUTEJ" do_ploho="$PLOHO"
    printf '── группа %s ──\n' "$1"
    "проверка_$1"
    printf '   итог группы %s: путей %s, неудач %s\n' \
        "$1" "$(( PUTEJ - do_putej ))" "$(( PLOHO - do_ploho ))"
}

самопроверка() { # $1 = имя группы или пусто (все)
    local grupp="${1:-}" imya nashlas=0
    DVOR_STENDA=$(mktemp -d "/tmp/${PREFIKS_PROBY}selftest-XXXXXX")
    trap 'rm -rf "${DVOR_STENDA:-/tmp/нет-такого-стенда}"' EXIT
    # Самовызовы пробы читают СВОЙ конфиг с именем продукта: на машине без
    # продукта PRODUCT_DIR пуст, и проба отказывала «данные неполны» раньше,
    # чем доходила до случая, который стенд строил.
    printf 'PRODUCT_DIR="proba"\n' > "$DVOR_STENDA/harness.conf"
    export HARNESS_CONF="$DVOR_STENDA/harness.conf"
    стенд_подставного_docker

    for imya in $GRUPPY; do
        [ -z "$grupp" ] || [ "$grupp" = "$imya" ] || continue
        nashlas=1
        выполнить_группу "$imya"
    done
    if [ "$nashlas" = 0 ]; then
        printf 'группы «%s» нет. Есть: %s\n' "$grupp" "$GRUPPY"
        return 1
    fi

    if [ "$PLOHO" = 0 ]; then
        printf 'SELFTEST: зелёный (путей %s, неудач 0; групп %s)\n' \
            "$PUTEJ" "$(printf '%s' "${grupp:-$GRUPPY}" | wc -w)"
        return 0
    fi
    printf 'SELFTEST: КРАСНЫЙ (путей %s, неудач %s)\n' "$PUTEJ" "$PLOHO"
    return 1
}


# ── РАЗБОР АРГУМЕНТОВ И ЗАПУСК ──────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --подъём|--порт|--соседи|--чужое|--память|--архив|--снятие|--selftest) REZHIM="$1" ;;
        --группа) GRUPPA="${2:?--группа без значения}"; shift ;;
        --корень) SREDA_KOREN="${2:?--корень без значения}"; shift ;;
        --данные) DANNYE="${2:?--данные без значения}"; shift ;;
        --номер-порта) PORT="${2:?--номер-порта без значения}"; shift ;;
        *) printf 'неизвестный аргумент: %s\n' "$1"; usage ;;
    esac
    shift
done

case "$REZHIM" in
    --подъём)   режим_подъём ;;
    --порт)     режим_порт ;;
    --соседи)   режим_соседи ;;
    --чужое)    режим_чужое ;;
    --память)   режим_память ;;
    --архив)    режим_архив ;;
    --снятие)   режим_снятие ;;
    --selftest) самопроверка "$GRUPPA"; exit $? ;;
    *)          printf 'режим не назван\n'; usage ;;
esac
RC=$?

if [ "$RC" = 0 ]; then
    printf 'ПРОБА ПРОЙДЕНА (%s)\n' "$REZHIM"
elif [ "$RC" = 2 ]; then
    printf 'проба не делалась: %s отложен до конца ночного окна\n' "$REZHIM"
else
    printf 'проба не прошла: %s, причина названа выше\n' "$REZHIM"
fi
exit "$RC"
