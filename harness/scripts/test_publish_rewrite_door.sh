#!/usr/bin/env bash
#
# test_publish_rewrite_door.sh — контракт законной двери переписи публичной истории:
#
#   bash scripts/publish.sh --переписать-историю <каталог-копии> --бэкап <файл.bundle>
#
# Владелец 03.11.2026 дал слово переписать публичную историю. Обычный путь
# publish.sh — только вперёд, и так остаётся; перепись — отдельный режим того же
# скрипта, потому что сторож команд пускает к публичному удалённому только его.
#
# Всё живёт в mktemp-стенде: голый репозиторий вместо публичного удалённого,
# подставные паспорт, harness.conf, журнал согласий и проект с заглушками
# ворот. Боевой /etc/harness и живой журнал согласий проба не читает: корень
# установки подменён HARNESS_HOME, а каждый ключ конфига, который загрузчик
# сохраняет из окружения, задан явно значением стенда.
#
# Отличить «режима нет» от «стенд сломан»: проверки с меткой СТЕНД судят сам
# стенд (дерево копии равно дереву сборки, гейт видит след в грязной копии и не
# видит в чистой). Они обязаны быть зелёными и на коде без режима.
#
# Код возврата: 0 — все пробы зелёные, 1 — есть красные.
set -uo pipefail

SCRIPTS_DIR=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
# Дверь для мутационной пробы: испорченная копия скрипта подставляется сюда.
PUBLISH_SH="${PUBLISH_UNDER_TEST:-$SCRIPTS_DIR/publish.sh}"
# Та же дверь для ворот согласия: испорченная копия deploy_guard.py встаёт и в
# проект стенда (её зовёт publish.sh), и в прямые вызовы ворот.
GUARD_PY="${DEPLOY_GUARD_UNDER_TEST:-$SCRIPTS_DIR/deploy_guard.py}"
REAL_GIT=$(command -v git)
ROOT=$(mktemp -d -t publish-door.XXXXXX)
trap 'chmod -R u+w "$ROOT" 2>/dev/null; rm -rf "$ROOT"' EXIT

# Свой HOME и никакого системного конфига git: ни подмены адресов из
# ~/.gitconfig, ни ключей ssh. Транспорт — только file: настоящий GitHub из
# стенда недостижим, даже если адрес где-то просочится.
export HOME="$ROOT/home"
mkdir -p "$HOME"
export GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 GIT_ALLOW_PROTOCOL=file GIT_SSH_COMMAND=false
# Журнал слова и ключи согласия судья берёт у носителя: отдельным процессом — у машины.
# Поэтому ворота стенда — обёртка с носителем стенда (obertka_vorot ниже): её зовут и
# publish.sh проекта стенда, и прямые вызовы ворот (guard).
git config --global user.name "proba-stend"
git config --global user.email "proba-stend@stend.invalid"
git config --global init.defaultBranch master
git config --global advice.detachedHead false

TOTAL=0
FAILED=0
STAND_FAILED=0
ok()  { TOTAL=$((TOTAL + 1)); echo "  ок    $*"; }
bad() { TOTAL=$((TOTAL + 1)); FAILED=$((FAILED + 1)); echo "  ПЛОХО $*"; }
stand_bad() { STAND_FAILED=$((STAND_FAILED + 1)); bad "СТЕНД $*"; }

# Запрещённый образец: имя проекта из подставного паспорта. Гейт чистоты ищет
# его без регистра в дереве и во всей истории.
FORBIDDEN="ProbaShopXyz"
# Пакет, из которого собрано уже опубликованное дерево, — не HEAD пакета стенда
# (боевой случай 03.11.2026: метка «пакет: 810ad41» при пакете d91f0d1).
STALE_PKG="810ad41"

commit_all() { git -C "$1" add -A && git -C "$1" commit -qm "$2"; }
tag_at() { git -C "$1" tag -a "$2" -m "релиз $2"; }

final_tree() { # $1=каталог $2=хэш пакета: дерево master — одно на сборку и копию
    printf '# jig-harness\nСтавится куда угодно.\n' > "$1/README.md"
    mkdir -p "$1/harness"
    printf 'ПОРОГ = 15\n' > "$1/harness/kod.py"
    printf 'стенд пробы двери переписи\nпакет: %s\nсобрано: 2026-11-03T00:00:00+05:00\n' \
        "$2" > "$1/.публичная-сборка"
}

make_package() { # $1=стенд: подставной пакет, как proba_dvor в самотесте publish.sh
    local p="$1/pkg"
    mkdir -p "$p/harness/config" "$p/harness/память" "$p/harness/шаблоны-задач"
    printf '# харнес\n@STATE.md\n' > "$p/harness/CLAUDE.md"
    printf 'состояние\n' > "$p/harness/STATE.md"
    printf '# ТОН\nзамер речи\n' > "$p/harness/память/TONE.md"
    printf '# ТОН\n\n<!-- потолки: мягкий=700 жёсткий=1500 -->\n' \
        > "$p/harness/шаблоны-задач/ТОН-заготовка.md"
    printf 'исключить: []\nзаменить_шаблоном:\n  harness/память/TONE.md: harness/шаблоны-задач/ТОН-заготовка.md\nобезличить_ключи: [PROJECT_DIR]\nоставить: [harness/CLAUDE.md]\n' \
        > "$p/harness/config/public-build.yaml"
    printf '# Что нового\n\n## 2026-11-03 — Проба двери\n\n### Исправлено\n\n- Сессия больше не обрывается.\n\n### Что изменилось для вас\n\n- Ничего делать не нужно.\n' \
        > "$p/ЧТО-НОВОГО.md"
    git init -q "$p"
    commit_all "$p" "пакет стенда"
    git -C "$p" remote add jig "$1/remote.git"
}

# Обёртка ворот пробы: настоящие ворота deploy_guard.py в процессе обёртки, их судье —
# носитель стенда. После карточки А судья не слушает окружение ни при каком ключе
# (спека §3.2), и отдельным процессом дверь «Да» стенда не видит — открытие проба
# судит только так (отступление 7 плана), отказ — и отдельным процессом.
# Без Носитель у судьи (до А3) обёртка печатает «ОБЁРТКА ПРОБЫ: …» и выходит кодом 3.
obertka_vorot() {  # $1=файл обёртки $2=настоящие ворота $3=паспорт стенда $4=ручки стенда
    # Путь обёртки бывает ссылкой на настоящие ворота (проект стенда из ссылок): открыть его
    # на запись значило бы переписать сами ворота, поэтому ссылка снимается до записи.
    rm -f "$1"
    python3 - "$1" "$(readlink -f "$2")" "$3" "$4" <<'PY'
import sys
out, real, passport, handles = sys.argv[1:5]
head = f"REAL, PASSPORT, HANDLES = {real!r}, {passport!r}, {handles!r}\n"
body = r'''
import dataclasses, importlib.util, pathlib, sys

def number(value, default):
    text = str(value or "").strip()
    return int(text) if text.isdigit() else default

def stand_carrier(judge, config):
    cls = getattr(judge, "Носитель", None)
    if cls is None or not dataclasses.is_dataclass(cls):
        raise LookupError("в судье нет dataclass Носитель")
    values, keys = config.из_файла(PASSPORT), config.из_файла(HANDLES)
    folder = pathlib.Path(values.get("LOG_DIR") or PASSPORT)
    known = {"папка": folder, "uid": None, "паспорт": values, "граница": folder,
             "окно_мин": number(keys.get("CONFIRM_MAX_AGE_MIN"), 60),
             "срок_кнопки_мин": number(keys.get("CONSENT_BUTTON_TTL_MIN"), None), "ручки": keys}
    fields = dataclasses.fields(cls)
    unknown = [f.name for f in fields if f.name not in known
               and f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING]
    if unknown:
        raise LookupError(f"у Носитель обязательные поля, которых проба не знает: {unknown}")
    return cls(**{f.name: known[f.name] for f in fields if f.name in known})

sys.argv[0] = REAL
spec = importlib.util.spec_from_file_location("deploy_guard", REAL)
door = importlib.util.module_from_spec(spec)
spec.loader.exec_module(door)
try:
    carrier = stand_carrier(door.слово_вл, door.конф)
except LookupError as trouble:
    print(f"ОБЁРТКА ПРОБЫ: {trouble}")
    sys.exit(3)
door.слово_вл.носитель = lambda: carrier
sys.exit(door.main())
'''
with open(out, "w", encoding="utf-8") as fh:
    fh.write("# Обёртка пробы (носитель стенда): сгенерирована пробой, не править.\n" + head + body)
PY
}

