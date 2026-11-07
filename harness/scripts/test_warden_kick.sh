#!/usr/bin/env bash
# Проба пинка тишины сторожа сессий (раздел 6, Silence-watchdog): что пинок
# говорит смене.
#
# Беда: пинок всегда писал «Заблокирована она — бери следующую по порядку
# (work-queue.py --порядок)» — и про СВОБОДНУЮ задачу, которую прибор только
# что назвал; а команда без пути к карте у work-queue.py не запускается.
# Правило: фраза «Заблокирована она» — только когда прибор ответил
# «СЛЕДУЮЩЕЙ РАБОТЫ НЕТ…»; в пинке всегда исполнимая команда
# «python3 $PROJECT_DIR/scripts/work-queue.py $DEVMAP --порядок».
#
# Стенд. Целого прогона сторожа нет: из файла сторожа вырезаются раздел 6 и
# функция agent_prompt и исполняются как есть. Вокруг — подставы: tmux в PATH
# (отвечает «claude» и пишет текст пинка в файл стенда), work-queue.py стенда
# («работа: 1» и заданный ответ на --следующая; --порядок отдаёт настоящему
# прибору, скопированному в стенд, с мини-картой стенда), say/jlog/tg_send —
# в файлы стенда. Живая tmux-сессия, канал и /var/log/harness не задеты:
# сокета tmux у пробы нет вовсе, а STAND_STRACE=1 прогоняет случаи под strace
# и краснит при любом открытии или connect вне стенда и системных путей
# ([[the-probe-played-on-production]]).
#
# Дверь для мутаций: WARDEN_KICK_UNDER_TEST — путь к испорченной копии сторожа.
#
#   bash scripts/test_warden_kick.sh
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
DAEMON="${WARDEN_KICK_UNDER_TEST:-}"
if [ -z "$DAEMON" ]; then
    for cand in "$HERE/../harness/demons/session-warden.sh" "$HERE/../demons/session-warden.sh"; do
        [ -f "$cand" ] && { DAEMON="$cand"; break; }
    done
fi
[ -f "$DAEMON" ] || { echo "ПРОБА СЛОМАНА: нет сторожа ($DAEMON)"; exit 1; }
[ -f "$HERE/work-queue.py" ] && [ -d "$HERE/lib" ] \
    || { echo "ПРОБА СЛОМАНА: нет прибора очереди рядом с пробой ($HERE)"; exit 1; }

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/project/scripts" "$T/log" "$T/home"
cp "$HERE/work-queue.py" "$T/project/scripts/work-queue-real.py"
cp -r "$HERE/lib" "$T/project/scripts/lib"
rm -rf "$T/project/scripts/lib/__pycache__"
printf '%s\n' '{"queue": [], "lines": [{"id": "hr", "tasks": [{"id": "hr.a", "status": "wip"}]}]}' \
    > "$T/project/dev-map.yaml"
: > "$T/install.conf"
printf 'LOG_DIR="%s/log"\nPROJECT_DIR="%s/project"\nACTIVE_LINE="hr"\n' "$T" "$T" > "$T/harness.conf"

