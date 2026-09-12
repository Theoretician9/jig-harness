#!/usr/bin/env bash
#
# memory-revision.sh — ежемесячная ревизия памяти, правил и скилов.
#
# Что держит: память проекта не превращается в свалку, правила — в декларации,
# скилы — в мёртвый груз. Запись без даты и без срабатываний, правило без
# команды проверки, скил без вызовов — всё это кандидаты на удаление или
# доработку; решает владелец, но СПИСОК С ГОТОВЫМИ ПРЕДЛОЖЕНИЯМИ обязан
# приходить к нему сам (правило «несистемное решение требует, чтобы кто-то
# помнил» — CLAUDE.md §18; процедура «ноль вызовов — диагноз» — UNIFIED/13).
#
# Откуда взят: собран для стартового пакета; демон-ревизии — точка крепления
# модуля эволюции (00-ВВОДНЫЕ §3).
#
# Чем доказывается: анализ — ГРУБАЯ ЭВРИСТИКА (grep, не разбор смысла);
# доказывается на живом при установке: подложить запись памяти без «когда» и
# правило «должен …» без backtick-команды → ближайшая ревизия обязана назвать
# оба и предложить готовое действие. Ложные срабатывания эвристики — цена,
# заложенная в жанр: это предложения владельцу, не автоматические правки.
#
# Запуск: cron, раз в месяц (1-е число, 09:00), от имени $AGENT_USER.
set -euo pipefail

# Резка/счёт по СИМВОЛАМ, не байтам (та же грабля, что в tg_send.sh):
# в C-локали крона ${#строка} и ${строка:0:N} считают байты и рвут кириллицу.
export LC_ALL=C.UTF-8

INSTALL_CONF="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-/etc/harness/install.conf}}"
HARNESS_CONF="${HARNESS_CONF:-/etc/harness/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "memory-revision: нет $INSTALL_CONF — установка не завершена (01-SPEC §0)"; exit 1; }
[ -r "$HARNESS_CONF" ] || { echo "memory-revision: нет $HARNESS_CONF — скопируйте harness/config/harness.conf в /etc/harness/"; exit 1; }
# Окружение старше конфига: общий загрузчик вместо голого source (улика
# 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/konf.sh"
konf_zagruzit
: "${PROJECT_DIR:?пуст PROJECT_DIR}" "${LOG_DIR:?пуст LOG_DIR}" "${HEARTBEAT_DIR:?пуст HEARTBEAT_DIR}"

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# ── Когда ревизия нужна ВНЕ календаря ───────────────────────────────────────
# Улика 11.09.2026: УКАЗАНИЯ.md 7985/8000 и MEMORY.md 24931/25000 уперлись в
# потолок за две недели, а ревизия ходит раз в месяц. Владелец: «Зачем это мне
# решать? Это должно работать автоматически <…> и тоже определяться кодом».
# Порог — данные (MEMORY_REVISION_TRIGGER); заполнение считает гейт гигиены,
# второго счёта не заводим.
MEMORY_REVISION_TRIGGER="${MEMORY_REVISION_TRIGGER:-0.9}"
HYGIENE="$PROJECT_DIR/scripts/check-context-hygiene.py"

nuzhna_revizia() {   # $1=доля заполнения  $2=порог → 0 нужна, 1 нет
    awk -v d="$1" -v t="$2" 'BEGIN { exit !(d >= t) }'
}

max_dolya() {        # наибольшая доля заполнения постоянных файлов, или 0
    python3 "$HYGIENE" --доли 2>/dev/null | python3 -c "$MAX_SNIPPET" 2>/dev/null || echo 0
}

MAX_SNIPPET='import json,sys
try: доли = json.load(sys.stdin)
except Exception: print(0); sys.exit(0)
print(max((з["доля"] for з in доли.values()), default=0))'

