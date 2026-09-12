#!/usr/bin/env bash
#
# vozmozhnost.sh — установка «возможностей» харнеса одной командой.
#
# Зачем отдельный механизм рядом с таблицей расширений. Строка таблицы
# `установка-расширений` — это одна-две команды (`/plugin install ...`).
# Возможность — крупный набор: системные пакеты, скрипты в ~/bin, свои скилы,
# свои запреты сторожу, свой список уборки, свой реестр действий владельца.
# Такое нельзя носить прозой «сделай то, потом это»: инструкция из шести частей
# живёт до первого забывшего. Поэтому возможность — КАТАЛОГ С ПАСПОРТОМ, а
# установка — код.
#
# Что даёт: любой набор ставится в любой момент одной командой, до установки не
# занимает ни строки контекста (скилы не сгенерированы, запреты молчат), а после
# установки сам врастает в носители харнеса.
#
# Устройство каталога возможности (всё, кроме МАНИФЕСТ.conf и ustanovit.sh, —
# по желанию; отсутствующее просто пропускается):
#
#   МАНИФЕСТ.conf     паспорт: условие, требования, пороги — ДАННЫЕ
#   ustanovit.sh     установка: шаги, --dry-run, --selftest — КОД
#   skills-src/*.md   источники скилов; собираются, ТОЛЬКО когда установлено
#   память/*.md       записи в память проекта (+ строка таблицы срабатываний)
#   bin/*             скрипты в ~/bin (петля, эмулятор и т.п.)
#   запреты.list      строки сторожу guard_bash: регэксп<TAB>сообщение
#   уборка.list       строки демону disk-cleanup: каталог<TAB>дней
#   гейты/*.py        гейты pre-commit (запускаются, когда установлено)
#   gitignore.list    строки в .gitignore проекта
#   ЧЕЛОВЕКУ.md       что физически не может сделать агент — владельцу
#   ЧЕК-ЛИСТ.md       приёмка возможности, включая больные случаи
#
# Команды:
#   vozmozhnost.sh список                 что вообще есть и что установлено
#   vozmozhnost.sh требования <id>        проверка ВЕЩЕЙ, с адресом у каждой
#   vozmozhnost.sh поставить <id> [--dry-run]
#   vozmozhnost.sh состояние [<id>]
#   vozmozhnost.sh снять <id>             отключить (файлы на диске остаются)
#   vozmozhnost.sh --selftest             больной и здоровый случай во временном
#
# Код возврата: 0 — сделано, 1 — не сделано (причина названа), 2 — нет такой
# возможности или нет конфига установки.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_CONF="${HARNESS_INSTALL_CONF:-/etc/harness/install.conf}"
HARNESS_CONF_PATH="${HARNESS_CONF:-/etc/harness/harness.conf}"

# ── каталог возможностей ────────────────────────────────────────────────────
# Порядок: переменная окружения (тесты) → раскладка на сервере (PROJECT_DIR из
# паспорта) → раскладка пакета (харнес/scripts рядом с харнес/возможности) →
# раскладка на сервере без конфига (scripts/ рядом с харнес/).
caps_dir() {
  if [[ -n "${HARNESS_CAPS_DIR:-}" ]]; then printf '%s\n' "$HARNESS_CAPS_DIR"; return 0; fi
  local pd=""
  if [[ -r "$INSTALL_CONF" ]]; then
    pd=$(sed -n 's/^[[:space:]]*PROJECT_DIR=\"\{0,1\}\([^"#]*\)\"\{0,1\}.*/\1/p' "$INSTALL_CONF" | tail -n1)
  fi
  local c
  for c in "${pd:+$pd/харнес/возможности}" "$HERE/../возможности" "$HERE/../харнес/возможности"; do
    [[ -d "$c" ]] && { (cd "$c" && pwd); return 0; }
  done
  return 1
}

say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit "${2:-1}"; }

# ── чтение паспорта возможности ─────────────────────────────────────────────
# source, а не парсер: файл наш, лежит рядом с кодом установки, и без source
# многострочные значения (ПРОБЫ_ПАКЕТОВ) пришлось бы разбирать вручную.
load_manifest() { # $1 = каталог возможности
  local m="$1/МАНИФЕСТ.conf"
  [[ -r "$m" ]] || die "нет паспорта $m — это не каталог возможности" 2
  bash -n "$m" || die "паспорт $m не читается оболочкой (bash -n красный)"
  # shellcheck disable=SC1090
  source "$m"
  [[ -n "${ID:-}" && -n "${CONDITION:-}" ]] || die "в паспорте $m пусто ID или CONDITION"
}

