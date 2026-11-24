#!/usr/bin/env bash
#
# publish.sh — перенести ПРОВЕРЕННОЕ в публичную ветку харнеса.
#
# Задача владельца 11.09.2026: «мне нужно чтобы было две версии гита — ту
# которая проверена и я ее публично публикую, и та, которую я доделываю».
#
# Отсюда смысл двери: публичная ветка показывает людям то, что владелец
# ПРОВЕРИЛ. Поэтому здесь три замка, и каждый из них — про это:
#   1) ворота харнеса зелёные (иначе публикуем заведомо больное);
#   2) согласие владельца с предметом ПУБЛИКАЦИИ — не «выкатывай, да» и не
#      «на гит отправляй, да»: одно «да» — одно действие (правило владельца);
#   3) каждая задача, попадающая в публичную ветку, в dev-map.yaml имеет
#      статус `done` (проверено владельцем) либо `owner_testable: false`.
#      Это И-4 на витрине: непроверенное не выдаётся за проверенное.
#
# Имена веток — ДАННЫЕ (harness.conf): PUBLIC_BRANCH и PUBLIC_REMOTE. Своих
# умолчаний нет — схему выбирает владелец, а не код.
#
# Запуск:
#   bash scripts/publish.sh            # проверить и опубликовать
#   bash scripts/publish.sh --проверка # только проверить, ничего не менять
#   bash scripts/publish.sh [--проверка] --поезд [<sha пакета>]
#                                       # поезд: ворота по разнице с прошлым выпуском,
#                                       # согласие и публикация — срез пакета
#   bash scripts/publish.sh --selftest
#   bash scripts/publish.sh --переписать-историю <каталог-копии> --бэкап <файл.bundle>
#                                       # перепись публичной истории (см. ниже)
#
# Код возврата: 0 — опубликовано (или проверка прошла), 1 — отказ замка,
# 2 — не настроено.
set -uo pipefail

# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/config.sh"
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/events.sh"
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/release_mark.sh"
# PUBLIC_PASPORT — путь паспорта для сборщика: только от паспорта двери (ниже), не из окружения;
# самотест называет свой двор присваиванием уже ПОСЛЕ загрузки.
# Так же TYPICAL_COPY_CHECK — судья замка 6г: из окружения боевой запуск подменил бы его и прошёл замок.
konf_zagruzit_dveri STARTER_DIR PUBLIC_BRANCH PUBLIC_REMOTE PUBLIC_BUILD_DIR PUBLIC_COMMIT_EMAIL PUBLIC_PASPORT PAKET_SHA PREDMET_ZAMKA TYPICAL_COPY_CHECK MERGE_QUEUE_WAIT_MIN

# Судья слова владельца — рядом с этим скриптом, а не по окружению: путь от реального файла.
SCRIPTS_REAL="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
PROJECT_DIR="${PROJECT_DIR:?пуст PROJECT_DIR — не прочитан паспорт установки}"
# Имя агента — из паспорта: вшитое сюда, оно след НАШЕЙ установки.
PACKAGE_DIR="${STARTER_DIR:-/home/$(konf_iz_fajla AGENT_USER)/starter/STARTER-PACKAGE}"

say() { printf '%s\n' "$*"; }

# ── Манифест слоя 1: что в выпуске личность и стандарты ────────────────────
# Эталон личности печатает root-скрипт self-seal по ЭТОМУ манифесту, поэтому
# манифест пишется тут, на выпуске, а не в момент печати. Слой 2 (instance.yaml)
# в манифест не входит: он у каждой установки свой. Прошлый манифест читается
# ДО записи нового — он может лежать там же, куда пишем.
# Строка, различная в старом и новом, — файл изменён, добавлен или удалён.
# $1=прошлый манифест (текст), $2=новый (текст).
сказать_смену_слоя1() {
    local changed
    changed="$(printf '%s\n%s\n' "$1" "$2" | grep . | LC_ALL=C sort | uniq -u \
        | sed 's/^[0-9a-f]\{64\}  //' | LC_ALL=C sort -u | paste -sd, | sed 's/,/, /g')"
    [ -z "$changed" ] || say "меняются личность/стандарты: $changed"
}

# Дверь для запроса согласия (ask-consent.py): считает и пишет манифест в пакет.
# Выпуск его не использует — манифест публичной сборки пишет build-public.py.
# $1=пакет, $2=прошлый манифест (нет файла — прошлого нет, названы все файлы).
layer1_sums() { # $1=каталог self → строки sha256sum слоя 1
    (cd "$1" && find . -type f ! -path ./MANIFEST.sha256 ! -path ./instance.yaml -printf '%P\n' \
        | LC_ALL=C sort | xargs -d '\n' -r sha256sum)
}

manifest_sloya1() {
    local self="$1/harness/self" prev="$2" old="" new
    [ -d "$self" ] || { say "ОТКАЗ: у пакета нет каталога $self"; return 1; }
    [ -f "$prev" ] && old="$(LC_ALL=C sort "$prev")"
    new="$(layer1_sums "$self")"
    [ -n "$new" ] || { say "ОТКАЗ: в $self нет файлов слоя 1"; return 1; }
    printf '%s\n' "$new" > "$self/.MANIFEST.sha256.new" && mv -f "$self/.MANIFEST.sha256.new" "$self/MANIFEST.sha256" \
        || { say "ОТКАЗ: манифест слоя 1 не записан в $self"; return 1; }
    сказать_смену_слоя1 "$old" "$new"
    return 0
}

# Срез поезда: слой 1 читается из коммита среза. Манифест в пакет не пишется — манифест
# пакета описывает его рабочее дерево (его сверяет check-self-manifest.py), а не срез.
# $1=пакет, $2=прошлый манифест, $3=sha среза.
manifest_sloya1_srez() {
    local view old="" new=""
    view=$(mktemp -d) || { say "ОТКАЗ: временный каталог среза не создан"; return 1; }
    if git -C "$1" archive "$3" harness/self 2>/dev/null | tar -x -C "$view" 2>/dev/null; then
        new="$(layer1_sums "$view/harness/self")"
    fi
    rm -rf "$view"
    [ -n "$new" ] || { say "ОТКАЗ: в срезе ${3:0:12} пакета нет файлов слоя 1"; return 1; }
    [ -f "$2" ] && old="$(LC_ALL=C sort "$2")"
    сказать_смену_слоя1 "$old" "$new"
}

# Дверь стоит ДО проверки веток: манифест не про публичную ветку, и запрос
# согласия зовёт её на каждую публикацию.
if [ "${1:-}" = "--манифест-слоя1" ]; then
    case "$#" in
        3) manifest_sloya1 "$2" "$3" || exit 1 ;;
        4) manifest_sloya1_srez "$2" "$3" "$4" || exit 1 ;;
        *) say "ОТКАЗ: нужно --манифест-слоя1 <пакет> <прошлый манифест> [<sha среза>]"; exit 1 ;;
    esac
    exit 0
fi

# ── Дверь замка 1: слово владельца на пропуск полного прогона ──────────────
# Замок «ворота зелёные» поставил владелец и вправе снять его на выпуск; снятие
# обязано быть законной дверью, а не обходом ([[a-guard-without-a-legal-door]]).
# Дверь — файл $LOG_DIR/publish-gates-waiver.json: дословная цитата, id входящего,
# причина, срок. Срок конечный (не больше 6 часов с выдачи): запись без срока — не
# дверь. Окружение дверью быть не может. Вместо полного прогона идёт быстрое
# подмножество, и пропуск пишется в журнал публикации.
# Просроченная запись — то же, что записи нет: слово владельца отслужило, и отказ
# на ней держал бы выпуск до ручной правки файла.
# Коды: 0 — дверь открыта (цитата в stdout), 3 — записи нет или она просрочена
# (строка о просрочке в stderr), 1 — запись негодна.
PRAVO_NA_PROPUSK_MAX=21600
proverit_zapis_propuska() { # $1=каталог журнала
    python3 -I - "$1/publish-gates-waiver.json" "$PRAVO_NA_PROPUSK_MAX" <<'PY'
import json, sys, time
from pathlib import Path
путь, потолок = Path(sys.argv[1]), int(sys.argv[2])
if not путь.is_file():
    sys.exit(3)
try:
    з = json.loads(путь.read_text(encoding="utf-8"))
    поля = [str(з.get(k) or "").strip() for k in ("слово", "id_входящего", "причина")]
    выдана, до = int(з["выдана"]), int(з["до"])
except (OSError, ValueError, KeyError, TypeError, AttributeError) as беда:
    print(f"запись о пропуске ворот нечитаема: {беда}", file=sys.stderr); sys.exit(1)
if not all(поля):
    print("в записи о пропуске пусты слово, id входящего или причина", file=sys.stderr); sys.exit(1)
if not 0 < до - выдана <= потолок:
    print(f"срок записи о пропуске вне 0..{потолок} с", file=sys.stderr); sys.exit(1)
if до < time.time():
    срок = time.strftime("%Y-%m-%d %H:%M", time.localtime(до))
    print(f"пропуск ворот просрочен (до {срок}) — как нет пропуска", file=sys.stderr); sys.exit(3)
print(поля[0])
PY
}
# Та же запись снимает и замок 3 (статусы задач): слово «без прогона полного»
# владелец сказал про выпуск целиком. Честность держится оглаской: журнал
# публикации и список непроверенных в канал владельцу, статус done не ставится.
# $1=каталог журнала, $2=список непроверенных; 0 — пропуск по записи, иначе 1.
propustit_statusy_po_slovu() {
    local katalog="$1" spisok="$2" slovo
    slovo=$(proverit_zapis_propuska "$katalog" 2>/dev/null) || return 1
    printf '%s статусы пропущены по слову владельца «%s»; непроверенных: %s\n' \
        "$(date -Is)" "$slovo" "$(printf '%s\n' "$spisok" | grep -c .)" >> "$katalog/publish.log"
    say "замок 3 · статусы пропущены по слову владельца «$slovo»; уезжает непроверенное — $(printf '%s\n' "$spisok" | grep -c .) задач(и):"
    printf '%s\n' "$spisok" | sed 's/^/  • /'
    [ "$CHECK_ONLY" = 1 ] && return 0
    bash "$SCRIPTS_REAL/tg_send.sh" "Публикую по твоему слову «$slovo». В публичную ветку уезжает непроверенное тобой: $(printf '%s\n' "$spisok" | sed 's/ (статус.*//' | paste -sd, | sed 's/,/, /g')" >/dev/null 2>&1 || true
}

# Полные ворота идут десятки минут; если последняя ПОЛНАЯ строка итога (режим
# «обычный» или «всё») снята на этом же sha чистого дерева, а дерево чисто и сейчас,
# второй прогон ничего не проверит нового. Правило то же, что у самих ворот: они
# выходят кодом суда карантина, красные под записью карантина ими приняты —
# принимает и замок. Строки хакатона и выборки полную не заслоняют; строка без
# «код» (старый формат) права не даёт.
# Строка на sha-предке HEAD годна, если с неё и в рабочем дереве легли только
# служебные отметки — добавленные строки в классе DAEMON_OWNED_PATHS под память/
# (судья service-only-diff.py): их пишут демоны, коды и проверки они не меняют.
# Печатает время итога; 0 — итог годен.
gates_summary_reusable() { # $1=каталог журнала
    local summary="$1/ворота-итог.jsonl" head found sha ts
    [ -s "$summary" ] || return 1
    head=$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null) || return 1
    found=$(python3 -I - "$summary" "$head" "$PROJECT_DIR" <<'PY'
import json, subprocess, sys
try:
    lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
except OSError:
    sys.exit(1)
head, project = sys.argv[2], sys.argv[3]


def on_head_line(sha):
    return sha == head or subprocess.run(
        ["git", "-C", project, "merge-base", "--is-ancestor", sha, head],
        capture_output=True).returncode == 0


rows = []
for line in lines:
    try:
        row = json.loads(line)
    except ValueError:
        continue
    if isinstance(row, dict) and row.get("режим") in ("обычный", "всё") and row.get("sha"):
        rows.append(row)
last = next((row for row in reversed(rows) if on_head_line(str(row["sha"]))), None)
if last is None or not (last.get("код") == 0 and last.get("новых") == 0
                        and last.get("чисто") is True):
    sys.exit(1)
print(f'{last["sha"]}\t{last.get("ts", "?")}')
PY
) || return 1
    sha=${found%%$'\t'*}; ts=${found#*$'\t'}
    if [ "$sha" != "$head" ] || [ -n "$(git -C "$PROJECT_DIR" status --porcelain 2>/dev/null)" ]; then
        (cd "$PROJECT_DIR" && python3 -I "$SCRIPTS_REAL/service-only-diff.py" "$sha" --под память/ \
            >/dev/null 2>&1) || return 1
    fi
    printf 'итог от %s\n' "$ts"
}

# Путь «по слову» не гоняет check-secrets.sh сам: скан секретов в составе быстрых
# ворот держат данные gates-hackathon.yaml рядом со скриптами ворот. Нет файла или
# нет проверки, чей cmd зовёт check-secrets.sh, — публиковать без скана нельзя.
hackathon_gates_scan_secrets() { # $1=каталог скриптов ворот
    python3 -I - "$1/../harness/config/gates-hackathon.yaml" <<'PY'
import sys
try:
    import yaml
    checks = yaml.safe_load(open(sys.argv[1], encoding="utf-8")).get("checks") or []
    found = any(isinstance(c, dict) and "check-secrets.sh" in str(c.get("cmd", ""))
                for c in checks)
except (OSError, ValueError, AttributeError, ImportError):
    found = False
sys.exit(0 if found else 1)
PY
}

zamok_vorot() { # $1=каталог журнала [$2=каталог скриптов ворот] → 0 пустить, 1 отказ
    local katalog="$1" skripty="${2:-$SCRIPTS_REAL}" slovo rc itog
    slovo=$(proverit_zapis_propuska "$katalog" 2>/tmp/публикация-пропуск.err); rc=$?
    if [ "$rc" = 3 ]; then
        [ -s /tmp/публикация-пропуск.err ] && say "замок 1 · $(cat /tmp/публикация-пропуск.err)"
        if itog=$(gates_summary_reusable "$katalog"); then
            say "замок 1 · ворота не гоняю: взят зелёный полный итог того же sha ($itog)"; return 0
        fi
        bash "$skripty/gates.sh" > /tmp/публикация-ворота.log 2>&1 || {
            say "ОТКАЗ: ворота красные — публиковать нельзя. Хвост:"
            tail -5 /tmp/публикация-ворота.log; return 1; }
        say "замок 1 · ворота зелёные"; return 0
    fi
    if [ "$rc" != 0 ]; then
        say "ОТКАЗ: $(cat /tmp/публикация-пропуск.err)"; return 1
    fi
    if ! hackathon_gates_scan_secrets "$skripty"; then
        say "ОТКАЗ: в составе быстрых ворот (gates-hackathon.yaml) нет проверки секретов — по слову публиковать нельзя"
        return 1
    fi
    if ! { bash "$skripty/gates.sh" --хакатон \
           && python3 -I "$skripty/build-public.py" --selftest; } \
           > /tmp/публикация-ворота.log 2>&1; then
        say "ОТКАЗ: быстрые ворота красные — публиковать нельзя. Хвост:"
        tail -5 /tmp/публикация-ворота.log; return 1
    fi
    printf '%s ворота пропущены по слову владельца «%s»; быстрое подмножество зелёное\n' \
        "$(date -Is)" "$slovo" >> "$katalog/publish.log"
    say "замок 1 · ворота пропущены по слову владельца «$slovo»; быстрое подмножество зелёное"
}

# ── Уборка на выходе: один обработчик на весь ход ──────────────────────────
# Шаг, заводящий временное, вписывает его сюда, а не ставит свой trap: второй
# trap на EXIT молча снимает первый.
CLEANUP_DIRS=()
QUEUE_LOCK_HOLDER=""
cleanup_on_exit() {
    local rc=$?
    if [ "${TRAIN:-0}" = 1 ]; then train_record "${LOG_DIR:-}" "$rc"; fi
    train_slice_tree_remove
    release_train_lock
    release_merge_queue_lock
    release_mark_drop
    local d
    for d in "${CLEANUP_DIRS[@]}"; do rm -rf "$d"; done
}
trap cleanup_on_exit EXIT

# ── Замок очереди слияния на всё время публикации ──────────────────────────
# Предмет согласия (замок 2) — HEAD пакета, а очередь слияния двигает его, пока идут
# ворота. Публикация берёт ТОТ ЖЕ файл flock, что take_lock в merge-queue.py, и держит
# его до выхода — очередь в это время ждёт.
# Замок держит отдельный процесс-держатель, а не дескриптор этой оболочки: дескриптор
# унаследовали бы ворота, судьи и tg_send, и их осиротевший потомок держал бы очередь
# после выхода публикации. Держатель — coproc: его трубу bash детям не передаёт, и
# держатель отпускает замок, как только трубу закрыл последний её владелец — эта
# оболочка, даже убитая без обработчика выхода.
MERGE_QUEUE_LOCK_NAME="merge-queue.lock"
read -r -d '' MERGE_QUEUE_HOLDER_PY <<'PY' || true
import fcntl, sys, time
путь, предел = sys.argv[1], float(sys.argv[2])
try:
    замок = open(путь, "a")
except OSError as беда:
    print(f"ОТКАЗ: файл замка очереди слияния не открыт: {беда}", flush=True); sys.exit(1)
начало, сказано = time.monotonic(), False
while True:
    try:
        fcntl.flock(замок, fcntl.LOCK_EX | fcntl.LOCK_NB); break
    except BlockingIOError:
        if not сказано:
            print("занято", flush=True); сказано = True
        if time.monotonic() - начало > предел:
            print(f"ОТКАЗ: очередь слияния занята дольше {предел:g} с — публикация не начата", flush=True); sys.exit(1)
        time.sleep(0.2)
print("взят", flush=True)
sys.stdin.read()
PY
take_merge_queue_lock() { # $1=каталог журнала $2=предел ожидания, с → 0 взят, 1 отказ
    local otvet stroka
    # stderr держателя — в ту же трубу: чужой поток вывода он держал бы открытым, пока жив.
    coproc QUEUE_LOCK { exec python3 -I -c "$MERGE_QUEUE_HOLDER_PY" "$1/$MERGE_QUEUE_LOCK_NAME" "$2" 2>&1; }
    # bash снимает имена coproc, когда держатель выходит: номер и pid — в свои переменные.
    otvet=${QUEUE_LOCK[0]}
    QUEUE_LOCK_HOLDER=$QUEUE_LOCK_PID
    while read -r -u "$otvet" stroka; do
        case "$stroka" in
            взят) say "замок очереди слияния взят: слияния ждут конца публикации"; return 0 ;;
            занято) say "очередь слияния занята, жду до $2 с…" ;;
            *) say "$stroka" ;;
        esac
    done
    QUEUE_LOCK_HOLDER=""
    return 1
}
# Отпускает сразу, не дожидаясь закрытия трубы на выходе оболочки.
release_merge_queue_lock() {
    [ -n "$QUEUE_LOCK_HOLDER" ] || return 0
    kill "$QUEUE_LOCK_HOLDER" 2>/dev/null
    wait "$QUEUE_LOCK_HOLDER" 2>/dev/null
    QUEUE_LOCK_HOLDER=""
}

# ── Поезд выпусков (--поезд): ворота судят разницу с прошлым выпуском, предмет — срез ──
# Полные ворота судят всё дерево часами: старое красное держит выпуск, а пакет тем временем
# уходит вперёд и сжигает кнопку. Поезд режет срез — HEAD master проекта и HEAD пакета (или
# названный sha пакета) — и судит, собирает и публикует именно его; ворота — ядро из
# release-train.yaml и проверки gates.sh, задетые диапазоном «прошлый выпуск..срез».
TRAIN=0
TRAIN_JOURNAL="release-train.jsonl"
TRAIN_CONFIG="harness/config/release-train.yaml"
TRAIN_STATE=""
TRAIN_SLICE_PROJECT="" TRAIN_SLICE_PACKAGE="" TRAIN_PREV_TAG="" TRAIN_PREV_PROJECT="" TRAIN_PREV_LINK=""
TRAIN_SELECTED="" TRAIN_RED="" TRAIN_RED_NAMES="" TRAIN_QUARANTINED=0 TRAIN_QUARANTINED_NAMES="" TRAIN_CORE="" TRAIN_SEC_CORE="" TRAIN_SEC_SELECTION=""
TRAIN_GATES=""
TRAIN_STARTED=0
TRAIN_TREE="" TRAIN_GATES_LOG=""
RELEASE_TAG=""
PACKAGE_VIEW=""

# Замок поезда: второй ход при идущем первом отказывает сразу, а не судит и не пишет вперемешку.
# Замок держит отдельный процесс, а не дескриптор оболочки — по той же причине, что у очереди
# (дескриптор унаследовали бы ворота). Держатель живёт, пока жив его родитель-оболочка: убитая
# без обработчика выхода, она замок не удержит.
TRAIN_LOCK_NAME="release-train.lock"
TRAIN_LOCK_HOLDER=""
read -r -d '' TRAIN_LOCK_HOLDER_PY <<'PY' || true
import fcntl, os, sys, time
path, owner = sys.argv[1], int(sys.argv[2])
try:
    lock = open(path, "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("занято", flush=True); sys.exit(1)
except OSError as error:
    print(f"не открыт: {error}", flush=True); sys.exit(1)
print("взят", flush=True)
sys.stdout.close()
while os.getppid() == owner:
    time.sleep(0.2)
PY
take_train_lock() { # $1=каталог журнала → 0 взят, 1 отказ
    # BASHPID — до подстановки процесса: внутри неё он уже pid самого держателя.
    local fd answer owner=$BASHPID
    exec {fd}< <(exec python3 -I -c "$TRAIN_LOCK_HOLDER_PY" "$1/$TRAIN_LOCK_NAME" "$owner" 2>&1)
    TRAIN_LOCK_HOLDER=$!
    read -r -u "$fd" answer
    exec {fd}<&-
    [ "$answer" = взят ] && return 0
    TRAIN_LOCK_HOLDER=""
    case "$answer" in
        занято) say "ОТКАЗ: поезд уже идёт — замок $1/$TRAIN_LOCK_NAME занят другим ходом" ;;
        *) say "ОТКАЗ: замок поезда $1/$TRAIN_LOCK_NAME ${answer:-не взят}" ;;
    esac
    return 1
}
release_train_lock() {
    [ -n "$TRAIN_LOCK_HOLDER" ] || return 0
    kill "$TRAIN_LOCK_HOLDER" 2>/dev/null
    wait "$TRAIN_LOCK_HOLDER" 2>/dev/null
    TRAIN_LOCK_HOLDER=""
}

