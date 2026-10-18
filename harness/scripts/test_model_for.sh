#!/usr/bin/env bash
# Проба model-for.sh: неизвестный вид работы назван вслух в stderr.
#
# Беда (3.5 отчёта соседней установки, их 7f91711): `model-for.sh ревью` вернул
# haiku МОЛЧА — вид в таблице зовётся `ревью_кода`, и опечатка в виде работы
# опустила ревью на самую дешёвую модель. Строка уходила только в
# model-dlya.log, а демоны зовут с 2>/dev/null на stdout — жалобу не видел
# никто. Правило: неизвестный вид — строка в stderr с именем вида; код
# возврата 0 и умолчание остаются (звонящие демоны не должны падать).
#
# Стенд: своя таблица, свой LOG_DIR; конфиги — несуществующие пути стенда,
# чтобы боевой harness.conf не подмешал DEMON_MODEL или LOG_DIR.
#
# Дверь для мутаций: MODEL_FOR_UNDER_TEST — путь к испорченной копии;
# MODELS_LIB_UNDER_TEST — испорченная копия scripts/lib/models.sh (model_dlya).
#
#   bash scripts/test_model_for.sh
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
SUBJECT="${MODEL_FOR_UNDER_TEST:-$HERE/model-for.sh}"
[ -f "$SUBJECT" ] || { echo "ПРОБА СЛОМАНА: нет $SUBJECT"; exit 1; }
MODELS_LIB="${MODELS_LIB_UNDER_TEST:-$HERE/lib/models.sh}"
[ -f "$MODELS_LIB" ] || { echo "ПРОБА СЛОМАНА: нет $MODELS_LIB"; exit 1; }

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/log"
cat > "$T/models.yaml" <<'YAML'
модели:
  спека: opus
  ревью_кода: opus
  дежурный: haiku
умолчание: haiku
YAML

bad=0
total=0
verdict() {  # $1 = 1 (верно) / 0, $2 = имя, $3 = подробность
    total=$((total + 1))
    if [ "$1" = 1 ]; then
        echo "  ок    $2"
    else
        echo "  ПЛОХО $2${3:+ — $3}"
        bad=$((bad + 1))
    fi
}

# Прогон: stdout → $T/out, stderr → $T/err, код → $T/rc.
call() {  # $1 = вид работы
    local rc=0
    (cd "$T" && HARNESS_INSTALL_CONF="$T/нет-install.conf" HARNESS_CONF="$T/нет-harness.conf" \
        MODELS_CONF="$T/models.yaml" LOG_DIR="$T/log" DEMON_MODEL= PROJECT_DIR="$T" \
        timeout 30 bash "$SUBJECT" "$1" > "$T/out" 2> "$T/err") || rc=$?
    printf '%s' "$rc" > "$T/rc"
}

# ── БОЛЬНОЙ СЛУЧАЙ: вида нет в таблице ─────────────────────────────────────
call ревью
verdict "$(grep -q 'ревью' "$T/err" && echo 1 || echo 0)" \
    "БОЛЬНОЙ 3.5: вид «ревью» не из таблицы — назван в stderr" \
    "stderr: «$(cat "$T/err")»"
verdict "$([ "$(cat "$T/rc")" = 0 ] && [ "$(tr -d '\n' < "$T/out")" = haiku ] && echo 1 || echo 0)" \
    "неизвестный вид: умолчание haiku и код 0 — звонящий демон не падает" \
    "код $(cat "$T/rc"), stdout «$(tr -d '\n' < "$T/out")»"

# ── сосед: известный вид молчит в stderr ───────────────────────────────────
call ревью_кода
verdict "$([ ! -s "$T/err" ] && [ "$(tr -d '\n' < "$T/out")" = opus ] && echo 1 || echo 0)" \
    "известный вид «ревью_кода» — opus, stderr пуст" \
    "stdout «$(tr -d '\n' < "$T/out")», stderr «$(cat "$T/err")»"

