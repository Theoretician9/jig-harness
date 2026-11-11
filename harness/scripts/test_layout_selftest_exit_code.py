#!/usr/bin/env python3
"""Проба: код выхода самотеста раскладчика не портит его собственная уборка (Д-16).

Самотест печатал «SELFTEST: зелёный» и выходил кодом 1: хвост самотеста уже убрал
стенд, а EXIT-ловушка под `set -euo pipefail` делала chmod по убранному каталогу,
и упавший chmod менял `exit 0` на 1. Установщик судит самотест по коду под
pipefail, и установка с нуля вставала на шаге 7-а.

Полный самотест идёт ~12 минут, поэтому проба исполняет не его, а те же строки:
ловушки EXIT и хвост блока --selftest достаются РАЗБОРОМ раскладчика и исполняются
под тем же `set -euo pipefail`. Пустое извлечение — провал.

Случаи:
  1) хвост как есть при ok_flag=1 → «SELFTEST: зелёный» и код 0, стенд убран;
  2) стенд уже убран, exit 0 → код 0;
  3) стенд и внешний каталог (OUTSIDE) убраны, exit 0 → код 0;
  4) стенд жив, в нём каталог без права записи, exit 0 → код 0, стенд убран;
  5) стенд убран, exit 1 → код 1 (уборка не прячет провал);
  6) хвост как есть при ok_flag=0 → код 1.
Условие проба создаёт сама: свой каталог harness-run-* под TMPDIR.

Дверь мутаций: LAYOUT_SCRIPT — раскладчик, который судит проба.
Прогон: python3 scripts/test_layout_selftest_exit_code.py   Код: 0 — все случаи прошли.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def layout_script() -> Path:
    """Раскладчик лежит только в пакете: из пакета берётся рядом, из проекта — по каталогу пакета."""
    if os.environ.get("LAYOUT_SCRIPT"):
        return Path(os.environ["LAYOUT_SCRIPT"])
    beside = HERE.parent / "razlozhit-harnes.sh"
    if beside.is_file():
        return beside
    sys.path.insert(0, str(HERE / "lib"))
    import config
    return config.katalog_paketa() / "harness" / "razlozhit-harnes.sh"


def selftest_block(text: str) -> list[str]:
    """Строки блока `if [[ "${1:-}" == "--selftest" ]]; then … fi` верхнего уровня."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith('if [[ "${1:-}" == "--selftest" ]]')), None)
    if start is None:
        return []
    end = next((i for i in range(start + 1, len(lines)) if lines[i] == "fi"), None)
    return lines[start + 1:end] if end else []


def exit_traps(block: list[str]) -> list[str]:
    return [l.strip() for l in block if re.match(r"\s*trap\s.*\sEXIT\s*$", l)]


def tail(block: list[str]) -> list[str]:
    """Хвост самотеста — всё после последнего судимого случая (case_result)."""
    last = max((i for i, l in enumerate(block) if l.lstrip().startswith("case_result ")), default=None)
    return block[last + 1:] if last is not None else []


def run(work: Path, traps: list[str], body: str) -> subprocess.CompletedProcess:
    script = "\n".join(["set -euo pipefail", 'T=$(mktemp -d -t proba-razlozhit-XXXXXX)', *traps, body]) + "\n"
    return subprocess.run(["bash", "-c", script], env={**os.environ, "TMPDIR": str(work)},
                          capture_output=True, text=True, timeout=60)


def main() -> int:
    script = layout_script()
    if not script.is_file():
        print(f"САМОТЕСТ-КОД: нет раскладчика {script}")
        return 1
    block = selftest_block(script.read_text(encoding="utf-8"))
    traps, end = exit_traps(block), tail(block)
    if not block or not traps or not end:
        print(f"САМОТЕСТ-КОД: разбор раскладчика пуст (строк блока {len(block)}, "
              f"EXIT-ловушек {len(traps)}, строк хвоста {len(end)}) — судить нечего")
        return 1
    tail_text = "\n".join(end)
    cases = [
        ("1) хвост как есть, ok_flag=1 → зелёный и код 0", "ok_flag=1\n" + tail_text, 0, "SELFTEST: зелёный"),
        ("2) стенд убран, exit 0 → код 0", 'rm -rf "$T"; exit 0', 0, None),
        ("3) стенд и OUTSIDE убраны, exit 0 → код 0",
         'OUTSIDE=$(mktemp -d -t proba-razlozhit-outside-XXXXXX); rm -rf "$T" "$OUTSIDE"; exit 0', 0, None),
        ("4) стенд с каталогом без записи, exit 0 → код 0", 'mkdir -p "$T/ro/x"; chmod a-w "$T/ro"; exit 0', 0, None),
        ("5) стенд убран, exit 1 → код 1", 'rm -rf "$T"; exit 1', 1, None),
        ("6) хвост как есть, ok_flag=0 → код 1", "ok_flag=0\n" + tail_text, 1, None),
    ]
    failed = 0
    root = Path(tempfile.mkdtemp(prefix="harness-run-layout-exit-"))
    try:
        for name, body, want_rc, want_out in cases:
            work = Path(tempfile.mkdtemp(dir=root))
            r = run(work, traps, body)
            left = sorted(p.name for p in work.iterdir())
            ok = r.returncode == want_rc and not left and (want_out is None or want_out in r.stdout)
            failed += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {name}: код {r.returncode}, стендов осталось {len(left)}"
                  + ("" if ok else f"; вывод: {(r.stdout + r.stderr).strip()[-200:]!r}"))
    finally:
        subprocess.run(["chmod", "-R", "u+w", str(root)], stderr=subprocess.DEVNULL)
        shutil.rmtree(root, ignore_errors=True)
    print(f"САМОТЕСТ-КОД: случаев {len(cases)}, упало {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
