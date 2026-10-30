#!/usr/bin/env bash
# Проба-контракт: схема карты знает уровень «хакатон» и долг «до продакшна».
#
# Исполнитель C. Файл: scripts/pre-commit-gate.sh. Спека:
# docs/specs/2026-10-28-режим-хакатон.md («Пока долг открыт, статус done запрещён»).
#
# КОНТРАКТ:
#   * УРОВНИ валидатора схемы (DEVMAP_VALIDATOR) — плюс «хакатон»:
#     карточка с глубина.уровень: хакатон схему проходит.
#   * После валидатора схемы гейт зовёт ОДНУ правду о долге —
#       python3 "<каталог самого pre-commit-gate.sh>/hackathon.py" --проверить-карту <карта>
#     (каталог — dirname от readlink -f скрипта, как у lib/config.sh), и её
#     код 1 красит гейт; вывод hackathon.py показывается как есть. Своей копии
#     правил о долге в гейте нет. Так в режиме --devmap-only <карта> И в
#     обычном pre-commit (карта из индекса, $STAGE_TMP/dev-map.yaml).
#   * Красный по долгу называет id долга (<id>-prod-debt) и слово «долг»;
#     карточка хакатона без долга — подсказка «hackathon.py --включить-задачу».
#     (Тексты печатает hackathon.py --проверить-карту — исполнитель A2; гейт их
#     не переписывает. Если A2 печатает иначе — правится hackathon.py.)
#   * Долг закрыт — статус в cards.CLOSED_STATUSES (done, принято). Долг в
#     test ОТКРЫТ: карточка хакатона в done при таком долге — красный.
#
#   * Обычный pre-commit (карта в индексе): тот же devmap_check над копией
#     карты из индекса, и его отказ валит коммит — «GATE FAIL: devmap_schema».
#     Код возврата hackathon.py не глотается ни в devmap_check («; true»),
#     ни в строке вызова («|| true»).
#
# Случаи: уровень допустим; done при открытом долге — красный; без долга —
# красный с подсказкой; задача в test при долге в test — зелёный; done при
# долге в принято/done — зелёный; гейт зовёт hackathon.py (подменой на
# заглушку в копии scripts/); обычная карта — как прежде (НЕИЗМЕННО, зелёные
# и до кода).
#
# Стенд свой: tmp-каталог, свои карты, LOG_DIR. Боевая dev-map.yaml не
# читается и не пишется. Пульт характеров валидатор берёт из cwd — проба
# запускает гейт из корня предмета (только чтение).
# Дверь мутаций: HACKATHON_PROBE_SRC — корень судимого дерева (копия проекта).
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "${HACKATHON_PROBE_SRC:-$HERE/..}" && pwd)"
# shellcheck disable=SC1091
source "$HERE/lib/selftest_cases.sh"

STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT
mkdir -p "$STAND/log"
TODAY=$(date +%F)

card() {  # $1=id $2=status $3=уровень или пусто, дальше — строки сверх
    local id="$1" status="$2" level="$3"
    shift 3
    printf '      - id: %s\n        status: %s\n        title: "Задача %s"\n        summary: "образец пробы"\n' \
        "$id" "$status" "$id"
    [ -n "$level" ] && printf '        глубина: { уровень: %s, характер: починка, кем: смена, когда: "%s" }\n' \
        "$level" "$TODAY"
    local row
    for row in "$@"; do printf '        %s\n' "$row"; done
}
DONE_ROW="tested_at: \"$TODAY\""
ACCEPTED_ROWS=('проверка: { команда: "true" }' "принято_когда: \"$TODAY\"")

write_map() {  # $1=путь, stdin — карточки
    {
        printf 'version: 1\nqueue: []\nlayers:\n  - { id: infra, label: "Инфра", color: "#5aa06a" }\n'
        printf 'lines:\n  - id: core\n    layer: infra\n    name: "Ядро"\n    tasks:\n'
        cat
    } > "$1"
}

