#!/usr/bin/env bash
# Собрать юнит панели из шаблона по паспорту установки и поставить его.
#
# Зачем. Путь проекта у каждой установки свой, а в юните он стоял вшитым:
# на чужой машине служба падала бы на старте с 200/CHDIR («не смог перейти в
# рабочий каталог»), и панель — единственное окно владельца в харнес — просто
# не поднималась бы. Тот же отказ случился у нас 12.09.2026, когда сменились
# права каталога проекта: причина была не в коде панели, а в том, что юнит
# ничего не знает о своей установке.
#
# Значения берутся СТРОГО из паспорта (konf_iz_fajla), а не из окружения:
# подставленная переменная собрала бы юнит для чужого дерева.
#
# Запуск:
#   bash харнес/scripts/unit-paneli.sh            # собрать и поставить
#   bash харнес/scripts/unit-paneli.sh --показать # напечатать, ничего не менять
#   bash харнес/scripts/unit-paneli.sh --selftest
set -euo pipefail

SAM="$(readlink -f "${BASH_SOURCE[0]}")"
ZDES="$(dirname "$SAM")"
# shellcheck source=/dev/null
source "$ZDES/lib/konf.sh"

UNIT_NAME="harness-panel.service"
UNIT_DIR="${PANEL_UNIT_DIR:-/etc/systemd/system}"

sobrat_unit() {  # $1 = шаблон, $2 = путь проекта → печатает юнит
    local shablon="$1" proekt="$2"
    [ -f "$shablon" ] || { echo "нет шаблона юнита: $shablon" >&2; return 1; }
    [ -n "$proekt" ] || { echo "пуст PROJECT_DIR — юнит собирать не из чего" >&2; return 1; }
    sed "s|@PROJECT_DIR@|$proekt|g" "$shablon"
}

# ── самотест: подставной паспорт и шаблон, пять путей ───────────────────────
if [ "${1:-}" = "--selftest" ]; then
    T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
    ok=1
    proba() { # $1 = имя, $2 = ждём, $3 = вышло
        if [ "$2" = "$3" ]; then printf '  ок    %s\n' "$1"
        else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$3"; ok=0; fi
    }
    printf 'WorkingDirectory=@PROJECT_DIR@\nExecStart=/usr/bin/python3 @PROJECT_DIR@/панель/server.py\n' \
        > "$T/shablon.in"

    OUT=$(sobrat_unit "$T/shablon.in" "/srv/подставной")
    proba "путь подставлен во все места" "2" \
          "$(printf '%s\n' "$OUT" | grep -c '/srv/подставной')"
    proba "плейсхолдеров не осталось" "0" \
          "$(printf '%s\n' "$OUT" | grep -c '@PROJECT_DIR@' || true)"

    # БОЛЬНОЙ СЛУЧАЙ: пустой путь. Юнит с пустым WorkingDirectory systemd
    # принимает молча и падает на старте — отказ обязан быть здесь.
    RC_PUSTO=0
    sobrat_unit "$T/shablon.in" "" >/dev/null 2>&1 || RC_PUSTO=$?
    proba "БОЛЬНОЙ: пустой PROJECT_DIR — отказ, а не пустой юнит" "1" "$RC_PUSTO"

    RC_NET=0
    sobrat_unit "$T/net-takogo.in" "/srv/подставной" >/dev/null 2>&1 || RC_NET=$?
    proba "БОЛЬНОЙ: нет шаблона — отказ" "1" "$RC_NET"

    # БОЛЬНОЙ: значение берётся из ПАСПОРТА, а не из окружения.
    printf 'PROJECT_DIR="/из-паспорта"\n' > "$T/pasport.conf"
    IZ_FAJLA=$(HARNESS_INSTALL_CONF="$T/pasport.conf" PROJECT_DIR=/из-окружения \
               bash -c 'source "$1"; konf_iz_fajla PROJECT_DIR' _ "$ZDES/lib/konf.sh")
    proba "БОЛЬНОЙ: путь взят из паспорта, а не из окружения" "/из-паспорта" "$IZ_FAJLA"

    [ "$ok" = 1 ] && { echo "САМОТЕСТ ПРОЙДЕН: 5 путей, из них три больных"; exit 0; }
    echo "САМОТЕСТ ПРОВАЛЕН"; exit 1
fi

konf_zagruzit
PROEKT="$(konf_iz_fajla PROJECT_DIR)"
SHABLON="$PROEKT/харнес/панель/$UNIT_NAME.in"
[ -f "$SHABLON" ] || SHABLON="$ZDES/../панель/$UNIT_NAME.in"

if [ "${1:-}" = "--показать" ]; then
    sobrat_unit "$SHABLON" "$PROEKT"
    exit 0
fi

VREMENNYJ=$(mktemp)
trap 'rm -f "$VREMENNYJ"' EXIT
sobrat_unit "$SHABLON" "$PROEKT" > "$VREMENNYJ"

if [ -f "$UNIT_DIR/$UNIT_NAME" ] && cmp -s "$VREMENNYJ" "$UNIT_DIR/$UNIT_NAME"; then
    echo "[юнит панели] уже собран под $PROEKT — ничего не меняю"
    exit 0
fi

sudo install -m 644 "$VREMENNYJ" "$UNIT_DIR/$UNIT_NAME"
sudo systemctl daemon-reload
echo "[юнит панели] собран под $PROEKT и поставлен в $UNIT_DIR/$UNIT_NAME"
echo "[юнит панели] применить: sudo systemctl restart $UNIT_NAME"
