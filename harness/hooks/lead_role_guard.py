#!/usr/bin/env python3
"""PreToolUse: роли помощников держит код, а не текст агента.

Два вида запуска (владелец 29.10.2026): исполнитель (harness-worker) делает одну
задачу сам и никого не запускает; управляющий (harness-lead) ведёт задачу по
трубе, запускает исполнителей и сам ничего не пишет — как основная смена.
Таблица ролей — данные: harness/config/helper-roles.yaml.

Роль берётся из `agent_type` во входе хука (так её пишет и file_claim_guard.py);
смена и чужие типы — без agent_type или вне таблицы — не трогаются и следа не
оставляют. Каждое решение по роли из таблицы — строка $LOG_DIR/роли-помощников.jsonl
{роль, инструмент, решение, причина}: сторож, чьих отказов не видно, не отладить.

Отказ — код 2 и причина в stderr с подсказкой законного хода. Сбой самого хука
(битый вход, нет таблицы) работу не роняет: код 0 и строка skipped.

Bash управляющего судится по тексту команды консервативно: любое непонятное —
отказ, потому что законный ход всегда есть — отдать запись исполнителю.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import config                                            # noqa: E402
import events                                            # noqa: E402

SRC = "scripts/hooks/lead_role_guard.py"
EVENT = "role.decision"
FILE_TOOLS = ("Edit", "Write", "NotebookEdit")
AGENT_TOOLS = ("Agent", "Task")
HINT = "Запиши это через исполнителя: Agent с subagent_type harness-worker и явной model."
# Обёртки фонового запуска и безвредные перенаправления: сами ничего не пишут.
WRAPPERS = ("setsid", "nohup")
NULL_SINKS = ("/dev/null", "&1", "&2")
LOG_DIR_PREFIX = config.log_dir().rstrip("/") + "/"


def roles_path() -> Path:
    return Path(os.environ.get("HARNESS_HELPER_ROLES") or ROOT / "harness" / "config" / "helper-roles.yaml")


def record(outcome: str, session: str, data: dict) -> None:
    events.emit(SRC, EVENT, outcome, journal="роли-помощников", session=session or None, data=data)


def strip_heredocs(command: str) -> str:
    """Тело heredoc — текст, а не команды; без этого запись в журнал не проходит."""
    out, end = [], None
    for line in command.split("\n"):
        if end is not None:
            if line.strip() == end:
                end = None
            continue
        out.append(line)
        m = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", line)
        if m:
            end = m.group(1)
    return "\n".join(out)


def redirect_targets(command: str) -> list:
    targets = re.findall(r"\d*>>?\s*(&?\S+)", command)
    return [t.strip("'\"") for t in targets]


def target_ok(target: str, role: dict) -> bool:
    if target in NULL_SINKS or target.startswith(LOG_DIR_PREFIX):
        return True
    return any(target.endswith("/" + name) or target == name for name in role.get("пишет_только", []))


def git_subcommand(args: list) -> str:
    """Подкоманда git после глобальных флагов; флаг, выполняющий чужой код, возвращается как есть.

    `-c diff.external=…` и `--config-env`, `--exec-path` превращают чтение в запуск произвольной
    команды, поэтому подкомандой они не станут и не попадут в список разрешённых. У `-C <путь>`
    аргумент — отдельное слово.
    """
    it = iter(args)
    for word in it:
        if word == "-C":
            next(it, None)
        elif word.startswith(("-c", "--config-env", "--exec-path")):
            return word
        elif not word.startswith("-"):
            return word
    return ""


def script_allowed(call: list, role: dict) -> bool:
    """Запись списка «скрипты» — путь (любые флаги) или «путь --флаг …»: у второго вида звать
    можно только с перечисленными флагами, любой другой флаг в argv — отказ.

    Нужен там, где у скрипта есть и чтение, и запись (spec_approval.py: --состояние читает,
    --отправить пишет владельцу; launch-plan.py: --прижить и --закрепить пишут в карту).
    Проверяются ВСЕ слова argv: скрипт ищет свой режим в любом месте строки.
    """
    flags = {w for w in call[1:] if w.startswith("-")}
    for entry in role.get("скрипты", []):
        path, *allowed = entry.split()
        if call[:1] == [path] and (not allowed or flags <= set(allowed)):
            return True
    return False


def segment_verdict(words: list, role: dict) -> str:
    """Причина отказа для одной простой команды или пусто."""
    while words and words[0] in WRAPPERS:
        words = words[1:]
    if not words:
        return ""
    name = os.path.basename(words[0])
    if name == "git":
        sub = git_subcommand(words[1:])
        return "" if sub in role.get("git", []) else f"git {sub or '?'} пишет в дерево"
    if name in ("python3", "bash", "sh"):
        script_at = next((i for i, w in enumerate(words) if i and not w.startswith("-")), 0)
        call = words[script_at:]
        script = call[0] if call else ""
        return "" if script_allowed(call, role) else f"{name} {script[:40]}: скрипт вне списка"
    return "" if name in role.get("команды", []) else f"{name}: команда вне списка чтения"


def bash_verdict(command: str, role: dict) -> str:
    if "`" in command or "$(" in command:
        return "подстановка команд скрывает запись"
    text = strip_heredocs(command)
    for target in redirect_targets(text):
        if not target_ok(target, role):
            return f"перенаправление в {target} — запись вне журнала решений"
    text = re.sub(r"\d*>>?\s*&?\S+", " ", text)
    text = re.sub(r"<\s*\S+", " ", text)
    for part in re.split(r"\|\||&&|[;|&\n]", text):
        try:
            words = shlex.split(part)
        except ValueError:
            return "команда не разбирается"
        reason = segment_verdict(words, role)
        if reason:
            return reason
    return ""


def agent_verdict(inp: dict, role: dict) -> str:
    kind = str(inp.get("subagent_type") or "")
    if kind not in role.get("запускает", []):
        allowed = ", ".join(role.get("запускает", [])) or "никого"
        return f"запуск типа «{kind or 'без типа'}» не разрешён роли (можно: {allowed})"
    if role.get("требует_модель") and not inp.get("model"):
        return "запуск без явной модели: умолчание наследует самую дорогую"
    return ""


def verdict(tool: str, inp: dict, role: dict) -> str:
    """Причина отказа или пусто."""
    if tool in AGENT_TOOLS:
        return agent_verdict(inp, role)
    if "пишет_только" not in role:
        return ""
    if tool in FILE_TOOLS:
        path = str(inp.get("file_path") or inp.get("notebook_path") or "")
        return "" if target_ok(path, role) else f"{tool} {path}: управляющий файлы не пишет"
    if tool == "Bash":
        return bash_verdict(str(inp.get("command") or ""), role)
    return ""


def decide(payload: dict) -> tuple:
    """(код выхода, текст отказа)."""
    kind = str(payload.get("agent_type") or "")
    tool = str(payload.get("tool_name") or "")
    table = yaml.safe_load(roles_path().read_text(encoding="utf-8")) or {}
    role = (table.get("роли") or {}).get(kind)
    if role is None:
        return 0, ""
    inp = payload.get("tool_input") or {}
    reason = verdict(tool, inp, role)
    record("ok", str(payload.get("session_id") or ""),
           {"роль": kind, "инструмент": tool, "решение": "отказ" if reason else "пропуск",
            "причина": reason or "-", "агент": str(payload.get("agent_id") or "-")})
    if not reason:
        return 0, ""
    return 2, f"Роль {kind}: {reason}. {HINT}"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        code, text = decide(payload) if isinstance(payload, dict) else (0, "")
    except Exception as exc:  # noqa: BLE001 — своя ошибка хука не роняет работу
        record("skipped", "", {"почему": f"{type(exc).__name__}: {exc}"[:300]})
        return 0
    if text:
        print(text, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
