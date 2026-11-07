#!/usr/bin/env bash
# Проба: у демона уборки диска есть самотест, и он безопасен.
#
# Почему. Приёмка доказывает демона только формой «--selftest», а уборка её не
# знала — задачи про уборку висели с исходом «доказать-нечем». Самотест демона,
# который удаляет, сам опаснее всего: зови он настоящую уборку, команда
# проверки чистила бы диск и двигала метку свежести.
#
# Что судится:
#   * `disk-cleanup.sh --selftest` завершается кодом 0 при любом давлении диска;
#   * боевое (стенд, который демон видит как свой конфиг) не тронуто: старые
#     файлы на местах, метки disk-pressure и disk-cleanup не сдвинуты и новых нет;
#   * настоящие чистильщики (docker, npm, pip, uv, канал) не зовутся;
#   * самотест живой: копия демона с перевёрнутым порогом давления валит его
#     кодом 1 (не 2 — «незнакомый ключ» живостью не считается).
#
# Боевое здесь — песочница mktemp: окружение чистое (env -i), конфиги, HOME и
# HARNESS_HOME — стендовые, инструменты машины подменены в PATH. Путь к демону —
# из HARNESS_DISK_CLEANUP ([[probe-loads-the-live-mechanism]]).
#
# Имена переменных ЛАТИНИЦЕЙ: bash не берёт кириллические идентификаторы.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/selftest_cases.sh"

DISK="${HARNESS_DISK_CLEANUP:-$HERE/../harness/demons/disk-cleanup.sh}"
STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT

# ── стенд: «боевое» глазами демона ──────────────────────────────────────────
ZH="$STAND/zhurnal"
mkdir -p "$STAND/bin" "$STAND/home" "$STAND/koren" "$STAND/proekt/scripts" \
         "$ZH/hb" "$ZH/inbox/секреты" "$STAND/mem-logs" "$STAND/mem-sessions"

for tool in docker npm pip uv curl; do
    printf '#!/usr/bin/env bash\nprintf "%s\\n" >> "%s/calls.log"\nexit 0\n' "$tool" "$STAND" > "$STAND/bin/$tool"
done
printf '#!/usr/bin/env bash\nprintf "tg_send\\n" >> "%s/calls.log"\nexit 0\n' "$STAND" \
    > "$STAND/proekt/scripts/tg_send.sh"
printf '#!/usr/bin/env bash\nexit 1\n' > "$STAND/bin/pgrep"
cat > "$STAND/bin/df" <<'DF'
#!/usr/bin/env bash
case "$*" in
    *pcent*) printf 'Use%%\n %s%%\n' "${STAND_DF_PCT:-40}" ;;
    *used*)  printf ' Used\n 1000000\n' ;;
    *)       exec /usr/bin/df "$@" ;;
esac
DF
chmod +x "$STAND/bin/"* "$STAND/proekt/scripts/tg_send.sh"

cat > "$STAND/install.conf" <<CONF
PROJECT_NAME="stend"
PROJECT_DIR="$STAND/proekt"
AGENT_USER="no-such-user-stend"
SECRETS_DIR="$STAND/home"
TG_TOKEN_FILE="$STAND/no-token"
TG_CHAT_ID=""
AUTONOMY="semi"
HEARTBEAT_DIR="$ZH/hb"
LOG_DIR="$ZH"
CONF
cat > "$STAND/harness.conf" <<CONF
DISK_MAX_PCT="85"
DISK_TRIGGER_PCT="80"
MEM_LOG_DIR="$STAND/mem-logs"
MEM_SESSION_DIR="$STAND/mem-sessions"
TMP_SWEEP_ENABLED="0"
DOCKER_SWEEP_ENABLED="0"
CONF

# Отходы, которые настоящий прогон удалил бы, и метки свежести — все старые.
for f in "$STAND/mem-logs/old.log" "$STAND/mem-sessions/old.jsonl" \
         "$ZH/old-run.log" "$ZH/inbox/секреты/123" "$ZH/hb/disk-pressure" "$ZH/hb/disk-cleanup"; do
    printf 'stend\n' > "$f"
    touch -d '2020-01-01 00:00' "$f"
