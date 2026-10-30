#!/usr/bin/env python3
"""Пробы ворот журналов G1–G4: scripts/check-event-writers.py и events.py lost --gate.

Зачем. Журнал харнеса стал одной функцией записи (Ф1), но функция ничего не
стоит, если завтрашний механизм снова допишет `>> x.jsonl` мимо неё, новый
журнал не попадёт в реестр, а потеря строки будет лежать в lost.jsonl, пока её
не прочтёт человек. Ворота держат это КОДОМ (спека
docs/specs/2026-10-27-логирование-харнеса.md §9, критерий 8):
  G1 — прямая дозапись в журнал мимо emit, демон без меток запуска, src без
       паспорта краснят; долг — данные, которые только сокращаются и находятся
       обходом в обе стороны;
  G2 — реестр ↔ код (коммит) и диск → реестр (прогон ворот): журнал без строки
       реестра, старая строка в переведённом журнале, режим переведённого;
  G3 — потеря без разбора и тишина журнала, которая без поломки невозможна,
       при подтверждённой работе сессии; здоровая тишина (пауза, лимит) — не
       красная;
  G4 — гейт секретов в журналах видит новые места: .events/ и archive/*.gz.
Каждый больной случай идёт ПЕРВЫМ в своей группе и судится по коду возврата
И по названию файла:строки / журнала из стенда: красный без названия не
помогает чинить, а «красный» от упавшего скрипта — не красный ворот.
Пустой набор нигде не зеленеет: сломанный регэксп даёт «судимых 0» / «долг
найден 0 из K» — и это красный (находки Прил. Б п. 13, В п. 9).

Стенды — временные каталоги: дерево кода с git (для сверки долга с HEAD),
каталог журналов, состояние. Механизм зовётся с дверями --root,
HARNESS_JOURNALS, HARNESS_LOG_DIR, HARNESS_EVENTS_STATE, PANEL_STATE_DIR,
EVENTS_GROUP; путь к механизму — двери HARNESS_EVENT_WRITERS и
HARNESS_EVENTS_CLI (мутация копии судит копию). Боевых журналов проба не
читает; чтение боевого — только КОПИИ: реестр harness/config/journals.yaml
(помесячное семейство tokens-*: правка обязана жить в живых данных) и код
живых писателей в стенд-дерево (heredoc-писатели tokens-collector.sh и
memory-revision.sh; четыре файла долга без мест журнала) — форма живого кода,
а не пересказ её. Утечку двери видно по числам: на стенде 3 файла на диске,
на боевом — 38; в G1 стенда «прямых записей 1».

Случаи ревью Ф2 (.tasks/hr.harness-detailed-logging/review-f2.md) идут
первыми группами: долг — пары (файл, журнал) (находка 2), python в
bash-heredoc (1), писатель реестра по цели записи (3), python-правило по цели
(4), ловушка и ветки демонов (5), нечитаемый источник G3 (6); плюс усиление по
зелёным мутациям Ф2-16, 19, 20, 21, 43.

    python3 scripts/test_events_gates.py
Код возврата: 0 — все пути прошли; 1 — есть упавший.
"""
from __future__ import annotations

import datetime as dt
import grp
import importlib.util
import gzip
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
GATE = os.environ.get("HARNESS_EVENT_WRITERS") or str(ROOT / "scripts" / "check-event-writers.py")
CLI = os.environ.get("HARNESS_EVENTS_CLI") or str(ROOT / "scripts" / "events.py")
SECRETS_GATE = ROOT / "scripts" / "check-secret-in-logs.py"
LIVE_REGISTRY = ROOT / "harness" / "config" / "journals.yaml"
CARD = "hr.probe-debt"
TIMEOUT = 120

paths = failures = not_judged = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global paths, failures
    paths += 1
    if ok:
        print(f"ок  · {name}")
    else:
        failures += 1
        print(f"ПЛОХО · {name}: {detail[:600]}")


def skip(name: str, why: str) -> None:
    """Случай не судился — это НЕ зелёный, число печатается в итоге."""
    global not_judged
    not_judged += 1
    print(f"НЕ СУДИТСЯ · {name}: {why}")


def red_naming(name: str, res: tuple[int, str], *needles: str) -> None:
    """Красный ворот = rc 1 И названо то, что сломано. rc 2 (неверный вызов,
    нет механизма) — не красный ворот, а отказ."""
    rc, out = res
    missing = [n for n in needles if n not in out]
    check(name, rc == 1 and not missing, f"rc={rc}, не названо {missing}; вывод: {out}")


def green(name: str, res: tuple[int, str], *needles: str) -> None:
    rc, out = res
    missing = [n for n in needles if not re.search(n, out)]
    check(name, rc == 0 and not missing, f"rc={rc}, нет {missing}; вывод: {out}")


def events_group() -> tuple[str, int]:
    try:
        g = grp.getgrnam("harness-panel")
        if g.gr_gid in os.getgroups():
            return g.gr_name, g.gr_gid
    except KeyError:
        pass
    return grp.getgrgid(os.getgid()).gr_name, os.getgid()


GROUP_NAME, GROUP_GID = events_group()


def clean_env(**doors: str) -> dict:
    """Окружение без боевых дверей: забытая дверь не должна молча вести в
    боевой каталог."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("HARNESS_") and k not in ("LOG_DIR", "PANEL_STATE_DIR", "EVENTS_GROUP")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["EVENTS_GROUP"] = GROUP_NAME
    env.update(doors)
    return env


def run(cmd: list[str], env: dict) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=TIMEOUT,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "таймаут"
    return p.returncode, p.stdout + p.stderr


def write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(path, mode)


def dump_yaml(path: Path, data) -> None:
    write(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


def line_of(text: str, marker: str) -> int:
    for i, line in enumerate(text.split("\n"), 1):
        if marker in line:
            return i
    raise AssertionError(f"маркер {marker!r} не найден в тексте стенда")


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=probe",
                    "-c", "user.email=probe@localhost", "-c", "init.defaultBranch=master",
                    "-C", str(root), *args], check=True, capture_output=True, stdin=subprocess.DEVNULL)


# ───────────────────────── стенд дерева кода (G1, G2 --registry) ─────────────────────────

DAEMON_SELFTEST = """#!/bin/bash
set -euo pipefail
source "$(dirname "$0")/../../scripts/lib/config.sh"
if [ "${1:-}" = "--selftest" ]; then
    T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
    echo selftest
    exit 0