OUT=""
RC=0
gate() {  # $1=каталог scripts/ (предмет или копия), $2=карта
    OUT=$(cd "$SRC" && LOG_DIR="$STAND/log" timeout 120 bash "$1/pre-commit-gate.sh" --devmap-only "$2" 2>&1)
    RC=$?
}
says() { printf '%s' "$OUT" | grep -qF -- "$1" && echo да || echo нет; }

echo "── 1. уровень «хакатон» допустим в схеме (долг открыт, задача в работе) ──"
{ card hr.h wip хакатон; card hr.h-prod-debt plan ""; } | write_map "$STAND/ok.yaml"
gate "$SRC/scripts" "$STAND/ok.yaml"
case_check "код 0" "0" "$RC"
case_check "схема не судит уровень «хакатон» неизвестным" "нет" "$(says 'глубина.уровень «хакатон»')"

echo "── 2. БОЛЬНОЙ СЛУЧАЙ: done при открытом долге (долг в test) — красный ──"
{ card hr.h done хакатон "$DONE_ROW"; card hr.h-prod-debt test ""; } | write_map "$STAND/done-open.yaml"
gate "$SRC/scripts" "$STAND/done-open.yaml"
case_check "код ненулевой, красный называет долг hr.h-prod-debt" "да" \
    "$([ "$RC" != 0 ] && [ "$(says 'hr.h-prod-debt')" = да ] && echo да || echo нет)"
case_check "красный говорит слово «долг»" "да" "$(says 'долг')"

echo "── 3. БОЛЬНОЙ СЛУЧАЙ: карточка хакатона без долга — красный с подсказкой ──"
card hr.h test хакатон | write_map "$STAND/no-debt.yaml"
gate "$SRC/scripts" "$STAND/no-debt.yaml"
case_check "код ненулевой, подсказка «hackathon.py --включить-задачу»" "да" \
    "$([ "$RC" != 0 ] && [ "$(says 'hackathon.py --включить-задачу')" = да ] && echo да || echo нет)"
case_check "подсказка называет задачу: «--включить-задачу hr.h»" "да" "$(says '--включить-задачу hr.h')"

echo "── 4. задача в test, долг в test — зелёный (работает, не продакшн) ──"
{ card hr.h test хакатон; card hr.h-prod-debt test ""; } | write_map "$STAND/test-test.yaml"
gate "$SRC/scripts" "$STAND/test-test.yaml"
case_check "код 0" "0" "$RC"

echo "── 5. done при долге в «принято» — зелёный ──"
{ card hr.h done хакатон "$DONE_ROW"; card hr.h-prod-debt принято "" "${ACCEPTED_ROWS[@]}"; } \
    | write_map "$STAND/done-accepted.yaml"
gate "$SRC/scripts" "$STAND/done-accepted.yaml"
case_check "код 0" "0" "$RC"

echo "── 6. done при долге в done — зелёный ──"
{ card hr.h done хакатон "$DONE_ROW"; card hr.h-prod-debt done "" "$DONE_ROW"; } \
    | write_map "$STAND/done-done.yaml"
gate "$SRC/scripts" "$STAND/done-done.yaml"
case_check "код 0" "0" "$RC"

