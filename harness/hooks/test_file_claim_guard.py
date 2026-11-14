#!/usr/bin/env python3
"""Проба заявки на файл (scripts/hooks/file_claim_guard.py) и who-writes.py.

Случай 29.10.2026: помощники пишут в общем дереве, и `git commit -- путь` одного
забрал в коммит чужие правки mechanisms.yaml и probe-mutations.yaml — кто какой
файл пишет, не знал никто.

Что судит, на стенде (свой LOG_DIR, свой harness.conf, свой каталог пакета):
  * первая запись помощника пишет заявку {агент, полный путь}; повтор той же
    записи нового следа не пишет и молчит;
  * запись ДРУГОГО помощника в занятый путь — предупреждение с именем занявшего
    и счётчиком (не отказ: код 0), повтор — счётчик растёт;
  * окно живости — данные FILE_CLAIM_MIN: заявка старше окна не мешает, моложе —
    мешает (стенд с окном 7 мин, заявки 8 и 6 мин назад);
  * одноимённые файлы в разных каталогах друг друга не занимают;
  * общий файл — данные FILE_CLAIM_SHARED: своё предупреждение про вставку по
    якорю и `git commit -- путь`; необщий файл первой записью не предупреждает;
  * смена (нет agent_id) заявок не пишет; путь вне дерева и пакета — тоже;
  * Bash: перенаправление `>`/`>>`, `tee`, `sed -i` заявляют путь; чтение — нет;
  * хук никогда не роняет запись: битый журнал, LOG_DIR-файл, битый вход,
    нечисловое окно — код 0, при внутренней ошибке — строка skipped;
  * who-writes.py: живые заявки по журналу, `--count` — число занятых путей;
  * настоящее пересечение (12.11.2026: из 103 file.claim.warn за сутки настоящее —
    одно, судья скорости резал потолок по шуму): warn — только держатель другой
    задачи (задача — метка «Часть:» запуска, не карта; неизвестная — пересечение),
    путь не в .tasks/<своя задача>/, заявка держателя живая, пара (агент, путь) —
    один warn за окно; задача в file.claim — из того же источника;
  * исключительный замок (FILE_CLAIM_EXCLUSIVE) на стенде из двух git-деревьев —
    «проекта» и «пакета»: отказ кодом 2 чужой задаче, держатель — задача по метке
    «Часть:» запуска, снятие коммитом обоих деревьев, дверь на один путь, «брошено»,
    «без-заявки», цена (0 git-вызовов на обычном пути), само-ошибки хука.

Механизм берётся рядом с пробой либо из HARNESS_FILE_CLAIM_GUARD (дверь для
мутаций: копия с поломкой); команда — HARNESS_WHO_WRITES.
Прогон: python3 scripts/hooks/test_file_claim_guard.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = Path(os.environ.get("HARNESS_FILE_CLAIM_GUARD") or HERE / "file_claim_guard.py")
WHO = Path(os.environ.get("HARNESS_WHO_WRITES") or HERE.parent / "who-writes.py")
JOURNAL = "заявки-на-файлы.jsonl"
SRC = "scripts/hooks/file_claim_guard.py"
CLAIM_EVENT = "file.claim"
SHARED_REL = "harness/config/stand-shared.yaml"
HC = "harness/config/harness.conf"
MECH = "harness/config/mechanisms.yaml"
LIB_FILES = ("config.py", "events.py", "launch_plan_lib.py", "patterns.py")
LIB_DIR = HERE.parent / "lib"

checked = failed = 0


def check(name: str, expected, actual) -> None:
    global checked, failed
    checked += 1
    if expected == actual:
        print(f"  ок    {name}")
    else:
        failed += 1
        print(f"  ПЛОХО {name}: ждали «{expected}», вышло «{actual}»")


class Stand:
    """Свой каталог журналов, свой harness.conf, свой «пакет» — корень заявок."""

    def __init__(self, base: Path, window_min: str = "7", shared: str = SHARED_REL,
                 exclusive: str = "harness/config/stand-exclusive.yaml"):
        self.base = base
        self.logs = base / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.pkg = base / "pkg"
        self.pkg.mkdir(parents=True, exist_ok=True)
        self.conf = base / "harness.conf"
        self.conf.write_text(f'FILE_CLAIM_MIN="{window_min}"\nFILE_CLAIM_SHARED="{shared}"\n'
                             f'FILE_CLAIM_EXCLUSIVE="{exclusive}"\n', encoding="utf-8")

    def env(self) -> dict:
        return {**os.environ, "HARNESS_LOG_DIR": str(self.logs), "HARNESS_CONF": str(self.conf),
                "HARNESS_STARTER": str(self.pkg), "HARNESS_TASK": "hr.stand",
                "PYTHONDONTWRITEBYTECODE": "1"}

    def path(self, rel: str) -> str:
        return str(self.pkg / rel)

    def hook(self, payload, raw: str | None = None) -> tuple[int, str]:
        proc = subprocess.run([sys.executable, str(GUARD)], input=raw if raw is not None
                              else json.dumps(payload, ensure_ascii=False),
                              capture_output=True, text=True, env=self.env(), timeout=30)
        context = ""
        if proc.stdout.strip():
            try:
                context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
            except (ValueError, KeyError, TypeError):
                context = "НЕ-JSON:" + proc.stdout
        return proc.returncode, context

    def edit(self, agent: str | None, rel: str, tool: str = "Edit") -> tuple[int, str]:
        payload = {"tool_name": tool, "tool_input": {"file_path": self.path(rel)},
                   "session_id": "s-stand", "cwd": str(self.pkg)}
        if agent:
            payload.update(agent_id=agent, agent_type="harness-worker")
        return self.hook(payload)

    def bash(self, agent: str, command: str) -> tuple[int, str]:
        return self.hook({"tool_name": "Bash", "tool_input": {"command": command},
                          "session_id": "s-stand", "cwd": str(self.pkg), "agent_id": agent})

    def rows(self, event: str | None = None) -> list:
        try:
            lines = (self.logs / JOURNAL).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        found = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("src") == SRC and (event is None or row.get("event") == event):
                found.append(row)
        return found

    def claims(self, agent: str | None = None) -> list:
        return [r for r in self.rows(CLAIM_EVENT)
                if agent is None or (r.get("data") or {}).get("агент") == agent]

    def plant_claim(self, agent: str, rel: str, minutes_ago: float, task: str = "hr.other") -> None:
        ts = (dt.datetime.now().astimezone() - dt.timedelta(minutes=minutes_ago)).isoformat(
            timespec="seconds")
        row = {"v": 1, "ts": ts, "src": SRC, "event": CLAIM_EVENT, "outcome": "ok",
               "task": task, "task_from": "arg", "session": "s-old",
               "data": {"путь": self.path(rel), "агент": agent, "тип": "harness-worker"}}
        with (self.logs / JOURNAL).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def who(self, *args: str) -> tuple[int, str]:
        proc = subprocess.run([sys.executable, str(WHO), *args], capture_output=True,
                              text=True, env=self.env(), timeout=30)
        return proc.returncode, proc.stdout


def judge_claim_and_conflict(tmp: Path) -> None:
    stand = Stand(tmp / "conflict")
    code, context = stand.edit("agentA", "src/app.py")
    rows = stand.claims("agentA")
    check("первая запись помощника: код 0 и без предупреждения", (0, ""), (code, context))
    check("первая запись пишет одну заявку", 1, len(rows))
    check("в заявке полный путь", stand.path("src/app.py"),
          (rows[0].get("data") or {}).get("путь") if rows else None)
    check("в заявке задача не из HARNESS_TASK: без метки запуска — «агент:<id>»", "агент:agentA",
          rows[0].get("task") if rows else None)
    code, context = stand.edit("agentA", "src/app.py", tool="Write")
    check("повтор своей записи: молча", (0, ""), (code, context))
    check("повтор своей записи нового следа не пишет", 1, len(stand.claims("agentA")))

    code, context = stand.edit("agentB", "src/app.py")
    check("чужая запись в занятый путь: не отказ (код 0)", 0, code)
    check("предупреждение называет занявшего", True, "agentA" in context)
    check("предупреждение со счётчиком №1", True, "№1" in context)
    check("вторая запись тоже заявлена", 1, len(stand.claims("agentB")))
    code, context = stand.edit("agentB", "src/app.py")
    check("повтор чужой записи: счётчик №2", True, "№2" in context)
    check("повтор пары (агент, путь) за окно — второго file.claim.warn нет", 1,
          len(stand.rows("file.claim.warn")))

    code, context = stand.edit("agentB", "other/app.py")
    check("одноимённый файл в другом каталоге не занят", (0, ""), (code, context))


def judge_window(tmp: Path) -> None:
    stand = Stand(tmp / "window", window_min="7")
    stand.plant_claim("agentOld", "old.py", minutes_ago=8)
    stand.plant_claim("agentFresh", "fresh.py", minutes_ago=6)
    check("заявка старше окна из данных (8 > 7 мин) не мешает", (0, ""),
          stand.edit("agentB", "old.py"))
    code, context = stand.edit("agentB", "fresh.py")
    check("заявка моложе окна из данных (6 < 7 мин) мешает", True, "agentFresh" in context)
    stand.plant_claim("agentB", "mine.py", minutes_ago=5)
    stand.edit("agentB", "mine.py")
    check("своя заявка старше половины окна обновляется", 2, len(
        [r for r in stand.claims("agentB") if r["data"]["путь"] == stand.path("mine.py")]))


def judge_shared(tmp: Path) -> None:
    stand = Stand(tmp / "shared")
    code, context = stand.edit("agentA", SHARED_REL)
    check("общий файл из данных: предупреждение на первой записи", True, "общий файл" in context)
    check("общий файл: про вставку по якорю", True, "якор" in context)
    check("общий файл: про git commit -- путь", True, "git commit --" in context)
    code, context = stand.edit("agentB", SHARED_REL)
    check("общий файл, занятый другим: занявший назван", True, "agentA" in context)
    check("общий файл, занятый другим: текст общего файла", True, "общий файл" in context)
    code, context = stand.edit("agentA", "plain.py")
    check("необщий файл первой записью не предупреждает", (0, ""), (code, context))
    other = Stand(tmp / "shared-other", shared="harness/config/another.yaml")
    check("общий список — из данных: на стенде без этого пути предупреждения нет", (0, ""),
          other.edit("agentA", SHARED_REL))


def judge_who_is_writing(tmp: Path) -> None:
    stand = Stand(tmp / "who")
    stand.edit(None, "shift.py")
    check("смена (нет agent_id) заявок не пишет", 0, len(stand.claims()))
    stand.hook({"tool_name": "Edit", "tool_input": {"file_path": str(tmp / "outside.py")},
                "agent_id": "agentA"})
    check("путь вне дерева и пакета не заявляется", 0, len(stand.claims()))
    stand.hook({"tool_name": "Read", "tool_input": {"file_path": stand.path("r.py")},
                "agent_id": "agentA"})
    check("чтение не заявляется", 0, len(stand.claims()))


def judge_bash(tmp: Path) -> None:
    stand = Stand(tmp / "bash")
    cases = [
        ("перенаправление >", "echo x > red.txt", "red.txt"),
        ("перенаправление >> без пробела", "printf y>>app.log", "app.log"),
        ("tee -a", "echo z | tee -a teed.txt", "teed.txt"),
        ("sed -i", "sed -i 's/a/b/' sedded.yaml", "sedded.yaml"),
        ("полный путь", f"cat a > {stand.path('abs.txt')}", "abs.txt"),
    ]
    for name, command, rel in cases:
        stand.bash("agentA", command)
        paths = {(r.get("data") or {}).get("путь") for r in stand.claims("agentA")}
        check(f"Bash: {name} заявляет путь", True, stand.path(rel) in paths)
    before = len(stand.claims())
    stand.bash("agentA", "cat red.txt 2>/dev/null; grep -n x app.log")
    check("Bash: чтение и 2>/dev/null не заявляют", before, len(stand.claims()))
    code, context = stand.edit("agentB", "red.txt")
    check("заявка из Bash видна Edit другого помощника", True, "agentA" in context)
    stand.bash("agentH", "cat > doc.txt <<'EOF'\nтекст > body.txt, и >> ещё\nEOF\necho x > after.txt")
    paths = {(r.get("data") or {}).get("путь") for r in stand.claims("agentH")}
    check("here-doc: цель cat заявлена, тело — нет, запись после тела — да",
          {stand.path("doc.txt"), stand.path("after.txt")}, paths)


def judge_never_breaks(tmp: Path) -> None:
    stand = Stand(tmp / "empty")
    check("пустой журнал: запись проходит", (0, ""), stand.edit("agentA", "e.py"))
    (stand.logs / JOURNAL).write_text("мусор\n{не json\n", encoding="utf-8")
    check("битый журнал: запись проходит", 0, stand.edit("agentB", "e2.py")[0])
    check("битый журнал: заявка всё равно пишется", 1, len(stand.claims("agentB")))
    check("битый вход: код 0", 0, stand.hook(None, raw="{нет")[0])
    check("вход-массив: код 0", 0, stand.hook(None, raw="[1, 2]")[0])

    broken = Stand(tmp / "bad-window", window_min="abc")
    check("нечисловое окно: код 0", 0, broken.edit("agentA", "w.py")[0])
    skipped = [r for r in broken.rows() if r.get("outcome") == "skipped"]
    check("внутренняя ошибка оставляет строку skipped", True, bool(skipped))

    as_file = Stand(tmp / "log-is-file")
    as_file.logs.rmdir()
    as_file.logs.write_text("", encoding="utf-8")
    check("LOG_DIR — файл, а не каталог: код 0", 0, as_file.edit("agentA", "f.py")[0])


def judge_who_writes(tmp: Path) -> None:
    stand = Stand(tmp / "list", window_min="7")
    stand.plant_claim("agentA", "a.py", minutes_ago=1)
    stand.plant_claim("agentB", "a.py", minutes_ago=2)
    stand.plant_claim("agentC", "c.py", minutes_ago=3)
    stand.plant_claim("agentOld", "old.py", minutes_ago=9)
    code, out = stand.who("--count")
    check("who-writes --count: живых путей 2 (старый не в счёт)", (0, "2"), (code, out.strip()))
    code, out = stand.who()
    check("who-writes называет обоих на одном пути", True, "agentA" in out and "agentB" in out)
    check("who-writes не называет устаревшую заявку", False, "agentOld" in out)
    empty = Stand(tmp / "list-empty")
    check("who-writes --count на пустом журнале: 0", (0, "0"),
          tuple(x.strip() if isinstance(x, str) else x for x in empty.who("--count")))


# ───────────────────────── исключительный замок ─────────────────────────

class TreeStand(Stand):
    """«Проект» (с копией хука и scripts/lib) и «пакет» — два git-дерева с одним
    исключительным файлом под одним относительным путём; свои журналы и conf."""

    def __init__(self, base: Path, window_min: str = "7", package_git: bool = True,
                 commit_files: tuple = (HC, MECH)):
        super().__init__(base, window_min, exclusive=f"{HC} {MECH}")
        self.proj = base / "proj"
        self.transcripts = base / "transcripts"
        for rel in ("harness/config", "scripts/hooks", "scripts/lib"):
            (self.proj / rel).mkdir(parents=True, exist_ok=True)
        shutil.copy(GUARD, self.proj / "scripts/hooks/file_claim_guard.py")
        shutil.copy(WHO, self.proj / "scripts/who-writes.py")
        for name in LIB_FILES:
            shutil.copy(LIB_DIR / name, self.proj / "scripts/lib" / name)
        (self.pkg / "harness/config").mkdir(parents=True, exist_ok=True)
        self.bin_path = os.environ["PATH"]
        for root, with_git in ((self.proj, True), (self.pkg, package_git)):
            for rel in (HC, MECH):
                (root / rel).write_text("# стенд\n", encoding="utf-8")
            if with_git:
                self.git(root, "init", "-q")
                for rel in commit_files:
                    self.commit(root, rel, ago_s=3600)

    def env(self) -> dict:
        return {**super().env(), "PATH": self.bin_path}

    def git(self, root: Path, *args: str, date: str | None = None) -> None:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)

    def commit(self, root: Path, rel: str, ago_s: float = 0) -> None:
        moment = dt.datetime.now().astimezone() - dt.timedelta(seconds=ago_s)
        self.git(root, "add", rel)
        self.git(root, "commit", "-q", "-m", "c", date=moment.isoformat(timespec="seconds"))

    def commit_both(self, rel: str) -> None:
        for root in (self.proj, self.pkg):
            self.commit(root, rel)

    def dirty(self, rel: str = HC, where: tuple = ("proj", "pkg")) -> None:
        for name in where:
            root = self.proj if name == "proj" else self.pkg
            with (root / rel).open("a", encoding="utf-8") as fh:
                fh.write(f"# ханк {time.time()}\n")

    def path(self, rel: str, where: str = "proj") -> str:
        return str((self.proj if where == "proj" else self.pkg) / rel)

    def launch(self, agent: str, label: str = "", parent_call: str = "", task: str = "-") -> str:
        """Строка agent.launch и meta.json помощника; возвращает tool_use_id."""
        call = f"toolu_{agent}"
        row = {"v": 1, "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
               "src": "scripts/hooks/agent_budget_guard.py", "event": "agent.launch",
               "outcome": "ok", "task": task,
               "data": {"вызов": call, "часть": label, "родитель": parent_call}}
        with (self.logs / "вызовы-агентов.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        meta = self.transcripts / "s-stand" / "subagents" / f"agent-{agent}.meta.json"
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({"toolUseId": call, "agentType": "harness-worker"}),
                        encoding="utf-8")
        return call

    def part(self, agent: str, task: str) -> str:
        return self.launch(agent, label=f"Часть: {task}#1")

    def run(self, payload: dict, env: dict | None = None) -> tuple[int, str, str]:
        payload = {"session_id": "s-stand", "cwd": str(self.proj),
                   "transcript_path": str(self.transcripts / "s-stand.jsonl"), **payload}
        proc = subprocess.run([sys.executable, str(self.proj / "scripts/hooks/file_claim_guard.py")],
                              input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                              text=True, env=env or self.env(), timeout=30)
        context = ""
        if proc.stdout.strip():
            context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        return proc.returncode, context, proc.stderr

    def go(self, agent: str | None, rel: str = HC, where: str = "proj",
           env: dict | None = None) -> tuple[int, str, str]:
        payload = {"tool_name": "Edit", "tool_input": {"file_path": self.path(rel, where)}}
        if agent:
            payload.update(agent_id=agent, agent_type="harness-worker")
        return self.run(payload, env)

    def sh(self, agent: str, command: str) -> tuple[int, str, str]:
        return self.run({"tool_name": "Bash", "tool_input": {"command": command},
                         "agent_id": agent, "agent_type": "harness-worker"})

    def door(self, rel: str, reason: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.proj / "scripts/hooks/file_claim_guard.py"),
                               "--door", rel, reason], capture_output=True, text=True,
                              env=self.env(), cwd=self.proj, timeout=30)

    def refused(self) -> list:
        return self.rows("file.claim.refused")

    def who(self, *args: str) -> tuple[int, str]:
        proc = subprocess.run([sys.executable, str(self.proj / "scripts/who-writes.py"), *args],
                              capture_output=True, text=True, env=self.env(), timeout=30)
        return proc.returncode, proc.stdout


def held_stand(tmp: Path, name: str, **kwargs) -> TreeStand:
    """Задача hr.A (помощник a1) уже писала в HC, ханки не закоммичены; b1 — hr.B."""
    stand = TreeStand(tmp / name, **kwargs)
    stand.part("a1", "hr.A")
    stand.part("b1", "hr.B")
    stand.go("a1")
    stand.dirty()
    return stand


def judge_lock_sick_cases(tmp: Path) -> None:
    stand = TreeStand(tmp / "s2")
    stand.part("a1", "hr.A")
    stand.part("b1", "hr.B")
    code, context, _ = stand.go("a1")
    check("С2: первая запись держателя на чистый файл — код 0", 0, code)
    check("первая заявка на исключительный путь — напоминание «закоммить сразу»", True,
          "закоммить свои строки" in context)
    claim = stand.claims("a1")[0]
    check("в заявке задача из метки запуска", "hr.A", claim.get("task"))
    stand.dirty()
    code, context, err = stand.go("b1")
    check("С2: чужая задача в грязный общий конфиг — отказ, код 2", 2, code)
    check("отказ пишется в stderr, stdout пуст", "", context)
    check("отказ называет задачу держателя, помощника, тип и дверь", [True] * 4,
          ["hr.A" in err, "a1" in err, "harness-worker" in err, "--door" in err])
    refused = stand.refused()
    check("строка file.claim.refused с видом «занято»", ["занято"],
          [(r.get("data") or {}).get("вид") for r in refused])
    check("отказанный заявки не пишет", 0, len(stand.claims("b1")))
    check("после отказа держатель по-прежнему A: A пишет — код 0", 0, stand.go("a1")[0])

    stand = held_stand(tmp, "s1")
    stand.dirty(MECH)
    stand.go("a1", MECH)
    stand.dirty(MECH)
    mech = stand.path(MECH)
    check("С1: sed -i в чужой исключительный файл — отказ", 2,
          stand.sh("b1", f"sed -i 's/a/b/' {mech}")[0])
    check("С1: cat > путь <<EOF в чужой исключительный файл — отказ", 2,
          stand.sh("b1", f"cat > {mech} <<EOF\nx\nEOF")[0])
    check("Bash с несколькими путями: отказ по одному из них отказывает всё", 2,
          stand.sh("b1", f"echo x > {stand.path('free.txt')}; echo y > {mech}")[0])


def judge_lock_pair(tmp: Path) -> None:
    stand = TreeStand(tmp / "pair")
    stand.part("a1", "hr.A")
    stand.part("b1", "hr.B")
    stand.go("a1", HC, where="pkg")
    stand.dirty()
    check("пакетная пара: A пишет пакетный файл, B проектный — отказ", 2, stand.go("b1")[0])
    time.sleep(1.1)
    stand.commit(stand.proj, HC)
    check("проект A закоммичен, пакет ещё нет — B всё равно отказ", 2, stand.go("b1")[0])
    stand.commit(stand.pkg, HC)
    check("оба дерева закоммичены — B проходит", 0, stand.go("b1")[0])


def judge_lock_abandoned_and_unknown(tmp: Path) -> None:
    stand = TreeStand(tmp / "abandoned", window_min="7")
    stand.part("b1", "hr.B")
    stand.plant_claim("a1", HC, minutes_ago=10, task="hr.A")
    stand.dirty()
    code, _, err = stand.go("b1")
    check("брошено: заявка старше окна, файл грязный — отказ", 2, code)
    check("брошено: вид в журнале", ["брошено"],
          [(r.get("data") or {}).get("вид") for r in stand.refused()])
    check("брошено: текст называет оркестратора", True, "оркестратор" in err)

    stand = TreeStand(tmp / "no-claim")
    stand.part("b1", "hr.B")
    stand.dirty()
    code, _, err = stand.go("b1")
    check("без заявки: файл грязный, заявок нет — отказ", 2, code)
    check("без заявки: вид «без-заявки»", ["без-заявки"],
          [(r.get("data") or {}).get("вид") for r in stand.refused()])
    check("без заявки: текст про дверь с причиной", True, "дверь с причиной" in err)

    stand = TreeStand(tmp / "unknown")
    stand.part("a1", "hr.A")
    stand.go("a1")
    stand.dirty()
    code, _, err = stand.go("zz9")
    check("неизвестный помощник (нет meta.json) против A — отказ", 2, code)
    check("отказанный назван как «агент:<id>»", ["агент:zz9"],
          [r.get("task") for r in stand.refused()])
    stand = TreeStand(tmp / "unknown-holder")
    stand.part("a1", "hr.A")
    stand.go("zz9")
    stand.dirty()
    code, _, err = stand.go("a1")
    check("держатель — неизвестный помощник: A отказ, держатель назван", (2, True),
          (code, "агент:zz9" in err))


def judge_lock_shift(tmp: Path) -> None:
    stand = held_stand(tmp, "shift-vs-a")
    code, _, err = stand.go(None)
    check("смена против держателя A — отказ", (2, True), (code, "hr.A" in err))

    stand = TreeStand(tmp / "a-vs-shift")
    stand.part("a1", "hr.A")
    stand.go(None)
    stand.dirty()
    code, _, err = stand.go("a1")
    check("A против держателя «смена» — отказ", (2, True), (code, "смена" in err))
    row = stand.claims()[0]
    check("строка заявки смены: task=смена, data.агент=-", ("смена", "-"),
          (row.get("task"), (row.get("data") or {}).get("агент")))
    sys.path.insert(0, str(LIB_DIR))
    import launch_plan_lib
    check("launch_plan_lib не считает смену помощником", None, launch_plan_lib.agent_id(row))
    sys.path.pop(0)


def judge_lock_same_task_and_parent(tmp: Path) -> None:
    stand = held_stand(tmp, "same-task")
    stand.part("a2", "hr.A")
    stand.launch("a3", label="Часть: hr.A#2")
    code, _, err = stand.go("a2")
    check("второй помощник той же задачи — код 0, без отказа", (0, 0), (code, len(stand.refused())))

    stand = TreeStand(tmp / "parent")
    manager = stand.launch("m1", label="Часть: hr.A#0")
    stand.launch("c1", label="", parent_call=manager, task="hr.card")
    stand.part("b1", "hr.B")
    stand.go("c1")
    stand.dirty()
    check("помощник без метки: задача — метка запуска родителя, не поле task", "hr.A",
          stand.claims("c1")[0].get("task"))
    check("задача родителя совпала с A — код 0", 0, stand.go("m1")[0])
    code, _, err = stand.go("b1")
    check("чужая задача против держателя по родителю — отказ с hr.A", (2, True),
          (code, "hr.A" in err))

    stand = TreeStand(tmp / "label-vs-task")
    stand.launch("a1", label="Часть: hr.A#1", task="-")
    stand.launch("b1", label="", task="hr.card")
    stand.go("a1")
    check("метка hr.A при task=«-»: держатель по метке", "hr.A", stand.claims("a1")[0].get("task"))
    stand.go("b1")
    check("нет метки и родителя: «агент:id», а не task карты", "агент:b1",
          stand.claims("b1")[0].get("task"))


def judge_lock_release(tmp: Path) -> None:
    stand = held_stand(tmp, "release")
    time.sleep(1.1)
    stand.commit_both(HC)
    code, _, _ = stand.go("b1")
    check("снятие коммитом проекта и пакета: B пишет — код 0", 0, code)
    check("заявка B с задачей hr.B", "hr.B", stand.claims("b1")[0].get("task"))
    stand.dirty()
    check("старая заявка A не мешает B при новой грязи", 0, stand.go("b1")[0])
    check("A после коммита стала чужой: отказ «занято»", 2, stand.go("a1")[0])

    stand = held_stand(tmp, "partial")
    time.sleep(1.1)
    stand.commit_both(HC)
    time.sleep(1.1)
    stand.dirty()
    stand.go("b1")
    check("частичный коммит: граница отсекла заявки, последняя A — B «без-заявки»", ["без-заявки"],
          [(r.get("data") or {}).get("вид") for r in stand.refused()])
    check("частичный коммит: A пишет — код 0", 0, stand.go("a1")[0])
    stand.go("b1")
    check("после записи A держатель назван: B «занято»", ["без-заявки", "занято"],
          [(r.get("data") or {}).get("вид") for r in stand.refused()])

    stand = TreeStand(tmp / "old-episode")
    stand.part("a1", "hr.A")
    stand.plant_claim("a1", HC, minutes_ago=120, task="hr.A")
    stand.dirty()
    time.sleep(1.1)
    stand.commit_both(HC)
    time.sleep(1.1)
    stand.dirty()
    code, _, err = stand.go("a1")
    check("своя заявка до предпоследнего коммита — не хвост: «без-заявки»", (2, ["без-заявки"]),
          (code, [(r.get("data") or {}).get("вид") for r in stand.refused()][-1:]))

    stand = TreeStand(tmp / "own-agent")
    stand.part("a1", "hr.A")
    stand.plant_claim("a1", HC, minutes_ago=3, task="-")
    stand.dirty()
    check("старая заявка того же агента с task=«-» — своя, код 0", 0, stand.go("a1")[0])


def judge_lock_refresh_and_junk(tmp: Path) -> None:
    for minutes_ago, expected in ((4, 2), (2, 1)):
        stand = TreeStand(tmp / f"half-window-{minutes_ago}", window_min="7")
        stand.part("a1", "hr.A")
        stand.plant_claim("a1", HC, minutes_ago=minutes_ago, task="hr.A")
        stand.dirty()
        check(f"держатель пишет, его заявка {minutes_ago} мин при окне 7: код 0", 0, stand.go("a1")[0])
        check(f"заявка держателя старше половины окна освежается ({minutes_ago} мин)", expected,
              len(stand.claims("a1")))

    stand = held_stand(tmp, "junk")
    stand.dirty()
    with (stand.logs / JOURNAL).open("a", encoding="utf-8") as fh:
        fh.write('мусор\n{не json\n[1]\n"строка"\n{"ts": 5}\n{"ts": "не время"}\n')
    code, _, err = stand.go("b1")
    check("мусорные строки журнала не ломают разбор соседних: B «занято», держатель A", (2, True),
          (code, "hr.A" in err))
    check("мусор в журнале: A пишет — код 0", 0, stand.go("a1")[0])


def judge_lock_script_writes(tmp: Path) -> None:
    stand = held_stand(tmp, "script")
    reading = (f"python3 - <<'EOF'\nimport yaml\n"
               f"print(yaml.safe_load(open('{HC}').read()))\nEOF")
    code, _, _ = stand.sh("b1", reading)
    check("здоровый: python читает исключительный путь — пропуск без заявки", (0, 0),
          (code, len(stand.claims("b1"))))
    check("здоровый: cat и grep пути — пропуск", 0,
          stand.sh("b1", f"cat {HC} | grep x; grep -n y {HC}")[0])
    writing_c = f"python3 -c \"from pathlib import Path; Path('{HC}').write_text('x')\""
    check("больной: python3 -c пишет в путь чужой задачи — отказ", 2, stand.sh("b1", writing_c)[0])
    writing_doc = (f"python3 - <<'EOF'\nimport yaml\n"
                   f"yaml.safe_dump({{}}, open('{stand.path(HC, 'pkg')}', 'w'))\nEOF")
    check("больной: here-doc python пишет пакетный путь чужой задачи — отказ", 2,
          stand.sh("b1", writing_doc)[0])
    check("держатель пишет python-ом в свой путь — код 0", 0, stand.sh("a1", writing_c)[0])


def judge_lock_door(tmp: Path) -> None:
    stand = held_stand(tmp, "door")
    check("короткая причина двери: код 1", 1, stand.door(HC, "мало").returncode)
    check("короткая причина: двери нет", False, (stand.logs / "file-claim.door").exists())
    check("дверь не открыта — отказ", 2, stand.go("b1")[0])
    check("дверь открыта: код 0", 0, stand.door(HC, "срочная правка, держатель молчит").returncode)
    code, _, _ = stand.go("b1")
    check("дверь пропускает B", 0, code)
    gate = [r for r in stand.claims("b1") if (r.get("data") or {}).get("дверь")]
    check("пропуск по двери — строка с data.дверь и причиной", ["срочная правка, держатель молчит"],
          [(r.get("data") or {}).get("дверь") for r in gate])
    check("строка открытия двери в журнале", 1, len(stand.rows("file.claim.door")))
    stand.go("a1", MECH)
    stand.dirty(MECH)
    check("дверь на другой путь не пропускает", 2, stand.go("b1", MECH)[0])
    door = stand.logs / "file-claim.door"
    door.write_text(json.dumps({"путь": MECH, "почему": "просроченная дверь тест",
                                "до": int(time.time()) - 5}), encoding="utf-8")
    check("пропуск B по двери не делает его держателем: A при закрытой двери — код 0", 0,
          stand.go("a1")[0])
    check("просроченная дверь не пропускает", 2, stand.go("b1", MECH)[0])
    check("дверь на путь вне FILE_CLAIM_EXCLUSIVE: код 1", 1,
          stand.door("src/app.py", "причина достаточной длины").returncode)


def judge_lock_not_exclusive(tmp: Path) -> None:
    stand = TreeStand(tmp / "plain")
    stand.part("a1", "hr.A")
    stand.part("b1", "hr.B")
    stand.go("a1", "src/app.py")
    code, context, _ = stand.go("b1", "src/app.py")
    check("не исключительный путь с чужой заявкой: код 0 и предупреждение", (0, True),
          (code, "a1" in context))
    stand.go("a1", SHARED_REL)
    code, context, _ = stand.go("b1", SHARED_REL)
    check("общий (SHARED) путь: предупреждение, не отказ", (0, True), (code, "общий файл" in context))


def judge_lock_worktree_path(tmp: Path) -> None:
    """Характеристика Р-2 (спека §2, Т4): путь в дереве помощника
    `.claude/worktrees/<id>/…` даёт ключ не из FILE_CLAIM_EXCLUSIVE — замка нет, а заявка
    пишется и питает «живость». Зелёная ДО кода: хук не меняется, проба держит, что так и
    останется. Поломка, что оставила бы зелёной: нет — снятие префикса `.claude/worktrees/<id>/`
    в tree_rel сделало бы ключ исключительным, и первая строка покраснела бы."""
    stand = held_stand(tmp, "worktree")
    tree = stand.proj / ".claude" / "worktrees" / "agent-b1"
    stand.git(stand.proj, "worktree", "add", "-q", "-b", "worktree-agent-b1", str(tree))
    with (tree / HC).open("a", encoding="utf-8") as fh:
        fh.write("# ханк помощника в своём дереве\n")
    before = len(stand.refused())
    code, _, err = stand.go("b1", f".claude/worktrees/agent-b1/{HC}")
    check("Т4: чужая грязь в исключительном файле основного, запись в тот же путь своего "
          "дерева — не отказ", (0, ""), (code, err))
    check("Т4: строки отказа нет", before, len(stand.refused()))
    check("Т4: заявка записана с путём дерева", [str(tree / HC)],
          [(r.get("data") or {}).get("путь") for r in stand.claims("b1")])


def judge_lock_never_breaks(tmp: Path) -> None:
    stand = TreeStand(tmp / "nogit")
    stand.part("b1", "hr.B")
    env = {**stand.env(), "PATH": str(tmp)}
    code, _, _ = stand.go("b1", env=env)
    check("нет git в стенде: код 0", 0, code)
    check("нет git: строка skipped", True, any(r.get("outcome") == "skipped" for r in stand.rows()))

    stand = TreeStand(tmp / "bad-meta")
    stand.part("b1", "hr.B")
    (stand.transcripts / "s-stand/subagents/agent-b1.meta.json").write_text("{не json", encoding="utf-8")
    check("битый meta.json: код 0", 0, stand.go("b1")[0])
    check("битый meta.json: строка skipped", True,
          any(r.get("outcome") == "skipped" for r in stand.rows()))

    stand = TreeStand(tmp / "bad-launch")
    stand.part("b1", "hr.B")
    with (stand.logs / "вызовы-агентов.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": dt.datetime.now().astimezone().isoformat(),
                             "event": "agent.launch", "data": "не словарь"}) + "\n")
    check("битая строка запуска: код 0, соседние строки разобраны", (0, "hr.B"),
          (stand.go("b1")[0], stand.claims("b1")[0].get("task")))
    check("битая строка запуска замок не выключает: skipped нет", False,
          any(r.get("outcome") == "skipped" for r in stand.rows()))

    stand = TreeStand(tmp / "bad-journal")
    stand.part("b1", "hr.B")
    (stand.logs / JOURNAL).write_text("мусор\n{не json\n", encoding="utf-8")
    check("битый журнал заявок, файл чист: код 0", 0, stand.go("b1")[0])

    stand = TreeStand(tmp / "clean")
    stand.part("b1", "hr.B")
    check("чистый файл без заявок: код 0", (0, 0), (stand.go("b1")[0], len(stand.refused())))


def judge_lock_no_package(tmp: Path) -> None:
    stand = TreeStand(tmp / "no-pkg", package_git=False)
    stand.part("a1", "hr.A")
    stand.part("b1", "hr.B")
    stand.go("a1")
    stand.dirty(HC, where=("proj",))
    check("пакета нет (не репозиторий): замок по проекту — отказ", 2, stand.go("b1")[0])

    stand = TreeStand(tmp / "not-in-head", commit_files=(HC,))
    stand.part("b1", "hr.B")
    code, _, _ = stand.go("b1", MECH)
    check("файла нет в HEAD проекта: код 0", 0, code)
    check("файла нет в HEAD: строка skipped", True,
          any(r.get("outcome") == "skipped" for r in stand.rows()))


def judge_lock_cost(tmp: Path) -> None:
    stand = TreeStand(tmp / "cost")
    stand.part("a1", "hr.A")
    counter = tmp / "git-calls"
    wrapper = tmp / "gitbin"
    wrapper.mkdir()
    (wrapper / "git").write_text(f'#!/bin/sh\necho x >> {counter}\nexec {shutil.which("git")} "$@"\n')
    (wrapper / "git").chmod(0o755)
    stand.bin_path = f"{wrapper}:{os.environ['PATH']}"
    stand.go(None, "src/plain.py")
    stand.go("a1", "src/plain.py")
    check("обычный путь у смены и помощника: 0 вызовов git", False, counter.exists())
    stand.go("a1")
    check("живость счётчика: исключительный путь зовёт git", True, counter.exists())


def judge_real_intersection(tmp: Path) -> None:
    def warns(stand: TreeStand, agent: str) -> int:
        return len([r for r in stand.rows("file.claim.warn")
                    if (r.get("data") or {}).get("агент") == agent])

    stand = TreeStand(tmp / "same-task-workers")
    stand.part("a1", "hr.A")
    stand.launch("a2", label="Часть: hr.A#2")
    stand.go("a1", "src/app.py")
    check("а) два исполнителя одной задачи, один файл: код 0 и молча", (0, ""),
          stand.go("a2", "src/app.py")[:2])
    check("а) два исполнителя одной задачи: file.claim.warn нет", 0, warns(stand, "a2"))

    journal = ".tasks/hr.A/журнал.md"
    stand = TreeStand(tmp / "own-journal")
    stand.launch("m1", label="Часть: hr.A#0")
    stand.part("w1", "hr.A")
    stand.go("m1", journal)
    check("б) исполнитель в журнал своей задачи, держатель — её управляющий: молча", (0, ""),
          stand.go("w1", journal)[:2])
    check("б) журнал своей задачи: file.claim.warn нет", 0, warns(stand, "w1"))

    stand = TreeStand(tmp / "own-card-other-holder")
    stand.part("b1", "hr.B")
    stand.part("w1", "hr.A")
    stand.go("b1", journal)
    check("б2) путь в .tasks/<своя задача>/ при держателе чужой задачи: file.claim.warn нет",
          0, (stand.go("w1", journal), warns(stand, "w1"))[1])
    code, context, _ = stand.go("b1", journal)
    check("б3) живость: чужая задача пишет в карточку hr.A — warn есть", (0, True, 1),
          (code, "w1" in context, warns(stand, "b1")))

    stand = TreeStand(tmp / "two-tasks")
    stand.part("a1", "hr.A")
    stand.part("b1", "hr.B")
    stand.go("a1", "src/app.py")
    first = stand.go("b1", "src/app.py")
    stand.go("b1", "src/app.py")
    stand.go("b1", "src/app.py")
    check("в) разные задачи, один код-файл: первая правка предупреждает", (0, True),
          (first[0], "a1" in first[1]))
    check("в) три правки подряд — file.claim.warn ровно один", 1, warns(stand, "b1"))

    stand = TreeStand(tmp / "unknown-side")
    stand.part("a1", "hr.A")
    stand.go("a1", "src/app.py")
    check("е) задачу пишущего установить нельзя (нет meta.json) — пересечение, warn", 1,
          (stand.go("zz9", "src/app.py"), warns(stand, "zz9"))[1])

    stand = TreeStand(tmp / "abandoned-holder", window_min="7")
    stand.part("b1", "hr.B")
    stand.plant_claim("a1", "src/app.py", minutes_ago=8, task="hr.A")
    stand.plant_claim("a1", "src/fresh.py", minutes_ago=6, task="hr.A")
    check("г) держатель чужой задачи брошен (8 мин при окне 7): молча, warn нет", ((0, ""), 0),
          (stand.go("b1", "src/app.py")[:2], warns(stand, "b1")))
    check("г) живость границы: держатель 6 мин при окне 7 — warn есть", 1,
          (stand.go("b1", "src/fresh.py"), warns(stand, "b1"))[1])

    stand = TreeStand(tmp / "shared-first")
    stand.part("a1", "hr.A")
    code, context, _ = stand.go("a1", SHARED_REL)
    check("ж) общий файл, первая запись без держателя: напоминание есть, file.claim.warn нет",
          (0, True, 0), (code, "общий файл" in context, warns(stand, "a1")))

    stand = TreeStand(tmp / "task-from-label")
    devmap = stand.base / "dev-map.yaml"
    devmap.write_text("tasks:\n  - id: hr.map-wip\n    status: wip\n"
                      "  - id: hr.A\n    status: plan\n", encoding="utf-8")
    env = {k: v for k, v in stand.env().items() if k != "HARNESS_TASK"}
    env["HARNESS_DEVMAP"] = str(devmap)
    stand.part("a1", "hr.A")
    stand.go("a1", "src/x.py", env=env)
    rows = stand.claims("a1")
    check("д) задача в file.claim — из метки «Часть:», не из единственной wip карты", "hr.A",
          rows[0].get("task") if rows else None)
    lines = [line for line in stand.who()[1].splitlines() if "src/x.py" in line or "a1" in line]
    check("д) who-writes называет задачу по метке", True, any("hr.A" in line for line in lines))


def judge_lock_who_writes(tmp: Path) -> None:
    stand = TreeStand(tmp / "who-lock", window_min="7")
    stand.plant_claim("a1", HC, minutes_ago=10, task="hr.A")
    stand.plant_claim("b1", MECH, minutes_ago=2, task="hr.B")
    stand.dirty(HC)
    stand.dirty(MECH)
    code, out = stand.who()
    check("who-writes показывает задачу держателя", True, "hr.A" in out and "hr.B" in out)
    brosheno = [line for line in out.splitlines() if "hr.A" in line]
    check("who-writes: заявка старше окна — «брошено»", True,
          bool(brosheno) and "брошено" in brosheno[0])
    fresh = [line for line in out.splitlines() if "hr.B" in line]
    check("who-writes: свежая — «держит до коммита»", True,
          bool(fresh) and "держит до коммита" in fresh[0])
    check("who-writes --count: оба пути", "2", stand.who("--count")[1].strip())
    time.sleep(1.1)
    stand.commit_both(HC)
    check("who-writes: закоммиченный путь никого не держит", True, "hr.A" not in stand.who()[1])

    stand = TreeStand(tmp / "who-orphan")
    check("who-writes: чистый путь без заявок — «заявок нет»", True, "нет" in stand.who()[1])
    stand.dirty(HC)
    out = stand.who()[1]
    check("who-writes: грязный путь без заявок назван «ханки без заявки»", True,
          f"{HC}  — ханки без заявки (держатель неизвестен)" in out)
    check("who-writes --count считает путь без заявки", "1", stand.who("--count")[1].strip())


def main() -> int:
    for name, path in (("механизма", GUARD), ("команды who-writes", WHO)):
        if not path.exists():
            print(f"  ПЛОХО {name} нет: {path}")
            print(f"SELFTEST: КРАСНЫЙ ({name} нет)")
            return 1
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        judge_claim_and_conflict(tmp)
        judge_window(tmp)
        judge_shared(tmp)
        judge_who_is_writing(tmp)
        judge_bash(tmp)
        judge_never_breaks(tmp)
        judge_who_writes(tmp)
        for judge in (judge_lock_sick_cases, judge_lock_pair, judge_lock_abandoned_and_unknown,
                      judge_lock_shift, judge_lock_same_task_and_parent, judge_lock_release,
                      judge_lock_refresh_and_junk, judge_lock_script_writes, judge_lock_door,
                      judge_lock_not_exclusive, judge_lock_worktree_path,
                      judge_lock_never_breaks,
                      judge_lock_no_package, judge_lock_cost, judge_lock_who_writes,
                      judge_real_intersection):
            judge(tmp)
    print(f"SELFTEST: {'зелёный' if not failed else 'КРАСНЫЙ'} ({checked} случаев, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
