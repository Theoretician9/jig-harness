#!/usr/bin/env python3
"""Где лежит ревью спеки — одна договорённость на двух судей.

Скил работы агентов кладёт отчёты ревьюеров в .tasks/<id>/review/*.md, а
исторически ревью лежит рядом со спекой как <спека>-REVIEW.md. Когда каждый
судья (check-pipeline.py, pipeline.py) знал только одно место, сделанное
ревью для другого не существовало и смене печаталось «НЕТ».
"""
from pathlib import Path


def spec_review_path(root, task_id, spec):
    """Путь существующего ревью спеки либо None; для сообщения «нет» — beside_spec()."""
    beside = beside_spec(root, spec)
    if beside and beside.is_file():
        return beside
    if task_id:
        found = sorted((Path(root) / ".tasks" / task_id / "review").glob("*.md"))
        if found:
            return found[0]
    return None


def beside_spec(root, spec):
    """Канонический путь «рядом со спекой»: туда кладут, когда ревью ещё нет."""
    if not spec:
        return None
    base = Path(root) / spec
    return base.with_name(base.stem + "-REVIEW" + base.suffix)