installed_marker() { printf '%s\n' "$1/.установлено"; }
is_installed() { [[ -f "$(installed_marker "$1")" ]]; }

# ── требования: проверяется ВЕЩЬ, у каждого пропуска есть адрес ──────────────
free_gb() { # $1 = путь; печатает целые ГБ свободного места на его ФС
  df -P -k "$1" 2>/dev/null | awk 'NR==2{printf "%d\n", int($4/1048576)}'
}
mem_total_mb() { awk '/^MemTotal:/{printf "%d\n", int($2/1024)}' /proc/meminfo; }

check_requirements() { # $1 = каталог возможности; rc=1 при невыполненном жёстком
  local dir="$1" hard_bad=0 home_dir="${HOME:-/root}"
  load_manifest "$dir"
  say "Требования возможности «${ID}»:"
  # диск
  local need_gb="${NEED_DISK_GB:-0}" have_gb
  have_gb=$(free_gb "$home_dir"); have_gb="${have_gb:-0}"
  if (( have_gb >= need_gb )); then
    say "  [ок ] диск: свободно ${have_gb} ГБ (нужно ${need_gb})"
  else
    say "  [НЕТ] диск: свободно ${have_gb} ГБ, нужно ${need_gb}."
    say "        адрес: bash харнес/demons/disk-cleanup.sh && df -h $home_dir"
    hard_bad=1
  fi
  # память
  local need_mb="${NEED_MEM_TOTAL_MB:-0}" have_mb
  have_mb=$(mem_total_mb)
  if (( have_mb >= need_mb )); then
    say "  [ок ] память: всего ${have_mb} МБ (нужно ${need_mb})"
  else
    say "  [НЕТ] память: всего ${have_mb} МБ, нужно ${need_mb}. адрес: тариф VPS"
    hard_bad=1
  fi
  local want_mb="${WANT_MEM_TOTAL_MB:-0}"
  (( want_mb > 0 && have_mb < want_mb )) && \
    say "  [ ~ ] памяти ${have_mb} МБ при советуемых ${want_mb}: работать будет, но тесно — держи потолки из паспорта"
  # sudo
  if [[ "${NEED_SUDO:-нет}" == "да" ]]; then
    if sudo -n true 2>/dev/null; then
      say "  [ок ] sudo без пароля есть — системный шаг выполнится сам"
    else
      say "  [ ~ ] sudo без пароля нет: системный шаг будет ВЫПИСАН файлом,"
      say "        его выполняет владелец одной командой (адрес — в выводе установки)"
    fi
  fi
  # KVM — проверяется ВЕЩЬ: устройство есть И доступно на чтение-запись
  if [[ "${WANT_KVM:-нет}" == "да" ]]; then
    if [[ -r /dev/kvm && -w /dev/kvm ]]; then
      say "  [ок ] /dev/kvm доступен — локальный эмулятор будет"
    elif [[ -e /dev/kvm ]]; then
      say "  [ ~ ] /dev/kvm есть, но прав нет: локальный эмулятор пропустится."
      say "        адрес: sudo usermod -aG kvm $(id -un) && перезайти в сессию"
    else
      say "  [ ~ ] /dev/kvm нет (VPS без вложенной виртуализации): локальный"
      say "        эмулятор пропустится, останутся Firebase Test Lab и EAS"
    fi
  fi
  # пробы вещей, а не имён пакетов
  if [[ -n "${PROBES:-}" ]]; then
    local line name cmd
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      name="${line%%=*}"; cmd="${line#*=}"
      if bash -c "$cmd" >/dev/null 2>&1; then
        say "  [ок ] $name: «$cmd» работает"
      else
        say "  [ ~ ] $name: «$cmd» не работает — поставится системным шагом"
      fi
    done <<< "${PROBES}"
  fi
  return "$hard_bad"
}

