#!/usr/bin/env bash
# Проба коммита карты демоном devmap-selfheal.sh — целым прогоном на стенде.
#
# Что судится. Демон зовёт модель, та правит dev-map.yaml, демон коммитит.
# Две беды этого шага пришли с соседней установки (отчёт 18.10.2026):
#   * 3.6 — модель переписала статус СУЩЕСТВУЮЩЕЙ карточки (`test → wip`
#     через 7 минут после выката), и демон это закоммитил. Правило: демон
#     только дописывает; статус, который модель сменила, возвращается к
#     прежнему строкой, остальная правка едет в коммит, журнал называет
#     карточку. Удалённая карточка и нечитаемая карта — отказ целиком.
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
    # mid-run.sh случая — то, что делает с репозиторием сосед, пока модель думает.
    cat > "$R/scripts/claude-demon.sh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$PROJECT_DIR/../prompt.txt"
[ -f "$PROJECT_DIR/../mid-run.sh" ] && bash "$PROJECT_DIR/../mid-run.sh"
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
# 17.11 и 19.11 отказ ЦЕЛИКОМ из-за одной строки статуса стоил всей ночной
# правки (33 и 87 пропусков) и тревоги «демон мёртв». Сторож отвергает строку,
# а не правку: статус возвращён, пропуски закоммичены.
new_stand
before=$(head_of "$T")
map_text wip wip 1 1 0 > "$T/model-map.yaml"          # hr.a: test → wip, плюс hr.n
out=$(run_daemon "$T")
after=$(head_of "$T")
verdict "$([ "$before" != "$after" ] && [ "$(status_in_head "$T" hr.a)" = test ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ 3.6: модель сменила статус hr.a test→wip — в HEAD hr.a прежний test" \
    "HEAD сдвинут: $([ "$before" != "$after" ] && echo да || echo нет), hr.a в HEAD = $(status_in_head "$T" hr.a)"
verdict "$([ "$(in_head_map "$T" hr.n)" = 1 ] && [ "$(cat "$T/rc")" = 0 ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ 19.11: чужая строка статуса не топит правку — hr.n закоммичена, код 0" \
    "hr.n в HEAD: $(in_head_map "$T" hr.n), rc=$(cat "$T/rc"); вывод: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
verdict "$(printf '%s' "$out" | grep -q 'hr\.a' && printf '%s' "$out" | grep -qi 'статус' && echo 1 || echo 0)" \
    "БОЛЬНОЙ 3.6: возврат назван в журнале — имя карточки и слово «статус»" \
    "вывод: $(printf '%s' "$out" | tail -3 | tr '\n' ' ')"
rejected=$(find "$T/log/devmap" -maxdepth 1 -name 'rejected-*.yaml' -print -quit)
verdict "$([ -n "$rejected" ] && cmp -s "$T/model-map.yaml" "$rejected" && echo 1 || echo 0)" \
    "3.6: правка модели как была, со сменой статуса, сохранена в rejected-*.yaml" "отвергнутая: ${rejected:-нет}"
verdict "$([ "$(cat "$T/log/devmap/audit.cursor" 2>/dev/null)" = "$before" ] && echo 1 || echo 0)" \
    "3.6: пропуски заведены — водораздел сдвинут на HEAD сверки" \
    "audit.cursor: $(cat "$T/log/devmap/audit.cursor" 2>/dev/null || echo нет)"

# ── 3.6: обмен статусами — счёт статусов тот же, карточки другие ────────────
new_stand
before=$(head_of "$T")
map_text wip test 1 0 0 > "$T/model-map.yaml"         # hr.a test→wip, hr.b wip→test
run_daemon "$T" >/dev/null
verdict "$([ "$before" = "$(head_of "$T")" ] && [ -z "$(git -C "$T/repo" status --porcelain -- dev-map.yaml)" ] && [ "$(cat "$T/rc")" = 0 ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ 3.6: обмен статусами hr.a↔hr.b (число test/wip то же) — оба возвращены, коммитить нечего, код 0" \
    "hr.a=$(status_in_head "$T" hr.a), hr.b=$(status_in_head "$T" hr.b), дерево: $(git -C "$T/repo" status --porcelain -- dev-map.yaml), rc=$(cat "$T/rc")"

