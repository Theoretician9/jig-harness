#!/usr/bin/env bash
# Проба общего пула тяжёлых прогонов (scripts/heavy-slot.sh, спека шага 2 § 4.2).
#
# Стенд свой: каталог mktemp, свой install.conf (LOG_DIR стенда) и harness.conf
# (HEAVY_SLOTS); замки мест и журнал событий — под стендом, боевые не трогаются.
# Двери мутаций: HEAVY_SLOT_SH — обёртка; HEAVY_SLOT_GATES, HEAVY_SLOT_PRECOMMIT,
# HEAVY_SLOT_ACCEPTANCE — вызывающие, которые обязаны идти через пул; HEAVY_SLOT_PUBLISH —
# выпуск, который ставит метку (и), HEAVY_SLOT_RELEASE_LIB — её библиотека.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
SLOT="${HEAVY_SLOT_SH:-$HERE/heavy-slot.sh}"
GATES="${HEAVY_SLOT_GATES:-$HERE/gates.sh}"
PRECOMMIT="${HEAVY_SLOT_PRECOMMIT:-$HERE/pre-commit-gate.sh}"
ACCEPTANCE="${HEAVY_SLOT_ACCEPTANCE:-$HERE/acceptance.py}"
PUBLISH="${HEAVY_SLOT_PUBLISH:-$HERE/publish.sh}"
RELEASE_LIB="${HEAVY_SLOT_RELEASE_LIB:-$HERE/lib/release_mark.sh}"
source "$HERE/lib/selftest_cases.sh"

T=$(mktemp -d -t proba-heavy-slot-XXXXXX)
PIDS=()
cleanup() {
    local p
    for p in ${PIDS[@]+"${PIDS[@]}"}; do kill -KILL -- "-$p" 2>/dev/null; kill -KILL "$p" 2>/dev/null; done
    rm -rf "$T"
}
trap cleanup EXIT

mkdir -p "$T/log"
printf 'LOG_DIR="%s"\n' "$T/log" > "$T/install.conf"
printf 'HEAVY_SLOTS=2\n' > "$T/harness.conf"
unset HEAVY_SLOT_HELD HARNESS_IN_CHECKS_SLICE HEAVY_SLOTS HARNESS_LOG_DIR HARNESS_EVENTS_STATE HARNESS_RELEASE_PID
export HARNESS_RELEASE_MARK="$T/release.mark"
export HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" LOG_DIR="$T/log"
export HARNESS_TASK=hr.proba-heavy-slot
# Проба сама бывает в срезе (ночные ворота): срез стенда назван файлом, а не машиной.
echo '0::/system.slice/proba.service' > "$T/cgroup-off"
export HARNESS_CHECKS_SLICE_CGROUP_FILE="$T/cgroup-off"
EVENTS="$T/log/heavy-slots.jsonl"

slot() { bash "$SLOT" -- "$@"; }

# Держатель единственного места в своей группе процессов; ждёт, пока место взято.
hold_one() {  # $1 = секунд держать; ставит HOLDER (pid обёртки)
    HEAVY_SLOTS=1 setsid bash "$SLOT" -- sleep "$1" </dev/null >/dev/null 2>&1 &
    HOLDER=$!
    PIDS+=("$HOLDER")
    local i
    for i in $(seq 1 40); do
        [ -e "$T/log/heavy-slots/1.lock" ] && ! flock -n "$T/log/heavy-slots/1.lock" true 2>/dev/null && return 0
        sleep 0.1
    done
    echo "  ПЛОХО стенд: держатель не взял место за 4 с" >&2
}

echo "(а) HEAVY_SLOTS=2, пять команд по 3 с"
start=$(date +%s.%N)
for n in 1 2 3 4 5; do
    slot bash -c 'printf "s %s\n" "$(date +%s.%N)" >> "$1"; sleep 3; printf "e %s\n" "$(date +%s.%N)" >> "$1"' _ "$T/trace" &
