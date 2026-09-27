#!/usr/bin/env bash
# Выключатель продукта: убрать его с глаз, не потеряв ни байта.
#
# Владелец 21.09.2026: «упаковать его на этом сервере так, чтобы он не мешал
# вообще, в какой нибудь докер может быть, и убрать по дальше, выключить, чтобы
# вообще не мешал а только место на диске занимал, но когда понадобится, чтобы
# можно было легко и быстро развернуть и включить».
#
# Что значит «не мешал»: не занимал порты и память, не будил cron, не отвечал
# наружу через nginx. Что значит «легко развернуть»: одна команда, и через
# секунды всё как было.
#
# Имён продукта в этом файле НЕТ: носители перечислены данными
# (harness/config/product-power.yaml), и на другом проекте инструмент работает
# без правки кода.
#
# ЧЕГО ЭТОТ СКРИПТ НЕ ДЕЛАЕТ НИКОГДА (И-1):
#   * не удаляет тома (`docker compose down`, тем более с -v);
#   * не удаляет образы — с ними подъём занимает секунды, без них минуты сборки;
#   * не удаляет файлы: cron уезжает в соседнее имя, симлинк nginx снимается,
#     сам конфиг остаётся на месте.
# Именно тома и образы и есть «только место на диске занимал».
#
# Состояние запоминается файлом: `on` обязан поднять ровно то, что `off`
# опустил, а не «всё, что найдёт» — иначе подъём включит то, что владелец
# выключил сам.
#
#   bash scripts/product-power.sh status | off | on | --только-стенд off|on
#   bash scripts/product-power.sh --selftest
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/lib/config.sh"
konf_zagruzit

# Носители НЕ перечисляются, а НАХОДЯТСЯ по имени продукта из паспорта
# (PRODUCT_DIR). Список, записанный руками, разошёлся бы с живой машиной молча,
# а имя продукта в коде харнеса — чужое имя в общем инструменте.
PRODUCT_DIR="${PRODUCT_DIR:?пуст PRODUCT_DIR — не прочитан паспорт установки}"

# Контуры продукта = проекты docker compose, чьё имя начинается с имени
# продукта: «<продукт>» боевой, «<продукт>-*» — стенды.
proekty() {  # печатает имена проектов compose, по одному в строке
    docker ps -a --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null \
        | grep -E "^${PRODUCT_DIR}(-|$)" | sort -u
}

kontejnery_proekta() {  # $1 — имя проекта
    docker ps -a --filter "label=com.docker.compose.project=$1" \
        --format '{{.Names}}' 2>/dev/null | sort
}

# --только-стенд гоняет ПОЛНЫЙ круг «выключил → включил» на стендах, не трогая
# ни боевой контур, ни общие носители. Без такой ручки круг проверялся бы
# только на живом продукте, то есть никогда. Живой случай 21.09: сужение,
# которое сужало лишь контейнеры, уронило прод на минуту.
TOLKO_STEND=0
if [ "${1:-}" = "--только-стенд" ]; then TOLKO_STEND=1; shift; fi

KONTEJNERY=()
while IFS= read -r proekt; do
    [ -n "$proekt" ] || continue
    # Боевой контур — проект РОВНО с именем продукта; всё остальное стенды.
    if [ "$TOLKO_STEND" = 1 ] && [ "$proekt" = "$PRODUCT_DIR" ]; then continue; fi
    while IFS= read -r imya; do
        [ -n "$imya" ] && KONTEJNERY+=("$imya")
    done < <(kontejnery_proekta "$proekt")
done < <(proekty)

# Общие носители зовутся по имени продукта — так их назвала установка.
NGINX_SAJT="$(ls "${NGINX_AVAILABLE_DIR:-/etc/nginx/sites-available}" 2>/dev/null \
              | grep -m1 -E "^${PRODUCT_DIR}(-|$|\\.)" || true)"
