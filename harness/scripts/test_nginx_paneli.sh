#!/usr/bin/env bash
# Пробы сборщика рубежа `scripts/nginx-paneli.sh`.
#
# Каждая проба строит СВОЙ стенд в mktemp: дерево nginx, свой паспорт, свой
# юнит и подставные nginx/sudo/systemctl в PATH стенда. Боевые /etc/nginx и
# /etc/harness не читаются и не пишутся ни одной пробой — иначе проба играет
# на боевом (запись памяти «проба-играет-на-боевом»).
#
# Ожидание «код не ноль» здесь недостаточно: 127 «команды нет» зеленит такую
# пробу впустую. Каждый красный путь требует ПРИЧИНУ — слово из собственного
# сообщения сборщика.
#
# Имена переменных ЛАТИНИЦЕЙ: bash кириллические не берёт и печатает в ошибку
# само значение.
set -uo pipefail

ZDES="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SBORSHCHIK="$ZDES/nginx-paneli.sh"
HOST_STENDA="panel.stend.example"
PORT_STENDA=8787

ok=1
putej=0
proba() {  # $1 = имя, $2 = ждём, $3 = вышло
    putej=$((putej + 1))
    if [ "$2" = "$3" ]; then printf '  ок    %s\n' "$1"
    else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$3"; ok=0; fi
}

