#!/usr/bin/env bash
# Проба-контракт: быстрые ворота хакатона (gates.sh --хакатон) и выкат в режиме.
#
# Исполнитель D. Файлы: scripts/gates.sh, harness/config/gates-hackathon.yaml
# (новый), scripts/deploy.sh. Спека: docs/specs/2026-10-28-режим-хакатон.md
# (строка «Ворота» и «Выкат» таблицы; И-2, И-3 не снимаются).
#
# КОНТРАКТ gates.sh --хакатон:
#   * Состав — ДАННЫЕ: harness/config/gates-hackathon.yaml, форма
#         checks:
#           - name: секреты
#             cmd: bash scripts/check-secrets.sh "$ROOT"
#     cmd исполняется из корня дерева (ROOT, как у прочих проверок) через
#     функцию «проверка» (lib/gates_check.sh): проверка "<name>" bash -c "<cmd>".
#   * Обязательные имена и чем они судят (подстрока cmd):
#       секреты        — check-secrets.sh
#       сторож команд  — test_guard_bash.py
#       имена файлов   — check-file-names.py
#       схема карты    — pre-commit-gate.sh --devmap-only
#       синтаксис      — .py/.sh, изменённые против HEAD (индекс и рабочее
#                        дерево): битый файл — красный
#   * Идут ТОЛЬКО проверки файла: строки «ок · / ПЛОХО · / жёлтый · <имя>» в
#     выводе — ровно имена из yaml, каждая один раз. Прогон ≤ 180 с.
#   * Итог в $LOG_DIR/ворота-итог.jsonl с "режим":"хакатон" (не «обычный»:
#     быстрый прогон не годится за полный). Код выхода — число красных.
#   * Список checks пуст (checks: []), ключа нет или файл не читается —
#     ОТКАЗ: код ≠ 0, ни одной строки проверки, в выводе слово «отказ».
#     Строка итога в ворота-итог.jsonl при отказе либо не пишется, либо несёт
#     красных > 0 — «зелёных 0, красных 0» за прогон не годится.
#   * «имена файлов» и «синтаксис» на ПУСТОМ наборе изменений (в выкате дерево
#     чистое и индекс пуст) — не доказательство: последней строкой вывода
#     проверка печатает «изменений нет» (её видно в строке «ок · <имя> — …»).
#     На непустом наборе этих слов в строке нет (например «проверено
#     файлов: N»): иначе «изменений нет» печаталось бы всегда и ничего не
#     различало.
#
# КОНТРАКТ deploy.sh:
#   * После сверки секретов (шаг 1а) и ДО тега и сборки:
#       python3 "$SCRIPT_DIR/hackathon.py" --активен --карта "$PROJECT_DIR/dev-map.yaml"
#     код 0 (режим жив и задача в работе — хакатона) → bash "$SCRIPT_DIR/gates.sh" --хакатон;
#     ворота красные → выкат отменён кодом 1, в выводе слово «хакатон».
#     Код ≠ 0 — выкат как прежде, gates.sh не зовётся.
#   * Откат на :previous при мёртвом health и живой смоук — как были.
#
# Стенд свой: копия отслеживаемых файлов scripts/ и harness/ в tmp-репозитории,
# своя карта-образец, LOG_DIR стенда; для выката — паспорт, harness.conf,
# подставной docker и заглушки hackathon.py/gates.sh, как в
# scripts/test_deploy_product.sh. Боевые карта, журналы ворот и выкат не
# трогаются. Секрет для посева собирается в момент прогона, в тексте пробы его нет.
# Дверь мутаций: HACKATHON_PROBE_SRC — корень судимого дерева (копия проекта).
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "${HACKATHON_PROBE_SRC:-$HERE/..}" && pwd)"
# shellcheck disable=SC1091
source "$HERE/lib/selftest_cases.sh"

STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT
TODAY=$(date +%F)

# ── часть 1: состав быстрых ворот — разбором yaml ────────────────────────────
echo "── 1. состав harness/config/gates-hackathon.yaml ──"
SPEC="$SRC/harness/config/gates-hackathon.yaml"
python3 - "$SPEC" > "$STAND/spec.tsv" 2>"$STAND/spec.err" <<'PY'
import sys, yaml
data = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
for item in data.get("checks") or []:
    print(f"{item.get('name', '')}\t{item.get('cmd', '')}")
