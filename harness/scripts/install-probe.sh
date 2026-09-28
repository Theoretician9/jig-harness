#!/usr/bin/env bash
# install-probe.sh — живая проба УСТАНОВКИ С НУЛЯ в одноразовом контейнере.
#
# СЛУЧАЙ (Ф5 ревизии харнеса, 28.09.2026). Критерий приёмки требует «установка в
# контейнере проходит с нуля», а механизма не было: 13.09.2026 пробу провели
# РУКАМИ в контейнере harness-proba-ustanovki, нашли дефект (установщик не знал о
# панели) — и контейнер с тех пор висел две недели, занимая 1,24 ГБ. Единственный
# гейт про установку (test_panel_install.py) судит ТЕКСТ установщика: есть ли в
# нём шаг про панель. Текст не доказывает, что установка проходит.
#
# Что делает: поднимает контейнер из образа чистой машины (данные —
# INSTALL_PROBE_IMAGE), монтирует пакет только для чтения, кладёт ЗАПОЛНЕННЫЙ
# install.conf с подставными значениями (установщик не задаёт вопросов, когда
# паспорт заполнен), прогоняет USTANOVIT.sh и проверяет ИТОГ: пользователь,
# паспорт, юнит, расписание, раскладка. Контейнер и его тома убираются трапом.
#
# Секреты стенда ГЕНЕРИРУЮТСЯ, а не копируются с машины: скопированный токен —
# живой ключ в чужом дереве ([[the-probes-secret-was-a-real-one]]).
#
# Запуск (фоном, идёт минутами — apt, node, Claude Code):
#   setsid nohup bash scripts/install-probe.sh > /var/log/harness/установка-проба.log 2>&1 &
#   bash scripts/install-probe.sh --selftest   # логика без docker
# Код возврата: 0 — установка прошла, число провалов — если нет, 77 — мерить
# нечем (нет docker, образа или пакета).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
# shellcheck disable=SC1091
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/config.sh"
konf_zagruzit

NE_PRIMENIMO=77
PROBE_LOG="$LOG_DIR/проба-установки.log"
# Образ и потолок — ДАННЫЕ: на другой машине образ зовут иначе, а медленная
# сеть требует больше времени. Зашитые значения сделали бы пробу непереносимой.
OBRAZ="${INSTALL_PROBE_IMAGE:-harness-chistaya-mashina:latest}"
POTOLOK_MIN="${INSTALL_PROBE_MINUTES:-30}"
# Домашний каталог НАШЕГО агента литералом — след установки: замок 6
# публикации отверг сборку 28.09.2026. Путь выводится из паспорта,
# тем же приёмом, что в publish.sh.
PAKET="${STARTER_DIR:-/home/$(konf_iz_fajla AGENT_USER)/starter/STARTER-PACKAGE}"

# Пути ВНУТРИ контейнера-стенда: корень той машины задаёт паспорт стенда, а не
# наша установка, поэтому здесь литералы законны. Имя переменной говорит, что это
# чужая машина, — и по нему же стоит исключение в scripts/check-install-root.py.
KONTEJNER_STATE="/var/lib/harness"
KONTEJNER_LOG="/var/log/harness"
KONTEJNER_CONF="/etc/harness"

OK=0
BAD=0

skazat() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"; }

put_ok() { OK=$((OK + 1)); printf '  ок    %s\n' "$1"; }
put_bad() { BAD=$((BAD + 1)); printf '  ПЛОХО %s\n' "$1"; }

