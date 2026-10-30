#!/usr/bin/env python3
"""PreToolUse на запись файла: заявка помощника на путь и предупреждение о чужой.

Помощники пишут в одном дереве, и `git commit -- путь` одного забирает в коммит
чужие незакоммиченные правки того же файла. Кто какой файл пишет — знает этот
хук: первая запись помощника в путь дописывает заявку {агент, полный путь} в
$LOG_DIR/заявки-на-файлы.jsonl (events.emit, событие file.claim). Запись в путь,
заявленный ДРУГИМ помощником за окно FILE_CLAIM_MIN минут, получает
предупреждение в additionalContext с именем занявшего и счётчиком — не отказ:
реестры правят все, вставкой по якорю. Для общих файлов (FILE_CLAIM_SHARED,
пути от корня дерева) текст свой: коммитить только вставку по якорю. Оба ключа —
данные harness.conf; ключа нет в установленном конфиге — берётся конфиг дерева.

Помощник узнаётся по agent_id во входе хука (как у model_guard.py); смена
заявок не пишет — её запись чужим занятием не считается. Заявка живёт, пока
помощник пишет: своя заявка старше половины окна обновляется следующей записью.

Bash заявляет то, что видно в тексте команды: перенаправление `>`/`>>`/`&>`,
`tee`, `sed -i`. Не видно: запись изнутри python/скриптов, cp/mv/git, пути в
переменных и подстановках.

Хук не роняет запись никогда: любая своя ошибка — код 0 и строка skipped.
Заявлять имеет смысл только пути дерева проекта и пакета: черновики в /tmp
журнал не засоряют.
"""
from __future__ import annotations

import datetime as dt
import functools
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import config                                            # noqa: E402
import events                                            # noqa: E402

# Имя журнала стоит литералом и в emit(): ворота G2 узнают писателя по нему.
JOURNAL = "заявки-на-файлы"
SRC = "scripts/hooks/file_claim_guard.py"
CLAIM_EVENT = "file.claim"
WARN_EVENT = "file.claim.warn"
WINDOW_KEY = "FILE_CLAIM_MIN"
SHARED_KEY = "FILE_CLAIM_SHARED"
# Окно в час — сотни заявок; хвоста в 1 МБ хватает с запасом, а журнал за
# месяц читать на каждой записи дорого.
TAIL_BYTES = 1 << 20
PATH_FIELD = {"Write": "file_path", "Edit": "file_path", "NotebookEdit": "notebook_path"}
REDIRECTS = (">", ">>", ">|", "&>", "&>>")
OPERATOR_CHARS = set("();<>|&")
UNEXPANDED = set("$`*?~")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def setting(name: str) -> str:
    """Ключ установленного harness.conf, иначе — конфига дерева."""
    tree_conf = config.корень_дерева(__file__) / "harness" / "config" / "harness.conf"
    return (config.из_файла(config.путь_harness_conf()).get(name)
            or config.из_файла(str(tree_conf)).get(name) or "")


def window_seconds() -> float:
    return float(setting(WINDOW_KEY)) * 60


def journal_path() -> Path:
    return Path(events.log_dir()) / f"{JOURNAL}.jsonl"


@functools.lru_cache(maxsize=1)
def current_task() -> str:
    # Трейлер Dev-Map из истории называет ПРЕДЫДУЩУЮ задачу — не берём.
    task, source = events.current_task()
    return task if source in ("env", "map") and task else "-"


def record(event: str, outcome: str, session: str, data: dict) -> None:
    events.emit(SRC, event, outcome, journal="заявки-на-файлы", task=current_task(),
                session=session or None, data=data)


def command_targets(words: list) -> list:
    """Файлы, которые пишет простая команда сама: tee и sed -i."""
    if not words:
        return []
    name, args = os.path.basename(words[0]), words[1:]
    if name == "tee":
        return [a for a in args if not a.startswith("-")]
    in_place = any(a == "--in-place" or (a.startswith("-i") and not a.startswith("--"))
                   for a in args)
    if name != "sed" or not in_place:
        return []
    files, script_given, skip = [], False, False
    for arg in args:
        if skip:
            skip = False
        elif arg in ("-e", "-f", "--expression", "--file"):
            script_given = skip = True
        elif not arg.startswith("-"):
            files.append(arg)
    return files if script_given else files[1:]


def strip_heredocs(command: str) -> str:
    """Команда без тел here-doc: тело — данные, его `>` ничего не пишет."""
    kept, ends = [], []
    for line in command.split("\n"):
        if ends:
            if line.strip() == ends[0]:
                ends.pop(0)
            continue
        kept.append(line)
        ends += [m.group(2) for m in HEREDOC.finditer(line)]
    return "\n".join(kept)


