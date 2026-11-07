#!/usr/bin/env bash
# Пробы обёртки демона на машинном выводе CLI (карточка hr.demons-stream-json,
# контракт .tasks/hr.demons-stream-json/pretask.yaml).
#
# Зачем: обёртка судит лимит по ТЕКСТУ всего вывода — регэксп «429|rate limit»
# ловит и ответ модели (check-builder пишет пробы про лимиты, ton-watch
# разбирает речь), час сброса приходит строкой, расход демонов невидим.
# CLI отдаёт те же факты полями (`--output-format stream-json --verbose`).
#
# Проба зовёт НАСТОЯЩУЮ обёртку и НАСТОЯЩЕГО судью; подставной `claude` в PATH
# печатает поток. Успешный поток — живой образец замера 24.10.2026
# (claude 2.1.299): 4 события system/init, assistant, rate_limit_event, result.
# Образец — данные пробы и едет вместе с ней: fixtures/ рядом со скриптом.
# Файл только в $LOG_DIR этой машины красил бы гейт у каждого клиента.
#
# ОБРАЗЦЫ ОТКАЗОВ СОБРАНЫ ПО СХЕМЕ CLI (spec §3), ЖИВОГО ЛИМИТА ЕЩЁ НЕ БЫЛО:
# это настоящие события образца с минимальной правкой полей (is_error,
# api_error, api_error_status, error, rate_limit_info.status=rejected).
#
#   bash scripts/test_demon_stream_json.sh [образец.jsonl]
# Код возврата: 0 — все случаи прошли, 1 — есть упавший, 2 — стенд не собран.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER="$HERE/claude-demon.sh"
JUDGE="$HERE/limit-in-log.py"
ASK="$HERE/ask-the-model.py"
FIXTURE="$HERE/fixtures/stream-json-sample.jsonl"
SAMPLE="${1:-${STREAM_JSON_SAMPLE:-$FIXTURE}}"
source "$HERE/lib/selftest_cases.sh"

# М2: гейт зовёт пробу без доводов — образец обязан лежать рядом с ней.
# Судится место по умолчанию, даже когда образец передан доводом.
echo "── образец потока ──"
case_check "М2 образец лежит рядом с пробой ($FIXTURE)" да \
    "$([ -r "$FIXTURE" ] && echo да || echo нет)"

# Без образца проба не судит ничего: без него по умолчанию — красный случай
# М2 выше, иначе — отказ стенда.
if [ ! -r "$SAMPLE" ]; then
    echo "СТЕНД: нет образца потока $SAMPLE" >&2
    [ "$SAMPLE" = "$FIXTURE" ] && { printf '\nитого путей %s, неудач %s\n' "$CASES_PATHS" "$CASES_FAILED"; exit 1; }
    exit 2
fi

STAND="$(mktemp -d -t demon-stream-probe.XXXXXX)"
trap 'rm -rf "$STAND"' EXIT
mkdir -p "$STAND/bin" "$STAND/log" "$STAND/heartbeat" "$STAND/panel-state" "$STAND/flows" "$STAND/root/scripts"

# Своя пауза: боевой файл паузы владельца проба не читает и не трогает.
export HARNESS_PANEL_STATE="$STAND/panel-state"
export PATH="$STAND/bin:$PATH"
export LOG_DIR="$STAND/log" HEARTBEAT_DIR="$STAND/heartbeat"
export TG_SEND="$STAND/bin/tg_send.sh" TG_OUT="$STAND/told.txt"
export PYTHONDONTWRITEBYTECODE=1
: > "$TG_OUT"

cat > "$STAND/bin/tg_send.sh" <<'SEND'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TG_OUT"
SEND

# Подставной CLI. Требование --verbose — текст отказа из сборки CLI 2.1.299
# (spec §2; живьём вызов без флага не делался). Доводы пишутся по строке.
cat > "$STAND/bin/claude" <<'CLAUDE'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$CLAUDE_ARGS"
stream=0 verbose=0 prev=""
for a in "$@"; do
    [ "$prev" = "--output-format" ] && [ "$a" = "stream-json" ] && stream=1
    [ "$a" = "--output-format=stream-json" ] && stream=1
    [ "$a" = "--verbose" ] && verbose=1
    prev="$a"
