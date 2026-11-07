#!/usr/bin/env bash
#
# test_warden_speed.sh — проба ветки 6д сторожа сессий: недобор задач до
# режима скорости (спека .tasks/hr.speed-modes/spec.md, Ч4, п. 6.2–6.3, 10.3.8б).
#
# Судит СВОЙ код сторожа, а не судью: функции speed_kick_step, speed_escalate,
# speed_judge_cfg (и их соседи say, jlog, tg_send, agent_prompt,
# подавлен_кулдауном) берутся из НАСТОЯЩЕГО session-warden.sh, двери панели —
# из НАСТОЯЩЕГО scripts/lib/pane.sh. Подставлено и названо:
#   • судья — свой <проект стенда>/harness/demons/speed_mode.py: отвечает
#     строкой из файла стенда и пишет каждый вызов с доводами (так видно, какие
#     метки сторож передал судье); load_data()["сторож"] — пороги эскалации;
#   • tmux — свой файл первым в PATH, тексты кладёт в файл стенда;
#   • канал — свой <проект стенда>/scripts/tg_send.sh;
#   • LOG_DIR, ROTATION_LOG, состояние панели — каталоги стенда.
# Что стенд не дотягивается до живых tmux, канала и /var/log/harness, проба
# доказывает сама — один проход под strace (случай «стенд»).
#
# Двери копий для мутаций: WARDEN_SPEED_DAEMON — путь сторожа, WARDEN_SPEED_PANE —
# путь pane.sh.
#
#   bash scripts/test_warden_speed.sh
# Код возврата: 0 — зелёная, 1 — красная.
#
# Имена переменных ЛАТИНИЦЕЙ (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
source "$HERE/lib/selftest_cases.sh"
# В проекте демоны под harness/demons; в пакете пробы лежат в harness/scripts,
# и демоны — соседний каталог.
DAEMON="${WARDEN_SPEED_DAEMON:-$HERE/../harness/demons/session-warden.sh}"
[ -f "$DAEMON" ] || DAEMON="$HERE/../demons/session-warden.sh"
PANE_LIB="${WARDEN_SPEED_PANE:-$HERE/lib/pane.sh}"
for f in "$DAEMON" "$PANE_LIB"; do
    [ -s "$f" ] || { echo "ПЛОХО: нет файла по пути раскладки: $f — проверять нечего"; exit 1; }
done

T=$(mktemp -d -t warden-speed.XXXXXX)
trap 'rm -rf "$T"' EXIT

# ── функции сторожа: вырезаются из файла по имени ───────────────────────────
# Разбор, а не список строк: пустой вырез — красный, а не «нечего проверять».
FUNCS="say подавлен_кулдауном jlog tg_send agent_prompt speed_judge_cfg speed_escalate speed_kick_step"
awk -v names="$FUNCS" '
    BEGIN { n = split(names, list, " "); for (i = 1; i <= n; i++) want[list[i] "() {"] = 1 }
    !inside { for (k in want) if (index($0, k) == 1) { inside = 1; got[k] = 1; break } }
    inside { print; if ($0 ~ /^}/ || ($0 ~ /\(\) \{.*\}[[:space:]]*$/ && $0 !~ /<</)) inside = 0 }
    END { for (k in want) if (!(k in got)) { print "нет функции " k > "/dev/stderr"; exit 1 } }
' "$DAEMON" > "$T/funcs.sh" || { echo "ПЛОХО: в $DAEMON нет функций ветки 6д — проверять нечего"; exit 1; }
bash -n "$T/funcs.sh" || { echo "ПЛОХО: вырез функций сторожа не разбирается bash — проверять нечего"; exit 1; }

# ── стенд ───────────────────────────────────────────────────────────────────
PROJ="$T/proj"
mkdir -p "$PROJ/harness/demons" "$PROJ/scripts" "$PROJ/judge" "$T/bin" "$T/log" "$T/panel"
cat > "$PROJ/harness/demons/speed_mode.py" <<'PY'
"""Судья стенда: ответы — из файлов judge/, каждый вызов — строка в calls.jsonl."""
import json
import os
import sys
import time

JUDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "judge")


def read(name, default=""):
    try:
        with open(os.path.join(JUDGE, name), encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return default


def load_data():
    return {"сторож": json.loads(read("cfg.json"))}


def arg(args, key):
    return float(args[args.index(key) + 1]) if key in args else 0.0


if __name__ == "__main__":
    args = sys.argv[1:]
    with open(os.path.join(JUDGE, "calls.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(args, ensure_ascii=False) + "\n")
    mode = "сторожу" if "--сторожу" in args else "показать"
    rc = int(read(f"rc.{mode}", "0") or 0)
    if rc:
        sys.stderr.write("Traceback (most recent call last):\n  судья стенда упал\n")
        sys.exit(rc)
    if mode == "показать":
        sys.stdout.write(read("shown"))
        sys.exit(0)
    verdict = read("verdict").rstrip("\n")
    cfg, now = load_data()["сторож"], time.time()
    # Окна антиспама — слово судьи; стенд повторяет их, чтобы было видно, что
    # сторож передал метку, а не что окно считает он сам.
    over, kick = arg(args, "--последнее-лишнее"), arg(args, "--последний-пинок")
    if verdict.startswith("лишнее") and over > 0 and now - over < cfg["лишнее_не_чаще_мин"] * 60:
        verdict = "молчу\tо лишнем говорили недавно"
    if verdict.startswith("недобор") and kick > 0 and now - kick < cfg["пинок_не_чаще_мин"] * 60:
        verdict = "молчу\tпинок был недавно"
    print(verdict)
PY
printf '%s\n' '{"пинок_не_чаще_мин": 20, "лишнее_не_чаще_мин": 60, "эскалация_после_пинков": 3, "эскалация_не_чаще_мин": 720}' \
    > "$PROJ/judge/cfg.json"
cat > "$PROJ/scripts/tg_send.sh" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$T/tg.log"
EOF
cat > "$T/bin/tmux" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$T/tmux.log"
case "\$1 \$*" in
    "display-message "*pane_dead*) echo 0 ;;
    "display-message "*pane_current_command*) echo claude ;;
    "send-keys "*" -l -- "*) printf '%s\n' "\${@: -1}" >> "$T/sent.txt" ;;
esac
exit 0
EOF
chmod +x "$PROJ/scripts/tg_send.sh" "$T/bin/tmux"

# Один проход сторожа: свежая оболочка, как у настоящего обхода. Довод —
# толчок, прошедший раньше в этом проходе: owner (6а), service (пульс/тишина).
cat > "$T/pass.sh" <<'EOF'
set -uo pipefail
source "$PANE_LIB"
source "$STAND/funcs.sh"
TMUX_KICKED=0
case "${1:-}" in
    owner) pane_kick_owner_locked() { return 0; }
           pane_kick_owner "толчок входящих стенда" || echo "ПРЕДЫСТОРИЯ: толчок входящих не прошёл" ;;
    service) agent_prompt "пульс стенда" || echo "ПРЕДЫСТОРИЯ: служебный толчок не прошёл" ;;
esac
OWNER_WAITING="${OWNER_WAITING_STAND:-0}"
speed_kick_step
EOF
unset TMUX TMUX_PANE
STAND_ENV=(env PATH="$T/bin:$PATH" STAND="$T" PANE_LIB="$PANE_LIB" PROJECT_DIR="$PROJ"
    LOG_DIR="$T/log" HARNESS_LOG_DIR="$T/log" ROTATION_LOG="$T/log/rotation.jsonl"
    HARNESS_PANEL_STATE="$T/panel" HARNESS_CONF="$T/harness.conf" HARNESS_INSTALL_CONF="$T/harness.conf"
    TMUX_SESSION=warden-speed-stand SESSION_ID=stand-session PANE_ENTER_DELAY=0)