# ── БОЛЬНОЙ СЛУЧАЙ 19.11.2026: статус сменил КОММИТ, влитый во время прогона ──
# Модель думала 6 минут; в это время очередь слияний влила ветку, где три
# карточки ушли wip→test, — дерево карты сменилось под моделью. Сторож сравнил
# снимок «до» с картой «после» и записал чужой коммит на модель: отказ всей
# правки с 87 пропусками, код 1. Смена, которую принёс HEAD, — не модели.
merge_mid_run() {
    cat > "$T/mid-run.sh" <<'SH'
cd "$PROJECT_DIR"
sed -i 's/^        status: wip$/        status: test/' dev-map.yaml
git add dev-map.yaml
git -c user.name=t -c user.email=t@t commit -q -m 'слито: hr.b в test

Dev-Map: hr.b'
SH
}
new_stand
merge_mid_run
map_text test test 1 1 0 > "$T/model-map.yaml"        # модель дописала hr.n к влитой карте
before=$(head_of "$T")
out=$(run_daemon "$T")
verdict "$([ "$(git -C "$T/repo" rev-list --count "$before..HEAD")" = 2 ] && [ "$(in_head_map "$T" hr.n)" = 1 ] \
            && [ "$(status_in_head "$T" hr.b)" = test ] && [ "$(cat "$T/rc")" = 0 ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ 19.11: смену hr.b wip→test принёс влитый коммит — правка модели закоммичена, код 0" \
    "коммитов после: $(git -C "$T/repo" rev-list --count "$before..HEAD"), hr.n в HEAD: $(in_head_map "$T" hr.n), hr.b=$(status_in_head "$T" hr.b), rc=$(cat "$T/rc"); вывод: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
verdict "$(printf '%s' "$out" | grep -q 'hr\.b(' && echo 0 || echo 1)" \
    "БОЛЬНОЙ 19.11: смена из влитого коммита не названа сменой модели" \
    "вывод: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
# Сосед: влитый коммит не прикрывает модель — её собственные смены возвращаются:
# hr.a к статусу до прогона, hr.b (влито test, модель поверх — done) к статусу
# HEAD, а не к снимку «до»: возврат не откатывает влитый коммит.
new_stand
merge_mid_run
map_text wip done 1 1 0 > "$T/model-map.yaml"         # влито hr.b test; модель: hr.a test→wip, hr.b →done
run_daemon "$T" >/dev/null
verdict "$([ "$(status_in_head "$T" hr.a)" = test ] && [ "$(status_in_head "$T" hr.b)" = test ] && [ "$(in_head_map "$T" hr.n)" = 1 ] && echo 1 || echo 0)" \
    "19.11, сосед: влитый коммит не прикрывает смену самой модели — hr.a возвращён, hr.b к влитому test" \
    "hr.a=$(status_in_head "$T" hr.a), hr.b=$(status_in_head "$T" hr.b), hr.n в HEAD: $(in_head_map "$T" hr.n)"

# ── статус, который строкой не вернуть (карточка в одну строку), — отказ целиком
new_stand
printf '      - { id: hr.f, status: wip, title: "Строчная" }\n' >> "$T/repo/dev-map.yaml"
git -C "$T/repo" add -A
git -C "$T/repo" -c user.name=t -c user.email=t@t commit -q -m 'карта: строчная карточка

Dev-Map: hr.f'
git -C "$T/repo" -c user.name=t -c user.email=t@t commit -q --allow-empty -m 'работа после строчной'
cp "$T/repo/dev-map.yaml" "$T/snapshot-before.yaml"
{ map_text test wip 1 1 0; printf '      - { id: hr.f, status: test, title: "Строчная" }\n'; } > "$T/model-map.yaml"
before=$(head_of "$T")
run_daemon "$T" >/dev/null
verdict "$([ "$before" = "$(head_of "$T")" ] && cmp -s "$T/snapshot-before.yaml" "$T/repo/dev-map.yaml" && [ "$(cat "$T/rc")" = 1 ] && echo 1 || echo 0)" \
    "статус строчной карточки не вернуть строкой — отказ целиком, карта = снимок, код 1" \
    "HEAD сдвинут: $([ "$before" != "$(head_of "$T")" ] && echo да || echo нет), rc=$(cat "$T/rc")"

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
# Отказ целиком остался для удалённой карточки: её строкой не вернуть.
new_stand
map_text done wip 1 0 0 > "$T/repo/dev-map.yaml"      # чужая правка до прогона: hr.a test→done
cp "$T/repo/dev-map.yaml" "$T/snapshot-before.yaml"
map_text done wip 0 1 0 > "$T/model-map.yaml"         # модель: выкинула hr.b, плюс hr.n
before=$(head_of "$T")
out1=$(run_daemon "$T")
verdict "$([ "$before" = "$(head_of "$T")" ] && echo 1 || echo 0)" \
    "VG-01, проход 1: удаление карточки моделью — коммита нет" \
    "коммит есть: hr.b в HEAD = $(in_head_map "$T" hr.b)"
