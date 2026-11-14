#!/usr/bin/env python3
"""Проба двери укладки (layout-door.py): нет копии — нет перезаписи, всё или ничего.

Дверь — единственное место, где файл пакета ложится в проект. Её правила судятся
здесь, на её уровне: мутации раскладчика ручные, а мутации двери подставляет
mutation-probe.sh («подмена: из-копии»), поэтому дверь берётся РЯДОМ с пробой.

Каждый случай — свой стенд mktemp: мини-пакет, мини-проект, свой install.conf
(PROJECT_NAME="стенд") и harness.conf (BACKUP_DIR внутри стенда). Боевые
/var/backups/harness, /etc/harness и проект не читаются и не пишутся.
«Проект не тронут» — множество (путь, тип, inode, режим, blob) ВСЕГО мини-проекта.

Двери нет — каждая проверка красная «нет двери», а не трасса и не зелень.
Итог: «ДВЕРЬ УКЛАДКИ: путей N, неудач M», код 0/1.
"""
import datetime
import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

DOOR = (Path(__file__).parent / "layout-door.py").absolute()
DOOR_PRESENT = DOOR.is_file()
MARKER = "РАСКЛАДКА НЕ НАЧАТА: "
# Keys a caller's environment could leak into a case; each case sets its own.
DOOR_KEYS = ("HARNESS_LAYOUT_ARCHIVE", "HARNESS_LAYOUT_KILL_AFTER_WRITE",
             "HARNESS_LAYOUT_PLAN_OUT", "HARNESS_SELFTEST", "LAYOUT_REMOVE_MAX_PERCENT")

# Mini-package: (group, path in project, content, mode). Plan order = this order.
FILES = (
    ("scripts", "scripts/deploy.sh", "#!/bin/sh\necho пакет\n", 0o755),
    ("scripts", "scripts/tool.py", "print('пакет')\n", 0o644),
    ("config", "harness/config/x.yaml", "ключ: пакет\n", 0o644),
    ("skills", "harness/skills/x/SKILL.md", "# скил пакета\n", 0o644),
)
FOREIGN = "#!/bin/sh\necho правлено здесь\n"
# The door reads `crontab -l` through PATH; a stand answers with its own fake so no case
# ever reads the live crontab of the user running the probe.
CRONTAB_NONE = '#!/bin/sh\necho "no crontab for стенд" >&2\nexit 1\n'
CRONTAB_BROKEN = ('#!/bin/sh\necho "crontab: cannot open crontab file: Permission denied" >&2\n'
                  'exit 2\n')


class Probe:
    def __init__(self) -> None:
        self.paths = 0
        self.failures = 0

    def check(self, label: str, name: str, ok: bool, detail: str = "") -> None:
        self.paths += 1
        if not DOOR_PRESENT:
            self.failures += 1
            print(f"  ПЛОХО {label} {name}: нет двери {DOOR}")
        elif ok:
            print(f"  ок    {label} {name}")
        else:
            self.failures += 1
            print(f"  ПЛОХО {label} {name}: {detail}")

    def crashed(self, label: str, error: Exception) -> None:
        self.paths += 1
        self.failures += 1
        print(f"  ПЛОХО {label} проба упала: {type(error).__name__}: {error}")


PROBE = Probe()


