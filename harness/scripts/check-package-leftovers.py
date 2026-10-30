#!/usr/bin/env python3
"""Гейт «остатки в пакете»: заглушки памяти, мёртвые файлы, ссылки в пустоту.

Запись пакета обязана иметь тёзку в памяти проекта: прибор набора
(memory-to-package.py) переносит запись под тем же именем, и запись под другим
именем он не обновит никогда. Файл конфига пакета, которого не зовёт ни одна
строка кода, — мёртвый груз раскладки.

    check-package-leftovers.py [--package <корень пакета>] [--project <корень проекта>]
Код возврата: 0 — чисто; 1 — находки, по строке на находку «путь: причина»;
2 — пакета нет: «пакет не прочитан: <путь>».
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import config  # noqa: E402

STUB_MARK = "Полная запись с уликами в этот харнес не приехала"
LINK = re.compile(r"\[\[([^\]|#]+)")
# Указатель MEMORY.md ссылается ещё и файлом: `[…](путь/имя.md)` и `путь/имя.md`.
# В остальных записях «имя.md» в кавычках — чаще всего не ссылка (STATE.md, *.md).
INDEX_LINK = re.compile(r"\]\(([^)\s]+)\.md\)|`([^`\s]+)\.md`")
INDEX_NAME = "MEMORY.md"
# Заготовки пакета не копируют записи проекта: их содержимое обязано отличаться.
TEMPLATES = {"MEMORY.md", "ORDERS.md", "TONE.md"}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def code_lines(package: Path):
    """Строки кода пакета без комментариев и без документов: комментарий и
    документ имя файла упоминают, но не зовут."""
    for path in package.rglob("*"):
        if not path.is_file() or path.suffix == ".md" or "__pycache__" in path.parts:
            continue
        for line in read(path).splitlines():
            if not line.lstrip().startswith("#"):
                yield path, line


def memory_findings(package: Path, project: Path) -> list[str]:
    memory = package / "harness" / "память"
    names = {p.stem for p in memory.glob("*.md")}
    found = []
    for path in sorted(memory.glob("*.md")):
        rel = path.relative_to(package)
        text = read(path)
        if path.name not in TEMPLATES:
            if STUB_MARK in text:
                found.append(f"{rel}: заглушка вместо полной записи")
            if not (project / "память" / path.name).is_file():
                found.append(f"{rel}: нет в памяти проекта")
        links = set(LINK.findall(text))
        if path.name == INDEX_NAME:
            links |= {Path(a or b).name for a, b in INDEX_LINK.findall(text)}
        for link in sorted(links - names):
            found.append(f"{rel}: [[{link}]] — ссылка в пустоту")
    return found


def dead_file_findings(package: Path) -> list[str]:
    config_dir = package / "harness" / "config"
    candidates = [p for p in sorted(config_dir.iterdir()) if p.is_file()]
    lines = list(code_lines(package))
    return [f"{path.relative_to(package)}: мёртвый — ни одна строка кода пакета не зовёт его по имени"
            for path in candidates
            if not any(other != path and path.name in line for other, line in lines)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, default=config.katalog_paketa())
    parser.add_argument("--project", type=Path, default=config.корень_дерева(__file__))
    args = parser.parse_args()
    # Без harness/память проверка памяти молча находила «ничего» и отвечала кодом 0.
    for needed in (args.package, args.package / "harness/config", args.package / "harness/память"):
        if not needed.is_dir():
            print(f"пакет не прочитан: {needed}")
            return 2
    found = memory_findings(args.package, args.project) + dead_file_findings(args.package)
    if not found:
        # Число судимого в приговоре отличает «проверено» от «ничего не судили».
        records = len(list((args.package / "harness" / "память").glob("*.md")))
        files = sum(1 for p in (args.package / "harness" / "config").iterdir() if p.is_file())
        print(f"чисто: записей памяти {records}, файлов конфига {files}")
        return 0
    print("\n".join(found))
    return 1


if __name__ == "__main__":
    sys.exit(main())
