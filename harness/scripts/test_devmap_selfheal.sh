#!/usr/bin/env bash
# Проба коммита карты демоном devmap-selfheal.sh — целым прогоном на стенде.
#
# Что судится. Демон зовёт модель, та правит dev-map.yaml, демон коммитит.
# Две беды этого шага пришли с соседней установки (отчёт 18.10.2026):
#   * 3.6 — модель переписала статус СУЩЕСТВУЮЩЕЙ карточки (`test → wip`
#     через 7 минут после выката), и демон это закоммитил. Правило: демон
#     только дописывает; статус, который модель сменила, — отказ коммита и
#     строка в журнал с именем карточки.
#   * c0c2ae9 — задача, перенесённая приёмкой в архив без коммита, в коммите
#     демона выглядела пропавшей: демон коммитил одну карту, гейт потери задач
#     отказывал. Правило: грязный архив карты уходит тем же коммитом.
#
# Модель на стенде — подставная: `scripts/claude-demon.sh` стенда кладёт
# заранее написанную карту на место настоящей. Сеть, claude и боевые журналы
# не трогаются: свой LOG_DIR, HEARTBEAT_DIR и PROJECT_DIR в окружении И в
# конфиге стенда (скрипт читает конфиг после окружения).
#
# Каждый случай — свой свежий каталог mktemp -d: удалять после прогона нечего,
# кроме самого каталога по полному пути.
#
# Дверь для мутаций: DEVMAP_SELFHEAL_UNDER_TEST — путь к испорченной копии.
#
#   bash scripts/test_devmap_selfheal.sh
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
DAEMON="${DEVMAP_SELFHEAL_UNDER_TEST:-}"
if [ -z "$DAEMON" ]; then
    for candidate in "$HERE/../harness/demons/devmap-selfheal.sh" "$HERE/../demons/devmap-selfheal.sh"; do
        [ -f "$candidate" ] && { DAEMON="$(readlink -f "$candidate")"; break; }
    done
fi
[ -f "${DAEMON:-}" ] || { echo "ПРОБА СЛОМАНА: devmap-selfheal.sh не найден"; exit 1; }

bad=0
total=0
STANDS=()
cleanup() {
    local d
    for d in "${STANDS[@]}"; do rm -rf "$d"; done
}
trap cleanup EXIT

verdict() {  # $1 = 0/1 (1 = верно), $2 = имя случая, $3 = подробность
    total=$((total + 1))
    if [ "$1" = 1 ]; then
        echo "  ок    $2"
    else
        echo "  ПЛОХО $2${3:+ — $3}"
        bad=$((bad + 1))
    fi
}

# Карта стенда. $1 — статус hr.a, $2 — статус hr.b, $3 — «есть hr.b» (1/0),
# $4 — «есть новая карточка hr.n» (1/0), $5 — «у hr.a новый пункт чек-листа» (1/0).
map_text() {
    cat <<YAML
version: 1
lines:
  - id: harness
    name: "Стенд"
    tasks:
      - id: hr.a
        status: $1
        title: "Карточка А"
YAML
    if [ "$5" = 1 ]; then
        cat <<'YAML'
        checklist:
          - { summary: "Дописано демоном", done_at: "2026-10-18" }
YAML
    fi
    if [ "$3" = 1 ]; then
        cat <<YAML
      - id: hr.b
        status: $2
        title: "Карточка Б"
YAML
    fi
    if [ "$4" = 1 ]; then
        cat <<'YAML'
      - id: hr.n
        status: test
        title: "Новая карточка от демона"
YAML
    fi
}

archive_text() {  # $1 = «hr.b в архиве» (1/0)
    printf 'version: 1\nlines:\n  - id: harness\n    name: "Стенд"\n    tasks:\n'
    printf '      - id: hr.old\n        status: done\n        title: "Давняя"\n'
    if [ "$1" = 1 ]; then
        printf '      - id: hr.b\n        status: done\n        title: "Карточка Б"\n'
    fi
}

