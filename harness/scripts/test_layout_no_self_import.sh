#!/usr/bin/env bash
# Проба: личность приходит системным промптом носителя, а не @-импортом CLAUDE.md.
#
# hr.standards-and-policy §4.4: после носителя строки @harness/self/core.json и
# @harness/self/instance.yaml в CLAUDE.md дают смене личность ДВАЖДЫ (~3 200
# токенов). Их источники: шаблон CLAUDE-EN.md.in (из него собирается CLAUDE.md
# пакета) и слияние раскладки, которое дописывает в CLAUDE.md продукта
# недостающие @-строки пакета. Остальные импорты (@STATE.md, память) остаются.
# Вторая половина: раскладка пакета обязана положить в проект слой 1 носителя —
# standards.md, roles/, claude-run.sh, self-prompt.py, launch-policy.yaml — и
# назвать их в описи .layout-manifest (self/ раскладывается поимённо, новые
# файлы она сама не положит; K05 контракта).
#
# Раскладка гоняется ТОЛЬКО на стенде, как в её самотесте: свой проект в
# mktemp, свой install.conf и harness.conf из образца пакета с каталогом
# копий внутри стенда, HARNESS_SELFTEST=1 (в /etc не пишет), LOG_DIR и
# BACKUP_DIR окружения сняты. Живой проект не трогается.
#
# Где раскладчик: в пакете — рядом (harness/razlozhit-harnes.sh), в проекте —
# в пакете ${STARTER_DIR:-$HOME/starter/STARTER-PACKAGE}. Двери мутаций:
# LAYOUT_UNDER_TEST (раскладчик), BUILD_CLAUDE_MD_UNDER_TEST (сборщик CLAUDE.md).
#
#   bash scripts/test_layout_no_self_import.sh
# Код возврата: 0 — все случаи прошли, 1 — есть упавший.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

LAYOUT="${LAYOUT_UNDER_TEST:-}"
if [ -z "$LAYOUT" ]; then
    for cand in "$HERE/../razlozhit-harnes.sh" "${STARTER_DIR:-$HOME/starter/STARTER-PACKAGE}/harness/razlozhit-harnes.sh"; do
        [ -f "$cand" ] && { LAYOUT="$(readlink -f "$cand")"; break; }
    done
fi
[ -n "$LAYOUT" ] && [ -f "$LAYOUT" ] || { echo "ПРОБА СЛОМАНА: раскладчик не найден (LAYOUT_UNDER_TEST, STARTER_DIR)"; exit 1; }
PKG="$(dirname "$LAYOUT")"
BUILD="${BUILD_CLAUDE_MD_UNDER_TEST:-$HERE/build-claude-md.py}"
[ -f "$BUILD" ] || { echo "ПРОБА СЛОМАНА: нет $BUILD"; exit 1; }

STAND="$(mktemp -d "${TMPDIR:-/tmp}/sap-f2-layout-XXXXXX")"
trap 'rm -rf "$STAND"' EXIT

self_imports() { grep -c '^@harness/self/' "$1" 2>/dev/null || true; }

echo "── CLAUDE.md, собранный из шаблона ──"
# Сборщик пишет в живой CLAUDE.md, поэтому судится его функция «собрать»
# импортом, без записи: текст — тот, что лёг бы в файл.
BUILD="$BUILD" python3 - > "$STAND/собранный.md" 2> "$STAND/сборка.err" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("build_claude_md", os.environ["BUILD"])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
sys.stdout.write(m.собрать(m.корень()))
PY
case_check "сборщик собрал текст" да "$([ -s "$STAND/собранный.md" ] && echo да || echo нет)"
case_check "собранный CLAUDE.md: строк @harness/self/ — 0" 0 "$(self_imports "$STAND/собранный.md")"
case_check "собранный CLAUDE.md: @STATE.md остаётся" 1 "$(grep -cx '@STATE.md' "$STAND/собранный.md")"
case_check "CLAUDE.md пакета (его кладёт раскладка): строк @harness/self/ — 0" 0 "$(self_imports "$PKG/CLAUDE.md")"

