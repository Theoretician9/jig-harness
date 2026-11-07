#!/usr/bin/env bash
# Проба: пока лимит подписки держится, сторож сессий в панель не пишет
# (карточка hr.limit-nudge).
#
# Больной случай 30.10.2026, 16:08–17:30 (Asia/Qostanay): лимит виден на экране
# («Usage limit reached · continuing automatically at 5:30pm») и доказан отказом
# 429 с resetsAt, а сторож каждые 10 минут слал «Работы не видно N минут» (ветка
# 6), «Если лимит подписки обновился…» за 10 минут до сброса (ветка 6б) и на
# другой установке — «Владельцу нет отчёта» (пульс 6в). Каждый пинок стоил
# смене нового отказа 429.
#
# Образцы ЗАПИСАНЫ, а не выдуманы (scripts/fixtures/limit-nudge/):
#   session-tail-2026-10-30.jsonl — хвост живого транскрипта главной 11:08–12:30Z
#     (заметка usageLimitNote wrap_up, system/informational «continuing
#     automatically at 5:30pm», отказы 429 с resetsAt=1793363400, пинки сторожа);
#   helper-refusal-2026-10-30.jsonl — отказ 429 помощника (Agent failed);
#   screen-2026-10-30.txt — экран с фото владельца 30.10 17:25, дословно.
# Стенд транскрипта перекладывает записи на свои часы: «сейчас» стенда — это
# виртуальный миг того дня, все метки времени и resetsAt сдвинуты на одну дельту.
#
# Будущий судья (интерфейс, который проба требует):
#   python3 scripts/limit-in-log.py --действует <транскрипт> [--экран <файл>] [--сейчас <unix>]
#   → строка «1<TAB><сброс unix><TAB><подъём unix>» и код 0, пока лимит держится;
#     «0» и код 1, когда нет. Подъём = сброс + LIMIT_KICK_DELAY_SEC (10 с).
#     Срок неизвестен — поля сброса и подъёма пусты, а не выдуманы.
#   В стороже — обёртка limit_active, одна на обход; её спрашивают ветки 6, 6в, 6б.
#
# Случаи: Ф — судья, Д — экран, А — ветка 6, Б — пульс 6в, В+Г — весь лимит
# обходами по 10 минут. У каждого запрета есть контроль: то же условие без
# лимита обязано дать пинок, иначе «чинка удалением ветки» была бы зелёной.
# Имена переменных латиницей (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Qostanay

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"
DAEMON="$HERE/../harness/demons/session-warden.sh"
# В пакете демоны лежат в harness/demons, пробы — в harness/scripts.
[ -f "$DAEMON" ] || DAEMON="$HERE/../demons/session-warden.sh"
JUDGE="$HERE/limit-in-log.py"
FIX="$HERE/fixtures/limit-nudge"
TAIL="$FIX/session-tail-2026-10-30.jsonl"
SCREEN="$FIX/screen-2026-10-30.txt"
for f in "$DAEMON" "$JUDGE" "$TAIL" "$SCREEN"; do
    [ -s "$f" ] || { echo "ПЛОХО: нет файла по пути раскладки: $f — проверять нечего"; exit 1; }
done
RESET=1793363400            # resetsAt из записи отказа образца = 17:30 +05
WAKE=$((RESET + 10))
FSID=$(python3 -c 'import json,sys; print(json.loads(open(sys.argv[1]).readline())["sessionId"])' "$TAIL")
virt() { date -d "2026-10-30 $1 +05" +%s; }   # миг того дня → unix

# Образец обязан быть тем, за что выдаётся: пустой или подменённый хвост сделал
# бы все запреты ниже зелёными.
N429=$(grep -c '"apiErrorStatus":429' "$TAIL")
NNOTE=$(grep -c 'continuing automatically at 5:30pm' "$TAIL")
case_check "образец: отказов 429 с resetsAt в хвосте ≥ 1 (их $N429)" "да" "$([ "$N429" -ge 1 ] && echo да || echo нет)"
case_check "образец: заметка «continuing automatically» в хвосте ≥ 1 (их $NNOTE)" "да" "$([ "$NNOTE" -ge 1 ] && echo да || echo нет)"

