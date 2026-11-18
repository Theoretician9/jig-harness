#!/usr/bin/env python3
"""Типовая копия: самая старая версия у копий → update.py --поставить нового → без новых красных.

Зачем. Копия bkb 14.11.2026 ставила 0.12.1 (38c4ff1 → cbcfd82): у нас ворота зелёные,
у неё самопроверка нашла две новые красные — «библиотека процессов» и «изоляция tmux» —
и обновление откатилось. Чистое обновление пакета зелёное; красные встают от того, что
у любой живой копии своё: свой процесс в библиотеке и в кроне, свой .sh продукта. Наша
машина этого не видит: у неё HARNESS_DEV_GATES=1, свой tmux, свой пользователь.
Владелец 17.11.2026, после пятого отката у bkb: выпуск ставится ТЕМ ЖЕ процессом, что у
копии (`update.py --поставить`), с САМОЙ СТАРОЙ версии, что стоит у копий, — тогда выпуск,
который у копии откатится, не уйдёт.

Старое дерево. Версии копий — поле «версия_харнеса» страниц серверы/<копия>.md сети
эволюции (`--копии`, умолчание EVOLUTION_DIR/серверы из harness.conf). Из текста берутся
номер выпуска (`0.9.1` → метка v0.9.1), хэш публичного коммита или хэш пакета из
заголовка сборки («версия от …, пакет <хэш>») — и только то, что нашлось в истории
сборки (`--новое` — git publish.sh с публичной веткой и метками). Старейший по времени
коммита — старое дерево. Не узнанная версия названа строкой; не узнано ни одной — старое
дерево `--старое` (прошлый выпуск): путь стенда тот же, стенд один.

Стенд: публичный репозиторий стенда (выпуски — его коммиты), пакет копии — его клон,
проект копии — свой git, чужие PROJECT_NAME и AGENT_USER, подставной crontab, окружение
с нуля (`env` без нашего TMUX и HARNESS_DEV_GATES), слой установки и корень harness — в
стенде. PROJECT_DIR — только в паспорте стенда, не в окружении: у живой копии его там нет
(update.py зовут диспетчер и демон без него), а с ним самотест раскладчика 0.13.0
раскладывал стенд в проект копии — красная, которой у копии не бывает (15.11.2026).
Прежний выпуск ставится раскладкой и доустановкой; копия заводит свой процесс по слову
судьи и коммитит своё в проект. Новый выпуск уходит коммитом в публичный стенда, и ставит
его `update.py --поставить` проекта копии — прежнего выпуска, как диспетчер копии: тот
передаёт постановку обновлятору нового, а он гонит ворота до, раскладку, ворота после и
откат. Обновлятор копии старше договора перезапуска (sample-26 и bkb на 38c4ff1) передать не
умеет и сам откатывает на любой новой красной: такой копии агент харнеса шлёт команду
перехода, и стенд ставит ею (КОМАНДА_ПЕРЕХОДА нового выпуска), назвав это строкой. Наружу стенд не ходит: `sudo` стенда — заглушка с записью (перезапуск служб
update.py звал бы НАШИ службы), канал владельца выключен (HARNESS_TG_SEND пуст).

Данные проекта — как у живой копии (bkb 15.11.2026 откатила 0.13.0 на «секретах» и
«указаниях», которых чистый стенд не видел): свой код продукта с паролем в адресе базы,
разбитым на два литерала, своя ORDERS.md и очередь сообщений владельца, а за часы
обновления владелец пишет новое указание.

Проверки — выборка, которую `bash` стенда подставляет в зов gates.sh от update.py: полный
набор харнеса на стенде шёл бы часами. В выборке — проверки, чей предмет у копии свой
(harness/config/copy-subject-checks.yaml нового выпуска), главные проверки отката
(harness/config/update-rollback.yaml нового выпуска, если он есть) и те, что у нас под
карантином ворот: карантин прятал красную от публикации, а у копии его нет (0.13.0 ушёл
с красными «признаки» под карантином хозяина). Отказ — update.py не поставил (с его
причиной и красными) ИЛИ в воротах после есть новая красная харнеса против ворот до.
Новая красная проверки с предметом «данные» обновление не откатывает — она названа
строкой, а не отказом. Свой код копии (коммит проекта, строка crontab, свои .sh) после
постановки обязан быть цел.

    python3 scripts/check-typical-copy.py --старое <дерево выпуска> --новое <сборка> [--карантин <yaml>] [--копии <каталог>]
Вторая типовая копия — с продуктом-игрушкой (эталонный выкат §6.1): Dockerfile на alpine:3.20,
файл проверок с одним набором `image: артефакт`, ручка здоровья командой. Её deploy.sh идёт на
новом выпуске: `--план` — выход 0 и названная цепочка; `--проверка` — если docker и образ базы
есть (база — TYPICAL_COPY_TOY_IMAGE, умолчание alpine:3.20), иначе «пропущено: нет docker/образа».
Провал — красная «выкат продукта-игрушки». Контейнеры, образы и сети стенда убираются.
Дерево — корень сборки (в нём каталог harness/). Карантин — умолчанием наш
harness/config/gates-quarantine.yaml. Коды: 0 — новых красных харнеса нет,
1 — есть или update.py не поставил (названы), 2 — стенд не собрался.
"""
import argparse
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import config  # noqa: E402
from patterns import ЗАГОЛОВОК_РЕЛИЗА  # noqa: E402

