#!/usr/bin/env bash
# Проба выключателя фоновых процессов: обёртка scripts/run-background.sh.
#
# Что судится (спека .tasks/hr.process-dispatcher/spec.md §Б.1, §Б.2, §Б.2а,
# пункты 1–8 §Б.6). Ключ BACKGROUND_PROCESSES в harness.conf гасит каждое
# фоновое задание, а «по действию» обёртку не видит вовсе. Опечатка в ключе,
# нечитаемый или пустой конфиг — тоже «выключено»: открыть всё в окне
# `sudo tee` нельзя. Включённый путь обязан донести до ребёнка TERM, вернуть
# его rc и записать process.start/process.end, иначе диспетчер (§В) слеп.
#
# Стенд — свой на каждый случай: mktemp -d, свои HARNESS_HOME,
# HARNESS_INSTALL_CONF, HARNESS_CONF, LOG_DIR и библиотека через
# HARNESS_PROCESS_LIBRARY. Подопытная команда пишет файл-след. Боевые журналы,
# crontab и конфиг не трогаются.
#
# Механизмы проба находит РЯДОМ С СОБОЙ (подмена мутаций — «из-копии»):
# проект — scripts/ и harness/demons, пакет — harness/scripts и harness/demons.
#
#   bash scripts/test_run_background.sh
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
RB="$HERE/run-background.sh"
EVENTS_PY="$HERE/lib/events.py"
DEMONS=""
for candidate in "$ROOT/harness/demons" "$ROOT/demons"; do
    [ -d "$candidate" ] && { DEMONS="$candidate"; break; }
done
JOBS=""
for candidate in "$ROOT/harness/config/system-jobs.yaml" "$ROOT/config/system-jobs.yaml"; do
    [ -f "$candidate" ] && { JOBS="$candidate"; break; }
done
# shellcheck source=lib/selftest_cases.sh
source "$HERE/lib/selftest_cases.sh"

# Обёртки ещё нет — каждый случай краснеет своим словом, проба не падает.
[ -f "$RB" ] || echo "ВНИМАНИЕ: $RB не найден — случаи ниже красные"

