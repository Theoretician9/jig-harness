#!/usr/bin/env python3
"""Проба: самотест раскладки убирает стенд и при обрыве по timeout (SIGTERM).

Без этого каждый оборванный прогон оставлял в /tmp стенд на 0,7–1,7 ГБ, и уборка /tmp
ждала его срок. Условие создаёт сама проба: свой TMPDIR, свой предел; боевой /tmp не виден.
Итог: «САМОТЕСТ-УБОРКА: стендов осталось N», код 0 при N == 0.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def layout_script() -> Path:
    """Раскладчик лежит только в пакете: из пакета берётся рядом, из проекта — по каталогу пакета.
    LAYOUT_SCRIPT — дверь для проверки живости пробы мутированной копией."""
    if os.environ.get("LAYOUT_SCRIPT"):
        return Path(os.environ["LAYOUT_SCRIPT"])
    beside = HERE.parent / "razlozhit-harnes.sh"
    if beside.is_file():
        return beside
    sys.path.insert(0, str(HERE / "lib"))
    import config
    return config.katalog_paketa() / "harness" / "razlozhit-harnes.sh"


SCRIPT = layout_script()
LIMIT_SEC = 20


def main() -> int:
    if not SCRIPT.is_file():
        print(f"САМОТЕСТ-УБОРКА: нет раскладчика {SCRIPT}")
        return 1
    with tempfile.TemporaryDirectory(prefix="dc-layout-clean-") as tmp:
        env = {**os.environ, "TMPDIR": tmp}
        run = subprocess.run(["timeout", str(LIMIT_SEC), "bash", str(SCRIPT), "--selftest"], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=LIMIT_SEC + 60)
        left = sorted(p.name for p in Path(tmp).glob("proba-razlozhit-*"))
        # Прогон, дошедший до конца сам, не проверил обрыв: стенд мог и не успеть появиться.
        if run.returncode != 124:
            print(f"САМОТЕСТ-УБОРКА: обрыва не было (код {run.returncode}), проба ничего не судит")
            return 1
        print(f"САМОТЕСТ-УБОРКА: стендов осталось {len(left)} {left}")
        return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