done
wait
wall=$(python3 -c 'import sys; print(float(sys.argv[2]) - float(sys.argv[1]))' "$start" "$(date +%s.%N)")
peak=$(python3 - "$T/trace" <<'PY'
import sys
moves = sorted((float(t), 1 if k == "s" else -1) for k, t in (l.split() for l in open(sys.argv[1])))
now = peak = 0
for _, d in sorted(moves, key=lambda m: (m[0], m[1])):
    now += d
    peak = max(peak, now)
print(peak)
PY
)
case_check "(а) одновременно не больше 2 и места оба в деле" 2 "$peak"
case_check "(а) пять команд по 3 с в 2 места — не быстрее 9 с" 1 "$(python3 -c 'import sys; print(int(float(sys.argv[1]) >= 9))' "$wall")"
case_check "(а) и не медленнее 13 с (места идут параллельно)" 1 "$(python3 -c 'import sys; print(int(float(sys.argv[1]) < 13))' "$wall")"
case_check "(а) строк хода 10 (все пять прошли)" 10 "$(wc -l < "$T/trace")"

echo "(б) вложенный вызов внутри места не ждёт"
HEAVY_SLOTS=1 timeout 10 bash "$SLOT" -- bash "$SLOT" -- true
case_check "(б) место 1, вложенный heavy-slot — код 0, без взаимной блокировки" 0 "$?"

echo "(в) в срезе — без места"
hold_one 30
HEAVY_SLOTS=1 timeout 3 bash "$SLOT" -- true 2>/dev/null
case_check "(в) контроль: место занято — вне среза ждёт (timeout 124)" 124 "$?"
HEAVY_SLOTS=1 HARNESS_IN_CHECKS_SLICE=1 timeout 3 bash "$SLOT" -- true
case_check "(в) HARNESS_IN_CHECKS_SLICE=1 — команда идёт сразу" 0 "$?"
echo '0::/harness.slice/harness-checks.slice/harness-nightly-gates.service' > "$T/cgroup"
HEAVY_SLOTS=1 HARNESS_CHECKS_SLICE_CGROUP_FILE="$T/cgroup" timeout 3 bash "$SLOT" -- true
case_check "(в) юнит в cgroup среза без метки (ночные ворота) — идёт сразу" 0 "$?"

echo "(г) убитый держатель освобождает место"
kill -KILL "$HOLDER" 2>/dev/null
sleep 0.3
HEAVY_SLOTS=1 timeout 5 bash "$SLOT" -- true 2>/dev/null
case_check "(г) KILL обёртки — место свободно, хотя её команда (sleep) жива" 0 "$?"
kill -KILL -- "-$HOLDER" 2>/dev/null

echo "(д) ожидание дольше 5 с — событие heavy.slot-wait"
rm -f "$EVENTS"
HEAVY_SLOTS=1 timeout 10 bash "$SLOT" -- true
case_check "(д) свободное место — событий нет" 0 "$(cat "$EVENTS" 2>/dev/null | grep -c heavy.slot-wait)"
hold_one 7
HEAVY_SLOTS=1 timeout 20 bash "$SLOT" -- echo метка-ожидания >/dev/null 2>&1
case_check "(д) ждавший получил место после держателя" 0 "$?"
verdict=$(python3 - "$EVENTS" <<'PY'
import json, sys
try:
    rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
except OSError:
    rows = []
rows = [r for r in rows if r.get("event") == "heavy.slot-wait"]
if len(rows) != 1:
    print(f"событий {len(rows)}")
    sys.exit()
r, d = rows[0], rows[0].get("data") or {}
bad = []
if not (5 <= int(d.get("секунд", 0)) <= 12):
    bad.append(f"секунд={d.get('секунд')}")
if "метка-ожидания" not in str(d.get("команда", "")):
    bad.append(f"команда={d.get('команда')!r}")
if r.get("task") != "hr.proba-heavy-slot":
    bad.append(f"task={r.get('task')}")
print("; ".join(bad) or "ок")
PY
)
case_check "(д) одно событие: секунды 5–12, команда, задача" ок "$verdict"

