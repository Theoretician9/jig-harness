#!/usr/bin/env python3
"""Проба: причины красных ворот лежат файлом в LOG_DIR и переживают откат обновления.

    python3 scripts/test_gates_red_reasons.py

Копия сети 16.11.2026, четвёртый откат: её update.py старый (11.11, без перезапуска
новым обновлятором), вывод ворот он держит в памяти и называет владельцу только имена
красных — «библиотека процессов, секреты» без причины. Ворота новой версии идут и под
старым обновлятором, поэтому причину сохраняют они сами: $LOG_DIR/ворота-причины-красных.txt —
по каждой итоговой красной имя, код и первые строки вывода. Файл переписывается каждым
прогоном (без красных — «красных нет»), откат его не трогает: LOG_DIR не раскладывается.

Договор:
  * итоговая красная (сразу и «красная и соло») — в файле с первой строкой причины;
  * зелёная и флейк (соло зелёная) в файл не идут;
  * прогон без красных переписывает файл строкой «красных нет» — прежние причины не врут;
  * gates.sh сохраняет файл перед каждой строкой «=== итог:».

Стенд — фиктивные проверки через вход обёртки; свой LOG_DIR, полные ворота не идут.
Двери для мутаций: GATES_CHECK_LIB (путь обёртки), GATES_SH (путь gates.sh).
Код возврата: 0 — все случаи зелёные, 1 — есть красный.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = os.environ.get("GATES_CHECK_LIB") or str(ROOT / "scripts/lib/gates_check.sh")
GATES_SH = os.environ.get("GATES_SH") or str(ROOT / "scripts/gates.sh")
REASONS = "ворота-причины-красных.txt"

# $1 — LOG_DIR стенда, $2 — обёртка, $3 — GATES_RECHECK, $4 — GATES_JOBS, $5 — 1: с красными.
STAND = r"""
set -uo pipefail
export LC_ALL=C.UTF-8
LOG_DIR="$1"
. "$2"
GATES_RECHECK="$3"
GATES_JOBS="$4"
GATES_REDS_FILE=$(mktemp)
rm -f "$1/был"
проверка "зелёная" bash -c 'echo всё хорошо'
if [ "$5" = 1 ]; then
    проверка "флейк" bash -c 'if [ -f "$0/был" ]; then echo второй раз прошла; else : > "$0/был"; echo упала в общем; exit 1; fi' "$1"
    проверка "секреты" bash -c 'echo "ПРИЧИНА app/settings.py:2 пароль в адресе базы"; echo вторая строка; exit 3'
fi
gates_wait_all
gates_recheck_reds
gates_save_reds
printf 'СЧЁТ %s %s\n' "$GREEN" "$RED"
"""

failed: list[str] = []
passed: list[str] = []


def case(name: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(name)
    print(f"  {'ок' if ok else 'ПЛОХО'} · {name}" + ("" if ok else f" — {detail}"))


def run_stand(log_dir: Path, recheck: str, jobs: str, with_reds: str = "1") -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HARNESS_LOG_DIR", "GATES_"))}
    proc = subprocess.run(["bash", "-c", STAND, "stand", str(log_dir), LIB, recheck, jobs, with_reds],
                          capture_output=True, text=True, timeout=120, env=env)
    return proc.stdout + proc.stderr


def reasons(log_dir: Path) -> str:
    path = log_dir / REASONS
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="harness-run-red-reasons-") as tmp:
        for recheck, jobs in (("0", "1"), ("1", "1"), ("1", "3")):
            mode = f"перепрогон {recheck}, работ {jobs}"
            log_dir = Path(tmp) / f"r{recheck}j{jobs}"
            log_dir.mkdir()
            out = run_stand(log_dir, recheck, jobs)
            text = reasons(log_dir)
            case(f"{mode}: файл причин есть", bool(text), f"файла {log_dir / REASONS} нет; вывод: {out[-300:]}")
            case(f"{mode}: красная названа с кодом и первой строкой причины",
                 bool(re.search(r"секреты \(код 3\)", text)) and "ПРИЧИНА app/settings.py:2" in text,
                 text[-300:] or "пусто")
            flake_red = recheck == "0"
            case(f"{mode}: зелёная не в файле, флейк — только как красная без перепрогона",
                 "зелёная" not in text and (("флейк" in text) == flake_red), text[-300:] or "пусто")
            out = run_stand(log_dir, recheck, jobs, with_reds="0")
            text = reasons(log_dir)
            case(f"{mode}: прогон без красных переписал файл «красных нет»",
                 "красных нет" in text and "секреты" not in text, text[-300:] or "пусто")
    gates = Path(GATES_SH).read_text(encoding="utf-8")
    totals = len(re.findall(r'^\s*echo "=== итог:', gates, re.M))
    lines = gates.splitlines()
    saved = sum(1 for i, line in enumerate(lines) if re.match(r'^\s*echo "=== итог:', line)
                and any(re.match(r"^\s*gates_save_reds\b", p) for p in lines[max(0, i - 4):i]))
    case("gates.sh: перед каждой строкой «=== итог:» — gates_save_reds", totals > 0 and saved == totals,
         f"итогов {totals}, с сохранением {saved}")
    print(f"ПРИЧИНЫ КРАСНЫХ ВОРОТ: путей {len(passed) + len(failed)}, неудач {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
