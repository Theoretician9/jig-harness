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
#   source "$HERE/lib/konf.sh"
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

konf_zagruzit() {  # $@ — необязательно: ключи сверх объявленных в конфигах
    local install_conf="${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-/etc/harness/install.conf}}"
    local harness_conf="${HARNESS_CONF:-/etc/harness/harness.conf}"

    local key val saved=()
    for key in $(printf '%s\n' "$@"; sed -n 's/^[[:space:]]*\([A-Z][A-Z0-9_]*\)=.*/\1/p' \
                 "$install_conf" "$harness_conf" 2>/dev/null | sort -u); do
        val="${!key-}"
        # Пустое значение окружения значит «не задано» — берём из конфига.
        [ -n "$val" ] && saved+=("$key=$val")
    done

    # shellcheck disable=SC1090
    [ -r "$install_conf" ] && source "$install_conf"
    # shellcheck disable=SC1090
    [ -r "$harness_conf" ] && source "$harness_conf"

    local pair
    for pair in ${saved[@]+"${saved[@]}"}; do
        printf -v "${pair%%=*}" '%s' "${pair#*=}"
    done
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
        "${HARNESS_INSTALL_CONF:-${INSTALL_CONF:-/etc/harness/install.conf}}" \
        "${HARNESS_CONF:-/etc/harness/harness.conf}" 2>/dev/null | tail -1
}

# Самотест: обе функции без него не проверял никто, а konf_iz_fajla — вход
# сторожа инварианта И-3 (ревью кода 12.09.2026, F2-gen-03).
konf_selftest() {
    local T ok=1
    T=$(mktemp -d)
    trap 'rm -rf "$T"' RETURN
    printf 'PROJECT_DIR="/из-install"\nCOMMON_KEY="из-install"\n' > "$T/i.conf"
    printf 'COMMON_KEY="из-harness"\nTHRESHOLD=42\n' > "$T/h.conf"

    # Имена переменных ЛАТИНИЦЕЙ: bash кириллические не берёт и печатает в
    # ошибку само значение (улика 12.08.2026 — так утёк токен бота).
    proba() {  # $1 = что проверяем, $2 = получено, $3 = ждём
        if [ "$2" = "$3" ]; then
            printf '  ок    %s\n' "$1"
        else
            printf '  ПЛОХО %s: ждали «%s», получили «%s»\n' "$1" "$3" "$2"; ok=0
        fi
    }

    local lib="${BASH_SOURCE[0]}" out
    out=$(HARNESS_INSTALL_CONF="$T/i.conf" HARNESS_CONF="$T/h.conf" PROJECT_DIR=из-окружения \
          bash -c 'source "$1"; konf_zagruzit; printf "%s|%s|%s" "$PROJECT_DIR" "$COMMON_KEY" "$THRESHOLD"' _ "$lib")
    proba "БОЛЬНОЙ СЛУЧАЙ: окружение старше файла (улика про смену модели)" \
          "$(printf '%s' "$out" | cut -d'|' -f1)" "из-окружения"
    proba "ключ в ОБОИХ файлах: harness.conf читается вторым и побеждает" \
          "$(printf '%s' "$out" | cut -d'|' -f2)" "из-harness"
    proba "ключ, которого в окружении нет, берётся из harness.conf" \
          "$(printf '%s' "$out" | cut -d'|' -f3)" "42"

    proba "konf_iz_fajla берёт значение мимо окружения (сторож И-3)" \
          "$(HARNESS_INSTALL_CONF="$T/i.conf" HARNESS_CONF="$T/h.conf" PROJECT_DIR=из-окружения \
             bash -c 'source "$1"; konf_iz_fajla PROJECT_DIR' _ "$lib")" "/из-install"
    proba "отсутствующего ключа нет и в ответе — пусто, а не мусор" \
          "$(HARNESS_INSTALL_CONF="$T/i.conf" HARNESS_CONF="$T/h.conf" \
             bash -c 'source "$1"; konf_iz_fajla NO_SUCH_KEY' _ "$lib")" ""
    proba "конфигов нет вовсе — загрузчик не роняет скрипт под set -e" \
          "$(HARNESS_INSTALL_CONF=/нет/такого HARNESS_CONF=/нет/такого \
             bash -c 'set -euo pipefail; source "$1"; konf_zagruzit && echo цел' _ "$lib")" "цел"

    [ "$ok" = 1 ] && { echo "САМОТЕСТ ПРОЙДЕН: 6 путей, первым — больной случай"; return 0; }
    echo "САМОТЕСТ ПРОВАЛЕН"; return 1
}

# Файл и подключается (source), и запускается (`konf.sh --selftest`).
if [ "${BASH_SOURCE[0]}" = "${0}" ] && [ "${1:-}" = "--selftest" ]; then
    konf_selftest
fi
