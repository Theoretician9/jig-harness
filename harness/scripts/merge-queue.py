#!/usr/bin/env python3
"""Очередь слияния: ветка помощника → master основного дерева, строго по одной.

    python3 scripts/merge-queue.py <ветка|путь-дерева> [--json]
    python3 scripts/merge-queue.py --убрать [--json]

Помощник работает в своём дереве `<проект>/.claude/worktrees/agent-*`; сюда он
приходит с готовой веткой. Очередь под замком: rebase на master, ворота во
временном дереве, сверка пакета, `merge --ff-only`, запись файлов харнеса в пакет,
снятие дерева. Основное дерево трогает одна команда — ff-слияние: git сам отказывает,
если оно пересекается с чужими правками (код 4), и ничего не сбрасывает.

Коды: 0 слито · 1 конфликт rebase · 2 неверный вызов · 3 ворота красные ·
4 правки основного мешают · 5 грязное дерево помощника · 6 master не стоит ·
7 пакет разошёлся · 8 замок очереди не взят · 9 слито, пакет не закоммичен.
Контракт: спека копий кода помощников §4.
"""
from __future__ import annotations

import argparse
import difflib
import fcntl
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB_DIR = HERE / "lib"
sys.path.insert(0, str(LIB_DIR))
import config  # noqa: E402


def _load(name: str, path: Path):
    """Модуль по пути: `import events` из каталога scripts взял бы scripts/events.py, а не
    библиотеку; compare-with-package.py с дефисом иначе не импортируется."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


events = _load("events_lib", LIB_DIR / "events.py")

SRC = "scripts/merge-queue.py"
RESULTS = {0: "слито", 1: "конфликт", 2: "неверный-вызов", 3: "ворота", 4: "мешают-правки",
           5: "грязное-дерево", 6: "master-не-стоит", 7: "пакет-разошёлся",
           8: "замок-не-взят", 9: "пакет-не-закоммичен"}
HUNK_LINES = 200
STALE_TEMP_SECONDS = 3600
LOCK_RETRY_SECONDS = 2
LOCK_RETRY_TIMES = 5
REMEMBERED_PATHS = 500
CC_LOCK_NOTE = "держит Claude Code, уберёт --убрать"


class Stop(Exception):
    """Прогон кончен с кодом; текст — что сказать человеку."""

    def __init__(self, code: int, message: str = "", files: list | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.files = files or []


class Report:
    """Объект --json: тот же, что уходит в журнал."""

    def __init__(self, target: str, tidy: bool = False):
        # Уборка — не слияние: её «слито» commit-friction счёл бы итогом очереди.
        self.success = "убрано" if tidy else RESULTS[0]
        self.fields = {"код": 0, "итог": RESULTS[0], "ветка": target, "sha_до": "", "sha_после": "",
                       "файлов": 0, "пакет_файлов": 0, "ожидание_с": 0.0, "ворота_с": None,
                       "повторов": 0, "конфликт": [], "оставлено": []}

    def finish(self, code: int) -> dict:
        self.fields.update(код=code, итог=self.success if code == 0 else RESULTS[code])
        return self.fields


class Context:
    """Корни и ключи из конфига, не из cwd: копия скрипта в дереве помощника или в пакете
    иначе слила бы в чужой проект."""

    def __init__(self):
        self.project = Path(config.конфиг()["PROJECT_DIR"])
        self.package = config.katalog_paketa()
        self.log_dir = Path(config.log_dir())
        values = config.конфиг(config.путь_harness_conf())
        self.retries = int(values.get("MERGE_QUEUE_RETRIES") or 3)
        self.wait_seconds = float(values.get("MERGE_QUEUE_WAIT_MIN") or 30) * 60
        self.mq_dir = self.project / ".claude" / "worktrees"


OUT = sys.stdout


def say(text: str) -> None:
    print(text, file=OUT)


def git(where: Path, *args: str, binary: bool = False) -> subprocess.CompletedProcess:
    """git без унаследованных GIT_*: вызов из хука иначе судил бы чужой индекс."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")}
    env["LC_ALL"] = "C"
    extra = {} if binary else {"text": True, "encoding": "utf-8", "errors": "surrogateescape"}
    return subprocess.run(["git", "-C", str(where), *args], capture_output=True, env=env, **extra)