echo "── раскладка на стенде ──"
make_conf() {  # $1 = каталог случая
    local c=$1
    cat > "$c/install.conf" <<EOF
PROJECT_DIR="$c/proj"
PROJECT_NAME="стенд"
AGENT_USER="$(id -un)"
LOG_DIR="$c/log"
HEARTBEAT_DIR="$c/hb"
EOF
    sed "s|^BACKUP_DIR=.*|BACKUP_DIR=\"$c/копии\"|" "$PKG/config/harness.conf" > "$c/harness.conf"
    [ "$(grep -c '^BACKUP_DIR=' "$c/harness.conf")" = 1 ] \
        || { echo "ПРОБА СЛОМАНА: в образце harness.conf не одна строка BACKUP_DIR"; exit 1; }
}
lay() {  # $1 = каталог случая → код раскладки; вывод в lay.out
    env HARNESS_SELFTEST=1 HARNESS_INSTALL_CONF="$1/install.conf" HARNESS_CONF="$1/harness.conf" \
        BACKUP_DIR= LOG_DIR= LAYOUT_LOCK_WAIT_SEC= timeout 600 bash "$LAYOUT" >> "$1/lay.out" 2>&1
}

# Чистая установка: CLAUDE.md ложится из пакета.
C="$STAND/чистая"; mkdir -p "$C"; make_conf "$C"
lay "$C"; rc=$?
case_check "чистая установка: раскладка прошла" 0 "$rc"
case_check "чистая установка: CLAUDE.md положен" да "$([ -f "$C/proj/CLAUDE.md" ] && echo да || echo нет)"
case_check "чистая установка: строк @harness/self/ — 0" 0 "$(self_imports "$C/proj/CLAUDE.md")"

# Носитель и слой 1 доехали до проекта и названы в описи.
WANT="harness/self/standards.md harness/self/roles/orchestrator.md harness/self/roles/lead.md
harness/self/roles/worker.md harness/self/roles/explorer.md scripts/claude-run.sh
scripts/self-prompt.py harness/config/launch-policy.yaml"
missing=(); unnamed=()
python3 - "$C/proj/.layout-manifest" > "$C/опись.txt" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
if path.is_file():
    lines = path.read_text(encoding="utf-8").split("\n")
    cut = lines.index("# оставлено") if "# оставлено" in lines else len(lines)
    for line in lines[2:cut]:
        fields = line.split("\t")
        if len(fields) == 3:
            print(fields[2])
PY
case_check "опись .layout-manifest разобрана и не пуста" да "$([ -s "$C/опись.txt" ] && echo да || echo нет)"
for f in $WANT; do
    [ -f "$C/proj/$f" ] || missing+=("$f")
    grep -qxF -- "$f" "$C/опись.txt" || unnamed+=("$f")
done
case_check "слой 1 и носитель положены в проект" "" "${missing[*]:-}"
case_check "слой 1 и носитель названы в описи раскладки" "" "${unnamed[*]:-}"

# Продукт со своим CLAUDE.md без импортов: слияние дописывает импорты харнеса,
# но не личность.
C="$STAND/продукт"; mkdir -p "$C/proj"; make_conf "$C"
printf 'правила продукта\n' > "$C/proj/CLAUDE.md"
lay "$C"; lay "$C"
case_check "продукт: текст продукта цел" да "$(head -1 "$C/proj/CLAUDE.md" | grep -qx 'правила продукта' && echo да || echo нет)"
case_check "продукт после двух раскладок: строк @harness/self/ — 0" 0 "$(self_imports "$C/proj/CLAUDE.md")"
case_check "продукт: импорт @STATE.md дописан один раз (слияние живо)" 1 "$(grep -cx '@STATE.md' "$C/proj/CLAUDE.md")"

# Обновление установки, у которой прежняя раскладка уже дописала личность:
# без снятия старых строк смена получает личность дважды.
C="$STAND/обновление"; mkdir -p "$C/proj"; make_conf "$C"
printf 'правила продукта\n\n<!-- харнес: личность агента и память -->\n@harness/self/core.json\n@harness/self/instance.yaml\n@STATE.md\n' \
    > "$C/proj/CLAUDE.md"
lay "$C"
case_check "обновление: прежние строки @harness/self/ сняты" 0 "$(self_imports "$C/proj/CLAUDE.md")"
case_check "обновление: текст продукта и @STATE.md целы" да \
    "$(head -1 "$C/proj/CLAUDE.md" | grep -qx 'правила продукта' && [ "$(grep -cx '@STATE.md' "$C/proj/CLAUDE.md")" = 1 ] && echo да || echo нет)"

printf '\nитог test_layout_no_self_import: случаев %s, неудач %s\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
