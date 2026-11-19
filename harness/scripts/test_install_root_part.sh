#!/usr/bin/env bash
# Проба: root-часть установки не оставляет копии ложных тревог и мёртвых механизмов
# (находки копий 10–11.11.2026, пачка evo-install-root-part).
#
# А. Шаг 13 гоняет демонов до входа владельца: их rc≠0 ложился в РАБОЧИЙ журнал
#    запусков process-runs.jsonl, и сторож меток заводил ложные карточки
#    «демон падает». Журнал запусков пробы установки — свой (HARNESS_RUNS_DIR).
# Б. Юнит службы проверок с StandardOutput=append: systemd открывает файл ДО смены
#    пользователя — новый журнал root:root, демон от агента пишет в него по имени
#    и падает «Permission denied». Журнал открывает сам процесс агента; журналы
#    установщика (install-*.log) заводятся агентом до записи root-ом, а прежние
#    root-овые журналы шаг 1б возвращает агенту на каждом прогоне.
# В. Уборка /tmp зовёт `sudo -n <читатель держателей>`, а правила на него не было:
#    шаг «держатели» мёртв на каждой копии. Читатель — root-овая копия вне дерева
#    агента, правило sudoers — ровно та команда, которую зовёт уборка.
# Г. Обновление root-часть не ставит, а пробелы в ней называет владельцу одной
#    командой. Дверей среза (мутации без потолка) и читателя держателей в его
#    сверке не было — копия, обновлённая без установщика, жила без них молча.
#
# Условие проба создаёт сама: свой каталог, сухой прогон установщика (root не
# нужен, ничего не исполняется), стенды обёртки и шага 13 с подставными
# исполнителями. Боевые /etc, /var/log/harness и crontab не трогаются.
# Двери мутаций: USTANOVIT_UNDER_TEST, RUN_BACKGROUND_UNDER_TEST,
# PROCESS_LIBRARY_UNDER_TEST, TMP_SWEEP_UNDER_TEST, UPDATE_UNDER_TEST.
set -u
here=$(cd "$(dirname "$0")/.." && pwd)
# Проба лежит и в пакете (harness/scripts), и в проекте (scripts/), а судит пакет.
[ -f "$here/../USTANOVIT.sh" ] || here="$(python3 -c "import sys; sys.path.insert(0, '$here/scripts/lib'); import config; print(config.katalog_paketa() / 'harness')" 2>/dev/null || true)"
src=${USTANOVIT_UNDER_TEST:-$here/../USTANOVIT.sh}
[ -f "$src" ] || { echo "SKIP: установщик $src не найден (проба живёт в пакете)"; exit 77; }
rb=${RUN_BACKGROUND_UNDER_TEST:-$here/scripts/run-background.sh}
plib=${PROCESS_LIBRARY_UNDER_TEST:-$here/scripts/lib/process_library.py}
sweep=${TMP_SWEEP_UNDER_TEST:-$here/scripts/tmp-sweep.py}
upd=${UPDATE_UNDER_TEST:-$here/scripts/update.py}
for f in "$rb" "$plib" "$sweep" "$upd" "$here/config/system-jobs.yaml"; do
  [ -f "$f" ] || { echo "FAIL: нет $f — стенд не собрать"; exit 1; }
done
fail=0; total=0
ok()  { total=$((total + 1)); echo "ok:   $1"; }
bad() { total=$((total + 1)); echo "FAIL: $1"; fail=$((fail + 1)); }
t=$(mktemp -d "${TMPDIR:-/tmp}/harness-run-XXXXXX"); trap 'rm -rf "$t"' EXIT

# ── сухой прогон установщика: свежий и --resume со штампами всех шагов ──
mkdir -p "$t/pkg" "$t/stamps" "$t/etc"
ln -s "$here" "$t/pkg/harness"
for u in "$here/../UNIFIED" "$here/../sluzhebnoe/UNIFIED" "$here/../../UNIFIED"; do
  [ -d "$u" ] && { ln -s "$(cd "$u" && pwd)" "$t/pkg/UNIFIED"; break; }
done
[ -e "$t/pkg/UNIFIED" ] || { echo "FAIL: UNIFIED рядом с пакетом не найден — стенд не собрать"; exit 1; }
sed "s|^STAMP_DIR=/var/lib/harness/install-steps\$|STAMP_DIR=$t/stamps|" "$src" > "$t/pkg/USTANOVIT.sh"
[ "$(grep -c "^STAMP_DIR=$t/stamps\$" "$t/pkg/USTANOVIT.sh")" = 1 ] \
  || { echo "FAIL: строка STAMP_DIR установщика не найдена — штампы стенда не подставить"; exit 1; }