def git_out(where: Path, *args: str) -> str:
    return git(where, *args).stdout.strip()


def rev(where: Path, ref: str) -> str | None:
    proc = git(where, "rev-parse", "--verify", "-q", ref)
    return proc.stdout.strip() if proc.returncode == 0 else None


def blob(where: Path, ref: str, path: str) -> bytes | None:
    proc = git(where, "cat-file", "blob", f"{ref}:{path}", binary=True)
    return proc.stdout if proc.returncode == 0 else None


# ───────────────────────────── замок очереди ─────────────────────────────

def take_lock(ctx: Context):
    """Замок на весь прогон; (файл замка, секунд ожидания). Не взят за предел — код 8."""
    ctx.log_dir.mkdir(parents=True, exist_ok=True)
    handle = open(ctx.log_dir / "merge-queue.lock", "w")
    started = time.monotonic()
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle, time.monotonic() - started
        except BlockingIOError:
            if time.monotonic() - started > ctx.wait_seconds:
                raise Stop(8, f"замок очереди занят дольше {ctx.wait_seconds / 60:g} мин")
            time.sleep(0.2)


# ───────────────────────────── деревья ─────────────────────────────

def list_trees(ctx: Context) -> list[dict]:
    """Деревья из `worktree list --porcelain`: {path, branch, locked}."""
    trees: list[dict] = []
    for block in git_out(ctx.project, "worktree", "list", "--porcelain").split("\n\n"):
        tree: dict = {"path": None, "branch": None, "locked": False}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                tree["path"] = Path(value)
            elif key == "branch":
                tree["branch"] = value.removeprefix("refs/heads/")
            elif key == "locked":
                tree["locked"] = True
        if tree["path"]:
            trees.append(tree)
    return trees


def find_tree(ctx: Context, target: str) -> dict:
    """Дерево помощника по имени ветки или по пути; нет — код 2."""
    here = Path(target).resolve() if "/" in target else None
    for tree in list_trees(ctx):
        if tree["path"] == ctx.project or tree["branch"] in (None, "master"):
            continue
        if tree["branch"] == target or (here and tree["path"].resolve() == here):
            return tree
    raise Stop(2, f"нет ветки или дерева помощника: {target}")


def require_main_on_master(ctx: Context) -> None:
    head = git_out(ctx.project, "symbolic-ref", "--short", "-q", "HEAD")
    if head != "master":
        raise Stop(2, f"основное дерево не на master (на «{head or 'detached'}»)")


def require_clean(tree: dict) -> None:
    dirty = git_out(tree["path"], "status", "--porcelain")
    if dirty:
        raise Stop(5, f"в дереве {tree['path'].name} есть незакоммиченное:\n{dirty}")


def merged_branches(ctx: Context) -> set[str]:
    return set(git_out(ctx.project, "branch", "--merged", "master",
                       "--format=%(refname:short)").splitlines())


def helper_worked(ctx: Context, branch: str) -> bool:
    """Ветка с одной записью журнала ссылок только создана: сейчас она «слита» в master
    лишь потому, что помощник ещё не успел сделать коммит — снимать её нельзя."""
    entries = git_out(ctx.project, "reflog", "show", "--format=%gs", f"refs/heads/{branch}")
    return len(entries.splitlines()) > 1


def remove_tree(ctx: Context, tree: dict, force: bool = False) -> str | None:
    """Снять дерево и слитую ветку; текст отказа или None. --force — только для своих mq-*."""
    args = ["worktree", "remove", *(["--force"] if force else []), str(tree["path"])]
    proc = git(ctx.project, *args)
    if proc.returncode:
        return proc.stderr.strip()
    if tree["branch"] and not force:
        proc = git(ctx.project, "branch", "-d", tree["branch"])
        return proc.stderr.strip() if proc.returncode else None
    return None


def clean_helper_tree(ctx: Context, tree: dict) -> str | None:
    """Слитое чистое дерево без замка — снять. Возврат: причина, почему оставлено, или None."""
    if tree["locked"]:
        return f"{tree['path'].name}: {CC_LOCK_NOTE}"
    if git_out(tree["path"], "status", "--porcelain"):
        return f"{tree['path'].name}: есть незакоммиченное"
    failure = remove_tree(ctx, tree)
    return f"{tree['path'].name}: {failure}" if failure else None