# Новый стенд: репозиторий с картой (hr.a test, hr.b wip), архивом, подставной
# моделью и одним рабочим коммитом без трейлера — чтобы демону было что чинить.
# Стенд создаётся в главной оболочке: из $(…) список STANDS не вернулся бы.
new_stand() {
    T=$(mktemp -d)
    STANDS+=("$T")
    local R="$T/repo"
    mkdir -p "$R/scripts/lib" "$R/docs" "$T/log" "$T/hb"
    map_text test wip 1 0 0 > "$R/dev-map.yaml"
    archive_text 0 > "$R/docs/dev-map-archive.yaml"
    printf 'заметки\n' > "$R/notes.txt"
    # Подставная модель: кладёт карту, написанную случаем, на место настоящей.
    # Карты случая нет — модель ничего не правит (второй проход VG-01).
    cat > "$R/scripts/claude-demon.sh" <<'SH'
#!/usr/bin/env bash
[ -f "$STAND_MODEL_MAP" ] || exit 0
cp "$STAND_MODEL_MAP" "$PROJECT_DIR/dev-map.yaml"
SH
    printf 'model_dlya() { printf haiku; }\nmodel_imya_dopustimo() { return 0; }\n' \
        > "$R/scripts/lib/models.sh"
    git -C "$T" init -q -b master repo
    # Имя автора — в конфиге стенда: демон коммитит своей рукой, и без него
    # git отказал бы ДО правила, которое судится (красный от стенда).
    git -C "$R" config user.name стенд
    git -C "$R" config user.email stand@example.invalid
    git -C "$R" add -A
    git -C "$R" -c user.name=t -c user.email=t@t commit -q -m 'стенд: начало

Dev-Map: hr.a'
    git -C "$R" -c user.name=t -c user.email=t@t commit -q --allow-empty -m 'работа без трейлера'
    printf 'PROJECT_NAME="t"\nPROJECT_DIR="%s"\nLOG_DIR="%s/log"\nHEARTBEAT_DIR="%s/hb"\n' \
        "$R" "$T" "$T" > "$T/install.conf"
    : > "$T/harness.conf"
}

# Прогон демона на стенде. Печатает его вывод; код — в файле $T/rc.
run_daemon() {  # $1 = стенд
    local T="$1" rc=0 out
    out=$(cd "$T" && env -u SELFHEAL_CLAUDE_TIMEOUT_SEC -u DEVMAP_SERVICE_PREFIXES \
            HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
            PROJECT_DIR="$T/repo" LOG_DIR="$T/log" HEARTBEAT_DIR="$T/hb" \
            STAND_MODEL_MAP="$T/model-map.yaml" \
            timeout 60 bash "$DAEMON" 30 2>&1) || rc=$?
    printf '%s' "$rc" > "$T/rc"
    printf '%s' "$out"
}

head_of() { git -C "$1/repo" rev-parse HEAD; }
status_in_head() {  # $1 = стенд, $2 = id → статус карточки в HEAD
    git -C "$1/repo" show HEAD:dev-map.yaml | python3 -c '
import sys, yaml
d = yaml.safe_load(sys.stdin) or {}
for л in d.get("lines") or []:
    for з in л.get("tasks") or []:
        if з.get("id") == sys.argv[1]:
            print(з.get("status"))
' "$2"
}
in_head_map() {  # $1 = стенд, $2 = id → 1, если карточка в HEAD-карте
    git -C "$1/repo" show HEAD:dev-map.yaml | grep -q -- "- id: $2\$" && echo 1 || echo 0
}

# ── 3.6, БОЛЬНОЙ СЛУЧАЙ: модель сменила статус существующей карточки ────────
new_stand
before=$(head_of "$T")
map_text wip wip 1 1 0 > "$T/model-map.yaml"          # hr.a: test → wip, плюс hr.n
out=$(run_daemon "$T")
after=$(head_of "$T")
verdict "$([ "$before" = "$after" ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ 3.6: модель сменила статус hr.a test→wip — коммита нет" \
    "коммит есть: $(git -C "$T/repo" log -1 --format=%s), hr.a в HEAD = $(status_in_head "$T" hr.a)"
verdict "$(printf '%s' "$out" | grep -q 'hr\.a' && printf '%s' "$out" | grep -qi 'статус' && echo 1 || echo 0)" \
    "БОЛЬНОЙ 3.6: отказ назван в журнале — имя карточки и слово «статус»" \
    "вывод: $(printf '%s' "$out" | tail -3 | tr '\n' ' ')"
verdict "$([ ! -s "$T/log/devmap/audit.cursor" ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ 3.6: водораздел не сдвинут — пропуск всплывёт в следующий прогон" \
    "audit.cursor записан"

