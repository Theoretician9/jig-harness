#!/usr/bin/env python3
"""Probe: the card automaton and the outcome gate judge an outcome as ONE judge.

Source: spec docs/specs/2026-10-18-map-stays-clean.md §0, §9 Ф0; contract
pretask-f0.yaml of the task. The sick case: a message whose
card lives only in docs/dev-map-archive.yaml was a candidate for a NEW card in
map-from-message.py (`накопленные()`), while tasks-from-channel.py saw its
outcome — the automaton would resurrect what the map review had removed.

"One judge" means the same text set (`разобранные()`: map + archive +
satellite) AND the same predicate (`channel_queue.есть_исход()`): a bare
satellite line `<id>` with no reason (written by `пометить()` with an empty
reason) is an outcome for the gate, and a private `startswith(id + " ")` in
the automaton does not see it — id 333.3 tells the two predicates apart.

Why every answer is also written as an absolute: after the fix both sides call
the same functions, so "equal" alone would compare a function with itself.

Door: MAP_FROM_MESSAGE_PY — path of the map-from-message.py copy to judge
(default: next to this file). The gate is loaded as the NEIGHBOUR of that copy
(`модуль("tasks-from-channel")` of the copy's own lib), never from the live
tree. A door pointing nowhere is a failure, not a silent fallback to the live
file.

Stand: a temp directory with its own map, archive, satellite and inbox;
LOG_DIR and friends point into it. The work word is «надо»: it is in
harness/config/queue.yaml AND in the built-in default, so a scripts-only tree
copy (mutation runner) judges the same.

Run: python3 scripts/test_map_from_message_outcome.py
Exit: 0 — every case green, 1 — at least one bad.
"""
import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

UNDER_TEST = Path(os.environ.get("MAP_FROM_MESSAGE_PY")
                  or Path(__file__).resolve().parent / "map-from-message.py")

WORK_TEXT = "надо починить карту, сообщение {}"

# id -> where its outcome lives; the expected answer is written out, not derived.
EXPECTED_OUTCOME = {
    "111.1": True,    # only in the archive (the sick case of §0)
    "222.2": False,   # nowhere
    "222.9": True,    # satellite line with a reason
    "333.3": True,    # bare satellite line, no reason
    "444.4": False,   # nowhere, file in the inbox ROOT
}
EXPECTED_PENDING = {"222.2", "444.4"}

checked = failed = 0


def case(name: str, ok: bool, detail: str = "") -> None:
    global checked, failed
    checked += 1
    failed += 0 if ok else 1
    print(("  ок    " if ok else "  ПЛОХО ") + name + ("" if ok or not detail else f": {detail}"))


def build_stand(root: Path) -> None:
    (root / "docs").mkdir()
    (root / "память").mkdir()
    (root / "log").mkdir()
    done = root / "inbox" / "обработано"
    done.mkdir(parents=True)
    (root / "dev-map.yaml").write_text(
        "version: 1\nlines:\n  - id: core\n    tasks:\n"
        "      - id: hr.other\n        status: plan\n"
        "        summary: \"карточка без ссылок на сообщения\"\n", encoding="utf-8")
    (root / "docs" / "dev-map-archive.yaml").write_text(
        "tasks:\n  - id: hr.archived\n    status: done\n    источник: inbox:111.1\n",
        encoding="utf-8")
    (root / "память" / ".tasks-triaged").write_text("222.9 не-задача\n333.3\n", encoding="utf-8")
    for message_id in ("111.1", "222.2", "222.9", "333.3"):
        (done / f"{message_id}.txt").write_text(WORK_TEXT.format(message_id), encoding="utf-8")
    (root / "inbox" / "444.4.txt").write_text(WORK_TEXT.format("444.4"), encoding="utf-8")