PREFIX="warden-limsil-"
sweep_panes() {
    local name
    while IFS= read -r name; do
        case "$name" in "$PREFIX"*) tmux kill-session -t "$name" 2>/dev/null ;; esac
    done < <(tmux ls -F '#{session_name}' 2>/dev/null)
}
sweep_panes
T=$(mktemp -d -t warden-limsil.XXXXXX)
PANE="${PREFIX}$$"
trap 'sweep_panes; rm -rf "$T"' EXIT

# ── перекладка образца на часы стенда ───────────────────────────────────────
# retime.py <образец> <выход> <виртуальное сейчас> [без-признаков]
# Берёт записи не новее виртуального мига, сдвигает timestamp и resetsAt на
# дельту «настоящее − виртуальное». «без-признаков» выбрасывает все машинные
# признаки лимита (429, заметки usageLimitNote, system/informational) — контроль.
cat > "$T/retime.py" <<'PY'
import datetime, json, os, sys, time
src, dst, virtual = sys.argv[1], sys.argv[2], int(sys.argv[3])
clean = len(sys.argv) > 4
delta = time.time() - virtual

def shift(node):
    if isinstance(node, dict):
        return {k: (int(v + delta) if k == "resetsAt" and isinstance(v, (int, float)) else shift(v))
                for k, v in node.items()}
    if isinstance(node, list):
        return [shift(v) for v in node]
    return node

out, newest = [], None
for line in open(src, encoding="utf-8"):
    rec = json.loads(line)
    moment = datetime.datetime.fromisoformat(rec["timestamp"].replace("Z", "+00:00")).timestamp()
    if moment > virtual:
        continue
    if clean and (rec.get("apiErrorStatus") == 429 or rec.get("usageLimitNote")
                  or rec.get("subtype") == "informational"):
        continue
    rec = shift(rec)
    moved = moment + delta
    stamp = datetime.datetime.fromtimestamp(moved, datetime.timezone.utc)
    rec["timestamp"] = stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"
    out.append(json.dumps(rec, ensure_ascii=False))
    newest = moved
if not out:
    raise SystemExit("перекладка: ни одной записи не новее виртуального мига — проверять нечего")
os.makedirs(os.path.dirname(dst), exist_ok=True)
with open(dst, "w", encoding="utf-8") as stream:
    stream.write("\n".join(out) + "\n")
os.utime(dst, (newest, newest))
print(int(delta))
PY

# ── Ф. судья «лимит держится» на записанном образце ─────────────────────────
echo "── Ф. судья: limit-in-log.py --действует на хвосте 30.10 ──"
verdict() {  # $1 = виртуальный миг, $2 = файл экрана ("" — без экрана)
    python3 "$JUDGE" --действует "$TAIL" ${2:+--экран "$2"} --сейчас "$(virt "$1")" 2>/dev/null \
        | head -1 | tr '\t' ' '
}
case_check "Ф1: 16:20, отказа 429 ещё нет, экран и заметка CLI — лимит держится, сброс 17:30, подъём +10 с" \
    "1 $RESET $WAKE" "$(verdict 16:20:09 "$SCREEN")"
case_check "Ф2: 16:40, отказ 429 с resetsAt — лимит держится, сброс 17:30, подъём 17:30:10" \
    "1 $RESET $WAKE" "$(verdict 16:40:09 "")"
case_check "Ф3: 17:20, за 10 мин до сброса — всё ещё держится (пинок рано)" \
    "1 $RESET $WAKE" "$(verdict 17:20:05 "")"
