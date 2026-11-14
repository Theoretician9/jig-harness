#!/usr/bin/env bash
# Проба: код 79 обёртки демона — «личность не собрана», а не провал и не тревога.
#
# Демоны зовут модель через claude-demon.sh → claude-run.sh (§4.4).
# Носитель отдаёт 79, когда личность класса не собрана: модель не
# запускалась, владельцу о сборке он сигналит сам. Каждый, кто разбирает код
# обёртки, до правки видел в 79 обычную неудачу: «ревизия провалена», «починка
# НЕ удалась» с карточкой тревоги, «Поток проверок упал» владельцу, «проверка
# приёма ПРОВАЛЕНА» с сообщением о битой передаче. Всё это — ложные тревоги
# поверх одного настоящего сигнала носителя.
#
# Список разборщиков берётся РАЗБОРОМ: файлы, где названа обёртка
# (harness/demons/*.sh и scripts/*.py), минус явные исключения с причиной.
# Пустой разбор — провал; новый файл вне списка — провал (решить, разбирает ли
# он код 79); файл списка, переставший звать обёртку, — тоже провал.
#
# Подставная обёртка доезжает до демона дверью окружения CLAUDE_DEMON_SH
# (решение ведущего по Н-3 контракта): она пишет имя демона в след и выходит
# с 79, ничего не печатая, — фраза «личность не собрана» в журнале может
# взяться только от самого демона. Каталог проекта — стенд без своей
# claude-demon.sh: демон, мимо двери зовущий $PROJECT_DIR/scripts/claude-demon.sh,
# получает 127 и красный «обёртка не звана».
#
# Пять демонов гоняются ЦЕЛИКОМ на стенде (свой проект, свой LOG_DIR, подставные
# tg_send.sh, alert-task.py, board.py, pause.py; окружение собрано с нуля env -i).
# session-warden.sh целиком не гоняется: вызов (стр. ~4120) стоит в конце
# ротации, после подъёма агента в tmux. Судится его УЧАСТОК: от строки вызова
# обёртки с именем session-warden до конца функции, с подставными say, jlog,
# tg_send, model_dlya.
#
#   bash scripts/test_carrier_exit79.sh
# Код возврата: 0 — все случаи прошли, 1 — есть упавший.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
source "$HERE/lib/selftest_cases.sh"
# Две раскладки: проект (scripts/ рядом с harness/demons) и пакет (harness/scripts).
DEMONS=""
for cand in "$HERE/../harness/demons" "$HERE/../demons"; do
    [ -d "$cand" ] && { DEMONS="$(cd "$cand" && pwd)"; break; }
done
[ -n "$DEMONS" ] || { echo "ПРОБА СЛОМАНА: каталога демонов нет рядом с $HERE"; exit 1; }

STAND="$(mktemp -d "${TMPDIR:-/tmp}/sap-f2-carrier79-XXXXXX")"
trap 'rm -rf "$STAND"' EXIT

# Разборщики, которые проба гоняет, и чем: «стенд» — демон целиком, «участок» —
# кусок файла от вызова обёртки до конца функции.
RUNNERS="check-builder.sh project-revision.sh session-warden.sh checks-stream.sh ton-watch.sh devmap-selfheal.sh"
declare -A EXCLUDED=(
    [check-daemon-calls-model.py]="гейт: ищет вызовы модели мимо обёртки, код 79 не разбирает"
    [limit-in-log.py]="судья лимита: зовётся самой обёрткой, кода модели не получает"
    [check-login-expiry.py]="разбирает код 79 сам, его случай — scripts/test_login_expiry_exit79.py (K19)"
    [test_process_dispatcher_v2.py]="проба: обёртку называет в тексте стенда"
    [test_process_state.py]="проба разборщика состояний: свой claude-demon.sh с другим кодом лимита, код 79 не разбирает"
)

