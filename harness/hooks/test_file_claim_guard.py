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
  * who-writes.py: живые заявки по журналу, `--count` — число занятых путей.

Механизм берётся рядом с пробой либо из HARNESS_FILE_CLAIM_GUARD (дверь для
мутаций: копия с поломкой); команда — HARNESS_WHO_WRITES.
Прогон: python3 scripts/hooks/test_file_claim_guard.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = Path(os.environ.get("HARNESS_FILE_CLAIM_GUARD") or HERE / "file_claim_guard.py")
WHO = Path(os.environ.get("HARNESS_WHO_WRITES") or HERE.parent / "who-writes.py")
JOURNAL = "заявки-на-файлы.jsonl"
SRC = "scripts/hooks/file_claim_guard.py"
CLAIM_EVENT = "file.claim"
SHARED_REL = "harness/config/stand-shared.yaml"

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

    def __init__(self, base: Path, window_min: str = "7", shared: str = SHARED_REL):
        self.base = base
        self.logs = base / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.pkg = base / "pkg"
        self.pkg.mkdir(parents=True, exist_ok=True)
        self.conf = base / "harness.conf"
        self.conf.write_text(f'FILE_CLAIM_MIN="{window_min}"\nFILE_CLAIM_SHARED="{shared}"\n',
                             encoding="utf-8")

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

    def plant_claim(self, agent: str, rel: str, minutes_ago: float) -> None:
        ts = (dt.datetime.now().astimezone() - dt.timedelta(minutes=minutes_ago)).isoformat(
            timespec="seconds")
        row = {"v": 1, "ts": ts, "src": SRC, "event": CLAIM_EVENT, "outcome": "ok",
               "task": "hr.other", "task_from": "arg", "session": "s-old",
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
    check("в заявке задача из HARNESS_TASK", "hr.stand", rows[0].get("task") if rows else None)
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
    check("предупреждение оставило след в журнале", 2, len(stand.rows("file.claim.warn")))

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
    print(f"SELFTEST: {'зелёный' if not failed else 'КРАСНЫЙ'} ({checked} случаев, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
