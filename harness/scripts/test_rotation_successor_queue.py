#!/usr/bin/env python3
"""П3 (ротация с преемником): хук очереди при ротации с преемником.

Спека docs/specs/2026-11-02-ротация-ждёт-помощников.md, раздел 14.8. На этапе
«дорабатывает» преемник (флаг чужой сессии) запускает помощников свободно,
старая смена — отказ и строка очереди. Текст отказа старой смене обязан
назвать её дорабатывающей и НЕ велеть отвечать владельцу в канал: канал теперь
у преемника (раздел 14.7), а прежний текст раздела 5 говорил «отвечай ему в
канал как обычно».

Стенд свой: каталог флага и очереди (HARNESS_PANEL_STATE), LOG_DIR. Хук — из
живого файла (дверь мутаций HARNESS_ROTATION_QUEUE_GUARD). Итог — строка
«ПРОБА ЗЕЛЁНАЯ: …» / «ПРОБА КРАСНАЯ: …», код 0 / 1; стенд не годен — «ПРОБА
СЛОМАНА: …», код 2.
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
HOOK = Path(os.environ.get("HARNESS_ROTATION_QUEUE_GUARD")
            or next((p for p in (HERE / "hooks" / "rotation_queue_guard.py",
                                 HERE.parent / "hooks" / "rotation_queue_guard.py") if p.is_file()),
                    HERE / "hooks" / "rotation_queue_guard.py"))
OLD, NEW = "old-shift-0001", "new-shift-0002"
FLAG_NAME = "rotation-waits-helpers.json"
QUEUE_NAME = "rotation-queue.jsonl"

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, got: str = "") -> None:
    checks.append((name, ok, got))
    print(f"  {'ок   ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f": {got}"))


def stand() -> tuple[Path, dict]:
    root = Path(tempfile.mkdtemp(prefix="rot-successor-p3-"))
    atexit.register(shutil.rmtree, root, True)
    for sub in ("log", "hb", "panel"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "install.conf").write_text(
        f'LOG_DIR="{root}/log"\nHEARTBEAT_DIR="{root}/hb"\nPANEL_STATE_DIR="{root}/panel"\nSTATE_DIR="{root}"\n',
        encoding="utf-8")
    (root / "harness.conf").write_text("ROTATE_FLAG_STALE_MIN=30\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env.update({"HARNESS_INSTALL_CONF": str(root / "install.conf"), "HARNESS_CONF": str(root / "harness.conf"),
                "HARNESS_PANEL_STATE": str(root / "panel"), "LOG_DIR": str(root / "log"),
                "HARNESS_LOG_DIR": str(root / "log"), "PYTHONDONTWRITEBYTECODE": "1"})
    return root, env


def put_flag(root: Path, stage: str) -> None:
    now = int(time.time())
    (root / "panel" / FLAG_NAME).write_text(json.dumps({
        "сессия": OLD, "этап": stage, "с": now - 600, "проверено": now, "ждали_сек": 0,
        "причина": "стенд П3", "помощников": 7, "принуждена": 0, "сказано": None,
        "преемник": NEW, "tmux": "agent-old-1795209000", "поручено": now - 900,
        "с_доработки": now - 300}, ensure_ascii=False), encoding="utf-8")


def launch(env: dict, session: str, prompt: str) -> tuple[int, str]:
    payload = {"tool_name": "Agent", "session_id": session,
               "tool_input": {"description": "проба П3", "subagent_type": "harness-worker", "model": "sonnet",
                              "prompt": f"Часть: hr.proba#1\n{prompt}"}}
    done = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload, ensure_ascii=False),
                          capture_output=True, text=True, env=env, timeout=60)
    return done.returncode, done.stderr


def queue_rows(root: Path) -> list:
    try:
        return [json.loads(line) for line in (root / "panel" / QUEUE_NAME).read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except OSError:
        return []


def main() -> int:
    if not HOOK.is_file():
        print(f"ПРОБА СЛОМАНА: П3 — хука нет по пути {HOOK}")
        return 2
    root, env = stand()

    # Стенд годен, только если на этапе «идёт» хук держит старую смену, как сегодня.
    put_flag(root, "идёт")
    rc, err = launch(env, OLD, "задача на этапе идёт")
    if rc != 2 or len(queue_rows(root)) != 1:
        print(f"ПРОБА СЛОМАНА: П3 — на этапе «идёт» хук не отказал старой смене (код {rc}, строк очереди "
              f"{len(queue_rows(root))}): {err.strip()[:200]}")
        return 2

    print("── этап «дорабатывает» ──")
    put_flag(root, "дорабатывает")
    rc, err = launch(env, NEW, "задача преемника")
    check("преемник запускает помощника — код 0", rc == 0, f"код {rc}: {err.strip()[:160]}")
    check("запуск преемника в очередь не лёг", len(queue_rows(root)) == 1, f"строк {len(queue_rows(root))}")

    rc, err = launch(env, OLD, "новая задача старой смены")
    check("старая смена — отказ, код 2", rc == 2, f"код {rc}")
    rows = queue_rows(root)
    check("запуск старой смены — строка в очереди преемника", len(rows) == 2 and rows[-1].get("сессия") == OLD,
          f"строк {len(rows)}")
    text = err.lower()
    check("текст отказа называет старую смену дорабатывающей", "дорабатывающ" in text, err.strip()[:200])
    check("текст отказа называет преемника", "преемник" in text, err.strip()[:200])
    check("текст отказа НЕ велит отвечать владельцу в канал (канал у преемника)",
          "в канал как обычно" not in text, err.strip()[:200])

    failed = [c for c in checks if not c[1]]
    if failed:
        print(f"ПРОБА КРАСНАЯ: П3 хук очереди — неудач {len(failed)} из {len(checks)}; первая: {failed[0][0]}")
        return 1
    print(f"ПРОБА ЗЕЛЁНАЯ: П3 хук очереди — путей {len(checks)}, преемник запускает, старая — в очередь")
    return 0


if __name__ == "__main__":
    sys.exit(main())