# ── врастание в носители после успешной установки ───────────────────────────
project_dir() {
  [[ -n "${HARNESS_PROJECT_DIR:-}" ]] && { printf '%s\n' "$HARNESS_PROJECT_DIR"; return 0; }
  [[ -r "$INSTALL_CONF" ]] || return 1
  sed -n 's/^[[:space:]]*PROJECT_DIR=\"\{0,1\}\([^"#]*\)\"\{0,1\}.*/\1/p' "$INSTALL_CONF" | tail -n1
}
log_dir() {
  [[ -n "${HARNESS_LOG_DIR:-}" ]] && { printf '%s\n' "$HARNESS_LOG_DIR"; return 0; }
  local d=""
  [[ -r "$INSTALL_CONF" ]] && d=$(sed -n 's/^[[:space:]]*LOG_DIR=\"\{0,1\}\([^"#]*\)\"\{0,1\}.*/\1/p' "$INSTALL_CONF" | tail -n1)
  printf '%s\n' "${d:-/var/log/harness}"
}
caps_log_name() {
  local n=""
  [[ -r "$HARNESS_CONF_PATH" ]] && n=$(sed -n 's/^[[:space:]]*CAPABILITIES_LOG_NAME=\"\{0,1\}\([^"#]*\)\"\{0,1\}.*/\1/p' "$HARNESS_CONF_PATH" | tail -n1)
  printf '%s\n' "${n:-возможности.jsonl}"
}

