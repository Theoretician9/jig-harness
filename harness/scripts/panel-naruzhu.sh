#!/usr/bin/env bash
# Вывести панель наружу одной командой владельца — без терминала.
#
# Зачем. Ревью спеки 13.09.2026 (S-05): цепочка «установка печатает строку
# certbot, владелец её выполнит» упирается в человека у терминала, которого в
# контуре НЕТ (владелец видит только канал). Механизм, записанный как
# «владелец когда-нибудь сделает», мёртв и выглядит живым.
#
# Имя не спрашивается, а ВЫВОДИТСЯ из данных: внешний адрес машины →
# `harness.<адрес-через-дефисы>.sslip.io`. Вопрос владельцу оправдан лишь
# там, где ответ нельзя вывести (указание 13.09.2026). Своё имя он может
# назвать словом: «панель наружу my.example.com».
#
#   panel-naruzhu.sh [<имя>]   → поставить nginx, выпустить сертификат,
#                                записать адрес, собрать рубеж, перезапустить
#   panel-naruzhu.sh --снять   → очистить адрес и выключить рубеж
#   panel-naruzhu.sh --selftest
set -euo pipefail

SAM="$(readlink -f "${BASH_SOURCE[0]}")"
ZDES="$(dirname "$SAM")"
# shellcheck source=/dev/null
source "$ZDES/lib/konf.sh"

NGINX_DIR="${NGINX_DIR:-/etc/nginx}"
NGINX_BIN="${NGINX_BIN:-/usr/sbin/nginx}"
CERTBOT_BIN="${CERTBOT_BIN:-certbot}"
APT_BIN="${APT_BIN:-apt-get}"

skazhi() { printf '%s\n' "$*"; }
otkaz()  { printf '[панель наружу] ОТКАЗ — %s\n' "$1" >&2; exit 1; }

vneshnij_adres() {  # печатает IPv4 машины или пусто
    # Своим адресом машина знает себя вернее, чем внешняя служба: маршрут до
    # 1.1.1.1 не шлёт пакетов и работает без сети наружу.
    ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1
}

imya_po_adresu() {  # $1 = IPv4 → harness.<адрес-через-дефисы>.sslip.io
    local adres="$1"
    [ -n "$adres" ] || return 1
    printf 'harness.%s.sslip.io' "${adres//./-}"
}

# ── самотест ────────────────────────────────────────────────────────────────
if [ "${1:-}" = "--selftest" ]; then
    PROBY="$ZDES/test_panel_naruzhu.sh"
    [ -f "$PROBY" ] || { echo "нет проб: $PROBY" >&2; exit 1; }
    exec bash "$PROBY"
fi

konf_zagruzit
ROOT_SKRIPT="${PANEL_ROOT_SKRIPT:-/usr/local/lib/harness/panel-naruzhu-root.sh}"

# Права проверяются ДО первого изменения и ИМЕННО на нужную команду: `sudo -n
# true` зелёно и там, где нашей строки правил нет вовсе (ревью кода F-05).
sudo -n -l "$ROOT_SKRIPT" >/dev/null 2>&1 \
    || otkaz "нет права запускать $ROOT_SKRIPT — переустанови панель шагом установки"

if [ "${1:-}" = "--снять" ]; then
    sudo -n "$ROOT_SKRIPT" --снять
    exit $?
fi

IMYA="${1:-}"
if [ -z "$IMYA" ]; then
    ADRES="$(vneshnij_adres)"
    [ -n "$ADRES" ] || otkaz "не удалось узнать адрес машины — назови имя словом: «панель наружу my.example.com»"
    # Приватный адрес именем наружу не станет: sslip.io отдаст 10.0.0.5, и
    # Let's Encrypt до машины не дойдёт. За NAT (облака — всегда) отказ обязан
    # называть настоящую причину, а не «имя не указывает на эту машину».
    case "$ADRES" in
        10.*|127.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*|100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*)
            otkaz "адрес машины приватный ($ADRES) — назови внешнее имя словом: «панель наружу my.example.com»" ;;
    esac
    IMYA="$(imya_po_adresu "$ADRES")"
    skazhi "[панель наружу] имя выведено из адреса машины: $IMYA"
fi

sudo -n "$ROOT_SKRIPT" "$IMYA"
