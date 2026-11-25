#!/usr/bin/env python3
"""Пробы очереди слияния scripts/merge-queue.py — до кода (Т1).

Спека задачи §4 (контракт, шаги 1–7, коды 0–9), §7 Т1 а…о.
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
Механизм — рядом или HARNESS_MERGE_QUEUE (дверь для мутаций копии). Копию класть в каталог,
где её соседи — символьные ссылки на scripts/* (`ln -s scripts/* <каталог>/`, кроме самого
merge-queue.py): механизм грузит их от своего HERE.

Ворота по изменённым файлам (спека поезда выпусков §7, фаза 4): после
pre-commit-gate.sh очередь берёт выбор `select-checks.py` (рядом с механизмом, как
compare-with-package.py; корень — временное дерево вершины) по файлам master..<вершина> и,
если выбор не пуст, гоняет в том же дереве `bash scripts/gates.sh --только "<имена через |>"`.
Стенд кладёт в проект заглушку gates.sh (объявления как у боевого — их разбирает настоящий
select-checks; исполняет только названные --только, пишет $LOG_DIR/stub-gates-sh.log, по
MQ_STUB_GATES_SLEEP спит), mechanisms.yaml и release-train.yaml (`выбор: {потолок_мин}`,
`всегда_всё`). Красный выбор — код 3; дольше потолка — код 3 с причиной «не дошли»; запись
merge-queue.jsonl и --json несут `выбор` (имена), `выбор_с`, `дерево` (дерево вершины).

Прогон: python3 scripts/test_merge_queue.py
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Дверь мутаций копии. Копии нужен каталог с соседями-ссылками: механизм берёт их от своего
# HERE — lib/ (LIB_DIR), select-checks.py (выбор ворот), compare-with-package.py (сверка с
# пакетом), install-new.py (перенос в пакет), git-commit-locked.sh (коммит карты). Голая копия
# вне scripts/ краснеет и без мутации (20 неудач из 29 на lq1), и мутант «убит» ложно.
QUEUE = Path(os.environ.get("HARNESS_MERGE_QUEUE") or HERE / "merge-queue.py")
HEAL_HOOK = HERE / "post-commit-heal-index.sh"
JOURNAL = "docs/ИЗМЕНЕНИЯ-ПАКЕТА.md"
RETRIES = 3
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
STUB_GATE = """#!/usr/bin/env bash
# Заглушка ворот стенда: что видела — в журнал; краснеет по GATE_RED.
# Сверка одной карты (--devmap-only) — не ворота: своя строка в stub-devmap.log.
if [ "${1:-}" = "--devmap-only" ]; then
  echo "$2" >> "$LOG_DIR/stub-devmap.log"
  [ -z "${MQ_STUB_DEVMAP_RED:-}" ] || { echo "[dev-map] ✗ стенд: карта красная"; exit 1; }
  exit 0
fi
{
  echo "== $PWD"
  for f in a.txt b.txt; do [ -f "$f" ] && echo "$f: $(cat "$f")"; done
} >> "$LOG_DIR/stub-gate.log"
if [ -n "${MQ_STUB_SLEEP:-}" ]; then sleep "$MQ_STUB_SLEEP"; fi
if [ -n "${MQ_STUB_MOVE:-}" ]; then
  env -u GIT_DIR -u GIT_INDEX_FILE -u GIT_WORK_TREE \\
    git -C "$MQ_STUB_MOVE" commit -q --allow-empty -m "master ушёл"
fi
# MQ_STUB_MOVE_ONCE=<проект>:<путь>: один раз за стенд master получает коммит этого файла.
# MQ_STUB_MOVE_TEXT — что дописать (без ключа — «# сдвиг master»).
if [ -n "${MQ_STUB_MOVE_ONCE:-}" ] && [ ! -e "$LOG_DIR/stub-moved-once" ]; then
  : > "$LOG_DIR/stub-moved-once"
  proj="${MQ_STUB_MOVE_ONCE%%:*}"; rel="${MQ_STUB_MOVE_ONCE#*:}"
  echo "${MQ_STUB_MOVE_TEXT:-# сдвиг master}" >> "$proj/$rel"
  env -u GIT_DIR -u GIT_INDEX_FILE -u GIT_WORK_TREE \\
    git -C "$proj" commit -q -m "master сдвинут: $rel" -- "$rel"
fi
# MQ_STUB_MOVE_FILE: чужой коммит с файлом в master основного — один раз (метка в LOG_DIR),
# иначе повтор очереди сдвигал бы master снова.
if [ -n "${MQ_STUB_MOVE_FILE:-}" ] && [ ! -e "$LOG_DIR/stub-moved" ]; then
  : > "$LOG_DIR/stub-moved"
  echo "print('чужая')" > "$MQ_STUB_MOVE_ROOT/$MQ_STUB_MOVE_FILE"
  env -u GIT_DIR -u GIT_INDEX_FILE -u GIT_WORK_TREE \\
    git -C "$MQ_STUB_MOVE_ROOT" commit -q -m "чужой коммит" -- "$MQ_STUB_MOVE_FILE"
fi
# Текст GATE_RED — в вывод ворот: П2 кладёт туда строки «[pre-commit] GATE FAIL: <имя>».
# MQ_STUB_RED_LINES: красные строки в форме боевых ворот (lib/gates_check.sh, pre-commit-gate.sh).
if [ -n "${MQ_STUB_RED_LINES:-}" ]; then
  echo "  ПЛОХО · проба-первая (код 1)"
  echo "  ПЛОХО · проба-первая (код 1) — красная и соло"
  echo "[pre-commit] GATE FAIL: проба-вторая" >&2
  echo "=== ворота превысили общий предел 5400 с — пул погашен, итог красный ==="
  exit 1
fi
if [ -f GATE_RED ]; then cat GATE_RED; echo "ворота: красный"; exit 1; fi
exit 0
"""

# Заглушка gates.sh: объявления в форме боевого (`проверка "имя" команда`) — их разбирает
# настоящий select-checks; исполняются только названные в --только.
STUB_GATES_SH = """#!/usr/bin/env bash
[ -n "${HEAVY_SLOT_HELD:-}" ] || exec bash "$(dirname "$0")/heavy-slot.sh" -- bash "$0" "$@"
only=""
[ "${1:-}" = "--только" ] && only="${2:-}"
echo "только=$only" >> "$LOG_DIR/stub-gates-sh.log"
# Чьё дерево судят (первая строка node_lib.py) и каким красным соло не нужен (М4).
echo "без_соло=${GATES_NO_RECHECK:-}|узел=$(head -1 scripts/node_lib.py)" >> "$LOG_DIR/stub-no-recheck.log"
# Сон — фоном в той же группе, pid в журнал: В3 судит, что потолок убил группу, а не один bash.
if [ -n "${MQ_STUB_GATES_SLEEP:-}" ]; then
  sleep "$MQ_STUB_GATES_SLEEP" & echo $! > "$LOG_DIR/stub-gates-sleep.pid"; wait $!
fi
red=0
проверка() {
    local name="$1"; shift
    case "|$only|" in *"|$name|"*) ;; *) return 0 ;; esac
    echo "проверка=$name" >> "$LOG_DIR/stub-gates-sh.log"
    # Строка красной — как у боевого gates_check.sh (_gates_print_block): очередь берёт имена из неё.
    # MQ_STUB_SOLO=<имя>: красная печатается вариантом «красная и соло» (перепрогон один).
    local solo=""
    [ "$name" = "${MQ_STUB_SOLO:-}" ] && solo=" — красная и соло"
    "$@" || { rc=$?; printf '  ПЛОХО · %s (код %d)%s\\n' "$name" "$rc" "$solo"; red=1; }
}
проверка "проба-узла" python3 scripts/probe_node.py
проверка "проба-вторая" python3 scripts/probe_other.py
exit $red
"""
# Проба узла зовёт node_lib импортом: правка node_lib.py задевает её ТОЛЬКО графом «кто
# зовёт» (не файл проверки, не самотест, не входы механизма, не упоминание в команде).
# Заглушка пула мест (И-2 ревью фазы 4): по MQ_STUB_SLOT_WAIT «все места заняты» столько
# секунд, затем, как боевой heavy-slot.sh, метка места потомкам; HEAVY_SLOT_ACQUIRED_FILE (если
# задан) — отметка «место получено», от неё очередь считает потолок. Повторный вход места не ждёт.
STUB_HEAVY_SLOT = """#!/usr/bin/env bash
max=""
[ "${1:-}" = "--не-дольше" ] && { max="$2"; shift 2; }
# --место-очереди: своё место ворот очереди у боевого пула; заглушке оно — как любое.
[ "${1:-}" = "--место-очереди" ] && shift
[ "${1:-}" = "--" ] && shift
# MQ_STUB_SLOT_FOREVER: места нет никогда — как боевой пул, ждёт --не-дольше, без него «вечно».
if [ -z "${HEAVY_SLOT_HELD:-}" ] && [ -n "${MQ_STUB_SLOT_FOREVER:-}" ]; then
  echo "место: нет, жду ${max:-без предела}" >> "$LOG_DIR/stub-gates-sh.log"
  sleep "${max:-600}"
fi
if [ -z "${HEAVY_SLOT_HELD:-}" ] && [ -n "${MQ_STUB_SLOT_WAIT:-}" ]; then
  echo "heavy-slot: все 4 мест заняты — жду своей очереди" >&2
  echo "место: жду $MQ_STUB_SLOT_WAIT" >> "$LOG_DIR/stub-gates-sh.log"
  sleep "$MQ_STUB_SLOT_WAIT"
