#!/usr/bin/env bash
#
# restore-probe.sh — поднять свежий дамп прода в ОТДЕЛЬНОЙ базе и
# сверить числа. Отвечает на вопрос, на который «дамп снят» не отвечает:
# встанем ли мы из него.
#
# Замечание владельца 10.09.2026 (П-2 к ревизии): И-1 держался на вере —
# дамп проверялся на «файл не пустой», восстановление не пробовали ни разу.
# Первая живая проба: 204 МБ поднялись за 19 с, 0 ошибок, 48 таблиц.
#
# Имя базы — с префиксом DROPPABLE_DB_PREFIX: только такие умеет убирать
# штатная дверь scripts/drop-database.sh, и только их сторож даёт удалить.
# Данные прода и стенда не трогаются: заливка идёт в НОВУЮ базу.
#
# Запуск: bash scripts/restore-probe.sh [дамп]
#         (без аргумента берётся самый свежий db-*.dump из $BACKUP_DIR)
set -uo pipefail
# Ручной прогон не должен будить владельца: --без-отправки печатает вердикт в
# stdout и дверь тревоги не зовёт. Флаг снимается из аргументов до разбора дампа.
NO_SEND=0; _args=()
for _a in "$@"; do
    if [ "$_a" = "--без-отправки" ]; then NO_SEND=1; else _args+=("$_a"); fi
done
set -- ${_args[@]+"${_args[@]}"}
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/config.sh"
konf_zagruzit
: "${BACKUP_DIR:?пуст BACKUP_DIR}" "${LOG_DIR:?пуст LOG_DIR}"
# Проверять нечего, если в каталоге копий НЕТ ДАМПОВ. Судится явление, а не
# ключ конфига: ревью кода 28.09.2026 (C-3) показало, что при пустом
# BACKUP_DBS проба молчала бы зелёным даже с дампами на диске — единственное
# живое доказательство И-1 выключалось бы одной строкой конфига.
# Самопроверка исключена явно: ранний выход убил бы её, а идти она обязана
# при любом конфиге (поймано живым прогоном 28.09.2026).
if [ "${1:-}" != "--selftest" ]; then
    EST_DAMP=$(find "$BACKUP_DIR" -name 'db-*.dump' -type f 2>/dev/null | head -1)
    if [ -z "$EST_DAMP" ]; then
        # Расхождение ключа и диска называется вслух: «базы снимаются, а
        # дампов нет» — это поломка копий, а не тишина.
        if [ -n "${BACKUP_DBS:-}" ]; then
            echo "ВНИМАНИЕ: BACKUP_DBS задан, а дампов баз в $BACKUP_DIR нет — копии не снимаются"
        fi
        echo "проба восстановления не применима: дампов баз в $BACKUP_DIR нет"
        mkdir -p "${HEARTBEAT_DIR}" 2>/dev/null || true
        # В метке лежит ВЕРДИКТ, а не пустота: «проба ходила» и «проверять
        # было что» — разные вопросы, и сводка обязана их различать.
        printf 'неприменимо: дампов баз нет (%s)\n' "$(date '+%Y-%m-%d %H:%M:%S')" \
            > "${HEARTBEAT_DIR}/restore-probe" 2>/dev/null || true
        exit 0
    fi
fi
# Префикс ОБЯЗАТЕЛЕН и берётся из конфига: своё умолчание («проба_») не совпало
# бы с тем, что требует штатная дверь drop-database.sh, и база стала бы вечной
# (ревью 10.09.2026).
: "${DROPPABLE_DB_PREFIX:?в harness.conf пуст DROPPABLE_DB_PREFIX — имя пробной базы задать нечем}"
PREFIX="$DROPPABLE_DB_PREFIX"
# Контейнер и пользователь: ПУСТО — законное значение, а не поломка. Пустой
# ключ означает «контейнера продукта нет», и проба поднимает свой одноразовый
# (ветка нужен_свой_контейнер ниже). Вшитого имени продукта здесь по-прежнему
# нет: на чужой установке ключ либо назван, либо пуст.
CONT="${PROBE_DB_CONTAINER:-}"
USER_DB="${PROBE_DB_USER:-postgres}"

# Дамп, уводящий заливку в ЧУЖУЮ базу. `psql < dump` исполняет метакоманды: одна
# строка «\connect app» — и 204 МБ ложатся не в пробную базу, а в ту, что названа
# в дампе. Сегодня дампы снимаются без --create, но BACKUP_DBS меняется строкой
# конфига, и тогда проба сама затрёт живые данные (ревью 10.09.2026, sec-09).
# Признак читается ЗАРАНЕЕ и отдельно от заливки — отказ должен случиться до
# создания базы, а не после первой залитой таблицы.
метакоманды_в_дампе() { # $1=путь дампа → печатает найденные строки, rc=0 если нашлись
    grep -m3 -n -E "^\\\\(connect|c)([[:space:]]|$)|^(CREATE|DROP) DATABASE" "$1" 2>/dev/null
}

# Нужен ли СВОЙ контейнер: названного в конфиге нет или он не запущен.
#
# Замер 28.09.2026: продукт выключен владельцем с 21.09, контейнера его базы
# (имя из PROBE_DB_CONTAINER) нет, и проба отказывала на первом шаге — то есть И-1
# держался на прогоне от 10.09 и старел молча. Проба обязана уметь работать без
# продукта: копии продукта существуют и тогда, когда сам продукт погашен.
# Само РЕШЕНИЕ отделено от вопроса к docker: иначе его нельзя судить пробой без
# живого контейнера, и мутация «всегда поднимать свой» оставалась зелёной
# (ревью 28.09.2026, F-03/M3). Ответ docker приходит аргументом.
свой_нужен_по_ответу() { # $1 = имя из конфига, $2 = ответ docker inspect → rc 0, если свой нужен
    [ -n "${1:-}" ] || return 0
    [ "${2:-}" = "true" ] && return 1
    return 0
}

нужен_свой_контейнер() { # $1 = имя контейнера из конфига
    свой_нужен_по_ответу "${1:-}" \
        "$(docker inspect -f '{{.State.Running}}' "${1:-}" 2>/dev/null | head -1)"
}

# Роли создаются ТОЛЬКО в своём одноразовом контейнере: в боевом это правка
# боевой СУБД, на что проба права не имеет. Решение вынесено в функцию, потому
# что мутация «снять замок» проходила все пробы (ревью 28.09.2026, F-03/M5).
роли_создавать() { # $1 = свой ли контейнер (1|0)
    [ "${1:-0}" = 1 ]
}

# Маркер завершения инициализации базы. Печатает его вход официального образа и
# только при ПЕРВОМ старте; образ — данные (PROBE_DB_IMAGE), поэтому отсутствие
# маркера должно быть НАЗВАНО, а не принято за готовность (ревью 28.09, F-05).
маркер_готовности() { # $1 = текст журнала контейнера → rc 0, если маркер есть
    printf '%s' "${1:-}" | grep -q "ready for start up"
}

# Жнец остатков прерванных прогонов: трап не срабатывает ни на SIGKILL (OOM на
# заливке 205 МБ), ни на перезагрузке машины, а `docker container prune` берёт
# только остановленные и томов не трогает вовсе. Убранное НАЗЫВАЕТСЯ числом:
# молча подобранная утечка неотличима от её отсутствия.
убрать_прошлые_прогоны() { # $1 = имя тома текущего прогона (не трогать)
    local svoj_tom="${1:-}" staryj ubrano_kont=0 ubrano_tom=0
    while IFS= read -r staryj; do
        [ -n "$staryj" ] || continue
        docker rm -fv "$staryj" >/dev/null 2>&1 && ubrano_kont=$((ubrano_kont + 1))
    done < <(docker ps -aq --filter "name=harness-restore-proba-" 2>/dev/null)
    while IFS= read -r staryj; do
        [ -n "$staryj" ] || continue
        [ "$staryj" = "$svoj_tom" ] && continue
        docker volume rm -f "$staryj" >/dev/null 2>&1 && ubrano_tom=$((ubrano_tom + 1))
    done < <(docker volume ls -q --filter "name=harness-restore-proba-" 2>/dev/null)
    printf 'жнец прошлых прогонов: контейнеров убрано %s, томов %s\n' "$ubrano_kont" "$ubrano_tom"
}

