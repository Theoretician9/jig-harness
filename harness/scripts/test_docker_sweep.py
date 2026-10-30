#!/usr/bin/env python3
"""Пробы уборки docker по правилам (Ф3 задачи pb.raise-disk-autocleanup-efficiency).

Что судится: `scripts/docker-sweep.py` РЯДОМ с пробой (мутации подставляют копию дерева)
убирает по возрасту и защите ровно то, что разрешают правила: старые неиспользуемые образы
(кроме latest/previous/тегов отката/защищённых и образов, у которых хоть один тег защищён),
остановленные контейнеры старше срока, безымянные тома без ссылок — только после выдержки
между двумя пометками, кэш сборки — `builder prune --keep-storage`; без --разрешено и без
--dry-run отказывается, не позвав docker; журнал удалений 0600; закрытый stdout ход не роняет.
Демон `harness/demons/disk-cleanup.sh` зовёт механизм дверью DOCKER_SWEEP_SCRIPT по
выключателю DOCKER_SWEEP_ENABLED; выкат зовёт его с --после-выката.

Механизм УДАЛЯЕТ, поэтому проба НИКОГДА не зовёт настоящий docker: PATH прогона состоит
только из каталогов стенда (`bin` с заглушкой `docker` и `py` с ссылкой на python), и перед
каждым прогоном `command -v docker` обязан вести в стенд — иначе «ПЛОХО СТЕНД» и выход.
Заглушка читает снимок из файлов стенда (dfv.json, df.txt, stopped.txt, inspect.json,
volinspect.json), пишет каждый вызов JSON-массивом в calls.log, на успешном удалении правит
снимок, по маркеру fail-<глагол> отказывает кодом 1, на незнакомый вызов — код 9.
Стенды — `mkdtemp(prefix="докер-стенд-")`; убираются только свои (живой pid — не трогается).

Правки ревью Ф3 (review-code-f3.md), контракт для кода — mutations-f3-todo.md задачи:
* C-1 — боевой ход ночью и по давлению требует `--свои <регулярка>` (данные DOCKER_SWEEP_OWN), пусто —
  отказ кодом 2 до docker (C1); контейнер снимается, только если его compose-проект или имя
  совпали со списком своих (C2, M4) и нет подключённого тома (C3); безымянный том — только если
  ход с --тома видел его подключённым к СВОЕМУ контейнеру (C4; D5, D5b, D11 эту историю дают).
* M-1 — возраст образа считается от первой встречи механизмом, не от CreatedAt (M1; D1, D3, D7,
  D8, D10, D11 проводят историю: образ появляется в час своей сборки).
* M-2 — МБ кэша в итоге берутся из «Total reclaimed space» ответа docker (M2).
* M-3 — без --тома нет ни `volume inspect`, ни меток томов (D5).

D9 судится всегда: PATH без заглушки состоит из пустого каталога стенда, поэтому docker хоста
недостижим по имени, и прогон идёт только сухим.
D13 — ЕДИНСТВЕННАЯ текстовая проба: deploy.sh полным прогоном на стенде не запустить (он
поднимает стек, ставит теги и ходит в health), поэтому судится место вызова в тексте.

Модуля нет — случаи D1–D11 красные «нет модуля», D12 и D13 красны нынешним демоном и выкатом.
Итог: «УБОРКА DOCKER: случаев N, неудач M», код 0/1.
"""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SWEEP = HERE / "docker-sweep.py"
DEPLOY = HERE / "deploy.sh"
DISK_PROBE = HERE / "test_disk_report.py"
STAND_PREFIX = "докер-стенд-"
CLI_TIMEOUT = 60
CLEANUP_TIMEOUT = 120
SWEEP_STUB_MARK = "ЗАГЛУШКА-ДОКЕР-УБОРКИ"
PROTECT = ("--защита", "sample-13")
# Разрешающий список СВОИХ (C-1): compose-проект или имя контейнера. Без него боевой ход отказывает.
OWN_RE = "^jig-stand"
OWN = ("--свои", OWN_RE)
KEYS = (*OWN, *PROTECT)
OWN_LABELS = {"com.docker.compose.project": "jig-stand"}
CLASSES = ("контейнеры", "образы", "тома", "кэш сборки")
JOURNAL_KEYS = {"когда", "класс", "идентификатор", "мб", "правило", "действие"}
NOW = int(dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.timezone.utc).timestamp())
H = 3600
DAY = 86400
TZ5 = dt.timezone(dt.timedelta(hours=5))


