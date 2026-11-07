#!/usr/bin/env bash
#
# test_memory_revision_threshold.sh — проба: ежедневный круг ревизии памяти
# (`memory-revision.sh --по-порогу`) гасит открытую тревогу
# memory-revision:findings, когда находок нет, и не гасит, когда они есть.
#
# Почему: красная тревога гасится только из прогона, дошедшего до осмотра.
# Круг по порогу при заполнении ниже порога выходил до осмотра, и тревога,
# причина которой уже закрыта, висела до месячного прогона
# (.tasks/hr.stale-alerts-03-11/отчёт.md, строка memory-revision:findings).
#
# Стенд: копия сторожа в своём дереве (оно же PROJECT_DIR), свой паспорт и
# конфиг, `env -i`. Гейт гигиены — заглушка: доля из STAND_SHARE, переполненных
# нет. Писатель тревог — заглушка: пишет доводы каждого зова в журнал стенда;
# на `--is-green` отвечает 1 — ключ открыт. Дверь канала — заглушка с кодом 0.
# Источник находки при доле ниже порога — правило «должен» без команды
# проверки в CLAUDE.md стенда (раздел 2 сторожа).
#
#   bash scripts/test_memory_revision_threshold.sh
# Дверь для мутаций: HARNESS_MEMORY_REVISION — файл сторожа.
# Код возврата: 0 — зелёная, 1 — красная.
set -uo pipefail
export LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
source "$HERE/lib/selftest_cases.sh"
WATCHER="${HARNESS_MEMORY_REVISION:-$ROOT/harness/demons/memory-revision.sh}"
KEY="memory-revision:findings"

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

build_stand() {  # $1 = каталог стенда
    local s="$1"
    mkdir -p "$s/tree/harness/demons" "$s/tree/scripts/lib" "$s/log" "$s/hb" "$s/home"
    cp "$WATCHER" "$s/tree/harness/demons/memory-revision.sh"
    cp "$HERE/lib/config.sh" "$HERE/lib/config.py" "$s/tree/scripts/lib/"
    cat > "$s/tree/scripts/check-context-hygiene.py" <<'PY'
import json, os, sys
if "--доли" in sys.argv:
    print(json.dumps({"ORDERS.md": {"доля": float(os.environ["STAND_SHARE"])}}))
sys.exit(0)
PY
    printf 'import sys\nsys.exit(0)\n' > "$s/tree/scripts/check-proposals.py"
    cat > "$s/tree/scripts/alert-task.py" <<'PY'
import json, os, sys
with open(os.environ["STAND_ALERT_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\n")
sys.exit(1 if "--is-green" in sys.argv else 0)
PY
    printf '#!/usr/bin/env bash\nexit 0\n' > "$s/tree/scripts/tg_send.sh"
    chmod +x "$s/tree/scripts/tg_send.sh"
    cat > "$s/install.conf" <<CONF
PROJECT_NAME="proba-mr"
PROJECT_DIR="$s/tree"
AGENT_USER="$(id -un)"
LOG_DIR="$s/log"
HEARTBEAT_DIR="$s/hb"
TG_CHAT_ID="100500"
TG_TOKEN_FILE="$s/nonexistent"
CONF
    printf 'HYGIENE_BACKLOG_FILES=""\nMEMORY_REVISION_TRIGGER="0.9"\nSERVICE_LOCKS_SUBDIR="services"\n' \
        > "$s/harness.conf"
}

run_threshold() {  # $1 = стенд, $2 = доля; печатает код выхода
    local s="$1" rc=0
    : > "$s/alert.log"
    ( cd "$s/home" && env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$s/home" LC_ALL=C.UTF-8 \
        PYTHONDONTWRITEBYTECODE=1 HARNESS_HOME="$s/home" \
        HARNESS_INSTALL_CONF="$s/install.conf" HARNESS_CONF="$s/harness.conf" \
        STAND_SHARE="$2" STAND_ALERT_LOG="$s/alert.log" \
        timeout 120 bash "$s/tree/harness/demons/memory-revision.sh" --по-порогу ) \
        > "$s/out.log" 2>&1 < /dev/null || rc=$?
    printf '%s' "$rc"
}

# Сколько раз писатель позван с `--resolve <ключ>` — разбор журнала стенда.
resolves() {  # $1 = стенд
    python3 - "$1/alert.log" "$KEY" <<'PY'
import json, sys
n = 0
for line in open(sys.argv[1], encoding="utf-8"):
    argv = json.loads(line)
    n += any(a == "--resolve" and b == sys.argv[2] for a, b in zip(argv, argv[1:]))
print(n)
PY
}

echo "── memory-revision --по-порогу: гашение тревоги ──"

# Контроль стенда: при доле выше порога и без находок гашение уже есть —
# если здесь 0, стенд слеп к зову писателя, и красное ниже ничего не значит.
S="$T/control"; build_stand "$S"
case_check "контроль: доля 0.95, находок нет — круг выходит с 0" "0" "$(run_threshold "$S" 0.95)"
case_check "контроль: доля 0.95, находок нет — resolve $KEY позван" "1" "$(resolves "$S")"

# БОЛЬНОЙ СЛУЧАЙ: доля ниже порога, находок нет, ключ открыт.
S="$T/low-clean"; build_stand "$S"
case_check "доля 0.5, находок нет — круг выходит с 0" "0" "$(run_threshold "$S" 0.5)"
case_check "БОЛЬНОЙ СЛУЧАЙ: доля 0.5, находок нет, ключ открыт — resolve $KEY позван" "1" \
    "$(resolves "$S")"

# Находка есть (правило без команды проверки) — тревогу гасить нельзя.
S="$T/low-findings"; build_stand "$S"
printf '# Правила\n\nАгент должен помнить про прополку памяти.\n' > "$S/tree/CLAUDE.md"
case_check "доля 0.5, находка есть — круг выходит с 0" "0" "$(run_threshold "$S" 0.5)"
case_check "доля 0.5, находка есть — resolve $KEY не позван" "0" "$(resolves "$S")"

echo "ГАШЕНИЕ ПО ПОРОГУ: путей $CASES_PATHS, неудач $CASES_FAILED"
[ "$CASES_FAILED" = 0 ]
