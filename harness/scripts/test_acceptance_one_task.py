#!/usr/bin/env python3
"""Проба `acceptance.py --задача <id>`: приёмка одной карточки, не всей карты.

Повтор приёмки карточки, чью пробу уже починили, ждал ночного таймера
acceptance-watch: очередь работ убирает такую карточку из «готовых», но сама
приёмка не идёт. `--задача` судит только названные id; с `--применить` пишет
их исходы в карту под общим локом dev-map.lock и не переписывает список
владельца — он собран по всей карте, а прогон видел одну карточку.

Стенд свой: дерево с копией прибора (scripts/lib — ссылка на настоящую),
карта с двумя карточками test, заглушка alert-task.py (ключ red:* — код 1),
install.conf и каталог журналов стенда. Судится настоящий вывод прибора.

    python3 scripts/test_acceptance_one_task.py

Дверь для мутаций ([[probe-loads-the-live-mechanism]]):
    HARNESS_ACCEPTANCE   путь acceptance.py под пробой
Последняя строка: `acceptance --задача: случаев N, падений F`.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

SUBJECT = Path(os.environ.get("HARNESS_ACCEPTANCE") or HERE / "acceptance.py")

cases = Cases()
check = cases.check

STUB_WRITER = '''#!/usr/bin/env python3
import sys
key = sys.argv[sys.argv.index("--is-green") + 1]
print("КРАСНОЕ " + key + ": заведена")
sys.exit(1)
'''

MAP = '''lines:
  - id: harness
    name: стенд
    tasks:
      - id: t.one
        status: test
        title: первая
        проверка: { команда: "python3 scripts/alert-task.py --is-green red:one", ждём: "ЗЕЛЁНОЕ red:one" }
      - id: t.two
        status: test
        title: вторая
        проверка: { команда: "python3 scripts/alert-task.py --is-green red:two", ждём: "ЗЕЛЁНОЕ red:two" }
'''


def build(root: Path) -> dict:
    proj, log = root / "proj", root / "log"
    (proj / "scripts").mkdir(parents=True)
    log.mkdir()
    shutil.copy2(SUBJECT, proj / "scripts" / "acceptance.py")
    (proj / "scripts" / "lib").symlink_to(HERE / "lib")
    (proj / "scripts" / "alert-task.py").write_text(STUB_WRITER, encoding="utf-8")
    (proj / "dev-map.yaml").write_text(MAP, encoding="utf-8")
    forms = root / "acceptance-forms.yaml"
    forms.write_text("по_имени: []\n", encoding="utf-8")
    conf = root / "install.conf"
    conf.write_text(f'PROJECT_DIR="{proj}"\nLOG_DIR="{log}"\n', encoding="utf-8")
    env = dict(os.environ, HARNESS_INSTALL_CONF=str(conf), HARNESS_CONF=str(root / "harness.conf"),
               PROJECT_DIR=str(proj), LOG_DIR=str(log), HARNESS_LOG_DIR=str(log),
               ACCEPTANCE_FORMS=str(forms), SERVICE_LOCKS_SUBDIR="services",
               HARNESS_MAP_LOCK_WAIT="1", PYTHONDONTWRITEBYTECODE="1")
    env.pop("HARNESS_IN_CHECKS_SLICE", None)
    return {"proj": proj, "log": log, "env": env, "map": proj / "dev-map.yaml",
            "queue": proj / "docs" / "приёмка" / "очередь-владельца.md",
            "lock": log / "locks" / "services" / "dev-map.lock"}


def run(stand: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(stand["proj"] / "scripts" / "acceptance.py"), *args],
                          cwd=stand["proj"], env=stand["env"], capture_output=True, text=True,
                          timeout=120)


def marked(stand: dict, ид: str) -> bool:
    """В блоке карточки стоит пометка провала."""
    текст = stand["map"].read_text(encoding="utf-8")
    блок = текст.split(f"- id: {ид}\n", 1)[1].split("\n      - id:", 1)[0]
    return "провал_проверки:" in блок


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="acceptance-one-task-") as tmp:
        stand = build(Path(tmp) / "a")

        print("── сухой прогон одной карточки ──")
        done = run(stand, "--задача", "t.one")
        check("сухой --задача: судит одну карточку", True, '"задач в test": 1' in done.stdout)
        check("сухой --задача: карта не тронута", MAP, stand["map"].read_text(encoding="utf-8"))
        done = run(stand, "--задача", "t.one,t.two")
        check("сухой --задача: две через запятую", True, '"задач в test": 2' in done.stdout)

        print("── --задача --применить ──")
        done = run(stand, "--задача", "t.one", "--применить")
        check("--применить: код 1 — провал судим", 1, done.returncode)
        check("--применить: в карте пометка у названной", True, marked(stand, "t.one"))
        check("--применить: соседняя карточка не тронута", False, marked(stand, "t.two"))
        check("--применить: вывод только названной в журнале", (True, False),
              ((stand["log"] / "priyomka" / "t.one.вывод").is_file(),
               (stand["log"] / "priyomka" / "t.two.вывод").is_file()))
        check("--применить: список владельца не переписан", False, stand["queue"].exists())

        print("── лок карты занят ──")
        stand["lock"].parent.mkdir(parents=True, exist_ok=True)
        before = stand["map"].read_text(encoding="utf-8")
        with open(stand["lock"], "a", encoding="utf-8") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            done = run(stand, "--задача", "t.two", "--применить")
        check("лок занят: код 2, не «прогон состоялся»", 2, done.returncode)
        check("лок занят: карта не тронута", before, stand["map"].read_text(encoding="utf-8"))
        check("лок занят: причина названа", True, "лок" in done.stdout + done.stderr)

        print("── негодный довод ──")
        done = run(stand, "--задача", "t.нет")
        check("карточки нет в test: код 2", 2, done.returncode)
        check("карточки нет в test: названа", True, "t.нет" in done.stdout + done.stderr)
        done = run(stand, "--задача")
        check("--задача без id: код 2", 2, done.returncode)

        print("── без --задача — вся карта, как прежде ──")
        full = build(Path(tmp) / "b")
        done = run(full, "--применить")
        check("вся карта: судит обе", True, '"задач в test": 2' in done.stdout)
        check("вся карта: пометки у обеих", (True, True), (marked(full, "t.one"), marked(full, "t.two")))
        check("вся карта: список владельца написан", True, full["queue"].is_file())

    print(f"acceptance --задача: случаев {cases.paths}, падений {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
