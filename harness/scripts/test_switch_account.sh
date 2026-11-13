#!/usr/bin/env bash
# Пробы ЯДРА переключения смены на аккаунт по ключу setup-token
# (задача hr.switch-account, срез «ядро» 11.11.2026, план .tasks/hr.switch-account/план.md).
#
# Что держат пробы. Смена поднимается через komanda_agenta; между срывом
# чужого носителя (`env -u CLAUDE_CODE_OAUTH_TOKEN`) и носителем личности
# (claude-run.sh) встаёт обёртка scripts/as-account.sh: она читает поле
# `запуск` из state.json и кладёт ключ профиля в ОКРУЖЕНИЕ claude. Беды, которые
# здесь ловятся: смена поднялась не на том ключе; ключ виден в argv, выводе или
# вести; обёртка споткнулась и смена не встала; канал засыпан вестями
# respawn-петли; две записи state.json потеряли одну; сторож входа кричит на
# законный профиль или молчит на чужой ключ; проверка после подъёма судит
# файл, а не процесс.
#
# Стенд свой: временный каталог, свой сервер tmux (TMUX_TMPDIR, TMUX снят),
# подставной claude (пишет sha256 своей CLAUDE_CODE_OAUTH_TOKEN, а не значение),
# подставной tg_send с журналом, подставной API организации на 127.0.0.1
# (дверь ANTHROPIC_ORG_URL в check-login-expiry.организация()), фиктивные
# ключи, рождённые прогоном. Боевые ключи, state и канал не трогаются.
#
# Двери, которые пробы ждут от кода:
#   HARNESS_ACCOUNT_DIR  каталог profiles.json/state.json/.lock (умолч. $STATE_DIR/account)
#   SECRETS_DIR          где лежат файлы ключей (поле «ключ» профиля — имя файла)
#   TG_SEND              чем слать весть владельцу (как у claude-demon.sh)
#   ANTHROPIC_ORG_URL    адрес живого вызова организация()
#   CLAUDE_BIN, SELF_PROMPT_PY, HARNESS_ALERT_TASK — двери claude-run.sh
#   ACCOUNT_WARDEN       чем account-switch.py будит сторожа (этап 1б; умолч. сторож рядом со скриптом)
#   HARNESS_SESSION_WARDEN  чей сторож судят пробы 1б-С (дверь мутаций)
#
#   bash scripts/test_switch_account.sh
# Код возврата: 0 — все случаи прошли, 1 — есть упавший.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
LIB="$HERE/lib/config.sh"
WRAP="$HERE/as-account.sh"
DOOR="$HERE/account-switch.py"
LOGIN_PY="$HERE/check-login-expiry.py"
source "$HERE/lib/selftest_cases.sh"

# Свой сервер tmux: унаследованная $TMUX увела бы вызовы на боевой. Имя
# сервера своё (-L): сервер по умолчанию из TMUX_TMPDIR, переживший стенд,
# судья смен читал как гнездо агента. Обёртка первой в PATH ведёт туда и
# голый tmux двери.
unset TMUX TMUX_PANE
S="$(mktemp -d "${TMPDIR:-/tmp}/switch-acc-XXXXXX")"
export TMUX_TMPDIR="$S/tmux"
mkdir -p "$TMUX_TMPDIR" "$S/bin" "$S/home" "$S/log" "$S/secrets" "$S/acc" "$S/out" "$S/empty" "$S/tmux-bin"
REAL_TMUX="$(command -v tmux)"
SOCK_NAME="switch-acc-$$"
printf '#!/bin/sh\nexec "%s" -L "%s" "$@"\n' "$REAL_TMUX" "$SOCK_NAME" > "$S/tmux-bin/tmux"
chmod +x "$S/tmux-bin/tmux"
export PATH="$S/tmux-bin:$PATH"
chmod 700 "$S/secrets"
API_PID=""
cleanup() {
    "$REAL_TMUX" -L "$SOCK_NAME" kill-server 2>/dev/null || true
    [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null
    rm -rf "$S"
}
trap cleanup EXIT

yes_no() { "$@" >/dev/null 2>&1 && echo да || echo нет; }
sha() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }

