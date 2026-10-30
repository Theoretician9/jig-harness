#!/usr/bin/env python3
"""Проба: правило secrets шага «безопасность» в режиме задачи видит секрет,
который добавила задача, хотя check-secrets.sh печатает находку без значения.

Почему отдельный файл. check-secrets.sh (И-3) печатает находку так:
    СЕКРЕТ? <путь>:<номер>: <вид> [скрыто]
Текста строки в выводе больше нет, и сверка «текст находки == добавленная
задачей строка» в rule_secrets (scripts/check-security.py) отбрасывает ВСЁ —
режим --task говорит «чисто» при секрете в коммите задачи.

КОНТРАКТ для rule_secrets в режиме задачи (ctx.task_id не None):
  * находка check-secrets.sh берётся, если её (путь, номер строки) входит в
    {(путь, n) для n, _ из ctx.raw[путь]} — то есть строку с этим номером в
    этом файле добавила задача. Текст находки в сверке не участвует;
  * совпадение только номера в ДРУГОМ файле и только файла при ДРУГОМ номере —
    не находка (стенд держит оба капкана);
  * в поле text находки нет значения секрета (ни куска длиннее 4 знаков).
Режим --staged (task_id None) этой пробой не судится.

Случаи:
  0. контроль стенда: сам check-secrets.sh находит все четыре строки;
  1. задача добавила две строки с секретами → находки secrets ровно на
     app/conf.py:3 и app/conf.py:4;
  2. секрет в строке, которую задача не добавляла (app/conf.py:2 — тот же
     файл, app/old.cfg:3 — тот же номер), → находки нет;
  3. в находках нет куска ни одного значения (без находок судить нечего —
     случай красный, а не зелёный на пустом месте).

Значения порождаются при каждом прогоне и не печатаются; префикс формата
github и имя ключа собраны из частей, чтобы сама проба не краснила
check-secrets.sh на дереве. Механизм берётся РЯДОМ с пробой (подмена
«из-копии» в probe-mutations.yaml): из копии дерева судится копия.

Запуск: python3 scripts/test_security_secrets_masked.py; 0 — зелёная, 1 — красная.
"""
from __future__ import annotations

import importlib.util
import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "check-security.py"
TASK = "t.mask-1"
CHECKLIST_NAME = "security-check-list.yaml"
ALPHABET = string.ascii_letters + string.digits


def probe_checklist() -> Path:
    """Чек-лист — данные, не механизм: в копии дерева (прибор мутаций копирует
    только scripts/) его рядом нет, тогда он берётся из каталога запуска."""
    for candidate in (HERE.parent / "harness" / "config" / CHECKLIST_NAME,
                      HERE.parent / "config" / CHECKLIST_NAME,
                      Path.cwd() / "harness" / "config" / CHECKLIST_NAME):
        if candidate.is_file():
            return candidate
    raise SystemExit(f"чек-лист {CHECKLIST_NAME} не найден")


def rand(length: int) -> str:
    # Цифра в конце обязательна: безкавычечное присвоение без цифры check-secrets не ловит.
    return "".join(secrets.choice(ALPHABET) for _ in range(length - 1)) + secrets.choice(string.digits)


def git(root, *args):
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.quotepath=false",
         "-C", str(root), *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"git {args[0]}: {done.stderr.strip()}")
    return done.stdout


def write(root, name, text):
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def leaks(text: str, value: str) -> bool:
    return any(value[i:i + 5] in text for i in range(len(value) - 4))


def build_stand(tmp: Path) -> tuple[Path, list[str]]:
    """Репозиторий: базовый коммит без задачи и коммит задачи поверх."""
    root = tmp / "repo"
    root.mkdir()
    git(root, "init", "-q")
    gh = "gh" + "p_"
    key = "DB_PASS" + "WORD"
    old_assign, old_gh = rand(24), gh + rand(36)
    new_gh, new_assign = gh + rand(36), rand(24)
    write(root, "app/conf.py", f"# конфиг\n{key}={old_assign}\n")
    write(root, "app/old.cfg", f"a = 1\nb = 2\nGH = {old_gh}\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "база\n")
    write(root, "app/conf.py", f"# конфиг\n{key}={old_assign}\nGH = {new_gh}\nAPI_{key}={new_assign}\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", f"правка задачи\n\nDev-Map: {TASK}\n")
    return root, [old_assign, old_gh, new_gh, new_assign]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="proba-secrets-masked-"))
    results = []

    def check(name, ok, why=""):
        results.append(ok)
        print(f"  {'ок   ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f": {why}"))

    try:
        # Паспорт пустой: каталог ключей живой установки к стенду отношения не имеет.
        for var in ("HARNESS_INSTALL_CONF", "HARNESS_CONF"):
            (tmp / var).write_text("", encoding="utf-8")
            os.environ[var] = str(tmp / var)
        os.environ["SECURITY_CHECKLIST"] = str(probe_checklist())
        root, values = build_stand(tmp)

        raw = subprocess.run(["bash", str(HERE / "check-secrets.sh"), str(root)],
                             capture_output=True, text=True, timeout=120).stdout
        expected_raw = ["app/conf.py:2:", "app/conf.py:3:", "app/conf.py:4:", "app/old.cfg:3:"]
        missing = [m for m in expected_raw if f"СЕКРЕТ? {m}" not in raw]
        check("0 контроль: check-secrets.sh видит все четыре строки стенда", not missing,
              f"не найдены {missing}")

        spec = importlib.util.spec_from_file_location("check_security", MODULE_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        findings, _ = mod.run_rules(TASK, root, mod.load_checklist())
        got = sorted((f["file"], f["line"]) for f in findings if f["rule"] == "secrets")

        want = [("app/conf.py", 3), ("app/conf.py", 4)]
        check("1 секреты в добавленных задачей строках — находки на app/conf.py:3 и :4",
              all(w in got for w in want), f"находки secrets {got}, ждали {want}")
        foreign = [g for g in got if g not in want]
        check("2 секрет в строке не задачи (тот же файл / тот же номер) — не находка",
              not foreign, f"лишние находки {foreign}")
        secret_findings = [f for f in findings if f["rule"] == "secrets"]
        leaked = sum(1 for f in secret_findings
                     if any(leaks(json.dumps(f, ensure_ascii=False), v) for v in values))
        check("3 в находках нет значения секрета", bool(secret_findings) and not leaked,
              "находок нет — судить нечего" if not secret_findings
              else f"значение в {leaked} находках (не печатается)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = results.count(False)
    print(f"СЕКРЕТЫ-ПОД-МАСКОЙ: случаев {len(results)}, неудач {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
