#!/usr/bin/env python3
"""Пробы прибора набора памяти: список панели равен записям пакета.

Откуда. Разбор правок соседней установки (18.10.2026, п. 3.11): прибор
`scripts/memory-to-package.py` клал и убирал файлы пакета, а список
`harness/config/memory-set.txt`, который читает панель, не трогал — реестр
говорил «пишет», панель видела 14 имён при 27 записях пакета.

Договор: после «включить» и «исключить» строки списка без «#» — ровно
записи пакета по алфавиту, строки-комментарии шапки остаются как были.

Стенд — свой пакет, своя память, свой список и свой LOG_DIR во временном
каталоге. Пакет стенда создаётся ДО зова: на недоступный для записи каталог
прибор перезапускает себя через `sudo -n` без окружения, и тот запуск работает
с НАСТОЯЩИМ пакетом. Поэтому проба отказывается звать прибор без стенда и
сличает настоящий каталог памяти пакета до и после.

Дверь пробы: HARNESS_MEMORY_TO_PACKAGE — путь копии прибора (мутации).
Прогон: python3 scripts/test_memory_to_package.py
"""
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import config  # noqa: E402

TOOL = Path(os.environ.get("HARNESS_MEMORY_TO_PACKAGE")
            or Path(__file__).resolve().parent / "memory-to-package.py")
REAL_MEMORY = config.katalog_paketa() / "harness" / "память"
REAL_SET = config.корень_дерева(__file__) / "harness" / "config" / "memory-set.txt"
# Имя, которого нет ни в настоящей памяти проекта, ни в настоящем пакете: если
# прибор всё же уйдёт в настоящий sudo (подмена в PATH не сработала), он
# откажет «записи нет» раньше, чем тронет пакет или список панели.
NOWHERE_NAME = "zz-stand-only-probe.md"
HEADER = ["# Записи памяти для новой установки.", "#", "# Список правит прибор набора."]

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


def snapshot(directory: Path) -> dict:
    if not directory.is_dir():
        return {}
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.iterdir()) if p.is_file()}


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def stand(base: Path) -> dict:
    """Пакет с двумя записями, память проекта с тремя, список с шапкой и
    устаревшей строкой: устаревшая строка различает «переписан» и «дописан»."""
    write(base / "pkg/harness/память/MEMORY.md", "# указатель\n")
    write(base / "pkg/harness/память/alpha.md", "альфа\n")
    for name in ("alpha.md", "beta.md", "gamma.md"):
        write(base / "prj" / name, f"{name}\n")
    write(base / "set.txt", "\n".join(HEADER + ["MEMORY.md", "alpha.md", "stale.md"]) + "\n")
    (base / "log").mkdir()
    env = dict(os.environ)
    env.update(HARNESS_STARTER=str(base / "pkg"), HARNESS_PAMYAT=str(base / "prj"),
               HARNESS_NABOR=str(base / "set.txt"), LOG_DIR=str(base / "log"),
               PYTHONDONTWRITEBYTECODE="1")
    return env


def run_tool(env: dict, *args: str) -> tuple[int, str]:
    package = Path(env["HARNESS_STARTER"]) / "harness" / "память"
    # Без стенда прибор уходит в sudo и судит НАСТОЯЩИЙ пакет: не звать вовсе.
    if not (package.is_dir() and os.access(package, os.W_OK)):
        return -1, f"стенда нет: {package} — прибор не зван"
    done = subprocess.run([sys.executable, str(TOOL), *args], env=env,
                          capture_output=True, text=True, timeout=60)
    return done.returncode, done.stdout + done.stderr


def set_parts(path: Path) -> tuple[list, list]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [l for l in lines if l.startswith("#")], [l for l in lines if l and not l.startswith("#")]


def package_names(env: dict) -> list:
    return sorted(p.name for p in (Path(env["HARNESS_STARTER"]) / "harness" / "память").glob("*.md"))


