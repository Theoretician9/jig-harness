#!/usr/bin/env python3
"""Гейт «ключи образца harness.conf»: образец знает ключи, которые читает код.

Установщик кладёт ОБРАЗЕЦ, а ключ, вписанный руками в живой конфиг, до новой
установки не доезжает. Правила:
  1. ключ живого конфига, который читает код (имя в строке кода, не в
     комментарии), есть в образце — строкой `KEY=` или закомментированной
     `# KEY=` (описан, умолчание держит код);
  2. каждый ключ «ключи:» доустановки стоит в образце НЕзакомментированным:
     чистая установка обязана получить то же, что обновлённая;
  3. ключ из «снять_ключи:» в образце не стоит;
  4. ни живой конфиг, ни образец не ставят ключ дважды: загрузчик берёт
     последнее, и правка владельца в первой строке молча не работает.

Почему только ключи живого конфига, а не все имена из кода: синтаксический
поиск даёт сотни имён вне образца (локальные переменные, двери проб).

    check-sample-config-keys.py [--sample F] [--live F] [--jobs F] [--code-root D]
Код возврата: 0 — чисто; 1 — находки; 2 — явно названный файл не прочитан.
"""
import argparse
import os
from collections import Counter
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import config  # noqa: E402

CODE_SUFFIXES = {".sh", ".py"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".tasks"}
# Деревья помощников — копии чужих веток, а не код этого корня: 122 дерева — 2 ГБ кода.
SKIP_PATHS = {Path(".claude/worktrees")}
COMMENTED_KEY = re.compile(r"^\s*#\s*([A-Z][A-Z0-9_]*)=", re.MULTILINE)
NAME = re.compile(r"[A-Z0-9_]+")
# Та же строка присваивания, что у загрузчика (config.из_текста): судим то, что он прочтёт.
ASSIGNED = re.compile(r"\s*([A-Z][A-Z0-9_]*)\s*=")


def repeated(text: str) -> dict[str, int]:
    """Ключ → сколько раз присвоен, только поставленные больше одного раза."""
    counts = Counter(m.group(1) for line in text.splitlines() if (m := ASSIGNED.match(line)))
    return {key: n for key, n in sorted(counts.items()) if n > 1}


def code_files(root: Path):
    for top, dirs, files in os.walk(root):
        here = Path(top).relative_to(root)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and here / d not in SKIP_PATHS]
        yield from (Path(top) / f for f in files if Path(f).suffix in CODE_SUFFIXES)


def code_names(root: Path) -> set[str]:
    """Имена из строк кода корня, кроме комментариев: комментарий ключ упоминает, но не читает.

    По строке, без склейки: память не растёт с объёмом кода."""
    names = set()
    for path in code_files(root):
        with open(path, encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if not line.lstrip().startswith("#"):
                    names.update(NAME.findall(line))
    return names


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
    live_text = live_path.read_text(encoding="utf-8", errors="replace") if live_path.is_file() else ""
    live = set(config.из_текста(live_text))
    code = code_names(args.code_root) if live else set()

    found = [f"{key}: нет в образце — живой конфиг его объявил и код читает"
             for key in sorted(live - active - described) if key in code]
    found += [f"{key}: нет в образце — «ключи:» доустановки его доносит, чистая установка не получит"
              for key in sorted(set(jobs.get("ключи") or {}) - active)]
    found += [f"{key}: снят («снять_ключи:»), а образец его раздаёт"
              for key in sorted(set(jobs.get("снять_ключи") or []) & active)]
    found += [f"{key}: стоит дважды и больше ({n}) в {where} — загрузчик берёт последнее"
              for where, text in ((live_path, live_text), (args.sample, sample))
              for key, n in repeated(text).items()]
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
