#!/usr/bin/env python3
"""test_remote_run.py — стенд клиента runner (scripts/remote-run.py): дверь, `готов`, падение на локальный,
прогон `ворота`/`пробы` целиком — снимок, отправка, задание, опрос, итог, сироты, паспорт, среда.

Спека runner прогонов на втором сервере: §3.1 п. 1, 3–11; §4.2–4.4; §5.1; §6.1, §6.2; план, задачи T3, T4.

Стенд: копия remote-run.py и scripts/lib/ во временном каталоге — ВНЕ PROJECT_DIR машины, иначе
дверь не поверит HARNESS_DOOR_STAND_HOME (config.корень_двери). Паспорт стенда — его install.conf и
harness.conf; подставной `ssh` в PATH пишет доводы в файл и отвечает по сценарию стенда. Код
стороны runner (harness/runner/*) — НАСТОЯЩИЙ, из дерева пробы: ответ «готов» стенда собран по
договору входа, а случай (и) гонит сам вход в его стендовой двери RUNNER_STAND_ROOT — договор
ключей ответа судится живым входом, а не копией его формы в пробе.

Прогон (T4) идёт через настоящие runner-entry и runner-job в их стендовой двери: push — в голые
репозитории стенда, задание — в jobs/<id>, места — по меткам slots/. Тело прогона (runner-run, T2) и
сборщик итога (runner-collect, T2) подставлены одним сценарием FAKE_BODY: он пишет results/<id>/ по
договору §3.2 и сам ставит «предел», если спит дольше суммы потолков задания. Пока T2 нет, стенд
судит клиента, а не сторону runner.

Механизм — из двери HARNESS_REMOTE_RUN (путь копии под мутацией), иначе рядом с пробой.

    python3 scripts/test_remote_run.py
Код возврата: 0 — все случаи ок, 1 — есть ПЛОХО.
"""
from __future__ import annotations

import fnmatch
import importlib.machinery
import importlib.util
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
LIB = SCRIPTS / "lib"
sys.path.insert(0, str(LIB))
from selftest_cases import Cases  # noqa: E402
import config  # noqa: E402

MECHANISM = Path(os.environ.get("HARNESS_REMOTE_RUN") or SCRIPTS / "remote-run.py")
RUNNER_SRC = SCRIPTS.parent / "harness" / "runner"
RUNNER_CONF = SCRIPTS.parent / "harness" / "config" / "runner.yaml"
ADDRESS = "runner@stand.example"
FOREIGN = "evil@foreign.example"
PROJECT_REPO = "/srv/runner/repo/project.git"

# Подставной ssh: доводы — строкой JSON в файл вызовов; ответ — из файла сценария стенда.
# Сценарий с «entry» отвечает настоящим входом runner: последний довод — SSH_ORIGINAL_COMMAND,
# корень — стендовая дверь входа (её слушают только без SUDO_USER). «answers» подменяют ответ
# одной команды (её первое слово): код/вывод/сон, или настоящий архив «итог» с дописанной записью.
# Вход зовёт `sudo -n /usr/local/sbin/runner-job` — в стенде его нет: «прогнать»/«отменить» стенд
# отдаёт runner-job рядом со входом той же стендовой дверью, а задание «прогнать» пишет в received/.
FAKE_SSH = r'''#!/usr/bin/env python3
import io, json, os, subprocess, sys, tarfile, time
stand = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(stand, "ssh-calls.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\n")
with open(os.path.join(stand, "ssh-scenario.json"), encoding="utf-8") as fh:
    scenario = json.load(fh)
words = sys.argv[-1].split()
verb = words[0] if words else ""
answer = scenario.get("answers", {}).get(verb)


def entry_env():
    env = {k: v for k, v in os.environ.items() if k != "SUDO_USER"}
    env.update(SSH_ORIGINAL_COMMAND=sys.argv[-1], RUNNER_STAND_ROOT=scenario["stand_root"])
    return env


def with_member(real: bytes, spec: dict) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(real)) as src, tarfile.open(fileobj=out, mode="w") as dst:
        for member in src.getmembers():
            dst.addfile(member, src.extractfile(member))
        info = tarfile.TarInfo(spec["name"])
        if spec.get("type") == "sym":
            info.type, info.linkname = tarfile.SYMTYPE, spec["link"]
            dst.addfile(info)
        elif spec.get("type") == "hard":
            info.type, info.linkname = tarfile.LNKTYPE, spec["link"]
            dst.addfile(info)
        else:
            info.size = 1
            dst.addfile(info, io.BytesIO(b"x"))
    return out.getvalue()


if scenario.get("entry") and (answer is None or "append" in answer):
    if verb in ("прогнать", "отменить") and len(words) == 2:
        data = sys.stdin.buffer.read() if verb == "прогнать" else b""
        if verb == "прогнать":
            received = os.path.join(scenario["stand_root"], "received")
            os.makedirs(received, exist_ok=True)
            with open(os.path.join(received, os.path.basename(words[1]) + ".json"), "wb") as fh:
                fh.write(data)
        job = os.path.join(os.path.dirname(scenario["entry"]), "runner-job")
        mode = "старт" if verb == "прогнать" else "стоп"
        sys.exit(subprocess.run([sys.executable, job, mode, words[1]], input=data,
                                env=entry_env()).returncode)
    if answer is None:
        sys.exit(subprocess.run([sys.executable, scenario["entry"]], env=entry_env()).returncode)
    real = subprocess.run([sys.executable, scenario["entry"]], env=entry_env(), capture_output=True)
    sys.stdout.buffer.write(with_member(real.stdout, answer["append"]))
    sys.exit(real.returncode)
answer = scenario if answer is None else answer
time.sleep(answer.get("sleep", 0))
sys.stdout.write(answer.get("stdout", ""))
sys.stderr.write(answer.get("stderr", ""))
sys.exit(answer.get("code", 0))
'''

