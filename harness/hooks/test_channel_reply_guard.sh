#!/usr/bin/env bash
# Проба Stop-хука channel_reply_guard.py: смена не кончает ход, пока на
# последнее сообщение владельца не ушло ни одной ДОСТАВЛЕННОЙ отправки в канал.
#
# Беда (владелец 30.10.2026): «он в cli отчитывается, а мне в телегу молчит;
# надо чтобы это тоже кодом контролировалось». До хука ответ в канал держался
# вниманием (CLAUDE.md «Channel rules», сторож тишины 30 мин).
#
# Носители, разобранные пробой (.tasks/hr.reply-in-channel/проба.md):
#   сообщение владельца — файл $LOG_DIR/inbox/<эпоха>.<нс>.txt или
#     inbox/обработано/<то же имя>; имя — момент приёма и ключ антиповтора;
#     пишет только диспетчер и только для чата владельца. Голос ложится файлом
#     ПОСЛЕ расшифровки (mtime позже имени до 163 с) — время сообщения:
#     max(имя, mtime). Команда боту и ответ панели в inbox.jsonl есть, файла нет.
#   ответ — строка $LOG_DIR/tg_out.jsonl с iz_sessii=true (пишется ПОСЛЕ
#     доставки) или метка $LOG_DIR/last_report (ставится после доставки текста
#     И файла из сессии; файл в tg_out.jsonl не попадает). Отказ — строка
#     tg_failures.log, ответом не считается; строка демона (iz_sessii=false) — тоже.
#     Короткая квитанция-обещание («…ответ следом») — тоже не ответ: признаки и
#     предел длины в harness/config/channel-reply.yaml, путь — CHANNEL_REPLY_MARKERS.
#   жёлтый режим — ключи harness.conf CHANNEL_REPLY_YELLOW_FROM (ГГГГ-ММ-ДД),
#     CHANNEL_REPLY_YELLOW_DAYS (3), жёсткость после него CHANNEL_REPLY_STRICT.
#     В жёлтом и при STRICT=0 — напоминание один раз на сообщение; после
#     жёлтого при STRICT=1 — блок на каждом Stop до ответа.
#   метрика — delivery-latency.py --без-ответа --окно ОТ ДО: строка
#     «без ответа: N из M»; ответ засчитывается в срок не больше 30 мин,
#     один ответ после серии закрывает всю серию.
#
# Стенд: свои install.conf/harness.conf, HARNESS_HOME и LOG_DIR во временном
# каталоге; конфиг читается ПОСЛЕ окружения ([[the-probe-played-on-production]]).
# Каждый случай — свежий mktemp -d.
#
# Двери для мутаций: CHANNEL_REPLY_GUARD_UNDER_TEST — испорченная копия хука,
# DELIVERY_LATENCY_UNDER_TEST — испорченная копия прибора.
#
#   bash scripts/hooks/test_channel_reply_guard.sh
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
HOOK="${CHANNEL_REPLY_GUARD_UNDER_TEST:-$HERE/channel_reply_guard.py}"
METER="${DELIVERY_LATENCY_UNDER_TEST:-$ROOT/scripts/delivery-latency.py}"
# Хук молчит без TMUX (headless-демон), а ночные ворота идут без него: стенд
# сам задаёт сессию агента, иначе проба судила бы окружение запуска.
export TMUX="/tmp/tmux-stand/default,0,0"

bad=0
total=0
STANDS=()
cleanup() {
    local d
    for d in "${STANDS[@]}"; do rm -rf "$d"; done
}
trap cleanup EXIT

verdict() {  # $1 = 1 (верно) / 0, $2 = имя, $3 = подробность
    total=$((total + 1))
    if [ "$1" = 1 ]; then
        echo "  ок    $2"
    else
        echo "  ПЛОХО $2${3:+ — $3}"
        bad=$((bad + 1))
    fi
}

NOW=$(date +%s)
TODAY=$(date +%F)
LONG_AGO=$(date -d '-10 days' +%F)

# Боевой журнал хук трогать не вправе: метка начала, сверка в конце.
REAL_LOG=$(env -u LOG_DIR -u HARNESS_INSTALL_CONF -u HARNESS_CONF -u HARNESS_HOME \
    bash -c 'source "$1/scripts/lib/config.sh"; konf_zagruzit; printf "%s" "${LOG_DIR:-}"' _ "$ROOT" 2>/dev/null)
