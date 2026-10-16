#!/usr/bin/env bash
# Пробы шага установки docker (harness/scripts/install-docker.sh и шаг_2б
# установщика).
#
# Больной случай — живой: на сервере docker не было, продукт запускается
# docker compose, агент без root поставить его не может, и владельцу пришлось
# слать root-команду руками (14.10.2026).
#
# Стенд в mktemp: подставные apt-get, usermod, systemctl, sudo, id, getent,
# dpkg-query, tmux, sleep и docker; группы tmux-сервера агента — файл стенда
# вместо /proc. Настоящий docker машины не виден: подставной лежит первым в
# PATH и отвечает только тогда, когда стенд «поставил» его.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${INSTALL_DOCKER_SCRIPT:-$HERE/install-docker.sh}"
# Установщик лежит в КОРНЕ пакета. В пакете scripts/ — это harness/scripts/, и
# «два уровня выше» попадают куда надо; в проекте раскладчик кладёт scripts/ в
# корень, и тот же путь уходит в /var/www — awk падал «cannot open file», а
# четыре случая краснели без внятной причины (прогон 15.10.2026). Правило
# поиска пакета одно и живёт в scripts/lib/config.py — здесь его спрашивают,
# а не повторяют.
INSTALLER="${INSTALL_DOCKER_INSTALLER:-}"
if [ -z "$INSTALLER" ]; then
  INSTALLER="$HERE/../../USTANOVIT.sh"
  if [ ! -f "$INSTALLER" ]; then
    INSTALLER="$(python3 -c "import sys; sys.path.insert(0, '$HERE/lib'); import config; print(config.katalog_paketa() / 'USTANOVIT.sh')" 2>/dev/null || true)"
  fi
fi
if [ ! -f "$INSTALLER" ]; then
  printf 'ПЛОХО установщика нет ни рядом, ни в пакете: судить шаг установки нечем (искали «%s»)\n' "$INSTALLER"
  exit 1
fi
AGENT=stendagent
DOCKER_GID=999
SERVER_PID=4242

ok=1
paths=0
check() {
    paths=$((paths + 1))
    if [ "$2" = "$3" ]; then printf '  ок    %s\n' "$1"
    else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$3"; ok=0; fi
}
exists() { [ -e "$1" ] && echo да || echo нет; }
has_line() { grep -qx -- "$2" "$1" 2>/dev/null && echo да || echo нет; }
count() { [ -f "$1" ] && grep -c . "$1" || echo 0; }

# Состояние машины — файлы в $1/state:
#   groups         — группы агента в /etc/group (по строке)
#   installed      — docker поставлен; active — служба docker поднята
#   agent-active   — юнит агента активен
#   server-groups  — gid-ы живого tmux-сервера агента (нет файла — нет сервера)
stand() {
    local T="$1"
    mkdir -p "$T/bin" "$T/state" "$T/proc/$SERVER_PID" "$T/proc/self"
    printf '0::/user.slice/user-0.slice/session-1.scope\n' > "$T/proc/self/cgroup"
    printf '%s\n' "$AGENT" > "$T/state/groups"
    cat > "$T/bin/apt-get" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$T/state/apt-calls"
case "\$*" in *install*) [ "\${STUB_APT_RC:-0}" = 0 ] || { echo "E: Unable to locate package docker-compose-v2" >&2; exit "\$STUB_APT_RC"; } ;; esac
case "\$*" in *install*docker.io*docker-compose-v2*) touch "$T/state/installed" ;; esac
exit 0
EOF
    cat > "$T/bin/usermod" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$T/state/usermod-calls"
[ -n "\${STUB_USERMOD_NOOP:-}" ] && exit 0
[ "\$1 \$2" = "-aG docker" ] && printf 'docker\n' >> "$T/state/groups"
exit 0
EOF
    cat > "$T/bin/id" <<EOF
