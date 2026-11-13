#!/usr/bin/env python3
"""Probes of the alert-queue drain in sentinel.sh, written BEFORE its code.

Task hr.watchers-report-must-create-model-task (spec §3.2 p.9, R-1; plan
Task 1.7). A watcher that met a busy map lock leaves its intent in
`$LOG_DIR/тревоги-ждут.jsonl` (code 4). The next writer call drains it — but a
watcher may stay silent for hours, so sentinel (systemd timer, every 5 min)
calls `alert-task.py --drain` every run. A failing writer must not stop
sentinel: it reaches its end and says the code out loud.

Sentinel runs through its OWN entry point on a stand: own passport, own
LOG_DIR and heartbeat directory, own map, a stub `systemctl` that answers
«active» (the real units are never asked or restarted), fresh warden marks
(so sentinel does not raise the warden), no channel door (a message falls into
sentinel.log only). The live map is compared by sha1 before and after.

    python3 scripts/test_sentinel_drain.py

Door for mutation runs ([[probe-loads-the-live-mechanism]]):
    HARNESS_SENTINEL   path of sentinel.sh under test
Last line: `sentinel drain: случаев N, падений F`.
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
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import test_alert_task as alerts  # noqa: E402  (the stand of the alert writer probes)
from selftest_cases import Cases  # noqa: E402


def default_sentinel() -> Path:
    """harness/demons in the project, demons/ beside scripts/ in the package."""
    for candidate in (HERE.parent / "harness" / "demons" / "sentinel.sh",
                      HERE.parent / "demons" / "sentinel.sh"):
        if candidate.is_file():
            return candidate
    return HERE.parent / "harness" / "demons" / "sentinel.sh"


SENTINEL = Path(os.environ.get("HARNESS_SENTINEL") or default_sentinel())

cases = Cases()
check = cases.check

STUB_SYSTEMCTL = """#!/usr/bin/env bash
case "$1" in
    is-active) echo active ;;
    *) : ;;
esac
exit 0
"""

BROKEN_WRITER = """#!/usr/bin/env python3
import sys
print("писатель пробы сломан", file=sys.stderr)
sys.exit(3)
"""


class SentinelStand(alerts.Stand):
    def __init__(self, base: Path):
        super().__init__(base)
        scripts = self.project / "scripts"
        shutil.copytree(HERE / "lib", scripts / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        # The writer loads its neighbours (limit_mark, board, transliterate) from
        # its own directory: every top-level script goes along, not a guessed list.
        for neighbour in HERE.glob("*.py"):
            shutil.copy(neighbour, scripts / neighbour.name)
        shutil.copy(alerts.WRITER, scripts / "alert-task.py")
        self.bin = base / "bin"
        self.bin.mkdir()
        (self.bin / "systemctl").write_text(STUB_SYSTEMCTL, encoding="utf-8")
        (self.bin / "systemctl").chmod(0o755)
        self.heartbeat = base / "heartbeat"
        for mark in ("session-warden.запуск", "session-warden"):
            (self.heartbeat / mark).touch()

    def break_writer(self) -> None:
        (self.project / "scripts" / "alert-task.py").write_text(BROKEN_WRITER, encoding="utf-8")

    def sentinel(self) -> dict:
        started = time.time()
        env = self.env(PATH=f"{self.bin}:{os.environ.get('PATH', '')}",
                       SENTINEL_UNITS="harness-agent", HEARTBEAT_DIR=str(self.heartbeat))
        try:
            done = subprocess.run(["bash", str(SENTINEL)], env=env, capture_output=True,
                                  text=True, timeout=120, cwd=str(self.base))
            rc, out = done.returncode, done.stdout + done.stderr
        except subprocess.TimeoutExpired:
            rc, out = -9, "предел 120 с истёк"
        mark = self.heartbeat / "sentinel"
        log = self.log / "sentinel.log"
        return {"rc": rc, "out": out,
                "log": log.read_text(encoding="utf-8") if log.exists() else "",
                "reached_end": mark.exists() and mark.stat().st_mtime >= started - 1}


def section_drain(tmp: Path) -> None:
    print("── отложенное намерение доносит sentinel (п.9, Р-1) ──")
    st = SentinelStand(tmp / "drain")
    holder = st.hold_lock(30)
    try:
        rc, _, _ = st.raise_alert("sentinel-probe:x", "Беда при занятом локе", timeout=20)
    finally:
        holder.kill()
        holder.wait()
    check("стенд: намерение отложено (код 4, строка в очереди) — иначе случай ни о чём",
          (4, 1), (rc, len(st.queue_rows())))
    got = st.sentinel()
    check("sentinel дошёл до конца (метка heartbeat поставлена)", True, got["reached_end"])
    check("после прогона sentinel очередь отложенных пуста", 0, len(st.queue_rows()))
    check("после прогона sentinel карточка в стенд-карте", 1, len(st.by_source("sentinel-probe:x")))
    check("в журнале тревог «донесена»", True, "донесена" in st.kinds("sentinel-probe:x"))

    before = alerts.sha1(st.map)
    got = st.sentinel()
    check("пустая очередь: второй прогон карту не трогает", before, alerts.sha1(st.map))
    check("пустая очередь: sentinel дошёл до конца", True, got["reached_end"])


def section_broken_writer(tmp: Path) -> None:
    print("── писатель упал: sentinel идёт дальше и говорит об этом ──")
    st = SentinelStand(tmp / "broken")
    st.break_writer()
    got = st.sentinel()
    check("писатель вернул 3 — sentinel всё равно дошёл до конца", True, got["reached_end"])
    said = got["out"] + got["log"]
    check("sentinel назвал донос тревог и код писателя («донос тревог: код 3»)", True,
          "донос тревог: код 3" in said)


# Door of the stand: the code depends on the text, every call is logged.
STUB_DOOR = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$(dirname "$0")/door-calls.log"
case "$*" in
    *ПРАВИЛА*) exit 4 ;;
    *СВЯЗЬ*)   exit 1 ;;
esac
exit 0
"""