# Подставной tmux: has-session — есть, display-message — «claude»,
# send-keys -l — текст в файл. Любой другой вызов — в журнал вызовов.
cat > "$T/bin/tmux" <<TMUX
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$T/tmux-calls.txt"
case "\$1" in
    has-session) exit 0 ;;
    display-message) echo claude; exit 0 ;;
    send-keys) while [ \$# -gt 0 ] && [ "\$1" != "-l" ]; do shift; done
               [ "\${2:-}" = "--" ] && shift
               printf '%s\n' "\${2:-}" >> "$T/kick.txt"; exit 0 ;;
esac
exit 1
TMUX
chmod +x "$T/bin/tmux"

# Подставной прибор очереди: ответ на --следующая — файл стенда.
cat > "$T/project/scripts/work-queue.py" <<'PY'
import os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
if "--порядок" in args:
    os.execv(sys.executable, [sys.executable, os.path.join(here, "work-queue-real.py")] + args)
if "--следующая" in args:
    print(open(os.path.join(here, "..", "..", "next.txt"), encoding="utf-8").read().strip())
    sys.exit()
print("работа: 1")
print("ждут: ")
PY

# Вырезка из сторожа: раздел 6 до раздела 6в и функция agent_prompt.
sed -n '/^# ── 6\. Silence-watchdog/,/^# ── 6в\./p' "$DAEMON" | sed '$d' > "$T/section6.sh"
sed -n '/^agent_prompt() {/,/^}/p' "$DAEMON" > "$T/agent_prompt.sh"
grep -q 'agent_prompt "' "$T/section6.sh" && grep -q '^agent_prompt() {' "$T/agent_prompt.sh" \
    || { echo "ПРОБА СЛОМАНА: в стороже не найден раздел 6 с пинком или agent_prompt"; exit 1; }

cat > "$T/run.sh" <<RUN
set -euo pipefail
say() { printf '%s\n' "\$*" >> "$T/say.txt"; }
jlog() { printf '%s\n' "\$*" >> "$T/jlog.txt"; }
tg_send() { printf '%s\n' "\$*" >> "$T/kanal.txt"; }
limit_until_text() { echo "лимит"; }
сохранить_снимок() { echo "$T/снимок"; }
helpers_present() { [ "\${JUDGE_HELPERS:-0}" != 0 ]; }
pane_cmd() { tmux display-message -p -t "\$TMUX_SESSION" '#{pane_current_command}'; }
pane_kick_service() { tmux send-keys -t "\$TMUX_SESSION" -l -- "\$1"; }
PROJECT_DIR="$T/project" LOG_DIR="$T/log" TMUX_SESSION="proba-stend-kick"
WORK_PAUSED=0 JUDGE_WORK_MIN=45 SILENCE_MAX_MIN=30 JUDGE_HELPERS=0 LIMIT_ACTIVE=0
SESSION_ID=proba WARDEN_RC=0 TRANSCRIPT="$T/net.jsonl"
source "$T/agent_prompt.sh"
source "$T/section6.sh"
RUN

# Окружение стенда: ни один путь не ведёт на боевые места.
stand_env=(env -i PATH="$T/bin:/usr/local/bin:/usr/bin:/bin" HOME="$T/home" LC_ALL=C.UTF-8
    HARNESS_HOME="$T" HARNESS_CONF="$T/harness.conf" HARNESS_INSTALL_CONF="$T/install.conf"
    LOG_DIR="$T/log" PROJECT_DIR="$T/project" STATE_DIR="$T/state" HARNESS_ACTIVE_LINE=hr
    TMUX_TMPDIR="$T/tmux" PYTHONDONTWRITEBYTECODE=1)
STRACE=()
[ "${STAND_STRACE:-0}" = 1 ] && STRACE=(strace -f -qq -e trace=open,openat,creat,connect,execve -o "$T/strace.txt" -s 300)

bad=0; total=0
verdict() {  # $1 = 1 (верно) / 0, $2 = имя, $3 = подробность
    total=$((total + 1))
    if [ "$1" = 1 ]; then echo "  ок    $2"
    else echo "  ПЛОХО $2${3:+ — $3}"; bad=$((bad + 1)); fi
}

kick_for() {  # $1 — ответ прибора на --следующая; печатает текст пинка
    printf '%s\n' "$1" > "$T/next.txt"
    rm -f "$T/kick.txt"
    "${STRACE[@]}" "${stand_env[@]}" bash "$T/run.sh" >> "$T/run.out" 2>&1
    cat "$T/kick.txt" 2>/dev/null
}

# Команда «Порядок работ: …» из пинка, запущенная окружением стенда.
order_runs() {  # $1 — текст пинка; печатает 1/0
    local cmd; cmd=$(printf '%s' "$1" | sed -n 's/.*Порядок работ: //p')
    [ -n "$cmd" ] || { echo 0; return; }
    local -a words; read -ra words <<< "$cmd"
    if "${STRACE[@]/$T\/strace.txt/$T/strace2.txt}" "${stand_env[@]}" "${words[@]}" > "$T/order.out" 2>&1 \
        && grep -q '^работа: ' "$T/order.out"; then echo 1; else echo 0; fi
}

PHRASE="Заблокирована она"

# ── есть работа: прибор назвал задачу — фразы о блокировке нет ────────────
kick=$(kick_for "СЛЕДУЮЩАЯ РАБОТА: hr.a — начатая")
verdict "$(printf '%s' "$kick" | grep -qF "СЛЕДУЮЩАЯ РАБОТА: hr.a" && echo 1 || echo 0)" \
    "есть работа — пинок ушёл и называет hr.a" "пинок: ${kick:-не ушёл}; $(tail -3 "$T/run.out" 2>/dev/null)"
verdict "$(printf '%s' "$kick" | grep -qF "$PHRASE" && echo 0 || echo 1)" \
    "есть работа — «$PHRASE» нет" "пинок: $kick"
verdict "$(order_runs "$kick")" \
    "есть работа — команда «Порядок работ» из пинка запускается с путями стенда, код 0" \
    "команда: $(printf '%s' "$kick" | sed -n 's/.*Порядок работ: //p'); вывод: $(head -2 "$T/order.out" 2>/dev/null)"

# ── работы нет: прибор так и сказал — фраза есть ──────────────────────────
kick=$(kick_for "СЛЕДУЮЩЕЙ РАБОТЫ НЕТ: всё заблокировано либо сделано.")
verdict "$(printf '%s' "$kick" | grep -qF "$PHRASE" && echo 1 || echo 0)" \
    "работы нет — «$PHRASE — бери следующую» есть" "пинок: ${kick:-не ушёл}"
verdict "$(order_runs "$kick")" \
    "работы нет — команда «Порядок работ» из пинка запускается, код 0" \
    "команда: $(printf '%s' "$kick" | sed -n 's/.*Порядок работ: //p'); вывод: $(head -2 "$T/order.out" 2>/dev/null)"

# ── изоляция: под strace ни одного пути вне стенда и системы ──────────────
if [ "${STAND_STRACE:-0}" = 1 ]; then
    leaks=$(cat "$T/strace.txt" "$T/strace2.txt" 2>/dev/null | python3 -c '
import re, sys
stand = sys.argv[1]
system = ("/usr/", "/lib", "/etc/ld.so", "/etc/localtime", "/etc/nsswitch.conf", "/etc/passwd",
          "/etc/group", "/proc/", "/dev/null", "/dev/tty", "/sys/", "/etc/python", "/bin/")
for line in sys.stdin:
    if "connect(" in line and "AF_UNIX" in line and "/var/run/nscd/" not in line:
        print("connect: " + line.strip()); continue
    for path in re.findall(r"(?:openat|open|creat|execve)\((?:AT_FDCWD, )?\"([^\"]+)\"", line):
        if path.startswith(stand) or path.startswith(system) or not path.startswith("/"):
            continue
        print(path)' "$T" | sort -u)
    opened=$(grep -c 'openat(' "$T/strace.txt" 2>/dev/null)
    verdict "$([ -z "$leaks" ] && [ "${opened:-0}" -gt 0 ] && echo 1 || echo 0)" \
        "изоляция: открытий под strace ${opened:-0}, вне стенда и системы — ни одного" "$leaks"
fi

echo
echo "всего путей: $total, неудач: $bad"
[ "$bad" = 0 ]