def blob_of(path) -> str | None:
    """git hash-object без git: sha1("blob <len>\\0" + байты)."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def git_blob(path) -> str:
    return subprocess.run(["git", "hash-object", str(path)], capture_output=True,
                          text=True, check=False).stdout.strip()


def read_bytes(path) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def mode_of(path) -> int | None:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def snapshot(root: Path) -> dict:
    """(тип, inode, режим) и blob/цель ссылки каждого пути под root, каталоги тоже."""
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                kind = ("l", os.readlink(path))
            elif stat.S_ISDIR(st.st_mode):
                kind = ("d", "")
            elif stat.S_ISREG(st.st_mode):
                kind = ("f", blob_of(path))
            else:
                kind = ("?", "")
            entries[os.path.relpath(path, root)] = (kind, st.st_ino, stat.S_IMODE(st.st_mode))
    return entries


def snapshot_diff(before: dict, after: dict) -> str:
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    return f"изменилось путей {len(changed)}: {changed[:5]}"


class Result:
    def __init__(self, rc, out: str, err: str) -> None:
        self.rc, self.out, self.err = rc, out, err

    @property
    def both(self) -> str:
        return self.out + "\n" + self.err

    def brief(self) -> str:
        return f"код {self.rc}; stdout {self.out[-300:]!r}; stderr {self.err[-300:]!r}"


class Stand:
    """Мини-пакет + мини-проект + конфиги стенда в одном mktemp."""

    def __init__(self, laid: bool = True) -> None:
        self.t = Path(tempfile.mkdtemp(prefix="layout-door-probe-"))
        self.pkg = self.t / "пакет"
        self.proj = self.t / "проект"
        self.backups = self.t / "копии"
        self.install_conf = self.t / "install.conf"
        self.harness_conf = self.t / "harness.conf"
        self.plan_file = self.t / "план.bin"
        self.plan = [(group, str(self.pkg / rel), rel) for group, rel, _, _ in FILES]
        self.bin = self.t / "bin"
        self.bin.mkdir()
        self.set_crontab(CRONTAB_NONE)
        self.proj.mkdir()
        for _, rel, content, mode in FILES:
            src = self.pkg / rel
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(content, encoding="utf-8")
            os.chmod(src, mode)
            if laid:
                (self.proj / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, self.proj / rel)
        self.install_conf.write_text(
            f'PROJECT_NAME="стенд"\nPROJECT_DIR="{self.proj}"\n', encoding="utf-8")
        self.set_backup_dir(self.backups)

    def __enter__(self) -> "Stand":
        return self

    def __exit__(self, *_exc) -> None:
        shutil.rmtree(self.t, ignore_errors=True)

    def set_backup_dir(self, path) -> None:
        self.backup_value = str(path)
        self.harness_conf.write_text(f'BACKUP_DIR="{path}"\n', encoding="utf-8")

    def set_limit(self, value) -> None:
        """harness.conf стенда: BACKUP_DIR и ключ порога; None — строки ключа нет."""
        key = "" if value is None else f'LAYOUT_REMOVE_MAX_PERCENT="{value}"\n'
        self.harness_conf.write_text(f'BACKUP_DIR="{self.backup_value}"\n' + key,
                                     encoding="utf-8")

    def set_crontab(self, script: str) -> None:
        fake = self.bin / "crontab"
        fake.write_text(script, encoding="utf-8")
        os.chmod(fake, 0o755)

    def foreign(self, rel: str, content: str = FOREIGN, mode: int = 0o700) -> Path:
        path = self.proj / rel
        path.write_text(content, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in DOOR_KEYS}
        env.update(HARNESS_SELFTEST="1", HARNESS_INSTALL_CONF=str(self.install_conf),
                   HARNESS_CONF=str(self.harness_conf), BACKUP_DIR="", PROJECT_NAME="",
                   PROJECT_DIR="", LAYOUT_REMOVE_MAX_PERCENT="", PYTHONDONTWRITEBYTECODE="1",
                   LOG_DIR=str(self.t / "журналы"),
                   PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}")
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env

    def write_plan(self, plan=None) -> None:
        records = plan if plan is not None else self.plan
        self.plan_file.write_bytes(b"".join(
            ("\t".join(record) + "\0").encode("utf-8") for record in records))

    def door_args(self) -> list:
        return ["--plan", str(self.plan_file), "--project", str(self.proj)]

    def run(self, plan=None, args=(), **env) -> Result:
        self.write_plan(plan)
        return execute([sys.executable, str(DOOR), *self.door_args(), *args], self.env(**env))

    def run_patched(self, patch: str, plan=None, args=(), **env) -> Result:
        """Дверь в дочернем python с подменённой функцией: os.kill и sys.exit
        двери не должны убить саму пробу."""
        self.write_plan(plan)
        return execute([sys.executable, "-c", CHILD_PATCHED, str(DOOR), patch, str(self.proj),
                        *self.door_args(), *args], self.env(**env))

    def runs(self) -> list:
        return sorted(p for p in (self.backups / "раскладка").glob("стенд-*") if p.is_dir())

    def all_runs(self) -> int:
        return len([p for p in (self.backups / "раскладка").glob("*") if p.is_dir()])


def execute(argv: list, env: dict) -> Result:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120,
                              check=False)
    except subprocess.TimeoutExpired as late:
        return Result(None, str(late.stdout or ""), str(late.stderr or "") + "\nТАЙМАУТ 120 с")
    return Result(done.returncode, done.stdout, done.stderr)


def journal(archive: Path) -> list:
    try:
        return (archive / "journal.tsv").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def action_lines(lines: list) -> list:
    return [line.split("\t") for line in lines
            if line.split("\t")[0] in ("перекрыт", "положен-новый") and line.count("\t") == 3]


def lines_naming(text: str, words: str, path: str) -> list:
    return [line for line in text.splitlines() if words in line and path in line]


def first_marker_line(err: str) -> str:
    return err.splitlines()[0] if err.splitlines() else ""


CHILD_PATCHED = r'''
import importlib.util, os, sys
door, patch, project = sys.argv[1], sys.argv[2], os.path.realpath(sys.argv[3])
spec = importlib.util.spec_from_file_location("layout_door", door)
door_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(door_module)
if patch == "spoil-copy":
    real_copy = door_module.copy_to_archive
    def spoiled_copy(src, dst_in_archive):
        real_copy(src, dst_in_archive)
        with open(dst_in_archive, "r+b") as fh:
            first = fh.read(1)
            fh.seek(0)
            fh.write(bytes([first[0] ^ 1]) if first else b"x")
        return door_module.blob(str(dst_in_archive))
    door_module.copy_to_archive = spoiled_copy
elif patch == "foreign-source":
    real_blob = door_module.blob
    def lying_blob(path):
        if str(path).endswith(".layout-tmp") and \
                os.path.realpath(str(path)).startswith(project + os.sep):
            return "0" * 40
        return real_blob(path)
    door_module.blob = lying_blob
elif patch == "fail-manifest-replace":
    import errno
    real_replace = os.replace
    def failing_replace(src, dst, *args, **kwargs):
        if os.path.basename(str(dst)) == ".layout-manifest":
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return real_replace(src, dst, *args, **kwargs)
    os.replace = failing_replace
elif patch in ("exdev", "exdev-spoil", "order-spy", "edit-in-move", "link-before-remove",
               "edit-before-remove", "dirlink-before-remove"):
    # A move project → <archive>/files/<path> is caught at os.rename/os.replace, the calls
    # shutil.move also goes through: the probe does not depend on the door's own names.
    import atexit, errno, shutil
    fired, swapped = [], []
    real_rename = os.rename

    def swap_between_passes():
        # Pass 1 has judged the path; the change lands before pass 2 removes it.
        target = os.environ["PROBE_SWAP"]
        if patch == "link-before-remove":
            os.unlink(target)
            os.symlink(os.environ["PROBE_SWAP_TO"], target)
            print("ПОДМЕНА: ссылка поставлена после первой записи укладки", file=sys.stderr)
        elif patch == "edit-before-remove":
            with open(target, "ab") as fh:
                fh.write(b"!")
            print("ПОДМЕНА: файл правлен после первой записи укладки", file=sys.stderr)
        else:
            real_rename(target, os.environ["PROBE_SWAP_TO"])
            os.symlink(os.environ["PROBE_SWAP_TO"], target)
            print("ПОДМЕНА: каталог подменён ссылкой после первой записи укладки",
                  file=sys.stderr)
    files_part = os.sep + "files" + os.sep

    def in_project(path):
        parent = os.path.realpath(os.path.dirname(os.path.abspath(str(path))))
        return parent == project or parent.startswith(project + os.sep)

    def into_archive(src, dst):
        return in_project(src) and files_part in str(dst) and not in_project(dst)

    def moving(real):
        def wrapper(src, dst, *args, **kwargs):
            if into_archive(src, dst):
                fired.append(str(dst))
                archive, rel = str(dst).split(files_part, 1)
                if patch.startswith("exdev"):
                    raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), str(src))
                if patch == "order-spy":
                    try:
                        with open(os.path.join(archive, "journal.tsv"), encoding="utf-8") as fh:
                            lines = fh.read().splitlines()
                    except OSError:
                        lines = []
                    seen = any(l.startswith("снят\t") and l.endswith("\t" + rel) for l in lines)
                    print(f"ПОРЯДОК {rel}: " + ("строка «снят» до переноса" if seen
                                                 else "строки «снят» нет при переносе"),
                          file=sys.stderr)
                if patch == "edit-in-move":
                    with open(str(src), "ab") as fh:
                        fh.write(b"!")
            result = real(src, dst, *args, **kwargs)
            if (patch.endswith("-before-remove") and not swapped
                    and str(src).endswith(".layout-tmp") and in_project(dst)
                    and os.path.basename(str(dst)) != ".layout-manifest"):
                swapped.append(str(dst))
                swap_between_passes()
            return result
        return wrapper

    os.rename = moving(os.rename)
    os.replace = moving(os.replace)
    if patch == "exdev-spoil":
        real_copyfile = shutil.copyfile
        def spoiling_copyfile(src, dst, *args, **kwargs):
            out = real_copyfile(src, dst, *args, **kwargs)
            if into_archive(src, dst):
                fired.append("порча " + str(dst))
                with open(dst, "r+b") as fh:
                    first = fh.read(1)
                    fh.seek(0)
                    fh.write(bytes([first[0] ^ 1]) if first else b"x")
            return out
        shutil.copyfile = spoiling_copyfile
    atexit.register(lambda: print(f"ПОДМЕНА {patch}: срабатываний {len(fired)}", file=sys.stderr))
sys.exit(door_module.main(sys.argv[4:]))
'''

CHILD_SHORT_WRITE = r'''
import importlib.util, os, resource, sys
spec = importlib.util.spec_from_file_location("layout_door", sys.argv[1])
door_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(door_module)
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
line = "перекрыт\t" + "0" * 40 + "\t" + "1" * 40 + "\tscripts/" + "длинное-имя-" * 8
resource.setrlimit(resource.RLIMIT_FSIZE, (100, 100))
try:
    door_module.write_journal_line(fd, line)
except OSError as refusal:
    print("ОТКАЗ", refusal)
    sys.exit(0)
print("МОЛЧА: короткая запись засчитана")
'''

CHILD_BLOB = r'''
import importlib.util, sys
spec = importlib.util.spec_from_file_location("layout_door", sys.argv[1])
door_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(door_module)
print(door_module.blob(sys.argv[2]))
'''


# ─────────────────────────────── случаи ──────────────────────────────────────

def case_d1() -> None:
    """Перекрытие: копия с blob и режимом, журнал шапка → перекрыт → готово → итог.
    Ловит: копия не снимается; копия без режима (644/755); журнал без шапки/итога."""
    with Stand() as s:
        dst = s.foreign("scripts/deploy.sh", mode=0o700)
        old_blob, new_blob = git_blob(dst), git_blob(s.pkg / "scripts/deploy.sh")
        r = s.run()
        PROBE.check("D1", "перекрытие: код 0", r.rc == 0, r.brief())
        PROBE.check("D1", "в проекте файл пакета",
                    read_bytes(dst) == read_bytes(s.pkg / "scripts/deploy.sh"), r.brief())
        PROBE.check("D1", "режим файла в проекте — режим источника (755)",
                    mode_of(dst) == 0o755, f"режим {mode_of(dst)!r}")
        runs = s.runs()
        PROBE.check("D1", "один архив копии/раскладка/стенд-*", len(runs) == 1,
                    f"архивов {len(runs)}")
        copy = runs[0] / "files" / "scripts/deploy.sh" if runs else s.t / "нет"
        PROBE.check("D1", "копия в files/ с blob прежнего файла", blob_of(copy) == old_blob,
                    f"blob копии {blob_of(copy)!r}, ждали {old_blob}")
        PROBE.check("D1", "копия с режимом 700 (cp -p)", mode_of(copy) == 0o700,
                    f"режим копии {mode_of(copy)!r}")
        lines = journal(runs[0]) if runs else []
        PROBE.check("D1", "шапка журнала «# прогон <pid> раскладка план=4 перекрыть=1»",
                    bool(lines) and re.fullmatch(r"# прогон \d+ раскладка план=4 перекрыть=1",
                                                 lines[0]) is not None, f"журнал {lines[:2]}")
        overwritten = f"перекрыт\t{old_blob}\t{new_blob}\tscripts/deploy.sh"
        done = "готово\t-\t-\tscripts/deploy.sh"
        PROBE.check("D1", "строка перекрыт с blob было/стало = git hash-object, затем готово",
                    overwritten in lines and done in lines
                    and lines.index(overwritten) < lines.index(done), f"журнал {lines}")
        PROBE.check("D1", "последняя строка «итог\\t-\\t-\\t1»",
                    bool(lines) and lines[-1] == "итог\t-\t-\t1", f"журнал {lines[-1:]}")
        PROBE.check("D1", "сводка «дверь укладки: перекрыто 1, новых 0,»",
                    "дверь укладки: перекрыто 1, новых 0," in r.out, r.brief())
        door_blob = execute([sys.executable, "-c", CHILD_BLOB, str(DOOR), str(dst)],
                            s.env()).out.strip()
        PROBE.check("D1", "blob() двери = git hash-object", door_blob == git_blob(dst),
                    f"blob() {door_blob!r}, git {git_blob(dst)}")


def case_d2() -> None:
    """Архив недоступен: ни одной записи, код 3, краткая причина без пути архива.
    Ловит: запись до первой беды (новый файл лёг раньше); отказ кодом 1."""
    with Stand() as s:
        (s.t / "файл").write_text("обычный файл\n", encoding="utf-8")
        s.set_backup_dir(f"{s.t}/файл/копии")
        s.foreign("scripts/deploy.sh")
        s.foreign("harness/config/x.yaml", "ключ: правлено\n", 0o644)
        (s.proj / "scripts/tool.py").unlink()
        before = snapshot(s.proj)
        r = s.run()
        PROBE.check("D2", "архив недоступен: код 3", r.rc == 3, r.brief())
        PROBE.check("D2", "первая строка stderr — маркер и краткая причина без пути архива",
                    first_marker_line(r.err) == MARKER + "2 файла отличаются от пакета, а копию "
                    "снять некуда (ключ BACKUP_DIR)", f"первая строка {first_marker_line(r.err)!r}")
        PROBE.check("D2", "беда названа: «копию снять некуда: <архив>: Not a directory»",
                    any("копию снять некуда" in line and f"{s.t}/файл/копии" in line
                        and "Not a directory" in line for line in r.err.splitlines()), r.brief())
        PROBE.check("D2", "нет «дверь укладки:»", "дверь укладки:" not in r.both, r.brief())
        after = snapshot(s.proj)
        PROBE.check("D2", "проект не тронут (новый файл не лёг)", before == after,
                    snapshot_diff(before, after))
        s.set_backup_dir(s.backups)
        r = s.run()
        PROBE.check("D2", "пара: годный BACKUP_DIR — код 0 и новый файл лёг",
                    r.rc == 0 and (s.proj / "scripts/tool.py").is_file(), r.brief())


def case_d3() -> None:
    """Нет места: журнал на /dev/full — код 3, проект не тронут; пара пишет положен-новый.
    Ловит: ошибка записи журнала проглочена; положен-новый не пишется при архиве (R2-08)."""
    with Stand() as s:
        s.foreign("scripts/deploy.sh")
        (s.proj / "scripts/tool.py").unlink()
        (s.proj / "harness/config/x.yaml").unlink()
        full = s.backups / "раскладка" / "в"
        full.mkdir(parents=True)
        (full / "journal.tsv").symlink_to("/dev/full")
        before = snapshot(s.proj)
        r = s.run(HARNESS_LAYOUT_ARCHIVE=full)
        PROBE.check("D3", "нет места: код 3 и маркер первой строкой",
                    r.rc == 3 and r.err.startswith(MARKER), r.brief())
        PROBE.check("D3", "«журнал не записан: No space left on device»",
                    "журнал не записан: No space left on device" in r.err, r.brief())
        after = snapshot(s.proj)
        PROBE.check("D3", "проект не тронут", before == after, snapshot_diff(before, after))
        good = s.backups / "раскладка" / "в2"
        r = s.run(HARNESS_LAYOUT_ARCHIVE=good)
        PROBE.check("D3", "пара: журнал пишется — код 0", r.rc == 0, r.brief())
        lines = journal(good)
        PROBE.check("D3", "шапка режима «обновление» при заданном архиве",
                    bool(lines) and re.fullmatch(r"# прогон \d+ обновление план=4 перекрыть=1",
                                                 lines[0]) is not None, f"журнал {lines[:1]}")
        for rel in ("scripts/tool.py", "harness/config/x.yaml"):
            PROBE.check("D3", f"положен-новый на {rel}",
                        f"положен-новый\t-\t{blob_of(s.pkg / rel)}\t{rel}" in lines,
                        f"журнал {lines}")
        PROBE.check("D3", "итог считает перекрыт + положен-новый (3)",
                    bool(lines) and lines[-1] == "итог\t-\t-\t3", f"журнал {lines[-1:]}")


def case_d4() -> None:
    """Короткая запись: под RLIMIT_FSIZE os.write отдаёт 100 из N без исключения.
    Ловит: число записанных байт не сверяется — короткая запись «удаётся»."""
    with tempfile.TemporaryDirectory(prefix="layout-door-probe-") as t:
        r = execute([sys.executable, "-c", CHILD_SHORT_WRITE, str(DOOR),
                     str(Path(t) / "journal.tsv")], dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        found = re.search(r"журнал не записан: записано (\d+) из (\d+) байт", r.out)
        PROBE.check("D4", "короткая запись — отказ «записано X из Y байт», X < Y",
                    found is not None and int(found.group(1)) < int(found.group(2)), r.brief())


def case_d5() -> None:
    """Обрыв после 2-й записи: каждый изменённый путь уже в журнале, копия на месте.
    Ловит: строка журнала после os.replace; положен-новый не пишется при архиве."""
    with Stand() as s:
        dst = s.foreign("scripts/deploy.sh")
        old_blob = blob_of(dst)
        (s.proj / "scripts/tool.py").unlink()
        (s.proj / "harness/config/x.yaml").unlink()
        before = snapshot(s.proj)
        archive = s.backups / "раскладка" / "д"
        r = s.run(HARNESS_LAYOUT_ARCHIVE=archive, HARNESS_LAYOUT_KILL_AFTER_WRITE="2")
        PROBE.check("D5", "обрыв: убита SIGKILL (код -9/137)", r.rc in (-9, 137), r.brief())
        lines = journal(archive)
        PROBE.check("D5", "строки итог нет", bool(lines) and
                    not any(line.startswith("итог") for line in lines), f"журнал {lines}")
        after = snapshot(s.proj)
        changed = {rel for rel, (kind, _ino, mode) in after.items() if kind[0] == "f"
                   and (rel not in before or (before[rel][0], before[rel][2]) != (kind, mode))}
        journaled = {fields[3] for fields in action_lines(lines)}
        PROBE.check("D5", "изменено ровно 2 пути (дверь убила себя после 2-й записи)",
                    len(changed) == 2, f"изменены {sorted(changed)}")
        PROBE.check("D5", "изменённых путей вне журнала 0", bool(changed) and changed <= journaled,
                    f"вне журнала {sorted(changed - journaled)}")
        copies_ok = [blob_of(archive / "files" / fields[3]) == fields[1]
                     for fields in action_lines(lines) if fields[0] == "перекрыт"]
        PROBE.check("D5", "у каждого перекрыт в files/ копия с blob-было",
                    bool(copies_ok) and all(copies_ok), f"журнал {lines}")
        PROBE.check("D5", "blob-было deploy.sh — прежний файл проекта",
                    f"перекрыт\t{old_blob}\t{blob_of(s.pkg / 'scripts/deploy.sh')}\tscripts/deploy.sh"
                    in lines, f"журнал {lines}")
    # Обрыв после ПЕРВОЙ записи: перекрываемый файл первый в плане. Обрыв после второй
    # судит только «положен-новый»: «перекрыт», перенесённый за os.replace, успевает лечь.
    with Stand() as s:
        dst = s.foreign("scripts/deploy.sh")
        old_blob = blob_of(dst)
        archive = s.backups / "раскладка" / "д1"
        r = s.run(HARNESS_LAYOUT_ARCHIVE=archive, HARNESS_LAYOUT_KILL_AFTER_WRITE="1")
        lines = journal(archive)
        PROBE.check("D5", "обрыв после 1-й записи: убита SIGKILL, файл уже пакетный",
                    r.rc in (-9, 137) and blob_of(dst) == blob_of(s.pkg / "scripts/deploy.sh"),
                    r.brief())
        PROBE.check("D5", "обрыв после 1-й записи: «перекрыт» deploy.sh в журнале до обрыва",
                    f"перекрыт\t{old_blob}\t{blob_of(s.pkg / 'scripts/deploy.sh')}\tscripts/deploy.sh"
                    in lines, f"журнал {lines}")


def case_d6() -> None:
    """Копия не совпала: подменённая copy_to_archive портит байт — стоп, файл прежний.
    Ловит: снята сверка blob копии с blob-было."""
    with Stand() as s:
        dst = s.foreign("scripts/deploy.sh")
        r = s.run_patched("spoil-copy")
        PROBE.check("D6", "копия испорчена: код 1", r.rc == 1, r.brief())
        PROBE.check("D6", "«копия не совпала с файлом: …scripts/deploy.sh»",
                    bool(lines_naming(r.both, "копия не совпала с файлом", "scripts/deploy.sh")),
                    r.brief())
        PROBE.check("D6", "файл проекта прежний", read_bytes(dst) == FOREIGN.encode(), r.brief())


def case_d7() -> None:
    """Источник изменился: blob временного файла не равен blob плана — стоп, файл прежний.
    Ловит: снята сверка blob временного файла перед os.replace."""
    with Stand() as s:
        dst = s.foreign("scripts/deploy.sh")
        r = s.run_patched("foreign-source")
        PROBE.check("D7", "источник сменился: код 1", r.rc == 1, r.brief())
        PROBE.check("D7", "«источник изменился во время раскладки: …scripts/deploy.sh»",
                    bool(lines_naming(r.both, "источник изменился во время раскладки",
                                      "scripts/deploy.sh")), r.brief())
        PROBE.check("D7", "файл проекта прежний", read_bytes(dst) == FOREIGN.encode(), r.brief())


def case_d8() -> None:
    """Самотест целится в живое: каталог копий вне каталога паспорта стенда.
    Ловит: снято правило стенда — самотест пишет архив вне стенда."""
    with Stand() as s:
        outside = Path(tempfile.mkdtemp(prefix="layout-door-live-"))
        try:
            s.set_backup_dir(outside)
            s.foreign("scripts/deploy.sh")
            before = snapshot(s.proj)
            r = s.run()
            PROBE.check("D8", "каталог копий вне стенда: код 3 и причина",
                        r.rc == 3 and "самотест целится в живой каталог копий" in r.err, r.brief())
            PROBE.check("D8", "второй каталог после пуст", not any(outside.iterdir()),
                        f"в нём {sorted(p.name for p in outside.iterdir())}")
            after = snapshot(s.proj)
            PROBE.check("D8", "проект не тронут", before == after, snapshot_diff(before, after))
        finally:
            shutil.rmtree(outside, ignore_errors=True)
        s.set_backup_dir(s.backups)
        r = s.run(HARNESS_INSTALL_CONF=None)
        PROBE.check("D8", "пара: HARNESS_INSTALL_CONF не задан под самотестом — тот же отказ",
                    r.rc == 3 and "самотест целится в живой каталог копий" in r.err, r.brief())
        PROBE.check("D8", "пара: архива нет и проект не тронут",
                    not s.backups.exists() and snapshot(s.proj) == before, r.brief())


def case_d9() -> None:
    """Ключа BACKUP_DIR нет: перекрыть есть что — отказ с путём; только новые — ключ не читается.
    Ловит: умолчание пути; ключ читается всегда (тогда пара отказывает)."""
    with Stand() as s:
        s.harness_conf.write_text('ACTIVE_LINE=""\n', encoding="utf-8")
        s.foreign("scripts/deploy.sh")
        before = snapshot(s.proj)
        r = s.run()
        PROBE.check("D9", "ключа нет: код 3 и «ключ BACKUP_DIR пуст в <путь стенда>»",
                    r.rc == 3 and f"ключ BACKUP_DIR пуст в {s.harness_conf}" in r.err, r.brief())
        after = snapshot(s.proj)
        PROBE.check("D9", "проект не тронут", before == after, snapshot_diff(before, after))
    with Stand(laid=False) as s:
        s.harness_conf.write_text('ACTIVE_LINE=""\n', encoding="utf-8")
        r = s.run()
        PROBE.check("D9", "пара: только новые файлы без ключа — код 0, всё легло",
                    r.rc == 0 and all((s.proj / rel).is_file() for _, rel, _, _ in FILES),
                    r.brief())
        PROBE.check("D9", "пара: каталога копий нет", not s.backups.exists(), r.brief())


def case_d10() -> None:
    """Группа skills: отличие перекрывается без копии и вне «перекрыто».
    Ловит: skills с копией — архив на каждой раскладке (R2-17)."""
    with Stand() as s:
        skill = s.foreign("harness/skills/x/SKILL.md", "# правлено\n", 0o644)
        r = s.run()
        PROBE.check("D10", "skills: код 0 и файл пакета",
                    r.rc == 0 and read_bytes(skill) == read_bytes(s.pkg / "harness/skills/x/SKILL.md"),
                    r.brief())
        PROBE.check("D10", "skills: каталога копий нет", not s.backups.exists(), r.brief())
        PROBE.check("D10", "сводка «перекрыто 0»", "дверь укладки: перекрыто 0," in r.out,
                    r.brief())


def case_d11() -> None:
    """Остаток .<имя>.layout-tmp: у отличного файла перезаписан и исчез, у равного остался.
    Ловит: остаток не судится; остаток удаляется rm (И-1)."""
    with Stand() as s:
        s.foreign("scripts/deploy.sh")
        stale = s.proj / "scripts/.deploy.sh.layout-tmp"
        stale.write_text("недописано\n", encoding="utf-8")
        kept = s.proj / "harness/config/.x.yaml.layout-tmp"
        kept.write_text("недописано\n", encoding="utf-8")
        r = s.run()
        PROBE.check("D11", "остатки: код 0", r.rc == 0, r.brief())
        PROBE.check("D11", "остаток у отличного файла исчез", not os.path.lexists(stale),
                    r.brief())
        PROBE.check("D11", "остаток у равного файла остался нетронутым",
                    read_bytes(kept) == "недописано\n".encode(), r.brief())
        for rel in ("scripts/.deploy.sh.layout-tmp", "harness/config/.x.yaml.layout-tmp"):
            PROBE.check("D11", f"назван «остаток оборванной раскладки: …{rel}»",
                        bool(lines_naming(r.out, "остаток оборванной раскладки:", rel)), r.brief())


def case_d12() -> None:
    """Назначение не обычный файл или за проектом — код 3, проект не тронут.
    Ловит: тип назначения не судится (mv -f кладёт файл ВНУТРЬ каталога, rc 0)."""
    with Stand() as s:
        (s.proj / "scripts/deploy.sh").unlink()
        (s.proj / "scripts/deploy.sh").mkdir()
        (s.proj / "scripts/deploy.sh/внутри.txt").write_text("своё\n", encoding="utf-8")
        before = snapshot(s.proj)
        r = s.run()
        PROBE.check("D12", "каталог на месте файла: код 3, «назначение не обычный файл: …»",
                    r.rc == 3 and bool(lines_naming(r.err, "назначение не обычный файл:",
                                                    "scripts/deploy.sh")), r.brief())
        after = snapshot(s.proj)
        PROBE.check("D12", "каталог: проект не тронут", before == after, snapshot_diff(before, after))
    with Stand() as s:
        target = s.t / "чужой.txt"
        target.write_text("чужое\n", encoding="utf-8")
        (s.proj / "scripts/deploy.sh").unlink()
        (s.proj / "scripts/deploy.sh").symlink_to(target)
        before = snapshot(s.proj)
        r = s.run()
        PROBE.check("D12", "ссылка на месте файла: код 3, «назначение не обычный файл: …»",
                    r.rc == 3 and bool(lines_naming(r.err, "назначение не обычный файл:",
                                                    "scripts/deploy.sh")), r.brief())
        PROBE.check("D12", "ссылка: проект и цель ссылки не тронуты",
                    snapshot(s.proj) == before and read_bytes(target) == "чужое\n".encode(),
                    r.brief())
    with Stand() as s:
        outside = s.t / "вне"
        outside.mkdir()
        shutil.rmtree(s.proj / "harness/config")
        (s.proj / "harness/config").symlink_to(outside)
        before = snapshot(s.proj)
        r = s.run()
        PROBE.check("D12", "родитель ссылкой за проект: код 3, «за проектом: …»",
                    r.rc == 3 and bool(lines_naming(r.err, "за проектом:", "harness/config/x.yaml")),
                    r.brief())
        PROBE.check("D12", "за проектом: проект и чужой каталог не тронуты",
                    snapshot(s.proj) == before and not any(outside.iterdir()), r.brief())


def case_d13() -> None:
    """Источник — ссылка: не кладётся, названа; остальное кладётся.
    Ловит: дверь кладёт ссылку (или её цель) как файл пакета."""
    with Stand(laid=False) as s:
        (s.pkg / "scripts/link.sh").symlink_to("deploy.sh")
        plan = s.plan + [("scripts", str(s.pkg / "scripts/link.sh"), "scripts/link.sh")]
        r = s.run(plan)
        PROBE.check("D13", "источник-ссылка: код 0", r.rc == 0, r.brief())
        PROBE.check("D13", "ссылка не положена", not os.path.lexists(s.proj / "scripts/link.sh"),
                    r.brief())
        PROBE.check("D13", "ссылка названа в выводе", "link.sh" in r.both, r.brief())
        PROBE.check("D13", "остальные файлы плана легли",
                    all(read_bytes(s.proj / rel) == read_bytes(s.pkg / rel) for _, rel, _, _ in FILES),
                    r.brief())


def case_d14() -> None:
    """Все беды списком: каталог на месте файла И архив недоступен — обе в выводе.
    Ловит: ход 1 останавливается на первой беде."""
    with Stand() as s:
        (s.t / "файл").write_text("обычный файл\n", encoding="utf-8")
        s.set_backup_dir(f"{s.t}/файл/копии")
        s.foreign("harness/config/x.yaml", "ключ: правлено\n", 0o644)
        (s.proj / "scripts/deploy.sh").unlink()
        (s.proj / "scripts/deploy.sh").mkdir()
        before = snapshot(s.proj)
        r = s.run()
        PROBE.check("D14", "две беды: код 3", r.rc == 3, r.brief())
        PROBE.check("D14", "в выводе обе: «назначение не обычный файл» и «копию снять некуда»",
                    "назначение не обычный файл:" in r.err and "копию снять некуда" in r.err,
                    r.brief())
        after = snapshot(s.proj)
        PROBE.check("D14", "проект не тронут", before == after, snapshot_diff(before, after))


def case_d15() -> None:
    """Пробные двери вне самотеста не действуют и названы.
    Ловит: боевая раскладка убивает себя по переменной из окружения."""
    with Stand(laid=False) as s:
        plan_out = s.t / "план.out"
        r = s.run(HARNESS_SELFTEST=None, HARNESS_LAYOUT_KILL_AFTER_WRITE="1",
                  HARNESS_LAYOUT_PLAN_OUT=plan_out)
        PROBE.check("D15", "вне самотеста: код 0 и всё легло",
                    r.rc == 0 and all((s.proj / rel).is_file() for _, rel, _, _ in FILES),
                    r.brief())
        PROBE.check("D15", "«HARNESS_LAYOUT_KILL_AFTER_WRITE вне самотеста — не действует»",
                    "HARNESS_LAYOUT_KILL_AFTER_WRITE вне самотеста — не действует" in r.both,
                    r.brief())
        PROBE.check("D15", "«HARNESS_LAYOUT_PLAN_OUT вне самотеста — не действует», файла нет",
                    "HARNESS_LAYOUT_PLAN_OUT вне самотеста — не действует" in r.both
                    and not plan_out.exists(), r.brief())


def case_d16() -> None:
    """Повтор того же плана: ни один файл не переписан, архивов +0; план двери = план стенда.
    Ловит: укладка без сличения (новый mtime/inode); архив на пустом прогоне."""
    with Stand() as s:
        s.foreign("scripts/deploy.sh")
        r = s.run()
        PROBE.check("D16", "первый прогон: код 0 и один архив", r.rc == 0 and s.all_runs() == 1,
                    f"архивов {s.all_runs()}; {r.brief()}")
        stamps = {rel: (os.lstat(s.proj / rel).st_ino, os.lstat(s.proj / rel).st_mtime_ns)
                  for _, rel, _, _ in FILES if (s.proj / rel).exists()}
        plan_out = s.t / "план.out"
        r = s.run(HARNESS_LAYOUT_PLAN_OUT=plan_out)
        PROBE.check("D16", "повтор: код 0", r.rc == 0, r.brief())
        again = {rel: (os.lstat(s.proj / rel).st_ino, os.lstat(s.proj / rel).st_mtime_ns)
                 for rel in stamps if (s.proj / rel).exists()}
        PROBE.check("D16", "повтор: inode и mtime каждого файла плана прежние",
                    len(stamps) == len(FILES) and again == stamps,
                    f"изменились {sorted(k for k in stamps if again.get(k) != stamps[k])}")
        PROBE.check("D16", "повтор: архивов прибавилось 0", s.all_runs() == 1,
                    f"архивов {s.all_runs()}")
        raw = read_bytes(plan_out) or b""
        accepted = [tuple(rec.split("\t")) for rec in raw.decode("utf-8", "replace").split("\0") if rec]
        PROBE.check("D16", "PLAN_OUT непустой и равен плану стенда",
                    bool(accepted) and set(accepted) == set(s.plan),
                    f"принято {len(accepted)}: {accepted[:2]}")


def case_d17() -> None:
    """HARNESS_LAYOUT_ARCHIVE вне <копии>/раскладка — отказ; внутри — работа.
    Ловит: снята проверка границы архива (§0)."""
    with Stand() as s:
        s.foreign("scripts/deploy.sh")
        stray = s.backups / "чужое"
        before = snapshot(s.proj)
        r = s.run(HARNESS_LAYOUT_ARCHIVE=stray)
        PROBE.check("D17", "архив вне раскладка/: код 3, «архив вне каталога копий»",
                    r.rc == 3 and "архив вне каталога копий" in r.err, r.brief())
        PROBE.check("D17", "чужой каталог не создан, проект не тронут",
                    not stray.exists() and snapshot(s.proj) == before, r.brief())
        own = s.backups / "раскладка" / "свой"
        r = s.run(HARNESS_LAYOUT_ARCHIVE=own)
        PROBE.check("D17", "пара: архив внутри раскладка/ — код 0 и строка перекрыт",
                    r.rc == 0 and any(f[0] == "перекрыт" for f in action_lines(journal(own))),
                    r.brief())


def case_d18() -> None:
    """Запись плана за проект («..», ведущий «/») — обе беды поимённо, код 3.
    Ловит: назначение плана не судится — файл ложится вне проекта."""
    with Stand() as s:
        s.foreign("scripts/deploy.sh")
        tool = str(s.pkg / "scripts/tool.py")
        absolute = f"{s.t}/абс.txt"
        plan = s.plan + [("scripts", tool, "../вне.txt"), ("scripts", tool, absolute)]
        before = snapshot(s.proj)
        r = s.run(plan)
        PROBE.check("D18", "запись за проект: код 3", r.rc == 3, r.brief())
        PROBE.check("D18", "«за проектом:» для «../вне.txt» и для ведущего «/»",
                    bool(lines_naming(r.err, "за проектом:", "../вне.txt"))
                    and bool(lines_naming(r.err, "за проектом:", absolute)), r.brief())
        PROBE.check("D18", "вне проекта ничего не легло, проект не тронут",
                    not (s.t / "вне.txt").exists() and not Path(absolute).exists()
                    and snapshot(s.proj) == before, r.brief())
        inside = "scripts/../внутри.txt"
        r = s.run(s.plan + [("scripts", tool, inside)])
        PROBE.check("D18", "пара: «..» в тексте, но путь остаётся в проекте — код 3, «за проектом:»",
                    r.rc == 3 and bool(lines_naming(r.err, "за проектом:", inside))
                    and not (s.proj / "внутри.txt").exists()
                    and snapshot(s.proj) == before, r.brief())


def case_d19() -> None:
    """PROJECT_NAME пуст, архив нужен — отказ, архива нет; только новые — работа.
    Ловит: архив с пустым именем проекта (чужие прогоны неотличимы)."""
    with Stand() as s:
        s.install_conf.write_text(f'PROJECT_DIR="{s.proj}"\n', encoding="utf-8")
        s.foreign("scripts/deploy.sh")
        before = snapshot(s.proj)
        r = s.run()
        PROBE.check("D19", "PROJECT_NAME пуст: код 3, «PROJECT_NAME пуст в <install.conf>»",
                    r.rc == 3 and f"PROJECT_NAME пуст в {s.install_conf}" in r.err, r.brief())
        PROBE.check("D19", "архива нет, проект не тронут",
                    not (s.backups / "раскладка").exists() and snapshot(s.proj) == before,
                    r.brief())
    with Stand(laid=False) as s:
        s.install_conf.write_text(f'PROJECT_DIR="{s.proj}"\n', encoding="utf-8")
        r = s.run()
        PROBE.check("D19", "пара: только новые файлы — код 0, всё легло",
                    r.rc == 0 and all((s.proj / rel).is_file() for _, rel, _, _ in FILES),
                    r.brief())


def case_d20() -> None:
    """Копия уже есть в архиве (решение смены Р11): уже снятая копия не заменяется никогда.
    Ловит: вторая копия затирает копию подлинника (И-1)."""
    with Stand() as s:
        s.foreign("scripts/deploy.sh")
        archive = s.backups / "раскладка" / "повтор"
        earlier = archive / "files" / "scripts/deploy.sh"
        earlier.parent.mkdir(parents=True)
        earlier.write_text("подлинник прежнего прогона\n", encoding="utf-8")
        earlier_blob = blob_of(earlier)
        before = snapshot(s.proj)
        r = s.run(HARNESS_LAYOUT_ARCHIVE=archive)
        PROBE.check("D20", "копия в архиве уже есть: код 3, «копия уже есть в архиве: …»",
                    r.rc == 3 and bool(lines_naming(r.err, "копия уже есть в архиве:",
                                                    "scripts/deploy.sh")), r.brief())
        PROBE.check("D20", "прежняя копия цела, проект не тронут",
                    blob_of(earlier) == earlier_blob and snapshot(s.proj) == before, r.brief())
    # Дубль РАЗНЫМИ группами (R1-06, Р7 контракта Ф-б): «(scripts, scripts)» не отличил бы
    # перепутанные группы в тексте беды.
    with Stand() as s:
        s.foreign("scripts/deploy.sh")
        plan = s.plan + [("config", str(s.pkg / "scripts/tool.py"), "scripts/deploy.sh")]
        before = snapshot(s.proj)
        r = s.run(plan)
        PROBE.check("D20", "назначение дважды в плане: код 3, «назначение дважды в плане: "
                    "scripts/deploy.sh (scripts, config)»",
                    r.rc == 3 and "назначение дважды в плане: scripts/deploy.sh (scripts, config)"
                    in r.err, r.brief())
        PROBE.check("D20", "дубль: слов «копия уже есть в архиве» нет",
                    "копия уже есть в архиве" not in r.both, r.brief())
        PROBE.check("D20", "дубль: проект не тронут", snapshot(s.proj) == before, r.brief())
    with Stand() as s:
        dst = s.foreign("scripts/deploy.sh")
        old_blob = blob_of(dst)
        archive = s.backups / "раскладка" / "заранее"
        archive.mkdir(parents=True)
        r = s.run(HARNESS_LAYOUT_ARCHIVE=archive)
        PROBE.check("D20", "пара: заранее созданный пустой архив — код 0, копия с blob-было",
                    r.rc == 0 and blob_of(archive / "files" / "scripts/deploy.sh") == old_blob,
                    r.brief())


def finished_archive(s: Stand, name: str) -> tuple:
    """Архив удачного прогона: журнал кончается «итог». → (каталог, строки журнала)."""
    archive = s.backups / "раскладка" / name
    s.foreign("scripts/deploy.sh")
    r = s.run(HARNESS_LAYOUT_ARCHIVE=archive)
    lines = journal(archive)
    if r.rc != 0 or not lines or not lines[-1].startswith("итог"):
        raise RuntimeError(f"стенд: удачный прогон не дал архива с итогом: {r.brief()}")
    return archive, lines


def case_d21() -> None:
    """Отказ хода 1 не оставляет записи архива (спека §3.2): ни каталога, ни шапки.
    Ловит: архив готовится при уже найденных бедах; «копия уже есть» после makedirs —
    журнал без «итог» читается Ф-г «прогон оборван», чужому журналу дописана шапка."""
    with Stand() as s:
        (s.proj / "scripts/deploy.sh").unlink()
        (s.proj / "scripts/deploy.sh").mkdir()
        s.foreign("harness/config/x.yaml", "ключ: правлено\n", 0o644)
        before = snapshot(s.proj)
        r = s.run()
        PROBE.check("D21", "каталог на месте файла + годный архив: код 3, беда названа",
                    r.rc == 3 and bool(lines_naming(r.err, "назначение не обычный файл:",
                                                    "scripts/deploy.sh")), r.brief())
        PROBE.check("D21", "архивов копии/раскладка/* 0 (отказ не оставил шапку без итога)",
                    s.all_runs() == 0,
                    f"архивов {s.all_runs()}, журнал {[journal(p) for p in s.runs()][:1]}")
        PROBE.check("D21", "проект не тронут", snapshot(s.proj) == before,
                    snapshot_diff(before, snapshot(s.proj)))
    with Stand() as s:
        archive, lines = finished_archive(s, "законченный")
        (s.proj / "harness/config/x.yaml").write_text("ключ: правлено\n", encoding="utf-8")
        (s.proj / "scripts/tool.py").unlink()
        (s.proj / "scripts/tool.py").mkdir()
        r = s.run(HARNESS_LAYOUT_ARCHIVE=archive)
        PROBE.check("D21", "заданный архив + каталог на месте файла: код 3", r.rc == 3, r.brief())
        after = journal(archive)
        PROBE.check("D21", "чужой законченный журнал: строк прежнее число, последняя «итог»",
                    after == lines, f"было {len(lines)}, стало {len(after)}: {after[len(lines):]}")
    with Stand() as s:
        archive, lines = finished_archive(s, "законченный")
        s.foreign("scripts/deploy.sh", "#!/bin/sh\necho второе правлено\n")
        before = snapshot(s.proj)
        r = s.run(HARNESS_LAYOUT_ARCHIVE=archive)
        PROBE.check("D21", "заданный архив уже несёт копию: код 3, «копия уже есть в архиве»",
                    r.rc == 3 and bool(lines_naming(r.err, "копия уже есть в архиве:",
                                                    "scripts/deploy.sh")), r.brief())
        after = journal(archive)
        PROBE.check("D21", "«копия уже есть»: строк журнала прежнее число, шапка не дописана",
                    after == lines, f"было {len(lines)}, стало {len(after)}: {after[len(lines):]}")
        PROBE.check("D21", "«копия уже есть»: проект не тронут", snapshot(s.proj) == before,
                    snapshot_diff(before, snapshot(s.proj)))


def case_d22() -> None:
    """Нечитаемый файл хода 1 — беда хода 1: код 3, маркер, путь и причина ОС; не трасса.
    Ловит: PermissionError из blob()/stat летит трассой с кодом 1 — update.py читает
    «сбой середины» и раскладывает прежнюю версию поверх нетронутого проекта."""
    if os.geteuid() == 0:
        print("  ПРОПУЩЕН D22: под root режим 000 читается — случай невоспроизводим, "
              "прогоните от имени агента")
        return
    with Stand() as s:
        dst = s.foreign("scripts/deploy.sh", mode=0o000)
        before = snapshot(s.proj)
        r = s.run()
        after = snapshot(s.proj)
        os.chmod(dst, 0o700)
        PROBE.check("D22", "назначение режима 000: код 3 и маркер первой строкой",
                    r.rc == 3 and r.err.startswith(MARKER), r.brief())
        PROBE.check("D22", "назначение названо вместе с «Permission denied»",
                    bool(lines_naming(r.err, "Permission denied", "scripts/deploy.sh")), r.brief())
        PROBE.check("D22", "нет трассы Traceback", "Traceback" not in r.both, r.brief())
        PROBE.check("D22", "назначение 000: проект не тронут, архива нет",
                    before == after and s.all_runs() == 0,
                    f"{snapshot_diff(before, after)}; архивов {s.all_runs()}")
    with Stand(laid=False) as s:
        src = s.pkg / "scripts/tool.py"
        os.chmod(src, 0o000)
        r = s.run()
        os.chmod(src, 0o644)
        PROBE.check("D22", "источник режима 000: код 3, маркер, путь и «Permission denied»",
                    r.rc == 3 and r.err.startswith(MARKER)
                    and bool(lines_naming(r.err, "Permission denied", str(src))), r.brief())
        PROBE.check("D22", "источник 000: в пустой проект не легло ни одного файла",
                    not any(s.proj.iterdir()), f"в проекте {sorted(p.name for p in s.proj.iterdir())}")


# ══ Ф-б: опись раскладки (PROJECT_DIR/.layout-manifest) на уровне двери ══════
# Опись судится ЗДЕСЬ своим разбором байт, не модулем описи: проба двери не верит
# тому, кого проверяет вместе с ней.

MANIFEST = ".layout-manifest"
MANIFEST_TMP = "..layout-manifest.layout-tmp"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def manifest_text(body, kept=(), package: str = "-") -> str:
    """body: (blob, группа, путь); kept: (blob, причина, путь). Договор §3.3."""
    order = lambda row: row[2].encode("utf-8")
    lines = ["# layout-manifest 2", f"# пакет {package}"]
    lines += ["\t".join(row) for row in sorted(body, key=order)]
    lines.append("# оставлено")
    lines += ["\t".join(row) for row in sorted(kept, key=order)]
    return "\n".join(lines) + "\n"


def read_manifest(path) -> dict | None:
    """Опись своим разбором: шапка, тело {путь: (blob, группа)}, оставлено, байты."""
    raw = read_bytes(path)
    if raw is None:
        return None
    lines = raw.decode("utf-8", "replace").split("\n")
    cut = lines.index("# оставлено") if "# оставлено" in lines else len(lines)
    rows = lambda chunk: {f[2]: (f[0], f[1]) for f in (l.split("\t") for l in chunk if l)
                          if len(f) == 3}
    return {"head": lines[:2], "body": rows(lines[2:cut]), "kept": rows(lines[cut + 1:]),
            "raw": raw, "has_kept": cut < len(lines)}


def plan_rows(s: Stand) -> dict:
    """Ожидаемое тело по стенду: файлы плана без группы skills."""
    return {rel: (blob_of(s.pkg / rel), group) for group, rel, _, _ in FILES if group != "skills"}


def as_rows(rows: dict) -> list:
    return [(b, g, p) for p, (b, g) in rows.items()]


def put_prior(s: Stand, text) -> bytes:
    data = text.encode("utf-8") if isinstance(text, str) else text
    (s.proj / MANIFEST).write_bytes(data)
    return data


def manifest_stat(s: Stand):
    try:
        st = os.lstat(s.proj / MANIFEST)
    except OSError:
        return None
    return st.st_ino, st.st_mtime_ns


def put_file(s: Stand, rel: str, text: str) -> str:
    path = s.proj / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return blob_of(path)


def case_d23() -> None:
    """Первая опись: тело = план стенда без skills, «# пакет -», «опись заведена: N».
    Зелен на поломке? «skills в описи» — нет: FILES несёт skills, тело сравнивается равенством."""
    PROBE.check("D23", "стенд несёт запись группы skills (иначе исключение не судится)",
                any(group == "skills" for group, _, _, _ in FILES), "в FILES нет skills")
    with Stand(laid=False) as s:
        r = s.run()
        m = read_manifest(s.proj / MANIFEST)
        expected = plan_rows(s)
        PROBE.check("D23", "первая раскладка: код 0 и .layout-manifest есть",
                    r.rc == 0 and m is not None, r.brief())
        m = m or {"head": [], "body": {}, "kept": {}, "raw": b"", "has_kept": False}
        PROBE.check("D23", "шапка «# layout-manifest 2», «# пакет -» (довода нет)",
                    m["head"] == ["# layout-manifest 2", "# пакет -"], f"шапка {m['head']}")
        PROBE.check("D23", f"тело == план стенда без skills ({len(expected)} строк)",
                    m["body"] == expected, f"тело {m['body']}")
        PROBE.check("D23", "секция «# оставлено» пустая, одной строкой, файл кончается «\\n»",
                    m["has_kept"] and m["kept"] == {} and m["raw"].endswith("# оставлено\n".encode()),
                    f"конец {m['raw'][-40:]!r}")
        PROBE.check("D23", f"stdout «опись заведена: {len(expected)} …; лишнее начну считать "
                    "со следующей раскладки»",
                    f"опись заведена: {len(expected)} " in r.out
                    and "лишнее начну считать со следующей раскладки" in r.out, r.brief())


def case_d24() -> None:
    """Повтор не переписывает опись (R1-18); другой коммит пакета — переписывает.
    Зелен на поломке? «пишется всегда» — нет: os.replace даёт новый inode на повторе."""
    with Stand(laid=False) as s:
        s.run()
        first = manifest_stat(s)
        body = (read_manifest(s.proj / MANIFEST) or {}).get("body")
        r = s.run()
        PROBE.check("D24", "повтор: код 0, inode и mtime_ns описи прежние",
                    r.rc == 0 and first is not None and manifest_stat(s) == first,
                    f"было {first}, стало {manifest_stat(s)}; {r.brief()}")
        r = s.run(args=("--package-commit", COMMIT))
        m = read_manifest(s.proj / MANIFEST) or {"head": [], "body": None}
        after = manifest_stat(s)
        PROBE.check("D24", "другой --package-commit: код 0, опись переписана (inode новый)",
                    r.rc == 0 and first is not None and after is not None and after[0] != first[0],
                    f"было {first}, стало {after}; {r.brief()}")
        PROBE.check("D24", f"шапка «# пакет {COMMIT[:12]}…», тело то же",
                    m["head"][1:2] == [f"# пакет {COMMIT}"] and body is not None and m["body"] == body,
                    f"шапка {m['head']}")


def case_d25() -> None:
    """Упавшая укладка опись не пишет: отказ хода 1 (код 3) и сбой хода 2 (код 1) при
    ПРЕЖНЕЙ описи; пара — удачный прогон её переписывает.
    Зелен на поломке? «опись пишется при коде 1» — нет: прежняя опись несёт строку,
    которую удачный прогон снимает («уже не было»), байты обязаны остаться прежними."""
    def prior(s: Stand) -> bytes:
        gone = ("d" * 40, "scripts", "scripts/gone.sh")
        return put_prior(s, manifest_text(as_rows(plan_rows(s)) + [gone]))

    with Stand() as s:
        was = prior(s)
        ino = manifest_stat(s)
        (s.proj / "scripts/deploy.sh").unlink()
        (s.proj / "scripts/deploy.sh").mkdir()
        r = s.run()
        PROBE.check("D25", "отказ хода 1 (каталог на месте файла): код 3, опись побайтно и "
                    "по inode прежняя", r.rc == 3 and read_bytes(s.proj / MANIFEST) == was
                    and manifest_stat(s) == ino, r.brief())
    with Stand() as s:
        was = prior(s)
        ino = manifest_stat(s)
        s.foreign("scripts/deploy.sh")
        r = s.run_patched("spoil-copy")
        PROBE.check("D25", "сбой хода 2 (копия испорчена): код 1, опись побайтно и по inode прежняя",
                    r.rc == 1 and read_bytes(s.proj / MANIFEST) == was and manifest_stat(s) == ino,
                    r.brief())
    with Stand() as s:
        was = prior(s)
        r = s.run()
        m = read_manifest(s.proj / MANIFEST) or {"body": {}}
        PROBE.check("D25", "пара: удачный прогон — код 0, опись переписана, gone.sh ушёл",
                    r.rc == 0 and read_bytes(s.proj / MANIFEST) != was
                    and m["body"] == plan_rows(s), f"тело {m['body']}; {r.brief()}")


def case_d26() -> None:
    """Запись «источник не найден» (<группа>\\t-\\t<назначение>) — не беда хода 1:
    прежние строки группы остаются в теле и НЕ в «снято». «-» с «..» — беда «за проектом».
    Зелен на поломке? «absent выкинут из плана молча» — нет: сводка обязана назвать группу.
    «предохранитель группы снят» (строки судятся как лишнее) — нет: строку группы пишут
    отдельно от суда, поэтому судится «снято 0» и «отложено 2 (группа без источника 2)»
    (ревью кода Ф-б R1-09; сводка по факту — Ф-в)."""
    with Stand() as s:
        unified = {rel: (put_file(s, rel, f"глава {rel}\n"), "unified")
                   for rel in ("UNIFIED/a.md", "UNIFIED/b.md")}
        put_prior(s, manifest_text(as_rows(plan_rows(s)) + as_rows(unified)))
        r = s.run(s.plan + [("unified", "-", "UNIFIED/")])
        PROBE.check("D26", "«unified\\t-\\tUNIFIED/»: код 0, нет «источник не обычный файл»",
                    r.rc == 0 and "источник не обычный файл" not in r.both, r.brief())
        body = (read_manifest(s.proj / MANIFEST) or {"body": {}})["body"]
        PROBE.check("D26", "прежние строки UNIFIED/ в теле с прежними blob и группой",
                    all(body.get(p) == v for p, v in unified.items()), f"тело {body}")
        PROBE.check("D26", "сводка «группа unified: источник не найден — её 2 »",
                    "группа unified: источник не найден — её 2 " in r.out, r.brief())
        summary = next((line for line in r.out.splitlines() if line.startswith("опись: в описи ")), "")
        PROBE.check("D26", "предохранитель группы: «опись: в описи 5; снято 0;» и "
                    "«отложено 2 (группа без источника 2)»",
                    summary.startswith("опись: в описи 5; снято 0;")
                    and "отложено 2 (группа без источника 2)" in summary,
                    f"сводка {summary!r}; {r.brief()}")
        r = s.run(s.plan + [("unified", "-", "UNIFIED/../x")])
        PROBE.check("D26", "«-» с «UNIFIED/../x»: код 3, «за проектом: UNIFIED/../x»",
                    r.rc == 3 and bool(lines_naming(r.err, "за проектом:", "UNIFIED/../x")),
                    r.brief())


def case_d27() -> None:
    """Нечитаемая опись: копия в архив прогона побайтно, журнал закончен «итог 0», новая
    опись заведена. Пара — архив недоступен: прежняя опись цела, проект не тронут.
    Зелен на поломке? «затёрта без копии» — нет: копия судится побайтно в files/."""
    prior = "# layout-manifest 1\nстарый формат\n".encode("utf-8")
    with Stand() as s:
        put_prior(s, prior)
        r = s.run()
        runs = s.runs()
        PROBE.check("D27", "чужая версия: код 0, один архив стенд-*", r.rc == 0 and len(runs) == 1,
                    f"архивов {len(runs)}; {r.brief()}")
        copy = runs[0] / "files" / MANIFEST if runs else s.t / "нет"
        PROBE.check("D27", "копия files/.layout-manifest побайтно прежняя",
                    read_bytes(copy) == prior, f"копия {read_bytes(copy)!r}")
        lines = journal(runs[0]) if runs else []
        PROBE.check("D27", "журнал: «# опись нечитаема: files/.layout-manifest», последняя «итог\\t-\\t-\\t0»",
                    "# опись нечитаема: files/.layout-manifest" in lines
                    and bool(lines) and lines[-1] == "итог\t-\t-\t0", f"журнал {lines}")
        m = read_manifest(s.proj / MANIFEST) or {"head": [], "body": {}}
        PROBE.check("D27", "новая опись заведена: версия 2, тело = план без skills",
                    m["head"][:1] == ["# layout-manifest 2"] and m["body"] == plan_rows(s),
                    f"шапка {m['head']}")
        PROBE.check("D27", "вывод «опись нечитаема (…), копия: …files/.layout-manifest»",
                    bool(lines_naming(r.both, "опись нечитаема (", "files/.layout-manifest")), r.brief())
    with Stand() as s:
        (s.t / "файл").write_text("обычный файл\n", encoding="utf-8")
        s.set_backup_dir(f"{s.t}/файл/копии")
        put_prior(s, prior)
        before = snapshot(s.proj)
        r = s.run()
        PROBE.check("D27", "пара, архив недоступен: код 0, опись побайтно прежняя",
                    r.rc == 0 and read_bytes(s.proj / MANIFEST) == prior, r.brief())
        PROBE.check("D27", "пара: «архив недоступен (…) — прежняя опись оставлена»",
                    "архив недоступен (" in r.both and "прежняя опись оставлена" in r.both, r.brief())
        after = snapshot(s.proj)
        PROBE.check("D27", "пара: множество проекта (путь, blob, режим, inode) прежнее",
                    before == after, snapshot_diff(before, after))


def case_d28() -> None:
    """Ключ порога из harness.conf стенда: 10 % при лишнем 50 % — отложено; нет ключа —
    «не задан — отложено 2»; не число — названо и отложено (решение смены а). Код 0 во всех
    трёх, и в Ф-в во всех трёх снятия нет (файлы лишнего на месте).
    Зелен на поломке? «ключ не читается» — нет: только прочитанный «10» даёт «порог:»;
    «не число = снимать» — нет: судится «отложено 2» и файлы на месте."""
    with Stand() as s:
        extra = {rel: (put_file(s, rel, f"лишнее {rel}\n"), "scripts")
                 for rel in ("scripts/old1.sh", "scripts/old2.sh")}
        rows = plan_rows(s)
        prior_rows = as_rows({p: rows[p] for p in ("scripts/deploy.sh", "scripts/tool.py")}) + as_rows(extra)
        for key_line, label, words in (
                ('LAYOUT_REMOVE_MAX_PERCENT="10"', "ключ 10 % при лишнем 2 из 4",
                 ("порог: снять 2 из 4", "LAYOUT_REMOVE_MAX_PERCENT=10", "отложено")),
                ("", "строки ключа нет", ("ключ LAYOUT_REMOVE_MAX_PERCENT не задан — отложено 2",)),
                ('LAYOUT_REMOVE_MAX_PERCENT="abc"', "ключ «abc»",
                 ("ключ LAYOUT_REMOVE_MAX_PERCENT не число: abc", "отложено 2"))):
            s.harness_conf.write_text(f'BACKUP_DIR="{s.backups}"\n' + (key_line + "\n" if key_line else ""),
                                      encoding="utf-8")
            put_prior(s, manifest_text(prior_rows))
            r = s.run()
            missing = [w for w in words if w not in r.out]
            PROBE.check("D28", f"{label}: код 0, в выводе {', '.join(words)}",
                        r.rc == 0 and not missing, f"нет {missing}; {r.brief()}")
            body = (read_manifest(s.proj / MANIFEST) or {"body": {}})["body"]
            PROBE.check("D28", f"{label}: строки лишнего в теле (снятия нет)",
                        all(body.get(p) == v for p, v in extra.items()), f"тело {body}")
            PROBE.check("D28", f"{label}: файлы лишнего на месте с прежним blob",
                        all(blob_of(s.proj / p) == v[0] for p, v in extra.items()),
                        f"{[(p, blob_of(s.proj / p)) for p in extra]}")


def case_d29() -> None:
    """Сбой записи описи после удачного хода 2: код 0 (файлы легли), «опись не записана»,
    прежняя опись цела, временного файла описи нет.
    Зелен на поломке? «сбой описи даёт код 1» — нет: судится код 0 и слова отказа."""
    with Stand() as s:
        was = put_prior(s, manifest_text(as_rows(plan_rows(s)) + [("d" * 40, "scripts", "scripts/gone.sh")]))
        r = s.run_patched("fail-manifest-replace")
        PROBE.check("D29", "os.replace описи отказал: код 0 и «опись не записана: … — прежняя опись оставлена»",
                    r.rc == 0 and "опись не записана:" in r.both and "прежняя опись оставлена" in r.both,
                    r.brief())
        PROBE.check("D29", "прежняя опись побайтно цела", read_bytes(s.proj / MANIFEST) == was,
                    f"опись {read_bytes(s.proj / MANIFEST)!r}")
        leftovers = sorted(p.name for p in s.proj.iterdir() if "layout-manifest" in p.name
                           and p.name != MANIFEST)
        PROBE.check("D29", f"временного {MANIFEST_TMP} нет", not leftovers, f"остались {leftovers}")


GATE_LINE = re.compile(r'\s*проверка\s+"[^"]+"\s+(.*)')
# Зов, а не упоминание: имя раскладчика и сразу за ним --selftest, строка не комментарий.
SELFTEST_CALL = re.compile(r"""^(?!\s*#).*razlozhit-harnes\.sh["'}]*\s+--selftest""", re.M)


def gate_runs_layout_selftest(gates_text: str, scripts_dir: Path) -> list:
    """Строки ворот, которые зовут самотест раскладчика: прямо или через скрипт
    scripts/, в котором стоит сам зов «razlozhit-harnes.sh … --selftest»."""
    found = []
    for line in gates_text.splitlines():
        match = GATE_LINE.match(line)
        if not match:
            continue
        command = match.group(1)
        if SELFTEST_CALL.search(command):
            found.append(line.strip())
            continue
        for word in command.split():
            script = scripts_dir / Path(word).name if word.startswith("scripts/") else None
            if (script is None or not script.is_file()
                    or script.name in ("gates.sh", Path(__file__).name)):
                continue
            if SELFTEST_CALL.search(script.read_text(encoding="utf-8", errors="replace")):
                found.append(line.strip())
                break
    return found


def case_d30() -> None:
    """Самотест раскладчика зовут ворота (ревью кода Ф-б R1-08): только он судит
    plan_absent, info/exclude, коммит пакета и связку «дверь ↔ раскладчик» (фб-а…фб-м).
    gates.sh берётся РЯДОМ с пробой, как и дверь.
    Зелен на поломке? «строка ворот есть, а реестра нет» — да: паспорт строки ворот
    судит check-registry.py, не эта проба."""
    gates = Path(__file__).parent / "gates.sh"
    PROBE.check("D30", f"{gates.name} рядом с пробой есть", gates.is_file(), f"нет {gates}")
    text = gates.read_text(encoding="utf-8") if gates.is_file() else ""
    found = gate_runs_layout_selftest(text, gates.parent)
    PROBE.check("D30", "в воротах есть строка «проверка …», зовущая razlozhit-harnes.sh --selftest",
                bool(found), "ни одной: самотест раскладчика гоняет только рука смены")


# ══ Ф-в: снятие по описи — дверь переносит лишнее в files/ архива прогона ═════
# Ключ порога стенд пишет сам (Stand.set_limit): одна строка BACKUP_DIR его не несёт,
# а без ключа Ф-в не снимает ничего. «Файл не тронут» судится наличием, blob и inode.

OLD = "scripts/old.sh"
OLD_TEXT = "#!/bin/sh\necho старое\n"


def summary_of(r: Result) -> str:
    return next((line for line in r.out.splitlines() if line.startswith("опись: в описи ")), "")


def lay_prior(s: Stand, extra: dict, keep_plan=None) -> dict:
    """Прежняя опись = строки плана (все или keep_plan) + extra {путь: текст}; файлы
    extra ложатся в проект. → {путь: blob} лишнего."""
    rows = plan_rows(s)
    if keep_plan is not None:
        rows = {p: rows[p] for p in keep_plan}
    blobs = {rel: put_file(s, rel, text) for rel, text in extra.items()}
    put_prior(s, manifest_text(as_rows(rows) + [(b, "scripts", p) for p, b in blobs.items()]))
    return blobs


def archived(s: Stand) -> dict:
    """{путь от files/: blob} по всем архивам стенда."""
    found = {}
    for run in sorted((s.backups / "раскладка").glob("*")):
        root = run / "files"
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                path = os.path.join(dirpath, name)
                found[os.path.relpath(path, root)] = blob_of(path)
    return found


def all_journal_lines(s: Stand) -> list:
    return [line for run in sorted((s.backups / "раскладка").glob("*")) for line in journal(run)]


def removed_lines(lines: list, rel: str) -> list:
    return [line for line in lines if line.startswith("снят\t") and line.endswith("\t" + rel)]


def without(snap: dict, *paths) -> dict:
    return {k: v for k, v in snap.items() if k not in paths}


def file_id(path) -> tuple:
    """(есть, blob, inode, режим): «не тронут» = всё четыре прежние."""
    try:
        st = os.lstat(path)
    except OSError:
        return (False, None, None, None)
    return (True, blob_of(path), st.st_ino, stat.S_IMODE(st.st_mode))


def patch_fired(r: Result, patch: str) -> int:
    match = re.search(rf"ПОДМЕНА {re.escape(patch)}: срабатываний (\d+)", r.err)
    return int(match.group(1)) if match else 0


def case_d31() -> None:
    """Снятие: прежняя опись = план + scripts/old.sh (blob файла на диске), ключ 100 →
    old.sh перенесён в files/ архива (blob, режим, ТОТ ЖЕ inode — перенос, а не копия),
    журнал «снят → готово → итог 1», чужой scripts/foreign.sh вне описи цел, весь прочий
    проект цел, новая опись без old.sh, сводка «снято 1: scripts/old.sh» и нули.
    Зелен на поломке? «mv → rm» — нет: blob копии в files/; «копия + rm вместо mv» — нет:
    inode; «итог не считает снят» — нет: итог 1 при перекрыто 0; «снято то, чего нет в
    описи» — нет: foreign.sh по blob+inode, остальное — снимком; «строка выпала без
    снятия» — нет: файл обязан лежать в архиве."""
    with Stand() as s:
        s.set_limit("100")
        blobs = lay_prior(s, {OLD: OLD_TEXT})
        os.chmod(s.proj / OLD, 0o750)
        old_ino = os.lstat(s.proj / OLD).st_ino
        foreign = s.foreign("scripts/foreign.sh", "#!/bin/sh\necho чужой\n", 0o700)
        foreign_before = file_id(foreign)
        before = without(snapshot(s.proj), OLD, MANIFEST)
        r = s.run()
        PROBE.check("D31", "снятие: код 0", r.rc == 0, r.brief())
        PROBE.check("D31", "scripts/old.sh в проекте нет", not os.path.lexists(s.proj / OLD),
                    f"{file_id(s.proj / OLD)}; {r.brief()}")
        runs = s.runs()
        PROBE.check("D31", "один архив копии/раскладка/стенд-*", len(runs) == 1,
                    f"архивов {len(runs)}")
        copy = runs[0] / "files" / OLD if runs else s.t / "нет"
        PROBE.check("D31", "files/scripts/old.sh: blob прежний", blob_of(copy) == blobs[OLD],
                    f"blob {blob_of(copy)!r}, ждали {blobs[OLD]}")
        PROBE.check("D31", "files/scripts/old.sh: режим 750 прежний", mode_of(copy) == 0o750,
                    f"режим {mode_of(copy)!r}")
        PROBE.check("D31", "перенос, а не копия + rm: inode в архиве прежний",
                    file_id(copy)[2] == old_ino, f"inode {file_id(copy)[2]}, был {old_ino}")
        lines = journal(runs[0]) if runs else []
        removed = f"снят\t{blobs[OLD]}\t-\t{OLD}"
        done = f"готово\t-\t-\t{OLD}"
        PROBE.check("D31", "журнал: «снят\\t<blob описи>\\t-\\tscripts/old.sh», затем «готово»",
                    removed in lines and done in lines and lines.index(removed) < lines.index(done),
                    f"журнал {lines}")
        PROBE.check("D31", "последняя строка «итог\\t-\\t-\\t1» (снят считается)",
                    bool(lines) and lines[-1] == "итог\t-\t-\t1", f"журнал {lines}")
        PROBE.check("D31", "чужой scripts/foreign.sh вне описи: есть, blob, inode, режим прежние",
                    file_id(foreign) == foreign_before, f"{foreign_before} → {file_id(foreign)}")
        after = without(snapshot(s.proj), MANIFEST)
        PROBE.check("D31", "прочий проект не тронут (снимок без old.sh и описи)", after == before,
                    snapshot_diff(before, after))
        m = read_manifest(s.proj / MANIFEST) or {"body": {}, "kept": {}}
        PROBE.check("D31", "новая опись: тело = план, old.sh нет ни в теле, ни в «оставлено»",
                    m["body"] == plan_rows(s) and OLD not in m["kept"],
                    f"тело {m['body']}; оставлено {m['kept']}")
        summary = summary_of(r)
        PROBE.check("D31", "сводка «опись: в описи 4; снято 1: scripts/old.sh; перекрыто 0;»",
                    summary.startswith(f"опись: в описи 4; снято 1: {OLD}; перекрыто 0;"),
                    f"сводка {summary!r}")
        PROBE.check("D31", "нули печатаются: «оставлено 0», «уже не было 0»; «снял бы» нет",
                    "оставлено 0" in summary and summary.endswith("уже не было 0")
                    and "снял бы" not in r.both, f"сводка {summary!r}")


def case_d32() -> None:
    """Опустевшие каталоги снимаются до корня укладки, корень — никогда; каталог с чужим
    файлом остаётся; каталоги в журнал не пишутся.
    Зелен на поломке? «rmdir не делается» — нет: scripts/sub и scripts/a обязаны исчезнуть;
    «rmdir через корень» — нет: UNIFIED/ опустел и обязан остаться; «rmtree вместо rmdir»
    — нет: harness/demo/foreign.txt судится blob+inode."""
    with Stand() as s:
        s.set_limit("100")
        extra = {"scripts/sub/old.sh": "один в каталоге\n", "scripts/a/b/old.sh": "глубоко\n",
                 "harness/demo/old.yaml": "старое: да\n", "UNIFIED/only.md": "# одна глава\n"}
        blobs = lay_prior(s, extra)
        foreign = s.foreign("harness/demo/foreign.txt", "чужой\n", 0o644)
        foreign_before = file_id(foreign)
        r = s.run()
        PROBE.check("D32", "код 0, все 4 файла в files/ с прежним blob",
                    r.rc == 0 and all(archived(s).get(p) == b for p, b in blobs.items()),
                    f"в архиве {archived(s)}; {r.brief()}")
        PROBE.check("D32", "scripts/sub и scripts/a (цепочка a/b) сняты rmdir",
                    not os.path.lexists(s.proj / "scripts/sub")
                    and not os.path.lexists(s.proj / "scripts/a"),
                    f"sub {os.path.lexists(s.proj / 'scripts/sub')}, a {os.path.lexists(s.proj / 'scripts/a')}")
        PROBE.check("D32", "корень укладки scripts/ на месте", (s.proj / "scripts").is_dir(), "")
        # The stand's project root, not a chapter search: written with the slash so the
        # chapter-seekers gate does not read it as a path bound to this file's place.
        chapters = s.proj / "UNIFIED/"
        PROBE.check("D32", "корень укладки UNIFIED/ опустел, но на месте (корень не снимается)",
                    chapters.is_dir() and not os.path.lexists(chapters / "only.md"),
                    f"UNIFIED {os.path.lexists(chapters)}")
        PROBE.check("D32", "harness/demo с чужим файлом на месте, foreign.txt не тронут",
                    (s.proj / "harness/demo").is_dir() and file_id(foreign) == foreign_before,
                    f"{foreign_before} → {file_id(foreign)}")
        lines = all_journal_lines(s)
        dirs = {"scripts/sub", "scripts/a", "scripts/a/b", "harness/demo", "UNIFIED"}
        PROBE.check("D32", "каталоги в журнал не пишутся",
                    not [l for l in lines if l.split("\t")[-1].rstrip("/") in dirs],
                    f"журнал {lines}")


def case_d33() -> None:
    """Обрыв и порядок «строка снят → перенос → готово».
    Обрыв: два кандидата, план весь равен (записей укладки 0), KILL_AFTER_WRITE=1 → ровно
    один путь исчез, у него строка «снят» и файл в files/; второй на месте и БЕЗ «снят».
    Порядок: подмена os.rename/os.replace в момент переноса читает журнал архива.
    Зелен на поломке? «строка снят после переноса» — нет: «порядок» видит журнал в момент
    переноса (обрыв этого не различает: убийство идёт после обоих); «снято без строки» —
    нет: исчезнувший путь обязан иметь «снят»; «удалён без копии» — нет: blob в files/."""
    pair = {"scripts/old1.sh": "первый\n", "scripts/old2.sh": "второй\n"}
    with Stand() as s:
        s.set_limit("100")
        blobs = lay_prior(s, pair)
        r = s.run(HARNESS_LAYOUT_KILL_AFTER_WRITE=1)
        lines = all_journal_lines(s)
        gone = [p for p in pair if not os.path.lexists(s.proj / p)]
        PROBE.check("D33", "обрыв: код ≠ 0, строки «итог» нет",
                    r.rc not in (0, None) and not [l for l in lines if l.startswith("итог")],
                    f"журнал {lines}; {r.brief()}")
        PROBE.check("D33", "обрыв: из проекта исчез ровно один путь", len(gone) == 1,
                    f"исчезли {gone}")
        PROBE.check("D33", "обрыв: исчезнувший назван «снят» с blob описи и лежит в files/",
                    len(gone) == 1 and f"снят\t{blobs[gone[0]]}\t-\t{gone[0]}" in lines
                    and archived(s).get(gone[0]) == blobs[gone[0]],
                    f"журнал {lines}; архив {archived(s)}")
        rest = [p for p in pair if p not in gone]
        PROBE.check("D33", "обрыв: второй на месте с прежним blob и без строки «снят»",
                    len(rest) == 1 and blob_of(s.proj / rest[0]) == blobs[rest[0]]
                    and not removed_lines(lines, rest[0]), f"на месте {rest}; журнал {lines}")
    with Stand() as s:
        s.set_limit("100")
        lay_prior(s, pair)
        r = s.run_patched("order-spy")
        PROBE.check("D33", "порядок: код 0, перенос в архив перехвачен дважды",
                    r.rc == 0 and patch_fired(r, "order-spy") == 2, r.brief())
        PROBE.check("D33", "порядок: в момент каждого переноса строка «снят» уже в журнале",
                    all(f"ПОРЯДОК {p}: строка «снят» до переноса" in r.err for p in pair)
                    and "строки «снят» нет при переносе" not in r.err, r.brief())


def case_d34() -> None:
    """Архив недоступен (BACKUP_DIR внутри обычного файла), перекрывать нечего, снимать 1 →
    беда хода 1: код 3, «к снятию, а архив недоступен», путь и «Not a directory»; проект
    вместе с описью не тронут. Пара — D31 (тот же стенд с годным архивом снимает).
    Зелен на поломке? «архив нужен только перекрытию» — нет: здесь перекрывать нечего, а
    код обязан быть 3; «снятие rm без архива» — нет: снимок проекта с old.sh."""
    with Stand() as s:
        (s.t / "файл").write_text("обычный файл\n", encoding="utf-8")
        s.set_backup_dir(f"{s.t}/файл/копии")
        s.set_limit("100")
        lay_prior(s, {OLD: OLD_TEXT})
        before = snapshot(s.proj)
        r = s.run()
        PROBE.check("D34", "код 3, маркер, «1 файл прежней версии к снятию, а архив недоступен»",
                    r.rc == 3 and r.err.startswith(MARKER)
                    and "к снятию, а архив недоступен" in first_marker_line(r.err), r.brief())
        PROBE.check("D34", "подробно: scripts/old.sh и «Not a directory» одной строкой",
                    bool(lines_naming(r.err, "Not a directory", OLD)), r.brief())
        after = snapshot(s.proj)
        PROBE.check("D34", "проект и опись не тронуты (снимок с .layout-manifest)", before == after,
                    snapshot_diff(before, after))


def case_d35() -> None:
    """Разные ФС: os.rename в архив отдаёт EXDEV (стенд на одной /dev/sda1 иначе его не
    создаст) → копия, сверка blob, затем снятие исходника; копия испорчена → код 1
    «копия не совпала», исходник ЦЕЛ.
    Зелен на поломке? «EXDEV не обработан» — нет: код 0 и файл в files/; «unlink до сверки»
    — нет: порченая копия при целом исходнике обязательна; «подмена не сработала» — нет:
    счёт срабатываний ≥ 1 судится."""
    with Stand() as s:
        s.set_limit("100")
        blobs = lay_prior(s, {OLD: OLD_TEXT})
        os.chmod(s.proj / OLD, 0o750)
        r = s.run_patched("exdev")
        PROBE.check("D35", "EXDEV: подмена сработала, код 0",
                    patch_fired(r, "exdev") >= 1 and r.rc == 0, r.brief())
        runs = s.runs()
        copy = runs[0] / "files" / OLD if runs else s.t / "нет"
        PROBE.check("D35", "EXDEV: в files/ прежний blob и режим 750, в проекте old.sh нет",
                    blob_of(copy) == blobs[OLD] and mode_of(copy) == 0o750
                    and not os.path.lexists(s.proj / OLD),
                    f"blob {blob_of(copy)!r}, режим {mode_of(copy)!r}, в проекте {file_id(s.proj / OLD)}")
        lines = journal(runs[0]) if runs else []
        PROBE.check("D35", "EXDEV: журнал «снят», «готово», «итог\\t-\\t-\\t1»",
                    f"снят\t{blobs[OLD]}\t-\t{OLD}" in lines and f"готово\t-\t-\t{OLD}" in lines
                    and bool(lines) and lines[-1] == "итог\t-\t-\t1", f"журнал {lines}")
    with Stand() as s:
        s.set_limit("100")
        lay_prior(s, {OLD: OLD_TEXT})
        before = file_id(s.proj / OLD)
        r = s.run_patched("exdev-spoil")
        PROBE.check("D35", "EXDEV + порча копии: подмена сработала, код 1, «копия не совпала»",
                    patch_fired(r, "exdev-spoil") >= 1 and r.rc == 1 and "копия не совпала" in r.err,
                    r.brief())
        PROBE.check("D35", "порча копии: old.sh в проекте цел (есть, blob, inode, режим)",
                    file_id(s.proj / OLD) == before, f"{before} → {file_id(s.proj / OLD)}")
        PROBE.check("D35", "порча копии: строки «итог» нет",
                    not [l for l in all_journal_lines(s) if l.startswith("итог")],
                    f"журнал {all_journal_lines(s)}")


def case_d36() -> None:
    """Порог и законная дверь. Опись 4 строки, лишнего 3 (75 %), ключ 10 → код 0, снято 0,
    «отложено 3 (порог 3)», строки в теле, файлы на месте. Отказы довода (код 3, проект не
    тронут): пустая причина, причина с \\n, довод без значения. Затем --lift-threshold
    проба → сняты РОВНО 3, «# снять сверх порога: проба» второй строкой журнала, «снято
    сверх порога по доводу: проба», итог 3.
    Зелен на поломке? «порог роняет раскладку» — нет: код 0; «отложенные выпали из тела»
    — нет: тело судится; «довод снимает больше» — нет: множество files/ равно трём;
    «довод без значения молча выпал» (dict(zip)) — нет: ждётся код 3."""
    extra = {f"scripts/old{i}.sh": f"лишнее {i}\n" for i in (1, 2, 3)}
    with Stand() as s:
        s.set_limit("10")
        blobs = lay_prior(s, extra, keep_plan=("scripts/deploy.sh",))
        r = s.run()
        summary = summary_of(r)
        PROBE.check("D36", "порог 10 при 75 %: код 0, «снято 0», «отложено 3 (порог 3)»",
                    r.rc == 0 and "снято 0" in summary and "отложено 3 (порог 3)" in summary,
                    f"сводка {summary!r}; {r.brief()}")
        PROBE.check("D36", "порог: 3 файла на месте с прежним blob, в архиве ничего",
                    all(blob_of(s.proj / p) == b for p, b in blobs.items()) and not archived(s),
                    f"архив {archived(s)}")
        body = (read_manifest(s.proj / MANIFEST) or {"body": {}})["body"]
        PROBE.check("D36", "порог: 3 отложенные строки в теле описи",
                    all(body.get(p, ("",))[0] == b for p, b in blobs.items()), f"тело {body}")
        for args, label in ((("--lift-threshold", ""), "пустая причина"),
                            (("--lift-threshold", "про\nба"), "причина с переводом строки"),
                            (("--lift-threshold", "про\tба"), "причина с табом"),
                            (("--lift-threshold",), "довод без значения")):
            before = snapshot(s.proj)
            r = s.run(args=args)
            after = snapshot(s.proj)
            PROBE.check("D36", f"{label}: код 3, маркер, проект и опись не тронуты",
                        r.rc == 3 and r.err.startswith(MARKER) and before == after,
                        f"{snapshot_diff(before, after)}; {r.brief()}")
        r = s.run(args=("--lift-threshold", "проба"))
        PROBE.check("D36", "--lift-threshold проба: код 0, сняты ровно 3 (множество files/)",
                    r.rc == 0 and archived(s) == blobs, f"архив {archived(s)}; {r.brief()}")
        PROBE.check("D36", "довод: в проекте ни одного из трёх",
                    not any(os.path.lexists(s.proj / p) for p in blobs), "")
        lines = all_journal_lines(s)
        PROBE.check("D36", "журнал: «# снять сверх порога: проба» сразу после шапки, итог 3",
                    len(lines) > 1 and lines[0].startswith("# прогон ")
                    and lines[1] == "# снять сверх порога: проба" and lines[-1] == "итог\t-\t-\t3",
                    f"журнал {lines}")
        PROBE.check("D36", "stdout «снято сверх порога по доводу: проба» и «снято 3: …»",
                    "снято сверх порога по доводу: проба" in r.out
                    and f"снято 3: {', '.join(sorted(blobs))}" in summary_of(r), r.brief())


def case_d37() -> None:
    """Ключа порога нет → снятие не делается, «ключ LAYOUT_REMOVE_MAX_PERCENT не задан —
    отложено 3», код 0, строки в теле; следующий прогон называет их снова. Ключ «abc» →
    отложено (решение смены а), строка «не число: abc», код 0.
    Зелен на поломке? «нет ключа = снимать» (Ф-б None) — нет: файлы на месте и «отложено 3»;
    «отложенное названо один раз» — нет: второй прогон судится тем же текстом."""
    extra = {f"scripts/old{i}.sh": f"лишнее {i}\n" for i in (1, 2, 3)}
    with Stand() as s:
        s.set_limit(None)
        blobs = lay_prior(s, extra, keep_plan=("scripts/deploy.sh",))
        for run in ("первый", "второй"):
            r = s.run()
            PROBE.check("D37", f"без ключа, {run} прогон: код 0, «ключ LAYOUT_REMOVE_MAX_PERCENT "
                        "не задан — отложено 3»",
                        r.rc == 0 and "ключ LAYOUT_REMOVE_MAX_PERCENT не задан — отложено 3" in r.out,
                        r.brief())
            body = (read_manifest(s.proj / MANIFEST) or {"body": {}})["body"]
            PROBE.check("D37", f"без ключа, {run} прогон: файлы на месте, строки в теле, архив пуст",
                        all(blob_of(s.proj / p) == b and body.get(p, ("",))[0] == b
                            for p, b in blobs.items()) and not archived(s),
                        f"тело {body}; архив {archived(s)}")
    with Stand() as s:
        s.set_limit("abc")
        blobs = lay_prior(s, extra, keep_plan=("scripts/deploy.sh",))
        r = s.run()
        PROBE.check("D37", "ключ «abc»: код 0, «не число: abc», «отложено 3», «снято 0»",
                    r.rc == 0 and "ключ LAYOUT_REMOVE_MAX_PERCENT не число: abc" in r.out
                    and "отложено 3" in summary_of(r) and "снято 0" in summary_of(r),
                    f"сводка {summary_of(r)!r}; {r.brief()}")
        PROBE.check("D37", "ключ «abc»: файлы на месте, архив пуст",
                    all(blob_of(s.proj / p) == b for p, b in blobs.items()) and not archived(s),
                    f"архив {archived(s)}")


def case_d38() -> None:
    """Расписание: снятый файл, на который указывает `crontab -l`, назван; «no crontab» —
    молча; иной отказ crontab — «расписание не прочитано:», код 0. crontab — подделка
    стенда в PATH (живое расписание не читается).
    Зелен на поломке? «назвать каждый снятый» — нет: строка deploy.sh в расписании не
    снята и не должна быть названа, а «no crontab» не даёт ни строки; «отказ = пусто» —
    нет: exit 2 обязан дать «расписание не прочитано»."""
    with Stand() as s:
        s.set_limit("100")
        lay_prior(s, {OLD: OLD_TEXT})
        s.set_crontab("#!/bin/sh\ncat <<'CRON'\n"
                      f"0 4 * * * bash {s.proj}/{OLD}\n30 5 * * * bash {s.proj}/scripts/deploy.sh\n"
                      "CRON\n")
        r = s.run()
        PROBE.check("D38", "расписание с old.sh: код 0, old.sh снят",
                    r.rc == 0 and not os.path.lexists(s.proj / OLD), r.brief())
        PROBE.check("D38", f"«в расписании строка на снятый файл: {OLD}»",
                    f"в расписании строка на снятый файл: {OLD}" in r.out, r.brief())
        PROBE.check("D38", "неснятый scripts/deploy.sh из расписания не назван",
                    not lines_naming(r.both, "в расписании", "scripts/deploy.sh"), r.brief())
    with Stand() as s:
        s.set_limit("100")
        lay_prior(s, {OLD: OLD_TEXT})
        r = s.run()
        PROBE.check("D38", "«no crontab»: код 0, снят, ни «в расписании», ни «не прочитано»",
                    r.rc == 0 and not os.path.lexists(s.proj / OLD)
                    and "в расписании" not in r.both and "расписание не прочитано" not in r.both,
                    r.brief())
    with Stand() as s:
        s.set_limit("100")
        lay_prior(s, {OLD: OLD_TEXT})
        s.set_crontab(CRONTAB_BROKEN)
        r = s.run()
        PROBE.check("D38", "crontab exit 2: код 0, снят, «расписание не прочитано:»",
                    r.rc == 0 and not os.path.lexists(s.proj / OLD)
                    and "расписание не прочитано:" in r.out, r.brief())


def case_d39() -> None:
    """Заданный архив уже несёт files/scripts/old.sh (перекрывать нечего, снимать 1) →
    код 3, «копия уже есть в архиве: scripts/old.sh»; проект не тронут, чужая копия в
    архиве побайтно прежняя, журнала не появилось.
    Зелен на поломке? «taken проверяет только перекрываемые» — нет: перекрываемых 0;
    «перенос поверх чужой копии» — нет: blob чужой копии судится."""
    with Stand() as s:
        s.set_limit("100")
        lay_prior(s, {OLD: OLD_TEXT})
        archive = s.backups / "раскладка" / "занят"
        taken = archive / "files" / OLD
        taken.parent.mkdir(parents=True)
        taken.write_text("чужая копия\n", encoding="utf-8")
        taken_before = file_id(taken)
        before = snapshot(s.proj)
        r = s.run(HARNESS_LAYOUT_ARCHIVE=archive)
        PROBE.check("D39", "код 3, «копия уже есть в архиве: scripts/old.sh»",
                    r.rc == 3 and bool(lines_naming(r.err, "копия уже есть в архиве:", OLD)),
                    r.brief())
        PROBE.check("D39", "проект не тронут", snapshot(s.proj) == before,
                    snapshot_diff(before, snapshot(s.proj)))
        PROBE.check("D39", "чужая копия в архиве не тронута, journal.tsv не создан",
                    file_id(taken) == taken_before and not (archive / "journal.tsv").exists(),
                    f"{taken_before} → {file_id(taken)}")


def case_d40() -> None:
    """Состояния, в которых снимать нельзя (ключ 100, снять разрешено всё): правлен на
    1 байт, ссылка-файл, каталог, ссылка-каталог за проект, ссылка-каталог ВНУТРИ проекта
    (scripts/sub → real: перенос sub/x.sh унёс бы real/x.sh — дефект Ф-б), файла нет,
    STATE.md (ярус 1), scripts/hooks/guard_bash.py (ярус 2) → снято 0, каждый назван
    своим словом; цели ссылок побайтно прежние; проект не тронут, кроме описи.
    Затем окно перед переносом: old.sh подменён ссылкой ПОСЛЕ хода 1 (решение смены д —
    перепроверка прямо перед переносом) → ссылка цела; и правка в самый момент переноса →
    сверка blob после переноса: код 1, данные в архиве.
    Зелен на поломке? «blob читается сквозь ссылку» — нет: строки ссылок несут blob ЦЕЛИ;
    «цепочка — внутри проекта, а не равна» — нет: sub/x.sh обязан быть «ссылка»; «правлен
    снят» — нет: наличие, blob и inode; «перепроверки нет» — нет: подменённая ссылка
    обязана остаться, а парный old2.sh в том же прогоне — сняться."""
    with Stand() as s:
        s.set_limit("100")
        outside = s.t / "вне"
        outside.mkdir()
        (outside / "x.sh").write_text("вне проекта\n", encoding="utf-8")
        target = s.t / "цель-вне.sh"
        target.write_text("цель ссылки\n", encoding="utf-8")
        rows = {"scripts/edited.sh": put_file(s, "scripts/edited.sh", "echo our\n")}
        put_file(s, "scripts/edited.sh", "echo ouR\n")
        os.symlink(target, s.proj / "scripts/ln.sh")
        rows["scripts/ln.sh"] = blob_of(target)
        (s.proj / "scripts/dir.sh").mkdir()
        rows["scripts/dir.sh"] = "e" * 40
        os.symlink(outside, s.proj / "scripts/out")
        rows["scripts/out/x.sh"] = blob_of(outside / "x.sh")
        rows["scripts/sub/x.sh"] = put_file(s, "scripts/real/x.sh", "внутри проекта\n")
        os.symlink("real", s.proj / "scripts/sub")
        rows["scripts/gone.sh"] = "d" * 40
        rows["STATE.md"] = put_file(s, "STATE.md", "состояние\n")
        rows["scripts/hooks/guard_bash.py"] = put_file(s, "scripts/hooks/guard_bash.py", "страж\n")
        put_prior(s, manifest_text(as_rows(plan_rows(s)) + [(b, "scripts", p) for p, b in rows.items()]))
        watched = {p: file_id(s.proj / p) for p in
                   ("scripts/edited.sh", "scripts/ln.sh", "scripts/real/x.sh", "STATE.md",
                    "scripts/hooks/guard_bash.py")}
        targets = {target: file_id(target), outside / "x.sh": file_id(outside / "x.sh")}
        links = {p: os.readlink(s.proj / p) for p in ("scripts/ln.sh", "scripts/out", "scripts/sub")}
        before = without(snapshot(s.proj), MANIFEST)
        r = s.run()
        summary = summary_of(r)
        PROBE.check("D40", "код 0, «снято 0», «оставлено 5», «уже не было 1»",
                    r.rc == 0 and "снято 0" in summary and "оставлено 5" in summary
                    and "уже не было 1" in summary, f"сводка {summary!r}; {r.brief()}")
        for path, was in watched.items():
            PROBE.check("D40", f"{path} не тронут: есть, blob, inode, режим прежние",
                        file_id(s.proj / path) == was, f"{was} → {file_id(s.proj / path)}")
        PROBE.check("D40", "ссылки scripts/ln.sh, scripts/out, scripts/sub — ссылки с прежней целью",
                    all(os.path.islink(s.proj / p) and os.readlink(s.proj / p) == t
                        for p, t in links.items()), f"{links}")
        PROBE.check("D40", "цели ссылок за проектом побайтно прежние",
                    all(file_id(p) == was for p, was in targets.items()), f"{targets}")
        after = without(snapshot(s.proj), MANIFEST)
        PROBE.check("D40", "проект не тронут, кроме описи", after == before,
                    snapshot_diff(before, after))
        PROBE.check("D40", "в архиве ни одного файла", not archived(s), f"архив {archived(s)}")
        kept = (read_manifest(s.proj / MANIFEST) or {"kept": {}})["kept"]
        for path, reason in (("scripts/edited.sh", "правлен здесь"), ("scripts/ln.sh", "ссылка"),
                             ("scripts/dir.sh", "не файл"), ("scripts/out/x.sh", "за проектом"),
                             ("scripts/sub/x.sh", "ссылка")):
            PROBE.check("D40", f"«оставлено» в описи: {path} — {reason}",
                        kept.get(path, ("", ""))[1] == reason, f"оставлено {kept}")
        PROBE.check("D40", "отдельные строки: «уже не было: scripts/gone.sh», «не снимается: "
                    "scripts/hooks/guard_bash.py», «из описи вынут (не дело раскладки), файл "
                    "не тронут: STATE.md», «опись испорчена» нет",
                    "уже не было: scripts/gone.sh" in r.out
                    and "не снимается: scripts/hooks/guard_bash.py" in r.out
                    and "из описи вынут (не дело раскладки), файл не тронут: STATE.md" in r.out
                    and "опись испорчена" not in r.out, r.brief())
    with Stand() as s:
        s.set_limit("100")
        s.foreign("scripts/deploy.sh")
        blobs = lay_prior(s, {OLD: OLD_TEXT, "scripts/old2.sh": "второй\n"})
        copy = s.t / "вне-копия.sh"
        copy.write_text(OLD_TEXT, encoding="utf-8")
        r = s.run_patched("link-before-remove", PROBE_SWAP=s.proj / OLD, PROBE_SWAP_TO=copy)
        PROBE.check("D40", "окно: подмена ссылкой встала после первой записи укладки",
                    "ПОДМЕНА: ссылка поставлена" in r.err, r.brief())
        PROBE.check("D40", "окно: old.sh — ссылка с прежней целью, цель побайтно прежняя",
                    os.path.islink(s.proj / OLD) and os.readlink(s.proj / OLD) == str(copy)
                    and blob_of(copy) == blobs[OLD], f"{file_id(s.proj / OLD)}; {r.brief()}")
        lines = all_journal_lines(s)
        PROBE.check("D40", "окно: для old.sh нет строки «снят» и нет копии в files/",
                    not removed_lines(lines, OLD) and OLD not in archived(s),
                    f"журнал {lines}; архив {archived(s)}")
        PROBE.check("D40", "окно, пара: неподменённый old2.sh в том же прогоне снят (код 0)",
                    r.rc == 0 and removed_lines(lines, "scripts/old2.sh") != []
                    and archived(s).get("scripts/old2.sh") == blobs["scripts/old2.sh"],
                    f"архив {archived(s)}; {r.brief()}")
        m = read_manifest(s.proj / MANIFEST) or {"body": {}, "kept": {}}
        PROBE.check("D40", "окно: old.sh в описи не сирота (тело или «оставлено»)",
                    OLD in m["body"] or OLD in m["kept"], f"опись {m}")
    with Stand() as s:
        s.set_limit("100")
        lay_prior(s, {OLD: OLD_TEXT})
        r = s.run_patched("edit-in-move")
        runs = s.runs()
        copy = runs[0] / "files" / OLD if runs else s.t / "нет"
        PROBE.check("D40", "правка в момент переноса: код 1, «снятый файл изменился во время "
                    "раскладки: scripts/old.sh — он в архиве»",
                    patch_fired(r, "edit-in-move") >= 1 and r.rc == 1
                    and f"снятый файл изменился во время раскладки: {OLD} — он в архиве" in r.err,
                    r.brief())
        PROBE.check("D40", "правка в момент переноса: данные целы — в files/ правленые байты",
                    read_bytes(copy) == (OLD_TEXT.encode("utf-8") + b"!"), f"копия {read_bytes(copy)!r}")
        lines = all_journal_lines(s)
        PROBE.check("D40", "правка в момент переноса: строка «снят» есть, «итог» нет",
                    bool(removed_lines(lines, OLD)) and not [l for l in lines if l.startswith("итог")],
                    f"журнал {lines}")


def skipped_lines(out: str, rel: str) -> list:
    return [line for line in out.splitlines() if line.startswith(f"не снято: {rel} — ")]


def case_d41() -> None:
    """Окно между ходом 1 и снятием (решение смены д, ревью кода Ф-в R1-01, R1-04): ход 1
    судит путь чистым, после первой записи укладки (до хода снятия) его меняют.
    1) old.sh правлен на байт → код 0, на месте с правкой, в архиве нет, «не снято:
       scripts/old.sh — правлен здесь»; парный old2.sh снят.
    2) каталог scripts/sub унесён ЗА проект и заменён ссылкой туда (второй харнес на машине
       несёт те же blob) → файл за проектом цел и не в архиве, «не снято: scripts/sub/old.sh
       — за проектом» — слово модуля описи для того же состояния (одно правило, R1-04).
    3) scripts/sub → ссылка на scripts/real ВНУТРИ проекта → цел, «… — ссылка».
    Зелен на поломке? «перепроверки правки нет» — нет: правленый файл обязан остаться;
    «перепроверки каталога нет» — нет: файл за проектом судится по inode и blob; «строки
    "не снято" нет» — нет: строка судится по пути; «у двери своё слово» — нет: слова
    абсолютные (§3.3 спеки), не взяты у модуля."""
    with Stand() as s:
        s.set_limit("100")
        s.foreign("scripts/deploy.sh")
        blobs = lay_prior(s, {OLD: OLD_TEXT, "scripts/old2.sh": "второй\n"})
        r = s.run_patched("edit-before-remove", PROBE_SWAP=s.proj / OLD)
        PROBE.check("D41", "1 правка: подмена встала после первой записи укладки, код 0",
                    "ПОДМЕНА: файл правлен" in r.err and r.rc == 0, r.brief())
        PROBE.check("D41", "1 правка: old.sh на месте с правленными байтами",
                    read_bytes(s.proj / OLD) == OLD_TEXT.encode("utf-8") + b"!",
                    f"{file_id(s.proj / OLD)}; {r.brief()}")
        lines = all_journal_lines(s)
        PROBE.check("D41", "1 правка: для old.sh нет «снят» и нет копии в files/",
                    not removed_lines(lines, OLD) and OLD not in archived(s),
                    f"журнал {lines}; архив {archived(s)}")
        PROBE.check("D41", "1 правка, пара: old2.sh в том же прогоне снят",
                    archived(s).get("scripts/old2.sh") == blobs["scripts/old2.sh"],
                    f"архив {archived(s)}")
        PROBE.check("D41", "1 правка: строка «не снято: scripts/old.sh — правлен здесь»",
                    skipped_lines(r.out, OLD) == [f"не снято: {OLD} — правлен здесь"], r.brief())
        m = read_manifest(s.proj / MANIFEST) or {"body": {}, "kept": {}}
        PROBE.check("D41", "1 правка: old.sh в описи не сирота (тело или «оставлено»)",
                    OLD in m["body"] or OLD in m["kept"], f"опись {m}")
    for label, where, word in (("2 за проект", "вне", "за проектом"),
                               ("3 внутри проекта", "проект/scripts/real", "ссылка")):
        with Stand() as s:
            s.set_limit("100")
            s.foreign("scripts/deploy.sh")
            rel = "scripts/sub/old.sh"
            blobs = lay_prior(s, {rel: OLD_TEXT, "scripts/old2.sh": "второй\n"})
            moved_to = s.t / where / "sub" if where == "вне" else s.t / where
            moved_to.parent.mkdir(parents=True, exist_ok=True)
            was = file_id(s.proj / rel)
            r = s.run_patched("dirlink-before-remove", PROBE_SWAP=s.proj / "scripts/sub",
                              PROBE_SWAP_TO=moved_to)
            PROBE.check("D41", f"{label}: подмена каталога встала после первой записи, код 0",
                        "ПОДМЕНА: каталог подменён" in r.err and r.rc == 0, r.brief())
            PROBE.check("D41", f"{label}: файл за ссылкой цел — есть, blob, inode, режим прежние",
                        file_id(moved_to / "old.sh") == was,
                        f"{was} → {file_id(moved_to / 'old.sh')}; {r.brief()}")
            lines = all_journal_lines(s)
            PROBE.check("D41", f"{label}: для {rel} нет «снят» и нет копии в files/",
                        not removed_lines(lines, rel) and rel not in archived(s),
                        f"журнал {lines}; архив {archived(s)}")
            PROBE.check("D41", f"{label}, пара: old2.sh снят",
                        archived(s).get("scripts/old2.sh") == blobs["scripts/old2.sh"],
                        f"архив {archived(s)}")
            PROBE.check("D41", f"{label}: строка «не снято: {rel} — …» есть",
                        len(skipped_lines(r.out, rel)) == 1, r.brief())
            PROBE.check("D41", f"{label}: причина словом описи «{word}» (одно правило, R1-04)",
                        skipped_lines(r.out, rel) == [f"не снято: {rel} — {word}"], r.brief())


def case_d42() -> None:
    """Кандидат из «оставлено» (правка откачена, blob = описи), а между ходами его снова
    правят (ревью кода Ф-в R1-03; спека §3.3: «оставлено» держится между прогонами) →
    файл на месте, в архиве нет, и в НОВОЙ описи он в «оставлено» с причиной «правлен
    здесь».
    Зелен на поломке? «пропущенный дверью путь выпал из описи» — нет: опись судится своим
    разбором; «пропуск потому что снятие не шло» — нет: парный old2.sh обязан сняться."""
    with Stand() as s:
        s.set_limit("100")
        s.foreign("scripts/deploy.sh")
        row = put_file(s, OLD, OLD_TEXT)
        pair = put_file(s, "scripts/old2.sh", "второй\n")
        put_prior(s, manifest_text(as_rows(plan_rows(s)) + [(pair, "scripts", "scripts/old2.sh")],
                                   kept=[(row, "правлен здесь", OLD)]))
        r = s.run_patched("edit-before-remove", PROBE_SWAP=s.proj / OLD)
        PROBE.check("D42", "подмена встала, код 0", "ПОДМЕНА: файл правлен" in r.err and r.rc == 0,
                    r.brief())
        PROBE.check("D42", "old.sh на месте с правкой, в архиве нет",
                    read_bytes(s.proj / OLD) == OLD_TEXT.encode("utf-8") + b"!"
                    and OLD not in archived(s), f"архив {archived(s)}; {r.brief()}")
        PROBE.check("D42", "пара: old2.sh снят", archived(s).get("scripts/old2.sh") == pair,
                    f"архив {archived(s)}")
        m = read_manifest(s.proj / MANIFEST) or {"body": {}, "kept": {}}
        PROBE.check("D42", "новая опись: old.sh в «оставлено» с причиной «правлен здесь»",
                    m["kept"].get(OLD, ("", ""))[1] == "правлен здесь",
                    f"тело {sorted(m['body'])}; оставлено {m['kept']}")


def case_d43() -> None:
    """--lift-threshold снимает РОВНО отложенное прежним прогоном (спека §10 R2-06, Ф-в 1и;
    ревью кода R1-06). Прогон 1: 3 лишних отложены порогом 10. Пакет между прогонами
    потерял scripts/tool.py (новый кандидат, тоже сверх порога). Прогон 2 с доводом →
    сняты ровно 3 прежних, tool.py на месте и в теле описи.
    Зелен на поломке? «довод снимает порог для всех» — нет: tool.py судится наличием,
    blob и строкой тела; «довод не снял ничего» — нет: множество files/ равно трём."""
    extra = {f"scripts/old{i}.sh": f"лишнее {i}\n" for i in (1, 2, 3)}
    with Stand() as s:
        s.set_limit("10")
        blobs = lay_prior(s, extra, keep_plan=("scripts/deploy.sh", "scripts/tool.py"))
        r = s.run()
        PROBE.check("D43", "прогон 1: код 0, «отложено 3 (порог 3)», архив пуст",
                    r.rc == 0 and "отложено 3 (порог 3)" in summary_of(r) and not archived(s),
                    f"сводка {summary_of(r)!r}; {r.brief()}")
        tool = s.proj / "scripts/tool.py"
        tool_was = file_id(tool)
        smaller = [record for record in s.plan if record[2] != "scripts/tool.py"]
        r = s.run(plan=smaller, args=("--lift-threshold", "проба"))
        PROBE.check("D43", "прогон 2 с доводом: код 0, сняты ровно 3 отложенных прежде",
                    r.rc == 0 and archived(s) == blobs, f"архив {archived(s)}; {r.brief()}")
        PROBE.check("D43", "новый кандидат scripts/tool.py на месте (есть, blob, inode, режим)",
                    file_id(tool) == tool_was, f"{tool_was} → {file_id(tool)}")
        body = (read_manifest(s.proj / MANIFEST) or {"body": {}})["body"]
        PROBE.check("D43", "scripts/tool.py в теле новой описи (снимется следующим судом)",
                    "scripts/tool.py" in body, f"тело {sorted(body)}")


def case_d44() -> None:
    """Ключ порога, который модуль описи не принимает числом, назван ОДНОЙ строкой «ключ
    LAYOUT_REMOVE_MAX_PERCENT не число: <значение>» — и на «-5», «inf», «nan», которые
    float() разбирает (ревью кода R1-07, замер З1); снятие отложено, файлы на месте.
    Зелен на поломке? «дверь судит float()» — нет: «-5»/«inf»/«nan» без строки красны;
    «строка дважды» — нет: строк ровно одна."""
    extra = {f"scripts/old{i}.sh": f"лишнее {i}\n" for i in (1, 2, 3)}
    for raw in ("-5", "inf", "nan", "abc"):
        with Stand() as s:
            s.set_limit(raw)
            blobs = lay_prior(s, extra, keep_plan=("scripts/deploy.sh",))
            r = s.run()
            named = [line for line in r.out.splitlines()
                     if "LAYOUT_REMOVE_MAX_PERCENT" in line and "не число" in line]
            PROBE.check("D44", f"ключ «{raw}»: ровно одна строка «ключ LAYOUT_REMOVE_MAX_PERCENT "
                        f"не число: {raw}»",
                        named == [f"ключ LAYOUT_REMOVE_MAX_PERCENT не число: {raw}"],
                        f"строки {named}; {r.brief()}")
            PROBE.check("D44", f"ключ «{raw}»: код 0, «отложено 3», файлы на месте, архив пуст",
                        r.rc == 0 and "отложено 3" in summary_of(r) and not archived(s)
                        and all(blob_of(s.proj / p) == b for p, b in blobs.items()),
                        f"сводка {summary_of(r)!r}; архив {archived(s)}")


# ══ Ф-г: откат по журналу прямого прогона (--rollback-of <архив прямого>) ═════
# Прямой прогон — та же дверь с HARNESS_LAYOUT_ARCHIVE (режим обновления), откат — дверь
# с планом A, --rollback-of и своим архивом «…-откат» тем же HARNESS_LAYOUT_ARCHIVE.
# Имена архивов — по выражению «стенд-ГГГГММДД-ЧЧММСС-pid[-откат]»: дверь судит имя.
# «Множество» — (путь, blob, режим) обычных файлов корней укладки без яруса 1 описи;
# остатки *.layout-tmp считаются отдельно. Судится своим кодом, не модулем описи.

FWD_NAME = "стенд-20261024-000000-1"
ROLLBACK_ARG = "--rollback-of"
NEW = "scripts/new.sh"
NEW_TEXT = "#!/bin/sh\necho новое B\n"
DEPLOY_B = "#!/bin/sh\necho пакет B\n"
LAYOUT_ROOTS = ("scripts/", "harness/", "UNIFIED/")
NOT_IN_SET = ("harness/skills/*", "*/.установлено", "*.layout-tmp",
              "harness/config/pipeline.yaml", "harness/config/queue.yaml",
              "harness/config/models.yaml", "harness/config/audit.yaml",
              "harness/config/live-check-state.yaml")
COUNTS = re.compile(r"снято (\d+), возвращено (\d+), оставлено (\d+)(?:: (.*?))?; "
                    r"строк без «готово» (\d+)")


def layout_files(root: Path) -> list:
    found = []
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            if rel.startswith(LAYOUT_ROOTS) and stat.S_ISREG(os.lstat(path).st_mode):
                found.append(rel)
    return found


def layout_set(root: Path) -> set:
    return {(rel, blob_of(root / rel), mode_of(root / rel)) for rel in layout_files(root)
            if not any(fnmatch.fnmatchcase(rel, mask) for mask in NOT_IN_SET)}


def layout_remnants(root: Path) -> list:
    return sorted(rel for rel in layout_files(root) if rel.endswith(".layout-tmp"))


def blob_of_data(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def blob_of_text(text: str) -> str:
    return blob_of_data(text.encode("utf-8"))


def set_diff(before: set, after: set) -> str:
    return f"ушло {sorted(before - after)[:4]}; пришло {sorted(after - before)[:4]}"


def fwd_path(s: Stand) -> Path:
    return s.backups / "раскладка" / FWD_NAME


def rb_path(s: Stand) -> Path:
    return s.backups / "раскладка" / (FWD_NAME + "-откат")


def plan_b(s: Stand, changed: dict, added: dict) -> list:
    """План новой версии: записи плана A, где changed подменяет источник, плюс added."""
    root = s.t / "пакет-б"
    def source(rel: str, content: str) -> str:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o755 if rel.endswith(".sh") else 0o644)
        return str(path)
    plan = [(group, source(rel, changed[rel]) if rel in changed else src, rel)
            for group, src, rel in s.plan]
    return plan + [("scripts", source(rel, content), rel) for rel, content in added.items()]


def extend_a(s: Stand, files: dict) -> None:
    """Добавить файлы в пакет A (и уложить их в проект, как Stand кладёт свои)."""
    for rel, content in files.items():
        src = s.pkg / rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(content, encoding="utf-8")
        os.chmod(src, 0o644)
        (s.proj / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, s.proj / rel)
        s.plan.append(("scripts", str(src), rel))


def lay_a(s: Stand) -> None:
    """«До обновления»: дверь уложила A и завела опись."""
    r = s.run()
    if r.rc != 0:
        raise RuntimeError(f"стенд: раскладка A не прошла: {r.brief()}")


def forward(s: Stand, plan: list, patch: str = "", **env) -> Result:
    if patch:
        return s.run_patched(patch, plan=plan, HARNESS_LAYOUT_ARCHIVE=fwd_path(s), **env)
    return s.run(plan=plan, HARNESS_LAYOUT_ARCHIVE=fwd_path(s), **env)


def forward_ok(s: Stand, plan: list) -> None:
    r = forward(s, plan)
    if r.rc != 0:
        raise RuntimeError(f"стенд: прямой прогон B не прошёл: {r.brief()}")


def rollback(s: Stand, args=(), of=None, **env) -> Result:
    target = fwd_path(s) if of is None else of
    return s.run(args=(ROLLBACK_ARG, str(target), *args), HARNESS_LAYOUT_ARCHIVE=rb_path(s), **env)


def counts(r: Result):
    """(снято, возвращено, оставлено, пути оставленных, без «готово») строки отката."""
    line = next((l for l in r.out.splitlines()
                 if l.startswith("откат по журналу ") and FWD_NAME + ":" in l), "")
    match = COUNTS.search(line)
    if not match:
        return None
    K, M, L, kept, G = match.groups()
    return int(K), int(M), int(L), kept or "", int(G)


def forge_forward(s: Stand, lines: list, files: dict) -> Path:
    """Архив прямого прогона, записанный рукой стенда: так воспроизводятся точки обрыва,
    которые HARNESS_LAYOUT_KILL_AFTER_WRITE не достаёт (договор Ф-г, крайние случаи)."""
    archive = fwd_path(s)
    (archive / "files").mkdir(parents=True)
    (archive / "journal.tsv").write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    for rel, data in files.items():
        path = archive / "files" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return archive


def kinds_at(lines: list, kind: str, rel: str) -> list:
    return [i for i, l in enumerate(lines) if l.startswith(kind + "\t") and l.endswith("\t" + rel)]


ACTION_KINDS = ("перекрыт", "положен-новый", "снят", "возвращён")


def case_d45() -> None:
    """A → B → откат: «до» = укладка A + свой scripts/deploy.sh (X ≠ A); B = A + new.sh и
    deploy.sh = B. После отката множество = «до»; new.sh в files/ архива ОТКАТА с blob B;
    deploy.sh — X с режимом 700; журнал отката: шапка «откат … по=<прямой>», укладка A,
    затем «снят», затем «возвращён» (R1-17); «итог» = числу строк действий (К9); строка
    «откат по журналу …: снято 1, возвращено 1, оставлено 0; строк без «готово» 0».
    Зелен на поломке? «откат = обычная раскладка A» — нет: new.sh остался бы, deploy.sh
    стал бы A; «возвращён не в итоге» — нет: итог сверяется со счётом строк; «возврат до
    укладки A» — нет: укладка A затёрла бы X, а порядок строк судится."""
    with Stand() as s:
        lay_a(s)
        deploy = s.foreign("scripts/deploy.sh")
        x_blob = blob_of(deploy)
        before = layout_set(s.proj)
        forward_ok(s, plan_b(s, {"scripts/deploy.sh": DEPLOY_B}, {NEW: NEW_TEXT}))
        new_blob = blob_of(s.proj / NEW)
        r = rollback(s)
        PROBE.check("D45", "откат: код 0", r.rc == 0, r.brief())
        after = layout_set(s.proj)
        PROBE.check("D45", "множество (путь, blob, режим) = до обновления", after == before,
                    set_diff(before, after))
        PROBE.check("D45", "остатков *.layout-tmp 0", layout_remnants(s.proj) == [],
                    f"{layout_remnants(s.proj)}")
        PROBE.check("D45", "new.sh в files/ архива отката, blob B",
                    blob_of(rb_path(s) / "files" / NEW) == new_blob,
                    f"blob {blob_of(rb_path(s) / 'files' / NEW)!r}, ждали {new_blob}")
        PROBE.check("D45", "deploy.sh — свой X, режим 700",
                    blob_of(deploy) == x_blob and mode_of(deploy) == 0o700,
                    f"{file_id(deploy)}, ждали {x_blob}")
        lines = journal(rb_path(s))
        PROBE.check("D45", "шапка «# прогон <pid> откат план=4 … по=<архив прямого>»",
                    bool(lines) and re.match(r"# прогон \d+ откат план=4 перекрыть=\d+ по=", lines[0])
                    is not None and lines[0].endswith(f"по={fwd_path(s)}"), f"журнал {lines[:2]}")
        laid = kinds_at(lines, "перекрыт", "scripts/deploy.sh")
        removed = kinds_at(lines, "снят", NEW)
        restored = kinds_at(lines, "возвращён", "scripts/deploy.sh")
        PROBE.check("D45", "порядок: укладка A (перекрыт deploy.sh) → снят new.sh → возвращён deploy.sh",
                    bool(laid and removed and restored) and laid[0] < removed[0] < restored[0],
                    f"журнал {lines}")
        PROBE.check("D45", "«возвращён» несёт blob-стало X",
                    bool(restored) and lines[restored[0]].split("\t")[2] == x_blob, f"журнал {lines}")
        actions = sum(1 for l in lines if l.split("\t")[0] in ACTION_KINDS)
        PROBE.check("D45", f"итог = числу строк действий ({actions})",
                    bool(lines) and lines[-1] == f"итог\t-\t-\t{actions}", f"журнал {lines[-2:]}")
        PROBE.check("D45", "строка «откат по журналу …: снято 1, возвращено 1, оставлено 0; "
                    "строк без «готово» 0»", counts(r) == (1, 1, 0, "", 0), r.brief())


def case_d46() -> None:
    """Обрывы прямого прогона.
    1) убит HARNESS_LAYOUT_KILL_AFTER_WRITE=2 после перекрытия deploy.sh и нового new.sh
       (у new.sh нет «готово», нет «итог») → откат: множество = до, «строк без «готово» 1».
    2) «снят» без «готово», файл только в архиве и ПРАВЛЕН (Stop после rename, договор Ф-в
       состояние 3), путь в проекте пуст → возвращён правленый (решение смены (г)).
    3) то же, но путь в проекте занят → не перезаписан (есть, blob, inode), код 1, путь назван.
    Снятый путь стоит в прежней описи (отложенная строка, как оставляет Ф-в): это и отличает
    его от снятого затравкой (D48).
    Зелен на поломке? «без готово = не сделано» — нет: new.sh обязан сняться; «правленая
    копия = копии нет» — нет: 2 обязан вернуть байты; «занято — перезаписать» — нет: inode."""
    with Stand() as s:
        lay_a(s)
        s.foreign("scripts/deploy.sh")
        before = layout_set(s.proj)
        r = forward(s, plan_b(s, {"scripts/deploy.sh": DEPLOY_B}, {NEW: NEW_TEXT}),
                    HARNESS_LAYOUT_KILL_AFTER_WRITE="2")
        lines = journal(fwd_path(s))
        PROBE.check("D46", "1 стенд: прямой убит, new.sh «положен-новый» без «готово», итога нет",
                    r.rc != 0 and kinds_at(lines, "положен-новый", NEW) != []
                    and kinds_at(lines, "готово", NEW) == [] and not lines[-1].startswith("итог"),
                    f"журнал {lines}; {r.brief()}")
        r = rollback(s)
        after = layout_set(s.proj)
        PROBE.check("D46", "1 откат: код 0, множество = до", r.rc == 0 and after == before,
                    f"{set_diff(before, after)}; {r.brief()}")
        PROBE.check("D46", "1 строка отката: снято 1, возвращено 1, «строк без «готово» 1»",
                    counts(r) is not None and counts(r)[:2] == (1, 1) and counts(r)[4] == 1,
                    r.brief())
    edited = OLD_TEXT.encode("utf-8") + "правка в архиве\n".encode("utf-8")
    for label, occupied in (("2 путь пуст", None), ("3 путь занят", "#!/bin/sh\necho занято\n")):
        with Stand() as s:
            lay_a(s)
            row = blob_of_text(OLD_TEXT)
            prior = (manifest_text(as_rows(plan_rows(s)) + [(row, "scripts", OLD)])
                     + f"# отложено\n{OLD}\n")
            put_prior(s, prior)
            forge_forward(s, ["# прогон 1 обновление план=4 перекрыть=0",
                              f"# опись до прогона: files/{MANIFEST} {blob_of_text(prior)}",
                              "# снять сверх порога: проба",
                              f"снят\t{row}\t-\t{OLD}"],
                          {MANIFEST: prior.encode("utf-8"), OLD: edited})
            target = s.proj / OLD
            if occupied:
                s.foreign(OLD, occupied)
            was = file_id(target)
            r = rollback(s)
            if occupied is None:
                PROBE.check("D46", f"{label}: код 0, old.sh возвращён правленым (байты архива)",
                            r.rc == 0 and read_bytes(target) == edited,
                            f"{file_id(target)}; {r.brief()}")
                PROBE.check("D46", f"{label}: в журнале отката «возвращён» для old.sh",
                            kinds_at(journal(rb_path(s)), "возвращён", OLD) != [],
                            f"журнал {journal(rb_path(s))}")
            else:
                PROBE.check("D46", f"{label}: old.sh не тронут (есть, blob, inode, режим)",
                            file_id(target) == was, f"{was} → {file_id(target)}")
                PROBE.check("D46", f"{label}: код 1, путь назван словами",
                            r.rc == 1 and any(OLD in l for l in r.both.splitlines()), r.brief())


def case_d47() -> None:
    """Stop до replace у «перекрыт» (подменённая копия, как D6): свой scripts/own.sh (его нет в
    A, B его несёт) остался X, в прямом журнале строка без «готово». Откат → own.sh
    побайтно X и ТОТ ЖЕ inode (R1-05: возврат не писал), «строк без «готово» 1».
    Зелен на поломке? «без готово — вернуть из копии» — нет: inode сменился бы; «без готово
    не считается» — нет: G судится числом."""
    with Stand() as s:
        lay_a(s)
        own = s.foreign("scripts/own.sh", "#!/bin/sh\necho своё\n")
        was = file_id(own)
        before = layout_set(s.proj)
        r = forward(s, plan_b(s, {}, {"scripts/own.sh": "#!/bin/sh\necho пакет B own\n"}),
                    patch="spoil-copy")
        lines = journal(fwd_path(s))
        PROBE.check("D47", "стенд: прямой — код 1, «перекрыт own.sh» без «готово», own.sh прежний",
                    r.rc == 1 and kinds_at(lines, "перекрыт", "scripts/own.sh") != []
                    and kinds_at(lines, "готово", "scripts/own.sh") == [] and file_id(own) == was,
                    f"журнал {lines}; {r.brief()}")
        r = rollback(s)
        PROBE.check("D47", "откат: код 0, own.sh есть, blob X, inode и режим прежние",
                    r.rc == 0 and file_id(own) == was, f"{was} → {file_id(own)}; {r.brief()}")
        PROBE.check("D47", "журнал отката: «возвращён» для own.sh нет",
                    kinds_at(journal(rb_path(s)), "возвращён", "scripts/own.sh") == [],
                    f"журнал {journal(rb_path(s))}")
        PROBE.check("D47", "множество = до", layout_set(s.proj) == before,
                    set_diff(before, layout_set(s.proj)))
        PROBE.check("D47", "строка отката: «строк без «готово» 1»",
                    counts(r) is not None and counts(r)[4] == 1, r.brief())


def case_d48() -> None:
    """Чего откат не возвращает и не уносит.
    1) свой scripts/new.sh до обновления, B кладёт пакетный (перекрыт X→B), A его не несёт →
       после отката свой new.sh побайтно и с режимом, в архиве отката его нет (R1-17).
    Стенда «снят затравкой — не возвращать» здесь нет: это правило Ф-д, Ф-г такой путь возвращает (см. журнал задачи).
    Зелен на поломке? «снять всё, что B положила» — нет: свой new.sh ушёл бы в архив отката."""
    with Stand() as s:
        lay_a(s)
        own_text = "#!/bin/sh\necho своё новое\n"
        own = s.foreign(NEW, own_text)
        before = layout_set(s.proj)
        forward_ok(s, plan_b(s, {}, {NEW: NEW_TEXT}))
        r = rollback(s)
        PROBE.check("D48", "1 откат: код 0, свой new.sh побайтно, режим 700",
                    r.rc == 0 and read_bytes(own) == own_text.encode("utf-8") and mode_of(own) == 0o700,
                    f"{file_id(own)}; {r.brief()}")
        PROBE.check("D48", "1 new.sh не в files/ архива отката", not (rb_path(s) / "files" / NEW).exists(),
                    f"архив {archived(s)}")
        PROBE.check("D48", "1 множество = до; строка отката: снято 0, возвращено 1",
                    layout_set(s.proj) == before and counts(r) is not None and counts(r)[:2] == (0, 1),
                    f"{set_diff(before, layout_set(s.proj))}; {r.brief()}")


def case_d49() -> None:
    """Новый файл B правлен после обновления → остаётся на месте, «оставлено 1: new.sh», в
    секции «оставлено» описи с причиной «правлен здесь» и blob с диска (расхождение 15).
    Зелен на поломке? «снять по журналу без сверки диска» — нет: правка ушла бы в архив;
    «оставленный выпал из описи» — нет: секция судится своим разбором."""
    with Stand() as s:
        lay_a(s)
        forward_ok(s, plan_b(s, {}, {NEW: NEW_TEXT}))
        edited_blob = put_file(s, NEW, "#!/bin/sh\necho правлено после обновления\n")
        was = file_id(s.proj / NEW)
        r = rollback(s)
        PROBE.check("D49", "код 0, new.sh на месте с правкой (есть, blob, inode, режим)",
                    r.rc == 0 and file_id(s.proj / NEW) == was, f"{was} → {file_id(s.proj / NEW)}; {r.brief()}")
        c = counts(r)
        PROBE.check("D49", "строка отката: снято 0, оставлено 1: scripts/new.sh",
                    c is not None and c[0] == 0 and c[2] == 1 and NEW in c[3], r.brief())
        kept = (read_manifest(s.proj / MANIFEST) or {"kept": {}})["kept"]
        PROBE.check("D49", "опись: new.sh в «оставлено» — (blob с диска, «правлен здесь»)",
                    kept.get(NEW) == (edited_blob, "правлен здесь"), f"оставлено {kept}")


def case_d50() -> None:
    """Порога у отката нет (R2-07): прежняя опись 8 строк, ключ 25 %, B принесла 12 новых →
    откат снимает все 12, слова «порог» в выводе нет.
    Зелен на поломке? «judge_extra / порог и в откате» — нет: 12 из 8 при 25 % отложило бы
    всё."""
    with Stand() as s:
        extend_a(s, {f"scripts/a{i}.sh": f"файл A {i}\n" for i in range(5)})
        s.set_limit("25")
        lay_a(s)
        rows = (read_manifest(s.proj / MANIFEST) or {"body": {}})["body"]
        PROBE.check("D50", "стенд: прежняя опись 8 строк", len(rows) == 8, f"тело {sorted(rows)}")
        before = layout_set(s.proj)
        new = {f"scripts/n{i:02d}.sh": f"новое {i}\n" for i in range(12)}
        forward_ok(s, plan_b(s, {}, new))
        r = rollback(s)
        PROBE.check("D50", "код 0, все 12 сняты, все 12 в files/ архива отката",
                    r.rc == 0 and not any(os.path.lexists(s.proj / p) for p in new)
                    and all((rb_path(s) / "files" / p).is_file() for p in new),
                    f"в проекте {[p for p in new if os.path.lexists(s.proj / p)]}; {r.brief()}")
        PROBE.check("D50", "«порог» в выводе нет; строка отката «снято 12»",
                    "порог" not in r.both and counts(r) is not None and counts(r)[0] == 12, r.brief())
        PROBE.check("D50", "множество = до", layout_set(s.proj) == before,
                    set_diff(before, layout_set(s.proj)))


def case_d51() -> None:
    """Опись после отката = опись ДО обновления (расхождение 1): прежняя опись с секцией
    «# отложено» → прямой прогон пишет «# опись до прогона: files/.layout-manifest <blob>» и
    кладёт копию; после отката .layout-manifest побайтно прежний. Прежней описи не было →
    после отката опись = план A.
    Зелен на поломке? «опись после отката = план A» — нет: отложенная строка пропала бы;
    «копия не снимается» — нет: строка журнала и байты копии судятся."""
    with Stand() as s:
        put_file(s, OLD, OLD_TEXT)
        put_prior(s, manifest_text(as_rows(plan_rows(s)) + [(blob_of_text(OLD_TEXT), "scripts", OLD)]))
        lay_a(s)
        prior = read_bytes(s.proj / MANIFEST) or b""
        PROBE.check("D51", "стенд: прежняя опись несёт «# отложено»",
                    "# отложено".encode("utf-8") in prior, f"опись {prior!r}")
        forward_ok(s, plan_b(s, {}, {NEW: NEW_TEXT}))
        lines = journal(fwd_path(s))
        want = f"# опись до прогона: files/{MANIFEST} {blob_of_data(prior)}"
        PROBE.check("D51", "журнал прямого: строка «# опись до прогона: files/.layout-manifest <blob>»",
                    want in lines, f"журнал {lines}")
        PROBE.check("D51", "files/.layout-manifest архива прямого побайтно = прежней",
                    read_bytes(fwd_path(s) / "files" / MANIFEST) == prior,
                    f"копия {read_bytes(fwd_path(s) / 'files' / MANIFEST)!r}")
        r = rollback(s)
        PROBE.check("D51", "откат: код 0, .layout-manifest побайтно прежняя",
                    r.rc == 0 and read_bytes(s.proj / MANIFEST) == prior,
                    f"опись {read_bytes(s.proj / MANIFEST)!r}; {r.brief()}")
    with Stand() as s:
        forward_ok(s, plan_b(s, {}, {NEW: NEW_TEXT}))
        r = rollback(s)
        m = read_manifest(s.proj / MANIFEST) or {"body": {}, "kept": {}}
        PROBE.check("D51", "без прежней описи: код 0, тело = план A, new.sh нет ни в теле, ни в «оставлено»",
                    r.rc == 0 and m["body"] == plan_rows(s) and NEW not in m["kept"]
                    and not os.path.lexists(s.proj / NEW), f"тело {m['body']}; {r.brief()}")


def case_d52() -> None:
    """Архив отката недоступен (journal.tsv архива отката → /dev/full): откат открывает
    архив ВСЕГДА (расхождение 7) — беда хода 1, код 3, «РАСКЛАДКА НЕ НАЧАТА», «No space
    left on device»; проект после прямого прогона не изменился (с inode). B только
    добавила new.sh: укладке A писать нечего, архив нужен одному снятию.
    Зелен на поломке? «архив только когда укладка пишет» — нет: откат прошёл бы кодом 0 без
    журнала; «снять без архива» — нет: снимок проекта с inode."""
    with Stand() as s:
        lay_a(s)
        forward_ok(s, plan_b(s, {}, {NEW: NEW_TEXT}))
        rb_path(s).mkdir(parents=True)
        os.symlink("/dev/full", rb_path(s) / "journal.tsv")
        before = snapshot(s.proj)
        r = rollback(s)
        PROBE.check("D52", "код 3, маркер первой строкой", r.rc == 3 and r.err.startswith(MARKER),
                    r.brief())
        PROBE.check("D52", "причина «No space left on device»", "No space left on device" in r.err,
                    r.brief())
        PROBE.check("D52", "проект не тронут (снимок с inode)", snapshot(s.proj) == before,
                    snapshot_diff(before, snapshot(s.proj)))


def case_d53() -> None:
    """Беды хода 1 отката: --rollback-of вне копии/раскладка → код 3 «архив прямого прогона
    вне каталога копий»; journal.tsv прямого — каталог → код 3 «журнал прямого прогона не
    читается»; проект в обоих не тронут.
    Зелен на поломке? «довод не судится» — нет: обычная раскладка дала бы код 0;
    «нечитаемый журнал = пустой» — нет: «снято 0» кодом 0 вместо отказа."""
    with Stand() as s:
        lay_a(s)
        forward_ok(s, plan_b(s, {}, {NEW: NEW_TEXT}))
        outside = s.t / "чужие" / FWD_NAME
        shutil.copytree(fwd_path(s), outside)
        before = snapshot(s.proj)
        r = rollback(s, of=outside)
        PROBE.check("D53", "вне каталога копий: код 3, «архив прямого прогона вне каталога копий»",
                    r.rc == 3 and "архив прямого прогона вне каталога копий" in r.err, r.brief())
        PROBE.check("D53", "вне каталога копий: проект не тронут", snapshot(s.proj) == before,
                    snapshot_diff(before, snapshot(s.proj)))
    with Stand() as s:
        lay_a(s)
        forward_ok(s, plan_b(s, {}, {NEW: NEW_TEXT}))
        (fwd_path(s) / "journal.tsv").unlink()
        (fwd_path(s) / "journal.tsv").mkdir()
        before = snapshot(s.proj)
        r = rollback(s)
        PROBE.check("D53", "журнал — каталог: код 3, «журнал прямого прогона не читается»",
                    r.rc == 3 and "журнал прямого прогона не читается" in r.err, r.brief())
        PROBE.check("D53", "журнал — каталог: проект не тронут", snapshot(s.proj) == before,
                    snapshot_diff(before, snapshot(s.proj)))


ORPHAN = "стенд-20261023-120000-42"
FOREIGN_ORPHAN = "стенд-x-20261023-120000-43"


def put_orphan(s: Stand, name: str) -> Path:
    archive = s.backups / "раскладка" / name
    archive.mkdir(parents=True)
    (archive / "journal.tsv").write_text(
        "# прогон 42 раскладка план=4 перекрыть=0\n"
        f"положен-новый\t-\t{'b' * 40}\tscripts/b.sh\n"
        f"положен-новый\t-\t{'b' * 40}\tscripts/c.sh\n", encoding="utf-8")
    return archive


def orphan_lines(r: Result) -> list:
    return [l for l in r.out.splitlines() if l.startswith("оборванная раскладка ")]


def case_d54() -> None:
    """Читатель оборванных: журнал «стенд-…» без итога (2 строки без «готово») и журнал
    «стенд-x-…» без итога → раскладка (код 0) называет только первый «… 2 строк без
    «готово»» и дописывает ему «разобран\\t-\\t-\\t<HARNESS_LAYOUT_ARCHIVE>»; вторая раскладка
    его не называет; раскладка с кодом 3 «разобран» не дописывает.
    Зелен на поломке? «маска имени» — нет: стенд-x назвался бы и получил строку;
    «разобран и при отказе» — нет: журнал при коде 3 судится побайтно."""
    with Stand() as s:
        mine = put_orphan(s, ORPHAN)
        foreign = put_orphan(s, FOREIGN_ORPHAN)
        foreign_was = read_bytes(foreign / "journal.tsv")
        own = s.backups / "раскладка" / "стенд-20261024-000100-5"
        s.foreign("scripts/deploy.sh")
        r = s.run(HARNESS_LAYOUT_ARCHIVE=own)
        named = orphan_lines(r)
        PROBE.check("D54", "код 0, названа ровно одна: «оборванная раскладка …-42: 2 строк без «готово»»",
                    r.rc == 0 and len(named) == 1 and ORPHAN + ":" in named[0]
                    and named[0].endswith("2 строк без «готово»"), f"строки {named}; {r.brief()}")
        lines = journal(mine)
        PROBE.check("D54", "оборванному дописано «разобран\\t-\\t-\\t<архив этого прогона>»",
                    bool(lines) and lines[-1].startswith("разобран\t-\t-\t")
                    and lines[-1].split("\t")[3] in (str(own), os.path.realpath(own)), f"журнал {lines}")
        PROBE.check("D54", "журнал стенд-x не тронут", read_bytes(foreign / "journal.tsv") == foreign_was,
                    f"{read_bytes(foreign / 'journal.tsv')!r}")
        r = s.run()
        PROBE.check("D54", "вторая раскладка: оборванных не названо", r.rc == 0 and orphan_lines(r) == [],
                    f"строки {orphan_lines(r)}; {r.brief()}")
    with Stand() as s:
        mine = put_orphan(s, ORPHAN)
        was = read_bytes(mine / "journal.tsv")
        (s.proj / "scripts/deploy.sh").unlink()
        (s.proj / "scripts/deploy.sh").mkdir()
        r = s.run()
        PROBE.check("D54", "пара: раскладка с кодом 3 «разобран» не дописала",
                    r.rc == 3 and read_bytes(mine / "journal.tsv") == was, f"{r.brief()}")


def case_d55() -> None:
    """Читатель в режиме отката: архив --rollback-of (без итога — прямой убит) НЕ назван, но в
    его журнал дописан «разобран\\t-\\t-\\t<архив отката>»; посторонний оборванный того же
    проекта назван и тоже разобран архивом отката.
    Зелен на поломке? «исключения нет» — нет: прямой назвался бы оборванным; «читатель
    только в обычном режиме» — нет: посторонний обязан назваться."""
    with Stand() as s:
        lay_a(s)
        s.foreign("scripts/deploy.sh")
        other = put_orphan(s, ORPHAN)
        forward(s, plan_b(s, {"scripts/deploy.sh": DEPLOY_B}, {NEW: NEW_TEXT}),
                HARNESS_LAYOUT_KILL_AFTER_WRITE="2")
        r = rollback(s)
        named = orphan_lines(r)
        PROBE.check("D55", "код 0; архив прямого не назван оборванным",
                    r.rc == 0 and not [l for l in named if FWD_NAME + ":" in l], f"строки {named}; {r.brief()}")
        PROBE.check("D55", "посторонний оборванный назван", [l for l in named if ORPHAN + ":" in l] != [],
                    f"строки {named}")
        rb_values = (str(rb_path(s)), os.path.realpath(rb_path(s)))
        for name, archive in (("прямой", fwd_path(s)), ("посторонний", other)):
            lines = journal(archive)
            PROBE.check("D55", f"{name}: последняя строка «разобран\\t-\\t-\\t<архив отката>»",
                        bool(lines) and lines[-1].startswith("разобран\t-\t-\t")
                        and lines[-1].split("\t")[3] in rb_values, f"журнал {lines[-2:]}")


def case_d56() -> None:
    """Откат без журнала прямого (прямой ничего не писал: каталога нет; каталог пуст) →
    код 0, «снято 0, возвращено 0, оставлено 0; строк без «готово» 0», журнал отката с
    шапкой «откат … по=<прямой>» есть, проект не тронут.
    Зелен на поломке? «нет журнала = беда» — нет: код 0 судится; «архив отката только при
    записях» — нет: шапка обязана быть (иначе update.py не отличит старый раскладчик)."""
    for label, make in (("каталога нет", False), ("каталог пуст", True)):
        with Stand() as s:
            lay_a(s)
            if make:
                fwd_path(s).mkdir(parents=True)
            before = without(snapshot(s.proj), MANIFEST)
            manifest_was = read_bytes(s.proj / MANIFEST)
            r = rollback(s)
            PROBE.check("D56", f"{label}: код 0, «снято 0, возвращено 0, оставлено 0; строк без «готово» 0»",
                        r.rc == 0 and counts(r) == (0, 0, 0, "", 0), r.brief())
            lines = journal(rb_path(s))
            PROBE.check("D56", f"{label}: журнал отката с шапкой «откат … по=<прямой>»",
                        bool(lines) and " откат " in lines[0] and lines[0].endswith(f"по={fwd_path(s)}"),
                        f"журнал {lines[:2]}")
            PROBE.check("D56", f"{label}: проект не тронут, опись побайтно",
                        without(snapshot(s.proj), MANIFEST) == before
                        and read_bytes(s.proj / MANIFEST) == manifest_was,
                        snapshot_diff(before, without(snapshot(s.proj), MANIFEST)))


def case_d57() -> None:
    """Остаток .new.sh.layout-tmp (обрыв B между write_temp и replace, Р16) у файла, которого
    нет в плане A → решение смены (в): назван строкой «остаток оборванной раскладки: …»,
    НЕ удалён, байты прежние. Пара: остаток у файла плана A (deploy.sh) перезаписан укладкой,
    как Ф-а.
    Зелен на поломке? «остатки только по плану» — нет: остаток new.sh назван не был бы;
    «остаток убран» — нет: байты судятся."""
    with Stand() as s:
        lay_a(s)
        forward_ok(s, plan_b(s, {"scripts/deploy.sh": DEPLOY_B}, {NEW: NEW_TEXT}))
        remnant = s.proj / "scripts/.new.sh.layout-tmp"
        remnant.write_bytes(b"\xd0\xbe\xd0\xb1\xd1\x80\xd1\x8b\xd0\xb2\xd0\xbe\xd0\xba\n")
        was = file_id(remnant)
        (s.proj / "scripts/.deploy.sh.layout-tmp").write_text("обрывок A\n", encoding="utf-8")
        r = rollback(s)
        named = [l for l in r.out.splitlines()
                 if "остаток оборванной раскладки" in l and "scripts/.new.sh.layout-tmp" in l]
        PROBE.check("D57", "код 0, остаток new.sh назван строкой «остаток оборванной раскладки: …»",
                    r.rc == 0 and len(named) == 1, f"строки {named}; {r.brief()}")
        PROBE.check("D57", "остаток new.sh не удалён и не убран: есть, blob, inode прежние; «убран» нет",
                    file_id(remnant) == was and not any("убран" in l for l in named),
                    f"{was} → {file_id(remnant)}")
        PROBE.check("D57", "пара: остаток deploy.sh перезаписан укладкой A, deploy.sh = A",
                    not os.path.lexists(s.proj / "scripts/.deploy.sh.layout-tmp")
                    and read_bytes(s.proj / "scripts/deploy.sh") == read_bytes(s.pkg / "scripts/deploy.sh"),
                    f"остатки {layout_remnants(s.proj)}")


def case_d58() -> None:
    """--rollback-of вместе с --lift-threshold (решение смены (и)) → код 3, маркер, проект не
    тронут, архива отката нет.
    Зелен на поломке? «довод порога в откате молча не действует» — нет: код 0."""
    with Stand() as s:
        lay_a(s)
        forward_ok(s, plan_b(s, {}, {NEW: NEW_TEXT}))
        before = snapshot(s.proj)
        r = rollback(s, args=("--lift-threshold", "проба"))
        PROBE.check("D58", "код 3, маркер первой строкой, --rollback-of назван",
                    r.rc == 3 and r.err.startswith(MARKER) and ROLLBACK_ARG in r.err, r.brief())
        PROBE.check("D58", "проект не тронут, архива отката нет",
                    snapshot(s.proj) == before and not rb_path(s).exists(),
                    f"{snapshot_diff(before, snapshot(s.proj))}; архив {rb_path(s).exists()}")


def case_d59() -> None:
    """Путь журнала прямого за проектом (Р21: журнал — подложимые данные): «положен-новый - B
    ../вне/x» и «перекрыт X B /etc/x» → код 3 «журнал прямого прогона не читается: … за
    проектом»; файлы вне проекта не тронуты (снимок стенда без каталога копий).
    Зелен на поломке? «пути журнала не судятся» — нет: ../вне/x с blob B ушёл бы в архив
    отката как «новый файл B»."""
    for label, kind, was in (("../вне/x", "положен-новый", "-"), ("/etc/x", "перекрыт", "1" * 40)):
        with Stand() as s:
            lay_a(s)
            outside = s.t / "вне" / "x"
            outside.parent.mkdir()
            outside.write_text("за проектом\n", encoding="utf-8")
            forge_forward(s, ["# прогон 1 обновление план=4 перекрыть=0",
                              f"{kind}\t{was}\t{blob_of(outside)}\t{label}",
                              f"готово\t-\t-\t{label}", "итог\t-\t-\t1"], {})
            before = {k: v for k, v in snapshot(s.t).items() if not k.startswith("копии")}
            r = rollback(s)
            PROBE.check("D59", f"{label}: код 3, «журнал прямого прогона не читается: … за проектом»",
                        r.rc == 3 and bool(lines_naming(r.err, "журнал прямого прогона не читается", "за проектом")),
                        r.brief())
            after = {k: v for k, v in snapshot(s.t).items() if not k.startswith("копии")}
            PROBE.check("D59", f"{label}: стенд вне каталога копий не тронут", after == before,
                        snapshot_diff(before, after))


def case_d60() -> None:
    """Три честности читателя и отката (ревью Ф-г, круг 1).
    (а) Обычный откат после ЗАКОНЧЕННОГО прямого прогона (в журнале «итог»): прямому «разобран»
    не дописывается и оборванным он не назван.
    (в) Журнал своего проекта без «итог», который не читается: названа строка «журнал не
    прочитан: <архив>: <причина>», журнал побайтно прежний (оборванным не объявлен).
    (е) Копия прежней описи не снята (в архиве уже лежит файл на её месте): прямой пишет
    «# опись не снята: …», откат говорит «копия описи до обновления не снята», а не «прежней
    описи в архиве нет».
    Зелен на поломке? «разобран всегда» — нет: (а) судит последнюю строку журнала; «нечитаемый =
    нет» — нет: (в) судит слова и байты; «одна фраза на оба случая» — нет: (е) судит обе."""
    with Stand() as s:
        lay_a(s)
        forward_ok(s, plan_b(s, {}, {NEW: NEW_TEXT}))
        r = rollback(s)
        lines = journal(fwd_path(s))
        PROBE.check("D60", "(а) закончен прямой: откат код 0, прямой не назван оборванным",
                    r.rc == 0 and not [l for l in orphan_lines(r) if FWD_NAME in l], r.brief())
        PROBE.check("D60", "(а) в журнале прямого нет «разобран»",
                    not [l for l in lines if l.startswith("разобран\t")], f"журнал {lines[-3:]}")
    with Stand() as s:
        bad = s.backups / "раскладка" / ORPHAN
        bad.mkdir(parents=True)
        text = "# прогон 42 раскладка план=4 перекрыть=0\nперекрыт\tне-blob\t-\tscripts/b.sh\n"
        (bad / "journal.tsv").write_text(text, encoding="utf-8")
        r = s.run()
        named = [l for l in r.out.splitlines() if l.startswith("журнал не прочитан: ")]
        PROBE.check("D60", "(в) битый журнал: код 0, названа строка «журнал не прочитан: <архив>: …»",
                    r.rc == 0 and len(named) == 1 and ORPHAN in named[0] and named[0].count(":") >= 2,
                    f"строки {named}; {r.brief()}")
        PROBE.check("D60", "(в) битый журнал: «оборванная раскладка» не сказано, «разобран» не дописан",
                    orphan_lines(r) == [] and read_bytes(bad / "journal.tsv") == text.encode("utf-8"),
                    f"{orphan_lines(r)}; {read_bytes(bad / 'journal.tsv')!r}")
    with Stand() as s:
        put_file(s, OLD, OLD_TEXT)
        put_prior(s, manifest_text(as_rows(plan_rows(s)) + [(blob_of_text(OLD_TEXT), "scripts", OLD)]))
        lay_a(s)
        squatter = fwd_path(s) / "files" / MANIFEST
        squatter.parent.mkdir(parents=True)
        squatter.write_text("занято\n", encoding="utf-8")
        forward_ok(s, plan_b(s, {}, {NEW: NEW_TEXT}))
        lines = journal(fwd_path(s))
        PROBE.check("D60", "(е) журнал прямого: «# опись не снята: …», строки «# опись до прогона» нет",
                    any(l.startswith("# опись не снята: ") for l in lines)
                    and not any(l.startswith("# опись до прогона") for l in lines), f"журнал {lines}")
        r = rollback(s)
        PROBE.check("D60", "(е) откат: «копия описи до обновления не снята», «в архиве нет» не сказано",
                    r.rc == 0 and "копия описи до обновления не снята" in r.out
                    and "прежней описи в архиве нет" not in r.out, r.brief())


# ══ Ф-д: затравка по истории пакета (--package-root <стенд-пакет с git>) ═══════
# История — git init ВНУТРИ стенда и коммиты plumbing'ом (рабочее дерево пакета не
# трогается, ни checkout, ни ветки рабочего дерева), авторство — окружением. LOG_DIR
# стенда — внутри стенда (и в install.conf, и в окружении). Вопрос и согласие стенд пишет
# сам в форме их живых писателей: update.py — журнал вопроса (решение (а1)), диспетчер —
# confirmations.jsonl (ts с поясом, text в нижнем регистре, message_id, kind).
# Ложный git в bin/ стенда пишет каждый зов в метку и передаёт настоящему: так судится
# «git не зван» (чистая установка), а пара доказывает, что метка видит зов.
# Согласие дверь берёт у НОСИТЕЛЯ слова (owner_word.носитель), не у LOG_DIR окружения:
# дверь идёт раннером CARRIER_RUNNER, который в её процессе ставит Носитель стенда.

SEED = "harness/ОПИСЬ.md"
SEED_TEXT = "# ОПИСЬ прежней версии харнеса\n"
REAL_GIT = shutil.which("git") or "/usr/bin/git"
PACKAGE_ROOT_ARG = "--package-root"
ASKED_LOG = "раскладка-спрошено.jsonl"
CONSENTS = "confirmations.jsonl"
OWNER_TZ = datetime.timezone(datetime.timedelta(hours=5))
SEED_DONE = "снято по истории пакета: "
SEED_MARK = "# по истории пакета: "
# Договор таблицы перевода (план Task 1, контракт interfaces.table). Стенд-пакет несёт её
# копию: дверь может брать таблицу и рядом с собой, и из корня пакета — стенд годен обоим.
def real_history_table() -> str:
    """Стенд кладёт НАСТОЯЩУЮ таблицу данных: копия в пробе отстала бы от неё молча.
    Ищется рядом (проект, пакет) или от рабочего каталога — прибор мутаций копирует лишь scripts/."""
    name = "layout-history-paths.yaml"
    here = Path(__file__).resolve().parent
    for folder in (here.parent / "harness" / "config", here.parent / "config",
                   Path.cwd() / "harness" / "config"):
        if (folder / name).is_file():
            return (folder / name).read_text(encoding="utf-8")
    raise FileNotFoundError(f"нет таблицы перевода {name} рядом с пробой")


HISTORY_TABLE = real_history_table()


def git_env(index: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="стенд", GIT_AUTHOR_EMAIL="stand@example.invalid",
               GIT_COMMITTER_NAME="стенд", GIT_COMMITTER_EMAIL="stand@example.invalid",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    return env


def git(root: Path, *args, data: bytes | None = None, index: Path | None = None) -> str:
    done = subprocess.run([REAL_GIT, "-C", str(root), *args], input=data, capture_output=True,
                          env=git_env(index), check=False)
    if done.returncode != 0:
        raise RuntimeError(f"стенд: git {args[0]} отказал: "
                           f"{done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout.decode("utf-8").strip()


def git_commit(root: Path, ref: str, files: dict, parent: str = "") -> str:
    """Коммит ровно с files {путь пакета: текст} на ref; рабочее дерево не трогается."""
    index = root / ".git" / "стенд-индекс"
    index.unlink(missing_ok=True)
    rows = "".join(f"100644 {git(root, 'hash-object', '-w', '--stdin', data=text.encode('utf-8'))}"
                   f"\t{path}\n" for path, text in files.items())
    git(root, "update-index", "--index-info", data=rows.encode("utf-8"), index=index)
    tree = git(root, "write-tree", index=index)
    commit = git(root, "commit-tree", tree, *(("-p", parent) if parent else ()),
                 "-m", f"стенд {ref}")
    git(root, "update-ref", f"refs/heads/{ref}", commit)
    return commit


def package_files(s: Stand) -> dict:
    """Пакет A стенда путями ПАКЕТА: scripts/ проекта живут в harness/scripts/ пакета."""
    files = {"harness/config/layout-history-paths.yaml": HISTORY_TABLE}
    for _, rel, content, _ in FILES:
        files[rel.replace("scripts/", "harness/scripts/", 1) if rel.startswith("scripts/")
              else rel] = content
    return files


def seed_stand(s: Stand, branch: dict, old: dict | None = None) -> None:
    """Стенд Ф-д: дверь уложила A (опись есть); в s.pkg git: master = A поверх коммита-предка
    с branch {путь пакета: текст} (история пакета: достижима из HEAD) и old — прежнего
    коммита (история «старого кода»). LOG_DIR стенда, ложный git с меткой."""
    lay_a(s)
    table = s.pkg / "harness" / "config" / "layout-history-paths.yaml"
    table.parent.mkdir(parents=True, exist_ok=True)
    table.write_text(HISTORY_TABLE, encoding="utf-8")
    subprocess.run([REAL_GIT, "init", "-q", "-b", "master", str(s.pkg)], env=git_env(),
                   capture_output=True, check=True)
    base = git_commit(s.pkg, "прежний", {**package_files(s), **old}) if old else ""
    if branch:
        base = git_commit(s.pkg, "ранее", {**package_files(s), **branch}, base)
    git_commit(s.pkg, "master", package_files(s), base)
    git(s.pkg, "symbolic-ref", "HEAD", "refs/heads/master")
    point_log_dir(s, s.t / "журналы")
    s.git_mark = s.t / "git-зовы"
    fake = s.bin / "git"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{s.git_mark}'\n"
                    f"exec '{REAL_GIT}' \"$@\"\n", encoding="utf-8")
    os.chmod(fake, 0o755)


def point_log_dir(s: Stand, log_dir: Path) -> None:
    """LOG_DIR стенда: строкой install.conf и окружением, одним значением."""
    log_dir.mkdir(parents=True, exist_ok=True)
    s.log_dir = log_dir
    s.install_conf.write_text(f'PROJECT_NAME="стенд"\nPROJECT_DIR="{s.proj}"\n'
                              f'LOG_DIR="{log_dir}"\n', encoding="utf-8")


def empty_project(s: Stand) -> None:
    """Чистая установка после seed_stand: проект и каталог копий пусты."""
    for path in s.proj.iterdir():
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    shutil.rmtree(s.backups, ignore_errors=True)


def git_calls(s: Stand) -> list:
    raw = read_bytes(s.git_mark)
    return raw.decode("utf-8", "replace").splitlines() if raw else []


# Раннер двери: судья слова получает носитель стенда подменой owner_word.носитель в процессе
# двери. lib — рядом с дверью: тот же модуль owner_word, что импортирует layout_history двери.
CARRIER_RUNNER = r"""
import json, pathlib, runpy, sys
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["lib"])
import owner_word
carrier = owner_word.Носитель(папка=pathlib.Path(spec["папка"]), uid=None, паспорт={},
                              граница=pathlib.Path(spec["граница"]), окно_мин=60,
                              срок_кнопки_мин=15, ручки={})
owner_word.носитель = lambda: carrier
sys.argv = spec["argv"]
runpy.run_path(spec["argv"][0], run_name="__main__")
"""


def seed_run(s: Stand, root=None, carrier: Path | None = None, **env) -> Result:
    """Дверь с --package-root; carrier — папка носителя слова (по умолчанию LOG_DIR стенда)."""
    target = s.pkg if root is None else root
    log_dir = getattr(s, "log_dir", s.t / "журналы")
    s.write_plan()
    spec = {"lib": str(DOOR.parent / "lib"), "папка": str(carrier or log_dir), "граница": str(s.t),
            "argv": [str(DOOR), *s.door_args(), PACKAGE_ROOT_ARG, str(target)]}
    return execute([sys.executable, "-c", CARRIER_RUNNER, json.dumps(spec, ensure_ascii=False)],
                   s.env(LOG_DIR=log_dir, **env))


def owner_time(minutes_ago: int) -> str:
    moment = datetime.datetime.now(OWNER_TZ) - datetime.timedelta(minutes=minutes_ago)
    return moment.isoformat(timespec="seconds")


def ask(s: Stand, paths: list, minutes_ago: int = 60) -> None:
    """Строка журнала вопроса — как пишет update.py (решение (а1))."""
    line = {"ts": owner_time(minutes_ago), "пути": paths,
            "вопрос": f"На сервере {len(paths)} файл от прежних версий харнеса: "
                      f"{', '.join(paths)}. Убрать в архив? Ответь ровно «да, убрать»."}
    with open(s.log_dir / ASKED_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def agree(s: Stand, minutes_ago: int = 30, text: str = "да, убрать", extra: str = "") -> None:
    """Запись диспетчера в confirmations.jsonl (zapisat_soglasiya); extra — сырая строка после."""
    line = {"ts": owner_time(minutes_ago), "text": text, "message_id": "9001", "kind": "any"}
    with open(s.log_dir / CONSENTS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n" + extra)


def put_seed(s: Stand, rel: str = SEED, text: str = SEED_TEXT) -> str:
    return put_file(s, rel, text)


def seed_named(r: Result) -> list:
    return [line for line in r.out.splitlines() if line.startswith("по истории пакета снял бы ")]


def seed_done(r: Result) -> list:
    return [line for line in r.out.splitlines() if line.startswith(SEED_DONE)]


def case_ds1() -> None:
    """Чистая установка: проект пуст → код 0, строк затравки нет, каталога копий нет, git не
    зван. Вторая половина — --package-root на несуществующий каталог: тоже код 0 и без
    «затравки нет» (кандидатов нет — истории не спрашивают). Пара — тот же стенд с
    неизвестным файлом: метка зова git НЕ пуста (прибор видит зов, иначе «не зван» зелен
    даром).
    Зелен на поломке? «git зовётся всегда» — нет: метка судится; «метка не пишется вовсе»
    — нет: пара обязана её увидеть. До кода Ф-д первые две половины зелены по построению
    (дверь довод не знает) — красна только пара."""
    with Stand() as s:
        seed_stand(s, {SEED: SEED_TEXT})
        empty_project(s)
        r = seed_run(s)
        PROBE.check("DS1", "пустой проект: код 0, «по истории» и «затравки нет» нет",
                    r.rc == 0 and "по истории" not in r.both and "затравки нет" not in r.both,
                    r.brief())
        PROBE.check("DS1", "пустой проект: каталога копий нет, git не зван",
                    not s.backups.exists() and git_calls(s) == [],
                    f"копии {s.backups.exists()}; зовы {git_calls(s)}")
    with Stand() as s:
        seed_stand(s, {SEED: SEED_TEXT})
        empty_project(s)
        r = seed_run(s, root=s.t / "нет-такого")
        PROBE.check("DS1", "несуществующий корень пакета, пустой проект: код 0, «затравки нет» нет, "
                    "git не зван", r.rc == 0 and "затравки нет" not in r.both and git_calls(s) == [],
                    f"зовы {git_calls(s)}; {r.brief()}")
    with Stand() as s:
        seed_stand(s, {SEED: SEED_TEXT})
        put_seed(s)
        r = seed_run(s)
        PROBE.check("DS1", "пара: неизвестный файл — git зван (метка видит зов), код 0",
                    r.rc == 0 and git_calls(s) != [], f"зовы {git_calls(s)}; {r.brief()}")


def case_ds2() -> None:
    """Неизвестный наш файл без вопроса (с описью и без — первая опись, как у нас вживую) →
    «по истории пакета снял бы 1: harness/ОПИСЬ.md; ждёт «да, убрать»», файл на месте,
    проект кроме описи не тронут, в архиве ничего.
    Зелен на поломке? «согласие не судится — снимает сразу» — нет: файл судится есть/blob/
    inode/режим; «затравка не считается» — нет: строка дословно."""
    for label, first in (("опись есть", False), ("первая опись", True)):
        with Stand() as s:
            seed_stand(s, {SEED: SEED_TEXT})
            if first:
                (s.proj / MANIFEST).unlink()
            put_seed(s)
            seed_before = file_id(s.proj / SEED)
            before = without(snapshot(s.proj), MANIFEST)
            r = seed_run(s)
            PROBE.check("DS2", f"{label}: код 0, «по истории пакета снял бы 1: {SEED}; ждёт «да, убрать»»",
                        r.rc == 0 and f"по истории пакета снял бы 1: {SEED}; ждёт «да, убрать»" in r.out,
                        r.brief())
            PROBE.check("DS2", f"{label}: {SEED} на месте (есть, blob, inode, режим)",
                        file_id(s.proj / SEED) == seed_before, f"{seed_before} → {file_id(s.proj / SEED)}")
            after = without(snapshot(s.proj), MANIFEST)
            PROBE.check("DS2", f"{label}: проект кроме описи не тронут, в архиве ничего",
                        after == before and not archived(s),
                        f"{snapshot_diff(before, after)}; архив {archived(s)}")


def case_ds3() -> None:
    """Вопрос назвал harness/ОПИСЬ.md, «да, убрать» после него → «снято по истории пакета:
    harness/ОПИСЬ.md», файла нет, files/ архива побайтно X, журнал «# по истории пакета:
    <путь>» сразу перед «снят X - <путь>», затем «готово»; опись его не содержит; сводка
    описи «снято 0» (снятое по истории в счёт описи не идёт, Р9).
    Зелен на поломке? «снял, но без архива» — нет: blob в files/; «новое слово действия
    вместо пометки» — нет: строка «снят» и пометка судятся дословно; «сводка описи считает
    затравку» — нет: «снято 0»."""
    with Stand() as s:
        seed_stand(s, {SEED: SEED_TEXT})
        x_blob = put_seed(s)
        ask(s, [SEED])
        agree(s)
        r = seed_run(s)
        PROBE.check("DS3", f"код 0, строка «{SEED_DONE}{SEED}»",
                    r.rc == 0 and seed_done(r) == [SEED_DONE + SEED], r.brief())
        PROBE.check("DS3", f"{SEED} в проекте нет", not os.path.lexists(s.proj / SEED),
                    f"{file_id(s.proj / SEED)}")
        PROBE.check("DS3", f"files/{SEED} архива — blob X", archived(s).get(SEED) == x_blob,
                    f"архив {archived(s)}, ждали {x_blob}")
        lines = all_journal_lines(s)
        removed = f"снят\t{x_blob}\t-\t{SEED}"
        mark = SEED_MARK + SEED
        at = lines.index(removed) if removed in lines else -1
        PROBE.check("DS3", "журнал: «# по истории пакета: <путь>» сразу перед «снят X - <путь>», "
                    "затем «готово»", at > 0 and lines[at - 1] == mark
                    and f"готово\t-\t-\t{SEED}" in lines[at:], f"журнал {lines}")
        m = read_manifest(s.proj / MANIFEST) or {"body": {SEED: 0}, "kept": {}}
        PROBE.check("DS3", "опись: пути нет ни в теле, ни в «оставлено»",
                    SEED not in m["body"] and SEED not in m["kept"], f"тело {sorted(m['body'])}")
        PROBE.check("DS3", "сводка описи «опись: в описи 3; снято 0;» (затравка — своей строкой)",
                    summary_of(r).startswith("опись: в описи 3; снято 0;"), f"сводка {summary_of(r)!r}")


def case_ds4() -> None:
    """То же согласие, BACKUP_DIR внутри обычного файла → код 3 «РАСКЛАДКА НЕ НАЧАТА»,
    проект не тронут (снимок с inode): архив снятия по затравке — беда хода 1, как у Ф-в.
    Зелен на поломке? «снятие пишет без плана архива» — нет: код 0/1 вместо 3."""
    with Stand() as s:
        seed_stand(s, {SEED: SEED_TEXT})
        put_seed(s)
        ask(s, [SEED])
        agree(s)
        (s.t / "файл").write_text("обычный файл\n", encoding="utf-8")
        s.set_backup_dir(f"{s.t}/файл/копии")
        before = snapshot(s.proj)
        r = seed_run(s)
        PROBE.check("DS4", "код 3, маркер первой строкой",
                    r.rc == 3 and r.err.startswith(MARKER), r.brief())
        PROBE.check("DS4", "проект не тронут (снимок с inode)", snapshot(s.proj) == before,
                    snapshot_diff(before, snapshot(s.proj)))


def case_ds5() -> None:
    """«Да, убрать» есть, но вопрос назвал другой путь (scripts/x.sh) → ОПИСЬ.md только
    назван («снял бы 1»), файл на месте, «снято по истории» нет. Пара — DS3.
    Зелен на поломке? «согласие действует на любой путь» — нет: файл судится file_id."""
    with Stand() as s:
        seed_stand(s, {SEED: SEED_TEXT})
        put_seed(s)
        seed_before = file_id(s.proj / SEED)
        ask(s, ["scripts/x.sh"])
        agree(s)
        r = seed_run(s)
        PROBE.check("DS5", f"код 0, «по истории пакета снял бы 1: {SEED}», «{SEED_DONE}» нет",
                    r.rc == 0 and seed_named(r)[:1] == [f"по истории пакета снял бы 1: {SEED}; ждёт «да, убрать»"]
                    and seed_done(r) == [], r.brief())
        PROBE.check("DS5", f"{SEED} на месте, в архиве нет",
                    file_id(s.proj / SEED) == seed_before and SEED not in archived(s),
                    f"{file_id(s.proj / SEED)}; архив {archived(s)}")


def case_ds6() -> None:
    """Порог: опись = план (снимать по описи нечего), LAYOUT_REMOVE_MAX_PERCENT="1", три
    поимённо согласованных неизвестных → сняты все три (его «да, убрать» на названный
    список порогом не судится, расхождение 10), «порог» в выводе нет.
    Зелен на поломке? «поимённое судится порогом» — нет: 3 из 6 при 1 % отложились бы."""
    trio = {SEED: SEED_TEXT, "harness/СТАРОЕ.md": "# старая заметка\n",
            "scripts/old-tool.sh": "#!/bin/sh\necho старый инструмент\n"}
    history = {("harness/scripts/old-tool.sh" if rel.startswith("scripts/") else rel): text
               for rel, text in trio.items()}
    with Stand() as s:
        seed_stand(s, history)
        s.set_limit("1")
        blobs = {rel: put_seed(s, rel, text) for rel, text in trio.items()}
        ask(s, sorted(trio))
        agree(s)
        r = seed_run(s)
        PROBE.check("DS6", "код 0, все три в проекте сняты",
                    r.rc == 0 and not any(os.path.lexists(s.proj / rel) for rel in trio), r.brief())
        PROBE.check("DS6", "все три в files/ архива с прежним blob",
                    all(archived(s).get(rel) == b for rel, b in blobs.items()),
                    f"архив {archived(s)}")
        done = seed_done(r)
        PROBE.check("DS6", "одна строка «снято по истории пакета: …» с тремя путями, «порог» нет",
                    len(done) == 1 and all(rel in done[0] for rel in trio) and "порог" not in r.out,
                    r.brief())
        lines = all_journal_lines(s)
        PROBE.check("DS6", "журнал: у каждого «снят» своя пометка «# по истории пакета:» выше",
                    all(removed_lines(lines, rel) and SEED_MARK + rel in lines
                        and lines.index(SEED_MARK + rel) < lines.index(removed_lines(lines, rel)[0])
                        for rel in trio), f"журнал {lines}")


def case_ds7() -> None:
    """Нечитаемая опись («# layout-manifest 1») + неизвестный наш файл + вопрос и согласие →
    «затравки нет: опись нечитаема», файл на месте (иначе все прежние строки описи стали бы
    «неизвестными»).
    Зелен на поломке? «затравка при нечитаемой описи» — нет: файл судится file_id."""
    with Stand() as s:
        seed_stand(s, {SEED: SEED_TEXT})
        put_prior(s, "# layout-manifest 1\nстарый формат\n")
        put_seed(s)
        seed_before = file_id(s.proj / SEED)
        ask(s, [SEED])
        agree(s)
        r = seed_run(s)
        PROBE.check("DS7", "код 0, «затравки нет: опись нечитаема»",
                    r.rc == 0 and "затравки нет: опись нечитаема" in r.out, r.brief())
        PROBE.check("DS7", f"{SEED} на месте, «{SEED_DONE}» нет, в архиве files/{SEED} нет",
                    file_id(s.proj / SEED) == seed_before and seed_done(r) == []
                    and SEED not in archived(s), f"{file_id(s.proj / SEED)}; архив {archived(s)}")


def case_ds8() -> None:
    """confirmations.jsonl: верное «да, убрать» после вопроса И следом битая строка →
    «согласие не прочитано (…) — по истории не снимаю», файл на месте, код 0.
    Зелен на поломке? «битая строка пропущена, верная засчитана» — нет: файл бы снялся;
    «Unreadable = согласие» — нет: то же."""
    with Stand() as s:
        seed_stand(s, {SEED: SEED_TEXT})
        put_seed(s)
        seed_before = file_id(s.proj / SEED)
        ask(s, [SEED])
        agree(s, extra='{"ts": "битая строка\n')
        r = seed_run(s)
        named = [l for l in r.out.splitlines() if l.startswith("согласие не прочитано (")]
        PROBE.check("DS8", "код 0, «согласие не прочитано (…) — по истории не снимаю»",
                    r.rc == 0 and len(named) == 1 and named[0].endswith("— по истории не снимаю"),
                    f"строки {named}; {r.brief()}")
        PROBE.check("DS8", f"{SEED} на месте, «{SEED_DONE}» нет",
                    file_id(s.proj / SEED) == seed_before and seed_done(r) == [],
                    f"{file_id(s.proj / SEED)}; {r.brief()}")


def case_ds9() -> None:
    """Устаревшая опись (Р8): опись несёт scripts/f.sh с blob Fb, на диске Fa — blob ПРЕЖНЕГО
    коммита стенд-пакета (harness/scripts/f.sh), в плане f.sh нет, ключ порога 100 →
    «снято по истории пакета: scripts/f.sh», журнал «снят <Fa> - scripts/f.sh» (blob ДИСКА)
    с пометкой выше, в files/ Fa. Пара — на диске Fc, которого в истории нет → «оставлено:
    правлен здесь», файл на месте.
    Зелен на поломке? «row = blob описи» — нет: строка журнала судится с Fa; «правлен здесь
    по истории не судится» — нет: файл обязан уйти; «наш = известен путь» — нет: пара Fc."""
    f_rel, f_pkg = "scripts/f.sh", "harness/scripts/f.sh"
    fa, fb, fc = "#!/bin/sh\necho f старого кода\n", "#!/bin/sh\necho f версии B\n", \
        "#!/bin/sh\necho f правлен владельцем\n"
    for label, disk, gone in (("Fa из истории", fa, True), ("пара: Fc вне истории", fc, False)):
        with Stand() as s:
            seed_stand(s, {}, old={f_pkg: fa})
            s.set_limit("100")
            fb_blob = blob_of_text(fb)
            put_prior(s, manifest_text(as_rows(plan_rows(s)) + [(fb_blob, "scripts", f_rel)]))
            disk_blob = put_file(s, f_rel, disk)
            disk_before = file_id(s.proj / f_rel)
            r = seed_run(s)
            lines = all_journal_lines(s)
            if gone:
                PROBE.check("DS9", f"{label}: код 0, «{SEED_DONE}{f_rel}», файла нет",
                            r.rc == 0 and seed_done(r) == [SEED_DONE + f_rel]
                            and not os.path.lexists(s.proj / f_rel), r.brief())
                PROBE.check("DS9", f"{label}: журнал «снят <Fa> - {f_rel}» (blob диска, не описи)",
                            f"снят\t{disk_blob}\t-\t{f_rel}" in lines, f"журнал {lines}")
                PROBE.check("DS9", f"{label}: пометка «{SEED_MARK}{f_rel}» перед «снят»",
                            SEED_MARK + f_rel in lines and f"снят\t{disk_blob}\t-\t{f_rel}" in lines
                            and lines.index(SEED_MARK + f_rel)
                            < lines.index(f"снят\t{disk_blob}\t-\t{f_rel}"), f"журнал {lines}")
                PROBE.check("DS9", f"{label}: files/{f_rel} — Fa", archived(s).get(f_rel) == disk_blob,
                            f"архив {archived(s)}")
            else:
                kept = (read_manifest(s.proj / MANIFEST) or {"kept": {}})["kept"]
                PROBE.check("DS9", f"{label}: код 0, файл на месте, «{SEED_DONE}» нет",
                            r.rc == 0 and file_id(s.proj / f_rel) == disk_before and seed_done(r) == [],
                            f"{file_id(s.proj / f_rel)}; {r.brief()}")
                PROBE.check("DS9", f"{label}: опись — «оставлено» {f_rel} «правлен здесь»",
                            kept.get(f_rel, ("", ""))[1] == "правлен здесь", f"оставлено {kept}")


def case_ds10() -> None:
    """Самотест, LOG_DIR вне стенда → «согласие не прочитано (самотест целится в живой
    LOG_DIR)», файл на месте. (а) живой /var/log/harness — только чтение, проба судит строку;
    (б) каталог вне стенда с вопросом и согласием — без стража файл снялся бы.
    Зелен на поломке? «страж снят» — нет: (б) судит файл, (а) и (б) — строку."""
    outside = Path(tempfile.mkdtemp(prefix="layout-door-probe-log-"))
    try:
        for label, log_dir, prime in (("(а) живой /var/log/harness", Path("/var/log/harness"), False),
                                      ("(б) каталог вне стенда", outside, True)):
            with Stand() as s:
                seed_stand(s, {SEED: SEED_TEXT})
                put_seed(s)
                seed_before = file_id(s.proj / SEED)
                if prime:
                    point_log_dir(s, log_dir)
                    ask(s, [SEED])
                    agree(s)
                else:
                    s.log_dir = log_dir
                    s.install_conf.write_text(f'PROJECT_NAME="стенд"\nPROJECT_DIR="{s.proj}"\n'
                                              f'LOG_DIR="{log_dir}"\n', encoding="utf-8")
                r = seed_run(s)
                PROBE.check("DS10", f"{label}: код 0, «согласие не прочитано (самотест целится в живой "
                            "LOG_DIR)»", r.rc == 0
                            and "согласие не прочитано (самотест целится в живой LOG_DIR)" in r.out,
                            r.brief())
                PROBE.check("DS10", f"{label}: {SEED} на месте, «{SEED_DONE}» нет",
                            file_id(s.proj / SEED) == seed_before and seed_done(r) == [],
                            f"{file_id(s.proj / SEED)}; {r.brief()}")
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def case_ds11() -> None:
    """Окно согласия — от вопроса до ответа, не от «сейчас» (FD-02): вопрос 10 суток назад,
    «да, убрать» через сутки после него (9 суток назад) → файл снят. Пара: вопрос 20 суток
    назад, ответ через 9 суток после него (11 суток назад) → не годно, файл на месте.
    Строка «список затравки: [...]» несёт пути списком (FD-08).
    Зелен на поломке? «окно от сейчас» — нет: первая половина обязана снять; «окна нет» —
    нет: вторая обязана оставить."""
    day = 24 * 60
    with Stand() as s:
        seed_stand(s, {SEED: SEED_TEXT})
        put_seed(s)
        ask(s, [SEED], minutes_ago=10 * day)
        agree(s, minutes_ago=9 * day)
        r = seed_run(s)
        PROBE.check("DS11", f"вопрос 10 суток назад, ответ через сутки: код 0, «{SEED_DONE}{SEED}»",
                    r.rc == 0 and seed_done(r) == [SEED_DONE + SEED], r.brief())
    with Stand() as s:
        seed_stand(s, {SEED: SEED_TEXT})
        put_seed(s)
        before = file_id(s.proj / SEED)
        ask(s, [SEED], minutes_ago=20 * day)
        agree(s, minutes_ago=11 * day)
        r = seed_run(s)
        PROBE.check("DS11", "вопрос 20 суток назад, ответ через 9 суток: не снято, файл на месте",
                    r.rc == 0 and seed_done(r) == [] and file_id(s.proj / SEED) == before, r.brief())
    with Stand() as s:
        seed_stand(s, {SEED: SEED_TEXT})
        put_seed(s)
        r = seed_run(s)
        PROBE.check("DS11", "без вопроса: машинная строка «список затравки» несёт путь списком JSON",
                    f'список затравки: ["{SEED}"]' in r.out.splitlines(), r.brief())


def case_ds12() -> None:
    """Слово — у носителя, а не в LOG_DIR двери: вопрос и «да, убрать» в LOG_DIR стенда,
    носитель слова — другой каталог стенда с пустым журналом согласий → «да, убрать» после
    него нет, файл на месте. Пара — тот же стенд, носитель = LOG_DIR → снято (как DS3).
    Зелен на поломке? «дверь читает согласие из config.log_dir()» — нет: первая половина
    сняла бы файл; «носитель не читается вовсе» — нет: пара обязана снять."""
    with Stand() as s:
        seed_stand(s, {SEED: SEED_TEXT})
        put_seed(s)
        before = file_id(s.proj / SEED)
        ask(s, [SEED])
        agree(s)
        other = s.t / "носитель"
        other.mkdir()
        (other / CONSENTS).write_text("", encoding="utf-8")
        r = seed_run(s, carrier=other)
        PROBE.check("DS12", "согласие в LOG_DIR, носитель другой: код 0, ««да, убрать» после него "
                    "нет», файл на месте", r.rc == 0 and "«да, убрать» после него нет" in r.out
                    and seed_done(r) == [] and file_id(s.proj / SEED) == before, r.brief())
    with Stand() as s:
        seed_stand(s, {SEED: SEED_TEXT})
        put_seed(s)
        ask(s, [SEED])
        agree(s)
        r = seed_run(s)
        PROBE.check("DS12", f"пара: носитель = LOG_DIR стенда → «{SEED_DONE}{SEED}», файла нет",
                    r.rc == 0 and seed_done(r) == [SEED_DONE + SEED]
                    and not os.path.lexists(s.proj / SEED), r.brief())

# Дверь в отдельном python с потолком памяти: дефект «читать /dev/zero до конца» падает
# MemoryError за секунды, а не съедает машину (обрыв сессии 24.10, 42 OOM). Пиковую память
# двери отдаёт RUSAGE_CHILDREN этой обёртки — у неё ровно один ребёнок.
MEASURED_RUN = r"""
import json, resource, subprocess, sys, time
ceiling, timeout = int(sys.argv[1]), float(sys.argv[2])
def limit():
    resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
start = time.monotonic()
try:
    done = subprocess.run(sys.argv[3:], capture_output=True, text=True, timeout=timeout,
                          preexec_fn=limit)
    rc, out, err = done.returncode, done.stdout, done.stderr
except subprocess.TimeoutExpired as late:
    rc, out, err = None, str(late.stdout or ""), str(late.stderr or "")
print(json.dumps({"rc": rc, "out": out, "err": err, "seconds": time.monotonic() - start,
                  "maxrss_kb": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss}))
"""
GARBAGE_AS_CEILING = 1_500_000_000
GARBAGE_RSS_KB = 200_000
GARBAGE_SECONDS = 30


def measured_rollback(s: Stand) -> dict:
    s.write_plan()
    argv = [sys.executable, str(DOOR), *s.door_args(), ROLLBACK_ARG, str(fwd_path(s))]
    done = subprocess.run([sys.executable, "-c", MEASURED_RUN, str(GARBAGE_AS_CEILING),
                           str(GARBAGE_SECONDS), *argv],
                          capture_output=True, text=True, timeout=GARBAGE_SECONDS + 60,
                          env=s.env(HARNESS_LAYOUT_ARCHIVE=rb_path(s)))
    return json.loads(done.stdout.strip().splitlines()[-1])


def case_d61() -> None:
    """Копия описи в архиве прямого прогона — не обычный файл (ссылка на /dev/zero, FIFO без
    писателя): откат отвечает за секунды, память двери < 200 МБ, без трассы питона, и опись
    «к состоянию до обновления» не объявляет — копия не прочитана. Пара — D51 (обычная копия
    возвращается побайтно).
    Зелен на поломке? «предел чтения без проверки типа» — нет: FIFO без писателя повиснет до
    таймаута; «проверка типа без предела» — /dev/zero её не пройдёт, но FIFO тоже; «чтение
    до конца» — нет: под потолком 1,5 ГБ дверь падает MemoryError с трассой."""
    for name, plant in (("/dev/zero", lambda p: p.symlink_to("/dev/zero")),
                        ("FIFO", lambda p: os.mkfifo(p))):
        with Stand() as s:
            put_file(s, OLD, OLD_TEXT)
            put_prior(s, manifest_text(as_rows(plan_rows(s)) + [(blob_of_text(OLD_TEXT), "scripts", OLD)]))
            lay_a(s)
            forward_ok(s, plan_b(s, {}, {NEW: NEW_TEXT}))
            copy = fwd_path(s) / "files" / MANIFEST
            PROBE.check("D61", f"стенд ({name}): прямой прогон снял копию описи", copy.is_file(),
                        f"копии нет: {copy}")
            copy.unlink()
            plant(copy)
            run = measured_rollback(s)
            both = run["out"] + run["err"]
            PROBE.check("D61", f"копия описи — {name}: откат завершился за {GARBAGE_SECONDS} с",
                        run["rc"] is not None, f"таймаут; {run['seconds']:.1f} с")
            PROBE.check("D61", f"копия описи — {name}: память двери < {GARBAGE_RSS_KB // 1000} МБ",
                        run["maxrss_kb"] < GARBAGE_RSS_KB, f"пик {run['maxrss_kb'] // 1000} МБ")
            PROBE.check("D61", f"копия описи — {name}: без трассы питона",
                        "Traceback" not in both, run["err"][-300:])
            PROBE.check("D61", f"копия описи — {name}: «опись возвращена к состоянию до обновления» не сказано",
                        "опись возвращена к состоянию до обновления" not in both, both[-300:])
    case_d61_oversized()


def journal_limit() -> int:
    spec = importlib.util.spec_from_file_location("layout_journal_probe",
                                                  DOOR.parent / "lib" / "layout_journal.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.JOURNAL_LIMIT


def sparse_blob(size: int) -> str:
    """blob git файла из `size` нулей — потоком, без файла в памяти."""
    digest = hashlib.sha1(b"blob %d\0" % size)
    chunk = b"\0" * (1 << 20)
    left = size
    while left:
        digest.update(chunk[:min(left, len(chunk))])
        left -= min(left, len(chunk))
    return digest.hexdigest()


def case_d61_oversized() -> None:
    """Копия описи — ОБЫЧНЫЙ файл больше JOURNAL_LIMIT (разреженный, на диске пусто), и журнал
    прямого называет ровно его blob: blob сойдётся, судит один предел размера. Откат за секунды,
    опись «к состоянию до обновления» не объявлена, в проекте опись не стала файлом нулей, и
    память двери не выше здорового отката той же стойки + четверть предела: размер судится до
    чтения, а не прочитанным.
    Пара — здоровый откат той же стойки (копия цела): его пик — мерка памяти.
    Зелен на поломке? «снять проверку длины» — нет: blob совпал, опись из нулей ляжет в проект
    и будет объявлена; «читать предел+1 и судить прочитанное» — нет: пик выше мерки на ~64 МБ."""
    limit = journal_limit()
    runs = {}
    for name in ("здоровая", "больше предела"):
        with Stand() as s:
            put_file(s, OLD, OLD_TEXT)
            put_prior(s, manifest_text(as_rows(plan_rows(s)) + [(blob_of_text(OLD_TEXT), "scripts", OLD)]))
            lay_a(s)
            forward_ok(s, plan_b(s, {}, {NEW: NEW_TEXT}))
            copy = fwd_path(s) / "files" / MANIFEST
            if name == "больше предела":
                copy.unlink()
                with open(copy, "wb"):
                    pass
                os.truncate(copy, limit + 1)
                tsv = fwd_path(s) / "journal.tsv"
                lines = tsv.read_text(encoding="utf-8").splitlines(keepends=True)
                prefix = f"# опись до прогона: files/{MANIFEST} "
                marked = [i for i, line in enumerate(lines) if line.startswith(prefix)]
                PROBE.check("D61", "стенд (больше предела): журнал прямого называет копию описи",
                            len(marked) == 1, f"строк «опись до прогона»: {len(marked)}")
                for i in marked:
                    lines[i] = prefix + sparse_blob(limit + 1) + "\n"
                tsv.write_text("".join(lines), encoding="utf-8")
            runs[name] = (measured_rollback(s), read_bytes(s.proj / MANIFEST) or b"")
    healthy, _ = runs["здоровая"]
    run, manifest = runs["больше предела"]
    both = run["out"] + run["err"]
    PROBE.check("D61", "пара: здоровый откат той же стойки — код 0, опись возвращена",
                healthy["rc"] == 0 and "опись возвращена к состоянию до обновления"
                in healthy["out"] + healthy["err"], (healthy["out"] + healthy["err"])[-300:])
    PROBE.check("D61", f"копия описи — обычный файл {limit + 1} Б: откат за {GARBAGE_SECONDS} с, без трассы",
                run["rc"] is not None and "Traceback" not in both, f"{run['seconds']:.1f} с; {run['err'][-300:]}")
    PROBE.check("D61", "копия описи больше предела: «опись возвращена к состоянию до обновления» не сказано, "
                "опись проекта не из нулей предела",
                "опись возвращена к состоянию до обновления" not in both and len(manifest) <= limit,
                f"опись {len(manifest)} Б; {both[-300:]}")
    ceiling = healthy["maxrss_kb"] + limit // 4 // 1024
    PROBE.check("D61", f"копия описи больше предела: пик памяти двери < здоровый + {limit // 4 // 1024 // 1024} МБ "
                f"({ceiling // 1000} МБ) — размер судится до чтения",
                run["maxrss_kb"] < ceiling,
                f"пик {run['maxrss_kb'] // 1000} МБ, здоровый {healthy['maxrss_kb'] // 1000} МБ")


def case_d62() -> None:
    """Запуск без обязательных доводов или с --help: подсказка и код 2, не трасса KeyError.
    Ловит: parse_args снова берёт options["--plan"] без проверки."""
    env = {k: v for k, v in os.environ.items() if k not in DOOR_KEYS}
    for name, args in (("пусто", []), ("--help", ["--help"]), ("-h", ["-h"]),
                       ("без --project", ["--plan", "/нет-плана"])):
        r = execute([sys.executable, str(DOOR), *args], env)
        both = r.out + r.err
        PROBE.check("D62", f"{name}: код 2, «Запуск: layout-door.py», без Traceback",
                    r.rc == 2 and "Запуск: layout-door.py" in both and "Traceback" not in both,
                    f"код {r.rc}; {both[-300:]}")


CASES = (case_d1, case_d2, case_d3, case_d4, case_d5, case_d6, case_d7, case_d8, case_d9,
         case_d10, case_d11, case_d12, case_d13, case_d14, case_d15, case_d16, case_d17,
         case_d18, case_d19, case_d20, case_d21, case_d22, case_d23, case_d24, case_d25,
         case_d26, case_d27, case_d28, case_d29, case_d30, case_d31, case_d32, case_d33,
         case_d34, case_d35, case_d36, case_d37, case_d38, case_d39, case_d40, case_d41,
         case_d42, case_d43, case_d44, case_d45, case_d46, case_d47, case_d48, case_d49,
         case_d50, case_d51, case_d52, case_d53, case_d54, case_d55, case_d56, case_d57,
         case_d58, case_d59, case_d60, case_d61, case_d62, case_ds1, case_ds2, case_ds3,
         case_ds4, case_ds5, case_ds6, case_ds7, case_ds8, case_ds9, case_ds10, case_ds11, case_ds12)


def main() -> int:
    print(f"дверь: {DOOR}" + ("" if DOOR_PRESENT else " — НЕТ, каждая проверка красная"))
    for case in CASES:
        label = case.__name__.removeprefix("case_").upper()
        try:
            case()
        except Exception as error:      # a probe bug must show as red, not as a trace
            PROBE.crashed(label, error)
    print(f"ДВЕРЬ УКЛАДКИ: путей {PROBE.paths}, неудач {PROBE.failures}")
    return 0 if PROBE.failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