fi
"""

# Ловушка рабочего пути по образцу: rc=$? первой командой, harness_run_end "$rc".
TRAP_OK = "trap 'rc=$?; harness_run_end \"$rc\"' EXIT\n"
SH_HEAD = "#!/bin/bash\nset -euo pipefail\n"


def debt(file: str, *journals: str, card: str | None = CARD) -> dict:
    """Запись долга G1. Единица долга — пара (файл, журнал реестра): файл из
    долга, начавший писать НОВЫЙ журнал, — новый писатель, а не старый долг."""
    entry = {"file": file}
    if card is not None:
        entry["card"] = card
    entry["journals"] = list(journals)
    return entry


BASE_FILES = {
    # Дверь: прямая дозапись в самой функции записи — законна и не судится.
    "scripts/lib/events.py": "import os\nfd = os.open('x.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT)\n",
    "scripts/good.py": ("import events\n"
                        "events.emit(\"scripts/good.py\", \"probe.ok\", \"ok\", journal=\"gates\")\n"),
    "scripts/legacy.sh": ("#!/bin/bash\nL='{}'\n"
                          "printf '%s\\n' \"$L\" >> \"$LOG_DIR/gates.jsonl\"\n"),
    "scripts/plain.sh": "#!/bin/bash\necho \"нет записи\" >> \"$LOG_DIR/plain.log\"\n",
    # Проба пишет стенд-журналы законно — тесты не судятся.
    "scripts/test_writer.py": "with open('t.jsonl', 'a') as fh:\n    fh.write('{}')\n",
    "harness/demons/d1.sh": DAEMON_SELFTEST + "harness_run d1\n" + TRAP_OK + "echo work\n",
    "harness/demons/d2.sh": (DAEMON_SELFTEST + "harness_run d2\nT=$(mktemp -d)\n"
                             "trap 'rc=$?; rm -rf \"$T\"; harness_run_end \"$rc\"' EXIT\necho work\n"),
    "harness/demons/old.sh": DAEMON_SELFTEST + "echo work\n",
    "scripts/pydaemon.py": "import events\nwith events.run(\"pydaemon\"):\n    pass\n",
}
BASE_CODE_FILES = 7          # всё, кроме двери events.py и test_writer.py
BASE_JOBS = [
    {"имя": "d1", "расписание": "*/5 * * * *", "команда": "harness/demons/d1.sh", "журнал": "d1.log"},
    {"имя": "d1-press", "расписание": "40 * * * *", "команда": "harness/demons/d1.sh --по-давлению", "журнал": "d1.log"},
    {"имя": "d2", "расписание": "0 4 * * *", "команда": "harness/demons/d2.sh", "журнал": "d2.log"},
    {"имя": "old", "расписание": "0 5 * * *", "команда": "harness/demons/old.sh", "журнал": "old.log"},
    {"имя": "pydaemon", "расписание": "0 6 * * *", "команда": "scripts/pydaemon.py", "журнал": "py.log"},
]


def base_registry() -> dict:
    return {
        "ignore_dirs": ["demon-stream", "проба-хука", ".events"],
        "name_is_key_dirs": ["PANEL_STATE_DIR"],
        "digest_keys": ["sha256", "head", "commit", "ino_from"],
        "service_flags": ["LATE_WATCH"],
        "legacy_writers": [debt("scripts/legacy.sh", "gates")],
        "legacy_daemons": [{"file": "harness/demons/old.sh", "card": CARD}],
        "journals": {
            "gates": {"file": "gates.jsonl", "writers": ["scripts/legacy.sh", "scripts/good.py"],
                      "time_field": "ts", "time_kind": "tz"},
            "sample": {"file": "s.jsonl", "kind": "sample", "writers": "none",
                       "why": "образец потока, пишет человек", "time_kind": "none"},
        },
    }


def base_mechanisms() -> dict:
    return {"механизмы": [{"файл": f} for f in
                          ("scripts/good.py", "scripts/legacy.sh", "harness/demons/d1.sh",
                           "harness/demons/d2.sh", "harness/demons/old.sh", "scripts/pydaemon.py")]}


class Tree:
    """Дерево кода стенда. head — реестр, закоммиченный в HEAD (None — без
    коммита вовсе); registry — рабочий реестр."""

    def __init__(self, tmp: Path, files=None, registry=None, head="same", jobs=None,
                 mechanisms=None, drop=()):
        self.root = Path(tempfile.mkdtemp(prefix="tree-", dir=tmp))
        self.files = dict(BASE_FILES if files is None else files)
        for f in drop:
            self.files.pop(f, None)
        reg = base_registry() if registry is None else registry
        for rel, text in self.files.items():
            write(self.root / rel, text, 0o755 if rel.endswith(".sh") else None)
        dump_yaml(self.root / "harness/config/system-jobs.yaml", {"задания": BASE_JOBS if jobs is None else jobs})
        dump_yaml(self.root / "harness/config/mechanisms.yaml", base_mechanisms() if mechanisms is None else mechanisms)
        git(self.root, "init", "-q")
        if head is not None:
            dump_yaml(self.root / "harness/config/journals.yaml", reg if head == "same" else head)
            git(self.root, "add", "-A")
            git(self.root, "commit", "-qm", "стенд")
        dump_yaml(self.root / "harness/config/journals.yaml", reg)
        self.logs = Path(tempfile.mkdtemp(prefix="logs-", dir=tmp))

    def add(self, rel: str, text: str) -> int:
        """Кладёт файл в рабочее дерево (не в HEAD); возвращает номер строки
        маркера «# ЗДЕСЬ», если он есть."""
        write(self.root / rel, text, 0o755 if rel.endswith(".sh") else None)
        return line_of(text, "ЗДЕСЬ") if "ЗДЕСЬ" in text else 0

    def gate(self, *flags: str, **doors: str) -> tuple[int, str]:
        env = clean_env(HARNESS_LOG_DIR=str(self.logs), **doors)
        return run([sys.executable, GATE, "--root", str(self.root), *flags], env)


def with_registry(**changes) -> dict:
    reg = base_registry()
    reg.update(changes)
    return reg


# ───────────────────────── G1/G2: долг — пары (файл, журнал) ─────────────────────────
# Ревью Ф2 находка 2: единица долга «файл» пропускала новый журнал в любом из
# 41 файла долга мимо обоих коммитных гейтов — а это самые горячие файлы.

LEGACY = BASE_FILES["scripts/legacy.sh"]
NEW_JOURNAL_LINE = "printf '%s\\n' \"$L\" >> \"$LOG_DIR/novyj.jsonl\"  # ЗДЕСЬ\n"
OTHER_LINE = "printf '%s\\n' \"$L\" >> \"$LOG_DIR/other.jsonl\"  # ЗДЕСЬ\n"


def registry_with_other(*legacy_journals: str) -> dict:
    """Реестр с вторым журналом other (писатель legacy.sh); долг legacy.sh — по парам."""
    reg = with_registry(legacy_writers=[debt("scripts/legacy.sh", *legacy_journals)])
    reg["journals"]["other"] = {"file": "other.jsonl", "writers": ["scripts/legacy.sh"],
                                "time_field": "ts", "time_kind": "tz"}
    return reg


def case_g1_debt_pairs(tmp: Path) -> None:
    print("— G1/G2: файл из долга пишет НОВЫЙ журнал (долг — пары файл·журнал) —")

    t = Tree(tmp)
    n = t.add("scripts/legacy.sh", LEGACY + NEW_JOURNAL_LINE)
    red_naming("G1·файл из долга начал писать новый журнал novyj.jsonl → красный с файлом:строкой",
               t.gate(), f"scripts/legacy.sh:{n}")
    red_naming("G2·файл из долга пишет журнал, которого нет в реестре → красный --registry",
               t.gate("--registry"), "novyj.jsonl", f"scripts/legacy.sh:{n}")

    reg = registry_with_other("gates")
    t = Tree(tmp, registry=reg)
    n = t.add("scripts/legacy.sh", LEGACY + OTHER_LINE)
    red_naming("G1·файл из долга пишет журнал реестра other, пары (legacy.sh, other) в долге нет → красный",
               t.gate(), f"scripts/legacy.sh:{n}")
    green("G2·тот же случай в --registry: журнал other в реестре, писатель его называет → зелёный",
          t.gate("--registry"))

    t = Tree(tmp, registry=registry_with_other("gates", "other"), head=registry_with_other("gates"))
    t.add("scripts/legacy.sh", LEGACY + OTHER_LINE)
    red_naming("G1·долг вырос ПАРОЙ против HEAD (legacy.sh + other) при том же наборе файлов → красный",
               t.gate(), "scripts/legacy.sh", "other")

    t = Tree(tmp, registry=registry_with_other("gates", "other"))
    red_naming("G1·пара долга (legacy.sh, other) без записи в other в коде → красный «долг найден»",
               t.gate(), "scripts/legacy.sh", "other", "долг найден")

    # Здоровые.
    t = Tree(tmp, registry=registry_with_other("gates", "other"))
    t.add("scripts/legacy.sh", LEGACY + OTHER_LINE)
    green("G1·обе пары долга найдены в коде → зелёный", t.gate(), r"долг найден (\d+) из \1\b")

    t = Tree(tmp, registry=registry_with_other("gates"), head=registry_with_other("gates", "other"))
    green("G1·долг сократился парой (other переведён, пара вычеркнута) → зелёный", t.gate(),
          r"долг найден (\d+) из \1\b")


# ───────────────────────── G1: python внутри bash-heredoc ─────────────────────────
# Ревью Ф2 находка 1: tokens-collector.sh:572 и memory-revision.sh:262 пишут
# журналы питоном из heredoc, а .sh судился только регэкспами >> и tee.

HEREDOC_TOKENS = ("#!/bin/bash\nset -euo pipefail\nLOG_DIR=${LOG_DIR:-/var/log/harness}\n"
                  "python3 - \"$LOG_DIR\" <<'PY'\n"
                  "import json, os, sys, datetime\n"
                  "log_dir = sys.argv[1]\n"
                  "month = datetime.date.today().strftime('%Y-%m')\n"
                  "events_path  = os.path.join(log_dir, f\"tok-{month}.jsonl\")\n"
                  "if True:\n"
                  "    with open(events_path, \"a\", encoding=\"utf-8\") as fh:  # ЗДЕСЬ\n"
                  "        fh.write(json.dumps({}) + \"\\n\")\n"
                  "PY\n")

HEREDOC_OFFERS = ("#!/bin/bash\nset -euo pipefail\n"
                  "write_offers() {\n"
                  "    OFFERS=\"$LOG_DIR/offers.jsonl\"\n"
                  "    printf '%s\\n' \"$1\" | while IFS= read -r line; do\n"
                  "        python3 - \"$OFFERS\" \"$line\" <<'PYEOF' || true\n"
                  "import json, sys\n"
                  "path, text = sys.argv[1], sys.argv[2]\n"
                  "with open(path, \"a\", encoding=\"utf-8\") as fh:  # ЗДЕСЬ\n"
                  "    fh.write(json.dumps({\"text\": text}) + \"\\n\")\n"
                  "PYEOF\n"
                  "    done\n"
                  "}\n"
                  "write_offers \"• x\"\n")

