#!/usr/bin/env bash
# Пробы СИГНАЛОВ ЛИМИТА для автоперехода аккаунта
# (этап 1в, группа Б; спека задачи §1в.2, §1в.4, §1в.10;
# контракт — pretask.yaml там же).
#
# Что держат пробы. Автопереход будят два существующих места: limit-watch.sh
# (лимит смены доказан) и claude-demon.sh (демон получил 77). Оба зовут дверь
# `account-switch.py --авто --от смена|демоны` и по её коду решают, ставить ли
# метку лимита, слать ли весть, будить ли смену, повторять ли вызов модели.
# Беды, которые здесь ловятся: весть о лимите вдобавок к переходу; громкая метка
# при переходе; пинок смены в час сброса мимо двери; демоны хором зовут дверь;
# демон получает два ответа или повтор с тем же id сессии; весть демона вдобавок
# к вести двери; метка «все в лимите» устаревает через 6 ч при недельном окне;
# метка держится после перехода по ключу; старые отказы 429 держат лимит после
# перехода; помощник с идущей командой объявлен стоящим в лимите; ключ в журналах.
#
# Стенд свой: временный каталог, КОПИЯ дерева ($S/proj) — в ней настоящие
# limit-watch.sh, claude-demon.sh, claude-run.sh, limit_mark.py, limit-in-log.py,
# stall-in-log.py, lib/, а на месте двери account-switch.py — подставная: пишет
# свои доводы в файл и отвечает кодом из файла (очередь кодов, последний
# повторяется). Код, забывший про PROJECT_DIR, всё равно попадёт в подставную:
# рядом со скриптом лежит она же. pane.sh — подставной: пинок пишется строкой.
# Подставной claude судит sha своей CLAUDE_CODE_OAUTH_TOKEN (ключ не пишет):
# ключ alpha — отказ по лимиту, beta — ответ. Свои LOG_DIR, STATE_DIR, каталог
# аккаунтов, транскрипты, канал. Ключи фиктивные, рождены прогоном. Боевые
# журналы, метки, crontab и ключи не трогаются. Каждый вызов — под timeout.
#
# Отказы и «вести нет» судятся только у ЖИВОГО механизма: случай «дверь не
# звана» или «стоит = 1» зелёный лишь вместе с доказательством, что тот же стенд
# дверь зовёт / разбор понимает ключ (иначе он был бы зелёным до кода).
#
# Двери мутаций (путь к испорченной КОПИИ; в стенд кладётся она):
#   LIMIT_WATCH_UNDER_TEST, CLAUDE_DEMON_UNDER_TEST, LIMIT_MARK_UNDER_TEST,
#   LIMIT_IN_LOG_UNDER_TEST
#
#   bash scripts/test_switch_account_limit.sh
# Код возврата: 0 — все случаи прошли, 1 — есть упавший.
set -uo pipefail
export LC_ALL=C.UTF-8
export PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
SRC_WATCH="${LIMIT_WATCH_UNDER_TEST:-$HERE/../harness/demons/limit-watch.sh}"
SRC_DEMON="${CLAUDE_DEMON_UNDER_TEST:-$HERE/claude-demon.sh}"
SRC_MARK="${LIMIT_MARK_UNDER_TEST:-$HERE/limit_mark.py}"
SRC_INLOG="${LIMIT_IN_LOG_UNDER_TEST:-$HERE/limit-in-log.py}"
source "$HERE/lib/selftest_cases.sh"

unset TMUX TMUX_PANE CLAUDE_CODE_OAUTH_TOKEN PROJECT_DIR INSTALL_CONF OWNER_TZ \
      LIMIT_SAY_MIN LIMIT_FRESH_MIN LIMIT_KICK_DELAY_SEC LIMIT_KICK_MAX_MIN LIMIT_STALE_MIN
S="$(mktemp -d "${TMPDIR:-/tmp}/switch-limit-XXXXXX")"
trap 'rm -rf "$S"' EXIT
mkdir -p "$S/proj/scripts" "$S/proj/harness/demons" "$S/home" "$S/secrets" "$S/state/account" \
         "$S/state-bare" "$S/door" "$S/bin" "$S/runs" "$S/tr"
chmod 700 "$S/secrets"
ACC="$S/state/account"
DOOR="$S/door"

