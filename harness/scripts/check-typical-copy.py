#!/usr/bin/env python3
"""Типовая чистая копия: прежний выпуск → обновление до нового → самопроверка без новых красных.

Зачем. Копия bkb 14.11.2026 ставила 0.12.1 (38c4ff1 → cbcfd82): у нас ворота зелёные,
у неё самопроверка нашла две новые красные — «библиотека процессов» и «изоляция tmux» —
и обновление откатилось. Чистое обновление пакета зелёное; красные встают от того, что
у любой живой копии своё: свой процесс в библиотеке и в кроне, свой .sh продукта. Наша
машина этого не видит: у неё HARNESS_DEV_GATES=1, свой tmux, свой пользователь.

Стенд: пакет копии — git-клон (прежний выпуск, затем новый коммит), проект копии —
свой git, чужие PROJECT_NAME и AGENT_USER, подставной crontab, окружение с нуля
(`env` без нашего TMUX и HARNESS_DEV_GATES), слой установки и корень harness — в стенде.
PROJECT_DIR — только в паспорте стенда, не в окружении: у живой копии его там нет
(update.py зовут диспетчер и демон без него), а с ним самотест раскладчика 0.13.0
раскладывал стенд в проект копии — красная, которой у копии не бывает (15.11.2026).
Раскладка и доустановка — те же, что зовёт update.py --поставить; между выпусками копия
заводит свой процесс по слову судьи.

Данные проекта — как у живой копии (bkb 15.11.2026 откатила 0.13.0 на «секретах» и
«указаниях», которых чистый стенд не видел): свой код продукта с паролем в адресе базы,
разбитым на два литерала, своя ORDERS.md и очередь сообщений владельца, а за часы
обновления владелец пишет новое указание.

Проверки — те, чей предмет у копии свой (harness/config/copy-subject-checks.yaml нового
выпуска), и те, что у нас под карантином ворот: карантин прятал красную от публикации,
а у копии его нет (0.13.0 ушёл с красными «признаки» под карантином хозяина). Идут до и
после; новая красная харнеса — отказ с именем и первыми строками вывода. Новая красная
проверки с предметом «данные» обновление не откатывает (update.py того же выпуска) — она
названа строкой, а не отказом.

    python3 scripts/check-typical-copy.py --старое <дерево выпуска> --новое <дерево выпуска> [--карантин <yaml>]
Вторая типовая копия — с продуктом-игрушкой (эталонный выкат §6.1): Dockerfile на alpine:3.20,
файл проверок с одним набором `image: артефакт`, ручка здоровья командой. Её deploy.sh идёт на
новом выпуске: `--план` — выход 0 и названная цепочка; `--проверка` — если docker и образ базы
есть (база — TYPICAL_COPY_TOY_IMAGE, умолчание alpine:3.20), иначе «пропущено: нет docker/образа».
Провал — красная «выкат продукта-игрушки». Контейнеры, образы и сети стенда убираются.
Дерево — корень сборки (в нём каталог harness/). Карантин — умолчанием наш
harness/config/gates-quarantine.yaml. Коды: 0 — новых красных харнеса нет,
1 — есть (названы), 2 — стенд не собрался.
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

STEP_TIMEOUT = 900
# Самопроверка — под тем же пределом, что у копии (UPDATE_GATES_TIMEOUT_SEC, умолчание
# update.py): один «самотест раскладчика» идёт полчаса, и 900 с обрывали стенд раньше вердикта.
SELF_CHECK_TIMEOUT = 5400
TOY_NAME = "typical-toy"  # латиница: имя образа и контейнеров стенда, docker кириллицу не принимает
TOY_BASE = "alpine:3.20"
TOY_RED = "выкат продукта-игрушки"
TOY_LINE = "типовая копия с продуктом-игрушкой: "
TOY_CHECKS = ("suites:\n  - name: na-artefakte\n    image: артефакт\n    base: alpine:3.20\n"
              "    install: 'true'\n    manifests: []\n    timeout_s: 60\n    steps:\n      - cmd: 'true'\n")
OWN_SCRIPT = "scripts/typical-backup.sh"
OWN_TMUX = "scripts/typical-preview.sh"
# Свой код продукта копии: пароль в адресе базы, разбитый на два литерала (находка bkb 10.11.2026).
OWN_PRODUCT = "app/settings.py"
OWN_PRODUCT_TEXT = 'DATABASE_URL = ("postgresql://typical:Typical2026pass"\n                "@db:5432/typical")\n'  # не-секрет: пароль стенда, судья обязан найти его в коде копии
# Сообщения владельца копии: занесённое указание, не указание и — за часы обновления — новое.
OWNER_MESSAGES = (("1794000000.1", "я тебе говорил искать по карте", 1794000000),
                  ("1794000100.2", "ок, спасибо", 1794000100))
OWNER_LATE = ("1794700000.3", "больше не перезапускай панель днём", 1794700000)
OWN_ORDER = "\n- искать дома по карте <!-- inbox:1794000000.1 -->\n"
HERE = Path(__file__).resolve().parent


def run(command: list, env: dict, cwd=None, timeout: int = STEP_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(command, env=env, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)


def git(where: Path, *args) -> None:
    subprocess.run(["git", "-C", str(where), "-c", "user.name=typical-copy",
                    "-c", "user.email=typical-copy@stand", *args],
                   check=True, capture_output=True, timeout=STEP_TIMEOUT)


class Stand:
    def __init__(self, base: Path, sample_conf: Path, name: str = "типовая-копия"):
        self.base, self.package, self.project = base, base / "пакет", base / "копия"
        self.crontab = base / "crontab"
        for folder in (self.package, self.project, base / "bin", base / "журналы", base / "дом", base / "корень"):
            folder.mkdir(parents=True)
        self.crontab.write_text("", encoding="utf-8")
        tool = base / "bin" / "crontab"
        tool.write_text('#!/bin/sh\n[ "$1" = -l ] && exec cat "$FAKE_CRON"\n'
                        '[ "$1" = - ] && exec cat > "$FAKE_CRON"\nexit 1\n', encoding="utf-8")
        tool.chmod(0o755)
        (base / "install.conf").write_text(
            f'PROJECT_DIR="{self.project}"\nPROJECT_NAME="{name}"\nAGENT_USER="kopiya"\n'
            f'LOG_DIR="{base}/журналы"\nHEARTBEAT_DIR="{base}/метки"\n', encoding="utf-8")
        conf = re.sub(r'(?m)^BACKUP_DIR=.*$', f'BACKUP_DIR="{base}/копии"',
                      sample_conf.read_text(encoding="utf-8"))
        (base / "harness.conf").write_text(conf, encoding="utf-8")
        self.env = {"PATH": f"{base}/bin:/usr/local/bin:/usr/bin:/bin", "HOME": f"{base}/дом",
                    "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HARNESS_SELFTEST": "1",
                    "HARNESS_HOME": f"{base}/корень", "HARNESS_INSTALL_CONF": f"{base}/install.conf",
                    "HARNESS_CONF": f"{base}/harness.conf", "HARNESS_DOOR_STAND_HOME": str(base),
                    "HARNESS_STARTER": str(self.package),
                    "HARNESS_PROCESS_LIBRARY_LOCAL": f"{base}/слой-процессов.json",
                    "FAKE_CRON": str(self.crontab)}
        git(self.package, "init", "-q")
        git(self.project, "init", "-q")
        (self.project / OWN_PRODUCT).parent.mkdir(parents=True)
        (self.project / OWN_PRODUCT).write_text(OWN_PRODUCT_TEXT, encoding="utf-8")
        git(self.project, "add", OWN_PRODUCT)
        git(self.project, "commit", "-q", "-m", "копия")

    def release(self, tree: Path, label: str) -> None:
        """Пакет копии уходит на выпуск tree (коммит в её клоне) и раскладывается в проект."""
        for item in self.package.iterdir():
            if item.name != ".git":
                shutil.rmtree(item) if item.is_dir() else item.unlink()
        shutil.copytree(tree, self.package, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        git(self.package, "add", "-A")
        git(self.package, "commit", "-q", "--allow-empty", "-m", label)
        for command in (["bash", str(self.package / "harness" / "razlozhit-harnes.sh")],
                        [sys.executable, str(self.project / "scripts" / "install-new.py")]):
            done = run(command, self.env)
            if done.returncode != 0:
                tail = (done.stdout + done.stderr).strip().splitlines()[-3:]
                raise RuntimeError(f"{label}: {Path(command[1]).name} код {done.returncode}: {' | '.join(tail)}")

    def own_process(self) -> None:
        """Копия заводит свой процесс по слову судьи и свой .sh продукта."""
        for rel, text in ((OWN_SCRIPT, "#!/bin/sh\ntrue\n"),
                          (OWN_TMUX, '#!/bin/sh\ntmux new-session -d -s typical-preview "sleep 1"\n')):
            (self.project / rel).write_text(text, encoding="utf-8")
            (self.project / rel).chmod(0o755)
        config = self.project / "harness" / "config"
        library = json.loads((config / "process-library.json").read_text(encoding="utf-8"))
        library["процессы"].append({"id": "typical-backup", "название": "Копия базы продукта", "класс": "фоновый",
                                    "запуск": "крон", "статус": "работает", "файлы": [OWN_SCRIPT]})
        (config / "process-library.json").write_text(json.dumps(library, indent=2, ensure_ascii=False) + "\n",
                                                     encoding="utf-8")
        schedule = config / "process-library.yaml"
        schedule.write_text(re.sub(r"(?m)^(процессы:.*\n)",
                                   r'\1  typical-backup:\n    запуск: крон\n    расписание: "0 4 * * *"\n'
                                   f'    команда: "{OWN_SCRIPT}"\n    журнал: typical-backup.log\n',
                                   schedule.read_text(encoding="utf-8"), count=1), encoding="utf-8")
        with self.crontab.open("a", encoding="utf-8") as cron:
            cron.write(f"0 4 * * * {self.project}/scripts/run-background.sh typical-backup -- "
                       f"{self.project}/{OWN_SCRIPT} >>{self.base}/журналы/typical-backup.log 2>&1\n")
        orders = self.project / "память" / "ORDERS.md"
        orders.write_text(orders.read_text(encoding="utf-8") + OWN_ORDER, encoding="utf-8")
        for message in OWNER_MESSAGES:
            self.owner_says(*message)

    def owner_says(self, ident: str, text: str, when: int) -> None:
        folder = self.base / "журналы" / "inbox" / "обработано"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{ident}.txt").write_text(text, encoding="utf-8")
        os.utime(folder / f"{ident}.txt", (when, when))

    def add_toy_product(self, base_image: str) -> None:
        """Продукт-игрушка: свой git, Dockerfile, файл проверок; ключи выката — в конфиг копии."""
        product = self.base / "product"
        (product / "deploy").mkdir(parents=True)
        (product / "Dockerfile").write_text(f'FROM {base_image}\nCMD ["sleep", "300"]\n', encoding="utf-8")
        (product / "deploy" / "server-checks.yaml").write_text(TOY_CHECKS, encoding="utf-8")
        git(product, "init", "-q")
        git(product, "add", "-A")
        git(product, "commit", "-q", "-m", "игрушка")
        keys = (f'PRODUCT_REPO="{product}"\nSTACK_IMAGES="{TOY_NAME}"\n'
                f"STACK_BUILD_CMD='docker build -q -t {TOY_NAME}:${{IMAGE_TAG:-latest}} .'\n"
                'STACK_HEALTH_CMD="true"\nDEPLOY_HEALTH_WAIT_SEC="4"\n'
                'STACK_CHECKS_FILE="deploy/server-checks.yaml"\nSTACK_CHECK_MIN_FREE_MB="512"\n'
                f'STACK_CHECK_UID="{os.getuid()}:{os.getgid()}"\n')
        conf = self.base / "harness.conf"
        conf.write_text(conf.read_text(encoding="utf-8") + "\n" + keys, encoding="utf-8")
        self.env.update({"DEPLOY_CHECK_WORK_ROOT": str(self.base / "копии-проверок"),
                         "DEPLOY_CHECK_LOG_ROOT": str(self.base / "журналы-проверок")})

    def deploy(self, key: str) -> subprocess.CompletedProcess:
        return run(["bash", str(self.project / "scripts" / "deploy.sh"), key], self.env, cwd=self.project)

    def clean_docker(self) -> None:
        """Контейнеры наборов, сети и образы игрушки не остаются: всё — по имени установки."""
        for listing, remove in ((["ps", "-aq"], ["rm", "-f"]), (["network", "ls", "-q"], ["network", "rm"])):
            found = run(["docker", *listing, "--filter", f"name={TOY_NAME}"], self.env)
            ids = found.stdout.split() if found.returncode == 0 else []
            if ids:
                run(["docker", *remove, *ids], self.env)
        images = run(["docker", "images", "-q", TOY_NAME], self.env)
        if images.returncode == 0 and images.stdout.split():
            run(["docker", "rmi", "-f", *dict.fromkeys(images.stdout.split())], self.env)

    def self_check(self, checks: list) -> str:
        done = run(["bash", str(self.project / "scripts" / "gates.sh"), "--только", "|".join(checks)],
                   self.env, cwd=self.project, timeout=SELF_CHECK_TIMEOUT)
        return done.stdout + done.stderr


def checks_of(tree: Path, quarantine: Path) -> tuple:
    """(проверки стенда, имена с предметом «данные»): предмет копии из нового выпуска и наш карантин."""
    subject = (yaml.safe_load((tree / "harness" / "config" / "copy-subject-checks.yaml")
                              .read_text(encoding="utf-8")) or {}).get("проверки") or []
    held = ((yaml.safe_load(quarantine.read_text(encoding="utf-8")) or {}).get("карантин") or []
            if quarantine.is_file() else [])
    names = [entry["имя"] for entry in subject] + [entry["гейт"] for entry in held]
    return list(dict.fromkeys(names)), {entry["имя"] for entry in subject if entry.get("предмет") == "данные"}


def default_quarantine() -> Path:
    # Проект кладёт конфиги в harness/config, пакет (судья в harness/scripts) — в config.
    for folder in (HERE.parent / "harness" / "config", HERE.parent / "config"):
        if (folder / "gates-quarantine.yaml").is_file():
            return folder / "gates-quarantine.yaml"
    return HERE.parent / "harness" / "config" / "gates-quarantine.yaml"


def reds_parser(tree: Path):
    """Разбор красных — тот же, что у самопроверки update.py нового выпуска."""
    spec = importlib.util.spec_from_file_location("typical_copy_update", tree / "harness" / "scripts" / "update.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.красные_из_вывода


def docker_has(image: str) -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=60,
                          stdin=subprocess.DEVNULL).returncode == 0


def toy_product_lines(new: Path, sample_conf: Path, base_image: str) -> list:
    """Вторая типовая копия: выкат продукта-игрушки на новом выпуске. Строки итога; провал — «новая красная»."""
    work = Path(tempfile.mkdtemp(prefix="proba-typical-copy-toy-"))
    stand = None
    try:
        stand = Stand(work, sample_conf, name=TOY_NAME)
        stand.release(new, "новый выпуск")
        stand.add_toy_product(base_image)
        plan = stand.deploy("--план")
        said = plan.stdout + plan.stderr
        if plan.returncode != 0 or "сборка: docker build" not in said or "тесты (этап T)" not in said:
            return [f"типовая копия: новая красная «{TOY_RED}»: --план код {plan.returncode}: "
                    + said.strip()[-300:]]
        lines = [f"{TOY_LINE}--план код 0, цепочка названа"]
        if not docker_has(base_image):
            return lines + [f"{TOY_LINE}--проверка пропущено: нет docker/образа {base_image}"]
        check = stand.deploy("--проверка")
        said = check.stdout + check.stderr
        if check.returncode != 0 or "success" not in said:
            return lines + [f"типовая копия: новая красная «{TOY_RED}»: --проверка код {check.returncode}: "
                            + said.strip()[-300:]]
        return lines + [f"{TOY_LINE}--проверка: тесты success"]
    except (RuntimeError, OSError, subprocess.SubprocessError) as trouble:
        return [f"типовая копия: новая красная «{TOY_RED}»: стенд игрушки не собрался — {trouble}"]
    finally:
        if stand is not None and shutil.which("docker"):
            stand.clean_docker()
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--старое", required=True, type=Path)
    ap.add_argument("--новое", required=True, type=Path)
    ap.add_argument("--карантин", type=Path, default=default_quarantine())
    args = ap.parse_args()
    old, new = args.старое.resolve(), args.новое.resolve()
    base = Path(tempfile.mkdtemp(prefix="proba-typical-copy-"))
    try:
        checks, data_checks = checks_of(new, args.карантин)
        stand = Stand(base, old / "harness" / "config" / "harness.conf")
        stand.release(old, "прежний выпуск")
        stand.own_process()
        parse = reds_parser(new)
        before = parse(stand.self_check(checks))
        stand.owner_says(*OWNER_LATE)
        stand.release(new, "новый выпуск")
        after_text = stand.self_check(checks)
        after = parse(after_text)
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError) as trouble:
        print(f"типовая копия: стенд не собрался — {trouble}")
        return 2
    finally:
        shutil.rmtree(base, ignore_errors=True)
    if before is None or after is None:
        print("типовая копия: итог самопроверки не прочитан — " + after_text.strip()[-300:])
        return 2
    for name in sorted(name for name in after if name in data_checks):
        print(f"типовая копия: красная в данных копии «{name}» (откат её не снимает — обновление встанет и "
              f"назовёт): {' | '.join(after.причины.get(name, [])) or 'вывода нет'}")
    fresh = sorted(name for name in after - before if name not in data_checks)
    for name in fresh:
        print(f"типовая копия: новая красная «{name}»: {' | '.join(after.причины.get(name, [])) or 'вывода нет'}")
    toy = toy_product_lines(new, old / "harness" / "config" / "harness.conf",
                            os.environ.get("TYPICAL_COPY_TOY_IMAGE") or TOY_BASE)
    print("\n".join(toy))
    if fresh or any("новая красная" in line for line in toy):
        return 1
    print(f"типовая копия: новых красных нет (харнеса; проверок {len(checks)}, красных до {sum(before.values())}, "
          f"после {sum(after.values())})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
