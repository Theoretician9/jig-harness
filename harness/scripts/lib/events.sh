#!/usr/bin/env bash
# Дверь bash к общей записи событий: логика одна, в scripts/lib/events.py.
# Код возврата всегда 0 — писатель под `set -e` не умирает из-за журнала;
# неудача записи видна в stderr и в $LOG_DIR/.events/lost.jsonl.
#
#   source scripts/lib/events.sh
#   harness_event <журнал> <src> <событие> <исход> [ключ=значение …] [--json '<data>']

EVENTS_PY="${HARNESS_EVENTS_LIB:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/events.py}"

harness_event() {
    python3 -I "$EVENTS_PY" emit "$@" || true
    return 0
}
