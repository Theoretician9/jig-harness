#!/usr/bin/env bash
# Проба: pre-commit судит СОСТАВ коммита (снимок индекса), а не рабочее дерево.
#
# Откуда взята. Рабочее дерево общее для всех исполнителей, а гейты, обходящие
# диск (check-event-writers.py — os.walk, check-external-callers.py — файлы по
# пути), судили чужие незакоммиченные файлы. 02.11.2026 демон «память в гит»
# шесть раз подряд получил «GATE FAIL: писатели_журналов» из-за чужого файла
# вне git, которого в его коммите не было.
#
# Стенд: клон проекта в mktemp, своя установка (HARNESS_INSTALL_CONF: PROJECT_DIR,
# LOG_DIR, STATE_DIR — стенд), свои crontab/systemctl/sudo на PATH: внешние
# вызовы судят только строки стенда, боевые расписания и службы не участвуют.
# Хук — живой pre-commit стенда, коммиты — настоящие `git commit`.
#
# Каждому «не краснит» дан парный «краснит»: снимок, который ничего не судит,
# прошёл бы все «не краснит», а гейт по диску — все «краснит» с диском.
# Поэтому есть случаи, где индекс и диск РАСХОДЯТСЯ в обе стороны.
#
# Двери мутаций: PRE_COMMIT_GATE_UNDER_TEST — хук, EVENT_WRITERS_UNDER_TEST и
# EXTERNAL_CALLERS_UNDER_TEST — гейты; копии кладутся в стенд вместо боевых.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
GATE="${PRE_COMMIT_GATE_UNDER_TEST:-$HERE/pre-commit-gate.sh}"
WRITERS="${EVENT_WRITERS_UNDER_TEST:-$HERE/check-event-writers.py}"
CALLERS="${EXTERNAL_CALLERS_UNDER_TEST:-$HERE/check-external-callers.py}"

# Источник клона — дерево проекта. В пакете скрипт лежит в harness/scripts/,
# и раскладка там другая: тогда клонируется проект из паспорта установки.
SRC="$(git -C "$HERE" rev-parse --show-toplevel 2>/dev/null)"
if [ ! -f "$SRC/scripts/pre-commit-gate.sh" ]; then
    # shellcheck disable=SC1091
    source "$HERE/lib/config.sh"
    SRC="$(konf_iz_fajla PROJECT_DIR)"
fi
[ -f "$SRC/scripts/pre-commit-gate.sh" ] || { echo "ПЛОХО: нет дерева проекта для клона ($SRC)"; echo "ИТОГ: путей 1, неудач 1"; exit 1; }

T="$(mktemp -d -t proba-precommit-snapshot-XXXXXX)"
trap 'rm -rf "$T"' EXIT
S="$T/repo"

fails=0; total=0
case_check() {  # имя, условие (код 0 — прошло)
    total=$((total + 1))
    if [ "$2" = 0 ]; then echo "  ок    $1"; else echo "  ПЛОХО $1"; fails=$((fails + 1)); fi
}

# inbox обязателен: без него хук под pipefail выходит кодом 1 уже после
# «все гейты пройдены» (find по несуществующему каталогу).
mkdir -p "$T/log/inbox" "$T/state" "$T/secrets" "$T/bin"
cat > "$T/install.conf" <<EOF
PROJECT_DIR="$S"
LOG_DIR="$T/log"
STATE_DIR="$T/state"
SECRETS_DIR="$T/secrets"
EOF
export HARNESS_INSTALL_CONF="$T/install.conf"
unset PROJECT_DIR LOG_DIR STATE_DIR SECRETS_DIR HEARTBEAT_DIR PANEL_STATE_DIR INSTALL_CONF GIT_INDEX_FILE

# Подставные внешние вызывающие. crontab и список юнитов отдают строки стенда:
# их выводит stand_sources из библиотеки процессов клона, так что гейт
# библиотека_процессов зелёный по построению, а живой crontab не участвует.
# systemctl показывает работающую службу, только если стенд положил её путь в $T/stale.
cat > "$T/bin/crontab" <<EOF
#!/usr/bin/env bash
[ "\${1:-}" = "-l" ] && cat "$T/crontab.lines"
EOF
cat > "$T/bin/systemctl" <<EOF
#!/usr/bin/env bash
case "\$1" in
    list-unit-files) cat "$T/units.lines" ;;
    list-units) [ ! -s "$T/stale" ] || echo "harness-proba.service loaded active running proba" ;;
    show) [ -s "$T/stale" ] || exit 0
          echo "ActiveState=active"
          echo "ActiveEnterTimestamp=\$(date -d '-1 hour' '+%a %Y-%m-%d %H:%M:%S %Z')"
          echo "ExecStart={ path=\$(cat "$T/stale") ; argv[]=\$(cat "$T/stale") ; }" ;;
