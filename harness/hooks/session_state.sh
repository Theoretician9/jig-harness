#!/usr/bin/env bash
# Взят из UNIFIED/templates/hooks/session_state.sh; добавлено: конфиг /etc/harness/install.conf,
# свежесть документа передачи, режим прав, режим AUTONOMY, systemd, метки heartbeat;
# убраны миграции/health конкретного проекта.
#
# Хук SessionStart: показать, в каком состоянии проект застала новая сессия.
# Без этого сессия начинается со слепого места: незакоммиченные правки прошлого
# сеанса, упавший юнит или протухшая метка обнаруживаются случайно и посреди
# работы. Дешевле напечатать это сразу. Печатает в stdout — харнес добавляет
# вывод в контекст сессии.
#
# set без -e — осознанно (fail-open): хук обязан напечатать максимум состояния;
# отказ одной подсистемы (нет docker, нет git) не должен убивать весь отчёт —
# о недоступном говорим строкой в вывод, а не молчаливым выходом.
set -uo pipefail

# source запуска — из JSON-нагрузки хука на stdin (startup|clear|compact|resume).
# После компакта полный отчёт о состоянии не нужен — нужен короткий блок-якорь
# (В-1: stdout PreCompact модель не видит, поэтому напоминание живёт здесь).
# `cat` без предела ЖДЁТ ВЕЧНО, когда stdin — открытая труба без данных: так
# хук, позванный из смены руками, провисел 19 минут 15.10.2026, а сторож считал
# это работой. Хук Claude Code подаёт JSON сразу, поэтому двух секунд довольно.
hook_source=$(timeout 2 cat 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("source") or "")' 2>/dev/null || echo "")
if [ "$hook_source" = "compact" ]; then
  echo "=== после компакта ==="
  echo "Контекст только что пережат — решения могли потеряться. Сверься с памятью"
  echo "(память/MEMORY.md) и передачей (docs/handover/SESSION-HANDOFF-*.md); если"
  echo "передача отстала от сделанного — обнови её сейчас."
  echo "Второй компакт подряд запрещён — вместо него ротация сессии (session-warden скомандует)."
  exit 0
fi

# --- Сверка носителя личности смены (спека §6.2, контракт Н-5) ---
# Смена, поднятая голым claude, мимо komanda_agenta или со старым файлом
# личности, работает без стандартов — и никто об этом не знает. Первой строкой
# вывода, раньше всего остального: оно не должно утонуть в отчёте.
# $PPID хука — промежуточная «sh -c», не claude (замер 10.11.2026), поэтому вверх
# по цепочке ищется БЛИЖАЙШИЙ процесс с argv0 claude. Подстрока в доводах не
# годится: у claude-run.sh в доводах тоже «claude». Хук стартует и у каждого
# claude -p (демоны, приборы) — им личность не смены, и они уходят отсюда СРАЗУ,
# без вызова сборщика: сборщик стоит секунды на каждый такой запуск.
nearest_claude_argv() {  # заполняет SHIFT_ARGV; 1 — claude в цепочке нет
    local root="${SESSION_PROC_ROOT:-/proc}" cur="${SESSION_PROC_START:-$PPID}" depth=0 k v next
    SHIFT_ARGV=()
    while [ "${cur:-0}" -gt 1 ] 2>/dev/null && [ "$depth" -lt 40 ]; do
        [ -r "$root/$cur/cmdline" ] || return 1
        mapfile -d '' -t SHIFT_ARGV < "$root/$cur/cmdline"
        [ "${#SHIFT_ARGV[@]}" -gt 0 ] && [ "${SHIFT_ARGV[0]##*/}" = claude ] && return 0
        next=""
        while read -r k v; do [ "$k" = "PPid:" ] && { next="$v"; break; }; done < "$root/$cur/status" 2>/dev/null
        cur="$next"; depth=$((depth + 1))
    done
    SHIFT_ARGV=()
    return 1
}