fi
[ -n "${HEAVY_SLOT_ACQUIRED_FILE:-}" ] && : > "$HEAVY_SLOT_ACQUIRED_FILE"
export HEAVY_SLOT_HELD=1
exec "$@"
"""
GRAPH_FILES = {
    "scripts/gates.sh": STUB_GATES_SH,
    "scripts/heavy-slot.sh": STUB_HEAVY_SLOT,
    # Проба узла зовёт и общий узел стенда (К-1): правка узла выбирает её графом, не «всё».
    "scripts/probe_node.py": ("import subprocess\nimport sys\nsys.path.insert(0, 'scripts')\n"
                              "import node_lib\n\n"
                              "subprocess.run([\"bash\", \"scripts/shared_node.sh\"], check=True)\n"
                              "sys.exit(node_lib.verdict())\n"),
    "scripts/node_lib.py": "def verdict():\n    return 0\n",
    "scripts/probe_other.py": "print('вторая')\n",
    "scripts/shared_node.sh": "# общий узел стенда\n",
    "harness/config/mechanisms.yaml": "механизмы: []\n",
    "harness/config/release-train.yaml": ("выбор: { потолок_мин: 15 }\n"
                                          "всегда_всё:\n  - scripts/shared_node.sh\n"),
}

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
        files.update(GRAPH_FILES)
        for rel, text in files.items():
            self.write(self.proj, rel, text)
        # Файлы стенда выбора тоже лежат в пакете: без них правка ветки читалась бы как
        # «пакет удалил строки master» — код 7 раньше, чем судит выбор.
        for rel in ("scripts/tool.py", "scripts/tool2.py",
                    *(r for r in GRAPH_FILES if r.startswith("scripts/"))):
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
                            "STARTER_DIR", "HARNESS_TASK", "SERVICE_LOCKS_SUBDIR",
                            "HEAVY_SLOT_HELD", "HARNESS_IN_CHECKS_SLICE", "HEAVY_SLOT_ACQUIRED_FILE")}
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

    def gates_sh_log(self) -> list:
        """Строки заглушки gates.sh: «только=…» на вызов, «проверка=…» на исполненную."""
        path = self.logs / "stub-gates-sh.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def last_data(self) -> dict:
        """data последней строки merge-queue.jsonl — то, что ушло в журнал (после маскировки)."""
        rows = self.rows()
        return (rows[-1].get("data") or {}) if rows else {}

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
        f'#!/bin/bash\ncase " $* " in *" read-tree "*) echo "read-tree сломан" >&2; exit 1 ;; esac\n'
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
    code, data, _ = s.queue(branch, "--json")
    check("е: мешающий файл назван в JSON", (4, ["e.txt"]), (code, data.get("мешают")))

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
          ("Очередь слияния" in git(s.pkg, "log", "-1", "--format=%B"),
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


def case_p_package_superset(tmp: Path) -> None:
    # Поломка: пакет = master + блоки помощников, вершина ⊇ пакета — отказ 7 вместо записи
    # вершины; или наоборот: лишнее пакета молча затёрто вершиной.
    print("п) вершина включает пакет")

    def stand(name: str, rel: str, master: str, package: str, tip: str) -> tuple:
        s = Stand(tmp / name)
        s.change(s.proj, rel, master)
        s.change(s.pkg, "harness/" + rel, master)
        s.write(s.pkg, "harness/" + rel, package)  # правка помощника в пакете, не закоммичена
        branch, wt = s.tree(name)
        s.change(wt, rel, tip)
        before = s.sha()
        code, _, out = s.queue(branch)
        return code, out, s.read("harness/" + rel, s.pkg), before, s.sha()

    text = "scripts/reg.sh"
    master = "один\nдва\nтри\n"
    tip = "один\nблок-A\nдва\nблок-B\nтри\n"
    code, _, pkg, _, _ = stand("п-а", text, master, "один\nблок-A\nдва\nтри\n", tip)
    check("п(а): пакет = master + A, вершина = master + A + B — код 0, в пакете вершина",
          (0, tip), (code, pkg))
    code, out, pkg, before, after = stand("п-б", text, master, "один\nблок-A\nдва\nблок-C\nтри\n", tip)
    check("п(б): пакет = master + A + C — код 7, пакет и master не тронуты",
          (7, "один\nблок-A\nдва\nблок-C\nтри\n", before), (code, pkg, after))
    check("п(б): отказ называет строку C, не только путь", (True, False),
          ("+ блок-C" in out, "+ блок-A" in out))
    code, out, _, _, _ = stand("п-б2", text, master, "один\nтри\n", "один\nдва\nблок-B\nтри\n")
    check("п(б): удалённое в пакете, но живое в вершине — код 7, строка названа",
          (7, True), (code, "- два" in out))

    reg = "scripts/reg.yaml"
    master = "# реестр\nпробы:\n  x:\n    м: 1\nмеханизмы:\n- файл: f0\n  имя: ноль\n"
    package = ("# реестр\nпробы:\n  b:\n    м: 'бэ'\n  a:\n    м: 'а'\n  x:\n    м: 1\n"
               "механизмы:\n- имя: ку\n  файл: q\n- файл: f0\n  имя: ноль\n")
    tip = ("# реестр\nпробы:\n  a:\n    м: а\n  b:\n    м: бэ\n  x:\n    м: 1\n  d:\n    м: дэ\n"
           "механизмы:\n- файл: f0\n  имя: ноль\n- файл: q\n  имя: ку\n- файл: r\n  имя: эр\n")
    code, _, pkg, _, _ = stand("п-в", reg, master, package, tip)
    check("п(в): yaml, другой порядок и оформление вставок — код 0, в пакете вершина",
          (0, tip), (code, pkg))
    code, out, pkg, _, _ = stand("п-в2", reg, master, package.replace("имя: ку", "имя: другое")
                                 .replace("  x:\n", "  c:\n    м: це\n  x:\n"), tip)
    check("п(в): yaml, лишняя запись и иной механизм — код 7, названы c и файл q",
          (7, True, True, False), (code, "пробы → c" in out, "механизмы → q" in out,
                                   "пробы → a" in out))
    code, out, _, _, _ = stand("п-в3", reg, master, package.replace("# реестр\n",
                               "# реестр\n# поправка пакета\n"), tip)
    check("п(в): yaml, комментарий только в пакете — код 7, строка названа",
          (7, True), (code, "+ # поправка пакета" in out))
    code, out, _, _, _ = stand("п-в4", reg, master, master.replace("  x:\n    м: 1\n", "  e: 0\n"),
                               master.replace("  x:\n", "  d:\n    м: дэ\n  x:\n"))
    check("п(в): yaml, запись удалена в пакете, жива в вершине — код 7, запись названа",
          (7, True), (code, "пробы → x (удалена в пакете)" in out))


def case_i_two_queues(tmp: Path) -> None:
    # Поломка: нет flock — вторая очередь мешает первой (код ≠ 0 или правка потеряна).
    print("и) две очереди разом")
    s = Stand(tmp / "и")
    b1, w1 = s.tree("i1")
    b2, w2 = s.tree("i2")
    s.change(w1, "a.txt", "a-i1\n")
    s.change(w2, "b.txt", "b-i2\n")
    env = s.env(MQ_STUB_SLEEP="3")
    # Вторая стартует, когда первая уже в воротах (первый блок заглушки): иначе при очереди
    # пачкой обе могли бы уехать одной пачкой без ожидания — проба мигала бы.
    procs = [subprocess.Popen(s.argv(b1, "--json"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=env, cwd=s.base)]
    deadline = time.monotonic() + 60
    while not s.gate_blocks() and time.monotonic() < deadline and procs[0].poll() is None:
        time.sleep(0.1)
    procs.append(subprocess.Popen(s.argv(b2, "--json"), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env, cwd=s.base))
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
    check("и: вторая (пришла во время ворот первой) ждала (> 0 с)", True, waits[1] > 0)


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


SAMPLE = "/".join(("harness", "config", "harness.conf"))


def case_s_package_carried(tmp: Path) -> None:
    # Поломка: файл пакета, уже равный вершине (помощник положил его сам), не попадает в
    # коммит пакета — пакет копит незакоммиченное; коммитов пакета больше одного; в коммит
    # уезжает чужая правка пакета; сообщение — не ветки.
    print("s) пакет догоняется целиком одним коммитом с сообщением ветки")
    s = Stand(tmp / "s")
    branch, wt = s.tree("s")
    s.write(wt, "scripts/new.py", "новый файл\n")
    s.write(wt, "scripts/tool.py", "новый\n")
    git(wt, "add", "--", "scripts/new.py", "scripts/tool.py")
    git(wt, "commit", "-qm", "Первая правка: tool и new")
    s.change(wt, "scripts/tool2.py", "вершина\n", "Вершина ветки: tool2")
    s.write(s.pkg, "harness/scripts/tool.py", "новый\n")      # помощник положил сам
    s.write(s.pkg, "harness/scripts/new.py", "новый файл\n")  # и новый файл
    s.write(s.pkg, "harness/scripts/other.py", "чужое\n")     # чужая правка пакета
    head = s.sha("HEAD", s.pkg)
    code, data, _ = s.queue(branch, "--json")
    check("s: код 0", 0, code)
    check("s: в пакете ровно один новый коммит", "1",
          git(s.pkg, "rev-list", "--count", f"{head}..HEAD").strip())
    check("s: в коммит пакета ушли все три файла ветки",
          ["harness/scripts/new.py", "harness/scripts/tool.py", "harness/scripts/tool2.py"],
          sorted(git(s.pkg, "show", "--format=", "--name-only", "HEAD").split()))
    check("s: в пакете грязна только чужая правка", "?? harness/scripts/other.py",
          git(s.pkg, "status", "--porcelain").strip())
    check("s: заголовок коммита пакета — заголовок вершины ветки", "Вершина ветки: tool2",
          git(s.pkg, "log", "-1", "--format=%s").strip())
    body = git(s.pkg, "log", "-1", "--format=%b")
    check("s: тело называет коммиты ветки и очередь", (True, True),
          ("Первая правка: tool и new" in body, f"Очередь слияния: {branch}" in body))
    check("s: пакет_файлов 3", 3, data.get("пакет_файлов"))


def keys_stand(tmp: Path, name: str) -> tuple:
    """Стенд: образец в master и пакете; машинный конфиг знает OWNED со своим значением
    и не знает OLD (давний пропуск, не этого слияния); ветка вносит в образец NEW."""
    s = Stand(tmp / name)
    sample = '# старый ключ\nOLD="1"\nOWNED="пакет"\n'
    s.change(s.proj, SAMPLE, sample, "образец")
    s.change(s.pkg, SAMPLE, sample, "образец")   # в пакете образец лежит тем же путём
    machine = s.conf.read_text(encoding="utf-8") + '# слово владельца\nOWNED="владелец"\n'
    s.conf.write_text(machine, encoding="utf-8")
    branch, wt = s.tree(name)
    s.change(wt, SAMPLE, sample.replace('OWNED="пакет"', 'OWNED="новое"')
             + '# предел патча, байт\nNEW="5"\n')
    return s, branch, machine


def case_q_new_sample_key(tmp: Path) -> None:
    # Поломка: ключ, внесённый слиянием в образец, не доходит до машинного конфига
    # (инцидент 15.11: EVOLUTION_MAX_PATCH_BYTES) / дописано лишнее / переписано стоящее /
    # нет копии / повтор дописывает дубль.
    print("q) ключ, внесённый слиянием в образец, дописан в машинный конфиг")
    s, branch, machine = keys_stand(tmp, "q")
    code, data, _ = s.queue(branch, "--json")
    after = s.conf.read_text(encoding="utf-8")
    check("q: код 0", 0, code)
    check("q: прежний текст конфига цел — только дописано", True, after.startswith(machine))
    check("q: дописан ровно NEW с комментарием образца", '\n# предел патча, байт\nNEW="5"\n',
          after[len(machine.rstrip("\n")) + 1:])
    check("q: давний пропуск OLD не тронут", False, 'OLD=' in after)
    check("q: отчёт называет ключ", ["NEW"], data.get("ключи_машины"))
    copies = list((s.logs / "панель-копии").glob("harness.conf.*"))
    check("q: копия прежнего конфига снята", [machine],
          [c.read_text(encoding="utf-8") for c in copies])
    b2, w2 = s.tree("q2")
    s.change(w2, "a.txt", "a-q2\n")
    s.queue(b2, "--json")
    check("q: следующее слияние ничего не дописывает", after, s.conf.read_text(encoding="utf-8"))


def case_r_key_unwritable(tmp: Path) -> None:
    # Поломка: конфиг не записан, а очередь молчит «слито» — ключа на машине нет.
    print("r) машинный конфиг не пишется — отказ называет ключ")
    s, branch, machine = keys_stand(tmp, "r")
    s.conf.chmod(0o444)
    before = s.sha()
    code, data, out = s.queue(branch, "--json")
    check("r: код 10", 10, code)
    check("r: слито — master ушёл вперёд", True, s.sha() != before and s.sha() == data.get("sha_после"))
    check("r: отказ называет NEW и конфиг", True, "NEW" in out and str(s.conf) in out)
    check("r: конфиг не тронут", machine, s.conf.read_text(encoding="utf-8"))


def case_r2_key_root_dir(tmp: Path) -> None:
    # Поломка: каталог конфига чужой (root), файл свой — очередь судит каталог и
    # отдаёт код 10, хотя ключ дописать можно (23.11: 11 ключей не дошли до машины).
    print("r2) каталог конфига закрыт, файл свой — ключ дописан")
    s, branch, machine = keys_stand(tmp, "r2")
    closed = s.base / "etc-root"
    closed.mkdir()
    s.conf = s.conf.rename(closed / "harness.conf")
    closed.chmod(0o555)
    try:
        code, data, _ = s.queue(branch, "--json")
        after = s.conf.read_text(encoding="utf-8")
    finally:
        closed.chmod(0o755)
    check("r2: код 0", 0, code)
    check("r2: дописан NEW, прежнее цело", (True, True),
          (after.startswith(machine), after.endswith('# предел патча, байт\nNEW="5"\n')))
    check("r2: отчёт называет ключ", ["NEW"], data.get("ключи_машины"))
    check("r2: копия снята вне закрытого каталога", 1,
          len(list((s.logs / "панель-копии").glob("harness.conf.*"))))


MAP_TEXT = ("lines:\n- id: l1\n  tasks:\n  - id: t1\n    status: plan\n"
            "  - id: t2\n    status: plan\n  - id: t3\n    status: plan\n")


def map_stand(tmp: Path, name: str) -> tuple:
    """Карта в master, ветка помощника правит t1 — слияние трогает dev-map.yaml."""
    s = Stand(tmp / name)
    s.change(s.proj, "dev-map.yaml", MAP_TEXT, "карта")
    branch, wt = s.tree(name)
    s.change(wt, "dev-map.yaml", MAP_TEXT.replace("plan", "test", 1), "t1 в test")
    return s, branch


def hold_map_lock(s: Stand, seconds: float) -> subprocess.Popen:
    """Писатель карты держит ОБЩИЙ лок карты; возврат — когда лок взят."""
    lock = s.logs / "locks/services/dev-map.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c",
                               "import fcntl,sys,time\n"
                               "h=open(sys.argv[1],'a'); fcntl.flock(h, fcntl.LOCK_EX)\n"
                               "print('взят', flush=True); time.sleep(float(sys.argv[2]))",
                               str(lock), str(seconds)], stdout=subprocess.PIPE, text=True)
    holder.stdout.readline()
    return holder


def case_t_dirty_map(tmp: Path) -> None:
    # Поломка: очередь не коммитит карту писателей — код 4 «мешают-правки» (16.11 трижды);
    # коммит мимо общего лока карты — гонка с писателем.
    print("т) грязная карта основного — очередь коммитит её сама под локом карты")
    s, branch = map_stand(tmp, "t")
    s.write(s.proj, "dev-map.yaml", MAP_TEXT.replace("t3\n    status: plan", "t3\n    status: wip"))
    holder = hold_map_lock(s, 3)
    started = time.monotonic()
    code, _, _ = s.queue(branch, "--json")
    waited = time.monotonic() - started
    holder.wait()
    check("т: код 0", 0, code)
    subjects = git(s.proj, "log", "--format=%s", "master").splitlines()
    want = f"Карта: записи писателей карты — коммит очереди слияния перед {branch}"
    check("т: коммит карты в истории", True, want in subjects)
    text = s.read("dev-map.yaml")
    check("т: обе правки карты в master", (True, True),
          ("t1\n    status: test" in text, "t3\n    status: wip" in text))
    check("т: карта основного чиста", "", git(s.proj, "status", "--porcelain", "--", "dev-map.yaml"))
    check("т: коммит ждал лок писателя карты", True, waited >= 2.5)
    check("т: карта закоммичена до ворот — ворота один раз", 1, len(s.gate_blocks()))


def devmap_checks(s: Stand) -> list:
    path = s.logs / "stub-devmap.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def case_map_moved_master(tmp: Path) -> None:
    # Поломка: сдвиг master одним коммитом карты гонит ворота заново (25–30 мин на круг
    # под нагрузкой, 21.11 ветка потеряла 3 часа) — прогонов ворот 2, повторов_без_ворот 0.
    print("кс) во время ворот master сдвинут только картой — ворота не повторяются")
    s = Stand(tmp / "кс")
    s.change(s.proj, "dev-map.yaml", MAP_TEXT, "карта")
    branch, wt = s.tree("ks")
    s.change(wt, "a.txt", "a-ks\n")
    code, data, _ = s.queue(branch, "--json", MQ_STUB_MOVE_ONCE=f"{s.proj}:dev-map.yaml")
    check("кс: код 0", 0, code)
    check("кс: ворота прогнаны 1 раз", 1, len(s.gate_blocks()))
    check("кс: повторов 1, повторов_без_ворот 1", (1, 1),
          (data.get("повторов"), data.get("повторов_без_ворот")))
    check("кс: в master правка ветки и сдвиг карты", ("a-ks\n", True),
          (s.read("a.txt"), s.read("dev-map.yaml").endswith("# сдвиг master\n")))
    check("кс: ветка не трогала карту — сверки карты нет", [], devmap_checks(s))


def case_other_moved_master(tmp: Path) -> None:
    # Поломка: проверка «только карта» пропускает любой сдвиг master — ворота не судили
    # чужой файл вместе с веткой (прогонов 1 вместо 2).
    print("кд) во время ворот master сдвинут другим файлом — ворота повторяются")
    s = Stand(tmp / "кд")
    s.change(s.proj, "dev-map.yaml", MAP_TEXT, "карта")
    branch, wt = s.tree("kd")
    s.change(wt, "a.txt", "a-kd\n")
    code, data, out = s.queue(branch, "--json", MQ_STUB_MOVE_ONCE=f"{s.proj}:b.txt")
    check("кд: код 0", 0, code)
    check("кд: ворота прогнаны 2 раза", 2, len(s.gate_blocks()))
    check("кд: повторов 1, повторов_без_ворот 0", (1, 0),
          (data.get("повторов"), data.get("повторов_без_ворот")))


def case_map_moved_branch_map(tmp: Path) -> None:
    # Поломка: ветка тоже правит карту, а после rebase карту никто не сверил — склейка
    # двух правок карты (дубль id) ушла бы в master без проверки; или прогнаны полные ворота.
    print("кк) master сдвинут картой, ветка тоже правит карту — только сверка карты")
    s, branch = map_stand(tmp, "kk")
    code, data, out = s.queue(branch, "--json", MQ_STUB_MOVE_ONCE=f"{s.proj}:dev-map.yaml")
    check("кк: код 0", 0, code)
    check("кк: полных ворот 1", 1, len(s.gate_blocks()))
    check("кк: сверка карты прогнана 1 раз", 1, len(devmap_checks(s)))
    check("кк: повторов_без_ворот 1", 1, data.get("повторов_без_ворот"))
    text = s.read("dev-map.yaml")
    check("кк: в master обе правки карты", (True, True),
          ("t1\n    status: test" in text, text.endswith("# сдвиг master\n")))
    check("кк: строка «master сдвинут только картой» в выводе", True,
          "master сдвинут только картой: ворота не повторяю" in out)


def case_map_moved_branch_map_red(tmp: Path) -> None:
    # Поломка: красная сверка карты после rebase не останавливает слияние — склейка
    # двух правок карты с дублем id легла бы в master.
    print("кк2) сверка карты ветки после rebase красная — код 3, master без ветки")
    s, branch = map_stand(tmp, "kk2")
    code, _, out = s.queue(branch, "--json", MQ_STUB_MOVE_ONCE=f"{s.proj}:dev-map.yaml",
                           MQ_STUB_DEVMAP_RED="1")
    check("кк2: код 3, полных ворот 1", (3, 1), (code, len(s.gate_blocks())))
    check("кк2: правки ветки в master нет", False, "t1\n    status: test" in s.read("dev-map.yaml"))


def case_u_broken_map(tmp: Path) -> None:
    # Поломка: невалидная карта закоммичена — сломанный YAML лёг бы в master.
    print("у) невалидная карта основного — не коммитится, код 4 с причиной")
    s, branch = map_stand(tmp, "u")
    broken = MAP_TEXT + "  - id: [t4\n"
    s.write(s.proj, "dev-map.yaml", broken)
    master = s.sha()
    code, data, out = s.queue(branch, "--json")
    check("у: код 4, мешает карта", (4, ["dev-map.yaml"]), (code, data.get("мешают")))
    check("у: причина — невалидная карта", True, "невалидн" in out)
    check("у: master на месте, карта не тронута", (master, broken), (s.sha(), s.read("dev-map.yaml")))


def case_v_called_from_tree(tmp: Path) -> None:
    # Поломка: очередь из дерева помощника грузит модули и держит cwd в дереве, которое
    # сама снимает, — итог падает ModuleNotFoundError/getcwd, JSON не печатается.
    print("в2) вызов из своего дерева: дерево снято, JSON «слито» пришёл")
    s = Stand(tmp / "в2")
    for rel in ("lib", "compare-with-package.py", "install-new.py", "git-commit-locked.sh",
                "select-checks.py", "gates_cache.py", "check-who-calls.py"):
        source = HERE / rel
        (shutil.copytree if source.is_dir() else shutil.copy2)(source, s.proj / "scripts" / rel)
    shutil.copy2(QUEUE, s.proj / "scripts/merge-queue.py")
    git(s.proj, "add", "-A")
    git(s.proj, "commit", "-qm", "очередь в проекте")
    branch, wt = s.tree("v")
    tip = s.change(wt, "a.txt", "a1\n")
    proc = subprocess.run([sys.executable, "scripts/merge-queue.py", branch, "--json"],
                          capture_output=True, text=True, env=s.env(), cwd=wt, timeout=600)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        data = {}
    check("в2: код 0 и JSON «слито»", (0, "слито"), (proc.returncode, data.get("итог")))
    check("в2: без ModuleNotFoundError и getcwd", "", "\n".join(
        line for line in proc.stderr.splitlines() if "ModuleNotFound" in line or "getcwd" in line))
    check("в2: master == вершина, дерево снято", (tip, False), (s.sha(), wt.exists()))


# ─── Д: уборка деревьев помощников (спека тяжёлых прогонов §Д, ревью-3 п.10–11) ───
# Стенд держит WORKTREE_IDLE_H=6 в своём harness.conf; время создания ветки — первая запись
# reflog (GIT_COMMITTER_DATE при `worktree add`), простой — mtime индекса дерева
# (.git/worktrees/<имя>/index). Очередь тяжёлых прогонов — $LOG_DIR/очередь-мутаций/очередь.json
# стенда, не боевая.
IDLE_H = 6
HOUR = 3600
TIDY_LINE = r"убрано \d+, оставлено \d+ \(грязных \d+, неслитых \d+, в очереди \d+\)"


def aged_tree(s: Stand, name: str, created_h: float, index_h: float,
              start: str = "master") -> tuple[str, Path]:
    """Дерево помощника без коммитов (одна запись reflog), созданное created_h часов назад,
    индекс которого не трогали index_h часов."""
    branch, where = f"worktree-agent-{name}", s.proj / ".claude/worktrees" / f"agent-{name}"
    created = int(time.time() - created_h * HOUR)
    subprocess.run(["git", "-C", str(s.proj), "worktree", "add", "-q", "-b", branch, str(where), start],
                   check=True, capture_output=True,
                   env={**os.environ, **GIT_ENV, "GIT_COMMITTER_DATE": f"@{created} +0000"})
    touch_index(s, name, index_h)
    return branch, where


def touch_index(s: Stand, name: str, index_h: float) -> None:
    index = s.proj / ".git" / "worktrees" / f"agent-{name}" / "index"
    when = time.time() - index_h * HOUR
    os.utime(index, (when, when))


def idle_stand(tmp: Path, name: str) -> Stand:
    s = Stand(tmp / name)
    with s.conf.open("a", encoding="utf-8") as fh:
        fh.write(f'WORKTREE_IDLE_H="{IDLE_H}"\n')
    return s


def case_w_idle_trees(tmp: Path) -> None:
    """Д(а)–(д), ревью-3 п.11: дерево без коммита снимается только при И (создано И индекс
    старше WORKTREE_IDLE_H), чистое, слитое, с reflog."""
    print("Д) уборка деревьев без коммита по возрасту")
    s = idle_stand(tmp, "Д")
    # (а) Поломка: helper_worked по-прежнему держит любую ветку с одной записью reflog —
    # 15 брошенных деревьев в .claude/worktrees так и лежат.
    b_a, w_a = aged_tree(s, "da", created_h=7, index_h=7)
    # (б) Поломка: возраст не смотрится вовсе (снимается любое без коммита).
    b_b, w_b = aged_tree(s, "db", created_h=5, index_h=7)
    # (в) Поломка: «создано ИЛИ индекс» — помощник, который час назад трогал индекс, теряет дерево.
    b_c, w_c = aged_tree(s, "dc", created_h=7, index_h=1)
    # (г) Поломка: возраст без проверки грязи — неотслеженный файл помощника уходит с деревом (И-1).
    b_d, w_d = aged_tree(s, "dd", created_h=7, index_h=7)
    s.write(w_d, "черновик.txt", "данные\n")
    touch_index(s, "dd", 7)
    # (д) Поломка: «по возрасту без слитости» — ветка с коммитом не в master и одной записью reflog.
    scratch = s.proj / ".claude/worktrees" / "mq-scratch"
    git(s.proj, "worktree", "add", "-q", "--detach", str(scratch), "master")
    foreign = s.change(scratch, "e.txt", "e-неслитое\n")
    git(s.proj, "worktree", "remove", "--force", str(scratch))
    b_e, w_e = aged_tree(s, "de", created_h=7, index_h=7, start=foreign)
    # (ж) ревью-3 п.11. Поломка: нет reflog → «время создания 0» → снято как древнее.
    b_g, w_g = aged_tree(s, "dg", created_h=7, index_h=7)
    (s.proj / ".git" / "logs" / "refs" / "heads" / b_g).unlink()
    code, _, out = s.queue("--убрать")
    check("Д: --убрать код 0", 0, code)
    check("Д(а): без коммита, без замка, чистое, создано и индекс 7 ч назад — снято (дерево и ветка)",
          (False, False), (w_a.exists(), s.branch_exists(b_a)))
    check("Д(б): создано 5 ч назад — оставлено", (True, True), (w_b.exists(), s.branch_exists(b_b)))
    check("Д(в): создано 7 ч, индекс 1 ч — оставлено (условие «И», не «ИЛИ»)", (True, True),
          (w_c.exists(), s.branch_exists(b_c)))
    check("Д(г): грязное (неотслеженный файл) — оставлено вместе с файлом", (True, "данные\n"),
          (w_d.exists(), s.read("черновик.txt", w_d) if w_d.exists() else None))
    check("Д(д): неслитое с одной записью reflog — оставлено", (True, True),
          (w_e.exists(), s.branch_exists(b_e)))
    check("Д(ж): у ветки нет reflog (время создания неизвестно) — оставлено", (True, True),
          (w_g.exists(), s.branch_exists(b_g)))
    check("Д: --убрать печатает «убрано N, оставлено M (грязных K, неслитых U, в очереди Q)»",
          True, bool(re.search(TIDY_LINE, out)))


def queue_request(s: Stand, tree: Path, taken: int | None) -> None:
    """Висящая заявка очереди тяжёлых прогонов стенда на дерево tree; taken — pid захода."""
    place = s.logs / "очередь-мутаций"
    place.mkdir(parents=True, exist_ok=True)
    request = {"знак": f"проба-{taken}", "ids": ["hr.проба"], "команда": ["true"], "с": time.time(),
               "заход": taken, "дерево": str(tree), "попыток": 0, "не_раньше": 0, "среда": {}}
    (place / "очередь.json").write_text(json.dumps({"заявки": [request], "итоги": {}},
                                                   ensure_ascii=False), encoding="utf-8")


def case_x_queued_tree(tmp: Path) -> None:
    """Д(е), ревью-3 п.10: дерево из поля «дерево» висящей заявки не снимается ни на пути
    merge_branch → finish_tree, ни в cleanup; висящая — и ожидающая, и взятая в заход."""
    print("Д(е) дерево с заявкой очереди тяжёлых прогонов")
    for label, taken in (("ожидающая", None), ("взятая в заход", os.getpid())):
        s = idle_stand(tmp, f"Де-{'взятая' if taken else 'ждёт'}")
        # Путь слияния. Поломка: проверка заявки только в cleanup — finish_tree снимает
        # дерево, в котором идёт (или вот-вот пойдёт) заход.
        branch, wt = s.tree("qe")
        s.change(wt, "a.txt", "a-qe\n")
        queue_request(s, wt, taken)
        code, data, _ = s.queue(branch, "--json")
        check(f"Д(е) {label}: слияние прошло (код 0)", 0, code)
        check(f"Д(е) {label}: merge_branch → finish_tree оставил дерево с заявкой", (True, True),
              (wt.exists(), s.branch_exists(branch)))
        check(f"Д(е) {label}: «оставлено» называет дерево", True,
              wt.name in json.dumps(data.get("оставлено"), ensure_ascii=False))
        # Путь уборки. Поломка: смотрится только `заход is None` (взятую снимают посреди захода)
        # или место очереди зашито боевым путём (заявку стенда не видно).
        branch2, wt2 = s.tree("qc")
        s.change(wt2, "b.txt", "b-qc\n")
        git(s.proj, "merge", "-q", "--ff-only", branch2)
        queue_request(s, wt2, taken)
        code, _, out = s.queue("--убрать")
        check(f"Д(е) {label}: --убрать оставил слитое чистое дерево с заявкой", (0, True, True),
              (code, wt2.exists(), s.branch_exists(branch2)))
        check(f"Д(е) {label}: печать считает его «в очереди 1»", True, "в очереди 1" in out)


def case_y_unified_carried(tmp: Path) -> None:
    # Поломка: главы UNIFIED/ числятся живыми данными сверки — очередь их в пакет не
    # несёт, пакет отстаёт, раскладка отдаёт копиям старые главы (19.11: раздел RFC).
    print("y) главы UNIFIED/ уходят в пакет, как код")
    s = Stand(tmp / "y")
    chapter = "UNIFIED/05-ПАЙПЛАЙН.md"
    s.change(s.proj, chapter, "# Пайплайн\n", "глава в проекте")
    s.change(s.pkg, chapter, "# Пайплайн\n", "глава в пакете")
    branch, wt = s.tree("y")
    s.change(wt, chapter, "# Пайплайн\n\n## RFC\n", "Глава: раздел RFC")
    head = s.sha("HEAD", s.pkg)
    code, data, out = s.queue(branch, "--json")
    check("y: код 0", 0, code)
    check("y: глава в пакете стенда — как в ветке", "# Пайплайн\n\n## RFC\n",
          s.read(chapter, s.pkg))
    check("y: глава в коммите очереди пакета", [chapter],
          git(s.pkg, "-c", "core.quotepath=false", "show", "--format=", "--name-only",
              "HEAD").split() if s.sha("HEAD", s.pkg) != head else [])
    check("y: пакет_файлов 1", 1, data.get("пакет_файлов"))


def case_sel_graph_red(tmp: Path) -> None:
    """Фаза 4, больной: ветка ломает пробу, задетую только графом «кто зовёт»."""
    # Поломка: очередь гоняет только pre-commit-gate (как до фазы 4) — код 0, ветка влита;
    # выбор без правила 3 — проба узла не выбрана, тот же исход; выбор «всё» вместо
    # задетого — запись называет и пробу-вторую.
    print("В1) ветка ломает пробу, задетую только графом")
    s = Stand(tmp / "В1")
    branch, wt = s.tree("g1")
    s.change(wt, "scripts/node_lib.py", "def verdict():\n    return 1\n")
    master = s.sha()
    code, data, out = s.queue(branch, "--json")
    check("В1: код 3", 3, code)
    check("В1: master на месте, ветка жива", (master, True), (s.sha(), s.branch_exists(branch)))
    # Второй вызов — узнавание красной на дереве master (спека шаг2 § 5а.1 п.3).
    check("В1: gates.sh звался с --только «проба-узла» и исполнил её (ветка, затем master)",
          ["только=проба-узла", "проверка=проба-узла"] * 2, s.gates_sh_log())
    check("В1: красная проба названа в выводе", True, "проба-узла" in out)
    check("В1: --json и журнал несут выбор [проба-узла]", (["проба-узла"], ["проба-узла"]),
          (data.get("выбор"), s.last_data().get("выбор")))
    check("В1: временного дерева mq-* нет", [], s.mq_trees())


def case_sel_doc_only(tmp: Path) -> None:
    """Фаза 4: правка документа — только pre-commit-gate, выбор пуст."""
    # Поломка: gates.sh зовётся и с пустым выбором (боевой gates.sh на `--только ""` падает,
    # без --только — полный прогон); поле выбор не пишется при пустом выборе.
    print("В2) правка документа — только pre-commit-gate")
    s = Stand(tmp / "В2")
    branch, wt = s.tree("g2")
    tip = s.change(wt, "docs/заметка.md", "текст\n")
    code, data, _ = s.queue(branch, "--json")
    check("В2: код 0, влито", (0, tip), (code, s.sha()))
    check("В2: pre-commit-gate шёл", True, bool(s.gate_blocks()))
    check("В2: gates.sh не звался", [], s.gates_sh_log())
    check("В2: --json и журнал несут выбор []", ([], []),
          (data.get("выбор"), s.last_data().get("выбор")))


def case_sel_ceiling(tmp: Path) -> None:
    """Фаза 4: выбор дольше выбор.потолок_мин — «не дошли», код 3."""
    # Поломка: потолок не читается из release-train.yaml (умолчание 15 мин — прогон ждёт
    # заглушку до конца); subprocess.run(timeout=…) без своей группы процессов — убитый bash
    # оставляет sleep, тот держит трубу, и очередь всё равно ждёт до конца сна; исход
    # «ворота красные» вместо «не дошли».
    print("В3) выбор дольше потолка — «не дошли»")
    s = Stand(tmp / "В3")
    s.change(s.proj, "harness/config/release-train.yaml",
             "выбор: { потолок_мин: 0.1 }\nвсегда_всё:\n  - scripts/shared_node.sh\n",
             "потолок стенда 6 с")
    branch, wt = s.tree("g3")
    s.change(wt, "scripts/node_lib.py", "def verdict():\n    return 0  # ок\n")
    master = s.sha()
    started = time.monotonic()
    code, _, out = s.queue(branch, MQ_STUB_GATES_SLEEP="60")
    elapsed = time.monotonic() - started
    check("В3: код 3", 3, code)
    check("В3: причина «не дошли»", True, "не дошли" in out)
    check("В3: master на месте, ветка жива", (master, True), (s.sha(), s.branch_exists(branch)))
    check("В3: очередь не ждала сна заглушки (< 40 с при потолке 6 с)", True, elapsed < 40)
    check("В3: временного дерева mq-* нет", [], s.mq_trees())
    # Поломка: по потолку убит один bash (proc.kill), а не группа — фоновый сон заглушки живёт.
    pid_file = s.logs / "stub-gates-sleep.pid"
    pid = int(pid_file.read_text().strip()) if pid_file.exists() else None
    deadline = time.monotonic() + 5
    while pid and alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    check("В3: потомок gates.sh (фоновый сон) убит вместе с группой", (True, False),
          (pid is not None, bool(pid) and alive(pid)))


def case_sel_record_ceiling(tmp: Path) -> None:
    """Узкий выбор: «не дошли» — причина и красные в записи, не только в say()."""
    # Поломка: причина пишется одной строкой для обоих исходов (из текста Stop) — «красные»
    # при потолке; красные берутся из выбора, а не из вывода — убитый выбор назвал бы пробу.
    print("З1) выбор дольше потолка — запись: причина «не дошли», красных нет")
    s = Stand(tmp / "З1")
    s.change(s.proj, "harness/config/release-train.yaml",
             "выбор: { потолок_мин: 0.1 }\nвсегда_всё:\n  - scripts/shared_node.sh\n",
             "потолок стенда 6 с")
    branch, wt = s.tree("z1")
    s.change(wt, "scripts/node_lib.py", "def verdict():\n    return 0  # ок\n")
    code, data, _ = s.queue(branch, "--json", MQ_STUB_GATES_SLEEP="60")
    row = s.last_data()
    check("З1: код 3", 3, code)
    check("З1: причина «не дошли» в --json и журнале", ("не дошли", "не дошли"),
          (data.get("причина"), row.get("причина")))
    check("З1: красные [] в --json и журнале", ([], []), (data.get("красные"), row.get("красные")))


def case_sel_record_red(tmp: Path) -> None:
    """Узкий выбор: красный выбор — причина «красные» и имена красных."""
    # Поломка: красные = весь выбор (проба-вторая зелёная, но названа); имена не разбираются из
    # строки «ПЛОХО · <имя> (код N)» боевого формата; поле только в --json, не в журнале.
    print("З2) красный выбор — запись: причина «красные», имя красной")
    s = Stand(tmp / "З2")
    branch, wt = s.tree("z2")
    s.change(wt, "scripts/node_lib.py", "def verdict():\n    return 1\n")
    s.change(wt, "scripts/probe_other.py", "print('вторая, зелёная')\n")
    code, data, _ = s.queue(branch, "--json")
    row = s.last_data()
    check("З2: код 3, выбор — обе пробы", (3, ["проба-узла", "проба-вторая"]), (code, data.get("выбор")))
    check("З2: причина «красные» в --json и журнале", ("красные", "красные"),
          (data.get("причина"), row.get("причина")))
    check("З2: красные — только проба-узла в --json и журнале", (["проба-узла"], ["проба-узла"]),
          (data.get("красные"), row.get("красные")))


def case_sel_record_red_strict(tmp: Path) -> None:
    """Узкий выбор: имя красной — из строки боевого формата целиком, вариант «соло» тоже."""
    # Поломка: разбор берёт любую строку со словом «ПЛОХО» — вывод самой пробы называет
    # красной несуществующую проверку; разбор без варианта «— красная и соло» теряет красную.
    print("З3) красная «соло» и строка «ПЛОХО» в выводе пробы — красные: только проба-узла")
    s = Stand(tmp / "З3")
    branch, wt = s.tree("z3")
    s.change(wt, "scripts/node_lib.py",
             "def verdict():\n    print('  ПЛОХО · обманка: строка вывода пробы')\n    return 1\n")
    code, data, _ = s.queue(branch, "--json", MQ_STUB_SOLO="проба-узла")
    check("З3: код 3, причина «красные»", (3, "красные"), (code, data.get("причина")))
    check("З3: красные — только проба-узла", ["проба-узла"], data.get("красные"))


def case_sel_range_from_rebase(tmp: Path) -> None:
    """Узкий выбор: диапазон — от sha master при rebase, не от имени master."""
    # Поломка: диапазон «master..вершина» читает master после pre-commit (2–6 мин): чужой коммит
    # за это время попадает в разницу, и выбор берёт его проверки. Чужой коммит здесь трогает
    # probe_other.py — файл проверки «проба-вторая»; ветка трогает только node_lib.py.
    print("Ш1) master сдвинулся между rebase и выбором — выбор без файлов чужого коммита")
    s = Stand(tmp / "Ш1")
    branch, wt = s.tree("w1")
    s.change(wt, "scripts/node_lib.py", "def verdict():\n    return 0  # ветка\n")
    code, _, _ = s.queue(branch, "--json", MQ_STUB_MOVE_FILE="scripts/probe_other.py",
                         MQ_STUB_MOVE_ROOT=str(s.proj))
    calls = [line for line in s.gates_sh_log() if line.startswith("только=")]
    check("Ш1: чужой коммит сделан (стенд жив)", True, (s.logs / "stub-moved").exists())
    check("Ш1: код 0 — влито после повтора", 0, code)
    check("Ш1: выбор первого круга не взял проба-вторая (файл чужого коммита)",
          [], [line for line in calls[:1] if "проба-вторая" in line])
    check("Ш1: выбор гонялся (проба-узла)", True, any("проба-узла" in line for line in calls))


PRE_REBASE_GIT = """#!/usr/bin/env bash
# Обёртка git стенда Ш2: перед первым rebase — чужой коммит в master основного, т.е. между
# чтением sha master очередью и самим rebase. Дальше — настоящий git.
case " $* " in
  *" rebase --abort "*) ;;
  *" rebase "*)
    if [ ! -e "$LOG_DIR/pre-rebase-moved" ]; then
      : > "$LOG_DIR/pre-rebase-moved"
      echo "print('чужая')" > "$MQ_PRE_REBASE_ROOT/scripts/probe_other.py"
      env -u GIT_DIR -u GIT_INDEX_FILE -u GIT_WORK_TREE \\
        REAL_GIT -C "$MQ_PRE_REBASE_ROOT" commit -q -m "чужой коммит" -- scripts/probe_other.py
    fi ;;
