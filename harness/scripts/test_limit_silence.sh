#!/usr/bin/env bash
# Проба тишины при лимите: одно сообщение со временем, дальше молчит ВСЁ.
#
# Откуда взята. Владелец 17.10.2026 снимком канала: «Если упёрся в лимит
# подписки, то всё, пауза, до того момента как не обновится <…> зачем потом
# столько сообщений слать об одном и том же <…> первое самое должно сразу
# иметь инфорсацию о времени, и время должно быть в 24 часовом формате <…> до
# того момента как будет обновление никаких сообщений больше поступать не
# должно (ну кроме там смены сессии, это ладно) <…> дернуть он должен ровно в
# тот момент когда лимит обновится».
#
# Замер того окна (12:41 → 15:10, $LOG_DIR/tg_out.jsonl): пять сообщений
# вместо одного — «упёрся в лимит» БЕЗ времени, «фоновый демон не получил
# ответ модели (resets 3:10pm)», дважды «смена не отчитывается вам N минут» и
# «лимит обновился».
#
# Проверка СОЗДАЁТ своё условие: свой LOG_DIR, свой паспорт, подставной curl и
# tmux. Боевой канал и боевая панель не трогаются (память:
# проба-играет-на-боевом), а ключ в паспорте ГЕНЕРИРУЕТСЯ, а не копируется
# (память: the-probes-secret-was-a-real-one).
#
# Имена переменных ЛАТИНИЦЕЙ: bash не берёт кириллические идентификаторы.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT

STAND_PROJECT="$STAND/proekt"
TR_DIR_NAME="${STAND_PROJECT//\//-}"
mkdir -p "$STAND_PROJECT/scripts/lib" "$STAND_PROJECT/harness/demons" \
         "$STAND/zhurnal" "$STAND/transkripty/$TR_DIR_NAME" "$STAND/bin"

for f in tg_send.sh limit_mark.py limit-in-log.py stall-in-log.py pause.py \
         check-channel-messages.py language-level.py; do
    cp "$HERE/$f" "$STAND_PROJECT/scripts/"
done
cp "$HERE/lib/config.sh" "$HERE/lib/config.py" "$HERE/lib/patterns.py" \
   "$HERE/lib/channel_queue.py" "$STAND_PROJECT/scripts/lib/" 2>/dev/null || true
# Дверь в панель подключает дверь журнала событий: без неё source в pane.sh падает.
cp "$HERE/lib/pane.sh" "$HERE/lib/events.sh" "$HERE/lib/events.py" "$STAND_PROJECT/scripts/lib/" 2>/dev/null || true
cp "$HERE/../harness/demons/limit-watch.sh" "$STAND_PROJECT/harness/demons/"

# Паспорт стенда: номер чата выдуманный, ключ сгенерирован здесь и сейчас.
CHAT_STENDA=100500          # не-секрет: выдуманный номер чата стенда
{
    printf 'TG_CHAT_ID="%s"\n' "$CHAT_STENDA"            # не-секрет: чат стенда
    printf 'TG_TOKEN_FILE="%s"\n' "$STAND/klyuch"        # не-секрет: путь, а не значение
    printf 'PROJECT_DIR="%s"\n' "$STAND_PROJECT"
    printf 'LOG_DIR="%s"\n' "$STAND/zhurnal"
    printf 'TMUX_SESSION="proba-paneli"\n'
    # Ключ пояса стоит в паспорте, как на живой машине: общий загрузчик берёт
    # значение окружения ТОЛЬКО для ключей, названных в файле, — иначе проба
    # мерила бы не ту дверь.
    printf 'OWNER_TZ=""\n'
} > "$STAND/install.conf"
python3 -c "import secrets,io,sys; io.open(sys.argv[1],'w').write('11'+secrets.token_hex(16)+'\n')" "$STAND/klyuch"

