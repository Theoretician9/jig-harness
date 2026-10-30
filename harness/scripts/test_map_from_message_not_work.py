#!/usr/bin/env python3
"""Probe: the card automaton skips a message only for the reason «не работа».

Card hr.map-from-message-not-work-substring. The sick case: the urgency judge
(scripts/urgency.py) answers «слово срочности «не работает»» for «панель не
работает» — the substring «не работа» sits inside it, and the automaton read it
as "not work": the card was never created. The real "not work" reason starts
the line: «не работа (ответ, реакция) — доезжает сразу».

Reasons are taken from the REAL urgency judge (the neighbour of the copy under
test), not copied: a copied reason would stay green after urgency.py rewords it.
The literal «слово срочности «не работает»» is the one case that reddens a
`startswith("не работа")` without a word boundary.

Two roads, two cases: the predicate itself and the call site in `завести`
(a dry run through main with a stub model — the stub's mark file is the sign
that the automaton went on to the work instead of skipping it).

Door: MAP_FROM_MESSAGE_PY — path of the map-from-message.py copy to judge
(default: next to this file). A door pointing nowhere is a failure.

Run: python3 scripts/test_map_from_message_not_work.py
Exit: 0 — every case green, 1 — at least one bad.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

UNDER_TEST = Path(os.environ.get("MAP_FROM_MESSAGE_PY")
                  or Path(__file__).resolve().parent / "map-from-message.py")

# текст -> ждём ли «не работа»; ожидание записано абсолютом, не выведено.
EXPECTED_BY_TEXT = {
    "панель не работает": False,
    "срочно, карта не работает": False,
    "спасибо, понял": True,
}
# Прямая строка причины: именно её пропускает startswith без границы слова.
EXPECTED_BY_REASON = {
    "слово срочности «не работает»": False,
    "не работает": False,
    "не работа (ответ, реакция) — доезжает сразу": True,
}

checked = failed = 0


def case(name: str, ok: bool, detail: str = "") -> None:
    global checked, failed
    checked += 1
    failed += 0 if ok else 1
    print(("  ок    " if ok else "  ПЛОХО ") + name + ("" if ok or not detail else f": {detail}"))


def load_mechanism():
    spec = importlib.util.spec_from_file_location("map_from_message_under_test", UNDER_TEST)
    mechanism = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mechanism)
    return mechanism


def judge_predicate(mechanism) -> None:
    predicate = getattr(mechanism, "is_not_work_reason", None)
    if predicate is None:
        case("в механизме есть is_not_work_reason(причина)", False, "функции нет")
        return
    urgency = getattr(mechanism, "модуль")("urgency")
    for text, expected in EXPECTED_BY_TEXT.items():
        _, reason = urgency.срочно(text)
        got = predicate(reason)
        case(f"«{text}» → причина «{reason}» → не работа: {expected}", got is expected,
             f"ответ {got}")
    for reason, expected in EXPECTED_BY_REASON.items():
        got = predicate(reason)
        case(f"причина «{reason}» → не работа: {expected}", got is expected, f"ответ {got}")


def judge_call_site(stand: Path) -> None:
    """«панель не работает» — работа: сухой прогон обязан дойти до модели."""
    message_id = "555.5"
    done = stand / "inbox" / "обработано"
    done.mkdir(parents=True)
    (done / f"{message_id}.txt").write_text("панель не работает", encoding="utf-8")
    map_file = stand / "dev-map.yaml"
    map_file.write_text(
        "version: 1\nlines:\n  - id: core\n    tasks:\n"
        "      - id: hr.other\n        status: plan\n"
        "        summary: \"карточка без ссылок на сообщения\"\n", encoding="utf-8")
    (stand / "docs").mkdir()
    (stand / "docs" / "dev-map-archive.yaml").write_text("tasks: []\n", encoding="utf-8")
    (stand / "память").mkdir()
    (stand / "log").mkdir()
    mark = stand / "model-called"
    stub = stand / "stub-model.py"
    stub.write_text(
        "import json, pathlib\n"
        f"pathlib.Path({str(mark)!r}).write_text('1')\n"
        "print(json.dumps({'задачи': ['починить панель'], 'имена': ['fix-panel'],"
        " 'слои': ['harness']}))\n", encoding="utf-8")
    env = dict(os.environ, LOG_DIR=str(stand / "log"),
               HEARTBEAT_DIR=str(stand / "log" / "heartbeat"),
               HARNESS_PANEL_STATE=str(stand / "log" / "panel-state"),
               HARNESS_SPROSIT_MODEL=str(stub), PYTHONDONTWRITEBYTECODE="1")
    run = subprocess.run(
        [sys.executable, str(UNDER_TEST), "--сообщение", message_id, "--сухой",
         "--карта", str(map_file), "--инбокс", str(stand / "inbox"), "--линия", "core"],
        capture_output=True, text=True, env=env, cwd=stand, timeout=120)
    tail = (run.stdout + run.stderr).strip().splitlines()[-3:]
    case("завести(«панель не работает»): модель спрошена, карточка не пропущена",
         mark.is_file() and "завёл бы 1" in run.stdout, f"код {run.returncode}, хвост {tail}")


def main() -> int:
    if not UNDER_TEST.is_file():
        case(f"механизм загружен по двери MAP_FROM_MESSAGE_PY ({UNDER_TEST})", False,
             "файла нет — боевой файл вместо него не беру")
        print(f"путей {checked}, неудач {failed}")
        return 1
    mechanism = load_mechanism()
    judge_predicate(mechanism)
    with tempfile.TemporaryDirectory(prefix="not-work-") as tmp:
        judge_call_site(Path(tmp))
    print(f"путей {checked}, неудач {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