esac
exit 0
EOF
printf '#!/usr/bin/env bash\nexit 1\n' > "$T/bin/sudo"
chmod +x "$T/bin/"*
export PATH="$T/bin:$PATH"

# Источники запуска стенда — из библиотеки процессов клона: строка крона на каждую
# крон-запись (путь — на клон), юниты служб и «состояние_из», git-хуки записей git-<хук>.
# Файл, который зовёт cron в случаях пробы (ext_target.sh), вносится в библиотеку
# клона своей записью: иначе он «без записи» и гейт библиотеки краснит каждый коммит.
stand_sources() {  # клон, каталог стенда
    python3 - "$1" "$2" <<'PY'
import json, sys
from pathlib import Path
import yaml
root, stand = Path(sys.argv[1]), Path(sys.argv[2])
lib = root / "harness/config/process-library.json"
sched = root / "harness/config/process-library.yaml"
data = json.loads(lib.read_text(encoding="utf-8"))
data["процессы"].append({"id": "proba-ext-target", "название": "цель крона стенда пробы",
                         "запуск": "крон", "статус": "работает", "файлы": ["scripts/ext_target.sh"]})
lib.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
text = sched.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)
heads = [i for i, line in enumerate(lines) if line.startswith("процессы:")]
assert len(heads) == 1, "в process-library.yaml нет единственного раздела «процессы:»"
lines.insert(heads[0] + 1, "  proba-ext-target:\n    запуск: крон\n    расписание: '*/5 * * * *'\n"
             "    команда: scripts/ext_target.sh\n")
text = "".join(lines)
assert "proba-ext-target" in (yaml.safe_load(text).get("процессы") or {})
sched.write_text(text, encoding="utf-8")
cron, units, hooks = [], [], []
for rec in data["процессы"]:
    if str(rec.get("статус", "")).startswith("снят"):
        continue
    own = [f for f in rec.get("файлы", []) if not f.startswith("/")]
    if rec.get("запуск") == "крон" and own:
        cron.append(f"*/5 * * * * {root}/{own[0]}")
    units += rec.get("юниты") or ([f"harness-{rec['id']}.service"] if rec.get("запуск") == "служба" else [])
    units += (rec.get("состояние_из") or {}).get("systemd") or []
    if rec.get("запуск") == "хук" and rec["id"].startswith("git-") and own:
        hooks.append(f"{rec['id'][4:]} {own[0]}")
assert cron and units and hooks, "библиотека клона не дала крона, юнитов или хуков"
(stand / "crontab.lines").write_text("\n".join(cron) + "\n", encoding="utf-8")
(stand / "units.lines").write_text("".join(f"{u} enabled enabled\n" for u in dict.fromkeys(units)), encoding="utf-8")
(stand / "hooks.lines").write_text("\n".join(hooks) + "\n", encoding="utf-8")
PY
}