case_check "Ф4: 17:30:15, подъём прошёл — не держится" "0" "$(verdict 17:30:15 "")"
python3 "$T/retime.py" "$TAIL" "$T/f-clean.jsonl" "$(virt 16:15:00)" clean >/dev/null
case_check "Ф5 контроль: хвост без машинных признаков (слова «лимит» в ответах остались) — не лимит" \
    "0" "$(python3 "$JUDGE" --действует "$T/f-clean.jsonl" 2>/dev/null | head -1 | tr '\t' ' ')"

# ── Д. экран: признак лимита и время без выдумки ───────────────────────────
echo "── Д. экран с фото владельца и строка «None» ──"
tmux new-session -d -x 200 -y 60 -s "${PANE}-d" "cat '$SCREEN'; exec sleep 600"
sleep 0.5
case_check "Д1: экран с фото — сторож видит лимит (--признак-лимита)" "0" \
    "$(bash "$DAEMON" --признак-лимита "${PANE}-d" >/dev/null 2>&1; echo $?)"
printf '%s\n' "> Работы не видно 51 минут. СЛЕДУЮЩАЯ РАБОТА: hr.скачай-видео-по-ссылке-https — начата" \
    > "$T/kick-only.txt"
tmux new-session -d -x 200 -y 60 -s "${PANE}-k" "cat '$T/kick-only.txt'; exec sleep 600"
sleep 0.5
case_check "Д2 контроль: кириллический пинок без слов лимита — не лимит" "1" \
    "$(bash "$DAEMON" --признак-лимита "${PANE}-k" >/dev/null 2>&1; echo $?)"
in_text() { printf '%s\n' "$1" > "$T/text.txt"; python3 "$JUDGE" --в-тексте "$T/text.txt" 2>/dev/null; }
case_check "Д3: «Usage limit reached again · continuing automatically at 5:30pm» — обновится в 17:30" \
    "обновится в 17:30" "$(in_text "● Usage limit reached again · continuing automatically at 5:30pm · esc to cancel")"
case_check "Д4: экран с фото целиком — время 24-часовое 17:30 с поясом" \
    "обновится в 17:30 (Asia/Qostanay)" "$(python3 "$JUDGE" --в-тексте "$SCREEN" 2>/dev/null)"
case_check "Д5: «resets None» — лимит без выдуманного времени" \
    "срок сброса CLI не назвал" "$(in_text "You've hit your session limit · resets None")"
case_check "Д6: «continuing automatically at None» — лимит без выдуманного времени" \
    "срок сброса CLI не назвал" "$(in_text "Usage limit reached · continuing automatically at None · esc to cancel")"
printf '%s\n' "Usage limit reached · continuing automatically at None · esc to cancel" > "$T/none-screen.txt"
python3 "$T/retime.py" "$TAIL" "$T/f-early.jsonl" "$(virt 16:09:00)" clean >/dev/null
case_check "Д7: экран «at None», в журнале срока нет — держится, сброс и подъём пусты" \
    "1  " "$(python3 "$JUDGE" --действует "$T/f-early.jsonl" --экран "$T/none-screen.txt" 2>/dev/null | head -1 | tr '\t' ' ')"

# ── стенд сторожа: своя панель, свой LOG_DIR, канал-файл ───────────────────
cp /bin/cat "$T/claude"
mkdir -p "$T/project/scripts/lib" "$T/projects"
for f in work-queue.py limit_mark.py limit-in-log.py stall-in-log.py; do
    cp "$HERE/$f" "$T/project/scripts/"
