#!/usr/bin/env bash
# Проба: работа помощников — это работа (карточка
# hr.false-stall-alarm-while-subagents-work, спека и контракт в .tasks/ той же
# карточки).
#
# Больной случай 25.10.2026: главная сессия стояла у приглашения и ждала
# уведомления помощника, помощник работал (последний ответ 0,7 мин назад), а
# сторож объявил «Работа встала: агент молчит 20 минут» — в том же обходе, что
# и пинок, на который смена ответила через 3 секунды. Следом ежеминутный сторож
# написал «простой 0 мин… смена просто молчала». Тот же вечер: два сообщения
# «Лимит обновился» об одном событии и число простоя от возраста метки (99), а
# не от последней работы (102).
#
# Три стенда, все свои, боевой канал и боевая панель не трогаются:
#   С — прибор «судья работы» (stall-in-log.py --работа) на записанных формах
#       транскриптов: главная сессия + subagents/agent-<id>.jsonl;
#   А — сторож сессий целым прогоном: своя tmux-панель (копия cat под именем
#       claude пишет всё, что ей прислали, в файл), свой LOG_DIR, канал-файл;
#   Р — «фоновые задачи живы» перед ротацией: функция сторожа на своём
#       каталоге задач (ссылка вывода помощника на его транскрипт);
#   Б — дверь канала tg_send.sh, судья метки limit_mark.py и limit-watch.sh:
#       подставные curl и tmux, ключ стенда сгенерирован.
# Формы записей сняты по СТРУКТУРЕ с живого транскрипта 25.10 (запуск
# toolUseResult.status=async_launched, SendMessage, <task-notification> в трёх
# типах записей, отказ isApiErrorMessage); текст переписки выдуман.
#
# tmux стенда — свой сервер на прогон (-L warden-subwork-<pid>), гасится по
# выходу. Имена переменных латиницей (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"
DAEMON="$HERE/../harness/demons/session-warden.sh"
WATCHER="$HERE/../harness/demons/limit-watch.sh"
# В пакете демоны лежат рядом (harness/demons), в проекте — под harness/.
[ -f "$DAEMON" ] || DAEMON="$HERE/../demons/session-warden.sh"
[ -f "$WATCHER" ] || WATCHER="$HERE/../demons/limit-watch.sh"
for f in "$DAEMON" "$WATCHER"; do
    [ -f "$f" ] || { echo "ПЛОХО: демона нет по пути раскладки: $f — проверять нечего"; exit 1; }
done