done
if [ "$stream" = 1 ] && [ "$verbose" = 0 ]; then
    echo "Error: When using --print, --output-format=stream-json requires --verbose" >&2
    exit 1
fi
cat "$CLAUDE_FLOW"
exit "${CLAUDE_CODE:-0}"
CLAUDE
chmod +x "$STAND/bin/claude" "$STAND/bin/tg_send.sh"
export CLAUDE_ARGS="$STAND/args.txt"

# Час сброса ЧИСЛОМ: нарочно не совпадает с часом в тексте («2pm» = 14:00),
# чтобы судья, взявший час из текста, покраснел.
# Минуты :37 — чтобы круглый час allowed_warning (живое resetsAt) не совпал.
RESET_AT=$(( ($(date +%s) / 3600 + 3) * 3600 + 37 * 60 ))
[ "$(date -d "@$RESET_AT" +%H)" = "14" ] && RESET_AT=$((RESET_AT + 3600))
RESET_HHMM="$(date -d "@$RESET_AT" +%H:%M)"

# Образцы — правкой НАСТОЯЩИХ событий, по одному файлу на случай.
python3 - "$SAMPLE" "$STAND/flows" "$RESET_AT" <<'PY' || { echo "СТЕНД: образцы не собраны" >&2; exit 2; }
import copy, json, sys
from pathlib import Path

sample, out, reset_at = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
events = [json.loads(line) for line in open(sample, encoding="utf-8") if line.strip()]
by_type = {e["type"]: e for e in events}
assert [e["type"] for e in events] == ["system", "assistant", "rate_limit_event", "result"], \
    "образец не тот: ждали 4 события init/assistant/rate_limit_event/result"
init, assistant, warning, result = (by_type[t] for t in
                                    ("system", "assistant", "rate_limit_event", "result"))

def write(name, flow):
    (out / name).write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in flow),
                            encoding="utf-8")

def answered(text):
    a, r = copy.deepcopy(assistant), copy.deepcopy(result)
    a["message"]["content"] = [{"type": "text", "text": text}]
    r["result"] = text
    return a, r

def refused(text, api_error, status, limit_info=None):
    # СИНТЕТИКА ПО СХЕМЕ CLI (spec §3): subtype остаётся success, признак — is_error.
    a, r = answered(text)
    r.update(is_error=True, api_error_status=status)
    if api_error is not None:
        r["api_error"] = api_error
    a.update(error="rate_limit" if limit_info else "unknown", is_api_error_message=True)
    if api_error is not None:
        a["api_error"] = api_error
    if limit_info:
        a["api_error_params"] = {"rate_limit_info": limit_info}
    return a, r

# Успех — живой образец байт в байт.
(out / "ok.jsonl").write_bytes(Path(sample).read_bytes())

# Ответ МОДЕЛИ со словами лимита и «429» — не отказ.
a, r = answered("Проба лимита: отказ 429, rate limit — «You've hit your usage limit · resets 3pm»")
write("model-says-429.jsonl", [init, a, warning, r])

# Лимит плана. Живое allowed_warning (своё resetsAt) идёт ДО rejected: судья,
# взявший первое попавшееся resetsAt, назовёт не тот час.
limit_info = {"status": "rejected", "resetsAt": reset_at, "rateLimitType": "seven_day"}
rejected = copy.deepcopy(warning)
rejected["rate_limit_info"] = dict(limit_info, utilization=1.0)
a, r = refused("You've hit your weekly limit · resets Oct 26, 2pm (Asia/Qostanay)",
               "usage_limit_reached", 429, limit_info)
write("limit.jsonl", [init, warning, a, rejected, r])

# Тот же лимит, но вид отказа CLI переименовал (неизвестный = отсутствует),
# кода 429 и числа сброса нет — остаётся запасной разбор ТЕКСТА result.
a, r = refused("You've hit your weekly limit · resets 3:10pm (Asia/Qostanay)",
               "usage_cap_renamed_in_future_cli", None)
write("limit-unknown-kind.jsonl", [init, a, r])

