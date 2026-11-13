#!/usr/bin/env bash
# Проба Stop-хука stop_reminder.sh: «изменённых файлов без коммита» не считает
# файлы, которые пишут ДЕМОНЫ.
#
# Беда (3.4 отчёта соседней установки, 18.10.2026): хук горел каждый ход, потому
# что считал работой без коммита `память/.tasks-triaged` и `docs/приёмка/*` —
# их пишет и коммитит демон, смене там делать нечего. Сигнал, который горит
# всегда, учит себя не читать ([[a-signal-that-does-not-discriminate]]).
#
# Правило: пути демонов — ДАННЫЕ, ключ DAEMON_OWNED_PATHS в harness.conf
# (через пробел; путь, кончающийся «/», — каталог целиком). Нет ключа —
# считается всё, как раньше.
#
# Стенд: свой git-репозиторий, свои install.conf и harness.conf, свой LOG_DIR;
# PROJECT_DIR и LOG_DIR заданы и в окружении — скрипт читает конфиг ПОСЛЕ
# окружения ([[the-probe-played-on-production]]). Каждый случай — свежий
# каталог mktemp -d.
#
# Дверь для мутаций: STOP_REMINDER_UNDER_TEST — путь к испорченной копии.
#
#   bash scripts/hooks/test_stop_reminder.sh
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
HOOK="${STOP_REMINDER_UNDER_TEST:-$HERE/stop_reminder.sh}"
[ -f "$HOOK" ] || { echo "ПРОБА СЛОМАНА: нет $HOOK"; exit 1; }

# Хук берёт конфиг только своей дверью (АР-3): HARNESS_CONF/HARNESS_INSTALL_CONF из
# окружения он не слушает, а настоящий файл хука пошёл бы на паспорт машины и её LOG_DIR.
# Поэтому зовём КОПИЮ хука вне PROJECT_DIR машины (с lib/ и deploy_guard.py рядом, как в
# раскладке) и называем паспорт стенда HARNESS_DOOR_STAND_HOME. HARNESS_INSTALL_CONF и
# HARNESS_CONF стенда остаются в окружении для детей хука (work-queue.py и др.).
SRC_SCRIPTS="$HERE/.."
[ -d "$SRC_SCRIPTS/lib" ] || SRC_SCRIPTS="$HERE/../scripts"
HOOK_SRC="$HOOK"
door_copy() {  # $1 — каталог копии: hooks/stop_reminder.sh, lib/, deploy_guard.py
    mkdir -p "$1/hooks"
    cp -r "$SRC_SCRIPTS/lib" "$1/lib"
    cp "$SRC_SCRIPTS/deploy_guard.py" "$1/"
    cp "$HOOK_SRC" "$1/hooks/stop_reminder.sh"
}
DOOR_COPY=$(mktemp -d)
door_copy "$DOOR_COPY"
HOOK="$DOOR_COPY/hooks/stop_reminder.sh"
STAND_HOOK=""   # своя копия хука стенда (ворота-заглушка рядом с ней); пусто — общая

DIRTY_WORDS="изменённых файлов без коммита"
bad=0
total=0
STANDS=()
cleanup() {
    local d
    for d in "${STANDS[@]}"; do rm -rf "$d"; done
    rm -rf "$DOOR_COPY"
}
trap cleanup EXIT

# Стенд в главной оболочке: из $(…) список STANDS не вернулся бы.
# $1 — строка ключа для harness.conf (пусто — ключа нет вовсе).
new_stand() {
    T=$(mktemp -d)
    STANDS+=("$T")
    STAND_HOOK=""
    local R="$T/repo"
    mkdir -p "$R/память" "$R/docs/приёмка" "$R/docs/приёмка-старая" "$R/scripts" "$T/log"
    printf 'разобрано\n' > "$R/память/.tasks-triaged"
    printf 'очередь\n' > "$R/docs/приёмка/очередь-владельца.md"
    printf 'старое\n' > "$R/docs/приёмка-старая/запись.md"
    printf 'true\n' > "$R/scripts/work.sh"
    git -C "$T" init -q -b master repo
    git -C "$R" config user.name стенд
    git -C "$R" config user.email stand@example.invalid
    git -C "$R" add -A
    git -C "$R" commit -q -m 'стенд'
    printf 'PROJECT_DIR="%s"\nLOG_DIR="%s/log"\n' "$R" "$T" > "$T/install.conf"
    printf '%s\n' "${1:-}" > "$T/harness.conf"
}

