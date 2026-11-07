#!/usr/bin/env bash
# Проба: пульс сторожа сессий слышен владельцу, даже когда смена молчит
# (карточка hr.pulse-never-fired).
#
# Больной случай 02.11.2026 (замер по логам сторожа и tg_out.jsonl):
#   А. Метка лимита от 31.10 11:55 (недельный лимит одной модели, сброс 04.11
#      18:00) держала LIMIT_ACTIVE=1 весь день, хотя смена после метки отвечала:
#      судья limit-in-log.py --действует --метка считал метку уликой, пока срок
#      впереди, и не смотрел на ответы новее неё. Ветка тишины и пинок
#      пробуждения заглушены 9 и 8 раз; смена стояла у приглашения
#      09:13:56→10:58:35 (105 мин) при работе в очереди.
#   Б. Пульс 6в молчит, когда главная простаивает (работа 36 ≥ SILENCE_MAX_MIN
#      10): обход 09:50 — pulse_verdict 36 36 0 → «молчу», владельцу ни слова.
#   В. Порог 30 мин при обходе раз в 10 мин: провал 11:01:19→11:38:51 (37,5 мин)
#      не замечен — обход 11:30 видел 28 мин.
#   Г. Вердикт «молчу» в журнал не пишется вовсе — решение невидимо.
#
# Интерфейс, который проба требует от починки:
#   * судья limit-in-log.py --действует <тр> --метка: содержательный ответ
#     смены НОВЕЕ метки (или отказа) снимает лимит — «0», код 1. Не снимают:
#     запись с текстом лимита (её узнаёт лимит_в_тексте) и отказ новее ответа.
#   * ключ WARDEN_PERIOD_MIN в harness.conf — шаг обхода сторожа, равен шагу
#     cron задания session-warden в harness/config/system-jobs.yaml. Пульс
#     действует на обходе, если REPORT_AGE_MIN + WARDEN_PERIOD_MIN > PULSE_MAX_MIN
#     (на следующем обходе было бы поздно).
#   * статус сторожа владельцу (tg_send, одно сообщение): работа есть (wip в
#     карте или очередь), отчёта нет ≥ порога, главная простаивает и пинок пульса
#     на этом обходе не уходит. Текст начинается «Статус от сторожа:» и несёт:
#     id карточек wip, «Помощников: N», последний шаг (последняя непустая
#     строка .tasks/<id>/журнал.md) и почему смена молчит («простаивает N мин»
#     или фраза limit_until_text). Метка — $LOG_DIR/pulse.auto (last_report не
#     трогается), повтор не раньше PULSE_MAX_MIN. Строка журнала «пульс: статус
#     владельцу …».
#   * лимит ДОКАЗАН (отказ новее ответа, срок впереди) — статуса нет: правило
#     владельца 17.10 «одно сообщение о лимите, дальше тишина до сброса».
#   * каждый обход, где пульс промолчал, — ровно одна строка «пульс: молчу — <почему>».
#
# Стенд свой: tmux-сессия warden-pulse-<pid> (копия cat под именем claude
# пишет присланное в файл), свой LOG_DIR, канал-файл вместо tg_send.sh.
# Имена переменных латиницей (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Qostanay

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"
# В проекте демоны под harness/demons, конфиг под harness/config; в пакете
# пробы лежат в harness/scripts, и те же каталоги — соседи.
DAEMON="$HERE/../harness/demons/session-warden.sh"
[ -f "$DAEMON" ] || DAEMON="$HERE/../demons/session-warden.sh"
WATCHER="$HERE/../harness/demons/limit-watch.sh"
[ -f "$WATCHER" ] || WATCHER="$HERE/../demons/limit-watch.sh"
CONF_DIR="$HERE/../harness/config"
[ -d "$CONF_DIR" ] || CONF_DIR="$HERE/../config"
JUDGE="$HERE/limit-in-log.py"
for f in "$DAEMON" "$WATCHER" "$JUDGE" "$CONF_DIR/harness.conf" "$CONF_DIR/system-jobs.yaml"; do
    [ -s "$f" ] || { echo "ПЛОХО: нет файла по пути раскладки: $f — проверять нечего"; exit 1; }
done