PY
NAMES=$(cut -f1 "$STAND/spec.tsv" | sed '/^$/d')
case_check "yaml разобран, проверок > 0" "да" "$([ -n "$NAMES" ] && echo да || echo нет)"
cmd_of() { awk -F'\t' -v n="$1" '$1 == n { print $2 }' "$STAND/spec.tsv"; }
for pair in "секреты|check-secrets.sh" "сторож команд|test_guard_bash.py" \
            "имена файлов|check-file-names.py" "схема карты|pre-commit-gate.sh --devmap-only" \
            "синтаксис|"; do
    name="${pair%%|*}"; needle="${pair#*|}"
    cmd="$(cmd_of "$name")"
    case_check "проверка «$name» есть${needle:+ и зовёт $needle}" "да" \
        "$([ -n "$cmd" ] && printf '%s' "$cmd" | grep -qF -- "$needle" && echo да || echo нет)"
done

# ── стенд ворот: копия отслеживаемых файлов, своя карта, свой git ────────────
TREE="$STAND/tree"
mkdir -p "$TREE" "$STAND/log"
if git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
    # Отслеживаемые и новые неигнорируемые: исполнитель гоняет пробу до коммита.
    (cd "$SRC" && git ls-files -z --cached --others --exclude-standard -- scripts harness .gitignore \
        | tar --null -T - -cf -) | tar -xf - -C "$TREE"
else
    cp -r "$SRC/scripts" "$SRC/harness" "$TREE/"
fi
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
        summary: "образец пробы быстрых ворот"
        глубина: { уровень: обычная, характер: починка, кем: смена, когда: "$TODAY" }
EOF
stand_git() { GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
              git -C "$TREE" -c user.name=proba -c user.email=p@p "$@"; }
stand_git init -q
stand_git add -A
stand_git commit -qm "стенд быстрых ворот"

GATES_OUT=""
GATES_RC=0
GATES_SECS=0
run_gates() {
    local started=$SECONDS
    # Без флага gates.sh пошёл бы ПОЛНЫМ прогоном на полчаса — пробе это запрещено.
    if ! grep -qF -- '--хакатон' "$TREE/scripts/gates.sh"; then
        GATES_OUT="gates.sh не знает флага --хакатон — прогон не начат"
        GATES_RC=99; GATES_SECS=999
        return
    fi
    GATES_OUT=$(cd "$TREE" && LOG_DIR="$STAND/log" GIT_CONFIG_GLOBAL=/dev/null \
                timeout 400 bash "$TREE/scripts/gates.sh" --хакатон 2>&1)
    GATES_RC=$?
    GATES_SECS=$((SECONDS - started))
}
ran_names() {  # имена проверок из строк вывода, по одной, в порядке прогона
    printf '%s\n' "$GATES_OUT" | python3 -c '
import re, sys
for line in sys.stdin:
    hit = re.match(r"^  (ок|ПЛОХО|жёлтый) · (.+?)(?: — .*| \(код \d+\)| \(обкатка.*)?$", line.rstrip("\n"))
    if hit:
        print(hit.group(2))'
}
status_of() { printf '%s\n' "$GATES_OUT" | grep -E "^  (ок|ПЛОХО|жёлтый) · $1( |$)" | awk '{print $1}' | head -1; }
says_no_changes() {  # строка «ок · <имя> — …» несёт «изменений нет»?
    printf '%s\n' "$GATES_OUT" | grep -E "^  ок · $1 — " | grep -qF 'изменений нет' && echo да || echo нет
}

echo "── 2. чистое дерево: идут ровно проверки yaml, зелёные, ≤ 180 с ──"
run_gates
printf '%s\n' "$GATES_OUT" | tail -8 | sed 's/^/      /'
case_check "код 0 на чистом дереве" "0" "$GATES_RC"
case_check "прогон ≤ 180 с (вышло $GATES_SECS с)" "да" "$([ "$GATES_SECS" -le 180 ] && echo да || echo нет)"
case_check "в выводе ровно проверки yaml, каждая один раз" \
    "$(printf '%s\n' "$NAMES" | sort | tr '\n' '|')" "$(ran_names | sort | tr '\n' '|')"
case_check "проверок в выводе не ноль" "да" "$([ -n "$(ran_names)" ] && echo да || echo нет)"
case_check "итог в ворота-итог.jsonl с режимом «хакатон»" "да" \
    "$(tail -1 "$STAND/log/ворота-итог.jsonl" 2>/dev/null | grep -qF '"режим":"хакатон"' && echo да || echo нет)"
