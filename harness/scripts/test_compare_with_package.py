#!/usr/bin/env python3
"""Пробы гейта сверки с пакетом (package_sync): судить только пути ЭТОГО коммита.

Откуда. 11–12.11.2026, четыре случая за вечер: при трёх параллельных задачах
гейт отказывал коммиту проекта, потому что в пакете лежал незакоммиченный файл
ДРУГОЙ задачи (её пара в проекте уже закоммичена или пары нет вовсе). Каждая
задача держала коммиты остальных.

Контракт гейта `scripts/compare-with-package.py` (stdin — staged-пути, cwd —
проект, пакет — STARTER_DIR из HARNESS_CONF):
  (а) коммит путей A, в пакете грязен чужой путь B → rc 0;
  (б) двойник A в пакете незакоммичен → rc 1, в отчёте путь двойника;
  (в) двойник A в пакете закоммичен, но отличается от файла коммита → rc 1;
  (г) в коммите нет файлов харнеса → гейт неприменим, rc 0.
  (д) process-library.json расходится только полем «замер» → rc 0, иным → rc 1.
Дверь пробы: COMPARE_WITH_PACKAGE_GATE — путь копии гейта (мутации).

Прогон: python3 scripts/test_compare_with_package.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

GATE = Path(os.environ.get("COMPARE_WITH_PACKAGE_GATE")
            or Path(__file__).resolve().parent / "compare-with-package.py")
JOURNAL = "docs/ИЗМЕНЕНИЯ-ПАКЕТА.md"

paths = 0
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global paths, failures
    paths += 1
    if ok:
        print(f"  ок    {name}")
    else:
        failures += 1
        print(f"  ПЛОХО {name}" + (f": {detail}" if detail else ""))


def git(where: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(where), "-c", "user.name=p", "-c", "user.email=p@p", *args],
                   capture_output=True, check=True)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def stand(base: Path, name: str) -> tuple[Path, Path, Path]:
    """Проект и пакет, где всё сходится и закоммичено; каждый случай отличается
    от него одним изменением. Возвращает (проект, пакет, harness.conf)."""
    prj, pkg = base / f"{name}-prj", base / f"{name}-pkg"
    for root, sub in ((prj, "scripts"), (pkg, "harness/scripts")):
        for f in ("a.sh", "b.sh"):
            write(root / sub / f, "исходный\n")
        git(root, "init", "-q")
    write(prj / JOURNAL, f"# журнал\n\n| {date.today():%d.%m} | правка | доказано здесь |\n")
    write(pkg / "README.md", "пакет\n")
    for root in (prj, pkg):
        git(root, "add", "-A")
        git(root, "commit", "-qm", "начальный")
    conf = base / f"{name}-harness.conf"
    write(conf, f'STARTER_DIR="{pkg}"\n')
    return prj, pkg, conf


def run_gate(prj: Path, conf: Path, staged: list[str]) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if k != "GIT_INDEX_FILE"}
    env["HARNESS_CONF"] = str(conf)
    r = subprocess.run([sys.executable, str(GATE)], input="\n".join(staged) + "\n",
                       capture_output=True, text=True, cwd=prj, env=env, timeout=60)
    return r.returncode, r.stdout + r.stderr


def stage_change(prj: Path, pkg: Path, text: str, commit_pkg: bool) -> None:
    """Коммит проекта меняет A; в пакете двойник A — та же правка."""
    write(prj / "scripts/a.sh", text)
    git(prj, "add", "scripts/a.sh")
    write(pkg / "harness/scripts/a.sh", text)
    if commit_pkg:
        git(pkg, "commit", "-qam", "двойник A")


def main() -> int:
    if not GATE.is_file():
        print(f"гейта нет: {GATE}")
        return 1
    base = Path(tempfile.mkdtemp(prefix="proba-pkgsync-"))
    try:
        # (а1) Чужой B грязен в пакете, его пара в проекте уже закоммичена
        # (задача закоммитила проект, пакет — ещё нет).
        prj, pkg, conf = stand(base, "a1")
        stage_change(prj, pkg, "правка A\n", commit_pkg=True)
        write(pkg / "harness/scripts/b.sh", "чужая правка B\n")
        rc, out = run_gate(prj, conf, ["scripts/a.sh"])
        check("(а1) чужой грязный B (пара в проекте чиста) — коммит A проходит", rc == 0, out)

        # (а2) Чужой грязный файл пакета без пары в проекте.
        prj, pkg, conf = stand(base, "a2")
        stage_change(prj, pkg, "правка A\n", commit_pkg=True)
        write(pkg / "README.md", "чужая правка README\n")
        rc, out = run_gate(prj, conf, ["scripts/a.sh"])
        check("(а2) чужой грязный README.md пакета без пары — коммит A проходит", rc == 0, out)

        # (а3) Чужой новый неотслеживаемый файл пакета, пара в проекте закоммичена.
        prj, pkg, conf = stand(base, "a3")
        stage_change(prj, pkg, "правка A\n", commit_pkg=True)
        write(prj / "scripts/c.sh", "новый\n")
        git(prj, "add", "scripts/c.sh")
        git(prj, "commit", "-qm", "c")
        write(pkg / "harness/scripts/c.sh", "новый\n")
        rc, out = run_gate(prj, conf, ["scripts/a.sh"])
        check("(а3) чужой новый c.sh в пакете — коммит A проходит", rc == 0, out)

        # (б) Двойник A в пакете совпадает, но не закоммичен.
        prj, pkg, conf = stand(base, "b")
        stage_change(prj, pkg, "правка A\n", commit_pkg=False)
        rc, out = run_gate(prj, conf, ["scripts/a.sh"])
        check("(б) двойник A незакоммичен в пакете — отказ", rc == 1, out)
        check("(б) …и назван путь двойника", "harness/scripts/a.sh" in out, out)

        # (б2) То же при чужой грязи рядом: отказ не теряется.
        prj, pkg, conf = stand(base, "b2")
        stage_change(prj, pkg, "правка A\n", commit_pkg=False)
        write(pkg / "README.md", "чужая правка README\n")
        rc, out = run_gate(prj, conf, ["scripts/a.sh"])
        check("(б2) двойник A незакоммичен + чужая грязь — отказ", rc == 1, out)
        check("(б2) …и назван путь двойника", "harness/scripts/a.sh" in out, out)

        # (в) Двойник A закоммичен в пакете, но отличается от файла коммита.
        prj, pkg, conf = stand(base, "v")
        stage_change(prj, pkg, "правка A в пакете\n", commit_pkg=True)
        write(prj / "scripts/a.sh", "другая правка A\n")
        git(prj, "add", "scripts/a.sh")
        rc, out = run_gate(prj, conf, ["scripts/a.sh"])
        check("(в) двойник A закоммичен, но расходится — отказ", rc == 1, out)
        check("(в) …и назван путь", "scripts/a.sh" in out, out)

        # (г) В коммите нет файлов харнеса: пакет грязен, журнала за сегодня нет.
        prj, pkg, conf = stand(base, "g")
        write(prj / JOURNAL, "# журнал\n")
        write(prj / "STATE.md", "состояние\n")
        git(prj, "add", "STATE.md", JOURNAL)
        write(pkg / "harness/scripts/b.sh", "чужая правка B\n")
        write(pkg / "README.md", "чужая правка README\n")
        rc, out = run_gate(prj, conf, ["STATE.md", JOURNAL])
        check("(г) коммит без файлов харнеса — гейт неприменим (rc 0)", rc == 0, out)
        check("(г) …и сказано «файлов харнеса в коммите: 0»", "в коммите: 0" in out, out)

        # (д) Библиотека процессов: поле «замер» пишет живой диспетчер на сервере,
        # в пакете оно «не измерено» — сверка его не судит, а всё остальное судит.
        def library(замер: str, имя: str) -> str:
            return ('{"процессы": [{"имя": "%s", "доля_нагрузки": '
                    '{"приписка": "п", "замер": %s}}]}\n' % (имя, замер))
        lib = "harness/config/process-library.json"   # путь один в проекте и в пакете
        for case, prj_text, rc_want, title in (
                ("d1", library('{"доля": 0.4}', "сборщик"), 0,
                 "(д1) библиотека расходится только «замером» — rc 0"),
                ("d2", library('{"доля": 0.4}', "другой"), 1,
                 "(д2) библиотека расходится не только «замером» — отказ")):
            prj, pkg, conf = stand(base, case)
            write(pkg / lib, library('"не измерено"', "сборщик"))
            git(pkg, "add", "-A")
            git(pkg, "commit", "-qm", "библиотека")
            write(prj / lib, prj_text)
            git(prj, "add", lib)
            rc, out = run_gate(prj, conf, [lib])
            check(title, rc == rc_want, out)

        # Здоровый: двойник закоммичен и совпадает, пакет чист.
        prj, pkg, conf = stand(base, "ok")
        stage_change(prj, pkg, "правка A\n", commit_pkg=True)
        rc, out = run_gate(prj, conf, ["scripts/a.sh"])
        check("здоровый: двойник закоммичен и совпадает — rc 0", rc == 0, out)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print(f"ИТОГ: путей {paths}, провалов {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