PREFIX="warden-pulse-"
sweep_panes() {
    local name
    while IFS= read -r name; do
        case "$name" in "$PREFIX"*) tmux kill-session -t "$name" 2>/dev/null ;; esac
    done < <(tmux ls -F '#{session_name}' 2>/dev/null)
}
sweep_panes
T=$(mktemp -d -t warden-pulse.XXXXXX)
PANE="${PREFIX}$$"
trap 'sweep_panes; rm -rf "$T"' EXIT

SID="7c3e1f20-5b4a-4d8e-9f61-2a0b3c4d5e6f"
H1="a1511aa2c0ffee001"
STEP="- Запущен #2: тесты до кода, жду."
virt() { date -d "$1 +05" +%s; }   # миг дня инцидента → unix
NOW_V=$(virt "2026-11-02 09:50:06")
# Метка 31.10 11:55, сброс 04.11 18:00 — переложены на часы стенда той же дельтой.
MARK_AGO=$(( NOW_V - $(virt "2026-10-31 11:55:00") ))
RESET_AHEAD=$(( $(virt "2026-11-04 18:00:00") - NOW_V ))
REFUSAL_AGO=$(( NOW_V - $(virt "2026-10-31 11:54:16") ))
ago() { echo $(( NOW_V - $(virt "2026-11-02 $1") )); }   # секунд от мига до обхода 09:50:06

# ── генератор транскриптов стенда ───────────────────────────────────────────
# Строки сцены (секунды — «сколько назад»):
#   answer <сек> [текст]          — содержательный ответ модели;
#   limittext <сек> <модель>      — запись с текстом лимита («You've hit your weekly limit»);
#   refusal <сек> <resetsAt>      — отказ CLI: 429, isApiErrorMessage, quotaLimits;
#   launch <сек> <id>             — запуск помощника (Agent + async_launched);
#   helper <id> <сек>             — ответ помощника в subagents/agent-<id>.jsonl.
# Формы сняты с живого транскрипта 31.10 06:54Z (отказ недельного лимита) и
# стенда test_warden_subagent_work.sh (запуск помощника).
cat > "$T/scene.py" <<'PY'
import datetime, json, os, sys, time, uuid

main_path, sid = sys.argv[1], sys.argv[2]
now = time.time()
main, helpers = [], {}