case_check "чистое дерево: «имена файлов» говорит «изменений нет», а не молчаливое ок" "ок-да" \
    "$(status_of 'имена файлов')-$(says_no_changes 'имена файлов')"
case_check "чистое дерево: «синтаксис» говорит «изменений нет», а не молчаливое ок" "ок-да" \
    "$(status_of 'синтаксис')-$(says_no_changes 'синтаксис')"

echo "── 2б. непустой набор: здоровый новый .py — ок, и «изменений нет» не печатается ──"
printf 'print(1)\n' > "$TREE/scripts/seeded_ok.py"
stand_git add -A
run_gates
case_check "синтаксис: ок без «изменений нет»" "ок-нет" \
    "$(status_of 'синтаксис')-$(says_no_changes 'синтаксис')"
case_check "имена файлов: ок без «изменений нет»" "ок-нет" \
    "$(status_of 'имена файлов')-$(says_no_changes 'имена файлов')"
stand_git rm -q --cached -- scripts/seeded_ok.py
rm -f "$TREE/scripts/seeded_ok.py"

echo "── 3. БОЛЬНОЙ СЛУЧАЙ: посеяны секрет, битый .py и .sh, кириллическое имя кода ──"
KEY_NAME="api""_key"
KEY_VALUE="$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-28)"
printf '%s = "%s"\n' "$KEY_NAME" "$KEY_VALUE" > "$TREE/scripts/seeded_config.py"
printf 'def broken(:\n' > "$TREE/scripts/seeded_broken.py"
printf 'if then fi (\n' > "$TREE/scripts/seeded_broken.sh"
printf 'print(1)\n' > "$TREE/scripts/проба-имени.py"
stand_git add -A
run_gates
case_check "код ненулевой, секреты — красная" "да-ПЛОХО" \
    "$([ "$GATES_RC" != 0 ] && echo да || echo нет)-$(status_of 'секреты')"
case_check "синтаксис — красная (битые .py и .sh)" "ПЛОХО" "$(status_of 'синтаксис')"
case_check "синтаксис называет оба битых файла" "да" \
    "$(printf '%s' "$GATES_OUT" | grep -qF seeded_broken.py && printf '%s' "$GATES_OUT" | grep -qF seeded_broken.sh && echo да || echo нет)"
case_check "имена файлов — красная (кириллица в имени кода)" "ПЛОХО" "$(status_of 'имена файлов')"
# Итог в журнале обязан нести тех же красных, что и код выхода: «0» там прячет провал.
case_check "итог в ворота-итог.jsonl: красных = коду выхода (> 0)" "да" \
    "$(tail -1 "$STAND/log/ворота-итог.jsonl" 2>/dev/null | python3 -c '
import json, sys
rec = json.loads(sys.stdin.read() or "{}")
print("да" if rec.get("красных") == int(sys.argv[1]) > 0 else "нет")' "$GATES_RC")"
stand_git rm -q --cached -- scripts/seeded_config.py scripts/seeded_broken.py \
    scripts/seeded_broken.sh "scripts/проба-имени.py"
rm -f "$TREE/scripts/seeded_config.py" "$TREE/scripts/seeded_broken.py" \
      "$TREE/scripts/seeded_broken.sh" "$TREE/scripts/проба-имени.py"

echo "── 3а. БОЛЬНОЙ СЛУЧАЙ: состав пуст или нечитаем — отказ, а не «ноль красных» ──"
HK_YAML="$TREE/harness/config/gates-hackathon.yaml"
cp "$HK_YAML" "$STAND/gates-hackathon.keep"
for variant in "пустой список|checks: []" "нет ключа checks|# состав стёрт" "файла нет|"; do
    label="${variant%%|*}"; body="${variant#*|}"
    if [ -n "$body" ]; then printf '%s\n' "$body" > "$HK_YAML"; else rm -f "$HK_YAML"; fi
    before=$(wc -l < "$STAND/log/ворота-итог.jsonl" 2>/dev/null || echo 0)
    run_gates
    case_check "$label: код ≠ 0, проверок 0, слово «отказ»" "да|0|да" \
        "$([ "$GATES_RC" != 0 ] && echo да || echo нет)|$(ran_names | grep -c .)|$(printf '%s' "$GATES_OUT" | grep -qF 'отказ' && echo да || echo нет)"
    case_check "$label: итог «красных 0» в журнал не лёг" "да" \
        "$(tail -n +"$((before + 1))" "$STAND/log/ворота-итог.jsonl" 2>/dev/null | python3 -c '
