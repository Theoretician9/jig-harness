#!/usr/bin/env bash
# Проба-контракт: метку «хакатон» сообщение в канал несёт само (tg_send.sh),
# и заготовка приказа помощнику в режиме лежит на месте.
#
# Исполнитель E. Файлы: scripts/tg_send.sh, harness/шаблоны-задач/хакатон-приказ.md
# (новый). Спека: docs/specs/2026-10-28-режим-хакатон.md («Метку „хакатон“
# сообщение в канал несёт само, её ставит tg_send.sh, а не память»).
#
# КОНТРАКТ tg_send.sh (текстовая отправка):
#   * Метка — строка «хакатон · работает, не продакшн». Отправленный текст =
#     метка + перевод строки + исходный текст, без других правок; та же строка
#     уходит в tg_out.jsonl (журнал исходящих пишет то, что ушло).
#   * Задача берётся из карты "$PROJECT_DIR/dev-map.yaml" (паспорт), двумя путями:
#       1) ОТЧЁТ СМЕНЫ (признак тот же, что у отметить_отчёт: HARNESS_REPORT или
#          CLAUDE_CODE_SESSION_ID не пусты) и единственная карточка в статусе wip —
#          уровня хакатон;
#       2) текст называет id карточки уровня хакатон, чей статус не done/принято
#          (cards.CLOSED_STATUSES). id — целым словом: регулярка
#          (?<![\w.-])<id>(?![\w-]|\.\w) — «hr.h2» не метит «hr.h».
#     Уровень судит scripts/hackathon.py (is_hackathon), своей копии правил нет.
#   * Иначе — текст уходит как прежде, без метки. Карточка хакатона в done
#     или принято, названная в тексте, метки не даёт.
#   * Блок решения метки упал (битая карта, сломанный hackathon.py, нет yaml) —
#     отправка идёт без метки, код 0, но в stderr tg_send.sh строка со словами
#     «метка хакатона потеряна»: молча терять метку нельзя (И-4).
#   * Сухая дверь отправки — подставной curl в PATH (как в
#     scripts/test_tg_send_commit_trees.sh); новой env-двери не нужно.
#
# КОНТРАКТ заготовки: harness/шаблоны-задач/хакатон-приказ.md — приказ ОДНОМУ
#   помощнику в режиме: одна проба на главный путь, больной случай живьём, код,
#   один ревьюер И-1…И-4, быстрые ворота gates.sh --хакатон, выкат deploy.sh,
#   перевод задачи в режим через hackathon.py --включить-задачу. Проба ищет эти
#   слова в тексте (список ниже, PHRASES).
#
# Стенд свой: паспорт, harness.conf, LOG_DIR, карта-образец, подставной curl.
# Боевой канал, токен и dev-map.yaml не трогаются; ключ стенда сгенерирован здесь.
# Дверь мутаций: HACKATHON_PROBE_SRC — корень судимого дерева (копия проекта);
# HACKATHON_ORDER_TEMPLATE — путь заготовки (в пакете она лежит иначе).
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "${HACKATHON_PROBE_SRC:-$HERE/..}" && pwd)"
TEMPLATE="${HACKATHON_ORDER_TEMPLATE:-$SRC/harness/шаблоны-задач/хакатон-приказ.md}"
# shellcheck disable=SC1091
source "$HERE/lib/selftest_cases.sh"

MARK="хакатон · работает, не продакшн"
STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT
TREE="$STAND/harnes"
mkdir -p "$TREE/scripts" "$STAND/zhurnal" "$STAND/bin"
for f in tg_send.sh limit_mark.py check-channel-messages.py language-level.py hackathon.py pipeline.py; do
    [ -f "$SRC/scripts/$f" ] && cp "$SRC/scripts/$f" "$TREE/scripts/"
done
cp -r "$SRC/scripts/lib" "$TREE/scripts/"
GIT_CONFIG_GLOBAL=/dev/null git -C "$TREE" init -q

