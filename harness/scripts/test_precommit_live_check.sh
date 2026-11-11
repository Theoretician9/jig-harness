#!/usr/bin/env bash
# Проба: судья живой проверки (`build-live-check-list.py --проверить`) стоит в pre-commit.
#
# Откуда взята. Судья следа стоял только в воротах (gates.sh), и запись живой проверки
# без доказательства доезжала до коммита (спека живой проверки §5, план часть 3).
# Решение Р2 оркестратора 11.11.2026: в pre-commit судья судит только ДАННЫЕ живой
# проверки и читает их из ИНДЕКСА (правило К-4 хука), документ не сверяет — свежесть
# документа судят ворота. Судья идёт около 10 с, поэтому зовётся только когда данные
# живой проверки есть в индексе.
#
# Как устроена. Каждый случай — свой временный git-репозиторий (TMPDIR harness-run-)
# со своей копией хука, подключённого в .git/hooks; соседние гейты хука заменены
# заглушками «пройдено» (их список берётся РАЗБОРОМ исходника хука — пустой список =
# провал), настоящий судья и его библиотека скопированы в репозиторий и закоммичены.
# Коммит идёт настоящим `git commit`: судится то, что увидит человек.
# Двери: PRECOMMIT_UNDER_TEST — хук, который судит проба (дверь мутаций);
#        LIVE_CHECK_JUDGE_SRC — исходник настоящего судьи для двора;
#        PRECOMMIT_LIVE_CHECK_JUDGE — дверь ХУКА: путь судьи вместо
#        scripts/build-live-check-list.py снимка индекса (здесь — подставной судья,
#        который считает вызовы и отдаёт заданный код).
set -u
here=$(cd "$(dirname "$0")" && pwd)
src=${PRECOMMIT_UNDER_TEST:-$here/pre-commit-gate.sh}
judge_src=${LIVE_CHECK_JUDGE_SRC:-$here/build-live-check-list.py}
TOP=$(mktemp -d -t harness-run-XXXXXX); trap 'rm -rf "$TOP"' EXIT
export TMPDIR="$TOP"
fail=0; n_ok=0; n_bad=0
ok()  { echo "ok:   $1"; n_ok=$((n_ok + 1)); }
bad() { echo "FAIL: $1"; fail=1; n_bad=$((n_bad + 1)); }

# Двор не наследует чужой git и чужие пути судьи: иначе проба судила бы живой проект.
unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_OBJECT_DIRECTORY GIT_PREFIX \
      HARNESS_MECHANISMS HARNESS_LIVE_CHECK_STATE HARNESS_LIVE_CHECK_CLASSES \
      HARNESS_LIVE_CHECK_DOC HARNESS_DEVMAP HARNESS_PROBE_MUTATIONS HARNESS_INVARIANTS \
      HARNESS_LIVE_CHECK_UNITS PRECOMMIT_LIVE_CHECK_JUDGE PRODUCT_DIR PRODUCT_LINT_CMD
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null

# Соседи хука, которых он зовёт по пути, — разбором исходника.
neighbours=$(grep -o '"\$\(SNAP_DIR/scripts\|SCRIPT_DIR\)/[^"]*"' "$src" \
  | sed -e 's/^"\$SNAP_DIR\/scripts\///' -e 's/^"\$SCRIPT_DIR\///' -e 's/"$//' \
  | grep -v '^build-live-check-list\.py$' | sort -u)
[ -n "$neighbours" ] || { echo "FAIL: соседи хука не найдены разбором $src"; exit 1; }

STATE_GOOD='проверено:
  scripts/a.sh:
    исход: ждёт
    ждёт: слова владельца о живом прогоне
'
STATE_BAD='проверено:
  scripts/a.sh:
    исход: ок
    живьём: позвал scripts/a.sh на дворе
    увидели: вывод механизма про двор и его итог
    след: logs/следа-нет-такого.log
'