printf 'LOG_DIR="%s"\n' "$T/log" > "$T/harness.conf"

fresh() {  # чистое состояние: журналы, метки, ответы судьи
    rm -rf "$T/log" "$T/sent.txt" "$T/tmux.log" "$T/tg.log" "$PROJ/judge"/{calls.jsonl,verdict,shown,rc.*}
    mkdir -p "$T/log"
}
judge() { printf '%b' "$1" > "$PROJ/judge/verdict"; printf '%b' "$2" > "$PROJ/judge/shown"; }
pass() {  # довод — предыстория прохода; вывод сторожа — в $T/pass.out
    "${STAND_ENV[@]}" OWNER_WAITING_STAND="${OWNER_WAITING_STAND:-0}" bash "$T/pass.sh" "${1:-}" > "$T/pass.out" 2>&1
}
count() { if [ -f "$2" ]; then grep -c -- "$1" "$2"; else echo 0; fi; }  # строк с образцом; нет файла — 0
speed_texts() { count '^\[сторож скорости\]' "$T/sent.txt"; }
all_texts() { [ -f "$T/sent.txt" ] && wc -l < "$T/sent.txt" || echo 0; }
said() { grep -q -- "$1" "$T/pass.out" && echo да || echo нет; }
events() { count "\"$1\"" "$T/log/режим-скорости.jsonl"; }
calls() { [ -f "$PROJ/judge/calls.jsonl" ] && wc -l < "$PROJ/judge/calls.jsonl" || echo 0; }
last_arg() {  # значение довода в последнем вызове судьи --сторожу
    python3 - "$PROJ/judge/calls.jsonl" "$1" <<'PY'
import json, sys
calls = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8")]
calls = [c for c in calls if "--сторожу" in c]
print(calls[-1][calls[-1].index(sys.argv[2]) + 1] if calls else "нет вызова")
PY
}
age() { touch -d "-$2 min" "$T/log/$1"; }   # состарить метку на N минут
tg_count() { [ -f "$T/tg.log" ] && wc -l < "$T/tg.log" || echo 0; }

SHOWN_MED='Режим «средний»: 3 задачи одновременно. Сейчас могу 3.\nВ работе 1, могу взять ещё 2.\n'

# ── 1. недобор: пинок с числом и id, метка, событие ─────────────────────────
echo "── 1. недобор → пинок ──"
fresh; judge 'недобор\t2\thr.x hr.y' "$SHOWN_MED"
pass
case_check "1: один текст скорости в панели" "1" "$(speed_texts)"
case_check "1: в тексте число и id задач через запятую" "да" \
    "$(grep -q 'Возьми ещё 2: hr.x, hr.y' "$T/sent.txt" 2>/dev/null && echo да || echo нет)"
case_check "1: в тексте обе строки слов судьи" "да" \
    "$(grep -q 'Режим «средний»: 3 задачи одновременно. Сейчас могу 3. В работе 1, могу взять ещё 2.' "$T/sent.txt" 2>/dev/null && echo да || echo нет)"
case_check "1: метка speed.kick поставлена" "да" "$([ -f "$T/log/speed.kick" ] && echo да || echo нет)"
case_check "1: событие speed.kick в журнале режима (одно)" "1" "$(events speed.kick)"
case_check "1: в событии число в работе из слов судьи" "да" \
    "$(grep '"speed.kick"' "$T/log/режим-скорости.jsonl" 2>/dev/null | grep -q '"в_работе": *"1"' && echo да || echo нет)"
case_check "1: первый вызов — метки пинка ещё нет (0)" "0" "$(last_arg --последний-пинок)"

# ── 2. повтор: метка пинка передана судье, окно — его слово ────────────────
echo "── 2. повтор в окне → судья знает о пинке, молчание ──"
KICK_TS=$(stat -c '%Y' "$T/log/speed.kick")
pass
case_check "2: судье передан миг метки speed.kick" "$KICK_TS" "$(last_arg --последний-пинок)"
case_check "2: второго текста нет" "1" "$(speed_texts)"