# Роли, которых дамп требует: владельцы, получатели и отзываемые права, члены
# групп. Имя роли берётся ИЗ ДАМПА: своё умолчание было бы именем продукта, а
# его в коде харнеса быть не должно.
#
# Признак привязан к НАЧАЛУ строки DDL — как у соседа метакоманды_в_дампе и по
# той же причине: без привязки строка ДАННЫХ внутри COPY со словами «OWNER TO
# zloy» заводила роль в контейнере пробы. Роль в дампе названа не только через
# OWNER TO: GRANT … TO, REVOKE … FROM и ALTER DEFAULT PRIVILEGES FOR ROLE дают
# ~125 «role does not exist» на здоровом дампе. PUBLIC — слово SQL, не роль.
# Имя берётся после ПОСЛЕДНЕГО TO/FROM строки: pg_dump пишет по одной роли на
# оператор, а WITH GRANT OPTION стоит после имени и в класс имени не попадает.
роли_в_дампе() { # $1 = путь дампа
    local name_re='"?([A-Za-z0-9_-]+)"?'
    {
        grep -ohE '^(ALTER|CREATE|GRANT)[^;]* OWNER TO "?[A-Za-z0-9_-]+"?' "$1" \
            | sed -E "s/.* OWNER TO $name_re/\1/"
        grep -ohE '^(GRANT|REVOKE|ALTER DEFAULT PRIVILEGES)[^;]* (TO|FROM) "?[A-Za-z0-9_-]+"?' "$1" \
            | sed -E "s/.* (TO|FROM) $name_re/\2/"
        grep -ohE '^ALTER DEFAULT PRIVILEGES FOR ROLE "?[A-Za-z0-9_-]+"?' "$1" \
            | sed -E "s/.* FOR ROLE $name_re/\1/"
        # Членство «GRANT роль TO роль» — без ON: первое слово тоже роль.
        grep -ohE '^GRANT "?[A-Za-z0-9_-]+"? TO ' "$1" \
            | sed -E "s/^GRANT $name_re TO /\1/"
    } 2>/dev/null | grep -vx 'PUBLIC' | LC_ALL=C sort -u
}

# Образ своего контейнера под версию pg_dump, снявшего дамп. Дамп pg_dump 17 в
# сервере 16 не заливается («transaction_timeout», «\restrict»), а pg_dump не
# снимает сервер новее себя — значит, ориентир именно его версия из шапки.
# Шапка ищется в начале файла: дамп бывает в сотни МБ, а без шапки читать его
# весь ради отказа незачем.
image_for_dump() { # $1 = дамп, $2 = образ по умолчанию, $3 = шаблон с {major}
    local major
    major=$(head -n 30 "$1" 2>/dev/null | grep -m1 -oE '^-- Dumped by pg_dump version [0-9]+' \
        | grep -oE '[0-9]+$')
    if [ -n "$major" ] && [ -n "${3:-}" ]; then
        printf '%s\n' "${3//\{major\}/$major}"
    else
        printf '%s\n' "$2"
    fi
}

# Тег -3.6: в 17-3.5 сервер 17.5, и дамп pg_dump 17.11 с «\restrict» в нём не
# заливается. Шаблон образа: НЕЗАДАННЫЙ ключ даёт умолчание, ПУСТОЙ — выключает подбор.
# Живой /etc/harness/harness.conf ключа не имеет, и без умолчания в коде правка
# не подействовала бы на боевом прогоне до развёртывания конфига.
PROBE_IMAGE_TEMPLATE_DEFAULT='postgis/postgis:{major}-3.6'
image_template() {
    printf '%s\n' "${PROBE_DB_IMAGE_TEMPLATE-$PROBE_IMAGE_TEMPLATE_DEFAULT}"
}

# Контейнер, из которого снят дамп: имя берётся из команды дампа в BACKUP_DBS
# («docker exec [опции] КОНТЕЙНЕР pg_dump …») по имени дампа db-ИМЯ.dump, иначе —
# PROBE_DB_CONTAINER. Движок и образ продукта — данные установки, а не код пробы.
container_from_command() { # $1 = команда дампа → печатает имя контейнера, rc 1 если не docker exec
    local -a w; local t prev="" seen=0 skip=0
    read -ra w <<< "$1"
    for t in "${w[@]}"; do
        if (( skip )); then skip=0; prev=$t; continue; fi
        if (( ! seen )); then
            [ "$t" = exec ] && [ "$prev" = docker ] && seen=1
            prev=$t; continue
        fi
        case "$t" in
            -e|-u|-w|--env|--user|--workdir|--env-file) skip=1 ;;
            -*) ;;
            *) printf '%s\n' "$t"; return 0 ;;
        esac
    done
    return 1
}

dump_source_container() { # $1 = путь дампа
    local base entry cmd
    base=$(basename "$1"); base=${base#db-}; base=${base%.dump}
    while IFS= read -r entry; do
        entry=$(printf '%s' "$entry" | sed 's/^ *//;s/ *$//')
        [ "${entry%%=*}" = "$base" ] || continue
        cmd="${entry#*=}"
        container_from_command "$cmd" && return 0
    done <<< "${BACKUP_DBS:-}"
    [ -n "${PROBE_DB_CONTAINER:-}" ] && printf '%s\n' "$PROBE_DB_CONTAINER"
}

# Как запущен контейнер-источник: образ, переменные, команда. Переносится БЕЛЫЙ
# СПИСОК переменных запуска базы: чёрный список по именам пропускал бы
# DATABASE_URL с паролем в URL, *_PWD, *_DSN. Пароль стенда свой. Из команды
# берётся только `postgres -c имя=значение` без путей: `config_file=/etc/…`
# указывает на файл, примонтированный в продукт, а в стенде его нет.
# Печатает строки «I<TAB>образ», «E<TAB>NAME=VALUE», «C<TAB>аргумент команды».
source_run_profile() { # $1 = имя контейнера-источника → rc 1, если inspect не ответил
    docker inspect "$1" 2>/dev/null | python3 -c '
import json, re, sys
try:
    c = json.load(sys.stdin)[0]["Config"]
    print("I\t" + c["Image"])
except Exception:
    sys.exit(1)
allowed = re.compile(r"(POSTGRES_(USER|DB|INITDB_ARGS|INITDB_WALDIR|HOST_AUTH_METHOD)|PGDATA|LANG|LC_[A-Z_]+|TZ)$")
for e in c.get("Env") or []:
    if allowed.match(e.split("=", 1)[0]):
        print("E\t" + e)
cmd = c.get("Cmd") or []
if cmd[:1] == ["postgres"]:
    print("C\tpostgres")
    for i, a in enumerate(cmd[1:-1], 1):
        v = cmd[i + 1]
        if a == "-c" and "=" in v and "/" not in v:
            print("C\t-c\nC\t" + v)
'
}

# Текст тревоги владельцу — по виду исхода; пусто = молчать. «Не применима» —
# решение владельца (дампов нет), тревога о нём стала бы шумом. Без кодов
# инвариантов и путей: владелец их не открывает.
alert_message() { # $1 = вид (not_restored|not_run|not_applicable|restored), $2 = ошибок, $3 = таблиц, $4 = причина
    case "${1:-}" in
        not_restored)
            printf 'Проверка резервной копии: база из свежей копии НЕ восстановилась целиком. Ошибок при заливке: %s, таблиц поднялось: %s. Пока это не разобрано, копии нельзя считать годными. Подробности в журнале проверки; управляющий разберёт причину.\n' \
                "${2:-?}" "${3:-0}" ;;
        not_run)
            printf 'Проверка резервной копии не проведена: %s. Годна ли копия, пока неизвестно; проверка повторится по расписанию, а причину надо устранить сейчас.\n' \
                "${4:-причина не названа}" ;;
    esac
}

