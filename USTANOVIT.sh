#!/usr/bin/env bash
# USTANOVIT.sh — установщик харнеса одним скриптом (шаги 0–9 из 01-SPEC-УСТАНОВКА.md).
#
# Что делает: под root проходит спеку установки целиком — install.conf, пользователь
#   agent + каталоги + sudoers, apt-пакеты + node, Claude Code с фиксацией версии,
#   юнит harness-agent, секреты, settings.json + переменные качества, раскладка
#   харнеса (razlozhit-harnes.sh от agent, с доставкой UNIFIED), harness.conf,
#   канал Telegram (вариант Б — свой диспетчер, дефолт и единственный
#   поддерживаемый: юнит harness-dispatcher; вариант А — telegram-плагин,
#   ЭКСПЕРИМЕНТ после приёмки: bun + .env плагина),
#   crontab демонов точно по таблице §9 и прогон каждого демона в `env -i`.
#   Каждый шаг: заголовок → команды → проверка из 01-SPEC; провал проверки —
#   стоп с именем шага и командой диагностики. Прогоны демонов не прерывают
#   установку — провалы собираются в сводку в конце.
# Принцип владельца: всё, что можно сделать кодом, — код; человеку остаётся
#   только физически неавтоматизируемое (OAuth-вход, сопряжение бота, первый
#   промпт) — список печатается в конце блоком «ОСТАЛОСЬ РУКАМИ».
# Запуск: sudo bash USTANOVIT.sh   (из каталога пакета: рядом harness/,
#   рядом или уровнем выше — UNIFIED/)
#   --dry-run : печатает каждый шаг и команды, ничего не исполняет (root не нужен)
#   --resume  : пропускает шаги с отметкой о выполнении; и без него скрипт
#               идемпотентен — повторный запуск не ломает живое
# Интерактив: все вопросы — ОДНИМ блоком в начале (имя проекта, каталоги,
#   chat_id, токен, вариант канала); если /etc/harness/install.conf уже
#   заполнен — значения берутся оттуда, вопросы пропускаются.
# Чем проверяется: bash -n и полный прогон --dry-run (печатает все шаги, exit 0).
#   Живой смоук в изоляции невозможен (apt, systemd, сеть) — установка
#   ДОКАЗЫВАЕТСЯ прогоном на чистом VPS по ЧЕК-ЛИСТ-ПРИЁМКИ.md, раздел А.

set -Eeuo pipefail

# ---------------------------------------------------------------- обвязка ----

# trap ERR: set -e не имеет права ронять установку молча. Любая упавшая
# команда печатает имя текущего шага, саму команду и подсказку про --resume.
CURRENT_STEP="до шагов"
CURRENT_CMD=""
trap 'rc=$?;
  cmd="$BASH_COMMAND"
  case "$cmd" in eval*) cmd="${CURRENT_CMD:-$cmd}" ;; esac
  printf "\nОШИБКА на шаге «%s» (код %s), упавшая команда:\n  %s\nПочини причину и запусти с --resume: sudo bash %s --resume\n" \
    "$CURRENT_STEP" "$rc" "$cmd" "${BASH_SOURCE[0]}" >&2' ERR

DRY=0
RESUME=0
SELFTEST=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --resume)  RESUME=1 ;;
    --selftest) SELFTEST=1 ;;
    *) echo "неизвестный флаг: $arg (допустимы --dry-run, --resume, --selftest)" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_SRC="$SCRIPT_DIR/harness"
CONF="${HARNESS_INSTALL_CONF:-/etc/harness/install.conf}"
STAMP_DIR=/var/lib/harness/install-steps

# ── Проверка ответов-путей: значение проверяется ТАМ, ГДЕ ВХОДИТ ─────────────
# Улика 11.08.2026, живая установка: на вопрос «каталог кода» пришло
# «СТАРТОВ/var/www/проект» (склеенная вставка в терминал). Установщик
# принял ответ без единой проверки, и кривой путь доехал до systemd:
#   mkdir -p создал каталог ОТНОСИТЕЛЬНО текущего — успешно;
#   chown прошёл — успешно;
#   проверка шага 1 «каталоги принадлежат агенту» ПОЗЕЛЕНЕЛА (из того же cwd);
#   и только systemd отбил юнит общим словом «bad unit file setting».
# Три зелёные проверки на кривом значении — потому что каждая проверяла ФАКТ
# (каталог есть, владелец тот), а не СВОЙСТВО (путь абсолютный).
#
# Отсюда два правила, и оба здесь кодом:
#   1. ответ проверяется в момент ввода, с внятной причиной отказа;
#   2. значение проверяется ЕЩЁ РАЗ при каждом запуске, до первого шага, —
#      потому что при --resume вопросы не задаются вовсе, а конфиг мог быть
#      поправлен руками (в том числе неверно).
путь_годен() { # $1 = путь; печатает причину отказа в stdout, rc=1 при отказе
  local p="$1"
  [[ -n "$p" ]]                  || { printf 'путь пуст\n'; return 1; }
  [[ "$p" == /* ]]               || { printf 'путь должен быть АБСОЛЮТНЫМ (начинаться со «/»): «%s». Именно на этом установка встала 11.08: относительный путь дошёл до systemd\n' "$p"; return 1; }
  [[ "$p" != "/" ]]              || { printf 'корень «/» каталогом проекта быть не может\n'; return 1; }
  [[ "$p" != *[[:space:]]* ]]    || { printf 'в пути есть пробел или таб — это почти всегда склеенная вставка: «%s»\n' "$p"; return 1; }
  [[ "$p" != *..* ]]             || { printf 'в пути есть «..» — путь должен быть прямым: «%s»\n' "$p"; return 1; }
  case "$p" in
    *'$'*|*'`'*|*'"'*|*"'"*|*'|'*|*';'*|*'&'*)
      printf 'в пути есть символ оболочки ($ ` " '"'"' | ; &) — так путь не пишут: «%s»\n' "$p"; return 1 ;;
  esac
  return 0
}

нормализовать_путь() { # хвостовые слэши прочь: /opt/x/ и /opt/x — один каталог
  local p="$1"
  while [[ "$p" == */ && "$p" != "/" ]]; do p="${p%/}"; done
  printf '%s\n' "$p"
}

спросить_путь() { # $1 = текст вопроса, $2 = значение по умолчанию; печатает путь
  local prompt="$1" default="$2" ans reason
  while :; do
    read -r -p "  $prompt [$default]: " ans
    ans="$(нормализовать_путь "${ans:-$default}")"
    if reason=$(путь_годен "$ans"); then printf '%s\n' "$ans"; return 0; fi
    printf '      %s\n' "$reason" >&2
    printf '      введите заново (пример: %s)\n' "$default" >&2
  done
}

сверить_пути_конфига() { # rc=1 + перечень: вызывается ДО первого шага, каждый запуск
  local bad=0 name value reason
  for name in PROJECT_DIR SECRETS_DIR LOG_DIR HEARTBEAT_DIR; do
    value="${!name:-}"
    if ! reason=$(путь_годен "$value"); then
      printf '  %s: %s\n' "$name" "$reason"
      bad=1
    fi
  done
  return "$bad"
}

проверить_юнит() { # $1 = имя шага, $2 = путь к unit-файлу
  # systemd-analyze verify ДО enable: иначе systemd отвечает общим
  # «has a bad unit file setting», и причину приходится искать в журнале.
  # Пропуск с адресом, а не молчание: на машине без systemd-analyze проверка
  # честно объявляется пропущенной.
  local step="$1" unit="$2" out
  if (( DRY )); then printf '  + systemd-analyze verify %s\n' "$unit"; return 0; fi
  if ! command -v systemd-analyze >/dev/null 2>&1; then
    printf '  ПРОПУСК: systemd-analyze нет — юнит не выверен до включения\n'
    printf '     адрес: apt-get install -y systemd; затем %s --resume\n' "${BASH_SOURCE[0]}"
    return 0
  fi
  printf '  проверка: systemd-analyze verify %s\n' "$unit"
  # verify пишет замечания в stderr и при этом часто возвращает 0 —
  # смотрим на ТЕКСТ, а не только на код: иначе проверка врёт зелёным.
  # Только про НАШ файл: verify тянет зависимости и жалуется на посторонние
  # юниты машины (улика 12.08.2026 — вечное «pure-ftpd: PIDFile ... legacy
  # directory»). Без отбора установка вставала бы из-за чужого юнита.
  out=$(systemd-analyze verify "$unit" 2>&1 | grep -F "$unit:" || true)
  if [[ -n "$out" ]]; then
    printf '%s\n' "$out" | sed 's/^/    | /'
    стоп "$step" "systemd забраковал юнит ДО включения (текст выше)" \
      "systemd-analyze verify $unit; grep -n 'WorkingDirectory\|ExecStart' $unit"
  fi
  printf '  OK: юнит выверен systemd до включения\n'
  CHECKED_LIST+=("$step: systemd-analyze verify $unit — без замечаний")
}

# сводка для финала
DONE_LIST=()      # что поставлено
CHECKED_LIST=()   # что проверено
FAILED_LIST=()    # что упало (не остановив установку)

заголовок() { printf '\n==================== %s ====================\n' "$*"; }
скажи()     { printf '%s\n' "$*"; }

