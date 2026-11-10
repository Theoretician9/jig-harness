#!/usr/bin/env bash
# Проба повтора квитанции «принял»: сеть упала — повторить, ответ API с ошибкой — нет.
# Условие своё: подставной curl в своём PATH, функция берётся из боевого файла
# разбором, а не копией. Мутация: HARNESS_DISPATCHER=<копия с мутацией>.
set -uo pipefail
SRC="${HARNESS_DISPATCHER:-$(dirname "$(readlink -f "$0")")/tg-dispatcher.sh}"
T=$(mktemp -d /tmp/proba-ack-retry-XXXXXX); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin"
sed -n '/^kvitanciya() {/,/^}/p' "$SRC" > "$T/fn.sh"
[ -s "$T/fn.sh" ] || { echo "ПРОБА КРАСНАЯ: функция kvitanciya не найдена"; exit 1; }
# curl: первые FAIL_N вызовов — rc=28, потом ответ из $REPLY
cat > "$T/bin/curl" <<'C'
#!/usr/bin/env bash
cat >/dev/null
n=$(cat "$STATE/n" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$STATE/n"
[ "$n" -le "$FAIL_N" ] && exit 28
printf '%s' "$REPLY"
C
chmod +x "$T/bin/curl"
run() {  # FAIL_N REPLY → число вызовов curl и события
    rm -f "$T/n" "$T/ev" "$T/say"
    ( export PATH="$T/bin:$PATH" STATE="$T" FAIL_N="$1" REPLY="$2" KVITANCIYA_PAUZA_S=0
      API=x LOG_DIR="$T"
      say() { echo "$*" >> "$T/say"; }
      harness_event() { echo "$4" >> "$T/ev"; }
      . "$T/fn.sh"; kvitanciya 1 ts 2 текст )
    echo "$(cat "$T/n") $(tr '\n' ,< "$T/ev")"
}
bad=0
check() { [ "$2" = "$3" ] || { echo "КРАСНО: $1: ждали «$2», вышло «$3»"; bad=$((bad+1)); }; }
OK='{"ok":true,"result":{"message_id":5}}'
check "сеть упала раз, потом ушло" "2 ok," "$(run 1 "$OK")"
check "сеть упала дважды, третья ушла" "3 ok," "$(run 2 "$OK")"
check "сеть мертва — три попытки и error" "3 error," "$(run 9 "")"
check "ответ API с ошибкой не повторяется" "1 error," "$(run 0 '{"ok":false,"description":"x"}')"
check "здоровая отправка — один вызов" "1 ok," "$(run 0 "$OK")"
[ "$bad" = 0 ] && { echo "ПРОБА ЗЕЛЁНАЯ: 5 путей"; exit 0; }
echo "ПРОБА КРАСНАЯ: $bad"; exit 1
