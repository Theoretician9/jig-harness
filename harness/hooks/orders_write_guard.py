#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сторож записи память/ORDERS.md агентами демонов.

PreToolUse на Write|Edit|NotebookEdit|Bash. 12.11.2026 агент потока проверок гасил
тревогу «ORDERS.md 97 % потолка» и целиком переписал файл — указания владельца
пропали. Снимает указание только владелец; прополка идёт scripts/weed-the-orders.py.

Агент демона узнаётся по переменной HARNESS_DEMON: её ставит scripts/claude-demon.sh
перед вызовом модели. У смены и оркестратора её нет — для них хук молчит.
Читать ORDERS.md и писать в другие файлы можно всем.

Bash разбирается по словам оболочки: перенаправление, tee, sed -i, cp/mv в файл,
dd of=, питон с записью. Путь сравнивается по настоящему файлу, поэтому «..», «./»
и cd перед командой не меняют решения.

Отказ — код 2, причина в stderr. Пропуск — код 0; сбой самого хука — тоже пропуск,
как у соседей: сломанный сторож не должен останавливать работу.
Двери проб: ORDERS_GUARD_ROOT — корень проекта.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

# Корень — по месту хука, не по CLAUDE_PROJECT_DIR: Claude Code ставит его в каталог
# запуска, а демон идёт из пустого каталога — корень указал бы мимо ORDERS.md.
ROOT = Path(os.environ.get("ORDERS_GUARD_ROOT") or Path(__file__).resolve().parent.parent.parent)
ORDERS = os.path.realpath(ROOT / "память" / "ORDERS.md")
PATH_KEYS = ("file_path", "notebook_path")
FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")"}
ANY_ARG_WRITERS = {"tee", "truncate", "rm", "shred"}
DEST_LAST_WRITERS = {"cp", "mv", "install"}
PYTHON_WRITE = re.compile(r"""open\(\s*[^,)]+,\s*['"][^'"]*[wax+]|write_text|write_bytes""")
QUOTED_PATH = re.compile(r"""['"]([^'"]+)['"]""")
DENY = ("Запись в память/ORDERS.md закрыта для агентов демонов. Указание из ORDERS.md "
        "снимает только владелец; переписанный файл теряет его слова (так пропали указания 12.11). "
        "Законная дверь: заведи карточку в dev-map.yaml или скажи оркестратору — он передаст владельцу. "
        "Прополка файла идёт скриптом scripts/weed-the-orders.py, а не ручной правкой.")


def is_orders(path: str) -> bool:
    return os.path.realpath(path) == ORDERS


def absolute(path: str, cwd: str) -> str:
    return path if os.path.isabs(path) else os.path.join(cwd, path)


def strip_heredocs(command: str) -> str:
    """Тело heredoc — текст, а не команды; строка с «<<» остаётся."""
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
    return ";".join(out)


def split_segments(command: str) -> list:
    lexer = shlex.shlex(strip_heredocs(command), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    segments, current = [], []
    for token in lexer:
        if token in SEPARATORS:
            segments.append(current)
            current = []
        else:
            current.append(token)
    segments.append(current)
    return [s for s in segments if s]


def split_redirects(tokens: list) -> tuple:
    """(слова команды, цели перенаправлений вывода)."""
    words, targets, i = [], [], 0
    while i < len(tokens):
        if tokens[i].startswith(">") and i + 1 < len(tokens):
            targets.append(tokens[i + 1])
            i += 2
        elif tokens[i].startswith("<"):
            i += 2 if tokens[i] in ("<", "<<", "<<-", "<<<") else 1
        else:
            words.append(tokens[i])
            i += 1
    return words, targets


def python_targets(code: str, cwd: str) -> list:
    return [absolute(p, cwd) for p in QUOTED_PATH.findall(code)] if PYTHON_WRITE.search(code) else []


def command_targets(words: list, cwd: str) -> list:
    """Пути, в которые пишет одна команда; не пишет — пустой список."""
    name = os.path.basename(words[0])
    args = words[1:]
    operands = [absolute(a, cwd) for a in args if not a.startswith("-")]
    if name in ANY_ARG_WRITERS:
        return operands
    if name == "sed" and any(a.startswith("-i") or a == "--in-place" or a.startswith("--in-place=")
                             for a in args):
        return operands
    if name in DEST_LAST_WRITERS:
        return operands if name == "mv" else operands[-1:]
    if name == "dd":
        return [absolute(a[3:], cwd) for a in args if a.startswith("of=")]
    if name.startswith("python"):
        return [t for a in args for t in python_targets(a, cwd)]
    if name in ("bash", "sh") and "-c" in args:
        at = args.index("-c") + 1
        return bash_targets(args[at], cwd) if at < len(args) else []
    return []


def bash_targets(command: str, cwd: str) -> list:
    targets = []
    for tokens in split_segments(command):
        words, redirects = split_redirects(tokens)
        targets += [absolute(t, cwd) for t in redirects]
        if not words:
            continue
        if words[0] == "cd" and len(words) > 1:
            cwd = absolute(words[1], cwd)
        else:
            targets += command_targets(words, cwd)
    return targets


def write_targets(event: dict) -> list:
    """Пути записи вызова инструмента, абсолютные; их судит вызывающий.
    Общий разбор: им же судит запись файлов харнеса harness_files_guard.py."""
    tool, inp = event.get("tool_name"), event.get("tool_input")
    if not isinstance(inp, dict):
        return []
    cwd = event.get("cwd") or os.getcwd()
    if tool in FILE_TOOLS:
        return [absolute(inp[k], cwd) for k in PATH_KEYS if inp.get(k)]
    if tool == "Bash":
        return bash_targets(inp.get("command") or "", cwd)
    return []


def main() -> int:
    if not os.environ.get("HARNESS_DEMON"):
        return 0
    try:
        denied = any(is_orders(t) for t in write_targets(json.loads(sys.stdin.read())))
    except Exception as error:  # noqa: BLE001 — сбой хука не запрещает работу
        print(f"orders_write_guard: пропуск, хук не разобрал вход: {type(error).__name__}: {error}",
              file=sys.stderr)
        return 0
    if denied:
        print(DENY, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
