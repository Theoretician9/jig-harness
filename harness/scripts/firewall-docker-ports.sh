#!/usr/bin/env bash
# Закрыть снаружи порты, опубликованные контейнерами docker, кроме названных.
#
# СЛУЧАЙ (03.10.2026). На машине кроме нашего продукта живёт второй проект
# владельца: его postgres (5433) и redis (6380) опубликованы контейнерами на
# 0.0.0.0, redis отвечал PONG БЕЗ ПАРОЛЯ. ufw их не закрывает и закрыть НЕ
# МОЖЕТ: docker кладёт DNAT в таблицу nat, пакет идёт через FORWARD мимо INPUT,
# где стоят правила ufw. Единственное место, где такой пакет судится, — цепочка
# DOCKER-USER: docker создаёт её пустой и больше не трогает.
#
# Почему не список запрещённых. Четыре правила «--dport N -j DROP» закрывают
# четыре порта и молчат о пятом: любой новый контейнер со строкой `ports:`
# снова торчит в интернет. Поэтому названо РАЗРЕШЁННОЕ (ключ
# DOCKER_PORTS_ALLOWED в harness.conf — данные; элемент «порт», «порт/tcp» или
# «порт/udp»), а остальное закрыто.
#
# Правило «обратные пакеты» стоит ПЕРВЫМ, и это не украшение: без него глухой
# DROP на входящем интерфейсе рубит ОТВЕТЫ на исходящие соединения контейнеров
# — они приходят с того же eth0 и идут тем же FORWARD. Контейнеры остались бы
# без интернета, а причина выглядела бы как поломка сети.
#
# Запуск:
#   firewall-docker-ports.sh              # поставить правила (нужен root)
#   firewall-docker-ports.sh --показать   # напечатать правила, ничего не менять
#   firewall-docker-ports.sh --проверить  # стоят ли правила: rc=0 да, rc=1 нет
#   firewall-docker-ports.sh --закрепить   # юнит, который ставит их при старте
#   firewall-docker-ports.sh --selftest   # пробы на подставных данных
set -euo pipefail

SAM="$(readlink -f "${BASH_SOURCE[0]}")"
ZDES="$(dirname "$SAM")"
# shellcheck source=/dev/null
source "$ZDES/lib/config.sh"

CEPOCHKA="DOCKER-USER"

# Правила ядра читает и пишет только root. Демон осмотра бежит от агента, и
# без этой строки он получал бы «iptables: command not found» (бинарник лежит
# в /usr/sbin, которого нет в PATH обычного пользователя) и объявлял бы запор
# неизвестным КАЖДОЕ утро — а краснящий зря сторож отключают.
POD_ROOT=""
[ "$(id -u)" = "0" ] || POD_ROOT="sudo -n"

# Внешний интерфейс берётся у ядра, а не вшивается: на чужой машине он не eth0
# (гейт check-host-independence.py). Переменная окружения — только для проб.
vneshnij_interfejs() {
    [ -n "${FIREWALL_IFACE:-}" ] && { printf '%s' "$FIREWALL_IFACE"; return 0; }
    ip -o route show default 2>/dev/null | awk '{print $5; exit}'
}

# Печатает правила цепочки по порядку: хвост команды после «-A DOCKER-USER».
# Чистая функция — её и судит самотест; постановка отделена от решения.
# Элемент списка: «порт» (tcp), «порт/tcp» или «порт/udp». Всё остальное —
# отказ ДО постановки: иначе iptables отказал бы уже после «-F», и цепочка
# осталась бы пустой, то есть открытой настежь.
pravila() {  # $1 = интерфейс, $2 = разрешённые порты через запятую
    local iface="$1" spisok="${2:-}" port proto elem
    [ -n "$iface" ] || { echo "не определён внешний интерфейс — закрывать нечего" >&2; return 1; }
    printf -- '-i %s -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN\n' "$iface"
    # Разбор через read, а не «for ... in $(…)»: значение с пробелом рвётся молча.
    while IFS= read -r elem; do
        [ -n "$elem" ] || continue
        port="${elem%%/*}"; proto=tcp
        [ "$port" = "$elem" ] || proto="${elem#*/}"
        case "$proto" in tcp|udp) ;; *)
            echo "DOCKER_PORTS_ALLOWED: «$elem» — протокол должен быть tcp или udp" >&2; return 1 ;;
        esac
        if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
            echo "DOCKER_PORTS_ALLOWED: «$elem» — порт должен быть числом 1..65535" >&2; return 1
        fi
        printf -- '-i %s -p %s --dport %s -j RETURN\n' "$iface" "$proto" "$port"
    done < <(printf '%s\n' "$spisok" | tr ',' '\n' | tr -d ' ')
    printf -- '-i %s -j DROP\n' "$iface"
    printf -- '-j RETURN\n'
}

