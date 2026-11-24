#!/usr/bin/env python3
"""П2 (ротация с преемником): дорабатывающей смене канал и входящие закрыты.

Спека docs/specs/2026-11-02-ротация-ждёт-помощников.md, раздел 14.7. Ротация с
преемником оставляет старую смену живой («дорабатывает»): она доводит своих
помощников, а канал, входящие и STATE.md — у преемника. guard_bash знает
session_id вызова и по флагу ротации отказывает старой смене в tg_send.sh,
путях inbox/ и записи STATE.md; преемнику и прочим командам старой — проход.

Стенд свой: install.conf, LOG_DIR, каталог флага (HARNESS_PANEL_STATE) во
временном каталоге. Сторож — из живого файла (дверь мутаций GUARD_UNDER_TEST).
Итог — строка «ПРОБА ЗЕЛЁНАЯ: …» / «ПРОБА КРАСНАЯ: …», код 0 / 1; стенд не
годен — «ПРОБА СЛОМАНА: …», код 2.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Проект: scripts/hooks/; пакет: harness/hooks/ рядом с harness/scripts/.
GUARD = Path(os.environ.get("GUARD_UNDER_TEST")
             or next((p for p in (HERE / "hooks" / "guard_bash.py", HERE.parent / "hooks" / "guard_bash.py")
                      if p.is_file()), HERE / "hooks" / "guard_bash.py"))
OLD, NEW = "old-shift-0001", "new-shift-0002"
FLAG_NAME = "rotation-waits-helpers.json"

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, got: str = "") -> None:
    checks.append((name, ok, got))
    print(f"  {'ок   ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f": {got}"))


def stand() -> tuple[Path, dict]:
    root = Path(tempfile.mkdtemp(prefix="rot-successor-p2-"))
    atexit.register(shutil.rmtree, root, True)
    for sub in ("log/inbox/обработано", "hb", "panel", "proj/.tasks/x"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "install.conf").write_text(
        f'LOG_DIR="{root}/log"\nHEARTBEAT_DIR="{root}/hb"\nPANEL_STATE_DIR="{root}/panel"\n'
        f'STATE_DIR="{root}"\nPROJECT_DIR="{root}/proj"\nAUTONOMY="semi"\n', encoding="utf-8")
    (root / "harness.conf").write_text("ROTATE_FLAG_STALE_MIN=30\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_SESSION_ID", "TMUX", "TMUX_PANE")}
    env.update({"HARNESS_INSTALL_CONF": str(root / "install.conf"), "HARNESS_CONF": str(root / "harness.conf"),
                "HARNESS_PANEL_STATE": str(root / "panel"), "LOG_DIR": str(root / "log"),
                "HARNESS_LOG_DIR": str(root / "log"), "PYTHONDONTWRITEBYTECODE": "1"})
    return root, env


def put_flag(root: Path, stage: str | None) -> None:
    path = root / "panel" / FLAG_NAME
    if stage is None:
        path.unlink(missing_ok=True)
        return
    now = int(time.time())
    path.write_text(json.dumps({
        "сессия": OLD, "этап": stage, "с": now - 600, "проверено": now, "ждали_сек": 0,
        "причина": "стенд П2", "помощников": 2, "принуждена": 0, "сказано": None,
        "преемник": NEW, "tmux": "agent-old-1795209000", "поручено": now - 900,
        "с_доработки": now - 300}, ensure_ascii=False), encoding="utf-8")


def guard(env: dict, cwd: Path, session: str, command: str) -> tuple[int, str]:
    payload = {"tool_name": "Bash", "session_id": session, "tool_input": {"command": command}}
    done = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload, ensure_ascii=False),
                          capture_output=True, text=True, env=env, cwd=str(cwd), timeout=60)
    return done.returncode, done.stderr


def main() -> int:
    if not GUARD.is_file():
        print(f"ПРОБА СЛОМАНА: П2 — сторожа нет по пути {GUARD}")
        return 2
    root, env = stand()
    log = root / "log"
    tg = f'bash {root}/proj/scripts/tg_send.sh "отчёт владельцу"'
    inbox_read = f"cat {log}/inbox/1795241794.055998119.txt"
    inbox_move = f"mv {log}/inbox/1795241794.055998119.txt {log}/inbox/обработано/"
    journal = f"echo 'шаг 2: помощник закончил' >> {root}/proj/.tasks/x/журнал.md"
    state_write = f"echo 'Сейчас: …' > {root}/proj/STATE.md"
    cwd = root / "proj"

    # Стенд годен, только если без флага те же команды проходят: иначе красное
    # значило бы чужой отказ сторожа, а не отсутствие правила.
    put_flag(root, None)
    for name, cmd in (("tg_send.sh", tg), ("чтение inbox", inbox_read), ("перенос inbox", inbox_move),
                      ("журнал задачи", journal), ("запись STATE.md", state_write)):
        rc, err = guard(env, cwd, OLD, cmd)
        if rc != 0:
            print(f"ПРОБА СЛОМАНА: П2 — без флага сторож отказал в «{name}» (код {rc}): {err.strip()[:300]}")
            return 2

    print("── флаг «дорабатывает»: старая смена ──")
    put_flag(root, "дорабатывает")
    for name, cmd in (("tg_send.sh — отказ", tg), ("чтение inbox/ — отказ", inbox_read),
                      ("перенос inbox/ в обработано — отказ", inbox_move),
                      ("запись STATE.md — отказ", state_write)):
        rc, err = guard(env, cwd, OLD, cmd)
        check(f"старая смена: {name} (код 2)", rc == 2, f"код {rc}")
        check(f"старая смена: {name} — текст называет её дорабатывающей",
              "дорабатывающ" in err.lower(), err.strip()[:160] or "stderr пуст")
    rc, err = guard(env, cwd, OLD, journal)
    check("старая смена: журнал задачи — проход", rc == 0, f"код {rc}: {err.strip()[:160]}")
    rc, err = guard(env, cwd, OLD, "git log -1 --format=%h")
    check("старая смена: чтение git — проход", rc == 0, f"код {rc}: {err.strip()[:160]}")

    print("── флаг «дорабатывает»: преемник ──")
    for name, cmd in (("tg_send.sh", tg), ("чтение inbox/", inbox_read), ("запись STATE.md", state_write)):
        rc, err = guard(env, cwd, NEW, cmd)
        check(f"преемник: {name} — проход", rc == 0, f"код {rc}: {err.strip()[:160]}")

    print("── этап «идёт» (передача ещё пишется) — канал у старой смены ──")
    put_flag(root, "идёт")
    rc, err = guard(env, cwd, OLD, tg)
    check("этап «идёт»: tg_send.sh старой смены — проход (отчёт о передаче)", rc == 0,
          f"код {rc}: {err.strip()[:160]}")

    failed = [c for c in checks if not c[1]]
    if failed:
        print(f"ПРОБА КРАСНАЯ: П2 guard_bash — неудач {len(failed)} из {len(checks)}; первая: {failed[0][0]}")
        return 1
    print(f"ПРОБА ЗЕЛЁНАЯ: П2 guard_bash — путей {len(checks)}, дорабатывающей канал и входящие закрыты")
    return 0


if __name__ == "__main__":
    sys.exit(main())