# Подставной секрет стенда: генерируется из случайных байтов. Формой похож на
# токен бота, но не является ничьим ключом.
# Мусор прежних прогонов: контейнеры своего префикса, кроме нынешнего. Свой
# trap спасает не всегда — по SIGKILL (так пробу убивал потолок прибора
# независимости) он не исполняется вовсе. 28.09.2026 на машине нашлось 15 живых
# контейнеров пробы, свободной памяти 266 МБ — и systemd в новом контейнере уже
# не поднимался за 120 с: проба падала от собственного мусора. Число называется
# вслух: молча выброшенное неотличимо от небывшего.
ubrat_musor() {  # $1 — имя нынешнего контейнера (его не трогаем)
    local nyneshnij="${1:-}" starye chislo
    starye=$(docker ps -a --filter "name=harness-install-proba-" \
                --format '{{.Names}}' 2>/dev/null | grep -v "^${nyneshnij}$" || true)
    chislo=$(printf '%s\n' "$starye" | grep -c . || true)
    case "$chislo" in ""|*[!0-9]*) chislo=0 ;; esac
    [ "$chislo" = 0 ] && return 0
    skazat "мусор прежних прогонов: контейнеров $chislo — убираю"
    printf '%s\n' "$starye" | while IFS= read -r staryj; do
        [ -n "$staryj" ] || continue
        docker rm -fv "$staryj" >/dev/null 2>&1 || skazat "не убрался: $staryj"
    done
    return 0
}

podstavnoj_sekret() {
    printf '%s:%s' "$((RANDOM % 900000 + 100000))" \
        "$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 30)"
}

# Паспорт установки для стенда. Отдельной функцией — её судит самотест:
# без заполненного паспорта установщик задаёт вопросы и проба виснет навсегда.
pasport_stenda() {
    local koren="$1"
    # Имена ключей — как в ЖИВОМ /etc/harness/install.conf, а не по памяти:
    # установщик пропускает опрос только если непусты PROJECT_NAME, PROJECT_DIR,
    # SECRETS_DIR и TG_CHAT_ID. Ошибка в имени = вопрос = повисшая проба.
    cat <<CONF
PROJECT_NAME="proba"
PROJECT_DIR="$koren"
SECRETS_DIR="$KONTEJNER_STATE/secrets"
AGENT_USER="agent"
TG_CHAT_ID="100200300"
AUTONOMY="semi"
OWNER_TZ="Etc/UTC"
STATE_DIR="$KONTEJNER_STATE"
HEARTBEAT_DIR="$KONTEJNER_STATE/heartbeat"
LOG_DIR="$KONTEJNER_LOG"
TG_CHANNEL_VARIANT="B"
ACTIVE_LINE="harness"
CONF
}

# Ключи, при непустоте которых установщик НЕ задаёт вопросов. Берутся из его
# собственного условия опроса, а не из памяти: правка установщика иначе оставит
# пробу висеть на первом вопросе, и самотест этого не заметит.
kluchi_oprosa() {
    grep -oE '\$\{[A-Z_]+:-\}' "$PAKET/USTANOVIT.sh" 2>/dev/null \
        | sed 's/[${:}-]//g' | sort -u | head -20
}

# Применимость: чем мерить. «Нет docker» и «нет образа» — разные ответы, и оба
# не равны «установка сломана».
pochemu_nelzya() {
    command -v docker >/dev/null 2>&1 || { echo "docker не установлен"; return 0; }
    docker info >/dev/null 2>&1 || { echo "docker не отвечает"; return 0; }
    docker image inspect "$OBRAZ" >/dev/null 2>&1 \
        || { echo "образа чистой машины «$OBRAZ» нет (собери его или задай INSTALL_PROBE_IMAGE)"; return 0; }
    [ -f "$PAKET/USTANOVIT.sh" ] || { echo "пакета нет: $PAKET/USTANOVIT.sh"; return 0; }
    return 1
}