echo "(е) --не-дольше: держатель лока не ждёт место бесконечно"
rm -f "$EVENTS"
hold_one 30
s0=$(date +%s)
HEAVY_SLOTS=1 timeout 15 bash "$SLOT" --не-дольше 2 -- true 2>/dev/null
case_check "(е) место занято, предел 2 с — команда идёт без места, код 0" 0 "$?"
case_check "(е) и не раньше предела, не позже 6 с" 1 "$(( $(date +%s) - s0 >= 2 && $(date +%s) - s0 <= 6 ))"
case_check "(е) событие heavy.slot-overflow" 1 "$(grep -c '"heavy.slot-overflow"' "$EVENTS" 2>/dev/null)"

echo "(з) код команды и сигнал вызывающего доходят до команды"
# Место 1 держит (е): второе место свободно.
HEAVY_SLOTS=2 timeout 10 bash "$SLOT" -- bash -c 'exit 7' 2>/dev/null
case_check "(з) код команды 7 — код обёртки 7" 7 "$?"
# Срок сна свой у каждого прогона пробы: параллельный прогон не подменит ответ pgrep.
# TERM — одному pid обёртки (так гасит её вызывающий), не группе: timeout гасил бы группу
# целиком и пересылку не проверял.
NAP="31.$$"
HEAVY_SLOTS=2 bash "$SLOT" -- sleep "$NAP" 2>/dev/null &
wrapper=$!
sleep 0.5
kill -TERM "$wrapper"
wait "$wrapper" 2>/dev/null
sleep 0.3
case_check "(з) TERM обёртке — её команда (sleep) тоже кончилась" 0 "$(pgrep -fxc "sleep $NAP")"
pkill -fx "sleep $NAP" 2>/dev/null

echo "(ж) вызывающие идут через пул: при занятом месте ждут"
# Место занято держателем (hold_one выше): вызывающий обязан встать в ожидание.
caller_waits() {  # $1 = имя, дальше — команда
    local name="$1" out="$T/caller.out"; shift
    : > "$out"
    HEAVY_SLOTS=1 setsid "$@" </dev/null >"$out" 2>&1 &
    local pid=$!
    PIDS+=("$pid")
    sleep 2
    kill -KILL -- "-$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    case_check "(ж) $name ждёт место (вывод: $(head -c 120 "$out" | tr '\n' ' '))" 1 "$(grep -c 'жду своей очереди' "$out")"
}
caller_waits "gates.sh --selftest" bash "$GATES" --selftest
caller_waits "pre-commit-gate.sh --selftest-devmap" bash "$PRECOMMIT" --selftest-devmap
out=$(HEAVY_SLOTS=1 HEAVY_SLOT_COMMIT_WAIT_S=1 timeout 60 bash "$PRECOMMIT" --selftest-devmap 2>&1)
case_check "(ж) pre-commit под замком коммита ждёт не дольше HEAVY_SLOT_COMMIT_WAIT_S" 1 "$(grep -c 'иду без места' <<< "$out")"

verdict=$(python3 - "$ACCEPTANCE" "$SLOT" <<'PY'
import contextlib, importlib.util, os, sys, types
acc_path, slot = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(os.path.dirname(acc_path), "lib"))
spec = importlib.util.spec_from_file_location("acceptance_under_probe", acc_path)
acc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acc)
seen = []
stub = types.SimpleNamespace(
    settings=lambda root: {"окно_сбора_сек": 0, "задач": 1, "ждать_итога_мин": 1},
    request=lambda ids, command, *a, **k: (seen.append(command), (0, []))[1])
