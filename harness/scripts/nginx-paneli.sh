#!/usr/bin/env bash
# Собрать рубеж панели (конфиг nginx) из шаблона по паспорту установки.
#
# Зачем. Защита панели опирается на конфиг nginx в четырёх местах кода, а
# самого конфига не было ни в репозитории, ни в пакете: на чистой установке
# рубеж не возникал вовсе (ревизия безопасности 12.09.2026).
#
# Почему сборщик НЕ переписывает существующий файл. Живой конфиг собран не
# только из шаблона: certbot дописывает в него 443-блок и пути к сертификату.
# Перезапись шаблоном (без TLS) убила бы вход совсем — cookie пропуска идёт с
# флагом Secure, по http браузер его не сохранит, а звать certbot нам нельзя
# (домен и почта — выбор владельца). Поэтому: файла нет — пишем; файл есть —
# СВЕРЯЕМ и печатаем расхождения (ревью спеки 13.09.2026, S-01).
#
# Запуск:
#   bash scripts/nginx-paneli.sh             # собрать (или сверить) и включить
#   bash scripts/nginx-paneli.sh --показать  # напечатать, ничего не менять
#   bash scripts/nginx-paneli.sh --переписать # заменить существующий, с копией
#   bash scripts/nginx-paneli.sh --selftest
set -euo pipefail

SAM="$(readlink -f "${BASH_SOURCE[0]}")"
ZDES="$(dirname "$SAM")"
# shellcheck source=/dev/null
source "$ZDES/lib/konf.sh"

IMYA="harness-panel"
NGINX_DIR="${NGINX_DIR:-/etc/nginx}"
# Полным путём: PATH агента не содержит /usr/sbin, и поиск по имени объявил бы
# исправную машину машиной без nginx (ревью спеки S-02, S-12).
NGINX_BIN="${NGINX_BIN:-/usr/sbin/nginx}"
PANEL_UNIT="${PANEL_UNIT:-/etc/systemd/system/harness-panel.service}"
# Каталог выданных сертификатов — точкой подстановки, а не переключателем
# «пропустить проверку»: переключатель проба нажала бы, и проверка осталась бы
# непроверенной (ревью кода 13.09.2026, F-12).
LETSENCRYPT_DIR="${LETSENCRYPT_DIR:-/etc/letsencrypt/live}"

otkaz() { echo "[рубеж панели] ОТКАЗ — $1" >&2; exit 1; }

host_iz_url() {  # $1 = PANEL_URL → хост, либо отказ словом
    local url="$1"
    [ -n "$url" ] || otkaz "PANEL_URL пуст: панель наружу не выведена, рубеж собирать не из чего"
    case "$url" in
        https://*) : ;;
        *) otkaz "PANEL_URL не на https: cookie пропуска идёт с Secure, по http вход не сработает" ;;
    esac
    local host; host=$(printf '%s' "${url#https://}" | cut -d/ -f1)
    # Порт в PANEL_URL молча уехал бы и в server_name, и в путь к сертификату:
    # nginx такое имя не сопоставит никогда, а гейт судит хост БЕЗ порта —
    # два разбора одного значения разъезжаются молча (ревью кода F-09).
    case "$host" in
        *:*) otkaz "PANEL_URL с портом ($host) не поддержан: рубеж слушает 443" ;;
    esac
    printf '%s' "$host"
}

port_paneli() {  # порт РАБОТАЮЩЕЙ панели: установленный юнит, потом его файл
    local iz_systemd=""
    if command -v systemctl >/dev/null 2>&1; then
        iz_systemd=$(systemctl show harness-panel -p ExecStart 2>/dev/null \
                     | grep -o -- '--порт [0-9]\+' | grep -o '[0-9]\+' || true)
    fi
    if [ -n "$iz_systemd" ]; then printf '%s' "$iz_systemd"; return 0; fi
    local iz_fajla=""
    [ -f "$PANEL_UNIT" ] && iz_fajla=$(grep -o -- '--порт [0-9]\+' "$PANEL_UNIT" \
                                       | grep -o '[0-9]\+' | head -1 || true)
    [ -n "$iz_fajla" ] || otkaz "порт панели неоткуда взять: ни systemd, ни $PANEL_UNIT не называют «--порт N»"
    printf '%s' "$iz_fajla"
}

sobrat() {  # $1 = шаблон, $2 = хост, $3 = порт → печатает конфиг
    local shablon="$1" host="$2" port="$3"
    [ -f "$shablon" ] || otkaz "нет шаблона рубежа: $shablon"
    sed -e "s|@PANEL_HOST@|$host|g" -e "s|@PANEL_PORT@|$port|g" "$shablon"
}

proverit_konfig() {  # rc 0 — конфиг nginx цел; печатает причину сам
    if [ ! -x "$NGINX_BIN" ] && ! command -v "$NGINX_BIN" >/dev/null 2>&1; then
        echo "[рубеж панели] nginx не найден по пути $NGINX_BIN — проверять нечем" >&2
        return 2
    fi
    sudo -n "$NGINX_BIN" -t >/dev/null 2>&1
}

# ── самотест ────────────────────────────────────────────────────────────────
if [ "${1:-}" = "--selftest" ]; then
    PROBY="$ZDES/test_nginx_paneli.sh"
    [ -f "$PROBY" ] || { echo "нет проб: $PROBY" >&2; exit 1; }
    exec bash "$PROBY"
fi

konf_zagruzit
PROEKT="$(konf_iz_fajla PROJECT_DIR)"
SHABLON="$PROEKT/harness/panel/nginx-panel.conf.in"
[ -f "$SHABLON" ] || SHABLON="$ZDES/../harness/panel/nginx-panel.conf.in"

