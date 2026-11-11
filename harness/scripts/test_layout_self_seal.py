#!/usr/bin/env python3
"""Проба: раскладка печатает эталон личности по каталогу С МАНИФЕСТОМ и не глотает отказ (Д-18).

Живая установка 11.11.2026: раскладчик звал `sudo -n self-seal "$PROJECT_DIR"`, а в
проект раскладка кладёт слой 1 без MANIFEST.sha256 — манифест есть только в пакете
(публичной сборке). self-seal отвечал «нет манифеста», `|| true` глотал отказ,
/etc/harness/self не появлялся, и защита эталона не действовала ни на одной машине.

Стенд пробы — свой каталог harness-run-* под TMPDIR:
  * копия пакета без .git, в неё кладётся harness/self/MANIFEST.sha256 тем же
    писателем, что у публичной сборки (build-public.py: записать_манифест_слоя1);
  * install.conf и harness.conf стенда (проект, журналы, копии — внутри стенда);
  * подставной sudo первым в PATH: self-seal исполняется НАСТОЯЩИМ self-seal.sh
    пакета с SELF_ETALON_DIR внутри стенда, прочее (systemctl restart) — отказ;
  * токен бота — несуществующий файл, API — закрытый порт: сеть не трогается.
Раскладка идёт БЕЗ HARNESS_SELFTEST — иначе self-seal не зовётся вовсе. Живые
/etc/harness/self и /usr/local/lib/harness не трогаются; /etc/harness/harness.conf
раскладка кладёт только если его нет, поэтому без него проба отказывается судить.

Случаи:
  1) self-seal настоящий → раскладка код 0, довод self-seal — каталог с
     harness/self/MANIFEST.sha256, эталон стенда лёг (core.json на месте);
  2) self-seal отказал → код раскладки ≠ 0;
  3) self-seal ответил 0 → код раскладки 0 (код в случае 2 — от отказа, не от иного).
  4) правила sudo на self-seal нет (отвечает сам sudo) → строка-предупреждение, код 0;
  5) sudo пишет свою строку («unable to resolve host») перед отказом self-seal → код ≠ 0:
     строка sudo в общем потоке не делает отказ печати предупреждением.
В случаях 2–5 раскладка обязана дойти до sudo с self-seal: нет обращения — провал.

Дверь мутаций: LAYOUT_SCRIPT — раскладчик, который кладётся в копию пакета вместо своего.
Прогон: python3 scripts/test_layout_self_seal.py   Код: 0 — все случаи прошли.
"""
import getpass
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIMIT_SEC = 900

FAKE_SUDO = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STAND_SUDO_LOG"
# Живой sudo без разрешимого имени машины пишет это на КАЖДЫЙ вызов, до вывода команды.
[ "$STAND_SEAL_MODE" = refuse-noisy ] && echo "sudo: unable to resolve host стенд: Name or service not known" >&2
[ "${1:-}" = -n ] && shift
case "${1:-} ${2:-}" in
  "-l "*/self-seal)
    [ "$STAND_SEAL_MODE" = norule ] && { echo "sudo: a password is required" >&2; exit 1; }
    exit 0 ;;
esac
case "${1:-}" in
  */self-seal)
    shift
    printf '%s\n' "${1:-}" >> "$STAND_SEAL_ARGS"
    case "$STAND_SEAL_MODE" in
      refuse|refuse-noisy) echo "self-seal: отказ: стенд пробы" >&2; exit 1 ;;
      ok) exit 0 ;;
      norule) echo "sudo: a password is required" >&2; exit 1 ;;
      *) SELF_ETALON_DIR="$STAND_ETALON" exec bash "$STAND_SEAL_SH" "$@" ;;
    esac ;;
