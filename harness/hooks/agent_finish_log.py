#!/usr/bin/env python3
"""SubagentStop: строка agent.finish в журнал вызовов — конец запуска помощника.

Статус строит дерево запусков по agent.launch (data.вызов) и закрывает ветку по
agent.finish с тем же вызовом. PostToolUse на Agent для конца не годится: фоновый
помощник возвращается сразу после запуска. Вызов узнаётся по meta.json помощника
(subagents/agent-<agent_id>.meta.json, поле toolUseId) — того же, что читает
agent_budget_guard.py. Нет файла — строки нет: конец без вызова дерево не закроет.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import events                                            # noqa: E402

SRC = "scripts/hooks/agent_finish_log.py"
FINISH_EVENT = "agent.finish"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        meta = (Path(str(payload.get("transcript_path") or "")).parent
                / str(payload.get("session_id") or "") / "subagents"
                / f"agent-{payload.get('agent_id')}.meta.json")
        call = str(json.loads(meta.read_text(encoding="utf-8")).get("toolUseId") or "")
    except (ValueError, OSError, AttributeError):
        return 0
    if call:
        events.emit(SRC, FINISH_EVENT, "ok", journal="вызовы-агентов",
                    session=payload.get("session_id") or None, data={"вызов": call})
    return 0


if __name__ == "__main__":
    sys.exit(main())
