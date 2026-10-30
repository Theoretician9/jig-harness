#!/usr/bin/env python3
"""Probe: two package commits in one second still give the LAST commit's build.

Files from `git archive` get mtime = commit time, and rsync by default skips a
file whose size and whole-second mtime match. Two commits in the same second
that change a file without changing its size would leave the OLD content in
the public build while the label claims the new hash, and the cleanliness
check stays green. The second is created by data (one commit date), not by
waiting, so the probe is deterministic.

Door: BUILD_PUBLIC_UNDER_TEST — path to the build-public.py copy to judge
(default: the one next to this file). Mutation runs point it at a copy.

Run: python3 scripts/test_build_public_same_second.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

UNDER_TEST = Path(os.environ.get("BUILD_PUBLIC_UNDER_TEST")
                  or Path(__file__).resolve().parent / "build-public.py")
SAME_SECOND = dict(os.environ,
                   GIT_COMMITTER_DATE="2026-01-01T00:00:00+00:00",
                   GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00")
RACY = "harness/STATE.md"

checked = failed = 0


def probe(ok: bool, text: str) -> None:
    global checked, failed
    checked += 1
    failed += 0 if ok else 1
    print(("  ок    " if ok else "  ПЛОХО ") + text)


def git(package: Path, *args: str, env=None) -> str:
    return subprocess.run(["git", "-C", str(package), "-c", "user.name=t",
                           "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True, env=env).stdout.strip()


def commit_same_second(package: Path, text: str) -> None:
    (package / RACY).write_text(text, encoding="utf-8")
    git(package, "add", "-A")
    git(package, "commit", "-qm", text.strip(), env=SAME_SECOND)


def label_hash(build: Path, label: str) -> str:
    for line in (build / label).read_text(encoding="utf-8").splitlines():
        if line.startswith("пакет:"):
            return line.split(":", 1)[1].strip()
    return ""


def build_by_cli(stand: Path, package: Path, build: Path) -> subprocess.CompletedProcess:
    """The door publish.sh uses: the script itself, every input from the stand."""
    return subprocess.run(
        [sys.executable, str(UNDER_TEST), "--пакет", str(package), "--куда", str(build),
         "--паспорт", str(stand / "паспорт.conf"), "--конфиг", str(stand / "harness.conf"),
         "--данные", str(stand / "public-build.yaml")],
        capture_output=True, text=True, timeout=120)


def stand_package(stand: Path) -> Path:
    package = stand / "пакет"
    (package / "harness").mkdir(parents=True)
    git(package, "init", "-q", "-b", "master")
    for name in ("паспорт.conf", "harness.conf", "public-build.yaml"):
        (stand / name).write_text("", encoding="utf-8")
    return package


def content_case(stand: Path) -> None:
    package = stand_package(stand)
    build = stand / "сборка"
    build.mkdir()

    commit_same_second(package, "состояние А\n")
    first_time = git(package, "log", "-1", "--format=%ct")
    first = build_by_cli(stand, package, build)
    first_stat = (build / RACY).stat() if (build / RACY).exists() else None
    commit_same_second(package, "состояние Б\n")
    second = build_by_cli(stand, package, build)

    # The condition judges itself: without it the probe is green on the defect.
    probe(first.returncode == 0 and second.returncode == 0,
          f"обе сборки прошли (коды {first.returncode}, {second.returncode}"
          f"{': ' + (second.stderr or first.stderr).strip() if second.returncode or first.returncode else ''})")
    probe(git(package, "log", "-1", "--format=%ct") == first_time
          and first_stat is not None and int(first_stat.st_mtime) == int(first_time)
          and first_stat.st_size == (package / RACY).stat().st_size,
          f"условие создано: оба коммита в секунду {first_time}, размер тот же, "
          "mtime файла в сборке = время коммита")
    built = (build / RACY).read_text(encoding="utf-8") if (build / RACY).exists() else ""
    probe(built == "состояние Б\n",
          f"БОЛЬНОЙ: содержимое в сборке — последнего коммита (в сборке {built.strip()!r})")


def label_case(stand: Path) -> None:
    """The label is rewritten by every build, its mtime is the second of writing.

    That second is created here too: right before the build's real rsync the
    label in the temporary tree gets the mtime of the label already built.
    In-process, because only there the moment between writing and rsync exists.
    """
    spec = importlib.util.spec_from_file_location("build_public_under_test", UNDER_TEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    label = module.МЕТКА

    package = stand_package(stand)
    build = stand / "сборка"
    build.mkdir()
    commit_same_second(package, "состояние А\n")
    module.собрать(package, str(build), {}, {})
    commit_same_second(package, "состояние Б\n")

    real_run = subprocess.run
    aligned = []

    def run_label_same_second(args, *rest, **options):
        if args and args[0] == "rsync":
            source = Path(args[-2]) / label
            target = (build / label).stat()
            os.utime(source, ns=(target.st_atime_ns, target.st_mtime_ns))
            aligned.append(source.stat().st_mtime_ns == target.st_mtime_ns
                           and source.stat().st_size == target.st_size)
        return real_run(args, *rest, **options)

    subprocess.run = run_label_same_second
    try:
        module.собрать(package, str(build), {}, {})
    finally:
        subprocess.run = real_run

    probe(aligned == [True],
          f"условие метки создано: rsync сборки позван {len(aligned)} раз, "
          "метке даны mtime и размер метки в сборке")
    head = git(package, "rev-parse", "--short", "HEAD")
    got = label_hash(build, label)
    probe(got == head,
          f"БОЛЬНОЙ: метка несёт хэш последнего коммита (метка {got or '—'}, HEAD {head})")


def main() -> int:
    print(f"судится: {UNDER_TEST}")
    with tempfile.TemporaryDirectory(prefix="same-second-content-") as tmp:
        content_case(Path(tmp))
    with tempfile.TemporaryDirectory(prefix="same-second-label-") as tmp:
        label_case(Path(tmp))
    print(f"путей {checked}, неудач {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