make_project() { # $1=стенд: проект — ссылки на настоящие скрипты, ворота и канал заглушены
    local pr="$1/proj" f
    mkdir -p "$pr/scripts"
    for f in "$SCRIPTS_DIR"/*; do ln -s "$f" "$pr/scripts/$(basename "$f")"; done
    rm -f "$pr/scripts/gates.sh" "$pr/scripts/tg_send.sh" "$pr/scripts/deploy.sh" "$pr/scripts/publish.sh"
    # Дверь — КОПИЯ файлом (АР-3): настоящий publish.sh взял бы паспорт машины и её
    # tg_send.sh рядом с собой; копия вне PROJECT_DIR машины верит HARNESS_DOOR_STAND_HOME.
    cp "$PUBLISH_SH" "$pr/scripts/publish.sh"
    obertka_vorot "$pr/scripts/deploy_guard.py" "$GUARD_PY" "$1/install.conf" "$1/harness.conf"
    # Ворота обычного пути в режим переписи не входят. Заглушка пишет, что её
    # звали, и отказывает, пока стенд не разрешит (файл gates-rc): так старый
    # код без режима останавливается на замке 1 и ничего не публикует.
    printf '#!/bin/sh\necho "gates $*" >> "%s/gates-called"\nexit "$(cat "%s/gates-rc" 2>/dev/null || echo 1)"\n' \
        "$1" "$1" > "$pr/scripts/gates.sh"
    printf '#!/bin/sh\necho "$*" >> "%s/tg-sent"\n' "$1" > "$pr/scripts/tg_send.sh"
    printf '#!/bin/sh\necho "заглушка стенда: выкат не звать" >&2\nexit 1\n' > "$pr/scripts/deploy.sh"
    chmod +x "$pr/scripts/gates.sh" "$pr/scripts/tg_send.sh" "$pr/scripts/deploy.sh"
    printf 'проект стенда\n' > "$pr/README.md"
    git init -q "$pr"
    git -C "$pr" add README.md
    git -C "$pr" commit -qm "проект стенда"
}

make_configs() { # $1=стенд
    printf 'PROJECT_DIR="%s"\nAGENT_USER="podstavnoj"\nLOG_DIR="%s"\nPROJECT_NAME="%s"\nTG_CHAT_ID="100000001"\nPRODUCT_DIR="tovarxyz"\nAUTONOMY="semi"\nSTARTER_DIR="%s"\n' \
        "$1/proj" "$1/log" "$FORBIDDEN" "$1/pkg" > "$1/install.conf"
    printf 'PUBLIC_REMOTE="jig"\nPUBLIC_BRANCH="master"\nPUBLIC_BUILD_DIR="%s"\nPUBLIC_COMMIT_NAME="proba-harness"\nPUBLIC_COMMIT_EMAIL="proba@harness.local"\nCONFIRM_MAX_AGE_MIN="180"\nPUBLISH_NEEDS_OWNER="1"\nSTARTER_DIR="%s"\n' \
        "$1/build" "$1/pkg" > "$1/harness.conf"
}

make_wrapper() { # $1=стенд: git в PATH стенда — журнал push и, по флагу, гонка
    mkdir -p "$1/bin"
    cat > "$1/bin/git" <<EOF
#!/bin/bash
# Обёртка стенда. Каждый push пишется в журнал по аргументу на строку. Когда
# взведён флаг гонки, перед ПЕРВОЙ отправкой с арендой чужой коммит уходит в
# remote — remote сдвигается ПОСЛЕ того, как дверь его прочла. Отправка дальше
# исполняется настоящим git: судит аренда, а не обёртка.
is_push=0; lease=0
for a in "\$@"; do
    [ "\$a" = push ] && is_push=1
    case "\$a" in --force-with-lease*) lease=1 ;; esac
done
if [ "\$is_push" = 1 ]; then
    printf '%s\n' "--- push" "\$@" >> "$1/push-args.log"
    # Флаг «после отправки»: отправка двери проходит, и СРАЗУ за ней чужая
    # ветка появляется в remote — сверка после отправки обязана это увидеть.
    if [ "\$lease" = 1 ] && [ -e "$1/after-armed" ] && [ ! -e "$1/after-fired" ]; then
        "$REAL_GIT" "\$@"; rc=\$?
        if [ "\$rc" = 0 ]; then
            touch "$1/after-fired"
            "$REAL_GIT" -C "$1/foreign" push -q origin HEAD:refs/heads/novaya >/dev/null 2>&1 \\
                || echo "--- чужой push не прошёл" >> "$1/push-args.log"
        fi
        exit "\$rc"
    fi
    if [ "\$lease" = 1 ] && [ -e "$1/race-armed" ] && [ ! -e "$1/race-fired" ]; then
        touch "$1/race-fired"
        "$REAL_GIT" -C "$1/foreign" push -q origin HEAD:refs/heads/master >/dev/null 2>&1 \\
            || echo "--- чужой push не прошёл" >> "$1/push-args.log"
    fi
fi
exec "$REAL_GIT" "\$@"
EOF
    chmod +x "$1/bin/git"
}

# Стенд: $1=имя, $2=вид копии (clean | dirty | tree-differs | extra-tag |
# clean-stale | dirty-stale | no-label). «-stale» — метка итогового дерева сборки
# и копии называет прошлый пакет (STALE_PKG), а не HEAD пакета стенда; no-label —
# метки в итоговом дереве нет ни там, ни там. Деревья сборки и копии равны.
# Печатает путь стенда; болтовня сборки стенда уходит в stderr, а не в путь.
make_stand() { build_stand "$@" >&2; printf '%s\n' "$ROOT/$1"; }
build_stand() {
    local s="$ROOT/$1" kind="$2" pkg_head label
    mkdir -p "$s/log"
    git init -q --bare "$s/remote.git"
    make_package "$s"
    make_project "$s"
    make_configs "$s"
    make_wrapper "$s"
    pkg_head=$(git -C "$s/pkg" rev-parse HEAD)
    label=$pkg_head
    case "$kind" in *-stale) label=$STALE_PKG ;; esac

    # Старая история — в сборке, она же опубликована. Первый коммит несёт
    # запрещённый образец: так выглядит история, ради которой перепись.
    mkdir -p "$s/build"
    git init -q "$s/build"
    printf 'старая версия, магазин %s\n' "$FORBIDDEN" > "$s/build/README.md"
    printf 'стенд\nпакет: %s\n' "$pkg_head" > "$s/build/.публичная-сборка"
    commit_all "$s/build" "версия от 2026-09-12"; tag_at "$s/build" v0.1.0
    mkdir -p "$s/build/harness"; printf 'ПОРОГ = 10\n' > "$s/build/harness/kod.py"
    commit_all "$s/build" "версия от 2026-10-01"; tag_at "$s/build" v0.2.0
    final_tree "$s/build" "$label"
    [ "$kind" = no-label ] && rm -f "$s/build/.публичная-сборка"
    commit_all "$s/build" "версия от 2026-11-01"; tag_at "$s/build" v0.3.0
    git -C "$s/build" remote add jig "$s/remote.git"
    git -C "$s/build" push -q jig refs/heads/master 'refs/tags/*:refs/tags/*'
    git -C "$s/build" fetch -q jig

    # Копия — другая история с тем же деревом master и теми же тегами.
    mkdir -p "$s/copy"
    git init -q "$s/copy"
    if [ "${kind%-stale}" = dirty ]; then
        printf 'первая версия, магазин %s\n' "$FORBIDDEN" > "$s/copy/README.md"
    else
        printf 'первая версия\n' > "$s/copy/README.md"
    fi
    printf 'стенд\nпакет: %s\n' "$pkg_head" > "$s/copy/.публичная-сборка"
    commit_all "$s/copy" "версия от 2026-09-12 (переписано)"; tag_at "$s/copy" v0.1.0
    mkdir -p "$s/copy/harness"; printf 'ПОРОГ = 10\n' > "$s/copy/harness/kod.py"
    commit_all "$s/copy" "версия от 2026-10-01 (переписано)"; tag_at "$s/copy" v0.2.0
    final_tree "$s/copy" "$label"
    [ "$kind" = no-label ] && rm -f "$s/copy/.публичная-сборка"
    [ "$kind" = tree-differs ] && printf 'лишнее\n' > "$s/copy/lishnee.txt"
    commit_all "$s/copy" "версия от 2026-11-01 (переписано)"; tag_at "$s/copy" v0.3.0
    [ "$kind" = extra-tag ] && tag_at "$s/copy" v9.9.9

    git -C "$s/remote.git" bundle create "$s/old.bundle" --all >/dev/null 2>&1
}

say_consent() { # $1=стенд $2=возраст для date -d $3=текст → печатает ts записи
    local ts; ts=$(date -Iseconds -d "$2")
    printf '{"ts":"%s","text":"%s","message_id":"proba-%s"}\n' \
        "$ts" "$3" "$RANDOM" >> "$1/log/confirmations.jsonl"
    printf '%s' "$ts"
}

# Слово владельца — так, как его раскладывает tg-dispatcher.sh: строка в машинный
# журнал inbox.jsonl, файл очереди inbox/<ts>.txt и, если судья согласия
# (scripts/lib/patterns.py, вердикт_согласия) признал строку согласием, запись
# в confirmations.jsonl с видом. Судья — настоящий: «да, переписывай публичную
# историю jig» он судит как «нет», и в журнал согласий фраза НЕ попадает —
# до ворот она доходит только очередью. Режим queue — запись в журнал согласий
# не делается вовсе (те же фразы, что ворота видят лишь в очереди).
owner_says() { # $1=стенд $2=возраст для date -d $3=текст [$4=queue] → печатает ts
    python3 - "$1/log" "$2" "$3" "${4:-}" "$SCRIPTS_DIR/lib" <<'PY'
import json, os, random, subprocess, sys
log_dir, age, text, mode, lib_dir = sys.argv[1:6]
sys.path.insert(0, lib_dir)
import patterns
when = float(subprocess.check_output(["date", "+%s.%N", "-d", age], text=True))
ts = subprocess.check_output(["date", "-Iseconds", "-d", "@%d" % when], text=True).strip()
message_id = str(random.randint(100000, 999999))
os.makedirs(f"{log_dir}/inbox", exist_ok=True)
with open(f"{log_dir}/inbox.jsonl", "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"ts": ts, "message_id": message_id, "chat_id": "100000001", "text": text},
                        ensure_ascii=False) + "\n")
queue_file = f"{log_dir}/inbox/{when:.9f}.txt"
with open(queue_file, "w", encoding="utf-8") as fh:
    fh.write(text + "\n")
os.utime(queue_file, (when, when))
for line in text.splitlines():
    line = line.lower().strip()
    verdict = patterns.вердикт_согласия(line) if line else "нет"
    if verdict == "нет" or mode == "queue":
        continue
    record = {"ts": ts, "text": line, "message_id": message_id}
    if verdict != "текст":
        record["kind"] = verdict
    with open(f"{log_dir}/confirmations.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
print(ts, end="")
PY
}

# Вопрос ворот: отказ ворот и есть вопрос агента владельцу (_запомнить_вопрос).
# Ворота зовутся тем же вызовом, что делает дверь, и обязаны отказать. Метка
# вопроса сдвигается на минуту раньше будущего ответа: стенд не ждёт часами,
# а ответ, которым владелец «отвечает два часа назад», должен лечь ПОСЛЕ вопроса.
ask_question() { # $1=стенд $2=возраст ответа $3=действие (по умолчанию перепись)
    local s="$1" act="${3:-перепись}" out
    out=$(guard "$s" --действие "$act" --только-согласие)
    # Ворота «ожидание» в согласие.json больше не пишут (текст двери не открывает, 06.11.2026):
    # вопрос — это сам отказ ворот; просьбу кнопки заводит часть 2.
    if [ "$(rc_of "$out")" = 0 ]; then
        stand_bad "вопрос «$act»: ворота не отказали — дверь открыта до вопроса"
    fi
}

# Кнопка «Да», как её пишет диспетчер: конверт «отправлено», затем «да» на тот же nonce.
press_yes() { # $1=стенд $2=возраст для date -d $3=предмет [$4=вид, по умолчанию перепись]
    : >> "$1/log/inbox.jsonl"
    python3 - "$1/log" "$2" "$3" "${4:-перепись}" <<'PY'
import json, random, secrets, subprocess, sys
log, age, target, kind = sys.argv[1:5]
when = int(subprocess.check_output(["date", "+%s", "-d", age], text=True))
nonce, message_id = secrets.token_urlsafe(16), random.randint(4000, 999999)
with open(f"{log}/consent-buttons.jsonl", "a", encoding="utf-8") as fh:
    for event, at, sender in (("consent.sent", when - 30, None), ("consent.yes", when, 777000)):
        ts = subprocess.check_output(["date", "-Iseconds", "-d", f"@{at}"], text=True).strip()
        data = {"nonce": nonce, "вид": kind, "цель": target, "message_id": message_id, "from_id": sender}
        fh.write(json.dumps({"v": 1, "ts": ts, "src": "scripts/lib/consent_buttons.py", "event": event,
                             "outcome": "ok", "data": data}, ensure_ascii=False) + "\n")
PY
}

asked_subject() { # $1=стенд → цель последнего запроса кнопки вида «перепись», который положила дверь
    python3 - "$1/log/consent-requests.jsonl" <<'PY'
import json, sys
try:
    rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
except OSError:
    rows = []
targets = [r["data"]["цель"] for r in rows
           if r.get("event") == "consent.request" and (r.get("data") or {}).get("вид") == "перепись"]
print(targets[-1] if targets else "", end="")
PY
}

# Согласие на перепись: дверь просит кнопку тем же зовом ворот, что в publish.sh, владелец
# жмёт «Да» на предмет из её запроса. Предмет несёт вершину удалённого ДО (спека §5).
consent() { # $1=стенд $2=возраст нажатия
    local subject
    guard "$1" --действие перепись --только-согласие --просить --перепись "$1/copy" > /dev/null
    subject=$(asked_subject "$1")
    [ -n "$subject" ] || { stand_bad "$(basename "$1"): дверь не положила запрос кнопки «перепись»"; return; }
    press_yes "$1" "$2" "$subject"
}

# Кнопка не тратится (спека §6): гасит её только новый предмет — после переписи вершина другая.
button_opens() { # $1=стенд → 0, если ворота переписи открыты кнопкой на нынешний предмет
    [ "$(rc_of "$(guard "$1" --действие перепись --только-согласие --перепись "$1/copy")")" = 0 ]
}

set_window() { # $1=стенд $2=минуты: порог свежести «да» в конфиге стенда
    sed -i "s/^CONFIRM_MAX_AGE_MIN=.*/CONFIRM_MAX_AGE_MIN=\"$2\"/" "$1/harness.conf"
}

