#!/usr/bin/env python3
"""Проба: перезапуск, убивающий помощников, при идущих помощниках — отказ
(scripts/hooks/guard_bash.py; карточка hr.block-restart-with-active-helpers,
спека docs/specs/2026-11-02-ротация-ждёт-помощников.md, разделы 2, 9, 10).

Слово владельца 02.11.2026: «Перезапуск при работающих помощниках нельзя
делать, это не просьба, это должно быть системной частью харнеса».

Почему именно harness-agent и перезагрузка. Помощники — подагенты внутри
процесса claude смены. Юнит harness-agent останавливается ExecStop=tmux
kill-session -t agent: панель гибнет вместе с claude и всеми его помощниками;
перезагрузка машины — тем более. Юнит harness-dispatcher держит только
tg-dispatcher.sh — его перезапуск помощников не задевает и остаётся как был.

«Идущий помощник» — ОДНО определение на харнес: stall-in-log.py --работа.
Идёт = запуск без результата в транскрипте родителя (нет уведомления о конце
фонового запуска, нет tool_result у запуска не фоном). Свежесть ответа не
признак: помощник 25 минут ждёт лог ворот и не пишет ничего (замер ревью: до
49 мин). Завис = идёт, но молчит дольше HELPER_HUNG_MIN — тоже отказ, со словом
«завис». Судится транскрипт СМЕНЫ (свежайший с entrypoint cli), а не
вызывающего хук.

Стенд на каждый случай свой: каталог транскриптов проекта (CLAUDE_PROJECTS_DIR),
паспорт установки с PROJECT_DIR и LOG_DIR, журнал входящих владельца для двери
(ключ стенда HARNESS_OWNER_WORD_STEND=1). Сторож берётся рядом с пробой либо из
GUARD_UNDER_TEST (дверь мутаций, как у test_guard_bash.py).
Прогон: python3 scripts/hooks/test_guard_bash_restart_helpers.py
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = Path(os.environ.get("GUARD_UNDER_TEST") or HERE / "guard_bash.py")
SID = "5d2a9c1e-7b3f-4e10-9a8d-0c1b2a3f4e5d"
HEADLESS_SID = "9ef19392-0000-4000-8000-000000000001"
HELPER = "a1511aa2c0ffee001"
PROJECT = "/var/www/proba-smena"
NOW = float(int(time.time()))
ROOT = Path(tempfile.mkdtemp(prefix="guard-restart-helpers-"))

checked = failed = 0


def check(name: str, expected, actual) -> None:
    global checked, failed
    checked += 1
    if expected == actual:
        print(f"  ок    {name}")
    else:
        failed += 1
        print(f"  ПЛОХО {name}: ждали «{expected}», вышло «{actual}»")


def stamp(minutes_ago: float) -> str:
    moment = datetime.datetime.fromtimestamp(NOW - minutes_ago * 60, datetime.timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def rec(kind: str, minutes_ago: float, entrypoint: str = "cli", **extra) -> dict:
    record = {"type": kind, "timestamp": stamp(minutes_ago), "sessionId": SID, "entrypoint": entrypoint}
    record.update(extra)
    return record


def say(minutes_ago: float, **extra) -> dict:
    return rec("assistant", minutes_ago, message={"role": "assistant", "content": [{"type": "text", "text": "Шаг."}]},
               **extra)


def agent_call(tool_id: str, description: str, minutes_ago: float, background: bool) -> dict:
    return rec("assistant", minutes_ago, message={"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": "Agent",
         "input": {"description": description, "prompt": "задание стенда", "subagent_type": "harness-worker",
                   "run_in_background": background}}]})


def async_launch(minutes_ago: float) -> list:
    tool_id = "toolu_stand_" + HELPER
    return [agent_call(tool_id, "проба-помощник", minutes_ago, True),
            rec("user", minutes_ago, message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "Async agent launched successfully."}]},
                toolUseResult={"status": "async_launched", "agentId": HELPER, "description": "проба-помощник"})]


def notification(minutes_ago: float) -> dict:
    return rec("user", minutes_ago, message={"role": "user", "content":
               f"<task-notification>\n<task-id>{HELPER}</task-id>\n<status>completed</status>\n</task-notification>"})


def helper_bash_call(minutes_ago: float) -> dict:
    return rec("assistant", minutes_ago, agentId=HELPER, isSidechain=True, message={"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_bash_1", "name": "Bash",
         "input": {"command": "for i in $(seq 1 38); do tail -1 $L | grep -q ВОРОТА-КОНЕЦ && break; sleep 60; done"}}]})


def helper_tool_result(minutes_ago: float) -> dict:
    return rec("user", minutes_ago, agentId=HELPER, isSidechain=True, message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_bash_1", "content": "готово"}]})


def write_lines(path: Path, records: list, minutes_ago: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    os.utime(path, (NOW - minutes_ago * 60, NOW - minutes_ago * 60))


def scenario(name: str, main: list | None, helper: list | None = None, helper_quiet_min: float = 0,
             inbox: str = "", headless: bool = False, rotating: bool = False) -> dict:
    """Стенд одного случая. main=None — транскрипта смены нет вовсе."""
    root = ROOT / name
    folder = root / "projects" / PROJECT.replace("/", "-")
    folder.mkdir(parents=True)
    (root / "log").mkdir()
    (root / "log" / "inbox.jsonl").write_text(inbox, encoding="utf-8")
    if rotating:
        (root / "log" / "rotation.jsonl").write_text(json.dumps(
            {"ts": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z"), "event": "rotate_command",
             "session": SID, "detail": "стенд"}, ensure_ascii=False) + "\n", encoding="utf-8")
    (root / "install.conf").write_text(f'PROJECT_DIR="{PROJECT}"\nLOG_DIR="{root / "log"}"\n', encoding="utf-8")
    transcript = folder / f"{SID}.jsonl"
    if main is not None:
        write_lines(transcript, main + [say(0.5)], 0.5)
    if helper is not None:
        sub = folder / SID / "subagents"
        write_lines(sub / f"agent-{HELPER}.jsonl", helper, helper_quiet_min)
        (sub / f"agent-{HELPER}.meta.json").write_text(
            json.dumps({"description": "проба-помощник"}, ensure_ascii=False), encoding="utf-8")
    caller = transcript
    if headless:
        # Свежее транскрипта смены: проверка приёма сторожа, headless, без помощников.
        caller = folder / f"{HEADLESS_SID}.jsonl"
        write_lines(caller, [rec("assistant", 0.1, entrypoint="sdk-cli",
                                 message={"role": "assistant", "content": [{"type": "text", "text": "приём"}]})], 0.1)
    return {"root": root, "caller": caller}


def guard(command: str, st: dict, caller: str | None = None, guard_path: Path = GUARD) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({"LOG_DIR": str(st["root"] / "log"), "HELPER_FRESH_MIN": "20", "HELPER_HUNG_MIN": "120",
                "HARNESS_INSTALL_CONF": str(st["root"] / "install.conf"),
                "CLAUDE_PROJECTS_DIR": str(st["root"] / "projects"),
                "HARNESS_OWNER_WORD_STEND": "1", "CONFIRM_MAX_AGE_MIN": "60",
                "PYTHONDONTWRITEBYTECODE": "1"})
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": SID,
               "transcript_path": str(st["caller"]) if caller is None else caller,
               "tool_input": {"command": command}}
    return subprocess.run([sys.executable, str(guard_path)], input=json.dumps(payload, ensure_ascii=False),
                          env=env, capture_output=True, text=True, timeout=60)


def inbox_line(text: str) -> str:
    when = datetime.datetime.now().astimezone()
    return json.dumps({"ts": when.isoformat(), "chat_id": "1000000001", "текст": text, "message_id": "777"},
                      ensure_ascii=False) + "\n"


def broken_instrument_guard(name: str = "broken",
                            body: str = "import sys\nraise SystemExit('прибор стенда упал')\n") -> Path:
    """Копия сторожа в раскладке проекта с прибором-заглушкой: падает, молчит
    дольше предела или несёт мусор (R-12, M-02)."""
    lib = next((p for p in (GUARD.parent.parent / "lib", GUARD.parent.parent / "scripts" / "lib")
                if p.is_dir()), None)
    copy = ROOT / name / "scripts"
    (copy / "hooks").mkdir(parents=True)
    shutil.copy2(GUARD, copy / "hooks" / "guard_bash.py")
    if lib:
        shutil.copytree(lib, copy / "lib")
    (copy / "stall-in-log.py").write_text(body, encoding="utf-8")
    return copy / "hooks" / "guard_bash.py"


def main() -> int:
    check("сторож есть по пути раскладки", True, GUARD.is_file())
    live = scenario("живой", async_launch(40), [say(1, agentId=HELPER, isSidechain=True)], 1)

    print("── БОЛЬНОЙ СЛУЧАЙ 02.11.2026: помощник работает, смена перезапускает агента ──")
    for command in ("systemctl restart harness-agent",
                    "sudo systemctl restart harness-agent",
                    "sudo -n systemctl restart harness-agent.service",
                    "systemctl stop harness-agent",
                    "cd /tmp && systemctl restart harness-agent"):
        got = guard(command, live)
        check(f"«{command}» при идущем помощнике — отказ", 2, got.returncode)
        check(f"«{command}»: отказ называет помощников", True, "помощник" in got.stderr)
    got = guard("systemctl restart harness-agent", live)
    check("отказ называет законную дверь", True, "двер" in got.stderr)
    check("отказ называет самого помощника по описанию", True, "проба-помощник" in got.stderr)
    check("отказ говорит, что перезапуск делает смена сама после помощников (R-19)", True,
          "смена сама" in got.stderr)

    print("── R-11: остальные формы, обрывающие помощников ──")
    for command in ("reboot", "sudo reboot", "sudo shutdown -r now", "systemctl reboot", "sudo systemctl poweroff",
                    "sudo service harness-agent restart",
                    "sudo systemctl restart harness-panel harness-agent",
                    "systemctl --no-block restart harness-agent",
                    "sudo systemctl restart 'harness-agent.service'"):
        got = guard(command, live)
        check(f"«{command}» при идущем помощнике — отказ своими словами", (2, True),
              (got.returncode, "помощник" in got.stderr))

    print("── R-2: «идёт» = нет результата, а не «свежий ответ» ──")
    in_tool = scenario("в-инструменте", async_launch(60),
                       [say(30, agentId=HELPER, isSidechain=True), helper_bash_call(25)], 25)
    got = guard("systemctl restart harness-agent", in_tool)
    check("помощник 25 мин ждёт лог ворот (tool_use без результата) — идёт, отказ", 2, got.returncode)
    quiet = scenario("молчит", async_launch(60),
                     [helper_bash_call(30), helper_tool_result(26), say(25, agentId=HELPER, isSidechain=True)], 25)
    got = guard("systemctl restart harness-agent", quiet)
    check("помощник молчит 25 мин, уведомления о конце нет — идёт, отказ", 2, got.returncode)
    sync_open = scenario("не-фоном", [agent_call("toolu_sync_1", "синхронный-помощник", 30, False)],
                         [say(1, agentId=HELPER, isSidechain=True)], 1)
    got = guard("systemctl restart harness-agent", sync_open)
    check("запуск не фоном без tool_result — идёт, отказ", 2, got.returncode)
    check("отказ называет помощника, запущенного не фоном", True, "синхронный-помощник" in got.stderr)
    hung = scenario("завис", async_launch(200), [say(130, agentId=HELPER, isSidechain=True)], 130)
    got = guard("systemctl restart harness-agent", hung)
    check("помощник молчит 130 мин (> HELPER_HUNG_MIN=120) — отказ со словом «завис»", (2, True),
          (got.returncode, "завис" in got.stderr))

    print("── ЗДОРОВАЯ ПОЛОВИНА: помощников нет или они закончили — как раньше ──")
    none = scenario("пусто", [])
    got = guard("systemctl restart harness-agent", none)
    check("помощников не было — перезапуск агента проходит", 0, got.returncode)
    done = scenario("уведомил", async_launch(60) + [notification(5)], [say(6, agentId=HELPER, isSidechain=True)], 6)
    got = guard("systemctl restart harness-agent", done)
    check("помощник уведомил о конце — перезапуск проходит", 0, got.returncode)
    sync_done = scenario("не-фоном-готов", [
        agent_call("toolu_sync_1", "синхронный-помощник", 40, False),
        rec("user", 30, message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_sync_1", "content": "готово"}]},
            toolUseResult={"status": "completed", "agentId": HELPER})],
        [say(31, agentId=HELPER, isSidechain=True)], 31)
    got = guard("systemctl restart harness-agent", sync_done)
    check("запуск не фоном вернул tool_result — перезапуск проходит", 0, got.returncode)
    nothing = scenario("нет-смены", None)
    got = guard("systemctl restart harness-agent", nothing, caller="")
    check("транскрипта смены нет — судить нечего, проходит", 0, got.returncode)

    print("── R-10: судится смена, а не вызывающий хук ──")
    headless = scenario("headless", async_launch(40), [say(1, agentId=HELPER, isSidechain=True)], 1, headless=True)
    got = guard("systemctl restart harness-agent", headless)
    check("вызов из свежего headless-транскрипта без помощников — у смены помощник идёт, отказ",
          2, got.returncode)
    got = guard("systemctl restart harness-agent", live, caller="")
    check("пустой transcript_path вызывающего — судится смена, отказ", 2, got.returncode)

    print("── R-12: прибор упал — незнание не разрешение ──")
    if GUARD.is_file():
        broken = broken_instrument_guard()
        got = guard("systemctl restart harness-agent", live, guard_path=broken)
        check("прибор упал — отказ своими словами", (2, True), (got.returncode, "помощник" in got.stderr))
    else:
        check("прибор упал — отказ своими словами", (2, True), "сторожа нет — проверять нечего")

    print("── M-02: прибор молчит дольше предела или несёт мусор — незнание не разрешение ──")
    if GUARD.is_file():
        # 30 с: дольше предела прибора в стороже и меньше тайм-аута вызова сторожа пробой (60 с).
        slow = broken_instrument_guard("slow", "import time\ntime.sleep(30)\n")
        got = guard("systemctl restart harness-agent", live, guard_path=slow)
        check("прибор спит дольше предела — отказ", 2, got.returncode)
        junk = broken_instrument_guard("junk", "print('мусор вместо счёта')\nprint('помощников\\tмного')\n")
        got = guard("systemctl restart harness-agent", live, guard_path=junk)
        check("прибор печатает мусор с кодом 0 — отказ", 2, got.returncode)
    else:
        check("прибор спит дольше предела — отказ", 2, "сторожа нет — проверять нечего")
        check("прибор печатает мусор с кодом 0 — отказ", 2, "сторожа нет — проверять нечего")

    print("── 5а: дверь — слово владельца с названием перезапуска ──")
    word = scenario("слово", async_launch(40), [say(1, agentId=HELPER, isSidechain=True)], 1,
                    inbox=inbox_line("перезапусти агента, помощников не жалко"))
    got = guard("systemctl restart harness-agent", word)
    check("свежее слово владельца «перезапусти агента» — дверь открыта", 0, got.returncode)
    bare = scenario("голое-да", async_launch(40), [say(1, agentId=HELPER, isSidechain=True)], 1,
                    inbox=inbox_line("да"))
    got = guard("systemctl restart harness-agent", bare)
    check("голое «да» владельца дверь не открывает", 2, got.returncode)

    print("── F-02: слово владельца — дверь только при согласии сейчас ──")
    for n, (phrase, expected) in enumerate((("перезапусти агента потом, когда помощники закончат", 2),
                             ("ребут пока не делай, агент занят", 2),
                             ("ребут сервера не нужен", 2),
                             ("перезапусти агента завтра", 2),
                             ("перезапусти агента", 0),
                             ("перезапусти, помощников не жалко", 0))):
        said = scenario(f"слово-{n}", async_launch(40),
                        [say(1, agentId=HELPER, isSidechain=True)], 1, inbox=inbox_line(phrase))
        got = guard("systemctl restart harness-agent", said)
        check(f"слово владельца «{phrase}» — {'дверь открыта' if expected == 0 else 'дверь закрыта'}",
              expected, got.returncode)

    print("── F-01: агент сам дописывает --owner к --rotate-now ──")
    for stand_, expected, name in ((live, 2, "помощник идёт, слова владельца нет — отказ"),
                                   (word, 0, "свежее слово владельца — дверь открыта")):
        got = guard("bash harness/demons/session-warden.sh --rotate-now --owner", stand_)
        check(f"«session-warden.sh --rotate-now --owner»: {name}", expected, got.returncode)

    print("── R-3: «выход» во время ротации сторожа — тоже при помощниках нельзя ──")
    rot_live = scenario("ротация-живой", async_launch(40), [say(1, agentId=HELPER, isSidechain=True)], 1,
                        rotating=True)
    got = guard("pkill claude", rot_live)
    check("ротация ведётся, помощник идёт — «pkill claude» отказан", 2, got.returncode)
    rot_none = scenario("ротация-пусто", [], rotating=True)
    got = guard("pkill claude", rot_none)
    check("ротация ведётся, помощников нет — «pkill claude» проходит, как раньше", 0, got.returncode)

    print("── F-03: перезапуск после heredoc в той же команде ──")
    got = guard("cat > /tmp/note.txt <<'EOF'\nпервая строка заметки\nвторая строка\nEOF\n"
                "systemctl restart harness-agent", live)
    check("heredoc, затем «systemctl restart harness-agent» при идущем помощнике — отказ", 2, got.returncode)

    print("── перезапуски, не задевающие помощников, — как раньше ──")
    for command in ("systemctl restart harness-dispatcher",
                    "sudo -n systemctl restart harness-panel",
                    "systemctl status harness-agent",
                    "systemctl is-active harness-agent",
                    "sudo shutdown -c",
                    "grep 'systemctl restart harness-agent' docs/x.md",
                    "echo reboot"):
        got = guard(command, live)
        check(f"«{command}» при идущем помощнике — проходит", 0, got.returncode)

    shutil.rmtree(ROOT, ignore_errors=True)
    print(f"\nПЕРЕЗАПУСК ПРИ ПОМОЩНИКАХ: проверок {checked}, неудач {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