# Подставной curl: ответ Bot API, какой ждёт tg_send, и строка в журнал вызовов.
cat > "$STAND/bin/curl" <<'CURL'
#!/usr/bin/env bash
printf 'vyzvan\n' >> "$STAND_CURL_LOG"
printf '{"ok":true,"result":{"message_id":1}}'
CURL
# Подставной tmux: панель якобы занята агентом, нажатия пишутся в файл.
cat > "$STAND/bin/tmux" <<'TMUX'
#!/usr/bin/env bash
case "${1:-}" in
    display-message)
        # pane_dead спрашивают первым: у мёртвой панели tmux печатает прежнюю
        # команду, и живой сторож обязан это различать.
        case "${*}" in *pane_dead*) printf '%s\n' "${PANE_DEAD_OTVET:-0}" ;;
                       *) printf '%s\n' "${PANE_CMD_OTVET:-claude}" ;; esac ;;
    has-session)     exit 0 ;;
    send-keys)       printf '%s\n' "$*" >> "$STAND_TMUX_LOG" ;;
esac
TMUX
chmod +x "$STAND/bin/curl" "$STAND/bin/tmux"

CURL_LOG="$STAND/zhurnal/curl.log"
TMUX_LOG="$STAND/zhurnal/tmux.log"
MARK="$STAND/zhurnal/лимит-подписки.состояние"
TRANSCRIPT="$STAND/transkripty/$TR_DIR_NAME/proba.jsonl"

sreda() {  # общее окружение стенда
    env PATH="$STAND/bin:$PATH" \
        STAND_CURL_LOG="$CURL_LOG" STAND_TMUX_LOG="$TMUX_LOG" \
        LOG_DIR="$STAND/zhurnal" PROJECT_DIR="$STAND_PROJECT" \
        HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/install.conf" \
        HEARTBEAT_DIR="$STAND/zhurnal/heartbeat" \
        CLAUDE_PROJECTS_DIR="$STAND/transkripty" \
        PANE_ENTER_DELAY=0 LIMIT_KICK_DELAY_SEC=0 \
        PANE_CMD_OTVET="${PANE_CMD_OTVET:-claude}" PANE_DEAD_OTVET="${PANE_DEAD_OTVET:-0}" \
        "$@"
}

otpravit() {  # аргументы tg_send.sh; печатает код возврата
    local rc=0
    sreda bash "$STAND_PROJECT/scripts/tg_send.sh" "$@" >/dev/null 2>&1 || rc=$?
    printf '%s\n' "$rc"
}

mark_judge() {  # аргументы limit_mark.py
    sreda python3 "$STAND_PROJECT/scripts/limit_mark.py" "$@" 2>/dev/null
}

# Счёт строк — wc, а не `grep -c . || echo 0`: на ПУСТОМ файле grep печатает
# «0» И отдаёт код 1, поэтому «||» дописывал второй ноль, и проба сравнивала
# «0» с «0\n0» (память: failure-under-set-e-is-silent).
strok_v() { [ -f "$1" ] && wc -l < "$1" | tr -d ' ' || echo 0; }
vyzovov_curl() { strok_v "$CURL_LOG"; }
v_ocheredi() { strok_v "$STAND/zhurnal/панель-недоставленное.jsonl"; }
nazhatij() { strok_v "$TMUX_LOG"; }

zhurnal_sessii() {  # $1 = секунд назад успешный ответ («нет» — не было), $2 = отказ
    python3 - "$TRANSCRIPT" "$1" "$2" <<'ZH'
import datetime, json, sys, time
путь, ответ, отказ = sys.argv[1], sys.argv[2], sys.argv[3]


def метка(сек):
    return datetime.datetime.fromtimestamp(time.time() - int(сек),
                                           datetime.UTC).isoformat()


строки = []
if ответ != "нет":
    строки.append(json.dumps({"type": "assistant", "timestamp": метка(ответ)}))
if отказ != "нет":
    строки.append(json.dumps({"apiErrorStatus": 429, "timestamp": метка(отказ),
                              "resetsAt": int(time.time()) + 7200}))
open(путь, "w", encoding="utf-8").write("\n".join(строки) + "\n")
ZH
}

echo "── здоровая половина: метки нет — сообщение уходит ──"
: > "$CURL_LOG"
case_check "без метки лимита обычное сообщение доставлено (код 0)" \
    "0" "$(otpravit 'Статус 15:40. Живая проверка: 191 из 262.')"
case_check "и транспорт действительно позван" "1" "$(vyzovov_curl)"

