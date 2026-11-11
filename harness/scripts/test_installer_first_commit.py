#!/usr/bin/env python3
"""Проба: отказ первого коммита каркаса не даёт установщику кода 0 (Д-17).

Живая установка 11.11.2026: pre-commit проекта («GATE FAIL: семена») отверг первый
коммит каркаса, шаги 11–12 остались без штампа, проект — без единого коммита, а
USTANOVIT.sh вышел кодом 0. Отказ ушёл строкой в «УПАЛО», и ни установщик, ни тот,
кто его зовёт, провала не увидели.

Целиком установщик на стенде не запустить (root, apt, systemd), поэтому проба
собирает из него малый установщик: РАЗБОРОМ достаются все определения функций,
ловушка ERR и хвост прогона (от строки «шаг_0» до конца файла). Шаги, кроме
шаг_11, подменены пустыми; зафиксировать_пакет — пустой (живой зашит на
/home/<агент>/starter/STARTER-PACKAGE, его проба не трогает); осталось_руками —
пустой. первый_коммит_каркаса, шаг_11, финал и сводка — как в установщике.
Проект стенда — свой git с pre-commit-симлинком на подставной гейт.

Случаи:
  1) гейт отвергает первый коммит → код установщика ≠ 0, коммита нет;
  2) гейт пропускает → код 0, коммит есть (проба отличает успех от отказа).
Условие проба создаёт сама: свой каталог harness-run-* под TMPDIR.

Дверь мутаций: USTANOVIT_UNDER_TEST — установщик, который судит проба.
Прогон: python3 scripts/test_installer_first_commit.py   Код: 0 — все случаи прошли.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FUNC_START = re.compile(r"^([^\s#(){}=]+)\(\)\s*\{")
STUBBED = ("зафиксировать_пакет", "осталось_руками")
KEEP_STEP = "шаг_11"


def installer() -> Path:
    if os.environ.get("USTANOVIT_UNDER_TEST"):
        return Path(os.environ["USTANOVIT_UNDER_TEST"])
    beside = HERE.parent.parent / "USTANOVIT.sh"
    if beside.is_file():
        return beside
    sys.path.insert(0, str(HERE / "lib"))
    import config
    return config.katalog_paketa() / "USTANOVIT.sh"


def functions(lines: list[str]) -> dict[str, str]:
    """Определения функций верхнего уровня: однострочные и до строки «}»."""
    found, i = {}, 0
    while i < len(lines):
        m = FUNC_START.match(lines[i])
        if not m:
            i += 1
            continue
        if lines[i].rstrip().endswith("}") and lines[i].count("{") == lines[i].count("}"):
            found[m.group(1)] = lines[i]
            i += 1
            continue
        end = next((j for j in range(i + 1, len(lines)) if re.match(r"^\}\s*(#.*)?$", lines[j])), None)
        if end is None:
            break
        found[m.group(1)] = "\n".join(lines[i:end + 1])
        i = end + 1
    return found


def err_trap(lines: list[str]) -> str:
    start = next((i for i, l in enumerate(lines) if l.startswith("trap 'rc=$?;")), None)
    if start is None:
        return ""
    end = next((j for j in range(start, len(lines)) if lines[j].rstrip().endswith("' ERR")), None)
    return "\n".join(lines[start:end + 1]) if end is not None else ""


def run_tail(lines: list[str]) -> str:
    start = next((i for i, l in enumerate(lines) if l == "шаг_0"), None)
    return "\n".join(lines[start:]) if start is not None else ""


def build_mini(src: Path, stand: Path) -> tuple[str, str]:
    """Малый установщик; вторым — беда разбора (пусто, если всё найдено)."""
    lines = src.read_text(encoding="utf-8").splitlines()
    funcs, trap, tail = functions(lines), err_trap(lines), run_tail(lines)
    steps = sorted(set(re.findall(r"^(шаг_[0-9]+[а-яa-z]?)$", tail, re.M)))
    need = ("первый_коммит_каркаса", KEEP_STEP, "финал", "сводка", "делай")
    missing = [n for n in need if n not in funcs]
    if missing or not trap or not tail or KEEP_STEP not in steps:
        return "", (f"разбор установщика пуст: функций {len(funcs)}, нет {missing}, "
                    f"ловушка ERR {'есть' if trap else 'нет'}, хвост {'есть' if tail else 'нет'}, шагов {len(steps)}")
    stubs = [f"{n}() {{ :; }}" for n in steps if n != KEEP_STEP]
    stubs += ["зафиксировать_пакет() { return 0; }", "осталось_руками() { :; }"]
    body = [
        "set -Eeuo pipefail",
        trap,
        f"SELF='{src}'; SCRIPT_DIR='{src.parent}'",
        "DRY=0; RESUME=0; SELFTEST=0",
        "DONE_LIST=(); CHECKED_LIST=(); FAILED_LIST=(); MANUAL_LIST=()",
        "STEP_PULSE_SEC=1; PULSE_PID=''; CURRENT_STEP='до шагов'; CURRENT_CMD=''",
        # Агент без дома: любой путь /home/<агент>/… в стенде ведёт в пустоту, не в живой пакет.
        "AGENT_USER=harness-run-nobody; AS_AGENT=",
        f"PROJECT_DIR='{stand}/proj'; LOG_DIR='{stand}/log'; STAMP_DIR='{stand}/stamps'",
        f"MANUAL_FILE='{stand}/log/ОСТАЛОСЬ-РУКАМИ.txt'",
        *[v for k, v in funcs.items() if k not in STUBBED],
        *stubs,
        "FINAL_DONE=0",
        "trap 'финал' EXIT",
        tail,
    ]
    return "\n".join(body) + "\n", ""


def make_project(proj: Path, gate_rc: int) -> None:
    git_env = clean_env()
    subprocess.run(["git", "init", "-q", str(proj)], check=True, env=git_env)
    (proj / "README.md").write_text("стенд\n", encoding="utf-8")
    gate = proj / "scripts" / "pre-commit-gate.sh"
    gate.parent.mkdir(parents=True)
    gate.write_text(f"#!/usr/bin/env bash\necho 'GATE FAIL: семена (стенд)' >&2\nexit {gate_rc}\n"
                    if gate_rc else "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    gate.chmod(0o755)
    (proj / ".git" / "hooks" / "pre-commit").symlink_to("../../scripts/pre-commit-gate.sh")


def clean_env() -> dict:
    """Окружение без GIT_*: личный индекс вызывающего не должен течь в стенд."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def has_commit(proj: Path) -> bool:
    return subprocess.run(["git", "-C", str(proj), "rev-parse", "-q", "--verify", "HEAD"],
                          capture_output=True, env=clean_env()).returncode == 0


