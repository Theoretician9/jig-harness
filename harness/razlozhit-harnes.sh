#!/usr/bin/env bash
# Раскладчик харнеса на сервер. Откуда взят: написан для этого пакета (этап 3 плана №1).
# Что делает: копирует каталог harness/ в $PROJECT_DIR по конвенциям путей пакета:
#   hooks/   -> $PROJECT_DIR/scripts/hooks/   (объявление: .claude/settings.json репо)
#   scripts/ -> $PROJECT_DIR/scripts/
#   demons/  -> $PROJECT_DIR/harness/demons/   (их ждёт crontab из 01-SPEC-УСТАНОВКА §9)
#   config/harness.conf -> /etc/harness/harness.conf (если ещё нет — не затирает живой)
#   CLAUDE.md, STATE.md, память/, dev-map.yaml, шаблоны-задач/, skills/ -> $PROJECT_DIR/
# Идемпотентен: повторный запуск обновляет код, НЕ трогает живые данные
# (STATE.md, память/, dev-map.yaml копируются только если их ещё нет — они живут у агента).
# Чем проверяется: --selftest раскладывает во временный каталог и сверяет дерево.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${HARNESS_INSTALL_CONF:-/etc/harness/install.conf}"

if [[ "${1:-}" == "--selftest" ]]; then
  T=$(mktemp -d)
  # временный конфиг — больной случай: раскладка не должна требовать sudo и сети
  cat > "$T/install.conf" <<EOF
PROJECT_DIR="$T/proj"
PROJECT_NAME="стенд"
AGENT_USER="$(id -un)"
LOG_DIR="$T/log"
HEARTBEAT_DIR="$T/hb"
EOF
  # Стенд harness.conf: образец пакета, где каталог копий — внутри $T. Без него
  # вложенный вызов читал бы живой /etc/harness/harness.conf, и архив
  # самотеста лёг бы в живой каталог копий (R2-02). Строка ключа ровно одна:
  # иначе неясно, какую из них подменили.
  make_stand_conf() {  # $1=файл стенда, $2=значение BACKUP_DIR или --no-key
    local sample="$HERE/config/harness.conf" lines
    if [[ "$2" == --no-key ]]; then
      grep -v '^BACKUP_DIR=' "$sample" > "$1" || true
      return 0
    fi
    lines=$(grep -c '^BACKUP_DIR=' "$sample" || true)
    [[ "$lines" == 1 ]] || { echo "SELFTEST: стенд harness.conf не собран (строк BACKUP_DIR в образце: $lines)"; exit 1; }
    sed "s|^BACKUP_DIR=.*|BACKUP_DIR=\"$2\"|" "$sample" > "$1"
  }
  make_stand_conf "$T/harness.conf" "$T/копии"
  # bash "$0", не "$0": после scp с Windows exec-бита может не быть,
  # и прямой перезапуск падал бы «Permission denied».
  # HARNESS_SELFTEST=1 — селфтест НИКОГДА не трогает /etc (П-11): без флага
  # прогон под root на машине без /etc/harness/harness.conf клал туда боевой
  # конфиг прямо из теста. Доказательство — ls /etc/harness до/после селфтеста.
  # Каждый вложенный вызов идёт ЗДЕСЬ: BACKUP_DIR=, LOG_DIR= и
  # LAYOUT_LOCK_WAIT_SEC= пустыми — окружение смены не перебивает каталог копий,
  # замки и ожидание стенда (пустое значит «не задано»). С LOG_DIR из окружения
  # стенд брал бы ЖИВОЙ <LOG_DIR>/locks: живой layout.lock и живой update.lock.
  run_stand() {  # $1=install.conf $2=harness.conf, дальше VAR=знач
    env HARNESS_SELFTEST=1 HARNESS_INSTALL_CONF="$1" HARNESS_CONF="$2" BACKUP_DIR= LOG_DIR= \
      LAYOUT_LOCK_WAIT_SEC= "${@:3}" bash "$0"
  }
  run_stand "$T/install.conf" "$T/harness.conf" || { echo "SELFTEST: раскладка упала"; exit 1; }
  ok_flag=1
  for f in "$T/proj/scripts/hooks/guard_bash.py" "$T/proj/scripts/deploy.sh" \
           "$T/proj/harness/demons/heartbeat-watch.sh" "$T/proj/CLAUDE.md" \
           "$T/proj/.claude/settings.json" "$T/proj/harness/skills/task-intake/SKILL.md" \
           "$T/proj/harness/panel/server.py"; do
    [[ -f "$f" ]] || { echo "SELFTEST: нет $f"; ok_flag=0; }
  done
  # БОЛЬНОЙ СЛУЧАЙ C4: перезапись обязана менять inode. cp -f писал в тот же
  # номер файла, а его в этот момент исполняет живой диспетчер канала — bash
  # дочитывал подменённый на ходу скрипт и ломался на середине.
  TARGET_FILE="$T/proj/scripts/hooks/guard_bash.py"
  # Файл ИЗМЕНИЛСЯ — перезапись обязана дать новый inode. Портим копию в
  # проекте: иначе раскладка законно ничего не трогает (см. пробу ниже).
  echo "# правка на сервере" >> "$TARGET_FILE"
  INODE_BEFORE=$(stat -c %i "$TARGET_FILE" 2>/dev/null || echo нет)
  # Случай м): перекрытый файл обязан лечь копией в архив стенда — blob правки.
  archived_copy_check() {  # $1=метка случая, $2=путь в проекте, $3=blob прежнего файла
    local found=0 copy
    while IFS= read -r copy; do
      [[ -n "$copy" && "$(git hash-object "$copy")" == "$3" ]] && found=1
    done < <(find "$T/копии/раскладка" -path "*/files/$2" -type f 2>/dev/null || true)
    if (( found )); then
      echo "  ок    м) $1: прежний $2 лёг копией в архив стенда"
    else
      echo "  ПЛОХО м) $1: копии прежнего $2 (blob ${3:0:12}) в $T/копии/раскладка/*/files/ нет — файл перезаписан без копии"; ok_flag=0
    fi
  }
  C4_WAS=$(git hash-object "$TARGET_FILE")
  run_stand "$T/install.conf" "$T/harness.conf" >/dev/null || true
  INODE_AFTER=$(stat -c %i "$TARGET_FILE" 2>/dev/null || echo нет)
  if [[ "$INODE_BEFORE" != "$INODE_AFTER" ]]; then
    echo "  ок    изменившийся файл перезаписан с новым номером ($INODE_BEFORE → $INODE_AFTER): живой процесс дочитает прежний"
  else
    echo "  ПЛОХО перезапись идёт в тот же номер файла — работающий скрипт рвётся на середине"; ok_flag=0
  fi
  archived_copy_check C4 scripts/hooks/guard_bash.py "$C4_WAS"

  # БОЛЬНОЙ СЛУЧАЙ 12.09.2026: НЕизменившийся файл раскладка трогать не должна.
  # Прежде она копировала всё подряд, mtime обновлялся у каждого файла, и гейт
  # внешних вызовов после каждой раскладки объявлял службы «работают на старом
  # коде» — трижды за смену, с ненужным перезапуском панели и диспетчера.
  MTIME_BEFORE=$(stat -c %Y "$TARGET_FILE" 2>/dev/null || echo нет)
  sleep 1
  run_stand "$T/install.conf" "$T/harness.conf" >/dev/null || true
  MTIME_AFTER=$(stat -c %Y "$TARGET_FILE" 2>/dev/null || echo нет)
  if [[ "$MTIME_BEFORE" == "$MTIME_AFTER" ]]; then
    echo "  ок    БОЛЬНОЙ СЛУЧАЙ: неизменившийся файл не тронут — гейт свежести не будят зря"
  else
    echo "  ПЛОХО раскладка обновила время у неизменившегося файла ($MTIME_BEFORE → $MTIME_AFTER)"; ok_flag=0
  fi

  # генератор скилов на сервере работоспособен: источники PACKAGE: доставлены,
  # главы UNIFIED на месте. Проверяется прогоном, а не наличием файлов.
  if python3 "$T/proj/harness/BUILD-SKILLS.py" >/dev/null 2>"$T/скилы.err"; then :; else
    echo "SELFTEST: пересборка скилов на разложенном сервере упала: $(tr '\n' ' ' < "$T/скилы.err" | cut -c1-200)"; ok_flag=0
  fi
  # возможности доставлены целиком и исполняемы
  for f in "$T/proj/harness/vozmozhnosti/mobilnaya-razrabotka/install.sh" \
           "$T/proj/harness/vozmozhnosti/mobilnaya-razrabotka/МАНИФЕСТ.conf" \
           "$T/proj/harness/vozmozhnosti/mobilnaya-razrabotka/bin/mobile-loop" \
           "$T/proj/scripts/capability.sh"; do
    [[ -f "$f" ]] || { echo "SELFTEST: нет $f"; ok_flag=0; }
  done
  # метка установки — живые данные: повторная доставка кода НЕ должна её снимать,
  # иначе обновление харнеса молча отключало бы установленный набор
  : > "$T/proj/harness/vozmozhnosti/mobilnaya-razrabotka/.установлено"
  run_stand "$T/install.conf" "$T/harness.conf" >/dev/null
  [[ -f "$T/proj/harness/vozmozhnosti/mobilnaya-razrabotka/.установлено" ]] || \
    { echo "SELFTEST: повторная раскладка сняла метку установленной возможности"; ok_flag=0; }
  # .gitignore создан при первой раскладке и с нужными строками
  if [[ -f "$T/proj/.gitignore" ]] && grep -q '__pycache__/' "$T/proj/.gitignore"; then
    :
  else
    echo "SELFTEST: .gitignore не создан или без __pycache__/"; ok_flag=0
  fi
  # идемпотентность: правим STATE.md и .gitignore как «живые», повторная раскладка не затирает
  echo "живое состояние" > "$T/proj/STATE.md"
  echo "живой gitignore" > "$T/proj/.gitignore"
  run_stand "$T/install.conf" "$T/harness.conf" >/dev/null
  grep -q "живое состояние" "$T/proj/STATE.md" || { echo "SELFTEST: затёрт живой STATE.md"; ok_flag=0; }
  # БОЛЬНОЙ СЛУЧАЙ 12.09.2026: cp -f затёр РАБОЧИЕ правила проекта заготовкой
  # пакета. Пока файлы совпадали, это было незаметно; когда заготовка стала
  # английской, пятая раскладка за смену снесла правила (вернули из git).
  echo "живые правила проекта" > "$T/proj/CLAUDE.md"
  run_stand "$T/install.conf" "$T/harness.conf" >/dev/null
  grep -q "живые правила проекта" "$T/proj/CLAUDE.md" \
    || { echo "SELFTEST: затёрт живой CLAUDE.md проекта"; ok_flag=0; }
  [[ -f "$T/proj/harness/CLAUDE.md" ]] \
    || { echo "SELFTEST: эталон harness/CLAUDE.md не доставлен"; ok_flag=0; }
  grep -q "живой gitignore" "$T/proj/.gitignore" || { echo "SELFTEST: затёрт живой .gitignore"; ok_flag=0; }

  # ── БОЛЬНОЙ СЛУЧАЙ: образец harness.conf в проекте ────────────────────────
  # Гейт ключей образца по умолчанию судит <проект>/harness/config/harness.conf,
  # поэтому раскладка обязана заменять его образцом пакета. Образец — не живой
  # конфиг: живой живёт в /etc/harness и не затирается (селфтест /etc не трогает
  # вовсе). Второй случай кладёт в проект образец ПРЕЖНЕЙ версии: «кладу, только
  # если нет» (как pipeline.yaml) зеленел бы на чистой раскладке.
  SAMPLE_PKG="$HERE/config/harness.conf"
  SAMPLE_PROJ="$T/proj/harness/config/harness.conf"
  if [[ -f "$SAMPLE_PKG" ]]; then
    run_stand "$T/install.conf" "$T/harness.conf" >/dev/null || true
    if cmp -s "$SAMPLE_PKG" "$SAMPLE_PROJ"; then
      echo "  ок    раскладка положила образец harness.conf в проект, побайтно равный пакету"
    else
      echo "  ПЛОХО после раскладки $SAMPLE_PROJ не равен образцу пакета ($( [[ -f "$SAMPLE_PROJ" ]] && echo отличается || echo нет файла ))"; ok_flag=0
    fi
    mkdir -p "$(dirname "$SAMPLE_PROJ")"
    { echo "# образец прежней версии харнеса"; sed '$d' "$SAMPLE_PKG"; } > "$SAMPLE_PROJ"
    SAMPLE_WAS=$(git hash-object "$SAMPLE_PROJ")
    run_stand "$T/install.conf" "$T/harness.conf" >/dev/null || true
    if cmp -s "$SAMPLE_PKG" "$SAMPLE_PROJ"; then
      echo "  ок    БОЛЬНОЙ: образец прежней версии в проекте заменён образцом пакета"
    else
      echo "  ПЛОХО БОЛЬНОЙ: образец прежней версии в проекте пережил раскладку — гейт ключей образца судит старый файл"; ok_flag=0
    fi
    archived_copy_check "образец прежней версии" harness/config/harness.conf "$SAMPLE_WAS"
  else
    echo "SELFTEST: в пакете нет config/harness.conf — случай образца в проекте судить не по чему"; ok_flag=0
  fi

  # ── БОЛЬНОЙ СЛУЧАЙ: маски сторожа модели — файл пакета, не настройка ──────
  # Маски «путь → (вид, кто)» лежали в models.yaml, а тот — LIVE_SETTINGS: на
  # установке v0.6.0 раздела не было вовсе, раскладка его не доносила, и сторож
  # модели молча пропускал любую запись. Теперь маски — model-artifacts.yaml,
  # раскладка кладёт его КАЖДЫЙ раз. Второй прогон — поверх ПРЕЖНЕЙ версии
  # файла: «кладу, только если нет» зеленел бы на чистой раскладке. Живая
  # models.yaml при этом цела — её метка переживает обе раскладки.
  ART_PKG="$HERE/config/model-artifacts.yaml"
  ART_PROJ="$T/proj/harness/config/model-artifacts.yaml"
  MODELS_PROJ="$T/proj/harness/config/models.yaml"
  if [[ -f "$ART_PKG" ]]; then
    mkdir -p "$(dirname "$MODELS_PROJ")"
    [[ -f "$MODELS_PROJ" ]] || cp "$HERE/config/models.yaml" "$MODELS_PROJ"
    echo "# метка владельца: живая таблица моделей" >> "$MODELS_PROJ"
    MODELS_BEFORE=$(sha256sum < "$MODELS_PROJ")
    run_stand "$T/install.conf" "$T/harness.conf" >/dev/null || true
    if cmp -s "$ART_PKG" "$ART_PROJ"; then
      echo "  ок    раскладка положила model-artifacts.yaml в проект, побайтно равный пакету"
    else
      echo "  ПЛОХО после раскладки $ART_PROJ не равен файлу пакета ($( [[ -f "$ART_PROJ" ]] && echo отличается || echo нет файла ))"; ok_flag=0
    fi
    { echo "# маски прежней версии харнеса"; sed '$d' "$ART_PKG"; } > "$ART_PROJ"
    ART_WAS=$(git hash-object "$ART_PROJ")
    run_stand "$T/install.conf" "$T/harness.conf" >/dev/null || true
    if cmp -s "$ART_PKG" "$ART_PROJ"; then
      echo "  ок    БОЛЬНОЙ: прежняя версия model-artifacts.yaml в проекте заменена файлом пакета"
    else
      echo "  ПЛОХО БОЛЬНОЙ: прежняя версия model-artifacts.yaml пережила раскладку — сторож судит старые маски"; ok_flag=0
    fi
    archived_copy_check "маски прежней версии" harness/config/model-artifacts.yaml "$ART_WAS"
    if [[ "$(sha256sum < "$MODELS_PROJ")" == "$MODELS_BEFORE" ]]; then
      echo "  ок    живая models.yaml проекта не тронута двумя раскладками"
    else
      echo "  ПЛОХО раскладка изменила живую models.yaml проекта — значения владельца затёрты"; ok_flag=0
    fi
  else
    echo "  ПЛОХО в пакете нет config/model-artifacts.yaml — маскам сторожа модели не с чего доехать до установки"; ok_flag=0
  fi

  # ── БОЛЬНОЙ СЛУЧАЙ: каталог назначения без права записи ───────────────────
  # Улика 11.08.2026: раскладка упала на cp посередине и оставила
  # полуобновлённое дерево. Ждём: отказ ДО первого копирования, названные
  # каталоги, готовая команда chown — и ни одного скопированного файла.
  if [[ $(id -u) -eq 0 ]]; then
    echo "SELFTEST: случай прав ПРОПУЩЕН — под root каталог без права записи невоспроизводим; прогоните от имени агента"
  else
    mkdir -p "$T/закрытый"
    cat > "$T/закрытый.conf" <<EOF