# is_error без вида, без кода и без слов лимита.
a, r = refused("API Error: Connection error.", None, None)
write("error-plain.jsonl", [init, a, r])

# Неизвестный вид отказа без слов лимита — не лимит.
a, r = refused("API Error: 503 upstream overloaded", "some_future_kind", 503)
write("error-unknown-kind.jsonl", [init, a, r])

# Отказ доступа: текст — живой случай 15–21.09.2026, поля — синтетика по схеме.
a, r = refused("Your organization has disabled Claude subscription access for Claude Code",
               "org_disabled_credential", 403)
write("denied.jsonl", [init, a, r])

# Вывод без единого события JSON (CLI упал до потока) — прежний текстовый судья.
(out / "plain-limit.txt").write_text(
    "You've hit your weekly limit · resets Sep 16, 6pm (Asia/Qostanay)\n", encoding="utf-8")

# Обрыв: события есть, итога нет; модель успела написать слова лимита.
a, _ = answered("Пишу пробу: «You've hit your usage limit · resets 3pm», код 429")
write("cut.jsonl", [init, a])
PY

flow() { export CLAUDE_FLOW="$STAND/flows/$1" CLAUDE_CODE="$2"; }
reset_state() {
    rm -f "$LOG_DIR/лимит-демонов.состояние" "$LOG_DIR/лимит-демонов.состояние.сказано" \
          "$LOG_DIR/отказ-демонов.состояние" "$LOG_DIR/отказ-демонов.сказано"
    : > "$TG_OUT"
}
told() { grep -c . "$TG_OUT"; }
yes_no() { if "$@"; then echo да; else echo нет; fi; }
SPEND="$LOG_DIR/demon-spend.jsonl"
# Строки РАСХОДА: строка demon.start пишется до вызова и расходом не является.
spend_lines() { [ -f "$SPEND" ] || { echo 0; return; }; grep -v '"demon.start"' "$SPEND" | grep -c . || true; }
# Поле ПОСЛЕДНЕЙ строки расхода; «нет строки» — отдельный ответ, не пусто.
spend_field() {
    python3 - "$SPEND" "$1" <<'PY'
import json, sys
try:
    last = [l for l in open(sys.argv[1], encoding="utf-8") if l.strip()][-1]
except (OSError, IndexError):
    print("нет строки"); sys.exit()
v = json.loads(last).get(sys.argv[2], "нет поля")
print(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v)
PY
}

# ─────────────────────── судья: limit-in-log.py --в-потоке ───────────────────
echo "── судья по потоку ──"
judge() { JOUT="$(python3 "$JUDGE" --в-потоке "$STAND/flows/$1" 2>/dev/null)"; JRC=$?; }
judge limit.jsonl
case_check "С1 лимит по полю api_error/429 — код 0" 0 "$JRC"
case_check "С2 час сброса — ЧИСЛОМ resetsAt события rejected, не из текста и не из allowed_warning" \
    "обновится в $RESET_HHMM" "${JOUT%% (*}"
judge ok.jsonl
case_check "С3 живой успех (allowed_warning в потоке) — не лимит, код 1" 1 "$JRC"
judge model-says-429.jsonl
case_check "С4 «429» и слова лимита в ОТВЕТЕ модели — не лимит, код 1" 1 "$JRC"
judge limit-unknown-kind.jsonl
case_check "С5 неизвестный вид + текст лимита в result — запасной разбор текста, код 0" 0 "$JRC"
case_check "С6 запасной разбор переводит час в 24 часа" "обновится в 15:10 (Asia/Qostanay)" "$JOUT"
judge error-plain.jsonl
case_check "С7 is_error без вида и без слов лимита — не лимит, код 1" 1 "$JRC"
judge error-unknown-kind.jsonl
case_check "С8 неизвестный вид без слов лимита — не лимит, код 1" 1 "$JRC"
judge cut.jsonl
case_check "С9 нет события result — «итога нет», свой код 3" 3 "$JRC"
python3 "$JUDGE" --в-потоке "$STAND/flows/нет-такого.jsonl" >/dev/null 2>&1
case_check "С10 поток не прочитан — код 2, а не «не лимит»" 2 "$?"
judge plain-limit.txt
case_check "С11 вывод без JSON — прежний разбор текста, код 0" 0 "$JRC"
case_check "С12 прежний разбор: час в 24 часа" "обновится в Sep 16, 18:00 (Asia/Qostanay)" "$JOUT"

