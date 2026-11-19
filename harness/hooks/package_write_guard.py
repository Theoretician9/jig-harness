#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сторож записи в общий пакет из дерева помощника: файл харнеса в пакет несёт очередь.

PreToolUse на Write|Edit|NotebookEdit|Bash. При HELPER_WORKTREE=1 у каждого помощника своё
дерево, а пакет — один на всех. 15.11.2026 помощники писали файлы харнеса прямо в пакет
до слияния своей ветки, и merge-queue.py отказывал кодом 7 («пакет разошёлся») всем
остальным веткам, пока чужая правка лежала в пакете, — ветки стояли часами. Файл,
у которого есть пара в проекте, очередь сама переносит в пакет из диффа ветки и
коммитит; помощнику его в пакет писать незачем.

Файлы пакета без пары в проекте (раскладчик, слой 1 harness/self/, CLAUDE.md пакета)
очередь не переносит и кодом 7 не судит — их хук пропускает: отказ оставил бы им
ноль дверей. Основная сессия (оркестратор, управляющий) пишет пакет как раньше.

Разбор путей Bash (write_targets) видит перенаправление, cp, sed -i и python -c с путём
в кавычках, но не видит цели записи питона в heredoc и в скрипте из scratchpad, patch и
git apply: 19.11.2026 четыре помощника так писали пакет. Для них судит ТЕКСТ команды: в нём
путь пакета (каталог или файл с парой) и писатель — python с признаком записи (в тексте или
в скрипте вне дерева, который он запускает), patch без --dry-run, git apply/am без --check.

Сессия помощника узнаётся по полю cwd входа: оно лежит под <проект>/.claude/worktrees/.
Отказ — код 2, причина в stderr. Сбой самого хука — пропуск со строкой в stderr,
как у соседей: сломанный сторож не должен останавливать работу.
Двери проб: HARNESS_CONF (ключ), HARNESS_STARTER (пакет).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lib"))
import config  # noqa: E402
from orders_write_guard import write_targets  # noqa: E402

KEY = "HELPER_WORKTREE"
DENY = ("{rel} в пакете — файл харнеса с парой в проекте ({pair}). Из дерева помощника пакет "
        "не пишут: правку делай в своём дереве ({pair}) и коммить в ветку — "
        "очередь слияния (python3 scripts/merge-queue.py <ветка>) сама перенесёт его в пакет "
        "и закоммитит. Чужая правка в общем пакете стопорит очередь кодом 7 для всех веток.")
TEXT_DENY = ("Команда: {how} пакет ({where}) мимо разбора путей. Из дерева помощника пакет "
             "не пишут: пакет переносит merge-queue — очередь слияния (python3 scripts/merge-queue.py "
             "<ветка>) сама перенесёт правку из твоей ветки. Правь файл в своём дереве; читать пакет "
             "питоном можно — без записи в том же вызове.")
PY_NAME = r"(?:[\w./~-]*/)?python3?(?:\.\d+)?"
# Имя команды — после начала звена, префиксов окружения и обёрток (timeout, heavy-slot …).
LEAD = r"^\s*(?:[\w]+=\S*\s+|\(\s*|!\s*|time\s+|nice\s+|env\s+|timeout\s+\S+\s+|\S*heavy-slot\.sh\s+--\s+|then\s+|do\s+)*"
PYTHON = re.compile(LEAD + PY_NAME + r"(?=\s|$)")
PY_SCRIPT = re.compile(LEAD + PY_NAME + r"\s+(?:-[A-Za-z]+\s+)*([^\s<>-]\S*\.py)(?=\s|$)")
PATCH = re.compile(LEAD + r"(?:[\w./-]*/)?patch\s(?!.*--dry-run)")
GIT_APPLY = re.compile(LEAD + r"git\s(?:-C\s+\S+\s+)?(?:apply|am)\b(?!.*--(?:check|stat|numstat|summary))")
GIT_C = re.compile(r"git\s+-C\s+(\S+)")
CD = re.compile(r"^\s*cd\s+(\S+)")
ASSIGN = re.compile(r"(?:^|[\s;&|(])(\w+)=(\S+)")
VAR = re.compile(r"\$\{?(\w+)\}?")
FOR = re.compile(r"^\s*for\s+(\w+)\s+in\s+(.+)$")
HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
PY_WRITE = re.compile(r"""open\([^\n]*?,\s*(?:mode\s*=\s*)?['"][rbt]*[wax+]"""
                      r"""|\.open\(\s*(?:mode\s*=\s*)?['"][rbt]*[wax+]|write_text|write_bytes"""
                      r"""|shutil\.|os\.(?:replace|rename|remove|unlink)|\.(?:rename|replace|touch|unlink)\(""")
PATH_TAIL = r"[^\s'\"`;&|()<>,\[\]{}$]*"


def worktree_mode() -> bool:
    """Как у launch_plan_guard: установленный harness.conf, затем конфиг дерева; не «1» = 0."""
    tree_conf = ROOT / "harness" / "config" / "harness.conf"
    value = (config.из_файла(config.путь_harness_conf()).get(KEY)
             or config.из_файла(str(tree_conf)).get(KEY) or "")
    return value == "1"


def in_helper_tree(cwd: str) -> bool:
    return any(p.name == "worktrees" and p.parent.name == ".claude"
               for p in Path(os.path.realpath(cwd)).parents)


