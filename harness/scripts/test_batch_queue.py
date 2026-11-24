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

import contextlib
import datetime
import fcntl
import json
import os
import shutil
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
marker = os.environ.get("FAKE_KILL_ONCE")
if marker and not os.path.exists(marker):
    open(marker, "w").close()
    os.kill(os.getppid(), signal.SIGKILL)
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
for i in ids:
    papers = os.path.join(os.environ["FAKE_PAPERS"], i)
    with open(papers, "w", encoding="utf-8") as fh:
        fh.write("бумага " + i + "\\n")
    print("итог задачи " + i)
sys.exit(int(os.environ.get("FAKE_RC", "0")))
'''


STAND_ACCEPTANCE = '''import sys
open(sys.argv[sys.argv.index("--в") + 1], "w").write("{}")
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
        # Дерево заявки — каталог стенда: исполнитель читает историю его acceptance.py
        # (подставной пишет пустой файл), а не боевого дерева, где пробу гонят ворота.
        # Очередь стенда — копия lib: рядом с ней её config и events, и копия под мутацией тоже.
        shutil.copytree(HERE / "lib", tmp / "kit" / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(QUEUE, tmp / "kit" / "lib" / "batch_queue.py")
        self.queue = tmp / "kit" / "lib" / "batch_queue.py"
        scripts = tmp / "scripts"
        scripts.mkdir()
        (scripts / "acceptance.py").write_text(STAND_ACCEPTANCE, encoding="utf-8")
        self.env = {**os.environ, "FAKE_CALLS": str(self.calls), "FAKE_PAPERS": str(self.papers),
                    **fake}

    def start(self, ids: str, window: float = 0, cap: int = 6, wait: float = 30) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, str(self.queue), "--место", str(self.place), "--окно", str(window),
             "--потолок", str(cap), "--ждать", str(wait), ids, "--",
             sys.executable, str(self.executor)],
            env=self.env, cwd=str(self.tmp), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

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
    # Исполнитель стартует отдельным процессом за доли секунды: вторая заявка приходит позже
    # этого старта, но раньше конца окна — иначе без окна пачка собралась бы сама.
    time.sleep(1.0)
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
    """Исполнитель — отдельный процесс; его смерть посреди захода не отказывает чужим
    заявкам, а возвращает их в очередь: следующий исполнитель гонит заход заново."""
    s = Stand(tmp, FAKE_KILL_ONCE=str(tmp / "убит"), FAKE_SLEEP="1")
    a = s.start("hr.a", window=1.5)
    time.sleep(0.3)
    b = s.start("hr.b", window=1.5, wait=60)
    start = time.monotonic()
    (rc_a, _), (rc_b, out_b) = finish(a, 60), finish(b, 60)
    check("9 исполнитель убит посреди захода — заявки не отказаны, заход прогнан снова: оба кода 0",
          (0, 0, True), (rc_a, rc_b, time.monotonic() - start < 50))
    check("10 заход шёл дважды: убитый и довёдший, оба с обоими id", ["hr.a,hr.b", "hr.a,hr.b"], s.runs())


def probe_deadline(tmp: Path) -> None:
    """Предел ожидания заявителя заявку не снимает: она остаётся в очереди и её
    дослуживает исполнитель, а не пропадает молча (слово владельца 17.11)."""
    s = Stand(tmp, FAKE_SLEEP="6")
    a = s.start("hr.a", window=0)
    time.sleep(1)
    start = time.monotonic()
    b = s.start("hr.b", window=0, wait=2)
    rc_b, out_b = finish(b, 30)
    took = time.monotonic() - start
    finish(a, 30)
    check("11 предел ожидания: заявка за долгим заходом уходит с отказом 75 в срок",
          (BUSY_RC, True), (rc_b, took < 5))
    check("12 отказ по пределу назван словами и говорит, кто дослужит", (True, True),
          ("не дождались" in out_b, "дослужит heavy-queue" in out_b))
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline and not s.paper("hr.b"):
        time.sleep(0.5)
    check("13 заявка не снята после ухода заявителя: её дослужил исполнитель", ["hr.a", "hr.b"],
          sorted(i for call in s.runs() for i in call.split(",")))


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
            [sys.executable, str(s.queue), "--место", str(s.place), "--окно", "0",
             "--потолок", "6", "--ждать", "30", "hr.a", "--", sys.executable, str(s.executor)],
            env=s.env, cwd=str(s.tmp), stdout=out, stderr=subprocess.STDOUT, text=True)
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
        [sys.executable, str(s.queue), "--место", str(s.place), "--окно", "1.5", "--потолок", "6",
         "--ждать", "30", "hr.a", "--", sys.executable, str(other_executor)],
        env=s.env, cwd=str(s.tmp), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(0.4)
    b = s.start("hr.b", window=1.5)
    (rc_a, _), (rc_b, _) = finish(a), finish(b)
    check("18 заявки двух деревьев — каждая своей командой, не одной пачкой",
          ["чужое:hr.a", "hr.b"], s.runs())
    check("19 обе заявки получили код 0", (0, 0), (rc_a, rc_b))