alert_owner() { # те же доводы, что у alert_message
    local msg sender="${RESTORE_PROBE_SENDER:-$PROJECT_DIR/scripts/tg_send.sh}"
    msg=$(alert_message "$@")
    [ -n "$msg" ] || return 0
    if [ "$NO_SEND" = 1 ]; then
        printf 'ВЕРДИКТ (без отправки): %s\n' "$msg"
        return 0
    fi
    # --task: за вестью есть работа модели (разобрать причину), карточка по ключу.
    "$sender" "$msg" --task restore-probe:failed >/dev/null 2>&1 && return 0
    echo "проба: тревога владельцу не ушла — дверь отказала"
    return 1
}

# Выход «ПРОБА НЕ ПРОВЕДЕНА»: строка в журнал и тревога. Отказ двери выход не
# меняет — проба и так завершается отказом.
abort_not_run() { # $1 = причина
    echo "$(date '+%Y-%m-%d %H:%M:%S')  ПРОБА НЕ ПРОВЕДЕНА: $1"
    alert_owner not_run "" "" "$1" || true
    exit 1
}

# Пароль одноразовой базы: генерируется, в секреты не ходит — боевой пароль в
# командной строке контейнера был бы лишним путём утечки. Что этот пароль всё же
# видно: `docker inspect` контейнера, его /proc/<pid>/environ и argv `docker run`
# в `ps`. Для одноразового контейнера без публикации порта это принято.
пароль_пробы() {
    tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24
}

if [ "${1:-}" = "--selftest" ]; then
    ok=1
    # Фальшивая дверь тревоги ставится ПЕРВОЙ и наследуется всеми вложенными
    # прогонами: боевой tg_send.sh из самопроверки не зовётся никогда — иначе
    # каждый прогон ворот слал бы владельцу ложную тревогу.
    dvor_trevogi=$(mktemp -d)
    printf '#!/usr/bin/env bash\nprintf "=== вызов\\n%%s\\n" "$*" >> "%s/door.log"\n' \
        "$dvor_trevogi" > "$dvor_trevogi/sender.sh"
    printf '#!/usr/bin/env bash\nexit 1\n' > "$dvor_trevogi/sender-broken.sh"
    chmod +x "$dvor_trevogi/sender.sh" "$dvor_trevogi/sender-broken.sh"
    export RESTORE_PROBE_SENDER="$dvor_trevogi/sender.sh"
    door_calls() { cat "$dvor_trevogi/door.log" 2>/dev/null | grep -c '^=== вызов'; }
    проба() { # $1=ждём(да|нет) $2=содержимое дампа $3=имя случая
        local f; f=$(mktemp)
        printf '%s\n' "$2" > "$f"
        local got=нет
        метакоманды_в_дампе "$f" >/dev/null 2>&1 && got=да
        rm -f "$f"
        if [ "$got" = "$1" ]; then printf '  ок    %s\n' "$3"
        else printf '  ПЛОХО %s: ждали «%s», получили «%s»\n' "$3" "$1" "$got"; ok=0; fi
    }
    # БОЛЬНОЙ СЛУЧАЙ: дамп с --create переключает базу посреди заливки.
    проба да  'CREATE DATABASE app;'"$(printf '\n')"'\connect app'"$(printf '\n')"'COPY x FROM stdin;' \
        "БОЛЬНОЙ СЛУЧАЙ: дамп с --create уводит заливку в чужую базу"
    проба да  '\c app' "короткая форма метакоманды \\c"
    проба да  'DROP DATABASE app;' "снос базы в теле дампа"
    проба нет 'COPY public.дома (id) FROM stdin;'"$(printf '\n')"'1' \
        "обычный дамп таблиц — метакоманд нет"
    проба нет "-- \\connect упомянут в комментарии" "упоминание в комментарии — не команда"
    проба нет "SELECT 'CREATE DATABASE x';" "текст внутри значения — не команда"
    # ── БОЛЬНОЙ СЛУЧАЙ 28.09.2026 (ревизия харнеса, Ось В) ───────────────────
    # Проба требовала ЖИВОЙ контейнер продукта (PROBE_DB_CONTAINER). Продукт
    # выключен владельцем с 21.09, контейнера нет — и проба отказывала бы каждый
    # прогон: «ОТКАЗ: базу не создать». То есть И-1 («данные целы») не был
    # доказан ни разу с 10.09, а расписание раз в месяц это скрывало.
    if нужен_свой_контейнер "нет-такого-контейнера-$$"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: контейнера продукта нет — проба поднимет свой"
    else echo "  ПЛОХО без контейнера продукта проба снова просто откажет"; ok=0; fi
    # ── БОЛЬНОЙ СЛУЧАЙ 28.09.2026: баз в копиях нет вовсе ───────────────────
    # Владелец снял продукт с сервера целиком, BACKUP_DBS опустел. Прежняя
    # редакция падала на «пуст PROBE_DB_CONTAINER» — присмотр краснел бы
    # еженедельно на здоровой машине. Проба СОЗДАЁТ условие: свой конфиг и
    # свой каталог меток, боевые не трогаются.
    dvor_bez_baz=$(mktemp -d)
    mkdir -p "$dvor_bez_baz/kopii"
    printf 'BACKUP_DIR="%s/kopii"\nLOG_DIR="%s"\nBACKUP_DBS=""\nDROPPABLE_DB_PREFIX=""\nPROBE_DB_CONTAINER=""\nPROBE_DB_USER=""\nHEARTBEAT_DIR="%s/metki"\n' \
        "$dvor_bez_baz" "$dvor_bez_baz" "$dvor_bez_baz" > "$dvor_bez_baz/harness.conf"
    vyvod_bez_baz=$(HARNESS_CONF="$dvor_bez_baz/harness.conf" HEARTBEAT_DIR="$dvor_bez_baz/metki" \
        bash "$0" 2>&1); rc_bez_baz=$?
    if [ "$rc_bez_baz" = 0 ] && printf '%s' "$vyvod_bez_baz" | grep -q "не применима"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: баз в копиях нет — проба говорит «не применима», а не отказывает"
    else
        echo "  ПЛОХО без баз в копиях проба вернула $rc_bez_baz: «$vyvod_bez_baz»"; ok=0
    fi
    if [ -f "$dvor_bez_baz/metki/restore-probe" ]; then
        echo "  ок    метка присмотра поставлена и при неприменимости — молчание не путается со смертью"
    else echo "  ПЛОХО метки нет: присмотр решит, что проба умерла"; ok=0; fi
    # Неприменимость — не отказ: дампов нет по решению владельца, и тревога
    # раз в неделю о том, что он сам решил, стала бы шумом.
    if [ "$(door_calls)" = 0 ]; then
        echo "  ок    дампов нет — владельца не тревожим (живой прогон, дверь не звали)"
    else echo "  ПЛОХО «не применима» ушло владельцу тревогой: $(door_calls) вызовов"; ok=0; fi
    # БОЛЬНОЙ СЛУЧАЙ: ключ пуст, а дамп на диске ЛЕЖИТ. Судить надо явление:
    # прежняя редакция вышла бы нулём и не проверила бы ничего (ревью кода C-3).
    printf 'дамп' > "$dvor_bez_baz/kopii/db-lavka.dump"
    vyvod_s_dampom=$(HARNESS_CONF="$dvor_bez_baz/harness.conf" \
        HEARTBEAT_DIR="$dvor_bez_baz/metki" bash "$0" 2>&1)
    if printf '%s' "$vyvod_s_dampom" | grep -q "не применима"; then
        echo "  ПЛОХО дамп на диске есть, а проба назвала себя неприменимой"; ok=0
    else
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: дамп на диске есть — проба идёт проверять, а не молчит"
    fi
    rm -rf "$dvor_bez_baz"
    # Решение судим по всем четырём ответам docker, включая ЕДИНСТВЕННЫЙ случай
    # «свой не нужен»: без него мутация «всегда поднимать свой» была зелёной, а
    # это значит поднимать свой контейнер рядом с работающей базой продукта.
    реш() { # $1=ждём(да|нет) $2=имя $3=ответ docker $4=имя случая
        local got=нет
        свой_нужен_по_ответу "$2" "$3" && got=да
        if [ "$got" = "$1" ]; then printf '  ок    %s\n' "$4"
        else printf '  ПЛОХО %s: ждали «%s», получили «%s»\n' "$4" "$1" "$got"; ok=0; fi
    }
    реш нет "база-продукта" "true"  "контейнер продукта работает — своего НЕ поднимаем"
    реш да  "база-продукта" "false" "контейнер есть, но погашен — нужен свой"
    реш да  "база-продукта" ""      "контейнера нет вовсе (docker молчит) — нужен свой"
    реш да  ""              "true"  "имя не задано — свой контейнер"
    # Замок «роли только в своём контейнере»: снятый, он уводит CREATE ROLE в
    # боевую СУБД продукта.
    if роли_создавать 1; then echo "  ок    в своём контейнере роли из дампа создаются"
    else echo "  ПЛОХО в своём контейнере роли не создать — заливка даст «role does not exist»"; ok=0; fi
    if ! роли_создавать 0; then echo "  ок    БОЛЬНОЙ СЛУЧАЙ: в БОЕВОЙ контейнер роли не создаются"
    else echo "  ПЛОХО проба правит роли в боевой СУБД продукта"; ok=0; fi
    # Маркер готовности: чужой образ его не печатает, и это обязано быть названо.
    if маркер_готовности "LOG: database system is ready to accept connections
