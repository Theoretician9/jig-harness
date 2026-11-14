#!/usr/bin/env bash
# Пробы печати эталона слоя 1 (спека §5.4, В-1, В-4 — варианты (а)).
#
# self-seal — root-скрипт с NOPASSWD: любой его довод, кроме каталога выкладки, —
# запись root куда угодно. Поэтому пробы держат прежде всего ОТКАЗЫ: каждый
# случай отказа строится на годной выкладке, где испорчено ровно одно, и судит
# весь стенд побайтно до/после — эталон, выкладку и то, что рядом. Частичная
# копия при отказе на середине видна той же сверкой: эталон заранее лежит
# прошлым выпуском, а испорченный файл — последний в манифесте.
#
# Что закреплено (двери для кода):
#   self-seal.sh (рядом с пробой; ставится в /usr/local/lib/harness/self-seal):
#     ровно один довод — абсолютный каталог выкладки без «..» и без симлинков;
#     читает <выкладка>/harness/self/MANIFEST.sha256 (формат sha256sum, пути от
#     harness/self/), сверяет ВСЕ sha256 и лишь затем кладёт перечисленное в
#     SELF_ETALON_DIR (по умолчанию /etc/harness/self) с правами 0644; файл в
#     каталоге сборки создаётся заново, права ставятся ему, а не пути из выкладки
#     (Н1: путь мог стать симлинком после сверки);
#     instance.yaml (слой 2) не трогает. Выкладку не меняет.
#   publish.sh --манифест-слоя1 <пакет> <прошлый манифест>: кладёт
#     <пакет>/harness/self/MANIFEST.sha256 и печатает строку
#     «меняются личность/стандарты: <файлы>», если слой 1 разошёлся с прошлым.
#   update.py: _разложить_пакет() после раскладки зовёт
#     sudo -n /usr/local/lib/harness/self-seal <пакет>; отказ sudo — в выводе
#     молча, но ровно один сигнал писателю тревог (дверь HARNESS_ALERT_TASK).
#   USTANOVIT.sh пакета: правило NOPASSWD на self-seal в записать_sudoers и
#     установка self-seal root 0755.
#
# Двери пробы:
#   SELF_SEAL_SH   испытуемый self-seal (по умолчанию — self-seal.sh рядом с пробой)
# Механизмы ищутся РЯДОМ с пробой: из копии дерева зовётся сама проба.
# sudo не зовётся: в update.py он подставной через PATH; живые /etc и
# /usr/local не трогаются. Писатель тревог — тоже стенда: без двери отказ
# подставного sudo заводил живую карточку update:self-seal-refused.
#
#   bash scripts/test_self_seal.sh
# Код возврата: 0 — все случаи прошли, 1 — есть упавший.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
SEAL="${SELF_SEAL_SH:-$HERE/self-seal.sh}"
source "$HERE/lib/selftest_cases.sh"

INSTALLER="$HERE/../../USTANOVIT.sh"
if [ ! -f "$INSTALLER" ]; then
    INSTALLER="$(python3 -c "import sys; sys.path.insert(0, '$HERE/lib'); import config; print(config.katalog_paketa() / 'USTANOVIT.sh')" 2>/dev/null || true)"
fi

STAND="$(mktemp -d "${TMPDIR:-/tmp}/sap-ch13-XXXXXX")" || exit 1
trap 'rm -rf "$STAND"' EXIT

# Слой 1 стенда; worker.md последним — на нём ловится копия до сверки всех.
LAYER1=(core.json standards.md roles/lead.md roles/worker.md)
LAYER1_SORTED="$(printf '%s\n' "${LAYER1[@]}" | sort | tr '\n' ' ')"

write_layer1() { # $1=каталог self $2=метка выпуска
    local f
    mkdir -p "$1/roles"
    for f in "${LAYER1[@]}"; do
        printf '{"файл": "%s", "выпуск": "%s"}\n' "$f" "$2" > "$1/$f"
    done
}