# Печатает причину, если смена без личности; пусто — порядок. Код 2 — claude -p:
# не смена, отметку не трогаем ни в какую сторону.
shift_without_self_reason() {
    local a prev="" file="" want got asm tmp
    nearest_claude_argv || { echo "родитель не claude"; return 0; }
    for a in "${SHIFT_ARGV[@]:1}"; do
        case "$a" in -p|--print) return 2 ;; esac
        [ "$prev" = --append-system-prompt-file ] && file="$a"
        case "$a" in --append-system-prompt-file=*) file="${a#*=}" ;; esac
        prev="$a"
    done
    [ -n "$file" ] || { echo "claude без --append-system-prompt-file (мимо komanda_agenta)"; return 0; }
    [ -f "$file" ] || { echo "файла личности нет: $file"; return 0; }
    [ -s "$file" ] || { echo "файл личности пуст: $file"; return 0; }
    asm="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../self-prompt.py"
    [ -f "$asm" ] || return 0   # сборщика нет — метку судить нечем
    tmp=$(mktemp) || return 0
    timeout 5 python3 "$asm" --класс оркестратор --в "$tmp" >/dev/null 2>&1
    want=$(head -1 "$tmp" 2>/dev/null); rm -f "$tmp"
    [ -n "$want" ] || { echo "сборщик не дал метку класса «оркестратор»"; return 0; }
    got=$(head -1 "$file")
    [ "$got" = "$want" ] || echo "метка файла «$got» не равна текущей сборке «$want»"
    return 0
}

self_reason=$(shift_without_self_reason); self_rc=$?
if [ -n "$self_reason" ]; then
  echo "смена поднята без личности: $self_reason"
  echo "  Поднять смену надо через komanda_agenta (scripts/lib/config.sh), иначе стандарты и роль не загружены."
fi

# Окружение старше конфига: общий загрузчик вместо голого source (улика
# 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
# У него же живёт корень установки: зашитые «/var/log/harness» и «/etc/harness»
# ставили харнес ровно на одну машину (владелец 21.09).
# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../lib/config.sh"
conf_path="${HARNESS_INSTALL_CONF:-$(konf_koren)/install.conf}"
project_name="проект"
project_dir=""
autonomy="semi"
heartbeat_dir=""
log_dir=""

if [ -r "$conf_path" ]; then
  konf_zagruzit
  project_name="${PROJECT_NAME:-$project_name}"
  project_dir="${PROJECT_DIR:-}"
  autonomy="${AUTONOMY:-semi}"
  heartbeat_dir="${HEARTBEAT_DIR:-$heartbeat_dir}"
  log_dir="${LOG_DIR:-$log_dir}"
else
  echo "⚠ конфиг $conf_path недоступен — работаю со значениями по умолчанию"
fi

# Сигнал о смене без личности хук сам не шлёт: он идёт под claude, а
# alert-task.py отказывает зовам из-под claude. Кладём отметку, сигнал по ней
# шлёт сторож сессий на обходе. Атомарно (временный файл рядом + mv): сторож
# читает отметку в любой момент. Порядок снимает прежнюю отметку; claude -p
# (self_rc 2) — не смена, отметка не тронута.
shift_mark_file="${SHIFT_SELF_MARK:-${log_dir:+$log_dir/shift-without-self.mark}}"
if [ -n "$shift_mark_file" ] && [ "$self_rc" -ne 2 ]; then
  if [ -n "$self_reason" ]; then
    shift_mark_tmp=$(mktemp "$shift_mark_file.XXXXXX" 2>/dev/null) \
      && printf '%s %s\n' "$(date +%s)" "$self_reason" > "$shift_mark_tmp" \
      && chmod 644 "$shift_mark_tmp" \
      && mv -f "$shift_mark_tmp" "$shift_mark_file" || rm -f "${shift_mark_tmp:-}"
  else
    rm -f "$shift_mark_file"
  fi
fi

# Резка по СИМВОЛАМ, не байтам. `cut -c` считает байты и рвёт кириллицу
# посередине: таблица скилов едет в контекст каждой сессии, и пять её строк
# приходили с обрывком вместо буквы (разбор кода 11.09.2026, пункт 11).
обрезать() { # $1 = сколько символов; текст со stdin
    LC_ALL=C.UTF-8 awk -v n="$1" '{print substr($0, 1, n)}'
}

