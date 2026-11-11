#!/usr/bin/env bash
# Проба Ф2 (волна 1 спеки hr.watchers-report-must-create-model-task): пять
# сторожей, что шли «только владельцу», заводят задачу модели и гасят её на
# зелёном.
#
# Что судится на каждом стенде (К-10 спеки, §8 Ф2):
#   * беда → зов двери `tg_send.sh --task <ключ>` (или писателя `--key`)
#     РОВНО с ключом причины, а не текста;
#   * повтор той же беды — тот же ключ: в стенд-карте одна карточка, в журнале
#     писателя `повтор`;
#   * зелёное → `alert-task.py --resolve <ключ>`, после него `--is-green` 0;
#     ключ, поднятый в этом же прогоне, не гасится;
#   * summary карточки несёт команду перезапуска сторожа;
#   * текст вести владельцу и код выхода сторожа ПОБАЙТНО прежние: эталон —
#     копии сторожей до правки (`before-w2/`), прогнанные на таком же стенде;
#   * писателя нет или он отказал — сторож не падает: код и вести как у эталона;
#   * боевая карта не тронута (sha1 до и после).
#
# Стенд: копия сторожа в своём дереве (оно же PROJECT_DIR), подставная дверь
# канала (пишет доводы и, как настоящая, зовёт писателя ДО отправки), обёртка
# писателя (пишет доводы и передаёт зов НАСТОЯЩЕМУ alert-task.py), своя карта,
# свой журнал, `env -i`. Bot API не зовётся вовсе: дверь подставная.
# Уборка диска гонится целиком, но docker, df, pgrep, npm/pip/uv и
# disk-report.py на стенде подставные — боевой docker и /tmp она не трогает.
#
# Двери для мутаций (копия механизма вместо живого файла):
#   HARNESS_NIGHTLY_GATES HARNESS_DISK_CLEANUP HARNESS_HEARTBEAT_WATCH
#   HARNESS_MEMORY_COMMIT HARNESS_SERVER_HYGIENE — файлы сторожей
#   HARNESS_ALERT_TASK — писатель; W2_BEFORE_DIR — каталог эталонов;
#   HARNESS_OWNER_SENDERS — данные отправителей (случай «волна 1 — card»).
#
# `--сторож <имя>` гонит один стенд (имя сторожа или `senders` — только данные
# отправителей): весь набор идёт минуты, а мутация судит один механизм.
#
# Прогон — только фоном: из-под сессии писатель отвечает «зов из сессии» (6).
# Имена переменных ЛАТИНИЦЕЙ: bash не берёт кириллические идентификаторы.
set -uo pipefail
export LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib/selftest_cases.sh"

DEMONS="$ROOT/harness/demons"
declare -A SRC=(
    [nightly-gates]="${HARNESS_NIGHTLY_GATES:-$DEMONS/nightly-gates.sh}"
    [disk-cleanup]="${HARNESS_DISK_CLEANUP:-$DEMONS/disk-cleanup.sh}"
    [heartbeat-watch]="${HARNESS_HEARTBEAT_WATCH:-$DEMONS/heartbeat-watch.sh}"
    [memory-commit]="${HARNESS_MEMORY_COMMIT:-$DEMONS/memory-commit.sh}"
    [server-hygiene]="${HARNESS_SERVER_HYGIENE:-$DEMONS/server-hygiene.sh}"
)
WRITER="${HARNESS_ALERT_TASK:-$HERE/alert-task.py}"
SENDERS_DATA="${HARNESS_OWNER_SENDERS:-$ROOT/harness/config/owner-senders.yaml}"
ONLY=""
if [ "${1:-}" = "--сторож" ]; then
    ONLY="${2:-}"
    case "$ONLY" in
        nightly-gates|disk-cleanup|heartbeat-watch|memory-commit|server-hygiene|senders) ;;
        *) echo "ОТКАЗ: --сторож «$ONLY» — нет такого стенда"; exit 2 ;;
    esac
fi
want() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }
BEFORE="${W2_BEFORE_DIR:-$ROOT/.tasks/hr.watchers-report-must-create-model-task/before-w2}"
LIVE_MAP="$ROOT/dev-map.yaml"
LIVE_SHA=$(sha1sum "$LIVE_MAP" 2>/dev/null | cut -d' ' -f1)

T=$(mktemp -d -t proba-watchers-wave2-XXXXXX)
trap 'rm -rf "$T"' EXIT

# ── судья: разбор журналов двери и писателя ────────────────────────────────
cat > "$T/judge.py" <<'PY'
import json, re, sys
from pathlib import Path


def calls(path):
    found = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return found
    for line in lines:
        if line.strip():
            try:
                found.append(json.loads(line))
            except ValueError:
                found.append(["<битая строка>"])
    return found


def after(argv, flags):
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a in flags]


def tasks(map_path, key):
    import yaml
    data = yaml.safe_load(Path(map_path).read_text(encoding="utf-8")) or {}
    return [t for line in data.get("lines") or [] for t in line.get("tasks") or []
            if t.get("источник") == f"тревога:{key}"]