dry() { HARNESS_INSTALL_CONF="$t/etc/install.conf" timeout 300 bash "$t/pkg/USTANOVIT.sh" --dry-run "$@" 2>&1; }
fresh=$(dry); rc=$?
[ "$rc" = 0 ] && ok "условие: свежий --dry-run, код 0" || bad "условие: свежий --dry-run, код $rc"
mapfile -t steps < <(awk '/^шаг_0$/ {on=1} on && /^шаг_[^ ]+$/ {print}' "$src")
[ "${#steps[@]}" -ge 10 ] || { echo "FAIL: главная последовательность шагов не разобрана (${#steps[@]})"; exit 1; }
for s in "${steps[@]}"; do touch "$t/stamps/$s.done"; done
resumed=$(dry --resume); rc=$?
[ "$rc" = 0 ] && ok "условие: --resume со штампами, код 0" || bad "условие: --resume со штампами, код $rc"
# Шаг 1б заголовка не печатает и идёт на каждом прогоне: при --resume со штампами
# его строки — почти всё, что осталось, поэтому судится весь вывод.
# Правила 50-agent — строки файла после «записать файл /etc/sudoers.d/50-agent».
agent_sudoers() { awk 'index($0, "записать файл /etc/sudoers.d/50-agent")>0 {on=1; next} on && !/^    \| / {exit} on {sub(/^    \| /, ""); print}' <<<"$1"; }
log_dir=$(sed -n "s/^  + mkdir -p '\([^']*\)' .*/\1/p" <<<"$fresh" | head -1)
agent=$(sed -n 's/^  + chown -R \([^:]*\):.*/\1/p' <<<"$fresh" | head -1)
[ -n "$log_dir" ] && [ -n "$agent" ] && ok "условие: каталог журналов ($log_dir) и агент ($agent) разобраны" \
  || bad "условие: каталог журналов или пользователь агента не разобраны из сухого прогона"
first_line() { grep -nF -- "$1" <<<"$2" | head -1 | cut -d: -f1; }

# ── А1: обёртка пишет журнал запусков туда, куда велит HARNESS_RUNS_DIR ──
S="$t/rb"; mkdir -p "$S/home" "$S/log" "$S/probe"
printf 'PROJECT_DIR="%s"\n' "$S" > "$S/install.conf"
printf 'BACKGROUND_PROCESSES=on\n' > "$S/harness.conf"
printf 'версия: 1\nвыключатель:\n  ключ: BACKGROUND_PROCESSES\n  исключения: []\nпроцессы: {}\n' > "$S/library.yaml"
run_rb() {  # $1 = id, дальше — переменные окружения; ребёнок выходит кодом 1
  local id="$1"; shift
  env -u HARNESS_TASK -u HARNESS_LOG_DIR -u HARNESS_EVENTS_STATE -u INSTALL_CONF -u HARNESS_RUNS_DIR \
      -u PROJECT_DIR -u BACKGROUND_PROCESSES HARNESS_HOME="$S/home" HARNESS_INSTALL_CONF="$S/install.conf" \
      HARNESS_CONF="$S/harness.conf" LOG_DIR="$S/log" HARNESS_PROCESS_LIBRARY="$S/library.yaml" \
      PYTHONDONTWRITEBYTECODE=1 "$@" bash "$rb" "$id" -- sh -c 'exit 1' >/dev/null 2>&1
}
ends_of() { grep -s '"process.end"' "$1" | grep -c "\"id\": *\"$2\""; }
run_rb probe-control
[ "$(ends_of "$S/log/process-runs.jsonl" probe-control)" = 1 ] \
  && ok "А1 контроль: без двери запуск лёг в журнал запусков каталога журналов" \
  || bad "А1 контроль: без двери запуск не лёг в $S/log/process-runs.jsonl — стенд обёртки не работает"
run_rb probe-install HARNESS_RUNS_DIR="$S/probe"
[ "$(ends_of "$S/probe/process-runs.jsonl" probe-install)" = 1 ] \
  && ok "А1: с HARNESS_RUNS_DIR запуск лёг в свой журнал запусков" \
  || bad "А1: с HARNESS_RUNS_DIR запуска нет в $S/probe/process-runs.jsonl (run-background.sh)"
[ "$(ends_of "$S/log/process-runs.jsonl" probe-install)" = 0 ] \
  && ok "А1: рабочий журнал запусков пробы установки не видит" \
  || bad "А1: rc пробы установки лёг в рабочий process-runs.jsonl — сторож меток примет его за падение"