def main() -> int:
    src = installer()
    if not src.is_file():
        print(f"ПЕРВЫЙ-КОММИТ: нет установщика {src}")
        return 1
    root = Path(tempfile.mkdtemp(prefix="harness-run-first-commit-"))
    failed = 0
    try:
        cases = [("1) гейт отверг первый коммит → код ≠ 0, коммита нет", 1, False),
                 ("2) гейт пропустил → код 0, коммит есть", 0, True)]
        for n, (name, gate_rc, want_ok) in enumerate(cases):
            stand = root / f"c{n}"
            (stand / "log").mkdir(parents=True)
            mini, why = build_mini(src, stand)
            if why:
                print(f"ПЕРВЫЙ-КОММИТ: {why} — судить нечего")
                return 1
            make_project(stand / "proj", gate_rc)
            (stand / "mini.sh").write_text(mini, encoding="utf-8")
            env = {**clean_env(), "TMPDIR": str(stand)}
            r = subprocess.run(["bash", str(stand / "mini.sh")], cwd=stand, env=env,
                               capture_output=True, text=True, timeout=120)
            committed = has_commit(stand / "proj")
            ok = (r.returncode == 0) == want_ok and committed == want_ok
            failed += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {name}: код {r.returncode}, коммит {'есть' if committed else 'нет'}"
                  + ("" if ok else f"; хвост вывода: {(r.stdout + r.stderr).strip()[-300:]!r}"))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"ПЕРВЫЙ-КОММИТ: случаев 2, упало {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
