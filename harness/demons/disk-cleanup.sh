#!/usr/bin/env bash
#
# disk-cleanup.sh — автоматическая уборка диска.
#
# Что держит: диск не дорастает до аварии незаметно. Появился 03.08.2026 после
# аварии: диск дорос до 95%, память кончилась, своп забился, load average
# улетел до 201 — и всё это накопилось незаметно, потому что никто ничего
# не убирал, а мониторинг хост не видел вовсе.
#
# Откуда взят: UNIFIED/templates/disk-cleanup.sh (живой скрипт боевого
# сервера), плейсхолдеры заменены на чтение /etc/harness/{install,harness}.conf.
# Чем доказывается: прогоном --dry-run на приёмке (01-SPEC §9) и первым
# ночным прогоном; порог «диск всё ещё занят» — сообщением в канал.
#
# Что делает (в порядке от самого безопасного к самому заметному):
#   1. Подрезает разросшиеся логи контейнеров
#   2. Сносит «висячие» docker-образы (те, что без имени, остатки пересборок)
#   3. Чистит кеш сборок docker
#   4. Оставляет по KEEP_ROLLBACK свежих датированных тега на сервис, остальные сносит
#   5. Чистит кеши пакетных менеджеров пользователя
#   6. Снимает осиротевших помощников инструментария (см. шаг 6)
#   7. Убирает отходы службы истории разговоров (каталоги задаются в harness.conf)
#
# Чего НЕ делает намеренно:
#   - не трогает образы, на которых работают контейнеры
#   - не трогает теги :latest и :previous (это откат, он должен быть всегда)
#   - не трогает тома с данными (базы, minio, метрики)
#   - не останавливает и не пересоздаёт контейнеры
#
# Запуск: cron, ночью, от имени $AGENT_USER (в группе docker — когда docker
# появится). Флаг --dry-run показывает, что было бы сделано, ничего не меняя.
#
# ОТСТУПЛЕНИЕ ОТ ШАБЛОНА, осознанное: в шаблоне set -uo (без -e), здесь по
# норме демонов харнеса set -euo pipefail — поэтому каждый шаг, которому
# позволено не получиться (docker ещё не установлен, кеша нет), обёрнут явно,
# и его отказ оставляет строку в логе, а не роняет уборку молча.
set -euo pipefail

# ── конфиги ─────────────────────────────────────────────────────────────────
INSTALL_CONF="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-/etc/harness/install.conf}}"
HARNESS_CONF="${HARNESS_CONF:-/etc/harness/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "disk-cleanup: нет $INSTALL_CONF — установка не завершена (01-SPEC §0)"; exit 1; }
[ -r "$HARNESS_CONF" ] || { echo "disk-cleanup: нет $HARNESS_CONF — скопируйте harness/config/harness.conf в /etc/harness/"; exit 1; }
# Окружение старше конфига: общий загрузчик вместо голого source (улика
# 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/konf.sh"
konf_zagruzit
: "${HEARTBEAT_DIR:?пуст HEARTBEAT_DIR}" "${LOG_DIR:?пуст LOG_DIR}"