make_release() { # $1=каталог выкладки: слой 1, instance.yaml и манифест
    local self="$1/harness/self"
    write_layer1 "$self" выпуск-2
    printf 'копия: проба\n' > "$self/instance.yaml"
    (cd "$self" && sha256sum "${LAYER1[@]}") > "$self/MANIFEST.sha256"
}

sha_of() { sha256sum < "$1" | cut -d' ' -f1; }

# Состояние каталога побайтно: пути, типы, права, цели симлинков, содержимое.
snapshot() {
    [ -e "$1" ] || { echo НЕТ; return; }
    (cd "$1" && find . -printf '%p|%y|%m|%l\n' | sort \
        && find . -type f -print0 | sort -z | xargs -0 -r sha256sum) | sha256sum | cut -c1-16
}

# Свой случай — свой каталог: выкладка v/, эталон e/etalon с прошлым выпуском.
new_case() {
    CASE="$(mktemp -d "$STAND/c-XXXX")"
    V="$CASE/v"
    ETALON="$CASE/e/etalon"
    make_release "$V"
    write_layer1 "$ETALON" выпуск-1
    find "$ETALON" -type f -exec chmod 0644 {} +
    SEAL_CWD="$CASE"
}

seal() { # доводы self-seal → код; вывод в $STAND/seal.out, $STAND/seal.err
    [ -f "$SEAL" ] || { : > "$STAND/seal.out"; echo "нет $SEAL" > "$STAND/seal.err"; return 127; }
    (cd "$SEAL_CWD" && SELF_ETALON_DIR="$ETALON" bash "$SEAL" "$@") \
        > "$STAND/seal.out" 2> "$STAND/seal.err"
}

# Эталон == слой 1 выкладки: тот же набор (instance.yaml нет), те же байты, 0644.
etalon_verdict() {
    local files bytes=да modes=да f
    files="$(cd "$ETALON" 2>/dev/null && find . ! -type d ! -name MANIFEST.sha256 -printf '%P\n' | sort | tr '\n' ' ')"
    for f in "${LAYER1[@]}"; do
        cmp -s "$V/harness/self/$f" "$ETALON/$f" || bytes=нет
        [ "$(stat -c %a "$ETALON/$f" 2>/dev/null)" = 644 ] || modes=нет
    done
    [ "$files" = "$LAYER1_SORTED" ] && files=слой1
    printf 'набор=%s|байты=%s|0644=%s' "$files" "$bytes" "$modes"
}

refusal() { # $1=имя случая, дальше доводы self-seal; стенд случая уже испорчен
    local name="$1" before after rc
    shift
    before="$(snapshot "$CASE")"
    seal "$@"; rc=$?
    after="$(snapshot "$CASE")"
    if [ ! -f "$SEAL" ]; then
        case_check "$name" "отказ|стенд цел|причина" "self-seal.sh нет рядом с пробой"
        return
    fi
    case_check "$name" "отказ|стенд цел|причина" \
        "$([ "$rc" -ne 0 ] && echo отказ || echo "rc 0")|$([ "$before" = "$after" ] && echo "стенд цел" || echo "стенд изменён")|$([ -s "$STAND/seal.err" ] && echo причина || echo молча)"
}

echo "── self-seal: всё сверилось — копия ──"
new_case
rm -rf "$ETALON"
seal "$V"; RC=$?
case_check "эталона ещё нет: rc 0" 0 "$RC"
case_check "эталона ещё нет: слой 1 лёг побайтно, 0644, без instance.yaml" \
    "набор=слой1|байты=да|0644=да" "$(etalon_verdict)"

new_case
V_BEFORE="$(snapshot "$V")"
seal "$V"; RC=$?
case_check "эталон прошлого выпуска: rc 0" 0 "$RC"
case_check "эталон прошлого выпуска заменён слоем 1 выкладки" \
    "набор=слой1|байты=да|0644=да" "$(etalon_verdict)"
case_check "выкладка не тронута" "$V_BEFORE" "$(snapshot "$V")"
FIRST="$(snapshot "$CASE")"
seal "$V"; RC=$?
case_check "повторный запуск: rc 0 и стенд тот же (идемпотентен)" "0|$FIRST" "$RC|$(snapshot "$CASE")"