git clone -q "$SRC" "$S" || { echo "ПЛОХО: клон не создан"; echo "ИТОГ: путей 1, неудач 1"; exit 1; }
g() { git -C "$S" -c user.name=proba -c user.email=proba@stend -c core.quotepath=false "$@"; }
# Свой harness.conf стенда: копия шаблона из клона с выдуманным именем продукта.
# Живой /etc/harness/harness.conf пробу не судит: на свежей копии у шаблона
# PRODUCT_DIR и PRODUCT_ALIASES пусты, и раздел 12 краснел без всякой поломки.
# HARNESS_CONF читают и хук стенда, и гейты, и сама проба.
g show HEAD:harness/config/harness.conf | sed 's/^PRODUCT_ALIASES=.*/PRODUCT_ALIASES="probeprodukt"/' > "$T/harness.conf"
grep -q '^PRODUCT_ALIASES="probeprodukt"$' "$T/harness.conf" || { echo "ПЛОХО: в шаблоне нет PRODUCT_ALIASES"; echo "ИТОГ: путей 1, неудач 1"; exit 1; }
export HARNESS_CONF="$T/harness.conf"
cp "$GATE" "$S/scripts/pre-commit-gate.sh"
cp "$WRITERS" "$S/scripts/check-event-writers.py"
cp "$CALLERS" "$S/scripts/check-external-callers.py"
cp -r "$HERE/lib/." "$S/scripts/lib/"
rm -rf "$S/scripts/lib/__pycache__"
printf '#!/usr/bin/env bash\necho цель\n' > "$S/scripts/ext_target.sh"
chmod +x "$S/scripts/ext_target.sh"
echo "заметка 0" > "$S/probe-note.txt"
# Отслеживаемые файлы для случаев «чужая неиндексированная правка»: импорты и харнес_не_чист.
printf 'x = 1\n' > "$S/scripts/probe_import.py"
printf 'x = 1\n' > "$S/scripts/probe_clean.py"
stand_sources "$S" "$T" || { echo "ПЛОХО: источники стенда не выведены из библиотеки"; echo "ИТОГ: путей 1, неудач 1"; exit 1; }
g add -A && g commit -q --no-verify -m "стенд" || { echo "ПЛОХО: коммит стенда"; echo "ИТОГ: путей 1, неудач 1"; exit 1; }
# Git-хуки — те, что называет библиотека (записи git-<хук>), симлинками, как в живом проекте.
while read -r hook file; do
    ln -sf "../../$file" "$S/.git/hooks/$hook"
done < "$T/hooks.lines"
[ -x "$S/.git/hooks/pre-commit" ] || { echo "ПЛОХО: библиотека клона не назвала хук pre-commit"; echo "ИТОГ: путей 1, неудач 1"; exit 1; }
BASE_REV="$(g rev-parse HEAD)"

# Нарушитель — прямая дозапись в журнал мимо emit, файла нет ни в долге, ни в реестре.
STRAY="scripts/hooks/stray_writer.py"
stray_text() {
    printf '%s\n' '"""Чужой писатель журнала мимо emit."""' \
        'with open("/var/log/harness/stray-probe.jsonl", "a") as fh:' \
        '    fh.write("{}\n")'
}
CLEAN_TEXT='"""Тот же файл без записи в журнал."""'

n=0
OUT=""; RC=0
# Коммит заметки. «-o» — режим демона памяти: git собирает ВРЕМЕННЫЙ индекс,
# и хук обязан судить его, а не общий .git/index.
commit_note() {  # [-o] — только заметка
    n=$((n + 1))
    echo "заметка $n" > "$S/probe-note.txt"
    if [ "${1:-}" = "-o" ]; then
        OUT="$(g commit -m "заметка $n" -o probe-note.txt 2>&1)"; RC=$?
    else
        g add probe-note.txt
        OUT="$(g commit -m "заметка $n" 2>&1)"; RC=$?
    fi
}
passed() {  # коммит прошёл: код 0, хук дошёл до конца, HEAD — эта заметка
    [ "$RC" = 0 ] && grep -q 'все гейты пройдены' <<< "$OUT" \
        && [ "$(g log -1 --format=%s)" = "заметка $n" ]
}
failed_on() {  # $1 — гейт: коммит отменён именно им
    [ "$RC" != 0 ] && grep -qF "GATE FAIL: $1" <<< "$OUT" \
        && [ "$(g log -1 --format=%s)" != "заметка $n" ]
}
show_fail() {  # у неудачи — причина из вывода хука, иначе хвост вывода
    [ "$1" = 0 ] && return
    { grep -E 'GATE FAIL|КРАСНЫЙ|СЛОМАНЫ|работает на старом' <<< "$OUT" || tail -4 <<< "$OUT"; } \
        | head -5 | sed 's/^/        /'
    echo "        (код $RC, HEAD: $(g log -1 --format=%s))"
}
check() {  # имя, условие
    case_check "$1" "$2"; show_fail "$2"
}
unstage_all() { g reset -q 2>/dev/null; }
# Дыра пропускает нарушителя в HEAD стенда; без отката следующий гейт
# краснел бы на чужом нарушении, а не на своём.
back_to_base() {  # пути — к виду базового коммита стенда, проскочившее откатить коммитом
    unstage_all
    local p
    for p in "$@"; do
        if g cat-file -e "$BASE_REV:$p" 2>/dev/null; then g show "$BASE_REV:$p" > "$S/$p"; else rm -f "$S/$p"; fi
        g add -A -- "$p" 2>/dev/null
    done
    g diff --cached --quiet || g commit -q --no-verify -m "стенд: откат пробы"
}
restore_target() {  # файл из HEAD вместе с битом исполнения, иначе git видит правку режима
    g show HEAD:scripts/ext_target.sh > "$S/scripts/ext_target.sh"
    chmod +x "$S/scripts/ext_target.sh"
}

