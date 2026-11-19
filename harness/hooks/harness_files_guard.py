#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сторож файлов харнеса у копии: что положила раскладка, копия не правит.

PreToolUse на Write|Edit|NotebookEdit|Bash. Файл харнеса — путь из описи раскладки
<проект>/.layout-manifest: правка на месте у копии теряется следующим обновлением
и не доезжает до других копий. Улучшение харнеса идёт только через хозяина —
находкой в сеть HarnessEvolution (слово владельца 14.11.2026). Отказ называет эту
дверь и слой копии (harness/config/copy-layer.yaml): места, где копия пишет своё.

Хозяин — установка с EVOLUTION_ROLE=host в harness.conf: она и пишет харнес, и
отвечает на находки. У неё хук молчит. Пути записи Bash разбирает общий разбор
orders_write_guard.write_targets (перенаправление, tee, sed -i, cp/mv, dd, python).

Отказ — код 2, причина в stderr. Сбой самого хука — пропуск со строкой в stderr,
как у соседей: сломанный сторож не должен останавливать работу.
Двери проб: HARNESS_FILES_GUARD_ROOT — корень проекта; HARNESS_CONF — конфиг.
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lib"))
import config  # noqa: E402
import layout_manifest  # noqa: E402
from orders_write_guard import write_targets  # noqa: E402

# Корень — по месту хука, как у сторожа приказов: CLAUDE_PROJECT_DIR — каталог запуска.
ROOT = Path(os.environ.get("HARNESS_FILES_GUARD_ROOT") or HERE.parent.parent)
DENY = ("{rel} — файл харнеса (положен раскладкой, .layout-manifest): у копии только для чтения, "
        "обновление затрёт правку.\n"
        "Хочешь изменить — находка строителю харнеса в репозиторий HarnessEvolution "
        "(ляжет в находки/<сервер>/<ГГГГ-ММ-ДД>-<суть>.md; формат записи — README сети). "
        "Запиши файл <ГГГГ-ММ-ДД>-<суть>.md вне файлов харнеса, затем:\n"
        "  python3 scripts/evolution-net.py --находка <файл>    # печатает sha подписанного коммита\n"
        "  python3 scripts/evolution-net.py --отправить <sha>\n"
        "Своё — в слое копии: {places}.")


def is_host() -> bool:
    return config.конфиг(config.путь_harness_conf()).get("EVOLUTION_ROLE") == "host"


def harness_files(masks: list) -> set:
    manifest = layout_manifest.parse_bytes((ROOT / layout_manifest.MANIFEST_NAME).read_bytes())
    if isinstance(manifest, layout_manifest.ManifestBroken):
        raise ValueError(f"опись нечитаема: {manifest.reason}")
    return {rel for rel in manifest.body if not any(fnmatch.fnmatchcase(rel, m) for m in masks)}


def relative(path: str, root_real: str) -> str | None:
    real = os.path.realpath(path)
    return os.path.relpath(real, root_real) if real.startswith(root_real + os.sep) else None


def main() -> int:
    try:
        targets = write_targets(json.loads(sys.stdin.read()))
        if not targets or is_host() or not (ROOT / layout_manifest.MANIFEST_NAME).is_file():
            return 0
        places = yaml.safe_load((ROOT / "harness" / "config" / "copy-layer.yaml")
                                .read_text(encoding="utf-8"))["места"]
        root_real = os.path.realpath(ROOT)
        laid = harness_files([p["маска"] for p in places if p.get("маска")])
        hit = next((rel for rel in (relative(t, root_real) for t in targets) if rel in laid), None)
    except Exception as error:  # noqa: BLE001 — сбой хука не запрещает работу
        print(f"harness_files_guard: пропуск, хук не разобрал вход: {type(error).__name__}: {error}",
              file=sys.stderr)
        return 0
    if hit:
        print(DENY.format(rel=hit, places="; ".join(p["место"] for p in places)), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