done
cp "$HERE"/lib/*.py "$HERE/lib/config.sh" "$T/project/scripts/lib/"
cat > "$T/project/scripts/tg_send.sh" <<CHANNEL
#!/usr/bin/env bash
# Ключи двери — отдельным файлом, строка на вызов «<ключи><TAB><текст>»: channel.txt
# остаётся прежним, а случай В4 видит, с каким ключом ушла весть 6б.
FLAGS=""; TEXT=""
for A in "\$@"; do
    case "\$A" in --*) FLAGS="\$FLAGS \$A" ;; *) [ -n "\$TEXT" ] || TEXT="\$A" ;; esac
done
printf '%s\t%s\n' "\${FLAGS# }" "\$(printf '%s' "\$TEXT" | tr '\n' ' ')" >> "$T/channel-flags.txt"
for A in "\$@"; do
    case "\$A" in --*) continue ;; esac
    printf '%s\n' "\$A" | tr '\n' ' ' >> "$T/channel.txt"
    printf '\n' >> "$T/channel.txt"
    break
done
CHANNEL
chmod +x "$T/project/scripts/tg_send.sh"
A_DIR="$T/projects/${T//\//-}-project"
# Порог ротации задан здесь, а не взят из образца: хвост образца занимает 506k из
# 1000k, лимит застаёт смену с назревшей ротацией, и её отказ — ещё один голос
# владельцу. Живой порог 75 → 50 (07.11) сделал этот случай обычным.
SEED=""
for c in "$HERE/../config/harness.conf" "${HARNESS_CONF:-/etc/harness/harness.conf}"; do
    [ -f "$c" ] && { SEED="$c"; break; }
done
[ -n "$SEED" ] || { echo "ПЛОХО: образца harness.conf нет — проверять нечем"; exit 1; }
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
CTX_WINDOW_TOKENS=1000000  # не-секрет: размер окна модели в токенах
CTX_ROTATE_PCT=50
SILENCE_MAX_MIN=10
PULSE_MAX_MIN=30
PULSE_OWNER_MIN=60
LIMIT_STALL_MIN=20
HELPER_FRESH_MIN=20
KICK_ANSWER_WAIT_MIN=5
TG_CHAT_ID=""
TG_TOKEN_FILE="/nonexistent"
CONF

stand() {  # $1 = файл экрана ("" — пустой экран): свежий LOG_DIR, карта с работой, панель
    rm -rf "$T/log" "$T/state" "$A_DIR"
    mkdir -p "$T/log/inbox/обработано" "$T/state/panel-state" "$A_DIR"
    : > "$T/channel.txt"; : > "$T/pane.txt"; : > "$T/channel-flags.txt"
    printf '%s\n' '{"queue": [], "lines": [{"id": "hr", "tasks": [{"id": "hr.b", "status": "wip"}]}]}' \
        > "$T/project/dev-map.yaml"
    tmux kill-session -t "$PANE" 2>/dev/null
    tmux new-session -d -x 200 -y 60 -s "$PANE" "${1:+cat '$1';} exec $T/claude >> $T/pane.txt"
    sleep 0.5
    [ "$(tmux display-message -p -t "$PANE" '#{pane_current_command}')" = claude ] \
        || { echo "ПЛОХО: подставной агент не встал в панель — проверять нечего"; exit 1; }
}
run_warden() {
    env -u TMUX -u CLAUDE_CODE_SESSION_ID HARNESS_CONF="$T/harness.conf" HARNESS_INSTALL_CONF="$T/harness.conf" \
        HARNESS_ACTIVE_LINE=hr PANE_ENTER_DELAY=0.2 \
        HARNESS_PANEL_STATE="$T/state/panel-state" CLAUDE_PROJECTS_DIR="$T/projects" \
        bash "$DAEMON" >> "$T/log/warden.out" 2>&1 < /dev/null
    sleep 0.3
}
# Метки LOG_DIR стареют вместе с виртуальными часами: mtime и эпохи внутри
# коротких файлов (метка лимита хранит и миг постановки, и час сброса).
age_log() {  # $1 = секунд
    local f now
    [ "$1" -ne 0 ] || return 0
    now=$(date +%s)
    while IFS= read -r f; do
        if [ "$(stat -c %s "$f")" -lt 200 ]; then
            python3 - "$f" "$1" "$now" <<'PY'
import re, sys
path, s, now = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
text = open(path, encoding="utf-8", errors="replace").read()
moved = re.sub(r"\b\d{10}\b", lambda m: str(int(m.group()) - s) if abs(int(m.group()) - now) < 5 * 86400 else m.group(), text)
if moved != text:
    open(path, "w", encoding="utf-8").write(moved)
PY
        fi
        touch -d "@$(( $(stat -c %Y "$f") - $1 ))" "$f"
    done < <(find "$T/log" -type f)
}
report_at() {  # $1 = виртуальный миг последнего отчёта, $2 = виртуальное «сейчас»
    local ago=$(( $(virt "$2") - $(virt "$1") ))
    echo $(( $(date +%s) - ago )) > "$T/log/last_report"
    touch -d "@$(( $(date +%s) - ago ))" "$T/log/last_report"
}
transcript_at() {  # $1 = виртуальный миг [clean] → печатает дельту
    python3 "$T/retime.py" "$TAIL" "$A_DIR/$FSID.jsonl" "$(virt "$1")" ${2:+clean} \
        || { echo "ПЛОХО: перекладка образца не вышла — проверять нечего"; exit 1; }
}
pane_lines() { grep -c . "$T/pane.txt" 2>/dev/null || true; }
pane_has() { grep -qF -- "$1" "$T/pane.txt" 2>/dev/null && echo да || echo нет; }
event() { local n; n=$(grep -c "\"event\":\"$1\"" "$T/log/rotation.jsonl" 2>/dev/null); echo "${n:-0}"; }
said() { local n; n=$(grep -c -- "$1" "$T/channel.txt" 2>/dev/null); echo "${n:-0}"; }

# ── А. ветка 6 «Работы не видно» при доказанном лимите ──────────────────────
echo "── А0 контроль: 16:30, тот же хвост без признаков лимита — ветка 6 будит ──"
stand ""
transcript_at 16:30:08 clean >/dev/null
report_at 16:10:58 16:30:08
run_warden
case_check "А0: условие ветки 6 создано — в панель ушло «Работы не видно»" "да" "$(pane_has 'Работы не видно')"
# Контроль красный — стенд не работает: причина из вывода сторожа, а не догадка.
[ "$(pane_has 'Работы не видно')" = да ] || tail -5 "$T/log/warden.out" 2>/dev/null | sed 's/^/      сторож: /'

echo "── А1: 16:30, отказ 429 есть, метки нет, на экране лимит — в панель ни слова ──"
stand "$SCREEN"
transcript_at 16:30:08 >/dev/null
report_at 16:10:58 16:30:08
run_warden
case_check "А1: условие создано — сторож сам доказал лимит (журнал сторожа)" "да" \
    "$(grep -q 'доказан 1' "$T/log/warden.out" "$T/log/session-warden.log" 2>/dev/null && echo да || echo нет)"
case_check "А1: в панель не ушло «Работы не видно»" "нет" "$(pane_has 'Работы не видно')"
case_check "А1: события silence_kick нет" "0" "$(event silence_kick)"
case_check "А1: в панель не ушло ничего (никакой пинок до сброса)" "0" "$(pane_lines)"

echo "── А2: 16:40, метка limit-watch стоит с 16:23 — в панель ни слова ──"
stand "$SCREEN"
DELTA=$(transcript_at 16:40:09)
printf '%s limit %s\n' "$(( $(date +%s) - ($(virt 16:40:09) - $(virt 16:23:04)) ))" "$(( RESET + DELTA ))" \
    > "$T/log/лимит-подписки.состояние"
report_at 16:10:58 16:40:09
run_warden
case_check "А2: в панель не ушло «Работы не видно»" "нет" "$(pane_has 'Работы не видно')"
case_check "А2: в панель не ушло ничего" "0" "$(pane_lines)"
case_check "А2: владельцу ничего (о лимите уже сказал limit-watch)" "0" "$(grep -c . "$T/channel.txt")"

# Экран — догадка, журнал — факт: чинка «смотреть только на экран» прошла бы
# А1 и А2, поэтому здесь экран пуст и метки нет.
echo "── А3: 16:40, экран пуст, метки нет — лимит только в журнале, в панель ни слова ──"
stand ""
transcript_at 16:40:09 >/dev/null
report_at 16:10:58 16:40:09
run_warden
case_check "А3: в панель не ушло ничего" "0" "$(pane_lines)"

# ── Б. пульс 6в «Владельцу нет отчёта» при видимом лимите ──────────────────
echo "── Б0 контроль: 16:15, без признаков лимита, работа 6 мин, отчёта 31 мин — пульс просит статус ──"
stand ""
transcript_at 16:15:00 clean >/dev/null
report_at 15:44:00 16:15:00
run_warden
case_check "Б0: условие пульса создано — в панель ушло «Владельцу нет отчёта»" "да" "$(pane_has 'Владельцу нет отчёта')"

echo "── Б1: 16:15, заметка CLI «continuing automatically at 5:30pm» и экран — пульс молчит ──"
stand "$SCREEN"
transcript_at 16:15:00 >/dev/null
report_at 15:44:00 16:15:00
run_warden
case_check "Б1: в панель не ушло «Владельцу нет отчёта»" "нет" "$(pane_has 'Владельцу нет отчёта')"
case_check "Б1: события pulse_kick нет" "0" "$(event pulse_kick)"
case_check "Б1: владельцу не ушло «не отчитывается»" "0" "$(said 'не отчитывается')"

# Отказа 429 в 16:15 ещё нет: лимит называет запись CLI system/informational.
# Экран пуст — чинка «по экрану» или «только по 429» здесь краснеет.
echo "── Б2: 16:15, экран пуст, лимит только заметкой CLI в журнале — пульс молчит ──"
stand ""
transcript_at 16:15:00 >/dev/null
report_at 15:44:00 16:15:00
run_warden
case_check "Б2: в панель не ушло ничего" "0" "$(pane_lines)"

# ── Г0. 17:20 одним обходом: ветка 6б «пробую разбудить» за 10 мин до сброса ─
# Живой журнал 30.10 17:20:14: «лимит подписки: тишина 68 мин — пробую разбудить
# смену». Прошлый пинок 6б был в 16:30, метка limit-watch стоит с 16:23. Отдельный
# обход, а не только серия ниже: в серии кулдаун 6б зависит от хода часов стенда.
echo "── Г0: 17:20, метка лимита и пинок 6б в 16:30 — до сброса 10 мин, в панель ни слова ──"
stand "$SCREEN"
DELTA=$(transcript_at 17:20:05)
NOW_R=$(date +%s)
printf '%s limit %s\n' "$(( NOW_R - ($(virt 17:20:05) - $(virt 16:23:04)) ))" "$(( RESET + DELTA ))" \
    > "$T/log/лимит-подписки.состояние"
KICK_AT=$(( NOW_R - ($(virt 17:20:05) - $(virt 16:30:12)) ))
echo "$KICK_AT" > "$T/log/лимит-подписки.пинок"; touch -d "@$KICK_AT" "$T/log/лимит-подписки.пинок"
report_at 16:10:58 17:20:05
run_warden
case_check "Г0: в панель не ушло «Если лимит подписки обновился»" "нет" "$(pane_has 'Если лимит подписки обновился')"
case_check "Г0: в панель не ушло ничего" "0" "$(pane_lines)"

# ── В+Г. весь лимит обходами по 10 минут, как 30.10 ─────────────────────────
echo "── В+Г: обходы 16:10…17:29:55 и 17:40 — одно сообщение владельцу, пинков до сброса нет ──"
stand "$SCREEN"
report_at 16:10:58 16:10:05
PREV_V=""; PREV_R=""; BEFORE_KICKS=0; KICK_1720=0; MSG_TICK=""; MSG_DELTA=""
for at in 16:10:05 16:20:05 16:30:05 16:40:05 16:50:05 17:00:05 17:10:05 17:20:05 17:29:55; do
    V=$(virt "$at"); R=$(date +%s)
    [ -z "$PREV_V" ] || age_log $(( (V - PREV_V) - (R - PREV_R) ))
    PREV_V=$V; PREV_R=$R
    DELTA=$(transcript_at "$at")
    lines0=$(pane_lines); msgs0=$(grep -c . "$T/channel.txt")
    out0=$(grep -c '' "$T/log/warden.out" 2>/dev/null || echo 0)
    run_warden
    grew=$(( $(pane_lines) - lines0 ))
    tail -n +"$((out0 + 1))" "$T/log/warden.out" \
        | grep -E 'тишина: очередь|лимит подписки|пинка не трачу|решение лимитной' | sed 's/^/      /'
    BEFORE_KICKS=$((BEFORE_KICKS + grew))
    [ "$at" != 17:20:05 ] || KICK_1720=$grew
    if [ "$(grep -c . "$T/channel.txt")" -gt "$msgs0" ] && [ -z "$MSG_TICK" ]; then
        MSG_TICK=$at; MSG_DELTA=$DELTA
    fi
    echo "  обход $at: в панель строк +$grew, владельцу всего $(grep -c . "$T/channel.txt")"
done
case_check "Г1: 17:20 (за 10 мин до сброса) — пинка нет" "0" "$KICK_1720"
case_check "Г2: за весь лимит до сброса в панель не ушло ничего" "0" "$BEFORE_KICKS"
case_check "В0: условие создано — ротация назрела, сторож её отменил" "да" \
    "$(grep -q 'ротация ОТМЕНЕНА' "$T/log/warden.out" && echo да || echo нет)"
case_check "В1: за весь лимит владельцу ровно одно сообщение" "1" "$(grep -c . "$T/channel.txt")"
WANT_HM=""
[ -z "$MSG_DELTA" ] || WANT_HM=$(date -d "@$(( RESET + MSG_DELTA ))" +%H:%M)
HAS_HM=нет
for d in -60 0 60; do
    [ -n "$WANT_HM" ] || break
    hm=$(date -d "@$(( RESET + MSG_DELTA + d ))" +%H:%M)
    grep -qF "$hm" "$T/channel.txt" && HAS_HM=да
done
case_check "В2: в сообщении час сброса 24-часовой (17:30 на часах стенда = ${WANT_HM:-нет сообщения})" "да" "$HAS_HM"
case_check "В3: в сообщениях нет «pm/am» и «None»" "0" "$(grep -ciE '[0-9](am|pm)|None' "$T/channel.txt")"
# Спека hr.alert-self-sensors-attention §0.3: весть 6б о лимите идёт мимо очереди
# досыла — её же метка лимита иначе придержала бы её у двери канала.
LIMIT_SAID=$(grep -c 'Упёрся в лимит' "$T/channel-flags.txt" 2>/dev/null)
LIMIT_FLAGGED=$(grep 'Упёрся в лимит' "$T/channel-flags.txt" 2>/dev/null | cut -f1 | grep -c -- '--несмотря-на-лимит')
case_check "В4: метки не было, 6б объявила лимит — весть ушла с --несмотря-на-лимит (вестей ${LIMIT_SAID:-0})" "да" \
    "$([ "${LIMIT_SAID:-0}" -ge 1 ] && [ "$LIMIT_FLAGGED" = "$LIMIT_SAID" ] && echo да || echo нет)"
V=$(virt 17:40:05); R=$(date +%s)
age_log $(( (V - PREV_V) - (R - PREV_R) ))
transcript_at 17:40:05 >/dev/null
lines0=$(pane_lines)
run_warden
case_check "Г3 контроль: 17:40, сброс прошёл, смена молчит — сторож будит" "да" \
    "$([ "$(pane_lines)" -gt "$lines0" ] && echo да || echo нет)"

passed=$((CASES_PATHS - CASES_FAILED))
if [ "$CASES_FAILED" -eq 0 ]; then
    echo "итог: $passed/$CASES_PATHS — во время лимита сторож молчит, будит после сброса"
    exit 0
fi
echo "итог: $passed/$CASES_PATHS — КРАСНЫЙ ($CASES_FAILED проб)"
exit 1
