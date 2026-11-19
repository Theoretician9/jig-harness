#!/usr/bin/env python3
"""Проба: потолок повода «о-чём-сообщение-владельца» покрывает поток владельца.

Больной случай 14.11.2026: потолок повода стоял 30 вызовов в сутки, а
владелец пишет до 82 сообщений в сутки (07.11). Потолок кончался к вечеру, и
каждое следующее сообщение map-from-message.py судил БЕЗ модели — по первой
строке. За 07–14.11 так завелось 124 карточки, из них 24 — 14.11 («Что
происходит», «Ответ им отправил?»). Haiku на этот вопрос стоит 0,002 $ и 7 с.

══════════════════════════════ КОНТРАКТ ══════════════════════════════════════
`ask-the-model.py --повод о-чём-сообщение-владельца --вопрос-файл Ф`:
 • за сутки уже 79 вызовов этого повода (пиковый день 07.11: 29 судила модель
   и 50 завелись вслепую) — модель зовётся, ответ по схеме, код 0;
 • за сутки уже 1000 вызовов (цикл зовущего, а не поток владельца) — отказ
   потолком, код 3, модель не зовётся.
═════════════════════════════════════════════════════════════════════════════

Стенд: свой LOG_DIR с журналом вызовов, модель — заглушка CLAUDE_BIN (пишет
метку вызова), сборщик личности — заглушка SELF_PROMPT_PY, сигнал —
HARNESS_ALERT_TASK=/bin/true. Живой журнал вызовов не читается и не пишется.
Механизм находится РЯДОМ с пробой (ask-the-model.py и корень дерева с
harness/config/model-call-reasons.yaml) — для мутаций проба зовётся из копии.
Прогон: python3 scripts/test_owner_message_ceiling.py; код 0 — зелёный.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True

ASK = Path(__file__).resolve().parent / "ask-the-model.py"
REASON = "о-чём-сообщение-владельца"
PEAK_DAY_CALLS = 79
RUNAWAY_CALLS = 1000

ANSWER = {"задачи": [], "имена": [], "слои": [], "указания": [],
          "вопросы": ["что происходит"], "ответы": []}


def stand(root: Path, prior_calls: int) -> dict:
    log_dir = root / "log"
    log_dir.mkdir()
    now = time.time()
    with open(log_dir / "модель-вызовы.jsonl", "w", encoding="utf-8") as fh:
        for i in range(prior_calls):
            fh.write(json.dumps({"время": now - 60 - i, "зачем": REASON,
                                 "модель": "haiku", "по_схеме": True},
                                ensure_ascii=False) + "\n")
    marker = root / "model-called"
    envelope = json.dumps({"result": json.dumps(ANSWER, ensure_ascii=False),
                           "total_cost_usd": 0.002, "num_turns": 1},
                          ensure_ascii=False)
    claude = root / "claude"
    claude.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat <<'EOF'\n{envelope}\nEOF\n",
                      encoding="utf-8")
    claude.chmod(0o755)
    self_prompt = root / "self-prompt.py"
    self_prompt.write_text(
        "import sys\nopen(sys.argv[sys.argv.index('--в') + 1], 'w')"
        ".write('Метка сборки: проба\\n')\n", encoding="utf-8")
    question = root / "вопрос.txt"
    question.write_text("СООБЩЕНИЕ ВЛАДЕЛЬЦА:\nЧто происходит", encoding="utf-8")
    return {"log_dir": log_dir, "marker": marker, "claude": claude,
            "self_prompt": self_prompt, "question": question}


def ask(root: Path, prior_calls: int):
    s = stand(root, prior_calls)
    env = dict(os.environ, LOG_DIR=str(s["log_dir"]), CLAUDE_BIN=str(s["claude"]),
               SELF_PROMPT_PY=str(s["self_prompt"]), HARNESS_ALERT_TASK="/bin/true",
               CLAUDE_CODE_OAUTH_TOKEN="проба", PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, str(ASK), "--повод", REASON,
                           "--вопрос-файл", str(s["question"]), "--таймаут", "60"],
                          capture_output=True, text=True, env=env, timeout=120)
    return done, s["marker"].exists()


def main() -> int:
    bad = 0

    def case(name: str, ok: bool, detail: str) -> None:
        nonlocal bad
        bad += not ok
        print(("  ок    " if ok else "  ПЛОХО ") + name + ("" if ok else f" — {detail}"))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "пик"
        root.mkdir()
        done, called = ask(root, PEAK_DAY_CALLS)
        try:
            answer = json.loads(done.stdout)
        except ValueError:
            answer = None
        case(f"БОЛЬНОЙ СЛУЧАЙ: {PEAK_DAY_CALLS} вызовов за сутки (пик 07.11) — "
             f"сообщение судит модель",
             done.returncode == 0 and called and answer == ANSWER,
             f"код {done.returncode}, модель звана: {called}, "
             f"stderr: {done.stderr.strip()[:160]}")

        root = Path(tmp) / "цикл"
        root.mkdir()
        done, called = ask(root, RUNAWAY_CALLS)
        case(f"{RUNAWAY_CALLS} вызовов за сутки (цикл зовущего) — отказ потолком",
             done.returncode == 3 and not called and "при потолке" in done.stderr,
             f"код {done.returncode}, модель звана: {called}")

    print(f"проба потолка повода «{REASON}»: неудач {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
