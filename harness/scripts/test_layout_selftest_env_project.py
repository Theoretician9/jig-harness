#!/usr/bin/env python3
"""Проба: самотест раскладчика не пишет в PROJECT_DIR из окружения вызывающего.

Окружение старше конфига (scripts/lib/config.sh): вложенная раскладка стенда, которой
досталось PROJECT_DIR вызывающего, ложится не в стенд, а в его проект. Улика — замок 6г
15.11.2026: самопроверка копии с PROJECT_DIR в окружении прогнала «самотест раскладчика»,
и пакетный process-library.json затёр запись копии typical-backup (2 вхождения → 0).

Условие создаёт проба: проект «копии» со своей записью в библиотеке процессов и
PROJECT_DIR, HEARTBEAT_DIR, PROJECT_NAME окружения, указывающие на него; свой TMPDIR и своя копия harness/ пакета.
Самотест идёт до конца первой раскладки стенда (опись в <стенд>/proj) и обрывается:
весь самотест — полчаса, а вред делает уже первая. Судится: проект копии побайтно
прежний, и первая раскладка легла в стенд — без неё проба ничего не судила.
Итог: «САМОТЕСТ-ОКРУЖЕНИЕ: …», код 0 — проект копии не тронут.
"""
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIMIT_SEC = 600
OWN_RECORD = "typical-backup"


def package_harness(work: Path) -> Path:
    """Копия пакета: самотест судит соседние файлы раскладчика ($HERE), одинокая
    копия раскладчика падала бы раньше стенда. LAYOUT_SCRIPT — дверь мутированной копии."""
    sys.path.insert(0, str(HERE / "lib"))
    import config
    # Пакет целиком, без .git: соседи harness/ (UNIFIED с главами для сборки скилов) раскладка
    # кладёт в проект, а ссылку наружу отвергает.
    shutil.copytree(config.katalog_paketa(), work / "пакет", ignore=shutil.ignore_patterns(".git", "__pycache__"))
    tree = work / "пакет" / "harness"
    if os.environ.get("LAYOUT_SCRIPT"):
        shutil.copyfile(os.environ["LAYOUT_SCRIPT"], tree / "razlozhit-harnes.sh")
    return tree


def snapshot(root: Path) -> dict:
    """Путь → sha256 каждого файла дерева (каталоги — пустой строкой)."""
    rows = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        rows[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
    return rows


def make_copy(project: Path) -> None:
    config = project / "harness" / "config"
    config.mkdir(parents=True)
    library = {"процессы": [{"id": OWN_RECORD, "название": "Копия базы продукта", "класс": "фоновый",
                             "запуск": "крон", "статус": "работает", "файлы": ["scripts/typical-backup.sh"]}]}
    (config / "process-library.json").write_text(json.dumps(library, ensure_ascii=False, indent=2) + "\n",
                                                 encoding="utf-8")


def first_layout_done(tmp: Path) -> bool:
    return any(tmp.glob("proba-razlozhit-*/proj/.layout-manifest"))


def run_until_first_layout(script: Path, env: dict, tmp: Path, copy: Path, before: dict) -> tuple:
    """Самотест до описи первой раскладки стенда (или до вреда копии) → (дошла ли раскладка
    до стенда, хвост вывода самотеста — назвать, почему не дошла)."""
    out = tmp / "самотест.out"
    with open(out, "wb") as sink:
        proc = subprocess.Popen(["bash", str(script), "--selftest"], env=env, stdin=subprocess.DEVNULL,
                                stdout=sink, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + LIMIT_SEC
    done = False
    try:
        while time.monotonic() < deadline and proc.poll() is None:
            done = first_layout_done(tmp)
            if done or snapshot(copy) != before:
                break
            time.sleep(1)
    finally:
        # TERM: ловушка самотеста убирает стенд; KILL — если не ушёл сам.
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    tail = " | ".join(out.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:])
    return done, f"код {proc.returncode}: {tail}"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="dc-layout-env-") as name:
        tmp = Path(name)
        script = package_harness(tmp) / "razlozhit-harnes.sh"
        if not script.is_file():
            print(f"САМОТЕСТ-ОКРУЖЕНИЕ: нет раскладчика {script}")
            return 1
        copy = tmp / "копия"
        make_copy(copy)
        before = snapshot(copy)
        env = {**os.environ, "TMPDIR": str(tmp), "PROJECT_DIR": str(copy),
               "HEARTBEAT_DIR": str(tmp / "метки-копии"), "PROJECT_NAME": "копия-вызывающего"}
        reached, trace = run_until_first_layout(script, env, tmp, copy, before)
        after = snapshot(copy)
        bad = []
        if after != before:
            changed = sorted(k for k in set(after) | set(before) if after.get(k) != before.get(k))
            lib = copy / "harness" / "config" / "process-library.json"
            own = OWN_RECORD in lib.read_text(encoding="utf-8") if lib.is_file() else False
            bad.append(f"проект копии из PROJECT_DIR окружения изменён ({len(changed)} путей, "
                       f"запись {OWN_RECORD} {'на месте' if own else 'затёрта'}): {', '.join(changed[:3])}")
        if (tmp / "метки-копии").exists():
            bad.append("HEARTBEAT_DIR вызывающего дошёл до стенда")
        if not reached and not bad:
            bad.append(f"первая раскладка стенда не дошла до описи (предел {LIMIT_SEC} с) — проба ничего "
                       f"не судила; самотест {trace}")
        for line in bad:
            print(f"  ПЛОХО {line}")
        print(f"САМОТЕСТ-ОКРУЖЕНИЕ: {'КРАСНЫЙ' if bad else 'проект копии не тронут, раскладка легла в стенд'}")
        return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
