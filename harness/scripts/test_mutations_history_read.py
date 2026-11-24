#!/usr/bin/env python3
"""Пробы файла истории git для захода мутаций (спека тяжёлых прогонов §А.2,
ревью-3 п.1–3): историю читает исполнитель ВНЕ среза и пишет в файл, заход в срезе
отвечает из файла и git не зовёт.

    python3 scripts/test_mutations_history_read.py

Контракт, который пробы ждут от кода:
  * `acceptance.py --история <ids> --в <файл>` — та же выборка, что у захода (механизмы,
    строки, долги бумаги), без прогона мутаций; код 0 — файл записан;
  * повтор git — только на таймаут и OSError, до 3 попыток; ответ с rc≠0 — ответ, без повтора;
    не прочиталось — событие `mutations.history-unread` с причиной, код ≠ 0;
  * в срезе (HARNESS_IN_CHECKS_SLICE=1) с HARNESS_GIT_HISTORY=<файл> `_git_lines` отвечает
    из файла; ключа нет — живой git и событие `mutations.history-miss`;
  * прибору мутаций HARNESS_GIT_HISTORY не передаётся (самотесты копий во временных репо).

Стенд — временный каталог со своим git (коммиты с трейлером `Dev-Map: <id>`), своими
таблицами мутаций, своим LOG_DIR, подставным прибором и подставным git (пишет метку на
каждый вызов и зовёт настоящий). Боевые журналы и очередь не трогаются.

Дверь для мутационного прогона: HARNESS_ACCEPTANCE — путь acceptance.py под пробой.
Последняя строка: `история мутаций: N путей, F ПЛОХО`.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

ACCEPTANCE = Path(os.environ.get("HARNESS_ACCEPTANCE") or HERE / "acceptance.py")
REAL_GIT = shutil.which("git") or "/usr/bin/git"

cases = Cases()
check = cases.check

MECHANISM = "scripts/m.py"
PROOF = "scripts/test_m.py"
PROBES = f"""пробы:
  {PROOF}:
    механизм: {MECHANISM}
    доказательство: python3 {PROOF}
    подмена: HARNESS_M
    мутация:
    - 's@a@b@'
