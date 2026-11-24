#!/usr/bin/env python3
"""Проба раскладчика: свои записи библиотеки процессов не теряются, когда слой установки закрыт.

Находка копии сети 15.11.2026: 0.13.0 откатился на «библиотеке процессов». Один путь
воспроизведён: слой установки (PROCESS_LIBRARY_LOCAL, /etc/harness/agent/…) копия
записать не может — шаг переноса печатал «ВНИМАНИЕ» и шёл дальше (`|| true`), пакетный
process-library.json ложился поверх проектного, своя запись копии пропадала, и
самопроверка краснела «НАРУШЕНИЕ без записи: crontab :: typical-backup».

Стенд — тот же, что у замка 6г (check-typical-copy.py: свой git пакета и проекта, свой
crontab, окружение с нуля): прежний выпуск, копия заводит свой процесс typical-backup,
каталог слоя закрывается на запись, новый выпуск раскладывается.

  К1 слой закрыт → раскладчик отказывает кодом 3 («не начата»), причина называет запись
     typical-backup и путь слоя; update.py берёт её в слова владельцу;
  К2 при отказе проект не тронут: process-library.json и опись побайтно прежние;
  К3 откат (--откат) тем же отказом не останавливается: возврат прежней версии важнее;
  К4 контроль — слой открыт: раскладка проходит, запись в слое, самопроверка
     «библиотека процессов» без «без записи … typical-backup».
Какая поломка оставит пробу зелёной? Отказ после укладки — краснеет К2; отказ без имени
записи или пути — К1; отказ и при откате — К3; отказ всегда — К4.

Дверь мутаций: LAYOUT_SCRIPT — раскладчик, который кладётся в копию пакета стенда.
Прогон (фоном, минуты: три раскладки): python3 scripts/test_layout_carry_own_refusal.py
Последняя строка: `СВОИ ЗАПИСИ ПРИ ЗАКРЫТОМ СЛОЕ: путей N, неудач F`.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import config  # noqa: E402

OWN_ID = "typical-backup"
paths = failures = 0


def report(ok: bool, text: str, detail: str = "") -> None:
    global paths, failures
    paths += 1
    failures += not ok
    print(("ок    · " if ok else "КРАСН · ") + text + ("" if ok else f": {detail}"))


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package_tree(work: Path) -> Path:
    """Копия пакета; раскладчик в ней — из двери LAYOUT_SCRIPT, если она задана."""
    tree = work / "выпуск"
    shutil.copytree(config.katalog_paketa(), tree, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    if os.environ.get("LAYOUT_SCRIPT"):
        shutil.copyfile(os.environ["LAYOUT_SCRIPT"], tree / "harness" / "razlozhit-harnes.sh")
    return tree


def next_release(stand, tree: Path, label: str) -> None:
    """Новый коммит пакета копии — без раскладки: её зовёт сама проба."""
    shutil.copytree(tree, stand.package, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    typical.git(stand.package, "add", "-A")
    typical.git(stand.package, "commit", "-q", "--allow-empty", "-m", label)


def lay(stand, *extra) -> subprocess.CompletedProcess:
    return typical.run(["bash", str(stand.package / "harness" / "razlozhit-harnes.sh"), *extra], stand.env)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="proba-carry-own-"))
    closed = work / "закрыто"
    try:
        tree = package_tree(work)
        stand = typical.Stand(work / "стенд", tree / "harness" / "config" / "harness.conf")
        closed.mkdir()
        layer = closed / "agent" / "process-library.local.json"
        stand.env["HARNESS_PROCESS_LIBRARY_LOCAL"] = str(layer)
        stand.release(tree, "прежний выпуск")
        stand.own_process()
        library = stand.project / "harness" / "config" / "process-library.json"
        manifest = stand.project / ".layout-manifest"
        before = (library.read_bytes(), manifest.read_bytes())
        next_release(stand, tree, "новый выпуск")
        closed.chmod(0o555)

        done = lay(stand)
        said = (done.stdout + done.stderr).strip()
        reason = update._причина_отказа_раскладки(done.stderr)
        report(done.returncode == 3, "К1 слой закрыт → раскладчик отказывает кодом 3 (не начата)",
               f"код {done.returncode}: {said[-400:]}")
        report(OWN_ID in reason and str(closed) in reason,
               f"К1 причина для владельца называет {OWN_ID} и путь слоя", f"причина {reason!r}")
        report((library.read_bytes(), manifest.read_bytes()) == before,
               "К2 при отказе process-library.json и опись проекта побайтно прежние",
               "проект тронут: " + ("библиотека другая " if library.read_bytes() != before[0] else "")
               + ("опись другая" if manifest.read_bytes() != before[1] else ""))

        back = lay(stand, "--откат", str(work / "журнала-нет"))
        refused = [line for line in back.stderr.splitlines()
                   if line.startswith(update.МАРКЕР_РАСКЛАДКА_НЕ_НАЧАТА) and OWN_ID in line]
        report(not refused, "К3 откат не останавливается отказом про слой", f"отказ {refused!r}")

        closed.chmod(0o755)
        done = lay(stand)
        report(done.returncode == 0, "К4 слой открыт → раскладка проходит",
               f"код {done.returncode}: {(done.stdout + done.stderr).strip()[-400:]}")
        in_layer = json.loads(layer.read_text(encoding="utf-8"))["процессы"] if layer.is_file() else []
        report(any(r.get("id") == OWN_ID for r in in_layer), f"К4 {OWN_ID} — в слое установки",
               f"слой {[r.get('id') for r in in_layer]}")
        judged = stand.self_check(["библиотека процессов"])
        report("=== итог:" in judged and f"без записи: crontab :: {OWN_ID}" not in judged,
               f"К4 самопроверка без «без записи: crontab :: {OWN_ID}»", judged.strip()[-400:])
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError) as trouble:
        report(False, "стенд собран", f"{type(trouble).__name__}: {trouble}")
    finally:
        if closed.is_dir():
            closed.chmod(0o755)
        shutil.rmtree(work, ignore_errors=True)
    print(f"СВОИ ЗАПИСИ ПРИ ЗАКРЫТОМ СЛОЕ: путей {paths}, неудач {failures}")
    return 1 if failures else 0


typical = load(HERE / "check-typical-copy.py", "carry_probe_typical")
update = load(HERE / "update.py", "carry_probe_update")

if __name__ == "__main__":
    sys.exit(main())