#!/usr/bin/env bash
[ "\$1" = "-nG" ] && { tr '\n' ' ' < "$T/state/groups"; echo; exit 0; }
exec /usr/bin/id "\$@"
EOF
    printf '#!/usr/bin/env bash\n[ "$1 $2" = "group docker" ] && echo "docker:x:%s:"\n' "$DOCKER_GID" > "$T/bin/getent"
    cat > "$T/bin/dpkg-query" <<EOF
#!/usr/bin/env bash
[ -n "\${STUB_DOCKER_CE:-}" ] && { printf 'install ok installed'; exit 0; }
exit 1
EOF
    # Новый сервер берёт группы из /etc/group в момент старта юнита.
    cat > "$T/bin/systemctl" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$T/state/systemctl-calls"
case "\$*" in
  "enable --now docker") [ -n "\${STUB_DOCKER_DEAD:-}" ] || touch "$T/state/active" ;;
  *is-enabled*" docker"|*is-active*" docker") [ -f "$T/state/active" ] ;;
  *is-active*harness-agent*) [ -f "$T/state/agent-active" ] ;;
  "stop harness-agent.service") [ -n "\${STUB_SERVER_OUTSIDE_CGROUP:-}" ] || rm -f "$T/state/server-groups" ;;
  "start harness-agent.service")
    [ -f "$T/state/server-groups" ] && exit 0
    if grep -qx docker "$T/state/groups" && [ -z "\${STUB_NEW_SERVER_NO_GROUP:-}" ]; then
      echo "1001 $DOCKER_GID"; else echo 1001; fi > "$T/state/server-groups" ;;
esac
EOF
    cat > "$T/bin/tmux" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$T/state/tmux-calls"
case "\$1" in
  display-message)
    [ -f "$T/state/server-groups" ] || exit 1
    printf 'Groups:\t%s\n' "\$(cat "$T/state/server-groups")" > "$T/proc/$SERVER_PID/status"
    echo $SERVER_PID ;;
  kill-server) rm -f "$T/state/server-groups" ;;
esac
EOF
    # Подставной sudo пишет, ОТ КОГО и ЧТО звали: проба судит вызов, а не echo.
    cat > "$T/bin/sudo" <<EOF
#!/usr/bin/env bash
[ "\$1" = "-u" ] && { STUB_AS_USER="\$2"; export STUB_AS_USER; shift 2; }
printf -- '-u %s %s\n' "\${STUB_AS_USER:-root}" "\$*" >> "$T/state/sudo-calls"
exec "\$@"
EOF
    cat > "$T/bin/docker" <<EOF
#!/usr/bin/env bash
[ -f "$T/state/installed" ] || { echo "docker: command not found" >&2; exit 127; }
case "\$1" in
  compose) echo "Docker Compose version v2.24.6"; exit 0 ;;
  info)
    [ -f "$T/state/active" ] || { echo "Cannot connect to the Docker daemon" >&2; exit 1; }
    if [ -n "\${STUB_AS_USER:-}" ] && ! grep -qx docker "$T/state/groups"; then
      echo "permission denied while trying to connect to the Docker daemon socket" >&2; exit 1
    fi
    echo "Server Version: 24.0.7"; exit 0 ;;
esac
exit 0
EOF
    printf '#!/usr/bin/env bash\nexit 0\n' > "$T/bin/sleep"
    chmod +x "$T/bin/"*
}

run_step() {  # $1 = каталог стенда; итог шага — в $1/summary
    PATH="$1/bin:/usr/bin:/bin" INSTALL_DOCKER_PROC_DIR="$1/proc" \
        bash "$SCRIPT" "$AGENT" "$1/summary" 2>&1
}
ready_machine() {  # docker стоит, служба поднята, агент в группе
    touch "$1/state/installed" "$1/state/active"; printf 'docker\n' >> "$1/state/groups"
}

# ── 1. БОЛЬНОЙ: docker нет вовсе (живой случай 14.10.2026) ───────────────
T=$(mktemp -d); stand "$T"
OUT=$(run_step "$T"); RC=$?
check "БОЛЬНОЙ: docker нет — шаг доходит до конца (rc 0)" "0" "$RC"
check "БОЛЬНОЙ: docker нет — apt ставит docker.io и docker-compose-v2" "да" \
      "$(grep -q 'install -y docker.io docker-compose-v2' "$T/state/apt-calls" 2>/dev/null && echo да || echo нет)"