стоп() { # стоп "имя шага" "что не так" "команда диагностики"
  printf '\nСТОП на шаге «%s»: %s\n' "$1" "$2" >&2
  printf 'Диагностика: %s\n' "$3" >&2
  printf 'После починки перезапустите: sudo bash %s --resume\n' "${BASH_SOURCE[0]}" >&2
  exit 1
}

делай() { # печатает команду; в живом режиме исполняет (eval — ради пайпов/редиректов)
  printf '  + %s\n' "$1"
  CURRENT_CMD="$1"   # для trap ERR: печатать саму команду, а не «eval "$1"»
  if (( ! DRY )); then eval "$1"; fi
}

делай_тихо() { # исполняет без печати команды (для команд с секретами печатаем легенду)
  printf '  + %s\n' "$1"
  CURRENT_CMD="$1 (реальная команда скрыта — секрет)"
  if (( ! DRY )); then eval "$2"; fi
}

проверка() { # проверка "имя шага" "описание" "команда-проверка" "команда диагностики"
  printf '  проверка: %s\n' "$3"
  if (( DRY )); then return 0; fi
  if eval "$3" >/dev/null 2>&1; then
    printf '  OK: %s\n' "$2"
    CHECKED_LIST+=("$1: $2")
  else
    стоп "$1" "провалена проверка: $2" "$4"
  fi
}

записать_файл() { # записать_файл путь права владелец  (содержимое — stdin)
  local path="$1" mode="$2" owner="$3" content
  content="$(cat)"
  printf '  + записать файл %s (права %s, владелец %s):\n' "$path" "$mode" "$owner"
  printf '%s\n' "$content" | sed 's/^/    | /'
  if (( ! DRY )); then
    mkdir -p "$(dirname "$path")"
    printf '%s\n' "$content" > "$path"
    chmod "$mode" "$path"
    chown "$owner" "$path"
  fi
}

отметить() { (( DRY )) || { mkdir -p "$STAMP_DIR"; touch "$STAMP_DIR/$1.done"; }; }
уже_сделан() { (( RESUME )) && [[ -f "$STAMP_DIR/$1.done" ]]; }

# ── сверка со спекой (П-1 ревью №2): спека и установщик расходятся молча ────
# Классовая починка: crontab шага 9 сверяется НЕ с собственной копией таблицы,
# а с таблицей §9 самой спеки — она лежит рядом с установщиком. Функции
# самодостаточны (printf, не скажи): их гоняет изолированный тест.

демоны_из_спеки() { # путь к системные-задания.yaml → имена демонов, по одному в строке
  # Состав системы — ДАННЫЕ, а не проза. Прежде список вынимался awk-ом из §9
  # спеки установки, и спека отстала молча: 10 имён против 13 в данных
  # (замер 11.09.2026 — не было obnovlenie-watch, pamyat-kommit и
  # memory-revision-порог). Сверка с отставшим списком — это сверка ни с чем.
  python3 -c '
import sys, yaml
д = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
for з in (д.get("задания") or []):
    имя = з.get("имя") or ""
    if имя and not имя.endswith("-порог"):
        print(имя)
' "$1" 2>/dev/null | sort -u
}

сверить_демонов_со_спекой() { # $1 = реестр; строки crontab — на stdin
  # rc=1 — расхождение; rc=2 — сверить НЕЧЕМ (нет python3 с yaml). Два разных
  # исхода: на ЧИСТОЙ машине питон появляется только на шаге 2, и сухой прогон
  # до установки падал «сверка невозможна» — то есть СТОП там, где человек
  # всего лишь смотрит, что установщик собирается делать (живой прогон в
  # контейнере ubuntu:24.04, 12.09.2026). Плюс прежний текст врал про «блок §9
  # спеки»: список давно берётся из данных, а не из прозы.
  local spec="$1" spec_names cron_names missing extra
  if ! python3 -c 'import yaml' >/dev/null 2>&1; then
    printf '  сверить нечем: нет python3 с модулем yaml (ставится шагом 2) — сверка отложена\n'
    return 2
  fi
  spec_names=$(демоны_из_спеки "$spec")
  if [[ -z "$spec_names" ]]; then
    printf '  реестр %s не дал ни одного имени задания — сверка невозможна\n' "$spec"
    return 1
  fi
  cron_names=$(grep -oE '/[A-Za-z0-9_-]+\.sh([[:space:]]|$)' | sed 's|^/||; s|\.sh[[:space:]]*$||; s|\.sh$||' | sort -u)
  missing=$(comm -23 <(printf '%s\n' "$spec_names") <(printf '%s\n' "$cron_names"))
  extra=$(comm -13 <(printf '%s\n' "$spec_names") <(printf '%s\n' "$cron_names"))
  if [[ -n "$missing" || -n "$extra" ]]; then
    [[ -n "$missing" ]] && printf '  РАСХОЖДЕНИЕ со спекой §9 — в crontab НЕТ демонов: %s\n' "$(printf '%s' "$missing" | tr '\n' ' ')"
    [[ -n "$extra"   ]] && printf '  РАСХОЖДЕНИЕ со спекой §9 — в crontab ЛИШНИЕ демоны: %s\n' "$(printf '%s' "$extra" | tr '\n' ' ')"
    return 1
  fi
  return 0
}

# ------------------------------------------------------- селфтест проверок ----
# Проверяется то, что доказуемо без сервера: разбор ответов-путей. Живая часть
# (apt, systemd, OAuth) доказывается прогоном по ЧЕК-ЛИСТ-ПРИЁМКИ.md, раздел А.
if (( SELFTEST )); then
  ok_flag=1
  проба_пути() { # $1 = ожидание (годен|плохо), $2 = путь, $3 = имя случая
    local want="$1" p="$2" name="$3" rc=0
    путь_годен "$p" >/dev/null || rc=1
    if [[ "$want" == годен && $rc -eq 0 ]] || [[ "$want" == плохо && $rc -eq 1 ]]; then
      printf '  ок    %s\n' "$name"
    else
      printf '  ПЛОХО %s (путь «%s»)\n' "$name" "$p"; ok_flag=0
    fi
  }
  # БОЛЬНЫЕ СЛУЧАИ — первым делом тот, на котором установка встала живьём
  проба_пути плохо 'СТАРТОВ/var/www/проект' 'улика 11.08: склеенная вставка, путь относительный'
  проба_пути плохо 'var/www/app'        'относительный путь'
  проба_пути плохо ''                   'пустой ответ'
  проба_пути плохо '/'                  'корень как каталог проекта'
  проба_пути плохо '/opt/my app'        'пробел в пути'
  проба_пути плохо '/opt/../etc'        '«..» в пути'
  проба_пути плохо '/opt/$HOME/x'       'подстановка оболочки в пути'
  проба_пути плохо '/opt/x;rm -rf /'    'точка с запятой в пути'
  # ЗДОРОВЫЕ
  проба_пути годен '/opt/myproject'                'обычный абсолютный путь'
  проба_пути годен '/var/www/проект'               'то, что имелось в виду живьём'
  проба_пути годен '/etc/myproject/secrets'        'каталог секретов'
  проба_пути годен '/home/хозяин/проект-по-русски' 'кириллица в пути законна (в пакете есть harness/)'
  # нормализация хвостовых слэшей
  [[ "$(нормализовать_путь '/opt/x///')" == '/opt/x' ]] \
    && printf '  ок    хвостовые слэши срезаны\n' \
    || { printf '  ПЛОХО хвостовые слэши не срезаны\n'; ok_flag=0; }
  [[ "$(нормализовать_путь '/')" == '/' ]] \
    && printf '  ок    корень нормализацией не съеден\n' \
    || { printf '  ПЛОХО корень съеден нормализацией\n'; ok_flag=0; }
  # сверка конфига: кривое значение ловится ДО первого шага
  PROJECT_DIR='СТАРТОВ/var/www/x' SECRETS_DIR=/etc/x LOG_DIR=/var/log/x HEARTBEAT_DIR=/var/lib/x \
    сверить_пути_конфига >/dev/null && { printf '  ПЛОХО кривой PROJECT_DIR в конфиге не пойман\n'; ok_flag=0; } \
    || printf '  ок    кривой путь в конфиге ловится до первого шага\n'
  PROJECT_DIR=/opt/x SECRETS_DIR=/etc/x LOG_DIR=/var/log/x HEARTBEAT_DIR=/var/lib/x \
    сверить_пути_конфига >/dev/null && printf '  ок    здоровый конфиг проходит\n' \
    || { printf '  ПЛОХО здоровый конфиг забракован\n'; ok_flag=0; }
  (( ok_flag )) && { echo "SELFTEST: зелёный (16 путей, включая улику живой установки 11.08)"; exit 0; }
  exit 1
fi

# ------------------------------------------------------------ предпосылки ----

if (( ! DRY )) && [[ "$(id -u)" != "0" ]]; then
  скажи "нужен root: запустите  sudo bash ${BASH_SOURCE[0]}" >&2
  exit 1
fi
[[ -d "$HARNESS_SRC" ]] || { скажи "рядом со скриптом нет каталога harness/ — запускайте из каталога STARTER-PACKAGE" >&2; exit 1; }
[[ -f "$HARNESS_SRC/razlozhit-harnes.sh" ]] || { скажи "нет $HARNESS_SRC/razlozhit-harnes.sh — пакет неполный" >&2; exit 1; }

