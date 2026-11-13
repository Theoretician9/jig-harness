#!/usr/bin/env bash
#
# gates-snapshot.sh — прогон ворот по снимку коммита HEAD, фоном.
#
# Полный прогон идёт десятки минут и читает дерево, в котором работает смена:
# правка посреди прогона меняла его итог, и дерево держали нетронутым. Снимок —
# git worktree коммита HEAD: смена правит живое дерево сразу после запуска.
#
# HEAD, а не индекс: worktree строится только из коммита, и итог относится к
# sha, который можно назвать и выкатить; незакоммиченное судит pre-commit.
#
# Место — ключ GATES_SNAPSHOT_DIR (пусто — ~/gates-snapshots агента): создаётся
# самой командой, без шага от root. Не под $LOG_DIR: удержание, секрет в логах
# и журналы-диск обходят $LOG_DIR рекурсивно и судили бы файлы снимка. Не под
# /tmp: уборка /tmp сносит каталоги посреди многочасового прогона.
# Корень снимку не экспортируется через PROJECT_DIR (секреты и самотест
# сторожа нарочно читают PROJECT_DIR только из паспорта) — ворота снимка
# берут корень от собственного файла, поэтому зовутся по пути в снимке.
#
# Запуск:
#   gates-snapshot.sh [аргументы gates.sh]   # например --всё или --только "секреты|гигиена"
# Печатает «снимок: <каталог>» и «журнал: <файл>»; последняя строка журнала —
# «ВОРОТА-КОНЕЦ rc=N» (N — число красных). Снимок убирается по завершении,
# брошенные (старше двух суток) — при следующем запуске.
set -euo pipefail

# shellcheck disable=SC1091
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/config.sh"
konf_zagruzit

# Двое суток: ночной --всё ограничен 20 часами, и каталог моложе может быть
# идущим прогоном.
KEEP_MIN=2880

# Фоновая половина: идёт из копии этого файла В СНИМКЕ, чтобы правка живого
# файла посреди прогона не подменила bash недочитанный текст.
run_in_snapshot() {  # $1 = живой корень, $2 = снимок, далее — аргументы gates.sh
    local live="$1" snap="$2" rc=0 names
    shift 2
    names="$(python3 -c 'import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))["живое"]
print(len(d), "—", ", ".join(x["проверка"] for x in d))' "$snap/harness/config/gates-live.yaml")" \
        || names="? — список harness/config/gates-live.yaml не прочитан"
    echo "снимок $(git -C "$snap" rev-parse HEAD) в $snap"
    echo "судит живое: $names"
    bash "$snap/scripts/gates.sh" "$@" || rc=$?
    git -C "$live" worktree remove --force "$snap" || echo "снимок не убран: $snap"
    echo "ВОРОТА-КОНЕЦ rc=$rc"
}

# Самый свежий снимок остаётся всегда: по нему смотрят, что судили последние ворота.
# Итог — в PRUNED_N и PRUNED_KB (их печатает --убрать).
prune_old() {  # $1 = живой корень, $2 = каталог снимков
    local d newest kb
    PRUNED_N=0 PRUNED_KB=0
    # Пустой каталог: ls отвечает кодом 2, а pipefail с set -e уронил бы весь прогон.
    newest="$(ls -1td -- "$2"/*/ 2>/dev/null | head -1)" || true
    for d in "$2"/*/; do
        [ "$d" = "$newest" ] && continue
        # Глоб «*/» разворачивает симлинк, а rm -rf -- "ссылка/" вычистил бы его цель.
        [ -L "${d%/}" ] && continue
        [ -n "$(find "$d" -maxdepth 0 -mmin +"$KEEP_MIN" 2>/dev/null)" ] || continue
        kb="$(du -sk -- "$d" 2>/dev/null | cut -f1)"
        git -C "$1" worktree remove --force "$d" 2>/dev/null || rm -rf -- "$d"
        [ -e "$d" ] || { PRUNED_N=$((PRUNED_N + 1)); PRUNED_KB=$((PRUNED_KB + ${kb:-0})); }
    done
    git -C "$1" worktree prune
}

if [ "${1:-}" = "--прогон" ]; then
    shift
    run_in_snapshot "$@"
    exit 0
fi

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
BASE="${GATES_SNAPSHOT_DIR:-$HOME/gates-snapshots}"
case "$(readlink -m "$BASE")/" in
    "$(readlink -m "$LOG_DIR")"/*)
        echo "gates-snapshot: место снимков $BASE под \$LOG_DIR — три ворот обходят журналы и судили бы снимок" >&2
        exit 2 ;;
    /tmp/*)
        echo "gates-snapshot: место снимков $BASE под /tmp — уборка /tmp снесла бы снимок посреди прогона" >&2
        exit 2 ;;
esac
# Уборка без прогона — шаг ночной уборки диска (disk-cleanup.sh): снимки копятся и
# тогда, когда ворота не гоняются, а без прогона prune_old не звался.
if [ "${1:-}" = "--убрать" ]; then
    [ -d "$BASE" ] || { echo "снимки ворот: каталога $BASE нет — убирать нечего"; exit 0; }
    prune_old "$ROOT" "$BASE"
    echo "снимки ворот: убрано $PRUNED_N, $((PRUNED_KB / 1024)) МБ; осталось $(find "$BASE" -mindepth 1 -maxdepth 1 -type d | wc -l) (самый свежий — всегда)"
    exit 0
fi
mkdir -p "$BASE"
chmod 700 "$BASE"
prune_old "$ROOT" "$BASE"

SHA="$(git -C "$ROOT" rev-parse HEAD)"
LABEL="$(date +%Y%m%d-%H%M%S)-${SHA:0:8}"
SNAP="$BASE/$LABEL"
LOG="$LOG_DIR/gates-snapshot-$LABEL.log"
git -C "$ROOT" worktree add -q --detach "$SNAP" "$SHA"
setsid nohup bash "$SNAP/scripts/gates-snapshot.sh" --прогон "$ROOT" "$SNAP" "$@" \
    > "$LOG" 2>&1 < /dev/null &
echo "снимок: $SNAP"
echo "журнал: $LOG"
