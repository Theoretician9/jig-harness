#!/usr/bin/env bash
# Пробы шага установщика «голосовые»: venv faster-whisper для transcribe_voice.py.
#
# Откуда. Разбор правок соседней установки (18.10.2026, их b3a207b): голосовые
# сообщения владельца расшифровывает scripts/transcribe_voice.py, а он ищет
# venv в STATE_DIR/voice/venv (transcribe_voice.py:30). На чистой машине этого
# venv не ставит НИКТО: в USTANOVIT.sh про голосовые 0 строк, и первое голосовое
# владельца падает отказом «нет venv». У нас venv стоит руками с 11.09, поэтому
# живой машиной это не доказать.
#
# Что доказуемо без чистой установки — и что нет. Стенд исполняет САМ шаг,
# вынутый из USTANOVIT.sh, с подменёнными sudo/runuser/python3/chown: проба
# судит, ЧТО шаг зовёт, от чьего имени, куда и что делает с отметкой
# `--resume`. Что настоящий pip поставит faster-whisper и venv заработает —
# здесь НЕ доказывается: это проверка стенда чистой установки (install-probe).
#
# Договор шага (его пишет шаг «код»):
#   * функция `шаг_2в` в USTANOVIT.sh, строка `шаг_2в` в последовательности
#     прогона — после `шаг_2б` (нужен python3-venv шага 2);
#   * venv создаётся `python3 -m venv "$STATE_DIR/voice/venv"` ОТ ИМЕНИ
#     $AGENT_USER (`sudo -u` или `runuser -u`): диспетчер и агент живут им;
#   * faster-whisper ставит pip ЭТОГО venv, тоже от имени агента;
#   * успех — отметка шаг_2в.done; отказ pip — строка в FAILED_LIST со словом
#     «голос», отметки нет, установка не обрывается (голосовые — не основа);
#   * --resume с отметкой — пропуск без единого вызова;
#   * --dry-run печатает команду venv с путём …/voice/venv.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Установщик в КОРНЕ пакета; правило поиска пакета одно — scripts/lib/config.py.
INSTALLER="${INSTALL_VOICE_INSTALLER:-}"
if [ -z "$INSTALLER" ]; then
  INSTALLER="$HERE/../../USTANOVIT.sh"
  if [ ! -f "$INSTALLER" ]; then
    INSTALLER="$(python3 -c "import sys; sys.path.insert(0, '$HERE/lib'); import config; print(config.katalog_paketa() / 'USTANOVIT.sh')" 2>/dev/null || true)"
  fi
fi
if [ ! -f "$INSTALLER" ]; then
  printf 'ПЛОХО установщика нет ни рядом, ни в пакете: судить шаг нечем (искали «%s»)\n' "$INSTALLER"
  exit 1
fi
AGENT=stendagent

ok=1
paths=0
check() {
    paths=$((paths + 1))
    if [ "$2" = "$3" ]; then printf '  ок    %s\n' "$1"
    else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$3"; ok=0; fi
}
exists() { [ -e "$1" ] && echo да || echo нет; }
yes_if() { if "$@"; then echo да; else echo нет; fi; }