check "БОЛЬНОЙ: docker нет — агент добавлен в группу docker" "да" "$(has_line "$T/state/usermod-calls" "-aG docker $AGENT")"
check "БОЛЬНОЙ: docker нет — служба включена и поднята" "да" "$(has_line "$T/state/systemctl-calls" "enable --now docker")"
check "БОЛЬНОЙ: docker нет — docker info звали ОТ АГЕНТА" "да" "$(has_line "$T/state/sudo-calls" "-u $AGENT docker info")"
check "БОЛЬНОЙ: docker нет — compose version звали ОТ АГЕНТА" "да" "$(has_line "$T/state/sudo-calls" "-u $AGENT docker compose version")"
check "чистая машина (юнита агента нет) — агента не перезапускали" "нет" \
      "$(grep -qE '^(stop|start) harness-agent' "$T/state/systemctl-calls" && echo да || echo нет)"
rm -rf "$T"

# ── 2. Повторный прогон на готовой машине: ничего не ставится заново ─────
T=$(mktemp -d); stand "$T"; ready_machine "$T"
touch "$T/state/agent-active"; echo "1001 $DOCKER_GID" > "$T/state/server-groups"
OUT=$(run_step "$T"); RC=$?
check "готовая машина — rc 0" "0" "$RC"
check "готовая машина — apt не звали" "нет" "$(exists "$T/state/apt-calls")"
check "готовая машина — usermod не звали" "нет" "$(exists "$T/state/usermod-calls")"
check "готовая машина — агента не перезапускали" "нет" "$(grep -qE '^(stop|start) harness-agent' "$T/state/systemctl-calls" && echo да || echo нет)"
check "готовая машина — в итоге нет строки о перезапуске" "нет" "$(exists "$T/summary")"
rm -rf "$T"

# ── 3. БОЛЬНОЙ: существующая установка — агент живой, группу дали сейчас ──
# Живой tmux-сервер со старыми группами: без перезапуска docker у агента
# отвечает permission denied, хотя sudo -u зеленеет.
T=$(mktemp -d); stand "$T"; touch "$T/state/installed" "$T/state/active" "$T/state/agent-active"
echo 1001 > "$T/state/server-groups"
OUT=$(run_step "$T"); RC=$?
check "БОЛЬНОЙ: живой агент без группы — rc 0" "0" "$RC"
check "БОЛЬНОЙ: живой агент без группы — apt не звали" "нет" "$(exists "$T/state/apt-calls")"
check "БОЛЬНОЙ: живой агент без группы — usermod один раз" "1" "$(count "$T/state/usermod-calls")"
check "БОЛЬНОЙ: живой агент без группы — юнит агента перезапущен" "да" \
      "$(has_line "$T/state/systemctl-calls" "start harness-agent.service")"
check "БОЛЬНОЙ: живой агент без группы — у НОВОГО tmux-сервера группа docker" "да" \
      "$(grep -qw "$DOCKER_GID" "$T/state/server-groups" 2>/dev/null && echo да || echo нет)"
check "БОЛЬНОЙ: живой агент без группы — строка в итоге установки" "да" \
      "$(has_line "$T/summary" "агент перезапущен, чтобы получить группу docker")"
rm -rf "$T"

# ── 3б. БОЛЬНОЙ: tmux-сервер вне cgroup юнита (поднят сторожем из cron) ──
T=$(mktemp -d); stand "$T"; touch "$T/state/installed" "$T/state/active" "$T/state/agent-active"
echo 1001 > "$T/state/server-groups"
OUT=$(STUB_SERVER_OUTSIDE_CGROUP=1 run_step "$T"); RC=$?
check "БОЛЬНОЙ: сервер вне cgroup — rc 0" "0" "$RC"
check "БОЛЬНОЙ: сервер вне cgroup — kill-server звали" "да" "$(has_line "$T/state/tmux-calls" "kill-server")"
check "БОЛЬНОЙ: сервер вне cgroup — новый сервер с группой docker" "да" \
      "$(grep -qw "$DOCKER_GID" "$T/state/server-groups" 2>/dev/null && echo да || echo нет)"
