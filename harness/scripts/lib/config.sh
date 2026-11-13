#!/usr/bin/env bash
# shellcheck shell=bash
# Чтение конфигов харнеса так, чтобы ОКРУЖЕНИЕ было старше файла.
#
# Откуда взят. Улика 12.09.2026: проба переключения модели шла с
# `TMUX_SESSION=проба-модели`, но `source` конфига перезаписал переменную
# значением «agent» — и команда «/model fable» ушла в РАБОЧУЮ панель агента.
# Сессия сменила модель посреди работы, и заметил это владелец, а не проверка.
#
# Замер того же часа: 29 скриптов харнеса сорсят конфиг и читают из него
# PROJECT_DIR, LOG_DIR, TMUX_SESSION, AGENT_START_CMD. У большинства защиты
# нет — значит ЛЮБАЯ проба, задающая эти ключи в окружении, играет на боевом.
#
# Приём один: запомнить окружение ДО source и вернуть ПОСЛЕ.
#
#   source "$HERE/lib/config.sh"
#   konf_zagruzit
#
# Без аргументов защищаются ВСЕ ключи, объявленные в конфигах: список ключей
# в каждом скрипте — частный случай, который забывают дополнить (первая
# редакция требовала перечисления, и уже второй скрипт забыл SECRETS_DIR).
# Аргументы остаются для ключа, которого в конфигах ещё нет.
#
# ВАЖНО про порядок: дефолт ключа конфига (`X="${X:-15}"`) ставится ПОСЛЕ
# konf_zagruzit, а не до. Поставленный до, он для загрузчика неотличим от
# значения окружения и переживёт файл — четыре порога session-warden так стали
# мёртвыми данными (ревью кода 12.09.2026, F1-gen-01).
#
# Пути конфигов НЕ экспортируются: экспорт разослал бы подменённый путь всем
# потомкам, включая боевую сессию агента, которую поднимает session-warden
# (то же ревью, F1-gen-02). Порядок имён — `HARNESS_INSTALL_CONF` старше
# голого `INSTALL_CONF`, как во всех 25 вызывающих скриптах: голое имя
# переживает наследование, и разный порядок означал бы, что скрипт проверяет
# читаемость одного файла, а загрузчик читает другой (F2-gen-02).

# ── КОРЕНЬ УСТАНОВКИ ────────────────────────────────────────────────────────
# Умолчание пути к конфигам живёт ЗДЕСЬ и только здесь. До 21.09.2026 строка
# «/etc/harness/…» была размножена по 71 файлу, и харнес ставился ровно на одну
# машину: чужая, где нет права писать в /etc, установку не переживала.
# Владелец 21.09: «чтобы люди могли его ставить на любую машину».
#
# Порядок: HARNESS_HOME (окружение) → /etc/harness, если он существует →
# ~/.harness. Проверка существования, а не права записи: на нашей машине
# /etc/harness есть и читается всеми, а решать про запись — дело установки.
konf_koren() {
    if [ -n "${HARNESS_HOME:-}" ]; then printf '%s' "$HARNESS_HOME"; return 0; fi
    if [ -d /etc/harness ]; then printf '%s' /etc/harness; return 0; fi
    printf '%s' "${HOME:-/root}/.harness"
}

konf_zagruzit() {  # $@ — необязательно: ключи сверх объявленных в конфигах
    local koren; koren="$(konf_koren)"
    _konf_chitat okruzhenie "$koren" "${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-$koren/install.conf}}" \
        "${HARNESS_CONF:-$koren/harness.conf}" "$@"
}