# Подставное тело прогона (runner-run + runner-collect, T2): ждёт `sleep` из body.json стенда, итог
# пишет по договору §3.2 атомарно (временный каталог и rename) и снимает jobs/<id>. Спит дольше суммы
# потолков задания — останавливается на потолке с исходом «предел», как юнит с RuntimeMaxSec.
# «итог» сценария переписывает поля итог.json, «big_mb» кладёт вывод ступени такого размера.
FAKE_BODY = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
run_id = sys.argv[1]
root = Path(os.environ["RUNNER_STAND_ROOT"])
job = json.loads((root / "jobs" / run_id).read_text(encoding="utf-8"))
scenario = json.loads((root / "body.json").read_text(encoding="utf-8")) if (root / "body.json").exists() else {}
limit = sum(job["потолки_с"].values())
started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
outcome = "готов"
if scenario.get("sleep", 0) > limit:
    time.sleep(limit)
    outcome = "предел"
else:
    time.sleep(scenario.get("sleep", 0))
steps = [] if outcome == "предел" else [
    {"имя": step["имя"], "код": 0, "секунд": 0.1, "ждал_места_с": 0, "красные": [], "выбор": []}
    for step in job["ступени"]]
total = {"id": run_id, "вид": job["вид"], **{key: job[key] for key in ("основа", "предмет", "база", "пакет")},
         "паспорт_sha": job["паспорт"].get("sha"), "исход": outcome, "ступени": steps,
         "пропущен_pre_commit": False, "начат": started, "кончен": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
         "машина": "стенд", "служба": {"SERVICE_RESULT": "success", "EXIT_CODE": "exited", "EXIT_STATUS": "0"}}
total.update(scenario.get("итог", {}))
temp = root / "results" / f".{run_id}.tmp"
temp.mkdir(parents=True)
(temp / "итог.json").write_text(json.dumps(total, ensure_ascii=False), encoding="utf-8")
(temp / "ступени.jsonl").write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in steps),
                                    encoding="utf-8")
if scenario.get("big_mb"):
    (temp / "вывод-выбор.log").write_bytes(os.urandom(scenario["big_mb"] * 2**20))