START_MARK=$(mktemp)
STANDS+=("$START_MARK")

# Стенд в главной оболочке: из $(…) список STANDS не вернулся бы.
# $1 — строки harness.conf (пусто — жёлтый режим с сегодняшнего дня).
new_stand() {
    T=$(mktemp -d)
    STANDS+=("$T")
    mkdir -p "$T/log/inbox/обработано" "$T/log/inbox/вложения" "$T/log/inbox/файлы" \
             "$T/log/inbox/секреты" "$T/home"
    printf 'PROJECT_DIR="%s"\nLOG_DIR="%s/log"\n' "$ROOT" "$T" > "$T/install.conf"
    printf '%s\n' "${1-CHANNEL_REPLY_YELLOW_FROM=\"$TODAY\"
CHANNEL_REPLY_YELLOW_DAYS=\"3\"
CHANNEL_REPLY_STRICT=\"0\"}" > "$T/harness.conf"
    : > "$T/log/tg_out.jsonl"
    : > "$T/log/tg_failures.log"
    : > "$T/log/inbox.jsonl"
    MID=100
}

iso() { date -d "@$1" -Is; }

# Сообщение владельца: $1 = эпоха приёма, $2 = вид (текст|файл|голос|команда),
# $3 = каталог (обработано|корень). Голос: файл появляется позже имени.
owner_msg() {
    local e="$1" kind="${2:-текст}" where="${3:-обработано}" dir name
    MID=$((MID + 1))
    dir="$T/log/inbox/обработано"; [ "$where" = корень ] && dir="$T/log/inbox"
    name="$e.000000001.txt"
    case "$kind" in
        текст)
            printf 'слова владельца\n' > "$dir/$name"
            touch -d "@$e" "$dir/$name"
            printf '{"ts":"%s","message_id":"%s","chat_id":"1","text":"слова владельца"}\n' \
                "$(iso "$e")" "$MID" >> "$T/log/inbox.jsonl" ;;
        файл)
            printf '{"message_id":%s,"document":{"file_id":"x","file_name":"a.pdf"}}' "$MID" > "$dir/$name"
            touch -d "@$e" "$dir/$name"
            printf '{"ts":"%s","message_id":"%s","chat_id":"1","text":""}\n' \
                "$(iso "$e")" "$MID" >> "$T/log/inbox.jsonl" ;;
        голос)
            printf 'расшифровка голоса\n' > "$dir/$name"
            touch -d "@$((e + 90))" "$dir/$name"
            printf '{"ts":"%s","message_id":"%s","chat_id":"1","text":"","вид":"вложение"}\n{"ts":"%s","message_id":"%s","chat_id":"1","text":"расшифровка голоса","вид":"расшифровка"}\n' \
                "$(iso "$e")" "$MID" "$(iso "$((e + 90))")" "$MID" >> "$T/log/inbox.jsonl" ;;
        команда)
            printf '{"ts":"%s","message_id":"%s","chat_id":"1","text":"/статус"}\n' \
                "$(iso "$e")" "$MID" >> "$T/log/inbox.jsonl" ;;
    esac
}

reply()  { printf '{"ts":"%s","uroven":"","iz_sessii":true,"text":"ответ"}\n'  "$(iso "$1")" >> "$T/log/tg_out.jsonl"; }
daemon() { printf '{"ts":"%s","uroven":"","iz_sessii":false,"text":"тревога"}\n' "$(iso "$1")" >> "$T/log/tg_out.jsonl"; }
refused() {
    printf '{"ts":"%s","script":"tg_send","why":"API не подтвердил доставку","api":"","sent_parts":0,"total_parts":1}\n' \
        "$(iso "$1")" >> "$T/log/tg_failures.log"
}

# Прогон хука: печатает stderr, код — в $T/rc. $1 — stop_hook_active, $2 — payload целиком.
run_hook() {
    local active="${1:-false}" payload="${2:-}" rc=0 out
    [ -n "$payload" ] || payload=$(printf '{"session_id":"stand","transcript_path":"%s/transcript.jsonl","cwd":"%s","hook_event_name":"Stop","stop_hook_active":%s}' "$T" "$ROOT" "$active")
    out=$(cd "$T" && printf '%s' "$payload" | env HARNESS_HOME="$T/home" \
            HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
            PROJECT_DIR="$ROOT" LOG_DIR="$T/log" HARNESS_LOG_DIR="$T/log" \
            timeout 30 python3 "$HOOK" 2>&1 >/dev/null) || rc=$?
    printf '%s' "$rc" > "$T/rc"
    printf '%s' "$out"
}
rc() { cat "$T/rc"; }
is() { [ "$1" = "$2" ] && echo 1 || echo 0; }
asks_reply() {  # stderr велит ответить владельцу командой канала
    printf '%s' "$1" | grep -q 'tg_send\.sh' && printf '%s' "$1" | grep -qi 'владельц' && echo 1 || echo 0
}

