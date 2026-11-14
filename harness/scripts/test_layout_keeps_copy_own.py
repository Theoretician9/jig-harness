#!/usr/bin/env python3
"""Проба двери укладки: опись не снимает своё копии (layout-door.py, суд прежней описи).

Своё копии — путь, тронутый коммитами пакета, которые достижимы из локальных веток,
но не из HEAD, тегов и remote-веток (`git log --branches --not HEAD --tags --remotes`).
Так лежат коммиты копии после `update.py --сбросить-свои`: ветка-страховка
server-<host>-local-<время> держит их, HEAD пакета стоит на публичном коммите.
Раскладка клала такой файл, пока свой коммит стоял на HEAD, — он в описи, а в плане
после сброса его нет. Снять его — потерять своё копии (И-1).

Стенд (свой mktemp): пакет git — P0 (a.sh, old.sh), P (публичный: old.sh убран),
S (свой, в отдельной ветке: добавил x.sh); HEAD = P. Проект: a.sh, old.sh, x.sh
и прежняя опись со всеми тремя. Дверь зовётся как раскладчиком: --package-root.

  О1 ветка-страховка по имени update.py: x.sh не снят, на месте, остаётся в описи,
     строка «своё копии, оставлено: harness/demons/x.sh»; контроль — old.sh,
     который публичный пакет клал и убрал, снят.
  О2 та же ветка переименована владельцем копии: правило судит коммиты, не имя ветки.

Какая поломка оставит пробу зелёной? «Есть чужая ветка — ничего не снимать» —
контроль old.sh краснеет. «Своё = вся история пакета» — old.sh в P0, контроль
краснеет. «Своё = только ветки server-*-local-*» — краснеет О2.

Двери нет — каждая проверка красная. Итог: «ОПИСЬ И СВОЁ КОПИИ: путей N, неудач M», код 0/1.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
DOOR = (HERE / "layout-door.py").absolute()
# Таблица перевода путей пакета — стенд кладёт копию. Проба лежит в scripts/ проекта
# (таблица в harness/config/) или в harness/scripts/ пакета (таблица в harness/config/).
RULES = next((p.absolute() for p in (HERE.parent / "harness" / "config" / "layout-history-paths.yaml",
                                     HERE.parent / "config" / "layout-history-paths.yaml")
              if p.is_file()), None)
MANIFEST = ".layout-manifest"
DOOR_KEYS = ("HARNESS_LAYOUT_ARCHIVE", "HARNESS_LAYOUT_KILL_AFTER_WRITE",
             "HARNESS_LAYOUT_PLAN_OUT", "HARNESS_SELFTEST", "LAYOUT_REMOVE_MAX_PERCENT")
KEEP = "harness/demons/a.sh"
PUBLIC_GONE = "harness/demons/old.sh"
OWN = "harness/demons/x.sh"
SAFETY_BRANCH = "server-стенд-local-20261114-0000"
CRONTAB_NONE = '#!/bin/sh\necho "no crontab for стенд" >&2\nexit 1\n'


class Probe:
    def __init__(self) -> None:
        self.paths = 0
        self.failures = 0

    def check(self, label: str, name: str, ok: bool, detail: str = "") -> None:
        self.paths += 1
        if ok and DOOR.is_file():
            print(f"  ок    {label} {name}")
            return
        self.failures += 1
        print(f"  ПЛОХО {label} {name}: {detail if DOOR.is_file() else f'нет двери {DOOR}'}")


PROBE = Probe()


def blob_of(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "стенд", "GIT_AUTHOR_EMAIL": "s@t",
           "GIT_COMMITTER_NAME": "стенд", "GIT_COMMITTER_EMAIL": "s@t"}
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          env=env, check=True)
    return done.stdout.strip()


def put(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o755)
    return path


class Stand:
    def __init__(self, branch: str) -> None:
        self.t = Path(tempfile.mkdtemp(prefix="layout-own-probe-"))
        self.pkg, self.proj = self.t / "пакет", self.t / "проект"
        self.texts = {KEEP: "#!/bin/sh\necho a\n", PUBLIC_GONE: "#!/bin/sh\necho old\n",
                      OWN: "#!/bin/sh\necho своё копии\n"}
        self.build_package(branch)
        self.build_project()

    def build_package(self, branch: str) -> None:
        self.pkg.mkdir()
        git(self.pkg, "init", "-q", "-b", "master")
        rules = self.pkg / "harness" / "config" / "layout-history-paths.yaml"
        rules.parent.mkdir(parents=True)
        shutil.copyfile(RULES, rules)
        put(self.pkg, KEEP, self.texts[KEEP])
        put(self.pkg, PUBLIC_GONE, self.texts[PUBLIC_GONE])
        git(self.pkg, "add", "-A")
        git(self.pkg, "commit", "-q", "-m", "P0 публичный")
        git(self.pkg, "rm", "-q", PUBLIC_GONE)
        git(self.pkg, "commit", "-q", "-m", "P публичный: old.sh убран")
        git(self.pkg, "tag", "v-public")
        self.public = git(self.pkg, "rev-parse", "HEAD")
        # Состояние после update.py --сбросить-свои: S в ветке-страховке, HEAD на P.
        git(self.pkg, "checkout", "-q", "-b", branch)
        put(self.pkg, OWN, self.texts[OWN])
        git(self.pkg, "add", OWN)
        git(self.pkg, "commit", "-q", "-m", "S своё копии: демон x.sh")
        git(self.pkg, "checkout", "-q", "master")

    def build_project(self) -> None:
        self.proj.mkdir()
        rows = []
        for rel, text in self.texts.items():
            rows.append((blob_of(put(self.proj, rel, text)), "harness", rel))
        rows.sort(key=lambda row: row[2].encode("utf-8"))
        body = "\n".join("\t".join(row) for row in rows)
        (self.proj / MANIFEST).write_text(
            f"# layout-manifest 2\n# пакет {self.public}\n{body}\n# оставлено\n", encoding="utf-8")
        (self.t / "install.conf").write_text(
            f'PROJECT_NAME="стенд"\nPROJECT_DIR="{self.proj}"\n', encoding="utf-8")
        (self.t / "harness.conf").write_text(
            f'BACKUP_DIR="{self.t / "копии"}"\nLAYOUT_REMOVE_MAX_PERCENT="100"\n', encoding="utf-8")
        bin_dir = self.t / "bin"
        bin_dir.mkdir()
        put(bin_dir, "crontab", CRONTAB_NONE)

    def run(self) -> subprocess.CompletedProcess:
        plan = self.t / "план.bin"
        plan.write_bytes(f"harness\t{self.pkg / KEEP}\t{KEEP}\0".encode("utf-8"))
        env = {k: v for k, v in os.environ.items() if k not in DOOR_KEYS}
        env.update(HARNESS_SELFTEST="1", HARNESS_INSTALL_CONF=str(self.t / "install.conf"),
                   HARNESS_CONF=str(self.t / "harness.conf"), BACKUP_DIR="", PROJECT_NAME="",
                   PROJECT_DIR="", PYTHONDONTWRITEBYTECODE="1", LOG_DIR=str(self.t / "журналы"),
                   PATH=f"{self.t / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}")
        return subprocess.run([sys.executable, str(DOOR), "--plan", str(plan),
                               "--project", str(self.proj), "--package-commit", self.public,
                               "--package-root", str(self.pkg)],
                              capture_output=True, text=True, env=env, timeout=120, check=False)

    def __enter__(self) -> "Stand":
        return self

    def __exit__(self, *_exc) -> None:
        shutil.rmtree(self.t, ignore_errors=True)


def removed_line(out: str) -> str:
    return next((line for line in out.splitlines() if "опись: в описи" in line), "")


def judge(label: str, branch: str) -> None:
    with Stand(branch) as s:
        own_in_history = git(s.pkg, "log", "--branches", "--not", "HEAD", "--tags", "--remotes",
                             "--name-only", "--format=")
        PROBE.check(label, f"стенд: {OWN} — путь своего коммита вне HEAD и тегов",
                    own_in_history.split() == [OWN], f"git log дал {own_in_history!r}")
        r = s.run()
        brief = f"код {r.returncode}; stdout {r.stdout[-600:]!r}; stderr {r.stderr[-300:]!r}"
        PROBE.check(label, "дверь прошла: код 0", r.returncode == 0, brief)
        summary = removed_line(r.stdout)
        PROBE.check(label, f"контроль: {PUBLIC_GONE} (публичный пакет убрал) снят",
                    not (s.proj / PUBLIC_GONE).exists() and PUBLIC_GONE in summary, brief)
        PROBE.check(label, f"{OWN} не в строке снятия", bool(summary) and OWN not in summary,
                    f"строка {summary!r}")
        PROBE.check(label, f"{OWN} на месте, содержимое цело",
                    (s.proj / OWN).is_file()
                    and (s.proj / OWN).read_text(encoding="utf-8") == s.texts[OWN],
                    "файла нет или он другой")
        PROBE.check(label, f"строка «своё копии, оставлено: {OWN}»",
                    f"своё копии, оставлено: {OWN}" in r.stdout, brief)
        manifest = (s.proj / MANIFEST).read_text(encoding="utf-8") if (s.proj / MANIFEST).is_file() else ""
        PROBE.check(label, f"опись по-прежнему называет {OWN} (следующий прогон судит снова)",
                    any(line.endswith("\t" + OWN) for line in manifest.splitlines()),
                    f"опись {manifest[-300:]!r}")


def main() -> int:
    if RULES is None:
        print(f"  ПЛОХО таблицы layout-history-paths.yaml рядом с пробой нет ({HERE})")
        print("ОПИСЬ И СВОЁ КОПИИ: путей 1, неудач 1")
        return 1
    judge("О1", SAFETY_BRANCH)
    judge("О2", "мои-правки")
    print(f"ОПИСЬ И СВОЁ КОПИИ: путей {PROBE.paths}, неудач {PROBE.failures}")
    return 1 if PROBE.failures else 0


if __name__ == "__main__":
    sys.exit(main())