STANDS=()
cleanup() {
    local d
    for d in ${STANDS[@]+"${STANDS[@]}"}; do
        case "$d" in /tmp/*|"${TMPDIR:-/tmp}"/*) rm -rf -- "$d" ;; esac
    done
}
trap cleanup EXIT

# Свой стенд: конфиг с заданной строкой, библиотека с одним исключением.
# $1 = текст harness.conf; «-» — файла нет вовсе.
new_stand() {
    T="$(mktemp -d)"
    STANDS+=("$T")
    mkdir -p "$T/home" "$T/log"
    printf 'PROJECT_DIR="%s"\n' "$ROOT" > "$T/install.conf"
    if [ "$1" = "-" ]; then CONF="$T/no-such-harness.conf"
    else CONF="$T/harness.conf"; printf '%s' "$1" > "$CONF"; fi
    cat > "$T/library.yaml" <<'YAML'
версия: 1
выключатель:
  ключ: BACKGROUND_PROCESSES
  исключения:
    - id: probe-exempt
      основание: "исключение стенда пробы"
процессы: {}
YAML
    JOURNAL="$T/log/process-runs.jsonl"
    LOST="$T/log/.events/lost.jsonl"
    set_stand_env
}

# Окружение обёртки: всё — стенда; наследованные двери журнала и задачи сняты,
# иначе проба судила бы окружение запуска, а не обёртку. Массив, а не функция:
# фоновый вызов функции — подоболочка, и $! был бы её pid, а не обёртки.
set_stand_env() {
    STAND_ENV=(env -u HARNESS_TASK -u HARNESS_LOG_DIR -u HARNESS_EVENTS_STATE -u INSTALL_CONF
        -u CLAUDE_CODE_SESSION_ID -u PROJECT_DIR -u BACKGROUND_PROCESSES
        HARNESS_HOME="$T/home" HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$CONF"
        LOG_DIR="$T/log" HARNESS_PROCESS_LIBRARY="$T/library.yaml" PYTHONDONTWRITEBYTECODE=1)
}

run_rb() { "${STAND_ENV[@]}" bash "$RB" "$@" 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&-; }

# Фоновый запуск обёртки с INT и QUIT по умолчанию: фоновый ребёнок
# неинтерактивного bash получает их игнорируемыми, и проба INT судила бы не
# обёртку. PID обёртки — в WPID.
launch_rb() {
    "${STAND_ENV[@]}" python3 -I -c '
import os, signal, sys
for s in (signal.SIGINT, signal.SIGQUIT, signal.SIGPIPE):
    signal.signal(s, signal.SIG_DFL)
os.execvp(sys.argv[1], sys.argv[1:])' bash "$RB" "$@" \
        >>"$T/wrapper.out" 2>&1 </dev/null 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- &
    WPID=$!
}

alive() { # $1 = pid; зомби — уже не жив
    local st
    st=$(awk '{print $3}' "/proc/$1/stat" 2>/dev/null) || return 1
    [ -n "$st" ] && [ "$st" != Z ]
}

wait_for() { # $1 = десятые доли секунды, остальное — условие
    local n="$1"; shift
    while [ "$n" -gt 0 ]; do "$@" && return 0; sleep 0.1; n=$((n - 1)); done
    return 1
}

# Дождаться конца обёртки не дольше $2 десятых; не дождались — убить всех.
reap() { # $1 = pid обёртки, $2 = срок; печатает «ушла» или «висела»
    if wait_for "$2" bash -c 'st=$(awk "{print \$3}" "/proc/$1/stat" 2>/dev/null); [ -z "$st" ] || [ "$st" = Z ]' _ "$1"; then
        wait "$1" 2>/dev/null; echo ушла
    else
        kill -KILL "$1" 2>/dev/null; wait "$1" 2>/dev/null; echo висела
    fi
}

# Строки журнала стенда: «событие|исход|id|pid|rc|сигнал|с|cpu_с|sid».
events() {
    python3 -I - "$JOURNAL" <<'PY'
import json, sys
try:
    rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
except FileNotFoundError:
    rows = []
for r in rows:
    d = r.get("data") or {}
    print("|".join(str(x) for x in (r.get("event"), r.get("outcome"), d.get("id"), d.get("pid"),
                                     d.get("rc"), d.get("сигнал", ""), d.get("с", ""),
                                     d.get("cpu_с", ""), d.get("sid", ""))))
PY
}
count_event() { events | awk -F'|' -v e="$1" '$1 == e' | wc -l; }
end_field() { events | awk -F'|' -v n="$1" '$1 == "process.end" {print $n; exit}'; }

# Ребёнок с ловушкой TERM: пишет готовность и, получив TERM, след и rc 7.
write_child() {
    cat > "$T/child.sh" <<'SH'
#!/bin/bash
trap 'echo TERM > "$2"; kill "$sp" 2>/dev/null; exit 7' TERM
sleep 30 & sp=$!
echo $$ > "$1"
wait "$sp"
exit 0
SH
}

echo "── 1. выключено: команда не запускается ──"
new_stand 'BACKGROUND_PROCESSES=off
'
out=$(run_rb probe-job -- sh -c 'echo след > "$1"; exit 3' _ "$T/trace"); rc=$?
case_check "1 БОЛЬНОЙ СЛУЧАЙ: off — следа команды нет" "нет" "$([ -e "$T/trace" ] && echo есть || echo нет)"
case_check "1 off — выход 0, а не rc команды" "0" "$rc"
case_check "1 off — одна строка stdout" "1" "$(printf '%s\n' "$out" | grep -c .)"
case_check "1 строка называет id и значение ключа" "да" \
    "$(printf '%s' "$out" | grep -q '^\[probe-job\] выключено выключателем (BACKGROUND_PROCESSES=off)' && echo да || echo "нет: $out")"
case_check "1 одно process.skip с исходом skipped" "process.skip|skipped|probe-job" \
    "$(events | cut -d'|' -f1-3 | tr '\n' ' ' | sed 's/ $//')"
OFF_STAND="$T"

echo "── 2. включено: команда идёт, rc насквозь ──"
new_stand 'BACKGROUND_PROCESSES=on
'
run_rb probe-job -- sh -c 'sleep 1; echo след > "$1"; exit 3' _ "$T/trace" >/dev/null; rc=$?
case_check "2 on — след команды есть" "есть" "$([ -e "$T/trace" ] && echo есть || echo нет)"
case_check "2 rc команды (3) проходит насквозь" "3" "$rc"
case_check "2 одно process.start и одно process.end" "1 1" "$(count_event process.start) $(count_event process.end)"
case_check "2 process.end: rc 3, исход error" "3|error" "$(end_field 5)|$(end_field 2)"
case_check "2 process.end: с ≥ 1 на sleep 1" "да" \
    "$(end_field 7 | awk '{print ($1 + 0 >= 1) ? "да" : "нет: " $0}')"
case_check "2 process.end несёт cpu_с числом" "да" \
    "$(end_field 8 | awk '/^[0-9]+([.,][0-9]+)?$/ {print "да"; next} {print "нет: " $0}')"
case_check "2 process.start несёт id, pid и sid" "да" \
    "$(events | awk -F'|' '$1 == "process.start" && $3 == "probe-job" && $4 ~ /^[0-9]+$/ && $9 ~ /^[0-9]+$/ {print "да"; exit}')"
case_check "2 pid start и end — один" "да" \
    "$(events | awk -F'|' '$1 == "process.start" {s=$4} $1 == "process.end" {e=$4} END {print (s != "" && s == e) ? "да" : "нет: " s "/" e}')"
run_rb probe-job -- true >/dev/null; rc=$?
case_check "2 rc 0 → выход 0 и исход ok" "0|ok" "$rc|$(events | awk -F'|' '$1 == "process.end" {o=$2} END {print o}')"

echo "── 3. выключено, но id в исключениях ──"
new_stand 'BACKGROUND_PROCESSES=off
'
run_rb probe-exempt -- sh -c 'echo след > "$1"' _ "$T/trace" >/dev/null; rc=$?
case_check "3 исключение при off — след есть" "есть" "$([ -e "$T/trace" ] && echo есть || echo нет)"
case_check "3 исключение — process.start и process.end, без process.skip" "1 1 0" \
    "$(count_event process.start) $(count_event process.end) $(count_event process.skip)"

echo "── 4. как судится значение ключа ──"
new_stand 'OTHER_KEY="1"
'
run_rb probe-job -- sh -c 'echo след > "$1"' _ "$T/trace" >/dev/null
case_check "4 ключа нет, файл непуст — работает (умолчание on)" "есть" "$([ -e "$T/trace" ] && echo есть || echo нет)"

new_stand 'BACKGROUND_PROCESSES=of
'
out=$(run_rb probe-job -- sh -c 'echo след > "$1"' _ "$T/trace"); rc=$?
case_check "4 БОЛЬНОЙ СЛУЧАЙ: опечатка «of» — выключено" "нет|0" "$([ -e "$T/trace" ] && echo есть || echo нет)|$rc"
case_check "4 строка называет значение «of»" "да" \
    "$(printf '%s' "$out" | grep -Eq 'BACKGROUND_PROCESSES=of([^f]|$)' && echo да || echo "нет: $out")"
case_check "4 опечатка — process.skip" "1" "$(count_event process.skip)"

for kind in нет пуст; do
    if [ "$kind" = нет ]; then new_stand -; else new_stand ''; fi
    out=$(run_rb probe-job -- sh -c 'echo след > "$1"' _ "$T/trace"); rc=$?
    case_check "4 БОЛЬНОЙ СЛУЧАЙ: файла конфига $kind — выключено, выход 0" "нет|0" \
        "$([ -e "$T/trace" ] && echo есть || echo нет)|$rc"
    case_check "4 файла $kind — строка «конфиг не прочитан»" "да" \
        "$(printf '%s' "$out" | grep -q 'конфиг не прочитан' && echo да || echo "нет: $out")"
    case_check "4 файла $kind — process.skip" "1" "$(count_event process.skip)"
done

echo "── 5. сигнал обёртке доходит до ребёнка ──"
for sig in TERM INT; do
    new_stand 'BACKGROUND_PROCESSES=on
'
    write_child
    launch_rb probe-job -- bash "$T/child.sh" "$T/ready" "$T/sig"
    if wait_for 50 test -s "$T/ready"; then
        kill -"$sig" "$WPID" 2>/dev/null
        gone=$(reap "$WPID" 50)
    else
        gone="ребёнок не поднялся"; kill -KILL "$WPID" 2>/dev/null; wait "$WPID" 2>/dev/null
    fi
    cpid=$(cat "$T/ready" 2>/dev/null)
    child_alive=$([ -n "$cpid" ] && alive "$cpid" && echo жив || echo нет)
    [ "$child_alive" = жив ] && kill -KILL "$cpid" 2>/dev/null
    case_check "5 $sig обёртке → ребёнок получил TERM" "TERM" "$(cat "$T/sig" 2>/dev/null || echo нет)"
    case_check "5 $sig: обёртка дождалась ребёнка и ушла" "ушла|нет" "$gone|$child_alive"
    case_check "5 $sig: process.end — rc ребёнка 7, сигнал TERM, исход error" "7|TERM|error" \
        "$(end_field 5)|$(end_field 6)|$(end_field 2)"
done

echo "── 6. убийство KILL ──"
new_stand 'BACKGROUND_PROCESSES=on
'
run_rb probe-job -- sh -c 'kill -KILL $$' >/dev/null
case_check "6 ребёнок убит KILL сам → rc 137, сигнал KILL, исход error" "137|KILL|error" \
    "$(end_field 5)|$(end_field 6)|$(end_field 2)"

new_stand 'BACKGROUND_PROCESSES=on
'
write_child
launch_rb probe-job -- bash "$T/child.sh" "$T/ready" "$T/sig"
started=$(wait_for 50 test -s "$T/ready" && wait_for 30 bash -c '[ "$(grep -c "process.start" "$1" 2>/dev/null)" -ge 1 ]' _ "$JOURNAL" && echo да || echo нет)
kill -KILL "$WPID" 2>/dev/null; wait "$WPID" 2>/dev/null
cpid=$(cat "$T/ready" 2>/dev/null)
[ -n "$cpid" ] && kill -TERM "$cpid" 2>/dev/null
sleep 0.5
case_check "6 обёртка убита KILL → process.start есть, process.end нет" "да|1|0" \
    "$started|$(count_event process.start)|$(count_event process.end)"
case_check "6 pid в process.start — pid обёртки (вход «оборван» В2)" "$WPID" \
    "$(events | awk -F'|' '$1 == "process.start" {print $4; exit}')"

echo "── 7. дескрипторы, замок, журнал ──"
new_stand 'BACKGROUND_PROCESSES=on
'
printf 'чужой ввод\n' > "$T/stdin"
"${STAND_ENV[@]}" bash "$RB" probe-job -- sh -c 'echo $$ > "$1"; exec sleep 3' _ "$T/ready" \
    >>"$T/wrapper.out" 2>&1 <"$T/stdin" 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- &
WPID=$!
fds="ребёнок не поднялся"; fd0=""
if wait_for 50 test -s "$T/ready"; then
    sleep 0.3
    cpid=$(cat "$T/ready")
    fds=$(ls "/proc/$cpid/fd" 2>/dev/null | sort -n | tr '\n' ' ' | sed 's/ $//')
    fd0=$(readlink "/proc/$cpid/fd/0" 2>/dev/null)
fi
reap "$WPID" 60 >/dev/null
case_check "7 ребёнок видит только fd 0–2" "0 1 2" "$fds"
case_check "7 stdin ребёнка — /dev/null" "/dev/null" "$fd0"

new_stand 'BACKGROUND_PROCESSES=on
'
launch_rb probe-job -- sh -c 'exec 9>"$1"; flock -n 9 && echo взят > "$2"; sleep 2' _ "$T/lock" "$T/locked"
held=$(wait_for 50 test -s "$T/locked" && { flock -n "$T/lock" true && echo свободен || echo держит; } || echo "не взят")
gone=$(reap "$WPID" 60)
after=$(flock -n "$T/lock" true && echo свободен || echo держит)
case_check "7 замок flock -n 9 ребёнок берёт, пока жива обёртка" "держит" "$held"
case_check "7 после конца — замок свободен (обёртка его не держит)" "ушла|свободен" "$gone|$after"

new_stand 'BACKGROUND_PROCESSES=on
'
run_rb probe-job -- sh -c 'echo строка-вывода; echo строка-ошибки >&2' >>"$T/job.log" 2>&1
case_check "7 >>лог 2>&1 — обе строки ребёнка в логе" "2" \
    "$(grep -cE '^строка-(вывода|ошибки)$' "$T/job.log" 2>/dev/null || echo 0)"

echo "── 8. форма событий и цена ──"
# Цена — CPU обёртки и всех её потомков (user+sys из wait4), а не стена: под
# нагрузкой сервера стена мерит очередь к ядрам, а не обёртку (спека §Б.2
# считала ≈70 мс CPU на emit). Стена и loadavg печатаются рядом как сведение.
# Печатает «стена_мс cpu_мс» на каждый из $1 запусков команды "${@:2}".
cost_runs() {
    python3 -I - "$@" 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- <<'PY'
import os, subprocess, sys, time
for _ in range(int(sys.argv[1])):
    t0 = time.perf_counter()
    child = subprocess.Popen(sys.argv[2:], stdout=subprocess.DEVNULL)
    _, _, usage = os.wait4(child.pid, 0)
    wall = time.perf_counter() - t0
    print(round(wall * 1000), round((usage.ru_utime + usage.ru_stime) * 1000))
PY
}
# Медиана столбца $1 строк «стена cpu»; пустой список — «нет», а не ноль.
median() { local n="$1"; shift; printf '%s\n' "$@" | awk -v n="$n" 'NF {print $n}' | sort -n \
    | awk '{a[NR]=$1} END {print (NR ? a[int((NR + 1) / 2)] : "нет")}'; }
new_stand 'BACKGROUND_PROCESSES=on
'
COST_ON="$T"
mapfile -t on_runs < <(cost_runs 20 "${STAND_ENV[@]}" bash "$RB" probe-job -- true)
T="$OFF_STAND"; CONF="$T/harness.conf"; JOURNAL="$T/log/process-runs.jsonl"; set_stand_env
mapfile -t off_runs < <(cost_runs 20 "${STAND_ENV[@]}" bash "$RB" probe-job -- true)
on_cpu=$(median 2 ${on_runs[@]+"${on_runs[@]}"})
off_cpu=$(median 2 ${off_runs[@]+"${off_runs[@]}"})
echo "  цена CPU (медиана): включённый $on_cpu мс, выключенный $off_cpu мс"
echo "  сведение, не судится — стена (медиана): включённый $(median 1 ${on_runs[@]+"${on_runs[@]}"}) мс, выключенный $(median 1 ${off_runs[@]+"${off_runs[@]}"}) мс; loadavg $(cat /proc/loadavg), ядер $(nproc)"
# Включённый путь — два emit по ≈100 мс CPU (замер 05.11), выключенный — один
# emit + разбор исключений.
case_check "8 цена включённого пути — медиана CPU ≤ 400 мс на 20 запусков" "да" \
    "$(echo "$on_cpu" | awk '{print ($1 != "нет" && $1 <= 400) ? "да" : "нет: " $1 " мс"}')"
case_check "8 цена выключенного пути — медиана CPU ≤ 300 мс на 20 запусков" "да" \
    "$(echo "$off_cpu" | awk '{print ($1 != "нет" && $1 <= 300) ? "да" : "нет: " $1 " мс"}')"
# Стенд выключенного пути — стенд случая 1: там уже одно process.skip.
case_check "8 цена мерила настоящую работу: 20 skip и 20 start" "20|20" \
    "$(( $(count_event process.skip) - 1 ))|$(JOURNAL="$COST_ON/log/process-runs.jsonl" count_event process.start)"

# Форма — по журналам ВСЕХ стендов: каждый случай выше писал свои события.
form=$(python3 -I - "$EVENTS_PY" "${STANDS[@]}" <<'PY'
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("events", sys.argv[1])
try:
    ev = importlib.util.module_from_spec(spec)
    # dataclass ищет свой модуль в sys.modules: без записи exec_module падает.
    sys.modules["events"] = ev
    spec.loader.exec_module(ev)
    outcomes = set(ev.OUTCOMES)
except Exception as exc:  # noqa: BLE001
    print(f"events.py не загрузился: {type(exc).__name__}"); sys.exit()
rows, lost, bad = 0, 0, []
for stand in sys.argv[2:]:
    path = os.path.join(stand, "log", "process-runs.jsonl")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if not line.strip():
                continue
            rows += 1
            r = json.loads(line)
            if r.get("v") != 1: bad.append(f"v={r.get('v')}")
            if r.get("task") != "-": bad.append(f"task={r.get('task')}")
            if r.get("task_from") != "env": bad.append(f"task_from={r.get('task_from')}")
            if r.get("outcome") not in outcomes: bad.append(f"outcome={r.get('outcome')}")
            if r.get("event") not in ("process.skip", "process.start", "process.end"):
                bad.append(f"event={r.get('event')}")
    lost_path = os.path.join(stand, "log", ".events", "lost.jsonl")
    if os.path.exists(lost_path):
        lost += sum(1 for l in open(lost_path, encoding="utf-8") if l.strip())
print(f"{rows}|{lost}|{';'.join(sorted(set(bad)))[:300]}")
PY
)
case_check "8 строк событий на стендах больше нуля (пустой журнал — красный)" "да" \
    "$(printf '%s' "$form" | awk -F'|' '{print ($1 + 0 > 0) ? "да" : "нет: " $0}')"
case_check "8 каждая строка — v1, task «-», task_from env, исход из OUTCOMES" "" \
    "$(printf '%s' "$form" | cut -d'|' -f3)"
case_check "8 ни одной строки в .events/lost.jsonl стендов" "0" "$(printf '%s' "$form" | cut -d'|' -f2)"

echo "── Б.2а. фоновый зовёт фоновый только через обёртку ──"
# Список вызовов — разбором демонов; имена демонов — разбором заданий крона.
gate=$(python3 -I - "${DEMONS:-нет}" "${JOBS:-нет}" <<'PY'
import os, re, sys
import yaml
demons, jobs_file = sys.argv[1], sys.argv[2]
try:
    jobs = yaml.safe_load(open(jobs_file, encoding="utf-8"))["задания"]
    names = sorted({os.path.basename(j["команда"].split()[0]) for j in jobs})
    files = sorted(f for f in os.listdir(demons) if f.endswith(".sh"))
except Exception as exc:  # noqa: BLE001
    print(f"СЛОМАНО\t{type(exc).__name__}: {exc}"); sys.exit()
# Исключения поимённо: (файл, образец строки, почему вне обёртки).
EXEMPT = [
    ("session-warden.sh", r'setsid nohup "\$0"',
     "самоотсоединение сторожа (--rotate-now, --досмотр-выхода) живёт внутри id session-warden"),
    ("panel-tunnel.sh", r'setsid "\$CLOUDFLARED_BIN"', "cloudflared — часть panel-tunnel"),
    ("sentinel.sh", r'session-warden\.sh"? --kick', "пинок --kick — подъём, не работа (§Б.2а, Д.2)"),
    ("sentinel.sh", r'session-warden\.sh --selftest', "самотест в круге проверок sentinel, не прогон задания"),
]
ref = re.compile(r'(?:harness/demons/|\$\{?HERE\}?/)(' + "|".join(map(re.escape, names)) + r')(?![\w.-])')
head = re.compile(r'(?:^|[;&|(!]|\$\(|\b(?:then|do|if|exec|bash|sh|python3|nohup|setsid|env|time))\s*'
                  r'(?:[A-Z_][A-Z0-9_]*=\S*\s+)*$')
setsid = re.compile(r'(?:^|[\s;&|(])setsid\s')
used = set()
for name in files:
    # Продолжение строки «\» склеивается: setsid и путь демона бывают на разных строках.
    logical, start, buf = [], 0, ""
    for no, raw in enumerate(open(os.path.join(demons, name), encoding="utf-8").read().splitlines(), 1):
        start = start or no
        if raw.endswith("\\"):
            buf += raw[:-1] + " "; continue
        logical.append((start, buf + raw)); start, buf = 0, ""
    for no, line in logical:
        if line.lstrip().startswith("#"):
            continue
        direct = any(head.search(re.sub(r'["\']?(?:\$\{?\w+\}?/)?$', "", line[:m.start()]))
                     for m in ref.finditer(line))
        if not (direct or setsid.search(line) or "run-background.sh" in line):
            continue
        where = f"{name}:{no}"
        if "run-background.sh" in line:
            print(f"ОБЁРТКА\t{where}"); continue
        hit = next((i for i, (f, p, _) in enumerate(EXEMPT) if f == name and re.search(p, line)), None)
        if hit is not None:
            used.add(hit); print(f"ИСКЛЮЧЕНИЕ\t{where}\t{EXEMPT[hit][2]}")
        else:
            print(f"МИМО\t{where}\t{line.strip()[:160]}")
for i, (f, p, why) in enumerate(EXEMPT):
    if i not in used:
        print(f"ОТСТАЛО\t{f}\t{p} — {why}")
PY
)
printf '%s\n' "$gate" | grep -E '^(МИМО|ОТСТАЛО|СЛОМАНО)' | sed 's/^/    /'
case_check "Б.2а список запусков разбором демонов непуст" "да" \
    "$([ "$(printf '%s\n' "$gate" | grep -cE '^(ОБЁРТКА|ИСКЛЮЧЕНИЕ|МИМО)')" -gt 0 ] && echo да || echo "нет: $gate")"
case_check "Б.2а БОЛЬНОЙ СЛУЧАЙ: ни одного запуска демона мимо run-background.sh" "0" \
    "$(printf '%s\n' "$gate" | grep -cE '^(МИМО|СЛОМАНО)')"
case_check "Б.2а каждое исключение ещё стоит в коде (отставшее — красное)" "0" \
    "$(printf '%s\n' "$gate" | grep -c '^ОТСТАЛО')"
for need in "sentinel.sh" "limit-watch.sh" "session-warden.sh"; do
    case_check "Б.2а $need зовёт фонового через обёртку" "да" \
        "$(printf '%s\n' "$gate" | grep -q "^ОБЁРТКА	$need:" && echo да || echo нет)"
done

echo
echo "ВЫКЛЮЧАТЕЛЬ: путей $CASES_PATHS, неудач $CASES_FAILED"
[ "$CASES_FAILED" = 0 ]