if [ "${1:-}" = "--selftest" ]; then
    ok=1
    nuzhna_revizia 0.95 0.9 || { echo "  ПЛОХО 95 % — ревизия обязана быть нужна"; ok=0; }
    echo "  ок    95 % заполнения — ревизия нужна"
    if nuzhna_revizia 0.42 0.9; then echo "  ПЛОХО 42 % признано порогом"; ok=0; fi
    echo "  ок    БОЛЬНОЙ СЛУЧАЙ: 42 % заполнения — ревизия НЕ нужна"
    nuzhna_revizia 0.9 0.9 || { echo "  ПЛОХО ровно порог не засчитан"; ok=0; }
    echo "  ок    ровно порог считается достижением порога"
    d=$(max_dolya)
    case "$d" in ''|*[!0-9.]*) echo "  ПЛОХО заполнение не число: «$d»"; ok=0;;
                 *) echo "  ок    заполнение читается числом ($d)";; esac
    [ "$ok" = 1 ] && { echo "SELFTEST: зелёный (4 пути, среди них больной)"; exit 0; }
    echo "SELFTEST: КРАСНЫЙ"; exit 1
fi

QUIET=0
if [ "${1:-}" = "--по-порогу" ]; then
    DOLYA=$(max_dolya)
    if ! nuzhna_revizia "$DOLYA" "$MEMORY_REVISION_TRIGGER"; then
        say "заполнение $DOLYA ниже порога $MEMORY_REVISION_TRIGGER — ревизия не нужна"
        exit 0
    fi
    say "заполнение $DOLYA достигло порога $MEMORY_REVISION_TRIGGER — ревизия вне календаря"
    # Владельцу такой список не нужен: он про мою уборку, а не про продукт
    # («не надо вываливать кучу своих рассуждений», 11.09.2026). Принуждение
    # остаётся: находки уходят в предложения, а просроченные красят ворота.
    QUIET=1
fi

# Один прогон за раз (Д-10, единый паттерн демонов). Лок — сервисный каталог.
LOCK_FILE="$LOG_DIR/locks/${SERVICE_LOCKS_SUBDIR:-services}/memory-revision.lock"
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>>"$LOCK_FILE"
flock -n 9 || { say "прогон уже идёт (лок $LOCK_FILE) — выхожу"; exit 0; }

MEMORY_DIR="$PROJECT_DIR/${MEMORY_DIR_NAME:-память}"
MEMORY_INDEX="$PROJECT_DIR/${MEMORY_INDEX_NAME:-MEMORY.md}"
CLAUDE_MD="$PROJECT_DIR/CLAUDE.md"
SKILLS_DIR="$PROJECT_DIR/${SKILLS_DIR_NAME:-harness/skills}"
SKILLS_LOG="$LOG_DIR/${SKILLS_LOG_NAME:-skills.jsonl}"

