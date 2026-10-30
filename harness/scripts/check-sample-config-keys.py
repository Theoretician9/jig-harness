#!/usr/bin/env python3
"""Гейт «ключи образца harness.conf»: образец знает ключи, которые читает код.

Установщик кладёт ОБРАЗЕЦ, а ключ, вписанный руками в живой конфиг, до новой
установки не доезжает. Правила:
  1. ключ живого конфига, который читает код (имя в строке кода, не в
     комментарии), есть в образце — строкой `KEY=` или закомментированной
     `# KEY=` (описан, умолчание держит код);
  2. каждый ключ «ключи:» доустановки стоит в образце НЕзакомментированным:
     чистая установка обязана получить то же, что обновлённая;
  3. ключ из «снять_ключи:» в образце не стоит.

Почему только ключи живого конфига, а не все имена из кода: синтаксический
поиск даёт сотни имён вне образца (локальные переменные, двери проб).

    check-sample-config-keys.py [--sample F] [--live F] [--jobs F] [--code-root D]
Код возврата: 0 — чисто; 1 — находки; 2 — явно названный файл не прочитан.
"""
import argparse
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import config  # noqa: E402

CODE_SUFFIXES = {".sh", ".py"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".tasks"}
COMMENTED_KEY = re.compile(r"^\s*#\s*([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def code_text(root: Path) -> str:
    """Весь код корня без строк-комментариев: комментарий ключ упоминает, но не читает."""
    chunks = []
    for path in root.rglob("*"):
        if path.suffix not in CODE_SUFFIXES or SKIP_DIRS & set(path.relative_to(root).parts):
            continue
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        chunks.extend(line for line in lines if not line.lstrip().startswith("#"))
    return "\n".join(chunks)


def is_read(key: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9_]){key}(?![A-Z0-9_])", code) is not None


def main() -> int:
    parser = argparse.ArgumentParser()
    root = config.корень_дерева(__file__)
    parser.add_argument("--sample", type=Path, default=root / "harness" / "config" / "harness.conf")
    parser.add_argument("--live", type=Path)
    parser.add_argument("--jobs", type=Path, default=root / "harness" / "config" / "system-jobs.yaml")
    parser.add_argument("--code-root", type=Path, default=root)
    args = parser.parse_args()

    for named in (args.sample, args.live, args.jobs):
        if named and not named.is_file():
            print(f"не прочитан: {named}", file=sys.stderr)
            return 2
    sample = args.sample.read_text(encoding="utf-8")
    active, described = set(config.из_текста(sample)), set(COMMENTED_KEY.findall(sample))
    jobs = yaml.safe_load(args.jobs.read_text(encoding="utf-8")) or {}

    live_path = args.live or Path(config.путь_harness_conf())
    live = set(config.из_файла(str(live_path))) if live_path.is_file() else set()
    code = code_text(args.code_root) if live else ""

    found = [f"{key}: нет в образце — живой конфиг его объявил и код читает"
             for key in sorted(live - active - described) if is_read(key, code)]
    found += [f"{key}: нет в образце — «ключи:» доустановки его доносит, чистая установка не получит"
              for key in sorted(set(jobs.get("ключи") or {}) - active)]
    found += [f"{key}: снят («снять_ключи:»), а образец его раздаёт"
              for key in sorted(set(jobs.get("снять_ключи") or []) & active)]
    if not found:
        # Число судимого в приговоре отличает «проверено» от «ничего не судили».
        print(f"чисто: ключей живого конфига {len(live)}, "
              f"ключей доустановки {len(set(jobs.get('ключи') or {}))}, "
              f"снятых ключей {len(set(jobs.get('снять_ключи') or []))}")
        return 0
    print("\n".join(found))
    return 1


if __name__ == "__main__":
    sys.exit(main())
