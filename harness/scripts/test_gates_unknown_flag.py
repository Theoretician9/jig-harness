#!/usr/bin/env python3
"""Проба: незнакомый ключ ворот — код 2 и справка, ни одной проверки.

    python3 scripts/test_gates_unknown_flag.py

Почему. 19.11.2026 двое помощников позвали `bash scripts/gates.sh --help`: ворота узнают
ключи сравнениями `[ "${1:-}" = "--…" ]`, а незнакомый ключ молча шёл дальше обычным
прогоном — все ворота, машина занята на час.

Стенд: копия НАСТОЯЩЕГО gates.sh в свой каталог, рядом подставные scripts/lib/config.sh,
stand_sweep.sh и gates_check.sh: `проверка` только пишет имя в файл меток, остальные
функции обёртки — пустые. heavy-slot.sh на стенде нет — пул не зовётся. Ни одна настоящая
проверка не идёт, сколько бы ворота ни звали.

Известные ключи — РАЗБОРОМ gates.sh (сравнения с "${1:-}"); пустой разбор — красный.
Случаи:
  * --help, -h, --vse и «всё» (опечатки), «--только x --help» — код 2, справка называет
    все известные ключи, меток проверок нет;
  * каждый известный ключ — не отказ (на стенде прогон может упасть, но не справкой);
  * без ключа и с «--только x» — проверки идут (стенд живой: без этого случая молчание
    подставной обёртки сошло бы за отказ ворот).

Дверь для мутаций: GATES_SH — путь ворот (мутировать КОПИЮ).
Код возврата: 0 — зелёная, 1 — красная.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATES = Path(os.environ.get("GATES_SH") or HERE / "gates.sh")
UNKNOWN = (("--help",), ("-h",), ("--vse",), ("всё",), ("--только", "x", "--help"))
REFUSAL = "неизвестный ключ"
RUN_LIMIT_SEC = 300

CONFIG_STUB = 'konf_zagruzit() { LOG_DIR="$STAND_LOG"; }\n'
SWEEP_STUB = "sweep_dead_stands() { :; }\n"
CHECK_STUB = """GREEN=0; RED=0; YELLOW=0
проверка() { printf '%s\\n' "$1" >> "$STAND_MARKS"; }
gates_join_or_lead() { :; }
gates_wait_all() { :; }
gates_recheck_reds() { :; }
gates_save_reds() { :; }
gates_flakes_tail() { :; }
gates_dev_skipped_line() { :; }
"""

checked = failed = 0


def check(name: str, expected, actual) -> None:
    global checked, failed
    checked += 1
    if expected == actual:
        print(f"  ок    {name}")
    else:
        failed += 1
        print(f"  ПЛОХО {name}: ждали «{expected}», вышло «{actual}»")


def known_flags(text: str) -> list[str]:
    return sorted(set(re.findall(r'"\$\{1:-\}" = "(-[^"]+)"', text)))


class Stand:
    def __init__(self, base: Path):
        self.base = base
        scripts = base / "scripts"
        (scripts / "lib").mkdir(parents=True)
        shutil.copy2(GATES, scripts / "gates.sh")
        for name, text in (("config.sh", CONFIG_STUB), ("stand_sweep.sh", SWEEP_STUB),
                           ("gates_check.sh", CHECK_STUB)):
            (scripts / "lib" / name).write_text(text, encoding="utf-8")
        self.logs = base / "log"
        self.logs.mkdir()
        self.marks = base / "marks"

    def run(self, *args: str) -> tuple[int, str, list[str]]:
        """(код, весь вывод, имена позванных проверок)."""
        self.marks.unlink(missing_ok=True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GATES_", "HEAVY_SLOT", "HARNESS_RELEASE"))}
        env.update(STAND_LOG=str(self.logs), STAND_MARKS=str(self.marks), LOG_DIR=str(self.logs))
        proc = subprocess.run(["bash", str(self.base / "scripts/gates.sh"), *args],
                              capture_output=True, text=True, env=env, cwd=self.base,
                              timeout=RUN_LIMIT_SEC, start_new_session=True)
        marks = self.marks.read_text(encoding="utf-8").split() if self.marks.exists() else []
        return proc.returncode, proc.stdout + proc.stderr, marks


def main() -> int:
    if not GATES.is_file():
        print(f"ПЛОХО: нет {GATES} — проверять нечего")
        return 1
    known = known_flags(GATES.read_text(encoding="utf-8"))
    print(f"известные ключи разбором gates.sh: {' '.join(known) or 'НЕТ'}")
    check("разбор нашёл ключи и среди них --только", True, "--только" in known)
    with tempfile.TemporaryDirectory(prefix="proba-gates-flag-") as raw:
        stand = Stand(Path(raw))
        code, _, marks = stand.run()
        check("без ключа: проверки идут (стенд живой)", True, bool(marks))
        code, _, marks = stand.run("--только", "x")
        check("--только x: не отказ и проверки идут", (True, True), (code != 2, bool(marks)))
        # Известный ключ не отказ: на стенде прогон может упасть по-своему, но не справкой.
        for flag in known:
            code, out, _ = stand.run(*((flag, "x") if flag == "--только" else (flag,)))
            check(f"{flag}: известный ключ — не отказ", (False, True),
                  (REFUSAL in out, code != 2))
        for args in UNKNOWN:
            name = " ".join(args)
            check(f"{name}: ключ не из известных", False, args[-1] in known)
            code, out, marks = stand.run(*args)
            check(f"{name}: код 2", 2, code)
            check(f"{name}: ни одной проверки", [], marks[:3])
            check(f"{name}: справка называет все известные ключи", [],
                  [f for f in known if f not in out.split(REFUSAL, 1)[-1]])
    print(f"итог: {'зелёный' if not failed else 'КРАСНЫЙ'} ({checked} проверок, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
