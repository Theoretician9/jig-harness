#!/usr/bin/env python3
"""Пробы счётчика трения коммитов scripts/commit-friction.py — до кода.

Спека .tasks/hr.per-helper-code-copies/спека.md §1, §4.5, Р-6, Т5. Контракт:
  commit-friction.py --дней N [--json]; без --дней или N не целое > 0 — код 2.
  Источники за N дней (окно по `ts`): $LOG_DIR/gates.jsonl (gate == "pre-commit"),
  merge-queue.jsonl (прогон ворот очереди — строка с числом в data.ворота_с; итог — хвост
  события после «mq.»), guard.jsonl и pamyat-kommit.log (строка «ГГГГ-ММ-ДД ЧЧ:ММ:СС …» задаёт
  время следующим строкам с отступом) — следы замков `index.lock|cannot lock ref`;
  коммиты — `git -C <PROJECT_DIR> log --first-parent master --since` (PROJECT_DIR — install.conf).
  --json: {дней, прогонов_хука (= pre-commit минус ворота очереди), прогонов_ворот_очереди,
  коммитов, попыток_на_коммит (округл. 2; коммитов 0 — null), падения {гейт: n} (поле failed
  через запятую), package_sync, очередь {итог: n}, ожидание_с {p50, max}, ворота_с {p50},
  следы_замков}. Пусто везде — «нет данных», код 0.
Стенд: свой LOG_DIR и HARNESS_LOG_DIR, install.conf (HARNESS_INSTALL_CONF), git-проект с
датами коммитов. Механизм — рядом или HARNESS_COMMIT_FRICTION (дверь для мутаций копии).

Прогон: python3 scripts/test_commit_friction.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Пояс задан: строка pamyat-kommit.log без пояса — местное время, и проба судит это на любой
# машине (в UTC «местное» и «UTC» совпали бы, и подмена пояса осталась бы зелёной).
os.environ["TZ"] = "Etc/GMT-5"
time.tzset()

HERE = Path(__file__).resolve().parent
FRICTION = Path(os.environ.get("HARNESS_COMMIT_FRICTION") or HERE / "commit-friction.py")
NOW = dt.datetime.now().astimezone()
IN, OUT = NOW - dt.timedelta(days=1), NOW - dt.timedelta(days=5)
# За 2 ч до окна в 3 дня: прочитанное как UTC (+5 ч) попало бы внутрь.
EDGE = NOW - dt.timedelta(days=3, hours=2)

checked = failed = 0


def check(name: str, expected, actual) -> None:
    global checked, failed
    checked += 1
    if expected == actual:
        print(f"  ок    {name}")
    else:
        failed += 1
        print(f"  ПЛОХО {name}: ждали «{expected}», вышло «{actual}»")


def iso(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")


def jsonl(path: Path, rows: list) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


class Stand:
    def __init__(self, base: Path):
        self.logs, self.proj = base / "logs", base / "proj"
        self.logs.mkdir(parents=True)
        self.proj.mkdir()
        self.git("init", "-q", "-b", "master")
        self.install = base / "install.conf"
        self.install.write_text(f'PROJECT_DIR="{self.proj}"\n', encoding="utf-8")

    def git(self, *args: str, when: dt.datetime | None = None) -> None:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        if when:
            env.update(GIT_AUTHOR_DATE=iso(when), GIT_COMMITTER_DATE=iso(when))
        subprocess.run(["git", "-C", str(self.proj), *args], check=True, capture_output=True,
                       env=env)

    def commit(self, when: dt.datetime) -> None:
        self.git("commit", "-q", "--allow-empty", "-m", f"коммит {iso(when)}", when=when)

    def run(self, *args: str) -> tuple[int, str]:
        env = {**os.environ, "LOG_DIR": str(self.logs), "HARNESS_LOG_DIR": str(self.logs),
               "HARNESS_INSTALL_CONF": str(self.install), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("PROJECT_DIR", None)
        proc = subprocess.run([sys.executable, str(FRICTION), *args], capture_output=True,
                              text=True, env=env, cwd=self.logs, timeout=60)
        return proc.returncode, proc.stdout + proc.stderr


def gate(at, verdict="pass", failed_gates="", name="pre-commit") -> dict:
    return {"ts": iso(at), "gate": name, "head": "abc", "staged": 1, "verdict": verdict,
            "failed": failed_gates}


def mq(at, outcome_word, gate_s, wait_s) -> dict:
    return {"v": 1, "ts": iso(at), "src": "scripts/merge-queue.py", "event": f"mq.{outcome_word}",
            "outcome": "ok" if outcome_word == "слито" else "error",
            "data": {"ворота_с": gate_s, "ожидание_с": wait_s}}


def fill(s: Stand) -> None:
    """Окно 3 дня. Внутри: 8 pre-commit (4 красных, 1 жёлтый с полем failed), 1 чужой гейт;
    3 строки очереди (2 с воротами); 3 коммита master (+1 в чужой ветке); следы замков 2 + 1.
    Снаружи — по одной строке каждого вида и след замка у края окна."""
    jsonl(s.logs / "gates.jsonl", [
        gate(OUT, "fail", "package_sync"),
        gate(IN, "fail", "package_sync"), gate(IN, "fail", "package_sync"),
        gate(IN, "fail", "a,package_sync"), gate(IN, "fail", "b"),
        gate(IN), gate(IN), gate(IN), gate(IN, "yellow", "проба-жёлтая"),
        gate(IN, "fail", "x", name="nightly"),
    ])
    jsonl(s.logs / "merge-queue.jsonl", [
        mq(OUT, "слито", 100.0, 500),
        mq(IN, "слито", 4.0, 0), mq(IN, "слито", 6.0, 10), mq(IN, "конфликт", None, 2),
    ])
    jsonl(s.logs / "guard.jsonl", [
        {"ts": iso(OUT), "cmd_head": "fatal: Unable to create '/p/.git/index.lock'"},
        {"ts": iso(IN), "cmd_head": "fatal: Unable to create '/p/.git/index.lock'"},
        {"ts": iso(IN), "cmd_head": "error: cannot lock ref 'refs/heads/master'"},
        {"ts": iso(IN), "cmd_head": "ls -la"},
    ])
    (s.logs / "pamyat-kommit.log").write_text(
        f"{OUT:%Y-%m-%d %H:%M:%S}  коммит памяти\n"
        "    fatal: Unable to create '/p/.git/index.lock': File exists.\n"
        f"{IN:%Y-%m-%d %H:%M:%S}  коммит памяти\n"
        "    fatal: Unable to create '/p/.git/index.lock': File exists.\n"
        f"{IN:%Y-%m-%d %H:%M:%S}  правок памяти нет\n"
        f"{EDGE:%Y-%m-%d %H:%M:%S}  коммит памяти у края окна\n"
        "    fatal: Unable to create '/p/.git/index.lock': File exists.\n", encoding="utf-8")
    s.commit(NOW - dt.timedelta(days=10))
    for hours in (30, 20, 2):
        s.commit(NOW - dt.timedelta(hours=hours))
    s.git("checkout", "-q", "-b", "чужая")
    s.commit(NOW - dt.timedelta(hours=1))
    s.git("checkout", "-q", "master")


def case_numbers(tmp: Path) -> None:
    # Поломка, что оставила бы зелёной: нет — каждое число выводится из своего источника и
    # своего окна; забытое окно, вычитание ворот или чужой гейт меняют своё поле.
    print("синтетические журналы: точные числа")
    s = Stand(tmp / "числа")
    fill(s)
    code, out = s.run("--дней", "3", "--json")
    check("код 0", 0, code)
    try:
        got = json.loads(out)
    except ValueError:
        got = {}
    expected = {"дней": 3, "прогонов_хука": 6, "прогонов_ворот_очереди": 2, "коммитов": 3,
                "попыток_на_коммит": 2.0, "падения": {"package_sync": 3, "a": 1, "b": 1},
                "package_sync": 3, "очередь": {"слито": 2, "конфликт": 1},
                "ожидание_с": {"p50": 2, "max": 10}, "ворота_с": {"p50": 5}, "следы_замков": 3}
    for key, value in expected.items():
        check(f"--json {key}", value, got.get(key))
    code, out = s.run("--дней", "3")
    check("без --json: код 0 и package_sync в тексте", (0, True), (code, "package_sync" in out))


def case_empty(tmp: Path) -> None:
    # Поломка, что оставила бы зелёной: деление на ноль ловит код; «нет данных» — текст.
    print("пустые журналы: «нет данных», код 0")
    s = Stand(tmp / "пусто")
    code, out = s.run("--дней", "3")
    check("пусто: код 0", 0, code)
    check("пусто: «нет данных»", True, "нет данных" in out)
    code, out = s.run("--дней", "3", "--json")
    try:
        got = json.loads(out)
    except ValueError:
        got = {}
    check("пусто --json: код 0, коммитов 0, попыток_на_коммит null", (0, 0, None),
          (code, got.get("коммитов"), got.get("попыток_на_коммит", "нет поля")))
    # Поломка: «пусто» судит только хук и коммиты — одни следы замков сказали бы «нет данных».
    s = Stand(tmp / "только-замки")
    jsonl(s.logs / "guard.jsonl", [{"ts": iso(IN), "cmd_head": "cannot lock ref 'x'"}])
    check("только следы замков — не «нет данных»", False, "нет данных" in s.run("--дней", "3")[1])
    s = Stand(tmp / "только-очередь")
    jsonl(s.logs / "merge-queue.jsonl", [mq(IN, "конфликт", None, 1)])
    check("только итоги очереди — не «нет данных»", False, "нет данных" in s.run("--дней", "3")[1])


def case_bad_call(tmp: Path) -> None:
    # Поломка, что оставила бы зелёной: умолчание N — ловит «без --дней → 2».
    print("неверный вызов: код 2")
    s = Stand(tmp / "вызов")
    for args in ((), ("--дней",), ("--дней", "abc"), ("--дней", "0")):
        check(f"{' '.join(args) or 'без аргументов'} — код 2", 2, s.run(*args)[0])


CASES = (case_numbers, case_empty, case_bad_call)


def main() -> int:
    global checked, failed
    if not FRICTION.exists():
        print(f"нет {FRICTION}: случаи идут, механизма нет — красные")
    with tempfile.TemporaryDirectory() as raw:
        for case in CASES:
            try:
                case(Path(raw))
            except Exception as exc:  # noqa: BLE001 — падение случая = провал, не падение пробы
                checked += 1
                failed += 1
                print(f"  ПЛОХО {case.__name__}: исключение {type(exc).__name__}: {exc}")
    print(f"SELFTEST: {'зелёный' if not failed else 'КРАСНЫЙ'} ({checked} проверок, "
          f"{len(CASES)} случаев, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