def tree_digest(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.md5(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def stand_env(root: Path) -> dict:
    env = dict(os.environ)
    env.update(LOG_DIR=str(root / "log"), HEARTBEAT_DIR=str(root / "log" / "heartbeat"),
               HARNESS_PANEL_STATE=str(root / "log" / "panel-state"),
               PYTHONDONTWRITEBYTECODE="1")
    return env


def load_mechanism():
    spec = importlib.util.spec_from_file_location("map_from_message_under_test", UNDER_TEST)
    mechanism = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mechanism)
    return mechanism


def judge_pending(mechanism, inbox: Path, map_file: Path) -> None:
    pending = set(getattr(mechanism, "накопленные")(inbox, map_file))
    case("(а) исход только в архиве: накопленные() не берёт 111.1", "111.1" not in pending,
         f"взяты {sorted(pending)}")
    case("(б) без исхода: накопленные() берёт 222.2 из обработано/", "222.2" in pending,
         f"взяты {sorted(pending)}")
    case("(б2) файл в корне инбокса: накопленные() берёт 444.4", "444.4" in pending,
         f"взяты {sorted(pending)}")
    case("(в) исход в спутнике с причиной: накопленные() не берёт 222.9", "222.9" not in pending,
         f"взяты {sorted(pending)}")
    case("(в2) голая строка спутника: накопленные() не берёт 333.3", "333.3" not in pending,
         f"взяты {sorted(pending)}")
    case("(в3) накопленные() — ровно {222.2, 444.4}", pending == EXPECTED_PENDING,
         f"взяты {sorted(pending)}")


def judge_one_judge(mechanism, stand: Path, map_file: Path) -> None:
    gate = getattr(mechanism, "модуль")("tasks-from-channel")
    gate_text = getattr(gate, "разобранные")(stand)
    gate_predicate = getattr(getattr(gate, "очередь"), "есть_исход")
    automaton = getattr(mechanism, "исход_есть")
    for message_id, expected in EXPECTED_OUTCOME.items():
        mine, theirs = automaton(map_file, message_id), gate_predicate(message_id, gate_text)
        case(f"(г) исход_есть == есть_исход гейта: {message_id} (ждём {expected})",
             mine == theirs == expected, f"автомат {mine}, гейт {theirs}")


def judge_instrument(stand: Path, map_file: Path, inbox: Path) -> None:
    run = subprocess.run(
        [sys.executable, str(UNDER_TEST), "--сверить-с-гейтом",
         "--карта", str(map_file), "--инбокс", str(inbox)],
        capture_output=True, text=True, env=stand_env(stand), cwd=stand, timeout=120)
    lines = run.stdout.splitlines()
    tail = (run.stdout + run.stderr).strip().splitlines()[-3:]
    case("(д) прибор: разница 0 — «автомат: 2, гейт: 2, разница множеств: 0», код 0",
         run.returncode == 0 and "автомат: 2, гейт: 2, разница множеств: 0" in lines,
         f"код {run.returncode}, хвост {tail}")
    case("(д2) прибор не печатает id разницы — ни один номер стенда в выводе",
         run.returncode == 0 and not any(i in run.stdout for i in EXPECTED_OUTCOME),
         f"код {run.returncode}, хвост {tail}")


def main() -> int:
    if not UNDER_TEST.is_file():
        case(f"механизм загружен по двери MAP_FROM_MESSAGE_PY ({UNDER_TEST})", False,
             "файла нет — боевой файл вместо него не беру")
        print(f"путей {checked}, неудач {failed}")
        return 1
    with tempfile.TemporaryDirectory(prefix="f0-outcome-") as tmp:
        stand = Path(tmp)
        build_stand(stand)
        map_file, inbox = stand / "dev-map.yaml", stand / "inbox"
        before = tree_digest(stand)
        os.environ.update({k: v for k, v in stand_env(stand).items()
                           if k in ("LOG_DIR", "HEARTBEAT_DIR", "HARNESS_PANEL_STATE")})
        mechanism = load_mechanism()
        case("механизм загружен по двери MAP_FROM_MESSAGE_PY",
             Path(mechanism.__file__).resolve() == UNDER_TEST.resolve(), mechanism.__file__)
        judge_pending(mechanism, inbox, map_file)
        judge_one_judge(mechanism, stand, map_file)
        judge_instrument(stand, map_file, inbox)
        after = tree_digest(stand)
        case("стенд не тронут: md5 карты, архива, спутника и инбокса до == после",
             before == after, f"изменились {sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))}")
    print(f"путей {checked}, неудач {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
