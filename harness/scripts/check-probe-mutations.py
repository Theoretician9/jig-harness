#!/usr/bin/env python3
"""Ворота «проба без записи мутаций»: у каждого файла пробы есть мутация.

Замер 27.10.2026: четыре файла проб не имели записи в probe-mutations.yaml, и
оба прежних прибора молчали — они судят только строки, которые В данных есть.
Контракт — в scripts/test_check_probe_mutations.py.

Двери (проба не читает боевые данные): HARNESS_PROBE_MUTATIONS,
HARNESS_INVARIANTS, HARNESS_PROBE_TREE.
Код возврата: 0 — все покрыты; 1 — нарушения или пустой набор; 2 — читать нечем.
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import probe_coverage  # noqa: E402


def env_path(name: str, *default: str) -> Path:
    given = os.environ.get(name)
    return Path(given) if given else ROOT.joinpath(*default)


def refuse(message: str) -> None:
    print(message)
    sys.exit(2)


def probe_files(tree: Path) -> list[str]:
    # Непроиндексированная, но не игнорируемая проба — ровно тот молчаливый случай.
    listed = subprocess.run(
        ["git", "-C", str(tree), "ls-files", "--cached", "--others",
         "--exclude-standard"], capture_output=True, text=True)
    if listed.returncode != 0:
        refuse(f"дерево не читается git: {tree}: {listed.stderr.strip()}")
    return sorted(f for f in listed.stdout.splitlines()
                  if fnmatch.fnmatchcase(os.path.basename(f), "test_*.py")
                  or fnmatch.fnmatchcase(os.path.basename(f), "test_*.sh"))


def main() -> int:
    mutations = env_path("HARNESS_PROBE_MUTATIONS", "harness", "config",
                         "probe-mutations.yaml")
    invariants = env_path("HARNESS_INVARIANTS", "harness", "config", "invariants.yaml")
    tree = env_path("HARNESS_PROBE_TREE", ".")
    for carrier in (mutations, invariants):
        if not carrier.exists():
            refuse(f"носителя нет: {carrier}")

    probes = probe_files(tree)
    covered = probe_coverage.covered_probes(mutations, invariants)
    exceptions = probe_coverage.declared_exceptions(mutations)

    problems = []
    for key, reason in exceptions.items():
        if not str(reason or "").strip():
            problems.append(f"исключение без причины: {key}")
        if not any(probe_coverage.under_exception(p, key) for p in probes):
            problems.append(f"исключение ни к чему не относится: {key}")
    for probe in probes:
        if probe not in covered and not any(
                probe_coverage.under_exception(probe, key) for key in exceptions):
            problems.append(f"без записи мутаций: {probe}")

    print(f"судимых проб: {len(probes)}")
    for problem in problems:
        print(problem)
    return 1 if problems or not probes else 0


if __name__ == "__main__":
    sys.exit(main())
