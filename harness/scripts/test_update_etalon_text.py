#!/usr/bin/env python3
"""Проба: весть об отказе печати эталона называет ТОТ эталон, что читают модели.

Стенд копии 14.11.2026: гейт «корень установки» краснел на update.py — в тексте
карточки отказа self-seal стоял путь эталона строкой. Строка не только краснила
гейт: на машине без права писать в /etc эталон лежит под ~/.harness, а при
двери SELF_ETALON_DIR — где укажет дверь, и весть назвала бы чужой каталог.
Источник пути один — тот же, что у self-prompt.py: SELF_ETALON_DIR, иначе
<корень машины>/self.

Случаи (каждый — свой процесс, окружение не утекает):
  * дверь SELF_ETALON_DIR задана — весть называет её каталог;
  * двери нет — весть называет <корень_машины()>/self (корень подменён стендом);
  * гейт «корень установки» на update.py под пробой — ни одной находки.

Дверь мутаций: UPDATE_PY — update.py под пробой.
Прогон: python3 scripts/test_update_etalon_text.py   Код: 0 — все случаи прошли.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from selftest_cases import Cases  # noqa: E402

UPDATE = Path(os.environ.get("UPDATE_PY") or HERE / "update.py")

FAKE_ALERT = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["STAND_ALERT_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
"""

# Корень машины подменён стендом: живой /etc/harness пробе не хозяин.
CALL = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("update_under_probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.конф_модуль.корень_машины = lambda: sys.argv[2]
module._сигнал_отказа_печати("код 1: стенд пробы")
"""


def alert_text(stand: Path, etalon_door: str | None) -> str:
    log = stand / "alert.jsonl"
    log.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "SELF_ETALON_DIR"}
    env.update(HARNESS_ALERT_TASK=str(stand / "alert-task"), STAND_ALERT_LOG=str(log))
    if etalon_door:
        env["SELF_ETALON_DIR"] = etalon_door
    subprocess.run([sys.executable, "-c", CALL, str(UPDATE), str(stand / "mashina")],
                   env=env, capture_output=True, text=True, timeout=60, check=True)
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    if len(calls) != 1:
        return f"вызовов писателя тревог {len(calls)}, ждали 1"
    argv = calls[0]
    return argv[argv.index("--text") + 1]


def gate_findings(stand: Path) -> list:
    tree = stand / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy(UPDATE, tree / "scripts" / "update.py")
    spec = importlib.util.spec_from_file_location("install_root_gate", HERE / "check-install-root.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    (tree / "harness").mkdir()
    return gate.находки(tree)


def main() -> int:
    cases = Cases()
    with tempfile.TemporaryDirectory(prefix="harness-run-etalon-") as tmp:
        stand = Path(tmp)
        writer = stand / "alert-task"
        writer.write_text(FAKE_ALERT, encoding="utf-8")
        writer.chmod(0o755)

        door = str(stand / "dver-etalona")
        text = alert_text(stand, door)
        cases.check("БОЛЬНОЙ СЛУЧАЙ: дверь SELF_ETALON_DIR — весть называет её каталог",
                    (True, False), (door in text, "/etc/harness" in text))

        text = alert_text(stand, None)
        cases.check("двери нет — весть называет <корень машины>/self",
                    (True, False), (f"{stand / 'mashina'}/self" in text, "/etc/harness" in text))

        cases.check("гейт «корень установки» на update.py — ни одной находки",
                    [], gate_findings(stand))
    print(f"итог test_update_etalon_text: случаев {cases.paths}, неудач {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