touch_file() { printf 'правка\n' >> "$T/repo/$1"; }

# Прогон хука: печатает stderr, код — в $T/rc.
run_hook() {
    local rc=0 out
    out=$(cd "$T" && printf '{}' | env -u HANDOVER_MAX_AGE_MIN -u DEVMAP_TRACE_FRESH_MIN \
            HARNESS_DOOR_STAND_HOME="$T" \
            HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
            PROJECT_DIR="$T/repo" LOG_DIR="$T/log" \
            timeout 60 bash "${STAND_HOOK:-$HOOK}" 2>&1 >/dev/null) || rc=$?
    printf '%s' "$rc" > "$T/rc"
    printf '%s' "$out"
}

verdict() {  # $1 = 1 (верно) / 0, $2 = имя, $3 = подробность
    total=$((total + 1))
    if [ "$1" = 1 ]; then
        echo "  ок    $2"
    else
        echo "  ПЛОХО $2${3:+ — $3}"
        bad=$((bad + 1))
    fi
}

KEY='DAEMON_OWNED_PATHS="память/.tasks-triaged docs/приёмка/"'

# ── БОЛЬНОЙ СЛУЧАЙ: изменён только файл демона ─────────────────────────────
new_stand "$KEY"
touch_file "память/.tasks-triaged"
out=$(run_hook)
verdict "$(printf '%s' "$out" | grep -q "$DIRTY_WORDS" && echo 0 || echo 1)" \
    "БОЛЬНОЙ 3.4: изменён только память/.tasks-triaged (пишет демон) — не «работа без коммита»" \
    "хук сказал: $(printf '%s' "$out" | grep "$DIRTY_WORDS")"
verdict "$([ "$(cat "$T/rc")" = 0 ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ 3.4: других хвостов нет — ход закончен (код 0)" "код $(cat "$T/rc")"

# ── БОЛЬНОЙ СЛУЧАЙ: каталог демона (кириллический путь — git его экранирует) ─
new_stand "$KEY"
touch_file "docs/приёмка/очередь-владельца.md"
out=$(run_hook)
verdict "$(printf '%s' "$out" | grep -q "$DIRTY_WORDS" && echo 0 || echo 1)" \
    "БОЛЬНОЙ 3.4: файл каталога docs/приёмка/ — не «работа без коммита»" \
    "хук сказал: $(printf '%s' "$out" | grep "$DIRTY_WORDS")"

# ── сосед: правка смены считается, как раньше ──────────────────────────────
new_stand "$KEY"
touch_file "scripts/work.sh"
out=$(run_hook)
verdict "$(printf '%s' "$out" | grep -q "$DIRTY_WORDS: 1\$" && echo 1 || echo 0)" \
    "правка смены (scripts/work.sh) — «изменённых файлов без коммита: 1»" \
    "хук сказал: ${out:-ничего}"

# ── сосед: смесь — считается только правка смены ──────────────────────────
new_stand "$KEY"
touch_file "память/.tasks-triaged"
touch_file "docs/приёмка/очередь-владельца.md"
touch_file "scripts/work.sh"
out=$(run_hook)
verdict "$(printf '%s' "$out" | grep -q "$DIRTY_WORDS: 1\$" && echo 1 || echo 0)" \
    "смесь из двух файлов демонов и правки смены — счёт 1" \
    "хук сказал: $(printf '%s' "$out" | grep "$DIRTY_WORDS")"

# ── сосед: каталог по префиксу, а не по началу строки ──────────────────────
new_stand "$KEY"
touch_file "docs/приёмка-старая/запись.md"
out=$(run_hook)
verdict "$(printf '%s' "$out" | grep -q "$DIRTY_WORDS: 1\$" && echo 1 || echo 0)" \
    "docs/приёмка-старая/ — не каталог docs/приёмка/, считается" \
    "хук сказал: ${out:-ничего}"

# ── сосед: список — данные; ключа нет — не исключается ничего ──────────────
new_stand ""
touch_file "память/.tasks-triaged"
out=$(run_hook)
verdict "$(printf '%s' "$out" | grep -q "$DIRTY_WORDS: 1\$" && echo 1 || echo 0)" \
    "ключа DAEMON_OWNED_PATHS нет — файл считается, список не вшит в код" \
    "хук сказал: ${out:-ничего}"

