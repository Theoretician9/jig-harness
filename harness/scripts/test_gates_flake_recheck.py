#!/usr/bin/env python3
"""Проба: красная общего прогона ворот перепрогоняется соло; соло-зелёная — флейк, не красная.

    python3 scripts/test_gates_flake_recheck.py

Слово владельца 14.11.2026: «если это падает не потому что ошибка в коде, то зачем
нам эти флейки нужны». Три полных прогона ворот подряд дали по одному новому
красному, и каждый соло был зелёным 3/3. Договор:
  * GATES_RECHECK=1 (его ставит gates.sh) — красная общего прогона не считается
    сразу; gates_recheck_reds после барьера гонит её один раз отдельно;
  * соло зелёная — флейк: в RED не идёт, пишется событие gate.flake с именем в
    журнал секунд ворот, счётчик FLAKES растёт, вывод называет «флейк N раз за
    7 дней», а со второго за неделю — «— починить»;
  * соло красная — красная, как раньше: «ПЛОХО · имя (код N)» и вывод соло;
  * без GATES_RECHECK обёртка считает красную сразу (стенды проб читают счёт
    сразу после вызова);
  * gates.sh ставит GATES_RECHECK=1 и зовёт gates_recheck_reds перед КАЖДЫМ итогом.

Стенд — фиктивные проверки через вход обёртки; свой LOG_DIR, полные ворота не идут.
Двери для мутаций: GATES_CHECK_LIB (путь обёртки), GATES_SH (путь gates.sh).
Код возврата: 0 — все случаи зелёные, 1 — есть красный.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = os.environ.get("GATES_CHECK_LIB") or str(ROOT / "scripts/lib/gates_check.sh")
GATES_SH = os.environ.get("GATES_SH") or str(ROOT / "scripts/gates.sh")
JOURNAL = "gates-timing.jsonl"

# $1 — LOG_DIR стенда, $2 — обёртка, $3 — GATES_RECHECK, $4 — GATES_JOBS.
# «флейк» падает при первом запуске в прогоне и проходит при втором (метка в $1/был);
# каждый запуск проверки — строка в $1/<имя>.runs.
STAND = r"""
set -uo pipefail
export LC_ALL=C.UTF-8
LOG_DIR="$1"
. "$2"
GATES_RECHECK="$3"
GATES_JOBS="$4"
rm -f "$1/был"
проверка "зелёная" bash -c 'echo x >> "$0/зелёная.runs"; echo всё хорошо' "$1"
проверка "флейк" bash -c 'echo x >> "$0/флейк.runs"; if [ -f "$0/был" ]; then echo второй раз прошла; else : > "$0/был"; echo упала в общем; exit 1; fi' "$1"
проверка "красная" bash -c 'echo x >> "$0/красная.runs"; echo сломано по-настоящему; exit 3' "$1"
gates_wait_all
gates_recheck_reds
printf 'СЧЁТ %s %s %s\n' "$GREEN" "$RED" "${FLAKES:-нет}"
"""

failed: list[str] = []
passed: list[str] = []


def case(name: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(name)
    print(f"  {'ок' if ok else 'ПЛОХО'} · {name}" + ("" if ok else f" — {detail}"))


def run_stand(log_dir: Path, recheck: str = "1", jobs: str = "1", **extra: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HARNESS_LOG_DIR", "GATES_"))}
    env.update(extra)
    proc = subprocess.run(["bash", "-c", STAND, "stand", str(log_dir), LIB, recheck, jobs],
                          capture_output=True, text=True, timeout=120, env=env)
    return proc.stdout + proc.stderr


def runs(log_dir: Path, name: str) -> int:
    path = log_dir / f"{name}.runs"
    return len(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else 0


def flake_rows(log_dir: Path) -> list[dict]:
    path = log_dir / JOURNAL
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == "gate.flake"]


def flake_line(out: str) -> str:
    return next((l for l in out.splitlines() if l.lstrip().startswith("флейк · флейк")), "")


def judge_pass(label: str, jobs: str) -> None:
    with tempfile.TemporaryDirectory(prefix="gates-flake-") as tmp:
        log_dir = Path(tmp)
        out = run_stand(log_dir, "1", jobs)
        case(f"{label}: счёт — зелёных 1, красных 1 (только настоящая), флейков 1",
             "СЧЁТ 1 1 1" in out, out[-600:])
        case(f"{label}: красная и соло названа «ПЛОХО · красная (код 3)» с выводом",
             "ПЛОХО · красная (код 3)" in out and "сломано по-настоящему" in out, out[-600:])
        case(f"{label}: флейк не назван «ПЛОХО»", "ПЛОХО · флейк" not in out, out[-600:])
        case(f"{label}: флейк гонялся дважды (общий прогон + соло)", runs(log_dir, "флейк") == 2,
             f"запусков {runs(log_dir, 'флейк')}")
        case(f"{label}: красная гонялась дважды (общий прогон + соло)", runs(log_dir, "красная") == 2,
             f"запусков {runs(log_dir, 'красная')}")
        case(f"{label}: зелёная не перепрогонялась", runs(log_dir, "зелёная") == 1,
             f"запусков {runs(log_dir, 'зелёная')}")
        line = flake_line(out)
        case(f"{label}: строка флейка — «флейк 1 раз за 7 дней», без «починить»",
             "флейк 1 раз за 7 дней" in line and "починить" not in line, f"строка: «{line}»\n{out[-600:]}")
        rows = flake_rows(log_dir)
        case(f"{label}: событие gate.flake с именем проверки — одно, только у флейка",
             [(r.get("data") or {}).get("name") for r in rows] == ["флейк"], f"события: {rows}")


def main() -> int:
    judge_pass("последовательно", "1")
    judge_pass("пул 3", "3")

    with tempfile.TemporaryDirectory(prefix="gates-flake-again-") as tmp:
        log_dir = Path(tmp)
        run_stand(log_dir)
        out = run_stand(log_dir)
        line = flake_line(out)
        case("второй флейк той же проверки за неделю — «флейк 2 раз за 7 дней — починить»",
             "флейк 2 раз за 7 дней — починить" in line, f"строка: «{line}»\n{out[-600:]}")

    with tempfile.TemporaryDirectory(prefix="gates-flake-old-") as tmp:
        log_dir = Path(tmp)
        old = (datetime.now().astimezone() - timedelta(days=8)).isoformat(timespec="seconds")
        recent = (datetime.now().astimezone() - timedelta(days=6)).isoformat(timespec="seconds")
        rows = [{"v": 1, "ts": old, "src": "scripts/gates.sh", "event": "gate.flake", "outcome": "ok",
                 "data": {"name": "флейк"}},
                {"v": 1, "ts": recent, "src": "scripts/gates.sh", "event": "gate.flake", "outcome": "ok",
                 "data": {"name": "другая"}}]
        (log_dir / JOURNAL).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                       encoding="utf-8")
        line = flake_line(run_stand(log_dir))
        case("флейк старше 7 дней и флейк другой проверки не в счёт — «1 раз»",
             "флейк 1 раз за 7 дней" in line, f"строка: «{line}»")

    with tempfile.TemporaryDirectory(prefix="gates-flake-off-") as tmp:
        log_dir = Path(tmp)
        out = run_stand(log_dir, "0")
        case("без GATES_RECHECK красная считается сразу, перепрогона нет",
             "СЧЁТ 1 2" in out and runs(log_dir, "флейк") == 1 and not flake_rows(log_dir),
             f"запусков флейка {runs(log_dir, 'флейк')}\n{out[-600:]}")

    # Очередь слияния знает, что проверка красна на самом master (М4 тяжёлых прогонов): её
    # красная ветки соло не повторяется — флейк ветки дал бы ту же красную, что master.
    # Поломка: GATES_NO_RECHECK не читается (красная гонится дважды); снимает соло всем (флейк красный).
    with tempfile.TemporaryDirectory(prefix="gates-flake-norecheck-") as tmp:
        log_dir = Path(tmp)
        out = run_stand(log_dir, GATES_NO_RECHECK="другая|красная")
        case("GATES_NO_RECHECK: названная красная — без соло, флейк — с соло",
             (runs(log_dir, "красная"), runs(log_dir, "флейк"), "СЧЁТ 1 1 1" in out) == (1, 2, True),
             f"запусков красной {runs(log_dir, 'красная')}, флейка {runs(log_dir, 'флейк')}\n{out[-600:]}")

    gates = Path(GATES_SH).read_text(encoding="utf-8")
    finals = [m.start() for m in re.finditer(r'echo "=== итог', gates)]
    wired = all("gates_recheck_reds" in gates[gates.rfind("gates_wait_all", 0, at):at] for at in finals)
    case("gates.sh: перед каждым итогом после барьера — gates_recheck_reds",
         len(finals) >= 2 and wired, f"итогов {len(finals)}, у всех ли перепрогон: {wired}")
    case("gates.sh: включает перепрогон (GATES_RECHECK=1)",
         re.search(r"^GATES_RECHECK=1\b", gates, re.M) is not None, "строки GATES_RECHECK=1 нет")
    case("gates.sh: число флейков — в итоге прогона",
         all("flake" in gates[at:gates.find("\n", at)].lower() for at in finals), "строка итога не называет флейки")

    total = len(passed) + len(failed)
    print(f"ФЛЕЙКИ ВОРОТ: случаев {total}, неудач {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
