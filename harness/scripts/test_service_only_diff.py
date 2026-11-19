#!/usr/bin/env python3
"""Проба единого судьи класса служебных путей: `service-only-diff.py`.

Откуда. Спека тяжёлых прогонов, разделы Е и Ж; ревью ЕЖ п. 3, 5, 6: принадлежность
пути к классу DAEMON_OWNED_PATHS уже разбирает встроенный python stop_reminder.sh, Е
добавил бы третий разбор в bash, Ж — четвёртый. Судья один: `--путь` зовут memory-commit.sh
(шаг Е.3) и stop_reminder.sh; режимы разницы зовут gates.sh::gates_clean_at_end и
publish.sh::gates_summary_reusable (раздел Ж).

Контракт.
  * Класс — ключ DAEMON_OWNED_PATHS из harness.conf (дверь HARNESS_CONF), пути через
    пробел, «/» на конце — каталог целиком. Пустой или отсутствующий ключ — код 1 в
    любом режиме (пустой разбор не делает всё служебным).
  * `--путь <p>` — код 0: путь в классе, 1 — нет.
  * `<от> <до> [--под <префикс>]` — разница двух коммитов репозитория текущего каталога.
    Код 0 — каждый изменённый путь в классе (и под префиксом, если он назван) и у каждого
    только добавленные строки: `git diff --numstat -z --no-renames`, удалений 0. Двоичный
    («-» в numstat) — отказ; переименование и удаление — отказ (без --no-renames numstat
    даёт путь «a => b» с 0/0, и разбор его пропустил бы); новый файл класса (+N/−0) годен.
    Отметки — данные проверок «указания» и «задачи из канала»: новая строка гасит долг,
    удалённая его воскрешает (ревью ЕЖ п. 5, 8).
  * `<от> [--под <префикс>]` — то же против РАБОЧЕГО дерева (индекс и правки на диске),
    плюс неотслеженный файл вне класса — отказ; неотслеженный новый файл класса годен
    (ревью ЕЖ п. 3: служебные отметки лежат незакоммиченными до часа).
  * `--под память/` — фильтр Ж: `docs/приёмка/` в ключе есть, но его читают проверки,
    поэтому для итога ворот он не служебный (ревью ЕЖ п. 6).

Стенд — свой harness.conf и свой git-репозиторий во временном каталоге на случай;
боевой конфиг и проект не читаются.
Дверь мутаций: HARNESS_SERVICE_ONLY_DIFF — путь к копии судьи.

Прогон: python3 scripts/test_service_only_diff.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
JUDGE = Path(os.environ.get("HARNESS_SERVICE_ONLY_DIFF") or HERE / "service-only-diff.py")
KEY = 'DAEMON_OWNED_PATHS="память/.tasks-triaged память/.orders-processed docs/приёмка/"'

# (строка ключа, путь, ждём код, поломка, которую ловит)
CASES = (
    (KEY, "память/.tasks-triaged", 0, "файл класса не узнан — демон коммитил бы отметки сразу"),
    (KEY, "память/.orders-processed", 0, "в классе только первый путь ключа"),
    (KEY, "docs/приёмка/очередь.md", 0, "«/» на конце не понят как каталог"),
    (KEY, "память/ORDERS.md", 1, "правка владельца признана служебной — ждала бы час"),
    (KEY, "память/.tasks-triaged.bak", 1, "файл класса судится префиксом, а не целиком"),
    (KEY, "docs/приёмка-старая/запись.md", 1, "каталог судится префиксом строки без «/»"),
    (KEY, "x/память/.tasks-triaged", 1, "путь судится концом строки, а не от корня"),
    ("DAEMON_OWNED_PATHS=\"\"", "память/.tasks-triaged", 1, "пустой ключ сделал путь служебным"),
    ("", "память/.tasks-triaged", 1, "нет ключа — класс вшит в код"),
)


def judge(tmp: Path, key_line: str, *args: str, cwd: Path | None = None) -> tuple[int, str]:
    conf = tmp / "harness.conf"
    conf.write_text(key_line + "\n", encoding="utf-8")
    env = {**os.environ, "HARNESS_CONF": str(conf), "PYTHONDONTWRITEBYTECODE": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    proc = subprocess.run([sys.executable, str(JUDGE), *args], capture_output=True,
                          text=True, errors="replace", env=env, cwd=cwd or tmp, timeout=60)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


# ── Ж: режимы разницы ─────────────────────────────────────────────────────────
FULL_KEY = ('DAEMON_OWNED_PATHS="память/.tasks-triaged память/.orders-processed '
            'память/.details-triaged память/.указания-разобрано docs/приёмка/"')


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=p", "-c", "user.email=p@p",
                           *args], check=True, capture_output=True, text=True, env=env).stdout


def repo_at_a(root: Path) -> tuple[Path, str]:
    """Репозиторий в состоянии A: отметки, указания, код, карантин, приёмка → (путь, sha A)."""
    repo = root / "repo"
    files = {"память/.tasks-triaged": "отметка 0\n", "память/.orders-processed": "разобрано 0\n",
             "память/.указания-разобрано": "указание 0\n", "память/ORDERS.md": "указание владельца\n",
             "scripts/x.py": "print(1)\n", "harness/config/gates-quarantine.yaml": "карантин: []\n",
             "docs/приёмка/очередь.md": "очередь\n"}
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    git(repo, "init", "-q", "-b", "master")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "A")
    return repo, git(repo, "rev-parse", "HEAD").strip()


def append(repo: Path, name: str, text: str = "новая строка\n") -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def drop_first_line(repo: Path, name: str) -> None:
    path = repo / name
    path.write_text("".join(path.read_text(encoding="utf-8").splitlines(True)[1:]), encoding="utf-8")


def replace_text(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")


def commit(repo: Path, msg: str = "шаг") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)


def make_binary(repo: Path) -> None:
    with (repo / "память/.tasks-triaged").open("ab") as fh:
        fh.write(b"\x00\x01\x02 dvoichnoe\n")


def steps(*actions):
    """Шаги после A. Разница коммитов коммитит сама; режим дерева оставляет правку на диске."""
    def run(repo: Path) -> None:
        for action in actions:
            action(repo)
    return run


TRIAGED = "память/.tasks-triaged"
ADD_TRIAGED = lambda r: append(r, TRIAGED)          # noqa: E731
CODE_EDIT = lambda r: append(r, "scripts/x.py", "print(2)\n")   # noqa: E731

# (ключ, шаги, режим «коммиты»/«дерево», доводы сверх sha, ждём код, ловит)
DIFF_CASES = (
    (FULL_KEY, steps(ADD_TRIAGED, commit), "коммиты", (), 0,
     "добавленная строка отметки не признана служебной — Ж не срабатывает никогда"),
    (FULL_KEY, steps(lambda r: append(r, "память/.указания-разобрано"),
                     lambda r: append(r, "память/.orders-processed"), commit), "коммиты", (), 0,
     "кириллический путь разобран без -z (кавычки core.quotepath) — не узнан"),
    (FULL_KEY, steps(lambda r: append(r, "память/.details-triaged"), commit), "коммиты", (), 0,
     "новый файл класса (+N/−0) признан не служебным"),
    (FULL_KEY, steps(ADD_TRIAGED, commit, ADD_TRIAGED, commit), "коммиты", (), 0,
     "служебных коммитов два — судится не вся разница от A"),
    (FULL_KEY, steps(CODE_EDIT, commit), "коммиты", (), 1,
     "правка кода признана служебной"),
    (FULL_KEY, steps(lambda r: append(r, "harness/config/gates-quarantine.yaml", "# x\n"), commit),
     "коммиты", (), 1, "правка данных проверок (карантин) признана служебной"),
    (FULL_KEY, steps(lambda r: append(r, "память/ORDERS.md"), commit), "коммиты", (), 1,
     "правка владельца под память/ признана служебной (префикс вместо класса)"),
    (FULL_KEY, steps(lambda r: drop_first_line(r, TRIAGED), commit), "коммиты", (), 1,
     "удалённая строка отметки (долг ожил) признана служебной"),
    (FULL_KEY, steps(lambda r: replace_text(r, TRIAGED, "иная\n"), commit), "коммиты", (), 1,
     "замена строки (+1/−1) судится по добавлениям, удаления не считаются"),
    (FULL_KEY, steps(CODE_EDIT, commit, ADD_TRIAGED, commit), "коммиты", (), 1,
     "код, затем служебный: судится только последний коммит (ревью ЕЖ п. 4)"),
    (FULL_KEY, steps(lambda r: git(r, "mv", TRIAGED, "память/.details-triaged"), commit),
     "коммиты", (), 1, "git mv внутри класса: без --no-renames путь «a => b» 0/0 пропущен (ревью ЕЖ п. 5)"),
    (FULL_KEY, steps(lambda r: git(r, "mv", TRIAGED, "память/архив-отметок"), commit),
     "коммиты", (), 1, "git mv из класса признан служебным"),
    (FULL_KEY, steps(lambda r: git(r, "rm", "-q", TRIAGED), commit), "коммиты", (), 1,
     "удалён файл класса — признан служебным"),
    (FULL_KEY, steps(make_binary, commit), "коммиты", (), 1,
     "двоичный «-» в numstat прочитан как 0 удалений"),
    (FULL_KEY, steps(lambda r: append(r, "docs/приёмка/очередь.md"), commit),
     "коммиты", ("--под", "память/"), 1, "фильтр --под не судится: docs/приёмка/ читают проверки"),
    (FULL_KEY, steps(lambda r: append(r, "docs/приёмка/очередь.md"), commit), "коммиты", (), 0,
     "каталог ключа («/» на конце) в режиме разницы не узнан"),
    ('DAEMON_OWNED_PATHS=""', steps(ADD_TRIAGED, commit), "коммиты", (), 1,
     "пустой ключ: пустой разбор сделал разницу служебной"),
    ("", steps(ADD_TRIAGED, commit), "коммиты", (), 1, "нет ключа — класс вшит в код"),
    # Против рабочего дерева: один довод <от>.
    (FULL_KEY, steps(ADD_TRIAGED), "дерево", (), 0,
     "незакоммиченная отметка снимает «чисто» — Ж не срабатывает (ревью ЕЖ п. 3)"),
    (FULL_KEY, steps(ADD_TRIAGED, commit, lambda r: append(r, "память/.orders-processed")),
     "дерево", (), 0, "служебный коммит плюс незакоммиченная отметка: судится не от <от>"),
    (FULL_KEY, steps(lambda r: append(r, "память/.details-triaged")), "дерево", (), 0,
     "неотслеженный новый файл класса признан грязью"),
    (FULL_KEY, steps(lambda r: drop_first_line(r, TRIAGED)), "дерево", (), 1,
     "удалённая на диске строка отметки признана служебной"),
    (FULL_KEY, steps(lambda r: append(r, "scripts/новый.py", "print(3)\n")), "дерево", (), 1,
     "неотслеженный файл вне класса не судится (git diff его не видит)"),
    (FULL_KEY, steps(CODE_EDIT, lambda r: git(r, "add", "scripts/x.py")), "дерево", (), 1,
     "правка в индексе не судится"),
    (FULL_KEY, steps(CODE_EDIT, commit, ADD_TRIAGED), "дерево", (), 1,
     "коммит кода после <от> не судится в режиме дерева"),
    (FULL_KEY, steps(lambda r: append(r, "память/ORDERS.md")), "дерево", (), 1,
     "незакоммиченная правка владельца признана служебной"),
    (FULL_KEY, steps(lambda r: append(r, "docs/приёмка/очередь.md")),
     "дерево", ("--под", "память/"), 1, "фильтр --под не судится в режиме рабочего дерева"),
    ('DAEMON_OWNED_PATHS=""', steps(ADD_TRIAGED), "дерево", (), 1,
     "пустой ключ в режиме рабочего дерева сделал грязь служебной"),
)


def diff_cases(root: Path) -> int:
    failed = 0
    for n, (key_line, action, mode, extra, want, breakage) in enumerate(DIFF_CASES, 1):
        case_dir = root / f"ж{n}"
        case_dir.mkdir()
        repo, sha_a = repo_at_a(case_dir)
        action(repo)
        args = (sha_a, "HEAD") if mode == "коммиты" else (sha_a,)
        code, out = judge(case_dir, key_line, *args, *extra, cwd=repo)
        ok = code == want
        failed += not ok
        print(f"  {'ок   ' if ok else 'ПЛОХО'} Ж{n} [{mode}{' ' + ' '.join(extra) if extra else ''}] "
              f"→ код {code}, ждали {want}" + ("" if ok else f" (ловит: {breakage}; {out[-200:]})"))
    # Битый довод: неизвестный <от> — отказ git, а не «пустая разница, только класс».
    case_dir = root / "ж-битый"
    case_dir.mkdir()
    repo, _ = repo_at_a(case_dir)
    code, out = judge(case_dir, FULL_KEY, "0" * 40, "HEAD", cwd=repo)
    ok = code != 0
    failed += not ok
    print(f"  {'ок   ' if ok else 'ПЛОХО'} Ж-битый: неизвестный <от> → код {code}, ждали ≠ 0"
          + ("" if ok else f" (ловит: отказ git прочитан как пустая разница; {out[-200:]})"))
    return failed


def main() -> int:
    if not JUDGE.exists():
        print(f"нет {JUDGE}: случаи идут, механизма нет — красные")
    failed = 0
    total = len(CASES) + len(DIFF_CASES) + 1
    with tempfile.TemporaryDirectory(prefix="proba-service-only-") as raw:
        print("── --путь ──")
        for key_line, path, want, breakage in CASES:
            code, out = judge(Path(raw), key_line, "--путь", path)
            ok = code == want
            failed += not ok
            print(f"  {'ок   ' if ok else 'ПЛОХО'} {path} при «{key_line or 'нет ключа'}» → "
                  f"код {code}, ждали {want}" + ("" if ok else f" (ловит: {breakage}; {out[-200:]})"))
        print("── Ж: разница коммитов и рабочего дерева ──")
        failed += diff_cases(Path(raw))
    print(f"SELFTEST: {'зелёный' if not failed else 'КРАСНЫЙ'} ({total} случаев, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
