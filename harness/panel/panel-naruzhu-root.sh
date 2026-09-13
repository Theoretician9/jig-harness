#!/usr/bin/env bash
# Вывод панели наружу — ЕДИНСТВЕННОЕ действие, которое агент делает от root.
#
# Почему отдельный файл вне дерева проекта. Ревью кода 13.09.2026 (F-01, F-02,
# F-03) показало, что «узкие» строки sudoers узкими не были: `install -m 644
# /tmp/* …` принимает в аргументах любые опции (man 5 sudoers: в аргументах «*»
# берёт и слэши), `certbot … *` пускает `--pre-hook` с произвольной командой, а
# право запускать от root файл из каталога, куда агент пишет, равно NOPASSWD:
# ALL. Поэтому прав ровно одно: запустить ЭТОТ скрипт. Он лежит в
# /usr/local/lib/harness (root:root 0755), агент туда не пишет, а внутри
# проверяет единственный аргумент — имя хоста.
#
#   panel-naruzhu-root.sh <имя-хоста>   → nginx, сертификат, адрес, рубеж
#   panel-naruzhu-root.sh --снять       → адрес пуст, рубеж выключен
set -euo pipefail

ZDES="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
SHABLON="${PANEL_NGINX_SHABLON:-$ZDES/nginx-panel.conf.in}"
HARNESS_CONF="${HARNESS_CONF:-/etc/harness/harness.conf}"
INSTALL_CONF="${INSTALL_CONF:-/etc/harness/install.conf}"
NGINX_DIR="${NGINX_DIR:-/etc/nginx}"
NGINX_BIN="${NGINX_BIN:-/usr/sbin/nginx}"
LE_DIR="${LE_DIR:-/etc/letsencrypt}"
APT_BIN="${APT_BIN:-apt-get}"
CERTBOT_BIN="${CERTBOT_BIN:-certbot}"
LOG_DIR="${LOG_DIR:-/var/log/harness}"

otkaz() { printf '[панель наружу] ОТКАЗ — %s\n' "$1" >&2; exit 1; }

imya_godno() {  # $1 = имя; только буквы, цифры, дефисы и точки
    printf '%s' "$1" | grep -qE '^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)+$'
}

zapisat_adres() {  # $1 = значение PANEL_URL (может быть пустым)
    local znachenie="$1" kopii="$LOG_DIR/панель-копии" metka
    metka=$(date +%Y%m%d-%H%M%S)
    mkdir -p "$kopii"
    cp -p "$HARNESS_CONF" "$kopii/harness.conf.$metka"
    if grep -qE '^\s*PANEL_URL=' "$HARNESS_CONF"; then
        sed -i "s|^\s*PANEL_URL=.*|PANEL_URL=\"$znachenie\"|" "$HARNESS_CONF"
    else
        printf 'PANEL_URL="%s"\n' "$znachenie" >> "$HARNESS_CONF"
    fi
    printf '{"ts":"%s","ключ":"PANEL_URL","стало":"%s","кто":"%s"}\n' \
        "$(date -Is)" "$znachenie" "${SUDO_USER:-root}" >> "$LOG_DIR/панель.jsonl"
}

sobrat_rubezh() {  # $1 = имя хоста
    local imya="$1" port vremennyj
    port=$(systemctl show harness-panel -p ExecStart 2>/dev/null \
           | grep -o -- '--порт [0-9]\+' | grep -o '[0-9]\+' | head -1)
    [ -n "$port" ] || otkaz "порт панели неоткуда взять: служба не описана в systemd"
    [ -f "$SHABLON" ] || otkaz "нет образца правил: $SHABLON"
    # Временный файл — В ЦЕЛЕВОМ каталоге, а не в /tmp: предсказуемый путь в
    # общем /tmp позволял бы подложить симлинк и получить перезапись чужого
    # файла от root (ревью кода 13.09.2026, F-11).
    vremennyj=$(mktemp "$NGINX_DIR/sites-available/.panel.XXXXXX")
    chmod 0600 "$vremennyj"
    sed -e "s|@PANEL_HOST@|$imya|g" -e "s|@PANEL_PORT@|$port|g" "$SHABLON" > "$vremennyj"
    # Существующий конфиг НЕ переписываем вслепую: его дописывал certbot, и
    # перезапись отняла бы у владельца вход (ревью рубежа 13.09.2026, F-01).
    if [ -f "$NGINX_DIR/sites-available/harness-panel" ]; then
        cp -p "$NGINX_DIR/sites-available/harness-panel" \
              "$NGINX_DIR/sites-available/harness-panel.$(date +%Y%m%d-%H%M).bak"
    fi
    install -m 644 "$vremennyj" "$NGINX_DIR/sites-available/harness-panel"
    rm -f "$vremennyj"
    ln -sfn "$NGINX_DIR/sites-available/harness-panel" \
            "$NGINX_DIR/sites-enabled/harness-panel"
    if ! "$NGINX_BIN" -t >/dev/null 2>&1; then
        rm -f "$NGINX_DIR/sites-enabled/harness-panel"
        otkaz "nginx -t покраснел на нашем конфиге — рубеж НЕ включён"
    fi
    systemctl reload nginx
}