{
    printf 'TG_CHAT_ID="100500"\n'                       # не-секрет: выдуманный чат стенда
    printf 'TG_TOKEN_FILE="%s"\n' "$STAND/klyuch"        # не-секрет: путь, а не значение
    printf 'PROJECT_DIR="%s"\n' "$TREE"
    printf 'LOG_DIR="%s"\n' "$STAND/zhurnal"
    printf 'SECRETS_DIR="%s"\n' "$STAND"
    printf 'TMUX_SESSION="proba-metki"\n'
    printf 'AGENT_START_CMD="true"\n'
} > "$STAND/install.conf"
python3 -c "import secrets,io,sys; io.open(sys.argv[1],'w').write('11'+secrets.token_hex(16)+'\n')" "$STAND/klyuch"
: > "$STAND/harness.conf"

# Подставной curl: доводы — в файл через NUL, ответ — успех Bot API.
cat > "$STAND/bin/curl" <<'CURL'
#!/usr/bin/env bash
cat > /dev/null
for a in "$@"; do printf '%s\0' "$a"; done > "$STAND_CURL_ARGS"
printf '{"ok":true,"result":{"message_id":1}}'
CURL
chmod +x "$STAND/bin/curl"
CURL_ARGS="$STAND/zhurnal/curl.args"
OUT_LOG="$STAND/zhurnal/tg_out.jsonl"

TODAY=$(date +%F)
card() {  # $1=id $2=status $3=уровень
    printf '      - id: %s\n        status: %s\n        title: "Задача %s"\n        summary: "образец пробы"\n' \
        "$1" "$2" "$1"
    printf '        глубина: { уровень: %s, характер: починка, кем: смена, когда: "%s" }\n' "$3" "$TODAY"
}
write_map() {  # stdin — карточки
    {
        printf 'version: 1\nqueue: []\nlayers:\n  - { id: infra, label: "Инфра", color: "#5aa06a" }\n'
        printf 'lines:\n  - id: core\n    layer: infra\n    name: "Ядро"\n    tasks:\n'
        cat
    } > "$TREE/dev-map.yaml"
}

RC=0
send() {  # $1 = report|daemon, $2 = текст
    : > "$CURL_ARGS"
    local report=""
    [ "$1" = report ] && report=1
    env -u CLAUDE_CODE_SESSION_ID -u HARNESS_TG_FORCE -u PRODUCT_REPO \
        PATH="$STAND/bin:$PATH" STAND_CURL_ARGS="$CURL_ARGS" HARNESS_REPORT="$report" \
        HARNESS_HOME="$STAND" LOG_DIR="$STAND/zhurnal" PROJECT_DIR="$TREE" \
        HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
        HEARTBEAT_DIR="$STAND/zhurnal/heartbeat" CLAUDE_PROJECTS_DIR="$STAND/net" \
        timeout 60 bash "$TREE/scripts/tg_send.sh" "$2" > "$STAND/vyvod.txt" 2> "$STAND/oshibki.txt"
    RC=$?
}
sent_text() {  # текст, который ушёл в Bot API (довод text= подставного curl)
    python3 - "$CURL_ARGS" <<'PY'
import sys
args = open(sys.argv[1], encoding="utf-8").read().split("\0")
texts = [a[5:] for a in args if a.startswith("text=")]
sys.stdout.write(texts[0] if texts else "<не отправлено>")
PY
}
journal_text() { tail -1 "$OUT_LOG" 2>/dev/null | jq -r '.text' 2>/dev/null; }

TEXT_PLAIN="Готово: проба прошла, 5 случаев."
TEXT_NAMED="Готово по hr.h: проба прошла, 5 случаев."
TEXT_OTHER="Готово по hr.h2: проба прошла, 5 случаев."

echo "── условие стенда: тексты уходят без метки, когда хакатона на карте нет ──"
{ card hr.o wip обычная; } | write_map
send report "$TEXT_PLAIN"
case_check "стенд: отправка доходит до подставного curl, текст как есть" "0|$TEXT_PLAIN" "$RC|$(sent_text)"

