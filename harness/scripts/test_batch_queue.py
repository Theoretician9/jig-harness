#!/usr/bin/env python3
"""Пробы очереди заявок с окном сбора (закон пачек, слово владельца 14.11):
заявки, пришедшие в окно или пока заход идёт, уходят ОДНИМ заходом; падение
захода — отказ каждой заявке, а не вечное ожидание.

    python3 scripts/test_batch_queue.py

Стенд — свой временный каталог очереди (замки там же) и подставной исполнитель
вместо настоящего среза: пишет строку захода в журнал и бумагу каждому id.
Дверь для мутационного прогона: HARNESS_BATCH_QUEUE — путь batch_queue.py под пробой.
Последняя строка: `очередь пачек: N путей, F ПЛОХО`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

QUEUE = Path(os.environ.get("HARNESS_BATCH_QUEUE") or HERE / "lib" / "batch_queue.py")
BUSY_RC = 75

cases = Cases()
check = cases.check

# Подставной исполнитель: последний довод — id через запятую.
EXECUTOR = '''#!/usr/bin/env python3
import os, signal, sys, time
ids = sys.argv[-1].split(",")
with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as fh:
    fh.write(sys.argv[-1] + "\\n")
if os.environ.get("FAKE_EARLY"):
    print("начат " + sys.argv[-1], flush=True)
if os.environ.get("FAKE_KILL_RUNNER"):
    os.kill(os.getppid(), signal.SIGKILL)
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
for i in ids:
    papers = os.path.join(os.environ["FAKE_PAPERS"], i)
    with open(papers, "w", encoding="utf-8") as fh:
        fh.write("бумага " + i + "\\n")
    print("итог задачи " + i)
sys.exit(int(os.environ.get("FAKE_RC", "0")))
'''


class Stand:
    def __init__(self, tmp: Path, **fake: str) -> None:
        self.tmp = tmp
        self.place = tmp / "очередь"
        self.papers = tmp / "бумаги"
        self.papers.mkdir(parents=True)
        self.calls = tmp / "заходы.log"
        self.executor = tmp / "executor.py"
        self.executor.write_text(EXECUTOR, encoding="utf-8")
        self.env = {**os.environ, "FAKE_CALLS": str(self.calls), "FAKE_PAPERS": str(self.papers),
                    **fake}

    def start(self, ids: str, window: float = 0, cap: int = 6, wait: float = 30) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, str(QUEUE), "--место", str(self.place), "--окно", str(window),
             "--потолок", str(cap), "--ждать", str(wait), ids, "--",
             sys.executable, str(self.executor)],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def runs(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").split() if self.calls.is_file() else []

    def paper(self, i: str) -> bool:
        return (self.papers / i).is_file()


def finish(proc: subprocess.Popen, timeout: float = 60) -> tuple[int | None, str]:
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        proc.kill()
        return None, "ТАЙМАУТ — вечное ожидание"


def probe_window(tmp: Path) -> None:
    s = Stand(tmp)
    a = s.start("hr.a", window=2)
    time.sleep(0.4)
    b = s.start("hr.b", window=2)
    (rc_a, out_a), (rc_b, out_b) = finish(a), finish(b)
    check("1 два вызова подряд в окне — один заход с обоими id", ["hr.a,hr.b"], s.runs())
    check("2 бумаги обеим задачам", (True, True), (s.paper("hr.a"), s.paper("hr.b")))
    check("3 обе заявки получили итог захода: код 0 и строку своего id", (0, 0, True, True),
          (rc_a, rc_b, "итог задачи hr.a" in out_a, "итог задачи hr.b" in out_b))


def probe_after_window(tmp: Path) -> None:
    s = Stand(tmp, FAKE_SLEEP="3")
    a = s.start("hr.a", window=1)
    time.sleep(1.8)
    b = s.start("hr.b", window=1)
    (rc_a, _), (rc_b, _) = finish(a), finish(b)
    check("4 вызов после закрытия окна — новый заход за первым", ["hr.a", "hr.b"], s.runs())
    check("5 второй заход дошёл: оба кода 0, обе бумаги", (0, 0, True, True),
          (rc_a, rc_b, s.paper("hr.a"), s.paper("hr.b")))


def probe_failure(tmp: Path) -> None:
    s = Stand(tmp, FAKE_RC="3")
    start = time.monotonic()
    a = s.start("hr.a", window=1.5)
    time.sleep(0.3)
    b = s.start("hr.b", window=1.5)
    (rc_a, out_a), (rc_b, out_b) = finish(a, 30), finish(b, 30)
    check("6 падение захода — обе заявки получают отказ с его кодом, а не 0", (3, 3), (rc_a, rc_b))
    check("7 отказ называет код захода", (True, True),
          ("кодом 3" in out_a, "кодом 3" in out_b))
    check("8 падение захода не держит заявки дольше захода", True, time.monotonic() - start < 20)


def probe_runner_killed(tmp: Path) -> None:
    s = Stand(tmp, FAKE_KILL_RUNNER="1", FAKE_SLEEP="1")
    a = s.start("hr.a", window=1.5)
    time.sleep(0.3)
    b = s.start("hr.b", window=1.5, wait=40)
    start = time.monotonic()
    finish(a, 30)
    rc_b, out_b = finish(b, 30)
    check("9 исполнитель убит посреди захода — ждущая заявка получает отказ 75, не висит",
          (BUSY_RC, True), (rc_b, time.monotonic() - start < 20))
    check("10 отказ называет умерший заход", True, "умер" in out_b)


def probe_deadline(tmp: Path) -> None:
    s = Stand(tmp, FAKE_SLEEP="6")
    a = s.start("hr.a", window=0)
    time.sleep(1)
    start = time.monotonic()
    b = s.start("hr.b", window=0, wait=2)
    rc_b, out_b = finish(b, 30)
    took = time.monotonic() - start
    finish(a, 30)
    s.env["FAKE_SLEEP"] = "0"
    finish(s.start("hr.c", window=0), 30)
    check("11 предел ожидания: заявка за долгим заходом уходит с отказом 75 в срок",
          (BUSY_RC, True), (rc_b, took < 5))
    check("12 отказ по пределу назван словами", True, "не дождались" in out_b)
    check("13 отозванная заявка не гонится после ухода заявителя", ["hr.a", "hr.c"], s.runs())


def probe_cap(tmp: Path) -> None:
    s = Stand(tmp)
    procs = []
    for i in ("hr.a", "hr.b", "hr.c"):
        procs.append(s.start(i, window=2, cap=2))
        time.sleep(0.3)
    codes = [finish(p)[0] for p in procs]
    check("14 потолок пачки 2: три заявки — два захода, первый с двумя id",
          ["hr.a,hr.b", "hr.c"], s.runs())
    check("15 все три заявки получили код 0", [0, 0, 0], codes)


def probe_stream(tmp: Path) -> None:
    """Ход захода виден, пока он идёт: строка исполнителя доходит до журнала
    запуска и до заход.log в месте очереди сразу, а не в конце (14.11: 4 ч, 0 байт)."""
    s = Stand(tmp, FAKE_EARLY="1", FAKE_SLEEP="4")
    launch = tmp / "запуск.log"
    with launch.open("w", encoding="utf-8") as out:
        a = subprocess.Popen(
            [sys.executable, str(QUEUE), "--место", str(s.place), "--окно", "0",
             "--потолок", "6", "--ждать", "30", "hr.a", "--", sys.executable, str(s.executor)],
            env=s.env, stdout=out, stderr=subprocess.STDOUT, text=True)
        time.sleep(2)
        seen = launch.read_text(encoding="utf-8")
        run_log = s.place / "заход.log"
        live = run_log.read_text(encoding="utf-8") if run_log.is_file() else ""
        rc, _ = finish(a, 30)
    check("16 построчно: строка захода в журнале запуска и в заход.log, пока заход идёт",
          (True, True), ("начат hr.a" in seen, "начат hr.a" in live))
    check("17 итог после захода дошёл, код 0", (0, True),
          (rc, "итог задачи hr.a" in launch.read_text(encoding="utf-8")))


def probe_own_tree(tmp: Path) -> None:
    """Заявка гонится командой СВОЕГО дерева: очередь общая для всех деревьев,
    а исполнитель — любой заявитель. Чужое дерево (worktree помощника) сносят
    посреди захода — заявка главного дерева не должна жить в нём (14.11 22:57)."""
    s = Stand(tmp)
    other = tmp / "дерево-помощника"
    other.mkdir()
    other_executor = other / "executor.py"
    other_executor.write_text(EXECUTOR.replace('sys.argv[-1] + "\\n"', '"чужое:" + sys.argv[-1] + "\\n"'),
                              encoding="utf-8")
    a = subprocess.Popen(
        [sys.executable, str(QUEUE), "--место", str(s.place), "--окно", "1.5", "--потолок", "6",
         "--ждать", "30", "hr.a", "--", sys.executable, str(other_executor)],
        env=s.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(0.4)
    b = s.start("hr.b", window=1.5)
    (rc_a, _), (rc_b, _) = finish(a), finish(b)
    check("18 заявки двух деревьев — каждая своей командой, не одной пачкой",
          ["чужое:hr.a", "hr.b"], s.runs())
    check("19 обе заявки получили код 0", (0, 0), (rc_a, rc_b))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for name, probe in (("окно", probe_window), ("после окна", probe_after_window),
                            ("падение", probe_failure), ("убит", probe_runner_killed),
                            ("предел", probe_deadline), ("потолок", probe_cap),
                            ("построчно", probe_stream),
                            ("своё дерево", probe_own_tree)):
            try:
                probe(tmp / name)
            except Exception as error:  # упавший стенд — красный путь, а не трассировка
                check(f"{name}: стенд отвечает", "ответ", f"{type(error).__name__}: {error}")
    print(f"очередь пачек: {cases.paths} путей, {cases.failures} ПЛОХО")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