guard() { # $1=стенд, дальше доводы deploy_guard.py; вывод, код — последней строкой
    local s="$1"; shift
    # Своя обёртка ворот у каждого стенда: у стендов Б3 проекта нет, а ворота нужны.
    [ -f "$s/vorota-obertka.py" ] \
        || obertka_vorot "$s/vorota-obertka.py" "$GUARD_PY" "$s/install.conf" "$s/harness.conf"
    ( export HARNESS_HOME="$s" HARNESS_CONF="$s/harness.conf" HARNESS_INSTALL_CONF="$s/install.conf" \
             HARNESS_LOG_DIR="$s/log" LOG_DIR="$s/log"
      python3 "$s/vorota-obertka.py" "$@" 2>&1
      echo "rc=$?" )
}

refs_of() { # $1=репозиторий: ветки и теги «sha ref», отсортированно
    git -C "$1" for-each-ref --format='%(objectname) %(refname)' refs/heads refs/tags | sort
}

run_publish() { # $1=стенд, дальше — доводы publish.sh; печатает вывод, код — последней строкой
    local s="$1"; shift
    (
        cd "$s" || exit 99
        # Каждый ключ, который загрузчик конфига сохраняет из окружения, задан
        # стендом: унаследованный PROJECT_DIR увёл бы скрипт на живой проект.
        export HARNESS_HOME="$s" HARNESS_CONF="$s/harness.conf" HARNESS_INSTALL_CONF="$s/install.conf" \
               HARNESS_DOOR_STAND_HOME="$s" \
               HARNESS_LOG_DIR="$s/log" LOG_DIR="$s/log" PROJECT_DIR="$s/proj" STARTER_DIR="$s/pkg" \
               PUBLIC_REMOTE=jig PUBLIC_BRANCH=master PUBLIC_BUILD_DIR="$s/build" \
               PUBLIC_COMMIT_NAME=proba-harness PUBLIC_COMMIT_EMAIL=proba@harness.local \
               PUBLIC_PASPORT="$s/install.conf" AGENT_USER=podstavnoj \
               STATE_DIR="$s/state" HEARTBEAT_DIR="$s/state/hb" PANEL_STATE_DIR="$s/state/panel" \
               SECRETS_DIR="$s/state/secrets" PATH="$s/bin:$PATH"
        unset INSTALL_CONF PACKAGE_DIR
        timeout 300 bash "$s/proj/scripts/publish.sh" "$@" 2>&1
        echo "rc=$?"
    )
}

rc_of() { printf '%s\n' "$1" | tail -1 | sed 's/^rc=//'; }

# Строки отказа. Ворота печатают «предмет: …» первой строкой (спека §5: дверь сверяет по ней
# предмет перед пушем), поэтому за «ОТКАЗ: предмет: …» причина ворот — следующей строкой.
refusal_lines() { # $1=вывод
    awk 'p { print; p = 0 } /^ОТКАЗ:/ { print; p = /^ОТКАЗ: предмет: / }' <<< "$1"
}

# Путь нового режима: обычный путь печатает свой заголовок и первым зовёт ворота.
# Вывод судится здесь-строкой, не трубой: под pipefail ранний выход grep -q
# на выводе больше буфера трубы (проба Е3) читается как «не найдено».
in_mode() { # $1=вывод $2=стенд → пусто, если это путь режима; иначе причина
    grep -q '=== публикация' <<< "$1" && { echo "пошёл обычный путь (заголовок публикации)"; return; }
    [ -e "$2/gates-called" ] && { echo "звались ворота обычного пути"; return; }
    grep -q '^опубликовано' <<< "$1" && { echo "ответ обычного пути «опубликовано»"; return; }
    return 0
}

# Отказ двери: $1=название $2=стенд $3=образец причины (egrep, без регистра), дальше доводы.
expect_refusal() {
    local name="$1" s="$2" reason="$3"; shift 3
    local remote_before build_before out rc why=""
    remote_before=$(refs_of "$s/remote.git")
    build_before=$(git -C "$s/build" rev-parse master)
    out=$(run_publish "$s" "$@"); rc=$(rc_of "$out")
    local mode_why; mode_why=$(in_mode "$out" "$s")
    [ -n "$mode_why" ] && why="$why; $mode_why"
    [ "$rc" = 1 ] || why="$why; код $rc вместо 1"
    grep -q '^ОТКАЗ:' <<< "$out" || why="$why; нет строки «ОТКАЗ:»"
    grep -Eqi "$reason" <<< "$(refusal_lines "$out")" || why="$why; причина не названа (ждали /$reason/)"
    [ "$(refs_of "$s/remote.git")" = "$remote_before" ] || why="$why; remote ИЗМЕНЁН"
    [ "$(git -C "$s/build" rev-parse master)" = "$build_before" ] || why="$why; сборка переведена"
    if [ -z "$why" ]; then
        ok "$name"
    else
        bad "$name —${why#;} | $(printf '%s\n' "$out" | grep -v '^rc=' | tail -2 | tr '\n' ' ' | cut -c1-200)"
    fi
}

# Успех двери: $1=название $2=стенд, дальше доводы. Код 0, путь режима, remote == копия.
expect_success() {
    local name="$1" s="$2"; shift 2
    local out rc why=""
    out=$(run_publish "$s" "$@"); rc=$(rc_of "$out")
    why="$why$(m=$(in_mode "$out" "$s"); [ -n "$m" ] && echo "; $m")"
    [ "$rc" = 0 ] || why="$why; код $rc вместо 0"
    [ "$(refs_of "$s/remote.git")" = "$(refs_of "$s/copy")" ] || why="$why; ссылки remote ≠ ссылкам копии"
    if [ -z "$why" ]; then
        ok "$name"
    else
        bad "$name —${why#;} | $(printf '%s\n' "$out" | grep -v '^rc=' | tail -2 | tr '\n' ' ' | cut -c1-200)"
    fi
}

arm_foreign() { # $1=стенд: клон remote с чужим коммитом — для гонки и сверки после отправки
    git clone -q "$1/remote.git" "$1/foreign"
    printf 'чужое\n' > "$1/foreign/chuzhoe.txt"
    commit_all "$1/foreign" "чужой коммит"
}

echo "=== проба двери переписи публичной истории: $PUBLISH_SH ==="
echo "стенд: $ROOT"

# ── СТЕНД: условия, без которых красное не значит «режима нет» ──────────────
S=$(make_stand base clean)
if [ "$(git -C "$S/copy" rev-parse 'master^{tree}')" = "$(git -C "$S/build" rev-parse 'master^{tree}')" ] \
   && [ "$(git -C "$S/copy" rev-parse master)" != "$(git -C "$S/build" rev-parse master)" ]; then
    ok "СТЕНД дерево master копии равно дереву сборки, история другая"
else
    stand_bad "дерево копии не равно дереву сборки или история та же"
fi
case "$(git -C "$S/pkg" remote get-url jig) $(git -C "$S/build" remote get-url jig)" in
    "$S/remote.git $S/remote.git") ok "СТЕНД публичный удалённый — локальный путь стенда, не сеть" ;;
    *) stand_bad "адрес удалённого не локальный путь стенда" ;;
esac
if (cd "$S/remote.git" && git bundle verify "$S/old.bundle" >/dev/null 2>&1); then
    ok "СТЕНД бандл старой истории проходит git bundle verify"
else
    stand_bad "бандл старой истории не проходит verify"
fi
gate() { # $1=стенд $2=каталог: настоящий гейт чистоты на подставном паспорте
    HARNESS_HOME="$1" HARNESS_CONF="$1/harness.conf" HARNESS_INSTALL_CONF="$1/install.conf" \
    python3 "$SCRIPTS_DIR/check-public-clean.py" "$2" --пакет "$1/pkg" --проект "$1/proj" \
        --паспорт "$1/install.conf" --конфиг "$1/harness.conf" 2>&1
}
G_CLEAN=$(gate "$S" "$S/copy"); G_RC=$?
if [ "$G_RC" = 0 ] && ! printf '%s' "$G_CLEAN" | grep -q 'история' \
   && printf '%s' "$G_CLEAN" | grep -q 'следов установки нет'; then
    ok "СТЕНД гейт чистоты по чистой копии: следов нет ни в дереве, ни в истории"
