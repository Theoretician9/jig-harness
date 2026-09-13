#!/usr/bin/env bash
# Пробы обёртки вызова модели демонами: отказ по лимиту обязан быть ВИДЕН.
#
# СЛУЧАЙ 13.09.2026. Три демона получили от claude «You've hit your weekly
# limit · resets Sep 16, 6pm» и отчитались обычной неудачей: devmap-selfheal
# «починка НЕ удалась (код 1) — попробую в следующий раз», ton-watch «агент
# тона вернул 1 — повторим завтра», stroitel-proverok «агент строителя вернул
# 1». Карта не чинилась двое суток, и никто не сказал ни слова: сторож
# limit-watch судит по отказам 429 в журнале СМЕНЫ, а headless-прогон демона в
# этот журнал не пишет вовсе.
#
# Проба создаёт своё условие: подставная команда `claude` в PATH печатает то,
# что печатает настоящая, и возвращает тот же код. Сети здесь нет.
#
#   bash scripts/test_claude_demon.sh
# Код возврата: 0 — все пути прошли, 1 — есть упавший.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OBERTKA="$HERE/claude-demon.sh"
PUTEJ=0
NEUDACH=0

itog() {  # itog "имя" "ждём" "вышло"
    PUTEJ=$((PUTEJ + 1))
    if [ "$2" = "$3" ]; then
        printf 'ок  · %s\n' "$1"
    else
        NEUDACH=$((NEUDACH + 1))
        printf 'ПЛОХО · %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$3"
    fi
}

STAND="$(mktemp -d)"
trap 'rm -rf "$STAND"' EXIT
mkdir -p "$STAND/bin" "$STAND/log" "$STAND/heartbeat"

# Подставной канал: вместо отправки владельцу — строка в файл. Так видно, что
# сказано и СКОЛЬКО раз.
cat > "$STAND/bin/tg_send.sh" <<'SEND'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TG_VYHOD"
SEND
chmod +x "$STAND/bin/tg_send.sh"

# Подставная модель: что печатает, задаётся файлом сценария.
cat > "$STAND/bin/claude" <<'CLAUDE'
#!/usr/bin/env bash
cat "$CLAUDE_SCENARIJ"
exit "${CLAUDE_KOD:-0}"
CLAUDE
chmod +x "$STAND/bin/claude"

export PATH="$STAND/bin:$PATH"
export LOG_DIR="$STAND/log" HEARTBEAT_DIR="$STAND/heartbeat"
export TG_SEND="$STAND/bin/tg_send.sh" TG_VYHOD="$STAND/сказано.txt"
: > "$TG_VYHOD"

echo "── БОЛЬНОЙ СЛУЧАЙ: недельный лимит ──"
printf "You've hit your weekly limit · resets Sep 16, 6pm (Asia/Qostanay)\n" > "$STAND/лимит.txt"
export CLAUDE_SCENARIJ="$STAND/лимит.txt" CLAUDE_KOD=1
VYVOD=$(bash "$OBERTKA" проба-демон -p "задание" 2>&1); KOD=$?
itog "отказ по лимиту — свой код 77, а не общий 1" 77 "$KOD"
itog "владельцу сказано один раз" 1 "$(grep -c . "$TG_VYHOD")"
itog "в сообщении названа дата сброса" "да" "$(grep -q 'Sep 16' "$TG_VYHOD" && echo да || echo нет)"
# Пояс — часть времени: без него «6pm» читается неверно. Живой прогон
# 13.09.2026 показал обрезку по скобке: «resets Sep 16, 6pm (Asia/Qostanay».
itog "пояс из скобок не потерян" "да" "$(grep -q 'Asia/Qostanay)' "$TG_VYHOD" && echo да || echo нет)"
itog "в сообщении назван демон" "да" "$(grep -q 'проба-демон' "$TG_VYHOD" && echo да || echo нет)"