STEP_TIMEOUT = 900
# Самопроверка — под тем же пределом, что у копии (UPDATE_GATES_TIMEOUT_SEC, умолчание
# update.py): один «самотест раскладчика» идёт полчаса, и 900 с обрывали стенд раньше вердикта.
SELF_CHECK_TIMEOUT = 5400
# Постановка: ворота до и после, каждые под пределом копии, раскладка и раскладка отката.
UPDATE_TIMEOUT = 2 * SELF_CHECK_TIMEOUT + 4 * STEP_TIMEOUT
UPDATE_LINE = "[обновление]"
RELAYED_LINES = 30
VERSION_FIELD = "версия_харнеса"
RELEASE_NUMBER = re.compile(r"\b(\d+\.\d+\.\d+)\b")
HEX = re.compile(r"\b([0-9a-f]{7,40})\b")
FOREIGN_TEXT_CAP = 120  # текст страницы копии — чужой: в вывод замка не длиннее строки
# bash стенда: одиночный зов gates.sh (так зовёт update.py) идёт выборкой стенда, ход каждого
# прогона — в ворота-N.log. Прочие зовы bash — как есть.
GATES_SHIM = """#!/bin/bash
if [ "$#" = 1 ] && [[ "$1" == *scripts/gates.sh ]]; then
    n=$(( $(ls "@BASE@"/ворота-*.log 2>/dev/null | wc -l) + 1 ))
    /bin/bash "$1" --только "$(cat "@BASE@/выборка")" 2>&1 | tee "@BASE@/ворота-$n.log"
    rc=${PIPESTATUS[0]}
    if [ "$n" = 1 ] && [ -x "@BASE@/между-воротами" ]; then "@BASE@/между-воротами"; fi
    exit "$rc"
fi
exec /bin/bash "$@"
"""
# sudo стенда: update.py перезапускает службы через `sudo -n systemctl` — у нас это НАШИ службы.
# Перезапуск записан и «удался», прочее — отказ, как у копии без прав.
SUDO_SHIM = """#!/bin/sh
echo "$*" >> "@BASE@/sudo.log"
[ "$1" = -n ] && [ "$2" = systemctl ] && exit 0
exit 1
"""
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
OWN_CRON = "run-background.sh typical-backup -- "
# Договор перезапуска обновлятора (update.py МЕТКА_ПЕРЕЗАПУСКА, 14.11.2026). Обновлятор копии
# старше него сам ведёт постановку новой версии, и откатывает любая новая красная, — этого
# выпуск не исправит. Такой копии агент харнеса шлёт команду перехода (КОМАНДА_ПЕРЕХОДА
# нового выпуска); стенд ставит ею же, иначе каждый выпуск отказывал бы на данных копии.
RELAUNCH_MARK = "HARNESS_UPDATE_RELAUNCHED"
BRIDGE_LINE = "типовая копия: обновлятор копии без перезапуска — ставит команда перехода нового выпуска"