# Дверь для пробы — как у сайта nginx и каталога копий выше. Без неё проба
# опиралась на БОЕВОЕ состояние машины: файл расписания продукта существует
# только пока продукт включён, а на выключенном он уже уехал в «.vykluchen» —
# и путь «сужение не трогает cron» не мог позеленеть никогда (провал приёмки
# 22.09.2026, красный до 26.09). Проверка обязана СОЗДАВАТЬ своё условие.
CRON_FAJL="${PRODUCT_CRON_FILE:-/etc/cron.d/$PRODUCT_DIR}"
BAZA_KONTEJNER="$(kontejnery_proekta "$PRODUCT_DIR" | grep -m1 -E 'db|postgres' || true)"
BAZA_POLZOVATEL="${PRODUCT_DB_USER:-$PRODUCT_DIR}"
BAZA_IMYA="${PRODUCT_DB_NAME:-$PRODUCT_DIR}"
# Состояние живёт в общем каталоге панели, а не в /var/lib/harness: туда
# вправе писать только единственный писатель настроек через sudo, и давать
# агенту это право значило бы дать ему право менять ЛЮБУЮ развилку, включая
# спрос владельца перед выкатом (И-2). Тот же довод у паузы работы —
# scripts/pause.py, и место у них общее не случайно.
SOSTOYANIE="${PRODUCT_POWER_STATE:-$PANEL_STATE_DIR/product-power.json}"
NGINX_ENABLED="${NGINX_ENABLED_DIR:-/etc/nginx/sites-enabled}"

say() { printf '%s\n' "$*"; }

zhivye() {  # печатает имена запущенных контейнеров продукта, по одному в строке
    local imya
    for imya in "${KONTEJNERY[@]}"; do
        [ -n "$(docker ps -q -f "name=^${imya}$" 2>/dev/null)" ] && printf '%s\n' "$imya"
    done
    return 0
}

status() {
    local zhiv
    zhiv="$(zhivye | grep -c . || true)"
    say "контейнеров продукта запущено: $zhiv из ${#KONTEJNERY[@]}"
    zhivye | sed 's/^/    /'
    say "сайт nginx $NGINX_SAJT: $([ -e "$NGINX_ENABLED/$NGINX_SAJT" ] && echo включён || echo снят)"
    say "cron $CRON_FAJL: $([ -e "$CRON_FAJL" ] && echo на месте || echo убран)"
    say "состояние выключения: $([ -s "$SOSTOYANIE" ] && cat "$SOSTOYANIE" || echo нет)"
    # Тома называются вслух ВСЕГДА: выключение не должно выглядеть удалением.
    say "тома (не трогаются): $(docker volume ls --format '{{.Name}}' 2>/dev/null \
        | grep -cE "^${PRODUCT_DIR}(-|_|$)" || true) шт."
}

# Можно ли вообще записать состояние. Проверяется ПЕРВЫМ ДЕЛОМ, до остановки.
#
# ЖИВОЙ СЛУЧАЙ 21.09.2026, 07:03. Каталог состояния принадлежит root, запись
# упала «Permission denied» — а скрипт остановил контейнеры, снял сайт, убрал
# cron и сказал «выключено». Обратно подняться было нечем: `on` честно отвечал
# «записи о выключении нет», и продукт поднимали руками. Полуобновлённое
# состояние хуже необновлённого ([[a-half-updated-tree]]): проверять право
# записи надо ДО первой необратимой строки, а не после.
mozhno_zapisat() {
    mkdir -p "$(dirname "$SOSTOYANIE")" 2>/dev/null
    local proba="$SOSTOYANIE.proba"
    if printf 'x' > "$proba" 2>/dev/null; then
        rm -f "$proba"
        return 0
    fi
    return 1
}