# ══ «СЛЕДУЮЩАЯ РАБОТА» не называет сделанную задачу (п.6 отчёта чужого сервера 25.10) ══
# Беда: хук после каждого хода писал «<id> — внимание владельца: «первым
# делом» — бери её» про карточку в test (выкачена, ждёт проверки владельца).
# За смену ~20 раз. Сигнал истинен и у здоровой системы — сведений не несёт
# ([[a-signal-that-does-not-discriminate]]).
# Правило: test без провала проверки хук «бери её» не называет — даже со
# словом владельца и незакрытыми пунктами чеклиста; wip, plan и test с
# провалом проверки — называет.
# Стенд: копия прибора очереди (work-queue.py + lib/) в scripts/ стенда — хук
# зовёт его оттуда; своя карта, свой журнал внимания в LOG_DIR стенда.
# Дверь для мутаций: STOP_REMINDER_QUEUE_UNDER_TEST — путь к копии прибора.
QUEUE_SRC="${STOP_REMINDER_QUEUE_UNDER_TEST:-}"
if [ -z "$QUEUE_SRC" ]; then
    for cand in "$HERE/../work-queue.py" "$HERE/../scripts/work-queue.py"; do
        [ -f "$cand" ] && { QUEUE_SRC="$cand"; break; }
    done
fi
QUEUE_LIB="$(dirname "$(readlink -f "$HERE/../work-queue.py" 2>/dev/null || true)")/lib"
[ -d "$QUEUE_LIB" ] || QUEUE_LIB="$HERE/../scripts/lib"
[ -f "$QUEUE_SRC" ] && [ -d "$QUEUE_LIB" ] \
    || { echo "ПРОБА СЛОМАНА: нет прибора очереди ($QUEUE_SRC) или lib ($QUEUE_LIB)"; exit 1; }

ATTENTION_ID="1790000000.5"

# $1 — YAML карточки (одна задача, отступ 4). Стенд с картой, закоммиченной,
# чтобы хвост «изменённых файлов без коммита» не смешивался со случаем.
queue_stand() {
    new_stand ""
    local R="$T/repo"
    cp "$QUEUE_SRC" "$R/scripts/work-queue.py"
    cp -r "$QUEUE_LIB" "$R/scripts/lib"
    # Байткод не коммитим: прибор перепишет .pyc, и «без коммита» смешается со случаем.
    rm -rf "$R/scripts/lib/__pycache__"
    printf 'queue: []\nlines:\n- id: h\n  tasks:\n%s\n' "$1" > "$R/dev-map.yaml"
    printf '{"id": "%s", "слово": "первым делом"}\n' "$ATTENTION_ID" > "$T/log/внимание.jsonl"
    git -C "$R" add -A
    git -C "$R" commit -q -m 'карта стенда'
}

card() {  # $1 id, $2 status, $3 доп. строки (уже с отступом)
    printf '  - id: %s\n    status: %s\n    источник: "inbox:%s"\n    checklist:\n    - label: выкатить\n      done: false\n    - label: проверить на живом\n      done: false\n%s' \
        "$1" "$2" "$ATTENTION_ID" "${3:-}"
}

names_it() {  # $1 вывод, $2 id → 1 если хук велит «бери её» про эту карточку
    printf '%s' "$1" | grep -F -- "$2" | grep -qF "бери её" && echo 1 || echo 0
}

# ── БОЛЬНОЙ СЛУЧАЙ п.6: test, слово владельца «первым делом», пункты открыты ─
queue_stand "$(card pr.vykachena test)"
out=$(run_hook)
verdict "$([ "$(names_it "$out" pr.vykachena)" = 0 ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ п.6: задача в test (ждёт проверки владельца) — «бери её» нет" \
    "хук сказал: $(printf '%s' "$out" | grep -F pr.vykachena)"

# ── тот же test без слова владельца — тоже молчит ─────────────────────────
queue_stand "$(card pr.vykachena test)"
: > "$T/log/внимание.jsonl"
out=$(run_hook)
verdict "$([ "$(names_it "$out" pr.vykachena)" = 0 ] && echo 1 || echo 0)" \
    "задача в test без слова владельца — «бери её» нет" \
    "хук сказал: $(printf '%s' "$out" | grep -F pr.vykachena)"

# ── соседи: начатая и плановая работа называются ──────────────────────────
queue_stand "$(card pr.nachata wip)"
out=$(run_hook)
verdict "$(names_it "$out" pr.nachata)" \
    "задача в wip — хук называет её «бери её»" "хук сказал: ${out:-ничего}"

queue_stand "$(card pr.v-plane plan)"
out=$(run_hook)
verdict "$(names_it "$out" pr.v-plane)" \
    "задача в plan — хук называет её «бери её»" "хук сказал: ${out:-ничего}"