# Режим ДВЕРИ: дверь (скрипт, что пускает необратимое по слову владельца) не берёт конфиг из
# окружения. Окружение назвало бы ей чужой паспорт, а конфиг bash исполняет как код: чужой
# harness.conf определил бы python3(), отвечающий «согласие есть» (АР-3, дыра б).
# Корень паспорта — /etc/harness, иначе ~/.harness агента; HARNESS_HOME, HARNESS_CONF,
# HARNESS_INSTALL_CONF и INSTALL_CONF не читаются. Единственная явная дверь — стенд пробы:
# HARNESS_DOOR_STAND_HOME называет каталог его паспорта, и дверь верит ей, только если сама
# (readlink -f вызывающего файла) лежит вне PROJECT_DIR паспорта машины, то есть это копия в
# стенде, а не настоящее дерево.
konf_dveri_puti() {  # $1 = файл двери; ставит INSTALL_CONF, HARNESS_INSTALL_CONF, HARNESS_CONF
    local door koren=/etc/harness proekt
    door="$(readlink -f "$1")"
    [ -d /etc/harness ] || koren="${HOME:-/root}/.harness"
    if [ -n "${HARNESS_DOOR_STAND_HOME:-}" ]; then
        proekt=$(sed -n "s/^[[:space:]]*PROJECT_DIR=[\"']\{0,1\}\([^\"'#]*\).*/\1/p" \
                 "$koren/install.conf" 2>/dev/null | tail -1)
        if [ -z "$proekt" ] || [[ "$door/" != "${proekt%/}/"* ]]; then koren="$HARNESS_DOOR_STAND_HOME"; fi
    fi
    KONF_DVERI_KOREN="$koren"
    INSTALL_CONF="$koren/install.conf"; HARNESS_INSTALL_CONF="$INSTALL_CONF"; HARNESS_CONF="$koren/harness.conf"
}

# Ключи двери — ТОЛЬКО из паспорта: окружение не перекрывает ни один. После законного «Да»
# дверь иначе исполнила бы чужой DB_ADMIN_EXEC и положила дамп и след в чужие каталоги.
# Поэтому каждый ключ, который дверь берёт умолчанием `X="${X:-…}"`, передаётся аргументом —
# загрузчик снимает его до чтения файла (проба носителя слова судит это разбором).
konf_zagruzit_dveri() {  # $@ — как у konf_zagruzit; дверь — вызывающий файл
    konf_dveri_puti "${BASH_SOURCE[1]}"
    _konf_chitat dveri "$KONF_DVERI_KOREN" "$INSTALL_CONF" "$HARNESS_CONF" "$@"
}

# $1 = режим (okruzhenie — окружение старше файла; dveri — окружение не читается вовсе),
# $2 = корень, $3 = install.conf, $4 = harness.conf, дальше — ключи сверх объявленных.
_konf_chitat() {
    local rezhim="$1" koren="$2" install_conf="$3" harness_conf="$4"
    shift 4

    local key val saved=()
    for key in $(printf '%s\n' "$@"; sed -n 's/^[[:space:]]*\([A-Z][A-Z0-9_]*\)=.*/\1/p' \
                 "$install_conf" "$harness_conf" 2>/dev/null | sort -u); do
        if [ "$rezhim" = dveri ]; then
            # Дверь исполняет необратимое по слову владельца: ключ из окружения она не берёт.
            unset "$key"
            continue
        fi
        val="${!key-}"
        # Пустое значение окружения значит «не задано» — берём из конфига.
        [ -n "$val" ] && saved+=("$key=$val")
    done
    # Места по умолчанию тоже могут прийти окружением и пережить `: "${X:=…}"`.
    # Сроки согласия (CONSENT_*) — настройка судьи двери: ключа может не быть в паспорте машины,
    # и тогда окружение продлило бы «Да», поэтому они снимаются по имени, а не по файлу.
    if [ "$rezhim" = dveri ]; then
        # shellcheck disable=SC2046
        unset LOG_DIR STATE_DIR HEARTBEAT_DIR PANEL_STATE_DIR SECRETS_DIR $(compgen -v CONSENT_)
    fi

    # shellcheck disable=SC1090
    [ -r "$install_conf" ] && source "$install_conf"
    # shellcheck disable=SC1090
    [ -r "$harness_conf" ] && source "$harness_conf"

    local pair
    for pair in ${saved[@]+"${saved[@]}"}; do
        printf -v "${pair%%=*}" '%s' "${pair#*=}"
    done

    # Места харнеса — ОТ КОРНЯ, и только если конфиг их не назвал. Умолчания
    # ставятся ЗДЕСЬ, после чтения файлов: поставленные выше, они неотличимы от
    # значения окружения и делают ключ конфига мёртвым
    # ([[default-above-loader-kills-setting]]).
    # До 21.09.2026 эти же умолчания были зашиты в 45 файлах строками
    # «/var/log/harness» и «/var/lib/harness» — на чужой машине харнес писал бы
    # в чужие каталоги или не писал вовсе.
    : "${LOG_DIR:=$koren/log}"
    # Корень СОСТОЯНИЯ назван отдельным ключом, а места под ним выводятся из
    # него. На нашей машине корней исторически два: конфиги в /etc/harness,
    # состояние в /var/lib/harness — и пока каждое место называлось своим
    # абсолютным путём, новое место (panel-state) требовало нового пути в
    # каждом читателе. Теперь новое место — это строка ниже, и только она.
    : "${STATE_DIR:=$koren/state}"
    : "${HEARTBEAT_DIR:=$STATE_DIR/heartbeat}"
    : "${PANEL_STATE_DIR:=$STATE_DIR/panel-state}"
    : "${SECRETS_DIR:=$STATE_DIR/secrets}"
    return 0
}

