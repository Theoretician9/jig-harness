#!/usr/bin/env python3
"""Проба: после чистой раскладки карточки помощников равны сборке из личности проекта (Р4).

Живой стенд 13.11.2026: раскладка кладёт в
<проект>/.claude/agents карточки, собранные когда-то на машине разработки (ядро
старой версии, instance этой установки). Личность чистой установки другая, и
`self-prompt.py --карточки --проверить` отвечает 1, а сторож запуска помощников
(agent_budget_guard.card_state) отказывает КАЖДОМУ типу: на новой машине ни один
помощник не запускается.

Стенд — свой каталог harness-run-* под TMPDIR: копия пакета без .git,
install.conf и harness.conf стенда, подставные sudo/systemctl/crontab первыми в
PATH (отказ, живая машина не трогается), HARNESS_SELFTEST=1 (раскладка не
трогает /etc и не зовёт self-seal). Эталон личности — каталог стенда, которого
нет: сборка и сверка берут слой 1 из дерева проекта, раскладка видит ту же дверь
SELF_ETALON_DIR.

Случаи (типы карточек — разбором раздела «карточки» политики РАЗЛОЖЕННОГО
проекта; пустой список — провал):
  1) раскладка код 0; 1б) она сказала «карточки помощников пересобраны» (код 3
     сборщика без эталона — успех, не «ВНИМАНИЕ»); 1в) без двери
     HARNESS_LAYOUT_SELF_SEAL самотест печать не зовёт (подставные sudo/systemctl/
     crontab пишут свои доводы в STAND_STUB_LOG);
  2) self-prompt.py --карточки --проверить в проекте → код 0;
  3) card_state(<тип>) для каждого типа → (True, "") — сторож не отказывает;
  4) (ревью #5, Н1) повтор раскладки на том же стенде не трогает ни одной
     карточки: inode, mtime_ns и sha256 каждого файла .claude/agents прежние, и
     ни одна карточка не легла в архив раскладки (файлы …/files/.claude/agents/*
     под стендом до и после повтора равны). Список карточек — разбором каталога
     проекта; пустой — провал. Зелёной её оставила бы только поломка, которая
     пишет карточку мимо проекта стенда.
  5) (ревью #5, Н2) порядок «печать эталона → карточки». Свой стенд, где уже
     лежит СТАРЫЙ эталон (core.json другой версии). Подставной sudo отвечает 0
     на `-n -l …/self-seal`, а на `-n …/self-seal <выкладка>` кладёт
     <выкладка>/harness/self в эталон стенда (без instance.yaml и MANIFEST —
     манифест пакета вне git и не судится здесь). Печать при HARNESS_SELFTEST=1
     открывает дверь HARNESS_LAYOUT_SELF_SEAL=1 (её добавляет правка Н2: сейчас
     печать в самотесте выключена, и случай красный на «печать не позвана»).
     Судится: печать позвана; эталон стенда — новый (core.json равен пакетному);
     `self-prompt.py --карточки --проверить` относительно НОВОГО эталона → 0.
     Карточки, собранные до печати, несут старое ядро и дают код 1.

Дверь мутаций: LAYOUT_SCRIPT — раскладчик, который кладётся в копию пакета вместо своего.
Прогон: python3 scripts/test_layout_builds_cards.py   Код: 0 — все случаи прошли.
"""
import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
LIMIT_SEC = 900
STUB = '#!/usr/bin/env bash\nprintf "%s %s\\n" "${0##*/}" "$*" >> "${STAND_STUB_LOG:-/dev/null}"\nexit 1\n'
# Печать эталона стенда (Н2): правило sudo «есть», self-seal кладёт слой 1 выкладки в
# эталон стенда. Остальные вызовы sudo — отказ, как у STUB.
SEAL_SUDO = """#!/usr/bin/env bash
if [ "$*" = "-n -l /usr/local/lib/harness/self-seal" ]; then exit 0; fi
if [ "$1 $2" = "-n /usr/local/lib/harness/self-seal" ] && [ "$#" -eq 3 ]; then
  printf '%s\\n' "$3" >> "$STAND_SEAL_LOG"
  rm -rf "$STAND_ETALON.new" && cp -r "$3/harness/self" "$STAND_ETALON.new" || exit 1
  rm -f "$STAND_ETALON.new/instance.yaml" "$STAND_ETALON.new/MANIFEST.sha256"
  rm -rf "$STAND_ETALON" && mv "$STAND_ETALON.new" "$STAND_ETALON"
  exit 0
fi
exit 1
"""


def package_root() -> Path:
    beside = HERE.parent.parent
    if (beside / "harness" / "razlozhit-harnes.sh").is_file():
        return beside
    sys.path.insert(0, str(HERE / "lib"))
    import config
    return config.katalog_paketa()


