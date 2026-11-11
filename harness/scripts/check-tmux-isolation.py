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

Второе правило — X (или
`new`) в позиции команды (начало строки, после `;`, `&&`, `||`, `|`, `$(`,
`then`, `do`, `else`) без своего сервера поднимает сессию на БОЕВОМ сервере
рядом с сессией agent (11.11.2026 так делали восемь проб сторожа). Зелёно, если:
  - в глобальных доводах назван сервер `-L` или `-S`;
  - файл на верхнем уровне (без отступа) до этой строки делает
    `export TMUX_TMPDIR=` и `unset … TMUX`;
  - файл до этой строки кладёт первой в PATH обёртку tmux, а обёртка пишется
    с `-L`/`-S`;
  - над вызовом (или в той же строке) стоит метка `# tmux-боевой: <причина>` —
    осознанный боевой сервер (сам запуск агента) или долг; метки гейт печатает.
Комментарии и тела heredoc не судятся, строки с обратной косой в конце склеиваются. `own_tmux`,
`"$REAL_TMUX"`, `tmux` как довод (`mkproc … tmux new-session`) и массив
`X=(tmux new-session …)` — не вызов. Правило живёт в этом файле, а не в новом:
ему нужны те же разбор строк с продолжениями, обход дерева, самотест и ворота.

Файлы .py не судятся: в дереве tmux через subprocess с TMUX_TMPDIR зовёт одна
проба, и окружение у неё — собранный с нуля словарь.

    python3 scripts/check-tmux-isolation.py              # дерево
    python3 scripts/check-tmux-isolation.py <файл…>      # названные файлы
    python3 scripts/check-tmux-isolation.py --selftest
Код возврата: 0 — чисто, 1 — есть опасная уборка или new-session на боевом сервере.
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


HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")
# tmux в позиции команды; `X=(tmux …` и `mkproc … tmux` сюда не попадают
NEW_CALL = re.compile(r"(?:^|[;&|]|\$\(|\b(?:then|do|else)\b)\s*tmux(?=\s)(?P<rest>[^;&|]*)")
NEW_ACTIONS = ("new-session", "new")
GLOBAL_WITH_ARG = ("-L", "-S", "-f", "-c", "-T")
TOP_EXPORT_TMPDIR = re.compile(r"^export\s+TMUX_TMPDIR=")
TOP_PATH_FIRST = re.compile(r"^(?:export\s+)?PATH=[\"']?(?!\$\{?PATH\b)\S+?:")
WRAPPER_WRITE = re.compile(r"exec\b.*\s-[LS]\s.*/tmux\b")
BATTLE_MARK = "tmux-боевой:"


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


def scrubbed_lines(text: str) -> list[str]:
    """Физические строки без комментариев и тел heredoc (номера строк сохраняются)."""
    out, end_tag = [], None
    for line in text.splitlines():
        if end_tag is not None:
            if line.strip() == end_tag:
                end_tag = None
            out.append("")
            continue
        if line.lstrip().startswith("#"):
            out.append("")
            continue
        out.append(line)
        opened = HEREDOC.search(line)
        if opened:
            end_tag = opened.group(2)
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


def tmux_action(rest: str) -> tuple[str, bool]:
    """(действие, назван ли сервер) по доводам после слова tmux."""
    named, tokens = False, rest.split()
    i = 0
    while i < len(tokens):
        word = tokens[i]
        if word in GLOBAL_WITH_ARG:
            named = named or word in ("-L", "-S")
            i += 2
        elif word.startswith("-"):
            named = named or word[:2] in ("-L", "-S")
            i += 1
        else:
            return word, named
    return "", named


def new_session_verdicts(text: str) -> dict[int, str]:
    """Номер строки с `tmux new-session` → «bad» (мимо своего сервера) или «marked»."""
    raw = text.splitlines()
    lines = logical_lines("\n".join(scrubbed_lines(text)))
    top_unset = [n for n, line in lines if TOP_UNSET.match(line)]
    top_export = [n for n, line in lines if TOP_EXPORT_TMPDIR.match(line)]
    top_path = [n for n, line in lines if TOP_PATH_FIRST.match(line)]
    wrapper = [n for n, line in lines if WRAPPER_WRITE.search(line)]
    verdicts = {}
    for number, line in lines:
        for call in NEW_CALL.finditer(line):
            action, named = tmux_action(call.group("rest"))
            if action not in NEW_ACTIONS or named:
                continue
            before = lambda found: any(n < number for n in found)
            if (before(top_export) and before(top_unset)) or (before(top_path) and before(wrapper)):
                continue
            near = raw[number - 2] if number >= 2 else ""
            marked = BATTLE_MARK in near or BATTLE_MARK in raw[number - 1]
            verdicts[number] = "marked" if marked else "bad"
    return verdicts


def own_server_findings(text: str) -> list[int]:
    """Номера строк, где tmux new-session уходит на боевой сервер без метки."""
    return sorted(n for n, v in new_session_verdicts(text).items() if v == "bad")


def tree_files(root: Path) -> list[Path]:
    files = []
    for top in SCAN_DIRS:
        files.extend(p for p in (root / top).rglob("*.sh") if p.is_file())
    return sorted(files)


