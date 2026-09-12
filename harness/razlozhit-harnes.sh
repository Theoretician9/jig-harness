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
AGENT_USER="$(id -un)"
LOG_DIR="$T/log"
HEARTBEAT_DIR="$T/hb"
EOF
  # bash "$0", не "$0": после scp с Windows exec-бита может не быть,
  # и прямой перезапуск падал бы «Permission denied».
  # HARNESS_SELFTEST=1 — селфтест НИКОГДА не трогает /etc (П-11): без флага
  # прогон под root на машине без /etc/harness/harness.conf клал туда боевой
  # конфиг прямо из теста. Доказательство — ls /etc/harness до/после селфтеста.
  HARNESS_SELFTEST=1 HARNESS_INSTALL_CONF="$T/install.conf" bash "$0" || { echo "SELFTEST: раскладка упала"; exit 1; }
  ok_flag=1
  for f in "$T/proj/scripts/hooks/guard_bash.py" "$T/proj/scripts/deploy.sh" \
           "$T/proj/harness/demons/heartbeat-watch.sh" "$T/proj/CLAUDE.md" \
           "$T/proj/.claude/settings.json" "$T/proj/harness/skills/приём-задачи/SKILL.md" \
           "$T/proj/harness/panel/server.py" \
           "$T/proj/harness/ОПИСЬ.md"; do
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
  HARNESS_SELFTEST=1 HARNESS_INSTALL_CONF="$T/install.conf" bash "$0" >/dev/null || true
  INODE_AFTER=$(stat -c %i "$TARGET_FILE" 2>/dev/null || echo нет)
  if [[ "$INODE_BEFORE" != "$INODE_AFTER" ]]; then
    echo "  ок    изменившийся файл перезаписан с новым номером ($INODE_BEFORE → $INODE_AFTER): живой процесс дочитает прежний"
  else
    echo "  ПЛОХО перезапись идёт в тот же номер файла — работающий скрипт рвётся на середине"; ok_flag=0
  fi

  # БОЛЬНОЙ СЛУЧАЙ 12.09.2026: НЕизменившийся файл раскладка трогать не должна.
  # Прежде она копировала всё подряд, mtime обновлялся у каждого файла, и гейт
  # внешних вызовов после каждой раскладки объявлял службы «работают на старом
  # коде» — трижды за смену, с ненужным перезапуском панели и диспетчера.
  MTIME_BEFORE=$(stat -c %Y "$TARGET_FILE" 2>/dev/null || echo нет)
  sleep 1
  HARNESS_SELFTEST=1 HARNESS_INSTALL_CONF="$T/install.conf" bash "$0" >/dev/null || true
  MTIME_AFTER=$(stat -c %Y "$TARGET_FILE" 2>/dev/null || echo нет)
  if [[ "$MTIME_BEFORE" == "$MTIME_AFTER" ]]; then
    echo "  ок    БОЛЬНОЙ СЛУЧАЙ: неизменившийся файл не тронут — гейт свежести не будят зря"
  else
    echo "  ПЛОХО раскладка обновила время у неизменившегося файла ($MTIME_BEFORE → $MTIME_AFTER)"; ok_flag=0
  fi

  # генератор скилов на сервере работоспособен: источники PACKAGE: доставлены,
  # главы UNIFIED на месте. Проверяется прогоном, а не наличием файлов.
  if python3 "$T/proj/harness/SBORKA-SKILOV.py" >/dev/null 2>"$T/скилы.err"; then :; else
    echo "SELFTEST: пересборка скилов на разложенном сервере упала: $(tr '\n' ' ' < "$T/скилы.err" | cut -c1-200)"; ok_flag=0
  fi
  # возможности доставлены целиком и исполняемы
  for f in "$T/proj/harness/vozmozhnosti/mobilnaya-razrabotka/ustanovit.sh" \
           "$T/proj/harness/vozmozhnosti/mobilnaya-razrabotka/МАНИФЕСТ.conf" \
           "$T/proj/harness/vozmozhnosti/mobilnaya-razrabotka/bin/mobile-loop" \
           "$T/proj/scripts/vozmozhnost.sh"; do
    [[ -f "$f" ]] || { echo "SELFTEST: нет $f"; ok_flag=0; }
  done
  # метка установки — живые данные: повторная доставка кода НЕ должна её снимать,
  # иначе обновление харнеса молча отключало бы установленный набор
  : > "$T/proj/harness/vozmozhnosti/mobilnaya-razrabotka/.установлено"
  HARNESS_SELFTEST=1 HARNESS_INSTALL_CONF="$T/install.conf" bash "$0" >/dev/null
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
  HARNESS_SELFTEST=1 HARNESS_INSTALL_CONF="$T/install.conf" bash "$0" >/dev/null
  grep -q "живое состояние" "$T/proj/STATE.md" || { echo "SELFTEST: затёрт живой STATE.md"; ok_flag=0; }
  # БОЛЬНОЙ СЛУЧАЙ 12.09.2026: cp -f затёр РАБОЧИЕ правила проекта заготовкой
  # пакета. Пока файлы совпадали, это было незаметно; когда заготовка стала
  # английской, пятая раскладка за смену снесла правила (вернули из git).
  echo "живые правила проекта" > "$T/proj/CLAUDE.md"
  HARNESS_SELFTEST=1 HARNESS_INSTALL_CONF="$T/install.conf" bash "$0" >/dev/null
  grep -q "живые правила проекта" "$T/proj/CLAUDE.md" \
    || { echo "SELFTEST: затёрт живой CLAUDE.md проекта"; ok_flag=0; }
  [[ -f "$T/proj/harness/CLAUDE.md" ]] \
    || { echo "SELFTEST: эталон harness/CLAUDE.md не доставлен"; ok_flag=0; }
  grep -q "живой gitignore" "$T/proj/.gitignore" || { echo "SELFTEST: затёрт живой .gitignore"; ok_flag=0; }

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
    deny=$(HARNESS_SELFTEST=1 HARNESS_INSTALL_CONF="$T/закрытый.conf" bash "$0" 2>&1) || deny_rc=$?
    chmod 700 "$T/закрытый/proj"
    [[ $deny_rc -ne 0 ]] || { echo "SELFTEST: раскладка в закрытый каталог НЕ отказала (rc=0)"; ok_flag=0; }
    grep -q "РАСКЛАДКА НЕ НАЧАТА" <<<"$deny" || { echo "SELFTEST: отказ не назвал себя («РАСКЛАДКА НЕ НАЧАТА»): ${deny:0:200}"; ok_flag=0; }
    grep -q "sudo chown -R" <<<"$deny" || { echo "SELFTEST: в отказе нет готовой команды chown"; ok_flag=0; }
    grep -q "$T/закрытый/proj" <<<"$deny" || { echo "SELFTEST: отказ не назвал каталог"; ok_flag=0; }
    # Главное: НИ ОДНОГО файла. Пустой каталог — весь смысл проверки.
    copied=$(find "$T/закрытый/proj" -mindepth 1 | wc -l)
    [[ "$copied" -eq 0 ]] || { echo "SELFTEST: при отказе прав скопировано $copied объектов — дерево полуобновлено"; ok_flag=0; }
    echo "  ок    БОЛЬНОЙ СЛУЧАЙ: закрытый на запись каталог → отказ до первого cp, скопировано 0 объектов"
  fi
  chmod -R u+w "$T" 2>/dev/null || true
  rm -rf "$T"
  [[ $ok_flag -eq 1 ]] && echo "SELFTEST: зелёный" || exit 1
  exit 0