else
    stand_bad "гейт по чистой копии: код $G_RC, $(printf '%s' "$G_CLEAN" | tail -2 | tr '\n' ' ')"
fi
DIRTY=$(make_stand dirty dirty)
G_DIRTY=$(gate "$DIRTY" "$DIRTY/copy"); G_RC=$?
if printf '%s' "$G_DIRTY" | grep -q 'история .*образец=PROJECT_NAME'; then
    ok "СТЕНД гейт чистоты видит след в старой части истории грязной копии (код гейта $G_RC: история — лишь предупреждение)"
else
    stand_bad "гейт не видит след в грязной копии: код $G_RC, $(printf '%s' "$G_DIRTY" | tail -2 | tr '\n' ' ')"
fi

# ── 0: режим распознан ───────────────────────────────────────────────────────
S=$(make_stand c0 clean); consent "$S" '-10 minutes'
expect_refusal "0 режим распознан: копии нет — отказ режима, а не обычный путь" \
    "$S" 'копи' --переписать-историю "$S/net-takoj-kopii" --бэкап "$S/old.bundle"

# ── 1: нет согласия ──────────────────────────────────────────────────────────
S=$(make_stand c1 clean)
expect_refusal "1 нет согласия владельца — отказ, remote не изменён" \
    "$S" 'соглас' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"

# ── 2: согласие старше 3 часов ───────────────────────────────────────────────
S=$(make_stand c2 clean); consent "$S" '-4 hours'
expect_refusal "2 согласие старше 3 часов — отказ" \
    "$S" 'соглас' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"

# ── 3: бэкап не годится ──────────────────────────────────────────────────────
# Испорчен ЗАГОЛОВОК: `git bundle verify` судит заголовок и предпосылки, а
# обрезанный пакет внутри бандла он пропускает (замер 03.11.2026, git 2.43).
S=$(make_stand c3a clean); consent "$S" '-10 minutes'
{ printf 'это не бандл\n'; tail -c +20 "$S/old.bundle"; } > "$S/broken.bundle"
expect_refusal "3а бэкап испорчен (verify не проходит) — отказ" \
    "$S" 'бэкап|бандл|bundle' --переписать-историю "$S/copy" --бэкап "$S/broken.bundle"
S=$(make_stand c3b clean); consent "$S" '-10 minutes'
expect_refusal "3б бэкапа нет — отказ" \
    "$S" 'бэкап|бандл|bundle' --переписать-историю "$S/copy" --бэкап "$S/net.bundle"

# ── 4: на remote лишняя ветка ────────────────────────────────────────────────
S=$(make_stand c4 clean); consent "$S" '-10 minutes'
git -C "$S/build" push -q jig master:refs/heads/chuzhaya-vetka
git -C "$S/remote.git" bundle create "$S/old.bundle" --all >/dev/null 2>&1
expect_refusal "4 на remote ветка, которой нет в копии — отказ с её именем" \
    "$S" 'chuzhaya-vetka' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"

# ── 4б: в копии тег, которого нет у remote ───────────────────────────────────
S=$(make_stand c4b extra-tag); consent "$S" '-10 minutes'
expect_refusal "4б в копии тег, которого нет у remote — отказ с его именем" \
    "$S" 'v9\.9\.9' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"

# ── 5: remote ушёл вперёд между чтением и отправкой ──────────────────────────
# Гонка настоящая: обёртка git в PATH стенда отправляет чужой коммит в remote
# в момент ПЕРВОГО push с --force-with-lease, то есть после ls-remote двери.
# Предварительная сверка её не увидит — остановить может только аренда на
# атомарной отправке.
S=$(make_stand c5 clean); consent "$S" '-10 minutes'
git clone -q "$S/remote.git" "$S/foreign"
printf 'чужое\n' > "$S/foreign/chuzhoe.txt"
commit_all "$S/foreign" "чужой коммит"
FOREIGN=$(git -C "$S/foreign" rev-parse HEAD)
touch "$S/race-armed"
TAGS_BEFORE=$(git -C "$S/remote.git" for-each-ref --format='%(objectname) %(refname)' refs/tags | sort)
BUILD_BEFORE=$(git -C "$S/build" rev-parse master)
OUT=$(run_publish "$S" --переписать-историю "$S/copy" --бэкап "$S/old.bundle"); RC=$(rc_of "$OUT")
WHY=""
[ -e "$S/race-fired" ] || WHY="$WHY; отправки с --force-with-lease не было — гонку судить нечем"
WHY="$WHY$(m=$(in_mode "$OUT" "$S"); [ -n "$m" ] && echo "; $m")"
[ "$RC" = 1 ] || WHY="$WHY; код $RC вместо 1"
printf '%s\n' "$OUT" | grep -q '^ОТКАЗ:' || WHY="$WHY; нет строки «ОТКАЗ:»"
[ "$(git -C "$S/remote.git" rev-parse refs/heads/master)" = "$FOREIGN" ] \
    || WHY="$WHY; master на remote не чужой коммит — дверь перезаписала"
[ "$(git -C "$S/remote.git" for-each-ref --format='%(objectname) %(refname)' refs/tags | sort)" = "$TAGS_BEFORE" ] \
    || WHY="$WHY; теги на remote изменены — отправка не атомарна"
[ "$(git -C "$S/build" rev-parse master)" = "$BUILD_BEFORE" ] || WHY="$WHY; сборка переведена"
if [ -z "$WHY" ]; then
    ok "5 remote сдвинут после чтения — аренда отвергла атомарную отправку, remote не тронут"
else
    bad "5 гонка с арендой —${WHY#;} | $(printf '%s\n' "$OUT" | grep -v '^rc=' | tail -2 | tr '\n' ' ' | cut -c1-200)"
fi

# ── 6: дерево копии не равно дереву сборки ───────────────────────────────────
S=$(make_stand c6 tree-differs); consent "$S" '-10 minutes'
expect_refusal "6 дерево master копии ≠ дереву сборки — отказ" \
    "$S" 'дерев' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"

# ── 6б: след установки в истории копии ───────────────────────────────────────
consent "$DIRTY" '-10 minutes'
expect_refusal "6б гейт чистоты находит след в истории копии — отказ" \
    "$DIRTY" 'след|чистот|образец' --переписать-историю "$DIRTY/copy" --бэкап "$DIRTY/old.bundle"

# ── 7: успех ─────────────────────────────────────────────────────────────────
S=$(make_stand c7 clean); consent "$S" '-10 minutes'
REMOTE_BEFORE=$(refs_of "$S/remote.git")
BUNDLE_SHA=$(sha256sum "$S/old.bundle" | cut -d' ' -f1)
OUT=$(run_publish "$S" --переписать-историю "$S/copy" --бэкап "$S/old.bundle"); RC=$(rc_of "$OUT")
WHY=""
WHY="$WHY$(m=$(in_mode "$OUT" "$S"); [ -n "$m" ] && echo "; $m")"
[ "$RC" = 0 ] || WHY="$WHY; код $RC вместо 0"
[ "$(refs_of "$S/remote.git")" = "$(refs_of "$S/copy")" ] || WHY="$WHY; ссылки remote ≠ ссылкам копии"
[ "$(git -C "$S/build" rev-parse master)" = "$(git -C "$S/copy" rev-parse master)" ] \
    || WHY="$WHY; master сборки не переведён на копию"
[ -z "$(git -C "$S/build" status --porcelain 2>&1)" ] || WHY="$WHY; git status в сборке не пуст"
[ "$(git -C "$S/build" for-each-ref --format='%(objectname) %(refname)' refs/tags | sort)" \
  = "$(git -C "$S/remote.git" for-each-ref --format='%(objectname) %(refname)' refs/tags | sort)" ] \
    || WHY="$WHY; теги сборки ≠ тегам remote"
printf '%s' "$OUT" | grep -q "$BUNDLE_SHA" || WHY="$WHY; sha256 бандла не напечатан"
printf '%s' "$OUT" | grep -qi 'откат' || WHY="$WHY; нет слова «откат»"
printf '%s' "$OUT" | grep -qF "$S/old.bundle" || WHY="$WHY; путь бандла не напечатан"
button_opens "$S" && WHY="$WHY; прежнее «Да» открывает ворота и после переписи — предмет кнопку не погасил"
# Одна отправка: --atomic и аренда на КАЖДЫЙ прежний ref с его прежним sha.
LEASE=$(python3 - "$S/push-args.log" "$REMOTE_BEFORE" <<'PY'
import re, sys
try:
    текст = open(sys.argv[1], encoding="utf-8").read()
except OSError:
    print("push не звался"); sys.exit(0)
блоки = [б.split("\n") for б in текст.split("--- push\n") if б.strip()]
нужно = [строка.split() for строка in sys.argv[2].splitlines() if строка.strip()]
def годен(блок):
    if "--atomic" not in блок:
        return False
    for sha, ref in нужно:
        короткое = re.sub(r"^refs/(heads|tags)/", "", ref)
        if not any(a in (f"--force-with-lease={ref}:{sha}", f"--force-with-lease={короткое}:{sha}")
                   for a in блок):
            return False
    return True
if not any(годен(б) for б in блоки):
    print(f"нет одной отправки с --atomic и арендой на все {len(нужно)} ref (отправок {len(блоки)})")
PY
)
[ -n "$LEASE" ] && WHY="$WHY; $LEASE"
if [ -z "$WHY" ]; then
    ok "7 успех: remote == копия, теги переставлены, сборка переведена, sha256 и откат напечатаны, кнопка погашена предметом"
else
    bad "7 успех —${WHY#;} | $(printf '%s\n' "$OUT" | grep -v '^rc=' | tail -2 | tr '\n' ' ' | cut -c1-200)"
fi

