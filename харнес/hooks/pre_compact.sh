#!/usr/bin/env bash
# Новый хук (в UNIFIED/templates/hooks аналога нет); стиль и конфиг — как у соседних хуков набора.
#
# Хук PreCompact: перед сжатием контекста записать событие в журнал ротаций —
# по этим записям session-warden считает компакты сессии (потолок MAX_COMPACTS).
#
# ТОЛЬКО журнал (В-1): stdout PreCompact-хука до модели НЕ доходит, поэтому
# напоминание «сверься с памятью и передачей» печатает session_state.sh на
# SessionStart с source=compact — тот вывод модель видит.
#
# set -euo pipefail допустим: шагов мало и каждый рискованный (разбор stdin,
# запись в журнал) обёрнут в fail-open через `|| ...`.
set -euo pipefail

conf_path="${HARNESS_INSTALL_CONF:-/etc/harness/install.conf}"
log_dir="/var/log/harness"
if [ -r "$conf_path" ]; then
  # Окружение старше конфига: общий загрузчик вместо голого source (улика
  # 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
  # shellcheck disable=SC1091
  source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../lib/konf.sh"
  konf_zagruzit
  log_dir="${LOG_DIR:-$log_dir}"
fi

# Идентификатор сессии — из JSON-нагрузки хука на stdin; не разобрали — "unknown"
# (fail-open: журнал без session лучше, чем упавший хук).
payload=$(cat 2>/dev/null || true)
session=$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id") or "unknown")' 2>/dev/null || echo "unknown")
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Размер транскрипта на момент компакта — единственный честный замер окна:
# компакт наступает, когда окно кончилось, значит здесь видно, во сколько байт
# JSONL оно упирается на этой модели. Из него калибруется CTX_WINDOW_BYTES
# (harness.conf), которым session-warden считает проценты. Без этого поля
# калибровать нечем: пункт Д чек-листа приёмки требует «откалиброван по
# фактическому размеру транскрипта в момент ротации» (улика приёмки 11.08.2026).
transcript=$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("transcript_path") or "")' 2>/dev/null || echo "")
bytes=$(stat -c %s "$transcript" 2>/dev/null || echo 0)

mkdir -p "$log_dir" 2>/dev/null || true
printf '{"ts":"%s","event":"pre_compact","session":"%s","transcript_bytes":%s}\n' "$ts" "$session" "$bytes" >> "$log_dir/rotation.jsonl" 2>/dev/null \
  || echo "⚠ журнал $log_dir/rotation.jsonl недоступен — событие pre_compact не записано" >&2

exit 0