esac
exit 1
"""


def package_root() -> Path:
    beside = HERE.parent.parent
    if (beside / "harness" / "razlozhit-harnes.sh").is_file():
        return beside
    sys.path.insert(0, str(HERE / "lib"))
    import config
    return config.katalog_paketa()


def write_manifest(pkg: Path) -> None:
    """Манифест кладёт писатель публичной сборки: второго писателя проба не заводит."""
    spec = importlib.util.spec_from_file_location("build_public", pkg / "harness" / "scripts" / "build-public.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.записать_манифест_слоя1(pkg)


def make_stand(root: Path) -> dict:
    src = package_root()
    pkg = root / "pkg"
    shutil.copytree(src, pkg, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    if os.environ.get("LAYOUT_SCRIPT"):
        shutil.copy2(os.environ["LAYOUT_SCRIPT"], pkg / "harness" / "razlozhit-harnes.sh")
    write_manifest(pkg)
    for d in ("log", "hb", "копии", "tmp", "bin"):
        (root / d).mkdir()
    (root / "install.conf").write_text(
        f'PROJECT_DIR="{root}/proj"\nPROJECT_NAME="стенд"\nAGENT_USER="{getpass.getuser()}"\n'
        f'LOG_DIR="{root}/log"\nHEARTBEAT_DIR="{root}/hb"\n', encoding="utf-8")
    sample = (pkg / "harness" / "config" / "harness.conf").read_text(encoding="utf-8").splitlines()
    conf = [f'BACKUP_DIR="{root}/копии"' if l.startswith("BACKUP_DIR=") else l for l in sample]
    (root / "harness.conf").write_text("\n".join(conf) + "\n", encoding="utf-8")
    sudo = root / "bin" / "sudo"
    sudo.write_text(FAKE_SUDO, encoding="utf-8")
    sudo.chmod(0o755)
    return {"pkg": pkg, "manifest": pkg / "harness" / "self" / "MANIFEST.sha256"}


def run_layout(root: Path, pkg: Path, mode: str) -> tuple[subprocess.CompletedProcess, list[str], bool]:
    """Код раскладки, доводы вызовов self-seal и было ли обращение к sudo про self-seal вообще."""
    seal_args = root / f"seal-args-{mode}.txt"
    sudo_log = root / f"sudo-{mode}.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({
        "PATH": f"{root}/bin:{env.get('PATH', '/usr/bin:/bin')}",
        "TMPDIR": str(root / "tmp"),
        "HARNESS_SELFTEST": "0",
        "HARNESS_INSTALL_CONF": str(root / "install.conf"),
        "HARNESS_CONF": str(root / "harness.conf"),
        "BACKUP_DIR": "", "LOG_DIR": "", "LAYOUT_LOCK_WAIT_SEC": "",
        "TG_TOKEN_FILE": str(root / "нет-токена"),
        "TG_API_BASE": "http://127.0.0.1:9",
        "STAND_SUDO_LOG": str(sudo_log),
        "STAND_SEAL_ARGS": str(seal_args),
        "STAND_SEAL_MODE": mode,
        "STAND_SEAL_SH": str(pkg / "harness" / "scripts" / "self-seal.sh"),
        "STAND_ETALON": str(root / "etalon"),
    })
    r = subprocess.run(["bash", str(pkg / "harness" / "razlozhit-harnes.sh")], cwd=root, env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=LIMIT_SEC)
    args = seal_args.read_text(encoding="utf-8").split() if seal_args.exists() else []
    asked = sudo_log.exists() and "/self-seal" in sudo_log.read_text(encoding="utf-8")
    return r, args, asked


def verdict(name: str, ok: bool, detail: str, r: subprocess.CompletedProcess) -> int:
    print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}"
          + ("" if ok else f"; хвост вывода: {(r.stdout + r.stderr).strip()[-300:]!r}"))
    return 0 if ok else 1


def main() -> int:
    if not Path("/etc/harness/harness.conf").is_file():
        print("ЭТАЛОН-РАСКЛАДКИ: нет /etc/harness/harness.conf — раскладка без самотеста положила бы "
              "туда образец; на этой машине проба не судит")
        return 1
    root = Path(tempfile.mkdtemp(prefix="harness-run-self-seal-"))
    failed = 0
    try:
        stand = make_stand(root)
        if not stand["manifest"].is_file() or not stand["manifest"].read_text(encoding="utf-8").strip():
            print("ЭТАЛОН-РАСКЛАДКИ: манифест стенда не записан — судить нечего")
            return 1

        r, args, _ = run_layout(root, stand["pkg"], "real")
        has_manifest = bool(args) and all((Path(a) / "harness/self/MANIFEST.sha256").is_file() for a in args)
        sealed = (root / "etalon" / "core.json").is_file()
        failed += verdict("1) self-seal настоящий → код 0, довод с манифестом, эталон лёг",
                          r.returncode == 0 and has_manifest and sealed,
                          f"код {r.returncode}, вызовов self-seal {len(args)}, довод {args or '—'}, "
                          f"манифест в доводе {'есть' if has_manifest else 'нет'}, эталон {'лёг' if sealed else 'нет'}", r)

        r, args, _ = run_layout(root, stand["pkg"], "refuse")
        failed += verdict("2) self-seal отказал → код раскладки ≠ 0", r.returncode != 0 and bool(args),
                          f"код {r.returncode}, вызовов self-seal {len(args)}", r)

        r, args, _ = run_layout(root, stand["pkg"], "ok")
        failed += verdict("3) self-seal ответил 0 → код раскладки 0", r.returncode == 0 and bool(args),
                          f"код {r.returncode}, вызовов self-seal {len(args)}", r)

        r, _, asked = run_layout(root, stand["pkg"], "norule")
        warned = "ВНИМАНИЕ: эталон личности не напечатан" in r.stdout and "нет правила sudo" in r.stdout
        failed += verdict("4) правила sudo нет → предупреждение, код 0", r.returncode == 0 and asked and warned,
                          f"код {r.returncode}, sudo спрошен {'да' if asked else 'нет'}, "
                          f"предупреждение {'есть' if warned else 'нет'}", r)

        r, args, _ = run_layout(root, stand["pkg"], "refuse-noisy")
        failed += verdict("5) строка sudo перед отказом self-seal → код раскладки ≠ 0",
                          r.returncode != 0 and bool(args),
                          f"код {r.returncode}, вызовов self-seal {len(args)}", r)
    finally:
        subprocess.run(["chmod", "-R", "u+w", str(root)], stderr=subprocess.DEVNULL)
        shutil.rmtree(root, ignore_errors=True)
    print(f"ЭТАЛОН-РАСКЛАДКИ: случаев 5, упало {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