echo "── self-seal: дерево разошлось с манифестом — отказ ──"
new_case
printf 'чужое\n' > "$V/harness/self/roles/stranger.md"
refusal "лишний файл в self/ не из манифеста — отказ" "$V"

new_case
printf 'правка мимо выпуска\n' >> "$V/harness/self/roles/worker.md"
refusal "изменённый файл (последний в манифесте) — отказ, частичной копии нет" "$V"

new_case
rm "$V/harness/self/roles/worker.md"
refusal "файла из манифеста нет — отказ, частичной копии нет" "$V"

new_case
rm "$V/harness/self/MANIFEST.sha256"
refusal "манифеста нет — отказ" "$V"

new_case
printf 'наружу\n' > "$V/outside.txt"
printf '%s  ../../outside.txt\n' "$(sha_of "$V/outside.txt")" >> "$V/harness/self/MANIFEST.sha256"
refusal "путь в манифесте с .. — отказ" "$V"

new_case
printf 'абсолютный\n' > "$CASE/abs.txt"
printf '%s  %s\n' "$(sha_of "$CASE/abs.txt")" "$CASE/abs.txt" >> "$V/harness/self/MANIFEST.sha256"
refusal "абсолютный путь в манифесте — отказ" "$V"

# Симлинк в self/ с верным sha: root прочёл бы чужой файл и выложил его 0644.
new_case
printf 'закрытое\n' > "$CASE/outside.md"
ln -s "$CASE/outside.md" "$V/harness/self/roles/link.md"
printf '%s  roles/link.md\n' "$(sha_of "$CASE/outside.md")" >> "$V/harness/self/MANIFEST.sha256"
refusal "файл слоя 1 — симлинк наружу — отказ" "$V"

# Гонка Н1 (ревью ЧП#2): выкладку пишет агент, и между сверкой дерева и копией он
# подменяет файл слоя 1 симлинком на чужой файл. Под root любое действие по пути из
# выкладки, идущее по ссылке (chmod, chown), меняет цель — /etc/shadow стал бы 0644.
# Окно ловится детерминированно: в копию испытуемого перед циклом копии вставлена
# подмена, ровно то, что агент делает в своём каталоге. Судится не код возврата (отказ
# и так будет — хэш не сойдётся), а права всего, что лежит вне эталона.
echo "── self-seal: подмена файла симлинком после сверки — права чужих файлов целы ──"
new_case
VICTIM="$CASE/victim.secret"
printf 'закрытое\n' > "$VICTIM"
chmod 0600 "$VICTIM"
modes_outside() { # права и владельцы всех файлов стенда вне эталона, кроме подменяемого пути
    (cd "$CASE" && find . -path ./e -prune -o ! -path ./v/harness/self/core.json -printf '%p|%y|%m|%U\n' | sort)
}
RACY="$CASE/self-seal-race.sh"
RACE_ANCHOR='^for i in "\${!PATHS\[@\]}"; do$'
if [ -f "$SEAL" ] && grep -q -- "$RACE_ANCHOR" "$SEAL"; then
    sed "/$RACE_ANCHOR/i ln -sfn -- '$VICTIM' '$V/harness/self/core.json'" "$SEAL" > "$RACY"
    MODES_BEFORE="$(modes_outside)"
    (cd "$CASE" && SELF_ETALON_DIR="$ETALON" bash "$RACY" "$V") > "$STAND/seal.out" 2> "$STAND/seal.err"; RC=$?
    case_check "подмена после сверки: подмена сработала (core.json выкладки стал ссылкой)" да \
        "$([ -L "$V/harness/self/core.json" ] && echo да || echo нет)"
    case_check "подмена после сверки: отказ" отказ "$([ "$RC" -ne 0 ] && echo отказ || echo "rc 0")"
    case_check "подмена после сверки: цель ссылки осталась 0600" 600 "$(stat -c %a "$VICTIM")"
    case_check "подмена после сверки: ни один файл вне эталона не сменил права" \
        "$MODES_BEFORE" "$(modes_outside)"