# ─────────────────────────── обёртка: claude-demon.sh ─────────────────────────
echo "── обёртка: доводы CLI ──"
reset_state
flow ok.jsonl 0
OUT="$(bash "$WRAPPER" проба-демон -p --model x "задание" --output-format text 2>/dev/null)"; RC=$?
case_check "О1 ровно один --output-format" 1 "$(grep -cx -- '--output-format' "$CLAUDE_ARGS")"
case_check "О2 формат — stream-json, переданный text вырезан" "stream-json" \
    "$(grep -x -A1 -- '--output-format' "$CLAUDE_ARGS" | tail -n1)"
case_check "О3 --verbose стоит всегда (без него CLI отказывает)" 1 "$(grep -cx -- '--verbose' "$CLAUDE_ARGS")"
case_check "О4 --strict-mcp-config сохранён" 1 "$(grep -cx -- '--strict-mcp-config' "$CLAUDE_ARGS")"

echo "── обёртка: живой успех ──"
case_check "О5 успех — код 0" 0 "$RC"
case_check "О6 демону на stdout — result.result, а не JSON" "ок" "${OUT:0:60}"
case_check "О7 владельцу не сказано ничего" 0 "$(told)"
INIT_UUID="$(head -n1 "$SAMPLE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["uuid"])')"
case_check "О8 сырой поток лёг в \$LOG_DIR/demon-stream/<демон>.jsonl" да \
    "$(yes_no grep -qs "$INIT_UUID" "$LOG_DIR/demon-stream/проба-демон.jsonl")"

echo "── обёртка: строка расхода ──"
case_check "Р1 одна строка на вызов" 1 "$(spend_lines)"
case_check "Р2 демон" "проба-демон" "$(spend_field демон)"
case_check "Р3 модель из init" "claude-haiku-5-5" "$(spend_field модель)"
case_check "Р4 session_id" "1aeb7165-5587-4527-8065-bcbdea662b57" "$(spend_field session_id)"
case_check "Р5 цена total_cost_usd" "0.00251958" "$(spend_field total_cost_usd)"
case_check "Р6 cache_creation_input_tokens" 12038 "$(spend_field cache_creation_input_tokens)"
case_check "Р7 cache_read_input_tokens" 10978 "$(spend_field cache_read_input_tokens)"
case_check "Р8 input_tokens" 2 "$(spend_field input_tokens)"
case_check "Р9 output_tokens" 4 "$(spend_field output_tokens)"
case_check "Р10 num_turns" 1 "$(spend_field num_turns)"
case_check "Р11 duration_ms" 1559 "$(spend_field duration_ms)"
case_check "Р12 is_error" false "$(spend_field is_error)"
case_check "Р13 когда — момент вызова (ISO с датой сегодня)" да \
    "$(yes_no grep -q "$(date +%Y-%m-%d)" <<<"$(spend_field когда)")"

echo "── обёртка: «429» в ответе модели ──"
reset_state
flow model-says-429.jsonl 0
OUT="$(bash "$WRAPPER" проба-демон -p "задание" 2>/dev/null)"; RC=$?
case_check "О9 ответ модели со словом «429» — код 0, а не 77" 0 "$RC"
case_check "О10 о лимите владельцу не сказано" 0 "$(told)"
case_check "О11 состояние лимита не записано" нет "$(yes_no test -f "$LOG_DIR/лимит-демонов.состояние")"
case_check "О12 ответ отдан демону как есть" да "$(yes_no grep -q 'Проба лимита: отказ 429' <<<"$OUT")"