verdict "$(cmp -s "$T/snapshot-before.yaml" "$T/repo/dev-map.yaml" && echo 1 || echo 0)" \
    "БОЛЬНОЙ VG-01, проход 1: дерево карты после отказа = снимок «до» (с чужой правкой hr.a)" \
    "hr.b в дереве: $(grep -c -- '- id: hr.b$' "$T/repo/dev-map.yaml"), hr.n в дереве: $(grep -c -- '- id: hr.n$' "$T/repo/dev-map.yaml")"
rm -f "$T/model-map.yaml"                             # проход 2: модель ничего не правит
out2=$(run_daemon "$T")
verdict "$([ "$(in_head_map "$T" hr.b)" = 1 ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ VG-01, проход 2: отвергнутое удаление hr.b в HEAD не уехало" \
    "hr.b в HEAD: $(in_head_map "$T" hr.b); коммит: $(git -C "$T/repo" log -1 --format=%s); вывод: $(printf '%s' "$out2" | tail -2 | tr '\n' ' ')"
verdict "$([ "$(in_head_map "$T" hr.n)" = 0 ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ VG-01, проход 2: карточка hr.n из отвергнутой правки в HEAD не уехала" \
    "hr.n в HEAD: $(in_head_map "$T" hr.n)"
# Сосед: «держать отказ» не значит «больше не коммитить никогда» — чужая
# правка hr.a, честно грязная до прогона, уезжает вторым проходом.
verdict "$([ "$before" != "$(head_of "$T")" ] && [ "$(status_in_head "$T" hr.a)" = done ] && echo 1 || echo 0)" \
    "VG-01, проход 2, сосед: чужая правка hr.a test→done закоммичена, карта не заперта" \
    "HEAD сдвинут: $([ "$before" != "$(head_of "$T")" ] && echo да || echo нет), hr.a в HEAD = $(status_in_head "$T" hr.a), rc=$(cat "$T/rc")"

# ── БОЛЬНОЙ СЛУЧАЙ 14.11.2026: гейт отверг ЧУЖУЮ правку, ехавшую в коммите ────
# В дереве лежали незакоммиченные переводы карточек в test (смена ждала мутаций),
# демон коммитил карту целиком, гейт пайплайна отказал — и пропуски, заведённые
# моделью, не уехали вовсе (код 1). Правило: на отказе демон коммитит одну свою
# правку поверх HEAD, чужая остаётся в дереве как была.
new_stand
cat > "$T/repo/.git/hooks/pre-commit" <<'SH'
#!/usr/bin/env bash
git show :dev-map.yaml | grep -A1 -- '- id: hr.b$' | grep -q 'status: test' && { echo 'гейт: hr.b в test без мутаций'; exit 1; }
exit 0
SH
chmod +x "$T/repo/.git/hooks/pre-commit"
map_text test test 1 0 0 > "$T/repo/dev-map.yaml"     # чужая правка: hr.b wip→test
map_text test test 1 1 0 > "$T/model-map.yaml"        # модель: дописала hr.n
before=$(head_of "$T")
out=$(run_daemon "$T")
verdict "$([ "$before" != "$(head_of "$T")" ] && [ "$(in_head_map "$T" hr.n)" = 1 ] && [ "$(cat "$T/rc")" = 0 ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ 14.11: гейт отверг чужую правку — своя (hr.n) закоммичена одна, код 0" \
    "HEAD сдвинут: $([ "$before" != "$(head_of "$T")" ] && echo да || echo нет), hr.n в HEAD: $(in_head_map "$T" hr.n), rc=$(cat "$T/rc"); вывод: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