# Подставные: каждый вызов пишет строку «-u <от кого> <команда>» в state/calls.
# python3 -m venv X создаёт X/bin/pip и X/bin/python — тоже подставные, их
# вызовы идут туда же; STUB_PIP_RC — код возврата pip.
stand() {
    local T="$1"
    mkdir -p "$T/bin" "$T/state" "$T/stamps" "$T/var/voice-root"
    cat > "$T/bin/sudo" <<EOF
#!/usr/bin/env bash
[ "\$1" = "-u" ] && { STUB_AS_USER="\$2"; export STUB_AS_USER; shift 2; }
[ "\$1" = "-H" ] && shift
exec "\$@"
EOF
    cat > "$T/bin/runuser" <<EOF
#!/usr/bin/env bash
[ "\$1" = "-u" ] && { STUB_AS_USER="\$2"; export STUB_AS_USER; shift 2; }
[ "\$1" = "--" ] && shift
exec "\$@"
EOF
    cat > "$T/bin/python3" <<EOF
#!/usr/bin/env bash
printf -- '-u %s python3 %s\n' "\${STUB_AS_USER:-root}" "\$*" >> "$T/state/calls"
if [ "\$1" = "-m" ] && [ "\$2" = "venv" ]; then
  dst="\${@: -1}"
  mkdir -p "\$dst/bin"
  for b in pip python python3; do
    printf '#!/usr/bin/env bash\nprintf -- "-u %%s %%s %%s\\\\n" "\${STUB_AS_USER:-root}" "\$0" "\$*" >> "$T/state/calls"\nexit \${STUB_PIP_RC:-0}\n' > "\$dst/bin/\$b"
    chmod +x "\$dst/bin/\$b"
  done
  exit 0
fi
exec /usr/bin/python3 "\$@"
EOF
    cat > "$T/bin/chown" <<EOF
#!/usr/bin/env bash
printf -- '-u %s chown %s\n' "\${STUB_AS_USER:-root}" "\$*" >> "$T/state/calls"
EOF
    # mkdir и install «от root»: стенд живёт одним пользователем, поэтому root
    # изображается так — закрытые на запись предки внутри стенда на время
    # вызова открываются и закрываются обратно. От имени агента (STUB_AS_USER)
    # — настоящий вызов без поблажек: закрытый каталог отказывает, как на
    # чистой машине, где /var/lib/harness создан root (VG-08).
    cat > "$T/root-emul.sh" <<EOF
#!/usr/bin/env bash
# \$1 = настоящая команда, дальше её доводы; пути — доводы без «-»
real="\$1"; shift
printf -- '-u %s %s %s\n' "\${STUB_AS_USER:-root}" "\$(basename "\$real")" "\$*" >> "$T/state/calls"
if [ -n "\${STUB_AS_USER:-}" ] && [ "\${STUB_AS_USER}" != root ]; then exec "\$real" "\$@"; fi
opened=()
for a in "\$@"; do
  case "\$a" in "$T"/*) d=\$(dirname "\$a")
    while [ "\${d#$T}" != "\$d" ] && [ "\$d" != "$T" ]; do
      if [ -d "\$d" ] && [ ! -w "\$d" ]; then chmod u+w "\$d"; opened+=("\$d"); fi
      d=\$(dirname "\$d")
    done ;; esac
done
args=(); skip=0
for a in "\$@"; do
  if (( skip )); then skip=0; continue; fi
  case "\$a" in -o|-g|-m) skip=1; continue ;; -o*|-g*|-m*) [ "\$(basename "\$real")" = install ] && continue ;; esac
  args+=("\$a")
done
rc=0; "\$real" "\${args[@]}" || rc=\$?
for d in "\${opened[@]}"; do chmod u-w "\$d"; done
exit \$rc
EOF
    printf '#!/usr/bin/env bash\nexec bash "%s" /usr/bin/mkdir "$@"\n' "$T/root-emul.sh" > "$T/bin/mkdir"
    printf '#!/usr/bin/env bash\nexec bash "%s" /usr/bin/install "$@"\n' "$T/root-emul.sh" > "$T/bin/install"
    cat > "$T/bin/id" <<EOF
#!/usr/bin/env bash
case "\$1" in -u) echo 1001 ;; -g) echo 1001 ;; *) exec /usr/bin/id "\$@" ;; esac
EOF
    chmod +x "$T/bin/"*
}

extract_fn() {  # $1 = имя функции: однострочная или до первой «}» в начале строки
    # Пробелы между «()» и «{» законны: «скажи()     {» в установщике
    # выровнен, и точное «скажи() {» его не находило — функция выпадала, и
    # путь --resume падал на 127 вместо суждения.
    awk -v n="$1" '
      function head(s) { return index(s, n "()") == 1 && substr(s, length(n) + 3) ~ /^[[:space:]]*\{/ }
      head($0) { on = 1 }
      on { print }
      on && (/^}/ || (head($0) && /}[[:space:]]*$/)) { exit }
    ' "$INSTALLER"
}

STEP_PRESENT=$(yes_if grep -q '^шаг_2в() {' "$INSTALLER")

# ── 1. Шаг объявлен и стоит в прогоне после docker ───────────────────────────
check "установщик: функция шага голосовых шаг_2в объявлена" "да" "$STEP_PRESENT"
check "установщик: шаг_2в в последовательности прогона" "1" "$(grep -cx 'шаг_2в' "$INSTALLER")"
after=$(awk '/^шаг_2б$/ { b = NR } /^шаг_2в$/ { v = NR } END { print (b && v && v > b) ? "да" : "нет" }' "$INSTALLER")
check "установщик: шаг_2в идёт после шаг_2б (нужен python3-venv шага 2)" "да" "$after"

run_step() {  # $1 = каталог стенда, $2 = RESUME; окружение — STUB_PIP_RC
    local T="$1"
    {
        for f in скажи заголовок делай проверка стоп отметить уже_сделан шаг_2в; do extract_fn "$f"; done
        cat <<'EOF'
DONE_LIST=(); CHECKED_LIST=(); FAILED_LIST=()
шаг_2в
printf 'FAILED=%s\n' "${#FAILED_LIST[@]}"; printf 'F: %s\n' "${FAILED_LIST[@]}"
EOF
    } > "$T/step.sh"
    PATH="$T/bin:$PATH" DRY=0 RESUME="$2" SELF=USTANOVIT.sh HARNESS_SRC="$T/pkg" STAMP_DIR="$T/stamps" \
        AGENT_USER="$AGENT" STATE_DIR="$T/var/voice-root" \
        bash -c 'set -Eeuo pipefail; source "$0"' "$T/step.sh" 2>&1
}

if [ "$STEP_PRESENT" = да ]; then
    # ── 2. Шаг исполняется: venv от имени агента, pip этого venv ─────────────
    T=$(mktemp -d); stand "$T"
    OUT=$(STUB_PIP_RC=0 run_step "$T" 0); RC=$?
    VENV="$T/var/voice-root/voice/venv"
    check "шаг прошёл — rc 0" "0" "$RC"
    check "БОЛЬНОЙ: venv создан в STATE_DIR/voice/venv от имени агента" "да" \
          "$(yes_if grep -qx -- "-u $AGENT python3 -m venv $VENV" "$T/state/calls")"
    check "БОЛЬНОЙ: faster-whisper ставит pip ЭТОГО venv от имени агента" "да" \
          "$(yes_if grep -qE -- "^-u $AGENT $VENV/bin/(pip|python3?) .*install.*faster-whisper" "$T/state/calls")"
    check "venv от root не создавался" "нет" \
          "$(yes_if grep -q -- '^-u root python3 -m venv' "$T/state/calls")"
    check "шаг прошёл — отказов нет" "да" "$(yes_if grep -qx 'FAILED=0' <<<"$OUT")"
    check "шаг прошёл — отметка шаг_2в.done стоит" "да" "$(exists "$T/stamps/шаг_2в.done")"
    rm -rf "$T"

    # ── 3. pip отказал: громко, без отметки, установка идёт дальше ───────────
    T=$(mktemp -d); stand "$T"
    OUT=$(STUB_PIP_RC=1 run_step "$T" 0); RC=$?
    check "БОЛЬНОЙ: pip отказал — установка не оборвана (rc 0)" "0" "$RC"
    check "БОЛЬНОЙ: pip отказал — строка про голосовые в FAILED_LIST" "да" \
          "$(yes_if grep -qiE '^F: .*голос' <<<"$OUT")"
    check "БОЛЬНОЙ: pip отказал — отметки нет (повтор доставит venv)" "нет" "$(exists "$T/stamps/шаг_2в.done")"
    VENV="$T/var/voice-root/voice/venv"
    check "БОЛЬНОЙ VG-08: отказ pip называет, что чинить — pip и путь venv в строке FAILED_LIST" "да" \
          "$(yes_if grep -qE -- "^F: .*(pip.*$VENV|$VENV.*pip)" <<<"$OUT")"
    rm -rf "$T"

    # ── 3а. VG-08: STATE_DIR создан root, агенту в него не писать ─────────────
    # На чистой машине шаг 2 делает mkdir $STATE_DIR/panel-state от root и
    # отдаёт агенту только panel-state: /var/lib/harness — root:root 755, и
    # `sudo -u агент python3 -m venv …/voice/venv` падает Permission denied.
    # Здесь шаг работал лишь потому, что voice/ заведён руками 11.09.
    T=$(mktemp -d); stand "$T"; chmod 555 "$T/var/voice-root"
    OUT=$(STUB_PIP_RC=0 run_step "$T" 0); RC=$?
    chmod 755 "$T/var/voice-root"
    VENV="$T/var/voice-root/voice/venv"
    give=$(grep -nE -- "^-u root (install .*-o ?$AGENT .*$T/var/voice-root/voice/?\$|chown (-R )?$AGENT(:$AGENT)? $T/var/voice-root/voice/?\$)" "$T/state/calls" | head -1 | cut -d: -f1)
    venv_at=$(grep -nx -- "-u $AGENT python3 -m venv $VENV" "$T/state/calls" | head -1 | cut -d: -f1)
    check "БОЛЬНОЙ VG-08: STATE_DIR от root — шаг отдаёт voice/ агенту (install -o или chown) ДО venv" "да" \
          "$( [ -n "$give" ] && [ -n "$venv_at" ] && [ "$give" -lt "$venv_at" ] && echo да || echo нет)"
    check "БОЛЬНОЙ VG-08: STATE_DIR от root — venv агента создан (voice/venv/bin есть)" "да" "$(exists "$VENV/bin")"
    check "БОЛЬНОЙ VG-08: STATE_DIR от root — отказов нет" "да" "$(yes_if grep -qx 'FAILED=0' <<<"$OUT")"
    check "VG-08: STATE_DIR от root — rc 0" "0" "$RC"
    rm -rf "$T"

    # ── 4. --resume с отметкой — пропуск без вызовов ─────────────────────────
    T=$(mktemp -d); stand "$T"; : > "$T/stamps/шаг_2в.done"
    OUT=$(STUB_PIP_RC=0 run_step "$T" 1); RC=$?
    check "--resume с отметкой — rc 0" "0" "$RC"
    check "--resume с отметкой — ни одного вызова python3/pip" "нет" "$(exists "$T/state/calls")"
    check "--resume с отметкой — сказано «пропуск»" "да" "$(yes_if grep -q 'пропуск' <<<"$OUT")"
    rm -rf "$T"
else
    check "исполнение шага на стенде" "шаг есть" "шага нет — механизм не написан, судить исполнение нечем"
fi

# ── 5. Сухой прогон печатает команду venv ────────────────────────────────────
T=$(mktemp -d)
HARNESS_INSTALL_CONF="$T/install.conf" bash "$INSTALLER" --dry-run > "$T/out" 2>&1
check "установщик --dry-run отработал (rc 0)" "0" "$?"
check "БОЛЬНОЙ: --dry-run печатает venv голосовых (-m venv …/voice/venv)" "да" \
      "$(yes_if grep -qE -- '-m venv [^ ]*/voice/venv' "$T/out")"
rm -rf "$T"

if [ "$ok" = 1 ]; then
    echo "SELFTEST: зелёный ($paths путей; первый больной — venv голосовых не ставил никто, 18.10)"
    exit 0
fi
echo "SELFTEST: КРАСНЫЙ (путей $paths)"
exit 1