# iptables -S сам дописывает «-m tcp»/«-m udp» перед --dport; намеченный текст
# его не содержит, и без сглаживания сверка краснела бы на любом разрешении.
snyat_modul_protokola() { sed -E 's/ -m (tcp|udp) / /'; }

# Постановка в одну семью адресов. Если правило не встало ПОСЛЕ «-F», цепочка
# пуста — открыто всё; поэтому при сбое ставится закрытый минимум (без
# разрешений), и только потом отказ.
postavit_semju() {  # $1 = iptables|ip6tables, $2 = правила, $3 = закрытый минимум
    local semja="$1" spisok="$2" minimum="$3" pravilo
    # shellcheck disable=SC2086
    $POD_ROOT "$semja" -N "$CEPOCHKA" 2>/dev/null || true
    # shellcheck disable=SC2086
    $POD_ROOT "$semja" -F "$CEPOCHKA"
    while IFS= read -r pravilo; do
        [ -n "$pravilo" ] || continue
        # shellcheck disable=SC2086
        if ! $POD_ROOT "$semja" -A "$CEPOCHKA" $pravilo; then
            echo "$semja: правило не встало («$pravilo») — ставлю закрытый минимум" >&2
            $POD_ROOT "$semja" -F "$CEPOCHKA" || true
            while IFS= read -r pravilo; do
                # shellcheck disable=SC2086
                $POD_ROOT "$semja" -A "$CEPOCHKA" $pravilo || true
            done <<< "$minimum"
            return 1
        fi
    done <<< "$spisok"
}