# ── 8: обычный путь по-прежнему только вперёд ────────────────────────────────
# Тот же стенд, что у успеха до переписи: ворота разрешены, remote ушёл вперёд
# чужим коммитом, сборка на старой истории. Обычная публикация обязана
# отказать расхождением и ничего не отправить.
# Согласие — «Да» кнопкой на предмет публикации: текст двери не открывает (06.11.2026).
# Предмет здесь берётся у ворот: его верность судит test_consent_doors.py, а здесь —
# только то, что за открытым замком согласия расхождение всё равно отказывает.
S=$(make_stand c8 clean)
echo 0 > "$S/gates-rc"
git clone -q "$S/remote.git" "$S/foreign"
printf 'чужое\n' > "$S/foreign/chuzhoe.txt"
commit_all "$S/foreign" "чужой коммит"
git -C "$S/foreign" push -q origin HEAD:refs/heads/master
PREDMET=$(guard "$S" --действие публикация --только-согласие --публикация "$S/pkg" | sed -n 's/^предмет: //p' | head -1)
[ -n "$PREDMET" ] || stand_bad "8: ворота не назвали предмет публикации"
python3 - "$S/log" "$PREDMET" <<'PY'
import datetime, json, sys
log, target = sys.argv[1], sys.argv[2]
ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
data = {"nonce": "proba-c8", "вид": "публикация", "цель": target, "message_id": 4000}
with open(f"{log}/consent-buttons.jsonl", "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"v": 1, "ts": ts, "src": "проба", "event": "consent" + ".yes", "outcome": "ok",
                         "data": data}, ensure_ascii=False) + "\n")
PY
: >> "$S/log/inbox.jsonl"
REMOTE_BEFORE=$(refs_of "$S/remote.git")
OUT=$(run_publish "$S"); RC=$(rc_of "$OUT")
if [ "$RC" = 1 ] && printf '%s\n' "$OUT" | grep -q '^ОТКАЗ: расхождение' \
   && [ "$(refs_of "$S/remote.git")" = "$REMOTE_BEFORE" ]; then
    ok "8 обычный режим: расхождение — «ОТКАЗ: расхождение», remote не тронут"
else
    bad "8 обычный режим при расхождении: код $RC | $(printf '%s\n' "$OUT" | grep -v '^rc=' | tail -3 | tr '\n' ' ' | cut -c1-240)"
fi

# ── Б1: у переписи своё согласие, не общее «публикация» ─────────────────────
# Владелец, сказавший «да, публикуй» про очередной релиз, переписи истории не
# разрешал. Запись про перепись открывает дверь — это проба 7.
S=$(make_stand b1a clean); say_consent "$S" '-2 hours' "да, публикуй" > /dev/null
expect_refusal "Б1а «да, публикуй» двухчасовой давности перепись не открывает" \
    "$S" 'соглас' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"

# Обычная публикация только что потратила своё «да»: окно повтора той попытки
# переписи не касается.
S=$(make_stand b1b clean); say_consent "$S" '-20 minutes' "да, публикуй" > /dev/null
G=$(guard "$S" --действие публикация --только-согласие)
if [ "$(rc_of "$G")" = 1 ]; then
    ok "БОЛЬНОЙ СЛУЧАЙ: текстовое «да, публикуй» обычную публикацию не открывает"
else
    bad "текстовое «да, публикуй» открыло публикацию: $(printf '%s' "$G" | tr '\n' ' ' | cut -c1-160)"
fi
expect_refusal "Б1б «да», только что потраченное обычной публикацией, — не «та же попытка» переписи" \
    "$S" 'соглас' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"

# Одно «Да» — одна перепись: сразу после успеха повтор по той же кнопке закрыт — вершина
# удалённого другая, предмет другой. Бэкап переснят, сборка переведена — держит только согласие.
S=$(make_stand b1c clean); consent "$S" '-10 minutes'
OUT=$(run_publish "$S" --переписать-историю "$S/copy" --бэкап "$S/old.bundle")
if [ "$(rc_of "$OUT")" = 0 ]; then
    git -C "$S/remote.git" bundle create "$S/new.bundle" --all >/dev/null 2>&1
    expect_refusal "Б1в повтор переписи по тому же «да» сразу после успеха — отказ" \
        "$S" 'соглас' --переписать-историю "$S/copy" --бэкап "$S/new.bundle"
else
    bad "Б1в первая перепись не прошла — повтор судить нечем: код $(rc_of "$OUT")"
fi

# ── Б2: кнопка не тратится, гасит её только новый предмет ──────────────────
# «Да» кнопкой на публикацию переписи не открывает. «Да» на перепись, отправка упала на
# гонке — кнопка цела: remote вернули на место, тот же предмет, вторая попытка проходит
# той же кнопкой. После переписи предмет другой — та же кнопка ворота не открывает.
S=$(make_stand b2 clean)
PREDMET=$(guard "$S" --действие публикация --только-согласие --публикация "$S/pkg" | sed -n 's/^предмет: //p' | head -1)
[ -n "$PREDMET" ] || stand_bad "Б2: ворота не назвали предмет публикации"
press_yes "$S" '-30 minutes' "$PREDMET" публикация
G=$(guard "$S" --действие публикация --только-согласие --публикация "$S/pkg")
[ "$(rc_of "$G")" = 0 ] || stand_bad "Б2: кнопка «Да» на предмет публикации ворота публикации не открыла"
expect_refusal "Б2 «Да» кнопкой на публикацию перепись не открывает" \
    "$S" 'соглас' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"
consent "$S" '-10 minutes'
arm_foreign "$S"; touch "$S/race-armed"
OUT=$(run_publish "$S" --переписать-историю "$S/copy" --бэкап "$S/old.bundle"); RC=$(rc_of "$OUT")
WHY=""
[ -e "$S/race-fired" ] || WHY="$WHY; гонки не было — отправки с арендой нет"
[ "$RC" = 1 ] || WHY="$WHY; код $RC вместо 1"
printf '%s\n' "$OUT" | grep -q '^ОТКАЗ:' || WHY="$WHY; нет строки «ОТКАЗ:»"
git -C "$S/build" push -q -f jig master
button_opens "$S" || WHY="$WHY; после упавшей отправки и возврата remote кнопка «Да» на тот же предмет не открывает"
OUT2=$(run_publish "$S" --переписать-историю "$S/copy" --бэкап "$S/old.bundle")
[ "$(rc_of "$OUT2")" = 0 ] && [ "$(refs_of "$S/remote.git")" = "$(refs_of "$S/copy")" ] \
    || WHY="$WHY; вторая попытка той же кнопкой не прошла (код $(rc_of "$OUT2"))"
button_opens "$S" && WHY="$WHY; после переписи та же кнопка открывает ворота — предмет её не погасил"
if [ -z "$WHY" ]; then
    ok "Б2 упала отправка — кнопка цела, повтор той же кнопкой прошёл, после переписи она погашена предметом"
else
    bad "Б2 кнопка не тратится —${WHY#;} | $(printf '%s\n' "$OUT" "$OUT2" | grep '^ОТКАЗ' | head -2 | tr '\n' ' ' | cut -c1-200)"
fi

