#!/usr/bin/env bash
# Проба ежеминутного сторожа лимита: свой стенд, свой канал, боевое не трогаем.
#
# Откуда взята. Владелец 13.09.2026: «почему это сразу не ловится как только
# появляется сообщение о лимите? Как сделать срабатывание мгновенным?» Смена
# простояла 3,5 часа; сообщение о начале простоя дошло за 5 минут, о конце —
# не дошло вовсе, пока смена не сказала сама.
#
# Проверка СОЗДАЁТ своё условие: подставной транскрипт с отказом 429, свой
# LOG_DIR и подменённый tg_send.sh — иначе она мерила бы боевую сессию и слала
# владельцу пробные сообщения (память: проба-играет-на-боевом).
#
# Имена переменных ЛАТИНИЦЕЙ: bash не берёт кириллические идентификаторы и
# печатает в ошибку само значение (память: cyrillic-in-bash-names).
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Дверь для мутаций: LIMIT_WATCH_UNDER_TEST — путь к испорченной копии сторожа
# (копия лежит в дереве с scripts/lib: сторож берёт загрузчик конфига от себя).
WATCHER="${LIMIT_WATCH_UNDER_TEST:-$HERE/../harness/demons/limit-watch.sh}"
source "$HERE/lib/selftest_cases.sh"

STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT

# Подставное дерево: проект с фальшивым каналом и прибором из боевого кода.
# Каталог транскриптов назван ТАК ЖЕ, как его вычисляет сторож из пути проекта
# (путь с «/» заменёнными на «-»), иначе проба молча меряет пустоту: первая
# редакция пробы дала 7 «неудач» там, где сторож просто не находил транскрипт.
STAND_PROJECT="$STAND/proekt"
TR_DIR_NAME="${STAND_PROJECT//\//-}"
mkdir -p "$STAND/proekt/scripts/lib" "$STAND/zhurnal" "$STAND/transkripty/$TR_DIR_NAME"
cp "$HERE/limit-in-log.py" "$HERE/stall-in-log.py" "$HERE/limit_mark.py" "$STAND/proekt/scripts/"
cp "$HERE/lib/config.sh" "$HERE/lib/config.py" "$STAND/proekt/scripts/lib/"
# Подставной канал повторяет разбор доводов БОЕВОЙ двери: ключ
# «--несмотря-на-лимит» (им помечены сообщения о лимите и о смене сессии) не
# является текстом. Первая редакция писала «$1» и после появления ключа стала
# записывать в канал сам ключ — проба читала текст и перестала его видеть.
cat > "$STAND/proekt/scripts/tg_send.sh" <<'KANAL'
#!/usr/bin/env bash
for A in "$@"; do
    [ "$A" = "--несмотря-на-лимит" ] && continue
    printf '%s\n' "$A" >> "$LOG_DIR/kanal.txt"
    break
done
KANAL
chmod +x "$STAND/proekt/scripts/tg_send.sh"

TRANSCRIPT="$STAND/transkripty/$TR_DIR_NAME/proba.jsonl"

progon() {  # запускает сторожа на стенде
    env LOG_DIR="$STAND/zhurnal" PROJECT_DIR="$STAND/proekt" \
        HEARTBEAT_DIR="$STAND/zhurnal/heartbeat" \
        CLAUDE_PROJECTS_DIR="$STAND/transkripty" \
        HARNESS_INSTALL_CONF="$STAND/net-takogo.conf" HARNESS_CONF="$STAND/net-takogo.conf" \
        bash "$WATCHER" >/dev/null 2>&1
}

skazano() { grep -c . "$STAND/zhurnal/kanal.txt" 2>/dev/null || echo 0; }

# Жизнь смены задаётся ЗАПИСЬЮ УСПЕШНОГО ОТВЕТА, а не `touch` (ревью 27.09,
# F-01). Прежний стенд оживлял смену прикосновением к файлу — и тем кодировал
# в пробе тот самый дефект, который она должна ловить: сторож считал работой
# любую дописанную строку, включая отказ 429 и пинок сторожа. Проба, задающая
# условие тем же неверным способом, что и код, не проверяет ничего.
zhurnal() {  # $1 = секунд назад успешный ответ (или «нет»), $2 = секунд назад отказ
    python3 - "$TRANSCRIPT" "$1" "$2" <<'ZH'
import datetime, json, sys, time
путь, ответ, отказ = sys.argv[1], sys.argv[2], sys.argv[3]


def метка(сек):
    момент = datetime.datetime.fromtimestamp(time.time() - int(сек), datetime.UTC)
    return момент.isoformat()


строки = []
if ответ != "нет":
    строки.append(json.dumps({"type": "assistant", "timestamp": метка(ответ)}))
if отказ != "нет":
    строки.append(json.dumps({"type": "assistant", "error": "rate_limit",
                              "isApiErrorMessage": True, "apiErrorStatus": 429,
                              "timestamp": метка(отказ)}))
with open(путь, "w", encoding="utf-8") as файл:
    файл.write("\n".join(строки) + ("\n" if строки else ""))
ZH
}