echo "── БОЛЬНОЙ СЛУЧАЙ 17.10: метка стоит — обычное сообщение ПРИДЕРЖАНО ──"
mark_judge --поставить limit --сброс "$(( $(date +%s) + 7200 ))" >/dev/null
: > "$CURL_LOG"
# Исход по умолчанию — ПРИДЕРЖАТЬ И ДОСЛАТЬ: звонящий о правиле не знает (из
# 24 отправителей владельцу — никто), а молча выброшенная весть о нарушении
# гигиены или о мёртвом механизме стоит дороже залпа (оба ревью 17.10.2026).
RC_HELD=$(otpravit 'Смена работает, но не отчитывается вам 69 минут — статус у неё прошу уже несколько раз.')
case_check "при держащемся лимите сообщение принято, но НЕ доставлено (код 0)" \
    "0" "$RC_HELD"
case_check "и транспорт НЕ позван ни разу" "0" "$(vyzovov_curl)"
case_check "придержанное записано в журнал, а не потеряно молча" \
    "да" "$([ -s "$STAND/zhurnal/придержано-лимитом.jsonl" ] && echo да || echo нет)"
case_check "в журнале придержанного виден текст сообщения" \
    "да" "$(grep -q 'не отчитывается' "$STAND/zhurnal/придержано-лимитом.jsonl" 2>/dev/null && echo да || echo нет)"

echo "── смена сессии и само сообщение о лимите проходят ──"
: > "$CURL_LOG"
case_check "с «--несмотря-на-лимит» сообщение уходит (код 0)" \
    "0" "$(otpravit --несмотря-на-лимит 'Смена поднята: продолжаю живую проверку механизмов.')"
case_check "и транспорт позван" "1" "$(vyzovov_curl)"

echo "── весть о лимите: время числом, 24-часовой формат, человеческим языком ──"
SBROS_UNIX=$(( $(date +%s) + 7200 ))
rm -f "$MARK"
mark_judge --поставить limit --сброс "$SBROS_UNIX" >/dev/null
VEST=$(mark_judge --весть-о-лимите)
ZHDEM_CHAS=$(sreda python3 -c 'import datetime,sys; print(datetime.datetime.fromtimestamp(int(sys.argv[1])).strftime("%H:%M"))' "$SBROS_UNIX")
case_check "ожидание часа посчиталось (иначе проверка ниже зелена ни за что)" \
    "да" "$([ -n "$ZHDEM_CHAS" ] && echo да || echo нет)"
case_check "весть называет время обновления ровно так, как считает машина" \
    "да" "$(printf '%s' "$VEST" | grep -qF "в $ZHDEM_CHAS" && echo да || echo нет)"
case_check "время в 24-часовом формате, без am/pm" \
    "нет" "$(printf '%s' "$VEST" | grep -qiE 'am|pm' && echo да || echo нет)"
case_check "весть не несёт машинного жаргона (429, resets)" \
    "нет" "$(printf '%s' "$VEST" | grep -qiE '429|resets' && echo да || echo нет)"
case_check "весть обещает продолжить после обновления" \
    "да" "$(printf '%s' "$VEST" | grep -q 'продолжу' && echo да || echo нет)"
case_check "судья помнит время сброса стоящей метки" \
    "$SBROS_UNIX" "$(mark_judge --сброс-стоящей)"

echo "── час печатается в поясе ВЛАДЕЛЬЦА, а не машины ──"
# Ревью 17.10.2026: на свежем сервере пояс обычно UTC, и владелец прочёл бы
# час, которого у него нет. Пояс — ключ настроек OWNER_TZ.
CHAS_UTC=$(sreda env OWNER_TZ=UTC python3 "$STAND_PROJECT/scripts/limit_mark.py" --весть-о-лимите --сброс "$SBROS_UNIX")
ZHDEM_UTC=$(python3 -c 'import datetime,sys,zoneinfo; print(datetime.datetime.fromtimestamp(int(sys.argv[1]), zoneinfo.ZoneInfo("UTC")).strftime("%H:%M"))' "$SBROS_UNIX")
case_check "при OWNER_TZ=UTC назван час этого пояса" \
    "да" "$(printf '%s' "$CHAS_UTC" | grep -qF "в $ZHDEM_UTC" && echo да || echo нет)"
case_check "и это НЕ час машины (иначе пояс не применён)" \
    "нет" "$([ "$ZHDEM_UTC" = "$ZHDEM_CHAS" ] && echo да || echo нет)"