# ── фиктивные ключи и организации (рождены прогоном, не настоящие) ─────────
rnd() { head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
KEY_A="proba-klyuch-alpha-$(rnd)"
KEY_B="proba-klyuch-beta-$(rnd)"
KEY_G="proba-klyuch-gamma-$(rnd)"
KEY_D="proba-klyuch-delta-$(rnd)"
KEY_FOREIGN="proba-klyuch-chuzhoj-$(rnd)"
KEY_DEAD="proba-klyuch-mertvyj-$(rnd)"   # API его не знает: 401
ORG_A="0rg-aaaa-$(rnd)"; ORG_B="0rg-bbbb-$(rnd)"; ORG_C="0rg-cccc-$(rnd)"
ALL_KEYS=("$KEY_A" "$KEY_B" "$KEY_G" "$KEY_D" "$KEY_FOREIGN" "$KEY_DEAD")
printf '%s' "$KEY_A" > "$S/secrets/claude-oauth-alpha"
printf '%s' "$KEY_B" > "$S/secrets/claude-oauth-beta"
printf '%s' "$KEY_G" > "$S/secrets/claude-oauth-gamma"
printf '%s' "$KEY_D" > "$S/secrets/claude-oauth-delta"
printf '%s' "$KEY_DEAD" > "$S/secrets/claude-oauth-dead"
: > "$S/secrets/claude-oauth-empty"
# Тот же ключ alpha, но файл с концом строки CRLF и пробелом: дверь судит ключ
# без краевых пробелов, и в окружение обязан уйти ровно он, а не «ключ\r».
printf ' %s \r\n' "$KEY_A" > "$S/secrets/claude-oauth-crlf"
printf '%s' "$KEY_FOREIGN" > "$S/outside-secrets"
chmod 600 "$S"/secrets/*

# ── конфиг стенда ──────────────────────────────────────────────────────────
: > "$S/home/install.conf"
printf 'TMUX_SESSION="switch-proba"\nLOGIN_EXPECTED_ORG="%s %s %s"\n' "$ORG_A" "$ORG_B" "$ORG_C" \
    > "$S/home/harness.conf"
export HARNESS_HOME="$S/home" HARNESS_CONF="$S/home/harness.conf" HARNESS_INSTALL_CONF="$S/home/install.conf"
export LOGIN_ORG_TIMEOUT=5
export LOG_DIR="$S/log" STATE_DIR="$S/state" SECRETS_DIR="$S/secrets" HARNESS_ACCOUNT_DIR="$S/acc"
# Файл входа /login — свой: его время судит «вход в сессии» (случаи 1б-В); боевой не читается.
export CLAUDE_CREDENTIALS="$S/home/credentials.json"
unset PROJECT_DIR INSTALL_CONF CLAUDE_CODE_OAUTH_TOKEN

# ── подставной claude: sha своей переменной и доводы, значение не пишет ────
cat > "$S/bin/claude" <<'CLAUDE'
#!/usr/bin/env bash
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN+x}" ]; then
    printf '%s' "$CLAUDE_CODE_OAUTH_TOKEN" | sha256sum | cut -d' ' -f1 > "$CLAUDE_TRACE.sha"
else
    printf 'нет' > "$CLAUDE_TRACE.sha"
fi
printf '%s\0' "$@" > "$CLAUDE_TRACE.args"
# Держится, пока цел её стенд: прогон, убитый KILL-ом, ловушку не зовёт, а
# вечная заглушка под именем claude жила бы после него (улика 11.11 05:44).
[ "${CLAUDE_STAY:-0}" = 1 ] && while [ -e "$0" ]; do sleep 1; done
exit 0
CLAUDE
cat > "$S/bin/self-prompt.py" <<'COLLECTOR'
import os, sys
a = sys.argv[1:]
out = a[a.index("--в") + 1]
os.makedirs(os.path.dirname(out), exist_ok=True)
open(out, "w", encoding="utf-8").write("Метка сборки: проба-switch\nличность\n")
COLLECTOR
cat > "$S/bin/alert-task.py" <<'ALERT'
import os, sys
open(os.environ["ALERT_OUT"], "a", encoding="utf-8").write(" ".join(sys.argv[1:]) + "\n")
ALERT
cat > "$S/bin/tg_send.sh" <<'TG'
#!/usr/bin/env bash
printf '%s\n' "$*" | tr '\n' ' ' >> "$TG_OUT"; printf '\n' >> "$TG_OUT"
exit 0
TG
chmod +x "$S/bin/claude" "$S/bin/tg_send.sh"
export CLAUDE_BIN="$S/bin/claude" SELF_PROMPT_PY="$S/bin/self-prompt.py" \
       HARNESS_ALERT_TASK="$S/bin/alert-task.py" TG_SEND="$S/bin/tg_send.sh" \
       HARNESS_TG_SEND="$S/bin/tg_send.sh" TG_OUT="$S/out/tg.log" CLAUDE_TRACE="$S/out/trace" \
       ALERT_OUT="$S/out/alert.log"
: > "$TG_OUT"; : > "$ALERT_OUT"

# ── подставной API организации: Bearer → sha → организация в заголовке ─────
cat > "$S/bin/org_api.py" <<'API'
import hashlib, http.server, json, os, sys
MAP = json.load(open(sys.argv[1], encoding="utf-8"))
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        key = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        digest = hashlib.sha256(key.encode()).hexdigest()
        org = MAP.get(digest, "")
        with open(sys.argv[3], "a", encoding="utf-8") as log:
            log.write(f"{self.path} {digest[:8]} {org or '-'}\n")
        self.send_response(200 if org else 401)
        if org:
            self.send_header("anthropic-organization-id", org)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")
    def log_message(self, *a):
        pass
srv = http.server.HTTPServer(("127.0.0.1", 0), H)
open(sys.argv[2], "w").write(str(srv.server_address[1]))
srv.serve_forever()
API
printf '{"%s":"%s","%s":"%s","%s":"%s","%s":"%s","%s":"%s"}\n' \
    "$(sha "$KEY_A")" "$ORG_A" "$(sha "$KEY_B")" "$ORG_B" "$(sha "$KEY_G")" "$ORG_C" \
    "$(sha "$KEY_D")" "$ORG_A" "$(sha "$KEY_FOREIGN")" "$ORG_A" > "$S/api-map.json"
python3 -I "$S/bin/org_api.py" "$S/api-map.json" "$S/api.port" "$S/out/api.log" 2>"$S/out/api.err" &
API_PID=$!
for _ in $(seq 50); do [ -s "$S/api.port" ] && break; sleep 0.1; done
export ANTHROPIC_ORG_URL="http://127.0.0.1:$(cat "$S/api.port" 2>/dev/null)/v1/messages"

# ── данные: профили (фикстура) и запись state.json ─────────────────────────
write_profiles() {
    python3 -I - "$S/acc/profiles.json" "$KEY_A" "$KEY_B" "$KEY_G" "$ORG_A" "$ORG_B" <<'PY'
import hashlib, json, sys
path, ka, kb, kg, oa, ob = sys.argv[1:]
fp = lambda k: hashlib.sha256(k.encode()).hexdigest()[:8]
p = lambda n, o, f, k: {"имя": n, "подпись": n.upper(), "орг": o, "почта": "",
                        "ключ": f, "отпечаток": fp(k) if k else ""}
json.dump({"профили": [
    p("alpha", oa, "claude-oauth-alpha", ka),
    p("beta", ob, "claude-oauth-beta", kb),
    # орг профиля B, а API отвечает на ключ gamma организацией C — расхождение
    p("gamma", ob, "claude-oauth-gamma", kg),
    p("nokey", oa, "claude-oauth-net-takogo", ""),
    p("emptykey", oa, "claude-oauth-empty", ""),
    p("sneaky", oa, "../outside-secrets", ""),
    p("dead", oa, "claude-oauth-dead", ""),
    p("crlf", oa, "claude-oauth-crlf", ka),
    # другой ключ (delta), а организация та же, что у alpha: расхождение видно
    # только по самому ключу процесса, по организации его не поймать
    p("dlt", oa, "claude-oauth-delta", ""),
]}, open(path, "w", encoding="utf-8"), ensure_ascii=False)
PY
}
state() {  # $1 = запуск, $2 = идёт (может быть пусто)
    printf '{"запуск": "%s", "идёт": "%s", "переключения": []}\n' "$1" "${2:-}" > "$S/acc/state.json"
}
state_field() {  # $1 = поле → значение или «нет-файла»/«битый»
    python3 -I -c 'import json,sys
try: print(json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2], ""))
except FileNotFoundError: print("нет-файла")
except ValueError: print("битый")' "$S/acc/state.json" "$1"
}

# ── подъём смены настоящим komanda_agenta (без tmux) ───────────────────────
N=0
run_shift() {  # [VAR=знач…] — доп. окружение; печатает путь файла вывода
    N=$((N + 1))
    rm -f "$CLAUDE_TRACE".*
    ( cd "$S/empty" && env "$@" bash -c 'source "$1"; eval "$(komanda_agenta "claude --model proba-model --dangerously-skip-permissions")"' \
        _ "$LIB" ) > "$S/out/run-$N.log" 2>&1
}
traced_sha() { cat "$CLAUDE_TRACE.sha" 2>/dev/null || echo не-запущен; }
args_have() { tr '\0' '\n' < "$CLAUDE_TRACE.args" 2>/dev/null | grep -qxF -- "$1"; }
tg_count() { grep -c . "$TG_OUT" 2>/dev/null || true; }

echo "── стенд ──"
case_check "стенд: подставной API отвечает организацией по ключу" "$ORG_A" \
    "$(python3 -I -c 'import sys,urllib.request as u
r=u.Request(sys.argv[1],data=b"{}",headers={"Authorization":"Bearer "+sys.argv[2]})
print(u.urlopen(r,timeout=5).headers.get("anthropic-organization-id",""))' "$ANTHROPIC_ORG_URL" "$KEY_A" 2>/dev/null)"
run_shift
case_check "стенд: komanda_agenta поднимает подставной claude (цепочка жива)" да "$(yes_no test -s "$CLAUDE_TRACE.args")"

echo "── П-1: state=профиль → процесс несёт ключ профиля (через komanda_agenta) ──"
TEXT="$(bash -c 'source "$1"; komanda_agenta "claude --model m --dangerously-skip-permissions"' _ "$LIB")"
printf '%s\n' "$TEXT" > "$S/out/komanda.txt"
case_check "П-1 komanda_agenta: env -u … as-account.sh … claude-run.sh --класс оркестратор" да \
    "$(yes_no grep -qE '^env -u CLAUDE_CODE_OAUTH_TOKEN bash [^ ]*/scripts/as-account\.sh bash [^ ]*/scripts/claude-run\.sh --класс оркестратор ' "$S/out/komanda.txt")"
case_check "П-1 обёртка есть и исполняема" да "$(yes_no test -x "$WRAP")"
write_profiles
state alpha
run_shift
P1_SHA="$(traced_sha)"
case_check "П-1 запуск=alpha: процесс несёт sha ключа alpha" "$(sha "$KEY_A")" "$P1_SHA"
case_check "П-1 claude-run.sh в цепочке: --append-system-prompt-file дошёл" да "$(yes_no args_have --append-system-prompt-file)"
case_check "П-1 доводы вызывающего дошли" да "$(yes_no args_have --dangerously-skip-permissions)"
state beta alpha
run_shift
case_check "П-1 A→B: запуск=beta — процесс несёт sha ключа beta" "$(sha "$KEY_B")" "$(traced_sha)"
state login alpha
run_shift
case_check "П-1 запуск=login — носителя нет" нет "$(traced_sha)"
state crlf
run_shift
case_check "П-1 файл ключа с пробелами и CRLF: в окружении ключ без краёв, как его судила дверь" "$(sha "$KEY_A")" "$(traced_sha)"

echo "── П-2: чужой носитель в оболочке ──"
state login
run_shift CLAUDE_CODE_OAUTH_TOKEN="$KEY_FOREIGN"
case_check "П-2 login: чужой ключ оболочки не доходит" нет "$(traced_sha)"
state alpha
run_shift CLAUDE_CODE_OAUTH_TOKEN="$KEY_FOREIGN"
case_check "П-2 профиль: чужой ключ заменён ключом профиля" "$(sha "$KEY_A")" "$(traced_sha)"

echo "── П-10: запасной путь login — смена встаёт, весть одна ──"
fallback_case() {  # $1 = имя, $2 = подготовка (eval); три подъёма подряд
    local log="$S/log-$1" before after
    mkdir -p "$log"; before=$(tg_count)
    eval "$2"
    local i shas=""
    for i in 1 2 3; do run_shift LOG_DIR="$log"; shas="$shas$(traced_sha) "; done
    after=$(tg_count)
    case_check "П-10 $1: смена встала трижды без носителя" "нет нет нет " "$shas"
    case_check "П-10 $1: весть владельцу ровно одна на три подъёма" 1 "$((after - before))"
}
fallback_case "state.json нет" 'write_profiles; rm -f "$S/acc/state.json"'
fallback_case "state.json битый" 'printf "{" > "$S/acc/state.json"'
fallback_case "неизвестный профиль" 'state ghost'
fallback_case "файла ключа нет" 'state nokey'
fallback_case "файл ключа пуст" 'state emptykey'
fallback_case "ключ вне SECRETS_DIR (../)" 'state sneaky'
# Антиповтор не вечный: после удачного подъёма новая беда — снова весть.
LOGR="$S/log-reset"; mkdir -p "$LOGR"; before=$(tg_count)
resolves() { grep -cxF -- "--resolve as-account:not-profile" "$ALERT_OUT" 2>/dev/null || true; }
state nokey; run_shift LOG_DIR="$LOGR"; run_shift LOG_DIR="$LOGR"
state alpha; run_shift LOG_DIR="$LOGR"; run_shift LOG_DIR="$LOGR"
resolved=$(resolves)
state nokey; run_shift LOG_DIR="$LOGR"; run_shift LOG_DIR="$LOGR"
case_check "П-10 беда → удача → беда: вестей две" 2 "$(( $(tg_count) - before ))"
# Весть о беде — работа модели: она заводит карточку, удача её снимает (owner-senders.yaml, card).
case_check "П-10 каждая весть о беде заводит карточку as-account:not-profile" "$(tg_count)" \
    "$(grep -cF -- "--task as-account:not-profile" "$TG_OUT" || true)"
case_check "П-10 удача после беды снимает карточку ровно раз (два удачных подъёма)" 1 "$resolved"
# Возможность не заведена (каталога нет) — тишина: не шуметь копиям без аккаунтов.
before=$(tg_count)
run_shift HARNESS_ACCOUNT_DIR="$S/net-kataloga" LOG_DIR="$S/log-off"
run_shift HARNESS_ACCOUNT_DIR="$S/net-kataloga" LOG_DIR="$S/log-off"
case_check "П-10 каталога аккаунтов нет: смена на login" нет "$(traced_sha)"
case_check "П-10 каталога аккаунтов нет: вестей нет" 0 "$(( $(tg_count) - before ))"

echo "── П-11: дверь записи state.json ──"
write_profiles; rm -f "$S/acc/state.json"
pids=()
for i in $(seq 10); do
    if [ $((i % 2)) = 0 ]; then who=alpha; else who=beta; fi
    python3 "$DOOR" --запуск "$who" > "$S/out/door-$i.log" 2>&1 &
    pids+=("$!")
done
wait "${pids[@]}"   # не голый wait: он ждал бы и подставной API
case_check "П-11 десять записей разом: файл цел и переключений 10" "10" \
    "$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print(len(d["переключения"]) if d.get("запуск") in ("alpha","beta") else "запуск-не-тот")' \
       "$S/acc/state.json" 2>/dev/null || echo нет-или-битый)"
for i in $(seq 15); do python3 "$DOOR" --запуск alpha >/dev/null 2>&1; done
case_check "П-11 переключений хранится не больше 20" "20" \
    "$(python3 -I -c 'import json,sys; print(len(json.load(open(sys.argv[1],encoding="utf-8"))["переключения"]))' "$S/acc/state.json" 2>/dev/null || echo нет-или-битый)"
# Отказ судится только у ЖИВОЙ двери: несуществующий скрипт тоже «отказывает»
# и файл не трогает — без этого условия пробы отказа зелены до кода.
state alpha
rc=0; python3 "$DOOR" --запуск beta > "$S/out/door-beta.log" 2>&1 || rc=$?
DOOR_LIVE="$([ "$rc" = 0 ] && [ "$(state_field запуск)" = beta ] && echo жива || echo мертва)"
case_check "П-11 дверь принимает известный профиль с ключом" жива "$DOOR_LIVE"
refuse_case() {  # $1 = цель, $2 = имя случая
    local was rc=0
    was=$(sha256sum < "$S/acc/state.json")
    python3 "$DOOR" --запуск "$1" > "$S/out/door-refuse.log" 2>&1 || rc=$?
    case_check "П-11 $2: у живой двери отказ (код не 0), файл не тронут" "жива|отказ|тот-же" \
        "$DOOR_LIVE|$([ "$rc" != 0 ] && echo отказ || echo принято)|$([ "$was" = "$(sha256sum < "$S/acc/state.json")" ] && echo тот-же || echo изменён)"
}
refuse_case ghost "неизвестный профиль"
refuse_case nokey "файла ключа нет"
refuse_case emptykey "файл ключа пуст"
refuse_case sneaky "ключ вне SECRETS_DIR"
refuse_case "../../x" "имя не [a-z0-9-]"
# Цель проверяется живым вызовом ДО записи: иначе смена поднимется на мёртвом
# ключе и не сможет даже откатить себя сама.
refuse_case dead "живой вызов: ключ не принят (401)"
refuse_case gamma "живой вызов: орг ключа ≠ орг профиля"
rc=0; python3 "$DOOR" --запуск login > "$S/out/door-login.log" 2>&1 || rc=$?
case_check "П-11 --запуск login принят" "0|login" "$rc|$(state_field запуск)"

echo "── П-12: дверь заведения профиля (орг подтверждается живым вызовом) ──"
rc=0; python3 "$DOOR" --профиль-добавить delta --подпись "Дельта" --орг "$ORG_A" --ключ claude-oauth-delta \
    > "$S/out/add-ok.log" 2>&1 || rc=$?
ADD="$rc|$(python3 -I -c 'import json,sys
d=[p for p in json.load(open(sys.argv[1],encoding="utf-8"))["профили"] if p.get("имя")=="delta"]
print(d[0].get("отпечаток","") if d else "нет")' "$S/acc/profiles.json" 2>/dev/null)"
case_check "П-12 орг совпала: профиль delta заведён, отпечаток sha256[:8]" "0|$(sha "$KEY_D" | cut -c1-8)" "$ADD"
ADD_LIVE="$([ "$ADD" = "0|$(sha "$KEY_D" | cut -c1-8)" ] && echo жива || echo мертва)"
add_refuse() {  # $1 = имя случая, дальше — доводы двери
    local name="$1" was rc=0; shift
    was=$(sha256sum < "$S/acc/profiles.json")
    python3 "$DOOR" --профиль-добавить "$@" > "$S/out/add-refuse.log" 2>&1 || rc=$?
    case_check "П-12 $name: у живой двери отказ, профили не тронуты" "жива|отказ|тот-же" \
        "$ADD_LIVE|$([ "$rc" != 0 ] && echo отказ || echo принято)|$([ "$was" = "$(sha256sum < "$S/acc/profiles.json")" ] && echo тот-же || echo изменён)"
}
add_refuse "орг не та, что назвал живой вызов" eps --подпись e --орг "$ORG_B" --ключ claude-oauth-delta
add_refuse "имя не [a-z0-9-]" Bad_Name --подпись x --орг "$ORG_A" --ключ claude-oauth-delta
add_refuse "файла ключа нет" eps --подпись e --орг "$ORG_A" --ключ claude-oauth-net-takogo
add_refuse "ключ вне SECRETS_DIR" eps --подпись e --орг "$ORG_A" --ключ ../outside-secrets
case_check "П-12 у живой двери ключа нет в profiles.json" "жива|нет" "$ADD_LIVE|$(yes_no grep -qF "$KEY_D" "$S/acc/profiles.json")"

echo "── П-7: сторож входа знает законный профиль ──"
write_profiles; state alpha
python3 -I - "$LOGIN_PY" "$KEY_A" "$KEY_FOREIGN" "$ORG_A" "$ORG_B" "$ORG_C" > "$S/out/p7.txt" 2>"$S/out/p7.err" <<'PY'
import importlib.util, sys
path, ka, kf, oa, ob, oc = sys.argv[1:]
def say(k, v): print(f"{k}\t{v}")
try:
    spec = importlib.util.spec_from_file_location("cle", path); m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
except Exception as e:
    say("загрузка", type(e).__name__); sys.exit(0)
def t(k, f):
    try: say(k, f())
    except Exception as e: say(k, "ОШИБКА " + type(e).__name__)
t("орг-дверь", lambda: m.организация(ka)[0] == oa)
t("профиль-alpha", lambda: (m.профиль_носителя(ka) or {}).get("имя"))
t("профиль-чужой", lambda: m.профиль_носителя(kf))
набор = (oa, ob, oc)
t("законный", lambda: m.вердикт_смены(ka, oa, набор, профиль=m.профиль_носителя(ka))[0])
t("законный-имя", lambda: "alpha" in m.вердикт_смены(ka, oa, набор, профиль=m.профиль_носителя(ka))[1])
t("чужой-орг-в-наборе", lambda: m.вердикт_смены(kf, oa, набор, профиль=m.профиль_носителя(kf))[0])
t("профиль-орг-не-та", lambda: m.вердикт_смены(ka, ob, набор, профиль=m.профиль_носителя(ka))[0])
t("ключа-в-строке-нет", lambda: ka not in m.вердикт_смены(ka, oa, набор, профиль=m.профиль_носителя(ka))[1])
# Ключ, негодный для заголовка (CR внутри), http.client отвергает ValueError,
# в тексте которой стоит ВЕСЬ заголовок — повод не должен его повторять.
t("повод-без-ключа", lambda: ka not in m.организация(ka + "\r")[1] and ka not in m.организация(ka + "\nx")[1])
PY
p7() { awk -F'\t' -v k="$1" '$1==k {print $2}' "$S/out/p7.txt"; }
case_check "П-7 организация() ходит по двери ANTHROPIC_ORG_URL" True "$(p7 орг-дверь)"
case_check "П-7 ключ alpha узнан как профиль запуска" alpha "$(p7 профиль-alpha)"
case_check "П-7 чужой ключ — не профиль" None "$(p7 профиль-чужой)"
case_check "П-7 носитель = профиль и орг совпала → не тревога" 0 "$(p7 законный)"
case_check "П-7 строка [смена] называет профиль" True "$(p7 законный-имя)"
case_check "П-7 иной ключ при орг из набора → тревога" 1 "$(p7 чужой-орг-в-наборе)"
case_check "П-7 ключ профиля, орг ≠ орг профиля → тревога" 1 "$(p7 профиль-орг-не-та)"
case_check "П-7 ключа нет в строке вердикта" True "$(p7 ключа-в-строке-нет)"
case_check "П-7 ключ, негодный для заголовка: повод живого вызова его не печатает" True "$(p7 повод-без-ключа)"

echo "── (г) проверка после подъёма: процесс смены, не файл ──"
write_profiles
# printf, а не heredoc: проверка «функция до определения» не понимает heredoc и
# принимала бы source внутри него за подключение посреди теста.
printf 'source "%s"\neval "exec $(komanda_agenta "claude --model proba-model --dangerously-skip-permissions")"\n' \
    "$LIB" > "$S/bin/podyom.sh"
raise() {  # поднять (или переподнять) смену в своей панели и дождаться claude
    rm -f "$CLAUDE_TRACE".*
    if tmux has-session -t switch-proba 2>/dev/null; then
        tmux respawn-pane -k -t switch-proba "CLAUDE_STAY=1 bash $S/bin/podyom.sh"
    else
        tmux new-session -d -s switch-proba -c "$S/empty" "CLAUDE_STAY=1 bash $S/bin/podyom.sh"
    fi
    for _ in $(seq 100); do [ -s "$CLAUDE_TRACE.args" ] && return 0; sleep 0.1; done
    return 1
}
check() { local rc=0; python3 "$DOOR" --проверить > "$S/out/check-$1.log" 2>&1 || rc=$?; echo "$rc"; }
state alpha
case_check "стенд: смена поднялась в своей панели" да "$(yes_no raise)"
G_A="$(check a)|$(state_field идёт)"
case_check "(г) процесс на ключе alpha, орг совпала → 0 и идёт=alpha" "0|alpha" "$G_A"
# Отказ проверки судится только у проверки, умеющей сказать «да» (иначе зелен до кода).
G_LIVE="$([ "$G_A" = "0|alpha" ] && echo жива || echo мертва)"
state beta alpha
case_check "(г) запуск=beta без переподъёма (процесс на alpha) → не 0, идёт не тронут" "жива|не0|alpha" \
    "$G_LIVE|$(r=$(check b); [ "$r" != 0 ] && echo не0 || echo 0)|$(state_field идёт)"
state dlt alpha
case_check "(г) запуск=dlt (орг та же, ключ другой), процесс на alpha → не 0: судится ключ процесса" "жива|не0|alpha" \
    "$G_LIVE|$(r=$(check d); [ "$r" != 0 ] && echo не0 || echo 0)|$(state_field идёт)"
state gamma alpha; raise >/dev/null
case_check "(г) процесс на ключе gamma, но API называет чужую профилю орг → не 0" "жива|не0|alpha" \
    "$G_LIVE|$(r=$(check g); [ "$r" != 0 ] && echo не0 || echo 0)|$(state_field идёт)"
state login alpha
case_check "(г) запуск=login без переподъёма (процесс несёт ключ gamma) → не 0, идёт не тронут" "жива|не0|alpha" \
    "$G_LIVE|$(r=$(check lk); [ "$r" != 0 ] && echo не0 || echo 0)|$(state_field идёт)"
raise >/dev/null
case_check "(г) login: носителя в процессе нет → 0 и идёт=login" "0|login" "$(check l)|$(state_field идёт)"
tmux capture-pane -p -S - -t switch-proba > "$S/out/pane.txt" 2>/dev/null || true

# ═══ Этап 1б: смена из Telegram — дверь (--хочу/--ждёт/--итог), сторож, гейт ═══
# Дверь зовёт сторож сессий. Боевой сторож из пробы не зовётся НИКОГДА: дверь
# работает из КОПИИ дерева стенда, где на месте сторожа — подставной, и ей же
# названа дверь ACCOUNT_WARDEN. Код, забывший дверь, всё равно попадёт в
# подставной: путь по умолчанию — рядом со скриптом, то есть в стенде.
mkdir -p "$S/proj/scripts" "$S/proj/harness/demons"
cp "$DOOR" "$S/proj/scripts/account-switch.py"
cp "$LOGIN_PY" "$S/proj/scripts/check-login-expiry.py"
cp -r "$HERE/lib" "$S/proj/scripts/lib"
cat > "$S/proj/harness/demons/session-warden.sh" <<'WARDEN'
#!/usr/bin/env bash
# Подставной сторож: доводы и поле `запуск` state.json на миг вызова.
zapusk=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8")).get("запуск",""))' \
    "$HARNESS_ACCOUNT_DIR/state.json" 2>/dev/null)
printf '%s|%s\n' "$*" "$zapusk" >> "$WARDEN_CALLS"
exit 0
WARDEN
chmod +x "$S/proj/harness/demons/session-warden.sh"
DOOR_B="$S/proj/scripts/account-switch.py"
export ACCOUNT_WARDEN="$S/proj/harness/demons/session-warden.sh" WARDEN_CALLS="$S/out/warden-calls"
: > "$WARDEN_CALLS"
warden_calls() { local n; n=$(grep -c . "$WARDEN_CALLS" 2>/dev/null); echo "${n:-0}"; }
state_sw() {  # $1 запуск, $2 идёт, $3 откуда, $4 куда, $5 откат (true|false)
    printf '{"запуск": "%s", "идёт": "%s", "переключение": {"откуда": "%s", "куда": "%s", "начало": "2026-11-13T08:00:00+05:00", "откат": %s}, "переключения": []}\n' \
        "$1" "$2" "$3" "$4" "$5" > "$S/acc/state.json"
}
sw_field() {  # $1 = поле переключения → значение; переключения нет → «нет»
    python3 -I -c 'import json,sys
p=json.load(open(sys.argv[1],encoding="utf-8")).get("переключение") or {}
v=p.get(sys.argv[2], "нет") if p else "нет"
print(str(v).lower() if isinstance(v,bool) else v)' "$S/acc/state.json" "$1" 2>/dev/null || echo битый
}
state_sha() { sha256sum < "$S/acc/state.json" | cut -d' ' -f1; }
run_b() {  # $1 = имя журнала, дальше доводы двери → код
    local name="$1" rc=0; shift
    python3 "$DOOR_B" "$@" > "$S/out/b-$name.log" 2>&1 || rc=$?
    echo "$rc"
}

echo "── 1б-Х: --хочу пишет выбор и будит сторожа, переключает не она ──"
write_profiles; state alpha alpha; : > "$WARDEN_CALLS"; before=$(tg_count)
H1="$(run_b h-beta --хочу beta)|$(state_field запуск)|$(state_field идёт)|$(sw_field откуда)>$(sw_field куда)|$(sw_field откат)"
case_check "1б-Х --хочу beta → 0; запуск=beta, идёт=alpha; переключение alpha>beta, не откат" "0|beta|alpha|alpha>beta|false" "$H1"
H_LIVE="$([ "$H1" = "0|beta|alpha|alpha>beta|false" ] && echo жива || echo мертва)"
# Сторож ротирует сам, когда помощников 0 и передача написана: --owner или
# --rotate-now (немедленный «/exit» без поручения передачи) обошли бы обе двери.
case_check "1б-Х сторож позван раз, ровно «--account-switch», после записи запуска" "--account-switch|beta" \
    "$(paste -sd'#' "$WARDEN_CALLS")"
case_check "1б-Х в канал ничего: итог скажет --итог одним сообщением" "жива|0" "$H_LIVE|$(( $(tg_count) - before ))"
hochu_refuse() {  # $1 = цель, $2 = имя случая, $3 = вестей ждём
    local was before_tg rc
    was=$(state_sha); before_tg=$(tg_count); : > "$WARDEN_CALLS"
    rc=$(run_b "refuse-$1" --хочу "$1")
    case_check "1б-Х $2: отказ, state не тронут, сторож не зван, вестей $3" "жива|отказ|тот-же|0|$3" \
        "$H_LIVE|$([ "$rc" != 0 ] && echo отказ || echo принято)|$([ "$was" = "$(state_sha)" ] && echo тот-же || echo изменён)|$(warden_calls)|$(( $(tg_count) - before_tg ))"
}
state alpha alpha
hochu_refuse dead "ключ цели не принят живым вызовом (401)" 1
case_check "1б-Х весть об отказе называет подпись цели и «не перешёл»" да \
    "$(tail -n1 "$TG_OUT" | grep -q 'DEAD' && tail -n1 "$TG_OUT" | grep -q 'е перешёл' && echo да || echo нет)"
hochu_refuse gamma "орг ключа цели не та, что у профиля" 1
hochu_refuse ghost "профиль не заведён" 0
hochu_refuse "../x" "имя не [a-z0-9-]" 0
state_sw alpha beta beta alpha true
hochu_refuse beta "идёт откат — новый выбор не принимается" 1
state alpha alpha; was=$(state_sha); before=$(tg_count); : > "$WARDEN_CALLS"
rc=$(run_b h-same --хочу alpha)
case_check "1б-Х цель = идущий: 0, state не тронут, сторож не зван, одна весть «уже»" "жива|0|тот-же|0|1" \
    "$H_LIVE|$rc|$([ "$was" = "$(state_sha)" ] && echo тот-же || echo изменён)|$(warden_calls)|$(( $(tg_count) - before ))"
state alpha alpha; : > "$WARDEN_CALLS"
case_check "1б-Х --хочу login: без живого вызова, переключение alpha>login, сторож зван" "0|login|alpha>login|1" \
    "$(run_b h-login --хочу login)|$(state_field запуск)|$(sw_field откуда)>$(sw_field куда)|$(warden_calls)"

echo "── 1б-Ж: --ждёт — ротировать (0), подтвердить (3), нечего (1) ──"
state alpha alpha; raise >/dev/null
state_sw beta alpha alpha beta false
J0="$(run_b j-rot --ждёт)|$(tr -d '\n' < "$S/out/b-j-rot.log")"
case_check "1б-Ж процесс на alpha, цель beta → 0 и печатает beta" "0|beta" "$J0"
state alpha alpha
case_check "1б-Ж переключения нет → 1" 1 "$(run_b j-none --ждёт)"
state beta alpha; raise >/dev/null; state_sw beta alpha alpha beta false
# Процесс уже на цели (подъём случился иначе — respawn, перезагрузка): ротировать
# второй раз — значит снова оборвать смену; нужен только итог.
case_check "1б-Ж процесс уже на цели → 3 (подтвердить, не ротировать)" 3 "$(run_b j-done --ждёт)"

echo "── 1б-В: «/login» внутри сессии — ключ окружения не доказывает аккаунт ──"
# Улика 13.11.2026: окружение смены несло ключ nikita-f3d (429, недельный лимит),
# а работа шла на bkb — владелец вошёл «/login» внутри сессии. /proc/<pid>/environ
# этого не видит; видно только, что файл входа переписан после старта процесса.
V_LIVE="$(r=$(run_b v-live --ждёт); [ "$r" = 3 ] && echo жива || echo мертва)"
sleep 1.2; printf '{}' > "$CLAUDE_CREDENTIALS"
case_check "1б-В файл входа новее процесса на цели → --ждёт 0 (ротировать), а не 3" "жива|0" "$V_LIVE|$(run_b v-j --ждёт)"
state beta alpha
case_check "1б-В --проверить → 2 (судить нечем), идёт не тронут" "жива|2|alpha" "$V_LIVE|$(check v)|$(state_field идёт)"
state beta beta; : > "$WARDEN_CALLS"
case_check "1б-В --хочу beta при идёт=beta → не «уже»: переключение на beta, сторож зван" "жива|0|beta|1" \
    "$V_LIVE|$(run_b v-h --хочу beta)|$(sw_field куда)|$(warden_calls)"
gate_shift() {  # $1 = имя → «код<TAB>строка [смена]»; процесс смены — настоящий из панели стенда
    python3 -I - "$LOGIN_PY" "$KEY_B" > "$S/out/gs-$1.txt" 2>"$S/out/gs-$1.err" <<'PY'
import contextlib, datetime, importlib.util, io, sys
path, login_key = sys.argv[1:]
spec = importlib.util.spec_from_file_location("cle", path); m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
сейчас = datetime.datetime.now()
m.чужие_файлы = lambda *a, **k: []
m.сроки = lambda *a, **k: (сейчас - datetime.timedelta(hours=1), сейчас + datetime.timedelta(days=20))
m.записать_историю = lambda *a, **k: None
m.отметиться = lambda *a, **k: None
m.записать_эталон = lambda *a, **k: False
# Пути носителей молчат; tmux — настоящий сервер стенда: по нему ищется процесс смены.
_tmux = m._значение
m._значение = lambda cmd: _tmux(cmd) if cmd[0] == "tmux" else ""
m._из_памяти_tmux = lambda: ""
m.подписочный_ключ = lambda *a, **k: login_key
m.оболочки_панелей = lambda *a, **k: []
sys.argv = ["check-login-expiry.py", "--без-пробы"]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = m.main()
line = next((s for s in buf.getvalue().splitlines() if s.startswith("[смена]")), "нет строки")
print(f"{rc}\t{line}")
PY
}
state beta beta; gate_shift new
touch -d '-1 hour' "$CLAUDE_CREDENTIALS"; gate_shift old
case_check "1б-В гейт: файл входа старше процесса → 0, «на профиле beta»" "0|да" \
    "$(cut -f1 "$S/out/gs-old.txt")|$(grep -qF 'на профиле beta' "$S/out/gs-old.txt" && echo да || echo нет)"
case_check "1б-В гейт: файл входа новее процесса → [смена] 2 и называет /login" "2|да" \
    "$(cut -f1 "$S/out/gs-new.txt")|$(grep -qF '/login' "$S/out/gs-new.txt" && echo да || echo нет)"
state_sw beta alpha alpha beta false

echo "── 1б-И: --итог — проверка процесса, одна весть, откат ──"
: > "$WARDEN_CALLS"; before=$(tg_count)
I1="$(run_b i-ok --итог)|$(state_field идёт)|$(sw_field куда)|$(warden_calls)|$(( $(tg_count) - before ))"
case_check "1б-И процесс на beta → 0, идёт=beta, переключение снято, сторож не зван, весть одна" "0|beta|нет|0|1" "$I1"
I_LIVE="$([ "$I1" = "0|beta|нет|0|1" ] && echo жива || echo мертва)"
case_check "1б-И весть «перешёл» называет подпись BETA" "жива|да" \
    "$I_LIVE|$(tail -n1 "$TG_OUT" | grep -q 'ерешёл' && tail -n1 "$TG_OUT" | grep -q 'BETA' && echo да || echo нет)"
state alpha alpha; raise >/dev/null; state_sw beta alpha alpha beta false
: > "$WARDEN_CALLS"; before=$(tg_count)
rc=$(run_b i-fail --итог)
case_check "1б-И процесс остался на alpha → откат: не 0, запуск=alpha, идёт=alpha, переключение beta>alpha откат" \
    "жива|не0|alpha|alpha|beta>alpha|true" \
    "$I_LIVE|$([ "$rc" != 0 ] && echo не0 || echo 0)|$(state_field запуск)|$(state_field идёт)|$(sw_field откуда)>$(sw_field куда)|$(sw_field откат)"
case_check "1б-И откат: сторож позван раз «--account-switch» с запуском alpha" "жива|--account-switch|alpha" \
    "$I_LIVE|$(paste -sd'#' "$WARDEN_CALLS")"
case_check "1б-И откат: одна весть «не перешёл на BETA … ALPHA»" "жива|1|да" \
    "$I_LIVE|$(( $(tg_count) - before ))|$(tail -n1 "$TG_OUT" | grep -q 'е перешёл' && tail -n1 "$TG_OUT" | grep -q 'BETA' && tail -n1 "$TG_OUT" | grep -q 'ALPHA' && echo да || echo нет)"
# Откат удался: молча — весть о неудаче с «возвращаю» уже ушла.
: > "$WARDEN_CALLS"; before=$(tg_count)
case_check "1б-И откат подтверждён (процесс на alpha) → 0, переключение снято, вестей 0, сторож не зван" "жива|0|alpha|нет|0|0" \
    "$I_LIVE|$(run_b i-back --итог)|$(state_field идёт)|$(sw_field куда)|$(( $(tg_count) - before ))|$(warden_calls)"
state beta alpha; raise >/dev/null; state_sw alpha alpha beta alpha true
: > "$WARDEN_CALLS"; before=$(tg_count)
rc=$(run_b i-back-fail --итог)
case_check "1б-И откат НЕ удался → не 0, переключение снято (без петли), сторож не зван, весть одна" "жива|не0|нет|0|1" \
    "$I_LIVE|$([ "$rc" != 0 ] && echo не0 || echo 0)|$(sw_field куда)|$(warden_calls)|$(( $(tg_count) - before ))"
state alpha alpha
# Код 2 даёт и argparse на незнакомом доводе — судится только у живого --итог.
case_check "1б-И переключения нет → 2, вестей нет" "жива|2|0" "$I_LIVE|$(before=$(tg_count); r=$(run_b i-none --итог); echo "$r|$(( $(tg_count) - before ))")"
tmux capture-pane -p -S - -t switch-proba >> "$S/out/pane.txt" 2>/dev/null || true

echo "── 1б-С: сторож — переключение аккаунта только полным подъёмом и только в тишине ──"
WARDEN_SH="${HARNESS_SESSION_WARDEN:-$HERE/../harness/demons/session-warden.sh}"
[ -f "$WARDEN_SH" ] || WARDEN_SH="$HERE/../demons/session-warden.sh"
wbody() { sed -n "/^$1() {/,/^}/p" "$WARDEN_SH"; }
wfn() {  # $1 = функция, дальше доводы → «слово|код»
    local fn="$1" out rc=0; shift
    out=$(bash -c 'eval "$1"; shift; "$@"' _ "$(wbody "$fn")" "$fn" "$@" 2>/dev/null) || rc=$?
    echo "$out|$rc"
}
case_check "1б-С стенд: тело способ_ротации найдено в стороже" да "$([ -n "$(wbody способ_ротации)" ] && echo да || echo нет)"
case_check "1б-С способ_ротации: смена аккаунта (8-й довод 1) → full, хоть всё прочее звало бы clear" "full|0" \
    "$(wfn способ_ротации 0 0 0 24 0 0 0 1)"
case_check "1б-С способ_ротации: без аккаунта (8-й довод 0) — прежний clear" "clear|0" \
    "$(wfn способ_ротации 0 0 0 24 0 0 0 0)"
case_check "1б-С действие_по_сроку_помощников: аккаунт + «ротировать» → «сообщить»" "сообщить|0" \
    "$(wfn действие_по_сроку_помощников 1 ротировать)"
case_check "1б-С действие_по_сроку_помощников: не аккаунт — ключ как есть" "ротировать|0" \
    "$(wfn действие_по_сроку_помощников 0 ротировать)"
ACT="$(wfn действие_по_сроку_помощников 1 ротировать)"; ACT="${ACT%|*}"
case_check "1б-С помощники идут 999 мин из 60 при аккаунте → ротации нет (код 1)" "жива|1" \
    "$([ "$ACT" = сообщить ] && echo жива || echo мертва)|$(r=$(wfn rotation_helpers_verdict 2 0 999 60 "$ACT"); echo "${r##*|}")"

echo "── 1б-Г: гейт «срок входа» сверяет ключ, на котором реально идёт работа ──"
gate() {  # $1 = имя, $2 = ключ файла /login, $3 = память tmux («» — пусто) → «код<TAB>строка [аккаунт]»
    python3 -I - "$LOGIN_PY" "$2" "$3" > "$S/out/gate-$1.txt" 2>"$S/out/gate-$1.err" <<'PY'
import contextlib, datetime, importlib.util, io, sys
path, login_key, tmux_mem = sys.argv[1:]
spec = importlib.util.spec_from_file_location("cle", path); m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
сейчас = datetime.datetime.now()
# Всё, что смотрит на боевую машину, подменено: дом, сроки, история, пульс,
# пути носителей, процесс и панели смены. Остаётся вопрос «аккаунт».
m.чужие_файлы = lambda *a, **k: []
m.сроки = lambda *a, **k: (сейчас - datetime.timedelta(hours=1), сейчас + datetime.timedelta(days=20))
m.записать_историю = lambda *a, **k: None
m.отметиться = lambda *a, **k: None
m.записать_эталон = lambda *a, **k: False
m._значение = lambda *a, **k: ""
m._из_памяти_tmux = lambda: tmux_mem
m.подписочный_ключ = lambda *a, **k: login_key
m.процесс_смены = lambda *a, **k: None
m.оболочки_панелей = lambda *a, **k: []
sys.argv = ["check-login-expiry.py", "--без-пробы"]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = m.main()
line = next((s for s in buf.getvalue().splitlines() if s.startswith("[аккаунт]")), "нет строки")
print(f"{rc}\t{line}")
PY
}
gate_rc() { cut -f1 "$S/out/gate-$1.txt" 2>/dev/null; }
gate_has() { cut -f2- "$S/out/gate-$1.txt" 2>/dev/null | grep -qF -- "$2" && echo да || echo нет; }
write_profiles
state alpha alpha; gate g1 "$KEY_DEAD" ""
case_check "1б-Г смена на профиле alpha, доступ /login истёк → не 2: 0 по ключу профиля, строка его называет" "0|да" \
    "$(gate_rc g1)|$(gate_has g1 alpha)"
state alpha alpha; gate g2 "$KEY_B" ""
case_check "1б-Г смена на alpha, /login жив (орг B) → судится профиль, а не /login" "0|да" "$(gate_rc g2)|$(gate_has g2 alpha)"
state gamma gamma; gate g3 "$KEY_DEAD" ""
case_check "1б-Г профиль gamma: орг ключа ≠ орг профиля → 1" 1 "$(gate_rc g3)"
state login login; gate g4 "$KEY_DEAD" ""
case_check "1б-Г запуск=login, /login истёк → 2, как раньше" "2|да" "$(gate_rc g4)|$(gate_has g4 /login)"
state nokey nokey; gate g5 "$KEY_DEAD" ""
case_check "1б-Г файла ключа профиля нет (смена встала на /login) → судится /login: 2" "2|да" "$(gate_rc g5)|$(gate_has g5 /login)"
state alpha alpha; gate g6 "$KEY_DEAD" "CLAUDE_CODE_OAUTH_TOKEN=$KEY_FOREIGN"
case_check "1б-Г чужой носитель в памяти tmux назван раньше профиля" да "$(gate_has g6 tmux)"

echo "── П-13: модельные демоны зовут claude-run.sh НАПРЯМУЮ — ключ профиля кладёт сама дверь ──"
# Улика 11.11 00:0x: claude-demon.sh, ask-the-model.py, self-prompt-live.sh зовут
# claude-run.sh без as-account.sh, и claude брал вход /login чужой организации
# («subscription access disabled», rc 1). Одна дверь: ключ профиля `запуск`
# кладёт claude-run.sh, если его ещё нет в окружении.
run_direct() {  # [VAR=знач…] — доп. окружение; зов как у claude-demon.sh:133
    N=$((N + 1))
    rm -f "$CLAUDE_TRACE".*
    ( cd "$S/empty" && env -u CLAUDE_CODE_OAUTH_TOKEN "$@" bash "$HERE/claude-run.sh" --класс демон \
        -p --strict-mcp-config --model proba-model "вопрос пробы" ) > "$S/out/run-$N.log" 2>&1
}
write_profiles; state alpha
run_direct
case_check "П-13 стенд: прямой зов дошёл до claude (-p в доводах)" да "$(yes_no args_have -p)"
case_check "П-13 запуск=alpha, ключа в окружении нет → claude несёт sha ключа alpha" "$(sha "$KEY_A")" "$(traced_sha)"
case_check "П-13 ключа нет в доводах claude" нет "$(yes_no grep -qaF proba-klyuch "$CLAUDE_TRACE.args")"
case_check "П-13 ключа нет в выводе двери" нет "$(yes_no grep -qF proba-klyuch "$S/out/run-$N.log")"
state beta
run_direct
case_check "П-13 запуск=beta → sha ключа beta (читается state, не зашит)" "$(sha "$KEY_B")" "$(traced_sha)"
state crlf
run_direct
case_check "П-13 файл ключа с CRLF и пробелами → ключ без краёв" "$(sha "$KEY_A")" "$(traced_sha)"
state alpha
run_direct CLAUDE_CODE_OAUTH_TOKEN="$KEY_B"
case_check "П-13 ключ уже в окружении (смена через as-account) → не подменяется" "$(sha "$KEY_B")" "$(traced_sha)"
state login
run_direct
case_check "П-13 запуск=login → без ключа, claude запущен" "нет|да" "$(traced_sha)|$(yes_no args_have -p)"
run_direct HARNESS_ACCOUNT_DIR="$S/net-kataloga" LOG_DIR="$S/log-direct-off"
case_check "П-13 profiles.json нет → запуск без ключа, как сейчас" "нет|да" "$(traced_sha)|$(yes_no args_have -p)"

echo "── П-6: ключа нет нигде, куда смотрит человек или журнал ──"
if [ "$P1_SHA" = "$(sha "$KEY_A")" ]; then
    leaks=0
    for k in "${ALL_KEYS[@]}"; do
        grep -rlF -- "$k" "$S/out" "$S/acc" "$S"/log* 2>/dev/null | sed "s|^|  утечка: |" && leaks=$((leaks + 1))
    done
    tr '\0' '\n' < "$CLAUDE_TRACE.args" 2>/dev/null | grep -qF proba-klyuch && leaks=$((leaks + 1))
    P6="$([ "$leaks" = 0 ] && echo чисто || echo "утечек $leaks")"
else
    P6="не судимо: смена не поднялась на профиле (П-1 красная)"
fi
case_check "П-6 ключа нет в argv, выводе, tg, state/profiles, журналах, прокрутке панели" чисто "$P6"

printf '\nитог test_switch_account: случаев %s, неудач %s\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" = 0 ]