echo "── БОЛЬНОЙ СЛУЧАЙ: свежий отказ 429, смена молчит ──"
zhurnal 240 60
progon
case_check "о лимите сказано с ПЕРВОГО обхода" "1" "$(skazano)"
case_check "метка выставлена — session-warden не повторит" \
    "да" "$([ -f "$STAND/zhurnal/лимит-подписки.состояние" ] && echo да || echo нет)"

echo "── тот же лимит на следующем обходе: молчим ──"
progon
case_check "повторного сообщения нет (один судья — общая метка)" "1" "$(skazano)"

echo "── БОЛЬНОЙ СЛУЧАЙ 13.09: журнал свеж, но отказ свежий — это НЕ возобновление ──"
# Улика владельца: «Не верно определяет что работа вознобновилась и шлёт мне
# эти сообщения пока она стоит… Зачем мне этот спам». Пока сессия стоит в
# лимите, каждая её попытка дописывает в журнал запись об ОТКАЗЕ и обновляет
# время файла — свежесть журнала сама по себе работой не является.
zhurnal 1500 60
progon
case_check "о возобновлении молчим, пока отказ свежий" "1" "$(skazano)"
case_check "метка НЕ снята — работа всё ещё стоит" \
    "да" "$([ -f "$STAND/zhurnal/лимит-подписки.состояние" ] && echo да || echo нет)"

echo "── смена ожила: о возобновлении говорим СРАЗУ ──"
# Работа пошла = свежих отказов в журнале больше нет. Старый отказ остаётся
# историей, а не признаком остановки.
zhurnal 30 3600
progon
case_check "о возобновлении сказано на первом же обходе" "2" "$(skazano)"
case_check "метка снята" \
    "нет" "$([ -f "$STAND/zhurnal/лимит-подписки.состояние" ] && echo да || echo нет)"
# БОЛЬНОЙ СЛУЧАЙ 27.09.2026 (снимок владельца): прибор объявил «Упёрся в лимит
# подписки», а через 12 минут — «Признака лимита не было». Повод терялся,
# потому что этот демон писал в метку одно поле, а читали оба демона два.
# Проба считала СООБЩЕНИЯ и текста не читала, поэтому дефект жил.
case_check "весть о возобновлении называет ЛИМИТ, а не «признака не было»" \
    "да" "$(grep -q 'Лимит обновился' "$STAND/zhurnal/kanal.txt" && echo да || echo нет)"
case_check "и не опровергает сама себя" \
    "нет" "$(grep -q 'Признака лимита не было' "$STAND/zhurnal/kanal.txt" && echo да || echo нет)"

echo "── БОЛЬНОЙ СЛУЧАЙ: старый отказ при молчащей смене — тревоги нет ──"
zhurnal 240 3600
progon
case_check "прошедший лимит владельца не будит" "2" "$(skazano)"

echo "── отказ свежий, но смена ПИШЕТ: помощник упал, работа идёт ──"
zhurnal 30 60
progon
case_check "живая смена с упавшим помощником тревоги не даёт" "2" "$(skazano)"

echo "── отказов нет вовсе ──"
zhurnal 240 нет
progon
case_check "чистый журнал — молчание" "2" "$(skazano)"