"""
GREEN = "=== мутационная проба: покраснели как надо 2, дыр 0, из кэша 0 ==="

# Подставной прибор: строка «механизм|есть ли HARNESS_GIT_HISTORY» в журнал, итог зелёный.
STUB_PROBE = f'''#!/usr/bin/env python3
import os, sys
with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as fh:
    fh.write(sys.argv[-1] + "|" + ("есть" if "HARNESS_GIT_HISTORY" in os.environ else "нет") + "\\n")
print("{GREEN}")
'''

# Подставной git: метка на ЛЮБОЙ вызов; FAKE_GIT_FAIL=<подкоманда> — отказ кодом 128.
FAKE_GIT = f'''#!/usr/bin/env bash
echo "$*" >> "$FAKE_GIT_MARK"
if [ -n "${{FAKE_GIT_FAIL:-}}" ]; then
    case " $* " in *" $FAKE_GIT_FAIL "*) echo "fatal: подставной отказ" >&2; exit 128 ;; esac
fi
exec {REAL_GIT} "$@"
'''


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Stand:
    """Корень со своим git и копией acceptance.py; задача hr.a правит механизм."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.root = tmp / "дерево"
        self.logs = tmp / "logs"
        self.logs.mkdir(parents=True)
        self.root.mkdir()
        self.git_env = {"GIT_AUTHOR_NAME": "p", "GIT_AUTHOR_EMAIL": "p@s",
                        "GIT_COMMITTER_NAME": "p", "GIT_COMMITTER_EMAIL": "p@s"}
        self.git("init", "-q")
        config = self.root / "harness" / "config"
        config.mkdir(parents=True)
        (config / "invariants.yaml").write_text("инварианты: []\n", encoding="utf-8")
        (config / "probe-mutations.yaml").write_text(PROBES, encoding="utf-8")
        for name in (MECHANISM, PROOF):
            self.write(name)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "стенд")
        scripts = self.root / "scripts"
        shutil.copy(ACCEPTANCE, scripts / "acceptance.py")
        (scripts / "lib").symlink_to(ACCEPTANCE.resolve().parent / "lib")
        self.commit("hr.a", MECHANISM)
        self.commit("hr.b", MECHANISM)
        self.history = tmp / "история.json"
        self.calls = tmp / "прибор.log"
        self.mark = tmp / "git-метка.log"
        self.stub = tmp / "stub-probe.py"
        self.stub.write_text(STUB_PROBE, encoding="utf-8")
        fake_bin = tmp / "bin"
        fake_bin.mkdir()
        (fake_bin / "git").write_text(FAKE_GIT, encoding="utf-8")
        (fake_bin / "git").chmod(0o755)
        self.fake_bin = fake_bin
        self.install = tmp / "install.conf"
        self.install.write_text(f'PROJECT_DIR="{self.root}"\nLOG_DIR="{self.logs}"\n', encoding="utf-8")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True,
                              text=True, env={**os.environ, **self.git_env}).stdout.strip()

    def write(self, name: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("x = 1\n")

    def commit(self, task: str, *names: str) -> None:
        for name in names:
            self.write(name)
        self.git("add", *names)
        self.git("commit", "-q", "-m", f"правка\n\nDev-Map: {task}")

    def paper(self, task: str) -> None:
        log = self.root / ".tasks" / task / "мутации.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"коммит {self.git('rev-parse', 'HEAD')}\n--- {MECHANISM}\n{GREEN}\n"
                       f"=== мутации задачи {task}: покраснели 2, дыр 0 ===\n", encoding="utf-8")

    def env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HARNESS_", "MUTATION_", "MUT_")) and k not in ("LOG_DIR", "PROJECT_DIR")}
        env.update(self.git_env, LOG_DIR=str(self.logs), HARNESS_LOG_DIR=str(self.logs),
                   HARNESS_INSTALL_CONF=str(self.install), HARNESS_EVENTS_STATE=str(self.tmp / "state"),
                   HARNESS_MUTATION_PROBE=f"{sys.executable} {self.stub}", STUB_CALLS=str(self.calls),
                   FAKE_GIT_MARK=str(self.mark), PYTHONDONTWRITEBYTECODE="1")
        env.update(extra)
        return env

    def run(self, *args: str, **extra: str) -> tuple[int | None, str]:
        try:
            r = subprocess.run([sys.executable, str(self.root / "scripts" / "acceptance.py"), *args],
                               cwd=str(self.root), env=self.env(**extra), capture_output=True, text=True,
                               errors="replace", timeout=180)
            return r.returncode, r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            return None, "ТАЙМАУТ"

    def record(self, ids: str) -> tuple[int | None, str]:
        """История вне среза настоящим git."""
        return self.run("--история", ids, "--в", str(self.history))

    def in_slice(self, ids: str, **extra: str) -> tuple[int | None, str]:
        """Заход в срезе: подставной git первым в PATH, файл истории в env."""
        self.mark.unlink(missing_ok=True)
        return self.run("--мутации-задачи", ids, HARNESS_IN_CHECKS_SLICE="1",
                        PATH=f"{self.fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}", **extra)

    def marks(self) -> list:
        return self.mark.read_text(encoding="utf-8").splitlines() if self.mark.is_file() else []

    def events(self) -> str:
        return "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in self.logs.rglob("*.jsonl"))


