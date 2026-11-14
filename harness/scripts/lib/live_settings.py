#!/usr/bin/env python3
"""Досылка ключей в живые YAML-настройки при раскладке (Г3б).

Раскладка не затирает pipeline.yaml, queue.yaml и прочие живые файлы, но новый
код ждёт новых ключей. Судья «правил ли владелец файл» — история пакета в git:
если blob живого файла когда-либо лежал в пакете, владелец его не менял и
файл заменяется пакетным. Правленый файл получает только недостающие верхние
ключи, текстом в конец; вложенные недостающие называются, но не пишутся.

Файл записей (records=True) не пишется никогда: его ведёт живой механизм, а
прошлые пакетные версии несли записи установки (И-1).

CLI: live_settings.py <пакетный> <живой> [--записи] — печатает строки итога.
"""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml



def package_blobs(package: Path) -> set:
    """Blob'ы всех версий файла в git пакета; не git или git упал — пустое множество."""
    try:
        done = subprocess.run(
            ["git", "-C", str(package.parent), "log", "--format=", "--raw", "--no-abbrev",
             "--", package.name],
            capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return set()
    blobs = set()
    for line in done.stdout.splitlines():
        fields = line.split("\t")[0].split()
        if line.startswith(":") and len(fields) >= 4 and set(fields[3]) != {"0"}:
            blobs.add(fields[3])
    return blobs


def blob_of(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def top_blocks(text: str) -> dict:
    """Верхний ключ → его текстовый блок: комментарии над ключом, строка ключа, тело."""
    root = yaml.compose(text)
    if not isinstance(root, yaml.MappingNode) or root.flow_style:
        raise ValueError("верхний уровень не блочный словарь")
    lines = text.split("\n")
    key_lines = [(key.value, key.start_mark.line) for key, _ in root.value]
    starts = []
    for _, index in key_lines:
        while index > 0 and lines[index - 1].startswith("#"):
            index -= 1
        starts.append(index)
    blocks = {}
    for position, (name, _) in enumerate(key_lines):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        blocks[name] = "\n".join(lines[starts[position]:end]).rstrip("\n")
    return blocks


def nested_missing(package, live, prefix: str = "") -> list:
    """Подключи, что есть в пакетном словаре и нет в живом; списки не разбираются."""
    names = []
    for key, value in package.items():
        if key not in live:
            if prefix:
                names.append(f"{prefix}{key}")
        elif isinstance(value, dict) and isinstance(live[key], dict):
            names += nested_missing(value, live[key], f"{prefix}{key}.")
    return names


def append_missing(live_text: str, package_text: str, missing: list) -> str:
    blocks = top_blocks(package_text)
    ordered = [blocks[key] for key in blocks if key in missing]
    return live_text.rstrip("\n") + "\n\n" + "\n\n".join(ordered) + "\n"


def verify_appended(new_text: str, live: dict, package: dict, missing: list) -> str:
    """Причина отказа или пустая строка: результат читается и значения не разошлись."""
    try:
        merged = yaml.safe_load(new_text)
    except yaml.YAMLError as error:
        return f"вставка не читается как YAML ({type(error).__name__})"
    if not isinstance(merged, dict):
        return "вставка даёт не словарь"
    if any(merged.get(key) != value for key, value in live.items()):
        return "вставка меняет значения владельца"
    if any(merged.get(key) != package[key] for key in missing):
        return "дописанные ключи не совпали с пакетом"
    return ""


def write_atomically(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".layout-tmp")
    try:
        tmp.write_bytes(data)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def settle(live: Path, package: Path, known_blobs: set, records: bool = False) -> list:
    """Сверяет живой файл с пакетным; возвращает строки итога."""
    name = live.name
    head = f"  {name} — "
    live_bytes, package_bytes = live.read_bytes(), package.read_bytes()
    if live_bytes == package_bytes:
        return [head + ("записи установки, ключей хватает" if records else "совпадает с пакетом")]
    if not records and blob_of(live_bytes) in known_blobs:
        write_atomically(live, package_bytes)
        return [head + "заменён (не правился)"]
    try:
        live_data = yaml.safe_load(live_bytes)
        package_data = yaml.safe_load(package_bytes)
    except yaml.YAMLError as error:
        return [head + f"не тронут: не читается YAML ({type(error).__name__})"]
    if not isinstance(live_data, dict) or not isinstance(package_data, dict):
        return [head + "не тронут: верхний уровень не словарь"]
    missing = [key for key in package_data if key not in live_data]
    lines = []
    if records:
        lines.append(head + "записи установки, не пишу; " + (
            f"верхних ключей нет: {', '.join(map(str, missing))}" if missing else "ключей хватает"))
    elif missing:
        lines.append(add_missing(live, live_data, package_bytes.decode("utf-8"), package_data, missing))
        if live.read_bytes() != live_bytes:
            live_data = yaml.safe_load(live.read_bytes())
    nested = nested_missing(package_data, live_data)
    if nested:
        lines.append(head + f"вложенные без ключа: {', '.join(nested)}")
    return lines or [head + "своё, недостающих ключей нет"]


def add_missing(live: Path, live_data: dict, package_text: str, package_data: dict, missing: list) -> str:
    head = f"  {live.name} — "
    names = ", ".join(map(str, missing))
    try:
        new_text = append_missing(live.read_text(encoding="utf-8"), package_text, missing)
    except ValueError as error:
        return head + f"не тронут: {error}, недостаёт: {names}"
    reason = verify_appended(new_text, live_data, package_data, missing)
    if reason:
        return head + f"не тронут: {reason}, недостаёт: {names}"
    write_atomically(live, new_text.encode("utf-8"))
    return head + f"дописано ключей {len(missing)}: {names}"


def main(argv: list) -> int:
    args = [a for a in argv if a != "--записи"]
    if len(args) != 2:
        print("usage: live_settings.py <пакетный> <живой> [--записи]", file=sys.stderr)
        return 2
    package, live = Path(args[0]), Path(args[1])
    for line in settle(live, package, package_blobs(package), records="--записи" in argv):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