# ── самотест: правила судятся текстом, железо не трогается ─────────────────
if [ "${1:-}" = "--selftest" ]; then
    ok=1
    proba() {  # $1 = имя, $2 = ждём, $3 = вышло
        if [ "$2" = "$3" ]; then printf '  ок    %s\n' "$1"
        else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$1" "$2" "$3"; ok=0; fi
    }

    P=$(pravila "proba0" "8080, 9000")
    proba "обратные пакеты судятся ПЕРВЫМ правилом" "да" \
          "$(printf '%s\n' "$P" | head -1 | grep -q 'ESTABLISHED -j RETURN' && echo да || echo нет)"
    proba "разрешённых портов столько, сколько названо" "2" \
          "$(printf '%s\n' "$P" | grep -c -- '--dport')"
    # Пробел ПЕРЕД портом в списке — обычная человеческая запись. Судится
    # точным текстом правила: иначе проба зеленеет от пробела перед «-j».
    proba "пробел в списке портов обрезан" "1" \
          "$(printf '%s\n' "$P" | grep -c -- '--dport 9000 -j RETURN' || true)"
    # БОЛЬНОЙ: разрешение обязано стоять ВЫШЕ запрета, иначе оно мертво.
    proba "БОЛЬНОЙ: разрешение выше запрета" "да" \
          "$( [ "$(printf '%s\n' "$P" | grep -n -- '--dport 8080' | cut -d: -f1)" \
               -lt "$(printf '%s\n' "$P" | grep -n -- '-j DROP' | cut -d: -f1)" ] && echo да || echo нет)"

    PUSTO=$(pravila "proba0" "")
    # БОЛЬНОЙ: пустой список — закрыто ВСЁ, а не открыто всё. Умолчание
    # «ничего не разрешено» — единственное безопасное.
    proba "БОЛЬНОЙ: пустой список разрешённых — запрет остаётся" "1" \
          "$(printf '%s\n' "$PUSTO" | grep -c -- '-j DROP')"
    proba "БОЛЬНОЙ: пустой список — ни одного разрешённого порта" "0" \
          "$(printf '%s\n' "$PUSTO" | grep -c -- '--dport' || true)"
    # БОЛЬНОЙ: запрет привязан к ВХОДЯЩЕМУ интерфейсу. Глухой «-j DROP» без
    # «-i» убил бы и разговор контейнеров между собой.
    proba "БОЛЬНОЙ: запрет только на внешнем интерфейсе" "да" \
          "$(printf '%s\n' "$PUSTO" | grep -- '-j DROP' | grep -q '^-i proba0 ' && echo да || echo нет)"
    # БОЛЬНОЙ: нет интерфейса — отказ СЛОВОМ, а не пустые правила. Пустой
    # вывод был бы принят постановкой за «закрывать нечего».
    PRICHINA=$(pravila "" "80" 2>&1 >/dev/null || true)
    proba "БОЛЬНОЙ: нет интерфейса — отказ назван словом" "да" \
          "$(printf '%s' "$PRICHINA" | grep -q 'не определён внешний интерфейс' && echo да || echo нет)"

    # udp: разбор протокола. Без суффикса — tcp, как было.
    PU=$(pravila "proba0" "8080,15637/udp,9000/tcp")
    proba "udp: порт с /udp ставится как -p udp" "1" \
          "$(printf '%s\n' "$PU" | grep -c -- '-p udp --dport 15637 -j RETURN' || true)"
    proba "udp: порт без суффикса остаётся tcp" "1" \
          "$(printf '%s\n' "$PU" | grep -c -- '-p tcp --dport 8080 -j RETURN' || true)"
    proba "udp: /tcp равен умолчанию" "1" \
          "$(printf '%s\n' "$PU" | grep -c -- '-p tcp --dport 9000 -j RETURN' || true)"
    proba "udp: в правиле не осталось «/»" "0" \
          "$(printf '%s\n' "$PU" | grep -c -- '/' || true)"
    # БОЛЬНОЙ: мусор отвергается ДО постановки, молча не принимается.
    for MUSOR in "15637/sctp" "abc" "70000/udp" "0" "80/"; do
        proba "БОЛЬНОЙ: мусор «$MUSOR» — отказ" "отказ" \
              "$(pravila proba0 "$MUSOR" >/dev/null 2>&1 && echo принято || echo отказ)"
    done
    proba "БОЛЬНОЙ: отказ назван словом" "да" \
          "$( { pravila proba0 "15637/sctp" 2>&1 >/dev/null || true; } | grep -q 'tcp или udp' && echo да || echo нет)"
    # БОЛЬНОЙ: то, что iptables -S напечатает для udp, сверка обязана признать своим.
    proba "БОЛЬНОЙ: вывод iptables -S с «-m udp» равен намеченному" "$(printf '%s\n' "$PU" | grep 15637)" \
          "$(echo '-i proba0 -p udp -m udp --dport 15637 -j RETURN' | snyat_modul_protokola)"
    # БОЛЬНОЙ: сбой постановки после «-F» не оставляет цепочку пустой.
    STUB=$(mktemp); LOG=$(mktemp); trap 'rm -f "$STUB" "$LOG"' EXIT
    cat > "$STUB" <<'STUB_KONEC'
#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
case "$*" in *"-p udp"*) exit 1 ;; esac
exit 0
STUB_KONEC
    chmod +x "$STUB"
    POD_ROOT="$STUB"; export STUB_LOG="$LOG"
    MIN=$(pravila proba0 "")
    postavit_semju iptables "$PU" "$MIN" 2>/dev/null && SBOJ=postavilos || SBOJ=otkaz
    proba "БОЛЬНОЙ: сбой постановки — отказ, а не молчание" "otkaz" "$SBOJ"
    proba "БОЛЬНОЙ: после сбоя цепочка закрыта (DROP среди поставленных после -F)" "да" \
          "$(sed -n '/^-F/h;/^-F/!H;${x;p}' "$LOG" | grep -q -- '-j DROP' && echo да || echo нет)"

    [ "$ok" = 1 ] && { echo "самотест: всё зелено"; exit 0; }
    echo "самотест: есть красное"; exit 1
fi

