#!/usr/bin/env bash
# Пробы root-скрипта вывода панели (harness/panel/panel-naruzhu-root.sh).
#
# Это ЕДИНСТВЕННОЕ, что агент делает от root, поэтому больные случаи здесь —
# про аргумент: имя проверяется скриптом, а не строкой sudoers (ревью кода
# 13.09.2026, F-01/F-02: «узкие» строки правил узкими не были).
#
# Стенд в mktemp: подставные apt-get, certbot, systemctl, nginx и свои
# каталоги. Боевые /etc/nginx и /etc/letsencrypt не читаются.
set -uo pipefail

ZDES="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKRIPT="$ZDES/../harness/panel/panel-naruzhu-root.sh"
[ -f "$SKRIPT" ] || SKRIPT="$ZDES/../panel/panel-naruzhu-root.sh"
IMYA_STENDA="panel.stend.example"

ok=1
putej=0
proba() {
    putej=$((putej + 1))
    if [ "$2" = "$3" ]; then printf '  ок    %s\n' "$1"
    else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$3"; ok=0; fi
}

stend() {  # $1 = каталог
    local T="$1"
    mkdir -p "$T/bin" "$T/nginx/sites-available" "$T/nginx/sites-enabled" \
             "$T/letsencrypt/live" "$T/log"
    printf 'PANEL_URL=""\nPUSH_NEEDS_OWNER=0\n' > "$T/harness.conf"
    cp "$ZDES/../harness/panel/nginx-panel.conf.in" "$T/shablon.in" 2>/dev/null \
        || cp "$ZDES/../panel/nginx-panel.conf.in" "$T/shablon.in"
    printf '#!/usr/bin/env bash\ntouch "%s/apt-zvan"\nexit 0\n' "$T" > "$T/bin/apt-get"
    cat > "$T/bin/certbot" <<CB
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$T/certbot-zvan"
mkdir -p "$T/letsencrypt/live/$IMYA_STENDA"
exit \${CERTBOT_RC:-0}
CB
    printf '#!/usr/bin/env bash\nif [ "$1" = "show" ]; then echo "ExecStart={ argv[]=server.py --порт 8787 ; }"; fi\necho "$@" >> "%s/systemctl-zvan"\nexit 0\n' "$T" > "$T/bin/systemctl"
    printf '#!/usr/bin/env bash\nexit ${NGINX_T_RC:-0}\n' > "$T/bin/nginx"
    chmod +x "$T/bin/"*
}

zvat() {  # $1 = каталог, дальше аргументы
    local T="$1"; shift
    PATH="$T/bin:/usr/bin:/bin" HARNESS_CONF="$T/harness.conf" \
    PANEL_NGINX_SHABLON="$T/shablon.in" NGINX_DIR="$T/nginx" \
    NGINX_BIN="$T/bin/nginx" LE_DIR="$T/letsencrypt" APT_BIN="$T/bin/apt-get" \
    CERTBOT_BIN="$T/bin/certbot" LOG_DIR="$T/log" \
        bash "$SKRIPT" "$@" 2>&1
}

# ── 1. БОЛЬНЫЕ: аргумент, который строка sudoers пропустила бы ────────────
# Ровно эти формы и делали прежние правила равными NOPASSWD: ALL.
for zloj in "--pre-hook" "-d x --pre-hook id" "../../etc/passwd" \
            "panel.example.com;id" "panel.example.com --config /tmp/x" \
            "" "PANEL.example.com" "panel.example.com:8443"; do
    T=$(mktemp -d); stend "$T"
    VYVOD=$(zvat "$T" "$zloj"); RC=$?
    proba "БОЛЬНОЙ: аргумент «${zloj:-(пусто)}» — отказ" "1" "$RC"
    proba "БОЛЬНОЙ: «${zloj:-(пусто)}» — certbot не звали" "0" \
          "$(find "$T" -maxdepth 1 -name certbot-zvan | wc -l)"
    rm -rf "$T"
done

# ── 2. Здоровый путь: сертификат, адрес, рубеж, перезапуск ───────────────
T=$(mktemp -d); stend "$T"
VYVOD=$(zvat "$T" "$IMYA_STENDA"); RC=$?
proba "здоровое имя — rc 0" "0" "$RC"
proba "сертификат выпущен фиксированными аргументами" "да" \
      "$(grep -q -- '--register-unsafely-without-email' "$T/certbot-zvan" && echo да || echo нет)"