LIVE_HEREDOC_WRITERS = {
    "harness/demons/tokens-collector.sh": 'open(events_path, "a"',
    "harness/demons/memory-revision.sh": 'open(путь, "a"',
}


def case_g1_heredoc(tmp: Path) -> None:
    print("— G1: python внутри bash-heredoc —")

    t = Tree(tmp)
    n = t.add("scripts/hd-tokens.sh", HEREDOC_TOKENS)
    red_naming("G1·python3 - <<'PY' в .sh: open(events_path, \"a\"), путь tok-{month}.jsonl → красный",
               t.gate(), f"scripts/hd-tokens.sh:{n}")

    t = Tree(tmp)
    n = t.add("harness/demons/hd-offers.sh", HEREDOC_OFFERS)
    red_naming("G1·heredoc в функции: путь .jsonl пришёл в sys.argv из bash-переменной → красный",
               t.gate(), f"harness/demons/hd-offers.sh:{n}")

    # Живые образцы (копия кода, не журналы): форма, а не пересказ формы.
    copied = 0
    t = Tree(tmp)
    expect = []
    for rel, marker in LIVE_HEREDOC_WRITERS.items():
        live = ROOT / rel
        text = live.read_text(encoding="utf-8") if live.is_file() else ""
        if marker not in text:
            skip(f"G1·живой писатель {rel}", "образца нет — файл переведён или переписан")
            continue
        t.add(rel, text)
        expect.append(f"{rel}:{line_of(text, marker)}")
        copied += 1
    if copied:
        red_naming(f"G1·живые писатели heredoc (копия, {copied} файла) вне долга → красный с файлом:строкой",
                   t.gate(), *expect)

    # Здоровые.
    t = Tree(tmp)
    t.add("scripts/hd-cat.sh", ("#!/bin/bash\ncat <<'EOF'\n"
                                "with open(\"/var/log/harness/x.jsonl\", \"a\") as fh:\nEOF\n"))
    green("G1·тот же текст в heredoc для cat (не python) → зелёный", t.gate(), r"прямых записей 1,")

    t = Tree(tmp)
    t.add("scripts/hd-lock.sh", ("#!/bin/bash\npython3 - \"$LOG_DIR\" <<'PY'\n"
                                 "import fcntl, os, sys\n"
                                 "J = os.path.join(sys.argv[1], \"x.jsonl\")\n"
                                 "with open(os.path.join(sys.argv[1], \"x.lock\"), \"a\") as lock:\n"
                                 "    fcntl.flock(lock, fcntl.LOCK_EX)\nPY\n"))
    green("G1·python heredoc держит замок .lock (журнал только назван) → зелёный", t.gate(),
          r"прямых записей 1,")


# ───────────────────────── G1: python-правило судит цель записи ─────────────────────────
# Ревью Ф2 находка 4: файл с «.jsonl» где угодно делал красным ЛЮБОЙ open(…,"a")
# в нём — 7 ложных из 10 на выборке; 4 файла долга попали туда только так.

PY_HEAD = ("import fcntl, os, tempfile\nfrom pathlib import Path\n"
           "LOG_DIR = Path(os.environ.get('LOG_DIR', '/var/log/harness'))\n"
           "INBOX = LOG_DIR / \"inbox.jsonl\"   # журнал только назван\n")

PY_NOT_JOURNAL = {
    "замок согласия (deploy_guard.py:749)":
        "with open(LOG_DIR / \"согласие.lock\", \"a\") as lock:\n    fcntl.flock(lock, fcntl.LOCK_EX)\n",
    "замок по параметру функции (lib/cards.py:127)":
        "def map_lock(path):\n    with open(path, \"a\", encoding=\"utf-8\") as handle:\n"
        "        fcntl.flock(handle, fcntl.LOCK_EX)\n",
    "замок рядом с журналом (alert-task.py:132)":
        "LOCK = os.path.join(str(LOG_DIR), \".journal.lock\")\nwith open(LOCK, \"a\") as guard:\n"
        "    fcntl.flock(guard, fcntl.LOCK_SH)\n",
    "журнал .log (live-check-daemon.py:268)":
        "trace = LOG_DIR / \"live-check.log\"\nwith trace.open(\"a\", encoding=\"utf-8\") as fh:\n"
        "    fh.write('x\\n')\n",
    "журнал .log литералом (alert-task.py:410)":
        "with open(os.path.join(str(LOG_DIR), \"tg_failures.log\"), \"a\") as fh:\n    fh.write('x')\n",
    "стенд самотеста в mkdtemp (stall-in-log.py:452)":
        "def selftest():\n    T = tempfile.mkdtemp()\n    J = os.path.join(T, \"probe.jsonl\")\n"
        "    with open(J, \"a\", encoding=\"utf-8\") as fh:\n        fh.write('{}')\n",
}

LIVE_OUT_OF_DEBT = ("scripts/deploy_guard.py", "scripts/lib/cards.py",
                    "scripts/live-check-daemon.py", "scripts/live-check-inbox.py")


def case_g1_py_target(tmp: Path) -> None:
    print("— G1: python-правило судит цель записи, а не файл —")

    for what, body in PY_NOT_JOURNAL.items():
        t = Tree(tmp)
        t.add("scripts/notjournal.py", PY_HEAD + body)
        green(f"G1·{what} в файле, где .jsonl назван, → не прямая запись, зелёный", t.gate(),
              r"прямых записей 1,")

    t = Tree(tmp)
    t.add("scripts/stand.sh", ("#!/bin/bash\nif [ \"${1:-}\" = \"--selftest\" ]; then\n"
                               "    T=$(mktemp -d)\n    echo '{}' >> \"$T/hb.jsonl\"\n    exit 0\nfi\n"))
    green("G1·bash-стенд самотеста >> \"$T/…jsonl\" при T=$(mktemp -d) (heartbeat-watch.sh:140) → зелёный",
          t.gate(), r"прямых записей 1,")

    # Живые файлы долга, где мест журнала нет (копия кода): обязаны выйти из долга.
    t = Tree(tmp)
    copied = []
    for rel in LIVE_OUT_OF_DEBT:
        live = ROOT / rel
        if live.is_file():
            t.add(rel, live.read_text(encoding="utf-8"))
            copied.append(rel)
        else:
            skip(f"G1·живой {rel}", "файла нет")
    if copied:
        rc, out = t.gate()
        named = [rel for rel in copied if f"{rel}:" in out]
        check(f"G1·живые {len(copied)} файла долга без мест журнала (копия) вне долга → не названы, "
              f"прямых записей 1", rc == 0 and not named and "прямых записей 1," in out,
              f"rc={rc}, названы {named}; вывод: {out}")

    # Больной рядом: настоящая запись в журнал по имени-переменной остаётся красной.
    t = Tree(tmp)
    n = t.add("scripts/varjournal.py", PY_HEAD + "with open(INBOX, \"a\") as fh:  # ЗДЕСЬ\n    fh.write('{}')\n")
    red_naming("G1·open(INBOX, \"a\"), где INBOX = LOG_DIR / \"inbox.jsonl\" → красный (цель — журнал)",
               t.gate(), f"scripts/varjournal.py:{n}")


# ───────────────────────── G1: писатели ─────────────────────────

