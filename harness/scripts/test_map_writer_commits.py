#!/usr/bin/env python3
"""Probe: the shared map writer commits its own cards.

Case of 15.11.2026: alert-task.py and evolution-net.py wrote dev-map.yaml in the
MAIN tree through `cards.write_cards_locked` and left it uncommitted; the merge
queue refused every helper with code 4 until the orchestrator committed by hand.
The probe builds its own git repository (the stand), installs the project's
post-commit hook into it and judges what the queue needs: after a write the
map in HEAD equals the map on disk, and `git merge --ff-only` of a branch that
touches the map goes through.

The library is found NEXT TO this file (mutation runs call the probe from a
copy of the tree). The live map is compared by sha1 before and after.

    python3 scripts/test_map_writer_commits.py
Last line: `map-writer-commits: случаев N, падений F`.
"""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import cards  # noqa: E402
from selftest_cases import Cases  # noqa: E402

LIVE_MAP = HERE.parent / "dev-map.yaml"
HEAL_HOOK = HERE / "post-commit-heal-index.sh"

cases = Cases()
check = cases.check

MAP = """lines:
  - id: harness
    name: "Харнес"
    tasks:
      - id: hr.old
        status: plan
        title: "старая"
  - id: other
    name: "Другая"
    tasks:
      - id: ot.old
        status: plan
        title: "другая"
"""


def card(card_id: str) -> list[str]:
    return [f"      - id: {card_id}\n", "        status: plan\n", '        title: "новая"\n']


def sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest() if path.exists() else "нет"


def git(repo: Path, *args: str, check_rc: bool = True) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"})
    if check_rc and done.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


class Stand:
    """Own repository with a tracked map and the project's post-commit hook."""

    def __init__(self, base: Path, name: str):
        self.repo = base / name
        self.repo.mkdir(parents=True)
        self.map = self.repo / "dev-map.yaml"
        self.lock = base / f"{name}.lock"
        git(self.repo, "init", "-q", "-b", "master")
        git(self.repo, "config", "user.email", "p@p")
        git(self.repo, "config", "user.name", "proba")
        hooks = self.repo / ".git" / "hooks"
        (hooks / "post-commit").symlink_to(HEAL_HOOK)
        self.map.write_text(MAP, encoding="utf-8")
        (self.repo / "README").write_text("сосед карты\n", encoding="utf-8")
        git(self.repo, "add", "dev-map.yaml", "README")
        git(self.repo, "commit", "-qm", "стенд")

    def write(self, card_id: str, line: str = "harness") -> tuple[bool, str]:
        err = io.StringIO()
        with redirect_stderr(err):
            done = cards.write_cards_locked(self.map, line, [card(card_id)],
                                            lock_file=self.lock)
        return done, err.getvalue()

    def head_map(self) -> str:
        return git(self.repo, "show", "HEAD:dev-map.yaml")

    def commits(self) -> int:
        return int(git(self.repo, "rev-list", "--count", "HEAD").strip())

    def dirty(self) -> str:
        return git(self.repo, "status", "--porcelain").strip()


