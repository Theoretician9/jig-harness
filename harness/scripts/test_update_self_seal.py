#!/usr/bin/env python3
"""Проба: обновление не глотает отказ печати эталона личности (Р6).

Живой стенд 13.11.2026: update.py после
раскладки зовёт `sudo -n /usr/local/lib/harness/self-seal <пакет>` и любой исход
отбрасывает. Отказ печати оставляет эталон /etc/harness/self прежним: модели
получают старую личность, а обновление отчитывается успехом, и никто не узнаёт.

Отказ — сигнал: ровно ОДИН вызов писателя тревог (alert-task.py, дверь
HARNESS_ALERT_TASK) с ключом update:self-seal-refused (форма «<сторож>:<род
беды>»). Успех печати — ни одного сигнала.

Стенд — свой каталог harness-run-*: подставной sudo первым в PATH (пишет свои
доводы и отвечает заданным кодом), подставной писатель тревог (пишет доводы
строкой JSON), пакет — каталог стенда (HARNESS_STARTER). Каждый случай идёт в
своём процессе: окружение стенда не утекает в соседний случай.

Ещё (ревью #5, мутации): писатель тревог без двери — <КОРЕНЬ>/scripts/alert-task.py
под python (живой путь, а не только дверь); sudo нет вовсе (OSError) — тоже
отказ и сигнал; удавшаяся раскладка зовёт печать эталона, ПОТОМ пересборку
карточек; пересборка зовёт self-prompt.py --карточки --в <КОРЕНЬ>/.claude/agents.
КОРЕНЬ модуля проба подменяет каталогом стенда (подставные self-prompt.py и
alert-task.py): живое дерево не трогается.

Ещё (ревью #5, Н4): сбой пересборки карточек не молчит и не роняет обновление.
Удавшаяся раскладка, сборщик карточек отвечает кодом не 0 и не 3 (1, 2) или
не запускается вовсе (таймаут, OSError) → _разложить_пакет не бросает
исключение, а в «упавших» есть запись «карточки помощников» (канал упавших
сборщиков производных, его читает _разложить_и_проверить). Код 0 и код 3
(эталона нет, слой 1 из дерева) — записи нет. Сборщик и install-new.py — файлы
стенда под КОРНЕМ (живой процесс, любой способ вызова); таймаут и OSError —
подменой subprocess.run модуля только для вызова self-prompt.py. Сбой карточек
дописывается к упавшим сборщикам производных (не вытесняет их); откат карточки не
пересобирает и сбоем их не пишет.

Дверь мутаций: UPDATE_PY — update.py под пробой.
Прогон: python3 scripts/test_update_self_seal.py   Код: 0 — все случаи прошли.
"""
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
KEY = "update:self-seal-refused"

FAKE_SUDO = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STAND_SUDO_LOG"
[ "$STAND_SUDO_CODE" = 0 ] || echo "self-seal: отказ: стенд пробы" >&2
exit "$STAND_SUDO_CODE"
"""

FAKE_ALERT = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["STAND_ALERT_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
"""