echo "── БОЛЬНОЙ СЛУЧАЙ: время сброса CLI не назвал — врать нечем ──"
rm -f "$MARK"
mark_judge --поставить limit >/dev/null
VEST_BEZ=$(mark_judge --весть-о-лимите)
case_check "без времени сброса весть НЕ выдумывает час" \
    "нет" "$(printf '%s' "$VEST_BEZ" | grep -qE 'в [0-9][0-9]:[0-9][0-9]' && echo да || echo нет)"
case_check "и говорит об этом прямо" \
    "да" "$(printf '%s' "$VEST_BEZ" | grep -q 'не назвал' && echo да || echo нет)"
case_check "времени сброса у такой метки нет — код 1" \
    "1" "$(mark_judge --сброс-стоящей >/dev/null 2>&1; echo $?)"

echo "── БОЛЬНОЙ СЛУЧАЙ (ревью 17.10): молчать можно ТОЛЬКО из-за лимита ──"
# У метки три повода. «blocked» — работы в очереди нет, смена у приглашения:
# штатное состояние. «nemota» — смена молчит, а признака лимита НЕТ, и весть о
# мёртвой смене ставит эту метку СТРОКОЙ ВЫШЕ своей отправки.
for POVOD in blocked nemota; do
    rm -f "$MARK"; : > "$CURL_LOG"
    mark_judge --поставить "$POVOD" >/dev/null
    case_check "повод «$POVOD» лимитом не считается — сообщение ушло" \
        "0" "$(otpravit 'Работа встала: агент молчит 25 минут. Признака лимита не видно.')"
    case_check "и транспорт позван при поводе «$POVOD»" "1" "$(vyzovov_curl)"
done

rm -f "$MARK"; : > "$CURL_LOG"
printf '%s limit 0\n' "$(( $(date +%s) - 7 * 3600 ))" > "$MARK"
case_check "метка старше шести часов (смена умерла в лимите) канал не закрывает" \
    "0" "$(otpravit 'Гигиена сервера: порт отдан наружу — нужен разбор.')"
: > "$CURL_LOG"; printf 'мусор\n' > "$MARK"
case_check "битая метка канал не закрывает" \
    "0" "$(otpravit 'Копии базы нет третьи сутки.')"
: > "$CURL_LOG"; : > "$MARK"
case_check "пустая метка (диск был полон при постановке) канал не закрывает" \
    "0" "$(otpravit 'Диск занят на 95% — нужен разбор вручную.')"

echo "── придержанное не теряется: очередь досыла ──"
rm -f "$MARK" "$STAND/zhurnal/панель-недоставленное.jsonl"; : > "$CURL_LOG"
mark_judge --поставить limit --сброс "$(( $(date +%s) + 7200 ))" >/dev/null
case_check "весть, нужная после лимита, принята (код 0) и не доставлена сейчас" \
    "0" "$(otpravit 'Утренняя сводка харнеса: живых 25, МЁРТВЫХ 2.')"
case_check "транспорт при этом не позван" "0" "$(vyzovov_curl)"
case_check "и весть легла в очередь досыла" "1" "$(v_ocheredi)"
case_check "в очереди виден её текст" \
    "да" "$(grep -q 'МЁРТВЫХ' "$STAND/zhurnal/панель-недоставленное.jsonl" && echo да || echo нет)"

RC_NE_DOSYLAT=$(otpravit --не-досылать 'Смена работает, но не отчитывается вам 69 минут.')
case_check "весть, устаревающая после лимита, отвергнута (код 3)" "3" "$RC_NE_DOSYLAT"
case_check "и в очередь досыла она НЕ легла" "1" "$(v_ocheredi)"

RC_DOSYL=$(otpravit --досыл 'Утренняя сводка харнеса: живых 25, МЁРТВЫХ 2.')
case_check "круг досыла при лимите отвергнут (код 3)" "3" "$RC_DOSYL"
case_check "и строк в очереди не прибавилось" "1" "$(v_ocheredi)"

echo "── подъём ровно в момент обновления: пинок в панель ──"
# Отказ в журнале свежий (возобновление не объявлять), а время сброса ПРОШЛО:
# смена стоит, лимит уже обновился — её обязаны разбудить.
rm -f "$MARK" "$TMUX_LOG"
zhurnal_sessii 1500 60
mark_judge --поставить limit --сброс "$(( $(date +%s) - 5 ))" >/dev/null
sreda bash "$STAND_PROJECT/harness/demons/limit-watch.sh" >/dev/null 2>&1
case_check "сброс прошёл — смена разбужена нажатиями в панель" \
    "да" "$([ "$(nazhatij)" -gt 0 ] && echo да || echo нет)"