PostgreSQL init process complete; ready for start up."; then
        echo "  ок    маркер инициализации официального образа распознан"
    else echo "  ПЛОХО маркер готовности не распознан — ожидание выжжет 180 с впустую"; ok=0; fi
    if ! маркер_готовности "LOG: database system is ready to accept connections"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: журнал без маркера не считается подтверждением"
    else echo "  ПЛОХО журнал чужого образа принят за подтверждённую готовность"; ok=0; fi
    # Жнец — живой случай: остаток прошлого прогона должен исчезнуть, и число
    # убранного должно быть названо вслух.
    obraz_dlya_zhnetsa=$(docker images -q 2>/dev/null | head -1)
    if [ -z "$obraz_dlya_zhnetsa" ]; then
        echo "  ПЛОХО живую пробу жнеца провести нечем: ни одного локального образа docker"; ok=0
    else
        kont_ostatok="harness-restore-proba-selftest-$$"
        tom_ostatok="harness-restore-proba-selftest-$$-data"
        docker create --name "$kont_ostatok" -v "$tom_ostatok:/data" \
            "$obraz_dlya_zhnetsa" >/dev/null 2>&1 || true
        vyvod_zhnetsa=$(убрать_прошлые_прогоны "нет-такого-тома")
        if ! docker inspect "$kont_ostatok" >/dev/null 2>&1 \
           && ! docker volume inspect "$tom_ostatok" >/dev/null 2>&1; then
            echo "  ок    БОЛЬНОЙ СЛУЧАЙ: остаток прерванного прогона (контейнер и том) убран жнецом"
        else
            echo "  ПЛОХО остаток прогона не убран — утечка томов продолжится"; ok=0
            docker rm -fv "$kont_ostatok" >/dev/null 2>&1 || true
            docker volume rm -f "$tom_ostatok" >/dev/null 2>&1 || true
        fi
        if printf '%s' "$vyvod_zhnetsa" | grep -qE 'контейнеров убрано [1-9]'; then
            echo "  ок    жнец назвал число убранного — утечка не подбирается молча"
        else echo "  ПЛОХО жнец молчит о том, что убрал: «$vyvod_zhnetsa»"; ok=0; fi
    fi
    # Пароль пробной базы ГЕНЕРИРУЕТСЯ, а не берётся из секретов: боевой пароль
    # в командной строке одноразового контейнера — лишний путь утечки
    # ([[the-probes-secret-was-a-real-one]]).
    # БОЛЬНОЙ СЛУЧАЙ 28.09.2026: в своём контейнере дамп дал 49 ошибок «role
    # does not exist» и ноль таблиц — дамп несёт OWNER-указания на роли
    # продукта, а чистая база их не знает. Роли берутся ИЗ ДАМПА, а не из имени
    # продукта: имя продукта в коде харнеса запрещено гейтом.
    d=$(mktemp)
    printf 'ALTER TABLE public.дома OWNER TO app_ro;\nALTER SCHEMA public OWNER TO "app-rw";\nCOMMENT ON ROLE x IS 1;\n' > "$d"
    printf 'COPY public.t (c) FROM stdin;\nкомментарий: OWNER TO zloy\n\\.\n' >> "$d"
    got=$(роли_в_дампе "$d" | tr '\n' ' ')
    rm -f "$d"
    if [ "$got" = "app-rw app_ro " ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: роли владельцев взяты из дампа (в кавычках тоже),"
        echo "        а строка ДАННЫХ внутри COPY роль не заводит"
    else echo "  ПЛОХО роли из дампа разобраны как «$got»"; ok=0; fi
    p1=$(пароль_пробы); p2=$(пароль_пробы)
    if [ -n "$p1" ] && [ "$p1" != "$p2" ] && [ "${#p1}" -ge 16 ]; then
        echo "  ок    пароль пробной базы каждый раз новый и не короче 16 знаков"
    else echo "  ПЛОХО пароль пробы предсказуем: «$p1» / «$p2»"; ok=0; fi
    # «Каждый раз новый» мало: метка времени тоже каждый раз новая и при этом
    # угадывается (мутация ревью 28.09 — `date +%s%N`). Алфавит объявлен буквами
    # и цифрами; пароль из одних цифр — признак счётчика, а не случайности.
    if printf '%s' "$p1" | grep -q '[A-Za-z]'; then
        echo "  ок    пароль взят из объявленного алфавита, а не из метки времени"
    else echo "  ПЛОХО пароль состоит из одних цифр — это счётчик, а не случайность: «$p1»"; ok=0; fi
    # ── БОЛЬНОЙ СЛУЧАЙ 05.10–26.10.2026: роли из GRANT/REVOKE ─────────────────
    # Четыре прогона подряд проба писала «НЕ восстановился» на здоровом дампе:
    # роли продукта названы в нём не только через OWNER TO, но и через GRANT …
    # TO, REVOKE … FROM и ALTER DEFAULT PRIVILEGES FOR ROLE — ~125 ошибок «role
    # does not exist». PUBLIC — слово SQL, а не роль: CREATE ROLE на нём падал
    # бы каждый прогон. postgres в образе уже есть, создание идемпотентно —
    # в списке он безвреден.
    роли() { # $1=ждём (через пробел, порядок LC_ALL=C) $2=содержимое дампа $3=имя случая
        local f got; f=$(mktemp)
        printf '%s\n' "$2" > "$f"
        got=$(роли_в_дампе "$f" | tr '\n' ' '); got="${got% }"
        rm -f "$f"
        if [ "$got" = "$1" ]; then printf '  ок    %s\n' "$3"
        else printf '  ПЛОХО %s: ждали «%s», получили «%s»\n' "$3" "$1" "$got"; ok=0; fi
    }
    роли "app_user" 'GRANT SELECT,INSERT ON TABLE public.t TO app_user;' \
        "БОЛЬНОЙ СЛУЧАЙ: роль из GRANT … TO заводится"
    роли "app-rw" 'GRANT ALL ON SEQUENCE public.s TO "app-rw" WITH GRANT OPTION;' \
        "GRANT … TO в кавычках и с WITH GRANT OPTION"
    роли "app_revoked" 'REVOKE ALL ON TABLE public.t FROM app_revoked;' \
        "роль из REVOKE … FROM"
    роли "app_owner app_watch" \
        'ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public GRANT SELECT ON TABLES TO app_watch;' \
        "ALTER DEFAULT PRIVILEGES: и владелец FOR ROLE, и получатель TO"
    роли "app_child app_parent" 'GRANT app_parent TO app_child;' \
        "членство GRANT роль TO роль — заводятся обе"
    роли "postgres" "$(printf '%s\n' 'REVOKE ALL ON SCHEMA public FROM PUBLIC;' \
            'GRANT USAGE ON SCHEMA public TO PUBLIC;' \
            'ALTER DEFAULT PRIVILEGES FOR ROLE postgres REVOKE ALL ON FUNCTIONS FROM PUBLIC;')" \
        "PUBLIC ролью не считается, postgres остаётся (создание идемпотентно)"
    роли "" "$(printf '%s\n' 'COPY public.t (c) FROM stdin;' 'письмо: GRANT SELECT ON t TO zloy' \
            'и ещё: REVOKE ALL ON t FROM zloy2' '\.')" \
        "строка ДАННЫХ внутри COPY с GRANT/REVOKE роль не заводит"
    # ── БОЛЬНОЙ СЛУЧАЙ 05.10–26.10.2026: дамп pg 17 в образе 16 ──────────────
    # Свой контейнер поднимался из PROBE_DB_IMAGE (16), а дамп снят pg_dump 17:
    # «transaction_timeout» и «\restrict» — синтаксис старшего pg_dump на
    # младшем сервере. Версию решает pg_dump, а не сервер-источник: pg_dump не
    # снимает сервер новее себя, и синтаксис дампа — его.
    shapka() { # $1=версия сервера $2=версия pg_dump
        printf -- '--\n-- PostgreSQL database dump\n--\n\n'
        printf -- '-- Dumped from database version %s (Debian %s-1.pgdg13+1)\n' "$1" "$1"
        printf -- '-- Dumped by pg_dump version %s (Debian %s-1.pgdg13+1)\n' "$2" "$2"
    }
    obraz() { # $1=ждём $2=начало дампа $3=шаблон $4=имя случая
        local f got; f=$(mktemp)
        printf '%s\nSET statement_timeout = 0;\n' "$2" > "$f"
        got=$(image_for_dump "$f" "postgis/postgis:16-3.4" "$3")
        rm -f "$f"
        if [ "$got" = "$1" ]; then printf '  ок    %s\n' "$4"
        else printf '  ПЛОХО %s: ждали «%s», получили «%s»\n' "$4" "$1" "$got"; ok=0; fi
    }
    shablon='postgis/postgis:{major}-3.5'
    obraz "postgis/postgis:17-3.5" "$(shapka 17.6 17.6)" "$shablon" \
        "БОЛЬНОЙ СЛУЧАЙ: дамп pg_dump 17 — образ major 17"
    obraz "postgis/postgis:16-3.5" "$(shapka 16.4 16.4)" "$shablon" "дамп 16 — образ major 16"
    obraz "postgis/postgis:17-3.5" "$(shapka 15.8 17.6)" "$shablon" \
        "сервер 15, pg_dump 17 — образ по pg_dump"
    obraz "postgis/postgis:16-3.4" "-- дамп без шапки" "$shablon" \
        "шапки нет — PROBE_DB_IMAGE как есть"
    obraz "postgis/postgis:16-3.4" "$(shapka 17.6 17.6)" "" \
        "шаблон пуст — PROBE_DB_IMAGE как есть (подбор выключается данными)"
    # ── БОЛЬНОЙ СЛУЧАЙ 05.10–26.10.2026: отказ пробы не доходил до владельца ──
    # Исход печатался только в журнал крона: четыре недели «НЕ восстановился»
    # не прочёл никто. Решение «слать или нет» — чистая функция по виду исхода
    # (alert_message: пусто — молчать), отправка — alert_owner через дверь
    # RESTORE_PROBE_SENDER (здесь — фальшивая, пишет в файл).
    trevoga() { # $1=ждём вызовов $2=вид исхода $3=ошибок $4=таблиц $5=причина $6=имя случая
        : > "$dvor_trevogi/door.log"
        alert_owner "$2" "$3" "$4" "$5" >/dev/null 2>&1
        local got; got=$(door_calls)
        if [ "$got" = "$1" ]; then printf '  ок    %s\n' "$6"
        else printf '  ПЛОХО %s: ждали вызовов двери %s, получили %s\n' "$6" "$1" "$got"; ok=0; fi
    }
    # Шаблон: незаданный ключ — умолчание кода, пустой — подбор выключен.
    if [ "$(unset PROBE_DB_IMAGE_TEMPLATE; image_template)" = 'postgis/postgis:{major}-3.6' ]; then
        echo "  ок    ключ шаблона не задан — действует умолчание кода"
    else echo "  ПЛОХО без ключа шаблона подбор образа не работает до правки живого конфига"; ok=0; fi
    if [ -z "$(PROBE_DB_IMAGE_TEMPLATE="" image_template)" ]; then
        echo "  ок    ключ шаблона пуст — подбор образа выключен данными"
    else echo "  ПЛОХО пустой ключ шаблона не выключает подбор"; ok=0; fi
    trevoga 1 not_restored 128 100 "" \
        "БОЛЬНОЙ СЛУЧАЙ: дамп не восстановился — владельцу ровно одно сообщение"
    trevoga 1 not_run "" "" "контейнер не поднялся" "проба не проведена — сообщение уходит"
    trevoga 0 not_applicable "" "" "" "дампов нет — тишина"
    trevoga 0 restored 0 100 "" "дамп восстановился — тишина"
    # Текст — для владельца: что случилось, сколько ошибок, что делать. Без
    # кодов инвариантов и путей — он их не открывает.
    tekst=$(alert_message not_restored 128 100 "" 2>/dev/null)
    tekst_ne_provedena=$(alert_message not_run "" "" "контейнер не поднялся" 2>/dev/null)
    if printf '%s' "$tekst" | grep -q '128' \
       && printf '%s' "$tekst" | grep -qi 'восстанов' \
       && ! printf '%s' "$tekst" | grep -qE 'И-1|/var/|scripts/|\.sh' \
       && [ "${#tekst}" -le 600 ] \
       && printf '%s' "$tekst_ne_provedena" | grep -q 'контейнер не поднялся'; then
        echo "  ок    текст тревоги простыми словами: число ошибок есть, кодов и путей нет, причина названа"
    else echo "  ПЛОХО текст тревоги: «$tekst» / «$tekst_ne_provedena»"; ok=0; fi
    # Недоставленная тревога — та же тишина, если о ней молчать.
    vyvod_dveri=$(RESTORE_PROBE_SENDER="$dvor_trevogi/sender-broken.sh" \
        alert_owner not_restored 128 100 "" 2>&1); rc_dveri=$?
    if [ "$rc_dveri" != 0 ] && printf '%s' "$vyvod_dveri" | grep -q 'не ушла'; then
        echo "  ок    дверь отказала — проба говорит «тревога не ушла» и возвращает отказ"
    else echo "  ПЛОХО отказ двери проглочен: rc=$rc_dveri, «$vyvod_dveri»"; ok=0; fi
    # Живой путь целиком: функция, которую основной ход не зовёт, зелена в
    # случаях выше. Образ заведомо несуществующий — docker отказывает за доли
    # секунды, и проба доходит до «НЕ ПРОВЕДЕНА».
    dvor_zhivoj=$(mktemp -d)
    mkdir -p "$dvor_zhivoj/kopii/2026-11-02_0000"
    printf 'CREATE TABLE t ();\n' > "$dvor_zhivoj/kopii/2026-11-02_0000/db-selftest.dump"
    printf '%s\n' "BACKUP_DIR=\"$dvor_zhivoj/kopii\"" "LOG_DIR=\"$dvor_zhivoj\"" 'BACKUP_DBS=""' \
        'DROPPABLE_DB_PREFIX="proba_"' 'PROBE_DB_CONTAINER=""' 'PROBE_DB_USER=""' \
        'PROBE_DB_IMAGE="harness-restore-selftest.invalid/none:0"' 'PROBE_DB_IMAGE_TEMPLATE=""' \
        "HEARTBEAT_DIR=\"$dvor_zhivoj/metki\"" > "$dvor_zhivoj/harness.conf"
    : > "$dvor_trevogi/door.log"
    vyvod_zhivoj=$(HARNESS_CONF="$dvor_zhivoj/harness.conf" LOG_DIR="$dvor_zhivoj" \
        HEARTBEAT_DIR="$dvor_zhivoj/metki" bash "$0" 2>&1)
    if printf '%s' "$vyvod_zhivoj" | grep -q 'НЕ ПРОВЕДЕНА' && [ "$(door_calls)" = 1 ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: живой прогон «НЕ ПРОВЕДЕНА» — тревога ушла одна"
    else
        echo "  ПЛОХО живой прогон: вызовов двери $(door_calls), исход «$(printf '%s' "$vyvod_zhivoj" | tail -1)»"; ok=0
    fi
    # ── БОЛЬНЫЙ СЛУЧАЙ 09.11.2026: образ и переменные источника, вердикт без отправки ──
    # Заглушка docker в своём PATH: inspect отдаёт образ продукта с его переменными
    # (среди них пароль — он в стенд не идёт), run пишет аргументы в журнал, а
    # таблиц «поднялось» 0 — проба доходит до отказа и до тревоги.
    dvor_stub="$dvor_zhivoj/stub"; mkdir -p "$dvor_stub"
    cat > "$dvor_stub/docker" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_LOG"
case "$1" in
    inspect)
        if [ "$2" = "-f" ]; then echo false
        elif [ "$2" = "cf_postgres" ]; then
            printf '[{"Config":{"Image":"product-pg:latest","Env":["POSTGRES_USER=cfuser","POSTGRES_DB=cfdb","POSTGRES_PASSWORD=zhivoj-sekret","PATH=/bin","DATABASE_URL=postgres://u:urlsekret@h/db","DB_PWD=pwdsekret","LANG=C.UTF-8"],"Cmd":["postgres","-c","config_file=/etc/postgresql/pg.conf","-c","shared_preload_libraries=pg_partman_bgw","-c","hba_file=/etc/hba.conf","--requirepass","cmdsekret"]}}]\n'  # не-секрет: поддельные значения заглушки docker, проба ждёт, что их не напечатают
        else exit 1; fi ;;
    logs) echo "PostgreSQL init process complete; ready for start up." ;;
    exec) case "$*" in *information_schema*) echo 0 ;; esac ;;
    run) while [ $# -gt 0 ]; do [ "$1" = --env-file ] && { cat "$2" > "$STUB_LOG.env"; stat -c %a "$2" > "$STUB_LOG.mode"; echo "$2" > "$STUB_LOG.path"; }; shift; done; echo stubid ;;
esac
exit 0
STUB
    chmod +x "$dvor_stub/docker"
    printf '%s\n' 'BACKUP_DBS="cf_core_db=docker exec -i -u postgres cf_postgres pg_dump cf_core_db"' \
        'PROBE_DB_IMAGE_TEMPLATE=""' >> "$dvor_zhivoj/harness.conf"
    sed -i '/^PROBE_DB_IMAGE=/d' "$dvor_zhivoj/harness.conf"
    printf 'CREATE TABLE t ();\n' > "$dvor_zhivoj/kopii/2026-11-02_0000/db-cf_core_db.dump"
    rm -f "$dvor_zhivoj/kopii/2026-11-02_0000/db-selftest.dump"
    stub_run() { # аргументы пробы → её stdout; журнал docker — в $dvor_stub/log
        : > "$dvor_stub/log"; : > "$dvor_trevogi/door.log"; rm -f "$dvor_stub"/log.*
        PATH="$dvor_stub:$PATH" STUB_LOG="$dvor_stub/log" HARNESS_CONF="$dvor_zhivoj/harness.conf" \
            LOG_DIR="$dvor_zhivoj" HEARTBEAT_DIR="$dvor_zhivoj/metki" bash "$0" "$@" 2>&1
    }
    vyvod_stub=$(stub_run); run_line=$(grep '^run ' "$dvor_stub/log")
    if printf '%s' "$run_line" | grep -q 'product-pg:latest postgres -c shared_preload_libraries=pg_partman_bgw$' \
       && grep -qx 'POSTGRES_DB=cfdb' "$dvor_stub/log.env" && grep -qx 'LANG=C.UTF-8' "$dvor_stub/log.env"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: стенд поднят образом, переменными и командой источника"
    else echo "  ПЛОХО run без образа/переменных источника: «$run_line»"; ok=0; fi
    # Всё, чего нет в белом списке, — в стенд не идёт; и в argv ничего, кроме --env-file.
    if ! cat "$dvor_stub/log" "$dvor_stub/log.env" | grep -qE 'zhivoj-sekret|urlsekret|pwdsekret|cmdsekret|DATABASE_URL|DB_PWD|requirepass' \
       && grep -q '^POSTGRES_PASSWORD=.' "$dvor_stub/log.env" && ! printf '%s' "$run_line" | grep -q 'POSTGRES_'; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: URL с паролем, *_PWD, чужие ключи Cmd не перенесены; пароль стенда свой и не в argv"
    else echo "  ПЛОХО секрет источника перенесён или переменные в argv"; ok=0; fi
    if ! printf '%s' "$run_line" | grep -qE 'config_file|hba_file'; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: аргументы Cmd с путями к файлам продукта отброшены"
    else echo "  ПЛОХО в стенд ушёл путь к файлу, которого там нет"; ok=0; fi
    if [ "$(cat "$dvor_stub/log.mode")" = 600 ] && [ ! -e "$(cat "$dvor_stub/log.path")" ]; then
        echo "  ок    env-файл с правами 0600 и удалён после запуска"
    else echo "  ПЛОХО env-файл: права $(cat "$dvor_stub/log.mode"), остался на диске"; ok=0; fi
    if grep -q 'exec -i harness-restore-proba-[0-9]* pg_isready -U cfuser' "$dvor_stub/log"; then
        echo "  ок    пользователь стенда взят из POSTGRES_USER источника"
    else echo "  ПЛОХО пользователь стенда не из переменных источника"; ok=0; fi
    if [ "$(door_calls)" = 1 ]; then echo "  ок    обычный прогон: отказ пробы — тревога владельцу ушла"
    else echo "  ПЛОХО обычный прогон: вызовов двери $(door_calls), ждали 1"; ok=0; fi
    vyvod_bez=$(stub_run --без-отправки)
    if [ "$(door_calls)" = 0 ] && printf '%s' "$vyvod_bez" | grep -q 'ВЕРДИКТ (без отправки)'; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: --без-отправки — дверь не звана, вердикт в stdout"
    else echo "  ПЛОХО --без-отправки: вызовов двери $(door_calls), вывод «$(printf '%s' "$vyvod_bez" | tail -2)»"; ok=0; fi
    # Явная подмена образа сильнее источника.
    : > "$dvor_stub/log"
    printf '%s\n' 'PROBE_DB_IMAGE_TEMPLATE="my/pg:{major}"' >> "$dvor_zhivoj/harness.conf"
    printf -- '-- Dumped by pg_dump version 17.2\nCREATE TABLE t ();\n' > "$dvor_zhivoj/kopii/2026-11-02_0000/db-cf_core_db.dump"
    stub_run >/dev/null
    if grep '^run ' "$dvor_stub/log" | grep -q ' my/pg:17'; then
        echo "  ок    PROBE_DB_IMAGE_TEMPLATE остаётся явной подменой образа"
    else echo "  ПЛОХО явный шаблон не победил образ источника"; ok=0; fi
    # Контейнер-источник не найден inspect — прежний подбор, а не отказ.
    sed -i 's/cf_postgres/net_takogo/' "$dvor_zhivoj/harness.conf"
    sed -i '/^PROBE_DB_IMAGE_TEMPLATE=/d' "$dvor_zhivoj/harness.conf"
    stub_run >/dev/null
    if grep '^run ' "$dvor_stub/log" | grep -q 'postgis/postgis:17-3.6'; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: inspect источника молчит — прежний подбор по версии дампа"
    else echo "  ПЛОХО без inspect источника проба не вернулась к подбору по версии"; ok=0; fi
    if [ "$(container_from_command 'docker exec -i -e A=b -u u cname pg_dump x')" = cname ] \
       && ! container_from_command 'mysqldump -h host db' >/dev/null; then
        echo "  ок    имя контейнера из команды дампа: опции с аргументом пропущены, не-docker — пусто"
    else echo "  ПЛОХО разбор команды дампа"; ok=0; fi
    rm -rf "$dvor_zhivoj" "$dvor_trevogi"
    (( ok )) && { echo "SELFTEST: зелёный (55 путей: 6 метакоманды, 5 выбор контейнера, 4 дампов нет вовсе, 2 замок ролей, 1 роли владельцев, 7 роли из GRANT/REVOKE, 2 маркер готовности, 2 жнец остатков, 2 пароль, 5 образ по версии дампа, 2 шаблон образа, 7 тревога владельцу, 10 образ и запуск источника, вердикт без отправки)"; exit 0; }
    echo "SELFTEST: КРАСНЫЙ"; exit 1
fi


# Дамп выбирается ДО подъёма контейнера: образ своего контейнера зависит от
# версии pg_dump в шапке этого дампа.
DUMP="${1:-$(find "$BACKUP_DIR" -name 'db-*.dump' -type f -printf '%T@ %p\n' 2>/dev/null \
      | sort -nr | head -1 | cut -d' ' -f2-)}"
[ -n "$DUMP" ] && [ -s "$DUMP" ] || abort_not_run "дампа не нашлось в $BACKUP_DIR"

# Свой контейнер, если названного в конфиге нет: копии продукта существуют и
# тогда, когда сам продукт погашен владельцем (замер 28.09.2026 — проба
# отказывала на первом шаге с 21.09).
SVOJ_KONT=0
if нужен_свой_контейнер "$CONT"; then
    # Образ и запуск: явная подмена шаблоном — первой; иначе как у контейнера, из
    # которого снят дамп (замер 09.11.2026: чужой образ не знает расширений
    # продукта, и целый дамп давал ложный отказ И-1); иначе прежний подбор по версии.
    RUN_ENV=(); RUN_CMD=(); USER_DB="postgres"; OBRAZ=""
    if [ -z "${PROBE_DB_IMAGE_TEMPLATE:-}" ]; then
        ISTOCHNIK=$(dump_source_container "$DUMP")
        PROFIL=""
        [ -n "$ISTOCHNIK" ] && PROFIL=$(source_run_profile "$ISTOCHNIK")
        while IFS=$'\t' read -r vid znach; do
            case "$vid" in
                I) OBRAZ="$znach" ;;
                E) RUN_ENV+=("$znach")
                   [ "${znach%%=*}" = POSTGRES_USER ] && USER_DB="${znach#*=}" ;;
                C) RUN_CMD+=("$znach") ;;
            esac
        done <<< "$PROFIL"
        [ -n "$OBRAZ" ] && echo "запуск как у контейнера $ISTOCHNIK: образ $OBRAZ, переменных ${#RUN_ENV[@]}"
    fi
    [ -n "$OBRAZ" ] || OBRAZ=$(image_for_dump "$DUMP" "${PROBE_DB_IMAGE:-postgis/postgis:16-3.4}" "$(image_template)")
    CONT="harness-restore-proba-$$"
    echo "контейнера продукта нет — поднимаю свой ($OBRAZ, $CONT)"
    # Том ИМЕНОВАННЫЙ и с тем же префиксом: образ базы объявляет VOLUME, и
    # `docker rm` без `-v` оставлял анонимный том каждый прогон — за одну ночь
    # 28.09.2026 утекло 1,1 ГБ, а уборщик диска томов не трогает вовсе.
    # Именованный том можно убрать точно, не задевая чужие висячие.
    TOM="harness-restore-proba-$$-data"
    убрать_прошлые_прогоны "$TOM"
    # Трап ставится ДО запуска: `docker run` умеет СОЗДАТЬ контейнер и отказать
    # на старте, и тогда уборки не было бы вовсе. Трап идемпотентен. INT и TERM
    # названы явно: замер 28.09.2026 — EXIT-трап исполняется при TERM, но НЕ
    # при INT (Ctrl-C ручного прогона), а при KILL не исполняется никогда.
    # Перед удалением СОХРАНЯЕМ журнал контейнера: 28.09.2026 сервер оборвал
    # соединение посреди заливки, контейнер унесло уборкой, и причина исчезла
    # вместе с ним — разбираться было нечем.
    # Журнал кладётся с отметкой времени: усечение одним именем стирало причину
    # обрыва на следующем прогоне, а при недельном расписании разбор почти
    # наверняка случается позже (ревью 28.09.2026, F-15).
    KONT_LOG="$LOG_DIR/проба-восстановления.контейнер.$(date +%F_%H%M).log"
    trap 'rm -f "${ENV_FILE:-}"; docker logs "$CONT" > "$KONT_LOG" 2>&1 || true;
          docker rm -fv "$CONT" >/dev/null 2>&1 || true;
          docker volume rm -f "$TOM" >/dev/null 2>&1 || true' INT TERM EXIT
    # Переменные идут файлом 0600, а не -e: argv виден в `ps` всем.
    ENV_FILE=$(mktemp); chmod 600 "$ENV_FILE"
    { printf '%s\n' ${RUN_ENV[@]+"${RUN_ENV[@]}"}; printf 'POSTGRES_PASSWORD=%s\n' "$(пароль_пробы)"; } > "$ENV_FILE"
    ZAPUSK=$(docker run -d --name "$CONT" -v "$TOM:/var/lib/postgresql/data" \
            --env-file "$ENV_FILE" "$OBRAZ" ${RUN_CMD[@]+"${RUN_CMD[@]}"} 2>&1); RC_RUN=$?
    rm -f "$ENV_FILE"
    [ "$RC_RUN" = 0 ] || {
        # Причина отказа НУЖНА: без неё «контейнер не поднялся» ничем не
        # отличается от «образа нет» и «места нет».
        echo "ОТКАЗ: свой контейнер для пробы не поднялся (образ $OBRAZ): $ZAPUSK"
        abort_not_run "контейнер не поднялся (образ $OBRAZ)"
    }
    SVOJ_KONT=1
    # Ждём НАСТОЯЩЕЙ готовности. Официальный образ при первом запуске поднимает
    # ВРЕМЕННЫЙ сервер для инициализации, а потом гасит его и стартует основной:
    # `pg_isready` отвечает «да» уже на временном, и заливка 205 МБ обрывалась
    # посреди COPY словами «received fast shutdown request» (замер 28.09.2026 —
    # три прогона впустую). Признак — строка образа о завершении инициализации.
    for _ in $(seq 1 90); do
        маркер_готовности "$(docker logs "$CONT" 2>&1)" && break
        sleep 2
    done
    # Чем кончилось ожидание — говорим вслух: маркер печатает только вход
    # официального образа и только при первой инициализации, а образ — данные.
    if маркер_готовности "$(docker logs "$CONT" 2>&1)"; then
        echo "готовность: маркер инициализации получен"
    else
        echo "ВНИМАНИЕ: образ $OBRAZ не напечатал маркер инициализации за 180 с —"
        echo "          дальше жду только pg_isready, и обрыв заливки на выключении"
        echo "          временного сервера снова возможен (замер 28.09.2026: три прогона)"
    fi
    for _ in $(seq 1 60); do
        docker exec -i "$CONT" pg_isready -U "$USER_DB" >/dev/null 2>&1 && break
        sleep 2
    done
    if ! docker exec -i "$CONT" pg_isready -U "$USER_DB" >/dev/null 2>&1; then
        echo "ОТКАЗ: свой контейнер не стал готов за 300 с (образ $OBRAZ: нет строки о готовности или база не отвечает)"
        abort_not_run "база не поднялась"
    fi