import json, sys
print("нет" if any(json.loads(l).get("красных", 0) == 0 for l in sys.stdin if l.strip()) else "да")')"
done
cp "$STAND/gates-hackathon.keep" "$HK_YAML"

# ── часть 2: deploy.sh в режиме — стенд как у test_deploy_product.sh ─────────
HARNESS="$STAND/harnes"
PRODUCT="$STAND/produkt"
FAKEBIN="$STAND/bin"
COPY="$STAND/scripts-deploy"
mkdir -p "$HARNESS" "$PRODUCT" "$FAKEBIN" "$STAND/zhurnal" "$STAND/sekrety" "$COPY"
cat > "$STAND/install.conf" <<CONF
PROJECT_NAME="proba"
PROJECT_DIR="$HARNESS"
LOG_DIR="$STAND/zhurnal"
SECRETS_DIR="$STAND/sekrety"
TMUX_SESSION="net-takoj"
AGENT_START_CMD="true"
AUTONOMY="semi"
HEARTBEAT_DIR="$STAND/zhurnal/hb"
CONF
for repo in "$HARNESS" "$PRODUCT"; do
    printf 'стенд\n' > "$repo/README"
    git -C "$repo" init -q
    git -C "$repo" -c user.email="proba@stend" -c user.name="проба" add -A
    git -C "$repo" -c user.email="proba@stend" -c user.name="проба" commit -qm "стенд"
done
cat > "$FAKEBIN/docker" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${DOCKER_JOURNAL:-/dev/null}"
exit 0
SH
chmod +x "$FAKEBIN/docker"

