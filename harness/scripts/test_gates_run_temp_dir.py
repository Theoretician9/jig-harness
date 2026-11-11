#!/usr/bin/env python3
"""Проба: пробы ворот получают TMPDIR с префиксом harness-run-, и после прогона его нет.

Зачем: голый `mktemp -d` в пробе кладёт tmp.XXXX прямо в /tmp, а уборка /tmp такое считает
«неизвестным» и держит гигабайты. Условие создаёт сама проба: свой TMPDIR и подставной mktemp
в PATH, который пишет TMPDIR каждого вызова и зовёт настоящий. Судится самотест ворот
(gates.sh --selftest): он делает голые mktemp, как любая проба.
Итог: «TEMP DIR ВОРОТ: голых вызовов N, мимо harness-run- M, осталось K», код 0 при M == K == 0, N > 0.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

GATES = Path(os.environ.get("GATES_SCRIPT", Path(__file__).resolve().parent / "gates.sh"))
SHIM = """#!/usr/bin/env bash
printf '%s\\t%s\\n' "$*" "${TMPDIR:-}" >> "$MKTEMP_LOG"
exec /usr/bin/mktemp "$@"
"""


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="dc-gates-tmp-") as base:
        base = Path(base)
        (base / "bin").mkdir()
        (base / "run").mkdir()
        shim = base / "bin" / "mktemp"
        shim.write_text(SHIM)
        shim.chmod(0o755)
        log = base / "calls.tsv"
        env = {**os.environ, "TMPDIR": str(base / "run"), "MKTEMP_LOG": str(log),
               "PATH": f"{base / 'bin'}:{os.environ['PATH']}"}
        subprocess.run(["bash", str(GATES), "--selftest"], env=env, cwd=GATES.parent.parent,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        calls = [line.rstrip("\n").split("\t") for line in log.read_text().splitlines()] \
            if log.exists() else []
        bare = [c for c in calls if "-t" not in c[0].split() and "XXX" not in c[0]]
        wrong = [c for c in bare if not Path(c[1]).name.startswith("harness-run-")]
        left = sorted(p.name for p in (base / "run").iterdir())
        print(f"TEMP DIR ВОРОТ: голых вызовов {len(bare)}, мимо harness-run- {len(wrong)}, "
              f"осталось {len(left)} {left}")
        # Пустой список голых вызовов — провал: проба ничего не судила.
        return 0 if bare and not wrong and not left else 1


if __name__ == "__main__":
    sys.exit(main())
