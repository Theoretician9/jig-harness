#!/usr/bin/env bash
#
# task-closer.sh — закрывает брошенные задачи в карте разработки.
#
# Что держит: правило «карта не врёт» (CLAUDE.md §16): задача, висящая в wip
# без живого процесса агента дольше TASK_STALL_HOURS, — это не работа, это
# враньё дашборду. Демон переводит её в plan и пишет человеческую причину:
# владелец видит «брошено тогда-то, потому-то», а не вечные 40%.
#
# Откуда взят: собран для стартового пакета; принцип «пропуск чинится сам,
# а не докладом человеку» — из devmap-selfheal.sh.
#
# Как правится карта (решение Д-1 ревью 09.08.2026): yaml.safe_load — ТОЛЬКО
# для чтения и поиска протухших wip (многострочные |-блоки и инлайн-формы
# регулярками не разобрать). Запись — построчная хирургия исходного текста:
# найти блок задачи по id, заменить строку status:, вписать closed_reason:
# с тем же отступом, убрать id из строки queue. Перезапись дерева через
# yaml.safe_dump стирала комментарии карты (легенду статусов, калибровку ЕР) —
# комментарии в dev-map.yaml несут нормы, терять их нельзя. Запись атомарная
# (tmp + os.replace), после хирургии YAML перечитывается — сломали разметку,
# значит карта не трогается вовсе.
#
# Чем доказывается: встроенным больным случаем — `task-closer.sh --selftest`
# создаёт во временном каталоге карту с задачей, брошенной в 2020 году
# (плюс комментарии и многострочный summary), и проверяет: задача помечена
# с причиной и убрана из queue, комментарии на месте, YAML валиден.
# Прогоняется при установке (01-SPEC §9) и при любой правке этого файла.
#
# Модель статусов карты не знает «failed» (done/test/wip/plan) — поэтому
# брошенная задача возвращается в plan с полем closed_reason и причиной:
# видно и парсеру, и человеку, и дашборд не ломается о неизвестный статус.
#
# Запуск: cron, раз в час, от имени $AGENT_USER.
set -euo pipefail

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# ── самотест: больной случай во временном каталоге ──────────────────────────
if [ "${1:-}" = "--selftest" ]; then
    T=$(mktemp -d)
    trap 'rm -rf "$T"' EXIT
    mkdir -p "$T/hb" "$T/log" "$T/proj"
    cat > "$T/install.conf" <<EOF
PROJECT_NAME="selftest"
PROJECT_DIR="$T/proj"
AGENT_USER="no-such-user-selftest"
SECRETS_DIR="$T"
TG_TOKEN_FILE="$T/no-token"
TG_CHAT_ID=""
AUTONOMY="semi"
HEARTBEAT_DIR="$T/hb"
LOG_DIR="$T/log"
EOF
    cat > "$T/harness.conf" <<EOF
TASK_STALL_HOURS=48
EOF
    # Карта с двумя wip: одна брошена в 2020 (с многострочным summary —
    # |-блок ломал построчную правку регулярками), вторая начата сегодня.
    # Комментарии — нарочно (Д-1): safe_dump их стирал, хирургия обязана сохранить.
    cat > "$T/proj/dev-map.yaml" <<EOF
version: 1
# калибровка ЕР — комментарий обязан пережить закрытие задачи
queue: [t.stale, t.fresh]
lines:
  - id: core
    # комментарий внутри направления — тоже неприкосновенен
    tasks:
      - id: t.stale
        status: wip
        title: "Брошенная задача"
        summary: |-
          первая строка описания
          вторая строка описания
        when: { started: "2020-01-01", estimated: true }
      - id: t.fresh
        status: wip
        title: "Свежая задача"
        when: { started: "$(date +%Y-%m-%d)", estimated: true }
