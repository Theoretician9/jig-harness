"""Ворота журналов G1 и G2: прямая дозапись мимо emit, метки демонов, реестр ↔ код ↔ диск.

    python3 scripts/check-event-writers.py [--root DIR]             G1 (по умолчанию)
    python3 scripts/check-event-writers.py [--root DIR] --registry  G2 код ↔ реестр (коммит)
    python3 scripts/check-event-writers.py [--root DIR] --disk      G2 диск → реестр (прогон ворот)

Код возврата: 0 — зелёный, 1 — красный, 2 — неверный вызов. Ворота ничего не пишут.

Зачем. Одна функция записи ничего не стоит, если завтрашний механизм допишет
`>> x.jsonl` мимо неё. Долг (все сегодняшние писатели и демоны) — данные
journals.yaml: `legacy_writers`, `legacy_daemons`. Единица долга писателей —
пара (файл, журнал реестра): файл из долга, начавший писать НОВЫЙ журнал, —
новый писатель, а не старый долг. Единица долга демонов — файл. Множество пар
может только сокращаться (сверка с HEAD) и обязано находиться обходом в обе
стороны, иначе сломанный регэксп дал бы «0 нарушений» и зелёный.

Место дозаписи судится по ЦЕЛИ: журнал — литерал `.jsonl` в аргументе или имя с
присвоенным путём `.jsonl`; замок, `.log`, параметр функции и путь из
mktemp/tempfile (стенд самотеста) журналом не считаются. Python в bash-heredoc
(`python3 - <<'PY'`) судится python-правилом, а `sys.argv[i]` разрешается в
аргумент bash-вызова. Обход статический и слеп к пути, пришедшему из другого
файла или из `$(…)`: это закрывает G2 по содержимому переведённого журнала
(--disk).
"""
from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
REGISTRY_REL = "harness/config/journals.yaml"
DOOR_REL = "scripts/lib/events.py"
CODE_TOPS = ("scripts", "harness")
SKIP_DIRS = frozenset({"__pycache__", "node_modules", ".git"})
ANY_JOURNAL = "*"

SH_TEE_FLAGS_END = re.compile(r"[;|&<>)]")
SH_TOKEN = re.compile(r"\"[^\"]*\"|'[^']*'|[^\s;|&<>)]+")
SH_VAR_REF = re.compile(r"\$\{?(\w+)")
SH_ASSIGN = re.compile(r"^\s*(?:(?:local|export|readonly|declare)\s+)?([A-Za-z_]\w*)=(.*)$")
JSONL_TAIL = re.compile(r"([^\s\"'/]*\.jsonl)")
JSONL_LITERAL = re.compile(r"[\"']([^\"']*\.jsonl)[\"']")
PLACEHOLDER = re.compile(r"\{[^}]*\}|\$\{[^}]*\}|\$\w+|%[sd]")
TEMP_SOURCE = re.compile(r"mktemp|mkdtemp|mkstemp|tempfile|TemporaryDirectory|NamedTemporaryFile")
EMIT_SRC = re.compile(r"\bemit\(\s*[\"']([^\"']+)[\"']")
EMIT_JOURNAL = re.compile(r"\bemit\((?:[^()]|\([^()]*\))*?journal\s*=\s*[\"']([^\"']+)[\"']", re.S)
SH_EVENT = re.compile(r"\bharness_event\s+(\S+)\s+(\S+)")
HEREDOC_START = re.compile(r"(?<!<)<<(?!<)-?\s*[\"']?([A-Za-z_]\w*)[\"']?")
PYTHON_WORD = re.compile(r"^(?:\S*/)?python3?(?:\.\d+)?$")
SH_FUNC_NAME = re.compile(r"^(?:function\s+)?([\w-]+)\s*(?:\(\))?\s*\{\s*$")
SH_FUNC_START = re.compile(r"^(?:function\s+)?[\w-]+\s*(?:\(\))?\s*\{\s*$")
FUNC_END = re.compile(r"^\}\s*(?:#.*)?$")
SERVICE_ARG = re.compile(r"\$\{?1(?!\d)")
RUN_BEGIN = re.compile(r"^harness_run\s+\S+")
EXIT_TRAP = re.compile(r"^trap\s+(?:(?P<q>['\"])(?P<body>.*?)(?P=q)|(?P<fn>[^\s'\"]+))\s+(?:EXIT|0)\b")
RUN_END = re.compile(r"harness_run_end\s+\"\$rc\"")
CASE_START = re.compile(r"^case\b")
TEST_ONE_ARG = re.compile(r"^\[\[?\s.*\$\{?1(?!\d).*\]\]?\s*&&")
PY_ASSIGN = re.compile(r"^\s*([^\W\d][\w.]*(?:\s*,\s*[^\W\d][\w.]*)*)\s*(?::\s*[^=]+?)?=(?!=)\s*(.*)$")
IDENT = re.compile(r"[^\W\d]\w*")
PY_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+([^\W\d]\w*)")
NOT_JOURNAL_LITERAL = re.compile(r"[\"'][^\"']*\.(?:lock|log|tmp|pid)[\"']")
PY_OPEN = re.compile(r"\bopen\(")
PY_MODULES = frozenset({"os", "io", "codecs", "gzip", "bz2"})
PY_MODE_ARG = re.compile(r",\s*(?:mode\s*=\s*)?[\"']([abt+]*a[abt+]*)[\"']")
PY_MODE_RECEIVER = re.compile(r"\(\s*(?:mode\s*=\s*)?[\"']([abt+]*a[abt+]*)[\"']")
PY_O_APPEND = re.compile(r"\bO_" + "APPEND")
PY_ARGV = re.compile(r"sys\.argv\[(\d+)\]")
STATEMENT_LINES = 12