fi

DB="${PREFIX}restore_$(date +%m%d_%H%M)"

# Проба оставляет за собой базу на 278 МБ, а убрать её может только штатная
# дверь со словом владельца (И-1). Ревью 10.09.2026 поймало рост: три прогона —
# 834 МБ. Пока прежняя проба не убрана, новую не заводим: инструмент не имеет
# права копить то, что сам убрать не вправе.
# Замок спрашивает БОЕВОЙ контейнер: в своём, только что поднятом, прежних
# пробных баз не может быть никогда — там этот запрос охранял бы пустоту
# (ревью 28.09.2026, F-04). Утечку своих контейнеров и томов сторожит жнец выше.
OLD_PROBES=0
if [ "$SVOJ_KONT" = 0 ]; then
    OLD_PROBES=$(docker exec -i "$CONT" psql -U "$USER_DB" -d postgres -tAc \
        "select count(*) from pg_database where datname like '${PREFIX}restore_%'" 2>/dev/null | tr -d ' ')
fi
if [ "${OLD_PROBES:-0}" -gt 0 ] 2>/dev/null; then
    echo "ОТКАЗ: прежних пробных баз — ${OLD_PROBES}. Убери их штатной дверью:"
    docker exec -i "$CONT" psql -U "$USER_DB" -d postgres -tAc \
        "select '  DB_ADMIN_EXEC=''docker exec -i $CONT'' bash scripts/drop-database.sh '||datname \
         ||'   ('||pg_size_pretty(pg_database_size(datname))||')' \
         from pg_database where datname like '${PREFIX}restore_%'" 2>/dev/null
    abort_not_run "место занято прежней пробной базой"