# Записи памяти и строки указателя/таблицы. Строки лежат в самой записи
# комментариями-метками — источник истины один, файл записи:
#   <!-- указатель: - [Имя](файл.md) — о чём -->
#   <!-- таблица: когда происходит вот это | проверить себя -->
graft_memory() { # $1 = каталог возможности, $2 = каталог проекта
  local dir="$1" proj="$2" f
  [[ -d "$dir/память" ]] || return 0
  mkdir -p "$proj/память"
  for f in "$dir/память"/*.md; do
    [[ -e "$f" ]] || continue
    cp -f "$f" "$proj/память/"
  done
  python3 - "$dir/память" "$proj/память/MEMORY.md" <<'PY'
import re, sys
from pathlib import Path
src, index = Path(sys.argv[1]), Path(sys.argv[2])
if not index.is_file():
    print("  память: нет указателя MEMORY.md — записи скопированы, строки не вросли")
    sys.exit(0)
text = index.read_text(encoding="utf-8")
lines_point, lines_table = [], []
for f in sorted(src.glob("*.md")):
    body = f.read_text(encoding="utf-8")
    lines_point += re.findall(r"<!--\s*указатель:\s*(.+?)\s*-->", body)
    lines_table += re.findall(r"<!--\s*таблица:\s*(.+?)\s*-->", body)
added = 0
for row in lines_table:                      # строки таблицы срабатываний
    cells = [c.strip() for c in row.split("|")]
    md = "| " + " | ".join(cells) + " |"
    if md in text:
        continue
    rows = [m for m in re.finditer(r"^\|.*\|$", text, re.M)]
    if not rows:
        continue
    last = rows[-1]
    text = text[:last.end()] + "\n" + md + text[last.end():]
    added += 1
# Раздела «Указатель» в MEMORY.md больше нет: он повторял таблицу вторым
# списком и стоил трети объёма, который едет в контекст каждую сессию
# (прополка 01.09.2026, 30 880 → 19 302 знака). Запись находится по ссылке
# из таблицы. Строку `<!-- указатель: … -->` поэтому не вращиваем, а
# называем вслух: молчаливое дописывание в конец файла отрастило бы
# указатель заново.
if lines_point:
    print(f"  память: строк `указатель:` {len(lines_point)} — не вросли, "
          "раздела больше нет; триггер записи задаётся строкой `таблица:`")
index.write_text(text, encoding="utf-8")
print(f"  память: записи скопированы, строк вросло {added} (повтор не дублирует)")
PY
}

graft_gitignore() { # $1 = каталог возможности, $2 = каталог проекта
  local list="$1/gitignore.list" gi="$2/.gitignore" marker="# возможность: ${ID}"
  [[ -r "$list" ]] || return 0
  [[ -f "$gi" ]] || : > "$gi"
  if grep -qF "$marker" "$gi"; then
    say "  .gitignore: строки возможности уже есть"
    return 0
  fi
  { printf '\n%s\n' "$marker"; grep -v '^[[:space:]]*#' "$list" | grep -v '^[[:space:]]*$'; } >> "$gi"
  say "  .gitignore: строки возможности добавлены"
}

graft_human() { # $1 = каталог возможности, $2 = каталог проекта
  local src="$1/${HUMAN_DOC:-ЧЕЛОВЕКУ.md}"
  [[ -r "$src" ]] || return 0
  mkdir -p "$2/docs/возможности"
  cp -f "$src" "$2/docs/возможности/${ID}-РУКАМИ.md"
  say "  реестр действий владельца: docs/возможности/${ID}-РУКАМИ.md (отправить владельцу ВЛОЖЕНИЕМ)"
}

rebuild_skills() { # $1 = каталог проекта; rc=1 — скилы НЕ пересобраны
  local gen="$1/харнес/SBORKA-SKILOV.py"
  if [[ ! -f "$gen" ]]; then
    say "  скилы: генератора нет ($gen) — пересборка невозможна"
    return 1
  fi
  if python3 "$gen" >/dev/null 2>"$1/.скилы-ошибка"; then
    rm -f "$1/.скилы-ошибка"
    say "  скилы: пересобраны — скилы возможности появились в харнес/skills/"
    return 0
  fi
  say "  скилы: пересборка УПАЛА — $(tr '\n' ' ' < "$1/.скилы-ошибка" | cut -c1-300)"
  return 1
}

register() { # $1 = каталог возможности
  local dir="$1" proj log marker
  proj=$(project_dir) || die "нет $INSTALL_CONF и не задан HARNESS_PROJECT_DIR — установка не завершена (01-SPEC §0)" 2
  [[ -n "$proj" ]] || die "PROJECT_DIR пуст в $INSTALL_CONF" 2
  log=$(log_dir)
  marker=$(installed_marker "$dir")
  # Метка ставится ДО пересборки скилов: генератор собирает скилы только
  # установленных возможностей, и порядок здесь — часть механизма, а не вкус.
  {
    printf 'ID="%s"\n' "$ID"
    printf 'ДАТА="%s"\n' "$(date -u +%FT%TZ)"
    printf 'ПАСПОРТ_SHA256="%s"\n' "$(sha256sum "$dir/МАНИФЕСТ.conf" | cut -d' ' -f1)"
  } > "$marker"
  say "  метка: $(basename "$marker") поставлена"
  mkdir -p "$log"
  printf '{"ts":"%s","возможность":"%s","событие":"установлена","паспорт_sha256":"%s"}\n' \
    "$(date -u +%FT%TZ)" "$ID" "$(sha256sum "$dir/МАНИФЕСТ.conf" | cut -d' ' -f1)" \
    >> "$log/$(caps_log_name)" 2>/dev/null || say "  реестр $log/$(caps_log_name) недоступен (установка не затронута)"
  graft_memory "$dir" "$proj"
  graft_gitignore "$dir" "$proj"
  graft_human "$dir" "$proj"
  rebuild_skills "$proj"
}

# ── команды ─────────────────────────────────────────────────────────────────
cmd_list() {
  local root d
  root=$(caps_dir) || die "каталог возможностей не найден (ни HARNESS_CAPS_DIR, ни PROJECT_DIR, ни рядом со скриптом)" 2
  say "Возможности в $root:"
  say ""
  local found=0
  for d in "$root"/*/; do
    [[ -f "$d/МАНИФЕСТ.conf" ]] || continue
    found=1
    ( load_manifest "$d"
      if is_installed "$d"; then
        printf '  [УСТАНОВЛЕНА] %s\n' "$ID"
      else
        printf '  [ не стоит  ] %s\n' "$ID"
      fi
      printf '      %s\n' "${NAME:-}"
      printf '      условие: %s\n' "$CONDITION"
      printf '      ставится: vozmozhnost.sh поставить %s\n\n' "$ID" )
  done
  (( found )) || say "  (пусто — ни одного каталога с МАНИФЕСТ.conf)"
}

resolve() { # $1 = id → печатает каталог
  local root="$1" id="$2"
  [[ -f "$root/$id/МАНИФЕСТ.conf" ]] || return 1
  printf '%s\n' "$root/$id"
}