# ── Б3: ключ окна переписи не ослабляет обычные действия ────────────────────
# Двухдневное «да» не годно ни публикации, ни выкату, ни отправке — с каким бы
# ключом ни звали ворота. Свежее то же слово текстом тоже не годно: текст двери
# не открывает (06.11.2026), и ключи стенда строгие — зелёное не значит «ручка отпущена».
for pair in "публикация|да, публикуй" "deploy|да, выкатывай" "push|да, запушь в гит"; do
    act=${pair%%|*}; text=${pair#*|}
    F="$ROOT/b3-fresh-$act"; O="$ROOT/b3-old-$act"
    for d in "$F" "$O"; do mkdir -p "$d/log"; make_configs "$d"; set_window "$d" 60; done
    say_consent "$F" '-5 minutes' "$text" > /dev/null
    say_consent "$O" '-2 days' "$text" > /dev/null
    G=$(guard "$F" --действие "$act" --только-согласие)
    if [ "$(rc_of "$G")" = 1 ]; then ok "Б3 свежее текстовое «$text» «$act» не открывает"
    else bad "Б3 свежее текстовое «$text» открыло «$act»: $(printf '%s' "$G" | tr '\n' ' ' | cut -c1-120)"; fi
    G=$(guard "$O" --действие "$act" --только-согласие --окно-мин 999999)
    G2=$(guard "$O" --действие "$act" --только-согласие)
    WHY=""
    [ "$(rc_of "$G")" != 0 ] || WHY="$WHY; с --окно-мин 999999 дано согласие ($(printf '%s' "$G" | grep -v '^rc=' | tail -1 | cut -c1-80))"
    [ "$(rc_of "$G2")" = 1 ] || WHY="$WHY; после него обычный замок согласия прошёл (код $(rc_of "$G2"))"
    if [ -z "$WHY" ]; then
        ok "Б3 «$act»: двухдневное «да» не годно и с --окно-мин 999999, обычный замок после — отказ"
    else
        bad "Б3 «$act» —${WHY#;}"
    fi
done

# ── П4: сборка впереди remote ───────────────────────────────────────────────
# В сборке неопубликованный коммит, копия повторяет его дерево. Перепись
# вынесла бы наружу то, что обычным путём не публиковалось и ворот не прошло.
S=$(make_stand p4 clean); consent "$S" '-10 minutes'
printf 'неопубликованное\n' > "$S/build/new.txt"; commit_all "$S/build" "не публиковалось"
printf 'неопубликованное\n' > "$S/copy/new.txt"; commit_all "$S/copy" "не публиковалось (копия)"
if [ "$(git -C "$S/copy" rev-parse 'master^{tree}')" = "$(git -C "$S/build" rev-parse 'master^{tree}')" ] \
   && [ "$(git -C "$S/build" rev-parse master)" != "$(git -C "$S/remote.git" rev-parse master)" ]; then
    ok "СТЕНД П4: дерево копии = дереву сборки, сборка впереди remote"
else
    stand_bad "П4: деревья разошлись или сборка не впереди remote"
fi
expect_refusal "П4 сборка впереди remote неопубликованным коммитом — отказ" \
    "$S" '.' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"

# ── П8: лишние доводы ───────────────────────────────────────────────────────
S=$(make_stand p8 clean); consent "$S" '-10 minutes'
expect_refusal "П8 лишний довод после --бэкап <файл> — отказ, а не молчание" \
    "$S" '.' --переписать-историю "$S/copy" --бэкап "$S/old.bundle" --лишнее

# ── m1: срок кнопки на перепись — 60 мин, его задаёт вид, не конфиг (спека §5) ──
S=$(make_stand m1a clean); set_window "$S" 10; consent "$S" '-50 minutes'
expect_success "m1а конфиг 10 мин, «Да» кнопкой на перепись 50 мин назад — перепись идёт" \
    "$S" --переписать-историю "$S/copy" --бэкап "$S/old.bundle"
S=$(make_stand m1b clean); set_window "$S" 60; consent "$S" '-4 hours'
expect_refusal "m1б конфиг 60 мин, «да» 4 часа назад — отказ" \
    "$S" 'соглас' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"
S=$(make_stand m1c clean); set_window "$S" 300; consent "$S" '-4 hours'
expect_refusal "m1в конфиг 300 мин, «да» 4 часа назад — отказ: окно переписи конфигом не растягивается" \
    "$S" 'соглас' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"

# ── m2: бэкап устарел ───────────────────────────────────────────────────────
# После снятия бэкапа на remote ушёл ещё один коммит (пустой: дерево то же).
# Бэкап исправен, но прежнего master в нём нет — откатить было бы не во что.
S=$(make_stand m2 clean); consent "$S" '-10 minutes'
git -C "$S/build" commit -q --allow-empty -m "версия от 2026-11-02"
git -C "$S/build" push -q jig master
expect_refusal "m2 бэкап снят до последней публикации (не покрывает remote) — отказ" \
    "$S" 'бэкап|бандл|bundle' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"

# ── m4: пакет внутри бандла обрезан, заголовок цел ──────────────────────────
S=$(make_stand m4 clean); consent "$S" '-10 minutes'
head -c "$(( $(stat -c %s "$S/old.bundle") - 200 ))" "$S/old.bundle" > "$S/cut.bundle"
if (cd "$S/remote.git" && git bundle verify "$S/cut.bundle" >/dev/null 2>&1); then
    ok "СТЕНД m4: обрезанный бандл проходит git bundle verify — ловить его должна дверь"
else
    stand_bad "m4: verify сам ловит обрезанный пакет — проба не судит развёртывание"
fi
expect_refusal "m4 пакет в бандле обрезан при целом заголовке — отказ" \
    "$S" 'бэкап|бандл|bundle' --переписать-историю "$S/copy" --бэкап "$S/cut.bundle"

# ── m5: сверка после отправки ───────────────────────────────────────────────
# Отправка двери прошла, и сразу за ней в remote появилась чужая ветка: remote
# ≠ копии. Дверь обязана это сказать отказом, а не «переписано».
S=$(make_stand m5 clean); consent "$S" '-10 minutes'
arm_foreign "$S"; touch "$S/after-armed"
OUT=$(run_publish "$S" --переписать-историю "$S/copy" --бэкап "$S/old.bundle"); RC=$(rc_of "$OUT")
WHY=""
[ -e "$S/after-fired" ] || WHY="$WHY; отправка с арендой не прошла — сверку судить нечем"
[ "$RC" != 0 ] || WHY="$WHY; код 0 при remote ≠ копии"
printf '%s\n' "$OUT" | grep -q '^ОТКАЗ:' || WHY="$WHY; нет строки «ОТКАЗ:»"
printf '%s\n' "$OUT" | grep -q '^переписано' && WHY="$WHY; сказано «переписано»"
if [ -z "$WHY" ]; then
    ok "m5 remote ≠ копии после отправки — отказ, а не «переписано»"
else
    bad "m5 сверка после отправки —${WHY#;} | $(printf '%s\n' "$OUT" | grep -v '^rc=' | tail -2 | tr '\n' ' ' | cut -c1-200)"
fi

# ── В: ответ владельца засчитывается, только если дан ПОСЛЕ вопроса двери ───
# Порядок — как в жизни: первый запуск двери → отказ (ворота запомнили вопрос)
# → слово владельца → второй запуск. Порог конфига стенда — боевые 60 минут.
# Открывает только ответ ПОСЛЕ вопроса, явно называющий перепись: голое «да» и
# «моё да» (вид any) переписи не открывают ни до вопроса, ни после — у
# необратимого предмет называет сам владелец, как у сноса базы.
door() { run_publish "$1" --переписать-историю "$1/copy" --бэкап "$1/old.bundle"; }
# Итог запуска одним словом: открыта | отказ | иное(код).
door_verdict() { # $1=вывод $2=стенд $3=ссылки remote до запуска
    if [ "$(rc_of "$1")" = 0 ] && [ "$(refs_of "$2/remote.git")" = "$(refs_of "$2/copy")" ]; then
        echo открыта
    # Без трубы в grep -q: под pipefail ранний выход grep даёт пишущему SIGPIPE,
    # и найденное читается как «не найдено».
    elif [ "$(rc_of "$1")" = 1 ] && grep -qi 'соглас' <<< "$(refusal_lines "$1")" \
         && [ "$(refs_of "$2/remote.git")" = "$3" ]; then
        echo отказ
    else
        echo "иное(код $(rc_of "$1"): $(printf '%s\n' "$1" | grep '^ОТКАЗ:' | head -1 | cut -c1-120))"
    fi
}
# Два запуска подряд: оба обязаны отказать согласием, remote не тронут.
two_refusals() { # $1=название $2=стенд
    local s="$2" before out1 out2 v1 v2
    before=$(refs_of "$s/remote.git")
    out1=$(door "$s"); v1=$(door_verdict "$out1" "$s" "$before")
    out2=$(door "$s"); v2=$(door_verdict "$out2" "$s" "$before")
    if [ "$v1 $v2" = "отказ отказ" ]; then
        ok "$1"
    else
        bad "$1 — первый запуск: $v1, второй: $v2 (ждали отказ, отказ)"
    fi
}
# Вопрос задан первым запуском двери, затем ответ; второй запуск судит ответ.
answer_after_question() { # $1=стенд $2=текст $3=носитель (dispatcher|queue|legacy) → «первый второй»
    local s="$1" before out v1 v2
    before=$(refs_of "$s/remote.git")
    out=$(door "$s"); v1=$(door_verdict "$out" "$s" "$before")
    sleep 1                            # ответ строго позже вопроса, в другой секунде
    case "$3" in
        dispatcher) owner_says "$s" now "$2" > /dev/null ;;
        queue)      owner_says "$s" now "$2" queue > /dev/null ;;
        legacy)     say_consent "$s" now "$2" > /dev/null ;;
    esac
    out=$(door "$s"); v2=$(door_verdict "$out" "$s" "$before")
    echo "$v1 $v2"
}
reset_log() { rm -rf "$1/log"; mkdir -p "$1/log"; }

