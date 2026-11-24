#!/usr/bin/env python3
"""Проба строк харнеса в .gitignore проекта (scripts/lib/ignore_lines.py).

Откуда взято. Копия сети (находка 11.11.2026): раскладчик писал .gitignore
заготовкой только при его отсутствии, и уже установленный проект не получал
строк новых механизмов (.tasks/*/живьём, .tasks/*/живьём-*) — следы живой
проверки ложились в git. Модуль дописывает недостающие строки в живой файл,
не трогая чужих.

Стенд — свой временный каталог; живой .gitignore проекта не читается.

Прогон:
    python3 scripts/test_ignore_lines.py
Код возврата: 0 — все случаи прошли, 1 — есть упавший.
"""
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "scripts" / "lib" / "ignore_lines.py"
# Строки находки: без них следы живой проверки уезжают в коммиты копии.
FINDING_LINES = (".tasks/*/живьём", ".tasks/*/живьём-*")
OWN_TEXT = "# свой файл копии\nnode_modules/\n.tasks/*/живьём-старое"  # без перевода строки в конце

failures = 0
cases = 0


def report(name: str, troubles: list) -> None:
    global failures, cases
    cases += 1
    if troubles:
        failures += 1
        print(f"  ПЛОХО {name}: {'; '.join(troubles)}")
    else:
        print(f"  ок    {name}")


def load_module():
    if not MODULE.is_file():
        return None
    spec = importlib.util.spec_from_file_location("ignore_lines", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def once_each(text: str, lines) -> list:
    rows = text.split("\n")
    wrong = {line: rows.count(line) for line in lines if rows.count(line) != 1}
    return [f"строки не по одному разу: {wrong}"] if wrong else []


def case_created(module, stand: Path) -> list:
    """(1) файла нет → создан, каждая строка харнеса по разу, строки находки среди них."""
    path = stand / ".gitignore"
    said = module.ensure(path)
    if not path.is_file():
        return [f"файл не создан, итог {said!r}"]
    troubles = once_each(path.read_text(encoding="utf-8"), module.LINES)
    absent = [line for line in FINDING_LINES if line not in module.LINES]
    if absent:
        troubles.append(f"строк находки нет в перечне харнеса: {absent}")
    if "создан" not in said:
        troubles.append(f"итог не сказал «создан»: {said!r}")
    return troubles


def case_live_appended(module, stand: Path) -> list:
    """(2) живой файл копии без строк харнеса → свой текст цел, недостающие дописаны по разу."""
    path = stand / ".gitignore"
    path.write_text(OWN_TEXT, encoding="utf-8")
    said = module.ensure(path)
    text = path.read_text(encoding="utf-8")
    troubles = once_each(text, module.LINES)
    if not text.startswith(OWN_TEXT + "\n"):
        troubles.append("свой текст копии изменён (дописывать можно только в конец)")
    if "дописано" not in said or FINDING_LINES[0] not in said:
        troubles.append(f"итог не назвал дописанные строки: {said!r}")
    return troubles


def case_repeat_empty(module, stand: Path) -> list:
    """(3) повтор раскладки → файл побайтно тот же, итог «на месте»."""
    path = stand / ".gitignore"
    path.write_text(OWN_TEXT, encoding="utf-8")
    module.ensure(path)
    first = path.read_bytes()
    said = module.ensure(path)
    troubles = []
    if path.read_bytes() != first:
        troubles.append("повтор изменил файл")
    if "на месте" not in said:
        troubles.append(f"итог повтора не «на месте»: {said!r}")
    return troubles


def case_commented_is_missing(module, stand: Path) -> list:
    """(4) строка закомментирована или с отступом → не считается стоящей, дописана."""
    path = stand / ".gitignore"
    path.write_text(f"# {FINDING_LINES[0]}\n  {FINDING_LINES[1]}\n", encoding="utf-8")
    module.ensure(path)
    return once_each(path.read_text(encoding="utf-8"), FINDING_LINES)


def case_git_ignores(module, stand: Path) -> list:
    """(5) живой .gitignore после досылки: git не видит след живой проверки задачи."""
    subprocess.run(["git", "init", "-q", str(stand)], check=True, capture_output=True)
    path = stand / ".gitignore"
    path.write_text(OWN_TEXT, encoding="utf-8")
    module.ensure(path)
    trace = stand / ".tasks" / "hr.x" / "живьём-01" / "след.txt"
    trace.parent.mkdir(parents=True)
    trace.write_text("след\n", encoding="utf-8")
    done = subprocess.run(["git", "-C", str(stand), "-c", "core.quotePath=false",
                           "status", "--porcelain", "-uall", "-z"],
                          capture_output=True, text=True, check=True)
    seen = [entry[3:] for entry in done.stdout.split("\0") if entry]
    if ".gitignore" not in seen:
        return [f"git status не видит даже .gitignore стенда: {seen}"]
    leaked = [entry for entry in seen if entry.startswith(".tasks/")]
    return [f"след живой проверки виден git: {leaked}"] if leaked else []


def main() -> int:
    module = load_module()
    print("── .gitignore проекта: строки харнеса досылаются ──")
    for name, case in (
            ("(1) файла нет → создан со строками харнеса", case_created),
            ("(2) живой файл: свой текст цел, недостающие дописаны по разу", case_live_appended),
            ("(3) повтор не меняет файл", case_repeat_empty),
            ("(4) закомментированная строка не считается стоящей", case_commented_is_missing),
            ("(5) git не видит след живой проверки после досылки", case_git_ignores)):
        if module is None:
            report(name, [f"модуля нет: {MODULE}"])
            continue
        stand = Path(tempfile.mkdtemp(prefix="proba-ignore-lines-"))
        try:
            troubles = case(module, stand)
        except Exception as error:  # noqa: BLE001 — сбой случая и есть находка
            troubles = [f"исключение {type(error).__name__}: {error}"]
        finally:
            shutil.rmtree(stand, ignore_errors=True)
        report(name, troubles)
    print(f"GITIGNORE ПРОЕКТА: случаев {cases}, неудач {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
