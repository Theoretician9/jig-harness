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

DIRTY_WORDS="изменённых файлов без коммита"
bad=0
total=0
STANDS=()
cleanup() {
    local d
    for d in "${STANDS[@]}"; do rm -rf "$d"; done
}
trap cleanup EXIT

# Стенд в главной оболочке: из $(…) список STANDS не вернулся бы.
# $1 — строка ключа для harness.conf (пусто — ключа нет вовсе).
new_stand() {
    T=$(mktemp -d)
    STANDS+=("$T")
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
    out=$(cd "$T" && printf '{}' | env -u HANDOVER_MAX_AGE_MIN \
            HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
            PROJECT_DIR="$T/repo" LOG_DIR="$T/log" \
            timeout 60 bash "$HOOK" 2>&1 >/dev/null) || rc=$?
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

echo
echo "всего путей: $total, неудач: $bad"
[ "$bad" = 0 ]