# В1а: «да» / «моё да» за два часа ДО вопроса — ни первый запуск, ни второй.
for pair in "dispatcher|да" "dispatcher|моё да" "legacy|да"; do
    how=${pair%%|*}; text=${pair#*|}
    S=$(make_stand "v1a-$how-$RANDOM" clean); set_window "$S" 60
    if [ "$how" = legacy ]; then say_consent "$S" '-2 hours' "$text" > /dev/null
    else owner_says "$S" '-2 hours' "$text" > /dev/null; fi
    two_refusals "В1а «$text» ($how) за 2 ч до вопроса двери — отказ и на первом, и на втором запуске" "$S"
done

# В1б: «да» (вид any), уже потраченное выкатом, переписи не открывает.
S=$(make_stand v1b clean); set_window "$S" 60
ask_question "$S" '-50 minutes' deploy
D=$(owner_says "$S" '-50 minutes' "да")
G=$(guard "$S" --действие deploy --только-согласие)
if [ "$(rc_of "$G")" = 1 ]; then
    ok "БОЛЬНОЙ СЛУЧАЙ В1б: текстовое «да» после отказа ворот выкат не открывает"
else
    bad "В1б: текстовое «да» открыло выкат: $(printf '%s' "$G" | tr '\n' ' ' | cut -c1-160)"
fi
two_refusals "В1б «да», потраченное выкатом 50 мин назад, — перепись: отказ и на первом, и на втором запуске" "$S"

# В1в: после успешной переписи старое «моё да» и новый бэкап второй переписи не дают.
S=$(make_stand v1c clean); set_window "$S" 60; consent "$S" '-30 minutes'
OUT=$(door "$S")
if [ "$(rc_of "$OUT")" = 0 ] && [ "$(refs_of "$S/remote.git")" = "$(refs_of "$S/copy")" ]; then
    ok "СТЕНД В1в: первая перепись по кнопке на предмет двери прошла"
    owner_says "$S" '-100 minutes' "моё да" > /dev/null
    git -C "$S/remote.git" bundle create "$S/old.bundle" --all >/dev/null 2>&1
    git -C "$S/copy" commit -q --amend -m "версия от 2026-11-01 (переписано второй раз)"
    two_refusals "В1в после переписи «моё да» (any, 100 мин назад) + новый бэкап — вторая перепись: отказ дважды" "$S"
else
    stand_bad "В1в: первая перепись не прошла (код $(rc_of "$OUT")) — повтор судить нечем"
fi

# В1г: явное слово, сказанное ДО вопроса двери, тоже не ответ на него.
S=$(make_stand v1d clean); set_window "$S" 60
owner_says "$S" '-30 minutes' "да, переписывай публичную историю jig" > /dev/null
two_refusals "В1г «да, переписывай публичную историю jig» за 30 мин ДО вопроса — отказ дважды" "$S"

# В1д: ответ текстом после вопроса, явно называющий перепись, не открывает; «Да» кнопкой на
# предмет из запроса двери — открывает.
for text in "да, переписывай публичную историю jig" "да, перепиши публичную историю jig"; do
    S=$(make_stand "v1e-$RANDOM" clean); set_window "$S" 60
    R=$(answer_after_question "$S" "$text" dispatcher)
    SUBJECT=$(asked_subject "$S")
    [ -n "$SUBJECT" ] && press_yes "$S" now "$SUBJECT"
    BEFORE=$(refs_of "$S/remote.git")
    V3=$(door_verdict "$(door "$S")" "$S" "$BEFORE")
    if [ "$R $V3" = "отказ отказ открыта" ] && ! button_opens "$S"; then
        ok "В1д вопрос → «$text» → отказ; «Да» кнопкой на предмет двери → перепись прошла, кнопка погашена"
    else
        bad "В1д «$text» после вопроса — запуски: $R, по кнопке: $V3 (ждали отказ отказ, открыта), предмет запроса: ${SUBJECT:-нет}"
    fi
done

# В1е и В2: ответ после вопроса, но предмет не назван или назван не тот.
# Голое «да» — вид any; «переписку», «переписал», «переписать можно» судья
# диспетчера тоже пишет видом any; вопрос со знаком «?» — не согласие. Каждая
# фраза — тремя носителями: как пишет диспетчер, только очередь, старая запись
# журнала без вида.
S=$(make_stand v2 clean); set_window "$S" 60
for text in "да" "да, переписку глянул" "да, переписал карту" "да, переписать можно" \
            "да, а зачем переписывать историю?" "да, перепиши публичную историю?"; do
    for how in dispatcher queue legacy; do
        # Открытая дверь переписала remote — следующей фразе нужен свежий стенд.
        [ "$(refs_of "$S/remote.git")" = "$(refs_of "$S/copy")" ] \
            && { S=$(make_stand "v2-$RANDOM" clean); set_window "$S" 60; }
        reset_log "$S"
        R=$(answer_after_question "$S" "$text" "$how")
        if [ "$R" = "отказ отказ" ]; then
            ok "В2 после вопроса «$text» ($how) — перепись не открыта"
        else
            bad "В2 после вопроса «$text» ($how) — запуски: $R (ждали отказ отказ)"
        fi
    done
done

# В3: отрицание после согласия — отказ.
for text in "не переписывай пока" "стоп"; do
    S=$(make_stand "v3-$RANDOM" clean); set_window "$S" 60
    consent "$S" '-40 minutes'; owner_says "$S" '-5 minutes' "$text" > /dev/null
    expect_refusal "В3 «да» на перепись, затем «$text» — отказ" \
        "$S" 'соглас|отмен' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"
done

# ── Г: перепись открывает только ответ на вопрос САМОЙ двери ────────────────
# Отказ ворот любого действия пишет общее «ожидание»; ответ после чужого
# вопроса — ответ на чужой вопрос. Свежесть вопроса двери — окно действия
# (3 часа), а не порог конфига.
for pair in "deploy|перепиши абзац в отчёте, там криво" "deploy|да, переписывай публичную историю jig"; do
    act=${pair%%|*}; text=${pair#*|}
    S=$(make_stand "g1-$RANDOM" clean); set_window "$S" 60
    G=$(guard "$S" --действие "$act" --только-согласие)
    [ "$(rc_of "$G")" = 1 ] || stand_bad "Г1 вопрос «$act»: ворота не отказали"
    sleep 1
    owner_says "$S" now "$text" > /dev/null
    two_refusals "Г1 вопрос ворот «$act», затем «$text» — перепись: отказ и на первом, и на втором запуске двери" "$S"
done
for text in "да, перепиши тест" "да, переписывай публичную историю jig"; do
    S=$(make_stand "g1b-$RANDOM" clean); set_window "$S" 60
    G=$(guard "$S" --действие публикация --только-согласие)
    [ "$(rc_of "$G")" = 1 ] || stand_bad "Г1б вопрос публикации: ворота не отказали"
    owner_says "$S" now "$text" > /dev/null
    two_refusals "Г1б вопрос (отказ) публикации, затем «$text» — перепись: отказ дважды" "$S"
done

# Г2: после вопроса двери открывает только повелительный глагол переписи и в
# той же фразе её предмет — «истори» или имя публичного удалённого. Цитата
# чужих слов — не приказ, даже если кавычка стоит не вплотную к глаголу.
for text in "перепиши описание карточки hr.x, там опечатка" \
            "Глянул отчёт. Карту не трогай пока, а заголовок перепиши покороче. И ещё: напомни про бэкап" \
            "ПЕРЕПИШИ" \
            "перепиши заголовок покороче. историю карточки я прочёл" \
            "он написал «да, переписывай публичную историю jig»"; do
    S=$(make_stand "g2-$RANDOM" clean); set_window "$S" 60
    R=$(answer_after_question "$S" "$text" dispatcher)
    if [ "$R" = "отказ отказ" ]; then
        ok "Г2 после вопроса двери «$text» — перепись не открыта"
    else
        bad "Г2 после вопроса двери «$text» — запуски: $R (ждали отказ отказ)"
    fi
done

# Г3: срок кнопки на перепись — 60 мин вида; конфиг 300 его не растягивает (спека §5).
for age in 55 65 170; do
    S=$(make_stand "g3-$RANDOM" clean); set_window "$S" 300
    consent "$S" "-$age minutes"
    if [ "$age" -lt 60 ]; then
        expect_success "Г3 «Да» кнопкой $age мин назад (срок вида 60, конфиг 300) — перепись идёт" \
            "$S" --переписать-историю "$S/copy" --бэкап "$S/old.bundle"
    else
        expect_refusal "Г3 «Да» кнопкой $age мин назад (срок вида 60, конфиг 300) — отказ: кнопка истекла" \
            "$S" 'соглас|истекл' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"
    fi
done

# ── Д: фраза двери не открывает обычную публикацию ──────────────────────────
# «Одно да — одно действие»: в словах переписи есть «публичн» и имя удалённого,
# то есть предмет публикации, но разрешение дано на перепись.
DOOR_WORD="да, переписывай публичную историю jig"
for how in before-question publication-question door-question; do
    D="$ROOT/d-$how"; mkdir -p "$D/log"; make_configs "$D"; set_window "$D" 60
    case "$how" in
        before-question) owner_says "$D" '-10 minutes' "$DOOR_WORD" > /dev/null ;;
        publication-question)
            guard "$D" --действие публикация --только-согласие > /dev/null; sleep 1
            owner_says "$D" now "$DOOR_WORD" > /dev/null ;;
        door-question)
            guard "$D" --действие перепись --только-согласие > /dev/null; sleep 1
            owner_says "$D" now "$DOOR_WORD" > /dev/null
            G=$(guard "$D" --действие перепись --только-согласие)
            if [ "$(rc_of "$G")" = 1 ]; then ok "Д door-question: текст переписи дверь переписи не открывает"
            else bad "Д door-question: текст открыл перепись: $(grep -v '^rc=' <<< "$G" | tail -1 | cut -c1-120)"; fi ;;
    esac
    G=$(guard "$D" --действие публикация --только-согласие)
    if [ "$(rc_of "$G")" = 1 ] && grep -q 'нет согласия' <<< "$G"; then
        ok "Д «$DOOR_WORD» ($how) — обычная публикация: «нет согласия»"
    else
        bad "Д «$DOOR_WORD» ($how) — публикация: код $(rc_of "$G"), $(grep -v '^rc=' <<< "$G" | tail -1 | cut -c1-120)"
    fi
done

# ── Е: выжившие мутанты ревью-3 ─────────────────────────────────────────────
# Е1: запись вида deploy (диспетчер ставит его фразе про выкат и гит) после
# вопроса двери — выкат и отправка, а не перепись.
S=$(make_stand e1 clean); set_window "$S" 60
R=$(answer_after_question "$S" "да, выкатывай и запушь историю в гит" dispatcher)
if [ "$R" = "отказ отказ" ]; then
    ok "Е1 вид deploy со словами «гит» и «истори» после вопроса двери — перепись не открыта"
else
    bad "Е1 вид deploy после вопроса двери — запуски: $R (ждали отказ отказ)"
fi
if grep -q '"kind": "deploy"' "$S/log/confirmations.jsonl" 2>/dev/null; then
    ok "СТЕНД Е1: диспетчер записал фразу видом deploy"
else
    stand_bad "Е1: в журнале согласий нет записи вида deploy — пробе нечего судить"
fi

# Е2: автомат снимает согласие с обратимого, перепись к нему не относится.
S=$(make_stand e2 clean)
sed -i 's/^AUTONOMY=.*/AUTONOMY="auto"/' "$S/install.conf"
expect_refusal "Е2 AUTONOMY=auto, слова владельца нет — перепись: отказ согласием" \
    "$S" 'соглас' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"

# Е3: вывод гейта чистоты больше буфера трубы. Подмена — обёртка над НАСТОЯЩИМ
# гейтом: его вывод по грязной копии и следом мегабайт строк. Труба в grep -q
# под pipefail читает такой вывод как «чисто» (SIGPIPE пишущего).
S=$(make_stand e3 dirty); consent "$S" '-10 minutes'
rm -f "$S/proj/scripts/check-public-clean.py"
cat > "$S/proj/scripts/check-public-clean.py" <<EOF
import subprocess, sys
rc = subprocess.run([sys.executable, "$SCRIPTS_DIR/check-public-clean.py", *sys.argv[1:]]).returncode
sys.stdout.flush()
for i in range(20000):
    print(f"[чистота] строка стенда {i:05d}: большой вывод гейта, след ищется выше")
sys.exit(rc)
EOF
BIG=$(HARNESS_HOME="$S" HARNESS_CONF="$S/harness.conf" HARNESS_INSTALL_CONF="$S/install.conf" \
      python3 "$S/proj/scripts/check-public-clean.py" "$S/copy" --пакет "$S/pkg" --проект "$S/proj" \
      --паспорт "$S/install.conf" 2>&1)
if [ "${#BIG}" -gt 300000 ] && grep -q '⚠ история' <<< "$BIG"; then
    ok "СТЕНД Е3: вывод подменённого гейта больше 300 КБ и несёт «⚠ история»"
else
    stand_bad "Е3: вывод гейта ${#BIG} знаков, «⚠ история»: $(grep -c '⚠ история' <<< "$BIG")"
fi
expect_refusal "Е3 грязная копия при выводе гейта больше 64 КБ — отказ чистотой" \
    "$S" 'след|чистот|образец' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"

# Е4: в бэкапе тысячи ссылок сверх remote — список голов больше буфера трубы.
# Бэкап покрывает remote, ложного «бэкап устарел» быть не должно.
S=$(make_stand e4 clean); consent "$S" '-10 minutes'
git clone -q --mirror "$S/remote.git" "$S/mirror.git"
MASTER=$(git -C "$S/mirror.git" rev-parse master)
for i in $(seq -w 1 5000); do echo "create refs/tags/zz-lishnij-teg-$i $MASTER"; done \
    | git -C "$S/mirror.git" update-ref --stdin
git -C "$S/mirror.git" bundle create "$S/big.bundle" --all >/dev/null 2>&1
HEADS=$(git bundle list-heads "$S/big.bundle" 2>/dev/null)
if [ "${#HEADS}" -gt 200000 ] && grep -q ' refs/heads/master$' <<< "$HEADS"; then
    ok "СТЕНД Е4: голов в бэкапе ${#HEADS} знаков, master в них есть"
