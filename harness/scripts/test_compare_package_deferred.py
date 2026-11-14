#!/usr/bin/env python3
"""Пробы: сверка с пакетом в дереве помощника откладывается до очереди слияния.

Спека задачи §5, Р-1, Т3. Контракт:
  HELPER_WORKTREE="1" (HARNESS_CONF, затем конфиг дерева; не «1» — 0) и корень
  `git rev-parse --show-toplevel` — СВЯЗАННОЕ дерево (`--git-dir` ≠ `--git-common-dir`),
  лежащее под `<родитель --git-common-dir>/.claude/worktrees/` → содержимое с пакетом не
  сверяется, печать «[сверка с пакетом] отложена: пакет догонит очередь слияния», код 0;
  строка журнала `docs/ИЗМЕНЕНИЯ-ПАКЕТА.md` за сегодня по-прежнему обязательна (иначе 1).
  Ключ 0, основное дерево или связанное дерево вне `.claude/worktrees/` (снимки
  gates-snapshot.sh) — сверка как раньше.
Стенд: свой git-проект и git-пакет, harness.conf с STARTER_DIR и ключом (HARNESS_CONF),
свой install.conf (HARNESS_INSTALL_CONF). Механизм — рядом или HARNESS_COMPARE_PACKAGE
(дверь для мутаций копии).

Прогон: python3 scripts/test_compare_package_deferred.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPARE = Path(os.environ.get("HARNESS_COMPARE_PACKAGE") or HERE / "compare-with-package.py")
DEFERRED = "отложена"
HARNESS_FILE = "scripts/tool.py"
JOURNAL_REL = "docs/ИЗМЕНЕНИЯ-ПАКЕТА.md"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

checked = failed = 0


def check(name: str, expected, actual) -> None:
    global checked, failed
    checked += 1
    if expected == actual:
        print(f"  ок    {name}")
    else:
        failed += 1
        print(f"  ПЛОХО {name}: ждали «{expected}», вышло «{actual}»")


def git(where: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(where), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **GIT_ENV}).stdout


class Stand:
    """proj (scripts/tool.py, журнал правок) и pkg (harness/scripts/tool.py) — одинаковые."""

    def __init__(self, base: Path, key: str | None):
        self.base = base
        self.proj, self.pkg = base / "proj", base / "pkg"
        for root, rel in ((self.proj, HARNESS_FILE), (self.pkg, "harness/scripts/tool.py")):
            (root / rel).parent.mkdir(parents=True)
            (root / rel).write_text("исходный\n", encoding="utf-8")
        (self.proj / ".gitignore").write_text(".claude/worktrees/\n", encoding="utf-8")
        (self.proj / JOURNAL_REL).parent.mkdir(parents=True)
        (self.proj / JOURNAL_REL).write_text("| дата | правка |\n", encoding="utf-8")
        for root in (self.proj, self.pkg):
            git(root, "init", "-q", "-b", "master")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "начальный")
        self.conf = base / "harness.conf"
        self.conf.write_text(f'STARTER_DIR="{self.pkg}"\n'
                             + (f'HELPER_WORKTREE="{key}"\n' if key is not None else ""),
                             encoding="utf-8")
        self.install = base / "install.conf"
        self.install.write_text(f'PROJECT_DIR="{self.proj}"\nAGENT_USER="нет-такого"\n',
                                encoding="utf-8")

    def tree(self, where: Path, branch: str) -> Path:
        git(self.proj, "worktree", "add", "-q", "-b", branch, str(where), "master")
        return where

    @staticmethod
    def diverge(root: Path, journal_today: bool = True) -> None:
        """Правка файла харнеса только в проекте (staged) — пакет не тронут."""
        (root / HARNESS_FILE).write_text("правка помощника\n", encoding="utf-8")
        git(root, "add", HARNESS_FILE)
        if journal_today:
            with (root / JOURNAL_REL).open("a", encoding="utf-8") as fh:
                fh.write(f"| {date.today().strftime('%d.%m')} | проба |\n")

    def run(self, cwd: Path) -> tuple[int, str]:
        env = {**os.environ, "HARNESS_CONF": str(self.conf),
               "HARNESS_INSTALL_CONF": str(self.install), "PYTHONDONTWRITEBYTECODE": "1"}
        for name in ("HARNESS_STARTER", "STARTER_DIR", "PROJECT_DIR", "GIT_DIR", "GIT_INDEX_FILE"):
            env.pop(name, None)
        proc = subprocess.run([sys.executable, str(COMPARE)], input=HARNESS_FILE + "\n",
                              capture_output=True, text=True, cwd=cwd, env=env, timeout=60)
        return proc.returncode, proc.stdout + proc.stderr


def case_deferred(tmp: Path) -> None:
    # Поломка, что оставила бы зелёной: нет — без режима «отложено» расхождение даёт 1.
    # Каталоги стенда латиницей: слово «отложена» в пути сделало бы печать зелёной даром.
    print("ключ 1, дерево под .claude/worktrees: сверка отложена")
    s = Stand(tmp / "t-deferred", "1")
    wt = s.tree(s.proj / ".claude" / "worktrees" / "agent-x", "worktree-agent-x")
    s.diverge(wt)
    code, out = s.run(wt)
    check("расхождение с пакетом в дереве помощника — код 0", 0, code)
    check("печать «отложена»", True, DEFERRED in out)
    check("пакет не тронут", "исходный\n",
          (s.pkg / "harness/scripts/tool.py").read_text(encoding="utf-8"))
    code, out = s.run(wt / "scripts")
    check("запуск из подкаталога дерева — тоже отложена (условие по корню, не по cwd)",
          (0, True), (code, DEFERRED in out))


def case_journal_still_required(tmp: Path) -> None:
    # Поломка, что оставила бы зелёной: «отложено» как ранний return 0 до проверки журнала.
    print("ключ 1, дерево: строка журнала за сегодня обязательна")
    s = Stand(tmp / "t-journal", "1")
    wt = s.tree(s.proj / ".claude" / "worktrees" / "agent-y", "worktree-agent-y")
    s.diverge(wt, journal_today=False)
    code, out = s.run(wt)
    check("без строки журнала за сегодня — код 1", 1, code)
    check("и отказ называет журнал правок, а не пакет", (True, False),
          (JOURNAL_REL.split("/")[-1] in out, "РАСХОЖДЕНИЕ С ПАКЕТОМ" in out))


def case_key_off(tmp: Path) -> None:
    # Поломка, что оставила бы зелёной: откладывать по одному пути без ключа — строки 1-2.
    print("ключ 0 / нет ключа: сверка как раньше и в дереве")
    for key in ("0", None):
        s = Stand(tmp / f"t-key-{key}", key)
        wt = s.tree(s.proj / ".claude" / "worktrees" / "agent-z", "worktree-agent-z")
        s.diverge(wt)
        code, out = s.run(wt)
        check(f"ключ {key!r}: расхождение в дереве — код 1", 1, code)
        check(f"ключ {key!r}: слова «отложена» нет", False, DEFERRED in out)


def case_outside(tmp: Path) -> None:
    # Поломка, что оставила бы зелёной: условие «любое связанное дерево» (строка снимка) или
    # «путь содержит .claude/worktrees» без git (строка подложного каталога).
    print("ключ 1, корень вне дерева помощника: сверка как раньше")
    s = Stand(tmp / "t-outside", "1")
    s.diverge(s.proj)
    code, out = s.run(s.proj)
    check("основное дерево — код 1, не отложена", (1, False), (code, DEFERRED in out))
    snap = s.tree(tmp / "t-snapshot" / "20261113-000000-abc", "snap")
    s.diverge(snap)
    code, out = s.run(snap)
    check("связанное дерево вне .claude/worktrees (снимок ворот) — код 1", (1, False),
          (code, DEFERRED in out))
    fake = tmp / "t-fake" / ".claude" / "worktrees" / "agent-f"
    fake.mkdir(parents=True)
    git(fake, "init", "-q", "-b", "master")
    (fake / HARNESS_FILE).parent.mkdir(parents=True)
    (fake / JOURNAL_REL).parent.mkdir(parents=True)
    (fake / JOURNAL_REL).write_text("", encoding="utf-8")
    s.diverge(fake)
    code, out = s.run(fake)
    check("отдельный репозиторий в каталоге .claude/worktrees — код 1", (1, False),
          (code, DEFERRED in out))


CASES = (case_deferred, case_journal_still_required, case_key_off, case_outside)


def main() -> int:
    global checked, failed
    if not COMPARE.exists():
        print(f"нет {COMPARE}")
        print("SELFTEST: КРАСНЫЙ (механизма нет)")
        return 1
    with tempfile.TemporaryDirectory() as raw:
        for case in CASES:
            try:
                case(Path(raw))
            except Exception as exc:  # noqa: BLE001 — падение случая = провал, не падение пробы
                checked += 1
                failed += 1
                print(f"  ПЛОХО {case.__name__}: исключение {type(exc).__name__}: {exc}")
    print(f"SELFTEST: {'зелёный' if not failed else 'КРАСНЫЙ'} ({checked} проверок, "
          f"{len(CASES)} случаев, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