cmd_install() { # $1 = id, остальное — флаги установщика
  local root id dir dry=0
  root=$(caps_dir) || die "каталог возможностей не найден" 2
  id="$1"; shift || true
  [[ "${1:-}" == "--dry-run" ]] && dry=1
  dir=$(resolve "$root" "$id") || die "нет возможности «$id» — список: vozmozhnost.sh список" 2
  load_manifest "$dir"
  if is_installed "$dir" && (( ! dry )); then
    say "Возможность «$ID» уже установлена ($(sed -n 's/^ДАТА=//p' "$(installed_marker "$dir")"))."
    say "Повторная установка допустима и идемпотентна; чтобы точно переставить —"
    say "сначала: vozmozhnost.sh снять $ID"
  fi
  say "=== Возможность «$ID»"
  say "Условие включения: $CONDITION"
  say ""
  if ! check_requirements "$dir"; then
    die "жёсткое требование не выполнено — установка НЕ начиналась (причина и адрес выше)"
  fi
  say ""
  [[ -x "$dir/ustanovit.sh" || -r "$dir/ustanovit.sh" ]] || die "нет $dir/ustanovit.sh"
  # bash "$0", не "$0": после копирования с Windows exec-бита может не быть.
  local rc=0
  PACK_DIR="$dir" PACK_ID="$ID" \
  HARNESS_INSTALL_CONF="$INSTALL_CONF" HARNESS_CONF="$HARNESS_CONF_PATH" \
    bash "$dir/ustanovit.sh" "$@" || rc=$?
  if (( rc )); then
    die "установка возможности «$ID» остановлена на своём шаге (код $rc) — причина выше; повтор безопасен"
  fi
  if (( dry )); then
    say ""
    say "--dry-run: ничего не установлено и не зарегистрировано."
    return 0
  fi
  say ""
  say "Регистрация возможности:"
  local reg_rc=0
  register "$dir" || reg_rc=$?
  if (( reg_rc )); then
    say ""
    say "ОСТАЛОСЬ (возможность поставлена, но во ВСЕ носители не вросла):"
    say "  скилы возможности не собраны — без них правила пакета не сработают."
    say "  адрес: python3 <PROJECT_DIR>/харнес/SBORKA-SKILOV.py"
    say "  (нужен каталог глав UNIFIED рядом — см. 01-SPEC §7-а)"
    return 1
  fi
  say ""
  say "Готово: возможность «$ID» установлена и вросла в носители."
  [[ -r "$dir/${CHECKLIST:-ЧЕК-ЛИСТ.md}" ]] && \
    say "Приёмка возможности: $dir/${CHECKLIST:-ЧЕК-ЛИСТ.md} — до её прохождения статус работы «test», не «done» (И-4)."
  return 0
}