echo "── тот же лимит второй раз: владельцу НЕ повторяем ──"
bash "$OBERTKA" проба-демон -p "задание" >/dev/null 2>&1
itog "второе сообщение не ушло" 1 "$(grep -c . "$TG_VYHOD")"
itog "второй демон тоже молчит" 1 "$(bash "$OBERTKA" другой-демон -p "z" >/dev/null 2>&1; grep -c . "$TG_VYHOD")"

echo "── здоровый вызов: вывод отдан как есть, кода 77 нет ──"
printf 'ответ модели\n' > "$STAND/ответ.txt"
export CLAUDE_SCENARIJ="$STAND/ответ.txt" CLAUDE_KOD=0
VYVOD=$(bash "$OBERTKA" проба-демон -p "задание" 2>/dev/null); KOD=$?
itog "здоровый вызов: код 0" 0 "$KOD"
itog "здоровый вызов: вывод модели отдан вызывающему" "ответ модели" "$VYVOD"
itog "о снятии лимита сказано" "да" "$(grep -q 'снят\|сброс\|работают' "$TG_VYHOD" && echo да || echo нет)"

echo "── обычная неудача моделью: НЕ выдаётся за лимит ──"
printf 'что-то пошло не так\n' > "$STAND/беда.txt"
export CLAUDE_SCENARIJ="$STAND/беда.txt" CLAUDE_KOD=1
: > "$TG_VYHOD"
VYVOD=$(bash "$OBERTKA" проба-демон -p "задание" 2>&1); KOD=$?
itog "обычный отказ отдаёт код модели" 1 "$KOD"
itog "об обычном отказе владельцу не пишем" 0 "$(grep -c . "$TG_VYHOD")"

echo "── запрет из CLAUDE.md держится обёрткой ──"
export CLAUDE_SCENARIJ="$STAND/ответ.txt" CLAUDE_KOD=0
SLED="$STAND/аргументы.txt"
cat > "$STAND/bin/claude" <<'CLAUDE2'
#!/usr/bin/env bash
printf '%s\n' "$*" > "$SLED_ARG"
cat "$CLAUDE_SCENARIJ"
exit "${CLAUDE_KOD:-0}"
CLAUDE2
chmod +x "$STAND/bin/claude"
SLED_ARG="$SLED" bash "$OBERTKA" проба-демон -p "задание" >/dev/null 2>&1
itog "--strict-mcp-config добавлен всегда (иначе обрывается канал владельца)" \
     "да" "$(grep -q -- '--strict-mcp-config' "$SLED" && echo да || echo нет)"

echo "── запись состояния идёт ПОД ЛОКОМ ──"
# Прямое доказательство вместо гонки «на удачу»: проба САМА держит тот же лок
# и смотрит, ждёт ли демон. Сценарий «три демона разом» оказался зелёным и без
# лока — разброс запуска (20 мс) больше окна между проверкой и записью, и
# такая проба доказывала очередь, а не замок.
export CLAUDE_SCENARIJ="$STAND/лимит.txt" CLAUDE_KOD=1
rm -f "$LOG_DIR/лимит-демонов.состояние"
: > "$TG_VYHOD"
mkdir -p "$LOG_DIR/locks"
exec 7>>"$LOG_DIR/locks/лимит-демонов.lock"
flock 7                                   # лок у пробы
bash "$OBERTKA" под-локом -p "задание" >/dev/null 2>&1 &
DEMON_PID=$!
sleep 1
itog "пока лок держит проба — демон не пишет владельцу" 0 "$(grep -c . "$TG_VYHOD")"
flock -u 7                                # отпускаем
wait "$DEMON_PID" || true
itog "лок отпущен — сообщение ушло" 1 "$(grep -c . "$TG_VYHOD")"
exec 7>&-

echo "── второй демон в том же периоде молчит ──"
itog "второй демон сообщения не добавил" 1 \
     "$(bash "$OBERTKA" второй-в-периоде -p "z" >/dev/null 2>&1; grep -c . "$TG_VYHOD")"

printf '\nитого путей %s, неудач %s\n' "$PUTEJ" "$NEUDACH"
[ "$NEUDACH" -eq 0 ]