echo "── БОЛЬНОЙ СЛУЧАЙ 26.09 (ревью 27.09, F-01): файл пишется, работа стоит ──"
# Сторож сессий в 21:20 пишет «тишина 25 мин — пробую разбудить смену», и его
# пинок дописывает строку в журнал. Прежний прибор смотрел на время ФАЙЛА и
# в 21:21 объявлял владельцу «работа пошла снова, простой 0 мин». Таких
# «простой 0 мин» в боевом журнале пять.
zhurnal 1500 60          # успешного ответа нет 25 минут, отказ свежий
progon
case_check "о новом лимите сказано" "3" "$(skazano)"
zhurnal 1500 400         # отказ уже не свежий, строки в журнал дописаны
progon
case_check "дописанные строки возобновлением не считаются" "3" "$(skazano)"
case_check "метка на месте: успешного ответа не было 25 минут" \
    "да" "$([ -f "$STAND/zhurnal/лимит-подписки.состояние" ] && echo да || echo нет)"

echo "── БОЛЬНОЙ СЛУЧАЙ 14.10: лимит пришёл, когда стояла тихая метка «blocked» ──"
# Метку «blocked» ставит сторож сессий, пока работы нет и смена у приглашения.
# Пришёл лимит — о нём обязаны сказать: тихая метка не занимает места громкой.
rm -f "$STAND/zhurnal/лимит-подписки.состояние"
printf '%s blocked\n' "$(( $(date +%s) - 900 ))" > "$STAND/zhurnal/лимит-подписки.состояние"
zhurnal 1500 60
progon
case_check "лимит поверх «blocked» — владельцу сказано" "4" "$(skazano)"

echo "── лимит снят: повторный заход devmap-selfheal по флагу, и только по нему ──"
# Подставной демон карты лежит в проекте стенда: сторож зовёт его от
# PROJECT_DIR, и боевой демон с боевым журналом недосягаем. Каждый запуск —
# строка с LOG_DIR, которым его позвали.
mkdir -p "$STAND/proekt/harness/demons"
cat > "$STAND/proekt/harness/demons/devmap-selfheal.sh" <<STUB
#!/usr/bin/env bash
printf '%s\n' "\$LOG_DIR" >> "$STAND/devmap-zapuski.txt"
STUB
# Подставная обёртка: разбор доводов боевой run-background.sh, без её выключателя
# (конфига у стенда нет, а боевая при пустом конфиге гасит запуск). Id пишется в
# файл: заход мимо обёртки или под чужим именем — неудача.
cat > "$STAND/proekt/scripts/run-background.sh" <<STUB
#!/usr/bin/env bash
[ "\${2:-}" = "--" ] || exit 2
printf '%s\n' "\$1" >> "$STAND/obertka-zapuski.txt"
shift 2
exec "\$@"
STUB
chmod +x "$STAND/proekt/scripts/run-background.sh"
RETRY_FLAG="$STAND/zhurnal/devmap/retry-after-limit"
zapuski() { grep -c . "$STAND/devmap-zapuski.txt" 2>/dev/null || echo 0; }
wait_zapusk() {  # демон уходит в фон: ждём его строку до 10 с
    local i
    for i in $(seq 1 20); do [ "$(zapuski)" -ge "$1" ] && return 0; sleep 0.5; done
}
# Метка стоит с прошлого случая; флага нет — ветка возобновления пройдена, демон не зван.
rm -f "$RETRY_FLAG"
zhurnal 30 3600
progon
sleep 2
case_check "без флага: метка снята (ветка «лимит снят» пройдена)" \
    "нет" "$([ -f "$STAND/zhurnal/лимит-подписки.состояние" ] && echo да || echo нет)"
case_check "без флага: devmap-selfheal не запущен" "0" "$(zapuski)"
zhurnal 1500 60
progon
case_check "лимит снова доказан — метка стоит" \
    "да" "$([ -f "$STAND/zhurnal/лимит-подписки.состояние" ] && echo да || echo нет)"
mkdir -p "$STAND/zhurnal/devmap"; : > "$RETRY_FLAG"
zhurnal 30 3600
progon
wait_zapusk 1
case_check "с флагом: devmap-selfheal запущен ровно один раз" "1" "$(zapuski)"
case_check "с флагом: флаг повтора снят" "нет" "$([ -e "$RETRY_FLAG" ] && echo да || echo нет)"
case_check "демон позван с LOG_DIR стенда, а не боевым" "$STAND/zhurnal" "$(head -1 "$STAND/devmap-zapuski.txt" 2>/dev/null)"
case_check "демон пошёл через обёртку под своим id" "devmap-selfheal" "$(head -1 "$STAND/obertka-zapuski.txt" 2>/dev/null)"
progon
sleep 2
case_check "следующий обход второго захода не даёт" "1" "$(zapuski)"

printf '\nЛИМИТ-СТОРОЖ: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