# ── А2: шаг 13 отдаёт демонам свой журнал запусков ──
extract() { awk -v f="$1" 'index($0, f "() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src"; }
: > "$t/lib.sh"
for f in шаг_13 демоны_из_спеки команда_скрипта журнал_агента; do
  body=$(extract "$f")
  [ -n "$body" ] || { echo "FAIL: функция $f не найдена в установщике"; exit 1; }
  printf '%s\n' "$body" >> "$t/lib.sh"
done
P="$t/proekt"; mkdir -p "$P/scripts" "$t/instlog"
cat > "$P/scripts/run-background.sh" <<RB
#!/usr/bin/env bash
printf '%s %s\n' "\$1" "\${HARNESS_RUNS_DIR:-}" >> "$t/ran.log"
exit 1
RB
chmod +x "$P/scripts/run-background.sh"
cat > "$t/run13.sh" <<'RUN'
. "$1"; T=$2
заголовок() { :; }; скажи() { :; }; уже_сделан() { return 1; }; отметить() { :; }
делай() { printf '%s\n' "$1" >> "$T/did.log"; }
sudo() { [ "$1" = -u ] && shift 2; "$@"; }
DRY=0; PROJECT_DIR="$T/proekt"; LOG_DIR="$T/instlog"; HEARTBEAT_DIR="$T/hb"
AGENT_USER="harness-probe-nouser"; SCRIPT_DIR="$SCRIPT_DIR_PKG"
DONE_LIST=(); CHECKED_LIST=(); FAILED_LIST=(); MANUAL_LIST=()
шаг_13 > "$T/step13.out" 2>&1
RUN
: > "$t/ran.log"; : > "$t/did.log"
SCRIPT_DIR_PKG="$(dirname "$here")" timeout 300 bash "$t/run13.sh" "$t/lib.sh" "$t"
n_ran=$(grep -c . "$t/ran.log")
[ "$n_ran" -ge 10 ] && ok "А2 условие: шаг 13 прогнал демонов: $n_ran" \
  || bad "А2 условие: шаг 13 прогнал демонов меньше 10 ($n_ran) — список не разобран"
runs_dirs=$(awk '{print $2}' "$t/ran.log" | sort -u)
if [ "$(grep -c . <<<"$runs_dirs")" = 1 ] && [ -n "$runs_dirs" ] && [ "$runs_dirs" != "$t/instlog" ]; then
  ok "А2: каждый демон шага 13 пишет запуски в свой каталог ($runs_dirs), не в каталог журналов"
else
  bad "А2: демоны шага 13 пишут запуски в рабочий журнал (HARNESS_RUNS_DIR: «$(tr '\n' ' ' <<<"$runs_dirs")»)"
fi
grep -qF -- "install -d -o harness-probe-nouser -g harness-probe-nouser '$runs_dirs'" "$t/did.log" \
  && ok "А2: каталог журнала запусков пробы заводится агентом" \
  || bad "А2: каталог $runs_dirs не заводится агентом до прогона (install -d -o …)"
grep -qF -- "touch '$t/instlog/install-demon-probe.log' && chown harness-probe-nouser:harness-probe-nouser '$t/instlog/install-demon-probe.log'" "$t/did.log" \
  && ok "Б2: журнал прогона демонов заводится агентом до записи" \
  || bad "Б2: install-demon-probe.log пишет root, не заведя его агентом"

# ── Б1: журнал службы проверок открывает процесс агента, а не systemd ──
U="$t/units"; mkdir -p "$U/proj/scripts" "$U/logs"
cat > "$U/proj/scripts/run-background.sh" <<'FAKE'
#!/bin/sh
echo "прогон $1"
FAKE
chmod +x "$U/proj/scripts/run-background.sh"
units_out=$(HARNESS_PROCESS_LIBRARY="$here/config/process-library.yaml" \
  HARNESS_PROCESS_LIBRARY_JSON="$here/config/process-library.json" \
  HARNESS_PROCESS_LIBRARY_LOCAL="$U/no-local-layer.json" \
  python3 -I - "$plib" "$here/scripts/lib" "$U" <<'PY' 2>&1
import importlib.util, os, shlex, subprocess, sys
plib, lib, U = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, lib)
spec = importlib.util.spec_from_file_location("process_library_under_test", plib)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
conf = {"CHECKS_CPU_WEIGHT": "idle", "CHECKS_CPU_QUOTA": "200%", "CHECKS_MEMORY_HIGH": "2G", "CHECKS_MEMORY_MAX": "3G"}
units = m.checks_units(f"{U}/proj", f"{U}/logs", "agent", conf=conf)
library = m.load().get("процессы") or {}
services = {n: s for n, s in units.items() if n.endswith(".service")}
print(f"условие служб {len(services)}")
for name, text in services.items():
    pid = name[len("harness-"):-len(".service")]
    journal = f"{U}/logs/{library[pid]['журнал']}"
    lines = text.splitlines()
    for line in lines:
        if line.startswith(("StandardOutput=", "StandardError=")) and line.split("=", 1)[1].startswith(("append:", "file:", "truncate:")):
            print(f"systemd {name}: {line}")
    starts = [line.split("=", 1)[1] for line in lines if line.startswith("ExecStart=")]
    if len(starts) != 1:
        print(f"нет ExecStart {name}"); continue
    if "$" in starts[0] or "%" in starts[0]:
        print(f"подстановка {name}: в ExecStart «$» или «%» — systemd подставит их сам")
    subprocess.run(shlex.split(starts[0]), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=60)
    try:
        body, owner = open(journal, encoding="utf-8").read(), os.stat(journal).st_uid
    except OSError:
        print(f"не открыт {name}: ExecStart не пишет {journal}"); continue
    if f"прогон {pid}" not in body or owner != os.getuid():
        print(f"не открыт {name}: в {journal} нет строки прогона или чужой владелец")
PY
)
n_services=$(sed -n 's/^условие служб \([0-9]*\)$/\1/p' <<<"$units_out")
[ "${n_services:-0}" -ge 2 ] && ok "Б1 условие: юнитов службы проверок $n_services" \
  || bad "Б1 условие: юниты службы проверок не порождены: $(head -3 <<<"$units_out")"
