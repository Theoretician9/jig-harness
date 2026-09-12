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
hook_source=$(cat 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("source") or "")' 2>/dev/null || echo "")
if [ "$hook_source" = "compact" ]; then
  echo "=== после компакта ==="
  echo "Контекст только что пережат — решения могли потеряться. Сверься с памятью"
  echo "(память/MEMORY.md) и передачей (docs/handover/SESSION-HANDOFF-*.md); если"
  echo "передача отстала от сделанного — обнови её сейчас."
  echo "Второй компакт подряд запрещён — вместо него ротация сессии (session-warden скомандует)."
  exit 0
fi

conf_path="${HARNESS_INSTALL_CONF:-/etc/harness/install.conf}"
project_name="проект"
project_dir=""
autonomy="semi"
heartbeat_dir="/var/lib/harness/heartbeat"
log_dir="/var/log/harness"

if [ -r "$conf_path" ]; then
  # Окружение старше конфига: общий загрузчик вместо голого source (улика
  # 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
  # shellcheck disable=SC1091
  source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../lib/konf.sh"
  konf_zagruzit
  project_name="${PROJECT_NAME:-$project_name}"
  project_dir="${PROJECT_DIR:-}"
  autonomy="${AUTONOMY:-semi}"
  heartbeat_dir="${HEARTBEAT_DIR:-$heartbeat_dir}"
  log_dir="${LOG_DIR:-$log_dir}"
else
  echo "⚠ конфиг $conf_path недоступен — работаю со значениями по умолчанию"
fi

# Резка по СИМВОЛАМ, не байтам. `cut -c` считает байты и рвёт кириллицу
# посередине: таблица скилов едет в контекст каждой сессии, и пять её строк
# приходили с обрывком вместо буквы (разбор кода 11.09.2026, пункт 11).
обрезать() { # $1 = сколько символов; текст со stdin
    LC_ALL=C.UTF-8 awk -v n="$1" '{print substr($0, 1, n)}'
}

echo "=== $project_name: состояние на старте сессии ==="

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
  failed_units=$(systemctl list-units --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | tr '\n' ' ')
  if [ -n "${failed_units// /}" ]; then
    echo "⚠ упавшие systemd-юниты: $failed_units"
  else
    echo "systemd: упавших юнитов нет"
  fi
else
  echo "⚠ systemctl недоступен — состояние юнитов не проверено"
fi

# --- контейнеры (если docker есть) ---
if command -v docker >/dev/null 2>&1; then
  down=$( { docker ps --filter status=restarting --format '{{.Names}}(restarting)' 2>/dev/null; \
           docker ps -a --filter status=exited --format '{{.Names}}(exited)' 2>/dev/null; } | tr '\n' ' ')
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

skills_dir="$project_dir/харнес/skills"
if [ -d "$skills_dir" ]; then
  # Каким языком писать владельцу — считается по ЕГО сообщениям (владелец
# 11.09.2026: «Твой язык должен подстраиваться к моему поведению»). Печатается
# здесь, потому что правило, которое не видно в начале хода, — не правило.
LANG_LEVEL=$(python3 "$project_dir/scripts/uroven-yazyka.py" 2>/dev/null || echo "обычный")
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
  # Действующие указания живут отдельно и грузятся всегда: память/УКАЗАНИЯ.md.
  # Обрезка байтами (head -c) + iconv -c: `cut -c` считает БАЙТЫ и рвёт
  # кириллицу посреди буквы (тот же случай, что в vorota.sh); iconv
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

# Очередь и ПОРЯДОК — одним вызовом: карта весит 390 КБ, и разбирать её дважды
# ради двух строк значит платить 0,8 с на каждом старте сессии.
queue_tool="$project_dir/scripts/ochered-raboty.py"
if [ -f "$queue_tool" ] && [ -f "$project_dir/dev-map.yaml" ]; then
  queue_out=$(python3 "$queue_tool" "$project_dir/dev-map.yaml" --порядок 2>/dev/null)
  echo "очередь работы: $(printf '%s\n' "$queue_out" | head -1)"
  # «Сейчас делаем» — ответ на вопрос, который иначе решается в голове: место
  # задачи назначает код (владелец 12.09.2026 — «это должно контролироваться
  # кодом и быть безотказным»).
  printf '%s\n' "$queue_out" | grep -m1 '^сейчас делаем:' || true
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
       "свежие: bash $project_dir/харнес/demons/tokens-collector.sh"
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