# ── сосед: test с провалом проверки — починка за сменой, называется ────────
queue_stand "$(card pr.provalena test '    провал_проверки: "смоук красный"
')"
out=$(run_hook)
verdict "$(names_it "$out" pr.provalena)" \
    "задача в test с провалом проверки — хук называет её «бери её»" "хук сказал: ${out:-ничего}"

# ══ wip со свежим следом — «идёт», а не «доведи до test» ══════════════════════
# Беда: хук требовал «доведи до test или назови блокер» и «бери её» от задачи,
# по которой смена только что закоммитила, — сигнал горел каждый ход.
# Правило: след (коммит с трейлером Dev-Map: <id> или правка
# .tasks/<id>/журнал.md) не старше DEVMAP_TRACE_FRESH_MIN (умолчание 120) —
# строка «· идёт: <id>, последний след N мин назад»; поле blocked — строка
# «· ждёт: <id> — <текст>»; без следа и блокера — «доведи до test». Строки «·»
# ход не держат: код 0.
NOTE_RE='· идёт: %s, последний след [0-9]+ мин назад'

journal_aged() {  # $1 id, $2 возраст правки журнала в секундах
    mkdir -p "$T/repo/.tasks/$1"
    printf 'шаг\n' > "$T/repo/.tasks/$1/журнал.md"
    touch -d "@$(( $(date +%s) - $2 ))" "$T/repo/.tasks/$1/журнал.md"
}

# Вердикт «идёт»: строка «идёт» есть, «доведи» и «бери её» про задачу нет, код 0.
judge_active() {  # $1 вывод, $2 id, $3 имя случая
    # shellcheck disable=SC2059
    verdict "$(printf '%s' "$1" | grep -qE "$(printf "$NOTE_RE" "$2")" && echo 1 || echo 0)" \
        "$3 — «· идёт: $2, последний след N мин назад»" "хук сказал: ${1:-ничего}"
    verdict "$(printf '%s' "$1" | grep -qF "доведи до test" && echo 0 || echo 1)" \
        "$3 — нет «доведи до test»" "хук сказал: $(printf '%s' "$1" | grep -F "доведи")"
    verdict "$([ "$(names_it "$1" "$2")" = 0 ] && echo 1 || echo 0)" \
        "$3 — нет «бери её»" "хук сказал: $(printf '%s' "$1" | grep -F "бери её")"
    verdict "$([ "$(cat "$T/rc")" = 0 ] && echo 1 || echo 0)" \
        "$3 — только строки «·», ход закончен (код 0)" "код $(cat "$T/rc"), хук сказал: ${1:-ничего}"
}

judge_abandoned() {  # $1 вывод, $2 id, $3 имя случая
    verdict "$(printf '%s' "$1" | grep -qF "1 задач(и) в статусе wip — доведи до test или назови блокер" && echo 1 || echo 0)" \
        "$3 — «доведи до test или назови блокер»" "хук сказал: ${1:-ничего}"
    verdict "$(printf '%s' "$1" | grep -qF "идёт: $2" && echo 0 || echo 1)" \
        "$3 — строки «идёт» нет" "хук сказал: $(printf '%s' "$1" | grep -F "идёт:")"
    verdict "$([ "$(cat "$T/rc")" = 2 ] && echo 1 || echo 0)" \
        "$3 — ход держится (код 2)" "код $(cat "$T/rc")"
}

# ── свежий коммит с трейлером Dev-Map ─────────────────────────────────────
queue_stand "$(card pr.idet wip)"
git -C "$T/repo" commit -q --allow-empty -m 'шаг работы' -m 'Dev-Map: pr.idet'
judge_active "$(run_hook)" pr.idet "свежий коммит с трейлером Dev-Map"

# ── свежая правка журнала без коммита ─────────────────────────────────────
queue_stand "$(card pr.idet wip)"
journal_aged pr.idet 300
judge_active "$(run_hook)" pr.idet "журнал задачи правлен 5 мин назад, коммита нет"

# ── граница порога: след ровно 120 мин назад — ещё идёт ───────────────────
queue_stand "$(card pr.idet wip)"
journal_aged pr.idet $(( 120 * 60 + 20 ))
judge_active "$(run_hook)" pr.idet "граница: след 120 мин назад (порог 120)"

# ── за границей: 121 мин — брошена ────────────────────────────────────────
queue_stand "$(card pr.idet wip)"
journal_aged pr.idet $(( 121 * 60 + 20 ))
judge_abandoned "$(run_hook)" pr.idet "за границей: след 121 мин назад"