proba "адрес записан" "да" \
      "$(grep -q 'PANEL_URL="https://panel.stend.example"' "$T/harness.conf" && echo да || echo нет)"
proba "соседний ключ не тронут" "да" \
      "$(grep -q 'PUSH_NEEDS_OWNER=0' "$T/harness.conf" && echo да || echo нет)"
proba "копия конфига снята до правки" "1" \
      "$(find "$T/log/панель-копии" -name 'harness.conf.*' | wc -l)"
proba "рубеж собран и включён" "1" \
      "$(find "$T/nginx/sites-enabled" -name harness-panel | wc -l)"
proba "плейсхолдеров в собранном нет" "0" \
      "$(grep -c '@PANEL_' "$T/nginx/sites-available/harness-panel" || true)"
proba "панель перезапущена" "да" \
      "$(grep -q 'restart harness-panel' "$T/systemctl-zvan" && echo да || echo нет)"
rm -rf "$T"

# ── 3. Сертификат уже есть — certbot не зовётся (квота Let's Encrypt) ────
T=$(mktemp -d); stend "$T"; mkdir -p "$T/letsencrypt/live/$IMYA_STENDA"
zvat "$T" "$IMYA_STENDA" >/dev/null
proba "сертификат на месте — повторного выпуска нет" "0" \
      "$(find "$T" -maxdepth 1 -name certbot-zvan | wc -l)"
rm -rf "$T"

# ── 4. БОЛЬНОЙ: nginx -t красный — рубеж НЕ включён ──────────────────────
T=$(mktemp -d); stend "$T"
printf '#!/usr/bin/env bash\nexit 1\n' > "$T/bin/nginx"; chmod +x "$T/bin/nginx"
VYVOD=$(zvat "$T" "$IMYA_STENDA"); RC=$?
proba "БОЛЬНОЙ: nginx -t красный — отказ" "1" "$RC"
proba "БОЛЬНОЙ: симлинк снят" "0" \
      "$(find "$T/nginx/sites-enabled" -name harness-panel | wc -l)"
rm -rf "$T"

# ── 5. БОЛЬНОЙ: сертификат не выпустился — адрес НЕ записан ──────────────
T=$(mktemp -d); stend "$T"
printf '#!/usr/bin/env bash\nexit 1\n' > "$T/bin/certbot"; chmod +x "$T/bin/certbot"
VYVOD=$(zvat "$T" "$IMYA_STENDA"); RC=$?
proba "БОЛЬНОЙ: сертификат не выпущен — отказ" "1" "$RC"
proba "БОЛЬНОЙ: адрес не записан" "да" \
      "$(grep -q 'PANEL_URL=""' "$T/harness.conf" && echo да || echo нет)"
rm -rf "$T"

# ── 6. Снятие: адрес пуст, ссылка снята ──────────────────────────────────
T=$(mktemp -d); stend "$T"
zvat "$T" "$IMYA_STENDA" >/dev/null
VYVOD=$(zvat "$T" --снять); RC=$?
proba "снятие — rc 0" "0" "$RC"
proba "адрес пуст" "да" "$(grep -q 'PANEL_URL=""' "$T/harness.conf" && echo да || echo нет)"
proba "ссылка снята" "0" "$(find "$T/nginx/sites-enabled" -name harness-panel | wc -l)"
rm -rf "$T"

# ── 7. БОЛЬНОЙ: в sites-enabled ФАЙЛ, а не наша ссылка ───────────────────
# Обещать «вход больше не работает», не выключив рубеж, — ложь владельцу.
T=$(mktemp -d); stend "$T"
printf 'server { listen 443 ssl; }\n' > "$T/nginx/sites-enabled/harness-panel"
VYVOD=$(zvat "$T" --снять); RC=$?
proba "БОЛЬНОЙ: посторонний файл — сказано прямо" "да" \
      "$(printf '%s' "$VYVOD" | grep -q 'не трогаю его' && echo да || echo нет)"
proba "БОЛЬНОЙ: посторонний файл цел" "1" \
      "$(find "$T/nginx/sites-enabled" -maxdepth 1 -name harness-panel -type f | wc -l)"
rm -rf "$T"

if [ "$ok" = 1 ]; then
    echo "SELFTEST: зелёный ($putej путей; больные первыми — аргументы, которыми прежние правила sudoers давали root)"
    exit 0
fi
echo "SELFTEST: КРАСНЫЙ (путей $putej)"
exit 1
