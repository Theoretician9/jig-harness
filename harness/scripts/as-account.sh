#!/usr/bin/env bash
# Обёртка запуска смены: кладёт ключ профиля `запуск` в ОКРУЖЕНИЕ claude.
#
#   bash scripts/as-account.sh <команда и доводы…>
#
# Стоит в komanda_agenta между срывом чужого носителя и claude-run.sh. Без
# set -e и set -u-падений: последняя строка ВСЕГДА `exec "$@"` — любой сбой
# здесь значит смену на входе /login, а не смену, которая не встала.
# Ключ живёт только в переменной окружения: не в доводах, не в выводе, не в
# вести. Нет profiles.json — возможность не заведена, тихо `exec`.
# Двери: HARNESS_ACCOUNT_DIR, SECRETS_DIR, LOG_DIR, TG_SEND, HARNESS_ALERT_TASK.
export LC_ALL=C.UTF-8
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/lib/config.sh" 2>/dev/null && konf_zagruzit 2>/dev/null

MARK="${LOG_DIR:-/tmp}/as-account.весть"

verdict="$(python3 "$HERE/lib/accounts.py" --файл-ключа-запуска 2>/dev/null)"
rc=$?
trouble=""
if [ "$rc" = 0 ] && [ "$verdict" = login ]; then
    :
elif [ "$rc" = 0 ] && [ -n "$verdict" ] && [ -r "$verdict" ]; then
    CLAUDE_CODE_OAUTH_TOKEN="$(<"$verdict")"
    # Края срезаются, как у двери (accounts.ключ — strip): иначе «ключ\r» из
    # CRLF-файла уйдёт в окружение, хотя живой вызов судил ключ без него.
    CLAUDE_CODE_OAUTH_TOKEN="${CLAUDE_CODE_OAUTH_TOKEN#"${CLAUDE_CODE_OAUTH_TOKEN%%[![:space:]]*}"}"
    CLAUDE_CODE_OAUTH_TOKEN="${CLAUDE_CODE_OAUTH_TOKEN%"${CLAUDE_CODE_OAUTH_TOKEN##*[![:space:]]}"}"
    export CLAUDE_CODE_OAUTH_TOKEN
    [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ] || unset CLAUDE_CODE_OAUTH_TOKEN
elif [ "$rc" = 3 ]; then
    trouble="$verdict"
else
    trouble="ОТКАЗ разбор запуска не удался (код $rc)"
fi

# Весть одна на беду: та же причина подряд — молчим (respawn-петля не шумит);
# удачный подъём снимает метку, и новая беда снова громкая.
# Беда — работа модели, а не сведение владельцу: весть заводит карточку, удача её снимает.
if [ -z "$trouble" ]; then
    [ -f "$MARK" ] && python3 "${HARNESS_ALERT_TASK:-$HERE/alert-task.py}" \
        --resolve as-account:not-profile </dev/null >/dev/null 2>&1
    rm -f "$MARK" 2>/dev/null
elif [ "$(cat "$MARK" 2>/dev/null)" != "$trouble" ]; then
    mkdir -p "$(dirname "$MARK")" 2>/dev/null
    printf '%s' "$trouble" > "$MARK" 2>/dev/null
    bash "${TG_SEND:-$HERE/tg_send.sh}" \
        "Смена поднята на входе /login, а не на профиле аккаунта: ${trouble#ОТКАЗ }" \
        --task as-account:not-profile >/dev/null 2>&1
fi
exec "$@"
