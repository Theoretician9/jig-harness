#!/usr/bin/env bash
# Проба: строка итога ворот (ворота-итог.jsonl) называет, ЧТО проверено:
# `sha` — HEAD дерева ворот и `чисто` — было ли дерево чистым (true/false).
#
# Откуда взята. Спека эталонного выката §3 строка 14, §3а, фаза 7:
# полные ворота (медиана 72 мин) перед публикацией гоняются второй раз —
# publish.sh замок 1 не может знать, что то же дерево уже прошло, потому что в
# итоге нет ни sha, ни чистоты дерева. Без этих полей пропуск повтора судить нечем.
#
# Контракт, который судится:
#   * КАЖДАЯ строка итога gates.sh (путь --хакатон и основной путь, в том числе
#     --только) несёт "sha" — полный `git rev-parse HEAD` корня ворот — и
#     "чисто" — JSON true/false: `git status --porcelain` корня пуст;
#   * оба снимаются в НАЧАЛЕ прогона: коммит или файл, появившиеся за время
#     ворот, проверены не были. Иначе публикация пропустила бы ворота на sha,
#     который ворота не видели (больной случай 4).
#   * прочие поля строки (режим, зелёных, красных) остаются;
#   * "код" — КОД ВЫХОДА этого прогона gates.sh (число). Основной путь выходит
#     кодом суда карантина (новые + беды карантина), а не числом красных: при
#     «красных 0, новых 0» код бывает ненулевым (ревью 7а: RC=26 на стенде). Замок
#     публикации верит строке итога — без кода он пропустил бы ворота с кодом > 0;
#   * "чисто" — true, только если дерево чисто в НАЧАЛЕ и в КОНЦЕ прогона и HEAD
#     конца тот же, что в начале: проверка, создавшая файл или сделавшая коммит
#     посреди ворот, оставляет итог, который не описывает проверенное дерево.
#     Исключение (спека тяжёлых прогонов, Ж): правки и коммиты, где только
#     добавленные строки в классе DAEMON_OWNED_PATHS под память/, «чисто» не снимают
#     ни в начале, ни в конце — судит service-only-diff.py (случаи Ж ниже).
#
# Стенд свой: копия отслеживаемых файлов scripts/ и harness/ в tmp-репозитории,
# свой gates-hackathon.yaml с одной проверкой, свой LOG_DIR. Полные ворота не
# запускаются: основной путь — `--только` с именем, которого нет (ноль проверок).
# Боевые журналы не трогаются.
# Дверь мутаций: GATES_SUMMARY_PROBE_SRC — корень судимого дерева (копия проекта).
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "${GATES_SUMMARY_PROBE_SRC:-$HERE/..}" && pwd)"
# shellcheck disable=SC1091
source "$HERE/lib/selftest_cases.sh"

STAND=$(mktemp -d -t proba-gates-summary-XXXXXX)
trap 'rm -rf "$STAND"' EXIT
TREE="$STAND/tree"
LOGS="$STAND/log"
mkdir -p "$TREE" "$LOGS"
if git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
    (cd "$SRC" && git ls-files -z --cached --others --exclude-standard -- scripts harness .gitignore \
        | tar --null -T - -cf -) | tar -xf - -C "$TREE"
else
    cp -r "$SRC/scripts" "$SRC/harness" "$TREE/"
    [ -f "$SRC/.gitignore" ] && cp "$SRC/.gitignore" "$TREE/"
fi
stand_git() { GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
              git -C "$TREE" -c user.name=proba -c user.email=p@p "$@"; }
# Одна проверка (её тело — stand-during.sh в корне стенда); MOVE_DURING — коммит ПРАВКИ
# КОДА посреди прогона (больной случай 4; пустой коммит Ж счёл бы служебным — разница
# пуста), SERVICE_DURING — коммит добавленной строки служебной отметки посреди прогона,
# SERVICE_DIRTY_DURING — та же строка, но без коммита, CLEAN_DURING — неотслеживаемый
# файл посреди прогона, FAIL_DURING — красная.
cat > "$TREE/harness/config/gates-hackathon.yaml" <<'EOF'
checks:
  - name: стенд
    cmd: 'bash stand-during.sh'
