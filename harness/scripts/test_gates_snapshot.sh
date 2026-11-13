#!/usr/bin/env bash
# Проба прогона ворот по снимку (scripts/gates-snapshot.sh).
#
# Стенд в mktemp: свой git-репозиторий «живого дерева» с копиями ворот, своя
# установка (HARNESS_HOME: LOG_DIR, STATE_DIR, PROJECT_DIR — стенд). Ворота
# гоняются выборкой из одной проверки «секреты»: она судит индекс, и секрет,
# положенный в индекс живого дерева ПОСЛЕ запуска, обязан остаться невидимым
# снимку. Поломка, которая оставила бы пробу зелёной, — ворота судят живое
# дерево; поэтому второй случай — секрет внутри коммита: снимок обязан
# покраснеть, иначе «зелёный» значил бы «ничего не судил».
#
# Двери для мутаций: GATES_SNAPSHOT_UNDER_TEST — обёртка, GATES_UNDER_TEST —
# сами ворота (копия кладётся в стенд вместо боевой).
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
WRAPPER="${GATES_SNAPSHOT_UNDER_TEST:-$HERE/gates-snapshot.sh}"
GATES="${GATES_UNDER_TEST:-$HERE/gates.sh}"
# Две раскладки: в проекте scripts/ рядом с harness/, в пакете — harness/scripts/.
LIVE_LIST="$HERE/../harness/config/gates-live.yaml"
[ -r "$LIVE_LIST" ] || LIVE_LIST="$HERE/../config/gates-live.yaml"

T="$(mktemp -d)"
# Снимки — вне /tmp (там обёртка отказывает): свой каталог в /var/tmp, путь
# доезжает до обёртки ключом GATES_SNAPSHOT_DIR стендового harness.conf. Не в
# $HOME: дом — свойство хозяина машины, под чужим или пустым домом стенд не
# поднимался и проба судила дом, а не обёртку.
K="$(mktemp -d -p /var/tmp .gates-snapshot-probe.XXXXXX)"
SNAPS="$K/snaps"
cleanup() {
    [ -d "$T/live/.git" ] && git -C "$T/live" worktree prune 2>/dev/null
    rm -rf "$T" "$K"
}
trap cleanup EXIT

fails=0; total=0
case_check() {  # имя, условие (код 0 — прошло)
    total=$((total + 1))
    if [ "$2" = 0 ]; then echo "  ок    $1"; else echo "  ПЛОХО $1"; fails=$((fails + 1)); fi
}

# Секрет собирается здесь, а не пишется строкой: строка в этом файле красила бы
# боевой гейт секретов.
FAKE="ghp_$(printf 'A%.0s' $(seq 36))"

mkdir -p "$T/home" "$T/log" "$T/state" "$T/secrets" "$T/live/scripts" "$T/live/harness/config"
cat > "$T/home/install.conf" <<EOF
PROJECT_DIR="$T/live"
LOG_DIR="$T/log"
STATE_DIR="$T/state"
SECRETS_DIR="$T/secrets"
EOF
echo "GATES_SNAPSHOT_DIR=\"$SNAPS\"" > "$T/home/harness.conf"
export HARNESS_HOME="$T/home"
unset PROJECT_DIR LOG_DIR STATE_DIR SECRETS_DIR GATES_SNAPSHOT_DIR HARNESS_INSTALL_CONF INSTALL_CONF HARNESS_CONF

cp "$WRAPPER" "$T/live/scripts/gates-snapshot.sh"
cp "$GATES" "$T/live/scripts/gates.sh"
cp "$HERE/check-secrets.sh" "$T/live/scripts/"
cp -r "$HERE/lib" "$T/live/scripts/lib"
rm -rf "$T/live/scripts/lib/__pycache__"
cp "$LIVE_LIST" "$T/live/harness/config/gates-live.yaml"
echo "чистый файл" > "$T/live/data.txt"
git -C "$T/live" init -q
git -C "$T/live" -c user.name=t -c user.email=t@t add -A
git -C "$T/live" -c user.name=t -c user.email=t@t commit -qm init