sys.modules["batch_queue"] = stub
with contextlib.redirect_stdout(sys.stderr):
    os.environ.pop("HEAVY_SLOT_HELD", None)
    acc._queued_mutations(["x"])
    os.environ["HEAVY_SLOT_HELD"] = "1"
    acc._queued_mutations(["x"])
free, held = seen
bad = []
if not (os.path.basename(free[1]) == "heavy-slot.sh" and free[2] == "--"):
    bad.append(f"без места заход не через heavy-slot: {free[:3]}")
if any(os.path.basename(a) == "heavy-slot.sh" for a in held):
    bad.append("место заявителя уже взято, а заход берёт второе")
print("; ".join(bad) or "ок")
PY
)
case_check "(ж) acceptance --мутации-задачи: заход через место; место заявителя — второе не берёт" ок "$verdict"

echo "(и) идёт выпуск: новым заявкам места нет, потомкам выпуска — есть"
# Выпуск стенда — живой процесс не из нашего рода; метку ставит библиотека от его имени.
sleep 60 >/dev/null 2>&1 &
REL=$!
PIDS+=("$REL")
bash -c 'source "$1"; release_mark_take "$2"' _ "$RELEASE_LIB" "$REL"
case_check "(и) метка выпуска: pid выпуска в первом поле" "$REL" "$(cut -d' ' -f1 "$HARNESS_RELEASE_MARK" 2>/dev/null)"
HEAVY_SLOTS=2 timeout 3 bash "$SLOT" -- true 2>"$T/rel.err"
case_check "(и) БОЛЬНОЙ СЛУЧАЙ: выпуск идёт, места свободны — новая заявка ждёт (timeout 124)" 124 "$?"
case_check "(и) ждущая заявка говорит, что ждёт выпуск" 1 "$(grep -c 'идёт выпуск' "$T/rel.err")"
HEAVY_SLOTS=2 HARNESS_RELEASE_PID="$REL" timeout 3 bash "$SLOT" -- true
case_check "(и) потомок выпуска (HARNESS_RELEASE_PID) берёт место сразу" 0 "$?"
HEAVY_SLOTS=2 HARNESS_RELEASE_PID=1 timeout 3 bash "$SLOT" -- true 2>/dev/null
case_check "(и) чужой HARNESS_RELEASE_PID — ждёт, как все" 124 "$?"
HEAVY_SLOTS=2 HEAVY_SLOT_HELD=1 timeout 3 bash "$SLOT" -- true
case_check "(и) идущий (место уже взято, HEAVY_SLOT_HELD) дослуживает" 0 "$?"
rm -f "$EVENTS"
HEAVY_SLOTS=2 timeout 15 bash "$SLOT" --не-дольше 2 -- true 2>/dev/null
case_check "(и) --не-дольше при выпуске уходит по сроку, код 0" 0 "$?"
case_check "(и) и событие heavy.slot-overflow" 1 "$(grep -c '"heavy.slot-overflow"' "$EVENTS" 2>/dev/null)"
# Чужой pid в той же метке: снять её может только сам выпуск.
bash -c 'source "$1"; release_mark_drop' _ "$RELEASE_LIB"
case_check "(и) снятие не своим процессом — метка цела" 1 "$([ -s "$HARNESS_RELEASE_MARK" ] && echo 1 || echo 0)"
# Тот же pid, другое время старта: pid выпуска занят новым процессом — выпуска нет.
printf '%s 1\n' "$REL" > "$HARNESS_RELEASE_MARK"
HEAVY_SLOTS=2 timeout 3 bash "$SLOT" -- true
case_check "(и) pid метки занят другим процессом (время старта не то) — выпуска нет" 0 "$?"
bash -c 'source "$1"; release_mark_take "$2"' _ "$RELEASE_LIB" "$REL"
kill -KILL "$REL" 2>/dev/null; wait "$REL" 2>/dev/null
HEAVY_SLOTS=2 timeout 3 bash "$SLOT" -- true
case_check "(и) выпуск умер, метка осталась — место выдаётся" 0 "$?"
out=$(bash -c 'source "$1"; release_mark_take; [ "$HARNESS_RELEASE_PID" = "$$" ] && echo pid; release_mark_drop; [ -e "$HARNESS_RELEASE_MARK" ] || echo снята' _ "$RELEASE_LIB" | paste -sd' ')
case_check "(и) взявший метку: HARNESS_RELEASE_PID = его pid; своё снятие убирает метку" "pid снята" "$out"
HEAVY_SLOTS=2 timeout 3 bash "$SLOT" -- true 2>"$T/rel.err"
case_check "(и) метки нет — место выдаётся молча (код:stderr)" "0:" "$?:$(head -c 120 "$T/rel.err")"
# Выпуск ставит метку до ворот и снимает её в общем обработчике выхода.
verdict=$(python3 - "$PUBLISH" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
bad = []
take = text.find("\nrelease_mark_take")
gates = text.find("\nbuild_in_scratch --заметки")
if take < 0 or gates < 0 or take > gates:
    bad.append("метка не берётся до заметок и ворот")
body = re.search(r"\ncleanup_on_exit\(\) \{\n(.*?)\n\}", text, re.S)
if not body or "release_mark_drop" not in body.group(1):
    bad.append("cleanup_on_exit метку не снимает")
if not re.search(r'\n(source|\.) [^\n]*/lib/release_mark\.sh"\n', text):
    bad.append("библиотека метки не подключена")
print("; ".join(bad) or "ок")
PY
)
case_check "(и) publish.sh: метка до ворот, снятие в cleanup_on_exit" ок "$verdict"