# ─── Одна очередь тяжёлых прогонов (спека тяжёлых прогонов §А, ревью-3) ───
# Контракт, который пробы ждут от кода:
#   * место очереди — $LOG_DIR/очередь-мутаций (LOG_DIR стенда через свой install.conf);
#   * заявка в очередь.json: знак, ids, команда, с, заход, дерево, среда, попыток, не_раньше;
#   * `batch_queue.py --дослужить` гонит готовые заявки (каждую в её `дерево`, со своей
#     `среда` и MUTATION_LOCK_WAIT_MIN=2) и выходит, когда готовых по `не_раньше` нет;
#     перед заходом с ids зовёт `<дерево>/scripts/acceptance.py --история <ids> --в <файл>`;
#   * `batch_queue.py --подать --без-ids --дерево D [--ждать С] -- команда…` ставит заявку,
#     пускает исполнителя и ждёт итога; вышел срок — 75 и «дослужит heavy-queue», заявка цела;
#   * события — журналы $LOG_DIR/*.jsonl; тревога — `scripts/alert-task.py` рядом с очередью
#     (подставной, пишет вызовы).
# Стенд гонит КОПИЮ lib (очередь из HARNESS_BATCH_QUEUE): живой alert-task и боевая очередь
# /var/log/harness/очередь-мутаций не трогаются.

HEAVY_EXECUTOR = '''#!/usr/bin/env python3
import json, os, signal, sys, time
behavior = json.load(open({behavior!r}, encoding="utf-8"))
ids = sys.argv[3].split(",") if len(sys.argv) > 3 else []
names = ("LOG_DIR", "MUTATION_LOCK_WAIT_MIN", "HARNESS_GIT_HISTORY", "HARNESS_INVARIANTS",
         "HARNESS_PROBE_MUTATIONS", "MUT_ONLY_LINES", "SECRET_TOKEN")
with open({calls!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"метка": sys.argv[2], "ids": ids, "cwd": os.getcwd(),
                          "env": {{n: os.environ.get(n) for n in names}}}}, ensure_ascii=False) + "\\n")
if behavior.get("kill"):
    os.kill(os.getppid(), signal.SIGKILL)
    os.killpg(os.getpgid(0), signal.SIGKILL)
time.sleep(behavior.get("sleep", 0))
for i in ids:
    line = behavior.get("lines", {{}}).get(i, behavior.get("default_line", "дыр 0, бумага стенда"))
    if line:
        print("мутации задачи " + i + ": " + line, flush=True)
sys.exit(behavior.get("rc", 0))
'''

# Подставной acceptance.py дерева: только режим --история (пишет файл истории).
HEAVY_ACCEPTANCE = '''#!/usr/bin/env python3
import json, sys
with open({history_calls!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
rc = int(open({history_rc!r}).read().strip() or 0)
if "--история" in sys.argv and "--в" in sys.argv and rc == 0:
    open(sys.argv[sys.argv.index("--в") + 1], "w").write("{{}}")
sys.exit(rc)
'''

HEAVY_ALERT = '''#!/usr/bin/env python3
import json, sys
open({alerts!r}, "a", encoding="utf-8").write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
'''


