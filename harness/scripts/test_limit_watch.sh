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
# печатает в ошибку само значение (память: кириллица-в-именах-bash).
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHER="$HERE/../harness/demons/limit-watch.sh"
paths=0
fails=0

itog() {  # $1=ждём $2=вышло $3=имя
    paths=$((paths + 1))
    if [ "$1" = "$2" ]; then printf '  ок    %s\n' "$3"
    else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$3" "$1" "$2"; fails=$((fails + 1)); fi
}

STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT

# Подставное дерево: проект с фальшивым каналом и прибором из боевого кода.
# Каталог транскриптов назван ТАК ЖЕ, как его вычисляет сторож из пути проекта
# (путь с «/» заменёнными на «-»), иначе проба молча меряет пустоту: первая
# редакция пробы дала 7 «неудач» там, где сторож просто не находил транскрипт.
STAND_PROJECT="$STAND/proekt"
TR_DIR_NAME="${STAND_PROJECT//\//-}"
mkdir -p "$STAND/proekt/scripts/lib" "$STAND/zhurnal" "$STAND/transkripty/$TR_DIR_NAME"
cp "$HERE/limit-v-zhurnale.py" "$STAND/proekt/scripts/"
cp "$HERE/lib/konf.sh" "$STAND/proekt/scripts/lib/"
cat > "$STAND/proekt/scripts/tg_send.sh" <<'KANAL'
#!/usr/bin/env bash
printf '%s\n' "$1" >> "$LOG_DIR/kanal.txt"
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

otkaz_nazad() {  # $1 = сколько секунд назад был отказ 429
    python3 - "$TRANSCRIPT" "$1" <<'PY'
import datetime, json, sys, time
момент = datetime.datetime.fromtimestamp(time.time() - int(sys.argv[2]), datetime.UTC)
with open(sys.argv[1], "w", encoding="utf-8") as файл:
    файл.write(json.dumps({"error": "rate_limit",
                           "timestamp": момент.isoformat()}) + "\n")
PY
}

echo "── БОЛЬНОЙ СЛУЧАЙ: свежий отказ 429, смена молчит ──"
otkaz_nazad 60
touch -d "4 minutes ago" "$TRANSCRIPT"
progon
itog "1" "$(skazano)" "о лимите сказано с ПЕРВОГО обхода"
itog "да" "$([ -f "$STAND/zhurnal/лимит-подписки.состояние" ] && echo да || echo нет)" \
     "метка выставлена — session-warden не повторит"

echo "── тот же лимит на следующем обходе: молчим ──"
progon
itog "1" "$(skazano)" "повторного сообщения нет (один судья — общая метка)"

echo "── БОЛЬНОЙ СЛУЧАЙ 13.09: журнал свеж, но отказ свежий — это НЕ возобновление ──"
# Улика владельца: «Не верно определяет что работа вознобновилась и шлёт мне
# эти сообщения пока она стоит… Зачем мне этот спам». Пока сессия стоит в
# лимите, каждая её попытка дописывает в журнал запись об ОТКАЗЕ и обновляет
# время файла — свежесть журнала сама по себе работой не является.
otkaz_nazad 60
touch "$TRANSCRIPT"
progon
itog "1" "$(skazano)" "о возобновлении молчим, пока отказ свежий"
itog "да" "$([ -f "$STAND/zhurnal/лимит-подписки.состояние" ] && echo да || echo нет)" \
     "метка НЕ снята — работа всё ещё стоит"

echo "── смена ожила: о возобновлении говорим СРАЗУ ──"
# Работа пошла = свежих отказов в журнале больше нет. Старый отказ остаётся
# историей, а не признаком остановки.
otkaz_nazad 3600
touch "$TRANSCRIPT"
progon
itog "2" "$(skazano)" "о возобновлении сказано на первом же обходе"
itog "нет" "$([ -f "$STAND/zhurnal/лимит-подписки.состояние" ] && echo да || echo нет)" \
     "метка снята"

echo "── БОЛЬНОЙ СЛУЧАЙ: старый отказ при молчащей смене — тревоги нет ──"
otkaz_nazad 3600
touch -d "4 minutes ago" "$TRANSCRIPT"
progon
itog "2" "$(skazano)" "прошедший лимит владельца не будит"

echo "── отказ свежий, но смена ПИШЕТ: помощник упал, работа идёт ──"
otkaz_nazad 60
touch "$TRANSCRIPT"
progon
itog "2" "$(skazano)" "живая смена с упавшим помощником тревоги не даёт"

echo "── отказов нет вовсе ──"
printf '%s\n' '{"type":"assistant","timestamp":"2026-09-13T00:00:00Z"}' > "$TRANSCRIPT"
touch -d "4 minutes ago" "$TRANSCRIPT"
progon
itog "2" "$(skazano)" "чистый журнал — молчание"

printf '\nЛИМИТ-СТОРОЖ: путей %d, неудач %d\n' "$paths" "$fails"
[ "$fails" -eq 0 ]