# Итог установки судится ВНУТРИ контейнера по пяти следам, которые обязаны
# появиться. Каждый — своей командой: «установка прошла» без следов ничем не
# отличается от «скрипт завершился с нулём».
sledy_ustanovki() {
    local kont="$1" koren="$2"
    docker exec "$kont" bash -lc "id agent >/dev/null 2>&1" \
        && put_ok "пользователь agent заведён" \
        || put_bad "пользователя agent нет — шаг 1 установки не сработал"
    docker exec "$kont" bash -lc "test -s $KONTEJNER_CONF/install.conf" \
        && put_ok "паспорт установки на месте" \
        || put_bad "паспорта установки в контейнере нет или он пуст"
    docker exec "$kont" bash -lc "test -f $koren/CLAUDE.md && test -d $koren/scripts" \
        && put_ok "харнес разложен в каталог проекта" \
        || put_bad "раскладки нет: $koren без CLAUDE.md или без scripts/"
    docker exec "$kont" bash -lc \
        "systemctl list-unit-files 2>/dev/null | grep -q harness-agent" \
        && put_ok "юнит harness-agent объявлен системе" \
        || put_bad "юнита harness-agent нет — агент не поднимется после перезагрузки"
    docker exec "$kont" bash -lc "crontab -u agent -l 2>/dev/null | grep -q harness" \
        && put_ok "расписание демонов заведено агенту" \
        || put_bad "в расписании агента нет ни одного демона харнеса"
}