done

inventory() {  # всё боевое стенда: путь и время правки (метки — это и есть время)
    find "$ZH/hb" "$ZH/inbox" "$ZH/old-run.log" "$STAND/mem-logs" "$STAND/mem-sessions" \
         -printf '%p %T@\n' 2>/dev/null | sort
}
changed() {  # имена боевого, пропавшего, сдвинутого или нового против описи до прогона
    diff <(printf '%s\n' "$BEFORE") <(inventory) | sed -n 's/^[<>] \([^ ]*\) .*/\1/p' \
        | xargs -r -n1 basename | sort -u | tr '\n' ' '
}

run_selftest() {  # $1 = демон, $2 = занятость диска в %; печатает вывод, код в $RC
    RC=0
    OUT=$(cd "$STAND" && env -i PATH="$STAND/bin:/usr/local/bin:/usr/bin:/bin" HOME="$STAND/home" \
          LC_ALL=C.UTF-8 HARNESS_HOME="$STAND/koren" \
          HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
          STAND_DF_PCT="$2" timeout 120 bash "$1" --selftest 2>&1) || RC=$?
}

BEFORE=$(inventory)
case_check "условие создано: на стенде 6 старых файлов боевого" "6" \
    "$(find "$ZH/hb" "$ZH/inbox/секреты" "$ZH/old-run.log" "$STAND/mem-logs" "$STAND/mem-sessions" -type f | wc -l)"

# ── БОЛЬНОЙ СЛУЧАЙ первым: давление не достигнуто ───────────────────────────
echo "── давление не достигнуто (диск 50% при пороге 80%) ──"
run_selftest "$DISK" 50
case_check "--selftest при 50%: код 0" "0" "$RC"
[ "$RC" = 0 ] || printf '%s\n' "$OUT" | tail -5 | sed 's/^/    | /'
case_check "--selftest при 50%: боевое не удалено, метки не сдвинуты, новых нет" "" "$(changed)"
case_check "--selftest при 50%: настоящие чистильщики и канал не звались" "" \
    "$(sort -u "$STAND/calls.log" 2>/dev/null | tr '\n' ' ')"

# ── давление достигнуто: самотест всё равно не трогает боевое ───────────────
echo "── давление достигнуто (диск 95%) ──"
run_selftest "$DISK" 95
case_check "--selftest при 95%: код 0" "0" "$RC"
case_check "--selftest при 95%: боевое не удалено, метки не сдвинуты, новых нет" "" "$(changed)"
case_check "--selftest при 95%: настоящие чистильщики и канал не звались" "" \
    "$(sort -u "$STAND/calls.log" 2>/dev/null | tr '\n' ' ')"

# ── живость: перевёрнутый порог давления самотест обязан поймать ────────────
echo "── мутант: порог давления перевёрнут ──"
TREE="$STAND/derevo"
mkdir -p "$TREE/harness/demons" "$TREE/scripts"
cp -r "$(dirname "$DISK")/../../scripts/lib" "$TREE/scripts/"
cp "$(dirname "$DISK")/../../scripts/disk-report.py" "$TREE/scripts/"
sed 's/-lt "\$DISK_TRIGGER_PCT"/-ge "$DISK_TRIGGER_PCT"/' "$DISK" > "$TREE/harness/demons/disk-cleanup.sh"
case_check "мутант построен (строка порога найдена и перевёрнута)" "да" \
    "$(cmp -s "$DISK" "$TREE/harness/demons/disk-cleanup.sh" && echo нет || echo да)"
run_selftest "$TREE/harness/demons/disk-cleanup.sh" 50
case_check "мутант: --selftest краснеет кодом 1" "1" "$RC"
case_check "мутант: боевое всё равно не тронуто" "" "$(changed)"

printf '\nСАМОТЕСТ УБОРКИ ДИСКА: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