grep -q '^systemd ' <<<"$units_out" \
  && bad "Б1: журнал юнита открывает systemd до смены пользователя (новый файл — root): $(grep '^systemd ' <<<"$units_out" | head -2 | tr '\n' ';')" \
  || ok "Б1: ни один юнит службы проверок не отдаёт файл журнала systemd"
grep -qE '^(не открыт|нет ExecStart|подстановка) ' <<<"$units_out" \
  && bad "Б1: команда юнита сама не ведёт журнал задания: $(grep -E '^(не открыт|нет ExecStart|подстановка) ' <<<"$units_out" | head -2 | tr '\n' ';')" \
  || ok "Б1: команда каждого юнита дописывает журнал задания от своего пользователя"

# ── Б2: журналы установщика — агентские; прежние root-овые возвращаются агенту ──
want="touch '$log_dir/install-rollout.log' && chown $agent:$agent '$log_dir/install-rollout.log'"
l_own=$(first_line "$want" "$fresh"); l_tee=$(first_line "tee '$log_dir/install-rollout.log'" "$fresh")
[ -n "$l_own" ] && [ -n "$l_tee" ] && [ "$l_own" -lt "$l_tee" ] \
  && ok "Б2: журнал раскладки заводится агентом до записи root-ом" \
  || bad "Б2: install-rollout.log пишет root (tee), не заведя его агентом (строки: агент ${l_own:-нет}, tee ${l_tee:-нет})"
repair="find '$log_dir' -maxdepth 1 -name '*.log' ! -name install.log -user root -exec chown $agent:$agent {} +"
for mode in fresh resumed; do
  grep -qF -- "$repair" <<<"${!mode}" \
    && ok "Б2 ($mode): шаг 1б возвращает агенту root-овые журналы" \
    || bad "Б2 ($mode): шаг 1б не возвращает агенту журналы, которые прежняя установка завела root-ом"
done

# ── В: читатель держателей уборки /tmp — root-овая копия и ровно её правило ──
reader_install="install -o root -g root -m 0755 '$here/scripts/tmp-sweep.py' /usr/local/lib/harness/tmp-sweep-holders"
for mode in fresh resumed; do
  out="${!mode}"; out="${out//$t\/pkg\/harness/$here}"
  grep -qF -- "$reader_install" <<<"$out" \
    && ok "В ($mode): шаг 1б ставит root-овую копию читателя держателей" \
    || bad "В ($mode): root-овой копии читателя держателей нет (/usr/local/lib/harness/tmp-sweep-holders)"