for code in 1 0; do
    echo "── обёртка: лимит по полям, код CLI $code (код при отказе API живьём не замерен) ──"
    reset_state
    flow limit.jsonl "$code"
    BEFORE="$(spend_lines)"
    bash "$WRAPPER" проба-демон -p "задание" >/dev/null 2>&1; RC=$?
    case_check "Л1/$code лимит — код 77" 77 "$RC"
    case_check "Л2/$code владельцу сказано один раз" 1 "$(told)"
    case_check "Л3/$code в сообщении час сброса ЧИСЛОМ ($RESET_HHMM), не «14:00» текста" да \
        "$(yes_no grep -q "обновится в $RESET_HHMM" "$TG_OUT")"
    case_check "Л4/$code строка расхода есть и на отказе" "$((BEFORE + 1))" "$(spend_lines)"
    case_check "Л5/$code в расходе is_error" true "$(spend_field is_error)"
    case_check "Л6/$code в расходе api_error" "usage_limit_reached" "$(spend_field api_error)"
done

echo "── обёртка: неизвестный вид, текст лимита в result — запасной судья ──"
reset_state
flow limit-unknown-kind.jsonl 1
bash "$WRAPPER" проба-демон -p "задание" >/dev/null 2>&1; RC=$?
case_check "Л7 запасной текст по result даёт лимит — код 77" 77 "$RC"
case_check "Л8 час из текста в 24 часа" да "$(yes_no grep -q 'обновится в 15:10' "$TG_OUT")"

echo "── обёртка: CLI упал до потока (вывод без JSON) — прежний судья ──"
reset_state
flow plain-limit.txt 1
bash "$WRAPPER" проба-демон -p "задание" >/dev/null 2>&1; RC=$?
case_check "Л9 текст лимита без JSON — код 77" 77 "$RC"
case_check "Л10 час из текста" да "$(yes_no grep -q 'обновится в Sep 16, 18:00' "$TG_OUT")"

for code in 0 1; do
    echo "── обёртка: is_error без лимита, код CLI $code ──"
    reset_state
    flow error-plain.jsonl "$code"
    OUT="$(bash "$WRAPPER" проба-демон -p "задание" 2>/dev/null)"; RC=$?
    case_check "Е1/$code is_error — не 0 и не 77" да "$(yes_no test "$RC" -ne 0 -a "$RC" -ne 77)"
    case_check "Е2/$code о лимите не сказано" 0 "$(told)"
done

echo "── обёртка: is_error при СТОЯЩЕМ лимите не снимает его ──"
reset_state
printf 'обновится в 03:00' > "$LOG_DIR/лимит-демонов.состояние"
: > "$LOG_DIR/лимит-демонов.состояние.сказано"
flow error-unknown-kind.jsonl 0
bash "$WRAPPER" проба-демон -p "задание" >/dev/null 2>&1
case_check "Е3 «Лимит снят» не сказан на отказе с кодом 0" 0 "$(told)"
case_check "Е4 состояние лимита осталось" да "$(yes_no test -f "$LOG_DIR/лимит-демонов.состояние")"

echo "── обёртка: обрыв без result ──"
for code in 1 0; do
    reset_state
    printf 'обновится в 03:00' > "$LOG_DIR/лимит-демонов.состояние"
    : > "$LOG_DIR/лимит-демонов.состояние.сказано"
    flow cut.jsonl "$code"
    BEFORE="$(spend_lines)"
    OUT="$(bash "$WRAPPER" проба-демон -p "задание" 2>/dev/null)"; RC=$?
    case_check "Б1/$code обрыв — не 0 и не 77 (слова лимита в ответе модели не судятся)" да \
        "$(yes_no test "$RC" -ne 0 -a "$RC" -ne 77)"
    case_check "Б2/$code ни «лимит», ни «снят» владельцу" 0 "$(told)"
    case_check "Б3/$code демону не отдан сырой JSON" нет "$(yes_no grep -q '"type"' <<<"$OUT")"
    case_check "Б4/$code в журнале обёртки — «без итога»" да \
        "$(yes_no grep -q 'без итога' "$LOG_DIR/claude-demon.log")"
    case_check "Б5/$code строки расхода нет — расхода CLI не назвал" "$BEFORE" "$(spend_lines)"
    : > "$LOG_DIR/claude-demon.log"
done

echo "── обёртка: повторяющийся отказ доступа ──"
reset_state
flow denied.jsonl 1
bash "$WRAPPER" devmap-selfheal -p "z" >/dev/null 2>&1
case_check "Д1 подпись отказа — не сырой JSON события" нет \
    "$(yes_no grep -q '"type"' "$LOG_DIR/отказ-демонов.состояние")"