fi

FOUND=$(метакоманды_в_дампе "$DUMP")
if [ -n "$FOUND" ]; then
    echo "ОТКАЗ: в дампе есть команды смены базы — заливка ушла бы мимо пробной:"
    printf '  %s\n' "$FOUND"
    echo "Сними дамп без --create (или вырежи метакоманды) и повтори."
    abort_not_run "дамп уводит заливку в чужую базу"
fi

echo "=== проба восстановления: $(date '+%Y-%m-%d %H:%M:%S') ==="
echo "дамп: $DUMP ($(du -h "$DUMP" | cut -f1)) → база $DB в контейнере $CONT"
docker exec -i "$CONT" psql -U "$USER_DB" -d postgres -c "CREATE DATABASE \"$DB\"" \
    || { echo "ОТКАЗ: базу не создать — проба не проведена"; abort_not_run "пробную базу не создать"; }
if роли_создавать "$SVOJ_KONT"; then
    # Роли владельцев нужны дампу: в чистом контейнере их нет, и заливка даёт
    # «role does not exist» на каждой таблице (замер 28.09.2026 — 49 ошибок, 0
    # таблиц). Роль без входа и без прав: она нужна только как владелец объектов.
    while IFS= read -r rol; do
        [ -n "$rol" ] || continue
        # Идемпотентно: роль вроде postgres в образе уже есть, и голый CREATE ROLE
        # печатал бы тревогу каждый прогон — сторож, который кричит всегда.
        # </dev/null: `docker exec -i` читает stdin цикла и съедал все роли, кроме
        # первой (стенд 02.11.2026: app_y не заведена, хотя найдена в дампе).
        docker exec -i "$CONT" psql -U "$USER_DB" -d postgres \
            -c "DO \$\$ BEGIN CREATE ROLE \"$rol\" NOLOGIN;
                EXCEPTION WHEN duplicate_object THEN NULL; END \$\$" >/dev/null 2>&1 </dev/null \
            || echo "проба: роль $rol не создана — ошибки владельца возможны"
    done <<< "$(роли_в_дампе "$DUMP")"