[ -f "$HOOK" ] || echo "  (хука нет: $HOOK — проба обязана быть красной)"

# ── (а) сообщение владельца без отправки ───────────────────────────────────
new_stand
owner_msg $((NOW - 300)) текст корень
out=$(run_hook)
verdict "$(is "$(rc)" 2)" "(а) сообщение без ответа — ход не кончается (код 2)" "код $(rc)"
verdict "$(asks_reply "$out")" "(а) stderr велит ответить владельцу через tg_send.sh" "stderr: ${out:-пусто}"

# ── (б) после сообщения доставленная отправка ──────────────────────────────
new_stand
owner_msg $((NOW - 300))
reply $((NOW - 200))
out=$(run_hook)
verdict "$(is "$(rc)" 0)" "(б) ответ доставлен после сообщения — код 0" "код $(rc), stderr: $out"

# (б2) ответ ФАЙЛОМ: в tg_out.jsonl не пишется, остаётся только last_report
new_stand
owner_msg $((NOW - 300))
printf '%s\n' $((NOW - 100)) > "$T/log/last_report"
out=$(run_hook)
verdict "$(is "$(rc)" 0)" "(б2) ответ файлом (только last_report новее сообщения) — код 0" "код $(rc)"

# (б3) отправка РАНЬШЕ сообщения ответом не считается
new_stand
reply $((NOW - 305))
owner_msg $((NOW - 300))
printf '%s\n' $((NOW - 305)) > "$T/log/last_report"
out=$(run_hook)
verdict "$(is "$(rc)" 2)" "(б3) отправка за 5 с ДО сообщения — не ответ (код 2)" "код $(rc)"

# ── (в) отказ доставки и строка демона ответом не считаются ────────────────
new_stand
owner_msg $((NOW - 300))
refused $((NOW - 200))
out=$(run_hook)
verdict "$(is "$(rc)" 2)" "(в) после сообщения только отказ tg_failures.log — код 2" "код $(rc)"

new_stand
owner_msg $((NOW - 300))
daemon $((NOW - 200))
out=$(run_hook)
verdict "$(is "$(rc)" 2)" "(в2) после сообщения только весть демона (iz_sessii=false) — код 2" "код $(rc)"

# ── (г) антиповтор ─────────────────────────────────────────────────────────
new_stand
owner_msg $((NOW - 300)) текст корень
run_hook >/dev/null; first=$(rc)
run_hook >/dev/null; second=$(rc)
verdict "$([ "$first" = 2 ] && [ "$second" = 0 ] && echo 1 || echo 0)" \
    "(г) второй Stop на то же сообщение — код 0 (напоминание один раз)" "коды $first, $second"
mv "$T/log/inbox/$((NOW - 300)).000000001.txt" "$T/log/inbox/обработано/"
run_hook >/dev/null
verdict "$(is "$(rc)" 0)" "(г) сообщение перенесено в обработано/ — тот же ключ, код 0" "код $(rc)"
owner_msg $((NOW - 60)) текст корень
run_hook >/dev/null
verdict "$(is "$(rc)" 2)" "(г) новое сообщение владельца — снова код 2" "код $(rc)"
STATE_NAMES=$(find "$T/log" -maxdepth 1 -type f ! -name tg_out.jsonl ! -name tg_failures.log ! -name inbox.jsonl ! -name 'ответы-в-канал.jsonl' -printf '%f\n')
verdict "$([ -n "$STATE_NAMES" ] && echo 1 || echo 0)" \
    "(г) файл состояния антиповтора лежит в LOG_DIR стенда" "в $T/log нового файла нет"

# ── (д) stop_hook_active ───────────────────────────────────────────────────
new_stand
owner_msg $((NOW - 300))
run_hook true >/dev/null
verdict "$(is "$(rc)" 0)" "(д) stop_hook_active=true — код 0 (без петли)" "код $(rc)"
run_hook false >/dev/null
verdict "$(is "$(rc)" 2)" "(д) следующий обычный Stop — код 2 (активный вызов антиповтор не тратит)" "код $(rc)"