else
    case_check "подмена после сверки: перед циклом копии стоит якорь «for i in \"\${!PATHS[@]}\"; do»" \
        есть "нет якоря в $SEAL"
fi

echo "── self-seal: довод не каталог выкладки — отказ ──"
new_case
refusal "довод — файл, не каталог — отказ" "$V/harness/self/core.json"

new_case
refusal "довод — несуществующий каталог — отказ" "$CASE/net"

new_case
refusal "довод относительный — отказ" v

new_case
refusal "довод с .. — отказ" "$CASE/v/../v"

new_case
ln -s "$V" "$CASE/link"
refusal "довод — симлинк на выкладку — отказ" "$CASE/link"

new_case
refusal "два довода — отказ" "$V" "$V"

new_case
SEAL_CWD="$V"
refusal "ноль доводов (из каталога выкладки) — отказ"

echo "── publish.sh: манифест выпуска и строка в запросе согласия ──"
PUB="$STAND/pub"
PKG="$PUB/pkg"
mkdir -p "$PUB/proj"
make_release "$PKG"
rm "$PKG/harness/self/MANIFEST.sha256"
# Ветки пусты: проскочи вызов мимо двери в основной путь — отказ конфига (2), не публикация.
printf 'PROJECT_DIR="%s"\nPUBLIC_BRANCH=""\n' "$PUB/proj" > "$PUB/conf"
# Дверь берёт конфиг только своей дверью (АР-3): зовём КОПИЮ publish.sh (с lib/) вне
# PROJECT_DIR машины, паспорт стенда — HARNESS_DOOR_STAND_HOME. Настоящий файл взял бы
# паспорт машины, и «ветки пусты» перестали бы быть правдой.
mkdir -p "$PUB/door" "$PUB/home"
cp "$HERE/publish.sh" "$PUB/door/publish.sh"
cp -r "$HERE/lib" "$PUB/door/lib"
cp "$PUB/conf" "$PUB/home/install.conf"
cp "$PUB/conf" "$PUB/home/harness.conf"
PUB_DOOR=нет
grep -qF -- '--манифест-слоя1' "$PUB/door/publish.sh" && PUB_DOOR=да

publish_manifest() { # $1=прошлый манифест → код; вывод в $PUB/out
    [ "$PUB_DOOR" = да ] || { echo "двери --манифест-слоя1 нет — publish.sh не зовём" > "$PUB/out"; return 1; }
    HARNESS_CONF="$PUB/conf" HARNESS_INSTALL_CONF="$PUB/conf" HARNESS_DOOR_STAND_HOME="$PUB/home" \
        bash "$PUB/door/publish.sh" --манифест-слоя1 "$PKG" "$1" > "$PUB/out" 2>&1
}
changed_line() { grep -m1 'меняются личность/стандарты:' "$PUB/out" || echo "строки нет"; }
names_in_line() { # $1=строка → какие из файлов слоя 1 (и новые) в ней названы
    local f out=""
    for f in "${LAYER1[@]}" roles/new.md instance.yaml; do
        printf '%s' "$1" | grep -qF "$f" && out="$out$f "
    done
    printf '%s' "${out:-никаких}"
}
manifest_verdict() {
    local m="$PKG/harness/self/MANIFEST.sha256" paths form=да check=да
    [ -f "$m" ] || { echo "манифеста нет"; return; }
    grep -qvE '^[0-9a-f]{64}  [^ ]+$' "$m" && form=нет
    (cd "$PKG/harness/self" && sha256sum -c --quiet MANIFEST.sha256 >/dev/null 2>&1) || check=нет
    paths="$(awk '{print $2}' "$m" | sort | tr '\n' ' ')"
    printf 'пути=%s|формат sha256sum=%s|сверка=%s' "$paths" "$form" "$check"
}

publish_manifest "$PUB/нет-прошлого.sha256"; RC=$?
case_check "прошлого выпуска нет: rc 0" 0 "$RC"
case_check "прошлого выпуска нет: манифест слоя 1 без instance.yaml и себя" \
    "пути=$LAYER1_SORTED|формат sha256sum=да|сверка=да" "$(manifest_verdict)"