def case_g1_writers(tmp: Path) -> None:
    print("— G1: прямая дозапись мимо emit —")

    t = Tree(tmp)
    text = "#!/bin/bash\nX=1\necho \"$X\" >> \"$LOG_DIR/x.jsonl\"  # ЗДЕСЬ\n"
    n = t.add("scripts/new-writer.sh", text)
    red_naming("G1·bash >> в .jsonl вне долга → красный с файлом:строкой", t.gate(),
               f"scripts/new-writer.sh:{n}")

    t = Tree(tmp)
    text = ("import os\nd = os.environ['LOG_DIR']\n"
            "with open(os.path.join(d, \"x.jsonl\"), \"a\", encoding=\"utf-8\") as fh:  # ЗДЕСЬ\n"
            "    fh.write('{}\\n')\n")
    n = t.add("scripts/new_writer.py", text)
    red_naming("G1·python open(…, \"a\") вне долга → красный с файлом:строкой", t.gate(),
               f"scripts/new_writer.py:{n}")

    t = Tree(tmp)
    text = ("from pathlib import Path\nJ = Path('/tmp') / \"y.jsonl\"\n"
            "with J.open(\"a\") as fh:  # ЗДЕСЬ\n    fh.write('{}')\n")
    n = t.add("harness/panel/pathwriter.py", text)
    red_naming("G1·pathlib .open(\"a\") в harness/ → красный", t.gate(), f"harness/panel/pathwriter.py:{n}")

    t = Tree(tmp)
    text = "#!/bin/bash\nJ=\"$LOG_DIR/y.jsonl\"\nprintf '%s\\n' '{}' >> \"$J\"  # ЗДЕСЬ\n"
    n = t.add("scripts/varwriter.sh", text)
    red_naming("G1·bash >> \"$J\", где J присвоен путь .jsonl → красный", t.gate(), f"scripts/varwriter.sh:{n}")

    t = Tree(tmp)
    text = "#!/bin/bash\necho '{}' | tee -a \"$LOG_DIR/z.jsonl\" >/dev/null  # ЗДЕСЬ\n"
    n = t.add("scripts/teewriter.sh", text)
    red_naming("G1·tee -a в .jsonl → красный", t.gate(), f"scripts/teewriter.sh:{n}")

    t = Tree(tmp)
    text = ("import os\nP = '/tmp/w.jsonl'\n"
            "fd = os.open(P, os.O_WRONLY | os.O_APPEND | os.O_CREAT)  # ЗДЕСЬ\n")
    n = t.add("scripts/rawwriter.py", text)
    red_naming("G1·os.open(O_APPEND) в файле с .jsonl → красный", t.gate(), f"scripts/rawwriter.py:{n}")

    t = Tree(tmp)
    text = "#!/bin/sh\necho '{}' >> /var/log/harness/q.jsonl  # ЗДЕСЬ\n"
    n = t.add("scripts/noext", text)
    red_naming("G1·файл без расширения с шебангом sh → судится", t.gate(), f"scripts/noext:{n}")

    t = Tree(tmp, registry=with_registry(legacy_writers=[
        debt("scripts/legacy.sh", "gates"), debt("scripts/plain.sh", "gates")]))
    red_naming("G1·запись долга без прямой записи в коде → красный «долг найден 1 из 2»", t.gate(),
               "scripts/plain.sh", "долг найден 1 из 2")

    head = base_registry()
    work = with_registry(legacy_writers=[debt("scripts/legacy.sh", "gates"), debt("scripts/grown.sh", "gates")])
    t = Tree(tmp, registry=work, head=head)
    t.add("scripts/grown.sh", "#!/bin/bash\necho x >> \"$LOG_DIR/gates.jsonl\"\n")
    red_naming("G1·долг вырос против HEAD → красный с именем добавленного", t.gate(), "scripts/grown.sh")

    t = Tree(tmp, registry=with_registry(legacy_writers=[debt("scripts/legacy.sh", "gates", card=None)]))
    red_naming("G1·запись долга без card → красный", t.gate(), "scripts/legacy.sh")

    t = Tree(tmp, files={}, registry=with_registry(legacy_writers=[], legacy_daemons=[]), jobs=[])
    red_naming("G1·пустое дерево кода → красный «судимых файлов 0»", t.gate(), "судимых файлов 0")

    t = Tree(tmp)
    text = ("import events\n"
            "events.emit(\"scripts/ghost.py\", \"probe.x\", \"ok\", journal=\"gates\")  # ЗДЕСЬ\n")
    n = t.add("scripts/emitter.py", text)
    red_naming("G1·src-литерал emit без паспорта в mechanisms.yaml → красный", t.gate(),
               "scripts/ghost.py", f"scripts/emitter.py:{n}")

    t = Tree(tmp)
    text = ("#!/bin/bash\nsource scripts/lib/events.sh\n"
            "harness_event gates scripts/ghost.sh probe.x ok  # ЗДЕСЬ\n")
    n = t.add("scripts/bashemitter.sh", text)
    red_naming("G1·src-литерал harness_event без паспорта → красный", t.gate(),
               "scripts/ghost.sh", f"scripts/bashemitter.sh:{n}")

    # Здоровые — после больных.
    t = Tree(tmp)
    green("G1·здоровое дерево → зелёный, числа стенда точно", t.gate(),
          rf"судимых файлов {BASE_CODE_FILES}, прямых записей 1, в долге 1, долг найден 1 из 1",
          r"демонов 4\b", r"литералов src 1, без паспорта 0")

    t = Tree(tmp)
    t.add("scripts/logwriter.sh", "#!/bin/bash\necho x >> \"$LOG_DIR/demon.log\"\n")
    green("G1·>> в .log (не журнал) → зелёный", t.gate(), rf"судимых файлов {BASE_CODE_FILES + 1}\b")

    head = with_registry(legacy_writers=[debt("scripts/legacy.sh", "gates"), debt("scripts/fixed.sh", "gates")])
    t = Tree(tmp, head=head)
    t.add("scripts/fixed.sh", "#!/bin/bash\nsource scripts/lib/events.sh\n")
    green("G1·долг сократился (писатель переведён и вычеркнут) → зелёный", t.gate(), r"в долге 1\b")

    t = Tree(tmp, head=None)
    green("G1·нет HEAD → «не судится: базы нет», не красный", t.gate(), r"не судится: базы нет")


# ───────────────────────── G1: демоны ─────────────────────────