echo "── 1. отчёт смены, задача в работе — хакатона: метка первой строкой ──"
{ card hr.h wip хакатон; card hr.o plan обычная; } | write_map
send report "$TEXT_PLAIN"
case_check "код 0; ушло: метка, перевод строки, исходный текст" "0|$MARK
$TEXT_PLAIN" "$RC|$(sent_text)"
case_check "журнал исходящих пишет то же, что ушло (с меткой)" "$MARK
$TEXT_PLAIN" "$(journal_text)"

echo "── 2. весть демона называет id карточки хакатона (в test) — метка ──"
{ card hr.h test хакатон; card hr.o wip обычная; card hr.h2 plan обычная; } | write_map
send daemon "$TEXT_NAMED"
case_check "id хакатона в тексте — метка первой строкой" "$MARK
$TEXT_NAMED" "$(sent_text)"

echo "── 3. НЕИЗМЕННО: без хакатона — без метки ──"
send report "$TEXT_PLAIN"
case_check "НЕИЗМЕННО: отчёт смены, в работе обычная — без метки" "$TEXT_PLAIN" "$(sent_text)"
send daemon "$TEXT_OTHER"
case_check "НЕИЗМЕННО: id «hr.h2» (обычная) не метит по «hr.h» — целым словом" "$TEXT_OTHER" "$(sent_text)"
{ card hr.h wip хакатон; } | write_map
send daemon "$TEXT_PLAIN"
case_check "НЕИЗМЕННО: весть демона без id при хакатоне в работе — без метки" "$TEXT_PLAIN" "$(sent_text)"

echo "── 3а. id закрытой карточки хакатона (done, принято) — без метки ──"
for closed in done принято; do
    { card hr.h "$closed" хакатон; card hr.o wip обычная; } | write_map
    send daemon "$TEXT_NAMED"
    case_check "хакатон в «$closed», id в тексте — без метки" "$TEXT_NAMED" "$(sent_text)"
done

echo "── 3б. БОЛЬНОЙ СЛУЧАЙ: блок метки упал — отправка идёт, потеря метки названа ──"
cp "$TREE/scripts/hackathon.py" "$STAND/hackathon.keep"
printf 'raise RuntimeError("hackathon.py сломан пробой")\n' > "$TREE/scripts/hackathon.py"
{ card hr.h wip хакатон; } | write_map
send report "$TEXT_PLAIN"
case_check "код 0, текст ушёл без метки" "0|$TEXT_PLAIN" "$RC|$(sent_text)"
case_check "в stderr tg_send строка «метка хакатона потеряна»" "да" \
    "$(grep -qF 'метка хакатона потеряна' "$STAND/oshibki.txt" && echo да || echo нет)"
cp "$STAND/hackathon.keep" "$TREE/scripts/hackathon.py"

echo "── 4. заготовка приказа помощнику: $TEMPLATE ──"
case_check "заготовка есть" "да" "$([ -s "$TEMPLATE" ] && echo да || echo нет)"
PHRASES=("одна проба" "главный путь" "больной случай" "живьём" "один ревьюер"
         "И-1" "И-2" "И-3" "И-4" "gates.sh --хакатон" "deploy.sh" "hackathon.py --включить-задачу")
missing=""
for phrase in "${PHRASES[@]}"; do
    grep -qiF -- "$phrase" "$TEMPLATE" 2>/dev/null || missing="$missing «$phrase»"
done
case_check "заготовка называет каждое из ${#PHRASES[@]} требований" "" "$missing"

if [ "$CASES_FAILED" -eq 0 ]; then
    printf '\nПРОБА ЗЕЛЁНАЯ: случаев %d\n' "$CASES_PATHS"; exit 0
fi
printf '\nПРОБА КРАСНАЯ: неудач %d из %d\n' "$CASES_FAILED" "$CASES_PATHS"; exit 1
