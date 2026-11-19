#!/usr/bin/env python3
"""Актуальный RFC и цепочка его замен — по индексу rfc-editor, без модели.

    python3 scripts/standard-current.py 2616              ← номер (или RFC2616)
    python3 scripts/standard-current.py json              ← ключевые слова названия
    python3 scripts/standard-current.py internet message format

Номер: печатает цепочки Obsoleted-by до актуальных и у каждого актуального —
Updated-by (обновление не заменяет документ, но его ДОЛЖНО читать вместе с ним).
Слова: актуальные (никем не заменённые) RFC, в названии которых есть все слова.

Индекс — https://www.rfc-editor.org/rfc-index.txt (дверь RFC_INDEX_URL), кэш —
$LOG_DIR/rfc-index.txt, живёт неделю: индекс меняется несколько раз в месяц,
а замена стандарта — событие лет, так что недельный кэш не врёт о замене, а
каждый вызов не тянет 2 МБ. Сеть недоступна — ответ по старому кэшу с
предупреждением в stderr.

Коды: 0 — ответ есть; 1 — не найдено или индекса нет; 2 — нет довода.
Проба: scripts/test_standard_current.py (записанный фрагмент настоящего индекса).
"""
from __future__ import annotations

import os
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import config                                             # noqa: E402

INDEX_URL = "https://www.rfc-editor.org/rfc-index.txt"
CACHE_TTL = 7 * 24 * 3600
SEARCH_LIMIT = 15

ENTRY_START = re.compile(r"^(\d+) ")
FIELD = re.compile(r"\((Obsoleted by|Updated by|Status:) ([^)]*)\)")
RFC_REF = re.compile(r"RFC(\d+)")


def cache_path() -> Path:
    return Path(config.log_dir()) / "rfc-index.txt"


def fetch(target: Path) -> bool:
    url = os.environ.get("RFC_INDEX_URL") or INDEX_URL
    try:
        with urllib.request.urlopen(url, timeout=60) as answer:
            data = answer.read()
    except OSError as error:
        print(f"standard-current: индекс не скачан ({url}): {error}", file=sys.stderr)
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
    return True


def load_index() -> str | None:
    cache = cache_path()
    fresh = cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL
    if not fresh and not fetch(cache):
        if not cache.exists():
            return None
        print(f"standard-current: отвечаю по старому кэшу {cache}", file=sys.stderr)
    return cache.read_text(encoding="utf-8", errors="replace")


def parse(text: str) -> dict[int, dict]:
    """Запись индекса — строка с номером в первой колонке и строки с отступом под ней.

    Примеры в шапке индекса набраны с отступом, поэтому за записи не идут.
    """
    entries, current = {}, None
    for line in text.splitlines():
        start = ENTRY_START.match(line)
        if start:
            current = entries.setdefault(int(start.group(1)), {"text": line[start.end():]})
        elif not line.strip():
            current = None
        elif current is not None and line.startswith(" "):
            current["text"] += " " + line.strip()
    for entry in entries.values():
        text = entry["text"]
        entry["title"] = text.split(". ", 1)[0].rstrip(".")
        entry["issued"] = text.strip() != "Not Issued."
        fields = {name: value for name, value in FIELD.findall(text)}
        entry["obsoleted_by"] = [int(n) for n in RFC_REF.findall(fields.get("Obsoleted by", ""))]
        entry["updated_by"] = [int(n) for n in RFC_REF.findall(fields.get("Updated by", ""))]
        entry["status"] = fields.get("Status:", "").strip()
    return entries


def chains(index: dict, number: int, path: tuple = ()) -> list[tuple]:
    """Все пути по Obsoleted-by от номера до незаменённых (или отсутствующих в индексе)."""
    path = path + (number,)
    successors = [n for n in index.get(number, {}).get("obsoleted_by", []) if n not in path]
    if not successors:
        return [path]
    return [p for n in successors for p in chains(index, n, path)]


def describe(index: dict, number: int) -> str:
    entry = index.get(number)
    if entry is None:
        return f"RFC {number} — нет в индексе"
    line = f"RFC {number} — {entry['title']} [{entry['status'] or '?'}]"
    if entry["updated_by"]:
        line += "; обновлён: " + ", ".join(f"RFC {n}" for n in entry["updated_by"])
    return line


def answer_number(index: dict, number: int) -> int:
    entry = index.get(number)
    if entry is None:
        print(f"RFC {number} нет в индексе", file=sys.stderr)
        return 1
    if not entry["issued"]:
        print(f"RFC {number} не издан (Not Issued)", file=sys.stderr)
        return 1
    paths = chains(index, number)
    if paths == [(number,)]:
        print(f"RFC {number} актуален, никем не заменён")
    else:
        print(f"RFC {number} — {entry['title']} — заменён; цепочки:")
        for path in paths:
            print("  " + " → ".join(str(n) for n in path))
    print("актуальные:")
    for current in sorted({path[-1] for path in paths}):
        print("  " + describe(index, current))
    return 0


def answer_words(index: dict, words: list[str]) -> int:
    wanted = [w.lower() for w in words]
    found = [n for n, e in index.items()
             if e["issued"] and not e["obsoleted_by"]
             and all(w in e["title"].lower() for w in wanted)]
    if not found:
        print(f"актуальных RFC со словами «{' '.join(words)}» в названии нет", file=sys.stderr)
        return 1
    found.sort(reverse=True)
    print(f"по словам «{' '.join(words)}»: {len(found)}, новые первыми")
    print("актуальные:")
    for number in found[:SEARCH_LIMIT]:
        print("  " + describe(index, number))
    if len(found) > SEARCH_LIMIT:
        print(f"  … ещё {len(found) - SEARCH_LIMIT}: уточни слова")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    text = load_index()
    if text is None:
        print("standard-current: индекса нет — ни кэша, ни сети", file=sys.stderr)
        return 1
    index = parse(text)
    number = re.fullmatch(r"(?i)(?:rfc\s*)?(\d+)", " ".join(argv))
    if number:
        return answer_number(index, int(number.group(1)))
    return answer_words(index, argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