PANEL_URL_ZNACHENIE="$(konf_iz_fajla PANEL_URL)"
HOST="$(host_iz_url "$PANEL_URL_ZNACHENIE")"
PORT="$(port_paneli)"

# Разбор ключей ЯВНЫЙ: неизвестный аргумент проваливался в путь записи —
# `--pokazat` (латиницей, как все имена в харнесе) молча правил /etc/nginx и
# перезапускал nginx (ревью кода 13.09.2026, F-02).
case "${1:-}" in
    "") : ;;
    --показать) sobrat "$SHABLON" "$HOST" "$PORT"; exit 0 ;;
    --переписать) : ;;
    *) otkaz "неизвестный ключ: $1 (есть --показать, --переписать, --selftest)" ;;
esac

# Сертификат: шаблон обещал эту проверку в комментарии, а её не было —
# на чистой установке владелец получал «nginx -t покраснел» без причины (F-11).
# Проверка через sudo: /etc/letsencrypt/live закрыт правами 700 root, и под
# агентом обычный `[ -d ]` отвечает «нет» при живом сертификате — живой прогон
# 13.09.2026 отказал на исправной машине.
if ! sudo -n test -d "$LETSENCRYPT_DIR/$HOST" 2>/dev/null; then
    otkaz "нет сертификата для $HOST — выпусти его и повтори: sudo certbot certonly --nginx -d $HOST"
fi

DOSTUPNO="$NGINX_DIR/sites-available/$IMYA"
VKLYUCHENO="$NGINX_DIR/sites-enabled/$IMYA"
VREMENNYJ=$(mktemp); trap 'rm -f "$VREMENNYJ"' EXIT
sobrat "$SHABLON" "$HOST" "$PORT" > "$VREMENNYJ"

# Права проверяются ДО первой записи: полуобновлённое дерево хуже
# необновлённого (ревью спеки S-13).
sudo -n true 2>/dev/null || otkaz "нет прав sudo без пароля — в /etc/nginx ничего не тронуто"

if [ -f "$DOSTUPNO" ] && [ "${1:-}" != "--переписать" ]; then
    echo "[рубеж панели] конфиг уже есть: $DOSTUPNO — не переписываю (его дописывал certbot)"
    # Судья ОДИН — гейт: свой список опор в сборщике был бы четвёртой копией
    # правил и разошёлся бы молча (ревью кода 13.09.2026, F-17). Гейт судит
    # эффективную конфигурацию, а не файл, и различает блоки.
    if ! python3 "$ZDES/check-rubezh-paneli.py"; then
        otkaz "живой рубеж разошёлся с кодом — правь конфиг руками или зови --переписать"
    fi
    exit 0
fi

# Снимок ДО: конфиг мог быть красным из-за ЧУЖОГО сайта, и снимать свой
# симлинк за это нельзя (ревью спеки S-12).
BYLO_ZELENO=0
RC_PROVERKI=0
proverit_konfig && BYLO_ZELENO=1 || RC_PROVERKI=$?

# Проверить конфиг НЕЧЕМ — значит и включать нельзя: непроверенный рубеж
# подхватит первый же reload (certbot renew, ребут), и это будет не наш выбор.
if [ "$BYLO_ZELENO" = 0 ] && [ "$RC_PROVERKI" = 2 ]; then
    otkaz "проверить конфиг нечем ($NGINX_BIN) — в /etc/nginx ничего не тронуто"
fi

# ЧУЖОЙ носитель в sites-enabled: обычный файл, а не наша ссылка. Затереть его
# ln-ом значит потерять рабочий рубеж без копии — владелец теряет вход в
# панель, и восстановить нечего (ревью кода 13.09.2026, F-01, инвариант И-1).
if [ -e "$VKLYUCHENO" ] && [ ! -L "$VKLYUCHENO" ]; then
    KOPIYA_VKL="$VKLYUCHENO.$(date +%Y%m%d-%H%M).bak"
    sudo cp -p "$VKLYUCHENO" "$KOPIYA_VKL"
    otkaz "в sites-enabled лежит ФАЙЛ, а не ссылка: копия снята ($KOPIYA_VKL), разберись руками"
fi

if [ -f "$DOSTUPNO" ]; then
    KOPIYA="$DOSTUPNO.$(date +%Y%m%d-%H%M).bak"
    sudo cp -p "$DOSTUPNO" "$KOPIYA"
    echo "[рубеж панели] копия прежнего: $KOPIYA"
fi
SSYLKA_BYLA=0
[ -L "$VKLYUCHENO" ] && SSYLKA_BYLA=1
sudo install -m 644 "$VREMENNYJ" "$DOSTUPNO"
sudo ln -sfn "$DOSTUPNO" "$VKLYUCHENO"

if ! proverit_konfig; then
    if [ "$BYLO_ZELENO" = 1 ]; then
        # Снимаем ссылку, только если её поставили МЫ: чужую снять значит
        # выключить рабочий рубеж за чужую вину.
        [ "$SSYLKA_BYLA" = 0 ] && sudo rm -f "$VKLYUCHENO"
        otkaz "nginx -t покраснел на нашем конфиге — рубеж НЕ включён, $DOSTUPNO оставлен для разбора"
    fi
    [ "$SSYLKA_BYLA" = 0 ] && sudo rm -f "$VKLYUCHENO"
    otkaz "nginx -t был красным ЕЩЁ ДО нас (чужой сайт) — рубеж не включён, почини чужой конфиг и повтори"
fi

sudo systemctl reload nginx
echo "[рубеж панели] собран под $HOST → 127.0.0.1:$PORT и включён"
