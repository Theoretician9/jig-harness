#!/usr/bin/env python3
"""Пробы очереди слияния scripts/merge-queue.py — до кода (Т1).

Спека .tasks/hr.per-helper-code-copies/спека.md §4 (контракт, шаги 1–7, коды 0–9), §7 Т1 а…о.
Что проба ждёт от кода (сверх спеки):
  * корни: проект — PROJECT_DIR из install.conf (HARNESS_INSTALL_CONF), пакет —
    config.katalog_paketa() (HARNESS_STARTER); cwd пробы — не проект;
  * ключи MERGE_QUEUE_RETRIES, MERGE_QUEUE_WAIT_MIN — из HARNESS_CONF;
  * замок, журнал и запомненные пути — под LOG_DIR / HARNESS_LOG_DIR;
  * `--json` — ровно один объект на stdout {код, итог, ветка, sha_до, sha_после, файлов,
    пакет_файлов, ожидание_с, ворота_с, повторов, конфликт, оставлено};
  * без --json — строка `merge-queue: <итог> <ветка> <sha-до>..<sha-после>`, имена файлов
    конфликта — в stdout;
  * ворота — `bash scripts/pre-commit-gate.sh` во временном дереве `.claude/worktrees/mq-*`
    проекта, с окружением вызова (заглушка стенда читает LOG_DIR и MQ_STUB_*);
  * `--убрать`: `mq-*` старше часа — по mtime каталога дерева;
  * журнал merge-queue.jsonl: src scripts/merge-queue.py, event mq.<итог>, outcome ok при
    коде 0, иначе error; `mq.откат` — data называет путь.
Стенд на каждый случай: git-проект (master, .gitignore с .claude/worktrees/, .gitattributes с
union для журнала правок) и git-пакет; дерево помощника — `git worktree add -b
worktree-agent-<x> <проект>/.claude/worktrees/agent-<x>`. Заглушка ворот пишет, какое дерево
и какие файлы видела, краснеет по файлу GATE_RED в дереве, по MQ_STUB_SLEEP спит, по
MQ_STUB_MOVE делает пустой коммит в master основного (master ушёл).
Механизм — рядом или HARNESS_MERGE_QUEUE (дверь для мутаций копии).

Прогон: python3 scripts/test_merge_queue.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = Path(os.environ.get("HARNESS_MERGE_QUEUE") or HERE / "merge-queue.py")
HEAL_HOOK = HERE / "post-commit-heal-index.sh"
JOURNAL = "docs/ИЗМЕНЕНИЯ-ПАКЕТА.md"
RETRIES = 3
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
STUB_GATE = """#!/usr/bin/env bash
# Заглушка ворот стенда: что видела — в журнал; краснеет по GATE_RED.
{
  echo "== $PWD"
  for f in a.txt b.txt; do [ -f "$f" ] && echo "$f: $(cat "$f")"; done
} >> "$LOG_DIR/stub-gate.log"
if [ -n "${MQ_STUB_SLEEP:-}" ]; then sleep "$MQ_STUB_SLEEP"; fi
if [ -n "${MQ_STUB_MOVE:-}" ]; then
  env -u GIT_DIR -u GIT_INDEX_FILE -u GIT_WORK_TREE \\
    git -C "$MQ_STUB_MOVE" commit -q --allow-empty -m "master ушёл"