# ── (е) пинок сторожа и служебное — не сообщение владельца ─────────────────
new_stand
owner_msg $((NOW - 600))
reply $((NOW - 500))
owner_msg $((NOW - 100)) команда
printf '{"message_id":555,"voice":{"file_id":"v"}}' > "$T/log/inbox/вложения/555.json"
printf 'x' > "$T/log/inbox/файлы/снимок.png"
printf 'x' > "$T/log/inbox/секреты/ключ.txt"
printf 'служебное\n' > "$T/log/inbox/README.txt"
printf '{"ts":"%s","event":"delivery.kick_sent","outcome":"ok","src":"scripts/lib/pane.sh"}\n' "$(iso $((NOW - 50)))" > "$T/log/доставка.jsonl"
printf '{"type":"user","message":{"role":"user","content":"[сторож сессий] Работы не видно 30 мин. Продолжай текущий шаг."}}\n' > "$T/transcript.jsonl"
out=$(run_hook)
verdict "$(is "$(rc)" 0)" "(е) после ответа — только пинок сторожа, команда боту, вложение в обработке, служебные файлы — код 0" "код $(rc), stderr: $out"

new_stand
printf '{"type":"user","message":{"role":"user","content":"Контекст очищен сторожем сессий (это не сообщение владельца)."}}\n' > "$T/transcript.jsonl"
printf 'служебное\n' > "$T/log/inbox/README.txt"
out=$(run_hook)
verdict "$(is "$(rc)" 0)" "(е) сообщений владельца нет, в стенограмме системное уведомление — код 0" "код $(rc)"

# ── (ж) серия ──────────────────────────────────────────────────────────────
new_stand
owner_msg $((NOW - 400))
owner_msg $((NOW - 380))
owner_msg $((NOW - 360))
reply $((NOW - 300))
run_hook >/dev/null
verdict "$(is "$(rc)" 0)" "(ж) серия из 3, один ответ после последнего — код 0" "код $(rc)"

new_stand
owner_msg $((NOW - 400))
reply $((NOW - 390))
owner_msg $((NOW - 380))
owner_msg $((NOW - 360))
run_hook >/dev/null
verdict "$(is "$(rc)" 2)" "(ж) ответ между 1-м и 2-м, после 3-го ничего — код 2" "код $(rc)"

# ── (з) голос и вложение — сообщения владельца ─────────────────────────────
new_stand
owner_msg $((NOW - 300)) файл
run_hook >/dev/null
verdict "$(is "$(rc)" 2)" "(з) вложение владельца без ответа — код 2" "код $(rc)"

new_stand
owner_msg $((NOW - 300)) голос
run_hook >/dev/null
verdict "$(is "$(rc)" 2)" "(з) голосовое владельца без ответа — код 2" "код $(rc)"

new_stand
owner_msg $((NOW - 300)) голос      # файл расшифровки лёг в NOW-210
reply $((NOW - 250))
run_hook >/dev/null
verdict "$(is "$(rc)" 2)" "(з) отправка между приёмом голоса и появлением расшифровки — не ответ (код 2)" "код $(rc)"

new_stand
owner_msg $((NOW - 300)) голос
reply $((NOW - 150))
run_hook >/dev/null
verdict "$(is "$(rc)" 0)" "(з) ответ после расшифровки голоса — код 0" "код $(rc)"

# ── (и) fail-open ──────────────────────────────────────────────────────────
new_stand
rm -rf "$T/log/inbox"
run_hook >/dev/null
verdict "$(is "$(rc)" 0)" "(и) каталога inbox нет — код 0" "код $(rc)"

new_stand
run_hook >/dev/null
verdict "$(is "$(rc)" 0)" "(и) inbox пуст — код 0" "код $(rc)"

new_stand
owner_msg $((NOW - 300))
rm -f "$T/log/tg_out.jsonl"
run_hook >/dev/null
verdict "$(is "$(rc)" 0)" "(и) журнала отправок нет — код 0" "код $(rc)"

new_stand
owner_msg $((NOW - 300))
run_hook false 'не json' >/dev/null
verdict "$(is "$(rc)" 0)" "(и) битый payload — код 0" "код $(rc)"