# Значения журнала поезда не охраняются, а идут в git и в bash -c ядра: годен только коммит проекта.
train_commit_ok() { # $1=значение → 0 — 40 hex и коммит проекта
    [[ "$1" =~ ^[0-9a-f]{40}$ ]] && git -C "$PROJECT_DIR" cat-file -e "$1^{commit}" 2>/dev/null
}

# Ворота судят срез в своём дереве: живое дерево проекта очередь двигает, пока они идут.
train_slice_tree() { # → TRAIN_TREE; 1 — отказ
    local where="$PROJECT_DIR/.claude/worktrees"
    if ! { mkdir -p "$where" && TRAIN_TREE=$(mktemp -d "$where/release-train-XXXXXX"); }; then
        TRAIN_TREE=""; say "ОТКАЗ: каталог дерева среза в $where не создан"; return 1
    fi
    git -C "$PROJECT_DIR" worktree add -q --detach "$TRAIN_TREE" "$TRAIN_SLICE_PROJECT" > /dev/null 2>&1 && return 0
    say "ОТКАЗ: дерево проекта на срезе ${TRAIN_SLICE_PROJECT:0:12} не создано"
    train_slice_tree_remove
    return 1
}
train_slice_tree_remove() {
    [ -n "$TRAIN_TREE" ] || return 0
    git -C "$PROJECT_DIR" worktree remove --force "$TRAIN_TREE" > /dev/null 2>&1 \
        || rmdir "$TRAIN_TREE" 2> /dev/null
    TRAIN_TREE=""
}
# Ход, убитый KILL, ловушку выхода не исполняет, и его дерево среза остаётся. Под замком поезда
# другого живого хода нет, поэтому каждое release-train-* — сирота: снять его и регистрации без каталога.
train_sweep_trees() {
    local tree
    for tree in "$PROJECT_DIR/.claude/worktrees"/release-train-*; do
        [ -e "$tree" ] || continue
        git -C "$PROJECT_DIR" worktree remove --force "$tree" > /dev/null 2>&1 || rmdir "$tree" 2> /dev/null
    done
    git -C "$PROJECT_DIR" worktree prune > /dev/null 2>&1
}

# Потолок из release-train.yaml, минуты → секунды.
train_ceiling_sec() { # $1=release-train.yaml $2=ключ [$3=вложенный ключ]
    python3 -I -c 'import sys, yaml
value = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
for key in sys.argv[2:]:
    value = value[key]
print(float(value) * 60)' "$@"
}

# Значение поля из последней строки журнала поезда, где поле_поиска = значение (и состояние, если названо).
# Строка — событие events.emit: поля хода в data.
train_journal_find() { # $1=журнал $2=поле $3=значение $4=поле ответа [$5=состояние]
    python3 -I - "$@" <<'PY'
import json, sys
путь, поле, значение, ответ = sys.argv[1:5]
состояние = sys.argv[5] if len(sys.argv) > 5 else None
найдено = ""
try:
    строки = open(путь, encoding="utf-8").read().splitlines()
except OSError:
    строки = []
for строка in строки:
    try:
        з = json.loads(строка)["data"]
    except (ValueError, KeyError, TypeError):
        continue
    if (isinstance(з, dict) and з.get(поле) == значение and з.get(ответ)
            and состояние in (None, з.get("состояние"))):
        найдено = str(з[ответ])
print(найдено)
PY
}

# Прошлый выпуск — последний тег публичной ветки; коммит проекта, из которого он собран, —
# по записи поезда. Выпуски до поезда пары не записали: тогда последний master проекта до
# времени тега (публикация держит замок очереди, слияний между воротами и тегом нет).
train_previous_release() { # $1=каталог журнала → TRAIN_PREV_*; 1 — прошлого выпуска нет
    local sborka="$PUBLIC_BUILD_DIR" data
    TRAIN_PREV_TAG=$(git -C "$sborka" describe --tags --abbrev=0 "$PUBLIC_REMOTE/$PUBLIC_BRANCH" 2>/dev/null) || return 1
    TRAIN_PREV_PROJECT=$(train_journal_find "$1/$TRAIN_JOURNAL" тег "$TRAIN_PREV_TAG" срез_проекта выпущен)
    TRAIN_PREV_LINK="запись"
    [ -n "$TRAIN_PREV_PROJECT" ] && return 0
    data=$(git -C "$sborka" log -1 --format=%cI "$TRAIN_PREV_TAG" 2>/dev/null) || return 1
    TRAIN_PREV_PROJECT=$(git -C "$PROJECT_DIR" rev-list -1 --before="$data" master 2>/dev/null)
    TRAIN_PREV_LINK="связь по времени"
    [ -n "$TRAIN_PREV_PROJECT" ]
}

# Срез пакета — довод (после кнопки) или HEAD; срез проекта — из записи хода, резавшего этот
# срез, иначе HEAD master. Влитое после среза едет следующим поездом.
train_cut_slice() { # $1=каталог журнала $2=sha пакета из довода (пусто — HEAD) → 0, 1 отказ
    local head after
    head=$(git -C "$PACKAGE_DIR" rev-parse --verify -q HEAD) || { say "ОТКАЗ: HEAD пакета $PACKAGE_DIR не прочитан"; return 1; }
    if ! TRAIN_SLICE_PACKAGE=$(git -C "$PACKAGE_DIR" rev-parse --verify -q "${2:-HEAD}^{commit}") \
       || ! git -C "$PACKAGE_DIR" merge-base --is-ancestor "$TRAIN_SLICE_PACKAGE" "$head"; then
        say "ОТКАЗ: среза пакета «$2» нет в истории пакета"; return 1
    fi
    [ -n "$2" ] && TRAIN_SLICE_PROJECT=$(train_journal_find "$1/$TRAIN_JOURNAL" срез_пакета "$TRAIN_SLICE_PACKAGE" срез_проекта)
    if [ -z "$TRAIN_SLICE_PROJECT" ]; then
        TRAIN_SLICE_PROJECT=$(git -C "$PROJECT_DIR" rev-parse --verify -q master) \
            || { say "ОТКАЗ: master проекта $PROJECT_DIR не прочитан"; return 1; }
    fi
    say "срез: проект $TRAIN_SLICE_PROJECT, пакет $TRAIN_SLICE_PACKAGE"
    after=$(git -C "$PACKAGE_DIR" rev-list --count "$TRAIN_SLICE_PACKAGE..$head")
    [ "$after" = 0 ] || say "после среза влито $after коммит(ов) в пакет — следующий поезд"
}

# Сборщик, номер выпуска и судья чистоты читают пакет как каталог с HEAD. Срез — не HEAD,
# поэтому им дан вид пакета на срезе: свой .git на объектах пакета (alternates) и с теми же
# удалёнными — их адреса судья ищет в сборке. Сам пакет не трогается.
train_package_view() { # $1=sha среза → PACKAGE_VIEW; 1 — отказ
    local view objects remote
    view=$(mktemp -d -t publish-train-package-XXXXXX) || { say "ОТКАЗ: каталог вида пакета не создан"; return 1; }
    CLEANUP_DIRS+=("$view")
    objects="$(git -C "$PACKAGE_DIR" rev-parse --path-format=absolute --git-common-dir)/objects"
    if ! { git init -q "$view" && printf '%s\n' "$objects" > "$view/.git/objects/info/alternates" \
           && git -C "$view" -c advice.detachedHead=false checkout -q --detach "$1"; }; then
        say "ОТКАЗ: вид пакета на срезе ${1:0:12} не собран"; return 1
    fi
    for remote in $(git -C "$PACKAGE_DIR" remote); do
        git -C "$view" remote add "$remote" "$(git -C "$PACKAGE_DIR" remote get-url "$remote")" || return 1
    done
    PACKAGE_VIEW="$view"
}

# Ядро — стоп-список release-train.yaml: любой красный — срез не едет.
train_core() { # $1=корень (release-train.yaml, scripts/) $2=журнал ворот → 0 зелёное, 1 красное, 2 не дошло
    local spisok imya komanda ceiling rc
    if ! ceiling=$(train_ceiling_sec "$1/$TRAIN_CONFIG" ядро_потолок_мин 2>&1); then
        TRAIN_CORE="не прочитано"; say "ОТКАЗ: потолок ядра не прочитан из $TRAIN_CONFIG: ${ceiling:0:200}"; return 1
    fi
    if ! spisok=$(python3 -I - "$1/$TRAIN_CONFIG" "$TRAIN_PREV_PROJECT" "$TRAIN_SLICE_PROJECT" 2>&1 <<'PY'
import sys, yaml
данные, прошлый, срез = sys.argv[1:4]
for пункт in (yaml.safe_load(open(данные, encoding="utf-8")) or {}).get("ядро") or []:
    команда = str(пункт["команда"]).replace("{прошлый}", прошлый).replace("{срез}", срез)
    print(f"{пункт['имя']}\t{команда}")
PY
    ) || [ -z "$spisok" ]; then
        TRAIN_CORE="не прочитано"; say "ОТКАЗ: ядро поезда не прочитано из $TRAIN_CONFIG: ${spisok:0:200}"; return 1
    fi
    while IFS=$'\t' read -r imya komanda; do
        (cd "$1" && timeout --kill-after=30 "$ceiling" bash -c "$komanda") >> "$2" 2>&1 < /dev/null; rc=$?
        case "$rc" in
            0) ;;
            124|137) TRAIN_CORE="не дошло: $imya"; return 2 ;;
            *) TRAIN_CORE="красное: $imya"; say "ОТКАЗ: красное ядро — «$imya». Хвост:"; tail -5 "$2"; return 1 ;;
        esac
    done <<< "$spisok"
    TRAIN_CORE="зелёное"
}

# Выбор — проверки gates.sh, задетые диапазоном (select-checks.py --поезд). Дольше потолка —
# «не дошли»: молчание ворот не читается успехом. Код ворот не судья: карантин гасит его для
# старой поломки в дереве, а поезд везёт срез к копиям — красная в диапазоне стоит и в карантине.
train_selection() { # $1=каталог скриптов $2=журнал ворот → 0 зелёный, 1 красный, 2 не дошли
    local imena potolok rc nachalo itog krasnye karantin imya held=() fresh=()
    TRAIN_RED=0
    imena=$(python3 -I "$1/select-checks.py" --корень "$(dirname "$1")" --поезд --имена \
                    --диапазон "$TRAIN_PREV_PROJECT..$TRAIN_SLICE_PROJECT" 2>>"$2") || return 2
    TRAIN_SELECTED=$(printf '%s' "$imena" | tr '|' '\n' | grep -c .)
    [ -n "$imena" ] || return 0
    potolok=$(train_ceiling_sec "$(dirname "$1")/$TRAIN_CONFIG" выбор потолок_мин 2>>"$2") || return 2
    nachalo=$(( $(wc -l < "$2") + 1 ))
    # --всё: тяжёлую пробу, названную во входы_проб, выбор берёт, а без флага gates.sh её не гонит.
    timeout --kill-after=30 "$potolok" bash "$1/gates.sh" --только "$imena" --всё >> "$2" 2>&1 < /dev/null; rc=$?
    case "$rc" in 124|137) return 2 ;; esac
    itog=$(tail -n +"$nachalo" "$2")
    TRAIN_RED=$(printf '%s\n' "$itog" | sed -n 's/^=== итог: зелёных [0-9]*, красных \([0-9]*\).*/\1/p' | tail -1)
    krasnye=$(printf '%s\n' "$itog" | sed -n 's/^  ПЛОХО · \(.*\) (код [0-9]*).*/\1/p' | sort -u)
    karantin=$(printf '%s\n' "$itog" | sed -n 's/^  карантин · \(.*\) — красный, .*/\1/p' | sort -u)
    # Красная под годной карточкой карантина срез не держит (просроченная карточка строки «карантин ·»
    # не получает — красная считается новой), но ходит именем в замок 1, запись хода и просьбу кнопки.
    while IFS= read -r imya; do
        [ -n "$imya" ] || continue
        if grep -qxF -- "$imya" <<< "$karantin"; then held+=("$imya"); else fresh+=("$imya"); fi
    done <<< "$krasnye"
    TRAIN_QUARANTINED=${#held[@]}
    TRAIN_QUARANTINED_NAMES=$(printf '%s\n' "${held[@]}" | paste -sd, - | sed 's/,/, /g')
    TRAIN_RED_NAMES=$(printf '%s\n' "${fresh[@]}" | paste -sd, - | sed 's/,/, /g')
    # Счёт итога — до вычета: красная без разобранного имени остаётся новой и стоит.
    [[ "$TRAIN_RED" =~ ^[0-9]+$ ]] && TRAIN_RED=$(( TRAIN_RED - TRAIN_QUARANTINED ))
    [ "$rc" = 0 ] && [ "$TRAIN_RED" = 0 ] && return 0
    return 1
}

# Ключи двери согласия для поезда: срез и — чтобы владелец видел это на кнопке — карантинные красные выбора.
train_guard_args() {
    TRAIN_GUARD_ARGS=(--срез "$TRAIN_SLICE_PACKAGE")
    [ "$TRAIN_QUARANTINED" -gt 0 ] && TRAIN_GUARD_ARGS+=(--в-карантине "$TRAIN_QUARANTINED_NAMES")
}

train_gates() { # $1=каталог журнала → 0 пустить, 1 стоп
    local rc
    if ! train_commit_ok "$TRAIN_SLICE_PROJECT" || ! train_commit_ok "$TRAIN_PREV_PROJECT"; then
        TRAIN_GATES="отказ"
        say "ОТКАЗ: срез «${TRAIN_SLICE_PROJECT:0:48}» или прошлый выпуск «${TRAIN_PREV_PROJECT:0:48}» — не коммит проекта; журнал поезда $1/$TRAIN_JOURNAL не годен"
        return 1
    fi
    train_slice_tree || return 1
    TRAIN_GATES_LOG="$1/release-train-gates-$(date +%Y%m%d-%H%M%S)-$BASHPID.log"
    train_judge_slice "$TRAIN_TREE" "$TRAIN_GATES_LOG"; rc=$?
    train_slice_tree_remove
    return "$rc"
}

train_judge_slice() { # $1=дерево проекта на срезе $2=журнал ворот → 0 пустить, 1 стоп
    local t0 rc quarantined=""
    # Проверки берут проект по паспорту (PROJECT_DIR), а пакет — через katalog_paketa()
    # (HARNESS_STARTER): без этих двух они судили бы живые проект и пакет, которые очередь
    # двигает во время ворот, а не срез.
    local -x PROJECT_DIR="$1" HARNESS_STARTER="$PACKAGE_VIEW"
    # Карточки агентов несут путь проекта: собранные под живое дерево, они не равны сборке под
    # PROJECT_DIR среза, и сторож запуска помощников отказывает каждой пробе, что его зовёт.
    (cd "$1" && python3 -I scripts/self-prompt.py --карточки --в "$1/.claude/agents") >> "$2" 2>&1 \
        || say "карточки агентов в дереве среза не пересобраны — пробы сторожа запуска покраснеют; журнал: $2"
    t0=$(date +%s)
    train_core "$1" "$2"; rc=$?
    TRAIN_SEC_CORE=$(( $(date +%s) - t0 ))
    case "$rc" in
        1) TRAIN_STATE="красное ядро"; TRAIN_GATES="$TRAIN_STATE"; return 1 ;;
        2) TRAIN_STATE="не дошли"; TRAIN_GATES="$TRAIN_STATE"
           say "ОТКАЗ: ядро поезда не дошло за потолок — «${TRAIN_CORE#не дошло: }»; журнал: $2"; return 1 ;;
    esac
    t0=$(date +%s)
    train_selection "$1/scripts" "$2"; rc=$?
    TRAIN_SEC_SELECTION=$(( $(date +%s) - t0 ))
    [ "$TRAIN_QUARANTINED" -gt 0 ] && quarantined=", в карантине $TRAIN_QUARANTINED: $TRAIN_QUARANTINED_NAMES"
    say "замок 1 · ворота поезда: диапазон ${TRAIN_PREV_PROJECT:0:8}..${TRAIN_SLICE_PROJECT:0:8}, выбрано ${TRAIN_SELECTED:-?}, красных ${TRAIN_RED:-?}$quarantined, ядро $TRAIN_CORE, секунд $((TRAIN_SEC_CORE + TRAIN_SEC_SELECTION))"
    case "$rc" in
        0) TRAIN_GATES="зелёные"; return 0 ;;
        2) TRAIN_STATE="не дошли"
           say "ОТКАЗ: ворота поезда не дошли (предел выбора или сбой выбора) — срез не судим; журнал: $2" ;;
        *) TRAIN_STATE="красный выбор"
           say "ОТКАЗ: красные в выборе: ${TRAIN_RED_NAMES:-код ворот без имён, см. $2} — срез не едет" ;;
    esac
    TRAIN_GATES="$TRAIN_STATE"
    return 1
}

# Посадка поезда: срез, прошлый выпуск, вид пакета на срезе и ворота по разнице.
# Замок очереди — только на чтение пары HEAD проекта/пакета: очередь коммитит их парой, и срез
# между ними разорвал бы пару. Дальше он не нужен: ворота идут в своём дереве проекта на срезе,
# сборка и номер читают вид пакета на срезе, предмет согласия и сверка перед пушем называют срез,
# а слияния лишь дописывают историю обоих. Держи поезд замок на воротах — очередь ждала бы их и
# падала кодом 8. Зелёные ворота записываются сразу: дверь согласия этого же хода ищет их в журнале.
train_board() { # $1=каталог журнала $2=sha пакета из довода $3=предел ожидания замка, с → 0 ехать, 1 стоп
    local rc
    TRAIN_STARTED=$(date +%s)
    take_train_lock "$1" || return 1
    train_sweep_trees
    take_merge_queue_lock "$1" "$3" || return 1
    train_cut_slice "$1" "$2"; rc=$?
    release_merge_queue_lock
    [ "$rc" = 0 ] || return 1
    say "замок очереди слияния отпущен: срез снят, слияния идут, пока судятся ворота"
    if ! train_previous_release "$1"; then
        say "ОТКАЗ: прошлого выпуска нет — поезд не идёт; первая публикация — bash scripts/publish.sh --первая"
        return 1
    fi
    say "прошлый выпуск: $TRAIN_PREV_TAG, проект $TRAIN_PREV_PROJECT ($TRAIN_PREV_LINK)"
    PAKET_SHA="$TRAIN_SLICE_PACKAGE"
    train_package_view "$PAKET_SHA" || return 1
    train_gates "$1" || return 1
    TRAIN_STATE="ворота зелёные"
    train_record "$1" ""
    TRAIN_STATE=""
}

# Одна строка на ход поезда; зовётся из обработчика выхода, поэтому пишет и оборванный ход.
# Через общую дверь событий: журнал судим реестром, а писатель не дописывает его в обход.
train_record() { # $1=каталог журнала $2=код хода
    local seichas pola ishod=refused; seichas=$(date +%s)
    case "$2" in ""|0) ishod=ok ;; esac
    pola=$(python3 -I - "${TRAIN_STATE:-отказ}" "$2" "$TRAIN_SLICE_PROJECT" \
        "$TRAIN_SLICE_PACKAGE" "$TRAIN_PREV_TAG" "$TRAIN_PREV_PROJECT" "$TRAIN_PREV_LINK" "$RELEASE_TAG" \
        "$TRAIN_SELECTED" "$TRAIN_RED" "$TRAIN_CORE" "$TRAIN_SEC_CORE" "$TRAIN_SEC_SELECTION" \
        "$(( TRAIN_STARTED > 0 ? seichas - TRAIN_STARTED : 0 ))" "$TRAIN_GATES" "$TRAIN_RED_NAMES" "$TRAIN_QUARANTINED" "$TRAIN_GATES_LOG" "$TRAIN_QUARANTINED_NAMES" \
        <<'PY'
import json, sys
п = sys.argv[1:]
число = lambda x: int(x) if x.isdigit() else None  # noqa: E731
print(json.dumps({"состояние": п[0], "код": число(п[1]), "срез_проекта": п[2], "срез_пакета": п[3],
                  "прошлый_тег": п[4], "прошлый_sha_проекта": п[5], "связь": п[6], "тег": п[7],
                  "выбрано": число(п[8]), "красных": число(п[9]), "ядро": п[10],
                  "секунды": {"ядро": число(п[11]), "выбор": число(п[12]), "всего": число(п[13])},
                  "ворота": п[14], "красные": п[15], "в_карантине": число(п[16]),
                  "журнал_ворот": п[17], "в_карантине_имена": п[18]}, ensure_ascii=False))
PY
    ) || { say "ВНИМАНИЕ: строка хода поезда не собрана"; return 0; }
    HARNESS_LOG_DIR="$1" harness_event release-train scripts/publish.sh train.step "$ishod" --json "$pola"
}