KEEP_ROLLBACK="${KEEP_ROLLBACK:-2}"          # сколько датированных тегов отката хранить на сервис
LOG_TRUNCATE_MB="${LOG_TRUNCATE_MB:-20}"     # логи контейнеров крупнее — обнулять
# Кеш сборок: 24ч, а не неделя. Замер 05.08 — за смену активных пересборок
# набежало 12.8 ГБ кеша, а фильтр «старше недели» не трогал НИЧЕГО из него:
# уборка отчиталась «освобождено 24 МБ» при диске на 81%. Ручной прогон без
# фильтра освободил 13 ГБ и вернул диск к 66%. Кеш моложе суток стоит держать —
# он ускоряет соседние пересборки; всё, что старше, дешевле собрать заново.
BUILD_CACHE_KEEP_H="${BUILD_CACHE_KEEP_H:-24}"
# Возраст, с которого осиротевший помощник считается брошенным (шаг 6). Сутки:
# живой сеанс столько не держит помощника без родителя.
# 08.08 переменной здесь НЕ БЫЛО, хотя шаг 6 ею пользуется. При `set -u` первая
# же найденная сирота роняла бы скрипт с `unbound variable` — вместе с итогом
# уборки и оповещением «диск всё ещё забит». Проверено больным случаем: до
# правки шаг падал с кодом 1, после — снимает сироту и доходит до конца.
ORPHAN_MIN_AGE_SEC="${ORPHAN_MIN_AGE_SEC:-86400}"
# Шаблон имён вспомогательных процессов инструментария (шаг 6). Дефолт (в
# harness.conf) — помощники плагина памяти claude-mem; имена другие — поменяй
# ТАМ, не здесь: пороги — данные, не код.
ORPHAN_PATTERN="${ORPHAN_PATTERN:-chroma-mcp|claude-mem/[^ ]*/scripts/(mcp-server|worker)}"
# Порог «после уборки всё ещё тесно» — тот же, что у гигиены (harness.conf).
DISK_MAX_PCT="${DISK_MAX_PCT:-85}"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

# `tee` писал и в файл, и в поток, а крон льёт поток в ТОТ ЖЕ файл — каждая
# строка выходила дважды, и лог выглядел так, будто уборка запускалась дважды.
# Пишем только в поток: крон сам направит его в лог, а при ручном запуске видно
# на экране.
say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
run() {
    if [ "$DRY_RUN" = 1 ]; then
        say "  [пробный запуск] $*"
    else
        # Отказ шага — строка в логе, не молчание и не падение всей уборки.
        "$@" >/dev/null 2>&1 || say "  (шаг не удался: $*)"
    fi
}

# Один прогон за раз (Д-10, единый паттерн демонов): две уборки наперегонки
# делят одни и те же docker-объекты. Лок — сервисный каталог (конвенция К-1б).
LOCK_FILE="$LOG_DIR/locks/${SERVICE_LOCKS_SUBDIR:-services}/disk-cleanup.lock"
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>>"$LOCK_FILE"
flock -n 9 || { say "прогон уже идёт (лок $LOCK_FILE) — выхожу"; exit 0; }

used_kb() { df --output=used / | tail -1 | tr -d ' '; }
pct()     { df --output=pcent / | tail -1 | tr -d ' %'; }

BEFORE_KB=$(used_kb)
say "=== уборка начата (занято ${BEFORE_KB} КБ, $(pct)%) ==="

# Сервер начинает чистым: docker появляется только вместе с продуктом
# (01-SPEC §2). Пока его нет, шаги 1–4 пропускаются С ЗАПИСЬЮ — пропуск,
# неотличимый от сделанного, это молчаливый отказ.
if command -v docker >/dev/null 2>&1; then

# --- 1. Логи контейнеров ------------------------------------------------------
# Ротация настроена в /etc/docker/daemon.json, но применяется только к контейнерам,
# созданным ПОСЛЕ её появления. Долгоживущие контейнеры до пересоздания её не знают,
# поэтому подрезаем их здесь. truncate безопасен: docker пишет с O_APPEND и
# продолжит с новой позиции, перезапуск не нужен.
#
# Логи лежат под root, а скрипт ходит по расписанию без пароля — поэтому режем не
# через sudo, а вспомогательным контейнером: он и так root внутри, а пользователь
# расписания состоит в группе docker. Никаких паролей в cron.
LOGDIR=/var/lib/docker/containers
if [ "$DRY_RUN" = 1 ]; then
    n=$(docker run --rm -v "$LOGDIR:/c:ro" alpine:3.20 \
        sh -c "find /c -name '*-json.log' -size +${LOG_TRUNCATE_MB}M 2>/dev/null | wc -l" 2>/dev/null) || n=0
    say "1. логи контейнеров: обнулил бы ${n:-0} файлов крупнее ${LOG_TRUNCATE_MB} МБ"
