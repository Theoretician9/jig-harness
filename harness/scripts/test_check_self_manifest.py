#!/usr/bin/env python3
"""Проба: манифест эталона в пакете сходится с файлами слоя 1 (Р7, 0.11.1).

Раскладка нашей установки печатает эталон личности из ПАКЕТА
(razlozhit-harnes.sh → sudo self-seal <пакет>), а манифест пакета пишет только
дверь запроса согласия на публикацию. Роли поменяли двумя коммитами (13.11.2026),
манифест остался старым: печать отказала бы «sha256 не сошёлся: roles/lead.md».
Судья check-self-manifest.py держит те же правила, что self-seal.sh, но ДО печати —
в pre-commit, когда коммит трогает harness/self/.

Стенд — свой каталог: пакет с harness/self (core.json, standards.md, roles/*,
instance.yaml) и манифестом, посчитанным пробой. Случаи:
  верный манифест → 0; instance.yaml в манифест не входит и лишним не считается;
  неверный хэш роли → 1, в выводе имя роли и дверь починки (--манифест-слоя1);
  файл есть, в манифесте нет → 1; в манифесте есть, файла нет → 1;
  манифеста нет → 1; строка негодна → 1; путь с «..» → 1;
  пакет без --пакет берётся из HARNESS_STARTER (дверь config.katalog_paketa).

Дверь мутаций: CHECK_SELF_MANIFEST — судья под пробой.
Прогон: python3 scripts/test_check_self_manifest.py   Код: 0 — все случаи прошли.
"""
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

CHECKER = Path(os.environ.get("CHECK_SELF_MANIFEST") or HERE / "check-self-manifest.py")
LAYER1 = {"core.json": "{}\n", "standards.md": "# стандарты\n",
          "roles/lead.md": "# ведущий\n", "roles/worker.md": "# исполнитель\n"}


def make_pkg(root: Path) -> Path:
    self_dir = root / "harness" / "self"
    for rel, text in LAYER1.items():
        (self_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (self_dir / rel).write_text(text, encoding="utf-8")
    (self_dir / "instance.yaml").write_text("копия: стенд\n", encoding="utf-8")
    write_manifest(self_dir, sorted(LAYER1))
    return root


def write_manifest(self_dir: Path, rels: list[str]) -> None:
    lines = [f"{hashlib.sha256((self_dir / r).read_bytes()).hexdigest()}  {r}\n" for r in rels]
    (self_dir / "MANIFEST.sha256").write_text("".join(lines), encoding="utf-8")


def run(pkg: Path, by_env: bool = False) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "HARNESS_STARTER"}
    args = [sys.executable, str(CHECKER)]
    if by_env:
        env["HARNESS_STARTER"] = str(pkg)
    else:
        args += ["--пакет", str(pkg)]
    return subprocess.run(args, capture_output=True, text=True, env=env, timeout=60)


def case(c: Cases, name: str, spoil, code: int, must_say: tuple = (), by_env: bool = False) -> None:
    with tempfile.TemporaryDirectory(prefix="r7-self-manifest-") as tmp:
        pkg = make_pkg(Path(tmp))
        spoil(pkg / "harness" / "self")
        done = run(pkg, by_env)
        out = done.stdout + done.stderr
        c.check(f"{name}: код", code, done.returncode)
        for word in must_say:
            c.check(f"{name}: в выводе «{word}»", True, word in out)


def wrong_role_hash(s: Path) -> None:
    (s / "roles" / "lead.md").write_text("# ведущий, правка без манифеста\n", encoding="utf-8")


def extra_file(s: Path) -> None:
    (s / "roles" / "explorer.md").write_text("# разведчик\n", encoding="utf-8")


def missing_file(s: Path) -> None:
    (s / "roles" / "worker.md").unlink()


def no_manifest(s: Path) -> None:
    (s / "MANIFEST.sha256").unlink()


def bad_line(s: Path) -> None:
    with (s / "MANIFEST.sha256").open("a", encoding="utf-8") as f:
        f.write("не строка sha256sum\n")


def dotdot_path(s: Path) -> None:
    outside = s.parent / "outside.md"
    outside.write_text("x\n", encoding="utf-8")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    with (s / "MANIFEST.sha256").open("a", encoding="utf-8") as f:
        f.write(f"{digest}  ../outside.md\n")


def fix_by_hint(s: Path) -> None:
    """Правка роли, затем команда из подсказки судьи — следующий суд обязан быть зелёным."""
    wrong_role_hash(s)
    out = run(s.parent.parent).stdout
    command = next((l.split(": ", 1)[1] for l in out.splitlines() if "Пересчитать" in l), "false")
    subprocess.run(["bash", "-c", command], check=False, timeout=60)


def main() -> int:
    c = Cases()
    case(c, "верный манифест, instance.yaml вне манифеста", lambda s: None, 0)
    case(c, "неверный хэш роли", wrong_role_hash, 1, ("roles/lead.md", "Пересчитать"))
    case(c, "подсказка пересчёта чинит манифест", fix_by_hint, 0)
    case(c, "файл не в манифесте", extra_file, 1, ("roles/explorer.md",))
    case(c, "в манифесте нет файла", missing_file, 1, ("roles/worker.md",))
    case(c, "манифеста нет", no_manifest, 1, ("MANIFEST.sha256",))
    case(c, "строка манифеста негодна", bad_line, 1, ("не строка sha256sum",))
    case(c, "путь с «..»", dotdot_path, 1, ("../outside.md",))
    case(c, "пакет из HARNESS_STARTER, верный", lambda s: None, 0, by_env=True)
    case(c, "пакет из HARNESS_STARTER, неверный хэш", wrong_role_hash, 1, ("roles/lead.md",), by_env=True)
    print(f"{'ПРОЙДЕНО' if c.ok else 'ПРОВАЛ'}: путей {c.paths}, плохих {c.failures}")
    return 0 if c.ok else 1


if __name__ == "__main__":
    sys.exit(main())