def stamp(ago):
    moment = datetime.datetime.fromtimestamp(now - ago, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def base(kind, ago, **extra):
    record = {"parentUuid": None, "isSidechain": False, "type": kind, "uuid": str(uuid.uuid4()),
              "timestamp": stamp(ago), "sessionId": sid, "userType": "external",
              "entrypoint": "cli", "cwd": "/stand", "version": "2.1.299"}
    record.update(extra)
    return record


def answer(ago, content, model="claude-sonnet-4-5", **extra):
    return base("assistant", ago, message={"model": model, "type": "message", "role": "assistant",
                                           "content": content, "stop_reason": "end_turn"}, **extra)


LIMIT_TEXT = "You've hit your weekly limit · resets Nov 4, 6pm (Asia/Qostanay)"
for raw in sys.stdin.read().splitlines():
    parts = raw.split(maxsplit=2)
    if not parts:
        continue
    kind, ago = parts[0], int(parts[1]) if parts[0] != "helper" else 0
    if kind == "answer":
        text = parts[2] if len(parts) > 2 else "Сделано: пробы написаны, жду ворот."
        main.append((ago, answer(ago, [{"type": "text", "text": text}])))
    elif kind == "limittext":
        main.append((ago, answer(ago, [{"type": "text", "text": LIMIT_TEXT}], model=parts[2])))
    elif kind == "refusal":
        main.append((ago, base("assistant", ago, error="rate_limit", isApiErrorMessage=True,
                               apiErrorStatus=429,
                               quotaLimits={"status": "rejected", "resetsAt": int(parts[2]),
                                            "rateLimitType": "seven_day"},
                               message={"model": "<synthetic>", "role": "assistant",
                                        "content": [{"type": "text", "text": LIMIT_TEXT}]})))
    elif kind == "launch":
        agent_id = parts[2]
        call = {"type": "tool_use", "id": "toolu_stand_" + agent_id, "name": "Agent",
                "input": {"description": "Пробы пульса", "prompt": "задание стенда",
                          "subagent_type": "general-purpose"}}
        result = {"isAsync": True, "status": "async_launched", "agentId": agent_id,
                  "description": "Пробы пульса", "outputFile": "/tmp/stand/" + agent_id + ".output"}
        main.append((ago, answer(ago, [call])))
        main.append((ago, base("user", ago, message={"role": "user", "content": [
            {"tool_use_id": "toolu_stand_" + agent_id, "type": "tool_result",
             "content": [{"type": "text", "text": "Async agent launched successfully."}]}]},
            toolUseResult=result)))
    elif kind == "helper":
        agent_id, ago = parts[1], int(parts[2])
        helpers.setdefault(agent_id, []).append(ago)
    else:
        raise SystemExit("неизвестный вид записи сцены: " + kind)

main.sort(key=lambda item: -item[0])
os.makedirs(os.path.dirname(main_path), exist_ok=True)
with open(main_path, "w", encoding="utf-8") as stream:
    for _, record in main:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
newest = min((ago for ago, _ in main), default=0)
os.utime(main_path, (now - newest, now - newest))
for agent_id, moments in helpers.items():
    path = os.path.join(main_path[:-len(".jsonl")], "subagents", "agent-" + agent_id + ".jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tag = {"agentId": agent_id, "isSidechain": True}
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(base("user", max(moments) + 60, message={"role": "user", "content": "задание"}, **tag),
                                ensure_ascii=False) + "\n")
        for ago in sorted(moments, reverse=True):
            stream.write(json.dumps(answer(ago, [{"type": "text", "text": "Шаг сделан."}], **tag),
                                    ensure_ascii=False) + "\n")
    os.utime(path, (now - min(moments), now - min(moments)))
    with open(path[:-len(".jsonl")] + ".meta.json", "w", encoding="utf-8") as stream:
        json.dump({"agentType": "general-purpose", "description": "Пробы пульса",
                   "spawnDepth": 1, "requestShape": "background", "model": "sonnet"}, stream, ensure_ascii=False)
PY
scene() {  # $1 = путь главного транскрипта; сцена — на stdin
    rm -rf "${1%.jsonl}" "$1"
    python3 "$T/scene.py" "$1" "$SID" || { echo "ПЛОХО: генератор сцены упал — проверять нечего"; exit 1; }
}
put_mark() {  # $1 = каталог журнала: метка 31.10 со сбросом 04.11, как на бою
    mkdir -p "$1"
    printf '%s limit %s\n' "$(( $(date +%s) - MARK_AGO ))" "$(( $(date +%s) + RESET_AHEAD ))" \
        > "$1/лимит-подписки.состояние"
}

# ── А1. судья: ответ смены новее метки снимает лимит ────────────────────────
echo "── А1. судья limit-in-log.py --действует --метка ──"
mkdir -p "$T/a1/log"
printf 'LOG_DIR="%s"\n' "$T/a1/log" > "$T/a1/harness.conf"
A1_TR="$T/a1/tr/$SID.jsonl"
judge_mark() {  # → первое поле ответа судьи и код через пробел
    local out rc=0
    out=$(env LOG_DIR="$T/a1/log" HARNESS_CONF="$T/a1/harness.conf" HARNESS_INSTALL_CONF="$T/a1/harness.conf" \
        python3 "$JUDGE" --действует "$A1_TR" --метка 2>/dev/null) || rc=$?
    printf '%s %s\n' "$(printf '%s' "$out" | head -1 | cut -f1)" "$rc"
}
put_mark "$T/a1/log"
mkdir -p "$(dirname "$A1_TR")"; : > "$A1_TR"
case_check "А1 условие: метка стоит, сброс впереди, транскрипт пуст — лимит держится" \
    "1 0" "$(judge_mark)"

printf 'answer %s\n' "$(ago 09:13:56)" | scene "$A1_TR"
case_check "А1.1 БОЛЬНОЙ СЛУЧАЙ 02.11: метка 31.10, смена ответила в 09:13:56 — лимит не держится" \
    "0 1" "$(judge_mark)"

printf 'refusal %s %s\nanswer %s\n' "$REFUSAL_AGO" "$(( $(date +%s) + RESET_AHEAD ))" "$(ago 09:13:56)" \
    | scene "$A1_TR"
case_check "А1.2: отказ недельного лимита 31.10 со сроком 04.11, ответ новее — лимит не держится" \
    "0 1" "$(judge_mark)"

printf 'limittext %s <synthetic>\n' "$(ago 09:13:56)" | scene "$A1_TR"
case_check "А1.3 контроль: после метки только запись CLI с текстом лимита — держится" \
    "1 0" "$(judge_mark)"

printf 'limittext %s claude-sonnet-4-5\n' "$(ago 09:13:56)" | scene "$A1_TR"
case_check "А1.4 контроль: «ответ» новее метки — сам текст лимита («You've hit your weekly limit») — держится" \
    "1 0" "$(judge_mark)"

printf 'answer %s\nrefusal %s %s\n' "$(ago 09:13:56)" "$(ago 09:40:00)" "$(( $(date +%s) + RESET_AHEAD ))" \
    | scene "$A1_TR"
case_check "А1.5 контроль: отказ 429 новее ответа — держится" "1 0" "$(judge_mark)"

printf 'answer %s\n' "$(( MARK_AGO + 600 ))" | scene "$A1_TR"
case_check "А1.6 контроль: ответ старше метки — держится" "1 0" "$(judge_mark)"

# ── А2б. limit-watch: тот же судья снимает ложную метку ─────────────────────
echo "── А2б. limit-watch: метка 31.10, смена ответила минуту назад — метка снята ──"
W_PROJECT="$T/w/proekt"
W_TR="$T/w/transkripty/${W_PROJECT//\//-}/$SID.jsonl"
mkdir -p "$W_PROJECT/scripts/lib" "$T/w/zhurnal/heartbeat"
cp "$HERE/limit-in-log.py" "$HERE/stall-in-log.py" "$HERE/limit_mark.py" "$W_PROJECT/scripts/"
cp "$HERE/lib/config.sh" "$HERE/lib/config.py" "$W_PROJECT/scripts/lib/"
cat > "$W_PROJECT/scripts/tg_send.sh" <<'KANAL'
#!/usr/bin/env bash
printf '%s\n' "${@: -1}" | tr '\n' ' ' >> "$LOG_DIR/kanal.txt"; printf '\n' >> "$LOG_DIR/kanal.txt"
KANAL
chmod +x "$W_PROJECT/scripts/tg_send.sh"
put_mark "$T/w/zhurnal"
printf 'answer 60\n' | scene "$W_TR"
env LOG_DIR="$T/w/zhurnal" PROJECT_DIR="$W_PROJECT" HEARTBEAT_DIR="$T/w/zhurnal/heartbeat" \
    CLAUDE_PROJECTS_DIR="$T/w/transkripty" \
    HARNESS_INSTALL_CONF="$T/w/net.conf" HARNESS_CONF="$T/w/net.conf" \
    bash "$WATCHER" > "$T/w/watch.out" 2>&1 < /dev/null
case_check "А2б: смена отвечает после метки — limit-watch снял метку (возобновление не заглушено)" \
    "нет" "$([ -f "$T/w/zhurnal/лимит-подписки.состояние" ] && echo да || echo нет)"

# ── стенд сторожа: своя панель, свой LOG_DIR, канал-файл ───────────────────
cp /bin/cat "$T/claude"
mkdir -p "$T/project/scripts/lib" "$T/projects"
for f in work-queue.py limit_mark.py limit-in-log.py stall-in-log.py; do
    cp "$HERE/$f" "$T/project/scripts/"
done
cp "$HERE"/lib/*.py "$HERE/lib/config.sh" "$T/project/scripts/lib/"
# Канал: текст — последний довод (ключи двери идут перед ним), ключи — в свой файл.
cat > "$T/project/scripts/tg_send.sh" <<CHANNEL
#!/usr/bin/env bash
printf '%s\n' "\${@: -1}" | tr '\n' ' ' >> "$T/channel.txt"
printf '\n' >> "$T/channel.txt"
CHANNEL
chmod +x "$T/project/scripts/tg_send.sh"
A_DIR="$T/projects/${T//\//-}-project"
TR="$A_DIR/$SID.jsonl"
cp "$CONF_DIR/harness.conf" "$T/harness.conf"
cat >> "$T/harness.conf" <<CONF
AGENT_USER="$(id -un)"
LOG_DIR="$T/log"
PROJECT_DIR="$T/project"
STATE_DIR="$T/state"
TMUX_SESSION="$PANE"
AGENT_START_CMD="$T/claude"
SERVICE_LOCKS_SUBDIR="проверка"
ACTIVE_LINE="hr"
CTX_WINDOW_TOKENS=1000000  # не-секрет: размер окна модели в токенах
SILENCE_MAX_MIN=10
PULSE_MAX_MIN=30
PULSE_OWNER_MIN=60
WARDEN_PERIOD_MIN=10
LIMIT_STALL_MIN=20
HELPER_FRESH_MIN=20
KICK_ANSWER_WAIT_MIN=5
TG_CHAT_ID=""
TG_TOKEN_FILE="/nonexistent"
CONF

stand() {  # $1 = «работа» | «пусто»: свежий LOG_DIR, карта, журнал карточки, панель
    rm -rf "$T/log" "$T/state" "$T/project/.tasks"
    mkdir -p "$T/log/inbox/обработано" "$T/state/panel-state" "$A_DIR" "$T/project/.tasks/hr.b"
    : > "$T/channel.txt"; : > "$T/pane.txt"
    if [ "$1" = работа ]; then
        printf '%s\n' '{"queue": [], "lines": [{"id": "hr", "tasks": [{"id": "hr.b", "status": "wip"}]}]}' \
            > "$T/project/dev-map.yaml"
    else
        printf '%s\n' '{"queue": [], "lines": [{"id": "hr", "tasks": []}]}' > "$T/project/dev-map.yaml"
    fi
    printf '# hr.b — журнал\n\n- Замер готов.\n%s\n\n' "$STEP" > "$T/project/.tasks/hr.b/журнал.md"
    tmux kill-session -t "$PANE" 2>/dev/null
    tmux new-session -d -x 200 -y 60 -s "$PANE" "exec $T/claude >> $T/pane.txt"
    sleep 0.5
    [ "$(tmux display-message -p -t "$PANE" '#{pane_current_command}')" = claude ] \
        || { echo "ПЛОХО: подставной агент не встал в панель — проверять нечего"; exit 1; }
}
run_warden() {
    env -u TMUX -u CLAUDE_CODE_SESSION_ID -u HARNESS_REPORT \
        HARNESS_CONF="$T/harness.conf" HARNESS_INSTALL_CONF="$T/harness.conf" \
        HARNESS_ACTIVE_LINE=hr PANE_ENTER_DELAY=0.2 \
        HARNESS_PANEL_STATE="$T/state/panel-state" CLAUDE_PROJECTS_DIR="$T/projects" \
        bash "$DAEMON" >> "$T/log/warden.out" 2>&1 < /dev/null
    sleep 0.3
}
report_ago() {  # $1 = секунд назад последний отчёт смены
    echo $(( $(date +%s) - $1 )) > "$T/log/last_report"
    touch -d "@$(( $(date +%s) - $1 ))" "$T/log/last_report"
}
pane_has() { grep -qF -- "$1" "$T/pane.txt" 2>/dev/null && echo да || echo нет; }
statuses() { local n; n=$(grep -c 'Статус от сторожа' "$T/channel.txt" 2>/dev/null); echo "${n:-0}"; }
status_has() { grep 'Статус от сторожа' "$T/channel.txt" 2>/dev/null | grep -qF -- "$1" && echo да || echo нет; }
silent_lines() { local n; n=$(grep -c 'пульс: молчу — ' "$T/log/warden.out" 2>/dev/null); echo "${n:-0}"; }
silent_has() { grep 'пульс: молчу — ' "$T/log/warden.out" 2>/dev/null | grep -qF -- "$1" && echo да || echo нет; }
show_pulse() {  # причина красного — из журнала сторожа, а не догадка
    grep -E 'пульс|тишина|лимит' "$T/log/warden.out" 2>/dev/null | tail -4 | sed 's/^/      сторож: /'
}

# ── Б1 + А2. 09:50:06: метка 31.10, главная у приглашения 36 мин, отчёта 36 мин ─
echo "── Б1. обход 09:50 (pulse_verdict 36 36 0): статус владельцу от самого сторожа ──"
stand работа
put_mark "$T/log"
printf 'answer %s\n' "$(ago 09:13:56)" | scene "$TR"
report_ago "$(ago 09:13:40)"
REPORT_MTIME=$(stat -c %Y "$T/log/last_report")
run_warden
show_pulse
case_check "Б1.1 БОЛЬНОЙ СЛУЧАЙ 02.11: владельцу ушёл ровно один статус сторожа" "1" "$(statuses)"
case_check "Б1.2: в статусе карточка в работе hr.b" "да" "$(status_has 'hr.b')"
case_check "Б1.3: в статусе число помощников («Помощников: 0»)" "да" "$(status_has 'Помощников: 0')"
case_check "Б1.4: в статусе последний шаг из журнала карточки" "да" "$(status_has 'тесты до кода, жду.')"
case_check "Б1.5: в статусе причина молчания смены («простаивает 36 мин»)" "да" "$(status_has 'простаивает 36 мин')"
case_check "Б1.6: строка журнала «пульс: статус владельцу»" "да" \
    "$(grep -q 'пульс: статус владельцу' "$T/log/warden.out" && echo да || echo нет)"
case_check "Б1.7: своя метка pulse.auto поставлена" "да" "$([ -f "$T/log/pulse.auto" ] && echo да || echo нет)"
case_check "Б1.8: last_report не тронут (статус сторожа — не отчёт смены)" "$REPORT_MTIME" \
    "$(stat -c %Y "$T/log/last_report")"
case_check "А2а: метка лимита ложная — ветка тишины будит смену («Работы не видно 36»)" "да" \
    "$(pane_has 'Работы не видно 36')"
case_check "А2а: «лимит до … — молчу» в журнале нет" "нет" \
    "$(grep -q 'лимит до .* — молчу' "$T/log/warden.out" && echo да || echo нет)"

echo "── Б1к. следующий обход сразу: статус не повторяется ──"
run_warden
case_check "Б1.9: в кулдауне PULSE_MAX_MIN второго статуса нет" "1" "$(statuses)"
case_check "Г1: обход в кулдауне — ровно одна строка «пульс: молчу — »" "1" "$(silent_lines)"

echo "── Б1п. pulse.auto старше PULSE_MAX_MIN, работа осталась: статус снова ──"
OLD=$(( $(date +%s) - 31 * 60 ))
echo "$OLD" > "$T/log/pulse.auto"; touch -d "@$OLD" "$T/log/pulse.auto"
run_warden
case_check "Б1.10: через PULSE_MAX_MIN — второй статус" "2" "$(statuses)"

echo "── Б1н. работы нет (карта пуста, помощников нет) — статуса нет ──"
stand пусто
printf 'answer %s\n' "$(ago 09:13:56)" | scene "$TR"
report_ago "$(ago 09:13:40)"
run_warden
case_check "Б1.11 контроль: работы нет — статуса нет" "0" "$(statuses)"
case_check "Г2: работы нет — одна строка «пульс: молчу — », причина «работы нет»" "1 да" \
    "$(silent_lines) $(silent_has 'работы нет')"

echo "── Б1л. лимит ДОКАЗАН (отказ новее ответа, срок впереди) — статуса нет ──"
stand работа
L_RESET=$(( $(date +%s) + 7200 ))
printf 'answer %s\nrefusal %s %s\n' 2400 1800 "$L_RESET" | scene "$TR"
report_ago 2400
run_warden
show_pulse
case_check "Б1.12: доказанный лимит — статуса сторожа нет (правило владельца 17.10)" "0" "$(statuses)"
case_check "Г3: одна строка «пульс: молчу — » с «лимит до $(date -d "@$L_RESET" +%H:%M)»" "1 да" \
    "$(silent_lines) $(silent_has "лимит до $(date -d "@$L_RESET" +%H:%M)")"

# ── Б2. прежний пинок пульса цел ────────────────────────────────────────────
echo "── Б2а. главная работает (ответ 3 мин), отчёта 31 мин — пинок пульса как прежде ──"
stand работа
printf 'answer 180\n' | scene "$TR"
report_ago $(( 31 * 60 + 20 ))
run_warden
case_check "Б2.1: в панель «Владельцу нет отчёта 31 минут, а работа идёт»" "да" \
    "$(pane_has 'Владельцу нет отчёта 31 минут, а работа идёт')"
case_check "Б2.2: смену спросили — статуса сторожа нет" "0" "$(statuses)"

echo "── Б2б. главная у приглашения 36 мин, помощник ответил 1 мин назад — пинок с помощниками ──"
stand работа
printf 'launch %s %s\nanswer %s\nhelper %s 60\n' 2400 "$H1" 2160 "$H1" | scene "$TR"
report_ago 2180
run_warden
case_check "Б2.3: в панель «Работают помощники:»" "да" "$(pane_has 'Работают помощники:')"
case_check "Б2.4: хвост пинка с помощниками прежний" "да" \
    "$(pane_has 'Новых помощников ради отчёта не запускай.')"

# ── В. порог с учётом шага обхода ───────────────────────────────────────────
echo "── В0. данные: шаг обхода — ключ harness.conf, равный шагу cron ──"
CRON_STEP=$(python3 - "$CONF_DIR/system-jobs.yaml" <<'PY'
import re, sys, yaml
jobs = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["задания"]
spec = next(j["расписание"] for j in jobs if j["имя"] == "session-warden")
m = re.fullmatch(r"\*/(\d+) \* \* \* \*", spec)
print(m.group(1) if m else "не разобран: " + spec)
PY
)
CONF_STEP=$(sed -n 's/^WARDEN_PERIOD_MIN=\([0-9]*\).*/\1/p' "$CONF_DIR/harness.conf" | tail -1)
case_check "В0: WARDEN_PERIOD_MIN в harness.conf = шаг cron session-warden ($CRON_STEP)" \
    "$CRON_STEP" "${CONF_STEP:-ключа нет}"