def case_g1_daemons(tmp: Path) -> None:
    print("— G1: демоны, harness_run и ловушка рабочего пути —")

    files = dict(BASE_FILES)
    files["harness/demons/d2.sh"] = (DAEMON_SELFTEST + "harness_run d2\nT=$(mktemp -d)\n"
                                     "trap 'rc=$?; rm -rf \"$T\"' EXIT\necho work\n")
    red_naming("G1·ловушка рабочего пути без harness_run_end → красный", Tree(tmp, files=files).gate(),
               "harness/demons/d2.sh")

    files = dict(BASE_FILES)
    files["harness/demons/d2.sh"] = (DAEMON_SELFTEST + "harness_run d2\nT=$(mktemp -d)\n"
                                     "trap 'rm -rf \"$T\"; harness_run_end \"$?\"' EXIT\necho work\n")
    red_naming("G1·ловушка без rc=$? первой командой → красный", Tree(tmp, files=files).gate(),
               "harness/demons/d2.sh")

    files = dict(BASE_FILES)
    files["harness/demons/d2.sh"] = (DAEMON_SELFTEST + "harness_run d2\nT=$(mktemp -d)\n"
                                     "trap 'rm -rf \"$T\"; rc=$?; harness_run_end \"$rc\"' EXIT\necho work\n")
    red_naming("G1·ловушка с rc=$?; harness_run_end \"$rc\", но rc=$? НЕ первой командой → красный (Ф2-16)",
               Tree(tmp, files=files).gate(), "harness/demons/d2.sh", "rc=$?")

    files = dict(BASE_FILES)
    files["harness/demons/d1.sh"] = (DAEMON_SELFTEST.replace(
        "if [ \"${1:-}\" = \"--selftest\" ]", "harness_run d1\nif [ \"${1:-}\" = \"--selftest\" ]")
        + TRAP_OK + "echo work\n")
    red_naming("G1·harness_run выше ветки --selftest → красный", Tree(tmp, files=files).gate(),
               "harness/demons/d1.sh", "выше служебной ветки")

    files = dict(BASE_FILES)
    files["harness/demons/d1.sh"] = (DAEMON_SELFTEST + "harness_run d1\n" + TRAP_OK + "LATE_WATCH=0\n"
                                     "if [ \"$LATE_WATCH\" = 1 ]; then\n    echo досмотр\n    exit 0\nfi\n"
                                     "echo work\n")
    red_naming("G1·harness_run выше ветки флага LATE_WATCH (service_flags) → красный",
               Tree(tmp, files=files).gate(), "harness/demons/d1.sh", "выше служебной ветки")

    # Ревью Ф2 находка 5: правило зеленело без ловушки, не судило ловушку-функцию
    # и не видело веток, какие стоят у живых демонов.
    files = dict(BASE_FILES)
    files["harness/demons/d1.sh"] = DAEMON_SELFTEST + "harness_run d1\necho work\n"
    red_naming("G1·harness_run есть, ловушки EXIT на рабочем пути нет → красный",
               Tree(tmp, files=files).gate(), "harness/demons/d1.sh", "EXIT")

    files = dict(BASE_FILES)
    files["harness/demons/d1.sh"] = (DAEMON_SELFTEST + "on_exit() {\n    rm -f \"$T\"\n}\n"
                                     "harness_run d1\nT=$(mktemp)\ntrap on_exit EXIT\necho work\n")
    red_naming("G1·trap on_exit EXIT, тело функции без rc=$? и harness_run_end → красный",
               Tree(tmp, files=files).gate(), "harness/demons/d1.sh")

    case_tail = ("case \"${1:-}\" in\n    --dry-run)  DRY_RUN=1 ;;\n"
                 "    --selftest) echo selftest; exit 0 ;;\n    \"\")         : ;;\nesac\n")
    files = dict(BASE_FILES)
    files["harness/demons/d1.sh"] = SH_HEAD + "DRY_RUN=0\nharness_run d1\n" + TRAP_OK + case_tail + "echo work\n"
    red_naming("G1·harness_run выше ветки case \"${1:-}\" (disk-cleanup.sh:103) → красный",
               Tree(tmp, files=files).gate(), "harness/demons/d1.sh", "выше служебной ветки")

    files = dict(BASE_FILES)
    files["harness/demons/d1.sh"] = (SH_HEAD + "LATE=0\nharness_run d1\n" + TRAP_OK
                                     + "[ \"${1:-}\" = \"--досмотр\" ] && LATE=1\necho work \"$LATE\"\n")
    red_naming("G1·harness_run выше ветки [ \"${1:-}\" = … ] && (session-warden.sh:2313) → красный",
               Tree(tmp, files=files).gate(), "harness/demons/d1.sh", "выше служебной ветки")

    t = Tree(tmp, jobs=BASE_JOBS + [{"имя": "fresh", "расписание": "0 7 * * *",
                                     "команда": "harness/demons/fresh.sh", "журнал": "f.log"}])
    t.add("harness/demons/fresh.sh", DAEMON_SELFTEST + "echo work\n")
    red_naming("G1·новый демон без harness_run вне долга → красный", t.gate(), "harness/demons/fresh.sh")

    files = dict(BASE_FILES)
    files["scripts/pydaemon.py"] = "import events\nprint('работа без меток')\n"
    red_naming("G1·python-демон без events.run вне долга → красный", Tree(tmp, files=files).gate(),
               "scripts/pydaemon.py")

    reg = with_registry(legacy_daemons=[{"file": "harness/demons/old.sh", "card": CARD},
                                        {"file": "harness/demons/d1.sh", "card": CARD}])
    red_naming("G1·долг демонов: демон уже с метками, а в долге → красный «вычеркни»",
               Tree(tmp, registry=reg).gate(), "harness/demons/d1.sh", "долг найден 1 из 2")

    head = base_registry()
    work = with_registry(legacy_daemons=[{"file": "harness/demons/old.sh", "card": CARD},
                                         {"file": "harness/demons/old2.sh", "card": CARD}])
    t = Tree(tmp, registry=work, head=head,
             jobs=BASE_JOBS + [{"имя": "old2", "расписание": "0 8 * * *",
                                "команда": "harness/demons/old2.sh", "журнал": "o.log"}])
    t.add("harness/demons/old2.sh", DAEMON_SELFTEST + "echo work\n")
    red_naming("G1·долг демонов вырос против HEAD → красный", t.gate(), "harness/demons/old2.sh")

    # Здоровые.
    green("G1·ловушка внутри ветки --selftest без harness_run_end → не судится, зелёный",
          Tree(tmp).gate(), r"демонов 4\b")

    files = dict(BASE_FILES)
    files["harness/demons/d1.sh"] = (SH_HEAD + "if [ \"${1:-}\" = \"--selftest\" ]; then\n"
                                     "T=$(mktemp -d)\ntrap 'rm -rf \"$T\"' EXIT\necho selftest\nexit 0\nfi\n"
                                     "harness_run d1\n" + TRAP_OK + "echo work\n")
    green("G1·ловушка БЕЗ отступа внутри ветки --selftest без harness_run_end → не судится, зелёный (Ф2-19)",
          Tree(tmp, files=files).gate(), r"демонов 4\b")

    files = dict(BASE_FILES)
    files["harness/demons/d1.sh"] = (DAEMON_SELFTEST + "harness_run d1\n" + TRAP_OK + "cat <<'EOF'\n"
                                     "if [ \"${1:-}\" = \"--x\" ]; then\nEOF\necho work\n")
    green("G1·«if $1» внутри heredoc ниже harness_run — не служебная ветка, зелёный",
          Tree(tmp, files=files).gate(), r"демонов 4\b")

    files = dict(BASE_FILES)
    files["harness/demons/d1.sh"] = (DAEMON_SELFTEST + "harness_run d1\n" + TRAP_OK + "cat <<'EOF'\n"
                                     "if [ \"${1:-}\" = \"--x\" ]; then\n    exit 0\nfi\nEOF\necho work\n")
    green("G1·пара «if $1 … fi» целиком внутри heredoc ниже harness_run — не ветка, зелёный (Ф2-20)",
          Tree(tmp, files=files).gate(), r"демонов 4\b")

    files = dict(BASE_FILES)
    files["harness/demons/d1.sh"] = (DAEMON_SELFTEST + "helper() {\n"
                                     "    if [ \"${1:-}\" = \"--inner\" ]; then\n        echo x\n    fi\n}\n"
                                     "harness_run d1\n" + TRAP_OK + "helper --inner\n")
    green("G1·«if $1» внутри функции выше harness_run — не верхний уровень, зелёный",
          Tree(tmp, files=files).gate(), r"демонов 4\b")

    files = dict(BASE_FILES)
    files["harness/demons/d1.sh"] = (DAEMON_SELFTEST + "harness_run d1\n" + TRAP_OK + "helper() {\n"
                                     "if [ \"${1:-}\" = \"--inner\" ]; then\n    echo x\nfi\n}\n"
                                     "helper --inner\n")
    green("G1·«if $1 … fi» БЕЗ отступа в теле функции НИЖЕ harness_run — не ветка, зелёный (Ф2-21а)",
          Tree(tmp, files=files).gate(), r"демонов 4\b")

    files = dict(BASE_FILES)
    files["harness/demons/d1.sh"] = (DAEMON_SELFTEST + "harness_run d1\n" + TRAP_OK + "for arg in a b; do\n"
                                     "    if [ \"${1:-}\" = \"--inner\" ]; then\n        echo \"$arg\"\n    fi\n"
                                     "done\n")
    green("G1·«if $1 … fi» С отступом в цикле верхнего уровня НИЖЕ harness_run — не ветка, зелёный (Ф2-21б)",
          Tree(tmp, files=files).gate(), r"демонов 4\b")

    files = dict(BASE_FILES)
    files["harness/demons/d1.sh"] = (DAEMON_SELFTEST + "on_exit() {\n    rc=$?\n    rm -f \"$T\"\n"
                                     "    harness_run_end \"$rc\"\n}\nharness_run d1\nT=$(mktemp)\n"
                                     "trap on_exit EXIT\necho work\n")
    green("G1·trap on_exit EXIT, тело: rc=$? первой, harness_run_end \"$rc\" → зелёный",
          Tree(tmp, files=files).gate(), r"демонов 4\b")

    files = dict(BASE_FILES)
    files["harness/demons/d1.sh"] = SH_HEAD + "DRY_RUN=0\n" + case_tail + "harness_run d1\n" + TRAP_OK + "echo work\n"
    green("G1·ветка case \"${1:-}\" выше harness_run → зелёный", Tree(tmp, files=files).gate(), r"демонов 4\b")

    files = dict(BASE_FILES)
    files["harness/demons/d1.sh"] = (SH_HEAD + "LATE=0\n[ \"${1:-}\" = \"--досмотр\" ] && LATE=1\n"
                                     "harness_run d1\n" + TRAP_OK + "echo work \"$LATE\"\n")
    green("G1·ветка [ \"${1:-}\" = … ] && выше harness_run → зелёный", Tree(tmp, files=files).gate(),
          r"демонов 4\b")


# ───────────────────────── G2: писатель — по цели записи, не по подстроке ─────────────────────────
# Ревью Ф2 находка 3: 4 записи живого реестра называют писателем файл, где имя
# журнала стоит в докстроке, комментарии, чтении или пробе (внимание,
# предложения, пайплайн-обходы, env-teardown).

def registry_with_attn(writer: str) -> dict:
    reg = base_registry()
    reg["journals"]["attn"] = {"file": "attn.jsonl", "writers": [writer], "time_field": "ts", "time_kind": "tz"}
    return reg