else
    n=$(docker run --rm -v "$LOGDIR:/c" alpine:3.20 sh -c "
        n=\$(find /c -name '*-json.log' -size +${LOG_TRUNCATE_MB}M 2>/dev/null | wc -l)
        find /c -name '*-json.log' -size +${LOG_TRUNCATE_MB}M -exec truncate -s 0 {} \; 2>/dev/null
        echo \$n" 2>/dev/null | tail -1) || n=0
    say "1. логи контейнеров: обнулено ${n:-0} файлов крупнее ${LOG_TRUNCATE_MB} МБ"
fi

# --- 2. Висячие образы --------------------------------------------------------
# Это <none>:<none> — слои, оставшиеся от пересборок. Docker сам не удалит образ,
# на котором работает контейнер, так что операция безопасна по построению.
dangling=$(docker images -f dangling=true -q 2>/dev/null | wc -l) || dangling=0
run docker image prune -f
say "2. висячие образы: было $dangling"

# --- 2б. Остановленные контейнеры старше суток --------------------------------
# Улика 12.09.2026: осмотр сессии каждый раз печатал «контейнеры не в строю:
# great_engelbart(exited)» — это был осиротевший контейнер сборки `npm run
# build`, висевший десять часов. Предупреждение, которое горит всегда, не несёт
# сведений: в следующий раз рядом с ним незаметно встанет упавшая боевая служба.
#
# Порог суток, а не голый prune: контейнер, остановленный владельцем час назад,
# он мог остановить НАМЕРЕННО. Сутки — граница, после которой это мусор.
stopped=$(docker ps -a -q --filter status=exited --filter status=created 2>/dev/null | wc -l) || stopped=0
run docker container prune -f --filter "until=${STOPPED_KEEP_H:-24}h"
say "2б. остановленные контейнеры старше ${STOPPED_KEEP_H:-24}ч убраны (было всего $stopped)"

# --- 3. Кеш сборок ------------------------------------------------------------
run docker builder prune -f --filter "until=${BUILD_CACHE_KEEP_H}h"
say "3. кеш сборок старше ${BUILD_CACHE_KEEP_H}ч вычищен"

# --- 4. Старые теги отката ----------------------------------------------------
# Теги вида ГГГГММДД_ЧЧММ_хеш ставит scripts/tag-current.sh перед деплоем.
# Оставляем KEEP_ROLLBACK самых свежих на каждый сервис — этого хватает, чтобы
# откатиться на пару шагов назад. :latest и :previous не трогаем никогда.
# Репозиторий отрезается по ПОСЛЕДНЕМУ «:», не первому: у реестра с портом
# (host:5000/img:tag) первое двоеточие сидит внутри имени, и awk -F: считал
# репозиторием «host». Сортировка — целыми строками: префикс-репо у строк
# одинаков, значит сортируются фактически теги.
stale_tags=$(
    docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
      | grep -E ':[0-9]{8}_[0-9]{4}_[0-9a-f]+$' \
      | sed 's/:[^:]*$//' | sort -u \
      | while read -r repo; do
            docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
              | grep -E ':[0-9]{8}_[0-9]{4}_[0-9a-f]+$' \
              | while IFS= read -r img; do
                    [ "${img%:*}" = "$repo" ] && printf '%s\n' "$img"
                done \
              | sort -r \
              | tail -n "+$((KEEP_ROLLBACK + 1))"
        done
) || stale_tags=""
if [ -n "$stale_tags" ]; then
    count=$(printf '%s\n' "$stale_tags" | wc -l)
    printf '%s\n' "$stale_tags" | while read -r tag; do run docker rmi "$tag"; done
    say "4. теги отката: удалено $count устаревших, оставлено по $KEEP_ROLLBACK свежих на сервис"
else
    say "4. теги отката: лишних нет (храним по $KEEP_ROLLBACK на сервис)"
fi

else
    say "1–4. docker не установлен (продукт ещё не начат) — шаги пропущены"
fi

# --- 5. Кеши пакетных менеджеров ----------------------------------------------
# Все три восстанавливаются сами при следующей установке — терять нечего.
command -v npm >/dev/null 2>&1 && run npm cache clean --force
command -v pip >/dev/null 2>&1 && run pip cache purge
command -v uv  >/dev/null 2>&1 && run uv cache prune
say "5. кеши npm / pip / uv вычищены (какие есть)"

# --- 5-а. Каталоги установленных возможностей ---------------------------------
# Крупный набор (мобильная разработка и подобные) приносит свои каталоги отходов:
# снимки экрана, кеш сборок Gradle, скачанные результаты прогонов на реальных
# устройствах. Список лежит в самой возможности — файл `уборка.list` со строками
# «КАТАЛОГ<TAB>ДНЕЙ», и читается ТОЛЬКО у установленных (метка `.установлено`).
#
# Отдельным демоном это делать нельзя: набор демонов сверяется с таблицей §9
# спеки при установке, и лишняя строка crontab считалась бы расхождением.
# Уборка возможности — ДАННЫЕ существующего демона, а не новый демон.
#
# Подстановки в путях — только $HOME и $LOG_DIR, и подставляются они заменой
# строки, а не eval: данные из файла оболочка не исполняет.
CAPS_DIR="${PROJECT_DIR:-}/harness/возможности"
caps_cleaned=0
if [ -n "${PROJECT_DIR:-}" ] && [ -d "$CAPS_DIR" ]; then
    for pack in "$CAPS_DIR"/*/; do
        [ -f "${pack}.установлено" ] || continue
        [ -f "${pack}уборка.list" ] || continue
        pack_name=$(basename "$pack")
        while IFS=$'\t' read -r target days; do
            case "$target" in ''|\#*) continue ;; esac
            [ -n "$days" ] || { say "5-а. $pack_name: строка «$target» без числа дней — пропущена"; continue; }
            target="${target//\$HOME/$HOME}"
            target="${target//\$LOG_DIR/$LOG_DIR}"
            if [ ! -d "$target" ]; then
                # Пропуск с адресом: каталога может не быть законно (шаг
                # возможности пропускался), но молчать об этом нельзя.
                say "5-а. $pack_name: $target — каталога нет, чистить нечего"
                continue
            fi
            n=$(find "$target" -type f -mtime "+$days" 2>/dev/null | wc -l)
            if [ "$DRY_RUN" = 1 ]; then
                say "5-а. $pack_name: удалил бы ${n} файлов старше ${days} сут в $target"
            else
                find "$target" -type f -mtime "+$days" -delete 2>/dev/null || \
                    say "5-а. $pack_name: часть файлов в $target не удалилась (права?)"
                say "5-а. $pack_name: удалено ${n} файлов старше ${days} сут в $target"
            fi
            caps_cleaned=$((caps_cleaned + 1))
        done < "${pack}уборка.list"
    done
fi
[ "$caps_cleaned" = 0 ] && say "5-а. установленных возможностей со своей уборкой нет"

# --- 6. Осиротевшие процессы инструментария -----------------------------------
# 08.08: своп забился на 91%, и крупнейшим держателем оказался НЕ сервис, а
# `chroma-mcp` из плагина памяти Claude Code — 722 МБ, запущен 34 дня назад,
# породивший его сеанс давно умер, а процесс висел на systemd. Третий подобный
# случай за неделю.
#
# Условия сложены так, чтобы под нож не попало ничего живого:
#   * имя из узкого списка помощников (chroma-mcp / mcp-server плагинов);
#   * возраст больше суток;
#   * родитель — systemd (то есть сеанс, который его запустил, умер).
# Живой сеанс держит своих помощников сам, и его родитель — не systemd.
orphans=0
# Шаблон включает и РОДИТЕЛЯ (worker плагина): 08.08 я снял его детей, а он
# через минуту породил трёх новых по 460 МБ. Убирать надо того, кто плодит.
for pid in $(pgrep -f "$ORPHAN_PATTERN" 2>/dev/null || true); do
    ppid=$(awk '{print $4}' "/proc/$pid/stat" 2>/dev/null) || continue
    [ "$ppid" = "1" ] || continue
    # Оболочка помощником не бывает: под шаблон может попасть чужая командная
    # строка, просто упоминающая имя (проверка 08.08 поймала собственный pgrep).
    comm=$(cat "/proc/$pid/comm" 2>/dev/null) || continue
    case "$comm" in bash|sh|dash|zsh|pgrep|grep) continue ;; esac
    age=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ') || continue
    [ -n "$age" ] && [ "$age" -gt "$ORPHAN_MIN_AGE_SEC" ] || continue
    swap_kb=$(awk '/VmSwap/{print $2}' "/proc/$pid/status" 2>/dev/null) || swap_kb=0
    say "6. осиротевший помощник pid=$pid (${age}с, своп ${swap_kb:-0} КБ) — снимаю"
    # Вежливо, потом жёстко: TERM → подождать до 5 с → KILL, если не ушёл.
    # Сразу KILL нельзя (процесс не сбросит состояние), только TERM — мало:
    # зависший в свопе помощник TERM игнорирует и висит дальше.
    run kill "$pid"
    if [ "$DRY_RUN" = 0 ]; then
        for _ in 1 2 3 4 5; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
            say "  pid=$pid не ушёл по TERM за 5с — добиваю KILL"
            run kill -9 "$pid"
        fi
    fi
    orphans=$((orphans + 1))
done
if [ "$orphans" = 0 ]; then say "6. осиротевших помощников нет"; fi

# --- 7. Побочный продукт службы истории разговоров -----------------------------
# Замер 08.08 (владелец спросил «что лежит мёртвым грузом»): служба истории
# накопила 148 МБ собственных логов в 76 файлах — ротации у неё нет вообще, —
# и 775 МБ стенограмм наблюдательских сессий: 479 файлов меньше чем за месяц,
# примерно 26 МБ в день. Знания службы хранятся не в них, а в её базе; это
# именно отходы, и их не убирал никто.
#
# Возраст, а не размер: свежие нужны для разбора «что было вчера».
# Каталоги ЗАДАЮТСЯ в harness.conf: пустые, пока служба памяти не поставлена.
MEM_LOGS_KEEP_D="${MEM_LOGS_KEEP_D:-14}"
MEM_SESSIONS_KEEP_D="${MEM_SESSIONS_KEEP_D:-7}"
# Каталоги — переменными, чтобы больной случай можно было построить на подделке,
# не дожидаясь двух недель и не рискуя настоящими файлами.
MEM_LOG_DIR="${MEM_LOG_DIR:-}"
MEM_SESSION_DIR="${MEM_SESSION_DIR:-}"

# Подпись включает НОМЕР шага: вшитая «7.» внутри заставила бы копировать всю
# функцию ради восьмого шага — так и случилось 10.09.2026, ревью поймало дубль.
clean_old() {   # каталог, шаблон, дни, подпись (с номером шага)
    dir="$1"; pattern="$2"; days="$3"; label="$4"
    # Ненастроенный шаг обязан оставить след, иначе он неотличим от сделанного.
    [ -n "$dir" ] || { say "$label: КАТАЛОГ НЕ ЗАДАН — шаг не работает, задайте его"; return; }
    [ -d "$dir" ] || { say "$label: каталога $dir нет — пропускаю"; return; }
    n=$(find "$dir" -maxdepth 1 -name "$pattern" -type f -mtime "+$days" 2>/dev/null | wc -l)
    if [ "${n:-0}" = 0 ]; then say "$label: старше ${days} дн. нет"; return; fi
    mb=$(find "$dir" -maxdepth 1 -name "$pattern" -type f -mtime "+$days" -printf '%s\n' 2>/dev/null \
         | awk '{s+=$1} END {printf "%.0f", s/1048576}')
    say "$label: удаляю ${n} файлов старше ${days} дн. (${mb:-0} МБ)"
    if [ "$DRY_RUN" = 1 ]; then
        say "  [пробный запуск] find $dir -maxdepth 1 -name '$pattern' -mtime +$days -delete"
    else
        find "$dir" -maxdepth 1 -name "$pattern" -type f -mtime "+$days" -delete 2>/dev/null || true
    fi
}

# То же, что clean_old, но с чёрным списком имён (регэксп для grep -Ev).
# Отдельная функция, а не флаг: у семи вызовов clean_old исключений нет вовсе,
# и лишний позиционный параметр читался бы хуже, чем имя, называющее разницу.
clean_old_except() { # каталог, шаблон, дни, подпись, регэксп-исключение
    dir="$1"; pattern="$2"; days="$3"; label="$4"; skip="$5"
    [ -d "$dir" ] || { say "$label: каталога $dir нет — пропускаю"; return; }
    list=$(find "$dir" -maxdepth 1 -name "$pattern" -type f -mtime "+$days" 2>/dev/null \
           | grep -Ev "/($skip)$" || true)
    n=$(printf '%s' "$list" | grep -c . || true)
    if [ "${n:-0}" = 0 ]; then say "$label: старше ${days} дн. нет"; return; fi
    mb=$(printf '%s\n' "$list" | xargs -r stat -c '%s' 2>/dev/null | awk '{s+=$1} END {printf "%.0f", s/1048576}')
    say "$label: удаляю ${n} файлов старше ${days} дн. (${mb:-0} МБ)"
    if [ "$DRY_RUN" = 1 ]; then
        say "  [пробный запуск] удаление не выполняется"
    else
        printf '%s\n' "$list" | xargs -r rm -f 2>/dev/null || true
    fi
}

clean_old "$MEM_LOG_DIR"     '*.log'   "$MEM_LOGS_KEEP_D"     "7. логи службы истории"
clean_old "$MEM_SESSION_DIR" '*.jsonl' "$MEM_SESSIONS_KEEP_D" "7. стенограммы наблюдателя"

# ── 8. Логи харнеса ─────────────────────────────────────────────────────────
# У пути записи обязан быть ХОЗЯИН: до этого шага 1185 одноразовых логов
# прогонов (33 МБ) не убирал никто, а гейт удержания уже называл этого хозяина
# по имени — то есть обещал уборку, которой не было. Срок берётся ОТТУДА ЖЕ,
# из harness/config/удержание.yaml: два источника разъехались бы молча.
RETENTION="$PROJECT_DIR/harness/config/удержание.yaml"
LOG_KEEP=""
if [ -r "$RETENTION" ]; then
    LOG_KEEP=$(python3 -c "
import yaml, io, sys
d = yaml.safe_load(io.open(sys.argv[1], encoding='utf-8'))
print(next((p['срок_дней'] for p in d['пути'] if p['маска'] == '*.log'), ''))" "$RETENTION" 2>/dev/null)
fi
if [ -n "$LOG_KEEP" ] && [ "$LOG_KEEP" -gt 0 ] 2>/dev/null; then
    # Хроники не трогаем НИКОГДА (ревью 10.09.2026): guard_exceptions.log —
    # единственный след fail-open сторожа, install-*.log — протокол приёмки.
    # Их политика в удержание.yaml стоит отдельной строкой со сроком 0.
    n_chr=$(find "$LOG_DIR" -maxdepth 1 -type f -mtime "+$LOG_KEEP" \
            \( -name 'guard_exceptions.log' -o -name 'install-*.log' -o -name 'rotation-forced.log' \) \
            2>/dev/null | wc -l)
    [ "${n_chr:-0}" -gt 0 ] && say "8. хроник старше ${LOG_KEEP} дн: ${n_chr} — не трогаю (история, не мусор)"
    # Журналы, названные в инварианты.yaml, — улики жизни механизмов: покрытие
    # ищет в них след срабатывания. Удалив их по сроку, уборка сама и создала бы
    # красное «следа нет» ровно в день, когда след становится обязателен
    # (ревью 10.09.2026). Список читается ОТТУДА ЖЕ, а не переписывается сюда.
    INV="$PROJECT_DIR/harness/config/инварианты.yaml"
    KEEP_LOGS=$(python3 -c "
import yaml, io, sys, re
d = yaml.safe_load(io.open(sys.argv[1], encoding='utf-8'))
имена = {i.get('журнал') for i in d['инварианты'] if str(i.get('журнал','')).endswith('.log')}
print('|'.join(sorted(re.escape(n) for n in имена if n)))" "$INV" 2>/dev/null)
    clean_old_except "$LOG_DIR" '*.log' "$LOG_KEEP" "8. логи харнеса" \
        "guard_exceptions.log|install-.*\.log|rotation-forced\.log${KEEP_LOGS:+|$KEEP_LOGS}" 
else
    say "8. срок хранения логов не прочитался из $RETENTION — шаг пропущен"
fi

# --- итог ---------------------------------------------------------------------
AFTER_KB=$(used_kb)
FREED_MB=$(( (BEFORE_KB - AFTER_KB) / 1024 ))
say "=== уборка завершена: освобождено ${FREED_MB} МБ, занято $(pct)% ==="

# Если после уборки всё равно тесно — молчать нельзя. Оповещение идёт тем же
# каналом, что и остальные сигналы харнеса, чтобы всё приходило в одно место.
NOW_PCT=$(pct)
if [ "$DRY_RUN" = 0 ] && [ "$NOW_PCT" -ge "$DISK_MAX_PCT" ]; then
    say "ВНИМАНИЕ: после уборки занято ${NOW_PCT}% (порог ${DISK_MAX_PCT}%) — шлю оповещение"
    TOP=$( (docker system df 2>/dev/null || du -xsh /var/log /home 2>/dev/null) | tail -n +1 | tr '\n' ' ')
    ALERT="Автоуборка отработала, но диск всё ещё занят на ${NOW_PCT}%.
Освободить удалось ${FREED_MB} МБ — этого мало, нужен разбор вручную.
Крупное: ${TOP}"
    # Сначала штатный отправитель tg_send.sh (режет длинное, пишет отказы в
    # tg_failures.log); прямой curl — только запасной путь, если его нет/отказал.
    if [ -x "${PROJECT_DIR:-/nonexistent}/scripts/tg_send.sh" ] \
       && "${PROJECT_DIR:-/nonexistent}/scripts/tg_send.sh" "$ALERT" >/dev/null 2>&1; then
        say "оповещение доставлено (tg_send.sh, ok:true)"
    # Адресат и токен — из install.conf, не из кода.
    # Ненастроенный канал обязан оставить след. Иначе получится молчаливый отказ:
    # уборка выглядит работающей, а сигнал «диск забит» не приходит никогда, и
    # об этом никто не узнаёт (правило «мягкий отказ оставляет след»).
    elif [ -z "${TG_CHAT_ID:-}" ] || [ -z "${TG_TOKEN_FILE:-}" ]; then
        say "ОПОВЕЩЕНИЕ НЕ НАСТРОЕНО: заполните TG_TOKEN_FILE и TG_CHAT_ID в install.conf — сообщение о занятом диске отправить некуда"
    elif [ ! -r "$TG_TOKEN_FILE" ]; then
        say "ОПОВЕЩЕНИЕ НЕ УШЛО: файл с токеном $TG_TOKEN_FILE не читается"
    else
        TOKEN=$(cat "$TG_TOKEN_FILE")
        # Успех отправки — только ok:true (норма 01-SPEC §7); URL с токеном —
        # через stdin (curl -K -), не в argv: argv виден любому в ps.
        if printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TOKEN" \
            | curl -s -m 20 -K - -X POST \
            -d chat_id="$TG_CHAT_ID" \
            --data-urlencode "text=$ALERT" | jq -e '.ok == true' >/dev/null; then
            say "оповещение доставлено (ok:true)"
        else
            say "оповещение НЕ доставлено — API не вернул ok:true"
            exit 1
        fi
    fi
fi

# Метка heartbeat — только при успешном завершении (пробный запуск не в счёт:
# он ничего не убирал, засчитывать его страховкой нельзя).
[ "$DRY_RUN" = 1 ] || touch "$HEARTBEAT_DIR/disk-cleanup"