echo "── В1. обход 11:30: отчёт 11:01:19 (28 мин), главная работает — пинок сейчас ──"
stand работа
printf 'answer %s\n' 120 | scene "$TR"
report_ago $(( $(virt "2026-11-02 11:30:06") - $(virt "2026-11-02 11:01:19") ))
run_warden
show_pulse
case_check "В1.1 БОЛЬНОЙ СЛУЧАЙ 02.11: 28 + шаг 10 > 30 — «Владельцу нет отчёта 28» на этом обходе" "да" \
    "$(pane_has 'Владельцу нет отчёта 28')"

echo "── В1.2. тот же обход, WARDEN_PERIOD_MIN=1 — шаг берётся из данных ──"
stand работа
printf 'answer %s\n' 120 | scene "$TR"
report_ago $(( 28 * 60 + 47 ))
cp "$T/harness.conf" "$T/harness.conf.base"
printf 'WARDEN_PERIOD_MIN=1\n' >> "$T/harness.conf"
run_warden
mv "$T/harness.conf.base" "$T/harness.conf"
case_check "В1.2 контроль: шаг 1 мин — 28 мин ещё рано, пинка нет" "нет" "$(pane_has 'Владельцу нет отчёта')"

echo "── В1.3. главная простаивает, отчёт 28 мин — статус сторожа на этом обходе ──"
stand работа
printf 'answer %s\n' "$(ago 09:13:56)" | scene "$TR"
report_ago $(( 28 * 60 + 47 ))
run_warden
case_check "В1.3: 28 + шаг 10 > 30 — статус сторожа ушёл" "1" "$(statuses)"