# ── (к) жёлтый режим и жёсткость — по конфигу ──────────────────────────────
twice() { run_hook >/dev/null; local a; a=$(rc); run_hook >/dev/null; printf '%s %s' "$a" "$(rc)"; }

new_stand "CHANNEL_REPLY_YELLOW_FROM=\"$TODAY\"
CHANNEL_REPLY_YELLOW_DAYS=\"3\"
CHANNEL_REPLY_STRICT=\"1\""
owner_msg $((NOW - 300))
codes=$(twice)
verdict "$(is "$codes" "2 0")" "(к) жёлтый идёт, STRICT=1 — всё ещё напоминание один раз (2, 0)" "коды $codes"

new_stand "CHANNEL_REPLY_YELLOW_FROM=\"$LONG_AGO\"
CHANNEL_REPLY_YELLOW_DAYS=\"3\"
CHANNEL_REPLY_STRICT=\"1\""
owner_msg $((NOW - 300))
codes=$(twice)
verdict "$(is "$codes" "2 2")" "(к) жёлтый истёк, STRICT=1 — блок на каждом Stop (2, 2)" "коды $codes"
reply $((NOW - 10))
run_hook >/dev/null
verdict "$(is "$(rc)" 0)" "(к) жёлтый истёк, STRICT=1, ответ ушёл — код 0" "код $(rc)"

new_stand "CHANNEL_REPLY_YELLOW_FROM=\"$LONG_AGO\"
CHANNEL_REPLY_YELLOW_DAYS=\"3\"
CHANNEL_REPLY_STRICT=\"0\""
owner_msg $((NOW - 300))
codes=$(twice)
verdict "$(is "$codes" "2 0")" "(к) жёлтый истёк, STRICT=0 — напоминание один раз (2, 0)" "коды $codes"

new_stand "CHANNEL_REPLY_YELLOW_FROM=\"$LONG_AGO\"
CHANNEL_REPLY_YELLOW_DAYS=\"30\"
CHANNEL_REPLY_STRICT=\"1\""
owner_msg $((NOW - 300))
codes=$(twice)
verdict "$(is "$codes" "2 0")" "(к) длина жёлтого из конфига (30 дн) — ещё жёлтый (2, 0)" "коды $codes"

# ── (л) метрика: доля сообщений без ответа ─────────────────────────────────
# X — начало серии наблюдений, 3 ч назад. В окне 5 сообщений владельца:
#   m1 X — ответ X+60; m2 X+600 и голос m3 X+620 — один ответ X+800 после
#   серии; m4 X+2400 — после него лишь весть демона и отказ доставки;
#   m5 X+4800 — ответ X+4860 (уже после m4+40 мин, в срок m4 не входит).
# Вне окна — безответное m0 трое суток назад; в окне — команда боту без файла.
new_stand
X=$((NOW - 3 * 3600))
owner_msg $((NOW - 3 * 86400))
owner_msg "$X";               reply $((X + 60))
owner_msg $((X + 600))
owner_msg $((X + 620)) голос; reply $((X + 800))
owner_msg $((X + 2400));      daemon $((X + 2460)); refused $((X + 2520))
owner_msg $((X + 3000)) команда
owner_msg $((X + 4800));      reply $((X + 4860))
mout=$(cd "$T" && env HARNESS_HOME="$T/home" HARNESS_INSTALL_CONF="$T/install.conf" \
        HARNESS_CONF="$T/harness.conf" LOG_DIR="$T/log" HARNESS_LOG_DIR="$T/log" \
        timeout 60 python3 "$METER" --без-ответа --окно "$(date -d "@$((X - 3600))" '+%F %T')" "$(date -d "@$NOW" '+%F %T')" 2>&1)
verdict "$(printf '%s' "$mout" | grep -q 'без ответа: 1 из 5' && echo 1 || echo 0)" \
    "(л) метрика за окно: «без ответа: 1 из 5» (серия — один ответ, демон и отказ — не ответ, команда и вне окна — не в счёт)" \
    "прибор сказал: $(printf '%s' "$mout" | tail -3 | tr '\n' ' ')"

