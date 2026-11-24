#!/usr/bin/env python3
"""Очередь слияния: ветки помощников → master основного дерева, пачкой под одним замком.

    python3 scripts/merge-queue.py <ветка|путь-дерева> [--json]
    python3 scripts/merge-queue.py --убрать [--json]

Помощник работает в своём дереве `<проект>/.claude/worktrees/agent-*`; сюда он
приходит с готовой веткой и встаёт заявкой в `$LOG_DIR/merge-queue/заявки/`. Слиятель —
тот, кто держит замок: берёт до MERGE_QUEUE_BATCH_MAX заявок, складывает их цепочкой
во временном дереве, гонит ворота на префиксах цепочки (красные — бисекция), сверяет
пакет, `merge --ff-only` по sha, пишет файлы харнеса в пакет, снимает деревья и раскладывает
итог каждой ветке в `итоги/`. Основное дерево трогает одна команда — ff-слияние: git сам
отказывает, если оно пересекается с чужими правками (код 4), и ничего не сбрасывает.

Коды: 0 слито · 1 конфликт rebase · 2 неверный вызов · 3 ворота красные ·
4 правки основного мешают · 5 грязное дерево помощника · 6 master не стоит ·
7 пакет разошёлся · 8 замок очереди не взят · 9 слито, пакет не закоммичен ·
10 слито, новый ключ образца не дописан в машинный конфиг.
Контракт: спека копий кода помощников §4.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import difflib
import fcntl
import fnmatch
import importlib.util
import io
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter
from pathlib import Path

import yaml

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
cards = _load("cards_lib", LIB_DIR / "cards.py")

SRC = "scripts/merge-queue.py"
RESULTS = {0: "слито", 1: "конфликт", 2: "неверный-вызов", 3: "ворота", 4: "мешают-правки",
           5: "грязное-дерево", 6: "master-не-стоит", 7: "пакет-разошёлся",
           8: "замок-не-взят", 9: "пакет-не-закоммичен", 10: "ключи-не-дописаны"}
HUNK_LINES = 200
STALE_TEMP_SECONDS = 3600
LOCK_RETRY_SECONDS = 2
LOCK_RETRY_TIMES = 5
REMEMBERED_PATHS = 500
CC_LOCK_NOTE = "держит Claude Code, уберёт --убрать"
DIRTY_NOTE = "есть незакоммиченное"
QUEUED_NOTE = "ждёт заявка очереди тяжёлых прогонов"
IDLE_NOTE = "ещё не простаивает"
MQ_JOURNAL = "merge-queue"
QUEUE_DIRS = ("заявки", "взяты", "итоги", "отменены", "битые")
POLL_SECONDS = 0.5
# Итог и отмена живут дольше любого ожидания: оно ограничено пульсом слиятеля.
RESULT_TTL_SECONDS = 86400
PAUSE_MAX_SECONDS = 60
# Слиятель, убитый на заявке дважды, — заявка его роняет: итог 6, а не третий круг.
ORPHAN_LIMIT = 2
SELECTION_BEAT_SECONDS = 60
HOUR_SECONDS = 3600
SAMPLE_CONF = "harness/config/harness.conf"
MAP = "dev-map.yaml"
TRAIN = "harness/config/release-train.yaml"
# Писатели держат лок карты секунды; демон — до получаса, и очередь за ним не стоит:
# карта остаётся грязной, и слияние решит, мешает ли она.
MAP_LOCK_WAIT_SECONDS = 60
COMMIT_WRAPPER = HERE / "git-commit-locked.sh"


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
                       "повторов": 0, "повторов_без_ворот": 0, "повторов_без_выбора": 0,
                       "повторы_с": 0.0, "выбор": [], "выбор_с": None, "дерево": "", "конфликт": [], "мешают": [], "оставлено": [], "красные": [],
                       "красны_на_master": [],
                       "ключи_машины": []}

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
        self.batch_max = int(values.get("MERGE_QUEUE_BATCH_MAX") or 4)
        self.bisect_max = int(values.get("MERGE_QUEUE_BISECT_MAX") or 3)
        self.wait_seconds = (float(values.get("MERGE_QUEUE_WAIT_MIN") or 30)
                             + 2 * selection_ceiling(self.project)) * 60
        self.idle_seconds = float(values.get("WORKTREE_IDLE_H") or 6) * HOUR_SECONDS
        self.mq_dir = self.project / ".claude" / "worktrees"
        self.queue_dir = self.log_dir / "merge-queue"


OUT = sys.stdout


def say(text: str) -> None:
    print(text, file=OUT)


def git_env() -> dict:
    """Среда без унаследованных GIT_*: вызов из хука иначе судил бы чужой индекс."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")}
    env["LC_ALL"] = "C"
    return env


def git(where: Path, *args: str, binary: bool = False) -> subprocess.CompletedProcess:
    """quotepath=off — путь уходит в вывод подачи и в журнал UTF-8, а не восьмеричными escape."""
    env = git_env()
    extra = {} if binary else {"text": True, "encoding": "utf-8", "errors": "surrogateescape"}
    return subprocess.run(["git", "-c", "core.quotepath=off", "-C", str(where), *args],
                          capture_output=True, env=env, **extra)


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
            waited = time.monotonic() - started
            if waited > ctx.wait_seconds:
                refusal = Stop(8, f"замок очереди занят дольше {ctx.wait_seconds / 60:g} мин")
                refusal.waited = waited
                raise refusal
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
    # Имя ветки не начинается с точки, а «.» — своё дерево подающего: цель разбирает подача,
    # у слиятеля cwd другой.
    here = Path(target).resolve() if "/" in target or target.startswith(".") else None
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
    # git_out кода возврата не смотрит: у пропавшего каталога пустой stdout сошёл бы за «чисто».
    if not tree["path"].is_dir():
        raise Stop(5, f"дерева {tree['path'].name} нет на месте")
    dirty = git_out(tree["path"], "status", "--porcelain")
    if dirty:
        raise Stop(5, f"в дереве {tree['path'].name} есть незакоммиченное:\n{dirty}")


def merged_branches(ctx: Context) -> set[str]:
    return set(git_out(ctx.project, "branch", "--merged", "master",
                       "--format=%(refname:short)").splitlines())


def helper_worked(ctx: Context, tree: dict) -> bool:
    """Дерево можно убирать: помощник коммитил (в журнале ссылок ветки больше одной
    записи) либо ветка создана и индекс дерева не трогали дольше WORKTREE_IDLE_H.
    Условие «И»: помощник, недавно тронувший индекс, работает. Нет журнала ссылок —
    время создания неизвестно, дерево остаётся."""
    created = git_out(ctx.project, "reflog", "show", "--date=unix", "--format=%gd",
                      f"refs/heads/{tree['branch']}").split()
    if len(created) > 1:
        return True
    index = git_out(tree["path"], "rev-parse", "--path-format=absolute", "--git-path", "index")
    if not created or not index or not Path(index).is_file():
        return False
    limit = time.time() - ctx.idle_seconds
    return int(created[0].rsplit("@{", 1)[1].rstrip("}")) < limit and Path(index).stat().st_mtime < limit


