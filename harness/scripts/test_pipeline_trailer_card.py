#!/usr/bin/env python3
"""Проба: гейт пайплайна судит крупную работу по карточке из трейлера Dev-Map.

16.11 в карте было пять wip, и
правило «ровно одна wip на всю карту» краснило каждый крупный коммит и каждое
слияние: смена и помощники ходили дверью HARNESS_PIPELINE_FORCE.

Контракт, который судится:
  * pre-commit (сообщения ещё нет), wip две и больше, работа крупная — суд
    откладывается до стадии commit-msg (код 0, слово «commit-msg» в выводе);
  * commit-msg: трейлер называет wip-карточку со спекой и ревью — код 0;
    трейлер называет карточку без spec: — код 1, отказ про spec:;
    трейлера нет — код 1, отказ называет число wip и дверь «Dev-Map: <id>»;
  * одна wip без трейлера — судится на pre-commit, как прежде;
  * очередь слияний (commit-msg нет): трейлеры ветки приходят переменной
    HARNESS_PIPELINE_DEV_MAP; пустая — отказ сразу, без откладывания;
  * merge-queue.branch_trailers берёт id только из коммитов ветки.

Окружение своё: git-репозиторий во временном каталоге с копией scripts/,
свой harness.conf, LOG_DIR, доска. Дверь мутаций: PIPELINE_GATE_UNDER_TEST —
путь к check-pipeline.py, который судится (кладётся в стенд вместо копии);
MERGE_QUEUE_UNDER_TEST — путь к merge-queue.py (рядом с ним нужен lib/).

    python3 scripts/test_pipeline_trailer_card.py
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent
GATE = Path(os.environ.get("PIPELINE_GATE_UNDER_TEST") or HERE / "check-pipeline.py").resolve()
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases                          # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="pipeline-trailer-probe-"))
REPO = WORK / "repo"
LOG_DIR = WORK / "log"
CONF = WORK / "harness.conf"
LOG_DIR.mkdir()
(WORK / "install.conf").write_text(f'LOG_DIR="{LOG_DIR}"\n', encoding="utf-8")
CONF.write_text('PROD_PATHS="app/"\nGUARD_PATHS="scripts/hooks/"\nAUTOMATION_LEVEL="full"\n',
                encoding="utf-8")
os.environ.update({
    "HARNESS_HOME": str(WORK / "home"),
    "HARNESS_INSTALL_CONF": str(WORK / "install.conf"),
    "HARNESS_CONF": str(CONF),
    "LOG_DIR": str(LOG_DIR),
    "HARNESS_LOG_DIR": str(LOG_DIR),
    "HARNESS_DOSKA": str(WORK / "doska.json"),
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
})
for door in ("HARNESS_PIPELINE_FORCE", "HARNESS_PIPELINE_WHY", "HARNESS_PIPELINE_DEV_MAP",
             "HARNESS_BACKLOG_OWNER_WORD", "HACKATHON_UNTIL"):
    os.environ.pop(door, None)

cases = Cases()
TODAY = subprocess.run(["date", "+%Y-%m-%d"], capture_output=True, text=True).stdout.strip()
WIP_IDS = ["hr.w1", "hr.w2", "hr.w3", "hr.w4", "hr.w5"]


def git(*args, where=REPO):
    return subprocess.run(["git", "-C", str(where), "-c", "user.name=proba", "-c",
                           "user.email=p@p", *args], capture_output=True, text=True, timeout=60)


def card(task_id, status="wip", spec=True):
    rows = [f"      - id: {task_id}", f"        status: {status}",
            f'        title: "Задача {task_id}"', f'        summary: "образец пробы {task_id}"',
            f"        глубина: {{ уровень: крупная, характер: разработка, кем: смена, "
            f'когда: "{TODAY}" }}',
            f'        when: {{ started: "{TODAY}" }}']
    if spec:
        rows.append('        spec: "docs/spec.md"')
    return "\n".join(rows)


def map_text(cards_text):
    return ("version: 1\nqueue: []\nlayers:\n  - { id: infra, label: \"Инфра\", color: \"#5aa06a\" }\n"
            "lines:\n  - id: core\n    layer: infra\n    name: \"Ядро\"\n    tasks:\n"
            + "\n".join(cards_text) + "\n")


def build_repo():
    (REPO / "scripts" / "hooks").mkdir(parents=True)
    for source in [*(SOURCE / "scripts").glob("*.py"), *(SOURCE / "scripts").glob("*.sh")]:
        shutil.copy2(source, REPO / "scripts" / source.name)
    shutil.copy2(GATE, REPO / "scripts" / "check-pipeline.py")
    shutil.copytree(SOURCE / "scripts" / "lib", REPO / "scripts" / "lib",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(SOURCE / "scripts" / "hooks" / "guard_bash.py", REPO / "scripts" / "hooks")
    (REPO / "harness" / "config").mkdir(parents=True)
    shutil.copy2(SOURCE / "harness" / "config" / "pipeline.yaml", REPO / "harness" / "config")
    (REPO / "docs").mkdir()
    (REPO / "docs" / "spec.md").write_text("спека случая\n", encoding="utf-8")
    (REPO / "docs" / "spec-REVIEW.md").write_text("ревью спеки случая\n", encoding="utf-8")
    (REPO / "dev-map.yaml").write_text(map_text([card("hr.p", "plan")]), encoding="utf-8")
    git("init", "-q", "-b", "master")
    git("add", "-A")
    git("commit", "-qm", "стенд")


def stage(args, env):
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "check-pipeline.py"), *args],
                          capture_output=True, text=True, timeout=180, cwd=REPO, env=env)
    return done.returncode, done.stdout + done.stderr


def run(map_cards, message, extra_env=None, commit_msg=True):
    """Карта map_cards — в HEAD; в индекс — крупная правка (новый файл прода, 350 строк).

    Стадии как у git: pre-commit, затем commit-msg с message, если pre-commit
    пропустил. Вывод — (код pre-commit, код commit-msg или None, весь вывод)."""
    (REPO / "dev-map.yaml").write_text(map_text(map_cards), encoding="utf-8")
    git("add", "dev-map.yaml")
    git("commit", "-qm", "карта случая")
    # Каждый случай — свой новый файл: признак «новый файл» входит в цену.
    (REPO / "app").mkdir(exist_ok=True)
    big = REPO / "app" / f"big{len(list((REPO / 'app').iterdir()))}.py"
    big.write_text("".join(f"x{i} = {i}\n" for i in range(350)), encoding="utf-8")
    git("add", str(big.relative_to(REPO)))
    env = {**os.environ, **(extra_env or {})}
    pre, out = stage([], env)
    second = None
    if pre == 0 and commit_msg:
        message_file = WORK / "message.txt"
        message_file.write_text(message, encoding="utf-8")
        second, more = stage(["--commit-msg", str(message_file)], env)
        out += more
    git("commit", "-qm", "правка случая")
    return pre, second, out


def five_wip(spec_of=lambda task_id: True):
    return [card(task_id, spec=spec_of(task_id)) for task_id in WIP_IDS] + [card("hr.p", "plan")]


def gate_cases():
    build_repo()

    pre, second, out = run(five_wip(), "правка\n\nDev-Map: hr.w1\n")
    cases.check("Т-1 пять wip, трейлер hr.w1 со спекой: pre-commit 0, commit-msg 0",
                (0, 0), (pre, second))
    cases.check("Т-1 pre-commit назвал отложенный суд словом «commit-msg»", True, "commit-msg" in out)
    cases.check("Т-1 commit-msg судил именно hr.w1: «hr.w1: крупная … спека и ревью на месте»",
                True, "hr.w1: крупная" in out and "спека и ревью на месте" in out)

    pre, second, out = run(five_wip(), "правка без трейлера\n")
    cases.check("Т-2 пять wip, трейлера нет: pre-commit 0 (отложил), commit-msg 1", (0, 1),
                (pre, second))
    cases.check("Т-2 отказ называет число «wip — 5» и дверь «Dev-Map: <id>»", (True, True),
                ("wip — 5" in out, "Dev-Map: <id>" in out))

    pre, second, out = run(five_wip(lambda task_id: task_id != "hr.w2"), "правка\n\nDev-Map: hr.w2\n")
    cases.check("Т-3 трейлер на hr.w2 без spec: — commit-msg 1, отказ про spec:", (0, 1, True),
                (pre, second, "spec:" in out and "hr.w2" in out))

    pre, second, out = run(five_wip(), "правка\n\nDev-Map: hr.p\n")
    cases.check("Т-4 трейлер на карточку в plan — задача не названа: commit-msg 1, «wip — 5»",
                (0, 1, True), (pre, second, "wip — 5" in out))

    pre, _, out = run([card("hr.w1"), card("hr.p", "plan")], "", commit_msg=False)
    cases.check("Т-5 одна wip без трейлера — суд на pre-commit, как прежде: код 0, «hr.w1: крупная»",
                (0, True, False), (pre, "hr.w1: крупная" in out, "commit-msg" in out))
    pre, _, out = run([card("hr.w1", spec=False), card("hr.p", "plan")], "", commit_msg=False)
    cases.check("Т-6 одна wip без spec: — pre-commit 1, отказ про spec:", (1, True),
                (pre, "spec:" in out))

    pre, _, out = run(five_wip(), "", {"HARNESS_PIPELINE_DEV_MAP": "hr.w1"}, commit_msg=False)
    cases.check("Т-7 очередь: трейлеры ветки «hr.w1» переменной — pre-commit 0 и суд hr.w1",
                (0, True), (pre, "hr.w1: крупная" in out))
    pre, _, out = run(five_wip(lambda task_id: task_id != "hr.w3"), "",
                      {"HARNESS_PIPELINE_DEV_MAP": "hr.w3"}, commit_msg=False)
    cases.check("Т-8 очередь: трейлер ветки на hr.w3 без spec: — pre-commit 1", (1, True),
                (pre, "spec:" in out))
    pre, _, out = run(five_wip(), "", {"HARNESS_PIPELINE_DEV_MAP": ""}, commit_msg=False)
    cases.check("Т-9 очередь: трейлеров у ветки нет — отказ сразу, «wip — 5» и дверь трейлера",
                (1, True, True), (pre, "wip — 5" in out, "Dev-Map: <id>" in out))


def queue_cases():
    queue = os.environ.get("MERGE_QUEUE_UNDER_TEST") or HERE / "merge-queue.py"
    spec = importlib.util.spec_from_file_location("merge_queue_probe", queue)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    where = WORK / "queue"
    where.mkdir()
    git("init", "-q", "-b", "master", where=where)
    for number, message in enumerate(("база\n\nDev-Map: hr.master-only\n",
                                      "ветка 1\n\nDev-Map: hr.w1\n",
                                      "ветка 2\n\nDev-Map: hr.w2, hr.w1\n")):
        (where / f"f{number}.txt").write_text(str(number), encoding="utf-8")
        git("add", "-A", where=where)
        git("commit", "-qm", message, where=where)
        if number == 0:
            git("checkout", "-qb", "work", where=where)
    tip = git("rev-parse", "work", where=where).stdout.strip()
    cases.check("Т-10 merge-queue.branch_trailers: id только коммитов ветки, без повторов",
                "hr.w2 hr.w1", module.branch_trailers(where, tip))


def main():
    try:
        gate_cases()
        queue_cases()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    print(f"{'ЗЕЛЁНЫЙ' if cases.ok else 'КРАСНЫЙ'}: путей {cases.paths}, провалов {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