verdict "$([ "$(status_in_head "$T" hr.b)" = wip ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ 14.11: отвергнутая чужая правка hr.b в HEAD не уехала" "hr.b в HEAD = $(status_in_head "$T" hr.b)"
verdict "$(grep -A1 -- '- id: hr.b$' "$T/repo/dev-map.yaml" | grep -q 'status: test' && grep -q -- '- id: hr.n$' "$T/repo/dev-map.yaml" && [ -z "$(git -C "$T/repo" diff --cached --name-only)" ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ 14.11: чужая правка hr.b осталась в дереве, индекс чист" \
    "индекс: $(git -C "$T/repo" diff --cached --name-only | tr '\n' ' '); дерево: $(git -C "$T/repo" diff --stat | tail -1)"
verdict "$(cmp -s "$T/repo/dev-map.yaml" "$T/model-map.yaml" && echo 1 || echo 0)" \
    "14.11, сосед: дерево карты = правка модели поверх чужой, ничего не потеряно"

# ── сверка: служебные темы и покрытые картой коммиты не считаются пропуском ──
# Фильтр судится на ЖИВОМ значении ключа из harness.conf (разбор файла), а не
# на копии списка: копия разъехалась бы с конфигом молча.
CONF_PREFIXES=$(sed -n 's/^DEVMAP_SERVICE_PREFIXES="\(.*\)"$/\1/p' "$HERE/../harness/config/harness.conf" | head -1)
verdict "$([ -n "$CONF_PREFIXES" ] && echo 1 || echo 0)" \
    "ключ DEVMAP_SERVICE_PREFIXES разобран из harness.conf (пустой разбор — провал пробы)" "значение пусто"
new_stand
for subject in 'Правка памяти с панели: .orders' 'ПЕРЕДАЧА смены 04.11 01:52' 'Рубеж смены 21:03: STATE.md' 'devmap: пропуски заведены в карту (selfheal)'; do
    git -C "$T/repo" -c user.name=t -c user.email=t@t commit -q --allow-empty -m "$subject"
done
list_out=$(cd "$T" && DEVMAP_SERVICE_PREFIXES="$CONF_PREFIXES" HARNESS_INSTALL_CONF="$T/install.conf" \
    HARNESS_CONF="$T/harness.conf" bash "$DAEMON" --список-пропусков 30 2>/dev/null)
verdict "$(printf '%s' "$list_out" | grep -qiE 'памяти с панели|передача|рубеж|devmap:' && echo 0 || echo 1)" \
    "служебные темы (правка памяти с панели, передача, рубеж смены, devmap:) без учёта регистра не в выборке" \
    "в выборке: $(printf '%s' "$list_out" | cut -c1-80 | tr '\n' '|')"
verdict "$(printf '%s' "$list_out" | grep -q 'работа без трейлера' && echo 1 || echo 0)" \
    "служебный фильтр не съел рабочий коммит" "выборка: $list_out"