# Сессия, запущенная не из PROJECT_DIR, не загружает CLAUDE.md, память и скилы
# проекта: агент работает вслепую и не знает об этом. Предупреждение — первой
# строкой, пока оно не утонуло в отчёте.
own_dir=$(readlink -f "$project_dir" 2>/dev/null || echo "$project_dir")
# Подкаталог проекта — свой: claude поднимается по дереву к CLAUDE.md проекта.
if [ -n "$project_dir" ] && case "$(pwd -P)/" in "$own_dir"/*) false ;; *) true ;; esac; then
  echo "⚠⚠⚠ СЕССИЯ ЗАПУЩЕНА НЕ ИЗ $project_dir (сейчас: $(pwd -P)): CLAUDE.md, память и скилы проекта НЕ загружены. Скажи владельцу и перезапусти: cd $project_dir && claude"
fi

echo "=== $project_name: состояние на старте сессии ==="

# --- ПЕРВАЯ ЗАДАЧА НОВОЙ УСТАНОВКИ: личность копии пуста ---
# Пакет кладёт instance.yaml пустым: личность «строитель харнеса» на чужом
# проекте ложна. Заполняет агент после вопроса владельцу в канале —
# интерактивных вопросов в терминале нет. Путь к коду продукта нужен только
# копии-проекту: у агента самого харнеса продукта нет, PRODUCT_PATH пуст законно.
# PRODUCT_DIR — имя compose-проекта, а не путь: его заполненность пути не заменяет.
# Старым образцом считается «копия: project» (внутренний) или «копия:
# project» (публичная 0.9.1) + роль агента харнеса на установке с другим именем (тот же признак, что у раскладки); любая иная
# заполненная личность своя. Ошибка чтения — молчим (fail-open).
if [ -n "$project_dir" ]; then
  first_task=$(python3 - "$project_dir/harness/self/instance.yaml" "${PRODUCT_PATH:-}" "${PROJECT_NAME:-}" <<'PY' 2>/dev/null
import sys, yaml
path, product, name = sys.argv[1], sys.argv[2].strip(), sys.argv[3].strip()
try:
    data = yaml.safe_load(open(path, encoding="utf-8")) or {}
except OSError:
    sys.exit(0)
empty = [k for k in ("копия", "роль", "цель") if not str(data.get(k) or "").strip()]
role = str(data.get("роль") or "").strip()
if empty:
    print("instance.yaml пуст: " + ", ".join(empty))
elif (str(data["копия"]).strip() in ("project", "project") and role == "агент самого харнеса"
      and name and name != "project"):
    print("instance.yaml — старый образец пакета (агент самого харнеса) на проекте «%s»" % name)
elif role != "агент самого харнеса" and not product:
    print("PRODUCT_PATH пуст (путь к коду продукта не задан)")
PY
)
  if [ -n "$first_task" ]; then
    echo "‼ ПЕРВАЯ ЗАДАЧА, ДО ВСЕГО ОСТАЛЬНОГО ($first_task):"
    echo "  спроси владельца в канале назначение копии (что за проект, какая цель)"
    echo "  и путь к коду продукта; запиши ответ через python3 scripts/self-set.py <поле> <значение>"
    echo "  (копия, роль, цель: владельцу придёт кнопка «Да»; instance.yaml руками не правится)"
    echo "  и в PRODUCT_PATH (harness.conf)."
    echo "  Вопросов в терминале не задавай — только через канал владельца."
  fi
fi

# --- СВЕРКИ СЛОЁВ ПАМЯТИ (спека hr.standards-and-policy §5.3–§5.4) ---
# Слой 2: правка instance.yaml мимо двери self-set.py видна по sha256 последнего «да»;
# пустое обязательное поле — один вопрос в сутки. Слой 1: дерево против эталона;
# эталона нет — молчим, об этом говорит сборщик личности. Сбой сверки не роняет старт.
if [ -n "$project_dir" ]; then
  timeout 15 python3 "$project_dir/scripts/self-set.py" --старт 2>/dev/null
  python3 - "$project_dir/harness/self" "${SELF_ETALON_DIR:-$(konf_koren)/self}" <<'PY' 2>/dev/null
import hashlib, sys
from pathlib import Path
tree, etalon = Path(sys.argv[1]), Path(sys.argv[2])
# instance.yaml — слой 2, MANIFEST.sha256 печатает выпуск: в эталон не входят.
SKIP = {"instance.yaml", "MANIFEST.sha256"}
def digests(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and p.relative_to(root).as_posix() not in SKIP}
if etalon.is_dir() and tree.is_dir():
    sealed, living = digests(etalon), digests(tree)
    diverged = sorted(k for k in set(sealed) | set(living) if sealed.get(k) != living.get(k))
    if diverged:
        print("⚠ слой 1 разошёлся с эталоном: " + ", ".join(diverged))
PY
fi

# --- ПАУЗА ВЛАДЕЛЬЦА: первой строкой, до всего остального ---
# 21.09.2026 владелец нажал «паузу», а смена работала ещё полчаса. Физически её
# держит хук pause_guard.py; здесь пауза НАЗЫВАЕТСЯ, чтобы преемник узнал о ней
# из отчёта, а не из отказа первой же команды.
if [ -n "$project_dir" ] && python3 "$project_dir/scripts/pause.py" --идёт-ли 2>/dev/null; then
  echo "⏸ ВЛАДЕЛЕЦ ПОСТАВИЛ ПАУЗУ — работать нельзя: $(python3 "$project_dir/scripts/pause.py" --почему 2>/dev/null)"
  echo "  Снимает её только он: кнопка «продолжить» в меню бота или слово «продолжай»."
fi

# --- НАМЕЧЕННОЕ: то, что смена должна доделать, пережившее лимит и ротацию ---
# Владелец 21.09.2026: «Лимит не должен сбрасывать намеченные планы». Запись
# ставит КОД (гейт пайплайна в момент отказа) и он же снимает её, когда шаг
# пройден, — план, который надо вспомнить, лимита не переживает.
if [ -n "$project_dir" ]; then
  namechennoe=$(python3 "$project_dir/scripts/board.py" --читать намечено 2>/dev/null)
  doska_kod=$?
  if [ "$doska_kod" = 3 ]; then
    # «Доска не читается» — не «записей нет»: молчание здесь скрыло бы долг.
    echo "⚠ доска состояния НЕ ЧИТАЕТСЯ — намеченное неизвестно: $project_dir/scripts/board.py --всё"
  elif [ "$doska_kod" = 0 ] && [ -n "$namechennoe" ]; then
    # Код 0 — запись ЖИВА. Без него в осмотр печаталась сама строка доски
    # «намечено: записи нет», то есть пустота выглядела намеченным планом.
    echo "📌 НАМЕЧЕНО (пережило смену): $namechennoe"
  fi
  # ОСТАЛЬНЫЕ живые записи доски. Ревью 27.09 (M-01): сторож памяти клал долг
  # ключом «память-не-коммитится», а читался только «намечено» — долг лежал
  # там, где его никто не спрашивает, то есть механизм был мёртв и выглядел
  # живым ([[a-mechanism-that-needs-a-human-is-dead]]). Любой писатель доски
  # теперь доходит до смены сам, без правки этого файла под каждый ключ.
  prochee=$(python3 "$project_dir/scripts/board.py" --всё 2>/dev/null \
            | grep -v "^намечено:" || true)
  if [ -n "$prochee" ]; then
    echo "📌 НА ДОСКЕ:"
    printf '%s\n' "$prochee" | sed 's/^/   /'
  fi
else
  # Молчать нельзя ровно здесь: у соседних строк (git, передача, режим прав)
  # недоступный PROJECT_DIR называется явным «⚠», а намеченное пропадало без
  # звука — преемник не узнавал, что его вообще не смотрели. Это тот же класс
  # отказа, ради которого намеченное и заводили (находка ревью I-1, 23.09).
  echo "⚠ намеченное НЕ ПРОВЕРЕНО — PROJECT_DIR недоступен ($conf_path)"
fi

# --- СЛЕДУЮЩАЯ РАБОТА: чтобы «свободной работы нет» не решалось на глаз ------
# Владелец 26.09.2026: «нужна иерархия, если у тебя блокер, но при этом есть
# любая другая работа, она должна исполняться, вплоть до следующего блокера.
# Это должен быть механизм». В тот день смена трижды за час встала со словами
# «свободной работы нет», пока прибор держал незаблокированных кандидатов.
if [ -n "$project_dir" ] && [ -f "$project_dir/dev-map.yaml" ]; then
  python3 "$project_dir/scripts/work-queue.py" "$project_dir/dev-map.yaml" \
      --следующая 2>/dev/null | head -4
fi

# --- режим хакатон: включён ли и сколько карточек его уровня ---
if [ -n "$project_dir" ]; then
  python3 "$project_dir/scripts/hackathon.py" --строка 2>&1 | head -2 \
    || echo "хакатон: строка недоступна"
fi

# --- режим автономии из конфига ---
echo "автономия: $autonomy"
[ "$autonomy" = "semi" ] && echo "  (полуавтомат: деплой и push — через ворота; владелец подтверждает в Telegram)"

# --- git-состояние и настройки проекта ---
if [ -n "$project_dir" ] && cd "$project_dir" 2>/dev/null; then
  if git rev-parse --git-dir >/dev/null 2>&1; then
    echo "ветка: $(git rev-parse --abbrev-ref HEAD 2>/dev/null) · последний коммит: $(git log -1 --format='%h %s' 2>/dev/null | обрезать 80)"

    ahead=$(git rev-list --count '@{u}..HEAD' 2>/dev/null)
    if [ -z "${ahead:-}" ]; then
      echo "⚠ upstream не настроен — «отправлено ли» проверить нельзя"
    elif [ "$ahead" -gt 0 ]; then
      echo "⚠ не отправлено в удалённый репозиторий: $ahead коммит(ов)"
    fi

    dirty=$(git status --porcelain 2>/dev/null | grep -v '^??' | wc -l)
    [ "${dirty:-0}" -gt 0 ] && { echo "⚠ незакоммичено файлов: $dirty"; git status --short 2>/dev/null | grep -v '^??' | head -5 | sed 's/^/    /'; }

    # Свежесть документа передачи меряется КОММИТАМИ, а не mtime файла: передача
    # всегда пишется за минуту ДО коммита и входит в него же, поэтому сравнение
    # mtime с временем последнего коммита давало ложную тревогу на каждом старте
    # (случай 10.09.2026: файл записан за 64 с до коммита, вошёл в него, сторож
    # кричал «прошлая сессия её не обновила»). Признак верный — сколько коммитов
    # легло ПОСЛЕ того, в котором передачу трогали последний раз.
    newest_handoff=$(ls -1t docs/handover/SESSION-HANDOFF-*.md 2>/dev/null | head -1)
    if [ -z "$newest_handoff" ]; then
      echo "⚠ документа передачи нет (docs/handover/SESSION-HANDOFF-*.md)"
    elif [ -n "$(git status --porcelain -- "$newest_handoff" 2>/dev/null)" ]; then
      echo "передача: $newest_handoff — правится прямо сейчас (не закоммичена)"
    else
      handoff_commit=$(git log -1 --format=%H -- "$newest_handoff" 2>/dev/null)
      behind=$(git rev-list --count "${handoff_commit:-HEAD}..HEAD" 2>/dev/null || echo 0)
      # Один коммит запаса — рубеж бывает закрыт двумя коммитами подряд.
      if [ "${behind:-0}" -le 1 ]; then
        echo "передача: $newest_handoff — свежая (коммитов после неё: ${behind:-0})"
      else
        echo "⚠ передача отстала на $behind коммит(ов): $newest_handoff — рубежи закрывались без неё"
      fi
    fi

    # Режим прав из настроек проекта (permissions.defaultMode).
    perm_mode=$(python3 - <<'PY' 2>/dev/null
import json
for p in (".claude/settings.local.json", ".claude/settings.json"):
    try:
        m = json.load(open(p, encoding="utf-8")).get("permissions", {}).get("defaultMode")
        if m:
            print(m)
            break
    except Exception:
        pass
PY
)
    if [ -n "${perm_mode:-}" ]; then
      echo "режим прав: $perm_mode"
    else
      echo "⚠ режим прав не определён (permissions.defaultMode не найден в .claude/settings*.json)"
    fi
  else
    echo "⚠ git недоступен: $project_dir — не репозиторий; состояние кода не проверить"
  fi
else
  echo "⚠ PROJECT_DIR (${project_dir:-не задан}) недоступен — git, передача и режим прав не проверены"
fi

# --- упавшие systemd-юниты ---
if command -v systemctl >/dev/null 2>&1; then
  # Известный отказ — юнит хостинга (cloud-init на bootcmd), чинить который харнесу нечем:
  # перечень в данных boot-check.yaml, он не краснит смену, но и не скрыт.
  known_units=$(python3 - "$project_dir/harness/config/boot-check.yaml" <<'PY' 2>/dev/null
import sys, yaml
try:
    for юнит in (yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}).get("известный_отказ") or []:
        print(юнит)
except OSError:
    pass
PY
)
  all_failed=$(systemctl list-units --failed --no-legend --plain 2>/dev/null | awk '{print $1}')
  failed_units=$(printf '%s\n' "$all_failed" | grep -vxF -f <(printf '%s\n' "$known_units") | grep . | tr '\n' ' ' || true)
  known_failed=$(printf '%s\n' "$all_failed" | grep -xF -f <(printf '%s\n' "$known_units") | grep . | tr '\n' ' ' || true)
  if [ -n "${failed_units// /}" ]; then
    echo "⚠ упавшие systemd-юниты: $failed_units"
  else
    echo "systemd: упавших юнитов нет"
  fi
  [ -z "${known_failed// /}" ] || echo "известный отказ хоста (не чинится харнесом): $known_failed"
else
  echo "⚠ systemctl недоступен — состояние юнитов не проверено"
fi

# --- контейнеры (если docker есть) ---
if command -v docker >/dev/null 2>&1; then
  down=$( { docker ps --filter status=restarting --format '{{.Names}}(restarting)' 2>/dev/null; \
           docker ps -a --filter status=exited --format '{{.Names}} {{.Status}}' 2>/dev/null \
             | sed -n 's/^\([^ ]*\) Exited (\([1-9][0-9]*\)).*/\1(exited \2)/p'; } | tr '\n' ' ')
  if [ -n "${down// /}" ]; then
    echo "⚠ контейнеры не в строю: $down"
  else
    echo "контейнеры: упавших и перезапускающихся нет"
  fi
