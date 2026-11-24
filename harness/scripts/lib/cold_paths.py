#!/usr/bin/env python3
"""Сверка обновления с «холодными» путями: что грузится при старте claude, а не при вызове.

Спека ротация-ждёт-помощников, раздел 14.16. Горячее (хуки, скрипты, библиотеки,
конфиги, демоны) читается с диска при каждом вызове — ротации не просит. Холодное
(CLAUDE.md и его `@`-файлы, личность `harness/self/**`, `.claude/settings.json`,
`.mcp.json`, карточки помощников) процесс claude прочитал при старте и держит:
чтобы обновление дошло, роль надо обновить новым процессом (путь преемника).

    cold_paths.py --после-раскладки <архив>

Пути действий `перекрыт` / `положен-новый` / `снят` из `journal.tsv` архива сверяются
с масками. Задето холодное — заявка ротации (`rotation_flag.py --request`) с причиной
«обновление» и путями в «подробно»; нет — только строка «холодное не задето».
Код всегда 0: сверка не смеет откатить обновление, а сбой читаемо виден в строке.

Маски — данные `harness/config/cold-paths.yaml` (ключ `ROTATE_COLD_PATHS` в
harness.conf; дверь стенда — HARNESS_COLD_PATHS). `**` и `*` — через любую глубину.
`@`-файлы из `с_ат_строками` берутся разбором: список в файле устарел бы с первой
новой строкой `@` в CLAUDE.md.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as конф                                     # noqa: E402
import layout_journal                                     # noqa: E402
import rotation_flag                                      # noqa: E402

REASON = "обновление"
FORWARD_KINDS = layout_journal.NEW_VERSION_ACTIONS
# `@путь` — импорт файла в CLAUDE.md; путь обязан иметь точку или косую черту, чтобы `@` в прозе не считался.
AT_IMPORT = re.compile(r"(?<![\w`])@(\.?/?[\w./*\-]*[./][\w./*\-]*)")


def project_root() -> Path:
    return Path(конф.конфиг().get("PROJECT_DIR") or конф.корень_дерева())


def masks_file() -> Path:
    named = os.environ.get("HARNESS_COLD_PATHS") or конф.из_файла(конф.путь_harness_conf()).get("ROTATE_COLD_PATHS") \
        or "harness/config/cold-paths.yaml"
    path = Path(named)
    return path if path.is_absolute() else project_root() / path


def load_masks(file: Path) -> tuple[list[str], list[str]]:
    """(маски холодных путей, файлы с `@`-строками). Данные — строки или `{маска: …, почему: …}`."""
    import yaml
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    masks = [str(item["маска"] if isinstance(item, dict) else item) for item in data.get("холодные") or []]
    return masks, [str(p) for p in data.get("с_ат_строками") or []]


def at_imports(root: Path, names: list[str]) -> list[str]:
    """Пути `@…` из названных файлов (от корня проекта); `./` снимается, файл нужен как маска."""
    found: list[str] = []
    for name in names:
        try:
            text = (root / name).read_text(encoding="utf-8")
        except OSError:
            continue
        in_fence = False
        for line in text.splitlines():
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
            elif not in_fence:
                found += [m.removeprefix("./") for m in AT_IMPORT.findall(line)]
    return found


def mask_regex(mask: str) -> re.Pattern:
    """`**` и `*` — любая глубина; остальное буквально."""
    return re.compile("".join(".*" if part.startswith("*") else re.escape(part)
                              for part in re.split(r"(\*+)", mask) if part) + r"\Z")


def touched_cold(paths: list[str], masks: list[str]) -> list[str]:
    patterns = [mask_regex(m) for m in masks]
    return sorted({p for p in paths if any(r.match(p) for r in patterns)})


def after_layout(archive: str) -> int:
    journal, why = layout_journal.read_journal(archive)
    if journal is None:
        print(f"журнала раскладки нет или он не прочитан ({why or 'нет файла'}): сверка холодного пропущена")
        return 0
    paths = [a.path for a in journal.actions if a.kind in FORWARD_KINDS]
    masks, at_files = load_masks(masks_file())
    cold = touched_cold(paths, masks + at_imports(project_root(), at_files))
    if not cold:
        print(f"холодное не задето (путей в журнале: {len(paths)})")
        return 0
    rotation_flag.put_request(REASON, "", "", " ".join(cold))
    print(f"холодное задето: {' '.join(cold)}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--после-раскладки":
        return after_layout(argv[1])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