# Ждёт последней строки «ВОРОТА-КОНЕЦ rc=N» в журнале прогона; печатает журнал.
wait_end() {  # $1 = вывод обёртки
    local log i
    log="$(sed -n 's/^журнал: //p' <<< "$1")"
    [ -n "$log" ] || { echo "нет строки «журнал:» в выводе обёртки"; return 1; }
    for i in $(seq 120); do
        tail -1 "$log" 2>/dev/null | grep -q '^ВОРОТА-КОНЕЦ rc=' && { cat "$log"; return 0; }
        sleep 0.5
    done
    echo "прогон не кончился за 60 с: $log"; cat "$log" 2>/dev/null; return 1
}

echo "── 1. правка живого дерева после запуска не меняет итог снимка"
OUT="$(bash "$T/live/scripts/gates-snapshot.sh" --только секреты 2>&1)"
# Секрет — в индекс живого дерева, уже после того как обёртка вернулась.
echo "token = $FAKE" > "$T/live/data.txt"
git -C "$T/live" add data.txt
LOG1="$(wait_end "$OUT")"
case_check "снимок не видит секрета живого индекса: «ок · секреты»" "$(grep -q 'ок · секреты' <<< "$LOG1"; echo $?)"
case_check "последняя строка журнала — ВОРОТА-КОНЕЦ rc=0" "$(tail -1 <<< "$LOG1" | grep -qx 'ВОРОТА-КОНЕЦ rc=0'; echo $?)"
case_check "выборка не выдаёт себя за полный прогон: режим «выборка» в итоге" \
    "$(tail -1 "$T/log/ворота-итог.jsonl" 2>/dev/null | grep -q '"режим":"выборка"'; echo $?)"
# Контроль стенда: живое дерево секрет действительно несёт.
case_check "контроль: живое дерево секрет несёт (гейт по нему красный)" \
    "$(bash "$T/live/scripts/check-secrets.sh" "$T/live" >/dev/null 2>&1; [ $? = 1 ]; echo $?)"

echo "── 2. снимок убран после прогона"
SNAP1="$(sed -n 's/^снимок: //p' <<< "$OUT")"
case_check "каталог снимка назван и снимок лежал в каталоге из ключа GATES_SNAPSHOT_DIR" \
    "$(case "$SNAP1" in "$SNAPS/"?*) echo 0;; *) echo 1;; esac)"
case_check "каталога снимка больше нет" "$([ -n "$SNAP1" ] && [ ! -e "$SNAP1" ]; echo $?)"
case_check "git worktree list без снимка" "$(git -C "$T/live" worktree list | grep -qF "$SNAPS"; [ $? = 1 ]; echo $?)"

echo "── 3. число проверок, судящих живое, напечатано и равно списку данных"
N_LIST="$(python3 -c 'import sys, yaml; print(len(yaml.safe_load(open(sys.argv[1]))["живое"]))' "$LIVE_LIST" 2>/dev/null)"
case_check "список данных не пуст (${N_LIST:-?})" "$([ "${N_LIST:-0}" -gt 0 ]; echo $?)"
case_check "журнал прогона печатает «судит живое: $N_LIST»" "$(grep -q "судит живое: $N_LIST " <<< "$LOG1"; echo $?)"
# Имя в списке, которого нет среди проверок ворот, — список ни о чём.
UNKNOWN="$(python3 - "$LIVE_LIST" "$GATES" <<'EOF'
import re, sys, yaml
names = {d["проверка"] for d in yaml.safe_load(open(sys.argv[1]))["живое"]}
checks = set(re.findall(r'^\s*проверка "([^"$]+)"', open(sys.argv[2]).read(), re.M))
print("ok", " | ".join(sorted(names - checks)))
EOF
)"
# «ok» впереди — знак, что разбор дошёл до конца: упавший разбор дал бы пустой
# ответ, а пустой ответ читался бы как «чужих нет».
case_check "каждое имя списка — проверка ворот (${UNKNOWN:-разбор упал})" "$([ "$UNKNOWN" = "ok " ]; echo $?)"

echo "── 4. секрет внутри коммита — снимок красный"
git -C "$T/live" -c user.name=t -c user.email=t@t commit -qm secret
OUT="$(bash "$T/live/scripts/gates-snapshot.sh" --только секреты 2>&1)"
LOG2="$(wait_end "$OUT")"
case_check "снимок с секретом в коммите: «ПЛОХО · секреты» и rc=1" \
    "$(grep -q 'ПЛОХО · секреты' <<< "$LOG2" && tail -1 <<< "$LOG2" | grep -qx 'ВОРОТА-КОНЕЦ rc=1'; echo $?)"

