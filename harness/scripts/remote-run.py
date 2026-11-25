#!/usr/bin/env python3
"""remote-run.py — клиент runner прогонов на втором сервере: дверь, проверка готовности, падение на локальный.

Спека runner прогонов на втором сервере: §3.1 п. 1, 3–11; §4.2–4.4; §5.1; §6.1, §6.2.

Вся клиентская логика runner живёт здесь: очередь, приёмка и пул тяжёлых прогонов только зовут
клиент и читают его итог. Ключи REMOTE_RUNNER, RUNNER_* берутся в режиме двери — из паспорта
машины (config.корень_двери): окружение назвало бы клиенту чужой адрес и чужой ключ хоста, а
клиент отправляет туда код проекта.

    remote-run.py готов
        0 — runner готов, ответ входа (JSON) в stdout;
        10 — дверь закрыта (тихо) или runner недоступен (строка «runner недоступен: <причина> —
             прогон локально» в stderr и событие runner.fallback в журнале runner).

    remote-run.py ворота --основа <sha> --предмет <sha> --база <sha> --ступени <json> --итог-в <каталог>
    remote-run.py пробы|мутации --дерево <каталог> --ступени <json> --итог-в <каталог>
    remote-run.py сироты
        Ступень — {"имя", "потолок_с", …}: потолок клиент переносит в задание.потолки_с.
        0 — итог получен и сверен, в stdout JSON {"машина": "runner", "исход", "ступени"}, итог
            распакован в --итог-в; 10 — как у `готов` (вызывающий идёт локально); 11 — потолок
            вышел («не дошли», исход «предел»): локально НЕ перегонять, это удвоило бы потолок.
        «пробы» и «мутации» берут снимок дерева (основа = HEAD дерева, предмет = снимок отслеживаемых
        файлов); «база» им не нужна — runner-job требует sha, шлём основу.

    паспорт.sha — sha256 канонического JSON (sort_keys, ensure_ascii=False) объекта
    {"паспорт": {"install.conf": текст, "harness.conf": текст}, "среда": {…}} задания.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import NamedTuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import config  # noqa: E402
import events  # noqa: E402
import git_redirect  # noqa: E402

SRC = "scripts/remote-run.py"
UNAVAILABLE = 10
LIMIT = 11
RUNNER_DIR = "harness/runner"
ENTRY = f"{RUNNER_DIR}/runner-entry"


class Door(NamedTuple):
    address: str
    key: str
    known_hosts: str
    project: Path
    package: Path
    state_dir: Path
    values: dict


class Unavailable(Exception):
    """Причина, по которой прогон уходит локально."""


def passport() -> tuple[str, dict]:
    root = config.корень_двери(__file__)
    return root, {**config.из_файла(f"{root}/install.conf"), **config.из_файла(f"{root}/harness.conf")}


def door() -> Door | None:
    """Дверь из паспорта машины; None — закрыта: адреса нет или ключ хоста не закреплён."""
    root, values = passport()
    address = values.get("REMOTE_RUNNER", "")
    known_hosts = values.get("RUNNER_KNOWN_HOSTS", "")
    if not address or not known_hosts or not Path(known_hosts).is_file():
        return None
    home = Path(os.environ.get("HOME") or "/root")
    agent = values.get("AGENT_USER") or "agent"
    return Door(address=address,
                key=values.get("RUNNER_SSH_KEY") or str(home / ".ssh" / "runner_ed25519"),
                known_hosts=known_hosts,
                project=Path(values.get("PROJECT_DIR", "")),
                package=Path(values.get("STARTER_DIR") or f"/home/{agent}/starter/STARTER-PACKAGE"),
                state_dir=Path(config._места(root, values.get("STATE_DIR", ""))["STATE_DIR"]),
                values=values)


def ssh_options(d: Door) -> list[str]:
    """Доводы ssh без адреса и команды: их же берёт GIT_SSH_COMMAND, чтобы push шёл тем же закреплённым путём.

    Одно держимое соединение на все вызовы прогона: новое рукопожатие под нагрузкой стоит 3,6 с.
    """
    sockets = d.state_dir / "runner" / "cm"
    sockets.mkdir(parents=True, exist_ok=True)
    sockets.chmod(0o700)
    return ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={d.known_hosts}", "-o", "IdentitiesOnly=yes", "-i", d.key,
            "-o", "ControlMaster=auto", "-o", "ControlPersist=600", "-o", f"ControlPath={sockets}/%C"]


def ssh_argv(d: Door, *command: str) -> list[str]:
    """ssh без чужого конфига и агента: ключ хоста только закреплённый, ключ входа только свой."""
    return [*ssh_options(d), d.address, *command]


def fallback(reason: str) -> int:
    """Уйти локально НЕ молча: строка вызывающему и событие в журнал runner."""
    print(f"runner недоступен: {reason} — прогон локально", file=sys.stderr)
    events.emit(SRC, "runner.fallback", "skipped", journal="runner", data={"причина": reason})
    return UNAVAILABLE


def entry_sha_function():
    """entry_sha входа runner — по пути файла, не копией: одна реализация на клиент и runner."""
    path = config.корень_дерева(__file__) / ENTRY
    if not path.is_file():
        raise Unavailable(f"вход не найден: {path}")
    loader = importlib.machinery.SourceFileLoader("runner_entry", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("runner_entry", loader))
    loader.exec_module(module)
    return module.entry_sha


def local_entry_sha(project: Path) -> str:
    """Хеш harness/runner/* ЗДЕШНЕГО master: runner ставится из master, не из рабочего дерева."""
    def git(*args: str) -> bytes:
        done = subprocess.run(["git", "-C", str(project), *args], capture_output=True)
        if done.returncode:
            raise Unavailable(f"git {args[0]} в {project}: код {done.returncode}: "
                              f"{done.stderr.decode(errors='replace').strip()[:200]}")
        return done.stdout
    # Вход хеширует только обычные файлы каталога: подкаталог или ссылка разошлись бы с ним навсегда.
    entries = [e.split("\t", 1) for e in git("ls-tree", "-z", "master", f"{RUNNER_DIR}/").decode().split("\0") if e]
    names = [name for meta, name in entries if meta.split()[:2] in (["100644", "blob"], ["100755", "blob"])]
    files = {Path(name).name: git("show", f"master:{name}") for name in names}
    return entry_sha_function()(files)


def refuse_redirects(*repos: Path) -> None:
    for repo in dict.fromkeys(repos):
        if not (repo / ".git").exists():
            continue
        found = git_redirect.redirects(repo)
        if found:
            raise Unavailable(f"в конфиге {repo} заданы {', '.join(found)}")


def ask_ready(d: Door) -> dict:
    timeout = int(d.values.get("RUNNER_SSH_TIMEOUT_SEC") or 10)
    try:
        done = subprocess.run(ssh_argv(d, "готов"), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Unavailable(f"ssh не ответил за {timeout} с") from None
    if done.returncode:
        last = (done.stderr.strip().splitlines() or ["?"])[-1][:200]
        raise Unavailable(f"ssh код {done.returncode}: {last}")
    try:
        answer = json.loads(done.stdout)
    except ValueError:
        raise Unavailable(f"ответ «готов» не JSON: {done.stdout[:80]!r}") from None
    if not isinstance(answer, dict):
        raise Unavailable(f"ответ «готов» не объект: {done.stdout[:80]!r}")
    return answer


def judge_ready(answer: dict, d: Door, expected_sha: str) -> None:
    versions = answer.get("версии") or {}
    for tool in ("git", "python3"):
        if not versions.get(tool):
            raise Unavailable(f"нет {tool}")
    # Нет ключа читалось бы как 0 и при пороге 0 проходило: договор ключей судим явно.
    free = answer.get("свободно_гб")
    if isinstance(free, bool) or not isinstance(free, (int, float)):
        raise Unavailable(f"нет числа свободно_гб в ответе: {free!r}")
    floor = float(d.values.get("RUNNER_MIN_FREE_GB") or 20)
    if free < floor:
        raise Unavailable(f"свободно {free:g} ГБ < RUNNER_MIN_FREE_GB {floor:g}")
    if answer.get("sha_входа") != expected_sha:
        raise Unavailable(f"вход устарел: sha_входа {str(answer.get('sha_входа'))[:12]} ≠ master {expected_sha[:12]}")


def ready(d: Door) -> dict:
    """Ответ входа runner, если по нему можно гнать прогон; иначе Unavailable с причиной."""
    refuse_redirects(d.project, d.package)
    expected = local_entry_sha(d.project)
    answer = ask_ready(d)
    judge_ready(answer, d, expected)
    return answer


def cmd_ready() -> int:
    d = door()
    if d is None:
        return UNAVAILABLE
    try:
        answer = ready(d)
    except (Unavailable, OSError) as why:
        return fallback(str(why))
    print(json.dumps(answer, ensure_ascii=False))
    return 0


# ---- прогон: снимок, отправка, задание, опрос, итог (спека §3.1 п. 5–11, §4.2, §4.3, §5.1, §6) ----

PUSH_TIMEOUT_SEC = 300
RESULT_READ_TIMEOUT_SEC = 300
SNAPSHOT_IDENTITY = {"GIT_AUTHOR_NAME": "runner-snapshot", "GIT_AUTHOR_EMAIL": "runner-snapshot@localhost",
                     "GIT_COMMITTER_NAME": "runner-snapshot", "GIT_COMMITTER_EMAIL": "runner-snapshot@localhost"}


class Request(NamedTuple):
    kind: str
    tree: Path | None  # None — дерево проекта из паспорта (ворота)
    base_commit: str
    subject: str       # "" — взять снимок дерева
    range_base: str
    stages: list
    out_dir: Path


def number(values: dict, key: str, default: int) -> int:
    return int(values.get(key) or default)


def clean_git_env(**extra: str) -> dict:
    """Окружение git без GIT_* вызывающего (индекс и каталог хука очереди) и без глобального конфига:
    глобальный url.X.insteadOf увёл бы push на чужой адрес, а клиент шлёт туда код проекта."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", **extra)
    return env


def git_out(repo: Path, *args: str, env: dict | None = None) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=env or clean_git_env())
    if done.returncode:
        raise Unavailable(f"git {args[0]} в {repo}: код {done.returncode}: {done.stderr.strip()[:200]}")
    return done.stdout.strip()


def take_snapshot(tree: Path) -> str:
    """Коммит снимка ОТСЛЕЖИВАЕМЫХ файлов дерева, ни на какой ветке.

    Временный индекс: общий индекс и stash общего дерева помощников не трогаются, а неотслеживаемое
    (.env) в снимок не попадает, даже если не названо в .gitignore."""
    with tempfile.TemporaryDirectory(prefix="mq-runner-index-") as tmp:
        env = clean_git_env(GIT_INDEX_FILE=f"{tmp}/index", **SNAPSHOT_IDENTITY)
        git_out(tree, "read-tree", "HEAD", env=env)
        git_out(tree, "add", "-u", env=env)
        written = git_out(tree, "write-tree", env=env)
        return git_out(tree, "commit-tree", written, "-p", "HEAD", "-m", "снимок дерева для runner", env=env)


def check_secrets(*args: str, cwd: Path | None = None) -> bool:
    checker = config.корень_дерева(__file__) / "scripts" / "check-secrets.sh"
    return subprocess.run(["bash", str(checker), *args], capture_output=True, cwd=cwd,
                          env=clean_git_env()).returncode == 0


def package_head(d: Door) -> str:
    """HEAD пакета; грязный пакет — отказ: его в это время пишет sync_package, снимать с него нельзя."""
    if not (d.package / ".git").exists():
        raise Unavailable(f"пакет не найден: {d.package}")
    if git_out(d.package, "status", "--porcelain"):
        raise Unavailable("пакет не закоммичен")
    return git_out(d.package, "rev-parse", "HEAD")


def push_refs(d: Door, repo: Path, name: str, refs: dict[str, str]) -> None:
    """push по АДРЕСУ паспорта (не по имени удалённого), хуки выключены; refs: имя ссылки → sha."""
    url = f"ssh://{d.address}/srv/runner/repo/{name}.git"
    env = clean_git_env(GIT_SSH_COMMAND=shlex.join(ssh_options(d)))
    argv = ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "push", "-q", url,
            *(f"{sha}:{ref}" for ref, sha in refs.items())]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=PUSH_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        raise Unavailable(f"отправка версии в {name}: не уложилась в {PUSH_TIMEOUT_SEC} с") from None
    if done.returncode:
        last = (done.stderr.strip().splitlines() or ["?"])[-1][:200]
        raise Unavailable(f"отправка версии в {name}: код {done.returncode}: {last}")


def new_run_id() -> str:
    host = re.sub(r"[^A-Za-z0-9]+", "-", socket.gethostname()).strip("-") or "host"
    return f"{host}-{os.getpid()}-{time.time_ns()}"


def runner_conf() -> dict:
    import yaml
    path = config.корень_дерева(__file__) / "harness" / "config" / "runner.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def passport_texts() -> dict:
    """Паспорт задания: наши install.conf и harness.conf без вычеркнутых ключей и с путями коробки."""
    root, _ = passport()
    conf = runner_conf()
    strike, boxed = conf["паспорт"]["вычеркнуть"], conf["коробка"]
    texts = {}
    for name in ("install.conf", "harness.conf"):
        values = config.из_файла(f"{root}/{name}")
        kept = {k: v for k, v in values.items() if not any(fnmatch.fnmatchcase(k, p) for p in strike)}
        if name == "harness.conf":
            kept["REMOTE_RUNNER"] = ""
        kept = {k: boxed.get(k, v) for k, v in kept.items()}
        texts[name] = "".join(f'{k}="{v}"\n' for k, v in kept.items())
    present = {k for text in texts.values() for k in config.из_текста(text)}
    texts["install.conf"] += "".join(f'{k}="{v}"\n' for k, v in boxed.items() if k not in present)
    return texts


def task_environment() -> dict:
    """Из окружения клиента уезжает только белый список runner.yaml; значение с переводом строки — нет."""
    rules = runner_conf()["среда"]
    return {name: value for name, value in sorted(os.environ.items())
            if name not in rules["запрет"] and any(fnmatch.fnmatchcase(name, p) for p in rules["шаблоны"])
            and "\n" not in value and "\0" not in value}


def build_passport(env: dict) -> dict:
    texts = passport_texts()
    with tempfile.TemporaryDirectory(prefix="mq-runner-passport-") as tmp:
        for name, text in texts.items():
            (Path(tmp) / name).write_text(text, encoding="utf-8")
        if not check_secrets(tmp):
            raise Unavailable("паспорт с секретом")
    canonical = json.dumps({"паспорт": texts, "среда": env}, sort_keys=True, ensure_ascii=False)
    return {**texts, "sha": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def build_job(d: Door, req: Request, run_id: str, subject: str, package_sha: str) -> dict:
    env = task_environment()
    missing = [step["имя"] for step in req.stages if "потолок_с" not in step]
    if missing:
        raise SystemExit(f"remote-run: у ступеней {missing} нет потолок_с")
    return {"id": run_id, "вид": req.kind, "основа": req.base_commit, "предмет": subject,
            "база": req.range_base, "пакет": package_sha,
            "ступени": [{k: v for k, v in step.items() if k != "потолок_с"} for step in req.stages],
            "потолки_с": {step["имя"]: step["потолок_с"] for step in req.stages},
            "ждать_места_с": number(d.values, "RUNNER_WAIT_SEC", 120),
            "среда": env, "паспорт": build_passport(env), "семя": {}}


def ssh_call(d: Door, *command: str, data: str | None = None, timeout: float):
    """Ответ входа runner или None, если ssh не уложился в предел."""
    try:
        return subprocess.run(ssh_argv(d, *command), input=data, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def pending_dir(d: Door) -> Path:
    return d.state_dir / "runner" / "идут"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cancel(d: Door, run_id: str) -> None:
    """Снять прогон на runner. Файл идут/<id> снимается, только если runner ответил (код 1 — юнита нет)."""
    done = ssh_call(d, f"отменить {run_id}", timeout=number(d.values, "RUNNER_SSH_TIMEOUT_SEC", 10))
    if done is not None and done.returncode in (0, 1):
        (pending_dir(d) / run_id).unlink(missing_ok=True)


def cancel_orphans(d: Door) -> None:
    """Сироты: id, чей pid здесь не жив, — отменить на runner. Каждый запуск, кроме `готов`."""
    where = pending_dir(d)
    for name in sorted(os.listdir(where)) if where.is_dir() else []:
        parts = name.rsplit("-", 2)
        if len(parts) == 3 and parts[1].isdigit() and not pid_alive(int(parts[1])):
            cancel(d, name)


def wait_for_result(d: Door, run_id: str) -> None:
    """Ждать итог. Опрос, молчащий дольше RUNNER_LOST_SEC, — потеря связи: снять прогон; долгое «идёт» — нет."""
    poll = number(d.values, "RUNNER_POLL_SEC", 15)
    lost = number(d.values, "RUNNER_LOST_SEC", 300)
    timeout = number(d.values, "RUNNER_SSH_TIMEOUT_SEC", 10)
    silent_since = None
    while True:
        done = ssh_call(d, f"состояние {run_id}", timeout=timeout)
        answer = done.stdout.strip() if done is not None and done.returncode == 0 else None
        if answer == "готов":
            return
        if answer == "нет такого":
            raise Unavailable("итог runner битый: прогон исчез на runner")
        if answer == "идёт":
            silent_since = None
        else:
            silent_since = silent_since or time.monotonic()
            if time.monotonic() - silent_since > lost:
                cancel(d, run_id)
                raise Unavailable("связь потеряна на прогоне")
        time.sleep(poll)


def result_limit(d: Door) -> int:
    return number(d.values, "RUNNER_RESULT_MAX_MB", 200) * 2**20


def fetch_result(d: Door, run_id: str) -> bytes:
    limit = result_limit(d)
    proc = subprocess.Popen(ssh_argv(d, f"итог {run_id}"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    watchdog = threading.Timer(RESULT_READ_TIMEOUT_SEC, proc.kill)
    watchdog.start()
    try:
        data = proc.stdout.read(limit + 1)
        if len(data) > limit:
            proc.kill()
            raise Unavailable(f"итог runner битый: больше {limit // 2**20} МБ")
    finally:
        watchdog.cancel()
        proc.stdout.close()
        proc.wait()
    if proc.returncode:
        raise Unavailable(f"итог runner битый: вход не отдал итог (код {proc.returncode})")
    return data


def refuse_unsafe_members(tar: tarfile.TarFile, limit: int) -> None:
    """Отказ ДО распаковки: filter="data" пропускает ссылки внутрь каталога, а их в итоге быть не должно."""
    total = 0
    for member in tar.getmembers():
        if member.name.startswith("/") or ".." in PurePosixPath(member.name).parts:
            raise Unavailable(f"итог runner битый: путь {member.name!r}")
        if not (member.isfile() or member.isdir()):
            raise Unavailable(f"итог runner битый: запись {member.name!r} — ссылка или не файл")
        total += member.size
    if total > limit:
        raise Unavailable(f"итог runner битый: распакованный размер больше {limit // 2**20} МБ")


def unpack_result(d: Door, data: bytes, dest: Path) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            refuse_unsafe_members(tar, result_limit(d))
            tar.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as why:
        raise Unavailable(f"итог runner битый: архив не разобран ({why})") from None


def read_summary(folder: Path, sent: dict) -> dict:
    try:
        summary = json.loads((folder / "итог.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Unavailable("итог runner битый: нет итог.json или он не JSON") from None
    if not isinstance(summary, dict):
        raise Unavailable("итог runner битый: итог.json не объект")
    for key in ("id", "основа", "предмет"):
        if summary.get(key) != sent[key]:
            raise Unavailable(f"итог runner битый: {key} итога не равна отправленной")
    return summary


def receive_result(d: Door, sent: dict, out_dir: Path) -> dict:
    data = fetch_result(d, sent["id"])
    with tempfile.TemporaryDirectory(prefix="mq-runner-") as tmp:
        unpack_result(d, data, Path(tmp))
        summary = read_summary(Path(tmp), sent)
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(tmp, out_dir, dirs_exist_ok=True)
    return summary


def start_run(d: Door, job: dict) -> None:
    """«прогнать» после записи идут/<id>: ssh молчит или 255 — прогон мог стартовать, файл остаётся сироте."""
    pending = pending_dir(d) / job["id"]
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("", encoding="utf-8")
    done = ssh_call(d, f"прогнать {job['id']}", data=json.dumps(job, ensure_ascii=False),
                    timeout=job["ждать_места_с"] + number(d.values, "RUNNER_SSH_TIMEOUT_SEC", 10))
    if done is None or done.returncode == 255:
        raise Unavailable("прогнать: ssh не ответил")
    if done.returncode:
        pending.unlink(missing_ok=True)
        last = (done.stderr.strip().splitlines() or ["?"])[-1][:200]
        raise Unavailable(f"прогнать: код {done.returncode}: {last}")


def run(d: Door, req: Request) -> int:
    """Одна дорога на ворота, мутации и пробы: версия → задание → опрос → итог."""
    tree = req.tree or d.project
    ready(d)
    package_sha = package_head(d)
    subject = req.subject
    if not subject:
        subject = take_snapshot(tree)
        if not check_secrets("--range", f"HEAD..{subject}", str(tree), cwd=tree):
            raise Unavailable("снимок с секретом")
    refuse_redirects(tree)
    job = build_job(d, req, new_run_id(), subject, package_sha)
    run_id = job["id"]
    shas = (("основа", req.base_commit), ("предмет", subject), ("база", req.range_base))
    push_refs(d, tree, "project", {f"refs/runs/{run_id}/{name}": sha for name, sha in shas})
    push_refs(d, d.package, "package", {f"refs/runs/{run_id}/пакет": package_sha})
    start_run(d, job)
    wait_for_result(d, run_id)
    try:
        summary = receive_result(d, job, req.out_dir)
    finally:
        (pending_dir(d) / run_id).unlink(missing_ok=True)
    outcome = summary.get("исход")
    answer = {"машина": "runner", "исход": outcome, "ступени": summary.get("ступени", [])}
    if outcome == "предел":
        print(json.dumps({**answer, "причина": "не дошли"}, ensure_ascii=False))
        return LIMIT
    if outcome != "готов":
        raise Unavailable(f"итог runner битый: исход {outcome!r}")
    print(json.dumps(answer, ensure_ascii=False))
    return 0


def with_door(action) -> int:
    """Дверь закрыта — тихо 10; иначе сироты, затем действие; недоступность — строка и событие, код 10."""
    d = door()
    if d is None:
        return UNAVAILABLE
    try:
        cancel_orphans(d)
        return action(d)
    except (Unavailable, OSError) as why:
        return fallback(str(why))


def parse_request(kind: str, argv: list[str]) -> Request:
    parser = argparse.ArgumentParser(prog=f"remote-run.py {kind}")
    parser.add_argument("--ступени", required=True)
    parser.add_argument("--итог-в", required=True)
    if kind == "ворота":
        for name in ("основа", "предмет", "база"):
            parser.add_argument(f"--{name}", required=True)
    else:
        parser.add_argument("--дерево", required=True)
    args = vars(parser.parse_args(argv))
    stages, out_dir = json.loads(args["ступени"]), Path(args["итог_в"])
    if kind == "ворота":
        return Request(kind, None, args["основа"], args["предмет"], args["база"], stages, out_dir)
    tree = Path(args["дерево"])
    head = git_out(tree, "rev-parse", "HEAD")
    return Request(kind, tree, head, "", head, stages, out_dir)


def cmd_run(kind: str, argv: list[str]) -> int:
    req = parse_request(kind, argv)
    return with_door(lambda d: run(d, req))


COMMANDS = {"готов": lambda argv: cmd_ready() if not argv else None,
            "ворота": lambda argv: cmd_run("ворота", argv),
            "мутации": lambda argv: cmd_run("мутации", argv),
            "пробы": lambda argv: cmd_run("пробы", argv),
            "сироты": lambda argv: with_door(lambda d: 0)}


def main(argv: list[str]) -> int:
    handler = COMMANDS.get(argv[0]) if argv else None
    result = handler(argv[1:]) if handler else None
    if result is None:
        print(__doc__, file=sys.stderr)
        return 2
    return result


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