# ── Замок 6в: заметки выпуска простым языком и тот же номер, что у тега ─────
# Слово владельца 24.10.2026: «в релизы харнеса писать человеческим языком что было
# исправлено добавлено». Судится и до кнопки (обе ветки зовут эту функцию): правка
# заметок после «Да» сдвигает пакет и сжигает кнопку.
# Номер считает version.py тем же счётом, что и тег; запись его только называет.
next_release_number() { # $1=сборка $2=пакет → X.Y.Z; код 1 — причина вместо номера
    local otvet
    otvet=$(python3 -I "$SCRIPTS_REAL/version.py" --следующая --пакет "$2" --сборка "$1" \
                    --удалённый "$PUBLIC_REMOTE" 2>&1) || { printf '%s' "$otvet"; return 1; }
    printf '%s' "$otvet" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["следующая"])' 2>/dev/null \
        || { printf 'ответ version.py не разобран: %s' "${otvet:0:120}"; return 1; }
}

# Пустой индекс сборки — повтор упавшей отправки или «публиковать нечего»: запись
# тогда уже вышла по праву, судить нечего.
judge_release_notes() { # $1=сборка (индекс собран) $2=пакет → 0 годится, 1 отказ
    local sborka="$1" vyvod nomer nazvan
    git -C "$sborka" diff --cached --quiet && { say "замок 6в · в сборке нового нет — заметки не судятся"; return 0; }
    nomer=$(next_release_number "$sborka" "$2") || { say "ОТКАЗ: замок 6в · номер выпуска не посчитан — $nomer"; return 1; }
    say "номер выпуска: v$nomer"
    if ! vyvod=$(python3 -I "$SCRIPTS_REAL/check-release-notes.py" "$sborka" 2>&1); then
        say "ОТКАЗ: релиз без записи простым языком — публикация отменена"
        printf '%s\n' "$vyvod" | sed 's/^/  /'
        return 1
    fi
    nazvan=$(grep -m1 '^## ' "$sborka/ЧТО-НОВОГО.md" | grep -oE 'Выпуск [0-9]+(\.[0-9]+)+' | head -1)
    nazvan="${nazvan#Выпуск }"
    if [ "$nazvan" != "$nomer" ]; then
        say "ОТКАЗ: замок 6в · верхняя запись ЧТО-НОВОГО называет выпуск ${nazvan:-«без номера»}, а будет поставлен v$nomer — нужно «Выпуск $nomer»"
        return 1
    fi
    say "замок 6в · заметки выпуска простым языком есть, номер v$nomer совпадает"
}

# ── Задачи, попадающие на витрину, обязаны быть ПРОВЕРЕНЫ владельцем ────────
# Трейлер «Dev-Map: <id>» стоит в коммитах ПРОЕКТА, а публикуется ПАКЕТ: они
# коммитятся парой, и связать их можно только временем. Поэтому спрашиваем:
# какие задачи появились в проекте ПОСЛЕ момента, на котором стоит публичная
# ветка. Читать историю пакета бесполезно — трейлеров там ноль, и замок
# пропускал бы всё и всегда (замер 11.09.2026, поймано до включения).
#
# Чтение трейлеров — отдельной функцией, и вот почему. Самотест судил её через
# ЖИВУЮ историю этой машины («есть ли за сутки хоть один трейлер») и 21.09.2026
# покраснел на исправном коде: с 14.09 коммитов просто не было. Проба обязана
# СОЗДАВАТЬ своё условие, а не совпадать с состоянием машины
# ([[a-check-creates-its-own-condition]]) — с отдельной функцией она строит свой
# репозиторий со своим трейлером и судит то же, что зовёт бой.
#
# Отказ git и пустая история — РАЗНЫЕ ответы (код 2 против пустого вывода):
# «не прочитали» ≠ «нечего проверять», и замок, прочитавший отказ как чистоту,
# выпустил бы наружу непроверенное ([[command-failure-read-as-empty]]).
ids_iz_istorii() { # $1=момент ISO (пусто — вся история) [$2=вершина истории, без неё HEAD] → id задач из трейлеров
    local since="$1" vyvod
    local ogr=()
    [ -n "$since" ] && ogr=(--since="$since")
    vyvod=$(git -C "$PROJECT_DIR" log "${ogr[@]}" --format=%B "${2:-HEAD}" -- 2>/dev/null) || return 2
    printf '%s' "$vyvod" | sed -n 's/^Dev-Map: *//p' | sort -u
}

непроверенные_с() { # $1=момент ISO → печатает id задач, не готовых к витрине
    local ids rc
    # Поезд везёт срез проекта: задача, влитая после среза, едет следующим поездом и здесь не судится.
    ids=$(ids_iz_istorii "$1" "${TRAIN_SLICE_PROJECT:-}"); rc=$?
    if [ "$rc" = 2 ]; then
        printf 'ОТКАЗ-ИСТОРИИ\n'   # читатель обязан остановиться, а не считать это чистотой
        return 0
    fi
    [ -n "$ids" ] || return 0
    # Разбор — отдельным файлом: его зовут двое, замок в бою и проба в
    # самотесте. Копия внутри оболочки досталась бы только замку.
    DEVMAP="$PROJECT_DIR/dev-map.yaml" \
    DEVMAP_ARCHIVE="$PROJECT_DIR/docs/dev-map-archive.yaml" \
    IDS="$ids" python3 -I "$SCRIPTS_REAL/vitrina_status.py"
}

# ── Замки 4-6 и публикация собранного дерева ────────────────────────────────
# Наружу уезжает не пакет, а СБОРКА: инструмент без нашей мастерской. Порядок
# замков — единственное, что здесь своё; сборка, поиск секретов и поиск следов
# зовутся готовыми.
# [--сухо] — прогнать замки 4-6 и выйти, ничего не отправляя;
# [--заметки] — собрать и судить только замок 6в (до кнопки: замки 5-6г тяжелее)
опубликовать_сборку() {
    local sborka="$PUBLIC_BUILD_DIR" paket="${PACKAGE_VIEW:-$PACKAGE_DIR}" itog hesh suho=0 rezhim="${1:-}"
    [ "$rezhim" = "--сухо" ] && suho=1
    # Собирается ровно тот коммит пакета, на который дано согласие (замок 2): HEAD,
    # прочитанный здесь заново, мог уйти вперёд за время замка 1. У поезда paket — вид на
    # срезе: его HEAD и есть срез, а сдвиг самого пакета после среза не мешает.
    local paket_sha="${PAKET_SHA:-$(git -C "$paket" rev-parse HEAD 2>/dev/null)}"
    # Прошлый выпуск — манифест в каталоге сборки: снять ДО сборки, она его заменит.
    local proshlyj=""
    [ -f "$sborka/harness/self/MANIFEST.sha256" ] && proshlyj="$(LC_ALL=C sort "$sborka/harness/self/MANIFEST.sha256")"

    # Замок 4: собрать из УЧТЁННОГО состояния пакета.
    # Паспорт передаётся ЯВНО: без довода сборщик и гейт читают боевой
    # /etc/harness/install.conf, и самотест судил машину, а не код — «здоровая»
    # проба зеленела лишь потому, что настоящие имена не встретились в
    # подставном дереве (ревью кода 12.09.2026, №10).
    local pasport="${PUBLIC_PASPORT:-$KONF_DVERI_KOREN/install.conf}"
    itog=$(python3 -I "$SCRIPTS_REAL/build-public.py" \
                   --пакет "$paket" --куда "$sborka" --паспорт "$pasport" \
                   --коммит "$paket_sha" 2>&1) || {
        say "ОТКАЗ: сборка не прошла — $itog"; return 1; }
    hesh=$(printf '%s' "$itog" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["пакет"])' 2>/dev/null)
    # Пустой хэш означает, что вывод сборщика не разобран: дальше он ушёл бы в
    # сообщение коммита строкой «пакет » — и публичная история перестала бы
    # отвечать, из какого состояния пакета собрана (ревью кода 12.09.2026, №12).
    [ -n "$hesh" ] || { say "ОТКАЗ: не разобран вывод сборщика, хэш пакета пуст"; return 1; }
    say "замок 4 · собрано: $itog"
    # Новый манифест написал сборщик по собранному дереву.
    сказать_смену_слоя1 "$proshlyj" "$(LC_ALL=C sort "$sborka/harness/self/MANIFEST.sha256" 2>/dev/null)"

    # Своя история у сборки: публичная ветка ведётся здесь, а не в пакете.
    [ -d "$sborka/.git" ] || git -C "$sborka" init -q -b "$PUBLIC_BRANCH"
    # Удалённый настраивается КАЖДЫЙ раз, а не только при создании .git:
    # каталог сборки мог появиться раньше (проверка секретов, ручной осмотр),
    # и тогда публичной ветки в нём нет — замок 3 судил бы по пустоте.
    # Адрес берётся у ПАКЕТА и выверяется КАЖДЫЙ раз. Прежняя редакция
    # настраивала его лишь при отсутствии: сменив адрес публичного репозитория,
    # владелец продолжал бы отправлять по старому — молча. А пустая подстановка
    # (`remote add jig ""`) завершается успешно и навсегда закрывает починку:
    # `get-url` после неё отвечает 0 (ревью кода 12.09.2026, №6).
    local adres tekushchij
    adres=$(git -C "$paket" remote get-url "$PUBLIC_REMOTE" 2>/dev/null || true)
    [ -n "$adres" ] || { say "ОТКАЗ: у пакета нет удалённого «$PUBLIC_REMOTE» — публиковать некуда"; return 1; }
    tekushchij=$(git -C "$sborka" remote get-url "$PUBLIC_REMOTE" 2>/dev/null || true)
    if [ -z "$tekushchij" ]; then
        git -C "$sborka" remote add "$PUBLIC_REMOTE" "$adres"
    elif [ "$tekushchij" != "$adres" ]; then
        say "адрес публичного изменился: $tekushchij → $adres"
        git -C "$sborka" remote set-url "$PUBLIC_REMOTE" "$adres"
    fi
    git -C "$sborka" fetch --quiet "$PUBLIC_REMOTE" 2>/dev/null || true
    git -C "$sborka" add -A
    [ "$rezhim" = "--заметки" ] && { judge_release_notes "$sborka" "$paket"; return; }

    # Замок 5 (И-3): у нового репозитория своих хуков нет, и сторож секретов
    # его не сторожит — зовём явно, по временному индексу.
    local vyvod
    if ! vyvod=$(bash "$SCRIPTS_REAL/check-secrets.sh" "$sborka" 2>&1); then
        say "ОТКАЗ: в сборке похожее на секрет — публикация отменена"
        printf '%s\n' "$vyvod" | sed 's/^/  /'
        return 1
    fi
    say "замок 5 · секретов в сборке нет"

    # Прошлые секции CHANGELOG опубликованы до замка имён копий сети: обезличить их до
    # суда тем же кодом, что и version.py --оформить, иначе замок 6 отказывает по старой истории.
    if [ -f "$sborka/CHANGELOG.md" ]; then
        python3 -I - "$SCRIPTS_REAL" "$sborka/CHANGELOG.md" <<'PY' || { say "ОТКАЗ: прошлый CHANGELOG сборки не обезличен"; return 1; }
import sys
sys.path.insert(0, sys.argv[1])
import version
журнал = open(sys.argv[2], encoding="utf-8").read()
журнал = version._судья_чистоты().обезличить_имена_копий(журнал, version._паспорт())
open(sys.argv[2], "w", encoding="utf-8").write(журнал)
PY
    fi
    # Замок 6: следы установки. Образцы берутся не из данных сборщика.
    if ! vyvod=$(python3 -I "$SCRIPTS_REAL/check-public-clean.py" "$sborka" \
                         --пакет "$paket" --проект "$PROJECT_DIR" \
                         --паспорт "$pasport" 2>&1); then
        say "ОТКАЗ: в сборке следы этой установки — публикация отменена"
        printf '%s\n' "$vyvod" | sed 's/^/  /'
        return 1
    fi
    say "замок 6 · следов установки в сборке нет"

    judge_release_notes "$sborka" "$paket" || return 1

    # Замок 6г: типовая чистая копия — прошлый выпуск обновляется до этого сборкой
    # копии (не наш пользователь, не наш tmux, без HARNESS_DEV_GATES), самопроверка без
    # новых красных. Копия сети 14.11.2026 откатывала 0.12.1 на двух красных, которых
    # наша машина не видела. Первой публикации сравнивать не с чем.
    # TYPICAL_COPY_CHECK — дверь самотеста: подставной судья вместо стенда на гигабайт; загрузчик
    # двери снял его из окружения, назвать его может только присваивание после загрузки.
    if git -C "$sborka" rev-parse --verify --quiet "$PUBLIC_REMOTE/$PUBLIC_BRANCH^{commit}" >/dev/null; then
        local proshloe
        proshloe=$(mktemp -d -t proba-typical-release-XXXXXX)
        git -C "$sborka" archive "$PUBLIC_REMOTE/$PUBLIC_BRANCH" | tar -x -C "$proshloe"
        if ! vyvod=$(python3 -I "${TYPICAL_COPY_CHECK:-$SCRIPTS_REAL/check-typical-copy.py}" --старое "$proshloe" --новое "$sborka" 2>&1); then
            rm -rf "$proshloe"
            say "ОТКАЗ: типовая чистая копия не обновилась без новых красных — публикация отменена"
            printf '%s\n' "$vyvod" | sed 's/^/  /'
            return 1
        fi
        rm -rf "$proshloe"
        say "замок 6г · ${vyvod##*$'\n'}"
    else
        say "замок 6г · прошлого выпуска нет — типовой копии обновляться не с чего"
    fi

    # Сухой прогон доходит ровно досюда: замки 4-6 ПРОГНАНЫ, а ничего не
    # закоммичено и не отправлено. Прежде «--проверка» печатала «публиковать
    # можно», не выполнив ни одного из них: владелец видел зелёное, тратил
    # согласие (оно протухает за час) — и настоящий прогон падал на замке 6.
    if [ "$suho" = 1 ]; then
        say "сухой прогон: замки 4, 5 и 6 пройдены — публиковать можно"
        return 0
    fi

    # Состояние публичной ветки спрашивается у САМОГО удалённого (ls-remote),
    # а не у локальной копии ссылки: после упавшего push дерево не менялось,
    # индекс тих, а remote-tracking помнит прежнее — и прежняя редакция
    # отвечала «публиковать нечего» с кодом 0, хотя коммит так и не уехал
    # (ревью кода 12.09.2026, И-4: это ложь о состоянии публичной ветки).
    local tam
    tam=$(git -C "$sborka" ls-remote "$PUBLIC_REMOTE" \
          "refs/heads/$PUBLIC_BRANCH" 2>/dev/null | cut -f1)
    if git -C "$sborka" diff --cached --quiet \
       && [ -n "$tam" ] \
       && [ "$(git -C "$sborka" rev-parse HEAD 2>/dev/null)" = "$tam" ]; then
        say "публиковать нечего: опубликованное совпадает со сборкой"
        return 0
    fi
    # Нечего коммитить, но HEAD впереди удалённой — значит прошлый push не
    # прошёл: коммит не делаем, а отправку повторяем.
    if git -C "$sborka" diff --cached --quiet; then
        say "новых изменений нет, но прошлая отправка не прошла — повторяю отправку"
    else
        # Номер версии, файл VERSION и секция CHANGELOG считаются и пишутся ДО
        # коммита: они едут ТЕМ ЖЕ коммитом, что и сборка. Разъедься они —
        # человек прочитал бы заметки одной версии рядом с кодом другой.
        # Первая строка сообщения НЕ меняется: её разбирает update.py
        # (`бытие_из_своего_пакета`), и новый формат погасил бы извещение об
        # обновлении у всех, кто уже поставил харнес. Версия едет телом и тегом.
        local otvet_versii nomer_versii
        # Имя удалённого передаётся ЯВНО: прошлая версия считается по тегам,
        # которые реально опубликованы. По локальным тегам счёт после упавшей
        # отправки тега пропускал бы номер, оставляя вышедший релиз без метки
        # навсегда (ревью кода 12.09.2026, R-2).
        otvet_versii=$(python3 -I "$SCRIPTS_REAL/version.py" --оформить \
                       --пакет "$paket" --сборка "$sborka" \
                       --проект "$PROJECT_DIR" \
                       --удалённый "$PUBLIC_REMOTE" 2>&1) || {
            say "ОТКАЗ: версия релиза не посчитана — $otvet_versii"; return 1; }
        nomer_versii=$(printf '%s' "$otvet_versii" \
            | python3 -I -c 'import json,sys; d=json.load(sys.stdin); print(d["следующая"]); sys.stderr.write(d["почему"])' 2>/dev/null)
        [ -n "$nomer_versii" ] || {
            say "ОТКАЗ: ответ version.py не разобран: ${otvet_versii:0:120}"; return 1; }
        say "версия релиза: v$nomer_versii"
        # Заметки релиза собраны машиной из ЗАГОЛОВКОВ КОММИТОВ ПАКЕТА, а их
        # пишет смена — и назвать продукт в заголовке может так же легко, как в
        # коде. CHANGELOG появляется ПОСЛЕ замка 6, поэтому судья чистоты
        # зовётся второй раз: уже по дереву с заметками (замер 13.10.2026 — в
        # заметки попадало 40 заголовков из 532).
        if ! vyvod=$(python3 -I "$SCRIPTS_REAL/check-public-clean.py" "$sborka" \
                             --пакет "$paket" --проект "$PROJECT_DIR" \
                             --паспорт "$pasport" 2>&1); then
            say "ОТКАЗ: следы установки в заметках релиза — публикация отменена"
            printf '%s\n' "$vyvod" | sed 's/^/  /'
            return 1
        fi
        say "замок 6б · в заметках релиза следов установки нет"
        # Индекс собран ДО замков — VERSION и CHANGELOG появились после него.
        git -C "$sborka" add -A
        git -C "$sborka" -c user.name="$PUBLIC_COMMIT_NAME" \
            -c user.email="$PUBLIC_COMMIT_EMAIL" \
            commit -q -m "версия от $(date +%F), пакет $hesh" \
                      -m "версия v$nomer_versii" || {
            say "ОТКАЗ: коммит сборки не прошёл"; return 1; }
    fi

    # Между замком 2 и пушем прошли минуты (замки 1, 3-6, версия): пакет, адрес,
    # вершина публичной ветки и коммиты сборки обязаны быть теми, что одобрил владелец.
    if [ "$(git -C "$paket" rev-parse HEAD 2>/dev/null)" != "$paket_sha" ]; then
        say "ОТКАЗ: пакет сдвинулся после согласия — не отправлено"; return 1
    fi
    if [ -n "${PREDMET_ZAMKA:-}" ] && ! vyvod=$(python3 -I "$SCRIPTS_REAL/lib/door_subjects.py" \
            сверить-публикацию "$PREDMET_ZAMKA" "$PACKAGE_DIR" "$sborka" "$PUBLIC_REMOTE" "$PUBLIC_BRANCH" \
            ${PACKAGE_VIEW:+"$paket_sha" "${LOG_DIR:-}/$TRAIN_JOURNAL"} 2>&1); then
        say "ОТКАЗ: предмет публикации изменился после согласия — не отправлено: $vyvod"; return 1
    fi

    # Только вперёд. Публичную историю переписывает владелец руками и по своему
    # слову: у тех, кто уже склонировал, обновление держится на ff-only, и
    # force сломал бы его навсегда.
    if git -C "$sborka" rev-parse --verify -q "$PUBLIC_REMOTE/$PUBLIC_BRANCH" >/dev/null; then
        if ! git -C "$sborka" merge-base --is-ancestor \
               "$PUBLIC_REMOTE/$PUBLIC_BRANCH" HEAD; then
            say "ОТКАЗ: расхождение с публичной веткой — в неё писали помимо сборки."
            say "Разбирает человек: force здесь запрещён, он ломает обновление у всех, кто склонировал."
            return 1
        fi
    fi
    if ! git -C "$sborka" push --quiet "$PUBLIC_REMOTE" "HEAD:$PUBLIC_BRANCH" 2>&1; then
        say "ОТКАЗ: push в $PUBLIC_BRANCH не прошёл — разберись руками"
        return 1
    fi
    # Тег ставится ПОСЛЕ успешной отправки ветки, и только так: тег на
    # неотправленный коммит — обещание версии, которой нет ни у кого.
    # Номер берётся из VERSION уже ОПУБЛИКОВАННОГО дерева, а не из ответа
    # счётчика: на пути «коммитить нечего, повторяю отправку» счётчик не
    # звался вовсе, а тег прошлой попытки всё равно надо дожать.
    local nomer teg
    nomer=$(tr -d '[:space:]' < "$sborka/VERSION" 2>/dev/null || true)
    if [ -z "$nomer" ]; then
        say "ВНИМАНИЕ: в сборке нет VERSION — релиз опубликован без тега"
    else
        teg="v$nomer"
        if git -C "$sborka" rev-parse -q --verify "refs/tags/$teg" >/dev/null; then
            say "тег $teg уже стоит"
        elif ! git -C "$sborka" -c user.name="$PUBLIC_COMMIT_NAME" \
                  -c user.email="$PUBLIC_COMMIT_EMAIL" \
                  tag -a "$teg" -m "релиз $teg, пакет $hesh"; then
            say "ОТКАЗ: тег $teg не поставлен — ветка опубликована, метки версии нет"
            return 1
        fi
        if ! git -C "$sborka" push --quiet "$PUBLIC_REMOTE" "refs/tags/$teg" 2>&1; then
            say "ОТКАЗ: тег $teg не отправлен — ветка опубликована, у людей нет метки версии"
            return 1
        fi
        say "релиз помечен: $teg"
        RELEASE_TAG="$teg"
    fi
    say "опубликовано: $PUBLIC_BRANCH ← сборка из пакета $hesh"
    return 0
}

