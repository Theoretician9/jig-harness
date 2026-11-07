#!/usr/bin/env bash
# Проба волны 3 спеки hr.watchers-report-must-create-model-task: семь записей
# `kind: debt` в owner-senders.yaml. Сторож, что шлёт весть только владельцу,
# на беде зовёт дверь `tg_send.sh --task <демон>:<ключ>` (или писателя
# `alert-task.py --key`, где двери нет), на зелёном — `alert-task.py --resolve`.
#
# Что судится на каждом стенде (как в волне 2, scripts/test_watchers_wave2.sh):
#   * беда → ключ причины, а не текста; повтор беды — тот же ключ, одна карточка;
#   * зелёное → `--resolve`, после него `--is-green` 0; поднятое в этом же
#     прогоне не гасится;
#   * summary карточки несёт команду перезапуска сторожа;
#   * текст вестей и код выхода ПОБАЙТНО как у эталона (`before-w3/`, копии
#     сторожей с HEAD до правки), в том числе когда писателя нет или он отказал;
#   * боевая карта не тронута, прямой curl мимо двери не звался.
#
# Стенд — копия сторожа в своём дереве (оно же PROJECT_DIR), подставная дверь
# канала (пишет доводы и, как настоящая, зовёт писателя ДО отправки), обёртка
# писателя (след доводов → НАСТОЯЩИЙ alert-task.py), своя карта, `env -i`.
# Окружение каждого сторожа подставное: судья приёмки, валидатор карты, агент
# строителя, extensions.py, гейт гигиены, ручка здоровья, systemctl — заглушки
# стенда. Сторож сессий гоняется целиком на СВОЁМ сервере tmux (TMUX_TMPDIR
# стенда) с подставным агентом — копией sleep под именем claude.
#
# Сторожа, держащие лок карты весь прогон (acceptance-watch, check-builder):
# писатель, позванный изнутри, законно откладывает намерение (лок занят, код 4).
# Поэтому после каждого их шага стенд доносит очередь (`--drain`), как это
# делает sentinel, и только потом судит карточку.
#
# Двери для мутаций: HARNESS_ACCEPTANCE_WATCH HARNESS_CHECK_BUILDER
#   HARNESS_EXTENSIONS_WATCH HARNESS_MEMORY_REVISION HARNESS_PRODUCT_HEALTH
#   HARNESS_SENTINEL HARNESS_SESSION_WARDEN — файлы сторожей;
#   HARNESS_ALERT_TASK — писатель; W3_BEFORE_DIR — эталоны;
#   HARNESS_OWNER_SENDERS — данные отправителей.
# `--сторож <имя>` гонит один стенд (или `senders` — только данные).
#
# Прогон — только фоном: из-под сессии писатель отвечает «зов из сессии» (6).
# Имена переменных ЛАТИНИЦЕЙ.
set -uo pipefail
export LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1
# Переменная TMUX указывает клиенту на сокет ЖИВОГО сервера, если тест
# запущен из панели: без снятия любой голый tmux ушёл бы туда.
unset TMUX TMUX_PANE

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib/selftest_cases.sh"

DEMONS="$ROOT/harness/demons"
WATCHERS="acceptance-watch check-builder extensions-watch memory-revision product-health sentinel session-warden"
declare -A SRC=(
    [acceptance-watch]="${HARNESS_ACCEPTANCE_WATCH:-$DEMONS/acceptance-watch.sh}"
    [check-builder]="${HARNESS_CHECK_BUILDER:-$DEMONS/check-builder.sh}"
    [extensions-watch]="${HARNESS_EXTENSIONS_WATCH:-$DEMONS/extensions-watch.sh}"
    [memory-revision]="${HARNESS_MEMORY_REVISION:-$DEMONS/memory-revision.sh}"
    [product-health]="${HARNESS_PRODUCT_HEALTH:-$DEMONS/product-health.sh}"
    [sentinel]="${HARNESS_SENTINEL:-$DEMONS/sentinel.sh}"
    [session-warden]="${HARNESS_SESSION_WARDEN:-$DEMONS/session-warden.sh}"
)
WRITER="${HARNESS_ALERT_TASK:-$HERE/alert-task.py}"
SENDERS_DATA="${HARNESS_OWNER_SENDERS:-$ROOT/harness/config/owner-senders.yaml}"
SEED_CONF="$ROOT/harness/config/harness.conf"
ONLY=""
if [ "${1:-}" = "--сторож" ]; then
    ONLY="${2:-}"
    case " $WATCHERS senders " in
        *" $ONLY "*) ;;
        *) echo "ОТКАЗ: --сторож «$ONLY» — нет такого стенда"; exit 2 ;;
    esac
fi
want() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }
BEFORE="${W3_BEFORE_DIR:-$ROOT/.tasks/hr.watchers-report-must-create-model-task/before-w3}"
LIVE_MAP="$ROOT/dev-map.yaml"
LIVE_SHA=$(sha1sum "$LIVE_MAP" 2>/dev/null | cut -d' ' -f1)