case_check "в панель ушёл текст про продолжение работы" \
    "да" "$(grep -q 'продолжай' "$TMUX_LOG" 2>/dev/null && echo да || echo нет)"

case_check "нажатий ровно два: текст и Enter отдельно" "2" "$(nazhatij)"
case_check "текст подан с ключами -l -- (иначе tmux примет его за имя клавиши)" \
    "да" "$(grep -q 'send-keys .*-l --' "$TMUX_LOG" && echo да || echo нет)"
case_check "сброс записан как разбуженный — второй раз тем же сбросом не будим" \
    "да" "$([ -s "$STAND/zhurnal/лимит-подписки.пинок-сброса" ] && echo да || echo нет)"
: > "$TMUX_LOG"
sreda bash "$STAND_PROJECT/harness/demons/limit-watch.sh" >/dev/null 2>&1
case_check "БОЛЬНОЙ СЛУЧАЙ: тот же сброс второго пинка не даёт" "0" "$(nazhatij)"

echo "── БОЛЬНОЙ СЛУЧАЙ: в панели не агент — пинка нет ──"
rm -f "$MARK" "$TMUX_LOG" "$STAND/zhurnal/лимит-подписки.пинок-сброса"
zhurnal_sessii 1500 60
mark_judge --поставить limit --сброс "$(( $(date +%s) - 5 ))" >/dev/null
PANE_CMD_OTVET=bash sreda bash "$STAND_PROJECT/harness/demons/limit-watch.sh" >/dev/null 2>&1
case_check "в панели оболочка — текст в неё не шлём" "0" "$(nazhatij)"
case_check "и причина названа в журнале сторожа" \
    "да" "$(grep -q 'не claude/node' "$STAND/zhurnal/limit-watch.log" && echo да || echo нет)"

echo "── сброс в НЕСКОЛЬКИХ секундах: сторож досыпает до него ──"
rm -f "$MARK" "$TMUX_LOG" "$STAND/zhurnal/лимит-подписки.пинок-сброса"
zhurnal_sessii 1500 60
# Сброс на ТРИ секунды вперёд, а ждём не меньше двух: секунды целые, и между
# постановкой метки и стартом сторожа успевает пройти доля секунды — при
# сбросе «+2» замер давал 1 и красил прогон ворот на верном коде (17.10.2026).
mark_judge --поставить limit --сброс "$(( $(date +%s) + 3 ))" >/dev/null
NACHALO=$(date +%s)
sreda bash "$STAND_PROJECT/harness/demons/limit-watch.sh" >/dev/null 2>&1
PROSHLO=$(( $(date +%s) - NACHALO ))
case_check "смена разбужена после сна до секунды сброса" \
    "да" "$([ "$(nazhatij)" -gt 0 ] && echo да || echo нет)"
case_check "и сторож действительно ждал (сон не проскочил)" \
    "да" "$([ "$PROSHLO" -ge 2 ] && echo да || echo нет)"

echo "── БОЛЬНОЙ СЛУЧАЙ: смена уже работает — будить нечего ──"
rm -f "$MARK" "$TMUX_LOG" "$STAND/zhurnal/лимит-подписки.пинок-сброса"
zhurnal_sessii 10 60          # успешный ответ 10 с назад: работа идёт
mark_judge --поставить limit --сброс "$(( $(date +%s) - 5 ))" >/dev/null
sreda bash "$STAND_PROJECT/harness/demons/limit-watch.sh" >/dev/null 2>&1
case_check "работающую смену пинок не перебивает" "0" "$(nazhatij)"

echo "── БОЛЬНОЙ СЛУЧАЙ: до сброса далеко — пинка не тратим ──"
rm -f "$MARK" "$TMUX_LOG"
zhurnal_sessii 1500 60
mark_judge --поставить limit --сброс "$(( $(date +%s) + 2400 ))" >/dev/null
sreda bash "$STAND_PROJECT/harness/demons/limit-watch.sh" >/dev/null 2>&1
case_check "до обновления 40 мин — панель не трогаем" "0" "$(nazhatij)"

printf '\nТИШИНА ПРИ ЛИМИТЕ: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