make_repo() {  # имя → путь репозитория с закоммиченной основой (без хука)
  local r="$TOP/$1" n
  mkdir -p "$r/scripts/lib" "$r/harness/config" "$r/docs" "$r/log" || return 1
  git -C "$r" init -q -b master
  git -C "$r" config user.name проба; git -C "$r" config user.email probe@localhost
  cp "$src" "$r/scripts/pre-commit-gate.sh"; chmod +x "$r/scripts/pre-commit-gate.sh"   # копия мутации бывает без бита
  cp "$here/lib/config.sh" "$r/scripts/lib/config.sh"
  cp "$here/lib/probe_coverage.py" "$r/scripts/lib/probe_coverage.py"
  cp "$judge_src" "$r/scripts/build-live-check-list.py"
  while IFS= read -r n; do
    mkdir -p "$(dirname "$r/scripts/$n")"
    case "$n" in
      *.sh) printf '#!/usr/bin/env bash\ncat >/dev/null 2>&1 || true\nexit 0\n' > "$r/scripts/$n" ;;
      *)    printf '#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n' > "$r/scripts/$n" ;;
    esac
    chmod +x "$r/scripts/$n"
  done <<< "$neighbours"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$r/scripts/a.sh"
  cat > "$r/harness/config/mechanisms.yaml" <<'EOF'
механизмы:
  - файл: scripts/a.sh
    имя: проба двора
    вид: скрипт
    проверяется: bash scripts/a.sh
EOF
  cat > "$r/harness/config/live-check-classes.yaml" <<'EOF'
классы:
  scripts/a.sh: {класс: E}
причины_непроверяемого:
  - нужен живой владелец
EOF
  printf '%s' "$STATE_GOOD" > "$r/harness/config/live-check-state.yaml"
  : > "$r/harness.conf"; : > "$r/install.conf"
  # Документ — сборкой самого судьи: основа двора сходится с данными.
  ( cd "$r" && python3 scripts/build-live-check-list.py >/dev/null ) || return 1
  echo 'log/' > "$r/.gitignore"
  git -C "$r" add -A && git -C "$r" commit -q --no-verify -m основа || return 1
  ln -sf "$r/scripts/pre-commit-gate.sh" "$r/.git/hooks/pre-commit"
  printf '%s\n' "$r"
}

commit() {  # репозиторий → OUT, RC; HEAD до и после — в H0, H1
  local r="$1"
  H0=$(git -C "$r" rev-parse HEAD)
  RC=0
  OUT=$(cd "$r" && PROJECT_DIR="$r" LOG_DIR="$r/log" HARNESS_LOG_DIR="$r/log" \
        HARNESS_CONF="$r/harness.conf" HARNESS_INSTALL_CONF="$r/install.conf" \
        git -c core.hooksPath="$r/.git/hooks" commit -q -m проба 2>&1 < /dev/null) || RC=$?
  H1=$(git -C "$r" rev-parse HEAD)
  # Хук не позван — любой «прошёл» ниже был бы ложным зелёным.
  grep -q '^\[pre-commit\] staged:' <<<"$OUT" || bad "хук не запущен в $(basename "$r"): $OUT"
}

