#!/usr/bin/env python3
"""Проба генератора system-jobs.yaml (hr.process-dispatcher, часть А2, спека §А.3).

Источник расписания — process-library.yaml; system-jobs.yaml читают 9
потребителей, поэтому четыре раздела (задания, метки, запас-порога,
метки_без_задания) в нём ПОРОЖДАЮТСЯ генератором между маркерами, а гейт
`--check` ловит расхождение. Ожидаемое берётся РАЗБОРОМ библиотеки на стенде
(задания — `process_library.jobs()`, остальные три раздела — 1:1), а не
вшитым списком: состав заданий решает библиотека, проба судит только
«порождённое = генератор(библиотека)».

Стенд — свой временный каталог на каждый случай: копии process-library.yaml и
system-jobs.yaml; генератор зовётся с дверями на эти копии. Живые файлы проба
не трогает.

Двери (умолчания — файлы рядом с пробой):
    HARNESS_PROCESS_LIBRARY      откуда копировать process-library.yaml; генератору — путь копии
    HARNESS_SYSTEM_JOBS          откуда копировать system-jobs.yaml; генератору — путь копии
    HARNESS_BUILD_SYSTEM_JOBS    путь генератора (дверь мутаций: копия генератора)
    HARNESS_INSTALL_NEW          путь install-new.py (случай 7: вставить_ключ)

Прогон (только фоном):
    python3 scripts/test_build_system_jobs.py
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
BEGIN = "# >>> порождено из process-library.yaml — руками не править"
END = "# <<<"
SECTIONS = ("задания", "метки", "запас-порога", "метки_без_задания")
MIRRORED = SECTIONS[1:]
TIMEOUT_S = 120


def config_dir() -> Path:
    # Раскладка проекта и пакета разные: конфиги лежат там, где config/system-jobs.yaml.
    for folder in HERE.parents:
        if (folder / "config" / "system-jobs.yaml").is_file():
            return folder / "config"
    return HERE.parent / "harness" / "config"


CONFIG = config_dir()
LIBRARY_SRC = Path(os.environ.get("HARNESS_PROCESS_LIBRARY") or CONFIG / "process-library.yaml")
JOBS_SRC = Path(os.environ.get("HARNESS_SYSTEM_JOBS") or CONFIG / "system-jobs.yaml")
GENERATOR = Path(os.environ.get("HARNESS_BUILD_SYSTEM_JOBS") or HERE / "build-system-jobs.py")
INSTALL_NEW = Path(os.environ.get("HARNESS_INSTALL_NEW") or HERE / "install-new.py")

sys.path.insert(0, str(HERE / "lib"))
import process_library  # noqa: E402

paths = failures = 0


def report(ok: bool, text: str, detail: str = "") -> None:
    global paths, failures
    paths += 1
    if ok:
        print("ок    · " + text)
        return
    failures += 1
    print("НЕ ок · " + text + (f": {detail}" if detail else ""))


class Stand:
    """Временный каталог с копиями библиотеки и system-jobs; генератор видит только их."""

    def __init__(self, root: Path, name: str):
        self.dir = root / name
        self.dir.mkdir()
        self.library = self.dir / "process-library.yaml"
        self.jobs = self.dir / "system-jobs.yaml"
        shutil.copyfile(LIBRARY_SRC, self.library)
        shutil.copyfile(JOBS_SRC, self.jobs)

    def run(self, *args) -> tuple:
        if not GENERATOR.is_file():
            return None, f"генератора нет: {GENERATOR}"
        env = {**os.environ, "HARNESS_PROCESS_LIBRARY": str(self.library),
               "HARNESS_SYSTEM_JOBS": str(self.jobs)}
        try:
            done = subprocess.run([sys.executable, str(GENERATOR), *args], cwd=self.dir, env=env,
                                  capture_output=True, text=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return None, f"генератор не кончил за {TIMEOUT_S} с"
        return done.returncode, done.stdout + done.stderr

    def jobs_text(self) -> str:
        return self.jobs.read_text(encoding="utf-8")

    def library_text(self) -> str:
        return self.library.read_text(encoding="utf-8")

    def expected(self) -> dict:
        # Состав заданий решает библиотека (process_library.jobs), а не проба.
        os.environ["HARNESS_PROCESS_LIBRARY"] = str(self.library)
        library = process_library.load()
        found = {"задания": process_library.jobs()}
        found.update({section: library.get(section) or {} for section in MIRRORED})
        return found


def split_block(text: str):
    """(до, блок, после) — по маркерам, каждый ровно одной строкой и ровно один раз."""
    lines = text.splitlines(keepends=True)
    begins = [i for i, line in enumerate(lines) if line.rstrip("\n") == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\n") == END]
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        return None
    b, e = begins[0], ends[0]
    return "".join(lines[:b + 1]), "".join(lines[b + 1:e]), "".join(lines[e:])


def short(output: str) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return " | ".join(lines[-3:])[:300]


def section_bounds(text: str, section: str):
    """Строки раздела верхнего уровня: (индекс якоря, индекс следующего раздела)."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(rf"{re.escape(section)}:\s*(#|$)", line)), None)
    if start is None:
        return lines, None, None
    stop = next((i for i in range(start + 1, len(lines)) if re.match(r"[^\s#]", lines[i])), len(lines))
    return lines, start, stop


def set_schedule(text: str, name: str, value: str) -> str:
    lines, start, stop = section_bounds(text, "процессы")
    head = next(i for i in range(start, stop) if lines[i].rstrip() == f"  {name}:")
    line = next(i for i in range(head + 1, stop) if re.match(r"    расписание:", lines[i]))
    lines[line] = f'    расписание: "{value}"\n'
    return "".join(lines)


def insert_after_anchor(text: str, section: str, line: str) -> str:
    lines, start, _ = section_bounds(text, section)
    lines.insert(start + 1, line)
    return "".join(lines)


def bump_first_value(text: str, section: str) -> tuple:
    lines, start, stop = section_bounds(text, section)
    for i in range(start + 1, stop):
        match = re.match(r"(  )([^\s#][^:]*): (\d+)\s*$", lines[i])
        if match:
            lines[i] = f"{match.group(1)}{match.group(2)}: {int(match.group(3)) + 1}\n"
            return "".join(lines), match.group(2)
    raise ValueError(f"в разделе {section} нет числовой записи")


def other_schedule(current: str) -> str:
    return next(v for v in ("17 4 * * *", "19 4 * * *") if v != current)


def generated_stand(root: Path, name: str):
    stand = Stand(root, name)
    rc, out = stand.run()
    return stand, rc, out


def case_generate(root: Path) -> None:
    stand = Stand(root, "1-generate")
    before = yaml.safe_load(stand.jobs_text()) or {}
    expected = stand.expected()
    library = yaml.safe_load(stand.library_text()) or {}
    no_schedule = [n for n, r in (library.get("процессы") or {}).items() if "расписание" not in (r or {})]
    report(bool(expected["задания"]) and bool(expected["метки"]),
           f"1 стенд: библиотека даёт заданий {len(expected['задания'])}, меток {len(expected['метки'])}, "
           f"без расписания {len(no_schedule)} (пустой разбор — красный)")
    rc, out = stand.run()
    report(rc == 0, "1 генератор на стенде кончил с выходом 0", f"выход {rc}, {short(out)}")
    text = stand.jobs_text()
    parts = split_block(text)
    report(parts is not None, "1 маркеры стоят по одному разу, начало раньше конца",
           f"нет пары «{BEGIN}» / «{END}»")
    if parts is None:
        report(False, "1 блок между маркерами — ровно четыре раздела в порядке спеки", "блока нет")
        report(False, "1 разделы результата равны разбору библиотеки поштучно", "блока нет")
        return
    block = yaml.safe_load(parts[1]) or {}
    report(list(block) == list(SECTIONS),
           "1 блок между маркерами — ровно четыре раздела в порядке спеки", f"в блоке {list(block)}")
    after = yaml.safe_load(text) or {}
    for section in SECTIONS:
        got, want = after.get(section), expected[section]
        if section == "задания":
            got_names = [j.get("имя") for j in got or []]
            want_names = [j["имя"] for j in want]
            diff = [j["имя"] for j in want if j not in (got or [])] + \
                   [n for n in got_names if n not in want_names]
        else:
            diff = sorted(set(want) ^ set(got or {})) + [k for k in want if (got or {}).get(k) != want[k]]
        count_ok = len(got or []) == len(want)
        report(got == want and count_ok,
               f"1 «{section}» результата = библиотека поштучно, счёт {len(got or [])} = {len(want)}",
               f"расходятся: {sorted(set(map(str, diff)))[:8]}")
    leaked = [n for n in no_schedule if n in [j.get("имя") for j in after.get("задания") or []]]
    report(not leaked, "1 записи без расписания (таймер) в задания не попали", f"{leaked}")
    kept = [k for k in before if k not in SECTIONS and before[k] != after.get(k)]
    report(not kept and all(k in after for k in before),
           "1 непорождённые разделы прочитались как до генерации", f"изменились {kept}")
    lost = [s for s in SECTIONS if before.get(s) != after.get(s)]
    report(not lost, "1 разделы до генерации = после (перенос без потерь на согласованном снимке)",
           f"разошлись {lost}")
    rc, out = stand.run("--check")
    report(rc == 0, "1 --check на только что порождённом стенде — выход 0", f"выход {rc}, {short(out)}")


def case_outside_untouched(root: Path) -> None:
    stand = Stand(root, "2-outside")
    original = stand.jobs_text()
    rc, out = stand.run()
    parts = split_block(stand.jobs_text())
    if rc != 0 or parts is None:
        report(False, "2 вне маркеров файл байт в байт прежний", f"генерация не прошла: выход {rc}, {short(out)}")
        return
    prefix, _, suffix = parts
    head = prefix[:-len(BEGIN) - 1]
    tail = suffix[len(END) + 1:]
    report(original.startswith(head) and original.endswith(tail) and "ключи:" in tail,
           "2 первая генерация: текст до маркера — начало прежнего файла, после — его конец",
           "генератор тронул текст вне блока")
    nonce = uuid.uuid4().hex[:8]
    lines = stand.jobs_text().splitlines(keepends=True)
    lines.insert(1, f"# проба: строка вне маркеров сверху {nonce}\n")
    lines.append(f"# проба: строка вне маркеров снизу {nonce}\n")
    stand.jobs.write_text("".join(lines), encoding="utf-8")
    marked = split_block(stand.jobs_text())
    job = stand.expected()["задания"][0]
    new = other_schedule(job["расписание"])
    stand.library.write_text(set_schedule(stand.library_text(), job["имя"], new), encoding="utf-8")
    rc, out = stand.run()
    again = split_block(stand.jobs_text())
    same = again is not None and again[0] == marked[0] and again[2] == marked[2]
    report(rc == 0 and same, "2 повторная генерация: до и после маркеров байт в байт прежние (со строками пробы)",
           f"выход {rc}, {short(out)}" if rc != 0 else "текст вне маркеров изменился")
    got = [j for j in (yaml.safe_load(stand.jobs_text()) or {}).get("задания") or [] if j.get("имя") == job["имя"]]
    report(bool(got) and got[0].get("расписание") == new,
           f"2 блок при этом переписан: «{job['имя']}» получил новое расписание", f"в файле {got}")


def case_library_changed(root: Path) -> None:
    stand, rc, out = generated_stand(root, "3-library-changed")
    job = stand.expected()["задания"][0]
    new = other_schedule(job["расписание"])
    stand.library.write_text(set_schedule(stand.library_text(), job["имя"], new), encoding="utf-8")
    changed = [j for j in stand.expected()["задания"] if j["имя"] == job["имя"]][0]["расписание"] == new
    report(changed, f"3 стенд: расписание «{job['имя']}» в библиотеке стало {new}")
    rc, out = stand.run("--check")
    report(rc == 1, "3 библиотека изменилась, system-jobs нет → --check выход 1", f"выход {rc}, {short(out)}")
    report(rc == 1 and job["имя"] in out, f"3 в выводе --check названо задание «{job['имя']}»", short(out))


def case_manual_edit(root: Path) -> None:
    stand, rc, out = generated_stand(root, "4-manual-edit")
    parts = split_block(stand.jobs_text())
    if rc != 0 or parts is None:
        report(False, "4 ручная правка в порождённом блоке → --check выход 1", f"генерация не прошла: выход {rc}")
        return
    prefix, block, suffix = parts
    commands = [j["команда"] for j in stand.expected()["задания"]]
    command = next(c for c in commands if block.count(c) == 1)
    edited = block.replace(command, command + ".ручная-правка")
    stand.jobs.write_text(prefix + edited + suffix, encoding="utf-8")
    parsed = yaml.safe_load(stand.jobs_text()) or {}
    report(any(j.get("команда") == command + ".ручная-правка" for j in parsed.get("задания") or []),
           "4 стенд: команда задания внутри блока изменена руками")
    rc, out = stand.run("--check")
    report(rc == 1, "4 ручная правка в порождённом блоке → --check выход 1", f"выход {rc}, {short(out)}")


def case_marks_checked(root: Path) -> None:
    edits = (
        ("метки", lambda text: bump_first_value(text, "метки")),
        ("метки", lambda text: (insert_after_anchor(text, "метки", "  probe-mark: 7\n"), "probe-mark")),
        ("запас-порога", lambda text: bump_first_value(text, "запас-порога")),
        ("метки_без_задания",
         lambda text: (insert_after_anchor(text, "метки_без_задания", '  probe-mark: "проба"\n'), "probe-mark")),
    )
    for number, (section, edit) in enumerate(edits):
        stand, rc, out = generated_stand(root, f"5-marks-{number}")
        before = stand.expected()[section]
        text, key = edit(stand.library_text())
        stand.library.write_text(text, encoding="utf-8")
        after = stand.expected()[section]
        if before == after:
            report(False, f"5 стенд: правка «{section}.{key}» в библиотеке", "разбор не изменился")
            continue
        rc, out = stand.run("--check")
        report(rc == 1, f"5 правка «{section}.{key}» в библиотеке, system-jobs нет → --check выход 1",
               f"выход {rc}, {short(out)}")


def case_idempotent(root: Path) -> None:
    stand, rc, out = generated_stand(root, "6-idempotent")
    first = stand.jobs_text()
    rc2, out2 = stand.run()
    report(rc == 0 and rc2 == 0 and stand.jobs_text() == first,
           "6 повторный запуск генератора — файл байт в байт тот же",
           f"выходы {rc}/{rc2}, {short(out + out2)}" if rc != 0 or rc2 != 0 else "файл изменился")


def load_install_new():
    spec = importlib.util.spec_from_file_location("install_new_probe", INSTALL_NEW)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def case_insert_key(root: Path) -> None:
    stand, rc, out = generated_stand(root, "7-insert-key")
    text = stand.jobs_text()
    parts = split_block(text)
    if rc != 0 or parts is None:
        report(False, "7 вставить_ключ не пишет в порождённый раздел", f"генерация не прошла: выход {rc}")
        report(False, "7 вставить_ключ пишет в непорождённый раздел вне блока", f"генерация не прошла: выход {rc}")
        return
    insert = load_install_new().вставить_ключ
    new, reason = insert(text, "метки.probe-mark", "7")
    report(new == text and bool(reason),
           "7 вставить_ключ(«метки.…») на порождённом файле: якорь только вне блока → отказ, текст тот же",
           "вставил в порождённый блок" if new != text else "текст тот же, но без причины отказа")
    new, reason = insert(text, "ключи.PROBE_KEY", '"1"')
    moved = split_block(new)
    report(not reason and moved is not None and moved[:2] == parts[:2] and "PROBE_KEY" in moved[2],
           "7 вставить_ключ(«ключи.…»): вставлено вне блока, блок не тронут", reason or "вставка не там")


CASES = (case_generate, case_outside_untouched, case_library_changed, case_manual_edit,
         case_marks_checked, case_idempotent, case_insert_key)


def main() -> int:
    print(f"генератор: {GENERATOR}{'' if GENERATOR.is_file() else '  (НЕТ ФАЙЛА)'}")
    with tempfile.TemporaryDirectory(prefix="test-build-system-jobs-") as root:
        for case in CASES:
            try:
                case(Path(root))
            except Exception as error:  # случай назван, остальные идут дальше
                report(False, f"{case.__name__} упал", f"{type(error).__name__}: {error}")
    print(f"СБОРКА SYSTEM-JOBS: путей {paths}, неудач {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