UNIFIED_SRC=""
for cand in "$SCRIPT_DIR/../UNIFIED" "$SCRIPT_DIR/UNIFIED" "$SCRIPT_DIR/../../UNIFIED"; do
  [[ -d "$cand" ]] && UNIFIED_SRC="$(cd "$cand" && pwd)" && break
done
[[ -n "$UNIFIED_SRC" ]] || { скажи "каталог UNIFIED не найден рядом с пакетом (искал: ../UNIFIED, ./UNIFIED, ../../UNIFIED) — доставка глав обязательна (01-SPEC §7-а)" >&2; exit 1; }

# ------------------------------------------- опрос: все вопросы одним блоком ----

PROJECT_NAME=""; PROJECT_DIR=""; SECRETS_DIR=""; TG_CHAT_ID=""; BOT_TOKEN=""
AGENT_USER="agent"; AUTONOMY="semi"; OWNER_TZ=""
HEARTBEAT_DIR="/var/lib/harness/heartbeat"; LOG_DIR="/var/log/harness"
# Канал: Б (свой диспетчер) — дефолт и единственный поддерживаемый;
# А (telegram-плагин) — эксперимент, включать только после приёмки (решение владельца).
TG_TOKEN_FILE=""; CC_VERSION=""; TG_CHANNEL_VARIANT="B"

if [[ -r "$CONF" ]]; then
  # shellcheck source=/dev/null
  source "$CONF"
fi

опрос() {
  заголовок "ОПРОС (единственный интерактив; дальше скрипт идёт без остановок)"
  if [[ -n "${PROJECT_NAME:-}" && -n "${PROJECT_DIR:-}" && -n "${SECRETS_DIR:-}" && -n "${TG_CHAT_ID:-}" ]]; then
    скажи "  $CONF уже заполнен — вопросы пропущены, значения оттуда:"
    скажи "  PROJECT_NAME=$PROJECT_NAME PROJECT_DIR=$PROJECT_DIR SECRETS_DIR=$SECRETS_DIR TG_CHAT_ID=$TG_CHAT_ID вариант канала=$TG_CHANNEL_VARIANT"
    TG_TOKEN_FILE="${TG_TOKEN_FILE:-$SECRETS_DIR/tg_bot_token}"
    OWNER_TZ="${OWNER_TZ:-Etc/UTC}"
    if [[ ! -s "${TG_TOKEN_FILE}" ]]; then
      if (( DRY )); then
        скажи "  (dry-run: токен бота был бы запрошен — файл $TG_TOKEN_FILE пуст)"
      else
        read -r -s -p "  токен бота от BotFather (вставьте, ввод скрыт): " BOT_TOKEN; echo
        [[ -n "$BOT_TOKEN" ]] || { скажи "токен пуст, а $TG_TOKEN_FILE отсутствует — без него канал не собрать"; exit 1; }
      fi
    fi
    return 0
  fi
  if (( DRY )); then
    PROJECT_NAME="${PROJECT_NAME:-myproject}"
    PROJECT_DIR="${PROJECT_DIR:-/opt/$PROJECT_NAME}"
    SECRETS_DIR="${SECRETS_DIR:-/etc/$PROJECT_NAME/secrets}"
    TG_CHAT_ID="${TG_CHAT_ID:-100000001}"
    BOT_TOKEN="demo"
    OWNER_TZ="${OWNER_TZ:-Etc/UTC}"
    TG_TOKEN_FILE="$SECRETS_DIR/tg_bot_token"
    скажи "  (dry-run: конфига нет, вопросы не задаю — подставлены демо-значения)"
    скажи "  PROJECT_NAME=$PROJECT_NAME PROJECT_DIR=$PROJECT_DIR SECRETS_DIR=$SECRETS_DIR TG_CHAT_ID=$TG_CHAT_ID вариант канала=$TG_CHANNEL_VARIANT"
    return 0
  fi
  while :; do
    read -r -p "  1/7 имя проекта латиницей (напр. myproject): " PROJECT_NAME
    [[ "$PROJECT_NAME" =~ ^[A-Za-z0-9_-]+$ ]] && break
    скажи "      только латиница/цифры/дефис/подчёркивание"
  done
  PROJECT_DIR=$(спросить_путь "2/7 каталог кода" "/opt/$PROJECT_NAME")
  SECRETS_DIR=$(спросить_путь "3/7 каталог секретов" "/etc/$PROJECT_NAME/secrets")
  while :; do
    read -r -p "  4/7 TG_CHAT_ID владельца (число; узнать: написать боту @userinfobot): " TG_CHAT_ID
    [[ "$TG_CHAT_ID" =~ ^-?[0-9]+$ ]] && break
    скажи "      chat_id — это число"
  done
  while :; do
    read -r -s -p "  5/7 токен бота от BotFather (вставьте, ввод скрыт): " BOT_TOKEN; echo
    [[ -n "$BOT_TOKEN" ]] && break
    скажи "      токен пустой — без него канала не будет"
  done
  read -r -p "  6/7 вариант канала: Б — свой диспетчер (поддерживается) / А — telegram-плагин (эксперимент, после приёмки) [Б]: " ans
  case "${ans:-Б}" in
    [AaАа])
      TG_CHANNEL_VARIANT="A"
      скажи "      ВНИМАНИЕ: вариант А — экспериментальный, механика подтверждений слабее;"
      скажи "      пункт чек-листа про юнит harness-dispatcher будет КРАСНЫМ (спека: до приёмки А не включать);"
      скажи "      поддерживаемый путь — Б, вариант А пробуйте только после приёмки."
      ;;
    *) TG_CHANNEL_VARIANT="B" ;;
  esac
  # Пояс ВЛАДЕЛЬЦА, не датацентра: по нему считаются расписания демонов, иначе
  # «ночные» работы идут у него днём (улика 12.08.2026 — сервер в UTC, владелец
  # на +5: «утренняя» сводка приходила в 13:00, ночной бэкап — в 9 утра).
  # Подтянуть неоткуда: по адресу сервера определяется пояс датацентра, а пояс
  # собеседника мессенджер не отдаёт. Значит — спросить, как остальный паспорт.
  while :; do
    read -r -p "  7/7 часовой пояс ВЛАДЕЛЬЦА (напр. Asia/Yekaterinburg; список: timedatectl list-timezones) [Etc/UTC]: " OWNER_TZ
    OWNER_TZ="${OWNER_TZ:-Etc/UTC}"
    [[ -f "/usr/share/zoneinfo/$OWNER_TZ" ]] && break
    скажи "      такого пояса нет в /usr/share/zoneinfo — проверьте написание"
  done
  TG_TOKEN_FILE="$SECRETS_DIR/tg_bot_token"
  скажи ""
  скажи "  принято: PROJECT_NAME=$PROJECT_NAME PROJECT_DIR=$PROJECT_DIR SECRETS_DIR=$SECRETS_DIR"
  скажи "           TG_CHAT_ID=$TG_CHAT_ID TG_TOKEN_FILE=$TG_TOKEN_FILE вариант канала=$TG_CHANNEL_VARIANT"
  скажи "  дальше вопросов не будет."
}

# --------------------------------------------------------------- шаги 0–9 ----

шаг_0() { # install.conf — единое место значений (01-SPEC §0)
  local step="шаг 0 — install.conf"; CURRENT_STEP="$step"
  уже_сделан шаг_0 && { скажи "[$step] пропуск (--resume)"; return 0; }
  заголовок "$step"
  записать_файл "$CONF" 644 root:root <<EOF
# Паспорт установки. Заполняется при установке, см. 00-ВВОДНЫЕ.md (§5).
PROJECT_NAME="$PROJECT_NAME"       # имя проекта латиницей
PROJECT_DIR="$PROJECT_DIR"         # каталог кода
AGENT_USER="$AGENT_USER"           # системный пользователь агента
CC_VERSION="$CC_VERSION"           # версия Claude Code, фиксируется на шаге 3
SECRETS_DIR="$SECRETS_DIR"         # ВНЕ вебрута и репо
TG_TOKEN_FILE="$TG_TOKEN_FILE"     # файл с токеном бота
TG_CHAT_ID="$TG_CHAT_ID"           # chat_id владельца
AUTONOMY="$AUTONOMY"               # semi | full — режим из 00-ВВОДНЫЕ §1.2; данные, не код
OWNER_TZ="$OWNER_TZ"               # часовой пояс ВЛАДЕЛЬЦА: по нему живёт сервер и расписания демонов
HEARTBEAT_DIR="$HEARTBEAT_DIR"
LOG_DIR="$LOG_DIR"
TG_CHANNEL_VARIANT="$TG_CHANNEL_VARIANT"  # A — telegram-плагин | B — свой диспетчер (01-SPEC §7)
EOF
  # Проверка смотрит на СВОЙСТВО, а не только на факт «непусто»: пустой и
  # относительный путь одинаково не годятся, и второе уже стоило установки.
  проверка "$step" "конфиг читается, все пути абсолютны" \
    "bash -n $CONF && source $CONF && [ -n \"\$PROJECT_NAME\" ] && [ -n \"\$TG_CHAT_ID\" ] && case \"\$PROJECT_DIR\$SECRETS_DIR\$LOG_DIR\$HEARTBEAT_DIR\" in *' '*) false ;; esac && [ \"\${PROJECT_DIR:0:1}\" = / ] && [ \"\${SECRETS_DIR:0:1}\" = / ] && [ \"\${LOG_DIR:0:1}\" = / ] && [ \"\${HEARTBEAT_DIR:0:1}\" = / ] && echo OK" \
    "bash -n $CONF; grep -n 'DIR' $CONF"
  # Время сервера — по поясу ВЛАДЕЛЬЦА, а не датацентра: расписания демонов
  # считаются по времени машины, и на сервере в UTC «утренняя» сводка приходит
  # владельцу из UTC+5 в 13:00 (улика 12.08.2026). cron читает пояс при старте —
  # без перезапуска расписания остались бы в прежнем.
  if [[ "$OWNER_TZ" != "$(timedatectl show -p Timezone --value 2>/dev/null)" ]]; then
    делай "timedatectl set-timezone '$OWNER_TZ'"
    делай "systemctl restart cron 2>/dev/null || systemctl restart crond 2>/dev/null || true"
  fi
  проверка "$step" "системное время идёт по поясу владельца" \
    "[ \"\$(timedatectl show -p Timezone --value)\" = '$OWNER_TZ' ]" \
    "timedatectl; grep -n OWNER_TZ $CONF"
  DONE_LIST+=("install.conf: $CONF (644)")
  DONE_LIST+=("часовой пояс сервера: $OWNER_TZ (пояс владельца)")
  отметить шаг_0
}