case_check "прошлого выпуска нет: в строке все файлы слоя 1" \
    "core.json standards.md roles/lead.md roles/worker.md " "$(names_in_line "$(changed_line)")"

cp "$PKG/harness/self/MANIFEST.sha256" "$PUB/prev.sha256" 2>/dev/null
publish_manifest "$PUB/prev.sha256"; RC=$?
case_check "слой 1 не менялся: rc 0 и строки нет" "0|строки нет" "$RC|$(changed_line)"

printf 'копия: другая\n' > "$PKG/harness/self/instance.yaml"
publish_manifest "$PUB/prev.sha256"; RC=$?
case_check "изменён только instance.yaml (слой 2): строки нет" "0|строки нет" "$RC|$(changed_line)"

printf 'правка роли\n' >> "$PKG/harness/self/roles/worker.md"
publish_manifest "$PUB/prev.sha256"; RC=$?
case_check "изменена роль: строка называет ровно её" "0|roles/worker.md " "$RC|$(names_in_line "$(changed_line)")"

cp "$PKG/harness/self/MANIFEST.sha256" "$PUB/prev.sha256" 2>/dev/null
printf 'новая роль\n' > "$PKG/harness/self/roles/new.md"
publish_manifest "$PUB/prev.sha256"; RC=$?
case_check "добавлена роль: строка называет ровно её" "0|roles/new.md " "$RC|$(names_in_line "$(changed_line)")"

cp "$PKG/harness/self/MANIFEST.sha256" "$PUB/prev.sha256" 2>/dev/null
rm "$PKG/harness/self/roles/lead.md"
publish_manifest "$PUB/prev.sha256"; RC=$?
case_check "удалена роль: строка называет ровно её" "0|roles/lead.md " "$RC|$(names_in_line "$(changed_line)")"

echo "── update.py: после раскладки sudo -n self-seal, без права — молча ──"
UPD="$STAND/upd"
mkdir -p "$UPD/bin" "$UPD/log"
make_release "$UPD/pkg"
UPD_PKG="$(readlink -f "$UPD/pkg")"
cat > "$UPD/bin/sudo" <<'SUDO'
#!/usr/bin/env bash
printf 'sudo %s\n' "$*" >> "$UPD_TRACE"
[ "${FAKE_SUDO_RC:-1}" = 0 ] && exit 0
echo "sudo: a password is required" >&2
exit 1
SUDO
chmod +x "$UPD/bin/sudo"
cat > "$UPD/bin/alert" <<'ALERT'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$UPD_ALERTS"
ALERT
chmod +x "$UPD/bin/alert"
# Раскладчик, пересборки и доустановка подставные: зовётся только та половина
# _разложить_пакет, что решает про печать. sudo по абсолютному пути тоже уходит
# в подставной — живой sudo из пробы не зовётся никогда.
cat > "$UPD/driver.py" <<'DRIVER'
import importlib.util
import os
import subprocess
import sys

update_py, trace, fake_sudo = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("update_under_test", update_py)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
real_run = subprocess.run


def note(word):
    with open(trace, "a", encoding="utf-8") as fh:
        fh.write(word + "\n")


def fake_layout(command, *args, **kwargs):
    note("раскладка")
    return subprocess.CompletedProcess(command, 0, "", "")


def run(*args, **kwargs):
    command = args[0] if args else kwargs.get("args")
    if isinstance(command, (list, tuple)):
        words = [str(w) for w in command]
        if any("install-new.py" in w for w in words):
            return subprocess.CompletedProcess(command, 0, "", "")
        if os.path.basename(words[0]) == "sudo":
            command = [fake_sudo, *words[1:]]
            args = (command, *args[1:]) if args else args
            if not args:
                kwargs["args"] = command
    return real_run(*args, **kwargs)


subprocess.run = run
module._run_layout = fake_layout
module._save_seed_view = lambda *a, **k: None
# Живая пересборка переписала бы .claude/agents дерева, где идёт проба, по живому эталону.
module._пересобрать_карточки = lambda: note("карточки") or False
module.пересобрать_производные = lambda *a, **k: []
try:
    result = repr(module._разложить_пакет())