class GitPatch:
    """Подмена subprocess.run в процессе пробы: git-вызовы судит answer(argv) —
    исключение, CompletedProcess или None (звать настоящий). Счёт вызовов — в calls."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls: list = []
        self.real = subprocess.run

    def __enter__(self):
        def run(argv, *a, **kw):
            if isinstance(argv, (list, tuple)) and argv and argv[0] == "git":
                self.calls.append(list(argv))
                reply = self.answer(list(argv), len(self.calls))
                if isinstance(reply, BaseException):
                    raise reply
                if reply is not None:
                    return reply
            return self.real(argv, *a, **kw)
        subprocess.run = run
        return self

    def __exit__(self, *exc) -> None:
        subprocess.run = self.real


def in_process(stand: Stand, name: str, answer, act, slice_file: Path | None = None):
    """(итог act(модуль) или текст исключения, вызовы git) — acceptance копии стенда в этом
    процессе; окружение стенда (и среза с файлом истории) — на время вызова."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(stand.env())
    if slice_file:
        os.environ.update(HARNESS_IN_CHECKS_SLICE="1", HARNESS_GIT_HISTORY=str(slice_file))
    saved_argv = sys.argv
    try:
        acc = load(stand.root / "scripts" / "acceptance.py", name)
        with GitPatch(answer) as patch:
            try:
                result = act(acc)
            except BaseException as error:  # noqa: BLE001 — упавший режим = красный путь
                result = f"{type(error).__name__}: {error}"
        return result, patch.calls
    finally:
        sys.argv = saved_argv
        os.environ.clear()
        os.environ.update(saved)


def history_mode(stand: Stand):
    """act: `acceptance.py --история hr.a --в <файл>` через main()."""
    def act(acc):
        sys.argv = [str(stand.root / "scripts" / "acceptance.py"), "--история", "hr.a", "--в",
                    str(stand.history)]
        return acc.main()
    return act


def timeout_once(argv: list, n: int):
    return subprocess.TimeoutExpired(argv, 120) if n == 1 else None


def timeout_always(argv: list, n: int):
    return subprocess.TimeoutExpired(argv, 120)


def merge_base_no(argv: list, n: int):
    if "merge-base" in argv:
        return subprocess.CompletedProcess(argv, 1, "", "")
    return None


def any_call(argv: list, n: int):
    return None


def probe_retry(tmp: Path) -> None:
    """Поломка: нет повтора — один таймаут git (в срезе git ждёт память) делает заявку
    «история не прочитана»."""
    s = Stand(tmp / "повтор")
    code, _ = in_process(s, "acc_retry", timeout_once, history_mode(s))
    check("1 git молчит раз (таймаут) → --история повторяет и кончает кодом 0, файл есть",
          (0, True), (code, s.history.is_file()))
    check("1 события history-unread нет", False, "mutations.history-unread" in s.events())
    got, calls = in_process(s, "acc_retry_read", any_call,
                            lambda acc: acc.механизмы_задачи("hr.a", s.root), slice_file=s.history)
    check("2 в срезе из файла: механизмы hr.a после повтора — [m.py], git не зван",
          ([MECHANISM], []), (got, calls))


def probe_always_silent(tmp: Path) -> None:
    """Поломки: повтор бесконечный или его нет; не прочиталось — молча (без события и
    причины), заявка закрывается «шаг пуст»."""
    s = Stand(tmp / "молчит")
    code, calls = in_process(s, "acc_silent", timeout_always, history_mode(s))
    first = calls[0] if calls else None
    check("3 git молчит всегда → --история кончает кодом ≠ 0", True, code not in (0, None))
    check("3 тот же вызов git — ровно 3 попытки", 3, sum(1 for c in calls if c == first))
    events = s.events()
    check("3 событие mutations.history-unread с причиной «таймаут»", (True, True),
          ("mutations.history-unread" in events, "таймаут" in events))


