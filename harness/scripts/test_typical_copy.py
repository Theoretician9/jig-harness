#!/usr/bin/env python3
"""Проба замка «типовая чистая копия» (check-typical-copy.py): стенд видит то, чего не видит наша машина.

Два стенда одного выпуска пакета:
  чистый      — прежний и новый выпуск одинаковы → новых красных нет, код 0;
  мутация     — в новом выпуске изоляция tmux судит копию как НАШУ машину
                (dev_install() всегда «да», как при HARNESS_DEV_GATES=1): свой .sh
                копии краснит — код 1, в выводе «новая красная «изоляция tmux»» и причина.
Какая поломка оставит пробу зелёной: стенд, который берёт наше окружение (HARNESS_DEV_GATES
из живого конфига) или не заводит свой файл копии, — на чистом стенде мутация не видна.

Прогон (только фоном, минуты: четыре раскладки):
    python3 scripts/test_typical_copy.py
Последняя строка: `ТИПОВАЯ КОПИЯ: путей N, неудач F`.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import config  # noqa: E402

JUDGE = HERE / "check-typical-copy.py"
MUTATED = "harness/scripts/check-tmux-isolation.py"
paths = failures = 0


def report(ok: bool, text: str, detail: str = "") -> None:
    global paths, failures
    paths += 1
    failures += not ok
    print(("ок    · " if ok else "КРАСН · ") + text + ("" if ok else f": {detail}"))


def judged(old: Path, new: Path) -> tuple:
    done = subprocess.run([sys.executable, str(JUDGE), "--старое", str(old), "--новое", str(new)],
                          capture_output=True, text=True, timeout=3600, stdin=subprocess.DEVNULL)
    return done.returncode, (done.stdout + done.stderr).strip()


def main() -> int:
    package = config.katalog_paketa()
    work = Path(tempfile.mkdtemp(prefix="proba-typical-copy-trees-"))
    try:
        clean = work / "выпуск"
        shutil.copytree(package, clean, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        rc, said = judged(clean, clean)
        report(rc == 0 and "новых красных нет" in said, "чистый выпуск → новых красных нет, код 0",
               f"код {rc}: {said[-300:]}")
        mutated = work / "мутация"
        shutil.copytree(clean, mutated)
        target = mutated / MUTATED
        text = target.read_text(encoding="utf-8")
        sick = text.replace('    return config.конфиг(config.путь_harness_conf()).get("HARNESS_DEV_GATES") == "1"\n',
                            "    return True\n", 1)
        if sick == text:
            report(False, "мутация «наша машина» не встала: строки dev_install() нет", MUTATED)
        else:
            target.write_text(sick, encoding="utf-8")
            rc, said = judged(clean, mutated)
            report(rc == 1 and "новая красная «изоляция tmux»" in said and "typical-preview.sh" in said,
                   "мутация «судит как нашу машину» → код 1, «изоляция tmux» и свой файл копии в причине",
                   f"код {rc}: {said[-300:]}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"ТИПОВАЯ КОПИЯ: путей {paths}, неудач {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