if [ "${1:-}" = "--selftest" ]; then
    ST_OK=1
    proba() { # $1 = имя, $2 = 0/1
        if [ "$2" = 1 ]; then printf '  ок    %s\n' "$1"
        else printf '  ПЛОХО %s\n' "$1"; ST_OK=0; fi
    }

    # БОЛЬНОЙ СЛУЧАЙ: паспорт стенда обязан содержать ВСЕ ключи, о которых
    # установщик спрашивает, иначе прогон повиснет на первом вопросе и проба
    # будет «идти» до потолка времени.
    PASPORT=$(pasport_stenda "/var/www/proba")
    # Ровно те ключи, которые проверяет условие опроса установщика.
    NUZHNY="PROJECT_NAME PROJECT_DIR SECRETS_DIR TG_CHAT_ID"
    NET=""
    for kluch in $NUZHNY; do
        printf '%s\n' "$PASPORT" | grep -q "^$kluch=" || NET="$NET $kluch"
    done
    # Пустой список ключей — не «всё сошлось», а отсутствие проверки: мутация
    # NUZHNY="" оставляла самотест зелёным (мутационный прогон 28.09.2026).
    proba "список нужных ключей не пуст" "$([ -n "$NUZHNY" ] && echo 1 || echo 0)"
    proba "паспорт стенда заполняет все ключи, о которых спрашивает установщик${NET:+ (нет:$NET)}" \
          "$([ -n "$NUZHNY" ] && [ -z "$NET" ] && echo 1 || echo 0)"

    # Условие опроса в установщике не разошлось с паспортом стенда: список
    # ключей читается из НЕГО, а не из этого файла.
    OPROS=$(kluchi_oprosa)
    RAZOSHLIS=""
    for kluch in $NUZHNY; do
        printf '%s\n' "$OPROS" | grep -q "^$kluch$" || RAZOSHLIS="$RAZOSHLIS $kluch"
    done
    proba "ключи опроса установщика и паспорт стенда сходятся${RAZOSHLIS:+ (нет в опросе:$RAZOSHLIS)}" \
          "$([ -n "$OPROS" ] && [ -z "$RAZOSHLIS" ] && echo 1 || echo 0)"

    # Секрет стенда СГЕНЕРИРОВАН: два вызова дают разное, и живого токена там нет.
    S1=$(podstavnoj_sekret); S2=$(podstavnoj_sekret)
    proba "подставной секрет генерируется, а не берётся с машины" \
          "$([ "$S1" != "$S2" ] && [ ${#S1} -gt 20 ] && echo 1 || echo 0)"
    ZHIVOJ=""
    if [ -n "${SECRETS_DIR:-}" ] && [ -d "$SECRETS_DIR" ]; then
        ZHIVOJ=$(grep -rhoE '[0-9]{6,}:[A-Za-z0-9_-]{30,}' "$SECRETS_DIR" 2>/dev/null | head -1 || true)
    fi
    proba "сгенерированный секрет не совпадает с живым токеном машины" \
          "$([ -z "$ZHIVOJ" ] || [ "$S1" != "$ZHIVOJ" ] && echo 1 || echo 0)"

    # БОЛЬНОЙ СЛУЧАЙ: нет образа — «не применимо», а не «установка сломана».
    VYVOD=$(OBRAZ="нет-такого-образа:0" bash -c '
        OBRAZ="нет-такого-образа:0"; PAKET="/нет-такого-пакета"
        command -v docker >/dev/null 2>&1 || { echo "docker не установлен"; exit 0; }
        docker image inspect "$OBRAZ" >/dev/null 2>&1 || { echo "образа нет"; exit 0; }
        echo ""' 2>&1) && RC=0 || RC=$?
    proba "выдуманный образ даёт причину, а не тишину" \
          "$([ -n "$VYVOD" ] && echo 1 || echo 0)"

    # Счёт следов: каждый провал увеличивает BAD, и код возврата равен их числу.
    OK=0; BAD=0
    # Печать стендовых строк глушится: иначе в отчёте самотеста появляются
    # «ПЛОХО», которых нет, и читатель считает пробу красной.
    put_ok "стендовый след" >/dev/null
    put_bad "стендовый провал" >/dev/null
    put_bad "второй провал" >/dev/null
    proba "счёт следов: успехов 1, провалов 2" \
          "$([ "$OK" = 1 ] && [ "$BAD" = 2 ] && echo 1 || echo 0)"

    # Потолок и образ — из данных, не из кода.
    proba "образ берётся из данных (INSTALL_PROBE_IMAGE)" \
          "$(INSTALL_PROBE_IMAGE=свой:1 bash -c 'echo "${INSTALL_PROBE_IMAGE:-harness-chistaya-mashina:latest}"' \
             | grep -q '^свой:1$' && echo 1 || echo 0)"
    proba "потолок минут берётся из данных (INSTALL_PROBE_MINUTES)" \
          "$(INSTALL_PROBE_MINUTES=7 bash -c 'echo "${INSTALL_PROBE_MINUTES:-30}"' \
             | grep -q '^7$' && echo 1 || echo 0)"

    # Имя контейнера и тома несёт свой префикс: уборка по префиксу не задевает
    # чужие висячие тома ([[anonymous-volume-outlives-container]]).
    IMYA="harness-install-proba-$$"
    proba "имя одноразового контейнера несёт свой префикс и pid" \
          "$(printf '%s' "$IMYA" | grep -q '^harness-install-proba-[0-9]\+$' && echo 1 || echo 0)"

    # БОЛЬНОЙ СЛУЧАЙ 28.09.2026: мусор прежних прогонов убил пробу. Судим
    # ПОВЕДЕНИЕ уборки на подставном docker: перечисленные контейнеры обязаны
    # быть удалены, нынешний — нет, и число названо вслух.
    STUB=$(mktemp -d)
    cat > "$STUB/docker" <<'DOCKER'
#!/usr/bin/env bash
if [ "$1 $2" = "ps -a" ]; then
    printf 'harness-install-proba-111\nharness-install-proba-222\nharness-install-proba-777\n'
    exit 0
fi
if [ "$1" = "rm" ]; then
    printf '%s\n' "${!#}" >> "$STUB_UBRANO"
    exit 0
fi
exit 0
DOCKER
    chmod +x "$STUB/docker"
    STUB_UBRANO="$STUB/убрано" ; : > "$STUB_UBRANO"
    # Функция зовётся в ПОДОБОЛОЧКЕ с подменённым PATH: так судится тот самый
    # код, что работает в бою, и без повторного запуска самого скрипта.
    SKAZANO=$( (export PATH="$STUB:$PATH" STUB_UBRANO="$STUB_UBRANO"
                ubrat_musor harness-install-proba-777) )
    UBRANO=$(sort -u "$STUB_UBRANO" 2>/dev/null | grep -c . || true)
    proba "уборка мусора: убраны оба чужих контейнера, нынешний не тронут" \
          "$([ "$UBRANO" = 2 ] && ! grep -q 'harness-install-proba-777' "$STUB_UBRANO" && echo 1 || echo 0)"
    proba "число мусора названо вслух" \
          "$(printf '%s' "$SKAZANO" | grep -q 'контейнеров 2' && echo 1 || echo 0)"
    rm -rf "$STUB"

    if [ "$ST_OK" = 1 ]; then
        echo "SELFTEST: зелёный (12 путей, первым — больной случай «паспорт стенда неполон»)"
        exit 0
    fi
    echo "SELFTEST: КРАСНЫЙ"; exit 1
fi

PRICHINA=$(pochemu_nelzya) && {
    skazat "ПРОБА НЕ ПРОВЕДЕНА: $PRICHINA"
    printf '%s  ПРОБА УСТАНОВКИ НЕ ПРОВЕДЕНА: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" \
        "$PRICHINA" >> "$PROBE_LOG" 2>/dev/null || true
    exit "$NE_PRIMENIMO"
}

KONT="harness-install-proba-$$"
KOREN_STENDA="/var/www/proba"
KONT_LOG="$LOG_DIR/проба-установки.контейнер.$(date +%F_%H%M).log"
# Трап ДО запуска и на INT TERM EXIT: docker run умеет создать контейнер и
# отказать на старте, а EXIT-трап не исполняется при Ctrl-C.
trap 'docker logs "$KONT" > "$KONT_LOG" 2>&1 || true;
      docker rm -fv "$KONT" >/dev/null 2>&1 || true' INT TERM EXIT

ubrat_musor "$KONT"

skazat "поднимаю чистую машину: образ $OBRAZ, контейнер $KONT"
ZAPUSK=$(docker run -d --name "$KONT" --privileged --cgroupns=host \
    -v "$PAKET:/mnt/paket:ro" -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
    -e DEBIAN_FRONTEND=noninteractive "$OBRAZ" /sbin/init 2>&1) && RC=0 || RC=$?
if [ "$RC" != 0 ]; then
    skazat "ОТКАЗ: контейнер не поднялся: $ZAPUSK"
    printf '%s  ПРОБА УСТАНОВКИ НЕ ПРОВЕДЕНА: контейнер не поднялся\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" >> "$PROBE_LOG" 2>/dev/null || true
    exit 1
fi

# Ждём systemd: без него нет ни юнитов, ни crontab, и установка упадёт на
# первом же systemctl — а выглядело бы это как дефект установщика.
GOTOV=0
for _ in $(seq 1 60); do
    if docker exec "$KONT" systemctl is-system-running 2>/dev/null \
            | grep -qE 'running|degraded'; then GOTOV=1; break; fi
    sleep 2
done
if [ "$GOTOV" != 1 ]; then
    skazat "ОТКАЗ: systemd в контейнере не поднялся за 120 с — судить установку нечем"
    printf '%s  ПРОБА УСТАНОВКИ НЕ ПРОВЕДЕНА: systemd не поднялся\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" >> "$PROBE_LOG" 2>/dev/null || true
    exit 1
fi
skazat "systemd готов"

SEKRET=$(podstavnoj_sekret)
docker exec "$KONT" mkdir -p "$KONTEJNER_CONF"
pasport_stenda "$KOREN_STENDA" \
    | docker exec -i "$KONT" tee "$KONTEJNER_CONF/install.conf" >/dev/null
# Файл токена: без него установщик спросит токен даже при заполненном паспорте.
docker exec "$KONT" mkdir -p "$KONTEJNER_STATE/secrets"
printf '%s' "$SEKRET" \
    | docker exec -i "$KONT" tee "$KONTEJNER_STATE/secrets/tg_bot_token" >/dev/null
docker exec "$KONT" chmod 600 "$KONTEJNER_STATE/secrets/tg_bot_token"
skazat "паспорт стенда положен — установщик не задаёт вопросов"

# Живая отправка в канал в контейнере НЕВОЗМОЖНА по построению: внешней службы
# там нет, а заглушку по сети не достать — на этой машине ufw держит INPUT DROP,
# и контейнер до хоста не дотягивается (замер 28.09.2026: «НЕ-ДОСТУЧАЛСЯ» при
# слушателе на 172.17.0.1:8099). Поэтому установщик идёт в режиме стенда
# (INSTALL_CHANNEL_CHECK=0): шаг канала СТАВИТ юнит, но не проверяет отправку и
# громко говорит об этом, а требование проверить канал руками уходит в
# «ОСТАЛОСЬ РУКАМИ». Раньше установка здесь СТОПалась, и расписание демонов —
# самое важное, что проба обязана увидеть, — не заводилось вовсе.
skazat "установщик пойдёт в режиме стенда: живая отправка в канал не проверяется"

# Полный вывод установщика — в ФАЙЛ с отметкой времени: в консоль идёт только
# хвост, а причина отказа живёт в начале (первый прогон 28.09.2026: код 1 при
# «упавших шагов нет», и начала вывода в журнале не осталось).
UST_LOG="$LOG_DIR/проба-установки.установщик.$(date +%F_%H%M).log"
skazat "прогон установщика (потолок $POTOLOK_MIN мин), полный вывод: $UST_LOG"
docker exec -e INSTALL_CHANNEL_CHECK=0 "$KONT" \
    timeout "${POTOLOK_MIN}m" bash /mnt/paket/USTANOVIT.sh > "$UST_LOG" 2>&1 \
    && URC=0 || URC=$?
tail -25 "$UST_LOG"
if [ "$URC" = 124 ]; then
    put_bad "установщик не кончился за $POTOLOK_MIN мин — повис (вопрос? сеть?)"
elif [ "$URC" != 0 ]; then
    put_bad "установщик вышел с кодом $URC — причина в $UST_LOG"
else
    put_ok "установщик прошёл целиком (код 0)"
fi

sledy_ustanovki "$KONT" "$KOREN_STENDA"
# Падения демонов при установке: ожидаемы ТОЛЬКО у тех, кто зовёт модель или
# канал — в контейнере нет ни OAuth-входа, ни живого Telegram. Остальные падения
# — настоящая находка: без этого следа проба зеленела бы при любом падении.
UPAVSHIE=$(grep -oE '^  - демон [a-z0-9-]+' "$UST_LOG" 2>/dev/null \
    | awk '{print $3}' | sort -u || true)
NEOZHIDANNYE=""
for demon in $UPAVSHIE; do
    if docker exec "$KONT" bash -lc \
            "grep -qE 'claude|tg_send' $KOREN_STENDA/harness/demons/$demon.sh" 2>/dev/null; then
        continue
    fi
    NEOZHIDANNYE="$NEOZHIDANNYE $demon"
done
if [ -z "$NEOZHIDANNYE" ]; then
    put_ok "упавшие демоны ($(printf '%s' "$UPAVSHIE" | tr '\n' ' ')) — только те, что ждут входа в модель или канал"
else
    put_bad "демоны упали не из-за входа в модель:$NEOZHIDANNYE — это находка установки"
fi

# Честность итога: канал в контейнере не проверялся, и это часть отчёта, а не
# умолчание. Требование обязано лежать в списке «осталось руками» установщика.
if docker exec "$KONT" bash -lc \
        "grep -q 'проверить канал живой отправкой' $KONTEJNER_LOG/ОСТАЛОСЬ-РУКАМИ.txt" 2>/dev/null; then
    put_ok "канал не проверен — и установщик сам требует проверить его руками"
else
    put_bad "канал не проверен, а требования проверить его руками в списке НЕТ"
fi

ITOG="$(date '+%Y-%m-%d %H:%M:%S')  === проба установки: следов ок $OK, провалов $BAD (образ $OBRAZ) ==="
echo "$ITOG"
printf '%s\n' "$ITOG" >> "$PROBE_LOG" 2>/dev/null || true
exit "$BAD"