bash "$WRAPPER" ton-watch -p "z" >/dev/null 2>&1
case_check "Д2 повтор у соседа — сказано один раз" 1 "$(told)"
case_check "Д3 владельцу — человеческий текст result, не JSON" да \
    "$(yes_no grep -q 'disabled Claude subscription access' "$TG_OUT")"

# М1. Смерть CLI после старта (OOM-kill — код 137) даёт поток без итога: init
# пришёл, result нет. Один обрыв — случайность (Б2 молчит), второй такой же
# подряд — тот же порог, что у любого отказа не по лимиту: второй одинаковый
# отказ доходит до владельца ОДИН раз (claude-demon.sh, otkaz_povtoryayushchijsya).
# Зелен на поломке? «подписью стал текст модели» — нет: в обрыве модель пишет
# слова лимита, и они владельцу не идут; «сказано на первом» — нет: М1а;
# «сказано на каждом» — нет: М1г.
echo "── обёртка: обрыв повторился (смерть CLI после старта) ──"
reset_state
flow cut.jsonl 137
bash "$WRAPPER" devmap-selfheal -p "z" >/dev/null 2>&1; RC=$?
case_check "М1а первый обрыв — владельцу 0" 0 "$(told)"
case_check "М1б код обрыва — не 0 и не 77" да "$(yes_no test "$RC" -ne 0 -a "$RC" -ne 77)"
bash "$WRAPPER" ton-watch -p "z" >/dev/null 2>&1
case_check "М1в второй обрыв подряд (у соседа) — владельцу сказано один раз" 1 "$(told)"
case_check "М1в весть называет обрыв потока" да "$(yes_no grep -q 'оборван' "$TG_OUT")"
case_check "М1в слова модели из оборванного потока владельцу не идут" нет \
    "$(yes_no grep -qiE 'usage limit|429' "$TG_OUT")"
bash "$WRAPPER" check-builder -p "z" >/dev/null 2>&1
case_check "М1г третий обрыв — второй вести нет" 1 "$(told)"
flow ok.jsonl 0
bash "$WRAPPER" check-builder -p "z" >/dev/null 2>&1
case_check "М1д здоровый вызов после обрывов — «Прошло» один раз" да \
    "$([ "$(told)" = 2 ] && grep -q '^Прошло' "$TG_OUT" && echo да || echo нет)"

# М4. Сырой поток демона растёт на каждый вызов (tee -a) — потолок в данных
# (DEMON_STREAM_MAX_KB). Сверх потолка файл урезается до ХВОСТА по границе
# строки: последний вызов цел, первая строка — целый JSON.
# Зелен на поломке? «урезан с хвоста, а не с головы» — нет: последняя строка
# судится байт в байт; «резка посреди строки» — нет: каждая строка разбирается;
# «потолок не из окружения/данных» — нет: подложено вчетверо больше потолка.
echo "── обёртка: сырой поток не растёт без предела ──"
reset_state
CAP_KB=16
stream_of() { printf '%s' "$LOG_DIR/demon-stream/$1.jsonl"; }
filler() {  # $1 = файл, $2 = КБ подложенных строк JSON
    mkdir -p "$(dirname "$1")"
    python3 - "$1" "$2" <<'PY'
import json, sys
with open(sys.argv[1], "w", encoding="utf-8") as out:
    n = 0
    while out.tell() < int(sys.argv[2]) * 1024:
        out.write(json.dumps({"type": "filler", "n": n, "pad": "x" * 200}) + "\n")
        n += 1
PY
}
# Судья потока: «размер последняя-строка-как-в-образце все-строки-JSON первая-строка».
stream_verdict() {
    python3 - "$1" "$SAMPLE" <<'PY'
import json, sys
data = open(sys.argv[1], "rb").read()
want_last = [l for l in open(sys.argv[2], "rb").read().split(b"\n") if l][-1]
lines = [l for l in data.split(b"\n") if l]
def parses(l):
    try:
        json.loads(l); return True
    except ValueError:
        return False
print(len(data), "да" if lines and lines[-1] == want_last else "нет",
      "да" if all(parses(l) for l in lines) else "нет",
      json.loads(lines[0]).get("type") if lines and parses(lines[0]) else "битая")
PY
}
flow ok.jsonl 0
filler "$(stream_of проба-потолок)" $((CAP_KB * 4))
DEMON_STREAM_MAX_KB=$CAP_KB bash "$WRAPPER" проба-потолок -p "z" >/dev/null 2>&1
read -r SIZE LAST_OK ALL_JSON FIRST_TYPE <<<"$(stream_verdict "$(stream_of проба-потолок)")"
case_check "М4а сверх потолка ${CAP_KB} КБ файл урезан до потолка" да \
    "$(yes_no test "$SIZE" -le $((CAP_KB * 1024)))"
