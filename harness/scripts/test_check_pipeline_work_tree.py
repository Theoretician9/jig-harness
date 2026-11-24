#!/usr/bin/env python3
"""Проба: гейт пайплайна ищет бумаги задачи в дереве, где идёт коммит.

Хук — симлинк в основное дерево, и гейт брал корень бумаг из своего пути:
крупная задача из git worktree помощника не коммитилась, пока спеку и ревью
не клали в основное дерево, а бумаги, лежащие только в основном дереве,
пропускали коммит из worktree, где их нет.

Контракт, который судится (настоящий `git commit` из worktree стенда, хуки
стенда — те же, что у проекта: commit-msg — симлинк на commit-msg-gate.sh,
pre-commit зовёт check-pipeline.py):
  * две wip, трейлер Dev-Map, спека и ревью ТОЛЬКО в worktree — коммит есть;
  * две wip, трейлер Dev-Map, бумаги ТОЛЬКО в основном дереве — отказ про спеку;
  * одна wip (суд на pre-commit), бумаги ТОЛЬКО в worktree — коммит есть.

Окружение своё: временный каталог, git-репозиторий с копией scripts/, свой
harness.conf, LOG_DIR и доска; боевой .tasks/ не читается. Дверь мутаций:
PIPELINE_GATE_UNDER_TEST — путь к check-pipeline.py, который судится.

    python3 scripts/test_check_pipeline_work_tree.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent
GATE = Path(os.environ.get("PIPELINE_GATE_UNDER_TEST") or HERE / "check-pipeline.py").resolve()
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases                          # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="pipeline-work-tree-probe-"))
REPO = WORK / "main"
LOG_DIR = WORK / "log"
CONF = WORK / "harness.conf"
LOG_DIR.mkdir()
(WORK / "install.conf").write_text(f'LOG_DIR="{LOG_DIR}"\n', encoding="utf-8")
CONF.write_text('PROD_PATHS="app/"\nGUARD_PATHS="scripts/hooks/"\nAUTOMATION_LEVEL="full"\n',
                encoding="utf-8")
os.environ.update({
    "HARNESS_HOME": str(WORK / "home"),
    "HARNESS_INSTALL_CONF": str(WORK / "install.conf"),
    "HARNESS_CONF": str(CONF),
    "LOG_DIR": str(LOG_DIR),
    "HARNESS_LOG_DIR": str(LOG_DIR),
    "HARNESS_DOSKA": str(WORK / "doska.json"),
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
})
for door in ("HARNESS_PIPELINE_FORCE", "HARNESS_PIPELINE_WHY", "HARNESS_PIPELINE_DEV_MAP",
             "HARNESS_BACKLOG_OWNER_WORD", "HACKATHON_UNTIL", "GIT_DIR", "GIT_WORK_TREE",
             "GIT_INDEX_FILE"):
    os.environ.pop(door, None)

cases = Cases()
TODAY = subprocess.run(["date", "+%Y-%m-%d"], capture_output=True, text=True).stdout.strip()
PAPERS = {"docs/wt.md": "спека случая\n", "docs/wt-REVIEW.md": "ревью спеки случая\n"}


def git(*args, where=REPO):
    return subprocess.run(["git", "-C", str(where), "-c", "user.name=proba", "-c",
                           "user.email=p@p", *args], capture_output=True, text=True, timeout=180)


def card(task_id):
    return "\n".join([
        f"      - id: {task_id}", "        status: wip",
        f'        title: "Задача {task_id}"', f'        summary: "образец пробы {task_id}"',
        f"        глубина: {{ уровень: крупная, характер: разработка, кем: смена, "
        f'когда: "{TODAY}" }}',
        f'        when: {{ started: "{TODAY}" }}',
        '        spec: "docs/wt.md"'])


def map_text(ids):
    return ("version: 1\nqueue: []\nlayers:\n  - { id: infra, label: \"Инфра\", color: \"#5aa06a\" }\n"
            "lines:\n  - id: core\n    layer: infra\n    name: \"Ядро\"\n    tasks:\n"
            + "\n".join(card(task_id) for task_id in ids) + "\n")


def build_repo():
    (REPO / "scripts" / "hooks").mkdir(parents=True)
    for source in [*(SOURCE / "scripts").glob("*.py"), *(SOURCE / "scripts").glob("*.sh")]:
        shutil.copy2(source, REPO / "scripts" / source.name)
    shutil.copy2(GATE, REPO / "scripts" / "check-pipeline.py")
    shutil.copytree(SOURCE / "scripts" / "lib", REPO / "scripts" / "lib",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(SOURCE / "scripts" / "hooks" / "guard_bash.py", REPO / "scripts" / "hooks")
    (REPO / "harness" / "config").mkdir(parents=True)
    shutil.copy2(SOURCE / "harness" / "config" / "pipeline.yaml", REPO / "harness" / "config")
    (REPO / "dev-map.yaml").write_text(map_text(["hr.wt"]), encoding="utf-8")
    git("init", "-q", "-b", "master")
    git("add", "-A")
    git("commit", "-qm", "стенд")
    hooks = REPO / ".git" / "hooks"
    # Как у проекта: хук — симлинк в основное дерево, путь гейта — от реального файла.
    (hooks / "commit-msg").symlink_to("../../scripts/commit-msg-gate.sh")
    pre = hooks / "pre-commit"
    pre.write_text(f'#!/usr/bin/env bash\nexec python3 "{REPO}/scripts/check-pipeline.py"\n',
                   encoding="utf-8")
    pre.chmod(0o755)


def write(root, files):
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")


def commit_from_work_tree(name, wip_ids, papers_in_work_tree):
    """Карта wip_ids — в master; worktree от неё; крупная правка коммитится из worktree.

    Бумаги лежат либо только в worktree (и идут в коммит), либо только в
    рабочем каталоге основного дерева. Вывод — (коммит прошёл, вывод git)."""
    write(REPO, {"dev-map.yaml": map_text(wip_ids)})
    git("add", "dev-map.yaml")
    git("commit", "-qm", f"карта случая {name}", "--no-verify")
    tree = WORK / f"wt-{name}"
    git("worktree", "add", "-q", str(tree), "-b", f"w-{name}")
    big = {f"app/big_{name}.py": "".join(f"x{i} = {i}\n" for i in range(350))}
    write(tree, big)
    for path in PAPERS:
        (REPO / path).unlink(missing_ok=True)
    if papers_in_work_tree:
        write(tree, PAPERS)
        git("add", *big, *PAPERS, where=tree)
    else:
        write(REPO, PAPERS)
        git("add", *big, where=tree)
    before = git("rev-parse", "HEAD", where=tree).stdout.strip()
    done = git("commit", "-q", "-m", f"правка {name}\n\nDev-Map: hr.wt\n", where=tree)
    after = git("rev-parse", "HEAD", where=tree).stdout.strip()
    return before != after, done.stdout + done.stderr


def gate_cases():
    build_repo()

    passed, out = commit_from_work_tree("a", ["hr.wt", "hr.other"], papers_in_work_tree=True)
    cases.check("Т-1 две wip, трейлер, бумаги только в worktree: коммит прошёл", True, passed)
    cases.check("Т-1 гейт судил hr.wt: «спека и ревью на месте»", True,
                "hr.wt: крупная" in out and "спека и ревью на месте" in out)
    if not passed:
        print(out)

    passed, out = commit_from_work_tree("b", ["hr.wt", "hr.other"], papers_in_work_tree=False)
    cases.check("Т-2 две wip, трейлер, бумаги только в основном дереве: отказ", False, passed)
    cases.check("Т-2 отказ называет недостающую спеку docs/wt.md", True,
                "docs/wt.md" in out and "спека" in out)

    passed, out = commit_from_work_tree("c", ["hr.wt"], papers_in_work_tree=True)
    cases.check("Т-3 одна wip (суд на pre-commit), бумаги только в worktree: коммит прошёл",
                True, passed)
    if not passed:
        print(out)


def main():
    try:
        gate_cases()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    print(f"{'ЗЕЛЁНЫЙ' if cases.ok else 'КРАСНЫЙ'}: путей {cases.paths}, провалов {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