FALSE_WRITERS = {
    "слово журнала в докстроке (urgency.py → внимание)":
        ("scripts/urgency.py", "\"\"\"Срочность: слово attn в докстроке, файл ничего не пишет.\"\"\"\nprint(1)\n"),
    "файл только ЧИТАЕТ журнал (pipeline.py → пайплайн-обходы)":
        ("scripts/pipe.py", "import os\nfor line in open(os.path.join(os.environ['LOG_DIR'], 'attn.jsonl')):\n"
                            "    pass\n"),
    "имя журнала в комментарии bash":
        ("scripts/cm.sh", "#!/bin/bash\n# когда-нибудь пишет attn.jsonl\necho ok\n"),
    "проба test_* (test_environment.sh → env-teardown)":
        ("scripts/test_attn.sh", "#!/bin/bash\nT=$(mktemp -d)\necho '{}' >> \"$T/attn.jsonl\"\n"
                                 "grep -c . \"$LOG_DIR/attn.jsonl\"\n"),
}

TOKENS_FAMILY = "tokens-[0-9][0-9][0-9][0-9]-[0-9][0-9].jsonl"  # не-секрет: маска имени журнала


def case_g2_writer_target(tmp: Path) -> None:
    print("— G2 --registry: писатель реестра судится по цели записи —")

    for what, (rel, text) in FALSE_WRITERS.items():
        t = Tree(tmp, registry=registry_with_attn(rel))
        t.add(rel, text)
        red_naming(f"G2·писатель attn — {what} → красный", t.gate("--registry"), "attn", rel)

    # Здоровые: место записи называет журнал.
    healthy = {
        "bash >> \"$LOG_DIR/attn.jsonl\" (tg-dispatcher.sh:75)":
            ("scripts/attnw.sh", "#!/bin/bash\nprintf '%s\\n' '{}' >> \"$LOG_DIR/attn.jsonl\"\n"),
        "bash >> \"$J\", J присвоен путь attn.jsonl":
            ("scripts/attnv.sh", "#!/bin/bash\nJ=\"$LOG_DIR/attn.jsonl\"\necho '{}' >> \"$J\"\n"),
        "emit(journal=\"attn\")":
            ("scripts/attne.py", "import events\nevents.emit(\"scripts/good.py\", \"a.x\", \"ok\", journal=\"attn\")\n"),
        "harness_event attn":
            ("scripts/attnh.sh", "#!/bin/bash\nharness_event attn scripts/good.py a.x ok\n"),
        "python heredoc open(путь, \"a\"), путь из sys.argv = \"$LOG_DIR/attn.jsonl\" (memory-revision.sh:262)":
            ("scripts/attnhd.sh", "#!/bin/bash\nA=\"$LOG_DIR/attn.jsonl\"\npython3 - \"$A\" <<'PY'\n"
                                  "import sys\nwith open(sys.argv[1], \"a\") as fh:\n    fh.write('{}')\nPY\n"),
    }
    for what, (rel, text) in healthy.items():
        t = Tree(tmp, registry=registry_with_attn(rel))
        t.add(rel, text)
        green(f"G2·писатель attn — {what} → зелёный", t.gate("--registry"), r"журналов в реестре 3\b")

    # Помесячное семейство: живой tokens-collector.sh пишет f"tokens-{month}.jsonl" из heredoc.
    reg = base_registry()
    reg["disk_families"] = [TOKENS_FAMILY]
    reg["journals"]["tokens-2026-10"] = {"file": "tokens-2026-10.jsonl", "writers": ["harness/demons/tc.sh"],
                                         "time_field": "ts", "time_kind": "Z"}
    t = Tree(tmp, registry=reg)
    t.add("harness/demons/tc.sh", HEREDOC_TOKENS.replace("tok-{month}", "tokens-{month}"))
    green("G2·писатель семейства tokens-<месяц> — open(events_path) с f\"tokens-{month}.jsonl\" → зелёный",
          t.gate("--registry"), r"журналов в реестре 3\b")


# ───────────────────────── G2: код ↔ реестр ─────────────────────────

def case_g2_registry(tmp: Path) -> None:
    print("— G2 --registry: код ↔ реестр (коммит) —")

    t = Tree(tmp)
    text = ("import events\n"
            "events.emit(\"scripts/good.py\", \"probe.x\", \"ok\", journal=\"новый-журнал\")  # ЗДЕСЬ\n")
    n = t.add("scripts/good2.py", text)
    red_naming("G2·emit в журнал без строки реестра → красный", t.gate("--registry"),
               "новый-журнал", f"scripts/good2.py:{n}")

    t = Tree(tmp)
    t.add("scripts/bashnew.sh", "#!/bin/bash\nharness_event чужой-журнал scripts/good.py probe.x ok\n")
    red_naming("G2·harness_event в журнал без строки реестра → красный", t.gate("--registry"), "чужой-журнал")

    reg = base_registry()
    reg["journals"]["orphan"] = {"file": "orphan.jsonl", "writers": ["scripts/plain.sh"],
                                 "time_field": "ts", "time_kind": "tz"}
    red_naming("G2·запись реестра, чей писатель журнала не называет → красный",
               Tree(tmp, registry=reg).gate("--registry"), "orphan")

    reg = base_registry()
    reg["journals"]["lostwriter"] = {"file": "lostwriter.jsonl", "writers": ["scripts/no-such.py"],
                                     "time_field": "ts", "time_kind": "tz"}
    red_naming("G2·писатель реестра — несуществующий файл → красный",
               Tree(tmp, registry=reg).gate("--registry"), "lostwriter")

    reg = base_registry()
    reg["journals"]["sample"].pop("why")
    red_naming("G2·writers: none без why → красный", Tree(tmp, registry=reg).gate("--registry"), "sample")

    reg = base_registry()
    reg["journals"]["gates"].update({"rotate": "monthly", "readers": ["scripts/reader.py"]})
    t = Tree(tmp, registry=reg)
    t.add("scripts/reader.py", "for line in open('/var/log/harness/gates.jsonl'):\n    pass\n")
    red_naming("G2·rotate: monthly при читателе мимо events.read() → красный", t.gate("--registry"),
               "gates", "scripts/reader.py")

    t = Tree(tmp, registry=with_registry(legacy_writers=[], journals={}))
    red_naming("G2·реестр без журналов → красный", t.gate("--registry"), "журналов в реестре 0")

    # Здоровые.
    reg = base_registry()
    reg["journals"]["gates"].update({"rotate": "monthly", "readers": ["scripts/reader.py"]})
    t = Tree(tmp, registry=reg)
    t.add("scripts/reader.py", "import events\nfor ev in events.read(None, None, journals=['gates']):\n    pass\n")
    green("G2·rotate: monthly, все читатели на events.read() → зелёный", t.gate("--registry"))

    t = Tree(tmp)
    (t.logs / "evo").mkdir()
    write(t.logs / "evo" / "незнакомый.jsonl", "{}\n")
    green("G2·--registry диск не судит (в pre-commit не судится) → зелёный",
          t.gate("--registry"), r"журналов в реестре 2\b")


# ───────────────────────── G2: диск → реестр ─────────────────────────

def disk_registry(migrated: bool = True) -> dict:
    reg = {"ignore_dirs": ["demon-stream", "проба-хука", ".events"],
           "journals": {
               "a": {"file": "a.jsonl", "writers": ["scripts/a.sh"], "time_field": "ts", "time_kind": "tz"},
               "m": {"file": "m.jsonl", "writers": ["scripts/m.py"], "time_field": "ts", "time_kind": "tz"},
               "evo-s": {"file": "evo/s.jsonl", "writers": ["scripts/s.sh"], "time_field": "ts", "time_kind": "tz"},
               "p": {"file": "p.jsonl", "dir": "panel_state", "writers": ["scripts/p.py"],
                     "time_field": "ts", "time_kind": "naive"},
           }}
    if migrated:
        reg["journals"]["m"].update({"migrated": "2026-10-20", "legacy_lines": 2, "mode": "0640"})
    return reg