def bash_targets(command: str) -> list:
    """Пути, в которые команда пишет по своему тексту; разобрать нельзя — пусто."""
    lexer = shlex.shlex(strip_heredocs(command).replace("\n", " ; "), posix=True,
                        punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    found, words, i = [], [], 0
    while i < len(tokens):
        token = tokens[i]
        if token in REDIRECTS:
            if words and words[-1].isdigit():
                words.pop()                 # номер потока, а не аргумент
            if i + 1 < len(tokens) and not set(tokens[i + 1]) <= OPERATOR_CHARS:
                found.append(tokens[i + 1])
            i += 2
            continue
        if set(token) <= OPERATOR_CHARS:
            found += command_targets(words)
            words = []
        else:
            words.append(token)
        i += 1
    found += command_targets(words)
    return [p for p in found if p and not p.startswith(("/dev/", "&")) and not set(p) & UNEXPANDED]


def targets(payload: dict) -> list:
    """Полные пути записи в пределах дерева проекта и пакета."""
    tool = payload.get("tool_name") or ""
    args = payload.get("tool_input") or {}
    if tool in PATH_FIELD:
        raw = [str(args.get(PATH_FIELD[tool]) or "")]
    elif tool == "Bash":
        raw = bash_targets(str(args.get("command") or ""))
    else:
        return []
    cwd = str(payload.get("cwd") or os.getcwd())
    roots = [str(config.корень_дерева(__file__)), str(config.katalog_paketa())]
    paths = [os.path.normpath(os.path.join(cwd, p)) for p in raw if p]
    return [p for p in dict.fromkeys(paths)
            if any(p.startswith(root.rstrip("/") + "/") for root in roots)]


def recent_rows(now: float, window_s: float) -> list:
    """Строки хука за окно; к строке добавлен её возраст `_age` в секундах."""
    try:
        with journal_path().open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - TAIL_BYTES))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
            age = now - dt.datetime.fromisoformat(row["ts"]).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(row, dict) and row.get("src") == SRC and age <= window_s:
            row["_age"] = age
            rows.append(row)
    return rows


def holders(rows: list) -> dict:
    """Путь → {агент: его последняя заявка}; журнал идёт по времени."""
    found: dict = {}
    for row in rows:
        data = row.get("data") or {}
        if row.get("event") == CLAIM_EVENT and row.get("outcome") == "ok":
            found.setdefault(data.get("путь"), {})[data.get("агент")] = row
    return found


def describe(agent: str, row: dict) -> str:
    data = row.get("data") or {}
    return (f"{agent} ({data.get('тип') or 'тип не назван'}, задача {row.get('task') or '-'}, "
            f"{int(row['_age'] // 60)} мин назад)")


def warning(path: str, others: dict, shared: bool, number: int) -> str:
    names = "; ".join(describe(a, r) for a, r in others.items())
    if shared:
        busy = f" Сейчас его пишут: {names}." if others else ""
        return (f"{path} — общий файл ({SHARED_KEY} в harness.conf): его правят все, "
                f"вставкой по якорю.{busy} Коммить только свою вставку по якорю: "
                f"`git commit -- путь` берёт ВЕСЬ файл вместе с чужими незакоммиченными "
                f"правками; перед коммитом смотри `git diff -- {path}`. "
                f"Предупреждение №{number} по этому пути.")
    return (f"Файл {path} уже пишет другой помощник: {names}. Не отказ — но перед "
            f"коммитом смотри `git diff -- {path}`: `git commit -- путь` заберёт и его "
            f"незакоммиченные правки. Кто что занял — python3 scripts/who-writes.py. "
            f"Предупреждение №{number} по этому пути.")


def decide(payload: dict) -> str:
    """Текст предупреждения или пусто; заявки и предупреждения — строками журнала."""
    agent = str(payload.get("agent_id") or "")
    if not agent:
        return ""
    paths = targets(payload)
    if not paths:
        return ""
    window_s = window_seconds()
    shared = setting(SHARED_KEY).split()
    rows = recent_rows(time.time(), window_s)
    claims = holders(rows)
    session = str(payload.get("session_id") or "")
    texts = []
    for path in paths:
        others = {a: r for a, r in claims.get(path, {}).items() if a != agent}
        mine = claims.get(path, {}).get(agent)
        if mine is None or mine["_age"] > window_s / 2:
            record(CLAIM_EVENT, "ok", session, {"путь": path, "агент": agent,
                                                 "тип": payload.get("agent_type") or None})
        is_shared = any(path.endswith("/" + rel.strip("/")) for rel in shared)
        if not others and not (is_shared and mine is None):
            continue
        number = 1 + sum(1 for r in rows if r.get("event") == WARN_EVENT
                         and (r.get("data") or {}).get("путь") == path
                         and (r.get("data") or {}).get("агент") == agent)
        record(WARN_EVENT, "ok", session, {"путь": path, "агент": agent, "общий": is_shared,
                                            "занявшие": sorted(others), "номер": number})
        texts.append(warning(path, others, is_shared, number))
    return "\n\n".join(texts)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        text = decide(payload) if isinstance(payload, dict) else ""
    except Exception as exc:  # noqa: BLE001 — своя ошибка хука не роняет запись
        record(CLAIM_EVENT, "skipped", "", {"почему": f"{type(exc).__name__}: {exc}"[:300]})
        return 0
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "additionalContext": text}},
                         ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