T=$(mktemp -d)
cleanup() {
    local d
    for d in "$T"/*/tmux; do
        [ -d "$d" ] && env -u TMUX tmux -S "$d/tmux-$(id -u)/default" kill-server 2>/dev/null
    done
    rm -rf "$T"
}
trap cleanup EXIT

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


def normalize(text, stand):
    text = text.replace(stand, "<СТЕНД>")
    # Зелёный итог подъёма систем sentinel дописывает к докладу и без метки
    # перезагрузки; эталон этой строки не знал. Красный итог не вырезается.
    text = re.sub(r"\nподъём систем \(boot_check, код 0\): [^\n]*", "", text)
    # Минуты и секунды простоя считаются от часов прогона: эталон и
    # проверяемый идут друг за другом, и «45 мин» у одного — «46 мин» у другого.
    text = re.sub(r"\d+ мин", "N мин", text)
    # Строку журнала с часами sentinel несёт в доклад как есть.
    text = re.sub(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", "<ВРЕМЯ>", text)
    # Карточка, заведённая писателем шагом раньше, — лишняя задача в стенд-карте.
    text = re.sub(r"было \d+, стало \d+", "было N, стало M", text)
    text = re.sub(r"\d+ ?с\b", "Nс", text)
    return re.sub(r"(?<=[ (])\d+с(?=[ .,)])", "Nс", text)


def main(cmd, *a):
    if cmd == "keys":          # door alert → ключи беды: --task у двери, --key у писателя
        got = {k for argv in calls(a[0]) for k in after(argv, ("--task", "--задача"))}
        got |= {k for argv in calls(a[1]) for k in after(argv, ("--key",))}
        print(" ".join(sorted(got)))
    elif cmd == "key-of":      # door фраза → ключи тех зовов, чей текст несёт фразу
        print(" ".join(sorted({k for argv in calls(a[0]) if any(a[1] in arg for arg in argv)
                               for k in after(argv, ("--task", "--задача"))})))
    elif cmd == "resolved":
        print(" ".join(sorted({k for argv in calls(a[0]) for k in after(argv, ("--resolve",))})))
    elif cmd == "minus":
        print(" ".join(sorted(set(a[0].split()) - set(a[1].split()))))
    elif cmd == "both":
        print(" ".join(sorted(set(a[0].split()) & set(a[1].split()))))
    elif cmd == "texts":       # door stand → вести владельцу без пары --task K
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
            print(json.dumps(normalize("\x1f".join(rest), a[1]), ensure_ascii=False))
    elif cmd == "file-with-task":
        print(sum(1 for argv in calls(a[0]) if "--file" in argv
                  and ("--task" in argv or "--задача" in argv)))
    elif cmd == "has-arg-pair":   # log flag value → 1, если в каком-то зове есть «flag value»
        print(int(any(a[2] in after(argv, (a[1],)) for argv in calls(a[0]))))
    elif cmd == "cards":
        print(len(tasks(a[0], a[1])))
    elif cmd == "summary":
        found = tasks(a[0], a[1])
        print(found[0].get("summary", "") if found else "")
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
        print(f"записей {found}, не card: {' '.join(sorted(not_card)) or '-'}")
        print(f"долг {debt}, потолок {data.get('debt_max')}")
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

for _ in $(seq 1 50); do
    [ "$(judge claude-ancestors $$)" = 0 ] && break
    sleep 0.2
done
if [ "$(judge claude-ancestors $$)" != 0 ]; then
    echo "ОТКАЗ: проба идёт под процессом claude — писатель ответит «зов из сессии»."
    echo "Гнать фоном: setsid nohup bash scripts/test_watchers_wave3.sh > /var/log/harness/<имя>.log 2>&1 < /dev/null &"
    exit 2
fi

# ── стенд ──────────────────────────────────────────────────────────────────
build_stand() {  # $1 = каталог, $2 = файл сторожа, $3 = имя, $4 = режим писателя real|absent|refuse
    local s="$1" src="$2" name="$3" mode="$4" tool
    mkdir -p "$s/tree/harness/demons" "$s/tree/scripts/lib" "$s/tree/scripts/hooks" "$s/tree/docs" \
             "$s/log/locks/services" "$s/hb" "$s/home" "$s/bin" "$s/panel" "$s/state" \
             "$s/secrets" "$s/steps" "$s/tmux"
    printf '%s\n' "$name" > "$s/name"
    printf '%s\n' "$mode" > "$s/mode"
    cp "$src" "$s/tree/harness/demons/$name.sh"
    cp "$HERE/lib/config.sh" "$HERE/lib/config.py" "$s/tree/scripts/lib/"

    cat > "$s/tree/scripts/tg_send.sh" <<'DOOR'
#!/usr/bin/env bash
# Подставная дверь: пишет свой ряд доводов; на --task K, как настоящая, зовёт
# писателя ДО отправки. Код — доставки: 0, текст — STAND_DOOR_TEXT_CODE.
python3 -c 'import json,sys; open(sys.argv[1],"a",encoding="utf-8").write(json.dumps(sys.argv[2:],ensure_ascii=False)+"\n")' \
    "$STAND_DOOR_LOG" "$@"
key=""; rest=()
while [ $# -gt 0 ]; do
    case "$1" in
        --task|--задача) key="${2:-}"; shift 2; continue ;;
        --*) [ "$1" = --file ] || [ "$1" = --caption ] && rest+=("$1") ;;
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

    # Подставные инструменты машины.
    printf '#!/usr/bin/env bash\nprintf "vyzvan\\n" >> "%s/curl.log"\nprintf %s\n' "$s" \
        "'{\"ok\":true,\"result\":{\"message_id\":1}}'" > "$s/bin/curl"
    printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >> "%s/claude.log"\nexit 1\n' "$s" > "$s/bin/claude"
    for tool in docker npm pip uv sudo; do printf '#!/usr/bin/env bash\nexit 1\n' > "$s/bin/$tool"; done
    printf '#!/usr/bin/env bash\nexit 1\n' > "$s/bin/pgrep"
    cat > "$s/bin/df" <<'DF'
#!/usr/bin/env bash
case "$*" in
    *pcent*) printf 'Use%%\n 40%%\n' ;;
    *-h*)    printf 'Filesystem Size Used Avail Use%% Mounted\n/dev/stand 100G 40G 60G 40%% /\n' ;;
    *)       exec /usr/bin/df "$@" ;;
esac
DF
    printf '#!/usr/bin/env bash\nprintf "x total used free shared buff available\\nMem: 8G 2G 4G 0 2G 6G\\n"\n' > "$s/bin/free"
    cat > "$s/bin/systemctl" <<'SC'
#!/usr/bin/env bash
case "$1" in
    is-active) echo active ;;
    show) echo "" ;;
    *) exit 1 ;;
esac
SC
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
PROJECT_NAME="proba-w3"
PROJECT_DIR="$s/tree"
AGENT_USER="$(id -un)"
LOG_DIR="$s/log"
HEARTBEAT_DIR="$s/hb"
STATE_DIR="$s/state"
SECRETS_DIR="$s/secrets"
TMUX_SESSION="proba-w3"
AGENT_START_CMD="true"
TG_CHAT_ID="100500"
TG_TOKEN_FILE="$s/token"
TG_CHANNEL_VARIANT="A"
AUTONOMY="semi"
CONF
    # Сторож сессий читает сотню ключей под set -u: ему — образец пакета,
    # остальным — короткий конфиг. Места и пороги стенда стоят ПОСЛЕ образца:
    # harness.conf читается после паспорта и перебил бы TMUX_SESSION.
    : > "$s/harness.conf"
    [ "$name" = session-warden ] && cp "$SEED_CONF" "$s/harness.conf"
    cat >> "$s/harness.conf" <<CONF
ACTIVE_LINE="harness"
ALERT_TASKS_PER_WATCHER="3"
ALERT_MASS_MIN="3"
ALERT_LOCK_WAIT_SEC="2"
DEVMAP_SUMMARY_MAX="500"
SERVICE_LOCKS_SUBDIR="services"
HEARTBEAT_PERIODS=""
TMUX_SESSION="proba-w3"
AGENT_START_CMD="true"
CONF
    "stubs_$name" "$s"
}

stand_env() {  # $1 = стенд
    local s="$1"
    printf '%s\n' "PATH=$s/bin:/usr/local/bin:/usr/bin:/bin" "HOME=$s/home" "LC_ALL=C.UTF-8" \
        "PYTHONDONTWRITEBYTECODE=1" "HARNESS_HOME=$s/home" "TMUX_TMPDIR=$s/tmux" \
        "HARNESS_INSTALL_CONF=$s/install.conf" "HARNESS_CONF=$s/harness.conf" \
        "HARNESS_DEVMAP=$s/tree/dev-map.yaml" "DEVMAP_ARCHIVE=$s/tree/docs/dev-map-archive.yaml" \
        "HARNESS_ACTIVE_LINE=harness" "HARNESS_DOSKA=$s/log/доска.json" \
        "HARNESS_PANEL_STATE=$s/panel" "CLAUDE_PROJECTS_DIR=$s/projects" \
        "HARNESS_TG_SEND=$s/tree/scripts/tg_send.sh" "HARNESS_ALERT_TASK=$s/tree/scripts/alert-task.py" \
        "AGENT_PROC_ROOT=$s/hb" "STAND_REAL_WRITER=$WRITER" "STAND_WRITER_MODE=$(cat "$s/mode")" "STAND_DIR=$s"
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
        timeout 240 bash "$s/tree/harness/demons/$(cat "$s/name").sh" "$@" ) \
        > "$s/steps/$step.out" 2>&1 < /dev/null || rc=$?
    printf '%s\n' "$rc" > "$s/steps/$step.rc"
}

writer_call() {  # $1 = стенд, дальше доводы НАСТОЯЩЕГО писателя; печатает код
    local s="$1" rc=0 line envv=()
    shift
    while IFS= read -r line; do envv+=("$line"); done < <(stand_env "$s")
    env -i "${envv[@]}" python3 "$WRITER" "$@" >/dev/null 2>&1 < /dev/null || rc=$?
    printf '%s' "$rc"
}
is_green() { writer_call "$1" --is-green "$2"; }
drain() { writer_call "$1" --drain >/dev/null; }

# ── заглушки окружения сторожей ────────────────────────────────────────────
stubs_acceptance-watch() {
    local p="$1/tree"
    cat > "$p/scripts/acceptance.py" <<'PY'
import os, sys
mode = os.environ.get("STAND_ACC", "ok")
if mode == "crash":
    print("Traceback (стенд): битая карта, судья упал"); sys.exit(3)
print('{"итог": {"доказано": 1, "провал": 0, "доказать-нечем": 0, "проверка-врёт": 0, "ждёт-владельца": 0}}')
print("  доказано  hr.sample-one")
sys.exit(0)
PY
    cat > "$p/scripts/archive-map.py" <<'PY'
import os
print(f"перенесено закрытых задач: {os.environ.get('STAND_ARCH', '0')}")
PY
    cat > "$p/scripts/pre-commit-gate.sh" <<'SH'
#!/usr/bin/env bash
[ "${STAND_GATE_RC:-0}" = 0 ] && { echo "✓ карта здорова"; exit 0; }
echo "✗ hr.sample-one: дубль ключа owner_testable"; exit 1
SH
}

stubs_check-builder() {
    local p="$1/tree"
    printf 'model_dlya() { echo sonnet; }\n' > "$p/scripts/lib/models.sh"
    cat > "$p/scripts/acceptance.py" <<'PY'
import os, sys
if "--формы" in sys.argv:
    print("python3 scripts/<имя>.py --selftest"); sys.exit(0)
if "--долг" in sys.argv:
    if os.environ.get("STAND_DOLG", "1") == "1":
        print("hr.sample-one :: образец карточки :: нет поля проверка")
    sys.exit(0)
sys.exit(0)
PY
    cat > "$p/scripts/claude-demon.sh" <<'SH'
#!/usr/bin/env bash
# Агент строителя стенда: удаляет образец из карты, если велено.
if [ "${STAND_AGENT:-ok}" = delete ]; then
    python3 - "$STAND_DIR/tree/dev-map.yaml" <<'PY'
import re, sys
p = sys.argv[1]
t = open(p, encoding="utf-8").read()
t = re.sub(r"\n      - id: hr\.sample-one\n(?:        .*\n)+", "\n", t)
open(p, "w", encoding="utf-8").write(t)
PY
fi
echo "проверок построено: 1"
exit "${STAND_AGENT_RC:-0}"
SH
    cat > "$p/scripts/pre-commit-gate.sh" <<'SH'
#!/usr/bin/env bash
[ "${STAND_GATE_RC:-0}" = 0 ] && { echo "✓ карта здорова"; exit 0; }
echo "✗ hr.sample-one: дубль ключа owner_testable"; exit 1
SH
    cat > "$p/scripts/board.py" <<'PY'
import json, os, sys
with open(os.path.join(os.environ["STAND_DIR"], "board.log"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\n")
PY
    printf 'STROITEL_CLAUDE_TIMEOUT_SEC="30"\n' >> "$1/harness.conf"
}

stubs_extensions-watch() {
    cat > "$1/tree/scripts/extensions.py" <<'PY'
import os, sys
mode = os.environ.get("STAND_EXT", "quiet")
if mode == "fail":
    print("поставлено 0, снято 0"); print("отказ: claude не найден в PATH"); sys.exit(1)
if mode == "changed":
    print("поставлено 1, снято 0"); sys.exit(0)
print("поставлено 0, снято 0"); sys.exit(0)
PY
}

stubs_memory-revision() {
    local p="$1/tree"
    cat > "$p/scripts/check-context-hygiene.py" <<'PY'
import json, os, sys
mode = os.environ.get("STAND_MR", "clean")
if "--доли" in sys.argv:
    print(json.dumps({"ORDERS.md": {"доля": 0.95 if mode == "findings" else 0.5}})); sys.exit(0)
if "--переполнены" in sys.argv and mode == "findings":
    print("ORDERS.md: 95 % потолка → ПРЕДЛОЖЕНИЕ: прополоть пересказы в записи памяти")
sys.exit(0)
PY
    printf 'import sys\nsys.exit(0)\n' > "$p/scripts/check-proposals.py"
    printf 'HYGIENE_BACKLOG_FILES=""\nMEMORY_REVISION_TRIGGER="0.9"\n' >> "$1/harness.conf"
}

stubs_product-health() {
    # Ручка здоровья — заглушка curl: продукта на стенде нет, порт не нужен.
    cat > "$1/bin/curl" <<'SH'
#!/usr/bin/env bash
case "${STAND_HEALTH:-200}" in
    200) printf '{"ok":true,"checks":[]}\n200' ;;
    503) printf '{"ok":false,"checks":[{"name":"наличие витрины","ok":false,"detail":"последний обход 1253 с назад, потолок 900 с"}]}\n503' ;;
    *)   printf '\n000'; exit 7 ;;
esac
SH
    chmod +x "$1/bin/curl"
    printf 'PRODUCT_HEALTH_URL="http://stand.invalid/health"\nPRODUCT_QUIET_STATE=""\n' >> "$1/harness.conf"
}

stubs_sentinel() {
    local p="$1/tree"
    cat > "$1/bin/tmux" <<'SH'
#!/usr/bin/env bash
case "$1" in display-message) echo "${STAND_PANE:-claude}" ;; *) exit 0 ;; esac
SH
    chmod +x "$1/bin/tmux"
    cat > "$p/harness/demons/session-warden.sh" <<'SH'
#!/usr/bin/env bash
case "${1:-}" in --selftest) echo "SELFTEST: зелёный (стенд)" ;; esac
exit 0
SH
    chmod +x "$p/harness/demons/session-warden.sh"
    printf 'print("SELFTEST: зелёный (стенд)")\n' > "$p/scripts/hooks/test_guard_bash.py"
    printf '#!/usr/bin/env bash\necho "секретов нет (стенд)"\n' > "$p/scripts/check-secrets.sh"
    cat > "$p/scripts/check-login-expiry.py" <<'PY'
import os, sys
if os.environ.get("STAND_LOGIN") == "red":
    print("вход истекает: осталось 0 ч (стенд)"); sys.exit(1)
print("вход жив: 300 ч (стенд)")
PY
    # Подъём систем зелёный: сценарий краснит доклад сроком входа, а не
    # boot_check. Обёртка следа настоящая; вывод — дословная выдержка из следа
    # живого boot_check без метки (03.11.2026).
    cp "$HERE/live-check.sh" "$p/scripts/"
    cat > "$p/scripts/boot_check.py" <<'PY'
print("агент: ок — active")
print("ядро: не проверено — метки нет, ядру не с чем сравниться")
print("поднялся: ок 15 из 17; не проверено 2: продукт, ядро")
PY
    printf 'SENTINEL_UNITS="stand-unit"\nBOOT_WAIT_SEC="0"\n' >> "$1/harness.conf"
}

stubs_session-warden() {
    local s="$1" p="$1/tree" f
    cp /bin/sleep "$s/claude"
    for f in work-queue.py limit_mark.py limit-in-log.py stall-in-log.py; do
        [ -f "$HERE/$f" ] && cp "$HERE/$f" "$p/scripts/"
    done
    cp "$HERE"/lib/*.py "$HERE"/lib/*.sh "$p/scripts/lib/"
    mkdir -p "$s/log/inbox/обработано" "$s/projects/${p//\//-}"
    cat > "$p/scripts/start-agent.sh" <<SH
#!/usr/bin/env bash
# Пусковой скрипт стенда: ok — сессия с подставным агентом, иначе отказ.
[ "\${STAND_START:-fail}" = ok ] || exit 1
tmux new-session -d -s proba-w3 "$s/claude 900"
SH
    chmod +x "$p/scripts/start-agent.sh"
    cat >> "$s/harness.conf" <<CONF
SILENCE_MAX_MIN="30"
PULSE_MAX_MIN="600"
PULSE_OWNER_MIN="600"
LIMIT_STALL_MIN="600"
AGENT_START_TIMEOUT_SEC="10"
AGENT_WAKE_DELAY="0"
PANE_ENTER_DELAY="0.2"
AGENT_DEAD_MIN="15"
CTX_WINDOW_TOKENS="400000"  # не-секрет: размер окна модели в токенах, а не ключ
TG_CHAT_ID=""
TG_TOKEN_FILE="/nonexistent"
CONF
}

# Транскрипт сессии стенда: последний ответ $2 минут назад.
transcript() {  # $1 = стенд, $2 = минут назад
    local s="$1" when f
    f="$s/projects/${s//\//-}-tree/proba.jsonl"
    when=$(( $(date +%s) - $2 * 60 ))
    printf '{"type":"assistant","timestamp":"%s"}\n' "$(date -u -d "@$when" +%Y-%m-%dT%H:%M:%SZ)" > "$f"
    touch -d "@$when" "$f"
}
stand_tmux() { TMUX_TMPDIR="$1/tmux" env -i PATH=/usr/bin:/bin HOME="$1/home" TMUX_TMPDIR="$1/tmux" tmux "${@:2}"; }

# ── сценарии: одинаковые шаги для эталона и для проверяемого ───────────────
scenario_acceptance-watch() {
    local s="$1" st
    for st in "a1-crash STAND_ACC=crash" "a2-crash-again STAND_ACC=crash" \
              "a3-map-broken STAND_ACC=ok STAND_ARCH=1 STAND_GATE_RC=1" \
              "a4-green STAND_ACC=ok STAND_ARCH=0 STAND_GATE_RC=0"; do
        # shellcheck disable=SC2086
        run_step "$s" $st --
        drain "$s"
    done
}

scenario_check-builder() {
    local s="$1" st
    for st in "c1-map-bad STAND_GATE_RC=1" "c2-map-bad-again STAND_GATE_RC=1" \
              "c3-tasks-deleted STAND_AGENT=delete" "c4-green STAND_GATE_RC=0" \
              "c5-nothing-to-build STAND_DOLG=0"; do
        # shellcheck disable=SC2086
        run_step "$s" $st --
        drain "$s"
    done
}

scenario_extensions-watch() {
    local s="$1"
    run_step "$s" x1-fail STAND_EXT=fail --
    run_step "$s" x2-fail-again STAND_EXT=fail --
    run_step "$s" x3-changed STAND_EXT=changed --
    run_step "$s" x4-quiet STAND_EXT=quiet --
}

scenario_memory-revision() {
    local s="$1"
    run_step "$s" r1-findings STAND_MR=findings --
    run_step "$s" r2-findings-again STAND_MR=findings --
    run_step "$s" r3-clean STAND_MR=clean --
    run_step "$s" r4-threshold-findings STAND_MR=findings -- --по-порогу
    run_step "$s" r5-findings-attachment STAND_MR=findings STAND_DOOR_TEXT_CODE=1 --
}

scenario_product-health() {
    local s="$1"
    run_step "$s" p1-bad STAND_HEALTH=503 --
    run_step "$s" p2-bad-again STAND_HEALTH=503 --
    run_step "$s" p3-ok STAND_HEALTH=200 --
    run_step "$s" p4-dead STAND_HEALTH=000 --
    run_step "$s" p5-ok STAND_HEALTH=200 --
}

scenario_sentinel() {
    local s="$1" st
    for st in "b1-boot-red STAND_LOGIN=red -- --boot" "b2-boot-red-again STAND_LOGIN=red -- --boot" \
              "b3-boot-green -- --boot" "t1-timer --"; do
        touch "$s/hb/session-warden.запуск" "$s/hb/session-warden"
        # shellcheck disable=SC2086
        run_step "$s" $st
    done
}

scenario_session-warden() {
    local s="$1"
    # w1: сессии нет, очередь непуста, агент молчит 45 мин; пусковой отказал.
    transcript "$s" 45
    run_step "$s" w1-no-session-start-fails STAND_START=fail --
    # w2: то же, пусковой поднял сессию с подставным агентом.
    run_step "$s" w2-no-session-revived STAND_START=ok --
    # w3: агент в панели, транскрипт свежий — агент на месте.
    transcript "$s" 1
    run_step "$s" w3-agent-present --
    is_green "$s" session-warden:no-agent > "$s/steps/w3.green"
    # w4: в панели оболочка дольше AGENT_DEAD_MIN, команда запуска не даёт claude.
    # kill-server возвращается раньше, чем сервер ушёл: new-session к
    # умирающему серверу отвечает «server exited unexpectedly», и сторож видит
    # «сессии нет» — шаг судил бы другую ветку. Ждём ухода сокета, поднимаем
    # с пределом и пишем, была ли оболочка в панели перед шагом.
    stand_tmux "$s" kill-server 2>/dev/null
    for _ in $(seq 1 50); do [ -S "$s/tmux/tmux-$(id -u)/default" ] || break; sleep 0.1; done
    for _ in $(seq 1 20); do
        stand_tmux "$s" new-session -d -s proba-w3 bash 2>/dev/null && break
        sleep 0.2
    done
    stand_tmux "$s" has-session -t proba-w3 2>/dev/null && echo да > "$s/steps/w4.shell" \
        || echo нет > "$s/steps/w4.shell"
    date -d '60 minutes ago' +%s > "$s/log/agent-pane.since"
    rm -f "$s/log/agent-pane.told"
    run_step "$s" w4-shell-start-fails STAND_START=fail --
    stand_tmux "$s" kill-server 2>/dev/null
}

run_watcher() {  # $1 = имя
    local name="$1" kind src mode
    for kind in base cur absent refuse; do
        src="${SRC[$name]}"; [ "$kind" = base ] && src="$BEFORE/$name.sh"
        mode=real
        case "$kind" in absent) mode=absent ;; refuse) mode=refuse ;; esac
        build_stand "$T/$name-$kind" "$src" "$name" "$mode"
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
    case_check "$1 $2: беда зовёт с ключом «$3»" "$3" "$(raised "$1" "$2")"
}
# Шаг, где ключ несут НЕСКОЛЬКО вестей: объединение ключей шага прячет весть,
# потерявшую --task. Судится ключ той вести, что несёт фразу.
expect_raised_by() {  # $1 = имя, $2 = шаг, $3 = фраза вести, $4 = ключ
    case_check "$1 $2: весть «$3» зовёт с ключом «$4»" "$4" \
        "$(judge key-of "$T/$1-cur/steps/$2.door" "$3")"
}
expect_resolved() {
    case_check "$1 $2: зелёное гасит «$3»" "" "$(judge minus "$3" "$(resolved "$1" "$2")")"
}
expect_card() {  # $1 = имя, $2 = ключ
    local s="$T/$1-cur"
    case_check "$1: по ключу $2 одна карточка в стенд-карте" "1" \
        "$(judge cards "$s/tree/dev-map.yaml" "$2" 2>/dev/null || echo ошибка)"
    case_check "$1: summary карточки $2 несёт перезапуск сторожа" "да" \
        "$(judge summary "$s/tree/dev-map.yaml" "$2" 2>/dev/null \
           | grep -qF "Перепроверить сторожем: bash harness/demons/$1.sh" && echo да || echo нет)"
}
expect_green() {
    case_check "$1: после зелёного --is-green $2 — 0" "0" "$(is_green "$T/$1-cur" "$2")"
}
no_self_resolve() {
    local name="$1" step
    for step in $(steps_of "$name"); do
        case_check "$name $step: поднятое не гасится тем же прогоном" "" \
            "$(judge both "$(raised "$name" "$step")" "$(resolved "$name" "$step")")"
    done
}

[ -d "$BEFORE" ] || echo "ВНИМАНИЕ: каталога эталонов нет: $BEFORE"

echo "── контроль стенда ──"
build_stand "$T/control" "${SRC[extensions-watch]}" extensions-watch real
CTRL_ENV=()
while IFS= read -r line; do CTRL_ENV+=("$line"); done < <(stand_env "$T/control")
env -i "${CTRL_ENV[@]}" STAND_DOOR_LOG="$T/control/door.log" STAND_ALERT_LOG="$T/control/alert.log" \
    bash "$T/control/tree/scripts/tg_send.sh" "Контроль стенда: беда" --task extensions-watch:control >/dev/null 2>&1
case_check "контроль: дверь стенда с --task завела карточку настоящим писателем" "1" \
    "$(judge cards "$T/control/tree/dev-map.yaml" extensions-watch:control 2>/dev/null || echo ошибка)"
case_check "контроль: summary несёт перезапуск сторожа" "да" \
    "$(judge summary "$T/control/tree/dev-map.yaml" extensions-watch:control 2>/dev/null \
       | grep -qF 'Перепроверить сторожем: bash harness/demons/extensions-watch.sh' && echo да || echo нет)"
env -i "${CTRL_ENV[@]}" STAND_ALERT_LOG="$T/control/alert.log" \
    python3 "$T/control/tree/scripts/alert-task.py" --resolve extensions-watch:control >/dev/null 2>&1
case_check "контроль: --resolve через обёртку → --is-green 0" "0" "$(is_green "$T/control" extensions-watch:control)"
case_check "контроль: без --resolve ключ не зелёный" "1" "$(is_green "$T/control" extensions-watch:never)"
# Лок карты, взятый как у acceptance-watch/check-builder: писатель откладывает,
# донос заводит. Без этого случаи двух сторожей с локом судили бы стенд.
(
    exec 9>"$T/control/log/locks/services/dev-map.lock"; flock -n 9
    env -i "${CTRL_ENV[@]}" STAND_DOOR_LOG="$T/control/door.log" STAND_ALERT_LOG="$T/control/alert.log" \
        bash "$T/control/tree/scripts/tg_send.sh" "Контроль: под локом" --task extensions-watch:locked >/dev/null 2>&1
)
case_check "контроль: под локом карты писатель карточку не завёл" "0" \
    "$(judge cards "$T/control/tree/dev-map.yaml" extensions-watch:locked 2>/dev/null || echo ошибка)"
drain "$T/control"
case_check "контроль: донос после лока завёл карточку" "1" \
    "$(judge cards "$T/control/tree/dev-map.yaml" extensions-watch:locked 2>/dev/null || echo ошибка)"

# Шаги беды, где эталон зовёт дверь, и коды эталона: стенд, не создавший беду
# или уронивший сторожа своей поломкой, судил бы пустоту.
declare -A RED_STEP=([acceptance-watch]=a1-crash [check-builder]=c1-map-bad
    [extensions-watch]=x1-fail [memory-revision]=r1-findings [product-health]=p1-bad
    [sentinel]=b1-boot-red [session-warden]=w1-no-session-start-fails)
declare -A BASE_RC=([acceptance-watch]="3 3 1 0" [check-builder]="1 1 1 0 0"
    [extensions-watch]="1 1 0 0" [memory-revision]="0 0 0 0 0" [product-health]="0 0 0 0 0"
    [sentinel]="0 0 0 0" [session-warden]="1 1 0 1")
# sentinel: красная проверка считается в подоболочке, и её RC до выхода не
# доходит — эталон выходит с 0 и на красном докладе.
base_rcs() { local st; for st in $(steps_of "$1"); do cat "$T/$1-base/steps/$st.rc"; done | paste -sd' ' -; }
for name in $WATCHERS; do
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

if want acceptance-watch; then
echo "── acceptance-watch: ключи ──"
expect_raised acceptance-watch a1-crash "acceptance-watch:crash"
expect_raised acceptance-watch a2-crash-again "acceptance-watch:crash"
expect_raised acceptance-watch a3-map-broken "acceptance-watch:crash"
expect_raised acceptance-watch a4-green ""
expect_resolved acceptance-watch a4-green "acceptance-watch:crash"
expect_card acceptance-watch acceptance-watch:crash
expect_green acceptance-watch acceptance-watch:crash
fi

if want check-builder; then
echo "── check-builder: ключи ──"
expect_raised check-builder c1-map-bad "check-builder:map-damaged"
expect_raised check-builder c2-map-bad-again "check-builder:map-damaged"
expect_raised check-builder c3-tasks-deleted "check-builder:map-damaged"
expect_raised check-builder c4-green ""
expect_resolved check-builder c4-green "check-builder:map-damaged"
expect_card check-builder check-builder:map-damaged
expect_green check-builder check-builder:map-damaged
# Спека Ф3: при порче карты — ещё и доска на 180 мин (писатель откладывает
# карточку, пока лок/карта не пускают, а смене причина нужна сразу).
case_check "check-builder c1: порча карты кладёт факт на доску «--на 180»" "1" \
    "$(judge has-arg-pair "$T/check-builder-cur/board.log" --на 180)"
fi

if want extensions-watch; then
echo "── extensions-watch: ключи ──"
expect_raised extensions-watch x1-fail "extensions-watch:failed"
expect_raised extensions-watch x2-fail-again "extensions-watch:failed"
expect_raised extensions-watch x3-changed ""
expect_resolved extensions-watch x3-changed "extensions-watch:failed"
expect_resolved extensions-watch x4-quiet "extensions-watch:failed"
expect_card extensions-watch extensions-watch:failed
expect_green extensions-watch extensions-watch:failed
fi

if want memory-revision; then
echo "── memory-revision: ключи ──"
expect_raised memory-revision r1-findings "memory-revision:findings"
expect_raised memory-revision r2-findings-again "memory-revision:findings"
expect_raised memory-revision r3-clean ""
expect_resolved memory-revision r3-clean "memory-revision:findings"
expect_card memory-revision memory-revision:findings
# Проверка гашения — сразу по журналу шага r3: r4/r5 поднимают ключ снова.
case_check "memory-revision r4: тихий прогон по порогу тоже заводит задачу (владельцу не шлёт)" \
    "memory-revision:findings" "$(raised memory-revision r4-threshold-findings)"
case_check "memory-revision r4: владельцу тихий прогон по-прежнему не пишет" "0" \
    "$(grep -c . "$T/memory-revision-cur/steps/r4-threshold-findings.door")"
expect_raised memory-revision r5-findings-attachment "memory-revision:findings"
case_check "memory-revision r5: вложение уходит без --task" "0" \
    "$(judge file-with-task "$T/memory-revision-cur/steps/r5-findings-attachment.door")"
case_check "memory-revision r5: вложение ушло (дверь отказала тексту)" "да" \
    "$(grep -q -- '--file' "$T/memory-revision-cur/steps/r5-findings-attachment.door" && echo да || echo нет)"
fi

if want product-health; then
echo "── product-health: ключи ──"
# Карточку и её summary проба не судит: линия продукта (Р-15) на стенде не
# определена — см. p-w3-notes.md.
expect_raised product-health p1-bad "product-health:trouble"
expect_raised product-health p3-ok ""
expect_resolved product-health p3-ok "product-health:trouble"
expect_raised product-health p4-dead "product-health:trouble"
expect_resolved product-health p5-ok "product-health:trouble"
expect_green product-health product-health:trouble
fi

if want sentinel; then
echo "── sentinel: ключи ──"
expect_raised sentinel b1-boot-red "sentinel:start-red"
expect_raised sentinel b2-boot-red-again "sentinel:start-red"
expect_raised sentinel b3-boot-green ""
expect_resolved sentinel b3-boot-green "sentinel:start-red"
expect_raised sentinel t1-timer ""
expect_card sentinel sentinel:start-red
expect_green sentinel sentinel:start-red
fi

if want session-warden; then
echo "── session-warden: ключи ──"
expect_raised session-warden w1-no-session-start-fails "session-warden:no-agent"
expect_raised_by session-warden w1-no-session-start-fails "поднять её командой" "session-warden:no-agent"
expect_raised session-warden w2-no-session-revived "session-warden:no-agent"
expect_raised_by session-warden w2-no-session-revived "поднял заново" "session-warden:no-agent"
expect_raised session-warden w3-agent-present ""
expect_resolved session-warden w3-agent-present "session-warden:no-agent"
case_check "session-warden: сразу после w3 --is-green no-agent — 0" "0" \
    "$(cat "$T/session-warden-cur/steps/w3.green" 2>/dev/null || echo нет-замера)"
expect_raised session-warden w4-shell-start-fails "session-warden:no-agent"
expect_raised_by session-warden w4-shell-start-fails "выпал в оболочку" "session-warden:no-agent"
for kind in base cur absent refuse; do
    case_check "session-warden $kind: перед w4 в панели оболочка" "да" \
        "$(cat "$T/session-warden-$kind/steps/w4.shell" 2>/dev/null || echo нет-замера)"
done
expect_card session-warden session-warden:no-agent
fi

if want senders; then
echo "── owner-senders: волна 3 — card ──"
SENDERS_OUT="$(judge senders "$SENDERS_DATA" "$ROOT/scripts/check-owner-senders.py" "$ROOT" \
    "$DEMONS/acceptance-watch.sh" "$DEMONS/check-builder.sh" "$DEMONS/extensions-watch.sh" \
    "$DEMONS/memory-revision.sh" "$DEMONS/product-health.sh" "$DEMONS/sentinel.sh" \
    "$DEMONS/session-warden.sh" 2>&1)"
case_check "owner-senders: ключи семи сторожей — девять записей, все card" "записей 9, не card: -" \
    "$(sed -n 1p <<< "$SENDERS_OUT")"
case_check "owner-senders: долга не осталось, debt_max 0" "долг 0, потолок 0" \
    "$(sed -n 2p <<< "$SENDERS_OUT")"
fi

echo "── боевое ──"
case_check "боевая карта не тронута (sha1)" "$LIVE_SHA" \
    "$(sha1sum "$LIVE_MAP" 2>/dev/null | cut -d' ' -f1)"
case_check "прямой curl мимо двери не звался ни на одном стенде" "0" \
    "$(cat "$T"/*/curl.log 2>/dev/null | grep -c . || true)"
case_check "настоящий claude ни на одном стенде не звался" "0" \
    "$(cat "$T"/*/claude.log 2>/dev/null | grep -c . || true)"

printf '\nВОЛНА-3 СТОРОЖЕЙ: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