class Disk:
    """Каталог журналов стенда: здоровый по построению; правки — в случаях."""

    def __init__(self, tmp: Path, registry: dict | None = None):
        self.base = Path(tempfile.mkdtemp(prefix="disk-", dir=tmp))
        self.logs = self.base / "log"
        self.panel = self.base / "panel-state"
        self.reg_path = self.base / "journals.yaml"
        self.root = self.base / "tree"
        dump_yaml(self.reg_path, disk_registry() if registry is None else registry)
        dump_yaml(self.root / "harness/config/journals.yaml", disk_registry() if registry is None else registry)
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        write(self.logs / "a.jsonl", json.dumps({"ts": now, "x": 1}) + "\n", 0o644)
        old = [json.dumps({"ts": now, "старое": i}) for i in range(2)]
        v1 = [json.dumps({"v": 1, "ts": now, "src": "scripts/m.py", "event": "m.x", "outcome": "ok"})
              for _ in range(3)]
        write(self.logs / "m.jsonl", "\n".join(old + v1) + "\n", 0o640)
        os.chown(self.logs / "m.jsonl", -1, GROUP_GID)
        write(self.logs / "evo" / "s.jsonl", "{}\n")
        write(self.logs / "demon-stream" / "поток.jsonl", "{}\n")
        write(self.logs / ".events" / "lost.jsonl", "")
        os.symlink("a.jsonl", self.logs / "ссылка.jsonl")
        # Имя файла в panel-state — ключ доступа: генерируется, не копируется.
        self.secret_name = "tok-" + secrets.token_hex(12) + ".jsonl"
        write(self.panel / self.secret_name, "{}\n", 0o600)

    def gate(self, *flags: str) -> tuple[int, str]:
        env = clean_env(HARNESS_LOG_DIR=str(self.logs), PANEL_STATE_DIR=str(self.panel),
                        HARNESS_JOURNALS=str(self.reg_path), HARNESS_EVENTS_STATE=str(self.logs / ".events"))
        return run([sys.executable, GATE, "--root", str(self.root), *flags], env)