def cleanup(ctx: Context, skip_branch: str | None = None) -> dict:
    """Уборка: слитые agent-* без замка, оборванные mq-* старше часа; неслитое не трогается."""
    merged = merged_branches(ctx)
    removed, left, unmerged = [], [], []
    for tree in list_trees(ctx):
        name = tree["path"].name
        if tree["path"].parent != ctx.mq_dir or (skip_branch and tree["branch"] == skip_branch):
            continue
        if name.startswith("mq-"):
            if not tree["path"].exists():
                continue
            if time.time() - tree["path"].stat().st_mtime > STALE_TEMP_SECONDS:
                remove_tree(ctx, tree, force=True)
                removed.append(name)
        elif name.startswith("agent-") and tree["branch"]:
            if tree["branch"] not in merged:
                unmerged.append(tree["branch"])
            elif helper_worked(ctx, tree["branch"]):
                reason = clean_helper_tree(ctx, tree)
                (left if reason else removed).append(reason or name)
    return {"убрано": removed, "оставлено": left, "неслитых": unmerged}


# ───────────────────────────── шаги слияния ─────────────────────────────

def rebase_on_master(tree: dict) -> None:
    """Конфликт: файлы и ханки в вывод, rebase снять, ветка остаётся на прежнем sha."""
    where = tree["path"]
    proc = git(where, "rebase", "master")
    if proc.returncode == 0:
        return
    files = git_out(where, "diff", "--name-only", "--diff-filter=U").splitlines()
    hunks = git(where, "diff").stdout.splitlines()[:HUNK_LINES]
    git(where, "rebase", "--abort")
    say("конфликт rebase на master, файлы:\n" + "\n".join(files))
    say("\n".join(hunks) if files else proc.stderr.strip())
    raise Stop(1, "", files)


