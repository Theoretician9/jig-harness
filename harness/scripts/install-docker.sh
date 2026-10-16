#!/usr/bin/env bash
# install-docker.sh — шаг установки: docker и docker compose для агента. Только root.
#
# Зачем: продукт запускается docker compose, а поставить docker без root агент
# не может — без этого шага владельцу остаётся root-команда руками.
# Цена: группа docker равна правам root для агента (сокет демона пускает
#   смонтировать / в контейнер); принято, пока продукт живёт на compose.
# Не лечит: docker-ce из репозитория Docker без compose-плагина — apt упрётся
#   в конфликт с containerd.io; отказ тогда называет, что поставить вместо.
#
# Идемпотентен: стоит — apt не зовётся, агент в группе — usermod не зовётся,
# живой tmux-сервер агента уже с группой — агент не перезапускается.
# Проверка шага — от имени агента (`docker compose version`, `docker info`) и
# по группам ЖИВОГО tmux-сервера агента: группы процесс берёт при старте, и
# `sudo -u` зеленел бы при агенте, у которого docker отвечает permission denied.
#
# Запуск: bash install-docker.sh <пользователь-агента> [файл-итога]
#   файл-итога — сюда дописываются строки для сводки установки.
# Код возврата: 0 — docker работает у агента, 1 — отказ (строка «ОТКАЗ docker»),
#   2 — не назван пользователь.
# Пробы: bash scripts/test_install_docker.sh
set -uo pipefail

AGENT_USER="${1:-}"
SUMMARY_FILE="${2:-/dev/null}"
AGENT_UNIT=harness-agent.service
# Корень /proc подменяется только пробой: группы сервера стенд пишет в свой файл.
PROC_DIR="${INSTALL_DOCKER_PROC_DIR:-/proc}"
[ -n "$AGENT_USER" ] || { echo "ОТКАЗ docker: не назван пользователь агента (bash $0 <пользователь>)" >&2; exit 2; }

# Причина отказа уходит и в итог установки: строка в прокрутке до владельца не доходит.
fail() { printf 'ОТКАЗ docker: %s\n' "$1" >&2; printf '%s\n' "$1" >> "$SUMMARY_FILE"; exit 1; }
in_docker_group() { id -nG "$AGENT_USER" | tr ' ' '\n' | grep -qx docker; }

docker_ce_hint() {
    dpkg-query -W -f='${Status}' docker-ce 2>/dev/null | grep -q 'ok installed' || return 0
    echo "  подсказка: стоит docker-ce из репозитория Docker — docker.io с ним конфликтует;" >&2
    echo "  поставьте compose-плагин: apt-get install -y docker-compose-plugin, затем повторите установку" >&2
}

# rc 0 — у живого tmux-сервера агента есть группа docker; rc 1 — нет;
# rc 2 — сервера нет вовсе.
server_has_docker_group() {
    local pid gid
    pid=$(sudo -u "$AGENT_USER" tmux display-message -p '#{pid}' 2>/dev/null) || return 2
    [ -n "$pid" ] || return 2
    gid=$(getent group docker | cut -d: -f3)
    [ -n "$gid" ] || return 1
    grep '^Groups:' "$PROC_DIR/$pid/status" 2>/dev/null | tr ' \t' '\n\n' | grep -qx "$gid"
}

if docker compose version >/dev/null 2>&1; then
    echo "  docker и compose уже стоят — apt пропущен"
else
    echo "  + apt-get install -y docker.io docker-compose-v2"
    DEBIAN_FRONTEND=noninteractive apt-get update -qq \
        || fail "apt-get update не прошёл (вывод выше)"
    if ! DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2; then
        docker_ce_hint
        fail "apt-get install docker.io docker-compose-v2 не прошёл (вывод выше)"
    fi
fi

if in_docker_group; then
    echo "  $AGENT_USER уже в группе docker"
else
    echo "  + usermod -aG docker $AGENT_USER"
    usermod -aG docker "$AGENT_USER" || fail "usermod -aG docker $AGENT_USER не прошёл"
    in_docker_group || fail "usermod прошёл, а $AGENT_USER в группе docker нет: getent group docker"
fi

echo "  + systemctl enable --now docker"
systemctl enable --now docker || fail "systemctl enable --now docker не прошёл"
systemctl is-enabled --quiet docker && systemctl is-active --quiet docker \
    || fail "служба docker не enabled+active: systemctl status docker --no-pager"

sudo -u "$AGENT_USER" docker compose version >/dev/null 2>&1 \
    || fail "docker compose version от имени $AGENT_USER не отвечает"
sudo -u "$AGENT_USER" docker info >/dev/null 2>&1 \
    || fail "docker info от имени $AGENT_USER не отвечает: id -nG $AGENT_USER; systemctl status docker"
echo "  OK: docker compose version и docker info от имени $AGENT_USER отвечают"

# Живой агент получает группу только с НОВЫМ tmux-сервером. Решает сам сервер,
# а не состояние юнита: сервер, поднятый сторожем из cron, живёт и при
# неактивном юните. Остановка юнита убивает сервер в его cgroup; сервер вне
# её — только kill-server, поэтому он явно, до старта.
server_has_docker_group; rc=$?
case "$rc" in
    0) echo "  tmux-сервер агента уже с группой docker — перезапуск не нужен" ;;
    2) echo "  tmux-сервера агента нет — новый поднимется уже с группой docker" ;;
    *)
        # Установщик, запущенный из tmux агента, сидит в cgroup его юнита:
        # stop убил бы и его — без сводки и с остановленным агентом.
        if grep -q "$AGENT_UNIT" "$PROC_DIR/self/cgroup" 2>/dev/null; then
            fail "перезапусти агента вне его tmux: sudo systemctl restart $AGENT_UNIT (установка запущена изнутри агента — перезапуск оборвал бы её саму)"
        fi
        echo "  + systemctl stop $AGENT_UNIT; tmux kill-server от $AGENT_USER; systemctl start $AGENT_UNIT"
        systemctl stop "$AGENT_UNIT" || fail "systemctl stop $AGENT_UNIT не прошёл"
        sudo -u "$AGENT_USER" tmux kill-server 2>/dev/null || true
        systemctl start "$AGENT_UNIT" || fail "systemctl start $AGENT_UNIT не прошёл: journalctl -u $AGENT_UNIT -n 30"
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            server_has_docker_group && break
            sleep 3
        done
        server_has_docker_group \
            || fail "агент перезапущен, а у его нового tmux-сервера группы docker нет: tmux display-message -p '#{pid}'; grep Groups /proc/<pid>/status"
        echo "  OK: агент перезапущен, у нового tmux-сервера группа docker"
        echo "агент перезапущен, чтобы получить группу docker" >> "$SUMMARY_FILE"
        ;;
esac
