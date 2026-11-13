#!/usr/bin/env python3
"""Помощник кэша ворот: хеши входов механизмов, список последовательных проверок, сжатие кэша.

Зовёт его только scripts/lib/gates_check.sh (один раз за прогон на каждую из команд):

    gates_cache.py inputs --mechanisms <mechanisms.yaml> --root <корень>
        строки «<файл>\\t<хеш входов>» | «<файл>\\tNOINPUTS» | «<файл>\\tMISSING <шаблон>»
    gates_cache.py serial --file <gates-serial.yaml>
        строки «<проверка>\\t<группа>»
    gates_cache.py compact <gates-cache.tsv>
        оставляет по последней строке на ключ не старше 14 суток

Вход механизма — путь или шаблон от корня проекта (`**` рекурсивно; абсолютный путь и
`$ПЕРЕМЕННАЯ` допустимы — пакет лежит вне проекта). Сам скрипт механизма входит всегда:
правка пробы сбрасывает её кэш. Шаблон без единого файла — MISSING: опечатка в шаблоне
иначе молча выбросила бы настоящий вход из ключа, и проба получила бы ложный зелёный.

    python3 scripts/gates_cache.py --selftest
"""
from __future__ import annotations

import fcntl
import glob
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path

import yaml

KEEP_DAYS = 14
SKIP_PARTS = ("__pycache__",)


def load_yaml(path: str):
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    with open(path, encoding="utf-8") as fh:
        return yaml.load(fh, Loader=loader)  # noqa: S506 — безопасный загрузчик выбран выше


def starter_dir() -> str:
    """Каталог пакета: $HARNESS_STARTER, иначе тот, что знает конфиг харнеса."""
    env = os.environ.get("HARNESS_STARTER")
    if env:
        return env
    sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
    import config  # noqa: PLC0415 — нужен только шаблонам с этой переменной
    return str(config.katalog_paketa())


def expand(pattern: str, root: str) -> list[str]:
    if "HARNESS_STARTER" in pattern:
        os.environ["HARNESS_STARTER"] = starter_dir()
    pattern = os.path.expandvars(pattern)
    found = glob.glob(os.path.join(root, pattern), recursive=True)
    return sorted(p for p in found if os.path.isfile(p)
                  and not any(part in SKIP_PARTS for part in Path(p).parts)
                  and not p.endswith(".pyc"))


class Hasher:
    """Хеш содержимого файла считается раз за прогон: входы механизмов пересекаются."""

    def __init__(self) -> None:
        self.memo: dict[str, str] = {}

    def file(self, path: str) -> str:
        if path not in self.memo:
            try:
                self.memo[path] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except OSError:
                self.memo[path] = "unreadable"
        return self.memo[path]


def inputs_digest(files: list[str], hasher: Hasher) -> str:
    text = "".join(f"{p}\0{hasher.file(p)}\n" for p in files)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mechanism_line(mech: dict, root: str, hasher: Hasher) -> str:
    script = mech["файл"]
    patterns = mech.get("входы") or []
    if not patterns:
        return f"{script}\tNOINPUTS"
    files = {os.path.join(root, script)}
    for pattern in patterns:
        matched = expand(str(pattern), root)
        if not matched:
            return f"{script}\tMISSING {pattern}"
        files.update(matched)
    return f"{script}\t{inputs_digest(sorted(files), hasher)}"


def cmd_inputs(mechanisms: str, root: str) -> int:
    hasher = Hasher()
    for mech in load_yaml(mechanisms)["механизмы"]:
        if mech.get("файл"):
            print(mechanism_line(mech, root, hasher))
    return 0


def cmd_serial(path: str) -> int:
    for group, body in (load_yaml(path).get("группы") or {}).items():
        for check in body.get("проверки") or []:
            print(f"{check}\t{group}")
    return 0


def compact_rows(rows: list[str], now: float) -> list[str]:
    """Последняя строка каждого ключа, не старше KEEP_DAYS. Порядок — по первому появлению."""
    last: dict[str, str] = {}
    for row in rows:
        parts = row.split("\t")
        if len(parts) >= 2 and parts[1].isdigit():
            last[parts[0]] = row
    limit = now - KEEP_DAYS * 86400
    return [r for r in last.values() if int(r.split("\t")[1]) >= limit]


def cmd_compact(path: str) -> int:
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = Path(path).read_text(encoding="utf-8").splitlines()
        kept = compact_rows(rows, time.time())
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".gates-cache-")
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write("".join(r + "\n" for r in kept))
        os.replace(tmp, path)
    return 0


def selftest() -> int:
    ok = True

    def case(name: str, cond: bool) -> None:
        nonlocal ok
        ok = ok and cond
        print(f"  {'ок' if cond else 'ПЛОХО'} · {name}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "d").mkdir()
        (root / "d/a.txt").write_text("1", encoding="utf-8")
        (root / "s.sh").write_text("x", encoding="utf-8")
        hasher = Hasher()
        described = {"файл": "s.sh", "входы": ["d/*.txt"]}
        first = mechanism_line(described, str(root), hasher)
        (root / "d/a.txt").write_text("2", encoding="utf-8")
        second = mechanism_line(described, str(root), Hasher())
        case("правка входа меняет хеш", first != second and "\t" in first and "MISSING" not in first)
        case("нет входов — NOINPUTS", mechanism_line({"файл": "s.sh"}, str(root), hasher).endswith("NOINPUTS"))
        case("шаблон без файлов — MISSING",
             "MISSING nope/*" in mechanism_line({"файл": "s.sh", "входы": ["nope/*"]}, str(root), hasher))
        (root / "s.sh").write_text("y", encoding="utf-8")
        case("правка самого скрипта меняет хеш", mechanism_line(described, str(root), Hasher()) != second)
    now = 2_000_000_000.0
    rows = [f"k1\t{int(now) - 10}\tn\t0\ta", f"k1\t{int(now) - 5}\tn\t3\tb", f"k2\t{int(now) - 99 * 86400}\tn\t0\tc"]
    kept = compact_rows(rows, now)
    case("сжатие: последняя строка ключа, старое уходит", kept == [rows[1]])
    print("итог:", "зелёно" if ok else "КРАСНО")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if argv[:1] == ["--selftest"]:
        return selftest()
    if argv[:1] == ["inputs"] and len(argv) == 5 and argv[1] == "--mechanisms" and argv[3] == "--root":
        return cmd_inputs(argv[2], argv[4])
    if argv[:1] == ["serial"] and len(argv) == 3 and argv[1] == "--file":
        return cmd_serial(argv[2])
    if argv[:1] == ["compact"] and len(argv) == 2:
        return cmd_compact(argv[1])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
