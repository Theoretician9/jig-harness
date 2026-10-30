#!/usr/bin/env python3
"""Проба: обёртка проверки ворот пишет секунды каждой проверки, читалка даёт топ медленных.

    python3 scripts/test_gates_timing.py

Стенд — три фиктивные проверки через вход обёртки (scripts/lib/gates_check.sh),
полные ворота не запускаются. Журнал — во временном LOG_DIR, боевой не трогается.
Дверь для мутаций: GATES_CHECK_LIB (путь обёртки), GATES_TIMING_READER (путь читалки),
GATES_SH (путь gates.sh для случая «самотест ворот»).
Код возврата: 0 — все случаи зелёные, 1 — есть красный.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = os.environ.get("GATES_CHECK_LIB") or str(ROOT / "scripts/lib/gates_check.sh")
SLOWEST = os.environ.get("GATES_TIMING_READER") or str(ROOT / "scripts/gates-timing.py")
GATES_SH = os.environ.get("GATES_SH") or str(ROOT / "scripts/gates.sh")
JOURNAL = "gates-timing.jsonl"

# Длительность долгой проверки выбрана выше шума запуска процесса (десятки мс),
# но так, чтобы проба шла секунды, а не минуты.
LONG_S = 1.2

STAND = r"""
set -uo pipefail
export LC_ALL=C.UTF-8
LOG_DIR="$1"
. "$2"
проверка "долгая"   bash -c 'sleep 1.2; echo готово'
проверка "падающая" bash -c 'echo сломалось; exit 3'
проверка "жёлтая"   bash -c 'sleep 0.2; echo находка; exit 77'
printf 'СЧЁТ %s %s %s\n' "$GREEN" "$RED" "$YELLOW"
"""

failed: list[str] = []
passed: list[str] = []


def case(name: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(name)
    print(f"  {'ок' if ok else 'ПЛОХО'} · {name}" + ("" if ok else f" — {detail}"))


def rows_of(log_dir: Path) -> list[dict]:
    path = log_dir / JOURNAL
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_stand(log_dir: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_LOG_DIR")}
    return subprocess.run(["bash", "-c", STAND, "stand", str(log_dir), LIB],
                          capture_output=True, text=True, timeout=60, env=env)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="gates-timing-") as tmp:
        log_dir = Path(tmp)
        proc = run_stand(log_dir)
        out = proc.stdout

        # Поведение ворот не меняется: счёт, строки вывода.
        case("счёт зелёных/красных/жёлтых прежний", "СЧЁТ 1 1 1" in out, out[-400:] + proc.stderr[-400:])
        case("вывод: зелёная одной строкой", "ок · долгая — готово" in out, out[-400:])
        case("вывод: красная с кодом", "ПЛОХО · падающая (код 3)" in out, out[-400:])
        case("вывод: жёлтая отдельно", "жёлтый · жёлтая" in out, out[-400:])

        rows = [r for r in rows_of(log_dir) if r.get("event") == "gate.check"]
        data = [r.get("data") or {} for r in rows]
        names = [d.get("name") for d in data]
        case("журнал: по записи на каждую проверку, по порядку",
             names == ["долгая", "падающая", "жёлтая"], f"имена в журнале: {names}")

        by_name = {d.get("name"): d for d in data}
        long_s = by_name.get("долгая", {}).get("secs")
        fail_s = by_name.get("падающая", {}).get("secs")
        case("секунды долгой — число не меньше её сна",
             isinstance(long_s, (int, float)) and LONG_S <= long_s < LONG_S + 5,
             f"secs долгой: {long_s!r}")
        case("секунды быстрой меньше долгой",
             isinstance(fail_s, (int, float)) and isinstance(long_s, (int, float)) and 0 <= fail_s < long_s,
             f"secs падающей {fail_s!r}, долгой {long_s!r}")
        case("код возврата каждой проверки",
             [d.get("rc") for d in data] == [0, 3, 77], f"rc: {[d.get('rc') for d in data]}")
        case("исход: красная — refused, остальные — ok",
             [r.get("outcome") for r in rows] == ["ok", "refused", "ok"],
             f"исходы: {[r.get('outcome') for r in rows]}")
        runs = {d.get("run") for d in data}
        case("одна метка прогона на все проверки прогона",
             len(runs) == 1 and all(runs), f"метки прогона: {runs}")

        # Второй прогон стенда — читалка берёт ПОСЛЕДНИЙ прогон, а не смесь.
        run_stand(log_dir)
        top = subprocess.run([sys.executable, SLOWEST, "--top", "2", "--json"],
                             capture_output=True, text=True, timeout=60,
                             env={**os.environ, "HARNESS_LOG_DIR": str(log_dir)})
        try:
            answer = json.loads(top.stdout)
        except ValueError:
            answer = {}
        items = answer.get("items") or []
        case("читалка: топ-2, долгая первой",
             top.returncode == 0 and [i.get("name") for i in items][:1] == ["долгая"] and len(items) == 2,
             f"код {top.returncode}, ответ {top.stdout[-300:]}{top.stderr[-300:]}")
        case("читалка: последний прогон, а не оба",
             answer.get("runs") == 1 and [i.get("seen") for i in items] == [1, 1],
             f"прогонов {answer.get('runs')!r}, seen {[i.get('seen') for i in items]}")
        avg = subprocess.run([sys.executable, SLOWEST, "--runs", "2", "--json"],
                             capture_output=True, text=True, timeout=60,
                             env={**os.environ, "HARNESS_LOG_DIR": str(log_dir)})
        try:
            avg_items = json.loads(avg.stdout).get("items") or []
        except ValueError:
            avg_items = []
        case("читалка: --runs 2 различает два прогона стенда",
             [i.get("seen") for i in avg_items][:1] == [2], avg.stdout[-300:] + avg.stderr[-300:])
        human = subprocess.run([sys.executable, SLOWEST, "--top", "1"],
                               capture_output=True, text=True, timeout=60,
                               env={**os.environ, "HARNESS_LOG_DIR": str(log_dir)})
        case("читалка: человеческий вывод называет проверку и секунды",
             human.returncode == 0 and "долгая" in human.stdout and "падающая" not in human.stdout,
             human.stdout[-300:] + human.stderr[-300:])

    with tempfile.TemporaryDirectory(prefix="gates-timing-empty-") as tmp:
        empty = subprocess.run([sys.executable, SLOWEST], capture_output=True, text=True, timeout=60,
                               env={**os.environ, "HARNESS_LOG_DIR": tmp})
        case("читалка: пустой журнал — ответ «нет данных», код 0",
             empty.returncode == 0 and "нет" in empty.stdout, empty.stdout + empty.stderr)

    # Ворота подключают ТУ ЖЕ обёртку: их собственный самотест судит запись секунд.
    selftest = subprocess.run(["bash", GATES_SH, "--selftest"], capture_output=True, text=True, timeout=120)
    case("gates.sh --selftest зелёный и судит журнал секунд",
         selftest.returncode == 0 and "секунд" in selftest.stdout,
         selftest.stdout[-400:] + selftest.stderr[-400:])

    total = len(passed) + len(failed)
    print(f"{'ПРОЙДЕНО' if not failed else 'ПРОВАЛ'}: {len(passed)} из {total}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