def section_commits(tmp: Path) -> None:
    print("── запись карточки ложится коммитом ──")
    st = Stand(tmp, "plain")
    done, _ = st.write("hr.new-card")
    check("запись состоялась", True, done)
    check("БОЛЬНОЙ СЛУЧАЙ 15.11: коммитов стало на один больше", 2, st.commits())
    check("карта в HEAD = карта на диске", st.map.read_text(encoding="utf-8"), st.head_map())
    check("дерево чистое (общий индекс вылечен хуком)", "", st.dirty())
    check("коммит меняет только карту, соседи на месте", ["README", "dev-map.yaml"],
          git(st.repo, "ls-tree", "--name-only", "HEAD").split())
    check("дифф коммита — одна карта", ["dev-map.yaml"],
          git(st.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").split())
    message = git(st.repo, "log", "-1", "--format=%B")
    check("в сообщении трейлер Dev-Map новой карточки", True,
          "Dev-Map: hr.new-card" in message)
    check("в теме назван писатель", True, Path(sys.argv[0]).name in message.splitlines()[0])


def section_merge_queue(tmp: Path) -> None:
    print("── очередь слияния после записи: ff-only ветки, трогающей карту ──")
    st = Stand(tmp, "queue")
    branch_tree = tmp / "queue-helper"
    git(st.repo, "worktree", "add", "-q", "-b", "helper", str(branch_tree))
    st.write("hr.alert-demo")
    git(branch_tree, "rebase", "-q", "master")
    helper_map = branch_tree / "dev-map.yaml"
    helper_map.write_text(helper_map.read_text(encoding="utf-8").replace(
        'title: "другая"', 'title: "другая, правка помощника"'), encoding="utf-8")
    git(branch_tree, "commit", "-qam", "правка помощника")
    merged = subprocess.run(["git", "-C", str(st.repo), "merge", "--ff-only", "helper"],
                            capture_output=True, text=True)
    check("merge --ff-only ветки помощника проходит (было: код 4 «мешают-правки»)",
          0, merged.returncode)


def section_foreign_edit(tmp: Path) -> None:
    print("── чужая незакоммиченная правка карты не уезжает в коммит писателя ──")
    st = Stand(tmp, "foreign")
    st.map.write_text(st.map.read_text(encoding="utf-8").replace(
        'title: "другая"', 'title: "чужая правка"'), encoding="utf-8")
    st.write("hr.own")
    head = st.head_map()
    check("своя карточка в HEAD", True, "- id: hr.own" in head)
    check("чужая правка в HEAD не попала", False, "чужая правка" in head)
    check("чужая правка жива на диске", True, "чужая правка" in st.map.read_text(encoding="utf-8"))
    check("своя карточка на диске", True, "- id: hr.own" in st.map.read_text(encoding="utf-8"))


def section_under_lock(tmp: Path) -> None:
    print("── коммит идёт под тем же локом карты ──")
    st = Stand(tmp, "locked")
    seen = tmp / "locked-seen"
    hook = st.repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f'#!/bin/sh\nif flock -n "{st.lock}" true; then echo free; else echo held; fi'
                    f' > "{seen}"\n', encoding="utf-8")
    hook.chmod(0o755)
    st.write("hr.locked")
    check("во время коммита лок карты держит писатель", "held",
          seen.read_text().strip() if seen.exists() else "хук не звался")


def section_commit_refused(tmp: Path) -> None:
    print("── коммит отвергнут: карточка не теряется, писатель не падает ──")
    st = Stand(tmp, "refused")
    hook = st.repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    done, err = st.write("hr.refused")
    check("запись состоялась", True, done)
    check("карточка на диске", True, "- id: hr.refused" in st.map.read_text(encoding="utf-8"))
    check("коммита нет", 1, st.commits())
    check("отказ коммита назван в stderr", True, "коммит" in err)


def section_untracked(tmp: Path) -> None:
    print("── карта вне git: запись как прежде, коммита нет ──")
    plain = tmp / "untracked"
    plain.mkdir()
    (plain / "dev-map.yaml").write_text(MAP, encoding="utf-8")
    err = io.StringIO()
    with redirect_stderr(err):
        done = cards.write_cards_locked(plain / "dev-map.yaml", "harness", [card("hr.nogit")],
                                        lock_file=tmp / "untracked.lock")
    check("запись состоялась", True, done)
    check("молча: карта вне git — не отказ", "", err.getvalue())


SECTIONS = (section_commits, section_merge_queue, section_foreign_edit, section_under_lock,
            section_commit_refused, section_untracked)


def main() -> int:
    live_before = sha1(LIVE_MAP)
    with tempfile.TemporaryDirectory(prefix="map-writer-commits-") as tmp:
        for section in SECTIONS:
            try:
                section(Path(tmp))
            except Exception as error:  # a crashed section is a failure, not a silence
                check(f"раздел {section.__name__} дошёл до конца", "дошёл",
                      f"упал: {type(error).__name__}: {error}")
    check("боевая карта не тронута", live_before, sha1(LIVE_MAP))
    print(f"map-writer-commits: случаев {cases.paths}, падений {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