else
    stand_bad "Е4: список голов бэкапа ${#HEADS} знаков"
fi
expect_success "Е4 бэкап с 5000 лишних ссылок покрывает remote — перепись идёт, а не «бэкап устарел»" \
    "$S" --переписать-историю "$S/copy" --бэкап "$S/big.bundle"

# ── Ж: блокеры ревью-4 ──────────────────────────────────────────────────────
# Ж1: оговорка в том же сообщении останавливает перепись, где бы она ни стояла:
# окно в три слова от глагола её не видит («— но не сегодня», «. Хотя нет»).
S=$(make_stand zh1 clean); set_window "$S" 60
for text in "переписывай историю jig, но не сейчас" \
            "переписывай историю jig. Хотя нет, подожди" \
            "перепиши историю jig завтра" \
            "переписывай историю jig — но не сегодня" \
            "переписывай историю jig, только сначала покажи мне diff" \
            "переписывай историю jig, когда скажу" \
            "да. переписывай публичную историю jig. только сначала бэкап проверь"; do
    [ "$(refs_of "$S/remote.git")" = "$(refs_of "$S/copy")" ] \
        && { S=$(make_stand "zh1-$RANDOM" clean); set_window "$S" 60; }
    reset_log "$S"
    R=$(answer_after_question "$S" "$text" dispatcher)
    if [ "$R" = "отказ отказ" ]; then
        ok "Ж1 после вопроса двери «$text» — оговорка в сообщении, перепись не открыта"
    else
        bad "Ж1 после вопроса двери «$text» — запуски: $R (ждали отказ отказ)"
    fi
done
# Без оговорки та же фраза текстом тоже не открывает; открывает «Да» кнопкой на предмет двери.
S=$(make_stand "zh1-plain" clean); set_window "$S" 60
R=$(answer_after_question "$S" "переписывай историю jig" dispatcher)
SUBJECT=$(asked_subject "$S")
[ -n "$SUBJECT" ] && press_yes "$S" now "$SUBJECT"
BEFORE=$(refs_of "$S/remote.git")
V3=$(door_verdict "$(door "$S")" "$S" "$BEFORE")
if [ "$R $V3" = "отказ отказ открыта" ]; then
    ok "Ж1 вопрос → «переписывай историю jig» без оговорок → отказ; «Да» кнопкой → перепись прошла"
else
    bad "Ж1 «переписывай историю jig» после вопроса — запуски: $R, по кнопке: $V3 (ждали отказ отказ, открыта)"
fi

# Ж2: отзыв по предмету переписи — история или имя публичного удалённого, а
# не только корень «перепис»: «да» дано и не потрачено, следующее слово его снимает.
for text in "историю jig пока не трогай" "jig пока не трогай" \
            "с историей jig погоди, сначала покажи мне копию" "отмени перепись истории"; do
    S=$(make_stand "zh2-$RANDOM" clean); set_window "$S" 60
    consent "$S" '-2 minutes'; owner_says "$S" now "$text" > /dev/null
    expect_refusal "Ж2 «да» на перепись, затем «$text» — согласие отозвано, отказ" \
        "$S" 'соглас|отмен' --переписать-историю "$S/copy" --бэкап "$S/old.bundle"
done

# Ж3: цитата в обычных кавычках — чужие слова, как и в «ёлочках».
S=$(make_stand zh3 clean); set_window "$S" 60
for text in 'в отчёте стоит "агент, перепиши историю jig" — убери это' \
            'он написал "да, переписывай публичную историю jig"'; do
    [ "$(refs_of "$S/remote.git")" = "$(refs_of "$S/copy")" ] \
        && { S=$(make_stand "zh3-$RANDOM" clean); set_window "$S" 60; }
    reset_log "$S"
    R=$(answer_after_question "$S" "$text" dispatcher)
    if [ "$R" = "отказ отказ" ]; then
        ok "Ж3 после вопроса двери «$text» — цитата, перепись не открыта"
    else
        bad "Ж3 после вопроса двери «$text» — запуски: $R (ждали отказ отказ)"
    fi
done

# ── З: метка копии называет прошлый пакет ───────────────────────────────────
# Дерево копии равно уже опубликованному дереву сборки, а оно собрано из пакета,
# которого в HEAD давно нет: пересобрать его нельзя, не изменив дерево. Свежесть
# метки для двери не судится; образцы, дерево и история — судятся.
door_out() { # $1=вывод → строки вывода без кода, в одну строку для отчёта
    printf '%s\n' "$1" | grep -v '^rc=' | tail -3 | tr '\n' ' ' | cut -c1-240
}
S=$(make_stand z1 clean-stale)
LABEL=$(sed -n 's/^пакет: *//p' "$S/copy/.публичная-сборка")
if [ "$LABEL" = "$STALE_PKG" ] && ! git -C "$S/pkg" cat-file -e "$STALE_PKG^{commit}" 2>/dev/null \
   && [ "$(git -C "$S/copy" rev-parse 'master^{tree}')" = "$(git -C "$S/build" rev-parse 'master^{tree}')" ] \
   && [ "$(sed -n 's/^пакет: *//p' "$S/build/.публичная-сборка")" = "$STALE_PKG" ]; then
    ok "СТЕНД З: метка копии и сборки — пакет $STALE_PKG, которого в пакете стенда нет; деревья равны"
else
    stand_bad "З: метка копии «$LABEL», деревья или метка сборки не те"
fi
consent "$S" '-10 minutes'
OUT=$(run_publish "$S" --переписать-историю "$S/copy" --бэкап "$S/old.bundle"); RC=$(rc_of "$OUT")
WHY=""
WHY="$WHY$(m=$(in_mode "$OUT" "$S"); [ -n "$m" ] && echo "; $m")"
[ "$RC" = 0 ] || WHY="$WHY; код $RC вместо 0"
[ "$(refs_of "$S/remote.git")" = "$(refs_of "$S/copy")" ] || WHY="$WHY; ссылки remote ≠ ссылкам копии"
grep -q 'остатки прошлой сборки' <<< "$OUT" && WHY="$WHY; дверь судила свежесть метки"
if [ -z "$WHY" ]; then
    ok "З1 метка копии — прошлый пакет, история чистая — гейт пройден, перепись успешна"
else
    bad "З1 устаревшая метка, чистая история —${WHY#;} | $(door_out "$OUT")"
fi

# З2: та же метка, след только в старом коммите — отказ находкой, не меткой.
S=$(make_stand z2 dirty-stale); consent "$S" '-10 minutes'
REMOTE_BEFORE=$(refs_of "$S/remote.git")
OUT=$(run_publish "$S" --переписать-историю "$S/copy" --бэкап "$S/old.bundle"); RC=$(rc_of "$OUT")
WHY=""
WHY="$WHY$(m=$(in_mode "$OUT" "$S"); [ -n "$m" ] && echo "; $m")"
[ "$RC" = 1 ] || WHY="$WHY; код $RC вместо 1"
grep -Eqi 'след|образец|истори' <<< "$(refusal_lines "$OUT")" || WHY="$WHY; в ОТКАЗ нет причины /след|образец|истори/"
grep -q 'остатки прошлой сборки' <<< "$OUT" && WHY="$WHY; отказ по свежести метки, а не по следу"
grep -q 'история .*образец=PROJECT_NAME' <<< "$OUT" || WHY="$WHY; находка в истории (образец=PROJECT_NAME) не напечатана"
[ "$(refs_of "$S/remote.git")" = "$REMOTE_BEFORE" ] || WHY="$WHY; remote ИЗМЕНЁН"
button_opens "$S" || WHY="$WHY; кнопка погасла на отказе — предмет тот же"
if [ -z "$WHY" ]; then
    ok "З2 метка — прошлый пакет, след в старом коммите — отказ находкой в истории, не меткой"
else
    bad "З2 устаревшая метка, грязная история —${WHY#;} | $(door_out "$OUT")"
fi

# З3: метки нет вовсе — это не собранное дерево; отказ гейта остаётся.
S=$(make_stand z3 no-label); consent "$S" '-10 minutes'
if [ ! -e "$S/copy/.публичная-сборка" ] \
   && [ "$(git -C "$S/copy" rev-parse 'master^{tree}')" = "$(git -C "$S/build" rev-parse 'master^{tree}')" ]; then
    ok "СТЕНД З3: метки в копии нет, деревья копии и сборки равны"
else
    stand_bad "З3: метка в копии есть или деревья разошлись"
fi
REMOTE_BEFORE=$(refs_of "$S/remote.git")
OUT=$(run_publish "$S" --переписать-историю "$S/copy" --бэкап "$S/old.bundle"); RC=$(rc_of "$OUT")
WHY=""
WHY="$WHY$(m=$(in_mode "$OUT" "$S"); [ -n "$m" ] && echo "; $m")"
[ "$RC" = 1 ] || WHY="$WHY; код $RC вместо 1"
grep -q '^ОТКАЗ:' <<< "$OUT" || WHY="$WHY; нет строки «ОТКАЗ:»"
grep -q 'нет метки' <<< "$OUT" || WHY="$WHY; гейт не сказал «нет метки»"
[ "$(refs_of "$S/remote.git")" = "$REMOTE_BEFORE" ] || WHY="$WHY; remote ИЗМЕНЁН"
if [ -z "$WHY" ]; then
    ok "З3 метки в копии нет — отказ гейта «не собранное дерево», remote не тронут"
else
    bad "З3 копия без метки —${WHY#;} | $(door_out "$OUT")"
fi

# З4: общий гейт без флага свежесть по-прежнему судит.
S="$ROOT/z1"
G=$(gate "$S" "$S/copy"); G_RC=$?
if [ "$G_RC" = 2 ] && grep -q 'остатки прошлой сборки' <<< "$G"; then
    ok "З4 гейт чистоты без флага при метке прошлого пакета — код 2 «остатки прошлой сборки»"
else
    bad "З4 гейт без флага ослаб: код $G_RC | $(printf '%s' "$G" | tail -2 | tr '\n' ' ' | cut -c1-200)"
fi

echo "проб: $TOTAL, красных: $FAILED (из них стенда: $STAND_FAILED)"
if [ "$FAILED" = 0 ]; then
    echo "ТЕСТ ПРОЙДЕН: $TOTAL проб"
    exit 0
fi
echo "ТЕСТ ПРОВАЛЕН: красных $FAILED из $TOTAL"
exit 1