fake_judge() {  # репозиторий, код → путь подставного судьи; вызовы — строки в $r/calls
  local r="$1" code="$2" j="$TOP/judge-$(basename "$1").py"
  cat > "$j" <<EOF
#!/usr/bin/env python3
import sys
with open("$r/calls", "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
print("[подставной судья] вызван, код $code")
sys.exit($code)
EOF
  chmod +x "$j"; : > "$r/calls"
  printf '%s\n' "$j"
}
calls() { grep -c . "$1/calls" 2>/dev/null || true; }

# ── 1. БОЛЬНОЙ: в индексе запись без следа → коммит отказан, причина названа ──
r=$(make_repo bad-index) || { echo "FAIL: двор bad-index не собран"; exit 1; }
printf '%s' "$STATE_BAD" > "$r/harness/config/live-check-state.yaml"
git -C "$r" add harness/config/live-check-state.yaml
# Условие двора: сам судья на этих данных краснеет — иначе отказ хука был бы не о них.
# Документ для этой сверки — свежий и вне двора: устаревший оборвал бы судью раньше записей.
( cd "$r" && export HARNESS_LIVE_CHECK_DOC="$TOP/doc-bad-index.md" \
  && python3 scripts/build-live-check-list.py >/dev/null \
  && python3 scripts/build-live-check-list.py --проверить 2>&1 ) | grep -q 'следа нет на диске' \
  && ok "1: судья на битых данных краснеет сам" || bad "1: двор не создал условие — судья битого не видит"
commit "$r"
[ "$RC" != 0 ] && [ "$H0" = "$H1" ] && ok "1: битые данные в индексе — коммит отказан (код $RC)" \
  || bad "1: битые данные в индексе, а коммит прошёл (код $RC)"
grep -q 'записей без доказательства' <<<"$OUT" && ok "1: в выводе «записей без доказательства»" \
  || bad "1: в выводе нет «записей без доказательства»"
grep -q 'следа нет на диске' <<<"$OUT" && ok "1: названа причина записи — следа нет на диске" \
  || bad "1: причина записи не названа"

# ── 2. К-4: битое только на диске, в индексе верное → коммит проходит ────────
r=$(make_repo bad-worktree) || { echo "FAIL: двор bad-worktree не собран"; exit 1; }
printf '%s' "$STATE_GOOD" | sed 's/слова владельца/решения владельца/' \
  > "$r/harness/config/live-check-state.yaml"
git -C "$r" add harness/config/live-check-state.yaml
printf '%s' "$STATE_BAD" > "$r/harness/config/live-check-state.yaml"   # только рабочее дерево
commit "$r"
[ "$RC" = 0 ] && [ "$H0" != "$H1" ] && ok "2: верное в индексе при битом диске — коммит прошёл" \
  || bad "2: верное в индексе, а коммит отказан (код $RC) — судья читает диск, не индекс"
grep -q '\[живая проверка\]' <<<"$OUT" && ok "2: судья позван и судил индекс" \
  || bad "2: судья живой проверки не позван на staged данных"

# ── 3. Данных живой проверки в индексе нет → судья не зовётся ───────────────
r=$(make_repo not-called) || { echo "FAIL: двор not-called не собран"; exit 1; }
export PRECOMMIT_LIVE_CHECK_JUDGE; PRECOMMIT_LIVE_CHECK_JUDGE=$(fake_judge "$r" 0)
echo заметка > "$r/notes.txt"; git -C "$r" add notes.txt
commit "$r"
[ "$RC" = 0 ] && ok "3: коммит без данных живой проверки прошёл" || bad "3: коммит без данных отказан (код $RC)"
[ "$(calls "$r")" = 0 ] && ok "3: без данных судья не зван (вызовов 0)" \
  || bad "3: без данных судья зван $(calls "$r") раз — 10 с на каждый коммит"
# Здоровый близнец: без него «вызовов 0» зелено и у хука, где судьи нет вовсе.
printf '%s' "$STATE_GOOD" | sed 's/слова владельца/решения владельца/' \
  > "$r/harness/config/live-check-state.yaml"
git -C "$r" add harness/config/live-check-state.yaml
commit "$r"
[ "$(calls "$r")" = 1 ] && ok "3: с данными в индексе судья зван ровно 1 раз" \
  || bad "3: с данными в индексе судья зван $(calls "$r") раз, ждали 1"
unset PRECOMMIT_LIVE_CHECK_JUDGE

# ── 4. Документ устарел → pre-commit НЕ отказывает (Р2: документ судят ворота) ─
r=$(make_repo stale-doc) || { echo "FAIL: двор stale-doc не собран"; exit 1; }
printf '%s' "$STATE_GOOD" | sed 's/слова владельца/решения владельца/' \
  > "$r/harness/config/live-check-state.yaml"
echo 'правлено руками, сборкой не пересобрано' >> "$r/docs/живая-проверка-механизмов.md"
git -C "$r" add harness/config/live-check-state.yaml docs/живая-проверка-механизмов.md
( cd "$r" && python3 scripts/build-live-check-list.py --проверить >/dev/null 2>&1 ) \
  && bad "4: двор не создал условие — документ сходится" || ok "4: документ двора устарел (судья ворот краснеет)"
commit "$r"
[ "$RC" = 0 ] && [ "$H0" != "$H1" ] && ok "4: устаревший документ — коммит прошёл" \
  || bad "4: устаревший документ остановил коммит (код $RC)"
grep -q '\[живая проверка\]' <<<"$OUT" && ok "4: судья позван и документ не сверял" \
  || bad "4: судья живой проверки не позван на staged данных"

# ── 5. Судья упал (код не 0 и не 1) → отказ, а не тихий пропуск ─────────────
r=$(make_repo judge-crash) || { echo "FAIL: двор judge-crash не собран"; exit 1; }
export PRECOMMIT_LIVE_CHECK_JUDGE; PRECOMMIT_LIVE_CHECK_JUDGE=$(fake_judge "$r" 2)
printf '%s' "$STATE_GOOD" | sed 's/слова владельца/решения владельца/' \
  > "$r/harness/config/live-check-state.yaml"
git -C "$r" add harness/config/live-check-state.yaml
commit "$r"
[ "$(calls "$r")" = 1 ] && ok "5: упавший судья был зван" || bad "5: судья не зван (вызовов $(calls "$r"))"
[ "$RC" != 0 ] && [ "$H0" = "$H1" ] && ok "5: упавший судья — коммит отказан (код $RC)" \
  || bad "5: судья упал кодом 2, а коммит прошёл — тихий пропуск"
unset PRECOMMIT_LIVE_CHECK_JUDGE

# ── Двор класса D: две пробы, связь с мутацией — у одной в probe-mutations.yaml,
#    у другой в invariants.yaml (команда «доказательство»). Основа двора судьёй зелёная.
make_d_repo() {  # имя → путь репозитория, где обе записи D доказаны и связаны с мутацией
  local r p
  r=$(make_repo "$1") || return 1
  mkdir -p "$r/logs"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$r/scripts/test_d.sh"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$r/scripts/test_i.sh"
  cat >> "$r/harness/config/mechanisms.yaml" <<'YML'
  - файл: scripts/test_d.sh
    имя: проба двора D по записи мутаций
    вид: проба
    проверяется: bash scripts/test_d.sh
  - файл: scripts/test_i.sh
    имя: проба двора D по инварианту
    вид: проба
    проверяется: bash scripts/test_i.sh
YML
  printf '  scripts/test_d.sh: {класс: D}\n  scripts/test_i.sh: {класс: D}\n' > "$TOP/d-classes"
  sed -i "/^  scripts\/a.sh: {класс: E}\$/r $TOP/d-classes" "$r/harness/config/live-check-classes.yaml"
  for p in d i; do
    printf 'механизм: scripts/test_%s.sh\n--- вывод механизма ---\nпроба двора класса D (%s) — зелёных 3, красных 0\n' \
      "$p" "$p" > "$r/logs/$p.log"
    printf '  scripts/test_%s.sh:\n    исход: ок\n    живьём: прогнал пробу test_%s на дворе\n    увидели: проба двора класса D (%s) — зелёных 3, красных 0\n    след: logs/%s.log\n' \
      "$p" "$p" "$p" "$p" >> "$r/harness/config/live-check-state.yaml"
  done
  cat > "$r/harness/config/probe-mutations.yaml" <<'YML'
пробы:
  scripts/test_d.sh:
    мутация: снять exit 0 в механизме двора
YML
  cat > "$r/harness/config/invariants.yaml" <<'YML'
инварианты:
  - имя: инвариант двора
    файл: scripts/a.sh
    мутация: снять exit 0 в scripts/a.sh
    доказательство: bash scripts/test_i.sh
YML
  ( cd "$r" && python3 scripts/build-live-check-list.py >/dev/null ) || return 1
  git -C "$r" add -A && git -C "$r" commit -q --no-verify -m 'основа D' || return 1
  printf '%s\n' "$r"
}

judge_on_tree() {  # репозиторий, ожидаемый код, имя случая — условие двора судьёй на дереве
  local rc=0
  ( cd "$1" && python3 scripts/build-live-check-list.py --данные >/dev/null 2>&1 ) || rc=$?
  [ "$rc" = "$2" ] && ok "$3: условие двора — судья на дереве даёт код $rc" \
    || bad "$3: двор не создал условие — судья на дереве даёт код $rc, ждали $2"
}

# ── 6. В индексе только probe-mutations.yaml, связь записи D с мутацией снята ──
r=$(make_d_repo d-mutations) || { echo "FAIL: двор d-mutations не собран"; exit 1; }
judge_on_tree "$r" 0 "6 (основа)"
sed -i 's/^  scripts\/test_d\.sh:$/  scripts\/test_d_old.sh:/' "$r/harness/config/probe-mutations.yaml"
git -C "$r" add harness/config/probe-mutations.yaml
judge_on_tree "$r" 1 "6"
commit "$r"
[ "$RC" != 0 ] && [ "$H0" = "$H1" ] && ok "6: связь D снята в probe-mutations.yaml — коммит отказан (код $RC)" \
  || bad "6: связь D снята в probe-mutations.yaml, а коммит прошёл (код $RC) — судья не зван"
grep -q 'scripts/test_d.sh: мутация пробы не названа' <<<"$OUT" \
  && ok "6: названа запись D без мутации" || bad "6: в выводе нет «scripts/test_d.sh: мутация пробы не названа»"

# ── 7. То же для invariants.yaml: «доказательство» перестало называть пробу ───
r=$(make_d_repo d-invariants) || { echo "FAIL: двор d-invariants не собран"; exit 1; }
sed -i 's/^    доказательство: bash scripts\/test_i\.sh$/    доказательство: bash scripts\/test_other.sh/' \
  "$r/harness/config/invariants.yaml"
git -C "$r" add harness/config/invariants.yaml
judge_on_tree "$r" 1 "7"
commit "$r"
[ "$RC" != 0 ] && [ "$H0" = "$H1" ] && ok "7: связь D снята в invariants.yaml — коммит отказан (код $RC)" \
  || bad "7: связь D снята в invariants.yaml, а коммит прошёл (код $RC) — судья не зван"
grep -q 'scripts/test_i.sh: мутация пробы не названа' <<<"$OUT" \
  && ok "7: названа запись D без мутации" || bad "7: в выводе нет «scripts/test_i.sh: мутация пробы не названа»"

# ── 8. В индексе только dev-map.yaml → судья не зовётся (карта — почти в каждом коммите) ─
r=$(make_repo devmap-only) || { echo "FAIL: двор devmap-only не собран"; exit 1; }
export PRECOMMIT_LIVE_CHECK_JUDGE; PRECOMMIT_LIVE_CHECK_JUDGE=$(fake_judge "$r" 0)
# Схему карты хук судит соседом hackathon.py рядом с собой (путь через readlink, разбор
# соседей его не видит) — заглушка «пройдено», как у прочих соседей.
printf '#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n' > "$r/scripts/hackathon.py"
printf 'version: 1\nlines: []\nlayers: []\nqueue: []\n' > "$r/dev-map.yaml"; git -C "$r" add dev-map.yaml
commit "$r"
[ "$RC" = 0 ] && ok "8: коммит одной карты прошёл" || bad "8: коммит одной карты отказан (код $RC)"
[ "$(calls "$r")" = 0 ] && ok "8: на одной карте судья не зван (вызовов 0)" \
  || bad "8: на одной карте судья зван $(calls "$r") раз — 7 с почти на каждый коммит"
unset PRECOMMIT_LIVE_CHECK_JUDGE

# ── 9. Окружение коммитящего указывает судье на верные данные вне индекса ────
#    Судья обязан судить СНИМОК: переменная пути у человека не должна увести его мимо.
r=$(make_repo env-bypass) || { echo "FAIL: двор env-bypass не собран"; exit 1; }
mkdir -p "$TOP/env-good"
cp "$r/harness/config/mechanisms.yaml" "$TOP/env-good/mechanisms.yaml"
printf '%s' "$STATE_GOOD" > "$TOP/env-good/live-check-state.yaml"
printf '%s' "$STATE_BAD" > "$r/harness/config/live-check-state.yaml"
git -C "$r" add harness/config/live-check-state.yaml
# Условие двора: с этими переменными судья на дереве зелёный — значит, отказ хука
# возможен, только если хук их снял.
( cd "$r" && HARNESS_MECHANISMS="$TOP/env-good/mechanisms.yaml" \
  HARNESS_LIVE_CHECK_STATE="$TOP/env-good/live-check-state.yaml" \
  python3 scripts/build-live-check-list.py --данные >/dev/null 2>&1 ) \
  && ok "9: условие двора — по окружению судья зелёный" \
  || bad "9: двор не создал условие — судья красный и по окружению"
export HARNESS_MECHANISMS="$TOP/env-good/mechanisms.yaml" \
       HARNESS_LIVE_CHECK_STATE="$TOP/env-good/live-check-state.yaml"
commit "$r"
unset HARNESS_MECHANISMS HARNESS_LIVE_CHECK_STATE
[ "$RC" != 0 ] && [ "$H0" = "$H1" ] && ok "9: окружение на верные данные, индекс битый — коммит отказан (код $RC)" \
  || bad "9: окружение увело судью мимо снимка — битый индекс прошёл (код $RC)"
grep -q 'следа нет на диске' <<<"$OUT" && ok "9: названа причина из снимка — следа нет на диске" \
  || bad "9: причина из снимка не названа"

echo "итог: зелёных $n_ok, красных $n_bad"
[ "$fail" = 0 ] && echo "ВСЁ ЗЕЛЁНОЕ" || echo "КРАСНОЕ"
exit $fail