def probe_answer_not_retried(tmp: Path) -> None:
    """Ревью-3 п.2. Поломка: повтор на любой rc≠0 — законный ответ merge-base «не предок»
    (код 1) трижды даёт history-unread, и задача с устаревшей бумагой уходит в «потеряна»."""
    s = Stand(tmp / "не-предок")
    s.paper("hr.a")
    s.commit("hr.a", MECHANISM)
    code, calls = in_process(s, "acc_mb", merge_base_no, history_mode(s))
    check("4 merge-base --is-ancestor дал 1 → ответ, без повтора (1 вызов), --история код 0",
          (1, 0), (sum(1 for c in calls if "merge-base" in c), code))
    check("4 события history-unread нет", False, "mutations.history-unread" in s.events())
    debts, calls = in_process(s, "acc_mb_read", any_call,
                              lambda acc: acc.mutation_paper_debts("hr.a", s.root), slice_file=s.history)
    # Вне истории HEAD бумагу судит сверка кода: её вызовы git тоже записаны в файл.
    check("4 в срезе из файла: бумага вне истории HEAD судила другой код — перегнать; git не зван",
          (True, []), (isinstance(debts, list) and any("другой код" in d and MECHANISM in d
                                                       for d in debts), calls))


def probe_slice_reads_file(tmp: Path) -> None:
    """Поломки: история выписана не вся («прочли два места из восьми») — заход в срезе
    зовёт git; файл истории течёт в окружение прибора (самотесты копий во временных
    репо отвечают из чужого файла — мутация «поймана» ложно, ревью-3 п.3)."""
    s = Stand(tmp / "срез")
    code, out = s.record("hr.a")
    check("5 --история hr.a вне среза настоящим git — код 0, файл есть", (0, True),
          (code, s.history.is_file()))
    code, out = s.in_slice("hr.a", HARNESS_GIT_HISTORY=str(s.history))
    check("5 заход в срезе с файлом истории: дыр 0, бумага записана", (0, True, True),
          (code, "мутации задачи hr.a: дыр 0" in out,
           (s.root / ".tasks" / "hr.a" / "мутации.log").is_file()))
    check("5 подставной git пишет метку на ЛЮБОЙ вызов — меток нет", [], s.marks())
    runs = s.calls.read_text(encoding="utf-8").splitlines() if s.calls.is_file() else []
    check("6 прибор мутаций позван без HARNESS_GIT_HISTORY", [f"{MECHANISM}|нет"], runs)


def probe_miss(tmp: Path) -> None:
    """Ревью-3 п.1. Поломка: промах файла (ключа нет) отвечает «None» — для всех id это
    «шаг пуст», итог по А.4: заявка закрыта без мутаций и без тревоги."""
    s = Stand(tmp / "промах")
    s.record("hr.a")
    code, out = s.in_slice("hr.a,hr.b", HARNESS_GIT_HISTORY=str(s.history))
    check("7 ключа hr.b в файле нет → живой git (метки есть) и итог hr.b «дыр 0», не «шаг пуст»",
          (True, True, False), (bool(s.marks()), "мутации задачи hr.b: дыр 0" in out,
                                "hr.b: механизмов с записями нет — шаг пуст" in out))
    check("7 промах файла — событие mutations.history-miss", True, "mutations.history-miss" in s.events())


def probe_rev_parse_silent(tmp: Path) -> None:
    """Ревью-3 п.1. Поломка: отказ rev-parse в мутации_пачки печатается «шаг пуст» — это
    итог, и заявка закрывается без прогона; должно быть «история не прочитана»."""
    s = Stand(tmp / "rev-parse")
    code, out = s.in_slice("hr.a", FAKE_GIT_FAIL="rev-parse")
    check("8 rev-parse молчит в срезе → «hr.a: история не прочитана», не «шаг пуст»", (True, False),
          ("мутации задачи hr.a: история не прочитана" in out, "hr.a: механизмов с записями нет — шаг пуст" in out))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for name, probe in (("повтор", probe_retry), ("молчит всегда", probe_always_silent),
                            ("ответ не повтор", probe_answer_not_retried),
                            ("срез из файла", probe_slice_reads_file), ("промах", probe_miss),
                            ("rev-parse", probe_rev_parse_silent)):
            try:
                probe(tmp)
            except Exception as error:  # упавший стенд — красный путь, а не трассировка
                check(f"{name}: стенд отвечает", "ответ", f"{type(error).__name__}: {error}")
    print(f"история мутаций: {cases.paths} путей, {cases.failures} ПЛОХО")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