else
  echo "docker отсутствует — контейнеры не проверяю"
fi

# --- напоминание о скилах: условия срабатывания в контекст каждой сессии ---
# Улика жанра: скил без явного условия в контексте молчит — модель «забывает»
# скилы, если их триггеры не стоят перед глазами (замер чужого хука активации:
# ~20% → ~84%). Печатаем таблицу «когда → скил» из description каждого SKILL.md:
# внимание модели — последний носитель, поэтому условия ей подкладывает код.

skills_dir="$project_dir/harness/skills"
if [ -d "$skills_dir" ]; then
  # Каким языком писать владельцу — считается по ЕГО сообщениям (владелец
# 11.09.2026: «Твой язык должен подстраиваться к моему поведению»). Печатается
# здесь, потому что правило, которое не видно в начале хода, — не правило.
LANG_LEVEL=$(python3 "$project_dir/scripts/language-level.py" 2>/dev/null || echo "обычный")
case "$LANG_LEVEL" in
  простой)     echo "язык владельцу: ПРОСТОЙ — без терминов; «гейт» → «проверка, которая не пропускает»" ;;
  технический) echo "язык владельцу: ТЕХНИЧЕСКИЙ — термины как есть, без пояснений" ;;
  *)           echo "язык владельцу: ОБЫЧНЫЙ — термин можно, но поясняй при первом появлении" ;;