temp.rename(root / "results" / run_id)
(root / "jobs" / run_id).unlink()
'''

RUNNER_FILES = {p.name: p.read_bytes() for p in sorted(RUNNER_SRC.iterdir())
                if p.is_file() and not p.is_symlink()}


def expected_entry_sha() -> str:
    """Хеш настоящей функцией входа: проба не держит своей копии договора."""
    loader = importlib.machinery.SourceFileLoader("runner_entry", str(RUNNER_SRC / "runner-entry"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("runner_entry", loader))
    loader.exec_module(module)
    return module.entry_sha(RUNNER_FILES)


def git(*args: str, cwd: Path) -> str:
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="proba", GIT_AUTHOR_EMAIL="p@x", GIT_COMMITTER_NAME="proba",
               GIT_COMMITTER_EMAIL="p@x")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


class Stand:
    def __init__(self, root: Path, *, runner: str = ADDRESS, entry: bool = True,
                 known_hosts: bool = True, min_free: str = "20",
                 ssh_timeout: str = "20", package: bool = False, conf_extra: str = "",
                 runner_files: dict | None = None) -> None:
        runner_files = runner_files or RUNNER_FILES
        self.root = root
        self.tree = root / "tree"
        (self.tree / "scripts").mkdir(parents=True)
        (self.tree / "harness" / "config").mkdir(parents=True)
        shutil.copy2(MECHANISM, self.tree / "scripts" / "remote-run.py")
        shutil.copytree(LIB, self.tree / "scripts" / "lib",
                        ignore=shutil.ignore_patterns("__pycache__"))
        # Снимок судит check-secrets.sh дерева клиента, паспорт и среду — данные его runner.yaml.
        shutil.copy2(SCRIPTS / "check-secrets.sh", self.tree / "scripts" / "check-secrets.sh")
        for name in ("runner.yaml", "secret-sources.yaml"):
            shutil.copy2(SCRIPTS.parent / "harness" / "config" / name, self.tree / "harness" / "config" / name)
        if entry:
            (self.tree / "harness" / "runner").mkdir()
            (self.tree / "harness" / "runner" / "runner-entry").write_bytes(RUNNER_FILES["runner-entry"])
        self.home = root / "home"
        self.log = root / "log"
        self.state = root / "state"
        for d in (self.home, self.log, self.state, root / "bin"):
            d.mkdir()
        self.project = root / "project"
        (self.project / "harness" / "runner").mkdir(parents=True)
        git("init", "-q", "-b", "master", cwd=self.project)
        for name, body in runner_files.items():
            (self.project / "harness" / "runner" / name).write_bytes(body)
        (self.project / "README").write_text("стенд\n", encoding="utf-8")
        git("add", "-A", cwd=self.project)
        git("commit", "-q", "-m", "стенд", cwd=self.project)
        self.package = root / ("package" if package else "нет-пакета")
        if package:
            self.package.mkdir()
            git("init", "-q", "-b", "master", cwd=self.package)
            (self.package / "VERSION").write_text("пакет\n", encoding="utf-8")
            git("add", "-A", cwd=self.package)
            git("commit", "-q", "-m", "пакет", cwd=self.package)
        self.known_hosts = self.home / "runner_known_hosts"
        if known_hosts:
            self.known_hosts.write_text("stand.example ssh-ed25519 AAAA\n", encoding="utf-8")
        self.key = self.home / "runner_ed25519"
        (self.home / "install.conf").write_text(
            f'PROJECT_DIR="{self.project}"\nLOG_DIR="{self.log}"\nSTATE_DIR="{self.state}"\n',
            encoding="utf-8")
        (self.home / "harness.conf").write_text(
            f'REMOTE_RUNNER="{runner}"\nRUNNER_SSH_KEY="{self.key}"\n'
            f'RUNNER_KNOWN_HOSTS="{self.known_hosts}"\nRUNNER_SSH_TIMEOUT_SEC="{ssh_timeout}"\n'
            f'RUNNER_MIN_FREE_GB="{min_free}"\nSTARTER_DIR="{self.package}"\n{conf_extra}',
            encoding="utf-8")
        (root / "bin" / "ssh").write_text(FAKE_SSH, encoding="utf-8")
        (root / "bin" / "ssh").chmod(0o755)
        self.scenario(stdout=json.dumps(self.ready()))
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("HARNESS_", "RUNNER_")) and k != "REMOTE_RUNNER"}
        self.env.update(PATH=f"{root / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                        HARNESS_DOOR_STAND_HOME=str(self.home), HARNESS_HOME=str(self.home),
                        LOG_DIR=str(self.log), STATE_DIR=str(self.state))

    @staticmethod
    def ready(**override) -> dict:
        # Форма ответа — договор runner-entry «готов»; случай (и) сверяет её с живым входом.
        answer = {"свободно_гб": 120.5,
                  "версии": {"git": "git version 2.43.0", "python3": "3.12.3",
                             "bash": "GNU bash, version 5.2.21(1)-release"},
                  "мест_свободно": {"всего": 6, "под_ворота": 2}, "sha_входа": expected_entry_sha()}
        answer.update(override)
        return answer

    def scenario(self, **fields) -> None:
        (self.root / "ssh-scenario.json").write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.tree / "scripts" / "remote-run.py"), *args],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def calls(self) -> list[list[str]]:
        path = self.root / "ssh-calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def events(self) -> list[dict]:
        path = self.log / "runner.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def answer_sha(stdout: str):
    try:
        return json.loads(stdout).get("sha_входа")
    except ValueError:
        return f"не JSON: {stdout[:80]!r}"


def fallback_reason(stand: Stand, done: subprocess.CompletedProcess) -> tuple:
    """(строка в выводе есть, причина события, код) — что видит вызывающий и журнал."""
    line = next((s for s in done.stderr.splitlines() if s.startswith("runner недоступен: ")), "")
    reasons = [e.get("data", {}).get("причина", "") for e in stand.events()
               if e.get("event") == "runner.fallback"]
    return (line.endswith(" — прогон локально"), reasons[-1] if reasons else None, done.returncode)


def with_stand(**kw):
    root = Path(tempfile.mkdtemp(prefix="remote-run-stand-"))
    return root, Stand(root, **kw)


SERVER_FILES = {**RUNNER_FILES, "runner-run": FAKE_BODY.encode()}
# Опрос — раз в секунду, потеря связи — 30 с: случаям прогона хватает, (с) и (с2) ставят свои 2 с.
POLL_CONF = 'RUNNER_POLL_SEC="1"\nRUNNER_LOST_SEC="30"\n'
GATE_STEPS = [{"имя": "pre-commit", "потолок_с": 60}, {"имя": "выбор", "имена": ["стенд"], "потолок_с": 60}]
PROBE_STEPS = [{"имя": "пробы", "команда": ["true"], "потолок_с": 60}]


def with_server(conf_extra: str = "", **kw):
    """Стенд с runner целиком: настоящие вход и runner-job, подставное тело, голые репозитории.

    Тело FAKE_BODY лежит и в коде стенда runner, и в harness/runner проекта: sha_входа равны."""
    root, st = with_stand(min_free="0", package=True, runner_files=SERVER_FILES,
                          conf_extra=POLL_CONF + conf_extra, **kw)
    server = root / "server"
    (server / "code").mkdir(parents=True)
    (server / "conf").mkdir()
    for name, body in SERVER_FILES.items():
        (server / "code" / name).write_bytes(body)
        (server / "code" / name).chmod(0o755)
    shutil.copy2(RUNNER_CONF, server / "conf" / "runner.yaml")
    for name in ("project", "package"):
        git("init", "-q", "--bare", str(server / "repo" / f"{name}.git"), cwd=root)
    st.server = server
    serve(st)
    # ../x из временного каталога клиента лёг бы сюда: TMPDIR — внутри корня стенда.
    (root / "tmp").mkdir()
    st.env["TMPDIR"] = str(root / "tmp")
    st.shas = {"база": git("rev-parse", "HEAD", cwd=st.project)}
    for key in ("основа", "предмет"):
        (st.project / f"{key}.txt").write_text(f"{key}\n", encoding="utf-8")
        git("add", f"{key}.txt", cwd=st.project)
        git("commit", "-q", "-m", key, cwd=st.project)
        st.shas[key] = git("rev-parse", "HEAD", cwd=st.project)
    return root, st


def serve(st: Stand, **answers) -> None:
    st.scenario(entry=str(st.server / "code" / "runner-entry"), stand_root=str(st.server), answers=answers)


def set_body(st: Stand, **scenario) -> None:
    (st.server / "body.json").write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")


def gates(st: Stand, steps=None) -> tuple[subprocess.CompletedProcess, Path]:
    out = Path(tempfile.mkdtemp(dir=st.root, prefix="итог-в-"))
    done = st.run("ворота", "--основа", st.shas["основа"], "--предмет", st.shas["предмет"],
                  "--база", st.shas["база"], "--ступени", json.dumps(steps or GATE_STEPS, ensure_ascii=False),
                  "--итог-в", str(out))
    return done, out


def probes(st: Stand) -> subprocess.CompletedProcess:
    out = Path(tempfile.mkdtemp(dir=st.root, prefix="итог-в-"))
    return st.run("пробы", "--дерево", str(st.project), "--ступени",
                  json.dumps(PROBE_STEPS, ensure_ascii=False), "--итог-в", str(out))


def verb_ids(st: Stand, verb: str) -> list[str]:
    return [c[-1].split()[1] for c in st.calls() if c and c[-1].startswith(f"{verb} ")]


def received(st: Stand, run_id: str) -> dict:
    path = st.server / "received" / f"{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def run_refs(st: Stand, repo: str, run_id: str) -> dict:
    """refs/runs/<id>/* голого репозитория стенда: последнее слово ссылки → sha."""
    lines = git("for-each-ref", "--format=%(refname) %(objectname)", f"refs/runs/{run_id}/",
                cwd=st.server / "repo" / f"{repo}.git").splitlines() if run_id else []
    return {ref.rsplit("/", 1)[1]: sha for ref, sha in (line.split() for line in lines)}


def running(st: Stand) -> list[str]:
    where = st.state / "runner" / "идут"
    return sorted(os.listdir(where)) if where.is_dir() else []


def answer_json(done: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(done.stdout)
    except ValueError:
        return {}


def broken(stand: Stand, done: subprocess.CompletedProcess) -> tuple:
    line, reason, code = fallback_reason(stand, done)
    return (line, bool(reason) and reason.startswith("итог runner битый"), code)


def main() -> int:
    cases = Cases()
    check = cases.check
    roots: list[Path] = []
    try:
        # (а) дверь закрыта пустым REMOTE_RUNNER: ни одного ssh, вывод пуст.
        root, st = with_stand(runner="")
        roots.append(root)
        done = st.run("готов")
        check("(а) REMOTE_RUNNER пуст — ssh не звался, вывод пуст",
              ([], "", "", []), (st.calls(), done.stdout, done.stderr, st.events()))

        # (б) окружение называет чужой паспорт и чужой адрес — дверь берёт адрес паспорта стенда.
        root, st = with_stand()
        roots.append(root)
        evil = root / "evil"
        evil.mkdir()
        (evil / "harness.conf").write_text(f'REMOTE_RUNNER="{FOREIGN}"\nRUNNER_KNOWN_HOSTS=""\n',
                                           encoding="utf-8")
        (evil / "install.conf").write_text(f'PROJECT_DIR="{evil}"\n', encoding="utf-8")
        st.env.update(HARNESS_HOME=str(evil), HARNESS_CONF=str(evil / "harness.conf"),
                      HARNESS_INSTALL_CONF=str(evil / "install.conf"), REMOTE_RUNNER=FOREIGN,
                      RUNNER_KNOWN_HOSTS="", RUNNER_SSH_KEY=str(evil / "key"))
        done = st.run("готов")
        calls = st.calls()
        check("(б) HARNESS_HOME со своим REMOTE_RUNNER — ssh на адрес паспорта стенда-двери",
              (True, True, False, 0),
              (bool(calls), all(ADDRESS in c for c in calls),
               any(FOREIGN in " ".join(c) for c in calls), done.returncode))
        argv = calls[0] if calls else []

        def option(name: str) -> str:
            pairs = [argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "-o"]
            return next((p.split("=", 1)[1] for p in pairs if p.split("=", 1)[0] == name), "")

        check("(б) ssh: ключ хоста закреплён, чужой конфиг и агент ssh не участвуют",
              ("yes", str(st.known_hosts), "yes", True, True, "auto",
               str(st.state / "runner" / "cm" / "%C")),
              (option("StrictHostKeyChecking"), option("UserKnownHostsFile"), option("IdentitiesOnly"),
               "-F" in argv and argv[argv.index("-F") + 1] == "/dev/null",
               "-i" in argv and argv[argv.index("-i") + 1] == str(st.key),
               option("ControlMaster"), option("ControlPath")))
        check("(б) готов: ответ runner — JSON в выводе, событий падения нет",
              (expected_entry_sha(), []),
              (answer_sha(done.stdout),
               [e for e in st.events() if e.get("event") == "runner.fallback"]))
        cm = st.state / "runner" / "cm"
        check("(б) каталог сокетов ssh — права 700", "0o700",
              oct(cm.stat().st_mode & 0o777) if cm.is_dir() else "нет каталога")

        # (в) ssh код 255 — строка в выводе и событие в журнале runner стенда.
        root, st = with_stand()
        roots.append(root)
        st.scenario(code=255, stderr="ssh: connect to host stand.example port 22: Connection refused\n")
        done = st.run("готов")
        line, reason, code = fallback_reason(st, done)
        check("(в) ssh код 255 — «runner недоступен: …» в выводе и runner.fallback в журнале",
              (True, True, 10), (line, bool(reason) and "255" in reason, code))

        # (г) чужой sha_входа — «вход устарел».
        root, st = with_stand()
        roots.append(root)
        st.scenario(stdout=json.dumps(st.ready(sha_входа="0" * 64)))
        line, reason, code = fallback_reason(st, st.run("готов"))
        check("(г) готов с чужим sha_входа — причина «вход устарел»",
              (True, True, 10), (line, bool(reason) and reason.startswith("вход устарел"), code))

        # (г2) входа рядом с клиентом нет — сравнить не с чем: недоступен, ssh не звался.
        root, st = with_stand(entry=False)
        roots.append(root)
        line, reason, code = fallback_reason(st, st.run("готов"))
        check("(г2) нет harness/runner/runner-entry — «вход не найден», ssh не звался",
              (True, True, 10, []),
              (line, bool(reason) and reason.startswith("вход не найден"), code, st.calls()))

        # (д) свободно меньше RUNNER_MIN_FREE_GB.
        root, st = with_stand()
        roots.append(root)
        st.scenario(stdout=json.dumps(st.ready(свободно_гб=5)))
        line, reason, code = fallback_reason(st, st.run("готов"))
        check("(д) свободно 5 ГБ < RUNNER_MIN_FREE_GB 20 — недоступен",
              (True, True, 10), (line, bool(reason) and reason.startswith("свободно"), code))

        # (д2) нет git на runner — недоступен.
        root, st = with_stand()
        roots.append(root)
        st.scenario(stdout=json.dumps(st.ready(версии={"git": "", "python3": "3.12.3", "bash": "5.2"})))
        line, reason, code = fallback_reason(st, st.run("готов"))
        check("(д2) на runner нет git — недоступен", (True, "нет git", 10), (line, reason, code))

        # (е) url.X.insteadOf в конфиге проекта стенда — отказ до ssh.
        root, st = with_stand()
        roots.append(root)
        git("config", "--local", "url.ssh://evil.example/.insteadOf", "ssh://runner@stand.example/",
            cwd=st.project)
        line, reason, code = fallback_reason(st, st.run("готов"))
        check("(е) url.X.insteadOf в .git/config стенда — отказ, ssh не звался",
              (True, True, 10, []),
              (line, bool(reason) and "url." in reason, code, st.calls()))

        # (ж) файла known_hosts нет — дверь закрыта.
        root, st = with_stand(known_hosts=False)
        roots.append(root)
        done = st.run("готов")
        check("(ж) нет файла known_hosts — дверь закрыта, ssh не звался",
              ([], 10), (st.calls(), done.returncode))

        # (з) ssh молчит дольше RUNNER_SSH_TIMEOUT_SEC — недоступен, а не висит.
        # Короткий предел — только здесь: остальным случаям 2 с под нагрузкой не хватает на настоящий вход.
        root, st = with_stand(ssh_timeout="2")
        roots.append(root)
        st.scenario(sleep=5, stdout=json.dumps(st.ready()))
        line, reason, code = fallback_reason(st, st.run("готов"))
        check("(з) ssh молчит дольше предела — «не ответил»", (True, True, 10),
              (line, bool(reason) and "не ответил" in reason, code))

        # (и) отвечает НАСТОЯЩИЙ вход runner (стендовая дверь): ключи его ответа клиент понимает.
        # Поломка, которую ловит: клиент и вход назвали поля «готов» по-разному — каждый зелёный
        # на своей копии формы, а живой runner всегда «недоступен».
        # Порог выше нуля: при нуле ответ без «свободно_гб» читался бы как 0 и проходил.
        root, st = with_stand(min_free="0.001")
        roots.append(root)
        server = root / "server"
        (server / "code").mkdir(parents=True)
        (server / "conf").mkdir()
        for name, body in RUNNER_FILES.items():
            (server / "code" / name).write_bytes(body)
        shutil.copy2(RUNNER_CONF, server / "conf" / "runner.yaml")
        st.scenario(entry=str(server / "code" / "runner-entry"), stand_root=str(server))
        done = st.run("готов")
        check("(и) ответ настоящего входа «готов» принят: код 0, sha_входа совпал, падений нет",
              (0, expected_entry_sha(), []),
              (done.returncode, answer_sha(done.stdout),
               [e.get("data") for e in st.events() if e.get("event") == "runner.fallback"]))

        # (и2) ответ «готов» без «свободно_гб» или с нечислом — недоступен, а не «свободно 0».
        # Порог 0 нарочно: отсутствие ключа обязано отказать само, а не через сравнение с порогом.
        for label, answer in (("нет ключа", {k: v for k, v in Stand.ready().items()
                                             if k != "свободно_гб"}),
                              ("нечисло", Stand.ready(свободно_гб="много"))):
            root, st = with_stand(min_free="0")
            roots.append(root)
            st.scenario(stdout=json.dumps(answer, ensure_ascii=False))
            line, reason, code = fallback_reason(st, st.run("готов"))
            check(f"(и2) «свободно_гб»: {label} — недоступен, причина названа, код 10",
                  (True, True, 10), (line, bool(reason) and "свободно_гб" in reason, code))

        # (и3) ответ — JSON, но не объект: падение на локальный, а не трассировка с кодом 1.
        for label, body in (("список", "[]"), ("строка", '"готов"')):
            root, st = with_stand()
            roots.append(root)
            st.scenario(stdout=body)
            done = st.run("готов")
            line, reason, code = fallback_reason(st, done)
            check(f"(и3) ответ «готов» — {label} — недоступен, код 10, без трассировки",
                  (True, True, 10, False),
                  (line, bool(reason), code, "Traceback" in done.stderr))

        # (к-ссылки) в harness/runner/ master есть подкаталог и символьная ссылка: хеш клиента
        # берёт только обычные файлы верхнего уровня — как entry_sha_dir на runner.
        root, st = with_stand()
        roots.append(root)
        (st.project / "harness" / "runner" / "sub").mkdir()
        (st.project / "harness" / "runner" / "sub" / "x").write_text("вложенный\n", encoding="utf-8")
        (st.project / "harness" / "runner" / "link").symlink_to("runner-entry")
        git("add", "-A", cwd=st.project)
        git("commit", "-q", "-m", "подкаталог и ссылка", cwd=st.project)
        done = st.run("готов")
        check("(к-ссылки) подкаталог и ссылка в harness/runner/ — sha_входа как у входа, код 0",
              (0, []), (done.returncode,
                        [e.get("data") for e in st.events() if e.get("event") == "runner.fallback"]))
        # ---- T4: прогон целиком (вход и runner-job настоящие, тело — FAKE_BODY) ----

        # (к) ворота: три sha проекта и HEAD пакета уезжают push на refs/runs/<id>/*, итог читается.
        # Зелёной оставит: ничего из «push не туда» — ссылка не на месте краснеет; клиент, что не
        # снимает идут/<id> после итога, краснеет последней строкой.
        root, st = with_server()
        roots.append(root)
        done, out = gates(st)
        ids = verb_ids(st, "прогнать")
        rid = ids[-1] if ids else ""
        answer = answer_json(done)
        check("(к) ворота: итог runner — код 0, машина runner, исход готов",
              (0, "runner", "готов"), (done.returncode, answer.get("машина"), answer.get("исход")))
        check("(к) push: refs/runs/<id>/{основа,предмет,база} проекта и refs/runs/<id>/пакет пакета",
              (st.shas, {"пакет": git("rev-parse", "HEAD", cwd=st.package)}),
              (run_refs(st, "project", rid), run_refs(st, "package", rid)))
        job = received(st, rid)
        check("(к) задание несёт отправленные sha; ssh только на адрес паспорта; push — в project.git",
              ({**st.shas, "пакет": git("rev-parse", "HEAD", cwd=st.package)}, True, True),
              ({key: job.get(key) for key in ("основа", "предмет", "база", "пакет")},
               all(ADDRESS in c for c in st.calls()),
               any(c[-1] == f"git-receive-pack '{PROJECT_REPO}'" for c in st.calls())))
        summary = out / "итог.json"
        check("(к) итог распакован в --итог-в, идут/ пуст, падений нет",
              (st.shas["основа"], [], []),
              (json.loads(summary.read_text(encoding="utf-8")).get("основа") if summary.is_file() else None,
               running(st), [e for e in st.events() if e.get("event") == "runner.fallback"]))

        # (л) снимок дерева (пробы): только отслеживаемое с незакоммиченной правкой, общий индекс цел.
        # Зелёной оставит: снимок `add -A` с .env в .gitignore стенда — здесь .gitignore нет, ловится;
        # снимок, что пишет общий индекс (`git add -u` без GIT_INDEX_FILE), меняет статус « M» → «M ».
        root, st = with_server()
        roots.append(root)
        (st.project / "README").write_text("стенд, правка\n", encoding="utf-8")
        (st.project / ".env").write_text("ENV_PROBE=1\n", encoding="utf-8")
        before = git("status", "--porcelain", cwd=st.project)
        done = probes(st)
        after = git("status", "--porcelain", cwd=st.project)
        ids = verb_ids(st, "прогнать")
        job = received(st, ids[-1]) if ids else {}
        head = git("rev-parse", "HEAD", cwd=st.project)
        subject = job.get("предмет", "")
        bare = st.server / "repo" / "project.git"
        names = git("ls-tree", "-r", "--name-only", subject, cwd=bare).splitlines() if subject else []
        check("(л) снимок: основа = HEAD, предмет — новый коммит, README с правкой, .env нет, код 0",
              (head, True, "стенд, правка", False, 0),
              (job.get("основа"), bool(subject) and subject != head,
               git("show", f"{subject}:README", cwd=bare) if "README" in names else None,
               ".env" in names, done.returncode))
        check("(л) git status --porcelain дерева до и после снимка равен (прогон был)",
              (before, True), (after, bool(ids)))

        # (м) отслеживаемая строка формы токена — «снимок с секретом», ни push, ни «прогнать».
        # Зелёной оставит: суд секретов по HEAD, а не по диапазону HEAD..<снимок> (правка не в HEAD — ловится).
        root, st = with_server()
        roots.append(root)
        (st.project / "README").write_text("api" + "_key = \"" + "Zx81qLm" + "42pR9\"\n", encoding="utf-8")
        line, reason, code = fallback_reason(st, probes(st))
        check("(м) снимок с формой токена — «снимок с секретом», прогона и push нет",
              (True, "снимок с секретом", 10, [], False),
              (line, reason, code, verb_ids(st, "прогнать"),
               any(c and c[-1].startswith("git-receive-pack") for c in st.calls())))

        # (н) пакет с незакоммиченной правкой — локально «пакет не закоммичен».
        # Зелёной оставит: проверка «грязи» только по неотслеживаемым (здесь правка отслеживаемого).
        root, st = with_server()
        roots.append(root)
        (st.package / "VERSION").write_text("пакет, правка\n", encoding="utf-8")
        done, _ = gates(st)
        line, reason, code = fallback_reason(st, done)
        check("(н) пакет с правкой — «пакет не закоммичен», «прогнать» не звался",
              (True, "пакет не закоммичен", 10, []), (line, reason, code, verb_ids(st, "прогнать")))

        # (о) архив итога с чужой записью — «итог runner битый», вне каталога пусто.
        # Зелёной оставит: только extractall(filter="data") — он пропускает ссылки ВНУТРЬ каталога,
        # поэтому ссылки здесь указывают на итог.json (их обязан отказать свой отказ, §5.1).
        root, st = with_server()
        roots.append(root)
        for label, spec in (("../x", {"name": "../x"}), ("/etc/x", {"name": "/etc/x"}),
                            ("символьная ссылка", {"name": "ссылка", "type": "sym", "link": "итог.json"}),
                            ("жёсткая ссылка", {"name": "жёсткая", "type": "hard", "link": "итог.json"})):
            serve(st, итог={"append": spec})
            done, out = gates(st)
            check(f"(о) архив итога с {label} — «итог runner битый», вне каталога ничего",
                  (True, True, 10, []),
                  (*broken(st, done), sorted(str(p) for p in root.rglob("x"))))

        # (п) основа итога не равна отправленной — битый, а не зелёный.
        # Зелёной оставит: сверка только id итога; или сверка основы с той, что вернул сам итог.
        root, st = with_server()
        roots.append(root)
        set_body(st, итог={"основа": "0" * 40})
        done, _ = gates(st)
        check("(п) основа итога ≠ отправленной — «итог runner битый»", (True, True, 10), broken(st, done))

        # (р) итог больше RUNNER_RESULT_MAX_MB — битый.
        # Зелёной оставит: предел, читаемый из среды или с умолчанием 200 вместо паспорта стенда.
        root, st = with_server(conf_extra='RUNNER_RESULT_MAX_MB="1"\n')
        roots.append(root)
        set_body(st, big_mb=2)
        done, _ = gates(st)
        check("(р) итог 2 МБ при RUNNER_RESULT_MAX_MB=1 — «итог runner битый»",
              (True, True, 10), broken(st, done))

        # (с) опрос не отвечает дольше RUNNER_LOST_SEC — отменить тот id, «связь потеряна на прогоне».
        # Спека §3.1 п. 7: «опрос не отвечает» (план T4 пишет «идёт» — случай (с2) держит разницу).
        # Зелёной оставит: ничего из «не отменил» — список отмен сверяется с id прогона.
        root, st = with_server(conf_extra='RUNNER_LOST_SEC="2"\n')
        roots.append(root)
        set_body(st, sleep=30)
        serve(st, состояние={"code": 255, "stderr": "ssh: connection to stand.example lost\n"})
        started = time.monotonic()
        line, reason, code = fallback_reason(st, gates(st)[0])
        ids = verb_ids(st, "прогнать")
        check("(с) опрос молчит дольше RUNNER_LOST_SEC — отменить <id>, «связь потеряна на прогоне»",
              (True, "связь потеряна на прогоне", 10, True, [], True),
              (line, reason, code, bool(ids) and verb_ids(st, "отменить") == ids, running(st),
               time.monotonic() - started < 25))

        # (с2) прогон отвечает «идёт» дольше RUNNER_LOST_SEC — это не потеря связи: итог дождан.
        # Зелёной оставит: ничего из «снимает долгий прогон» — отмена видна в вызовах.
        root, st = with_server(conf_extra='RUNNER_LOST_SEC="2"\n')
        roots.append(root)
        set_body(st, sleep=4)
        done, _ = gates(st)
        check("(с2) «идёт» 4 с при RUNNER_LOST_SEC=2 — итог дождан, отмены нет",
              (0, "готов", []), (done.returncode, answer_json(done).get("исход"), verb_ids(st, "отменить")))

        # (т) клиент убит SIGKILL после «прогнать» — следующий запуск зовёт отменить с тем id.
        # Зелёной оставит: id в идут/ пишется ПОСЛЕ «прогнать» и SIGKILL успел раньше — здесь клиент
        # убит, когда задание уже в jobs/, а файл идут/ проверяется отдельно.
        root, st = with_server()
        roots.append(root)
        set_body(st, sleep=30)
        client = subprocess.Popen(
            [sys.executable, str(st.tree / "scripts" / "remote-run.py"), "ворота", "--основа", st.shas["основа"],
             "--предмет", st.shas["предмет"], "--база", st.shas["база"], "--ступени",
             json.dumps(GATE_STEPS, ensure_ascii=False), "--итог-в", str(root / "итог-убитого")],
            env=st.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and client.poll() is None:
            ids = verb_ids(st, "прогнать")
            if ids and (st.server / "jobs" / ids[0]).exists():
                break
            time.sleep(0.2)
        orphan_seen = running(st)
        client.send_signal(signal.SIGKILL)
        client.wait()
        ids = verb_ids(st, "прогнать")
        orphan = ids[0] if ids else ""
        set_body(st)
        gates(st)
        check("(т) после SIGKILL: идут/<id> был, следующий запуск — отменить <id>, файл снят",
              ([orphan] if orphan else ["<прогона не было>"], True, False),
              (orphan_seen, bool(orphan) and orphan in verb_ids(st, "отменить"),
               bool(orphan) and (st.state / "runner" / "идут" / orphan).exists()))

        # (у) тело дольше потолка — код 11, «не дошли», локального прогона нет.
        # Зелёной оставит: «предел» как битый итог (код 10) — ловится кодом и строкой «прогон локально».
        root, st = with_server()
        roots.append(root)
        set_body(st, sleep=30)
        steps = [{**step, "потолок_с": 1} for step in GATE_STEPS]
        done, _ = gates(st, steps)
        answer = answer_json(done)
        check("(у) тело дольше потолка — код 11, причина «не дошли», исход предел, локально не идёт",
              (11, "не дошли", "предел", False, []),
              (done.returncode, answer.get("причина"), answer.get("исход"), "прогон локально" in done.stderr,
               [e for e in st.events() if e.get("event") == "runner.fallback"]))

        # (ф) паспорт и среда задания (§6.2).
        # Зелёной оставит: паспорт без вычёркивания — краснеет множеством ключей; среда целиком из
        # os.environ — краснеет списком лишних; значение с \n, которое runner-job отказал бы, —
        # проба читает задание, ПРИСЛАННОЕ клиентом, а не принятое runner-job.
        root, st = with_server(conf_extra='TG_TOKEN_FILE="/etc/harness/tg"\n'
                                          'EVOLUTION_URL="http://evolution.example"\n'
                                          'PUBLIC_REMOTE="git@github.com:x/y.git"\nPLAIN_PROBE="как есть"\n')
        roots.append(root)
        st.env.update(HARNESS_PROBE_ENV="есть", STRANGE_PROBE_KEY="нет", MUTATION_PROBE_NL="a\nb")
        gates(st)
        ids = verb_ids(st, "прогнать")
        job = received(st, ids[-1]) if ids else {}
        sent = job.get("паспорт", {})
        got = {**config.из_текста(sent.get("install.conf", "")), **config.из_текста(sent.get("harness.conf", ""))}
        ours = {**config.из_файла(str(st.home / "install.conf")), **config.из_файла(str(st.home / "harness.conf"))}
        strike = yaml.safe_load(RUNNER_CONF.read_text(encoding="utf-8"))["паспорт"]["вычеркнуть"]
        kept = {key for key in ours if not any(fnmatch.fnmatchcase(key, pattern) for pattern in strike)}
        check("(ф) паспорт задания = паспорт стенда минус паспорт.вычеркнуть, REMOTE_RUNNER=\"\"",
              (sorted(kept | {"REMOTE_RUNNER"}), "", "как есть"),
              (sorted(set(got) - {"SECRETS_DIR", "AGENT_USER"}), got.get("REMOTE_RUNNER"), got.get("PLAIN_PROBE")))
        boxed = {"PROJECT_DIR": "/w/project", "STARTER_DIR": "/w/package", "LOG_DIR": "/var/log/harness",
                 "STATE_DIR": "/var/lib/harness"}
        check("(ф) пути паспорта — пути коробки; паспорт.sha — sha256",
              (boxed, True), ({key: got.get(key) for key in boxed},
                              bool(re.fullmatch(r"[0-9a-f]{64}", str(sent.get("sha", ""))))))
        env = job.get("среда", {})
        check("(ф) среда: белый ключ уехал; ключ вне списка, HARNESS_HOME, дверь стенда, значение с \\n — нет",
              ("есть", []),
              (env.get("HARNESS_PROBE_ENV"),
               [key for key in ("STRANGE_PROBE_KEY", "HARNESS_HOME", "HARNESS_DOOR_STAND_HOME",
                                "MUTATION_PROBE_NL") if key in env]))
    finally:
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)

    print(f"test_remote_run: {'зелёный' if cases.ok else 'КРАСНЫЙ'} ({cases.paths} случаев, "
          f"ПЛОХО {cases.failures})")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
