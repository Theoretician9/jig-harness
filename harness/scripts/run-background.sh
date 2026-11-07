#!/usr/bin/env bash
# Обёртка запуска фонового процесса харнеса: выключатель BACKGROUND_PROCESSES.
#
#   run-background.sh <id> -- <команда> [доводы]
#   run-background.sh --разрешён <id>      # только вопрос: пойдёт ли запуск (rc 0 — да, 1 — нет)
#
# Стоит в каждой строке крона, в ExecStart таймера sentinel и там, где фоновый
# зовёт фоновый. Хуки, панель и помощники её не видят, поэтому «по действию»
# выключатель не гасит. Значение ключа читается ИЗ ФАЙЛА конфига, а не из
# окружения: окружению выключатель не хозяин.
#   on или ключа нет в непустом файле — работает;
#   off, любое иное значение, файла нет / пуст / не читается — выключено:
#   одна строка в вывод задания, process.skip в журнал, выход 0.
# Исключения (выключатель.исключения в process-library.yaml) работают при off;
# записи с признаком `запуск: служба-проверок` под этой обёрткой не идут: ключ для них не действует.
set -u
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/lib/config.sh"
# shellcheck disable=SC1091
source "$HERE/lib/events.sh"
konf_zagruzit

# --разрешён <id>: только ответ «пойдёт ли запуск» (rc 0 — да, 1 — погашен), без записи.
# Нужен кнопке ревизии: флаг кнопки снимается, лишь если запуск не будет погашен.
probe_only=0
if [ "${1:-}" = "--разрешён" ] && [ "$#" -eq 2 ]; then
    probe_only=1; set -- "$2" -- true
fi
if [ "$#" -lt 3 ] || [ "$2" != "--" ]; then
    echo "run-background.sh <id> -- <команда> [доводы]" >&2
    exit 2
fi
id="$1"; shift 2
# id идёт в JSON строкой: лишнее заменяем, чтобы запись не ломалась.
id_json="${id//[^A-Za-z0-9._-]/_}"

conf_dir="$(konf_koren)"
conf_file="${HARNESS_CONF:-$conf_dir/harness.conf}"
install_file="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-$conf_dir/install.conf}}"

# Пустой файл — окно `sudo tee` (усечение, потом запись): открывать в нём всё нельзя.
state=on; why=""
if [ ! -r "$conf_file" ] || [ ! -s "$conf_file" ]; then
    state=off; value="?"; why="конфиг не прочитан"
else
    value="$(konf_iz_fajla BACKGROUND_PROCESSES)"
    value="${value#"${value%%[![:space:]]*}"}"; value="${value%"${value##*[![:space:]]}"}"
    if [ -z "$value" ] && ! grep -Eqs '^[[:space:]]*BACKGROUND_PROCESSES=' "$install_file" "$conf_file"; then
        value=on
    fi
    [ "$value" = on ] || state=off
fi

# Запись с признаком «служба-проверок» гонит служба systemd: ключ её не гасит
# (признак — данные библиотеки), process.start/end пишутся как у всех.
python3 -I "$HERE/lib/process_library.py" checks-service "$id" && state=on

if [ "$state" = off ] && ! python3 -I "$HERE/lib/process_library.py" exempt "$id"; then
    [ "$probe_only" -eq 0 ] || exit 1
    if [ -n "$why" ]; then
        echo "[$id] выключено выключателем (BACKGROUND_PROCESSES=$value, $why)"
    else
        echo "[$id] выключено выключателем (BACKGROUND_PROCESSES=$value)"
    fi
    value_json="${value//[^A-Za-z0-9._?-]/_}"
    HARNESS_TASK=- HARNESS_LOG_DIR="$LOG_DIR" harness_event process-runs scripts/run-background.sh process.skip skipped --json "{\"id\":\"$id_json\",\"значение\":\"$value_json\"}"
    exit 0
fi

[ "$probe_only" -eq 0 ] || exit 0

# Ребёнок идёт ФОНОМ с ожиданием: ловушку bash исполняет только после конца
# переднего ребёнка, и TERM обёртке дошёл бы до ребёнка уже после его смерти.
# Фоновый ребёнок неинтерактивного bash игнорирует INT и QUIT, поэтому ему шлём TERM.
child=0; got=""
forward() {
    got=TERM
    [ "$child" -gt 0 ] && kill -TERM "$child" 2>/dev/null
}
trap forward TERM INT HUP

sid="$(ps -o sid= -p $$ 2>/dev/null | tr -d ' ')"
# Запись process.start идёт фоном параллельно ребёнку (она дорога: одна вилка
# на запись); process.end ждёт её по pid, чтобы start в журнале стоял раньше end.
HARNESS_TASK=- HARNESS_LOG_DIR="$LOG_DIR" harness_event process-runs scripts/run-background.sh process.start ok --json "{\"id\":\"$id_json\",\"pid\":$$,\"sid\":${sid:-0}}" &
start_pid=$!
# CPU дождавшихся потомков берём разностью: запись process.start и ps тоже
# числятся потомками, а сверка В1 ждёт только ребёнка. `times` пишем в файл, а не
# в подстановку: в подстановке она показала бы нули новой вилки.
times_file="$(mktemp)"
children_cpu_ms() {
    times >"$times_file"
    cpu_now_ms=0
    local cu cs v m s
    { read -r _; read -r cu cs; } <"$times_file"
    for v in $cu $cs; do
        m="${v%%m*}"; s="${v#*m}"; s="${s%s}"
        cpu_now_ms=$(( cpu_now_ms + 10#$m * 60000 + 10#${s/./} ))
    done
}
children_cpu_ms; cpu_before_ms=$cpu_now_ms
t0="$EPOCHREALTIME"
"$@" </dev/null &
child=$!
wait "$child"; rc=$?
# Первый wait, прерванный ловушкой, вернул 128+n обёртки, а не rc ребёнка. Повтор
# без проверки «жив ли»: bash помнит статус убранного ребёнка; 127 — статуса нет,
# тогда остаётся rc первого wait.
if [ -n "$got" ]; then
    wait "$child"; rc_again=$?
    [ "$rc_again" -eq 127 ] || rc=$rc_again
fi
t1="$EPOCHREALTIME"

# Секунды: микросекунды целым, без вилок.
us=$(( 10#${t1/./} - 10#${t0/./} ))
elapsed="$(( us / 1000000 )).$(printf '%03d' $(( us % 1000000 / 1000 )))"

children_cpu_ms
cpu_ms=$(( cpu_now_ms - cpu_before_ms ))
rm -f "$times_file"
# CPU снят до wait записи start: недождавшийся потомок в `times` не числится.
wait "$start_pid" 2>/dev/null
cpu_s="$(( cpu_ms / 1000 )).$(printf '%03d' $(( cpu_ms % 1000 )))"

sig=""
if [ -n "$got" ]; then
    sig=TERM
elif [ "$rc" -gt 128 ] && [ "$rc" -le 192 ]; then
    sig="$(kill -l $(( rc - 128 )) 2>/dev/null)"
fi
outcome=ok; [ "$rc" -eq 0 ] || outcome=error
extra=""; [ -z "$sig" ] || extra=",\"сигнал\":\"$sig\""
HARNESS_TASK=- HARNESS_LOG_DIR="$LOG_DIR" harness_event process-runs scripts/run-background.sh process.end "$outcome" --json "{\"id\":\"$id_json\",\"pid\":$$,\"rc\":$rc,\"с\":$elapsed,\"cpu_с\":$cpu_s$extra}"
exit "$rc"