echo "── 0. контроль стенда: чистый коммит проходит, crontab стенда прочитан"
commit_note -o
check "чистый commit -o проходит все гейты" "$(passed; echo $?)"
check "внешние_вызовы проверили строку стенда (счёт ≥ 1)" \
    "$(grep -qE 'зовут существующие файлы \([1-9][0-9]*\)' <<< "$OUT"; echo $?)"
check "библиотека_процессов судила источники стенда — чисто" \
    "$(grep -qE 'check-process-library: чисто, записей [1-9]' <<< "$OUT"; echo $?)"

echo "── 1. чужой мусор в рабочем дереве не краснит мой коммит"
stray_text > "$S/$STRAY"
commit_note -o
check "неиндексированный $STRAY, commit -o заметки — проходит" "$(passed; echo $?)"
commit_note
check "неиндексированный $STRAY, add + commit заметки — проходит" "$(passed; echo $?)"
# Чужой индекс: другой исполнитель добавил нарушителя в общий .git/index, а
# commit -o берёт только заметку — нарушителя в моём коммите нет.
g add "$STRAY"
commit_note -o
check "нарушитель в общем индексе, но не в commit -o — проходит" "$(passed; echo $?)"
unstage_all
rm -f "$S/$STRAY"

echo "── 2. нарушитель в коммите краснит писатели_журналов"
stray_text > "$S/$STRAY"
g add "$STRAY"
commit_note
check "нарушитель в индексе и на диске — GATE FAIL: писатели_журналов" "$(failed_on писатели_журналов; echo $?)"
# Диск чист, индекс несёт нарушителя: коммитится индекс.
echo "$CLEAN_TEXT" > "$S/$STRAY"
commit_note
check "нарушитель в индексе, на диске чистая правка — GATE FAIL: писатели_журналов" "$(failed_on писатели_журналов; echo $?)"
# Обратное расхождение: в индексе чистый файл, на диске — нарушитель.
g add "$STRAY"
stray_text > "$S/$STRAY"
commit_note
check "в индексе чистый файл, на диске нарушитель — проходит" "$(passed; echo $?)"
# Файл теперь в HEAD чистым; чужая неиндексированная правка делает его нарушителем.
commit_note -o
check "отслеживаемый файл испорчен неиндексированной правкой, commit -o — проходит" "$(passed; echo $?)"
echo "$CLEAN_TEXT" > "$S/$STRAY"

echo "── 3. внешние вызовы: что зовёт cron, обязано быть в коммите"
rm -f "$S/scripts/ext_target.sh"
commit_note -o
check "файл, который зовёт cron, удалён только с диска (чужое) — commit -o проходит" "$(passed; echo $?)"
restore_target
g rm -q scripts/ext_target.sh
commit_note
check "git rm файла, который зовёт cron — GATE FAIL: внешние_вызовы" "$(failed_on внешние_вызовы; echo $?)"
unstage_all
restore_target
g rm -q --cached scripts/ext_target.sh
commit_note
check "git rm --cached (файл на диске остался) — GATE FAIL: внешние_вызовы" "$(failed_on внешние_вызовы; echo $?)"
unstage_all
# Служба на старом коде — состояние машины, и судится оно по-прежнему на
# любом коммите: ослаблять это гейт не вправе.
touch "$S/scripts/ext_target.sh"
echo "$S/scripts/ext_target.sh" > "$T/stale"
commit_note -o
check "служба работает на коде старше правки — GATE FAIL: внешние_вызовы" "$(failed_on внешние_вызовы; echo $?)"
check "причина названа: «работает на старом коде»" "$(grep -q 'работает на старом коде' <<< "$OUT"; echo $?)"
rm -f "$T/stale"

echo "── 4. рост долга реестра в индексе судится против HEAD"
# База долга читается git-ом из HEAD, а снимок — не репозиторий: без GIT_DIR
# база «не найдена», и рост долга проходит молча.
DEBTOR="scripts/hooks/debt_writer.py"
printf '%s\n' '"""Новый писатель, внесённый в долг вместо перевода на emit."""' \
    'with open("/var/log/harness/confirmations.jsonl", "a") as fh:' \
    '    fh.write("{}\n")' > "$S/$DEBTOR"
