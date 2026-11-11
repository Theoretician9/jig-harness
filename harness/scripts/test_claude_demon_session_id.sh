#!/usr/bin/env bash
# Проба: id сессии обёртки демона на машине без uuidgen (Д-22, живая установка R7 11.11.2026).
#
# claude-demon.sh берёт id сессии у `uuidgen`, а шаг 2 установщика пакет uuid-runtime
# не ставит: на образе стенда R7 его не было — «uuidgen: command not found», и claude
# получил `--session-id ""`. Пустой id настоящий claude отвергает, и разрешитель токенов
# теряет, чей это вызов. Правило: id сессии — настоящий UUID и без uuidgen в PATH.
#
# Условие проба создаёт сама: свой PATH — ссылки на все программы PATH, КРОМЕ uuidgen
# (проверяется: `command -v uuidgen` в нём пуст), свои конфиги и LOG_DIR, подставные
# claude (пишет свои доводы), сборщик личности и писатель сигналов. Проект стенда
# держит scripts/ ссылкой на каталог обёртки: настоящие claude-run.sh, pause.py,
# limit-in-log.py. Сети и модели нет.
# Дверь мутаций: CLAUDE_DEMON_UNDER_TEST.
set -uo pipefail
export LC_ALL=C.UTF-8
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
OBERTKA="${CLAUDE_DEMON_UNDER_TEST:-$HERE/claude-demon.sh}"
[ -f "$OBERTKA" ] || { echo "FAIL: обёртки нет ($OBERTKA)"; exit 1; }
fail=0; total=0
ok()  { total=$((total + 1)); echo "ok:   $1"; }
bad() { total=$((total + 1)); echo "FAIL: $1"; fail=$((fail + 1)); }
T=$(mktemp -d "${TMPDIR:-/tmp}/harness-run-XXXXXX"); trap 'rm -rf "$T"' EXIT

# PATH без uuidgen: первое вхождение каждого имени, как его нашла бы оболочка.
mkdir -p "$T/nouuid" "$T/bin" "$T/etc" "$T/log" "$T/hb" "$T/proekt"
IFS=: read -ra dirs <<<"$PATH"
for d in "${dirs[@]}"; do
  [ -d "$d" ] || continue
  for f in "$d"/*; do
    n=${f##*/}
    [ "$n" = uuidgen ] && continue
    [ -x "$f" ] && [ ! -d "$f" ] && [ ! -e "$T/nouuid/$n" ] && ln -s "$f" "$T/nouuid/$n"
  done
done
cat > "$T/bin/claude" <<'CL'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$CLAUDE_DOVODY"
printf '{"type":"result","subtype":"success","is_error":false,"result":"ответ модели"}\n'
CL
cat > "$T/bin/tg_send.sh" <<'TG'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TG_VYHOD"
TG
cat > "$T/bin/self-prompt.py" <<'SP'
import os, sys
a = sys.argv[1:]
out = a[a.index("--в") + 1]
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8") as fh:
    fh.write("Метка сборки: проба-uuid\nличность\n")
SP
printf 'import sys\n' > "$T/bin/alert-task.py"
chmod +x "$T/bin/claude" "$T/bin/tg_send.sh"
ln -s "$HERE" "$T/proekt/scripts"
: > "$T/etc/install.conf"; : > "$T/etc/harness.conf"

NOPATH="$T/bin:$T/nouuid"
[ -z "$(PATH="$NOPATH" command -v uuidgen)" ] && ok "условие стенда: в PATH стенда uuidgen нет" \
  || bad "условие стенда: uuidgen в PATH стенда есть — проба судит не то"
for need in bash jq python3 mktemp tee date; do
  [ -n "$(PATH="$NOPATH" command -v "$need")" ] || bad "условие стенда: в PATH стенда нет $need"
done

out=$(env -i HOME="$T" LC_ALL=C.UTF-8 PATH="$NOPATH" \
  HARNESS_HOME="$T/etc" HARNESS_INSTALL_CONF="$T/etc/install.conf" HARNESS_CONF="$T/etc/harness.conf" \
  PROJECT_DIR="$T/proekt" LOG_DIR="$T/log" HEARTBEAT_DIR="$T/hb" STATE_DIR="$T/state" \
  TG_SEND="$T/bin/tg_send.sh" TG_VYHOD="$T/tg.txt" CLAUDE_BIN="$T/bin/claude" CLAUDE_DOVODY="$T/dovody.txt" \
  SELF_PROMPT_PY="$T/bin/self-prompt.py" HARNESS_ALERT_TASK="$T/bin/alert-task.py" \
  timeout 60 bash "$OBERTKA" проба-демон -p "задание" 2>&1); rc=$?

uuid_re='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
[ -s "$T/dovody.txt" ] && ok "условие стенда: подставной claude позван" \
  || bad "условие стенда: claude не позван (код $rc): $(tail -3 <<<"$out" | tr '\n' ' ')"
grep -q "uuidgen" <<<"$out" \
  && bad "без uuidgen: обёртка падает на нём ($(grep -m1 uuidgen <<<"$out"))" \
  || ok "без uuidgen: обёртка его не зовёт"
sid=$(awk 'prev == "--session-id" {print; exit} {prev = $0}' "$T/dovody.txt" 2>/dev/null)
grep -qE "$uuid_re" <<<"$sid" && ok "без uuidgen: claude получил --session-id UUID ($sid)" \
  || bad "без uuidgen: claude получил --session-id «$sid» — не UUID"
start_sid=$(jq -r 'select(.event == "demon.start") | .session_id' "$T/log/demon-spend.jsonl" 2>/dev/null | tail -1)
grep -qE "$uuid_re" <<<"$start_sid" && [ "$start_sid" = "$sid" ] \
  && ok "без uuidgen: строка demon.start несёт тот же UUID" \
  || bad "без uuidgen: в demon.start session_id «$start_sid» (у claude «$sid»)"
[ "$rc" = 0 ] && ok "без uuidgen: обёртка выходит с кодом 0" || bad "без uuidgen: код обёртки $rc"

echo "итого случаев $total, неудач $fail"
[ "$fail" = 0 ]