rm -rf "$T"

# ── 3в. БОЛЬНОЙ: группу дали руками раньше, а сервер старый ──────────────
# Агент «уже в группе», но живой процесс её не видит — судит процесс, не /etc/group.
T=$(mktemp -d); stand "$T"; ready_machine "$T"; touch "$T/state/agent-active"
echo 1001 > "$T/state/server-groups"
OUT=$(run_step "$T"); RC=$?
check "БОЛЬНОЙ: группа в /etc/group, сервер старый — usermod не звали" "нет" "$(exists "$T/state/usermod-calls")"
check "БОЛЬНОЙ: группа в /etc/group, сервер старый — агент перезапущен" "да" \
      "$(grep -qw "$DOCKER_GID" "$T/state/server-groups" 2>/dev/null && echo да || echo нет)"
rm -rf "$T"

# ── 3г. БОЛЬНОЙ: юнит агента неактивен, а сервер жив (поднят сторожем) ───
T=$(mktemp -d); stand "$T"; ready_machine "$T"
echo 1001 > "$T/state/server-groups"
OUT=$(run_step "$T"); RC=$?
check "БОЛЬНОЙ: юнит неактивен, сервер старый — rc 0" "0" "$RC"
check "БОЛЬНОЙ: юнит неактивен, сервер старый — сервер заменён на новый с группой" "да" \
      "$(grep -qw "$DOCKER_GID" "$T/state/server-groups" 2>/dev/null && echo да || echo нет)"
rm -rf "$T"

# ── 3д. БОЛЬНОЙ: установщик запущен из tmux агента (в cgroup его юнита) ──
# stop убил бы сам установщик: без сводки и с остановленным агентом.
T=$(mktemp -d); stand "$T"; ready_machine "$T"; touch "$T/state/agent-active"
echo 1001 > "$T/state/server-groups"
printf '0::/system.slice/harness-agent.service\n' > "$T/proc/self/cgroup"
OUT=$(run_step "$T"); RC=$?
check "БОЛЬНОЙ: изнутри агента — rc 1 (шаг не OK)" "1" "$RC"
check "БОЛЬНОЙ: изнутри агента — stop не звали" "нет" \
      "$(grep -q '^stop harness-agent' "$T/state/systemctl-calls" 2>/dev/null && echo да || echo нет)"
check "БОЛЬНОЙ: изнутри агента — kill-server не звали" "нет" "$(has_line "$T/state/tmux-calls" "kill-server")"
check "БОЛЬНОЙ: изнутри агента — в итоге «перезапусти агента вне его tmux»" "да" \
      "$(grep -q '^перезапусти агента вне его tmux: sudo systemctl restart harness-agent.service' "$T/summary" 2>/dev/null && echo да || echo нет)"
rm -rf "$T"

# ── 3е. БОЛЬНОЙ: перезапустили, а у нового сервера группы всё равно нет ──
T=$(mktemp -d); stand "$T"; ready_machine "$T"; touch "$T/state/agent-active"
echo 1001 > "$T/state/server-groups"
OUT=$(STUB_NEW_SERVER_NO_GROUP=1 run_step "$T"); RC=$?
check "БОЛЬНОЙ: новый сервер без группы — rc 1" "1" "$RC"
check "БОЛЬНОЙ: новый сервер без группы — ОТКАЗ словом" "да" \
      "$(printf '%s' "$OUT" | grep -q 'ОТКАЗ docker: агент перезапущен, а у его нового tmux-сервера группы docker нет' && echo да || echo нет)"
check "БОЛЬНОЙ: новый сервер без группы — строки «перезапущен» в итоге нет" "нет" \
      "$(grep -qx 'агент перезапущен, чтобы получить группу docker' "$T/summary" 2>/dev/null && echo да || echo нет)"