def hexid(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


class Probe:
    def __init__(self) -> None:
        self.cases = 0
        self.checks = 0
        self.failed_cases = 0
        self._case_failed = False

    def start(self, label: str, title: str) -> None:
        self.cases += 1
        self._case_failed = False
        print(f"{label} {title}")

    def check(self, label: str, name: str, ok: bool, detail="") -> bool:
        self.checks += 1
        if ok:
            print(f"  ок    {label} {name}")
        else:
            self._case_failed = True
            print(f"  ПЛОХО {label} {name}: {str(detail)[:700]}")
        return ok

    def fail(self, label: str, why: str) -> None:
        self.check(label, "случай не дошёл до суда", False, why)

    def finish(self) -> None:
        if self._case_failed:
            self.failed_cases += 1


PROBE = Probe()


# ─────────────────────────────── снимок docker ───────────────────────────────

def created_at(epoch: int) -> str:
    """Как `docker system df -v` печатает CreatedAt образа."""
    return dt.datetime.fromtimestamp(epoch, TZ5).strftime("%Y-%m-%d %H:%M:%S +0500 +05")


def iso_nano(epoch: int) -> str:
    """Как `docker inspect` печатает время: наносекунды и Z."""
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


def image(repo: str, tag: str, ident: str, age_h: float, size: str, containers: int = 0) -> dict:
    return {"ID": f"sha256:{hexid(ident)}", "Repository": repo, "Tag": tag,
            "Containers": str(containers), "CreatedAt": created_at(int(NOW - age_h * H)),
            "Size": size, "SharedSize": "0B", "UniqueSize": size}


OLD_APP = "old/app:v1"
DANGLING_OLD = f"sha256:{hexid('висячий-старый')}"
MID_APP = "mid/app:v1"


IMAGE_SPECS = (
    ("old/app", "v1", "old", 200, "500MB", 0),
    ("<none>", "<none>", "висячий-старый", 300, "300MB", 0),
    ("<none>", "<none>", "висячий-молодой", 5, "200MB", 0),
    ("used/app", "v1", "used", 400, "1.2GB", 1),
    ("young/app", "v1", "young", 10, "90MB", 0),
    ("svc/app", "latest", "svc-latest", 400, "700MB", 0),
    ("svc/app", "previous", "svc-previous", 400, "700MB", 0),
    ("svc/app", "20261001_1200_abcdef1", "svc-rollback", 400, "700MB", 0),
    ("sample-13-web", "v3", "sample-13", 400, "250MB", 0),
    ("multi/app", "v2", "multi", 400, "333MB", 0),
    ("multi/app", "latest", "multi", 400, "333MB", 0),
    ("mid/app", "v1", "mid", 72, "120MB", 0),
)


def images(min_age_h: float = 0) -> list:
    """Образы, которые уже были на машине за min_age_h часов до NOW."""
    return [image(repo, tag, ident, age, size, containers=used)
            for repo, tag, ident, age, size, used in IMAGE_SPECS if age >= min_age_h]


def container(name: str, image_ref: str, status: str, finished_age_h: float,
              labels: dict | None = None, mounts: tuple = ()) -> dict:
    return {"Id": hexid(f"контейнер-{name}"), "Name": f"/{name}",
            "Config": {"Image": image_ref, "Labels": dict(labels or {})},
            "State": {"Status": status, "FinishedAt": iso_nano(int(NOW - finished_age_h * H))},
            "Created": iso_nano(int(NOW - (finished_age_h + 1) * H)),
            "Mounts": [{"Type": "volume", "Name": v, "Source": f"/var/lib/docker/volumes/{v}/_data",
                        "Destination": "/data", "Driver": "local", "RW": True} for v in mounts]}


def containers() -> list:
    return [
        container("old_builder", "node:20", "exited", 48, labels=OWN_LABELS),
        container("young_builder", "node:20", "exited", 2, labels=OWN_LABELS),
        container("sample-13-worker", "python:3.12", "exited", 48),
        container("plain_name", "sample-13-api:1", "exited", 48),
        # Стоит в выдаче ps (гонка между ps и inspect): решает State.Status.
        # Свой: чужой отсеялся бы списком своих раньше, чем суд статуса.
        container("live_service", "svc/app:latest", "running", 300, labels=OWN_LABELS),
    ]


VOL_OLD = hexid("том-старый")
VOL_USED = hexid("том-занятый")
VOL_YOUNG = hexid("том-молодой")
VOL_NAMED = "pgdata"


def volume(name: str, links: int, anonymous: bool, age_d: float) -> tuple:
    row = {"Driver": "local", "Labels": "com.docker.volume.anonymous=" if anonymous
           else "com.docker.compose.project=shop", "Links": str(links),
           "Mountpoint": f"/var/lib/docker/volumes/{name}/_data", "Name": name,
           "Scope": "local", "Size": "1.5GB"}
    created = dt.datetime.fromtimestamp(int(NOW - age_d * DAY), TZ5).isoformat()
    return row, {"Name": name, "CreatedAt": created, "Driver": "local",
                 "Labels": {"com.docker.volume.anonymous": ""} if anonymous else {}}


def volumes(only=None) -> list:
    rows = [volume(VOL_OLD, 0, True, 30), volume(VOL_USED, 1, True, 30),
            volume(VOL_YOUNG, 0, True, 3), volume(VOL_NAMED, 0, False, 60)]
    return [r for r in rows if only is None or r[0]["Name"] in only]


DF_LINES = (
    {"Type": "Images", "TotalCount": "12", "Active": "2", "Size": "5.6GB", "Reclaimable": "3.1GB (55%)"},
    {"Type": "Containers", "TotalCount": "5", "Active": "1", "Size": "12MB", "Reclaimable": "9MB (75%)"},
    {"Type": "Local Volumes", "TotalCount": "4", "Active": "1", "Size": "6GB", "Reclaimable": "4.5GB (75%)"},
    {"Type": "Build Cache", "TotalCount": "40", "Active": "0", "Size": "30GB", "Reclaimable": "30GB (99%)"},
)

STUB = r'''#!@PY@
import json, sys
from pathlib import Path
S = Path(@STAND@)
args = sys.argv[1:]
with open(S / "calls.log", "a", encoding="utf-8") as log:
    log.write(json.dumps(args, ensure_ascii=False) + "\n")

def load(name):
    return json.loads((S / name).read_text(encoding="utf-8"))

def save(name, data):
    (S / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

def refuse_if_marked(verb, target):
    if (S / f"fail-{verb}").exists():
        sys.stderr.write(f"Error: conflict: unable to remove {target}: stand refuses\nsecond line of the error\n")
        sys.exit(1)

operands = [a for a in args[1:] if not a.startswith("-")]
head = args[:2]
if head[:2] == ["system", "df"]:
    if "-v" in args:
        print(json.dumps(load("dfv.json"), ensure_ascii=False))
    else:
        sys.stdout.write((S / "df.txt").read_text(encoding="utf-8"))
elif args[:1] == ["ps"]:
    sys.stdout.write((S / "stopped.txt").read_text(encoding="utf-8"))
elif args[:1] == ["inspect"] or head == ["container", "inspect"]:
    ids = [a for a in operands if a != "inspect"]
    rows = load("inspect.json")
    found = [r for r in rows if any(r["Id"].startswith(i) or r["Name"].lstrip("/") == i for i in ids)]
    print(json.dumps(found))
    if len(found) < len(ids):
        sys.stderr.write("Error: No such object\n")
        sys.exit(1)
elif head == ["volume", "inspect"]:
    names = operands[1:]
    found = [r for r in load("volinspect.json") if r["Name"] in names]
    print(json.dumps(found))
elif args[:1] == ["rmi"]:
    dfv = load("dfv.json")
    for target in operands:
        refuse_if_marked("rmi", target)
        dfv["Images"] = [r for r in dfv["Images"]
                         if f"{r['Repository']}:{r['Tag']}" != target and r["ID"] != target]
        print(f"Untagged: {target}")
    save("dfv.json", dfv)
elif args[:1] == ["rm"]:
    for target in operands:
        refuse_if_marked("rm", target)
        save("inspect.json", [r for r in load("inspect.json") if r["Id"] != target])
        lines = (S / "stopped.txt").read_text(encoding="utf-8").split()
        (S / "stopped.txt").write_text("".join(l + "\n" for l in lines if l != target), encoding="utf-8")
        print(target)
elif head == ["volume", "rm"]:
    for target in operands[1:]:
        refuse_if_marked("volume-rm", target)
        dfv = load("dfv.json")
        dfv["Volumes"] = [r for r in dfv["Volumes"] if r["Name"] != target]
        save("dfv.json", dfv)
        save("volinspect.json", [r for r in load("volinspect.json") if r["Name"] != target])
        print(target)
elif head == ["builder", "prune"]:
    refuse_if_marked("builder-prune", "build cache")
    # Строка docker CLI (не buildx): сколько освобождено НА САМОМ ДЕЛЕ — файл стенда prune-reclaimed.
    freed = (S / "prune-reclaimed").read_text(encoding="utf-8").strip() if (S / "prune-reclaimed").exists() else "28GB"
    print("Total reclaimed space: " + freed)
else:
    sys.stderr.write("НЕИЗВЕСТНЫЙ ВЫЗОВ " + " ".join(args) + "\n")
    sys.exit(9)
'''


def write_snapshot(stand: Path, vols=None) -> None:
    vol_rows = volumes(vols)
    (stand / "dfv.json").write_text(json.dumps(
        {"Images": images(), "Containers": [], "Volumes": [r for r, _ in vol_rows], "BuildCache": []},
        ensure_ascii=False), encoding="utf-8")
    (stand / "volinspect.json").write_text(json.dumps([i for _, i in vol_rows], ensure_ascii=False),
                                           encoding="utf-8")
    (stand / "df.txt").write_text("".join(json.dumps(line) + "\n" for line in DF_LINES), encoding="utf-8")
    rows = containers()
    (stand / "inspect.json").write_text(json.dumps(rows), encoding="utf-8")
    (stand / "stopped.txt").write_text("".join(r["Id"] + "\n" for r in rows), encoding="utf-8")


def set_links(stand: Path, name: str, links: int) -> None:
    dfv = json.loads((stand / "dfv.json").read_text(encoding="utf-8"))
    for row in dfv["Volumes"]:
        if row["Name"] == name:
            row["Links"] = str(links)
    (stand / "dfv.json").write_text(json.dumps(dfv, ensure_ascii=False), encoding="utf-8")


def write_images(stand: Path, rows: list) -> None:
    dfv = json.loads((stand / "dfv.json").read_text(encoding="utf-8"))
    dfv["Images"] = rows
    (stand / "dfv.json").write_text(json.dumps(dfv, ensure_ascii=False), encoding="utf-8")


def add_container(stand: Path, row: dict) -> None:
    rows = json.loads((stand / "inspect.json").read_text(encoding="utf-8")) + [row]
    (stand / "inspect.json").write_text(json.dumps(rows), encoding="utf-8")
    (stand / "stopped.txt").write_text("".join(r["Id"] + "\n" for r in rows), encoding="utf-8")


def drop_container(stand: Path, name: str) -> None:
    """Контейнер снят мимо механизма (чужой `compose down`)."""
    rows = [r for r in json.loads((stand / "inspect.json").read_text(encoding="utf-8"))
            if r["Name"] != f"/{name}"]
    (stand / "inspect.json").write_text(json.dumps(rows), encoding="utf-8")
    (stand / "stopped.txt").write_text("".join(r["Id"] + "\n" for r in rows), encoding="utf-8")


# ─────────────────────────────── стенд ───────────────────────────────────────

def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweep_dead_stands() -> int:
    """Только свои стенды умерших прогонов (SIGKILL пропускает finally)."""
    removed = 0
    for path in glob.glob(f"/tmp/{STAND_PREFIX}*"):
        try:
            pid = int(Path(path, "pid").read_text().strip())
        except (OSError, ValueError):
            pid = None
        if pid is not None and pid_alive(pid):
            continue
        if pid is None and time.time() - os.lstat(path).st_mtime < 3600:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def make_stand(vols=None) -> Path:
    stand = Path(tempfile.mkdtemp(prefix=STAND_PREFIX, dir="/tmp"))
    (stand / "pid").write_text(str(os.getpid()))
    for sub in ("bin", "py", "empty"):
        (stand / sub).mkdir()
    (stand / "py" / "python3").symlink_to(sys.executable)
    (stand / "calls.log").write_text("")
    stub = stand / "bin" / "docker"
    stub.write_text(STUB.replace("@PY@", sys.executable).replace("@STAND@", repr(str(stand))),
                    encoding="utf-8")
    stub.chmod(0o755)
    write_snapshot(stand, vols)
    return stand


def stand_env(stand: Path, with_docker: bool = True) -> dict:
    path = f"{stand}/bin:{stand}/py" if with_docker else f"{stand}/empty:{stand}/py"
    return {"PATH": path, "HOME": str(stand), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "TZ": "UTC", "PYTHONDONTWRITEBYTECODE": "1"}


def docker_seen_as(env: dict) -> str:
    return subprocess.run(["/bin/bash", "-c", "command -v docker || true"], env=env,
                          capture_output=True, text=True, timeout=CLI_TIMEOUT).stdout.strip()


def guard_stand(stand: Path, env: dict, with_docker: bool) -> None:
    where = docker_seen_as(env)
    want = f"{stand}/bin/docker" if with_docker else ""
    if where != want:
        print(f"ПЛОХО СТЕНД: docker → «{where or 'нет'}», ждал «{want or 'нет'}» — прогон не начат, проба остановлена")
        raise SystemExit(2)


def calls(stand: Path) -> list:
    return [json.loads(line) for line in
            (stand / "calls.log").read_text(encoding="utf-8").splitlines() if line]


def verb(call: list) -> str:
    if call[:2] in (["volume", "rm"], ["builder", "prune"], ["volume", "inspect"],
                    ["system", "df"], ["container", "inspect"]):
        return " ".join(call[:2])
    return call[0] if call else ""


MUTATING = ("rm", "rmi", "volume rm", "builder prune")
KNOWN = MUTATING + ("system df", "ps", "inspect", "container inspect", "volume inspect")


def mutating(stand: Path) -> list:
    return [c for c in calls(stand) if verb(c) in MUTATING]


def targets(stand: Path, which: str) -> list:
    skip = 2 if " " in which else 1
    return [a for c in calls(stand) if verb(c) == which for a in c[skip:] if not a.startswith("-")]


def forbidden(all_calls: list) -> list:
    bad = []
    for c in all_calls:
        v = verb(c)
        if len(c) >= 2 and c[1] == "prune" and c[0] in ("system", "volume", "image", "container"):
            bad.append(c)
        elif v == "builder prune" and any(a in ("-a", "--all") for a in c):
            bad.append(c)
        elif v in ("rm", "rmi", "volume rm") and any(
                a in ("-f", "--force", "-v", "--volumes") or re.fullmatch(r"-[a-z]*[fv][a-z]*", a) for a in c[1:]):
            bad.append(c)
    return bad


class Run:
    def __init__(self, proc: subprocess.CompletedProcess) -> None:
        self.rc = proc.returncode
        self.out = (proc.stdout or b"").decode("utf-8", "replace")
        self.err = (proc.stderr or b"").decode("utf-8", "replace")

    def __repr__(self) -> str:
        return f"rc={self.rc} out={self.out[:400]!r} err={self.err[-400:]!r}"

    def rows(self) -> dict:
        found = {}
        for line in self.out.splitlines():
            parts = line.split("\t")
            if len(parts) == 4 and parts[0] in CLASSES:
                found[parts[0]] = parts
        return found


def journal_path(stand: Path) -> Path:
    return stand / "журнал-удалений.jsonl"


def marks_path(stand: Path) -> Path:
    return stand / "метки-томов.json"


def sweep(stand: Path, *args: str, now: int = NOW, stdout=None, with_docker: bool = True,
          journal_file: Path | None = None) -> Run:
    env = stand_env(stand, with_docker)
    guard_stand(stand, env, with_docker)
    full = [sys.executable, "-B", str(SWEEP), *args, "--сейчас", str(now),
            "--журнал", str(journal_file or journal_path(stand)), "--метки", str(marks_path(stand))]
    proc = subprocess.run(full, cwd=stand, env=env, stdin=subprocess.DEVNULL,
                          stdout=stdout if stdout is not None else subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=CLI_TIMEOUT,
                          preexec_fn=lambda: os.umask(0))   # режим 0600 обязан ставить сам механизм
    return Run(proc)


HOLD_ALL = ("--образы-ч", "1e9", "--контейнеры-ч", "1e9", "--кэш-гб", "1e9")


def image_history(stand: Path) -> None:
    """Образы появляются на машине в час своей сборки (M-1): возраст считается от появления.

    Ход в каждый такой час видит только уже появившиеся образы; пороги HOLD_ALL — ничего не
    убирается, свой журнал — журнал случая чист. Последний ход видит все образы снимка."""
    for age in sorted({spec[3] for spec in IMAGE_SPECS}, reverse=True):
        write_images(stand, images(min_age_h=age))
        sweep(stand, "--ночь", "--разрешено", *KEYS, *HOLD_ALL, now=int(NOW - age * H),
              journal_file=stand / "журнал-истории.jsonl")


OWN_DB = "jig-stand-db"


def own_volume_history(stand: Path, now: int, *vols: str) -> None:
    """Тома были подключены к СВОЕМУ контейнеру (ход с --тома это видит), потом контейнер снят
    мимо механизма и ссылок не стало — только такой безымянный том механизм вправе убрать (C-1)."""
    add_container(stand, container(OWN_DB, "postgres:17", "exited", 30, labels=OWN_LABELS, mounts=vols))
    for vol in vols:
        set_links(stand, vol, 1)
    sweep(stand, "--ночь", "--разрешено", "--тома", *KEYS, *HOLD_ALL, now=now,
          journal_file=stand / "журнал-истории.jsonl")
    drop_container(stand, OWN_DB)
    for vol in vols:
        set_links(stand, vol, 0)


def volume_marks(stand: Path) -> list:
    """Имена томов стенда, попавшие в файл меток (файла нет — пусто)."""
    if not marks_path(stand).exists():
        return []
    got = marks(stand)
    names = {VOL_OLD, VOL_USED, VOL_YOUNG, VOL_NAMED}
    return sorted(names & set(got)) if isinstance(got, dict) else ["нечитаемо"]


def journal(stand: Path) -> list:
    path = journal_path(stand)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"нечитаемо": line})
    return out


