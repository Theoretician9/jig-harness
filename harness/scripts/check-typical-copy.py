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
Раскладка и доустановка — те же, что зовёт update.py --поставить; между выпусками копия
заводит свой процесс по слову судьи. Проверки самопроверки идут до и после; новая
красная (её не было до) — отказ с именем и последними строками вывода.

    python3 scripts/check-typical-copy.py --старое <дерево выпуска> --новое <дерево выпуска>
Дерево — корень сборки (в нём каталог harness/). Коды: 0 — новых красных нет,
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

# Проверки самопроверки, которые судят то, что у копии своё (находка bkb 14.11.2026).
CHECKS = ("библиотека процессов", "изоляция tmux")
STEP_TIMEOUT = 900
OWN_SCRIPT = "scripts/typical-backup.sh"
OWN_TMUX = "scripts/typical-preview.sh"


def run(command: list, env: dict, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(command, env=env, cwd=cwd, capture_output=True, text=True,
                          timeout=STEP_TIMEOUT, stdin=subprocess.DEVNULL)


def git(where: Path, *args) -> None:
    subprocess.run(["git", "-C", str(where), "-c", "user.name=typical-copy",
                    "-c", "user.email=typical-copy@stand", *args],
                   check=True, capture_output=True, timeout=STEP_TIMEOUT)


class Stand:
    def __init__(self, base: Path, sample_conf: Path):
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
            f'PROJECT_DIR="{self.project}"\nPROJECT_NAME="типовая-копия"\nAGENT_USER="kopiya"\n'
            f'LOG_DIR="{base}/журналы"\nHEARTBEAT_DIR="{base}/метки"\n', encoding="utf-8")
        conf = re.sub(r'(?m)^BACKUP_DIR=.*$', f'BACKUP_DIR="{base}/копии"',
                      sample_conf.read_text(encoding="utf-8"))
        (base / "harness.conf").write_text(conf, encoding="utf-8")
        self.env = {"PATH": f"{base}/bin:/usr/local/bin:/usr/bin:/bin", "HOME": f"{base}/дом",
                    "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HARNESS_SELFTEST": "1",
                    "HARNESS_HOME": f"{base}/корень", "HARNESS_INSTALL_CONF": f"{base}/install.conf",
                    "HARNESS_CONF": f"{base}/harness.conf", "HARNESS_STARTER": str(self.package),
                    "HARNESS_PROCESS_LIBRARY_LOCAL": f"{base}/слой-процессов.json",
                    "PROJECT_DIR": str(self.project), "FAKE_CRON": str(self.crontab)}
        git(self.package, "init", "-q")
        git(self.project, "init", "-q")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "копия")

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

    def self_check(self) -> str:
        done = run(["bash", str(self.project / "scripts" / "gates.sh"), "--только", "|".join(CHECKS)],
                   self.env, cwd=self.project)
        return done.stdout + done.stderr


def reds_parser(tree: Path):
    """Разбор красных — тот же, что у самопроверки update.py нового выпуска."""
    spec = importlib.util.spec_from_file_location("typical_copy_update", tree / "harness" / "scripts" / "update.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.красные_из_вывода


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--старое", required=True, type=Path)
    ap.add_argument("--новое", required=True, type=Path)
    args = ap.parse_args()
    old, new = args.старое.resolve(), args.новое.resolve()
    base = Path(tempfile.mkdtemp(prefix="proba-typical-copy-"))
    try:
        stand = Stand(base, old / "harness" / "config" / "harness.conf")
        stand.release(old, "прежний выпуск")
        stand.own_process()
        parse = reds_parser(new)
        before = parse(stand.self_check())
        stand.release(new, "новый выпуск")
        after_text = stand.self_check()
        after = parse(after_text)
    except (RuntimeError, OSError, subprocess.SubprocessError) as trouble:
        print(f"типовая копия: стенд не собрался — {trouble}")
        return 2
    finally:
        shutil.rmtree(base, ignore_errors=True)
    if before is None or after is None:
        print("типовая копия: итог самопроверки не прочитан — " + after_text.strip()[-300:])
        return 2
    fresh = sorted(after - before)
    for name in fresh:
        print(f"типовая копия: новая красная «{name}»: {' | '.join(after.причины.get(name, [])) or 'вывода нет'}")
    if fresh:
        return 1
    print(f"типовая копия: новых красных нет (проверок {len(CHECKS)}, красных до {sum(before.values())}, "
          f"после {sum(after.values())})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