echo "── разборщики кода обёртки — разбором дерева ──"
mapfile -t FOUND < <(grep -l 'claude-demon\.sh' "$DEMONS"/*.sh "$HERE"/*.py 2>/dev/null \
                     | xargs -r -n1 basename | grep -vx 'claude-demon\.sh' | sort -u)
case_check "разбор нашёл файлы, зовущие обёртку (пустой разбор — не доказательство)" да \
    "$([ "${#FOUND[@]}" -gt 0 ] && echo да || echo нет)"
outside=()
for f in "${FOUND[@]}"; do
    case " $RUNNERS " in *" $f "*) continue ;; esac
    [ -n "${EXCLUDED[$f]:-}" ] && continue
    outside+=("$f")
done
case_check "новых разборщиков вне списка проб и вне исключений нет" "" "${outside[*]:-}"
gone=()
for f in $RUNNERS "${!EXCLUDED[@]}"; do
    printf '%s\n' "${FOUND[@]}" | grep -qx -- "$f" || gone+=("$f")
done
case_check "каждый файл списка по-прежнему зовёт обёртку (список не устарел)" "" "${gone[*]:-}"

# Подставная обёртка: имя демона в след, код 79, ни слова на экран.
STUB="$STAND/claude-demon-79.sh"
cat > "$STUB" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "${1:-}" >> "$STUB_CALLS"
exit 79
SH
chmod +x "$STUB"

new_stand() {  # $1 = имя случая → каталог стенда
    local d="$STAND/$1"
    mkdir -p "$d/proj/scripts/lib" "$d/log" "$d/hb" "$d/home" "$d/state/panel-state"
    : > "$d/calls"; : > "$d/tg.log"; : > "$d/alerts.log"
    cat > "$d/proj/scripts/tg_send.sh" <<SH
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$d/tg.log"
SH
    cat > "$d/proj/scripts/alert-task.py" <<PY
import sys
with open("$d/alerts.log", "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
sys.exit(1 if "--is-green" in sys.argv else 0)
PY
    cp "$d/proj/scripts/alert-task.py" "$d/proj/scripts/board.py"
    printf 'import sys\nsys.exit(1)\n' > "$d/proj/scripts/pause.py"
    printf '#!/usr/bin/env bash\nexit 0\n' > "$d/proj/scripts/pre-commit-gate.sh"
    printf 'model_dlya() { echo haiku; }\n' > "$d/proj/scripts/lib/models.sh"
    cat > "$d/install.conf" <<CONF
PROJECT_NAME="проба"
PROJECT_DIR="$d/proj"
LOG_DIR="$d/log"
HEARTBEAT_DIR="$d/hb"
STATE_DIR="$d/state"
PANEL_STATE_DIR="$d/state/panel-state"
AGENT_USER="$(id -un)"
CONF
    : > "$d/harness.conf"
    chmod +x "$d"/proj/scripts/*.sh
    printf '%s' "$d"
}

git_init() {  # $1 = каталог; коммит всего, сообщение без трейлера Dev-Map
    git -C "$1" init -q -b master . && git -C "$1" add -A \
        && git -C "$1" -c user.name=проба -c user.email=проба@проба commit -q -m "стенд: исходное дерево"
}

# Окружение с нуля: переменные смены (LOG_DIR, MODELS_CONF, HARNESS_EVENTS_STATE…)
# не доезжают до демона, и он не пишет в живые каталоги.
run_daemon() {  # $1 = стенд, $2 = файл демона, дальше — доводы; вывод в run.out
    local d=$1 daemon=$2; shift 2
    (cd "$d" && env -i PATH="/usr/local/bin:/usr/bin:/bin" HOME="$d/home" LC_ALL=C.UTF-8 TMPDIR="$d" \
        HARNESS_HOME="$d/home" HARNESS_INSTALL_CONF="$d/install.conf" HARNESS_CONF="$d/harness.conf" \
        PROJECT_DIR="$d/proj" LOG_DIR="$d/log" HEARTBEAT_DIR="$d/hb" STATE_DIR="$d/state" \
        PANEL_STATE_DIR="$d/state/panel-state" CLAUDE_DEMON_SH="$STUB" STUB_CALLS="$d/calls" \
        ${EXTRA_ENV[@]+"${EXTRA_ENV[@]}"} timeout 300 bash "$daemon" "$@") > "$d/run.out" 2>&1
    echo $? > "$d/run.rc"
}

# Провал и тревога о входе — в журнале; владельцу и на доску вдобавок нельзя
# называть код 79 как неудачу: о сборке говорит носитель.
BAD_LOG='[Пп][Рр][Оо][Вв][Аа][Лл]|ДОСТУП НЕ РАБОТАЕТ|[Нн][Ее] [Уу][Дд][Аа][Лл]|[Уу][Пп][Аа][Лл]'
BAD_MSG="$BAD_LOG|(^|[^0-9])79([^0-9]|\$)"

judge() {  # $1 = стенд, $2 = имя демона в вызове обёртки, $3 = подпись случая
    local d=$1 name=$2 who=$3 text
    case_check "$who: подставная обёртка взята из двери CLAUDE_DEMON_SH и звана" да \
        "$(grep -qx -- "$name" "$d/calls" && echo да || echo нет)"
    text=$(cat "$d/run.out"; find "$d/log" -type f -exec cat {} + 2>/dev/null)
    case_check "$who: в журнале демона «личность не собрана»" да \
        "$(printf '%s\n' "$text" | grep -q 'личность не собрана' && echo да || echo нет)"
    case_check "$who: в журнале нет «провал…», «не удалась», «упал», «ДОСТУП НЕ РАБОТАЕТ»" "" \
        "$(printf '%s\n' "$text" | grep -E -- "$BAD_LOG" | head -3 | tr '\n' ' ')"
    case_check "$who: владельцу и на доску — ни тревоги (--key), ни провала, ни кода 79" "" \
        "$(cat "$d/tg.log" "$d/alerts.log" | grep -E -- "$BAD_MSG|--key" | head -3 | tr '\n' ' ')"
}

echo "── check-builder.sh: стенд целиком ──"
d=$(new_stand check-builder)
cp "$HERE/acceptance.py" "$d/proj/scripts/"
cp "$HERE"/lib/*.py "$d/proj/scripts/lib/"
cat > "$d/proj/dev-map.yaml" <<'MAP'
version: 1
lines:
  - id: harness
    layer: infra
    name: "Ядро"
    tasks:
      - id: t.без-способа
        status: test
        title: "Нет проверки"
        summary: "x"
MAP
git_init "$d/proj"
EXTRA_ENV=(ACTIVE_LINE=harness HARNESS_ACTIVE_LINE=harness)
run_daemon "$d" "$DEMONS/check-builder.sh"
judge "$d" check-builder "check-builder"

echo "── ton-watch.sh: стенд целиком ──"
d=$(new_stand ton-watch)
printf 'print("замер речи: 12 сообщений, медиана 40 знаков")\n' > "$d/proj/scripts/tone-measurement.py"
mkdir -p "$d/log/inbox/обработано" "$d/proj/память"
: > "$d/log/inbox/обработано/сообщение.txt"
EXTRA_ENV=()
run_daemon "$d" "$DEMONS/ton-watch.sh"
judge "$d" ton-watch "ton-watch"

echo "── devmap-selfheal.sh: стенд целиком ──"
d=$(new_stand devmap-selfheal)
cat > "$d/proj/dev-map.yaml" <<'MAP'
version: 1
lines:
  - id: harness
    layer: infra
    name: "Ядро"
    tasks:
      - id: t.живая
        status: wip
        title: "Живая"
        summary: "x"
MAP
git_init "$d/proj"
EXTRA_ENV=()
run_daemon "$d" "$DEMONS/devmap-selfheal.sh"
judge "$d" devmap-selfheal "devmap-selfheal"

echo "── checks-stream.sh: стенд целиком ──"
d=$(new_stand checks-stream)
printf 'print("проверок: 1\\nhr.a k:a")\n' > "$d/proj/scripts/work-queue.py"
: > "$d/proj/dev-map.yaml"
echo "0.1 0.1 0.1 1/1 1" > "$d/loadavg"
EXTRA_ENV=(CHECKS_STREAM_LOADAVG_FILE="$d/loadavg" CHECKS_STREAM_NPROC=1)
run_daemon "$d" "$DEMONS/checks-stream.sh"
judge "$d" checks-stream "checks-stream"

echo "── project-revision.sh: стенд целиком ──"
d=$(new_stand project-revision)
mkdir -p "$d/proj/harness/config" "$d/proj/docs"
cat > "$d/proj/dev-map.yaml" <<'MAP'
version: 1
lines:
  - id: harness
    layer: infra
    name: "Ядро"
    tasks:
      - id: hr.живая
        status: wip
        title: "Живая задача"
        summary: "её трогать нельзя"
MAP
printf 'echo a\n' > "$d/proj/scripts/a.sh"
printf 'модели:\n  ревизия: opus\n  дежурный: haiku\nумолчание: haiku\n' > "$d/proj/harness/config/models.yaml"
cat > "$d/proj/harness/config/audit.yaml" <<'CONF'
пороги: {знаков_на_порцию: 5000, потолок_минут: 2, максимум_находок: 3,
         максимум_знаков_карточки: 900, срок_находки_суток: 90}
области:
  - {имя: проба, пути: ['scripts/*.sh'], вопрос: 'что не так'}
CONF
printf 'ACTIVE_LINE="harness"\nREVIZIA_ENABLED=1\nREVIZIA_MODEL="opus"\nSERVICE_LOCKS_SUBDIR="proba"\n' > "$d/harness.conf"
git_init "$d/proj"
EXTRA_ENV=(REVIZIA_CONF="$d/proj/harness/config/audit.yaml")
run_daemon "$d" "$DEMONS/project-revision.sh" --сейчас
judge "$d" project-revision "project-revision"

echo "── session-warden.sh: участок проверки приёма после ротации ──"
d=$(new_stand session-warden)
# Участок: от строки вызова обёртки с именем session-warden до конца функции.
python3 - "$DEMONS/session-warden.sh" > "$d/участок.sh" <<'PY'
import re, sys
lines = open(sys.argv[1], encoding="utf-8").read().split("\n")
call = re.compile(r'(claude-demon\.sh|CLAUDE_DEMON_SH)\S*"?\s+session-warden\s')
starts = [i for i, s in enumerate(lines) if call.search(s) and not s.lstrip().startswith("#")]
if len(starts) != 1:
    sys.exit(f"вызовов обёртки с именем session-warden {len(starts)} (ждали 1)")
end = next((i for i in range(starts[0], len(lines)) if lines[i] == "}"), None)
if end is None:
    sys.exit("конца функции после вызова нет")
print("\n".join(lines[starts[0]:end]))
PY
seg_rc=$?
case_check "участок session-warden найден по вызову обёртки" 0 "$seg_rc"
cat > "$d/прогон.sh" <<SH
say() { printf '%s\n' "\$*" >> "$d/log/session-warden.log"; }
jlog() { printf '%s\n' "\$*" >> "$d/log/rotation.jsonl"; }
tg_send() { printf '%s\n' "\$*" >> "$d/tg.log"; }
alert_task() { printf '%s\n' "\$*" >> "$d/alerts.log"; }
model_dlya() { echo haiku; }
SESSION_ID=проба-сессия
REASON="проба"
segment() {
    local answer rc=0
$(cat "$d/участок.sh")
}
segment
SH
EXTRA_ENV=()
run_daemon "$d" "$d/прогон.sh"
judge "$d" session-warden "session-warden (участок)"

printf '\nитог test_carrier_exit79: случаев %s, неудач %s\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