FINDINGS=""
add() { FINDINGS="${FINDINGS}
• $1"; }

# ── 0. Файлы, упёршиеся в потолок ───────────────────────────────────────────
# Прополка — главная работа ревизии, а не побочная: файл на 99 % потолка
# останавливает ближайший коммит. Строки предложений собирает тот, кто считает
# заполнение (гейт гигиены): вложенный питон внутри демона ломался на кавычках.
while IFS= read -r stroka; do
    [ -n "$stroka" ] && add "$stroka"
done < <(python3 "$HYGIENE" --переполнены "$MEMORY_REVISION_TRIGGER" 2>/dev/null || true)

# ── 1. Записи памяти без даты и без следа срабатывания ──────────────────────
# Запись без «когда» невозможно ревизовать: неясно, месяц ей или год.
# Запись, ни разу не помянутая в индексе, — балласт, который едет в голову
# агента без отдачи.
if [ -d "$MEMORY_DIR" ]; then
    INDEX_BASE=$(basename "$MEMORY_INDEX")
    while IFS= read -r f; do
        base=$(basename "$f")
        # Пары регистров вместо -i — та же кириллическая грабля C-локали.
        if ! grep -qE '(К|к)огда[:*]|(З|з)аписано[: ]|(Д|д)ата[: ]' "$f"; then
            add "память/$base: нет поля «когда» → ПРЕДЛОЖЕНИЕ: дописать дату записи, иначе удалить при следующей ревизии — без даты запись нельзя состарить"
        fi
        # Ищем по имени БЕЗ .md: индекс ссылается вики-стилем [[имя]] —
        # поиск точного «имя.md» флагал бы каждую запись как безындексную.
        # -F (Д-5): имя файла — литерал, не регэксп; точка в имени совпадала
        # бы с любым символом и прятала безындексную запись за похожей.
        if [ -f "$MEMORY_INDEX" ] && ! grep -qF "${base%.md}" "$MEMORY_INDEX"; then
            add "память/$base: нет строки в индексе $( basename "$MEMORY_INDEX") → ПРЕДЛОЖЕНИЕ: внести строку в индекс или удалить файл — память вне индекса не находится и не срабатывает"
        fi
    # Индекс (MEMORY.md) — оглавление, не запись: поля «когда» у него нет
    # по жанру, обходить его как запись — ложный флаг каждый месяц.
    done < <(find "$MEMORY_DIR" -maxdepth 1 -name '*.md' ! -name "$INDEX_BASE" -type f 2>/dev/null)
else
    say "каталога памяти $MEMORY_DIR нет — памяти ещё не накоплено (нормально в первый месяц)"
fi

# ── 2. Правила CLAUDE.md без команды проверки ───────────────────────────────
# ЭВРИСТИКА: строка с «должен/нельзя/обязан/запрещено» без backtick-команды
# в самой строке и в двух соседних — правило держится вниманием, а не
# механикой (CLAUDE.md §18: признак несистемного решения).
if [ -f "$CLAUDE_MD" ]; then
    while IFS=: read -r ln _; do
        ctx=$(sed -n "$((ln > 2 ? ln - 2 : 1)),$((ln + 2))p" "$CLAUDE_MD")
        if ! printf '%s' "$ctx" | grep -q '`'; then
            rule=$(sed -n "${ln}p" "$CLAUDE_MD" | cut -c1-100)
            add "CLAUDE.md:$ln «${rule}…» — правило без команды проверки рядом → ПРЕДЛОЖЕНИЕ: дописать проверяющую команду/гейт, либо явно пометить строку как справку (не правило)"
        fi
    # Явные пары регистров вместо grep -i: в C-локали крона -i не работает
    # для кириллицы, и ревизия молча слепла бы на половину правил.
    done < <(grep -nE '(Д|д)олжен|(Н|н)ельзя|(О|о)бязан|(З|з)апрещено' "$CLAUDE_MD" | cut -d: -f1 | sed 's/$/:/')
else
    say "CLAUDE.md в $PROJECT_DIR нет — правил ещё нет"
fi

# ── 3. Молчащие скилы ───────────────────────────────────────────────────────
# «Ноль вызовов — диагноз» (UNIFIED/13): скил, не помянутый в журнале вызовов,
# либо не нужен, либо не встроен в процесс. Оба случая — к владельцу.
if [ -d "$SKILLS_DIR" ]; then
    while IFS= read -r sk; do
        sk_name=$(basename "$sk")
        # -F (Д-5): имя скила — литерал, не регэксп (та же грабля, что у памяти).
        if [ ! -f "$SKILLS_LOG" ] || ! grep -qF "$sk_name" "$SKILLS_LOG"; then
            add "скил $sk_name: ни одного вызова в журнале → ПРЕДЛОЖЕНИЕ: удалить, либо встроить вызов в процесс (хук/гейт), либо оставить с записанной причиной в реестре механизмов"
        fi
    done < <(find "$SKILLS_DIR" -mindepth 1 -maxdepth 1 \( -type d -o -name '*.md' \) 2>/dev/null)
    [ -f "$SKILLS_LOG" ] || say "журнала вызовов $SKILLS_LOG нет — все скилы формально молчат (журнал обязан существовать с первого дня, 01-SPEC §10 п.7)"
else
    say "каталога скилов $SKILLS_DIR нет — скилов ещё нет"
fi

# ── сводка владельцу ────────────────────────────────────────────────────────
if [ -n "$FINDINGS" ]; then
    N=$(printf '%s\n' "$FINDINGS" | grep -c '^•' || true)
    SUMMARY="Ежемесячная ревизия памяти/правил/скилов: позиций к решению — $N.$FINDINGS

По каждой — готовое предложение; решение за вами. Анализ грубый (grep-эвристика), ложные срабатывания возможны."
else
    SUMMARY="Ежемесячная ревизия памяти/правил/скилов: замечаний нет — записи датированы, правила с проверками, скилы зовутся."
fi
say "$SUMMARY"

# Записываем ПОСЛЕ доставки и best-effort: демон под set -e, и сбой записи
# (нет прав, кончилось место) валил его до отправки — механизм принуждения
# убивал механизм оповещения, а владелец не получал ревизию вовсе
# (ревью 10.09.2026).
записать_предложения() {
    # Предложения обязаны иметь НОСИТЕЛЬ, иначе демон — половина механизма:
    # 01.09.2026 ревизия прислала владельцу четыре части предложений, и через
    # девять дней не было выполнено ни одного (замечание владельца П-5).
    # Открытые старше срока красят ворота (scripts/check-predlozheniya.py).
    #
    # Ключ дубля — текст БЕЗ ЧИСЕЛ: в находке стоит номер строки («CLAUDE.md:25»),
    # и любая правка выше по файлу возвращала бы уже разобранное предложение
    # новым (ревью 10.09.2026). Закрытые тоже считаются: разобранное ложное
    # срабатывание не поднимается заново.
    OFFERS="$LOG_DIR/предложения.jsonl"
    [ -n "$FINDINGS" ] || return 0
    printf '%s\n' "$FINDINGS" | grep '^•' | while IFS= read -r line; do
        text=${line#• }
        python3 - "$OFFERS" "$text" <<'PYEOF' || true
import json, sys, datetime, hashlib, os, re
путь, текст = sys.argv[1], sys.argv[2]
ключ = hashlib.sha1(re.sub(r"\d+", "#", текст).encode()).hexdigest()[:8]
если_есть = False
if os.path.exists(путь):
    for строка in open(путь, encoding="utf-8"):
        try:
            если_есть = если_есть or json.loads(строка).get("id") == ключ
        except Exception:
            pass
if не_надо := если_есть:
    sys.exit(0)
with open(путь, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"id": ключ, "дата": datetime.date.today().isoformat(),
                         "текст": текст, "статус": "открыто"}, ensure_ascii=False) + "\n")
PYEOF
    done
}
записать_предложения || say "предложения в журнал не записались — сводка уже доставлена"