esac
exec REAL_GIT "$@"
"""


def case_sel_range_before_rebase(tmp: Path) -> None:
    """Узкий выбор: master сдвинулся между чтением sha и rebase — чужой коммит не в диапазоне."""
    # Поломка: sha master читается до rebase, а rebase идёт на имя master: rebase ложится на
    # новый master, и диапазон «старый sha..вершина» втягивает чужой коммит (probe_other.py —
    # файл «проба-вторая»). Ш1 этого не ловит: там master уходит уже после rebase.
    print("Ш2) master сдвинулся между чтением sha и rebase — выбор без файлов чужого коммита")
    s = Stand(tmp / "Ш2")
    bin_dir = s.base / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "git"
    wrapper.write_text(PRE_REBASE_GIT.replace("REAL_GIT", shutil.which("git")), encoding="utf-8")
    wrapper.chmod(0o755)
    branch, wt = s.tree("w2")
    s.change(wt, "scripts/node_lib.py", "def verdict():\n    return 0  # ветка\n")
    code, _, _ = s.queue(branch, "--json", MQ_PRE_REBASE_ROOT=str(s.proj),
                         PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    calls = [line for line in s.gates_sh_log() if line.startswith("только=")]
    check("Ш2: чужой коммит сделан перед rebase (стенд жив)", True,
          (s.logs / "pre-rebase-moved").exists())
    check("Ш2: код 0 — влито", 0, code)
    check("Ш2: выбор первого круга не взял проба-вторая (файл чужого коммита)",
          [], [line for line in calls[:1] if "проба-вторая" in line])
    check("Ш2: выбор гонялся (проба-узла)", True, any("проба-узла" in line for line in calls))


def alive(pid: int) -> bool:
    """Процесс жив и не зомби."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def case_sel_green(tmp: Path) -> None:
    """Фаза 4: зелёный выбор — влито, запись несёт выбор, выбор_с, дерево."""
    # Поломка: выбор_с не меряет прогон gates.sh (ноль, время select-checks); дерево — не
    # дерево вершины (master, sha коммита) или замаскировано в журнале как секрет
    # (нет в DIGEST_KEYS events.py); поля только в --json, не в журнале.
    print("В4) зелёный выбор — влито, поля записи")
    s = Stand(tmp / "В4")
    branch, wt = s.tree("g4")
    tip = s.change(wt, "scripts/node_lib.py", "def verdict():\n    return 0  # ещё ок\n")
    tip_tree = s.sha(f"{tip}^{{tree}}")
    code, data, _ = s.queue(branch, "--json", MQ_STUB_GATES_SLEEP="2")
    row = s.last_data()
    check("В4: код 0, влито", (0, tip), (code, s.sha()))
    check("В4: gates.sh исполнил пробу узла", ["только=проба-узла", "проверка=проба-узла"],
          s.gates_sh_log())
    check("В4: выбор [проба-узла] в --json и журнале", (["проба-узла"], ["проба-узла"]),
          (data.get("выбор"), row.get("выбор")))
    seconds = [d.get("выбор_с") for d in (data, row)]
    check("В4: выбор_с — число ≥ 2 (заглушка спала 2 с) в --json и журнале", True,
          all(isinstance(v, (int, float)) and v >= 2 for v in seconds))
    trees = [str(d.get("дерево") or "") for d in (data, row)]
    check("В4: дерево — дерево вершины (≥ 8 знаков префикса) в --json и журнале", True,
          all(len(v) >= 8 and tip_tree.startswith(v) for v in trees))


def case_sel_shared_node(tmp: Path) -> None:
    """Фаза 4, К-1 ревью (вариант 2, 20.11): общий узел в очереди — не «всё», а задетые."""
    # Поломка: правило 4 осталось в очереди («всё» шло 32–87 мин при потолке 30 — такую
    # ветку очередь не сливала никогда) — выбор называет и пробу-вторую; узел из всегда_всё
    # выкинут из графа целиком — выбор пуст, звавшую узел пробу не судит никто.
    print("В5) общий узел — только задетые проверки, не «всё»")
    s = Stand(tmp / "В5")
    branch, wt = s.tree("g5")
    tip = s.change(wt, "scripts/shared_node.sh", "# общий узел стенда, правка\n")
    code, data, _ = s.queue(branch, "--json")
    check("В5: код 0, влито", (0, tip), (code, s.sha()))
    check("В5: выбор — только проба, зовущая узел", ["проба-узла"], data.get("выбор"))
    check("В5: gates.sh получил только её", ["только=проба-узла", "проверка=проба-узла"],
          s.gates_sh_log())


def case_sel_slot_wait(tmp: Path) -> None:
    """Фаза 4, И-2 ревью: ожидание места heavy-slot не съедает потолок выбора."""
    # Поломка: часы потолка идут от запуска gates.sh (communicate(timeout=потолок)) — 10 с
    # ожидания места при потолке 6 с дают «не дошли» по чужой вине; выбор_с считает и
    # ожидание (медиана §11 «≤ 10 мин» мерит очередь мест, а не прогон).
    print("В7) ожидание места дольше потолка — не «не дошли»")
    s = Stand(tmp / "В7")
    s.change(s.proj, "harness/config/release-train.yaml",
             "выбор: { потолок_мин: 0.1 }\nвсегда_всё:\n  - scripts/shared_node.sh\n",
             "потолок стенда 6 с")
    branch, wt = s.tree("g7")
    tip = s.change(wt, "scripts/node_lib.py", "def verdict():\n    return 0  # место\n")
    code, data, out = s.queue(branch, "--json", MQ_STUB_SLOT_WAIT="10")
    check("В7: место ждали (заглушка пула спала)", True, "место: жду 10" in s.gates_sh_log())
    check("В7: код 0, влито — не «не дошли»", (0, tip, False), (code, s.sha(), "не дошли" in out))
    check("В7: проба узла исполнена", True, "проверка=проба-узла" in s.gates_sh_log())
    check("В7: выбор_с без ожидания места (< 10 с)", True,
          isinstance(data.get("выбор_с"), (int, float)) and data["выбор_с"] < 10)


def case_sel_slot_never(tmp: Path) -> None:
    """Фаза 4, §7.3: места нет совсем — ждут не дольше потолка и идут без места."""
    # Поломка: снят --не-дольше у heavy-slot — пул ждёт место без предела, очередь убивает
    # группу по 2 × потолок, и ветка с зелёным выбором получает «не дошли».
    print("В9) места нет никогда — выбор идёт без места после потолка")
    s = Stand(tmp / "В9")
    s.change(s.proj, "harness/config/release-train.yaml",
             "выбор: { потолок_мин: 0.1 }\nвсегда_всё:\n  - scripts/shared_node.sh\n",
             "потолок стенда 6 с")
    branch, wt = s.tree("g9")
    tip = s.change(wt, "scripts/node_lib.py", "def verdict():\n    return 0  # без места\n")
    code, _, out = s.queue(branch, "--json", MQ_STUB_SLOT_FOREVER="1")
    check("В9: пул ждал с пределом 6 с", True, "место: нет, жду 6" in s.gates_sh_log())
    check("В9: код 0, влито — не «не дошли»", (0, tip, False), (code, s.sha(), "не дошли" in out))
    check("В9: проба узла исполнена", True, "проверка=проба-узла" in s.gates_sh_log())


def case_sel_ceiling_from_master(tmp: Path) -> None:
    """Фаза 4, Н-1 ревью: потолок выбора — из master (проекта), ветка его не поднимает."""
    # Поломка: потолок читается из дерева ветки — ветка ставит себе 15 мин, и выбор, который
    # дольше потолка master (6 с), ждут до конца сна заглушки (60 с), а не «не дошли».
    print("В10) ветка поднимает себе потолок — действует потолок master")
    s = Stand(tmp / "В10")
    # Пакет — как проект: правку release-train.yaml ветки очередь несёт в пакет, не код 7.
    for root in (s.proj, s.pkg):
        s.change(root, "harness/config/release-train.yaml",
                 "выбор: { потолок_мин: 0.1 }\nвсегда_всё:\n  - scripts/shared_node.sh\n",
                 "потолок стенда 6 с")
    branch, wt = s.tree("g10")
    s.change(wt, "harness/config/release-train.yaml",
             "выбор: { потолок_мин: 15 }\nвсегда_всё:\n  - scripts/shared_node.sh\n",
             "ветка: потолок 15 мин")
    s.change(wt, "scripts/node_lib.py", "def verdict():\n    return 0  # свой потолок\n")
    master = s.sha()
    started = time.monotonic()
    code, _, out = s.queue(branch, MQ_STUB_GATES_SLEEP="60")
    elapsed = time.monotonic() - started
    check("В10: код 3 «не дошли» по потолку master", (3, True), (code, "не дошли" in out))
    check("В10: очередь не ждала сна заглушки (< 40 с)", True, elapsed < 40)
    check("В10: master на месте", master, s.sha())