def _git(where: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(where), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "p", "GIT_AUTHOR_EMAIL": "p@s",
                        "GIT_COMMITTER_NAME": "p", "GIT_COMMITTER_EMAIL": "p@s"})


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class Heavy:
    """Стенд очереди тяжёлых прогонов: основное дерево (git), копия lib с очередью под
    пробой, свой LOG_DIR, подставные исполнитель, acceptance --история и alert-task."""

    def __init__(self, tmp: Path, conf: str = 'BACKGROUND_PROCESSES="on"\n') -> None:
        self.tmp = tmp
        self.logs = tmp / "logs"
        self.place = self.logs / "очередь-мутаций"
        self.main = tmp / "дерево"
        self.logs.mkdir(parents=True)
        self.main.mkdir()
        _git(self.main, "init", "-q", "-b", "master")
        _git(self.main, "commit", "-q", "--allow-empty", "-m", "стенд")
        kit = tmp / "kit" / "scripts"
        shutil.copytree(HERE / "lib", kit / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(QUEUE, kit / "lib" / "batch_queue.py")
        self.queue = kit / "lib" / "batch_queue.py"
        self.behavior = tmp / "поведение.json"
        self.set(rc=0)
        self.calls = tmp / "заходы.jsonl"
        self.alerts = tmp / "тревоги.jsonl"
        self.history_calls = tmp / "история.jsonl"
        self.history_rc = tmp / "история.rc"
        self.history_rc.write_text("0", encoding="utf-8")
        self.executor = _write(self.main / "executor.py", HEAVY_EXECUTOR.format(
            behavior=str(self.behavior), calls=str(self.calls)))
        _write(self.main / "scripts" / "acceptance.py", HEAVY_ACCEPTANCE.format(
            history_calls=str(self.history_calls), history_rc=str(self.history_rc)))
        for scripts in (kit, self.main / "scripts"):
            _write(scripts / "alert-task.py", HEAVY_ALERT.format(alerts=str(self.alerts)))
        self.conf = tmp / "harness.conf"
        self.conf.write_text(conf, encoding="utf-8")
        self.install = tmp / "install.conf"
        self.install.write_text(f'PROJECT_DIR="{self.main}"\nLOG_DIR="{self.logs}"\n', encoding="utf-8")

    def set(self, **behavior) -> None:
        self.behavior.write_text(json.dumps(behavior, ensure_ascii=False), encoding="utf-8")

    def env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HARNESS_", "MUTATION_", "MUT_")) and k not in ("LOG_DIR", "PROJECT_DIR")}
        env.update({"LOG_DIR": str(self.logs), "HARNESS_LOG_DIR": str(self.logs),
                    "HARNESS_INSTALL_CONF": str(self.install), "HARNESS_CONF": str(self.conf),
                    "HARNESS_HOME": str(self.tmp), "HARNESS_EVENTS_STATE": str(self.tmp / "state"),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        env.update(extra)
        return env

    def command(self, label: str) -> list:
        return [sys.executable, str(self.executor), "--метка", label]

    def request(self, token: str, ids: list | None = None, label: str = "", **fields) -> dict:
        return {"знак": token, "ids": ids or [], "команда": self.command(label or token),
                "с": time.time(), "заход": None, "дерево": str(self.main), "среда": {},
                "попыток": 0, "не_раньше": 0, **fields}

    def seed(self, *requests: dict) -> None:
        self.place.mkdir(parents=True, exist_ok=True)
        (self.place / "очередь.json").write_text(
            json.dumps({"заявки": list(requests), "итоги": {}}, ensure_ascii=False), encoding="utf-8")

    def state(self) -> dict:
        try:
            return json.loads((self.place / "очередь.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"заявки": [], "итоги": {}}

    def edit(self, token: str, **fields) -> None:
        state = self.state()
        for request in state["заявки"]:
            if request["знак"] == token:
                request.update(fields)
        (self.place / "очередь.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def pending(self, token: str) -> dict | None:
        return next((з for з in self.state()["заявки"] if з.get("знак") == token), None)

    def result(self, token: str) -> dict | None:
        return self.state()["итоги"].get(token)

    def serve(self, timeout: float = 60, **extra: str) -> tuple[int | None, str, float]:
        start = time.monotonic()
        try:
            r = subprocess.run([sys.executable, str(self.queue), "--дослужить"], env=self.env(**extra),
                               capture_output=True, text=True, errors="replace", timeout=timeout,
                               start_new_session=True)
            return r.returncode, r.stdout + r.stderr, time.monotonic() - start
        except subprocess.TimeoutExpired:
            return None, "ТАЙМАУТ", time.monotonic() - start

    def submit(self, label: str, wait: float, tree: Path | None = None, **extra: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, str(self.queue), "--подать", "--без-ids", "--дерево", str(tree or self.main),
             "--ждать", str(wait), "--", *self.command(label)],
            env=self.env(**extra), cwd=str(tree or self.main), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)

    def runs(self) -> list:
        return _jsonl(self.calls)

    def events(self) -> str:
        return "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in self.logs.rglob("*.jsonl"))

    def alert_calls(self) -> list:
        return _jsonl(self.alerts)

    def by_label(self, label: str) -> list:
        return [з for з in self.state()["заявки"] if label in з.get("команда", [])]


@contextlib.contextmanager
def runner_lock(s: Heavy):
    """Проба держит исполнитель.lock: исполнитель, пущенный заявителем, выходит сразу."""
    s.place.mkdir(parents=True, exist_ok=True)
    with open(s.place / "исполнитель.lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
        fcntl.flock(fh, fcntl.LOCK_UN)


def dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def lost(result: dict | None) -> bool:
    return bool(result) and "потеряна" in json.dumps(result, ensure_ascii=False)


def heavy_busy_returns(tmp: Path) -> None:
    """А(а), А(б). Поломки: 75 закрывает заявку итогом; 75 гонится снова без паузы;
    75 считается попыткой и десяток занятых срезов делает заявку «потерянной»."""
    s = Heavy(tmp / "а")
    s.set(rc=BUSY_RC)
    s.seed(s.request("з1"))
    s.serve()
    first = s.pending("з1") or {}
    check("А(а) дверь дала 75 → заявка снова в очереди, итога нет, заход снят",
          (True, None, None), (bool(first), s.result("з1"), first.get("заход")))
    check("А(а) 75 → прогнана не раньше паузы (не_раньше ≥ сейчас + 30 с)", True,
          float(first.get("не_раньше") or 0) >= time.time() + 30)
    s.serve()
    check("А(а) повтор --дослужить сразу — команда не позвана второй раз", 1, len(s.runs()))
    for _ in range(9):
        s.edit("з1", не_раньше=0)
        s.serve()
    final = s.pending("з1") or {}
    check("А(б) 75 десять раз подряд → попыток 0, заявка не «потеряна»",
          (10, 0, False), (len(s.runs()), final.get("попыток"), lost(s.result("з1"))))


def heavy_runner_death(tmp: Path) -> None:
    """А(в), А(г). Поломки: смерть исполнителя закрывает заявку отказом (старый
    fail_orphans) или не считается попыткой; третья смерть не даёт «потеряна»."""
    s = Heavy(tmp / "в")
    s.set(kill=True)
    s.seed(s.request("з1"))
    s.serve()
    s.set(rc=BUSY_RC)
    s.serve()
    if len(s.runs()) == 1:
        s.edit("з1", не_раньше=0)
        s.serve()
    after = s.pending("з1") or {}
    check("А(в) умер исполнитель → заявка прогнана снова, попыток 1, итога-отказа нет",
          (2, 1, None), (len(s.runs()), after.get("попыток"), s.result("з1")))

    s = Heavy(tmp / "г")
    s.seed(s.request("з1", попыток=2, заход=dead_pid()))
    s.serve()
    check("А(г) третья смерть → итог «потеряна», команда не звана", (True, 0),
          (lost(s.result("з1")), len(s.runs())))
    check("А(г) «потеряна» — событие heavy.lost в журнале", True, "heavy.lost" in s.events())
    check("тревога: heavy.lost → alert-task позван с ключом heavy-runs:lost", True,
          any("--key" in c and "heavy-runs:lost" in c for c in s.alert_calls()))
    for path in s.logs.rglob("*.jsonl"):
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line) if line.strip() else None
            if row and row.get("event") == "heavy.lost":
                row["ts"] = datetime.datetime.fromtimestamp(time.time() - 25 * 3600).astimezone().isoformat(timespec="seconds")
            rows.append(json.dumps(row, ensure_ascii=False) if row else line)
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    s.serve()
    check("тревога: последнее событие 25 ч назад → гашение (--resolve heavy-runs:lost)", True,
          any("--resolve" in c and "heavy-runs:lost" in c for c in s.alert_calls()))


def heavy_own_results(tmp: Path) -> None:
    """А(д), А(е), ревью-3 п.1. Поломки: один код всей пачке; любая строка «мутации
    задачи …» — итог; «история не прочитана» закрывает заявку."""
    s = Heavy(tmp / "д")
    s.set(rc=1, default_line="", lines={"hr.a": "дыр 2, бумага стенда"})
    s.seed(s.request("за", ["hr.a"], label="пачка"), s.request("зб", ["hr.b"], label="пачка"))
    s.serve()
    check("А(д) пачка из двух id одним заходом", [["hr.a", "hr.b"]], [r["ids"] for r in s.runs()][:1])
    check("А(д) итог у одного, код 1 → закрыт он один (код 1), второй в очереди с попыток 1",
          (1, None, 1), ((s.result("за") or {}).get("код"), s.result("зб"),
                         (s.pending("зб") or {}).get("попыток")))
    for label, line in (("А(е) «не догнана — срок захода вышел»", "не догнана — срок захода вышел"),
                        ("ревью-3 п.1 «история не прочитана»", "история не прочитана (git молчит)")):
        s = Heavy(tmp / ("е" if "А(е)" in label else "е1"))
        s.set(rc=2, lines={"hr.a": line})
        s.seed(s.request("за", ["hr.a"]))
        s.serve()
        check(f"{label} → заявка в очереди, попыток 1, итога нет", (1, None),
              ((s.pending("за") or {}).get("попыток"), s.result("за")))


SHORT_TAIL = " — срок захода вышел; подай её снова, готовые мутации отдаст кэш прибора"


def heavy_short_progress(tmp: Path) -> None:
    """Недогнанная заявка с ходом (19.11: 14 → 18 → 33 мутаций, а заявку убил предел
    попыток). Поломки: «не догнана, новых мутаций K>0» тратит попытку; K не
    разбирается (5 и 0 — одно); K=0 тоже не тратит попытку — вечный круг без хода."""
    for rc in (1, 2):
        s = Heavy(tmp / f"ход{rc}")
        s.set(rc=rc, lines={"hr.a": "не догнана, новых мутаций 5" + SHORT_TAIL})
        s.seed(s.request("за", ["hr.a"], попыток=2))
        s.serve()
        check(f"А(е2) код {rc}, «не догнана, новых мутаций 5», попыток было 2 → в очереди, "
              "попыток 2, итога нет", (2, None),
              ((s.pending("за") or {}).get("попыток"), s.result("за")))
        s.set(rc=0)
        s.edit("за", не_раньше=0)
        s.serve()
        check(f"А(е2) код {rc}: следующий заход её не теряет — прогнана, итог 0, heavy.lost нет",
              (2, 0, False), (len(s.runs()), (s.result("за") or {}).get("код"), "heavy.lost" in s.events()))
    s = Heavy(tmp / "ход0")
    s.set(rc=2, lines={"hr.a": "не догнана, новых мутаций 0" + SHORT_TAIL})
    s.seed(s.request("за", ["hr.a"], попыток=2))
    s.serve()
    s.edit("за", не_раньше=0)
    s.serve()
    check("А(е3) «не догнана, новых мутаций 0», попыток было 2 → третья попытка, «потеряна», "
          "heavy.lost, команда звана один раз", (True, True, 1),
          (lost(s.result("за")), "heavy.lost" in s.events(), len(s.runs())))


def heavy_age(tmp: Path) -> None:
    """А(ж). Поломка: занятый срез держит заявку вечно — без предела возраста нет ни итога, ни тревоги."""
    s = Heavy(tmp / "ж")
    s.set(rc=BUSY_RC)
    s.seed(s.request("з1", с=time.time() - 25 * 3600))
    s.serve()
    check("А(ж) возраст > 24 ч при 75 → «потеряна» и событие heavy.lost", (True, True),
          (lost(s.result("з1")), "heavy.lost" in s.events()))


def heavy_requester_gone(tmp: Path) -> None:
    """А(з). Поломки: прибор по сроку снимает заявку (withdraw); брошенную заявку
    основного дерева никто не дослуживает."""
    s = Heavy(tmp / "з")
    s.seed(s.request("з1"))
    s.serve()
    check("А(з) заявитель ушёл, дерево основное → --дослужить даёт итог (код 0)", 0,
          (s.result("з1") or {}).get("код"))
    s = Heavy(tmp / "з2")
    with runner_lock(s):
        proc = s.submit("прибор", wait=2)
        rc, out = finish(proc, 30)
        left = s.by_label("прибор")
    check("А(з) прибор по сроку → 75 и строка «дослужит heavy-queue»", (BUSY_RC, True),
          (rc, "дослужит heavy-queue" in out))
    check("А(з) после 75 прибора заявка цела в очереди", 1, len(left))
    s.serve()
    sign = left[0]["знак"] if left else "-"
    check("А(з) брошенную заявку прибора дослужил --дослужить (итог код 0)", 0,
          (s.result(sign) or {}).get("код"))


def tree_files(where: Path) -> list:
    return sorted(str(p.relative_to(where)) for p in where.rglob("*") if ".git" not in p.parts)


def heavy_helper_tree(tmp: Path) -> None:
    """А(и). Поломки: заявку в ушедшее дерево помощника гонят (бумага делает дерево грязным
    навсегда) или молча роняют; гонят и дерево помощника под замком Claude Code — нет."""
    s = Heavy(tmp / "и")
    helper, locked = s.tmp / "помощник", s.tmp / "помощник-под-замком"
    _git(s.main, "worktree", "add", "-q", "-b", "h1", str(helper))
    _git(s.main, "worktree", "add", "-q", "-b", "h2", str(locked))
    _git(s.main, "worktree", "lock", "--reason", "claude agent", str(locked))
    before = tree_files(helper)
    s.seed(s.request("помощник", дерево=str(helper)), s.request("нет-дерева", дерево=str(s.tmp / "нет")))
    s.serve()
    check("А(и) дерево помощника без замка и несуществующее → команда не звана", [],
          [r["метка"] for r in s.runs()])
    check("А(и) событие heavy.requester-gone, заявки сняты из очереди", (True, None, None),
          ("heavy.requester-gone" in s.events(), s.pending("помощник"), s.pending("нет-дерева")))
    check("А(и) в дереве помощника новых файлов нет", before, tree_files(helper))
    s.seed(s.request("под-замком", дерево=str(locked)))
    s.serve()
    check("А(и) дерево помощника под замком Claude Code — гонится в нём (cwd)", [str(locked)],
          [r["cwd"] for r in s.runs()])


def heavy_lock_and_slice(tmp: Path) -> None:
    """А(к), А(л), ревью-3 п.7. Поломки: --дослужить ждёт замок исполнителя (строка cron
    копится); гонит заход из среза; держит замок, пока ждёт не_раньше чужой заявки."""
    s = Heavy(tmp / "к")
    s.seed(s.request("з1"))
    with runner_lock(s):
        rc, _, took = s.serve(timeout=30)
    check("А(к) исполнитель.lock держит проба → --дослужить выходит < 2 с, ничего не гонит",
          (True, 0, True), (rc is not None and took < 2, len(s.runs()), bool(s.pending("з1"))))
    s = Heavy(tmp / "л")
    s.seed(s.request("з1"))
    rc, _, _ = s.serve(HARNESS_IN_CHECKS_SLICE="1")
    check("А(л) --дослужить с HARNESS_IN_CHECKS_SLICE=1 → отказ (код ≠ 0), команда не звана",
          (True, 0), (rc not in (0, None), len(s.runs())))
    check("А(л) отказ в срезе записан событием очереди", True, "batch_queue" in s.events())
    s = Heavy(tmp / "к2")
    s.seed(s.request("з1", не_раньше=time.time() + 60))
    rc, _, took = s.serve(timeout=30)
    check("ревью-3 п.7: одна заявка с не_раньше +60 с → --дослужить выходит < 2 с, не гонит",
          (True, 0), (rc is not None and took < 2, len(s.runs())))


def heavy_env(tmp: Path) -> None:
    """Ревью-3 п.4, А.3. Поломки: команда заявки идёт в окружении исполнителя (боевой
    LOG_DIR и таблица); пачка смешивает заявки с разной средой; секрет уходит в очередь;
    очередь не ставит команде MUTATION_LOCK_WAIT_MIN=2."""
    s = Heavy(tmp / "среда")
    x, y = s.tmp / "журналы-заявителя", s.tmp / "таблица-заявителя.yaml"
    s.seed(s.request("з1", среда={"LOG_DIR": str(x), "HARNESS_INVARIANTS": str(y)}))
    s.serve()
    seen = (s.runs() or [{"env": {}}])[0]["env"]
    check("п.4: заявка с LOG_DIR=X, исполнитель с другим → команда видит X и таблицу заявителя",
          (str(x), str(y)), (seen.get("LOG_DIR"), seen.get("HARNESS_INVARIANTS")))
    check("А.3: команда заявки получила MUTATION_LOCK_WAIT_MIN=2", "2", seen.get("MUTATION_LOCK_WAIT_MIN"))

    s = Heavy(tmp / "пара")
    s.set(rc=0)
    x1, x2 = str(s.tmp / "x1"), str(s.tmp / "x2")
    s.seed(s.request("за", ["hr.a"], label="пачка", среда={"LOG_DIR": x1}),
           s.request("зб", ["hr.b"], label="пачка", среда={"LOG_DIR": x2}))
    s.serve()
    check("п.4: пачка берётся по паре (дерево, среда) — разные среды двумя заходами",
          [(["hr.a"], x1), (["hr.b"], x2)], sorted((r["ids"], r["env"]["LOG_DIR"]) for r in s.runs()))

    s = Heavy(tmp / "подать")
    secret = "probe-secret-" + str(time.time_ns())  # не-секрет
    with runner_lock(s):
        finish(s.submit("прибор", wait=1, HARNESS_PROBE_MUTATIONS=str(s.tmp / "пробы.yaml"),
                        MUT_ONLY_LINES="3,4", SECRET_TOKEN=secret), 30)
    got = (s.by_label("прибор") or [{}])[0]
    env = got.get("среда") or {}
    check("п.4: --подать пишет в заявку дерево и среду по списку двери",
          (str(s.main), str(s.tmp / "пробы.yaml"), "3,4", str(s.logs)),
          (got.get("дерево"), env.get("HARNESS_PROBE_MUTATIONS"), env.get("MUT_ONLY_LINES"), env.get("LOG_DIR")))
    check("п.4: секрет вне списка имён в заявку не попал", False,
          secret in json.dumps(s.state(), ensure_ascii=False))


def heavy_requester_killed(tmp: Path) -> None:
    """Ревью-3 п.6. Поломка: заход гонит сам заявитель (serve внутри request) — его смерть
    обрывает заход, и итог не пишется никому; чужие заявки получают попытку за чужую смерть."""
    s = Heavy(tmp / "п6")
    s.set(sleep=3, rc=0)
    s.seed(s.request("чужая", label="чужая"))
    proc = s.submit("заявитель", wait=60)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not any(r["метка"] == "заявитель" for r in s.runs()):
        time.sleep(0.2)
    mine = next((з["знак"] for з in s.state()["заявки"] if "заявитель" in з.get("команда", [])), None)
    proc.kill()
    proc.wait()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not (mine and s.result(mine) and s.result("чужая")):
        time.sleep(0.3)
    check("п.6: заявитель убит посреди захода → заход дошёл, итог его заявки код 0",
          0, (s.result(mine) or {}).get("код") if mine else "заявка не найдена")
    check("п.6: чужая заявка получила итог 0, без попытки за чужую смерть", 0,
          (s.result("чужая") or {}).get("код"))


def heavy_duplicate(tmp: Path) -> None:
    """Ревью-3 п.8. Поломка: прибор после 75 подаёт снова — вторая заявка без ids на ту же
    команду, заход гонится дважды."""
    s = Heavy(tmp / "п8")
    with runner_lock(s):
        finish(s.submit("прибор", wait=1), 30)
        finish(s.submit("прибор", wait=1), 30)
        count = len(s.by_label("прибор"))
    check("п.8: та же (дерево, команда, среда) без ids — одна заявка, не две", 1, count)
    rc, out = finish(s.submit("прибор", wait=40), 60)
    check("п.8: повторный заявитель ждёт итог первой: код 0, заход один", (0, 1),
          (rc, len([r for r in s.runs() if r["метка"] == "прибор"])))


def heavy_old_request(tmp: Path) -> None:
    """Ревью-3 п.9. Поломка: заявка старого формата (без дерево, среда, попыток, не_раньше)
    роняет разбор KeyError — очередь встаёт целиком."""
    s = Heavy(tmp / "п9")
    s.seed({"знак": "старая", "ids": [], "команда": s.command("старая"), "с": time.time(), "заход": None})
    rc, out, _ = s.serve()
    check("п.9: старая заявка без поля «дерево» — без трассировки, итог код 0, cwd — дерево из пути команды",
          (False, 0, [str(s.main)]),
          ("Traceback" in out, (s.result("старая") or {}).get("код"), [r["cwd"] for r in s.runs()]))


def heavy_history_step(tmp: Path) -> None:
    """А.2. Поломки: заход с ids идёт без файла истории (в срезе git снова ждёт память);
    история не прочиталась, а заход всё равно гонится или заявка закрывается."""
    s = Heavy(tmp / "история")
    s.seed(s.request("за", ["hr.a"]))
    s.serve()
    calls = _jsonl(s.history_calls)
    run = (s.runs() or [{"env": {}}])[0]
    check("А.2: перед заходом с ids исполнитель зовёт acceptance.py --история hr.a --в <файл>", True,
          any("--история" in c and "hr.a" in c and "--в" in c for c in calls))
    check("А.2: команде пришёл HARNESS_GIT_HISTORY, файл после захода удалён", (True, False),
          (bool(run["env"].get("HARNESS_GIT_HISTORY")),
           bool(run["env"].get("HARNESS_GIT_HISTORY")) and Path(run["env"]["HARNESS_GIT_HISTORY"]).exists()))
    s = Heavy(tmp / "история-нет")
    s.history_rc.write_text("2", encoding="utf-8")
    s.seed(s.request("за", ["hr.a"]))
    s.serve()
    check("А.2: история не прочиталась → команда не звана, заявка вернулась с попыток 1", (0, 1, None),
          (len(s.runs()), (s.pending("за") or {}).get("попыток"), s.result("за")))
    # Код 2 — acceptance сам поднял событие; иной код (упал, не запустился) — событие
    # поднимает очередь, иначе тревога heavy-runs:lost об этом не узнает.
    check("А.2: код 2 чтения истории — очередь событие не дублирует", 0,
          s.events().count("mutations.history-unread"))
    s = Heavy(tmp / "история-упала")
    s.history_rc.write_text("1", encoding="utf-8")
    s.seed(s.request("за", ["hr.a"]))
    s.serve()
    check("А.2: чтение истории упало кодом 1 → событие mutations.history-unread с причиной «код 1»",
          (True, True), ("mutations.history-unread" in s.events(), "код 1" in s.events()))


def heavy_switch_off(tmp: Path) -> None:
    """Решение управляющего по ревью-3 п.5 (исключения выключателю нет). Поломка: при
    BACKGROUND_PROCESSES≠on заявитель молча ждёт — человек не знает, что заявку дослужат после."""
    s = Heavy(tmp / "выкл", conf='BACKGROUND_PROCESSES="off"\n')
    with runner_lock(s):
        rc, out = finish(s.submit("прибор", wait=2, BACKGROUND_PROCESSES="off"), 30)
    check("п.5: выключатель off → «выключатель off — заявка ждёт в очереди», заявка цела",
          (True, 1), ("выключатель off — заявка ждёт в очереди" in out, len(s.by_label("прибор"))))


HEAVY_PROBES = (("А 75", heavy_busy_returns), ("А смерть", heavy_runner_death),
                ("А итог по id", heavy_own_results), ("А недогнана с ходом", heavy_short_progress),
                ("А возраст", heavy_age),
                ("А заявитель ушёл", heavy_requester_gone), ("А дерево помощника", heavy_helper_tree),
                ("А замок и срез", heavy_lock_and_slice), ("А среда", heavy_env),
                ("А смерть заявителя", heavy_requester_killed), ("А дубль", heavy_duplicate),
                ("А старая заявка", heavy_old_request), ("А история", heavy_history_step),
                ("А выключатель", heavy_switch_off))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for name, probe in (("окно", probe_window), ("после окна", probe_after_window),
                            ("падение", probe_failure), ("убит", probe_runner_killed),
                            ("предел", probe_deadline), ("потолок", probe_cap),
                            ("построчно", probe_stream),
                            ("своё дерево", probe_own_tree), *HEAVY_PROBES):
            try:
                probe(tmp / name)
            except Exception as error:  # упавший стенд — красный путь, а не трассировка
                check(f"{name}: стенд отвечает", "ответ", f"{type(error).__name__}: {error}")
    print(f"очередь пачек: {cases.paths} путей, {cases.failures} ПЛОХО")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