except Exception as error:  # noqa: BLE001
    result = f"исключение {type(error).__name__}: {error}"
note("итог " + result)
DRIVER

update_run() { # $1=код подставного sudo → след в $UPD/trace, сигналы в $UPD/alerts, вывод в $UPD/out
    : > "$UPD/trace"; : > "$UPD/alerts"
    PATH="$UPD/bin:$PATH" HARNESS_STARTER="$UPD_PKG" HARNESS_LOG_DIR="$UPD/log" HARNESS_TG_SEND="" \
        HARNESS_ALERT_TASK="$UPD/bin/alert" UPD_ALERTS="$UPD/alerts" \
        UPD_TRACE="$UPD/trace" FAKE_SUDO_RC="$1" \
        python3 "$UPD/driver.py" "$HERE/update.py" "$UPD/trace" "$UPD/bin/sudo" > "$UPD/out" 2>&1
}
EXPECTED_TRACE="раскладка|sudo -n /usr/local/lib/harness/self-seal $UPD_PKG|карточки|итог (True, [])"

update_run 1
case_check "нет права: раскладка, затем sudo -n self-seal <пакет>, update.py не падает" \
    "$EXPECTED_TRACE" "$(paste -sd'|' "$UPD/trace")"
case_check "нет права: update.py молчит об этом" "молчит" \
    "$(grep -qiE 'sudo|self-seal|password|эталон' "$UPD/out" && head -c 200 "$UPD/out" || echo молчит)"
case_check "нет права: ровно один сигнал, и он в писатель тревог стенда" "1|update:self-seal-refused" \
    "$(grep -c '' "$UPD/alerts")|$(grep -o 'update:self-seal-refused' "$UPD/alerts" | head -1)"

update_run 0
case_check "право есть: тот же вызов, итог раскладки (True, [])" \
    "$EXPECTED_TRACE" "$(paste -sd'|' "$UPD/trace")"
case_check "право есть: сигналов нет" 0 "$(grep -c '' "$UPD/alerts")"

echo "── USTANOVIT.sh: право NOPASSWD на self-seal через записать_sudoers ──"
if [ -f "$INSTALLER" ]; then
    RULE="$(python3 - "$INSTALLER" <<'PARSE'
import re
import sys

lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
start = re.compile(r"^\s*записать_sudoers\s+\S+\s*<<-?\s*['\"]?(\w+)['\"]?")
rule = re.compile(r"^\s*\$\{?AGENT_USER\}? ALL=\(root\) NOPASSWD: /usr/local/lib/harness/self-seal\s*$")
end = None
for line in lines:
    if end is None:
        found = start.match(line)
        end = found.group(1) if found else None
    elif line.strip() == end:
        end = None
    elif rule.match(line):
        print(line.strip())
        break
PARSE
)"
    case_check "правило self-seal лежит в теле записать_sudoers" есть "$([ -n "$RULE" ] && echo есть || echo нет)"
    if [ -n "$RULE" ] && [ -x /usr/sbin/visudo ]; then
        printf '%s\n' "${RULE/\$\{AGENT_USER\}/agent}" | sed 's/^\$AGENT_USER/agent/' > "$STAND/sudoers"
        case_check "правило проходит visudo -cf" 0 "$(/usr/sbin/visudo -cf "$STAND/sudoers" >/dev/null 2>&1; echo $?)"
    elif [ -n "$RULE" ]; then
        echo "  (visudo недоступен — правило судится только текстом)"
    else
        case_check "правило проходит visudo -cf" 0 "строки нет"
    fi
    case_check "установщик кладёт self-seal root 0755" есть \
        "$(grep -E 'install .*-o root.*-m 0?755.*/usr/local/lib/harness/self-seal|install .*-m 0?755.*-o root.*/usr/local/lib/harness/self-seal' "$INSTALLER" >/dev/null && echo есть || echo нет)"
else
    case_check "USTANOVIT.sh пакета найден" есть "нет: $INSTALLER"
fi

printf '\nитог test_self_seal: случаев %s, неудач %s\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