EOF
cat > "$TREE/stand-during.sh" <<'EOF'
sg() { GIT_CONFIG_GLOBAL=/dev/null git -c user.name=p -c user.email=p@p "$@"; }
if [ -n "${MOVE_DURING:-}" ]; then
    echo "правка $(date +%s%N)" >> scripts/moved_during.txt
    sg add scripts/moved_during.txt; sg commit -q -m "код посреди ворот"
fi
if [ -n "${SERVICE_DURING:-}" ]; then
    echo "отметка $(date +%s%N)" >> память/.tasks-triaged
    sg add память/.tasks-triaged; sg commit -q -m "служебная отметка посреди ворот"
fi
[ -z "${SERVICE_DIRTY_DURING:-}" ] || echo "отметка $(date +%s%N)" >> память/.tasks-triaged
[ -z "${CLEAN_DURING:-}" ] || echo посреди > during_gates.txt
echo стенд
[ -z "${FAIL_DURING:-}" ]
EOF
mkdir -p "$TREE/память"
printf 'отметка 0\n' > "$TREE/память/.tasks-triaged"
# Карантин стенда: одна запись на карточку, которой нет в карте, — беда карантина.
# Основной путь при нуле красных выходит кодом 1 (новых 0, бед 1): то самое
# «красных 0, новых 0, код > 0», которое замок публикации обязан видеть.
TODAY=$(date +%F); DUE=$(date -d '+3 days' +%F)
cat > "$TREE/harness/config/gates-quarantine.yaml" <<EOF
карантин:
  - гейт: "нет-такой-проверки"
    карточка: hr.not-in-stand-map
    заведено: "$TODAY"
    срок: "$DUE"
    причина: "беда карантина стенда"
EOF
# Карта-образец: суд карантина основного пути читает её из корня ворот.
cat > "$TREE/dev-map.yaml" <<EOF
version: 1
queue: []
layers:
  - { id: infra, label: "Инфра", color: "#5aa06a" }
lines:
  - id: core
    layer: infra
    name: "Ядро"
    tasks:
      - id: hr.proba
        status: wip
        title: "Карта стенда"
        summary: "образец пробы итога ворот"
EOF
stand_git init -q
# Байткод, который питон ворот пишет посреди прогона, — не правка дерева. Свой
# exclude стенда, а не .gitignore источника: копия прибора мутаций везёт только
# scripts/ и harness/, без .gitignore, и стенд иначе грязен до мутации.
printf '__pycache__/\n*.pyc\n' >> "$TREE/.git/info/exclude"
stand_git add -A
stand_git commit -qm "стенд итога ворот"

GATES_OUT=""
GATES_RC=""
run_gates() {  # доводы gates.sh; окружение — через env перед вызовом; код — в GATES_RC
    GATES_OUT=$(cd "$TREE" && LOG_DIR="$LOGS" GIT_CONFIG_GLOBAL=/dev/null PYTHONDONTWRITEBYTECODE=1 \
                timeout 400 bash "$TREE/scripts/gates.sh" "$@" 2>&1)
    GATES_RC=$?
}
last() {  # $1 = поле → значение из последней строки итога (JSON для не-строк)
    python3 - "$LOGS/ворота-итог.jsonl" "$1" <<'PY'
import json, sys
from pathlib import Path
path, key = Path(sys.argv[1]), sys.argv[2]
rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []
if not rows:
    print("нет-строки"); sys.exit()
value = rows[-1].get(key, "нет-поля")
print(value if isinstance(value, str) else json.dumps(value))
PY
}
summary_lines() { local n=0; [ -f "$LOGS/ворота-итог.jsonl" ] && n=$(grep -c . "$LOGS/ворота-итог.jsonl"); echo "${n:-0}"; }
EXTRA="$TREE/scripts/extra_file.txt"

echo "── 1. --хакатон, чистое дерево ──"
HEAD0=$(stand_git rev-parse HEAD)
n0=$(summary_lines); run_gates --хакатон
case_check "строка итога дописана" "$((n0 + 1))" "$(summary_lines)"
case_check "режим хакатон (поле осталось)" "хакатон" "$(last режим)"
case_check "sha = полный HEAD стенда" "$HEAD0" "$(last sha)"
case_check "чисто = true" "true" "$(last чисто)"
case_check "код = код выхода ворот (зелёный прогон, 0)" "$GATES_RC" "$(last код)"

