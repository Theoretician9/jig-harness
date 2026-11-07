#!/usr/bin/env bash
#
# test_mutation_probe_limits.sh — проба трёх пределов мутационной пробы и её
# ночного демона на стенде: (1) предел одного запуска пробы, (2) мягкая
# остановка по сроку от демона и жёлтая весть, (3) выпавшие записи и упавший
# читатель данных не дают зелёного итога. Прибор настоящий, данные, кэш,
# LOG_DIR и TMPDIR — свои во временном каталоге; отправка и задачи подставные.
# Двери для мутаций: MUTATION_PROBE_UNDER_TEST, NIGHTLY_MUTATIONS_UNDER_TEST.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PROBE="${MUTATION_PROBE_UNDER_TEST:-$HERE/mutation-probe.sh}"
DAEMON="${NIGHTLY_MUTATIONS_UNDER_TEST:-$HERE/../harness/demons/nightly-mutations.sh}"
for f in "$PROBE" "$DAEMON"; do
    [ -f "$f" ] || { echo "ПРОБА КРАСНАЯ: механизма нет ($f)"; exit 1; }
done

# Маркер стоит в пути стенда, и TMPDIR прибора лежит под ним: зависшая проба
# стенда находится по маркеру и только по нему (рядом может идти боевой прогон).
MARK="mplim-$$-$RANDOM"
T="$(mktemp -d "/tmp/$MARK.XXXX")"
cleanup() { pkill -9 -f "$MARK" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT

total=0; failed=0
check() {  # $1 = имя случая, $2 = 0/1 (1 — выполнено), $3 = подробность при неудаче
    total=$((total + 1))
    if [ "$2" = 1 ]; then printf '  ок    %s\n' "$1"
    else printf '  ПЛОХО %s: %s\n' "$1" "$3"; failed=$((failed + 1)); fi
}
has() { grep -qF -- "$1" "$2" 2>/dev/null && echo 1 || echo 0; }
hasnt() { grep -qF -- "$1" "$2" 2>/dev/null && echo 0 || echo 1; }
yes_if() { "$@" && echo 1 || echo 0; }
tail_of() { grep -v '^cp: ' "$1" 2>/dev/null | tail -4 | tr '\n' '|'; }

mkdir -p "$T/tmp" "$T/home" "$T/hb" "$T/proekt/scripts" "$T/log1" "$T/logd"
: > "$T/install.conf"; : > "$T/harness.conf"; : > "$T/none.yaml"
cat > "$T/proekt/scripts/tg_send.sh" <<'TG'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TG_LOG"
TG
chmod +x "$T/proekt/scripts/tg_send.sh"
cat > "$T/proekt/scripts/alert-task.py" <<'ALERT'
import os, sys
with open(os.environ["ALERT_LOG"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
ALERT

# Механизмы стенда: мутация X=1→X=2 ловится (green, green2), вешает пробу
# (hang) или проходит незамеченной (hole).
printf 'X=1\n[ "$X" = 1 ]\n' > "$T/green.sh"
printf 'X=1\n[ "$X" = 1 ]\n' > "$T/green2.sh"
printf 'X=1\n[ "$X" = 1 ] || sleep 999\n' > "$T/hang.sh"
printf 'X=1\ntrue\n' > "$T/hole.sh"

entry() {  # $1 = id/файл стенда; запись с одной парной мутацией
    printf '  - id: s-%s\n    файл: %s\n    доказательство: bash %s\n    мутация: "s/X=1/X=2/"\n    мутация_случай: "случай-%s"\n' \
        "$1" "$T/$1.sh" "$T/$1.sh" "$1"
}
inv() {  # $1 = имя файла данных, дальше id записей
    local name="$1"; shift
    { echo 'инварианты:'; for id in "$@"; do entry "$id"; done; } > "$T/$name.yaml"
}
inv hang green hang
inv two green green2
inv only2 green2
inv only1 green
inv hole hole
inv holetwo hole green2
# Две мутации при одном названном случае — запись не разбирается.
inv broken green
printf '  - id: s-broken\n    файл: %s\n    доказательство: bash %s\n    мутация: ["s/X=1/X=2/", "s/X=1/X=3/"]\n    мутация_случай: ["одна"]\n' \
    "$T/green2.sh" "$T/green2.sh" >> "$T/broken.yaml"
# Читатель падает ПОСЛЕ первой строки таблицы (у второй записи нет
# доказательства): без учёта его кода прибор отчитался бы зелёным по огрызку.
inv crash green
printf '  - id: s-crash\n    файл: %s\n    мутация: "s/X=1/X=2/"\n    мутация_случай: "без доказательства"\n' \
    "$T/green2.sh" >> "$T/crash.yaml"

COMMON=(HARNESS_HOME="$T/home" HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf"
        HARNESS_PROBE_MUTATIONS="$T/none.yaml" MUTATION_RUN_TIMEOUT_S=2 TMPDIR="$T/tmp" HEARTBEAT_DIR="$T/hb")
UNSET=(-u MUTATION_DEADLINE_TS -u MUT_CACHE_DAYS -u HARNESS_EVENTS_STATE -u NIGHTLY_MUTATIONS_MAX_MIN
       -u MUTATION_SOFT_STOP_MIN_BEFORE -u NIGHTLY_MUTATIONS_ALERT_GAP_H)

# Прогон прибора: $1 = данные, $2 = LOG_DIR, дальше KEY=VAL. Внешний предел 60 с —
# прибор без своего предела висел бы на sleep 999, и это красное, а не ожидание.
run_probe() {
    local data="$1" logdir="$2"; shift 2
    env "${UNSET[@]}" "${COMMON[@]}" LOG_DIR="$logdir" HARNESS_INVARIANTS="$T/$data.yaml" "$@" \
        timeout 60 bash "$PROBE" > "$T/out" 2>&1 < /dev/null
    RC=$?
}

# Прогон демона с настоящим прибором: $1 = данные, $2 = предел (мин),
# $3 = запас мягкой остановки (мин), $4 = MUT_CACHE_DAYS.
run_daemon() {
    : > "$T/tg.log"; : > "$T/alert.log"
    rm -f "$T/logd"/nightly-mutations.alert*-ts "$T/logd"/nightly-mutations-*.log
    env "${UNSET[@]}" "${COMMON[@]}" LOG_DIR="$T/logd" PROJECT_DIR="$T/proekt" \
        TG_LOG="$T/tg.log" ALERT_LOG="$T/alert.log" HARNESS_INVARIANTS="$T/$1.yaml" \
        NIGHTLY_MUTATIONS_MAX_MIN="$2" MUTATION_SOFT_STOP_MIN_BEFORE="$3" MUT_CACHE_DAYS="$4" \
        NIGHTLY_MUTATIONS_CMD="bash $PROBE" \
        timeout 90 bash "$DAEMON" > "$T/daemon.log" 2>&1 < /dev/null
    DRC=$?
}
tg_text() { tr '\n' ' ' < "$T/tg.log"; }

# ── (1) предел одного запуска пробы ─────────────────────────────────────────
started=$SECONDS
run_probe hang "$T/log1" MUT_CACHE_DAYS=7
took=$((SECONDS - started))
check "1 БОЛЬНОЙ: зависшая проба обрезана, прибор кончил сам (<40 с)" "$(yes_if [ "$took" -lt 40 ] && [ "$RC" != 124 ])" \
      "за $took с, код $RC, хвост: $(tail_of "$T/out")"
check "1 БОЛЬНОЙ: строка «таймаут: запуск пробы дольше 2 с» с именем случая" \
      "$(has 'ПЛОХО таймаут: запуск пробы дольше 2 с: s-hang: случай-hang' "$T/out")" "хвост: $(tail_of "$T/out")"
check "1 БОЛЬНОЙ: таймаут посчитан дырой (дыр 1, код 1)" \
      "$([ "$RC" = 1 ] && grep -q 'покраснели как надо 1, дыр 1,' "$T/out" && echo 1 || echo 0)" \
      "код $RC, хвост: $(tail_of "$T/out")"
check "1 зелёная запись кэширована (кэш вообще пишется)" "$(has 's-green: случай-green' "$T/log1/mutations-cache.tsv")" \
      "в кэше: $(cut -f3- "$T/log1/mutations-cache.tsv" 2>/dev/null | tr '\n' '|')"
check "1 БОЛЬНОЙ: таймаут в кэш НЕ записан" "$(hasnt 's-hang' "$T/log1/mutations-cache.tsv")" \
      "в кэше: $(cut -f3- "$T/log1/mutations-cache.tsv" 2>/dev/null | tr '\n' '|')"
run_probe hang "$T/log1" MUT_CACHE_DAYS=7
check "1 БОЛЬНОЙ: повтор с кэшем гоняет зависшую снова, а не «известная из кэша»" \
      "$([ "$RC" = 1 ] && grep -q 'ПЛОХО таймаут: запуск пробы дольше 2 с: s-hang' "$T/out" \
          && grep -q 'ок    из кэша.*s-green' "$T/out" && echo 1 || echo 0)" "код $RC, хвост: $(tail_of "$T/out")"

# ── (2) мягкая остановка ────────────────────────────────────────────────────
# Срок в прошлом: s-green отвечает кэш из (1), s-green2 не прогоняется.
run_probe two "$T/log1" MUT_CACHE_DAYS=7 MUTATION_DEADLINE_TS=1
check "2 БОЛЬНОЙ: после срока — «НЕДОГНАНА: не прогнано 1 из 2 (по сроку 1, выпало 0)»" \
      "$(has '=== мутационная проба НЕДОГНАНА: не прогнано 1 из 2 (по сроку 1, выпало 0) ===' "$T/out")" \
      "хвост: $(tail_of "$T/out")"
check "2 БОЛЬНОЙ: ответ из кэша отдан и после срока" "$(yes_if grep -q 'ок    из кэша.*s-green: случай-green' "$T/out")" \
      "хвост: $(tail_of "$T/out")"
check "2 БОЛЬНОЙ: непрогнанная не прогнана" "$(hasnt 's-green2' "$T/out")" "хвост: $(tail_of "$T/out")"
check "2 недогон по сроку — не отказ: дыр 0, код 0" \
      "$([ "$RC" = 0 ] && grep -q 'покраснели как надо 1, дыр 0, из кэша 1' "$T/out" && echo 1 || echo 0)" \
      "код $RC, хвост: $(tail_of "$T/out")"
run_probe only2 "$T/log1" MUT_CACHE_DAYS=0 MUTATION_DEADLINE_TS=$(( $(date +%s) + 600 ))
check "2 срок впереди: прогнано всё, «НЕДОГНАНА» нет" \
      "$([ "$RC" = 0 ] && grep -q 'мутация поймана: s-green2' "$T/out" && ! grep -q 'НЕДОГНАНА' "$T/out" && echo 1 || echo 0)" \
      "код $RC, хвост: $(tail_of "$T/out")"

# Демон: предел 1 мин, запас 1 мин — срок наступает сразу. s-green из кэша.
cp "$T/log1/mutations-cache.tsv" "$T/logd/mutations-cache.tsv"
run_daemon two 1 1 7
check "2 БОЛЬНОЙ демон: K>0 без дыр — жёлтая весть «прогнано 1 из 2 … следующий запуск»" \
      "$([ -s "$T/tg.log" ] && grep -q 'прогнано 1 из 2' "$T/tg.log" && grep -q 'следующий запуск' "$T/tg.log" && echo 1 || echo 0)" \
      "весть: «$(tg_text)», журнал: $(tail_of "$T/daemon.log")"
check "2 БОЛЬНОЙ демон: жёлтая весть без карточки nightly-mutations:broken и :red" \
      "$([ -s "$T/tg.log" ] && ! grep -q 'nightly-mutations:' "$T/tg.log" && echo 1 || echo 0)" "весть: «$(tg_text)»"
check "2 демон: жёлтое названо в журнале, код 0" \
      "$([ "$DRC" = 0 ] && grep -q 'жёлтое: прогнано 1 из 2' "$T/daemon.log" && echo 1 || echo 0)" \
      "код $DRC, журнал: $(tail_of "$T/daemon.log")"

# Демон: дыра (из кэша) при недогоне — красное, а не жёлтое.
run_probe hole "$T/logd" MUT_CACHE_DAYS=7
run_daemon holetwo 1 1 7
check "2 БОЛЬНОЙ демон: дыры при K>0 — красная весть nightly-mutations:red" \
      "$([ "$DRC" = 0 ] && grep -q -- '--task nightly-mutations:red' "$T/tg.log" && grep -q 'Не прогнано: 1 из 2' "$T/tg.log" && echo 1 || echo 0)" \
      "код $DRC, весть: «$(tg_text)»"
check "2 БОЛЬНОЙ демон: при дырах жёлтого текста нет" "$(hasnt 'следующий запуск' "$T/tg.log")" "весть: «$(tg_text)»"

# Демон: всё прогнано, дыр нет — молчит и снимает обе карточки.
run_daemon only1 10 1 0
check "2 демон: K=0, дыр нет — вести нет" "$(yes_if [ ! -s "$T/tg.log" ])" "весть: «$(tg_text)»"
check "2 демон: K=0, дыр нет — сняты nightly-mutations:red и :broken, код 0" \
      "$([ "$DRC" = 0 ] && grep -qE -- '--resolve nightly-mutations:red( |$)' "$T/alert.log" \
          && grep -qE -- '--resolve nightly-mutations:broken( |$)' "$T/alert.log" && echo 1 || echo 0)" \
      "код $DRC, писатель звали: «$(tr '\n' ';' < "$T/alert.log")»"

# ── (3) немое выпадение ─────────────────────────────────────────────────────
run_probe broken "$T/log1" MUT_CACHE_DAYS=0
check "3 БОЛЬНОЙ: непарная запись названа «выпало мутаций 2»" \
      "$(has 'ПЛОХО запись не разобрана, выпало мутаций 2: s-broken' "$T/out")" "хвост: $(tail_of "$T/out")"
check "3 БОЛЬНОЙ: выпадение в итоге «НЕДОГНАНА … выпало 2», код ненулевой" \
      "$([ "$RC" != 0 ] && grep -q 'НЕДОГНАНА: не прогнано 2 из 3 (по сроку 0, выпало 2)' "$T/out" && echo 1 || echo 0)" \
      "код $RC, хвост: $(tail_of "$T/out")"
run_probe crash "$T/log1" MUT_CACHE_DAYS=0
check "3 БОЛЬНОЙ: упавший читатель назван «читатель данных упал»" \
      "$(has 'ПЛОХО читатель данных упал (код 1)' "$T/out")" "хвост: $(tail_of "$T/out")"
check "3 БОЛЬНОЙ: огрызок до сбоя не зелёный — «выпало 1», код ненулевой" \
      "$([ "$RC" != 0 ] && grep -q 'НЕДОГНАНА: не прогнано 1 из 2 (по сроку 0, выпало 1)' "$T/out" && echo 1 || echo 0)" \
      "код $RC, хвост: $(tail_of "$T/out")"
run_daemon broken 10 1 0
check "3 БОЛЬНОЙ демон: выпавшие — весть nightly-mutations:broken, не жёлтая" \
      "$(grep -q -- '--task nightly-mutations:broken' "$T/tg.log" && grep -q 'выпало 2' "$T/tg.log" \
          && ! grep -q 'следующий запуск' "$T/tg.log" && echo 1 || echo 0)" "весть: «$(tg_text)»"

if [ "$failed" = 0 ]; then echo "ПРОБА ЗЕЛЁНАЯ: $total путей"; exit 0; fi
echo "ПРОБА КРАСНАЯ: неудач $failed из $total"; exit 1