def yaml_load(path):
    import yaml
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def main(cmd, *a):
    if cmd == "keys":          # door alert → ключи беды: --task у двери, --key у писателя
        got = {k for argv in calls(a[0]) for k in after(argv, ("--task", "--задача"))}
        got |= {k for argv in calls(a[1]) for k in after(argv, ("--key",))}
        print(" ".join(sorted(got)))
    elif cmd == "resolved":    # alert → ключи, погашенные --resolve
        print(" ".join(sorted({k for argv in calls(a[0]) for k in after(argv, ("--resolve",))})))
    elif cmd == "minus":       # «a b» «b» → чего из первого нет во втором
        print(" ".join(sorted(set(a[0].split()) - set(a[1].split()))))
    elif cmd == "both":
        print(" ".join(sorted(set(a[0].split()) & set(a[1].split()))))
    elif cmd == "texts":       # door base → вести владельцу без пары --task K
        for argv in calls(a[0]):
            rest, skip = [], False
            for arg in argv:
                if skip:
                    skip = False
                    continue
                if arg in ("--task", "--задача"):
                    skip = True
                    continue
                rest.append(arg)
            text = "\x1f".join(rest).replace(a[1], "<СТЕНД>")
            print(json.dumps(re.sub(r"метка \d+ мин", "метка N мин", text), ensure_ascii=False))
    elif cmd == "file-with-task":
        print(sum(1 for argv in calls(a[0]) if "--file" in argv
                  and ("--task" in argv or "--задача" in argv)))
    elif cmd == "cards":
        print(len(tasks(a[0], a[1])))
    elif cmd == "cards-prefix":   # map prefix → карточек, чей ключ начинается с prefix
        print(sum(1 for line in (yaml_load(a[0]).get("lines") or []) for t in line.get("tasks") or []
                  if str(t.get("источник", "")).startswith(f"тревога:{a[1]}")))
    elif cmd == "summary":
        found = tasks(a[0], a[1])
        print(found[0].get("summary", "") if found else "")
    elif cmd == "events":
        rows = []
        for line in Path(a[0]).read_text(encoding="utf-8").splitlines() if Path(a[0]).exists() else []:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("ключ") == a[1]:
                rows.append(row.get("событие", ""))
        print(",".join(rows))
    elif cmd == "senders":     # data gate root files… → записи ключей, что сторожа поднимают
        import importlib.util, yaml
        spec = importlib.util.spec_from_file_location("senders_gate", a[1])
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        data = yaml.safe_load(Path(a[0]).read_text(encoding="utf-8")) or {}
        found, not_card = 0, []
        for path in a[3:]:
            rel = str(Path(path).resolve().relative_to(Path(a[2]).resolve()))
            literals = set(gate.ПОДНЯТЬ_КЛЮЧ.findall(Path(path).read_text(encoding="utf-8")))
            for rec in (data.get("model_task") or {}).get(rel) or []:
                if rec.get("key") and any(gate._ключ_подходит(l, rec["key"]) for l in literals):
                    found += 1
                    if rec.get("kind") != "card":
                        not_card.append(f"{rec['key']}={rec.get('kind')}")
        debt = sum(1 for recs in (data.get("model_task") or {}).values()
                   for rec in recs or [] if rec.get("kind") == "debt")
        ceiling = data.get("debt_max")
        print(f"записей {found}, не card: {' '.join(sorted(not_card)) or '-'}")
        print("долг не выше потолка" if isinstance(ceiling, int) and debt <= ceiling
              else f"долг {debt} выше потолка {ceiling}")
    elif cmd == "claude-ancestors":
        n, pid = 0, int(a[0])
        while pid > 1:
            try:
                status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
            except OSError:
                break
            fields = dict(l.split(":\t", 1) for l in status.splitlines() if ":\t" in l)
            n += fields.get("Name", "").strip() == "claude"
            pid = int(fields.get("PPid", "0").strip() or 0)
        print(n)


main(*sys.argv[1:])
PY
judge() { python3 "$T/judge.py" "$@"; }

# Писатель, позванный из-под сессии, отвечает 6 и ничего не пишет: проба судила
# бы отказ, а не сторожа. Отрыв `setsid` делает сироту не сразу.
for _ in $(seq 1 50); do
    [ "$(judge claude-ancestors $$)" = 0 ] && break
    sleep 0.2
done
if [ "$(judge claude-ancestors $$)" != 0 ]; then
    echo "ОТКАЗ: проба идёт под процессом claude — писатель ответит «зов из сессии»."
    echo "Гнать фоном: setsid nohup bash scripts/test_watchers_wave2.sh > /var/log/harness/<имя>.log 2>&1 < /dev/null &"
    exit 2
fi

# ── стенд ──────────────────────────────────────────────────────────────────
build_stand() {  # $1 = каталог, $2 = файл сторожа, $3 = имя, $4 = режим писателя real|absent|refuse
    local s="$1" src="$2" name="$3" mode="$4" tool
    mkdir -p "$s/tree/harness/demons" "$s/tree/scripts/lib" "$s/tree/docs" \
             "$s/log/locks/services" "$s/hb" "$s/home" "$s/bin" "$s/panel" "$s/state" \
             "$s/secrets" "$s/steps"
    printf '%s\n' "$name" > "$s/name"
    printf '%s\n' "$mode" > "$s/mode"
    cp "$src" "$s/tree/harness/demons/$name.sh"
    cp "$HERE/lib/config.sh" "$HERE/lib/config.py" "$s/tree/scripts/lib/"

    cat > "$s/tree/scripts/tg_send.sh" <<'DOOR'
#!/usr/bin/env bash
# Подставная дверь: пишет свой ряд доводов; на --task K, как настоящая
# (tg_send.sh: писатель ДО отправки, заголовок — первая строка ≤110), зовёт
# писателя рядом с собой. Код — доставки: 0, текст — STAND_DOOR_TEXT_CODE.
python3 -c 'import json,sys; open(sys.argv[1],"a",encoding="utf-8").write(json.dumps(sys.argv[2:],ensure_ascii=False)+"\n")' \
    "$STAND_DOOR_LOG" "$@"
key=""; rest=()
while [ $# -gt 0 ]; do
    case "$1" in
        --task|--задача) key="${2:-}"; shift 2; continue ;;
        *) rest+=("$1") ;;
    esac
    shift