vykluchit() {
    if ! mozhno_zapisat; then
        say "ОТКАЗ: состояние выключения не записать ($SOSTOYANIE)."
        say "Без него подъём невозможен — не останавливаю НИЧЕГО."
        say "Починка: каталог должен быть доступен на запись, либо задайте"
        say "PRODUCT_POWER_STATE на путь, куда писать можно."
        return 1
    fi
    local bylo=()
    mapfile -t bylo < <(zhivye)
    if [ "${#bylo[@]}" = 0 ] && [ ! -e "$NGINX_ENABLED/$NGINX_SAJT" ]; then
        say "продукт уже выключен — делать нечего"
        return 0
    fi

    # Свежая копия ДО остановки: И-1 не про рекурсию, а про потерю данных, и
    # «выключено» — ровно тот момент, когда копию уже не снять привычным путём.
    # Копия снимается с БОЕВОЙ базы, поэтому при сужении её не делаем: круг на
    # стенде не должен ни писать, ни читать прод — третий случай той же
    # болезни «сужение сузило не всё» (21.09, круг на стенде снял 205 МБ с
    # прода впустую).
    if [ "$TOLKO_STEND" = 0 ] \
       && [ -n "$(docker ps -q -f "name=^${BAZA_KONTEJNER}$" 2>/dev/null)" ]; then
        local kuda="${PRODUCT_DUMP_DIR:-/var/backups/harness}/produkt-pered-vykluchenim-$(date +%Y%m%d-%H%M%S).dump"
        if docker exec -i "$BAZA_KONTEJNER" pg_dump -U "$BAZA_POLZOVATEL" "$BAZA_IMYA" > "$kuda" 2>/dev/null; then
            say "копия базы снята: $kuda ($(du -h "$kuda" | cut -f1))"
        else
            say "ОТКАЗ: копию базы снять не удалось — выключение отменено"
            rm -f "$kuda"
            return 1
        fi
    fi

    local imya
    for imya in "${bylo[@]}"; do
        docker stop "$imya" >/dev/null 2>&1 && say "остановлен $imya" \
            || say "ВНИМАНИЕ: $imya не остановился"
    done

    local snyat_sajt=0 ubran_cron=0
    # ЖИВОЙ СЛУЧАЙ 21.09.2026, 07:03. Круг гонялся «только на стенде»: список
    # контейнеров был сужен переменной, а сайт nginx и cron — ОБЩИЕ у прода и
    # стенда, и сужение их не касалось. Сайт снялся, и прод перестал отвечать
    # снаружи на минуту. Сужение обязано сужать ВСЁ, иначе слова «только
    # стенд» — неправда ([[the-probe-played-on-production]]).
    if [ "$TOLKO_STEND" = 1 ]; then
        say "прогон только по стенду — сайт nginx и cron НЕ трогаю: они общие у прода и стенда"
    elif [ -e "$NGINX_ENABLED/$NGINX_SAJT" ]; then
        # Снимается ТОЛЬКО симлинк: сам конфиг остаётся в sites-available.
        if sudo rm -f "$NGINX_ENABLED/$NGINX_SAJT" 2>/dev/null \
           && sudo nginx -t >/dev/null 2>&1 && sudo systemctl reload nginx 2>/dev/null; then
            snyat_sajt=1; say "сайт nginx снят, nginx перечитан"
        else
            say "ВНИМАНИЕ: сайт nginx снять не вышло — нужны права root"
        fi
    fi
    if [ "$TOLKO_STEND" = 0 ] && [ -e "$CRON_FAJL" ]; then
        # Файл не удаляется, а уезжает соседним именем: вернуть — одно mv.
        if sudo mv "$CRON_FAJL" "$CRON_FAJL.vykluchen" 2>/dev/null; then
            ubran_cron=1; say "cron продукта убран в $CRON_FAJL.vykluchen"
        else
            say "ВНИМАНИЕ: cron убрать не вышло — нужны права root"
        fi
    fi

    mkdir -p "$(dirname "$SOSTOYANIE")"
    printf '{"когда":"%s","контейнеры":[%s],"сайт":%d,"cron":%d}\n' \
        "$(date -Is)" \
        "$(printf '"%s",' "${bylo[@]}" | sed 's/,$//')" \
        "$snyat_sajt" "$ubran_cron" > "$SOSTOYANIE"
    say "выключено. Поднять обратно: bash scripts/product-power.sh on"
}

vkluchit() {
    if [ ! -s "$SOSTOYANIE" ]; then
        say "ОТКАЗ: записи о выключении нет — не знаю, что поднимать."
        say "Продукт мог быть выключен не мной; подниму только то, что сам опускал."
        return 1
    fi
    local imena sajt cron_bylo
    imena="$(python3 -c "
import json, sys
з = json.load(open(sys.argv[1], encoding='utf-8'))
print(' '.join(з.get('контейнеры') or []))
" "$SOSTOYANIE" 2>/dev/null)"
    sajt="$(python3 -c "
import json, sys
print(json.load(open(sys.argv[1], encoding='utf-8')).get('сайт', 0))
" "$SOSTOYANIE" 2>/dev/null)"
    cron_bylo="$(python3 -c "
import json, sys
print(json.load(open(sys.argv[1], encoding='utf-8')).get('cron', 0))
" "$SOSTOYANIE" 2>/dev/null)"

    local imya
    for imya in $imena; do
        docker start "$imya" >/dev/null 2>&1 && say "поднят $imya" \
            || say "ВНИМАНИЕ: $imya не поднялся"
    done
    [ "$cron_bylo" = "1" ] && sudo mv "$CRON_FAJL.vykluchen" "$CRON_FAJL" 2>/dev/null \
        && say "cron продукта возвращён"
    if [ "$sajt" = "1" ]; then
        sudo ln -sfn "/etc/nginx/sites-available/$NGINX_SAJT" "$NGINX_ENABLED/$NGINX_SAJT" 2>/dev/null \
            && sudo nginx -t >/dev/null 2>&1 && sudo systemctl reload nginx 2>/dev/null \
            && say "сайт nginx возвращён"
    fi
    rm -f "$SOSTOYANIE"
    say "включено. Проверить: bash scripts/product-power.sh status"
}