# ── 3. уступка: слово владельца во входящих ─────────────────────────────────
echo "── 3. OWNER_WAITING=1 → молчание ──"
fresh; judge 'недобор\t2\thr.x hr.y' "$SHOWN_MED"
OWNER_WAITING_STAND=1 pass
case_check "3: текста нет" "0" "$(all_texts)"
case_check "3: судья не спрошен" "0" "$(calls)"
case_check "3: строка «во входящих слово владельца»" "да" "$(said 'во входящих слово владельца')"
case_check "3: метки пинка нет" "нет" "$([ -f "$T/log/speed.kick" ] && echo да || echo нет)"

# ── 4. уступка: толчок входящих (6а) уже был в проходе ──────────────────────
echo "── 4. пинок 6а в этом проходе → молчание ──"
fresh; judge 'недобор\t2\thr.x hr.y' "$SHOWN_MED"
pass owner
case_check "4: предыстория прошла (толчок входящих отдан)" "нет" "$(said 'ПРЕДЫСТОРИЯ')"
case_check "4: текста скорости нет" "0" "$(speed_texts)"
case_check "4: строка «уже был пинок»" "да" "$(said 'уже был пинок')"
case_check "4: судья не спрошен" "0" "$(calls)"

# ── 5. уступка: служебный толчок (пульс/тишина) уже был в проходе ──────────
echo "── 5. служебный толчок в этом проходе → ровно один текст ──"
fresh; judge 'недобор\t2\thr.x hr.y' "$SHOWN_MED"
pass service
case_check "5: в панели ровно один текст — служебный" "1 0" "$(all_texts) $(speed_texts)"
case_check "5: строка «уже был пинок»" "да" "$(said 'уже был пинок')"

# ── 6. лишнее: совет, метка, окно — по метке ────────────────────────────────
echo "── 6. лишнее → совет, не чаще окна ──"
fresh; judge 'лишнее\t2' 'Режим «средний»: 3 задачи одновременно. Сейчас могу 3.\nВ работе 5, лишних 2.\n'
pass
case_check "6: один совет «Новых задач не бери»" "1" \
    "$(count '^\[сторож скорости\].*Лишних задач в работе: 2. Новых задач не бери' "$T/sent.txt")"
case_check "6: никто не остановлен (нет /exit, C-c)" "0" "$(count '/exit\|C-c' "$T/tmux.log")"
case_check "6: метка speed.over поставлена" "да" "$([ -f "$T/log/speed.over" ] && echo да || echo нет)"
case_check "6: событие speed.over (одно), speed.kick нет" "1 0" "$(events speed.over) $(events speed.kick)"
OVER_TS=$(stat -c '%Y' "$T/log/speed.over")
pass
case_check "6: в окне — судье передан миг speed.over" "$OVER_TS" "$(last_arg --последнее-лишнее)"
case_check "6: в окне — второго совета нет" "1" "$(speed_texts)"
age speed.over 61
pass
case_check "6: окно прошло — второй совет" "2" "$(speed_texts)"

# ── 7. эскалация: три пинка без прибавки → одно слово владельцу ────────────
echo "── 7. эскалация ──"
fresh; judge 'недобор\t2\thr.x hr.y' 'Режим «быстрый»: 5 задач одновременно. Сейчас могу 5.\nВ работе 3, могу взять ещё 2.\n'
for n in 1 2 3; do pass; [ -f "$T/log/speed.kick" ] && age speed.kick 21; done
case_check "7: три пинка без прибавки — пинков 3, владельцу 0" "3 0" "$(speed_texts) $(tg_count)"
pass; age speed.kick 21
case_check "7: третий неотвеченный — одно слово владельцу" "1" "$(tg_count)"
case_check "7: в слове числа из слов судьи (в работе 3, режим 5)" "да" \
    "$(grep -q 'в работе 3 при режиме 5' "$T/tg.log" 2>/dev/null && echo да || echo нет)"