EOF
    # bash "$0", не "$0": после scp с Windows exec-бита может не быть (П-5).
    INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" bash "$0"
    grep -A2 'id: t.stale' "$T/proj/dev-map.yaml" | grep -q 'status: plan' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: брошенная задача не переведена в plan"; exit 1; }
    grep -q 'closed_reason:' "$T/proj/dev-map.yaml" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: нет человеческой причины closed_reason"; exit 1; }
    grep -A2 'id: t.fresh' "$T/proj/dev-map.yaml" | grep -q 'status: wip' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: свежая задача тронута, а не должна"; exit 1; }
    grep -q 'вторая строка описания' "$T/proj/dev-map.yaml" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: многострочный summary потерян при перезаписи"; exit 1; }
    # Д-1: комментарии пережили правку, YAML валиден, задача ушла из queue
    grep -q '# калибровка ЕР' "$T/proj/dev-map.yaml" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: комментарий верхнего уровня стёрт правкой (Д-1)"; exit 1; }
    grep -q '# комментарий внутри направления' "$T/proj/dev-map.yaml" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: комментарий внутри блока стёрт правкой (Д-1)"; exit 1; }
    python3 -c 'import sys, yaml; yaml.safe_load(open(sys.argv[1], encoding="utf-8"))' \
        "$T/proj/dev-map.yaml" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: после хирургии YAML не читается"; exit 1; }
    if grep '^queue:' "$T/proj/dev-map.yaml" | grep -q 't.stale'; then
        echo "САМОТЕСТ ПРОВАЛЕН: закрытая задача осталась в queue"; exit 1
    fi
    grep '^queue:' "$T/proj/dev-map.yaml" | grep -q 't.fresh' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: из queue пропала живая задача"; exit 1; }
    [ -f "$T/hb/task-closer" ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: метка heartbeat не поставлена"; exit 1; }
    echo "САМОТЕСТ ПРОЙДЕН: брошенная помечена с причиной и убрана из queue, свежая цела, комментарии на месте, YAML валиден"
    exit 0
fi

# ── конфиги ─────────────────────────────────────────────────────────────────
INSTALL_CONF="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-/etc/harness/install.conf}}"
HARNESS_CONF="${HARNESS_CONF:-/etc/harness/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "task-closer: нет $INSTALL_CONF — установка не завершена (01-SPEC §0)"; exit 1; }
[ -r "$HARNESS_CONF" ] || { echo "task-closer: нет $HARNESS_CONF — скопируйте harness/config/harness.conf в /etc/harness/"; exit 1; }
# Окружение старше конфига: общий загрузчик вместо голого source (улика
# 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/konf.sh"
konf_zagruzit
: "${PROJECT_DIR:?пуст PROJECT_DIR}" "${HEARTBEAT_DIR:?пуст HEARTBEAT_DIR}" "${TASK_STALL_HOURS:?пуст TASK_STALL_HOURS}"

DEVMAP="$PROJECT_DIR/dev-map.yaml"
mkdir -p "$HEARTBEAT_DIR"

# Лок ОБЩИЙ НА КАРТУ, а не на демона: карту пишут пять сторон — живой агент,
# devmap-selfheal, приёмка, строитель проверок и этот демон. Запись здесь
# атомарная (mkstemp + rename), но атомарность спасает от полуфайла, а не от
# гонки: пока мы читали карту, агент дописал карточку, и наша замена стирает
# её целиком. Замер 12.09.2026: общий лок брали двое из пяти (ревью спеки
# ежемесячной ревизии, I-10).
#
# Дескриптор закрывается у КАЖДОГО потомка (9>&-): иначе долгий потомок
# наследует его и держит лок после нашего выхода — [[лок-утёк-в-потомка]].
LOK="${LOG_DIR:-/var/log/harness}/locks/${SERVICE_LOCKS_SUBDIR:-services}/dev-map.lock"
mkdir -p "$(dirname "$LOK")"
exec 9>"$LOK"
if ! flock -n 9; then
    echo "$(date '+%Y-%m-%dT%H:%M:%S%:z') task-closer: карту сейчас правит кто-то другой (лок $LOK) — выхожу, повторю по расписанию"
    touch "$HEARTBEAT_DIR/task-closer"
    exit 0
fi

# Карты нет — продукт ещё не начат: молча выйти успехом.
if [ ! -f "$DEVMAP" ]; then
    touch "$HEARTBEAT_DIR/task-closer"
    exit 0
fi

# Живой процесс агента — задачи не трогаем: даты в карте дневной точности,
# и работающая сессия может честно держать wip сутками.
# Д-1-остаток: pgrep -f 'claude' ловил ЛЮБОЙ argv со словом claude
# (tail -f claude.log, редактор с открытым claude.md) — ложное «жив» вечно
# держало брошенный wip. Сужение двумя ветками: точное имя процесса
# (pgrep -x; у скрипта claude comm = claude) ИЛИ команда, НАЧИНАЮЩАЯСЯ
# словом claude — якорь (^|/)claude( |$) по полной командной строке
# (покрывает «claude --flags» и «node /usr/local/bin/claude --flags»).
if pgrep -u "$AGENT_USER" -x claude >/dev/null 2>&1 \
   || pgrep -u "$AGENT_USER" -f '(^|/)claude( |$)' >/dev/null 2>&1; then
    say "процесс агента жив — брошенных нет по определению, выхожу"
    touch "$HEARTBEAT_DIR/task-closer"
    exit 0
fi

# ── правка карты: yaml читает, хирургия строк пишет (Д-1) ───────────────────
# 9>&- : потомок не наследует дескриптор лока (см. выше).
CLOSED=$(python3 - "$DEVMAP" "$TASK_STALL_HOURS" 9>&- <<'PY'
import sys, os, re, datetime, tempfile

try:
    import yaml
except ImportError:
    print("нет python3-yaml — sudo apt-get install -y python3-yaml", file=sys.stderr)
    sys.exit(3)

path, stall_hours = sys.argv[1], int(sys.argv[2])
today = datetime.date.today()
now = datetime.datetime.now()

with open(path, encoding="utf-8") as fh:
    text = fh.read()
data = yaml.safe_load(text)

def started_of(task):
    """Дата старта задачи или None. YAML отдаёт date для голой даты и str для взятой в кавычки."""
    when = task.get("when")
    if not isinstance(when, dict):
        return None
    raw = when.get("started")
    if isinstance(raw, datetime.date):
        return datetime.datetime(raw.year, raw.month, raw.day)
    if isinstance(raw, str):
        try:
            return datetime.datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None
    return None

# ── чтение: рекурсивный поиск протухших wip (id, причина) ───────────────────
stale = []

def walk(node):
    if isinstance(node, dict):
        if node.get("status") == "wip":
            started = started_of(node)
            if started is not None:
                age_h = (now - started).total_seconds() / 3600
                if age_h > stall_hours:
                    reason = (f"закрыта task-closer {today}: висела в wip с {started.date()} "
                              f"без живого процесса агента (порог {stall_hours} ч)")
                    stale.append((str(node.get("id", "?")), reason))
        for v in node.values():
            walk(v)
    elif isinstance(node, list):
        for v in node:
            walk(v)

if data is not None:
    walk(data)
if not stale:
    sys.exit(0)

# ── запись: построчная хирургия исходного текста ────────────────────────────
lines = text.split("\n")

def task_block(tid):
    """(строка id, отступ ключей, конец блока) блочной записи задачи или None."""
    pat = re.compile(r'^(\s*)(- )?id:\s*' + re.escape(tid) + r'\s*(#.*)?$')
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if m is None:
            continue
        key_indent = len(m.group(1)) + (2 if m.group(2) else 0)
        end = len(lines)
        for j in range(i + 1, len(lines)):
            s = lines[j].strip()
            if not s or s.startswith("#"):
                continue
            if len(lines[j]) - len(lines[j].lstrip()) < key_indent:
                end = j
                break
        return i, key_indent, end
    return None

def close_in_text(tid, reason):
    """status: wip → plan + closed_reason рядом; True, если правка легла."""
    # инлайн-форма: - { id: x, status: wip, ... } — правка внутри одной строки
    for i, ln in enumerate(lines):
        if "{" in ln and re.search(r'\bid:\s*' + re.escape(tid) + r'\s*[,}]', ln):
            if "status: wip" not in ln:
                return False
            lines[i] = ln.replace("status: wip", f'status: plan, closed_reason: "{reason}"', 1)
            return True
    blk = task_block(tid)
    if blk is None:
        return False
    i, key_indent, end = blk
    for j in range(i, end):
        m = re.match(r'^(\s*)status:\s*wip\s*(#.*)?$', lines[j])
        if m and len(m.group(1)) == key_indent:
            lines[j] = f"{m.group(1)}status: plan"
            lines.insert(j + 1, f'{m.group(1)}closed_reason: "{reason}"')
            return True
    return False

def drop_from_queue(tid):
    """Убирает id из строки queue: [a, b, ...] — закрытому в очереди не место."""
    pat = re.compile(r'^(\s*)queue:\s*\[(.*)\]\s*(#.*)?$')
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if m is None:
            continue
        items = [x.strip() for x in m.group(2).split(",") if x.strip()]
        items = [x for x in items if x.strip("\"'") != tid]
        tail = f"  {m.group(3)}" if m.group(3) else ""
        lines[i] = f'{m.group(1)}queue: [{", ".join(items)}]{tail}'
        return

closed = []
for tid, reason in stale:
    if close_in_text(tid, reason):
        drop_from_queue(tid)
        closed.append((tid, reason))
    else:
        print(f"задача {tid} протухла, но строка status: wip в тексте не найдена — карту не трогаю",
              file=sys.stderr)

if closed:
    new_text = "\n".join(lines)
    # хирургия обязана оставить YAML читаемым — иначе карта не трогается вовсе
    try:
        yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        print(f"после правки YAML сломался ({e}) — карта не тронута", file=sys.stderr)
        sys.exit(4)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".dev-map-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    os.replace(tmp, path)

for tid, reason in closed:
    print(f"{tid}\t{reason}")
PY
)

if [ -n "$CLOSED" ]; then
    while IFS=$'\t' read -r tid reason; do
        say "закрыта задача $tid: $reason"
    done <<< "$CLOSED"
else
    say "брошенных задач нет"
fi

touch "$HEARTBEAT_DIR/task-closer"