fi

[[ -f "$CONF" ]] || { echo "нет конфига $CONF — сначала шаг 0 из 01-SPEC-УСТАНОВКА"; exit 1; }
# Окружение старше конфига — тем же загрузчиком, что и весь харнес. Здесь он
# берётся ИЗ ПАКЕТА: в проекте его ещё нет, раскладка сама его и кладёт.
INSTALL_CONF="$CONF"
# shellcheck disable=SC1091
source "$HERE/scripts/lib/konf.sh"
konf_zagruzit
[[ -n "${PROJECT_DIR:-}" ]] || { echo "PROJECT_DIR пуст в $CONF"; exit 1; }

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
  exit 1
fi
# Запись через временный файл и mv, а не cp поверх: cp -f пишет в ТОТ ЖЕ
# inode, а этот файл в момент раскладки исполняет живой процесс (диспетчер
# канала читает свой скрипт на ходу) — он ломается на середине. mv подменяет
# ИМЯ: работающий процесс дочитывает прежний inode и доживает до перезапуска
# (ревью спеки обновления 11.09.2026, находка C4).
polozhit_fajl() {  # $1=источник  $2=цель
  local src="$1" dst="$2" tmp
  # Одинаковый файл НЕ трогаем. Раскладка копировала всё подряд, и время
  # изменения обновлялось у каждого файла — а гейт внешних вызовов судит
  # свежесть кода службы по mtime и после КАЖДОЙ раскладки объявлял панель и
  # диспетчер «работают на старом коде». За смену 12.09.2026 это случилось
  # трижды: ворота краснели, службы перезапускались впустую. Сравнение
  # содержимого убирает причину, а не симптом.
  if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
    return 0
  fi
  tmp="$(dirname "$dst")/.$(basename "$dst").new.$$"
  cp -f "$src" "$tmp" || return 1
  chmod --reference="$src" "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$dst"
}