def case_g2_disk(tmp: Path) -> None:
    print("— G2 --disk: диск → реестр (прогон ворот) —")

    d = Disk(tmp)
    write(d.logs / "evo" / "новый.jsonl", "{}\n")
    red_naming("G2·файл в подкаталоге без строки реестра → красный в --disk", d.gate("--disk"), "evo/новый.jsonl")
    tree = Tree(tmp)
    write(tree.logs / "evo" / "новый.jsonl", "{}\n")
    green("G2·тот же файл в --registry (pre-commit) не судится", tree.gate("--registry"))

    d = Disk(tmp)
    with open(d.logs / "m.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"без": "времени"}) + "\n")
    red_naming("G2·лишняя строка без v (и без времени) в переведённом журнале → красный",
               d.gate("--disk"), "m", "лишних строк 1")

    d = Disk(tmp)
    with open(d.logs / "m.jsonl", "a", encoding="utf-8") as fh:
        fh.write("{битая строка\n")
    red_naming("G2·битая строка в переведённом журнале считается старой → красный", d.gate("--disk"),
               "лишних строк 1")

    d = Disk(tmp)
    os.chmod(d.logs / "m.jsonl", 0o644)
    red_naming("G2·переведённый журнал 0644 при 0640 в реестре → красный", d.gate("--disk"), "m")

    other = [g for g in os.getgroups() if g != GROUP_GID]
    if other:
        d = Disk(tmp)
        os.chown(d.logs / "m.jsonl", -1, other[0])
        red_naming("G2·переведённый журнал чужой группы → красный", d.gate("--disk"), "m")
    else:
        skip("G2·переведённый журнал чужой группы", "у агента одна группа — подменить нечем")

    d = Disk(tmp)
    os.unlink(d.logs / "m.jsonl")
    rc, out = d.gate("--disk")
    check("G2·переведённых 1, судимых по (в)/(д) 0 (файла нет) → красный: пустой набор",
          rc == 1 and "переведённых 1" in out, f"rc={rc}; {out}")

    d = Disk(tmp, registry={"ignore_dirs": [".events"], "journals": {}})
    red_naming("G2·--disk при пустом реестре → красный", d.gate("--disk"), "журналов в реестре 0")

    # Здоровые.
    d = Disk(tmp)
    res = d.gate("--disk")
    green("G2·здоровый диск → зелёный, числа стенда точно", res,
          r"журналов в реестре 4, на диске 3, переведённых 1, лишних строк 0")
    check("G2·имя файла panel-state (ключ доступа) не напечатано", d.secret_name not in res[1],
          "имя файла из panel-state попало в вывод ворот")
    check("G2·ignore_dirs и симлинк не судятся (поток, lost.jsonl, ссылка)",
          not any(w in res[1] for w in ("поток.jsonl", "lost.jsonl", "ссылка.jsonl")), res[1])

    d = Disk(tmp, registry=disk_registry(migrated=False))
    os.chmod(d.logs / "m.jsonl", 0o644)
    green("G2·непереведённый 0644 → зелёный; «не судится: переведённых 0»", d.gate("--disk"),
          r"переведённых 0", r"не судится: переведённых 0")

    # Живой реестр (копия): помесячное семейство сборщика расхода и симлинк.
    d = Disk(tmp)
    shutil.copy(LIVE_REGISTRY, d.reg_path)
    shutil.copy(LIVE_REGISTRY, d.root / "harness/config/journals.yaml")
    for f in list(d.logs.rglob("*.jsonl")):
        if f.is_symlink() or f.is_file():
            f.unlink()
    today = dt.date.today()
    next_month = (today.replace(day=1) + dt.timedelta(days=32)).strftime("%Y-%m")
    write(d.logs / f"tokens-{next_month}.jsonl", "{}\n")
    write(d.logs / f"tokens-{today:%Y-%m}.jsonl", "{}\n")
    os.symlink(f"tokens-{next_month}.jsonl", d.logs / "tokens.jsonl")
    write(d.logs / "gates.jsonl", "{}\n", 0o644)
    green(f"G2·живой реестр: tokens-{next_month}.jsonl (новый месяц) и симлинк tokens.jsonl → зелёный",
          d.gate("--disk"), r"на диске 3\b")


# ───────────────────────── G3: потери и тишина ─────────────────────────

YESTERDAY = (dt.datetime.now().astimezone() - dt.timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)


def g3_registry(gates_min: bool = True, migrated: tuple[str, ...] = ("gates", "skills")) -> dict:
    reg = {"ignore_dirs": [".events"], "journals": {
        "gates": {"file": "gates.jsonl", "writers": ["scripts/pre-commit-gate.sh"], "time_field": "ts", "time_kind": "tz"},
        "skills": {"file": "skills.jsonl", "writers": ["scripts/hooks/skill_log.sh"], "time_field": "ts", "time_kind": "Z"},
    }}
    for name in migrated:
        reg["journals"][name].update({"migrated": "2026-10-20", "legacy_lines": 0})
    if gates_min:
        reg["journals"]["gates"]["min_daily"] = 1
    return reg


class State:
    def __init__(self, tmp: Path, registry: dict | None = None, gates_v1_yesterday: int = 1,
                 work: bool = True, lost: list[dict] | None = None):
        self.base = Path(tempfile.mkdtemp(prefix="g3-", dir=tmp))
        self.logs = self.base / "log"
        self.state = self.logs / ".events"
        self.reg_path = self.base / "journals.yaml"
        dump_yaml(self.reg_path, g3_registry() if registry is None else registry)
        lines = [json.dumps({"v": 1, "ts": YESTERDAY.isoformat(timespec="seconds"), "src": "scripts/pre-commit-gate.sh",
                             "event": "gate.verdict", "outcome": "ok"}) for _ in range(gates_v1_yesterday)]
        write(self.logs / "gates.jsonl", "".join(l + "\n" for l in lines))
        write(self.logs / "skills.jsonl", "")
        spent_at = YESTERDAY if work else YESTERDAY - dt.timedelta(days=2)
        tok = {"ts": spent_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
               "session": "probe", "model": "m", "in": 100, "out": 50, "cache_w": 0, "cache_r": 0,
               "tools": 0, "side": False}
        write(self.logs / f"tokens-{spent_at:%Y-%m}.jsonl", json.dumps(tok) + "\n")
        write(self.state / "lost.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in (lost or [])))

    def gate(self, **doors: str) -> tuple[int, str]:
        env = clean_env(HARNESS_LOG_DIR=str(self.logs), HARNESS_EVENTS_STATE=str(self.state),
                        HARNESS_JOURNALS=str(self.reg_path), **doors)
        return run([sys.executable, CLI, "lost", "--gate"], env)

    def tokens_files(self) -> list[Path]:
        return sorted(self.logs.glob("tokens-*.jsonl"))


def lost_row(kind: str = "lost", reason: str = "lock-timeout", journal: str = "guard") -> dict:
    return {"ts": YESTERDAY.isoformat(timespec="seconds"), "journal": journal, "src": "scripts/x.py",
            "reason": reason, "kind": kind}


def unread(name: str, res: tuple[int, str], *needles: str, forbidden: str) -> None:
    """«Не прочитал» ≠ «пусто»: названа причина «не читается» и названный
    источник, а ложного здорового ответа (forbidden) нет."""
    rc, out = res
    missing = [n for n in ("не читается", *needles) if n not in out]
    check(name, not missing and forbidden not in out,
          f"rc={rc}, не названо {missing}, ложное «{forbidden}» {'есть' if forbidden in out else 'нет'}; вывод: {out}")


def case_g3_unreadable(tmp: Path) -> None:
    print("— G3: источник не читается — это не «пусто» —")
    if os.geteuid() == 0:
        skip("G3·нечитаемые источники", "проба идёт от root — chmod 000 не закрывает чтение")
        return

    s = State(tmp, gates_v1_yesterday=0)
    for f in s.tokens_files():
        f.unlink()
    unread("G3·тишина, файла расхода нет (сборщик умер) → «признак работы не читается», не «работы не было»",
           s.gate(), "tokens", forbidden="работы не было")

    s = State(tmp, gates_v1_yesterday=0)
    for f in s.tokens_files():
        os.chmod(f, 0)
    unread("G3·тишина, файл расхода chmod 000 → «не читается», не «работы не было»",
           s.gate(), "tokens", forbidden="работы не было")

    s = State(tmp, lost=[lost_row()])
    os.chmod(s.state / "lost.jsonl", 0)
    unread("G3·lost.jsonl chmod 000 (потеря внутри) → «lost.jsonl не читается», не «потерь без разбора 0»",
           s.gate(), "lost.jsonl", forbidden="потерь без разбора 0")

    s = State(tmp)
    os.chmod(s.logs / "gates.jsonl", 0)
    rc, out = s.gate()
    unread("G3·журнал gates chmod 000 → красный «не читается», не тишина «событий v1 0»",
           (rc, out), "gates", forbidden="событий v1 0")
    check("G3·нечитаемый журнал с min_daily → rc 1", rc == 1, f"rc={rc}; {out}")


SKEW_ZONE = "Asia/Yekaterinburg"     # UTC+5 круглый год: местные 03:00 = 22:00 UTC прошлой даты


def case_g3_local_day(tmp: Path) -> None:
    """Ф2-43: сутки судятся по МЕСТНОМУ поясу, а не строкой ts[:10]. Событие
    вчера в 03:00 местного, записанное в UTC, несёт позавчерашнюю дату."""
    print("— G3: сутки события — местные, не дата строки —")
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(SKEW_ZONE)
    yday = (dt.datetime.now(zone) - dt.timedelta(days=1)).date()
    event = dt.datetime.combine(yday, dt.time(3, 0), zone).astimezone(dt.timezone.utc)
    if event.date() == yday:
        skip("G3·событие 03:00 местного в UTC", "дата UTC совпала с местной — стенд не различает")
        return
    s = State(tmp, gates_v1_yesterday=0)
    for f in s.tokens_files():
        f.unlink()
    write(s.logs / "gates.jsonl", json.dumps({"v": 1, "ts": event.isoformat(timespec="seconds"),
                                              "src": "scripts/pre-commit-gate.sh", "event": "gate.verdict",
                                              "outcome": "ok"}) + "\n")
    spent = dt.datetime.combine(yday, dt.time(12, 0), zone).astimezone(dt.timezone.utc)
    write(s.logs / f"tokens-{yday:%Y-%m}.jsonl",
          json.dumps({"ts": spent.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "session": "probe", "model": "m",
                      "in": 100, "out": 50}) + "\n")
    green(f"G3·событие вчера 03:00 {SKEW_ZONE} записано в UTC ({event:%Y-%m-%dT%H:%MZ}) → вчерашнее, тихих 0",
          s.gate(TZ=SKEW_ZONE), r"работа была: да\), тихих 0")


def case_g3(tmp: Path) -> None:
    print("— G3: потеря записи и тишина журнала —")

    red_naming("G3·строка lost без ack → красный «потерь без разбора 1»",
               State(tmp, lost=[lost_row()]).gate(), "потерь без разбора 1")

    red_naming("G3·журнал с min_daily без событий v1 за прошлые сутки при расходе > 0 → красный",
               State(tmp, gates_v1_yesterday=0).gate(), "gates")

    s = State(tmp, gates_v1_yesterday=0)
    old = json.dumps({"ts": YESTERDAY.isoformat(timespec="seconds"), "verdict": "pass"})
    write(s.logs / "gates.jsonl", old + "\n" + old + "\n")
    red_naming("G3·за сутки только строки старого вида (без v: 1) → красный: судятся события v1",
               s.gate(), "gates")

    red_naming("G3·переведённые есть, с min_daily — ни одного (P>0, J=0) → красный",
               State(tmp, registry=g3_registry(gates_min=False)).gate(), "с min_daily 0")

    # Здоровые.
    green("G3·lost, затем ack → зелёный",
          State(tmp, lost=[lost_row(), lost_row(kind="ack", reason="разобрано", journal="-")]).gate(),
          r"потерь без разбора 0")
    green("G3·строка mangled (событие записано) — не потеря",
          State(tmp, lost=[lost_row(kind="mangled", reason="secret")]).gate(), r"потерь без разбора 0")
    green("G3·тот же тихий журнал при расходе 0 → «не судится: работы не было»",
          State(tmp, gates_v1_yesterday=0, work=False).gate(), r"не судится: работы не было")
    green("G3·журнал без min_daily с нулевым днём → «не судится», не красный",
          State(tmp).gate(), r"skills.*нулевых дней")
    green("G3·здоровые сутки → зелёный, числа стенда точно", State(tmp).gate(),
          r"потерь без разбора 0; переведённых 2, с min_daily 1, судимых 1 \(работа была: да\), тихих 0")
    green("G3·до первого перевода → «не судится: переведённых 0»",
          State(tmp, registry=g3_registry(gates_min=False, migrated=())).gate(),
          r"не судится: переведённых 0")


# ───────────────────────── общее: ворота не пишут, G4 ─────────────────────────

def snapshot(path: Path) -> dict:
    return {str(p.relative_to(path)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(path.rglob("*")) if p.is_file() and not p.is_symlink()}


def case_no_writes(tmp: Path) -> None:
    print("— ворота ничего не пишут —")
    t = Tree(tmp)
    before_tree, before_logs = snapshot(t.root / "scripts"), snapshot(t.logs)
    t.gate(); t.gate("--registry")
    check("G1/G2·прогон по дереву не меняет код и журналы стенда",
          snapshot(t.root / "scripts") == before_tree and snapshot(t.logs) == before_logs, "файлы изменились")
    d = Disk(tmp)
    before = snapshot(d.base)
    d.gate("--disk")
    check("G2·--disk не меняет каталог журналов и panel-state", snapshot(d.base) == before, "файлы изменились")
    s = State(tmp, lost=[lost_row()])
    before = snapshot(s.base)
    s.gate()
    check("G3·lost --gate ничего не пишет (без курсора)", snapshot(s.base) == before, "файлы изменились")


def case_g4(tmp: Path) -> None:
    print("— G4: гейт секретов видит новые места журналов —")
    spec = importlib.util.spec_from_file_location("secret_in_logs", SECRETS_GATE)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        list_files = getattr(mod, "файлы_логов")
    except Exception as exc:  # noqa: BLE001 — модуль гейта не загрузился: это отказ, не зелёный
        check("G4·модуль check-secret-in-logs.py загружается", False, f"{type(exc).__name__}: {exc}")
        return
    logs = Path(tempfile.mkdtemp(prefix="g4-", dir=tmp))
    write(logs / ".events" / "lost.jsonl", "{}\n")
    (logs / "archive").mkdir()
    with gzip.open(logs / "archive" / "guard-2026-10.jsonl.gz", "wb") as fh:
        fh.write(b"{}\n")
    seen = {str(p.relative_to(logs)) for p in list_files(logs)}
    check("G4·.events/lost.jsonl в обходе гейта секретов", ".events/lost.jsonl" in seen, str(seen))
    check("G4·archive/*.jsonl.gz в обходе гейта секретов", "archive/guard-2026-10.jsonl.gz" in seen, str(seen))


def main() -> int:
    print(f"механизм ворот: {GATE}\nCLI: {CLI}\nгруппа событий стенда: {GROUP_NAME}")
    with tempfile.TemporaryDirectory(prefix="events-gates-") as tmp:
        os.chmod(tmp, 0o700)
        tmp_path = Path(tmp)
        for case in (case_g1_debt_pairs, case_g1_heredoc, case_g1_py_target, case_g2_writer_target,
                     case_g3_unreadable, case_g3_local_day,
                     case_g1_writers, case_g1_daemons, case_g2_registry, case_g2_disk, case_g3,
                     case_no_writes, case_g4):
            try:
                case(tmp_path)
            except Exception as exc:  # noqa: BLE001 — упавший стенд — неудача пробы, а не зелёный
                check(f"{case.__name__}: стенд собран", False, f"{type(exc).__name__}: {exc}")
    print(f"итого путей {paths}, неудач {failures}, не судится {not_judged}")
    return 0 if failures == 0 and paths > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