CALL = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("update_under_probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module._напечатать_эталон()
"""

# Живой путь писателя тревог: двери HARNESS_ALERT_TASK нет, КОРЕНЬ — стенд.
CALL_LIVE_WRITER = """
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("update_under_probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.КОРЕНЬ = pathlib.Path(sys.argv[2])
module._напечатать_эталон()
"""

# Порядок шагов удавшейся раскладки: всё вокруг подменено записью имени шага.
CALL_ORDER = """
import importlib.util, json, sys, types
spec = importlib.util.spec_from_file_location("update_under_probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
steps = []
module._launch_layout = lambda *a, **k: True
module._напечатать_эталон = lambda: steps.append("печать")
module._пересобрать_карточки = lambda: steps.append("карточки")
module.пересобрать_производные = lambda: []
module.subprocess = types.SimpleNamespace(run=lambda *a, **k: types.SimpleNamespace(returncode=0))
module._разложить_пакет()
print(json.dumps(steps, ensure_ascii=False))
"""

# Пересборка карточек: КОРЕНЬ — стенд, его self-prompt.py пишет свои доводы.
CALL_CARDS = """
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("update_under_probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.КОРЕНЬ = pathlib.Path(sys.argv[2])
module._пересобрать_карточки()
"""

FAKE_RECORDER = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["STAND_RECORD_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
"""

# Сбой пересборки карточек (Н4): раскладка удалась, печать и производные подменены,
# КОРЕНЬ — стенд со своими self-prompt.py (код из STAND_CARDS_CODE) и install-new.py.
# Таймаут и OSError нельзя дождаться живьём (предел 120 с) — их бросает подмена run.
CALL_CARDS_FAIL = """
import importlib.util, json, os, pathlib, subprocess, sys, types
spec = importlib.util.spec_from_file_location("update_under_probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.КОРЕНЬ = pathlib.Path(sys.argv[2])
mode = sys.argv[3]
module._launch_layout = lambda *a, **k: mode != "откат"
module._rollback_finished = lambda *a, **k: True
module._напечатать_эталон = lambda: None
derived = os.environ.get("STAND_DERIVED_FAILED")
module.пересобрать_производные = lambda *a, **k: [(derived, "стенд: сборщик упал")] if derived else []
real_run = subprocess.run
def run(argv, *a, **k):
    if "self-prompt.py" in " ".join(map(str, argv)):
        if mode == "таймаут":
            raise subprocess.TimeoutExpired(argv, k.get("timeout") or 120)
        if mode == "oserror":
            raise OSError("стенд пробы: сборщик не запустился")
    return real_run(argv, *a, **k)
proxy = types.SimpleNamespace(**{n: getattr(subprocess, n) for n in dir(subprocess) if not n.startswith("__")})
proxy.run = run
module.subprocess = proxy
try:
    _, failed = module._разложить_пакет()
    names = [str(x[0]) if isinstance(x, (list, tuple)) else str(x) for x in failed]
    print(json.dumps({"исключение": "", "упавшие": names}, ensure_ascii=False))
except Exception as error:
    print(json.dumps({"исключение": f"{type(error).__name__}: {error}", "упавшие": []}, ensure_ascii=False))
"""

FAKE_CARDS = f"""#!{sys.executable}
import os, sys
sys.exit(int(os.environ.get("STAND_CARDS_CODE", "0")))
"""

FAKE_INSTALL_NEW = f"""#!{sys.executable}
import sys
sys.exit(0)
"""

CARDS_FAILED = "карточки помощников"

cases = Cases()


def key_of(argv: list) -> str:
    for flag in ("--key", "--ключ"):
        if flag in argv and argv.index(flag) + 1 < len(argv):
            return argv[argv.index(flag) + 1]
    return ""


def run_case(root: Path, code: int, mode: str = "дверь") -> tuple:
    """(вызовы sudo, вызовы писателя тревог, код процесса, stderr).
    mode: «дверь» — писатель через HARNESS_ALERT_TASK; «живой» — <КОРЕНЬ>/scripts/alert-task.py;
    «без-sudo» — sudo в PATH нет (подставного нет, системный убран)."""
    case = root / f"код-{code}-{mode}"
    (case / "bin").mkdir(parents=True)
    (case / "pkg" / "harness").mkdir(parents=True)
    (case / "log").mkdir()
    sudo = case / "bin" / "sudo"
    sudo.write_text(FAKE_SUDO, encoding="utf-8")
    sudo.chmod(0o755)
    alert = case / "alert-task.py"
    alert.write_text(FAKE_ALERT, encoding="utf-8")
    alert.chmod(0o755)
    sudo_log, alert_log = case / "sudo.log", case / "alert.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(PATH=f"{case / 'bin'}:{env.get('PATH', '/usr/bin:/bin')}",
               HARNESS_STARTER=str(case / "pkg"), HARNESS_ALERT_TASK=str(alert),
               LOG_DIR=str(case / "log"), TMPDIR=str(case),
               STAND_SUDO_LOG=str(sudo_log), STAND_SUDO_CODE=str(code), STAND_ALERT_LOG=str(alert_log))
    call = [sys.executable, "-c", CALL, str(UPDATE)]
    if mode == "живой":
        env.pop("HARNESS_ALERT_TASK")
        (case / "scripts").mkdir()
        shutil.copy2(alert, case / "scripts" / "alert-task.py")
        call = [sys.executable, "-c", CALL_LIVE_WRITER, str(UPDATE), str(case)]
    if mode == "без-sudo":
        sudo.unlink()
        env["PATH"] = str(case / "bin")
    done = subprocess.run(call, env=env, cwd=case, capture_output=True, text=True, timeout=120)
    sudo_calls = sudo_log.read_text(encoding="utf-8").splitlines() if sudo_log.exists() else []
    alerts = [json.loads(l) for l in alert_log.read_text(encoding="utf-8").splitlines()] if alert_log.exists() else []
    return sudo_calls, alerts, done.returncode, done.stderr


def check_layout_steps(root: Path) -> None:
    """Удавшаяся раскладка: печать эталона, ПОТОМ карточки (карточки несут слой 1 из эталона);
    пересборка зовёт self-prompt.py --карточки --в <КОРЕНЬ>/.claude/agents."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    done = subprocess.run([sys.executable, "-c", CALL_ORDER, str(UPDATE)], env=env, cwd=root,
                          capture_output=True, text=True, timeout=120)
    try:
        steps = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        steps = f"не прочитано: код {done.returncode}; {done.stderr.strip()[-300:]}"
    cases.check("раскладка удалась: печать эталона, затем пересборка карточек", ["печать", "карточки"], steps)

    stand = root / "карточки"
    (stand / "scripts").mkdir(parents=True)
    recorder = stand / "scripts" / "self-prompt.py"
    recorder.write_text(FAKE_RECORDER, encoding="utf-8")
    record = stand / "record.log"
    done = subprocess.run([sys.executable, "-c", CALL_CARDS, str(UPDATE), str(stand)],
                          env=dict(env, STAND_RECORD_LOG=str(record)), cwd=root,
                          capture_output=True, text=True, timeout=120)
    calls = [json.loads(l) for l in record.read_text(encoding="utf-8").splitlines()] if record.exists() else []
    cases.check("пересборка: self-prompt.py --карточки --в <КОРЕНЬ>/.claude/agents",
                [["--карточки", "--в", str(stand / ".claude" / "agents")]], calls)


def cards_failure(root: Path, mode: str, derived: str = "") -> dict:
    """Один исход пересборки карточек: mode — «код-N», «таймаут», «oserror» или «откат»
    (раскладка вернула не успех, откат дошёл до итога; сборщик карточек отвечает 1).
    derived — имя упавшего сборщика производных рядом со сбоем карточек."""
    stand = root / f"карточки-{mode}{'-' + derived if derived else ''}"
    (stand / "scripts").mkdir(parents=True)
    for name, text in (("self-prompt.py", FAKE_CARDS), ("install-new.py", FAKE_INSTALL_NEW)):
        (stand / "scripts" / name).write_text(text, encoding="utf-8")
        (stand / "scripts" / name).chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(STAND_CARDS_CODE=mode.removeprefix("код-") if mode.startswith("код-") else "1",
               LOG_DIR=str(stand), TMPDIR=str(stand), STAND_DERIVED_FAILED=derived)
    done = subprocess.run([sys.executable, "-c", CALL_CARDS_FAIL, str(UPDATE), str(stand), mode],
                          env=env, cwd=stand, capture_output=True, text=True, timeout=120)
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"исключение": f"не прочитано: код {done.returncode}; {done.stderr.strip()[-300:]}", "упавшие": []}


def check_cards_failure(root: Path) -> None:
    """Н4: код не 0 и не 3, таймаут, OSError → «карточки помощников» в упавших, без исключения.
    Коды 1 и 2 оба: правка «== 1» вместо «не 0 и не 3» иначе осталась бы зелёной;
    код 3 и 0 — записи нет: правка «!= 0» сделала бы тревогой каждую установку без эталона."""
    for mode in ("код-1", "код-2", "таймаут", "oserror"):
        out = cards_failure(root, mode)
        cases.check(f"сбой карточек ({mode}): _разложить_пакет без исключения", "", out["исключение"])
        cases.check(f"сбой карточек ({mode}): «{CARDS_FAILED}» в упавших", True,
                    any(CARDS_FAILED in name for name in out["упавшие"]))
    for mode in ("код-0", "код-3"):
        out = cards_failure(root, mode)
        cases.check(f"карточки собраны ({mode}): без исключения, в упавших их нет", ("", False),
                    (out["исключение"], any(CARDS_FAILED in name for name in out["упавшие"])))
    # Сбой карточек дописывается к упавшим сборщикам производных, а не вытесняет их.
    out = cards_failure(root, "код-1", derived="scripts/build-стенд.py")
    cases.check("сбой карточек и упавший сборщик производных: в упавших оба", ("", True, True),
                (out["исключение"], any(CARDS_FAILED in name for name in out["упавшие"]),
                 "scripts/build-стенд.py" in out["упавшие"]))
    # Откат карточки не пересобирает — и не пишет их сбоем, даже если сборщик отвечал бы 1.
    out = cards_failure(root, "откат")
    cases.check("откат: без исключения, «карточки помощников» в упавших нет", ("", False),
                (out["исключение"], any(CARDS_FAILED in name for name in out["упавшие"])))


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="harness-run-update-self-seal-"))
    try:
        print(f"── update.py под пробой: {UPDATE} ──")
        sudo_calls, alerts, rc, err = run_case(root, 1)
        cases.check("отказ: обновление дошло до sudo self-seal", True,
                    any("/self-seal" in c for c in sudo_calls))
        cases.check("отказ: процесс не упал", (0, ""), (rc, "" if rc == 0 else err.strip()[-300:]))
        cases.check(f"отказ: ровно один сигнал с ключом {KEY}", [KEY], [key_of(a) for a in alerts])

        sudo_calls, alerts, rc, err = run_case(root, 0)
        cases.check("успех: обновление дошло до sudo self-seal", True,
                    any("/self-seal" in c for c in sudo_calls))
        cases.check("успех: сигналов нет", [], alerts)

        _, alerts, rc, err = run_case(root, 1, "живой")
        cases.check("отказ, писатель без двери (<КОРЕНЬ>/scripts/alert-task.py): ровно один сигнал",
                    ([KEY], 0), ([key_of(a) for a in alerts], rc))

        _, alerts, rc, err = run_case(root, 0, "без-sudo")
        cases.check("sudo нет вовсе: процесс не упал и ровно один сигнал",
                    (0, [KEY]), (rc, [key_of(a) for a in alerts]))

        check_layout_steps(root)
        check_cards_failure(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"итог test_update_self_seal: случаев {cases.paths}, неудач {cases.failures}")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