fi
if [ -f GATE_RED ]; then echo "ворота: красный"; exit 1; fi
exit 0
"""

checked = failed = 0


def check(name: str, expected, actual) -> None:
    global checked, failed
    checked += 1
    if expected == actual:
        print(f"  ок    {name}")
    else:
        failed += 1
        print(f"  ПЛОХО {name}: ждали «{expected}», вышло «{actual}»")


def git(where: Path, *args: str, ok: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV})
    if ok and proc.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


class Stand:
    def __init__(self, base: Path):
        self.base = base
        self.proj, self.pkg, self.logs = base / "proj", base / "pkg", base / "logs"
        self.logs.mkdir(parents=True)
        files = {"scripts/pre-commit-gate.sh": STUB_GATE, "scripts/tool.py": "исходный\n",
                 "scripts/tool2.py": "второй\n", ".gitignore": ".claude/worktrees/\n",
                 ".gitattributes": f"{JOURNAL} merge=union\n", JOURNAL: "| дата | правка |\n"}
        for name in "abcefgho":
            files[f"{name}.txt"] = f"{name}0\n"
        for rel, text in files.items():
            self.write(self.proj, rel, text)
        for rel in ("scripts/tool.py", "scripts/tool2.py"):
            self.write(self.pkg, "harness/" + rel, files[rel])
        for root in (self.proj, self.pkg):
            git(root, "init", "-q", "-b", "master")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "начальный")
        self.conf = base / "harness.conf"
        self.conf.write_text(f'HELPER_WORKTREE="1"\nMERGE_QUEUE_RETRIES="{RETRIES}"\n'
                             f'MERGE_QUEUE_WAIT_MIN="2"\nSTARTER_DIR="{self.pkg}"\n',
                             encoding="utf-8")
        self.install = base / "install.conf"
        self.install.write_text(f'PROJECT_DIR="{self.proj}"\nAGENT_USER="нет-такого"\n',
                                encoding="utf-8")

    @staticmethod
    def write(root: Path, rel: str, text: str) -> None:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")

    def read(self, rel: str, root: Path | None = None) -> str:
        return ((root or self.proj) / rel).read_text(encoding="utf-8")

    def env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "PROJECT_DIR",
                            "STARTER_DIR", "HARNESS_TASK")}
        env.update(GIT_ENV, LOG_DIR=str(self.logs), HARNESS_LOG_DIR=str(self.logs),
                   HARNESS_CONF=str(self.conf), HARNESS_INSTALL_CONF=str(self.install),
                   HARNESS_STARTER=str(self.pkg), HARNESS_EVENTS_STATE=str(self.base / "state"),
                   PYTHONDONTWRITEBYTECODE="1", **extra)
        return env

    def tree(self, name: str) -> tuple[str, Path]:
        branch, where = f"worktree-agent-{name}", self.proj / ".claude/worktrees" / f"agent-{name}"
        git(self.proj, "worktree", "add", "-q", "-b", branch, str(where), "master")
        return branch, where

    def change(self, root: Path, rel: str, text: str, message: str = "правка") -> str:
        self.write(root, rel, text)
        git(root, "add", "--", rel)
        git(root, "commit", "-qm", message, "--", rel)
        return git(root, "rev-parse", "HEAD").strip()

    def sha(self, ref: str = "master", root: Path | None = None) -> str:
        return git(root or self.proj, "rev-parse", ref, ok=False).strip()

    def argv(self, *args: str) -> list:
        return [sys.executable, str(QUEUE), *args]

    def queue(self, *args: str, **extra: str) -> tuple[int, dict, str]:
        """(код, объект --json или {}, весь вывод). cwd — не проект."""
        proc = subprocess.run(self.argv(*args), capture_output=True, text=True,
                              env=self.env(**extra), cwd=self.base, timeout=600)
        try:
            data = json.loads(proc.stdout) if "--json" in args else {}
        except ValueError:
            data = {}
        return proc.returncode, data, proc.stdout + proc.stderr

    def gate_blocks(self) -> list:
        """Блоки заглушки ворот: [{дерево, a.txt, b.txt}]."""
        path = self.logs / "stub-gate.log"
        blocks: list = []
        for line in (path.read_text(encoding="utf-8").splitlines() if path.exists() else []):
            if line.startswith("== "):
                blocks.append({"дерево": line[3:]})
            elif blocks and ": " in line:
                key, value = line.split(": ", 1)
                blocks[-1][key] = value
        return blocks

    def rows(self) -> list:
        path = self.logs / "merge-queue.jsonl"
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def mq_trees(self) -> list:
        return [line for line in git(self.proj, "worktree", "list", "--porcelain").splitlines()
                if line.startswith("worktree ") and "/mq-" in line]

    def branch_exists(self, branch: str) -> bool:
        return bool(git(self.proj, "branch", "--list", branch).strip())


def case_a_fast_forward(tmp: Path) -> None:
    # Поломка: merge --no-ff / слияние коммитом — sha master ≠ вершины.
    print("а) ff без rebase")
    s = Stand(tmp / "а")
    branch, wt = s.tree("a")
    tip = s.change(wt, "a.txt", "a1\n")
    before = s.sha()
    code, data, out = s.queue(branch, "--json")
    check("а: код 0", 0, code)
    check("а: master == вершина ветки (ff, без слияния коммитом)", tip, s.sha())
    check("а: рабочий файл основного обновлён", "a1\n", s.read("a.txt"))
    check("а: --json код/ветка/sha_до/sha_после", (0, branch, before, tip),
          (data.get("код"), data.get("ветка"), data.get("sha_до"), data.get("sha_после")))
    check("а: в --json все поля контракта", True,
          {"код", "итог", "ветка", "sha_до", "sha_после", "файлов", "пакет_файлов",
           "ожидание_с", "ворота_с", "повторов", "конфликт", "оставлено"} <= set(data))
    blocks = s.gate_blocks()
    check("а: ворота шли во временном дереве .claude/worktrees/mq-*", True,
          bool(blocks) and "/.claude/worktrees/mq-" in blocks[-1]["дерево"])
    check("а: временное дерево снято", [], s.mq_trees())
    check("а: дерево помощника и ветка убраны (замка нет)", (False, False),
          (wt.exists(), s.branch_exists(branch)))
    rows = s.rows()
    check("а: строка журнала mq.* outcome ok от scripts/merge-queue.py", True,
          bool(rows) and rows[-1].get("src") == "scripts/merge-queue.py"
          and str(rows[-1].get("event", "")).startswith("mq.") and rows[-1].get("outcome") == "ok")
    b2, wt2 = s.tree("a2")
    s.change(wt2, "b.txt", "b1\n")
    code, _, out = s.queue(b2)
    check("а: без --json — строка «merge-queue: … <ветка> <sha>..<sha>»", (0, True),
          (code, any(l.startswith("merge-queue: ") and b2 in l and ".." in l
                     for l in out.splitlines())))


def case_b_master_moved(tmp: Path) -> None:
    # Поломка: ворота до rebase — заглушка увидела бы a.txt старым.
    print("б) master ушёл без конфликта")
    s = Stand(tmp / "б")
    branch, wt = s.tree("b")
    s.change(wt, "b.txt", "b1\n")
    moved = s.change(s.proj, "a.txt", "a1\n", "master ушёл")
    code, _, _ = s.queue(branch, "--json")
    check("б: код 0", 0, code)
    last = (s.gate_blocks() or [{}])[-1]
    check("б: ворота видели ОБЕ правки", ("a1", "b1"), (last.get("a.txt"), last.get("b.txt")))
    check("б: в master обе правки", ("a1\n", "b1\n"), (s.read("a.txt"), s.read("b.txt")))
    check("б: коммит master — предок нового master (ff после rebase)", 0,
          subprocess.run(["git", "-C", str(s.proj), "merge-base", "--is-ancestor", moved,
                          "master"]).returncode)


def case_c_conflict(tmp: Path) -> None:
    # Поломка: забытый rebase --abort — остаётся rebase-merge; ветка сдвинута.
    print("в) конфликт rebase")
    s = Stand(tmp / "в")
    branch, wt = s.tree("c")
    tip = s.change(wt, "c.txt", "помощник\n")
    s.change(s.proj, "c.txt", "master\n")
    master = s.sha()
    code, _, out = s.queue(branch)
    check("в: код 1", 1, code)
    check("в: имя файла конфликта в выводе", True, "c.txt" in out)
    check("в: master и ветка на прежних sha", (master, tip), (s.sha(), s.sha(branch)))
    rebase_dir = git(wt, "rev-parse", "--git-path", "rebase-merge").strip()
    check("в: rebase снят (нет rebase-merge)", False, (wt / rebase_dir).exists()
          or Path(rebase_dir).exists())
    check("в: дерево помощника на месте", True, wt.exists())


def case_d_red_gate(tmp: Path) -> None:
    # Поломка: дерево ворот не снимается при красном; слияние при красных воротах.
    print("г) красные ворота")
    s = Stand(tmp / "г")
    branch, wt = s.tree("d")
    s.change(wt, "GATE_RED", "1\n")
    master = s.sha()
    code, _, _ = s.queue(branch)
    check("г: код 3", 3, code)
    check("г: master на месте", master, s.sha())
    check("г: временного дерева mq-* нет", ([], []),
          (s.mq_trees(), sorted(p.name for p in (s.proj / ".claude/worktrees").glob("mq-*"))))
    # Поломка: сбой read-tree не замечен — ворота судили бы master, а не ветку.
    shim = s.base / "shim"
    shim.mkdir()
    real_git = shutil.which("git")
    (shim / "git").write_text(
        f'#!/bin/bash\n[ "$3" = read-tree ] && {{ echo "read-tree сломан" >&2; exit 1; }}\n'
        f'exec {real_git} "$@"\n', encoding="utf-8")
    (shim / "git").chmod(0o755)
    branch2, wt2 = s.tree("d2")
    s.change(wt2, "c.txt", "c-d2\n")
    code, _, out = s.queue(branch2, PATH=f"{shim}:{os.environ['PATH']}")
    check("г: сбой read-tree — код 3 с понятным текстом", (3, True),
          (code, "read-tree" in out and "ворота не запущены" in out))
    check("г: после сбоя read-tree временного дерева нет", [], s.mq_trees())


def case_e_dirty_helper(tmp: Path) -> None:
    # Поломка: слить закоммиченное, не глядя на незакоммиченное помощника.
    print("д) грязное дерево помощника")
    s = Stand(tmp / "д")
    branch, wt = s.tree("e")
    s.change(wt, "e.txt", "e1\n")
    s.write(wt, "новый-неотслеживаемый.txt", "x\n")
    master = s.sha()
    code, _, _ = s.queue(branch)
    check("д: код 5", 5, code)
    check("д: master на месте", master, s.sha())


def case_f_dirty_main(tmp: Path) -> None:
    # Поломка: reset/checkout -f основного — чужие правки пропали бы.
    print("е) грязное основное дерево")
    s = Stand(tmp / "е")
    branch, wt = s.tree("f")
    s.change(wt, "e.txt", "e-помощник\n")
    s.write(s.proj, "e.txt", "e-чужая-правка\n")
    diff_before = git(s.proj, "diff")
    master = s.sha()
    code, _, _ = s.queue(branch)
    check("е: тот же путь грязен в основном — код 4", 4, code)
    check("е: байты и git diff основного до = после", ("e-чужая-правка\n", diff_before),
          (s.read("e.txt"), git(s.proj, "diff")))
    check("е: master на месте", master, s.sha())

    s = Stand(tmp / "е2")
    branch, wt = s.tree("f2")
    s.change(wt, "h.txt", "h1\n")
    s.write(s.proj, "f.txt", "f-чужая\n")
    s.write(s.proj, "g.txt", "g-чужая-staged\n")
    git(s.proj, "add", "g.txt")
    code, _, _ = s.queue(branch)
    check("е: другой путь грязен + чужой staged — код 0", 0, code)
    check("е: чужие правки целы", ("f-чужая\n", "g-чужая-staged\n", "h1\n"),
          (s.read("f.txt"), s.read("g.txt"), s.read("h.txt")))
    check("е: чужой staged остался staged", "g.txt",
          git(s.proj, "diff", "--cached", "--name-only").strip())


def case_g_locked(tmp: Path) -> None:
    # Поломка: worktree remove --force / branch -D — снял бы дерево Claude Code.
    print("ж) дерево под замком CC")
    s = Stand(tmp / "ж")
    branch, wt = s.tree("g")
    s.change(wt, "g.txt", "g1\n")
    git(s.proj, "worktree", "lock", "--reason", "claude code", str(wt))
    code, data, _ = s.queue(branch, "--json")
    check("ж: код 0", 0, code)
    check("ж: дерево и ветка на месте", (True, True), (wt.exists(), s.branch_exists(branch)))
    check("ж: --json оставлено называет дерево", True, str(wt.name) in json.dumps(
        data.get("оставлено"), ensure_ascii=False))


def case_h_package(tmp: Path) -> None:
    # Поломка: пакет не догнан / сверка после ff (7 пришло бы при сдвинутом master).
    print("з) пакет")
    s = Stand(tmp / "з")
    branch, wt = s.tree("h")
    s.change(wt, "scripts/tool.py", "новый\n")
    code, data, _ = s.queue(branch, "--json")
    check("з: код 0", 0, code)
    check("з: пакет обновлён", "новый\n", s.read("harness/scripts/tool.py", s.pkg))
    check("з: пакет закоммичен очередью", (True, ""),
          ("Очередь слияния" in git(s.pkg, "log", "-1", "--format=%s"),
           git(s.pkg, "status", "--porcelain").strip()))
    check("з: пакет_файлов 1", 1, data.get("пакет_файлов"))

    s = Stand(tmp / "з2")
    branch, wt = s.tree("h2")
    s.change(wt, "scripts/tool2.py", "вершина\n")
    s.write(s.pkg, "harness/scripts/tool2.py", "третье\n")
    master, pkg_head = s.sha(), s.sha("HEAD", s.pkg)
    code, _, out = s.queue(branch)
    check("з: пакет с третьим содержимым — код 7", 7, code)
    check("з: master, пакет и его HEAD не тронуты", (master, "третье\n", pkg_head),
          (s.sha(), s.read("harness/scripts/tool2.py", s.pkg), s.sha("HEAD", s.pkg)))
    check("з: отказ называет файл", True, "tool2.py" in out)

    # Поломка: в пакет пишется сырая вершина — числа замера сервера уезжают копиям.
    s = Stand(tmp / "з3")
    library = "harness/config/process-library.json"

    def library_text(measure) -> str:
        entries = [{"имя": "x", "доля_нагрузки": {"замер": measure, "приписка": "п"}}]
        return json.dumps({"процессы": entries}, indent=2, ensure_ascii=False) + "\n"

    s.change(s.proj, library, library_text(1))
    s.change(s.pkg, library, library_text("не измерено"))
    branch, wt = s.tree("h3")
    s.change(wt, library, library_text(7).replace('"п"', '"п2"'))
    code, _, _ = s.queue(branch)
    check("з: process-library.json в пакет — вычищенным (без чисел замера)",
          (0, library_text("не измерено").replace('"п"', '"п2"')),
          (code, s.read(library, s.pkg)))


def case_i_two_queues(tmp: Path) -> None:
    # Поломка: нет flock — вторая очередь мешает первой (код ≠ 0 или правка потеряна).
    print("и) две очереди разом")
    s = Stand(tmp / "и")
    b1, w1 = s.tree("i1")
    b2, w2 = s.tree("i2")
    s.change(w1, "a.txt", "a-i1\n")
    s.change(w2, "b.txt", "b-i2\n")
    env = s.env(MQ_STUB_SLEEP="3")
    procs = [subprocess.Popen(s.argv(b, "--json"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=env, cwd=s.base) for b in (b1, b2)]
    results = []
    for proc in procs:
        out, _ = proc.communicate(timeout=600)
        try:
            results.append((proc.returncode, json.loads(out)))
        except ValueError:
            results.append((proc.returncode, {}))
    check("и: обе очереди — код 0", [0, 0], [r[0] for r in results])
    check("и: обе правки в master", ("a-i1\n", "b-i2\n"), (s.read("a.txt"), s.read("b.txt")))
    waits = [float(r[1].get("ожидание_с") or 0) for r in results]
    check("и: одна из очередей ждала замок (> 0 с)", True, max(waits) > 0)


def case_j_union(tmp: Path) -> None:
    # Поломка: rebase без union — второй вызов дал бы 1.
    print("к) журнал правок пакета — union")
    s = Stand(tmp / "к")
    b1, w1 = s.tree("k1")
    b2, w2 = s.tree("k2")
    for wt, line in ((w1, "| 13.11 | первая |\n"), (w2, "| 13.11 | вторая |\n")):
        s.change(wt, JOURNAL, s.read(JOURNAL, wt) + line)
    codes = [s.queue(b)[0] for b in (b1, b2)]
    check("к: обе ветки — код 0", [0, 0], codes)
    text = s.read(JOURNAL)
    check("к: обе строки в журнале", (True, True), ("первая" in text, "вторая" in text))


def case_k_heal_hook(tmp: Path) -> None:
    # Поломка: post-commit в дереве помощника сбросил бы индекс ОСНОВНОГО.
    print("л) коммит `-- <пути>` в дереве под post-commit-heal-index.sh")
    s = Stand(tmp / "л")
    hook = s.proj / ".git/hooks/post-commit"
    shutil.copy(HEAL_HOOK, hook)
    hook.chmod(0o755)
    s.write(s.proj, "g.txt", "g-staged-основного\n")
    git(s.proj, "add", "g.txt")
    index_before = git(s.proj, "ls-files", "-s")
    staged_before = git(s.proj, "diff", "--cached")
    branch, wt = s.tree("l")
    s.write(wt, "o.txt", "o-помощник\n")
    git(wt, "add", "o.txt")
    git(wt, "commit", "-qm", "у себя", "--", "o.txt")
    check("л: индекс основного не тронут коммитом помощника", (index_before, staged_before),
          (git(s.proj, "ls-files", "-s"), git(s.proj, "diff", "--cached")))
    code, _, _ = s.queue(branch)
    check("л: очередь — код 0, staged основного цел", (0, "g.txt"),
          (code, git(s.proj, "diff", "--cached", "--name-only").strip()))


def case_l_retries(tmp: Path) -> None:
    # Поломка: бесконечный круг или нет повторов — код ≠ 6 / повторов ≠ RETRIES.
    print("м) master двигается каждый круг")
    s = Stand(tmp / "м")
    branch, wt = s.tree("m")
    s.change(wt, "a.txt", "a-m\n")
    code, data, _ = s.queue(branch, "--json", MQ_STUB_MOVE=str(s.proj))
    check("м: код 6", 6, code)
    check("м: повторов ровно MERGE_QUEUE_RETRIES", RETRIES, data.get("повторов"))
    check("м: прогонов ворот 1 + RETRIES", RETRIES + 1, len(s.gate_blocks()))
    check("м: правка помощника не в master", "a0\n", s.read("a.txt"))


def case_m_cleanup(tmp: Path) -> None:
    # Поломка: --убрать снимает неслитое; файл говорит push.
    print("н) --убрать")
    s = Stand(tmp / "н")
    b1, w1 = s.tree("n1")
    s.change(w1, "a.txt", "a-n1\n")
    git(s.proj, "merge", "-q", "--ff-only", b1)
    b2, w2 = s.tree("n2")
    s.change(w2, "b.txt", "b-n2\n")
    s.write(w2, "неотслеживаемый.txt", "данные\n")
    b3, w3 = s.tree("n3")  # только создано: «слито» лишь потому, что коммита ещё нет
    old = s.proj / ".claude/worktrees/mq-обрыв"
    git(s.proj, "worktree", "add", "-q", "--detach", str(old), "master")
    hour_ago = time.time() - 7200
    os.utime(old, (hour_ago, hour_ago))
    code, _, out = s.queue("--убрать")
    check("н: код 0", 0, code)
    check("н: слитое дерево и ветка сняты", (False, False), (w1.exists(), s.branch_exists(b1)))
    check("н: неслитое с неотслеживаемым оставлено", (True, True, "данные\n"),
          (w2.exists(), s.branch_exists(b2), s.read("неотслеживаемый.txt", w2)))
    check("н: оборванное mq-* старше часа снято", False, old.exists())
    # Поломка: stat() падает на каталоге, которого уже нет, — уборка обрывается.
    gone = s.proj / ".claude/worktrees/mq-исчез"
    git(s.proj, "worktree", "add", "-q", "--detach", str(gone), "master")
    shutil.rmtree(gone)
    code2, _, _ = s.queue("--убрать")
    check("н: исчезнувшее mq-* не валит уборку", 0, code2)
    check("н: свежее дерево без коммитов (помощник работает) оставлено", (True, True),
          (w3.exists(), s.branch_exists(b3)))
    check("н: печать «неслитых веток 1»", True, "неслитых веток 1" in out)
    # Поломка: уборка пишет mq.слито — commit-friction считал бы её слиянием.
    check("н: строка журнала уборки — mq.убрано", "mq.убрано",
          (s.rows() or [{}])[-1].get("event"))
    check("н: в очереди нет слова push", False,
          QUEUE.exists() and "push" in QUEUE.read_text(encoding="utf-8"))


def case_n_rollback(tmp: Path) -> None:
    # Поломка: пути ветки не запоминаются / не сверяются — события нет.
    print("о) чужой коммит вернул влитой путь")
    s = Stand(tmp / "о")
    b1, w1 = s.tree("o1")
    s.change(w1, "o.txt", "o-влито\n")
    s.queue(b1)
    s.write(s.proj, "o.txt", "o0\n")
    git(s.proj, "commit", "-qm", "чужой коммит старым индексом", "--", "o.txt", ok=False)
    b2, w2 = s.tree("o2")
    s.change(w2, "b.txt", "b-o2\n")
    s.queue(b2)
    back = [r for r in s.rows() if r.get("event") == "mq.откат"]
    check("о: событие mq.откат называет o.txt", True,
          bool(back) and "o.txt" in json.dumps(back[-1].get("data"), ensure_ascii=False))


def case_bad_call(tmp: Path) -> None:
    # Поломка: слить при HEAD основного не на master / при неизвестной ветке.
    print("неверный вызов")
    s = Stand(tmp / "вызов")
    check("нет такой ветки — код 2", 2, s.queue("worktree-agent-нет")[0])
    branch, wt = s.tree("x")
    s.change(wt, "a.txt", "a-x\n")
    git(s.proj, "checkout", "-q", "-b", "чужая")
    check("основное не на master — код 2", 2, s.queue(branch)[0])


CASES = (case_a_fast_forward, case_b_master_moved, case_c_conflict, case_d_red_gate,
         case_e_dirty_helper, case_f_dirty_main, case_g_locked, case_h_package,
         case_i_two_queues, case_j_union, case_k_heal_hook, case_l_retries, case_m_cleanup,
         case_n_rollback, case_bad_call)


def main() -> int:
    global checked, failed
    if not QUEUE.exists():
        print(f"нет {QUEUE}: случаи идут, механизма нет — красные")
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
