"""Какие пробы покрыты записью мутаций — ОДНО место для двух читателей.

Читают его `scripts/build-live-check-list.py` (предикат класса D) и
`scripts/check-probe-mutations.py` (ворота «проба без записи мутаций»). Две
копии разошлись бы молча: одна считала бы покрытой пробу, которую вторая
краснит.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml


def normalize_key(key: str) -> str:
    # Живые данные пишут одну пробу как `scripts/./t.py`, `harness/../scripts/t.py`
    # и `scripts/t.sh#имя` (несколько механизмов у одной пробы); файл у всех один.
    return os.path.normpath(str(key).split("#")[0])


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def covered_probes(mutations: Path, invariants: Path) -> set[str] | None:
    """Нормализованные пути проб с НАЗВАННОЙ мутацией, из обоих носителей.

    None — ни одного носителя нет на диске: «судить нечем» не равно «покрытых нет».
    """
    if not mutations.exists() and not invariants.exists():
        return None
    named: set[str] = set()
    if mutations.exists():
        named |= {normalize_key(probe)
                  for probe, record in (_load(mutations).get("пробы") or {}).items()
                  if (record or {}).get("мутация")}
    if invariants.exists():
        for invariant in _load(invariants).get("инварианты") or []:
            if invariant.get("мутация"):
                # Только слова, похожие на путь пробы, и ЦЕЛЫМ словом: подстрока
                # зачла бы `test_a.py` по команде, зовущей `test_a.py.bak`.
                named |= {normalize_key(word)
                          for word in str(invariant.get("доказательство") or "").split()
                          if word.endswith((".py", ".sh"))}
    return named


def declared_exceptions(mutations: Path) -> dict:
    """Ключ верхнего уровня `без_мутаций`: путь или каталог с `/` на конце → причина."""
    if not mutations.exists():
        return {}
    return _load(mutations).get("без_мутаций") or {}


def under_exception(probe: str, key: str) -> bool:
    # Каталог — только с `/` на конце: без него `scripts/test_e` накрыл бы
    # `scripts/test_env.sh` по префиксу.
    if key.endswith("/"):
        return probe.startswith(key)
    return probe == os.path.normpath(key)