# Проверяется ВОЗМОЖНОСТЬ писать, а не uid: под обычным пользователем скрипт
# упал бы на середине (полуработа хуже её отсутствия), а проба на подставных
# каталогах обязана идти тем же путём, что боевой запуск.
[ -w "$HARNESS_CONF" ] || otkaz "нет права писать $HARNESS_CONF — скрипт запускают через sudo одной строкой правил"
[ -w "$NGINX_DIR/sites-available" ] || otkaz "нет права писать $NGINX_DIR/sites-available — скрипт запускают через sudo"

if [ "${1:-}" = "--снять" ]; then
    zapisat_adres ""
    if [ -L "$NGINX_DIR/sites-enabled/harness-panel" ]; then
        rm -f "$NGINX_DIR/sites-enabled/harness-panel"
        systemctl reload nginx 2>/dev/null || true
        printf '[панель наружу] рубеж выключен\n'
    elif [ -e "$NGINX_DIR/sites-enabled/harness-panel" ]; then
        # Посторонний файл вместо нашей ссылки: снять его — значит выключить
        # чужой рубеж без спроса. Говорим прямо, а не обещаем несделанное.
        printf '[панель наружу] в sites-enabled лежит ФАЙЛ, а не наша ссылка — не трогаю его\n'
    fi
    systemctl restart harness-panel 2>/dev/null || true
    printf '[панель наружу] адрес снят\n'
    exit 0
fi

IMYA="${1:-}"
imya_godno "$IMYA" || otkaz "«$IMYA» не похоже на имя хоста: буквы, цифры, дефисы и точки, без схемы, порта и пути"

command -v "$NGINX_BIN" >/dev/null 2>&1 || [ -x "$NGINX_BIN" ] || {
    printf '[панель наружу] ставлю nginx\n'
    "$APT_BIN" -o DPkg::Lock::Timeout=120 install -y nginx >/dev/null 2>&1 \
        || otkaz "nginx не поставился"
}
command -v "$CERTBOT_BIN" >/dev/null 2>&1 || {
    printf '[панель наружу] ставлю certbot\n'
    "$APT_BIN" -o DPkg::Lock::Timeout=120 install -y certbot python3-certbot-nginx >/dev/null 2>&1 \
        || otkaz "certbot не поставился — сертификат выпустить нечем"
}

if [ ! -d "$LE_DIR/live/$IMYA" ]; then
    printf '[панель наружу] выпускаю сертификат для %s\n' "$IMYA"
    # Аргументы фиксированы здесь, а не приходят снаружи: через аргументы
    # certbot принимает --pre-hook с произвольной командой (ревью F-02).
    "$CERTBOT_BIN" certonly --nginx --non-interactive --agree-tos \
        --register-unsafely-without-email -d "$IMYA" >/dev/null 2>&1 \
        || otkaz "сертификат для $IMYA не выпущен: имя должно указывать на эту машину"
fi

zapisat_adres "https://$IMYA"
sobrat_rubezh "$IMYA"
# Служба читает адрес и chat_id один раз при первом запросе.
systemctl restart harness-panel
printf '[панель наружу] готово: https://%s — попроси ссылку командой «панель»\n' "$IMYA"