done
set -- "${rest[@]}"
is_file=0; text="${1:-}"
if [ "${1:-}" = "--file" ]; then is_file=1; text=""; [ "${3:-}" = "--caption" ] && text="${4:-}"; fi
writer="$(dirname "$0")/alert-task.py"
if [ -n "$key" ] && [ -n "$text" ] && [ -f "$writer" ]; then
    title="${text%%$'\n'*}"
    python3 "$writer" --key "$key" --title "${title:0:110}" --text "$text" </dev/null >/dev/null 2>&1 || true
fi
[ "$is_file" = 0 ] && [ -n "${STAND_DOOR_TEXT_CODE:-}" ] && exit "$STAND_DOOR_TEXT_CODE"
exit 0
DOOR
    chmod +x "$s/tree/scripts/tg_send.sh"

    if [ "$mode" != absent ]; then
        cat > "$s/tree/scripts/alert-task.py" <<'WRAP'
#!/usr/bin/env python3
"""Обёртка писателя стенда: след доводов, затем зов настоящего alert-task.py."""
import json, os, sys
with open(os.environ["STAND_ALERT_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\n")
if os.environ.get("STAND_WRITER_MODE") == "refuse":
    sys.exit(1)
os.execv(sys.executable, [sys.executable, os.environ["STAND_REAL_WRITER"], *sys.argv[1:]])
WRAP
        chmod +x "$s/tree/scripts/alert-task.py"
    fi

    # Подставные инструменты машины: уборка диска на стенде не трогает боевое.
    printf '#!/usr/bin/env bash\nprintf "vyzvan\\n" >> "%s/curl.log"\nprintf %s\n' "$s" \
        "'{\"ok\":true,\"result\":{\"message_id\":1}}'" > "$s/bin/curl"
    for tool in docker npm pip uv; do printf '#!/usr/bin/env bash\nexit 0\n' > "$s/bin/$tool"; done
    printf '#!/usr/bin/env bash\nexit 1\n' > "$s/bin/pgrep"
    cat > "$s/bin/df" <<'DF'
#!/usr/bin/env bash
case "$*" in
    *pcent*) printf 'Use%%\n %s%%\n' "${STAND_DF_PCT:-40}" ;;
    *used*)  printf ' Used\n 1000000\n' ;;
    *)       exec /usr/bin/df "$@" ;;
esac
DF
    chmod +x "$s/bin/"*

    cat > "$s/tree/dev-map.yaml" <<'MAP'
version: 1
layers:
  - { id: infra, label: "Инфраструктура", color: "#5aa06a" }
queue: []
lines:
  - id: harness
    layer: infra
    name: "Харнес"
    tasks:

      - id: hr.sample-one
        status: plan
        owner_testable: true
        заведено: "2026-10-01"
        title: "образец карточки"
        summary: "образец"
        volume: { loc: 10, complexity: 1, tokens_k: 5, estimated: true }
        when: { started: null, done: null, tested: null, estimated: true }
MAP
    printf 'version: 1\nlines:\n  - id: harness\n    name: "Харнес"\n    tasks: []\n' \
        > "$s/tree/docs/dev-map-archive.yaml"
    python3 -c "import secrets,sys; open(sys.argv[1],'w').write('11'+secrets.token_hex(16)+'\n')" "$s/token"
    cat > "$s/install.conf" <<CONF
PROJECT_NAME="proba-w2"
PROJECT_DIR="$s/tree"
AGENT_USER="proba"
LOG_DIR="$s/log"
HEARTBEAT_DIR="$s/hb"
STATE_DIR="$s/state"
SECRETS_DIR="$s/secrets"
TMUX_SESSION="proba-w2"
AGENT_START_CMD="true"
TG_CHAT_ID="100500"
TG_TOKEN_FILE="$s/token"
TG_CHANNEL_VARIANT="A"
AUTONOMY="semi"
CONF
    cat > "$s/harness.conf" <<'CONF'
ACTIVE_LINE="harness"
ALERT_TASKS_PER_WATCHER="3"
ALERT_MASS_MIN="3"
ALERT_LOCK_WAIT_SEC="2"
DEVMAP_SUMMARY_MAX="500"
SERVICE_LOCKS_SUBDIR="services"
TMP_SWEEP_ENABLED="0"
DOCKER_SWEEP_ENABLED="0"
DISK_MAX_PCT="85"
DISK_TRIGGER_PCT="80"
MEMORY_COMMIT_FAILS_MAX="3"
HEARTBEAT_PERIODS="alpha:10 beta:10 gamma:10"
CONF
}

stand_env() {  # $1 = стенд; печатает окружение стенда по строке
    local s="$1"
    printf '%s\n' "PATH=$s/bin:/usr/local/bin:/usr/bin:/bin" "HOME=$s/home" "LC_ALL=C.UTF-8" \
        "PYTHONDONTWRITEBYTECODE=1" "HARNESS_HOME=$s/home" \
        "HARNESS_INSTALL_CONF=$s/install.conf" "HARNESS_CONF=$s/harness.conf" \
        "HARNESS_DEVMAP=$s/tree/dev-map.yaml" "DEVMAP_ARCHIVE=$s/tree/docs/dev-map-archive.yaml" \
        "HARNESS_ACTIVE_LINE=harness" "HARNESS_DOSKA=$s/log/доска.json" \
        "HARNESS_PANEL_STATE=$s/panel" \
        "HARNESS_TG_SEND=$s/tree/scripts/tg_send.sh" "HARNESS_ALERT_TASK=$s/tree/scripts/alert-task.py" \
        "STAND_REAL_WRITER=$WRITER" "STAND_WRITER_MODE=$(cat "$s/mode")"
}