шаг_1() { # пользователь, права, служебные каталоги (01-SPEC §1)
  local step="шаг 1 — пользователь и каталоги"; CURRENT_STEP="$step"
  уже_сделан шаг_1 && { скажи "[$step] пропуск (--resume)"; return 0; }
  заголовок "$step"
  делай "id -u $AGENT_USER >/dev/null 2>&1 || adduser --disabled-password --gecos '' $AGENT_USER"
  делай "mkdir -p '$PROJECT_DIR' '$LOG_DIR' '$HEARTBEAT_DIR' /var/backups/harness"
  делай "chown -R $AGENT_USER:$AGENT_USER '$PROJECT_DIR' '$LOG_DIR' '$HEARTBEAT_DIR' /var/backups/harness"
  записать_файл /etc/sudoers.d/50-agent 440 root:root <<EOF
$AGENT_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart harness-agent, /usr/bin/systemctl status harness-agent
EOF
  проверка "$step" "sudoers корректен (visudo -cf)" \
    "visudo -cf /etc/sudoers.d/50-agent" \
    "visudo -cf /etc/sudoers.d/50-agent"
  проверка "$step" "пользователь агента отвечает" \
    "[ \"\$(sudo -u $AGENT_USER whoami)\" = '$AGENT_USER' ]" \
    "sudo -u $AGENT_USER whoami; id $AGENT_USER"
  проверка "$step" "каталоги принадлежат агенту" \
    "[ \"\$(stat -c '%U' '$PROJECT_DIR' '$LOG_DIR' '$HEARTBEAT_DIR' /var/backups/harness | sort -u)\" = '$AGENT_USER' ]" \
    "stat -c '%U %n' '$PROJECT_DIR' '$LOG_DIR' '$HEARTBEAT_DIR' /var/backups/harness"
  DONE_LIST+=("пользователь $AGENT_USER, каталоги, sudoers (только 2 systemctl), /var/backups/harness")
  отметить шаг_1
}

шаг_2() { # базовые пакеты + node (01-SPEC §2)
  local step="шаг 2 — apt-пакеты и node"; CURRENT_STEP="$step"
  уже_сделан шаг_2 && { скажи "[$step] пропуск (--resume)"; return 0; }
  заголовок "$step"
  делай "DEBIAN_FRONTEND=noninteractive apt-get update"
  делай "DEBIAN_FRONTEND=noninteractive apt-get install -y git tmux curl jq python3 python3-pip python3-venv python3-yaml shellcheck ripgrep unzip"
  делай "if command -v node >/dev/null 2>&1 && [ \"\$(node --version | sed 's/v//' | cut -d. -f1)\" -ge 20 ]; then echo 'node уже >=20 — nodesource пропущен'; else curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs; fi"
  проверка "$step" "все инструменты отвечают версией, node >= 20" \
    "git --version && tmux -V && python3 --version && jq --version && node --version && [ \"\$(node --version | sed 's/v//' | cut -d. -f1)\" -ge 20 ]" \
    "git --version; tmux -V; python3 --version; jq --version; node --version"
  DONE_LIST+=("apt: git tmux curl jq python3(+pip,venv,yaml) shellcheck ripgrep unzip; node LTS")
  отметить шаг_2
}

шаг_3() { # Claude Code: версия зафиксирована (01-SPEC §3); OAuth-вход — руками, в финале
  local step="шаг 3 — Claude Code"; CURRENT_STEP="$step"
  уже_сделан шаг_3 && { скажи "[$step] пропуск (--resume)"; return 0; }
  заголовок "$step"
  if (( DRY )); then
    скажи "  + V=\$(npm view @anthropic-ai/claude-code version)  # актуальная на момент установки"
    скажи "  + sed -i 's/^CC_VERSION=.*/CC_VERSION=\"\$V\"/' $CONF  # фиксация в конфиг"
    скажи "  + npm install -g \"@anthropic-ai/claude-code@\$V\""
    скажи "  проверка: [ \"\$(claude --version | grep -o '[0-9][0-9.]*' | head -1)\" = \"\$V\" ]"
  else
    local V
    if [[ -n "${CC_VERSION:-}" ]]; then
      V="$CC_VERSION"
      скажи "  + CC_VERSION уже зафиксирована в конфиге: $V — ставится строго она"
    else
      # npm view зовётся РОВНО один раз: два вызова могли бы вернуть разные
      # версии (гонка с публикацией) — зафиксировали бы не то, что поставили.
      скажи "  + V=\$(npm view @anthropic-ai/claude-code version)"
      CURRENT_CMD="npm view @anthropic-ai/claude-code version"
      V=$(npm view @anthropic-ai/claude-code version)
      скажи "    версия: $V"
      CC_VERSION="$V"
    fi
    делай "sed -i 's/^CC_VERSION=.*/CC_VERSION=\"$V\"/' $CONF"
    делай "npm install -g '@anthropic-ai/claude-code@$V'"
    проверка "$step" "claude --version = CC_VERSION ($V)" \
      "[ \"\$(claude --version | grep -o '[0-9][0-9.]*' | head -1)\" = '$V' ]" \
      "claude --version; grep CC_VERSION $CONF"
    DONE_LIST+=("Claude Code $V (версия зафиксирована в install.conf)")
  fi
  скажи "  ВНИМАНИЕ: OAuth-вход по подписке — физически ручной шаг; команда в блоке «ОСТАЛОСЬ РУКАМИ»."
  отметить шаг_3
}

шаг_4() { # юнит harness-agent (01-SPEC §4; Environment качества — по §6)
  local step="шаг 4 — юнит harness-agent"; CURRENT_STEP="$step"
  уже_сделан шаг_4 && { скажи "[$step] пропуск (--resume)"; return 0; }
  заголовок "$step"
  записать_файл /etc/systemd/system/harness-agent.service 644 root:root <<EOF
[Unit]
Description=Harness agent tmux session
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=forking
User=$AGENT_USER
Environment=HOME=/home/$AGENT_USER
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Environment=CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1
Environment=CLAUDE_CODE_AUTO_COMPACT_WINDOW=800000
Environment=DISABLE_AUTOUPDATER=1
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/scripts/запустить-агента.sh
ExecStop=/usr/bin/tmux kill-session -t agent
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
  # always, не on-failure (улика 11.08.2026): агент выходит с кодом 0 — и при
  # ротации, и когда сессия завершилась сама. Для systemd это успех, и юнит
  # оставался лежать; сессия не возвращалась, пока её не поднимут руками.
  # StartLimitIntervalSec=0 — чтобы серия быстрых падений не заблокировала юнит
  # насовсем: заблокированный автоподъём хуже частого.
  проверить_юнит "$step" /etc/systemd/system/harness-agent.service
  делай "systemctl daemon-reload && systemctl enable --now harness-agent"
  проверка "$step" "harness-agent активен" \
    "[ \"\$(systemctl is-active harness-agent)\" = active ]" \
    "systemctl status harness-agent; journalctl -u harness-agent -n 30"
  проверка "$step" "tmux-сессия agent существует" \
    "sudo -u $AGENT_USER tmux ls | grep -q '^agent'" \
    "sudo -u $AGENT_USER tmux ls"
  # ── Сторож систем: второй носитель присмотра, независимый от cron ─────────
  # Улика 11.08.2026 (владелец): «tmux слетал полностью; надо, чтобы при любом
  # падении и после перезагрузки точно произошёл запуск и проверка всех систем».
  # Юнита harness-agent для этого мало: он поднимает сессию, но не отвечает на
  # вопрос «а присмотр-то бежит?». Демоны присмотра живут в cron — умер cron,
  # и молчат разом и сторож сессий, и утренняя сводка, которая о нём рассказала
  # бы. Sentinel живёт на systemd-таймере: два носителя не отказывают одинаково.
  записать_файл /etc/systemd/system/harness-sentinel.service 644 root:root <<EOF
[Unit]
Description=Harness sentinel: юниты живы, cron бежит

[Service]
Type=oneshot
User=$AGENT_USER
Environment=HOME=/home/$AGENT_USER
Environment=PATH=/usr/local/bin:/usr/bin:/bin
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/harness/demons/sentinel.sh
EOF
  записать_файл /etc/systemd/system/harness-sentinel.timer 644 root:root <<EOF
[Unit]
Description=Harness sentinel каждые 5 минут

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
EOF
  записать_файл /etc/systemd/system/harness-sentinel-boot.service 644 root:root <<EOF
[Unit]
Description=Harness sentinel: подъём агента и проверка всех систем после загрузки
Wants=network-online.target
After=network-online.target harness-agent.service harness-dispatcher.service

[Service]
Type=oneshot
User=$AGENT_USER
Environment=HOME=/home/$AGENT_USER
Environment=PATH=/usr/local/bin:/usr/bin:/bin
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/harness/demons/sentinel.sh --boot
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
EOF
  проверить_юнит "$step" /etc/systemd/system/harness-sentinel.service
  проверить_юнит "$step" /etc/systemd/system/harness-sentinel.timer
  проверить_юнит "$step" /etc/systemd/system/harness-sentinel-boot.service
  делай "systemctl daemon-reload && systemctl enable --now harness-sentinel.timer && systemctl enable harness-sentinel-boot.service"
  проверка "$step" "таймер сторожа систем взведён" \
    "systemctl is-active harness-sentinel.timer | grep -q active" \
    "systemctl status harness-sentinel.timer; systemctl list-timers harness-sentinel.timer"
  проверка "$step" "решения сторожа систем доказаны больными случаями" \
    "sudo -u $AGENT_USER bash $PROJECT_DIR/harness/demons/sentinel.sh --selftest | tail -1 | grep -q зелёный" \
    "sudo -u $AGENT_USER bash $PROJECT_DIR/harness/demons/sentinel.sh --selftest"
  скажи "  (тестовый reboot из §4 — обязателен при приёмке: после него harness-sentinel-boot присылает владельцу отчёт о проверке систем)"
  DONE_LIST+=("systemd-юнит harness-agent (tmux, автозапуск, Restart=always)")
  DONE_LIST+=("sentinel: таймер каждые 5 минут + проверка всех систем после загрузки")
  отметить шаг_4
}