def section_resend_codes(tmp: Path) -> None:
    print("── досыл §3б: отказ правил (4) снимает строку с причиной, сбой связи (1) держит ──")
    st = SentinelStand(tmp / "resend")
    door = st.project / "scripts" / "tg_send.sh"
    door.write_text(STUB_DOOR, encoding="utf-8")
    door.chmod(0o755)
    queue = st.log / "панель-недоставленное.jsonl"
    rows = {"rules": "весть ПРАВИЛА стенда", "link": "весть СВЯЗЬ стенда", "ok": "весть стенда"}
    queue.write_text("".join(json.dumps({"текст": t}, ensure_ascii=False) + "\n"
                             for t in rows.values()), encoding="utf-8")
    got = st.sentinel()
    calls = (st.project / "scripts" / "door-calls.log")
    called = calls.read_text(encoding="utf-8") if calls.exists() else ""
    check("стенд: досыл позвал дверь по каждой строке очереди", True,
          all(t in called for t in rows.values()))
    left = queue.read_text(encoding="utf-8") if queue.exists() else ""
    check("сбой связи (1): строка осталась в очереди", True, rows["link"] in left)
    check("доставлено (0): строка снята", False, rows["ok"] in left)
    check("отказ правил (4): строка снята — вечный досыл не жжёт канал", False, rows["rules"] in left)
    said = got["out"] + got["log"]
    check("отказ правил (4): причина и текст названы в журнале sentinel", True,
          "правил" in said and rows["rules"] in said)


def main() -> int:
    live_before = alerts.sha1(alerts.LIVE_MAP)
    check(f"sentinel на месте: {SENTINEL.name}", True, SENTINEL.is_file())
    with tempfile.TemporaryDirectory(prefix="sentinel-drain-probe-") as tmp:
        for section in (section_drain, section_broken_writer, section_resend_codes):
            try:
                section(Path(tmp))
            except Exception as error:  # a crashed section is a failure, not a silence
                check(f"раздел {section.__name__} дошёл до конца", "дошёл",
                      f"упал: {type(error).__name__}: {error}")
            check(f"{section.__name__}: боевая карта не тронута", live_before,
                  alerts.sha1(alerts.LIVE_MAP))
    print(f"sentinel drain: случаев {cases.paths}, падений {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