run_step() {  # $1 = стенд, $2 = шаг, дальше ПЕРЕМЕННАЯ=значение…, «--», доводы сторожа
    local s="$1" step="$2" rc=0 line
    shift 2
    local extra=() envv=()
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do extra+=("$1"); shift; done
    [ "${1:-}" = "--" ] && shift
    while IFS= read -r line; do envv+=("$line"); done < <(stand_env "$s")
    : > "$s/steps/$step.door"; : > "$s/steps/$step.alert"
    ( cd "$s/home" && env -i "${envv[@]}" "STAND_DOOR_LOG=$s/steps/$step.door" \
        "STAND_ALERT_LOG=$s/steps/$step.alert" "${extra[@]}" \
        timeout 180 bash "$s/tree/harness/demons/$(cat "$s/name").sh" "$@" ) \
        > "$s/steps/$step.out" 2>&1 || rc=$?
    printf '%s\n' "$rc" > "$s/steps/$step.rc"
}

is_green() {  # $1 = стенд, $2 = ключ; печатает код настоящего писателя
    local s="$1" rc=0 line envv=()
    while IFS= read -r line; do envv+=("$line"); done < <(stand_env "$s")
    env -i "${envv[@]}" python3 "$WRITER" --is-green "$2" >/dev/null 2>&1 || rc=$?
    printf '%s' "$rc"
}

# ── сценарии сторожей: одинаковые шаги для эталона и для проверяемого ──────
scenario_nightly-gates() {
    local s="$1"
    run_step "$s" n1-red NIGHTLY_GATES_CMD="printf '%s\n' '  ПЛОХО · check-secrets (код 1)' '=== итог: зелёных 99, красных 1 ==='" --
    run_step "$s" n2-red-again NIGHTLY_GATES_CMD="printf '%s\n' '  ПЛОХО · test_guard_bash (код 1)' '  ПЛОХО · check-registry (код 2)' '=== итог: зелёных 98, красных 2 ==='" --
    run_step "$s" n3-broken NIGHTLY_GATES_CMD="true" --
    run_step "$s" n4-green NIGHTLY_GATES_CMD="printf '%s\n' '=== итог: зелёных 100, красных 0 ==='" --
}

scenario_disk-cleanup() {
    local s="$1"
    run_step "$s" d1-full STAND_DF_PCT=91 STAND_REPORT=vest --
    run_step "$s" d2-full-again STAND_DF_PCT=92 STAND_REPORT=vest --
    run_step "$s" d3-report-broken STAND_DF_PCT=91 STAND_REPORT=broken --
    touch "$s/hb/disk-alert"
    run_step "$s" d4-pressure-rearm STAND_DF_PCT=50 STAND_REPORT=rearm -- --по-давлению
    run_step "$s" d5-full-pressure STAND_DF_PCT=91 STAND_REPORT=vest -- --по-давлению
    run_step "$s" d6-night-rearm STAND_DF_PCT=50 STAND_REPORT=rearm --
}

scenario_heartbeat-watch() {
    local s="$1" i
    touch -d '30 days ago' "$s/hb/.install-stamp" "$s/hb/alpha"
    touch "$s/hb/beta" "$s/hb/gamma"
    run_step "$s" h1-one-dead --
    run_step "$s" h2-one-dead-again --
    touch "$s/hb/alpha"
    run_step "$s" h3-all-alive --
    sed -i "s/^HEARTBEAT_PERIODS=.*/HEARTBEAT_PERIODS=\"$(for i in $(seq -w 1 27); do printf 'd%s:10 ' "$i"; done)\"/" \
        "$s/harness.conf"
    run_step "$s" h4-mass --
    run_step "$s" h5-mass-again --
    for i in $(seq -w 1 27); do touch "$s/hb/d$i"; done
    run_step "$s" h6-mass-alive --
}

scenario_memory-commit() {
    local s="$1" p="$1/tree"
    cp "$HERE/board.py" "$HERE/pause.py" "$p/scripts/"
    mkdir -p "$p/память"
    git -C "$p" init -q
    git -C "$p" config user.email "proba@stend"; git -C "$p" config user.name "проба"
    printf 'начало\n' > "$p/README.md"; printf 'первая редакция\n' > "$p/память/запись.md"
    git -C "$p" add README.md память; git -C "$p" commit -q -m "начало"
    red_gate() {
        printf '#!/usr/bin/env bash\necho "[pre-commit] GATE FAIL: проба_гейта"\nexit 1\n' \
            > "$p/.git/hooks/pre-commit"
        chmod +x "$p/.git/hooks/pre-commit"
    }
    red_gate
    printf 'правка с панели\n' > "$p/память/запись.md"
    run_step "$s" m1-first-fail --
    run_step "$s" m2-second-fail --
    run_step "$s" m3-max-fail --
    rm -f "$p/.git/hooks/pre-commit"
    run_step "$s" m4-committed --
    # Гашение судится СРАЗУ после m4: m5 снова поднимает беду, и итог прогона красный.
    is_green "$s" memory-commit:blocked > "$s/steps/m4-committed.green"
    red_gate
    printf 'вторая правка\n' > "$p/память/запись.md"
    run_step "$s" m5-fail-again --
}

scenario_server-hygiene() {
    local s="$1"
    cat > "$s/tree/harness/demons/server_hygiene_check.py" <<'CHECK'
import os, sys
mode = os.environ.get("STAND_CHECK", "clean")
if mode == "clean":
    print("Гигиена: диск 41%, своп 0% — в норме"); sys.exit(0)
if mode == "broken":
    print("Traceback (стенд): проверка сломана"); sys.exit(3)
n = 1 if mode == "viol1" else 2
print("Гигиена: диск 41%, своп 0%")
for i in range(1, n + 1):
    print(f"• app-svc{i}: нет потолка памяти (mem_limit)")
sys.exit(1)
CHECK
    run_step "$s" y1-violations STAND_CHECK=viol1 --
    run_step "$s" y2-violations-again STAND_CHECK=viol2 --
    run_step "$s" y3-check-broken STAND_CHECK=broken --
    run_step "$s" y4-clean STAND_CHECK=clean --
    run_step "$s" y5-violations-attachment STAND_CHECK=viol1 STAND_DOOR_TEXT_CODE=1 --
}