шаг_5() { # секреты (01-SPEC §5)
  local step="шаг 5 — секреты"; CURRENT_STEP="$step"
  # Дыра resume: штамп есть, а файла токена нет или он пуст (например, стёрли
  # при починке) — пропускать нельзя, иначе канал не соберётся. Снимаем штамп
  # и перезаписываем шаг.
  if уже_сделан шаг_5 && [[ ! -s "$TG_TOKEN_FILE" ]]; then
    скажи "[$step] штамп стоит, но $TG_TOKEN_FILE пуст/отсутствует — снимаю штамп и переписываю шаг"
    rm -f "$STAMP_DIR/шаг_5.done"
  fi
  уже_сделан шаг_5 && { скажи "[$step] пропуск (--resume)"; return 0; }
  заголовок "$step"
  делай "mkdir -p '$SECRETS_DIR' && chown $AGENT_USER:$AGENT_USER '$SECRETS_DIR' && chmod 700 '$SECRETS_DIR'"
  if [[ -n "$BOT_TOKEN" ]]; then
    делай_тихо "printf '%s' 'токен-из-опроса (значение скрыто)' > $TG_TOKEN_FILE && chown $AGENT_USER:$AGENT_USER $TG_TOKEN_FILE && chmod 600 $TG_TOKEN_FILE" \
      "printf '%s' \"\$BOT_TOKEN\" > \"$TG_TOKEN_FILE\" && chown $AGENT_USER:$AGENT_USER \"$TG_TOKEN_FILE\" && chmod 600 \"$TG_TOKEN_FILE\""
  else
    скажи "  + токен не вводился — используется уже лежащий $TG_TOKEN_FILE"
    делай "chown $AGENT_USER:$AGENT_USER '$TG_TOKEN_FILE' && chmod 600 '$TG_TOKEN_FILE'"
  fi
  проверка "$step" "каталог секретов 700 $AGENT_USER" \
    "[ \"\$(stat -c '%a %U' '$SECRETS_DIR')\" = '700 $AGENT_USER' ]" \
    "stat -c '%a %U' '$SECRETS_DIR'"
  проверка "$step" "файл токена: 600 и непуст" \
    "[ \"\$(stat -c '%a' '$TG_TOKEN_FILE')\" = '600' ] && [ -s '$TG_TOKEN_FILE' ]" \
    "stat -c '%a %U' '$TG_TOKEN_FILE'; wc -c '$TG_TOKEN_FILE'"
  скажи "  (check-secrets.sh с больным случаем — после раскладки, его гоняет агент по 03-ЗАДАНИЮ)"
  DONE_LIST+=("секреты: $SECRETS_DIR (700), токен бота в $TG_TOKEN_FILE (600)")
  отметить шаг_5
}

шаг_6() { # конфигурация Claude Code (01-SPEC §6)
  local step="шаг 6 — settings.json и переменные качества"; CURRENT_STEP="$step"
  уже_сделан шаг_6 && { скажи "[$step] пропуск (--resume)"; return 0; }
  заголовок "$step"
  делай "mkdir -p /home/$AGENT_USER/.claude"
  записать_файл "/home/$AGENT_USER/.claude/settings.json" 644 "$AGENT_USER:$AGENT_USER" <<'EOF'
{
  "language": "russian",
  "permissions": { "defaultMode": "bypassPermissions" }
}
EOF
  делай "chown -R $AGENT_USER:$AGENT_USER /home/$AGENT_USER/.claude"
  # Каждая переменная дописывается ОТДЕЛЬНО и только если её ещё нет: общий
  # guard по первой из них на уже установленной машине либо дублировал все
  # три, либо молча пропускал новую (так DISABLE_AUTOUPDATER и не приехал бы
  # к тем, у кого харнес стоял до 11.08).
  делай "for v in 'CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1' 'CLAUDE_CODE_AUTO_COMPACT_WINDOW=800000' 'DISABLE_AUTOUPDATER=1'; do grep -q \"\${v%%=*}\" /home/$AGENT_USER/.profile 2>/dev/null || echo \"export \$v\" >> /home/$AGENT_USER/.profile; done; chown $AGENT_USER:$AGENT_USER /home/$AGENT_USER/.profile"
  проверка "$step" "settings.json — валидный JSON с bypassPermissions" \
    "python3 -c \"import json;d=json.load(open('/home/$AGENT_USER/.claude/settings.json'));assert d['permissions']['defaultMode']=='bypassPermissions'\"" \
    "cat /home/$AGENT_USER/.claude/settings.json"
  проверка "$step" "переменные качества в ~/.profile агента" \
    "grep -q CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING /home/$AGENT_USER/.profile && grep -q CLAUDE_CODE_AUTO_COMPACT_WINDOW /home/$AGENT_USER/.profile && grep -q DISABLE_AUTOUPDATER /home/$AGENT_USER/.profile" \
    "tail -5 /home/$AGENT_USER/.profile"
  проверка "$step" "те же переменные строками Environment= в юните агента" \
    "grep -q CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING /etc/systemd/system/harness-agent.service && grep -q CLAUDE_CODE_AUTO_COMPACT_WINDOW /etc/systemd/system/harness-agent.service && grep -q DISABLE_AUTOUPDATER /etc/systemd/system/harness-agent.service" \
    "grep Environment /etc/systemd/system/harness-agent.service"
  скажи "  (проверка действием — headless-проба записи файла — требует OAuth-входа: команда в «ОСТАЛОСЬ РУКАМИ»)"
  DONE_LIST+=("settings.json (bypassPermissions, russian) + переменные качества в ~/.profile и юнитах")
  отметить шаг_6
}