def check(files: list[Path]) -> int:
    if not files:
        print("[изоляция tmux] ОТКАЗ: судить нечего — список файлов пуст")
        return 1
    bad, own, marked = [], [], []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
        bad += [f"{shown}:{n}" for n in findings(text)]
        for number, verdict in new_session_verdicts(text).items():
            (own if verdict == "bad" else marked).append(f"{shown}:{number}")
    if bad:
        print(f"[изоляция tmux] УБОРКА МИМО СВОЕГО СЕРВЕРА: {len(bad)}")
        for place in bad:
            print(f"  ✗ {place}")
        print("  Починка: `unset TMUX TMUX_PANE` в начале файла или явный сервер — "
              "`env -u TMUX tmux -S <сокет стенда> kill-server`")
    if own:
        print(f"[изоляция tmux] new-session НА БОЕВОМ СЕРВЕРЕ: {len(own)}")
        for place in sorted(own):
            print(f"  ✗ {place}")
        print("  Починка: `unset TMUX TMUX_PANE` и `export TMUX_TMPDIR=<каталог стенда>` "
              "наверху файла, либо `tmux -L <имя> new-session`; осознанный боевой "
              "сервер — метка `# tmux-боевой: <причина>` над вызовом")
    if bad or own:
        return 1
    print(f"[изоляция tmux] чисто: файлов {len(files)}, tmux бьёт только в свой сервер"
          f" (с меткой «боевой»: {len(marked)})")
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
    # ── второе правило: tmux new-session мимо своего сервера ──
    bare = 'tmux new-session -d -s "$PANE" "exec $T/claude >> $T/pane.txt"\n'
    c.check("БОЛЬНОЙ: голый new-session в начале строки", [1], own_server_findings(bare))
    c.check("БОЛЬНОЙ: короткое `new` после &&", [2],
            own_server_findings('cd "$T" &&\ntrue && tmux new -d -s a\n'))
    c.check("БОЛЬНОЙ: new-session в $(…) и после `;`", [1, 2],
            own_server_findings('x=$(tmux new-session -d -s a)\ntrue; tmux new-session -d -s b\n'))
    c.check("БОЛЬНОЙ: строка с продолжением названа по первой строке", [2],
            own_server_findings('true\ntmux new-session -d \\\n    -s a\n'))
    c.check("БОЛЬНОЙ: export TMUX_TMPDIR без unset TMUX", [2],
            own_server_findings('export TMUX_TMPDIR="$T/t"\n' + bare))
    c.check("БОЛЬНОЙ: unset TMUX без export TMUX_TMPDIR", [2],
            own_server_findings('unset TMUX\n' + bare))
    c.check("БОЛЬНОЙ: export и unset ПОСЛЕ вызова", [1],
            own_server_findings(bare + 'export TMUX_TMPDIR="$T/t"\nunset TMUX\n'))
    c.check("БОЛЬНОЙ: export и unset с отступом (в функции) не наверху", [4],
            own_server_findings('f() {\n    export TMUX_TMPDIR="$T/t"; unset TMUX\n}\n' + bare))
    c.check("БОЛЬНОЙ: PATH с обёрткой, но обёртка без -L/-S", [3],
            own_server_findings('printf \'#!/bin/sh\\nexec tmux "$@"\\n\' > "$T/b/tmux"\n'
                                'export PATH="$T/b:$PATH"\n' + bare))
    c.check("здоровый: -L имя сервера", [],
            own_server_findings('tmux -L w1t new-session -d -s a\n'))
    c.check("здоровый: -S сокет", [],
            own_server_findings('tmux -S "$sock" new -d -s a\n'))
    c.check("здоровый: export TMUX_TMPDIR и unset TMUX наверху", [],
            own_server_findings('unset TMUX TMUX_PANE\nexport TMUX_TMPDIR="$T/t"\n' + bare))
    c.check("здоровый: обёртка с -L первой в PATH", [],
            own_server_findings('printf \'#!/bin/sh\\nexec "%s" -L "%s" "$@"\\n\' "$R" "$N" > "$T/b/tmux"\n'
                                'export PATH="$T/b:$PATH"\n' + bare))
    c.check("здоровый: комментарий", [],
            own_server_findings('# tmux new-session -d -s a\n    # tmux new-session -d -s a\n'))
    c.check("здоровый: тело heredoc", [],
            own_server_findings('cat > "$f" <<SH\ntmux new-session -d -s a\nSH\n'
                                "cat <<'EOF'\n  tmux new-session -d\nEOF\ntrue\n"))
    c.check("БОЛЬНОЙ: строка после heredoc судится", [4],
            own_server_findings('cat <<SH\nx\nSH\ntmux new-session -d -s a\n'))
    c.check("здоровый: tmux как довод mkproc", [],
            own_server_findings('mkproc "$T/p" tmux new-session -d -s a\n'))
    c.check("здоровый: own_tmux и $REAL_TMUX", [],
            own_server_findings('own_tmux new-session -d -s a\n"$REAL_TMUX" new-session -d\n'))
    c.check("здоровый: массив TMUX_TOP=(tmux …) не вызов", [],
            own_server_findings('TMUX_TOP=(tmux new-session -d -s agent)\n'))
    c.check("здоровый: другое действие (kill-session, has-session)", [],
            own_server_findings('tmux kill-session -t a\ntmux has-session -t a\n'))
    c.check("здоровый: `new` как имя сессии, не действие", [],
            own_server_findings('tmux send-keys -t new Enter\n'))
    c.check("известный долг: метка tmux-боевой над вызовом", [],
            own_server_findings('# tmux-боевой: так живёт агент\n' + bare))
    c.check("БОЛЬНОЙ: метка не над вызовом, а выше через строку", [3],
            own_server_findings('# tmux-боевой: x\ntrue\n' + bare))
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