PROJECT_DIR="$T/закрытый/proj"
AGENT_USER="$(id -un)"
LOG_DIR="$T/log"
HEARTBEAT_DIR="$T/hb"
EOF
    mkdir -p "$T/закрытый/proj"
    chmod 500 "$T/закрытый/proj"          # войти можно, писать нельзя
    deny_rc=0
    deny=$(run_stand "$T/закрытый.conf" "$T/harness.conf" 2>&1) || deny_rc=$?
    chmod 700 "$T/закрытый/proj"
    rights_bad=()
    # Код 3, а не просто ≠ 0: update.py читает код 1 как сбой середины и
    # раскладывает прежнюю версию поверх нетронутого проекта.
    [[ $deny_rc -eq 3 ]] || rights_bad+=("раскладка в закрытый каталог ответила кодом $deny_rc (ждали 3 — «не начата»)")
    grep -q "РАСКЛАДКА НЕ НАЧАТА" <<<"$deny" || rights_bad+=("отказ не назвал себя («РАСКЛАДКА НЕ НАЧАТА»): ${deny:0:200}")
    grep -q "sudo chown -R" <<<"$deny" || rights_bad+=("в отказе нет готовой команды chown")
    grep -q "$T/закрытый/proj" <<<"$deny" || rights_bad+=("отказ не назвал каталог")
    # Главное: НИ ОДНОГО файла. Пустой каталог — весь смысл проверки.
    copied=$(find "$T/закрытый/proj" -mindepth 1 | wc -l)
    [[ "$copied" -eq 0 ]] || rights_bad+=("при отказе прав скопировано $copied объектов — дерево полуобновлено")
    if (( ${#rights_bad[@]} == 0 )); then
      echo "  ок    п) БОЛЬНОЙ СЛУЧАЙ: закрытый на запись каталог → отказ кодом 3 до первого cp, скопировано 0 объектов"
    else
      printf '  ПЛОХО п) %s\n' "${rights_bad[@]}"; ok_flag=0
    fi
  fi

  # ══ Ф-а: нет копии — нет перезаписи; одна дверь укладки; всё или ничего ══
  # (hr.layout-manifest-removes-stale, случаи а–м). Каждый случай — свой стенд
  # $T/cases/<имя>: копия разложенного проекта-основы, свой install.conf
  # (PROJECT_NAME="стенд") и свой harness.conf с каталогом копий ВНУТРИ стенда.
  # «Проект не тронут» — множество (путь, тип, inode, режим, blob) ВСЕГО проекта
  # до и после, не один файл.
  make_stand_install() {  # $1=каталог стенда, $2=каталог проекта
    cat > "$1/install.conf" <<EOF
PROJECT_DIR="$2"
PROJECT_NAME="стенд"
AGENT_USER="$(id -un)"
LOG_DIR="$1/log"
HEARTBEAT_DIR="$1/hb"
EOF
  }
  new_case() {  # $1=имя → печатает каталог стенда с копией проекта-основы
    local c="$T/cases/$1"
    mkdir -p "$c"
    cp -a "$T/base/proj" "$c/proj"
    make_stand_install "$c" "$c/proj"
    make_stand_conf "$c/harness.conf" "$c/копии"
    printf '%s\n' "$c"
  }
  project_snapshot() {  # $1=каталог проекта → строка на объект: путь тип inode режим blob
    python3 - "$1" <<'PY'
import hashlib, os, stat, sys
root = sys.argv[1]
rows = []
for top, dirs, files in os.walk(root):
    for name in dirs + files:
        path = os.path.join(top, name)
        st = os.lstat(path)
        if stat.S_ISREG(st.st_mode):
            with open(path, "rb") as fh:
                data = fh.read()
            kind, blob = "f", hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        elif stat.S_ISLNK(st.st_mode):
            kind, blob = "l", "->" + os.readlink(path)
        elif stat.S_ISDIR(st.st_mode):
            kind, blob = "d", "-"
        else:
            kind, blob = "o", "-"
        rows.append(f"{os.path.relpath(path, root)}\t{kind}\t{st.st_ino}\t{stat.S_IMODE(st.st_mode):o}\t{blob}")
print("\n".join(sorted(rows)))
PY
  }
  snapshot_diff() {  # $1, $2 = снимки → число различающихся строк и первые из них
    local n
    # Пустой снимок «до» равен пустому «после» — без проекта судить нечего.
    [[ -s "$1" ]] || { printf 'снимок проекта «до» пуст — множество не судилось\n'; return 0; }
    n=$(diff "$1" "$2" | grep -c '^[<>]' || true)
    (( n == 0 )) && return 0
    printf 'множество проекта изменилось (%s строк): %s\n' "$n" \
      "$(diff "$1" "$2" | grep '^[<>]' | head -3 | tr '\n\t' '; ' || true)"
  }
  count_archives() {  # $1=каталог «раскладка» → сколько архивов в нём
    { find "$1" -mindepth 1 -maxdepth 1 2>/dev/null || true; } | wc -l
  }
  case_result() {  # $1=метка, $2=что судим, дальше — беды; без бед — «ок»
    local label="$1" what="$2"; shift 2
    if (( $# == 0 )); then
      echo "  ок    $label $what"
    else
      local item
      for item in "$@"; do echo "  ПЛОХО $label $what: $item"; done
      ok_flag=0
    fi
  }
  expect_texts() {  # $1=вывод, дальше — подстроки, которые обязаны быть; печатает недостающие
    local text="$1" want; shift
    for want in "$@"; do
      grep -qF -- "$want" <<<"$text" || printf 'в выводе нет «%s»\n' "$want"
    done
  }
  forbid_texts() {  # $1=вывод, дальше — подстроки, которых быть не должно
    local text="$1" bad_word; shift
    for bad_word in "$@"; do
      grep -qF -- "$bad_word" <<<"$text" && printf 'в выводе есть «%s»\n' "$bad_word"
    done
    return 0
  }
  collect() {  # строки stdin → в массив bad
    local line
    while IFS= read -r line; do [[ -n "$line" ]] && bad+=("$line"); done
    return 0
  }
  PKG_DEPLOY_BLOB=$(git hash-object "$HERE/scripts/deploy.sh")
  foreign_deploy() {  # $1=проект: чужой scripts/deploy.sh, режим 700
    printf '#!/bin/sh\n# свой deploy.sh этой установки — его теряет перезапись без копии\n' > "$1/scripts/deploy.sh"
    chmod 700 "$1/scripts/deploy.sh"
  }

  mkdir -p "$T/base" "$T/cases"
  make_stand_install "$T/base" "$T/base/proj"
  make_stand_conf "$T/base/harness.conf" "$T/base/копии"
  run_stand "$T/base/install.conf" "$T/base/harness.conf" >/dev/null 2>&1 \
    || { echo "SELFTEST: раскладка проекта-основы для случаев а–л упала — случаи не судимы"; exit 1; }

  # а) перекрытие с копией. Ловит: копия не снимается; копия без режима
  #    (644/755 вместо 700); журнал без шапки, без «готово» или без «итог».
  C=$(new_case a); P="$C/proj"; bad=()
  foreign_deploy "$P"
  was=$(git hash-object "$P/scripts/deploy.sh")
  rc=0; out=$(run_stand "$C/install.conf" "$C/harness.conf" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  [[ "$(git hash-object "$P/scripts/deploy.sh")" == "$PKG_DEPLOY_BLOB" ]] || bad+=("в проекте не пакетный scripts/deploy.sh")
  arch=$(find "$C/копии/раскладка" -mindepth 1 -maxdepth 1 -type d -name 'стенд-*' 2>/dev/null || true)
  n_arch=$(grep -c . <<<"$arch" || true)
  if [[ "$n_arch" != 1 ]]; then
    bad+=("архивов $C/копии/раскладка/стенд-*: $n_arch (ждали 1)")
  else
    copy="$arch/files/scripts/deploy.sh"
    if [[ -f "$copy" ]]; then
      [[ "$(git hash-object "$copy")" == "$was" ]] || bad+=("blob копии не равен прежнему")
      [[ "$(stat -c %a "$copy")" == 700 ]] || bad+=("режим копии $(stat -c %a "$copy") (ждали 700)")
    else
      bad+=("копии files/scripts/deploy.sh в архиве нет")
    fi
    j="$arch/journal.tsv"
    if [[ -f "$j" ]]; then
      grep -qE '^# прогон [0-9]+ [^ ]+ план=[0-9]+ перекрыть=1$' <(head -1 "$j") || bad+=("шапка журнала не «# прогон <pid> <режим> план=N перекрыть=1»: $(head -1 "$j")")
      grep -qxF "$(printf 'перекрыт\t%s\t%s\tscripts/deploy.sh' "$was" "$PKG_DEPLOY_BLOB")" "$j" || bad+=("в журнале нет «перекрыт <было> <стало> scripts/deploy.sh»")
      grep -qxF "$(printf 'готово\t-\t-\tscripts/deploy.sh')" "$j" || bad+=("в журнале нет «готово … scripts/deploy.sh»")
      grep -qE $'^итог\t-\t-\t[0-9]+$' <(tail -1 "$j") || bad+=("последняя строка журнала не «итог»: $(tail -1 "$j")")
      overlaps=$(grep -c $'^перекрыт\t' "$j" || true)
      [[ "$overlaps" == 1 ]] || bad+=("строк «перекрыт» $overlaps (ждали 1 — отличался один файл)")
    else
      bad+=("журнала $arch/journal.tsv нет")
    fi
  fi
  case_result "а)" "чужой scripts/deploy.sh (700) перекрыт пакетным, прежний — копией в архиве с blob и режимом, журнал полный" ${bad[@]+"${bad[@]}"}

  # б) архив недоступен — всё или ничего. В проекте один изменённый и два
  #    удалённых файла пакета (полуразложить ЕСТЬ что) и метка .установлено.
  #    Ловит: новые файлы легли раньше первой беды; метка «снята» переносом и
  #    не вернулась; отказ кодом 1; строка «раскладка завершена» при отказе.
  C=$(new_case b); P="$C/proj"; bad=()
  : > "$C/файл"
  make_stand_conf "$C/harness.conf" "$C/файл/копии"
  foreign_deploy "$P"
  rm -f "$P/scripts/hooks/guard_bash.py" "$P/harness/demons/heartbeat-watch.sh"
  : > "$P/harness/vozmozhnosti/mobilnaya-razrabotka/.установлено"
  project_snapshot "$P" > "$C/до"
  rc=0; out=$(run_stand "$C/install.conf" "$C/harness.conf" 2>&1) || rc=$?
  project_snapshot "$P" > "$C/после"
  [[ $rc -eq 3 ]] || bad+=("код $rc (ждали 3)")
  collect < <(expect_texts "$out" "РАСКЛАДКА НЕ НАЧАТА" "копию снять некуда" "Not a directory")
  collect < <(forbid_texts "$out" "раскладка завершена" "дверь укладки:")
  collect < <(snapshot_diff "$C/до" "$C/после")
  make_stand_conf "$C/harness.conf" "$C/копии"
  rc=0; out=$(run_stand "$C/install.conf" "$C/harness.conf" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("пара с годным BACKUP_DIR: код $rc (ждали 0)")
  case_result "б)" "BACKUP_DIR внутри обычного файла → код 3, проект не тронут; пара с годным ключом — код 0" ${bad[@]+"${bad[@]}"}

  # в) нет места: журнал архива — ссылка на /dev/full. Ловит: ошибка записи
  #    журнала проглочена; журнал открыт с O_NOFOLLOW (чужая причина — ELOOP).
  C=$(new_case v); P="$C/proj"; bad=()
  foreign_deploy "$P"
  mkdir -p "$C/копии/раскладка/в"
  ln -s /dev/full "$C/копии/раскладка/в/journal.tsv"
  project_snapshot "$P" > "$C/до"
  rc=0; out=$(run_stand "$C/install.conf" "$C/harness.conf" HARNESS_LAYOUT_ARCHIVE="$C/копии/раскладка/в" 2>&1) || rc=$?
  project_snapshot "$P" > "$C/после"
  [[ $rc -eq 3 ]] || bad+=("код $rc (ждали 3)")
  collect < <(expect_texts "$out" "РАСКЛАДКА НЕ НАЧАТА" "журнал не записан: No space left on device")
  collect < <(forbid_texts "$out" "раскладка завершена")
  collect < <(snapshot_diff "$C/до" "$C/после")
  rc=0; out=$(run_stand "$C/install.conf" "$C/harness.conf" HARNESS_LAYOUT_ARCHIVE="$C/копии/раскладка/в2" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("пара без ссылки на /dev/full: код $rc (ждали 0)")
  case_result "в)" "журнал на /dev/full → код 3, «журнал не записан», проект не тронут; пара — код 0" ${bad[@]+"${bad[@]}"}

  # г) короткая запись журнала живёт в пробе двери (её случай D4): python под
  #    RLIMIT_FSIZE получает «записано 100 из 200» без исключения. Ловит: число
  #    записанных байт не сверяется.
  bad=()
  door_probe="$HERE/scripts/test_layout_door.py"
  if [[ -f "$door_probe" ]]; then
    door_out=$(env BACKUP_DIR= PYTHONDONTWRITEBYTECODE=1 python3 "$door_probe" 2>&1 || true)
    grep -qE '^  ок +D4( |$)' <<<"$door_out" \
      || bad+=("в выводе пробы двери нет «  ок    D4»: $(grep -E 'D4' <<<"$door_out" | head -1 || true)")
  else
    bad+=("пробы двери $door_probe нет")
  fi
  case_result "г)" "короткая запись журнала — случай D4 пробы двери зелёный" ${bad[@]+"${bad[@]}"}

  # д) обрыв на середине: SIGKILL после второй записи в проект. Ловит: строка
  #    журнала после os.replace (изменённый путь вне журнала); «положен-новый»
  #    не пишется при заданном архиве (R2-08).
  C=$(new_case d); P="$C/proj"; bad=()
  foreign_deploy "$P"
  rm -f "$P/scripts/hooks/guard_bash.py" "$P/harness/demons/heartbeat-watch.sh"
  project_snapshot "$P" > "$C/до"
  rc=0; out=$(run_stand "$C/install.conf" "$C/harness.conf" HARNESS_LAYOUT_ARCHIVE="$C/копии/раскладка/д" \
                HARNESS_LAYOUT_KILL_AFTER_WRITE=2 2>&1) || rc=$?
  project_snapshot "$P" > "$C/после"
  [[ $rc -eq 137 ]] || bad+=("код $rc (ждали 137 — SIGKILL пробной двери)")
  collect < <(python3 - "$C/до" "$C/после" "$C/копии/раскладка/д" <<'PY'
import hashlib, sys
from pathlib import Path
def files(snap):
    out = {}
    for line in Path(snap).read_text(encoding="utf-8").splitlines():
        path, kind, _ino, mode, blob = line.split("\t")
        if kind == "f":
            out[path] = (blob, mode)
    return out
before, after, archive = files(sys.argv[1]), files(sys.argv[2]), Path(sys.argv[3])
changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
journal = archive / "journal.tsv"
if not journal.is_file():
    print(f"журнала {journal} нет")
    sys.exit()
rows = [line.split("\t") for line in journal.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")]
if any(r[0] == "итог" for r in rows):
    print("в журнале оборванного прогона есть «итог»")
named = {r[3] for r in rows if len(r) == 4 and r[0] in ("перекрыт", "положен-новый")}
if len(changed) != 2:
    print(f"изменённых путей {len(changed)} (ждали 2 — обрыв после второй записи): {sorted(changed)[:4]}")
outside = sorted(changed - named)
if outside:
    print(f"изменены вне журнала: {outside[:4]}")
overlapped = [r for r in rows if len(r) == 4 and r[0] == "перекрыт"]
if not any(r[3] in changed for r in overlapped):
    print("среди изменённых нет ни одной строки «перекрыт» — копия не судилась")
for r in overlapped:
    copy = archive / "files" / r[3]
    if not copy.is_file():
        print(f"копии files/{r[3]} нет")
        continue
    data = copy.read_bytes()
    blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    if blob != r[1] or blob != before.get(r[3], ("-",))[0]:
        print(f"копия files/{r[3]}: blob {blob[:12]}, в журнале было {r[1][:12]}")
PY
)
  case_result "д)" "обрыв после 2-й записи: итога нет, каждый изменённый путь в журнале, копия перекрытого с blob-было" ${bad[@]+"${bad[@]}"}

  # е) повтор той же версии: ни один файл не переписан. План берётся у двери
  #    (HARNESS_LAYOUT_PLAN_OUT); пустой план — провал. Кроме файлов плана
  #    судится всё дерево вне harness/skills/ (его пересобирает генератор):
  #    укладка мимо двери (cp -rf UNIFIED, scripts/lib) ставит новый mtime
  #    файлам, которых в плане нет вовсе.
  C=$(new_case e); P="$C/proj"; bad=()
  mtime_snapshot() {  # $1=проект → путь inode mtime_ns обычных файлов
    python3 - "$1" <<'PY'
import os, stat, sys
root = sys.argv[1]
for top, _dirs, files in os.walk(root):
    for name in files:
        path = os.path.join(top, name)
        st = os.lstat(path)
        if stat.S_ISREG(st.st_mode):
            print(f"{os.path.relpath(path, root)}\t{st.st_ino}\t{st.st_mtime_ns}")
PY
  }
  mtime_snapshot "$P" > "$C/до"
  archives_before=$(count_archives "$C/копии/раскладка")
  sleep 1
  rc=0; out=$(run_stand "$C/install.conf" "$C/harness.conf" HARNESS_LAYOUT_PLAN_OUT="$C/план.tsv" 2>&1) || rc=$?
  mtime_snapshot "$P" > "$C/после"
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0)")
  archives_after=$(count_archives "$C/копии/раскладка")
  [[ "$archives_after" == "$archives_before" ]] || bad+=("архивов прибавилось $((archives_after - archives_before)) (ждали 0)")
  collect < <(python3 - "$C/план.tsv" "$C/до" "$C/после" "$HERE" "$P" <<'PY'
import os, sys
from pathlib import Path
plan_file, before_f, after_f, here, proj = map(Path, sys.argv[1:])
def load(f):
    out = {}
    for line in f.read_text(encoding="utf-8").splitlines():
        path, ino, mtime = line.split("\t")
        out[path] = (ino, mtime)
    return out
before, after = load(before_f), load(after_f)
if not plan_file.is_file():
    print(f"плана {plan_file} нет — дверь его не отдала (HARNESS_LAYOUT_PLAN_OUT)")
    records = []
else:
    records = [r.split("\t") for r in plan_file.read_text(encoding="utf-8").split("\0") if r]
if not records:
    print("план пуст — судить нечего, а пустое множество не доказательство")
bad_rec = [r for r in records if len(r) != 3]
if bad_rec:
    print(f"записей плана не из трёх полей: {len(bad_rec)}")
dests = {r[2]: r[0] for r in records if len(r) == 3}
for dest, group in sorted(dests.items()):
    if group == "skills":
        continue
    if dest not in before or before[dest] != after.get(dest):
        print(f"файл плана {dest} ({group}): mtime/inode {before.get(dest)} → {after.get(dest)}")
        break
# Множество из пакета-стенда: каждый файл harness/scripts/** обязан быть в плане.
if records:
    missing = []
    for top, dirs, files in os.walk(here / "scripts"):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            rel = "scripts/" + str((Path(top) / name).relative_to(here / "scripts"))
            if rel not in dests:
                missing.append(rel)
    if missing:
        print(f"файлов пакета harness/scripts/ нет в плане: {len(missing)}, например {missing[:3]}")
skip = ("harness/skills/",)
moved = sorted(p for p in before.keys() & after.keys()
               if not p.startswith(skip) and "/__pycache__/" not in p
               and not p.endswith(".установлено") and before[p] != after[p])
if moved:
    print(f"переписаны повтором (вне harness/skills/): {len(moved)}, например {moved[:3]}")
PY
)
  case_result "е)" "повтор той же версии: mtime и inode прежние у файлов плана и всего дерева вне skills, архив +0" ${bad[@]+"${bad[@]}"}

  # ж) самотест целится в живое: каталог копий — второй mktemp вне $T. Ловит:
  #    снято правило стенда — самотест пишет архив вне стенда (на машине — в
  #    живой каталог копий). Живой каталог НЕ подставляется: признак тот же.
  C=$(new_case zh); P="$C/proj"; bad=()
  OUTSIDE=$(mktemp -d)
  make_stand_conf "$C/harness.conf" "$OUTSIDE"
  foreign_deploy "$P"
  project_snapshot "$P" > "$C/до"
  rc=0; out=$(run_stand "$C/install.conf" "$C/harness.conf" 2>&1) || rc=$?
  project_snapshot "$P" > "$C/после"
  [[ $rc -eq 3 ]] || bad+=("код $rc (ждали 3)")
  collect < <(expect_texts "$out" "самотест целится в живой каталог копий")
  collect < <(snapshot_diff "$C/до" "$C/после")
  outside_left=$(find "$OUTSIDE" -mindepth 1 | wc -l)
  [[ "$outside_left" == 0 ]] || bad+=("в каталоге вне стенда после прогона объектов: $outside_left (ждали 0)")
  rm -rf "$OUTSIDE"
  case_result "ж)" "BACKUP_DIR вне стенда → код 3, проект не тронут, каталог вне стенда пуст" ${bad[@]+"${bad[@]}"}

  # з) нет ключа. Ловит: умолчание пути (вторая дверь); ключ читается всегда —
  #    тогда пара (тот же конфиг БЕЗ ключа, пустой проект) отказывает.
  C=$(new_case z); P="$C/proj"; bad=()
  make_stand_conf "$C/harness.conf" --no-key
  foreign_deploy "$P"
  project_snapshot "$P" > "$C/до"
  rc=0; out=$(run_stand "$C/install.conf" "$C/harness.conf" 2>&1) || rc=$?
  project_snapshot "$P" > "$C/после"
  [[ $rc -eq 3 ]] || bad+=("код $rc (ждали 3)")
  collect < <(expect_texts "$out" "РАСКЛАДКА НЕ НАЧАТА" "ключ BACKUP_DIR пуст в $C/harness.conf")
  collect < <(snapshot_diff "$C/до" "$C/после")
  mkdir -p "$C/pair"
  make_stand_install "$C/pair" "$C/pair/proj"
  rc=0; out=$(run_stand "$C/pair/install.conf" "$C/harness.conf" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("пара (пустой проект, конфиг без ключа): код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  [[ -f "$C/pair/proj/scripts/deploy.sh" ]] || bad+=("пара: пустой проект не разложен")
  stray=$(find "$C" \( -name копии -o -name раскладка \) 2>/dev/null | head -3 || true)
  [[ -z "$stray" ]] || bad+=("пара: архив создан, хотя перекрывать нечего: $stray")
  case_result "з)" "без ключа BACKUP_DIR → код 3 с путём конфига; пустой проект на том же конфиге — код 0, архива нет" ${bad[@]+"${bad[@]}"}

  # и) группа skills: её пересобирает генератор, копия не снимается. Ловит:
  #    skills с копией — архив на каждой раскладке (R2-17). Зелёный и до кода;
  #    живость даёт мутация «skills с копией». Равенство пакету судится, потому
  #    что замер 19.10: вывод BUILD-SKILLS.py побайтно равен harness/skills пакета.
  C=$(new_case i); P="$C/proj"; bad=()
  skill="$P/harness/skills/task-intake/SKILL.md"
  echo "# правка скила на сервере" >> "$skill"
  edited=$(git hash-object "$skill")
  archives_before=$(count_archives "$C/копии/раскладка")
  rc=0; out=$(run_stand "$C/install.conf" "$C/harness.conf" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0)")
  [[ "$(git hash-object "$skill")" != "$edited" ]] || bad+=("правка скила пережила раскладку")
  cmp -s "$skill" "$HERE/skills/task-intake/SKILL.md" || bad+=("SKILL.md не равен пакету")
  archives_after=$(count_archives "$C/копии/раскладка")
  [[ "$archives_after" == "$archives_before" ]] || bad+=("архивов прибавилось $((archives_after - archives_before)) (ждали 0)")
  case_result "и)" "правленый harness/skills/task-intake/SKILL.md перекрыт без копии, архив +0" ${bad[@]+"${bad[@]}"}

  # к) остаток оборванной раскладки рядом с изменённым файлом — перезаписан
  #    своим временным и исчезает, и назван. Ловит: остаток не судится; убран rm.
  C=$(new_case k); P="$C/proj"; bad=()
  foreign_deploy "$P"
  echo "обрывок прошлой раскладки" > "$P/scripts/.deploy.sh.layout-tmp"
  rc=0; out=$(run_stand "$C/install.conf" "$C/harness.conf" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0)")
  [[ ! -e "$P/scripts/.deploy.sh.layout-tmp" ]] || bad+=("остаток scripts/.deploy.sh.layout-tmp пережил раскладку")
  collect < <(expect_texts "$out" "остаток оборванной раскладки" "scripts/.deploy.sh.layout-tmp")
  case_result "к)" "остаток .deploy.sh.layout-tmp у изменённого файла назван и исчез" ${bad[@]+"${bad[@]}"}

  # л) каталог на месте файла плана. Ловит: тип назначения не судится —
  #    нынешний mv -f молча кладёт файл ВНУТРЬ каталога с кодом 0 (замер 19.10).
  C=$(new_case l); P="$C/proj"; bad=()
  rm -f "$P/scripts/deploy.sh"
  mkdir "$P/scripts/deploy.sh"
  project_snapshot "$P" > "$C/до"
  rc=0; out=$(run_stand "$C/install.conf" "$C/harness.conf" 2>&1) || rc=$?
  project_snapshot "$P" > "$C/после"
  [[ $rc -eq 3 ]] || bad+=("код $rc (ждали 3)")
  collect < <(expect_texts "$out" "РАСКЛАДКА НЕ НАЧАТА" "назначение не обычный файл:" "scripts/deploy.sh")
  collect < <(forbid_texts "$out" "раскладка завершена")
  collect < <(snapshot_diff "$C/до" "$C/после")
  case_result "л)" "каталог scripts/deploy.sh/ на месте файла → код 3, путь назван, проект не тронут" ${bad[@]+"${bad[@]}"}

  # н) чистая установка без своего CLAUDE.md, дверь отказывает: шаги мимо двери
  #    идут только после её успеха (спека §3.4). Ловит: «один раз»-укладка
  #    корневого CLAUDE.md во время сбора плана — «проект не тронут» лжёт.
  #    Случай б) этого не видит: его проект-основа уже несёт CLAUDE.md.
  C=$(new_case n); P="$C/proj"; bad=()
  rm -f "$P/CLAUDE.md"
  : > "$C/файл"
  make_stand_conf "$C/harness.conf" "$C/файл/копии"
  foreign_deploy "$P"
  project_snapshot "$P" > "$C/до"
  rc=0; out=$(run_stand "$C/install.conf" "$C/harness.conf" 2>&1) || rc=$?
  project_snapshot "$P" > "$C/после"
  [[ $rc -eq 3 ]] || bad+=("код $rc (ждали 3)")
  [[ ! -e "$P/CLAUDE.md" ]] || bad+=("корневой CLAUDE.md лёг при отказе двери")
  collect < <(expect_texts "$out" "РАСКЛАДКА НЕ НАЧАТА")
  collect < <(forbid_texts "$out" "CLAUDE.md создан")
  collect < <(snapshot_diff "$C/до" "$C/после")
  case_result "н)" "проект без CLAUDE.md, дверь отказала → код 3, CLAUDE.md не создан, проект не тронут" ${bad[@]+"${bad[@]}"}

  # о) пакет без каталога, который раскладка кладёт безусловно (skills-src):
  #    раскладка останавливается до двери, проект не тронут. Ловит: ошибка find
  #    в `< <(find …)` под set -e не видна — пустая группа и код 0, а пересборка
  #    скилов на сервере падает позже «нет файла пакета skills-src/…».
  C=$(new_case o); P="$C/proj"; bad=()
  mkdir -p "$C/пакет"
  cp -a "$HERE" "$C/пакет/harness"
  rm -rf "$C/пакет/harness/skills-src"
  foreign_deploy "$P"
  project_snapshot "$P" > "$C/до"
  rc=0
  out=$(env HARNESS_SELFTEST=1 HARNESS_INSTALL_CONF="$C/install.conf" HARNESS_CONF="$C/harness.conf" \
          BACKUP_DIR= LOG_DIR= LAYOUT_LOCK_WAIT_SEC= bash "$C/пакет/harness/razlozhit-harnes.sh" 2>&1) || rc=$?
  project_snapshot "$P" > "$C/после"
  [[ $rc -ne 0 ]] || bad+=("код 0 — раскладка прошла без каталога пакета skills-src")
  collect < <(expect_texts "$out" "$C/пакет/harness/skills-src")
  collect < <(forbid_texts "$out" "дверь укладки:" "раскладка завершена")
  collect < <(snapshot_diff "$C/до" "$C/после")
  case_result "о)" "в пакете нет skills-src → раскладка остановлена до двери, путь назван, проект не тронут" ${bad[@]+"${bad[@]}"}

  # ══ Ф-б: опись раскладки <проект>/.layout-manifest — случаи фб-а…фб-и ══════
  # Опись судится здесь своим разбором байт, не модулем описи: самотест не верит
  # тому, кого проверяет. Пакеты-копии A/B/… лежат в $FB; проект каждого случая —
  # свой. Порог LAYOUT_REMOVE_MAX_PERCENT окружения смены обнуляется (run_pkg).
  FB="$T/fb"
  mkdir -p "$FB"
  LIVE_NAMES=$(sed -n 's/^LIVE_SETTINGS="\([^"]*\)".*/\1/p' "$0" | head -1)
  cat > "$FB/manifest.py" <<'PY'
import hashlib, sys
from pathlib import Path

def blob(path):
    data = Path(path).read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

def sections(project):
    path = Path(project) / ".layout-manifest"
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8").split("\n")
    cut = lines.index("# оставлено") if "# оставлено" in lines else len(lines)
    return lines, cut

def body(project):
    """Тело описи строками «путь\\tblob\\tгруппа»; описи нет — ни строки."""
    found = sections(project)
    if found is None:
        return
    lines, cut = found
    for line in lines[2:cut]:
        fields = line.split("\t")
        if len(fields) == 3:
            print(f"{fields[2]}\t{fields[0]}\t{fields[1]}")

def expected(plan_out, *live):
    """Ожидаемое тело по плану двери: без группы skills и без имён LIVE_SETTINGS."""
    skip = {f"harness/config/{name}" for name in live}
    for record in Path(plan_out).read_bytes().decode("utf-8").split("\0"):
        if not record:
            continue
        group, src, dest = record.split("\t")
        if group != "skills" and dest not in skip:
            print(f"{dest}\t{blob(src)}\t{group}")

def add(project, *pairs):
    """Дописать в тело строки (путь, группа) с blob файла проекта, тело — по пути."""
    found = sections(project)
    if found is None:
        sys.exit("описи нет — дописать нечего")
    lines, cut = found
    rows = [line for line in lines[2:cut] if line]
    rows += [f"{blob(Path(project) / rel)}\t{group}\t{rel}" for rel, group in zip(pairs[::2], pairs[1::2])]
    rows.sort(key=lambda line: line.split("\t")[2].encode("utf-8"))
    (Path(project) / ".layout-manifest").write_text("\n".join(lines[:2] + rows + lines[cut:]), encoding="utf-8")

{"body": body, "expected": expected, "add": add}[sys.argv[1]](*sys.argv[2:])
PY
  manifest_body() { python3 -B "$FB/manifest.py" body "$1" | LC_ALL=C sort; }
  # После «--» — доводы самого раскладчика (--снять-сверх-порога): до «--» всё
  # уходит в окружение, и довод без разделителя стал бы именем для env.
  run_pkg() {  # $1=каталог harness пакета-копии, $2=каталог стенда, дальше VAR=знач [-- доводы]
    local envs=() args=() after=0 item
    for item in "${@:3}"; do
      if (( after )); then args+=("$item")
      elif [[ "$item" == -- ]]; then after=1
      else envs+=("$item"); fi
    done
    env HARNESS_SELFTEST=1 HARNESS_INSTALL_CONF="$2/install.conf" HARNESS_CONF="$2/harness.conf" \
      BACKUP_DIR= LOG_DIR= LAYOUT_LOCK_WAIT_SEC= LAYOUT_REMOVE_MAX_PERCENT= ${envs[@]+"${envs[@]}"} \
      bash "$1/razlozhit-harnes.sh" ${args[@]+"${args[@]}"}
  }
  fb_stand() {  # $1=имя, $2=проект-образец или пусто → каталог стенда
    local c="$T/cases/$1"
    mkdir -p "$c"
    if [[ -n "${2:-}" ]]; then cp -a "$2" "$c/proj"; else mkdir -p "$c/proj"; fi
    make_stand_install "$c" "$c/proj"
    make_stand_conf "$c/harness.conf" "$c/копии"
    printf '%s\n' "$c"
  }
  summary_line() {  # $1=вывод → строка «опись: в описи …» или пусто
    grep -m1 '^опись: в описи ' <<<"$1" || true
  }
  # Пакеты-копии. A — пакет и scripts/old.sh; главы рядом (из проекта-основы:
  # второго искателя UNIFIED не заводим). B — A без old.sh, с scripts/new.sh.
  # B5 — A без old.sh и четырёх демонов. Ag — A без hooks/guard_bash.py.
  # NoU — A без глав рядом (ни один из кандидатов поиска не существует).
  mkdir -p "$FB/A" "$FB/B" "$FB/B5" "$FB/Ag" "$FB/NoU"
  cp -a "$HERE" "$FB/A/harness"
  printf '#!/bin/sh\necho старый скрипт пакета A\n' > "$FB/A/harness/scripts/old.sh"
  chmod 755 "$FB/A/harness/scripts/old.sh"
  OLD_BLOB=$(git hash-object "$FB/A/harness/scripts/old.sh")
  if [[ -d "$T/base/proj/UNIFIED" ]]; then cp -a "$T/base/proj/UNIFIED" "$FB/A/UNIFIED"; fi
  for k in B B5 Ag NoU; do cp -a "$FB/A/harness" "$FB/$k/harness"; done
  for k in B B5 Ag; do
    if [[ -d "$FB/A/UNIFIED" ]]; then cp -a "$FB/A/UNIFIED" "$FB/$k/UNIFIED"; fi
  done
  rm -f "$FB/B/harness/scripts/old.sh" "$FB/B5/harness/scripts/old.sh" "$FB/Ag/harness/hooks/guard_bash.py"
  printf '#!/bin/sh\necho новый скрипт пакета B\n' > "$FB/B/harness/scripts/new.sh"
  chmod 755 "$FB/B/harness/scripts/new.sh"
  mapfile -t FB_DEMONS < <(find "$FB/A/harness/demons" -maxdepth 1 -type f -name '*.sh' -printf '%f\n' | LC_ALL=C sort | head -4)
  for f in "${FB_DEMONS[@]}"; do rm -f "$FB/B5/harness/demons/$f"; done

  # фб-а) первая раскладка в пустой проект: тело описи = план двери без skills
  #   и без LIVE_SETTINGS, число строк — по плану стенда. Ловит: опись не
  #   пишется; skills или живые настройки в описи; лишняя/потерянная строка.
  #   Зелёным оставит: blob описи взят из ПРОЕКТА, а не из пакета (на первой
  #   раскладке они равны) — это судит M3/D23.
  C=$(fb_stand fb-a); P="$C/proj"; bad=()
  rc=0; out=$(run_pkg "$HERE" "$C" HARNESS_LAYOUT_PLAN_OUT="$C/план.tsv" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  if [[ -s "$C/план.tsv" ]]; then
    python3 -B "$FB/manifest.py" expected "$C/план.tsv" $LIVE_NAMES | LC_ALL=C sort > "$C/ждали"
    tr '\0' '\n' < "$C/план.tsv" | cut -f1,3 > "$C/назначения"
  else
    bad+=("плана двери $C/план.tsv нет — тело судить не с чем"); : > "$C/ждали"; : > "$C/назначения"
  fi
  manifest_body "$P" > "$C/тело"
  n_want=$(wc -l < "$C/ждали")
  [[ -f "$P/.layout-manifest" ]] || bad+=("описи $P/.layout-manifest нет")
  (( n_want > 0 )) || bad+=("ожидаемое тело пусто — пустое множество не доказательство")
  grep -q $'^skills\t' "$C/назначения" || bad+=("в плане стенда нет группы skills — её исключение не судится")
  [[ -n "$LIVE_NAMES" ]] || bad+=("LIVE_SETTINGS в $0 не найден")
  for name in $LIVE_NAMES; do
    grep -qxF "$(printf 'config\tharness/config/%s' "$name")" "$C/назначения" \
      || bad+=("harness/config/$name не шёл через дверь — исключение живой настройки не судится")
    if grep -q "^harness/config/$name"$'\t' "$C/тело"; then bad+=("живая настройка harness/config/$name в описи"); fi
  done
  n_diff=$(diff "$C/ждали" "$C/тело" | grep -c '^[<>]' || true)
  (( n_diff == 0 )) || bad+=("тело ≠ план без skills и LIVE_SETTINGS, различий $n_diff: $(diff "$C/ждали" "$C/тело" | grep '^[<>]' | head -3 | cut -f1 | tr '\n' ' ' || true)")
  [[ "$(head -1 "$P/.layout-manifest" 2>/dev/null || true)" == "# layout-manifest 2" ]] || bad+=("первая строка описи не «# layout-manifest 2»")
  grep -qE '^# пакет ([0-9a-f]{40}|-)$' <(sed -n 2p "$P/.layout-manifest" 2>/dev/null || true) || bad+=("вторая строка описи не «# пакет <коммит|->»")
  collect < <(expect_texts "$out" "опись заведена: $n_want " "лишнее начну считать со следующей раскладки")
  case_result "фб-а)" "первая раскладка: опись = план двери без skills и LIVE_SETTINGS ($n_want строк)" ${bad[@]+"${bad[@]}"}

  # фб-б) повтор той же версии: опись не переписана (R1-18). Ловит: опись
  #   пишется на каждой раскладке (новый inode/mtime). Зелёным оставит: опись
  #   переписана теми же байтами в тот же inode — судит D24 двери.
  bad=()
  st1=$(stat -c '%i %.9Y' "$P/.layout-manifest" 2>/dev/null || echo нет)
  [[ "$st1" != нет ]] || bad+=("описи после фб-а) нет — «не переписана» совпало бы с «не было»")
  rc=0; out=$(run_pkg "$HERE" "$C" 2>&1) || rc=$?
  st2=$(stat -c '%i %.9Y' "$P/.layout-manifest" 2>/dev/null || echo нет)
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0)")
  [[ "$st1" == "$st2" ]] || bad+=("inode/mtime описи: $st1 → $st2")
  case_result "фб-б)" "повтор той же версии: inode и mtime описи прежние" ${bad[@]+"${bad[@]}"}

  # Проект после раскладки пакетом A — образец для фб-г, фб-д, фб-е, фб-з.
  A1=$(fb_stand fb-A1)
  a1_rc=0; a1_out=$(run_pkg "$FB/A/harness" "$A1" 2>&1) || a1_rc=$?
  A1_BAD=()
  [[ $a1_rc -eq 0 ]] || A1_BAD+=("стенд: раскладка пакетом A — код $a1_rc: $(tail -2 <<<"$a1_out" | tr '\n' ' ')")
  manifest_body "$A1/proj" > "$A1/тело"
  grep -q "^scripts/old.sh"$'\t'"$OLD_BLOB"$'\t' "$A1/тело" \
    || A1_BAD+=("стенд: после пакета A в описи нет scripts/old.sh с его blob — снятие судить не по чему")

  # фб-в) A → B: old.sh убран из пакета, порог образца (25 %) его пропускает —
  #   в Ф-в он снят. Ловит: снято не то или больше одного пути; снятая строка
  #   осталась в теле описи (следующая раскладка назовёт её «уже не было»).
  #   Зелёным оставит: снятие без копии в архив — судит фв-а).
  C=$(fb_stand fb-v); P="$C/proj"; bad=()
  rc=0; out=$(run_pkg "$FB/A/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("прогон 1 (пакет A): код $rc")
  old_row=$(manifest_body "$P" | grep "^scripts/old.sh"$'\t' || true)
  [[ -n "$old_row" ]] || bad+=("после прогона 1 scripts/old.sh нет в описи — снятие судить не по чему")
  project_snapshot "$P" | cut -f1,2,4,5 > "$C/до"
  rc=0; out=$(run_pkg "$FB/B/harness" "$C" 2>&1) || rc=$?
  project_snapshot "$P" | cut -f1,2,4,5 > "$C/после"
  [[ $rc -eq 0 ]] || bad+=("прогон 2 (пакет B): код $rc: $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(expect_texts "$out" "снято 1: scripts/old.sh")
  changed=$(diff "$C/до" "$C/после" | grep '^[<>]' | cut -c3- | cut -f1 | LC_ALL=C sort -u \
            | grep -vxF -e scripts/new.sh -e scripts/old.sh -e .layout-manifest || true)
  [[ -z "$changed" ]] || bad+=("прогон 2 изменил пути кроме scripts/new.sh, scripts/old.sh и .layout-manifest: $(tr '\n' ' ' <<<"$changed" | cut -c1-300)")
  [[ ! -e "$P/scripts/old.sh" ]] || bad+=("scripts/old.sh на месте — снятия не было")
  manifest_body "$P" > "$C/тело-после"
  if grep -q "^scripts/old.sh"$'\t' "$C/тело-после"; then bad+=("снятый scripts/old.sh остался в теле описи"); fi
  case_result "фб-в)" "A → B: «снято 1: scripts/old.sh», old.sh снят и вне тела, проект кроме old.sh, new.sh и описи не тронут" ${bad[@]+"${bad[@]}"}

  # фб-г) A → пакет без глав рядом: строки UNIFIED/ не «снято», остаются в
  #   теле. Ловит: «источник не найден» читается как «убрали» (массовое
  #   снятие 43 глав). Зелёным оставит: ветки panel/vozmozhnosti/README/plugin
  #   без источника — копия пакета несёт их всегда (Р15).
  C=$(fb_stand fb-g "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  manifest_body "$P" | grep '^UNIFIED/' > "$C/главы-до" || true
  n_u=$(wc -l < "$C/главы-до")
  (( n_u > 0 )) || bad+=("после пакета A строк UNIFIED/ в описи 0 — случай зелен был бы без предохранителя (Р8)")
  rc=0; out=$(run_pkg "$FB/NoU/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  grep -qF "каталог UNIFIED рядом с пакетом не найден" <<<"$out" || bad+=("стенд: пакет без глав их всё-таки нашёл — случай не судим")
  grep -qE '^опись: в описи [0-9]+; снято 0;' <<<"$(summary_line "$out")" || bad+=("нет «опись: в описи N; снято 0;»: $(summary_line "$out")")
  collect < <(expect_texts "$out" "группа unified: источник не найден — её $n_u ")
  manifest_body "$P" | grep '^UNIFIED/' > "$C/главы-после" || true
  cmp -s "$C/главы-до" "$C/главы-после" || bad+=("строки UNIFIED/ в теле изменились: было $n_u, стало $(wc -l < "$C/главы-после")")
  case_result "фб-г)" "пакет без UNIFIED: снято 0, $n_u строк UNIFIED/ в теле, группа названа" ${bad[@]+"${bad[@]}"}

  # Порог стенда — половина доли 5 снимаемых файлов в описи: 5 из N это 500/N %,
  # а целый «1 %» перестаёт быть меньше доли, когда тело описи дорастает до 500.
  half_share_percent() {  # $1=строк в теле описи → порог в процентах, строго меньше доли 5 из N
    awk -v n="$1" 'BEGIN { printf "%g", 250 / n }'
  }

  # фб-д) порог: половина доли 5 из N, B5 убирает 5 файлов (доля > порога).
  #   Ловит: порог не читается из harness.conf стенда; при пороге всё равно
  #   «снято»; отложенные выпадают из тела. Зелёным оставит: «>» вместо «>=»
  #   — судит M6 (граница 37,5).
  C=$(fb_stand fb-d "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  five=("scripts/old.sh")
  for f in "${FB_DEMONS[@]}"; do five+=("harness/demons/$f"); done
  manifest_body "$P" > "$C/тело-до"
  n_prior=$(wc -l < "$C/тело-до")
  pct=$(half_share_percent "$n_prior")
  sed -i '/^LAYOUT_REMOVE_MAX_PERCENT=/d' "$C/harness.conf"
  echo "LAYOUT_REMOVE_MAX_PERCENT=\"$pct\"" >> "$C/harness.conf"
  : > "$C/пять-до"
  for rel in "${five[@]}"; do grep "^$rel"$'\t' "$C/тело-до" >> "$C/пять-до" || true; done
  [[ ${#five[@]} -eq 5 && $(wc -l < "$C/пять-до") -eq 5 ]] || bad+=("стенд: из 5 убранных в описи $(wc -l < "$C/пять-до") — порог судить не по чему")
  rc=0; out=$(run_pkg "$FB/B5/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(expect_texts "$out" "порог: снять 5 из $n_prior (" "LAYOUT_REMOVE_MAX_PERCENT=$pct" "отложено")
  grep -qE '^опись: в описи [0-9]+; снято 0;' <<<"$(summary_line "$out")" || bad+=("нет «опись: в описи N; снято 0;»: $(summary_line "$out")")
  manifest_body "$P" > "$C/тело-после"
  while IFS= read -r row; do
    grep -qxF "$row" "$C/тело-после" || bad+=("отложенная строка выпала из тела: ${row%%$'\t'*}")
  done < "$C/пять-до"
  for rel in "${five[@]}"; do [[ -f "$P/$rel" ]] || bad+=("$rel снят с диска — снятия в Ф-б нет"); done
  case_result "фб-д)" "порог $pct % при 5 из $n_prior: снято 0, порог назван, 5 строк в теле" ${bad[@]+"${bad[@]}"}

  # фб-е) запретный список в прежней описи: ярус 1 (STATE.md, память/x.md,
  #   harness/config/pipeline.yaml) — «опись испорчена» и вон из тела; ярус 2
  #   (scripts/hooks/guard_bash.py, убран из пакета Ag) — «не снимается», в теле.
  #   Ловит: запретное попадает в «снято». Зелёным оставит: .claude/settings.json
  #   вне плана — его источник безусловен, без него раскладка даёт код 3; судит M8.
  C=$(fb_stand fb-e "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  mkdir -p "$P/память"; echo "запись памяти этой установки" > "$P/память/x.md"
  tier1=(STATE.md память/x.md harness/config/pipeline.yaml)
  for rel in "${tier1[@]}"; do [[ -f "$P/$rel" ]] || bad+=("стенд: нет $P/$rel"); done
  python3 -B "$FB/manifest.py" add "$P" STATE.md scripts память/x.md scripts harness/config/pipeline.yaml config \
    || bad+=("стенд: строки яруса 1 в опись не дописаны")
  grep -q '^scripts/hooks/guard_bash.py'$'\t' <(manifest_body "$P") || bad+=("стенд: scripts/hooks/guard_bash.py нет в описи после A")
  rc=0; out=$(run_pkg "$FB/Ag/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(expect_texts "$out" "опись испорчена: STATE.md" "опись испорчена: память/x.md" \
                "опись испорчена: harness/config/pipeline.yaml" "не снимается: scripts/hooks/guard_bash.py")
  grep -qE '^опись: в описи [0-9]+; снято 0;' <<<"$(summary_line "$out")" || bad+=("в «снято» есть запретное: $(summary_line "$out")")
  manifest_body "$P" > "$C/тело-после"
  for rel in "${tier1[@]}"; do
    if grep -q "^$rel"$'\t' "$C/тело-после"; then bad+=("ярус 1 $rel остался в теле"); fi
  done
  grep -q '^scripts/hooks/guard_bash.py'$'\t' "$C/тело-после" || bad+=("ярус 2 scripts/hooks/guard_bash.py выпал из тела")
  case_result "фб-е)" "ярус 1 назван «опись испорчена» и вне тела, ярус 2 «не снимается» и в теле, снято 0" ${bad[@]+"${bad[@]}"}

  # фб-ж) проект — worktree: строка /.layout-manifest ровно одна в exclude общего
  #   .git после двух раскладок, git status описи не видит. Пара — проект-подкаталог
  #   чужого репозитория: его exclude побайтно прежний. Ловит: запись в чужой
  #   репозиторий; вторая строка на повторе; путь exclude от worktree. Зелёным
  #   оставит: git < 2.31 без --path-format — здесь 2.43, судит ревью (Р9).
  C="$T/cases/fb-zh"; mkdir -p "$C"; bad=()
  stand_git() {
    env GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_AUTHOR_NAME=стенд GIT_AUTHOR_EMAIL=stand@example.invalid \
        GIT_COMMITTER_NAME=стенд GIT_COMMITTER_EMAIL=stand@example.invalid git "$@"
  }
  { stand_git init -q "$C/main" && stand_git -C "$C/main" commit -q --allow-empty -m стенд \
      && stand_git -C "$C/main" worktree add -q "$C/wt"; } >/dev/null 2>&1 || bad+=("стенд: worktree не собран")
  make_stand_install "$C" "$C/wt"
  make_stand_conf "$C/harness.conf" "$C/копии"
  rc=0; out1=$(run_pkg "$HERE" "$C" 2>&1) || rc=$?
  rc2=0; out2=$(run_pkg "$HERE" "$C" 2>&1) || rc2=$?
  [[ $rc -eq 0 && $rc2 -eq 0 ]] || bad+=("коды $rc/$rc2 (ждали 0/0)")
  excl=$(stand_git -C "$C/wt" rev-parse --path-format=absolute --git-path info/exclude 2>/dev/null || true)
  [[ "$excl" == "$C/main/.git/"* ]] || bad+=("стенд: exclude worktree не в общем .git: $excl")
  n_line=$(grep -cxF '/.layout-manifest' "$excl" 2>/dev/null || true)
  [[ "$n_line" == 1 ]] || bad+=("строк /.layout-manifest в $excl: ${n_line:-0} (ждали 1)")
  [[ -f "$C/wt/.layout-manifest" ]] || bad+=("описи в worktree нет — «status не видит» судить не по чему")
  n_seen=$(stand_git -C "$C/wt" status --porcelain --untracked-files=all 2>/dev/null | grep -c 'layout-manifest' || true)
  [[ "$n_seen" == 0 ]] || bad+=("git status worktree видит опись ($n_seen строк)")
  collect < <(expect_texts "$out1" "info/exclude: строка /.layout-manifest дописана в ")
  collect < <(expect_texts "$out2" "info/exclude: строка /.layout-manifest уже есть в ")
  stand_git init -q "$C/outer" >/dev/null 2>&1 || bad+=("стенд: чужой репозиторий не собран")
  mkdir -p "$C/outer/sub/proj" "$C/pair"
  ex_outer="$C/outer/.git/info/exclude"
  [[ -f "$ex_outer" ]] || { mkdir -p "$(dirname "$ex_outer")"; printf '# стенд\n' > "$ex_outer"; }
  sum_before=$(sha256sum < "$ex_outer")
  make_stand_install "$C/pair" "$C/outer/sub/proj"
  make_stand_conf "$C/pair/harness.conf" "$C/pair/копии"
  rc=0; out=$(run_pkg "$HERE" "$C/pair" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("пара: код $rc (ждали 0)")
  [[ "$(sha256sum < "$ex_outer")" == "$sum_before" ]] || bad+=("пара: exclude чужого репозитория изменён")
  collect < <(expect_texts "$out" "info/exclude не тронут: проект не корень репозитория")
  case_result "фб-ж)" "worktree: одна строка в общем exclude, status описи не видит; подкаталог чужого репо — exclude не тронут" ${bad[@]+"${bad[@]}"}

  # фб-з) harness.conf стенда без строки ключа, лишнее есть: код 0, снятие
  #   отложено (решение Ф-в 3), old.sh на месте, «не задан». Ловит: нет ключа —
  #   раскладка падает, молчит о лишнем или снимает без порога. Зелёным оставит:
  #   ключ из окружения при отсутствии в файле — по правилу config.py окружение
  #   старше только ключа, стоящего в файле.
  C=$(fb_stand fb-z "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  sed -i '/^LAYOUT_REMOVE_MAX_PERCENT=/d' "$C/harness.conf"
  n_key=$(grep -c 'LAYOUT_REMOVE_MAX_PERCENT' "$C/harness.conf" || true)
  [[ "$n_key" == 0 ]] || bad+=("стенд: строка ключа осталась в $C/harness.conf ($n_key)")
  rc=0; out=$(run_pkg "$FB/B/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(expect_texts "$out" "ключ LAYOUT_REMOVE_MAX_PERCENT не задан — отложено 1")
  grep -qE '^опись: в описи [0-9]+; снято 0;.*; отложено 1' <<<"$(summary_line "$out")" \
    || bad+=("нет «опись: в описи N; снято 0; … отложено 1»: $(summary_line "$out" | cut -c1-300)")
  [[ "$(git hash-object "$P/scripts/old.sh" 2>/dev/null || true)" == "$OLD_BLOB" ]] || bad+=("scripts/old.sh снят без ключа порога")
  case_result "фб-з)" "без ключа порога: код 0, снято 0, отложено 1, old.sh на месте, «не задан — отложено 1»" ${bad[@]+"${bad[@]}"}

  # фб-и) запретный список модуля описи ↔ LIVE_SETTINGS раскладчика, в обе
  #   стороны. Ловит: новое имя в LIVE_SETTINGS без яруса 1 (ляжет в опись и
  #   станет «снято»). Зелёным оставит: маска fnmatch на harness/config/* —
  #   именами она не считается; судит M12.
  bad=()
  collect < <(python3 -B - "$HERE/scripts/lib/layout_manifest.py" "$LIVE_NAMES" <<'PY'
import importlib.util, sys
from pathlib import Path
path, live = Path(sys.argv[1]), set(sys.argv[2].split())
if not live:
    print("LIVE_SETTINGS раскладчика пуст или не найден")
if not path.is_file():
    print(f"нет модуля {path}")
    sys.exit()
try:
    spec = importlib.util.spec_from_file_location("layout_manifest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
except Exception as error:      # a broken module is a red case, not a trace
    print(f"модуль не загрузился: {type(error).__name__}: {error}")
    sys.exit()
data = getattr(module, "FORBIDDEN_NOT_LISTED", None)
if data is None:
    print("в модуле нет FORBIDDEN_NOT_LISTED")
    sys.exit()
def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    else:
        for item in value:
            yield from strings(item)
prefix = "harness/config/"
names = {s[len(prefix):] for s in strings(data)
         if s.startswith(prefix) and not any(c in s for c in "*?[") and "/" not in s[len(prefix):] and s != prefix}
if not names:
    print("в FORBIDDEN_NOT_LISTED нет ни одного имени harness/config/* — сверять не с чем")
for name in sorted(live - names):
    print(f"LIVE_SETTINGS {name} не в запретном — ляжет в опись")
for name in sorted(names - live):
    print(f"harness/config/{name} в запретном, но не в LIVE_SETTINGS — раскладка его затирает")
PY
)
  case_result "фб-и)" "FORBIDDEN_NOT_LISTED (harness/config/*) == LIVE_SETTINGS, оба непусты" ${bad[@]+"${bad[@]}"}

  # фб-к) info/exclude записать нельзя (ревью кода Ф-б R1-01): файлы уже легли,
  #   поэтому раскладка обязана дойти до конца кодом 0 и назвать отказ словом.
  #   Ловит: отказ записи под set -e роняет раскладчик кодом 1 (update.py
  #   откатывает обновление из-за строки игнора), шаги после двери не идут.
  #   Зелёным оставит: отказ git rev-parse — судит пара фб-ж.
  C="$T/cases/fb-k"; mkdir -p "$C"; bad=()
  stand_git init -q "$C/proj" >/dev/null 2>&1 || bad+=("стенд: репозиторий не собран")
  ex_k="$C/proj/.git/info/exclude"
  if [[ $(id -u) -eq 0 ]]; then
    # root пишет и в файл без права записи: отказ ставим формой — info не каталог.
    rm -rf "$C/proj/.git/info"; printf 'не каталог\n' > "$C/proj/.git/info"; ex_k="$C/proj/.git/info"
  else
    mkdir -p "$(dirname "$ex_k")"; [[ -f "$ex_k" ]] || printf '# стенд\n' > "$ex_k"
    chmod 444 "$ex_k"
  fi
  sum_k=$(sha256sum < "$ex_k" 2>/dev/null || echo нет)
  make_stand_install "$C" "$C/proj"
  make_stand_conf "$C/harness.conf" "$C/копии"
  rc=0; out=$(run_pkg "$HERE" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(expect_texts "$out" "info/exclude не тронут" "раскладка завершена: ")
  [[ -f "$C/proj/.layout-manifest" ]] || bad+=("описи нет — дверь не дошла, случай не судит exclude")
  [[ -f "$C/proj/.gitignore" ]] || bad+=("шаг после exclude не выполнен: .gitignore проекта не создан")
  [[ "$(sha256sum < "$ex_k" 2>/dev/null || echo нет)" == "$sum_k" ]] || bad+=("exclude без права записи изменён")
  chmod u+w "$ex_k" 2>/dev/null || true
  case_result "фб-к)" "info/exclude не записать: код 0, «info/exclude не тронут», раскладка завершена" ${bad[@]+"${bad[@]}"}

  # фб-к2) хук commit-msg (n-6 хакатона): без него суд хакатона по трейлеру
  #   Dev-Map в проекте не действует. Ловит: строка ln пропала, цель не та,
  #   цель неисполняема, хук роняет обычный коммит. pre-commit-гейт стенда не
  #   предмет и снимается со стенда перед коммитом.
  C=$(fb_stand fb-k2); P="$C/proj"; bad=()
  stand_git init -q "$P" >/dev/null 2>&1 || bad+=("стенд: репозиторий не собран")
  rc=0; out=$(run_pkg "$HERE" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  link=$(readlink "$P/.git/hooks/commit-msg" 2>/dev/null || echo нет)
  [[ "$link" == ../../scripts/commit-msg-gate.sh ]] || bad+=(".git/hooks/commit-msg → «$link», ждали ../../scripts/commit-msg-gate.sh")
  [[ -x "$P/.git/hooks/commit-msg" && -f "$(readlink -f "$P/.git/hooks/commit-msg")" ]] || bad+=("цель commit-msg нет или неисполняема")
  rm -f "$P/.git/hooks/pre-commit"
  printf 'обычная правка\n' > "$P/hook-probe.md"
  stand_git -C "$P" add hook-probe.md >/dev/null 2>&1
  rc=0; out=$(HARNESS_INSTALL_CONF="$C/install.conf" HARNESS_CONF="$C/harness.conf" \
    stand_git -C "$P" commit -q -m "обычный коммит" 2>&1) || rc=$?
  [[ $rc -eq 0 && "$link" != нет ]] || bad+=("обычный коммит через хук commit-msg: код $rc $(tail -2 <<<"$out" | tr '\n' ' ')")
  case_result "фб-к2)" "хук commit-msg — симлинк на scripts/commit-msg-gate.sh, цель исполняема, обычный коммит проходит" ${bad[@]+"${bad[@]}"}

  # фб-л) пакет без panel/, vozmozhnosti/, README-ПЛАГИН.md и .claude-plugin/
  #   (ревью кода Ф-б R1-04): строки четырёх групп «источник не найден», в теле,
  #   «снято 0». Ловит: назначение plan_absent каталога без «/» (точное
  #   совпадение вместо префикса — 20 файлов возможностей в «снято», порог 25 %
  #   их пропустит), ветка без plan_absent. Зелёным оставит: группа без строк в
  #   описи — стенд требует строк каждой группы больше нуля.
  C=$(fb_stand fb-l "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  NOX="$FB/NoX"; mkdir -p "$NOX"
  cp -a "$FB/A/harness" "$NOX/harness"
  if [[ -d "$FB/A/UNIFIED" ]]; then cp -a "$FB/A/UNIFIED" "$NOX/UNIFIED"; fi
  rm -rf "$NOX/harness/panel" "$NOX/harness/vozmozhnosti" "$NOX/harness/README-ПЛАГИН.md" "$NOX/harness/.claude-plugin"
  manifest_body "$P" > "$C/тело-до"
  for pre in harness/panel/ harness/vozmozhnosti/ "harness/README-ПЛАГИН.md"$'\t' harness/.claude-plugin/; do
    n_pre=$(grep -c "^$pre" "$C/тело-до" || true)
    (( n_pre > 0 )) || bad+=("стенд: строк ${pre%$'\t'} в описи после пакета A 0 — предохранитель не судится")
  done
  rc=0; out=$(run_pkg "$NOX/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  grep -qE '^опись: в описи [0-9]+; снято 0;' <<<"$(summary_line "$out")" || bad+=("нет «опись: в описи N; снято 0;»: $(summary_line "$out" | cut -c1-300)")
  collect < <(expect_texts "$out" "группа panel: источник не найден" "группа vozmozhnosti: источник не найден" \
                "группа plugin: источник не найден")
  manifest_body "$P" > "$C/тело-после"
  while IFS= read -r row; do
    grep -qxF "$row" "$C/тело-после" || bad+=("строка группы без источника выпала из тела: ${row%%$'\t'*}")
  done < <(grep -E '^(harness/panel/|harness/vozmozhnosti/|harness/README-ПЛАГИН\.md'$'\t''|harness/\.claude-plugin/)' "$C/тело-до" || true)
  case_result "фб-л)" "пакет без panel, vozmozhnosti, README-ПЛАГИН.md, .claude-plugin: снято 0, строки в теле, группы названы" ${bad[@]+"${bad[@]}"}

  # фб-м) коммит пакета в шапке описи (ревью кода Ф-б R1-07): копия пакета — свой
  #   репозиторий с коммитом → «# пакет <его sha>»; пара — копия внутри чужого
  #   репозитория → «# пакет -». Ловит: коммит пакета не вычисляется (всегда «-»),
  #   довод --package-commit не передан, взят sha чужого репозитория (сверка
  #   toplevel снята). Зелёным оставит: HEAD пакета с незакоммиченной правкой —
  #   шапка называет коммит, не дерево (так и задумано).
  C=$(fb_stand fb-m); P="$C/proj"; bad=()
  G="$FB/G"; mkdir -p "$G"; cp -a "$HERE" "$G/harness"
  # Главы рядом, как у пакета A: без них пустой проект не соберёт скилы (код 1 стенда).
  if [[ -d "$FB/A/UNIFIED" ]]; then cp -a "$FB/A/UNIFIED" "$G/UNIFIED"; fi
  sha_g=""
  { stand_git init -q "$G" && stand_git -C "$G" add -A && stand_git -C "$G" commit -q -m стенд; } >/dev/null 2>&1 \
    && sha_g=$(stand_git -C "$G" rev-parse HEAD 2>/dev/null) || bad+=("стенд: репозиторий пакета-копии не собран")
  [[ "$sha_g" =~ ^[0-9a-f]{40}$ ]] || bad+=("стенд: sha пакета-копии не получен: «$sha_g»")
  rc=0; out=$(run_pkg "$G/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  line2=$(sed -n 2p "$P/.layout-manifest" 2>/dev/null || true)
  [[ -n "$sha_g" && "$line2" == "# пакет $sha_g" ]] || bad+=("вторая строка описи «$line2», ждали «# пакет $sha_g»")
  C2=$(fb_stand fb-m2); O="$FB/outer-pkg"; mkdir -p "$O/pkg"; cp -a "$HERE" "$O/pkg/harness"
  if [[ -d "$FB/A/UNIFIED" ]]; then cp -a "$FB/A/UNIFIED" "$O/pkg/UNIFIED"; fi
  sha_o=""
  { stand_git init -q "$O" && stand_git -C "$O" add -A && stand_git -C "$O" commit -q -m чужой; } >/dev/null 2>&1 \
    && sha_o=$(stand_git -C "$O" rev-parse HEAD 2>/dev/null) || bad+=("пара: чужой репозиторий не собран")
  rc=0; out=$(run_pkg "$O/pkg/harness" "$C2" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("пара: код $rc (ждали 0)")
  line2=$(sed -n 2p "$C2/proj/.layout-manifest" 2>/dev/null || true)
  [[ "$line2" == "# пакет -" ]] || bad+=("пара: пакет внутри чужого репозитория — вторая строка «$line2», ждали «# пакет -» (чужой sha ${sha_o:0:12})")
  case_result "фб-м)" "коммит пакета: свой репозиторий — «# пакет <sha>», внутри чужого — «# пакет -»" ${bad[@]+"${bad[@]}"}

  # ══ Ф-в: снятие по описи, журнал «снят», замок на весь прогон — фв-а…фв-и ══
  # Стенд каждого случая — копия проекта после пакета A ($A1) со своим
  # harness.conf и своим LOG_DIR (install.conf стенда: <стенд>/log). Порог —
  # "100" строкой в harness.conf стенда (кроме фв-и): снятие ждётся. Опись и
  # журнал судит помощник probe_tool.py своим разбором, не модулем описи.
  cat > "$FB/probe_tool.py" <<'PY'
import fcntl, hashlib, os, sys, time
from pathlib import Path

def blob(path):
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

def sections(project):
    path = Path(project) / ".layout-manifest"
    lines = path.read_text(encoding="utf-8").split("\n") if path.is_file() else []
    cut = lines.index("# оставлено") if "# оставлено" in lines else len(lines)
    return lines[2:cut], lines[cut + 1:]

def body_paths(project):
    return {f[2] for f in (line.split("\t") for line in sections(project)[0]) if len(f) == 3}

def tree(project):
    """(путь, blob) файлов в корнях укладки — первых компонентах путей тела описи."""
    root = Path(project)
    for top in sorted({p.split("/", 1)[0] for p in body_paths(project) if "/" in p}):
        for dirpath, dirs, files in os.walk(root / top):
            # __pycache__ рождают питоновские шаги после двери, не снятие.
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                path = Path(dirpath) / name
                mark = "->" + os.readlink(path) if path.is_symlink() else blob(path)
                yield str(path.relative_to(root)), mark

def pairs(project):
    for rel, mark in sorted(tree(project)):
        print(f"{rel}\t{mark}")

def outside(project):
    listed = body_paths(project)
    for rel, mark in sorted(tree(project)):
        if rel not in listed:
            print(f"{rel}\t{mark}")

def kept(project):
    for line in sections(project)[1]:
        if line:
            print(line)

def journal(path, rel, want_blob):
    """Беды строками: «снят <blob> - <путь>» до «готово», итог считает снятие."""
    j = Path(path)
    if not j.is_file():
        print(f"журнала {j} нет")
        return
    rows = [l.split("\t") for l in j.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
    at = [i for i, r in enumerate(rows) if r == ["снят", want_blob, "-", rel]]
    done = [i for i, r in enumerate(rows) if r == ["готово", "-", "-", rel]]
    if not at:
        print(f"в журнале нет «снят {want_blob[:12]}… - {rel}»")
    if not done:
        print(f"в журнале нет «готово - - {rel}»")
    if at and done and not any(d > at[0] for d in done):
        print(f"«готово» {rel} раньше «снят» — строка после действия")
    acts = sum(1 for r in rows if r[0] in ("перекрыт", "положен-новый", "снят"))
    last = rows[-1] if rows else [""]
    if last[0] != "итог" or len(last) != 4:
        print(f"последняя строка журнала не «итог»: {last}")
    elif last[3] != str(acts):
        print(f"итог {last[3]}, а строк перекрыт+положен-новый+снят {acts}")

def holder(lock, ready, release):
    """Держит flock на файле, пока не появится release (не дольше 30 с)."""
    Path(lock).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    Path(ready + ".tmp").write_text(str(os.getpid()), encoding="utf-8")
    os.replace(ready + ".tmp", ready)
    for _ in range(300):
        if Path(release).exists():
            return
        time.sleep(0.1)

{"pairs": pairs, "outside": outside, "kept": kept, "journal": journal,
 "holder": holder}[sys.argv[1]](*sys.argv[2:])
PY
  probe_tool() { python3 -B "$FB/probe_tool.py" "$@"; }
  set_conf_key() {  # $1=harness.conf стенда $2=ключ $3=значение|--drop → беды строками
    local file="$1" key="$2" n
    sed -i "/^$key=/d" "$file"
    [[ "$3" == --drop ]] || printf '%s="%s"\n' "$key" "$3" >> "$file"
    n=$(grep -c "^$key=" "$file" || true)
    if [[ "$3" == --drop ]]; then
      [[ "$n" == 0 ]] || printf 'стенд: строка %s осталась в %s (%s)\n' "$key" "$file" "$n"
    else
      [[ "$n" == 1 ]] || printf 'стенд: строк %s в %s — %s (ждали 1)\n' "$key" "$file" "$n"
    fi
    return 0
  }
  archive_list() {  # $1=стенд → каталоги архивов стенда, по строке
    { find "$1/копии/раскладка" -mindepth 1 -maxdepth 1 -type d 2>/dev/null || true; } | LC_ALL=C sort
  }
  new_archive() {  # $1=список архивов «до» $2=стенд → архивы, появившиеся после
    LC_ALL=C comm -13 <(printf '%s\n' "$1" | LC_ALL=C sort) <(archive_list "$2")
  }
  archive_files() {  # $1=каталог архива → пути в его files/, по строке
    [[ -d "$1/files" ]] || return 0
    { find "$1/files" -type f -printf '%P\n' || true; } | LC_ALL=C sort
  }
  wait_file() {  # $1=файл $2=предел в десятых секунды → 0, когда файл непуст
    local i
    for ((i = 0; i < $2; i++)); do [[ -s "$1" ]] && return 0; sleep 0.1; done
    return 1
  }
  seconds_since() {  # $1=метка date +%s.%N → прошло секунд, одна цифра после точки
    awk -v a="$1" -v b="$(date +%s.%N)" 'BEGIN { printf "%.1f", b - a }'
  }
  # Держатель назван В СТРОКЕ маркера (update.py отдаёт владельцу только её) и только
  # он: раскладчик, не закрывший свой дескриптор, назвал бы держателем себя и расспрос.
  holder_in_marker() {  # $1=вывод $2=pid держателя → беда строкой
    local marker named
    marker=$(grep -m1 -F 'РАСКЛАДКА НЕ НАЧАТА: идёт другая раскладка' <<<"$1" || true)
    named=$(sed -n 's/.*держатель \([0-9][0-9, ]*\).*/\1/p' <<<"$marker" \
              | tr -cs '0-9' '\n' | sed '/^$/d' | LC_ALL=C sort -u | tr '\n' ' ' || true)
    [[ "$named" == "$2 " ]] || printf 'строка маркера называет держателей «%s» (ждали ровно %s): %s\n' \
      "${named% }" "$2" "${marker:-строки маркера нет}"
    return 0
  }
  kept_part() {  # $1=строка сводки → часть «оставлено …» до «; уже не было»
    sed -n 's/.*; \(оставлено [^;]*\);.*/\1/p' <<<"$1"
  }
  # R — пакет A без scripts/old.sh: единственное отличие от A — снятие, так что
  # архив нужен только ради него (фв-г, фв-д).
  mkdir -p "$FB/R"
  cp -a "$FB/A/harness" "$FB/R/harness"
  if [[ -d "$FB/A/UNIFIED" ]]; then cp -a "$FB/A/UNIFIED" "$FB/R/UNIFIED"; fi
  rm -f "$FB/R/harness/scripts/old.sh"

  # фв-а) A → B: old.sh перенесён в files/ архива с прежними blob и режимом, в
  #   проекте его нет, журнал «снят» ДО «готово», итог считает снятие; чужой
  #   scripts/own-local.sh вне описи цел. Ловит: mv → rm (копии нет); «снят»
  #   после переноса; снятие вне итога; снят файл вне описи. Зелёным оставит:
  #   обрыв между строкой «снят» и переносом — судит фв-г′).
  C=$(fb_stand fv-a "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  printf '#!/bin/sh\n# свой скрипт этой установки, вне описи\n' > "$P/scripts/own-local.sh"
  own_blob=$(git hash-object "$P/scripts/own-local.sh")
  old_mode=$(stat -c %a "$P/scripts/old.sh" 2>/dev/null || echo нет)
  before=$(archive_list "$C")
  rc=0; out=$(run_pkg "$FB/B/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  [[ ! -e "$P/scripts/old.sh" ]] || bad+=("scripts/old.sh остался в проекте")
  [[ "$(git hash-object "$P/scripts/own-local.sh" 2>/dev/null || true)" == "$own_blob" ]] \
    || bad+=("чужой scripts/own-local.sh вне описи изменён или снят")
  arch=$(new_archive "$before" "$C")
  n_arch=$(grep -c . <<<"$arch" || true)
  if [[ "$n_arch" != 1 ]]; then
    bad+=("новых архивов $n_arch (ждали 1)")
  else
    copy="$arch/files/scripts/old.sh"
    if [[ -f "$copy" ]]; then
      [[ "$(git hash-object "$copy")" == "$OLD_BLOB" ]] || bad+=("blob files/scripts/old.sh не равен прежнему")
      [[ "$(stat -c %a "$copy")" == "$old_mode" ]] || bad+=("режим files/scripts/old.sh $(stat -c %a "$copy") (был $old_mode)")
    else
      bad+=("files/scripts/old.sh в архиве нет — файл снят без копии")
    fi
    collect < <(probe_tool journal "$arch/journal.tsv" scripts/old.sh "$OLD_BLOB")
  fi
  collect < <(expect_texts "$out" "снято 1: scripts/old.sh")
  case_result "фв-а)" "A → B: old.sh в files/ архива (blob, режим), в проекте нет, «снят» до «готово», итог с ним; чужой файл цел" ${bad[@]+"${bad[@]}"}

  # фв-б) остальные состояния пути из «лишнего» — по стенду на каждое («снят» —
  #   фв-а, «правлен здесь» — фв-в).
  # б1) ссылки в описи: ln.sh → файл вне проекта; sub → каталог вне проекта;
  #   sub2 → scripts/real ВНУТРИ проекта. Пакет A: лишнее — только они. Ловит:
  #   снят суд realpath родителя (перенос sub/x.sh унёс бы файл вне проекта);
  #   «внутри проекта» вместо «равен» (перенос sub2/x.sh унёс бы scripts/real/x.sh).
  #   Зелёным оставит: fifo/каталог на месте пути описи — судят M и D40.
  C=$(fb_stand fv-b1 "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  printf 'цель вне проекта\n' > "$C/цель-вне"
  ln -s "$C/цель-вне" "$P/scripts/ln.sh"
  mkdir -p "$C/вне" "$P/scripts/real"
  printf '#!/bin/sh\necho вне\n' > "$C/вне/x.sh"
  ln -s "$C/вне" "$P/scripts/sub"
  printf '#!/bin/sh\necho real\n' > "$P/scripts/real/x.sh"
  ln -s real "$P/scripts/sub2"
  links=(scripts/ln.sh scripts/sub/x.sh scripts/sub2/x.sh)
  python3 -B "$FB/manifest.py" add "$P" scripts/ln.sh scripts scripts/sub/x.sh scripts scripts/sub2/x.sh scripts \
    || bad+=("стенд: строки ссылок в опись не дописаны")
  sums_before=$(git hash-object "$C/цель-вне" "$C/вне/x.sh" "$P/scripts/real/x.sh" | tr '\n' ' ')
  targets_before=$(readlink "$P/scripts/ln.sh" "$P/scripts/sub" "$P/scripts/sub2" | tr '\n' ' ')
  rc=0; out=$(run_pkg "$FB/A/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  [[ "$(git hash-object "$C/цель-вне" "$C/вне/x.sh" "$P/scripts/real/x.sh" 2>/dev/null | tr '\n' ' ')" == "$sums_before" ]] \
    || bad+=("цели ссылок или scripts/real/x.sh изменились либо исчезли")
  [[ "$(readlink "$P/scripts/ln.sh" "$P/scripts/sub" "$P/scripts/sub2" 2>/dev/null | tr '\n' ' ')" == "$targets_before" ]] \
    || bad+=("ссылки scripts/ln.sh, scripts/sub, scripts/sub2 не на месте")
  line=$(summary_line "$out")
  grep -qE '^опись: в описи [0-9]+; снято 0;' <<<"$line" || bad+=("нет «опись: в описи N; снято 0;»: $(cut -c1-300 <<<"$line")")
  kept_now=$(kept_part "$line")
  [[ "$kept_now" == "оставлено 3:"* ]] || bad+=("в сводке не «оставлено 3: …»: «$kept_now»")
  probe_tool kept "$P" > "$C/оставлено"
  for rel in "${links[@]}"; do
    grep -qF "$rel" <<<"$kept_now" || bad+=("$rel не назван в «оставлено» сводки")
    grep -qF "$rel" "$C/оставлено" || bad+=("$rel нет в секции «# оставлено» описи")
  done
  archived=$(find "$C/копии/раскладка" -path '*/files/scripts/*' \( -name ln.sh -o -path '*/files/scripts/sub*' -o -path '*/files/scripts/real/*' \) 2>/dev/null | head -3 || true)
  [[ -z "$archived" ]] || bad+=("в архиве путь ссылки или её цели: $(tr '\n' ' ' <<<"$archived")")
  case_result "фв-б)" "1 ссылки: ln.sh, sub (вне), sub2 (внутри) не тронуты, цели побайтно прежние, «оставлено 3» в сводке и описи" ${bad[@]+"${bad[@]}"}

  # б2) уже нет: old.sh удалён из проекта до раскладки B. Ловит: исчезнувший
  #   путь выбывает молча (нет числа «уже не было» в сводке); засчитан в «снято».
  #   Зелёным оставит: путь, исчезнувший МЕЖДУ ходом 1 и переносом, — решение (д), D40.
  C=$(fb_stand fv-b2 "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  rm -f "$P/scripts/old.sh"
  rc=0; out=$(run_pkg "$FB/B/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(expect_texts "$out" "уже не было: scripts/old.sh")
  line=$(summary_line "$out")
  grep -qE '^опись: в описи [0-9]+; снято 0;.*; уже не было 1( |;|$)' <<<"$line" \
    || bad+=("нет «опись: … снято 0; … уже не было 1»: $(cut -c1-300 <<<"$line")")
  if grep -q "^scripts/old.sh"$'\t' <(manifest_body "$P"); then bad+=("исчезнувший scripts/old.sh остался в теле описи"); fi
  case_result "фв-б)" "2 уже нет: «уже не было: scripts/old.sh», в сводке «уже не было 1», снято 0, вне тела" ${bad[@]+"${bad[@]}"}

  # б3) не снимается: scripts/hooks/guard_bash.py убран из пакета (Ag), ярус 2
  #   запретного. Ловит: запретное снимается переносом. Зелёным оставит: ярус 1
  #   (STATE.md, память/) — его строки вон из тела ещё в Ф-б, судит фб-е и M8.
  C=$(fb_stand fv-b3 "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  g_blob=$(git hash-object "$P/scripts/hooks/guard_bash.py" 2>/dev/null || echo нет)
  rc=0; out=$(run_pkg "$FB/Ag/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(expect_texts "$out" "не снимается: scripts/hooks/guard_bash.py")
  grep -qE '^опись: в описи [0-9]+; снято 0;' <<<"$(summary_line "$out")" || bad+=("нет «опись: в описи N; снято 0;»: $(summary_line "$out" | cut -c1-300)")
  [[ "$(git hash-object "$P/scripts/hooks/guard_bash.py" 2>/dev/null || true)" == "$g_blob" ]] || bad+=("scripts/hooks/guard_bash.py снят или изменён")
  [[ -z "$(find "$C/копии/раскладка" -path '*/files/scripts/hooks/guard_bash.py' 2>/dev/null || true)" ]] || bad+=("guard_bash.py лёг в архив — снятие было")
  case_result "фв-б)" "3 не снимается: guard_bash.py убран из пакета — назван, на месте, снято 0" ${bad[@]+"${bad[@]}"}

  # фв-в) old.sh правлен здесь на 1 байт → на месте, «оставлено 1: scripts/old.sh»
  #   в сводке и в секции описи; вторая раскладка B снова его называет. Ловит:
  #   сверка blob выкинута (правленое снято); «оставлено» не пишется в опись —
  #   вторая сводка молчит. Зелёным оставит: правка, откаченная к blob описи, —
  #   судит M17 (R1-11).
  C=$(fb_stand fv-v "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  printf 'x' >> "$P/scripts/old.sh"
  edited=$(git hash-object "$P/scripts/old.sh")
  for n in 1 2; do
    rc=0; out=$(run_pkg "$FB/B/harness" "$C" 2>&1) || rc=$?
    [[ $rc -eq 0 ]] || bad+=("прогон $n: код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
    line=$(summary_line "$out")
    grep -qE '^опись: в описи [0-9]+; снято 0;' <<<"$line" || bad+=("прогон $n: нет «снято 0»: $(cut -c1-300 <<<"$line")")
    [[ "$(kept_part "$line")" == "оставлено 1: scripts/old.sh" ]] || bad+=("прогон $n: в сводке «$(kept_part "$line")», ждали «оставлено 1: scripts/old.sh»")
    [[ "$(git hash-object "$P/scripts/old.sh" 2>/dev/null || true)" == "$edited" ]] || bad+=("прогон $n: правленый scripts/old.sh снят или изменён")
    probe_tool kept "$P" | grep -qF scripts/old.sh || bad+=("прогон $n: scripts/old.sh нет в секции «# оставлено» описи")
  done
  case_result "фв-в)" "правка old.sh на 1 байт: на месте, «оставлено 1: scripts/old.sh» в сводке и описи, дважды" ${bad[@]+"${bad[@]}"}

  # фв-г) нет места: R (A без old.sh), журнал архива → /dev/full → код 3,
  #   «No space left on device», old.sh на месте, опись побайтно прежняя; пара —
  #   тот же стенд с годным архивом снимает old.sh. Ловит: снятие без журнала
  #   (архив не открывается ради снятия); беда журнала проглочена. Зелёным
  #   оставит: короткая запись журнала — судит D4 двери.
  C=$(fb_stand fv-g "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  mkdir -p "$C/копии/раскладка/г"
  ln -s /dev/full "$C/копии/раскладка/г/journal.tsv"
  man_sum=$(sha256sum < "$P/.layout-manifest" 2>/dev/null || echo нет)
  project_snapshot "$P" > "$C/до"
  rc=0; out=$(run_pkg "$FB/R/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$C/копии/раскладка/г" 2>&1) || rc=$?
  project_snapshot "$P" > "$C/после"
  [[ $rc -eq 3 ]] || bad+=("код $rc (ждали 3)")
  collect < <(expect_texts "$out" "РАСКЛАДКА НЕ НАЧАТА" "No space left on device")
  collect < <(forbid_texts "$out" "раскладка завершена")
  collect < <(snapshot_diff "$C/до" "$C/после")
  [[ "$(sha256sum < "$P/.layout-manifest" 2>/dev/null || echo нет)" == "$man_sum" ]] || bad+=("опись изменилась")
  [[ -f "$P/scripts/old.sh" ]] || bad+=("scripts/old.sh снят при недоступном журнале")
  rc=0; out=$(run_pkg "$FB/R/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$C/копии/раскладка/г2" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("пара с годным архивом: код $rc (ждали 0)")
  [[ ! -e "$P/scripts/old.sh" && -f "$C/копии/раскладка/г2/files/scripts/old.sh" ]] \
    || bad+=("пара с годным архивом: old.sh не перенесён в г2/files/ — случай судил бы пустое")
  case_result "фв-г)" "журнал на /dev/full при снятии → код 3, ENOSPC назван, old.sh и опись прежние; пара снимает" ${bad[@]+"${bad[@]}"}

  # фв-г′) обрыв: B5 снимает 5, HARNESS_LAYOUT_KILL_AFTER_WRITE=1 — укладывать
  #   нечего, первая запись — первое снятие. Каждый исчезнувший путь назван «снят»
  #   и лежит в files/ с прежним blob; исчез ровно один. Ловит: строка «снят»
  #   после переноса; перенос до открытия журнала. Зелёным оставит: убийство
  #   между os.replace и unlink при разных ФС — судит D35.
  C=$(fb_stand fv-g2 "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  project_snapshot "$P" > "$C/до"
  rc=0; out=$(run_pkg "$FB/B5/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$C/копии/раскладка/гк" \
                HARNESS_LAYOUT_KILL_AFTER_WRITE=1 2>&1) || rc=$?
  project_snapshot "$P" > "$C/после"
  [[ $rc -eq 137 ]] || bad+=("код $rc (ждали 137 — SIGKILL пробной двери на первом снятии)")
  collect < <(python3 -B - "$C/до" "$C/после" "$C/копии/раскладка/гк" "${five[@]}" <<'PY'
import hashlib, sys
from pathlib import Path
def files(snap):
    out = {}
    for line in Path(snap).read_text(encoding="utf-8").splitlines():
        path, kind, _ino, _mode, blob = line.split("\t")
        if kind == "f":
            out[path] = blob
    return out
before, after, archive, five = files(sys.argv[1]), files(sys.argv[2]), Path(sys.argv[3]), sys.argv[4:]
gone = sorted(set(before) - set(after))
if len(gone) != 1:
    print(f"исчезло путей {len(gone)} (ждали 1 — обрыв на первом снятии): {gone[:5]}")
if set(gone) - set(five):
    print(f"исчезли пути вне пяти убранных из пакета: {sorted(set(gone) - set(five))[:3]}")
journal = archive / "journal.tsv"
rows = [l.split("\t") for l in journal.read_text(encoding="utf-8").splitlines()
        if l and not l.startswith("#")] if journal.is_file() else []
if not journal.is_file():
    print(f"журнала {journal} нет")
named = {r[3] for r in rows if len(r) == 4 and r[0] == "снят"}
for rel in gone:
    if rel not in named:
        print(f"{rel} исчез из проекта без строки «снят»")
    copy = archive / "files" / rel
    if not copy.is_file():
        print(f"{rel} исчез, а files/{rel} нет — файл потерян")
        continue
    data = copy.read_bytes()
    if hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() != before[rel]:
        print(f"files/{rel}: blob не равен прежнему")
if any(r[0] == "итог" for r in rows):
    print("в журнале оборванного прогона есть «итог»")
PY
)
  case_result "фв-г′)" "обрыв на первом снятии: исчез один путь, у него «снят» и копия в files/ с прежним blob" ${bad[@]+"${bad[@]}"}

  # фв-д) BACKUP_DIR внутри обычного файла, R снимает old.sh → код 3, «архив
  #   недоступен» с причиной ОС, проект прежний; пара — годный каталог копий
  #   снимает. Ловит: снятие без архива; беда архива ради снятия не в ходе 1
  #   (половина укладки до отказа). Зелёным оставит: архив пропал между ходом 1
  #   и переносом — окно секунд, D34.
  C=$(fb_stand fv-d "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  : > "$C/файл"
  make_stand_conf "$C/harness.conf" "$C/файл/копии"
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  project_snapshot "$P" > "$C/до"
  rc=0; out=$(run_pkg "$FB/R/harness" "$C" 2>&1) || rc=$?
  project_snapshot "$P" > "$C/после"
  [[ $rc -eq 3 ]] || bad+=("код $rc (ждали 3)")
  collect < <(expect_texts "$out" "РАСКЛАДКА НЕ НАЧАТА" "архив недоступен" "Not a directory" "scripts/old.sh")
  collect < <(snapshot_diff "$C/до" "$C/после")
  make_stand_conf "$C/harness.conf" "$C/копии"
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  rc=0; out=$(run_pkg "$FB/R/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 && ! -e "$P/scripts/old.sh" ]] || bad+=("пара с годным каталогом копий: код $rc, old.sh $([[ -e "$P/scripts/old.sh" ]] && echo на месте || echo снят) (ждали 0 и снят)")
  case_result "фв-д)" "BACKUP_DIR внутри файла при снятии → код 3, «архив недоступен: … Not a directory», проект прежний; пара снимает" ${bad[@]+"${bad[@]}"}

  # фв-е) порождённые: файлы корней укладки ВНЕ описи (скилы генератора,
  #   .установлено, *.lock) до и после раскладки B — то же множество (путь, blob),
  #   и оно непусто; снятие при этом было. Ловит: снятие по «нет в плане» вместо
  #   «есть в описи»; rmdir/перенос задевает соседей. Зелёным оставит: порождённый
  #   файл, чей путь совпал со строкой описи, — такой путь опись не отличит.
  C=$(fb_stand fv-e "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  : > "$P/harness/vozmozhnosti/mobilnaya-razrabotka/.установлено"
  printf 'занят\n' > "$P/scripts/hooks/state.lock"
  # Метка включает скилы возможности: генератор добавит их на ПЕРВОЙ раскладке
  # после метки. Добавленное до снимка «до» — не дело снятия.
  rc=0; out=$(run_pkg "$FB/A/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("стенд: раскладка A после метки — код $rc")
  probe_tool outside "$P" > "$C/вне-до"
  for want in harness/vozmozhnosti/mobilnaya-razrabotka/.установлено scripts/hooks/state.lock; do
    grep -q "^$want"$'\t' "$C/вне-до" || bad+=("стенд: $want не среди файлов вне описи — множество не то")
  done
  grep -q '^harness/skills/' "$C/вне-до" || bad+=("стенд: скилов генератора нет среди файлов вне описи — пустое множество не доказательство")
  rc=0; out=$(run_pkg "$FB/B/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  [[ ! -e "$P/scripts/old.sh" ]] || bad+=("снятия не было (old.sh на месте) — сохранность порождённых не судима")
  probe_tool outside "$P" > "$C/вне-после"
  n_diff=$(diff "$C/вне-до" "$C/вне-после" | grep -c '^[<>]' || true)
  (( n_diff == 0 )) || bad+=("множество вне описи изменилось ($n_diff строк): $(diff "$C/вне-до" "$C/вне-после" | grep '^[<>]' | head -3 | cut -f1 | tr '\n' ' ' || true)")
  case_result "фв-е)" "порождённые вне описи ($(wc -l < "$C/вне-до") файлов: скилы, .установлено, state.lock) до и после снятия равны" ${bad[@]+"${bad[@]}"}

  # фв-ж) замок на весь прогон и пропуск update.lock. Стенды — свой LOG_DIR
  #   (<стенд>/log из install.conf стенда), ключ ожидания строкой harness.conf.
  # 4 (первым: остальные части опираются на «свой LOG_DIR»). LOG_DIR и
  #   LAYOUT_LOCK_WAIT_SEC окружения вызывающего (как у смены) не доходят до
  #   стенда; после раскладки layout.lock стенда есть и свободен; update.lock
  #   проверкой не создан. Ловит: LOG_DIR= снят из run_pkg/run_stand (стенд взял
  #   бы живой locks/); замок с -o или утёкший в долгоживущее; проверка
  #   update.lock открывает с O_CREAT. Зелёным оставит: утечка в процесс, который
  #   переживает раскладку на секунды (не насовсем), — судит часть 5.
  C=$(fb_stand fv-zh4 "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_LOCK_WAIT_SEC 5)
  rc=0; out=$(LOG_DIR="$C/чужой-log" LAYOUT_LOCK_WAIT_SEC=abc run_pkg "$FB/A/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  [[ ! -e "$C/чужой-log" ]] || bad+=("LOG_DIR вызывающего дошёл до стенда: создан $C/чужой-log")
  collect < <(forbid_texts "$out" "не число: abc")
  lockf="$C/log/locks/layout.lock"
  if [[ -f "$lockf" ]]; then
    frc=0; flock -n "$lockf" true || frc=$?
    [[ $frc -eq 0 ]] || bad+=("после выхода раскладчика flock -n $lockf → $frc: замок утёк")
  else
    bad+=("замка $lockf нет — раскладка шла без своего замка")
  fi
  [[ ! -e "$C/log/locks/update.lock" ]] || bad+=("проверка update.lock создала файл $C/log/locks/update.lock")
  C2=$(new_case fv-zh4s)
  rc=0; out=$(LOG_DIR="$C2/чужой-log" run_stand "$C2/install.conf" "$C2/harness.conf" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("run_stand: код $rc (ждали 0)")
  [[ ! -e "$C2/чужой-log" ]] || bad+=("run_stand: LOG_DIR вызывающего дошёл до стенда")
  [[ -f "$C2/log/locks/layout.lock" ]] || bad+=("run_stand: замка $C2/log/locks/layout.lock нет")
  case_result "фв-ж)" "4: свой LOG_DIR стенда (окружение не доходит), layout.lock свободен после раскладки, update.lock не создан" ${bad[@]+"${bad[@]}"}

  # 1. Проба держит layout.lock сама → с ключом "2" раскладка ждёт ≥ 2 с и
  #   отказывает «идёт другая раскладка (ждал 2 с)», называет замок и pid
  #   держателя (решение ж); без строки ключа — отказ сразу, ключ назван. Проект
  #   и опись прежние. Ловит: замок не берётся; flock -w → -n (отказ < 2 с);
  #   умолчание-число без ключа; держатель не в строке маркера (update.py берёт
  #   только её, R1-09); свой дескриптор не закрыт до расспроса — назван и сам
  #   раскладчик (R1-10). Зелёным оставит: держатель назван по
  #   /proc/locks — у держателя-генератора (часть 5) там pid уже мёртвого flock.
  C=$(fb_stand fv-zh1 "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_LOCK_WAIT_SEC 2)
  lockf="$C/log/locks/layout.lock"
  probe_tool holder "$lockf" "$C/держу" "$C/отпусти" > "$C/держатель.out" 2>&1 & holder_job=$!
  if wait_file "$C/держу" 100; then
    hpid=$(cat "$C/держу")
    project_snapshot "$P" > "$C/до"
    t0=$(date +%s.%N); rc=0; out=$(run_pkg "$FB/B/harness" "$C" 2>&1) || rc=$?; waited=$(seconds_since "$t0")
    [[ $rc -eq 3 ]] || bad+=("ключ 2: код $rc (ждали 3)")
    awk -v w="$waited" 'BEGIN { exit !(w >= 2) }' || bad+=("ключ 2: отказ через $waited с (ждали ≥ 2)")
    collect < <(expect_texts "$out" "РАСКЛАДКА НЕ НАЧАТА: идёт другая раскладка (ждал 2 с" "$lockf")
    collect < <(holder_in_marker "$out" "$hpid")
    # «Сразу» судится против базы — тот же отказ с ключом 0 до и после: время до замка
    # (bash, config.sh, konf_zagruzit) растёт с нагрузкой машины, а абсолютный порог
    # мигал под прогоном ворот. Умолчание-число без ключа (≥ 1 с) база не прячет.
    collect < <(set_conf_key "$C/harness.conf" LAYOUT_LOCK_WAIT_SEC 0)
    t0=$(date +%s.%N); rc=0; run_pkg "$FB/B/harness" "$C" > /dev/null 2>&1 || rc=$?; base1=$(seconds_since "$t0")
    [[ $rc -eq 3 ]] || bad+=("база (ключ 0): код $rc (ждали 3)")
    collect < <(set_conf_key "$C/harness.conf" LAYOUT_LOCK_WAIT_SEC --drop)
    t0=$(date +%s.%N); rc=0; out=$(run_pkg "$FB/B/harness" "$C" 2>&1) || rc=$?; waited=$(seconds_since "$t0")
    nokey_rc=$rc
    collect < <(set_conf_key "$C/harness.conf" LAYOUT_LOCK_WAIT_SEC 0)
    t0=$(date +%s.%N); rc=0; run_pkg "$FB/B/harness" "$C" > /dev/null 2>&1 || rc=$?; base2=$(seconds_since "$t0")
    [[ $rc -eq 3 ]] || bad+=("база (ключ 0) после: код $rc (ждали 3)")
    collect < <(set_conf_key "$C/harness.conf" LAYOUT_LOCK_WAIT_SEC --drop)
    project_snapshot "$P" > "$C/после"
    [[ $nokey_rc -eq 3 ]] || bad+=("без ключа: код $nokey_rc (ждали 3)")
    echo "  замер фв-ж 1: без ключа $waited с, база (ключ 0) $base1 и $base2 с"
    awk -v w="$waited" -v a="$base1" -v b="$base2" 'BEGIN { m = a > b ? a : b; exit !(w - m < 1) }' \
      || bad+=("без ключа: отказ через $waited с при базе $base1/$base2 с (ждали разницу < 1 с)")
    collect < <(expect_texts "$out" "ключ LAYOUT_LOCK_WAIT_SEC не задан — без ожидания" "идёт другая раскладка")
    collect < <(snapshot_diff "$C/до" "$C/после")
  else
    bad+=("стенд: держатель layout.lock не встал за 10 с: $(tail -1 "$C/держатель.out" 2>/dev/null || true)")
  fi
  : > "$C/отпусти"; wait "$holder_job" || true
  case_result "фв-ж)" "1: layout.lock держит проба → ключ 2: ждал ≥ 2 с, код 3, в строке маркера держатель — он один; без ключа — сразу (против базы ключа 0), ключ назван; проект прежний" ${bad[@]+"${bad[@]}"}

  # 2. Держатель update.lock — предок: НЕИЗМЕНЁННЫЙ update.py пакета импортирован
  #   как есть, внутри его _замок_постановки() две раскладки subprocess.run
  #   (как _разложить_пакет) → обе код 0, каждая называет предка-держателя. Ловит:
  #   отказ любому держателю; пропуск по переменной окружения. Зелёным оставит:
  #   держатель в другом пространстве pid (контейнер) — «не найден», L7.
  C=$(fb_stand fv-zh2 "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  out=$(env HARNESS_INSTALL_CONF="$C/install.conf" HARNESS_CONF="$C/harness.conf" LOG_DIR="$C/log" \
          BACKUP_DIR= LAYOUT_LOCK_WAIT_SEC= LAYOUT_REMOVE_MAX_PERCENT= \
          python3 -B - "$HERE/scripts/update.py" "$FB/A/harness" 2>&1 <<'PY'
import importlib.util, os, subprocess, sys
update_py, package = sys.argv[1:3]
spec = importlib.util.spec_from_file_location("update_under_probe", update_py)
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)
child_env = dict(os.environ, HARNESS_SELFTEST="1")
with update._замок_постановки() as taken:
    print(f"замок постановки взят: {taken}; держатель {os.getpid()}")
    for n in (1, 2):
        done = subprocess.run(["bash", os.path.join(package, "razlozhit-harnes.sh")], env=child_env,
                              capture_output=True, text=True, timeout=900)
        print(f"раскладка {n}: код {done.returncode}")
        print(done.stdout + done.stderr)
PY
) || bad+=("python-стенд упал: $(tail -2 <<<"$out" | tr '\n' ' ')")
  taken_line=$(grep -m1 '^замок постановки взят: ' <<<"$out" || true)
  [[ "$taken_line" == "замок постановки взят: True; держатель "* ]] || bad+=("стенд: update.py не взял замок постановки: «${taken_line:-нет строки}»")
  upd_pid=${taken_line##* }
  for n in 1 2; do
    grep -qxF "раскладка $n: код 0" <<<"$out" || bad+=("раскладка $n под замком постановки: $(grep "^раскладка $n: код" <<<"$out" || echo нет строки)")
  done
  n_pass=$(grep -cF "замок обновления держит предок $upd_pid — раскладка идёт" <<<"$out" || true)
  [[ "$n_pass" == 2 ]] || bad+=("строк «замок обновления держит предок $upd_pid — раскладка идёт» $n_pass (ждали 2)")
  [[ -f "$C/log/locks/update.lock" ]] || bad+=("стенд: update.lock стенда не создан — update.py брал замок не в стенде")
  case_result "фв-ж)" "2: update.lock держит неизменённый update.py (предок) → две раскладки код 0, предок назван" ${bad[@]+"${bad[@]}"}

  # 3. Тот же update.lock держит процесс-не-предок → «идёт обновление харнеса»,
  #   код 3, держатель назван, проект прежний. Ловит: пропуск любому держателю;
  #   «держатель не найден» читается как «свободно». Зелёным оставит: держатель-
  #   предок через двойной fork вне цепочки PPid — сегодня у update.py такого нет.
  bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  probe_tool holder "$C/log/locks/update.lock" "$C/держу-обн" "$C/отпусти-обн" > "$C/держатель-обн.out" 2>&1 & holder_job=$!
  if wait_file "$C/держу-обн" 100; then
    hpid=$(cat "$C/держу-обн")
    project_snapshot "$P" > "$C/до"
    rc=0; out=$(run_pkg "$FB/B/harness" "$C" 2>&1) || rc=$?
    project_snapshot "$P" > "$C/после"
    [[ $rc -eq 3 ]] || bad+=("код $rc (ждали 3)")
    collect < <(expect_texts "$out" "РАСКЛАДКА НЕ НАЧАТА: идёт обновление харнеса" "держатель $hpid")
    collect < <(snapshot_diff "$C/до" "$C/после")
  else
    bad+=("стенд: держатель update.lock не встал за 10 с: $(tail -1 "$C/держатель-обн.out" 2>/dev/null || true)")
  fi
  : > "$C/отпусти-обн"; wait "$holder_job" || true
  case_result "фв-ж)" "3: update.lock держит не предок → код 3, «идёт обновление харнеса», держатель назван, проект прежний" ${bad[@]+"${bad[@]}"}

  # 5. Убийство держателя (R2-10): копия пакета, где BUILD-SKILLS.py — заглушка
  #   (метка «начал <pid> <ppid>», ждёт «отпусти» ≤ 30 с). SIGKILL bash
  #   раскладчика (= ppid заглушки, проверено по cmdline) → пока заглушка жива,
  #   flock -n не удаётся и вторая раскладка отказывает, называя pid заглушки
  #   (решение ж: зависший генератор); после выхода заглушки flock -n удаётся.
  #   Ловит: замок с -o / дескриптор закрыт для генератора ({fd}>&-); pid
  #   держателя из /proc/locks (там pid мёртвого flock). Зелёным оставит:
  #   генератор, запущенный через sudo (closefrom), — у нас его нет.
  C=$(fb_stand fv-zh5 "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_LOCK_WAIT_SEC 2)
  K="$FB/K"; mkdir -p "$K" "$C/заглушка"
  cp -a "$FB/A/harness" "$K/harness"
  if [[ -d "$FB/A/UNIFIED" ]]; then cp -a "$FB/A/UNIFIED" "$K/UNIFIED"; fi
  cat > "$K/harness/BUILD-SKILLS.py" <<PY
import os, time
from pathlib import Path
marks = Path("$C/заглушка")
(marks / "начал.tmp").write_text(f"{os.getpid()} {os.getppid()}\n", encoding="utf-8")
os.replace(marks / "начал.tmp", marks / "начал")
for _ in range(300):
    if (marks / "отпусти").exists():
        break
    time.sleep(0.1)
PY
  lockf="$C/log/locks/layout.lock"
  spid=""
  run_pkg "$K/harness" "$C" > "$C/раскладка.out" 2>&1 & layout_job=$!
  if wait_file "$C/заглушка/начал" 600; then
    read -r spid sppid < "$C/заглушка/начал" || true
    if [[ ! "$spid" =~ ^[0-9]+$ || ! "$sppid" =~ ^[0-9]+$ ]]; then
      bad+=("стенд: метка заглушки не «<pid> <ppid>»: $(cat "$C/заглушка/начал")"); spid=""
    elif [[ "$(tr '\0' ' ' < "/proc/$sppid/cmdline" 2>/dev/null || true)" != *"$K/harness/razlozhit-harnes.sh"* ]]; then
      bad+=("стенд: родитель заглушки $sppid не bash раскладчика — убивать некого")
    else
      kill -9 "$sppid" 2>/dev/null || true
      wait "$layout_job" 2>/dev/null || true
      if [[ ! -f "$lockf" ]]; then
        # Без файла flock -n отказывает «не открыть» (66) — это не «занят».
        bad+=("замка $lockf нет — раскладка шла без своего замка")
      elif kill -0 "$spid" 2>/dev/null; then
        frc=0; flock -n "$lockf" true 2>/dev/null || frc=$?
        [[ $frc -eq 1 ]] || bad+=("bash раскладчика убит, генератор жив, а flock -n → $frc (ждали 1 — занят): замок умер с bash")
        rc=0; out=$(run_pkg "$FB/A/harness" "$C" 2>&1) || rc=$?
        [[ $rc -eq 3 ]] || bad+=("вторая раскладка при живом генераторе: код $rc (ждали 3)")
        collect < <(expect_texts "$out" "идёт другая раскладка")
        collect < <(holder_in_marker "$out" "$spid")
      else
        bad+=("стенд: заглушка умерла раньше суда — замок не судим")
      fi
    fi
  else
    bad+=("стенд: заглушка генератора не встала за 60 с: $(tail -2 "$C/раскладка.out" 2>/dev/null | tr '\n' ' ' || true)")
  fi
  : > "$C/заглушка/отпусти"
  if [[ -n "$spid" ]]; then
    for ((i = 0; i < 300; i++)); do kill -0 "$spid" 2>/dev/null || break; sleep 0.1; done
    if kill -0 "$spid" 2>/dev/null; then
      bad+=("заглушка $spid не вышла за 30 с после «отпусти»")
    else
      frc=0; flock -n "$lockf" true || frc=$?
      [[ $frc -eq 0 ]] || bad+=("генератор вышел, а flock -n $lockf → $frc")
    fi
  fi
  wait "$layout_job" 2>/dev/null || true
  case_result "фв-ж)" "5: SIGKILL bash раскладчика → замок держит живой генератор (в строке маркера второй раскладки — он один), после его выхода свободен" ${bad[@]+"${bad[@]}"}

  # фв-з) A → B → A: второй A снимает ровно scripts/new.sh (files/ его архива —
  #   один путь), множество (путь, blob) корней укладки равно множеству после
  #   первого A. Ловит: снятое не возвращается укладкой; снято лишнее на обратном
  #   ходе. Зелёным оставит: режим файла (множество — пары путь/blob) — судит фв-а.
  C=$(fb_stand fv-z "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  probe_tool pairs "$P" > "$C/пары-A"
  [[ -s "$C/пары-A" ]] || bad+=("стенд: множество пар после A пусто — сравнивать нечего")
  rc=0; out=$(run_pkg "$FB/B/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("B: код $rc (ждали 0)")
  [[ -f "$P/scripts/new.sh" ]] || bad+=("стенд: после B нет scripts/new.sh — обратный ход судить не по чему")
  before=$(archive_list "$C")
  rc=0; out=$(run_pkg "$FB/A/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("второй A: код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(expect_texts "$out" "снято 1: scripts/new.sh")
  arch=$(new_archive "$before" "$C")
  if [[ $(grep -c . <<<"$arch" || true) == 1 ]]; then
    got=$(archive_files "$arch" | tr '\n' ' ')
    [[ "$got" == "scripts/new.sh " ]] || bad+=("files/ архива второго A: «$got» (ждали только scripts/new.sh)")
  else
    bad+=("архивов второго A: $(grep -c . <<<"$arch" || true) (ждали 1)")
  fi
  probe_tool pairs "$P" > "$C/пары-A2"
  n_diff=$(diff "$C/пары-A" "$C/пары-A2" | grep -c '^[<>]' || true)
  (( n_diff == 0 )) || bad+=("пары после A → B → A ≠ после A ($n_diff строк): $(diff "$C/пары-A" "$C/пары-A2" | grep '^[<>]' | head -3 | cut -f1 | tr '\n' ' ' || true)")
  case_result "фв-з)" "A → B → A: второй A снял только scripts/new.sh, пары корней укладки равны первому A" ${bad[@]+"${bad[@]}"}

  # фв-и) порог — половина доли 5 из N, B5 убирает 5 → снято 0, отложено 5 (порог 5), файлы на месте;
  #   повтор с --снять-сверх-порога проба снимает РОВНО эти 5 (files/ его архива),
  #   причина в журнале и выводе; без ключа порога — отложено 5, «не задан», код 0.
  #   Ловит: отложенные выпадают из тела (повтор снимать нечего); довод не
  #   передан двери; нет ключа = снимать. Зелёным оставит: «>» вместо «>=» на
  #   границе порога — судит M6.
  C=$(fb_stand fv-i "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  n_prior=$(manifest_body "$P" | wc -l)
  pct=$(half_share_percent "$n_prior")
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT "$pct")
  five_sorted=$(printf '%s\n' "${five[@]}" | LC_ALL=C sort | tr '\n' ' ')
  rc=0; out=$(run_pkg "$FB/B5/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("малый порог: код $rc (ждали 0)")
  collect < <(expect_texts "$out" "порог: снять 5 из $n_prior (" "LAYOUT_REMOVE_MAX_PERCENT=$pct" "отложено 5 (порог 5)")
  grep -qE '^опись: в описи [0-9]+; снято 0;' <<<"$(summary_line "$out")" || bad+=("малый порог: нет «снято 0»: $(summary_line "$out" | cut -c1-300)")
  for rel in "${five[@]}"; do [[ -f "$P/$rel" ]] || bad+=("малый порог: $rel снят при отложенном снятии"); done
  before=$(archive_list "$C")
  rc=0; out=$(run_pkg "$FB/B5/harness" "$C" -- --снять-сверх-порога проба 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("--снять-сверх-порога проба: код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(expect_texts "$out" "снято 5" "снято сверх порога по доводу: проба")
  arch=$(new_archive "$before" "$C")
  if [[ $(grep -c . <<<"$arch" || true) == 1 ]]; then
    got=$(archive_files "$arch" | tr '\n' ' ')
    [[ "$got" == "$five_sorted" ]] || bad+=("files/ архива снятия сверх порога: «$got», ждали «$five_sorted»")
    grep -qxF "# снять сверх порога: проба" "$arch/journal.tsv" 2>/dev/null || bad+=("в журнале нет «# снять сверх порога: проба»")
  else
    bad+=("архивов снятия сверх порога: $(grep -c . <<<"$arch" || true) (ждали 1)")
  fi
  for rel in "${five[@]}"; do [[ ! -e "$P/$rel" ]] || bad+=("--снять-сверх-порога: $rel на месте"); done
  C2=$(fb_stand fv-i2 "$A1/proj"); P2="$C2/proj"
  collect < <(set_conf_key "$C2/harness.conf" LAYOUT_REMOVE_MAX_PERCENT --drop)
  rc=0; out=$(run_pkg "$FB/B5/harness" "$C2" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("без ключа: код $rc (ждали 0)")
  collect < <(expect_texts "$out" "ключ LAYOUT_REMOVE_MAX_PERCENT не задан — отложено 5")
  grep -qE '^опись: в описи [0-9]+; снято 0;.*; отложено 5' <<<"$(summary_line "$out")" || bad+=("без ключа: нет «снято 0 … отложено 5»: $(summary_line "$out" | cut -c1-300)")
  for rel in "${five[@]}"; do [[ -f "$P2/$rel" ]] || bad+=("без ключа: $rel снят"); done
  case_result "фв-и)" "порог $pct %: отложено 5, файлы на месте; --снять-сверх-порога снял ровно 5; без ключа — отложено 5, код 0" ${bad[@]+"${bad[@]}"}

  # фв-и) довод: --снять-сверх-порога без значения, с пустым и с переводом
  #   строки → код 3, проект прежний; первые два — до замка (layout.lock не
  #   создан). Ловит: $2 под set -u (код 1, update.py откатывает); довод молча
  #   выпадает (dict(zip)); причина с \n ломает строку журнала. Зелёным оставит:
  #   причина с \t — тот же суд двери, D36.
  C=$(fb_stand fv-i3 "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  project_snapshot "$P" > "$C/до"
  for variant in нет пусто строки; do
    case "$variant" in
      нет)    lift=(--снять-сверх-порога) ;;
      пусто)  lift=(--снять-сверх-порога "") ;;
      строки) lift=(--снять-сверх-порога $'первая\nвторая') ;;
    esac
    rc=0; out=$(run_pkg "$FB/B/harness" "$C" -- "${lift[@]}" 2>&1) || rc=$?
    [[ $rc -eq 3 ]] || bad+=("довод «$variant»: код $rc (ждали 3)")
    collect < <(expect_texts "$out" "РАСКЛАДКА НЕ НАЧАТА")
    if [[ "$variant" != строки ]]; then
      collect < <(expect_texts "$out" "РАСКЛАДКА НЕ НАЧАТА: --снять-сверх-порога без причины")
      [[ ! -e "$C/log/locks/layout.lock" ]] || bad+=("довод «$variant»: layout.lock создан — отказ не до замка")
    fi
  done
  project_snapshot "$P" > "$C/после"
  collect < <(snapshot_diff "$C/до" "$C/после")
  case_result "фв-и)" "довод --снять-сверх-порога без значения / пустой / с переводом строки → код 3, проект прежний" ${bad[@]+"${bad[@]}"}

  # ══ Ф-г: откат по журналу прямого прогона (--откат <архив>) — фг-а…фг-д ══
  # Архивы называет проба, как update.py: прямой «стенд-<дата>-<время>-<pid>»,
  # откатный — он же с «-откат» (иначе дверь отвергнет имя). «Множество» —
  # (путь, blob, режим) обычных файлов корней укладки без яруса 1 описи (скилы,
  # .установлено, *.layout-tmp, живые настройки); остатки *.layout-tmp считаются
  # отдельно. Судит помощник fg_tool.py своим разбором, не модулем журнала.
  cat > "$FB/fg_tool.py" <<'PY'
import hashlib, os, stat, sys
from pathlib import Path

ROOTS = ("scripts", "harness", "UNIFIED")

def blob(path):
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

def walk(project):
    root = Path(project)
    for top in ROOTS:
        for dirpath, dirs, files in os.walk(root / top):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                path = Path(dirpath) / name
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue
                yield str(path.relative_to(root)), path

def triples(project, *live):
    skip = {f"harness/config/{name}" for name in live}
    for rel, path in sorted(walk(project)):
        if (rel.startswith("harness/skills/") or rel in skip or rel.endswith(".layout-tmp")
                or path.name == ".установлено"):
            continue
        print(f"{rel}\t{blob(path)}\t{stat.S_IMODE(os.lstat(path).st_mode):o}")

def remnants(project):
    print(sum(1 for rel, _ in walk(project) if rel.endswith(".layout-tmp")))

{"triples": triples, "remnants": remnants}[sys.argv[1]](*sys.argv[2:])
PY
  fg_set() { python3 -B "$FB/fg_tool.py" triples "$1" $LIVE_NAMES; }
  fg_set_diff() {  # $1, $2 = множества, $3 = что сравнивали → беда строкой
    local n
    [[ -s "$1" ]] || { printf '%s: множество «до» пусто — сравнивать нечего\n' "$3"; return 0; }
    n=$(diff "$1" "$2" | grep -c '^[<>]' || true)
    (( n == 0 )) || printf '%s: множества различны (%s строк): %s\n' "$3" "$n" \
      "$(diff "$1" "$2" | grep '^[<>]' | head -4 | tr '\n\t' '; ' || true)"
    return 0
  }
  # Шапка журнала отката: «# прогон <pid> откат план=<N> перекрыть=<M> по=<архив прямого>».
  rollback_header() {  # $1=архив отката $2=архив прямого → беда строкой
    local head
    head=$(head -1 "$1/journal.tsv" 2>/dev/null || true)
    [[ "$head" =~ ^#\ прогон\ [0-9]+\ откат\ план=[0-9]+\ перекрыть=[0-9]+\ по=(.*)$ \
       && "${BASH_REMATCH[1]}" == *"$(basename "$2")" ]] \
      || printf 'шапка журнала отката «%s» (ждали «# прогон <pid> откат … по=…%s»)\n' \
           "${head:-журнала $1/journal.tsv нет}" "$(basename "$2")"
    return 0
  }
  B_NEW_BLOB=$(git hash-object "$FB/B/harness/scripts/new.sh")
  # Стенд Ф-г: копия проекта после A и свой deploy.sh (X ≠ пакета) — проект «до
  # обновления». Порог 100 пишет каждый случай сам (беды ключа — в его bad).
  fg_stand() {  # $1=имя → каталог стенда
    local c
    c=$(fb_stand "$1" "$A1/proj")
    foreign_deploy "$c/proj"
    printf '%s\n' "$c"
  }

  # фг-а) A → B (архив прямого задан) → A с --откат <архив прямого> (свой архив
  #   «…-откат»): множество = «до обновления» (свой deploy.sh X возвращён с
  #   режимом 700, old.sh лёг укладкой A), new.sh в files/ архива ОТКАТА с blob B,
  #   шапка «откат … по=», строка «откат по журналу …: снято 1, возвращено 1».
  #   Ловит: --откат не дошёл до двери (обычная раскладка снимет new.sh по описи,
  #   но X потерян); возврат без режима; снятое ушло не в архив отката.
  #   Зелёным оставит: возврат, пишущий поверх правки после обновления, — здесь
  #   правки нет, судит D-случай двери.
  C=$(fg_stand fg-a); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  FWD="$C/копии/раскладка/стенд-20261024-000000-1"; RB="$FWD-откат"
  fg_set "$P" > "$C/множество-до"
  rc=0; out=$(run_pkg "$FB/B/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$FWD" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("прямой B: код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  [[ -f "$P/scripts/new.sh" ]] || bad+=("стенд: после B нет scripts/new.sh — откат судить не по чему")
  [[ -f "$FWD/files/scripts/deploy.sh" ]] || bad+=("стенд: свой deploy.sh не лёг копией в архив прямого — возвращать нечего")
  rc=0; out=$(run_pkg "$FB/A/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$RB" -- --откат "$FWD" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("откат A: код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  fg_set "$P" > "$C/множество-после"
  collect < <(fg_set_diff "$C/множество-до" "$C/множество-после" "до обновления против после отката")
  [[ -f "$RB/files/scripts/new.sh" && "$(git hash-object "$RB/files/scripts/new.sh")" == "$B_NEW_BLOB" ]] \
    || bad+=("в files/ архива отката нет scripts/new.sh с blob B: $(archive_files "$RB" | head -3 | tr '\n' ' ')")
  collect < <(rollback_header "$RB" "$FWD")
  line=$(grep -m1 -F 'откат по журналу ' <<<"$out" || true)
  [[ "$line" == *"$(basename "$FWD")"* ]] || bad+=("строка «откат по журналу» не называет архив прямого: «${line:-строки нет}»")
  collect < <(expect_texts "$line" "снято 1, возвращено 1, оставлено 0" "строк без «готово» 0")
  case_result "фг-а)" "1: A → B → A --откат: множество = до обновления (свой deploy.sh с режимом), new.sh в архиве отката, шапка «откат», «снято 1, возвращено 1»" ${bad[@]+"${bad[@]}"}

  # 2. Остаток .new.sh.layout-tmp (обрыв между записью временного и replace у
  #   файла ∉ A — его не перекроет укладка A) → откат его называет, содержимое не
  #   потеряно (в проекте или в files/ архива отката — решение (в) смены),
  #   множество без остатков = до обновления. Ловит: откат не судит остатки вне
  #   плана A (остаток живёт вечно, никто не назовёт); остаток стёрт rm.
  #   Зелёным оставит: остаток у файла плана A — его судит Ф-а к).
  C=$(fg_stand fg-a2); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  FWD="$C/копии/раскладка/стенд-20261024-000000-2"; RB="$FWD-откат"
  fg_set "$P" > "$C/множество-до"
  rc=0; out=$(run_pkg "$FB/B/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$FWD" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("прямой B: код $rc (ждали 0)")
  echo "обрывок прямого прогона" > "$P/scripts/.new.sh.layout-tmp"
  tmp_blob=$(git hash-object "$P/scripts/.new.sh.layout-tmp")
  rc=0; out=$(run_pkg "$FB/A/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$RB" -- --откат "$FWD" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("откат A: код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(expect_texts "$out" "остаток оборванной раскладки" "scripts/.new.sh.layout-tmp")
  kept_tmp=0
  for f in "$P/scripts/.new.sh.layout-tmp" "$RB/files/scripts/.new.sh.layout-tmp"; do
    [[ -f "$f" && "$(git hash-object "$f")" == "$tmp_blob" ]] && kept_tmp=1
  done
  (( kept_tmp )) || bad+=("остаток scripts/.new.sh.layout-tmp пропал: ни в проекте, ни в files/ архива отката")
  fg_set "$P" > "$C/множество-после"
  collect < <(fg_set_diff "$C/множество-до" "$C/множество-после" "без остатков: до обновления против после отката")
  case_result "фг-а)" "2: остаток .new.sh.layout-tmp вне плана A — откат называет его, содержимое цело, множество = до обновления" ${bad[@]+"${bad[@]}"}

  # фг-б) доводы отката: --откат без пути и с пустым → код 3 «довод --откат без
  #   пути» ДО замка (layout.lock не создан); --откат <архив> вместе с
  #   --снять-сверх-порога → код 3 (решение (и)), проект и журнал прямого прежние,
  #   архива отката нет; пара — тот же --откат без порога: код 0, шапка «откат».
  #   Ловит: $2 под set -u (код 1 → update.py откатывает откат); довод молча
  #   выпадает и идёт обычная раскладка; порог действует в откате. Зелёным
  #   оставит: отказ комбинации не тем словом — текст отказа договором не задан.
  C=$(fg_stand fg-b); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  FWD="$C/копии/раскладка/стенд-20261024-000000-3"; RB="$FWD-откат"
  project_snapshot "$P" > "$C/до"
  for variant in нет пусто; do
    case "$variant" in
      нет)   rb_args=("--откат") ;;
      пусто) rb_args=(--откат "") ;;
    esac
    rc=0; out=$(run_pkg "$FB/A/harness" "$C" -- "${rb_args[@]}" 2>&1) || rc=$?
    [[ $rc -eq 3 ]] || bad+=("довод «$variant»: код $rc (ждали 3)")
    collect < <(expect_texts "$out" "РАСКЛАДКА НЕ НАЧАТА: откат по журналу: довод --откат без пути")
    [[ ! -e "$C/log/locks/layout.lock" ]] || bad+=("довод «$variant»: layout.lock создан — отказ не до замка")
  done
  project_snapshot "$P" > "$C/после"
  collect < <(snapshot_diff "$C/до" "$C/после")
  rc=0; out=$(run_pkg "$FB/B/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$FWD" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("прямой B: код $rc (ждали 0)")
  cp "$FWD/journal.tsv" "$C/журнал-прямого" 2>/dev/null || bad+=("стенд: журнала прямого $FWD/journal.tsv нет")
  project_snapshot "$P" > "$C/до-комбинации"
  rc=0; out=$(run_pkg "$FB/A/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$RB" -- --откат "$FWD" --снять-сверх-порога проба 2>&1) || rc=$?
  [[ $rc -eq 3 ]] || bad+=("--откат вместе с --снять-сверх-порога: код $rc (ждали 3)")
  collect < <(expect_texts "$out" "РАСКЛАДКА НЕ НАЧАТА")
  project_snapshot "$P" > "$C/после-комбинации"
  collect < <(snapshot_diff "$C/до-комбинации" "$C/после-комбинации")
  cmp -s "$C/журнал-прямого" "$FWD/journal.tsv" || bad+=("отказ комбинации тронул журнал прямого прогона")
  [[ ! -e "$RB" ]] || bad+=("отказ комбинации создал архив отката $RB")
  rc=0; out=$(run_pkg "$FB/A/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$RB" -- --откат "$FWD" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("пара (--откат без порога): код $rc (ждали 0)")
  collect < <(rollback_header "$RB" "$FWD")
  case_result "фг-б)" "--откат без пути / пустой → код 3 до замка; с --снять-сверх-порога → код 3, ничего не тронуто; пара без порога — откат" ${bad[@]+"${bad[@]}"}

  # фг-в) читатель оборванных через раскладчик: в копии/раскладка стенда журнал
  #   «стенд-20200101-000000-1» без «итог» (одно действие без «готово») и чужой
  #   «стенд-x-20200101-000000-1» без «итог» → обычная раскладка (код 0) называет
  #   только первый «1 строк без «готово»» и дописывает ему «разобран»; следующая
  #   раскладка его уже не называет. Ловит: раскладчик не доводит читателя
  #   (дверь не зовёт его вне отката); имя маской «стенд-*»; «разобран» не
  #   дописан (называет каждый прогон). Зелёным оставит: «разобран» при коде 3 —
  #   судит D-случай двери.
  C=$(fb_stand fg-v "$A1/proj"); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  ORPH="$C/копии/раскладка/стенд-20200101-000000-1"; ALIEN="$C/копии/раскладка/стенд-x-20200101-000000-1"
  mkdir -p "$ORPH" "$ALIEN"
  for j in "$ORPH" "$ALIEN"; do
    printf '# прогон 1 раскладка план=1 перекрыть=0\nположен-новый\t-\t%s\tscripts/x.sh\n' "$B_NEW_BLOB" > "$j/journal.tsv"
  done
  cp "$ALIEN/journal.tsv" "$C/чужой-до"
  rc=0; out=$(run_pkg "$FB/A/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("раскладка 2: код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  line=$(grep -m1 -F "оборванная раскладка " <<<"$out" | grep -F "$(basename "$ORPH")" || true)
  [[ -n "$line" ]] || bad+=("раскладка 2 не назвала оборванный $(basename "$ORPH")")
  collect < <(expect_texts "$line" "1 строк без «готово»")
  grep -qF "$(basename "$ALIEN")" <<<"$out" && bad+=("раскладка 2 назвала чужой журнал $(basename "$ALIEN") — имя судится маской")
  grep -q $'^разобран\t-\t-\t' "$ORPH/journal.tsv" || bad+=("в журнал оборванного не дописан «разобран»")
  cmp -s "$C/чужой-до" "$ALIEN/journal.tsv" || bad+=("чужой журнал $(basename "$ALIEN") изменён")
  rc=0; out=$(run_pkg "$FB/A/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("раскладка 3: код $rc (ждали 0)")
  collect < <(forbid_texts "$out" "оборванная раскладка")
  case_result "фг-в)" "журнал без «итог» назван раскладкой 2 и разобран, раскладкой 3 — нет; чужое имя «стенд-x-…» не тронуто" ${bad[@]+"${bad[@]}"}

  # фг-г) фиксация ПРЕЖНЕГО раскладчика (пакет Ф-в, коммит 11d7deb, из истории
  #   пакета): --откат X он молча выбрасывает → код 0, «откат по журналу» нет,
  #   журнал архива «-откат» — шапка не «откат». Основа текста «не умеет»
  #   (решение (д)): update.py узнаёт старый раскладчик по шапке. Случай
  #   зелёный по построению — судит СТАРЫЙ код, не новый. Зелёным оставит:
  #   всё — это фиксация, а не проверка.
  OLD_COMMIT=11d7deb
  if git -C "$HERE/.." cat-file -e "$OLD_COMMIT^{commit}" 2>/dev/null; then
    C=$(fg_stand fg-g); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
    collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
    FWD="$C/копии/раскладка/стенд-20261024-000000-4"; RB="$FWD-откат"
    OLDP="$FB/old-fv"; mkdir -p "$OLDP"
    git -C "$HERE/.." archive "$OLD_COMMIT" harness | tar -x -C "$OLDP" || bad+=("стенд: пакет Ф-в из $OLD_COMMIT не собран")
    if [[ -d "$FB/A/UNIFIED" ]]; then cp -a "$FB/A/UNIFIED" "$OLDP/UNIFIED"; fi
    rc=0; out=$(run_pkg "$FB/B/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$FWD" 2>&1) || rc=$?
    [[ $rc -eq 0 ]] || bad+=("прямой B: код $rc (ждали 0)")
    rc=0; out=$(run_pkg "$OLDP/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$RB" -- --откат "$FWD" 2>&1) || rc=$?
    [[ $rc -eq 0 ]] || bad+=("прежний раскладчик с --откат: код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
    collect < <(forbid_texts "$out" "откат по журналу")
    head1=$(head -1 "$RB/journal.tsv" 2>/dev/null || true)
    [[ "$head1" != *" откат "* ]] || bad+=("шапка журнала прежнего раскладчика «$head1» — режим «откат» у старого кода")
    if [[ -e "$P/scripts/new.sh" ]]; then fg_new=на-месте; else fg_new=снят; fi
    echo "  замер фг-г: прежний раскладчик, new.sh $fg_new, шапка «${head1:-журнала нет}»"
    case_result "фг-г)" "прежний раскладчик (Ф-в $OLD_COMMIT) с --откат: код 0, «откат по журналу» нет, шапка не «откат» (фиксация старого кода)" ${bad[@]+"${bad[@]}"}
  else
    echo "  ПРОПУЩЕН фг-г) коммита Ф-в $OLD_COMMIT нет в истории рядом с пакетом ($HERE/..) — прежний раскладчик не собрать (фиксация, не проверка)"
  fi

  # фг-д) 1. Незнакомый довод «--откатт X» → код 3 «незнакомый довод» до замка,
  #   проект прежний (R1-15 Ф-в, решение (и2)). Ловит: «*) shift» — опечатка
  #   даёт обычную раскладку, update.py увидит это лишь по шапке. Зелёным
  #   оставит: незнакомое ЗНАЧЕНИЕ знакомого довода — судят фг-б и D-случаи.
  C=$(fg_stand fg-d1); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  project_snapshot "$P" > "$C/до"
  rc=0; out=$(run_pkg "$FB/A/harness" "$C" -- --откатт "$C/копии/раскладка/стенд-20261024-000000-5" 2>&1) || rc=$?
  project_snapshot "$P" > "$C/после"
  [[ $rc -eq 3 ]] || bad+=("код $rc (ждали 3)")
  collect < <(expect_texts "$out" "РАСКЛАДКА НЕ НАЧАТА" "незнакомый довод" "--откатт")
  [[ ! -e "$C/log/locks/layout.lock" ]] || bad+=("layout.lock создан — отказ не до замка")
  collect < <(snapshot_diff "$C/до" "$C/после")
  case_result "фг-д)" "1: незнакомый довод «--откатт» → код 3 до замка, довод назван, проект прежний" ${bad[@]+"${bad[@]}"}

  # 2. Замок держится и в откате: layout.lock держит проба → откат (ключ
  #   ожидания 1) → код 3 «идёт другая раскладка», проект, журнал прямого
  #   побайтно прежние, архива отката нет; после отпуска тот же откат — код 0,
  #   шапка «откат». Ловит: откат читает/пишет журнал прямого (разобран) до
  #   замка; откат обходит замок. Зелёным оставит: окно между отпуском замка и
  #   чтением журнала — судить нечем без шпиона.
  C=$(fg_stand fg-d2); P="$C/proj"; bad=(${A1_BAD[@]+"${A1_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_LOCK_WAIT_SEC 1)
  FWD="$C/копии/раскладка/стенд-20261024-000000-6"; RB="$FWD-откат"
  rc=0; out=$(run_pkg "$FB/B/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$FWD" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("прямой B: код $rc (ждали 0)")
  cp "$FWD/journal.tsv" "$C/журнал-прямого" 2>/dev/null || bad+=("стенд: журнала прямого $FWD/journal.tsv нет")
  lockf="$C/log/locks/layout.lock"
  probe_tool holder "$lockf" "$C/держу" "$C/отпусти" > "$C/держатель.out" 2>&1 & holder_job=$!
  if wait_file "$C/держу" 100; then
    project_snapshot "$P" > "$C/до"
    rc=0; out=$(run_pkg "$FB/A/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$RB" -- --откат "$FWD" 2>&1) || rc=$?
    project_snapshot "$P" > "$C/после"
    [[ $rc -eq 3 ]] || bad+=("откат при чужом замке: код $rc (ждали 3)")
    collect < <(expect_texts "$out" "РАСКЛАДКА НЕ НАЧАТА: идёт другая раскладка")
    collect < <(snapshot_diff "$C/до" "$C/после")
    cmp -s "$C/журнал-прямого" "$FWD/journal.tsv" || bad+=("откат при чужом замке тронул журнал прямого прогона")
    [[ ! -e "$RB" ]] || bad+=("откат при чужом замке создал архив отката $RB")
  else
    bad+=("стенд: держатель layout.lock не встал за 10 с: $(tail -1 "$C/держатель.out" 2>/dev/null || true)")
  fi
  : > "$C/отпусти"; wait "$holder_job" || true
  rc=0; out=$(run_pkg "$FB/A/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$RB" -- --откат "$FWD" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("откат после отпуска замка: код $rc (ждали 0)")
  collect < <(rollback_header "$RB" "$FWD")
  case_result "фг-д)" "2: layout.lock держит проба → откат код 3, журнал прямого и проект прежние, архива отката нет; после отпуска — откат с шапкой «откат»" ${bad[@]+"${bad[@]}"}

  # ══ Ф-д: затравка по истории пакета — фд-а…фд-и ══
  # Стенд-пакет H — копия пакета с главами рядом и СВОИМ git: master — пакет
  # как есть, ветка «страховка» — коммит с harness/ОПИСЬ.md (blob X) и пустым
  # harness/ПУСТО.md. H2 — тот же репозиторий, но коммит «страховки» доступен
  # только через refs/remotes/x/master. В H scripts/tg_send.sh — заглушка: любой
  # её зов оставляет метку «отправлено» двумя каталогами выше себя (у пакета —
  # $H, у разложенной копии — каталог стенда). Журнал вопроса и согласия пишет
  # проба в LOG_DIR стенда (<стенд>/log). FD_BAD — беды сборки стендов: без
  # истории случай был бы зелен даром.
  FD_BAD=()
  H="$FB/H"; mkdir -p "$H"; cp -a "$HERE" "$H/harness"
  if [[ -d "$FB/A/UNIFIED" ]]; then cp -a "$FB/A/UNIFIED" "$H/UNIFIED"; else FD_BAD+=("стенд: глав рядом с пакетом нет — группа unified пуста"); fi
  printf '#!/bin/sh\nd=$(cd "$(dirname "$0")/../.." && pwd)\necho "$*" >> "$d/отправлено"\n' > "$H/harness/scripts/tg_send.sh"
  chmod 755 "$H/harness/scripts/tg_send.sh"
  FD_X_TEXT='# опись, разложенная прежним кодом раскладки (стенд Ф-д)'
  FD_X=$(printf '%s\n' "$FD_X_TEXT" | git hash-object --stdin)
  FD_EMPTY=e69de29bb2d1d6434b8b29ae775ad8c2e48c5391
  { stand_git init -q "$H" && stand_git -C "$H" add -A && stand_git -C "$H" commit -q -m A \
      && stand_git -C "$H" checkout -q -b страховка \
      && printf '%s\n' "$FD_X_TEXT" > "$H/harness/ОПИСЬ.md" && : > "$H/harness/ПУСТО.md" \
      && stand_git -C "$H" add -A && stand_git -C "$H" commit -q -m страховка \
      && stand_git -C "$H" checkout -q -; } >/dev/null 2>&1 || FD_BAD+=("стенд: репозиторий пакета H не собран")
  [[ ! -e "$H/harness/ОПИСЬ.md" ]] || FD_BAD+=("стенд: harness/ОПИСЬ.md остался в рабочем дереве пакета H")
  fd_hist() {  # $1=репозиторий, дальше доводы выбора ссылок → строки --raw истории
    stand_git -C "$1" log --no-renames --raw --no-abbrev --format= "${@:2}" 2>/dev/null || true
  }
  grep -qF "$FD_X" <<<"$(fd_hist "$H" --branches --tags)" || FD_BAD+=("стенд: blob X не виден в --branches --tags пакета H")
  grep -qF "$FD_EMPTY" <<<"$(fd_hist "$H" --branches --tags -- harness/ПУСТО.md)" || FD_BAD+=("стенд: пустого blob по harness/ПУСТО.md в истории H нет")
  H2="$FB/H2"; cp -a "$H" "$H2"
  { sha_s=$(stand_git -C "$H2" rev-parse страховка) && stand_git -C "$H2" update-ref refs/remotes/x/master "$sha_s" \
      && stand_git -C "$H2" branch -q -D страховка; } >/dev/null 2>&1 || FD_BAD+=("стенд: ссылки пакета H2 не переложены")
  ! grep -qF "$FD_X" <<<"$(fd_hist "$H2" --branches --tags)" || FD_BAD+=("стенд: blob X виден в --branches --tags пакета H2 — пара не различает --all")
  grep -qF "$FD_X" <<<"$(fd_hist "$H2" --all)" || FD_BAD+=("стенд: blob X не виден и в --all пакета H2")
  # FD1 — проект после первой раскладки пакетом H (опись заведена) — образец случаев.
  FD1=$(fb_stand fd-base)
  fd1_rc=0; fd1_out=$(run_pkg "$H/harness" "$FD1" HARNESS_LAYOUT_PLAN_OUT="$FD1/план.tsv" 2>&1) || fd1_rc=$?
  [[ $fd1_rc -eq 0 ]] || FD_BAD+=("стенд: первая раскладка пакетом H — код $fd1_rc: $(tail -2 <<<"$fd1_out" | tr '\n' ' ')")
  [[ -f "$FD1/proj/.layout-manifest" ]] || FD_BAD+=("стенд: после первой раскладки H описи нет")
  rm -f "$FD1/отправлено" "$H/отправлено"
  fd_stand() {  # $1=имя → стенд с копией FD1 и harness/ОПИСЬ.md = X, если $2 == x
    local c
    c=$(fb_stand "$1" "$FD1/proj")
    [[ "${2:-}" != x ]] || printf '%s\n' "$FD_X_TEXT" > "$c/proj/harness/ОПИСЬ.md"
    printf '%s\n' "$c"
  }
  fd_question() {  # $1=стенд $2=ts, дальше пути → строка в <стенд>/log/раскладка-спрошено.jsonl
    mkdir -p "$1/log"
    python3 -c 'import json, sys; print(json.dumps({"ts": sys.argv[1], "пути": sys.argv[2:], "вопрос": "стенд"}, ensure_ascii=False))' \
      "$2" "${@:3}" >> "$1/log/раскладка-спрошено.jsonl"
  }
  fd_consent() {  # $1=стенд $2=ts $3=текст → запись диспетчера в <стенд>/log/confirmations.jsonl
    mkdir -p "$1/log"
    python3 -c 'import json, sys; print(json.dumps({"ts": sys.argv[1], "text": sys.argv[2], "message_id": "1", "kind": "any"}, ensure_ascii=False))' \
      "$2" "$3" >> "$1/log/confirmations.jsonl"
  }
  fd_blob() { git hash-object "$1" 2>/dev/null || echo нет; }
  fd_marked() {  # $1=журнал архива $2=путь $3=blob → беда, если «# по истории пакета:» не стоит перед «снят»
    local jl sl
    jl=$(grep -n -x -F "# по истории пакета: $2" "$1" 2>/dev/null | head -1 | cut -d: -f1 || true)
    sl=$(grep -n -x -F "$(printf 'снят\t%s\t-\t%s' "$3" "$2")" "$1" 2>/dev/null | head -1 | cut -d: -f1 || true)
    [[ -n "$jl" && -n "$sl" && "$jl" -lt "$sl" ]] \
      || printf 'в журнале нет пометки «# по истории пакета: %s» перед «снят» (строки %s / %s)\n' "$2" "${jl:-нет}" "${sl:-нет}"
    return 0
  }

  # фд-а) 1а: неизвестный harness/ОПИСЬ.md с blob X, X только в refs/heads/страховка
  #   → «по истории пакета снял бы 1: harness/ОПИСЬ.md; ждёт «да, убрать»», файл на
  #   месте, tg_send.sh не зван (§5: раскладка в канал не пишет). Ловит: раскладчик
  #   не передаёт двери --package-root; история не читается; снятие без вопроса;
  #   раскладка сама шлёт вопрос. Зелёным оставит: отправку мимо tg_send.sh
  #   (curl напрямую) — судит К16 по tg_out.
  C=$(fd_stand fd-a x); P="$C/proj"; bad=(${FD_BAD[@]+"${FD_BAD[@]}"})
  rc=0; out=$(run_pkg "$H/harness" "$C" 2>&1) || rc=$?
  FDA_C="$C"; FDA_OUT="$out"
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(expect_texts "$out" "по истории пакета снял бы 1: harness/ОПИСЬ.md; ждёт «да, убрать»")
  collect < <(forbid_texts "$out" "снято по истории пакета")
  [[ "$(fd_blob "$P/harness/ОПИСЬ.md")" == "$FD_X" ]] || bad+=("harness/ОПИСЬ.md снят или изменён без вопроса")
  for f in "$C/отправлено" "$H/отправлено"; do
    [[ ! -e "$f" ]] || bad+=("раскладка звала tg_send.sh ($f): $(head -c 200 "$f" | tr '\n' ' ')")
  done
  case_result "фд-а)" "1: X в refs/heads/страховка → «снял бы 1: harness/ОПИСЬ.md», файл на месте, tg_send.sh не зван" ${bad[@]+"${bad[@]}"}
  # 2. Пара: X только в refs/remotes/x/master → «снял бы» нет, путь истории не
  #   знает — «чужих вне описи N». Ловит: --all вместо --branches --tags.
  C=$(fd_stand fd-a2 x); P="$C/proj"; bad=(${FD_BAD[@]+"${FD_BAD[@]}"})
  rc=0; out=$(run_pkg "$H2/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(forbid_texts "$out" "по истории пакета снял бы" "снято по истории пакета")
  collect < <(expect_texts "$out" "чужих вне описи " " — история пакета их не знает")
  [[ "$(fd_blob "$P/harness/ОПИСЬ.md")" == "$FD_X" ]] || bad+=("harness/ОПИСЬ.md снят или изменён")
  [[ ! -e "$C/отправлено" ]] || bad+=("раскладка звала tg_send.sh")
  case_result "фд-а)" "2: X только в refs/remotes/x/master → «снял бы» нет, «чужих вне описи» есть" ${bad[@]+"${bad[@]}"}

  # фд-б) 1б: тот же файл + 1 байт → «не наш по содержимому: harness/ОПИСЬ.md», в
  #   «снял бы» его нет, файл на месте. Ловит: «наш» по пути без сверки blob.
  #   Зелёным оставит: правку, вернувшую blob к одному из blob истории, — это и
  #   есть «наш» по договору.
  C=$(fd_stand fd-b x); P="$C/proj"; bad=(${FD_BAD[@]+"${FD_BAD[@]}"})
  printf 'x' >> "$P/harness/ОПИСЬ.md"; fd_b_blob=$(fd_blob "$P/harness/ОПИСЬ.md")
  rc=0; out=$(run_pkg "$H/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(expect_texts "$out" "не наш по содержимому: harness/ОПИСЬ.md")
  line=$(grep -F "снял бы" <<<"$out" || true)
  [[ "$line" != *ОПИСЬ* ]] || bad+=("правленый harness/ОПИСЬ.md назван в «снял бы»: $line")
  [[ "$(fd_blob "$P/harness/ОПИСЬ.md")" == "$fd_b_blob" ]] || bad+=("правленый harness/ОПИСЬ.md снят или изменён")
  case_result "фд-б)" "X + 1 байт → «не наш по содержимому: harness/ОПИСЬ.md», не в «снял бы», на месте" ${bad[@]+"${bad[@]}"}

  # фд-в) 1в: пустой harness/ПУСТО.md, пустой blob в истории «страховки» по этому
  #   пути → путь не назван ни одной строкой (ни «снял бы», ни «не наш»), счёт
  #   «пустых вне описи N» есть (решение (д)). Ловит: пустой blob признан нашим;
  #   пустой — «не наш». Зелёным оставит: пустой, посчитанный «чужим», при другом
  #   пустом файле в проекте стенда — счёт «пустых» здесь не точный.
  C=$(fd_stand fd-v); P="$C/proj"; bad=(${FD_BAD[@]+"${FD_BAD[@]}"})
  : > "$P/harness/ПУСТО.md"
  rc=0; out=$(run_pkg "$H/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(expect_texts "$out" "пустых вне описи " " — затравка их не судит")
  collect < <(forbid_texts "$out" "ПУСТО.md")
  [[ -f "$P/harness/ПУСТО.md" && ! -s "$P/harness/ПУСТО.md" ]] || bad+=("harness/ПУСТО.md снят или изменён")
  case_result "фд-в)" "пустой файл с пустым blob в истории → не назван, «пустых вне описи» есть, на месте" ${bad[@]+"${bad[@]}"}

  # фд-г) 1г: устаревшая опись. Стенд-пакет Hg: коммит 1 — scripts/f.sh = Fa,
  #   коммит 2 — Fb (пакет B), коммит 3 — без f.sh (пакет C). Проект: раскладка B
  #   (опись знает f.sh = Fb), затем f.sh перезаписан Fa («раскладка старым
  #   кодом»), затем раскладка C → «снято по истории пакета: scripts/f.sh», в
  #   files/ архива Fa, в журнале пометка и «снят <Fa>» (blob ДИСКА, Р8) до
  #   «готово». Ловит: ours= не дошёл до judge_extra (правлен здесь → оставлено);
  #   кандидат с blob описи (Fb) — дверь не снимет или Stop.
  #   Пара 1: f.sh перезаписан байтами вне истории → «оставлено 1: scripts/f.sh».
  #   Пара 2 (расх. 11): f.sh уже в секции «оставлено», диск снова Fa → снят.
  #   Зелёным оставит: снятие «правленого здесь» по blob ДРУГОГО пути истории —
  #   судят H5/DS9.
  Hg="$FB/Hg"; mkdir -p "$Hg"; cp -a "$H/harness" "$Hg/harness"
  if [[ -d "$H/UNIFIED" ]]; then cp -a "$H/UNIFIED" "$Hg/UNIFIED"; fi
  fd_f="$Hg/harness/scripts/f.sh"
  printf '#!/bin/sh\necho f версии A\n' > "$fd_f"; chmod 755 "$fd_f"; FD_FA=$(fd_blob "$fd_f")
  FD_FA_SAVED="$FB/f-a.sh"; cp -p "$fd_f" "$FD_FA_SAVED"
  FDG_BAD=(${FD_BAD[@]+"${FD_BAD[@]}"})
  { stand_git init -q "$Hg" && stand_git -C "$Hg" add -A && stand_git -C "$Hg" commit -q -m Fa \
      && printf '#!/bin/sh\necho f версии B\n' > "$fd_f" \
      && stand_git -C "$Hg" add -A && stand_git -C "$Hg" commit -q -m Fb; } >/dev/null 2>&1 || FDG_BAD+=("стенд: репозиторий пакета Hg не собран")
  FD_FB=$(fd_blob "$fd_f")
  for k in fd-g fd-g2 fd-g3; do
    c=$(fd_stand "$k")
    while IFS= read -r l; do [[ -n "$l" ]] && FDG_BAD+=("$k: $l"); done < <(set_conf_key "$c/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
    rc=0; out=$(run_pkg "$Hg/harness" "$c" 2>&1) || rc=$?
    [[ $rc -eq 0 ]] || FDG_BAD+=("стенд $k: раскладка B — код $rc: $(tail -2 <<<"$out" | tr '\n' ' ')")
    grep -q "^scripts/f.sh"$'\t'"$FD_FB"$'\t' <(manifest_body "$c/proj") || FDG_BAD+=("стенд $k: после B в описи нет scripts/f.sh = Fb")
  done
  FDG="$T/cases/fd-g"; FDG2="$T/cases/fd-g2"; FDG3="$T/cases/fd-g3"
  cp -p "$FD_FA_SAVED" "$FDG/proj/scripts/f.sh"
  printf '#!/bin/sh\necho f правлен здесь\n' > "$FDG2/proj/scripts/f.sh"; fd_fc=$(fd_blob "$FDG2/proj/scripts/f.sh")
  cp -p "$FDG2/proj/scripts/f.sh" "$FDG3/proj/scripts/f.sh"
  { rm -f "$fd_f" && stand_git -C "$Hg" add -A && stand_git -C "$Hg" commit -q -m C; } >/dev/null 2>&1 || FDG_BAD+=("стенд: коммит C пакета Hg не собран")
  grep -qF "$FD_FA" <<<"$(fd_hist "$Hg" --branches --tags)" || FDG_BAD+=("стенд: Fa не виден в истории Hg")
  fd_g_removed() {  # $1=стенд $2=вывод $3=архивы «до» → беды строками
    local arch n_arch
    expect_texts "$2" "снято по истории пакета: scripts/f.sh"
    [[ ! -e "$1/proj/scripts/f.sh" ]] || echo "scripts/f.sh остался в проекте"
    arch=$(new_archive "$3" "$1")
    n_arch=$(grep -c . <<<"$arch" || true)
    if [[ "$n_arch" != 1 ]]; then
      echo "новых архивов $n_arch (ждали 1)"
    else
      [[ "$(fd_blob "$arch/files/scripts/f.sh")" == "$FD_FA" ]] || echo "в files/ архива нет scripts/f.sh с blob Fa"
      probe_tool journal "$arch/journal.tsv" scripts/f.sh "$FD_FA"
      fd_marked "$arch/journal.tsv" scripts/f.sh "$FD_FA"
    fi
    return 0
  }
  C="$FDG"; bad=(${FDG_BAD[@]+"${FDG_BAD[@]}"})
  before=$(archive_list "$C")
  rc=0; out=$(run_pkg "$Hg/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(fd_g_removed "$C" "$out" "$before")
  case_result "фд-г)" "1: опись f.sh = Fb, диск Fa ∈ истории, пакет без f.sh → «снято по истории пакета: scripts/f.sh», архив Fa, «снят <Fa>» с пометкой" ${bad[@]+"${bad[@]}"}
  C="$FDG2"; bad=(${FDG_BAD[@]+"${FDG_BAD[@]}"})
  rc=0; out=$(run_pkg "$Hg/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  line=$(summary_line "$out")
  [[ "$(kept_part "$line")" == "оставлено 1: scripts/f.sh" ]] || bad+=("в сводке «$(kept_part "$line")», ждали «оставлено 1: scripts/f.sh»")
  collect < <(forbid_texts "$out" "снято по истории пакета")
  [[ "$(fd_blob "$C/proj/scripts/f.sh")" == "$fd_fc" ]] || bad+=("правленый здесь scripts/f.sh снят или изменён")
  case_result "фд-г)" "2: пара — диск вне истории → «оставлено 1: scripts/f.sh», файл на месте" ${bad[@]+"${bad[@]}"}
  C="$FDG3"; bad=(${FDG_BAD[@]+"${FDG_BAD[@]}"})
  rc=0; out=$(run_pkg "$Hg/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("стенд: раскладка C с правкой вне истории — код $rc")
  probe_tool kept "$C/proj" | grep -qF scripts/f.sh || bad+=("стенд: scripts/f.sh не попал в секцию «# оставлено» — второй пары нет")
  cp -p "$FD_FA_SAVED" "$C/proj/scripts/f.sh"
  before=$(archive_list "$C")
  rc=0; out=$(run_pkg "$Hg/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(fd_g_removed "$C" "$out" "$before")
  case_result "фд-г)" "3: путь в секции «оставлено», диск снова Fa → снят по истории, архив Fa, «снят <Fa>» с пометкой" ${bad[@]+"${bad[@]}"}

  # фд-д) 1д: вопрос в журнале стенда называет harness/ОПИСЬ.md; согласие —
  #   1) «да, убрать» ПОСЛЕ вопроса → снят в архив (пометка, «снят X», «готово»);
  #   2) «да» (kind any) после вопроса → только назван, строка «вопрос … задан»;
  #   3) «да, убрать» ДО вопроса → только назван. Ловит: согласие не судится;
  #   «да» засчитано; время согласия не сравнивается с вопросом. Зелёным
  #   оставит: согласие старше 7 суток — судит H8.
  fd_q=$(date -Is -d '-2 hours'); fd_q_show=$(date -d "$fd_q" '+%Y-%m-%d %H:%M')
  fd_after=$(date -Is -d '-1 hours'); fd_before=$(date -Is -d '-3 hours')
  C=$(fd_stand fd-d1 x); P="$C/proj"; bad=(${FD_BAD[@]+"${FD_BAD[@]}"})
  fd_question "$C" "$fd_q" harness/ОПИСЬ.md; fd_consent "$C" "$fd_after" "да, убрать"
  before=$(archive_list "$C")
  rc=0; out=$(run_pkg "$H/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(expect_texts "$out" "снято по истории пакета: harness/ОПИСЬ.md")
  [[ ! -e "$P/harness/ОПИСЬ.md" ]] || bad+=("harness/ОПИСЬ.md остался в проекте при согласии")
  arch=$(new_archive "$before" "$C"); n_arch=$(grep -c . <<<"$arch" || true)
  if [[ "$n_arch" != 1 ]]; then
    bad+=("новых архивов $n_arch (ждали 1)")
  else
    [[ "$(fd_blob "$arch/files/harness/ОПИСЬ.md")" == "$FD_X" ]] || bad+=("в files/ архива нет harness/ОПИСЬ.md с blob X — снят без копии")
    collect < <(probe_tool journal "$arch/journal.tsv" harness/ОПИСЬ.md "$FD_X")
    collect < <(fd_marked "$arch/journal.tsv" harness/ОПИСЬ.md "$FD_X")
  fi
  case_result "фд-д)" "1: «да, убрать» после вопроса → снят в архив с blob X, пометка и «снят» в журнале" ${bad[@]+"${bad[@]}"}
  for variant in да до; do
    C=$(fd_stand "fd-d-$variant" x); P="$C/proj"; bad=(${FD_BAD[@]+"${FD_BAD[@]}"})
    fd_question "$C" "$fd_q" harness/ОПИСЬ.md
    if [[ "$variant" == да ]]; then fd_consent "$C" "$fd_after" "да"; else fd_consent "$C" "$fd_before" "да, убрать"; fi
    rc=0; out=$(run_pkg "$H/harness" "$C" 2>&1) || rc=$?
    [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
    collect < <(expect_texts "$out" "по истории пакета снял бы 1: harness/ОПИСЬ.md; ждёт «да, убрать»" \
      "вопрос о затравке задан $fd_q_show, «да, убрать» после него нет")
    collect < <(forbid_texts "$out" "снято по истории пакета")
    [[ "$(fd_blob "$P/harness/ОПИСЬ.md")" == "$FD_X" ]] || bad+=("harness/ОПИСЬ.md снят без согласия")
    if [[ "$variant" == да ]]; then
      case_result "фд-д)" "2: «да» (any) после вопроса → только назван, файл на месте" ${bad[@]+"${bad[@]}"}
    else
      case_result "фд-д)" "3: «да, убрать» ДО вопроса → только назван, файл на месте" ${bad[@]+"${bad[@]}"}
    fi
  done

  # фд-е) пакет без .git (копия без истории) + неизвестный файл → «затравки нет:
  #   пакет без истории», код 0, «раскладка завершена», файл на месте. Ловит:
  #   отказ git роняет раскладку (set -e, код 1 → update.py откатит). Зелёным
  #   оставит: PKG_ROOT всегда $HERE/.. — здесь git всё равно откажет (К13).
  NG="$FB/NoGit"; mkdir -p "$NG"; cp -a "$H/harness" "$NG/harness"
  if [[ -d "$H/UNIFIED" ]]; then cp -a "$H/UNIFIED" "$NG/UNIFIED"; fi
  C=$(fd_stand fd-e x); P="$C/proj"; bad=(${FD_BAD[@]+"${FD_BAD[@]}"})
  ! git -C "$NG" rev-parse --show-toplevel >/dev/null 2>&1 || bad+=("стенд: копия без .git лежит внутри чужого репозитория")
  rc=0; out=$(run_pkg "$NG/harness" "$C" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(expect_texts "$out" "затравки нет: пакет без истории" "раскладка завершена")
  [[ "$(fd_blob "$P/harness/ОПИСЬ.md")" == "$FD_X" ]] || bad+=("harness/ОПИСЬ.md снят или изменён")
  case_result "фд-е)" "пакет без .git → «затравки нет: пакет без истории», код 0, раскладка завершена, файл на месте" ${bad[@]+"${bad[@]}"}

  # фд-ж) расх. 6: вторая раскладка (фд-а): живые настройки (LIVE_SETTINGS) лежат
  #   побайтно равными blob истории и вне плана → ни одна не названа строкой
  #   затравки; «снял бы» — ровно 1 (ОПИСЬ.md). Ловит: запретный список не
  #   применён к затравке первым. Зелёным оставит: живую настройку, правленую
  #   владельцем (blob вне истории) и названную «не наш», — её судит H6.
  C="$FDA_C"; P="$C/proj"; out="$FDA_OUT"; bad=(${FD_BAD[@]+"${FD_BAD[@]}"})
  n_live=0
  for name in $LIVE_NAMES; do
    f="$P/harness/config/$name"
    [[ -f "$f" ]] || continue
    if grep -qF "$(fd_blob "$f")" <<<"$(fd_hist "$H" --branches --tags -- "harness/config/$name")"; then
      n_live=$((n_live + 1))
      grep -q "^harness/config/$name"$'\t' <(manifest_body "$P") && bad+=("стенд: harness/config/$name в теле описи — он не «вне плана»")
      collect < <(forbid_texts "$out" "harness/config/$name")
    fi
  done
  (( n_live > 0 )) || bad+=("стенд: ни одна живая настройка проекта не равна blob истории — судить нечего")
  collect < <(expect_texts "$out" "по истории пакета снял бы 1: harness/ОПИСЬ.md")
  case_result "фд-ж)" "живые настройки ($n_live) равны blob истории и вне плана → не названы; «снял бы 1»" ${bad[@]+"${bad[@]}"}

  # фд-з) режим отката: прямой B (архив прямого), затем неизвестный наш
  #   harness/ОПИСЬ.md + вопрос + «да, убрать», затем откат пакетом H (история
  #   есть) → «затравки нет: режим отката», файл на месте. Ловит: затравка
  #   считается и снимает в откате. Зелёным оставит: откат, вернувший снятое по
  #   затравке прямым прогоном, — судит UZ5.
  C=$(fd_stand fd-z); P="$C/proj"; bad=(${FD_BAD[@]+"${FD_BAD[@]}"})
  collect < <(set_conf_key "$C/harness.conf" LAYOUT_REMOVE_MAX_PERCENT 100)
  FWD="$C/копии/раскладка/стенд-20261024-000000-7"; RB="$FWD-откат"
  rc=0; out=$(run_pkg "$FB/B/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$FWD" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("стенд: прямой B — код $rc: $(tail -2 <<<"$out" | tr '\n' ' ')")
  [[ -f "$FWD/journal.tsv" ]] || bad+=("стенд: журнала прямого $FWD/journal.tsv нет — откатывать нечего")
  printf '%s\n' "$FD_X_TEXT" > "$P/harness/ОПИСЬ.md"
  fd_question "$C" "$fd_q" harness/ОПИСЬ.md; fd_consent "$C" "$fd_after" "да, убрать"
  rc=0; out=$(run_pkg "$H/harness" "$C" HARNESS_LAYOUT_ARCHIVE="$RB" -- --откат "$FWD" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("откат: код $rc (ждали 0): $(tail -2 <<<"$out" | tr '\n' ' ')")
  collect < <(rollback_header "$RB" "$FWD")
  collect < <(expect_texts "$out" "затравки нет: режим отката")
  collect < <(forbid_texts "$out" "снято по истории пакета" "по истории пакета снял бы")
  [[ "$(fd_blob "$P/harness/ОПИСЬ.md")" == "$FD_X" ]] || bad+=("harness/ОПИСЬ.md снят в откате")
  case_result "фд-з)" "откат пакетом с историей + согласие → «затравки нет: режим отката», файл на месте" ${bad[@]+"${bad[@]}"}

  # фд-и) таблица ⊇ правила раскладки: для КАЖДОЙ записи плана двери (план
  #   первой раскладки H и раскладки Hs — публичная сборка, главы в
  #   sluzhebnoe/UNIFIED) project_path(источник от корня пакета) == назначение.
  #   Таблица — $HERE/config, модуль — $HERE/scripts/lib (то, что судим). Записей
  #   > 0 по каждому плану. Ловит: таблица отстала от раскладки (новая группа,
  #   перенос глав). Зелёным оставит: обратное направление — «harness/<p>»
  #   приписывает «наш» и путям, куда раскладка не клала (risk_flags контракта).
  cat > "$FB/fd_table.py" <<'PY'
import os, sys
from pathlib import Path

here = sys.argv[1]
sys.path.insert(0, os.path.join(here, "scripts", "lib"))
try:
    import layout_history as history
    rules = history.load_rules(os.path.join(here, "config", "layout-history-paths.yaml"))
except Exception as error:  # проба судит, а не падает: отсутствие модуля — беда случая
    print(f"модуль истории или таблица не загрузились: {type(error).__name__}: {error}")
    sys.exit(0)
for plan, root in zip(sys.argv[2::2], sys.argv[3::2]):
    root = os.path.realpath(root)
    records = [r for r in Path(plan).read_bytes().decode("utf-8").split("\0") if r] if Path(plan).is_file() else []
    if not records:
        print(f"план {plan}: записей 0 — таблицу судить не с чем")
        continue
    wrong = []
    for record in records:
        group, src, dest = record.split("\t")
        rel = os.path.relpath(os.path.realpath(src), root)
        if rel.startswith(".."):
            wrong.append(f"{src}: источник вне корня пакета")
            continue
        got = history.project_path(rel, rules)
        if got != dest:
            wrong.append(f"{rel} → {got!r}, раскладка кладёт в {dest}")
    if wrong:
        print(f"план {plan}: расхождений {len(wrong)} из {len(records)}: " + "; ".join(wrong[:4]))
PY
  Hs="$FB/Hs"; mkdir -p "$Hs/sluzhebnoe"; cp -a "$H/harness" "$Hs/harness"
  if [[ -d "$H/UNIFIED" ]]; then cp -a "$H/UNIFIED" "$Hs/sluzhebnoe/UNIFIED"; fi
  C=$(fd_stand fd-i); bad=(${FD_BAD[@]+"${FD_BAD[@]}"})
  rc=0; out=$(run_pkg "$Hs/harness" "$C" HARNESS_LAYOUT_PLAN_OUT="$C/план.tsv" 2>&1) || rc=$?
  [[ $rc -eq 0 ]] || bad+=("стенд: раскладка публичной сборки — код $rc: $(tail -2 <<<"$out" | tr '\n' ' ')")
  n_sl=$(tr '\0' '\n' < "$C/план.tsv" 2>/dev/null | grep -c 'sluzhebnoe/UNIFIED/' || true)
  (( n_sl > 0 )) || bad+=("стенд: в плане публичной сборки нет источников sluzhebnoe/UNIFIED/ — перенос глав не судится")
  collect < <(PYTHONDONTWRITEBYTECODE=1 python3 -B "$FB/fd_table.py" "$HERE" "$FD1/план.tsv" "$H" "$C/план.tsv" "$Hs")
  case_result "фд-и)" "таблица перевода ⊇ плана раскладки (пакет и публичная сборка, глав sluzhebnoe $n_sl)" ${bad[@]+"${bad[@]}"}

  chmod -R u+w "$T" 2>/dev/null || true
  rm -rf "$T"
  [[ $ok_flag -eq 1 ]] && echo "SELFTEST: зелёный" || exit 1
  exit 0
fi

# Доводы раскладчика: снять отложенное порогом, откатить по журналу прямого
# прогона. Все отказы идут ДО замка и до чтения конфига: под set -u голый `$2`
# убил бы раскладчик кодом 1, а update.py откатывает по любому коду, кроме 3.
# Незнакомый довод тоже отказ: опечатка («--откатт») иначе молча дала бы
# обычную раскладку вместо отката.
LIFT_ARGS=()
ROLLBACK_ARGS=()
while (( $# )); do
  case "$1" in
    --снять-сверх-порога)
      if [[ -z "${2:-}" ]]; then
        echo "РАСКЛАДКА НЕ НАЧАТА: --снять-сверх-порога без причины" >&2
        exit 3
      fi
      LIFT_ARGS=(--lift-threshold "$2")
      shift 2 ;;
    --откат)
      if [[ -z "${2:-}" ]]; then
        echo "РАСКЛАДКА НЕ НАЧАТА: откат по журналу: довод --откат без пути" >&2
        exit 3
      fi
      ROLLBACK_ARGS=(--rollback-of "$2")
      shift 2 ;;
    *)
      echo "РАСКЛАДКА НЕ НАЧАТА: незнакомый довод: $1" >&2
      exit 3 ;;
  esac
done
# Порог снимает отложенное прямым прогоном; откат ничего не откладывает, и
# молча проигнорированный довод оставил бы владельца с мыслью, что он действует.
if (( ${#LIFT_ARGS[@]} && ${#ROLLBACK_ARGS[@]} )); then
  echo "РАСКЛАДКА НЕ НАЧАТА: довод --откат и --снять-сверх-порога несовместимы" >&2
  exit 3
fi

[[ -f "$CONF" ]] || { echo "нет конфига $CONF — сначала шаг 0 из 01-SPEC-УСТАНОВКА"; exit 1; }
# Окружение старше конфига — тем же загрузчиком, что и весь харнес. Здесь он
# берётся ИЗ ПАКЕТА: в проекте его ещё нет, раскладка сама его и кладёт.
INSTALL_CONF="$CONF"
# shellcheck disable=SC1091
source "$HERE/scripts/lib/config.sh"
konf_zagruzit
[[ -n "${PROJECT_DIR:-}" ]] || { echo "PROJECT_DIR пуст в $CONF"; exit 1; }

# ── замок раскладки — на ВЕСЬ прогон ────────────────────────────────────────
# Две раскладки разом читают одну опись и пишут один проект: вторая снимет то,
# что первая только что положила. Замок берётся ДО первого mkdir и держится
# дескриптором, который наследуют дверь и генераторы (BUILD-SKILLS.py и др.):
# если update.py убьёт bash по таймауту, а генератор ещё пишет, замок живёт,
# пока жив последний писатель. Поэтому без `flock -o` и без закрытия
# дескриптора для детей; отказ `exec` не глушится — причину ОС называет bash.
LOCK_DIR="$LOG_DIR/locks"
mkdir -p "$LOCK_DIR" || { echo "РАСКЛАДКА НЕ НАЧАТА: замок раскладки не открыт: $LOCK_DIR" >&2; exit 3; }
if ! exec {LAYOUT_LOCK_FD}>>"$LOCK_DIR/layout.lock"; then
  echo "РАСКЛАДКА НЕ НАЧАТА: замок раскладки не открыт: $LOCK_DIR/layout.lock" >&2
  exit 3
fi
LOCK_WAIT="${LAYOUT_LOCK_WAIT_SEC:-}"
if [[ "$LOCK_WAIT" =~ ^[0-9]+$ ]]; then
  LOCK_FLAGS=(-w "$LOCK_WAIT"); LOCK_WAITED="$LOCK_WAIT"
else
  LOCK_FLAGS=(-n); LOCK_WAITED=0
fi
if ! flock "${LOCK_FLAGS[@]}" "$LAYOUT_LOCK_FD"; then
  if [[ -z "$LOCK_WAIT" ]]; then
    echo "ключ LAYOUT_LOCK_WAIT_SEC не задан — без ожидания" >&2
  elif [[ ! "$LOCK_WAIT" =~ ^[0-9]+$ ]]; then
    echo "ключ LAYOUT_LOCK_WAIT_SEC не число: $LOCK_WAIT — без ожидания" >&2
  fi
  # Свой дескриптор закрыт до расспроса: иначе держателем назван был бы сам
  # раскладчик. pid берётся из /proc/*/fd, не из /proc/locks: там pid вышедшего
  # flock(1), а зависший генератор после убитого bash в списке не стоит вовсе.
  exec {LAYOUT_LOCK_FD}>&-
  # Держатель — в строке маркера: update.py отдаёт владельцу только её.
  HOLDER=$(python3 -B "$HERE/scripts/lib/layout_lock.py" --who-holds "$LOCK_DIR/layout.lock" 2>/dev/null) \
    || HOLDER="держатель не найден: расспрос замка не удался"
  echo "РАСКЛАДКА НЕ НАЧАТА: идёт другая раскладка (ждал $LOCK_WAITED с, $HOLDER): $LOCK_DIR/layout.lock" >&2
  exit 3
fi
# update.lock — замок обновления харнеса (update.py). Только чтение: пятого
# места записи раскладка не заводит. Держит предок — раскладка идёт, иначе нет.
python3 -B "$HERE/scripts/lib/layout_lock.py" --update-lock "$LOCK_DIR/update.lock" --layout-pid $$ \
  || exit $?

# Создание каталогов — не под set -e: их отказ это тот же отказ прав, и
# рассказать о нём должна проверка ниже, одним внятным сообщением, а не голое
# «mkdir: Permission denied» без подсказки, что делать.
mkdir -p "$PROJECT_DIR"/{scripts/hooks,harness/demons,.claude,docs/handover} 2>/dev/null || true

# ── права на запись — ДО первого копирования ────────────────────────────────
# Улика 11.08.2026: раскладка упала на cp посередине — часть каталогов
# принадлежала root после установки под sudo. Спас только set -e, но дерево
# осталось полуобновлённым: новые скрипты рядом со старыми, и никто не знает,
# какие из них какие. Полуобновлённое дерево хуже необновлённого, поэтому
# проверка идёт до первого cp: либо копируется всё, либо ни одного файла.
ближайший_существующий() { # каталог → он сам или ближайший существующий предок
  local d="$1"
  while [[ ! -d "$d" && "$d" != "/" ]]; do d=$(dirname "$d"); done
  printf '%s\n' "$d"
}
BLOCKED=()
for d in "$PROJECT_DIR" "$PROJECT_DIR/scripts" "$PROJECT_DIR/scripts/hooks" \
         "$PROJECT_DIR/harness" "$PROJECT_DIR/harness/demons" \
         "$PROJECT_DIR/.claude" "$PROJECT_DIR/docs/handover"; do
  # -w и -x оба: без бита x в каталог нельзя войти, и cp падает даже при -w.
  # Несозданный каталог — тот же отказ прав: mkdir выше уже пробовал.
  [[ -d "$d" && -w "$d" && -x "$d" ]] || BLOCKED+=("$(ближайший_существующий "$d")")
done
if (( ${#BLOCKED[@]} )); then
  # Каталоги схлопнулись до общих предков — чинить и показывать надо их.
  mapfile -t BLOCKED < <(printf '%s\n' "${BLOCKED[@]}" | sort -u)
  echo "РАСКЛАДКА НЕ НАЧАТА: нет права записи (ни один файл не скопирован)." >&2
  echo "Пользователь: $(id -un), каталоги:" >&2
  ls -ld "${BLOCKED[@]}" >&2
  echo >&2
  echo "Почините права и повторите — команда целиком:" >&2
  echo "  sudo chown -R $(id -un):$(id -gn) ${BLOCKED[*]}" >&2
  exit 3
fi
# Раскладчик только СОБИРАЕТ план укладки; файлы в проект кладёт дверь
# (scripts/layout-door.py) одним вызовом: сначала предпроверка и копии
# перекрываемого, потом запись. Укладка файла прямо отсюда писала поверх чужого
# без копии, и «проект не тронут» при отказе было нечем доказать.
PLAN=$(mktemp)
MARKS=""
trap 'rm -f "$PLAN"; [[ -z "$MARKS" ]] || rm -rf "$MARKS"' EXIT

plan_add() {  # $1=группа  $2=источник  $3=путь в проекте
  printf '%s\t%s\t%s\0' "$1" "$2" "${3#"$PROJECT_DIR/"}" >> "$PLAN"
}

# Группа без источника в пакете: дверь не должна принять её файлы в описи за
# «лишнее» и снять. Назначение каталога несёт «/» на конце.
plan_absent() {  # $1=группа  $2=назначение (каталог — с «/»)
  printf '%s\t-\t%s\0' "$1" "${2#"$PROJECT_DIR/"}" >> "$PLAN"
}

polozhit_fajl() {  # $1=группа  $2=источник  $3=цель
  plan_add "$1" "$2" "$3"
}

polozhit_v() {  # $1=группа  $2=каталог назначения, далее — файлы
  local group="$1" dst="$2" f
  shift 2
  for f in "$@"; do
    [[ -f "$f" ]] || continue
    plan_add "$group" "$f" "$dst/$(basename "$f")"
  done
}

lay_tree() {  # $1=группа  $2=каталог-источник  $3=каталог-назначение
  local group="$1" src="${2%/}" dst="${3%/}" f
  # Ошибка find внутри `< <(…)` под set -e не видна: без этой проверки пропавший
  # каталог пакета даёт пустую группу и «раскладка завершена».
  [[ -d "$src" ]] || { echo "нет каталога пакета: $src" >&2; exit 1; }
  while IFS= read -r -d '' f; do
    plan_add "$group" "$f" "$dst/${f#"$src"/}"
  done < <(find "$src" -name __pycache__ -prune -o \( -type f -o -type l \) -print0 | sort -z)
}

# Байткод в источнике ломает cp (каталог среди файлов) — та самая улика:
# любой прогон py-тестов рядом с пакетом создаёт __pycache__. Чистим до копирования.
find "$HERE" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
# Конфиги раньше хуков: новый сторож модели не должен встать без файла масок.
# Таблицы-данные механизмов: инварианты (покрытие) и удержание (политика путей
# записи). Раскладчик их не переносил, и гейт сверки с пакетом их не сторожил —
# правка в проекте пережила бы переустановку только случайно (10.09.2026).
# Образец harness.conf едет в проект побайтно и затирает прежний: гейт ключей
# образца судит именно этот файл, а живой конфиг лежит в /etc/harness и здесь
# не трогается. logrotate-harness остаётся в стороне: его место — /etc/harness.
polozhit_fajl config "$HERE/config/harness.conf" "$PROJECT_DIR/harness/config/harness.conf"
# Пульт владельца (pipeline.yaml) — ЖИВЫЕ данные сервера, а не пакета: 11.09
# владелец включил шаги для мелких и обычных задач, и раскладка 12.09 вернула
# ему пакетные умолчания молча. Файл кладётся ТОЛЬКО если его ещё нет — так же,
# как harness.conf в /etc. Гейт сверки с пакетом его тоже не сторожит.
LIVE_SETTINGS="pipeline.yaml queue.yaml models.yaml audit.yaml live-check-state.yaml"   # имя латиницей: bash не берёт кириллические имена
# live-check-state.yaml — записи живой проверки ЭТОЙ установки: затирание
# заменило бы их заготовкой из пакета, то есть потеряло бы работу (И-1).
for f in "$HERE"/config/*.yaml "$HERE"/config/*.txt "$HERE"/config/*.gz; do
  [[ -f "$f" ]] || continue
  name="$(basename "$f")"
  if [[ " $LIVE_SETTINGS " == *" $name "* ]] && [[ -f "$PROJECT_DIR/harness/config/$name" ]]; then
    echo "  $name — настройки владельца, не трогаю"
    continue
  fi
  polozhit_fajl config "$f" "$PROJECT_DIR/harness/config/$name"
done
# код — всегда обновляется
polozhit_v hooks "$PROJECT_DIR/scripts/hooks" "$HERE"/hooks/*.py "$HERE"/hooks/*.sh
polozhit_fajl settings "$HERE/hooks/project-settings.json" "$PROJECT_DIR/.claude/settings.json"
# Свои типы помощников (omitClaudeMd: старт без CLAUDE.md проекта): оболочка
# читает их только из <проект>/.claude/agents.
lay_tree agents "$HERE"/agents "$PROJECT_DIR/.claude/agents"
# «-r» обязателен: в scripts/ лежит КАТАЛОГ lib/ с общим модулем чтения
# конфига, от которого зависят четыре гейта. Без него cp печатал «omitting
# directory», раскладка шла дальше, и на новом сервере эти гейты падали бы
# импортом — а проверить было нечем: живой прогон раскладки из чистого клона
# делался впервые 10.09.2026 и нашёл это первым же запуском.
# Файлы верхнего уровня — через mv (среди них живой tg-dispatcher.sh),
# подкаталоги (lib/) — обычным cp -r: их никто не исполняет.
polozhit_v scripts "$PROJECT_DIR/scripts" "$HERE"/scripts/*
for subdir in "$HERE"/scripts/*/; do
  [[ -d "$subdir" ]] || continue
  subdir_name=$(basename "$subdir")
  lay_tree scripts-sub "$subdir" "$PROJECT_DIR/scripts/$subdir_name"
done
polozhit_v demons "$PROJECT_DIR/harness/demons" "$HERE"/demons/*
# Веб-панель управления: 11 файлов службы, которых раскладка не знала вовсе —
# правка панели не доезжала до другой машины (ревью спеки обновления, C5).
if [[ -d "$HERE/panel" ]]; then
  polozhit_v panel "$PROJECT_DIR/harness/panel" "$HERE"/panel/*
else
  plan_absent panel "$PROJECT_DIR/harness/panel/"
fi
# harness/ на сервере зеркалит пакет: скилы, шаблоны, генератор, опись, плагин —
# пути «harness/skills» и т.д. из harness.conf и CLAUDE.md остаются верными
# skills-src/ доставляется ОБЯЗАТЕЛЬНО: генератор скилов берёт из него источники
# типа PACKAGE: (таблица расширений). Без него любая пересборка скилов на
# сервере падает «нет файла пакета skills-src/…» — а пересборка нужна и при
# дополнении таблицы расширений (01-SPEC §8), и при установке возможности.
# Улика: сквозной прогон установки возможности, 11.08 — установка набора
# дошла до конца и честно сказала «скилы не собраны».
lay_tree skills "$HERE"/skills "$PROJECT_DIR/harness/skills"
lay_tree skills-src "$HERE"/skills-src "$PROJECT_DIR/harness/skills-src"
lay_tree templates "$HERE"/шаблоны-задач "$PROJECT_DIR/harness/шаблоны-задач"
# Возможности: КОД набора обновляется всегда, а метки `.установлено` — живые
# данные и принадлежат серверу. Метки снимаются копией ДО двери и возвращаются
# ПОСЛЕ неё (ниже): иначе доставка кода «отключала» уже установленный набор, и
# скилы возможности исчезали молча. Дверь отказала — копия в $MARKS уходит
# с trap, а метки в проекте не тронуты.
if [[ -d "$HERE/vozmozhnosti" ]]; then
  MARKS=$(mktemp -d)
  if [[ -d "$PROJECT_DIR/harness/vozmozhnosti" ]]; then
    while IFS= read -r mark; do
      [[ -z "$mark" ]] && continue
      rel="${mark#"$PROJECT_DIR/harness/vozmozhnosti/"}"
      mkdir -p "$MARKS/$(dirname "$rel")"
      cp "$mark" "$MARKS/$rel"
    done < <(find "$PROJECT_DIR/harness/vozmozhnosti" -maxdepth 2 -name '.установлено' 2>/dev/null)
  fi
  lay_tree vozmozhnosti "$HERE"/vozmozhnosti "$PROJECT_DIR/harness/vozmozhnosti"
else
  plan_absent vozmozhnosti "$PROJECT_DIR/harness/vozmozhnosti/"
fi
polozhit_fajl harness-top "$HERE"/BUILD-SKILLS.py "$PROJECT_DIR/harness/BUILD-SKILLS.py"
if [[ -f "$HERE/README-ПЛАГИН.md" ]]; then
  polozhit_fajl harness-top "$HERE/README-ПЛАГИН.md" "$PROJECT_DIR/harness/README-ПЛАГИН.md"
else
  plan_absent harness-top "$PROJECT_DIR/harness/README-ПЛАГИН.md"
fi
if [[ -d "$HERE/.claude-plugin" ]]; then
  lay_tree plugin "$HERE/.claude-plugin" "$PROJECT_DIR/harness/.claude-plugin"
else
  plan_absent plugin "$PROJECT_DIR/harness/.claude-plugin/"
fi
# CLAUDE.md проекта — ЖИВОЙ файл: в нём правила этого проекта, дополненные
# владельцем и первой задачей продукта. Раскладка кладёт его только на ЧИСТУЮ
# установку. Улика 12.09.2026: когда файл пакета стал английской заготовкой,
# безусловное `cp -f` затёрло рабочие правила проекта на пятой раскладке за
# смену — восстановлено из git. Эталон всегда доступен рядом:
# harness/CLAUDE.md.
polozhit_fajl harness-top "$HERE"/CLAUDE.md "$PROJECT_DIR/harness/CLAUDE.md"
# главы UNIFIED — нормативная база: агент читает их и пересобирает скилы.
# Ищем рядом с пакетом (STARTER-PACKAGE/../UNIFIED или внутри пакета).
UNIFIED_SRC=""
# Две раскладки: в пакете главы лежат рядом, в публичной сборке — в служебном
# подкаталоге (владелец 12.09.2026: корень не должен начинаться с исходников).
for cand in "$HERE/../UNIFIED" "$HERE/../../UNIFIED" "$HERE/UNIFIED" \
            "$HERE/../sluzhebnoe/UNIFIED" "$HERE/../../sluzhebnoe/UNIFIED"; do
  [[ -d "$cand" ]] && UNIFIED_SRC="$cand" && break
done
if [[ -n "$UNIFIED_SRC" ]]; then
  lay_tree unified "$UNIFIED_SRC" "$PROJECT_DIR/UNIFIED"
else
  echo "ВНИМАНИЕ: каталог UNIFIED рядом с пакетом не найден — главы не доставлены."
  echo "Скилы уже сгенерированы и работают; чтение глав (03-ЗАДАНИЕ §1 п.3) и"
  echo "пересборка скилов будут недоступны, пока UNIFIED не скопирован в $PROJECT_DIR/UNIFIED."
  plan_absent unified "$PROJECT_DIR/UNIFIED/"
fi

# Дверь укладки: весь план одним вызовом. Код 3 — «РАСКЛАДКА НЕ НАЧАТА», проект
# не тронут; update.py по нему не раскладывает прежнюю версию поверх.
# Коммит пакета и корень истории идут двери, только если пакет сам корень
# репозитория: копия без своего .git внутри чужого дерева дала бы чужой коммит
# и чужую историю.
PACKAGE_COMMIT="-"
PKG_ROOT="-"
pkg_top=$(git -C "$HERE/.." rev-parse --show-toplevel 2>/dev/null) || pkg_top=""
if [[ -n "$pkg_top" && "$(realpath "$pkg_top")" == "$(realpath "$HERE/..")" ]]; then
  PKG_ROOT="$(realpath "$HERE/..")"
  PACKAGE_COMMIT=$(git -C "$HERE/.." rev-parse HEAD 2>/dev/null) || PACKAGE_COMMIT="-"
  [[ -n "$PACKAGE_COMMIT" ]] || PACKAGE_COMMIT="-"
fi
python3 "$HERE/scripts/layout-door.py" --plan "$PLAN" --project "$PROJECT_DIR" \
  --package-commit "$PACKAGE_COMMIT" --package-root "$PKG_ROOT" ${LIFT_ARGS[@]+"${LIFT_ARGS[@]}"} \
  ${ROLLBACK_ARGS[@]+"${ROLLBACK_ARGS[@]}"} || exit $?

# Опись лежит в корне проекта и не должна попасть в коммиты проекта (граница §0:
# одна строка info/exclude, .gitignore не трогаем). Пишем только когда проект сам
# корень репозитория: у подкаталога чужого репозитория git отдаёт exclude РОДИТЕЛЯ.
# Относительный ответ rev-parse читаем от проекта: --path-format есть не во всех git.
exclude_line() {
  local top path
  top=$(git -C "$PROJECT_DIR" rev-parse --show-toplevel 2>/dev/null) || top=""
  if [[ -z "$top" ]]; then
    echo "info/exclude не тронут: нет репозитория"; return 0
  fi
  if [[ "$(realpath "$top")" != "$(realpath "$PROJECT_DIR")" ]]; then
    echo "info/exclude не тронут: проект не корень репозитория ($top)"; return 0
  fi
  path=$(git -C "$PROJECT_DIR" rev-parse --git-path info/exclude 2>/dev/null) || path=""
  if [[ -z "$path" ]]; then
    echo "info/exclude не тронут: git не назвал путь"; return 0
  fi
  [[ "$path" == /* ]] || path="$PROJECT_DIR/$path"
  if [[ -f "$path" ]] && grep -qxF '/.layout-manifest' "$path"; then
    echo "info/exclude: строка /.layout-manifest уже есть в $path"; return 0
  fi
  # Файлы уже легли: отказ записи (exclude только для чтения, каталог чужого
  # владельца) не должен ронять раскладку под set -e и запускать откат обновления.
  local why
  if why=$(append_exclude_line "$path" 2>&1); then
    echo "info/exclude: строка /.layout-manifest дописана в $path"
  else
    echo "info/exclude не тронут: $(head -n1 <<< "$why")"
  fi
  return 0
}
append_exclude_line() {
  local path="$1"
  mkdir -p "$(dirname "$path")" || return 1
  if [[ -s "$path" && -n "$(tail -c1 "$path")" ]]; then printf '\n' >> "$path" || return 1; fi
  printf '%s\n' '/.layout-manifest' >> "$path" || return 1
}
exclude_line
if [[ -n "$UNIFIED_SRC" ]]; then
  echo "UNIFIED доставлен: $PROJECT_DIR/UNIFIED"
fi

if [[ -n "$MARKS" ]]; then
  while IFS= read -r mark; do
    [[ -z "$mark" ]] && continue
    rel="${mark#"$MARKS/"}"
    cp "$mark" "$PROJECT_DIR/harness/vozmozhnosti/$rel"
  done < <(find "$MARKS" -name '.установлено' 2>/dev/null)
  # Имя файла кода — латиницей (задача hr.имена-файлов-латиницей): под старым
  # именем «установить.sh» chmod не находил ничего, и на чистой установке
  # ставильщик возможности приезжал без флага исполнения (12.09.2026).
  chmod +x "$PROJECT_DIR"/harness/vozmozhnosti/*/install.sh 2>/dev/null || true
  chmod +x "$PROJECT_DIR"/harness/vozmozhnosti/*/bin/* 2>/dev/null || true
  echo "возможности доставлены: $(cd "$PROJECT_DIR/harness/vozmozhnosti" && printf '%s ' */ 2>/dev/null)"
fi
# Откат возвращает владельцу его файл с его режимом (700, 644): общий +x его испортил бы.
if [[ ${#ROLLBACK_ARGS[@]} -eq 0 ]]; then
  chmod +x "$PROJECT_DIR"/scripts/*.sh "$PROJECT_DIR"/scripts/hooks/*.sh "$PROJECT_DIR"/harness/demons/*.sh
fi

# Корневой CLAUDE.md ложится только после двери: при её отказе проект не тронут.
if [[ ! -f "$PROJECT_DIR/CLAUDE.md" ]]; then
  cp -f "$HERE"/CLAUDE.md "$PROJECT_DIR/CLAUDE.md"
  echo "CLAUDE.md создан из заготовки пакета (чистая установка)"
else
  echo "CLAUDE.md проекта не тронут (живой файл); эталон — harness/CLAUDE.md"
fi

# живые данные — только если ещё нет
[[ -f "$PROJECT_DIR/STATE.md"    ]] || cp "$HERE/STATE.md"    "$PROJECT_DIR/STATE.md"
[[ -f "$PROJECT_DIR/dev-map.yaml" ]] || cp "$HERE/dev-map.yaml" "$PROJECT_DIR/dev-map.yaml"
[[ -d "$PROJECT_DIR/память"      ]] || cp -r "$HERE/память"    "$PROJECT_DIR/память"

# .gitignore — создаётся только при отсутствии, живой принадлежит агенту и не
# трогается. Причина: IDE-файлы и python-байткод не должны попадать в пакет/репо
# (улика: __pycache__ однажды уехал вместе с пакетом).
if [[ ! -f "$PROJECT_DIR/.gitignore" ]]; then
  cat > "$PROJECT_DIR/.gitignore" <<'EOF'
# Создан razlozhit-harnes.sh: IDE-файлы и байткод не должны попадать в пакет/репо.
__pycache__/
*.pyc
.env
.cursor/
.cursorrules
.vscode/
.idea/
docs/handover/*.tmp
EOF
  echo ".gitignore создан"
fi

# конфиг порогов — не затирать живой. В селфтесте шаг пропускается ВОВСЕ
# (П-11): селфтест не трогает /etc никогда — под root он клал бы боевой конфиг
# прямо из теста.
if [[ "${HARNESS_SELFTEST:-0}" == 1 ]]; then
  : # /etc/harness в селфтесте неприкосновенен
elif [[ ! -f /etc/harness/harness.conf ]]; then
  # «Нет прав» и «нет каталога» — разные беды, сообщение не должно врать (П-11):
  # каталога может не быть вовсе — создаём, если можем, иначе честно говорим,
  # чего именно не хватает и какой командой это чинится.
  if [[ ! -d /etc/harness ]] && ! mkdir -p /etc/harness 2>/dev/null; then
    echo "ВНИМАНИЕ: каталога /etc/harness нет и создать его прав не хватает — выполните: sudo mkdir -p /etc/harness && sudo cp '$HERE/config/harness.conf' /etc/harness/harness.conf"
  elif cp "$HERE/config/harness.conf" /etc/harness/harness.conf 2>/dev/null; then
    echo "конфиг порогов положен в /etc/harness/harness.conf"
  else
    echo "ВНИМАНИЕ: нет прав на запись в /etc/harness — выполните: sudo cp '$HERE/config/harness.conf' /etc/harness/harness.conf"
  fi
fi

# Ротация логов харнеса. Заведено 10.09.2026 по ревизии: без неё $LOG_DIR
# дорос до 121 МБ, а отдельные логи — до 7 МБ, и события в них тонули.
# Тот же порядок, что у конфига порогов: живой файл не затирается, нет прав —
# говорим, какой командой это чинится (П-11).
if [[ "${HARNESS_SELFTEST:-0}" == 1 ]]; then
  : # /etc в селфтесте неприкосновенен
elif [[ ! -f /etc/logrotate.d/harness ]]; then
  if cp "$HERE/config/logrotate-harness" /etc/logrotate.d/harness 2>/dev/null; then
    echo "ротация логов положена в /etc/logrotate.d/harness"
  else
    echo "ВНИМАНИЕ: нет прав на /etc/logrotate.d — выполните: sudo cp '$HERE/config/logrotate-harness' /etc/logrotate.d/harness"
  fi
fi

# pre-commit: симлинк, если репозиторий уже есть
if [[ -d "$PROJECT_DIR/.git" ]]; then
  ln -sf ../../scripts/pre-commit-gate.sh "$PROJECT_DIR/.git/hooks/pre-commit"
  echo "pre-commit подключён симлинком"
  # commit-msg: суд хакатона по трейлеру Dev-Map (сообщения на стадии pre-commit ещё нет)
  ln -sf ../../scripts/commit-msg-gate.sh "$PROJECT_DIR/.git/hooks/commit-msg"
  echo "commit-msg подключён симлинком"
else
  echo "репозитория ещё нет — после git init повторить раскладку (подключит pre-commit)"
fi

# Раскладка КОПИРУЕТ и никогда не убирает: файл, переименованный в проекте,
# возвращается из пакета вторым экземпляром. 11.09.2026 так вернулись пять
# кириллических имён кода наутро после выката задачи об их переводе — и рядом
# с `гейты/loop-check.py` встал старый `гейты/проверка-петли.py`, который
# pre-commit запускает тем же `гейты/*.py`. Раскладка обязана сказать об этом
# сама: гейт имён судит список коммита и такой возврат не видит.
vernuvshiesya=$(find "$PROJECT_DIR/harness" "$PROJECT_DIR/scripts" "$PROJECT_DIR/UNIFIED" \
  -type f \( -name '*.py' -o -name '*.sh' -o -name '*.ts' -o -name '*.sql' \) 2>/dev/null \
  | grep -P '[\x{0400}-\x{04FF}][^/]*\.(py|sh|ts|sql)$' || true)
if [[ -n "$vernuvshiesya" ]]; then
  echo
  echo "ВНИМАНИЕ: в проекте файлы кода с кириллическими именами:"
  echo "$vernuvshiesya" | sed 's/^/    /'
  echo "Это остатки в ПАКЕТЕ: раскладка их вернула. Убрать надо в пакете,"
  echo "иначе следующая раскладка положит их снова (CLAUDE.md, имена латиницей)."
fi

# Скилы СВЯЗЫВАЮТСЯ С ОБОЛОЧКОЙ: она читает только <проект>/.claude/skills, а
# раскладка кладёт их в harness/skills. Связь держали симлинки, поставленные
# руками в первый день, — и на чистой установке агент оставался БЕЗ СКИЛОВ,
# хотя каталог harness/skills выглядел полным (улика 12.09.2026). Связывает сам
# генератор; здесь он же и пересобирает скилы под доставленные возможности.
if ! python3 "$PROJECT_DIR/harness/BUILD-SKILLS.py"; then
  echo
  echo "ОТКАЗ: скилы не пересобраны и НЕ СВЯЗАНЫ с оболочкой."
  echo "Агент их не увидит: оболочка читает $PROJECT_DIR/.claude/skills."
  echo "Починка: python3 $PROJECT_DIR/harness/BUILD-SKILLS.py — причина выше."
  exit 1
fi

# Служба, чей код раскладка только что обновила, работает на старом: гейт
# внешних вызовов краснеет, и с ним — каждый коммит. Кого перезапускать,
# решает тот же гейт (второго счёта свежести не заводим); право на рестарт —
# строка sudoers из USTANOVIT.sh. Нет права — называем команду вслух.
while read -r unit; do
  if sudo -n systemctl restart "$unit" 2>/dev/null; then
    echo "перезапущена служба на старом коде: $unit"
  else
    echo "ВНИМАНИЕ: $unit работает на старом коде, прав на рестарт нет — выполните: sudo systemctl restart $unit"
  fi
done < <(python3 "$PROJECT_DIR/scripts/check-external-callers.py" 2>/dev/null \
  | sed -n 's/.*Перезапустить: sudo systemctl restart \([^ ]*\).*/\1/p' | sort -u)

# Меню бота живёт на стороне Telegram и ставится по данным (bot-commands.yaml).
# Его не ставил никто: 13.10.2026 владелец первой установки написал «я в чате с ботом не
# вижу меню». Раскладка сверяет и ставит — и при установке, и после обновления,
# привёзшего новую кнопку. Нет канала — сказано вслух, раскладка не падает.
if [[ "${HARNESS_SELFTEST:-0}" != 1 ]] && ! python3 "$PROJECT_DIR/scripts/bot-commands.py" --проверить >/dev/null 2>&1; then
  python3 "$PROJECT_DIR/scripts/bot-commands.py" --поставить \
    || echo "ВНИМАНИЕ: меню бота не поставлено — python3 $PROJECT_DIR/scripts/bot-commands.py --поставить"
fi

echo "раскладка завершена: $PROJECT_DIR"