echo "(к) --место-очереди: своё место ворот очереди сверх HEAVY_SLOTS"
# БОЛЬНОЙ СЛУЧАЙ 21.11.2026: ворота очереди слияния ждали место пула, держа общий замок
# очереди, — за ними стояли все подачи (40 ожиданий за 6 суток, 5,0 ч, до 7020 с). Очередь
# одна, поэтому место у неё одно, своё (mq.lock); второй её вызов ждёт, как все.
hold_one 30
HEAVY_SLOTS=1 timeout 3 bash "$SLOT" -- true 2>/dev/null
case_check "(к) контроль: все места заняты — обычная заявка ждёт (timeout 124)" 124 "$?"
HEAVY_SLOTS=1 timeout 3 bash "$SLOT" --место-очереди -- true 2>/dev/null
case_check "(к) все места заняты — заявка с --место-очереди идёт сразу, код 0" 0 "$?"
HEAVY_SLOTS=1 timeout 3 bash "$SLOT" --не-дольше 60 --место-очереди -- bash -c 'exit 5' 2>/dev/null
case_check "(к) вместе с --не-дольше (так зовёт очередь) — сразу, код команды 5" 5 "$?"
HEAVY_SLOTS=1 setsid bash "$SLOT" --место-очереди -- sleep 30 </dev/null >/dev/null 2>&1 &
QHOLDER=$!
PIDS+=("$QHOLDER")
held=0
for i in $(seq 1 40); do
    [ -e "$T/log/heavy-slots/mq.lock" ] && ! flock -n "$T/log/heavy-slots/mq.lock" true 2>/dev/null && { held=1; break; }
    sleep 0.1
done
case_check "(к) держатель места очереди взял замок mq.lock" 1 "$held"
HEAVY_SLOTS=1 timeout 3 bash "$SLOT" --место-очереди -- true 2>/dev/null
case_check "(к) место очереди занято, обычные тоже — второй вызов с флагом ждёт (timeout 124)" 124 "$?"
kill -KILL -- "-$QHOLDER" 2>/dev/null
kill -KILL -- "-$HOLDER" 2>/dev/null
sleep 0.3

echo "heavy-slot: путей $CASES_PATHS, неудач $CASES_FAILED"
[ "$CASES_FAILED" = 0 ]