def case_lock_wait_covers_ceiling(tmp: Path) -> None:
    """Фаза 4, И-3 ревью: предел ожидания замка очереди покрывает потолок выбора."""
    # Поломка: предел ожидания — голый MERGE_QUEUE_WAIT_MIN (30 мин живьём при потолке 30):
    # пока первая ветка сидит в выборе, вторая получает код 8; предел снят совсем — замок,
    # занятый сверх предела, ждут бесконечно (вторая часть случая).
    print("В8) вторая ветка во время долгого выбора первой — не код 8")
    s = Stand(tmp / "В8")
    s.change(s.proj, "harness/config/release-train.yaml",
             "выбор: { потолок_мин: 0.2 }\nвсегда_всё:\n  - scripts/shared_node.sh\n",
             "потолок стенда 12 с")
    # 0,05 мин = 3 с: голый предел короче выбора первой ветки (6 с сна заглушки).
    s.conf.write_text(s.conf.read_text(encoding="utf-8").replace(
        'MERGE_QUEUE_WAIT_MIN="2"', 'MERGE_QUEUE_WAIT_MIN="0.05"'), encoding="utf-8")
    b1, w1 = s.tree("g8a")
    b2, w2 = s.tree("g8b")
    s.change(w1,"scripts/node_lib.py", "def verdict():\n    return 0  # первая\n")
    s.change(w2, "docs/вторая.md", "текст\n")
    env = s.env(MQ_STUB_GATES_SLEEP="6")
    first = subprocess.Popen(s.argv(b1, "--json"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=env, cwd=s.base)
    deadline = time.monotonic() + 60
    while not s.gates_sh_log() and time.monotonic() < deadline and first.poll() is None:
        time.sleep(0.2)
    check("В8: первая дошла до выбора (держит замок)", True, bool(s.gates_sh_log()))
    code2, data2, out2 = s.queue(b2, "--json")
    first_out, _ = first.communicate(timeout=600)
    try:
        code1 = json.loads(first_out).get("код")
    except ValueError:
        code1 = None
    check("В8: обе ветки влиты (коды 0, 0), вторая не код 8", (0, 0), (code1, code2))
    check("В8: вторая ждала замок дольше голого предела 3 с", True,
          float(data2.get("ожидание_с") or 0) > 3)
    check("В8: в master обе правки", (True, True),
          ("первая" in s.read("scripts/node_lib.py"), (s.proj / "docs/вторая.md").exists()))
    # Предел остаётся пределом: замок, занятый дольше «голый предел + 2 × потолок» (3 + 24 с),
    # даёт код 8, а не вечное ожидание.
    import fcntl  # noqa: PLC0415 — нужен только здесь
    b3, w3 = s.tree("g8c")
    s.change(w3, "docs/третья.md", "текст\n")
    with open(s.logs / "merge-queue.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        started = time.monotonic()
        try:
            code3 = subprocess.run(s.argv(b3, "--json"), capture_output=True, text=True,
                                   env=s.env(), cwd=s.base, timeout=90).returncode
        except subprocess.TimeoutExpired:
            code3 = "ждала замок без предела (> 90 с)"
        waited = time.monotonic() - started
    check("В8: замок, занятый сверх предела, — код 8", 8, code3)
    check("В8: код 8 не раньше потолка выбора и не позже 60 с (предел из потолка)", True,
          20 <= waited < 60)


def case_sel_same_tree(tmp: Path) -> None:
    """Фаза 4: зелёная запись хука на то же дерево снимает только pre-commit, не выбор."""
    # Поломка: выбор встроен после раннего возврата checked_tree — помощник, чей master не
    # ушёл (дерево вершины = дерево, судимое хуком при коммите), сливает красный выбор.
    print("В6) то же дерево, что судил хук, — выбор всё равно гоняется")
    s = Stand(tmp / "В6")
    branch, wt = s.tree("g6")
    tip = s.change(wt, "scripts/node_lib.py", "def verdict():\n    return 1\n")
    master = s.sha()
    row = {"gate": "pre-commit", "tree": s.sha(f"{tip}^{{tree}}"), "base": master,
           "verdict": "pass"}
    (s.logs / "gates.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    code, _, _ = s.queue(branch, "--json")
    check("В6: pre-commit не гонялся (то же дерево)", [], s.gate_blocks())
    check("В6: код 3 — выбор гонялся и красный", 3, code)
    check("В6: master на месте", master, s.sha())
    # Второй вызов — узнавание красной на дереве master (спека шаг2 § 5а.1 п.3).
    check("В6: gates.sh исполнил пробу узла (ветка, затем master)",
          ["только=проба-узла", "проверка=проба-узла"] * 2, s.gates_sh_log())


# ───────── Очередь пачкой и замок данными (спека ит.3 §6) ─────────
# Пробы П1–П29 написаны до кода: на нынешней очереди (один flock на прогон, без заявок) они
# красные. Каталог очереди — $LOG_DIR/merge-queue/{заявки,взяты,итоги,отменены,битые}, файл пульс.
# Гонки ловит дверь MQ_TEST_PAUSE=<точка>:<секунд> (§4.4): слиятель кладёт метку
# $LOG_DIR/mq-pause-<точка> и спит; проба ждёт метку, а не удачу sleep.

MQ = "merge-queue"
REQUEST_KEYS = {"ветка", "дерево", "pid", "подано", "попыток", "одна", "слиятель", "orig_sha", "сирота"}
RFC3339 = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?([+-]\d\d:\d\d|Z)$")
QUEUED_WAIT = 10   # столько проба ждёт, что подача положила заявку; нынешний код не кладёт никогда
MARK_WAIT = 60     # столько проба ждёт метку двери


def mq_dir(s: Stand, sub: str = "") -> Path:
    return s.logs / MQ / sub if sub else s.logs / MQ


def mq_names(s: Stand, sub: str) -> list:
    """Имена заявок подкаталога без недописанных `.tmp-*` — по порядку подачи."""
    where = mq_dir(s, sub)
    return sorted(p.name for p in where.iterdir() if not p.name.startswith(".tmp-")) \
        if where.is_dir() else []


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def results_by_branch(s: Stand) -> dict:
    """{ветка: отчёт} из итоги/*.json — итог заявки без живого слушателя виден только тут."""
    found = {}
    for name in mq_names(s, "итоги"):
        report = read_json(mq_dir(s, "итоги") / name).get("отчёт") or {}
        if report.get("ветка"):
            found[report["ветка"]] = report
    return found


def wait_for(predicate, timeout: float, alive: tuple = ()) -> bool:
    """Ждать условие; все процессы alive вышли — дальше ждать нечего."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        if alive and all(p.poll() is not None for p in alive):
            return bool(predicate())
        time.sleep(0.1)
    return bool(predicate())


def spawn(s: Stand, *args: str, cwd: Path | None = None, **extra: str) -> subprocess.Popen:
    return subprocess.Popen(s.argv(*args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=s.env(**extra), cwd=cwd or s.base)


def collect(proc: subprocess.Popen, timeout: float = 300) -> tuple:
    """(код, объект --json, stderr); висящий процесс убит — код «висит»."""
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return "висит", {}, err
    try:
        return proc.returncode, json.loads(out), err
    except ValueError:
        return proc.returncode, {}, err


def set_conf(s: Stand, **keys: str) -> None:
    text = s.conf.read_text(encoding="utf-8")
    for key, value in keys.items():
        line = f'{key}="{value}"'
        text, hits = re.subn(rf'^{key}=".*"$', line, text, flags=re.M)
        if not hits:
            text += line + "\n"
    s.conf.write_text(text, encoding="utf-8")


def fast_limit(s: Stand, wait_min: str, ceiling_min: str) -> None:
    """Предел кода 8 = WAIT_MIN + 2 × потолок выбора (минуты). Потолок читается из master
    проекта — коммит до создания деревьев. Ветки этих проб правят только *.txt и GATE_RED:
    выбор пуст, короткий потолок его не режет."""
    rel = "harness/config/release-train.yaml"
    s.change(s.proj, rel, s.read(rel).replace("потолок_мин: 15", f"потолок_мин: {ceiling_min}"),
             "потолок стенда")
    set_conf(s, MERGE_QUEUE_WAIT_MIN=wait_min)


class HeldLock:
    """Замок очереди держит проба, как чужой слиятель. pulse — проба ещё и двигает пульс,
    как живой слиятель: тогда короткий предел пробы не даёт подачам код 8."""

    def __init__(self, s: Stand, pulse: bool = False):
        self.s, self.pulse = s, pulse

    def __enter__(self):
        self.s.logs.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.s.logs / "merge-queue.lock", "w")
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        self.beat()
        return self

    def beat(self) -> None:
        if self.pulse:
            mq_dir(self.s).mkdir(parents=True, exist_ok=True)
            (mq_dir(self.s) / "пульс").touch()

    def __exit__(self, *exc):
        self.handle.close()


def submit_batch(s: Stand, label: str, branches: list, pulse: bool = True, **extra: str) -> dict:
    """Пачка детерминированно: замок у пробы, подачи по одной, каждая кладёт заявку (порядок
    имён = порядок подачи), потом замок отпущен — один слиятель забирает всех разом.
    → {ветка: (код, --json, stderr)}."""
    procs = []
    queued = True
    with HeldLock(s, pulse) as held:
        for branch in branches:
            procs.append((branch, spawn(s, branch, "--json", **extra)))
            want = len(procs)
            if queued:
                queued = wait_for(lambda: held.beat() or len(mq_names(s, "заявки")) >= want,
                                  QUEUED_WAIT)
        check(f"{label}: все {len(branches)} заявки легли в заявки/, пока замок у пробы", True, queued)
    return {branch: collect(proc) for branch, proc in procs}


def rows_of(s: Stand, branch: str, event: str | None = None) -> list:
    return [r for r in s.rows() if (r.get("data") or {}).get("ветка") == branch
            and (event is None or r.get("event") == event)]


def write_request(s: Stand, sub: str, branch: str, tree: Path, **fields) -> str:
    """Заявка по схеме §4.1 (+ «сирота» ревью-2) — как её положила бы подача/слиятель."""
    name = f"{time.time_ns()}-{fields.get('pid', 999999)}.json"
    body = {"ветка": branch, "дерево": str(tree), "pid": 999999,
            "подано": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "попыток": 0,
            "одна": False, "слиятель": None, "orig_sha": None, "сирота": 0, **fields}
    mq_dir(s, sub).mkdir(parents=True, exist_ok=True)
    (mq_dir(s, sub) / name).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return name


def in_master(s: Stand, sha: str) -> bool:
    return subprocess.run(["git", "-C", str(s.proj), "merge-base", "--is-ancestor", sha, "master"],
                          capture_output=True).returncode == 0


def pause_mark(s: Stand, point: str) -> Path:
    return s.logs / f"mq-pause-{point}"


def kill_hard(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGKILL)
    proc.wait()


def case_p1_code8_by_pulse(tmp: Path) -> None:
    """Код 8 значит «очередь не двигалась дольше предела», а не «долго стоял в хвосте». Сегодня
    запись кода 8 несёт ожидание_с 0.0 (31 строка за 19–21.11) — улику не с чем сравнить."""
    # Поломка: ожидание только в --json (судим и журнал); код 8 вернули, а заявка осталась в
    # заявки/ и позже слилась чужим слиятелем (судим отменены/ и master после отпуска).
    print("П1) код 8 по пульсу: ожидание пишется, заявка отменена, ветка не влита")
    s = Stand(tmp / "П1")
    fast_limit(s, "0", "0.025")                     # предел 3 с
    branch, wt = s.tree("p1")
    s.change(wt, "a.txt", "a-p1\n")
    with HeldLock(s):                               # пульса нет: считать «подано»
        code, data, _ = collect(spawn(s, branch, "--json"), timeout=60)
        row = s.last_data()
        check("П1(Ф0): код 8", 8, code)
        check("П1(Ф0): ожидание_с ≥ 3 в --json и в журнале", (True, True),
              (float(data.get("ожидание_с") or 0) >= 3, float(row.get("ожидание_с") or 0) >= 3))
        check("П1: предел_с == 3 в --json", True, abs(float(data.get("предел_с") or 0) - 3) < 0.05)
        check("П1: заявка в отменены/, заявки/ пуст", (1, []),
              (len(mq_names(s, "отменены")), mq_names(s, "заявки")))
    other, wt2 = s.tree("p1b")
    s.change(wt2, "b.txt", "b-p1b\n")
    code2, _, _ = collect(spawn(s, other, "--json"))
    check("П1: после отпуска чужая подача слита, отменённая ветка — нет", (0, "a0\n"),
          (code2, s.read("a.txt")))


def case_p2_precommit_names(tmp: Path) -> None:
    """Красный pre-commit пишет в журнал имена упавших гейтов — без них «почему красное»
    приходится искать по хвосту вывода руками."""
    # Поломка: имена из любой строки с «GATE FAIL» — приманка без префикса [pre-commit] попала бы
    # в красные; ступень не различает pre-commit и выбор.
    print("П2) красный pre-commit — ступень и имена красных в журнале")
    s = Stand(tmp / "П2")
    branch, wt = s.tree("p2")
    s.change(wt, "GATE_RED", "[pre-commit] GATE FAIL: гейт-икс\nGATE FAIL: приманка\n")
    code, data, _ = s.queue(branch, "--json")
    row = s.last_data()
    check("П2: код 3", 3, code)
    check("П2(Ф0): ступень pre-commit в --json и журнале", ("pre-commit", "pre-commit"),
          (data.get("ступень"), row.get("ступень")))
    check("П2(Ф0): красные == [гейт-икс], приманки нет, в --json и журнале",
          (["гейт-икс"], ["гейт-икс"]), (data.get("красные"), row.get("красные")))
    check("П2(Ф0): причина «красные» в журнале", "красные", row.get("причина"))
    branch2, wt2 = s.tree("p2b")
    s.change(wt2, "scripts/node_lib.py", "def verdict():\n    return 1\n")
    code2, _, _ = s.queue(branch2, "--json")
    check("П2(Ф0): красный выбор — код 3, ступень «выбор» в журнале", (3, "выбор"),
          (code2, s.last_data().get("ступень")))


def case_p3_no_reasonless_code3(tmp: Path) -> None:
    """У кода 3 не остаётся пустой причины: «выбор не получен» отличим от красных."""
    # Поломка: причина константой «красные» — проба ждёт именно «нет выбора». «Не развёрнута»
    # пробой не судится (нужен битый объект) — мутант «без причины у не развёрнута» зелёный.
    print("П3) выбор не получен — код 3 с причиной «нет выбора»")
    s = Stand(tmp / "П3")
    branch, wt = s.tree("p3")
    s.change(wt, "harness/config/mechanisms.yaml", "механизмы: [\n")   # select-checks падает
    code, data, out = s.queue(branch, "--json")
    check("П3: код 3, «выбор проверок не получен» в выводе", (3, True),
          (code, "выбор проверок не получен" in out))
    check("П3(Ф0): причина «нет выбора» в --json и журнале", ("нет выбора", "нет выбора"),
          (data.get("причина"), s.last_data().get("причина")))


def case_p4_single_as_today(tmp: Path) -> None:
    """Одиночная подача ведёт себя как сегодня: те же поля и значения, одна строка журнала."""
    # Поломка: лишняя строка журнала от подачи (вторая к строке слиятеля) — строк должно быть 1.
    print("П4) одиночная подача — как сегодня, пачка из одной")
    s = Stand(tmp / "П4")
    branch, wt = s.tree("p4")
    tip = s.change(wt, "a.txt", "a-p4\n")
    before = s.sha()
    code, data, _ = collect(spawn(s, branch, "--json"))
    old = {"код": 0, "итог": "слито", "ветка": branch, "sha_до": before, "sha_после": tip,
           "файлов": 1, "пакет_файлов": 0, "повторов": 0, "конфликт": [], "мешают": [],
           "ключи_машины": []}
    check("П4: код 0 и прежние поля с прежними значениями", old,
          {k: data.get(k) for k in old})
    check("П4: прежние ключи все на месте (новые только добавлены)", True,
          {"ожидание_с", "ворота_с", "выбор", "выбор_с", "дерево", "оставлено"} <= set(data))
    check("П4: пачка из одной, прогонов 1", ([branch], 1),
          ((data.get("пачка") or {}).get("ветки"), (data.get("пачка") or {}).get("прогонов")))
    check("П4: строк журнала по ветке ровно 1", 1, len(rows_of(s, branch)))
    branch2, wt2 = s.tree("p4b")
    s.change(wt2, "b.txt", "b-p4b\n")
    code2, _, out2 = s.queue(branch2)
    check("П4: без --json — строка «merge-queue: слито <ветка> <sha>..<sha>»", (0, True),
          (code2, any(l.startswith(f"merge-queue: слито {branch2} ") for l in out2.splitlines())))


def case_p5_atomic_and_broken(tmp: Path) -> None:
    """Недописанная заявка не читается, битая не валит пачку — иначе одна оборванная запись
    останавливает всю очередь."""
    # Поломка: слиятель читает все файлы заявки/ — пачка падает на мусоре или .tmp съеден.
    print("П5) .tmp-заявка не тронута, битая — в битые/, настоящая слита")
    s = Stand(tmp / "П5")
    branch, wt = s.tree("p5")
    s.change(wt, "a.txt", "a-p5\n")
    mq_dir(s, "заявки").mkdir(parents=True)
    half = '{"ветка": "worktree-agent-p'
    (mq_dir(s, "заявки") / ".tmp-123").write_text(half, encoding="utf-8")
    (mq_dir(s, "заявки") / "0-мусор.json").write_text("мусор{", encoding="utf-8")   # голова очереди
    code, _, _ = collect(spawn(s, branch, "--json"))
    check("П5: настоящая заявка — код 0", 0, code)
    check("П5: .tmp-123 не тронут", half,
          (mq_dir(s, "заявки") / ".tmp-123").read_text(encoding="utf-8")
          if (mq_dir(s, "заявки") / ".tmp-123").exists() else "нет файла")
    check("П5: битая — в битые/", ["0-мусор.json"], mq_names(s, "битые"))
    check("П5: строка mq.битая в журнале", True, any(r.get("event") == "mq.битая" for r in s.rows()))


def case_p6_queued_while_locked(tmp: Path) -> None:
    """Подача при занятом замке встаёт в очередь файлом с полной схемой и абсолютным деревом:
    слиятель с чужим cwd должен найти дерево сам."""
    # Поломка: подача ждёт flock без файла — проба смотрит каталог, пока замок держится.
    print("П6) подача при занятом замке — заявка с полной схемой §4.1")
    s = Stand(tmp / "П6")
    branch, wt = s.tree("p6")
    s.change(wt, "a.txt", "a-p6\n")
    with HeldLock(s):
        proc = spawn(s, branch, "--json")
        queued = wait_for(lambda: mq_names(s, "заявки"), QUEUED_WAIT)
        check("П6: заявка появилась, пока замок держится", True, queued)
        body = read_json(mq_dir(s, "заявки") / mq_names(s, "заявки")[0]) if queued else {}
    check("П6: ключи заявки — схема §4.1 + сирота", sorted(REQUEST_KEYS), sorted(body))
    check("П6: ветка, абсолютное дерево, pid подачи", (branch, str(wt), proc.pid),
          (body.get("ветка"), body.get("дерево"), body.get("pid")))
    check("П6: подано — RFC 3339 со смещением", True, bool(RFC3339.match(str(body.get("подано")))))
    check("П6: попыток 0, одна false, слиятель/orig_sha null, сирота 0", (0, False, None, None, 0),
          tuple(body.get(k, "нет") for k in ("попыток", "одна", "слиятель", "orig_sha", "сирота")))
    check("П6: после отпуска — код 0", 0, collect(proc)[0])


def case_p7_release_race(tmp: Path) -> None:
    """Заявка, пришедшая в окне «слиятель решил отпустить → отпустил», не висит до предела."""
    # Поломка: подача пробует flock один раз — висит до предела (30 с) и уходит с кодом 8.
    print("П7) гонка отпуска: вторая подача слита сразу после отпуска")
    s = Stand(tmp / "П7")
    fast_limit(s, "0.5", "0.025")                   # предел 33 с
    b1, w1 = s.tree("p7a")
    b2, w2 = s.tree("p7b")
    s.change(w1, "a.txt", "a-p7a\n")
    s.change(w2, "b.txt", "b-p7b\n")
    first = spawn(s, b1, "--json", MQ_TEST_PAUSE="перед_отпуском:3")
    marked = wait_for(pause_mark(s, "перед_отпуском").exists, MARK_WAIT, (first,))
    check("П7: слиятель встал в точке перед_отпуском", True, marked)
    second = spawn(s, b2, "--json")
    code1, _, _ = collect(first)
    released = time.monotonic()
    code2, _, _ = collect(second, timeout=90)
    check("П7: обе — код 0", (0, 0), (code1, code2))
    check("П7: вторая слита ≤ 10 с после отпуска (не ждала предел)", True,
          time.monotonic() - released <= 10)


def case_p8_batch_one_gate(tmp: Path) -> None:
    """Пришедшие, пока шли ворота, вливаются одной проверкой — главная экономия задачи."""
    # Поломка: ворота на каждую ветку — блоков 3, а не 2 (первая одна + пачка из двух).
    print("П8) пачка — одни ворота на всех пришедших во время прогона")
    s = Stand(tmp / "П8")
    names = [s.tree(x) for x in ("p8a", "p8b", "p8c")]
    for (branch, wt), rel in zip(names, ("a.txt", "b.txt", "c.txt")):
        s.change(wt, rel, f"{rel}-{branch}\n")
    first = spawn(s, names[0][0], "--json", MQ_STUB_SLEEP="4")
    in_gates = wait_for(lambda: s.gate_blocks(), MARK_WAIT, (first,))
    check("П8: первый слиятель в воротах", True, in_gates)
    rest = []
    queued = True
    for branch, _ in names[1:]:
        rest.append(spawn(s, branch, "--json"))
        want = len(rest)
        queued = queued and wait_for(lambda: len(mq_names(s, "заявки")) >= want, QUEUED_WAIT)
    check("П8: вторая и третья встали в заявки/ во время ворот первой", True, queued)
    results = [collect(first)] + [collect(p) for p in rest]
    check("П8: все — код 0", [0, 0, 0], [r[0] for r in results])
    check("П8: блоков ворот 2", 2, len(s.gate_blocks()))
    batches = [(r[1].get("пачка") or {}).get("ветки") for r in results[1:]]
    check("П8: у второй и третьей общая пачка [вторая, третья]",
          [[names[1][0], names[2][0]]] * 2, batches)


def case_p9_conflict_master(tmp: Path) -> None:
    """Ветка, не сложившаяся с master, выпадает с кодом 1; соседи по пачке едут."""
    # Поломка: выпавшая остаётся в кандидате — её файл в master или пачка целиком с кодом 1.
    print("П9) конфликт с master посреди пачки")
    s = Stand(tmp / "П9")
    trees = [s.tree(x) for x in ("p9a", "p9b", "p9c")]
    s.change(trees[0][1], "a.txt", "a-p9a\n")
    s.change(trees[1][1], "c.txt", "c-p9b\n")
    s.change(trees[2][1], "b.txt", "b-p9c\n")
    s.change(s.proj, "c.txt", "c-master\n")
    res = submit_batch(s, "П9", [b for b, _ in trees])
    check("П9: коды 0, 1, 0", [0, 1, 0], [res[b][0] for b, _ in trees])
    check("П9: конфликт_с master у средней", "master", res[trees[1][0]][1].get("конфликт_с"))
    check("П9: в master крайние, средней нет", ("a-p9a\n", "b-p9c\n", "c-master\n"),
          (s.read("a.txt"), s.read("b.txt"), s.read("c.txt")))
    check("П9: ворота один раз на пачку", 1, len(s.gate_blocks()))


def merger_first(s: Stand, label: str, first: str, others: list, first_cwd: Path | None = None) -> tuple:
    """Слиятель известен заранее: первая подача встаёт в точке перед_забором, остальные кладут
    заявки, пока она стоит. → (процесс первой, [(ветка, процесс)], заявки остальных)."""
    lead = spawn(s, first, "--json", cwd=first_cwd, MQ_TEST_PAUSE="перед_забором:4")
    marked = wait_for(pause_mark(s, "перед_забором").exists, MARK_WAIT, (lead,))
    check(f"{label}: первая — слиятель, встала перед забором", True, marked)
    procs, bodies = [], []
    for item in others:
        branch, args, cwd = item if isinstance(item, tuple) else (item, (item,), None)
        procs.append((branch, spawn(s, *args, "--json", cwd=cwd)))
        want = len(procs) + 1
        ok = wait_for(lambda: len(mq_names(s, "заявки")) >= want, QUEUED_WAIT)
        names = mq_names(s, "заявки")
        bodies.append(read_json(mq_dir(s, "заявки") / names[-1]) if ok and names else {})
    return lead, procs, bodies


def case_p9b_conflict_neighbour(tmp: Path) -> None:
    """Конфликт с соседом по пачке называет соседа, а текст конфликта доходит до своей подачи."""
    # Поломка: конфликт_с всегда «master»; глобальный say — имя файла у слиятеля, не у подачи.
    print("П9б) конфликт с соседом по пачке — имя соседа и файл у своей подачи")
    s = Stand(tmp / "П9б")
    b1, w1 = s.tree("p9first")
    b2, w2 = s.tree("p9second")
    s.change(w1, "a.txt", "a-первая\n")
    s.change(w2, "a.txt", "a-вторая\n")
    lead, procs, _ = merger_first(s, "П9б", b1, [b2])
    code1, _, _ = collect(lead)
    code2, data2, err2 = collect(procs[0][1])
    check("П9б: первая 0, вторая 1", (0, 1), (code1, code2))
    check("П9б: конфликт_с — имя первой", b1, data2.get("конфликт_с"))
    check("П9б: имя файла в stderr подачи второй", True, "a.txt" in err2)


def case_p10_bisect_last(tmp: Path) -> None:
    """Красная пачка: убирать последнюю, пока не зелено, — виновна найденная, не первая."""
    # Поломка: бисекция убирает первую — влиты 2–3, а красная 3 слилась бы.
    print("П10) бисекция находит последнюю")
    s = Stand(tmp / "П10")
    trees = [s.tree(x) for x in ("p10a", "p10b", "p10c")]
    s.change(trees[0][1], "a.txt", "a-p10\n")
    s.change(trees[1][1], "b.txt", "b-p10\n")
    s.change(trees[2][1], "GATE_RED", "красная\n")
    res = submit_batch(s, "П10", [b for b, _ in trees])
    check("П10: коды 0, 0, 3", [0, 0, 3], [res[b][0] for b, _ in trees])
    check("П10: пачка.прогонов == 2 у виновной", 2,
          (res[trees[2][0]][1].get("пачка") or {}).get("прогонов"))
    check("П10: блоков ворот 2, GATE_RED не в master", (2, False),
          (len(s.gate_blocks()), (s.proj / "GATE_RED").exists()))


def case_p11_bisect_ceiling(tmp: Path) -> None:
    """Потолок бисекции: виновного нет — все в одиночки, строка возврата без «код» и ожидания."""
    # Поломка: потолок не читается — 3 блока в пачке; вернувшиеся несут чужие коммиты — вторая
    # краснела бы; ожидание_с числом в строке возврата — лишний образец в медиане Ф4.
    print("П11) потолок бисекции — все назад одиночками")
    s = Stand(tmp / "П11")
    set_conf(s, MERGE_QUEUE_BISECT_MAX="1")
    trees = [s.tree(x) for x in ("p11a", "p11b", "p11c")]
    s.change(trees[0][1], "GATE_RED", "красная\n")
    s.change(trees[1][1], "a.txt", "a-p11\n")
    s.change(trees[2][1], "b.txt", "b-p11\n")
    res = submit_batch(s, "П11", [b for b, _ in trees])
    check("П11: коды 3, 0, 0", [3, 0, 0], [res[b][0] for b, _ in trees])
    check("П11: блоков всего 5 (2 в пачке + 3 одиночки)", 5, len(s.gate_blocks()))
    back = [r for r in s.rows() if r.get("event") == "mq.возврат"]
    check("П11: mq.возврат ×3 — по одной на ветку", sorted(b for b, _ in trees),
          sorted((r.get("data") or {}).get("ветка") for r in back))
    check("П11: строка возврата: одна true, ожидание_с null, без код/итог, outcome skipped",
          [(True, None, False, "skipped")] * 3,
          [((r.get("data") or {}).get("одна"), (r.get("data") or {}).get("ожидание_с", "нет"),
            "код" in (r.get("data") or {}) or "итог" in (r.get("data") or {}), r.get("outcome"))
           for r in back])
    check("П11: строка возврата: попыток 1 (после возврата) и пачка", [(1, True)] * 3,
          [((r.get("data") or {}).get("попыток"), bool((r.get("data") or {}).get("пачка"))) for r in back])


def case_p12_tail_back(tmp: Path) -> None:
    """Хвост за виновной не осуждается: назад в очередь ровно своими коммитами (П12), числа
    прогонов — по строке на прогон (П14б), невлитые ветки не тронуты цепочкой (П15)."""
    # Поломка П12: хвост осуждён вместе с красной — ждём 0 у 3 и 4.
    # Поломка П14б: одна строка с ворота_с на пачку — 1 ≠ 4, commit-friction завысил бы хук.
    # Поломка П15: цепочка в деревьях помощников — sha ветки 3/4 ≠ orig, в ней коммит ветки 2.
    print("П12/П14б/П15) пачка из четырёх, красная вторая")
    s = Stand(tmp / "П12")
    trees = [s.tree(x) for x in ("p12a", "p12b", "p12c", "p12d")]
    tips = [s.change(trees[0][1], "a.txt", "a-p12\n"), s.change(trees[1][1], "GATE_RED", "красная\n"),
            s.change(trees[2][1], "b.txt", "b-p12\n"), s.change(trees[3][1], "c.txt", "c-p12\n")]
    mark = pause_mark(s, "перед_забором")
    procs = []
    queued = True
    with HeldLock(s):
        for branch, _ in trees:
            procs.append((branch, spawn(s, branch, "--json", MQ_TEST_PAUSE="перед_забором:3")))
            want = len(procs)
            queued = queued and wait_for(lambda: len(mq_names(s, "заявки")) >= want, QUEUED_WAIT)
        check("П12: четыре заявки легли, пока замок у пробы", True, queued)
    alive = tuple(p for _, p in procs)
    first_pause = wait_for(mark.exists, MARK_WAIT, alive)
    if first_pause:
        mark.unlink()
    second_pause = first_pause and wait_for(mark.exists, MARK_WAIT, alive)
    check("П15: слиятель встал перед забором второй пачки", True, second_pause)
    if second_pause:
        check("П15: на метке ветки 3 и 4 — на своём orig_sha", tips[2:],
              [s.sha(trees[2][0]), s.sha(trees[3][0])])
        check("П15: в ветках 3 и 4 нет коммита ветки 2", [False, False],
              [subprocess.run(["git", "-C", str(s.proj), "merge-base", "--is-ancestor", tips[1],
                               trees[k][0]], capture_output=True).returncode == 0 for k in (2, 3)])
    res = {branch: collect(proc) for branch, proc in procs}
    check("П12: коды 0, 3, 0, 0", [0, 3, 0, 0], [res[b][0] for b, _ in trees])
    check("П12: возврат 3 и 4 — строки mq.возврат", [1, 1],
          [len(rows_of(s, trees[k][0], "mq.возврат")) for k in (2, 3)])
    check("П12: блоков 5 (4 бисекция + 1 хвост)", 5, len(s.gate_blocks()))
    guilty = rows_of(s, trees[1][0])
    batch_id = ((guilty[-1].get("data") or {}).get("пачка") or {}).get("id") if guilty else None
    # Маскировщик журнала скрывал «<ns>-<pid>.json» при семизначном pid — все пачки сливались в одну.
    check("П14б: id пачки в журнале не скрыт маскировщиком", True,
          bool(batch_id) and "СКРЫТО" not in str(batch_id))
    numbered = [(r.get("data") or {}).get("ветка") for r in s.rows()
                if ((r.get("data") or {}).get("пачка") or {}).get("id") == batch_id
                and isinstance((r.get("data") or {}).get("ворота_с"), (int, float))]
    check("П14б: строк первой пачки с числом в ворота_с — 4, по одной у веток 1–4",
          sorted(b for b, _ in trees), sorted(numbered) if batch_id else ["нет пачки"])


def case_p13_orphan_live_pid(tmp: Path) -> None:
    """Всё во взяты/ при взятии замка — сироты убитого слиятеля, даже если pid жив (pid
    переиспользуется): иначе заявка висит вечно."""
    # Поломка: проверка os.kill(pid, 0) — сирота с живым pid не возвращается и не сливается.
    print("П13) сирота с живым pid возвращена и слита")
    s = Stand(tmp / "П13")
    bx, wx = s.tree("p13x")
    tip = s.change(wx, "a.txt", "a-p13x\n")
    by, wy = s.tree("p13y")
    s.change(wy, "b.txt", "b-p13y\n")
    sleeper = subprocess.Popen(["sleep", "300"])
    try:
        write_request(s, "взяты", bx, wx, pid=sleeper.pid, слиятель=sleeper.pid, orig_sha=tip)
        code, _, _ = collect(spawn(s, by, "--json"))
    finally:
        kill_hard(sleeper)
    check("П13: новая подача — код 0", 0, code)
    check("П13: сирота — итог 0 в итоги/, её правка в master", (0, "a-p13x\n"),
          ((results_by_branch(s).get(bx) or {}).get("код"), s.read("a.txt")))
    check("П13: взяты/ пуст", [], mq_names(s, "взяты"))


def case_p14_gate_seconds_plain(tmp: Path) -> None:
    """Строк пачки с числом в ворота_с столько же, сколько прогонов ворот: commit-friction
    вычитает их из строк pre-commit gates.jsonl."""
    # Поломка: число ворота_с у каждой строки пачки — 2 ≠ 1, хук занижен.
    print("П14) ворота_с без бисекции — одна строка с числом на пачку из двух")
    s = Stand(tmp / "П14")
    trees = [s.tree(x) for x in ("p14a", "p14b")]
    s.change(trees[0][1], "a.txt", "a-p14\n")
    s.change(trees[1][1], "b.txt", "b-p14\n")
    res = submit_batch(s, "П14", [b for b, _ in trees])
    check("П14: коды 0, 0", [0, 0], [res[b][0] for b, _ in trees])
    ids = {((res[b][1].get("пачка") or {}).get("id")) for b, _ in trees}
    check("П14: одна пачка на обе", True, len(ids) == 1 and None not in ids)
    numbered = [r for r in s.rows() if isinstance((r.get("data") or {}).get("ворота_с"), (int, float))
                and ((r.get("data") or {}).get("пачка") or {}).get("id") in ids]
    check("П14: строк с числом в ворота_с == блоков (1)", (1, 1), (len(numbered), len(s.gate_blocks())))


def case_p16_return_no_code8(tmp: Path) -> None:
    """Возврат в очередь не даёт код 8: предел меряется от пульса, а не от подачи."""
    # Поломка: предел от подачи — третья (4 прогона > 3 с от подачи) уходит с кодом 8.
    print("П16) вернувшаяся после бисекции не получает код 8")
    s = Stand(tmp / "П16")
    fast_limit(s, "0", "0.025")                     # предел 3 с
    trees = [s.tree(x) for x in ("p16a", "p16b", "p16c")]
    s.change(trees[0][1], "a.txt", "a-p16\n")
    s.change(trees[1][1], "GATE_RED", "красная\n")
    s.change(trees[2][1], "b.txt", "b-p16\n")
    res = submit_batch(s, "П16", [b for b, _ in trees], MQ_STUB_SLEEP="1")
    check("П16: коды 0, 3, 0 (третья не 8)", [0, 3, 0], [res[b][0] for b, _ in trees])
    check("П16: прогонов ворот 4 — дольше предела от подачи", 4, len(s.gate_blocks()))
    # Поломка «предел от подачи» в пачке выше не видна: вернувшаяся сразу берёт свободный замок.
    # Здесь замок держит живой слиятель-проба с движущимся пульсом дольше предела.
    late, wl = s.tree("p16d")
    s.change(wl, "c.txt", "c-p16d\n")
    with HeldLock(s, pulse=True) as held:
        proc = spawn(s, late, "--json")
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and proc.poll() is None:
            held.beat()
            time.sleep(0.5)
    check("П16: ждала 6 с при живом пульсе (предел 3 с) — код 0, не 8", 0, collect(proc)[0])


def case_p17_release_after_own(tmp: Path) -> None:
    """Слиятель отпускает замок после пачки со своей заявкой: его вызывающий не простаивает,
    publish и memory-commit не голодают."""
    # Поломка: держание до пустой очереди — A выходит после итога B.
    print("П17) слиятель уходит после своей пачки, не дожидаясь чужих")
    s = Stand(tmp / "П17")
    trees = [s.tree(x) for x in ("p17a", "p17b", "p17c")]
    for (branch, wt), rel in zip(trees, ("a.txt", "b.txt", "c.txt")):
        s.change(wt, rel, f"{rel}-{branch}\n")
    lead = spawn(s, trees[0][0], "--json", MQ_STUB_SLEEP="3")
    check("П17: A в воротах", True, wait_for(lambda: s.gate_blocks(), MARK_WAIT, (lead,)))
    rest = []
    queued = True
    for branch, _ in trees[1:]:
        rest.append(spawn(s, branch, "--json", MQ_STUB_SLEEP="3"))
        want = len(rest)
        queued = queued and wait_for(lambda: len(mq_names(s, "заявки")) >= want, QUEUED_WAIT)
    check("П17: B и C встали в очередь во время ворот A", True, queued)
    code_a, _, _ = collect(lead)
    check("П17: A вышел (код 0) раньше, чем появился итог B", (0, False),
          (code_a, trees[1][0] in results_by_branch(s)))
    check("П17: B и C — код 0", [0, 0], [collect(p)[0] for p in rest])


def case_p18_killed_after_result(tmp: Path) -> None:
    """Слиятель убит между итогом и снятием взяты/: итог не перезаписан, ветка не прогнана снова."""
    # Поломка: возврат сироты без проверки итога — X прогнан снова, итог перезаписан кодом 2.
    print("П18) слиятель убит после итога")
    s = Stand(tmp / "П18")
    bx, wx = s.tree("p18x")
    s.change(wx, "a.txt", "a-p18x\n")
    by, wy = s.tree("p18y")
    s.change(wy, "b.txt", "b-p18y\n")
    proc = spawn(s, bx, "--json", MQ_TEST_PAUSE="после_итога:30")
    marked = wait_for(pause_mark(s, "после_итога").exists, MARK_WAIT, (proc,))
    check("П18: слиятель встал после итога", True, marked)
    if marked:
        kill_hard(proc)
    else:
        collect(proc)
    blocks = len(s.gate_blocks())
    before = results_by_branch(s).get(bx)
    code_y, _, _ = collect(spawn(s, by, "--json"))
    check("П18: Y — код 0", 0, code_y)
    check("П18: итог X — 0 и не перезаписан", (0, before), ((before or {}).get("код"),
                                                           results_by_branch(s).get(bx)))
    check("П18: X не прогнан снова (блоков +1 от Y)", 1, len(s.gate_blocks()) - blocks)
    check("П18: дверь пишет строку mq.пауза в журнал", True,
          any(r.get("event") == "mq.пауза" for r in s.rows()))


def case_p19_killed_after_ff(tmp: Path) -> None:
    """Слиятель убит после ff, до итогов: ветка уже в master — итог 0 без ворот, ветка
    переставлена (предок master), иначе её дерево не уберётся никогда (ревью-2 В2)."""
    # Поломка: нет правила «уже в master» — лишний блок ворот или код 1; пустой вклад без
    # reset --keep — ветка X не предок master.
    print("П19) слиятель убит после ff")
    s = Stand(tmp / "П19")
    bx, wx = s.tree("p19x")
    s.change(wx, "a.txt", "a-p19x\n", "правка X п19")
    s.change(s.proj, "h.txt", "h-master\n", "master ушёл п19")   # rebase X даст новый sha
    by, wy = s.tree("p19y")
    s.change(wy, "b.txt", "b-p19y\n")
    proc = spawn(s, bx, "--json", MQ_TEST_PAUSE="после_ff:30")
    marked = wait_for(pause_mark(s, "после_ff").exists, MARK_WAIT, (proc,))
    check("П19: слиятель встал после ff", True, marked)
    if marked:
        kill_hard(proc)
    else:
        collect(proc)
    blocks = len(s.gate_blocks())
    code_y, _, _ = collect(spawn(s, by, "--json"))
    check("П19: Y — код 0", 0, code_y)
    check("П19: X — итог 0 в итоги/", 0, (results_by_branch(s).get(bx) or {}).get("код"))
    check("П19: X без ворот (блоков +1 от Y)", 1, len(s.gate_blocks()) - blocks)
    check("П19: коммит X в master один раз", 1,
          git(s.proj, "log", "--format=%s", "master").splitlines().count("правка X п19"))
    check("П19: ветка X снята или предок master", True,
          not s.branch_exists(bx) or in_master(s, s.sha(bx)))


def case_p20_killed_submit(tmp: Path) -> None:
    """Убитая подача и повторная: одна заявка, одно слияние; старые итоги убираются."""
    # Поломка: повторная подача пишет вторую заявку — две строки mq.слито; без уборки старый итог
    # лежит вечно.
    print("П20) убитая подача, повторная подача той же ветки, уборка итогов")
    s = Stand(tmp / "П20")
    branch, wt = s.tree("p20")
    s.change(wt, "a.txt", "a-p20\n")
    old = mq_dir(s, "итоги") / "старый.json"
    old.parent.mkdir(parents=True)
    old.write_text('{"отчёт": {}, "вывод": ""}', encoding="utf-8")
    two_days = time.time() - 2 * 86400
    os.utime(old, (two_days, two_days))
    with HeldLock(s):
        first = spawn(s, branch, "--json")
        queued = wait_for(lambda: mq_names(s, "заявки"), QUEUED_WAIT)
        check("П20: первая подача положила заявку", True, queued)
        kill_hard(first)
        again = spawn(s, branch, "--json")
        time.sleep(2)
        check("П20: при повторной подаче заявка одна", 1 if queued else "нет заявки",
              len(mq_names(s, "заявки")) if queued else "нет заявки")
    code, _, _ = collect(again)
    check("П20: повторная — код 0", 0, code)
    check("П20: строк mq.слито по ветке — 1", 1, len(rows_of(s, branch, "mq.слито")))
    check("П20: итог двухсуточной давности снят при взятии замка", False, old.exists())


def case_p21_package_mid_chain(tmp: Path) -> None:
    """Код 7 посреди цепочки: ветка выпадает, вершина цепочки не сдвигается на неё."""
    # Поломка: top сдвинут на ветку с кодом 7 — её файлы в master, sha_до третьей ≠ sha_после первой.
    print("П21) код 7 посреди цепочки")
    s = Stand(tmp / "П21")
    trees = [s.tree(x) for x in ("p21a", "p21b", "p21c")]
    s.change(trees[0][1], "a.txt", "a-p21\n")
    orig = s.change(trees[1][1], "scripts/tool2.py", "вершина\n")
    s.change(trees[2][1], "b.txt", "b-p21\n")
    s.write(s.pkg, "harness/scripts/tool2.py", "третье\n")          # пакет разошёлся
    res = submit_batch(s, "П21", [b for b, _ in trees])
    check("П21: коды 0, 7, 0", [0, 7, 0], [res[b][0] for b, _ in trees])
    check("П21: в master нет правки средней", "второй\n", s.read("scripts/tool2.py"))
    check("П21: ветка средней на orig_sha", orig, s.sha(trees[1][0]))
    first, third = res[trees[0][0]][1], res[trees[2][0]][1]
    check("П21: sha_до третьей == sha_после первой", True,
          bool(first.get("sha_после")) and third.get("sha_до") == first.get("sha_после"))


def case_p27_package_against_master(tmp: Path) -> None:
    """Сверка пакета ветки k опирается на master, не на before_k (ит.3 §4.2 п.5–6): иначе правка
    ветки j<k читается как «пакет удалил строки» — ложный код 7."""
    # Поломка: опора package_plan — before_k; вторая ветка того же файла получила бы код 7.
    print("П27) две ветки правят один файл пакета в одной пачке")
    s = Stand(tmp / "П27")
    text = "один\nдва\nтри\nчетыре\nпять\n"
    s.change(s.proj, "scripts/tool.py", text)
    s.change(s.pkg, "harness/scripts/tool.py", text)
    trees = [s.tree(x) for x in ("p27x", "p27y")]
    s.change(trees[0][1], "scripts/tool.py", text.replace("один", "ОДИН"))
    s.change(trees[1][1], "scripts/tool.py", text.replace("пять", "ПЯТЬ"))
    res = submit_batch(s, "П27", [b for b, _ in trees])
    check("П27: обе — код 0", [0, 0], [res[b][0] for b, _ in trees])
    check("П27: одна пачка на обе", True,
          len({str((res[b][1].get("пачка") or {}).get("ветки")) for b, _ in trees}) == 1
          and bool((res[trees[0][0]][1].get("пачка") or {}).get("ветки")))
    check("П27: в пакете обе правки", text.replace("один", "ОДИН").replace("пять", "ПЯТЬ"),
          s.read("harness/scripts/tool.py", s.pkg))


def case_p22_output_to_submitter(tmp: Path) -> None:
    """Текст для человека (файлы конфликта) приходит той подаче, чья ветка, а не слиятелю;
    в журнал вывод не попадает."""
    # Поломка: глобальный say — имя файла в stderr слиятеля, у подачи пусто.
    print("П22) вывод не-слиятелю")
    s = Stand(tmp / "П22")
    b1, w1 = s.tree("p22a")
    b2, w2 = s.tree("p22b")
    s.change(w1, "a.txt", "a-p22\n")
    s.change(w2, "c.txt", "c-p22\n")
    s.change(s.proj, "c.txt", "c-master\n")
    lead, procs, _ = merger_first(s, "П22", b1, [b2])
    code1, _, err1 = collect(lead)
    code2, _, err2 = collect(procs[0][1])
    check("П22: слиятель 0, вторая 1", (0, 1), (code1, code2))
    check("П22: c.txt в stderr второй, не у слиятеля", (True, False), ("c.txt" in err2, "c.txt" in err1))
    check("П22: в журнале нет поля вывод", [], [r.get("event") for r in s.rows()
                                               if "вывод" in (r.get("data") or {})])


def case_p23_relative_target(tmp: Path) -> None:
    """Подача «.» из своего дерева при чужом слиятеле: цель разбирает подача, не слиятель."""
    # Поломка: в заявку ушла цель как дана — у слиятеля (cwd проект) «.» = проект, код 2.
    print("П23) относительный путь при чужом слиятеле")
    s = Stand(tmp / "П23")
    b1, w1 = s.tree("p23a")
    b2, w2 = s.tree("p23b")
    s.change(w1, "a.txt", "a-p23\n")
    s.change(w2, "b.txt", "b-p23\n")
    lead, procs, bodies = merger_first(s, "П23", b1, [(b2, (".",), w2)], first_cwd=s.proj)
    check("П23: в заявке ветка и абсолютное дерево подачи", (b2, str(w2)),
          (bodies[0].get("ветка"), bodies[0].get("дерево")))
    check("П23: обе — код 0", (0, 0), (collect(lead)[0], collect(procs[0][1])[0]))


def case_p24_no_code3_for_returns(tmp: Path) -> None:
    """Число возвратов на решение не влияет: невиновный хвост не осуждается за чужие пачки."""
    # Поломка: правило «попыток > RETRIES → код 3» оставлено — зелёная ветка получает 3.
    print("П24) заявка с попыток 5 — зелёная, код 0")
    s = Stand(tmp / "П24")
    bx, wx = s.tree("p24x")
    s.change(wx, "a.txt", "a-p24x\n")
    by, wy = s.tree("p24y")
    s.change(wy, "b.txt", "b-p24y\n")
    write_request(s, "заявки", bx, wx, попыток=5)
    code, _, _ = collect(spawn(s, by, "--json"))
    check("П24: подача — код 0", 0, code)
    check("П24: заявка с попыток 5 — итог 0, правка в master", (0, "a-p24x\n"),
          ((results_by_branch(s).get(bx) or {}).get("код"), s.read("a.txt")))


def case_p25_two_live_submits(tmp: Path) -> None:
    """Две живые подачи одной ветки (одна заявка): обе выходят с кодом 8, никто не висит и не
    держит замок вечно (ревью-2 Б1)."""
    # Поломка: «FileNotFoundError при отмене → ждать» — вторая висит до предела пробы.
    print("П25) две живые подачи одной ветки — обе код 8, не висят")
    s = Stand(tmp / "П25")
    fast_limit(s, "0", "0.025")                     # предел 3 с
    branch, wt = s.tree("p25")
    s.change(wt, "a.txt", "a-p25\n")
    with HeldLock(s):
        started = time.monotonic()
        first = spawn(s, branch, "--json")
        queued = wait_for(lambda: mq_names(s, "заявки"), QUEUED_WAIT, (first,))
        second = spawn(s, branch, "--json")
        res = [collect(first, timeout=20), collect(second, timeout=20)]
        spent = time.monotonic() - started
        check("П25: первая положила заявку", True, queued)
        check("П25: обе — код 8", [8, 8], [r[0] for r in res])
        check("П25: обе вышли не позже предела + 2 с (+3 с запаса на запуск)", True, spent <= 8)
        check("П25: одна заявка на две подачи — в отменены/, заявки/ пуст", (1, []),
              (len(mq_names(s, "отменены")), mq_names(s, "заявки")))


def case_p26_orphan_kills_merger(tmp: Path) -> None:
    """Заявка, на которой слиятель умер дважды, получает итог 6 и не роняет каждого следующего
    (ревью-2 Б2): иначе очередь встаёт целиком."""
    # Поломка: без счётчика «сирота» третий слиятель берёт заявку снова — итога 6 нет.
    print("П26) слиятель дважды убит на заявке — итог 6, очередь идёт")
    s = Stand(tmp / "П26")
    trees = {x: s.tree(x) for x in ("p26x", "p26z", "p26w")}
    for x, rel in (("p26x", "a.txt"), ("p26z", "b.txt"), ("p26w", "c.txt")):
        s.change(trees[x][1], rel, f"{rel}-{x}\n")
    mark = pause_mark(s, "после_забора")
    kills = 0
    for x in ("p26x", "p26z"):
        proc = spawn(s, trees[x][0], "--json", MQ_TEST_PAUSE="после_забора:30")
        if wait_for(mark.exists, MARK_WAIT, (proc,)):
            kill_hard(proc)
            mark.unlink()
            kills += 1
        else:
            collect(proc)
    check("П26: слиятель дважды встал после забора и убит", 2, kills)
    code_w, _, _ = collect(spawn(s, trees["p26w"][0], "--json"))
    found = results_by_branch(s)
    check("П26: X — итог 6 без ворот, причина «слиятель умер»", (6, "слиятель умер"),
          ((found.get(trees["p26x"][0]) or {}).get("код"),
           (found.get(trees["p26x"][0]) or {}).get("причина")))
    check("П26: Z — итог 0, W — код 0", (0, 0), ((found.get(trees["p26z"][0]) or {}).get("код"), code_w))
    check("П26: ворота один раз (пачка Z, W)", 1, len(s.gate_blocks()))
    check("П26: правки X нет в master", "a0\n", s.read("a.txt"))


def case_p28_reset_keep_refused(tmp: Path) -> None:
    """Отказ reset --keep в дереве помощника (он правил файл, который приносит цепочка) — работа
    всё равно влита: итог 0, причина в выводе и оставлено, дерево с правкой цело (ит.3 §4.2 п.7)."""
    # Поломка: отказ роняет ветку в код 6, или finish_tree снимает дерево вместе с правкой.
    print("П28) отказ reset --keep — итог 0, дерево и правка целы")
    s = Stand(tmp / "П28")
    branch, wt = s.tree("p28")
    s.change(wt, "a.txt", "a-p28\n")
    # master уходит файлом h.txt: вершина цепочки ≠ orig_sha и h.txt у них разный — незакоммиченная
    # правка h.txt заставит reset --keep отказать (правку a.txt git перенёс бы молча).
    s.change(s.proj, "h.txt", "h-master\n", "master ушёл п28")
    proc = spawn(s, branch, "--json", MQ_TEST_PAUSE="после_ff:3")
    marked = wait_for(pause_mark(s, "после_ff").exists, MARK_WAIT, (proc,))
    check("П28: слиятель встал после ff", True, marked)
    if marked:
        s.write(wt, "h.txt", "правка помощника после подачи\n")
    code, data, err = collect(proc)
    check("П28: код 0 (влито)", 0, code)
    check("П28: «ветка не переставлена» в выводе подачи и в оставлено", (True, True),
          ("ветка не переставлена" in err,
           "ветка не переставлена" in json.dumps(data.get("оставлено"), ensure_ascii=False)))
    check("П28: дерево на месте, правка помощника цела", (True, "правка помощника после подачи\n"),
          (wt.exists(), s.read("h.txt", wt) if (wt / "h.txt").exists() else "нет файла"))


def case_p29_cleanup_spares_queued(tmp: Path) -> None:
    """cleanup не снимает дерево и ветку, чья заявка ещё в очереди: иначе слитая ветка придёт
    к своей очереди без дерева и получит код 2 вместо 0 (ит.3 §4.2 п.2)."""
    # Поломка: cleanup пропускает только ветки пачки — дерево X снято при пачке Y, X код 2.
    print("П29) cleanup не снимает ветку из очереди")
    s = Stand(tmp / "П29")
    by, wy = s.tree("p29y")
    s.change(wy, "b.txt", "b-p29y\n")
    bx, wx = s.tree("p29x")
    s.change(wx, "a.txt", "a-p29x\n")
    git(s.proj, "merge", "-q", "--ff-only", bx)               # X уже в master
    write_request(s, "заявки", by, wy, одна=True)               # впереди: Y пачкой из одной
    mark = pause_mark(s, "перед_забором")
    proc = spawn(s, bx, "--json", MQ_TEST_PAUSE="перед_забором:3")
    first = wait_for(mark.exists, MARK_WAIT, (proc,))
    if first:
        mark.unlink()
    second = first and wait_for(mark.exists, MARK_WAIT, (proc,))
    check("П29: слиятель встал перед забором пачки X (после пачки Y)", True, second)
    if second:
        check("П29: после пачки Y дерево и ветка X на месте", (True, True),
              (wx.exists(), s.branch_exists(bx)))
    code, _, _ = collect(proc)
    check("П29: Y — итог 0, X — код 0 (не 2)", (0, 0),
          ((results_by_branch(s).get(by) or {}).get("код"), code))


def case_p30_foreign_tree_in_request(tmp: Path) -> None:
    """Слиятель сверяет путь дерева из заявки с деревом её ветки: заявку пишет любой, кто
    пишет в LOG_DIR, а в этом пути слиятель зовёт git (ревью кода)."""
    # Поломка: слиятель верит полю «дерево» — заявка X с деревом Y слита кодом 0.
    print("П30) заявка с чужим деревом — код 5, ветка не влита")
    s = Stand(tmp / "П30")
    bx, wx = s.tree("p30x")
    s.change(wx, "a.txt", "a-p30x\n")
    by, wy = s.tree("p30y")
    s.change(wy, "b.txt", "b-p30y\n")
    write_request(s, "заявки", bx, wy)
    code_y, _, _ = collect(spawn(s, by, "--json"))
    check("П30: Y — код 0, X — итог 5, правки X нет в master", (0, 5, "a0\n"),
          (code_y, (results_by_branch(s).get(bx) or {}).get("код"), s.read("a.txt")))


def case_p31_broken_yaml_keeps_package(tmp: Path) -> None:
    """Непарсящийся YAML вершины сверка не судит кодом 7 сама по себе, но правку пакета, которой
    нет в вершине, видит строками (ревью кода): иначе зелёные ворота затёрли бы её."""
    # Поломка: сверка молчит на непарсящемся YAML — правка пакета «другой: 2» затёрта, код 0.
    print("П31) битый YAML вершины при правке пакета — код 7, пакет цел")
    s = Stand(tmp / "П31")
    s.change(s.proj, "scripts/data.yaml", "ключ: 1\n")
    s.change(s.pkg, "harness/scripts/data.yaml", "ключ: 1\nдругой: 2\n")
    branch, wt = s.tree("p31")
    s.change(wt, "scripts/data.yaml", "ключ: [\n")
    code, _, _ = s.queue(branch, "--json")
    check("П31: код 7, пакет не тронут", (7, "ключ: 1\nдругой: 2\n"),
          (code, s.read("harness/scripts/data.yaml", s.pkg)))


def case_p32_in_master_before_clean(tmp: Path) -> None:
    """«Уже в master» судится по orig_sha ДО require_clean (ит.3 §4.2 п.2): слитая ветка, чей
    помощник после слияния начал новую правку, получает итог 0, а не код 5 (мутации ревью)."""
    # Поломка: правила «уже в master» нет — пустой вклад после rebase тоже даёт 0, но только
    # после require_clean: грязное дерево слитой ветки — код 5.
    print("П32) слитая ветка с грязным деревом — итог 0 без ворот")
    s = Stand(tmp / "П32")
    bx, wx = s.tree("p32x")
    s.change(wx, "a.txt", "a-p32x\n")
    git(s.proj, "merge", "-q", "--ff-only", bx)
    s.write(wx, "e.txt", "новая правка помощника\n")
    by, wy = s.tree("p32y")
    s.change(wy, "b.txt", "b-p32y\n")
    write_request(s, "заявки", bx, wx)
    code_y, _, _ = collect(spawn(s, by, "--json"))
    check("П32: Y — код 0, X — итог 0, ворота один раз (только Y)", (0, 0, 1),
          (code_y, (results_by_branch(s).get(bx) or {}).get("код"), len(s.gate_blocks())))


def case_p33_commits_after_claim(tmp: Path) -> None:
    """Помощник коммитит после забора: влито поданное, ветка и дерево не тронуты, новый коммит
    не потерян и не снят вместе с деревом (ит.3 §4.2 п.7; мутации ревью)."""
    # Поломка: reset --keep без проверки HEAD — новый коммит выпал из ветки (И-1); finish_tree
    # без «ветка переставлена» — дерево с неслитым коммитом снято.
    print("П33) коммит помощника после забора — влито поданное, ветка и дерево целы")
    s = Stand(tmp / "П33")
    branch, wt = s.tree("p33")
    s.change(wt, "a.txt", "a-p33\n")
    proc = spawn(s, branch, "--json", MQ_TEST_PAUSE="после_забора:3")
    marked = wait_for(pause_mark(s, "после_забора").exists, MARK_WAIT, (proc,))
    check("П33: слиятель встал после забора", True, marked)
    later = s.change(wt, "b.txt", "b-p33 после подачи\n") if marked else ""
    code, _, err = collect(proc)
    check("П33: код 0, правка a.txt в master, b.txt нет", (0, "a-p33\n", "b0\n"),
          (code, s.read("a.txt"), s.read("b.txt")))
    check("П33: «коммиты после подачи не влиты» в выводе подачи", True,
          "коммиты после подачи не влиты" in err)
    check("П33: дерево на месте, ветка на новом коммите", (True, later),
          (wt.exists(), s.sha(branch) if s.branch_exists(branch) else "нет ветки"))


def case_p34_conflict_first(tmp: Path) -> None:
    """Конфликт первой ветки пачки снимает свой rebase в дереве цепочки: следующая ветка
    ложится на чистое дерево (мутации ревью)."""
    # Поломка: забытый rebase --abort — дерево цепочки в середине rebase, вторая ветка
    # получает ложный код 1 (или 6), хотя с master не конфликтует.
    print("П34) первая ветка пачки конфликтует с master — вторая слита")
    s = Stand(tmp / "П34")
    trees = [s.tree(x) for x in ("p34a", "p34b")]
    s.change(trees[0][1], "c.txt", "c-p34a\n")
    s.change(trees[1][1], "a.txt", "a-p34b\n")
    s.change(s.proj, "c.txt", "c-master\n")
    res = submit_batch(s, "П34", [b for b, _ in trees])
    check("П34: коды 1, 0; правка второй в master", ([1, 0], "a-p34b\n"),
          ([res[b][0] for b, _ in trees], s.read("a.txt")))


def case_p35_map_retry_chain_changed(tmp: Path) -> None:
    """Повтор без ворот (master сдвинут картой) — только для той же цепочки, что судили ворота:
    выпала ветка на повторе — над base стоит другое дерево, его ворота не видели (spec-REVIEW Н1)."""
    # Поломка: цепочка повтора не сверена с судимой — первая ветка конфликтует со сдвигом
    # карты и выпадает, вторая ложится на master одна, а ворота её одну не судили (1 прогон).
    print("П35) сдвиг master картой, на повторе выпала первая ветка — ворота заново")
    s = Stand(tmp / "П35")
    s.change(s.proj, "dev-map.yaml", MAP_TEXT, "карта")
    trees = [s.tree(x) for x in ("p35a", "p35b")]
    s.change(trees[0][1], "dev-map.yaml", MAP_TEXT + "# ветка p35a\n", "карта ветки")
    s.change(trees[1][1], "a.txt", "a-p35b\n")
    res = submit_batch(s, "П35", [b for b, _ in trees], MQ_STUB_MOVE_ONCE=f"{s.proj}:dev-map.yaml")
    check("П35: коды 1, 0", [1, 0], [res[b][0] for b, _ in trees])
    check("П35: ворота прогнаны 2 раза (цепочка повтора другая)", 2, len(s.gate_blocks()))
    check("П35: повторов_без_ворот у второй 0", 0, res[trees[1][0]][1].get("повторов_без_ворот"))


def case_queue_keys_in_sample(tmp: Path) -> None:
    """Новые ключи очереди — в образце harness.conf с комментарием (§4.5), и связь потолка с
    пачкой записана над ключом (ревью-2, спорное 1): иначе она живёт только в спеке."""
    # Поломка: ключей нет в образце — до машин не доходят (deliver_new_keys); потолок без
    # пометки «не меньше BATCH_MAX−1» поднимут вместе с пачкой — включатся одиночки.
    print("К) ключи MERGE_QUEUE_BATCH_MAX=4 и MERGE_QUEUE_BISECT_MAX=3 в образце")
    lines = (HERE.parent / SAMPLE).read_text(encoding="utf-8").splitlines()

    def found(key: str, value: str) -> int:
        return next((i for i, l in enumerate(lines) if re.fullmatch(rf'{key}="?{value}"?', l)), -1)

    def comment_above(index: int) -> str:
        start = index
        while start > 0 and lines[start - 1].startswith("#"):
            start -= 1
        return "\n".join(lines[start:index])

    batch, bisect = found("MERGE_QUEUE_BATCH_MAX", "4"), found("MERGE_QUEUE_BISECT_MAX", "3")
    check("К: оба ключа в образце с умолчаниями 4 и 3", (True, True), (batch > 0, bisect > 0))
    check("К: над ключами комментарий; над потолком — связь с BATCH_MAX", (True, True),
          (batch > 0 and lines[batch - 1].startswith("#"),
           bisect > 0 and lines[bisect - 1].startswith("#") and "BATCH_MAX" in comment_above(bisect)))


def case_conflict_utf8_path(tmp: Path) -> None:
    """П0 (спека §0): пути конфликта — читаемыми в выводе и журнале; список разобран по NUL."""
    # Поломка: git без core.quotepath=off — в выводе «\320\266…» вместо кириллицы; разбор без -z —
    # путь с табуляцией git закавычит («".tasks/x/a\tb.md"»), и строка в конфликт не та.
    print("П0) конфликт в кириллическом пути и в пути с табуляцией")
    s = Stand(tmp / "П0-пути")
    paths = (".tasks/x/журнал.md", ".tasks/x/a\tb.md")
    for rel in paths:
        s.write(s.proj, rel, "начало\n")
    git(s.proj, "add", "--", *paths)
    git(s.proj, "commit", "-qm", "журнал задачи", "--", *paths)
    branch, wt = s.tree("p0")
    for rel in paths:
        s.write(wt, rel, "начало\nветка\n")
        s.write(s.proj, rel, "начало\nmaster\n")
    git(wt, "commit", "-qam", "ветка пишет журнал")
    git(s.proj, "commit", "-qam", "master пишет журнал")
    code, data, out = s.queue(branch, "--json")
    check("П0: код 1", 1, code)
    check("П0: кириллический путь в выводе подачи, восьмеричных escape нет", (True, False),
          (paths[0] in out, "\\3" in out))
    check("П0: конфликт в --json и в журнале — оба пути ровно строками", (sorted(paths), sorted(paths)),
          (sorted(data.get("конфликт") or []), sorted(s.last_data().get("конфликт") or [])))


# Список входов выбора стенда Ф0b (спека §4.0 ветвь 2): без ключа — полный повтор, как сегодня.
REGATE_INPUTS = ("выбор_входы:\n  - scripts/gates.sh\n  - harness/config/mechanisms.yaml\n"
                 "  - harness/config/release-train.yaml\n")
# Вход «проба-вторая» в механизме: сдвиг этим файлом задевает её правилом 2.
SHIFT_INPUT_MECHANISM = ("механизмы:\n- файл: scripts/probe_other.py\n  входы:\n"
                         "  - scripts/shift_input.py\n")
# «проба-вторая» краснеет один раз: метку кладёт проба, проверка её снимает (П28б).
RED_ONCE_PROBE = ("import os\nimport pathlib\nimport sys\n\n"
                  "mark = pathlib.Path(os.environ['LOG_DIR'], 'x-red-once')\n"
                  "if mark.exists():\n    mark.unlink()\n    sys.exit(1)\n")


def regate_stand(tmp: Path, name: str, inputs: str | None = REGATE_INPUTS,
                 files: dict | None = None) -> Stand:
    """Стенд узкого повтора: в master файлы сдвига (`память/x.md`, `scripts/shift_input.py`),
    карта, ключ `выбор_входы:` и свои файлы случая. Файлы под пакетом — и в пакет: иначе правка
    ветки читалась бы как «пакет удалил строки master» (код 7 раньше, чем судит выбор)."""
    s = Stand(tmp / f"узкий-{name}")   # своё имя: «П26», «П27», «П30» у прежних случаев
    rel = "harness/config/release-train.yaml"
    own = {"память/x.md": "# заметки\n", "scripts/shift_input.py": "# вход сдвига\n",
           "dev-map.yaml": MAP_TEXT, **(files or {})}
    if inputs:
        own[rel] = s.read(rel) + inputs
    for path, text in own.items():
        s.write(s.proj, path, text)
    git(s.proj, "add", "--", *own)
    git(s.proj, "commit", "-qm", "стенд узкого повтора", "--", *own)
    carried = [p for p in own if p.startswith("scripts/")] + ["harness/config/mechanisms.yaml"]
    for path in carried:
        s.write(s.pkg, path if path.startswith("harness/") else "harness/" + path, s.read(path))
    git(s.pkg, "add", "-A")
    git(s.pkg, "commit", "-qm", "стенд узкого повтора")
    return s


def selection_calls(s: Stand) -> list:
    return [line for line in s.gates_sh_log() if line.startswith("только=")]


def branch_probe_node(s: Stand, name: str) -> tuple[str, Path]:
    """Ветка правит файл «проба-узла»: выбор первого круга непуст."""
    branch, wt = s.tree(name)
    s.change(wt, "scripts/probe_node.py", s.read("scripts/probe_node.py") + "# ветка\n")
    return branch, wt


def case_regate_outside(tmp: Path) -> None:
    """П26: сдвиг master вне входов проверок — повтор только pre-commit, выбор пуст."""
    # Поломка: полный повтор — две строки «только=»; узкий без pre-commit — один блок; счётчик
    # повторов_без_выбора или повторы_с не пишется — поля нет. Сон 1 с у pre-commit: время
    # повтора не ноль, повторы_с ≥ 1 (м7 ревью-3).
    print("П26) узкий повтор: сдвиг master вне входов — выбор не гонится")
    s = regate_stand(tmp, "П26")
    branch, _ = branch_probe_node(s, "r26")
    code, data, _ = s.queue(branch, "--json", MQ_STUB_SLEEP="1",
                            MQ_STUB_MOVE_ONCE=f"{s.proj}:память/x.md")
    check("П26: код 0, повторов 1", (0, 1), (code, data.get("повторов")))
    check("П26: выбор гонялся один раз (первый круг)", ["только=проба-узла"], selection_calls(s))
    check("П26: pre-commit — на обоих кругах", 2, len(s.gate_blocks()))
    check("П26: повторов_без_выбора 1", 1, data.get("повторов_без_выбора"))
    check("П26: итог несёт выбор ветки, узкий — свой пустой (м8 ревью-3)", (["проба-узла"], []),
          (data.get("выбор"), (data.get("узкий") or {}).get("выбор")))
    seconds = data.get("повторы_с")
    check("П26: повторы_с — число ≥ 1 (сон pre-commit на повторе)", True,
          isinstance(seconds, (int, float)) and seconds >= 1)


def case_regate_tip_rules(tmp: Path) -> None:
    """П27: сдвиг задел проверку по правилам из дерева вершины — узкий выбор её берёт."""
    # Поломка: диапазон base..tip — «только=проба-узла»; правила из master — входа нет, строки нет;
    # полный повтор с объединением — «проба-узла|проба-вторая».
    print("П27) узкий повтор: выбор по сдвигу, правила из дерева вершины")
    s = regate_stand(tmp, "П27")
    branch, wt = branch_probe_node(s, "r27")
    s.change(wt, "harness/config/mechanisms.yaml", SHIFT_INPUT_MECHANISM, "вход проба-вторая")
    code, _, _ = s.queue(branch, "--json", MQ_STUB_MOVE_ONCE=f"{s.proj}:scripts/shift_input.py")
    check("П27: код 0", 0, code)
    check("П27: второй выбор — ровно проба-вторая", ["только=проба-узла", "только=проба-вторая"],
          selection_calls(s))


def case_regate_chain_changed(tmp: Path) -> None:
    """П28(а), проба сохранности: на повторе выпала ветка — полный круг без диапазона сдвига."""
    # Поломка: узкий повтор при иной цепочке или полный с prev_tip..tip — в выбор второго круга
    # входит проба-вторая (файл выпавшей ветки и сдвига).
    print("П28а) цепочка изменилась на повторе — выбор только оставшейся ветки")
    s = regate_stand(tmp, "П28а")
    set_conf(s, MERGE_QUEUE_BATCH_MAX="2")
    first, _ = branch_probe_node(s, "r28a1")
    second, wt2 = s.tree("r28a2")
    s.change(wt2, "scripts/probe_other.py", s.read("scripts/probe_other.py") + "# ветка\n")
    res = submit_batch(s, "П28а", [first, second],
                       MQ_STUB_MOVE_ONCE=f"{s.proj}:scripts/probe_other.py")
    check("П28а: коды 0, 1", [0, 1], [res[first][0], res[second][0]])
    check("П28а: второй выбор — только выбор оставшейся ветки",
          ["только=проба-узла|проба-вторая", "только=проба-узла"], selection_calls(s))


def case_regate_red_once(tmp: Path) -> None:
    """П28(б): узкий красный — полный круг на том же base, повтор не тратится, поля чисты."""
    # Поломка: красный узкий как итог — код 3; узкий красный тратит повтор — повторов 2; поля
    # причина/красные остались от узкого — «красные» при коде 0; поля узкий нет.
    print("П28б) узкий повтор красный один раз — полный круг зелёный")
    s = regate_stand(tmp, "П28б", files={"scripts/probe_other.py": RED_ONCE_PROBE})
    branch, _ = branch_probe_node(s, "r28b")
    (s.logs / "x-red-once").touch()
    code, data, _ = s.queue(branch, "--json", MQ_STUB_MOVE_ONCE=f"{s.proj}:scripts/probe_other.py")
    check("П28б: код 0, повторов 1", (0, 1), (code, data.get("повторов")))
    # Красный узкий — красный выбор: М4 один раз гонит его на master (узнать, красна ли и там).
    check("П28б: выбор — первый круг, узкий, узнавание на master, полный с объединением",
          ["только=проба-узла", "только=проба-вторая", "только=проба-вторая",
           "только=проба-узла|проба-вторая"],
          selection_calls(s))
    check("П28б: узкий.красные — [проба-вторая]", ["проба-вторая"],
          (data.get("узкий") or {}).get("красные"))
    check("П28б: причина и красные итога пусты", (None, None),
          (data.get("причина") or None, data.get("красные") or None))


def case_regate_map_only(tmp: Path) -> None:
    """П28(в), проба сохранности: сдвиг только картой — ворот нет, как сегодня (Р3)."""
    # Поломка: узкий повтор и на сдвиге картой — второй блок pre-commit.
    print("П28в) сдвиг только картой при ключе выбор_входы — ворот нет")
    s = regate_stand(tmp, "П28в")
    branch, _ = branch_probe_node(s, "r28c")
    code, data, _ = s.queue(branch, "--json", MQ_STUB_MOVE_ONCE=f"{s.proj}:dev-map.yaml")
    check("П28в: код 0, блоков pre-commit 1, выбор 1 раз", (0, 1, 1),
          (code, len(s.gate_blocks()), len(selection_calls(s))))
    check("П28в: повторов_без_ворот 1", 1, data.get("повторов_без_ворот"))


def case_regate_shift_red(tmp: Path) -> None:
    """П28(г): сдвиг ломает проверку, которой ветка не трогала — полный круг её не теряет."""
    # Поломка: полный круг только по base..tip — проба-вторая не гонится, код 0, слито.
    print("П28г) сдвиг ломает проба-вторая — и узкий, и полный круг её гонят, код 3")
    s = regate_stand(tmp, "П28г")
    branch, _ = branch_probe_node(s, "r28d")
    code, _, _ = s.queue(branch, "--json", MQ_STUB_MOVE_ONCE=f"{s.proj}:scripts/probe_other.py",
                         MQ_STUB_MOVE_TEXT="raise SystemExit(1)")
    check("П28г: код 3, правки ветки в master нет", (3, False),
          (code, s.read("scripts/probe_node.py").endswith("# ветка\n")))
    # Третий прогон — узнавание М4 на master после красного узкого; полный круг его не повторяет.
    check("П28г: проба-вторая — в узком, узнавании на master и полном прогоне", 3,
          s.gates_sh_log().count("проверка=проба-вторая"))


def case_regate_race(tmp: Path) -> None:
    """П28(д): решение о повторе — по разности вершин после build_chain, а не в момент отказа ff."""
    # Поломка: «только картой» по base..master до паузы — ворот нет, проба-вторая не гонится; точки
    # паузы нет — метка не появляется. Пауза 15 с и повторов 1 (м2 ревью-3): опоздание стенда
    # дало бы третий круг — громко, а не тихо.
    print("П28д) после отказа ff master сдвинут входом — второй круг его судит")
    s = regate_stand(tmp, "П28д", files={"harness/config/mechanisms.yaml": SHIFT_INPUT_MECHANISM})
    branch, _ = branch_probe_node(s, "r28e")
    proc = spawn(s, branch, "--json", MQ_TEST_PAUSE="после_отказа_ff:15",
                 MQ_STUB_MOVE_ONCE=f"{s.proj}:dev-map.yaml")
    marked = wait_for(pause_mark(s, "после_отказа_ff").exists, MARK_WAIT, (proc,))
    if marked:
        s.change(s.proj, "scripts/shift_input.py", s.read("scripts/shift_input.py") + "# гонка\n",
                 "сдвиг входом во время паузы")
    code, data, _ = collect(proc)
    check("П28д: слиятель встал после отказа ff", True, marked)
    check("П28д: код 0, повторов 1", (0, 1), (code, data.get("повторов")))
    check("П28д: второй круг гонит проба-вторая", True,
          "проверка=проба-вторая" in s.gates_sh_log())


# Новая проверка сдвига: упоминает файл ветки — выбрать её может только выбор ветки.
ZET_CHECK = "проверка \"проба-зет\" bash -c 'echo scripts/tool2.py; exit 0'\n"


def case_regate_rules_moved(tmp: Path) -> None:
    """П30: сдвиг меняет правила выбора (gates.sh) — полный круг; без ключа — тоже полный."""
    # Поломка: выбор только по файлам сдвига — gates.sh ни у кого не вход, tool2.py не в сдвиге,
    # проба-зет не гонится; пустой список как «правила не задеты» — второй стенд узкий.
    # Красная до кода: точки паузы после_отказа_ff нет (м1 ревью-3).
    print("П30) сдвиг вписал проверку в gates.sh — второй круг её гонит")
    s = regate_stand(tmp, "П30", inputs="выбор_входы: [scripts/gates.sh]\n")
    branch, wt = s.tree("r30")
    s.change(wt, "scripts/tool2.py", "второй, ветка\n")
    proc = spawn(s, branch, "--json", MQ_TEST_PAUSE="после_отказа_ff:15",
                 MQ_STUB_MOVE_ONCE=f"{s.proj}:память/x.md")
    marked = wait_for(pause_mark(s, "после_отказа_ff").exists, MARK_WAIT, (proc,))
    if marked:
        gates = s.read("scripts/gates.sh").replace("exit $red\n", ZET_CHECK + "exit $red\n")
        s.change(s.proj, "scripts/gates.sh", gates, "новая проверка в gates.sh")
    code, _, _ = collect(proc)
    check("П30: слиятель встал после отказа ff", True, marked)
    check("П30: код 0, второй круг гонит проба-зет", (0, True),
          (code, "проверка=проба-зет" in s.gates_sh_log()))
    bare = regate_stand(tmp, "П30б", inputs=None)
    branch, _ = branch_probe_node(bare, "r30b")
    code, _, _ = bare.queue(branch, "--json", MQ_STUB_MOVE_ONCE=f"{bare.proj}:память/x.md")
    check("П30б: без ключа выбор_входы — второй круг полный", (0, ["только=проба-узла"] * 2),
          (code, selection_calls(bare)))


def case_regate_rules_list_shrunk(tmp: Path) -> None:
    """П30в (м4 ревью-3): сдвиг вынес gates.sh из списка и тут же вписал проверку — полный круг."""
    # Поломка: список только из base — там gates.sh уже нет, сдвиг судится узко, проба-зет
    # (её выбирает лишь файл ветки) не гонится. Список прошлой вершины его ещё несёт.
    print("П30в) сдвиг вынес gates.sh из выбор_входы и поменял gates.sh — второй круг полный")
    s = regate_stand(tmp, "П30в", inputs="выбор_входы: [scripts/gates.sh]\n")
    branch, wt = s.tree("r30c")
    s.change(wt, "scripts/tool2.py", "второй, ветка\n")
    proc = spawn(s, branch, "--json", MQ_TEST_PAUSE="после_отказа_ff:15",
                 MQ_STUB_MOVE_ONCE=f"{s.proj}:память/x.md")
    marked = wait_for(pause_mark(s, "после_отказа_ff").exists, MARK_WAIT, (proc,))
    if marked:
        rel = "harness/config/release-train.yaml"
        s.change(s.proj, rel, s.read(rel).replace("выбор_входы: [scripts/gates.sh]\n",
                                                  "выбор_входы: [harness/config/mechanisms.yaml]\n"),
                 "gates.sh вынесен из выбор_входы")
        gates = s.read("scripts/gates.sh").replace("exit $red\n", ZET_CHECK + "exit $red\n")
        s.change(s.proj, "scripts/gates.sh", gates, "новая проверка в gates.sh")
    code, _, _ = collect(proc)
    check("П30в: слиятель встал после отказа ff", True, marked)
    check("П30в: код 0, второй круг гонит проба-зет", (0, True),
          (code, "проверка=проба-зет" in s.gates_sh_log()))


def case_regate_no_selection(tmp: Path) -> None:
    """П28е: узкий выбор не получен (сдвиг сломал mechanisms.yaml вне списка) — не слито."""
    # Поломка: «нет выбора» узкого как зелёный — круг из одного pre-commit, ветка слита без выбора.
    print("П28е) сдвиг ломает mechanisms.yaml вне выбор_входы — узкий без выбора не сливает")
    s = regate_stand(tmp, "П28е", inputs="выбор_входы: [scripts/gates.sh]\n")
    branch, _ = branch_probe_node(s, "r28f")
    code, data, _ = s.queue(branch, "--json", MQ_STUB_MOVE_TEXT="механизмы: [",
                            MQ_STUB_MOVE_ONCE=f"{s.proj}:harness/config/mechanisms.yaml")
    check("П28е: не слито — код 3, правки ветки в master нет", (3, False),
          (code, s.read("scripts/probe_node.py").endswith("# ветка\n")))
    check("П28е: узкий — ступень «выбор»", "выбор", (data.get("узкий") or {}).get("ступень"))


def case_red_names(tmp: Path) -> None:
    """Код 3 несёт имена красных: повтор подачи без чтения gates-run-*.out."""
    # БОЛЬНОЙ СЛУЧАЙ 21.11.2026: 26 подач с кодом 3 за сутки, 8,3 ч стены ворот; запись очереди
    # говорила только «ворота красные», и каждый повтор начинался с поиска красного в выводе.
    # Поломка: поля нет или оно пусто; берётся одна форма строк (ПЛОХО без GATE FAIL — stderr);
    # строка общего предела не названа; повтор соло («— красная и соло») — второе имя.
    print("кр) код 3 — поле «красные» в --json и в журнале")
    s = Stand(tmp / "кр")
    branch, wt = s.tree("r")
    s.change(wt, "a.txt", "a1\n")
    master = s.sha()
    code, data, _ = s.queue(branch, "--json", MQ_STUB_RED_LINES="1")
    check("кр: код 3, master на месте", (3, master), (code, s.sha()))

    def verdict(red) -> str:
        if not isinstance(red, list):
            return f"не список: {red!r}"
        named = [n for n in red if "предел" not in str(n)]
        limit = [n for n in red if "предел" in str(n)]
        return "ок" if (named == ["проба-первая", "проба-вторая"] and len(limit) == 1) else repr(red)

    check("кр: --json «красные» — проба-первая (один раз), проба-вторая, общий предел", "ок",
          verdict(data.get("красные")))
    check("кр: журнал merge-queue.jsonl несёт те же «красные»", "ок",
          verdict(s.last_data().get("красные")))


def no_recheck_log(s: Stand) -> list:
    """Строки заглушки gates.sh «без_соло=<имена>|узел=<первая строка node_lib.py>» по вызову."""
    path = s.logs / "stub-no-recheck.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def master_memory(s: Stand) -> dict:
    path = s.logs / "mq-master-red.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def case_master_red(tmp: Path) -> None:
    """М4-1: красная на самом master — соло-повтор не гонится; итог помнится по sha master."""
    # БОЛЬНОЙ СЛУЧАЙ 21.11.2026: с 14:47 master красен по 5 проверкам, каждая подача гнала общий
    # прогон и соло-повтор КАЖДОЙ красной — выбор_с 3286 с на подачу (mq.ворота 21:46).
    # Поломка: очередь не узнаёт красную master — вторая подача снова «без_соло=» пусто; узнаёт
    # на дереве ветки, а не master (узел=# правка); узнаёт каждый раз (лишний вызов gates.sh);
    # память не снимается, когда master тронул файлы проверки (узнавания после сдвига нет);
    # снимается на любом сдвиге master (правка документа — узнавание снова).
    print("М4-1) красная на master — соло не гонится, итог по sha master")
    s = Stand(tmp / "м41")

    def master_node(text: str, message: str) -> None:
        # node_lib.py лежит и в пакете стенда: правка master без пакета — код 7 до ворот.
        s.change(s.proj, "scripts/node_lib.py", text, message)
        s.change(s.pkg, "harness/scripts/node_lib.py", text, message)

    master_node("def verdict():\n    return 1\n", "master красный")
    branch1, wt1 = s.tree("m1")
    s.change(wt1, "scripts/node_lib.py", "# правка 1\ndef verdict():\n    return 1\n")
    code1, data1, _ = s.queue(branch1, "--json")
    master = s.sha()
    check("М4-1: первая подача — код 3", 3, code1)
    check("М4-1: ветка с соло, затем узнавание на дереве master без соло",
          ["без_соло=|узел=# правка 1", "без_соло=проба-узла|узел=def verdict():"], no_recheck_log(s))
    check("М4-1: память — проба-узла красна на sha master",
          {"проба-узла": {"sha": master, "красна": True}}, master_memory(s))
    check("М4-1: запись несёт «красны_на_master» [проба-узла]", (["проба-узла"], ["проба-узла"]),
          (data1.get("красны_на_master"), s.last_data().get("красны_на_master")))

    branch2, wt2 = s.tree("m2")
    s.change(wt2, "scripts/node_lib.py", "# правка 2\ndef verdict():\n    return 1\n")
    code2, data2, _ = s.queue(branch2, "--json")
    check("М4-1: вторая подача на том же master — код 3, один вызов gates.sh без соло для проба-узла",
          (3, ["без_соло=проба-узла|узел=# правка 2"]), (code2, no_recheck_log(s)[2:]))
    check("М4-1: вторая подача — «красны_на_master» [проба-узла]", ["проба-узла"],
          data2.get("красны_на_master"))

    s.change(s.proj, "docs/заметка.md", "сдвиг master документом\n", "документ")
    branch3, wt3 = s.tree("m3")
    s.change(wt3, "scripts/node_lib.py", "# правка 3\ndef verdict():\n    return 1\n")
    s.queue(branch3, "--json")
    check("М4-1: master сдвинут документом — память жива, узнавания нет",
          ["без_соло=проба-узла|узел=# правка 3"], no_recheck_log(s)[3:])

    master_node("# master\ndef verdict():\n    return 1\n", "узел в master")
    branch4, wt4 = s.tree("m4")
    s.change(wt4, "scripts/node_lib.py", "# правка 4\ndef verdict():\n    return 1\n")
    s.queue(branch4, "--json")
    check("М4-1: master тронул файл проверки — память снята, ветка с соло и узнавание заново",
          ["без_соло=|узел=# правка 4", "без_соло=проба-узла|узел=# master"], no_recheck_log(s)[4:])


def case_master_green_remembered(tmp: Path) -> None:
    """М4-1: зелёная на master тоже помнится — красная ветки не гонит master второй раз."""
    # Поломка: память пишет только красные — каждая красная подача при зелёном master платит
    # лишний прогон на master; зелёная записана красной — соло ветки снят на ветке-виновнице.
    print("М4-1з) зелёная на master помнится, соло ветки остаётся")
    s = Stand(tmp / "м41з")
    for name in ("z1", "z2"):
        branch, wt = s.tree(name)
        s.change(wt, "scripts/node_lib.py", f"# {name}\ndef verdict():\n    return 1\n")
        code, data, _ = s.queue(branch, "--json")
        check(f"М4-1з: {name} — код 3, «красны_на_master» []", (3, []), (code, data.get("красны_на_master")))
    # Master гонится без соло (спека шаг2 § 5а.1 п.3), как в М4-1.
    check("М4-1з: обе ветки с соло, master гонялся один раз без соло",
          ["без_соло=|узел=# z1", "без_соло=проба-узла|узел=def verdict():", "без_соло=|узел=# z2"],
          no_recheck_log(s))
    check("М4-1з: память — проба-узла зелёна на master",
          {"проба-узла": {"sha": s.sha(), "красна": False}}, master_memory(s))


def case_queue_own_files(tmp: Path) -> None:
    """М4-2: проверка из «очередь_без_входов» выбирается своими файлами, не входами механизма."""
    # Улика 21.11.2026: «обновление» и «население кода» — входы механизма scripts/** (кэш прав:
    # им нужна вся раскладка), и выбор брал их на каждую подачу, 950–2100 с дважды.
    # Поломка: очередь зовёт выбор без --очередь; ключ не читается; правило снимает и свой файл.
    print("М4-2) входы механизма не выбирают проверку из «очередь_без_входов»")
    mech = ("механизмы:\n  - имя: вторая\n    файл: scripts/probe_other.py\n"
            "    входы: ['scripts/**']\n")
    control = Stand(tmp / "м42к")
    control.change(control.proj, "harness/config/mechanisms.yaml", mech)
    branch, wt = control.tree("k")
    control.change(wt, "scripts/tool.py", "правка\n")
    _, data, _ = control.queue(branch, "--json")
    check("М4-2: контроль — без ключа входы механизма выбирают проба-вторая", ["проба-вторая"],
          data.get("выбор"))

    s = Stand(tmp / "м42")
    s.change(s.proj, "harness/config/mechanisms.yaml", mech)
    s.change(s.proj, "harness/config/release-train.yaml",
             s.read("harness/config/release-train.yaml") + "очередь_без_входов: [проба-вторая]\n")
    branch1, wt1 = s.tree("q1")
    tip = s.change(wt1, "scripts/tool.py", "правка\n")
    code1, data1, _ = s.queue(branch1, "--json")
    check("М4-2: правка чужого скрипта — выбор пуст, влито", ([], 0, tip), (data1.get("выбор"), code1, s.sha()))
    branch2, wt2 = s.tree("q2")
    s.change(wt2, "scripts/probe_other.py", "print('вторая, правка')\n")
    _, data2, _ = s.queue(branch2, "--json")
    check("М4-2: правка своего файла проверки — проба-вторая выбрана", ["проба-вторая"], data2.get("выбор"))


# ───────────── lq1: отказ не по вине ветки ─────────────
# Красные до кода: очередь не пересобирает собранные документы при конфликте и не держит заявку с кодом 3 по вине master.

BUILT_DOC = "docs/список.md"
BUILD_STUB = "scripts/build_stub.py"
# Генератор стенда: документ из data/*.txt; --проверить — совпадает ли; каждый зов — в журнал.
BUILD_STUB_TEXT = """import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
with open(os.path.join(os.environ["LOG_DIR"], "stub-build.log"), "a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
text = "".join(f"{p.stem}: {p.read_text(encoding='utf-8').strip()}\\n"
               for p in sorted((root / "data").glob("*.txt")))
target = root / "docs" / "список.md"
if "--проверить" in sys.argv[1:]:
    sys.exit(0 if target.exists() and target.read_text(encoding="utf-8") == text else 1)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(text, encoding="utf-8")
"""
# Список «файл → генератор» — данные реестра (поле собирает:, как у update.py), не код очереди.
BUILT_MECHANISMS = f"механизмы:\n- файл: {BUILD_STUB}\n  собирает: {BUILT_DOC}\n"
HELD = "держатся"
RED_NODE = "def verdict():\n    return 1\n"


def commit_files(root: Path, files: dict, message: str) -> str:
    """Несколько файлов одним коммитом: собранный документ едет вместе со своим источником."""
    for rel, text in files.items():
        Stand.write(root, rel, text)
    git(root, "add", "--", *files)
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD").strip()


def built_doc(a: str, b: str) -> str:
    return f"a: {a}\nb: {b}\n"


def built_stand(tmp: Path, name: str, plain_conflict: bool, generator: str = BUILD_STUB_TEXT) -> tuple:
    """Ветка двумя коммитами правит data/a.txt и пересобранный документ, master — data/b.txt и
    документ: смежные строки документа конфликтуют на КАЖДОМ коммите rebase. plain_conflict —
    ветка и master ещё и правят обычный c.txt по-разному. generator — текст генератора стенда.
    → (стенд, ветка, вершина ветки)."""
    s = Stand(tmp / name)
    commit_files(s.proj, {BUILD_STUB: generator, "harness/config/mechanisms.yaml": BUILT_MECHANISMS,
                          "data/a.txt": "a0\n", "data/b.txt": "b0\n", BUILT_DOC: built_doc("a0", "b0")},
                 "собранный документ стенда")
    commit_files(s.pkg, {"harness/" + BUILD_STUB: generator}, "генератор стенда")
    branch, wt = s.tree("built")
    first = {"data/a.txt": "a1\n", BUILT_DOC: built_doc("a1", "b0")}
    if plain_conflict:
        first["c.txt"] = "помощник\n"
    commit_files(wt, first, "ветка: a1")
    tip = commit_files(wt, {"data/a.txt": "a2\n", BUILT_DOC: built_doc("a2", "b0")}, "ветка: a2")
    master_side = {"data/b.txt": "b1\n", BUILT_DOC: built_doc("a0", "b1")}
    if plain_conflict:
        master_side["c.txt"] = "master\n"
    commit_files(s.proj, master_side, "master: b1")
    return s, branch, tip


def case_lq1_built_conflict_rebuilt(tmp: Path) -> None:
    """lq1-а: конфликт только в собранном документе — очередь пересобирает его генератором на
    каждом коммите rebase и сливает."""
    # Поломка: код 1 как сегодня; взята одна сторона без пересборки (в документе нет строки
    # второй стороны); пересобран только последний коммит (у первого документ разошёлся с
    # данными); генератор не из данных реестра (собирает:), а из списка в коде.
    print("lq1-а) конфликт только в собранном документе — пересборка, слито")
    s, branch, _ = built_stand(tmp, "lq1а", plain_conflict=False)
    code, data, _ = s.queue(branch, "--json")
    check("lq1-а: код 0", 0, code)
    check("lq1-а: документ master равен выводу генератора на обеих сторонах", built_doc("a2", "b1"),
          s.read(BUILT_DOC))
    first = git(s.proj, "log", "--format=%H", "--grep=ветка: a1", "master").strip()
    check("lq1-а: и на первом коммите ветки документ пересобран", built_doc("a1", "b1"),
          git(s.proj, "show", f"{first}:{BUILT_DOC}", ok=False) if first else "нет коммита")
    proc = subprocess.run([sys.executable, str(s.proj / BUILD_STUB), "--проверить"],
                          env=s.env(), cwd=s.proj, capture_output=True)
    check("lq1-а: генератор --проверить на master = 0", 0, proc.returncode)
    check("lq1-а: отчёт называет пересобранный документ", [BUILT_DOC], data.get("пересобрано"))


def case_lq1_built_and_plain_conflict(tmp: Path) -> None:
    """lq1-б: конфликт в собранном и в обычном файле — прежний код 1. Проба сохранности:
    сегодня зелёная; живость — мутацией «пересобирать, если собранный есть среди конфликтных»."""
    # Поломка: пересборка снимает конфликт собранного и молча берёт сторону обычного файла.
    print("lq1-б) конфликт в собранном и обычном — код 1")
    s, branch, tip = built_stand(tmp, "lq1б", plain_conflict=True)
    master = s.sha()
    code, data, _ = s.queue(branch, "--json")
    check("lq1-б: код 1", 1, code)
    check("lq1-б: конфликт называет оба файла", ["c.txt", BUILT_DOC], sorted(data.get("конфликт") or []))
    check("lq1-б: master и ветка на прежних sha", (master, tip), (s.sha(), s.sha(branch)))
    check("lq1-б: пересборки нет в отчёте", [], data.get("пересобрано") or [])


# Генератор, чей документ расходится с данными: --проверить всегда 1.
BUILD_STUB_CHECK_FAILS = BUILD_STUB_TEXT.replace(
    'sys.exit(0 if target.exists() and target.read_text(encoding="utf-8") == text else 1)', "sys.exit(1)")
# Генератор, который сам оставляет маркер конфликта, а --проверить у него честно 0.
BUILD_STUB_MARKER = BUILD_STUB_TEXT.replace('text = "".join(', 'text = "<<<<<<< стенд\\n" + "".join(')


def case_lq1_bad_generator(tmp: Path) -> None:
    """lq1-а2: генератор отказал проверкой или оставил маркер — прежний код 1, rebase снят."""
    # Поломка: --проверить не зовётся (документ, расходящийся с данными, влит); маркер
    # «<<<<<<<» в пересобранном документе не ищется (маркер влит в master).
    print("lq1-а2) генератор: --проверить = 1 или маркер в документе — код 1")
    for label, generator in (("проверить", BUILD_STUB_CHECK_FAILS), ("маркер", BUILD_STUB_MARKER)):
        s, branch, tip = built_stand(tmp, f"lq1а2-{label}", plain_conflict=False, generator=generator)
        master = s.sha()
        code, data, _ = s.queue(branch, "--json")
        check(f"lq1-а2: {label} — код 1, конфликт [документ], пересборки нет", (1, [BUILT_DOC], []),
              (code, data.get("конфликт"), data.get("пересобрано") or []))
        check(f"lq1-а2: {label} — master и ветка на прежних sha", (master, tip), (s.sha(), s.sha(branch)))


def master_change(s: Stand, rel: str, text: str, message: str) -> None:
    """Правка scripts/ в master и в пакете: без пакета подача упала бы кодом 7 до ворот."""
    s.change(s.proj, rel, text, message)
    s.change(s.pkg, "harness/" + rel, text, message)


def held_stand(tmp: Path, name: str, **conf: str) -> tuple:
    """master красен на проба-узла; ветка трогает node_lib.py, не ломая ничего сверх master:
    все красные подачи — «красны_на_master». → (стенд, ветка, дерево, --json подачи, код)."""
    s = Stand(tmp / name)
    if conf:
        set_conf(s, **conf)
    master_change(s, "scripts/node_lib.py", RED_NODE, "master красный")
    branch, wt = s.tree("held")
    s.change(wt, "scripts/node_lib.py", "# правка ветки\n" + RED_NODE)
    code, data, _ = s.queue(branch, "--json")
    return s, branch, wt, data, code


def held_names(s: Stand) -> list:
    return [read_json(mq_dir(s, HELD) / n).get("ветка") for n in mq_names(s, HELD)]


def held_request(s: Stand) -> dict:
    """Единственная держимая заявка стенда ({} — нет ни одной или их несколько)."""
    names = mq_names(s, HELD)
    return read_json(mq_dir(s, HELD) / names[0]) if len(names) == 1 else {}


def queued_names(s: Stand) -> list:
    return [read_json(mq_dir(s, "заявки") / n).get("ветка") for n in mq_names(s, "заявки")]


# Починка проба-узла: проба зовёт узел, но итог — 0. Правит файл пробы, не node_lib.py ветки.
FIXED_PROBE = GRAPH_FILES["scripts/probe_node.py"].replace(
    "sys.exit(node_lib.verdict())", "node_lib.verdict()\nsys.exit(0)")


def trigger(s: Stand, name: str) -> int:
    """Подача зелёной ветки (правит a.txt: выбор пуст) — слиятель встаёт и судит держимые."""
    branch, wt = s.tree(name)
    s.change(wt, "a.txt", f"{name}\n")
    return s.queue(branch, "--json")[0]


def case_lq1_blameless_held(tmp: Path) -> None:
    """lq1-в: код 3, все красные красны и на master, — заявка держится, одна строка события."""
    # Поломка: заявка теряется (сегодня: итог 3 и всё); держится и при своей красной;
    # событие пишется в канал или дважды.
    print("lq1-в) код 3 только по красной master — заявка держится")
    s, branch, wt, data, code = held_stand(tmp, "lq1в")
    check("lq1-в: код 3, красны_на_master [проба-узла]", (3, ["проба-узла"]),
          (code, data.get("красны_на_master")))
    check("lq1-в: отчёт — держится", True, data.get("держится"))
    check("lq1-в: заявка ветки в хранилище держимых", [branch], held_names(s))
    check("lq1-в: одна строка mq.держится в журнале очереди", 1, len(rows_of(s, branch, "mq.держится")))
    check("lq1-в: ветка не влита, дерево на месте", (False, True), (in_master(s, s.sha(branch)), wt.exists()))


def case_lq1_held_resubmitted(tmp: Path) -> None:
    """lq1-г: master позеленел по этим воротам — держимая заявка подаётся снова сама и сливается."""
    # Поломка: держимая не возвращается в очередь; возвращается, но мимо ворот; ждёт подачи
    # самой ветки, а не следующего слияния.
    print("lq1-г) master позеленел — держимая подаётся снова и сливается")
    s, branch, _, _, _ = held_stand(tmp, "lq1г")
    # Починка трогает файл пробы, а не node_lib.py ветки: без конфликта rebase.
    master_change(s, "scripts/probe_node.py", FIXED_PROBE, "master: проба-узла починена")
    check("lq1-г: подача-триггер слита", 0, trigger(s, "lq1g-trigger"))
    check("lq1-г: правка держимой ветки в master", True,
          s.read("scripts/node_lib.py").startswith("# правка ветки"))
    check("lq1-г: строка mq.слито держимой ветки", 1, len(rows_of(s, branch, "mq.слито")))
    check("lq1-г: хранилище держимых пусто", [], held_names(s))


def removed_reasons(s: Stand, branch: str) -> list:
    return [(r.get("data") or {}).get("причина") for r in rows_of(s, branch, "mq.держание-снято")]


def case_lq1_held_expired(tmp: Path) -> None:
    """lq1-д: срок MERGE_QUEUE_BLAMELESS_TTL_H и потолок MERGE_QUEUE_BLAMELESS_RETRIES — заявка
    снимается со строкой события, ветка не влита."""
    # Поломка: держится вечно; ключи не читаются из harness.conf; снята молча; снятая влита.
    print("lq1-д) срок и потолок повторов — держание снято с событием")
    s, branch, _, _, _ = held_stand(tmp, "lq1д-срок", MERGE_QUEUE_BLAMELESS_TTL_H="0.0001")
    check("lq1-д: срок — заявка сначала держится", [branch], held_names(s))
    time.sleep(1)
    code = trigger(s, "lq1d-trigger")
    check("lq1-д: срок — триггер слит, держимых нет", (0, []), (code, held_names(s)))
    check("lq1-д: срок — одна строка mq.держание-снято с причиной «срок»", ["срок"],
          removed_reasons(s, branch))
    check("lq1-д: срок — ветка не влита", False, in_master(s, s.sha(branch)))

    s, branch, _, _, _ = held_stand(tmp, "lq1д-потолок", MERGE_QUEUE_BLAMELESS_RETRIES="1")
    check("lq1-д: потолок — заявка сначала держится", [branch], held_names(s))
    # Сдвиг master по входам проба-узла, она по-прежнему красна: повтор тратится и снова красный.
    master_change(s, "scripts/probe_node.py", "# сдвиг\n" + GRAPH_FILES["scripts/probe_node.py"],
                  "master: сдвиг пробы, красна")
    code = trigger(s, "lq1d-trigger2")
    check("lq1-д: потолок — триггер слит, держимых нет", (0, []), (code, held_names(s)))
    check("lq1-д: потолок — держалась один раз, снята с причиной «потолок»", (1, ["потолок"]),
          (len(rows_of(s, branch, "mq.держится")), removed_reasons(s, branch)))
    check("lq1-д: потолок — ветка не влита", False, in_master(s, s.sha(branch)))

    # Повторное держание после повтора слиятеля: срок идёт на всё держание, «держится_с» прежний.
    s, branch, _, _, _ = held_stand(tmp, "lq1д-с")
    since = held_request(s).get("держится_с")
    time.sleep(1.1)
    master_change(s, "scripts/probe_node.py", "# сдвиг\n" + GRAPH_FILES["scripts/probe_node.py"],
                  "master: сдвиг пробы, красна")
    check("lq1-д: повтор — триггер слит", 0, trigger(s, "lq1d-trigger3"))
    request = held_request(s)
    check("lq1-д: повтор — держится снова, повторов_держания 1, держится_с прежний",
          ([branch], 1, True, since), (held_names(s), request.get("повторов_держания"),
                                       bool(since), request.get("держится_с")))


def case_lq1_own_fault_not_held(tmp: Path) -> None:
    """lq1-е: код 3 по вине ветки (своя красная, в том числе рядом с красной master) — не
    держится. Проба сохранности: сегодня не держится ничего; живость — мутацией «держать при
    непустом пересечении» вместо «все красные красны на master»."""
    print("lq1-е) код 3 по вине ветки — не держится")
    s = Stand(tmp / "lq1е")
    branch, wt = s.tree("own")
    s.change(wt, "scripts/node_lib.py", "# своя поломка\n" + RED_NODE)
    code, data, _ = s.queue(branch, "--json")
    check("lq1-е: своя красная — код 3, красны_на_master [], не держится", (3, [], None, []),
          (code, data.get("красны_на_master"), data.get("держится") or None, held_names(s)))

    s = Stand(tmp / "lq1е-смесь")
    master_change(s, "scripts/node_lib.py", RED_NODE, "master красный")
    branch, wt = s.tree("mixed")
    s.change(wt, "scripts/node_lib.py", "# правка\n" + RED_NODE)
    s.change(wt, "scripts/probe_other.py", "import sys\nsys.exit(1)\n")
    code, data, _ = s.queue(branch, "--json")
    check("lq1-е: смесь — код 3, на master красна только проба-узла", (3, ["проба-узла"]),
          (code, data.get("красны_на_master")))
    check("lq1-е: смесь — не держится, строки mq.держится нет", (None, [], 0),
          (data.get("держится") or None, held_names(s), len(rows_of(s, branch, "mq.держится"))))


def case_lq1_unrelated_shift_keeps_held(tmp: Path) -> None:
    """lq1-ж: master сдвинут мимо входов проба-узла, запомненный итог master по ней всё ещё
    красный — держимая не подаётся (горячий круг: тот же отказ на каждой подаче)."""
    # Поломка: возврат при любом сдвиге master — повтор тратит потолок и снова краснеет.
    print("lq1-ж) master сдвинут мимо входов пробы — держимая не подаётся")
    s, branch, _, _, _ = held_stand(tmp, "lq1ж")
    s.change(s.proj, "docs/заметка.md", "сдвиг master документом\n", "документ")
    check("lq1-ж: триггер слит", 0, trigger(s, "lq1zh-trigger"))
    check("lq1-ж: держимая на месте, повторов_держания 0, одна строка mq.держится",
          ([branch], 0, 1), (held_names(s), held_request(s).get("повторов_держания"),
                             len(rows_of(s, branch, "mq.держится"))))


def case_lq1_helper_resubmits_held(tmp: Path) -> None:
    """lq1-з: помощник подаёт держимую ветку снова — её заявка забрана из держатся/ (не вторая),
    прогон новый, потолок не тратится."""
    # Поломка: find_request не смотрит держатся/ — вторая заявка той же ветки; старый итог 3 не
    # снят — подача читает его без прогона; подача помощника растит счётчик повторов.
    print("lq1-з) помощник подаёт держимую ветку — заявка та же, потолок цел")
    s, branch, _, _, _ = held_stand(tmp, "lq1з")
    name = mq_names(s, HELD)
    code, data, _ = s.queue(branch, "--json")
    check("lq1-з: код 3, держится", (3, True), (code, data.get("держится")))
    check("lq1-з: держится та же заявка, повторов_держания 0", (name, 0),
          (mq_names(s, HELD), held_request(s).get("повторов_держания")))
    check("lq1-з: прогон новый — две строки mq.держится", 2, len(rows_of(s, branch, "mq.держится")))


def case_lq1_fix_merged_releases_held(tmp: Path) -> None:
    """lq1-и: master чинится слиянием через очередь — держимая возвращается в заявки/ сразу после
    пачки; подача её ветки не читает старый итог 3, а сливает."""
    # Поломка: возврат только в начале следующей подачи (после пачки не смотрят); старый итог 3
    # при возврате не снят — подача читает «держится» вместо слияния.
    print("lq1-и) починка master через очередь — держимая возвращается и сливается")
    s, branch, _, _, _ = held_stand(tmp, "lq1и")
    fix, wt = s.tree("lq1i-fix")
    s.change(wt, "scripts/probe_node.py", FIXED_PROBE, "ветка: проба-узла починена")
    code, _, _ = s.queue(fix, "--json")
    check("lq1-и: починка слита", 0, code)
    check("lq1-и: держимая сразу после пачки — в заявках, держатся/ пуст", ([branch], []),
          (queued_names(s), held_names(s)))
    code, data, _ = s.queue(branch, "--json")
    check("lq1-и: подача ветки — код 0, не старый итог 3", (0, None), (code, data.get("держится")))
    check("lq1-и: правка держимой ветки в master", True,
          s.read("scripts/node_lib.py").startswith("# правка ветки"))


def case_lq1_tidy_merges_released(tmp: Path) -> None:
    """lq1-к: `--убрать` при починенном master возвращает держимую и сам её сливает: ждать
    вернувшуюся некому (ночное задание)."""
    # Поломка: --убрать только возвращает в заявки/ — ветка ждёт чужой подачи.
    print("lq1-к) --убрать сливает вернувшуюся держимую")
    s, branch, _, _, _ = held_stand(tmp, "lq1к")
    master_change(s, "scripts/probe_node.py", FIXED_PROBE, "master: проба-узла починена")
    code, _, _ = s.queue("--убрать")
    check("lq1-к: --убрать код 0", 0, code)
    check("lq1-к: держимая влита, одна строка mq.слито", (True, 1),
          (s.read("scripts/node_lib.py").startswith("# правка ветки"), len(rows_of(s, branch, "mq.слито"))))
    check("lq1-к: ни в держатся/, ни в заявках", ([], []), (held_names(s), queued_names(s)))


CASES = (case_a_fast_forward, case_b_master_moved, case_c_conflict, case_d_red_gate,
         case_e_dirty_helper, case_f_dirty_main, case_g_locked, case_h_package,
         case_p_package_superset,
         case_i_two_queues, case_j_union, case_k_heal_hook, case_l_retries, case_m_cleanup,
         case_n_rollback, case_bad_call, case_q_new_sample_key, case_r_key_unwritable,
         case_r2_key_root_dir,
         case_s_package_carried, case_t_dirty_map, case_map_moved_master,
         case_other_moved_master, case_map_moved_branch_map, case_map_moved_branch_map_red,
         case_u_broken_map,
         case_v_called_from_tree, case_w_idle_trees, case_x_queued_tree,
         case_y_unified_carried,
         case_sel_graph_red, case_sel_doc_only, case_sel_ceiling, case_sel_green,
         case_sel_shared_node, case_sel_same_tree, case_sel_slot_wait,
         case_lock_wait_covers_ceiling, case_sel_slot_never, case_sel_ceiling_from_master,
         case_sel_record_ceiling, case_sel_record_red, case_sel_range_from_rebase,
         case_sel_range_before_rebase, case_sel_record_red_strict,
         # очередь пачкой (спека ит.3 §6): красные до кода
         case_p1_code8_by_pulse, case_p2_precommit_names, case_p3_no_reasonless_code3,
         case_p4_single_as_today, case_p5_atomic_and_broken, case_p6_queued_while_locked,
         case_p7_release_race, case_p8_batch_one_gate, case_p9_conflict_master,
         case_p9b_conflict_neighbour, case_p10_bisect_last, case_p11_bisect_ceiling,
         case_p12_tail_back, case_p13_orphan_live_pid, case_p14_gate_seconds_plain,
         case_p16_return_no_code8, case_p17_release_after_own, case_p18_killed_after_result,
         case_p19_killed_after_ff, case_p20_killed_submit, case_p21_package_mid_chain,
         case_p27_package_against_master, case_p22_output_to_submitter,
         case_p23_relative_target, case_p24_no_code3_for_returns, case_p25_two_live_submits,
         case_p26_orphan_kills_merger, case_p28_reset_keep_refused,
         case_p29_cleanup_spares_queued, case_p30_foreign_tree_in_request,
         case_p31_broken_yaml_keeps_package, case_p32_in_master_before_clean,
         case_p33_commits_after_claim, case_p34_conflict_first,
         case_p35_map_retry_chain_changed, case_queue_keys_in_sample,
         # читаемые пути и узкий повтор (спека пропускной способности очереди §0, §4.0)
         case_conflict_utf8_path, case_regate_outside, case_regate_tip_rules,
         case_regate_chain_changed, case_regate_red_once, case_regate_map_only,
         case_regate_shift_red, case_regate_race, case_regate_rules_moved,
         case_regate_rules_list_shrunk, case_regate_no_selection,
         case_red_names,
         case_master_red, case_master_green_remembered, case_queue_own_files,
         # отказ не по вине ветки (lq1): красные до кода
         case_lq1_built_conflict_rebuilt, case_lq1_built_and_plain_conflict,
         case_lq1_blameless_held, case_lq1_held_resubmitted, case_lq1_held_expired,
         case_lq1_own_fault_not_held, case_lq1_bad_generator,
         case_lq1_unrelated_shift_keeps_held, case_lq1_helper_resubmits_held,
         case_lq1_fix_merged_releases_held, case_lq1_tidy_merges_released)


def main() -> int:
    global checked, failed
    if not QUEUE.exists():
        print(f"нет {QUEUE}: случаи идут, механизма нет — красные")
    # MQ_TEST_ONLY=<регулярка по имени случая> — прогон части набора (долгий набор целиком — ночью).
    # MQ_CASES=<имена через запятую> — то же точными именами (синоним, им зовут прежние записи).
    names = [re.escape(n) for n in os.environ.get("MQ_CASES", "").split(",") if n]
    only = re.compile(os.environ.get("MQ_TEST_ONLY") or (f"^({'|'.join(names)})$" if names else ""))
    cases = [case for case in CASES if only.search(case.__name__)]
    if not cases:
        print(f"MQ_TEST_ONLY={only.pattern!r}: ни одного случая — провал")
        return 1
    with tempfile.TemporaryDirectory() as raw:
        for case in cases:
            try:
                case(Path(raw))
            except Exception as exc:  # noqa: BLE001 — падение случая = провал, не падение пробы
                checked += 1
                failed += 1
                print(f"  ПЛОХО {case.__name__}: исключение {type(exc).__name__}: {exc}")
    print(f"SELFTEST: {'зелёный' if not failed else 'КРАСНЫЙ'} ({checked} проверок, "
          f"{len(cases)} случаев, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