cp "$SRC/scripts/deploy.sh" "$SRC/scripts/deploy_guard.py" "$SRC/scripts/check-secrets.sh" "$COPY/"
cp -r "$SRC/scripts/lib" "$COPY/"
printf 'import sys\nsys.exit(0)\n' > "$COPY/fix-claude-mem-project.py"
# Заглушки: режим и ворота отвечают тем, что велела проба, и пишут, как их звали.
cat > "$COPY/hackathon.py" <<'PY'
import os, sys
with open(os.environ["MODE_JOURNAL"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
sys.exit(int(os.environ.get("STUB_MODE_RC", "1")))
PY
cat > "$COPY/gates.sh" <<'SH'
#!/usr/bin/env bash
printf 'vorota %s\n' "$*" >> "$ORDER_FILE"
exit "${STUB_GATES_RC:-0}"
SH

DEAD_URL="http://127.0.0.1:9/nikogo"
conf() {  # $1 — строка ручки живости
    cat > "$STAND/harness.conf" <<CONF
PRODUCT_DIR="proba"
PRODUCT_REPO="$PRODUCT"
BACKUP_DIR="$STAND/kopii"
STACK_BUILD_CMD="echo sborka >> $STAND/poryadok"
STACK_DB_DUMP_CMD="echo damp >> $STAND/poryadok; head -c 4096 /dev/zero"
STACK_MIGRATE_CMD="echo migracii >> $STAND/poryadok"
STACK_RELEASE_CMD="echo peresozdanie >> $STAND/poryadok"
STACK_UP_CMD="echo otkat >> $STAND/poryadok"
STACK_IMAGES="proba-api"
DEVMAP_AUDIT_DAYS=0
$1
CONF
}
fresh_yes() {  # свежее «да» владельца на выкат (как в test_deploy_product.sh)
    python3 - "$STAND" <<'PY'
import json, sys, time
stand = sys.argv[1]
ts = time.strftime("%Y-%m-%dT%H:%M:%S+05:00")
with open(f"{stand}/zhurnal/confirmations.jsonl", "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"ts": ts, "text": "да", "message_id": "1", "kind": "any"},
                        ensure_ascii=False) + "\n")
# Голое «да» засчитывается только НОВЕЕ вопроса ворот (решение смены 04.11:
# «да» до вопроса или в ту же секунду не считается). Метка «да» усечена до
# секунды, поэтому вопрос кладётся минутой раньше — как в жизни, где ответ
# владельца всегда приходит после вопроса.
with open(f"{stand}/zhurnal/согласие.json", "w", encoding="utf-8") as fh:
    json.dump({"ожидание": {"действие": "deploy", "ts": time.time() - 60}}, fh, ensure_ascii=False)
PY
}
VYVOD=""
RC=0
deploy() {  # $1 — код заглушки режима, $2 — код заглушки ворот
    : > "$STAND/poryadok"; : > "$STAND/mode.log"; : > "$STAND/docker.log"
    fresh_yes
    sleep 1   # тег выката именуется по секунде
    VYVOD=$(PATH="$FAKEBIN:$PATH" DOCKER_JOURNAL="$STAND/docker.log" \
            ORDER_FILE="$STAND/poryadok" MODE_JOURNAL="$STAND/mode.log" \
            STUB_MODE_RC="$1" STUB_GATES_RC="$2" \
            HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
            timeout 300 bash "$COPY/deploy.sh" 2>&1)
    RC=$?
    printf '%s\n' "$VYVOD" | sed 's/^/      /' | tail -3
}
order() { tr '\n' ' ' < "$STAND/poryadok" | sed 's/ $//'; }
has() { printf '%s' "$VYVOD" | grep -qF -- "$1" && echo да || echo нет; }

echo "── 4. выкат в режиме: быстрые ворота зелёные — выкат идёт, ворота до сборки ──"
conf 'STACK_HEALTH_CMD="true"'
deploy 0 0
case_check "deploy спросил режим: --активен --карта <PROJECT_DIR>/dev-map.yaml" \
    "--активен --карта $HARNESS/dev-map.yaml" "$(head -1 "$STAND/mode.log")"
case_check "порядок: быстрые ворота --хакатон до сборки; выкат ok, код 0" \
    "vorota --хакатон sborka damp migracii peresozdanie|0|да" "$(order)|$RC|$(has 'deploy ok')"
case_check "журнал docker стенда пишется: снимок образов звал image inspect" "да" \
    "$(grep -qF 'image inspect' "$STAND/docker.log" && echo да || echo нет)"

echo "── 5. БОЛЬНОЙ СЛУЧАЙ: в режиме быстрые ворота красные — выкат отменён до сборки ──"
# Код ворот 7, а не 1: без своей ветки отказа set -e тоже оборвал бы выкат, но
# кодом ворот и без слов об отмене — различает только код 1 и строка отказа.
deploy 0 7
case_check "код 1 (отказ ветки ворот, не код самих ворот)" "1" "$RC"
case_check "ворота звались, сборки нет" "vorota --хакатон" "$(order)"
case_check "до снимка образов не дошёл (docker image inspect не звался)" "нет" \
    "$(grep -qF 'image inspect' "$STAND/docker.log" && echo да || echo нет)"
case_check "отказ из ветки ворот: строка с «хакатон» и «выкат отменён»" "да" \
    "$(printf '%s\n' "$VYVOD" | grep -F 'хакатон' | grep -qF 'выкат отменён' && echo да || echo нет)"

echo "── 6. в режиме откат и живой смоук остались: мёртвый health — откат на :previous ──"
conf "STACK_HEALTH_URL=\"$DEAD_URL\""
deploy 0 0
case_check "НЕИЗМЕННО (откат остался): код 1" "1" "$RC"
case_check "порядок: ворота → сборка → дамп → миграции → пересоздание → откат" \
    "vorota --хакатон sborka damp migracii peresozdanie otkat" "$(order)"
case_check "НЕИЗМЕННО (откат остался): образ возвращён с :previous на :latest" "да" \
    "$(grep -qF 'tag proba-api:previous proba-api:latest' "$STAND/docker.log" && echo да || echo нет)"

echo "── 7. НЕИЗМЕННО: вне режима выкат как прежде, быстрые ворота не зовутся ──"
conf 'STACK_HEALTH_CMD="true"'
deploy 1 1
case_check "НЕИЗМЕННО: вне режима gates.sh не звался, выкат ok" \
    "sborka damp migracii peresozdanie|0" "$(order)|$RC"

if [ "$CASES_FAILED" -eq 0 ]; then
    printf '\nПРОБА ЗЕЛЁНАЯ: случаев %d\n' "$CASES_PATHS"; exit 0
fi
printf '\nПРОБА КРАСНАЯ: неудач %d из %d\n' "$CASES_FAILED" "$CASES_PATHS"; exit 1