echo "── В2. отчёт 15 мин, главная работает — тишина, и она записана ──"
stand работа
printf 'answer %s\n' 120 | scene "$TR"
report_ago $(( 15 * 60 + 20 ))
run_warden
case_check "В2.1: пинка нет" "нет" "$(pane_has 'Владельцу нет отчёта')"
case_check "Г4: одна строка «пульс: молчу — » с возрастом отчёта «15 мин»" "1 да" \
    "$(silent_lines) $(silent_has '15 мин')"

echo "── В3. граница: отчёт 20 мин — 20 + 10 = 30, следующий обход успеет ──"
stand работа
printf 'answer %s\n' 120 | scene "$TR"
report_ago $(( 20 * 60 + 20 ))
run_warden
case_check "В3: 20 мин — пинка нет" "нет" "$(pane_has 'Владельцу нет отчёта')"

# ── Г. пауза владельца: пульс молчит и говорит почему ───────────────────────
echo "── Г5. пауза владельца ──"
stand работа
printf 'answer %s\n' "$(ago 09:13:56)" | scene "$TR"
report_ago "$(ago 09:13:40)"
HARNESS_PANEL_STATE="$T/state/panel-state" python3 "$HERE/pause.py" --поставить "проба пульса" --кто владелец \
    >/dev/null 2>&1 || { echo "ПЛОХО: pause.py не поставил паузу на стенде — проверять нечего"; exit 1; }
run_warden
case_check "Г5: пауза — статуса нет" "0" "$(statuses)"
case_check "Г5: пауза — одна строка «пульс: молчу — » с «пауза»" "1 да" \
    "$(silent_lines) $(silent_has 'пауза')"

passed=$((CASES_PATHS - CASES_FAILED))
if [ "$CASES_FAILED" -eq 0 ]; then
    echo "итог: $passed/$CASES_PATHS — пульс слышен владельцу, молчание записано"
    exit 0
fi
echo "итог: $passed/$CASES_PATHS — КРАСНЫЙ ($CASES_FAILED проб)"
exit 1