done
rule=$(agent_sudoers "$fresh" | sed -n "s/^$agent ALL=(root) NOPASSWD: \(.*tmp-sweep-holders.*\)$/\1/p" | head -1)
[ -n "$rule" ] && ok "В: в 50-agent есть правило читателя держателей ($rule)" \
  || bad "В: в /etc/sudoers.d/50-agent нет правила на читатель держателей — уборка /tmp мертва"
mkdir -p "$t/sudo-bin"
printf '#!/bin/sh\nprintf "%%s\\n" "$*" > "%s/sudo-args"\nexit 1\n' "$t" > "$t/sudo-bin/sudo"
chmod +x "$t/sudo-bin/sudo"
env -u HARNESS_HOLDERS_READER PATH="$t/sudo-bin:$PATH" python3 -I - "$sweep" <<'PY' >/dev/null 2>&1
import importlib.util, sys
spec = importlib.util.spec_from_file_location("tmp_sweep_under_test", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.read_holders()
PY
asked=$(cat "$t/sudo-args" 2>/dev/null)
[ -n "$rule" ] && [ "$asked" = "-n $rule" ] \
  && ok "В: уборка зовёт sudo ровно той командой, что разрешена правилом" \
  || bad "В: уборка зовёт «sudo $asked», а правило разрешает «${rule:-нет правила}» — sudo откажет"
cp "$sweep" "$t/tmp-sweep-holders"; chmod 755 "$t/tmp-sweep-holders"
"$t/tmp-sweep-holders" --держатели 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if "inodes" in d else 1)' \
  && ok "В: копия читателя исполняется сама по себе и отдаёт держателей JSON" \
  || bad "В: копия tmp-sweep.py, запущенная как файл, не отдаёт держателей"

# ── Г: сверка root-части при обновлении знает двери среза и держателей ──
agent_rules=$(agent_sudoers "$fresh")
[ "$(grep -c . <<<"$agent_rules")" -ge 3 ] && ok "Г условие: правила 50-agent разобраны: $(grep -c . <<<"$agent_rules")" \
  || bad "Г условие: правила 50-agent из сухого прогона не разобраны"
gaps_out=$(AGENT_RULES="$agent_rules" python3 -I - "$upd" "$here/scripts" "$t/upd" <<'PY' 2>&1
import importlib.util, os, sys
from pathlib import Path
upd, scripts, stand = sys.argv[1], sys.argv[2], Path(sys.argv[3])
sys.path.insert(0, scripts); sys.path.insert(0, scripts + "/lib")
spec = importlib.util.spec_from_file_location("update_under_test", upd)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
agent_rules = os.environ["AGENT_RULES"]
units = ", ".join(f"/usr/bin/systemctl {a} {u}" for u in m.RESTARTED_UNITS for a in ("restart", "start", "stop", "status"))
def gaps(drop):
    etc = stand / drop.replace("/", "_")
    (etc / "harness").mkdir(parents=True); (etc / "logrotate.d").mkdir()
    (etc / "logrotate.d" / "harness").write_text("x\n", encoding="utf-8")
    kept = "\n".join(line for line in agent_rules.splitlines() if not drop or drop not in line)
    (etc / "harness" / "sudo-rights.txt").write_text(
        "# файл: /etc/sudoers.d/50-agent\n" + kept + "\n"
        "# файл: /etc/sudoers.d/harness-agent-readonly\nagent ALL=(root) NOPASSWD: /usr/bin/ss -ltnp\n"
        f"# файл: /etc/sudoers.d/harness-agent-units\nagent ALL=(root) NOPASSWD: {units}\n", encoding="utf-8")
    return m.root_part_gaps(etc=etc, enabled=lambda unit: True)
print("всё", gaps(""))
for door in ("/usr/local/sbin/harness-run-in-checks", "/usr/local/lib/harness/tmp-sweep-holders"):
    got = gaps(door)
    print("без", door, "назван" if len(got) == 1 and door in got[0] else f"не назван {got!r}")
PY
)
grep -qx 'всё \[\]' <<<"$gaps_out" && ok "Г: все правила установщика на месте — сверка молчит" \
  || bad "Г: правила установщика на месте, а сверка называет пробелы: $(head -3 <<<"$gaps_out" | tr '\n' ';')"
for door in /usr/local/sbin/harness-run-in-checks /usr/local/lib/harness/tmp-sweep-holders; do
  grep -qx "без $door назван" <<<"$gaps_out" \
    && ok "Г: нет правила на $door — сверка называет его" \
    || bad "Г: нет правила на $door — обновление молчит: $(grep "^без $door" <<<"$gaps_out")"
done

echo "итого случаев $total, неудач $fail"
[ "$fail" = 0 ]