esac

echo "--- скилы: когда какой обязателен (вызов пишется в журнал) ---"
  for sk in "$skills_dir"/*/SKILL.md; do
    [ -e "$sk" ] || continue
    name=$(basename "$(dirname "$sk")")
    cond=$(grep -m1 '^description:' "$sk" | sed 's/^description:[[:space:]]*//; s/["'"'"']//g' | обрезать 120)
    echo "  $name — $cond"
  done
else
  echo "каталог скилов $skills_dir не найден — напоминание об условиях недоступно"
fi

# --- возраст меток heartbeat ---
if [ -d "$heartbeat_dir" ]; then
  now_ts=$(date +%s)
  found=0
  for f in "$heartbeat_dir"/*; do
    [ -e "$f" ] || continue
    found=1
    mtime=$(stat -c %Y "$f" 2>/dev/null || echo "$now_ts")
    age_min=$(( (now_ts - mtime) / 60 ))
    echo "метка $(basename "$f"): обновлена $age_min мин назад"
  done
  [ "$found" -eq 0 ] && echo "⚠ каталог меток $heartbeat_dir пуст — фоновые контуры не отмечаются"
else
  echo "⚠ каталог меток $heartbeat_dir недоступен — возраст heartbeat не проверен"
fi

# --- очередь владельца, работа и расход: три вещи, которые агент иначе идёт
# смотреть тремя отдельными шагами. Цена шага — весь накопленный контекст
# (замер 12.08.2026: 158 535 токенов), поэтому осмотр обязан быть ОДНИМ
# вызовом. Этот же скрипт годится для ручного прогона на любом рубеже:
# отдельный «осмотр.sh» был бы вторым источником правды о том же самом.
inbox_dir="$log_dir/inbox"
if [ -d "$inbox_dir" ]; then
  waiting=$(find "$inbox_dir" -maxdepth 1 -type f -name '*.txt' 2>/dev/null | wc -l)
  if [ "$waiting" -gt 0 ]; then
    echo "⚠ ВХОДЯЩИЕ ОТ ВЛАДЕЛЬЦА: $waiting — прочитать до работы ($inbox_dir)"
  else
    echo "входящие: очередь пуста"
    # Ответ владельца мог прийти в окно ротации: диспетчер разложил его по
    # каталогу, а сессия, которой он адресован, уже закончилась. СЛУЧАЙ
    # 18.08.2026: ответ, снимавший блокировку работы, пролежал прочитанным-но-
    # неотработанным до следующего старта, и преемник начал с «ждём ответа».
    # Поэтому пустая очередь — не то же самое, что «ничего не приходило».
    last_done=$(find "$inbox_dir/обработано" -maxdepth 1 -type f -name '*.txt' \
                     -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
    if [ -n "$last_done" ]; then
      commit_ts=$(git -C "$project_dir" log -1 --format=%ct 2>/dev/null || echo 0)
      done_ts=$(stat -c %Y "$last_done" 2>/dev/null || echo 0)
      if [ "${done_ts:-0}" -gt "${commit_ts:-0}" ]; then
        echo "⚠ последнее сообщение владельца НОВЕЕ последнего коммита — перечитать:"
        head -c 400 "$last_done" | sed 's/^/    /'
        echo
      fi
    fi
  fi

  # ── последние слова владельца ─────────────────────────────────────────────
  # Не «есть ли непрочитанное», а «что он вообще велел». Сообщения владельца
  # лежат в обработано/ и в контекст не попадают ни одним путём: новая сессия
  # видит CLAUDE.md, STATE.md и память — но не канал. Из-за этого указание
  # «искать дома по карте, а не по названию» (19.08) не пережило ротацию, и
  # 20.08 владелец повторил его упрёком. Пять последних сообщений стоят около
  # тысячи символов — дешевле одного повторённого разговора.
  # Действующие указания живут отдельно и грузятся всегда: память/ORDERS.md.
  # Обрезка байтами (head -c) + iconv -c: `cut -c` считает БАЙТЫ и рвёт
  # кириллицу посреди буквы (тот же случай, что в gates.sh); iconv
  # выбрасывает недоеденный хвост, а не печатает «зад\ufffd».
  if [ -d "$inbox_dir/обработано" ]; then
    echo "последние слова владельца (полностью — $inbox_dir/обработано/):"
    find "$inbox_dir/обработано" -maxdepth 1 -type f -name '*.txt' -printf '%T@ %p\n' 2>/dev/null \
      | sort -rn | head -5 | cut -d' ' -f2- | tac | while read -r msg; do
        printf '    %s · %s\n' "$(date -r "$msg" '+%d.%m %H:%M')" \
          "$(head -c 300 "$msg" | tr '\n' ' ' | iconv -f utf-8 -t utf-8 -c 2>/dev/null)"
      done
  fi
fi

# --- комментарии владельца из панели, не дошедшие до агента ---
# Диспетчер пинает агента комментарием и отмечает доставку; пинок, не заставший
# агента в панели, не отмечается. Без этой строки такое слово владельца ждало
# бы только следующего удачного пинка (спека hr.kanban §4).
if [ -n "$project_dir" ] && [ -f "$project_dir/scripts/comments-intake.py" ]; then
  comment_claims=$(timeout 10 python3 "$project_dir/scripts/comments-intake.py" 2>/dev/null)
  comment_rc=$?
  if [ "$comment_rc" != 0 ]; then
    echo "⚠ комментарии панели не прочитаны (comments-intake.py, код $comment_rc): ${comment_claims:0:300}"
  elif [ -n "$comment_claims" ]; then
    echo "⚠ комментариев не доставлено: $(printf '%s\n' "$comment_claims" | wc -l) — слова владельца из панели:"
    printf '%s\n' "$comment_claims" | cut -f2- | head -c 1200 | iconv -f utf-8 -t utf-8 -c 2>/dev/null | sed 's/^/    /'
  fi
fi

# Очередь и ПОРЯДОК — одним вызовом: карта весит 390 КБ, и разбирать её дважды
# ради двух строк значит платить 0,8 с на каждом старте сессии.
queue_tool="$project_dir/scripts/work-queue.py"
if [ -f "$queue_tool" ] && [ -f "$project_dir/dev-map.yaml" ]; then
  queue_out=$(python3 "$queue_tool" "$project_dir/dev-map.yaml" --порядок 2>/dev/null)
  echo "очередь работы: $(printf '%s\n' "$queue_out" | head -1)"
  # «Сейчас делаем» — ответ на вопрос, который иначе решается в голове: место
  # задачи назначает код (владелец 12.09.2026 — «это должно контролироваться
  # кодом и быть безотказным»).
  printf '%s\n' "$queue_out" | grep -m1 '^сейчас делаем:' || true
fi

# Заявки помощников на файлы (scripts/hooks/file_claim_guard.py); список — scripts/who-writes.py.
echo "заняты файлы: $(python3 "$project_dir/scripts/who-writes.py" --count 2>/dev/null || echo '?')"

# «Сделано, но не заявлено»: вся работа карточки plan/wip закрыта, коммиты
# затихли. Только число — перевести в test решает смена (И-4).
acceptance_tool="$project_dir/scripts/acceptance.py"
if [ -f "$acceptance_tool" ] && [ -f "$project_dir/dev-map.yaml" ]; then
  python3 "$acceptance_tool" --кандидаты 2>/dev/null | tail -1 || true
fi

# Итог вложенности за сутки: глубина, параллельные, доля тяжёлых. Нет скрипта
# или он упал — молчим: строка не стоит провала старта сессии.
nesting_tool="$project_dir/scripts/launch-plan.py"
if [ -n "$project_dir" ] && [ -f "$nesting_tool" ]; then
  timeout 5 python3 "$nesting_tool" --метрики --сутки 1 --строка 2>/dev/null | head -1 || true
fi

# Расход показывается ПОСЛЕДНИМ и целиком: строка «контекст на шаг» — это цена
# каждого следующего шага, и видеть её полезно именно перед тем, как их делать.
tokens_summary="$log_dir/tokens-daily.summary"
if [ -f "$tokens_summary" ]; then
  sed 's/^/расход: /' "$tokens_summary"
  # Сводку пишет часовой сбор (cron :10). Без отметки момента агент принимает
  # её за «прямо сейчас» и называет владельцу числа, которых не измерял
  # (случай 12.08.2026: отчёт «1568 шагов» при последнем замере 1389).
  collected_at=$(date -r "$tokens_summary" +%H:%M 2>/dev/null)
  age_min=$(( ($(date +%s) - $(date -r "$tokens_summary" +%s 2>/dev/null || echo 0)) / 60 ))
  echo "расход: числа на $collected_at ($age_min мин назад), обновляются ежечасно в :10 —" \
       "свежие: bash $project_dir/harness/demons/tokens-collector.sh"
fi
# Версия CLI против паспорта установки. Ключ CC_VERSION лежал в install.conf
# и не читался НИКЕМ (ревизия 10.09.2026: данные-сирота). Расхождение важно:
# обновление Claude Code меняет и диалоги, и поведение хуков — а замечают это
# по странным симптомам, не по версии.
if [ -n "${CC_VERSION:-}" ] && command -v claude >/dev/null 2>&1; then
  live=$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
  if [ -n "$live" ] && [ "$live" != "$CC_VERSION" ]; then
    echo "⚠ версия Claude Code $live, в паспорте установки CC_VERSION=$CC_VERSION — обновите паспорт или откатите CLI"
  fi
fi

# Известные ПРОПУСКИ: сломалось, а ни один гейт не покраснел. Счётчик
# срабатываний гейтов о качестве не говорит — говорит этот (замечание
# владельца М-3, 10.09.2026). Пустой журнал через две недели значит, что его
# не заполняют, а не что пропусков нет.
misses="$log_dir/пропуски.jsonl"
if [ -f "$misses" ]; then
  since=$(date -d '30 days ago' +%Y-%m-%d 2>/dev/null)
  n=$(awk -v since="$since" -F'"дата": *"' 'NF>1 {split($2,d,"\""); if (d[1] >= since) c++} END {print c+0}' "$misses")
  last=$(tail -1 "$misses" | sed 's/.*"что": *"\([^"]*\)".*/\1/' | обрезать 60)
  echo "пропуски (сломалось, гейт молчал): за 30 дн — $n; последний: $last"
fi

exit 0