echo "── 1б. --хакатон, красная проверка ──"
FAIL_DURING=1 run_gates --хакатон
case_check "стенд: красная проверка даёт код 1" "1" "$GATES_RC"
case_check "код = код выхода ворот (красный прогон)" "$GATES_RC" "$(last код)"

echo "── 2. --хакатон, неотслеживаемый файл ──"
printf 'лишний\n' > "$EXTRA"
run_gates --хакатон
case_check "чисто = false (неотслеживаемый файл)" "false" "$(last чисто)"
case_check "sha = HEAD и при грязном дереве" "$HEAD0" "$(last sha)"
rm -f "$EXTRA"

echo "── 3. --хакатон, правка отслеживаемого файла ──"
cp "$TREE/harness/config/gates-hackathon.yaml" "$STAND/hackathon.keep"
printf '# правка стенда\n' >> "$TREE/harness/config/gates-hackathon.yaml"
run_gates --хакатон
case_check "чисто = false (правка отслеживаемого)" "false" "$(last чисто)"
cp "$STAND/hackathon.keep" "$TREE/harness/config/gates-hackathon.yaml"
case_check "стенд снова чист" "" "$(stand_git status --porcelain)"

echo "── 4. БОЛЬНОЙ СЛУЧАЙ: коммит посреди ворот ──"
HEAD_BEFORE=$(stand_git rev-parse HEAD)
MOVE_DURING=1 run_gates --хакатон
HEAD_AFTER=$(stand_git rev-parse HEAD)
case_check "стенд: HEAD сдвинулся за прогон" "да" "$([ "$HEAD_BEFORE" != "$HEAD_AFTER" ] && echo да || echo нет)"
case_check "sha = HEAD НАЧАЛА прогона, не конца" "$HEAD_BEFORE" "$(last sha)"
case_check "стенд: дерево в конце чисто (коммит не оставил правок)" "" "$(stand_git status --porcelain)"
case_check "чисто = false: HEAD конца не тот, что в начале" "false" "$(last чисто)"

echo "── 4б. проверка оставила файл посреди ворот ──"
case_check "стенд перед прогоном чист" "" "$(stand_git status --porcelain)"
CLEAN_DURING=1 run_gates --хакатон
case_check "стенд: файл появился за прогон" "да" "$([ -f "$TREE/during_gates.txt" ] && echo да || echo нет)"
case_check "чисто = false: дерево грязное в конце" "false" "$(last чисто)"
rm -f "$TREE/during_gates.txt"

echo "── 5. основной путь (--только, ноль проверок), чистое дерево ──"
HEAD5=$(stand_git rev-parse HEAD)
case_check "стенд перед прогоном чист" "" "$(stand_git status --porcelain)"
n5=$(summary_lines); run_gates --только "нет-такой-проверки"
case_check "строка итога основного пути дописана" "$((n5 + 1))" "$(summary_lines)"
case_check "режим выборка" "выборка" "$(last режим)"
case_check "sha = полный HEAD стенда" "$HEAD5" "$(last sha)"
case_check "чисто = true (дерево было чистым в начале)" "true" "$(last чисто)"
case_check "поле красных на месте" "0" "$(last красных)"
case_check "поле новых: 0" "0" "$(last новых)"
case_check "стенд: код выхода ненулевой при красных 0 (беда карантина)" "да" \
    "$([ "$GATES_RC" -gt 0 ] 2>/dev/null && echo да || echo "нет: $GATES_RC")"
case_check "код = код выхода ворот, а не число красных" "$GATES_RC" "$(last код)"

echo "── 6. основной путь, грязное дерево ──"
printf 'лишний\n' > "$EXTRA"
run_gates --только "нет-такой-проверки"
case_check "чисто = false" "false" "$(last чисто)"
rm -f "$EXTRA"