# Значение СТРОГО из файла, мимо окружения — для сторожей инвариантов.
#
# Улика 12.09.2026 (ревью кода, F1-gen-03): check-secrets.sh, сторож И-3, брал
# каталог сканирования из PROJECT_DIR. С защитой окружения любая переменная с
# этим родовым именем уводила проверку секретов на чужое дерево, и она молча
# отчитывалась «чисто». Сторожу инварианта окружение не хозяин.
#
# Порядок файлов тот же, что у konf_zagruzit: harness.conf читается вторым и
# потому побеждает — `tail -1`.
konf_iz_fajla() {  # $1 = ключ; печатает значение или пустое
    sed -n "s/^[[:space:]]*$1=[\"']\{0,1\}\([^\"'#]*\).*/\1/p" \
        "${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-$(konf_koren)/install.conf}}" \
        "${HARNESS_CONF:-$(konf_koren)/harness.conf}" 2>/dev/null | tail -1
}

# ── ЧЕМ ВХОДИТ АГЕНТ ────────────────────────────────────────────────────────
# Команда подъёма агента со СОРВАННЫМ носителем чужого входа.
#
# Откуда взята. Улика 10.10.2026, слово владельца: «при ротации сессий
# подключается аккаунт, в котором у меня закончился лимит». Переменная
# CLAUDE_CODE_OAUTH_TOKEN в порядке выбора учётки СТАРШЕ подписочного /login.
# Утром того же дня файловые носители убрали ВСЕ (vhod.sh, ~/.bashrc,
# ~/.profile, harness.conf) и проверили три оболочки — а ротация всё равно
# входила чужим ключом. Значение помнит СЕРВЕР tmux (поднят 30.09, когда
# переменная ещё стояла в профиле) и оболочка его панели: сервер — демон, он не
# умирает, оболочка панели живёт месяцами, и снять их память снаружи нельзя.
# Поэтому носитель срывается у САМОЙ команды запуска — на всех путях подъёма
# (новая сессия, send-keys в живую оболочку, respawn-pane).
#
# Имя переменной — не порог и не выбор установки, а имя чужой системы, поэтому
# живёт в коде; тот же носитель судит scripts/check-login-expiry.py.
NOSITEL_CHUZHOGO_VHODA="CLAUDE_CODE_OAUTH_TOKEN"

# Модель смены ставится ЗДЕСЬ, у единственной двери обоих путей подъёма: без
# --model claude берёт модель из ~/.claude/settings.json, а у замысла «смена —
# sonnet» (models.yaml) туда носителя не было. --model владельца в команде
# старше таблицы; нечитаемая таблица команду не меняет — model-for дал бы haiku.
komanda_agenta() {  # $1 = AGENT_START_CMD; печатает её же, без носителя входа
    local cmd="$1" first rest here table model
    first="${cmd%% *}"
    rest="${cmd#"$first"}"
    # Подстановка модели и обёртка личностью — два независимых шага: команда с
    # --model владельца обязана идти через носитель так же, как без него.
    # Команда, уже начатая с bash …/claude-run.sh, не claude и второй раз не оборачивается.
    if [ "$first" = "claude" ] || [[ "$first" == */claude ]]; then
        here="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.."
        if [[ "$cmd" != *" --model "* && "$cmd" != *" --model="* ]]; then
            table="${MODELS_CONF:-${PROJECT_DIR:-$here/..}/harness/config/models.yaml}"
            if [ -r "$table" ] && model=$(MODELS_CONF="$table" bash "$here/model-for.sh" смена) && [ -n "$model" ]; then
                rest=" --model $model$rest"
            fi
        fi
        cmd="bash ${PROJECT_DIR:-$here/..}/scripts/as-account.sh bash ${PROJECT_DIR:-$here/..}/scripts/claude-run.sh --класс оркестратор$rest"
    fi
    printf 'env -u %s %s' "$NOSITEL_CHUZHOGO_VHODA" "$cmd"
}