rm -rf "$T"

# ── 4. БОЛЬНОЙ: группа docker-ci — не «уже в группе» ─────────────────────
T=$(mktemp -d); stand "$T"; touch "$T/state/installed" "$T/state/active"
printf 'docker-ci\n' >> "$T/state/groups"
OUT=$(run_step "$T"); RC=$?
check "БОЛЬНОЙ: агент в docker-ci — usermod звали" "1" "$(count "$T/state/usermod-calls")"
check "БОЛЬНОЙ: агент в docker-ci — rc 0" "0" "$RC"
rm -rf "$T"

# ── 5. БОЛЬНОЙ: usermod «прошёл», а группы нет ───────────────────────────
T=$(mktemp -d); stand "$T"; touch "$T/state/installed" "$T/state/active"
OUT=$(STUB_USERMOD_NOOP=1 run_step "$T"); RC=$?
check "БОЛЬНОЙ: usermod без эффекта — rc 1" "1" "$RC"
check "БОЛЬНОЙ: usermod без эффекта — ОТКАЗ словом" "да" "$(printf '%s' "$OUT" | grep -q 'ОТКАЗ docker' && echo да || echo нет)"
rm -rf "$T"

# ── 6. БОЛЬНОЙ: apt отказал — громкий отказ, не тихий пропуск ─────────────
T=$(mktemp -d); stand "$T"
OUT=$(STUB_APT_RC=100 run_step "$T"); RC=$?
check "БОЛЬНОЙ: apt отказал — rc 1" "1" "$RC"
check "БОЛЬНОЙ: apt отказал — ОТКАЗ и причина apt" "да" \
      "$(printf '%s' "$OUT" | grep -q 'ОТКАЗ docker' && printf '%s' "$OUT" | grep -q 'Unable to locate' && echo да || echo нет)"
check "БОЛЬНОЙ: apt отказал без docker-ce — подсказки нет" "нет" \
      "$(printf '%s' "$OUT" | grep -q 'docker-compose-plugin' && echo да || echo нет)"
rm -rf "$T"

# ── 7. БОЛЬНОЙ: docker-ce из репозитория Docker — подсказка, что ставить ──
T=$(mktemp -d); stand "$T"
OUT=$(STUB_APT_RC=100 STUB_DOCKER_CE=1 run_step "$T"); RC=$?
check "БОЛЬНОЙ: docker-ce — rc 1" "1" "$RC"
check "БОЛЬНОЙ: docker-ce — подсказка docker-compose-plugin" "да" \
      "$(printf '%s' "$OUT" | grep -q 'apt-get install -y docker-compose-plugin' && echo да || echo нет)"
rm -rf "$T"

# ── 8. БОЛЬНОЙ: служба не поднялась — проверка шага не зеленеет ──────────
T=$(mktemp -d); stand "$T"
OUT=$(STUB_DOCKER_DEAD=1 run_step "$T"); RC=$?
check "БОЛЬНОЙ: служба мертва — rc 1" "1" "$RC"
check "БОЛЬНОЙ: служба мертва — причина названа" "да" "$(printf '%s' "$OUT" | grep -q 'ОТКАЗ docker' && echo да || echo нет)"
rm -rf "$T"

# ── 9. БОЛЬНОЙ: без имени агента — отказ до любого действия ──────────────
T=$(mktemp -d); stand "$T"
PATH="$T/bin:/usr/bin:/bin" bash "$SCRIPT" >/dev/null 2>&1; RC=$?
check "БОЛЬНОЙ: без имени агента — rc 2" "2" "$RC"
check "БОЛЬНОЙ: без имени агента — apt не звали" "нет" "$(exists "$T/state/apt-calls")"
rm -rf "$T"