echo "── 7. одна правда: гейт зовёт hackathon.py --проверить-карту (заглушка в копии scripts/) ──"
COPY="$STAND/scripts"
mkdir -p "$COPY"
cp "$SRC/scripts/pre-commit-gate.sh" "$COPY/"
cp -r "$SRC/scripts/lib" "$COPY/"
cat > "$COPY/hackathon.py" <<'PY'
import os, sys
with open(os.environ["STUB_JOURNAL"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
print("ZAGLUSHKA-SKAZALA-" + os.environ["STUB_ANSWER"])
sys.exit(int(os.environ["STUB_ANSWER"]))
PY
export STUB_JOURNAL="$STAND/stub.log"
export STUB_ANSWER=1
gate "$COPY" "$STAND/ok.yaml"
case_check "заглушка ответила 1 на здоровую карту — гейт красный, её вывод показан как есть" "да" \
    "$([ "$RC" != 0 ] && [ "$(says 'ZAGLUSHKA-SKAZALA-1')" = да ] && echo да || echo нет)"
case_check "заглушку позвали с --проверить-карту <карта>" "да" \
    "$(grep -qxF -- "--проверить-карту $STAND/ok.yaml" "$STAND/stub.log" 2>/dev/null && echo да || echo нет)"
export STUB_ANSWER=0
gate "$COPY" "$STAND/no-debt.yaml"
case_check "заглушка ответила 0 на карту без долга — гейт зелёный (своей копии правил нет)" "0" "$RC"

echo "── 8. НЕИЗМЕННО: обычная карта судится как прежде ──"
{ card hr.o done обычная "$DONE_ROW"; card hr.p plan мелкая; } | write_map "$STAND/plain.yaml"
gate "$SRC/scripts" "$STAND/plain.yaml"
case_check "НЕИЗМЕННО: обычная карта — код 0" "0" "$RC"
card hr.o plan несуществующий | write_map "$STAND/bad-level.yaml"
gate "$SRC/scripts" "$STAND/bad-level.yaml"
case_check "НЕИЗМЕННО: выдуманный уровень по-прежнему красный" "да" \
    "$([ "$RC" != 0 ] && [ "$(says 'глубина.уровень')" = да ] && echo да || echo нет)"

echo "── 9. БОЛЬНОЙ СЛУЧАЙ: обычный pre-commit, в индексе карта с хакатоном без долга — отказ ──"
# Временный репозиторий: копия scripts/ и harness/ судимого дерева, коммит
# карты-образца, затем в индекс — та же карта плюс карточка хакатона без
# долга. Гейт идёт целиком, как у коммита; отказ обязан прийти с шага схемы
# карты. Файлы контекста — заглушки стенда: в копии мутационной пробы корня
# проекта нет, а гигиена контекста без них валит коммит раньше карты.
REPO="$STAND/repo"
mkdir -p "$REPO/память"
if git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
    (cd "$SRC" && git ls-files -z --cached -- scripts harness .gitignore | tar --null -T - -cf -) | tar -xf - -C "$REPO"
else
    (cd "$SRC" && tar -cf - scripts harness) | tar -xf - -C "$REPO"
fi
printf '# CLAUDE.md стенда\n\n@STATE.md\n@./память/ORDERS.md\n@./память/MEMORY.md\n' > "$REPO/CLAUDE.md"
printf '# STATE стенда\n' > "$REPO/STATE.md"
printf '# ORDERS стенда\n' > "$REPO/память/ORDERS.md"
printf '# MEMORY стенда\n' > "$REPO/память/MEMORY.md"
repo_git() { GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
             git -C "$REPO" -c user.name=proba -c user.email=p@p "$@"; }
card hr.o plan "" | write_map "$REPO/dev-map.yaml"
repo_git init -q
repo_git add -A
repo_git commit -qm "стенд pre-commit" --no-verify
{ card hr.o plan ""; card hr.h test хакатон; } | write_map "$REPO/dev-map.yaml"
repo_git add dev-map.yaml
OUT=$(cd "$REPO" && LOG_DIR="$STAND/log" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
      timeout 400 bash scripts/pre-commit-gate.sh 2>&1)
RC=$?
printf '%s\n' "$OUT" | tail -4 | sed 's/^/      /'
case_check "условие: гейт дошёл до схемы карты (валидатор схемы сказал своё)" "да" "$(says '[dev-map] схема в порядке')"
case_check "коммит отклонён шагом карты: код ≠ 0 и «GATE FAIL: devmap_schema»" "да" \
    "$([ "$RC" != 0 ] && [ "$(says 'GATE FAIL: devmap_schema')" = да ] && echo да || echo нет)"
case_check "отказ называет задачу: «--включить-задачу hr.h»" "да" "$(says '--включить-задачу hr.h')"

if [ "$CASES_FAILED" -eq 0 ]; then
    printf '\nПРОБА ЗЕЛЁНАЯ: случаев %d\n' "$CASES_PATHS"; exit 0
fi
printf '\nПРОБА КРАСНАЯ: неудач %d из %d\n' "$CASES_FAILED" "$CASES_PATHS"; exit 1
