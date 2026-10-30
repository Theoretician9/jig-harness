#!/usr/bin/env python3
"""Probes of the acceptance form `alert-task.py --is-green <key>`, written BEFORE its code.

Task hr.watchers-report-must-create-model-task, criterion K-6 (spec §3.1, R-7;
plan Task 1.5). An alert card carries
`проверка: {команда: "python3 scripts/alert-task.py --is-green K", ждём: "ЗЕЛЁНОЕ K"}`;
until acceptance allows that form, every such card is judged
`доказать-нечем` and its return to `test` never closes by machine.

The judge is `позволена` of acceptance.py with the named-permission list
emptied (ACCEPTANCE_FORMS → an empty stand file): a line in the data would turn
the form green without the rule under test.

    python3 scripts/test_acceptance_is_green.py

Door for mutation runs ([[probe-loads-the-live-mechanism]]):
    HARNESS_ACCEPTANCE   path of acceptance.py under test
Last line: `acceptance --is-green: случаев N, падений F`.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

SUBJECT = Path(os.environ.get("HARNESS_ACCEPTANCE") or HERE / "acceptance.py")
FORM = "python3 scripts/alert-task.py --is-green"

cases = Cases()
check = cases.check

STUB_WRITER = '''#!/usr/bin/env python3
import sys
key = sys.argv[sys.argv.index("--is-green") + 1]
if key.startswith("green"):
    print("ЗЕЛЁНОЕ " + key)
    sys.exit(0)
print("КРАСНОЕ " + key + ": заведена, 2026-10-26T06:20:11+05:00")
sys.exit(1)
'''


def load(path: Path):
    spec = importlib.util.spec_from_file_location("acceptance_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def card(command: str, expect: str | None) -> dict:
    check_field = {"команда": command}
    if expect is not None:
        check_field["ждём"] = expect
    return {"id": "hr.alert-probe", "status": "test", "owner_testable": False,
            "источник": "тревога:probe", "проверка": check_field}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="acceptance-is-green-") as tmp:
        root = Path(tmp)
        empty_forms = root / "acceptance-forms.yaml"
        empty_forms.write_text("по_имени: []\n", encoding="utf-8")
        os.environ["ACCEPTANCE_FORMS"] = str(empty_forms)
        acc = load(SUBJECT)
        allowed = acc.позволена

        print("── форма --is-green разрешена (К-6) ──")
        check("форма из карточки тревоги разрешена",
              True, allowed(f"{FORM} nightly-gates:red"))
        check("ключ с тремя частями разрешён",
              True, allowed(f"{FORM} heartbeat-watch:dead:backup"))
        check("ключ длиной ровно 80 разрешён",
              True, allowed(f"{FORM} x:" + "a" * 78))
        check("в цепочке с разрешённым звеном разрешена",
              True, allowed(f"{FORM} nightly-gates:red && test -s dev-map.yaml"))

        print("── шире, чем нужно, форма не пускает ──")
        refused = {
            "ключа нет": f"{FORM}",
            "ключ пустой (пробел в конце)": f"{FORM} ",
            "два довода": f"{FORM} a:x b:y",
            "ключ с пробелом в кавычках": f"{FORM} 'a b'",
            "ключ с «;»": f"{FORM} 'a;rm -rf x'",
            "ключ с «;» без кавычек": f"{FORM} a;rm",
            "подстановка $()": f"{FORM} $(id)",
            "обратные кавычки": f"{FORM} `id`",
            "заглавные буквы": f"{FORM} A:x",
            "ключ с «/»": f"{FORM} a/b:x",
            "ключ длиной 81": f"{FORM} x:" + "a" * 79,
            "кириллица в ключе": f"{FORM} ночь:красное",
            "выход в родительский каталог": f"{FORM} ..:x",
            "чужой скрипт с тем же ключом": "python3 scripts/archive-map.py --is-green a:x",
            "путь вглубь": "python3 scripts/x/alert-task.py --is-green a:x",
            "довод после ключа": f"{FORM} a:x --resolve",
            "пишущая форма --resolve": "python3 scripts/alert-task.py --resolve a:x",
            "пишущая форма --drain": "python3 scripts/alert-task.py --drain",
            "пишущая форма --key": "python3 scripts/alert-task.py --key a:x --title t",
            "читающая, но не та --open": "python3 scripts/alert-task.py --open",
            "перенаправление в файл": f"{FORM} a:x > dev-map.yaml",
            "в фон": f"{FORM} a:x &",
            "труба": f"{FORM} a:x | sh",
        }
        for name, command in refused.items():
            check(f"запрещена: {name}", False, allowed(command))

        print("── форма описана словами для строителя проверок ──")
        words = [w for w in acc.ФОРМЫ_СЛОВАМИ if "--is-green" in w]
        check("в ФОРМЫ_СЛОВАМИ ровно одна строка с --is-green", 1, len(words))
        check("строка называет alert-task.py", True,
              bool(words) and "alert-task.py" in words[0])

        print("── приёмка судит карточку тревоги исполнением, не формой ──")
        (root / "scripts").mkdir()
        (root / "scripts" / "alert-task.py").write_text(STUB_WRITER, encoding="utf-8")
        verdict = acc.судить(card(f"{FORM} green:x", "ЗЕЛЁНОЕ green:x"), root)
        check("зелёный ключ → «доказано»", "доказано", verdict.get("исход"))
        verdict = acc.судить(card(f"{FORM} red:x", "ЗЕЛЁНОЕ red:x"), root)
        check("красный ключ (код 1) → «провал», не «доказать-нечем»",
              "провал", verdict.get("исход"))
        verdict = acc.судить(card(f"{FORM} green:x", None), root)
        check("без поля «ждём» → «доказать-нечем» (Ф-10 держится)",
              "доказать-нечем", verdict.get("исход"))
        verdict = acc.судить(card(f"{FORM} 'a;rm -rf x'", "ЗЕЛЁНОЕ a"), root)
        check("кривой ключ в карточке → «доказать-нечем», стаб не позван",
              "доказать-нечем", verdict.get("исход"))

    print(f"acceptance --is-green: случаев {cases.paths}, падений {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