# Прогоны до боевой сборки (заметки до кнопки, сухой) собирают в копии каталога сборки:
# общий PUBLIC_BUILD_DIR хранит манифест прошлого выпуска, по которому просьба кнопки
# (ask-consent.py) и боевая сборка называют смену личности/стандартов. Копия несёт и
# .git сборки: номер выпуска и прошлый выпуск для замка 6г считаются по нему.
build_in_scratch() { # $1=режим опубликовать_сборку → её код
    local kopiya rc
    kopiya=$(mktemp -d -t publish-scratch-build-XXXXXX) \
        || { say "ОТКАЗ: временный каталог сборки не создан"; return 1; }
    CLEANUP_DIRS+=("$kopiya")
    if [ -d "$PUBLIC_BUILD_DIR" ] && ! cp -a "$PUBLIC_BUILD_DIR/." "$kopiya/"; then
        say "ОТКАЗ: каталог сборки не скопирован во временный: $kopiya"; return 1
    fi
    PUBLIC_BUILD_DIR="$kopiya" опубликовать_сборку "$1"; rc=$?
    rm -rf "$kopiya"
    return "$rc"
}

# ── Дверь переписи публичной истории ───────────────────────────────────────
# Обычный путь — только вперёд (ff-only), и это держит обновление у тех, кто
# уже склонировал. Перепись истории — решение владельца, и ей нужна законная
# дверь, а не ручной force: сторож команд пускает к публичному удалённому
# только этот скрипт. Дверь отправляет ГОТОВУЮ копию истории одной атомарной
# отправкой, и каждая ссылка отправляется с арендой на тот sha, что дверь
# прочла: чужой коммит, пришедший между чтением и отправкой, отвергнет всё.
# Срок кнопки на перепись — owner_word.СРОК_ВИДА_МИН (60 мин).

# Ссылки репозитория, ветки и теги: «sha ref», отсортированно. Теги — sha
# самого объекта тега, не пересечённый: аренда сверяется именно с ним.
ssylki_kopii() { # $1=репозиторий
    git -C "$1" for-each-ref --format='%(objectname) %(refname)' refs/heads refs/tags | sort
}

# Ссылки публичного удалённого по адресу; HEAD и «^{}» отбрасываются.
ssylki_udalyonnogo() { # $1=адрес → «sha ref»; код 1, если спросить не удалось
    local otvet
    otvet=$(git ls-remote "$1" 'refs/heads/*' 'refs/tags/*' 2>/dev/null) || return 1
    printf '%s\n' "$otvet" | awk 'NF == 2 && $2 !~ /\^\{\}$/ {print $1, $2}' | sort
}

# Бэкап годен, если verify проходит и из него реально разворачивается
# репозиторий: verify судит заголовок и обрезанный пакет не замечает.
proverit_bekap() { # $1=файл $2=рабочий каталог
    [ -f "$1" ] || { say "ОТКАЗ: бэкап (bundle) не найден: $1"; return 1; }
    git init -q "$2/verify" && git -C "$2/verify" bundle verify "$1" >/dev/null 2>&1 \
        || { say "ОТКАЗ: бэкап (bundle) не проходит git bundle verify: $1"; return 1; }
    git clone -q --bare "$1" "$2/clone" >/dev/null 2>&1 && git -C "$2/clone" fsck --no-dangling >/dev/null 2>&1 \
        || { say "ОТКАЗ: бэкап (bundle) не разворачивается в репозиторий — пакет внутри повреждён: $1"; return 1; }
}

# Прежняя публичная история обязана лежать в бэкапе целиком: откат из него —
# единственный путь назад.
bekap_pokryvaet() { # $1=файл $2=«sha ref» удалённого
    local golovy stroka
    golovy=$(git bundle list-heads "$1" 2>/dev/null)
    while IFS= read -r stroka; do
        grep -qxF "$stroka" <<< "$golovy" || {
            say "ОТКАЗ: в бэкапе (bundle) нет ссылки «${stroka#* }» с её нынешним sha — бэкап устарел"
            return 1; }
    done <<< "$2"
}

# Ссылки удалённого и копии должны совпадать ПО ИМЕНАМ: лишнее у удалённого
# (чужая работа) или у копии (тег, которого люди не видели) — решение человека.
sverit_imena_ssylok() { # $1=«sha ref» удалённого $2=«sha ref» копии
    local tam u_nas lishnee
    tam=$(printf '%s\n' "$1" | cut -d' ' -f2 | LC_ALL=C sort); u_nas=$(printf '%s\n' "$2" | cut -d' ' -f2 | LC_ALL=C sort)
    lishnee=$(LC_ALL=C comm -23 <(printf '%s\n' "$tam") <(printf '%s\n' "$u_nas") | paste -sd' ')
    [ -z "$lishnee" ] || { say "ОТКАЗ: на удалённом есть ссылки, которых нет в копии: $lishnee"; return 1; }
    lishnee=$(LC_ALL=C comm -13 <(printf '%s\n' "$tam") <(printf '%s\n' "$u_nas") | paste -sd' ')
    [ -z "$lishnee" ] || { say "ОТКАЗ: в копии есть ссылки, которых нет на удалённом: $lishnee"; return 1; }
}

# Гейт чистоты по ИСТОРИИ копии. Сам гейт на историю отвечает кодом 0 и лишь
# предупреждает (убрать её можно только перезаписью — то есть этой дверью), а
# здесь перепись и есть то, что должно убрать следы: дверь считает находки сама.
# Свежесть метки не судим: дерево копии уже опубликовано и сверено с деревом
# сборки, а метка называет пакет, из которого оно собрано; пересобрать нельзя.
proverit_chistotu_kopii() { # $1=копия $2=паспорт
    local vyvod rc=0
    vyvod=$(python3 -I "$SCRIPTS_REAL/check-public-clean.py" "$1" \
                --пакет "$PACKAGE_DIR" --проект "$PROJECT_DIR" --паспорт "$2" \
                --свежесть-не-судить 2>&1) || rc=$?
    if [ "$rc" != 0 ] || grep -q '⚠ история' <<< "$vyvod"; then
        say "ОТКАЗ: в копии остались следы установки (гейт чистоты, код $rc)"
        printf '%s\n' "$vyvod" | sed 's/^/  /'
        return 1
    fi
}

# Согласие владельца на перепись — «Да» кнопкой на предмет переписи (вершина удалённого ДО входит
# в предмет: после переписи прежнее «Да» повторный force-push не откроет). Нет кнопки — ворота
# сами просят её; рабочий каталог двери убирает обработчик выхода.
PREDMET_PEREPISI=""
vzyat_soglasie_dveri() { # $1=копия
    local otvet
    otvet=$(HARNESS_LOG_DIR="$LOG_DIR" python3 -I "$SCRIPTS_REAL/deploy_guard.py" --действие перепись \
                --только-согласие --просить --перепись "$1" 2>&1) || {
        say "ОТКАЗ: $otvet"
        return 1; }
    # Предмет, по которому открыта дверь; перед force-push он пересчитывается.
    PREDMET_PEREPISI=$(printf '%s\n' "$otvet" | sed -n 's/^предмет: //p' | tail -1)
    [ -n "$PREDMET_PEREPISI" ] || { say "ОТКАЗ: ворота не назвали предмет переписи"; return 1; }
}

# Сборка переводится на новую историю: fetch берёт ветку и теги v*, ветка
# переставляется под проверкой старого sha, индекс сбрасывается.
perevesti_sborku() { # $1=адрес $2=новый sha ветки
    local sborka="$PUBLIC_BUILD_DIR" star
    star=$(git -C "$sborka" rev-parse "refs/heads/$PUBLIC_BRANCH") || return 1
    git -C "$sborka" remote set-url "$PUBLIC_REMOTE" "$1" 2>/dev/null \
        || git -C "$sborka" remote add "$PUBLIC_REMOTE" "$1" || return 1
    git -C "$sborka" fetch -q "$PUBLIC_REMOTE" \
            "+refs/heads/$PUBLIC_BRANCH:refs/remotes/$PUBLIC_REMOTE/$PUBLIC_BRANCH" '+refs/tags/v*:refs/tags/v*' \
        && git -C "$sborka" update-ref "refs/heads/$PUBLIC_BRANCH" "$2" "$star" \
        && git -C "$sborka" reset -q
}

perepisat_istoriyu() { # $1=каталог копии $2=файл бэкапа
    local kopiya bekap adres pasport tam u_nas refy="" stroka sha ref novyj soobshchenie rabochij
    local -a arendy=() spisok=()
    kopiya=$(readlink -f "$1" 2>/dev/null); bekap=$(readlink -f "$2" 2>/dev/null)
    say "=== перепись публичной истории: копия → $PUBLIC_REMOTE/$PUBLIC_BRANCH ==="
    git -C "${kopiya:-/нет}" rev-parse --git-dir >/dev/null 2>&1 \
        || { say "ОТКАЗ: копии (репозитория) нет: $1"; return 1; }
    git -C "$kopiya" rev-parse -q --verify "refs/heads/$PUBLIC_BRANCH" >/dev/null \
        || { say "ОТКАЗ: в копии нет ветки «$PUBLIC_BRANCH»"; return 1; }
    [ -n "$bekap" ] || { say "ОТКАЗ: не указан бэкап (bundle): --бэкап <файл.bundle>"; return 1; }
    rabochij=$(mktemp -d)
    CLEANUP_DIRS+=("$rabochij")
    proverit_bekap "$bekap" "$rabochij" || return 1
    say "бэкап: $bekap, sha256 $(sha256sum "$bekap" | cut -d' ' -f1)"

    adres=$(git -C "$PACKAGE_DIR" remote get-url "$PUBLIC_REMOTE" 2>/dev/null || true)
    [ -n "$adres" ] || { say "ОТКАЗ: у пакета нет удалённого «$PUBLIC_REMOTE» — публиковать некуда"; return 1; }
    tam=$(ssylki_udalyonnogo "$adres") && [ -n "$tam" ] \
        || { say "ОТКАЗ: ссылок публичного удалённого прочитать не удалось (или их нет)"; return 1; }
    u_nas=$(ssylki_kopii "$kopiya")
    sverit_imena_ssylok "$tam" "$u_nas" || return 1
    bekap_pokryvaet "$bekap" "$tam" || return 1
    [ "$(git -C "$PUBLIC_BUILD_DIR" rev-parse "refs/heads/$PUBLIC_BRANCH" 2>/dev/null) refs/heads/$PUBLIC_BRANCH" \
        = "$(printf '%s\n' "$tam" | grep " refs/heads/$PUBLIC_BRANCH\$")" ] \
        || { say "ОТКАЗ: ветка сборки не равна ветке удалённого — в сборке неопубликованное"; return 1; }

    [ "$(git -C "$kopiya" rev-parse "refs/heads/$PUBLIC_BRANCH^{tree}")" \
        = "$(git -C "$PUBLIC_BUILD_DIR" rev-parse "refs/heads/$PUBLIC_BRANCH^{tree}" 2>/dev/null)" ] \
        || { say "ОТКАЗ: дерево ветки в копии не равно дереву локальной сборки — перепись меняет историю, не содержимое"; return 1; }
    # Паспорт передаётся явно, как в замке 6: иначе гейт читает боевой.
    pasport="${PUBLIC_PASPORT:-$KONF_DVERI_KOREN/install.conf}"
    proverit_chistotu_kopii "$kopiya" "$pasport" || return 1
    say "проверки пройдены: ссылки сверены, бэкап покрывает удалённый, дерево равно сборке, следов в истории нет"

    vzyat_soglasie_dveri "$kopiya" || return 1
    while read -r sha ref; do
        arendy+=("--force-with-lease=$ref:$sha")
    done <<< "$tam"
    # Без «+» в refspec: принудительность даёт ТОЛЬКО аренда. «+» отменил бы её проверку.
    while read -r sha ref; do
        spisok+=("$ref:$ref")
    done <<< "$u_nas"
    # Между «Да» и отправкой удалённый и копия обязаны остаться теми же, что на кнопке.
    soobshchenie=$(python3 -I "$SCRIPTS_REAL/lib/door_subjects.py" сверить-перепись "$PREDMET_PEREPISI" \
                       "$kopiya" "$PACKAGE_DIR" "$PUBLIC_REMOTE" "$PUBLIC_BRANCH" 2>&1) \
        || { say "ОТКАЗ: $soobshchenie"; return 1; }
    if ! git -C "$kopiya" push --atomic "${arendy[@]}" "$adres" "${spisok[@]}" 2>&1; then
        say "ОТКАЗ: атомарная отправка не прошла (удалённый ушёл вперёд или отверг) — ничего не изменено"
        return 1
    fi
    say "отправлено одной атомарной отправкой: ссылок $(printf '%s\n' "$u_nas" | grep -c .)"
    say "откат: бэкап $bekap; прежние ссылки удалённого:"
    printf '%s\n' "$tam" | sed 's/^/  /'
    say "откат: git clone --mirror $bekap <каталог> && git -C <каталог> push --mirror <адрес «$PUBLIC_REMOTE» из git remote get-url>"
    [ "$(ssylki_udalyonnogo "$adres")" = "$u_nas" ] \
        || { say "ОТКАЗ: после отправки ссылки удалённого не равны ссылкам копии — смотри откат выше"; return 1; }
    novyj=$(git -C "$kopiya" rev-parse "refs/heads/$PUBLIC_BRANCH")
    perevesti_sborku "$adres" "$novyj" \
        || { say "ОТКАЗ: удалённый переписан, но сборку перевести не удалось — смотри откат выше"; return 1; }
    say "переписано: удалённый == копия, сборка переведена на новую историю"
}

