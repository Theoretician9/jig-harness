#!/usr/bin/env bash
# Носитель: единственное место, где код зовёт claude как модель (спека
# личности и политики §4.3). Собирает личность класса сборщиком и отдаёт файл
# в --append-system-prompt-file.
#
#   bash scripts/claude-run.sh --класс <класс> <доводы claude…>
# Код возврата: 79 — личность не собрана, claude НЕ запущен; иначе код claude.
#
# Почему носитель сам судит файл: `--append-system-prompt-file ""` даёт у claude
# код 0 без личности, и сбой сборки не виден ни по коду, ни по выводу.
# --strict-mcp-config носитель не добавляет: смена стартует без него, и флаг
# снял бы с неё плагины с MCP; вызывающий кладёт его в доводы сам.
# Двери проб: CLAUDE_BIN, SELF_PROMPT_PY, HARNESS_ALERT_TASK.
set -u
export LC_ALL=C.UTF-8

# Корень — по пути самого файла, а не по текущему каталогу: носитель зовут из
# любого места, и приманка в cwd не должна подменить сборщика.
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/lib/config.sh"
konf_zagruzit
# shellcheck disable=SC1091
source "$HERE/lib/account_key.sh"

if [ "${1:-}" != "--класс" ] || [ -z "${2:-}" ]; then
    echo "claude-run: нужен первый довод --класс <класс>" >&2
    exit 2
fi
cls="$2"
shift 2

ALERT="${HARNESS_ALERT_TASK:-$HERE/alert-task.py}"
signal() { # $1 ключ, $2 заголовок; сбой сигнала не должен менять решение носителя
    python3 "$ALERT" --key "$1" --title "$2" --watcher claude-run \
        --text "claude-run.sh, класс «$cls»: $2" >/dev/null 2>&1 || true
}
refuse() {
    echo "claude-run: личность класса «$cls» не собрана: $1" >&2
    signal claude-run:self-prompt-failed "личность не собрана, модель не запущена"
    exit 79
}

[ -n "${LOG_DIR:-}" ] || refuse "пуст LOG_DIR"
f="$LOG_DIR/self-prompt/$cls.md"
mkdir -p "$LOG_DIR/self-prompt" || refuse "каталог $LOG_DIR/self-prompt не создан"

python3 "${SELF_PROMPT_PY:-$HERE/self-prompt.py}" --класс "$cls" --в "$f"
rc=$?
case $rc in
    0) ;;
    3) signal claude-run:self-etalon-missing "эталона личности нет, слой 1 взят из копии харнеса" ;;
    *) refuse "сборщик вернул $rc" ;;
esac
[ -s "$f" ] || refuse "файл пуст или не создан"
head -n 1 "$f" | grep -q '^Метка сборки: ' || refuse "в первой строке нет метки сборки"

# Демоны идут мимо as-account.sh; без ключа профиля claude берёт /login чужой
# организации. Вести нет: беду входа видит сторож входа. Ключ — только окружение.
[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && account_key_export
exec "${CLAUDE_BIN:-claude}" --append-system-prompt-file "$f" "$@"
