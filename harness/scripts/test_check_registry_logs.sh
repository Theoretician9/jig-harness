#!/usr/bin/env bash
#
# test_check_registry_logs.sh — проба: check-registry.py сверяет, что журнал,
# названный в реестре, на машине есть. Без сверки имена журналов разошлись с
# кроном после перевода, и счётчики по имени ослепли молча (карточка
# hr.zhurnaly-demonov-nazvany-nesushchestvuyushchimi-imenami).
#
# КОНТРАКТ ВЫВОДА (для правки check-registry.py):
#   ровно одна строка-итог сверки, всегда, даже при нуле:
#       журналов из реестра нет: <N>
#   и на каждый отсутствующий журнал — строка с его именем (как претензия).
#   Общий код возврата пробу не волнует: его держат красным другие дыры
#   реестра, поэтому судим только строку итога и имя.
#
# Случаи:
#   1. живой реестр (копия system-jobs.yaml и mechanisms.yaml как есть) —
#      «журналов из реестра нет: 0»;
#   2. в копии system-jobs.yaml у первого задания журнал подменён на имя,
#      которого нет, — N больше, чем в случае 1, и подменённое имя названо.
#
# Скрипт берёт корень из своего места, а не из ключа, поэтому оба случая идут
# во временной копии scripts/, harness/, .claude/: журналы же смотрятся живые.
#
# Дверь для мутаций: CHECK_REGISTRY_SRC — путь судимого скрипта.
#   bash scripts/test_check_registry_logs.sh
# Код возврата: 0 — зелёная, 1 — красная.
set -uo pipefail
export LC_ALL=C.UTF-8

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
SRC="${CHECK_REGISTRY_SRC:-$ROOT/scripts/check-registry.py}"

T=$(mktemp -d /tmp/proba-zhurnaly-reestra-XXXXXX)
trap 'rm -rf "$T"' EXIT

FAIL=0
ok()  { echo "  ✓ $1"; }
bad() { echo "  ✗ $1"; FAIL=1; }

# Копия корня: судимый скрипт кладётся на своё место, чтобы КОРЕНЬ указал сюда.
make_root() {
    local dst="$1"
    mkdir -p "$dst"
    cp -a "$ROOT/scripts" "$ROOT/harness" "$dst/"
    [ -d "$ROOT/.claude" ] && cp -a "$ROOT/.claude" "$dst/"
    cp "$SRC" "$dst/scripts/check-registry.py"
}

# Число из строки итога; пусто — строки нет.
count_missing() {
    sed -n 's/^.*журналов из реестра нет: \([0-9][0-9]*\).*$/\1/p' "$1" | head -1
}

echo "проба: журналы из реестра существуют"

# ── 1. живой реестр ─────────────────────────────────────────────────────────
make_root "$T/live"
python3 "$T/live/scripts/check-registry.py" > "$T/live.out" 2>&1
N_LIVE=$(count_missing "$T/live.out")
if [ -z "$N_LIVE" ]; then
    bad "1. строки «журналов из реестра нет: N» нет в выводе"
elif [ "$N_LIVE" -ne 0 ]; then
    bad "1. журналов из реестра нет: $N_LIVE (ждём 0)"
    grep -F '.log' "$T/live.out" | head -10 | sed 's/^/      /'
else
    ok "1. живой реестр: журналов из реестра нет: 0"
fi

# ── 2. подмена имени журнала в копии ────────────────────────────────────────
make_root "$T/fake"
JOBS="$T/fake/harness/config/system-jobs.yaml"
REAL=$(python3 -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))["задания"][0]["журнал"])' "$JOBS")
FAKE="proba-net-takogo-zhurnala-$$.log"
if [ -z "$REAL" ] || [ -e "/var/log/harness/$FAKE" ]; then
    bad "2. условие не создано: журнал первого задания «$REAL», подмена «$FAKE»"
else
    # Подмена по якорю — первая строка «журнал: <имя>», остальное не трогаем.
    python3 - "$JOBS" "$REAL" "$FAKE" <<'PY'
import sys
путь, было, стало = sys.argv[1:]
текст = open(путь, encoding="utf-8").read()
якорь = f"журнал: {было}"
assert якорь in текст, якорь
open(путь, "w", encoding="utf-8").write(текст.replace(якорь, f"журнал: {стало}", 1))
PY
    python3 "$T/fake/scripts/check-registry.py" > "$T/fake.out" 2>&1
    N_FAKE=$(count_missing "$T/fake.out")
    if [ -z "$N_FAKE" ]; then
        bad "2. строки «журналов из реестра нет: N» нет в выводе"
    elif [ "$N_FAKE" -le "${N_LIVE:-0}" ]; then
        bad "2. подмена «$REAL» -> несуществующий не увеличила счёт ($N_FAKE при живом ${N_LIVE:-?})"
    elif ! grep -qF "$FAKE" "$T/fake.out"; then
        bad "2. счёт вырос ($N_FAKE), но подменённое имя не названо"
    else
        ok "2. подмена краснит: журналов из реестра нет: $N_FAKE, имя названо"
    fi
fi

if [ "$FAIL" -ne 0 ]; then
    echo "КРАСНАЯ: сверка журналов из реестра"
    exit 1
fi
echo "ЗЕЛЁНАЯ: сверка журналов из реестра"
exit 0