disk_report_stub() {  # уборка зовёт disk-report.py рядом с собой — на стенде подставной
    cat > "$1/tree/scripts/disk-report.py" <<'REPORT'
import os, sys
mode = os.environ.get("STAND_REPORT", "quiet")
if "--отправлено" in sys.argv:
    sys.exit(0)
if mode == "vest":
    print("весть: пересечение порога")
    print("Диск занят после уборки (стенд): нужен разбор.")
    sys.exit(0)
if mode == "rearm":
    print("перевзвод: диск ниже порога"); sys.exit(3)
if mode == "broken":
    print("Traceback (стенд): решение тревоги сломано"); sys.exit(1)
if mode == "below":   # как настоящий disk-report: ниже порога и метки disk-alert нет
    print("ниже порога"); sys.exit(3)
print("молчу: весть уже ушла"); sys.exit(3)
REPORT
}

# Прогон сценария сторожа в четырёх стендах: эталон, проверяемый, без писателя,
# писатель отказывает.
run_watcher() {  # $1 = имя сторожа
    local name="$1" kind src
    for kind in base cur absent refuse; do
        src="${SRC[$name]}"; [ "$kind" = base ] && src="$BEFORE/$name.sh"
        local mode=real
        case "$kind" in absent) mode=absent ;; refuse) mode=refuse ;; esac
        build_stand "$T/$name-$kind" "$src" "$name" "$mode"
        [ "$name" = disk-cleanup ] && disk_report_stub "$T/$name-$kind"
        "scenario_$name" "$T/$name-$kind"
    done
}

# ── случаи ─────────────────────────────────────────────────────────────────
steps_of() { ls "$T/$1-base/steps/" | sed -n 's/\.rc$//p' | sort; }

judge_same_as_base() {  # $1 = имя, $2 = вид стенда, $3 = подпись
    local name="$1" kind="$2" label="$3" step bad=""
    for step in $(steps_of "$name"); do
        [ "$(cat "$T/$name-$kind/steps/$step.rc")" = "$(cat "$T/$name-base/steps/$step.rc")" ] \
            || bad="$bad $step:код"
        [ "$(judge texts "$T/$name-$kind/steps/$step.door" "$T/$name-$kind")" \
          = "$(judge texts "$T/$name-base/steps/$step.door" "$T/$name-base")" ] \
            || bad="$bad $step:текст"
    done
    case_check "$name: $label — коды и вести владельцу как у эталона" "" "${bad# }"
}

raised() { judge keys "$T/$1-cur/steps/$2.door" "$T/$1-cur/steps/$2.alert"; }
resolved() { judge resolved "$T/$1-cur/steps/$2.alert"; }

expect_raised() {  # $1 = имя, $2 = шаг, $3 = ключи
    case_check "$1 $2: беда зовёт дверь с ключом «$3»" "$3" "$(raised "$1" "$2")"
}
expect_resolved() {  # $1 = имя, $2 = шаг, $3 = ключи
    case_check "$1 $2: зелёное гасит «$3»" "" "$(judge minus "$3" "$(resolved "$1" "$2")")"
}
expect_card() {  # $1 = имя, $2 = ключ, $3 = журнал ждём (события через запятую)
    local s="$T/$1-cur"
    case_check "$1: по ключу $2 одна карточка в стенд-карте" "1" \
        "$(judge cards "$s/tree/dev-map.yaml" "$2" 2>/dev/null || echo ошибка)"
    case_check "$1: журнал писателя по $2" "$3" "$(judge events "$s/log/тревоги-сторожей.jsonl" "$2")"
    case_check "$1: summary карточки $2 несёт перезапуск сторожа" "да" \
        "$(judge summary "$s/tree/dev-map.yaml" "$2" 2>/dev/null \
           | grep -qF "Перепроверить сторожем: bash harness/demons/$1.sh" && echo да || echo нет)"
}
expect_green() {  # $1 = имя, $2 = ключ
    case_check "$1: после зелёного --is-green $2 — 0" "0" "$(is_green "$T/$1-cur" "$2")"
}
no_self_resolve() {  # ключ, поднятый в шаге, в том же шаге не гасится
    local name="$1" step
    for step in $(steps_of "$name"); do
        case_check "$name $step: поднятое не гасится тем же прогоном" "" \
            "$(judge both "$(raised "$name" "$step")" "$(resolved "$name" "$step")")"
    done
}

[ -d "$BEFORE" ] || echo "ВНИМАНИЕ: каталога эталонов нет: $BEFORE"

# Контроль стенда: цепочка «дверь --task → обёртка → настоящий писатель» сама
# заводит карточку и гасит её. Без него красные случаи ниже судили бы стенд.
echo "── контроль стенда ──"
build_stand "$T/control" "${SRC[nightly-gates]}" nightly-gates real
CTRL_ENV=()
while IFS= read -r line; do CTRL_ENV+=("$line"); done < <(stand_env "$T/control")
env -i "${CTRL_ENV[@]}" STAND_DOOR_LOG="$T/control/door.log" STAND_ALERT_LOG="$T/control/alert.log" \
    bash "$T/control/tree/scripts/tg_send.sh" "Контроль стенда: беда" --task nightly-gates:control >/dev/null 2>&1
case_check "контроль: дверь стенда с --task завела карточку настоящим писателем" "1" \
    "$(judge cards "$T/control/tree/dev-map.yaml" nightly-gates:control 2>/dev/null || echo ошибка)"