# ── (м) квитанция-обещание — не ответ ──────────────────────────────────────
# Улика 03.11.2026: 02:10:34 «Прополка карты работает?», 02:11:08 смена
# отправила квитанцию ниже, ответ по сути остался в терминале, хук промолчал.
PROMISE='Принял: проверяю, работает ли прополка карты (последний запуск и результат). Ответ коротко следом.'
say() {  # $1 = эпоха доставки, $2 = текст исходящего смены
    python3 -c 'import json, sys; print(json.dumps({"ts": sys.argv[1], "uroven": "", "iz_sessii": True, "text": sys.argv[2]}, ensure_ascii=False))' \
        "$(iso "$1")" "$2" >> "$T/log/tg_out.jsonl"
}
chars() { python3 -c 'import sys; print(len(sys.argv[1]))' "$1"; }
padded() {  # $1 = текст, $2 = нужная длина: добивается точками
    python3 -c 'import sys; s = sys.argv[1]; print(s + "." * (int(sys.argv[2]) - len(s)))' "$1" "$2"
}
# Штатные признаки задаются явно: CHANNEL_REPLY_MARKERS вызывающего сюда не протекает.
STD_MARKERS="$ROOT/harness/config/channel-reply.yaml"

new_stand
owner_msg $((NOW - 300))
say $((NOW - 270)) "$PROMISE"
out=$(CHANNEL_REPLY_MARKERS="$STD_MARKERS" run_hook)
verdict "$(is "$(rc)" 2)" "(м1) после сообщения только квитанция «…ответ коротко следом» — код 2" "код $(rc)"
verdict "$(printf '%s' "$out" | grep -q 'Квитанция' && echo 1 || echo 0)" \
    "(м1) stderr говорит, что квитанция ответом не считается" "stderr: ${out:-пусто}"

new_stand
owner_msg $((NOW - 300))
say $((NOW - 270)) "$PROMISE"
say $((NOW - 200)) 'Да, работает: самотест 8/8, полоть нечего.'
CHANNEL_REPLY_MARKERS="$STD_MARKERS" run_hook >/dev/null
verdict "$(is "$(rc)" 0)" "(м2) квитанция, затем ответ по сути — код 0" "код $(rc)"

new_stand
owner_msg $((NOW - 300))
say $((NOW - 200)) 'Принял, работаю по очереди. Идут два помощника: хакатон и закрытие «простого стиля».'
CHANNEL_REPLY_MARKERS="$STD_MARKERS" run_hook >/dev/null
verdict "$(is "$(rc)" 0)" "(м3) «Принял, работаю по очереди…» без обещания — ответ (код 0)" "код $(rc)"

LONG='Прополка карты работает: последний запуск сегодня в 01:40, самотест 8/8, полоть нечего — архив пуст, лишних карточек в dev-map.yaml нет. Ночной запуск по расписанию идёт сразу за гейтами, отдельной правки не нужно; подробнее отвечу по журналу прогона, если захочешь.'
new_stand
owner_msg $((NOW - 300))
say $((NOW - 200)) "$LONG"
CHANNEL_REPLY_MARKERS="$STD_MARKERS" run_hook >/dev/null
verdict "$([ "$(rc)" = 0 ] && [ "$(chars "$LONG")" -gt 200 ] && echo 1 || echo 0)" \
    "(м4) длинный ответ по сути со словом «отвечу» ($(chars "$LONG") знаков > 200) — код 0" "код $(rc)"

