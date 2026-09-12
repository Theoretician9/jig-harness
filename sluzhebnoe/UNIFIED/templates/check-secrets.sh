#!/bin/bash
set -e
ENV_FILE="${1:-/opt/<проект>/secrets/.env}"

if [ ! -f "$ENV_FILE" ]; then
    echo "FAIL: $ENV_FILE not found"
    exit 1
fi

source "$ENV_FILE"
fail=0

check_not_default() {
    local val="$1"
    local name="$2"
    if [ "$val" = "changeme" ] || [ -z "$val" ]; then
        echo "FAIL: $name is default/empty"
        fail=1
    fi
}

check_min_length() {
    local val="$1"
    local name="$2"
    local min="$3"
    if [ ${#val} -lt "$min" ]; then
        echo "FAIL: $name too short (${#val} < $min)"
        fail=1
    fi
}

check_not_default "$SESSION_SECRET" "SESSION_SECRET"
check_min_length "$SESSION_SECRET" "SESSION_SECRET" 64
check_not_default "$TOTP_ENCRYPTION_KEY" "TOTP_ENCRYPTION_KEY"
check_not_default "$POSTGRES_PASSWORD" "POSTGRES_PASSWORD"
check_not_default "$GOOGLE_CLIENT_ID" "GOOGLE_CLIENT_ID"
check_not_default "$GOOGLE_CLIENT_SECRET" "GOOGLE_CLIENT_SECRET"

if [ $fail -eq 1 ]; then
    echo ""
    echo "SECRETS CHECK FAILED — fix /opt/<проект>/secrets/.env before deploying"
    exit 1
fi

echo "All secrets OK"
