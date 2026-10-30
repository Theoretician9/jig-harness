#!/usr/bin/env python3
"""Probes of `archive-map.py --снять … --причина` (task hr.watchers-report-
must-create-model-task, plan Ф0, Task 0.1).

The duplicate card hr.audit-owner-only-watcher-reports leaves the map with the
reason «слита в hr.watchers-report-must-create-model-task», by the plan's own
command under the map lock:

    flock "$LOG_DIR/locks/services/dev-map.lock" \
        python3 scripts/archive-map.py --снять <id> --причина "слита в <id>"

The archiver resolves its map and archive from its OWN location
(КОРЕНЬ = parent of scripts/), so every case runs a COPY of it inside a stand
tree: the live map is compared by sha1 before and after.

Door for mutation runs ([[probe-loads-the-live-mechanism]]):
    HARNESS_ARCHIVE_MAP   path of archive-map.py under test
Last line: `archive-map --причина: случаев N, падений F`.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

ARCHIVER = Path(os.environ.get("HARNESS_ARCHIVE_MAP") or HERE / "archive-map.py")
LIVE_MAP = HERE.parent / "dev-map.yaml"
REASON = "слита в hr.watchers-report-must-create-model-task"
DEFAULT_REASON = "срок находки истёк"

MAP = """version: 1
queue: []
lines:
  - id: harness
    layer: infra
    name: "Харнес"
    tasks:
      - id: hr.audit-dup
        status: plan
        owner_testable: false
        title: "дубль"
        summary: "образец"

      - id: hr.keep
        status: wip
        title: "живая"
        summary: "образец"
      - id: hr.other
        status: plan
        title: "соседка"
        summary: "образец"
"""

cases = Cases()
check = cases.check


def sha1(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return "нет файла"


class Stand:
    def __init__(self, base: Path):
        self.root = base
        (base / "scripts").mkdir(parents=True)
        (base / "docs").mkdir()
        shutil.copytree(HERE / "lib", base / "scripts" / "lib",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(ARCHIVER, base / "scripts" / "archive-map.py")
        self.map = base / "dev-map.yaml"
        self.archive = base / "docs" / "dev-map-archive.yaml"
        self.lock = base / "dev-map.lock"
        self.map.write_text(MAP, encoding="utf-8")

    def run(self, *args: str, under_flock: bool = False) -> tuple[int, str]:
        env = dict(os.environ)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [sys.executable, str(self.root / "scripts" / "archive-map.py"), *args]
        if under_flock:
            command = ["flock", "-w", "20", str(self.lock), *command]
        try:
            done = subprocess.run(command, capture_output=True, text=True, env=env,
                                  timeout=60, cwd=str(self.root))
        except subprocess.TimeoutExpired:
            return -9, "предел 60 с истёк"
        return done.returncode, done.stdout + done.stderr

    def archived(self) -> dict:
        if not self.archive.exists():
            return {}
        data = yaml.safe_load(self.archive.read_text(encoding="utf-8")) or {}
        return {t.get("id"): t for line in data.get("lines") or [] for t in line.get("tasks") or []}

    def map_ids(self) -> set:
        data = yaml.safe_load(self.map.read_text(encoding="utf-8")) or {}
        return {t.get("id") for line in data.get("lines") or [] for t in line.get("tasks") or []}


def main() -> int:
    live_before = sha1(LIVE_MAP)
    with tempfile.TemporaryDirectory(prefix="archive-map-probe-") as tmp:
        print("── --снять с --причина (Ф0, Task 0.1) ──")
        st = Stand(Path(tmp) / "reason")
        rc, out = st.run("--снять", "hr.audit-dup", "--причина", REASON, under_flock=True)
        check("--снять id --причина «…» под flock лока карты — код 0", 0, rc)
        check("снятая ушла из карты, соседи целы", {"hr.keep", "hr.other"}, st.map_ids())
        card = st.archived().get("hr.audit-dup") or {}
        check("в архиве closed_reason — названная причина", REASON, card.get("closed_reason"))
        check("в архиве у снятой есть снято_когда", True, bool(card.get("снято_когда")))
        check("текст причины id задачей не стал (в архиве нет «--причина»)", False,
              "--причина" in (st.archive.read_text(encoding="utf-8") if st.archive.exists() else ""))
        check("печать «снято: 1»", True, "снято: 1" in out)

        st = Stand(Path(tmp) / "default")
        rc, out = st.run("--снять", "hr.audit-dup")
        card = st.archived().get("hr.audit-dup") or {}
        check("без --причина — прежняя «срок находки истёк» (код 0)", (0, DEFAULT_REASON),
              (rc, card.get("closed_reason")))

        st = Stand(Path(tmp) / "two")
        rc, out = st.run("--снять", "hr.audit-dup", "hr.other", "--причина", REASON)
        archived = st.archived()
        check("два id и --причина: оба сняты с причиной", (0, [REASON, REASON]),
              (rc, [(archived.get(i) or {}).get("closed_reason") for i in ("hr.audit-dup", "hr.other")]))

        st = Stand(Path(tmp) / "empty-reason")
        before = sha1(st.map)
        rc, out = st.run("--снять", "hr.audit-dup", "--причина")
        check("--причина без текста — отказ (код ≠ 0), карта не тронута", (True, before),
              (rc not in (0, -9), sha1(st.map)))
        # A crash is not a refusal: an IndexError also answers non-zero.
        check("--причина без текста — отказ своим словом, без трассировки", (True, False),
              ("нужен ровно один текст" in out, "Traceback" in out))

        st = Stand(Path(tmp) / "blank-reason")
        before = sha1(st.map)
        rc, out = st.run("--снять", "hr.audit-dup", "--причина", "   ")
        check("--причина из одних пробелов — отказ, карта не тронута", (1, before, False),
              (rc, sha1(st.map), st.archive.exists()))

        st = Stand(Path(tmp) / "unknown")
        before = sha1(st.map)
        rc, out = st.run("--снять", "hr.no-such", "--причина", REASON)
        check("неизвестный id с --причина — код 1, карта и архив не тронуты", (1, before, False),
              (rc, sha1(st.map), st.archive.exists()))
    check("боевая карта не тронута", live_before, sha1(LIVE_MAP))
    print(f"archive-map --причина: случаев {cases.paths}, падений {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