case_check "7: слово под своей меткой задачи" "да" \
    "$(grep -q -- '--task session-warden:speed-unanswered' "$T/tg.log" 2>/dev/null && echo да || echo нет)"
pass; age speed.kick 21
case_check "7: следующий неотвеченный — второго слова нет (не чаще окна)" "1" "$(tg_count)"
judge 'недобор\t1\thr.z' 'Режим «быстрый»: 5 задач одновременно. Сейчас могу 5.\nВ работе 4, могу взять ещё 1.\n'
pass
case_check "7: прибавка — счёт неотвеченных снят" "0" "$(cat "$T/log/speed.unanswered" 2>/dev/null)"
judge 'ровно' 'Режим «быстрый»: 5 задач одновременно. Сейчас могу 5.\nВ работе 5, недобора нет.\n'
echo 2 > "$T/log/speed.unanswered"
pass
case_check "7: «ровно» — счёт неотвеченных снят" "нет" "$([ -f "$T/log/speed.unanswered" ] && echo да || echo нет)"

# ── 8. судья упал ───────────────────────────────────────────────────────────
echo "── 8. судья упал → молчание и строка в журнале ──"
fresh; judge 'недобор\t2\thr.x hr.y' "$SHOWN_MED"; echo 2 > "$PROJ/judge/rc.сторожу"
pass
case_check "8: --сторожу код 2 — текста нет" "0" "$(all_texts)"
case_check "8: строка «судья не ответил (код 2)»" "да" "$(said 'судья не ответил (код 2)')"
case_check "8: метки пинка нет" "нет" "$([ -f "$T/log/speed.kick" ] && echo да || echo нет)"
rm -f "$PROJ/judge/rc.сторожу"; echo 1 > "$PROJ/judge/rc.показать"
pass
case_check "8: --показать упал — текста нет" "0" "$(all_texts)"
case_check "8: строка «слов судьи нет»" "да" "$(said 'слов судьи нет')"

# ── 9. стенд не дотягивается до живого ─────────────────────────────────────
echo "── 9. стенд под strace ──"
if command -v strace >/dev/null 2>&1; then
    fresh; judge 'недобор\t2\thr.x hr.y' "$SHOWN_MED"
    "${STAND_ENV[@]}" OWNER_WAITING_STAND=0 strace -f -qq -e trace=execve,openat,connect \
        -o "$T/strace.txt" bash "$T/pass.sh" > "$T/pass.out" 2>&1
    case_check "9: под strace пинок ушёл (проход живой)" "1" "$(speed_texts)"
    case_check "9: строк трассы не ноль" "да" "$([ "$(wc -l < "$T/strace.txt")" -gt 20 ] && echo да || echo нет)"
    case_check "9: execve только своего tmux (живой не зван)" "0 да" \
        "$(grep 'execve(' "$T/strace.txt" | grep '/tmux"' | grep -vc "\"$T/bin/tmux\"") $(grep -q "execve(\"$T/bin/tmux\"" "$T/strace.txt" && echo да || echo нет)"
    case_check "9: живой канал не зван (tg_send.sh вне стенда)" "0" \
        "$(grep 'execve(' "$T/strace.txt" | grep 'tg_send' | grep -vc "\"$PROJ/scripts/tg_send.sh\"")"
    case_check "9: /var/log/harness не открыт" "0" "$(grep -c '/var/log/harness' "$T/strace.txt")"
    case_check "9: сокет tmux не тронут" "0" "$(grep 'connect(' "$T/strace.txt" | grep -c 'tmux-')"
else
    case_check "9: strace есть" "да" "нет"
fi

passed=$((CASES_PATHS - CASES_FAILED))
if [ "$CASES_FAILED" -eq 0 ]; then
    echo "итог: $passed/$CASES_PATHS — сторож добирает задачи до режима, уступает и зовёт владельца"
    exit 0
fi
echo "итог: $passed/$CASES_PATHS — КРАСНЫЙ ($CASES_FAILED проб)"
exit 1