fi

T0=$(date +%s)
docker exec -i "$CONT" psql -U "$USER_DB" -d "$DB" -v ON_ERROR_STOP=0 < "$DUMP" \
    > /dev/null 2> "$LOG_DIR/проба-восстановления.err"
RC=$?
T1=$(date +%s)
ERRS=$(grep -c '^ERROR' "$LOG_DIR/проба-восстановления.err" 2>/dev/null)
TABLES=$(docker exec -i "$CONT" psql -U "$USER_DB" -d "$DB" -tAc \
    "select count(*) from information_schema.tables where table_schema='public'" 2>/dev/null)
echo "psql rc=$RC, время $((T1 - T0)) с, строк ERROR: ${ERRS:-?}, таблиц поднялось: ${TABLES:-0}"
[ "${ERRS:-1}" != 0 ] && grep '^ERROR' "$LOG_DIR/проба-восстановления.err" | cut -c1-110 | head -3

# Дверь ходит в СУБД через DB_ADMIN_EXEC (у нас это контейнер ПРОДА), а база
# лежит в стенде — команда без указания контейнера не сработала бы вовсе.
if [ "$SVOJ_KONT" = 1 ]; then
    echo "пробный контейнер $CONT будет удалён вместе с базой $DB"
else
    echo "ПРОБНАЯ БАЗА: $DB (контейнер $CONT)"
