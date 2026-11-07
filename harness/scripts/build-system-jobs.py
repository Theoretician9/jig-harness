#!/usr/bin/env python3
"""Порождает разделы system-jobs.yaml из process-library.yaml.

Расписание живёт в библиотеке процессов; system-jobs.yaml читают девять
потребителей, поэтому четыре раздела (задания, метки, запас-порога,
метки_без_задания) лежат в нём между маркерами и переписываются отсюда.
Вне маркеров файл не меняется ни на байт.

    python3 scripts/build-system-jobs.py           переписать порождённый блок
    python3 scripts/build-system-jobs.py --check   выход 0 — блок сходится, 1 — расходится

Двери: HARNESS_PROCESS_LIBRARY (библиотека), HARNESS_SYSTEM_JOBS (цель).
"""
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import process_library  # noqa: E402

BEGIN = "# >>> порождено из process-library.yaml — руками не править"
END = "# <<<"
SECTIONS = ("задания", "метки", "запас-порога", "метки_без_задания")
MIRRORED = SECTIONS[1:]
HEADER = (
    "# Блок ниже порождён scripts/build-system-jobs.py из harness/config/process-library.yaml.\n"
    "# Править расписание и метки нужно там, затем запустить build-system-jobs.py:\n"
    "# гейт сверки краснеет при расхождении. Комментарии «почему такое значение» — в библиотеке.\n"
)
TOP_KEY = re.compile(r"[^\s#]")


def library_path() -> Path:
    return Path(os.environ.get("HARNESS_PROCESS_LIBRARY")
                or process_library._каталог_конфигов() / "process-library.yaml")


def target_path() -> Path:
    return Path(os.environ.get("HARNESS_SYSTEM_JOBS")
                or process_library._каталог_конфигов() / "system-jobs.yaml")


def section_span(lines: list, name: str):
    """(начало, конец) раздела верхнего уровня: от его комментариев в нулевой колонке
    над якорем до последней содержательной строки (хвостовые пустые и комментарии
    нулевой колонки принадлежат следующему разделу)."""
    anchor = next((i for i, line in enumerate(lines) if re.match(rf"{re.escape(name)}:\s*(#|$)", line)), None)
    if anchor is None:
        return None
    start = anchor
    while start > 0 and lines[start - 1].startswith("#"):
        start -= 1
    end = anchor + 1
    last = anchor
    while end < len(lines) and not TOP_KEY.match(lines[end]):
        if lines[end].strip() and not lines[end].startswith("#"):
            last = end
        end += 1
    return start, last + 1


def mirrored_text(library_lines: list) -> str:
    chunks = []
    for name in MIRRORED:
        span = section_span(library_lines, name)
        chunks.append("".join(library_lines[span[0]:span[1]]) if span else f"{name}: {{}}\n")
    return "\n".join(chunks)


def jobs_text(library_lines: list) -> str:
    """Задания в форме system-jobs; комментарий над записью в библиотеке едет с ней."""
    span = section_span(library_lines, "процессы")
    notes, pending = {}, []
    for line in library_lines[span[0]:span[1]]:
        if line.startswith("  #"):
            pending.append(line[2:])
        elif (head := re.match(r"  ([^\s#][^:]*):\s*$", line)):
            notes[head.group(1)] = pending
            pending = []
        else:
            pending = []
    out = ["задания:\n"]
    for job in process_library.jobs():
        out += [f"  {line}" for line in notes.get(job["имя"], [])]
        body = yaml.safe_dump([job], allow_unicode=True, sort_keys=False, width=10000)
        out += [f"  {line}\n" for line in body.splitlines()]
    return "".join(out)


def build_block() -> str:
    lines = library_path().read_text(encoding="utf-8").splitlines(keepends=True)
    return HEADER + jobs_text(lines) + "\n" + mirrored_text(lines)


def find_markers(lines: list):
    begins = [i for i, line in enumerate(lines) if line.rstrip("\n") == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\n") == END]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        raise SystemExit(f"маркеры порождённого блока: нужна ровно одна пара «{BEGIN}» / «{END}», "
                         f"найдено {len(begins)}/{len(ends)}")
    return begins[0], ends[0]


def first_region(lines: list):
    """Первая генерация: от «задания:» до конца «метки_без_задания»; всё чужое между ними — отказ."""
    first = next((i for i, line in enumerate(lines) if re.match(r"задания:\s*(#|$)", line)), None)
    tail = section_span(lines, "метки_без_задания")
    if first is None or tail is None:
        raise SystemExit("нет разделов «задания» и «метки_без_задания»: первую генерацию делать не из чего")
    stray = [line.split(":")[0] for line in lines[first + 1:tail[1]]
             if TOP_KEY.match(line) and line.split(":")[0] not in SECTIONS]
    if stray:
        raise SystemExit(f"между «задания» и «метки_без_задания» лежат чужие разделы {stray}: перенеси их вручную")
    return first, tail[1] - 1


def render(text: str) -> str:
    lines = text.splitlines(keepends=True)
    marks = find_markers(lines)
    block = build_block()
    if marks:
        return "".join(lines[:marks[0] + 1]) + block + "".join(lines[marks[1]:])
    first, last = first_region(lines)
    return "".join(lines[:first]) + BEGIN + "\n" + block + END + "\n" + "".join(lines[last + 1:])


def differing(actual_text: str, wanted_text: str) -> list:
    """Имена разошедшихся заданий и разделов; пусто — разбор равен, расходятся лишь комментарии."""
    actual = yaml.safe_load(actual_text) or {}
    wanted = yaml.safe_load(wanted_text) or {}
    names = []
    for section in SECTIONS:
        have, want = actual.get(section) or [], wanted.get(section) or []
        if have == want:
            continue
        names.append(f"раздел {section}")
        if section == "задания":
            by_name = lambda jobs: {j.get("имя"): j for j in jobs}
            have, want = by_name(have), by_name(want)
        names += [f"{section}.{key}" for key in sorted(set(have) | set(want), key=str)
                  if have.get(key) != want.get(key)]
    return names


def main() -> int:
    target = target_path()
    old = target.read_text(encoding="utf-8")
    new = render(old)
    if "--check" not in sys.argv[1:]:
        if new != old:
            # Через временный файл рядом: читатель не увидит половину записи.
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(new, encoding="utf-8")
            os.replace(tmp, target)
        return 0
    if new == old:
        return 0
    if find_markers(old.splitlines(keepends=True)) is None:
        print("в system-jobs.yaml нет порождённого блока: запусти scripts/build-system-jobs.py")
        return 1
    names = differing(old, new) or ["только комментарии порождённого блока"]
    print("порождённый блок расходится с process-library.yaml: " + ", ".join(names))
    print("исправление: python3 scripts/build-system-jobs.py")
    return 1


if __name__ == "__main__":
    sys.exit(main())
