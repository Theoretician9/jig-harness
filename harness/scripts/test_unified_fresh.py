#!/usr/bin/env python3
"""Проба: выгрузка UNIFIED/ХАРНЕС-ОДНИМ-ФАЙЛОМ.md собрана из нынешних глав и семян (Д-17).

Правка семени без пересборки выгрузки уезжает в выпуск незаметно: в пакете гейта
«семена» нет, он есть только в проекте. На машине пользователя он и срабатывает —
на ПЕРВОМ коммите установки, и проект остаётся без коммитов.

Проба копирует UNIFIED в свой каталог (harness-run-* под TMPDIR), зовёт там его же
сборщик BUILD.py и сравнивает собранное побайтно с выгрузкой из пакета. Сборщик
пишет рядом с собой, поэтому живой каталог не трогается. Отказ сборщика или
пустая сборка — провал, а не «свежа».

Дверь мутаций: UNIFIED_DIR — каталог UNIFIED, который судит проба.
Прогон: python3 scripts/test_unified_fresh.py   Код: 0 — выгрузка свежа.
"""
import difflib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_NAME = "ХАРНЕС-ОДНИМ-ФАЙЛОМ.md"


def unified_dir() -> Path:
    """Главы, которые судятся: рядом с пробой, иначе в каталоге пакета.

    Проба в пакете лежит в harness/scripts — главы в корне пакета; в проекте —
    в scripts/, и главы берутся у пакета по конфигу. Пакет копии — публичная
    сборка: там главы перенесены в sluzhebnoe/UNIFIED (public-build.yaml,
    «перенести»), и без этой ветки проба на копии отвечала «судить нечего».
    """
    if os.environ.get("UNIFIED_DIR"):
        return Path(os.environ["UNIFIED_DIR"])
    sys.path.insert(0, str(HERE / "lib"))
    import config
    roots = (HERE.parent.parent, config.katalog_paketa())
    places = [place for root in roots
              for place in (root / "UNIFIED", root / "sluzhebnoe" / "UNIFIED")]
    return next((place for place in places if (place / "BUILD.py").is_file()), places[-1])


def main() -> int:
    src = unified_dir()
    shipped = src / OUT_NAME
    if not (src / "BUILD.py").is_file() or not shipped.is_file():
        print(f"ВЫГРУЗКА: нет сборщика или выгрузки в {src} — судить нечего")
        return 1
    root = Path(tempfile.mkdtemp(prefix="harness-run-unified-fresh-"))
    try:
        stand = root / "UNIFIED"
        shutil.copytree(src, stand, ignore=shutil.ignore_patterns("__pycache__"))
        (stand / OUT_NAME).unlink()
        r = subprocess.run([sys.executable, "-I", "-B", str(stand / "BUILD.py")], cwd=root,
                           capture_output=True, text=True, timeout=120)
        built = stand / OUT_NAME
        if r.returncode != 0 or not built.is_file() or built.stat().st_size == 0:
            print(f"ВЫГРУЗКА: сборщик не собрал (код {r.returncode}): {(r.stdout + r.stderr).strip()[-300:]}")
            return 1
        want = built.read_text(encoding="utf-8")
        have = shipped.read_text(encoding="utf-8")
        if want == have:
            print(f"ВЫГРУЗКА: свежа ({len(have)} знаков)")
            return 0
        diff = list(difflib.unified_diff(have.splitlines(), want.splitlines(), "в пакете", "сборка",
                                         lineterm="", n=0))
        print(f"ВЫГРУЗКА: ОТСТАЛА — в пакете {len(have)} знаков, сборка даёт {len(want)}; "
              f"строк разницы {len(diff)}. Починка: python3 UNIFIED/BUILD.py и коммит")
        for line in diff[:12]:
            print("   ", line[:160])
        return 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