def marks(stand: Path):
    try:
        return json.loads(marks_path(stand).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return {"ошибка": str(error)}


def need_module(label: str) -> bool:
    if SWEEP.is_file():
        return True
    PROBE.check(label, "есть модуль", False, f"нет модуля {SWEEP}")
    return False


def mb_spellings(*decimal_mb: float) -> set:
    total = sum(decimal_mb)
    return {round(total), int(total), round(total * 1e6 / 1048576), int(total * 1e6 / 1048576)}


KEPT_IMAGES = ("used/app:v1", "young/app:v1", "svc/app:latest", "svc/app:previous",
               "svc/app:20261001_1200_abcdef1", "sample-13-web:v3", "multi/app:v2",
               "multi/app:latest", MID_APP)


def image_ids_of(*refs: str) -> set:
    return {r["ID"] for r in images() if f"{r['Repository']}:{r['Tag']}" in refs}


# ─────────────────────────────── случаи ──────────────────────────────────────

def case_d1(stand: Path) -> None:
    """Сухой прогон: ничего не меняет, журнала нет, опись называет образы и кэш, итог с МБ."""
    if not need_module("D1"):
        return
    image_history(stand)
    run = sweep(stand, "--ночь", "--dry-run", *KEYS)
    PROBE.check("D1", "код 0", run.rc == 0, run)
    PROBE.check("D1", "изменяющих вызовов нет", not mutating(stand), mutating(stand))
    PROBE.check("D1", "журнал удалений не создан", not journal_path(stand).exists())
    rows = run.rows()
    img = rows.get("образы")
    want_mb = mb_spellings(500, 300)
    PROBE.check("D1", f"строка «образы»: 2 шт., МБ ∈ {sorted(want_mb)}",
                img is not None and img[1] == "2" and img[2].isdigit()
                and any(abs(int(img[2]) - m) <= 1 for m in want_mb), rows)
    PROBE.check("D1", "строка «кэш сборки» есть", "кэш сборки" in rows, rows)
    total = re.search(r"^docker: убрал бы (\d+), (\d+) МБ$", run.out, re.M)
    PROBE.check("D1", "итог «docker: убрал бы N, M МБ», M не меньше образов",
                total is not None and int(total.group(2)) >= min(want_mb) - 1, run)


def case_d2(stand: Path) -> None:
    """Боевой ход без --разрешено: отказ кодом 2 со словом выключателя, docker не зван."""
    if not need_module("D2"):
        return
    run = sweep(stand, "--ночь", *KEYS)
    PROBE.check("D2", "код 2 и слово DOCKER_SWEEP_ENABLED",
                run.rc == 2 and "DOCKER_SWEEP_ENABLED" in run.out + run.err, run)
    PROBE.check("D2", "calls.log пуст", calls(stand) == [], calls(stand))


def case_d3(stand: Path) -> None:
    """Образы: убираются ровно старый тегированный и старый висячий; остальные по своим правилам целы."""
    if not need_module("D3"):
        return
    image_history(stand)
    run = sweep(stand, "--ночь", "--разрешено", *KEYS)
    PROBE.check("D3", "код 0", run.rc == 0, run)
    got = targets(stand, "rmi")
    PROBE.check("D3", f"rmi ровно {{{OLD_APP}, {DANGLING_OLD[:19]}…}}",
                sorted(got) == sorted([OLD_APP, DANGLING_OLD]), got)
    kept_ids = image_ids_of(*KEPT_IMAGES)
    touched = [t for t in got if t in KEPT_IMAGES or t in kept_ids
               or t == f"sha256:{hexid('висячий-молодой')}"]
    PROBE.check("D3", "целы: используемый, молодой, latest, previous, откат, защищённый, "
                "двутеговый, молодой висячий, 72-часовой", not touched, touched)
    rmi_flags = [c for c in calls(stand) if verb(c) == "rmi" and any(a.startswith("-") for a in c[1:])]
    PROBE.check("D3", "rmi без флагов", not rmi_flags, rmi_flags)
    rows = [r for r in journal(stand) if r.get("класс") == "образы"]
    PROBE.check("D3", "журнал: 2 строки образов «удалено» со всеми ключами",
                len(rows) == 2 and all(r.get("действие") == "удалено" and JOURNAL_KEYS <= set(r) for r in rows)
                and sorted(r.get("идентификатор") for r in rows) == sorted([OLD_APP, DANGLING_OLD]),
                journal(stand))


def case_d4(stand: Path) -> None:
    """Контейнеры: остановленный старше 24 ч — `rm <полный id>`; молодой, running, защищённые целы."""
    if not need_module("D4"):
        return
    run = sweep(stand, "--ночь", "--разрешено", *KEYS)
    old = hexid("контейнер-old_builder")
    got = targets(stand, "rm")
    PROBE.check("D4", "код 0, rm ровно [полный id old_builder]", run.rc == 0 and got == [old], (run, got))
    flags = [c for c in calls(stand) if verb(c) == "rm" and len(c) != 2]
    PROBE.check("D4", "rm без -v/-f, по одному id", not flags, flags)


def case_d5(stand: Path) -> None:
    """Тома: без --тома не трогаются и не помечаются; свой отвязанный — метка → 2 ч ничего → 25 ч удалён."""
    if not need_module("D5"):
        return
    base = ("--ночь", "--разрешено", *KEYS)
    # Свои все три: иначе именованный и молодой отсеялись бы принадлежностью раньше своих правил,
    # а ход без --тома не нашёл бы кандидатов и при снятом выключателе.
    own_volume_history(stand, NOW, VOL_OLD, VOL_NAMED, VOL_YOUNG)
    sweep(stand, *base, now=NOW + H // 2)
    PROBE.check("D5", "без --тома: volume rm не зван", not targets(stand, "volume rm"), calls(stand))
    looked = [c for c in calls(stand) if verb(c) == "volume inspect"]
    PROBE.check("D5", "без --тома: volume inspect не зван, тома не помечены (M-3)",
                not looked and not volume_marks(stand), (looked, volume_marks(stand)))
    first = sweep(stand, *base, "--тома", now=NOW + H)
    m1 = marks(stand)
    PROBE.check("D5", "первый проход с --тома: код 0, удалений томов нет",
                first.rc == 0 and not targets(stand, "volume rm"), (first, targets(stand, "volume rm")))
    PROBE.check("D5", "метка: старый безымянный помечен эпохой прохода; занятый, именованный и молодой — нет",
                isinstance(m1, dict) and m1.get(VOL_OLD) == NOW + H
                and VOL_USED not in m1 and VOL_NAMED not in m1 and VOL_YOUNG not in m1, m1)
    sweep(stand, *base, "--тома", now=NOW + 3 * H)
    PROBE.check("D5", "через 2 ч: не удаляет, метка прежняя",
                not targets(stand, "volume rm") and marks(stand).get(VOL_OLD) == NOW + H,
                (targets(stand, "volume rm"), marks(stand)))
    third = sweep(stand, *base, "--тома", now=NOW + 26 * H)
    PROBE.check("D5", "через 25 ч: volume rm ровно [старый безымянный]",
                third.rc == 0 and targets(stand, "volume rm") == [VOL_OLD], (third, targets(stand, "volume rm")))
    PROBE.check("D5", "volume prune не зван ни разу", not any(c[:2] == ["volume", "prune"] for c in calls(stand)))


def case_d5b(stand: Path) -> None:
    """Том стал занятым между проходами: не удалён, метка снята."""
    if not need_module("D5b"):
        return
    write_snapshot(stand, vols={VOL_OLD, VOL_USED, VOL_NAMED})
    base = ("--ночь", "--разрешено", "--тома", *KEYS)
    own_volume_history(stand, NOW - H, VOL_OLD)
    sweep(stand, *base)
    PROBE.check("D5b", "первый проход пометил старый безымянный", marks(stand).get(VOL_OLD) == NOW, marks(stand))
    set_links(stand, VOL_OLD, 1)
    run = sweep(stand, *base, now=NOW + 25 * H)
    PROBE.check("D5b", "через 25 ч при Links 1: не удалён, код 0",
                run.rc == 0 and not targets(stand, "volume rm"), (run, targets(stand, "volume rm")))
    PROBE.check("D5b", "в метках нет ни одного тома стенда", volume_marks(stand) == [], marks(stand))


def case_d6(stand: Path) -> None:
    """Кэш: сухой не чистит; после выката — ровно `builder prune -f --keep-storage 2GB`, образы целы."""
    if not need_module("D6"):
        return
    sweep(stand, "--ночь", "--dry-run", *KEYS)
    PROBE.check("D6", "--dry-run: builder prune не зван", not targets(stand, "builder prune")
                and not any(verb(c) == "builder prune" for c in calls(stand)), calls(stand))
    run = sweep(stand, "--после-выката", "--разрешено", "--кэш-гб", "2", *KEYS)
    prunes = [c for c in calls(stand) if verb(c) == "builder prune"]
    PROBE.check("D6", "код 0, ровно один вызов builder prune -f --keep-storage 2GB",
                run.rc == 0 and prunes == [["builder", "prune", "-f", "--keep-storage", "2GB"]], (run, prunes))
    PROBE.check("D6", "образы и контейнеры не тронуты", not targets(stand, "rmi") and not targets(stand, "rm"),
                mutating(stand))
    PROBE.check("D6", "ни system prune, ни builder prune -a", not forbidden(calls(stand)), forbidden(calls(stand)))


def case_d7(stand: Path) -> None:
    """Порог давления: образ 72 ч по давлению убран, ночью цел."""
    if not need_module("D7"):
        return
    sweep(stand, "--ночь", "--разрешено", *KEYS)
    PROBE.check("D7", f"ночью (168 ч) {MID_APP} цел", MID_APP not in targets(stand, "rmi"), targets(stand, "rmi"))
    other = make_stand()
    try:
        image_history(other)
        run = sweep(other, "--давление", "--разрешено", *KEYS)
        PROBE.check("D7", f"по давлению (48 ч) {MID_APP} убран, код 0",
                    run.rc == 0 and MID_APP in targets(other, "rmi"), (run, targets(other, "rmi")))
        PROBE.check("D7", "по давлению молодой (10 ч) цел", "young/app:v1" not in targets(other, "rmi"),
                    targets(other, "rmi"))
    finally:
        leak = forbidden(calls(other))
        PROBE.check("D7", "второй стенд: запретных вызовов нет", not leak, leak)
        shutil.rmtree(other, ignore_errors=True)


def case_d8(stand: Path) -> None:
    """Отказ rmi: обе попытки сделаны, в журнале две строки «отказ: <первая строка>», код 0."""
    if not need_module("D8"):
        return
    image_history(stand)
    (stand / "fail-rmi").write_text("")
    run = sweep(stand, "--ночь", "--разрешено", *KEYS)
    PROBE.check("D8", "код 0, rmi звана 2 раза (отказ первой не останавливает вторую)",
                run.rc == 0 and sorted(targets(stand, "rmi")) == sorted([OLD_APP, DANGLING_OLD]),
                (run, targets(stand, "rmi")))
    refused = [r for r in journal(stand) if r.get("класс") == "образы"]
    want = {t: f"отказ: Error: conflict: unable to remove {t}: stand refuses" for t in (OLD_APP, DANGLING_OLD)}
    PROBE.check("D8", "журнал: две строки «отказ: <первая строка ошибки docker>»",
                len(refused) == 2 and all(want.get(r.get("идентификатор")) == r.get("действие") for r in refused),
                journal(stand))


def case_d9(stand: Path) -> None:
    """Нет docker в PATH: «docker не установлен», код 0 (PATH — пустой каталог стенда)."""
    if not need_module("D9"):
        return
    run = sweep(stand, "--ночь", "--dry-run", *KEYS, with_docker=False)
    PROBE.check("D9", "код 0 и «docker не установлен»", run.rc == 0 and "docker не установлен" in run.out, run)
    PROBE.check("D9", "заглушка не звана (её нет в PATH)", calls(stand) == [], calls(stand))


def case_d10(stand: Path) -> None:
    """Журнал 0600 при umask 0; stdout в /dev/full — код 0, удаление дошло."""
    if not need_module("D10"):
        return
    run = sweep(stand, "--ночь", "--разрешено", *KEYS)
    mode = journal_path(stand).stat().st_mode & 0o777 if journal_path(stand).exists() else None
    PROBE.check("D10", "журнал есть, режим 0600 (umask прогона 0)", run.rc == 0 and mode == 0o600,
                (run, oct(mode) if mode is not None else "нет"))
    other = make_stand()
    try:
        image_history(other)
        with open("/dev/full", "wb") as full:
            closed = sweep(other, "--ночь", "--разрешено", *KEYS, stdout=full)
        PROBE.check("D10", ">/dev/full: код 0, rmi old/app:v1 и rm дошли",
                    closed.rc == 0 and OLD_APP in targets(other, "rmi") and targets(other, "rm"),
                    (closed, targets(other, "rmi")))
    finally:
        shutil.rmtree(other, ignore_errors=True)


def case_d11(stand: Path) -> None:
    """Все режимы подряд: ни одного prune-всего, rm/rmi с -f/-v, builder prune -a; а работа была."""
    if not need_module("D11"):
        return
    image_history(stand)
    own_volume_history(stand, NOW - 2 * H, VOL_OLD)
    for args, now in ((("--ночь", "--dry-run", "--тома"), NOW),
                      (("--ночь", "--разрешено", "--тома"), NOW),
                      (("--давление", "--разрешено", "--тома"), NOW + H),
                      (("--после-выката", "--разрешено"), NOW + 2 * H),
                      (("--ночь", "--разрешено", "--тома"), NOW + 26 * H)):
        sweep(stand, *args, *KEYS, now=now)
    every = calls(stand)
    PROBE.check("D11", "запретных вызовов нет", not forbidden(every), forbidden(every))
    PROBE.check("D11", "проба не пуста: rmi, rm, volume rm и builder prune случились",
                all(any(verb(c) == v for c in every) for v in MUTATING), sorted({verb(c) for c in every}))


OLD_BUILDER = hexid("контейнер-old_builder")


def case_c1(stand: Path) -> None:
    """Список своих пуст или не задан: боевой ход ночью и по давлению отказывает, docker не зван (C-1)."""
    if not need_module("C1"):
        return
    for label, args in (("без --свои, ночь", ("--ночь", "--разрешено", *PROTECT)),
                        ("--свои \"\", ночь", ("--ночь", "--разрешено", "--свои", "", *PROTECT)),
                        ("без --свои, давление", ("--давление", "--разрешено", *PROTECT))):
        run = sweep(stand, *args)
        PROBE.check("C1", f"{label}: код 2 и слово DOCKER_SWEEP_OWN",
                    run.rc == 2 and "DOCKER_SWEEP_OWN" in run.out + run.err, run)
    PROBE.check("C1", "calls.log пуст (отказ до первого вызова docker)", calls(stand) == [], calls(stand))


def case_c2(stand: Path) -> None:
    """Чужой compose-проект без --защита: его остановленный контейнер цел, снят только свой (C-1)."""
    if not need_module("C2"):
        return
    add_container(stand, container("shop-postgres-1", "postgres:17", "exited", 48,
                                   labels={"com.docker.compose.project": "shop"}))
    run = sweep(stand, "--ночь", "--разрешено", *OWN)
    got = targets(stand, "rm")
    PROBE.check("C2", "код 0, rm ровно [old_builder]: shop-postgres-1, sample-13-worker, plain_name целы",
                run.rc == 0 and got == [OLD_BUILDER], (run, got))


def case_c3(stand: Path) -> None:
    """Свой остановленный контейнер с подключённым томом не снимается (C-1)."""
    if not need_module("C3"):
        return
    add_container(stand, container(OWN_DB, "postgres:17", "exited", 48, labels=OWN_LABELS, mounts=(VOL_USED,)))
    run = sweep(stand, "--ночь", "--разрешено", *KEYS)
    got = targets(stand, "rm")
    PROBE.check("C3", f"код 0, {OWN_DB} (том подключён) цел, old_builder снят",
                run.rc == 0 and hexid(f"контейнер-{OWN_DB}") not in got and OLD_BUILDER in got, (run, got))


def case_c4(stand: Path) -> None:
    """Безымянный том чужого проекта после его `compose down` не удаляется даже с --тома (C-1)."""
    if not need_module("C4"):
        return
    write_snapshot(stand, vols={VOL_OLD})
    foreign = "sample-13-postgres-1"
    add_container(stand, container(foreign, "postgres:15-alpine", "running", 0,
                                   labels={"com.docker.compose.project": "sample-13"}, mounts=(VOL_OLD,)))
    set_links(stand, VOL_OLD, 1)
    base = ("--ночь", "--разрешено", "--тома", *KEYS)
    sweep(stand, *base)
    drop_container(stand, foreign)
    set_links(stand, VOL_OLD, 0)
    for hours in (1, 3, 26, 50):
        sweep(stand, *base, now=NOW + hours * H)
    PROBE.check("C4", "за 50 ч после отвязки volume rm не зван ни разу",
                not targets(stand, "volume rm"), targets(stand, "volume rm"))


POSTGIS = "postgis/postgis:16-3.4"


def case_m1(stand: Path) -> None:
    """Возраст образа — от появления на машине: скачанный сегодня образ двухлетней сборки цел (M-1)."""
    if not need_module("M1"):
        return
    rows = json.loads((stand / "dfv.json").read_text(encoding="utf-8"))["Images"]
    write_images(stand, rows + [image("postgis/postgis", "16-3.4", "postgis", 17000, "400MB")])
    for hours, gone in ((0, False), (24, False), (170, True)):
        sweep(stand, "--ночь", "--разрешено", *KEYS, now=NOW + hours * H)
        PROBE.check("M1", f"через {hours} ч после появления: {POSTGIS} {'снят' if gone else 'цел'}",
                    (POSTGIS in targets(stand, "rmi")) == gone, targets(stand, "rmi"))


def case_m2(stand: Path) -> None:
    """Кэш: итог печатает ЗАМЕР из ответа docker («Total reclaimed space»), не расчёт Reclaimable−keep (M-2)."""
    if not need_module("M2"):
        return
    for reclaimed, want in (("1.5GB", mb_spellings(1500)), ("0B", {0})):
        (stand / "prune-reclaimed").write_text(reclaimed)
        run = sweep(stand, "--после-выката", "--разрешено", "--кэш-гб", "2", *KEYS)
        row = run.rows().get("кэш сборки")
        total = re.search(r"^docker: убрано \d+, (\d+) МБ$", run.out, re.M)
        PROBE.check("M2", f"docker освободил {reclaimed}: строка кэша и итог — МБ ∈ {sorted(want)}, не 28000",
                    run.rc == 0 and row is not None and row[2].isdigit() and int(row[2]) in want
                    and total is not None and int(total.group(1)) in want, run)


def case_m4(stand: Path) -> None:
    """Остановленный руками контейнер (`docker run`, без метки compose, имя не своё) цел (M-4)."""
    if not need_module("M4"):
        return
    add_container(stand, container("tool", "busybox:latest", "exited", 48))
    run = sweep(stand, "--ночь", "--разрешено", *KEYS)
    got = targets(stand, "rm")
    PROBE.check("M4", "код 0, tool цел, old_builder снят",
                run.rc == 0 and hexid("контейнер-tool") not in got and OLD_BUILDER in got, (run, got))


DAEMON_STUB = f'''#!/usr/bin/env python3
import sys
with open(sys.argv[0].rsplit("/project/", 1)[0] + "/calls.log", "a", encoding="utf-8") as log:
    log.write("docker-sweep\\t{SWEEP_STUB_MARK} " + " ".join(sys.argv[1:]) + "\\n")
'''


def load_disk_probe():
    spec = importlib.util.spec_from_file_location("test_disk_report_stand", DISK_PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def case_d12(_stand: Path) -> None:
    """Демон: дверь DOCKER_SWEEP_SCRIPT, выключатель DOCKER_SWEEP_ENABLED (умолчание 0), ключи по режиму."""
    disk = load_disk_probe()
    script = disk.cleanup_script()
    if not PROBE.check("D12", "демон читает дверь DOCKER_SWEEP_SCRIPT (иначе не запускаю: пошёл бы настоящий)",
                       "DOCKER_SWEEP_SCRIPT" in script.read_text(encoding="utf-8"), script):
        return
    seen = {}
    for label, conf, args, pct in (("нет ключа", "", (), "50"),
                                   ("ключ 0", "DOCKER_SWEEP_ENABLED=0\n", (), "50"),
                                   ("ключ 1", "DOCKER_SWEEP_ENABLED=1\nDOCKER_SWEEP_OWN=^jig-stand\n", (), "50"),
                                   ("ключ 1 --dry-run", "DOCKER_SWEEP_ENABLED=1\n", ("--dry-run",), "50"),
                                   ("ключ 1 --по-давлению", "DOCKER_SWEEP_ENABLED=1\n", ("--по-давлению",), "86")):
        stand = disk.make_stand()
        try:
            door = stand / "project/scripts/docker-sweep.py"
            door.write_text(DAEMON_STUB, encoding="utf-8")
            door.chmod(0o755)
            env = disk.stand_env(stand) | {"DOCKER_SWEEP_SCRIPT": str(door)}
            bad = disk.stubs_in_front(stand) or disk.sweep_door_off_stand(stand, script)
            if bad or not Path(env["DOCKER_SWEEP_SCRIPT"]).resolve().is_relative_to(stand.resolve()):
                print(f"ПЛОХО СТЕНД: {bad or 'DOCKER_SWEEP_SCRIPT вне стенда'} — демон не запущен, проба остановлена")
                raise SystemExit(2)
            disk.set_conf(stand, disk.REPEAT_H, conf)
            (stand / "pct").write_text(pct)
            proc = subprocess.run(["bash", str(script), *args], cwd=stand, env=env, capture_output=True,
                                  timeout=CLEANUP_TIMEOUT)
            out = proc.stdout.decode("utf-8", "replace")
            log = disk.calls(stand)
            sweeps = [a for n, a, *_ in log if n == "docker-sweep"]
            real = [f"{n} {a}" for n, a, *_ in log
                    if (n == "docker-sweep" and not a.startswith(SWEEP_STUB_MARK))
                    or (n == "docker" and "system df" in a and "-v" in a.split())]
            PROBE.check("D12", f"{label}: страж — настоящий docker-sweep.py не ходил", not real, real)
            seen[label] = (proc.returncode, [s.split()[1:] for s in sweeps], out)
        finally:
            shutil.rmtree(stand, ignore_errors=True)
    off_line = lambda out: any("выключена" in l and re.search(r"docker|DOCKER_SWEEP", l) for l in out.splitlines())
    for label in ("нет ключа", "ключ 0"):
        rc, sweeps, out = seen[label]
        PROBE.check("D12", f"{label}: код 0, строка «… docker … выключена», заглушка не звана",
                    rc == 0 and not sweeps and off_line(out), (rc, sweeps, out[-500:]))
    rc, sweeps, out = seen["ключ 1"]
    PROBE.check("D12", "ключ 1: заглушка звана 1 раз с --ночь и --разрешено",
                rc == 0 and len(sweeps) == 1 and {"--ночь", "--разрешено"} <= set(sweeps[0])
                and "--dry-run" not in sweeps[0], (rc, sweeps, out[-500:]))
    PROBE.check("D12", "ключ 1: список своих DOCKER_SWEEP_OWN доходит ключом «--свои ^jig-stand»",
                len(sweeps) == 1 and any(sweeps[0][i:i + 2] == ["--свои", "^jig-stand"]
                                         for i in range(len(sweeps[0]))), (sweeps, out[-500:]))
    rc, sweeps, out = seen["ключ 1 --dry-run"]
    PROBE.check("D12", "--dry-run демона: заглушка с --dry-run и без --разрешено",
                rc == 0 and len(sweeps) == 1 and "--dry-run" in sweeps[0] and "--разрешено" not in sweeps[0],
                (rc, sweeps, out[-500:]))
    rc, sweeps, out = seen["ключ 1 --по-давлению"]
    PROBE.check("D12", "--по-давлению: заглушка с --давление и --разрешено",
                len(sweeps) == 1 and {"--давление", "--разрешено"} <= set(sweeps[0]) and "--ночь" not in sweeps[0],
                (rc, sweeps, out[-500:]))


def case_d13(_stand: Path) -> None:
    """Выкат: вызов docker-sweep.py --после-выката, не фатальный, до итоговой строки (текстовая проба)."""
    lines = DEPLOY.read_text(encoding="utf-8").splitlines()
    final = next((i for i, l in enumerate(lines) if re.match(r'\s*say "deploy ok', l)), None)
    calls_at = [i for i, l in enumerate(lines)
                if not l.lstrip().startswith("#") and "docker-sweep.py" in l and "--после-выката" in l]
    PROBE.check("D13", "итоговая строка say \"deploy ok есть", final is not None)
    PROBE.check("D13", "вызов docker-sweep.py с --после-выката есть (не в комментарии)", bool(calls_at))
    ok = [i for i in calls_at if "|| say" in lines[i] and final is not None and i < final]
    PROBE.check("D13", "вызов не фатальный («|| say …») и стоит ДО «deploy ok»", bool(ok),
                [(i + 1, lines[i].strip()) for i in calls_at])


CASES = (
    ("D1", "сухой прогон", case_d1),
    ("D2", "без --разрешено — отказ", case_d2),
    ("D3", "правила образов", case_d3),
    ("D4", "правила контейнеров", case_d4),
    ("D5", "тома: две пометки и выдержка", case_d5),
    ("D5b", "том стал занятым", case_d5b),
    ("D6", "кэш сборки после выката", case_d6),
    ("D7", "порог давления 48 ч", case_d7),
    ("D8", "отказ docker rmi", case_d8),
    ("D9", "docker не установлен", case_d9),
    ("D10", "журнал 0600 и /dev/full", case_d10),
    ("D11", "запретные вызовы за все режимы", case_d11),
    ("D12", "демон: дверь и выключатель", case_d12),
    ("D13", "выкат зовёт уборку кэша", case_d13),
    ("C1", "пустой список своих — отказ", case_c1),
    ("C2", "чужой compose-проект цел", case_c2),
    ("C3", "контейнер с томом цел", case_c3),
    ("C4", "безымянный том чужого проекта цел", case_c4),
    ("M1", "возраст образа от появления", case_m1),
    ("M2", "кэш: итог — замер", case_m2),
    ("M4", "контейнер, остановленный руками, цел", case_m4),
)


def main() -> int:
    removed = sweep_dead_stands()
    print(f"модуль: {SWEEP}" + ("" if SWEEP.is_file() else " — НЕТ"))
    print(f"стендов умерших прогонов убрано: {removed}")
    for label, title, case in CASES:
        PROBE.start(label, title)
        stand = None
        try:
            stand = make_stand()
            case(stand)
        except subprocess.TimeoutExpired as error:
            PROBE.fail(label, f"превышен предел {error.timeout} с")
        except Exception as error:      # поломка пробы или механизма — красный случай, не трасса
            PROBE.fail(label, f"{type(error).__name__}: {error}")
        finally:
            if stand is not None:
                every = calls(stand)
                PROBE.check(label, "страж: запретных вызовов docker нет", not forbidden(every), forbidden(every))
                unknown = [c for c in every if verb(c) not in KNOWN]
                PROBE.check(label, "страж: незнакомых вызовов docker нет", not unknown, unknown)
                shutil.rmtree(stand, ignore_errors=True)
            PROBE.finish()
    print(f"проверок {PROBE.checks}")
    print(f"УБОРКА DOCKER: случаев {PROBE.cases}, неудач {PROBE.failed_cases}")
    return 0 if PROBE.failed_cases == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