samotest() {
    local ok=1 T
    T=$(mktemp -d); trap 'rm -rf "$T"' RETURN

    # БОЛЬНОЙ СЛУЧАЙ: подъём без записи о выключении. Раньше такой скрипт
    # поднял бы «всё, что найдёт», то есть включил бы и то, что владелец
    # выключил своей рукой.
    local vyvod
    vyvod=$(PRODUCT_POWER_STATE="$T/нет.json" bash "$0" on 2>&1); local rc=$?
    if [ "$rc" != 0 ] && printf '%s' "$vyvod" | grep -q "записи о выключении нет"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: подъём без записи — отказ с причиной, а не «подниму всё»"
    else
        echo "  ПЛОХО подъём без записи прошёл: код $rc, ответ: ${vyvod:0:90}"; ok=0
    fi

    # БОЛЬНОЙ СЛУЧАЙ: запись есть, но пустая — тот же отказ, не «ничего не делать молча».
    : > "$T/пусто.json"
    vyvod=$(PRODUCT_POWER_STATE="$T/пусто.json" bash "$0" on 2>&1); rc=$?
    if [ "$rc" != 0 ]; then
        echo "  ок    пустая запись — отказ, а не молчаливый успех"
    else
        echo "  ПЛОХО пустая запись прошла как успех"; ok=0
    fi

    # Состояние читается и печатается: status не должен падать ни при какой
    # записи, иначе владелец останется без ответа на простой вопрос.
    printf '{"когда":"x","контейнеры":["a"],"сайт":1,"cron":0}\n' > "$T/есть.json"
    if PRODUCT_POWER_STATE="$T/есть.json" bash "$0" status >/dev/null 2>&1; then
        echo "  ок    status читает запись и не падает"
    else
        echo "  ПЛОХО status упал на нормальной записи"; ok=0
    fi
    if PRODUCT_POWER_STATE="$T/битая.json" bash -c "printf 'не json' > $T/битая.json; bash $0 status" >/dev/null 2>&1; then
        echo "  ок    битая запись не роняет status"
    else
        echo "  ПЛОХО битая запись уронила status"; ok=0
    fi

    # Список контейнеров — ДАННЫЕ в одном месте: расхождение списка и кода
    # означало бы, что часть продукта остаётся жить после «выключено».
    # Полный список судится по ВЫВОДУ: владельцу видна именно эта строка, а
    # внутренности проверять незачем. Прогон без сужения — отдельной оболочкой.
    local vyvod_polnyj
    vyvod_polnyj=$(PRODUCT_POWER_STATE="$T/нет.json" bash "$0" status 2>&1)
    if printf '%s' "$vyvod_polnyj" | grep -q "из ${#KONTEJNERY[@]}"; then
        echo "  ок    полный список контейнеров продукта на месте (${#KONTEJNERY[@]})"
    else
        echo "  ПЛОХО список контейнеров не сошёлся: ${vyvod_polnyj%%$'\n'*}"; ok=0
    fi
    # Ручка «--только-стенд» обязана работать: иначе круг «выключил → включил»
    # негде прогнать, кроме прода. Судим по ВЫВОДУ: контейнеров должно стать
    # столько, сколько их в стенде, а не во всём продукте.
    # Сколько контейнеров у стендов — считается ТЕМ ЖЕ способом, которым их
    # находит боевой путь: свой счёт разошёлся бы с ним молча.
    local skolko_stenda=0 proekt
    while IFS= read -r proekt; do
        [ -n "$proekt" ] && [ "$proekt" != "$PRODUCT_DIR" ] \
            && skolko_stenda=$(( skolko_stenda + $(kontejnery_proekta "$proekt" | grep -c .) ))
    done < <(proekty)
    local vyvod_stenda
    vyvod_stenda=$(PRODUCT_POWER_STATE="$T/нет.json" bash "$0" --только-стенд status 2>&1)
    if printf '%s' "$vyvod_stenda" | grep -q "из $skolko_stenda"; then
        echo "  ок    «--только-стенд» сужает список до стенда ($skolko_stenda)"
    else
        echo "  ПЛОХО сузить список нечем: круг проверяется только на проде"; ok=0
    fi

    # Главный запрет: ни одной КОМАНДЫ, удаляющей данные. Суд по тексту слеп к
    # соседям, поэтому комментарии отброшены — первая же редакция покраснела на
    # собственном объяснении запрета ([[a-text-judging-probe-is-blind-to-neighbours]]).
    # Проба неполная и это сказано вслух: она ловит явное имя команды, а не
    # всякий мыслимый способ удалить том.
    if sed 's/#.*//' "$0" | grep -qE 'docker[[:space:]]+(compose[[:space:]]+)?down|volume[[:space:]]+rm|rmi'; then
        echo "  ПЛОХО в скрипте есть команда, удаляющая тома или образы — это И-1"; ok=0
    else
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: ни одной команды, удаляющей тома или образы"
    fi

    # ЖИВОЙ БОЛЬНОЙ СЛУЧАЙ 21.09 07:03: состояние не записалось, а выключение
    # прошло. Проба создаёт условие — каталог, в который нельзя писать.
    local zapret="$T/zapret"
    mkdir -p "$zapret" && chmod 500 "$zapret"
    vyvod=$(PRODUCT_POWER_STATE="$zapret/состояние.json" bash "$0" off 2>&1); rc=$?
    if [ "$rc" != 0 ] && printf '%s' "$vyvod" | grep -q "не останавливаю НИЧЕГО"; then
        echo "  ок    ЖИВОЙ БОЛЬНОЙ 21.09: состояние не записать — отказ ДО остановки"
    else
        echo "  ПЛОХО состояние не записать, а выключение пошло: код $rc"; ok=0
    fi
    chmod 700 "$zapret"

    # ЖИВОЙ БОЛЬНОЙ СЛУЧАЙ 21.09 07:03: сужение до стенда не сузило сайт nginx,
    # общий с продом, и прод перестал отвечать снаружи.
    # Третий носитель того же правила: при сужении копия прода не снимается.
    vyvod=$(PRODUCT_POWER_STATE="$T/суж.json" PRODUCT_DUMP_DIR="$T" \
            bash "$0" --только-стенд off 2>&1)
    PRODUCT_POWER_STATE="$T/суж.json" bash "$0" --только-стенд on >/dev/null 2>&1
    if ! printf '%s' "$vyvod" | grep -q "копия базы снята"; then
        echo "  ок    при сужении копия БОЕВОЙ базы не снимается"
    else
        echo "  ПЛОХО круг на стенде снял копию прода"; ok=0
    fi

    # Судим ПОВЕДЕНИЕ, а не текст: тот же прогон со сужением обязан сказать
    # вслух, что общие носители не тронуты, и оставить cron на месте.
    # Условие проба создаёт САМА: свой файл расписания во временном каталоге.
    # Прежняя редакция смотрела на боевой /etc/cron.d, который на выключенном
    # продукте уже переименован, — и красила приёмку четверо суток.
    # Стенд ЦЕЛИКОМ свой: файл расписания, каталоги сайтов и включённый сайт.
    # Без включённого сайта прогон выходит первой же веткой («продукт уже
    # выключен»), до строки про общие носители не доходит, и проба судила бы
    # НИЧЕГО — зелёный от бездействия. Здесь продукт «включён», и сужение
    # обязано доказать, что общего оно не тронуло.
    STEND_CRON="$T/cron-продукта"
    : > "$STEND_CRON"
    mkdir -p "$T/sites-available" "$T/sites-enabled"
    : > "$T/sites-available/$PRODUCT_DIR"
    : > "$T/sites-enabled/$PRODUCT_DIR"
    vyvod=$(PRODUCT_POWER_STATE="$T/суж2.json" PRODUCT_DUMP_DIR="$T" \
            PRODUCT_CRON_FILE="$STEND_CRON" \
            NGINX_AVAILABLE_DIR="$T/sites-available" \
            NGINX_ENABLED_DIR="$T/sites-enabled" \
            bash "$0" --только-стенд off 2>&1)
    if printf '%s' "$vyvod" | grep -q "сайт nginx и cron НЕ трогаю" \
       && [ -e "$STEND_CRON" ] && [ -e "$T/sites-enabled/$PRODUCT_DIR" ]; then
        echo "  ок    ЖИВОЙ БОЛЬНОЙ 21.09: сужение сужает и сайт, и cron, а не только контейнеры"
    else
        echo "  ПЛОХО сужение не касается сайта или cron — «только стенд» будет неправдой"; ok=0
    fi

    [ "$ok" = 1 ] && { echo "САМОТЕСТ ПРОЙДЕН: 10 путей, из них два — живые больные случаи 21.09"; return 0; }
    echo "САМОТЕСТ ПРОВАЛЕН"; return 1
}

case "${1:-status}" in
    status)     status ;;
    off)        vykluchit ;;
    on)         vkluchit ;;
    --selftest) samotest ;;
    *) say "что делать: status | off | on | --selftest"; exit 2 ;;
esac