# ── сосед: строка в stderr несёт ИМЕННО вид, а не общую фразу ──────────────
call совсем-другое-дело
verdict "$(grep -q 'совсем-другое-дело' "$T/err" && echo 1 || echo 0)" \
    "второй неизвестный вид назван своим именем" "stderr: «$(cat "$T/err")»"

# ── VG-04, БОЛЬНОЙ СЛУЧАЙ: жалоба доходит по пути ДЕМОНОВ ─────────────────
# Демоны зовут не model-for.sh, а model_dlya из scripts/lib/models.sh, и та
# глушила stderr: строка о неизвестном виде не доходила ни до журнала демона
# (его stderr), ни до $LOG_DIR/model-dlya.log. Стенд — проект из трёх файлов
# (model-for.sh, lib/config.sh, lib/models.sh): model_dlya ищет model-for.sh
# по PROJECT_DIR, и боевой проект в зов попасть не должен.
mkdir -p "$T/proj/scripts/lib"
cp "$SUBJECT" "$T/proj/scripts/model-for.sh"
cp "$HERE/lib/config.sh" "$T/proj/scripts/lib/config.sh"
cp "$MODELS_LIB" "$T/proj/scripts/lib/models.sh"
rm -f "$T/log/model-dlya.log"
(cd "$T" && HARNESS_INSTALL_CONF="$T/нет-install.conf" HARNESS_CONF="$T/нет-harness.conf" \
    MODELS_CONF="$T/models.yaml" LOG_DIR="$T/log" DEMON_MODEL= PROJECT_DIR="$T/proj" \
    timeout 30 bash -c 'source "$PROJECT_DIR/scripts/lib/models.sh"; model_dlya вид-которого-нет' \
    > "$T/out" 2> "$T/daemon.log")
heard=0
grep -q 'вид-которого-нет' "$T/daemon.log" 2>/dev/null && heard=1
grep -q 'вид-которого-нет' "$T/log/model-dlya.log" 2>/dev/null && heard=1
verdict "$heard" \
    "БОЛЬНОЙ VG-04: model_dlya с неизвестным видом — вид назван в журнале демона (stderr или model-dlya.log)" \
    "stderr демона «$(cat "$T/daemon.log")», model-dlya.log «$(cat "$T/log/model-dlya.log" 2>/dev/null)»"
verdict "$([ "$(tr -d '\n' < "$T/out")" = haiku ] && echo 1 || echo 0)" \
    "VG-04: model_dlya на неизвестном виде отдаёт умолчание haiku — вызов claude не падает" \
    "stdout «$(tr -d '\n' < "$T/out")»"
# Сосед: известный вид по пути демонов журнал не засоряет.
rm -f "$T/log/model-dlya.log"
(cd "$T" && HARNESS_INSTALL_CONF="$T/нет-install.conf" HARNESS_CONF="$T/нет-harness.conf" \
    MODELS_CONF="$T/models.yaml" LOG_DIR="$T/log" DEMON_MODEL= PROJECT_DIR="$T/proj" \
    timeout 30 bash -c 'source "$PROJECT_DIR/scripts/lib/models.sh"; model_dlya ревью_кода' \
    > "$T/out" 2> "$T/daemon.log")
verdict "$([ "$(tr -d '\n' < "$T/out")" = opus ] && [ ! -s "$T/daemon.log" ] && [ ! -s "$T/log/model-dlya.log" ] && echo 1 || echo 0)" \
    "VG-04, сосед: известный вид через model_dlya — opus, журнал пуст" \
    "stdout «$(tr -d '\n' < "$T/out")», stderr «$(cat "$T/daemon.log")», model-dlya.log «$(cat "$T/log/model-dlya.log" 2>/dev/null)»"

echo
echo "всего путей: $total, неудач: $bad"
[ "$bad" = 0 ]