def queued_trees(ctx: Context) -> set[Path]:
    """Деревья из поля «дерево» висящих заявок очереди тяжёлых прогонов, и ожидающих, и
    взятых в заход. Читается без замка записи: файл пишется целиком и атомарно."""
    try:
        state = json.loads((ctx.log_dir / "очередь-мутаций" / "очередь.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {Path(request["дерево"]).resolve() for request in state.get("заявки", []) if request.get("дерево")}


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
        return f"{tree['path'].name}: {DIRTY_NOTE}"
    if tree["path"].resolve() in queued_trees(ctx):
        return f"{tree['path'].name}: {QUEUED_NOTE}"
    failure = remove_tree(ctx, tree)
    return f"{tree['path'].name}: {failure}" if failure else None


def cleanup(ctx: Context, skip_branches: frozenset | set = frozenset()) -> dict:
    """Уборка: слитые agent-* без замка, оборванные mq-* старше часа; неслитое не трогается.
    Ветки заявок очереди пропускаются: слитая ветка, ждущая своей очереди, иначе пришла бы
    без дерева."""
    merged = merged_branches(ctx)
    removed, left, unmerged = [], [], []
    for tree in list_trees(ctx):
        name = tree["path"].name
        if tree["path"].parent != ctx.mq_dir or tree["branch"] in skip_branches:
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
            elif not helper_worked(ctx, tree):
                left.append(f"{name}: {IDLE_NOTE}")
            else:
                reason = clean_helper_tree(ctx, tree)
                (left if reason else removed).append(reason or name)
    return {"убрано": removed, "оставлено": left, "неслитых": unmerged}


# ───────────────────────────── шаги слияния ─────────────────────────────

def rebase_onto(where: Path, onto: str) -> None:
    """Rebase на sha onto (в дереве цепочки), а не на имя master: имя читается заново и могло
    уйти вперёд, тогда диапазон выбора втянул бы чужие коммиты.
    Конфликт: файлы и ханки в вывод, rebase снять; ветка помощника не затронута."""
    proc = git(where, "rebase", onto)
    if proc.returncode == 0:
        return
    # По NUL: путь с управляющим знаком git закавычивает и при quotepath=off.
    listed = git(where, "diff", "--name-only", "-z", "--diff-filter=U").stdout
    files = [path for path in listed.split("\0") if path]
    hunks = git(where, "diff").stdout.splitlines()[:HUNK_LINES]
    git(where, "rebase", "--abort")
    say("конфликт rebase, файлы:\n" + "\n".join(files))
    say("\n".join(hunks) if files else proc.stderr.strip())
    raise Stop(1, "", files)


def branch_trailers(project: Path, tip: str, base: str = "master") -> str:
    """id трейлеров Dev-Map коммитов base..tip через пробел: гейт пайплайна судит названные
    ими карточки — стадии commit-msg, где он читает трейлер коммита, у ворот очереди нет."""
    return " ".join(cards.dev_map_ids(git_out(project, "log", "--format=%B", f"{base}..{tip}")))


def checked_tree(ctx: Context, tip: str) -> str:
    """Дерево вершины, если ПОСЛЕДНЯЯ строка хука для пары (дерево вершины, base =
    текущий master) зелёная (иначе пусто). base — HEAD, против которого хук судил:
    строка без base или с другим base судила другой предмет."""
    tree = rev(ctx.project, f"{tip}^{{tree}}")
    master = rev(ctx.project, "master")
    journal = ctx.log_dir / "gates.jsonl"
    if not tree or not master or not journal.is_file():
        return ""
    last = None
    for line in journal.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get("gate") == "pre-commit"
                and row.get("tree") == tree and row.get("base") == master):
            last = row
    return tree if last and last.get("verdict") == "pass" else ""


def selection_ceiling(where: Path) -> float:
    """Потолок ворот выбора, минут; дробный бывает (стенд). Данные поезда, не код."""
    train = yaml.safe_load((where / "harness/config/release-train.yaml").read_text(encoding="utf-8")) or {}
    return float((train.get("выбор") or {}).get("потолок_мин", 15))


# Строки красных: «ПЛОХО · имя (код N)» ворот (lib/gates_check.sh), «GATE FAIL: имя»
# pre-commit-gate.sh, строка общего предела ворот. Имена — в запись очереди: без них повтор
# подачи с кодом 3 начинался с чтения gates-run-*.out (26 подач с кодом 3 за сутки 21.11).
# Строки — только боевой формат целиком: вывод самой пробы со словом «ПЛОХО» или «GATE FAIL»
# без префикса не должен называть красной несуществующую проверку.
RED_LINE = re.compile(r"^(?:  ПЛОХО · (.+?) \(код \d+\)(?: — красная и соло)?"
                      r"|\[pre-commit\] GATE FAIL: (.+?)[ \t]*"
                      r"|(=== ворота превысили общий предел.*))$", re.M)


def red_names(output: str) -> list[str]:
    names = []
    for match in RED_LINE.finditer(output):
        name = match.group(1) or match.group(2) or "общий предел ворот"
        if name not in names:
            names.append(name)
    return names


def run_pre_commit(where: Path, env: dict, report: Report) -> None:
    started = time.monotonic()
    gate = subprocess.run(["bash", "scripts/pre-commit-gate.sh"], cwd=where, env=env,
                          capture_output=True, text=True, errors="replace")
    report.fields["ворота_с"] = round(time.monotonic() - started, 1)
    if gate.returncode:
        report.fields.update(причина="красные", ступень="pre-commit",
                             красные=red_names(gate.stdout + gate.stderr))
        say("\n".join((gate.stdout + gate.stderr).splitlines()[-40:]))
        raise Stop(3, "ворота красные")


def pick_checks(where: Path, spans: list) -> list[str]:
    """Имена проверок, задетых файлами всех диапазонов, — одним вызовом select-checks.py: правила
    выбора судят файл, так что выбор объединения файлов — объединение выборов, и порядок gates.sh
    выходит сам. LookupError — выбор не получен."""
    files = []
    for span in spans:
        proc = git(where, "diff", "--name-only", "-z", span)
        if proc.returncode:
            raise LookupError(proc.stderr.strip())
        files += [path for path in proc.stdout.split("\0") if path]
    if not files:
        return []
    picked = subprocess.run([sys.executable, str(HERE / "select-checks.py"), "--корень", str(where),
                             "--имена", "--очередь", "--файлы", *dict.fromkeys(files)],
                            capture_output=True, text=True, errors="replace")
    if picked.returncode:
        raise LookupError(picked.stderr.strip())
    return [n for n in picked.stdout.strip().split("|") if n]


def run_in_slot(where: Path, names: list, env: dict, project: Path,
                heartbeat=lambda: None) -> tuple[int, str, float]:
    """`gates.sh --только <имена>` в месте очереди пула. Возврат: код, вывод, секунды от
    получения места (ожидание места — не прогон)."""
    # Потолок — из master (проекта): ветка не поднимает его себе своим release-train.yaml.
    ceiling = selection_ceiling(project) * 60
    with tempfile.TemporaryDirectory(prefix="mq-slot-") as tmp:
        marker, log = Path(tmp, "место"), Path(tmp, "вывод")
        command = ["bash", "scripts/heavy-slot.sh", "--не-дольше", str(int(ceiling) or 1), "--место-очереди", "--",
                   "bash", "-c", ': > "$0"; exec bash scripts/gates.sh --только "$1"', str(marker), "|".join(names)]
        with log.open("w") as out:
            proc = subprocess.Popen(command, cwd=where, env=env, start_new_session=True,
                                    stdout=out, stderr=subprocess.STDOUT)
        began = last_beat = time.monotonic()
        acquired = None
        while proc.poll() is None:
            now = time.monotonic()
            if now - last_beat >= SELECTION_BEAT_SECONDS:
                heartbeat()
                last_beat = now
            if acquired is None and marker.exists():
                acquired = now
            if (acquired or began + ceiling) + ceiling < now:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise Stop(3, f"ворота выбора не дошли: дольше потолка {ceiling / 60:g} мин")
            time.sleep(0.2)
        # Прогон короче шага опроса: метку место поставило, а цикл её не застал — секунды без ожидания.
        if acquired is None and marker.exists():
            acquired = time.monotonic() - (time.time() - marker.stat().st_mtime)
        output = log.read_text(encoding="utf-8", errors="replace")
    return proc.returncode, output, round(time.monotonic() - (acquired or began), 1)


def run_selection(where: Path, spans: list, env: dict, report: Report, project: Path,
                  heartbeat=lambda: None) -> None:
    """Проверки gates.sh, задетые файлами диапазонов spans, в том же дереве.
    Пустой выбор gates.sh не зовёт: `--только ""` боевой gates.sh не принимает."""
    try:
        names = pick_checks(where, spans)
    except LookupError as error:
        report.fields.update(причина="нет выбора", ступень="выбор")
        raise Stop(3, f"выбор проверок не получен: {error}")
    report.fields["выбор"] = names
    if not names:
        return
    try:
        returncode, output, report.fields["выбор_с"] = run_in_slot(where, names, env, project, heartbeat)
    except Stop:
        report.fields.update(причина="не дошли", красные=[], ступень="выбор")
        raise
    if returncode:
        say("\n".join(output.splitlines()[-40:]))
        report.fields.update(причина="красные", красные=red_names(output), ступень="выбор")
        raise Stop(3, "ворота красные")


# Итог проверки на самом master — {имя: {"sha", "красна"}} в LOG_DIR. Красная на master
# красна у любой ветки: соло-повтор её красной (gates_check.sh, GATES_NO_RECHECK) только
# удваивает прогон. 21.11.2026 master был красен по 5 проверкам с 14:47, и каждая подача
# гнала общий прогон и соло каждой — выбор_с 3286 с на подачу.
def master_memory_file(ctx: Context) -> Path:
    return ctx.log_dir / "mq-master-red.json"


def master_verdicts(ctx: Context, where: Path) -> dict:
    """Запомненные итоги, чьих файлов master с узнавания не трогал: выбор очереди по диапазону
    узнавание..master их не называет. Якорь живых — нынешний master. where стоит на master."""
    try:
        memory = json.loads(master_memory_file(ctx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        memory = {}
    master = rev(ctx.project, "master")
    kept = {}
    for anchor in {v["sha"] for v in memory.values()}:
        try:
            touched = set(pick_checks(where, [f"{anchor}..{master}"]))
        except LookupError:  # файл пережил историю (коммит узнавания пропал) — итог не сверить
            continue
        kept.update({name: {"sha": master, "красна": v["красна"]}
                     for name, v in memory.items() if v["sha"] == anchor and name not in touched})
    return kept


def learn_master(ctx: Context, where: Path, env: dict, report: Report, known: dict) -> list:
    """Красные выбора с неизвестным итогом на master — один прогон на дереве master без соло
    (ложная красная master только строже: флейк ветки тогда красный). Возврат — красные выбора,
    красные и на master."""
    reds = [n for n in report.fields["красные"] if n in report.fields["выбор"]]
    unknown = [n for n in reds if n not in known]
    if unknown:
        git(where, "read-tree", "--reset", "-u", "master")
        _, output, _ = run_in_slot(where, unknown, {**env, "GATES_NO_RECHECK": "|".join(unknown),
                                                    "HARNESS_GATE_RANGE": "master..master"}, ctx.project,
                                lambda: beat(ctx))
        master_reds = red_names(output)
        known = {**known, **{n: {"sha": rev(ctx.project, "master"), "красна": n in master_reds}
                             for n in unknown}}
    master_memory_file(ctx).write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")
    return [n for n in reds if known[n]["красна"]]


def run_gates(ctx: Context, base: str, tip: str, report: Report, spans: list | None = None) -> None:
    """Ворота на вершине ветки во временном дереве: pre-commit-gate, затем выбор по файлам
    диапазонов spans (по умолчанию — ветки, base..tip). Зелёная запись хука на то же дерево
    против текущего master снимает только pre-commit: выбор хук не гонял. Дерево снимается всегда."""
    beat(ctx)
    same = checked_tree(ctx, tip)
    report.fields["дерево"] = rev(ctx.project, f"{tip}^{{tree}}") or ""
    where = ctx.mq_dir / f"mq-{os.getpid()}-{int(time.time())}"
    proc = git(ctx.project, "worktree", "add", "--detach", str(where), "master")
    if proc.returncode:
        raise Stop(6, f"временное дерево не создано: {proc.stderr.strip()}")
    try:
        known = master_verdicts(ctx, where)
        checkout = git(where, "read-tree", "--reset", "-u", tip)
        if checkout.returncode:
            report.fields.update(причина="не развёрнута", ступень="pre-commit")
            raise Stop(3, f"ворота не запущены: ветка не развёрнута во временном дереве: "
                          f"{checkout.stderr.strip()}")
        env = {k: v for k, v in os.environ.items()
               if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")}
        env["HARNESS_PIPELINE_DEV_MAP"] = branch_trailers(ctx.project, tip, base)
        env["HARNESS_GATE_RANGE"] = f"{base}..{tip}"
        env["GATES_NO_RECHECK"] = "|".join(n for n, v in known.items() if v["красна"])
        if same:
            report.fields["ворота_с"] = 0
            report.fields["ворота"] = f"то же дерево {same[:8]}"
        else:
            run_pre_commit(where, env, report)
        # Диапазон от master, на который сделан rebase: master идёт вперёд за время ворот, и чужие
        # коммиты иначе попадали бы в выбор ветки.
        try:
            run_selection(where, spans or [f"{base}..{tip}"], env, report, ctx.project, lambda: beat(ctx))
        except Stop:
            report.fields["красны_на_master"] = learn_master(ctx, where, env, report, known)
            raise
    finally:
        git(ctx.project, "worktree", "remove", "--force", str(where))
        beat(ctx)


def branch_changes(ctx: Context, base: str, tip: str) -> list[tuple[str, str]]:
    """[(статус, путь)] отличий вершины ветки от base — вершины цепочки под ней."""
    raw = git(ctx.project, "diff", "--name-status", "--no-renames", "-z", base, tip).stdout
    tokens = [t for t in raw.split("\0") if t]
    return list(zip(tokens[0::2], tokens[1::2]))


def contents_equal(compare, path: str, left: bytes | None, right: bytes | None) -> bool:
    if left is None or right is None:
        return left is right
    return compare.сравнимое(path, left) == compare.сравнимое(path, right)


def package_plan(ctx: Context, tip: str, changes: list[tuple[str, str]]) -> tuple[list, list]:
    """Сверка ДО слияния: (файлы ветки для пакета [(путь, цель)], удалённые в ветке).
    Правка рабочего файла пакета к master обязана входить в правку вершины — иначе код 7.
    Файл, уже равный вершине, тоже несётся: его положили в пакет руками, и без коммита
    очереди он лежал бы там незакоммиченным."""
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
        versions = (blob(ctx.project, "master", path), current, wanted)
        extras = [] if contents_equal(compare, path, current, wanted) else \
            package_extras(path, *(compare.сравнимое(path, v or b"") for v in versions))
        if extras:
            diverged.append((path, target, current, wanted, extras))
        else:
            write.append((path, target))
    if diverged:
        raise Stop(7, diverged_report(diverged))
    return write, deleted


def package_extras(path: str, master: bytes, package: bytes, tip: bytes) -> list[str]:
    """Что пакет изменил к master, а вершина нет; пусто — вершина включает пакет.
    Помощники вставляют блоки в общий пакет у одних якорей, и порядок вставок у пакета и у
    сведённой вершины разный: равенство байтов отказывало бы в законном слиянии."""
    texts = [v.decode("utf-8", "replace") for v in (master, package, tip)]
    if not path.endswith((".yaml", ".yml")):
        return line_extras(*(t.splitlines() for t in texts))
    # Комментарии YAML разбор теряет — их судят строками, записи — по значению.
    comments = [[l for l in t.splitlines() if l.lstrip().startswith("#")] for t in texts]
    return record_extras(*texts) + line_extras(*comments)


def line_extras(master: list, package: list, tip: list) -> list[str]:
    """Строки, добавленные (+) или удалённые (-) пакетом и не так же в вершине. Счёт без
    выравнивания diff: при другом порядке вставок diff сдвигает общие строки блоков."""
    before, ours, theirs = Counter(master), Counter(package), Counter(tip)
    extras = [f"+ {line}" for line in ((ours - before) - (theirs - before)).elements()]
    return extras + [f"- {line}" for line in ((before - ours) - (before - theirs)).elements()]


ABSENT = object()


def record_extras(master: str, package: str, tip: str) -> list[str]:
    try:
        yaml.safe_load(tip)
    except yaml.YAMLError:
        # Непарсящийся YAML вершины — дело ворот (они до ff красные): сверка до ворот иначе
        # отвечала бы кодом 7 вместо честного кода 3. Строками — чтобы правка пакета, которой
        # нет в вершине, всё равно не затёрлась.
        return line_extras(*(t.splitlines() for t in (master, package, tip)))
    try:
        before, ours, theirs = (records(yaml.safe_load(t)) for t in (master, package, tip))
    except yaml.YAMLError as exc:
        return [f"YAML не разобран: {str(exc).splitlines()[0]}"]
    extras = []
    for key in [k for k in ours if ours[k] != before.get(k, ABSENT)] + \
            [k for k in before if k not in ours]:
        value = ours.get(key, ABSENT)
        if theirs.get(key, ABSENT) != value:
            gone = " (удалена в пакете)" if value is ABSENT else ""
            extras.append(f"запись {' → '.join(key)}{gone}")
    return extras


def records(doc) -> dict:
    """Записи реестра: под ключом верхнего уровня — ключ словаря или элемент списка
    (элемент со словом «файл», как в mechanisms.yaml, — по нему)."""
    found: dict = {}
    for top, value in (doc.items() if isinstance(doc, dict) else [("", doc)]):
        if isinstance(value, dict):
            found.update({(str(top), str(k)): v for k, v in value.items()})
        elif isinstance(value, list):
            found.update({(str(top), record_id(v)): v for v in value})
        else:
            found[(str(top),)] = value
    return found


def record_id(item) -> str:
    if isinstance(item, dict) and "файл" in item:
        return str(item["файл"])
    return json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)


def diverged_report(diverged: list) -> str:
    lines = ["в пакете есть правки, которых нет в вершине ветки, ничего не тронуто:"]
    for path, target, current, wanted, extras in diverged:
        lines.append(f"  {path} ↔ {target}")
        lines += [f"    {extra}" for extra in extras[:40]]
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
            blocking = [line.strip() for line in text.splitlines() if line.startswith("\t")]
            if blocking != [MAP]:
                raise Stop(4, "правки основного дерева мешают слиянию:\n" + text.strip(), blocking)
            refusal = commit_dirty_map(ctx, branch)
            if refusal:
                raise Stop(4, refusal, blocking)
            return False                    # карта легла в master: rebase и круг заново
        if "index.lock" in text and attempt < LOCK_RETRY_TIMES:
            time.sleep(LOCK_RETRY_SECONDS)
            continue
        if "index.lock" in text:
            raise Stop(6, "основное дерево занято (index.lock)")
        if "fast-forward" in text or "diverging" in text.lower():
            return False
        raise Stop(6, f"слияние не удалось: {text.strip()}")
    return False


def shift_paths(project: Path, judged_tip: str, tip: str) -> list[str] | None:
    """Чем сливаемое дерево отличается от уже судимого: сдвиг master, склейки, пересборка.
    Обе стороны — неизменные sha после build_chain: коммит, легший в master позже, сюда не
    попадёт, а ff его не пропустит. None — разность не прочитана, судят полные ворота."""
    proc = git(project, "diff", "--name-only", "-z", judged_tip, tip)
    return [path for path in proc.stdout.split("\0") if path] if proc.returncode == 0 else None


def selection_inputs(project: Path, ref: str) -> list[str]:
    """Шаблоны `выбор_входы:` из release-train.yaml в ref. Пусто — ключа нет, он не список или
    файл не разобран: правила выбора могли измениться."""
    raw = blob(project, ref, TRAIN)
    try:
        listed = (yaml.safe_load(raw) or {}).get("выбор_входы") if raw else None
    except (yaml.YAMLError, AttributeError):
        return []
    return [str(pattern) for pattern in listed] if isinstance(listed, list) else []


def rules_moved(project: Path, refs: tuple, shift: list) -> bool:
    """Сдвиг задел сами правила выбора (gates.sh, mechanisms.yaml, судью): судить его выбором по
    старому смыслу нельзя. Список — объединение из base и прошлой вершины: сдвиг, вынесший путь
    из списка и тут же поменявший правило, всё равно виден; ветка так может путь лишь добавить."""
    groups = [selection_inputs(project, ref) for ref in refs]
    if not all(groups):
        return True
    return any(fnmatch.fnmatchcase(path, pattern)
               for path in shift for group in groups for pattern in group)


def check_branch_map(where: Path) -> None:
    """Сверка одной карты дерева ветки после rebase — тем же валидатором, что pre-commit:
    склейка двух правок карты может дать дубль id, которого не было ни в одной."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")}
    proc = subprocess.run(["bash", "scripts/pre-commit-gate.sh", "--devmap-only", MAP],
                          cwd=where, env=env, capture_output=True, text=True, errors="replace")
    if proc.returncode:
        say("\n".join((proc.stdout + proc.stderr).splitlines()[-40:]))
        raise Stop(3, "карта ветки после rebase на master красная")


def commit_dirty_map(ctx: Context, branch: str) -> str | None:
    """Грязная карта основного — отдельным коммитом, под общим локом писателей карты.
    None — карта чиста или закоммичена; строка — почему не коммитим.

    Писатели карты (строитель проверок, map-from-message и др.) пишут её под локом и
    не всегда коммитят; грязная карта держала очередь кодом 4 (16.11 трижды за час)."""
    if not git_out(ctx.project, "status", "--porcelain", "--", MAP):
        return None
    try:
        with cards.map_lock(cards.lock_path(), MAP_LOCK_WAIT_SECONDS):
            return commit_map_locked(ctx, branch)
    except cards.LockBusy:
        return f"карта не закоммичена: лок карты занят дольше {MAP_LOCK_WAIT_SECONDS} с"


def commit_map_locked(ctx: Context, branch: str) -> str | None:
    """Под локом карта на диске не меняется: проверенное и закоммиченное — одни байты.
    Личный индекс от HEAD плюс одна карта: чужие staged-пути общего индекса не уходят."""
    try:
        yaml.safe_load((ctx.project / MAP).read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        return f"карта невалидна (yaml.safe_load), не коммичу: {error}"
    base = rev(ctx.project, "HEAD")
    blob_sha = git_out(ctx.project, "hash-object", "-w", "--", MAP)
    message = (f"Карта: записи писателей карты — коммит очереди слияния перед {branch}\n\n"
               f"Писатели карты записали её под локом и не закоммитили; грязная карта\n"
               f"основного дерева мешала слиянию, очередь коммитит её под тем же локом.\n")
    with tempfile.TemporaryDirectory(prefix="mq-map-") as tmp:
        env = {**git_env(), "GIT_INDEX_FILE": os.path.join(tmp, "index")}
        subprocess.run(["git", "read-tree", base], cwd=ctx.project, env=env, check=True)
        subprocess.run(["git", "update-index", "--cacheinfo", f"100644,{blob_sha},{MAP}"],
                       cwd=ctx.project, env=env, check=True)
        done = subprocess.run(["bash", str(COMMIT_WRAPPER), "--base", base, "-F", "-"],
                              cwd=ctx.project, env=env, input=message, capture_output=True,
                              text=True)
    if done.returncode:
        tail = (done.stderr.strip().splitlines() or ["?"])[-1]
        return f"карта не закоммичена (обёртка, код {done.returncode}): {tail}"
    # Общий индекс держит прежний блоб карты: без сброса ff-слияние снова отказало бы.
    git(ctx.project, "update-index", "--cacheinfo", f"100644,{blob_sha},{MAP}")
    say(f"карта закоммичена очередью: {rev(ctx.project, 'HEAD')}")
    return None


def write_atomic(target: Path, data: bytes, executable: bool) -> None:
    """Через временный файл и замену: читатель пакета не увидит половину файла."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".mq-")
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(temp, 0o755 if executable else 0o644)
    os.replace(temp, target)


def package_message(ctx: Context, branch: str, before: str, tip: str) -> str:
    """Сообщение ветки: заголовок вершины, ниже — прочие коммиты ветки и метка очереди."""
    subjects = git(ctx.project, "log", "--reverse", "--format=%s", f"{before}..{tip}").stdout.splitlines()
    others = [f"- {s}" for s in subjects[:-1]]
    body = (["Коммиты ветки:", *others, ""] if others else []) + [f"Очередь слияния: {branch} {tip[:12]}"]
    return "\n".join([subjects[-1] if subjects else branch, "", *body]) + "\n"


def sync_package(ctx: Context, carry: list, branch: str, before: str, tip: str) -> tuple[int, str | None]:
    """Записать файлы ветки в пакет и закоммитить изменённые одним коммитом.
    (файлов в коммите, None — готово, иначе команда доделки)."""
    if not carry:
        return 0, None
    compare = _load("compare_with_package", HERE / "compare-with-package.py")
    for path, target in carry:
        mode = git(ctx.project, "ls-tree", tip, "--", path).stdout.split(" ", 1)[0]
        # Пакет держит сравнимый вид: числа замера process-library.json — серверу, не копиям.
        data = compare.сравнимое(path, blob(ctx.project, tip, path) or b"")
        if not target.exists() or target.read_bytes() != data:
            write_atomic(target, data, mode == "100755")
    paths = [str(target.relative_to(ctx.package)) for _, target in carry]
    message = package_message(ctx, branch, before, tip)
    added = git(ctx.package, "add", "--", *paths)
    staged = git(ctx.package, "diff", "--cached", "--name-only", "-z", "--", *paths).stdout
    changed = [p for p in staged.split("\0") if p]
    if not added.returncode and not changed:
        return 0, None
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8") as handle:
        handle.write(message)
        handle.flush()
        done = git(ctx.package, "commit", "-F", handle.name, "--", *changed) if not added.returncode else added
    if done.returncode == 0:
        return len(changed), None
    quoted = " ".join(shlex.quote(p) for p in paths)
    return len(changed), (f"git -C {shlex.quote(str(ctx.package))} add -- {quoted} && "
                          f"git -C {shlex.quote(str(ctx.package))} commit "
                          f"-m {shlex.quote(message.splitlines()[0])} -- {quoted}")


# ───────────────────────────── ключи машинного конфига ─────────────────────────────

def deliver_new_keys(ctx: Context, before: str, tip: str, report: Report) -> str | None:
    """Ключи, которые слияние внесло в образец, — дописать в машинный конфиг.

    На этой машине код идёт прямо из master: обновления (update.py → install-new.py),
    которое дописывает ключи образца у копий, здесь нет, и новый обязательный ключ
    валил читателя сети до ручной правки (EVOLUTION_MAX_PATCH_BYTES, 15.11.2026).
    Только ключи ЭТОГО слияния: давние пропуски машины живут на умолчаниях кода, и
    дописать их значит сменить поведение без решения. None — готово, иначе отказ."""
    install_new = _load("install_new", HERE / "install-new.py")
    old = install_new.ключи_образца((blob(ctx.project, before, SAMPLE_CONF) or b"").decode())
    new = install_new.ключи_образца((blob(ctx.project, tip, SAMPLE_CONF) or b"").decode())
    brought = "\n".join("\n".join(lines) for key, lines in new.items() if key not in old)
    target = Path(config.путь_harness_conf())
    text = target.read_text(encoding="utf-8")
    appended, keys, _ = install_new.дополнить_из_образца(text, brought)
    if not keys:
        return None
    report.fields["ключи_машины"] = keys
    if install_new.писатель.надо_поднять_права([target]):
        return f"слито, но {target} не пишется: ключи {', '.join(keys)} не дописаны — код их ждёт"
    install_new.писатель.снять_копию(target)
    install_new.писатель.записать_атомарно(target, appended)
    say(f"дописаны в {target}: {', '.join(keys)}")
    return None


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


# ───────────────────────────── заявки очереди ─────────────────────────────

def beat(ctx: Context) -> None:
    """Пульс слиятеля: по его mtime подача отличает движущуюся очередь от стоящей (код 8)."""
    ctx.queue_dir.mkdir(parents=True, exist_ok=True)
    (ctx.queue_dir / "пульс").touch()


def pause(ctx: Context, point: str) -> None:
    """Дверь проб: MQ_TEST_PAUSE=<точка>:<секунд> — слиятель встаёт в названной точке. Не больше
    минуты и со строкой в журнале: утёкшая в бой переменная видна, а не тихо замедляет очередь."""
    name, _, raw = os.environ.get("MQ_TEST_PAUSE", "").partition(":")
    if name != point:
        return
    seconds = min(float(raw or 0), PAUSE_MAX_SECONDS)
    # Строка журнала — до метки: проба по метке убивает слиятеля, и строка не успела бы лечь.
    events.emit(SRC, "mq.пауза", "ok", journal=MQ_JOURNAL, data={"точка": point, "секунд": seconds})
    (ctx.log_dir / f"mq-pause-{point}").touch()
    time.sleep(seconds)


def list_names(directory: Path) -> list[str]:
    """Имена заявок по порядку подачи; недописанные `.tmp-*` читать нельзя."""
    try:
        return sorted(p.name for p in directory.iterdir() if not p.name.startswith(".tmp-"))
    except FileNotFoundError:
        return []


def write_json_atomic(target: Path, data) -> None:
    """Через временный файл и замену: читатель не увидит половину JSON."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    # errors="replace": вывод git читается с surrogateescape (см. git()), и байт вне UTF-8 в
    # диффе пакета доезжал до итога суррогатом — json.dump падал, слиятель умирал «вне шагов
    # ветки», и ВСЕ заявки возвращались кодом 6 (23.11 03:4x). Итог с «�» лучше отсутствующего.
    with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as handle:
        json.dump(data, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, 0o644)
    os.replace(temp, target)


def quarantine(ctx: Context, sub: str, name: str) -> None:
    """Битая заявка — улика, а не ошибка пачки: в битые/, убирает только человек."""
    target = ctx.queue_dir / "битые" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(ctx.queue_dir / sub / name, target)
    except FileNotFoundError:
        return
    events.emit(SRC, "mq.битая", "error", journal=MQ_JOURNAL, data={"файл": name, "где": sub})


def read_request(ctx: Context, sub: str, name: str) -> dict | None:
    """Заявка целиком; None — файла нет (взял другой) или он битый (унесён в битые/)."""
    try:
        body = json.loads((ctx.queue_dir / sub / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        quarantine(ctx, sub, name)
        return None
    if not (isinstance(body, dict) and isinstance(body.get("ветка"), str)
            and isinstance(body.get("дерево"), str)):
        quarantine(ctx, sub, name)
        return None
    return body


def requested_at(request: dict) -> float:
    try:
        return dt.datetime.fromisoformat(request["подано"]).timestamp()
    except (KeyError, ValueError, TypeError):
        return time.time()


def queued_branches(ctx: Context) -> set[str]:
    """Ветки всех заявок заявки/ и взяты/: их деревья и ветки уборка не трогает."""
    found = set()
    for sub in ("заявки", "взяты"):
        for name in list_names(ctx.queue_dir / sub):
            request = read_request(ctx, sub, name)
            if request:
                found.add(request["ветка"])
    return found


def find_request(ctx: Context, branch: str) -> tuple[str, dict] | None:
    """Заявка этой ветки, если прежнюю подачу убили: вторую не пишем, ждём её итог."""
    for sub in ("заявки", "взяты"):
        for name in list_names(ctx.queue_dir / sub):
            request = read_request(ctx, sub, name)
            if request and request["ветка"] == branch:
                return name, request
    return None


def locate(ctx: Context, name: str) -> str | None:
    """Где лежит заявка. Заявка только переезжает между каталогами, и пока подача обходит их,
    может проскользнуть мимо — поэтому «нигде» подтверждается повтором."""
    for _ in range(3):
        for sub in QUEUE_DIRS:
            if (ctx.queue_dir / sub / name).exists():
                return sub
        time.sleep(0.05)
    return None


# ───────────────────────────── слиятель: заявка в руках ─────────────────────────────

class Entry:
    """Заявка в руках слиятеля: всё, что о ней известно на пути «взята → итог»."""

    def __init__(self, ctx: Context, name: str, request: dict):
        self.name, self.request = name, request
        self.branch = request["ветка"]
        self.tree = {"path": Path(request["дерево"]), "branch": self.branch, "locked": False}
        self.report = Report(self.branch)
        self.out = io.StringIO()
        self.done = False
        claimed = time.time()
        self.report.fields["ожидание_с"] = round(max(0.0, claimed - requested_at(request)), 1)
        self.begin_attempt(rev(ctx.project, "master") or "")

    @property
    def orig(self) -> str | None:
        return self.request.get("orig_sha")

    def begin_attempt(self, base: str) -> None:
        """Новый круг пачки (master ушёл): всё, что считали на прошлом, — заново."""
        self.fate, self.stop, self.red_run = "chain", None, None
        self.tip = self.before = ""
        self.changes, self.carry, self.deleted = [], [], []
        self.report.fields.update(sha_до=base, sha_после=base, файлов=0, ворота_с=None, выбор=[],
                                  выбор_с=None, дерево="", конфликт=[], мешают=[])
        self.report.fields.pop("ворота", None)
        self.out.seek(0)
        self.out.truncate()


@contextlib.contextmanager
def capture(buffer: io.StringIO):
    """Текст шага — в вывод ветки, а не слиятелю: итог отдаёт его той подаче, чья ветка."""
    global OUT
    saved, OUT = OUT, buffer
    try:
        yield
    finally:
        OUT = saved


def crash_text() -> str:
    return "исключение на шагах ветки:\n" + "\n".join(traceback.format_exc().splitlines()[-12:])


def claim(ctx: Context, name: str, request: dict) -> Entry | None:
    """Забор: rename заявки/ → взяты/ атомарен; None — заявку успела отменить подача."""
    (ctx.queue_dir / "взяты").mkdir(parents=True, exist_ok=True)
    try:
        os.rename(ctx.queue_dir / "заявки" / name, ctx.queue_dir / "взяты" / name)
    except FileNotFoundError:
        return None
    request.update(слиятель=os.getpid(), orig_sha=rev(ctx.project, f"refs/heads/{request['ветка']}"))
    write_json_atomic(ctx.queue_dir / "взяты" / name, request)
    return Entry(ctx, name, request)


def claim_pack(ctx: Context) -> list[Entry]:
    """До BATCH_MAX заявок по порядку имён; заявка «одна» идёт пачкой из одной."""
    taken: list[Entry] = []
    for name in list_names(ctx.queue_dir / "заявки"):
        if len(taken) == ctx.batch_max:
            break
        request = read_request(ctx, "заявки", name)
        if request is None or (taken and request.get("одна")):
            if request is not None:
                break
            continue
        entry = claim(ctx, name, request)
        if entry is None:
            continue
        taken.append(entry)
        if request.get("одна"):
            break
    return taken


def finish(ctx: Context, entry: Entry, code: int, info: dict) -> None:
    """Итог ветке: строка журнала, затем файл итога (подача, увидев итог, находит и строку),
    затем снять взяты/."""
    entry.report.fields["пачка"] = info
    result = entry.report.finish(code)
    events.emit(SRC, f"mq.{result['итог']}", "ok" if code == 0 else "error", journal=MQ_JOURNAL,
                data=result)
    write_json_atomic(ctx.queue_dir / "итоги" / entry.name,
                      {"отчёт": result, "вывод": entry.out.getvalue()})
    entry.done = True
    beat(ctx)
    pause(ctx, "после_итога")
    (ctx.queue_dir / "взяты" / entry.name).unlink(missing_ok=True)


def send_back(ctx: Context, entry: Entry, info: dict, single: bool) -> None:
    """Назад в очередь под прежним именем (встанет в голову): ветка не тронута, у неё свои
    коммиты. single — виновного не нашли, дальше она идёт одна."""
    request = {**entry.request, "попыток": int(entry.request.get("попыток") or 0) + 1,
               "слиятель": None, "orig_sha": None,
               "одна": bool(entry.request.get("одна")) or single}
    write_json_atomic(ctx.queue_dir / "взяты" / entry.name, request)
    os.rename(ctx.queue_dir / "взяты" / entry.name, ctx.queue_dir / "заявки" / entry.name)
    entry.done = True
    data = {k: v for k, v in entry.report.fields.items() if k not in ("код", "итог")}
    data.update(ожидание_с=None, одна=request["одна"], попыток=request["попыток"], пачка=info)
    events.emit(SRC, "mq.возврат", "skipped", journal=MQ_JOURNAL, data=data)


def pack_id(name: str) -> str:
    """Имя заявки без «.json»: с семизначным pid маскировщик журнала (patterns.секреты_в_речи)
    принимает «<ns>-<pid>.json» за секрет и пишет «<СКРЫТО>» — строки пачки не связать."""
    return name.removesuffix(".json")


def lone_info(entry: Entry) -> dict:
    return {"id": pack_id(entry.name), "ветки": [entry.branch], "прогонов": 0}


def sweep_old_results(ctx: Context) -> None:
    """Итоги и отмены старше суток — снять: подача их уже не ждёт. битые/ и взяты/ не трогаем."""
    limit = time.time() - RESULT_TTL_SECONDS
    for sub in ("итоги", "отменены"):
        for name in list_names(ctx.queue_dir / sub):
            path = ctx.queue_dir / sub / name
            try:
                if path.stat().st_mtime < limit:
                    path.unlink()
            except FileNotFoundError:
                pass


def resolve_orphans(ctx: Context) -> None:
    """Всё во взяты/ при взятии замка — сироты убитого слиятеля, pid не проверяем (он
    переиспользуется). Итог есть — снять; нет — назад в очередь со счётчиком «сирота»;
    дважды — заявка роняет слиятеля, ей итог 6."""
    for name in list_names(ctx.queue_dir / "взяты"):
        if (ctx.queue_dir / "итоги" / name).exists():
            (ctx.queue_dir / "взяты" / name).unlink(missing_ok=True)
            continue
        request = read_request(ctx, "взяты", name)
        if request is None:
            continue
        request.update(сирота=int(request.get("сирота") or 0) + 1, слиятель=None)
        write_json_atomic(ctx.queue_dir / "взяты" / name, request)
        if request["сирота"] < ORPHAN_LIMIT:
            os.rename(ctx.queue_dir / "взяты" / name, ctx.queue_dir / "заявки" / name)
            continue
        entry = Entry(ctx, name, request)
        entry.report.fields["причина"] = "слиятель умер"
        with capture(entry.out):
            say("слиятель дважды умер на этой заявке")
        finish(ctx, entry, 6, lone_info(entry))


def prepare_pack(ctx: Context) -> None:
    """Шаги на пачку, до забора. Ветки заявок уборка не снимает; report_rollbacks — на пачку,
    а не на ветку: откат одного пути иначе писался бы несколько раз."""
    require_main_on_master(ctx)
    cleanup(ctx, queued_branches(ctx))
    report_rollbacks(ctx)


def reject_own(ctx: Context, own: str, stop: Stop) -> None:
    """Пачку собрать нельзя (основное не на master): итог — той заявке, которую ждёт слиятель."""
    request = read_request(ctx, "заявки", own)
    entry = claim(ctx, own, request) if request else None
    if entry is None:
        return
    with capture(entry.out):
        say(stop.message)
    finish(ctx, entry, stop.code, lone_info(entry))


# ───────────────────────────── пачка: цепочка, ворота, ff ─────────────────────────────

class GateRun:
    """Один прогон ворот на префиксе цепочки 1..j."""

    def __init__(self, count: int, ok: bool, fields: dict, text: str):
        self.count, self.ok, self.fields, self.text = count, ok, fields, text


class GateResult:
    """Что дала бисекция: зелёный префикс, остальные ветки цепочки (первая из них — виновная,
    если потолок не исчерпан), число прогонов."""

    def __init__(self, green: list, rest: list, ceiling: bool, runs: int):
        self.green, self.rest, self.ceiling, self.runs = green, rest, ceiling, runs


def is_ancestor(project: Path, sha: str, of: str) -> bool:
    return git(project, "merge-base", "--is-ancestor", sha, of).returncode == 0


def conflict_owner(chain: list, entry: Entry, top: str, base: str, where: Path) -> str:
    """С кем конфликт: с master, если ветка не ложится и на него одного, иначе — с соседом,
    чья вершина top."""
    if top == base:
        return "master"
    git(where, "checkout", "--detach", entry.orig)
    alone = git(where, "rebase", base)
    if alone.returncode:
        git(where, "rebase", "--abort")
        return "master"
    return next(e.branch for e in chain if e.tip == top)


def require_own_tree(ctx: Context, entry: Entry) -> None:
    """Путь дерева из заявки — дерево именно этой ветки: заявку пишет любой, кто может писать
    в LOG_DIR, а слиятель зовёт в этом пути `reset --keep`."""
    path = entry.tree["path"].resolve()
    if not any(t["path"].resolve() == path and t["branch"] == entry.branch for t in list_trees(ctx)):
        raise Stop(5, f"{path} — не дерево ветки {entry.branch}")


def extend_chain(ctx: Context, entry: Entry, chain: list, top: str, base: str, where: Path,
                 check_map: bool) -> str:
    """Ветка встаёт в цепочку над top; возвращает новую вершину. Цепочка — только во временном
    дереве: ветки и деревья помощников до вердикта не меняются."""
    if not entry.orig:
        raise Stop(5, f"ветки {entry.branch} нет")
    if is_ancestor(ctx.project, entry.orig, base):
        entry.fate = "in_master"
        return top
    require_own_tree(ctx, entry)
    require_clean(entry.tree)
    checkout = git(where, "checkout", "--detach", entry.orig)
    if checkout.returncode:
        raise RuntimeError(f"checkout {entry.orig}: {checkout.stderr.strip()}")
    try:
        rebase_onto(where, top)
    except Stop as stop:
        entry.report.fields.update(конфликт=stop.files,
                                   конфликт_с=conflict_owner(chain, entry, top, base, where))
        raise
    tip = rev(where, "HEAD")
    if tip == top:
        # Вклад ветки уже у соседей выше или в master (rebase выбросил ставшие пустыми коммиты).
        entry.fate = "empty" if top == base else "back"
        return top
    changes = branch_changes(ctx, top, tip)
    if check_map and any(path == MAP for _, path in changes):
        check_branch_map(where)
    entry.carry, entry.deleted = package_plan(ctx, tip, changes)
    entry.before, entry.tip, entry.changes = top, tip, changes
    entry.report.fields["дерево"] = rev(ctx.project, f"{tip}^{{tree}}") or ""
    chain.append(entry)
    return tip


def build_chain(ctx: Context, pack: list, base: str, check_map: bool) -> list:
    """Цепочка пачки над base во временном дереве; выпавшие ветки несут свой отказ."""
    where = ctx.mq_dir / f"mq-{os.getpid()}-chain"
    git(ctx.project, "worktree", "remove", "--force", str(where))
    proc = git(ctx.project, "worktree", "add", "--detach", str(where), base)
    if proc.returncode:
        raise Stop(6, f"временное дерево цепочки не создано: {proc.stderr.strip()}")
    chain: list = []
    top = base
    try:
        for entry in pack:
            with capture(entry.out):
                try:
                    top = extend_chain(ctx, entry, chain, top, base, where, check_map)
                except Stop as stop:
                    entry.fate, entry.stop = "dropped", stop
                    if stop.message:
                        say(stop.message)
                except Exception:  # noqa: BLE001 — шаг одной ветки не роняет пачку
                    entry.report.fields["причина"] = "исключение"
                    entry.fate, entry.stop = "dropped", Stop(6, crash_text())
                    say(entry.stop.message)
    finally:
        git(ctx.project, "worktree", "remove", "--force", str(where))
    return chain


def run_prefix(ctx: Context, base: str, chain: list, count: int, shift: tuple = ()) -> GateRun:
    """Ворота на префиксе 1..count: красные — не исключение, а прогон с текстом и полями.
    shift — диапазоны сдвига master к выбору префикса (см. gate_chain)."""
    scratch, buffer, ok = Report("-"), io.StringIO(), True
    tip = chain[count - 1].tip
    with capture(buffer):
        try:
            run_gates(ctx, base, tip, scratch, [f"{base}..{tip}", *shift])
        except Stop as stop:
            if stop.code != 3:
                raise
            ok = False
            say(stop.message)
    run = GateRun(count, ok, scratch.fields, buffer.getvalue())
    last = chain[count - 1]
    last.report.fields.update(ворота_с=run.fields.get("ворота_с"), выбор_с=run.fields.get("выбор_с"))
    if "ворота" in run.fields:
        last.report.fields["ворота"] = run.fields["ворота"]
    for entry in chain[:count]:
        entry.report.fields["выбор"] = run.fields.get("выбор", [])
    if not ok:
        last.red_run = run
    return run


def gate_chain(ctx: Context, base: str, chain: list, shift: tuple = ()) -> GateResult:
    """Бисекция «убрать последнюю»: красный префикс 1..j — ворота на 1..j−1, пока не зелено,
    пока не остался один или пока не исчерпан BISECT_MAX перепрогонов (виновного нет).
    shift — диапазон сдвига prev_tip..tip в выбор КАЖДОГО прогона: проверка, задетая только
    сдвигом, в выбор ветки не входит, и без него круг слил бы при известной красной."""
    spare, count = ctx.bisect_max, 0
    size = len(chain)
    while size >= 1:
        run = run_prefix(ctx, base, chain, size, shift)
        count += 1
        if run.ok:
            return GateResult(chain[:size], chain[size:], False, count)
        if size == 1:
            break
        if spare == 0:
            return GateResult([], chain, True, count)
        spare, size = spare - 1, size - 1
    return GateResult([], chain, False, count)


def move_branch(entry: Entry, target: str) -> bool:
    """Ветку помощника — на влитую вершину, если он не коммитил после подачи. reset --keep
    не трогает незакоммиченное: отказ — работа влита, ветка остаётся как была."""
    where = entry.tree["path"]
    if rev(where, "HEAD") != entry.orig:
        say("коммиты после подачи не влиты")
        return False
    proc = git(where, "reset", "--keep", target)
    if proc.returncode == 0:
        return True
    note = "ветка не переставлена: незакоммиченное пересекается с влитым"
    say(f"{proc.stderr.strip()}\n{note}")
    entry.report.fields["оставлено"] += [proc.stderr.strip(), note]
    return False


def finish_tree(ctx: Context, entry: Entry) -> None:
    try:
        tree = find_tree(ctx, entry.branch)
    except Stop:
        return
    left = clean_helper_tree(ctx, tree)
    if left:
        say(left)
        entry.report.fields["оставлено"].append(left)


def guarded(entry: Entry, steps) -> int:
    """Шаги ветки после ff: исключение — итог 6 этой ветке, пачка идёт дальше."""
    with capture(entry.out):
        try:
            return steps()
        except Exception:  # noqa: BLE001
            entry.report.fields["причина"] = "исключение"
            say(crash_text())
            return 6


def deliver_merged(ctx: Context, entry: Entry) -> int:
    """Ветка влита ff: пакет, ключи, дерево. Код итога."""
    fields = entry.report.fields
    fields.update(sha_до=entry.before, sha_после=entry.tip, файлов=len(entry.changes))

    def steps() -> int:
        moved = move_branch(entry, entry.tip)
        remember_paths(ctx, entry.branch, entry.before, entry.tip, entry.changes)
        if entry.deleted:
            say("удалены в ветке, в пакете остаются (решает человек): " + ", ".join(entry.deleted))
        fields["пакет_файлов"], failed_command = sync_package(
            ctx, entry.carry, entry.branch, entry.before, entry.tip)
        keys_refusal = deliver_new_keys(ctx, entry.before, entry.tip, entry.report)
        if moved:
            finish_tree(ctx, entry)
        if failed_command:
            say("слито, но пакет не закоммичен; доделать:\n" + failed_command)
            return 9
        if keys_refusal:
            say(keys_refusal)
            return 10
        return 0

    return guarded(entry, steps)


def deliver_settled(ctx: Context, entry: Entry, base: str) -> int:
    """Вклад ветки уже в master (слиятель убит после ff или помощник сдал слитое): итог 0
    без ворот; пустой вклад — ветку на base, иначе её дерево не уберётся никогда."""
    entry.report.fields.update(sha_до=base, sha_после=base, файлов=0)

    def steps() -> int:
        if entry.fate != "empty" or move_branch(entry, base):
            finish_tree(ctx, entry)
        return 0

    return guarded(entry, steps)


def reject_green(entry: Entry, stop: Stop) -> int:
    """ff не состоялся: зелёному префиксу — отказ git (код 4/6), ветки не тронуты."""
    with capture(entry.out):
        say(stop.message)
    if stop.code == 4:
        entry.report.fields["мешают"] = stop.files
    return stop.code


def blame(entry: Entry) -> int:
    """Виновная: поля и хвост ворот того прогона, где она последняя."""
    run = entry.red_run
    keys = ("причина", "красные", "ступень", "красны_на_master")
    entry.report.fields.update({k: run.fields[k] for k in keys if k in run.fields})
    entry.out.write(run.text)
    return 3


def narrow_regate(ctx: Context, base: str, chain: list, pack: list, span: str) -> bool:
    """Узкий повтор на вершине: pre-commit (судит дерево целиком и дешевле выбора) и выбор только
    по сдвигу span, правила — из дерева вершины. Поля прогона — в `узкий`, а не в `выбор`: выбор
    ветки судил прошлый круг, и итог его не теряет."""
    scratch, buffer, ok = Report("-"), io.StringIO(), True
    with capture(buffer):
        try:
            run_gates(ctx, base, chain[-1].tip, scratch, [span])
        except Stop as stop:
            if stop.code != 3:
                raise
            ok = False
            say(stop.message)
    narrow = {k: scratch.fields[k] for k in ("выбор", "красные", "ступень") if k in scratch.fields}
    chain[-1].report.fields.update(ворота_с=scratch.fields["ворота_с"], выбор_с=scratch.fields["выбор_с"])
    for entry in pack:
        entry.report.fields["узкий"] = narrow
        entry.report.fields["повторов_без_выбора"] += int(ok and not narrow["выбор"])
    if not ok:
        chain[-1].out.write(buffer.getvalue())
    return ok


def judge_round(ctx: Context, base: str, chain: list, pack: list, last) -> tuple:
    """Ворота круга → (итог, вершина судимого дерева). last — (имена, вершина, выборы веток)
    прошлого зелёного, но не влитого круга; None — первый круг или прошлый не зелёный целиком.

    Писатели карты и оркестратор двигают master каждые минуты, а круг ворот — 25–30 мин под
    нагрузкой: полный повтор на каждом сдвиге стоил ≈ 9,7 ч слиятеля за 48 ч. Порядок ветвей
    важен: «только картой» — раньше правил выбора, иначе стенды без ключа шли бы полным кругом."""
    tip = chain[-1].tip if chain else None
    # Пропуск или узкий повтор — только для той же цепочки, что судили: на повторе ветка могла
    # выпасть, и остаток никто не судил. Судимая цепочка непуста, так что пустая — тоже другая.
    if last is None or [e.name for e in chain] != last[0]:
        if last is not None:
            say("цепочка изменилась после сдвига master: ворота повторяю")
        return gate_chain(ctx, base, chain), tip
    judged_tip = last[1]
    # Выбор ветки судил прошлый круг; begin_attempt его обнулил, а итог должен его нести.
    for entry, chosen in zip(chain, last[2]):
        entry.report.fields["выбор"] = chosen
    shift = shift_paths(ctx.project, judged_tip, tip)
    if shift == [MAP]:
        # Коммит карты прошёл свой pre-commit, карту ветки сверил build_chain; разность копится
        # от последнего судимого дерева.
        say("master сдвинут только картой: ворота не повторяю")
        for entry in pack:
            entry.report.fields["повторов_без_ворот"] += 1
        return GateResult(chain, [], False, 0), judged_tip
    span = (f"{judged_tip}..{tip}",)
    if shift is None or rules_moved(ctx.project, (base, judged_tip), shift):
        say("сдвиг master задел правила выбора: полный круг")
    elif narrow_regate(ctx, base, chain, pack, span[0]):
        return GateResult(chain, [], False, 1), tip
    # Тот же base и та же итерация: повтор не тратится, красная узкого входит в каждый прогон.
    return gate_chain(ctx, base, chain, span), tip


def run_pack(ctx: Context, pack: list, info: dict) -> None:
    retries, last = 0, None
    while True:
        # Карта коммитится до цепочки, чтобы ворота шли один раз; невалидную или под долгим
        # локом оставляем — мешает ли она, решит слияние.
        refusal = commit_dirty_map(ctx, pack[0].branch)
        if refusal:
            say(refusal)
        base = rev(ctx.project, "master")
        for entry in pack:
            entry.begin_attempt(base)
        # Карта ветки сверяется на любом повторе: «только картой» решается уже после цепочки, а её
        # дерево build_chain снимает.
        chain = build_chain(ctx, pack, base, check_map=last is not None)
        started = time.monotonic()
        result, judged_tip = judge_round(ctx, base, chain, pack, last)
        spent = time.monotonic() - started
        if retries:
            for entry in pack:
                entry.report.fields["повторы_с"] = round(entry.report.fields["повторы_с"] + spent, 1)
        info["прогонов"] += result.runs
        stop = None
        if result.green:
            try:
                merged = fast_forward(ctx, result.green[-1].tip)
            except Stop as refused:
                stop, merged = refused, False
            if not merged and stop is None:
                pause(ctx, "после_отказа_ff")
                if retries < ctx.retries:
                    retries += 1
                    last = None if result.rest else ([e.name for e in chain], judged_tip,
                                                     [e.report.fields["выбор"] for e in chain])
                    continue
                stop = Stop(6, f"master уходит из-под слияния: предел повторов {ctx.retries}")
        break
    for entry in pack:
        entry.report.fields["повторов"] = retries
    if result.green and stop is None:
        pause(ctx, "после_ff")
    for entry in result.green:
        finish(ctx, entry, deliver_merged(ctx, entry) if stop is None else reject_green(entry, stop), info)
    settle_rest(ctx, pack, result, base, info)


def settle_rest(ctx: Context, pack: list, result: GateResult, base: str, info: dict) -> None:
    """Все, кроме влитых: выпавшие — со своим кодом, виновная — 3, хвост — назад в очередь."""
    guilty = None if result.ceiling or not result.rest else result.rest[0]
    for entry in pack:
        if entry.done:
            continue
        if entry.fate == "dropped":
            finish(ctx, entry, entry.stop.code, info)
        elif entry.fate in ("in_master", "empty"):
            finish(ctx, entry, deliver_settled(ctx, entry, base), info)
        elif entry is guilty:
            finish(ctx, entry, blame(entry), info)
        else:
            send_back(ctx, entry, info, single=result.ceiling and entry in result.rest)


def run_pack_safely(ctx: Context, pack: list) -> None:
    """Исключение вне шагов одной ветки: не получившие итог — назад в очередь, слиятель
    выходит с кодом 6 (сирота не растёт: заявка не виновата)."""
    info = {"id": pack_id(pack[0].name), "ветки": [e.branch for e in pack], "прогонов": 0}
    try:
        run_pack(ctx, pack, info)
    except Exception as exc:  # noqa: BLE001
        trace = "\n".join(traceback.format_exc().splitlines()[-12:])
        for entry in pack:
            if not entry.done:
                send_back(ctx, entry, info, single=False)
        raise Stop(6, f"слиятель упал вне шагов ветки, заявки вернулись в очередь:\n{trace}") from exc


def release_due(ctx: Context, own: str) -> bool:
    """Замок отпускается, когда заявка слиятеля получила итог или пропала либо очередь пуста:
    иначе его вызывающий простаивал бы, а publish и memory-commit голодали."""
    return locate(ctx, own) != "заявки" or not list_names(ctx.queue_dir / "заявки")


def serve(ctx: Context, own: str) -> None:
    """Слиятель: пачка за пачкой, пока не получит итог заявка, которую ждёт его вызывающий."""
    beat(ctx)
    sweep_old_results(ctx)
    resolve_orphans(ctx)
    while True:
        try:
            prepare_pack(ctx)
        except Stop as stop:
            reject_own(ctx, own, stop)
            break
        pause(ctx, "перед_забором")
        pack = claim_pack(ctx)
        if not pack:
            break
        beat(ctx)
        pause(ctx, "после_забора")
        run_pack_safely(ctx, pack)
        if release_due(ctx, own):
            break
    pause(ctx, "перед_отпуском")


# ───────────────────────────── подача ─────────────────────────────

class QueueStop(Stop):
    """Отказ подачи с полями в объект --json; journal — писать ли строку (отмену заявки, которую
    ждут две подачи, пишет одна)."""

    def __init__(self, code: int, message: str, fields: dict, journal: bool = True):
        super().__init__(code, message)
        self.fields, self.journal = fields, journal


def try_lock(ctx: Context):
    ctx.log_dir.mkdir(parents=True, exist_ok=True)
    handle = open(ctx.log_dir / "merge-queue.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def read_result(ctx: Context, name: str) -> dict | None:
    try:
        return json.loads((ctx.queue_dir / "итоги" / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def stalled_refusal(ctx: Context, requested: float, journal: bool) -> QueueStop:
    return QueueStop(8, f"очередь не двигалась дольше {ctx.wait_seconds / 60:g} мин, заявка снята",
                     {"ожидание_с": round(time.time() - requested, 1), "предел_с": ctx.wait_seconds},
                     journal)


def cancel_request(ctx: Context, name: str) -> bool:
    (ctx.queue_dir / "отменены").mkdir(parents=True, exist_ok=True)
    try:
        os.rename(ctx.queue_dir / "заявки" / name, ctx.queue_dir / "отменены" / name)
    except FileNotFoundError:
        return False
    return True


def stalled(ctx: Context, requested: float) -> bool:
    """Очередь не двигалась дольше предела: от пульса слиятеля, а не от подачи — вернувшиеся
    и стоящие в хвосте за честной пачкой кодом 8 не наказываются."""
    try:
        pulse = (ctx.queue_dir / "пульс").stat().st_mtime
    except FileNotFoundError:
        pulse = 0.0
    return time.time() - max(pulse, requested) > ctx.wait_seconds


def await_result(ctx: Context, name: str, requested: float) -> dict:
    """Итог заявки: ждём, а при свободном замке сливаем сами."""
    while True:
        result = read_result(ctx, name)
        if result is not None:
            return result
        where = locate(ctx, name)
        if where == "итоги":
            time.sleep(0.05)
            continue
        if where in (None, "битые"):
            raise QueueStop(6, "заявка пропала из очереди", {"причина": "заявка пропала"})
        if where == "отменены":
            raise stalled_refusal(ctx, requested, journal=False)
        handle = try_lock(ctx)
        if handle is not None:
            try:
                serve(ctx, name)
            except Stop:
                raise
            except Exception as exc:  # noqa: BLE001
                # Без перевода трасса ушла бы кодом 1 — зовущий прочёл бы «конфликт».
                trace = "\n".join(traceback.format_exc().splitlines()[-12:])
                raise Stop(6, f"слиятель упал вне пачки:\n{trace}") from exc
            finally:
                handle.close()
            continue
        if where == "заявки" and stalled(ctx, requested):
            if cancel_request(ctx, name):
                raise stalled_refusal(ctx, requested, journal=True)
            continue
        time.sleep(POLL_SECONDS)


def submit(ctx: Context, target: str, report: Report) -> dict:
    """Подача: цель разбирает она (у слиятеля другой cwd), заявка — файлом, дальше ждём итог."""
    require_main_on_master(ctx)
    tree = find_tree(ctx, target)
    report.fields["ветка"] = tree["branch"]
    require_clean(tree)
    found = find_request(ctx, tree["branch"])
    if found:
        name, request = found
    else:
        name = f"{time.time_ns()}-{os.getpid()}.json"
        request = {"ветка": tree["branch"], "дерево": str(tree["path"]), "pid": os.getpid(),
                   "подано": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                   "попыток": 0, "одна": False, "сирота": 0, "слиятель": None, "orig_sha": None}
        write_json_atomic(ctx.queue_dir / "заявки" / name, request)
    return await_result(ctx, name, requested_at(request))


def tidy(ctx: Context, report: Report) -> None:
    found = cleanup(ctx, queued_branches(ctx))
    report.fields["оставлено"] = found["оставлено"]
    say("убрано: " + (", ".join(found["убрано"]) or "ничего"))
    for line in found["оставлено"]:
        say(f"оставлено: {line}")
    notes = [line.rsplit(": ", 1)[-1] for line in found["оставлено"]]
    # Оставленные по возрасту — отдельно: `git status` в дереве освежает mtime индекса,
    # и мерило «0 слитых чистых старше срока» без этого числа не сверить.
    say(f"убрано {len(found['убрано'])}, оставлено {len(found['оставлено'])} "
        f"(грязных {notes.count(DIRTY_NOTE)}, неслитых {len(found['неслитых'])}, "
        f"в очереди {notes.count(QUEUED_NOTE)}); не простояли срок: {notes.count(IDLE_NOTE)}")
    say(f"неслитых веток {len(found['неслитых'])}: " + ", ".join(found["неслитых"]))


def run_tidy(ctx: Context, report: Report) -> None:
    lock, waited = take_lock(ctx)
    try:
        report.fields["ожидание_с"] = round(waited, 1)
        tidy(ctx, report)
    finally:
        lock.close()


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
    if HERE.is_relative_to(ctx.mq_dir):
        # Копия из дерева помощника сняла бы под собой свои модули — итог упал бы без
        # JSON; слияние ведёт копия основного дерева.
        os.execv(sys.executable, [sys.executable, str(ctx.project / SRC), *argv])
    report = Report("-" if args.tidy else args.target, tidy=args.tidy)
    result, journal = None, True
    try:
        if args.tidy:
            run_tidy(ctx, report)
        else:
            result = submit(ctx, args.target, report)
    except Stop as stop:
        if stop.message:
            say(stop.message)
        report.fields.update(getattr(stop, "fields", {}))
        if hasattr(stop, "waited"):
            report.fields["ожидание_с"] = round(stop.waited, 1)
        journal = getattr(stop, "journal", True)
        report.finish(stop.code)
    else:
        if result is None:
            report.finish(0)
    if result:
        # Итог и текст ветки писал слиятель (строка журнала — тоже его).
        OUT.write(result["вывод"])
        fields, journal = result["отчёт"], False
    else:
        fields = report.fields
    code = fields["код"]
    if journal:
        events.emit(SRC, f"mq.{fields['итог']}", "ok" if code == 0 else "error", journal=MQ_JOURNAL,
                    data=fields)
    if args.as_json:
        print(json.dumps(fields, ensure_ascii=False), file=sys.stdout)
    else:
        sha = lambda value: (value or "")[:10]  # noqa: E731
        print(f"merge-queue: {fields['итог']} {fields['ветка']} "
              f"{sha(fields['sha_до'])}..{sha(fields['sha_после'])}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