PREFIX="warden-subwork-"
T=$(mktemp -d -t warden-subwork.XXXXXX)
PANE="${PREFIX}$$"
# Свой сервер tmux на прогон: панель стенда на боевом сервере ворота выката
# читали как работу агента (11.11.2026). Обёртка первой в PATH ведёт туда и
# голый tmux сторожа; TMUX снят, иначе клиент пошёл бы по сокету из неё.
# Сокет — в каталоге стенда (TMUX_TMPDIR) и уходит вместе с ним.
REAL_TMUX="$(command -v tmux)"
SOCK_NAME="$PANE"
unset TMUX TMUX_PANE
export TMUX_TMPDIR="$T/tmux"
mkdir -p "$T/tmux-bin" "$TMUX_TMPDIR"
printf '#!/bin/sh\nexec "%s" -L "%s" "$@"\n' "$REAL_TMUX" "$SOCK_NAME" > "$T/tmux-bin/tmux"
chmod +x "$T/tmux-bin/tmux"
export PATH="$T/tmux-bin:$PATH"
# Проба личности сторожа (self-prompt-live.sh) со стенда ушла бы фоном к модели
# через claude-run.sh стенда, а её тревога легла бы в канал стенда посреди
# чужого случая. Стенду — пустая проба.
printf '#!/bin/sh\nexit 0\n' > "$T/self-prompt-live.sh"
export SELF_PROMPT_LIVE_SH="$T/self-prompt-live.sh"
# Прогон, убитый KILL-ом, ловушку не зовёт: панель сама гасит свой сервер,
# когда прогона или каталога стенда не стало. Сервер — по pid из $TMUX панели:
# сокет к тому мигу может уйти вместе с каталогом. Сторож отвязан двойным
# fork: потомок claude в панели сторож сессий читал бы как работу агента.
pane_cmd() {  # $1 = команда панели → она же со сторожем жизни стенда
    printf '( { while kill -0 %s && [ -d %q ]; do sleep 2; done; s=${TMUX#*,}; kill "${s%%%%,*}"; } >/dev/null 2>&1 & ); %s' \
        "$$" "$T" "$1"
}
cleanup() { "$REAL_TMUX" -L "$SOCK_NAME" kill-server 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT

SID="5d2a9c1e-7b3f-4e10-9a8d-0c1b2a3f4e5d"
H0="a0c57aae0ddba1100"
H1="a1511aa2c0ffee001"
H2="a2b2b2b2b2b2b2b20"
H9="a9f9f9f9f9f9f9f90"
DESC1="Мутации к автоуборке диска Ф1 и Ф2"

# ── генератор транскриптов стенда ───────────────────────────────────────────
# Строки сцены: «main <вид> <сек назад> [id] [описание]», «helper <id> <вид>
# <сек назад>», «mtime <сек назад>», «shift <сек>» (вся сцена в прошлом — для
# проверки --на). Виды главной: ok, err, launch, send, notify-user,
# notify-attachment, notify-queue; помощника: ok, tool (Bash в полёте), err,
# launch <id> <описание> (помощник второго уровня запускается В ТРАНСКРИПТЕ
# родителя — замер 25.10: 8 из 8 таких, в главной только их уведомления).
cat > "$T/scene.py" <<'PY'
import datetime, json, os, sys, time, uuid

folder, sid = sys.argv[1], sys.argv[2]
main_events, helper_events, descriptions = [], {}, {}
mtime_ago, shift = None, 0
for raw in sys.stdin.read().splitlines():
    parts = raw.split(maxsplit=4)
    if not parts:
        continue
    if parts[0] == "shift":
        shift = int(parts[1])
    elif parts[0] == "mtime":
        mtime_ago = int(parts[1])
    elif parts[0] == "main":
        main_events.append((parts[1], int(parts[2]), parts[3:]))
        if parts[1] == "launch":
            descriptions[parts[3]] = parts[4] if len(parts) > 4 else ""
    elif parts[0] == "helper":
        parts = raw.split(maxsplit=5)
        helper_events.setdefault(parts[1], []).append((parts[2], int(parts[3]), parts[4:]))
        if parts[2] == "launch":
            descriptions[parts[4]] = parts[5] if len(parts) > 5 else ""
now = time.time() - shift


def stamp(ago):
    moment = datetime.datetime.fromtimestamp(now - ago, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def base(kind, ago, **extra):
    record = {"parentUuid": None, "isSidechain": False, "type": kind, "uuid": str(uuid.uuid4()),
              "timestamp": stamp(ago), "sessionId": sid, "userType": "external",
              "entrypoint": "cli", "cwd": "/stand", "version": "2.1.299"}
    record.update(extra)
    return record


def answer(ago, content, **extra):
    return base("assistant", ago, message={"model": "claude-stand", "type": "message", "role": "assistant",
                                           "content": content, "stop_reason": "end_turn"}, **extra)


def refusal(ago, **extra):
    return base("assistant", ago, error="rate_limit", isApiErrorMessage=True, apiErrorStatus=429,
                message={"model": "<synthetic>", "role": "assistant",
                         "content": [{"type": "text", "text": "API Error: rate limit reached"}]}, **extra)


def notification(agent_id):
    return ("<task-notification>\n<task-id>" + agent_id + "</task-id>\n<tool-use-id>toolu_stand_" + agent_id
            + "</tool-use-id>\n<output-file>/tmp/stand/" + sid + "/tasks/" + agent_id + ".output</output-file>\n"
            + "<status>completed</status>\n<summary>Agent \"" + descriptions.get(agent_id, "") + "\" completed"
            + "</summary>\n</task-notification>")


def main_records(kind, ago, rest):
    if kind == "ok":
        return [answer(ago, [{"type": "text", "text": " ".join(rest) or "Жду."}])]
    if kind == "err":
        return [refusal(ago)]
    if kind == "launch":
        agent_id, text = rest[0], descriptions[rest[0]]
        call = {"type": "tool_use", "id": "toolu_stand_" + agent_id, "name": "Agent",
                "input": {"description": text, "prompt": "задание стенда", "subagent_type": "general-purpose"}}
        result = {"isAsync": True, "status": "async_launched", "agentId": agent_id, "description": text,
                  "outputFile": "/tmp/stand/" + sid + "/tasks/" + agent_id + ".output",
                  "canReadOutputFile": True, "canContinueAgent": True}
        return [answer(ago, [call]),
                base("user", ago, message={"role": "user", "content": [
                    {"tool_use_id": "toolu_stand_" + agent_id, "type": "tool_result",
                     "content": [{"type": "text", "text": "Async agent launched successfully."}]}]},
                     toolUseResult=result)]
    if kind == "send":
        agent_id = rest[0]
        call = {"type": "tool_use", "id": "toolu_send_" + agent_id, "name": "SendMessage",
                "input": {"to": agent_id, "message": "Продолжай с того места, где остановился."}}
        return [answer(ago, [call]),
                base("user", ago, message={"role": "user", "content": [
                    {"tool_use_id": "toolu_send_" + agent_id, "type": "tool_result",
                     "content": [{"type": "text", "text": "Message sent."}]}]})]
    if kind == "notify-user":
        return [base("user", ago, message={"role": "user", "content": notification(rest[0])},
                     origin={"kind": "task-notification", "producer": "session-task"},
                     turnOrigin="task_notification", promptSource="system")]
    if kind == "notify-attachment":
        return [base("attachment", ago, attachment={"type": "queued_command", "prompt": notification(rest[0]),
                                                     "commandMode": "task-notification"})]
    if kind == "notify-queue":
        return [{"type": "queue-operation", "operation": "enqueue", "timestamp": stamp(ago),
                 "sessionId": sid, "content": notification(rest[0])}]
    raise SystemExit("неизвестный вид записи главной: " + kind)


def helper_records(agent_id, kind, ago, rest):
    tag = {"agentId": agent_id, "isSidechain": True}
    if kind == "launch":
        return [dict(record, **tag) for record in main_records("launch", ago, rest)]
    if kind == "ok":
        return [answer(ago, [{"type": "text", "text": "Шаг сделан."}], **tag)]
    if kind == "tool":
        return [answer(ago, [{"type": "tool_use", "id": "toolu_bash_" + str(ago), "name": "Bash",
                              "input": {"command": "bash scripts/long-run.sh"}}], **tag)]
    if kind == "err":
        return [refusal(ago, **tag)]
    raise SystemExit("неизвестный вид записи помощника: " + kind)


main_timed = []
for kind, ago, rest in main_events:
    for record in main_records(kind, ago, rest):
        main_timed.append((ago, len(main_timed), record))
main_timed.sort(key=lambda item: (-item[0], item[1]))
main_path = os.path.join(folder, sid + ".jsonl")
os.makedirs(folder, exist_ok=True)
with open(main_path, "w", encoding="utf-8") as stream:
    for _, _, record in main_timed:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
newest = min((ago for ago, _, _ in main_timed), default=0)
stamp_main = now - (mtime_ago if mtime_ago is not None else newest)
os.utime(main_path, (stamp_main, stamp_main))

launched_at = {rest[0]: ago for kind, ago, rest in main_events if kind == "launch"}
for events in helper_events.values():
    launched_at.update({rest[0]: ago for kind, ago, rest in events if kind == "launch"})
for agent_id, events in helper_events.items():
    start = launched_at.get(agent_id, max(ago for _, ago, _ in events) + 1)
    timed = [(start, 0, base("user", start, agentId=agent_id, isSidechain=True,
                             message={"role": "user", "content": "задание стенда"}))]
    for kind, ago, rest in events:
        for record in helper_records(agent_id, kind, ago, rest):
            timed.append((ago, len(timed), record))
    timed.sort(key=lambda item: (-item[0], item[1]))
    path = os.path.join(folder, sid, "subagents", "agent-" + agent_id + ".jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        for _, _, record in timed:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    newest_helper = min(ago for _, ago, _ in events)
    os.utime(path, (now - newest_helper, now - newest_helper))
    with open(path[:-len(".jsonl")] + ".meta.json", "w", encoding="utf-8") as stream:
        json.dump({"agentType": "general-purpose", "description": descriptions.get(agent_id, ""),
                   "toolUseId": "toolu_stand_" + agent_id, "spawnDepth": 1,
                   "requestShape": "background", "model": "sonnet"}, stream, ensure_ascii=False)
PY

scene() {  # $1 = каталог транскриптов проекта; сцена — на stdin
    rm -rf "$1"; mkdir -p "$1"
    python3 "$T/scene.py" "$1" "$SID" || { echo "ПЛОХО: генератор сцены упал — проверять нечего"; exit 1; }
}
iso_ago() { date -u -d "@$(( $(date +%s) - $1 ))" +%Y-%m-%dT%H:%M:%S.000Z; }

# ── стенд С: судья работы ───────────────────────────────────────────────────
C_DIR="$T/c/transcripts/-stand-c"
C_MAIN="$C_DIR/$SID.jsonl"
mkdir -p "$T/c/scripts/lib"
cp "$HERE/stall-in-log.py" "$T/c/scripts/"
cp "$HERE"/lib/*.py "$HERE/lib/config.sh" "$T/c/scripts/lib/" 2>/dev/null
printf 'HELPER_FRESH_MIN=20\n' > "$T/c/harness.conf"

judge() {  # доводы после --работа; пишет judge.out/err/rc
    local rc=0
    env HELPER_FRESH_MIN=20 HARNESS_CONF="$T/c/harness.conf" \
        python3 "$T/c/scripts/stall-in-log.py" --работа "$@" > "$T/judge.out" 2> "$T/judge.err" || rc=$?
    printf '%s\n' "$rc" > "$T/judge.rc"
}
# Значение поля судьи. Нет строки «работа_мин» — прибор поведения не знает:
# это и есть причина красного, она печатается вместо числа.
field() {
    if ! grep -q "^работа_мин"$'\t' "$T/judge.out" 2>/dev/null; then
        printf 'нет поведения: stall-in-log.py --работа не ответил (код %s)\n' "$(cat "$T/judge.rc")"
        return
    fi
    awk -F'\t' -v key="$1" '$1 == key { print $2; exit }' "$T/judge.out"
}
helper_line() {  # $1 = id → «<мин с ответа> <описание>» из строки помощника
    if ! grep -q "^работа_мин"$'\t' "$T/judge.out" 2>/dev/null; then
        printf 'нет поведения: stall-in-log.py --работа не ответил\n'; return
    fi
    awk -F'\t' -v id="$1" '$1 == "помощник" && $2 == id { print $3 " " $5; exit }' "$T/judge.out"
}

echo "── С. судья работы: главная ИЛИ живой помощник ──"
scene "$C_DIR" <<EOF
main launch 2700 $H0 Код Ф-а
main launch 2430 $H1 $DESC1
main notify-user 1620 $H0
main ok 1260 Жду.
helper $H0 ok 1625
helper $H1 ok 600
helper $H1 tool 300
helper $H1 ok 42
mtime 1260
EOF
judge "$C_MAIN"
case_check "Б1 судья: главная 21 мин, помощник ответил 42 с назад — работа 0 мин" "0" "$(field работа_мин)"
case_check "Б1 судья: главная названа отдельно (21 мин)" "21" "$(field главная_мин)"
case_check "Б1 судья: живой помощник ровно один (закончивший H0 не в счёт)" "1" "$(field помощников)"
case_check "Б1 судья: строка помощника — минуты с ответа и описание из .meta.json" \
    "0 $DESC1" "$(helper_line "$H1")"

scene "$C_DIR" <<EOF
shift 7200
main launch 2700 $H0 Код Ф-а
main launch 2430 $H1 $DESC1
main notify-user 1620 $H0
main ok 1260 Жду.
helper $H0 ok 1625
helper $H1 ok 42
EOF
judge "$C_MAIN" --на "$(iso_ago 7200)"
case_check "Б1 повтор по --на: тот же момент двумя часами раньше — помощник жив" "1" "$(field помощников)"
judge "$C_MAIN"
case_check "Б1 без --на: два часа спустя тот же помощник уже не работа" "0" "$(field помощников)"

scene "$C_DIR" <<EOF
main launch 3600 $H1 Помощник А
main launch 3590 $H2 Помощник Б
main ok 3120
main err 3000
main notify-user 2940 $H1
main notify-user 2938 $H2
main err 60
helper $H1 ok 3300
helper $H1 err 3000
helper $H2 ok 3250
helper $H2 err 3001
EOF
judge "$C_MAIN"
case_check "Б2 судья: главная в 429, оба помощника уведомили с отказом — помощников 0" "0" "$(field помощников)"
case_check "Б2 судья: работа — по главной (52 мин)" "52" "$(field работа_мин)"

scene "$C_DIR" <<EOF
main launch 1800 $H1 $DESC1
main ok 1500
helper $H1 ok 180
helper $H1 err 60
EOF
judge "$C_MAIN"
# Отказ API не отменяет «идёт»: CLI повторит запрос, ротация оборвала бы помощника так же.
case_check "Б3 судья: помощник в реестре, отказ свежее ответа, уведомления нет — идёт" "1" "$(field помощников)"
case_check "Б3 судья: работа — по главной (25 мин)" "25" "$(field работа_мин)"

for kind in notify-user notify-attachment notify-queue; do
    scene "$C_DIR" <<EOF
main launch 2400 $H1 $DESC1
main ok 2390
main $kind 300 $H1
helper $H1 ok 300
EOF
    judge "$C_MAIN"
    case_check "Б4 судья ($kind): помощник уведомил 5 мин назад — не работа" "0" "$(field помощников)"
done

scene "$C_DIR" <<EOF
main launch 3600 $H1 $DESC1
main notify-user 1800 $H1
main send 1200 $H1
helper $H1 ok 1850
helper $H1 ok 120
EOF
judge "$C_MAIN"
case_check "Б4г судья: уведомил, затем поднят SendMessage — снова работа (2 мин)" "2" "$(field работа_мин)"

scene "$C_DIR" <<EOF
main launch 1800 $H1 $DESC1
main ok 1500
helper $H1 ok 1000
helper $H1 tool 900
EOF
judge "$C_MAIN"
case_check "Б5 судья: Bash помощника в полёте 15 мин — работа (15)" "15" "$(field работа_мин)"

scene "$C_DIR" <<EOF
main launch 3000 $H1 $DESC1
main ok 1500
helper $H1 ok 1500
EOF
judge "$C_MAIN"
# «Идёт» = запуск без результата: помощник в длинном инструменте молчит десятки минут.
case_check "Б6 судья: помощник в реестре молчит 25 мин, уведомления нет — идёт" "1" "$(field помощников)"
case_check "Б6 судья: молчание 25 мин — не работа (мерка пульса: ответ ≤ 20 мин)" "25" "$(field работа_мин)"
# Файл помощника освежает не только ответ (результат инструмента, служебные
# записи): свежий mtime при ответе 25 мин назад — порог по ОТВЕТУ решает сам.
touch -d "@$(( $(date +%s) - 60 ))" "$C_DIR/$SID/subagents/agent-$H1.jsonl"
judge "$C_MAIN"
case_check "Б6б судья: файл помощника тронут 1 мин назад, ответ 25 мин назад — идёт, но не работа" "1 25" "$(field помощников) $(field работа_мин)"

scene "$C_DIR" <<EOF
main ok 1500
helper $H9 ok 60
EOF
judge "$C_MAIN"
case_check "Б12 судья: запуска в реестре нет, а транскрипт помощника жив — код 3" "3" "$(cat "$T/judge.rc")"
case_check "Б12 судья: отказ назван (слово «реестр» в причине)" \
    "да" "$(grep -q 'реестр' "$T/judge.err" && echo да || echo нет)"
case_check "Б12 судья: «помощников 0» при отказе не печатается" \
    "нет" "$(grep -q "^помощников"$'\t'"0" "$T/judge.out" && echo да || echo нет)"
scene "$C_DIR" <<EOF
main ok 1500
helper $H9 ok 3600
EOF
judge "$C_MAIN"
case_check "Б12б судья: давний чужой транскрипт помощника — не отказ, мерит (код 0)" "0" "$(cat "$T/judge.rc")"

scene "$C_DIR" <<EOF
main launch 3000 $H1 Родитель
main notify-queue 600 $H1
main ok 590
helper $H1 ok 2900
helper $H1 launch 2800 $H2 Внук: мутации второго уровня
helper $H1 ok 610
helper $H2 ok 60
EOF
judge "$C_MAIN"
case_check "Б14 судья: помощник второго уровня (запуск в транскрипте родителя) — не отказ (код 0)" "0" "$(cat "$T/judge.rc")"
case_check "Б14 судья: родитель уведомил, внук работает — помощников 1, работа 1" \
    "1 1" "$(field помощников) $(field работа_мин)"

# ── стенд Р: «фоновые задачи живы» перед ротацией ─────────────────────────
# Для помощника tasks/<id>.output — символическая ссылка на его транскрипт:
# время ссылки — миг запуска, время цели — последняя запись. Функция зовётся
# из файла сторожа (тот же приём, что у самотеста tg_send), каталог задач свой.
echo "── Р. ротация: ссылка вывода помощника старая, его транскрипт свежий ──"
R_SID="сессия-ротации"
R_TASKS="$T/r/claude-1/proj/$R_SID/tasks"
mkdir -p "$R_TASKS"
tasks_alive() {  # → «живы» | «нет»
    TASKS_ROOT="$T/r" ROTATE_TASK_FRESH_SEC=120 bash -c '
        source <(sed -n "/^фоновые_задачи_живы() {/,/^}/p" "$1")
        фоновые_задачи_живы "$2" && echo живы || echo нет' _ "$DAEMON" "$R_SID"
}
: > "$T/r/agent-$H1.jsonl"
ln -s "$T/r/agent-$H1.jsonl" "$R_TASKS/$H1.output"
touch -h -d "@$(( $(date +%s) - 437 ))" "$R_TASKS/$H1.output"
case_check "Р1: ссылка 437 с, цель 0 с — помощник жив, ротация ждёт" "живы" "$(tasks_alive)"
touch -d "@$(( $(date +%s) - 600 ))" "$T/r/agent-$H1.jsonl"
case_check "Р1 страж: и ссылка, и цель старше порога — не живы" "нет" "$(tasks_alive)"

# ── стенд А: сторож сессий целым прогоном ──────────────────────────────────
cp /bin/cat "$T/claude"
mkdir -p "$T/project/scripts/lib" "$T/projects"
for f in work-queue.py limit_mark.py limit-in-log.py stall-in-log.py; do
    cp "$HERE/$f" "$T/project/scripts/"
done
cp "$HERE"/lib/*.py "$HERE/lib/config.sh" "$T/project/scripts/lib/"
cat > "$T/project/scripts/tg_send.sh" <<CHANNEL
#!/usr/bin/env bash
for A in "\$@"; do
    case "\$A" in --*) continue ;; esac
    printf '%s\n' "\$A" | tr '\n' ' ' >> "$T/channel.txt"
    printf '\n' >> "$T/channel.txt"
    break
done
CHANNEL
chmod +x "$T/project/scripts/tg_send.sh"
A_DIR="$T/projects/${T//\//-}-project"

SEED=""
for c in "$HERE/../config/harness.conf" "${HARNESS_CONF:-/etc/harness/harness.conf}"; do
    [ -f "$c" ] && { SEED="$c"; break; }
done
[ -n "$SEED" ] || { echo "ПЛОХО: образца harness.conf нет ни в пакете, ни в /etc/harness — проверять нечем"; exit 1; }
cp "$SEED" "$T/harness.conf"
cat >> "$T/harness.conf" <<CONF
AGENT_USER="$(id -un)"
LOG_DIR="$T/log"
PROJECT_DIR="$T/project"
STATE_DIR="$T/state"
TMUX_SESSION="$PANE"
AGENT_START_CMD="$T/claude"
SERVICE_LOCKS_SUBDIR="проверка"
ACTIVE_LINE="hr"
CTX_WINDOW_TOKENS=400000  # не-секрет: размер окна модели в токенах, а не ключ
SILENCE_MAX_MIN=10
PULSE_MAX_MIN=30
PULSE_OWNER_MIN=60
LIMIT_STALL_MIN=20
HELPER_FRESH_MIN=20
KICK_ANSWER_WAIT_MIN=5
TG_CHAT_ID=""
TG_TOKEN_FILE="/nonexistent"
CONF

stand_a() {  # свежий LOG_DIR, карта с работой, новая панель
    rm -rf "$T/log" "$T/state" "$T/channel.txt" "$T/pane.txt"
    mkdir -p "$T/log/inbox/обработано" "$T/state/panel-state" "$T/proc"
    : > "$T/channel.txt"
    printf '%s\n' '{"queue": [], "lines": [{"id": "hr", "tasks": [{"id": "hr.b", "status": "wip"}]}]}' \
        > "$T/project/dev-map.yaml"
    echo "слово" > "$T/log/inbox/обработано/old.txt"
    touch -d "@$(( $(date +%s) - 7200 ))" "$T/log/inbox/обработано/old.txt"
    tmux kill-session -t "$PANE" 2>/dev/null
    # Агент — в каталоге проекта, как у боевого start-agent.sh: иначе сторож
    # законно зовёт «запущен не из проекта», и случай судит не ту ветку.
    tmux new-session -d -s "$PANE" -c "$T/project" "$(pane_cmd "exec $T/claude >> $T/pane.txt")"
    wait_until 5 pane_is_claude \
        || { echo "ПЛОХО: подставной агент не встал в панель за 5 с — проверять нечего"; exit 1; }
}
wait_until() {  # $1 = предел в секундах, дальше — условие; код 1, если не дождались
    local tries=$(( $1 * 10 )); shift
    until "$@"; do
        tries=$((tries - 1)); [ "$tries" -gt 0 ] || return 1
        sleep 0.1
    done
}
pane_is_claude() { [ "$(tmux display-message -p -t "$PANE" '#{pane_current_command}')" = claude ]; }
BARRIER="стенд-барьер-панели"
barriers() { local n; n=$(grep -cF -- "$BARRIER" "$T/pane.txt" 2>/dev/null); echo "${n:-0}"; }
barrier_passed() { [ "$(barriers)" -gt "$1" ]; }
run_warden() {
    # Пустой корень /proc: иначе nest_guard судит по живым процессам машины
    # и при пропаже гнезда шлёт USR1 боевому tmux.
    env -u TMUX -u CLAUDE_CODE_SESSION_ID HARNESS_CONF="$T/harness.conf" HARNESS_INSTALL_CONF="$T/harness.conf" \
        HARNESS_ACTIVE_LINE=hr PANE_ENTER_DELAY=0.2 AGENT_PROC_ROOT="$T/proc" \
        HARNESS_PANEL_STATE="$T/state/panel-state" CLAUDE_PROJECTS_DIR="$T/projects" \
        bash "$DAEMON" >> "$T/log/warden.out" 2>&1 < /dev/null
    # Барьер вместо паузы: cat пишет строки по порядку, значит метка, посланная
    # после сторожа, легла в файл панели позже всего, что прислал сторож.
    local before; before=$(barriers)
    tmux send-keys -t "$PANE" -l "$BARRIER" && tmux send-keys -t "$PANE" Enter \
        || { echo "ПЛОХО: панель стенда не принимает ввод — проверять нечего"; exit 1; }
    wait_until 5 barrier_passed "$before" \
        || { echo "ПЛОХО: присланное в панель не дошло до файла панели за 5 с — проверять нечего"; exit 1; }
}
report_ago() { echo $(( $(date +%s) - $1 )) > "$T/log/last_report"; touch -d "@$(( $(date +%s) - $1 ))" "$T/log/last_report"; }
stamp_ago() { echo $(( $(date +%s) - $2 )) > "$T/log/$1"; touch -d "@$(( $(date +%s) - $2 ))" "$T/log/$1"; }
said() { local n; n=$(grep -c -- "$1" "$T/channel.txt" 2>/dev/null); echo "${n:-0}"; }
lines_in() { [ -f "$1" ] && wc -l < "$1" | tr -d ' ' || echo 0; }
event() { local n; n=$(grep -c "\"event\":\"$1\"" "$T/log/rotation.jsonl" 2>/dev/null); echo "${n:-0}"; }
pane_has() { grep -qF -- "$1" "$T/pane.txt" 2>/dev/null && echo да || echo нет; }
exists() { [ -e "$1" ] && echo есть || echo нет; }
MARK_A="$T/log/лимит-подписки.состояние"
pause_a() {  # $1 = on|off — настоящий pause.py на каталоге состояния стенда
    if [ "$1" = on ]; then
        HARNESS_PANEL_STATE="$T/state/panel-state" python3 "$HERE/pause.py" --поставить "проба" --кто владелец >/dev/null \
            || { echo "ПЛОХО: pause.py не поставил паузу на стенде — проверять нечего"; exit 1; }
    else
        HARNESS_PANEL_STATE="$T/state/panel-state" python3 "$HERE/pause.py" --снять >/dev/null \
            || { echo "ПЛОХО: pause.py не снял паузу на стенде"; exit 1; }
    fi
}
fresh_mark() {  # $1 = метка в LOG_DIR → «свежая» (моложе 60 с) | «старая» | «нет»
    [ -e "$T/log/$1" ] || { echo нет; return; }
    [ $(( $(date +%s) - $(stat -c %Y "$T/log/$1") )) -lt 60 ] && echo свежая || echo старая
}
# Ход времени для обходов подряд: метки журнала стенда стареют на N секунд
# (mtime и число-эпоха в первой строке короткой метки), сцена главной
# строится от виртуальных часов VCLOCK — ответы лежат в VMAIN мигами.
VCLOCK=0; VMAIN=""
time_passes() {
    local f m v
    VCLOCK=$((VCLOCK + $1))
    while IFS= read -r f; do
        m=$(stat -c %Y "$f")
        if [ "$(stat -c %s "$f")" -lt 200 ]; then
            v=$(awk 'NR == 1 { print $1; exit }' "$f")
            if [[ "$v" =~ ^[0-9]{10}$ ]] && [ $(( m - v )) -le 120 ] && [ $(( v - m )) -le 120 ]; then
                sed -i "1s/^$v/$(( v - $1 ))/" "$f"
            fi
        fi
        touch -d "@$(( m - $1 ))" "$f"
    done < <(find "$T/log" -type f)
}
main_answers_scene() {  # главная: ответы в мигах VMAIN, на часах VCLOCK
    local at
    scene "$A_DIR" < <(for at in $VMAIN; do printf 'main ok %s Жду.\n' "$(( VCLOCK - at ))"; done)
}

echo "── А. Б1: 25.10 01:50 — главная 21 мин у приглашения, помощник работает ──"
stand_a
# Панель стенда — на своём сервере -L, а не на боевом: 11.11 две такие панели
# висели рядом с сессией агента, и ворота выката читали их как работу.
on_server() { "$@" has-session -t "=$PANE" 2>/dev/null && echo да || echo нет; }
case_check "стенд: панель на своём сервере tmux (-L), на боевом её нет" "да;нет" \
    "$(on_server "$REAL_TMUX" -L "$SOCK_NAME");$(on_server env -u TMUX -u TMUX_TMPDIR "$REAL_TMUX")"
scene "$A_DIR" <<EOF
main launch 2700 $H0 Код Ф-а
main launch 2430 $H1 $DESC1
main notify-user 1620 $H0
main ok 1260 Жду.
helper $H0 ok 1625
helper $H1 ok 600
helper $H1 tool 300
helper $H1 ok 42
mtime 1260
EOF
report_ago 1200
run_warden
case_check "Б1: владельцу ни одного сообщения" "0" "$(lines_in "$T/channel.txt")"
case_check "Б1: события agent_stalled нет" "0" "$(event agent_stalled)"
case_check "Б1: метки остановки (nemota) нет" "нет" "$(exists "$MARK_A")"
case_check "Б1: пинка тишины нет (он приказывает «бери следующую»)" "0" "$(event silence_kick)"
case_check "Б1: в панель не ушло «Работы не видно»" "нет" "$(pane_has 'Работы не видно')"

echo "── А. Б2 и Б11: окно лимита 00:20 — главная в 429, помощники уведомили с отказом ──"
stand_a
scene "$A_DIR" <<EOF
main launch 3600 $H1 Помощник А
main launch 3590 $H2 Помощник Б
main ok 3120
main err 3000
main notify-user 2940 $H1
main notify-user 2938 $H2
main err 1800
main err 600
main err 300
main err 60
helper $H1 ok 3300
helper $H1 err 3000
helper $H2 ok 3250
helper $H2 err 3001
EOF
printf '%s limit %s\n' "$(( $(date +%s) - 2880 ))" "$(( $(date +%s) + 3600 ))" > "$MARK_A"
report_ago 2400
run_warden
case_check "Б2: при стоящей метке лимита вестей владельцу 0" "0" "$(lines_in "$T/channel.txt")"
case_check "Б2: события agent_stalled нет" "0" "$(event agent_stalled)"
case_check "Б11: пульс-пинков в окне лимита 0 (отказы 429 не работа)" "0" "$(event pulse_kick)"
case_check "Б11: в панель не ушло «Владельцу нет отчёта»" "нет" "$(pane_has 'Владельцу нет отчёта')"

echo "── А. Б11б: отказы 429 старше окна лимита, транскрипт свежий — это не работа ──"
# Свежие 429 держат лимит (limit-in-log), и пульс молчит его веткой: возраст
# работы виден суду, только когда признак лимита старше LIMIT_KICK_MAX_MIN (60).
stand_a
scene "$A_DIR" <<EOF
main launch 5400 $H1 Помощник А
main ok 4800
main err 4500
main notify-user 4440 $H1
main err 4000
main err 3700
helper $H1 ok 5300
helper $H1 err 4600
mtime 60
EOF
report_ago 2400
run_warden
case_check "Б11б: пульс-пинков 0 — работа считается по ответу, а не по mtime транскрипта" "0" "$(event pulse_kick)"

echo "── А. Б5: Bash помощника в полёте 15 мин, главная молчит 25 ──"
stand_a
scene "$A_DIR" <<EOF
main launch 1800 $H1 $DESC1
main ok 1500
helper $H1 ok 1000
helper $H1 tool 900
EOF
report_ago 1200
run_warden
case_check "Б5: владельцу ни одного сообщения" "0" "$(lines_in "$T/channel.txt")"
case_check "Б5: события agent_stalled нет" "0" "$(event agent_stalled)"
case_check "Б5: работа 15 мин (> SILENCE_MAX_MIN 10) — пинка тишины нет" "0" "$(event silence_kick)"
case_check "Б5: в панель не ушло «Работы не видно»" "нет" "$(pane_has 'Работы не видно')"

echo "── А. Б5б: Bash помощника в полёте 15 мин, отчёта 31 мин — пульс просит статус ──"
stand_a
scene "$A_DIR" <<EOF
main launch 1800 $H1 $DESC1
main ok 1500
helper $H1 ok 1000
helper $H1 tool 900
EOF
report_ago 1860
run_warden
case_check "Б5б: живой помощник с работой старше порога тишины — пульс-пинок отдан" "1" "$(event pulse_kick)"
case_check "Б5б: в пинке список помощников" "да" "$(pane_has 'Работают помощники:')"

echo "── А. Б6: помощник без результата молчит 25 мин — пинок, без ответа 6 мин — тревоги нет (идёт) ──"
# «Идёт» = запуск без результата; свежесть — только потолок «завис»
# (HELPER_HUNG_MIN): помощник в длинном инструменте молчит десятки минут.
stand_a
scene "$A_DIR" <<EOF
main launch 3000 $H1 $DESC1
main ok 1500
helper $H1 ok 1500
EOF
report_ago 1200
run_warden
case_check "Б6 обход 1: владельцу пока ничего (только пинок)" "0" "$(lines_in "$T/channel.txt")"
case_check "Б6 обход 1: метка пинка немоты stall.kick поставлена" "есть" "$(exists "$T/log/stall.kick")"
case_check "Б6 обход 1: пинок дошёл до панели" "да" "$(pane_has '[сторож сессий]')"
stamp_ago stall.kick 360
: > "$T/channel.txt"; : > "$T/log/rotation.jsonl"
run_warden
case_check "Б6 обход 2: на пинок нет ответа 6 мин, помощник идёт — «Работа встала» нет" "0" "$(said 'Работа встала')"
case_check "Б6 обход 2: события agent_stalled нет" "0" "$(event agent_stalled)"
case_check "Б6 обход 2: метка пинка стоит (ждём дальше, не сбросили)" "есть" "$(exists "$T/log/stall.kick")"

echo "── А. Б6в: помощник молчит 130 мин (> HELPER_HUNG_MIN 120, завис) — пинок, без ответа 6 мин — «Работа встала» ──"
# Пара к Б6: зависший в «помощников» не входит, немоту ничто не держит.
stand_a
scene "$A_DIR" <<EOF
main launch 9000 $H1 $DESC1
main ok 1500
helper $H1 ok 7800
EOF
report_ago 1200
run_warden
case_check "Б6в обход 1: владельцу пока ничего (только пинок)" "0" "$(lines_in "$T/channel.txt")"
case_check "Б6в обход 1: метка пинка немоты stall.kick поставлена" "есть" "$(exists "$T/log/stall.kick")"
stamp_ago stall.kick 360
: > "$T/channel.txt"; : > "$T/log/rotation.jsonl"
run_warden
case_check "Б6в обход 2: помощник завис, на пинок нет ответа 6 мин — «Работа встала» ровно одно" "1" "$(said 'Работа встала')"
case_check "Б6в обход 2: событие agent_stalled в этом обходе" "1" "$(event agent_stalled)"

echo "── А. Б7: главная молчит 21 мин, помощников нет, на пинок ответ ──"
stand_a
scene "$A_DIR" <<EOF
main ok 1260
EOF
report_ago 1200
run_warden
case_check "Б7 обход 1: владельцу ничего — сначала пинок" "0" "$(lines_in "$T/channel.txt")"
case_check "Б7 обход 1: события agent_stalled нет" "0" "$(event agent_stalled)"
case_check "Б7 обход 1: метка пинка немоты stall.kick поставлена" "есть" "$(exists "$T/log/stall.kick")"
case_check "Б7 обход 1: метки остановки нет" "нет" "$(exists "$MARK_A")"
printf '{"type":"assistant","isSidechain":false,"sessionId":"%s","timestamp":"%s","message":{"role":"assistant","content":[{"type":"text","text":"Статус: продолжаю."}]}}\n' \
    "$SID" "$(iso_ago 5)" >> "$A_DIR/$SID.jsonl"
run_warden
case_check "Б7 обход 2: смена ответила на пинок — владельцу за оба обхода 0" "0" "$(lines_in "$T/channel.txt")"
case_check "Б7 обход 2: метка пинка снята" "нет" "$(exists "$T/log/stall.kick")"

echo "── А. Б7б: на пинок 29 мин назад смена работала 6 мин и отчиталась, потом молчит 23 ──"
# Ответы после пинка вместе с доставкой владельцу — признак работы: старый
# пинок погашен, немота начинается заново с нового пинка, а не с тревоги.
stand_a
scene "$A_DIR" <<EOF
main ok 1800
main ok 1700
main ok 1600
main ok 1500
main ok 1400
EOF
stamp_ago stall.kick 1740
report_ago 1450
run_warden
case_check "Б7б: «Работа встала» не ушла" "0" "$(said 'Работа встала')"
case_check "Б7б: события agent_stalled нет" "0" "$(event agent_stalled)"
case_check "Б7б: метка пинка поставлена заново (свежая)" "свежая" "$(fresh_mark stall.kick)"

pulse_scene() {
    stand_a
    scene "$A_DIR" <<EOF
main launch 2400 $H1 $DESC1
main ok 1500
helper $H1 ok 1200
helper $H1 ok 60
EOF
    report_ago 1860
    run_warden
}
echo "── А. Б8: помощники работают, отчёта 31 мин — пульс-пинок со списком; статуса нет 5 мин — одно сообщение ──"
pulse_scene
case_check "Б8 обход 1: пульс-пинок отдан" "1" "$(event pulse_kick)"
case_check "Б8 обход 1: в пинке список помощников" "да" "$(pane_has 'Работают помощники:')"
case_check "Б8 обход 1: в пинке описание помощника из .meta.json" "да" "$(pane_has "$DESC1")"
case_check "Б8 обход 1: пинок запрещает запускать новых помощников" \
    "да" "$(pane_has 'Новых помощников ради отчёта не запускай')"
case_check "Б8 обход 1: пинка тишины нет" "0" "$(event silence_kick)"
case_check "Б8 обход 1: владельцу ничего" "0" "$(lines_in "$T/channel.txt")"
stamp_ago pulse.kick 360
report_ago 2220
run_warden
case_check "Б8 обход 2: статуса нет 6 мин — «не ответила на запрос статуса» одно" "1" "$(said 'не ответила на запрос статуса')"
case_check "Б8 обход 2: в сообщении число помощников" "1" "$(said 'Работают помощники: 1')"
case_check "Б8 обход 2: в сообщении описание помощника" "1" "$(said "$DESC1")"
case_check "Б8 обход 2: в сообщении «Работа не стоит»" "1" "$(said 'Работа не стоит')"
case_check "Б8 обход 2: событие pulse_unanswered" "1" "$(event pulse_unanswered)"

echo "── А. Б8б: статус доставлен после пинка — владельцу 0 ──"
pulse_scene
case_check "Б8б обход 1: пульс-пинок отдан (условие случая создано)" "1" "$(event pulse_kick)"
stamp_ago pulse.kick 360
report_ago 30
: > "$T/channel.txt"
run_warden
case_check "Б8б обход 2: доставка от смены новее пинка — сообщений 0" "0" "$(lines_in "$T/channel.txt")"

echo "── А. Б8в: пульс-пинок 40 мин назад, отчёт после него 35 мин назад — новый пинок, не «не ответила» ──"
stand_a
scene "$A_DIR" <<EOF
main launch 2400 $H1 $DESC1
main ok 1500
helper $H1 ok 1200
helper $H1 ok 60
EOF
stamp_ago pulse.kick 2400
report_ago 2100
run_warden
case_check "Б8в: отчёт новее пинка — «не ответила на запрос статуса» нет" "0" "$(said 'не ответила на запрос статуса')"
case_check "Б8в: события pulse_unanswered нет" "0" "$(event pulse_unanswered)"
case_check "Б8в: отчёта 35 мин — новый пульс-пинок отдан" "1" "$(event pulse_kick)"

echo "── А. Б12: реестр пуст, транскрипт помощника жив — сторож называет отказ судьи ──"
stand_a
scene "$A_DIR" <<EOF
main ok 1500
helper $H9 ok 60
EOF
report_ago 1200
run_warden
case_check "Б12: в журнале сторожа «судья работы отказал»" \
    "да" "$(grep -q 'судья работы отказал' "$T/log/warden.out" && echo да || echo нет)"

echo "── А. Б15: главная отвечает на каждый пинок одним «Жду.» и молчит дальше ──"
# Ответ на пинок — реплика, а не работа: без доставки владельцу и без ответов
# дольше KICK_ANSWER_WAIT_MIN он немоту не гасит. Пять обходов — два круга
# «немота 20 мин → пинок → «Жду.» через минуту → молчание».
stand_a
VCLOCK=0; VMAIN="0"
report_ago 0
time_passes 1260
main_answers_scene
run_warden
case_check "Б15 обход 1 (немота 21 мин): пинок, метка stall.kick поставлена" "есть" "$(exists "$T/log/stall.kick")"
time_passes 60; VMAIN="$VMAIN $VCLOCK"
time_passes 480
main_answers_scene
run_warden
case_check "Б15 обход 2 («Жду.» 8 мин назад): владельцу пока ничего" "0" "$(said 'Работа встала')"
time_passes 750
main_answers_scene
run_warden
time_passes 60; VMAIN="$VMAIN $VCLOCK"
time_passes 480
main_answers_scene
run_warden
time_passes 750
main_answers_scene
run_warden
case_check "Б15: за два круга «пинок → «Жду.» → молчание» «Работа встала» ушла" \
    "да" "$([ "$(said 'Работа встала')" -ge 1 ] && echo да || echo нет)"
case_check "Б15: событие agent_stalled есть" "да" "$([ "$(event agent_stalled)" -ge 1 ] && echo да || echo нет)"

echo "── А. Б16: метки пинков не переживают паузу владельца ──"
pulse_scene
case_check "Б16 пульс: пульс-пинок до паузы отдан (условие создано)" "1" "$(event pulse_kick)"
pause_a on
run_warden
case_check "Б16 пульс: на паузе метка pulse.kick удалена" "нет" "$(exists "$T/log/pulse.kick")"
time_passes 10800
pause_a off
scene "$A_DIR" <<EOF
main launch 14000 $H1 $DESC1
main ok 12300
helper $H1 ok 12000
helper $H1 ok 60
EOF
: > "$T/channel.txt"
run_warden
case_check "Б16 пульс: после трёх часов паузы нет вести «не ответила на запрос статуса»" \
    "0" "$(said 'не ответила на запрос статуса')"
stand_a
scene "$A_DIR" <<EOF
main ok 1260
EOF
report_ago 1200
run_warden
case_check "Б16 немота: пинок до паузы, метка stall.kick (условие создано)" "есть" "$(exists "$T/log/stall.kick")"
pause_a on
run_warden
case_check "Б16 немота: на паузе метка stall.kick удалена" "нет" "$(exists "$T/log/stall.kick")"
time_passes 10800
pause_a off
scene "$A_DIR" <<EOF
main ok 12060
EOF
run_warden
case_check "Б16 немота: первый обход после паузы — «Работа встала» без свежего пинка не уходит" \
    "0" "$(said 'Работа встала')"

echo "── А. Б17: пинок немоты не ушёл (кулдаун LIMIT_RETRY_MIN) — метки stall.kick нет ──"
stand_a
printf '%s\n' '{"queue": [], "lines": [{"id": "hr", "tasks": []}]}' > "$T/project/dev-map.yaml"
scene "$A_DIR" <<EOF
main ok 1260
EOF
report_ago 1200
stamp_ago лимит-подписки.пинок 180
run_warden
case_check "Б17: пинок в панель не ушёл (условие создано)" "нет" "$(pane_has '[сторож сессий]')"
case_check "Б17: метка пинка немоты без пинка не ставится" "нет" "$(exists "$T/log/stall.kick")"
stamp_ago лимит-подписки.пинок 660
run_warden
case_check "Б17 страж: кулдаун прошёл — пинок ушёл и метка stall.kick есть" \
    "да есть" "$(pane_has '[сторож сессий]') $(exists "$T/log/stall.kick")"

echo "── А. Б18: очередь вся за блокерами, главная у приглашения, помощник работает — пульс ──"
stand_a
printf '%s\n' '{"queue": ["hr.b"], "lines": [{"id": "hr", "tasks": [{"id": "hr.b", "status": "wip", "blocked": "ждёт слова владельца"}]}]}' \
    > "$T/project/dev-map.yaml"
scene "$A_DIR" <<EOF
main launch 2400 $H1 $DESC1
main ok 1500 Жду.
helper $H1 ok 1200
helper $H1 ok 60
EOF
report_ago 1860
stamp_ago turn-end.stamp 1400
run_warden
case_check "Б18: помощник работает — пульс-пинок отдан (у приглашения не судится)" "1" "$(event pulse_kick)"

# ── стенд Б: дверь канала, судья метки, limit-watch ────────────────────────
B="$T/b"
B_PROJECT="$B/project"
B_DIR="$B/transcripts/${B_PROJECT//\//-}"
mkdir -p "$B_PROJECT/scripts/lib" "$B_PROJECT/harness/demons" "$B/log" "$B/bin" "$B_DIR"
for f in tg_send.sh limit_mark.py limit-in-log.py stall-in-log.py pause.py \
         check-channel-messages.py language-level.py; do
    cp "$HERE/$f" "$B_PROJECT/scripts/"
done
cp "$HERE"/lib/*.py "$HERE/lib/config.sh" "$HERE/lib/pane.sh" "$HERE/lib/events.sh" "$B_PROJECT/scripts/lib/" 2>/dev/null
cp "$WATCHER" "$B_PROJECT/harness/demons/"
{
    printf 'TG_CHAT_ID="100500"\n'                       # не-секрет: выдуманный чат стенда
    printf 'TG_TOKEN_FILE="%s"\n' "$B/key"             # не-секрет: путь, а не значение
    printf 'PROJECT_DIR="%s"\n' "$B_PROJECT"
    printf 'LOG_DIR="%s"\n' "$B/log"
    printf 'TMUX_SESSION="%sb"\n' "$PREFIX"
    printf 'OWNER_TZ=""\n'
    printf 'HELPER_FRESH_MIN=20\n'
} > "$B/install.conf"
python3 -c "import secrets,io,sys; io.open(sys.argv[1],'w').write('11'+secrets.token_hex(16)+'\n')" "$B/key"
# Потолки канала — машинной строкой документа тона, как на живом (1400).
mkdir -p "$B_PROJECT/память"
printf '<!-- потолки: мягкий=700 жёсткий=1400 -->\n' > "$B_PROJECT/память/TONE.md"
cat > "$B/bin/curl" <<'CURL'
#!/usr/bin/env bash
printf 'call\n' >> "$STAND_CURL_CALLS"
printf '%s\n' "$*" >> "$STAND_CURL_ARGS"
if [ "${STAND_CURL_FAIL:-0}" = 1 ]; then
    printf '{"ok":false,"error_code":502,"description":"Bad Gateway"}'
else
    printf '{"ok":true,"result":{"message_id":1}}'
fi
CURL
cat > "$B/bin/tmux" <<'TMUX'
#!/usr/bin/env bash
case "${1:-}" in
    display-message) case "${*}" in *pane_dead*) printf '0\n' ;; *) printf 'claude\n' ;; esac ;;
    has-session)     exit 0 ;;
    send-keys)       printf '%s\n' "$*" >> "$STAND_TMUX_LOG" ;;
esac
TMUX
chmod +x "$B/bin/curl" "$B/bin/tmux"
MARK_B="$B/log/лимит-подписки.состояние"
B_MAIN="$B_DIR/$SID.jsonl"

env_b() {
    env -u CLAUDE_CODE_SESSION_ID -u HARNESS_REPORT PATH="$B/bin:$PATH" \
        STAND_CURL_CALLS="$B/log/curl.calls" STAND_CURL_ARGS="$B/log/curl.args" STAND_TMUX_LOG="$B/log/tmux.log" \
        LOG_DIR="$B/log" PROJECT_DIR="$B_PROJECT" HEARTBEAT_DIR="$B/log/heartbeat" \
        HARNESS_INSTALL_CONF="$B/install.conf" HARNESS_CONF="$B/install.conf" \
        CLAUDE_PROJECTS_DIR="$B/transcripts" HELPER_FRESH_MIN=20 \
        PANE_ENTER_DELAY=0 LIMIT_KICK_DELAY_SEC=0 "$@"
}
stand_b() { rm -rf "$B/log"; mkdir -p "$B/log/heartbeat"; }
curl_calls() { lines_in "$B/log/curl.calls"; }
sent_with() { local n; n=$(grep -c -- "$1" "$B/log/tg_out.jsonl" 2>/dev/null); echo "${n:-0}"; }
args_have() { grep -qF -- "$1" "$B/log/curl.args" 2>/dev/null && echo да || echo нет; }
lift() {  # доводы limit_mark.py --снять …; печатает «простой<TAB>повод<TAB>весть»
    env_b python3 "$B_PROJECT/scripts/limit_mark.py" --снять "$@" 2>/dev/null
}
limit_scene() {
    scene "$B_DIR" <<EOF
main ok 6144
main err 6000
main err 120
main ok 20 Лимит снят.
EOF
}

echo "── Б. Б9: 25.10 01:10:42 — смена пишет при стоящей метке limit ──"
stand_b; limit_scene
printf '%s limit %s\n' "$(( $(date +%s) - 5940 ))" "$(( $(date +%s) - 60 ))" > "$MARK_B"
RC_B9=0
env_b CLAUDE_CODE_SESSION_ID="$SID" bash "$B_PROJECT/scripts/tg_send.sh" \
    'Лимит обновился, работа возобновилась. Сейчас: добиваю мутации диска.' >/dev/null 2>&1 || RC_B9=$?
case_check "Б9: дверь приняла сообщение смены (код 0)" "0" "$RC_B9"
case_check "Б9: транспорт позван ровно один раз" "1" "$(curl_calls)"
case_check "Б9: в одном сообщении весть судьи («простой составил»)" "да" "$(args_have 'простой составил')"
case_check "Б9: и текст смены" "да" "$(args_have 'добиваю мутации диска')"
case_check "Б9: в придержанное не легло ни строки" "0" "$(lines_in "$B/log/придержано-лимитом.jsonl")"
case_check "Б9: метку снял её судья по слову смены" "нет" "$(exists "$MARK_B")"
env_b bash "$B_PROJECT/harness/demons/limit-watch.sh" >/dev/null 2>&1
case_check "Б9: limit-watch следом молчит — «Лимит обновился» в исходящих одно" "1" "$(sent_with 'Лимит обновился')"

echo "── Б. Б9б (страж): демон без признака сессии при стоящей метке — придержано, как прежде ──"
stand_b; limit_scene
printf '%s limit %s\n' "$(( $(date +%s) - 5940 ))" "$(( $(date +%s) + 3600 ))" > "$MARK_B"
env_b bash "$B_PROJECT/scripts/tg_send.sh" 'Утренняя сводка харнеса: живых 25.' >/dev/null 2>&1
case_check "Б9б: транспорт не позван" "0" "$(curl_calls)"
case_check "Б9б: метка стоит" "есть" "$(exists "$MARK_B")"

echo "── Б. Б9в (страж): признак сессии есть, а успешного ответа после метки нет — придержано ──"
# Фоновый прогон, запущенный сменой до лимита, наследует CLAUDE_CODE_SESSION_ID
# и шлёт отчёт посреди лимита: признак вызова сессией не доказывает, что модель
# отвечает. Снимать метку можно только по ответу НОВЕЕ метки.
stand_b
scene "$B_DIR" <<EOF
main ok 6144
main err 6000
main err 120
EOF
printf '%s limit %s\n' "$(( $(date +%s) - 5940 ))" "$(( $(date +%s) + 3600 ))" > "$MARK_B"
env_b CLAUDE_CODE_SESSION_ID="$SID" bash "$B_PROJECT/scripts/tg_send.sh" \
    'Прогон ворот: 160 зелёных, 2 красных.' >/dev/null 2>&1
case_check "Б9в: транспорт не позван" "0" "$(curl_calls)"
case_check "Б9в: метка стоит" "есть" "$(exists "$MARK_B")"

echo "── Б. Б9г: текст смены 1360 знаков при стоящей метке — весть о лимите не теряется ──"
# Склейка «весть + текст» длиннее потолка канала, хотя текст смены один его
# проходит. Весть обязана дойти, а нет — метка обязана стоять, иначе
# «Лимит обновился» не скажет никто.
LONG_TEXT=""
step=0
while [ ${#LONG_TEXT} -lt 1340 ]; do
    step=$((step + 1))
    LONG_TEXT+="Шаг $step: прогон 75/75, мутаций 17, красных 0, осталось 2 шага."$'\n'
done
LONG_TEXT="${LONG_TEXT%$'\n'}"
GUARD_RC=0
printf '%s' "$LONG_TEXT" | PROJECT_DIR="$B_PROJECT" python3 "$B_PROJECT/scripts/check-channel-messages.py" >/dev/null 2>&1 || GUARD_RC=$?
[ "$GUARD_RC" = 0 ] && [ ${#LONG_TEXT} -le 1399 ] \
    || { echo "ПЛОХО СТЕНД: текст смены ${#LONG_TEXT} знаков не проходит сторожа канала сам (код $GUARD_RC) — проверять нечего"; exit 1; }
stand_b; limit_scene
printf '%s limit %s\n' "$(( $(date +%s) - 5940 ))" "$(( $(date +%s) - 60 ))" > "$MARK_B"
env_b CLAUDE_CODE_SESSION_ID="$SID" bash "$B_PROJECT/scripts/tg_send.sh" "$LONG_TEXT" >/dev/null 2>&1
case_check "Б9г: весть «простой составил» доставлена ИЛИ метка стоит" \
    "да" "$( { [ "$(args_have 'простой составил')" = да ] || [ -e "$MARK_B" ]; } && echo да || echo нет)"

echo "── Б. Б9д: доставка слова смены не подтверждена — метка стоит ──"
stand_b; limit_scene
printf '%s limit %s\n' "$(( $(date +%s) - 5940 ))" "$(( $(date +%s) - 60 ))" > "$MARK_B"
STAND_CURL_FAIL=1 env_b CLAUDE_CODE_SESSION_ID="$SID" bash "$B_PROJECT/scripts/tg_send.sh" \
    'Сейчас: добиваю мутации диска, 3 из 5.' >/dev/null 2>&1
case_check "Б9д: транспорт позван (условие создано)" "да" "$([ "$(curl_calls)" -ge 1 ] && echo да || echo нет)"
case_check "Б9д: API отказал — метка лимита не снята" "есть" "$(exists "$MARK_B")"

echo "── Б. Б10: простой от последней работы, а не от возраста метки ──"
stand_b; limit_scene
printf '%s limit 0\n' "$(( $(date +%s) - 5940 ))" > "$MARK_B"
case_check "Б10: последний ответ 102 мин назад, метка 99 — простой 102" \
    "102" "$(lift --транскрипт "$B_MAIN" | cut -f1)"

stand_b
scene "$B_DIR" <<EOF
main launch 7000 $H1 $DESC1
main ok 6000
helper $H1 ok 3900
helper $H1 err 3700
EOF
printf '%s limit 0\n' "$(( $(date +%s) - 3600 ))" > "$MARK_B"
case_check "Б10б: помощник работал после главной — простой от его ответа (65), не 60 и не 100" \
    "65" "$(lift --транскрипт "$B_MAIN" | cut -f1)"

stand_b
scene "$B_DIR" <<EOF
main ok 1940
main ok 10
EOF
printf '%s nemota 0\n' "$(( $(date +%s) - 1800 ))" > "$MARK_B"
VEST_B10=$(lift --транскрипт "$B_MAIN" | cut -f3)
case_check "Б10в: весть немоты называет простой по работе" \
    "да" "$(printf '%s' "$VEST_B10" | grep -qF 'работы не было 32 мин (ни сессии, ни помощников)' && echo да || echo нет)"
case_check "Б10в: «смена просто молчала» больше не пишется" \
    "нет" "$(printf '%s' "$VEST_B10" | grep -qF 'просто молчала' && echo да || echo нет)"

echo "── Б. Б13: limit-watch судит работу тем же судьёй — помощник ожил после сброса ──"
stand_b
scene "$B_DIR" <<EOF
main launch 7200 $H1 $DESC1
main ok 7190
main notify-user 1840 $H1
main err 1700
main send 400 $H1
helper $H1 ok 7000
helper $H1 ok 1900
helper $H1 err 1850
helper $H1 ok 300
helper $H1 ok 60
EOF
printf '%s limit %s\n' "$(( $(date +%s) - 1800 ))" "$(( $(date +%s) - 600 ))" > "$MARK_B"
env_b bash "$B_PROJECT/harness/demons/limit-watch.sh" >/dev/null 2>&1
case_check "Б13: главная 6 мин, помощник 1 мин — limit-watch снял метку" "нет" "$(exists "$MARK_B")"
case_check "Б13: и сказал «Лимит обновился» ровно один раз" "1" "$(sent_with 'Лимит обновился')"

echo "── Б. Б13б: судья работы падает трассировкой (код 1) — limit-watch судит по главной ──"
# Сбой прибора — стендовая обёртка: --работа падает, прочие вызовы и импорт
# идут в настоящий судью рядом.
mv "$B_PROJECT/scripts/stall-in-log.py" "$B_PROJECT/scripts/stall-in-log.real.py"
cat > "$B_PROJECT/scripts/stall-in-log.py" <<'PY'
import sys
if __name__ == "__main__" and "--работа" in sys.argv:
    raise RuntimeError("стенд: судья работы упал")
_real = __file__[:-len("stall-in-log.py")] + "stall-in-log.real.py"
exec(compile(open(_real, encoding="utf-8").read(), _real, "exec"))
PY
stand_b
scene "$B_DIR" <<EOF
main ok 7190
main err 1700
main ok 60 Лимит снят.
EOF
printf '%s limit %s\n' "$(( $(date +%s) - 1800 ))" "$(( $(date +%s) - 600 ))" > "$MARK_B"
env_b bash "$B_PROJECT/harness/demons/limit-watch.sh" >/dev/null 2>&1
case_check "Б13б: судья упал (условие создано)" \
    "да" "$(grep -q 'RuntimeError' "$B/log/limit-watch.judge.err" 2>/dev/null && echo да || echo нет)"
case_check "Б13б: главная ответила 1 мин назад — метка снята" "нет" "$(exists "$MARK_B")"
case_check "Б13б: «Лимит обновился» ровно один раз" "1" "$(sent_with 'Лимит обновился')"
mv "$B_PROJECT/scripts/stall-in-log.real.py" "$B_PROJECT/scripts/stall-in-log.py"

passed=$((CASES_PATHS - CASES_FAILED))
if [ "$CASES_FAILED" -eq 0 ]; then
    echo "итог: $passed/$CASES_PATHS — работа помощников судится как работа"
    exit 0
fi
echo "итог: $passed/$CASES_PATHS — КРАСНЫЙ ($CASES_FAILED проб)"
exit 1