case_check "контроль: summary несёт перезапуск сторожа" "да" \
    "$(judge summary "$T/control/tree/dev-map.yaml" nightly-gates:control 2>/dev/null \
       | grep -qF 'Перепроверить сторожем: bash harness/demons/nightly-gates.sh' && echo да || echo нет)"
env -i "${CTRL_ENV[@]}" STAND_ALERT_LOG="$T/control/alert.log" \
    python3 "$T/control/tree/scripts/alert-task.py" --resolve nightly-gates:control >/dev/null 2>&1
case_check "контроль: --resolve через обёртку → --is-green 0" "0" "$(is_green "$T/control" nightly-gates:control)"
case_check "контроль: без --resolve ключ не зелёный" "1" "$(is_green "$T/control" nightly-gates:never)"

# Шаги беды, где эталон зовёт дверь: пустой журнал двери значит, что сценарий
# не создал беду, и случаи ключей судили бы пустоту.
declare -A RED_STEP=([nightly-gates]=n1-red [disk-cleanup]=d1-full
    [heartbeat-watch]=h1-one-dead [memory-commit]=m1-first-fail [server-hygiene]=y1-violations)
# Коды эталона по шагам: стенд, уронивший сторожа своей поломкой, дал бы равные
# коды у эталона и проверяемого, и сверка «как у эталона» прошла бы впустую.
declare -A BASE_RC=([nightly-gates]="0 0 1 0" [disk-cleanup]="0 0 0 0 0 0"
    [heartbeat-watch]="0 0 0 0 0 0" [memory-commit]="1 1 1 0 1" [server-hygiene]="0 0 3 0 0")
base_rcs() { local st; for st in $(steps_of "$1"); do cat "$T/$1-base/steps/$st.rc"; done | paste -sd' ' -; }
for name in nightly-gates disk-cleanup heartbeat-watch memory-commit server-hygiene; do
    want "$name" || continue
    echo "── $name ──"
    case_check "$name: эталон до правки есть" "да" "$([ -f "$BEFORE/$name.sh" ] && echo да || echo нет)"
    run_watcher "$name"
    case_check "$name: беда создана — эталон в шаге ${RED_STEP[$name]} позвал дверь" "да" \
        "$([ -s "$T/$name-base/steps/${RED_STEP[$name]}.door" ] && echo да || echo нет)"
    case_check "$name: коды эталона по шагам — стенд не уронил сторожа" "${BASE_RC[$name]}" "$(base_rcs "$name")"
    judge_same_as_base "$name" cur "с писателем"
    judge_same_as_base "$name" absent "писателя НЕТ"
    judge_same_as_base "$name" refuse "писатель отказал (код 1)"
    no_self_resolve "$name"
done

if want nightly-gates; then
echo "── nightly-gates: ключи ──"
expect_raised nightly-gates n1-red "nightly-gates:red"
expect_raised nightly-gates n2-red-again "nightly-gates:red"
expect_raised nightly-gates n3-broken "nightly-gates:broken"
expect_raised nightly-gates n4-green ""
expect_resolved nightly-gates n4-green "nightly-gates:broken nightly-gates:red"
expect_card nightly-gates nightly-gates:red "заведена,повтор,зелёное"
expect_green nightly-gates nightly-gates:red
expect_green nightly-gates nightly-gates:broken
fi

if want disk-cleanup; then
echo "── disk-cleanup: ключи ──"
expect_raised disk-cleanup d1-full "disk-cleanup:full"
expect_raised disk-cleanup d2-full-again "disk-cleanup:full"
expect_raised disk-cleanup d3-report-broken "disk-cleanup:full"
expect_raised disk-cleanup d4-pressure-rearm ""
expect_resolved disk-cleanup d4-pressure-rearm "disk-cleanup:full"
expect_raised disk-cleanup d5-full-pressure "disk-cleanup:full"
expect_resolved disk-cleanup d6-night-rearm "disk-cleanup:full"
expect_card disk-cleanup disk-cleanup:full "заведена,повтор,повтор,зелёное,повтор,зелёное"
expect_green disk-cleanup disk-cleanup:full
fi

if want heartbeat-watch; then
echo "── heartbeat-watch: ключи ──"
expect_raised heartbeat-watch h1-one-dead "heartbeat-watch:dead:alpha"
expect_resolved heartbeat-watch h1-one-dead "heartbeat-watch:dead:beta heartbeat-watch:dead:gamma heartbeat-watch:mass"
expect_raised heartbeat-watch h2-one-dead-again "heartbeat-watch:dead:alpha"
expect_raised heartbeat-watch h3-all-alive ""
expect_resolved heartbeat-watch h3-all-alive "heartbeat-watch:dead:alpha heartbeat-watch:dead:beta heartbeat-watch:dead:gamma heartbeat-watch:mass"
expect_raised heartbeat-watch h4-mass "heartbeat-watch:mass"
expect_raised heartbeat-watch h5-mass-again "heartbeat-watch:mass"
expect_resolved heartbeat-watch h6-mass-alive "heartbeat-watch:mass"
expect_card heartbeat-watch heartbeat-watch:dead:alpha "заведена,повтор,зелёное"
expect_card heartbeat-watch heartbeat-watch:mass "заведена,повтор,зелёное"
case_check "heartbeat-watch: 27 мёртвых — поимённых карточек нет" "0" \
    "$(grep -c 'heartbeat-watch:dead:d' "$T/heartbeat-watch-cur/tree/dev-map.yaml")"
expect_green heartbeat-watch heartbeat-watch:dead:alpha
expect_green heartbeat-watch heartbeat-watch:mass
fi

if want memory-commit; then
echo "── memory-commit: ключи ──"
expect_raised memory-commit m1-first-fail "memory-commit:blocked"
for step in m2-second-fail m3-max-fail; do
    case_check "memory-commit $step: ключей, кроме memory-commit:blocked, нет" "" \
        "$(judge minus "$(raised memory-commit "$step")" "memory-commit:blocked")"