yes_no() { "$@" >/dev/null 2>&1 && echo да || echo нет; }
sha() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
rnd() { head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
count() { local n; n=$(grep -c -e "${2:-.}" "$1" 2>/dev/null); echo "${n:-0}"; }

# ── фиктивные ключи (рождены прогоном) и профили ───────────────────────────
KEY_A="proba-limit-alpha-$(rnd)"
KEY_B="proba-limit-beta-$(rnd)"
KF_A="kf-alpha-$(rnd)"
KF_B="kf-beta-$(rnd)"
printf '%s' "$KEY_A" > "$S/secrets/$KF_A"
printf '%s' "$KEY_B" > "$S/secrets/$KF_B"
chmod 600 "$S/secrets"/*
sha "$KEY_A" > "$DOOR/sha-a"
sha "$KEY_B" > "$DOOR/sha-b"
python3 -I - "$ACC/profiles.json" "$KF_A" "$KF_B" "$(sha "$KEY_A")" "$(sha "$KEY_B")" <<'PY'
import json, sys
путь, фа, фб, ша, шб = sys.argv[1:]
п = lambda имя, ф, ш: {"имя": имя, "подпись": имя.upper(), "орг": "0rg-" + имя, "почта": "",
                       "ключ": ф, "отпечаток": ш[:8]}
json.dump({"профили": [п("alpha", фа, ша), п("beta", фб, шб)]},
          open(путь, "w", encoding="utf-8"), ensure_ascii=False)
PY
state_launch() {  # $1 = запуск; переключений нет
    printf '{"запуск": "%s", "идёт": "%s", "переключения": []}\n' "$1" "$1" > "$ACC/state.json"
}
state_field() {
    python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8")).get(sys.argv[2],""))' \
        "$ACC/state.json" "$1" 2>/dev/null || echo битый
}
state_launch alpha

# ── конфиг стенда ──────────────────────────────────────────────────────────
: > "$S/home/install.conf"; : > "$S/home/harness.conf"
export HARNESS_HOME="$S/home" HARNESS_INSTALL_CONF="$S/home/install.conf" HARNESS_CONF="$S/home/harness.conf"
export SECRETS_DIR="$S/secrets" STATE_DIR="$S/state" HARNESS_ACCOUNT_DIR="$ACC"

# ── копия дерева: настоящие скрипты, подставные дверь, канал, панель ──────
cp -r "$HERE/lib" "$S/proj/scripts/lib"
rm -rf "$S/proj/scripts/lib/__pycache__"
cp "$SRC_MARK" "$S/proj/scripts/limit_mark.py"
cp "$SRC_INLOG" "$S/proj/scripts/limit-in-log.py"
cp "$HERE/stall-in-log.py" "$HERE/claude-run.sh" "$S/proj/scripts/"
cp "$SRC_DEMON" "$S/proj/scripts/claude-demon.sh"
cp "$SRC_WATCH" "$S/proj/harness/demons/limit-watch.sh"

# Подставная дверь. Пути стенда вписаны в неё: код, чистящий окружение, её не
# собьёт. Лок демонов судится попыткой взять его без ожидания.
cat > "$S/proj/scripts/account-switch.py" <<DOORPY
#!/usr/bin/env python3
import fcntl, json, os, sys
D = "$DOOR"
ACC = "$ACC"
with open(os.path.join(D, ".lock"), "a") as замок:
    fcntl.flock(замок, fcntl.LOCK_EX)
    путь_кодов = os.path.join(D, "codes")
    коды = [с.strip() for с in open(путь_кодов, encoding="utf-8") if с.strip()]
    код = int(коды[0]) if коды else 9
    if len(коды) > 1:
        open(путь_кодов, "w", encoding="utf-8").write("\n".join(коды[1:]) + "\n")
    лок = "без-LOG_DIR"
    if os.environ.get("LOG_DIR"):
        путь_лока = os.path.join(os.environ["LOG_DIR"], "locks", "лимит-демонов.lock")
        лок = "нет-файла"
        if os.path.exists(путь_лока):
            with open(путь_лока, "a") as л:
                try:
                    fcntl.flock(л, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(л, fcntl.LOCK_UN)
                    лок = "свободен"
                except BlockingIOError:
                    лок = "под-локом"
    цель = ""
    if os.path.exists(os.path.join(D, "switch_to")):
        цель = open(os.path.join(D, "switch_to"), encoding="utf-8").read().strip()
    if код == 0 and цель:
        с = json.load(open(os.path.join(ACC, "state.json"), encoding="utf-8"))
        с["запуск"] = цель
        json.dump(с, open(os.path.join(ACC, "state.json"), "w", encoding="utf-8"), ensure_ascii=False)
    with open(os.path.join(D, "calls.log"), "a", encoding="utf-8") as ф:
        ф.write(" ".join(sys.argv[1:]) + "\t" + str(код) + "\t" + лок + "\n")
    with open(os.path.join(D, "all-calls.log"), "a", encoding="utf-8") as ф:
        ф.write(" ".join(sys.argv[1:]) + "\n")
    with open(os.path.join(D, "events.log"), "a", encoding="utf-8") as ф:
        ф.write("дверь " + " ".join(sys.argv[1:]) + "\n")
if код == 0:
    print(цель or "beta")
sys.exit(код)
DOORPY
chmod +x "$S/proj/scripts/account-switch.py"

cat > "$S/proj/scripts/lib/pane.sh" <<PANE
# shellcheck shell=bash
# Подставная панель стенда: пинок — строка в журнал событий, tmux не трогается.
pane_kick_service() { printf 'пинок\n' >> "$DOOR/events.log"; PANE_REFUSAL=""; TMUX_KICKED=1; return 0; }
PANE

# Подставной канал — разбор доводов боевой двери: ключи «--…» текстом не являются.
cat > "$S/proj/scripts/tg_send.sh" <<'KANAL'
#!/usr/bin/env bash
for A in "$@"; do
    case "$A" in --*) continue ;; esac
    printf '%s\n' "$A" | tr '\n' ' ' >> "$LOG_DIR/kanal.txt"; printf '\n' >> "$LOG_DIR/kanal.txt"
    break
done
exit 0
KANAL
chmod +x "$S/proj/scripts/tg_send.sh"

# Подставной claude: alpha — отказ по лимиту, beta — ответ, иной/нет — отказ.
# Пишет «кто\tid сессии\tлок демонов», значение ключа — никогда. BARRIER — сколько
# вызовов alpha дождаться, прежде чем ответить (два демона в одну минуту).
RESET_A=$(( $(date +%s) + 7200 ))
cat > "$S/bin/claude" <<CLAUDE
#!/usr/bin/env bash
D="$DOOR"
sid=""; prev=""
for a in "\$@"; do [ "\$prev" = "--session-id" ] && sid="\$a"; prev="\$a"; done
if [ -n "\${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    s=\$(printf '%s' "\$CLAUDE_CODE_OAUTH_TOKEN" | sha256sum | cut -d' ' -f1)
    if [ "\$s" = "\$(cat "\$D/sha-a")" ]; then who=A; elif [ "\$s" = "\$(cat "\$D/sha-b")" ]; then who=B; else who=chuzhoj; fi
else
    who=net
fi
lok=без-LOG_DIR
if [ -n "\${LOG_DIR:-}" ]; then
    f="\$LOG_DIR/locks/лимит-демонов.lock"
    if [ ! -e "\$f" ]; then lok=нет-файла
    elif flock -n "\$f" true 2>/dev/null; then lok=свободен
    else lok=под-локом; fi
fi
printf '%s\t%s\t%s\n' "\$who" "\$sid" "\$lok" >> "\$D/claude-calls.log"
if [ "\$who" = B ]; then
    printf '{"type":"system","subtype":"init","model":"proba","session_id":"%s"}\n' "\$sid"
    printf '{"type":"assistant","message":{"content":[{"type":"text","text":"ответ модели B"}]},"session_id":"%s"}\n' "\$sid"
    printf '{"type":"result","subtype":"success","is_error":false,"result":"ответ модели B","session_id":"%s","num_turns":1,"usage":{"input_tokens":1,"output_tokens":1}}\n' "\$sid"
    exit 0
fi
if [ -s "\$D/barrier" ]; then
    n=\$(cat "\$D/barrier")
    for i in \$(seq 1 80); do
        [ "\$(grep -c '^A' "\$D/claude-calls.log" 2>/dev/null)" -ge "\$n" ] && break
        sleep 0.25
    done
fi
printf '{"type":"system","subtype":"init","model":"proba","session_id":"%s"}\n' "\$sid"
printf '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":$RESET_A}}\n'
printf '{"type":"result","subtype":"success","is_error":true,"api_error_status":429,"result":"You have hit your usage limit","session_id":"%s"}\n' "\$sid"
exit 1
CLAUDE
cat > "$S/bin/self-prompt.py" <<'SP'
import os, sys
a = sys.argv[1:]
out = a[a.index("--в") + 1]
os.makedirs(os.path.dirname(out), exist_ok=True)
open(out, "w", encoding="utf-8").write("Метка сборки: проба-limit\nличность\n")
SP
printf 'import sys\n' > "$S/bin/alert-task.py"
chmod +x "$S/bin/claude"
export CLAUDE_BIN="$S/bin/claude" SELF_PROMPT_PY="$S/bin/self-prompt.py" HARNESS_ALERT_TASK="$S/bin/alert-task.py" \
       TG_SEND="$S/proj/scripts/tg_send.sh"

door_reset() {  # $1 = очередь кодов («0 5»), $2 = на кого переключает код 0 (пусто — ни на кого)
    printf '%s\n' $1 > "$DOOR/codes"
    printf '%s' "${2:-}" > "$DOOR/switch_to"
    : > "$DOOR/calls.log"; : > "$DOOR/events.log"; : > "$DOOR/claude-calls.log"; rm -f "$DOOR/barrier"
}
door_calls() { count "$DOOR/calls.log" "${1:-.}"; }
# Живость нового пути — по журналу ВСЕХ вызовов двери за прогон (его door_reset
# не чистит): «не звана» засчитывается, только если тот же стенд её зовёт.
live() { [ "$(count "$DOOR/all-calls.log" "$1")" -gt 0 ] && echo жива || echo мертва; }
fresh_log() {  # $1 = имя → новый LOG_DIR
    mkdir -p "$S/runs/$1"; echo "$S/runs/$1"
}

# ═══ А-7: limit-watch.sh — дверь до метки, коды → метка/весть ══════════════
TR_DIR="$S/tr/$(printf '%s' "$S/proj" | sed 's|/|-|g')"
mkdir -p "$TR_DIR"
TRANSCRIPT="$TR_DIR/proba.jsonl"
lw_transcript() {  # $1 = сек назад успешный ответ, $2 = сек назад отказ 429 (или «нет»), $3 = resetsAt (или пусто)
    python3 -I - "$TRANSCRIPT" "$1" "$2" "${3:-}" <<'PY'
import datetime, json, sys, time
путь, ответ, отказ, сброс = sys.argv[1:]
метка = lambda сек: datetime.datetime.fromtimestamp(time.time() - int(сек), datetime.UTC).isoformat()
строки = [{"type": "user", "message": {"role": "user", "content": "задача"}, "timestamp": метка(int(ответ) + 5),
           "entrypoint": "cli"},
          {"type": "assistant", "message": {"model": "proba", "content": [{"type": "text", "text": "делаю"}]},
           "timestamp": метка(ответ)}]
if отказ != "нет":
    з = {"type": "assistant", "error": "rate_limit", "isApiErrorMessage": True, "apiErrorStatus": 429,
         "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API Error: Rate limit reached"}]},
         "timestamp": метка(отказ)}
    if сброс:
        з["resetsAt"] = int(сброс)
    строки.append(з)
open(путь, "w", encoding="utf-8").write("".join(json.dumps(с, ensure_ascii=False) + "\n" for с in строки))
PY
}
lw_run() {  # $1 = LOG_DIR, $2 = «с» (каталог аккаунтов с profiles.json) | «без» → код
    local log="$1" rc=0 acc
    if [ "${2:-с}" = без ]; then
        acc=(env -u HARNESS_ACCOUNT_DIR STATE_DIR="$S/state-bare")
    else
        acc=(env HARNESS_ACCOUNT_DIR="$ACC" STATE_DIR="$S/state")
    fi
    timeout 60 "${acc[@]}" LOG_DIR="$log" PROJECT_DIR="$S/proj" HEARTBEAT_DIR="$log/heartbeat" \
        CLAUDE_PROJECTS_DIR="$S/tr" bash "$S/proj/harness/demons/limit-watch.sh" >>"$log/run.out" 2>&1 || rc=$?
    echo "$rc"
}
mark_reason() { local f="$1/лимит-подписки.состояние"; [ -f "$f" ] && { awk '{print $2}' "$f"; return; }; echo нет; }
vests() { count "$1/kanal.txt"; }

echo "── А-7: лимит смены доказан → дверь до метки ──"
state_launch alpha
lw_transcript 1500 60 "$(( $(date +%s) + 7200 ))"
L=$(fresh_log lw-0); door_reset "0" beta
rc=$(lw_run "$L")
case_check "А-7 код 0: дверь позвана «--авто --от смена» ровно раз" 1 "$(door_calls '--авто --от смена')"
case_check "А-7 код 0: метки нет, вестей 0, выход 0" "нет|0|0" "$(mark_reason "$L")|$(vests "$L")|$rc"
case_check "А-7 код 0: строка об автопереходе в limit-watch.log" да "$(yes_no grep -q 'авто' "$L/limit-watch.log")"
printf '5\n' > "$DOOR/codes"
rc=$(lw_run "$L")
case_check "А-7 следующий обход (код 5): дверь позвана снова, метки нет, вестей 0" "2|нет|0" \
    "$(door_calls '--авто --от смена')|$(mark_reason "$L")|$(vests "$L")"

for code in 5 1; do
    L=$(fresh_log "lw-$code"); door_reset "$code"
    rc=$(lw_run "$L")
    case_check "А-7 код $code: дверь позвана, метки нет, вестей 0, выход 0" "1|нет|0|0" \
        "$(door_calls '--авто --от смена')|$(mark_reason "$L")|$(vests "$L")|$rc"
done
for code in 4 6; do
    L=$(fresh_log "lw-$code"); door_reset "$code"
    rc=$(lw_run "$L")
    case_check "А-7 код $code: метку и весть сделала дверь — limit-watch молчит, своей метки нет, выход 0" "1|нет|0|0" \
        "$(door_calls '--авто --от смена')|$(mark_reason "$L")|$(vests "$L")|$rc"
done
for code in 2 9; do
    L=$(fresh_log "lw-$code"); door_reset "$code"
    rc=$(lw_run "$L")
    case_check "А-7 код $code: дверь позвана" 1 "$(door_calls '--авто --от смена')"
    # Прежний путь: сбой автоперехода весть о лимите не глушит.
    case_check "А-7 код $code: прежний путь — метка limit, одна весть «Упёрся в лимит»" "limit|1|да" \
        "$(mark_reason "$L")|$(vests "$L")|$(yes_no grep -q 'Упёрся в лимит' "$L/kanal.txt")"
done
L=$(fresh_log lw-noacc); door_reset "0" beta
rc=$(lw_run "$L" без)
case_check "А-7 без profiles.json: у живой двери не звана, прежний путь (метка limit, весть)" "жива|0|limit|1" \
    "$(live "--авто --от смена")|$(door_calls)|$(mark_reason "$L")|$(vests "$L")"

echo "── М-7: вердикт двери помнится до нового 429 (код 1) и до часа сброса (код 4) ──"
# Ревью кода М-7: limit-watch ходит раз в минуту, и при том же старом 429 в
# транскрипте звал дверь — живой вызов ключом — каждую минуту часами. Вердикт
# лежит в $LOG_DIR/лимит-подписки.авто. Ловится: вердикт не пишется (дверь звана
# каждый обход); вердикт глушит и новый 429 (дверь не звана после нового отказа);
# вердикт кода 4 глушит час сброса.
state_launch alpha
lw_transcript 1500 60 "$(( $(date +%s) + 7200 ))"
L=$(fresh_log lw-m7-1); door_reset "1"
lw_run "$L" >/dev/null
M7_FIRST=$(door_calls '--авто --от смена')
lw_run "$L" >/dev/null
case_check "М-7 код 1, тот же старый 429: первый обход зовёт дверь, второй — нет" "1|1" \
    "$M7_FIRST|$(door_calls '--авто --от смена')"
case_check "М-7 код 1: вердикт записан в \$LOG_DIR/лимит-подписки.авто" да "$(yes_no test -s "$L/лимит-подписки.авто")"
sleep 2
lw_transcript 1500 0 "$(( $(date +%s) + 7200 ))"
b=$(door_calls '--авто --от смена')
lw_run "$L" >/dev/null
case_check "М-7 код 1: новый 429 новее вердикта → дверь звана снова" 1 "$(( $(door_calls '--авто --от смена') - b ))"
M7_RESET=$(( $(date +%s) + 7200 ))
lw_transcript 1500 60 "$M7_RESET"
L=$(fresh_log lw-m7-4); door_reset "4"
lw_run "$L" >/dev/null
# Код 4: метку limit со сбросом ближайшего ставит сама дверь — здесь за неё стенд.
printf '%s limit %s\n' "$(date +%s)" "$M7_RESET" > "$L/лимит-подписки.состояние"
b=$(door_calls '--авто --от смена')
lw_run "$L" >/dev/null
case_check "М-7 код 4 («все в лимите»): первый обход зовёт, до часа сброса следующий — нет" "1|0" \
    "$b|$(( $(door_calls '--авто --от смена') - b ))"
printf '%s limit %s\n' "$(( $(date +%s) - 3600 ))" "$(( $(date +%s) - 20 ))" > "$L/лимит-подписки.состояние"
lw_transcript 1200 нет
b=$(door_calls '--авто --от смена')
lw_run "$L" >/dev/null
case_check "М-7 код 4: в час сброса вердикт не держит — дверь звана перед пинком" 1 \
    "$(( $(door_calls '--авто --от смена') - b ))"
# Вне часа сброса вердикт кода 4 судит основной путь (метки нет — ветки пинка нет):
# сброс вердикта прошёл, а в транскрипте всё тот же старый 429 — дверь звана снова.
# Ловится: вердикт кода 4 держит и после своего часа сброса (случай выше идёт через
# ветку пинка, которая зовёт дверь без вердикта, и этого не видит).
L=$(fresh_log lw-m7-4past); door_reset "4"
lw_transcript 1500 60 "$(( $(date +%s) + 7200 ))"
printf '%s 4 %s\n' "$(( $(date +%s) - 3600 ))" "$(( $(date +%s) - 20 ))" > "$L/лимит-подписки.авто"
lw_run "$L" >/dev/null
case_check "М-7 код 4, сброс вердикта прошёл, метки нет → основной путь зовёт дверь" "нет|1" \
    "$(mark_reason "$L")|$(door_calls '--авто --от смена')"

echo "── А-7 / §1в.2 п. 3: час сброса — дверь перед пинком ──"
# Метка стоит, сброс 20 с назад, смена молчит 20 мин, свежих отказов нет:
# прежний код будит смену; новый сначала спрашивает дверь.
kick_case() {  # $1 = код двери → «звана|пинков|события|отметка пинка»
    local L sbros
    L=$(fresh_log "kick-$1"); door_reset "$1" beta
    sbros=$(( $(date +%s) - 20 ))
    printf '%s limit %s\n' "$(( $(date +%s) - 3600 ))" "$sbros" > "$L/лимит-подписки.состояние"
    lw_transcript 1200 нет
    lw_run "$L" >/dev/null
    KICK_LOG="$L"; KICK_SBROS="$sbros"
}
events_seq() { awk '{print $1}' "$DOOR/events.log" | paste -sd'|' -; }
kick_case 0
case_check "час сброса, код 0: дверь позвана, пинка нет, отметка пинка = сброс" "1|0|$KICK_SBROS" \
    "$(door_calls '--авто --от смена')|$(count "$DOOR/events.log" '^пинок')|$(cat "$KICK_LOG/лимит-подписки.пинок-сброса" 2>/dev/null)"
lw_run "$KICK_LOG" >/dev/null
case_check "час сброса, код 0: следующий обход дверь снова не зовёт (один вызов на сброс)" 1 \
    "$(door_calls '--авто --от смена')"
kick_case 1
case_check "час сброса, код 1 (текущий освободился): дверь, затем прежний пинок" "дверь|пинок" "$(events_seq)"
kick_case 4
case_check "час сброса, код 4: пинка нет" "1|0" "$(door_calls '--авто --от смена')|$(count "$DOOR/events.log" '^пинок')"
kick_case 2
case_check "час сброса, код 2: дверь, затем прежний пинок" "дверь|пинок" "$(events_seq)"

# ═══ А-8: claude-demon.sh — дверь при 77, один повтор, один ответ ══════════
dm_run() {  # $1 = LOG_DIR, $2 = имя демона, $3 = файл вывода → код
    local rc=0
    timeout 60 env LOG_DIR="$1" HEARTBEAT_DIR="$1/heartbeat" PROJECT_DIR="$S/proj" \
        bash "$S/proj/scripts/claude-demon.sh" "$2" -p "задание" >"$3" 2>"$3.err" || rc=$?
    echo "$rc"
}
starts() { jq -r 'select(.event == "demon.start") | .session_id' "$1/demon-spend.jsonl" 2>/dev/null | grep -c .; }
starts_uniq() { jq -r 'select(.event == "demon.start") | .session_id' "$1/demon-spend.jsonl" 2>/dev/null | sort -u | grep -c .; }
claude_who() { cut -f1 "$DOOR/claude-calls.log" | paste -sd' ' -; }

echo "── стенд демонов ──"
state_launch beta; door_reset "0"
L=$(fresh_log dm-stand)
rc=$(dm_run "$L" проба-демон "$L/out")
case_check "стенд: запуск=beta — цепочка claude-run → ключ профиля → ответ B, код 0" "0|B|ответ модели B" \
    "$rc|$(claude_who)|$(cat "$L/out")"

echo "── А-8: 77 на alpha, дверь 0 → повтор на beta ──"
state_launch alpha; door_reset "0" beta
L=$(fresh_log dm-0)
rc=$(dm_run "$L" проба-демон "$L/out")
case_check "А-8 код 0: дверь позвана «--авто --от демоны» ровно раз" 1 "$(door_calls '--авто --от демоны')"
case_check "А-8 код 0: дверь позвана под локом лимит-демонов.lock" под-локом "$(cut -f3 "$DOOR/calls.log" | head -1)"
case_check "А-8 код 0: повтор на beta, код 0" "A B|0" "$(claude_who)|$rc"
case_check "А-8 код 0: повтор вне лока" свободен "$(awk -F'\t' '$1 == "B" {print $3}' "$DOOR/claude-calls.log" | head -1)"
case_check "А-8 код 0: демону ОДИН ответ — ответ B, отказа в выводе нет" "1|1" \
    "$(count "$L/out")|$(count "$L/out" 'ответ модели B')"
case_check "А-8 код 0: в demon-spend.jsonl два demon.start с разными session_id" "2|2" "$(starts "$L")|$(starts_uniq "$L")"
case_check "А-8 код 0: claude получил разные --session-id" 2 "$(cut -f2 "$DOOR/claude-calls.log" | sort -u | grep -c .)"
case_check "А-8 код 0: вести демона нет" 0 "$(vests "$L")"

echo "── А-8: два демона в одну минуту → дверь 0 и 5, оба повторяют ──"
state_launch alpha; door_reset "0 5" beta; printf '2' > "$DOOR/barrier"
L=$(fresh_log dm-pair)
( rc=$(dm_run "$L" demon-odin "$L/out1"); echo "$rc" > "$L/rc1" ) &
( rc=$(dm_run "$L" demon-dva "$L/out2"); echo "$rc" > "$L/rc2" ) &
wait
case_check "А-8 пара: дверь позвана дважды, коды 0 и 5" "2|0 5" \
    "$(door_calls '--авто --от демоны')|$(cut -f2 "$DOOR/calls.log" | sort | paste -sd' ' -)"
case_check "А-8 пара: оба демона — код 0, у каждого один ответ B" "0|0|1|1|1|1" \
    "$(cat "$L/rc1" 2>/dev/null)|$(cat "$L/rc2" 2>/dev/null)|$(count "$L/out1")|$(count "$L/out1" 'ответ модели B')|$(count "$L/out2")|$(count "$L/out2" 'ответ модели B')"
case_check "А-8 пара: четыре вызова claude (A A B B), четыре разных session_id" "A A B B|4" \
    "$(claude_who | tr ' ' '\n' | sort | paste -sd' ' -)|$(starts_uniq "$L")"
case_check "А-8 пара: вестей 0" 0 "$(vests "$L")"

echo "── А-8: повтор снова 77 → выход 77 без вести ──"
state_launch alpha; door_reset "0"          # дверь отвечает 0, но запуск не меняет
L=$(fresh_log dm-again)
rc=$(dm_run "$L" проба-демон "$L/out")
case_check "А-8 повтор 77: дверь позвана, повтор был (A A)" "1|A A" "$(door_calls '--авто --от демоны')|$(claude_who)"
case_check "А-8 повтор 77: выход 77" 77 "$rc"
case_check "А-8 повтор 77: вести демона нет, строка в claude-demon.log" "0|да" \
    "$(vests "$L")|$(yes_no grep -q 'авто' "$L/claude-demon.log")"

for code in 4 6; do
    state_launch alpha; door_reset "$code"
    L=$(fresh_log "dm-$code")
    rc=$(dm_run "$L" проба-демон "$L/out")
    case_check "А-8 код $code: дверь позвана, повтора нет, выход 77, вести демона нет" "1|A|77|0" \
        "$(door_calls '--авто --от демоны')|$(claude_who)|$rc|$(vests "$L")"
done
for code in 1 2; do
    state_launch alpha; door_reset "$code"
    L=$(fresh_log "dm-$code")
    rc=$(dm_run "$L" проба-демон "$L/out")
    case_check "А-8 код $code: дверь позвана, повтора нет" "1|A" "$(door_calls '--авто --от демоны')|$(claude_who)"
    case_check "А-8 код $code: прежняя ветка — выход 77, одна весть демона" "77|1" "$rc|$(vests "$L")"
done
state_launch alpha; door_reset "0" beta
L=$(fresh_log dm-noacc)
rc=$(timeout 60 env -u HARNESS_ACCOUNT_DIR STATE_DIR="$S/state-bare" LOG_DIR="$L" HEARTBEAT_DIR="$L/heartbeat" \
     PROJECT_DIR="$S/proj" bash "$S/proj/scripts/claude-demon.sh" проба-демон -p "задание" >"$L/out" 2>&1; echo $?)
case_check "А-8 без profiles.json: у живой двери не звана, прежняя ветка (77, весть)" "жива|0|77|1" \
    "$(live "--авто --от демоны")|$(door_calls)|$rc|$(vests "$L")"

# ═══ limit_mark: новые функции, А-16, вход_после по переходу ══════════════
LM_LOG="$S/lm-log"; LM_ACC="$S/lm-acc"; LM_TR="$S/lm-tr.jsonl"
mkdir -p "$LM_LOG" "$LM_ACC"; : > "$LM_TR"
lm() {  # $1 = код python: модуль lm, NOW, TR; кладёт ответ в «итог» → печать
    timeout 30 env LOG_DIR="$LM_LOG" HARNESS_ACCOUNT_DIR="$LM_ACC" \
        python3 - "$S/proj/scripts" "$LM_TR" "$1" <<'PY' 2>"$S/lm.err" || echo "ОТКАЗ:$(tail -1 "$S/lm.err")"
import contextlib, io, sys, time
sys.path.insert(0, sys.argv[1])
import limit_mark as lm
NOW, TR, итог = int(time.time()), sys.argv[2], None
вывод = io.StringIO()
with contextlib.redirect_stdout(вывод):
    exec(sys.argv[3])
print(итог)
PY
}
lm_reset() { rm -rf "$LM_LOG" "$LM_ACC"; mkdir -p "$LM_LOG" "$LM_ACC"; }
mark_file() { printf '%s %s %s\n' "$1" "$2" "$3" > "$LM_LOG/лимит-подписки.состояние"; }
lm_state_switch() {  # $1 = сек назад, $2 = исход
    python3 -I - "$LM_ACC/state.json" "$1" "$2" <<'PY'
import datetime, json, sys, time
путь, назад, исход = sys.argv[1], int(sys.argv[2]), sys.argv[3]
когда = datetime.datetime.fromtimestamp(time.time() - назад).astimezone().isoformat(timespec="seconds")
json.dump({"запуск": "beta", "идёт": "beta",
           "переключения": [{"время": когда, "что": "итог", "откуда": "alpha", "куда": "beta", "исход": исход}]},
          open(путь, "w", encoding="utf-8"), ensure_ascii=False)
PY
}
MARK_FIELDS='open(lm.файл(), encoding="utf-8").read().split() if lm.файл().exists() else ["нет", "нет", "нет"]'

echo "── limit_mark.поставить_все_в_лимите ──"
R=$(( $(date +%s) + 5 * 3600 )); R2=$(( R + 600 ))
lm_reset
case_check "все_в_лимите: метки нет → 0, метка limit с этим сбросом" "0|limit|$R" \
    "$(lm "к = lm.поставить_все_в_лимите($R, 'BETA'); п = $MARK_FIELDS; итог = f'{к}|{п[1]}|{п[2]}'")"
case_check "все_в_лимите: стоит limit с будущим сбросом → 1, метка не тронута" "1|$R" \
    "$(lm "к = lm.поставить_все_в_лимите($R2, 'BETA'); итог = f'{к}|{($MARK_FIELDS)[2]}'")"
lm_reset; mark_file "$(( $(date +%s) - 7200 ))" limit "$(( $(date +%s) - 60 ))"
case_check "все_в_лимите: limit с прошедшим сбросом заменяется → 0, новый сброс" "0|limit|$R" \
    "$(lm "к = lm.поставить_все_в_лимите($R, 'BETA'); п = $MARK_FIELDS; итог = f'{к}|{п[1]}|{п[2]}'")"
lm_reset; mark_file "$(( $(date +%s) - 600 ))" nemota 0
case_check "все_в_лимите: громкая чужая (nemota) → 1, не тронута" "1|nemota" \
    "$(lm "к = lm.поставить_все_в_лимите($R, 'BETA'); итог = f'{к}|{($MARK_FIELDS)[1]}'")"
lm_reset; mark_file "$(( $(date +%s) - 600 ))" blocked 0
case_check "все_в_лимите: тихая blocked вытесняется → 0, limit" "0|limit" \
    "$(lm "к = lm.поставить_все_в_лимите($R, 'BETA'); итог = f'{к}|{($MARK_FIELDS)[1]}'")"
lm_reset; chmod 555 "$LM_LOG"
if [ -w "$LM_LOG" ]; then
    echo "  (запуск от root: каталог только для чтения не мешает — случай сбоя не судится)"
else
    case_check "все_в_лимите: метку не поставить → код ≥2, без исключения" "сбой" \
        "$(lm "к = lm.поставить_все_в_лимите($R, 'BETA'); итог = 'сбой' if isinstance(к, int) and к >= 2 else к")"
fi
chmod 755 "$LM_LOG"

echo "── М-6: два процесса в минуту сброса вытесняют метку с прошедшим сбросом → поставила одна ──"
# Ревью кода М-6: «прочитал прошедший сброс → снял → поставил» не атомарно: второй
# процесс снимал уже НОВУЮ метку первого и ставил свою — два кода 0, две вести.
# Барьер на первом чтении метки: оба увидели прошедший сброс, первый идёт сразу,
# второй — через 0,5 с (худший порядок). Барьер ждёт не дольше 5 с: код, который
# метку через прочитать() не читает, барьер не держит — тогда случай судит
# обычную гонку без принуждения.
cat > "$S/m6.py" <<'PY'
import os, sys, time
sys.path.insert(0, sys.argv[1])
import limit_mark as lm
роль, барьер, сброс = sys.argv[2], sys.argv[3], int(sys.argv[4])
прежнее, прочли = lm.прочитать, []
def прочитать():
    итог = прежнее()
    if not прочли:
        прочли.append(1)
        open(os.path.join(барьер, роль), "w").close()
        срок = time.time() + 5
        while time.time() < срок and len(os.listdir(барьер)) < 2:
            time.sleep(0.02)
        if роль == "второй":
            time.sleep(0.5)
    return итог
lm.прочитать = прочитать
print(lm.поставить_все_в_лимите(сброс, "BETA"))
PY
lm_reset; mark_file "$(( $(date +%s) - 7200 ))" limit "$(( $(date +%s) - 60 ))"
rm -rf "$S/m6-bar"; mkdir -p "$S/m6-bar"
for role in первый второй; do
    timeout 30 env LOG_DIR="$LM_LOG" HARNESS_ACCOUNT_DIR="$LM_ACC" \
        python3 "$S/m6.py" "$S/proj/scripts" "$role" "$S/m6-bar" "$R" > "$S/m6-$role.out" 2>&1 &
done
wait
case_check "М-6 метка limit со сбросом в прошлом, два процесса разом → коды «0 1», метка limit с новым сбросом" \
    "0 1|limit|$R" \
    "$(cat "$S/m6-первый.out" "$S/m6-второй.out" | sort | paste -sd' ')|$(awk '{print $2"|"$3}' "$LM_LOG/лимит-подписки.состояние" 2>/dev/null)"

echo "── limit_mark.весть_все_в_лимите, снять_молча, последний_переход ──"
case_check "весть_все_в_лимите: текст с подписью и часом" \
    "Все аккаунты в лимите. Первым освободится BETA в $(date -d "@$R" +%H:%M) — перейду на него сам." \
    "$(lm "итог = lm.весть_все_в_лимите('BETA', $R)")"
lm_reset; mark_file "$(( $(date +%s) - 600 ))" limit "$R"; date +%s > "$LM_LOG/лимит-подписки.пинок"
case_check "снять_молча: True, метки и пинка нет, ничего не напечатано" "True|нет|нет|тихо" \
    "$(lm "р = lm.снять_молча(); итог = f'{р}|{\"есть\" if lm.файл().exists() else \"нет\"}|{\"есть\" if (lm.каталог() / lm.ПИНОК).exists() else \"нет\"}|{\"тихо\" if not вывод.getvalue() else \"печать\"}'")"
lm_reset
case_check "последний_переход: строк переключения нет → 0" 0 "$(lm "итог = int(lm.последний_переход())")"
lm_state_switch 120 ок
case_check "последний_переход: строка «ок» 120 с назад → её время" да \
    "$(lm "итог = 'да' if abs(lm.последний_переход() - (NOW - 120)) <= 3 else lm.последний_переход()")"

echo "── limit_mark.вход_после: переход по ключу — тоже смена входа (Ф-42) ──"
lm_reset; lm_state_switch 300 ок
VP_LIVE=$(lm "итог = lm.вход_после(TR, NOW - 600)")
case_check "вход_после: «ок» новее метки → True" True "$VP_LIVE"
lm_reset; lm_state_switch 900 ок
case_check "вход_после: «ок» старше метки → False (у живого разбора)" "True|False" "$VP_LIVE|$(lm "итог = lm.вход_после(TR, NOW - 600)")"
lm_reset; lm_state_switch 300 "не перешёл"
case_check "вход_после: исход не «ок» → False (у живого разбора)" "True|False" "$VP_LIVE|$(lm "итог = lm.вход_после(TR, NOW - 600)")"
lm_reset; lm_state_switch 300 ок; mark_file "$(( $(date +%s) - 600 ))" limit "$R"
case_check "лимит_держится: метка старше перехода по ключу → нет" False "$(lm "итог = lm.лимит_держится(TR)")"

echo "── А-16: длинное окно — возраст метки со сбросом от max(когда, сброс) ──"
H7=$(( $(date +%s) - 7 * 3600 ))
lm_reset; mark_file "$H7" limit "$(( H7 + 11 * 3600 ))"
case_check "А-16 сброс через 11 ч, возраст 7 ч → лимит_держится = да" True "$(lm "итог = lm.лимит_держится(TR)")"
case_check "А-16 снять(): сброс впереди, возраст 7 ч → весть не пуста" "limit|непусто" \
    "$(lm "_, п, в = lm.снять(TR); итог = f'{п}|{\"непусто\" if в else \"пусто\"}'")"
lm_reset; mark_file "$H7" limit 0
case_check "А-16 без сброса, 7 ч → лимит_держится = нет (как было)" False "$(lm "итог = lm.лимит_держится(TR)")"
case_check "А-16 снять(): без сброса, 7 ч → весть пуста (как было)" "limit|пусто" \
    "$(lm "_, п, в = lm.снять(TR); итог = f'{п}|{\"непусто\" if в else \"пусто\"}'")"

# ═══ limit-in-log: --после, --метка с переходом, --стоит-в-лимите (А-14) ═══
LIL_TR="$S/lil.jsonl"
lil() {  # доводы limit-in-log.py → «код|stdout»
    local out rc=0
    out=$(timeout 30 env LOG_DIR="$LM_LOG" HARNESS_ACCOUNT_DIR="$LM_ACC" \
          python3 "$S/proj/scripts/limit-in-log.py" "$@" 2>/dev/null) || rc=$?
    echo "$rc|$out"
}
lil_code() { lil "$@" | cut -d'|' -f1; }
NOW=$(date +%s)
python3 -I - "$LIL_TR" "$(( NOW + 3600 ))" <<'PY'
import datetime, json, sys, time
путь, сброс = sys.argv[1], int(sys.argv[2])
м = lambda сек: datetime.datetime.fromtimestamp(time.time() - сек, datetime.UTC).isoformat()
строки = [{"type": "assistant", "message": {"model": "proba", "content": [{"type": "text", "text": "делаю"}]}, "timestamp": м(1500)},
          {"type": "assistant", "error": "rate_limit", "isApiErrorMessage": True, "apiErrorStatus": 429,
           "resetsAt": сброс, "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API Error"}]},
           "timestamp": м(600)}]
open(путь, "w", encoding="utf-8").write("".join(json.dumps(с) + "\n" for с in строки))
PY
echo "── limit-in-log --действует … --после ──"
lm_reset
case_check "стенд: отказ 429 600 с назад со сбросом впереди — лимит держится" 0 "$(lil_code --действует "$LIL_TR" --с-отказом)"
POSLE_LIVE=$(lil --действует "$LIL_TR" --после "$(( NOW - 300 ))")
case_check "--после новее отказа → старый 429 отброшен: код 1, «0»" "1|0" "$POSLE_LIVE"
case_check "--после старше отказа → лимит держится (у живого --после)" "1|0|0" \
    "$POSLE_LIVE|$(lil_code --действует "$LIL_TR" --после "$(( NOW - 900 ))")"
lm_state_switch 300 ок
METKA_LIVE=$(lil_code --действует "$LIL_TR" --метка)
case_check "--метка: переход «ок» новее отказа → лимит не держится (код 1)" 1 "$METKA_LIVE"
lm_state_switch 900 ок
case_check "--метка: переход старше отказа → держится (у живого разбора)" "1|0" \
    "$METKA_LIVE|$(lil_code --действует "$LIL_TR" --метка)"

echo "── А-14 разбор: limit-in-log --стоит-в-лимите <транскрипт помощника> ──"
helper_tr() {  # $1 = файл, дальше виды записей по порядку: ответ | 429 | вызов:<id> | итог:<id>
    local f="$1"; shift
    python3 -I - "$f" "$@" <<'PY'
import datetime, json, sys, time
путь, виды = sys.argv[1], sys.argv[2:]
строки = []
for н, вид in enumerate(виды):
    t = datetime.datetime.fromtimestamp(time.time() - 600 + н * 30, datetime.UTC).isoformat()
    if вид == "ответ":
        з = {"type": "assistant", "message": {"model": "proba", "content": [{"type": "text", "text": "готово"}]}}
    elif вид == "429":
        з = {"type": "assistant", "error": "rate_limit", "isApiErrorMessage": True, "apiErrorStatus": 429,
             "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API Error: Rate limit reached"}]}}
    elif вид.startswith("вызов:"):
        з = {"type": "assistant", "message": {"model": "proba", "content": [
            {"type": "tool_use", "id": вид[6:], "name": "Bash", "input": {"command": "sleep 600"}}]}}
    else:
        з = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": вид[5:], "content": "ок"}]}}
    з["timestamp"] = t
    строки.append(з)
open(путь, "w", encoding="utf-8").write("".join(json.dumps(с, ensure_ascii=False) + "\n" for с in строки))
PY
}
helper_tr "$S/h1.jsonl" ответ 429
STOIT_LIVE=$(lil_code --стоит-в-лимите "$S/h1.jsonl")
case_check "А-14 последняя запись — отказ 429 → стоит (0)" 0 "$STOIT_LIVE"
helper_tr "$S/h2.jsonl" 429 ответ
case_check "А-14 после отказа есть ответ → не стоит (1, у живого разбора)" "0|1" "$STOIT_LIVE|$(lil_code --стоит-в-лимите "$S/h2.jsonl")"
helper_tr "$S/h3.jsonl" ответ вызов:toolu_1
case_check "А-14 открытый tool_use без tool_result → не стоит (1, у живого разбора)" "0|1" "$STOIT_LIVE|$(lil_code --стоит-в-лимите "$S/h3.jsonl")"
helper_tr "$S/h4.jsonl" вызов:toolu_1 429
case_check "А-14 открытый tool_use и последний 429 → держит, не стоит (1, у живого разбора)" "0|1" "$STOIT_LIVE|$(lil_code --стоит-в-лимите "$S/h4.jsonl")"
helper_tr "$S/h5.jsonl" вызов:toolu_1 итог:toolu_1 429
case_check "А-14 tool_use закрыт tool_result, затем 429 → стоит (0)" 0 "$(lil_code --стоит-в-лимите "$S/h5.jsonl")"
case_check "А-14 транскрипт не прочесть → 2" 2 "$(lil_code --стоит-в-лимите "$S/net-takogo.jsonl")"

# ═══ А-18: фиктивных ключей и имён их файлов нет в журналах сигналов ═══════
echo "── А-18: ключей нет в limit-watch.log и claude-demon.log ──"
leaks() {  # $1 = имя журнала → число вхождений ключей и имён файлов ключей
    local n=0 d f s
    for d in "$S"/runs/*/; do
        f="$d$1"; [ -f "$f" ] || continue
        for s in "$KEY_A" "$KEY_B" "$KF_A" "$KF_B"; do
            n=$(( n + $(grep -cF -- "$s" "$f") ))
        done
    done
    echo "$n"
}
case_check "А-18 limit-watch.log: у живого нового пути ключей и имён файлов ключей 0" "жива|0" "$(live "--авто --от смена")|$(leaks limit-watch.log)"
case_check "А-18 claude-demon.log: у живого нового пути ключей и имён файлов ключей 0" "жива|0" "$(live "--авто --от демоны")|$(leaks claude-demon.log)"

printf '\nСИГНАЛЫ ЛИМИТА (1в, группа Б): случаев %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