new_stand
sha=$(git -C "$T/repo" log -1 --format=%H)
sha_list_before=$(cd "$T" && HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" bash "$DAEMON" --список-пропусков 30 2>/dev/null)
printf '        checklist:\n          - { summary: "работа %s", done_at: "2026-11-05" }\n' "${sha:0:10}" >> "$T/repo/dev-map.yaml"
sha_list_after=$(cd "$T" && HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" bash "$DAEMON" --список-пропусков 30 2>/dev/null)
verdict "$(printf '%s' "$sha_list_before" | grep -q "${sha:0:10}" && [ -z "$(printf '%s' "$sha_list_after" | tr -d '[:space:]')" ] && echo 1 || echo 0)" \
    "коммит, чей SHA назван в карте, не считается пропуском (до: в выборке, после: пусто)" \
    "до: $sha_list_before; после: $sha_list_after"

# ── метка жизни: отказ — тоже прогон ─────────────────────────────────────────
new_stand
map_text test wip 0 1 0 > "$T/model-map.yaml"
run_daemon "$T" >/dev/null
verdict "$([ -e "$T/hb/devmap-selfheal" ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ: отказ по удалению карточки ставит метку жизни" "метки нет, rc=$(cat "$T/rc")"

new_stand
cat > "$T/repo/scripts/claude-demon.sh" <<'SH'
#!/usr/bin/env bash
exit 3
SH
run_daemon "$T" >/dev/null
verdict "$([ -e "$T/hb/devmap-selfheal" ] && [ "$(cat "$T/rc")" = 1 ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ: сбой агента (код 3) ставит метку жизни, прогон отказывает кодом 1" "метка: $([ -e "$T/hb/devmap-selfheal" ] && echo есть || echo нет), rc=$(cat "$T/rc")"

new_stand
git -C "$T/repo" config core.hooksPath "$T/hooks"
mkdir -p "$T/hooks"; printf '#!/bin/sh\nexit 1\n' > "$T/hooks/pre-commit"; chmod +x "$T/hooks/pre-commit"
map_text test wip 1 1 0 > "$T/model-map.yaml"
run_daemon "$T" >/dev/null
verdict "$([ -e "$T/hb/devmap-selfheal" ] && [ "$(cat "$T/rc")" = 1 ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ: отказ pre-commit при коммите карты ставит метку жизни" "метка: $([ -e "$T/hb/devmap-selfheal" ] && echo есть || echo нет), rc=$(cat "$T/rc")"

# ── лимит: прогон оставляет флаг повтора для limit-watch ─────────────────────
new_stand
cat > "$T/repo/scripts/claude-demon.sh" <<'SH'
#!/usr/bin/env bash
exit 77
SH
run_daemon "$T" >/dev/null
verdict "$([ -e "$T/log/devmap/retry-after-limit" ] && [ -e "$T/hb/devmap-selfheal" ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ: отказ по лимиту оставляет флаг повтора retry-after-limit и метку жизни" \
    "флаг: $([ -e "$T/log/devmap/retry-after-limit" ] && echo есть || echo нет)"
new_stand
map_text test wip 1 1 0 > "$T/model-map.yaml"
run_daemon "$T" >/dev/null
verdict "$([ ! -e "$T/log/devmap/retry-after-limit" ] && echo 1 || echo 0)" \
    "сосед: успешный прогон флага повтора не оставляет"

# ── plan -> test законен, когда к карточке привязан коммит из выборки ───────
plan_stand() {
    new_stand
    map_text plan wip 1 0 0 > "$T/repo/dev-map.yaml"
    git -C "$T/repo" add -A
    git -C "$T/repo" -c user.name=t -c user.email=t@t commit -q -m 'карта: hr.a в плане

Dev-Map: hr.a'
    git -C "$T/repo" -c user.name=t -c user.email=t@t commit -q --allow-empty -m 'работа над hr.a'
    PLAN_SHA=$(git -C "$T/repo" log -1 --format=%H)
}
plan_stand
before=$(head_of "$T")
map_text test wip 1 0 1 | sed "s/Дописано демоном/коммит ${PLAN_SHA:0:10}/" > "$T/model-map.yaml"
run_daemon "$T" >/dev/null
verdict "$([ "$before" != "$(head_of "$T")" ] && [ "$(status_in_head "$T" hr.a)" = test ] && echo 1 || echo 0)" \
    "plan→test с привязанным SHA коммита из выборки — коммит есть" \
    "hr.a в HEAD = $(status_in_head "$T" hr.a), rc=$(cat "$T/rc")"
plan_stand
before=$(head_of "$T")
map_text test wip 1 0 1 > "$T/model-map.yaml"          # пункт без SHA
run_daemon "$T" >/dev/null
verdict "$([ "$before" != "$(head_of "$T")" ] && [ "$(status_in_head "$T" hr.a)" = plan ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ: plan→test без привязанного SHA — статус возвращён в plan, пункт закоммичен" "hr.a в HEAD = $(status_in_head "$T" hr.a)"
plan_stand
before=$(head_of "$T")
map_text done wip 1 0 1 | sed "s/Дописано демоном/коммит ${PLAN_SHA:0:10}/" > "$T/model-map.yaml"
run_daemon "$T" >/dev/null
verdict "$([ "$before" != "$(head_of "$T")" ] && [ "$(status_in_head "$T" hr.a)" = plan ] && echo 1 || echo 0)" \
    "сосед: plan→done даже с SHA не проходит — статус возвращён в plan (разрешены только plan→test и plan→wip)" "hr.a в HEAD = $(status_in_head "$T" hr.a)"
# БОЛЬНОЙ СЛУЧАЙ 17.11.2026: модель перевела две карточки plan→wip, назвав в их
# пунктах SHA коммитов из выборки (работа начата, но не проверена), — демон
# отверг всю правку с 33 пропусками и вышел с кодом 1, сторож меток назвал его
# падающим. Начатая коммитом работа в «plan» — та же ложь дашборду, что и
# сделанная, поэтому plan→wip с SHA законен так же, как plan→test.
plan_stand
before=$(head_of "$T")
map_text wip wip 1 0 1 | sed "s/Дописано демоном/коммит ${PLAN_SHA:0:10}/" > "$T/model-map.yaml"
run_daemon "$T" >/dev/null
verdict "$([ "$before" != "$(head_of "$T")" ] && [ "$(status_in_head "$T" hr.a)" = wip ] && [ "$(cat "$T/rc")" = 0 ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ 17.11: plan→wip с привязанным SHA коммита из выборки — коммит есть, код 0" \
    "hr.a в HEAD = $(status_in_head "$T" hr.a), rc=$(cat "$T/rc")"
plan_stand
before=$(head_of "$T")
map_text wip wip 1 0 1 > "$T/model-map.yaml"           # пункт без SHA
run_daemon "$T" >/dev/null
verdict "$([ "$before" != "$(head_of "$T")" ] && [ "$(status_in_head "$T" hr.a)" = plan ] && echo 1 || echo 0)" \
    "сосед: plan→wip без привязанного SHA — статус возвращён в plan" "hr.a в HEAD = $(status_in_head "$T" hr.a)"
# Промт и сторож — одно правило: промт 17.11 звал «сменой её статуса» без
# границы, а сторож пропускал одну смену. Модель шла за промтом и теряла всю
# правку. Промт обязан назвать ту же границу, что держит сторож.
verdict "$(grep -q 'Статус меняй только у карточки в plan: на wip или test' "$T/prompt.txt" 2>/dev/null \
            && ! grep -q 'сменой её статуса, если работа его меняет' "$T/prompt.txt" && echo 1 || echo 0)" \
    "БОЛЬНОЙ 17.11: промт называет ту же границу смены статуса, что держит сторож" \
    "промт: $(grep -n 'статус' "$T/prompt.txt" 2>/dev/null | head -3 | tr '\n' ' ')"

# ── модель оставила нечитаемый YAML: откат к снимку, а не падение ─────────────
new_stand
cp "$T/repo/dev-map.yaml" "$T/snapshot-before.yaml"
printf 'version: 1\nlines: [\n  - id: harness\n    tasks: {id: hr.a\n' > "$T/model-map.yaml"
before=$(head_of "$T")
out=$(run_daemon "$T")
rejected=$(find "$T/log/devmap" -maxdepth 1 -name 'rejected-*.yaml' -print -quit)
verdict "$([ "$before" = "$(head_of "$T")" ] && cmp -s "$T/snapshot-before.yaml" "$T/repo/dev-map.yaml" && echo 1 || echo 0)" \
    "БОЛЬНОЙ: битый YAML модели — коммита нет, карта в дереве = снимок до модели" \
    "HEAD сдвинут: $([ "$before" != "$(head_of "$T")" ] && echo да || echo нет), дерево = снимок: $(cmp -s "$T/snapshot-before.yaml" "$T/repo/dev-map.yaml" && echo да || echo нет)"
verdict "$([ -n "$rejected" ] && cmp -s "$T/model-map.yaml" "$rejected" && echo 1 || echo 0)" \
    "битый YAML модели сохранён в rejected-*.yaml" "отвергнутая: ${rejected:-нет}"
verdict "$(printf '%s' "$out" | grep -q 'карта после модели не читается' && [ -e "$T/hb/devmap-selfheal" ] && echo 1 || echo 0)" \
    "битый YAML назван в журнале («карта после модели не читается»), метка жизни стоит" \
    "метка: $([ -e "$T/hb/devmap-selfheal" ] && echo есть || echo нет); вывод: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"

# ── БОЛЬНОЙ СЛУЧАЙ 05.11.2026: поля-даты без кавычек, как в живой карте ───────
# yaml.safe_load отдаёт `done_at: 2026-11-05` объектом date, и сверка «до/после»,
# не умевшая его сериализовать, звала законную правку нечитаемой картой.
date_map() { map_text test wip 1 "$1" 1 | sed 's/done_at: "2026-10-18"/done_at: 2026-10-18/'; }
new_stand
date_map 0 > "$T/repo/dev-map.yaml"
git -C "$T/repo" add -A
git -C "$T/repo" -c user.name=t -c user.email=t@t commit -q -m 'карта: даты без кавычек

Dev-Map: hr.a'
git -C "$T/repo" -c user.name=t -c user.email=t@t commit -q --allow-empty -m 'работа после карты с датами'
date_map 1 > "$T/model-map.yaml"                      # модель дописала hr.n
before=$(head_of "$T")
out=$(run_daemon "$T")
verdict "$([ "$before" != "$(head_of "$T")" ] && [ "$(in_head_map "$T" hr.n)" = 1 ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ 05.11: карта с датами без кавычек, модель дописала hr.n — коммит есть" \
    "HEAD сдвинут: $([ "$before" != "$(head_of "$T")" ] && echo да || echo нет), rc=$(cat "$T/rc"); вывод: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
verdict "$(printf '%s' "$out" | grep -q 'не читается' && echo 0 || echo 1)" \
    "БОЛЬНОЙ 05.11: читаемая карта с датами не названа нечитаемой" \
    "вывод: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"

# ── тревога-карточка: агент упал — заведена, прогон удался — снята ────────────
# Писатель тревог подставной: пишет свои доводы строкой в файл стенда.
stub_alert_task() {
    printf 'import sys\nwith open(%s, "a", encoding="utf-8") as f:\n    f.write(" ".join(sys.argv[1:]) + "\\n")\n' \
        "'$T/alerts.txt'" > "$T/repo/scripts/alert-task.py"
}
new_stand
stub_alert_task
printf '#!/usr/bin/env bash\nexit 3\n' > "$T/repo/scripts/claude-demon.sh"
run_daemon "$T" >/dev/null
verdict "$(grep -q -- '--key devmap-selfheal:agent-failed' "$T/alerts.txt" 2>/dev/null && [ -e "$T/hb/devmap-selfheal" ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ: агент упал (код 3) — тревога devmap-selfheal:agent-failed заведена, метка жизни стоит" \
    "тревоги: $(tr '\n' '|' 2>/dev/null < "$T/alerts.txt" || echo нет), метка: $([ -e "$T/hb/devmap-selfheal" ] && echo есть || echo нет)"
verdict "$(grep -q -- '--resolve' "$T/alerts.txt" 2>/dev/null && echo 0 || echo 1)" \
    "сосед: упавший прогон тревогу не снимает" "тревоги: $(tr '\n' '|' 2>/dev/null < "$T/alerts.txt")"
# БОЛЬНОЙ СЛУЧАЙ 13.11.2026: чужой git держал index.lock, `git add` упал кодом 128
# под set -e — демон вышел без метки жизни и без строки в журнал, heartbeat-watch
# объявил его мёртвым на сутки. Отказ add — тот же отказ коммита: метка и код 1.
new_stand
map_text test wip 1 1 0 > "$T/model-map.yaml"
touch "$T/repo/.git/index.lock"
out_lock=$(run_daemon "$T")
verdict "$([ -e "$T/hb/devmap-selfheal" ] && [ "$(cat "$T/rc")" = 1 ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ: занятый index.lock при git add — метка жизни и код 1, не 128" \
    "метка: $([ -e "$T/hb/devmap-selfheal" ] && echo есть || echo нет), rc=$(cat "$T/rc")"
verdict "$(printf '%s' "$out_lock" | grep -q 'НЕ прошёл' && echo 1 || echo 0)" \
    "БОЛЬНОЙ: отказ add назван в журнале демона"

new_stand
stub_alert_task
map_text test wip 1 1 0 > "$T/model-map.yaml"
run_daemon "$T" >/dev/null
verdict "$(grep -qx -- '--resolve devmap-selfheal:agent-failed' "$T/alerts.txt" 2>/dev/null && echo 1 || echo 0)" \
    "успешный прогон снимает тревогу devmap-selfheal:agent-failed" \
    "тревоги: $(tr '\n' '|' 2>/dev/null < "$T/alerts.txt" || echo нет), rc=$(cat "$T/rc")"
verdict "$(grep -q -- '--key' "$T/alerts.txt" 2>/dev/null && echo 0 || echo 1)" \
    "сосед: успешный прогон тревогу не заводит" "тревоги: $(tr '\n' '|' 2>/dev/null < "$T/alerts.txt")"

echo
echo "всего путей: $total, неудач: $bad"
[ "$bad" = 0 ]