polozhit_v() {  # $1=каталог назначения, далее — файлы
  local dst="$1"; shift
  local f
  for f in "$@"; do
    [[ -f "$f" ]] || continue
    polozhit_fajl "$f" "$dst/$(basename "$f")" || return 1
  done
}

# Байткод в источнике ломает cp (каталог среди файлов) — та самая улика:
# любой прогон py-тестов рядом с пакетом создаёт __pycache__. Чистим до копирования.
find "$HERE" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
# код — всегда обновляется
polozhit_v "$PROJECT_DIR/scripts/hooks" "$HERE"/hooks/*.py "$HERE"/hooks/*.sh
polozhit_fajl "$HERE/hooks/project-settings.json" "$PROJECT_DIR/.claude/settings.json"
# «-r» обязателен: в scripts/ лежит КАТАЛОГ lib/ с общим модулем чтения
# конфига, от которого зависят четыре гейта. Без него cp печатал «omitting
# directory», раскладка шла дальше, и на новом сервере эти гейты падали бы
# импортом — а проверить было нечем: живой прогон раскладки из чистого клона
# делался впервые 10.09.2026 и нашёл это первым же запуском.
# Файлы верхнего уровня — через mv (среди них живой tg-dispatcher.sh),
# подкаталоги (lib/) — обычным cp -r: их никто не исполняет.
polozhit_v "$PROJECT_DIR/scripts" "$HERE"/scripts/*
for subdir in "$HERE"/scripts/*/; do
  [[ -d "$subdir" ]] && cp -rf "$subdir" "$PROJECT_DIR/scripts/"
done
polozhit_v "$PROJECT_DIR/harness/demons" "$HERE"/demons/*
# Таблицы-данные механизмов: инварианты (покрытие) и удержание (политика путей
# записи). Раскладчик их не переносил, и гейт сверки с пакетом их не сторожил —
# правка в проекте пережила бы переустановку только случайно (10.09.2026).
# harness.conf и logrotate-harness остаются в стороне: их место — /etc/harness.
mkdir -p "$PROJECT_DIR/harness/config"
# Пульт владельца (пайплайн.yaml) — ЖИВЫЕ данные сервера, а не пакета: 11.09
# владелец включил шаги для мелких и обычных задач, и раскладка 12.09 вернула
# ему пакетные умолчания молча. Файл кладётся ТОЛЬКО если его ещё нет — так же,
# как harness.conf в /etc. Гейт сверки с пакетом его тоже не сторожит.
LIVE_SETTINGS="пайплайн.yaml очередь.yaml модели.yaml ревизия.yaml"   # имя латиницей: bash не берёт кириллические имена
for f in "$HERE"/config/*.yaml "$HERE"/config/*.txt; do
  [[ -f "$f" ]] || continue
  name="$(basename "$f")"
  if [[ " $LIVE_SETTINGS " == *" $name "* ]] && [[ -f "$PROJECT_DIR/harness/config/$name" ]]; then
    echo "  $name — настройки владельца, не трогаю"
    continue
  fi
  polozhit_fajl "$f" "$PROJECT_DIR/harness/config/$name" || exit 1
done
# Веб-панель управления: 11 файлов службы, которых раскладка не знала вовсе —
# правка панели не доезжала до другой машины (ревью спеки обновления, C5).
if [[ -d "$HERE/panel" ]]; then
  mkdir -p "$PROJECT_DIR/harness/panel"
  polozhit_v "$PROJECT_DIR/harness/panel" "$HERE"/panel/*
fi
# harness/ на сервере зеркалит пакет: скилы, шаблоны, генератор, опись, плагин —
# пути «harness/skills» и т.д. из harness.conf и CLAUDE.md остаются верными
# skills-src/ доставляется ОБЯЗАТЕЛЬНО: генератор скилов берёт из него источники
# типа PACKAGE: (таблица расширений). Без него любая пересборка скилов на
# сервере падает «нет файла пакета skills-src/…» — а пересборка нужна и при
# дополнении таблицы расширений (01-SPEC §8), и при установке возможности.
# Улика: сквозной прогон установки возможности, 11.08 — установка набора
# дошла до конца и честно сказала «скилы не собраны».
cp -rf "$HERE"/skills "$HERE"/skills-src "$HERE"/шаблоны-задач "$PROJECT_DIR/harness/"
# Возможности: КОД набора обновляется всегда, а метки `.установлено` — живые
# данные и принадлежат серверу. rsync здесь не годится (может не стоять), поэтому
# метки снимаются до копирования и возвращаются после: иначе доставка кода
# «отключала» уже установленный набор, и скилы возможности исчезали молча.
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
  cp -rf "$HERE"/vozmozhnosti "$PROJECT_DIR/harness/"
  while IFS= read -r mark; do
    [[ -z "$mark" ]] && continue
    rel="${mark#"$MARKS/"}"
    cp "$mark" "$PROJECT_DIR/harness/vozmozhnosti/$rel"
  done < <(find "$MARKS" -name '.установлено' 2>/dev/null)
  # Имя файла кода — латиницей (задача hr.имена-файлов-латиницей): под старым
  # именем «установить.sh» chmod не находил ничего, и на чистой установке
  # ставильщик возможности приезжал без флага исполнения (12.09.2026).
  chmod +x "$PROJECT_DIR"/harness/vozmozhnosti/*/ustanovit.sh 2>/dev/null || true
  chmod +x "$PROJECT_DIR"/harness/vozmozhnosti/*/bin/* 2>/dev/null || true
  rm -rf "$MARKS"
  echo "возможности доставлены: $(cd "$PROJECT_DIR/harness/vozmozhnosti" && printf '%s ' */ 2>/dev/null)"
