#!/usr/bin/env python3
"""Гейт: tmux-вызов по TMUX_TMPDIR не бьёт в сервер из унаследованной $TMUX.

tmux при заданной переменной TMUX берёт сокет из неё и TMUX_TMPDIR не слушает,
а `setsid nohup` окружение не чистит: скрипт, запущенный из панели агента,
действует на боевой сервер вместе с сессией agent, хотя автор метил в свой.

Правило (узкое, чтобы не ловить законное):
  отказ, если в .sh-файле есть вызов `tmux … <действие на сервер>`
  (kill-server, kill-session, kill-window, send-keys, new-session,
  respawn-pane), который ОПИРАЕТСЯ на TMUX_TMPDIR — присваивает её в той же
  команде или файл делает `export TMUX_TMPDIR=`, — и при этом сервер не назван
  явно (`-S`/`-L`, значение может быть переменной), окружение не снято в самой
  команде (`env -i`, `env -u TMUX`, `TMUX= tmux …`) и файл не снял TMUX на
  верхнем уровне (`unset … TMUX …` без отступа) ДО строки вызова.
Строки с продолжением обратной косой чертой склеиваются перед разбором.
`tmux kill-session -t <имя>` без TMUX_TMPDIR — законная работа с боевым
сервером (свои именованные сессии проб), её гейт не судит. Обёртки вида
`stand_tmux "$s" kill-server` не судятся: судится строка, где стоит сам tmux.
Файлы .py не судятся: в дереве tmux через subprocess с TMUX_TMPDIR зовёт одна
проба, и окружение у неё — собранный с нуля словарь.

    python3 scripts/check-tmux-isolation.py              # дерево
    python3 scripts/check-tmux-isolation.py <файл…>      # названные файлы
    python3 scripts/check-tmux-isolation.py --selftest
Код возврата: 0 — чисто, 1 — есть опасная уборка.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from selftest_cases import Cases  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("scripts", "harness")

# Сам tmux — отдельное слово (own_tmux и $REAL_TMUX — не он), дальше до
# действия на сервер в той же команде. Комментарии не судим.
ACTIONS = "kill-server|kill-session|kill-window|send-keys|new-session|respawn-pane"
TMUX_CALL = re.compile(r"(?<![\w.$-])tmux\s(?P<args>[^#;|&]*?)\b(?:%s)\b" % ACTIONS)
INLINE_TMPDIR = re.compile(r"(?<!\w)TMUX_TMPDIR=")
EXPORT_TMPDIR = re.compile(r"^\s*export\s+TMUX_TMPDIR=", re.M)
NAMED_SERVER = re.compile(r"(?<!\S)-[SL]")
# TMUX= с пустым значением tmux считает неустановленной
LINE_ISOLATED = re.compile(
    r"\benv\s+(?:-\S+\s+)*?(?:-i\b|-u\s+TMUX(?!\w))|(?<!\w)TMUX=(?:\"\"|'')?(?=\s)")
TOP_UNSET = re.compile(r"^unset\s+(?:\w+\s+)*?TMUX(?!\w)")


def logical_lines(text: str) -> list[tuple[int, str]]:
    """(номер первой физической строки, команда) с склеенными продолжениями `\\`."""
    out, buf, start = [], "", 0
    for number, line in enumerate(text.splitlines(), 1):
        if not buf:
            start = number
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append((start, buf + line))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


def findings(text: str) -> list[int]:
    """Номера строк с вызовом tmux, который унаследованная $TMUX уводит на чужой сервер."""
    exported = bool(EXPORT_TMPDIR.search(text))
    lines = logical_lines(text)
    unset_at = next((n for n, line in lines if TOP_UNSET.match(line)), None)
    bad = []
    for number, line in lines:
        if line.lstrip().startswith("#"):
            continue
        call = TMUX_CALL.search(line)
        if not call:
            continue
        if not (exported or INLINE_TMPDIR.search(line[:call.start()])):
            continue
        if NAMED_SERVER.search(call.group("args")) or LINE_ISOLATED.search(line):
            continue
        if unset_at is not None and unset_at < number:
            continue
        bad.append(number)
    return bad


def tree_files(root: Path) -> list[Path]:
    files = []
    for top in SCAN_DIRS:
        files.extend(p for p in (root / top).rglob("*.sh") if p.is_file())
    return sorted(files)


def check(files: list[Path]) -> int:
    if not files:
        print("[изоляция tmux] ОТКАЗ: судить нечего — список файлов пуст")
        return 1
    bad = []
    for path in files:
        for number in findings(path.read_text(encoding="utf-8", errors="replace")):
            shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            bad.append(f"{shown}:{number}")
    if bad:
        print(f"[изоляция tmux] УБОРКА МИМО СВОЕГО СЕРВЕРА: {len(bad)}")
        for place in bad:
            print(f"  ✗ {place}")
        print("  Починка: `unset TMUX TMUX_PANE` в начале файла или явный сервер — "
              "`env -u TMUX tmux -S <сокет стенда> kill-server`")
        return 1
    print(f"[изоляция tmux] чисто: файлов {len(files)}, уборка tmux бьёт только в свой сервер")
    return 0


def selftest() -> int:
    c = Cases()
    sick = ('cleanup() {\n    for d in "$T"/*/tmux; do\n'
            '        [ -d "$d" ] && TMUX_TMPDIR="$d" tmux kill-server 2>/dev/null\n'
            '    done\n}\n')
    # Больной первым — ровно уборка теста волны 3 до правки.
    c.check("БОЛЬНОЙ: TMUX_TMPDIR в строке, $TMUX не снята", [3], findings(sick))
    c.check("БОЛЬНОЙ: export TMUX_TMPDIR и голый kill-session", [2],
            findings('export TMUX_TMPDIR="$S/tmux"\ntmux kill-session -t proba\n'))
    c.check("БОЛЬНОЙ: unset только TMUX_PANE", [2],
            findings('unset TMUX_PANE\nTMUX_TMPDIR="$d" tmux kill-server\n'))
    c.check("здоровый: -S сокет стенда", [],
            findings('TMUX_TMPDIR="$d" tmux -S "$d/tmux-1000/default" kill-server\n'))
    c.check("здоровый: -L имя сокета", [],
            findings('TMUX_TMPDIR="$d" tmux -L w1t kill-server\n'))
    c.check("здоровый: env -u TMUX", [],
            findings('env -u TMUX TMUX_TMPDIR="$d" tmux kill-server\n'))
    c.check("здоровый: env -i", [],
            findings('env -i PATH=/usr/bin TMUX_TMPDIR="$d" tmux kill-server\n'))
    c.check("здоровый: unset TMUX TMUX_PANE в файле", [],
            findings('unset TMUX TMUX_PANE\n' + sick))
    c.check("БОЛЬНОЙ: unset ПОСЛЕ вызова", [1],
            findings('TMUX_TMPDIR="$d" tmux kill-server\nunset TMUX\n'))
    c.check("БОЛЬНОЙ: unset в функции после вызова", [1],
            findings('TMUX_TMPDIR="$d" tmux kill-server\nsafe() {\n    unset TMUX\n}\n'))
    c.check("БОЛЬНОЙ: многострочный TMUX_TMPDIR с продолжением", [1],
            findings('TMUX_TMPDIR="$d" \\\n    tmux kill-server\n'))
    c.check("БОЛЬНОЙ: send-keys с TMUX_TMPDIR без -S", [1],
            findings('TMUX_TMPDIR="$d" tmux send-keys -t w "x" Enter\n'))
    c.check("здоровый: TMUX= в той же команде", [],
            findings('TMUX= TMUX_TMPDIR=$d tmux kill-server\n'))
    c.check("здоровый: -S с переменной", [],
            findings('TMUX_TMPDIR="$d" tmux -S "$sock" kill-server\n'))
    c.check("не этот класс: kill-session по имени без TMUX_TMPDIR", [],
            findings('tmux kill-session -t "$PANE" 2>/dev/null\n'))
    c.check("не этот класс: обёртка own_tmux", [],
            findings('export TMUX_TMPDIR="$S"\nown_tmux kill-server\n'))
    c.check("не этот класс: комментарий", [],
            findings('# TMUX_TMPDIR="$d" tmux kill-server\n'))
    print(f"[изоляция tmux] самотест: путей {c.paths}, неудач {c.failures}")
    return 0 if c.ok else 1


def main(argv: list[str]) -> int:
    if argv[:1] == ["--selftest"]:
        return selftest()
    if argv:
        return check([Path(a).resolve() for a in argv])
    return check(tree_files(ROOT))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