@dataclass(frozen=True)
class Site:
    """Место дозаписи: номер строки с 1 и имя файла журнала (с «*» на месте шаблона)."""
    line: int
    name: str


@dataclass
class Scan:
    sites: list[Site] = field(default_factory=list)
    emit_journals: set[str] = field(default_factory=set)
    event_journals: set[str] = field(default_factory=set)


def load_yaml(path: Path) -> dict:
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    with open(path, encoding="utf-8") as fh:
        return yaml.load(fh, Loader=loader) or {}


def load_events_lib():
    spec = importlib.util.spec_from_file_location("events_lib", SCRIPT_DIR / "lib" / "events.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPT_DIR / "lib"))
    sys.modules["events_lib"] = module
    spec.loader.exec_module(module)
    return module


# ───────────────────────── обход кода ─────────────────────────

def interpreter_kind(path: Path) -> str | None:
    """'py' | 'sh' | None: расширение, а без него — шебанг."""
    if path.suffix == ".py":
        return "py"
    if path.suffix == ".sh":
        return "sh"
    try:
        with open(path, "rb") as fh:
            head = fh.read(120)
    except OSError:
        return None
    first = head.split(b"\n", 1)[0]
    if not first.startswith(b"#!"):
        return None
    if re.search(rb"python", first):
        return "py"
    if re.search(rb"\b(bash|sh)\b", first):
        return "sh"
    return None


def code_files(root: Path) -> list[tuple[Path, str]]:
    """Судимый код: scripts/ и harness/ корня, без тестов и самой двери записи."""
    found = []
    for top in CODE_TOPS:
        for base, dirs, names in os.walk(root / top):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(names):
                path = Path(base) / name
                rel = path.relative_to(root).as_posix()
                if name.startswith("test_") or rel == DOOR_REL or path.is_symlink():
                    continue
                kind = interpreter_kind(path)
                if kind:
                    found.append((path, kind))
    return found


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def journal_glob(text: str) -> str:
    """Имя файла журнала из текста цели: после последнего «/», шаблоны — «*». Имя, где
    от журнала осталась одна «*» (`$DIR/$NAME.jsonl`), — пусто: оно подошло бы любому
    журналу, и ворота не привязывают его ни к одному (содержимое судит G2 по диску)."""
    name = re.sub(r"\$\{\w+:-([^}]*)\}?", r"\1", text.strip("\"'")).rsplit("/", 1)[-1]
    name = re.sub(r"\*+", "*", PLACEHOLDER.sub("*", name))
    return "" if name in ("*.jsonl", ".jsonl") else name


def balanced_statement(lines: list[str], index: int, column: int) -> str:
    """Текст от (строка, колонка) до закрытия скобок, не дальше STATEMENT_LINES строк."""
    parts, depth = [], 0
    for line in lines[index:index + STATEMENT_LINES]:
        part = line[column:] if not parts else line
        parts.append(part)
        depth += part.count("(") - part.count(")")
        if depth <= 0:
            break
    return "\n".join(parts)


def split_top_level(args: str) -> list[str]:
    parts, depth, current, quote = [], 0, "", ""
    for ch in args:
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += ch
    return [*parts, current]


# ───────────────────────── python: место дозаписи по цели ─────────────────────────

@dataclass
class PyIndex:
    """Что файл знает о путях: присвоения (и умолчания параметров), тела функций,
    имена, чьё КАЖДОЕ присвоение — путь из mktemp/tempfile, и константы-журналы
    других файлов (`cards.JOURNAL_NAME`)."""
    values: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    functions: dict[str, str] = field(default_factory=dict)
    temp: set[str] = field(default_factory=set)
    constants: dict[str, str] = field(default_factory=dict)


def python_index(lines: list[str], constants: dict[str, str]) -> PyIndex:
    """Имя с одним боевым присвоением стендом не считается: LOG_DIR из окружения и
    LOG_DIR из mkdtemp в самотесте — один и тот же журнал на боевом пути."""
    index = PyIndex(constants=constants)
    temp_as: set[str] = set()
    for i, line in enumerate(lines):
        if TEMP_SOURCE.search(line):
            temp_as |= set(re.findall(r"\bas\s+(\w+)", line))
        define = PY_DEF.match(line)
        if define:
            indent = len(define.group(1))
            body = []
            for inner in lines[i + 1:]:
                if inner.strip() and len(inner) - len(inner.lstrip()) <= indent:
                    break
                body.append(inner)
            index.functions[define.group(2)] = "\n".join(body)
            head = balanced_statement(lines, i, define.end(2))
            for arg in split_top_level(head[1:head.rfind(")")])[1:] if "(" in head else []:
                name, _, default = arg.partition("=")
                if default:
                    index.values.setdefault(name.split(":")[0].strip(), []).append((i + 1, default.strip()))
        match = PY_ASSIGN.match(line)
        if match:
            rhs = balanced_statement(lines, i, match.start(2))
            for lhs in match.group(1).split(","):
                index.values.setdefault(lhs.strip().rsplit(".", 1)[-1], []).append((i + 1, rhs))
    index.temp = {n for n, rhss in index.values.items() if all(TEMP_SOURCE.search(r) for _, r in rhss)} | temp_as
    return index


def python_journal_constants(text: str) -> dict[str, str]:
    """Верхний уровень: ИМЯ = "….jsonl" — то, на что ссылаются из других файлов."""
    return {m.group(1): journal_glob(m.group(2))
            for m in re.finditer(r"^([A-Z_][A-Z0-9_]*)\s*=\s*[\"']([^\"']*\.jsonl)[\"']", text, re.M)}


def assignments_before(index: PyIndex, ident: str, at: int) -> list[str]:
    """Присвоение имени, действующее в строке at: ближайшее выше неё (одно и то же имя
    в разных функциях — разные пути); выше нет — все, имя могло прийти константой снизу."""
    rows = index.values.get(ident, [])
    above = [text for line, text in rows if line <= at]
    return above[-1:] if above else [text for _, text in rows]


def resolve_journal(expr: str, index: PyIndex, at: int, depth: int = 4, seen: frozenset = frozenset()) -> str | None:
    """Имя журнала, на которое указывает выражение цели в строке at: литерал .jsonl в нём
    самом, в присвоении его имени, в теле вызванной функции или в константе другого файла.
    Замок/.log в выражении и путь из mktemp/tempfile — не журнал."""
    idents = set(IDENT.findall(expr))
    literals = JSONL_LITERAL.findall(expr)
    if literals:
        return None if idents & index.temp else journal_glob(literals[-1])
    if NOT_JOURNAL_LITERAL.search(expr) or depth == 0:
        return None
    for ident in sorted(idents - seen - index.temp):
        texts = [*assignments_before(index, ident, at), *([index.functions[ident]] if ident in index.functions else [])]
        for text in texts:
            found = resolve_journal(text, index, at, depth - 1, seen | {ident})
            if found:
                return found
        if ident in index.constants and ident not in index.values:
            return index.constants[ident]
    return None


def python_open_target(statement: str, call_start: int, line: str) -> tuple[str, bool] | None:
    """(выражение цели, дозапись ли) для open(…) / X.open(…) / os.open(…)."""
    receiver = re.search(r"([\w.]+)\.$", line[:call_start])
    if receiver and receiver.group(1).split(".")[-1] not in PY_MODULES:
        append = bool(PY_MODE_RECEIVER.search(statement) or re.search(r"\bmode\s*=\s*[\"'][abt+]*a", statement))
        return receiver.group(1), append
    first = split_top_level(statement[statement.index("(") + 1:])[0]
    append = bool(PY_MODE_ARG.search(statement) or PY_O_APPEND.search(statement)
                  or re.search(r"\bmode\s*=\s*[\"'][abt+]*a", statement))
    return first, append


def python_sites(text: str, constants: dict[str, str], argv: list[str] | None = None,
                 first_line: int = 1) -> list[Site]:
    if argv is not None:
        text = PY_ARGV.sub(lambda m: json.dumps(argv[int(m.group(1)) - 1] if 0 < int(m.group(1)) <= len(argv)
                                                else "<arg>", ensure_ascii=False), text)
    lines = text.split("\n")
    index = python_index(lines, constants)
    sites = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        for call in PY_OPEN.finditer(line):
            target = python_open_target(balanced_statement(lines, i, call.start()), call.start(), line)
            name = resolve_journal(target[0], index, i + 1) if target and target[1] else None
            if name:
                sites.append(Site(first_line + i, name))
    return sites


# ───────────────────────── bash: место дозаписи по цели ─────────────────────────

def mask_shell_line(line: str) -> str:
    """Строка с содержимым кавычек, замененным на «x», и без хвостового комментария."""
    out, quote, escaped = [], "", False
    for i, ch in enumerate(line):
        if escaped:
            out.append("x")
            escaped = False
        elif ch == "\\" and quote != "'":
            out.append("x")
            escaped = True
        elif quote:
            out.append(ch if ch == quote else "x")
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            break
        else:
            out.append(ch)
    return "".join(out)


def shell_targets(line: str) -> list[str]:
    """Цели >> и tee -a / tee --append одной строки; кавычки и комментарий не считаются."""
    masked = mask_shell_line(line)
    targets = [line[m.start(1):m.end(1)] for m in re.finditer(r">>\s*(\"[^\"]*\"|'[^']*'|[^\s;|&)]+)", masked)]
    for tee in re.finditer(r"\btee\b", masked):
        tail = masked[tee.end():]
        end = SH_TEE_FLAGS_END.search(tail)
        tail = tail[:end.start()] if end else tail
        tokens = [(m.group(0), tee.end() + m.start()) for m in SH_TOKEN.finditer(tail)]
        flags = [t for t, _ in tokens if t.startswith("-")]
        if any(f == "--append" or (not f.startswith("--") and "a" in f) for f in flags):
            targets += [line[at:at + len(t)] for t, at in tokens if not t.startswith("-")]
    return targets


def shell_heredocs(lines: list[str]) -> list[tuple[int, int, str]]:
    """[(строка открытия, строка терминатора, текст открытия)] с нуля."""
    spans, i = [], 0
    while i < len(lines):
        match = None if lines[i].lstrip().startswith("#") else HEREDOC_START.search(lines[i])
        if not match:
            i += 1
            continue
        end = i + 1
        while end < len(lines) and lines[end].strip() != match.group(1):
            end += 1
        spans.append((i, end, lines[i]))
        i = end + 1
    return spans


def shell_assignments(lines: list[str]) -> tuple[dict[str, str], set[str]]:
    """({переменная: имя журнала в значении}, переменные с присвоением из mktemp). Стендом
    считается только сама такая переменная: LOG_DIR="$T/log" в самотесте не делает боевой
    LOG_DIR стендом, потому что боевой приходит из конфига."""
    values: dict[str, list[str]] = {}
    for line in lines:
        match = SH_ASSIGN.match(line)
        if match:
            values.setdefault(match.group(1), []).append(match.group(2))
    temp = {n for n, vs in values.items() if any("mktemp" in v for v in vs)}
    names = {}
    for n, vs in values.items():
        for v in vs:
            if ".jsonl" in v and not set(SH_VAR_REF.findall(v)) & temp:
                names[n] = journal_glob(JSONL_TAIL.findall(v)[-1])
    return names, temp


def shell_functions(lines: list[str]) -> dict[str, str]:
    """{имя функции: текст тела} — для цели вида `$(имя_функции)`."""
    found = {}
    for line in lines:
        match = SH_FUNC_NAME.match(line)
        if match and match.group(1) not in found:
            found[match.group(1)] = "\n".join(function_body(lines, match.group(1)) or [])
    return found


def shell_target_name(target: str, names: dict[str, str], temp: set[str],
                      functions: dict[str, str] | None = None) -> str | None:
    text = target.strip("\"'")
    refs = set(SH_VAR_REF.findall(text))
    if refs & temp:
        return None
    if ".jsonl" in text:
        return journal_glob(text)
    for call in re.findall(r"\$\(\s*([\w-]+)", text):
        body = (functions or {}).get(call, "")
        if ".jsonl" in body:
            return journal_glob(JSONL_TAIL.findall(body)[-1])
    return next((names[r] for r in sorted(refs) if r in names), None)


def python_heredoc_args(opener: str, names: dict[str, str], temp: set[str]) -> list[str] | None:
    """Аргументы bash-вызова `python3 [-] арг… <<EOF` или None, если это не python."""
    match = HEREDOC_START.search(opener)
    tokens = SH_TOKEN.findall(opener[:match.start()])
    for i, token in enumerate(tokens):
        if PYTHON_WORD.match(token):
            args = tokens[i + 1:]
            args = args[1:] if args and args[0] == "-" else args
            return [shell_target_name(a, names, temp) or "<arg>" for a in args]
    return None


def shell_sites(text: str, constants: dict[str, str]) -> list[Site]:
    lines = text.split("\n")
    names, temp = shell_assignments(lines)
    functions = shell_functions(lines)
    spans = shell_heredocs(lines)
    body_lines = {n for start, end, _ in spans for n in range(start + 1, end + 1)}
    sites = []
    for i, line in enumerate(lines):
        if i in body_lines or line.lstrip().startswith("#"):
            continue
        for target in shell_targets(line):
            name = shell_target_name(target, names, temp, functions)
            if name:
                sites.append(Site(i + 1, name))
    for start, end, opener in spans:
        args = python_heredoc_args(opener, names, temp)
        if args is not None:
            sites += python_sites("\n".join(lines[start + 1:end]), constants, args, start + 2)
    return sorted(sites, key=lambda s: s.line)


# ───────────────────────── разбор файла ─────────────────────────

def scan_file(path: Path, kind: str, constants: dict[str, str]) -> Scan:
    text = read_text(path)
    scan = Scan(emit_journals=set(EMIT_JOURNAL.findall(text)) if kind == "py" else set())
    if kind == "py":
        scan.sites = python_sites(text, constants)
    else:
        scan.sites = shell_sites(text, constants)
        for line in text.split("\n"):
            m = SH_EVENT.search(line)
            if m and not line.lstrip().startswith("#"):
                scan.event_journals.add(m.group(1))
    return scan


def scan_all(files: list[tuple[Path, str]], root: Path) -> dict[str, Scan]:
    constants: dict[str, str] = {}
    for path, kind in files:
        if kind == "py":
            constants.update(python_journal_constants(read_text(path)))
    return {path.relative_to(root).as_posix(): scan_file(path, kind, constants) for path, kind in files}


def journal_files(registry: dict) -> dict[str, str]:
    """{ключ: имя файла} журналов реестра и закрытых от читалки (private_journals)."""
    files = {key: Path(str(e.get("file"))).name for key, e in (registry.get("journals") or {}).items()}
    files.update({str(e["name"]): Path(str(e["file"])).name for e in registry.get("private_journals") or []})
    return files


def journal_keys(name: str, registry: dict) -> set[str]:
    """Ключи, чей файл подходит имени места дозаписи (с «*» — шаблон месяца)."""
    return {key for key, base in journal_files(registry).items() if fnmatch.fnmatch(base, name)}


# ───────────────────────── демоны ─────────────────────────

def daemon_files(root: Path) -> list[str]:
    jobs = load_yaml(root / "harness/config/system-jobs.yaml").get("задания") or []
    files = []
    for job in jobs:
        words = str(job.get("команда", "")).split()
        if words and words[0] not in files:
            files.append(words[0])
    return files


def shell_top_level(lines: list[str]) -> list[tuple[int, str]]:
    """Строки без отступа вне heredoc и вне тела функции: (номер с 1, текст)."""
    result, heredoc_end, in_func = [], None, False
    for n, line in enumerate(lines, 1):
        if heredoc_end is not None:
            if line.strip() == heredoc_end:
                heredoc_end = None
            continue
        opener = None if line.lstrip().startswith("#") else HEREDOC_START.search(line)
        if opener:
            heredoc_end = opener.group(1)
        if in_func:
            in_func = not FUNC_END.match(line)
            continue
        if SH_FUNC_START.match(line):
            in_func = True
            continue
        if line and not line[0].isspace() and not line.startswith("#"):
            result.append((n, line))
    return result


def service_end(top: list[tuple[int, str]], flags: list[str]) -> int:
    """Номер последней строки последней служебной ветки; 0 — веток нет. Ветка — на
    верхнем уровне: if/elif на $1 или флаг из service_flags (до fi), case на $1
    (до esac) или `[ … $1 … ] && …` одной строкой."""
    flag_ref = re.compile(r"\$\{?(?:" + "|".join(map(re.escape, flags)) + r")\b") if flags else None
    judges = lambda line: bool(SERVICE_ARG.search(line) or (flag_ref and flag_ref.search(line)))
    end, chain_is_service, case_is_service = 0, False, False
    for n, line in top:
        if re.match(r"if\b", line):
            chain_is_service = False
        if re.match(r"(?:if|elif)\b", line) and judges(line):
            chain_is_service = True
        elif re.match(r"fi\b", line) and chain_is_service:
            end, chain_is_service = n, False
        if CASE_START.match(line):
            case_is_service = judges(line)
        elif re.match(r"esac\b", line) and case_is_service:
            end, case_is_service = n, False
        if TEST_ONE_ARG.match(line):
            end = n
    return end


def function_body(lines: list[str], name: str) -> list[str] | None:
    start = re.compile(rf"^(?:function\s+)?{re.escape(name)}\s*(?:\(\))?\s*\{{\s*$")
    for i, line in enumerate(lines):
        if start.match(line):
            body = []
            for inner in lines[i + 1:]:
                if FUNC_END.match(inner):
                    return body
                body.append(inner)
    return None


def trap_findings(n: int, trap: re.Match, lines: list[str]) -> list[str]:
    """Ловушка рабочего пути: тело начинается с rc=$? и зовёт harness_run_end "$rc"."""
    if trap.group("fn"):
        raw = function_body(lines, trap.group("fn"))
        if raw is None:
            return [f"строка {n}: ловушка {trap.group('fn')} EXIT — тела функции нет"]
        commands = [c.strip() for c in raw if c.strip() and not c.strip().startswith("#")]
        body = "\n".join(commands)
        first = commands[0] if commands else ""
    else:
        body = trap.group("body").strip()
        first = body
    if not first.startswith("rc=$?"):
        return [f"строка {n}: ловушка рабочего пути начинается не с rc=$?"]
    if not RUN_END.search(body):
        return [f"строка {n}: ловушка рабочего пути без harness_run_end \"$rc\""]
    return []


def shell_daemon_findings(text: str, flags: list[str]) -> list[str]:
    lines = text.split("\n")
    top = shell_top_level(lines)
    branch_end = service_end(top, flags)
    runs = [n for n, line in top if RUN_BEGIN.match(line)]
    traps = [(n, EXIT_TRAP.match(line)) for n, line in top if n > branch_end and EXIT_TRAP.match(line)]
    findings = []
    if not runs:
        findings.append("нет harness_run на рабочем пути")
    elif runs[0] < branch_end:
        findings.append(f"строка {runs[0]}: harness_run выше служебной ветки (до строки {branch_end})")
    if runs and not traps:
        findings.append("нет ловушки EXIT на рабочем пути: harness_run не закрыт harness_run_end")
    for n, trap in traps:
        findings += trap_findings(n, trap, lines)
    return findings


def daemon_findings(root: Path, flags: list[str]) -> dict[str, list[str]]:
    result = {}
    for rel in daemon_files(root):
        path = root / rel
        kind = interpreter_kind(path) if path.is_file() else None
        if kind == "sh":
            result[rel] = shell_daemon_findings(read_text(path), flags)
        elif kind == "py":
            result[rel] = [] if "events.run(" in read_text(path) else ["нет events.run("]
        else:
            result[rel] = ["файл демона не найден или не код"]
    return result


# ───────────────────────── литералы src и журналов ─────────────────────────

def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def src_literals(files: list[tuple[Path, str]], root: Path) -> list[tuple[str, int, str]]:
    found = []
    for path, kind in files:
        rel, text = path.relative_to(root).as_posix(), read_text(path)
        if kind == "py":
            found += [(rel, line_of(text, m.start()), m.group(1)) for m in EMIT_SRC.finditer(text)]
        else:
            for n, line in enumerate(text.split("\n"), 1):
                m = SH_EVENT.search(line)
                if m and not line.lstrip().startswith("#") and not m.group(2).startswith("$"):
                    found.append((rel, n, m.group(2)))
    return found


def journal_literals(files: list[tuple[Path, str]], root: Path) -> list[tuple[str, int, str]]:
    found = []
    for path, kind in files:
        rel, text = path.relative_to(root).as_posix(), read_text(path)
        if kind == "py":
            found += [(rel, line_of(text, m.start()), m.group(1)) for m in EMIT_JOURNAL.finditer(text)]
        else:
            for n, line in enumerate(text.split("\n"), 1):
                m = SH_EVENT.search(line)
                if m and not line.lstrip().startswith("#") and not m.group(1).startswith("$"):
                    found.append((rel, n, m.group(1)))
    return found


# ───────────────────────── долг ─────────────────────────

DebtItem = tuple[str, str]       # (файл, ключ журнала); у демонов ключ пуст


def debt_items(registry: dict, key: str) -> tuple[list[DebtItem], list[str]]:
    """(единицы долга, красные находки о записях без card или journals)."""
    items, problems = [], []
    for entry in registry.get(key) or []:
        file = str(entry.get("file", ""))
        if not str(entry.get("card") or "").strip():
            problems.append(f"{file}: запись долга {key} без card")
        journals = entry.get("journals")
        if key != "legacy_writers":
            items.append((file, ""))
        elif not journals:
            problems.append(f"{file}: запись долга {key} без journals")
        else:
            items += [(file, str(j)) for j in journals]
    return items, problems


def debt_base(root: Path, key: str) -> set[DebtItem] | None:
    """Единицы долга в HEAD; None — базы нет (нет коммита, файла или ключа). Запись
    старого вида (без journals) разрешает любой журнал своего файла."""
    shown = subprocess.run(["git", "-C", str(root), "show", f"HEAD:{REGISTRY_REL}"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if shown.returncode != 0:
        return None
    head = yaml.safe_load(shown.stdout) or {}
    if key not in head:
        return None
    base: set[DebtItem] = set()
    for entry in head[key] or []:
        journals = entry.get("journals") if key == "legacy_writers" else [""]
        base |= {(str(entry.get("file", "")), str(j)) for j in journals or [ANY_JOURNAL]}
    return base


def show_item(item: DebtItem) -> str:
    return f"{item[0]} · {item[1]}" if item[1] else item[0]


def judge_debt(root: Path, registry: dict, key: str, found: set[DebtItem], label: str,
               out: list[str], red: list[str]) -> tuple[list[DebtItem], int]:
    """Долг по ключу: card, рост против HEAD, находимость. Возвращает (единицы, найдено)."""
    items, problems = debt_items(registry, key)
    red += problems
    base = debt_base(root, key)
    if base is None:
        out.append(f"{key}: не судится: базы нет")
    else:
        red += [f"{show_item(i)}: долг {key} вырос против HEAD — новый писатель вместо перевода на emit"
                for i in items if i not in base and (i[0], ANY_JOURNAL) not in base]
    red += [f"{show_item(i)}: {label} в коде не найдено — вычеркни долг или почини обход"
            for i in items if i not in found]
    return items, sum(1 for i in items if i in found)


# ───────────────────────── G1 ─────────────────────────

def direct_write_findings(scans: dict[str, Scan], debt: list[DebtItem], registry: dict) -> list[str]:
    """Место дозаписи вне долга или в журнал, которого нет среди журналов долга файла."""
    allowed: dict[str, set[str]] = {}
    for file, journal in debt:
        allowed.setdefault(file, set()).add(journal)
    red = []
    for rel, scan in sorted(scans.items()):
        for site in scan.sites:
            if rel not in allowed:
                red.append(f"{rel}:{site.line}: прямая дозапись в .jsonl мимо events.emit")
            elif not journal_keys(site.name, registry) & allowed[rel]:
                red.append(f"{rel}:{site.line}: прямая дозапись в {site.name} — журнала нет среди "
                           f"долга файла ({', '.join(sorted(allowed[rel]))}): новый писатель")
    return red


def run_g1(root: Path, registry: dict) -> tuple[list[str], list[str]]:
    out, red = [], []
    files = code_files(root)
    scans = {rel: scan for rel, scan in scan_all(files, root).items() if scan.sites}
    found = {(rel, key) for rel, scan in scans.items() for site in scan.sites
             for key in journal_keys(site.name, registry)}
    items, found_n = judge_debt(root, registry, "legacy_writers", found, "прямой записи", out, red)
    red += direct_write_findings(scans, items, registry)
    out.insert(0, f"судимых файлов {len(files)}, прямых записей {sum(len(s.sites) for s in scans.values())}, "
                  f"в долге {len(items)}, долг найден {found_n} из {len(items)}")
    if not files:
        red.append("судимых файлов 0: пустой набор — обход сломан или дерево пусто")

    flags = [str(f) for f in registry.get("service_flags") or []]
    findings = daemon_findings(root, flags)
    bad = {(rel, "") for rel, f in findings.items() if f}
    items_d, found_d = judge_debt(root, registry, "legacy_daemons", bad, "нарушения меток", out, red)
    debt_d = {file for file, _ in items_d}
    for rel in sorted({file for file, _ in bad} - debt_d):
        red += [f"{rel}: {why}" for why in findings[rel]]
    out.append(f"демонов {len(findings)}, без меток {len(bad)}, в долге {len(items_d)}, "
               f"долг найден {found_d} из {len(items_d)}")

    passports = {str(m.get("файл")) for m in load_yaml(root / "harness/config/mechanisms.yaml").get("механизмы") or []}
    literals = src_literals(files, root)
    orphans = [(rel, n, src) for rel, n, src in literals if src not in passports]
    red += [f"{rel}:{n}: src «{src}» без паспорта в mechanisms.yaml" for rel, n, src in orphans]
    out.append(f"литералов src {len(literals)}, без паспорта {len(orphans)}")
    return out, red


# ───────────────────────── G2: код ↔ реестр ─────────────────────────

def writer_findings(root: Path, name: str, entry: dict, scans: dict[str, Scan]) -> list[str]:
    """Писатель — файл, где место дозаписи, emit(journal=…) или harness_event называет
    журнал; слово в докстроке, чтение и проба писателем не считаются."""
    writers = entry.get("writers")
    if writers == "none":
        return [] if str(entry.get("why") or "").strip() else [f"{name}: writers: none без why"]
    if not writers:
        return [f"{name}: нет ни writers, ни writers: none с причиной"]
    base = Path(entry["file"]).name
    problems = []
    for rel in writers:
        path = root / rel
        if not path.is_file():
            problems.append(f"{name}: писатель {rel} — файла нет")
        elif path.name.startswith("test_"):
            problems.append(f"{name}: писатель {rel} — проба test_*, а не писатель")
        else:
            scan = scans.get(rel) or scan_file(path, interpreter_kind(path) or "sh", {})
            writes = (any(fnmatch.fnmatch(base, s.name) for s in scan.sites)
                      or name in scan.emit_journals or name in scan.event_journals)
            if not writes:
                problems.append(f"{name}: писатель {rel} не пишет журнал {base} "
                                f"(ни места дозаписи, ни emit(journal=…), ни harness_event)")
    return problems


def reader_findings(root: Path, name: str, entry: dict) -> list[str]:
    if entry.get("rotate") != "monthly":
        return []
    problems = []
    for rel in entry.get("readers") or []:
        path = root / rel
        if not path.is_file() or "events.read(" not in read_text(path):
            problems.append(f"{name}: rotate: monthly, читатель {rel} не на events.read()")
    return problems


def run_registry(root: Path, registry: dict) -> tuple[list[str], list[str]]:
    journals = registry.get("journals") or {}
    out, red = [f"журналов в реестре {len(journals)}"], []
    if not journals:
        red.append("журналов в реестре 0: пустой набор — реестр не прочитан или пуст")
    files = code_files(root)
    scans = scan_all(files, root)
    names = set(journals)
    literals = journal_literals(files, root)
    red += [f"{rel}:{n}: журнал «{j}» не назван в реестре" for rel, n, j in literals if j not in names]
    known = set(journal_files(registry).values())
    sites = 0
    for rel, scan in sorted(scans.items()):
        for site in scan.sites:
            sites += 1
            if not any(fnmatch.fnmatch(base, site.name) for base in known):
                red.append(f"{rel}:{site.line}: запись в {site.name}, которого нет в реестре")
    out.append(f"судимых мест дозаписи {sites}, литералов журнала emit/harness_event {len(literals)}")
    for name, entry in journals.items():
        red += writer_findings(root, name, entry, scans)
        red += reader_findings(root, name, entry)
    return out, red


# ───────────────────────── G2: диск → реестр ─────────────────────────

def disk_files(log_dir: Path, ignore: set[str]) -> list[str]:
    found = []
    for base, dirs, names in os.walk(log_dir):
        dirs[:] = sorted(d for d in dirs if d not in ignore)
        for name in names:
            path = Path(base) / name
            if name.endswith(".jsonl") and not path.is_symlink():
                found.append(path.relative_to(log_dir).as_posix())
    return sorted(found)


def extra_lines(path: Path) -> int:
    """Строки без "v": 1 — битые, без времени и старого вида считаются одинаково."""
    extra = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                extra += 1
                continue
            extra += 0 if isinstance(row, dict) and row.get("v") == 1 else 1
    return extra


def migrated_findings(name: str, entry: dict, path: Path, events_lib) -> tuple[int, list[str]]:
    """(лишних строк, красные находки) одного переведённого журнала."""
    problems = []
    surplus = max(0, extra_lines(path) - int(entry.get("legacy_lines") or 0))
    if surplus:
        problems.append(f"{name}: лишних строк без \"v\": 1 — {surplus}")
    info = path.stat()
    want_mode = int(str(entry.get("mode") or "0640"), 8)
    if stat.S_IMODE(info.st_mode) != want_mode:
        problems.append(f"{name}: режим {stat.S_IMODE(info.st_mode):04o}, в реестре {want_mode:04o}")
    if info.st_gid != events_lib.events_gid():
        problems.append(f"{name}: группа файла не EVENTS_GROUP")
    return surplus, problems


def run_disk(root: Path, registry: dict) -> tuple[list[str], list[str]]:
    events_lib = load_events_lib()
    journals = registry.get("journals") or {}
    log_dir = Path(events_lib.log_dir())
    ignore = set(registry.get("ignore_dirs") or [])
    on_disk = disk_files(log_dir, ignore)
    registered = {e["file"] for e in journals.values() if e.get("dir") != "panel_state"}
    families = registry.get("disk_families") or []
    red = [f"{f}: файла нет в реестре журналов" for f in on_disk
           if f not in registered and not any(fnmatch.fnmatch(f, p) for p in families)]
    migrated = {n: e for n, e in journals.items() if e.get("migrated")}
    surplus_total, judged = 0, 0
    for name, entry in migrated.items():
        path = Path(events_lib.journal_file(entry))
        if not path.is_file():
            continue
        judged += 1
        surplus, problems = migrated_findings(name, entry, path, events_lib)
        surplus_total += surplus
        red += problems
    out = [f"журналов в реестре {len(journals)}, на диске {len(on_disk)}, переведённых {len(migrated)}, "
           f"лишних строк {surplus_total}"]
    if not journals:
        red.append("журналов в реестре 0: пустой набор — реестр не прочитан или пуст")
    if not migrated:
        out.append("не судится: переведённых 0")
    elif not judged:
        red.append(f"переведённых {len(migrated)}, судимых по (в)/(д) 0: пустой набор — файлов переведённых нет")
    return out, red


# ───────────────────────── вход ─────────────────────────

def repo_root() -> Path:
    shown = subprocess.run(["git", "-C", str(SCRIPT_DIR), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return Path(shown.stdout.strip()) if shown.returncode == 0 else SCRIPT_DIR.parent


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Ворота журналов G1/G2.")
    parser.add_argument("--root", default=None)
    parser.add_argument("--registry", action="store_true")
    parser.add_argument("--disk", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    root = Path(args.root).resolve() if args.root else repo_root()
    registry_file = Path(os.environ.get("HARNESS_JOURNALS") or root / REGISTRY_REL)
    registry = load_yaml(registry_file)
    out, red = [], []
    modes = []
    if not (args.registry or args.disk):
        modes.append(run_g1)
    if args.registry:
        modes.append(run_registry)
    if args.disk:
        modes.append(run_disk)
    for mode in modes:
        o, r = mode(root, registry)
        out += o
        red += r
    print("\n".join(out))
    if red:
        print("\n".join(red))
        print(f"КРАСНЫЙ: находок {len(red)}")
        return 1
    print("зелёный")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
