#!/usr/bin/env python3
"""Probes of the shared map writer in scripts/lib/cards.py, written BEFORE `cards.py --selftest` runs them.

Task hr.watchers-report-must-create-model-task (spec §3.4, R-2, R-4, R-14;
plan Task 1.2). `write_cards_locked` is the one writer of cards that
map-from-message.py and alert-task.py share: it takes the map lock ITSELF,
from the config path, calls `recheck` UNDER the lock and refuses a write that
loses an id or does not add exactly the new cards. `key_state` and
`alert_weights` are judged in test_alert_task.py (section weights) and are not
repeated here.

Every write happens in a CHILD process on its own stand (own LOG_DIR, own map):
the lock is process-global state, and the config is read from the environment
of the process that takes it ([[a-probe-of-global-state-needs-its-own-process]]).
The live map is compared by sha1 before and after.

    python3 scripts/test_cards_lib.py

Door for mutation runs ([[probe-loads-the-live-mechanism]]):
    HARNESS_CARDS_LIB   directory holding cards.py under test
Last line: `cards-lib: случаев N, падений F`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import test_alert_task as alerts  # noqa: E402  (the stand of the alert writer probes)
from selftest_cases import Cases  # noqa: E402

CARDS_DIR = Path(os.environ.get("HARNESS_CARDS_LIB") or HERE / "lib")

cases = Cases()
check = cases.check

# One call of write_cards_locked. argv: map line card_json source wait recheck(0|1) ready
CHILD = r'''
import json, sys, time
sys.path.insert(0, sys.argv[1])
import cards
map_path, line, card_text, source, wait, recheck, ready = sys.argv[2:9]
wait = None if wait == "none" else float(wait)
check = (lambda text: cards.open_card_by_source(text, source) is None) if recheck == "1" else None
open(ready, "w").close()
started = time.monotonic()
try:
    done = cards.write_cards_locked(map_path, line, [json.loads(card_text)],
                                    recheck=check, lock_wait_sec=wait)
    outcome = "written" if done else "skipped"
except cards.LockBusy:
    outcome = "busy"
except BaseException as error:          # MapRefused is a SystemExit
    outcome = "refused: " + str(error)
print(json.dumps({"outcome": outcome, "sec": time.monotonic() - started, "ended": time.time()},
                 ensure_ascii=False))
'''


def card_text(card_id: str, source: str, newline: bool = True) -> str:
    text = (f"      - id: {card_id}\n"
            f"        status: plan\n"
            f"        owner_testable: false\n"
            f"        источник: {source}\n"
            f"        title: \"карточка пробы\"\n"
            f"        summary: \"проба\"\n"
            f"        volume: {{ loc: 1, complexity: 1, tokens_k: 1, estimated: true }}\n"
            f"        when: {{ started: null, done: null, tested: null, estimated: true }}")
    return text + ("\n" if newline else "")


def child(st: alerts.Stand, card: str, source: str, wait: str = "none",
          recheck: bool = False, ready: str = os.devnull) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", CHILD, str(CARDS_DIR), str(st.map), "harness",
         json.dumps(card, ensure_ascii=False), source, wait, "1" if recheck else "0", ready],
        env=st.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def result(proc: subprocess.Popen, limit: float = 30) -> dict:
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return {"outcome": f"предел {limit} с истёк", "sec": limit}
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"outcome": "упал: " + (err.strip().splitlines() or ["?"])[-1], "sec": 0}


def section_lock(tmp: Path) -> None:
    print("── лок берётся сам, путь из конфига (Р-14, Р-1) ──")
    st = alerts.Stand(tmp / "lock")
    check("лок стенда лежит по пути конфига LOG_DIR/locks/services/dev-map.lock", True,
          str(st.lock).startswith(str(st.log)))
    holder = st.hold_lock(30)
    try:
        before = alerts.sha1(st.map)
        got = result(child(st, card_text("hr.lock-busy", "проба:busy"), "проба:busy", wait="0.5"))
        check("lock_wait_sec=0.5 при чужом flock на пути конфига → LockBusy", "busy", got["outcome"])
        check("LockBusy за ≤ 1,5 с (предел, а не боевые минуты)", True, got["sec"] <= 1.5)
        check("при LockBusy карта не тронута", before, alerts.sha1(st.map))
        # Секунда отсчитывается от метки «дошёл до ожидания»: запуск питона и
        # импорт под нагрузкой ворот съедали из неё до 0,2 с.
        ready = st.log / "lock-wait.ready"
        waiting = child(st, card_text("hr.lock-wait", "проба:wait"), "проба:wait", ready=str(ready))
        limit = time.monotonic() + 30
        while not ready.exists() and waiting.poll() is None and time.monotonic() < limit:
            time.sleep(0.05)
        time.sleep(1)
        check("lock_wait_sec=None ждёт, пока лок держат (через 1 с ещё не вышел)", None, waiting.poll())
    finally:
        released = time.time()
        holder.kill()
        holder.wait()
    got = result(waiting)
    check("lock_wait_sec=None после отпускания пишет", "written", got["outcome"])
    check("lock_wait_sec=None вышел не раньше, чем лок отпустили", True,
          got.get("ended", 0) >= released)
    check("карточка ждавшего зова в карте", 1, len([t for t in st.tasks() if t["id"] == "hr.lock-wait"]))


def section_recheck(tmp: Path) -> None:
    print("── recheck ПОД локом: два процесса одного источника → 1 карточка, 20 пар (Р-4) ──")
    st = alerts.Stand(tmp / "pairs")
    counts, outcomes = [], []
    for n in range(20):
        source = f"тревога:pair{n}:x"
        # Different ids on purpose: with one id the count check would hide a
        # recheck taken outside the lock.
        one = child(st, card_text(f"hr.pair{n}-a", source), source, recheck=True)
        two = child(st, card_text(f"hr.pair{n}-b", source), source, recheck=True)
        outcomes.append(sorted([result(one)["outcome"], result(two)["outcome"]]))
        counts.append(len(st.by_source(f"pair{n}:x")))
    check("в каждой из 20 пар ровно одна карточка", [1] * 20, counts)
    check("в каждой паре один записал, второй увидел карточку под локом", [["skipped", "written"]] * 20,
          outcomes)


def section_counts(tmp: Path) -> None:
    print("── сверки числа и id (общие на обоих писателей) ──")
    st = alerts.Stand(tmp / "counts")
    before = alerts.sha1(st.map)
    got = result(child(st, card_text("hr.sample-one", "проба:dup"), "проба:dup"))
    check("дубль существующего id → отказ («стало ≠ было + новые»)", True,
          got["outcome"].startswith("refused:") and "было" in got["outcome"])
    check("после отказа по числу карта не тронута", before, alerts.sha1(st.map))
    # No trailing newline: the neighbour's «- id:» line glues onto the last
    # field, and its id disappears from the parsed map.
    got = result(child(st, card_text("hr.glued", "проба:glue", newline=False), "проба:glue"))
    check("карточка, съевшая соседа → отказ «потеряла задачи»", True,
          got["outcome"].startswith("refused:") and "hr.sample-one" in got["outcome"])
    check("после отказа по пропавшему id карта не тронута", before, alerts.sha1(st.map))
    # The glued case above dies in the YAML parser, so it never reaches the
    # «lost id» check. Here the map stays VALID: the trailing «#» turns the
    # neighbour's «- id:» line into a comment, its fields land in hr.eat-b, and
    # two new ids make the count add up — only the «lost id» check can refuse.
    swallow = (card_text("hr.eat-a", "проба:eat") + card_text("hr.eat-b", "проба:eat")
               + "        tags: #")
    got = result(child(st, swallow, "проба:eat"))
    check("карточка съела соседа, а YAML цел и число сходится → отказ «потеряла задачи»", True,
          got["outcome"].startswith("refused:") and "потеряла" in got["outcome"]
          and "hr.sample-one" in got["outcome"])
    check("после отказа по пропавшему id (YAML цел) карта не тронута", before, alerts.sha1(st.map))
    got = result(child(st, card_text("hr.fine", "проба:fine"), "проба:fine"))
    check("годная карточка пишется", "written", got["outcome"])
    ids = [t["id"] for t in st.tasks()]
    check("годная встала первой в линии harness, соседи на месте", True,
          ids[:3] == ["hr.fine", "hr.sample-one", "hr.sample-two"])


def section_open_card(tmp: Path) -> None:
    print("── open_card_by_source ──")
    lib = alerts.load_cards()
    check("lib/cards.py несёт open_card_by_source", True, bool(lib) and hasattr(lib, "open_card_by_source"))
    if not lib or not hasattr(lib, "open_card_by_source"):
        return
    text = alerts.SAMPLE_MAP
    for status, want in (("plan", True), ("test", True), ("беклог", True),
                         ("done", False), ("принято", False)):
        card = alerts.manual_card("hr.alert-s-x", status, "тревога:s:x")
        mapped = text.replace("      - id: hr.sample-one\n", card + "      - id: hr.sample-one\n", 1)
        found = lib.open_card_by_source(mapped, "тревога:s:x")
        check(f"статус {status}: открытая — {want}", want, bool(found) and found.get("id") == "hr.alert-s-x")
    check("источника нет → None", None, lib.open_card_by_source(text, "тревога:nobody:x"))
    found = lib.open_card_by_source(text, "inbox:1790000000.100000000")
    check("найденная несёт свою линию", "harness", (found or {}).get("_line"))


def section_wiring(tmp: Path) -> None:
    print("── cards.py --selftest исполняет эти пробы ──")
    if os.environ.get("CARDS_LIB_PROBE_NESTED"):
        return
    env = alerts.clean_env()
    env["CARDS_LIB_PROBE_NESTED"] = "1"
    done = subprocess.run([sys.executable, str(CARDS_DIR / "cards.py"), "--selftest"],
                          capture_output=True, text=True, env=env, timeout=600)
    check("вывод cards.py --selftest несёт итог этих проб «cards-lib: случаев»", True,
          "cards-lib: случаев" in done.stdout)


SECTIONS = (section_lock, section_recheck, section_counts, section_open_card, section_wiring)


def main() -> int:
    live_before = alerts.sha1(alerts.LIVE_MAP)
    with tempfile.TemporaryDirectory(prefix="cards-lib-probe-") as tmp:
        for section in SECTIONS:
            try:
                section(Path(tmp))
            except Exception as error:  # a crashed section is a failure, not a silence
                check(f"раздел {section.__name__} дошёл до конца", "дошёл",
                      f"упал: {type(error).__name__}: {error}")
            check(f"{section.__name__}: боевая карта не тронута", live_before,
                  alerts.sha1(alerts.LIVE_MAP))
    print(f"cards-lib: случаев {cases.paths}, падений {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