шаг_7а() { # доставка и раскладка харнеса (01-SPEC §7-а)
  local step="шаг 7-а — раскладка харнеса"; CURRENT_STEP="$step"
  уже_сделан шаг_7а && { скажи "[$step] пропуск (--resume)"; return 0; }
  заголовок "$step"
  local starter="/home/$AGENT_USER/starter"
  local pkg_dst="$starter/STARTER-PACKAGE"
  if [[ "$(cd "$SCRIPT_DIR" && pwd)" != "$pkg_dst" ]]; then
    делай "mkdir -p '$pkg_dst' '$starter/UNIFIED'"
    делай "cp -a '$SCRIPT_DIR/.' '$pkg_dst/'"
    делай "cp -a '$UNIFIED_SRC/.' '$starter/UNIFIED/'"
    делай "chown -R $AGENT_USER:$AGENT_USER '$starter'"
  else
    скажи "  + пакет уже лежит в $pkg_dst — копирование пропущено"
  fi
  local rollout="$pkg_dst/harness/razlozhit-harnes.sh"
  # scp с Windows теряет exec-бит — возвращаем его до вызова (сам вызов идёт
  # через bash, но селфтест раскладчика перезапускает себя, и бит нужен живым).
  делай "chmod +x '$rollout'"
  проверка "$step" "самопроверка раскладчика зелёная" \
    "sudo -u $AGENT_USER bash '$rollout' --selftest | grep -q 'SELFTEST: зелёный'" \
    "sudo -u $AGENT_USER bash '$rollout' --selftest"
  делай "mkdir -p '$LOG_DIR' && sudo -u $AGENT_USER bash '$rollout' | tee '$LOG_DIR/install-rollout.log'"
  проверка "$step" "вывод раскладки: «UNIFIED доставлен» и «раскладка завершена»" \
    "grep -q 'UNIFIED доставлен' '$LOG_DIR/install-rollout.log' && grep -q 'раскладка завершена' '$LOG_DIR/install-rollout.log'" \
    "cat '$LOG_DIR/install-rollout.log'"
  проверка "$step" "каркас на месте: хуки, скрипты, демоны, скилы" \
    "[ -f '$PROJECT_DIR/scripts/hooks/guard_bash.py' ] && [ -f '$PROJECT_DIR/scripts/tg_send.sh' ] && [ -f '$PROJECT_DIR/harness/demons/heartbeat-watch.sh' ] && [ -d '$PROJECT_DIR/harness/skills' ]" \
    "ls '$PROJECT_DIR/scripts' '$PROJECT_DIR/harness/demons'"
  # harness.conf — раскладчик от agent не имеет прав на /etc; докладываем под root, живой не затираем
  делай "[ -f /etc/harness/harness.conf ] && echo 'harness.conf уже живой — не трогаю' || { cp '$pkg_dst/harness/config/harness.conf' /etc/harness/harness.conf && chmod 644 /etc/harness/harness.conf; }"
  проверка "$step" "/etc/harness/harness.conf существует и читается" \
    "[ -f /etc/harness/harness.conf ] && bash -n /etc/harness/harness.conf" \
    "ls -l /etc/harness/; bash -n /etc/harness/harness.conf"
  # Вход агента: долгий токен доходит до ВСЕХ, кто зовёт claude. Файл ставится
  # всегда (он безвреден, пока токена нет), а подключение в профиль оболочки —
  # отдельной строкой: cron читает harness.conf, панель tmux — профиль.
  # Улика 12.09.2026: носитель жил в домашнем каталоге и до cron не доходил,
  # а работавшая сессия была старше него на девять часов.
  делай "cp '$pkg_dst/harness/config/vhod.sh' /etc/harness/vhod.sh && chmod 644 /etc/harness/vhod.sh"
  делай "for f in .profile .bashrc; do grep -q 'harness/vhod.sh' /home/$AGENT_USER/\$f 2>/dev/null || printf '\\n# Вход агента: долгий токен Claude Code\\n[ -r /etc/harness/vhod.sh ] && . /etc/harness/vhod.sh\\n' >> /home/$AGENT_USER/\$f; done; chown $AGENT_USER:$AGENT_USER /home/$AGENT_USER/.profile /home/$AGENT_USER/.bashrc"
  проверка "$step" "долгий вход доходит до пути cron и до панели агента" \
    "grep -q 'vhod.sh' /etc/harness/harness.conf && grep -q 'vhod.sh' /home/$AGENT_USER/.profile && grep -q 'vhod.sh' /home/$AGENT_USER/.bashrc" \
    "grep -n vhod /etc/harness/harness.conf /home/$AGENT_USER/.profile /home/$AGENT_USER/.bashrc"
  скажи "  (после git init агент повторит раскладку — она подключит pre-commit симлинком; это его 03-ЗАДАНИЕ)"
  DONE_LIST+=("харнес разложен в $PROJECT_DIR (+UNIFIED, +harness.conf в /etc/harness)")
  отметить шаг_7а
}

