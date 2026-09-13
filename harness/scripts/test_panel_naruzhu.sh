#!/usr/bin/env bash
# Пробы команды «панель наружу» (scripts/panel-naruzhu.sh).
#
# Стенд в mktemp: подставные sudo, nginx, certbot, apt-get, systemctl, ip и
# свой паспорт. Боевые /etc/nginx, /etc/letsencrypt и /etc/harness не
# читаются и не пишутся: проверка обязана создавать своё условие, а не играть
# на живой машине.
#
# Имена переменных ЛАТИНИЦЕЙ — bash кириллические не берёт.
set -uo pipefail

ZDES="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KOMANDA="$ZDES/panel-naruzhu.sh"

ok=1
putej=0
proba() {
    putej=$((putej + 1))
    if [ "$2" = "$3" ]; then printf '  ок    %s\n' "$1"
    else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$3"; ok=0; fi
}

stend() {  # $1 = каталог, $2 = внешний адрес машины
    local T="$1" adres="$2"
    mkdir -p "$T/bin" "$T/root-skript" "$T/nginx/sites-enabled" "$T/letsencrypt/live"
    printf 'PROJECT_DIR="%s/proekt"\n' "$T" > "$T/install.conf"
    printf 'PANEL_URL=""\n' > "$T/harness.conf"
    # Подставной root-скрипт: обёртка обязана звать ЕГО, а не делать всё сама.
    cat > "$T/root-skript/panel-naruzhu-root.sh" <<ROOTS
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$T/root-zvan"
exit \${ROOT_RC:-0}
ROOTS
    chmod +x "$T/root-skript/panel-naruzhu-root.sh"
    cat > "$T/bin/sudo" <<SUDO
#!/usr/bin/env bash
if [ "\${1:-}" = "-n" ] && [ "\${2:-}" = "-l" ]; then
    [ -e "$T/bez-prav" ] && exit 1
    exit 0
fi
[ "\${1:-}" = "-n" ] && shift
exec "\$@"
SUDO
    cat > "$T/bin/ip" <<IPCMD
#!/usr/bin/env bash
[ "\${1:-}" = "route" ] && echo "1.1.1.1 via 10.0.0.1 dev eth0 src $adres uid 0"
exit 0
IPCMD
    chmod +x "$T/bin/"*
}

zvat() {  # $1 = каталог, дальше аргументы
    local T="$1"; shift
    PATH="$T/bin:/usr/bin:/bin" \
    HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
    PANEL_ROOT_SKRIPT="$T/root-skript/panel-naruzhu-root.sh" \
        bash "$KOMANDA" "$@" 2>&1
}

# ── 1. Имя выводится из адреса машины и уходит root-скрипту ───────────────
T=$(mktemp -d); stend "$T" "203.0.113.7"
VYVOD=$(zvat "$T"); RC=$?
proba "имя выведено из адреса машины — rc 0" "0" "$RC"
proba "root-скрипт позван с этим именем" "да" \
      "$(grep -q 'harness.203-0-113-7.sslip.io' "$T/root-zvan" && echo да || echo нет)"
rm -rf "$T"

# ── 2. Своё имя словом — берётся оно ──────────────────────────────────────
T=$(mktemp -d); stend "$T" "203.0.113.7"
VYVOD=$(zvat "$T" "my.example.com"); RC=$?
proba "своё имя — rc 0" "0" "$RC"
proba "root-скрипт позван именно с ним" "да" \
      "$(grep -qx 'my.example.com' "$T/root-zvan" && echo да || echo нет)"
rm -rf "$T"

# ── 3. БОЛЬНОЙ: адрес машины приватный (облака за NAT — всегда) ───────────
for chastnyj in 10.0.0.5 172.31.5.7 192.168.1.10 100.64.0.3; do
    T=$(mktemp -d); stend "$T" "$chastnyj"
    VYVOD=$(zvat "$T"); RC=$?
    proba "БОЛЬНОЙ: приватный адрес $chastnyj — отказ" "1" "$RC"
    proba "БОЛЬНОЙ: $chastnyj — сказано назвать имя словом" "да" \
          "$(printf '%s' "$VYVOD" | grep -q 'назови внешнее имя' && echo да || echo нет)"
    proba "БОЛЬНОЙ: $chastnyj — root-скрипт НЕ звали" "0" \
          "$(find "$T" -maxdepth 1 -name root-zvan | wc -l)"
    rm -rf "$T"
done

# ── 4. БОЛЬНОЙ: адреса не узнать и имя не названо ─────────────────────────
T=$(mktemp -d); stend "$T" ""
VYVOD=$(zvat "$T"); RC=$?
proba "БОЛЬНОЙ: адреса нет — отказ" "1" "$RC"
proba "БОЛЬНОЙ: сказано, что имя можно назвать словом" "да" \
      "$(printf '%s' "$VYVOD" | grep -q 'назови имя словом' && echo да || echo нет)"
rm -rf "$T"

# ── 5. БОЛЬНОЙ: права на root-скрипт не выданы ────────────────────────────
# `sudo -n true` зелено и там, где нашей строки правил нет вовсе.
T=$(mktemp -d); stend "$T" "203.0.113.7"; touch "$T/bez-prav"
VYVOD=$(zvat "$T"); RC=$?
proba "БОЛЬНОЙ: нет права на root-скрипт — отказ" "1" "$RC"
proba "БОЛЬНОЙ: названа переустановка панели" "да" \
      "$(printf '%s' "$VYVOD" | grep -q 'переустанови панель' && echo да || echo нет)"
proba "БОЛЬНОЙ: root-скрипт не звали" "0" \
      "$(find "$T" -maxdepth 1 -name root-zvan | wc -l)"
rm -rf "$T"

# ── 6. Отказ root-скрипта возвращается наружу, а не глотается ─────────────
T=$(mktemp -d); stend "$T" "203.0.113.7"
printf '#!/usr/bin/env bash\nexit 3\n' > "$T/root-skript/panel-naruzhu-root.sh"
chmod +x "$T/root-skript/panel-naruzhu-root.sh"
VYVOD=$(zvat "$T"); RC=$?
proba "БОЛЬНОЙ: root-скрипт отказал — код возврата не ноль" "3" "$RC"
rm -rf "$T"

# ── 7. Снятие уходит root-скрипту ────────────────────────────────────────
T=$(mktemp -d); stend "$T" "203.0.113.7"
VYVOD=$(zvat "$T" --снять); RC=$?
proba "снятие — rc 0" "0" "$RC"
proba "root-скрипт позван со «--снять»" "да" \
      "$(grep -qx -- '--снять' "$T/root-zvan" && echo да || echo нет)"
rm -rf "$T"

if [ "$ok" = 1 ]; then
    echo "SELFTEST: зелёный ($putej путей; больные: нет адреса, нет сертификата, рубеж не собран)"
    exit 0
fi
echo "SELFTEST: КРАСНЫЙ (путей $putej)"
exit 1
