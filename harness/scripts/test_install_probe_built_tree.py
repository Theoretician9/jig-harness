#!/usr/bin/env python3
"""Проба: гейт «установка с нуля» ставит из ПУБЛИЧНОЙ СБОРКИ, а не из сырого пакета.

Ночь 11.11.2026: install-probe.sh монтировал в контейнер сырой пакет, а манифест
слоя 1 (harness/self/MANIFEST.sha256) кладёт только build-public.py в собранное
дерево. С Д-18 раскладка печатает эталон по каталогу пакета и отказ печати даёт
код 1 — установка в стенде умирала на шаге 7-а «нет манифеста», хотя человек
ставит из сборки, где манифест есть. Проба судила не то, что получит человек.

Стенд — свой каталог harness-run-* под TMPDIR:
  * пакет-источник — git clone пакета (учтённое состояние, без неучтённого
    манифеста, если он лежит в рабочем дереве);
  * install.conf и harness.conf стенда, журналы — внутри стенда;
  * подставной docker первым в PATH: на `docker run` он судит каталог, который
    монтируется в /mnt/paket, пока тот ещё жив; прочее — ответ «ок».
Живой docker, /etc/harness и /var/log/harness не трогаются.

Случаи:
  1) install-probe дошёл до подъёма контейнера (`docker run` с /mnt/paket) — иначе
     судить нечего, провал;
  2) в смонтированном дереве есть harness/self/MANIFEST.sha256, и он сходится
     (`sha256sum -c`) — эталон в установке есть чем печатать;
  3) смонтированное дерево — публичная сборка (метка .публичная-сборка);
  4) пакет-источник не тронут: манифеста в нём нет, рабочее дерево чисто —
     сборка не пишет в источник;
  5) смонтирован не источник, и после выхода пробы этого каталога нет — сборка
     убрана за собой (диск машины ~81 %).

Дверь мутаций: INSTALL_PROBE_SCRIPT — проба установки вместо своей.
Прогон: python3 scripts/test_install_probe_built_tree.py   Код: 0 — все случаи прошли.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIMIT_SEC = 600

FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STAND_DOCKER_LOG"
case "${1:-}" in
  run)
    src=""
    for a in "$@"; do
      case "$a" in *:/mnt/paket:ro) src="${a%:/mnt/paket:ro}" ;; esac
    done
    printf 'смонтировано=%s\n' "$src" >> "$STAND_VERDICT"
    if [ -n "$src" ] && [ -f "$src/harness/self/MANIFEST.sha256" ] \
        && (cd "$src/harness/self" && sha256sum -c --quiet MANIFEST.sha256 >/dev/null 2>&1); then
      echo "манифест=сходится" >> "$STAND_VERDICT"
    else
      echo "манифест=нет" >> "$STAND_VERDICT"
    fi
    [ -f "$src/.публичная-сборка" ] && echo "метка=есть" >> "$STAND_VERDICT"
    echo 0123456789ab
    exit 0 ;;
  exec)
    [ "${2:-}" = -i ] && cat > /dev/null
    case "$*" in *is-system-running*) echo running ;; esac
    exit 0 ;;
esac
exit 0
"""


def package_root() -> Path:
    beside = HERE.parent.parent
    if (beside / "harness" / "razlozhit-harnes.sh").is_file():
        return beside
    sys.path.insert(0, str(HERE / "lib"))
    import config
    return config.katalog_paketa()


def make_stand(root: Path) -> Path:
    pkg = root / "пакет"
    subprocess.run(["git", "clone", "-q", "--shared", str(package_root()), str(pkg)],
                   check=True, env=clean_env())
    for d in ("журналы", "проект", "bin", "tmp"):
        (root / d).mkdir()
    (root / "install.conf").write_text(
        f'PROJECT_DIR="{root}/проект"\nPROJECT_NAME="стенд"\nAGENT_USER="agent"\n'
        f'TG_CHAT_ID="100200300"\nLOG_DIR="{root}/журналы"\nSTARTER_DIR="{pkg}"\n',
        encoding="utf-8")
    (root / "harness.conf").write_text(
        f'LOG_DIR="{root}/журналы"\nSTARTER_DIR="{pkg}"\n', encoding="utf-8")
    docker = root / "bin" / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    return pkg


def clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def run_probe(root: Path, pkg: Path) -> subprocess.CompletedProcess:
    script = os.environ.get("INSTALL_PROBE_SCRIPT") or str(HERE / "install-probe.sh")
    env = clean_env()
    env.update({
        "PATH": f"{root}/bin:{env.get('PATH', '/usr/bin:/bin')}",
        "TMPDIR": str(root / "tmp"),
        "HARNESS_HOME": str(root),
        "HARNESS_INSTALL_CONF": str(root / "install.conf"),
        "HARNESS_CONF": str(root / "harness.conf"),
        "LOG_DIR": str(root / "журналы"),
        "STARTER_DIR": str(pkg),
        "INSTALL_PROBE_IMAGE": "стенд-пробы:0",
        "STAND_DOCKER_LOG": str(root / "docker.log"),
        "STAND_VERDICT": str(root / "вердикт.txt"),
    })
    return subprocess.run(["bash", script], env=env, capture_output=True, text=True,
                          timeout=LIMIT_SEC)


def main() -> int:
    base = os.environ.get("TMPDIR") or "/tmp"
    root = Path(tempfile.mkdtemp(prefix="harness-run-install-probe-tree-", dir=base))
    fails = 0

    def check(ok: bool, text: str) -> None:
        nonlocal fails
        print(f"  {'ок   ' if ok else 'ПЛОХО'} {text}")
        fails += 0 if ok else 1

    try:
        pkg = make_stand(root)
        done = run_probe(root, pkg)
        verdict_file = root / "вердикт.txt"
        verdict = verdict_file.read_text(encoding="utf-8").splitlines() if verdict_file.exists() else []
        mounted = next((l.split("=", 1)[1] for l in verdict if l.startswith("смонтировано=")), "")

        check(bool(mounted), f"проба дошла до подъёма контейнера с /mnt/paket (код пробы {done.returncode})")
        if not mounted:
            print((done.stdout + done.stderr)[-2000:])
        check("манифест=сходится" in verdict,
              f"в смонтированном дереве есть harness/self/MANIFEST.sha256 и он сходится ({mounted or 'не смонтировано'})")
        check("метка=есть" in verdict, "смонтировано публичное дерево (метка .публичная-сборка)")
        status = subprocess.run(["git", "-C", str(pkg), "status", "--porcelain"],
                                capture_output=True, text=True, env=clean_env()).stdout.strip()
        check(not (pkg / "harness" / "self" / "MANIFEST.sha256").exists() and not status,
              f"пакет-источник не тронут{': ' + status.splitlines()[0] if status else ''}")
        check(bool(mounted) and Path(mounted).resolve() != pkg.resolve() and not Path(mounted).exists(),
              "смонтирован не источник, и сборка убрана после выхода пробы")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if fails:
        print(f"ПРОБА СБОРКИ ДЛЯ УСТАНОВКИ: КРАСНАЯ ({fails} из 5)")
        return 1
    print("ПРОБА СБОРКИ ДЛЯ УСТАНОВКИ: зелёная (5 из 5)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