# Граница предела: ровно promise_max_chars — ещё квитанция, на знак длиннее — уже ответ.
for edge in 200:2 201:0; do
    want_len=${edge%%:*} want_rc=${edge##*:}
    text=$(padded "$PROMISE" "$want_len")
    new_stand
    owner_msg $((NOW - 300))
    say $((NOW - 270)) "$text"
    CHANNEL_REPLY_MARKERS="$STD_MARKERS" run_hook >/dev/null
    verdict "$([ "$(rc)" = "$want_rc" ] && [ "$(chars "$text")" = "$want_len" ] && echo 1 || echo 0)" \
        "(м4) граница: квитанция-обещание $want_len знаков — код $want_rc" "код $(rc), длина $(chars "$text")"
done

new_stand
owner_msg $((NOW - 300))
say $((NOW - 270)) "$PROMISE"
CHANNEL_REPLY_MARKERS="$T/нет-такого.yaml" run_hook >/dev/null
verdict "$(is "$(rc)" 0)" "(м5) файла признаков нет — правило выключено, квитанция считается ответом (код 0)" "код $(rc)"

new_stand
owner_msg $((NOW - 300))
say $((NOW - 270)) "$PROMISE"
printf 'promise_markers: [\n  - "незакрыто\npromise_max_chars: {\n' > "$T/broken.yaml"
CHANNEL_REPLY_MARKERS="$T/broken.yaml" run_hook >/dev/null
verdict "$(is "$(rc)" 0)" "(м6) битый yaml признаков — не падает, правило выключено (код 0)" "код $(rc)"

# Маркер, который не совпадает ни с чем: квитанция проходит, только если
# признаки читаются из yaml, а не из набора, зашитого в код.
new_stand
owner_msg $((NOW - 300))
say $((NOW - 270)) "$PROMISE"
printf "promise_markers:\n  - '(?!x)x'\npromise_max_chars: 200\n" > "$T/never.yaml"
CHANNEL_REPLY_MARKERS="$T/never.yaml" run_hook >/dev/null
verdict "$(is "$(rc)" 0)" "(м7) в yaml только невозможный маркер — квитанция считается ответом (код 0)" "код $(rc)"

new_stand
owner_msg $((NOW - 300))
say $((NOW - 270)) "$PROMISE"
printf "promise_markers: 'ответ\\\\s+(коротко\\\\s+)?следом'\npromise_max_chars: 200\n" > "$T/scalar.yaml"
CHANNEL_REPLY_MARKERS="$T/scalar.yaml" run_hook >/dev/null
verdict "$(is "$(rc)" 0)" "(м8) promise_markers строкой, а не списком — правило выключено (код 0)" "код $(rc)"

new_stand
owner_msg $((NOW - 300))
say $((NOW - 270)) 'Уточнение: коммита части А в гите ещё нет. Тесты пишет следующий, коммит следом.'
CHANNEL_REPLY_MARKERS="$STD_MARKERS" run_hook >/dev/null
verdict "$(is "$(rc)" 0)" "(м9) короткий ответ по сути со словом «следом» — ответ (код 0)" "код $(rc)"

# ── (н) ход начат служебным сообщением ─────────────────────────────────────
# Образцы — настоящие записи из ~/.claude/projects (test_channel_reply_guard.samples.jsonl).
# Хвост хода как в жизни: ответ модели, tool_result, обратная связь Stop-хука.
SAMPLES="$HERE/test_channel_reply_guard.samples.jsonl"
turn() {  # $1 = имя образца запроса; пишет стенограмму стенда
    python3 - "$SAMPLES" "$1" "$T/transcript.jsonl" <<'PY'
import json, sys
by = {}
for line in open(sys.argv[1], encoding="utf-8"):
    row = json.loads(line)
    by[row["sample"]] = line.rstrip("\n")
order = [sys.argv[2], "assistant", "tool_result", "assistant", "stop_feedback"]
open(sys.argv[3], "w", encoding="utf-8").write("\n".join(by[n] for n in order) + "\n")
PY
}
journal() { python3 -c '
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1] + "/log/ответы-в-канал.jsonl", encoding="utf-8")] if len(sys.argv) > 1 else []
print(" ".join(r["event"] + ":" + r["outcome"] + ":" + str(r.get("data", {}).get("reason", "")) for r in rows))' "$T" 2>/dev/null; }

for sample in task_notification restart watchdog_report watchdog_work watchdog_limit clear rotation limit_guard; do
    new_stand
    owner_msg $((NOW - 300)) текст корень
    turn "$sample"
    out=$(run_hook)
    verdict "$(is "$(rc)" 0)" "(н) ход начат служебным («$sample») при висящем сообщении владельца — код 0" "код $(rc), stderr: $out"
done

for sample in owner_kick owner_voice owner_inbox owner_typed; do
    new_stand
    owner_msg $((NOW - 300)) текст корень
    turn "$sample"
    out=$(run_hook)
    verdict "$(is "$(rc)" 2)" "(н) ход начат владельцем («$sample») — код 2" "код $(rc)"
done

# Служебный ход не тратит антиповтор: следующий ход владельца всё ещё блокируется.
new_stand
owner_msg $((NOW - 300)) текст корень
turn task_notification
run_hook >/dev/null; svc=$(rc)
turn owner_typed
run_hook >/dev/null; own=$(rc)
verdict "$([ "$svc" = 0 ] && [ "$own" = 2 ] && echo 1 || echo 0)" \
    "(н) служебный ход антиповтор не тратит: затем ход владельца — код 2" "коды $svc, $own"

