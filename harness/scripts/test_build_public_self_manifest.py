#!/usr/bin/env python3
"""Probe: the public build carries the layer-1 manifest, and it matches the build.

On a user's machine self-seal.sh prints the personality reference from
harness/self/MANIFEST.sha256 of the PUBLIC build — the only package the user
has. A manifest written anywhere else (the package working tree, the project)
never reaches the user: self-seal refuses «нет манифеста», the installer
swallows the refusal, and /etc/harness/self never appears. So the contract is
judged where the user gets it — in the build itself:

  cd <build>/harness/self && sha256sum -c MANIFEST.sha256   → green,
  and the manifest names exactly the layer-1 files of the build
  (everything in harness/self except instance.yaml and the manifest).

The build depersonalizes documents, and harness/self/roles carry the project
path, so a manifest hashed from the PACKAGE tree would not match the build.
The stand creates that condition and checks it took effect.

Door: BUILD_PUBLIC_UNDER_TEST — path to the build-public.py copy to judge
(default: the one next to this file). Mutation runs point it at a copy.

Run: python3 scripts/test_build_public_self_manifest.py
"""
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

UNDER_TEST = Path(os.environ.get("BUILD_PUBLIC_UNDER_TEST")
                  or Path(__file__).resolve().parent / "build-public.py")
SELF = "harness/self"
MANIFEST = "MANIFEST.sha256"
NOT_LAYER1 = {MANIFEST, "instance.yaml"}
ROLE = "roles/worker.md"

checked = failed = 0


def probe(ok: bool, text: str) -> None:
    global checked, failed
    checked += 1
    failed += 0 if ok else 1
    print(("  ок    " if ok else "  ПЛОХО ") + text)


def git(package: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(package), "-c", "user.name=t",
                           "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def stand(root: Path) -> tuple[Path, Path, Path]:
    """Package with a layer 1 whose role names the project path, as the real one does."""
    project = root / "проект"
    project.mkdir()
    package = root / "пакет"
    self_dir = package / SELF
    (self_dir / "roles").mkdir(parents=True)
    (self_dir / "core.json").write_text('{"смысл": "освобождать внимание"}\n', encoding="utf-8")
    (self_dir / "standards.md").write_text("# Стандарты\n\n- И-4. Честный отчёт.\n",
                                           encoding="utf-8")
    (self_dir / ROLE).write_text(f"# Роль\n\nПроект — `{project}`.\n", encoding="utf-8")
    (self_dir / "instance.yaml").write_text("копия: стенд\n", encoding="utf-8")
    git(package, "init", "-q", "-b", "master")
    git(package, "add", "-A")
    git(package, "commit", "-qm", "слой 1 стенда")

    (root / "паспорт.conf").write_text(f"PROJECT_DIR={project}\n", encoding="utf-8")
    (root / "harness.conf").write_text("", encoding="utf-8")
    (root / "public-build.yaml").write_text("обезличить_ключи:\n  - PROJECT_DIR\n",
                                            encoding="utf-8")
    build = root / "сборка"
    build.mkdir()
    return package, build, project


def build_by_cli(root: Path, package: Path, build: Path) -> subprocess.CompletedProcess:
    """The door publish.sh uses: the script itself, every input from the stand."""
    return subprocess.run(
        [sys.executable, str(UNDER_TEST), "--пакет", str(package), "--куда", str(build),
         "--паспорт", str(root / "паспорт.conf"), "--конфиг", str(root / "harness.conf"),
         "--данные", str(root / "public-build.yaml"), "--коммит", "HEAD"],
        capture_output=True, text=True, timeout=120)


def layer1(self_dir: Path) -> set[str]:
    return {str(p.relative_to(self_dir)) for p in self_dir.rglob("*")
            if p.is_file() and str(p.relative_to(self_dir)) not in NOT_LAYER1}


def manifest_entries(path: Path) -> dict[str, str]:
    entries = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        if name:
            entries[name] = digest
    return entries


def main() -> int:
    print(f"судится: {UNDER_TEST}")
    with tempfile.TemporaryDirectory(prefix="harness-run-self-manifest-") as tmp:
        root = Path(tmp)
        package, build, project = stand(root)
        run = build_by_cli(root, package, build)
        probe(run.returncode == 0,
              f"сборка прошла (код {run.returncode}"
              f"{': ' + run.stderr.strip() if run.returncode else ''})")

        built_self = build / SELF
        role = built_self / ROLE
        role_text = role.read_text(encoding="utf-8") if role.is_file() else ""
        # The condition judges itself: without depersonalization a manifest
        # hashed from the package would pass as well.
        probe(role.is_file() and str(project) not in role_text,
              f"условие создано: путь проекта в {ROLE} сборки обезличен")

        manifest = built_self / MANIFEST
        probe(manifest.is_file() and not manifest.is_symlink(),
              f"БОЛЬНОЙ: в сборке есть {SELF}/{MANIFEST}")
        entries = manifest_entries(manifest) if manifest.is_file() else {}
        expected = layer1(built_self)
        probe(bool(expected) and set(entries) == expected,
              f"манифест называет ровно слой 1 сборки (в манифесте {sorted(entries)}, "
              f"в сборке {sorted(expected)})")

        wrong = [name for name, digest in entries.items()
                 if not (built_self / name).is_file()
                 or hashlib.sha256((built_self / name).read_bytes()).hexdigest() != digest]
        probe(bool(entries) and not wrong,
              f"суммы манифеста сходятся с файлами сборки (не сошлись: {wrong or '—'})")
        seal = subprocess.run(["sha256sum", "--quiet", "-c", MANIFEST], cwd=built_self,
                              capture_output=True, text=True) if manifest.is_file() else None
        probe(seal is not None and seal.returncode == 0,
              f"sha256sum -c в {SELF} сборки зелёный "
              f"({'манифеста нет' if seal is None else f'код {seal.returncode}'})")
    print(f"путей {checked}, неудач {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
