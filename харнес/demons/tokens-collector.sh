#!/usr/bin/env bash
#
# tokens-collector.sh — учёт расхода токенов по транскриптам Claude Code.
#
# Что держит: видимость расхода (Б-5 ревью 09.08.2026): владелец платит за
# токены, а до этого демона расход не мерил никто. Раз в час демон обходит
# JSONL-транскрипты сессий /home/$AGENT_USER/.claude/projects/*/*.jsonl
# ИНКРЕМЕНТАЛЬНО — курсор лежит JSON-ом в $LOG_DIR/tokens.cursor (запись
# атомарная: tmp + os.replace): {"files": {путь: {"pos": байты, "seen":
# [пары]}}, "recent": [пары]}; записи файлов, исчезнувших с диска, вычищаются
# при прогоне (П-17). Из новых строк берутся usage
# (input/output/cache_creation/cache_read) и model; ts события — из поля
# timestamp строки транскрипта (момент расхода), fallback — время прогона.
#
# Дедуп (П-3): стриминговый транскрипт пишет ОДИН ответ ассистента несколькими
# строками с одинаковым message.usage — без дедупа расход завышается кратно.
# Ключ — пара message.id + requestId; виденные пары живут в курсоре: у файлов,
# чей курсор не в конце, — пофайлово, у дочитанных — последние 500 пар в общем
# окне "recent" (множество не растёт вечно). Дубль — пропуск со счётчиком
# «дублей пропущено: K». Строка без message.id не дедупится — ключа нет.
#
# События уходят в ПОМЕСЯЧНЫЙ файл $LOG_DIR/tokens-YYYY-MM.jsonl (П-4:
# один вечный tokens.jsonl рос без предела и сканировался целиком ежечасно);
# символлинк $LOG_DIR/tokens.jsonl указывает на файл текущего месяца, прошлые
# месяцы лежат рядом в $LOG_DIR как tokens-YYYY-MM.jsonl. Формат события:
#   {"ts","session","model","in","out","cache_w","cache_r"}
# после прохода пересчитывается суточный итог ТОЛЬКО по файлу текущего
# месяца — ОДНА строка в $LOG_DIR/tokens-daily.summary, её включает в
# утреннюю сводку heartbeat-watch (Д-4в) как есть. Рядом кладётся подробный
# отчёт $LOG_DIR/tokens-daily.report (13.08.2026, запрос владельца): среднее
# на вопрос «почему кэш такой большой» не отвечает — одинаковое среднее дают
# и ровные шаги, и десяток гигантских среди мелочи. В отчёте распределение
# контекста (медиана, 90-й и 95-й процентили, максимум), доля попаданий кэша
# и сверка баланса «контекст × шаги ≈ фактически прошло»: разойдись она —
# врёт счётчик шагов или суточная выборка, и остальным числам верить нельзя.
#
# Поле model — с первого дня: заготовка мультимодельности. Когда моделей
# станет больше одной, разрез «по моделям» уже будет в данных, а не начнётся
# с нуля задним числом.
#
# Битая строка транскрипта пропускается со счётчиком, не валит прогон:
# транскрипт пишет чужой процесс, его формат — не наш контракт. Строка без
# usage (ход пользователя и т.п.) — не битая, просто не про токены.
#
# Чем доказывается: `bash tokens-collector.sh --selftest` — фейковые
# транскрипты: 3 строки с usage + 1 битая дают 3 события; стриминговый файл
# (3 строки с одинаковыми message.id+requestId) даёт ОДНО событие и «дублей
# пропущено: 2»; итог верен; повторный прогон не дублирует (курсор);
# исчезнувший файл вычищается из курсора.
#
# Запуск: cron, раз в час, от имени $AGENT_USER (01-SPEC §9).
set -euo pipefail

say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# ── самотест: фейковый транскрипт во временном каталоге ─────────────────────
if [ "${1:-}" = "--selftest" ]; then
    T=$(mktemp -d)
    trap 'rm -rf "$T"' EXIT
    mkdir -p "$T/hb" "$T/log" "$T/tr/-home-agent-proj"
    cat > "$T/install.conf" <<EOF