# Метки запуска демона (спека логирования §5.4): <имя>.start при старте и
# <имя>.end при выходе. Чистый bash: метки пишут и частые демоны. Каталог тот же,
# что у events.py state_dir().
harness_run_dir() { printf '%s' "${HARNESS_EVENTS_STATE:-${LOG_DIR:-/var/log/harness}/.events}/runs"; }

harness_run() {  # $1 = имя демона; ставит ловушку выхода для демонов без своей
    HARNESS_RUN_NAME="$1"; HARNESS_RUN_T0=$(date +%s)
    local dir stat
    dir="$(harness_run_dir)"
    stat="$(cat /proc/$$/stat 2>/dev/null)"
    # Поле 22 считается после последней «) »: имя процесса само может нести пробелы.
    { mkdir -p "$dir" && printf 'pid=%s started=%s proc_start=%s\n' "$$" "$HARNESS_RUN_T0" \
        "$(printf '%s' "${stat##*) }" | awk '{print $20}')" > "$dir/$1.start"; } \
        || echo "harness_run: метку $1.start записать не удалось" >&2
    trap 'rc=$?; harness_run_end "$rc"' EXIT
    return 0
}

harness_run_end() {  # $1 = код выхода демона; сам не выходит и код не меняет
    local dir now
    [ -n "${HARNESS_RUN_NAME:-}" ] || return 0
    dir="$(harness_run_dir)"; now=$(date +%s)
    { mkdir -p "$dir" && printf 'pid=%s exit=%s ended=%s seconds=%s\n' "$$" "$1" "$now" \
        "$((now - ${HARNESS_RUN_T0:-$now}))" > "$dir/$HARNESS_RUN_NAME.end"; } \
        || echo "harness_run_end: метку $HARNESS_RUN_NAME.end записать не удалось" >&2
    return 0
}

# Самотест: обе функции без него не проверял никто, а konf_iz_fajla — вход
# сторожа инварианта И-3 (ревью кода 12.09.2026, F2-gen-03).
konf_selftest() {
    # Помощник подключается ДО ловушки RETURN: она срабатывает и на конце
    # source внутри функции и снесла бы стенд, едва его заведя.
    source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/selftest_cases.sh"
    local T
    T=$(mktemp -d)
    trap 'rm -rf "$T"' RETURN
    printf 'PROJECT_DIR="/из-install"\nCOMMON_KEY="из-install"\n' > "$T/i.conf"
    printf 'COMMON_KEY="из-harness"\nTHRESHOLD=42\n' > "$T/h.conf"

    # Имена переменных ЛАТИНИЦЕЙ: bash кириллические не берёт и печатает в
    # ошибку само значение (улика 12.08.2026 — так утёк токен бота).

    local lib="${BASH_SOURCE[0]}" out
    out=$(HARNESS_INSTALL_CONF="$T/i.conf" HARNESS_CONF="$T/h.conf" PROJECT_DIR=из-окружения \
          bash -c 'source "$1"; konf_zagruzit; printf "%s|%s|%s" "$PROJECT_DIR" "$COMMON_KEY" "$THRESHOLD"' _ "$lib")
    case_check "БОЛЬНОЙ СЛУЧАЙ: окружение старше файла (улика про смену модели)" "из-окружения" \
          "$(printf '%s' "$out" | cut -d'|' -f1)"
    case_check "ключ в ОБОИХ файлах: harness.conf читается вторым и побеждает" "из-harness" \
          "$(printf '%s' "$out" | cut -d'|' -f2)"
    case_check "ключ, которого в окружении нет, берётся из harness.conf" "42" \
          "$(printf '%s' "$out" | cut -d'|' -f3)"

    case_check "konf_iz_fajla берёт значение мимо окружения (сторож И-3)" "/из-install" \
          "$(HARNESS_INSTALL_CONF="$T/i.conf" HARNESS_CONF="$T/h.conf" PROJECT_DIR=из-окружения \
             bash -c 'source "$1"; konf_iz_fajla PROJECT_DIR' _ "$lib")"
    case_check "отсутствующего ключа нет и в ответе — пусто, а не мусор" "" \
          "$(HARNESS_INSTALL_CONF="$T/i.conf" HARNESS_CONF="$T/h.conf" \
             bash -c 'source "$1"; konf_iz_fajla NO_SUCH_KEY' _ "$lib")"
    # БОЛЬНОЙ СЛУЧАЙ АР-4: режим двери — окружение не доживает до двери, ключ только из паспорта.
    printf 'DB_ADMIN_EXEC="из-паспорта"\n' >> "$T/h.conf"
    case_check "БОЛЬНОЙ СЛУЧАЙ АР-4: DB_ADMIN_EXEC окружением в режиме двери не доживает" "из-паспорта|пусто|$T/дверь/log" \
          "$(HARNESS_DOOR_STAND_HOME="$T/дверь" DB_ADMIN_EXEC=подлог EXTRA_KEY=подлог LOG_DIR=подлог \
             bash -c 'mkdir -p "$2"; cp "$3" "$2/install.conf"; cp "$4" "$2/harness.conf"; source "$1"
                      konf_zagruzit_dveri EXTRA_KEY; printf "%s|%s|%s" "$DB_ADMIN_EXEC" "${EXTRA_KEY:-пусто}" "${LOG_DIR:-пусто}"' \
             _ "$lib" "$T/дверь" "$T/i.conf" "$T/h.conf")"
    case_check "конфигов нет вовсе — загрузчик не роняет скрипт под set -e" "цел" \
          "$(HARNESS_INSTALL_CONF=/нет/такого HARNESS_CONF=/нет/такого \
             bash -c 'set -euo pipefail; source "$1"; konf_zagruzit && echo цел' _ "$lib")"

    # ── корень установки (владелец 21.09: «ставить на любую машину») ────────
    # БОЛЬНОЙ СЛУЧАЙ: на чужой машине нет /etc/harness и нет права его завести.
    # Пока умолчание было зашито в 71 файле, установка туда не доходила.
    mkdir -p "$T/дом"
    printf 'PROJECT_DIR="/из-чужого-дома"\n' > "$T/дом/install.conf"
    case_check "БОЛЬНОЙ СЛУЧАЙ: корень задан переменной — конфиг читается оттуда" "/из-чужого-дома" \
          "$(HARNESS_HOME="$T/дом" bash -c 'source "$1"; konf_zagruzit; printf "%s" "$PROJECT_DIR"' _ "$lib")"
    case_check "корень по умолчанию — /etc/harness, пока он есть" \
          "$([ -d /etc/harness ] && echo /etc/harness || echo "${HOME:-/root}/.harness")" \
          "$(bash -c 'source "$1"; konf_koren' _ "$lib")"
    case_check "нет /etc/harness и нет переменной — корень в доме пользователя" "$T/чужой/.harness" \
          "$(HOME="$T/чужой" bash -c 'source "$1"; if [ -d /etc/harness ]; then printf "%s" "$HOME/.harness"; else konf_koren; fi' _ "$lib")"
    # Явный путь конфига СТАРШЕ корня: иначе проба, подменившая один файл,
    # молча уехала бы на боевой (улика 12.09 — проба играла на боевом).
    case_check "HARNESS_INSTALL_CONF старше корня" "/из-install" \
          "$(HARNESS_HOME="$T/дом" HARNESS_INSTALL_CONF="$T/i.conf" HARNESS_CONF=/нет \
             bash -c 'source "$1"; konf_zagruzit; printf "%s" "$PROJECT_DIR"' _ "$lib")"

    # БОЛЬНОЙ СЛУЧАЙ: конфиг не назвал места — они обязаны лечь ПОД КОРЕНЬ, а не
    # в /var/log чужой машины.
    case_check "места от корня, когда конфиг молчит" "$T/дом/log|$T/дом/state/heartbeat|$T/дом/state/secrets" \
          "$(HARNESS_HOME="$T/дом" bash -c 'source "$1"; konf_zagruzit; printf "%s|%s|%s" "$LOG_DIR" "$HEARTBEAT_DIR" "$SECRETS_DIR"' _ "$lib")"
    # БОЛЬНОЙ СЛУЧАЙ: корень состояния назван в конфиге (наша машина — /var/lib),
    # а корень конфигов другой. Места обязаны ехать за STATE_DIR, иначе метки
    # панели легли бы в /etc/harness/state рядом с конфигами.
    printf 'STATE_DIR="/свой/двор"\n' >> "$T/дом/install.conf"
    case_check "heartbeat, panel-state и secrets едут за STATE_DIR" "/свой/двор/heartbeat|/свой/двор/panel-state|/свой/двор/secrets" \
          "$(HARNESS_HOME="$T/дом" bash -c 'source "$1"; konf_zagruzit; printf "%s|%s|%s" "$HEARTBEAT_DIR" "$PANEL_STATE_DIR" "$SECRETS_DIR"' _ "$lib")"
    # И обратное: место, названное поимённо, старше вывода из STATE_DIR.
    printf 'HEARTBEAT_DIR="/поимённо"\n' >> "$T/дом/install.conf"
    case_check "поимённое место старше вывода из STATE_DIR" "/поимённо" \
          "$(HARNESS_HOME="$T/дом" bash -c 'source "$1"; konf_zagruzit; printf "%s" "$HEARTBEAT_DIR"' _ "$lib")"
    # И обратное: конфиг назвал место — умолчание его НЕ перебивает.
    printf 'LOG_DIR="/из-конфига"\n' >> "$T/дом/install.conf"
    case_check "место из конфига старше умолчания от корня" "/из-конфига" \
          "$(HARNESS_HOME="$T/дом" bash -c 'source "$1"; konf_zagruzit; printf "%s" "$LOG_DIR"' _ "$lib")"

    # ── чем входит агент (больной случай 10.10.2026) ────────────────────────
    # Судится ПОВЕДЕНИЕ, а не текст команды: проба ставит носитель в окружение
    # и спрашивает запущенную команду, видит ли она его.
    case_check "БОЛЬНОЙ СЛУЧАЙ 10.10: носитель в окружении — запущенная команда его НЕ видит" "пусто" \
          "$(CLAUDE_CODE_OAUTH_TOKEN=чужой-ключ bash -c \
             'source "$1"; eval "$(komanda_agenta "printenv CLAUDE_CODE_OAUTH_TOKEN")" || printf пусто' \
             _ "$lib")"
    # Обратный случай: без срыва носитель виден. Иначе проба зелена сама по себе.
    case_check "без срыва носитель виден — проба мерит, а не совпадает" "чужой-ключ" \
          "$(CLAUDE_CODE_OAUTH_TOKEN=чужой-ключ printenv CLAUDE_CODE_OAUTH_TOKEN)"
    case_check "команда запуска доезжает целиком, с ключами модели" "env -u CLAUDE_CODE_OAUTH_TOKEN bash /selftest/scripts/as-account.sh bash /selftest/scripts/claude-run.sh --класс оркестратор --model claude-opus-5 --dangerously-skip-permissions" \
          "$(PROJECT_DIR=/selftest bash -c 'source "$1"; komanda_agenta "claude --model claude-opus-5 --dangerously-skip-permissions"' _ "$lib")"

    [ "$CASES_FAILED" = 0 ] && { echo "САМОТЕСТ ПРОЙДЕН: $CASES_PATHS путей, первым — больной случай"; return 0; }
    echo "САМОТЕСТ ПРОВАЛЕН"; return 1
}

# Файл и подключается (source), и запускается (`config.sh --selftest`).
if [ "${BASH_SOURCE[0]}" = "${0}" ] && [ "${1:-}" = "--selftest" ]; then
    konf_selftest
fi
