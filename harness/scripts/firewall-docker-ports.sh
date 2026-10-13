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
# DOCKER_PORTS_ALLOWED в harness.conf — данные), а остальное закрыто.
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
pravila() {  # $1 = интерфейс, $2 = разрешённые порты через запятую
    local iface="$1" spisok="${2:-}" port
    [ -n "$iface" ] || { echo "не определён внешний интерфейс — закрывать нечего" >&2; return 1; }
    printf -- '-i %s -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN\n' "$iface"
    # Разбор через read, а не «for ... in $(…)»: значение с пробелом рвётся молча.
    while IFS= read -r port; do
        [ -n "$port" ] || continue
        printf -- '-i %s -p tcp --dport %s -j RETURN\n' "$iface" "$port"
    done < <(printf '%s\n' "$spisok" | tr ',' '\n' | tr -d ' ')
    printf -- '-i %s -j DROP\n' "$iface"
    printf -- '-j RETURN\n'
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
        stoit="$(printf '%s\n' "$vyvod" | sed "/^-N /d;s|^-A $CEPOCHKA ||")"
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
for semja in iptables ip6tables; do
    # shellcheck disable=SC2086
    $POD_ROOT "$semja" -N "$CEPOCHKA" 2>/dev/null || true
    # shellcheck disable=SC2086
    $POD_ROOT "$semja" -F "$CEPOCHKA"
    while IFS= read -r pravilo; do
        [ -n "$pravilo" ] || continue
        # shellcheck disable=SC2086
        $POD_ROOT "$semja" -A "$CEPOCHKA" $pravilo
    done <<< "$SPISOK"
done

printf 'закрыто снаружи всё, кроме: %s (интерфейс %s)\n' "${RAZRESHENO:-ничего}" "$IFACE"