AGENT_USER="no-such-user-selftest"
HEARTBEAT_DIR="$T/hb"
LOG_DIR="$T/log"
EOF
    : > "$T/harness.conf"
    # Даты набора и обоих прогонов — ОДНА И ТА ЖЕ явная дата, а не «сегодня»
    # запускающего: иначе исход пробы зависит от часа суток (пункт 13).
    # Берём дату по UTC: моменты в наборе записаны с суффиксом «Z».
    # Дата ФИКСИРОВАНА, а не «сегодня». Улика 12.09.2026, 01:51 местного:
    # дата бралась по UTC (11.09), события строились с ней, а сбор считал сутки
    # по часам владельца (12.09) — и в итог попадало одно событие из четырёх.
    # Самотест краснел каждую ночь с 19:00 UTC до полуночи и был зелёным днём:
    # дефект ТЕСТА, выглядевший как дефект прибора. Тест, зависящий от времени
    # своего запуска, проверяет часы, а не прибор.
    TODAY="2026-06-15"
    export TOKENS_TODAY="$TODAY"
    # Гейты за сутки: два прогона, один красный. Третья запись — вчерашняя:
    # без отсечки по дате отчёт считал бы всю историю журнала суточной.
    printf '%s\n' \
        "{\"ts\":\"${TODAY}T09:00:00+00:00\",\"gate\":\"pre-commit\",\"verdict\":\"pass\"}" \
        "{\"ts\":\"${TODAY}T09:30:00+00:00\",\"gate\":\"pre-commit\",\"verdict\":\"fail\"}" \
        "{\"ts\":\"2000-01-01T00:00:00+00:00\",\"gate\":\"pre-commit\",\"verdict\":\"fail\"}" \
        > "$T/log/gates.jsonl"
    # Месяц — из фиксированной даты событий, а не из «сейчас»: иначе прибор
    # пишет в файл месяца события, а тест ищет файл текущего месяца.
    MONTH="${TODAY%-*}"
    EVENTS="$T/log/tokens-$MONTH.jsonl"
    # 3 строки с usage (две модели), 1 строка без usage, 1 битая.
    # У КАЖДОГО события свой timestamp. Без него прибор берёт дату из «сейчас»,
    # и набор оказывался привязан ко дню запуска: с фиксированной датой события
    # без метки уезжали в чужие сутки, а с «сегодня» ломался зеркальный прогон
    # в UTC. Проверка, исход которой зависит от часа запуска, не проверка.
    cat > "$T/tr/-home-agent-proj/sess-0001.jsonl" <<EOF
{"type":"assistant","timestamp":"${TODAY}T09:00:00.000Z","sessionId":"sess-0001","message":{"model":"claude-opus-4","usage":{"input_tokens":100,"output_tokens":10,"cache_creation_input_tokens":5,"cache_read_input_tokens":50}}}
{"type":"user","sessionId":"sess-0001","message":{"role":"user","content":"строка без usage — не событие и не битая"}}
{"type":"assistant","timestamp":"${TODAY}T09:01:00.000Z","sessionId":"sess-0001","message":{"model":"claude-opus-4","usage":{"input_tokens":200,"output_tokens":20,"cache_read_input_tokens":100}}}
это не JSON — битая строка, демон обязан пропустить её со счётчиком
{"type":"assistant","timestamp":"${TODAY}T09:02:00.000Z","sessionId":"sess-0001","message":{"model":"claude-sonnet-4","usage":{"input_tokens":50,"output_tokens":5}}}
EOF
    # П-3: стриминг — ТРИ строки одного ответа с одинаковыми message.id +
    # requestId и одинаковым usage; событие обязано получиться ОДНО.
    cat > "$T/tr/-home-agent-proj/sess-0002.jsonl" <<EOF
{"type":"assistant","timestamp":"${TODAY}T10:00:00.000Z","requestId":"req_001","sessionId":"sess-0002","message":{"id":"msg_001","model":"claude-opus-4","usage":{"input_tokens":7,"output_tokens":3}}}
{"type":"assistant","timestamp":"${TODAY}T10:00:01.000Z","requestId":"req_001","sessionId":"sess-0002","message":{"id":"msg_001","model":"claude-opus-4","usage":{"input_tokens":7,"output_tokens":3}}}
{"type":"assistant","timestamp":"${TODAY}T10:00:02.000Z","requestId":"req_001","sessionId":"sess-0002","message":{"id":"msg_001","model":"claude-opus-4","usage":{"input_tokens":7,"output_tokens":3}}}
EOF
    # Событие в 20:00 UTC: для владельца в UTC+5 это уже 01:00 СЛЕДУЮЩЕГО дня.
    # Пока сервер стоял в UTC, сравнение UTC-строки с локальной датой совпадало
    # случайно; после перевода сервера в пояс владельца (12.08.2026) такие
    # события уезжали в чужие сутки. Ниже итог считается дважды — в UTC+5 и в
    # UTC — на ОДНИХ И ТЕХ ЖЕ данных.
    cat > "$T/tr/-home-agent-proj/sess-0003.jsonl" <<EOF
{"type":"assistant","timestamp":"${TODAY}T20:00:00.000Z","requestId":"req_009","sessionId":"sess-0003","message":{"id":"msg_009","model":"claude-opus-4","usage":{"input_tokens":1000,"output_tokens":0,"cache_creation_input_tokens":11}}}
EOF
    # Прибор шага: два ответа с инструментами — один дробит (1 вызов), второй
    # собирает (3 вызова). БОЛЬНОЙ СЛУЧАЙ в знаменателе: ответы БЕЗ инструментов
    # (их выше четыре) в долю попасть не должны — иначе она мерила бы
    # разговорчивость, а не дробление. Расход нулевой, чтобы суточные суммы
    # выше остались прежними: прибор проверяется отдельно от арифметики.
    cat > "$T/tr/-home-agent-proj/sess-0004.jsonl" <<EOF
{"type":"assistant","timestamp":"${TODAY}T10:30:00.000Z","requestId":"req_010","sessionId":"sess-0004","message":{"id":"msg_010","model":"claude-opus-4","content":[{"type":"tool_use","name":"Bash"}],"usage":{"input_tokens":0,"output_tokens":0}}}
{"type":"assistant","timestamp":"${TODAY}T10:31:00.000Z","requestId":"req_011","sessionId":"sess-0004","message":{"id":"msg_011","model":"claude-opus-4","content":[{"type":"text","text":"собираю три проверки в один шаг"},{"type":"tool_use","name":"Bash"},{"type":"tool_use","name":"Read"},{"type":"tool_use","name":"Grep"}],"usage":{"input_tokens":0,"output_tokens":0}}}
EOF
    # bash "$0", не "$0": после scp с Windows exec-бита может не быть (П-5).
    OUT=$(TZ=Etc/GMT-5 TOKENS_TODAY="$TODAY" INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
          TOKENS_TRANSCRIPTS_DIR="$T/tr" bash "$0") \
        || { echo "САМОТЕСТ ПРОВАЛЕН: первый прогон упал"; echo "$OUT"; exit 1; }
    N=$(wc -l < "$EVENTS" | tr -d ' ')
    [ "$N" = 7 ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: событий в tokens-$MONTH.jsonl $N, ждали 7 (3 + 1 из стриминга + 1 ночное + 2 с инструментами)"; exit 1; }
    [ -L "$T/log/tokens.jsonl" ] && [ "$(readlink "$T/log/tokens.jsonl")" = "tokens-$MONTH.jsonl" ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: tokens.jsonl не символлинк на файл текущего месяца (П-4)"; exit 1; }
    echo "$OUT" | grep -q 'битых строк пропущено: 1' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: битая строка не посчитана"; echo "$OUT"; exit 1; }
    echo "$OUT" | grep -q 'дублей пропущено: 2' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: стриминговые дубли не пропущены со счётчиком (П-3)"; echo "$OUT"; exit 1; }
    # П-17: ts события — из поля timestamp строки транскрипта
    grep -q "${TODAY}T10:00:00" "$EVENTS" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: ts события не взят из timestamp транскрипта (П-17)"; exit 1; }
    # итог: вх 100+200+50+7, вых 10+20+5+3, кэш-чтение 50+100
    # БОЛЬНОЙ СЛУЧАЙ: в поясе владельца (UTC+5) событие 20:00Z принадлежит
    # СЛЕДУЮЩИМ суткам — его 1000 входных в сегодняшний итог попасть не должны.
    grep -q 'токены за сутки: вх 357 · вых 38 · кэш-запись 5 · кэш-чтение 150' "$T/log/tokens-daily.summary" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: суточный итог в поясе UTC+5 неверен:"; cat "$T/log/tokens-daily.summary"; exit 1; }
    grep -q 'claude-sonnet-4' "$T/log/tokens-daily.summary" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: разреза по моделям нет в итоге"; exit 1; }
    [ "$(wc -l < "$T/log/tokens-daily.summary" | tr -d ' ')" = 2 ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: итог не двумя строками (расход + прибор шага)"; exit 1; }
    # Прибор шага. Знаменатель — только шаги С инструментами (их 2 из шести
    # сегодняшних), доля одиночных — 1 из 2. Возьми знаменателем все события,
    # и та же картина показала бы 16% — беда выглядела бы вшестеро меньше.
    grep -q 'с одним вызовом 50% (из 2)' "$T/log/tokens-daily.summary" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: доля шагов с одним вызовом неверна:"; cat "$T/log/tokens-daily.summary"; exit 1; }
    grep -q 'шагов 6 · контекст на шаг 85' "$T/log/tokens-daily.summary" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: шаги и контекст на шаг неверны:"; cat "$T/log/tokens-daily.summary"; exit 1; }
    # Подробный отчёт. БОЛЬНОЙ СЛУЧАЙ уже в данных: шаги нарочно неровные
    # (0, 0, 7, 50, 155, 300), и среднее 85 говорит о них неправду в обе
    # стороны. Отчёт обязан показать и медиану 7, и максимум 300 — иначе он
    # не прибор, а то же среднее в новой рамке.
    REPORT="$T/log/tokens-daily.report"
    [ -f "$REPORT" ] || { echo "САМОТЕСТ ПРОВАЛЕН: tokens-daily.report не создан"; exit 1; }
    grep -q 'медиана                   7$' "$REPORT" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: медиана в отчёте неверна:"; cat "$REPORT"; exit 1; }
    grep -q 'максимум                  300$' "$REPORT" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: максимум в отчёте неверен:"; cat "$REPORT"; exit 1; }
    # 90-й процентиль ловит метод счёта: ближайший ранг даёт 300, а срез с
    # округлением вниз — 155. Без этой строки подмена метода прошла бы молча
    # (проверено: медиана на этих данных у обоих методов одна и та же).
    grep -q '90-й процентиль           300$' "$REPORT" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: 90-й процентиль в отчёте неверен:"; cat "$REPORT"; exit 1; }
    # Доля попаданий кэша: 150 прочитано из 512 всего контекста. Здесь она
    # низкая — на живом сервере она около 99%, и разница между этими двумя
    # картинами и есть ответ на вопрос «почему кэш такой большой».
    grep -q 'попаданий                 29,3%' "$REPORT" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: доля попаданий кэша неверна:"; cat "$REPORT"; exit 1; }
    # Сверка баланса: 85 × 6 = 510 при фактических 512 (потеря на целочисленном
    # среднем). Разойдись эти числа сильно — врёт счётчик шагов или выборка.
    grep -q 'Сверка: 85 × 6 шагов = 510; фактически прошло 512' "$REPORT" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: строка сверки неверна:"; cat "$REPORT"; exit 1; }
    grep -q 'прогонов гейтов           2, красных 1' "$REPORT" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: гейты за сутки посчитаны неверно (вчерашняя запись не отсечена?):"; cat "$REPORT"; exit 1; }
    # PROJECT_DIR в конфиге самотеста нет — репозитория не существует, и отдача
    # обязана честно показать прочерк. Ноль здесь читался бы как «за сутки не
    # сделано ни одного коммита», а это разные утверждения.
    grep -q 'коммитов за сутки         —' "$REPORT" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: без репозитория коммиты обязаны быть прочерком:"; cat "$REPORT"; exit 1; }
    # ЗЕРКАЛЬНО: те же данные в поясе UTC — событие 20:00Z принадлежит сегодня,
    # и вход вырастает ровно на его 1000. Один и тот же файл событий, разный
    # пояс — значит сутки считаются по часам владельца, а не по строке UTC.
    TZ=Etc/UTC TOKENS_TODAY="$TODAY" INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
        TOKENS_TRANSCRIPTS_DIR="$T/tr" bash "$0" >/dev/null \
        || { echo "САМОТЕСТ ПРОВАЛЕН: прогон в поясе UTC упал"; exit 1; }
    grep -q 'токены за сутки: вх 1357 · вых 38 · кэш-запись 16 · кэш-чтение 150' "$T/log/tokens-daily.summary" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: в поясе UTC ночное событие обязано попасть в сутки:"; cat "$T/log/tokens-daily.summary"; exit 1; }

    # повторный прогон без новых строк — курсор обязан не дать дублей
    TZ=Etc/GMT-5 TOKENS_TODAY="$TODAY" INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
        TOKENS_TRANSCRIPTS_DIR="$T/tr" bash "$0" >/dev/null \
        || { echo "САМОТЕСТ ПРОВАЛЕН: повторный прогон упал"; exit 1; }
    N2=$(wc -l < "$EVENTS" | tr -d ' ')
    [ "$N2" = 7 ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: повторный прогон надул события до $N2 — курсор не работает"; exit 1; }
    # П-17: файл исчез с диска → его запись вычищается из курсора
    rm "$T/tr/-home-agent-proj/sess-0002.jsonl"
    INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
        TOKENS_TRANSCRIPTS_DIR="$T/tr" bash "$0" >/dev/null \
        || { echo "САМОТЕСТ ПРОВАЛЕН: прогон после удаления файла упал"; exit 1; }
    if grep -q 'sess-0002' "$T/log/tokens.cursor"; then
        echo "САМОТЕСТ ПРОВАЛЕН: запись исчезнувшего файла не вычищена из курсора (П-17)"; exit 1
    fi
    [ -f "$T/hb/tokens-collector" ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: метка heartbeat не поставлена"; exit 1; }
    echo "САМОТЕСТ ПРОЙДЕН: 7 событий (битая посчитана, 2 стриминговых дубля пропущены), ts из транскрипта,"
    echo "сутки по часам владельца (одни данные, пояса UTC+5 и UTC дают разный итог), кэш-запись в сводке,"
    echo "прибор шага (шагов 6, контекст на шаг 85, одиночных 50% — знаменатель только по шагам с вызовами),"
    echo "подробный отчёт (медиана 7 и максимум 300 при среднем 85, попаданий кэша 29,3%, сверка баланса),"
    echo "помесячный файл с символлинком, повторный прогон без дублей, мёртвый файл вычищен из курсора"
    exit 0
fi

# ── конфиги ─────────────────────────────────────────────────────────────────
INSTALL_CONF="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-/etc/harness/install.conf}}"
HARNESS_CONF="${HARNESS_CONF:-/etc/harness/harness.conf}"
[ -r "$INSTALL_CONF" ] || { echo "tokens-collector: нет $INSTALL_CONF — установка не завершена (01-SPEC §0)"; exit 1; }
[ -r "$HARNESS_CONF" ] || { echo "tokens-collector: нет $HARNESS_CONF — скопируйте харнес/config/harness.conf в /etc/harness/"; exit 1; }
# Окружение старше конфига: общий загрузчик вместо голого source (улика
# 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/konf.sh"
konf_zagruzit
: "${AGENT_USER:?пуст AGENT_USER}" "${LOG_DIR:?пуст LOG_DIR}" "${HEARTBEAT_DIR:?пуст HEARTBEAT_DIR}"

TRANSCRIPTS_DIR="${TOKENS_TRANSCRIPTS_DIR:-/home/$AGENT_USER/.claude/projects}"
mkdir -p "$LOG_DIR" "$HEARTBEAT_DIR"

# Каталога транскриптов нет — агент ещё не запускался: считать нечего, это успех.
if [ ! -d "$TRANSCRIPTS_DIR" ]; then
    say "каталога транскриптов $TRANSCRIPTS_DIR ещё нет — агент не запускался, считать нечего"
    touch "$HEARTBEAT_DIR/tokens-collector"
    exit 0
fi

# Коммиты за сутки — знаменатель отдачи: расход сам по себе не говорит,
# много это или мало. Считает git, а не демон: чужую работу мерить нечем,
# репозитория может не быть вовсе — тогда «—» вместо выдуманного числа.
COMMITS_TODAY="—"
if [ -n "${PROJECT_DIR:-}" ] && git -C "$PROJECT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    COMMITS_TODAY=$(git -C "$PROJECT_DIR" log --since="00:00" --oneline 2>/dev/null | wc -l | tr -d ' ')
fi

# ── проход: курсор → новые строки → события → суточный итог ─────────────────
python3 - "$TRANSCRIPTS_DIR" "$LOG_DIR" "$COMMITS_TODAY" <<'PY'
import sys, os, json, glob, math, datetime, tempfile, collections

troot, log_dir = sys.argv[1], sys.argv[2]
коммитов_txt = sys.argv[3] if len(sys.argv) > 3 else "—"
def _сегодня():
    """Дата «сегодня». TOKENS_TODAY задаёт её явно — только для самотеста.

    Без явной даты самотест зависел от часа запуска: набор строился по местной
    дате, а сутки сверялись в двух поясах, и с расхождением дат проба краснела
    каждую ночь (разбор кода 11.09.2026, пункт 13). Проверка, исход которой
    зависит от времени суток, не проверка ([[проверка-создаёт-условие]]).
    """
    задано = os.environ.get("TOKENS_TODAY")
    if задано:
        try:
            return datetime.date.fromisoformat(задано)
        except ValueError:
            pass
    return datetime.date.today()


month = _сегодня().strftime("%Y-%m")
cursor_path  = os.path.join(log_dir, "tokens.cursor")
# П-4: помесячная ротация — события в tokens-YYYY-MM.jsonl текущего месяца,
# прошлые месяцы лежат рядом в log_dir; tokens.jsonl — символлинк на текущий.
events_path  = os.path.join(log_dir, f"tokens-{month}.jsonl")
events_link  = os.path.join(log_dir, "tokens.jsonl")
summary_path = os.path.join(log_dir, "tokens-daily.summary")
# Подробный отчёт (13.08.2026, запрос владельца): одна усреднённая строка не
# отвечает на вопрос «почему кэш такой большой». Отчёт показывает РАСПРЕДЕЛЕНИЕ
# контекста и сходимость «контекст × шаги ≈ прочитано из кэша».
report_path  = os.path.join(log_dir, "tokens-daily.report")
RECENT_MAX = 500  # окно глобально помнимых пар дедупа (П-3)

def write_atomic(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".tokens-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)

# Курсор: {"files": {путь: {"pos": байты, "seen": [пары]}}, "recent": [пары]}.
# Старый формат {путь: байты} мигрируется на лету. Битый курсор — честная
# строка и чтение с нуля: дубль события хуже потерянного не настолько,
# чтобы умирать.
cursor = {"files": {}, "recent": []}
if os.path.exists(cursor_path):
    try:
        with open(cursor_path, encoding="utf-8") as fh:
            raw_cur = json.load(fh)
        if isinstance(raw_cur, dict) and isinstance(raw_cur.get("files"), dict):
            cursor = {"files": raw_cur["files"], "recent": list(raw_cur.get("recent") or [])}
        elif isinstance(raw_cur, dict):
            cursor = {"files": {p: {"pos": int(v), "seen": []}
                                for p, v in raw_cur.items() if isinstance(v, (int, float))},
                      "recent": []}
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        print("курсор tokens.cursor битый — читаю транскрипты с нуля (возможны дубли событий)")

files = cursor["files"]

# П-17: записи файлов, которых больше нет на диске, вычищаются — курсор
# не должен таскать мёртвые пути вечно.
for path in [p for p in files if not os.path.exists(p)]:
    del files[path]

# Множество виденных пар дедупа: общее окно + пофайловые хвосты (П-3).
seen = set(cursor["recent"])
for rec in files.values():
    if isinstance(rec, dict):
        seen.update(rec.get("seen") or [])

def usage_of(obj):
    """(usage, model, message) строки транскрипта; Claude Code кладёт их в message."""
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None, None, msg
    return usage, (msg.get("model") or obj.get("model") or "unknown"), msg


def вызовов_инструментов(msg):
    """Сколько инструментов вызвано одним ответом модели.

    Цена шага — весь накопленный контекст (замер 12.08.2026: 158 535 токенов
    в среднем), поэтому два независимых вызова, разнесённые по двум шагам,
    стоят вдвое. Замер того же дня: 1081 шаг с инструментами, из них с одним
    вызовом — 1081, с двумя и более — ноль. Правило «собирай независимые
    вызовы в один шаг» есть в системном промпте и дало 0 из 1081 — значит
    держать его должен прибор, а не память.
    """
    content = msg.get("content")
    if not isinstance(content, list):
        return 0
    return sum(1 for c in content if isinstance(c, dict) and c.get("type") == "tool_use")

now_iso = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
events, broken, dups = [], 0, 0
finished_pairs = []  # пары дочитанных до конца файлов — уходят в общее окно

for path in sorted(glob.glob(os.path.join(troot, "*", "*.jsonl"))):
    size = os.path.getsize(path)
    rec = files.get(path)
    if not isinstance(rec, dict):
        rec = {"pos": 0, "seen": []}
    done = int(rec.get("pos") or 0)
    if size < done:
        print(f"{path}: файл короче курсора — усечён или пересоздан, читаю с нуля")
        done = 0
    if size > done:
        session = os.path.splitext(os.path.basename(path))[0]
        with open(path, "rb") as fh:
            fh.seek(done)
            chunk = fh.read(size - done)
        # Только целые строки: хвост без \n сейчас дописывается — оставить до
        # следующего прогона, курсор на него не двигать.
        cut = chunk.rfind(b"\n")
        if cut >= 0:
            file_seen = list(rec.get("seen") or [])
            for raw in chunk[:cut].split(b"\n"):
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    broken += 1
                    continue
                if not isinstance(obj, dict):
                    broken += 1
                    continue
                usage, model, msg = usage_of(obj)
                if usage is None:
                    continue
                # Дедуп стриминга (П-3): один ответ = несколько строк с тем же
                # message.id + requestId и тем же usage. Без message.id ключа
                # нет — строка считается как есть.
                mid = msg.get("id")
                if mid:
                    key = f"{mid}|{obj.get('requestId') or ''}"
                    if key in seen:
                        dups += 1
                        continue
                    seen.add(key)
                    file_seen.append(key)
                # ts — момент расхода из транскрипта, не момент прогона (П-17).
                ts = obj.get("timestamp")
                if not isinstance(ts, str) or not ts:
                    ts = now_iso
                events.append({
                    "ts": ts,
                    "session": obj.get("sessionId") or session,
                    "model": model,
                    "in": int(usage.get("input_tokens") or 0),
                    "out": int(usage.get("output_tokens") or 0),
                    "cache_w": int(usage.get("cache_creation_input_tokens") or 0),
                    "cache_r": int(usage.get("cache_read_input_tokens") or 0),
                    "tools": вызовов_инструментов(msg),
                })
            rec["pos"] = done + cut + 1
            rec["seen"] = file_seen
    # Окно дедупа не растёт вечно: у файла, дочитанного до конца, пофайловые
    # пары переезжают в общий хвост recent (обрезается до RECENT_MAX ниже).
    if int(rec.get("pos") or 0) >= size and rec.get("seen"):
        finished_pairs.extend(rec["seen"])
        rec["seen"] = []
    files[path] = rec

cursor["recent"] = (cursor["recent"] + finished_pairs)[-RECENT_MAX:]

# Порядок: сначала события, потом курсор — падение между ними даёт дубль,
# а не потерю; дубль виден в журнале, потеря — нет.
if events:
    with open(events_path, "a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
write_atomic(cursor_path, json.dumps(cursor, ensure_ascii=False) + "\n")

# Символлинк tokens.jsonl → файл текущего месяца (П-4). Обычный файл со
# старым вечным журналом не затирается — честная строка вместо потери данных.
if os.path.exists(events_path):
    target = os.path.basename(events_path)
    try:
        if os.path.islink(events_link):
            if os.readlink(events_link) != target:
                os.remove(events_link)
                os.symlink(target, events_link)
        elif os.path.exists(events_link):
            print(f"tokens.jsonl — обычный файл (старый формат), символлинк не ставлю; события идут в {target}")
        else:
            os.symlink(target, events_link)
    except OSError as e:
        print(f"символлинк tokens.jsonl не обновлён ({e}) — события в {target}")

def дата_события(ts):
    """Дата события в ЧАСАХ ВЛАДЕЛЬЦА (локальный пояс сервера).

    Транскрипт пишет момент в UTC («…Z»), а сутки считаются по локальной дате.
    Пока сервер стоял в UTC, сравнение строк совпадало случайно; после
    перевода сервера в пояс владельца (12.08.2026, UTC+5) окно «сегодня»
    съезжало на пять часов — ночные события уезжали во вчерашний итог.
    Неразобранный ts — первые 10 символов, как раньше: терять событие хуже.
    """
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[:10]
    return (dt.astimezone() if dt.tzinfo else dt).date().isoformat()


# Суточный итог: пересчёт ТОЛЬКО по файлу текущего месяца (П-4), одной строкой.
today = _сегодня().isoformat()
tot = {"in": 0, "out": 0, "cache_w": 0, "cache_r": 0}
by_model = collections.OrderedDict()
шагов = 0          # событий за сутки — за каждое плачен весь контекст
с_вызовами = 0     # шаги, где инструмент вызывался (поле tools есть)
одиночных = 0      # из них с ровно одним вызовом
вызовов_всего = 0  # сумма вызовов инструментов по шагам, где поле есть
контексты = []     # размер контекста каждого шага — для распределения
if os.path.exists(events_path):
    with open(events_path, encoding="utf-8") as fh:
        for raw in fh:
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if дата_события(str(e.get("ts", ""))) != today:
                continue
            m = by_model.setdefault(str(e.get("model", "unknown")), {"in": 0, "out": 0})
            for k in ("in", "out"):
                v = int(e.get(k) or 0)
                tot[k] += v
                m[k] += v
            tot["cache_w"] += int(e.get("cache_w") or 0)
            tot["cache_r"] += int(e.get("cache_r") or 0)
            шагов += 1
            контексты.append(int(e.get("in") or 0) + int(e.get("cache_w") or 0)
                             + int(e.get("cache_r") or 0))
            # События до 12.08.2026 поля не несут — они не портят долю, а
            # выпадают из знаменателя: показать нечестную долю хуже, чем прочерк.
            вызовов = e.get("tools")
            if isinstance(вызовов, int) and вызовов >= 1:
                с_вызовами += 1
                одиночных += вызовов == 1
                вызовов_всего += вызовов
models_txt = "; ".join(f"{name} — вх {v['in']} · вых {v['out']}" for name, v in by_model.items()) or "—"

# Прибор шага (12.08.2026). Расход = размер контекста × число шагов, причём
# контекст растёт с каждым шагом — значит расход сессии квадратичен по её
# длине. Одна строка итога этого не показывала: она говорила, СКОЛЬКО ушло,
# и молчала, ПОЧЕМУ. Три числа ниже показывают оба множителя и то, дробятся
# ли шаги; без них не узнать, помогли ли починки.
контекст_на_шаг = (tot["in"] + tot["cache_w"] + tot["cache_r"]) // шагов if шагов else 0
доля_одиночных = f"{100 * одиночных // с_вызовами}%" if с_вызовами else "—"
шаг_txt = (f"шагов {шагов} · контекст на шаг {контекст_на_шаг} "
           f"· с одним вызовом {доля_одиночных} (из {с_вызовами})")
# Запись в кэш — такой же оплаченный вход, как и остальные, и стоит дороже
# обычного. Без неё сводка врала не цифрой, а картиной: владелец 12.08.2026
# увидел «вх 317» при 14,5 млн чтения из кэша и справедливо не поверил —
# 298 696 записанных в кэш токенов не показывал никто.
write_atomic(summary_path,
    f"токены за сутки: вх {tot['in']} · вых {tot['out']} · кэш-запись {tot['cache_w']} "
    f"· кэш-чтение {tot['cache_r']} (по моделям: {models_txt})\n"
    f"{шаг_txt}\n")


# ── подробный отчёт (13.08.2026) ────────────────────────────────────────────
# Владелец увидел «кэш-чтение 77 миллионов» и спросил, почему так много.
# Среднее на этот вопрос не отвечает: оно одинаково и у ровных шагов, и у
# десятка гигантских среди мелочи. Отчёт показывает распределение и сводит
# баланс: контекст × шаги обязан сойтись с тем, что фактически прочитано.
def кратко(n: int) -> str:
    """Число глазами владельца: 77 319 309 → «77,3M». Порог — по разряду, а не
    по вкусу: до тысячи цифры читаются как есть, дальше рябят."""
    for предел, суффикс in ((1_000_000_000, "G"), (1_000_000, "M"), (1_000, "k")):
        if abs(n) >= предел:
            return f"{n / предел:.1f}".replace(".", ",") + суффикс
    return str(n)


def процентиль(ряд: list, доля: float) -> int:
    """Значение, ниже которого лежит `доля` ряда (метод ближайшего ранга).
    Без интерполяции: ранг честнее для целых токенов и не рождает чисел,
    которых в данных не было."""
    if not ряд:
        return 0
    к = max(1, math.ceil(доля * len(ряд)))
    return sorted(ряд)[к - 1]


коммитов = int(коммитов_txt) if коммитов_txt.isdigit() and int(коммитов_txt) > 0 else 0
# Гейты за сутки: сколько раз проверки прогонялись и сколько из них поймали
# брак. Журнал пишут сами гейты; нет журнала — нет чисел, а не нули: ноль
# читался бы как «всё чисто», хотя значит «не смотрели».
гейтов = красных = 0
gates_path = os.path.join(log_dir, "gates.jsonl")
try:
    with open(gates_path, encoding="utf-8") as fh:
        for raw in fh:
            try:
                g = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if дата_события(str(g.get("ts", ""))) != today:
                continue
            гейтов += 1
            красных += str(g.get("verdict")) == "fail"
except OSError:
    гейтов = красных = 0

контекст_всего = tot["in"] + tot["cache_w"] + tot["cache_r"]
попаданий = f"{100 * tot['cache_r'] / контекст_всего:.1f}%".replace(".", ",") if контекст_всего else "—"
на_шаг = f"{вызовов_всего / с_вызовами:.1f}".replace(".", ",") if с_вызовами else "—"
многовызовных = f"{100 * (с_вызовами - одиночных) // с_вызовами}%" if с_вызовами else "—"
выход_на_шаг = tot["out"] // шагов if шагов else 0
# Сходимость: произведение среднего на число шагов против фактического объёма.
# Разойдись они — врёт либо счётчик шагов, либо суточная выборка, и дальше
# отчёту верить нельзя. Строка стоит в отчёте именно как самопроверка.
сверка = контекст_на_шаг * шагов

отчёт = [
    f"РАСХОД ЗА СУТКИ — {today}",
    "",
    f"Шаги (обращения к модели)   {шагов}",
    f"  с вызовом инструмента     {с_вызовами}",
    f"  вызовов на такой шаг      {на_шаг}",
    f"  ровно один вызов          {доля_одиночных}",
    f"  два и больше              {многовызовных}",
    "",
    "Контекст, который несёт шаг",
    f"  среднее                   {кратко(контекст_на_шаг)}",
    f"  медиана                   {кратко(процентиль(контексты, 0.50))}",
    f"  90-й процентиль           {кратко(процентиль(контексты, 0.90))}",
    f"  95-й процентиль           {кратко(процентиль(контексты, 0.95))}",
    f"  максимум                  {кратко(max(контексты) if контексты else 0)}",
    "",
    "Кэш",
    f"  прочитано                 {кратко(tot['cache_r'])}",
    f"  записано                  {кратко(tot['cache_w'])}",
    f"  свежий вход мимо кэша     {кратко(tot['in'])}",
    f"  попаданий                 {попаданий}",
    "",
    "Ответы модели",
    f"  всего                     {кратко(tot['out'])}",
    f"  на шаг                    {выход_на_шаг}",
    "",
    "Отдача",
    f"  коммитов за сутки         {коммитов_txt}",
    f"  шагов на коммит           {шагов // коммитов if коммитов else '—'}",
    f"  ответов на коммит         {кратко(tot['out'] // коммитов) if коммитов else '—'}",
    f"  прогонов гейтов           {гейтов}, красных {красных}",
    "",
    f"Сверка: {кратко(контекст_на_шаг)} × {шагов} шагов = {кратко(сверка)}; "
    f"фактически прошло {кратко(контекст_всего)}.",
    f"Кэш покрыл {попаданий} этого объёма — остальное оплачено как свежий вход.",
]
write_atomic(report_path, "\n".join(отчёт) + "\n")

print(f"новых событий: {len(events)}; файлов в курсоре: {len(files)}")
if broken:
    print(f"битых строк пропущено: {broken}")
if dups:
    print(f"дублей пропущено: {dups}")
print("суточный итог пересчитан: tokens-daily.summary, tokens-daily.report")
PY

touch "$HEARTBEAT_DIR/tokens-collector"
say "метка heartbeat обновлена"