def run_gates(ctx: Context, tip: str, report: Report) -> None:
    """Ворота на вершине ветки во временном дереве; дерево снимается всегда."""
    where = ctx.mq_dir / f"mq-{os.getpid()}-{int(time.time())}"
    proc = git(ctx.project, "worktree", "add", "--detach", str(where), "master")
    if proc.returncode:
        raise Stop(6, f"временное дерево не создано: {proc.stderr.strip()}")
    try:
        checkout = git(where, "read-tree", "--reset", "-u", tip)
        if checkout.returncode:
            raise Stop(3, f"ворота не запущены: ветка не развёрнута во временном дереве: "
                          f"{checkout.stderr.strip()}")
        env = {k: v for k, v in os.environ.items()
               if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")}
        started = time.monotonic()
        gate = subprocess.run(["bash", "scripts/pre-commit-gate.sh"], cwd=where, env=env,
                              capture_output=True, text=True, errors="replace")
        report.fields["ворота_с"] = round(time.monotonic() - started, 1)
    finally:
        git(ctx.project, "worktree", "remove", "--force", str(where))
    if gate.returncode:
        say("\n".join((gate.stdout + gate.stderr).splitlines()[-40:]))
        raise Stop(3, "ворота красные")


def branch_changes(ctx: Context, tip: str) -> list[tuple[str, str]]:
    """[(статус, путь)] отличий вершины ветки от master."""
    raw = git(ctx.project, "diff", "--name-status", "--no-renames", "-z", "master", tip).stdout
    tokens = [t for t in raw.split("\0") if t]
    return list(zip(tokens[0::2], tokens[1::2]))


def contents_equal(compare, path: str, left: bytes | None, right: bytes | None) -> bool:
    if left is None or right is None:
        return left is right
    return compare.сравнимое(path, left) == compare.сравнимое(path, right)


def package_plan(ctx: Context, tip: str, changes: list[tuple[str, str]]) -> tuple[list, list]:
    """Сверка ДО слияния: (файлы к записи в пакет [(путь, цель)], удалённые в ветке).
    Рабочий файл пакета обязан равняться версии master или вершины — иначе код 7."""
    compare = _load("compare_with_package", HERE / "compare-with-package.py")
    skills = compare.скилы_наборов(ctx.package) if ctx.package.is_dir() else set()
    write, deleted, diverged = [], [], []
    for status, path in changes:
        inside = compare.место_в_пакете(path)
        if inside is None or compare.живой(path, skills):
            continue
        if status == "D":
            deleted.append(path)
            continue
        target = ctx.package / inside
        current = target.read_bytes() if target.exists() else None
        wanted = blob(ctx.project, tip, path)
        if contents_equal(compare, path, current, wanted):
            continue
        if contents_equal(compare, path, current, blob(ctx.project, "master", path)):
            write.append((path, target))
        else:
            diverged.append((path, target, current, wanted))
    if diverged:
        raise Stop(7, diverged_report(diverged))
    return write, deleted


def diverged_report(diverged: list) -> str:
    lines = ["пакет разошёлся с master и вершиной ветки, ничего не тронуто:"]
    for path, target, current, wanted in diverged:
        lines.append(f"  {path} ↔ {target}")
        old = (current or b"").decode("utf-8", "replace").splitlines()
        new = (wanted or b"").decode("utf-8", "replace").splitlines()
        lines += list(difflib.unified_diff(old, new, "пакет", "вершина", lineterm=""))[:40]
    return "\n".join(lines)


def fast_forward(ctx: Context, branch: str) -> bool:
    """True — слито; False — master ушёл (круг заново). Занятый index.lock — повторы."""
    for attempt in range(LOCK_RETRY_TIMES + 1):
        proc = git(ctx.project, "merge", "--ff-only", branch)
        text = proc.stdout + proc.stderr
        if proc.returncode == 0:
            return True
        if "would be overwritten" in text:
            raise Stop(4, "правки основного дерева мешают слиянию:\n" + text.strip())
        if "index.lock" in text and attempt < LOCK_RETRY_TIMES:
            time.sleep(LOCK_RETRY_SECONDS)
            continue
        if "index.lock" in text:
            raise Stop(6, "основное дерево занято (index.lock)")
        if "fast-forward" in text or "diverging" in text.lower():
            return False
        raise Stop(6, f"слияние не удалось: {text.strip()}")
    return False


def write_atomic(target: Path, data: bytes, executable: bool) -> None:
    """Через временный файл и замену: читатель пакета не увидит половину файла."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".mq-")
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(temp, 0o755 if executable else 0o644)
    os.replace(temp, target)


def sync_package(ctx: Context, write: list, branch: str, tip: str) -> str | None:
    """Записать файлы в пакет и закоммитить их; None — готово, иначе команда доделки."""
    if not write:
        return None
    compare = _load("compare_with_package", HERE / "compare-with-package.py")
    for path, target in write:
        mode = git(ctx.project, "ls-tree", tip, "--", path).stdout.split(" ", 1)[0]
        # Пакет держит сравнимый вид: числа замера process-library.json — серверу, не копиям.
        data = compare.сравнимое(path, blob(ctx.project, tip, path) or b"")
        write_atomic(target, data, mode == "100755")
    paths = [str(target.relative_to(ctx.package)) for _, target in write]
    message = f"Очередь слияния: {branch} {tip[:12]}"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8") as handle:
        handle.write(message + "\n")
        handle.flush()
        added = git(ctx.package, "add", "--", *paths)
        done = git(ctx.package, "commit", "-F", handle.name, "--", *paths) if not added.returncode else added
    if done.returncode == 0:
        return None
    quoted = " ".join(shlex.quote(p) for p in paths)
    return (f"git -C {shlex.quote(str(ctx.package))} add -- {quoted} && "
            f"git -C {shlex.quote(str(ctx.package))} commit -m {shlex.quote(message)} -- {quoted}")


# ───────────────────────────── запомненные пути (откат) ─────────────────────────────

def remembered_file(ctx: Context) -> Path:
    return ctx.log_dir / "merge-queue-paths.json"


def load_remembered(ctx: Context) -> dict:
    try:
        return json.loads(remembered_file(ctx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def report_rollbacks(ctx: Context) -> None:
    """Чужой коммит, собранный до нашего ff, мог вернуть влитой путь к версии «до»: в
    master путь равен blob «до» и не равен «после». Событие один раз, запись снимается."""
    remembered = load_remembered(ctx)
    for path, versions in list(remembered.items()):
        current = rev(ctx.project, f"master:{path}")
        if current == versions["до"] and current != versions["после"]:
            say(f"откат: {path} в master вернулся к версии до слияния {versions['ветка']}")
            events.emit(SRC, "mq.откат", "error", journal="merge-queue",
                        data={"путь": path, "ветка": versions["ветка"]})
            del remembered[path]
    remembered_file(ctx).write_text(json.dumps(remembered, ensure_ascii=False), encoding="utf-8")


def remember_paths(ctx: Context, branch: str, before: str, tip: str, changes: list) -> None:
    remembered = load_remembered(ctx)
    for status, path in changes:
        was = rev(ctx.project, f"{before}:{path}")
        if status != "D" and was:
            remembered[path] = {"до": was, "после": rev(ctx.project, f"{tip}:{path}"),
                                "ветка": branch}
    kept = dict(list(remembered.items())[-REMEMBERED_PATHS:])
    remembered_file(ctx).write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")


# ───────────────────────────── прогон ─────────────────────────────

def merge_branch(ctx: Context, target: str, report: Report) -> None:
    require_main_on_master(ctx)
    tree = find_tree(ctx, target)
    branch = tree["branch"]
    report.fields["ветка"] = branch
    cleanup(ctx, skip_branch=branch)
    report_rollbacks(ctx)
    require_clean(tree)
    report.fields["sha_до"] = report.fields["sha_после"] = rev(ctx.project, "master")
    while True:
        try:
            rebase_on_master(tree)
        except Stop as stop:
            report.fields["конфликт"] = stop.files
            raise
        tip = rev(tree["path"], "HEAD")
        run_gates(ctx, tip, report)
        changes = branch_changes(ctx, tip)
        write, deleted = package_plan(ctx, tip, changes)
        before = rev(ctx.project, "master")
        if fast_forward(ctx, branch):
            break
        if report.fields["повторов"] >= ctx.retries:
            raise Stop(6, f"master уходит из-под слияния: предел повторов {ctx.retries}")
        report.fields["повторов"] += 1
    report.fields.update(sha_до=before, sha_после=rev(ctx.project, "master"),
                         файлов=len(changes), пакет_файлов=len(write))
    remember_paths(ctx, branch, before, tip, changes)
    if deleted:
        say("удалены в ветке, в пакете остаются (решает человек): " + ", ".join(deleted))
    failed_command = sync_package(ctx, write, branch, tip)
    finish_tree(ctx, tree, report)
    if failed_command:
        raise Stop(9, "слито, но пакет не закоммичен; доделать:\n" + failed_command)


def finish_tree(ctx: Context, tree: dict, report: Report) -> None:
    left = clean_helper_tree(ctx, tree)
    if left:
        say(left)
        report.fields["оставлено"].append(left)


def tidy(ctx: Context, report: Report) -> None:
    found = cleanup(ctx)
    report.fields["оставлено"] = found["оставлено"]
    say("убрано: " + (", ".join(found["убрано"]) or "ничего"))
    for line in found["оставлено"]:
        say(f"оставлено: {line}")
    say(f"неслитых веток {len(found['неслитых'])}: " + ", ".join(found["неслитых"]))


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="merge-queue.py", description=__doc__.splitlines()[0])
    parser.add_argument("target", nargs="?", help="ветка помощника или путь его дерева")
    parser.add_argument("--убрать", action="store_true", dest="tidy")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if bool(args.target) == args.tidy:
        parser.error("нужна ветка (или путь дерева) либо --убрать, не оба сразу")
    return args


def main(argv: list[str]) -> int:
    global OUT
    args = parse(argv)
    OUT = sys.stderr if args.as_json else sys.stdout
    ctx = Context()
    report = Report("-" if args.tidy else args.target, tidy=args.tidy)
    code = 0
    lock = None
    try:
        lock, waited = take_lock(ctx)
        report.fields["ожидание_с"] = round(waited, 1)
        (tidy if args.tidy else lambda c, r: merge_branch(c, args.target, r))(ctx, report)
    except Stop as stop:
        code = stop.code
        if stop.message:
            say(stop.message)
    finally:
        if lock:
            lock.close()
    result = report.finish(code)
    events.emit(SRC, f"mq.{result['итог']}", "ok" if code == 0 else "error", journal="merge-queue",
                data=result)
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False), file=sys.stdout)
    else:
        sha = lambda value: (value or "")[:10]  # noqa: E731
        print(f"merge-queue: {result['итог']} {result['ветка']} "
              f"{sha(result['sha_до'])}..{sha(result['sha_после'])}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