echo "── 7. Ж: служебные отметки «чисто» не снимают (спека тяжёлых прогонов, Ж; ревью ЕЖ п. 3) ──"
# Контракт Ж: старт и конец судит один судья — service-only-diff.py в режиме рабочего
# дерева (`<sha начала> --под память/`): сдвиг HEAD и правки, где только добавленные
# строки в классе DAEMON_OWNED_PATHS, «чисто» не снимают; правка кода, удалённая
# строка отметки — снимают. Класс — harness.conf стенда (копия конфига машины с ключом
# стенда), дверь HARNESS_CONF; судья — тот, что лежит в scripts/ судимого дерева.
MACHINE_CONF=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import config; print(config.путь_harness_conf())' "$TREE/scripts/lib" 2>/dev/null || true)
{ grep -v '^DAEMON_OWNED_PATHS=' "$MACHINE_CONF" 2>/dev/null || true
  echo 'DAEMON_OWNED_PATHS="память/.tasks-triaged память/.orders-processed память/.details-triaged память/.указания-разобрано docs/приёмка/"'
} > "$STAND/harness.conf"
restore_marks() {  # вернуть отметку к закоммиченному виду (без git checkout стенда)
    stand_git show HEAD:память/.tasks-triaged > "$TREE/память/.tasks-triaged"
    rm -f "$TREE/память/.details-triaged"
}
zh_run() { HARNESS_CONF="$STAND/harness.conf" run_gates "$@"; }
# Основной путь (случаи 5–6) собрал производные документы docs/ (их нет на свежем стенде):
# неотслеженные, они грязнят СТАРТ каждого следующего прогона, и «false» случаев Ж
# говорил бы о них, а не о судимом. Кладём их в стенд коммитом.
stand_git add -A -- docs 2>/dev/null
stand_git diff --cached --quiet || stand_git commit -qm "производные документы стенда"

case_check "стенд перед Ж чист" "" "$(stand_git status --porcelain)"
HEAD7=$(stand_git rev-parse HEAD)
SERVICE_DURING=1 zh_run --хакатон
case_check "Ж1 стенд: HEAD сдвинулся за прогон" "да" "$([ "$HEAD7" != "$(stand_git rev-parse HEAD)" ] && echo да || echo нет)"
case_check "Ж1 sha = HEAD начала" "$HEAD7" "$(last sha)"
case_check "Ж1 служебный коммит посреди прогона → чисто = true" "true" "$(last чисто)"

MOVE_DURING=1 zh_run --хакатон
case_check "Ж2 коммит кода посреди прогона (с классом и судьёй) → чисто = false" "false" "$(last чисто)"

case_check "стенд перед Ж3 чист" "" "$(stand_git status --porcelain)"
SERVICE_DIRTY_DURING=1 zh_run --хакатон
case_check "Ж3 отметка дописана посреди прогона без коммита → чисто = true" "true" "$(last чисто)"
restore_marks

printf 'отметка до старта\n' >> "$TREE/память/.tasks-triaged"
zh_run --хакатон
case_check "Ж4 отметка дописана ДО старта ворот → чисто = true" "true" "$(last чисто)"
restore_marks

sed -i '1d' "$TREE/память/.tasks-triaged"
zh_run --хакатон
case_check "Ж5 строка отметки удалена ДО старта ворот → чисто = false" "false" "$(last чисто)"
restore_marks

printf 'новая отметка\n' > "$TREE/память/.details-triaged"
zh_run --хакатон
case_check "Ж6 новый неотслеженный файл класса до старта → чисто = true" "true" "$(last чисто)"
restore_marks

printf 'отметка до старта\n' >> "$TREE/память/.tasks-triaged"
printf 'лишний\n' > "$EXTRA"
zh_run --хакатон
case_check "Ж7 отметка плюс неотслеженный файл вне класса до старта → чисто = false" "false" "$(last чисто)"
rm -f "$EXTRA"; restore_marks

printf 'отметка до старта\n' >> "$TREE/память/.tasks-triaged"
zh_run --только "нет-такой-проверки"
case_check "Ж8 основной путь: отметка дописана до старта → чисто = true" "true" "$(last чисто)"
restore_marks
case_check "стенд после Ж чист" "" "$(stand_git status --porcelain)"

[ "$CASES_PATHS" -gt 0 ] || { echo "FAIL: ни одного случая"; exit 1; }
[ "$CASES_FAILED" -gt 0 ] && printf '%s\n' "$GATES_OUT" | tail -5 | sed 's/^/      последний прогон: /'
echo "SELFTEST: $([ "$CASES_FAILED" = 0 ] && echo зелёный || echo КРАСНЫЙ) ($CASES_PATHS проверок, неудач $CASES_FAILED)"
[ "$CASES_FAILED" = 0 ]