шаг_7() { # канал связи Telegram (01-SPEC §7); Б — поддерживаемый дефолт, А — эксперимент
  local step="шаг 7 — канал Telegram (вариант $TG_CHANNEL_VARIANT)"; CURRENT_STEP="$step"
  уже_сделан шаг_7 && { скажи "[$step] пропуск (--resume)"; return 0; }
  заголовок "$step"
  if [[ "$TG_CHANNEL_VARIANT" == "A" ]]; then
    скажи "  вариант А (ЭКСПЕРИМЕНТ, механика подтверждений слабее — поддерживается только Б):"
    скажи "  официальный telegram-плагин; getUpdates владеет ТОЛЬКО плагин."
    делай "[ -x /usr/local/bin/bun ] && echo 'bun уже стоит' || { curl -fsSL https://bun.sh/install | sudo -u $AGENT_USER bash; ln -sf /home/$AGENT_USER/.bun/bin/bun /usr/local/bin/bun; }"
    делай "mkdir -p /home/$AGENT_USER/.claude/channels/telegram"
    делай_тихо "printf 'TELEGRAM_BOT_TOKEN=%s\n' 'содержимое $TG_TOKEN_FILE (скрыто)' > /home/$AGENT_USER/.claude/channels/telegram/.env" \
      "printf 'TELEGRAM_BOT_TOKEN=%s\n' \"\$(cat '$TG_TOKEN_FILE')\" > /home/$AGENT_USER/.claude/channels/telegram/.env"
    делай "chown -R $AGENT_USER:$AGENT_USER /home/$AGENT_USER/.claude/channels && chmod 600 /home/$AGENT_USER/.claude/channels/telegram/.env"
    делай "systemctl disable --now harness-dispatcher 2>/dev/null || true  # при варианте А юнит диспетчера НЕ работает"
    проверка "$step" "bun доступен по /usr/local/bin (плагины не читают ~/.bashrc)" \
      "/usr/local/bin/bun --version" \
      "ls -l /usr/local/bin/bun; sudo -u $AGENT_USER /home/$AGENT_USER/.bun/bin/bun --version"
    проверка "$step" ".env плагина: формат TELEGRAM_BOT_TOKEN=..., права 600" \
      "grep -q '^TELEGRAM_BOT_TOKEN=.' /home/$AGENT_USER/.claude/channels/telegram/.env && [ \"\$(stat -c '%a' /home/$AGENT_USER/.claude/channels/telegram/.env)\" = '600' ]" \
      "stat -c '%a %U' /home/$AGENT_USER/.claude/channels/telegram/.env"
    проверка "$step" "юнит диспетчера не активен (единственный потребитель getUpdates — плагин)" \
      "[ \"\$(systemctl is-active harness-dispatcher 2>/dev/null || echo inactive)\" != active ]" \
      "systemctl status harness-dispatcher"
    DONE_LIST+=("канал вариант А (ЭКСПЕРИМЕНТ): bun + симлинк, .env плагина (600); диспетчер выключен")
  else
    скажи "  вариант Б (поддерживаемый): свой диспетчер tg-dispatcher.sh, юнит из 01-SPEC §4."
    скажи "  скрипт разложен шагом 7-а в $PROJECT_DIR/scripts/ — юнит ставится и включается сейчас."
    записать_файл /etc/systemd/system/harness-dispatcher.service 644 root:root <<EOF
[Unit]
Description=Harness Telegram dispatcher (single getUpdates consumer)
Wants=network-online.target
After=network-online.target

[Service]
User=$AGENT_USER
Environment=HOME=/home/$AGENT_USER
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Environment=CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1
Environment=CLAUDE_CODE_AUTO_COMPACT_WINDOW=800000
Environment=DISABLE_AUTOUPDATER=1
ExecStart=$PROJECT_DIR/scripts/tg-dispatcher.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    проверить_юнит "$step" /etc/systemd/system/harness-dispatcher.service
    делай "systemctl daemon-reload && systemctl enable --now harness-dispatcher"
    проверка "$step" "harness-dispatcher активен" \
      "[ \"\$(systemctl is-active harness-dispatcher)\" = active ]" \
      "systemctl status harness-dispatcher; journalctl -u harness-dispatcher -n 30"
    DONE_LIST+=("канал вариант Б: юнит harness-dispatcher (единственный getUpdates-поток)")
  fi
  # общая для обоих вариантов проверка отправки: успех ТОЛЬКО при ok:true + message_id
  скажи "  + проба отправки: curl sendMessage (токен в команде скрыт), успех только при ok:true и message_id"
  if (( ! DRY )); then
    local token sent_id
    token="$(cat "$TG_TOKEN_FILE")"
    # URL с токеном уходит через stdin (curl -K -), а не в argv: argv виден в ps.
    sent_id=$(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" \
      | curl -s -K - \
        -d chat_id="$TG_CHAT_ID" -d text="Канал установлен. Ответьте РЕПЛАЕМ на это сообщение словом: дошло" \
      | jq -e '.result.message_id') \
      || стоп "$step" "отправка НЕ подтверждена API (нет ok:true/message_id)" \
              "TOKEN=\$(sudo cat $TG_TOKEN_FILE); curl -s \"https://api.telegram.org/bot\$TOKEN/sendMessage\" -d chat_id=$TG_CHAT_ID -d text=проба | jq ."
    CHECKED_LIST+=("$step: доставка подтверждена API, message_id=$sent_id")
    скажи "  OK: message_id=$sent_id — сообщение владельцу ушло; вторая половина проверки (реплай «дошло») — в «ОСТАЛОСЬ РУКАМИ»"
    SENT_ID="$sent_id"
  else
    скажи "  проверка: jq -e '.result.message_id' в ответе sendMessage непуст"
  fi
  отметить шаг_7
}

шаг_8() { # базовый набор расширений (01-SPEC §8): ставится САМ, кодом
  local step="шаг 8 — базовый набор расширений"; CURRENT_STEP="$step"
  уже_сделан шаг_8 && { скажи "[$step] пропуск (--resume)"; return 0; }
  заголовок "$step"
  # Владелец 12.09.2026: «есть вещи которые сразу надо подключать по умолчанию
  # <…> у нас должен быть сразу набор установленный» и «Команды установки/
  # удаления плагинов и скилов не для человека, а для демонов и хуков».
  # Поэтому набор не печатается советом, а ставится: список — данные
  # (harness/config/расширения.yaml), установка — scripts/rasshirenija.py.
  скажи "  набор берётся из данных: $PROJECT_DIR/harness/config/расширения.yaml"
  # П-13: совет про --channels уместен ТОЛЬКО при варианте А — в финале
  # варианта Б такой команды нет, печатать её значит путать владельца.
  if [[ "$TG_CHANNEL_VARIANT" == "A" ]]; then
    скажи "  telegram-плагин (вариант А) активируется флагом --channels при запуске агента — команда в «ОСТАЛОСЬ РУКАМИ»;"
  fi
  # Ставится ОТ ИМЕНИ АГЕНТА: плагины живут в его ~/.claude, а шаг идёт под root.
  делай "sudo -u $AGENT_USER python3 '$PROJECT_DIR/scripts/rasshirenija.py' поставить-базовый"
  проверка "$step" "базовый набор расширений на месте" \
    "sudo -u $AGENT_USER python3 '$PROJECT_DIR/scripts/rasshirenija.py' состояние" \
    "sudo -u $AGENT_USER python3 '$PROJECT_DIR/scripts/rasshirenija.py' состояние"
  проверка "$step" "скилы и шаблоны разложены (дисциплина пайплайна)" \
    "[ -d '$PROJECT_DIR/harness/skills' ] && [ -d '$PROJECT_DIR/harness/шаблоны-задач' ]" \
    "ls '$PROJECT_DIR/harness'"
  DONE_LIST+=("базовый набор расширений (данные: harness/config/расширения.yaml)")
  отметить шаг_8
}

шаг_9() { # crontab демонов + прогон каждого в env -i (01-SPEC §9)
  local step="шаг 9 — crontab демонов и прогоны env -i"; CURRENT_STEP="$step"
  уже_сделан шаг_9 && { скажи "[$step] пропуск (--resume)"; return 0; }
  заголовок "$step"
  local D="$PROJECT_DIR/harness/demons"
  проверка "$step" "демоны разложены (иначе cron — тихий отказ)" \
    "[ -d '$D' ]" \
    "ls '$PROJECT_DIR/harness' 2>/dev/null || echo 'раскладка не выполнена (шаг 7-а)'"
  local cron_file="/tmp/harness-crontab.$$"
  записать_файл "$cron_file" 644 root:root <<EOF
*/10 * * * *  $D/session-warden.sh        >>$LOG_DIR/session-warden.log 2>&1
0 * * * *     $D/task-closer.sh           >>$LOG_DIR/task-closer.log 2>&1
0 3 * * *     $D/disk-cleanup.sh          >>$LOG_DIR/disk-cleanup.log 2>&1
30 3 * * *    $D/server-hygiene.sh        >>$LOG_DIR/server-hygiene.log 2>&1
0 4 * * *     $D/backup.sh                >>$LOG_DIR/backup.log 2>&1
15 4 * * *    $D/devmap-selfheal.sh       >>$LOG_DIR/devmap-selfheal.log 2>&1
5 * * * *     $D/evo-collector.sh         >>$LOG_DIR/evo-collector.log 2>&1
10 * * * *    $D/tokens-collector.sh      >>$LOG_DIR/tokens-collector.log 2>&1
0 8 * * *     $D/heartbeat-watch.sh       >>$LOG_DIR/heartbeat-watch.log 2>&1
0 9 1 * *     $D/memory-revision.sh       >>$LOG_DIR/memory-revision.log 2>&1
EOF
  делай "crontab -u $AGENT_USER - < '$cron_file'  # мы под root — ставим сразу, точно по таблице §9"
  проверка "$step" "crontab -l совпадает с таблицей §9" \
    "diff <(crontab -l -u $AGENT_USER) '$cron_file'" \
    "diff <(crontab -l -u $AGENT_USER) '$cron_file'; crontab -l -u $AGENT_USER"
  делай "rm -f '$cron_file'"
  local demons=(session-warden task-closer disk-cleanup server-hygiene backup devmap-selfheal evo-collector tokens-collector heartbeat-watch memory-revision)
  # П-1б (классовая починка): набор демонов сверяется с таблицей §9 САМОЙ
  # спеки, не с собственной копией — установщик лежит рядом со спекой.
  # В dry-run crontab не ставится — сверяется массив прогонов (тот же набор,
  # которым живёт шаг); расхождение установщика со спекой ловится и без root.
  local spec_md="$SCRIPT_DIR/harness/config/системные-задания.yaml"
  if [[ -f "$spec_md" ]]; then
    скажи "  сверка набора демонов с реестром системных заданий: $spec_md"
    local sverka_rc=0
    if (( DRY )); then
      printf '/%s.sh \n' "${demons[@]}" | сверить_демонов_со_спекой "$spec_md" || sverka_rc=$?
    else
      crontab -l -u "$AGENT_USER" | сверить_демонов_со_спекой "$spec_md" || sverka_rc=$?
    fi
    if (( sverka_rc == 2 )); then
      # Сверять нечем. В сухом прогоне это нормально (python3 ставится шагом 2),
      # в боевом — уже поздно: шаг 2 прошёл, значит сломан он.
      if (( DRY )); then
        скажи "  сверка отложена до боевого прогона: python3-yaml ставится шагом 2"
      else
        стоп "$step" "нет python3 с yaml после шага 2 — сверить набор демонов нечем" \
          "python3 -c 'import yaml'; dpkg -l python3-yaml"
      fi
    elif (( sverka_rc )); then
      стоп "$step" "набор демонов crontab расходится с реестром системных заданий (перечень выше)" \
        "crontab -l -u $AGENT_USER; cat '$spec_md'"
    fi
    (( sverka_rc == 2 )) || скажи "  OK: набор демонов совпадает с реестром (${#demons[@]} демонов)"
    (( DRY )) || CHECKED_LIST+=("$step: набор демонов crontab = реестру системные-задания.yaml")
  else
    скажи "  сверка с реестром пропущена: файла нет рядом ($spec_md)"
  fi
  скажи ""
  скажи "  прогон каждого демона в чистом окружении (как его увидит cron), образец §9:"
  local d rc
  for d in "${demons[@]}"; do
    скажи "  + sudo -u $AGENT_USER env -i HOME=/home/$AGENT_USER PATH=/usr/local/bin:/usr/bin:/bin /bin/sh -c '$D/$d.sh'; echo exit=\$?"
    if (( ! DRY )); then
      rc=0
      sudo -u "$AGENT_USER" env -i HOME="/home/$AGENT_USER" PATH=/usr/local/bin:/usr/bin:/bin \
        /bin/sh -c "$D/$d.sh" >>"$LOG_DIR/install-demon-probe.log" 2>&1 || rc=$?
      if (( rc == 0 )); then
        скажи "    exit=0 — $d зелёный"
        CHECKED_LIST+=("$step: $d в env -i, exit=0")
      else
        скажи "    exit=$rc — $d УПАЛ (не прерываемся, свод в конце; лог: $LOG_DIR/install-demon-probe.log)"
        FAILED_LIST+=("демон $d: exit=$rc в env -i — смотреть $LOG_DIR/install-demon-probe.log и $D/$d.sh")
      fi
    fi
  done
  скажи "  (метки heartbeat: ls -l $HEARTBEAT_DIR — успешные демоны обновили свои; полная сверка — после первых суток)"
  DONE_LIST+=("crontab $AGENT_USER: ${#demons[@]} демонов точно по таблице §9 (набор сверен со спекой); каждый прогнан в env -i")
  отметить шаг_9
}

# ------------------------------------------------------------------ финал ----

осталось_руками() {
  заголовок "ОСТАЛОСЬ РУКАМИ (только физически неавтоматизируемое)"
  local AGENT_DEAD_HINT=15
  if [[ -r "$HARNESS_SRC/config/harness.conf" ]]; then
    AGENT_DEAD_HINT=$(sed -n 's/^[[:space:]]*AGENT_DEAD_MIN=\([0-9]*\).*/\1/p' \
                      "$HARNESS_SRC/config/harness.conf" | tail -n1)
    AGENT_DEAD_HINT="${AGENT_DEAD_HINT:-15}"
  fi
  скажи ""
  local start_cmd="claude --dangerously-skip-permissions"
  if [[ -r "$HARNESS_SRC/config/harness.conf" ]]; then
    start_cmd=$(sed -n 's/^[[:space:]]*AGENT_START_CMD="\{0,1\}\([^"#]*\)"\{0,1\}.*/\1/p' \
                "$HARNESS_SRC/config/harness.conf" | tail -n1)
    start_cmd="${start_cmd:-claude --dangerously-skip-permissions}"
  fi
  скажи "ЭТОТ БЛОК СОХРАНЁН ФАЙЛОМ: $LOG_DIR/ОСТАЛОСЬ-РУКАМИ.txt — закрыли"
  скажи "терминал, потеряли прокрутку: откройте файл, там ровно этот текст."
  скажи ""
  скажи "ГДЕ ВВОДИТЬ. Всё ниже — команды ВАШЕЙ оболочки, кроме отмеченного"
  скажи "«ВНУТРИ tmux» и «ВНУТРИ Claude Code». В самого агента вы вводите ровно"
  скажи "один текст — первый промпт (пункт в); дальше только Telegram."
  скажи ""
  скажи "ВЫХОД ИЗ СЕССИИ АГЕНТА — Ctrl+B, отпустить, затем D (агент продолжает)."
  скажи "Ctrl+C дважды или /exit ЗАКРЫВАЮТ агента: tmux останется, работать будет"
  скажи "некому. Сторож session-warden заметит это за ~${AGENT_DEAD_HINT} мин, поднимет сам и"
  скажи "напишет в Telegram — но проще не ронять."
  скажи ""
  скажи "а) OAuth-вход (подписка Max) — ДЕЛАЙТЕ СРАЗУ В СЕССИИ АГЕНТА,"
  скажи "   тогда та же сессия и авторизуется, и останется работать:"
  скажи ""
  скажи "   sudo -u $AGENT_USER -H tmux attach -t agent"
  скажи "   $start_cmd     # ← ВНУТРИ tmux"
  скажи ""
  скажи "   Появится ссылка — откройте её В БРАУЗЕРЕ НА СВОЁМ КОМПЬЮТЕРЕ, войдите в"
  скажи "   аккаунт и вставьте код обратно. Вопрос о доверии к каталогу — Y."
  скажи "   Подключайтесь НОРМАЛЬНЫМ SSH-клиентом, НЕ браузерным терминалом:"
  скажи "   браузерные ломают длинную строку кода переносами → «OAuth error: Invalid code»."
  скажи "   Вход сохраняется в /home/$AGENT_USER/.claude/ — повторно не потребуется."
  скажи "   Команда запуска — ровно та, которой сторож поднимает агента после"
  скажи "   ротации (AGENT_START_CMD в harness.conf): без флага после первой"
  скажи "   ротации она сменилась бы у вас под руками."
  скажи ""
  скажи "   НЕ закрывайте Claude Code. Отключитесь: Ctrl+B, затем D."
  скажи ""
  скажи "   Проверка входа действием — в ВАШЕЙ оболочке (01-SPEC §3 и §6):"
  скажи ""
  скажи "   sudo -u $AGENT_USER -H bash -c 'cd ~ && claude -p \"ответь одним словом: работаю\" --output-format text'"
  скажи "   sudo -u $AGENT_USER -H bash -c 'cd ~ && claude -p \"создай файл /tmp/probe_perm.txt со словом ok и скажи готово\" --output-format text' && cat /tmp/probe_perm.txt"
  скажи ""
  if [[ "$TG_CHANNEL_VARIANT" == "A" ]]; then
    скажи "б) Запуск агента с каналом и сопряжение бота (вариант А — ЭКСПЕРИМЕНТ,"
    скажи "   механика подтверждений слабее; поддерживаемый путь — вариант Б):"
    скажи ""
    скажи "   sudo -u $AGENT_USER -H tmux attach -t agent"
    скажи "   # внутри tmux (флаг --channels нужен ПРИ КАЖДОМ запуске):"
    скажи "   claude --dangerously-skip-permissions --channels plugin:telegram@claude-plugins-official"
    скажи ""
    скажи "   Затем в Telegram напишите боту ДВА сообщения — 6-символьный код придёт"
    скажи "   в ответ на ВТОРОЕ. В сессии Claude Code выполните:"
    скажи ""
    скажи "   /telegram:access pair КОД-ИЗ-ВТОРОГО-СООБЩЕНИЯ"
    скажи "   /telegram:access policy allowlist   # обязательно"
    скажи ""
    скажи "   Отключиться от tmux: Ctrl+B, затем D — агент продолжит работать."
  else
    скажи "б) Двусторонняя проверка канала (вариант Б, через ДИСПЕТЧЕР):"
    скажи ""
    скажи "   Ответьте В TELEGRAM «дошло» на пробное сообщение бота, затем:"
    скажи ""
    скажи "   ls -t $LOG_DIR/inbox/ | head -1        # файл входящего появился"
    скажи "   cat \"$LOG_DIR/inbox/\$(ls -t $LOG_DIR/inbox/ | head -1)\"   # текст «дошло»"
    скажи "   # РУЧНОЙ getUpdates НЕ ВЫЗЫВАТЬ: диспетчер — единственный потребитель;"
    скажи "   # ручной вызов украдёт апдейт и вгонит юнит в 409-паузу на 300 с."
  fi
  скажи ""
  скажи "в) Первый промпт агенту — ЕДИНСТВЕННОЕ, что вводится ВНУТРИ Claude Code"
  скажи "   (фаза 3 ИНСТРУКЦИЯ-ВЛАДЕЛЬЦУ; при варианте А можно прямо в Telegram):"
  скажи ""
  скажи "   sudo -u $AGENT_USER -H tmux attach -t agent    # claude уже там после пункта а"
  скажи ""
  скажи "   Вставьте в чат агента этот текст:"
  скажи ""
  скажи "   Прочитай $PROJECT_DIR/harness/ОПИСЬ.md и файл"
  скажи "   /home/$AGENT_USER/starter/STARTER-PACKAGE/razrabotka/03-ЗАДАНИЕ-НОВОМУ-АГЕНТУ.md — и выполняй его"
  скажи "   по порядку, начиная с раздела 2. Отчитывайся в Telegram по каждому блоку."
  скажи ""
  скажи "   Затем Ctrl+B, затем D. С этого момента руль у агента; следите по Telegram."
  скажи ""
  скажи "г) Убедиться, что агент жив (в ВАШЕЙ оболочке, в любой момент):"
  скажи ""
  скажи "   systemctl is-active harness-agent"
  скажи "   sudo -u $AGENT_USER -H tmux ls"
  скажи "   sudo -u $AGENT_USER -H tmux display-message -p -t agent '#{pane_current_command}'"
  скажи ""
  скажи "   Последняя должна печатать claude или node. Печатает bash — агент выпал"
  скажи "   в оболочку: подключитесь и запустите «$start_cmd»,"
  скажи "   либо подождите — сторож поднимет сам и напишет вам."
}

сводка() {
  заголовок "ИТОГОВАЯ СВОДКА"
  скажи ""
  if (( DRY )); then скажи "Было бы поставлено (dry-run, ничего не исполнялось):"; else скажи "Поставлено:"; fi
  if ((${#DONE_LIST[@]})); then printf '  - %s\n' "${DONE_LIST[@]}"; fi
  скажи ""
  скажи "Проверено (каждая проверка — из 01-SPEC):"
  if ((${#CHECKED_LIST[@]})); then printf '  - %s\n' "${CHECKED_LIST[@]}"; else скажи "  (dry-run: проверки напечатаны, не исполнялись)"; fi
  скажи ""
  if ((${#FAILED_LIST[@]})); then
    скажи "УПАЛО (установка не прервана, чинить до приёмки):"
    printf '  - %s\n' "${FAILED_LIST[@]}"
    скажи "  Демоны, зовущие claude, зеленеют только ПОСЛЕ OAuth-входа — перепрогнать образцом §9."
  else
    скажи "Упавших шагов нет."
  fi
  скажи ""
  скажи "Дальше: блок «ОСТАЛОСЬ РУКАМИ» выше, затем razrabotka/ЧЕК-ЛИСТ-ПРИЁМКИ.md (раздел А) и razrabotka/03-ЗАДАНИЕ-НОВОМУ-АГЕНТУ.md."
  скажи ""
  скажи "Крупные наборы (возможности) НЕ ставятся при установке — только по условию:"
  скажи "   sudo -u $AGENT_USER bash $PROJECT_DIR/scripts/vozmozhnost.sh список"
  скажи "   (в комплекте «мобильная-разработка»: ставится, когда в задаче появилось приложение)"
}

# ----------------------------------------------------------------- прогон ----

(( DRY )) && заголовок "РЕЖИМ --dry-run: команды печатаются, НИЧЕГО не исполняется"
опрос

# Пути проверяются ПОСЛЕ опроса и ДО первого шага — на каждом запуске, включая
# --resume (там вопросов нет вовсе, а конфиг мог быть поправлен руками неверно).
if ! сверить_пути_конфига; then
  стоп "проверка значений до шагов" \
    "путь из $CONF не годится (причина выше) — ни один шаг не начинался" \
    "grep -n 'PROJECT_DIR\|SECRETS_DIR\|LOG_DIR\|HEARTBEAT_DIR' $CONF"
fi
шаг_0
шаг_1
шаг_2
шаг_3
шаг_4
шаг_5
шаг_6
шаг_7а
шаг_7
шаг_8
шаг_9
# Блок «ОСТАЛОСЬ РУКАМИ» — единственная в установке инструкция ЧЕЛОВЕКУ, и до
# сих пор она жила только в прокрутке терминала: закрыл окно — искать негде.
# tee кладёт её файлом; путь назван внутри самого блока (иначе владелец не
# узнает, что файл есть). Отказ записи не валит установку, но и не молчит:
# pipefail сделал бы упавший tee провалом всего прогона на последнем шаге.
MANUAL_FILE="$LOG_DIR/ОСТАЛОСЬ-РУКАМИ.txt"
if (( DRY )); then
  осталось_руками
elif ! осталось_руками | tee "$MANUAL_FILE"; then
  скажи "ВНИМАНИЕ: блок «ОСТАЛОСЬ РУКАМИ» не записан в $MANUAL_FILE — сохраните его из прокрутки терминала"
fi
сводка
exit 0