tg_deliver() {
    # Длинные ревизии — через tg_send.sh: он режет по СИМВОЛАМ и шлёт частями,
    # ничего не теряя. Прямой curl ниже — только запасной путь.
    if [ -x "$PROJECT_DIR/scripts/tg_send.sh" ]; then
        if "$PROJECT_DIR/scripts/tg_send.sh" "$SUMMARY"; then return 0; fi
        say "tg_send.sh отказал — пробую прямой curl"
    fi
    if [ -z "${TG_CHAT_ID:-}" ] || [ ! -s "${TG_TOKEN_FILE:-/nonexistent}" ]; then
        say "КАНАЛ НЕ НАСТРОЕН: TG_CHAT_ID/TG_TOKEN_FILE — ревизии некуда идти"
        return 1
    fi
    local token; token=$(cat "$TG_TOKEN_FILE")
    # Запасной путь не умеет частей — потолок Telegram 4096: режем по символам
    # (LC_ALL=C.UTF-8 выше; head -c резал БАЙТЫ и рвал кириллицу посередине),
    # с честной пометкой — полный текст остаётся в логе демона.
    local text="$SUMMARY"
    if [ "${#text}" -gt 3900 ]; then
        text="${text:0:3900}
…(обрезано, полный список — в $LOG_DIR/memory-revision.log)"
    fi
    # URL с токеном — через stdin (curl -K -), не в argv: argv виден в ps.
    printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" \
        | curl -s -m 20 -K - \
            -d chat_id="$TG_CHAT_ID" --data-urlencode "text=$text" \
        | jq -e '.ok == true' >/dev/null
}

if [ "$QUIET" = "1" ]; then
    say "запуск по порогу: владельцу не шлём, находки ушли в предложения"
    записать_предложения || say "предложения в журнал не записались"
    touch "$HEARTBEAT_DIR/memory-revision"
    exit 0
fi

if tg_deliver; then
    say "ревизия доставлена владельцу (ok:true)"
else
    say "ревизия НЕ доставлена — список выше остаётся только в логе"
    exit 1
fi

touch "$HEARTBEAT_DIR/memory-revision"