fi
echo "  убрать: DB_ADMIN_EXEC='docker exec -i $CONT' bash scripts/drop-database.sh $DB"
# Метка присмотра: без неё смерть этой пробы незаметна. Пишется ПОСЛЕ прогона
# и независимо от исхода — метка отвечает на вопрос «проба ходила», а исход
# говорит сама проба своим кодом возврата. Улика 13.09.2026: демон стоял в
# кроне без срока в HEARTBEAT_PERIODS, и его молчание не заметил бы никто.
mkdir -p "${HEARTBEAT_DIR}" 2>/dev/null || true
: > "${HEARTBEAT_DIR}/restore-probe" 2>/dev/null || true

if [ "$RC" = 0 ] && [ "${ERRS:-1}" = 0 ] && [ "${TABLES:-0}" -gt 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S')  ПРОБА ЗАВЕРШЕНА: дамп восстанавливается (таблиц ${TABLES}, ошибок 0)"
    # Зелёное снимает карточку отказа; писателя может не быть — проба от этого не падает.
    if [ -f "$PROJECT_DIR/scripts/alert-task.py" ]; then
        python3 "$PROJECT_DIR/scripts/alert-task.py" --resolve restore-probe:failed </dev/null >/dev/null 2>&1 || true
    fi
    exit 0
fi
echo "$(date '+%Y-%m-%d %H:%M:%S')  ПРОБА ЗАВЕРШЕНА: дамп НЕ восстановился целиком — это отказ И-1, разбирайся сейчас"
alert_owner not_restored "${ERRS:-?}" "${TABLES:-0}" "" || true
exit 1