# ── 3.6: обмен статусами — счёт статусов тот же, карточки другие ────────────
new_stand
before=$(head_of "$T")
map_text wip test 1 0 0 > "$T/model-map.yaml"         # hr.a test→wip, hr.b wip→test
run_daemon "$T" >/dev/null
verdict "$([ "$before" = "$(head_of "$T")" ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ 3.6: обмен статусами hr.a↔hr.b (число test/wip то же) — коммита нет" \
    "коммит есть: hr.a=$(status_in_head "$T" hr.a), hr.b=$(status_in_head "$T" hr.b)"

# ── 3.6, сосед: дописать — законно ──────────────────────────────────────────
new_stand
before=$(head_of "$T")
map_text test wip 1 1 1 > "$T/model-map.yaml"         # новая hr.n + пункт у hr.a
run_daemon "$T" >/dev/null
verdict "$([ "$before" != "$(head_of "$T")" ] && [ "$(in_head_map "$T" hr.n)" = 1 ] \
            && [ "$(status_in_head "$T" hr.a)" = test ] && echo 1 || echo 0)" \
    "новая карточка и пункт чек-листа без смены статуса — коммит есть" \
    "HEAD сдвинут: $([ "$before" != "$(head_of "$T")" ] && echo да || echo нет), hr.n в HEAD: $(in_head_map "$T" hr.n), rc=$(cat "$T/rc")"

# ── 3.6, сосед: статус сменил НЕ демон (правка приёмки до прогона) ──────────
# «До» — карта, какой демон застал её перед вызовом модели, а не HEAD: иначе
# законная правка приёмки (test → done) не уехала бы никогда.
new_stand
map_text done wip 1 0 0 > "$T/repo/dev-map.yaml"      # приёмка: hr.a test → done
map_text done wip 1 1 0 > "$T/model-map.yaml"         # модель только дописала hr.n
before=$(head_of "$T")
run_daemon "$T" >/dev/null
verdict "$([ "$before" != "$(head_of "$T")" ] && [ "$(status_in_head "$T" hr.a)" = done ] \
            && [ "$(in_head_map "$T" hr.n)" = 1 ] && echo 1 || echo 0)" \
    "статус сменила приёмка ДО прогона, модель только дописала — коммит есть" \
    "HEAD сдвинут: $([ "$before" != "$(head_of "$T")" ] && echo да || echo нет), hr.a=$(status_in_head "$T" hr.a), rc=$(cat "$T/rc")"

# ── 3.6, БОЛЬНОЙ СЛУЧАЙ: карточку удалила САМА модель ─────────────────────
# Снимок «до» снимается под общим локом карты прямо перед вызовом модели, а
# приёмка переносит в архив под тем же локом — значит, карточка, которой нет
# «после», удалена моделью. Это потеря задачи (И-1), а не перенос в архив:
# демон только дописывает.
new_stand
before=$(head_of "$T")
map_text test wip 0 1 0 > "$T/model-map.yaml"         # модель выкинула hr.b, дописала hr.n
out=$(run_daemon "$T")
verdict "$([ "$before" = "$(head_of "$T")" ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ 3.6: модель удалила карточку hr.b (не архив) — коммита нет" \
    "коммит есть: hr.b в HEAD: $(in_head_map "$T" hr.b)"
verdict "$(printf '%s' "$out" | grep -q 'hr\.b' && echo 1 || echo 0)" \
    "БОЛЬНОЙ 3.6: отказ называет удалённую карточку hr.b" \
    "вывод: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"

# ── c0c2ae9, БОЛЬНОЙ СЛУЧАЙ: карта и архив одним коммитом ───────────────────
new_stand
map_text test wip 0 0 0 > "$T/repo/dev-map.yaml"      # приёмка унесла hr.b из карты…
archive_text 1 > "$T/repo/docs/dev-map-archive.yaml"  # …в архив, без коммита
map_text test wip 0 1 0 > "$T/model-map.yaml"         # модель дописала hr.n
printf 'чужая правка\n' >> "$T/repo/notes.txt"         # посторонний грязный файл
before=$(head_of "$T")
run_daemon "$T" >/dev/null
after=$(head_of "$T")
files=$(git -C "$T/repo" -c core.quotepath=false show --name-only --format= HEAD | sort | tr '\n' ' ')
verdict "$([ "$before" != "$after" ] && [ "$(git -C "$T/repo" rev-list --count "$before..$after")" = 1 ] \
            && printf '%s' "$files" | grep -q 'docs/dev-map-archive.yaml' \
            && printf '%s' "$files" | grep -q 'dev-map.yaml' && echo 1 || echo 0)" \
    "БОЛЬНОЙ c0c2ae9: задача ушла в архив — карта и архив в ОДНОМ коммите" \
    "в коммите: ${files:-ничего}"