# ── 10. Установщик: шаг_2б ИСПОЛНЯЕТСЯ на стенде ─────────────────────────
# Функции берутся из самого USTANOVIT.sh, шаг зовёт подставной install-docker.sh.
# Шаг стоит в последовательности прогона — значит, его получает и повторный
# запуск на установленной машине: его отметки там ещё нет.
extract_fn() {  # $1 = имя функции: однострочная или до первой «}» в начале строки
    awk -v n="$1" '
      index($0, n "() {") == 1 { on = 1 }
      on { print }
      on && (/^}/ || (index($0, n "() {") == 1 && /}[[:space:]]*$/)) { exit }
    ' "$INSTALLER"
}
run_installer_step() {  # $1 = каталог, $2 = rc подставного шага, $3 = строка итога шага
    local T="$1"
    mkdir -p "$T/pkg/scripts" "$T/stamps"
    printf '#!/usr/bin/env bash\n[ -n "%s" ] && echo "%s" >> "$2"\nexit %s\n' "$3" "$3" "$2" > "$T/pkg/scripts/install-docker.sh"
    {
        for f in скажи заголовок отметить уже_сделан шаг_2б; do extract_fn "$f"; done
        cat <<'EOF'
DONE_LIST=(); CHECKED_LIST=(); FAILED_LIST=()
шаг_2б
printf 'FAILED=%s\n' "${#FAILED_LIST[@]}"; printf 'F: %s\n' "${FAILED_LIST[@]}"
printf 'D: %s\n' "${DONE_LIST[@]}"
EOF
    } > "$T/step.sh"
    DRY=0 RESUME=1 HARNESS_SRC="$T/pkg" STAMP_DIR="$T/stamps" AGENT_USER="$AGENT" \
        bash -c 'set -Eeuo pipefail; source "$0"' "$T/step.sh" 2>&1
}
check "установщик: шаг docker в последовательности прогона" "1" "$(grep -cx 'шаг_2б' "$INSTALLER")"

T=$(mktemp -d)
OUT=$(run_installer_step "$T" 1 "перезапусти агента вне его tmux: sudo systemctl restart harness-agent.service"); RC=$?
check "БОЛЬНОЙ: шаг упал — отказ в FAILED_LIST" "да" "$(printf '%s\n' "$OUT" | grep -q '^F: docker не поставлен' && echo да || echo нет)"
check "БОЛЬНОЙ: шаг упал — его причина строкой в FAILED_LIST" "да" \
      "$(printf '%s\n' "$OUT" | grep -qx 'F: docker: перезапусти агента вне его tmux: sudo systemctl restart harness-agent.service' && echo да || echo нет)"
check "БОЛЬНОЙ: шаг упал — отметки шага нет (повтор доставит docker)" "нет" "$(exists "$T/stamps/шаг_2б.done")"
check "БОЛЬНОЙ: шаг упал — установка не оборвана" "0" "$RC"
rm -rf "$T"

T=$(mktemp -d)
OUT=$(run_installer_step "$T" 0 "агент перезапущен, чтобы получить группу docker")
check "шаг прошёл — отказов нет" "да" "$(printf '%s\n' "$OUT" | grep -qx 'FAILED=0' && echo да || echo нет)"
check "шаг прошёл — отметка стоит" "да" "$(exists "$T/stamps/шаг_2б.done")"
check "шаг прошёл — строка о перезапуске агента в итоге" "да" \
      "$(printf '%s\n' "$OUT" | grep -qx 'D: агент перезапущен, чтобы получить группу docker' && echo да || echo нет)"
rm -rf "$T"

T=$(mktemp -d)
HARNESS_INSTALL_CONF="$T/install.conf" bash "$INSTALLER" --dry-run > "$T/out" 2>&1
check "установщик --dry-run печатает шаг docker" "да" "$(grep -q 'install-docker.sh' "$T/out" && echo да || echo нет)"
rm -rf "$T"

if [ "$ok" = 1 ]; then
    echo "SELFTEST: зелёный ($paths путей; первый больной — docker нет вовсе, живой случай 14.10)"
    exit 0
fi
echo "SELFTEST: КРАСНЫЙ (путей $paths)"
exit 1