sed -i "/^legacy_writers:/a\\  - {file: $DEBTOR, card: proba, journals: [confirmations]}" \
    "$S/harness/config/journals.yaml"
g add "$DEBTOR" harness/config/journals.yaml
commit_note
check "писатель и строка долга в индексе — GATE FAIL: писатели_журналов" "$(failed_on писатели_журналов; echo $?)"
check "причина названа: «вырос против HEAD»" "$(grep -q 'вырос против HEAD' <<< "$OUT"; echo $?)"
unstage_all
rm -f "$S/$DEBTOR"
g show HEAD:harness/config/journals.yaml > "$S/harness/config/journals.yaml"

# Гейты 5–12: на каждый — (а) чужое неиндексированное нарушение и узкий
# commit -o проходит, (б) то же нарушение в индексе краснит именно этот гейт.
# Для импортов и харнес_не_чист ещё (в): битое в индексе при чистом диске —
# коммитится индекс, и он обязан краснить.

echo "── 5. прополка_карты судит карту из коммита"
# Карточка длиннее потолка summary — только на диске, в индексе карта чистая.
python3 - "$S" <<'PY'
import importlib.util, re, sys
root = sys.argv[1]
spec = importlib.util.spec_from_file_location("weed", root + "/scripts/weed-the-map.py")
weed = importlib.util.module_from_spec(spec); spec.loader.exec_module(weed)
path = root + "/dev-map.yaml"
text = open(path, encoding="utf-8").read()
_, start, end = weed.карточки(text)[0]
block, k = re.subn(r'^(\s*summary:\s*")', lambda m: m.group(1) + "я" * 1300 + " ",
                   text[start:end], count=1, flags=re.M)
assert k == 1, "у первой карточки нет summary в кавычках"
open(path, "w", encoding="utf-8").write(text[:start] + block + text[end:])
PY
commit_note -o
check "длинный summary в неиндексированной dev-map.yaml, commit -o — проходит" "$(passed; echo $?)"
g add dev-map.yaml
commit_note
check "длинный summary в индексе — GATE FAIL: прополка_карты" "$(failed_on прополка_карты; echo $?)"
back_to_base dev-map.yaml

echo "── 6. писатели_карты судят коммит"
# Запись в карту мимо общего лока.
MAPW="scripts/probe_map_writer.sh"
printf '#!/usr/bin/env bash\nDEVMAP="$PROJECT_DIR/dev-map.yaml"\ncat x > "$DEVMAP"\n' > "$S/$MAPW"
commit_note -o
check "неотслеживаемый $MAPW, commit -o — проходит" "$(passed; echo $?)"
g add "$MAPW"
commit_note
check "$MAPW в индексе — GATE FAIL: писатели_карты" "$(failed_on писатели_карты; echo $?)"
back_to_base "$MAPW"

echo "── 7. загрузка_конфига судит коммит"
CONF="scripts/probe_config.sh"
printf '#!/usr/bin/env bash\nsource "$INSTALL_CONF"\n' > "$S/$CONF"
commit_note -o
check "неотслеживаемый $CONF, commit -o — проходит" "$(passed; echo $?)"
g add "$CONF"
commit_note
check "$CONF в индексе — GATE FAIL: загрузка_конфига" "$(failed_on загрузка_конфига; echo $?)"
back_to_base "$CONF"

echo "── 8. функция_до_определения судит коммит"
ORDER="scripts/probe_order.sh"
printf '#!/usr/bin/env bash\necho "$(obrezat 80)"\nobrezat() {\n  cat\n}\n' > "$S/$ORDER"
commit_note -o
check "неотслеживаемый $ORDER, commit -o — проходит" "$(passed; echo $?)"
g add "$ORDER"
commit_note
check "$ORDER в индексе — GATE FAIL: функция_до_определения" "$(failed_on функция_до_определения; echo $?)"
back_to_base "$ORDER"