konf_zagruzit DOCKER_PORTS_ALLOWED
# Умолчание ставится ПОСЛЕ загрузчика: поставленное выше, оно для него
# неотличимо от значения из файла, и ключ в конфиге становится мёртвым
# ([[default-above-loader-kills-setting]]).
RAZRESHENO="${DOCKER_PORTS_ALLOWED:-}"
IFACE="$(vneshnij_interfejs)"

SPISOK="$(pravila "$IFACE" "$RAZRESHENO")"

if [ "${1:-}" = "--показать" ]; then
    printf 'интерфейс: %s\nразрешено: %s\n\n' "$IFACE" "${RAZRESHENO:-—}"
    printf '%s\n' "$SPISOK" | sed "s|^|-A $CEPOCHKA |"
    exit 0
fi

# Сверка «правила стоят»: спрашивается МАШИНА, а не журнал установки, и обе
# семьи адресов — у контейнеров публикуются и [::]-слушатели.
if [ "${1:-}" = "--проверить" ]; then
    bedy=0
    for semja in iptables ip6tables; do
        # «Не прочитал» и «правил нет» — РАЗНЫЕ ответы. Без root iptables
        # отказывает, пустой вывод выглядел бы как «цепочка пуста», и сторож
        # поднял бы тревогу о дыре, которой нет ([[command-failure-read-as-empty]]).
        if ! vyvod="$($POD_ROOT "$semja" -S "$CEPOCHKA" 2>&1)"; then
            printf '%s: не смог прочитать правила (%s) — это НЕ «правил нет»\n' \
                   "$semja" "$(printf '%s' "$vyvod" | head -1)"
            exit 77
        fi
        stoit="$(printf '%s\n' "$vyvod" | sed "/^-N /d;s|^-A $CEPOCHKA ||" | snyat_modul_protokola)"
        if [ "$stoit" != "$SPISOK" ]; then
            printf '%s: правила не совпадают с намеченными\n' "$semja"
            bedy=1
        fi
    done
    [ "$bedy" = 0 ] && echo "правила стоят в обеих семьях адресов"
    exit "$bedy"
fi

# Закрепление. Правило живёт в памяти ядра и исчезает при перезагрузке вместе
# со всей таблицей, а docker при старте заводит DOCKER-USER ПУСТОЙ. Поэтому
# носитель — юнит: PartOf=docker.service возвращает правила и после
# перезапуска самого docker, не только машины.
#
# Путь берётся у САМОГО файла (readlink -f), а не из паспорта: юнит с чужим
# путём systemd принимает молча и падает лишь при старте.
if [ "${1:-}" = "--закрепить" ]; then
    UNIT=/etc/systemd/system/harness-firewall.service
    VREMENNYJ=$(mktemp); trap 'rm -f "$VREMENNYJ"' EXIT
    cat > "$VREMENNYJ" <<UNIT_KONEC
[Unit]
Description=Закрыть снаружи порты контейнеров docker (цепочка DOCKER-USER)
After=docker.service
Requires=docker.service
PartOf=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$SAM

[Install]
WantedBy=multi-user.target
UNIT_KONEC
    if [ -f "$UNIT" ] && cmp -s "$VREMENNYJ" "$UNIT"; then
        echo "юнит уже стоит и совпадает: $UNIT"
    else
        sudo install -m 644 "$VREMENNYJ" "$UNIT"
        sudo systemctl daemon-reload
        echo "юнит поставлен: $UNIT"
    fi
    sudo systemctl enable harness-firewall.service >/dev/null
    # «Включён» спрашивается у systemd, а не у кода возврата install: строку в
    # неверной секции systemd принимает молча ([[a-setting-ignored-in-silence]]).
    printf 'systemd говорит: %s\n' "$(systemctl is-enabled harness-firewall.service)"
    exit 0
fi

# ── постановка ─────────────────────────────────────────────────────────────
# Цепочка принадлежит нам целиком: docker её создаёт и больше в неё не пишет,
# поэтому чистка перед постановкой — не потеря чужого, а единственный способ
# сделать повтор безвредным.
MINIMUM="$(pravila "$IFACE" "")"
for semja in iptables ip6tables; do
    postavit_semju "$semja" "$SPISOK" "$MINIMUM"
done

printf 'закрыто снаружи всё, кроме: %s (интерфейс %s)\n' "${RAZRESHENO:-ничего}" "$IFACE"
