# shellcheck shell=bash
# ACCOUNT_KEY_TROUBLE читает вызывающий после source:
# shellcheck disable=SC2034
# Ключ профиля `запуск` → CLAUDE_CODE_OAUTH_TOKEN (только окружение).
#
# Зовут as-account.sh (смена) и claude-run.sh (демоны идут мимо as-account.sh;
# без ключа claude берёт /login чужой организации и отвечает «organization has
# disabled Claude subscription access»). Без set -e/-u: любой сбой = тихо без
# ключа. Ключ не печатается и не попадает в доводы.
# Итог в ACCOUNT_KEY_TROUBLE: пусто — всё хорошо (или вход /login задан профилем),
# иначе причина для вести вызывающего. Нужен HERE — каталог scripts/.
account_key_export() {
    ACCOUNT_KEY_TROUBLE=""
    local verdict rc
    verdict="$(python3 "$HERE/lib/accounts.py" --файл-ключа-запуска 2>/dev/null)"
    rc=$?
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
        ACCOUNT_KEY_TROUBLE="$verdict"
    else
        ACCOUNT_KEY_TROUBLE="ОТКАЗ разбор запуска не удался (код $rc)"
    fi
    return 0
}