cmd_state() {
  local root d id="${1:-}"
  root=$(caps_dir) || die "каталог возможностей не найден" 2
  for d in "$root"/*/; do
    [[ -f "$d/МАНИФЕСТ.conf" ]] || continue
    ( load_manifest "$d"
      [[ -n "$id" && "$id" != "$ID" ]] && exit 0
      printf '=== %s: %s\n' "$ID" "$(is_installed "$d" && echo УСТАНОВЛЕНА || echo 'не стоит')"
      if is_installed "$d"; then
        sed 's/^/    /' "$(installed_marker "$d")"
        local sha_now sha_was
        sha_now=$(sha256sum "$d/МАНИФЕСТ.conf" | cut -d' ' -f1)
        sha_was=$(sed -n 's/^ПАСПОРТ_SHA256="\(.*\)"$/\1/p' "$(installed_marker "$d")")
        [[ "$sha_now" != "$sha_was" ]] && \
          printf '    ВНИМАНИЕ: паспорт менялся после установки — перечитай МАНИФЕСТ.conf и переставь\n'
      fi
      printf '    скилы: %s\n' "${SKILLS:-—}"
      printf '    запреты сторожу: %s\n' "$([[ -r "$d/запреты.list" ]] && grep -cv '^[[:space:]]*\(#\|$\)' "$d/запреты.list" || echo 0) строк"
    )
  done
}

cmd_remove() {
  local root dir id="$1" proj
  root=$(caps_dir) || die "каталог возможностей не найден" 2
  dir=$(resolve "$root" "$id") || die "нет возможности «$id»" 2
  load_manifest "$dir"
  is_installed "$dir" || die "возможность «$ID» и так не установлена"
  rm -f "$(installed_marker "$dir")"
  say "Метка снята: скилы возможности исчезнут при пересборке, запреты сторожу и"
  say "список уборки перестанут действовать — они читаются только у установленных."
  proj=$(project_dir || true)
  if [[ -n "$proj" ]]; then
    rebuild_skills "$proj" || say "  скилы пересобрать не удалось — сделай это руками"
  fi
  printf '{"ts":"%s","возможность":"%s","событие":"снята"}\n' "$(date -u +%FT%TZ)" "$ID" \
    >> "$(log_dir)/$(caps_log_name)" 2>/dev/null || true
  say ""
  say "На диске ОСТАЛОСЬ (снос — решение владельца, не сессии, И-1):"
  say "  системные пакеты, Android SDK, образы эмулятора, скрипты в ~/bin,"
  say "  записи в память/ и строки в .gitignore. Что именно и как снести —"
  say "  раздел «Снятие» в $dir/ЧЕК-ЛИСТ.md."
}

# ── селфтест: больной и здоровый случай во временном каталоге ────────────────
selftest() {
  local T ok_flag=1
  T=$(mktemp -d)
  # проектный каталог с указателем памяти и заглушкой генератора скилов:
  # заглушка нужна, чтобы ДОКАЗАТЬ, что пересборка вызывается, а не поверить.
  mkdir -p "$T/proj/память" "$T/proj/харнес" "$T/log" "$T/caps/проба/skills-src" \
           "$T/caps/проба/память" "$T/caps/тяжёлая"
  cat > "$T/proj/память/MEMORY.md" <<'EOF'
# MEMORY.md — указатель

| когда происходит вот это | проверить себя |
|---|---|
| было раньше | старая строка |

## Указатель

- [Старая запись](old.md) — что-то
EOF
  cat > "$T/proj/харнес/SBORKA-SKILOV.py" <<'EOF'
import pathlib
pathlib.Path(__file__).with_name("скилы-собраны").write_text("да", encoding="utf-8")
EOF
  cat > "$T/caps/проба/МАНИФЕСТ.conf" <<'EOF'
ID="проба"
NAME="Пробная возможность селфтеста"
CONDITION="никогда — она существует только в тесте"
NEED_DISK_GB=0
NEED_MEM_TOTAL_MB=1
SKILLS="проба-скил"
EOF
  cat > "$T/caps/проба/ustanovit.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == "--dry-run" ]] && { echo "dry"; exit 0; }
: > "$PACK_DIR/.след-установки"
echo "поставлено"
EOF
  cat > "$T/caps/проба/память/проба-запись.md" <<'EOF'
<!-- указатель: [Проба](проба-запись.md) — запись из пробной возможности -->
<!-- таблица: наступил пробный случай | проверить пробу — [[проба-запись]] -->
# Проба
EOF
  printf 'проба.tmp\n' > "$T/caps/проба/gitignore.list"
  # больной случай: жёсткое требование заведомо не выполнить
  cat > "$T/caps/тяжёлая/МАНИФЕСТ.conf" <<'EOF'
ID="тяжёлая"
CONDITION="никогда"
NEED_DISK_GB=999999
EOF
  cat > "$T/caps/тяжёлая/ustanovit.sh" <<'EOF'
#!/usr/bin/env bash
: > "$PACK_DIR/.НЕ-ДОЛЖНО-ПОЯВИТЬСЯ"
EOF

  local env_pre=(env "HARNESS_CAPS_DIR=$T/caps" "HARNESS_PROJECT_DIR=$T/proj"
                 "HARNESS_LOG_DIR=$T/log" "HARNESS_INSTALL_CONF=$T/нет-конфига")

  # 1. список видит непоставленную возможность и её условие
  local out
  out=$("${env_pre[@]}" bash "$0" список)
  grep -q '\[ не стоит  \] проба' <<< "$out" || { echo "SELFTEST: список не показал «не стоит» для пробы"; ok_flag=0; }
  grep -q 'условие: никогда' <<< "$out" || { echo "SELFTEST: список не показал условие включения"; ok_flag=0; }

  # 2. БОЛЬНОЙ СЛУЧАЙ: жёсткое требование не выполнено → установщик не запускался
  local rc=0
  out=$("${env_pre[@]}" bash "$0" поставить тяжёлая 2>&1) || rc=$?
  (( rc == 1 )) || { echo "SELFTEST: непроходимое требование не остановило установку (rc=$rc)"; ok_flag=0; }
  [[ -e "$T/caps/тяжёлая/.НЕ-ДОЛЖНО-ПОЯВИТЬСЯ" ]] && { echo "SELFTEST: установщик запустился при невыполненном требовании"; ok_flag=0; }
  grep -q 'НЕ начиналась' <<< "$out" || { echo "SELFTEST: отказ не сказал, что установка не начиналась"; ok_flag=0; }

  # 3. --dry-run ничего не регистрирует
  "${env_pre[@]}" bash "$0" поставить проба --dry-run >/dev/null
  [[ -f "$T/caps/проба/.установлено" ]] && { echo "SELFTEST: --dry-run поставил метку"; ok_flag=0; }

  # 4. здоровый случай: ставится, врастает
  out=$("${env_pre[@]}" bash "$0" поставить проба 2>&1) || { echo "SELFTEST: установка пробы упала: $out"; ok_flag=0; }
  [[ -f "$T/caps/проба/.след-установки" ]] || { echo "SELFTEST: установщик не выполнился"; ok_flag=0; }
  [[ -f "$T/caps/проба/.установлено" ]] || { echo "SELFTEST: метка не поставлена"; ok_flag=0; }
  [[ -f "$T/proj/память/проба-запись.md" ]] || { echo "SELFTEST: запись памяти не скопирована"; ok_flag=0; }
  grep -q 'наступил пробный случай' "$T/proj/память/MEMORY.md" || { echo "SELFTEST: строка таблицы не вросла"; ok_flag=0; }
  # Раздела «Указатель» больше нет — строка `указатель:` вращиваться НЕ должна.
  # Проверяем именно отсутствие: молчаливое дописывание отрастило бы список заново.
  grep -q '\[Проба\](проба-запись.md)' "$T/proj/память/MEMORY.md" && { echo "SELFTEST: строка указателя вросла, хотя раздела нет"; ok_flag=0; }
  grep -q 'проба.tmp' "$T/proj/.gitignore" || { echo "SELFTEST: строка .gitignore не вросла"; ok_flag=0; }
  [[ -f "$T/proj/харнес/скилы-собраны" ]] || { echo "SELFTEST: пересборка скилов не вызвана"; ok_flag=0; }
  grep -q '"событие":"установлена"' "$T/log/возможности.jsonl" || { echo "SELFTEST: нет записи в реестре"; ok_flag=0; }

  # 5. идемпотентность: повтор не дублирует строк памяти и .gitignore
  local before after
  before=$(wc -l < "$T/proj/память/MEMORY.md"); : > "$T/proj/харнес/скилы-собраны"
  "${env_pre[@]}" bash "$0" поставить проба >/dev/null
  after=$(wc -l < "$T/proj/память/MEMORY.md")
  [[ "$before" == "$after" ]] || { echo "SELFTEST: повторная установка размножила строки памяти ($before → $after)"; ok_flag=0; }
  [[ $(grep -c 'проба.tmp' "$T/proj/.gitignore") == 1 ]] || { echo "SELFTEST: повтор размножил строки .gitignore"; ok_flag=0; }

  # 6. состояние замечает правку паспорта после установки
  printf '\nADDED="после установки"\n' >> "$T/caps/проба/МАНИФЕСТ.conf"
  out=$("${env_pre[@]}" bash "$0" состояние проба)
  grep -q 'паспорт менялся' <<< "$out" || { echo "SELFTEST: правка паспорта после установки не замечена"; ok_flag=0; }

  # 7. снять — метка уходит
  "${env_pre[@]}" bash "$0" снять проба >/dev/null
  [[ -f "$T/caps/проба/.установлено" ]] && { echo "SELFTEST: метка осталась после снятия"; ok_flag=0; }

  rm -rf "$T"
  if (( ok_flag )); then echo "SELFTEST: зелёный (7 проверок, включая больной случай)"; return 0; fi
  return 1
}

# ── разбор аргументов ───────────────────────────────────────────────────────
case "${1:-}" in
  список|list)        cmd_list ;;
  требования)         [[ -n "${2:-}" ]] || die "нужен id: vozmozhnost.sh требования <id>" 2
                      root=$(caps_dir) || die "каталог возможностей не найден" 2
                      dir=$(resolve "$root" "$2") || die "нет возможности «$2»" 2
                      check_requirements "$dir" ;;
  поставить|install)  [[ -n "${2:-}" ]] || die "нужен id: vozmozhnost.sh поставить <id>" 2
                      shift; cmd_install "$@" ;;
  состояние|status)   cmd_state "${2:-}" ;;
  снять|remove)       [[ -n "${2:-}" ]] || die "нужен id: vozmozhnost.sh снять <id>" 2
                      cmd_remove "$2" ;;
  --selftest)         selftest ;;
  ""|-h|--help)
    sed -n '/^# Команды:/,/^# Код возврата/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
  *) die "неизвестная команда «$1» — vozmozhnost.sh --help" 2 ;;
esac
