#!/usr/bin/env bash
# pamyat-kommit.sh — правки памяти, сделанные с панели, попадают в репозиторий.
#
# Что держит: память — часть кода, а не база. Владелец правит запись в браузере
# (11.09.2026: «Нужна возможность работать с памятью»), файл меняется сразу, но
# без коммита он живёт только на этой машине и исчезнет при первой же раскладке
# пакета. Решение, требующее «чтобы агент потом не забыл закоммитить», решением
# не является — поэтому коммит делает демон.
#
# Почему `git commit -o`: коммитятся ТОЛЬКО пути памяти, минуя индекс. Агент в
# этот момент может собирать свой коммит, и общий индекс увёл бы в него чужие
# файлы (грабля «параллельный стейджинг при идущем коммите»).
#
# Гейты pre-commit работают как обычно: правка памяти проходит те же ворота,
# что и любая другая. Не прошла — демон говорит об этом в лог и в канал, а не
# пытается обойти.
set -euo pipefail

CONF="${HARNESS_INSTALL_CONF:-/etc/harness/install.conf}"
# Значение конфига — до комментария: строка «PROJECT_DIR="…"   # каталог кода»
# без этой обрезки приезжала в cd целиком (поймано первым же прогоном).
znachenie() { grep -E "^$1=" "$CONF" 2>/dev/null | head -1 | cut -d= -f2- \
    | sed 's/[[:space:]]*#.*$//' | tr -d '"' | xargs || true; }
PROJECT_DIR="$(znachenie PROJECT_DIR)"
PROJECT_DIR="${PROJECT_DIR:?пуст PROJECT_DIR — не прочитан паспорт установки}"
LOG_DIR="$(znachenie LOG_DIR)"
LOG_DIR="${LOG_DIR:-/var/log/harness}"
HEARTBEAT_DIR="${HEARTBEAT_DIR:-/var/lib/harness/heartbeat}"
MEMORY_SUBDIR="память"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

cd "$PROJECT_DIR"

# Прополка указаний — ПЕРЕД сверкой изменений: она сама может их создать.
# Файл указаний упирался в потолок (7888 из 8000 знаков 13.09.2026), а
# держался ручной прополкой посреди другой работы — вниманием, а не
# механизмом. Прополка работает только при переполнении и ничего не теряет:
# длинная цитата уезжает в свою запись памяти, в указаниях остаётся начало и
# ссылка. Отказ прополки не мешает коммиту: память важнее её объёма.
if [ -x "$PROJECT_DIR/scripts/propolka-ukazanij.py" ]; then
    python3 "$PROJECT_DIR/scripts/propolka-ukazanij.py" 2>&1 | sed 's/^/    /' || \
        say "прополка указаний отказала — продолжаю коммит"
fi

# Что изменилось в памяти: и правки, и новые записи.
CHANGED="$(git status --porcelain -- "$MEMORY_SUBDIR" 2>/dev/null || true)"
if [ -z "$CHANGED" ]; then
    say "правок памяти нет"
    mkdir -p "$HEARTBEAT_DIR"
    : > "$HEARTBEAT_DIR/pamyat-kommit"
    exit 0
fi

COUNT="$(printf '%s\n' "$CHANGED" | grep -c . || true)"
say "правок памяти: $COUNT"
printf '%s\n' "$CHANGED" | sed 's/^/    /'

# Сообщение коммита называет ИМЕНА записей: по журналу панели потом видно, кто
# и когда правил, а по истории — что именно менялось.
NAMES="$(printf '%s\n' "$CHANGED" | awk '{print $NF}' | xargs -n1 basename 2>/dev/null | paste -sd', ' - || true)"
MSG="Правка памяти с панели: ${NAMES:-записи}

Записи изменены владельцем через веб-панель; демон pamyat-kommit переносит их
в репозиторий, потому что память — часть кода, а не база.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"

if git commit -o "$MEMORY_SUBDIR" -m "$MSG" >/tmp/pamyat-kommit.out 2>&1; then
    say "закоммичено: $(git log --oneline -1)"
else
    # «Коммитить нечего» и «гейты покраснели» — РАЗНЫЕ вещи, и владельцу их
    # нельзя называть одинаково. Живой случай 13.09.2026 в 00:20: демон увидел
    # временный файл пробы, взялся его коммитить, файл к тому моменту убрали —
    # и владелец получил тревогу «не прошли проверки», хотя все гейты прошли.
    # Его внимание — расходуемый ресурс (правило владельца 12.09), и тратить
    # его на исчезнувший файл нельзя.
    if grep -q "no changes added to commit\|nothing to commit\|nothing added to commit" \
            /tmp/pamyat-kommit.out; then
        say "коммитить нечего: правка исчезла между обходом и коммитом — владельцу не пишу"
        sed 's/^/    /' /tmp/pamyat-kommit.out | tail -5
        mkdir -p "$HEARTBEAT_DIR"
        : > "$HEARTBEAT_DIR/pamyat-kommit"
        exit 0
    fi
    say "коммит НЕ прошёл (гейты) — подробности ниже"
    sed 's/^/    /' /tmp/pamyat-kommit.out | tail -20
    # Причина в сообщении владельцу — та, что видна в выводе: имя красного
    # гейта. «Не прошли проверки» без имени заставляет его спрашивать меня.
    GATE_NAME=$(grep -m1 "GATE FAIL" /tmp/pamyat-kommit.out | sed 's/.*GATE FAIL: //')
    if [ -x "$PROJECT_DIR/scripts/tg_send.sh" ]; then
        bash "$PROJECT_DIR/scripts/tg_send.sh" \
            "Правку памяти с панели не удалось сохранить в хранилище кода: красный гейт${GATE_NAME:+ «$GATE_NAME»}. Файл на месте, разберусь." >/dev/null 2>&1 || true
    fi
    exit 1
fi

mkdir -p "$HEARTBEAT_DIR"
: > "$HEARTBEAT_DIR/pamyat-kommit"
