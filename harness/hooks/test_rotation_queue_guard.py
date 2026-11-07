#!/usr/bin/env python3
"""Проба очереди запусков на время ожидания ротации (scripts/hooks/rotation_queue_guard.py).

Карточка hr.drain-queue-before-rotation, спека
docs/specs/2026-11-02-ротация-ждёт-помощников.md (итерация 2). Слово владельца
02.11.2026: «…кодом запрещать начинать выполнять любые новые задачи, только
класть в очередь».

Пока сторож держит флаг «ротация ждёт помощников», новая задача смены не
стартует: запуск помощника (Agent/Task) и новое поручение законченному
(SendMessage) отказаны честным текстом и легли строкой в очередь. Преемник
после смены получает очередь списком; записи помечаются выпущенными ПОСЛЕ
доставки (--mark) и не удаляются.

Что судит, на стенде (свой каталог состояния через HARNESS_PANEL_STATE — дверь
pause.py, свой LOG_DIR, конфиг установки — пустой):
  * флаг живой своей сессии — отказ кодом 2 своими словами, строка очереди со
    всеми полями; повтор того же поручения — одна строка (R-13);
  * битый, но живой флаг — флаг стоит;
  * флага нет / флаг чужой сессии (R-4) / протухший флаг (R-5) — пропуск, у
    двух последних — след в журнале;
  * управляющий (harness-lead) — пропуск со следом; иной помощник — в очередь (R-16);
  * SendMessage — в очередь (R-6); чужой инструмент — пропуск;
  * --release печатает и НЕ помечает, --mark помечает, после — пусто (R-13);
  * восемь параллельных запусков при выпуске — восемь строк (R-17);
  * путь флага по умолчанию — panel-state, куда пишет пользователь агента (R-1);
  * флаг ротации входящих владельца НЕ держит: pause.py --holds-inbox держит
    только на паузе владельца, отказ хука велит отвечать владельцу в канал,
    диспетчер спрашивает --holds-inbox (R-14);
  * хук зарегистрирован на Agent|Task|SendMessage.

Механизм берётся рядом с пробой либо из HARNESS_ROTATION_QUEUE_GUARD (дверь
мутаций). Прогон: python3 scripts/hooks/test_rotation_queue_guard.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = Path(os.environ.get("HARNESS_ROTATION_QUEUE_GUARD") or HERE / "rotation_queue_guard.py")
# Раскладка: в проекте scripts/hooks рядом со scripts/, в пакете harness/hooks
# рядом с harness/scripts/.
SCRIPTS = next((p for p in (HERE.parent, HERE.parent / "scripts") if (p / "pause.py").is_file()), HERE.parent)
PAUSE = SCRIPTS / "pause.py"
DISPATCHER = SCRIPTS / "tg-dispatcher.sh"
LIB = SCRIPTS / "lib"
SETTINGS = next((p for p in (HERE.parent.parent / ".claude" / "settings.json",
                             HERE / "project-settings.json") if p.is_file()), None)
FLAG = "rotation-waits-helpers.json"
QUEUE = "rotation-queue.jsonl"
SID = "5d2a9c1e-7b3f-4e10-9a8d-0c1b2a3f4e5d"
REFUSAL = "ждёт ротации"

checked = failed = 0


def check(name: str, expected, actual) -> None:
    global checked, failed
    checked += 1
    if expected == actual:
        print(f"  ок    {name}")
    else:
        failed += 1
        print(f"  ПЛОХО {name}: ждали «{expected}», вышло «{actual}»")


def stand() -> dict:
    root = Path(tempfile.mkdtemp(prefix="rotation-queue-"))
    (root / "state").mkdir()
    (root / "log").mkdir()
    env = dict(os.environ)
    env.update({"HARNESS_PANEL_STATE": str(root / "state"), "LOG_DIR": str(root / "log"),
                "HARNESS_INSTALL_CONF": str(root / "нет-install.conf"),
                "ROTATE_FLAG_STALE_MIN": "30", "PYTHONDONTWRITEBYTECODE": "1"})
    return {"root": root, "state": root / "state", "log": root / "log", "env": env}


def run(st: dict, payload: dict | None = None, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GUARD), *args], env=st["env"],
                          input=json.dumps(payload, ensure_ascii=False) if payload is not None else "",
                          capture_output=True, text=True, timeout=30)


def launch(description: str, agent_id: str = "", agent_type: str = "harness-lead",
           session: str = SID) -> dict:
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": session,
               "transcript_path": "/нет/такого.jsonl",
               "tool_input": {"description": description, "subagent_type": "harness-worker",
                              "model": "sonnet",
                              "prompt": f"Часть: hr.проба-очереди#2\n{description}: сделай дело"}}
    if agent_id:
        payload["agent_id"] = agent_id
        payload["agent_type"] = agent_type
    return payload


def send_message(to: str, message: str) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "SendMessage", "session_id": SID,
            "transcript_path": "/нет/такого.jsonl", "tool_input": {"to": to, "message": message}}


def put_flag(st: dict, text: str | None = None, session: str = SID, age_sec: int = 0) -> None:
    path = st["state"] / FLAG
    moment = time.time() - age_sec
    path.write_text(
        text if text is not None else json.dumps(
            {"сессия": session, "этап": "ждёт", "с": int(moment), "проверено": int(moment),
             "ждали_сек": 0, "причина": "окно ≈76% ≥ 75%", "помощников": 2, "принуждена": 0,
             "сказано": None}, ensure_ascii=False),
        encoding="utf-8")
    os.utime(path, (moment, moment))


def queue(st: dict) -> list:
    path = st["state"] / QUEUE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def traced(st: dict, event: str, outcome: str | None = None) -> bool:
    """Событие записано хуком в rotation.jsonl стенда (журнал ротации, источник — хук);
    с outcome — ещё и с этим исходом: панель отличает отказ от пропуска и от беды."""
    path = st["log"] / "rotation.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []
    return any(r.get("event") == event and r.get("src") == "scripts/hooks/rotation_queue_guard.py"
               and (outcome is None or r.get("outcome") == outcome) for r in rows)


def hook_with_vanished_flag(st: dict, payload: dict) -> int:
    """hook() в процессе, где флаг исчез между проверкой main() и state() (гонка
    со снятием флага сторожем): state() отвечает «нет»."""
    code = ("import importlib.util, json, sys\n"
            "spec = importlib.util.spec_from_file_location('rqg', sys.argv[1])\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "m.rotation_flag.state = lambda now=None: ('нет', {})\n"
            "sys.exit(m.hook(json.loads(sys.argv[2])))\n")
    return subprocess.run([sys.executable, "-c", code, str(GUARD), json.dumps(payload, ensure_ascii=False)],
                          env=st["env"], capture_output=True, text=True, timeout=30).returncode


def numbers(text: str) -> list[str]:
    return re.findall(r"^\s*(\d+)\.", text, re.MULTILINE)


def main() -> int:
    check("механизм есть по пути раскладки", True, GUARD.is_file())

    print("── БОЛЬНОЙ СЛУЧАЙ 02.11.2026: ротация назрела, смена запускает нового помощника ──")
    st = stand()
    put_flag(st)
    got = run(st, launch("проба-один"))
    # Код и текст одной проверкой: python без файла механизма тоже отвечает
    # кодом 2, и «отказ» без своих слов — не отказ хука.
    check("флаг стоит — отказ кодом 2 своими словами «ждёт ротации… в очереди»",
          (2, True, True),
          (got.returncode, REFUSAL in got.stderr, "в очереди" in got.stderr))
    check("отказ велит не повторять запуск и не делать задачу самой (R-13)", True,
          "не повторяй" in got.stderr and "не делай" in got.stderr)
    check("отказ велит отвечать владельцу в канал: флаг ротации не глушит владельца (R-14)", True,
          "отвечай ему в канал" in got.stderr)
    rows = queue(st)
    check("в очереди ровно одна строка", 1, len(rows))
    row = rows[0] if rows else {}
    check("строка несёт description", "проба-один", row.get("description"))
    check("строка несёт subagent_type", "harness-worker", row.get("subagent_type"))
    check("строка несёт model", "sonnet", row.get("model"))
    check("строка несёт prompt целиком", True, "проба-один: сделай дело" in str(row.get("prompt")))
    check("строка несёт часть из метки поручения", "Часть: hr.проба-очереди#2", row.get("часть"))
    check("строка несёт время", True, bool(row.get("время")))
    check("строка несёт сессию", SID, row.get("сессия"))
    check("строка несёт номер", 1, row.get("номер"))
    check("строка ещё не выпущена", None, row.get("выпущено"))
    check("отказ с постановкой в очередь оставил событие rotation_queued с outcome «refused»", True,
          traced(st, "rotation_queued", "refused"))

    run(st, launch("проба-два"))
    check("второй запуск — вторая строка, первая цела", ["проба-один", "проба-два"],
          [r.get("description") for r in queue(st)])
    got = run(st, launch("проба-один"))
    check("повтор того же поручения — отказ, но строка не задвоена (R-13)",
          (2, ["проба-один", "проба-два"]),
          (got.returncode, [r.get("description") for r in queue(st)]))

    print("── R-6: новое поручение законченному помощнику (SendMessage) — тоже в очередь ──")
    got = run(st, send_message("a0c57aae0ddba1100", "Теперь сделай ещё и вот это"))
    check("SendMessage при флаге — отказ своими словами", (2, True), (got.returncode, REFUSAL in got.stderr))
    last = queue(st)[-1] if queue(st) else {}
    check("строка SendMessage несёт to и message", ("a0c57aae0ddba1100", "Теперь сделай ещё и вот это"),
          (last.get("to"), last.get("message")))

    print("── флаг битый, но живой — всё равно флаг ──")
    st2 = stand()
    put_flag(st2, "{битый")
    got = run(st2, launch("при-битом"))
    check("битый флаг — отказ и строка очереди", (2, 1), (got.returncode, len(queue(st2))))
    check("у отказа при битом флаге свои слова", True, REFUSAL in got.stderr)

    print("── ЗДОРОВАЯ ПОЛОВИНА: флага нет — работа как раньше ──")
    st3 = stand()
    got = run(st3, launch("без-флага"))
    check("флага нет — пропуск", 0, got.returncode)
    check("флага нет — очереди нет", 0, len(queue(st3)))
    got = run(st3, send_message("a0c57aae0ddba1100", "продолжай"))
    check("флага нет — SendMessage проходит", 0, got.returncode)

    print("── R-4: флаг другой сессии — пропуск со следом ──")
    st4 = stand()
    put_flag(st4, session="ushedshaya-smena-0000")
    got = run(st4, launch("новая-смена"))
    check("флаг чужой сессии — пропуск, очереди нет", (0, 0), (got.returncode, len(queue(st4))))
    check("пропуск по чужому флагу оставил след rotation_flag_foreign", True,
          traced(st4, "rotation_flag_foreign"))

    print("── R-5: флаг протух (сторож не освежал 2 часа) — пропуск со следом ──")
    st5 = stand()
    put_flag(st5, age_sec=7200)
    got = run(st5, launch("при-протухшем"))
    check("протухший флаг — пропуск, очереди нет", (0, 0), (got.returncode, len(queue(st5))))
    check("протухший флаг оставил след rotation_flag_stale", True, traced(st5, "rotation_flag_stale"))
    check("след rotation_flag_stale — с outcome «error» (флаг брошен сторожем — беда, не пропуск)", True,
          traced(st5, "rotation_flag_stale", "error"))
    fail_open = st5["log"] / "guard_exceptions.log"
    check("протухший флаг записан в guard_exceptions.log (log_fail_open)", True,
          fail_open.exists() and "протух" in fail_open.read_text(encoding="utf-8"))

    print("── R-16: изнутри помощника — только управляющий ──")
    before = len(queue(st))
    got = run(st, launch("исполнитель-управляющего", agent_id="a0c57aae0ddba1100"))
    check("запуск из управляющего при флаге — пропуск", 0, got.returncode)
    check("запуск из управляющего в очередь не лёг (очередь до него не пуста)", (True, before),
          (before > 0, len(queue(st))))
    check("пропуск управляющего оставил след rotation_queue_lead_pass", True,
          traced(st, "rotation_queue_lead_pass"))
    check("след rotation_queue_lead_pass — с outcome «ok»", True, traced(st, "rotation_queue_lead_pass", "ok"))
    got = run(st, launch("запуск-исполнителя", agent_id="a1511aa2c0ffee001", agent_type="harness-worker"))
    check("запуск из помощника другого типа при флаге — отказ и строка", (2, before + 1),
          (got.returncode, len(queue(st))))

    before = len(queue(st))
    got = run(st, {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": SID,
                   "tool_input": {"command": "ls"}})
    check("чужой инструмент своей сессии при живом флаге — пропуск, очередь не растёт", (0, before),
          (got.returncode, len(queue(st))))

    print("── M-01: флаг исчез между проверками (сторож снял его) — пропуск, очереди нет ──")
    st9 = stand()
    code = hook_with_vanished_flag(st9, launch("флаг-исчез"))
    check("hook() при state() «нет» — пропуск (0), очередь не создана", (0, False),
          (code, (st9["state"] / QUEUE).exists()))

    print("── R-13: выпуск очереди преемнику — пометка только после доставки ──")
    total = len(queue(st))
    got = run(st, None, "--release")
    check("выпуск — код 0", 0, got.returncode)
    check("преемник получает задачи списком", True,
          "проба-один" in got.stdout and "проба-два" in got.stdout)
    check("список называет часть задачи", True, "hr.проба-очереди#2" in got.stdout)
    listed = numbers(got.stdout)
    check("список пронумерован по всем невыпущенным (список не пуст)", (True, total), (bool(listed), len(listed)))
    check("--release сам НЕ помечает (доставка ещё не подтверждена)", True,
          bool(queue(st)) and not any(r.get("выпущено") for r in queue(st)))
    again = run(st, None, "--release")
    check("доставка не подтверждена — второй выпуск печатает те же задачи", (True, listed),
          (bool(listed), numbers(again.stdout)))
    got = run(st, None, "--mark", *listed)
    check("--mark — код 0", 0, got.returncode)
    rows = queue(st)
    check("после пометки записи НЕ удалены (очередь не пуста)", (True, total), (total > 0, len(rows)))
    check("после пометки каждая запись помечена «выпущено»", True,
          bool(rows) and all(r.get("выпущено") for r in rows))
    check("prompt в файле сохранён после выпуска", True,
          bool(rows) and "проба-один: сделай дело" in str(rows[0].get("prompt")))
    again = run(st, None, "--release")
    check("после пометки выпуск пуст — задача не уходит преемнику дважды", ("", 0),
          (again.stdout.strip(), again.returncode))
    got = run(st3, None, "--release")
    check("выпуск без очереди — пусто, код 0", ("", 0), (got.stdout.strip(), got.returncode))

    print("── R-17: пачка запусков и выпуск одновременно — ни одна строка не теряется ──")
    st6 = stand()
    put_flag(st6)
    procs = []
    for n in range(8):
        procs.append(subprocess.Popen([sys.executable, str(GUARD)], env=st6["env"], stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True))
        if n == 3:
            procs.append(subprocess.Popen([sys.executable, str(GUARD), "--release"], env=st6["env"],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL))
    payloads = iter(json.dumps(launch(f"пачка-{n}"), ensure_ascii=False) for n in range(8))
    for proc in procs:
        if proc.stdin:
            try:
                proc.stdin.write(next(payloads))
                proc.stdin.close()
            except BrokenPipeError:
                pass    # механизма нет — python вышел, не читая; судит счёт строк ниже
    for proc in procs:
        proc.wait(timeout=60)
    check("8 параллельных запусков при выпуске — 8 строк очереди",
          sorted(f"пачка-{n}" for n in range(8)), sorted(r.get("description", "") for r in queue(st6)))

    print("── R-1: путь по умолчанию — туда, куда пишет пользователь агента ──")
    clean = {k: v for k, v in os.environ.items()
             if k not in ("HARNESS_PANEL_STATE", "STATE_DIR", "PANEL_STATE_DIR", "HARNESS_INSTALL_CONF",
                          "HARNESS_HOME", "LOG_DIR")}
    where = subprocess.run([sys.executable, str(GUARD), "--where"], env=clean, capture_output=True,
                           text=True, timeout=30)
    folder = Path(where.stdout.strip()) if where.returncode == 0 and where.stdout.strip() else None
    expected = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                               "import config; print(config.panel_state_dir())", str(LIB)],
                              env=clean, capture_output=True, text=True, timeout=30).stdout.strip()
    check("--where без окружения называет каталог состояния панели (config.panel_state_dir)",
          expected or "config молчит", str(folder) if folder else f"нет ответа (код {where.returncode})")
    check("пользователь агента может писать в каталог флага по умолчанию", True,
          bool(folder) and folder.is_dir() and os.access(folder, os.W_OK))

    print("── R-14: флаг ротации входящих владельца не держит, держит только пауза ──")
    st7 = stand()

    def holds(st_: dict) -> tuple[int, str]:
        got_ = subprocess.run([sys.executable, str(PAUSE), "--holds-inbox"], env=st_["env"],
                              capture_output=True, text=True, timeout=30)
        return got_.returncode, got_.stdout.strip()

    check("pause.py есть по раскладке", True, PAUSE.is_file())
    code, _ = holds(st7)
    check("ни паузы, ни флага — входящие не держатся (код 1)", 1, code)
    put_flag(st7)
    code, word = holds(st7)
    check("живой флаг ротации без паузы — входящие не держатся (код 1)", (1, ""), (code, word))
    idle = subprocess.run([sys.executable, str(PAUSE), "--идёт-ли"], env=st7["env"],
                          capture_output=True, text=True, timeout=30)
    check("--идёт-ли при одном флаге ротации — паузы нет, как раньше (код 1)", 1, idle.returncode)
    put_flag(st7, age_sec=7200)
    code, _ = holds(st7)
    check("протухший флаг входящих не держит (код 1)", 1, code)
    st8 = stand()
    put_flag(st8)
    subprocess.run([sys.executable, str(PAUSE), "--поставить", "проба", "--кто", "владелец"],
                   env=st8["env"], capture_output=True, text=True, timeout=30)
    code, word = holds(st8)
    check("пауза владельца при живом флаге ротации — входящие держатся (код 0, слово «пауза»)", (0, True), (code, "пауза" in word))
    held = ""
    if DISPATCHER.is_file():
        text = DISPATCHER.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"^pauza_derzhit\(\) \{.*?^\}", text, re.MULTILINE | re.DOTALL)
        held = match.group(0) if match else ""
    check("тело pauza_derzhit в диспетчере найдено", True, bool(held))
    check("диспетчер спрашивает --holds-inbox, а не только паузу", True, "--holds-inbox" in held)

    print("── регистрация хука ──")
    blocks = []
    if SETTINGS:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        blocks = [(set(m.get("matcher", "").split("|")), [h.get("command", "") for h in m.get("hooks", [])])
                  for m in data.get("hooks", {}).get("PreToolUse", [])]
    check("в настройках есть блок Agent|Task с хуками (список разобран, не пуст)", True,
          any({"Agent", "Task"} <= names and cmds for names, cmds in blocks))
    check("rotation_queue_guard.py зарегистрирован на Agent, Task и SendMessage", True,
          any({"Agent", "Task", "SendMessage"} <= names and any("rotation_queue_guard.py" in c for c in cmds)
              for names, cmds in blocks))

    print(f"\nОЧЕРЕДЬ НА ВРЕМЯ РОТАЦИИ: проверок {checked}, неудач {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