fi
cp -f "$HERE"/SBORKA-SKILOV.py "$HERE"/ОПИСЬ.md "$PROJECT_DIR/harness/"
[[ -f "$HERE/README-ПЛАГИН.md" ]] && cp -f "$HERE/README-ПЛАГИН.md" "$PROJECT_DIR/harness/"
[[ -d "$HERE/.claude-plugin" ]] && cp -rf "$HERE/.claude-plugin" "$PROJECT_DIR/harness/"
# CLAUDE.md проекта — ЖИВОЙ файл: в нём правила этого проекта, дополненные
# владельцем и первой задачей продукта. Раскладка кладёт его только на ЧИСТУЮ
# установку. Улика 12.09.2026: когда файл пакета стал английской заготовкой,
# безусловное `cp -f` затёрло рабочие правила проекта на пятой раскладке за
# смену — восстановлено из git. Эталон всегда доступен рядом:
# harness/CLAUDE.md.
cp -f "$HERE"/CLAUDE.md "$PROJECT_DIR/harness/CLAUDE.md"
if [[ ! -f "$PROJECT_DIR/CLAUDE.md" ]]; then
  cp -f "$HERE"/CLAUDE.md "$PROJECT_DIR/CLAUDE.md"
  echo "CLAUDE.md создан из заготовки пакета (чистая установка)"
else
  echo "CLAUDE.md проекта не тронут (живой файл); эталон — harness/CLAUDE.md"
fi
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
  mkdir -p "$PROJECT_DIR/UNIFIED"
  cp -rf "$UNIFIED_SRC"/. "$PROJECT_DIR/UNIFIED/"
  echo "UNIFIED доставлен: $PROJECT_DIR/UNIFIED"
else
  echo "ВНИМАНИЕ: каталог UNIFIED рядом с пакетом не найден — главы не доставлены."
  echo "Скилы уже сгенерированы и работают; чтение глав (03-ЗАДАНИЕ §1 п.3) и"
  echo "пересборка скилов будут недоступны, пока UNIFIED не скопирован в $PROJECT_DIR/UNIFIED."
fi
chmod +x "$PROJECT_DIR"/scripts/*.sh "$PROJECT_DIR"/scripts/hooks/*.sh "$PROJECT_DIR"/harness/demons/*.sh

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
  ln -sf ../../scripts/pre-commit-hook.sh "$PROJECT_DIR/.git/hooks/pre-commit"
  echo "pre-commit подключён симлинком"
else
  echo "репозитория ещё нет — после git init повторить раскладку (подключит pre-commit)"
fi

# Раскладка КОПИРУЕТ и никогда не убирает: файл, переименованный в проекте,
# возвращается из пакета вторым экземпляром. 11.09.2026 так вернулись пять
# кириллических имён кода наутро после выката задачи об их переводе — и рядом
# с `гейты/proverka-petli.py` встал старый `гейты/проверка-петли.py`, который
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
if ! python3 "$PROJECT_DIR/harness/SBORKA-SKILOV.py"; then
  echo
  echo "ОТКАЗ: скилы не пересобраны и НЕ СВЯЗАНЫ с оболочкой."
  echo "Агент их не увидит: оболочка читает $PROJECT_DIR/.claude/skills."
  echo "Починка: python3 $PROJECT_DIR/harness/SBORKA-SKILOV.py — причина выше."
  exit 1
fi

echo "раскладка завершена: $PROJECT_DIR"