verdict "$([ -z "$(git -C "$T/repo" status --porcelain -- docs/dev-map-archive.yaml)" ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ c0c2ae9: архив после прогона не остался грязным" \
    "$(git -C "$T/repo" status --porcelain -- docs/dev-map-archive.yaml)"
verdict "$(printf '%s' "$files" | grep -q 'notes.txt' && echo 0 || echo 1)" \
    "c0c2ae9, сосед: посторонний грязный файл в коммит демона не попал" \
    "в коммите: $files"

# ── c0c2ae9, сосед: архив чист — коммит только карты, как было ──────────────
new_stand
map_text test wip 1 1 0 > "$T/model-map.yaml"
run_daemon "$T" >/dev/null
files=$(git -C "$T/repo" -c core.quotepath=false show --name-only --format= HEAD | tr '\n' ' ')
verdict "$([ "$files" = "dev-map.yaml " ] && echo 1 || echo 0)" \
    "архив не тронут — в коммите демона одна карта" "в коммите: $files"

# ── VG-01, БОЛЬНОЙ СЛУЧАЙ: отказ держит не один проход, а все ───────────────
# Отказ коммита оставлял правку модели в дереве: на втором проходе снимок «до»
# снимался уже с ней, сверка «до/после» была пуста, и демон коммитил запрет
# «вместе с чужой правкой». Правило: на отказе дерево карты возвращается к
# снимку, который демон застал перед вызовом модели, — с ЧУЖОЙ правкой этого
# снимка (И-1: приёмка сменила hr.b wip→test до прогона, и это не теряется).
new_stand
map_text test test 1 0 0 > "$T/repo/dev-map.yaml"     # чужая правка до прогона: hr.b wip→test
cp "$T/repo/dev-map.yaml" "$T/snapshot-before.yaml"
map_text wip test 1 1 0 > "$T/model-map.yaml"         # модель: hr.a test→wip, плюс hr.n
before=$(head_of "$T")
out1=$(run_daemon "$T")
verdict "$([ "$before" = "$(head_of "$T")" ] && echo 1 || echo 0)" \
    "VG-01, проход 1: смена статуса моделью — коммита нет" \
    "коммит есть: hr.a в HEAD = $(status_in_head "$T" hr.a)"
verdict "$(cmp -s "$T/snapshot-before.yaml" "$T/repo/dev-map.yaml" && echo 1 || echo 0)" \
    "БОЛЬНОЙ VG-01, проход 1: дерево карты после отказа = снимок «до» (с чужой правкой hr.b)" \
    "в дереве hr.a=$(python3 -c 'import sys,yaml;print([t["status"] for l in yaml.safe_load(open(sys.argv[1]))["lines"] for t in l["tasks"] if t["id"]=="hr.a"])' "$T/repo/dev-map.yaml"), hr.n в дереве: $(grep -c -- '- id: hr.n$' "$T/repo/dev-map.yaml")"
rm -f "$T/model-map.yaml"                             # проход 2: модель ничего не правит
out2=$(run_daemon "$T")
verdict "$([ "$(status_in_head "$T" hr.a)" = test ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ VG-01, проход 2: отвергнутая смена hr.a test→wip в HEAD не уехала" \
    "hr.a в HEAD = $(status_in_head "$T" hr.a); коммит: $(git -C "$T/repo" log -1 --format=%s); вывод: $(printf '%s' "$out2" | tail -2 | tr '\n' ' ')"
verdict "$([ "$(in_head_map "$T" hr.n)" = 0 ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ VG-01, проход 2: карточка hr.n из отвергнутой правки в HEAD не уехала" \
    "hr.n в HEAD: $(in_head_map "$T" hr.n)"
# Сосед: «держать отказ» не значит «больше не коммитить никогда» — чужая
# правка hr.b, честно грязная до прогона, уезжает вторым проходом.
verdict "$([ "$before" != "$(head_of "$T")" ] && [ "$(status_in_head "$T" hr.b)" = test ] && echo 1 || echo 0)" \
    "VG-01, проход 2, сосед: чужая правка hr.b wip→test закоммичена, карта не заперта" \
    "HEAD сдвинут: $([ "$before" != "$(head_of "$T")" ] && echo да || echo нет), hr.b в HEAD = $(status_in_head "$T" hr.b), rc=$(cat "$T/rc")"

echo
echo "всего путей: $total, неудач: $bad"
[ "$bad" = 0 ]