def load_compare():
    spec = importlib.util.spec_from_file_location("compare_with_package",
                                                  HERE.parent / "compare-with-package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def carried(targets: list) -> tuple[str, str] | None:
    """(путь в пакете, пара в проекте) первой цели, которую несёт очередь; None — таких нет."""
    package = os.path.realpath(config.katalog_paketa())
    compare = load_compare()
    for target in targets:
        real = os.path.realpath(target)
        if not real.startswith(package + os.sep):
            continue
        rel = os.path.relpath(real, package)
        pair = compare.место_в_проекте(rel)
        if pair and not compare.живой(pair):
            return rel, pair
    return None


def tree_root(cwd: str) -> str:
    """Корень дерева помощника по cwd: <проект>/.claude/worktrees/<имя>."""
    parts = Path(os.path.realpath(cwd)).parts
    for i in range(len(parts) - 2):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees":
            return str(Path(*parts[:i + 3]))
    return os.path.realpath(cwd)


def split_links(line: str) -> list:
    """Звенья строки оболочки по ; && || | & вне кавычек."""
    links, current, quote, i = [], [], "", 0
    while i < len(line):
        ch = line[i]
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch in ";&|":
            links.append("".join(current))
            current = []
            i += 2 if line[i:i + 2] in ("&&", "||") else 1
            continue
        current.append(ch)
        i += 1
    links.append("".join(current))
    return links


def links(command: str) -> list:
    """(звено, тело его heredoc) по порядку; тело heredoc — текст программы, не команды."""
    out, lines, i = [], command.split("\n"), 0
    while i < len(lines):
        line, body = lines[i], []
        i += 1
        found = HEREDOC.search(line)
        if found:
            while i < len(lines) and lines[i].strip() != found.group(1):
                body.append(lines[i])
                i += 1
            i += 1
        for link in split_links(line):
            out.append((link, "\n".join(body) if found and "<<" in link else ""))
    return out


def ad_hoc_script(link: str, cwd: str) -> str:
    """Текст питон-скрипта вне дерева, проекта и пакета (scratchpad): цель записи — в нём, не в
    команде. Скрипты дерева, проекта и пакета — механизмы со своими сторожами; их текст не судится."""
    found = PY_SCRIPT.search(QUOTED.sub("''", link))
    if not found:
        return ""
    tree = tree_root(cwd)
    homes = (tree + os.sep, str(Path(tree).parent.parent.parent) + os.sep, str(ROOT) + os.sep,
             os.path.realpath(config.katalog_paketa()) + os.sep)
    real = os.path.realpath(os.path.join(cwd, found.group(1)))
    if os.path.isfile(real) and not real.startswith(homes):
        return Path(real).read_text(encoding="utf-8", errors="replace")
    return ""


def package_mentions(text: str) -> list:
    """Пути пакета в тексте (как назван в конфиге и настоящий): сам пакет, его каталоги и файлы;
    соседка «пакет-x» — не он."""
    named = str(config.katalog_paketa())
    real = os.path.realpath(named)
    return [real + tail for package in {named, real}
            for tail in re.findall(re.escape(package) + rf"(?![\w.-])({PATH_TAIL})", text)]


def aimed(text: str) -> list:
    """Цели в пакете, запись в которые несёт очередь: каталог пакета (цель не видна) или файл с парой.
    Файл пакета без пары очередь не переносит — его запись законна, как у Write."""
    return [m for m in package_mentions(text) if os.path.isdir(m) or carried([m])]


def text_writer(command: str, cwd: str) -> tuple[str, str] | None:
    """(как пишет, куда): звено Bash пишет пакет путём, которого write_targets не видит; None — нет.

    Судится каждое звено отдельно: питон или patch и путь пакета — в ОДНОМ звене (его доводы,
    heredoc, скрипт из scratchpad), иначе cmp с пакетом рядом с правкой своего дерева — отказ.
    Путь в звено приходит и через cd в пакет, и через переменную оболочки (P=<пакет>; … $P/…)."""
    where, values = "", {}

    def expand(text: str) -> str:
        return VAR.sub(lambda m: values.get(m.group(1), m.group(0)), text)

    for link, body in links(command):
        link = expand(link)
        bare = QUOTED.sub("''", link)
        for name, value in ASSIGN.findall(link) + FOR.findall(link):
            values[name] = value.strip("'\"")
        went = CD.match(link)
        if went:
            where = " ".join(aimed(went.group(1).strip("'\"")))
            continue
        program = link + "\n" + expand(body)
        inside = where or " ".join(aimed(" ".join(GIT_C.findall(bare))))
        if PATCH.search(bare) or GIT_APPLY.search(bare):
            targets = aimed(program) or ([inside] if inside else [])
            if targets:
                return "patch или git apply пишет", targets[0]
        if PYTHON.search(bare):
            program += "\n" + ad_hoc_script(link, cwd)
            targets = aimed(program) or ([inside] if inside else [])
            if targets and PY_WRITE.search(program):
                return "python пишет", targets[0]
    return None


def main() -> int:
    try:
        event = json.loads(sys.stdin.read())
        hit = text_hit = None
        cwd = str(event.get("cwd") or os.getcwd())
        if in_helper_tree(cwd) and worktree_mode():
            hit = carried(write_targets(event))
            if not hit and event.get("tool_name") == "Bash":
                text_hit = text_writer(str((event.get("tool_input") or {}).get("command") or ""), cwd)
    except Exception as error:  # noqa: BLE001 — сбой хука не запрещает работу
        print(f"package_write_guard: пропуск, хук не разобрал вход: {type(error).__name__}: {error}",
              file=sys.stderr)
        return 0
    if hit:
        print(DENY.format(rel=hit[0], pair=hit[1]), file=sys.stderr)
        return 2
    if text_hit:
        print(TEXT_DENY.format(how=text_hit[0], where=text_hit[1]), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