if [ "${1:-}" = "--selftest" ]; then
    ok=1
    # БОЛЬНОЙ СЛУЧАЙ первым: несуществующая ветка обязана дать отказ, а не
    # молчаливый успех — «опубликовано» без публикации хуже, чем отказ.
    # Пустые переменные окружения проба создать НЕ может: скрипт читает
    # harness.conf и перезаписывает их. Условие создаётся пустым конфигом —
    # иначе проба судит машину, а не код (она молча падала до 11.09.2026).
    # Паспорт задан, а ветки пусты: условие пробы — ИМЕННО незаданные ветки.
    # Прежде проба подавала пустой конфиг целиком, и после снятия умолчания
    # пути скрипт честно отказывал раньше — про PROJECT_DIR (12.09.2026).
    # Дверь берёт паспорт рядом с СОБОЙ, а не из окружения: стенд кладёт КОПИЮ двери в
    # свой каталог, и паспорт стенда ей даёт HARNESS_DOOR_STAND_HOME (как у drop-database.sh).
    STEND=$(mktemp -d); mkdir -p "$STEND/scripts"
    cp "$SCRIPTS_REAL/publish.sh" "$STEND/scripts/publish.sh"
    ln -s "$SCRIPTS_REAL/lib" "$STEND/scripts/lib"
    PUSTOJ_KONFIG="$STEND/install.conf"
    printf 'PROJECT_DIR="%s"\nPUBLIC_BRANCH=""\n' "$(mktemp -d)" > "$PUSTOJ_KONFIG"
    OUT=$(HARNESS_DOOR_STAND_HOME="$STEND" bash "$STEND/scripts/publish.sh" --проверка 2>&1); RC=$?
    if [ "$RC" = 2 ] && printf '%s' "$OUT" | grep -q "PUBLIC_BRANCH"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: ветки не заданы — отказ с внятной причиной"
    else
        echo "  ПЛОХО ветки не заданы: код $RC, ответ: ${OUT:0:120}"; ok=0
    fi
    # БОЛЬНОЙ СЛУЧАЙ: паспорта нет вовсе. Прежде здесь стоял наш путь
    # умолчанием — скрипт брался публиковать ЧУЖУЮ установку как свою.
    : > "$PUSTOJ_KONFIG"
    OUT=$(HARNESS_DOOR_STAND_HOME="$STEND" bash "$STEND/scripts/publish.sh" --проверка 2>&1); RC=$?
    rm -rf "$STEND"
    if [ "$RC" != 0 ] && printf '%s' "$OUT" | grep -q "PROJECT_DIR"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: без паспорта отказ вслух, а не чужой каталог"
    else
        echo "  ПЛОХО без паспорта: код $RC, ответ: ${OUT:0:120}"; ok=0
    fi
    # БОЛЬНОЙ СЛУЧАЙ 2: сегодняшние задачи лежат в статусе test — замок обязан
    # их назвать. Прежняя редакция читала историю ПАКЕТА, где трейлеров нет, и
    # молча отвечала «всё проверено».
    # Смысл пробы — ЧТЕНИЕ ИСТОРИИ: замок обязан брать id задач из истории
    # ПРОЕКТА (там трейлеры Dev-Map), а не пакета, где их ноль. Прежняя
    # редакция судила по тому, остались ли в живой карте непроверенные
    # задачи, — и 14.09.2026 покраснела ровно потому, что их закрыли: проба
    # совпадала с состоянием машины вместо того, чтобы создавать своё условие.
    # Проба СТРОИТ свою историю: репозиторий, два коммита, трейлер в одном.
    # Прежняя редакция считала трейлеры за сутки в ЖИВОЙ истории машины и
    # 21.09.2026 покраснела на исправном коде — с 14.09 коммитов не было.
    ISTORIYA_DVOR=$(mktemp -d)
    (
        cd "$ISTORIYA_DVOR" || exit 1
        git init -q . && git config user.email проба@проба && git config user.name проба
        echo первый > a.txt && git add a.txt
        git commit -q -m "без трейлера"
        echo второй > b.txt && git add b.txt
        printf 'с трейлером\n\nDev-Map: hr.проба-чтения-истории\n' | git commit -q -F -
    ) >/dev/null 2>&1
    NASHLI=$(PROJECT_DIR="$ISTORIYA_DVOR" ids_iz_istorii "")
    if [ "$NASHLI" = "hr.проба-чтения-истории" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: трейлер из истории ПРОЕКТА прочитан (своё условие, не живая машина)"
    else
        echo "  ПЛОХО замок не читает трейлер Dev-Map из истории: вышло «$NASHLI»"; ok=0
    fi
    # Коммит без трейлера в тот же счёт не идёт: иначе «прочитали» нельзя
    # отличить от «нашли что попало».
    if [ "$(PROJECT_DIR="$ISTORIYA_DVOR" ids_iz_istorii "" | grep -c .)" = 1 ]; then
        echo "  ок    коммит без трейлера в счёт не идёт"
    else
        echo "  ПЛОХО коммит без трейлера попал в счёт"; ok=0
    fi
    # Поезд судит замок 3 до СРЕЗА проекта: задача, влитая после среза, едет следующим поездом.
    DO_SREZA=$(git -C "$ISTORIYA_DVOR" rev-parse HEAD~1)
    printf 'lines: []\n' > "$ISTORIYA_DVOR/dev-map.yaml"
    if [ -z "$(PROJECT_DIR="$ISTORIYA_DVOR" TRAIN_SLICE_PROJECT="$DO_SREZA" непроверенные_с "")" ] \
       && [ -n "$(PROJECT_DIR="$ISTORIYA_DVOR" TRAIN_SLICE_PROJECT="" непроверенные_с "")" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — замок 3 читает трейлеры до среза проекта, не до HEAD"
    else
        echo "  ПЛОХО поезд: замок 3 судит трейлер, влитый после среза проекта"; ok=0
    fi
    # ОТКАЗ git ≠ пустая история: замок, прочитавший отказ как чистоту, выпустил
    # бы наружу непроверенное.
    PROJECT_DIR="$ISTORIYA_DVOR/нет-такого" ids_iz_istorii "" >/dev/null 2>&1
    if [ $? = 2 ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: git отказал — код 2, а не пустота"
    else
        echo "  ПЛОХО отказ git прочитан как пустая история"; ok=0
    fi
    OTKAZ_VYVOD=$(PROJECT_DIR="$ISTORIYA_DVOR/нет-такого" непроверенные_с "")
    if [ "$OTKAZ_VYVOD" = "ОТКАЗ-ИСТОРИИ" ]; then
        echo "  ок    отказ доходит до читателя словом, а не молчанием"
    else
        echo "  ПЛОХО отказ истории не дошёл до читателя: «$OTKAZ_VYVOD»"; ok=0
    fi
    rm -rf "$ISTORIYA_DVOR"
    # БОЛЬНОЙ СЛУЧАЙ 3: разбор статусов витрины судится своим самотестом —
    # тем же файлом, который зовёт замок, а не его копией здесь.
    if python3 -I "$SCRIPTS_REAL/vitrina_status.py" --selftest | tail -1 \
       | grep -q "САМОТЕСТ ПРОЙДЕН"; then
        echo "  ок    разбор статусов витрины: «принято» не держит, «test» держит"
    else
        echo "  ПЛОХО самотест разбора статусов витрины не прошёл"; ok=0
    fi
    # Далёкое будущее: задач нет — и замок обязан молчать, а не выдумывать.
    if [ -z "$(непроверенные_с "$(date -d '1 day' +%Y-%m-%dT%H:%M:%S)")" ]; then
        echo "  ок    задач после момента нет — замок молчит"
    else
        echo "  ПЛОХО замок нашёл задачи в будущем"; ok=0
    fi
    # ── пути публикации СБОРКИ: подставной пакет и подставной удалённый ──
    # Улика «push не звался» — состояние удалённого репозитория, а не
    # подставной git в PATH: подставной git сломал бы и саму сборку, и проба
    # судила бы свою заглушку вместо кода (правило смены 12.09.2026).
    T=$(mktemp -d)
    proba_dvor() {  # готовит подставные пакет, удалённый и конфиги; печатает каталог
        local dvor="$1"
        mkdir -p "$dvor/пакет/harness/config" "$dvor/пакет/harness/память" \
                 "$dvor/пакет/harness/шаблоны-задач" "$dvor/сборка"
        printf '# харнес\n@STATE.md\n' > "$dvor/пакет/harness/CLAUDE.md"
        printf 'состояние\n' > "$dvor/пакет/harness/STATE.md"
        printf '# ТОН\nзамер речи\n' > "$dvor/пакет/harness/память/TONE.md"
        printf '# ТОН\n\n<!-- потолки: мягкий=700 жёсткий=1500 -->\n' \
            > "$dvor/пакет/harness/шаблоны-задач/ТОН-заготовка.md"
        printf 'исключить: []\nзаменить_шаблоном:\n  harness/память/TONE.md: harness/шаблоны-задач/ТОН-заготовка.md\nобезличить_ключи: [PROJECT_DIR]\nоставить: [harness/CLAUDE.md]\n' \
            > "$dvor/пакет/harness/config/public-build.yaml"
        : > "$dvor/пакет/ЧТО-НОВОГО.md"
        proba_zapis "$dvor" "${2:-Выпуск 0.1.0: первый}"
        git -C "$dvor/пакет" init -q -b master
        git -C "$dvor/пакет" add -A
        git -C "$dvor/пакет" -c user.name=t -c user.email=t@t commit -qm проба
        git init -q --bare "$dvor/удалённый.git"
        git -C "$dvor/пакет" remote add jig "$dvor/удалённый.git"
        printf 'PROJECT_DIR="%s"\nAGENT_USER="подставной"\nLOG_DIR="%s"\nPROJECT_NAME="Подставной"\nTG_CHAT_ID="100000001"\nPRODUCT_DIR="tovar"\n' \
            "$PROJECT_DIR" "$dvor" > "$dvor/паспорт.conf"
        printf 'PUBLIC_REMOTE="jig"\nPUBLIC_BRANCH="master"\nPUBLIC_BUILD_DIR="%s"\nPUBLIC_COMMIT_NAME="проба-харнес"\nPUBLIC_COMMIT_EMAIL="proba@harness.local"\n' \
            "$dvor/сборка" > "$dvor/харнес.conf"
        # Подставной судья типовой копии (замок 6г): код — из файла typical-rc двора,
        # доводы — в typical-args; настоящий стенд весит гигабайт и идёт своей пробой.
        printf '#!/usr/bin/env python3\nimport sys, pathlib\nd = pathlib.Path(%s)\n(d / "typical-args").write_text(" ".join(sys.argv[1:]) + " | прошлое: " + ",".join(sorted(p.name for p in pathlib.Path(sys.argv[2]).iterdir())))\nrc = int((d / "typical-rc").read_text()) if (d / "typical-rc").exists() else 0\nprint("типовая копия: новая красная «изоляция tmux»: свой файл копии" if rc else "типовая копия: новых красных нет (подставной)")\nsys.exit(rc)\n' \
            "'$dvor'" > "$dvor/typical-stub.py"
    }
    proba_zapis() {  # $1=двор $2=название: новая запись простым языком СВЕРХУ заметок
        local fajl="$1/пакет/ЧТО-НОВОГО.md" staroe
        staroe=$(cat "$fajl")
        printf '# Что нового\n\n## 2026-10-24 — %s\n\n### Исправлено\n\n- Сессия больше не обрывается.\n\n### Что изменилось для вас\n\n- Ничего делать не нужно.\n\n%s\n' \
            "$2" "${staroe#\# Что нового}" > "$fajl"
    }
    proba_publ() {  # запускает публикацию сборки в подставном дворе
        # ПОДОБОЛОЧКА: присваивания перед вызовом ФУНКЦИИ остаются в оболочке
        # после возврата, и дворы протекали друг в друга — проба, забывшая
        # задать переменную, зеленела на наследстве соседней.
        local dvor="$1"
        (
            PACKAGE_DIR="$dvor/пакет" PUBLIC_BUILD_DIR="$dvor/сборка" \
            PUBLIC_REMOTE=jig PUBLIC_BRANCH=master \
            PUBLIC_COMMIT_NAME="проба-харнес" PUBLIC_COMMIT_EMAIL="proba@harness.local" \
            PUBLIC_PASPORT="$dvor/паспорт.conf" TYPICAL_COPY_CHECK="$dvor/typical-stub.py" \
            PROJECT_DIR="$PROJECT_DIR" опубликовать_сборку 2>&1
        )
    }
    kommitov() { git -C "$1" rev-list --count master 2>/dev/null || echo 0; }

    # ДВЕРЬ ЗАМКА 1: пропуск полного прогона по слову владельца. Двор — подставной
    # проект с заглушками ворот: проба судит выбор прогона, а не сами ворота.
    proba_vorota() { # $1=двор $2=запись (json или пусто) → печатает вывод замка
        local d="$1"
        mkdir -p "$d/проект/scripts" "$d/проект/harness/config" "$d/лог"
        # Быстрая дверь открывается, только если скан секретов в составе быстрых ворот.
        printf 'checks:\n  - name: секреты\n    cmd: bash scripts/check-secrets.sh "$ROOT"\n' \
            > "$d/проект/harness/config/gates-hackathon.yaml"
        printf '#!/bin/sh\necho "gates $*" >> "%s/звали"\n' "$d" > "$d/проект/scripts/gates.sh"
        printf '#!/bin/sh\necho "secrets" >> "%s/звали"\n' "$d" > "$d/проект/scripts/check-secrets.sh"
        printf 'open("%s/звали","a").write("build\\n")\n' "$d" > "$d/проект/scripts/build-public.py"
        [ -n "$2" ] && printf '%s' "$2" > "$d/лог/publish-gates-waiver.json"
        ( PROJECT_DIR="$d/проект" zamok_vorot "$d/лог" "$d/проект/scripts" 2>&1 ); echo "rc=$?"
    }
    zapis_propuska() { # $1=выдана-смещение $2=до-смещение (сек от сейчас)
        local n; n=$(date +%s)
        printf '{"слово":"без прогона полного","id_входящего":"1793351076.532892567","причина":"проба","выдана":%s,"до":%s}' \
            "$((n+$1))" "$((n+$2))"
    }
    OUT=$(proba_vorota "$T/в1" ""); if printf '%s' "$OUT" | grep -q 'rc=0' \
       && grep -qx 'gates ' "$T/в1/звали" && ! grep -q -- 'хакатон' "$T/в1/звали"; then
        echo "  ок    ворота без записи о пропуске — полный прогон, как раньше"
    else echo "  ПЛОХО без записи не полный прогон: ${OUT:0:150}"; ok=0; fi
    OUT=$(proba_vorota "$T/в2" "$(zapis_propuska -60 3600)"); if printf '%s' "$OUT" | grep -q 'rc=0' \
       && grep -q 'gates --хакатон' "$T/в2/звали" && ! grep -q secrets "$T/в2/звали" \
       && grep -q build "$T/в2/звали" && grep -q 'ворота пропущены по слову владельца «без прогона полного»' "$T/в2/лог/publish.log"; then
        echo "  ок    запись о пропуске — быстрое подмножество и строка в журнале публикации"
    else echo "  ПЛОХО запись о пропуске не открыла быструю дверь: ${OUT:0:150}"; ok=0; fi
    # Просроченная запись — записи нет: полный прогон, как без неё, а не отказ.
    OUT=$(proba_vorota "$T/в3" "$(zapis_propuska -7200 -60)"); if printf '%s' "$OUT" | grep -q 'rc=0' \
       && printf '%s' "$OUT" | grep -q 'пропуск ворот просрочен (до .*) — как нет пропуска' \
       && ! printf '%s' "$OUT" | grep -q 'ОТКАЗ' \
       && grep -qx 'gates ' "$T/в3/звали" && ! grep -q -- 'хакатон' "$T/в3/звали"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: просроченная запись о пропуске — полный прогон, не отказ"
    else echo "  ПЛОХО просроченная запись: ${OUT:0:150}"; ok=0; fi
    OUT=$(proba_vorota "$T/в4" "$(zapis_propuska -60 999999)"); if printf '%s' "$OUT" | grep -q 'rc=1' \
       && printf '%s' "$OUT" | grep -q 'срок' && [ ! -e "$T/в4/звали" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: запись без конечного срока (дольше 6 часов) — не дверь"
    else echo "  ПЛОХО запись без срока пропущена: ${OUT:0:150}"; ok=0; fi
    OUT=$(proba_vorota "$T/в5" '{"слово":"","id_входящего":"1","причина":"x","выдана":1,"до":2}'); if printf '%s' "$OUT" | grep -q 'rc=1'; then
        echo "  ок    запись без цитаты владельца — не дверь"
    else echo "  ПЛОХО запись без цитаты пропущена: ${OUT:0:150}"; ok=0; fi

    # ДВЕРЬ ЗАМКА 3: та же запись пропускает статусы, владельцу уходит список.
    proba_statusy() { # $1=двор $2=запись → вывод и rc
        local d="$1"
        mkdir -p "$d/проект/scripts" "$d/лог"
        printf '#!/bin/sh\necho "$1" >> "%s/ушло"\n' "$d" > "$d/проект/scripts/tg_send.sh"
        [ -n "$2" ] && printf '%s' "$2" > "$d/лог/publish-gates-waiver.json"
        ( PROJECT_DIR="$d/проект" SCRIPTS_REAL="$d/проект/scripts" CHECK_ONLY=0 propustit_statusy_po_slovu "$d/лог" $'задача-а (статус test)\nзадача-б (статус test)' 2>&1 ); echo "rc=$?"
    }
    OUT=$(proba_statusy "$T/с1" ""); if printf '%s' "$OUT" | grep -q 'rc=1' && [ ! -e "$T/с1/ушло" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: статусы без записи о пропуске — замок 3 не снят"
    else echo "  ПЛОХО без записи статусы пропущены: ${OUT:0:150}"; ok=0; fi
    OUT=$(proba_statusy "$T/с2" "$(zapis_propuska -7200 -60)"); if printf '%s' "$OUT" | grep -q 'rc=1' && [ ! -e "$T/с2/ушло" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: статусы с просроченной записью — замок 3 не снят"
    else echo "  ПЛОХО просроченная запись сняла статусы: ${OUT:0:150}"; ok=0; fi
    OUT=$(proba_statusy "$T/с3" "$(zapis_propuska -60 3600)"); if printf '%s' "$OUT" | grep -q 'rc=0' \
       && grep -q 'задача-а, задача-б' "$T/с3/ушло" && grep -q 'статусы пропущены по слову владельца «без прогона полного»' "$T/с3/лог/publish.log"; then
        echo "  ок    запись о пропуске — статусы пропущены, список ушёл владельцу, строка в журнале"
    else echo "  ПЛОХО статусы по записи: ${OUT:0:150}"; ok=0; fi

    # ЗАМОК ОЧЕРЕДИ СЛИЯНИЯ: пока публикация его держит, take_lock самой очереди
    # (merge-queue.py, не копия правила) не берёт его; занятый — ожидание и отказ вслух.
    queue_takes_lock() { # $1=каталог журнала → код take_lock очереди за 1 с: 0 взяла, 8 занято
        python3 -I - "$SCRIPTS_REAL/merge-queue.py" "$1" <<'PY'
import importlib.util, sys, types
from pathlib import Path
spec = importlib.util.spec_from_file_location("queue", sys.argv[1]); queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)
try:
    queue.take_lock(types.SimpleNamespace(log_dir=Path(sys.argv[2]), wait_seconds=1))[0].close()
except queue.Stop as stop:
    sys.exit(stop.code)
PY
    }
    mkdir -p "$T/очередь"
    flock -x "$T/очередь/merge-queue.lock" sleep 5 &
    HOLDER_PID=$!
    sleep 1
    OUT=$( (take_merge_queue_lock "$T/очередь" 1; echo "rc=$?") 2>&1 )
    if printf '%s' "$OUT" | grep -q 'rc=1' && printf '%s' "$OUT" | grep -q 'очередь слияния занята, жду' \
       && printf '%s' "$OUT" | grep -q 'ОТКАЗ'; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: очередь сливает — публикация ждёт и отказывает вслух по пределу"
    else echo "  ПЛОХО занятая очередь: ${OUT:0:150}"; ok=0; fi
    wait "$HOLDER_PID"
    OUT=$( (take_merge_queue_lock "$T/очередь" 60 && queue_takes_lock "$T/очередь"; echo "rc=$?") 2>&1 )
    AFTER_EXIT=$(queue_takes_lock "$T/очередь" >/dev/null 2>&1; echo $?)
    if printf '%s' "$OUT" | grep -q 'rc=8' && [ "$AFTER_EXIT" = 0 ]; then
        echo "  ок    публикация держит замок очереди слияния: очередь не сливает, после выхода замок свободен"
    else echo "  ПЛОХО замок очереди: пока держит публикация — ${OUT:0:120}; после выхода — код $AFTER_EXIT"; ok=0; fi
    # Осиротевший потомок публикации (setsid, переживает её) замка не держит: обработчик
    # выхода в подоболочке не стоит, отпускает только закрытая труба держателя.
    ( take_merge_queue_lock "$T/очередь" 5 && { setsid sleep 30 & echo "$!" > "$T/очередь/сирота"; } ) >/dev/null 2>&1
    AFTER_ORPHAN=$(queue_takes_lock "$T/очередь" >/dev/null 2>&1; echo $?)
    kill "$(cat "$T/очередь/сирота" 2>/dev/null)" 2>/dev/null
    if [ -s "$T/очередь/сирота" ] && [ "$AFTER_ORPHAN" = 0 ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: потомок пережил публикацию — очередь берёт замок сразу"
    else echo "  ПЛОХО осиротевший потомок держит очередь: код take_lock $AFTER_ORPHAN"; ok=0; fi

    # СУХОЙ ПРОГОН: замки 4-6 обязаны выполниться, а коммита и отправки быть не
    # должно. Прежде «--проверка» печатала «публиковать можно», не выполнив ни
    # одного замка: владелец видел зелёное и тратил согласие впустую (ревью
    # кода 12.09.2026, №5).
    proba_dvor "$T/сухо"
    BYLO_SUHO=$(kommitov "$T/сухо/удалённый.git")
    OUT=$( (PACKAGE_DIR="$T/сухо/пакет" PUBLIC_BUILD_DIR="$T/сухо/сборка" \
            PUBLIC_REMOTE=jig PUBLIC_BRANCH=master \
            PUBLIC_COMMIT_NAME="проба-харнес" PUBLIC_COMMIT_EMAIL="proba@harness.local" \
            PUBLIC_PASPORT="$T/сухо/паспорт.conf" PROJECT_DIR="$PROJECT_DIR" \
            опубликовать_сборку --сухо 2>&1) ); RC=$?
    STALO_SUHO=$(kommitov "$T/сухо/удалённый.git")
    if [ "$RC" = 0 ] && printf '%s' "$OUT" | grep -q 'замок 6' \
       && printf '%s' "$OUT" | grep -q 'сухой прогон' && [ "$BYLO_SUHO" = "$STALO_SUHO" ]; then
        echo "  ок    сухой прогон: замки 4-6 выполнены, ничего не отправлено"
    else
        echo "  ПЛОХО сухой прогон: код $RC, коммитов $BYLO_SUHO→$STALO_SUHO: $(printf '%s' "$OUT" | tail -2 | tr '\n' ' ')"; ok=0
    fi

    # БОЛЬНОЙ: след установки в коде сборки — отказ ДО push.
    proba_dvor "$T/чистота"
    printf 'ПУТЬ = "%s"\n' "$PROJECT_DIR" > "$T/чистота/пакет/harness/след.py"
    git -C "$T/чистота/пакет" add -A
    git -C "$T/чистота/пакет" -c user.name=t -c user.email=t@t commit -qm след
    BYLO=$(kommitov "$T/чистота/удалённый.git")
    OUT=$(proba_publ "$T/чистота"); RC=$?
    if [ "$RC" != 0 ] && printf '%s' "$OUT" | grep -q 'образец=PROJECT_DIR' \
       && [ "$(kommitov "$T/чистота/удалённый.git")" = "$BYLO" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: след установки — отказ, push не звался"
    else
        echo "  ПЛОХО след установки не остановил публикацию: код $RC, ${OUT:0:100}"; ok=0
    fi

    # БОЛЬНОЙ: похожее на токен в сборке — отказ замком секретов ДО коммита.
    proba_dvor "$T/секрет"
    PREFIKS="12345678"
    OBRAZEC='TOKEN = "%s:AA%s"'   # не-секрет: образец формы, собирается из кусков
    printf "$OBRAZEC\n" "$PREFIKS" "$(printf 'a%.0s' $(seq 33))" \
        > "$T/секрет/пакет/harness/тайна.py"
    git -C "$T/секрет/пакет" add -A
    git -C "$T/секрет/пакет" -c user.name=t -c user.email=t@t commit -qm тайна
    OUT=$(proba_publ "$T/секрет"); RC=$?
    # Проба требует ПРИЧИНУ отказа, а не просто ненулевой код: «команды нет»
    # (127) выглядит как успешная защита и зеленит пробу впустую.
    if [ "$RC" != 0 ] && printf '%s' "$OUT" | grep -qi 'секрет' \
       && [ "$(kommitov "$T/секрет/удалённый.git")" = "0" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: похожее на секрет — отказ, ничего не уехало"
    else
        echo "  ПЛОХО секрет в сборке не остановил публикацию: код $RC, ${OUT:0:100}"; ok=0
    fi

    # Здоровый: чистый пакет — коммит своим автором, удалённый вырос на один.
    proba_dvor "$T/здоровый"
    OUT=$(proba_publ "$T/здоровый"); RC=$?
    SOOBSH=$(git -C "$T/здоровый/сборка" log -1 --format=%s 2>/dev/null)
    AVTOR=$(git -C "$T/здоровый/сборка" log -1 --format=%ae 2>/dev/null)
    HESH_PAKETA=$(git -C "$T/здоровый/пакет" rev-parse --short HEAD)
    if [ "$RC" = 0 ] && [ "$(kommitov "$T/здоровый/удалённый.git")" = "1" ] \
       && printf '%s' "$SOOBSH" | grep -q "^версия от" \
       && printf '%s' "$SOOBSH" | grep -q "$HESH_PAKETA" \
       && [ "$AVTOR" = "proba@harness.local" ]; then
        echo "  ок    здоровый: сборка опубликована, автор и хэш пакета в коммите"
    else
        echo "  ПЛОХО здоровая публикация не прошла: код $RC, «$SOOBSH», автор $AVTOR"; ok=0
    fi

    # Релиз по стандарту: у опубликованного есть НОМЕР, МЕТКА и ЗАМЕТКИ.
    # Проверяется удалённый, а не локальная сборка: тег, не уехавший наружу,
    # для человека не существует.
    TEGI=$(git -C "$T/здоровый/удалённый.git" tag -l 'v*' | tr '\n' ' ')
    VERS=$(tr -d '[:space:]' < "$T/здоровый/сборка/VERSION" 2>/dev/null)
    ZHURNAL=$(cat "$T/здоровый/сборка/CHANGELOG.md" 2>/dev/null)
    if [ -n "$VERS" ] && printf '%s' "$TEGI" | grep -q "v$VERS" \
       && printf '%s' "$ZHURNAL" | grep -q "## \[$VERS\]" \
       && printf '%s' "$ZHURNAL" | grep -q "$HESH_PAKETA"; then
        echo "  ок    релиз: версия $VERS, тег на удалённом, журнал с хэшем пакета"
    else
        echo "  ПЛОХО релиз без метки: VERSION «$VERS», теги «$TEGI», журнала знаков ${#ZHURNAL}"; ok=0
    fi

    # ВТОРОЙ РЕЛИЗ тем же путём: номер растёт, журнал НАКАПЛИВАЕТ секции.
    # Условие создаётся боевым способом — новый файл-механизм в пакете, и весь
    # путь идёт целиком, вместе со сборщиком. Он зовёт `rsync -a --delete`, а
    # VERSION и CHANGELOG.md лежат в сборке, но не в пакете: без починки
    # 12.09.2026 журнал обнулялся на каждой публикации и история версий
    # исчезала, оставляя одну секцию.
    mkdir -p "$T/здоровый/пакет/harness/demons"
    printf '#!/bin/sh\necho второй демон присмотра\n' \
        > "$T/здоровый/пакет/harness/demons/новый.sh"
    proba_zapis "$T/здоровый" "Выпуск 0.2.0: второй"
    git -C "$T/здоровый/пакет" add -A
    git -C "$T/здоровый/пакет" -c user.name=t -c user.email=t@t commit -qm "новый демон"
    OUT=$(proba_publ "$T/здоровый"); RC=$?
    VERS2=$(tr -d '[:space:]' < "$T/здоровый/сборка/VERSION" 2>/dev/null)
    TEGI2=$(git -C "$T/здоровый/удалённый.git" tag -l 'v*' | tr '\n' ' ')
    SEKCIJ=$(grep -c '^## \[' "$T/здоровый/сборка/CHANGELOG.md" 2>/dev/null || echo 0)
    if [ "$RC" = 0 ] && [ "$VERS2" = "0.2.0" ] && [ "$SEKCIJ" = "2" ] \
       && printf '%s' "$TEGI2" | grep -q "v0.1.0" \
       && printf '%s' "$TEGI2" | grep -q "v0.2.0"; then
        echo "  ок    второй релиз: новый механизм дал минор $VERS2, тегов 2, секций в журнале 2"
    else
        echo "  ПЛОХО второй релиз: код $RC, версия «$VERS2», теги «$TEGI2», секций $SEKCIJ"; ok=0
    fi
    TEGI="$TEGI2"

    # БОЛЬНОЙ (замок 6г): типовая чистая копия нашла новую красную — выпуск не уходит,
    # в отказе имя проверки и причина; судье дан прошлый выпуск с удалённого.
    printf 'ещё механизм\n' > "$T/здоровый/пакет/harness/demons/третий.sh"
    proba_zapis "$T/здоровый" "Выпуск 0.3.0: с красной копии"
    git -C "$T/здоровый/пакет" add -A
    git -C "$T/здоровый/пакет" -c user.name=t -c user.email=t@t commit -qm "красная копии"
    BYLO=$(kommitov "$T/здоровый/удалённый.git")
    echo 1 > "$T/здоровый/typical-rc"
    OUT=$(proba_publ "$T/здоровый"); RC=$?
    rm -f "$T/здоровый/typical-rc"
    if [ "$RC" != 0 ] && printf '%s' "$OUT" | grep -q 'типовая чистая копия' \
       && printf '%s' "$OUT" | grep -q 'новая красная «изоляция tmux»: свой файл копии' \
       && grep -q 'VERSION' "$T/здоровый/typical-args" \
       && [ "$(kommitov "$T/здоровый/удалённый.git")" = "$BYLO" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: новая красная типовой копии — отказ с именем и причиной, push не звался"
    else
        echo "  ПЛОХО красная типовой копии не остановила выпуск: код $RC, $(cut -c1-120 "$T/здоровый/typical-args" 2>/dev/null), ${OUT: -160}"; ok=0
    fi

    # БОЛЬНОЙ: удалённый ушёл вперёд чужим коммитом — отказ без force.
    CHUZHOJ=$(mktemp -d)
    git clone -q "$T/здоровый/удалённый.git" "$CHUZHOJ/копия"
    printf 'чужое\n' > "$CHUZHOJ/копия/чужое.txt"
    git -C "$CHUZHOJ/копия" add -A
    git -C "$CHUZHOJ/копия" -c user.name=ч -c user.email=ч@ч commit -qm чужое
    git -C "$CHUZHOJ/копия" push -q origin master
    BYLO=$(kommitov "$T/здоровый/удалённый.git")
    printf 'ещё\n' > "$T/здоровый/пакет/новое.txt"
    proba_zapis "$T/здоровый" "Выпуск 0.3.0: третий"
    git -C "$T/здоровый/пакет" add -A
    git -C "$T/здоровый/пакет" -c user.name=t -c user.email=t@t commit -qm ещё
    OUT=$(proba_publ "$T/здоровый"); RC=$?
    # Тег — обещание версии. Ветка не уехала — тега быть не должно: иначе на
    # удалённом появилась бы метка на коммит, которого там нет.
    TEGI_POSLE=$(git -C "$T/здоровый/удалённый.git" tag -l 'v*' | tr '\n' ' ')
    if [ "$RC" != 0 ] && printf '%s' "$OUT" | grep -qi 'расхожд' \
       && [ "$(kommitov "$T/здоровый/удалённый.git")" = "$BYLO" ] \
       && [ "$TEGI_POSLE" = "$TEGI" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: расхождение веток — отказ, история не переписана, тега нет"
    else
        echo "  ПЛОХО расхождение не остановило push: код $RC, ${OUT:0:100}"; ok=0
    fi
    rm -rf "$T" "$CHUZHOJ"

    # БОЛЬНОЙ: прошлая отправка не прошла (сеть, права, ff), дерево с тех пор
    # не менялось. Повтор обязан ОТПРАВИТЬ, а не сказать «публиковать нечего»:
    # прежняя редакция сверяла индекс с HEAD сборки и возвращала успех, хотя
    # коммит так и не уезжал (ревью кода 12.09.2026, И-4).
    # Условие создаётся ТАК, КАК ОНО БЫВАЕТ: падает сама отправка (нет прав на
    # удалённый), коммит и тег уже сделаны локально. Прежняя редакция пробы
    # сносила и пересоздавала удалённый ПОСЛЕ успешной публикации — тег при
    # этом существовал, номер рос, и проба зеленела, пока боевой путь стоял
    # намертво (ревью кода 12.09.2026, R-4).
    proba_dvor "$T/повтор"
    chmod -R a-w "$T/повтор/удалённый.git"
    OUT=$(proba_publ "$T/повтор"); RC=$?
    NET_TEGA=$(git -C "$T/повтор/удалённый.git" tag -l 'v*' | tr -d '[:space:]')
    if [ "$RC" != 0 ] && [ "$(kommitov "$T/повтор/удалённый.git")" = "0" ] && [ -z "$NET_TEGA" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: отправка не прошла — отказ, наружу не уехало ничего"
    else
        echo "  ПЛОХО упавшая отправка не дала отказа: код $RC, ${OUT:0:100}"; ok=0
    fi
    # Права вернулись: повтор обязан ДОЖАТЬ тот же релиз — тем же номером.
    chmod -R u+w "$T/повтор/удалённый.git"
    OUT=$(proba_publ "$T/повтор"); RC=$?
    TEGI_POVTOR=$(git -C "$T/повтор/удалённый.git" tag -l 'v*' | tr '\n' ' ')
    VERS_POVTOR=$(tr -d '[:space:]' < "$T/повтор/сборка/VERSION" 2>/dev/null)
    if [ "$RC" = 0 ] && [ "$(kommitov "$T/повтор/удалённый.git")" -ge 1 ] \
       && printf '%s' "$TEGI_POVTOR" | grep -q "v$VERS_POVTOR"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: повтор после упавшей отправки дожал релиз v$VERS_POVTOR"
    else
        echo "  ПЛОХО повтор не отправил: код $RC, коммитов $(kommitov "$T/повтор/удалённый.git"), теги «$TEGI_POVTOR», версия «$VERS_POVTOR», ${OUT:0:90}"; ok=0
    fi

    # БОЛЬНОЙ: публиковать нечего — в пакете с прошлого релиза ни одного
    # изменения. Второй прогон обязан промолчать, а не выпустить призрачный
    # релиз с новым номером и строкой «Изменений: 0» (ревью кода, R-3).
    proba_dvor "$T/призрак"
    proba_publ "$T/призрак" >/dev/null 2>&1
    TEGI_BYLO=$(git -C "$T/призрак/удалённый.git" tag -l 'v*' | tr '\n' ' ')
    BYLO=$(kommitov "$T/призрак/удалённый.git")
    OUT=$(proba_publ "$T/призрак"); RC=$?
    TEGI_STALO=$(git -C "$T/призрак/удалённый.git" tag -l 'v*' | tr '\n' ' ')
    if [ "$RC" = 0 ] && [ "$(kommitov "$T/призрак/удалённый.git")" = "$BYLO" ] \
       && [ "$TEGI_STALO" = "$TEGI_BYLO" ] \
       && printf '%s' "$OUT" | grep -q 'публиковать нечего'; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: изменений нет — второго релиза не выпущено (теги $TEGI_STALO)"
    else
        echo "  ПЛОХО призрачный релиз: код $RC, теги было «$TEGI_BYLO» стало «$TEGI_STALO», ${OUT:0:90}"; ok=0
    fi

    # БОЛЬНОЙ: в пакете есть что выпускать, а запись простым языком не
    # написана — сверху та же, что ушла прошлым релизом. Отказ замком 6в,
    # наружу второго коммита нет.
    proba_dvor "$T/беззаписи"
    proba_publ "$T/беззаписи" >/dev/null 2>&1
    BYLO=$(kommitov "$T/беззаписи/удалённый.git")
    printf 'ещё\n' > "$T/беззаписи/пакет/новое.txt"
    git -C "$T/беззаписи/пакет" add -A
    git -C "$T/беззаписи/пакет" -c user.name=t -c user.email=t@t commit -qm ещё
    OUT=$(proba_publ "$T/беззаписи"); RC=$?
    if [ "$RC" != 0 ] && [ "$BYLO" = 1 ] && printf '%s' "$OUT" | grep -q 'уже выходила' \
       && [ "$(kommitov "$T/беззаписи/удалённый.git")" = "$BYLO" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: релиз без новой записи простым языком — отказ, наружу не уехало"
    else
        echo "  ПЛОХО релиз без записи не остановлен: код $RC, коммитов было $BYLO, ${OUT:0:120}"; ok=0
    fi

    # ЗАМОК 6в В «--проверка»: заметки и номер выпуска судятся ДО кнопки — правка заметок
    # после «Да» сдвигает пакет и сжигает кнопку.
    # Стенд — копия двери со своим паспортом; судья согласия подставной и оставляет след.
    # Временное стенда — в своём TMPDIR: проба видит, убрано ли оно.
    proba_stend() { # $1=двор $2=тело deploy_guard.py, дальше — доводы двери → вывод и rc
        local d="$1" s="$1/стенд" telo="$2" f
        shift 2
        mkdir -p "$s/scripts" "$d/tmp"
        cp "$SCRIPTS_REAL/publish.sh" "$s/scripts/publish.sh"
        ln -s "$SCRIPTS_REAL/lib" "$s/scripts/lib"
        for f in build-public.py version.py check-release-notes.py; do ln -s "$SCRIPTS_REAL/$f" "$s/scripts/$f"; done
        printf '%s\n' "$telo" > "$s/scripts/deploy_guard.py"
        { cat "$d/паспорт.conf"; printf 'STARTER_DIR="%s"\n' "$d/пакет"; } > "$s/install.conf"
        cp "$d/харнес.conf" "$s/harness.conf"
        TMPDIR="$d/tmp" HARNESS_DOOR_STAND_HOME="$s" bash "$s/scripts/publish.sh" "$@" 2>&1; echo "rc=$?"
    }
    read -r -d '' SUDYA_NET <<'PY' || true
import pathlib, sys
(pathlib.Path(__file__).resolve().parents[2] / "согласие-звали").touch()
print("согласия нет (подставной судья)")
sys.exit(1)
PY
    proba_proverki() { proba_stend "$1" "$SUDYA_NET" --проверка; }
    proba_dvor "$T/номер" "Выпуск 0.2.0: не тот номер"
    OUT=$(proba_proverki "$T/номер")
    if printf '%s' "$OUT" | grep -q 'номер выпуска: v0.1.0' && printf '%s' "$OUT" | grep -q 'ОТКАЗ.*0\.2\.0.*v0\.1\.0' \
       && ! printf '%s' "$OUT" | grep -q 'rc=0' && [ ! -e "$T/номер/согласие-звали" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: --проверка: запись называет не тот номер — красный 6в с обоими номерами, до кнопки"
    else echo "  ПЛОХО номер в заметках не сверен до кнопки: $(printf '%s' "$OUT" | tail -4 | tr '\n' ' ')"; ok=0; fi
    # Манифест прошлого выпуска в общем каталоге сборки — опора строки «меняются
    # личность/стандарты» в просьбе кнопки: ранний суд обязан его не тронуть.
    proba_manifest() { # $1=двор: кладёт манифест «прошлого выпуска» в каталог сборки с меткой
        mkdir -p "$1/сборка/harness/self"
        : > "$1/сборка/.публичная-сборка"
        printf 'прошлый выпуск  harness/self/core.json\n' > "$1/сборка/harness/self/MANIFEST.sha256"
        cp "$1/сборка/harness/self/MANIFEST.sha256" "$1/манифест-до"
    }
    sborka_ne_tronuta() { # $1=двор → 0, если в каталоге сборки только метка и манифест-до, временное убрано
        cmp -s "$1/сборка/harness/self/MANIFEST.sha256" "$1/манифест-до" \
            && [ "$(find "$1/сборка" -type f | wc -l)" = 2 ] && [ -z "$(ls -A "$1/tmp")" ]
    }
    proba_dvor "$T/проверка"
    proba_manifest "$T/проверка"
    OUT=$(proba_proverki "$T/проверка")
    if printf '%s' "$OUT" | grep -q 'номер выпуска: v0.1.0' \
       && printf '%s' "$OUT" | grep -q 'замок 6в · заметки выпуска простым языком есть' \
       && [ -e "$T/проверка/согласие-звали" ]; then
        echo "  ок    --проверка: заметки и номер судятся до кнопки, затем спрашивается согласие"
    else echo "  ПЛОХО --проверка не судит заметки до кнопки: $(printf '%s' "$OUT" | tail -4 | tr '\n' ' ')"; ok=0; fi
    if sborka_ne_tronuta "$T/проверка"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: ранний суд заметок собирает во временном — манифест прошлого выпуска цел, временное убрано"
    else echo "  ПЛОХО ранний суд тронул общий каталог сборки или не убрал временное: $(find "$T/проверка/сборка" "$T/проверка/tmp" -maxdepth 3 | head -5 | tr '\n' ' ')"; ok=0; fi

    # Ранняя сборка упала — отказ сразу, с причиной, до кнопки.
    proba_dvor "$T/несборка"
    printf 'исключить: [\n' > "$T/несборка/пакет/harness/config/public-build.yaml"
    git -C "$T/несборка/пакет" -c user.name=t -c user.email=t@t commit -qam "сломанный конфиг сборки"
    OUT=$(proba_proverki "$T/несборка")
    if printf '%s' "$OUT" | grep -q 'ОТКАЗ: сборка не прошла' && printf '%s' "$OUT" | grep -q 'rc=1' \
       && [ ! -e "$T/несборка/согласие-звали" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: ранняя сборка упала — отказ с причиной до кнопки"
    else echo "  ПЛОХО упавшая ранняя сборка не отказала: $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi

    # БОЕВОЙ ХОД держит замок очереди, пока зовёт ворота: подставной судья согласия сам
    # пробует замок и оставляет потомка, переживающего публикацию. После выхода очередь
    # берёт замок сразу.
    read -r -d '' SUDYA_ZAMOK <<'PY' || true
import fcntl, pathlib, subprocess, sys
двор = pathlib.Path(__file__).resolve().parents[2]
(двор / "согласие-звали").touch()
сирота = subprocess.Popen(["sleep", "30"], start_new_session=True, close_fds=False,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
(двор / "сирота").write_text(str(сирота.pid))
with open(двор / "merge-queue.lock", "a") as замок:
    try:
        fcntl.flock(замок, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print("замок очереди свободен (подставной судья)")
    except BlockingIOError:
        print("замок очереди держится (подставной судья)")
sys.exit(1)
PY
    proba_dvor "$T/боевой"
    proba_manifest "$T/боевой"
    OUT=$(proba_stend "$T/боевой" "$SUDYA_ZAMOK")
    AFTER_RUN=$(queue_takes_lock "$T/боевой" >/dev/null 2>&1; echo $?)
    kill "$(cat "$T/боевой/сирота" 2>/dev/null)" 2>/dev/null
    if printf '%s' "$OUT" | grep -q 'замок очереди держится' && [ "$AFTER_RUN" = 0 ]; then
        echo "  ок    боевой ход: ворота зовутся под замком очереди, после выхода замок свободен при живом потомке"
    else echo "  ПЛОХО боевой ход и замок очереди: take_lock после выхода $AFTER_RUN; $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi
    if sborka_ne_tronuta "$T/боевой"; then
        echo "  ок    боевой ход до кнопки не трогает общий каталог сборки"
    else echo "  ПЛОХО боевой ход тронул каталог сборки до кнопки"; ok=0; fi

    # Предел ожидания очереди не целый — отказ «не настроено», а не синтаксическая ошибка bash.
    proba_dvor "$T/предел"
    printf 'MERGE_QUEUE_WAIT_MIN="0.5"\n' >> "$T/предел/харнес.conf"
    OUT=$(proba_stend "$T/предел" "$SUDYA_NET")
    if printf '%s' "$OUT" | grep -q 'MERGE_QUEUE_WAIT_MIN=«0.5».*целое число минут' && printf '%s' "$OUT" | grep -q 'rc=2' \
       && ! printf '%s' "$OUT" | grep -qi 'syntax\|синтакс' && [ ! -e "$T/предел/согласие-звали" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: дробный предел ожидания очереди — понятный отказ, кнопку не просили"
    else echo "  ПЛОХО дробный предел очереди: $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi

    # ── ПОЕЗД ВЫПУСКОВ: ворота судят разницу с прошлым выпуском, согласие и публикация — срез ──
    # Двор поезда — git-проект с двумя проверками: «правленая» (её файл в диапазоне) и «старая
    # красная» (красная, файл вне диапазона). Выбор считает НАСТОЯЩИЙ select-checks.py по
    # подставному gates.sh; ядро — подставной скрипт, код берёт из файла двора.
    proba_poezd_dvor() { # $1=двор → проект: прошлый выпуск, правка a.sh, документ, правка old-red.sh
        local d="$1" p="$1/проект"
        mkdir -p "$p/scripts" "$p/harness/config" "$p/docs" "$d/лог"
        # Пути двора — абсолютные: ворота идут во временном дереве проекта, а не в нём самом.
        { printf 'D="%s"\n' "$d"; cat <<'SH'
    echo "gates $*" >> "$D/звали"
    ONLY="${2:-}"; GREEN=0; RED=0
    cd "$(dirname "$0")/.." || exit 2
    проверка() {
        local n="$1"; shift
        case "|$ONLY|" in *"|$n|"*) ;; *) return ;; esac
        if "$@" > /dev/null 2>&1; then GREEN=$((GREEN + 1)); else RED=$((RED + 1)); printf '  ПЛОХО · %s (код 1)\n' "$n"; fi
    }
    проверка "правленая" bash scripts/a.sh
    проверка "старая красная" bash scripts/old-red.sh
    echo "=== итог: зелёных $GREEN, красных $RED ==="
    # Просроченный срок gates-quarantine.py судит как беду карантина: строки «карантин ·» нет, код 1.
    if [ -e "$D/карантин-просрочен" ] && [ "$RED" != 0 ]; then
        echo "  ПЛОХО · карантин: старая красная — срок карточки hr.proba истёк 2000-01-01"; exit 1
    fi
    if [ -e "$D/карантин" ] && [ "$RED" != 0 ]; then
        echo "  карантин · старая красная — красный, карточка hr.proba, до 2099-01-01"; exit 0
    fi
    # Беда карантина: красная названа под карточкой, а судья всё равно вернул ненулевой код.
    if [ -e "$D/карантин-беда" ] && [ "$RED" != 0 ]; then
        echo "  карантин · старая красная — красный, карточка hr.proba, до 2099-01-01"; exit 1
    fi
    [ "$RED" = 0 ]
SH
        } > "$p/scripts/gates.sh"
        printf '[ -e "%s/медленно" ] && sleep 30\n[ -f "%s/по-паспорту" ] && { bash "%s/по-паспорту" || exit 1; }\nexit 0\n' \
            "$d" "$d" "$d" > "$p/scripts/a.sh"
        printf 'exit 1\n' > "$p/scripts/old-red.sh"
        printf 'echo "ядро $*" >> "%s/звали"\n' "$d" > "$p/scripts/core.sh"
        printf '[ -f "%s/очередь-путь" ] && { flock -n "$(cat "%s/очередь-путь")" true; echo "очередь во время ворот: $?" >> "%s/звали"; }\n' \
            "$d" "$d" "$d" >> "$p/scripts/core.sh"
        printf '[ -f "%s/поезд-путь" ] && { flock -n "$(cat "%s/поезд-путь")" true; echo "поезд во время ворот: $?" >> "%s/звали"; }\n' \
            "$d" "$d" "$d" >> "$p/scripts/core.sh"
        printf '[ -f "%s/во-время-ядра" ] && bash "%s/во-время-ядра"\n[ -e "%s/ядро-медленно" ] && sleep 30\n' \
            "$d" "$d" "$d" >> "$p/scripts/core.sh"
        printf '[ -f "%s/по-паспорту" ] && { bash "%s/по-паспорту" || exit 1; }\n' "$d" "$d" >> "$p/scripts/core.sh"
        printf 'exit "$(cat "%s/ядро-код" 2>/dev/null || echo 0)"\n' "$d" >> "$p/scripts/core.sh"
        printf 'ядро:\n  - имя: секреты по диапазону\n    команда: "bash scripts/core.sh {прошлый}..{срез}"\nядро_потолок_мин: 0.05\nвыбор: { потолок_мин: 0.05 }\n' \
            > "$p/harness/config/release-train.yaml"
        printf 'механизмы: []\n' > "$p/harness/config/mechanisms.yaml"
        ln -s "$SCRIPTS_REAL/select-checks.py" "$p/scripts/select-checks.py"
        # Подставной сборщик карточек: пишет, изолирован ли питон и куда легли карточки.
        printf 'import sys\nwith open("%s/звали", "a", encoding="utf-8") as f:\n    f.write(f"карточки isolated={sys.flags.isolated} {chr(32).join(sys.argv[1:])}\\n")\n' \
            "$d" > "$p/scripts/self-prompt.py"
        git -C "$p" init -q -b master
        git -C "$p" add -A && git -C "$p" -c user.name=t -c user.email=t@t commit -qm "прошлый выпуск"
        printf '# правка\n' >> "$p/scripts/a.sh"
        git -C "$p" -c user.name=t -c user.email=t@t commit -qam "правка a.sh"
        printf 'документ\n' > "$p/docs/readme.md"
        git -C "$p" add -A && git -C "$p" -c user.name=t -c user.email=t@t commit -qm "документ"
        printf '# правка\n' >> "$p/scripts/old-red.sh"
        git -C "$p" -c user.name=t -c user.email=t@t commit -qam "правка old-red.sh"
    }
    proba_poezd_vorota() { # $1=двор $2=прошлый $3=срез → вывод замка 1, rc и состояние поезда
        rm -f "$1/звали"
        ( PROJECT_DIR="$1/проект"; TRAIN_PREV_PROJECT="$2"; TRAIN_SLICE_PROJECT="$3"
          train_gates "$1/лог" 2>&1; echo "rc=$? состояние=${TRAIN_STATE:-} ворота=${TRAIN_GATES:-} ядро=${TRAIN_CORE:-} красных=${TRAIN_RED:-} карантин=${TRAIN_QUARANTINED:-}: ${TRAIN_QUARANTINED_NAMES:-}" )
    }
    PD="$T/поезд"
    proba_poezd_dvor "$PD"
    S0=$(git -C "$PD/проект" rev-parse HEAD~3); S1=$(git -C "$PD/проект" rev-parse HEAD~2)
    S2=$(git -C "$PD/проект" rev-parse HEAD~1); S3=$(git -C "$PD/проект" rev-parse HEAD)
    OUT=$(proba_poezd_vorota "$PD" "$S0" "$S2")
    if printf '%s' "$OUT" | grep -q 'rc=0 состояние= ворота=зелёные' \
       && printf '%s' "$OUT" | grep -q "замок 1 · ворота поезда: диапазон ${S0:0:8}..${S2:0:8}, выбрано 1, красных 0, ядро зелёное, секунд [0-9]" \
       && grep -qx 'gates --только правленая --всё' "$PD/звали" && grep -qx "ядро $S0..$S2" "$PD/звали"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — красное вне диапазона не останавливает, выбор по диапазону и ядро"
    else echo "  ПЛОХО поезд: красное вне диапазона: $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ') | $(tr '\n' ' ' < "$PD/звали" 2>/dev/null)"; ok=0; fi
    if grep -qx "карточки isolated=1 --карточки --в $PD/проект/.claude/worktrees/release-train-.*/.claude/agents" "$PD/звали"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — карточки агентов пересобраны в дереве среза, сборщик позван с python3 -I"
    else echo "  ПЛОХО поезд: карточки в дереве среза: $(grep '^карточки' "$PD/звали" 2>/dev/null | tr '\n' ' ')"; ok=0; fi
    echo 1 > "$PD/ядро-код"
    OUT=$(proba_poezd_vorota "$PD" "$S0" "$S2")
    rm -f "$PD/ядро-код"
    if printf '%s' "$OUT" | grep -q 'rc=1 состояние=красное ядро' && printf '%s' "$OUT" | grep -q 'ОТКАЗ: красное ядро — «секреты по диапазону»' \
       && ! grep -q '^gates' "$PD/звали"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — красное ядро останавливает, выбор не гоняется"
    else echo "  ПЛОХО поезд: красное ядро: $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi
    touch "$PD/медленно"
    OUT=$(proba_poezd_vorota "$PD" "$S0" "$S2")
    rm -f "$PD/медленно"
    if printf '%s' "$OUT" | grep -q 'rc=1 состояние=не дошли' && printf '%s' "$OUT" | grep -q 'ворота поезда не дошли' \
       && ! printf '%s' "$OUT" | grep -q 'красные в выборе'; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — выбор дольше потолка: «не дошли», а не «красные» и не «зелёные»"
    else echo "  ПЛОХО поезд: выбор дольше потолка: $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi
    OUT=$(proba_poezd_vorota "$PD" "$S2" "$S3")
    if printf '%s' "$OUT" | grep -q 'rc=1 состояние=красный выбор' && printf '%s' "$OUT" | grep -q 'красные в выборе: старая красная'; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — красное в диапазоне останавливает срез с именем проверки"
    else echo "  ПЛОХО поезд: красное в диапазоне: $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi
    # Красная под годной карточкой карантина срез не держит, но и не прячется: имя — в замке 1,
    # в записи хода и в просьбе кнопки. Новая и просроченная красные по-прежнему стоят.
    touch "$PD/карантин"
    OUT=$(proba_poezd_vorota "$PD" "$S2" "$S3")
    rm -f "$PD/карантин"
    if printf '%s' "$OUT" | grep -q 'rc=0 состояние= ворота=зелёные' \
       && printf '%s' "$OUT" | grep -q 'красных 0, в карантине 1: старая красная, ядро' \
       && printf '%s' "$OUT" | grep -q 'красных=0 карантин=1: старая красная$' \
       && ! printf '%s' "$OUT" | grep -q 'ОТКАЗ'; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — красная в карантине срез не держит, но названа: замок 1 и поля хода"
    else echo "  ПЛОХО поезд: красная в карантине: $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi
    # Код ворот ≠ 0 при всех красных под карантином — беда самого карантина, а не «чисто»: ход отказывает.
    touch "$PD/карантин-беда"
    OUT=$(proba_poezd_vorota "$PD" "$S2" "$S3")
    rm -f "$PD/карантин-беда"
    if printf '%s' "$OUT" | grep -q 'rc=1 состояние=красный выбор' \
       && printf '%s' "$OUT" | grep -q 'красные в выборе: код ворот без имён' \
       && printf '%s' "$OUT" | grep -q 'красных=0 карантин=1: старая красная$'; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — все красные в карантине, но код ворот ненулевой: ход отказывает"
    else echo "  ПЛОХО поезд: беда карантина: $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi
    touch "$PD/карантин-просрочен"
    OUT=$(proba_poezd_vorota "$PD" "$S2" "$S3")
    rm -f "$PD/карантин-просрочен"
    if printf '%s' "$OUT" | grep -q 'rc=1 состояние=красный выбор' \
       && printf '%s' "$OUT" | grep -q 'красные в выборе: старая красная — срез не едет' \
       && printf '%s' "$OUT" | grep -q 'красных=1 карантин=0:'; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — красная с просроченным карантином останавливает срез"
    else echo "  ПЛОХО поезд: просроченный карантин: $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi
    # Имена карантина — в просьбе кнопки.
    ASKTXT=$(python3 -I - "$SCRIPTS_REAL" 2>&1 <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import deploy_guard
print(deploy_guard._почему("публикацию проверенного", ["старая красная", "ещё одна"]))
print(deploy_guard._почему("публикацию проверенного", []))
PY
)
    if [ "$(printf '%s\n' "$ASKTXT" | sed -n 1p)" = "скрипт просит: публикацию проверенного; в карантине 2: старая красная, ещё одна" ] \
       && [ "$(printf '%s\n' "$ASKTXT" | sed -n 2p)" = "скрипт просит: публикацию проверенного" ] \
       && [ "$( TRAIN_SLICE_PACKAGE=abc TRAIN_QUARANTINED=2 TRAIN_QUARANTINED_NAMES="старая красная, ещё одна"
                train_guard_args; printf '%s|' "${TRAIN_GUARD_ARGS[@]}" )" = "--срез|abc|--в-карантине|старая красная, ещё одна|" ] \
       && [ "$( TRAIN_SLICE_PACKAGE=abc TRAIN_QUARANTINED=0 train_guard_args; printf '%s|' "${TRAIN_GUARD_ARGS[@]}" )" = "--срез|abc|" ]; then
        echo "  ок    поезд — просьба кнопки называет карантинные красные («в карантине K: имена»)"
    else echo "  ПЛОХО просьба кнопки не называет карантин: $(printf '%s' "$ASKTXT" | tail -3 | tr '\n' ' ')"; ok=0; fi
    OUT=$(proba_poezd_vorota "$PD" "$S1" "$S2")
    if printf '%s' "$OUT" | grep -q 'rc=0' && printf '%s' "$OUT" | grep -q 'выбрано 0, красных 0' \
       && ! grep -q '^gates' "$PD/звали"; then
        echo "  ок    поезд — диапазон без задетых проверок: «выбрано 0», ворота не гоняются"
    else echo "  ПЛОХО поезд: пустой выбор: $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi
    touch "$PD/ядро-медленно"
    OUT=$(proba_poezd_vorota "$PD" "$S0" "$S2")
    rm -f "$PD/ядро-медленно"
    if printf '%s' "$OUT" | grep -q 'rc=1 состояние=не дошли ворота=не дошли ядро=не дошло: секреты по диапазону' \
       && ! grep -q '^gates' "$PD/звали"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — ядро дольше потолка: «не дошли», выбор не гоняется"
    else echo "  ПЛОХО поезд: ядро дольше потолка: $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi

    # Ворота судят СРЕЗ: очередь сливает в живое дерево проекта, пока они идут, и вердикт среза
    # от этого не меняется ни в красный (поломка после среза), ни в зелёный (починка после среза).
    PK="$T/поезд-срез"
    proba_poezd_dvor "$PK"
    K0=$(git -C "$PK/проект" rev-parse HEAD~3); K2=$(git -C "$PK/проект" rev-parse HEAD~1); K3=$(git -C "$PK/проект" rev-parse HEAD)
    proba_vlit() { # $1=файл скрипта $2=код выхода → во время ядра очередь вливает его в живое дерево проекта
        printf 'printf "exit %s\\n" > "%s/проект/scripts/%s"\ngit -C "%s/проект" -c user.name=q -c user.email=q@q commit -qam "очередь: %s"\n' \
            "$2" "$PK" "$1" "$PK" "$1" > "$PK/во-время-ядра"
    }
    proba_vlit a.sh 1
    OUT=$(proba_poezd_vorota "$PK" "$K0" "$K2")
    VLITO=$(git -C "$PK/проект" log -1 --format=%s)
    if printf '%s' "$OUT" | grep -q 'rc=0 состояние= ворота=зелёные' && [ "$VLITO" = "очередь: a.sh" ] \
       && grep -qx 'gates --только правленая --всё' "$PK/звали"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — поломка, влитая в проект во время ворот, не красит срез"
    else echo "  ПЛОХО поезд: поломка после среза судилась: «$VLITO» $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi
    proba_vlit old-red.sh 0
    OUT=$(proba_poezd_vorota "$PK" "$K2" "$K3")
    rm -f "$PK/во-время-ядра"
    DEREVYA=$(git -C "$PK/проект" worktree list --porcelain | grep -c '^worktree ')
    if printf '%s' "$OUT" | grep -q 'rc=1 состояние=красный выбор' && printf '%s' "$OUT" | grep -q 'красные в выборе: старая красная' \
       && [ "$(git -C "$PK/проект" log -1 --format=%s)" = "очередь: old-red.sh" ] && [ "$DEREVYA" = 1 ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — починка, влитая после среза, не делает срез зелёным; дерево среза убрано"
    else echo "  ПЛОХО поезд: починка после среза судилась (деревьев $DEREVYA): $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi

    # Проверка, читающая проект и пакет по паспорту, судит срез: поломка, влитая очередью в живые
    # проект и пакет во время ядра, не красит ни ядро, ни выбор. Места спрашиваются у НАСТОЯЩИХ
    # загрузчиков (konf_zagruzit, конфиг(), katalog_paketa()) и у искателей пакета проверок ворот:
    # паспорт стенда называет живое, и только окружение поезда, старшее файла, ведёт их на срез.
    PP="$T/поезд-паспорт"
    proba_poezd_dvor "$PP"
    P0=$(git -C "$PP/проект" rev-parse HEAD~3); P2=$(git -C "$PP/проект" rev-parse HEAD~1)
    mkdir -p "$PP/пакет" "$PP/вид-пакета"
    touch "$PP/пакет/USTANOVIT.sh" "$PP/вид-пакета/USTANOVIT.sh"
    printf 'PROJECT_DIR="%s/проект"\n' "$PP" > "$PP/install.conf"
    printf 'STARTER_DIR="%s/пакет"\n' "$PP" > "$PP/harness.conf"
    cat > "$PP/по-паспорту" <<SH
export HARNESS_INSTALL_CONF="$PP/install.conf" HARNESS_CONF="$PP/harness.conf"
source "$SCRIPTS_REAL/lib/config.sh" && konf_zagruzit || { echo "по паспорту: konf_zagruzit упал"; exit 1; }
sudili=\$(python3 -I - "$SCRIPTS_REAL" <<'PY'
import importlib.util, sys
from pathlib import Path
scripts = Path(sys.argv[1])
sys.path.insert(0, str(scripts / "lib"))
import config
def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), scripts / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
print(config.конфиг()["PROJECT_DIR"])
print(config.katalog_paketa())
print(load("compare-with-package").найти_пакет()[0])
print(load("check-stack-registry").installer_candidates()[0])
print(Path(load("check-cyrillic-in-names").файлы_пакета()[0]).parent)
PY
) || { echo "по паспорту: загрузчик упал"; exit 1; }
n=0
while IFS= read -r where; do
    n=\$((n + 1))
    [ ! -e "\$where/сломано" ] || { echo "по паспорту: живое \$where"; exit 1; }
done < <(printf '%s\n' "\$PROJECT_DIR" "\$sudili")
[ "\$n" = 6 ] || { echo "по паспорту: спрошено мест \$n из 6"; exit 1; }
SH
    printf 'touch "%s/проект/сломано" "%s/пакет/сломано"\ngit -C "%s/проект" add сломано\ngit -C "%s/проект" -c user.name=q -c user.email=q@q commit -qm "очередь: сломано"\n' \
        "$PP" "$PP" "$PP" "$PP" > "$PP/во-время-ядра"
    OUT=$(PACKAGE_VIEW="$PP/вид-пакета" proba_poezd_vorota "$PP" "$P0" "$P2")
    if printf '%s' "$OUT" | grep -q 'rc=0 состояние= ворота=зелёные' && [ -e "$PP/пакет/сломано" ] \
       && [ "$(git -C "$PP/проект" log -1 --format=%s)" = "очередь: сломано" ] && grep -qx 'gates --только правленая --всё' "$PP/звали"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — проверка по паспорту и по каталогу пакета судит срез: поломка, влитая в живые проект и пакет во время ворот, не красит срез"
    else echo "  ПЛОХО поезд: проверка по паспорту судила живое: $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ') | $(tr '\n' ' ' < "$PP/звали" 2>/dev/null)"; ok=0; fi

    # Согласие — на СРЕЗ пакета: пакет ушёл вперёд после кнопки, а публикуется срез, не HEAD.
    proba_dvor "$T/срез"
    proba_publ "$T/срез" > /dev/null 2>&1
    mkdir -p "$T/срез/пакет/harness/demons"
    printf 'срез\n' > "$T/срез/пакет/harness/demons/srez.sh"
    proba_zapis "$T/срез" "Выпуск 0.2.0: срез"
    git -C "$T/срез/пакет" add -A && git -C "$T/срез/пакет" -c user.name=t -c user.email=t@t commit -qm "срез"
    SREZ=$(git -C "$T/срез/пакет" rev-parse HEAD)
    PREDMET_SREZA=$(python3 -I - "$SCRIPTS_REAL/lib" "$T/срез/пакет" "$T/срез/сборка" "$SREZ" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import door_subjects
print(door_subjects.publish_subject(sys.argv[2], sys.argv[3], "jig", "master", sys.argv[4]))
PY
)
    printf 'после\n' > "$T/срез/пакет/posle-sreza.txt"
    git -C "$T/срез/пакет" add -A && git -C "$T/срез/пакет" -c user.name=t -c user.email=t@t commit -qm "после среза"
    OUT=$( ( PACKAGE_DIR="$T/срез/пакет"; CLEANUP_DIRS=(); LOG_DIR="$T/срез"
             TRAIN_STATE="проверка"; TRAIN_GATES="зелёные"; TRAIN_SLICE_PACKAGE="$SREZ"; train_record "$LOG_DIR" 0
             train_package_view "$SREZ" || exit 1
             PAKET_SHA="$SREZ" PREDMET_ZAMKA="$PREDMET_SREZA" proba_publ "$T/срез"; rc=$?
             rm -rf "$PACKAGE_VIEW"; exit "$rc" ) 2>&1 ); RC=$?
    OPUBL=$(git -C "$T/срез/удалённый.git" -c core.quotepath=false ls-tree -r --name-only master 2>/dev/null)
    SOOBSH=$(git -C "$T/срез/удалённый.git" log -1 --format=%s master 2>/dev/null)
    if [ "$RC" = 0 ] && [ -n "$PREDMET_SREZA" ] && printf '%s\n' "$OPUBL" | grep -qx 'harness/demons/srez.sh' \
       && ! printf '%s\n' "$OPUBL" | grep -q 'posle-sreza.txt' \
       && printf '%s' "$SOOBSH" | grep -q "$(git -C "$T/срез/пакет" rev-parse --short "$SREZ")"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — согласие на срез переживает сдвиг пакета, опубликован срез, а не HEAD"
    else echo "  ПЛОХО поезд: согласие на срез: код $RC, «$SOOBSH», $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi

    # Прошлый выпуск: без записи поезда — коммит проекта по времени тега; с записью — по записи.
    proba_proshlyj() { # $1=каталог журнала → «тег проект связь»
        ( PROJECT_DIR="$PD/проект"; PUBLIC_BUILD_DIR="$T/срез/сборка"; PUBLIC_REMOTE=jig; PUBLIC_BRANCH=master
          train_previous_release "$1" && echo "$TRAIN_PREV_TAG $TRAIN_PREV_PROJECT $TRAIN_PREV_LINK" )
    }
    TEG_SREZA=$(git -C "$T/срез/сборка" describe --tags --abbrev=0 jig/master 2>/dev/null)
    mkdir -p "$T/журнал-поезда"
    PO_VREMENI=$(proba_proshlyj "$T/журнал-поезда")
    ( TRAIN_STATE="выпущен"; RELEASE_TAG="$TEG_SREZA"; TRAIN_SLICE_PROJECT="$S1"; TRAIN_SLICE_PACKAGE="$SREZ"
      train_record "$T/журнал-поезда" 0 )
    PO_ZAPISI=$(proba_proshlyj "$T/журнал-поезда")
    if [ -n "$TEG_SREZA" ] && [ "$PO_VREMENI" = "$TEG_SREZA $S3 связь по времени" ] \
       && [ "$PO_ZAPISI" = "$TEG_SREZA $S1 запись" ]; then
        echo "  ок    поезд — прошлый выпуск: по записи release-train.jsonl, без неё — по времени тега с пометкой"
    else echo "  ПЛОХО поезд: прошлый выпуск: по времени «$PO_VREMENI», по записи «$PO_ZAPISI» (тег «$TEG_SREZA»)"; ok=0; fi

    # Замок очереди у поезда — только на срез: ворота судят срез, и слияния в это время идут.
    proba_posadka() { # $1=предел ожидания замка, с [$2=каталог журнала поезда] [$3=sha пакета из довода] [$4=проект] → вывод и rc
        ( PROJECT_DIR="$PD/проект"; PACKAGE_DIR="$T/срез/пакет"; PUBLIC_BUILD_DIR="$T/срез/сборка"
          PUBLIC_REMOTE=jig; PUBLIC_BRANCH=master; CLEANUP_DIRS=(); TRAIN_SLICE_PROJECT=""
          PROJECT_DIR="${4:-$PROJECT_DIR}"
          train_board "${2:-$T/журнал-поезда}" "${3:-}" "$1" 2>&1; echo "rc=$?"
          release_train_lock; rm -rf "${CLEANUP_DIRS[@]}" )
    }
    printf '%s\n' "$T/журнал-поезда/$MERGE_QUEUE_LOCK_NAME" > "$PD/очередь-путь"
    printf '%s\n' "$T/журнал-поезда/release-train.lock" > "$PD/поезд-путь"
    rm -f "$PD/звали"
    OUT=$(proba_posadka 5)
    rm -f "$PD/поезд-путь"
    if grep -qx 'очередь во время ворот: 0' "$PD/звали" 2>/dev/null && printf '%s' "$OUT" | grep -q 'замок очереди слияния взят' \
       && printf '%s' "$OUT" | grep -q "срез: проект $S3"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — замок очереди взят на срез и отпущен: во время ворот очередь берёт замок"
    else echo "  ПЛОХО поезд: замок очереди во время ворот: $(tr '\n' ' ' < "$PD/звали" 2>/dev/null) | $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi
    if grep -qx 'поезд во время ворот: 1' "$PD/звали" 2>/dev/null; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — замок поезда держится и во время ворот"
    else echo "  ПЛОХО поезд: замок поезда во время ворот не держится: $(tr '\n' ' ' < "$PD/звали" 2>/dev/null)"; ok=0; fi
    rm -f "$PD/звали"
    flock -x "$T/журнал-поезда/$MERGE_QUEUE_LOCK_NAME" sleep 4 &
    HOLDER_PID=$!
    sleep 1
    OUT=$(proba_posadka 1)
    wait "$HOLDER_PID"
    if printf '%s' "$OUT" | grep -q 'rc=1' && printf '%s' "$OUT" | grep -q 'очередь слияния занята' \
       && ! printf '%s' "$OUT" | grep -q 'срез: проект' && ! grep -q '^ядро' "$PD/звали" 2>/dev/null; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — очередь сливает: срез не режется, пока пара проект/пакет движется"
    else echo "  ПЛОХО поезд: срез при занятой очереди: $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi
    rm -f "$PD/очередь-путь"

    # Второй ход поезда при идущем первом — отказ сразу: ни среза, ни замка очереди, ни ворот,
    # и дерево среза первого хода цело: подметание идёт только под замком поезда.
    mkdir -p "$T/журнал-замок"
    ZHIVOE="$PD/проект/.claude/worktrees/release-train-живое"
    git -C "$PD/проект" worktree add -q --detach "$ZHIVOE" HEAD > /dev/null 2>&1
    flock -x "$T/журнал-замок/release-train.lock" sleep 4 &
    HOLDER_PID=$!
    sleep 1
    rm -f "$PD/звали"
    OUT=$(proba_posadka 5 "$T/журнал-замок")
    wait "$HOLDER_PID"
    if printf '%s' "$OUT" | grep -q 'rc=1' && printf '%s' "$OUT" | grep -q 'поезд уже идёт' \
       && ! printf '%s' "$OUT" | grep -q 'срез: проект' && ! printf '%s' "$OUT" | grep -q 'замок очереди слияния взят' \
       && ! grep -q '^ядро' "$PD/звали" 2>/dev/null && [ -e "$ZHIVOE/.git" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — второй ход при идущем первом: отказ «поезд уже идёт» до среза, дерево первого хода цело"
    else echo "  ПЛОХО поезд: второй ход при занятом замке поезда (дерево первого хода: $([ -e "$ZHIVOE/.git" ] && echo цело || echo СНЯТО)): $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi
    git -C "$PD/проект" worktree remove --force "$ZHIVOE" > /dev/null 2>&1

    # Ход, убитый KILL, оставляет дерево среза — и зарегистрированным, и с регистрацией без
    # каталога: следующий ход под замком поезда снимает оба. Чужое рядом цело: дерево помощника
    # с незакоммиченной работой и обычный каталог под именем release-train-* с файлом (И-1).
    OSTAVLENO="$PD/проект/.claude/worktrees/release-train-ostavleno"
    USHLO="$PD/проект/.claude/worktrees/release-train-ushlo"
    CHUZHOE="$PD/проект/.claude/worktrees/agent-чужое"
    NE_DEREVO="$PD/проект/.claude/worktrees/release-train-не-дерево"
    git -C "$PD/проект" worktree add -q --detach "$OSTAVLENO" HEAD > /dev/null 2>&1
    git -C "$PD/проект" worktree add -q --detach "$USHLO" HEAD > /dev/null 2>&1
    git -C "$PD/проект" worktree add -q --detach "$CHUZHOE" HEAD > /dev/null 2>&1
    printf 'работа помощника\n' > "$CHUZHOE/незакоммиченное.txt"
    mkdir -p "$NE_DEREVO" && printf 'данные\n' > "$NE_DEREVO/файл.txt"
    rm -rf "$USHLO"
    DO_HODA=$(git -C "$PD/проект" worktree list --porcelain | grep -c '^worktree ')
    mkdir -p "$T/журнал-сироты"
    OUT=$(proba_posadka 5 "$T/журнал-сироты")
    DEREVYA=$(git -C "$PD/проект" worktree list --porcelain | grep -c '^worktree ')
    if [ "$DO_HODA" = 4 ] && [ ! -e "$OSTAVLENO" ] && [ "$DEREVYA" = 2 ] \
       && [ "$(ls -A "$PD/проект/.claude/worktrees" | sort | tr '\n' ' ')" = "agent-чужое release-train-не-дерево " ] \
       && [ -f "$CHUZHOE/незакоммиченное.txt" ] && [ -f "$NE_DEREVO/файл.txt" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — дерево среза, оставленное убитым ходом, снято следующим ходом; чужое дерево и каталог с файлом целы"
    else echo "  ПЛОХО поезд: подметание деревьев (до хода $DO_HODA, после $DEREVYA): $(ls -A "$PD/проект/.claude/worktrees" | tr '\n' ' ') | $(printf '%s' "$OUT" | tail -2 | tr '\n' ' ')"; ok=0; fi
    git -C "$PD/проект" worktree remove --force "$CHUZHOE" > /dev/null 2>&1
    rm -rf "$NE_DEREVO"

    # Журнал поезда не охраняется: его значения — только коммиты проекта, иначе отказ до git и до bash -c.
    VNEDRENO="$T/внедрено"
    HEAD_PAKETA=$(git -C "$T/срез/пакет" rev-parse HEAD)
    proba_podlog() { # $1=каталог журнала $2=json строки журнала $3=sha пакета из довода → вывод посадки
        mkdir -p "$1" && printf '{"v": 1, "event": "train.step", "data": %s}\n' "$2" > "$1/release-train.jsonl"
        rm -f "$PD/звали"
        proba_posadka 5 "$1" "$3"
    }
    for PODLOG in "{\"срез_пакета\": \"$HEAD_PAKETA\", \"срез_проекта\": \"--output=$VNEDRENO\"}|$HEAD_PAKETA|срез проекта «--output=…»" \
                  "{\"состояние\": \"выпущен\", \"тег\": \"$TEG_SREZA\", \"срез_проекта\": \"x;touch $VNEDRENO;\"}||прошлый выпуск «x;touch …;»" \
                  "{\"состояние\": \"выпущен\", \"тег\": \"$TEG_SREZA\", \"срез_проекта\": \"$(printf '0%.0s' {1..40})\"}||прошлый выпуск — 40 нулей, не коммит" \
                  "{\"состояние\": \"выпущен\", \"тег\": \"$TEG_SREZA\", \"срез_проекта\": \"HEAD\"}||прошлый выпуск — имя ссылки, а не sha"; do
        IFS='|' read -r STROKA DOVOD CHTO <<< "$PODLOG"
        OUT=$(proba_podlog "$T/журнал-подлог-$RANDOM" "$STROKA" "$DOVOD")
        if printf '%s' "$OUT" | grep -q 'rc=1' && printf '%s' "$OUT" | grep -q 'не коммит проекта' \
           && [ ! -e "$VNEDRENO" ] && ! grep -q '^ядро' "$PD/звали" 2>/dev/null; then
            echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — подлог журнала ($CHTO): отказ, команда не исполнена"
        else echo "  ПЛОХО поезд: подлог журнала ($CHTO): $([ -e "$VNEDRENO" ] && echo 'ВНЕДРЕНО') $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi
        rm -f "$VNEDRENO"
    done

    # Вердикт ворот виден двери в ТОМ ЖЕ ходе: пакет ушёл за время ворот, а кнопка просится на срез.
    mkdir -p "$T/журнал-в2"
    # Прошлый выпуск — до починки old-red.sh: диапазон задевает только её, и ворота зелёные.
    printf '{"v": 1, "event": "train.step", "data": {"состояние": "выпущен", "тег": "%s", "срез_проекта": "%s"}}\n' "$TEG_SREZA" \
        "$(git -C "$PK/проект" rev-parse HEAD~1)" > "$T/журнал-в2/release-train.jsonl"
    git -C "$T/срез/пакет" -c user.name=t -c user.email=t@t commit --allow-empty -qm "срез хода"
    SREZ_HODA=$(git -C "$T/срез/пакет" rev-parse HEAD)
    printf 'git -C "%s" -c user.name=q -c user.email=q@q commit --allow-empty -qm "слияние во время ворот"\n' "$T/срез/пакет" > "$PK/во-время-ядра"
    OUT=$(proba_posadka 5 "$T/журнал-в2" "" "$PK/проект")
    rm -f "$PK/во-время-ядра"
    PREDMET_HODA=$(python3 -I - "$SCRIPTS_REAL/lib" "$T/срез/пакет" "$T/срез/сборка" "$SREZ_HODA" "$T/журнал-в2/release-train.jsonl" 2>&1 <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import door_subjects
print(door_subjects.publish_subject(sys.argv[2], sys.argv[3], "jig", "master", sys.argv[4], sys.argv[5]))
PY
)
    if printf '%s' "$OUT" | grep -q 'rc=0' && [ "${PREDMET_HODA%% *}" = "$SREZ_HODA" ] \
       && [ "$(git -C "$T/срез/пакет" rev-parse HEAD)" != "$SREZ_HODA" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: поезд — пакет ушёл за время ворот: кнопка в том же ходе просится на срез"
    else echo "  ПЛОХО поезд: дверь не видит зелёных ворот хода: «${PREDMET_HODA:0:160}» $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"; ok=0; fi

    # Каждый ход поезда оставляет строку в release-train.jsonl — и оборванный отказом.
    mkdir -p "$T/журнал-хода"
    ( CLEANUP_DIRS=(); TRAIN=1; LOG_DIR="$T/журнал-хода"; TRAIN_STATE="красное ядро"
      TRAIN_SLICE_PROJECT="$S2"; TRAIN_SLICE_PACKAGE="$SREZ"; TRAIN_PREV_TAG="v0.1.0"; TRAIN_PREV_PROJECT="$S0"
      TRAIN_GATES="красный выбор"; TRAIN_RED=1; TRAIN_RED_NAMES="новая красная"; TRAIN_QUARANTINED=1; TRAIN_QUARANTINED_NAMES="старая красная"
      trap cleanup_on_exit EXIT; exit 1 ) > /dev/null 2>&1
    python3 -I - "$T/журнал-хода/release-train.jsonl" "$S2" "$SREZ" "$S0" <<'PY'
import json, sys
try:
    строка = json.loads(open(sys.argv[1], encoding="utf-8").read().splitlines()[-1])
    з = строка["data"]
except (OSError, ValueError, IndexError, KeyError, TypeError):
    sys.exit(1)
# Строка — событие events.emit: поля хода в data, время и исход в корне.
годна = (строка.get("event") == "train.step" and строка.get("outcome") == "refused" and bool(строка.get("ts"))
         and з.get("состояние") == "красное ядро" and з.get("код") == 1 and з.get("срез_проекта") == sys.argv[2]
         and з.get("срез_пакета") == sys.argv[3] and з.get("прошлый_тег") == "v0.1.0"
         and з.get("прошлый_sha_проекта") == sys.argv[4] and "секунды" in з
         and з.get("ворота") == "красный выбор" and з.get("красных") == 1
         and з.get("красные") == "новая красная" and з.get("в_карантине") == 1
         and з.get("в_карантине_имена") == "старая красная")
sys.exit(0 if годна else 1)
PY
    if [ $? = 0 ]; then
        echo "  ок    поезд — ход, оборванный отказом, записан в release-train.jsonl: состояние, код, срезы, прошлый выпуск"
    else echo "  ПЛОХО поезд: строки хода нет или она неполна: $(tail -1 "$T/журнал-хода/release-train.jsonl" 2>/dev/null | cut -c1-200)"; ok=0; fi
    rm -rf "$T"

    PUTEJ=$(grep -c '^\s*echo "  ок    ' "$0" || true)
    [ "$ok" = 1 ] && { echo "САМОТЕСТ ПРОЙДЕН: $PUTEJ путей, первым — больной случай"; exit 0; }
    echo "САМОТЕСТ ПРОВАЛЕН"; exit 1
fi

# Код 2 = «не настроено», код 1 = «замок отказал». Разные вещи: первое чинит
# установка, второе — владелец. «${X:?}» даёт 1 на оба случая, поэтому явно.
if [ -z "${PUBLIC_BRANCH:-}" ] || [ -z "${PUBLIC_REMOTE:-}" ]    || [ -z "${PUBLIC_BUILD_DIR:-}" ] || [ -z "${PUBLIC_COMMIT_EMAIL:-}" ]; then
    echo "не настроено: в harness.conf пусты PUBLIC_REMOTE, PUBLIC_BRANCH, PUBLIC_BUILD_DIR и/или PUBLIC_COMMIT_EMAIL" >&2
    exit 2
fi

# Перепись истории — отдельный режим ДО обычной цепочки замков: ворота
# обычного пути в него не входят, у двери свои проверки.
if [ "${1:-}" = "--переписать-историю" ]; then
    [ "$#" = 4 ] && [ "${3:-}" = "--бэкап" ] || { say "ОТКАЗ: нужно --переписать-историю <каталог-копии> --бэкап <файл.bundle>"; exit 1; }
    perepisat_istoriyu "${2:-}" "${4:-}" || exit 1
    exit 0
fi

CHECK_ONLY=0
FIRST=0
[ "${1:-}" = "--проверка" ] && CHECK_ONLY=1
TRAIN_ARG=""
if [ "${1:-}" = "--поезд" ]; then
    TRAIN=1; TRAIN_ARG="${2:-}"
elif [ "$CHECK_ONLY" = 1 ] && [ "${2:-}" = "--поезд" ]; then
    TRAIN=1; TRAIN_ARG="${3:-}"
fi
# Первая публикация — особый случай, и решает его владелец, а не скрипт.
# Публичной ветки ещё нет: отсекать нечего, и замок 3 судить не по чему.
# 11.09.2026 владелец сказал: «1 публикуй как есть, проверю — обновим если
# что-то не так». Флаг работает ТОЛЬКО при отсутствующей публичной ветке и
# называет число непроверенных задач вслух — молчаливого «опубликовано» тут
# быть не должно.
[ "${1:-}" = "--первая" ] && FIRST=1
# Прямое слово владельца перевешивает замок 3: публичная ветка — ЕГО витрина,
# и он вправе выложить непроверенное, зная состав. Честность держится не
# отказом, а оглаской: список непроверенных задач уходит ему в канал этим же
# скриптом. Молчаливой публикации непроверенного не бывает (И-4).
PO_SLOVU=0
[ "${1:-}" = "--по-слову" ] && PO_SLOVU=1

say "=== публикация: сборка → $PUBLIC_REMOTE/$PUBLIC_BRANCH ==="

# Замок очереди закрывает окно «кнопка нажата → ворота → push»: слияние в нём сдвинуло
# бы пакет и сожгло нажатую кнопку. Окно «кнопка прислана → нажата» он не закрывает —
# слияния до нажатия меняют предмет согласия; его закрывает предмет-срез поезда, и поезд
# берёт замок только на срез (train_board) — и в проверке: её зелёный срез годен для кнопки.
# Проверка без поезда ничего не отправляет и слияний не держит. Предел ожидания — тот же, что у очереди.
if [ "$CHECK_ONLY" = 0 ] || [ "$TRAIN" = 1 ]; then
    [[ "${MERGE_QUEUE_WAIT_MIN:-30}" =~ ^[0-9]+$ ]] || {
        say "не настроено: MERGE_QUEUE_WAIT_MIN=«$MERGE_QUEUE_WAIT_MIN» в harness.conf — нужно целое число минут"
        exit 2; }
fi
if [ "$CHECK_ONLY" = 0 ] && [ "$TRAIN" = 0 ]; then
    take_merge_queue_lock "${LOG_DIR:?пуст LOG_DIR — не прочитан паспорт установки}" \
        "$(( ${MERGE_QUEUE_WAIT_MIN:-30} * 60 ))" || exit 1
fi
# Метка выпуска — до ворот (у поезда они в train_board): пул тяжёлых прогонов не выдаёт
# мест новым заявкам, кроме потомков выпуска, и ворота не стоят в очереди за помощниками.
release_mark_take \
    || say "внимание: метка выпуска не поставлена — ворота делят пул тяжёлых прогонов на общих правах"
# Поезд: срез, прошлый выпуск и ворота по разнице — первыми. Кнопка просится после зелёных
# ворот и на срез, а не на HEAD, поэтому слияния после среза её не сжигают.
# Почему у поезда ворота идут ДО замка 6в (у обычного хода — после): ворота поезда судят срез
# минуты, а не часы, и их вердикт нужен при любом исходе заметок. Отказ 6в («нет записи»,
# «не тот номер») чинится правкой пакета и новым срезом; стой ворота за ним, вердикт среза
# прятался бы за отказом заметок, и о красном коде узнавали бы лишь после правки заметок.
if [ "$TRAIN" = 1 ]; then
    train_board "${LOG_DIR:?пуст LOG_DIR — не прочитан паспорт установки}" "$TRAIN_ARG" \
        "$(( ${MERGE_QUEUE_WAIT_MIN:-30} * 60 ))" || exit 1
fi
# Заметки и номер выпуска — до кнопки и до ворот: правка заметок после «Да» сдвигает
# пакет и сжигает кнопку, а ворота идут десятки минут.
build_in_scratch --заметки || exit 1

# ── Замок 1: ворота ────────────────────────────────────────────────────────
# Свежесть согласия спрашивается ДО ворот (не тратя «да»), а тратится после:
# ворота идут 10–40 минут, и «да», полученное перед запуском, к замку 2 иначе
# старело. Прогон ворот — часть той же попытки, поэтому возраст судится на
# момент её начала.
POPYTKA_NACHATA=$(date +%s)
# Нет кнопки — ворота просят её сами (--просить), но не в проверке: --проверка сеть и владельца не трогает.
PREFLIGHT_ASK=""
[ "$CHECK_ONLY" = 1 ] || PREFLIGHT_ASK="--просить"
TRAIN_GUARD_ARGS=()
[ "$TRAIN" = 1 ] && train_guard_args
PREFLIGHT_REFUSAL=$(HARNESS_LOG_DIR="$LOG_DIR" python3 -I "$SCRIPTS_REAL/deploy_guard.py" --действие публикация --только-согласие \
        --публикация "$PACKAGE_DIR" "${TRAIN_GUARD_ARGS[@]}" $PREFLIGHT_ASK 2>&1) || {
    say "ОТКАЗ: $PREFLIGHT_REFUSAL"
    [ "$TRAIN" = 1 ] && say "после кнопки «Да» — тот же срез: bash scripts/publish.sh --поезд $TRAIN_SLICE_PACKAGE"
    exit 1
}
[ "$TRAIN" = 1 ] || zamok_vorot "${LOG_DIR:?пуст LOG_DIR — не прочитан паспорт установки}" || exit 1

# ── Замок 2: согласие владельца ИМЕННО на публикацию ───────────────────────
# Судит КОД ВОЗВРАТА ворот: 0 — согласие есть, любой другой (1, 2, упавший python) — стоп.
# Суд по тексту ответа пропускал всякий отказ, сказанный другими словами.
if [ "$TRAIN" = 0 ]; then
    PAKET_SHA=$(git -C "$PACKAGE_DIR" rev-parse HEAD 2>/dev/null) \
        || { say "ОТКАЗ: HEAD пакета $PACKAGE_DIR не прочитан — публиковать нечего"; exit 1; }
fi
if ! REFUSAL=$(HARNESS_LOG_DIR="$LOG_DIR" python3 -I "$SCRIPTS_REAL/deploy_guard.py" --действие публикация --только-согласие \
        --публикация "$PACKAGE_DIR" "${TRAIN_GUARD_ARGS[@]}" --свежесть-на "$POPYTKA_NACHATA" 2>&1); then
    say "ОТКАЗ: $REFUSAL"
    exit 1
fi
# Предмет, по которому открыт замок, ворота печатают строкой «предмет: …»; перед пушем он пересчитывается.
PREDMET_ZAMKA=$(printf '%s\n' "$REFUSAL" | sed -n 's/^предмет: //p' | tail -1)
say "замок 2 · согласие владельца на публикацию есть"

# ── Замок 3: на витрину — только проверенное владельцем ────────────────────
# Момент, на котором стоит публичная ветка. Нет ветки — нет и точки отсчёта:
# первую выбирает владелец, а не скрипт (вопрос задан 11.09.2026).
# Удалённый — ДАННЫЕ (PUBLIC_REMOTE в harness.conf). Вшитое имя уже стоило
# смены 11.09.2026: сверка версий искала «origin», живой пакет звал удалённый
# иначе. Здесь вшитое «harness» указывало на ПРИВАТНЫЙ репозиторий пакета —
# замок считал точку отсечки по нему, а публичная ветка живёт в другом.
# Точка отсчёта берётся ТАМ, где живёт публичная ветка. С переходом на сборку
# это каталог сборки, а не пакет: в пакете публичного удалённого может уже не
# быть, и замок 3 считал бы диапазон по пустоте.
OTKUDA_VETKA="$PACKAGE_DIR"
if git -C "${PUBLIC_BUILD_DIR:-/нет}" remote get-url "$PUBLIC_REMOTE" >/dev/null 2>&1; then
    OTKUDA_VETKA="$PUBLIC_BUILD_DIR"
fi
git -C "$OTKUDA_VETKA" fetch "$PUBLIC_REMOTE" 2>/dev/null || true
SINCE=$(git -C "$OTKUDA_VETKA" log -1 --format=%cI "$PUBLIC_REMOTE/$PUBLIC_BRANCH" 2>/dev/null) || true
if [ -z "$SINCE" ] && [ "$FIRST" = 0 ]; then
    say "ОТКАЗ: публичной ветки «$PUBLIC_BRANCH» ещё нет — первую точку отсечки называет владелец"
    say "Владелец сказал публиковать как есть? Тогда: bash scripts/publish.sh --первая"
    exit 1
fi
if [ -n "$SINCE" ] && [ "$FIRST" = 1 ]; then
    say "ОТКАЗ: публичная ветка «$PUBLIC_BRANCH» уже есть — --первая больше не про неё"
    exit 1
fi
if [ "$FIRST" = 1 ]; then
    # «--since=1970-01-01» git понимает как ПУСТОЙ диапазон (проверено: 0 против
    # 202 трейлеров без него) — счёт выходил нулевым и врал владельцу.
    CHISLO=$(непроверенные_с "" | grep -c . || true)
    say "ПЕРВАЯ ПУБЛИКАЦИЯ по слову владельца: уезжает как есть, задач без его"
    say "проверки — $CHISLO. Это записано; следующие публикации судит замок 3."
fi
# При первой публикации замку 3 судить не по чему: отсекать нечего, и число
# непроверенных уже названо вслух выше. Дальше он работает как обычно.
PENDING=""
[ "$FIRST" = 0 ] && PENDING=$(непроверенные_с "$SINCE")
if printf '%s' "$PENDING" | grep -q "ОТКАЗ-ИСТОРИИ"; then
    say "ОТКАЗ: историю проекта прочитать не удалось — замку судить не по чему"
    exit 2
fi
if printf '%s' "$PENDING" | grep -q "НЕТ-PyYAML"; then
    say "ОТКАЗ: нет PyYAML — статусы задач проверить нечем (замок судить не может)"
    exit 2
fi
if [ -n "$PENDING" ] && [ "$PO_SLOVU" = 0 ] && propustit_statusy_po_slovu "$LOG_DIR" "$PENDING"; then
    PENDING=""
fi
if [ -n "$PENDING" ] && [ "$PO_SLOVU" = 1 ]; then
    say "ПО СЛОВУ ВЛАДЕЛЬЦА: уезжает непроверенное им — $(printf '%s\n' "$PENDING" | grep -c .) задач(и):"
    printf '%s\n' "$PENDING" | sed 's/^/  • /'
    SPISOK=$(printf '%s\n' "$PENDING" | sed 's/ (статус.*//' | paste -sd', ')
    bash "$SCRIPTS_REAL/tg_send.sh" "Публикую по твоему слову. В публичную ветку уезжает непроверенное тобой: $SPISOK" >/dev/null 2>&1 || true
    PENDING=""
fi
if [ -n "$PENDING" ]; then
    say "ОТКАЗ: в публичную ветку попали бы задачи, которых владелец НЕ проверял:"
    printf '%s\n' "$PENDING" | sed 's/^/  • /'
    say "И-4: непроверенное не выдаётся за проверенное. Пусть владелец проверит и скажет — тогда статус станет done."
    exit 1
fi
if [ "$FIRST" = 1 ]; then
    say "замок 3 · первая публикация: не судит, число непроверенных названо выше"
else
    say "замок 3 · все задачи диапазона проверены владельцем"
fi

if [ "$CHECK_ONLY" = 1 ]; then
    build_in_scratch --сухо || exit 1
    TRAIN_STATE="проверка"
    exit 0
fi

# ── Публикация: собранное дерево, а не пакет ───────────────────────────────
опубликовать_сборку || exit 1
TRAIN_STATE="нечего везти"
[ -z "$RELEASE_TAG" ] || TRAIN_STATE="выпущен"