echo "── 9. семена судят коммит"
SEED="UNIFIED/templates/probe-seed.py"
printf 'x = 1\n' > "$S/$SEED"
commit_note -o
check "неотслеживаемый $SEED, commit -o — проходит" "$(passed; echo $?)"
back_to_base "$SEED"
# Чужая неиндексированная правка главы: выгрузка «одним файлом» отстаёт только на диске.
CHAPTER="UNIFIED/03-ПАМЯТЬ.md"
echo "проба чужой правки главы" >> "$S/$CHAPTER"
commit_note -o
check "неиндексированная правка $CHAPTER, commit -o — проходит" "$(passed; echo $?)"
back_to_base "$CHAPTER"
printf 'x = 1\n' > "$S/$SEED"
g add "$SEED"
commit_note
check "$SEED в индексе — GATE FAIL: семена" "$(failed_on семена; echo $?)"
back_to_base "$SEED"

echo "── 10. context_hygiene судит коммит"
ORPHAN="память/proba-orphan.md"
printf -- '---\nname: proba\n---\nсирота\n' > "$S/$ORPHAN"
commit_note -o
check "неотслеживаемый $ORPHAN, commit -o — проходит" "$(passed; echo $?)"
g add "$ORPHAN"
commit_note
check "$ORPHAN в индексе — GATE FAIL: context_hygiene" "$(failed_on context_hygiene; echo $?)"
back_to_base "$ORPHAN"

echo "── 11. импорты судят коммит"
IMP="scripts/probe_import.py"
printf 'x = 1\nfrom .probe_missing import y\n' > "$S/$IMP"
commit_note -o
check "битый импорт в неиндексированной правке $IMP, commit -o — проходит" "$(passed; echo $?)"
g add "$IMP"
commit_note
check "битый импорт в индексе — GATE FAIL: импорты" "$(failed_on импорты; echo $?)"
printf 'x = 1\n' > "$S/$IMP"
commit_note
check "битый импорт в индексе, на диске чисто — GATE FAIL: импорты" "$(failed_on импорты; echo $?)"
back_to_base "$IMP"

echo "── 12. харнес_не_чист судит коммит"
PURE="scripts/probe_clean.py"
# Имя продукта — из тех же данных, что читает гейт: литерал в харнесе сам
# был бы нарушением.
PRODUCT="$(python3 - "$S/scripts/check-harness-clean.py" "$T/harness.conf" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("clean", sys.argv[1])
clean = importlib.util.module_from_spec(spec); spec.loader.exec_module(clean)
print((clean.имена_продукта(sys.argv[2]) or [""])[0])
PY
)"
case_check "имя продукта задано в harness.conf" "$([ -n "$PRODUCT" ]; echo $?)"
printf 'x = 2  # %s\n' "$PRODUCT" > "$S/$PURE"
commit_note -o
check "имя продукта в неиндексированной правке $PURE, commit -o — проходит" "$(passed; echo $?)"
# Файл в коммите, но в индексе чистая версия: судить обязана она, а не диск.
printf 'x = 2\n' > "$S/$PURE"
g add "$PURE"
printf 'x = 2  # %s\n' "$PRODUCT" > "$S/$PURE"
commit_note
check "в индексе чистый $PURE, на диске имя продукта — проходит" "$(passed; echo $?)"
g add "$PURE"
commit_note
check "имя продукта в индексе — GATE FAIL: харнес_не_чист" "$(failed_on харнес_не_чист; echo $?)"
printf 'x = 3\n' > "$S/$PURE"
commit_note
check "имя продукта в индексе, на диске чисто — GATE FAIL: харнес_не_чист" "$(failed_on харнес_не_чист; echo $?)"
back_to_base "$PURE"

echo "── 13. сбой снятия снимка краснит, а не судит неполный снимок"
# Нехватка места моделируется пределом размера файла: большой файл уже в HEAD,
# коммит несёт только заметку, а checkout-index -a пишет его в снимок и получает
# EFBIG (SIGXFSZ проигнорирован — запись падает ошибкой, как при ENOSPC).
head -c 12M /dev/zero > "$S/probe-big.bin"
g add probe-big.bin && g commit -q --no-verify -m "большой файл"
n=$((n + 1))
echo "заметка $n" > "$S/probe-note.txt"
OUT="$( (ulimit -f 8192; trap '' XFSZ; g commit -m "заметка $n" -o probe-note.txt) 2>&1)"; RC=$?
check "снимок не снят целиком — GATE FAIL: снимок_индекса" "$(failed_on снимок_индекса; echo $?)"

echo "ИТОГ: путей $total, неудач $fails"
[ "$fails" = 0 ]
