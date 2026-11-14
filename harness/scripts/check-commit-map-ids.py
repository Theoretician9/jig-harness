#!/usr/bin/env python3
"""Замок коммита: добавленная строка файла сборки не несёт id карточки карты.

Замок сборки (check-public-clean.py) судит дерево целиком, но только при
публикации: чужой коммит вписал id карточки в пробу ПОСЛЕ вычистки, и релиз
0.11.1 снова встал на замке (14.11.2026). Здесь — стоп росту на коммите.

Судятся только строки «+» индекса (git diff --cached -U0): уже лежащие строки
с id чинятся вычисткой, а не блокируют каждый следующий коммит. Только файлы,
которые едут в сборку: проектный путь отображается в пакет
(compare-with-package.py), минус живые данные, исключённое и подменяемое
заготовкой (public-build.yaml). id карточек и их образец — из замка сборки.

    python3 scripts/check-commit-map-ids.py [--пакет <каталог>]

Код: 0 — чисто · 1 — находки «<файл>:<строка>: карточка=<id>» ·
77 — судить нечем (нет пакета с заготовкой карты или карта не читается):
жёлтый, а не зелёный — молчаливое «чисто» прятало бы беду.
"""
import fnmatch
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import yaml

ЗДЕСЬ = Path(__file__).resolve().parent
ЖЁЛТЫЙ = 77
ШАПКА_ХУНКА = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _ввезти(имя: str, файл: str):
    спека = importlib.util.spec_from_file_location(имя, ЗДЕСЬ / файл)
    модуль = importlib.util.module_from_spec(спека)
    спека.loader.exec_module(модуль)
    return модуль


чистота = _ввезти("check_public_clean", "check-public-clean.py")
сверка = _ввезти("compare_with_package", "compare-with-package.py")


def добавленные_строки(дифф: str) -> list:
    """[(файл, номер строки, текст)] — строки «+» из `git diff -U0`."""
    строки, файл, номер, в_шапке = [], None, 0, False
    for строка in дифф.splitlines():
        if строка.startswith("diff --git "):
            файл, в_шапке = None, True
        elif в_шапке and строка.startswith("+++ "):
            файл = строка[6:] if строка.startswith("+++ b/") else None
        elif m := ШАПКА_ХУНКА.match(строка):
            номер, в_шапке = int(m.group(1)), False
        elif not в_шапке and строка.startswith("+") and файл:
            строки.append((файл, номер, строка[1:]))
            номер += 1
    return строки


def едет_в_сборку(путь: str, правила: dict, скилы: set) -> bool:
    в_пакете = сверка.место_в_пакете(путь)
    if в_пакете is None or сверка.живой(путь, скилы):
        return False
    if в_пакете in (правила.get("заменить_шаблоном") or {}):
        return False
    return not any(fnmatch.fnmatch(в_пакете, маска) for маска in правила.get("исключить") or [])


def довод(имя: str) -> str | None:
    if имя in sys.argv[1:-1]:
        return sys.argv[sys.argv.index(имя) + 1]
    return None


def main() -> int:
    проект = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True,
                                 capture_output=True, text=True).stdout.strip())
    пакет = Path(довод("--пакет")) if довод("--пакет") else сверка.найти_пакет()[0]
    if пакет is None or not (пакет / "harness" / "dev-map.yaml").is_file():
        print(f"[карточки в коммите] нет пакета с заготовкой карты ({пакет}) — судить нечем")
        return ЖЁЛТЫЙ
    try:
        карточки = чистота.карточки_проекта(пакет, пакет, проект)
    except чистота.ОтказКарты as отказ:
        print(f"[карточки в коммите] {отказ} — судить нечем")
        return ЖЁЛТЫЙ
    правила = yaml.safe_load((проект / "harness/config/public-build.yaml")
                             .read_text(encoding="utf-8")) or {}
    скилы = сверка.скилы_наборов(пакет)
    дифф = subprocess.run(["git", "-c", "core.quotepath=false", "diff", "--cached", "-U0",
                           "--no-color", "--no-ext-diff", "--diff-filter=d"],
                          check=True, capture_output=True, text=True, cwd=проект).stdout
    образец = чистота.образец_карточек(карточки)
    находки = [f"{файл}:{номер}: карточка={ид}"
               for файл, номер, текст in добавленные_строки(дифф)
               if едет_в_сборку(файл, правила, скилы)
               for ид in dict.fromkeys(образец.findall(текст))]
    if not находки:
        return 0
    print("[карточки в коммите] id карточки карты в новой строке файла сборки — "
          "у копии такой карточки нет; связь с задачей — трейлер Dev-Map:")
    print("\n".join(f"  {н}" for н in находки))
    return 1


if __name__ == "__main__":
    sys.exit(main())