case_check "М4б последняя запись — итог этого вызова, байт в байт" да "$LAST_OK"
case_check "М4в резка по границе строки: каждая строка — JSON" да "$ALL_JSON"
case_check "М4г этот вызов цел: его init в файле" да \
    "$(yes_no grep -qs "$INIT_UUID" "$(stream_of проба-потолок)")"
# Пара: ниже потолка голова не трогается — подложенное начало на месте.
filler "$(stream_of проба-под-потолком)" 2
DEMON_STREAM_MAX_KB=$CAP_KB bash "$WRAPPER" проба-под-потолком -p "z" >/dev/null 2>&1
read -r SIZE LAST_OK ALL_JSON FIRST_TYPE <<<"$(stream_verdict "$(stream_of проба-под-потолком)")"
case_check "М4д пара: ниже потолка голова цела и итог дописан" "filler да" "$FIRST_TYPE $LAST_OK"
# Потолок — данные: ключ объявлен в образце harness.conf (проект или пакет).
SAMPLE_CONF=""
for c in "$HERE/../config/harness.conf" "$HERE/../harness/config/harness.conf"; do
    [ -r "$c" ] && { SAMPLE_CONF="$c"; break; }
done
case_check "М4е ключ DEMON_STREAM_MAX_KB со значением в образце harness.conf" да \
    "$(yes_no grep -qE '^DEMON_STREAM_MAX_KB="?[0-9]+' "${SAMPLE_CONF:-/нет-образца}")"

echo "── обёртка: пауза — модель не звана, расхода нет ──"
reset_state
flow ok.jsonl 0
BEFORE="$(spend_lines)"
printf '{"когда": %s, "почему": "проба", "кто": "владелец"}\n' "$(date +%s)" > "$HARNESS_PANEL_STATE/пауза.json"
bash "$WRAPPER" на-паузе -p "z" >/dev/null 2>&1; RC=$?
rm -f "$HARNESS_PANEL_STATE/пауза.json"
case_check "П1 пауза — 78" 78 "$RC"
case_check "П2 на паузе строки расхода нет" "$BEFORE" "$(spend_lines)"

# ───────────────────────────── ask-the-model.py ──────────────────────────────
# Там формат json: один объект result того же вида (spec §4). Образец — то же
# result-событие с правкой полей (СИНТЕТИКА, живого лимита ещё не было).
echo "── ask-the-model: лимит отдельной причиной ──"
printf '#!/usr/bin/env bash\necho haiku\n' > "$STAND/root/scripts/model-for.sh"
python3 - "$STAND/flows/limit.jsonl" "$STAND/limit-result.json" <<'PY'
import json, sys
last = [l for l in open(sys.argv[1], encoding="utf-8") if l.strip()][-1]
open(sys.argv[2], "w", encoding="utf-8").write(json.dumps(json.loads(last), ensure_ascii=False))
PY
for code in 1 0; do
    flow ../limit-result.json "$code"
    REASON="$(python3 - "$ASK" "$STAND/root" <<'PY' 2>/dev/null
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("ask_the_model", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
answer, _model, _sec, refusal, _price = m.спросить("в", {"n": "число"}, "дежурный", 30, Path(sys.argv[2]))
print(refusal if answer is None else "ответ принят")
PY
)"
    case_check "А1/$code is_error с usage_limit_reached — причина начинается «лимит подписки»" \
        "лимит подписки" "${REASON:0:14}"
done

printf '\nитого путей %s, неудач %s\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