def make_stand(root: Path) -> Path:
    pkg = root / "pkg"
    shutil.copytree(package_root(), pkg, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    if os.environ.get("LAYOUT_SCRIPT"):
        shutil.copy2(os.environ["LAYOUT_SCRIPT"], pkg / "harness" / "razlozhit-harnes.sh")
    for d in ("log", "hb", "копии", "tmp", "bin"):
        (root / d).mkdir()
    (root / "install.conf").write_text(
        f'PROJECT_DIR="{root}/proj"\nPROJECT_NAME="стенд"\nAGENT_USER="{getpass.getuser()}"\n'
        f'LOG_DIR="{root}/log"\nHEARTBEAT_DIR="{root}/hb"\n', encoding="utf-8")
    sample = (pkg / "harness" / "config" / "harness.conf").read_text(encoding="utf-8").splitlines()
    conf = [f'BACKUP_DIR="{root}/копии"' if l.startswith("BACKUP_DIR=") else l for l in sample]
    (root / "harness.conf").write_text("\n".join(conf) + "\n", encoding="utf-8")
    for name in ("sudo", "systemctl", "crontab"):
        stub = root / "bin" / name
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
    return pkg


def stand_env(root: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_", "SELF_")) and k not in ("PROJECT_DIR", "PROJECT_NAME", "LAUNCH_POLICY")}
    env.update({
        "PATH": f"{root}/bin:{env.get('PATH', '/usr/bin:/bin')}",
        "TMPDIR": str(root / "tmp"),
        "HARNESS_SELFTEST": "1",
        "HARNESS_INSTALL_CONF": str(root / "install.conf"),
        "HARNESS_CONF": str(root / "harness.conf"),
        "BACKUP_DIR": "", "LOG_DIR": "", "LAYOUT_LOCK_WAIT_SEC": "",
        "TG_TOKEN_FILE": str(root / "нет-токена"),
        "TG_API_BASE": "http://127.0.0.1:9",
        "SELF_ETALON_DIR": str(root / "нет-эталона"),
        "STAND_STUB_LOG": str(root / "заглушки.log"),
    })
    return env


CARD_STATE = """
import json, sys
sys.path.insert(0, sys.argv[1])
import agent_budget_guard as guard
print(json.dumps({t: list(guard.card_state(t)) for t in sys.argv[2:]}, ensure_ascii=False))
"""


def lay_out(pkg: Path, root: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(pkg / "harness" / "razlozhit-harnes.sh")], cwd=root, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=LIMIT_SEC)


def cards_snapshot(proj: Path) -> dict:
    """{имя карточки: (inode, mtime_ns, sha256)} по .claude/agents проекта."""
    out = {}
    for path in sorted((proj / ".claude" / "agents").glob("*.md")):
        st = path.stat()
        out[path.name] = (st.st_ino, st.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    return out


def archived_cards(root: Path) -> set:
    """Карточки в архивах раскладки где угодно под стендом (кроме самого проекта)."""
    proj = root / "proj"
    return {str(p.relative_to(root)) for p in root.rglob("*.md")
            if "/files/.claude/agents/" in str(p) and proj not in p.parents}


def check_repeat(pkg: Path, root: Path, env: dict, verdict) -> None:
    """Н1: вторая раскладка той же версии не переписывает и не архивирует карточки."""
    proj = root / "proj"
    before, archived_before = cards_snapshot(proj), archived_cards(root)
    verdict("4) карточки проекта разобраны до повтора (не пусто)", bool(before), f"{sorted(before) or '—'}")
    time.sleep(1.1)   # mtime_ns повтора обязан отличаться, если файл переписан
    r = lay_out(pkg, root, env)
    verdict("4) повтор раскладки код 0", r.returncode == 0,
            f"код {r.returncode}" + ("" if r.returncode == 0 else f"; хвост {(r.stdout + r.stderr)[-300:]!r}"))
    after = cards_snapshot(proj)
    moved = sorted(n for n in before if before[n] != after.get(n))
    verdict("4) повтор: inode/mtime/sha256 каждой карточки прежние", not moved,
            f"переписаны {len(moved)}: {moved[:4]}" if moved else f"карточек {len(before)}, все прежние")
    new_archived = sorted(archived_cards(root) - archived_before)
    verdict("4) повтор: ни одна карточка не легла в архив раскладки", not new_archived,
            f"в архиве {len(new_archived)}: {new_archived[:3]}" if new_archived else "архив карточек +0")


def check_seal_order(verdict) -> None:
    """Н2: стенд со СТАРЫМ эталоном; раскладка печатает новый, карточки собраны по нему."""
    root = Path(tempfile.mkdtemp(prefix="harness-run-layout-seal-"))
    try:
        pkg = make_stand(root)
        etalon, seal_log = root / "эталон", root / "печать.log"
        shutil.copytree(pkg / "harness" / "self", etalon,
                        ignore=shutil.ignore_patterns("instance.yaml", "MANIFEST.sha256"))
        core = json.loads((etalon / "core.json").read_text(encoding="utf-8"))
        core["версия"] = "0.старый-эталон-стенда"
        (etalon / "core.json").write_text(json.dumps(core, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (root / "bin" / "sudo").write_text(SEAL_SUDO, encoding="utf-8")
        env = stand_env(root)
        env.update(SELF_ETALON_DIR=str(etalon), STAND_ETALON=str(etalon), STAND_SEAL_LOG=str(seal_log),
                   HARNESS_LAYOUT_SELF_SEAL="1")
        r = lay_out(pkg, root, env)
        verdict("5) раскладка со старым эталоном код 0", r.returncode == 0,
                f"код {r.returncode}" + ("" if r.returncode == 0 else f"; хвост {(r.stdout + r.stderr)[-300:]!r}"))
        sealed = seal_log.read_text(encoding="utf-8").split() if seal_log.is_file() else []
        verdict("5) раскладка позвала печать эталона (sudo -n …/self-seal <выкладка>)", bool(sealed),
                f"позвана: {sealed}" if sealed else
                "печать не позвана — при HARNESS_SELFTEST=1 её открывает только дверь HARNESS_LAYOUT_SELF_SEAL=1")
        fresh = (etalon / "core.json").read_bytes() == (pkg / "harness" / "self" / "core.json").read_bytes()
        verdict("5) эталон стенда — новый (core.json равен пакетному)", fresh,
                "новый" if fresh else "остался старый: печати не было")
        proj = root / "proj"
        env_proj = dict(env, SELF_TREE_DIR=str(proj), LOG_DIR=str(root / "log"))
        v = subprocess.run([sys.executable, str(proj / "scripts" / "self-prompt.py"), "--карточки", "--проверить"],
                           cwd=proj, env=env_proj, capture_output=True, text=True, timeout=120)
        verdict("5) --карточки --проверить относительно НОВОГО эталона → код 0", v.returncode == 0 and fresh,
                f"код {v.returncode}, эталон {'новый' if fresh else 'старый — судить не о чем'}; "
                f"{v.stderr.strip()[:300]}")
    finally:
        subprocess.run(["chmod", "-R", "u+w", str(root)], stderr=subprocess.DEVNULL)
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="harness-run-layout-cards-"))
    failed = 0

    def verdict(name: str, ok: bool, detail: str) -> None:
        nonlocal failed
        failed += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}", flush=True)

    try:
        pkg = make_stand(root)
        env = stand_env(root)
        proj = root / "proj"
        r = lay_out(pkg, root, env)
        verdict("1) раскладка код 0", r.returncode == 0,
                f"код {r.returncode}" + ("" if r.returncode == 0 else f"; хвост {(r.stdout + r.stderr)[-300:]!r}"))
        # Чистая установка без эталона: сборщик отвечает 3 (слой 1 из дерева) — это успех
        # пересборки, не «ВНИМАНИЕ»: иначе владелец читает ложную тревогу в журнале установки.
        said = r.stdout + r.stderr
        verdict("1б) раскладка сказала «карточки помощников пересобраны», без «ВНИМАНИЕ»",
                "карточки помощников пересобраны" in said and "ВНИМАНИЕ: карточки помощников" not in said,
                "сказано" if "карточки помощников пересобраны" in said else "строки о карточках нет или тревога")

        stub_log = root / "заглушки.log"
        seal_calls = [l for l in (stub_log.read_text(encoding="utf-8").splitlines() if stub_log.is_file() else [])
                      if "self-seal" in l]
        verdict("1в) самотест без двери HARNESS_LAYOUT_SELF_SEAL эталон не печатает (sudo …/self-seal не звался)",
                not seal_calls, f"звался: {seal_calls[:2]}" if seal_calls else "не звался")

        policy_path = proj / "harness" / "config" / "launch-policy.yaml"
        policy = yaml.safe_load(policy_path.read_text(encoding="utf-8")) if policy_path.is_file() else {}
        types = list((policy or {}).get("карточки") or {})
        verdict("типы карточек разобраны из политики проекта", bool(types), f"типы {types or '—'}")

        env_proj = dict(env, SELF_TREE_DIR=str(proj), LOG_DIR=str(root / "log"))
        v = subprocess.run([sys.executable, str(proj / "scripts" / "self-prompt.py"), "--карточки", "--проверить"],
                           cwd=proj, env=env_proj, capture_output=True, text=True, timeout=120)
        verdict("2) --карточки --проверить → код 0", v.returncode == 0,
                f"код {v.returncode}; {v.stderr.strip()[:400]}")

        s = subprocess.run([sys.executable, "-c", CARD_STATE, str(proj / "scripts" / "hooks"), *types],
                           cwd=proj, env=env_proj, capture_output=True, text=True, timeout=120)
        try:
            states = json.loads(s.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            states = {}
            verdict("card_state прочитан", False, f"код {s.returncode}; {s.stderr.strip()[-300:]}")
        for kind in types:
            state = states.get(kind)
            verdict(f"3) card_state({kind}) без отказа", state == [True, ""], f"{state}")
        check_repeat(pkg, root, env, verdict)
    finally:
        subprocess.run(["chmod", "-R", "u+w", str(root)], stderr=subprocess.DEVNULL)
        shutil.rmtree(root, ignore_errors=True)
    check_seal_order(verdict)
    print(f"КАРТОЧКИ-РАСКЛАДКИ: упало {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