stend() {  # $1 = каталог, $2 = PANEL_URL, $3 = порт юнита; печатает пути
    local T="$1" url="$2" port="$3"
    mkdir -p "$T/nginx/sites-available" "$T/nginx/sites-enabled" "$T/bin" "$T/proekt/harness/panel"
    cp "$ZDES/../harness/panel/nginx-panel.conf.in" "$T/proekt/harness/panel/"
    printf 'PROJECT_DIR="%s/proekt"\n' "$T" > "$T/install.conf"
    printf 'PANEL_URL="%s"\n' "$url" > "$T/harness.conf"
    # В ФАЙЛЕ юнита нарочно другой порт: работает УСТАНОВЛЕННЫЙ юнит, и проба
    # обязана доказать приоритет systemd, а не совпадение двух источников.
    printf '[Service]\nExecStart=/usr/bin/python3 %s/proekt/harness/panel/server.py --порт 7777\n' \
        "$T" > "$T/unit.service"
    # Подставной nginx: краснеет, только если включён НАШ конфиг (так стенд
    # моделирует «конфиг был зелёным до нас» и «покраснел из-за нас»).
    # Подставной nginx: краснеет, только если включён НАШ конфиг (так стенд
    # моделирует «было зелено до нас»), и печатает эффективную конфигурацию по
    # -T — её судит гейт, которого зовёт сборщик.
    cat > "$T/bin/nginx" <<NGINX
#!/usr/bin/env bash
if [ -e "$T/nginx/sites-enabled/harness-panel" ] && [ -e "$T/krasnet" ]; then
    echo "nginx: [emerg] test failed" >&2; exit 1
fi
if [ "\${1:-}" = "-T" ]; then
    for f in "$T"/nginx/sites-enabled/*; do
        [ -e "\$f" ] || continue
        echo "# configuration file \$f:"
        cat "\$f"
        echo
    done
fi
exit 0
NGINX
    # Подставной sudo: боевой машины не касается, install/ln/rm делает сам.
    cat > "$T/bin/sudo" <<'SUDO'
#!/usr/bin/env bash
[ "${1:-}" = "-n" ] && shift
[ "${1:-}" = "true" ] && exit 0
exec "$@"
SUDO
    # Подставной systemctl: источник порта — УСТАНОВЛЕННЫЙ юнит.
    cat > "$T/bin/systemctl" <<SYSCTL
#!/usr/bin/env bash
if [ "\${1:-}" = "show" ]; then
    echo "ExecStart={ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 server.py --порт $port ; }"
    exit 0
fi
exit 0
SYSCTL
    chmod +x "$T/bin/nginx" "$T/bin/sudo" "$T/bin/systemctl"
    # Сертификат стенда: каталог, а не переключатель «пропустить проверку».
    mkdir -p "$T/letsencrypt/$HOST_STENDA"
}

zvat() {  # $1 = каталог стенда, дальше — аргументы сборщика; печатает вывод
    local T="$1"; shift
    PATH="$T/bin:/usr/bin:/bin" \
    HARNESS_INSTALL_CONF="$T/install.conf" HARNESS_CONF="$T/harness.conf" \
    NGINX_DIR="$T/nginx" NGINX_BIN="$T/bin/nginx" PANEL_UNIT="$T/unit.service" \
    LETSENCRYPT_DIR="$T/letsencrypt" \
        bash "$SBORSHCHIK" "$@" 2>&1
}

# ── 1. PANEL_URL пуст — отказ словом, ничего не тронуто ────────────────────
T=$(mktemp -d); stend "$T" "" "$PORT_STENDA"
VYVOD=$(zvat "$T"); RC=$?
proba "PANEL_URL пуст — rc" "1" "$RC"
proba "PANEL_URL пуст — причина словом" "да" \
      "$(printf '%s' "$VYVOD" | grep -q 'PANEL_URL пуст' && echo да || echo нет)"
proba "PANEL_URL пуст — в sites-available ничего не создано" "0" \
      "$(find "$T/nginx/sites-available" -type f | wc -l)"
rm -rf "$T"

# ── 2. PANEL_URL не https: cookie пропуска идёт с Secure ───────────────────
T=$(mktemp -d); stend "$T" "http://$HOST_STENDA" "$PORT_STENDA"
VYVOD=$(zvat "$T"); RC=$?
proba "PANEL_URL не https — rc" "1" "$RC"
proba "PANEL_URL не https — причина словом" "да" \
      "$(printf '%s' "$VYVOD" | grep -q 'не на https' && echo да || echo нет)"
rm -rf "$T"

# ── 3. Сборка: плейсхолдеров не осталось, хост и порт подставлены ──────────
T=$(mktemp -d); stend "$T" "https://$HOST_STENDA" "$PORT_STENDA"
VYVOD=$(zvat "$T" --показать)
proba "плейсхолдеров в выводе не осталось" "0" \
      "$(printf '%s' "$VYVOD" | grep -c '@PANEL_' || true)"
proba "хост подставлен" "да" \
      "$(printf '%s' "$VYVOD" | grep -q "server_name $HOST_STENDA;" && echo да || echo нет)"
proba "порт подставлен" "да" \
      "$(printf '%s' "$VYVOD" | grep -q "proxy_pass http://127.0.0.1:$PORT_STENDA;" && echo да || echo нет)"
rm -rf "$T"

# ── 4. Порт берётся из УСТАНОВЛЕННОГО юнита, а не из шаблона ───────────────
# Работает то, что стоит: юнит могли поставить раньше или править руками.
T=$(mktemp -d); stend "$T" "https://$HOST_STENDA" 9191
VYVOD=$(zvat "$T" --показать)
proba "порт взят из установленного юнита (9191)" "да" \
      "$(printf '%s' "$VYVOD" | grep -q 'proxy_pass http://127.0.0.1:9191;' && echo да || echo нет)"
rm -rf "$T"

# ── 5. Существующий конфиг НЕ переписывается (улика: certbot дописал TLS) ──
T=$(mktemp -d); stend "$T" "https://$HOST_STENDA" "$PORT_STENDA"
ZHIVOJ="$T/nginx/sites-available/harness-panel"
# Заглушка — отдельным файлом, как на живой машине (000-default-ssl).
printf 'server {\n    listen 443 ssl default_server;\n    server_name _;\n    return 444;\n}\n' \
    > "$T/nginx/sites-available/000-default-ssl"
ln -sfn "$T/nginx/sites-available/000-default-ssl" "$T/nginx/sites-enabled/000-default-ssl"
cat > "$ZHIVOJ" <<ZHIV
server {
    listen 443 ssl;   # managed by Certbot
    server_name $HOST_STENDA;
    location /vnutr/ { return 404; }
    location / {
        proxy_pass http://127.0.0.1:$PORT_STENDA;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
ZHIV
ln -sfn "$ZHIVOJ" "$T/nginx/sites-enabled/harness-panel"
SUMMA_DO=$(md5sum "$ZHIVOJ" | cut -d' ' -f1)
VYVOD=$(zvat "$T"); RC=$?
SUMMA_POSLE=$(md5sum "$ZHIVOJ" | cut -d' ' -f1)
proba "живой конфиг с опорами — rc 0" "0" "$RC"
proba "живой конфиг НЕ переписан (md5 совпал)" "$SUMMA_DO" "$SUMMA_POSLE"
proba "сказано, что не переписываю" "да" \
      "$(printf '%s' "$VYVOD" | grep -q 'не переписываю' && echo да || echo нет)"
rm -rf "$T"

# ── 5б. Порт из файла юнита — запасной источник, когда systemd молчит ─────
T=$(mktemp -d); stend "$T" "https://$HOST_STENDA" "$PORT_STENDA"
# Молчащий systemctl, а НЕ удалённый: удаление открыло бы путь к боевому
# /usr/bin/systemctl, и проба взяла бы порт живой панели этой машины.
printf '#!/usr/bin/env bash\nexit 0\n' > "$T/bin/systemctl"; chmod +x "$T/bin/systemctl"
VYVOD=$(zvat "$T" --показать)
proba "systemd молчит — порт берётся из файла юнита (7777)" "да" \
      "$(printf '%s' "$VYVOD" | grep -q 'proxy_pass http://127.0.0.1:7777;' && echo да || echo нет)"
rm -rf "$T"

# ── 6. Живой конфиг без опоры: расхождение названо, файл цел ───────────────
T=$(mktemp -d); stend "$T" "https://$HOST_STENDA" "$PORT_STENDA"
ZHIVOJ="$T/nginx/sites-available/harness-panel"
cat > "$ZHIVOJ" <<ZHIV
server {
    listen 443 ssl;
    server_name $HOST_STENDA;
    location / {
        proxy_pass http://127.0.0.1:$PORT_STENDA;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
ZHIV
ln -sfn "$ZHIVOJ" "$T/nginx/sites-enabled/harness-panel"
SUMMA_DO=$(md5sum "$ZHIVOJ" | cut -d' ' -f1)
VYVOD=$(zvat "$T"); RC=$?
SUMMA_POSLE=$(md5sum "$ZHIVOJ" | cut -d' ' -f1)
proba "БОЛЬНОЙ: живой конфиг без /vnutr/ и заглушки — rc" "1" "$RC"
proba "БОЛЬНОЙ: названа недостающая опора" "да" \
      "$(printf '%s' "$VYVOD" | grep -q '/vnutr/' && echo да || echo нет)"
proba "БОЛЬНОЙ: файл всё равно не тронут" "$SUMMA_DO" "$SUMMA_POSLE"
rm -rf "$T"

# ── 7. nginx -t покраснел ИЗ-ЗА НАС: симлинк снят, отказ словом ────────────
T=$(mktemp -d); stend "$T" "https://$HOST_STENDA" "$PORT_STENDA"
touch "$T/krasnet"
VYVOD=$(zvat "$T"); RC=$?
proba "БОЛЬНОЙ: nginx -t красный из-за нас — rc" "1" "$RC"
proba "БОЛЬНОЙ: симлинк снят" "0" \
      "$(find "$T/nginx/sites-enabled" -maxdepth 1 -name harness-panel | wc -l)"
proba "БОЛЬНОЙ: причина словом" "да" \
      "$(printf '%s' "$VYVOD" | grep -q 'НЕ включён' && echo да || echo нет)"
rm -rf "$T"

# ── 8. Бинаря nginx нет: отказ ДРУГИМИ словами, чем «конфиг плох» ──────────
T=$(mktemp -d); stend "$T" "https://$HOST_STENDA" "$PORT_STENDA"
rm -f "$T/bin/nginx"
VYVOD=$(zvat "$T"); RC=$?
proba "бинаря нет — сказано «не найден», а не «конфиг плох»" "да" \
      "$(printf '%s' "$VYVOD" | grep -q 'nginx не найден' && echo да || echo нет)"
proba "бинаря нет — про красный конфиг не сказано" "нет" \
      "$(printf '%s' "$VYVOD" | grep -q 'покраснел' && echo да || echo нет)"
rm -rf "$T"

# ── 9. Здоровый путь: конфига не было — собран и включён ───────────────────
T=$(mktemp -d); stend "$T" "https://$HOST_STENDA" "$PORT_STENDA"
VYVOD=$(zvat "$T"); RC=$?
proba "конфига не было — rc 0" "0" "$RC"
proba "конфиг создан" "1" "$(find "$T/nginx/sites-available" -name harness-panel | wc -l)"
proba "симлинк включён" "1" "$(find "$T/nginx/sites-enabled" -name harness-panel | wc -l)"
proba "в собранном есть заглушка default_server" "да" \
      "$(grep -q 'default_server' "$T/nginx/sites-available/harness-panel" && echo да || echo нет)"
rm -rf "$T"


# ── 10. Каждая опора sverit — своим больным стендом ───────────────────────
# Ревью кода 13.09.2026 (F-12): из шести проверок sverit пробой держалась
# ОДНА, остальные можно было удалить при зелёном самотесте.
zhivoj_konfig() {  # $1 = каталог, $2..$n = что ИСКЛЮЧИТЬ (по слову)
    local T="$1"; shift
    local bez="$*"
    {
        case "$bez" in *zaglushka*) : ;; *)
            printf 'server {\n    listen 443 ssl default_server;\n    server_name _;\n    return 444;\n}\n' ;;
        esac
        printf 'server {\n'
        case "$bez" in *listen443*) printf '    listen 80;\n' ;; *) printf '    listen 443 ssl;\n' ;; esac
        case "$bez" in *imya*) printf '    server_name chuzhoj.example;\n' ;;
                           *) printf '    server_name %s;\n' "$HOST_STENDA" ;; esac
        case "$bez" in *vnutr*) : ;; *) printf '    location /vnutr/ { return 404; }\n' ;; esac
        printf '    location / {\n'
        case "$bez" in *port*) printf '        proxy_pass http://127.0.0.1:9999;\n' ;;
                            *) printf '        proxy_pass http://127.0.0.1:%s;\n' "$PORT_STENDA" ;; esac
        case "$bez" in *realip*) : ;; *) printf '        proxy_set_header X-Real-IP $remote_addr;\n' ;; esac
        printf '    }\n}\n'
    } > "$T/nginx/sites-available/harness-panel"
    # Симлинк обязателен: заглушку сборщик ищет во ВКЛЮЧЁННЫХ конфигах, а не
    # в доступных — выключенный файл рубежом не является.
    ln -sfn "$T/nginx/sites-available/harness-panel" "$T/nginx/sites-enabled/harness-panel"
}

for sluchaj in zaglushka listen443 imya vnutr port realip; do
    T=$(mktemp -d); stend "$T" "https://$HOST_STENDA" "$PORT_STENDA"
    zhivoj_konfig "$T" "$sluchaj"
    VYVOD=$(zvat "$T"); RC=$?
    proba "БОЛЬНОЙ sverit: пропала опора «$sluchaj» — rc" "1" "$RC"
    proba "БОЛЬНОЙ sverit: «$sluchaj» названа в выводе" "да" \
          "$(printf '%s' "$VYVOD" | grep -q '✗' && echo да || echo нет)"
    rm -rf "$T"
done

# Здоровый конфиг того же вида обязан пройти: иначе шесть проб выше зеленели
# бы от любой поломки сверки.
T=$(mktemp -d); stend "$T" "https://$HOST_STENDA" "$PORT_STENDA"
zhivoj_konfig "$T" "nichego"
VYVOD=$(zvat "$T"); RC=$?
proba "здоровый конфиг того же вида — rc 0" "0" "$RC"
rm -rf "$T"

# ── 11. «--переписать»: копия снята, файл заменён ──────────────────────────
T=$(mktemp -d); stend "$T" "https://$HOST_STENDA" "$PORT_STENDA"
printf 'server { listen 443 ssl; server_name staryj.example; }\n' \
    > "$T/nginx/sites-available/harness-panel"
VYVOD=$(zvat "$T" --переписать); RC=$?
proba "--переписать: rc 0" "0" "$RC"
proba "--переписать: копия прежнего снята" "1" \
      "$(find "$T/nginx/sites-available" -name 'harness-panel.*.bak' | wc -l)"
proba "--переписать: файл заменён собранным" "да" \
      "$(grep -q "server_name $HOST_STENDA;" "$T/nginx/sites-available/harness-panel" && echo да || echo нет)"
rm -rf "$T"

# ── 12. Неизвестный ключ не проваливается в запись ────────────────────────
T=$(mktemp -d); stend "$T" "https://$HOST_STENDA" "$PORT_STENDA"
VYVOD=$(zvat "$T" --pokazat); RC=$?
proba "БОЛЬНОЙ: неизвестный ключ — отказ" "1" "$RC"
proba "БОЛЬНОЙ: ключ назван в отказе" "да" \
      "$(printf '%s' "$VYVOD" | grep -q 'неизвестный ключ' && echo да || echo нет)"
proba "БОЛЬНОЙ: при неизвестном ключе ничего не создано" "0" \
      "$(find "$T/nginx/sites-available" -type f | wc -l)"
rm -rf "$T"

# ── 13. Нет сертификата — отказ с командой, а не «nginx -t покраснел» ─────
T=$(mktemp -d); stend "$T" "https://$HOST_STENDA" "$PORT_STENDA"
rm -rf "$T/letsencrypt/$HOST_STENDA"
VYVOD=$(zvat "$T"); RC=$?
proba "БОЛЬНОЙ: сертификата нет — rc" "1" "$RC"
proba "БОЛЬНОЙ: напечатана команда certbot" "да" \
      "$(printf '%s' "$VYVOD" | grep -q 'certbot certonly' && echo да || echo нет)"
proba "БОЛЬНОЙ: без сертификата ничего не создано" "0" \
      "$(find "$T/nginx/sites-available" -type f | wc -l)"
rm -rf "$T"

# ── 14. В sites-enabled лежит ФАЙЛ, а не ссылка (И-1) ─────────────────────
# Ревью кода 13.09.2026, F-01: ln -sfn затирал бы чужой рабочий конфиг без
# копии, а откат по красному nginx -t оставлял каталог пустым.
T=$(mktemp -d); stend "$T" "https://$HOST_STENDA" "$PORT_STENDA"
printf 'server { listen 443 ssl; server_name %s; }\n' "$HOST_STENDA" \
    > "$T/nginx/sites-enabled/harness-panel"
SUMMA_DO=$(md5sum "$T/nginx/sites-enabled/harness-panel" | cut -d' ' -f1)
VYVOD=$(zvat "$T"); RC=$?
proba "БОЛЬНОЙ: в sites-enabled файл, а не ссылка — отказ" "1" "$RC"
proba "БОЛЬНОЙ: чужой файл не затёрт" "$SUMMA_DO" \
      "$(md5sum "$T/nginx/sites-enabled/harness-panel" | cut -d' ' -f1)"
proba "БОЛЬНОЙ: копия чужого файла снята" "1" \
      "$(find "$T/nginx/sites-enabled" -name 'harness-panel.*.bak' | wc -l)"
rm -rf "$T"

# ── 15. Проверить конфиг нечем — не включаем вслепую ──────────────────────
T=$(mktemp -d); stend "$T" "https://$HOST_STENDA" "$PORT_STENDA"
rm -f "$T/bin/nginx"
VYVOD=$(zvat "$T"); RC=$?
proba "БОЛЬНОЙ: бинаря нет — симлинк НЕ поставлен" "0" \
      "$(find "$T/nginx/sites-enabled" -maxdepth 1 -name harness-panel | wc -l)"
# Слово «нечем» печатает и проверка конфига, поэтому проба требует ИМЕННО
# отказ сборщика: иначе она зеленела бы и при «ставим вслепую» (ревью F-08).
proba "БОЛЬНОЙ: бинаря нет — отказ «ничего не тронуто»" "да" \
      "$(printf '%s' "$VYVOD" | grep -q 'ничего не тронуто' && echo да || echo нет)"
rm -rf "$T"

# ── 16. PANEL_URL с портом — отказ словом, а не молчаливый разъезд ────────
T=$(mktemp -d); stend "$T" "https://$HOST_STENDA:8443" "$PORT_STENDA"
VYVOD=$(zvat "$T" --показать); RC=$?
proba "БОЛЬНОЙ: PANEL_URL с портом — отказ" "1" "$RC"
proba "БОЛЬНОЙ: порт назван в отказе" "да" \
      "$(printf '%s' "$VYVOD" | grep -q 'с портом' && echo да || echo нет)"
rm -rf "$T"

if [ "$ok" = 1 ]; then
    echo "SELFTEST: зелёный ($putej путей; больные: конфиг не переписан, опора пропала, nginx -t красный)"
    exit 0
fi
echo "SELFTEST: КРАСНЫЙ (путей $putej)"
exit 1