done
expect_resolved memory-commit m4-committed "memory-commit:blocked"
case_check "memory-commit m4: сразу после гашения --is-green memory-commit:blocked — 0" "0" \
    "$(cat "$T/memory-commit-cur/steps/m4-committed.green" 2>/dev/null)"
expect_raised memory-commit m5-fail-again "memory-commit:blocked"
case_check "memory-commit: по ключу одна карточка и после второй беды" "1" \
    "$(judge cards "$T/memory-commit-cur/tree/dev-map.yaml" memory-commit:blocked 2>/dev/null || echo ошибка)"
case_check "memory-commit: summary несёт перезапуск сторожа" "да" \
    "$(judge summary "$T/memory-commit-cur/tree/dev-map.yaml" memory-commit:blocked 2>/dev/null \
       | grep -qF 'Перепроверить сторожем: bash harness/demons/memory-commit.sh' && echo да || echo нет)"
case_check "memory-commit: журнал кончается повтором после зелёного" "зелёное,повтор" \
    "$(judge events "$T/memory-commit-cur/log/тревоги-сторожей.jsonl" memory-commit:blocked | grep -o 'зелёное,повтор$')"
fi

if want server-hygiene; then
echo "── server-hygiene: ключи ──"
expect_raised server-hygiene y1-violations "server-hygiene:violations"
expect_raised server-hygiene y2-violations-again "server-hygiene:violations"
expect_raised server-hygiene y3-check-broken "server-hygiene:check-broken"
expect_raised server-hygiene y4-clean ""
expect_resolved server-hygiene y4-clean "server-hygiene:check-broken server-hygiene:violations"
expect_card server-hygiene server-hygiene:violations "заведена,повтор,зелёное,повтор"
expect_raised server-hygiene y5-violations-attachment "server-hygiene:violations"
case_check "server-hygiene y5: вложение уходит без --task" "0" \
    "$(judge file-with-task "$T/server-hygiene-cur/steps/y5-violations-attachment.door")"
case_check "server-hygiene y5: вложение ушло (дверь отказала тексту)" "да" \
    "$(grep -q -- '--file' "$T/server-hygiene-cur/steps/y5-violations-attachment.door" && echo да || echo нет)"
# y5 поднял ключ снова после зелёного — проверка по зелёному шагу y4:
case_check "server-hygiene: после y4 ключ check-broken зелёный" "0" \
    "$(is_green "$T/server-hygiene-cur" server-hygiene:check-broken)"
fi

# Данные отправителей: каждая запись ключа, который сторож волны поднимает,
# — card, а не долг. Ключи берутся РАЗБОРОМ файлов сторожей (регуляркой ворот),
# не списком в пробе; «записей 8» не даёт пустому набору пройти.
if want senders; then
echo "── owner-senders: волна 1 — card ──"
SENDERS_OUT="$(judge senders "$SENDERS_DATA" "$ROOT/scripts/check-owner-senders.py" "$ROOT" \
    "$DEMONS/nightly-gates.sh" "$DEMONS/disk-cleanup.sh" "$DEMONS/heartbeat-watch.sh" \
    "$DEMONS/memory-commit.sh" "$DEMONS/server-hygiene.sh" 2>&1)"
case_check "owner-senders: ключи пяти сторожей — восемь записей, все card" "записей 8, не card: -" \
    "$(sed -n 1p <<< "$SENDERS_OUT")"
case_check "owner-senders: долг по записям не выше debt_max" "долг не выше потолка" \
    "$(sed -n 2p <<< "$SENDERS_OUT")"
fi

# ── круг 2 ревью: дыры, которые основные сценарии обходили (review-code-w2.md) ──
# Стенды только для проверяемого сторожа: эталон этих путей не знал вовсе.
xraised() { judge keys "$T/$1/steps/$2.door" "$T/$1/steps/$2.alert"; }
xresolved() { judge resolved "$T/$1/steps/$2.alert"; }

# R2-01: имя демона из живого HEARTBEAT_PERIODS — с точкой и кириллицей.
if want heartbeat-watch; then
echo "── R2-01 heartbeat-watch: имя «session-warden.запуск» ──"
S="$T/hb-cyr"
build_stand "$S" "${SRC[heartbeat-watch]}" heartbeat-watch real
CYR="session-warden.запуск"
sed -i "s/^HEARTBEAT_PERIODS=.*/HEARTBEAT_PERIODS=\"$CYR:10 beta:10\"/" "$S/harness.conf"
touch -d '30 days ago' "$S/hb/.install-stamp" "$S/hb/$CYR"
touch "$S/hb/beta"
run_step "$S" c1-dead --
run_step "$S" c2-dead-again --
K1="$(judge minus "$(xraised hb-cyr c1-dead)" "")"
K2="$(judge minus "$(xraised hb-cyr c2-dead-again)" "")"
case_check "R2-01 c1: беда зовёт писателя одним ключом heartbeat-watch:dead:…" "1" \
    "$(wc -w <<< "$K1" | tr -d ' ')"
case_check "R2-01 c1: ключ начинается с heartbeat-watch:dead:" "да" \
    "$(case "$K1" in heartbeat-watch:dead:?*) echo да ;; *) echo "нет: $K1" ;; esac)"
case_check "R2-01 c1: ключ по форме писателя (--is-green 1, не 2 «не по форме»)" "1" "$(is_green "$S" "${K1:-пусто}")"
case_check "R2-01 c1: в ключе только латиница и [:._-] (нормализован)" "да" \
    "$(LC_ALL=C grep -qE '^[a-z0-9][a-z0-9:._-]{0,79}$' <<< "$K1" && echo да || echo нет)"