echo "── 5. место снимка под \$LOG_DIR — отказ, снимка нет"
OUT="$(GATES_SNAPSHOT_DIR="$T/log/snaps" bash "$T/live/scripts/gates-snapshot.sh" --только секреты 2>&1)"; RC=$?
case_check "отказ кодом 2 с причиной «под \$LOG_DIR» (код $RC)" "$([ "$RC" = 2 ] && grep -qF 'под $LOG_DIR' <<< "$OUT"; echo $?)"
case_check "каталог под \$LOG_DIR не создан, worktree не добавлен" \
    "$([ ! -e "$T/log/snaps" ] && [ "$(git -C "$T/live" worktree list | wc -l)" = 1 ]; echo $?)"
OUT="$(GATES_SNAPSHOT_DIR="$T/snaps" bash "$T/live/scripts/gates-snapshot.sh" --только секреты 2>&1)"; RC=$?
case_check "под /tmp — отказ кодом 2 с причиной «под /tmp» (код $RC)" "$([ "$RC" = 2 ] && grep -qF 'под /tmp' <<< "$OUT"; echo $?)"
case_check "каталог под /tmp не создан" "$([ ! -e "$T/snaps" ]; echo $?)"
case_check "каталог снимков из ключа закрыт от чужих (700)" "$([ "$(stat -c %a "$SNAPS")" = 700 ]; echo $?)"

echo "── 6. старые снимки (> 2 суток) убираются, свежие — нет"
git -C "$T/live" worktree add -q --detach "$SNAPS/old" HEAD
mkdir -p "$SNAPS/fresh"
touch -d '3 days ago' "$SNAPS/old"
touch -d '1 hour ago' "$SNAPS/fresh"
OUT="$(bash "$T/live/scripts/gates-snapshot.sh" --только секреты 2>&1)"
wait_end "$OUT" >/dev/null
case_check "старый снимок убран и из git worktree list" \
    "$([ ! -e "$SNAPS/old" ] && ! git -C "$T/live" worktree list | grep -q '/old '; echo $?)"
case_check "свежий каталог (идущий прогон) не тронут" "$([ -d "$SNAPS/fresh" ]; echo $?)"

echo "── 7. --убрать без прогона: старые убраны, самый свежий оставлен, даже если стар"
rm -rf "$SNAPS/fresh"
git -C "$T/live" worktree add -q --detach "$SNAPS/old-a" HEAD
git -C "$T/live" worktree add -q --detach "$SNAPS/old-b" HEAD
touch -d '5 days ago' "$SNAPS/old-a"
touch -d '3 days ago' "$SNAPS/old-b"
OUT="$(bash "$T/live/scripts/gates-snapshot.sh" --убрать 2>&1)"; RC=$?
case_check "старший снимок убран, из git worktree list тоже (код $RC)" \
    "$([ "$RC" = 0 ] && [ ! -e "$SNAPS/old-a" ] && ! git -C "$T/live" worktree list | grep -q '/old-a '; echo $?)"
case_check "самый свежий снимок оставлен, хоть и старше 2 суток" "$([ -d "$SNAPS/old-b" ]; echo $?)"
case_check "итог называет число и МБ: «снимки ворот: убрано 1, … МБ» ($OUT)" \
    "$(grep -qE '^снимки ворот: убрано 1, [0-9]+ МБ' <<< "$OUT"; echo $?)"

echo "── 8. --убрать: симлинк в каталоге снимков не проходится — цель за ним цела"
# «$d/» глоба разворачивает симлинк, и rm -rf -- "ссылка/" вычистил бы цель.
mkdir -p "$K/outside/sub" && echo данные > "$K/outside/sub/f"
touch -d '4 days ago' "$K/outside"
ln -s "$K/outside" "$SNAPS/link-c"
OUT="$(bash "$T/live/scripts/gates-snapshot.sh" --убрать 2>&1)"; RC=$?
case_check "цель симлинка цела ($OUT)" "$([ "$RC" = 0 ] && [ -f "$K/outside/sub/f" ]; echo $?)"

echo "ИТОГ: путей $total, неудач $fails"
[ "$fails" = 0 ]