def cases() -> None:
    if not TOOL.is_file():
        check("прибор набора существует", False, f"нет {TOOL}")
        return
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        env = stand(base)

        rc, out = run_tool(env, "beta.md", "включить")
        check("включить — rc 0", rc == 0, f"rc {rc}: {out.strip()[:300]}")
        header, names = set_parts(base / "set.txt")
        check("БОЛЬНОЙ 3.11: после «включить» список равен записям пакета",
              names == package_names(env) == ["MEMORY.md", "alpha.md", "beta.md"],
              f"список {names}, пакет {package_names(env)}")
        check("после «включить» шапка списка цела", header == HEADER, f"{header}")

        rc, out = run_tool(env, "alpha.md", "исключить")
        check("исключить — rc 0", rc == 0, f"rc {rc}: {out.strip()[:300]}")
        header, names = set_parts(base / "set.txt")
        check("БОЛЬНОЙ 3.11: после «исключить» имени в списке нет, список равен пакету",
              names == package_names(env) == ["MEMORY.md", "beta.md"],
              f"список {names}, пакет {package_names(env)}")
        check("после «исключить» шапка списка цела", header == HEADER, f"{header}")

        check("журнал прибора лёг в LOG_DIR стенда",
              (base / "log" / "панель.jsonl").is_file(), "панель.jsonl в стенде нет")

        rc, out = run_tool(env, "gamma.md", "включить")
        check("отказ на чужом имени не трогает список: нет записи — rc 5",
              run_tool(env, "nope.md", "включить")[0] == 5)
        check("список после отказа — прежний", set_parts(base / "set.txt")[1] == package_names(env),
              f"{set_parts(base / 'set.txt')[1]}")


def unwritable_package_case() -> None:
    """VG-05: стенд задал HARNESS_STARTER на пакет без права записи. Прибор
    перезапускал себя через `sudo -n` без окружения, и тот запуск работал с
    НАСТОЯЩИМ пакетом. Правило: окружение стенда задано — отказ словами,
    sudo не зовётся. `sudo` подменён в PATH стенда: пишет метку и не делает
    НИЧЕГО (код 0 — чтобы прибор, отдавший его код, был виден)."""
    real_project_memory = config.корень_дерева(__file__) / "память"
    if (real_project_memory / NOWHERE_NAME).exists() or (REAL_MEMORY / NOWHERE_NAME).exists():
        check("VG-05: имя стенда не занято в настоящих памяти и пакете", False, NOWHERE_NAME)
        return
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        env = stand(base)
        write(base / "prj" / NOWHERE_NAME, "стенд\n")
        package = Path(env["HARNESS_STARTER"]) / "harness" / "память"
        mark = base / "sudo-called"
        fake = base / "bin" / "sudo"
        write(fake, f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{mark}'\nexit 0\n")
        fake.chmod(0o755)
        env["PATH"] = f"{base / 'bin'}:{env.get('PATH', '')}"
        package.chmod(0o555)
        try:
            if os.access(package, os.W_OK):
                check("VG-05: пакет стенда закрыт на запись", False, "запись открыта (прогон от root?) — судить нечем")
                return
            done = subprocess.run([sys.executable, str(TOOL), NOWHERE_NAME, "включить"], env=env,
                                  capture_output=True, text=True, timeout=60)
        finally:
            package.chmod(0o755)
        out = done.stdout + done.stderr
        check("БОЛЬНОЙ VG-05: пакет стенда без записи — sudo не зван",
              not mark.exists(), f"sudo зван: {mark.read_text(encoding='utf-8').strip() if mark.exists() else ''}")
        check("БОЛЬНОЙ VG-05: отказ словами — rc 5, «отказ» и путь пакета стенда",
              done.returncode == 5 and "отказ" in out and str(package) in out,
              f"rc {done.returncode}: {out.strip()[:300]}")
        check("VG-05: в пакет стенда запись не легла", not (package / NOWHERE_NAME).exists())


def main() -> int:
    before = snapshot(REAL_MEMORY)
    set_before = REAL_SET.read_bytes() if REAL_SET.is_file() else None
    print("[стенд]")
    cases()
    unwritable_package_case()
    print("[настоящий пакет]")
    check("в настоящем каталоге памяти пакета есть записи (иначе сличать нечего)", bool(before),
          f"пусто: {REAL_MEMORY}")
    after = snapshot(REAL_MEMORY)
    changed = sorted(n for n in set(before) | set(after) if before.get(n) != after.get(n))
    check("настоящий пакет не тронут пробой", not changed, " ".join(changed))
    set_after = REAL_SET.read_bytes() if REAL_SET.is_file() else None
    check("настоящий список панели memory-set.txt не тронут пробой", set_before == set_after, str(REAL_SET))
    if failures:
        print(f"SELFTEST: КРАСНЫЙ ({failures} из {paths} путей)")
        return 1
    print(f"SELFTEST: зелёный ({paths} путей; первый больной — список панели 14 имён при 27 записях, 18.10)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