def run(command: list, env: dict, cwd=None, timeout: int = STEP_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(command, env=env, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)


def git(where: Path, *args) -> str:
    return subprocess.run(["git", "-C", str(where), "-c", "user.name=typical-copy",
                           "-c", "user.email=typical-copy@stand", *args],
                          check=True, capture_output=True, text=True, timeout=STEP_TIMEOUT).stdout.strip()


def git_answer(where: Path, *args) -> str | None:
    """Ответ git или None, если git отказал: вопрос к истории сборки, а не действие."""
    done = subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True,
                          timeout=STEP_TIMEOUT, stdin=subprocess.DEVNULL)
    return done.stdout.strip() if done.returncode == 0 else None


def tool(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class Stand:
    def __init__(self, base: Path, sample_conf: Path, name: str = "типовая-копия"):
        self.base, self.package, self.project = base, base / "пакет", base / "копия"
        self.public, self.crontab, self.own_commit = base / "публичный", base / "crontab", ""
        for folder in (self.public, self.package, self.project, base / "bin", base / "журналы", base / "дом",
                       base / "корень"):
            folder.mkdir(parents=True)
        self.crontab.write_text("", encoding="utf-8")
        tool(base / "bin" / "crontab", '#!/bin/sh\n[ "$1" = -l ] && exec cat "$FAKE_CRON"\n'
                                       '[ "$1" = - ] && exec cat > "$FAKE_CRON"\nexit 1\n')
        tool(base / "bin" / "bash", GATES_SHIM.replace("@BASE@", str(base)))
        tool(base / "bin" / "sudo", SUDO_SHIM.replace("@BASE@", str(base)))
        (base / "install.conf").write_text(
            f'PROJECT_DIR="{self.project}"\nPROJECT_NAME="{name}"\nAGENT_USER="kopiya"\n'
            f'LOG_DIR="{base}/журналы"\nHEARTBEAT_DIR="{base}/метки"\nPANEL_STATE_DIR="{base}/панель"\n',
            encoding="utf-8")
        conf = re.sub(r'(?m)^BACKUP_DIR=.*$', f'BACKUP_DIR="{base}/копии"',
                      sample_conf.read_text(encoding="utf-8"))
        (base / "harness.conf").write_text(conf, encoding="utf-8")
        self.env = {"PATH": f"{base}/bin:/usr/local/bin:/usr/bin:/bin", "HOME": f"{base}/дом",
                    "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HARNESS_SELFTEST": "1",
                    "HARNESS_HOME": f"{base}/корень", "HARNESS_INSTALL_CONF": f"{base}/install.conf",
                    "HARNESS_CONF": f"{base}/harness.conf", "HARNESS_DOOR_STAND_HOME": str(base),
                    "HARNESS_STARTER": str(self.package),
                    "HARNESS_PROCESS_LIBRARY_LOCAL": f"{base}/слой-процессов.json",
                    "FAKE_CRON": str(self.crontab), "HARNESS_TG_SEND": ""}
        git(self.public, "init", "-q", "-b", "master")
        git(self.package, "init", "-q", "-b", "master")
        git(self.package, "remote", "add", "origin", str(self.public))
        git(self.project, "init", "-q")
        (self.project / OWN_PRODUCT).parent.mkdir(parents=True)
        (self.project / OWN_PRODUCT).write_text(OWN_PRODUCT_TEXT, encoding="utf-8")
        git(self.project, "add", OWN_PRODUCT)
        git(self.project, "commit", "-q", "-m", "копия")

    def publish(self, tree: Path, label: str) -> None:
        """Выпуск tree — коммит публичной ветки стенда; пакет копии его ещё не видел."""
        for item in self.public.iterdir():
            if item.name != ".git":
                shutil.rmtree(item) if item.is_dir() else item.unlink()
        shutil.copytree(tree, self.public, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        git(self.public, "add", "-A")
        git(self.public, "commit", "-q", "--allow-empty", "-m", label)

    def release(self, tree: Path, label: str) -> None:
        """Установка с нуля: выпуск tree — в публичный, пакет копии — на него, раскладка в проект."""
        self.publish(tree, label)
        git(self.package, "fetch", "-q", "origin", "master")
        git(self.package, "checkout", "-q", "-B", "master", "FETCH_HEAD")
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
        git(self.project, "add", "-A")
        # Мимо крюка коммита копии: свой код живой копии лёг раньше её ворот (пароль bkb в коде),
        # а суд здесь — обновлению, а не коммиту копии.
        git(self.project, "commit", "-q", "--no-verify", "-m", "свой процесс копии")
        self.own_commit = git(self.project, "rev-parse", "HEAD")

    def relaunches(self) -> bool:
        """Умеет ли обновлятор копии передать постановку обновлятору нового выпуска."""
        return RELAUNCH_MARK in (self.project / "scripts" / "update.py").read_text(encoding="utf-8")

    def update(self, checks: list, bridge: str | None) -> subprocess.CompletedProcess:
        """Постановка, как у копии: update.py её установки из проекта, ворота — выборкой стенда.
        Обновлятор копии без перезапуска — команда перехода нового выпуска (`bridge`)."""
        (self.base / "выборка").write_text("|".join(checks), encoding="utf-8")
        command = ([sys.executable, str(self.project / "scripts" / "update.py"), "--поставить"]
                   if bridge is None else ["bash", "-c", bridge.format(проект=shlex.quote(str(self.project)))])
        return run(command, self.env, cwd=self.project, timeout=UPDATE_TIMEOUT)

    def gate_runs(self) -> list:
        """Выводы ворот постановки по порядку: до, после."""
        runs = sorted(self.base.glob("ворота-*.log"), key=lambda path: int(path.stem.split("-")[1]))
        return [path.read_text(encoding="utf-8", errors="replace") for path in runs]

    def own_code_gaps(self) -> list:
        """Что из своего кода копии пропало: коммит проекта, строка crontab, свои файлы."""
        gaps = [rel for rel in (OWN_SCRIPT, OWN_TMUX, OWN_PRODUCT) if not (self.project / rel).is_file()]
        if OWN_CRON not in self.crontab.read_text(encoding="utf-8"):
            gaps.append("строка crontab typical-backup")
        if git_answer(self.project, "merge-base", "--is-ancestor", self.own_commit, "HEAD") is None:
            gaps.append(f"коммит проекта {self.own_commit[:7]}")
        return gaps

    def owner_says(self, ident: str, text: str, when: int) -> None:
        folder = self.base / "журналы" / "inbox" / "обработано"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{ident}.txt").write_text(text, encoding="utf-8")
        os.utime(folder / f"{ident}.txt", (when, when))

    def owner_says_during_update(self, ident: str, text: str, when: int) -> None:
        """Сообщение владельца приходит за часы постановки: между воротами до и после."""
        target = self.base / "журналы" / "inbox" / "обработано" / f"{ident}.txt"
        tool(self.base / "между-воротами",
             f"#!/bin/sh\nmkdir -p {shlex.quote(str(target.parent))} && printf %s {shlex.quote(text)} > "
             f"{shlex.quote(str(target))} && touch -d @{when} {shlex.quote(str(target))}\n")

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
    """(проверки стенда, имена с предметом «данные»): предмет копии и главные проверки отката из
    нового выпуска, наш карантин."""
    subject = (yaml.safe_load((tree / "harness" / "config" / "copy-subject-checks.yaml")
                              .read_text(encoding="utf-8")) or {}).get("проверки") or []
    rollback = tree / "harness" / "config" / "update-rollback.yaml"
    main_checks = ((yaml.safe_load(rollback.read_text(encoding="utf-8")) or {}).get("главные") or []
                   if rollback.is_file() else [])
    held = ((yaml.safe_load(quarantine.read_text(encoding="utf-8")) or {}).get("карантин") or []
            if quarantine.is_file() else [])
    names = [entry["имя"] for entry in subject + main_checks] + [entry["гейт"] for entry in held]
    return list(dict.fromkeys(names)), {entry["имя"] for entry in subject if entry.get("предмет") == "данные"}


def default_servers() -> Path | None:
    """Страницы копий сети эволюции: EVOLUTION_DIR/серверы этой установки; ключа нет — None."""
    root = config.конфиг(config.путь_harness_conf()).get("EVOLUTION_DIR", "")
    return Path(os.path.expanduser(root)) / "серверы" if root else None


def copy_versions(servers: Path) -> list:
    """(копия, текст версии) из фронтматтера серверы/<копия>.md; страница без поля — не копия."""
    found = []
    for page in sorted(servers.glob("*.md")):
        head = re.match(r"---\n(.*?)\n---\n", page.read_text(encoding="utf-8", errors="replace"), re.S)
        try:
            fields = yaml.safe_load(head.group(1)) if head else {}
        except yaml.YAMLError:
            fields = {VERSION_FIELD: "фронтматтер не читается"}
        version = fields.get(VERSION_FIELD) if isinstance(fields, dict) else None
        if version:
            found.append((page.stem, " ".join(str(version).split())[:FOREIGN_TEXT_CAP]))
    return found


def release_commit(build: Path, text: str) -> str | None:
    """Публичный коммит, который называет текст версии копии: метка выпуска, хэш коммита или
    хэш пакета из заголовка сборки. Нет в истории сборки — None: версия не выдумывается."""
    for ref in [f"refs/tags/v{number}" for number in RELEASE_NUMBER.findall(text)] + HEX.findall(text):
        commit = git_answer(build, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if commit:
            return commit
    titles = git_answer(build, "log", "--all", "--format=%H %s") or ""
    for token in HEX.findall(text):
        for line in titles.splitlines():
            commit, _, subject = line.partition(" ")
            built = ЗАГОЛОВОК_РЕЛИЗА.fullmatch(subject)
            if built and (built.group(1).startswith(token) or token.startswith(built.group(1))):
                return commit
    return None


def copy_releases(build: Path, servers: Path | None) -> tuple:
    """→ (узнанные версии копий [(копия, коммит)] от самой старой, строки о неузнанных)."""
    if servers is None or not servers.is_dir():
        return [], [f"типовая копия: страниц копий нет ({servers or 'EVOLUTION_DIR пуст'})"]
    known, lines = [], []
    for copy, text in copy_versions(servers):
        commit = release_commit(build, text)
        if commit is None:
            lines.append(f"типовая копия: версия копии «{copy}» не узнана в истории выпусков: «{text}»")
        else:
            known.append((int(git_answer(build, "log", "-1", "--format=%ct", commit) or 0), copy, commit))
    return [(copy, commit) for _, copy, commit in sorted(known)], lines


def extracted(build: Path, commit: str, into: Path) -> Path:
    into.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(build), "archive", commit], capture_output=True, check=True,
                             timeout=STEP_TIMEOUT, stdin=subprocess.DEVNULL)
    subprocess.run(["tar", "-x", "-C", str(into)], input=archive.stdout, capture_output=True, check=True,
                   timeout=STEP_TIMEOUT)
    return into


def installed_stand(previous: Path, build: Path, servers: Path | None, base: Path) -> tuple:
    """Стенд с поставленным старым выпуском → (стенд, старое дерево, откуда словами) | (None, None, None).

    Старое — самая старая версия у копий. Версию, которую стенд с нуля не ставит (раскладчик или
    доустановка того выпуска не идут в стенде), названа, и берётся следующая по старшинству;
    последним — прошлый выпуск. Так стенд один, пока самая старая ставится."""
    releases, lines = copy_releases(build, servers)
    print("\n".join(lines))
    candidates = [(f"версии копии «{copy}» {commit[:7]}", copy, commit) for copy, commit in releases]
    candidates.append(("прошлого выпуска", "", ""))
    for number, (source, copy, commit) in enumerate(candidates):
        tree = extracted(build, commit, base / f"старое-{number}") if commit else previous
        stand = Stand(base / f"стенд-{number}", tree / "harness" / "config" / "harness.conf")
        try:
            stand.release(tree, "прежний выпуск")
        except RuntimeError as trouble:
            print(f"типовая копия: с {source} стенд не ставится — {trouble}")
            continue
        if not commit:
            print(f"типовая копия: старое — прошлый выпуск "
                  f"({'версий копий не узнано' if not releases else 'версии копий стенд не ставит'})")
        elif number == 0:
            print(f"типовая копия: старое — самая старая версия у копий: {copy} {commit[:7]}")
        else:
            print(f"типовая копия: старое — самая старая из ставящихся версий у копий: {copy} {commit[:7]}")
        return stand, tree, source
    return None, None, None


def default_quarantine() -> Path:
    # Проект кладёт конфиги в harness/config, пакет (судья в harness/scripts) — в config.
    for folder in (HERE.parent / "harness" / "config", HERE.parent / "config"):
        if (folder / "gates-quarantine.yaml").is_file():
            return folder / "gates-quarantine.yaml"
    return HERE.parent / "harness" / "config" / "gates-quarantine.yaml"


def new_updater(tree: Path):
    """update.py нового выпуска: его разбор красных — тот же, что у самопроверки, и его
    команда перехода — та, что агент харнеса шлёт копии со старым обновлятором."""
    spec = importlib.util.spec_from_file_location("typical_copy_update", tree / "harness" / "scripts" / "update.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    ap.add_argument("--копии", type=Path, default=None,
                    help="каталог страниц серверы/<копия>.md; умолчание — EVOLUTION_DIR/серверы")
    args = ap.parse_args()
    base = Path(tempfile.mkdtemp(prefix="proba-typical-copy-"))
    try:
        return judge(args.старое.resolve(), args.новое.resolve(), args.карантин,
                     args.копии if args.копии is not None else default_servers(), base)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def update_verdict(done: subprocess.CompletedProcess) -> tuple:
    """Итог update.py --поставить → (поставлено ли, причина отказа, строки «[обновление]»)."""
    said = done.stdout + done.stderr
    lines = [line for line in said.splitlines() if line.startswith(UPDATE_LINE)]
    installed = done.returncode == 0 and f"{UPDATE_LINE} поставлено:" in said
    reason = next((line.split("не поставлено: ", 1)[1] for line in lines if "не поставлено: " in line),
                  f"код {done.returncode}: " + said.strip()[-300:])
    return installed, reason, lines[-RELAYED_LINES:]


def judge(previous: Path, new: Path, quarantine: Path, servers: Path | None, base: Path) -> int:
    try:
        checks, data_checks = checks_of(new, quarantine)
        print(f"типовая копия: проверок {len(checks)}: {' | '.join(checks)}")
        stand, old, source = installed_stand(previous, new, servers, base)
        if stand is None:
            print("типовая копия: стенд не собрался — ни один старый выпуск не ставится")
            return 2
        stand.own_process()
        stand.owner_says_during_update(*OWNER_LATE)
        stand.publish(new, "новый выпуск")
        updater = new_updater(new)
        bridge = None if stand.relaunches() else getattr(updater, "КОМАНДА_ПЕРЕХОДА", None)
        if bridge is not None:
            print(BRIDGE_LINE)
        done = stand.update(checks, bridge)
        runs, gaps = stand.gate_runs(), stand.own_code_gaps()
        parse = updater.красные_из_вывода
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError) as trouble:
        print(f"типовая копия: стенд не собрался — {trouble}")
        return 2
    installed, reason, update_lines = update_verdict(done)
    print("\n".join(update_lines))
    if not installed and reason == "нового нет":
        print("типовая копия: стенд не собрался — update.py не увидел нового выпуска")
        return 2
    before, after = (parse(runs[0]), parse(runs[-1])) if len(runs) >= 2 else (None, None)
    if installed and (before is None or after is None):
        print("типовая копия: итог самопроверки не прочитан — " + (runs[-1] if runs else "ворот не было").strip()[-300:])
        return 2
    if not installed:
        print(f"типовая копия: update.py --поставить не поставил: {reason}")
    fresh = []
    if before is not None and after is not None:
        for name in sorted(name for name in after if name in data_checks):
            print(f"типовая копия: красная в данных копии «{name}» (откат её не снимает — обновление встанет и "
                  f"назовёт): {' | '.join(after.причины.get(name, [])) or 'вывода нет'}")
        fresh = sorted(name for name in after - before if name not in data_checks)
        for name in fresh:
            print(f"типовая копия: новая красная «{name}»: {' | '.join(after.причины.get(name, [])) or 'вывода нет'}")
    if gaps:
        print(f"типовая копия: новая красная «свой код копии»: после постановки нет — {', '.join(gaps)}")
    else:
        print(f"типовая копия: свой код копии цел: коммит проекта {stand.own_commit[:7]}, строка crontab "
              f"typical-backup, {OWN_SCRIPT}, {OWN_TMUX}, {OWN_PRODUCT}")
    toy = toy_product_lines(new, old / "harness" / "config" / "harness.conf",
                            os.environ.get("TYPICAL_COPY_TOY_IMAGE") or TOY_BASE)
    print("\n".join(toy))
    if not installed or fresh or gaps or any("новая красная" in line for line in toy):
        return 1
    print(f"типовая копия: update.py --поставить с {source} встал, новых красных нет (харнеса; проверок "
          f"{len(checks)}, красных до {sum(before.values())}, после {sum(after.values())})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