case_check "R2-01 c2: повтор — тот же ключ" "$K1" "$K2"
case_check "R2-01: в стенд-карте одна карточка heartbeat-watch:dead:*" "1" \
    "$(judge cards-prefix "$S/tree/dev-map.yaml" heartbeat-watch:dead: 2>/dev/null || echo ошибка)"
# Живая метка — только после судов c1/c2: --is-green читает доску СЕЙЧАС, и
# после c3 открытая карточка (код 1) уже погашена (код 0).
touch "$S/hb/$CYR"
run_step "$S" c3-alive --
case_check "R2-01 c3: живая метка гасит тот же ключ" "" "$(judge minus "$K1" "$(xresolved hb-cyr c3-alive)")"
case_check "R2-01: журнал писателя по ключу" "заведена,повтор,зелёное" \
    "$(judge events "$S/log/тревоги-сторожей.jsonl" "$K1")"
case_check "R2-01 c3: --is-green ключа — 0" "0" "$(is_green "$S" "${K1:-пусто}")"
fi

# R2-02: карточка disk-cleanup:full заведена БЕЗ метки disk-alert (запасная весть
# при сломанном решении; весть не доставлена) — зелёный диск обязан её гасить.
if want disk-cleanup; then
echo "── R2-02 disk-cleanup: гашение без метки disk-alert ──"
S="$T/disk-nomark"
build_stand "$S" "${SRC[disk-cleanup]}" disk-cleanup real
disk_report_stub "$S"
run_step "$S" e1-broken-fallback STAND_DF_PCT=91 STAND_REPORT=broken --
case_check "R2-02 e1: запасная весть завела disk-cleanup:full" "disk-cleanup:full" "$(xraised disk-nomark e1-broken-fallback)"
case_check "R2-02: метки disk-alert нет (путь без метки)" "нет" "$([ -e "$S/hb/disk-alert" ] && echo есть || echo нет)"
run_step "$S" e2-below-night STAND_DF_PCT=50 STAND_REPORT=below --
case_check "R2-02 e2: ночь, диск 50%, без метки — гасит disk-cleanup:full" "" \
    "$(judge minus disk-cleanup:full "$(xresolved disk-nomark e2-below-night)")"
case_check "R2-02 e2: --is-green disk-cleanup:full — 0" "0" "$(is_green "$S" disk-cleanup:full)"
run_step "$S" e3-undelivered STAND_DF_PCT=91 STAND_REPORT=vest STAND_DOOR_TEXT_CODE=1 --
case_check "R2-02 e3: недоставленная весть завела карточку (дверь до отправки)" "disk-cleanup:full" \
    "$(xraised disk-nomark e3-undelivered)"
case_check "R2-02 e3: весть не доставлена — сторож 1" "1" "$(cat "$S/steps/e3-undelivered.rc")"
run_step "$S" e4-below-pressure STAND_DF_PCT=50 STAND_REPORT=below -- --по-давлению
case_check "R2-02 e4: почасовой, диск 50%, без метки — гасит disk-cleanup:full" "" \
    "$(judge minus disk-cleanup:full "$(xresolved disk-nomark e4-below-pressure)")"
case_check "R2-02 e4: --is-green disk-cleanup:full — 0" "0" "$(is_green "$S" disk-cleanup:full)"
fi

# R2-03: правку памяти закоммитила смена — «правок памяти нет» и есть зелёное.
if want memory-commit; then
echo "── R2-03 memory-commit: «правок памяти нет» гасит blocked ──"
S="$T/mem-shift"; P="$S/tree"
build_stand "$S" "${SRC[memory-commit]}" memory-commit real
cp "$HERE/board.py" "$HERE/pause.py" "$P/scripts/"
mkdir -p "$P/память"
git -C "$P" init -q
git -C "$P" config user.email "proba@stend"; git -C "$P" config user.name "проба"
printf 'начало\n' > "$P/README.md"; printf 'первая редакция\n' > "$P/память/запись.md"
git -C "$P" add README.md память; git -C "$P" commit -q -m "начало"
printf '#!/usr/bin/env bash\necho "[pre-commit] GATE FAIL: проба_гейта"\nexit 1\n' > "$P/.git/hooks/pre-commit"
chmod +x "$P/.git/hooks/pre-commit"
printf 'правка с панели\n' > "$P/память/запись.md"
run_step "$S" k1-fail --
case_check "R2-03 k1: отказ гейта завёл memory-commit:blocked" "memory-commit:blocked" "$(xraised mem-shift k1-fail)"
rm -f "$P/.git/hooks/pre-commit"
git -C "$P" add память; git -C "$P" commit -q -m "смена закоммитила память"
run_step "$S" k2-nothing --
case_check "R2-03 k2: сторож видит «правок памяти нет»" "да" \
    "$(grep -qF 'правок памяти нет' "$S/steps/k2-nothing.out" && echo да || echo нет)"
case_check "R2-03 k2: «правок нет» гасит memory-commit:blocked" "" \
    "$(judge minus memory-commit:blocked "$(xresolved mem-shift k2-nothing)")"
case_check "R2-03 k2: --is-green memory-commit:blocked — 0" "0" "$(is_green "$S" memory-commit:blocked)"
case_check "R2-03: журнал писателя по ключу" "заведена,зелёное" \
    "$(judge events "$S/log/тревоги-сторожей.jsonl" memory-commit:blocked)"
fi

echo "── боевое ──"
case_check "боевая карта не тронута (sha1)" "$LIVE_SHA" \
    "$(sha1sum "$LIVE_MAP" 2>/dev/null | cut -d' ' -f1)"
case_check "прямой curl мимо двери не звался ни на одном стенде" "0" \
    "$(cat "$T"/*/curl.log 2>/dev/null | grep -c . || true)"

printf '\nВОЛНА-2 СТОРОЖЕЙ: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