# ── брошенная: следа нет вовсе ────────────────────────────────────────────
queue_stand "$(card pr.broshena wip)"
judge_abandoned "$(run_hook)" pr.broshena "брошенная wip без следа"

# ── порог — данные: DEVMAP_TRACE_FRESH_MIN=10, след 30 мин — брошена ──────
queue_stand "$(card pr.idet wip)"
printf 'DEVMAP_TRACE_FRESH_MIN=10\n' > "$T/harness.conf"
journal_aged pr.idet $(( 30 * 60 ))
judge_abandoned "$(run_hook)" pr.idet "порог из конфига 10 мин, след 30 мин"

# ── блокер в карточке — «ждёт», ход не держится ───────────────────────────
queue_stand "$(card pr.zhdet wip '    blocked: "ждёт ответа владельца"
')"
out=$(run_hook)
verdict "$(printf '%s' "$out" | grep -qF "· ждёт: pr.zhdet — ждёт ответа владельца" && echo 1 || echo 0)" \
    "блокер — «· ждёт: pr.zhdet — ждёт ответа владельца»" "хук сказал: ${out:-ничего}"
verdict "$(printf '%s' "$out" | grep -qF "доведи до test" && echo 0 || echo 1)" \
    "блокер — нет «доведи до test»" "хук сказал: $(printf '%s' "$out" | grep -F "доведи")"
verdict "$([ "$(cat "$T/rc")" = 0 ] && echo 1 || echo 0)" \
    "блокер — только строки «·», код 0" "код $(cat "$T/rc"), хук сказал: ${out:-ничего}"

# ══ Неотправленные коммиты: напоминание судят ворота, согласие — только кнопка ══
# Слово владельца 05.11.2026: «все вопросы по поводу моего Да должны приходить
# кнопками в телегу». Хук спрашивает ворота без --просить (кнопку просит только
# дверь) и при отказе ведёт к ask-consent, а не к «спроси да» текстом.
# Ворота стенда — заглушка: пишет свои доводы, отвечает кодом из файла. Хук зовёт ворота
# рядом с СОБОЙ (readlink -f), поэтому заглушка лежит в своей копии хука стенда.
push_stand() {  # $1 = код ворот стенда
    new_stand "$KEY"
    local R="$T/repo"
    door_copy "$T/door"
    cat > "$T/door/deploy_guard.py" <<'PYEOF'
import pathlib, sys
стенд = pathlib.Path(__file__).resolve().parents[1]
(стенд / "guard-args").write_text(" ".join(sys.argv[1:]), encoding="utf-8")
sys.exit(int((стенд / "guard-rc").read_text()))
PYEOF
    STAND_HOOK="$T/door/hooks/stop_reminder.sh"
    printf '%s' "$1" > "$T/guard-rc"
    git init -q --bare -b master "$T/remote.git"
    git -C "$R" remote add origin "$T/remote.git"
    git -C "$R" push -q -u origin master 2>/dev/null
    git -C "$R" commit -q --allow-empty -m 'неотправленный'
}

push_stand 1
out=$(run_hook)
verdict "$(printf '%s' "$out" | grep -qF 'нужна кнопка «Да» владельца' \
        && printf '%s' "$out" | grep -qF 'ask-consent.py --вид пуш' && echo 1 || echo 0)" \
    "пуш: ворота закрыты — напоминание ведёт к кнопке через ask-consent" "хук сказал: ${out:-ничего}"
verdict "$(printf '%s' "$out" | grep -qF 'сторож выката: можно' && echo 0 || echo 1)" \
    "пуш: ворота закрыты — «можно» не сказано" "хук сказал: ${out:-ничего}"
verdict "$([ "$(cat "$T/guard-args" 2>/dev/null)" = "--действие push --только-согласие" ] && echo 1 || echo 0)" \
    "пуш: ворота спрошены без --просить — хук кнопку не просит" "доводы: $(cat "$T/guard-args" 2>/dev/null || echo 'ворота не спрошены')"

push_stand 0
out=$(run_hook)
verdict "$(printf '%s' "$out" | grep -qF '1 коммит(ов) — git push (сторож выката: можно)' && echo 1 || echo 0)" \
    "пуш: ворота открыты — «git push (сторож выката: можно)»" "хук сказал: ${out:-ничего}"

echo
echo "всего путей: $total, неудач: $bad"
[ "$bad" = 0 ]