# STRICT=1: служебный ход не блокируется на каждом Stop.
new_stand "CHANNEL_REPLY_YELLOW_FROM=\"$LONG_AGO\"
CHANNEL_REPLY_YELLOW_DAYS=\"3\"
CHANNEL_REPLY_STRICT=\"1\""
owner_msg $((NOW - 300))
turn watchdog_report
codes=$(twice)
verdict "$(is "$codes" "0 0")" "(н) STRICT=1, ход начат пинком сторожа — коды 0 0" "коды $codes"
turn owner_typed
codes=$(twice)
verdict "$(is "$codes" "2 2")" "(н) STRICT=1, ход начат владельцем — коды 2 2" "коды $codes"

# ── (о) законная дверь: метка «ответ не требуется» ─────────────────────────
DOOR="reply-not-needed"
new_stand "CHANNEL_REPLY_YELLOW_FROM=\"$LONG_AGO\"
CHANNEL_REPLY_YELLOW_DAYS=\"3\"
CHANNEL_REPLY_STRICT=\"1\""
owner_msg $((NOW - 300))
printf 'владелец написал «ок», ответа не ждёт\n' > "$T/log/$DOOR"
touch -d "@$((NOW - 100))" "$T/log/$DOOR"
codes=$(twice)
verdict "$(is "$codes" "0 0")" "(о) метка с причиной новее сообщения — коды 0 0 (STRICT=1)" "коды $codes"
owner_msg $((NOW - 50)) текст корень
run_hook >/dev/null
verdict "$(is "$(rc)" 2)" "(о) новое сообщение владельца после метки — метка не действует (код 2)" "код $(rc)"

new_stand
owner_msg $((NOW - 300))
: > "$T/log/$DOOR"; touch -d "@$((NOW - 100))" "$T/log/$DOOR"
run_hook >/dev/null
verdict "$(is "$(rc)" 2)" "(о) метка без причины — не дверь (код 2)" "код $(rc)"

new_stand
owner_msg $((NOW - 300))
printf 'причина\n' > "$T/log/$DOOR"; touch -d "@$((NOW - 400))" "$T/log/$DOOR"
run_hook >/dev/null
verdict "$(is "$(rc)" 2)" "(о) метка старше сообщения — не дверь (код 2)" "код $(rc)"

new_stand
owner_msg $((NOW - 4000))
printf 'причина\n' > "$T/log/$DOOR"; touch -d "@$((NOW - 3600))" "$T/log/$DOOR"
run_hook >/dev/null
verdict "$(is "$(rc)" 2)" "(о) метка старше срока (60 мин > 30) — не дверь (код 2)" "код $(rc)"

# ── (п) журнал срабатываний ────────────────────────────────────────────────
new_stand
owner_msg $((NOW - 300)) текст корень
run_hook >/dev/null          # блок
run_hook >/dev/null          # антиповтор
turn task_notification
run_hook >/dev/null          # служебный
printf 'причина\n' > "$T/log/$DOOR"
run_hook >/dev/null          # дверь
got=$(journal)
verdict "$(is "$got" "channel_reply.block:refused:owner channel_reply.skip:skipped:repeat channel_reply.skip:skipped:service channel_reply.skip:skipped:door")" \
    "(п) журнал: блок, повтор, служебный ход, дверь — по строке на Stop" "журнал: ${got:-пусто}"

new_stand
owner_msg $((NOW - 300))
reply $((NOW - 200))
run_hook >/dev/null
verdict "$([ ! -e "$T/log/ответы-в-канал.jsonl" ] && echo 1 || echo 0)" \
    "(п) нет безответного сообщения — журнал не пишется" "файл есть"

# ── боевой LOG_DIR не тронут ───────────────────────────────────────────────
if [ -n "$REAL_LOG" ] && [ -d "$REAL_LOG" ]; then
    # Имена берутся у самого хука: то, что он создал в LOG_DIR стенда (г).
    touched=""
    while IFS= read -r name; do
        [ -n "$name" ] && [ "$REAL_LOG/$name" -nt "$START_MARK" ] && touched="$touched $name"
    done <<< "${STATE_NAMES:-}"
    verdict "$([ -n "${STATE_NAMES:-}" ] && [ -z "$touched" ] && echo 1 || echo 0)" \
        "боевой LOG_DIR: хук не писал туда своё состояние" "${touched:-имён состояния нет — (г) не прошёл}"
fi

echo
echo "всего путей: $total, неудач: $bad"
[ "$bad" = 0 ]
